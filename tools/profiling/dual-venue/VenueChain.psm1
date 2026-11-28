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
# READINESS (VENUE-FIFO-TICKET-BEFORE-PRECLAIM-1). A ticket has a phase. The caller's own request always refreshes its ticket to 'waiting' (ready). A holder that cannot take the
# claim yet (a lease batch waiting out Bachelor's lease cooldown or 3 h budget, Invoke-VenueLeaseBatch.ps1) sets phase 'cooldown' (New-VenueTicket -Phase, Update-VenueTicket -Phase):
# the claim goes to the oldest live ticket that is not in 'cooldown', so a waiter that is not ready keeps its place but never blocks a ready one, and when it turns ready it is again
# ahead of every later arrival (2026-11-20: a later batch took the claim 09:25:10Z ahead of one that had waited since 08:17:03Z, because lease batches took no ticket).
# A heartbeat rewrites the ticket as a WHOLE new file moved over the old one (Update-VenueTicket / Write-VenueTicketAtomic), never in place: an in-place rewrite showed a younger reader a
# busy or empty file, so an older READY ticket read as not ready and was overtaken (Grok CoS G4 on #878). Ticket readers share Delete so that rename never waits on a read.
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

# r13 (adversary r12 A1): the agent's results and receipts are read with their lease ids and tokens as the raw strings written (ConvertFrom-Json made a timestamp-shaped one a DateTime)
Import-Module (Join-Path $PSScriptRoot 'VenueRawJson.psm1') -DisableNameChecking

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

function Read-VenueClaimFile {
    # One read with FileShare.Read: a writer (CreateNew, FileShare.None) or a breaker (FileShare.Delete) holding the file makes it BUSY, never half-read.
    # -ShareDelete (tickets only): the reader also lets the file be renamed over while it reads, so a heartbeat's replacing rename (Write-VenueTicketAtomic) never waits on a reader.
    param([string]$Path, [switch]$ShareDelete)
    $share = $(if ($ShareDelete) { [IO.FileShare]::Read -bor [IO.FileShare]::Delete } else { [IO.FileShare]::Read })
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return [pscustomobject]@{ exists = $false; busy = $false; text = $null; lastWriteUtc = $null } }
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, $share)
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
    # r11 pass 1 (adversary A1): -MaxAttempts (1..5, default 5) is how many opens are tried; the late grant's final probe asks for one.
    param([Parameter(Mandatory)][string]$Path, [string]$ExpectedText = $null, [string]$ExpectedNonce = '', [ValidateRange(1, 5)][int]$MaxAttempts = 5)
    for ($attempt = 1; $attempt -le $MaxAttempts; $attempt++) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 'ABSENT' }
        $fs = $null
        # r11 (sol r10 B2): no sleep after the LAST attempt. It was followed by nothing but the BUSY answer, so a call that found the file held answered at 750 ms (opens at 0, 50, 150, 300, 500) when its last
        # open was at 500 ms: the late grant's release (reserve 0.75 s) took that one answer as the end of the reserve and gave up on a reader that let go at 650 ms. It now answers BUSY right after its last open (500 ms).
        try { $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::Delete) } catch { if ($attempt -lt $MaxAttempts) { Start-Sleep -Milliseconds (50 * $attempt) }; continue }
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
$script:TicketNameRx = [regex]'^([0-9]{19})-p([0-9]+)-([0-9]+)\.ticket\.json$'   # [0-9], never \d: \d also matches Arabic-Indic and other Unicode digits in .NET, which the [int64] cast then throws on (adversary A10)

function Resolve-VenueNow($Value) { if ($null -eq $Value) { [DateTime]::UtcNow } else { ConvertTo-VenueUtc $Value } }

# r8 rule 2 (adversary r7 pass 3 A1): the last phase this PROCESS read from each ticket (by path, with the nonce it was read under), so a ticket whose file is busy for longer than a settle window keeps
# the phase it was last seen with, and a ticket this process never read stays "phase unknown, not ready". Bounded: cleared past 512 entries; pruned to the files present at every census.
$script:TicketPhaseSeen = [Collections.Generic.Dictionary[string, object]]::new([StringComparer]::OrdinalIgnoreCase)

function Get-VenueTicketNameKey {
    # r8 rule 1: the owner pid and start ticks a ticket's file NAME carries (<ticks>-p<pid>-<start>.ticket.json), as text; $null for a name that is no ticket's. The name is readable when the file is not.
    param([string]$Name)
    $m = $script:TicketNameRx.Match([string]$Name)
    if (-not $m.Success) { return $null }
    [pscustomobject]@{ pidText = $m.Groups[2].Value; startText = $m.Groups[3].Value }
}

function Test-VenueOwnTicketRow {
    # r8 rule 1 (adversary r7 pass 3 A2, A7; the r6 A9 class "a batch queued behind itself"): a row that is ANOTHER ticket of the caller's own process (the pid and start ticks in the names match) and run directory
    # (compared when the ticket is readable; a busy file has only its name to go by) is the caller's own, and never blocks it: a stranded or half-made ticket of this very owner must not queue its next place behind it.
    param($Row, [string]$MyName, [string]$MyRunDir = '')
    if ($Row.name -ceq $MyName) { return $false }
    $mine = Get-VenueTicketNameKey $MyName
    $theirs = Get-VenueTicketNameKey $Row.name
    if ($null -eq $mine -or $null -eq $theirs -or $mine.pidText -cne $theirs.pidText -or $mine.startText -cne $theirs.startText) { return $false }
    if ($null -ne $Row.ticket) { return ([string]$Row.ticket.runDir -ceq $MyRunDir) }
    $true
}

function Get-VenueRowPhase {
    # r8 rule 2: a row's phase: the parsed ticket's, or (a BUSY file) the last one this process read, or $null (unknown).
    # A scalar string or $null, never the raw value: a one-element array would be unrolled by the pipeline into its element, and read as that string (the r7 A10 array case).
    param($Row)
    if ($null -ne $Row.ticket) { $own = $Row.ticket.phase; if ($own -is [string]) { return [string]$own } else { return $null } }
    $p = $Row.PSObject.Properties['lastPhase']
    if ($Row.state -ceq 'BUSY' -and $null -ne $p) { return $p.Value }
    $null
}

function Get-VenueClaimAgeSec {
    # r8 rule 4 (adversary r7 pass 3 A5): the age of a claim by its OWN claimedUtc, the clock the claim's expiry runs on (not the moment a caller's request happened to return). $null when it cannot be read.
    param($Claim, $NowUtc = $null)
    if ($null -eq $Claim) { return $null }
    $claimed = ConvertTo-VenueUtc $Claim.claimedUtc
    if ($null -eq $claimed) { return $null }
    [Math]::Max(0.0, ((Resolve-VenueNow $NowUtc) - $claimed).TotalSeconds)
}

function Get-VenueTicketDir {
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '')
    Join-Path (Split-Path -Parent (Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir)) "$Venue.tickets"
}

function ConvertTo-VenueTicketBytes($Fields) { [Text.UTF8Encoding]::new($false).GetBytes(($Fields | ConvertTo-Json -Compress) + "`n") }

function ConvertTo-VenueTicketPhase {
    # r9 (adversary r9 A4): ValidateSet compares with the current culture and ignores characters such as U+00AD, U+200D and U+0000, so 'wait<U+00AD>ing' bound as a valid phase and was STORED with the extra
    # character, a spelling no ordinal reader knows as ready. The stored value is the canonical lower-case word the set names, matched ORDINALLY (case-insensitive only); anything else is refused like a failed binding.
    param([AllowEmptyString()][string]$Phase, [Parameter(Mandatory)][string[]]$Allowed)
    foreach ($candidate in $Allowed) { if ([string]::Equals($Phase, $candidate, [StringComparison]::OrdinalIgnoreCase)) { return $candidate } }
    throw "VENUE_CLAIM_USAGE the ticket phase '$Phase' is not one of: $($Allowed -join ', ')"
}

function New-VenueTicket {
    <#
    .SYNOPSIS
    Take a place in the venue's line: one ticket file, created with CreateNew, named by the UTC ticks it was taken at (a clash moves to the next tick).
    .OUTPUTS
    [pscustomobject]@{ name; path; nonce; venue; runDir; ownerPid; createdUtc }
    #>
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$RunDir, [string]$Purpose = '', [ValidateRange(1, 86400)][int]$StaleAfterSec = 90,
        [string]$ClaimDir = '', [int]$OwnerPid = $PID, $NowUtc = $null, [ValidateSet('waiting', 'cooldown')][string]$Phase = 'waiting')
    $storedPhase = ConvertTo-VenueTicketPhase $Phase @('waiting', 'cooldown')   # r9 A4: before any file work
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
            phase = $storedPhase; nonce = [guid]::NewGuid().ToString('N')   # ValidateSet is case-insensitive and culture-aware: the stored spelling is the canonical word (ConvertTo-VenueTicketPhase)
        }
        $bytes = ConvertTo-VenueTicketBytes $fields
        $made = $false
        try {
            $fs = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)   # the only way a ticket is taken
            $made = $true
            try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }
        } catch {
            # adversary A9: a create that succeeded but whose write or flush then failed left a live ticket of this owner in the line, and the next tick's ticket queued behind it (a batch behind itself): that half-made one is taken back out.
            # r7 A9: a reader that holds the file for a moment refuses the delete, so it is retried for up to 1 s; a file that STILL cannot be removed is this owner's ticket, and it is the one returned (never a second one beside it).
            if ($made) {
                $gone = $false
                for ($retry = 0; $retry -lt 10 -and -not $gone; $retry++) {
                    try { [IO.File]::Delete($path); $gone = $true } catch { Start-Sleep -Milliseconds 100 }
                }
                if (-not $gone) { return [pscustomobject]@{ name = $name; path = $path; nonce = $fields.nonce; venue = $Venue; runDir = $RunDir; ownerPid = $OwnerPid; createdUtc = $now.ToString('o') } }
            }
            continue
        }
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
    $present = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    if (Test-Path -LiteralPath $dir -PathType Container) {
        foreach ($file in @(Get-ChildItem -LiteralPath $dir -Filter '*.ticket.json' -File -ErrorAction SilentlyContinue)) {
            $m = $script:TicketNameRx.Match($file.Name)
            if (-not $m.Success) { continue }
            [void]$present.Add($file.FullName)
            $keys = $null
            try { $keys = @([decimal]$m.Groups[1].Value, [decimal]$m.Groups[2].Value, [decimal]$m.Groups[3].Value) } catch { continue }   # a name whose number is past any decimal is no ticket of ours (pass 2 A7): skipped, never a throw out of the census
            $f = Read-VenueClaimFile $file.FullName -ShareDelete
            if (-not $f.exists) { continue }
            $row = [ordered]@{ name = $file.Name; path = $file.FullName; key = $keys[0]; pidKey = $keys[1]; startKey = $keys[2]; state = 'LIVE'; reason = $null; text = $f.text; ticket = $null; position = 0; ageSec = $null; heartbeatAgeSec = $null; lastPhase = $null }
            if ($f.busy) {
                $row.state = 'BUSY'
                $seen = $null
                if ($script:TicketPhaseSeen.TryGetValue($file.FullName, [ref]$seen)) { $row.lastPhase = $seen.phase }   # r8 rule 2: a busy file keeps the phase this process last read from it
            }
            else {
                $t = $null
                try { $t = $f.text | ConvertFrom-Json -ErrorAction Stop -NoEnumerate } catch { $t = $null }
                if ($t -is [Collections.IEnumerable] -and $t -isnot [string]) { $t = $null }   # r7 A10: a root array (which the pipeline would unwrap to its one element) is no ticket
                if ($null -eq $t -or [string]$t.schema -cne $script:TicketSchema -or [string]$t.venue -cne $Venue) {
                    if ($null -ne $f.lastWriteUtc -and ($now - $f.lastWriteUtc).TotalSeconds -lt $script:UnreadableGraceSec) { $row.reason = 'being-written' }
                    else { $row.state = 'STALE'; $row.reason = 'unreadable' }
                } else {
                    $row.ticket = $t
                    if ($script:TicketPhaseSeen.Count -ge 512) { $script:TicketPhaseSeen.Clear() }
                    $script:TicketPhaseSeen[$file.FullName] = [pscustomobject]@{ nonce = [string]$t.nonce; phase = $(if ($t.phase -is [string]) { [string]$t.phase } else { $null }) }
                    $why = Get-VenueTicketStaleReason -Ticket $t -NowUtc $now
                    if ($why) { $row.state = 'STALE'; $row.reason = $why }
                    $c = ConvertTo-VenueUtc $t.createdUtc; $b = ConvertTo-VenueUtc $t.heartbeatUtc
                    # r7 A12: clamped to the Int32 range ([int] of 4004121600 s, a ticket dated 1900, threw out of the census and out of a granted request)
                    if ($null -ne $c) { $row.ageSec = [int][Math]::Min(2147483647.0, [Math]::Max(0.0, ($now - $c).TotalSeconds)) }
                    if ($null -ne $b) { $row.heartbeatAgeSec = [int][Math]::Min(2147483647.0, [Math]::Max(0.0, ($now - $b).TotalSeconds)) }
                }
            }
            $rows.Add([pscustomobject]$row)
        }
    }
    $prefix = $null
    try { $prefix = [IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($dir)).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar } catch { $prefix = $null }
    if ($null -ne $prefix) { foreach ($k in @($script:TicketPhaseSeen.Keys)) { if ($k.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -and -not $present.Contains($k)) { [void]$script:TicketPhaseSeen.Remove($k) } } }   # r8 rule 2: a ticket no longer in its folder is forgotten
    $sorted = @($rows | Sort-Object -Property key, pidKey, startKey, name)   # arrival tick, then owner pid, then owner start ticks: equal ticks never fall back to enumeration order
    $pos = 0
    foreach ($r in $sorted) { if ($r.state -cne 'STALE') { $pos++; $r.position = $pos } }
    $sorted
}

function Write-VenueTicketAtomic {
    # VENUE-FIFO-TICKET-BEFORE-PRECLAIM-1 r5 (Grok CoS G4): the new body goes to a name beside the ticket and is moved over it in one replacing rename, so a reader sees the whole old
    # ticket or the whole new one, never a zero-length or half-written file (an in-place SetLength(0)+Write made an older READY ticket read as not ready, and a younger waiter overtook it).
    # The temp name does not end in .ticket.json, so Get-VenueTickets never lists it. Throws when the rename is refused (a handle without FileShare.Delete): Update-VenueTicket retries it
    # (five attempts, then returns BUSY), and Request-VenueClaim retries a BUSY for up to 2 s, then returns QUEUED with a TICKET_PHASE_BUSY note. A process that dies between the CreateNew and the
    # rename leaves its temp behind: Remove-VenueOrphanTicketTemps reaps those older than 5 minutes in Request-VenueClaim's stale sweep.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][byte[]]$Bytes, [Diagnostics.Stopwatch]$Clock = $null, [ValidateRange(0.0, 3600.0)][double]$BudgetSec = 0)
    $tmp = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    try {
        # r8 rule 6: the budget is asked before the temp is written too, not only before the rename (a rename already running cannot be cut short: that half is the platform's)
        if ($BudgetSec -gt 0 -and $null -ne $Clock -and $Clock.Elapsed.TotalSeconds -ge $BudgetSec) { throw 'TICKET_BUDGET the budget ended before the temp write: nothing was written' }
        $fs = [IO.File]::Open($tmp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $fs.Write($Bytes, 0, $Bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }
        if ($BudgetSec -gt 0 -and $null -ne $Clock -and $Clock.Elapsed.TotalSeconds -ge $BudgetSec) { throw 'TICKET_BUDGET the budget ended before the rename: nothing was published' }
        [IO.File]::Move($tmp, $Path, $true)   # MoveFileEx replace-existing. NOT [IO.File]::Replace: ReplaceFileW is not atomic for a concurrent open (measured: a reader saw the name ABSENT 1009 and BUSY 1541 times in 6 s of heartbeats, and an absent older ticket is no ticket at all), the rename-over showed 0 in 19058 reads. A ticket reaped between our read and this rename comes back, but only its heartbeating (so live) owner could have written it.
    } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
}

$script:TicketTmpRx = [regex]'^[0-9]{19}-p[0-9]+-[0-9]+\.ticket\.json\.[0-9a-f]{32}\.tmp$'   # ASCII digits only (adversary A10)

function Test-VenueTicketPhaseReady {
    # A ticket may be the head of the line only with a stored phase that is a KNOWN ready one. cooldown is not; neither is a null, missing, mis-spelled or unknown phase (adversary A13: only the string cooldown was excluded, so any other value counted as ready).
    # r7 A10: a SCALAR string, compared ORDINALLY (the -c operators are culture-aware: a soft hyphen inside 'wait-ing' compared equal, and a one-element array stringified to its element).
    param($Phase)
    ($Phase -is [string]) -and $script:ReadyTicketPhases.Contains($Phase)
}
$script:ReadyTicketPhases = [Collections.Generic.HashSet[string]]::new([string[]]@('waiting', 'probing', 'supplementary'), [StringComparer]::Ordinal)

function Remove-VenueOrphanTicketTemps {
    # VENUE-FIFO-TICKET-BEFORE-PRECLAIM-1 r6 (Grok CoS G8): reaps the `<ticket>.<guid>.tmp` files Write-VenueTicketAtomic leaves when its process dies between the temp's CreateNew and the rename.
    # Best effort: only a name of exactly that shape, only older than -OlderThanSec, and a file in use or already gone is skipped, never an error. A fresh temp is a heartbeat in flight and is kept.
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', [int]$OlderThanSec = 300)
    $dir = Get-VenueTicketDir -Venue $Venue -ClaimDir $ClaimDir
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return }
    # r7 A14: under the ticket directory's lock, which a heartbeat holds from before its temp is written until after its rename: a temp seen here is no publisher's in flight (a wall-clock step or a long pause
    # made a live one look older than 5 minutes). Lock not free in 0.2 s: the sweep is skipped, it is best effort.
    $lock = Enter-VenueTicketLock -TicketPath (Join-Path $dir 'sweep') -TimeoutMs 200
    if ($null -eq $lock) { return }
    try {
        $cut = [DateTime]::UtcNow.AddSeconds(-$OlderThanSec)
        foreach ($f in @(Get-ChildItem -LiteralPath $dir -Filter '*.ticket.json.*.tmp' -File -ErrorAction SilentlyContinue)) {
            if (-not $script:TicketTmpRx.IsMatch($f.Name) -or $f.LastWriteTimeUtc -ge $cut) { continue }
            try { [IO.File]::Delete($f.FullName) } catch { $null = $_ }
        }
    } finally { Exit-VenueTicketLock $lock }
}

function Test-VenueAdoptedByName {
    # r8 rule 1: a ticket object Find-VenueOwnTicket made for a BUSY file carries adopted='by-name' and no nonce; the file it names is this owner's only when its pid is the one in the name and its run directory is the caller's.
    param($Ticket, $Parsed)
    $flag = $Ticket.PSObject.Properties['adopted']
    if ($null -eq $flag -or [string]$flag.Value -cne 'by-name' -or $null -eq $Parsed) { return $false }
    $k = Get-VenueTicketNameKey ([string]$Ticket.name)
    ($null -ne $k) -and ([string]$Parsed.ownerPid -ceq $k.pidText) -and ([string]$Parsed.runDir -ceq [string]$Ticket.runDir) -and -not [string]::IsNullOrEmpty([string]$Parsed.nonce)
}

function Update-VenueTicket {
    <#
    .SYNOPSIS
    Heartbeat: rewrite the ticket's heartbeat (and, when given, its phase and staleAfterSec) as a whole new file moved over the old one (Write-VenueTicketAtomic).
    Returns OK | LOST (the ticket is gone or is not ours) | BUSY (a reader or the rename was refused five times; nothing was written). Every caller branches on all three:
    Request-VenueClaim retries BUSY for up to 2 s and then answers QUEUED (note TICKET_PHASE_BUSY); the lease batch's cooldown heartbeat retakes on LOST and logs TICKET_HEARTBEAT_MISSED on BUSY;
    the chain's probing/supplementary phase sets log TICKET_PHASE_<result> on LOST or BUSY.
    #>
    param([Parameter(Mandatory)]$Ticket, [ValidateSet('', 'waiting', 'cooldown', 'probing', 'supplementary')][string]$Phase = '', [int]$StaleAfterSec = 0, $NowUtc = $null, [ValidateRange(0.0, 3600.0)][double]$BudgetSec = 0)   # r8 rule 6: 0 (none) through 3600 s; NaN, a negative and Infinity are refused at binding. Every phase a caller passes (the chain runner sets probing and supplementary; the lease batch cooldown; a request waiting). ValidateSet is case-insensitive: stored lower-case below, as New-VenueTicket does. -BudgetSec (adversary A6, 0 = none): no attempt starts, and no back-off sleeps, past this many seconds (a monotonic clock), so a caller's bound is a bound
    $storedPhase = if ($Phase) { ConvertTo-VenueTicketPhase $Phase @('waiting', 'cooldown', 'probing', 'supplementary') } else { '' }   # r9 A4: the canonical word, matched ordinally
    $now = Resolve-VenueNow $NowUtc
    $clock = [Diagnostics.Stopwatch]::StartNew()
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        if ($attempt -gt 1) { Start-Sleep -Milliseconds (Get-VenueBackoffMs ($attempt - 1) $clock $BudgetSec) }   # every retry path below is a `continue`, and the back-off is slept here, never while the lock is held
        if ($BudgetSec -gt 0 -and $clock.Elapsed.TotalSeconds -ge $BudgetSec) { break }
        # adversary A2: the nonce check and the rename are one critical section under the ticket directory's lock, which every remover of a ticket (Remove-VenueTicket, the reaper) takes too. A File.Move over an ABSENT
        # name creates it (a rename-over also fails while ANY handle is open on the destination, measured, so no read handle can be held across it), so without the lock a ticket removed between the check and the rename came back.
        if (-not (Test-Path -LiteralPath $Ticket.path -PathType Leaf)) { return 'LOST' }   # no lock for a ticket that is not there
        if ($BudgetSec -gt 0 -and $clock.Elapsed.TotalSeconds -ge $BudgetSec) { break }   # r9 A5: asked again after the path check (a slow share can spend the whole budget in it): no lock attempt starts past the budget
        $lock = Enter-VenueTicketLock -TicketPath $Ticket.path -TimeoutMs $(if ($BudgetSec -gt 0) { [int][Math]::Max(1.0, [Math]::Min(1000.0, ($BudgetSec - $clock.Elapsed.TotalSeconds) * 1000.0)) } else { 1000 })
        if ($null -eq $lock) { continue }
        try {
            if ($BudgetSec -gt 0 -and $clock.Elapsed.TotalSeconds -ge $BudgetSec) { continue }   # r8 rule 6: asked again with the lock taken, before the read (the lock can be won late)
            if (-not (Test-Path -LiteralPath $Ticket.path -PathType Leaf)) { return 'LOST' }
            if ($BudgetSec -gt 0 -and $clock.Elapsed.TotalSeconds -ge $BudgetSec) { continue }   # r9 A5: and once more after this path check, before the read handle is opened
            $text = $null
            $fs = $null
            try {
                $fs = [IO.File]::Open($Ticket.path, [IO.FileMode]::Open, [IO.FileAccess]::Read, ([IO.FileShare]::Read -bor [IO.FileShare]::Delete))
                try { $sr = [IO.StreamReader]::new($fs, [Text.UTF8Encoding]::new($false), $false, 4096, $true); $text = $sr.ReadToEnd(); $sr.Dispose() } finally { $fs.Dispose() }
            } catch { continue }
            $j = $null; try { $j = $text | ConvertFrom-Json -ErrorAction Stop } catch { $j = $null }
            $want = [string]$Ticket.nonce
            if ($null -ne $j -and $want -ceq '' -and (Test-VenueAdoptedByName $Ticket $j)) { $want = [string]$j.nonce; try { $Ticket.nonce = $want } catch { $null = $_ } }   # r8 rule 1: a ticket adopted by NAME (its file was busy) takes its nonce from the file, once the file proves to be this owner's
            if ($null -eq $j -or $want -ceq '' -or [string]$j.nonce -cne $want) { return 'LOST' }
            $fields = [ordered]@{
                schema = [string]$j.schema; venue = [string]$j.venue; ownerPid = [int]$j.ownerPid; ownerCreatedUtc = (ConvertTo-VenueUtc $j.ownerCreatedUtc).ToString('o'); host = [string]$j.host
                runDir = [string]$j.runDir; purpose = [string]$j.purpose; createdUtc = (ConvertTo-VenueUtc $j.createdUtc).ToString('o'); heartbeatUtc = $now.ToString('o')
                staleAfterSec = $(if ($StaleAfterSec -gt 0) { $StaleAfterSec } else { [int]$j.staleAfterSec }); phase = $(if ($storedPhase) { $storedPhase } else { [string]$j.phase }); nonce = [string]$j.nonce
            }
            try { Write-VenueTicketAtomic -Path $Ticket.path -Bytes (ConvertTo-VenueTicketBytes $fields) -Clock $clock -BudgetSec $BudgetSec }   # r7 A6: the budget is asked once more right before the publish
            catch { if (-not (Test-Path -LiteralPath $Ticket.path -PathType Leaf)) { return 'LOST' }; continue }
            return 'OK'
        } finally { Exit-VenueTicketLock $lock }
    }
    'BUSY'
}

