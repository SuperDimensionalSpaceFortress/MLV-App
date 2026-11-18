<#
.SYNOPSIS
  The ONLY writer of the heavy-slot A/B switch file (HEAVY-SLOT-SECOND-SLOT-MEASURED-1): a bounded A/B window in which the
  fast-suites pytest ticket runs at HEAVY_SLOT_COUNT=2 in the B blocks and at the machine's normal count (1) in the A blocks.

.DESCRIPTION
  Builds always stay at count 1 (tools/coordination/build-guard.ps1 strips HEAVY_SLOT_COUNT from every build). Only the staged
  hub-tools\fast-suites-for-diff.ps1 patch reads the switch (HeavySlotAb.psm1 logic, inlined), so the possible overlaps are
  build + pytest and pytest + pytest, never two builds.

  This script never edits, installs or asks anything of heavy-slot itself and never opens, waits on or probes a heavy-slot
  mutex: it reads C:\ProgramData\HeavySlot\{ledger.jsonl, receipts\, held\} read-only. It never calls heavy-slot's reporting verb (that verb sweeps live waiters).

  PREFLIGHT (any failure ends REFUSED, exit 2, and no switch is left at count 2; a refusal writes nothing else):
    - a named mutex shows that no other driver is running (created owned; never waited on);
    - an existing switch file is reverted to count 1;
    - heavy-slot.ps1 SHA-256 equals the pin; the fast-suites script carries the line `HEAVY_SLOT_AB_READER v1`;
    - the CoreTemp feed is under 120 s old; free commit >= 16 GB; C: free >= 40 GiB.
  WINDOW: -Hours (max 25) of alternating -BlockMinutes blocks A, B, A, B ... starting with A. At each block edge the switch is
  rewritten atomically (temp file + move over the target) with state A (count 1) or B (count 2) and one row is appended to
  <WindowDir>\ab-blocks.jsonl. Every -PollSeconds one row is appended to <WindowDir>\ab-samples.jsonl.
  HARD ABORTS (defaults; each rewrites the switch to REVERTED / count 1, writes ab-receipt.json state ABORTED + reason, exit 3):
    thermal-cap (>= 95 C for >= 60 s in any 5 min), warm-while-2 (>= 90 C for > 50 % of a rolling 15 min while 2+ holders),
    stale-feed (> 180 s), low-memory (commit < 10 GB or physical < 6 GB on 2 consecutive samples), event-2004, low-disk
    (C: < 40 GiB), unguarded (ledger event), exclusive-admit (an admit with class != shared or wholeMachine = true),
    stop-file (<WindowDir>\ab-stop exists).
  END: state REVERTED / count 1, ab-receipt.json state DONE, exit 0. A try/finally reverts the switch on every exit path.
  The revert is a REWRITE of the switch file; this script never removes it. Killing the process makes the reader answer 1
  at once (driver pid gone).

  Exit: 0 DONE, 2 REFUSED, 3 ABORTED, 4 the final revert could not be proven (receipt switchFinal REVERT_FAILED; the reader still
  answers 1 once the process is gone). One window per directory: a -WindowDir that already holds ab-blocks.jsonl, ab-samples.jsonl or
  ab-receipt.json is refused (window-dir-used). A System Event 2004 count that cannot be read twice in a row aborts the window
  (event-2004-unreadable). Holders are exact (pid + start ticks) and a feed row counts as 2+ holders only when both bracketing polls saw 2+.
  Round 2 of the adversary pass: ONE driver per switch, whatever -MutexName says (an exclusive lock file <SwitchPath>.lock is held for the
  life of the process and never removed); the switch, its lock and the window dir may not be, or lie under, anything the driver only reads
  (path-overlap); a preflight read that fails is a refusal (preflight-failed), never an abort; a refusal whose revert cannot be proven
  exits 4; the ledger and receipt baselines are taken BEFORE the window clock starts and one last sweep runs after the final poll; the
  block applied is the one due NOW by the clock (a slow poll skips blocks), and the blocks log's startUtc is when the state was applied.
  Round 4 (CI run 44251460414 and adversary pass 3): a switch rewrite retries against a TIME budget (-SwitchRetrySeconds, default 10) so a
  reader that holds the file past ~1 s on a slow host never ends the window; the window dir is CLAIMED by an exclusive ab-window.lock (held,
  never removed, so a used dir stays used); evidence fails CLOSED: a ledger unreadable on 2 polls in a row or in the final sweep
  (ledger-unreadable), a held\*.json record unparseable on 2 polls (held-unreadable) and a receipt that never parses (receipt-unreadable)
  abort the window; free disk and memory are compared unrounded; a block's startUtc is taken BEFORE the switch write (the row also carries
  switchWrittenUtc); a feed row dated later than now + 30 s is invalid (feed-future refusal; a frozen future row ages into stale-feed); the
  containment checks resolve junctions, symlinks and short names first. Exit 5 = the final receipt could not be written (the switch is
  already reverted; the window must not be read as DONE).
  ASCII-only on purpose.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$WindowDir,
    [ValidateRange(0.001, 25)][double]$Hours = 24,
    [ValidateRange(0.01, 1500)][double]$BlockMinutes = 180,
    [ValidateRange(0.05, 600)][double]$PollSeconds = 15,
    [string]$SwitchPath = 'C:\mlvtmp\locks\heavy-slot-ab.json',
    [string]$HeavySlotDir = 'C:\ProgramData\HeavySlot',
    [string]$CoreTempFeed = 'C:\temp\hwinfo-log-adapter.csv',
    [string]$MemoryProbeFile = '',     # test seam: json {"freeCommitGB": n, "freePhysGB": n}; empty = Win32_OperatingSystem
    [string]$EventProbeFile = '',      # test seam: a file holding the number of System Event 2004 rows since the window began
    [string]$DiskProbeFile = '',       # test seam: json {"freeGiB": n} for C:; empty = the real drive
    [string]$FastSuitesPath = 'C:\!Layi Wkspc\MLV-App\.claude-state\fleet-runs\hub-tools\fast-suites-for-diff.ps1',
    [string]$PinSha = '691efb4c2cc1b98cd5a7379e03a2bb5b9eebaa1174a03b11ee7a48741fc06534',
    [string]$MutexName = 'Global\MLV-HeavySlotAbDriver',
    [switch]$NoPerfProbe,              # test seam: skip the processor performance query
    [double]$ProbeDelaySeconds = 0,    # test seam: sleep this long before each poll's probes (a slow host)
    [double]$CapTempC = 95,
    [double]$CapSeconds = 60,
    [double]$CapWindowSeconds = 300,
    [double]$WarmTempC = 90,
    [double]$WarmFraction = 0.5,
    [double]$WarmWindowSeconds = 900,
    [double]$FeedAbortSeconds = 180,
    [double]$MinFreeCommitGB = 10,
    [double]$MinFreePhysGB = 6,
    [double]$MinFreeDiskGiB = 40,
    [double]$PreflightFeedSeconds = 120,
    [double]$PreflightCommitGB = 16,
    [double]$EventCheckSeconds = 300,
    [double]$SwitchRetrySeconds = 10,         # how long one atomic switch rewrite keeps retrying against a reader that holds the file open
    [double]$FeedFutureSkewSeconds = 30,      # a feed row later than now + this is invalid (a future-dated row must never look fresh)
    [int]$ReceiptGiveUpReads = 200,           # a receipt that still does not parse after this many reads ends the window (never skipped)
    [double]$WindowClaimDelaySeconds = 0,     # test seam: pause between the unused-directory check and the claim, to make the race certain
    [double]$PostSwitchDelaySeconds = 0,      # test seam: pause right after a switch write (a descheduled driver)
    [double]$DiskProbeBytes = -1              # test seam: C: available bytes (>= 0 replaces the drive query)
)

