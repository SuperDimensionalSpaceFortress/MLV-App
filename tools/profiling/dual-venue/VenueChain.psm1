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
# ticks>.ticket.json, taken by CreateNew (so names sort in arrival order and two takers can never share one; a clash moves to the next tick, so the 19 digits are
# the arrival tick plus that bump). Equal ticks order by owner pid, then by owner start ticks, never by directory enumeration. Only the OLDEST LIVE ticket may try the
# claim (Request-VenueClaim -UseTicket); every other ticket holder gets state QUEUED. A ticket is STALE when its owner pid is dead (dead-pid), was reused
# (pid-reused), has not refreshed its heartbeat for staleAfterSec (heartbeat; default 3 x ClaimPollSec, widened by the owner to the probe's real upper bound
# while it sits in a quiet probe, Get-VenueTicketProbeStaleSec; a dead or reused pid is still stale at once),
# or has been unparseable for more than a writer needs (unreadable); the next waiter removes it (Request-VenueClaim reports it in .staleTickets) and the line
# moves up. A chain that releases its claim on GATE_BUSY keeps its ticket (it does not go to the back of the line) for a bounded number of cycles
# (Invoke-VenueChain.ps1 -TicketKeepMax); Remove-VenueTicket ends it.
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

function Get-VenueTicketProbeStaleSec {
    <#
    .SYNOPSIS
    How long a ticket may go without a heartbeat while its owner sits in ONE Wait-VenueQuiet probe: the probe's real upper bound plus a margin. Wait-VenueQuiet stops
    re-probing at -MaxWaitSec, but a probe already submitted runs on under um-run.ps1: its queue wait (-MaxQueueWaitSec 300), the agent's admission gate
    (-MaxAdmissionWaitSec, passed by the chain) and the claimed job (-TimeoutSec 120 + -MaxClaimedWaitSec 120).
    #>
    param([Parameter(Mandatory)][ValidateRange(0, 86400)][int]$QuietMaxWaitSec, [Parameter(Mandatory)][ValidateRange(1, 86400)][int]$AdmissionWaitSec,
        [ValidateRange(0, 86400)][int]$MarginSec = 90)
    $queueWaitSec = 300; $jobBudgetSec = 120; $claimedGraceSec = 120   # the um-run call in Wait-VenueQuiet.ps1
    $QuietMaxWaitSec + $queueWaitSec + $AdmissionWaitSec + $jobBudgetSec + $claimedGraceSec + $MarginSec
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
            $row = [ordered]@{ name = $file.Name; path = $file.FullName; key = [int64]$m.Groups[1].Value; pidKey = [decimal]$m.Groups[2].Value; startKey = [decimal]$m.Groups[3].Value; state = 'LIVE'; reason = $null; text = $f.text; ticket = $null; position = 0; ageSec = $null; heartbeatAgeSec = $null }
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
    $sorted = @($rows | Sort-Object -Property key, pidKey, startKey, name)   # arrival tick, then owner pid, then owner start ticks: equal ticks never fall back to enumeration order
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

function Update-VenueClaim {
    <#
    .SYNOPSIS
    BACHELOR-LEASE-BATCH-LEGS-1 r5 (dot 242171b3 B1): move the expiry of the venue's claim to now + -TtlSec, if it is still the one with -Nonce. The file is opened
    exclusively (FileShare.None: a reader sees BUSY, never a half-written claim), re-read, and only its expiresUtc is rewritten in place; every other byte stays, so a
    breaker that judged the old text finds it CHANGED and never removes a renewed claim. Returns @{ state (RENEWED | NOT_OURS | ABSENT | BUSY); expiresUtc }.
    #>
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$Nonce, [ValidateRange(60, 86400)][int]$TtlSec = 10800, [string]$ClaimDir = '')
    $path = Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return [pscustomobject]@{ state = 'ABSENT'; expiresUtc = $null } }
        $fs = $null
        try { $fs = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) } catch { Start-Sleep -Milliseconds (50 * $attempt); continue }
        try {
            $sr = [IO.StreamReader]::new($fs, [Text.UTF8Encoding]::new($false), $false, 4096, $true)
            $text = $sr.ReadToEnd(); $sr.Dispose()
            $j = $null; try { $j = $text | ConvertFrom-Json -ErrorAction Stop } catch { $j = $null }
            if ($null -eq $j -or [string]$j.nonce -cne $Nonce -or $text -cnotmatch '"expiresUtc":"[^"]*"') { return [pscustomobject]@{ state = 'NOT_OURS'; expiresUtc = $null } }
            $expires = [DateTime]::UtcNow.AddSeconds($TtlSec).ToString('o')
            $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($text -creplace '"expiresUtc":"[^"]*"', ('"expiresUtc":"' + $expires + '"')))
            $fs.SetLength(0); $fs.Position = 0
            $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true)
            return [pscustomobject]@{ state = 'RENEWED'; expiresUtc = $expires }
        } finally { $fs.Dispose() }
    }
    [pscustomobject]@{ state = 'BUSY'; expiresUtc = $null }
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

# SUPPLEMENTARY LEGS (VENUE-UM-SUPPLEMENTARY-WHEN-BACHELOR-HELD-1). A Bachelor chain may offer the same leg on ultra-magnus as a SUPPLEMENTARY run (Invoke-VenueChain.ps1).
# These helpers only READ (the spec, the venue's claim and line, the venue's staged-build cache) or run one bounded child; none of them claims, stages, builds or submits.