function Enter-VenueTicketLock {
    # adversary A2: one lock per ticket directory, held only for a ticket's nonce-check-and-publish, for a ticket's removal and for the orphan-temp sweep. Returns the lock once owned, or $null when it could not be
    # taken in -TimeoutMs (the caller then answers BUSY and retries).
    # r7 A4 + pass 2 A3: the lock is an EXCLUSIVELY OPENED FILE (<ticket dir>\.ticket.lock, FileMode.OpenOrCreate, FileShare.None), never a name: the file system decides what is the same directory, so a relative, dot-dot or
    # junction spelling of the claim folder all reach the one lock file (a named mutex keyed on the path's spelling did not). A holder that dies releases it with its process; the file is left in place and never listed
    # (it is neither *.ticket.json nor a ticket temp). Works across sessions and over a shared claim folder. The directory must exist (a ticket is in it).
    param([Parameter(Mandatory)][string]$TicketPath, [int]$TimeoutMs = 1000)
    $dir = $null
    try { $dir = [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($TicketPath))) } catch { $dir = $null }
    if ([string]::IsNullOrEmpty($dir) -or -not (Test-Path -LiteralPath $dir -PathType Container)) { return $null }
    $lockPath = Join-Path $dir '.ticket.lock'
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($true) {
        try { return [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch { if ($clock.Elapsed.TotalMilliseconds -ge $TimeoutMs) { return $null } }
        Start-Sleep -Milliseconds 15
        if ($clock.Elapsed.TotalMilliseconds -ge $TimeoutMs) { return $null }   # r8 rule 6: no attempt starts after the timeout (the budget of an update): the lock may have been released during the sleep
    }
}

function Exit-VenueTicketLock($Lock) {
    if ($null -eq $Lock) { return }
    try { $Lock.Dispose() } catch { $null = $_ }
}

function Remove-VenueTicketFile {
    # adversary A2: the one way a ticket file is removed (the owner's Remove-VenueTicket and the reaper in Request-VenueClaim): under the ticket directory's lock, so a heartbeat between its nonce
    # check and its rename cannot bring the ticket back. Same answers as Remove-VenueClaimIfUnchanged (REMOVED | CHANGED | ABSENT | BUSY); BUSY also when the lock was not free in 1 s.
    param([Parameter(Mandatory)][string]$Path, [string]$ExpectedText = $null, [string]$ExpectedNonce = '')
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 'ABSENT' }
    $lock = Enter-VenueTicketLock -TicketPath $Path -TimeoutMs 1000
    if ($null -eq $lock) { return 'BUSY' }
    try { Remove-VenueClaimIfUnchanged -Path $Path -ExpectedText $ExpectedText -ExpectedNonce $ExpectedNonce } finally { Exit-VenueTicketLock $lock }
}

function Get-VenueBackoffMs {
    # The back-off before the next attempt: 40 ms x attempt, cut to what is left of a budget (adversary A6) so a sleep never runs past it.
    param([int]$Attempt, [Diagnostics.Stopwatch]$Clock, [double]$BudgetSec = 0)
    $ms = 40.0 * $Attempt
    if ($BudgetSec -gt 0) { $ms = [Math]::Min([double]$ms, ($BudgetSec - $Clock.Elapsed.TotalSeconds) * 1000.0) }   # a [double] first operand (the Int32 overload rounds)
    [int][Math]::Floor([Math]::Max(0.0, $ms))   # Floor, never a cast ([int]0.6 is 1): a back-off never outlasts what is left
}

function Remove-VenueTicket {
    <#
    .SYNOPSIS
    End the caller's place in the line (only when the file still carries the ticket's nonce). Returns REMOVED | CHANGED | ABSENT | BUSY.
    #>
    param([Parameter(Mandatory)]$Ticket)
    $nonce = [string]$Ticket.nonce
    if ($nonce -ceq '' -and $null -ne $Ticket.PSObject.Properties['adopted']) {
        # r8 rule 1: a ticket adopted by NAME (its file was busy when Find-VenueOwnTicket found it): the nonce is read now. A file still busy cannot be ended yet (BUSY, kept and tried again); one that is not this owner's is CHANGED.
        $f = Read-VenueClaimFile $Ticket.path -ShareDelete
        if (-not $f.exists) { return 'ABSENT' }
        if ($f.busy) { return 'BUSY' }
        $j = $null; try { $j = $f.text | ConvertFrom-Json -ErrorAction Stop } catch { $j = $null }
        if (-not (Test-VenueAdoptedByName $Ticket $j)) { return 'CHANGED' }
        $nonce = [string]$j.nonce
    }
    Remove-VenueTicketFile -Path $Ticket.path -ExpectedNonce $nonce
}

function Find-VenueOwnTicket {
    # r7 adversary A7: this owner's oldest live ticket for the same run directory and purpose, as the ticket object New-VenueTicket returns, or $null. Used to take over a ticket whose creating call the caller gave up on.
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', [string]$RunDir = '', [string]$Purpose = '', [int]$OwnerPid = $PID, $NowUtc = $null)
    $ownStart = Get-VenueProcessStartUtc $OwnerPid
    foreach ($row in @(Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $NowUtc)) {   # oldest first
        if ($row.state -ceq 'STALE') { continue }
        if ($null -eq $row.ticket) {
            # r8 rule 1 (adversary r7 pass 3 A7): a ticket whose file is BUSY (its creating call was given up on while the write still held it) is this owner's when the pid and start ticks in its NAME are this owner's.
            # It is adopted by name (no nonce yet: Update-VenueTicket and Remove-VenueTicket read it from the file once it opens), never replaced by a second, later ticket.
            if ($row.state -ceq 'BUSY' -and $null -ne $ownStart -and $ownStart -ne [DateTime]::MinValue -and $row.pidKey -eq [decimal]$OwnerPid -and $row.startKey -eq [decimal]$ownStart.Ticks) {
                $seen = $null; $nonce = ''
                if ($script:TicketPhaseSeen.TryGetValue($row.path, [ref]$seen)) { $nonce = [string]$seen.nonce }
                return [pscustomobject]@{ name = $row.name; path = $row.path; nonce = $nonce; venue = $Venue; runDir = $RunDir; ownerPid = $OwnerPid; createdUtc = ''; adopted = 'by-name' }
            }
            continue
        }
        $tk = $row.ticket
        if ([string]$tk.runDir -cne $RunDir -or [string]$tk.purpose -cne $Purpose -or [string]$tk.ownerPid -cne [string]$OwnerPid -or [string]::IsNullOrEmpty([string]$tk.nonce)) { continue }
        return [pscustomobject]@{ name = $row.name; path = $row.path; nonce = [string]$tk.nonce; venue = $Venue; runDir = $RunDir; ownerPid = $OwnerPid; createdUtc = [string]$tk.createdUtc }
    }
    $null
}

function Get-VenueSettledLive {
    # r7 adversary A3 / A2: the live line (tickets that are not STALE) with every OLDER ticket whose file was busy for a moment (no parsed ticket, so no known phase) read again, for up to 1 s, before a head is chosen
    # from it (a ticket being created or renamed over is busy for milliseconds). One still unreadable after that stays "phase unknown, not ready", so a ticket held for good never blocks the line.
    param([Parameter(Mandatory)][string]$Venue, [string]$ClaimDir = '', $NowUtc = $null, [Parameter(Mandatory)][string]$MyName, $Live = $null, [string]$MyRunDir = '')
    $now = Resolve-VenueNow $NowUtc
    if ($null -eq $Live) { $Live = @(Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now | Where-Object { $_.state -cne 'STALE' }) }
    $clock = [Diagnostics.Stopwatch]::StartNew()
    while ($clock.Elapsed.TotalSeconds -lt 1.0) {
        $mine = @($Live | Where-Object { $_.name -ceq $MyName })
        # r8 rules 1 and 2: an older busy file is waited for only when it is neither the caller's own (never blocks it) nor last seen ready (its phase is known: a head)
        if ($mine.Count -eq 0 -or @($Live | Where-Object { $null -eq $_.ticket -and $_.name -cne $MyName -and $_.position -lt $mine[0].position -and -not (Test-VenueTicketPhaseReady (Get-VenueRowPhase $_)) -and -not (Test-VenueOwnTicketRow $_ $MyName $MyRunDir) }).Count -eq 0) { break }
        Start-Sleep -Milliseconds 100
        $Live = @(Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now | Where-Object { $_.state -cne 'STALE' })
    }
    $Live   # unrolled: every caller wraps the call in @()
}

function Select-VenueHead {
    # The head of a live line: the caller's own ticket, or the oldest ticket that is READY (a known ready phase). Returns zero or one row.
    # r8 rules 1 and 2: a ticket of the caller's own process and run never blocks it, and a busy file counts with the phase this process last read from it (a ticket never read stays "phase unknown, not ready").
    param($Live, [Parameter(Mandatory)][string]$MyName, [string]$MyRunDir = '')
    @($Live | Where-Object { $_.name -ceq $MyName -or ((Test-VenueTicketPhaseReady (Get-VenueRowPhase $_)) -and -not (Test-VenueOwnTicketRow $_ $MyName $MyRunDir)) } | Select-Object -First 1)
}

