# HeavySlotAb.psm1 - the reader of the heavy-slot A/B switch file (HEAVY-SLOT-SECOND-SLOT-MEASURED-1).
#
# The switch file (default C:\mlvtmp\locks\heavy-slot-ab.json) is written ONLY by Invoke-HeavySlotAbDriver.ps1. This module only
# reads it. Get-HeavySlotAbCount answers 2 when, and only when, ALL of these hold; anything else (a missing, unreadable or
# malformed file, an unknown field value, an expired window, a dead driver) answers 1, the machine's normal count:
#   schema == 'heavy-slot-ab.v1'; count == 2; state == 'B'; now < endUtc; endUtc - windowStartUtc <= 25 h;
#   driverPid is alive AND its process start time ticks equal driverStartTicks (Get-Process -Id; never a process census).
# A killed driver therefore turns the switch off at once: its pid is gone, whatever the file still says.
#
# The region between the BEGIN and END markers is copied VERBATIM into the staged hub-tools\fast-suites-for-diff.ps1 patch
# (fast-suites-ab.patch) so that script needs no import from a worktree; a test pins the two copies byte-equal.
# ASCII-only on purpose.

# HEAVY_SLOT_AB_READER v1 BEGIN
function Get-HeavySlotAbStatus {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$SwitchPath, [DateTime]$Now = [DateTime]::UtcNow)
    $fail = { param($why) [pscustomobject]@{ Count = 1; State = ''; Reason = $why } }
    $raw = $null
    for ($try = 0; $try -lt 2 -and $null -eq $raw; $try++) {
        try {
            # opened with delete sharing so this read can never make the driver's atomic rewrite fail
            $fs = [IO.File]::Open($SwitchPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
            try { $raw = (New-Object IO.StreamReader($fs)).ReadToEnd() } finally { $fs.Dispose() }
        }
        catch { if ($try -eq 0 -and (Test-Path -LiteralPath $SwitchPath -PathType Leaf)) { Start-Sleep -Milliseconds 120 } else { return (& $fail "unreadable: $($_.Exception.GetType().Name)") } }
    }
    try {
        $o = $raw | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $o) { return (& $fail 'empty') }
        $state = [string]$o.state
        if ([string]$o.schema -cne 'heavy-slot-ab.v1') { return (& $fail 'schema') }
        if ($state -cne 'B') { return (& $fail "state-$state") }
        if (-not ($o.count -is [int] -or $o.count -is [long]) -or [long]$o.count -ne 2) { return (& $fail 'count-not-2') }
        $toUtc = {
            param($v)
            if ($v -is [DateTime]) { return $v.ToUniversalTime() }
            if ($v -is [DateTimeOffset]) { return $v.UtcDateTime }
            return [DateTimeOffset]::Parse([string]$v, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime
        }
        $end = & $toUtc $o.endUtc
        $start = & $toUtc $o.windowStartUtc
        if ($Now.ToUniversalTime() -lt $start) { return (& $fail 'not-started') }   # a clock stepped back must not enable a window that has not begun
        if ($Now.ToUniversalTime() -ge $end) { return (& $fail 'expired') }
        if ($end -le $start -or ($end - $start) -gt [TimeSpan]::FromHours(25)) { return (& $fail 'window-over-25h') }
        if (-not ($o.driverPid -is [int] -or $o.driverPid -is [long]) -or [long]$o.driverPid -lt 1) { return (& $fail 'driver-pid') }
        if (-not ($o.driverStartTicks -is [int] -or $o.driverStartTicks -is [long])) { return (& $fail 'driver-start-ticks') }
        $p = $null
        try { $p = Get-Process -Id ([int]$o.driverPid) -ErrorAction Stop; $ticks = $p.StartTime.ToUniversalTime().Ticks } catch { return (& $fail 'driver-dead') }
        if ($ticks -ne [long]$o.driverStartTicks) { return (& $fail 'driver-pid-reused') }
        return [pscustomobject]@{ Count = 2; State = 'B'; Reason = 'ok' }
    }
    catch { return (& $fail "invalid: $($_.Exception.GetType().Name)") }
}
# HEAVY_SLOT_AB_READER v1 END

function Get-HeavySlotAbCount {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$SwitchPath, [DateTime]$Now = [DateTime]::UtcNow)
    return [int](Get-HeavySlotAbStatus -SwitchPath $SwitchPath -Now $Now).Count
}

