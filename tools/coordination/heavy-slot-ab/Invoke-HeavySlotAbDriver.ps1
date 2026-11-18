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
    [double]$EventCheckSeconds = 300
)

$ErrorActionPreference = 'Stop'
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
    $tmp = '{0}.tmp-{1}' -f $Path, $PID
    [IO.File]::WriteAllText($tmp, $Text, $utf8)
    [IO.File]::Move($tmp, $Path, $true)
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
function Read-Feed {
    # The CoreTemp feed rows newest-last: @{ utc; cpu; temp; watts }. Row times are LOCAL time (dd.MM.yyyy HH:mm:ss.fff).
    # Opened shared so the writer is never blocked; only the tail is read. An unreadable feed returns @().
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
            $temp = 0.0
            if (-not [double]::TryParse($f[$iTemp], [Globalization.NumberStyles]::Float, $inv, [ref]$temp)) { continue }
            $cpu = $null; $w = $null
            $x = 0.0
            if ($iCpu -ge 0 -and $f.Count -gt $iCpu -and [double]::TryParse($f[$iCpu], [Globalization.NumberStyles]::Float, $inv, [ref]$x)) { $cpu = $x }
            if ($iW -ge 0 -and $f.Count -gt $iW -and [double]::TryParse($f[$iW], [Globalization.NumberStyles]::Float, $inv, [ref]$x)) { $w = $x }
            $rows.Add([pscustomobject]@{ utc = [TimeZoneInfo]::ConvertTimeToUtc($dt, [TimeZoneInfo]::Local); cpu = $cpu; temp = $temp; watts = $w })
        }
        return $rows.ToArray()
    }
    catch { return @() }
}

function Get-MemoryProbe {
    # @{ commit; phys } in GB, or $null per field when unreadable.
    if ($MemoryProbeFile) {
        try { $m = Get-Content -LiteralPath $MemoryProbeFile -Raw | ConvertFrom-Json; return [pscustomobject]@{ commit = [double]$m.freeCommitGB; phys = [double]$m.freePhysGB } }
        catch { return [pscustomobject]@{ commit = $null; phys = $null } }
    }
    try {
        $os = Get-CimInstance -ClassName Win32_OperatingSystem -Property FreePhysicalMemory, FreeVirtualMemory -OperationTimeoutSec 10 -ErrorAction Stop
        return [pscustomobject]@{ commit = [Math]::Round($os.FreeVirtualMemory / 1MB, 2); phys = [Math]::Round($os.FreePhysicalMemory / 1MB, 2) }
    }
    catch { return [pscustomobject]@{ commit = $null; phys = $null } }
}