function Request-VenueClaim {
    <#
    .SYNOPSIS
    Try ONCE to take the venue's claim (it never waits; the caller decides whether to poll). Stale claims found on the way are broken (each once).
    With -UseTicket (or a -Ticket from an earlier call) the caller first takes / refreshes its place in the line, removes stale tickets, and only the OLDEST LIVE
    ticket may try the claim; any other gets state QUEUED. Without either it is the old ticketless attempt.
    .OUTPUTS
    [pscustomobject]@{ granted; state (TAKEN | LIVE | BUSY | QUEUED); claim (ours when granted, the holder's when LIVE or QUEUED); path; broken (one {reason; claim} per stale claim broken);
    ticket (the caller's, keep it and pass it back; $null when ticketless); queue (the live tickets, oldest first); position (the caller's place, 0 when ticketless); staleTickets (one {reason; name; ticket} per stale ticket removed); note ('' or a typed line the caller logs: TICKET_PHASE_BUSY when the flip to waiting stayed BUSY for 2 s, state QUEUED) }
    -NowUtc is the clock for tickets only (a test seam); the claim's own expiry always uses the real clock.
    #>
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$RunDir, [string]$Purpose = '', [ValidateRange(60, 86400)][int]$TtlSec = 10800,
        [string]$ClaimDir = '', [int]$OwnerPid = $PID, [switch]$UseTicket, $Ticket = $null, [ValidateRange(1, 86400)][int]$TicketStaleAfterSec = 90, $NowUtc = $null)
    $attemptArgs = @{ Venue = $Venue; RunDir = $RunDir; Purpose = $Purpose; TtlSec = $TtlSec; ClaimDir = $ClaimDir; OwnerPid = $OwnerPid }
    if (-not $UseTicket -and $null -eq $Ticket) {
        return (Invoke-VenueClaimAttempt @attemptArgs | Add-Member -NotePropertyName ticket -NotePropertyValue $null -PassThru | Add-Member -NotePropertyName queue -NotePropertyValue @() -PassThru |
            Add-Member -NotePropertyName position -NotePropertyValue 0 -PassThru | Add-Member -NotePropertyName staleTickets -NotePropertyValue @() -PassThru | Add-Member -NotePropertyName note -NotePropertyValue '' -PassThru)
    }
    $now = Resolve-VenueNow $NowUtc
    $t = $Ticket
    # r7 adversary A7: one place in the line per owner. A ticket this owner already made for this same run and purpose, whose creating call was given up on by the caller after the file was written (so the
    # caller holds no reference to it), is taken over here instead of being queued behind by a second, later ticket of the same owner. It then goes through the same flip to waiting as any ticket.
    if ($null -eq $t) { $t = Find-VenueOwnTicket -Venue $Venue -ClaimDir $ClaimDir -RunDir $RunDir -Purpose $Purpose -OwnerPid $OwnerPid -NowUtc $now }
    if ($null -ne $t) {
        # Grok CoS G7 (#878): act on EVERY answer of the cooldown -> waiting flip. LOST: taken out from under us, back of the line. BUSY: the rename was refused (a handle without FileShare.Delete), the ticket
        # still reads its old phase, so do NOT go on as if it landed (the line would see a not-ready ticket while we try for the claim): retry for up to 2 s, then answer QUEUED with a typed note.
        # adversary A6: the 2 s is measured on a monotonic clock and handed to every update as its budget (-BudgetSec), so no attempt starts and no back-off sleeps past it: a flip that lands is inside 2 s.
        $flipClock = [Diagnostics.Stopwatch]::StartNew()
        $flip = ''
        while ($true) {
            $room = 2.0 - $flipClock.Elapsed.TotalSeconds
            if ($room -le 0) { $flip = 'BUSY'; break }
            $flip = [string](Update-VenueTicket -Ticket $t -Phase 'waiting' -StaleAfterSec $TicketStaleAfterSec -NowUtc $now -BudgetSec $room)
            if ($flip -cne 'BUSY') { break }
        }
        if ($flip -ceq 'LOST') { $t = $null }
        elseif ($flip -cne 'OK') {
            $busyLine = @(Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now | Where-Object { $_.state -cne 'STALE' })
            $busyMine = @($busyLine | Where-Object { $_.name -ceq $t.name })
            # adversary A4: a refused heartbeat only matters when it left the ticket NOT ready. A ticket that already reads a ready phase (a read-only observer without FileShare.Delete refused the rename) goes on to the claim like any ready head.
            $mineReady = ($busyMine.Count -gt 0 -and $null -ne $busyMine[0].ticket -and (Test-VenueTicketPhaseReady $busyMine[0].ticket.phase))
            if (-not $mineReady) {
                $busySeen = Get-VenueClaim -Venue $Venue -ClaimDir $ClaimDir
                return [pscustomobject]@{ granted = $false; state = 'QUEUED'; claim = $busySeen.claim; path = $busySeen.path; broken = @(); ticket = $t; queue = $busyLine; position = $(if ($busyMine.Count -gt 0) { [int]$busyMine[0].position } else { 0 }); staleTickets = @()
                    note = "TICKET_PHASE_BUSY ticket=$($t.name) phase=waiting result=$flip (the flip to waiting did not land in 2 s; queued, tried again at the next request)" }
            }
        }
    }
    if ($null -eq $t) { $t = New-VenueTicket -Venue $Venue -RunDir $RunDir -Purpose $Purpose -StaleAfterSec $TicketStaleAfterSec -ClaimDir $ClaimDir -OwnerPid $OwnerPid -NowUtc $now }
    $removed = [System.Collections.Generic.List[object]]::new()
    Remove-VenueOrphanTicketTemps -Venue $Venue -ClaimDir $ClaimDir   # G8: best-effort reap of temps an earlier writer died on
    $line = Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now
    $stale = @($line | Where-Object { $_.state -ceq 'STALE' })
    foreach ($s in $stale) {
        if ((Remove-VenueTicketFile -Path $s.path -ExpectedText $s.text) -ceq 'REMOVED') { $removed.Add([pscustomobject]@{ reason = $s.reason; name = $s.name; ticket = $s.ticket }) }
    }
    if ($stale.Count -gt 0) { $line = Get-VenueTickets -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now }
    # adversary A3 / r7 A2: an OLDER ticket whose file is busy for a moment is read again (Get-VenueSettledLive) before the head is chosen, here and again after a grant
    $live = @(Get-VenueSettledLive -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now -MyName $t.name -MyRunDir $RunDir -Live @($line | Where-Object { $_.state -cne 'STALE' }))
    $mine = @($live | Where-Object { $_.name -ceq $t.name })
    $position = $(if ($mine.Count -gt 0) { [int]$mine[0].position } else { 0 })
    # VENUE-FIFO-TICKET-BEFORE-PRECLAIM-1: the head is the oldest live ticket that is READY. A ticket in phase 'cooldown' (its holder is waiting out Bachelor's lease cooldown or budget and
    # cannot take the claim yet) keeps its place but never blocks a ready waiter behind it; the caller's own ticket was just refreshed to 'waiting', so it is always ready.
    # A ticket whose phase cannot be read (no parsed ticket), or is not a KNOWN ready phase (adversary A13), is not known to be ready, so it cannot block either: it keeps its place and is judged at the next request.
    $head = @(Select-VenueHead -Live $live -MyName $t.name -MyRunDir $RunDir)
    if ($head.Count -eq 0 -or $head[0].name -cne $t.name) {
        $seen = Get-VenueClaim -Venue $Venue -ClaimDir $ClaimDir
        return [pscustomobject]@{ granted = $false; state = 'QUEUED'; claim = $seen.claim; path = $seen.path; broken = @(); ticket = $t; queue = $live; position = $position; staleTickets = @($removed); note = '' }
    }
    $r = Invoke-VenueClaimAttempt @attemptArgs
    if ($r.granted) {
        # adversary A1: the head was chosen from a snapshot, and an older ticket may have become ready since (its flip out of cooldown landed after the snapshot, while the claim was still held by another): a claim taken
        # from that stale snapshot is given back when the line NOW has an older ready ticket ahead of ours, so "ready first in line" holds when the claim frees. The older one asks again at its next poll.
        # r7 A3: the give-back is retried (a reader of the claim file refuses a release), and a claim that cannot be given back is KEPT as a grant (it still names this owner; a QUEUED answer would leak it).
        # r7 A12: a census that cannot be made (any throw) never costs a granted claim: the base returned the grant without one.
        $grantClock = [Diagnostics.Stopwatch]::StartNew()
        try {
            $after = @(Get-VenueSettledLive -Venue $Venue -ClaimDir $ClaimDir -NowUtc $now -MyName $t.name -MyRunDir $RunDir)
            $headAfter = @(Select-VenueHead -Live $after -MyName $t.name -MyRunDir $RunDir)
            if ($headAfter.Count -gt 0 -and $headAfter[0].name -cne $t.name) {
                $back = ''
                for ($giveBack = 1; $giveBack -le 5; $giveBack++) {
                    $back = [string](Release-VenueClaim -Venue $Venue -Nonce $r.claim.nonce -ClaimDir $ClaimDir)
                    if ($back -cin @('RELEASED', 'ABSENT', 'NOT_OURS')) { break }
                    Start-Sleep -Milliseconds (100 * $giveBack)
                }
                if ($back -cin @('RELEASED', 'ABSENT', 'NOT_OURS')) {
                    $seen = $null; try { $seen = Get-VenueClaim -Venue $Venue -ClaimDir $ClaimDir } catch { $seen = $null }
                    # r8 rule 7 (adversary r7 pass 3 A11): the queue returned is the census made AFTER the give-back, so the place returned is the caller's in that census (the snapshot's place could be one past it)
                    $myAfter = @($after | Where-Object { $_.name -ceq $t.name })
                    return [pscustomobject]@{ granted = $false; state = 'QUEUED'; claim = $(if ($seen) { $seen.claim }); path = $r.path; broken = @($r.broken); ticket = $t; queue = $after; position = $(if ($myAfter.Count -gt 0) { [int]$myAfter[0].position } else { $position }); staleTickets = @($removed); note = '' }
                }
            }
        } catch { $null = $_ }
        # r7 pass 2 A2: the census above can take long (a slow claim folder) and a short claim can expire meanwhile and be replaced: a grant that is more than 2 s old is checked to still name this owner before it
        # is handed over; one that does not is answered QUEUED with the holder's claim, never as a grant with an obsolete nonce.
        if ($grantClock.Elapsed.TotalSeconds -gt 2.0) {
            $own = $null; try { $own = Get-VenueClaim -Venue $Venue -ClaimDir $ClaimDir } catch { $own = $null }
            # r8 rule 3 (adversary r7 pass 3 A4): lost only on a POSITIVE answer: no claim (FREE), a stale one, or a live one that names ANOTHER nonce. A BUSY read (the file is held, and still names this owner) is not a loss.
            $lost = $null -ne $own -and ($own.state -cin @('FREE', 'STALE') -or ($own.state -ceq 'LIVE' -and $null -ne $own.claim -and -not [string]::IsNullOrEmpty([string]$own.claim.nonce) -and [string]$own.claim.nonce -cne [string]$r.claim.nonce))
            if ($lost) {
                return [pscustomobject]@{ granted = $false; state = 'QUEUED'; claim = $own.claim; path = $r.path; broken = @($r.broken); ticket = $t; queue = $live; position = $position; staleTickets = @($removed); note = '' }
            }
        }
    }
    $r | Add-Member -NotePropertyName ticket -NotePropertyValue $t -PassThru | Add-Member -NotePropertyName queue -NotePropertyValue $live -PassThru |
        Add-Member -NotePropertyName position -NotePropertyValue $position -PassThru | Add-Member -NotePropertyName staleTickets -NotePropertyValue @($removed) -PassThru | Add-Member -NotePropertyName note -NotePropertyValue '' -PassThru
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
        $age = $(if ($null -ne $claimed) { [int][Math]::Max(0.0, ((ConvertTo-VenueUtc $Status.nowUtc) - $claimed).TotalSeconds) } else { 'UNKNOWN' })
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
    param([Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)][string]$Nonce, [string]$ClaimDir = '', [ValidateRange(1, 5)][int]$MaxAttempts = 5)
    $path = Get-VenueClaimPath -Venue $Venue -ClaimDir $ClaimDir
    switch (Remove-VenueClaimIfUnchanged -Path $path -ExpectedNonce $Nonce -MaxAttempts $MaxAttempts) {
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

# r11 (Grok G17 on #814): the predicate's module path, a string only (no I/O at import). The file itself is read by Get-VenueClaimShapeSource on every call.
$script:VenueClaimShapeModulePath = Join-Path $PSScriptRoot '..\bachelor\AttrCudaArtifacts.psm1'
function Get-VenueClaimShapeSource {
    # VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r3 (adversary A1 on ce5a0380): the verbatim text of Get-AttrCudaBuildManifestClaimSha256, extracted from AttrCudaArtifacts.psm1 by
    # the same contract Get-AttrCudaEmbeddedFunctionSource embeds it into the leg and stage jobs with ("function <Name> {" ... "}" at column 0). Read as text, never imported:
    # this module imports nothing from the bachelor tree. A missing function is an error, never a silently different predicate.
    # r11 (Grok G17 on #814): read on EVERY call, never cached. Get-VenueStagedBuildManifest runs this very function INSIDE its bounded read (its text is embedded in
    # the worker's script), so the read is always inside -CallCapSec (r10's A1 on a4b659de) and a failed read (a scanner holding the file, a share blip) clears itself on
    # the next call. r10 read it once at import and kept a failure for the module's life.
    param([string]$Path = $script:VenueClaimShapeModulePath)
    # r5 (CI run 43580040634): a module that will not read is a typed refusal naming the file, never a bare ReadAllText exception
    $src = try { [IO.File]::ReadAllText($Path) } catch { throw "VENUE_CLAIM_PREDICATE_UNREADABLE the claim-shape predicate's module $Path could not be read: $($_.Exception.Message -replace '\s+', ' ')" }
    $mm = [regex]::Match($src, '(?ms)^function\s+Get-AttrCudaBuildManifestClaimSha256\s*\{.*?^\}')
    if (-not $mm.Success) { throw 'VENUE_CLAIM_PREDICATE_MISSING AttrCudaArtifacts.psm1 defines no extractable Get-AttrCudaBuildManifestClaimSha256' }
    $mm.Value
}

function Get-VenueStagedBuildManifest {
    <#
    .SYNOPSIS
    The sha256 of the build manifest already staged on a venue (<AgentShare>\cache\playback-attr-3-cuda-<sha12>-build.json, the file Invoke-VenueLeg hashes), read
    on a worker thread and given up after -TimeoutSec (a UNC path of a host that is down can block a Test-Path for minutes). Never stages anything.
    r5 (adversary A2): the same read also reports what the leg job's own validator (Assert-AttrCudaBuildManifest) needs INSIDE the file: it parses, it declares
    -SourceCommit, its dllPairManifestSha256 is 64 hex and its pendingSymbolPresence is a boolean. contentProblem says which fails ('' = none; $null = not checked,
    which is what an offline -ProbeScript that returns only a hash gets).
    .OUTPUTS
    [pscustomobject]@{ state (STAGED | NOT_STAGED | UNREACHABLE); sha256; path; error; contentProblem }
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}\z')][string]$SourceCommit, [ValidateRange(1.0, 600.0)][double]$TimeoutSec = 30, [scriptblock]$ProbeScript = $null,
        [switch]$VerifyArtifacts, [ValidateRange(0, 600)][int]$ArtifactTimeoutSec = 0, [scriptblock]$ArtifactProbeScript = $null, [ValidateRange(1.0, 600.0)][double]$ArtifactMinSec = 1,
        # Test-only (VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r2): the default manifest read first blocks its worker this long, as a read stuck on a dead SMB host does
        # (Thread.Sleep: a stop request cannot interrupt it). test_venue_stage_nightly.py uses it; production passes none.
        [ValidateRange(0, 600000)][int]$TestReadDelayMs = 0,
        # VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r6 (Grok G6/G7 on #814): the whole call (manifest read plus artifact hash) ends inside -CallCapSec seconds of its start
        # (0 = no such bound): the manifest read gets min(-TimeoutSec, -CallCapSec) (fix iteration, adversary A2 on 424df284), and the hash gets min(-ArtifactTimeoutSec,
        # what is left of -CallCapSec), none under -ArtifactMinSec (UNREACHABLE).
        # The stage nightly passes what is left of its -MaxTotalSec, so a 300 s hash never runs past the call's own cap.
        [ValidateRange(0.0, 86400.0)][double]$CallCapSec = 0,
        # Test-only (r6): the default artifact hash first blocks its worker this long, as a slow SMB hash of a large cached package does. Production passes none.
        [ValidateRange(0, 600000)][int]$TestArtifactDelayMs = 0,
        # Test-only (r10, adversary A3 on e9e6c210): the call's own thread waits this long before the manifest read, as time spent preparing it does. Production passes none.
        [ValidateRange(0, 600000)][int]$TestPreReadDelayMs = 0,
        # Test-only (r11, adversary A1 on 7754d16e): the call's own thread waits this long after the hash's budget is taken and before the hash starts, as a caller
        # descheduled there is. Production passes none.
        [ValidateRange(0, 600000)][int]$TestPreHashDelayMs = 0,
        # r12 (adversary A1 on 7754d16e, sibling Invoke-VenueStageNightly.ps1:287/:300): the caller's own deadline as an instant. -CallCapSec becomes at most what is left
        # of it when this call's clock starts, so a delay between the caller's sample and this call never moves the call's end later. $null = none.
        [Nullable[DateTime]]$CallDeadlineUtc = $null,
        # r22 (sol r21 blocker = adversary pass-23 A1 on a8e76648): a caller whose budget is MONOTONIC (the lease batch's guard budget, a Stopwatch) hands it over as that Stopwatch and its
        # end on it, never as a UTC instant: turning the remainder into an instant and back with a second UtcNow read let a forward wall-clock step (NTP, a VM resume) between the two
        # reads read as the budget spent. -CallCapSec becomes at most what is left of (-CallBudgetEndSec - -CallBudgetClock.Elapsed) when this call's clock starts, so a delay
        # between the caller's sample and this call still never moves the end later, and no wall-clock read sits between the two monotonic clocks. $null / 0 = none.
        [Diagnostics.Stopwatch]$CallBudgetClock = $null, [double]$CallBudgetEndSec = 0)
    # -ArtifactTimeoutSec 0 (the default): the artifact hash gets what is left of -TimeoutSec after the manifest read (under -ArtifactMinSec left, no hash: UNREACHABLE), so the whole call stays inside -TimeoutSec, as
    # every caller that passes 30 relies on. A caller that can afford more (the pre-claim plan preflight) or must afford less (the post-claim guard) says so.
    # r7 (sol r6 B1): -TimeoutSec and what is left of it are seconds with their fraction, never floored: the lease batch guard hands over a 2.8 s remainder as 2.8.
    # r28 (sol r27 blocker VENUE-STAGE-NONFINITE-ENDPOINT-ERASES-CAP-1, catalogue B-03): a -CallBudgetEndSec that is not a finite number (NaN, +Inf, -Inf) is REFUSED, typed,
    # before the call's clock starts and before any read. NaN passed through [Math]::Max/[Math]::Min and replaced a finite -CallCapSec (or, on a PowerShell that let the
    # assignment through, erased it: the read ran to -TimeoutSec). A non-finite endpoint never widens, and never silently narrows, the call's bound. -TimeoutSec and
    # -CallCapSec need no such check: their [ValidateRange] already refuses NaN and +/-Inf at binding.
    if (-not [double]::IsFinite($CallBudgetEndSec)) { throw "VENUE_BUDGET_ENDPOINT_NONFINITE -CallBudgetEndSec is $($CallBudgetEndSec.ToString([Globalization.CultureInfo]::InvariantCulture)); a budget endpoint that is not a finite number is refused, never read as a bound wider than -CallCapSec or -TimeoutSec" }
    $callClock = [Diagnostics.Stopwatch]::StartNew()
    if ($script:VenueTestPostClockStartDelayMs -gt 0) { $stallMs = $script:VenueTestPostClockStartDelayMs; $script:VenueTestPostClockStartDelayMs = 0; [Threading.Thread]::Sleep($stallMs) }
    if ($null -ne $CallBudgetClock -or $null -ne $CallDeadlineUtc) {
        # r12: the caller's end as what is left of it when this call's clock starts (a deadline or budget already past is a cap of 0.001 s: UNREACHABLE below, never "no cap")
        # (PowerShell hands a [Nullable[DateTime]] parameter over as the DateTime itself: no .Value)
        # r22 (sol r21 blocker): -CallBudgetClock/-CallBudgetEndSec is the caller's own monotonic budget (a Stopwatch and its end on it): no wall clock is read for it at all.
        # r22 fix iteration 1 (adversary pass-24 A1 on e3e61e8e, sibling of r12's own sample): the left-over is taken on the CALLER's clock, so it is a remainder AT THE SAMPLE, and the cap is
        # held against this call's clock, which has run since its start: the cap is that remainder plus what this clock read at the same instant. The two reads are two instants, so they
        # are BRACKETED: this clock, the caller's, this clock again. A caller descheduled before the first read is charged once (it is in both); a stall INSIDE the bracket (over
        # 50 ms between its two reads of this clock) is a sample of no instant, so the bracket is taken again (up to 5 times; the last keeps the first read, which only shortens).
        $sampleTry = 0
        do {
            $sampleTry++
            $sampleElapsed = $callClock.Elapsed.TotalSeconds
            if ($script:VenueTestDeadlineSampleDelayMs -gt 0) { $stallMs = $script:VenueTestDeadlineSampleDelayMs; $script:VenueTestDeadlineSampleDelayMs = 0; [Threading.Thread]::Sleep($stallMs) }
            $bdLeft = $(if ($null -ne $CallBudgetClock) { $CallBudgetEndSec - $CallBudgetClock.Elapsed.TotalSeconds } else { $null })
            $dlLeft = $(if ($null -ne $CallDeadlineUtc) { (([DateTime]$CallDeadlineUtc).ToUniversalTime() - [DateTime]::UtcNow.AddSeconds($script:VenueTestUtcStepSec)).TotalSeconds } else { $null })
        } while (($callClock.Elapsed.TotalSeconds - $sampleElapsed) -gt 0.05 -and $sampleTry -lt 5)
        if ($null -ne $bdLeft) {
            $bdCap = [Math]::Min(86400.0, [Math]::Max(0.001, $bdLeft + $sampleElapsed))
            $CallCapSec = $(if ($CallCapSec -gt 0) { [Math]::Min($CallCapSec, $bdCap) } else { $bdCap })
        }
        if ($null -ne $dlLeft) {
            $dlCap = [Math]::Min(86400.0, [Math]::Max(0.001, $dlLeft + $sampleElapsed))
            $CallCapSec = $(if ($CallCapSec -gt 0) { [Math]::Min($CallCapSec, $dlCap) } else { $dlCap })
        }
    }
    $path = $AgentShare.TrimEnd('\') + '\cache\playback-attr-3-cuda-' + $SourceCommit.Substring(0, 12) + '-build.json'
    # -ProbeScript is the offline test seam for a read that never answers; production passes none.
    $read = $(if ($null -ne $ProbeScript) { $ProbeScript } else {
            {
                param($p)
                # r13 (Dot A1''): a read that fails (a manifest a writer holds open, a share error) ends the read, so it is UNREACHABLE with its own error. Under the runspace's default
                # Continue, a failed byte read went on with no bytes and the read reported NOT_STAGED, a manifest it never looked at.
                $ErrorActionPreference = 'Stop'
                if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { return '' }
                # ONE read: the digest and the facts below come from the same bytes (a file replaced between two reads would pair one version's hash with another's content)
                $bytes = [IO.File]::ReadAllBytes($p)
                $sha = [Security.Cryptography.SHA256]::Create()
                try { ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
                # r6 (adversary A1): decoded exactly as Assert-AttrCudaBuildManifest's own file read does (UTF-8 default, ONE BOM consumed, UTF-16/32 by BOM), from the bytes already hashed
                $m = try { [IO.StreamReader]::new([IO.MemoryStream]::new($bytes), [Text.Encoding]::UTF8, $true).ReadToEnd() | ConvertFrom-Json } catch { $null }
                if ($null -eq $m) { 'UNPARSEABLE'; return }
                $c = $m.PSObject.Properties['sourceCommit']; 'C:' + $(if ($null -eq $c) { '' } else { [string]$c.Value })
                $d = $m.PSObject.Properties['dllPairManifestSha256']; 'D:' + $(if ($null -eq $d) { '' } else { ([string]$d.Value).ToLowerInvariant() })
                $s = $m.PSObject.Properties['pendingSymbolPresence']; 'S:' + $(if ($null -ne $s -and $s.Value -is [bool]) { 'bool' } else { 'bad' })
                # r13 (adversary r13 A1): and the three artifact digests the job hashes the cached artifacts against (playback-attr-3-cuda-job.ps1 $manifestChecks): a string of
                # 64 hex, the only value Get-Sha's digest can ever equal; a blank one makes the job throw, a non-string one throws in ToUpperInvariant.
                # VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r3 (adversary A1 on ce5a0380): each claim is read by Get-AttrCudaBuildManifestClaimSha256, the ONE claim-shape predicate
                # (tools/profiling/bachelor/AttrCudaArtifacts.psm1). Its TEXT is defined in this read's runspace below, the same text the leg job, the reference-frame runner and the
                # stage job's generator, binding and host guard embed, so every one of them accepts exactly the same claims. ('' = not of the accepted shape.)
                # This read is the ONE share-side soundness predicate: the nightly's reuse decision and the lease batch's plan and post-claim guards all take it.
                foreach ($k in 'packageZip', 'exe', 'dll') {
                    $v = Get-AttrCudaBuildManifestClaimSha256 $m $k
                    'A:' + $k + '=' + $(if ($v -cmatch '^[0-9a-f]{64}\z') { 'ok' } else { 'bad' })
                    # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1: the digest itself, for -VerifyArtifacts (the same bytes as the facts above)
                    if ($v -cmatch '^[0-9a-f]{64}\z') { 'H:' + $k + '=' + $v }
                }
            } })
    # r3: the default read runs with the claim-shape predicate defined (the read's own scriptblock is invoked inside, so it sees the function)
    # r12 (Grok G17 on #814): the predicate's module is read INSIDE this bounded read, on every call: Get-VenueClaimShapeSource's own text is defined in the worker and
    # run there, so the cap bounds that read too (r10's A1 on a4b659de) and a failed read (a scanner holding the file) clears on the next call. Its typed refusal comes
    # back as one VENUE_PREDICATE_REFUSED: line and is rethrown here, as the caller-side read threw it.
    $predicatePath = [string]$script:VenueClaimShapeModulePath
    $readText = $(if ($null -eq $ProbeScript) {
            "param(`$p)`nfunction Get-VenueClaimShapeSource {" + ${function:Get-VenueClaimShapeSource}.ToString() + "}`n" +
            "try { . ([scriptblock]::Create((Get-VenueClaimShapeSource -Path '" + ($predicatePath -replace "'", "''") + "'))) } catch { 'VENUE_PREDICATE_REFUSED:' + `$_.Exception.Message; return }`n" +
            $(if ($TestReadDelayMs -gt 0) { "[Threading.Thread]::Sleep($TestReadDelayMs)`n" } else { '' }) + "& {" + $read.ToString() + "`n} `$p"
        } else { $read.ToString() })
    # r6 fix iteration (adversary A2 on 424df284): -CallCapSec bounds the WHOLE call, the manifest read too (it bounded only the hash, so a read stuck past the cap
    # answered NOT_STAGED after it). Under 1 s of it (the least a read can be given) there is no read: UNREACHABLE.
    if ($TestPreReadDelayMs -gt 0) { [Threading.Thread]::Sleep($TestPreReadDelayMs) }
    # r12 (adversary A1 on 7754d16e, sibling :695): the read's end is fixed ONCE on the call clock, when its budget is taken, and the bounded wait ends at it (-Clock/-EndSec):
    # a remainder handed over as a number was counted again from the wait's own later start
    $readStartSec = $callClock.Elapsed.TotalSeconds
    # r10 (adversary A3 on e9e6c210): the end of the call's bound on its own clock (-TimeoutSec, or -CallCapSec when sooner), what an unbounded hash is given what is left of.
    # $readSec is a remainder already (the cap less the time before the read), so the hash's remainder taken from it counted that time twice.
    $boundEndSec = $(if ($CallCapSec -gt 0) { [Math]::Min($TimeoutSec, $CallCapSec) } else { $TimeoutSec })
    if ($CallCapSec -gt 0) {
        $capLeft = $CallCapSec - $readStartSec
        # r14 fix iteration 1 (adversary A1 on f7408e9f, sibling :723): the floor is $script:VenueMinReadSec (0.5 s), never 1: -TimeoutSec / -CallCapSec 1 (the least
        # they accept) less ANY preparation time was under 1 s, so the minimum bound could never read at all
        if ($capLeft -lt $script:VenueMinReadSec) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = "the staged manifest could not be read: no time is left of -CallCapSec $([Math]::Round($CallCapSec, 2))$(if ($null -ne $CallDeadlineUtc) { ' (-CallDeadlineUtc ' + ([DateTime]$CallDeadlineUtc).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ', [Globalization.CultureInfo]::InvariantCulture) + ')' })$(if ($null -ne $CallBudgetClock) { ' (-CallBudgetEndSec ' + [Math]::Round($CallBudgetEndSec, 2).ToString([Globalization.CultureInfo]::InvariantCulture) + ' on the caller''s clock)' }) ($([Math]::Floor([Math]::Max(0.0, $capLeft) * 100.0) / 100.0) s left, under $script:VenueMinReadSec)"; contentProblem = $null } }
    }
    # r13 (adversary A1 on 5fe750fa, siblings Invoke-VenueChain.ps1:354 / Invoke-VenueLeaseBatch.ps1:1250): the read ends at the BOUND's end on the call clock
    # ($boundEndSec: -TimeoutSec, or -CallCapSec when sooner), the end the hash below is measured against. r12 ended it at $readStartSec + -TimeoutSec, so time
    # spent before the read (preparing it, a descheduled caller) ran the call past -TimeoutSec: -TimeoutSec 1 after 1.5 s answered NOT_STAGED at ~2.2 s.
    # Under 1 s of it left (the least a read can be given) there is no read: UNREACHABLE.
    $readSec = $boundEndSec - $readStartSec
    if ($readSec -lt $script:VenueMinReadSec) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = "the staged manifest could not be read: no time is left of -TimeoutSec $([Math]::Round($TimeoutSec, 2)) ($([Math]::Floor([Math]::Max(0.0, $readSec) * 100.0) / 100.0) s left, under $script:VenueMinReadSec)"; contentProblem = $null } }
    # r14 (Grok G21 on #814): the read is handed the caller's REAL bound (-TimeoutSec) and -EndSec does the clamping, so a cut-short wait names the bound the
    # operator set ("cut the 2 s bound short"), never the remainder $readSec, which the wait's own later sample is always a little under (the clause fired on every timeout)
    # r16 (Grok G22 on #814): the read's end is the bound's OWN end unless -CallCapSec lowered it under -TimeoutSec; only then did something cut the bound short
    # (-EndIsBoundEnd off), so a plain timeout carries no clause
    $readCapped = ($CallCapSec -gt 0 -and $CallCapSec -lt $TimeoutSec)
    $r = Invoke-VenueBoundedRead -ScriptText $readText -Argument $path -TimeoutSec $TimeoutSec -Clock $callClock -EndSec $boundEndSec -EndIsBoundEnd:(-not $readCapped)
    if (-not $r.answered) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = $r.error; contentProblem = $null } }
    if ($null -eq $ProbeScript -and $r.value.Count -gt 0 -and ([string]$r.value[0]).StartsWith('VENUE_PREDICATE_REFUSED:', [StringComparison]::Ordinal)) { throw ([string]$r.value[0]).Substring('VENUE_PREDICATE_REFUSED:'.Length) }
    $sha = $(if ($r.value.Count -gt 0) { [string]$r.value[0] } else { '' })
    if ($sha -cmatch '^[0-9a-f]{64}$') {
        $problem = $null
        if ($r.value.Count -gt 1) {
            $facts = @($r.value | Select-Object -Skip 1 | ForEach-Object { [string]$_ })
            $commit = @($facts | Where-Object { $_.StartsWith('C:') }) | Select-Object -First 1
            $dll = @($facts | Where-Object { $_.StartsWith('D:') }) | Select-Object -First 1
            $sym = @($facts | Where-Object { $_.StartsWith('S:') }) | Select-Object -First 1
            $problem = $(if ($facts -contains 'UNPARSEABLE') { 'ATTRCUDA_BUILD_MANIFEST_UNPARSEABLE the staged manifest is not JSON' }
                elseif (-not [string]::Equals($commit.Substring(2), $SourceCommit, [StringComparison]::OrdinalIgnoreCase)) { "ATTRCUDA_BUILD_MANIFEST_COMMIT_MISMATCH the staged manifest declares '$($commit.Substring(2))', expected '$SourceCommit'" }
                elseif ($dll.Substring(2) -notmatch '^[0-9a-f]{64}$') { "ATTRCUDA_BUILD_MANIFEST_DLLPAIR_UNBOUND the staged manifest carries no well-formed dllPairManifestSha256 (got '$($dll.Substring(2))')" }
                elseif ($sym -ne 'S:bool') { 'ATTRCUDA_BUILD_MANIFEST_SYMBOL_PRESENCE_INVALID the staged manifest pendingSymbolPresence is not a boolean' }
                elseif ($badArt = @('packageZip', 'exe', 'dll' | Where-Object { $facts -cnotcontains "A:$_=ok" })) { "ATTRCUDA_BUILD_MANIFEST_ARTIFACT_SHA_MISSING the staged manifest carries no 64-hex sha256 for $($badArt -join ', ') (the leg job hashes the cached artifact against it)" }
                else { '' })
        }
        # LEASE-BATCH-PLAN-ARTIFACT-PREFLIGHT-1 (Dot 6440180395): a manifest whose digests are well-formed is not yet a build the leg job can run -- it hashes the CACHED exe, dll and
        # package zip against them (playback-attr-3-cuda-job.ps1 $manifestChecks) and throws after the lease opened when one is absent or differs. With -VerifyArtifacts the same
        # hash is taken here, once the manifest content is sound: artifactProblem is '' (all three match), BUILD_ARTIFACT_MISSING or BUILD_ARTIFACT_MISMATCH (first word = the typed
        # code), or $null (not asked, or the digests were not read). An artifact read that does not answer is UNREACHABLE, as a manifest read that does not answer is.
        $artifactProblem = $null
        if ($VerifyArtifacts -and $problem -ceq '') {
            $names = Get-VenueStagedArtifactNames -SourceCommit $SourceCommit
            $cacheDir = $AgentShare.TrimEnd('\') + '\cache'
            $items = @(foreach ($k in 'packageZip', 'exe', 'dll') {
                    $h = @($r.value | Select-Object -Skip 1 | ForEach-Object { [string]$_ } | Where-Object { $_.StartsWith("H:$k=") }) | Select-Object -First 1
                    @{ label = $k; name = $names[$k]; path = ($cacheDir + '\' + $names[$k]); sha = $h.Substring(("H:$k=").Length) }
                })
            $probe = $(if ($null -ne $ArtifactProbeScript) { $ArtifactProbeScript } else {
                    {
                        param($items)
                        # a read that fails ends the read (UNREACHABLE), never "absent": an unreadable file is not a missing one
                        $ErrorActionPreference = 'Stop'
                        foreach ($i in $items) {
                            # r7 adversary A2: -ArtifactMinSec is the least EACH file's read may start with, not only the first: a slow packageZip left the exe and the dll under it
                            if ($null -ne $i.clock -and ($i.endSec - $i.clock.Elapsed.TotalSeconds) -lt $i.minSec) { 'NOTIME:' + $i.label + ':' + $i.name; break }
                            if (-not (Test-Path -LiteralPath $i.path -PathType Leaf)) { 'MISSING:' + $i.label + ':' + $i.name; continue }
                            # r8b adversary A1: the existence check can take the time too, so the minimum is checked again before the open and the hash start
                            if ($null -ne $i.clock -and ($i.endSec - $i.clock.Elapsed.TotalSeconds) -lt $i.minSec) { 'NOTIME:' + $i.label + ':' + $i.name; break }
                            # r8b adversary A1: FileShare.Read, as the leg job hashes it (AttrCudaArtifacts.psm1 Get-Sha): a writer holding the file open fails this read as it would fail the leg
                            $fs = [IO.File]::Open($i.path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
                            try {
                                # r10 adversary A1: the open can take the time too (an SMB open), so the minimum is checked once more with the file open, before the hash starts
                                if ($null -ne $i.clock -and ($i.endSec - $i.clock.Elapsed.TotalSeconds) -lt $i.minSec) { 'NOTIME:' + $i.label + ':' + $i.name; break }
                                $sha = [Security.Cryptography.SHA256]::Create()
                                try { $got = ([BitConverter]::ToString($sha.ComputeHash($fs)) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
                            } finally { $fs.Dispose() }
                            if ($got -cne $i.sha) { 'MISMATCH:' + $i.label + ':' + $i.name + ':' + $got }
                        }
                    } })
            # adversary r2 A2: with no explicit bound the hash gets what is LEFT of -TimeoutSec, and none when less than -ArtifactMinSec is left (a 1 s floor would run the call past its own bound)
            # r12 (adversary A1 on 7754d16e, siblings :754/:762/:769/:776): the hash's END is fixed once, here, on the call clock -- explicit: min(now + -ArtifactTimeoutSec,
            # -CallCapSec); implicit: the bound's end -- and everything after is measured against it. r6-r11 took a remainder here and re-anchored it at the hash start
            # (hashEnd = elapsed + remainder), so a caller descheduled in between moved the end past the cap.
            $hashStartSec = $callClock.Elapsed.TotalSeconds
            $hashEndSec = $boundEndSec
            # r16 (Grok G22 on #814): did -CallCapSec lower the hash's end? Implicit budget: exactly when it lowered the bound's end; explicit: set below
            $hashCapped = $readCapped
            $artSec = $ArtifactTimeoutSec
            if ($artSec -gt 0) { $hashEndSec = $hashStartSec + $artSec; $hashCapped = $false }
            if ($artSec -gt 0 -and $CallCapSec -gt 0) {
                # r6 (Grok G6/G7 on #814): an explicit hash budget never runs the call past -CallCapSec; under -ArtifactMinSec left of it, no hash (UNREACHABLE)
                $capLeft = $CallCapSec - $hashStartSec
                if ($capLeft -lt $ArtifactMinSec) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = "the cached artifacts could not be hashed: no time is left of -CallCapSec $([Math]::Round($CallCapSec, 2)) after the manifest read ($([Math]::Floor([Math]::Max(0.0, $capLeft) * 100.0) / 100.0) s left, under $ArtifactMinSec)"; contentProblem = $null; artifactProblem = $null; artifactsUnhashed = $true } }
                if ($CallCapSec -lt $hashEndSec) { $hashEndSec = $CallCapSec; $hashCapped = $true }
            }
            if ($artSec -le 0) {
                # r7 (sol r6 B1): the exact remainder, not its whole seconds (2.7 s left was floored to 1, under the guard's 2 s minimum, and a healthy hash never started)
                # r6 fix iteration (A2): what is left of the manifest read's own bound, which -CallCapSec already shortened
                # r10 (adversary A3 on e9e6c210): from the bound's end on the call clock, never from $readSec (the time before the read was subtracted twice)
                $left = $boundEndSec - $hashStartSec
                # r10 (adversary pass 2 A3): the time left is printed rounded DOWN to hundredths -- Round(1.999, 2) printed "2 s left, under 2"
                # r5 (adversary r5 A2): -ArtifactMinSec is the least a hash may START with; the lease batch guard asks for its own minimum ($script:GuardReadMinSec, the same rule)
                if ($left -lt $ArtifactMinSec) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = "the cached artifacts could not be hashed: no time is left of -TimeoutSec $([Math]::Round($TimeoutSec, 2)) after the manifest read ($([Math]::Floor([Math]::Max(0.0, $left) * 100.0) / 100.0) s left, under $ArtifactMinSec)"; contentProblem = $null; artifactProblem = $null; artifactsUnhashed = $true } }
            }
            if ($TestPreHashDelayMs -gt 0) { [Threading.Thread]::Sleep($TestPreHashDelayMs) }
            # r12 (A1 on 7754d16e): what is left of that fixed end is taken again right before the launch; under -ArtifactMinSec of it, no hash (UNREACHABLE)
            $left = $hashEndSec - $callClock.Elapsed.TotalSeconds
            if ($left -lt $ArtifactMinSec) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = "the cached artifacts could not be hashed: no time is left before the hash's end ($([Math]::Round($hashEndSec, 2)) s into the call) to start it ($([Math]::Floor([Math]::Max(0.0, $left) * 100.0) / 100.0) s left, under $ArtifactMinSec)"; contentProblem = $null; artifactProblem = $null; artifactsUnhashed = $true } }
            # r7 adversary A2: the hash's end on the call's own clock, so the worker checks the minimum again before each file (r12: the fixed end, never re-anchored)
            $hashEnd = $hashEndSec
            foreach ($it in $items) { $it.clock = $callClock; $it.endSec = $hashEnd; $it.minSec = $ArtifactMinSec }
            # VENUE-STAGE-SAME-SHA-RESTAGE-GUARD-1 r8 (adversary A1 on 0c0eafd7): the default hash reads the manifest itself again LAST, in the same bounded read, against the
            # digest read above: a manifest a stager retired or replaced while its artifacts were hashed is BUILD_ARTIFACT_MISSING / _MISMATCH, never sound on its successor's
            # artifacts. No clock: a few-byte read is never NOTIME. An offline -ArtifactProbeScript still sees the three artifacts only.
            if ($null -eq $ArtifactProbeScript) { $items += @{ label = 'manifest'; name = [IO.Path]::GetFileName($path); path = $path; sha = $sha } }
            $probeText = $(if ($TestArtifactDelayMs -gt 0 -and $null -eq $ArtifactProbeScript) { "param(`$items)`n[Threading.Thread]::Sleep($TestArtifactDelayMs)`n& {" + $probe.ToString() + "`n} `$items" } else { $probe.ToString() })
            # r14 (Grok G21 on #814): the hash is handed its configured budget (-ArtifactTimeoutSec, or -TimeoutSec when it has none) and -EndSec ($hashEnd) clamps it,
            # as the manifest read above: the clause names the budget the operator set, never a remainder of it (the remainder stores into $artSec went with it; the
            # fixed end $hashEndSec, clamped to -CallCapSec above, is the only bound the wait takes)
            $ar = Invoke-VenueBoundedRead -ScriptText $probeText -Argument $items -TimeoutSec $(if ($ArtifactTimeoutSec -gt 0) { [double]$ArtifactTimeoutSec } else { $TimeoutSec }) -Clock $callClock -EndSec $hashEnd -EndIsBoundEnd:(-not $hashCapped)
            if (-not $ar.answered) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = "the cached artifacts could not be hashed: $($ar.error)"; contentProblem = $null; artifactProblem = $null; artifactsUnhashed = $true } }
            $lines = @($ar.value | ForEach-Object { [string]$_ })
            $missing = @($lines | Where-Object { $_.StartsWith('MISSING:') } | ForEach-Object { $p = $_.Split(':', 3); "$($p[1]) $($p[2])" })
            $differs = @($lines | Where-Object { $_.StartsWith('MISMATCH:') } | ForEach-Object { $p = $_.Split(':', 4); "$($p[1]) $($p[2]) hashes to $($p[3])" })
            $notime = @($lines | Where-Object { $_.StartsWith('NOTIME:') } | ForEach-Object { $p = $_.Split(':', 3); "$($p[1]) $($p[2])" })
            # a file found absent or different before the time ran out is still that typed problem; with none, a file never started is a hash that did not finish (UNREACHABLE)
            if (-not $missing.Count -and -not $differs.Count -and $notime.Count) { return [pscustomobject]@{ state = 'UNREACHABLE'; sha256 = $null; path = $path; error = "the cached artifacts could not be hashed: no time is left of -TimeoutSec $([Math]::Round($TimeoutSec, 2)) to start hashing $($notime[0]) (under $ArtifactMinSec s left)"; contentProblem = $null; artifactProblem = $null; artifactsUnhashed = $true } }
            # r9 (Grok G11 on #814): the manifest item gone or changed keeps its typed code, but its cause is the manifest, never a missing exe
            $manifestGone = @(@($missing) + @($differs) | Where-Object { $_.StartsWith('manifest ') })
            $artifactProblem = $(if ($manifestGone.Count) { "$(if (@($missing | Where-Object { $_.StartsWith('manifest ') }).Count) { 'BUILD_ARTIFACT_MISSING' } else { 'BUILD_ARTIFACT_MISMATCH' }) the build manifest was retired or replaced while its artifacts were hashed: $($manifestGone -join '; ') in $cacheDir" }
                elseif ($missing.Count) { "BUILD_ARTIFACT_MISSING the cached artifact is absent from $cacheDir`: $($missing -join '; ') (the leg job hashes it against the build manifest and throws after the lease opened)" }
                elseif ($differs.Count) { "BUILD_ARTIFACT_MISMATCH the cached artifact does not hash to the build manifest's digest in ${cacheDir}: $($differs -join '; ') (the leg job throws after the lease opened)" }
                else { '' })
        }
        return [pscustomobject]@{ state = 'STAGED'; sha256 = $sha; path = $path; error = $null; contentProblem = $problem; artifactProblem = $artifactProblem }
    }
    [pscustomobject]@{ state = 'NOT_STAGED'; sha256 = $null; path = $path; error = $null; contentProblem = $null; artifactProblem = $null }
}

