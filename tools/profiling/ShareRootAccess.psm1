# ShareRootAccess.psm1 (MINI3-CUTOVER-STATE-GAPS-1)
#
# Tells "the share root cannot be read" (access denied: System error 5, an UnauthorizedAccessException, or the
# credential-missing logon failures) apart from "the share is readable but the agent heartbeat is missing".
# Test-Path answers $false for both, so um-run.ps1 used to print 'No agent heartbeat' on a host that simply had no
# SMB credential (PC-MINI3 ran a day on that wrong lead).
#
# The probe touches ONLY the root itself, by name (one attribute read). It never lists the root and never
# touches processed\ or outbox\ (thousands of files; each is opened over SMB and Defender-scanned).
#
# Typed line: "SHARE_ACCESS_DENIED <unc root>" -- the whole remainder of the line is the root, nothing follows it.

Set-StrictMode -Version 1.0

# Win32 codes that mean "this host has no usable credential for the share": 5 ERROR_ACCESS_DENIED,
# 1326 ERROR_LOGON_FAILURE (unknown user name or bad password), 1219 ERROR_SESSION_CREDENTIAL_CONFLICT.
$script:DeniedWin32 = @(5, 1219, 1326)
$script:DeniedText = 'System error (5|1326|1219)\b|Access (to the path .* )?is denied|Logon failure|UnauthorizedAccess|Multiple connections to a server'

function Test-ShareAccessDeniedException {
    # True when the exception (or anything in its InnerException chain) is an access-denied class failure.
    param([Parameter(Mandatory = $true)][AllowNull()][object]$Exception)
    $e = $Exception
    $depth = 0
    while ($null -ne $e -and $depth -lt 8) {
        if ($e -is [System.Management.Automation.ErrorRecord]) { $e = $e.Exception; continue }
        if ($e -is [UnauthorizedAccessException]) { return $true }
        if ($e -is [System.Security.SecurityException]) { return $true }
        if ($e -is [System.IO.IOException] -and ($script:DeniedWin32 -contains ([int]($e.HResult -band 0xFFFF)))) { return $true }
        if ($e -is [System.ComponentModel.Win32Exception] -and ($script:DeniedWin32 -contains [int]$e.NativeErrorCode)) { return $true }
        if ($e -is [Exception] -and ([string]$e.Message) -match $script:DeniedText) { return $true }
        $e = if ($e -is [Exception]) { $e.InnerException } else { $null }
        $depth++
    }
    return $false
}

function Get-ShareRootAccess {
    <#
    .SYNOPSIS
        Probe a share root. Returns State = READABLE | ACCESS_DENIED | UNREACHABLE, plus Root and Detail.
    .PARAMETER Root
        The share root as the caller uses it (for example \\bachelor\mlv-agent).
    .PARAMETER Probe
        Test seam: a scriptblock invoked with the root. Throwing means the root cannot be read; returning means it can.
        The default reads the root's attributes ([IO.File]::GetAttributes): no listing, nothing below the root.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [scriptblock]$Probe = $null
    )
    if (-not $Probe) { $Probe = { param($r) [void][System.IO.File]::GetAttributes($r) } }
    try {
        [void](& $Probe $Root)
        return [pscustomobject]@{ State = 'READABLE'; Root = $Root; Detail = '' }
    } catch {
        $detail = ([string]$_.Exception.Message) -replace '\s+', ' '
        if (Test-ShareAccessDeniedException -Exception $_) {
            return [pscustomobject]@{ State = 'ACCESS_DENIED'; Root = $Root; Detail = $detail }
        }
        return [pscustomobject]@{ State = 'UNREACHABLE'; Root = $Root; Detail = $detail }
    }
}

function Get-ShareAccessDeniedLine {
    param([Parameter(Mandatory = $true)][string]$Root)
    return "SHARE_ACCESS_DENIED $Root"
}

Export-ModuleMember -Function Test-ShareAccessDeniedException, Get-ShareRootAccess, Get-ShareAccessDeniedLine