function Get-VenueSupplementarySpecSkip {
    <#
    .SYNOPSIS
    $null when the leg spec carries a criteria.supplementary block for the backend (an empty list counts: the leg is informational there), else the typed skip reason:
    SPEC_UNREADABLE | BACKEND_NOT_IN_SPEC | NO_SUPPLEMENTARY_CRITERIA. -Backend '' means the spec's first backend, as Invoke-VenueLeg does.
    #>
    param([Parameter(Mandatory)][string]$SpecPath, [string]$Backend = '')
    $spec = $null
    try { $spec = [IO.File]::ReadAllText($SpecPath) | ConvertFrom-Json -ErrorAction Stop } catch { return 'SPEC_UNREADABLE' }
    $backends = @($spec.backends | Where-Object { $_ })
    $b = $(if ($Backend) { $Backend } elseif ($backends.Count -gt 0) { [string]$backends[0] } else { '' })
    if ($backends.Count -gt 0 -and $backends -cnotcontains $b) { return 'BACKEND_NOT_IN_SPEC' }
    $block = $null
    if ($null -ne $spec.criteria -and $null -ne $spec.criteria.supplementary -and $b) { $block = $spec.criteria.supplementary.PSObject.Properties[$b] }
    if ($null -eq $block) { return 'NO_SUPPLEMENTARY_CRITERIA' }
    $null
}

function Get-VenueSupplementaryIdle {
    <#
    .SYNOPSIS
    Is the venue idle for a supplementary leg? Read-only, from the venue's own claim and line (never a process scan): idle when its claim is FREE or STALE and no LIVE ticket
    queues. .holder names the live claim or the queue when it is not. -OwnNonce: the caller already holds the claim with that nonce (it does not count as held), so the
    answer is "still no live ticket" -- the revalidation a supplementary claim makes BEFORE any work (Dot 180e287f: a waiter may queue between the first check and the claim).
    #>
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', [string]$OwnNonce = '')
    $s = Get-VenueClaimStatus -Venue $Venue -ClaimDir $ClaimDir
    $live = @($s.tickets | Where-Object { $_.state -cne 'STALE' })
    $held = ($s.claim.state -cin @('LIVE', 'BUSY')) -and -not ($OwnNonce -and $s.claim.claim -and [string]$s.claim.claim.nonce -ceq $OwnNonce)
    $holder = $(if ($s.claim.claim) { "pid=$($s.claim.claim.ownerPid) runDir=$($s.claim.claim.runDir)" } else { "claimState=$($s.claim.state)" })
    [pscustomobject]@{ idle = (-not $held -and $live.Count -eq 0); claimState = [string]$s.claim.state; liveTickets = $live.Count; holder = $holder }
}

function Get-VenueStagedBuildManifest {
    <#
    .SYNOPSIS
    The sha256 of the build manifest already staged on a venue (<AgentShare>\cache\playback-attr-3-cuda-<sha12>-build.json, the file Invoke-VenueLeg hashes), read
    on a worker thread and given up after -TimeoutSec (a UNC path of a host that is down can block a Test-Path for minutes). Never stages anything.
    .OUTPUTS
    [pscustomobject]@{ state (STAGED | NOT_STAGED | UNREACHABLE); sha256; path; error }
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit, [ValidateRange(1, 600)][int]$TimeoutSec = 30, [scriptblock]$ProbeScript = $null)
    $path = $AgentShare.TrimEnd('\') + '\cache\playback-attr-3-cuda-' + $SourceCommit.Substring(0, 12) + '-build.json'
    # -ProbeScript is the offline test seam for a read that never answers; production passes none.
    $read = $(if ($null -ne $ProbeScript) { $ProbeScript } else { { param($p) if (Test-Path -LiteralPath $p -PathType Leaf) { (Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant() } else { '' } } })
    $r = Invoke-VenueBoundedRead -ScriptText $read.ToString() -Argument $path -TimeoutSec $TimeoutSec
    if (-not $r.answered) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = $r.error } }
    $sha = $(if ($r.value.Count -gt 0) { [string]$r.value[0] } else { '' })
    if ($sha -cmatch '^[0-9a-f]{64}$') { return [pscustomobject]@{ state = 'STAGED'; sha256 = $sha; path = $path; error = $null } }
    [pscustomobject]@{ state = 'NOT_STAGED'; sha256 = $null; path = $path; error = $null }
}

$script:VenueAbandonedReads = 0

function Complete-VenueProcess {
    <#
    .SYNOPSIS
    The last call of a script that used Invoke-VenueBoundedRead. An abandoned read is still a live (foreground) thread, and the host waits for it when the script ends, so a read
    stuck on a dead UNC host would hold the process for as long as it lasts. When one was abandoned, flush and exit NOW with -ExitCode (everything was already written); else return.
    #>
    param([Parameter(Mandatory)][int]$ExitCode)
    if ($script:VenueAbandonedReads -gt 0) { [Console]::Out.Flush(); [Console]::Error.Flush(); [Environment]::Exit($ExitCode) }
}