$ErrorActionPreference = 'Stop'
Import-Module -Force (Join-Path $PSScriptRoot 'HeavySlotAb.psm1')    # Resolve-HeavySlotAbRealPath (outside the pinned reader region)
$inv = [Globalization.CultureInfo]::InvariantCulture
$utf8 = New-Object System.Text.UTF8Encoding($false)
$stopFile = Join-Path $WindowDir 'ab-stop'
$blocksFile = Join-Path $WindowDir 'ab-blocks.jsonl'
$samplesFile = Join-Path $WindowDir 'ab-samples.jsonl'
$receiptFile = Join-Path $WindowDir 'ab-receipt.json'

function Get-IsoUtc([DateTime]$t) { return $t.ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ss.fffZ', $inv) }

function ConvertTo-UtcInstant($v) {
    # ConvertFrom-Json already turns an ISO date string into a [DateTime] (UTC kind, whole ticks); a string is parsed as UTC. $null when
    # it is neither. Never go through [string] of a DateTime: that drops the fraction and renders local time.
    if ($null -eq $v -or "$v" -eq '') { return $null }
    if ($v -is [DateTime]) { return $v.ToUniversalTime() }
    if ($v -is [DateTimeOffset]) { return $v.UtcDateTime }
    try { return [DateTimeOffset]::Parse([string]$v, $inv, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime } catch { return $null }
}

function Write-AtomicText([string]$Path, [string]$Text) {
    # The temp name is FIXED (<path>.tmp, no pid) so the containment check can name it before anything is written: one driver per switch
    # and one per window dir, so nothing else ever uses it (adversary r4 A4).
    $tmp = $Path + '.tmp'
    [IO.File]::WriteAllText($tmp, $Text, $utf8)
    [IO.File]::Move($tmp, $Path, $true)
}

function ConvertTo-FiniteOrNull($v) {
    # A finite double or $null. NaN and +-Infinity (a probe file saying "NaN", a feed cell "1e999") are UNKNOWN, never a number a floor can
    # be compared with: every comparison against NaN is false, which would read as "above the floor" (adversary r4 A6).
    if ($null -eq $v) { return $null }
    if ($v -is [double] -or $v -is [single] -or $v -is [decimal]) { $d0 = [double]$v; if ([double]::IsFinite($d0)) { return $d0 } else { return $null } }
    $d = 0.0
    if ([double]::TryParse([string]$v, [Globalization.NumberStyles]::Float, $inv, [ref]$d) -and [double]::IsFinite($d)) { return $d }
    return $null
}

function Add-JsonLine([string]$Path, $Obj) {
    [IO.File]::AppendAllText($Path, (($Obj | ConvertTo-Json -Compress -Depth 6) + "`n"), $utf8)
}

$script:refused = $false
function Write-Refused([string]$Reason) {
    $script:refused = $true
    Write-Output "AB_REFUSED reason=$Reason"
    exit 2
}

# ---- probes -----------------------------------------------------------------------------------------------------------------
$script:feedFuture = 0
function Read-Feed {
    # The CoreTemp feed rows newest-last: @{ utc; cpu; temp; watts }. Row times are LOCAL time (dd.MM.yyyy HH:mm:ss.fff).
    # Opened shared so the writer is never blocked; only the tail is read. An unreadable feed returns @().
    # A row dated later than now + FeedFutureSkewSeconds is INVALID and dropped (counted in $script:feedFuture): a frozen future-dated row
    # would otherwise read as "fresh" for ever and hide a stalled feed (Dot B1).
    $script:feedFuture = 0
    try {
        $fs = [IO.File]::Open($CoreTempFeed, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        try {
            $len = $fs.Length
            $headBuf = New-Object byte[] ([int][Math]::Min(1024, $len))
            [void]$fs.Read($headBuf, 0, $headBuf.Length)
            $header = ([Text.Encoding]::ASCII.GetString($headBuf) -split "`r?`n")[0]
            $cols = @($header -split ',')
            $iDate = [Array]::IndexOf($cols, 'Date'); $iTime = [Array]::IndexOf($cols, 'Time')
            $iCpu = [Array]::IndexOf($cols, 'Total CPU Usage [%]'); $iTemp = [Array]::IndexOf($cols, 'CPU Package [C]'); $iW = [Array]::IndexOf($cols, 'CPU Package Power [W]')
            if ($iDate -lt 0 -or $iTime -lt 0 -or $iTemp -lt 0) { return @() }
            $from = [Math]::Max(0, $len - 65536)
            [void]$fs.Seek($from, [IO.SeekOrigin]::Begin)
            $buf = New-Object byte[] ([int]($len - $from))
            $got = 0
            while ($got -lt $buf.Length) { $n = $fs.Read($buf, $got, $buf.Length - $got); if ($n -le 0) { break }; $got += $n }
        }
        finally { $fs.Dispose() }
        $lines = @([Text.Encoding]::ASCII.GetString($buf, 0, $got) -split "`r?`n")
        if ($from -gt 0 -and $lines.Count -gt 0) { $lines = @($lines | Select-Object -Skip 1) }   # the first line is a cut one
        $rows = New-Object System.Collections.Generic.List[object]
        foreach ($l in $lines) {
            $f = $l -split ','
            if ($f.Count -le $iTemp -or $f[0] -eq 'Date') { continue }
            $dt = [DateTime]::MinValue
            if (-not [DateTime]::TryParseExact(('{0} {1}' -f $f[$iDate], $f[$iTime]), 'dd.MM.yyyy HH:mm:ss.fff', $inv, [Globalization.DateTimeStyles]::None, [ref]$dt)) { continue }
            $temp = ConvertTo-FiniteOrNull $f[$iTemp]
            if ($null -eq $temp) { continue }     # NaN / Infinity / unparseable temperature: the row is invalid, never "cool"
            $cpu = $null; $w = $null
            if ($iCpu -ge 0 -and $f.Count -gt $iCpu) { $cpu = ConvertTo-FiniteOrNull $f[$iCpu] }
            if ($iW -ge 0 -and $f.Count -gt $iW) { $w = ConvertTo-FiniteOrNull $f[$iW] }
            $rowUtc = [TimeZoneInfo]::ConvertTimeToUtc($dt, [TimeZoneInfo]::Local)
            if ($rowUtc -gt [DateTime]::UtcNow.AddSeconds($FeedFutureSkewSeconds)) { $script:feedFuture++; continue }
            $rows.Add([pscustomobject]@{ utc = $rowUtc; cpu = $cpu; temp = $temp; watts = $w })
        }
        return $rows.ToArray()
    }
    catch { return @() }
}

function Get-MemoryProbe {
    # @{ commit; phys } in GB, or $null per field when unreadable.
    if ($MemoryProbeFile) {
        try { $m = Get-Content -LiteralPath $MemoryProbeFile -Raw | ConvertFrom-Json; return [pscustomobject]@{ commit = (ConvertTo-FiniteOrNull $m.freeCommitGB); phys = (ConvertTo-FiniteOrNull $m.freePhysGB) } }
        catch { return [pscustomobject]@{ commit = $null; phys = $null } }
    }
    # Raw doubles: the floors are compared against the exact reading and only the printed text is ever rounded (adversary r2 A8).
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -Property FreePhysicalMemory, FreeVirtualMemory -OperationTimeoutSec 10 -ErrorAction Stop
        return [pscustomobject]@{ commit = ([double]$os.FreeVirtualMemory / 1MB); phys = ([double]$os.FreePhysicalMemory / 1MB) }
    }
    catch { return [pscustomobject]@{ commit = $null; phys = $null } }
}

function Get-DiskProbe {
    if ($DiskProbeBytes -ge 0) { return ($DiskProbeBytes / 1GB) }
    if ($DiskProbeFile) {
        try { return (ConvertTo-FiniteOrNull ((Get-Content -LiteralPath $DiskProbeFile -Raw | ConvertFrom-Json).freeGiB)) } catch { return $null }
    }
    try { return ([double][IO.DriveInfo]::new('C').AvailableFreeSpace / 1GB) } catch { return $null }
}

function Get-PerfProbe {
    if ($NoPerfProbe) { return [pscustomobject]@{ perf = $null; freq = $null } }
    try {
        $c = Get-CimInstance -ClassName Win32_PerfFormattedData_Counters_ProcessorInformation -Filter "Name='_Total'" -Property Name, PercentProcessorPerformance, ProcessorFrequency -OperationTimeoutSec 10 -ErrorAction Stop | Select-Object -First 1
        if ($null -eq $c) { return [pscustomobject]@{ perf = $null; freq = $null } }
        return [pscustomobject]@{ perf = [double]$c.PercentProcessorPerformance; freq = [Math]::Round([double]$c.ProcessorFrequency * [double]$c.PercentProcessorPerformance / 100.0, 0) }
    }
    catch { return [pscustomobject]@{ perf = $null; freq = $null } }
}

function Get-EventCount([DateTime]$SinceUtc) {
    # System Event 2004 (resource exhaustion) rows since the window began; $null when the count CANNOT be read. "No such events" is a
    # real zero (Get-WinEvent says NoMatchingEventsFound); any other failure, an unreadable probe file or a probe that is not a
    # whole number is unknown, never zero (adversary r1 A3).
    if ($EventProbeFile) {
        try {
            $n = 0
            if ([int]::TryParse(([IO.File]::ReadAllText($EventProbeFile)).Trim(), [ref]$n) -and $n -ge 0) { return $n }
            return $null
        }
        catch { return $null }
    }
    try {
        $ev = @(Get-WinEvent -FilterHashtable @{ LogName = 'System'; Id = 2004; StartTime = $SinceUtc.ToLocalTime() } -MaxEvents 5 -ErrorAction Stop)
        return $ev.Count
    }
    catch {
        if ([string]$_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { return 0 }
        return $null
    }
}

function Get-LiveHolders {
    # Live holder records: a held\*.json whose pid is alive with EXACTLY the recorded start time (100 ns ticks, the comparison heavy-slot
    # itself makes for its queue records), so a reused pid never reads as a holder (adversary r1 A8). A holder whose record cannot be
    # judged counts as absent, so the figure can undercount (TRAPS.md:29367); it never invents a holder.
    # A record that CANNOT BE PARSED (a lock, a half-written file) is counted in $script:heldUnreadable and two polls in a row end the
    # window (held-unreadable); a record that is simply gone, or whose pid is not alive, is an absent holder.
    $script:heldUnreadable = 0
    $out = New-Object System.Collections.Generic.List[object]
    $dir = Join-Path $HeavySlotDir 'held'
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return @() }
    foreach ($f in [IO.Directory]::GetFiles($dir, '*.json')) {
        $h = $null
        try { $h = Get-Content -LiteralPath $f -Raw | ConvertFrom-Json; if ($null -eq $h) { throw 'empty held record' } }
        catch { if (Test-Path -LiteralPath $f -PathType Leaf) { $script:heldUnreadable++ }; continue }
        try {
            $p = Get-Process -Id ([int]$h.pid) -ErrorAction Stop
            $rec = ConvertTo-UtcInstant $h.pidStart
            if ($null -eq $rec) { continue }
            if ($p.StartTime.ToUniversalTime().Ticks -ne $rec.Ticks) { continue }
            $out.Add([pscustomobject]@{ label = [string]$h.label; slots = @($h.slots); cls = [string]$h.class; whole = ($h.wholeMachine -eq $true) })
        }
        catch { continue }
    }
    return $out.ToArray()
}

# ---- switch ------------------------------------------------------------------------------------------------------------------
$script:switchAttemptStart = [DateTime]::UtcNow
$script:windowStart = [DateTime]::MinValue
$script:windowEnd = [DateTime]::MinValue
$script:selfTicks = 0
$script:blockSec = 0.0
$script:appliedBlock = -1
$script:appliedState = ''
$script:appliedCount = 1
function New-SwitchText([string]$State, [int]$Count, [int]$Block) {
    $o = [ordered]@{
        schema = 'heavy-slot-ab.v1'; count = $Count; state = $State
        windowStartUtc = (Get-IsoUtc $script:windowStart); endUtc = (Get-IsoUtc $script:windowEnd)
        driverPid = $PID; driverStartTicks = $script:selfTicks; block = $Block; updatedUtc = (Get-IsoUtc ([DateTime]::UtcNow))
    }
    return (($o | ConvertTo-Json -Compress) + "`n")
}
function Set-Switch([string]$State, [int]$Count, [int]$Block) {
    # A reader that has the file open without delete sharing makes one rewrite fail with "access denied" until it lets go. Retry against a
    # TIME budget (SwitchRetrySeconds, short jittered pauses) rather than a fixed attempt count: a busy or slow host (CI run 44251460414)
    # keeps the handle open longer than ~1 s, and a passing reader must never end a 24 h window. Only a reader that holds the file past
    # the whole budget fails the rewrite.
    # The block written is the block due at the moment of EACH attempt: a retry loop that outlasts a block edge must not land an obsolete
    # block (a B written while an A is due would enable count 2 in the wrong arm); the caller reads back what was really applied through
    # $script:appliedBlock / appliedState / appliedCount (adversary r5 A8).
    $lastError = $null
    $giveUp = [DateTime]::UtcNow.AddSeconds($SwitchRetrySeconds)
    $jitter = New-Object System.Random
    while ($true) {
        if ($State -cne 'REVERTED' -and $Block -ge 0 -and $script:blockSec -gt 0) {
            $dueNow = [int][Math]::Floor(([DateTime]::UtcNow - $script:windowStart).TotalSeconds / $script:blockSec)
            if ($dueNow -gt $Block) { $Block = $dueNow; $State = if ($Block % 2 -eq 0) { 'A' } else { 'B' }; $Count = if ($State -ceq 'B') { 2 } else { 1 } }
        }
        $script:appliedBlock = $Block; $script:appliedState = $State; $script:appliedCount = $Count
        $script:switchAttemptStart = [DateTime]::UtcNow    # the start of the attempt that finally lands is the block's real start (adversary r4 A1)
        try { Write-AtomicText $SwitchPath (New-SwitchText $State $Count $Block); return } catch { $lastError = $_ }
        if ([DateTime]::UtcNow -ge $giveUp) { break }
        Start-Sleep -Milliseconds (10 + $jitter.Next(0, 30))
    }
    throw $lastError
}

function Get-FullPathSafe([string]$p) { try { return (Resolve-HeavySlotAbRealPath $p) } catch { return $p } }
function Test-PathInside([string]$Child, [string]$Parent) {
    # $true when $Child is $Parent or lies under it (case-insensitive, junctions / symlinks / short names resolved to the real path first,
    # so an alias into a read-only input cannot slip past the containment check; adversary r3 A1).
    $c = (Get-FullPathSafe $Child).TrimEnd('\', '/')
    $p = (Get-FullPathSafe $Parent).TrimEnd('\', '/')
    return ($c -ieq $p) -or $c.StartsWith($p + '\', [StringComparison]::OrdinalIgnoreCase)
}

# ---- ledger / receipt scanners (state lives in script scope so the end-of-window sweep continues where the last poll stopped) --------
$script:ledgerPath = ''
$script:ledgerOffset = 0
$script:receiptDir = ''
$script:knownReceipts = $null
$script:receiptTries = @{}

$script:ledgerFailed = $false
$script:receiptStuck = @()
$script:finalSweep = $false
$script:heldUnreadable = 0
$script:ledgerExisted = $false
$script:receiptDirExisted = $false
function Get-LedgerBaselineOffset {
    # The ledger length at the baseline, moved back to just after the last newline: a record still being written at that instant starts
    # before the offset and ends after it, so its tail would otherwise read as a complete line that does not parse (adversary r5 A4).
    $bfs = [IO.File]::Open($script:ledgerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
    try {
        $len = $bfs.Length
        $n = [int][Math]::Min($len, 65536)
        if ($n -le 0) { return 0 }
        $buf = New-Object byte[] $n
        [void]$bfs.Seek($len - $n, [IO.SeekOrigin]::Begin)
        $got = 0
        while ($got -lt $n) { $rd = $bfs.Read($buf, $got, $n - $got); if ($rd -le 0) { break }; $got += $rd }
        for ($bi = $got - 1; $bi -ge 0; $bi--) { if ($buf[$bi] -eq 10) { return ($len - $n + $bi + 1) } }
        if ($n -eq $len) { return 0 }     # the whole file was scanned and holds no newline: it is one record still being written
        return $len                        # no newline in the last 64 KB of a bigger file: stay at the end
    }
    finally { $bfs.Dispose() }
}
function Get-LedgerFinding {
    # The first ledger row since window start that is an `unguarded` event or an admit with class != shared / wholeMachine; $null if none.
    # FAIL CLOSED: a read that fails sets $script:ledgerFailed and leaves the offset where it was, so nothing is lost and the caller counts
    # the failure (two in a row, or a failed final sweep, end the window: "no finding" is only ever said about a ledger that was READ).
    $script:ledgerFailed = $false
    # a ledger that existed at the baseline and is now GONE (renamed, deleted) is an unread source, not an empty one (adversary r4 A2)
    if (-not (Test-Path -LiteralPath $script:ledgerPath)) { if ($script:ledgerExisted) { $script:ledgerFailed = $true }; return $null }
    try {
        $lfs = [IO.File]::Open($script:ledgerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        try {
            if ($lfs.Length -lt $script:ledgerOffset) { $script:ledgerOffset = 0 }
            [void]$lfs.Seek($script:ledgerOffset, [IO.SeekOrigin]::Begin)
            $lb = New-Object byte[] ([int]($lfs.Length - $script:ledgerOffset))
            $lg = 0
            while ($lg -lt $lb.Length) { $k = $lfs.Read($lb, $lg, $lb.Length - $lg); if ($k -le 0) { break }; $lg += $k }
        }
        finally { $lfs.Dispose() }
        $txt = [Text.Encoding]::UTF8.GetString($lb, 0, $lg)
        $cut = $txt.LastIndexOf("`n")
        $body = if ($cut -ge 0) { $txt.Substring(0, $cut + 1) } else { '' }
        $tail = $txt.Substring($body.Length)     # the unterminated last record, if any: normally a write still in flight
        $lines = New-Object System.Collections.Generic.List[string]
        foreach ($l0 in ($body -split "`n")) { $lines.Add($l0) }
        if ($script:finalSweep -and $tail.Trim()) {
            # The window is over, so nothing more will complete that record: an unterminated tail that parses is READ (an `unguarded` row
            # with no trailing newline must still end the window), one that does not parse is an unread source (adversary r4 A3).
            $probe = $null
            try { $probe = $tail | ConvertFrom-Json } catch { $probe = $null }
            if ($null -eq $probe) { $script:ledgerFailed = $true; return $null }
            $lines.Add($tail)
            $body = $txt
        }
        $script:ledgerOffset += $utf8.GetByteCount($body)
        foreach ($line in $lines) {
            if (-not $line.Trim()) { continue }
            # A COMPLETE (newline-terminated) line that does not parse is an unread event, and its offset is already past it: it can never be
            # revisited, so it ends the window now rather than being skipped for a clean DONE (adversary r5 A4).
            $e = $null
            try { $e = $line | ConvertFrom-Json } catch { $e = $null }
            if ($null -eq $e -or $e -isnot [pscustomobject]) { return ('ledger-unreadable (a complete ledger line does not parse: {0})' -f $line.Substring(0, [Math]::Min(60, $line.Length))) }
            $at = ConvertTo-UtcInstant $e.at
            if ($null -eq $at -or $at -lt $script:windowStart) { continue }
            if ([string]$e.event -eq 'unguarded') { return "unguarded ledger event ticket=$($e.ticket)" }
            if ([string]$e.event -eq 'admit' -and ([string]$e.class -cne 'shared' -or $e.wholeMachine -eq $true)) { return "exclusive-admit ticket=$($e.ticket) class=$($e.class)" }
        }
    }
    catch { $script:ledgerFailed = $true }
    return $null
}

function Get-ReceiptFinding {
    # A NEW receipt file (not in the start-of-window baseline), whatever time its name carries, read until it parses: wholeMachine true or
    # class != shared. A name is marked seen only after it parsed. One that still does not parse after ReceiptGiveUpReads reads (or at
    # all, in the final sweep) is never skipped: its name goes to $script:receiptStuck and the caller ends the window (receipt-unreadable).
    $script:receiptStuck = @()
    $stuck = New-Object System.Collections.Generic.List[string]
    if (-not (Test-Path -LiteralPath $script:receiptDir -PathType Container)) {
        if ($script:receiptDirExisted) { $script:receiptStuck = @('(the receipts directory that existed at the window start is gone)') }
        return $null
    }
    foreach ($rf in [IO.Directory]::GetFiles($script:receiptDir, '*.json')) {
        $nm = [IO.Path]::GetFileName($rf)
        if ($script:knownReceipts.Contains($nm)) { continue }
        $rc = $null
        try { $rc = Get-Content -LiteralPath $rf -Raw | ConvertFrom-Json } catch { $rc = $null }
        if ($null -eq $rc) {
            $script:receiptTries[$nm] = [int]$script:receiptTries[$nm] + 1
            if ($script:receiptTries[$nm] -ge $ReceiptGiveUpReads -or $script:finalSweep) { $stuck.Add($nm) }
            continue
        }
        [void]$script:knownReceipts.Add($nm)
        if ($rc.wholeMachine -eq $true) { return "exclusive-admit receipt=$nm wholeMachine=true" }
        # fail closed: a receipt whose class is missing or empty is not "shared" (adversary r4 A5)
        if ([string]$rc.class -cne 'shared') { return "exclusive-admit receipt=$nm class=$($rc.class)" }
    }
    $script:receiptStuck = $stuck.ToArray()
    return $null
}

function Get-StuckReason {
    # The reason the last ledger / receipt scan leaves behind when it could not READ something, or $null.
    if ($script:receiptStuck.Count -gt 0) { return ('receipt-unreadable (cannot parse: {0})' -f (($script:receiptStuck | Select-Object -First 3) -join ', ')) }
    return $null
}

function Invoke-FinalSweep {
    # The last look after the final poll: the ledger and the receipts are each read again until they read, up to 4 tries 250 ms apart. A
    # finding wins; a source that STILL cannot be read is itself a finding (never a clean DONE built on an unread source).
    $script:finalSweep = $true
    try {
        $f = $null
        for ($try = 1; $try -le 4; $try++) {
            $f = Get-LedgerFinding
            if ($f -or -not $script:ledgerFailed) { break }
            if ($try -lt 4) { Start-Sleep -Milliseconds 250 }
        }
        if ($f) { return $f }
        if ($script:ledgerFailed) { return 'ledger-unreadable (the final sweep could not read the ledger in 4 tries)' }
        for ($try = 1; $try -le 4; $try++) {
            $f = Get-ReceiptFinding
            if ($f -or $script:receiptStuck.Count -eq 0) { break }
            if ($try -lt 4) { Start-Sleep -Milliseconds 250 }
        }
        if ($f) { return $f }
        return (Get-StuckReason)
    }
    finally { $script:finalSweep = $false }
}

# ---- preflight: nothing in this part writes anything (a refusal leaves at most the empty <switch>.lock file) -----------------
$createdNew = $false
$mutex = $null
try { $mutex = [Threading.Mutex]::new($true, $MutexName, [ref]$createdNew) } catch { Write-Refused "mutex-unavailable ($($_.Exception.GetType().Name))" }
if (-not $createdNew) { Write-Refused 'driver-running' }

# The files this driver WRITES (the switch and its lock / temp names, the window dir) must never be, or lie under, anything it only READS
# (adversary r2 A7): a SwitchPath pointed into the HeavySlot dir would overwrite heavy-slot itself.
$readOnlyInputs = @($HeavySlotDir, $FastSuitesPath, $CoreTempFeed, $MemoryProbeFile, $EventProbeFile, $DiskProbeFile) | Where-Object { $_ }
foreach ($ro in $readOnlyInputs) {
    foreach ($w in @($SwitchPath, ($SwitchPath + '.lock'), ($SwitchPath + '.tmp'), $WindowDir)) {
        if (Test-PathInside $w $ro) { Write-Refused "path-overlap ($w is, or lies under, the read-only input $ro)" }
    }
}
if (Test-PathInside $SwitchPath $WindowDir) { Write-Refused "path-overlap (the switch $SwitchPath lies under the window dir $WindowDir)" }

# ONE live driver per SWITCH, whatever -MutexName says: an exclusive lock file beside the switch is held for the life of the process
# (adversary r2 A10). It is never removed; a refusal leaves it as an empty file.
$script:switchLock = $null
$script:windowLock = $null
try {
    $lockDir = Split-Path -Parent $SwitchPath
    if ($lockDir) { New-Item -ItemType Directory -Path $lockDir -Force | Out-Null }
    $script:switchLock = [IO.File]::Open(($SwitchPath + '.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}
catch { Write-Refused "driver-running (the switch lock $SwitchPath.lock is held by another driver, or cannot be taken: $($_.Exception.GetType().Name))" }

$self = Get-Process -Id $PID
$script:selfTicks = $self.StartTime.ToUniversalTime().Ticks
$script:windowStart = [DateTime]::UtcNow
$script:windowEnd = $script:windowStart.AddHours($Hours)
$final = $null
$switchTouched = $false
$blockIndex = -1
$nSamples = 0
try {
    try {
        # An existing switch file is reverted first (a rewrite, never a removal). Only this driver, owner of mutex and lock, may do it.
        if (Test-Path -LiteralPath $SwitchPath -PathType Leaf) {
            try { Set-Switch 'REVERTED' 1 -1 } catch { Write-Refused "switch-revert-failed ($($_.Exception.GetType().Name))" }
        }
        $pinActual = 'missing'
        $hsCli = Join-Path (Join-Path $HeavySlotDir 'bin') 'heavy-slot.ps1'
        if (Test-Path -LiteralPath $hsCli -PathType Leaf) { $pinActual = (Get-FileHash -LiteralPath $hsCli -Algorithm SHA256).Hash.ToLowerInvariant() }
        if ($pinActual -ne $PinSha.Trim().ToLowerInvariant()) { Write-Refused "heavy-slot-sha-mismatch actual=$pinActual" }
        $fsText = if (Test-Path -LiteralPath $FastSuitesPath -PathType Leaf) { [IO.File]::ReadAllText($FastSuitesPath) } else { '' }
        if ($fsText -notmatch '(?m)^[^\r\n]*HEAVY_SLOT_AB_READER v1') { Write-Refused 'fast-suites-reader-marker-missing' }
        $feed0 = @(Read-Feed)
        if ($feed0.Count -eq 0 -and $script:feedFuture -gt 0) { Write-Refused "feed-future ($($script:feedFuture) row(s) dated later than now + ${FeedFutureSkewSeconds}s and none valid)" }
        if ($feed0.Count -eq 0) { Write-Refused 'feed-unreadable' }
        $feedAge0 = ([DateTime]::UtcNow - $feed0[-1].utc).TotalSeconds
        if ($feedAge0 -gt $PreflightFeedSeconds) { Write-Refused ('feed-stale age={0:N0}s' -f $feedAge0) }
        $mem0 = Get-MemoryProbe
        if ($null -eq $mem0.commit -or $mem0.commit -lt $PreflightCommitGB) { Write-Refused "low-commit free=$($mem0.commit)GB" }
        $disk0 = Get-DiskProbe
        if ($null -eq $disk0 -or $disk0 -lt $MinFreeDiskGiB) { Write-Refused "low-disk free=${disk0}GiB" }
        # One directory holds ONE window: the three streams are append-only, so a directory that already holds one would blend two windows
        # into one evaluation (adversary r1 A1).
        # The evidence the window is judged on must EXIST at the start: a HeavySlot with no ledger or no receipts directory yet has nothing
        # to read, and "nothing found" about a source that was never there would let a window end DONE on no evidence (adversary r5 A3).
        if (-not (Test-Path -LiteralPath (Join-Path $HeavySlotDir 'ledger.jsonl') -PathType Leaf)) { Write-Refused "ledger-missing (no ledger.jsonl in ${HeavySlotDir}: there is no evidence stream to read)" }
        if (-not (Test-Path -LiteralPath (Join-Path $HeavySlotDir 'receipts') -PathType Container)) { Write-Refused "receipts-dir-missing (no receipts directory in ${HeavySlotDir}: there is no evidence stream to read)" }
        # Every name this driver writes under the window dir, its temp name included, must be ABSENT (a dangling or live symlink counts):
        # a planted ab-receipt.json.tmp -> CoreTempFeed would be followed by the receipt write (adversary r5 A2).
        foreach ($leftover in 'ab-blocks.jsonl', 'ab-samples.jsonl', 'ab-receipt.json', 'ab-receipt.json.tmp', 'ab-window.lock') {
            $lp = Join-Path $WindowDir $leftover
            if ([IO.File]::Exists($lp) -or [IO.Directory]::Exists($lp) -or ($null -ne (Get-Item -LiteralPath $lp -Force -ErrorAction SilentlyContinue))) { Write-Refused "window-dir-used ($leftover already exists in $WindowDir; give each window its own directory)" }
        }
        # CLAIM the directory atomically (adversary r3 A2): the check above and the first write are not one step, so two drivers (distinct
        # switches, distinct mutex names) could both pass it. An exclusive CreateNew of ab-window.lock lets exactly one win; it is held for
        # the life of the process and never removed, so it also marks the directory used for ever.
        if ($WindowClaimDelaySeconds -gt 0) { Start-Sleep -Milliseconds ([int]($WindowClaimDelaySeconds * 1000)) }
        New-Item -ItemType Directory -Path $WindowDir -Force | Out-Null
        try { $script:windowLock = [IO.File]::Open((Join-Path $WindowDir 'ab-window.lock'), [IO.FileMode]::CreateNew, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch { Write-Refused "window-dir-used (ab-window.lock in $WindowDir was claimed by another driver first, or cannot be created: $($_.Exception.GetType().Name))" }
    }
    catch {
        # any preflight read that fails (a locked file, a bad path) is a refusal, never an abort of a window that has not started (adversary r2 A5)
        Write-Refused ("preflight-failed ($($_.Exception.GetType().Name)): $($_.Exception.Message)")
    }

    # ---- window: the ledger / receipt BASELINES are taken first, then the clock starts, so nothing appended in between can be missed
    # (adversary r2 A3; events older than the start are still filtered out by their own timestamp) ---------------------------------------
    $script:ledgerPath = Join-Path $HeavySlotDir 'ledger.jsonl'
    $script:ledgerExisted = [bool](Test-Path -LiteralPath $script:ledgerPath)
    $script:ledgerOffset = 0
    if ($script:ledgerExisted) {
        # a ledger that cannot be opened for the baseline keeps the plain length: the polls then fail to read it and end the window (ledger-unreadable)
        try { $script:ledgerOffset = Get-LedgerBaselineOffset } catch { $script:ledgerOffset = (Get-Item -LiteralPath $script:ledgerPath).Length }
    }
    $script:receiptDir = Join-Path $HeavySlotDir 'receipts'
    $script:receiptDirExisted = [bool](Test-Path -LiteralPath $script:receiptDir -PathType Container)
    $script:knownReceipts = New-Object 'System.Collections.Generic.HashSet[string]'
    if (Test-Path -LiteralPath $script:receiptDir -PathType Container) { foreach ($rf0 in [IO.Directory]::GetFiles($script:receiptDir, '*.json')) { [void]$script:knownReceipts.Add([IO.Path]::GetFileName($rf0)) } }
    $script:windowStart = [DateTime]::UtcNow
    $script:windowEnd = $script:windowStart.AddHours($Hours)
    $blockLen = [TimeSpan]::FromMinutes($BlockMinutes)
    $script:blockSec = $blockLen.TotalSeconds
    $blockState = ''
    $lastRowUtc = $script:windowStart
    $rowWin = New-Object System.Collections.Generic.List[object]
    $prevRowUtc = $null
    $prevHolders = $null
    $badMem = 0
    $badEvent = 0
    $badHeld = 0
    $badLedger = 0
    $nextEventCheck = $script:windowStart
    Write-Output ("AB_DRIVER start={0} end={1} hours={2} blockMinutes={3} pollSeconds={4} switch={5}" -f (Get-IsoUtc $script:windowStart), (Get-IsoUtc $script:windowEnd), $Hours, $BlockMinutes, $PollSeconds, $SwitchPath)

    while ($true) {
        $now = [DateTime]::UtcNow
        if ($now -ge $script:windowEnd) { break }
        # The block that is due NOW (by the clock, not "the next one"): a slow poll skips whole blocks instead of running a schedule that
        # lags the state actually applied (adversary r2 A2). The row's startUtc is the moment the state was really applied.
        $due = [int][Math]::Floor(($now - $script:windowStart).TotalSeconds / $blockLen.TotalSeconds)
        if ($due -gt $blockIndex) {
            $blockIndex = $due
            $blockState = if ($blockIndex % 2 -eq 0) { 'A' } else { 'B' }
            $cnt = if ($blockState -eq 'B') { 2 } else { 1 }
            # startUtc is taken BEFORE the write (adversary r3 A10): a reader can see the new state from the instant the file is replaced, so a
            # driver that is descheduled after the write must not log a start later than the state a ticket actually read. The row also
            # carries switchWrittenUtc, the moment the write returned.
            Set-Switch $blockState $cnt $blockIndex
            $blockIndex = $script:appliedBlock; $blockState = $script:appliedState; $cnt = $script:appliedCount    # what the landing attempt really wrote
            $startedAt = $script:switchAttemptStart   # taken just before the attempt that landed, not before a retry loop that may have run for seconds
            $writtenAt = [DateTime]::UtcNow
            $switchTouched = $true
            if ($PostSwitchDelaySeconds -gt 0) { Start-Sleep -Milliseconds ([int]($PostSwitchDelaySeconds * 1000)) }   # test seam: descheduled right after the write
            $schedStart = $script:windowStart.AddTicks($blockLen.Ticks * $blockIndex)
            $blockEnd = $schedStart.Add($blockLen); if ($blockEnd -gt $script:windowEnd) { $blockEnd = $script:windowEnd }
            Add-JsonLine $blocksFile ([ordered]@{ utc = (Get-IsoUtc $writtenAt); block = $blockIndex; state = $blockState; count = $cnt; startUtc = (Get-IsoUtc $startedAt); switchWrittenUtc = (Get-IsoUtc $writtenAt); scheduledStartUtc = (Get-IsoUtc $schedStart); endUtc = (Get-IsoUtc $blockEnd); windowStartUtc = (Get-IsoUtc $script:windowStart); windowEndUtc = (Get-IsoUtc $script:windowEnd) })
            Write-Output ("AB_BLOCK block={0} state={1} count={2} utc={3}" -f $blockIndex, $blockState, $cnt, (Get-IsoUtc $writtenAt))
        }

        # ---- sample ----------------------------------------------------------------------------------------------------------
        if ($ProbeDelaySeconds -gt 0) { Start-Sleep -Milliseconds ([int]($ProbeDelaySeconds * 1000)) }   # test seam: slow probes
        $now = [DateTime]::UtcNow
        $holders = @(Get-LiveHolders)
        if ($script:heldUnreadable -gt 0) { $badHeld++ } else { $badHeld = 0 }
        $rows = @(Read-Feed)
        $newRows = @($rows | Where-Object { $_.utc -gt $lastRowUtc })
        # A feed row between two polls is only credited with 2+ holders when BOTH bracketing polls saw 2+: the holders are sampled, not
        # continuous, so a second holder seen at alternate polls is not sustained overlap (adversary r1 A7).
        $rowHolders = if ($null -eq $prevHolders) { $holders.Count } else { [Math]::Min([int]$prevHolders, $holders.Count) }
        $regressedInRead = $false
        foreach ($r in $newRows) {
            # $prevRowUtc / $lastRowUtc are the NEWEST row time seen so far, never the time of the last row read: a row older than that
            # (a valid cool row at t+25 followed by a hot row at t) is a regression in the feed itself and must not replace the watermark
            # or silently get dt=0 (adversary r5 A1).
            if ($null -ne $prevRowUtc -and $r.utc -lt $prevRowUtc.AddSeconds(-2)) { $regressedInRead = $true }
            $dt = if ($null -ne $prevRowUtc) { [Math]::Min(6.0, [Math]::Max(0.0, ($r.utc - $prevRowUtc).TotalSeconds)) } else { 2.0 }
            $rowWin.Add([pscustomobject]@{ utc = $r.utc; dt = $dt; temp = $r.temp; holders = $rowHolders })
            if ($null -eq $prevRowUtc -or $r.utc -gt $prevRowUtc) { $prevRowUtc = $r.utc; $lastRowUtc = $r.utc }
        }
        $prevHolders = $holders.Count
        $keepFrom = $now.AddSeconds(-[Math]::Max($CapWindowSeconds, $WarmWindowSeconds) - 5)
        while ($rowWin.Count -gt 0 -and $rowWin[0].utc -lt $keepFrom) { $rowWin.RemoveAt(0) }
        $newest = if ($rows.Count -gt 0) { $rows[-1] } else { $null }
        # The feed's own clock must not run backwards: a newest row older than the newest row already ingested means the host clock was
        # stepped back (or the feed was rewritten), and the watermark would then hide every correctly dated row (adversary r4 A7). End closed.
        $clockBack = ($regressedInRead -or ($null -ne $newest -and $null -ne $prevRowUtc -and $newest.utc -lt $prevRowUtc.AddSeconds(-2)))
        $feedAge = if ($newest) { [Math]::Max(0.0, ($now - $newest.utc).TotalSeconds) } else { [double]::PositiveInfinity }
        $mem = Get-MemoryProbe
        $disk = Get-DiskProbe
        $perf = Get-PerfProbe
        $tempMax = $null
        if ($newRows.Count -gt 0) { $tempMax = ($newRows | Measure-Object -Property temp -Maximum).Maximum }
        $sample = [ordered]@{
            utc = (Get-IsoUtc $now); block = $blockIndex; state = $blockState; holders = $holders.Count
            labels = @($holders | ForEach-Object { $_.label }); slots = @($holders | ForEach-Object { ($_.slots -join '+') })
            temp_c = $(if ($newest) { $newest.temp } else { $null }); temp_max_c = $tempMax; cpu_pct = $(if ($newest) { $newest.cpu } else { $null })
            package_w = $(if ($newest) { $newest.watts } else { $null }); feed_age_s = $(if ([double]::IsInfinity($feedAge)) { $null } else { [Math]::Round($feedAge, 1) })
            free_commit_gb = $mem.commit; free_phys_gb = $mem.phys; perf_pct = $perf.perf; freq_mhz = $perf.freq; c_free_gib = $disk
        }
        Add-JsonLine $samplesFile $sample
        $nSamples++

        # ---- hard aborts -----------------------------------------------------------------------------------------------------
        $why = $null
        if (Test-Path -LiteralPath $stopFile) { $why = 'stop-file' }
        if (-not $why -and $clockBack) { $why = ('feed-time-regressed a feed row is older than one already read (newest row {0}, newest time seen {1}): the host clock stepped back or the feed was rewritten' -f $(if ($newest) { Get-IsoUtc $newest.utc } else { 'n/a' }), (Get-IsoUtc $prevRowUtc)) }
        if (-not $why) {
            $capFrom = $now.AddSeconds(-$CapWindowSeconds)
            $hot = ($rowWin.ToArray() | Where-Object { $_.utc -ge $capFrom -and $_.temp -ge $CapTempC } | Measure-Object -Property dt -Sum).Sum
            if ($hot -ge $CapSeconds) { $why = ('thermal-cap temp>={0}C for {1:N0}s in {2:N0}s' -f $CapTempC, $hot, $CapWindowSeconds) }
        }
        if (-not $why) {
            $warmFrom = $now.AddSeconds(-$WarmWindowSeconds)
            $warm = ($rowWin.ToArray() | Where-Object { $_.utc -ge $warmFrom -and $_.temp -ge $WarmTempC -and $_.holders -ge 2 } | Measure-Object -Property dt -Sum).Sum
            if ($holders.Count -ge 2 -and ($warm / $WarmWindowSeconds) -gt $WarmFraction) { $why = ('warm-while-2 temp>={0}C for {1:N0}s of {2:N0}s with 2+ holders' -f $WarmTempC, $warm, $WarmWindowSeconds) }
        }
        if (-not $why -and $feedAge -gt $FeedAbortSeconds) { $why = ('stale-feed age={0}s' -f $(if ([double]::IsInfinity($feedAge)) { 'unreadable' } else { [Math]::Round($feedAge) })) }
        if (-not $why) {
            if ($null -eq $mem.commit -or $null -eq $mem.phys -or $mem.commit -lt $MinFreeCommitGB -or $mem.phys -lt $MinFreePhysGB) { $badMem++ } else { $badMem = 0 }
            if ($badMem -ge 2) { $why = "low-memory commit=$($mem.commit)GB phys=$($mem.phys)GB on 2 consecutive samples" }
        }
        if (-not $why -and $now -ge $nextEventCheck) {
            $nextEventCheck = $now.AddSeconds($(if ($EventProbeFile) { 0 } else { $EventCheckSeconds }))
            $n = Get-EventCount $script:windowStart
            if ($null -eq $n) { $badEvent++ } else { $badEvent = 0 }
            if ($null -ne $n -and $n -gt 0) { $why = "event-2004 rows=$n since window start" }
            elseif ($badEvent -ge 2) { $why = 'event-2004-unreadable the System event count could not be read on 2 consecutive checks' }
        }
        if (-not $why -and ($null -eq $disk -or $disk -lt $MinFreeDiskGiB)) { $why = "low-disk C:=${disk}GiB" }
        if (-not $why) {
            if (@($holders | Where-Object { $_.whole -or ($_.cls -cne 'shared') }).Count -gt 0) { $why = 'exclusive-admit (a live holder is class != shared or wholeMachine)' }
        }
        # FAIL CLOSED on every evidence source (adversary r3 A3): "no finding" is only said about a source that was READ. Two failed reads of
        # the ledger in a row, an unparseable held record on two polls in a row, or a receipt that never parses all end the window.
        if (-not $why -and $badHeld -ge 2) { $why = 'held-unreadable (a held\*.json record could not be parsed on 2 consecutive polls)' }
        if (-not $why) {
            $why = Get-LedgerFinding
            if (-not $why) {
                if ($script:ledgerFailed) { $badLedger++ } else { $badLedger = 0 }
                if ($badLedger -ge 2) { $why = 'ledger-unreadable (the ledger could not be read on 2 consecutive polls)' }
            }
        }
        if (-not $why) { $why = Get-ReceiptFinding; if (-not $why) { $why = Get-StuckReason } }
        if ($why) { $final = @{ state = 'ABORTED'; reason = $why }; break }

        # sleep to the next poll, never past the next block edge or the window end
        $sleepMs = $PollSeconds * 1000.0
        $nextEdge = $script:windowStart.AddTicks($blockLen.Ticks * ($blockIndex + 1))
        $toEdge = (($nextEdge - [DateTime]::UtcNow).TotalMilliseconds)
        $toEnd = (($script:windowEnd - [DateTime]::UtcNow).TotalMilliseconds)
        $sleepMs = [Math]::Min($sleepMs, [Math]::Min($toEdge, $toEnd))
        if ($sleepMs -gt 0) { Start-Sleep -Milliseconds ([int][Math]::Ceiling($sleepMs)) }
    }
    if (-not $final) {
        # The window ran out: one last sweep of the ledger and the receipts, so an unguarded event or an exclusive admit that landed after
        # the final poll still ends the window as ABORTED (adversary r2 A3).
        $late = Invoke-FinalSweep
        $final = if ($late) { @{ state = 'ABORTED'; reason = $late } } else { @{ state = 'DONE'; reason = 'window-complete' } }
    }
}
catch {
    if (-not $final) { $final = @{ state = 'ABORTED'; reason = ('driver-exception: ' + $_.Exception.Message) } }
}
finally {
    # Every exit path: the switch goes back to count 1 first (an atomic rewrite, 5 tries, then read back to PROVE it), then the receipt is
    # written, then the mutex and the switch lock are released. A revert that cannot be proven is recorded as REVERT_FAILED and exits 4 on
    # EVERY path, a refusal included (an `exit` in a finally replaces the pending exit code); the reader still answers 1 once this
    # process is gone, because the driver pid it checks no longer exists.
    $revertFailed = $false
    $revertWhy = ''
    $receiptFailed = $false
    $receiptWritten = $false
    if ($switchTouched -or (Test-Path -LiteralPath $SwitchPath -PathType Leaf)) {
        $revertFailed = $true
        for ($tryNo = 1; $tryNo -le 5 -and $revertFailed; $tryNo++) {
            try {
                Set-Switch 'REVERTED' 1 $(if ($blockIndex -ge 0) { $blockIndex } else { -1 })
                $back = Get-Content -LiteralPath $SwitchPath -Raw | ConvertFrom-Json
                if ([string]$back.state -ceq 'REVERTED' -and [long]$back.count -eq 1) { $revertFailed = $false } else { $revertWhy = 'the read-back did not show REVERTED / count 1' }
            }
            catch { $revertWhy = $_.Exception.Message }
            if ($revertFailed -and $tryNo -lt 5) { Start-Sleep -Milliseconds 200 }
        }
        if ($revertFailed) { Write-Output "AB_REVERT_FAILED $revertWhy" }
    }
    if ($script:refused) { $final = $null }
    elseif (-not $final) { $final = @{ state = 'ABORTED'; reason = 'driver-interrupted' } }
    if ($final -and $revertFailed -and -not $script:refused) { $final = @{ state = 'ABORTED'; reason = ('revert-failed: ' + $revertWhy + ' (the window ended as ' + $final.state + ': ' + $final.reason + ')') } }
    if ($final -and (Test-Path -LiteralPath $WindowDir -PathType Container) -and $switchTouched) {
        $rcpt = [ordered]@{
            state = $final.state; reason = $final.reason; schema = 'heavy-slot-ab-receipt.v1'
            windowStartUtc = (Get-IsoUtc $script:windowStart); windowEndUtc = (Get-IsoUtc $script:windowEnd); endedUtc = (Get-IsoUtc ([DateTime]::UtcNow))
            hours = $Hours; blockMinutes = $BlockMinutes; pollSeconds = $PollSeconds; blocks = ($blockIndex + 1); samples = $nSamples
            driverPid = $PID; switchPath = $SwitchPath; switchFinal = $(if ($revertFailed) { 'REVERT_FAILED' } else { 'REVERTED' })
        }
        # A receipt that cannot be persisted (3 tries) is a failed window, never a DONE: the evaluator reads the receipt, so a window with no
        # receipt must not print AB_DONE or exit 0 (adversary r3 A9). The switch is already reverted above; exit 5 says "no receipt".
        $receiptText = (($rcpt | ConvertTo-Json -Compress) + "`n")
        $receiptWhy = ''
        for ($rtry = 1; $rtry -le 3 -and -not $receiptWritten; $rtry++) {
            try { Write-AtomicText $receiptFile $receiptText; $receiptWritten = $true }
            catch { $receiptWhy = $_.Exception.Message; if ($rtry -lt 3) { Start-Sleep -Milliseconds 200 } }
        }
        if (-not $receiptWritten) { $receiptFailed = $true; Write-Output "AB_RECEIPT_FAILED $receiptWhy" }
    }
    if ($final -and -not $receiptFailed) { Write-Output ("AB_{0} reason={1}" -f $(if ($final.state -eq 'DONE') { 'DONE' } else { 'ABORT' }), $final.reason) }
    elseif ($final -and $final.state -ne 'DONE') { Write-Output ("AB_ABORT reason={0}" -f $final.reason) }
    if ($mutex) { try { $mutex.ReleaseMutex() } catch { }; $mutex.Dispose() }
    if ($script:switchLock) { $script:switchLock.Dispose() }
    if ($script:windowLock) { $script:windowLock.Dispose() }
    if ($revertFailed) { exit 4 }
    if ($receiptFailed) { exit 5 }
}
if ($final -and $final.state -eq 'ABORTED') { exit 3 }
exit 0