function Resolve-HeavySlotAbRealPath {
    # The REAL full path of $Path: every existing component is looked up in its parent (which turns a short 8.3 name into the long one) and a
    # junction or symlink is replaced by its final target, so two spellings of one place compare equal. A component that does not exist yet
    # (a switch, a window dir not created yet) is appended unresolved. Used by the driver and the evaluator for the "never write over what
    # is only read" containment check (adversary r3 A1): a plain GetFullPath compare is fooled by an alias.
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path, [int]$Depth = 0)
    $full = [IO.Path]::GetFullPath($Path)
    if ($Depth -gt 16) { return $full }
    $root = [IO.Path]::GetPathRoot($full)
    $cur = $root
    $parts = @($full.Substring($root.Length) -split '[\\/]' | Where-Object { $_ })
    for ($i = 0; $i -lt $parts.Count; $i++) {
        $next = [IO.Path]::Combine($cur, $parts[$i])
        $info = $null
        try {
            $hit = @((New-Object IO.DirectoryInfo $cur).GetFileSystemInfos($parts[$i]))
            if ($hit.Count -eq 1) { $info = $hit[0] }
        }
        catch { $info = $null }
        if ($null -eq $info) {
            if ([IO.Directory]::Exists($next)) { $info = New-Object IO.DirectoryInfo $next }
            elseif ([IO.File]::Exists($next)) { $info = New-Object IO.FileInfo $next }
        }
        if ($null -eq $info) { return ([IO.Path]::Combine($cur, (($parts[$i..($parts.Count - 1)]) -join '\'))) }
        $target = $null
        try { $target = $info.ResolveLinkTarget($true) } catch { $target = $null }
        if ($null -ne $target) { $cur = Resolve-HeavySlotAbRealPath -Path $target.FullName -Depth ($Depth + 1) }
        else { $cur = $info.FullName }
    }
    return $cur
}

# =====================================================================================================================================
# r6 SHARED HELPERS. Six adversary passes found the same three defect classes again at sibling sites, because each round fixed one input
# at one site. Every site in the driver and the evaluator now calls ONE helper per class:
#   (a) WRITE SAFETY  Test-HeavySlotAbWriteAlias / Write-HeavySlotAbFile / Open-HeavySlotAbLockFile
#   (b) ROW PARSE     ConvertTo-HeavySlotAbUtc / Read-HeavySlotAbRow / Test-HeavySlotAbDuplicate
#   (c) ORDERING      Step-HeavySlotAbWatermark / Test-HeavySlotAbRetryMayStart
# =====================================================================================================================================

# ---- (a) WRITE SAFETY --------------------------------------------------------------------------------------------------------------
function Test-HeavySlotAbWriteAlias {
    # Why $Path may NOT be written, or $null. A name that exists and is a reparse point (symlink, junction), a directory, or a file with
    # more than one hard link (PowerShell LinkType 'HardLink': NumberOfLinks > 1) may alias another file, which a write would change.
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)
    $it = $null
    try { $it = Get-Item -LiteralPath $Path -Force -ErrorAction Stop } catch { $it = $null }
    if ($null -eq $it) {
        # Get-Item can fail on a dangling link: the name is still taken, and a write would follow it
        try { if ([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) { return 'a reparse point' } } catch { }
        return $null
    }
    if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) { return 'a reparse point (symlink or junction)' }
    if ($it.PSIsContainer) { return 'a directory' }
    if ([string]$it.LinkType -eq 'HardLink') { return 'a hard link (the file has more than one name)' }
    return $null
}

function Write-HeavySlotAbFile {
    # The ONE writer of every file the driver and the evaluator create, truncate or append (adversary pass 6 A1 and siblings).
    #  - A target that is an alias (Test-HeavySlotAbWriteAlias) is REFUSED: an IOException naming it, nothing written.
    #  - A whole-file write goes to a fresh temp name made with CreateNew (it can never open, follow or truncate a planted name) and is then
    #    MOVED over the target, so the target's NAME is replaced, never its bytes opened in place.
    #  - -Append opens the target for append only (never truncates), after the same alias check; the bytes are written in one call.
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Text, [switch]$Append)
    $why = Test-HeavySlotAbWriteAlias -Path $Path
    if ($why) { throw [IO.IOException]::new("write-alias: $Path is $why and may alias another file; this tool never writes through it") }
    $bytes = (New-Object Text.UTF8Encoding($false)).GetBytes($Text)
    if ($Append) {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
        return
    }
    $tmp = '{0}.{1}.tmp' -f $Path, [Guid]::NewGuid().ToString('N')
    $fs = [IO.File]::Open($tmp, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
    try { [IO.File]::Move($tmp, $Path, $true) }
    catch { try { [IO.File]::Delete($tmp) } catch { }; throw }
}

function Open-HeavySlotAbLockFile {
    # An exclusive lock file (never written to). OpenOrCreate on a planted alias would take FileShare.None on the aliased file (a read-only
    # input such as the feed) and block its writer, so the same alias rule applies (adversary pass 6 A1 sibling).
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path, [switch]$CreateNew)
    $why = Test-HeavySlotAbWriteAlias -Path $Path
    if ($why) { throw [IO.IOException]::new("write-alias: $Path is $why and may alias another file; this tool never locks through it") }
    $mode = if ($CreateNew) { [IO.FileMode]::CreateNew } else { [IO.FileMode]::OpenOrCreate }
    return [IO.File]::Open($Path, $mode, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}

# ---- (b) ROW PARSE -----------------------------------------------------------------------------------------------------------------
function ConvertTo-HeavySlotAbUtc($v) {
    # A UTC instant, or $null. ConvertFrom-Json already turns an ISO date string into a [DateTime]; a string is parsed as UTC. Never go
    # through [string] of a DateTime (that drops the fraction and renders local time).
    if ($null -eq $v -or "$v" -eq '') { return $null }
    if ($v -is [DateTime]) { return $v.ToUniversalTime() }
    if ($v -is [DateTimeOffset]) { return $v.UtcDateTime }
    if ($v -isnot [string]) { return $null }
    try { return [DateTimeOffset]::Parse($v, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime } catch { return $null }
}

function Read-HeavySlotAbRow {
    # The ONE row parser for every row shape (ledger, receipt, held, sample, feed). $Spec maps a field name to its kind:
    #   time, string, int, number, bool, object, array       REQUIRED: absent or null is unknown evidence
    #   time?, string?, int?, number?, bool?, object?, array?  OPTIONAL: absent or null is allowed (the event did not happen)
    # ONE RULE for both: a field that is present and non-null must have its kind (a string where a number belongs, a date that does not
    # parse, NaN / Infinity) or the WHOLE ROW is UNKNOWN EVIDENCE. Returns @{ ok; why; v } where v holds the parsed values (times as UTC
    # [DateTime], numbers as [double], ints as [long]). A caller never skips an unknown row and never counts it: it fails closed.
    [CmdletBinding()]
    param($Row, [Parameter(Mandatory = $true)][System.Collections.IDictionary]$Spec)
    $v = @{}
    if ($null -eq $Row -or $Row -isnot [pscustomobject]) { return [pscustomobject]@{ ok = $false; why = 'the row is not a JSON object'; v = $v } }
    foreach ($name in $Spec.Keys) {
        $kind = [string]$Spec[$name]
        $optional = $kind.EndsWith('?')
        $k = $kind.TrimEnd('?')
        $prop = $Row.PSObject.Properties[$name]
        $raw = $null
        if ($null -ne $prop) { $raw = $prop.Value }     # a plain assignment: an `if` expression would unroll a one-element array to its element
        if ($null -eq $raw) {
            if ($optional) { $v[$name] = $null; continue }
            return [pscustomobject]@{ ok = $false; why = "$name is missing or null"; v = $v }
        }
        $val = $null
        $good = $false
        switch ($k) {
            'time' { $val = ConvertTo-HeavySlotAbUtc $raw; $good = ($null -ne $val) }
            'string' { if ($raw -is [string]) { $val = $raw; $good = $true } }
            'int' { if ($raw -is [int] -or $raw -is [long]) { $val = [long]$raw; $good = $true } }
            'number' { if ($raw -is [int] -or $raw -is [long] -or $raw -is [double] -or $raw -is [decimal] -or $raw -is [single]) { $val = [double]$raw; $good = [double]::IsFinite($val) } }
            'bool' { if ($raw -is [bool]) { $val = $raw; $good = $true } }
            'object' { if ($raw -is [pscustomobject]) { $val = $raw; $good = $true } }
            'array' { if ($raw -is [array]) { $val = @($raw); $good = $true } }
            default { throw "Read-HeavySlotAbRow: unknown kind '$kind' for $name" }
        }
        if (-not $good) { return [pscustomobject]@{ ok = $false; why = ("{0} is not a valid {1} ({2})" -f $name, $k, ("$raw" -replace '\s+', ' ').Substring(0, [Math]::Min(40, ("$raw").Length))); v = $v } }
        $v[$name] = $val
    }
    return [pscustomobject]@{ ok = $true; why = ''; v = $v }
}

function Test-HeavySlotAbDuplicate {
    # $true when $Key was already seen in $Seen (a HashSet[string]); records it otherwise. Two rows with ONE identity (two receipts or two
    # held records for one ticket, two samples at one instant, two admits of one ticket) are unknown evidence: never counted twice, never
    # silently collapsed into one (adversary pass 6 A7).
    param([Parameter(Mandatory = $true)][AllowEmptyCollection()][System.Collections.Generic.HashSet[string]]$Seen, [Parameter(Mandatory = $true)][string]$Key)
    return (-not $Seen.Add($Key))
}

# ---- (c) ORDERING ------------------------------------------------------------------------------------------------------------------
function Step-HeavySlotAbWatermark {
    # The ONE ordering rule for a time-ordered stream read again and again (the CoreTemp feed). $Times are the row times of ONE read in
    # FILE order; $Watermark is the maximum ACCEPTED row time so far ($null before the first read). The read is refused (Regressed) when
    #   - any row is older than a row before it in the same read (file order must be non-decreasing), or
    #   - the read's newest row is older than the watermark (the stream was rewritten or the clock stepped back).
    # Otherwise the rows newer than the watermark are the NEW rows (their indexes in NewIndex) and the watermark moves to the read's
    # maximum. There is no tolerance: hiding even one hot row behind a future-dated cool one is the defect (adversary pass 6 A2).
    param([AllowEmptyCollection()][DateTime[]]$Times, $Watermark)
    $new = New-Object System.Collections.Generic.List[int]
    $max = $null
    for ($i = 0; $i -lt $Times.Count; $i++) {
        $t = $Times[$i]
        if ($null -ne $max -and $t -lt $max) {
            return [pscustomobject]@{ Regressed = $true; Why = ('row {0} ({1:o}) is older than an earlier row of the same read ({2:o})' -f $i, $t, $max); NewIndex = @(); Watermark = $Watermark }
        }
        if ($null -eq $max -or $t -gt $max) { $max = $t }
        if ($null -eq $Watermark -or $t -gt $Watermark) { $new.Add($i) }
    }
    if ($null -ne $Watermark -and $null -ne $max -and $max -lt $Watermark) {
        return [pscustomobject]@{ Regressed = $true; Why = ('the newest row ({0:o}) is older than the newest row already accepted ({1:o})' -f $max, $Watermark); NewIndex = @(); Watermark = $Watermark }
    }
    $wm = if ($null -eq $Watermark -or ($null -ne $max -and $max -gt $Watermark)) { $max } else { $Watermark }
    return [pscustomobject]@{ Regressed = $false; Why = ''; NewIndex = $new.ToArray(); Watermark = $wm }
}

function Test-HeavySlotAbRetryMayStart {
    # RULE (adversary pass 6 A6, chosen and tested): a retry attempt may not START after the budget minus its maximum jitter. The caller
    # asks this BEFORE it sleeps the jitter; $true only when Now + MaxJitterMs is still before GiveUp, so the attempt that follows the sleep
    # starts inside the budget. The first attempt never asks.
    param([Parameter(Mandatory = $true)][DateTime]$Now, [Parameter(Mandatory = $true)][DateTime]$GiveUp, [Parameter(Mandatory = $true)][double]$MaxJitterMs)
    return ($Now.AddMilliseconds($MaxJitterMs) -lt $GiveUp)
}

Export-ModuleMember -Function Get-HeavySlotAbStatus, Get-HeavySlotAbCount, Resolve-HeavySlotAbRealPath, Test-HeavySlotAbWriteAlias, Write-HeavySlotAbFile, Open-HeavySlotAbLockFile, ConvertTo-HeavySlotAbUtc, Read-HeavySlotAbRow, Test-HeavySlotAbDuplicate, Step-HeavySlotAbWatermark, Test-HeavySlotAbRetryMayStart