function Get-VenueStagedArtifactNames {
    <#
    .SYNOPSIS
    The cached artifact file names for a build, by the formulas Get-AttrCudaArtifactNames (tools/profiling/bachelor/AttrCudaArtifacts.psm1) derives and the leg job reads from the
    agent cache. Kept here as a copy because this module imports nothing from the bachelor tree; test_venue_lease_batch.py pins the two together.
    #>
    param([Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{40}\z')][string]$SourceCommit)
    $s = $SourceCommit.Substring(0, 12)
    @{ exe = "MLVApp-playback-attr-3-cuda-$s.exe"; dll = "igpu_recon_cuda-playback-attr-3-cuda-$s.dll"; packageZip = "MLVApp-playback-attr-3-cuda-$s-pkg.zip" }
}

$script:VenueAbandonedReads = 0
# r14 fix iteration 1 (adversary A1 on f7408e9f): the least time a staged-manifest read may START with. Under 1, so the least bound the call accepts
# (-TimeoutSec 1, -CallCapSec 1) still reads once its preparation took a few ms; the read itself is handed the caller's bound and ends at -EndSec.
$script:VenueMinReadSec = 0.5
# Test-only seams (r12 fix iteration, adversary A1/A2 on 09421b77), set from a test through the module scope; production leaves both 0. The first stalls the
# caller between the two clock samples -CallDeadlineUtc is taken from, the second between a bounded read's launch and its wait (a descheduled caller).
$script:VenueTestDeadlineSampleDelayMs = 0
$script:VenueTestPreWaitDelayMs = 0
# Test-only (r22 fix iteration 1, adversary pass-24 A1 on e3e61e8e): ONE stall right after the call's own clock starts and before the caller's budget or deadline is sampled, as a callee
# descheduled there is. It clears itself once used, and so does -VenueTestDeadlineSampleDelayMs (a deschedule happens once, and the sample is then taken again). Production leaves both 0.
$script:VenueTestPostClockStartDelayMs = 0
# Test-only (r22, sol r21 blocker / adversary pass-23 A1): the wall clock Get-VenueStagedBuildManifest's -CallDeadlineUtc is read against is stepped forward this many seconds, as an
# NTP/VM-resume step between a caller's own wall-clock sample and this call's is. Production leaves it 0. The lease batch no longer hands a monotonic budget over as an instant at
# all (-CallBudgetClock/-CallBudgetEndSec), so a step cannot reach its call; -CallDeadlineUtc stays for the nightly, whose own cap is a UTC deadline end to end.
$script:VenueTestUtcStepSec = 0
# Test-only (VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-R3-ADV-RESIDUE-1 r2, adversary-7efea617.json A4): Read-VenueFileBytesBounded's worker sleeps this many ms before it reads, as a share that
# answers late does. Production leaves it 0.
$script:VenueTestFileReadDelayMs = 0
# Test-only (VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R4-NARROW-1 r2, adversary-6596e055.json A3): Invoke-VenueBoundedRead sleeps this many ms after it created the worker and gave it its script and
# argument, right before the budget is looked at again and BeginInvoke runs, as a caller descheduled during worker setup does. Production leaves it 0.
$script:VenueTestPreBeginDelayMs = 0
# Test-only (VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R4-NARROW-1 r2, adversary-d10f5241.json A2): Read-VenueFileBytesBounded sleeps this many ms after its read answered and before it judges the answer against
# -EndUtc, as a caller suspended (or a wall clock stepped) inside the read does. Production leaves it 0.
$script:VenueTestPostReadDelayMs = 0
# Test-only (R2-RESIDUE-1 r1, adversary-3be19c39.json A2): Get-VenueLegHostLoad sleeps this many ms after Read-VenueFileBytesBounded handed it its bytes and before it classifies them, as a caller suspended
# between the read's own end check and the classification is. Production leaves it 0.
$script:VenueTestPostBytesDelayMs = 0
$script:VenueTestPostReceiptBytesDelayMs = 0   # the same stall after the RECEIPT read (the export-exemption return acts on that answer)
# Test-only (R2-NARROW-1 r7, adversary-aab90f89.json A1): Get-VenueLegHostLoad sleeps this many ms after it starts its read stopwatch and before it samples the caller's deadline, as a caller descheduled there is. Production leaves it 0.
$script:VenueTestPreDeadlineSampleDelayMs = 0
# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R4-NARROW-1 r2 (adversary-6596e055.json A3): the worker of Read-VenueFileBytesBounded. It is handed the ABSOLUTE UTC end of the caller's budget (endUtcTicks, 0 = none) and
# checks it before the existence probe and again before ReadAllBytes, so a read that was started in time still performs no I/O once the end has passed. (No apostrophe in the messages: this is single-quoted text.)
# Test-only (R2-NARROW-1 r2, adversary-baf2e69a.json A1): Read-VenueFileBytesBounded sleeps this many ms right after it samples the caller's stopwatch, as a caller descheduled there is, and with
# -VenueTestCaptureWorkerArg set keeps the argument it handed its worker in -VenueTestLastFileWorkerArg. Production leaves them 0 / $false / $null.
$script:VenueTestReadSampleDelayMs = 0
$script:VenueTestCaptureWorkerArg = $false
$script:VenueTestLastFileWorkerArg = $null
# R2-NARROW-1 r2 (adversary-7c5bfe86.json A4): the worker is handed the caller's STOPWATCH and its end too (clock, clockEndSec), and refuses when EITHER end has passed: a wall clock stepped back after the UTC end
# was projected leaves that end far in the future, while the stopwatch cannot step.
$script:VenueFileReadWorkerText = 'param($a) $ErrorActionPreference = ''Stop''; if ($a.delayMs -gt 0) { [Threading.Thread]::Sleep([int]$a.delayMs) }; if (($a.endUtcTicks -gt 0 -and [DateTime]::UtcNow.Ticks -ge $a.endUtcTicks) -or ($null -ne $a.clock -and $a.clock.Elapsed.TotalSeconds -ge $a.clockEndSec)) { throw ''the call end had passed before the read started; no file was touched'' }; if ($a.missingOk -and -not (Test-Path -LiteralPath $a.path -PathType Leaf)) { return }; if (($a.endUtcTicks -gt 0 -and [DateTime]::UtcNow.Ticks -ge $a.endUtcTicks) -or ($null -ne $a.clock -and $a.clock.Elapsed.TotalSeconds -ge $a.clockEndSec)) { throw ''the call end passed during the existence probe; the file was not read'' }; ,[IO.File]::ReadAllBytes($a.path)'

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
    # r7 (sol r6 B1): -TimeoutSec keeps its fraction (a caller's exact remainder), so no bound is rounded up past the caller's deadline or down under its minimum
    # r12 (adversary A1 on 7754d16e, sibling :836): -Clock/-EndSec, the caller's fixed end on its own clock. The wait is min(-TimeoutSec, EndSec - Clock), sampled right
    # before it (after the worker started), so time spent creating the worker is never added past the caller's end. No -Clock: -TimeoutSec from the wait, as before.
    # r16 (Grok G22 on #814): -EndIsBoundEnd says -EndSec is the end of -TimeoutSec ITSELF (measured from an earlier start on the caller's clock), not a cap the
    # caller set under it; a wait it shortens is then a plain timeout and the "cut ... short" clause is not printed. Absent: any shortening by -EndSec is named.
    param([Parameter(Mandatory)][string]$ScriptText, [Parameter(Mandatory)]$Argument, [Parameter(Mandatory)][ValidateRange(1.0, 600.0)][double]$TimeoutSec,
        [Diagnostics.Stopwatch]$Clock = $null, [double]$EndSec = 0, [switch]$EndIsBoundEnd)
    # r28 (sol r27 blocker VENUE-STAGE-NONFINITE-ENDPOINT-ERASES-CAP-1, catalogue B-03, sibling of Get-VenueStagedBuildManifest -CallBudgetEndSec): an -EndSec that is not a
    # finite number is REFUSED, typed, before the worker starts. NaN skipped both the clamp (NaN -lt x is false: the full -TimeoutSec wait) and the late-answer rejection
    # (Elapsed - NaN -gt 0 is false), so an answer past the caller's end was acted on.
    if (-not [double]::IsFinite($EndSec)) { throw "VENUE_BUDGET_ENDPOINT_NONFINITE -EndSec is $($EndSec.ToString([Globalization.CultureInfo]::InvariantCulture)); a budget endpoint that is not a finite number is refused, never read as a wait wider than the caller's end" }
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R3-MERGE-RESIDUE-1 r1 (adversary-55dcf49e.json A3): a caller whose budget is already spent starts NO read. The worker used to be launched first and the
    # clock looked at only afterwards, so a caller descheduled past its end still sent the worker to do the I/O (Test-Path, ReadAllBytes) and merely discarded the answer; the late-answer
    # check below protects the result, never the I/O. Every caller that hands over -Clock/-EndSec (Read-VenueFileBytesBounded, so Get-VenueLegHostLoad's second read, the chain's and the
    # batch's host-load reads, and the staged-manifest reads) gets this one check, so no per-site copy can miss it.
    if ($null -ne $Clock -and ($EndSec - $Clock.Elapsed.TotalSeconds) -le 0) {
        return [pscustomobject]@{ answered = $false; value = @(); error = "no time is left of the caller's budget to start the read (its end passed $([Math]::Round($Clock.Elapsed.TotalSeconds - $EndSec, 2)) s ago); nothing was started" }
    }
    $ps = [powershell]::Create()
    $abandoned = $false
    try {
        [void]$ps.AddScript($ScriptText)
        [void]$ps.AddArgument($Argument)
        # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R4-NARROW-1 r2 (adversary-6596e055.json A3): the budget is looked at AGAIN right before BeginInvoke. The check above runs before the worker is created and given
        # its script and argument; a caller descheduled during that setup used to resume past its end and still start the I/O. (Disposed unstarted by the finally below.)
        if ($script:VenueTestPreBeginDelayMs -gt 0) { [Threading.Thread]::Sleep($script:VenueTestPreBeginDelayMs) }
        if ($null -ne $Clock -and ($EndSec - $Clock.Elapsed.TotalSeconds) -le 0) {
            return [pscustomobject]@{ answered = $false; value = @(); error = "no time is left of the caller's budget to start the read (its end passed $([Math]::Round($Clock.Elapsed.TotalSeconds - $EndSec, 2)) s ago, during the worker's setup); nothing was started" }
        }
        $async = $ps.BeginInvoke()
        if ($script:VenueTestPreWaitDelayMs -gt 0) { [Threading.Thread]::Sleep($script:VenueTestPreWaitDelayMs) }
        # r13 (Grok G20 on #814): $endCut = the caller's end, not -TimeoutSec, set the wait (as the nightly's capCut), so the message reports the wait actually given
        $waitSec = $TimeoutSec; $endCut = $false
        if ($null -ne $Clock) { $toEnd = $EndSec - $Clock.Elapsed.TotalSeconds; if ($toEnd -lt $waitSec) { $waitSec = [Math]::Max(0.0, $toEnd); $endCut = $true } }
        if (-not $async.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($waitSec))) {
            $abandoned = $true
            $script:VenueAbandonedReads++
            try { [void]$ps.BeginStop($null, $null) } catch { }
            return [pscustomobject]@{ answered = $false; value = @()
                error ="no answer in $([Math]::Round($waitSec, 2)) s$(if ($endCut -and -not $EndIsBoundEnd) { " (the call's end cut the $([Math]::Round($TimeoutSec, 2)) s bound short)" })" }
        }
        # r12 fix iteration (adversary A2 on 09421b77): a caller descheduled past its fixed end finds the worker done and WaitOne(0) true; an answer the caller
        # sees only after -EndSec is never acted on (the worker finished, so it is disposed as usual)
        if ($null -ne $Clock -and ($late = $Clock.Elapsed.TotalSeconds - $EndSec) -gt 0) {
            return [pscustomobject]@{ answered = $false; value = @(); error = "an answer seen $([Math]::Round($late, 2)) s after the call's end is never acted on" }
        }
        [pscustomobject]@{ answered = $true; value = @($ps.EndInvoke($async)); error = $null }
    } catch {
        [pscustomobject]@{ answered = $false; value = @(); error = $_.Exception.Message }
    } finally { if (-not $abandoned) { $ps.Dispose() } }
}

function Read-VenueFileBytesBounded {
    <#
    .SYNOPSIS
    VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-R3-ADV-RESIDUE-1 r2 (adversary-7efea617.json A4): one file's bytes, read on Invoke-VenueBoundedRead's worker and given up on after -TimeoutSec, so a
    path that stopped answering (a dead share, a stalled disk) costs the caller its bound and never an unbounded wait. Returns the byte array; $null when -MissingIsNull and the file
    does not exist. A read that fails or does not answer in time THROWS a message that names the leaf and the reason, so the caller's own catch types it.
    #>
    # pass 2 (adversary-b3d61554.json A1): -Clock / -EndSec are the CALLER's one budget across every read it makes (Get-VenueLegHostLoad starts one Stopwatch and gives both reads the
    # same end), so two reads never each get a fresh bound, the wait is cut to what is left of it, and an answer that arrives after the end is never acted on (Invoke-VenueBoundedRead's
    # own late-answer check). Without them -TimeoutSec alone bounds this one read.
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R4-NARROW-1 r2 (adversary-ac722a96.json A4): -EndUtc is the caller's ABSOLUTE end (Get-VenueLegHostLoad's -CallDeadlineUtc). The worker's own end is the EARLIER of that and
    # the one derived from the stopwatch budget (an instant computed from a remainder on a wall clock that was stepped since the caller sampled it would otherwise land past the caller's end), and a
    # read whose absolute end has already passed on the wall clock starts nothing.
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][double]$TimeoutSec, [switch]$MissingIsNull, [Diagnostics.Stopwatch]$Clock = $null, [double]$EndSec = 0, [DateTime]$EndUtc)
    # (-VenueTestFileReadDelayMs is a test-only seam: the worker sleeps that long first, as a share that answers late does; production leaves it 0)
    # pass 2 (A11): $ErrorActionPreference Stop, so a failed read is a terminating error the bounded read reports (its message names the cause), never an empty answer read as "no file"
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R4-NARROW-1 r2 (adversary-6596e055.json A3): the worker is handed the ABSOLUTE UTC end of this read (the wait it is given: -TimeoutSec, cut to what is left of the caller's
    # -Clock/-EndSec) and refuses to touch the file once it has passed ($script:VenueFileReadWorkerText checks it before the probe and before ReadAllBytes)
    $boundSec = [Math]::Min(600.0, [Math]::Max(1.0, $TimeoutSec))
    # R2-NARROW-1 r2 (adversary-7c5bfe86.json A4, and adversary-baf2e69a.json A1): the monotonic end (the caller's stopwatch, or one started now) handed to the worker beside the UTC tick end is derived from
    # ONE sample of that clock taken here ($entrySec): a remainder measured at one sample and added to a later clock read lands past the real end by however long the caller was descheduled between them
    $workClock = $(if ($null -ne $Clock) { $Clock } else { [Diagnostics.Stopwatch]::StartNew() })
    $entrySec = $workClock.Elapsed.TotalSeconds
    if ($script:VenueTestReadSampleDelayMs -gt 0) { [Threading.Thread]::Sleep($script:VenueTestReadSampleDelayMs) }
    $waitSec = $(if ($null -ne $Clock) { [Math]::Min([double]$boundSec, $EndSec - $entrySec) } else { $boundSec })
    $workEndSec = $entrySec + [Math]::Max(0.0, $waitSec)
    $endTicks = [DateTime]::UtcNow.AddSeconds([Math]::Max(0.0, $waitSec)).Ticks
    if ($PSBoundParameters.ContainsKey('EndUtc')) {
        $absoluteEnd = $EndUtc.ToUniversalTime()
        if ($absoluteEnd.AddSeconds(-$script:VenueTestUtcStepSec) -le [DateTime]::UtcNow) { throw "the read of '$([IO.Path]::GetFileName($Path))' did not finish: the caller's absolute end had already passed; nothing was started" }
        if ($absoluteEnd.Ticks -lt $endTicks) { $endTicks = $absoluteEnd.Ticks }
    }
    $workerArg = @{ path = $Path; missingOk = [bool]$MissingIsNull; delayMs = $script:VenueTestFileReadDelayMs; endUtcTicks = $endTicks; clock = $workClock; clockEndSec = $workEndSec }
    if ($script:VenueTestCaptureWorkerArg) { $script:VenueTestLastFileWorkerArg = $workerArg }
    $r = $(if ($null -ne $Clock) { Invoke-VenueBoundedRead -ScriptText $script:VenueFileReadWorkerText -Argument $workerArg -TimeoutSec $boundSec -Clock $Clock -EndSec $EndSec -EndIsBoundEnd }
           else { Invoke-VenueBoundedRead -ScriptText $script:VenueFileReadWorkerText -Argument $workerArg -TimeoutSec $boundSec })
    if (-not $r.answered) { throw "the read of '$([IO.Path]::GetFileName($Path))' did not finish: $($r.error)" }
    # adversary-d10f5241.json A2: an answer is judged against the ABSOLUTE end when it is OBSERVED too, not only before the read starts: the late-answer check inside Invoke-VenueBoundedRead reads the
    # stopwatch alone, so a wall clock stepped (or a caller suspended) inside the read let bytes seen after the caller's UTC deadline be acted on and a leg be counted
    if ($PSBoundParameters.ContainsKey('EndUtc')) {
        if ($script:VenueTestPostReadDelayMs -gt 0) { [Threading.Thread]::Sleep($script:VenueTestPostReadDelayMs) }
        if ($EndUtc.ToUniversalTime().AddSeconds(-$script:VenueTestUtcStepSec) -le [DateTime]::UtcNow) { throw "the read of '$([IO.Path]::GetFileName($Path))' did not finish: an answer seen after the caller's absolute end is never acted on" }
    }
    $got = @($r.value)
    if ($got.Count -eq 0) {
        if ($MissingIsNull) { return $null }
        throw "the read of '$([IO.Path]::GetFileName($Path))' returned nothing"
    }
    return ,([byte[]]$got[0])
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
    try { return [pscustomobject]@{ state = 'FOUND'; result = (ConvertFrom-VenueJsonRawIdentity -Text $text); error = $null } }
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

# r6 (LEASE-BATCH-SIDECAR-READBACK-1, Grok G2 on #633): WRITTEN only says the write call returned. The sidecar is read back with the AGENT's own reader and verdict
# (Read-JobNotAfterSidecar + Get-JobNotAfterVerdict, lifted by AST from ultra-magnus-agent.ps1, never a second parser), so a file the agent would read as absent,
# unreadable, not an object, the wrong schema or job, without a parseable aliveUntilUtc, or already lapsed is caught before the open is submitted.
$script:LivenessReadbackText = @'
param($A)
foreach ($t in $A.functions) { . ([scriptblock]::Create($t)) }
$notAfterDir = $A.dir
$v = Get-JobNotAfterVerdict -Metadata ([pscustomobject]@{ notAfterUtc = $A.notAfterUtc }) -Sidecar (Read-JobNotAfterSidecar -JobId $A.jobId)
[ordered]@{ state = $v.state; livenessState = $v.livenessState; livenessUntilUtc = $v.livenessUntilUtc; why = $v.why } | ConvertTo-Json -Compress
'@

function Test-VenueJobLivenessReadback {
    <#
    .SYNOPSIS
    Read the job's sidecar back from the agent share on a bounded worker and judge it with the agent's own functions. Returns @{ live; state; livenessState; livenessUntilUtc; why; error; agentScriptSha256 }.
    live is true only when the agent's verdict calls the sidecar live and the job still open (or unbounded); anything else, including a worker that does not answer or an
    agent script whose functions cannot be lifted, is not live (fail closed). A live sidecar with state 'passed' means the job's own notAfterUtc has passed (the deadline
    crossed): the caller types that DEADLINE, not a liveness failure.
    #>
    param([Parameter(Mandatory)][string]$AgentShare, [Parameter(Mandatory)][string]$JobId, [string]$NotAfterUtc = '', [string]$AgentScript = '', [ValidateRange(1, 600)][int]$TimeoutSec = 30)
    # r2: agentScriptSha256 is the hash of the checkout's agent script the parser was lifted from, so a later drift between the checkout and the deployed agent shows in the receipt.
    $sha = $null
    $no = { param($why) [pscustomobject]@{ live = $false; state = 'UNKNOWN'; livenessState = 'UNKNOWN'; livenessUntilUtc = $null; why = $why; error = $why; agentScriptSha256 = $sha } }
    # r3 (Dot B1 on 46768a21): a hash or read that throws after Test-Path succeeded (the script locked or vanished) is a not-live answer like any other. It never
    # reaches the runner as an exception, which the runner's cleanup would read as a sent open.
    try {
        if (-not $AgentScript) { $AgentScript = Join-Path $PSScriptRoot '..\bachelor\ultra-magnus-agent.ps1' }
        if (-not (Test-Path -LiteralPath $AgentScript -PathType Leaf)) { return & $no "the agent script is not at $AgentScript" }
        $sha = (Get-FileHash -LiteralPath $AgentScript -Algorithm SHA256).Hash.ToLowerInvariant()
        $ast = [System.Management.Automation.Language.Parser]::ParseFile($AgentScript, [ref]$null, [ref]$null)
        $fns = @(foreach ($n in 'Get-JobNotAfterVerdict', 'Read-JobNotAfterSidecar') {
            $f = $ast.Find({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -eq $n }, $true)
            if ($null -ne $f) { $f.Extent.Text }
        })
        if ($fns.Count -ne 2) { return & $no 'the agent script does not define both Get-JobNotAfterVerdict and Read-JobNotAfterSidecar' }
        $arg = [pscustomobject]@{ functions = $fns; dir = ($AgentShare.TrimEnd('\') + '\notafter'); jobId = $JobId; notAfterUtc = $NotAfterUtc }
        $r = Invoke-VenueBoundedRead -ScriptText $script:LivenessReadbackText -Argument $arg -TimeoutSec $TimeoutSec
        if (-not $r.answered) { return & $no "read-back: $($r.error)" }
        $v = try { [string](@($r.value) -join '') | ConvertFrom-Json -ErrorAction Stop } catch { $null }
        if ($null -eq $v -or $null -eq $v.livenessState) { return & $no "read-back gave no verdict: $([string](@($r.value) -join ''))" }
        [pscustomobject]@{ live = ([string]$v.livenessState -ceq 'live' -and [string]$v.state -cin @('open', 'none')); state = [string]$v.state; livenessState = [string]$v.livenessState; livenessUntilUtc = $v.livenessUntilUtc; why = [string]$v.why; error = $null; agentScriptSha256 = $sha }
    } catch { return & $no "read-back threw: $($_.Exception.Message -replace '\s+', ' ')" }
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

function Read-VenueJsonObjectLine([string]$Line) {
    # LEASE-BATCH-IDLE-END-UNSEEN-1 r7 (adversary-065db820.json A2): one JSONL line as ONE JSON object, or $null. A line that is an array ([null,{..}]) reached the readers below
    # as a collection, and member enumeration answered .token/.leaseId/.endReason from the object inside it; ConvertFrom-Json also unrolls [{..}] and [[{..}]] to that object,
    # so the TEXT must open with '{' and the parse must be one PSCustomObject.
    if ([string]::IsNullOrWhiteSpace($Line) -or -not $Line.TrimStart().StartsWith('{')) { return $null }
    $o = try { ConvertFrom-VenueJsonRawIdentity -Text $Line } catch { $null }
    if ($o -is [System.Management.Automation.PSCustomObject]) { return $o }
    $null
}

function Read-VenueLeaseBatchReceipt {
    <#
    .SYNOPSIS
    The venue agent's own receipt for one batch lease (ultra-magnus-agent.ps1 lease-batch item 5): the line of <AgentShare>\logs\lease-batch-<Token>.jsonl whose token is
    -Token AND whose leaseId is -LeaseId (both scalar strings, ordinal), read by NAME on one bounded worker. A line with a missing, non-string or other token is never this batch's.
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
        $rec = Read-VenueJsonObjectLine $line
        # r12 (adversary r11 A2): ordinal on the exact lease id (-ceq is culture-aware); an empty or whitespace-only -LeaseId names no lease, so it matches no line
        # r2 (sol key r1 BLOCKER, PR 817): the line must also be THIS token's, a scalar string compared ordinally, so a foreign or token-less line neither answers nor hides the agent's own
        if ($null -ne $rec -and -not [string]::IsNullOrWhiteSpace($LeaseId) -and $rec.token -is [string] -and [string]::Equals($rec.token, $Token, [StringComparison]::Ordinal) -and
            $rec.leaseId -is [string] -and [string]::Equals($rec.leaseId, $LeaseId, [StringComparison]::Ordinal)) { return [pscustomobject]@{ state = 'FOUND'; record = $rec; error = $null } }
    }
    [pscustomobject]@{ state = 'ABSENT'; record = $null; error = "no line for lease $LeaseId among $($lines.Count)" }
}

function Get-VenueInterleavedLegPlan {
    <#
    .SYNOPSIS
    LEASE-BATCH-MULTI-BUILD-1: a lease-batch leg plan with its arms INTERLEAVED (A B A B ...) instead of run in blocks, so lease position and venue heat fall on
    every arm alike. Arms are taken in order of first appearance, one leg each per round, each arm's legs keeping their own order; an arm with more legs than the
    others runs its remainder at the end (A A A B -> A B A A). Every item is returned once; nothing is added or dropped.
    .OUTPUTS
    The -Items objects, reordered.
    #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Items)
    # An arm label is case-sensitive (the plan validates it with -cnotmatch, so A and a are two labels): an [ordered]@{} would key them case-insensitively and merge them.
    $queues = [System.Collections.Generic.Dictionary[string, System.Collections.Generic.Queue[object]]]::new([StringComparer]::Ordinal)
    $armOrder = [System.Collections.Generic.List[string]]::new()
    foreach ($it in $Items) {
        $arm = [string]$it.arm
        if (-not $queues.ContainsKey($arm)) { $queues[$arm] = [System.Collections.Generic.Queue[object]]::new(); $armOrder.Add($arm) }
        $queues[$arm].Enqueue($it)
    }
    $left = $Items.Count
    while ($left -gt 0) {
        # the comma keeps an array-valued item ONE output object (a bare Dequeue() result would be enumerated into its elements)
        foreach ($arm in $armOrder) { $q = $queues[$arm]; if ($q.Count -gt 0) { , $q.Dequeue(); $left-- } }
    }
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
        $evidence = Join-Path (Join-Path (Split-Path -Parent $root) 'evidence') ([IO.Path]::GetFileNameWithoutExtension($ReceiptPath))
        # A playback leg publishes <evidence>\<receiptId>\summary.json; an export leg publishes <evidence>\<receiptId>\export\summary.json (EXPORT-VENUE-LEG-1 r3: the
        # first venue proof read UNKNOWN because only the first was looked at). The first that exists is read.
        $summary = $null
        foreach ($candidate in @((Join-Path $evidence 'summary.json'), (Join-Path (Join-Path $evidence 'export') 'summary.json'))) {
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { $summary = $candidate; break }
        }
        if ($null -eq $summary) { return $null }
        $q = ([IO.File]::ReadAllText($summary) | ConvertFrom-Json).cpuQuiescence
        if ($null -eq $q -or $null -eq $q.timeMeanPercent) { return $null }
        [double]$q.timeMeanPercent
    } catch { $null }
}

function Test-VenueHlObject([object]$O) {
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1: an object or a dictionary to read members from; never a scalar, a string or a list (adversary-8c2884e9.json A1).
    if ($null -eq $O -or $O -is [string] -or $O -is [ValueType]) { return $false }
    if ($O -is [System.Collections.IDictionary]) { return $true }
    -not ($O -is [Array] -or $O -is [System.Collections.IList])
}

function Test-VenueOrdinalIn {
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-R3-ADV-RESIDUE-1 r1 (adversary-fd0141e8.json A2): membership by ORDINAL, case-sensitive comparison of printable ASCII only.
    # PowerShell's -ceq / -cin / -cnotin compare by the current culture, which ignores a soft hyphen (U+00AD) and other ignorable characters, so 'PA<U+00AD>SS' -cin
    # @('PASS') is true. A token or a member name with any character outside 0x20-0x7E is never in the set, whatever it would collate to.
    param([object]$Value, [string[]]$Set)
    if ($Value -isnot [string]) { return $false }
    foreach ($ch in $Value.ToCharArray()) { if ([int]$ch -lt 0x20 -or [int]$ch -gt 0x7E) { return $false } }
    foreach ($member in $Set) { if ([string]::Equals($Value, $member, [StringComparison]::Ordinal)) { return $true } }
    return $false
}

function Get-VenueHlMember([object]$O, [string]$N) {
    # The member's value as it is (the unary comma stops the pipeline unrolling a one-element array); a dictionary is read by key (adversary-b0c05520.json A8).
    if (-not (Test-VenueHlObject $O)) { return $null }
    if ($O -is [System.Collections.IDictionary]) { if ($O.Contains($N)) { return , $O[$N] }; return $null }
    $p = $O.PSObject.Properties[$N]
    if ($null -eq $p) { return $null }
    , $p.Value
}

function Test-VenueHlHasMember([object]$O, [string]$N) {
    # adversary-90151822.json A1: whether the member is there at all, so a member present with a null value is told apart from an absent one.
    if (-not (Test-VenueHlObject $O)) { return $false }
    if ($O -is [System.Collections.IDictionary]) { return [bool]$O.Contains($N) }
    $null -ne $O.PSObject.Properties[$N]
}

function ConvertFrom-VenueBomBytes([byte[]]$Bytes) {
    # adversary-ac6205bd.json A5: file bytes decoded by their byte-order mark (UTF-8, UTF-16 LE/BE, UTF-32), as Get-Content and ReadAllText do; no BOM reads
    # UTF-8. A forced UTF-8 decode turned a UTF-16 smoke result, receipt or spec into replacement characters and NULs (unknown, never its real standing).
    if ($null -eq $Bytes -or $Bytes.Length -eq 0) { return '' }
    $reader = [IO.StreamReader]::new([IO.MemoryStream]::new($Bytes), [Text.UTF8Encoding]::new($false), $true)
    try { $reader.ReadToEnd() } finally { $reader.Dispose() }
}

function Find-VenueJsonDuplicateMember {
    # adversary-b0c05520.json A2: ConvertFrom-Json keeps the LAST of two same-named members, so a duplicate could pick a clean value. The first duplicated member
    # name (case-insensitive, as PowerShell property lookup is) at the root or anywhere under a root hostLoad member, or $null when there is none.
    # adversary-90151822.json A2: -All walks the whole document (the receipt: a repeated export or evidence member must not pick the export branch or the hash).
    param([Parameter(Mandatory)][string]$Text, [switch]$All)
    # R2-NARROW-1 r2 (adversary-7c5bfe86.json A6, adversary-baf2e69a.json A4): JsonDocument.Parse defaults to a depth limit of 64, while the readers' own parsers accept far more (ConvertFrom-Json -Depth 100
    # for the figure readers, its default 1024 for the venue readers); a valid, duplicate-free document nested 65.. deep threw here and read unknown. The scan accepts the deepest the default reader does (1024);
    # a document a stricter figure parser refuses is judged unknown by its caller (compare-release-gui-smoke-ab.ps1 records the refusal), and one deeper than any parser accepts is refused by the parsers.
    $docOptions = [System.Text.Json.JsonDocumentOptions]::new()
    $docOptions.MaxDepth = 1024
    $doc = [System.Text.Json.JsonDocument]::Parse($Text, $docOptions)
    try {
        $walk = $null
        $walk = {
            param($E, [string]$Path, [bool]$Deep)
            if ($E.ValueKind -eq [System.Text.Json.JsonValueKind]::Object) {
                $seen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
                foreach ($m in $E.EnumerateObject()) {
                    if (-not $seen.Add($m.Name)) { return "$Path.$($m.Name)" }
                    if ($Deep -or [string]::Equals($m.Name, 'hostLoad', [StringComparison]::OrdinalIgnoreCase)) { $d = & $walk $m.Value "$Path.$($m.Name)" $true; if ($d) { return $d } }
                }
            } elseif ($Deep -and $E.ValueKind -eq [System.Text.Json.JsonValueKind]::Array) {
                $i = 0
                foreach ($x in $E.EnumerateArray()) { $d = & $walk $x "$Path[$i]" $true; if ($d) { return $d }; $i++ }
            }
            $null
        }
        & $walk $doc.RootElement '$' ([bool]$All)
    } finally { $doc.Dispose() }
}

function ConvertFrom-VenueSmokeHostLoad {
    <#
    .SYNOPSIS
    VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1: a leg's standing from its smoke result's hostLoad block (the release GUI smoke writes it: state, provisional, before/after
    snapshots with collected=). The canonical predicate of PLAYBACK-MEASURE-HOST-LOAD-GATE-1 (compare-release-gui-smoke-ab.ps1 Get-HostLoadComparisonEvidence):
    clean iff state is exactly 'quiet' AND provisional is the boolean false; every other combination is provisional. Added here: a before or after snapshot
    whose collected is not the boolean true (2026-11-21 leg a134532e: "collection failed: CimException") reads state 'unknown', never 'quiet' and never blank,
    and a missing block reads 'unknown'. Returns { state; provisional; collected; reason }. Pure: no file access.
    #>
    # r1 (adversary-8c2884e9.json A1): a property value is returned as it is (the comma stops the pipeline unrolling a one-element array, so [false] or [true]
    # is never read as the boolean inside it), and an array is never an object to read a property from. A2: the state is kept only when it is a plain ASCII
    # lower-case token (no padding, control or format character); anything else reads 'unknown', and only the exact ordinal 'quiet' is clean.
    # r1 (adversary-b0c05520.json A3): every interior (during) sample must be collected too; a during that is not a list, or an element that is not a collected
    # snapshot, is not collected. A8: a dictionary block (ConvertFrom-Json -AsHashtable) is read by its keys.
    param([AllowNull()][object]$HostLoad)
    if (-not (Test-VenueHlObject $HostLoad)) {
        return [pscustomobject]@{ state = 'unknown'; provisional = $true; collected = $false; reason = 'no hostLoad block in the smoke result' }
    }
    $errors = @()
    $collected = $true
    foreach ($side in 'before', 'after') {
        $snap = Get-VenueHlMember $HostLoad $side
        $c = Get-VenueHlMember $snap 'collected'
        if (-not ($c -is [bool] -and $c)) {
            $collected = $false
            $err = Get-VenueHlMember $snap 'error'
            $errors += "$side not collected$(if ($err) { " ($([string]$err))" })"
        }
    }
    # adversary-90151822.json A1: a during member that is present is judged even when its value is null (the smoke always writes a list, so null is malformed);
    # only an absent member (older smoke results) is skipped.
    if (Test-VenueHlHasMember $HostLoad 'during') {
        $during = Get-VenueHlMember $HostLoad 'during'
        if ($null -eq $during -or $during -is [string] -or -not ($during -is [System.Collections.IList])) { $collected = $false; $errors += 'during is not a list of snapshots' }
        else {
            $bad = 0
            foreach ($el in $during) { $c = Get-VenueHlMember $el 'collected'; if (-not ($c -is [bool] -and $c)) { $bad++ } }
            if ($bad -gt 0) { $collected = $false; $errors += "$bad during sample(s) not collected" }
        }
    }
    $stateRaw = Get-VenueHlMember $HostLoad 'state'
    $state = $(if ($stateRaw -is [string] -and [regex]::IsMatch($stateRaw, '\A[a-z][a-z_-]{0,31}\z')) { $stateRaw } else { 'unknown' })
    if (-not $collected) { $state = 'unknown' }
    $prov = Get-VenueHlMember $HostLoad 'provisional'
    $provisional = (-not ($prov -is [bool] -and -not $prov)) -or (-not [string]::Equals($state, 'quiet', [StringComparison]::Ordinal))
    $why = Get-VenueHlMember $HostLoad 'reason'
    $reason = @(@($errors) + @($(if ($why) { [string]$why }))) -join '; '
    [pscustomobject]@{ state = $state; provisional = [bool]$provisional; collected = [bool]$collected; reason = $reason }
}

function ConvertFrom-VenueSmokeResultText {
    <#
    .SYNOPSIS
    VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (adversary-de8e3799.json A1): a smoke result.json's host-load standing from its TEXT, read the way Get-VenueLegHostLoad
    reads it: a leading BOM is dropped; a repeated member at the root or under hostLoad reads 'unknown' (ConvertFrom-Json keeps the last one); the document is
    parsed with -NoEnumerate, so a root or hostLoad singleton array is never unrolled into a clean object; a root that is not one object reads 'unknown'.
    The block is then judged by ConvertFrom-VenueSmokeHostLoad. Pure.
    #>
    param([AllowNull()][AllowEmptyString()][string]$Text)
    $unknown = [pscustomobject]@{ state = 'unknown'; provisional = $true; collected = $false; reason = 'the smoke result could not be read' }
    try {
        if ([string]::IsNullOrEmpty($Text)) { return $unknown }
        if ($Text[0] -eq [char]0xFEFF) { $Text = $Text.Substring(1) }
        $dup = Find-VenueJsonDuplicateMember -Text $Text
        if ($dup) { $unknown.reason = "the smoke result repeats the member $dup"; return $unknown }
        $doc = ConvertFrom-Json -InputObject $Text -NoEnumerate
        if (-not (Test-VenueHlObject $doc)) { $unknown.reason = 'the smoke result is not one JSON object'; return $unknown }
        ConvertFrom-VenueSmokeHostLoad -HostLoad (Get-VenueHlMember $doc 'hostLoad')
    } catch {
        $unknown.reason = "the smoke result could not be read ($($_.Exception.Message -replace '\s+', ' '))"
        $unknown
    }
}

# VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2 (adversary-07b6a932.json A4): the typed receipt outcomes, the same list as DualVenueRunner.psm1
# $script:OutcomeEnum (this module does not import the runner); HostLoadReceiptOutcomeEnumParityTests pins the two lists equal.
$script:VenueReceiptOutcomeEnum = @('PASS', 'FAIL', 'VENUE_UNHEALTHY', 'VENUE_NOT_QUIESCENT', 'VENUE_HOST_MISMATCH', 'DEVICE_UNAVAILABLE', 'UNRESOLVED', 'RETRACTED', 'INVALID', 'VENUE_TOOLING', 'SCALE_NOT_HONOURED')

function Test-VenueReceiptIsExportLeg([object]$Rc) {
    # r3 (sol r2 B2): any object-valued export member exempted a playback receipt (export:{} on a playback receipt, or a leg SPEC passed as the receipt). An export
    # leg is now only a receipt shaped as the shipped writer writes one (Invoke-VenueLeg.ps1 `if ($isExport)`): the receipt schema (New-DvReceipt), scale and display
    # nulled (a playback receipt always carries both objects), and an export block that names its format and carries real export evidence: the export spec
    # binding (receiptSha256, 64 hex) or an export verdict (a non-empty string). Anything else is a playback receipt and must bind its smoke result.
    # r3 adversary-01a12498.json A1: scale:[] / display:[] (not objects) or absent members, any token-shaped format and any non-blank verdict passed. Now scale and
    # display must be PRESENT and exactly null, the format one the runner ships (DualVenueRunner.psm1 $script:ExportFormats), the spec binding a 64-hex receiptSha256,
    # the verdict null or an outcome token, and the receipt must bind NO playback evidence (evidence.resultJsonSha256 and logSha256 null or absent: the writer's
    # export branch never sets them). A receipt that binds a smoke result is a playback leg and its own bound smoke result decides.
    if (-not (Test-VenueHlObject $Rc)) { return $false }
    $schema = Get-VenueHlMember $Rc 'schema'
    if (-not ($schema -is [string] -and [string]::Equals($schema, 'mlv-app/dual-venue-receipt/v1', [StringComparison]::Ordinal))) { return $false }
    foreach ($n in 'scale', 'display') { if (-not (Test-VenueHlHasMember $Rc $n) -or $null -ne (Get-VenueHlMember $Rc $n)) { return $false } }
    $evd = Get-VenueHlMember $Rc 'evidence'
    if ($null -ne $evd -and -not (Test-VenueHlObject $evd)) { return $false }
    foreach ($n in 'resultJsonSha256', 'logSha256') { if ($null -ne (Get-VenueHlMember $evd $n)) { return $false } }
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (adversary-ac6205bd.json A6): NO playback evidence of any kind: a playback block (presentedFps) or a contact-frames
    # binding is a playback receipt's, never the export writer's, so a receipt carrying either is not an export leg whatever its export block says
    if ($null -ne (Get-VenueHlMember $Rc 'playback')) { return $false }
    if ($null -ne (Get-VenueHlMember $evd 'contactFramesJsonSha256')) { return $false }
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2 (adversary-07b6a932.json A4): the visual capture block and the smoke stdout/stderr bindings are playback
    # evidence too (Invoke-VenueLeg.ps1 sets receipt.visual only when Get-DvVisualCaptureRequest applies, never for an export leg, and the export branch
    # completes its receipt before the smoke streams are collected), so a receipt carrying any of them is not an export leg.
    if ($null -ne (Get-VenueHlMember $Rc 'visual')) { return $false }
    foreach ($n in 'smokeStderrSha256', 'smokeStdoutSha256') { if ($null -ne (Get-VenueHlMember $evd $n)) { return $false } }
    # adversary-c2bbe5f8.json A7: an ALLOW-list, not a deny-list. The export branch of Invoke-VenueLeg.ps1 sets only umRunJsonSha256, localEvidenceDir and
    # summaryJsonSha256 (and umRunOutcome at the run); every other evidence member (evidenceManifestSha256, artifactIndexPath, any name not listed, any case
    # variant) is a playback binding, so a receipt where one is non-null is not an export leg.
    if ($null -ne $evd) {
        $evdNames = $(if ($evd -is [System.Collections.IDictionary]) { @($evd.Keys | ForEach-Object { [string]$_ }) } else { @($evd.PSObject.Properties | ForEach-Object { $_.Name }) })
        # adversary-fd0141e8.json A2: the allow-list is matched ORDINALLY (a name with a soft hyphen is not 'summaryJsonSha256')
        foreach ($n in $evdNames) { if (-not (Test-VenueOrdinalIn $n @('summaryJsonSha256', 'umRunJsonSha256', 'localEvidenceDir', 'umRunOutcome')) -and $null -ne (Get-VenueHlMember $evd $n)) { return $false } }
    }
    $ex = Get-VenueHlMember $Rc 'export'
    if (-not (Test-VenueHlObject $ex)) { return $false }
    $fmt = Get-VenueHlMember $ex 'format'
    if (-not ($fmt -is [string] -and ([string]::Equals($fmt, 'prores', [StringComparison]::Ordinal) -or [string]::Equals($fmt, 'cdng-lossless', [StringComparison]::Ordinal)))) { return $false }
    $bind = Get-VenueHlMember $ex 'receiptSha256'
    if (-not ($bind -is [string] -and [regex]::IsMatch($bind, '\A[0-9a-f]{64}\z'))) { return $false }
    if (-not (Test-VenueHlHasMember $ex 'verdict')) { return $false }
    $verdict = Get-VenueHlMember $ex 'verdict'
    # adversary-ac6205bd.json A6: a non-null verdict is the receipt's own outcome (Invoke-VenueLeg.ps1 writes export.verdict = the export outcome), so an
    # invented token ('NONEXISTENT' beside outcome PASS) is not an export verdict
    # (Complete-Receipt may downgrade a PASS/FAIL outcome to INVALID or UNRESOLVED after the verdict is written, so that pair is the writer's too.)
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r2 (adversary-07b6a932.json A4): the outcome and a non-null verdict must be typed receipt outcomes (the
    # writer's enum, DualVenueRunner.psm1 $script:OutcomeEnum; Write-DvReceipt refuses anything else), never any upper-case token. A null verdict is the
    # writer's only before the export ran, and that receipt's outcome is never PASS or FAIL (the export branch sets the verdict before it completes a
    # PASS/FAIL), so a PASS or FAIL beside a null verdict is not the writer's.
    $outcome = Get-VenueHlMember $Rc 'outcome'
    # adversary-fd0141e8.json A2: every token comparison here is ORDINAL ASCII (Test-VenueOrdinalIn / String.Equals), never the culture-aware -cin / -ceq
    if (-not (Test-VenueOrdinalIn $outcome $script:VenueReceiptOutcomeEnum)) { return $false }
    if ($null -eq $verdict) { return -not (Test-VenueOrdinalIn $outcome @('PASS', 'FAIL')) }
    if (-not (Test-VenueOrdinalIn $verdict $script:VenueReceiptOutcomeEnum)) { return $false }
    [string]::Equals($verdict, $outcome, [StringComparison]::Ordinal) -or ((Test-VenueOrdinalIn $outcome @('INVALID', 'UNRESOLVED')) -and (Test-VenueOrdinalIn $verdict @('PASS', 'FAIL')))
}

function Get-VenuePlannedLegType {
    <#
    .SYNOPSIS
    VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (sol r5 B2): the PLANNED leg type of a leg, read from the leg spec the driver planned it from (never from its receipt):
    'export' only when the spec is one JSON object, repeats no member, and its legType is exactly the string 'export'; 'playback' for any other readable spec;
    'unknown' when the spec path is empty, unreadable or not one JSON object, or (with -ExpectSpecSha256) its bytes are not the planned bytes. Only 'export'
    grants Get-VenueLegHostLoad's export exemption, so an unreadable plan is never exempt.
    #>
    # adversary-fa329743.json A3/A4: -SpecText judges the spec text the caller ALREADY read for its plan (its legId, its budget), so no second read can see
    # other bytes and no extra (unbounded) file read happens; -SpecPath reads the file itself.
    param([AllowNull()][AllowEmptyString()][string]$SpecPath, [AllowNull()][AllowEmptyString()][string]$ExpectSpecSha256, [AllowNull()][string]$SpecText)
    try {
        if ($PSBoundParameters.ContainsKey('SpecText')) {
            if ([string]::IsNullOrEmpty($SpecText)) { return 'unknown' }
            $text = $SpecText
        } else {
        if ([string]::IsNullOrWhiteSpace($SpecPath) -or -not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) { return 'unknown' }
        $bytes = [IO.File]::ReadAllBytes($SpecPath)
        if (-not [string]::IsNullOrEmpty($ExpectSpecSha256)) {
            $got = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes))
            if (-not [string]::Equals($got, $ExpectSpecSha256, [StringComparison]::OrdinalIgnoreCase)) { return 'unknown' }
        }
        $text = ConvertFrom-VenueBomBytes $bytes
        }
        if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
        if (Find-VenueJsonDuplicateMember -Text $text -All) { return 'unknown' }
        $spec = ConvertFrom-Json -InputObject $text -NoEnumerate
        if (-not (Test-VenueHlObject $spec)) { return 'unknown' }
        $lt = Get-VenueHlMember $spec 'legType'
        if ($lt -is [string] -and [string]::Equals($lt, 'export', [StringComparison]::Ordinal)) { return 'export' }
        'playback'
    } catch { 'unknown' }
}

function Get-VenueLegHostLoad {
    <#
    .SYNOPSIS
    VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1: the leg's host-load standing, read from the run's own smoke result (<evidence>\<receiptId>\result.json, which Invoke-VenueLeg
    copies beside summary.json; the same evidence folder Get-VenueLegPreLoadMean reads), never from the receipt file. A playback leg (summary.json at the evidence
    root) with no result.json, an unreadable one or no hostLoad block is 'unknown' and provisional. An export leg (export\summary.json only) runs no GUI smoke:
    state 'not-applicable', provisional false, applies false, so its counting is unchanged. Returns ConvertFrom-VenueSmokeHostLoad's object plus applies.
    #>
    # r1 (adversary-b0c05520.json A1): the receipt decides, never the folder layout. A receipt whose export block is an object is an export leg; any other
    # receipt is a playback leg, and its result.json counts only when its bytes hash to the receipt's evidence.resultJsonSha256 (Invoke-VenueLeg.ps1 binds it):
    # no binding, a mismatch (bytes replaced after publication) or an unreadable receipt is 'unknown'. A2: a duplicated member is 'unknown'.
    # r3 adversary-01a12498.json A2: a caller that read the receipt's figures earlier passes -ExpectResultJsonSha256 (the evidence.resultJsonSha256 of the receipt
    # it took the figures from; '' when that receipt bound none). The receipt read here must bind the same value, so a receipt (and its result.json) replaced
    # between the two reads is 'unknown', never the other receipt's quiet standing. A receiptId that is not the file name is 'unknown' too.
    # r6 (sol r5 B2): the export exemption is granted ONLY from the PLANNED leg type (-PlannedLegType, from the leg spec the driver planned: Get-VenuePlannedLegType),
    # never inferred from receipt fields. A receipt shaped like an export receipt in a leg planned as playback (or whose planned type is unreadable or not given)
    # is read as a playback leg: it must bind its smoke result, or it is 'unknown' and provisional. The receipt shape check still applies on top of the plan.
    # r6 (adversary-de8e3799.json A4): -ExpectReceiptSha256 is the sha256 of the receipt BYTES the caller took its figures from; the receipt read here must be
    # those same bytes (so a receipt replaced between the two reads, or a duplicate-bearing receipt the figure read accepted, is 'unknown').
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-R3-ADV-RESIDUE-1 r2 (adversary-7efea617.json A4): each of the two file reads (the receipt, result.json) is given up on after -ReadTimeoutSec
    # (Read-VenueFileBytesBounded, on Invoke-VenueBoundedRead's worker), so a deadline stop is bounded by what the caller says is left. A caller near a deadline passes what remains;
    # under 1 s the read is not started and the standing is 'unknown' (uncounted, the safe side). A read not answered in time is 'unknown' with that reason, never a hang.
    param([string]$ReceiptPath, [AllowNull()][AllowEmptyString()][string]$ExpectResultJsonSha256, [AllowNull()][AllowEmptyString()][string]$PlannedLegType,
        [AllowNull()][AllowEmptyString()][string]$ExpectReceiptSha256, [double]$ReadTimeoutSec = 20.0, [DateTime]$CallDeadlineUtc)
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R4-NARROW-1 r2 (adversary-6596e055.json A4): -CallDeadlineUtc is the caller's ABSOLUTE end (the chain's and the batch's -DeadlineUtc). The caller used to sample "what is left"
    # as a number and hand that over, so a caller paused between the sample and this call's own stopwatch read after its deadline. The end is judged HERE, at this call's start, on the wall clock: the
    # shared budget of the two reads is min(-ReadTimeoutSec, the seconds to that end), and under 1 s to it nothing is started (unknown, provisional, uncounted: the safe side). Absent: -ReadTimeoutSec alone.
    $unknown = [pscustomobject]@{ state = 'unknown'; provisional = $true; collected = $false; reason = 'no smoke result.json for the leg'; applies = $true }
    try {
        if (-not $ReceiptPath) { return $unknown }
        if (-not ($ReadTimeoutSec -ge 1.0)) { $unknown.reason = 'no time was left to read the leg''s receipt and smoke result'; return $unknown }
        # pass 2 (adversary-b3d61554.json A1): ONE budget for both reads (this Stopwatch and the same end), never a fresh bound per read, and an answer past the end is not acted on
        $readClock = [Diagnostics.Stopwatch]::StartNew()
        if ($script:VenueTestPreDeadlineSampleDelayMs -gt 0) { [Threading.Thread]::Sleep($script:VenueTestPreDeadlineSampleDelayMs) }
        # $budgetSec is an ENDPOINT on $readClock (both reads pass it as -EndSec), never a duration: the earlier of -ReadTimeoutSec (measured from the stopwatch's start) and the stopwatch reading at
        # which the absolute deadline falls
        $budgetSec = $ReadTimeoutSec
        if ($PSBoundParameters.ContainsKey('CallDeadlineUtc')) {
            # R2-NARROW-1 r7 (adversary-aab90f89.json A1, introduced): the stopwatch is sampled BEFORE the wall clock, so the endpoint is that reading plus the seconds still left to the deadline. It used to
            # take the seconds left as the endpoint itself, which charged the time already on the stopwatch twice: a caller descheduled 7 s after the stopwatch started, with 12 s to its deadline,
            # got endpoint 5 on a stopwatch at 7 and refused a readable quiet standing with 5 s still left. Sampling the stopwatch first can only land the endpoint at or before the real end (the safe side).
            $sampleSec = $readClock.Elapsed.TotalSeconds
            $toDeadline = ($CallDeadlineUtc.ToUniversalTime() - [DateTime]::UtcNow).TotalSeconds
            if (-not ($toDeadline -ge 1.0)) { $unknown.reason = 'no time was left before the caller''s deadline to read the leg''s receipt and smoke result'; return $unknown }
            if (($sampleSec + $toDeadline) -lt $budgetSec) { $budgetSec = $sampleSec + $toDeadline }
        }
        $endArgs = @{}
        if ($PSBoundParameters.ContainsKey('CallDeadlineUtc')) { $endArgs['EndUtc'] = $CallDeadlineUtc.ToUniversalTime() }
        # R2-RESIDUE-1 r1 (adversary-3be19c39.json A2): the read's own end check runs before its bytes are handed back, so a caller suspended after it reached the classification (and the export exemption) past
        # the absolute end and returned a quiet or exempt answer the caller then counted. $pastEnd is asked again right before either of those two answers is returned.
        $absEnd = $(if ($PSBoundParameters.ContainsKey('CallDeadlineUtc')) { $CallDeadlineUtc.ToUniversalTime() } else { $null })
        # R2-RESIDUE-1 r2 (adversary-f1221390.json A2): the end is the absolute deadline when one was given AND the shared read budget ($budgetSec on $readClock) always: a call with no -CallDeadlineUtc (the
        # baseline, the leg's own read) that was suspended after its last read past -ReadTimeoutSec classified and returned quiet or exempt just the same
        $pastEnd = { ($null -ne $absEnd -and $absEnd.AddSeconds(-$script:VenueTestUtcStepSec) -le [DateTime]::UtcNow) -or $readClock.Elapsed.TotalSeconds -gt $budgetSec }
        $lateReason = 'an answer classified after the caller''s absolute end or its read budget is never acted on'
        $rcBytes = Read-VenueFileBytesBounded -Path $ReceiptPath -TimeoutSec $budgetSec -Clock $readClock -EndSec $budgetSec @endArgs
        if ($script:VenueTestPostReceiptBytesDelayMs -gt 0) { [Threading.Thread]::Sleep($script:VenueTestPostReceiptBytesDelayMs) }
        if ($PSBoundParameters.ContainsKey('ExpectReceiptSha256')) {
            $rcGot = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($rcBytes))
            if ([string]::IsNullOrEmpty($ExpectReceiptSha256) -or -not [string]::Equals($rcGot, $ExpectReceiptSha256, [StringComparison]::OrdinalIgnoreCase)) { $unknown.reason = 'the receipt is not the bytes the figures were read from'; return $unknown }
        }
        $rcText = ConvertFrom-VenueBomBytes $rcBytes
        if ($rcText.Length -gt 0 -and $rcText[0] -eq [char]0xFEFF) { $rcText = $rcText.Substring(1) }
        # adversary-90151822.json A2: a receipt that repeats a member anywhere is 'unknown' before its export or evidence member is read (the last one would win)
        $rcDup = Find-VenueJsonDuplicateMember -Text $rcText -All
        if ($rcDup) { $unknown.reason = "the receipt repeats the member $rcDup"; return $unknown }
        $rc = ConvertFrom-Json -InputObject $rcText -NoEnumerate
        if (-not (Test-VenueHlObject $rc)) { $unknown.reason = 'the receipt is not a JSON object'; return $unknown }
        if (Test-VenueHlHasMember $rc 'receiptId') {
            $rid = Get-VenueHlMember $rc 'receiptId'
            if (-not ($rid -is [string] -and [string]::Equals($rid, [IO.Path]::GetFileNameWithoutExtension($ReceiptPath), [StringComparison]::Ordinal))) { $unknown.reason = 'the receipt''s receiptId is not its file name'; return $unknown }
        }
        if ($PSBoundParameters.ContainsKey('ExpectResultJsonSha256')) {
            $boundNow = Get-VenueHlMember (Get-VenueHlMember $rc 'evidence') 'resultJsonSha256'
            $boundText = $(if ($null -eq $boundNow) { '' } elseif ($boundNow -is [string]) { $boundNow } else { $null })
            $expectText = $(if ($null -eq $ExpectResultJsonSha256) { '' } else { $ExpectResultJsonSha256 })
            if ($null -eq $boundText -or -not [string]::Equals($boundText, $expectText, [StringComparison]::OrdinalIgnoreCase)) { $unknown.reason = 'the receipt is not the one the figures were read from (evidence.resultJsonSha256 changed between reads)'; return $unknown }
        }
        $plannedExport = ($PlannedLegType -is [string]) -and [string]::Equals($PlannedLegType, 'export', [StringComparison]::Ordinal)
        if ($plannedExport -and (Test-VenueReceiptIsExportLeg $rc)) {
            if (& $pastEnd) { $unknown.reason = $lateReason; return $unknown }
            return [pscustomobject]@{ state = 'not-applicable'; provisional = $false; collected = $false; reason = 'export leg: no GUI smoke'; applies = $false } }
        $want = Get-VenueHlMember (Get-VenueHlMember $rc 'evidence') 'resultJsonSha256'
        if (-not ($want -is [string] -and [regex]::IsMatch($want, '\A[0-9a-fA-F]{64}\z'))) { $unknown.reason = 'the receipt binds no evidence.resultJsonSha256'; return $unknown }
        $root = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $ReceiptPath)))
        $evidence = Join-Path (Join-Path (Split-Path -Parent $root) 'evidence') ([IO.Path]::GetFileNameWithoutExtension($ReceiptPath))
        $result = Join-Path $evidence 'result.json'
        $bytes = Read-VenueFileBytesBounded -Path $result -TimeoutSec $budgetSec -MissingIsNull -Clock $readClock -EndSec $budgetSec @endArgs
        if ($script:VenueTestPostBytesDelayMs -gt 0) { [Threading.Thread]::Sleep($script:VenueTestPostBytesDelayMs) }
        if ($null -eq $bytes) { return $unknown }
        $got =[Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes))
        if (-not [string]::Equals($got, $want, [StringComparison]::OrdinalIgnoreCase)) { $unknown.reason = "result.json does not match the receipt's evidence.resultJsonSha256"; return $unknown }
        $text = ConvertFrom-VenueBomBytes $bytes
        if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
        $dup = Find-VenueJsonDuplicateMember -Text $text
        if ($dup) { $unknown.reason = "result.json repeats the member $dup"; return $unknown }
        # r1 (adversary-8c2884e9.json A1): -NoEnumerate and no $( ) around the value, so a one-element array (the root or the hostLoad block) is never unrolled
        $doc = ConvertFrom-Json -InputObject $text -NoEnumerate
        $hl = $null
        if (Test-VenueHlObject $doc) { $hl = Get-VenueHlMember $doc 'hostLoad' }
        $s = ConvertFrom-VenueSmokeHostLoad -HostLoad $hl
        if (& $pastEnd) { $unknown.reason = $lateReason; return $unknown }
        $s | Add-Member -NotePropertyName applies -NotePropertyValue $true
        $s
    } catch {
        $unknown.reason = "the smoke result.json could not be read ($($_.Exception.Message -replace '\s+', ' '))"
        $unknown
    }
}