function Get-DiskProbe {
    if ($DiskProbeFile) {
        try { return [double]((Get-Content -LiteralPath $DiskProbeFile -Raw | ConvertFrom-Json).freeGiB) } catch { return $null }
    }
    try { return [Math]::Round([IO.DriveInfo]::new('C').AvailableFreeSpace / 1GB, 2) } catch { return $null }
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
    $out = New-Object System.Collections.Generic.List[object]
    $dir = Join-Path $HeavySlotDir 'held'
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) { return @() }
    foreach ($f in [IO.Directory]::GetFiles($dir, '*.json')) {
        try {
            $h = Get-Content -LiteralPath $f -Raw | ConvertFrom-Json
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
$script:windowStart = [DateTime]::MinValue
$script:windowEnd = [DateTime]::MinValue
$script:selfTicks = 0
function Set-Switch([string]$State, [int]$Count, [int]$Block) {
    $o = [ordered]@{
        schema = 'heavy-slot-ab.v1'; count = $Count; state = $State
        windowStartUtc = (Get-IsoUtc $script:windowStart); endUtc = (Get-IsoUtc $script:windowEnd)
        driverPid = $PID; driverStartTicks = $script:selfTicks; block = $Block; updatedUtc = (Get-IsoUtc ([DateTime]::UtcNow))
    }
    # A reader that has the file open without delete sharing makes one rewrite fail with "access denied" for a moment: retry for ~1 s
    # before giving up, so a passing reader never ends a 24 h window.
    $text = ($o | ConvertTo-Json -Compress) + "`n"
    $lastError = $null
    for ($attempt = 1; $attempt -le 10; $attempt++) {
        try { Write-AtomicText $SwitchPath $text; return } catch { $lastError = $_; Start-Sleep -Milliseconds 100 }
    }
    throw $lastError
}

# ---- preflight ----------------------------------------------------------------------------------------------------------------
$createdNew = $false
$mutex = $null
try { $mutex = [Threading.Mutex]::new($true, $MutexName, [ref]$createdNew) } catch { Write-Refused "mutex-unavailable ($($_.Exception.GetType().Name))" }
if (-not $createdNew) { Write-Refused 'driver-running' }

$self = Get-Process -Id $PID
$script:selfTicks = $self.StartTime.ToUniversalTime().Ticks
$script:windowStart = [DateTime]::UtcNow
$script:windowEnd = $script:windowStart.AddHours($Hours)
$exit = 0
$final = $null
$switchTouched = $false
try {
    # An existing switch file is reverted first (a rewrite, never a removal). Only this driver, mutex owner, may do it.
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
    if ($feed0.Count -eq 0) { Write-Refused 'feed-unreadable' }
    $feedAge0 = ([DateTime]::UtcNow - $feed0[-1].utc).TotalSeconds
    if ($feedAge0 -gt $PreflightFeedSeconds) { Write-Refused ('feed-stale age={0:N0}s' -f $feedAge0) }
    $mem0 = Get-MemoryProbe
    if ($null -eq $mem0.commit -or $mem0.commit -lt $PreflightCommitGB) { Write-Refused "low-commit free=$($mem0.commit)GB" }
    $disk0 = Get-DiskProbe
    if ($null -eq $disk0 -or $disk0 -lt $MinFreeDiskGiB) { Write-Refused "low-disk free=${disk0}GiB" }
    # One directory holds ONE window: the three streams are append-only, so a directory that already holds one would blend two windows
    # into one evaluation (adversary r1 A1).
    foreach ($leftover in 'ab-blocks.jsonl', 'ab-samples.jsonl', 'ab-receipt.json') {
        if (Test-Path -LiteralPath (Join-Path $WindowDir $leftover)) { Write-Refused "window-dir-used ($leftover already exists in $WindowDir; give each window its own directory)" }
    }
    New-Item -ItemType Directory -Path $WindowDir -Force | Out-Null
    $swDir = Split-Path -Parent $SwitchPath
    if ($swDir) { New-Item -ItemType Directory -Path $swDir -Force | Out-Null }

    # ---- window -------------------------------------------------------------------------------------------------------------
    $script:windowStart = [DateTime]::UtcNow
    $script:windowEnd = $script:windowStart.AddHours($Hours)
    $blockLen = [TimeSpan]::FromMinutes($BlockMinutes)
    $blockIndex = -1
    $blockState = ''
    $nextEdge = $script:windowStart
    $ledgerPath = Join-Path $HeavySlotDir 'ledger.jsonl'
    $ledgerOffset = if (Test-Path -LiteralPath $ledgerPath) { (Get-Item -LiteralPath $ledgerPath).Length } else { 0 }
    $receiptDir = Join-Path $HeavySlotDir 'receipts'
    # Receipts: the names present at window start are the baseline. Any NEW name is read, whatever time its name carries (a ticket that
    # queued before the window still writes its receipt when it finishes inside it, adversary r1 A5), and is retried until it parses
    # (a half-written receipt must not be marked seen, A4); after 200 failed reads of one name it is given up on.
    $knownReceipts = New-Object 'System.Collections.Generic.HashSet[string]'
    $receiptTries = @{}
    if (Test-Path -LiteralPath $receiptDir -PathType Container) { foreach ($rf0 in [IO.Directory]::GetFiles($receiptDir, '*.json')) { [void]$knownReceipts.Add([IO.Path]::GetFileName($rf0)) } }
    $lastRowUtc = $script:windowStart
    $rowWin = New-Object System.Collections.Generic.List[object]
    $prevRowUtc = $null
    $prevHolders = $null
    $badMem = 0
    $badEvent = 0
    $nextEventCheck = $script:windowStart
    $nSamples = 0
    Write-Output ("AB_DRIVER start={0} end={1} hours={2} blockMinutes={3} pollSeconds={4} switch={5}" -f (Get-IsoUtc $script:windowStart), (Get-IsoUtc $script:windowEnd), $Hours, $BlockMinutes, $PollSeconds, $SwitchPath)

    while ($true) {
        $now = [DateTime]::UtcNow
        # block edge: rewrite the switch atomically and log the block
        if ($now -ge $nextEdge -and $now -lt $script:windowEnd) {
            $blockIndex++
            $blockState = if ($blockIndex % 2 -eq 0) { 'A' } else { 'B' }
            $cnt = if ($blockState -eq 'B') { 2 } else { 1 }
            Set-Switch $blockState $cnt $blockIndex
            $switchTouched = $true
            $blockEnd = $nextEdge.Add($blockLen); if ($blockEnd -gt $script:windowEnd) { $blockEnd = $script:windowEnd }
            Add-JsonLine $blocksFile ([ordered]@{ utc = (Get-IsoUtc $now); block = $blockIndex; state = $blockState; count = $cnt; startUtc = (Get-IsoUtc $nextEdge); endUtc = (Get-IsoUtc $blockEnd); windowStartUtc = (Get-IsoUtc $script:windowStart); windowEndUtc = (Get-IsoUtc $script:windowEnd) })
            Write-Output ("AB_BLOCK block={0} state={1} count={2} utc={3}" -f $blockIndex, $blockState, $cnt, (Get-IsoUtc $now))
            $nextEdge = $nextEdge.Add($blockLen)
        }
        if ($now -ge $script:windowEnd) { break }

        # ---- sample ----------------------------------------------------------------------------------------------------------
        $holders = @(Get-LiveHolders)
        $rows = @(Read-Feed)
        $newRows = @($rows | Where-Object { $_.utc -gt $lastRowUtc })
        # A feed row between two polls is only credited with 2+ holders when BOTH bracketing polls saw 2+: the holders are sampled, not
        # continuous, so a second holder seen at alternate polls is not sustained overlap (adversary r1 A7).
        $rowHolders = if ($null -eq $prevHolders) { $holders.Count } else { [Math]::Min([int]$prevHolders, $holders.Count) }
        foreach ($r in $newRows) {
            $dt = if ($null -ne $prevRowUtc) { [Math]::Min(6.0, [Math]::Max(0.0, ($r.utc - $prevRowUtc).TotalSeconds)) } else { 2.0 }
            $rowWin.Add([pscustomobject]@{ utc = $r.utc; dt = $dt; temp = $r.temp; holders = $rowHolders })
            $prevRowUtc = $r.utc
            $lastRowUtc = $r.utc
        }
        $prevHolders = $holders.Count
        $keepFrom = $now.AddSeconds(-[Math]::Max($CapWindowSeconds, $WarmWindowSeconds) - 5)
        while ($rowWin.Count -gt 0 -and $rowWin[0].utc -lt $keepFrom) { $rowWin.RemoveAt(0) }
        $newest = if ($rows.Count -gt 0) { $rows[-1] } else { $null }
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
            if (@($holders | Where-Object { $_.whole -or ($_.cls -and $_.cls -ne 'shared') }).Count -gt 0) { $why = 'exclusive-admit (a live holder is class != shared or wholeMachine)' }
        }
        if (-not $why -and (Test-Path -LiteralPath $ledgerPath)) {
            try {
                $lfs = [IO.File]::Open($ledgerPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
                try {
                    if ($lfs.Length -lt $ledgerOffset) { $ledgerOffset = 0 }
                    [void]$lfs.Seek($ledgerOffset, [IO.SeekOrigin]::Begin)
                    $lb = New-Object byte[] ([int]($lfs.Length - $ledgerOffset))
                    $lg = 0
                    while ($lg -lt $lb.Length) { $k = $lfs.Read($lb, $lg, $lb.Length - $lg); if ($k -le 0) { break }; $lg += $k }
                }
                finally { $lfs.Dispose() }
                $txt = [Text.Encoding]::UTF8.GetString($lb, 0, $lg)
                $cut = $txt.LastIndexOf("`n")
                if ($cut -ge 0) {
                    $ledgerOffset += $utf8.GetByteCount($txt.Substring(0, $cut + 1))
                    foreach ($line in ($txt.Substring(0, $cut) -split "`n")) {
                        if (-not $line.Trim()) { continue }
                        try { $e = $line | ConvertFrom-Json } catch { continue }
                        $at = ConvertTo-UtcInstant $e.at
                        if ($null -eq $at -or $at -lt $script:windowStart) { continue }
                        if ([string]$e.event -eq 'unguarded') { $why = "unguarded ledger event ticket=$($e.ticket)"; break }
                        if ([string]$e.event -eq 'admit' -and ([string]$e.class -ne 'shared' -or $e.wholeMachine -eq $true)) { $why = "exclusive-admit ticket=$($e.ticket) class=$($e.class)"; break }
                    }
                }
            }
            catch { }
        }
        if (-not $why -and (Test-Path -LiteralPath $receiptDir -PathType Container)) {
            foreach ($rf in [IO.Directory]::GetFiles($receiptDir, '*.json')) {
                $nm = [IO.Path]::GetFileName($rf)
                if ($knownReceipts.Contains($nm)) { continue }
                $rc = $null
                try { $rc = Get-Content -LiteralPath $rf -Raw | ConvertFrom-Json } catch { $rc = $null }
                if ($null -eq $rc) {
                    $receiptTries[$nm] = [int]$receiptTries[$nm] + 1
                    if ($receiptTries[$nm] -ge 200) { [void]$knownReceipts.Add($nm) }
                    continue
                }
                [void]$knownReceipts.Add($nm)   # marked seen only after it PARSED
                if ($rc.wholeMachine -eq $true) { $why = "exclusive-admit receipt=$nm wholeMachine=true"; break }
                if ($rc.class -and [string]$rc.class -ne 'shared') { $why = "exclusive-admit receipt=$nm class=$($rc.class)"; break }
            }
        }
        if ($why) { $final = @{ state = 'ABORTED'; reason = $why }; break }

        # sleep to the next poll, never past the next block edge or the window end
        $sleepMs = $PollSeconds * 1000.0
        $toEdge = (($nextEdge - [DateTime]::UtcNow).TotalMilliseconds)
        $toEnd = (($script:windowEnd - [DateTime]::UtcNow).TotalMilliseconds)
        $sleepMs = [Math]::Min($sleepMs, [Math]::Min($toEdge, $toEnd))
        if ($sleepMs -gt 0) { Start-Sleep -Milliseconds ([int][Math]::Ceiling($sleepMs)) }
    }
    if (-not $final) { $final = @{ state = 'DONE'; reason = 'window-complete' } }
}
catch {
    if (-not $final) { $final = @{ state = 'ABORTED'; reason = ('driver-exception: ' + $_.Exception.Message) } }
}
finally {
    # Every exit path: the switch goes back to count 1 first (an atomic rewrite, 5 tries, then read back to PROVE it), then the receipt is
    # written, then the mutex is released. A revert that cannot be proven is recorded as REVERT_FAILED and exits 4, never as REVERTED
    # (adversary r1 A9); the reader still answers 1 once this process is gone, because the driver pid it checks no longer exists.
    $revertFailed = $false
    $revertWhy = ''
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
        try { Write-AtomicText $receiptFile (($rcpt | ConvertTo-Json -Compress) + "`n") } catch { Write-Output "AB_RECEIPT_FAILED $($_.Exception.Message)" }
    }
    if ($final) { Write-Output ("AB_{0} reason={1}" -f $(if ($final.state -eq 'DONE') { 'DONE' } else { 'ABORT' }), $final.reason) }
    if ($mutex) { try { $mutex.ReleaseMutex() } catch { }; $mutex.Dispose() }
}
if ($revertFailed) { exit 4 }
if ($final -and $final.state -eq 'ABORTED') { exit 3 }
exit 0