function Invoke-VenueBoundedRead {
    <#
    .SYNOPSIS
    Run one share read (-ScriptText, handed -Argument) on a worker runspace and give up on it after -TimeoutSec: .answered is $false and .error says so. A worker that does not
    answer is ABANDONED, never joined: BeginStop only asks it to stop, and it is never Dispose()d (Dispose() stops a running pipeline SYNCHRONOUSLY and waits for it, so one read
    stuck in the kernel on a dead UNC host would hold the caller for as long as the read lasts and break the bound). The leaked runspace dies with the process.
    .OUTPUTS
    [pscustomobject]@{ answered; value (the script's output as an array); error }
    #>
    param([Parameter(Mandatory)][string]$ScriptText, [Parameter(Mandatory)]$Argument, [Parameter(Mandatory)][ValidateRange(1, 600)][int]$TimeoutSec)
    $ps = [powershell]::Create()
    $abandoned = $false
    try {
        [void]$ps.AddScript($ScriptText)
        [void]$ps.AddArgument($Argument)
        $async = $ps.BeginInvoke()
        if (-not $async.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($TimeoutSec))) {
            $abandoned = $true
            $script:VenueAbandonedReads++
            try { [void]$ps.BeginStop($null, $null) } catch { }
            return [pscustomobject]@{ answered = $false; value = @(); error = "no answer in $TimeoutSec s" }
        }
        [pscustomobject]@{ answered = $true; value = @($ps.EndInvoke($async)); error = $null }
    } catch {
        [pscustomobject]@{ answered = $false; value = @(); error = $_.Exception.Message }
    } finally { if (-not $abandoned) { $ps.Dispose() } }
}

function Get-VenueRemoteJobIds {
    <#
    .SYNOPSIS
    The remote job ids a (killed) client printed before it went silent: every `DVE_JOB_ID=<id>` line of its stdout, in order, distinct. A line whose id is not a plain file name
    (the id becomes a file name on the venue share) is kept as an EMPTY string, so the caller knows a job was named that it cannot look up. Nothing printed = no ids.
    #>
    param([string]$Text)
    $ids = [System.Collections.Generic.List[string]]::new()
    foreach ($m in [regex]::Matches([string]$Text, '(?m)^DVE_JOB_ID=([^\r\n]*?)[ \t]*\r?$')) {
        $id = $m.Groups[1].Value
        if ($id -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { $id = '' }
        if (-not $ids.Contains($id)) { $ids.Add($id) }
    }
    $ids.ToArray()
}

function Get-VenueRemoteJobState {
    <#
    .SYNOPSIS
    Has every remote job a killed client submitted landed its result on the venue? Read by NAME only (<AgentShare>\outbox\<jobId>.result.json, the file um-run.ps1 waits for;
    the share is never listed), on one bounded worker. A released local claim says nothing about the remote job: FINISHED needs every id's result present, and everything else is
    busy -- NOT_FINISHED (some result absent: queued, running or lost), UNKNOWN (no job id recoverable, an unusable id, or the share did not answer in -TimeoutSec). None of them is idle.
    .OUTPUTS
    [pscustomobject]@{ state (FINISHED | NOT_FINISHED | UNKNOWN); jobIds; pending; error }
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [string[]]$JobId = @(), [ValidateRange(1, 600)][int]$TimeoutSec = 30)
    $ids = @($JobId)
    $out = { param($state, $pending, $err) [pscustomobject]@{ state = $state; jobIds = $ids; pending = @($pending); error = $err } }
    if ($ids.Count -eq 0) { return & $out 'UNKNOWN' @() 'no remote job id recoverable from what the client printed' }
    if (@($ids | Where-Object { $_ -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$' }).Count -gt 0) { return & $out 'UNKNOWN' @() 'a printed remote job id is not a usable file name' }
    $paths = @($ids | ForEach-Object { $AgentShare.TrimEnd('\') + '\outbox\' + $_ + '.result.json' })
    $r = Invoke-VenueBoundedRead -ScriptText '$o = foreach ($p in $args[0]) { Test-Path -LiteralPath $p -PathType Leaf }; $o' -Argument $paths -TimeoutSec $TimeoutSec
    if (-not $r.answered) { return & $out 'UNKNOWN' @() $r.error }
    $landed = @($r.value | ForEach-Object { [bool]$_ })
    if ($landed.Count -ne $ids.Count) { return & $out 'UNKNOWN' @() "the share answered $($landed.Count) of $($ids.Count) reads" }
    $pending = @(for ($i = 0; $i -lt $ids.Count; $i++) { if (-not $landed[$i]) { $ids[$i] } })
    if ($pending.Count -eq 0) { return & $out 'FINISHED' @() $null }
    & $out 'NOT_FINISHED' $pending $null
}

function Get-VenueJobResult {
    <#
    .SYNOPSIS
    One venue job's result.json, read by NAME (<AgentShare>\outbox\<JobId>.result.json; the share is never listed) on one bounded worker (BACHELOR-LEASE-BATCH-LEGS-1: the
    batch runner reads a leg's admission from it, to prove the leg ran on the batch's lease).
    .OUTPUTS
    [pscustomobject]@{ state (FOUND | ABSENT | UNKNOWN); result (the parsed result when FOUND); error }
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [string]$JobId = '', [ValidateRange(1, 600)][int]$TimeoutSec = 30)
    if ($JobId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { return [pscustomobject]@{ state = 'UNKNOWN'; result = $null; error = 'no usable job id' } }
    $path = $AgentShare.TrimEnd('\') + '\outbox\' + $JobId + '.result.json'
    $r = Invoke-VenueBoundedRead -ScriptText 'if (Test-Path -LiteralPath $args[0] -PathType Leaf) { [IO.File]::ReadAllText($args[0]) } else { "<ABSENT>" }' -Argument $path -TimeoutSec $TimeoutSec
    if (-not $r.answered) { return [pscustomobject]@{ state = 'UNKNOWN'; result = $null; error = $r.error } }
    $text = [string](@($r.value) -join "`n")
    if ($text -ceq '<ABSENT>') { return [pscustomobject]@{ state = 'ABSENT'; result = $null; error = $null } }
    try { return [pscustomobject]@{ state = 'FOUND'; result = ($text | ConvertFrom-Json); error = $null } }
    catch { return [pscustomobject]@{ state = 'UNKNOWN'; result = $null; error = "result.json does not parse: $($_.Exception.Message)" } }
}

function Request-VenueJobCancel {
    <#
    .SYNOPSIS
    Ask the venue agent to cancel one job (BACHELOR-LEASE-BATCH-LEGS-1 r3): <AgentShare>\cancel\<JobId>.cancel, the agent's cooperative cancel request (ultra-magnus-agent.ps1
    header). Created with FileMode.CreateNew, so it never replaces a file, on one bounded worker, then checked by name. The agent reads it at the top of every loop of its
    admission gate (before any lease request), again after admission and before launch, and every slice of a running job. The cancel folder is the agent's: it is never created here.
    .OUTPUTS
    [pscustomobject]@{ state (REQUESTED | UNKNOWN); path; error }
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [string]$JobId = '', [ValidateRange(1, 600)][int]$TimeoutSec = 30)
    if ($JobId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { return [pscustomobject]@{ state = 'UNKNOWN'; path = $null; error = 'no usable job id' } }
    $path = $AgentShare.TrimEnd('\') + '\cancel\' + $JobId + '.cancel'
    $write = @'
$p = $args[0]
if (-not (Test-Path -LiteralPath (Split-Path -Parent $p) -PathType Container)) { '<NO_CANCEL_DIR>'; return }
if (-not (Test-Path -LiteralPath $p -PathType Leaf)) {
    try { $fs = [IO.File]::Open($p, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None); try { $bytes = [Text.Encoding]::ASCII.GetBytes('cancel'); $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() } } catch [IO.IOException] { }
}
if (Test-Path -LiteralPath $p -PathType Leaf) { '<PRESENT>' } else { '<ABSENT>' }
'@
    $r = Invoke-VenueBoundedRead -ScriptText $write -Argument $path -TimeoutSec $TimeoutSec
    if (-not $r.answered) { return [pscustomobject]@{ state = 'UNKNOWN'; path = $path; error = $r.error } }
    $said = [string](@($r.value) -join '')
    if ($said -ceq '<PRESENT>') { return [pscustomobject]@{ state = 'REQUESTED'; path = $path; error = $null } }
    [pscustomobject]@{ state = 'UNKNOWN'; path = $path; error = $(if ($said -ceq '<NO_CANCEL_DIR>') { 'the agent share has no cancel folder' } else { 'the cancel file is not there after the write' }) }
}

# BACHELOR-LEASE-BATCH-LEGS-1 r5. KEEPERS: an in-process worker runspace that runs one step every -EverySec while its owner lives, so a renewal goes on while the
# owner's own thread is blocked (in um-run.ps1, a by-name wait, a leg). It dies with the owner's process, which is the point: what it renews then lapses.
$script:VenueKeeperLoop = @'
param($S, $Body, $A, $Module)
Import-Module $Module -DisableNameChecking
$step = [scriptblock]::Create($Body)
while (-not $S.stop -and -not $S.done) {
    $until = [DateTime]::UtcNow.AddSeconds($S.everySec)
    while (-not $S.stop -and [DateTime]::UtcNow -lt $until) { Start-Sleep -Milliseconds 100 }
    if ($S.stop) { break }
    try { $S.last = [string](& $step $A $S); $S.ok++ } catch { $S.errors++; $S.lastError = ($_.Exception.Message -replace '\s+', ' ') }
    $S.runs++
}
'@

function Start-VenueKeeper {
    # One keeper: runs -StepText (param($A, $S); this module is imported) every -EverySec, the first one -EverySec from now, until Stop-VenueKeeper or until a step
    # sets $S.done. $S is a synchronized
    # hashtable: runs, ok, errors, last, lastError, plus whatever the step keeps there.
    param([Parameter(Mandatory)][string]$StepText, $Argument = $null, [Parameter(Mandatory)][ValidateRange(1, 3600)][int]$EverySec, [hashtable]$State = @{})
    $s = [hashtable]::Synchronized(@{ stop = $false; done = $false; everySec = $EverySec; runs = 0; ok = 0; errors = 0; last = $null; lastError = $null })
    foreach ($k in $State.Keys) { $s[$k] = $State[$k] }
    $ps = [powershell]::Create()
    [void]$ps.AddScript($script:VenueKeeperLoop).AddArgument($s).AddArgument($StepText).AddArgument($Argument).AddArgument((Join-Path $PSScriptRoot 'VenueChain.psm1'))
    [pscustomobject]@{ ps = $ps; async = $ps.BeginInvoke(); state = $s }
}

function Stop-VenueKeeper {
    # Stops a keeper and waits up to -TimeoutSec for its step to finish. A step that does not finish (a share write stuck on a dead host) is ABANDONED as
    # Invoke-VenueBoundedRead abandons a read: never Dispose()d, counted, so Complete-VenueProcess ends the process. Returns $true when it stopped in time.
    param($Keeper, [ValidateRange(1, 600)][int]$TimeoutSec = 15)
    if ($null -eq $Keeper) { return $true }
    $Keeper.state.stop = $true
    if ($Keeper.async.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($TimeoutSec))) {
        try { [void]$Keeper.ps.EndInvoke($Keeper.async) } catch { }
        $Keeper.ps.Dispose()
        return $true
    }
    $script:VenueAbandonedReads++
    try { [void]$Keeper.ps.BeginStop($null, $null) } catch { }
    $false
}

function Start-VenueClaimKeeper {
    <#
    .SYNOPSIS
    r5 (dot 242171b3 B1): renew the venue claim (Update-VenueClaim) every -RenewSec while the owner lives, so a wait longer than -TtlSec (a foreign-refusal wait for the
    lease, a by-name wait, the legs) never lets Get-VenueClaimStaleReason call a live owner's claim expired. A claim found NOT_OURS or ABSENT is lost: renewing stops.
    #>
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$Nonce, [ValidateRange(60, 86400)][int]$TtlSec = 10800, [ValidateRange(1, 3600)][int]$RenewSec = 600, [string]$ClaimDir = '')
    $step = @'
param($A, $S)
$r = Update-VenueClaim -Venue $A.venue -Nonce $A.nonce -TtlSec $A.ttlSec -ClaimDir $A.claimDir
if ($r.state -ceq 'RENEWED') { $S.renews++; $S.expiresUtc = $r.expiresUtc }
elseif ($r.state -ceq 'NOT_OURS' -or $r.state -ceq 'ABSENT') { $S.lost = $r.state; $S.done = $true }
$r.state
'@
    Start-VenueKeeper -StepText $step -Argument ([pscustomobject]@{ venue = $Venue; nonce = $Nonce; ttlSec = $TtlSec; claimDir = $ClaimDir }) -EverySec $RenewSec -State @{ renews = 0; expiresUtc = $null; lost = $null; renewSec = $RenewSec; ttlSec = $TtlSec }
}

function Get-VenueClaimKeeperLoss {
    <#
    .SYNOPSIS
    r5b (Grok G1, 6389240991): is a claim held under Start-VenueClaimKeeper still the holder's? $null while it is. Lost when the keeper found it NOT_OURS or ABSENT, or
    when the last good expiry has passed (the keeper's last renewal, else -ClaimExpiresUtc, the expiry the claim was taken with; an unreadable one counts as passed):
    renewals that keep coming back BUSY or throwing leave the claim to expire. Returns @{ reason (NOT_OURS | ABSENT | EXPIRED); lastGoodExpiresUtc; detail }.
    #>
    param([Parameter(Mandatory)]$Keeper, $ClaimExpiresUtc = $null, $NowUtc = $null)
    $s = $Keeper.state
    $exp = ConvertTo-VenueUtc $(if ($s.expiresUtc) { $s.expiresUtc } else { $ClaimExpiresUtc })
    $why = $(if ($s.lost) { [string]$s.lost } elseif ($null -eq $exp -or (Resolve-VenueNow $NowUtc) -ge $exp) { 'EXPIRED' } else { $null })
    if ($null -eq $why) { return $null }
    $expText = $(if ($null -ne $exp) { $exp.ToString('o') } else { 'UNKNOWN' })
    [pscustomobject]@{ reason = $why; lastGoodExpiresUtc = $(if ($null -ne $exp) { $expText }); detail = "claim=$why lastGoodExpiresUtc=$expText renews=$($s.renews) errors=$($s.errors) lastRenew=$($s.last)$(if ($s.lastError) { " lastError=$($s.lastError)" })" }
}

# r5 (sol B1, the runner side of LEASE-BATCH-OPEN-RUNNER-LIVENESS-1 #663): the open's runner-liveness sidecar, <AgentShare>\notafter\<jobId>.json, in exactly the shape
# the agent's Read-JobNotAfterSidecar / Get-JobNotAfterVerdict read (schema mlv-app/job-notafter-sidecar/v1, an object root, UTF-8, under 4096 bytes). Written to a tmp
# name beside it and moved over it in one replacing rename (the agent reads with FileShare ReadWrite|Delete, so the rename never waits on a read). The notafter folder
# is the agent's: it is never created here.
$script:LivenessWriteText = @'
param($A)
$dir = Split-Path -Parent $A.path
if (-not (Test-Path -LiteralPath $dir -PathType Container)) { '<NO_NOTAFTER_DIR>'; return }
$tmp = $A.path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
# r5b: '<WRITTEN>' only after both calls returned. A .NET exception is statement-terminating, so try/finally alone ran on past a denied write and said WRITTEN.
try {
    [IO.File]::WriteAllText($tmp, $A.json, [Text.UTF8Encoding]::new($false))
    [IO.File]::Move($tmp, $A.path, $true)
    '<WRITTEN>'
} catch { '<WRITE_FAILED> ' + ($_.Exception.Message -replace '\s+', ' ') } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
'@

function New-VenueJobLivenessWrite {
    # The argument for one sidecar write: path and JSON text for -JobId alive until -AliveUntilUtc, write number -Seq.
    param([Parameter(Mandatory)][string]$AgentShare, [Parameter(Mandatory)][string]$JobId, [Parameter(Mandatory)][DateTime]$AliveUntilUtc, [long]$Seq = 1)
    $json = [ordered]@{ schema = 'mlv-app/job-notafter-sidecar/v1'; jobId = $JobId; aliveUntilUtc = $AliveUntilUtc.ToUniversalTime().ToString('o'); writtenUtc = [DateTime]::UtcNow.ToString('o')
        seq = $Seq; writerPid = $PID; writerHost = [string]$env:COMPUTERNAME } | ConvertTo-Json -Compress
    [pscustomobject]@{ path = ($AgentShare.TrimEnd('\') + '\notafter\' + $JobId + '.json'); json = $json }
}

function Write-VenueJobLiveness {
    <#
    .SYNOPSIS
    One sidecar write on a bounded worker (the owner's thread is never held by a dead share). Returns @{ state (WRITTEN | NO_NOTAFTER_DIR | UNKNOWN); path; aliveUntilUtc; error }.
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [string]$JobId = '', [Parameter(Mandatory)][DateTime]$AliveUntilUtc, [long]$Seq = 1, [ValidateRange(1, 600)][int]$TimeoutSec = 30)
    if ($JobId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$') { return [pscustomobject]@{ state = 'UNKNOWN'; path = $null; aliveUntilUtc = $null; error = 'no usable job id' } }
    $w = New-VenueJobLivenessWrite -AgentShare $AgentShare -JobId $JobId -AliveUntilUtc $AliveUntilUtc -Seq $Seq
    $r = Invoke-VenueBoundedRead -ScriptText $script:LivenessWriteText -Argument $w -TimeoutSec $TimeoutSec
    $said = [string](@($r.value) -join '')
    $state = $(if (-not $r.answered) { 'UNKNOWN' } elseif ($said -ceq '<WRITTEN>') { 'WRITTEN' } elseif ($said -ceq '<NO_NOTAFTER_DIR>') { 'NO_NOTAFTER_DIR' } else { 'UNKNOWN' })
    [pscustomobject]@{ state = $state; path = $w.path; aliveUntilUtc = $AliveUntilUtc.ToUniversalTime().ToString('o'); error = $(if ($state -ceq 'NO_NOTAFTER_DIR') { 'the agent share has no notafter folder' } elseif ($said -clike '<WRITE_FAILED>*') { $said.Substring(15) } else { $r.error }) }
}

function Start-VenueJobLiveness {
    <#
    .SYNOPSIS
    r5: while the caller owns a submitted job, keep its runner-liveness sidecar -WindowSec ahead: one write now (bounded), then a keeper rewrites it every -RenewSec
    (seq 2, 3, ...). The keeper dies with the caller's process, so a runner killed early stops renewing and the agent ends the job's gate wait, and releases a pending
    or just-active lease, once aliveUntilUtc passes (#663). Stop it with Stop-VenueJobLiveness.
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [Parameter(Mandatory)][string]$JobId, [ValidateRange(5, 1800)][int]$WindowSec = 120, [ValidateRange(1, 600)][int]$RenewSec = 30)
    $first = Write-VenueJobLiveness -AgentShare $AgentShare -JobId $JobId -AliveUntilUtc ([DateTime]::UtcNow.AddSeconds($WindowSec)) -Seq 1
    $step = @'
param($A, $S)
$S.seq++
$until = [DateTime]::UtcNow.AddSeconds($A.windowSec)
$w = New-VenueJobLivenessWrite -AgentShare $A.share -JobId $A.jobId -AliveUntilUtc $until -Seq $S.seq
$said = [string](@(& ([scriptblock]::Create($A.writeText)) $w) -join '')
if ($said -ceq '<WRITTEN>') { $S.writes++; $S.aliveUntilUtc = $until.ToString('o') } else { $S.writeFailures++ }
$said
'@
    $arg = [pscustomobject]@{ share = $AgentShare; jobId = $JobId; windowSec = $WindowSec; writeText = $script:LivenessWriteText }
    $k = Start-VenueKeeper -StepText $step -Argument $arg -EverySec $RenewSec -State @{ seq = 1; writes = $(if ($first.state -ceq 'WRITTEN') { 1 } else { 0 }); writeFailures = $(if ($first.state -ceq 'WRITTEN') { 0 } else { 1 })
        aliveUntilUtc = $(if ($first.state -ceq 'WRITTEN') { $first.aliveUntilUtc } else { $null }); windowSec = $WindowSec; renewSec = $RenewSec }
    $k | Add-Member -NotePropertyName first -NotePropertyValue $first -PassThru | Add-Member -NotePropertyName jobId -NotePropertyValue $JobId -PassThru | Add-Member -NotePropertyName share -NotePropertyValue $AgentShare -PassThru
}

function Stop-VenueJobLiveness {
    <#
    .SYNOPSIS
    r5: the caller no longer owns the job, so the sidecar stops being renewed. -Remove deletes it (the job's result is in hand: the agent will not read it again);
    otherwise it is rewritten lapsed (aliveUntilUtc = now), so an agent still holding the job ends it at its next check, and left for the agent to read (it never
    deletes one). Idempotent. Returns @{ end (REMOVED | ABSENT | LAPSED | UNKNOWN | NONE); writes; writeFailures; lastSeq; lastAliveUntilUtc; stoppedInTime; error }.
    #>
    param($Liveness, [switch]$Remove)
    if ($null -eq $Liveness) { return [pscustomobject]@{ end = 'NONE'; writes = 0; writeFailures = 0; lastSeq = $null; lastAliveUntilUtc = $null; stoppedInTime = $true; error = $null } }
    if ($null -ne $Liveness.PSObject.Properties['stopped']) { return $Liveness.stopped }
    $inTime = Stop-VenueKeeper -Keeper $Liveness
    $s = $Liveness.state
    $end = 'UNKNOWN'; $err = $null
    if ($Remove) {
        $path = $Liveness.share.TrimEnd('\') + '\notafter\' + $Liveness.jobId + '.json'
        $r = Invoke-VenueBoundedRead -ScriptText 'if (Test-Path -LiteralPath $args[0] -PathType Leaf) { Remove-Item -LiteralPath $args[0] -Force; if (Test-Path -LiteralPath $args[0]) { "<PRESENT>" } else { "<REMOVED>" } } else { "<ABSENT>" }' -Argument $path -TimeoutSec 30
        $said = [string](@($r.value) -join '')
        $end = $(if ($said -ceq '<REMOVED>') { 'REMOVED' } elseif ($said -ceq '<ABSENT>') { 'ABSENT' } else { 'UNKNOWN' }); $err = $r.error
    } else {
        $w = Write-VenueJobLiveness -AgentShare $Liveness.share -JobId $Liveness.jobId -AliveUntilUtc ([DateTime]::UtcNow) -Seq ([long]$s.seq + 1)
        $end = $(if ($w.state -ceq 'WRITTEN') { 'LAPSED' } else { 'UNKNOWN' }); $err = $w.error
    }
    $out = [pscustomobject]@{ end = $end; writes = $s.writes; writeFailures = $s.writeFailures; lastSeq = $s.seq; lastAliveUntilUtc = $s.aliveUntilUtc; stoppedInTime = $inTime; error = $err }
    $Liveness | Add-Member -NotePropertyName stopped -NotePropertyValue $out -Force
    $out
}

function Read-VenueLeaseBatchReceipt {
    <#
    .SYNOPSIS
    The venue agent's own receipt for one batch lease (ultra-magnus-agent.ps1 lease-batch item 5): the line of <AgentShare>\logs\lease-batch-<Token>.jsonl whose leaseId is
    -LeaseId, read by NAME on one bounded worker.
    .OUTPUTS
    [pscustomobject]@{ state (FOUND | ABSENT | UNKNOWN); record; error }
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{12}$')][string]$Token, [string]$LeaseId = '', [ValidateRange(1, 600)][int]$TimeoutSec = 30)
    $path = $AgentShare.TrimEnd('\') + '\logs\lease-batch-' + $Token + '.jsonl'
    $r = Invoke-VenueBoundedRead -ScriptText 'if (Test-Path -LiteralPath $args[0] -PathType Leaf) { [IO.File]::ReadAllLines($args[0]) } else { "<ABSENT>" }' -Argument $path -TimeoutSec $TimeoutSec
    if (-not $r.answered) { return [pscustomobject]@{ state = 'UNKNOWN'; record = $null; error = $r.error } }
    $lines = @($r.value | ForEach-Object { [string]$_ } | Where-Object { $_ })
    if ($lines.Count -eq 1 -and $lines[0] -ceq '<ABSENT>') { return [pscustomobject]@{ state = 'ABSENT'; record = $null; error = $null } }
    foreach ($line in $lines) {
        $rec = try { $line | ConvertFrom-Json } catch { $null }
        if ($null -ne $rec -and [string]$rec.leaseId -ceq $LeaseId) { return [pscustomobject]@{ state = 'FOUND'; record = $rec; error = $null } }
    }
    [pscustomobject]@{ state = 'ABSENT'; record = $null; error = "no line for lease $LeaseId among $($lines.Count)" }
}

function Get-VenueLegPreLoadMean {
    <#
    .SYNOPSIS
    The leg's own pre-load mean, read from the run's summary.json (cpuQuiescence.timeMeanPercent), never from the receipt file. Invoke-VenueLeg writes the receipt as
    <root>\<card>\<legId>\<venue>\<receiptId>.json and the run evidence to <parent of root>\evidence\<receiptId>. $null when absent. (Same rule as Invoke-VenueChain's own.)
    #>
    param([string]$ReceiptPath)
    try {
        if (-not $ReceiptPath) { return $null }
        $root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $ReceiptPath)))
        $summary = Join-Path (Join-Path (Join-Path (Split-Path -Parent $root) 'evidence') ([IO.Path]::GetFileNameWithoutExtension($ReceiptPath))) 'summary.json'
        if (-not (Test-Path -LiteralPath $summary -PathType Leaf)) { return $null }
        $q = ([IO.File]::ReadAllText($summary) | ConvertFrom-Json).cpuQuiescence
        if ($null -eq $q -or $null -eq $q.timeMeanPercent) { return $null }
        [double]$q.timeMeanPercent
    } catch { $null }
}

function Get-VenueLeaseLedgerPath {
    # BACHELOR-LEASE-BATCH-LEGS-1 r2: <ClaimDir>\<venue>.lease-ledger.jsonl, one line per quiet lease a lease-batch run took (any run on this host), beside the claim.
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '')
    Join-Path (Split-Path -Parent (Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir)) "$Venue.lease-ledger.jsonl"
}

function Read-VenueLeaseLedger {
    # The ledger's leases that end after -SinceUtc, as @{ startUtc; endUtc; leaseId } (UTC DateTime). A line that does not parse is skipped. Written only under the
    # venue claim, so one writer at a time.
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', $SinceUtc = $null)
    $path = Get-VenueLeaseLedgerPath -Venue $Venue -ClaimDir $ClaimDir
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return @() }
    $since = $(if ($null -ne $SinceUtc) { ConvertTo-VenueUtc $SinceUtc } else { [DateTime]::MinValue })
    @(foreach ($line in [IO.File]::ReadAllLines($path)) {
            $r = try { $line | ConvertFrom-Json } catch { $null }
            if ($null -eq $r) { continue }
            $s = ConvertTo-VenueUtc $r.startUtc; $e = ConvertTo-VenueUtc $r.endUtc
            if ($null -ne $s -and $null -ne $e -and $e -gt $s -and $e -gt $since) { [pscustomobject]@{ startUtc = $s; endUtc = $e; leaseId = [string]$r.leaseId } }
        })
}

function Add-VenueLeaseLedger {
    # Appends one lease to the ledger (the caller holds the venue claim).
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', [Parameter(Mandatory)]$Row)
    $path = Get-VenueLeaseLedgerPath -Venue $Venue -ClaimDir $ClaimDir
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
    [IO.File]::AppendAllText($path, (($Row | ConvertTo-Json -Compress -Depth 4) + "`n"), [Text.UTF8Encoding]::new($false))
}

function Get-VenueLeaseBudgetWait {
    <#
    .SYNOPSIS
    How long to wait before asking Bachelor's heavy-slot.py for the next quiet lease (BACHELOR-LEASE-BATCH-LEGS-1 r2; its caps, CAPS-ADDENDUM 2026-10-23: a lease is at
    most -CapSec (45 min), none starts within -CooldownSec (30 min) of the last one's end, and leased time is at most -BudgetSec (90 min) in any -WindowSec (3 h)).
    The open time is the first moment, on a -StepSec grid, that is past the cooldown and whose WORST window (the one ending when a full-length lease opened then would
    end) holds at most BudgetSec - CapSec of the -Leases already taken. Pure: -Leases are @{ startUtc; endUtc }, -NowUtc is passed in.
    .OUTPUTS
    [pscustomobject]@{ reason (NONE | COOLDOWN | BUDGET); waitSec; openUtc; usedSec (in that window at openUtc); budgetRoomSec (at most CapSec); lastEndUtc }
    #>
    param([object[]]$Leases = @(), [Parameter(Mandatory)]$NowUtc, [int]$CapSec = 2700, [int]$CooldownSec = 1800, [int]$WindowSec = 10800, [int]$BudgetSec = 5400,
        [ValidateRange(1, 600)][int]$StepSec = 30)
    $now = ConvertTo-VenueUtc $NowUtc
    $rows = @(foreach ($l in @($Leases)) {
            if ($null -eq $l) { continue }
            $s = ConvertTo-VenueUtc $l.startUtc; $e = ConvertTo-VenueUtc $l.endUtc
            if ($null -ne $s -and $null -ne $e -and $e -gt $s) { [pscustomobject]@{ s = $s; e = $e } }
        })
    $used = {
        param([DateTime]$T)
        $from = $T.AddSeconds($CapSec - $WindowSec)
        $sum = 0.0
        foreach ($r in $rows) {
            $a = $(if ($r.s -gt $from) { $r.s } else { $from }); $z = $(if ($r.e -lt $T) { $r.e } else { $T })
            if ($z -gt $a) { $sum += ($z - $a).TotalSeconds }
        }
        $sum
    }
    $lastEnd = $(if ($rows.Count -gt 0) { ($rows | Sort-Object e | Select-Object -Last 1).e } else { $null })
    $t = $now; $reason = 'NONE'
    if ($null -ne $lastEnd -and $lastEnd.AddSeconds($CooldownSec) -gt $t) { $t = $lastEnd.AddSeconds($CooldownSec); $reason = 'COOLDOWN' }
    $limit = $t.AddSeconds($WindowSec)
    while ((& $used $t) + $CapSec -gt $BudgetSec -and $t -lt $limit) { $t = $t.AddSeconds($StepSec); $reason = 'BUDGET' }
    $u = [int][Math]::Round((& $used $t))
    [pscustomobject]@{ reason = $reason; waitSec = [int][Math]::Ceiling(($t - $now).TotalSeconds); openUtc = $t; usedSec = $u; budgetRoomSec = [int][Math]::Min($CapSec, $BudgetSec - $u); lastEndUtc = $lastEnd }
}

function Invoke-VenueBoundedProcess {
    <#
    .SYNOPSIS
    Run one child process and give up on it after -TimeoutSec (the child and its tree are killed). .exit is $null when it timed out.
    .OUTPUTS
    [pscustomobject]@{ exit; timedOut; stdout; stderr }
    #>
    param([Parameter(Mandatory)][string]$FilePath, [Parameter(Mandatory)][string[]]$ArgumentList, [Parameter(Mandatory)][ValidateRange(1, 86400)][int]$TimeoutSec)
    $psi = [Diagnostics.ProcessStartInfo]::new($FilePath)
    foreach ($a in $ArgumentList) { [void]$psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $outTask = $p.StandardOutput.ReadToEndAsync(); $errTask = $p.StandardError.ReadToEndAsync()
    $timedOut = -not $p.WaitForExit($TimeoutSec * 1000)
    if ($timedOut) { try { $p.Kill($true) } catch { }; [void]$p.WaitForExit(15000) }
    [void]$outTask.Wait(15000); [void]$errTask.Wait(15000)
    [pscustomobject]@{ exit = $(if ($timedOut) { $null } else { $p.ExitCode }); timedOut = $timedOut
        stdout = $(if ($outTask.IsCompleted) { $outTask.Result } else { '' }); stderr = $(if ($errTask.IsCompleted) { $errTask.Result } else { '' }) }
}

function Get-VenueDriverScriptMissing {
    <#
    .SYNOPSIS
    LANE-RETIRE-LIVE-DETACHED-DRIVER-GUARD-1: '' when every script a venue driver is about to launch exists, else the typed line
    `VENUE_DRIVER_SCRIPT_MISSING path=<first missing path>`. A driver (Invoke-VenueChain, or a lane-local detached driver that imported
    this module at start, so the check survives its worktree being removed) calls it before each launch and STOPS on a non-empty answer:
    a missing script is not "the venue was not measured" (2026-10-24: a driver logged VENUE_QUIET_UNKNOWN every 5 min for 40 min instead).
    #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Path)
    foreach ($p in $Path) {
        if ($p -and -not (Test-Path -LiteralPath $p -PathType Leaf)) { return "VENUE_DRIVER_SCRIPT_MISSING path=$p" }
    }
    return ''
}

Export-ModuleMember -Function Get-VenueDriverScriptMissing, Get-VenueMainRoot, Get-VenueClaimPath, Get-VenueClaim, Get-VenueClaimStaleReason, Request-VenueClaim, Release-VenueClaim,
    Get-VenueTicketDir, New-VenueTicket, Get-VenueTickets, Get-VenueTicketStaleReason, Get-VenueTicketProbeStaleSec, Update-VenueTicket, Remove-VenueTicket, Get-VenueClaimStatus, Format-VenueClaimStatus, Format-VenueTicketQueue,
    ConvertFrom-VenueGateLog, Get-VenueQuietRateTable, Format-VenueQuietRateTable, Select-VenueQuietWindow,
    Get-VenueSupplementarySpecSkip, Get-VenueSupplementaryIdle, Get-VenueStagedBuildManifest, Get-VenueRemoteJobIds, Get-VenueRemoteJobState, Invoke-VenueBoundedProcess, Complete-VenueProcess,
    Get-VenueJobResult, Request-VenueJobCancel, Read-VenueLeaseBatchReceipt, Get-VenueLegPreLoadMean, Get-VenueLeaseLedgerPath, Read-VenueLeaseLedger, Add-VenueLeaseLedger, Get-VenueLeaseBudgetWait,
    Update-VenueClaim, Start-VenueKeeper, Stop-VenueKeeper, Start-VenueClaimKeeper, Get-VenueClaimKeeperLoss, New-VenueJobLivenessWrite, Write-VenueJobLiveness, Start-VenueJobLiveness, Stop-VenueJobLiveness