function Limit-VenueHostLoadToDeadline {
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R2-NARROW-1 r7 (sol r7 A3): the ONE guard every caller of Get-VenueLegHostLoad applies where it CONSUMES the standing (the chain, the lease batch and
    # the baseline; it was an inline copy in the first two). A standing a caller sees after its own absolute end is never acted on, whatever the reader returned: the caller can be
    # suspended after the reader's last check (a standing that passed at 899.9 s of a 900 s cap and is consumed at 901 s must not become a counted row). Past the end it reads
    # unknown, provisional and uncollected; before it, the standing comes back unchanged.
    param([AllowNull()][object]$Standing, [Parameter(Mandatory)][DateTime]$DeadlineUtc)
    if ($DeadlineUtc -le [DateTime]::UtcNow) { return [pscustomobject]@{ state = 'unknown'; provisional = $true; collected = $false; reason = 'the standing was seen after the absolute end (-DeadlineUtc) and is never acted on'; applies = $true } }
    $Standing
}

function Get-VenueReferenceHostLoadRefusal {
    # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1: the typed line for a leg that must not be counted as a control/reference arm, or $null when it may be.
    # REFERENCE_PROVISIONAL leg=<id> reason=hostload-<state> (hostload-provisional when the state reads quiet but the leg is provisional).
    # r1 (adversary-8c2884e9.json A3): nothing is coerced. Only applies that is the boolean false (Get-VenueLegHostLoad's export leg) skips the rule; a leg is
    # clean only when provisional is the boolean false, collected the boolean true and state the exact ordinal 'quiet' (A2), so a standing that says
    # provisional=false beside an unknown state or an uncollected snapshot is still refused.
    param([AllowEmptyString()][AllowNull()][string]$LegId, [AllowNull()][object]$Standing)
    $applies = Get-VenueHlMember $Standing 'applies'
    if ($applies -is [bool] -and -not $applies) { return $null }
    $prov = Get-VenueHlMember $Standing 'provisional'; $coll = Get-VenueHlMember $Standing 'collected'; $st = Get-VenueHlMember $Standing 'state'
    $state = $(if ($st -is [string] -and [regex]::IsMatch($st, '\A[a-z][a-z_-]{0,31}\z')) { $st } else { 'unknown' })
    $isQuiet = [string]::Equals($state, 'quiet', [StringComparison]::Ordinal)
    if ($prov -is [bool] -and -not $prov -and $coll -is [bool] -and $coll -and $isQuiet) { return $null }
    $why = $(if ($isQuiet) { 'provisional' } else { $state })
    $id = $(if ([string]::IsNullOrWhiteSpace($LegId)) { 'UNKNOWN' } else { $LegId -replace '\s', '_' })
    "REFERENCE_PROVISIONAL leg=$id reason=hostload-$why"
}

function Get-VenueReferenceArmVerdict {
    <#
    .SYNOPSIS
    VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1: the control/reference arm of a comparison, legs given as { legId; hostLoad } (hostLoad = the smoke result's raw hostLoad
    block). A leg whose host load is provisional or unknown is never COUNTED as the arm: it is listed with its typed REFERENCE_PROVISIONAL line, and the verdict
    is INCONCLUSIVE (never a pass, never a regression). Every leg stays in legs, a null one included (A4: it is an unknown leg). COUNTED only when every leg is
    clean and there is at least one. r1 (adversary-8c2884e9.json A3): the block is always judged by ConvertFrom-VenueSmokeHostLoad; a caller-supplied
    standing (applies / collected fields) is never trusted. Pure.
    #>
    param([AllowNull()][AllowEmptyCollection()][object[]]$Legs)
    $all = [System.Collections.Generic.List[object]]::new(); $counted = [System.Collections.Generic.List[object]]::new(); $refusals = [System.Collections.Generic.List[string]]::new()
    foreach ($leg in @($Legs)) {
        # r1 (adversary-b0c05520.json A8): a hashtable leg is read by its keys, as an object leg by its properties
        $lidV = Get-VenueHlMember $leg 'legId'
        $lid = $(if ($lidV -is [string]) { $lidV } else { '' })
        $raw = Get-VenueHlMember $leg 'hostLoad'
        $s = ConvertFrom-VenueSmokeHostLoad -HostLoad $raw
        $line = Get-VenueReferenceHostLoadRefusal -LegId $lid -Standing $s
        $row = [pscustomobject]@{ legId = $lid; hostLoadState = [string]$s.state; hostLoadProvisional = [bool]$s.provisional; counted = ($null -eq $line) }
        $all.Add($row)
        if ($null -eq $line) { $counted.Add($row) } else { $refusals.Add($line) }
    }
    [pscustomobject]@{
        verdict = $(if ($refusals.Count -eq 0 -and $all.Count -gt 0) { 'COUNTED' } else { 'INCONCLUSIVE' })
        refusals = [string[]]$refusals.ToArray(); counted = [object[]]$counted.ToArray(); legs = [object[]]$all.ToArray()
    }
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
            $r = Read-VenueJsonObjectLine $line
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

function ConvertTo-VenueStartCapTime {
    <#
    .SYNOPSIS
    BENCH-START-CAP-FROM-LEG-COUNT-1 r2 (adversary A2/A3): THE one time parser of the start cap, shared by Get-VenueRunStartCap and Get-VenueStartCap.ps1. It is ConvertTo-VenueUtc
    (a DateTime, a DateTimeOffset or text), with one rule: a time WITHOUT a zone is UTC, on every host (a text with an offset keeps it). Text that is not a time is a typed refusal,
    START_CAP_BAD_TIME name=<which input>, never a silent $null that would skip a safety bound.
    #>
    param($Value, [string]$Name = 'time')
    $u = ConvertTo-VenueUtc $Value
    if ($null -eq $u) { throw "START_CAP_BAD_TIME name=$Name value='$("$Value" -replace '\s+', ' ')' (not a date and time; a time without a zone is read as UTC)" }
    $u
}

# BENCH-START-CAP-FROM-LEG-COUNT-1: the two measured constants behind a venue run's start cap. Evidence (Bachelor, PLAYBACK-PRESENT-TAIL-TIMING-1 r1b batch-01..03 and
# PLAYBACK-CUDA-FULLSCREEN-BLUR-UPLOAD-STALL-POST-502-1 r3 B01, legs of playSeconds 25): consecutive LEG RAN lines are 225-236 s apart (median ~232 s; r3 228 s per leg over
# an 8-leg batch), so a leg costs its playSeconds plus ~207 s of quiet probe, preload, launch and receipt: 210 s. A lease open or close costs ~35-40 s (batch-01 opens
# 35 s after BATCH_START and ends 5 s after the last leg).
$script:VenueLegOverheadSec = 210
$script:VenueBatchOpenCloseSec = 40

function Get-VenueRunStartCap {
    <#
    .SYNOPSIS
    BENCH-START-CAP-FROM-LEG-COUNT-1: the START CAP of a venue run (the instant after which the driver starts no further batch), sized from the PLANNED LEG COUNT and
    the per-leg duration instead of a hand-typed window (the 38 min / 29 min windows that cut BLUR r3 after one batch and PRESENT-TAIL #720 at 8 of 12 cells).
    cap = the projected end of the last planned leg + a margin (-MarginPct of the span, at least -MinMarginSec). The projection replays the lease rules the batch runner
    lives by with the pure Get-VenueLeaseBudgetWait: a lease is at most -CapSec, a leg starts only while the lease has its leg budget + -LeaseMarginSec of room, the next
    lease waits out the cooldown and the 3 h budget (so the budget waits are inside the projection, not a surprise at the cap).
    A leg costs -PlaySeconds + -LegOverheadSec (the leg spec's seconds plus the measured per-leg overhead, $script:VenueLegOverheadSec by default) + -LegCooldownSec (the runner's
    own -CooldownSec, slept inside the lease after every completed leg while work remains). The leg budget the start test uses is the larger of -LegBudgetSec and one leg's cost.
    A lease that cannot hold even one leg (a cap or budget room too small) is not forced to make progress: the function throws START_CAP_UNSIZABLE with the numbers, and so
    does arithmetic that leaves the DateTime range. A caller that passed -ExplicitCapUtc is not stopped by that: its cap is kept and the line says projection=UNSIZABLE.
    -LegPlaySeconds (r2, adversary A1): the PER-LEG play seconds in plan order (one per leg, each 1..36000, in double before any integer cast). With it each leg strides by ITS OWN
    seconds + overhead + cooldown, so a plan of long legs then short ones is not spread at its mean (the mean strode long legs too tightly and projected fewer batches than the
    runner's admission needs). Without it every leg is -PlaySeconds. The start test keeps the static budget of the LONGEST leg (>= the runner's, which grows to it), so the
    projected batch count is never below the runner's. A bad value or a count that is not -LegCount is START_CAP_BAD_LEG / START_CAP_USAGE.
    Times (-NowUtc, -HardBoundUtc, -ExplicitCapUtc) all go through ConvertTo-VenueStartCapTime: a zoneless time is UTC, an unparseable one is START_CAP_BAD_TIME (r2, A2/A3).
    -ExplicitCapUtc: the caller's own cap is kept exactly (source explicit); the computed value is reported beside it and never replaces it.
    -HardBoundUtc: an existing safety bound (the run's hard end, a lease notAfterUtc). The computed cap never extends past it: it is clamped and the typed line
    START_CAP_CLAMPED_TO_HARD_BOUND says so (projectedPastBound=1 when the last planned leg would START after the bound).
    Pure: -NowUtc and -Leases (earlier leases, @{ startUtc; endUtc }) are passed in.
    .OUTPUTS
    [pscustomobject]@{ capUtc; source (computed | clamped | explicit); computedCapUtc ($null when unsizable under an explicit cap); projectedEndUtc; spanSec; batches; legWallSec; waitSec; marginSec; clamped; projectedPastBound; lines (typed log lines); line (the START_CAP line) }
    #>
    param([Parameter(Mandatory)]$NowUtc, [Parameter(Mandatory)][ValidateRange(1, 10000)][int]$LegCount,
        [ValidateRange(1, 36000)][int]$PlaySeconds = 25, [ValidateRange(0, 36000)][int]$LegOverheadSec = $script:VenueLegOverheadSec,
        [ValidateRange(0, 3600)][int]$LegCooldownSec = 0, [ValidateRange(0, 36000)][int]$MaxPlaySeconds = 0,
        [ValidateRange(0, 3600)][int]$BatchOpenCloseSec = $script:VenueBatchOpenCloseSec,
        [object[]]$Leases = @(), [int]$CapSec = 2700, [int]$CooldownSec = 1800, [int]$WindowSec = 10800, [int]$BudgetSec = 5400,
        [ValidateRange(60, 2700)][int]$LegBudgetSec = 360, [ValidateRange(0, 900)][int]$LeaseMarginSec = 120,
        [ValidateRange(0, 500)][int]$MarginPct = 20, [ValidateRange(0, 86400)][int]$MinMarginSec = 600,
        $HardBoundUtc = $null, $ExplicitCapUtc = $null, [object[]]$LegPlaySeconds = @())
    $now = ConvertTo-VenueStartCapTime $NowUtc 'NowUtc'
    # whole seconds read as yyyy-MM-ddTHH:mm:ssZ; an instant with sub-second ticks (a caller's explicit cap) keeps them, so the line never moves it
    $fmt = { param($d) $u = $d.ToUniversalTime(); $u.ToString($(if (($u.Ticks % 10000000) -ne 0) { "yyyy-MM-dd'T'HH:mm:ss.FFFFFFF'Z'" } else { "yyyy-MM-dd'T'HH:mm:ss'Z'" }), [Globalization.CultureInfo]::InvariantCulture) }
    [long]$legWall = [long]$PlaySeconds + $LegOverheadSec + $LegCooldownSec
    # a bound or cap that is not a time is a typed refusal (A2): never a $null that silently skips the clamp, and refused even when a valid explicit cap would stand
    $hard = $(if ($null -ne $HardBoundUtc -and "$HardBoundUtc" -ne '') { ConvertTo-VenueStartCapTime $HardBoundUtc 'HardBoundUtc' } else { $null })
    $hasExplicit = ($null -ne $ExplicitCapUtc -and "$ExplicitCapUtc" -ne '')
    $explicit = $(if ($hasExplicit) { ConvertTo-VenueStartCapTime $ExplicitCapUtc 'ExplicitCapUtc' } else { $null })
    # the per-leg seconds (A1/A4): each is range-checked in [double] before it is rounded into a whole second the way the runner's [int] cast rounds it (r3, adversary pass 2 A3: the runner
    # reads 2450.1 as 2450, and a rounded-UP 2451 s refused a leg the runner holds in a 2700 s lease). A null list is no list (advisory A5: it passed the count check and left legWallSec 210).
    [long[]]$legSecs = @()
    if ($null -ne $LegPlaySeconds -and @($LegPlaySeconds).Count -gt 0) {
        if (@($LegPlaySeconds).Count -ne $LegCount) { throw "START_CAP_USAGE -LegPlaySeconds names $(@($LegPlaySeconds).Count) legs but -LegCount is $LegCount" }
        $legSecs = @(foreach ($x in $LegPlaySeconds) {
                $d = [double]::NaN
                try { $d = [double]$x } catch { $d = [double]::NaN }
                if (-not ($d -ge 1 -and $d -le 36000)) { throw "START_CAP_BAD_LEG a leg's play seconds '$x' is not a number from 1 to 36000" }
                [long][Math]::Round($d, [MidpointRounding]::ToEven)
            })
        [long]$legWall = [long][Math]::Ceiling(($legSecs | Measure-Object -Average).Average) + $LegOverheadSec + $LegCooldownSec
    }
    $t = $now; $batches = 0; [long]$waitTotal = 0; $span = [long]0; $margin = [long]0; $computed = $null; $lastLegStart = $now; $projErr = $null
    try {
        # each leg's own run time (its play seconds + the measured overhead), in plan order: the per-leg list when the caller has one (A1), else every leg is -PlaySeconds
        [long[]]$runs = $(if ($legSecs.Count -gt 0) { @($legSecs | ForEach-Object { [long]$_ + $LegOverheadSec }) } else { @(1..$LegCount | ForEach-Object { [long]$PlaySeconds + $LegOverheadSec }) })
        # a leg starts only with its leg budget + the lease margin still ahead of it. The runner's leg budget grows to the LONGEST leg seen (Invoke-VenueLeaseBatch.ps1, up to 2700 s),
        # so the start test uses the longest planned leg (a static budget, never below the runner's), not the mean
        [long]$longestRun = [Math]::Max(([long[]]$runs | Measure-Object -Maximum).Maximum, $(if ($MaxPlaySeconds -gt 0) { [long]$MaxPlaySeconds + $LegOverheadSec } else { 0 }))
        [long]$legBudget = [Math]::Min(2700, [Math]::Max([long]$LegBudgetSec, $longestRun))
        if ($longestRun -gt 2700) { throw "the longest planned leg needs $longestRun s (playSeconds $([long]$longestRun - $LegOverheadSec) + overhead $LegOverheadSec), more than the 2700 s a lease can hold" }
        # The projection is replayed under TWO leg-budget models and the LATER end wins (adversary r2 A1: a rolling-budget plan ends hours later when the leases are cut differently):
        #   static   - the budget is the longest planned leg from the first lease on (never below the runner's, so the batch count is never below the runner's admission count);
        #   adaptive - the runner's own rule: it starts at -LegBudgetSec and grows to a completed leg's seconds (min 2700), so early leases admit more legs and later ones fewer.
        # The adaptive replay takes a leg's seconds as its run time (play + overhead), the same timing the lease ledger rows use.
        $simulate = {
            param([bool]$Adaptive)
            $rows = [System.Collections.Generic.List[object]]::new(); foreach ($l in @($Leases)) { if ($null -ne $l) { $rows.Add($l) } }
            [long]$lb = $(if ($Adaptive) { [Math]::Min(2700, [long]$LegBudgetSec) } else { $legBudget })
            $tt = $now; $nb = 0; [long]$wt = 0; $lastStart = $now; [int]$next = 0
            while ($next -lt $LegCount) {
                $w = Get-VenueLeaseBudgetWait -Leases @($rows) -NowUtc $tt -CapSec $CapSec -CooldownSec $CooldownSec -WindowSec $WindowSec -BudgetSec $BudgetSec
                [long]$room = [Math]::Min([long]$CapSec, [long]$w.budgetRoomSec)
                [long]$need = $LeaseMarginSec + $lb + $BatchOpenCloseSec
                if ($room -lt $need) { throw "a lease holds at most roomSec=$room (capSec=$CapSec, budgetRoomSec=$($w.budgetRoomSec)) and one leg needs $need s (leaseMarginSec=$LeaseMarginSec + legBudgetSec=$lb + openCloseSec=$BatchOpenCloseSec): no leg fits a lease" }
                # leg k of a lease starts at open + OpenClose + the walls (run + the runner's sleep) of the legs before it, and is admitted while that start is within room - margin - leg budget
                [long]$lastStartOff = $BatchOpenCloseSec; [long]$off = $BatchOpenCloseSec; $first = $true; $expired = $false
                while ($next -lt $LegCount -and ($first -or $off -le ($room - $LeaseMarginSec - $lb))) {
                    # r3 (adversary A1, pass 1): a leg that cannot END inside the lease's room never completes there. As the first leg of a lease it can complete in no lease at all
                    # (the same room every time), so the plan is unsizable. As a later leg the runner still starts it (the start test only looks at the leg budget it knows so far),
                    # the lease expires under it (the lease is clamped, the child is bounded to the expiry: LEASE_EXPIRED, outcome INVALID, uncounted, NEVER re-run: Invoke-VenueLeaseBatch.ps1
                    # removes it from the pending list and ends the batch). So the leg is consumed, the lease ends at its room, the leg budget does not grow (the leg did not complete), and
                    # the next lease starts with the following leg (adversary pass 2 A2: a retry of the expired leg was an invented batch).
                    if (($off + $runs[$next]) -gt $room) {
                        if ($first) { throw "a lease holds at most roomSec=$room (capSec=$CapSec, budgetRoomSec=$($w.budgetRoomSec)) and the next leg needs $($BatchOpenCloseSec + $runs[$next]) s (openCloseSec=$BatchOpenCloseSec + the leg's run $($runs[$next]) s = playSeconds + overhead $LegOverheadSec): no leg fits a lease" }
                        $lastStartOff = $off; $expired = $true; $next++; break
                    }
                    $lastStartOff = $off; $off += $runs[$next] + $LegCooldownSec
                    if ($Adaptive) { $lb = [Math]::Min(2700, [Math]::Max($lb, $runs[$next])) }
                    $next++; $first = $false
                }
                $lastStart = $w.openUtc.AddSeconds($lastStartOff)
                # the lease ends after its last leg (the runner's own sleep follows a leg only while work remains, and the lease never outlasts its room); a leg cut by the expiry ends it at its room
                [long]$inLease = $(if ($expired) { $room } else { $lastStartOff + $runs[$next - 1] + $(if ($next -lt $LegCount) { $LegCooldownSec } else { 0 }) })
                # the runner's sleep after a leg is not cut short by the lease and it records the close it made after the sleep, so the ledger row (what the cooldown and the budget count)
                # and the time cursor both run to the end of the whole in-lease time, even past the lease's room
                $rows.Add([pscustomobject]@{ startUtc = $w.openUtc; endUtc = $w.openUtc.AddSeconds($inLease) })
                $wt += $w.waitSec; $nb++; $tt = $w.openUtc.AddSeconds($inLease)
            }
            [pscustomobject]@{ t = $tt; batches = $nb; wait = $wt; lastLegStart = $lastStart }
        }
        # r3 (adversary A1): the ADAPTIVE replay is the runner's own admission, so it alone decides whether the plan is sizable (its refusal is the one reported). The static replay is the
        # conservative second model: its budget is the longest leg from the first lease on, which the runner only reaches after that leg ran, so "no leg fits" there is dropped, not thrown,
        # and the adaptive end stands. When both succeed the later end still wins.
        $simAdaptive = & $simulate $true
        $simStatic = $null
        try { $simStatic = & $simulate $false } catch { $simStatic = $null }
        $pick = $(if ($null -ne $simStatic -and $simStatic.t -ge $simAdaptive.t) { $simStatic } else { $simAdaptive })
        $t = $pick.t; $batches = $(if ($null -ne $simStatic) { [Math]::Max($simStatic.batches, $simAdaptive.batches) } else { $simAdaptive.batches }); $waitTotal = $pick.wait; $lastLegStart = $pick.lastLegStart
        $span = [long][Math]::Ceiling(($t - $now).TotalSeconds)
        $margin = [Math]::Max([long]$MinMarginSec, [long][Math]::Ceiling($span * $MarginPct / 100.0))
        $computed = $t.AddSeconds($margin)
    } catch { $projErr = $_.Exception.Message }
    if ($null -ne $projErr -and -not $hasExplicit) { throw "START_CAP_UNSIZABLE legs=$LegCount legWallSec=$legWall $projErr" }
    $cap = $computed; $source = 'computed'; $clamped = $false; $lines = [System.Collections.Generic.List[string]]::new()
    $pastBound = $false
    if ($null -ne $computed -and $null -ne $hard -and $computed -gt $hard) {
        $cap = $hard; $source = 'clamped'; $clamped = $true; $pastBound = ($lastLegStart -gt $hard)
        $lines.Add("START_CAP_CLAMPED_TO_HARD_BOUND computedCapUtc=$(& $fmt $computed) hardBoundUtc=$(& $fmt $hard) projectedEndUtc=$(& $fmt $t) lastLegStartUtc=$(& $fmt $lastLegStart) projectedPastBound=$([int]$pastBound) (the existing bound wins; the cap is not extended past it)")
    }
    if ($hasExplicit) { $cap = $explicit; $source = 'explicit' }
    $proj = $(if ($null -ne $projErr) { " projection=UNSIZABLE reason=$($projErr -replace '\s+', ' ')" } else { " projectedEndUtc=$(& $fmt $t) spanSec=$span marginSec=$margin computedCapUtc=$(& $fmt $computed)" })
    $line = "START_CAP source=$source capUtc=$(& $fmt $cap) legs=$LegCount playSec=$PlaySeconds legOverheadSec=$LegOverheadSec legCooldownSec=$LegCooldownSec legWallSec=$legWall batches=$batches waitSec=$waitTotal$proj$(if ($null -ne $hard) { " hardBoundUtc=$(& $fmt $hard)" })"
    $lines.Insert(0, $line)
    [pscustomobject]@{ capUtc = $cap; capText = (& $fmt $cap); source = $source; computedCapUtc = $computed; projectedEndUtc = $t; spanSec = $span; batches = $batches; legWallSec = $legWall; waitSec = $waitTotal; marginSec = $margin
        clamped = $clamped; projectedPastBound = $pastBound; lines = @($lines); line = $line }
}

function Get-VenueLegPlaySeconds {
    <#
    .SYNOPSIS
    The playSeconds of a dual-venue leg spec (the leg spec's own seconds, the first input of the start cap), read the way the runner reads it: Invoke-VenueLeg and the plan
    preflight cast it with [int], so a JSON number or a numeric string both count. $null when the file is missing, not JSON, or has no positive value up to 36000.
    #>
    param([string]$SpecPath)
    try {
        if (-not $SpecPath -or -not (Test-Path -LiteralPath $SpecPath -PathType Leaf)) { return $null }
        # -NoEnumerate, as the runner's preflight reads a spec: a one-element array root stays an array and is not a leg spec
        try { $j = Get-Content -LiteralPath $SpecPath -Raw -ErrorAction Stop | ConvertFrom-Json -NoEnumerate -ErrorAction Stop } catch { return $null }
        if ($null -eq $j -or $j -isnot [System.Management.Automation.PSCustomObject] -or -not $j.PSObject.Properties['playSeconds']) { return $null }
        $v = $j.playSeconds
        if ($null -eq $v -or $v -is [bool]) { return $null }
        # r2 (adversary A4): the value is read as a [double] and range-checked BEFORE any integer cast, so 2147483647.1 (an Int32 cast that works, a Ceiling that does not) is
        # unreadable, not an exception. A number is that double; a string is parsed as a double (so "1,200" is 1200), and only text that is no decimal number ("0x4b0") falls back
        # to the runner's own [int] cast (Invoke-VenueLeg, the plan preflight), whose result is range-checked as well. A number is rounded to a whole second the way that cast rounds it (to nearest, ties to even; r3 A3).
        $d = [double]::NaN
        if ($v -is [double] -or $v -is [decimal] -or $v -is [int] -or $v -is [long] -or $v -is [single] -or $v -is [System.Numerics.BigInteger]) { $d = [double]$v }
        elseif ($v -is [string]) {
            $parsed = [double]::NaN
            if ([double]::TryParse($v, [Globalization.NumberStyles]'Float, AllowThousands', [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) { $d = $parsed }
            else { try { $d = [double][int]$v } catch { return $null } }
        }
        else { return $null }
        if (-not ($d -gt 0 -and $d -le 36000)) { return $null }
        # r3 (adversary pass 2 A3): rounded the way the runner's [int] cast rounds it (to nearest, ties to even), not up: 2450.1 is the 2450 s the runner plays
        $secs = [int][Math]::Round($d, [MidpointRounding]::ToEven)
        if ($secs -lt 1) { return $null }
        $secs
    } catch { return $null }
}

function Get-VenuePlanPlaySeconds {
    <#
    .SYNOPSIS
    The per-leg play seconds of a whole plan for the start cap: the mean (rounded up) of the legs whose spec could be read. A leg whose spec cannot be read is counted at the
    LONGEST readable leg (never at the default, which would shorten the cap); when no leg can be read the default (25) stands. Every unreadable spec is said, not hidden.
    .OUTPUTS
    [pscustomobject]@{ playSeconds; unknown (count of specs not read); known; line ($null, or the typed START_CAP_PLAYSECONDS_UNKNOWN line) }
    #>
    param([string[]]$SpecPaths = @(), [int]$Default = 25)
    $vals = @(foreach ($p in @($SpecPaths)) { Get-VenueLegPlaySeconds -SpecPath $p })
    $known = @($vals | Where-Object { $null -ne $_ })
    $unknown = @($SpecPaths).Count - $known.Count
    if ($known.Count -eq 0) {
        $line = $(if (@($SpecPaths).Count -gt 0) { "START_CAP_PLAYSECONDS_UNKNOWN unknown=$unknown known=0 usedSec=$Default (no leg spec could be read: the default stands)" } else { $null })
        return [pscustomobject]@{ playSeconds = $Default; maxPlaySeconds = $Default; unknown = $unknown; known = 0; line = $line; legSeconds = @() }
    }
    $max = [int](($known | Measure-Object -Maximum).Maximum)
    $sum = [double](($known | Measure-Object -Sum).Sum) + [double]$unknown * $max
    $mean = [int][Math]::Ceiling($sum / [double]@($SpecPaths).Count)
    $line = $(if ($unknown -gt 0) { "START_CAP_PLAYSECONDS_UNKNOWN unknown=$unknown known=$($known.Count) usedForUnknownSec=$max (an unreadable spec counts at the longest readable leg)" } else { $null })
    # r2 (A1): the per-leg seconds in plan order, an unreadable leg at the longest readable one; the start cap strides each leg by its own, not by the mean
    $legSeconds = @(foreach ($x in $vals) { if ($null -ne $x) { [int]$x } else { $max } })
    [pscustomobject]@{ playSeconds = $mean; maxPlaySeconds = $max; unknown = $unknown; known = $known.Count; line = $line; legSeconds = $legSeconds }
}

function Get-VenueLeaseCaps {
    <#
    .SYNOPSIS
    BACHELOR-DEDICATED-CAPS-RUNNER-1: Bachelor's heavy-slot.py lease caps, read from the text of its heavy-slot.lease.json (printed by the runner's exempt probe job) with
    the rules heavy-slot.py applies: enabled is true; max_minutes, pending_max_minutes, cooldown_minutes, max_fraction_per_3h and renew_stale_s are numbers >= 0;
    max_minutes > 0. Anything else (empty, missing file, not JSON, invalid) is the FALLBACK: the old mirrored constants, which are the tightest the owner ever set, so
    an unreadable file never loosens a cap. cooldown = cooldown_minutes*60, budget = max_fraction_per_3h*10800 (at most 10800), cap = min(MaxCapSec, max_minutes*60).
    r2 (CoS/Dot B1 on affa0d1a): the budget basis is ALWAYS the fixed 3 h window heavy-slot.py uses (10800 s), never the caller's -WindowSec, so a caller can never loosen
    it: a longer -WindowSec leaves the budget alone (Get-VenueLeaseBudgetWait then counts more of the earlier leases against it, which only tightens), and a shorter one
    can only shrink it (fraction * that window, never above the 3 h figure).
    r2 (B1b): a cooldown is never silently shortened. cooldown_minutes*60 is kept exactly (2880 min = 172800 s); only a value no int of seconds can hold is
    unrepresentable, and that is the FALLBACK (reason cooldown_minutes-unrepresentable), the old constants, logged as capsFallbackReason.
    .OUTPUTS
    [pscustomobject]@{ source (live | fallback); reason ($null when live); cooldownSec; budgetSec; capSec }
    #>
    param([AllowEmptyString()][AllowNull()][string]$Text = '', [int]$WindowSec = 10800, [int]$FallbackCooldownSec = 1800, [int]$FallbackBudgetSec = 5400, [int]$MaxCapSec = 2700,
        [int]$BudgetBasisSec = 10800)
    $fallback = { param([string]$Why) [pscustomobject]@{ source = 'fallback'; reason = $Why; cooldownSec = $FallbackCooldownSec; budgetSec = $FallbackBudgetSec; capSec = $MaxCapSec } }
    if ([string]::IsNullOrWhiteSpace($Text)) { return (& $fallback 'probe-printed-nothing') }
    $j = try { $Text | ConvertFrom-Json -ErrorAction Stop } catch { $null }
    if ($null -eq $j -or $j -isnot [pscustomobject]) { return (& $fallback 'not-a-json-object') }
    if ($j.enabled -isnot [bool] -or -not $j.enabled) { return (& $fallback 'enabled-is-not-true') }
    $v = @{}
    foreach ($k in 'max_minutes', 'pending_max_minutes', 'cooldown_minutes', 'max_fraction_per_3h', 'renew_stale_s') {
        $x = $j.$k
        $num = ($x -is [int] -or $x -is [long] -or $x -is [double] -or $x -is [decimal])
        if (-not $num -or $x -lt 0) { return (& $fallback "$k-is-not-a-number-at-least-0") }
        $v[$k] = [double]$x
    }
    if ($v['max_minutes'] -le 0) { return (& $fallback 'max_minutes-is-not-above-0') }
    $cooldownSec = [Math]::Ceiling($v['cooldown_minutes'] * 60)
    if ($cooldownSec -gt [int]::MaxValue) { return (& $fallback 'cooldown_minutes-unrepresentable') }
    $f = $v['max_fraction_per_3h']
    $fixedBudget = [Math]::Min($BudgetBasisSec, $f * $BudgetBasisSec)
    $windowBudget = [Math]::Min($WindowSec, $f * $WindowSec)
    [pscustomobject]@{ source = 'live'; reason = $null
        cooldownSec = [int]$cooldownSec
        budgetSec = [int][Math]::Floor([Math]::Min($fixedBudget, $windowBudget))
        capSec = [int][Math]::Floor([Math]::Min($MaxCapSec, $v['max_minutes'] * 60)) }
}

function New-VenueCapsProbeId {
    <#
    .SYNOPSIS
    The job id of the run's one caps probe: venue-quiet-probe-caps-<UTC stamp>-<12 hex of a fresh GUID>. The stamp alone is one-second resolution, so two runs that start in
    the same second would submit the same id (UmRunDrop refuses a job id already in use, and the run would read the fallback caps). Still matches the agent's
    GateExemptJobPattern ^(venue-quiet-probe|agent-swap)- and UmRunDrop's job id rule ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$.
    #>
    'venue-quiet-probe-caps-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 12)
}

function Resolve-VenueLeaseCaps {
    <#
    .SYNOPSIS
    The caps a run uses: the live ones (Get-VenueLeaseCaps), unless the caller passed one explicitly. A caller may only TIGHTEN: a cooldown shorter than the live one, or a
    budget above it, is LEASE_CAP_RELAXED, except with -AllowRelax (an offline stub um-run, the old shortening rule). The lease cap is the smaller of -CapSec and the live one.
    .OUTPUTS
    [pscustomobject]@{ ok; error; cooldownSec; budgetSec; capSec }
    #>
    param([Parameter(Mandatory)]$Caps, [int]$CooldownSec, [bool]$CooldownExplicit = $false, [int]$BudgetSec, [bool]$BudgetExplicit = $false, [int]$CapSec = 2700, [bool]$AllowRelax = $false)
    $bad = { param([string]$Msg) [pscustomobject]@{ ok = $false; error = "LEASE_CAP_RELAXED $Msg"; cooldownSec = $null; budgetSec = $null; capSec = $null } }
    $cd = $Caps.cooldownSec; $bud = $Caps.budgetSec
    if ($CooldownExplicit) {
        if ($CooldownSec -lt $Caps.cooldownSec -and -not $AllowRelax) { return (& $bad "-LeaseCooldownSec $CooldownSec is shorter than Bachelor's cooldown ($($Caps.cooldownSec) s, source=$($Caps.source))") }
        $cd = $CooldownSec
    }
    if ($BudgetExplicit) {
        if ($BudgetSec -gt $Caps.budgetSec -and -not $AllowRelax) { return (& $bad "-LeaseBudgetSec $BudgetSec is above Bachelor's budget ($($Caps.budgetSec) s per window, source=$($Caps.source))") }
        $bud = $BudgetSec
    }
    [pscustomobject]@{ ok = $true; error = $null; cooldownSec = [int]$cd; budgetSec = [int]$bud; capSec = [int][Math]::Min($CapSec, $Caps.capSec) }
}

function Invoke-VenueBoundedProcess {
    <#
    .SYNOPSIS
    Run one child process and give up on it after -TimeoutSec (the child and its tree are killed). .exit is $null when it timed out.
    .OUTPUTS
    [pscustomobject]@{ exit; timedOut; killConfirmed; stdout; stderr; notStarted; reason (DEADLINE | ROOM: nothing was started; KILLED_AT_START: started after -StartBeforeUtc and killed at once; '' otherwise); childPid }. killConfirmed (EXPORT-VENUE-LEG-1 r11) is $true when the child exited on its own, or was killed and HasExited read true after the 15 s wait; a caller that
    must not leave a child behind reads it.
    #>
    param([Parameter(Mandatory)][string]$FilePath, [Parameter(Mandatory)][string[]]$ArgumentList, [Parameter(Mandatory)][ValidateRange(1, 86400)][int]$TimeoutSec,
          [ValidateRange(100, 60000)][int]$CleanupWaitMs = 15000,
          [Diagnostics.Stopwatch]$Clock = $null, [double]$EndSec = 0, [Nullable[DateTime]]$StartBeforeUtc = $null,
          [ValidateRange(0, 600000)][int]$TestStartDelayMs = 0, [switch]$TestKillThrows, [ValidateRange(0, 600000)][int]$TestAfterStartDelayMs = 0, [ValidateRange(0, 600000)][int]$TestBeforeWaitDelayMs = 0, [ValidateRange(0, 600000)][int]$TestBeforeDrainDelayMs = 0)
    # r7 (adversary r7 pass 17 A1): the four offline seams above are inert unless the process says it is an offline test (MLV_VENUE_BOUNDED_TEST_SEAMS=1, set by the batch script only when one of its own seam switches is
    # asked for, and by the module tests). A value a caller never typed, one that $PSDefaultParameterValues (a profile, a wrapper) binds to the parameter, is indistinguishable from one it typed (both read as bound), so the
    # production guard cannot sit on the parameter: no caller of this function, in this repo or outside it, can inherit a delay or a failing kill into a production child.
    if ($env:MLV_VENUE_BOUNDED_TEST_SEAMS -cne '1') { $TestStartDelayMs = 0; $TestKillThrows = [switch]::new($false); $TestAfterStartDelayMs = 0; $TestBeforeWaitDelayMs = 0; $TestBeforeDrainDelayMs = 0 }
    # r6 fix (adversary r6 pass 15 A1, A2): right after Process.Start the child is also late when the caller's SAMPLED END is already used up (lateKind ROOM, as a passed -StartBeforeUtc is lateKind DEADLINE): it is killed at
    # once and reported KILLED_AT_START, so nothing runs on with no room. And a child that exited on its own is no answer when the wait began with no room left (WaitForExit(0) is true for an exited child): it is timedOut,
    # exit $null, never accepted. -TestAfterStartDelayMs (held right after Process.Start's late check) stands in for a parent descheduled between the launch and the wait.
    # r6 (Grok G10): a child that came to life only after -StartBeforeUtc WAS started and is killed at once: it is reported notStarted with reason KILLED_AT_START (not DEADLINE, which means nothing was started),
    # with killConfirmed and childPid (the pid of the process that was started; $null in every return where none was). A caller never reads an unconfirmed KILLED_AT_START as "no child".
    # -TestStartDelayMs (held after the checks, just before Process.Start) and -TestKillThrows (the tree kill throws) are offline seams for the batch's tests; a caller that passes neither is unchanged.
    # r5 (adversary r5 pass 14 A2): -Clock / -EndSec / -StartBeforeUtc are optional (a caller that passes none is unchanged). With them, the wait is NOT a fresh relative -TimeoutSec: it is what is left of the caller's
    # sampled end on its stopwatch AT the wait, so a caller descheduled between the launch and the wait gets no extra time; nothing is started with under 1 s left of that end or at or after -StartBeforeUtc (notStarted, with
    # the reason); and a child that came to life only after -StartBeforeUtc (a caller descheduled in the gap just before Process.Start) is killed at once and reported notStarted.
    # r11 (adversary r11 A5, A6): after the bound, the kill wait and both stream waits share ONE budget ($CleanupWaitMs: they used to add up to 3 x 15 s past a caller's deadline), and a tree kill
    # that threw is never "confirmed" because the root happened to exit: killConfirmed needs no kill error AND the root gone.
    $psi = [Diagnostics.ProcessStartInfo]::new($FilePath)
    foreach ($a in $ArgumentList) { [void]$psi.ArgumentList.Add($a) }
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
    if ($null -ne $StartBeforeUtc -and [DateTime]::UtcNow -ge $StartBeforeUtc) { return [pscustomobject]@{ exit = $null; timedOut = $false; killConfirmed = $true; stdout = ''; stderr = ''; notStarted = $true; reason = 'DEADLINE'; lateKind = ''; childPid = $null } }
    if ($null -ne $Clock -and -not (($EndSec - $Clock.Elapsed.TotalSeconds) -ge 1)) { return [pscustomobject]@{ exit = $null; timedOut = $false; killConfirmed = $true; stdout = ''; stderr = ''; notStarted = $true; reason = 'ROOM'; lateKind = ''; childPid = $null } }
    if ($TestStartDelayMs -gt 0) { [Threading.Thread]::Sleep($TestStartDelayMs) }
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $outTask = $p.StandardOutput.ReadToEndAsync(); $errTask = $p.StandardError.ReadToEndAsync()
    $lateKind = $(if ($null -ne $StartBeforeUtc -and [DateTime]::UtcNow -ge $StartBeforeUtc) { 'DEADLINE' } elseif ($null -ne $Clock -and -not (($EndSec - $Clock.Elapsed.TotalSeconds) -gt 0)) { 'ROOM' } else { '' })   # (r6 fix A4: written as "not greater", so a NaN end is no room, never a start)
    $lateStart = [bool]$lateKind
    if ($TestAfterStartDelayMs -gt 0) { [Threading.Thread]::Sleep($TestAfterStartDelayMs) }
    $waitSw = [Diagnostics.Stopwatch]::StartNew()   # the relative -TimeoutSec runs from here
    $waitMs = [double]$TimeoutSec * 1000.0
    if ($null -ne $Clock) { $waitMs = [Math]::Min($waitMs, ($EndSec - $Clock.Elapsed.TotalSeconds) * 1000.0) }
    $pastEnd = ($null -ne $Clock -and -not ($waitMs -gt 0))
    if ($TestBeforeWaitDelayMs -gt 0) { [Threading.Thread]::Sleep($TestBeforeWaitDelayMs) }   # (offline seam: a parent descheduled after the wait was computed and before it begins)
    # r8 (adversary r8 pass 18 A1): a wait sized once and begun later is stale by whatever happened between (a parent descheduled after it was computed got the whole stale wait past the end). With a clock the wait is taken in
    # slices of at most 250 ms, each sized from what is left of the sampled end and of the relative bound AT THAT MOMENT (a NaN is no room); a caller with no clock is unchanged (one wait of -TimeoutSec).
    $timedOut = $true
    if ($lateStart -or $pastEnd) { $timedOut = $true } elseif ($null -eq $Clock) { $timedOut = -not $p.WaitForExit([int][Math]::Max(0.0, [Math]::Min([double][int]::MaxValue, $waitMs))) } else {
        while ($true) {
            $leftMs = [Math]::Min([double]$TimeoutSec * 1000.0 - $waitSw.Elapsed.TotalMilliseconds, ($EndSec - $Clock.Elapsed.TotalSeconds) * 1000.0)
            if (-not ($leftMs -gt 0)) { break }
            if ($p.WaitForExit([int][Math]::Ceiling([Math]::Min(250.0, $leftMs)))) { $timedOut = $false; break }
        }
    }
    # r7 (adversary r7 pass 17 A2): WaitForExit is true for a child that had already exited, whenever the parent got to ask: an answer observed only after the sampled end is a timeout, never an answer (exit $null, as the
    # no-room-at-the-wait case above). The child needs no kill (it is gone), so killConfirmed reads from HasExited as for any timeout.
    if (-not $timedOut -and $null -ne $Clock -and -not (($EndSec - $Clock.Elapsed.TotalSeconds) -gt 0)) { $timedOut = $true }
    $killErrored = $false
    $cleanup = [Diagnostics.Stopwatch]::StartNew()
    # r8 (adversary r8 pass 18 A2, module sibling): the streams of a child that answered inside its end are still drained for up to -CleanupWaitMs (a descendant that holds the stdout keeps them open), which can run past the end:
    # the drain is bounded by the end too, and an answer that is only in hand after the end is a timeout (its tree is killed below).
    if (-not $timedOut) {
        # r9 (adversary r9 pass 19 A1): a drain sized once and begun later is as stale as the wait was: a parent descheduled between the sample and Task.Wait got the whole stale size past the end. Each stream is drained in slices of at most
        # 250 ms, each sized from what is left of the end and of the cleanup budget AT THAT MOMENT (a NaN is no room). -TestBeforeDrainDelayMs (once, after a slice is sized and before it waits) stands in for that parent.
        $drainPaused = $false
        foreach ($stream in @($outTask, $errTask)) {
            while (-not $stream.IsCompleted) {
                $m = [double]($CleanupWaitMs - $cleanup.ElapsedMilliseconds)
                if ($null -ne $Clock) { $m = [Math]::Min($m, ($EndSec - $Clock.Elapsed.TotalSeconds) * 1000.0) }
                if (-not ($m -gt 0)) { break }
                if ($TestBeforeDrainDelayMs -gt 0 -and -not $drainPaused) { $drainPaused = $true; [Threading.Thread]::Sleep($TestBeforeDrainDelayMs) }
                [void]$stream.Wait([int][Math]::Ceiling([Math]::Min(250.0, $m)))
            }
        }
        if ($null -ne $Clock -and -not (($EndSec - $Clock.Elapsed.TotalSeconds) -gt 0)) { $timedOut = $true }
    }
    # r8 (adversary r8 pass 18 A3): the tree is killed on EVERY timeout, also when the root had already exited (a descendant that outlived it keeps running: Kill($true) of an exited root takes its surviving descendants and does not
    # throw, measured), so killConfirmed never reads a root that merely exited as a tree that is gone. (-TestKillThrows: the kill is not made and reads as failed.)
    if ($timedOut) { if ($TestKillThrows) { $killErrored = $true } else { try { $p.Kill($true) } catch { $killErrored = $true } }; [void]$p.WaitForExit([int][Math]::Max([Math]::Min($CleanupWaitMs, 500), $CleanupWaitMs - $cleanup.ElapsedMilliseconds)) }   # (a floor of 500 ms: a drain that used the whole budget must still leave the kill time to land)
    [void]$outTask.Wait([int][Math]::Max(0, $CleanupWaitMs - $cleanup.ElapsedMilliseconds)); [void]$errTask.Wait([int][Math]::Max(0, $CleanupWaitMs - $cleanup.ElapsedMilliseconds))
    [pscustomobject]@{ exit = $(if ($timedOut) { $null } else { $p.ExitCode }); timedOut = $timedOut; killConfirmed = $(if ($timedOut) { (-not $killErrored) -and [bool]$p.HasExited } else { $true })
        stdout = $(if ($outTask.IsCompleted) { $outTask.Result } else { '' }); stderr = $(if ($errTask.IsCompleted) { $errTask.Result } else { '' }); notStarted = $lateStart; reason = $(if ($lateStart) { 'KILLED_AT_START' } else { '' }); lateKind = $lateKind; childPid = $p.Id; process = $p }   # (r9 G13: the Process, so a caller that judges the answer late AFTER this return can kill the tree of a root that answered)
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

Export-ModuleMember -Function New-VenueCapsProbeId, Get-VenueDriverScriptMissing, Get-VenueMainRoot, Get-VenueClaimPath, Get-VenueClaim, Get-VenueClaimStaleReason, Request-VenueClaim, Release-VenueClaim,
    Get-VenueTicketDir, New-VenueTicket, Get-VenueTickets, Get-VenueTicketStaleReason, Get-VenueTicketProbeStaleSec, Update-VenueTicket, Remove-VenueTicket, Get-VenueClaimStatus, Format-VenueClaimStatus, Format-VenueTicketQueue,
    ConvertFrom-VenueGateLog, Get-VenueQuietRateTable, Format-VenueQuietRateTable, Select-VenueQuietWindow,
    Get-VenueSupplementarySpecSkip, Get-VenueSupplementaryIdle, Get-VenueStagedBuildManifest, Get-VenueStagedArtifactNames, Get-VenueRemoteJobIds, Get-VenueRemoteJobState, Invoke-VenueBoundedProcess, Complete-VenueProcess,
    Get-VenueJobResult, Request-VenueJobCancel, Read-VenueLeaseBatchReceipt, Get-VenueInterleavedLegPlan, Get-VenueLegPreLoadMean, ConvertFrom-VenueSmokeHostLoad, ConvertFrom-VenueSmokeResultText, Find-VenueJsonDuplicateMember, Get-VenuePlannedLegType, Get-VenueLegHostLoad, Limit-VenueHostLoadToDeadline, Test-VenueReceiptIsExportLeg,Get-VenueReferenceHostLoadRefusal, Get-VenueReferenceArmVerdict, Get-VenueLeaseLedgerPath, Read-VenueLeaseLedger, Add-VenueLeaseLedger, Get-VenueLeaseBudgetWait, Get-VenueRunStartCap, Get-VenueLegPlaySeconds, Get-VenuePlanPlaySeconds, ConvertTo-VenueStartCapTime,Get-VenueLeaseCaps, Resolve-VenueLeaseCaps,
    Get-VenueClaimAgeSec, Update-VenueClaim, Start-VenueKeeper, Stop-VenueKeeper, Start-VenueClaimKeeper, Get-VenueClaimKeeperLoss, New-VenueJobLivenessWrite, Write-VenueJobLiveness, Test-VenueJobLivenessReadback, Start-VenueJobLiveness, Stop-VenueJobLiveness
