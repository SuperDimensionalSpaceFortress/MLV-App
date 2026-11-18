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

Export-ModuleMember -Function Get-HeavySlotAbStatus, Get-HeavySlotAbCount
