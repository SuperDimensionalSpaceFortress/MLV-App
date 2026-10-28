# HubProfile.psm1 - loads and validates a hub profile (see README.md in this folder).
# Get-HubProfile [-Path <file>] returns the parsed profile object, or throws a typed error:
#   HubProfileMissingKeyException   a required key is absent or empty (message: "hub-profile-missing-key: <key>")
#   HubProfileUnreadableException   the file is absent, is not valid JSON, or its root is not exactly one JSON object (a scalar, or an array of any length, even one holding a valid profile) (message: "hub-profile-unreadable: <path> (<why>)")
# The default path is hub-profile.json beside this module.
Set-StrictMode -Version Latest

if (-not ('HubProfileMissingKeyException' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
public class HubProfileMissingKeyException : Exception {
    public string Key { get; private set; }
    public HubProfileMissingKeyException(string key) : base("hub-profile-missing-key: " + key) { Key = key; }
}
public class HubProfileUnreadableException : Exception {
    public HubProfileUnreadableException(string path, string why) : base("hub-profile-unreadable: " + path + " (" + why + ")") { }
}
'@
}

$script:RequiredKeys = @('schema', 'project', 'boardRoot', 'repoSlug', 'repoAliases', 'ownerLogin', 'busRoot', 'busRef', 'cosFeedbackPath', 'cosOpenAsksOutFile')

function Get-HubProfile {
    [CmdletBinding()]
    param([string]$Path = (Join-Path $PSScriptRoot 'hub-profile.json'))

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw [HubProfileUnreadableException]::new($Path, 'file not found') }
    # -NoEnumerate keeps an array root as one Object[]; without it the pipeline unwraps [ {x} ] into x, and a one-element array would pass as the object.
    try { $p = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -NoEnumerate -ErrorAction Stop }
    catch { throw [HubProfileUnreadableException]::new($Path, 'not valid JSON') }
    # The root must be exactly one PSCustomObject. On pwsh 7 a scalar (17, "x", true) and an array both fail -is [PSCustomObject] (so does -is [pscustomobject]); a null root is $null.
    if ($p -is [array]) { throw [HubProfileUnreadableException]::new($Path, 'root is an array') }
    if ($null -eq $p -or $p -isnot [System.Management.Automation.PSCustomObject]) { throw [HubProfileUnreadableException]::new($Path, 'not a JSON object') }

    foreach ($k in $script:RequiredKeys) {
        $prop = $p.PSObject.Properties[$k]
        $empty = $null -eq $prop -or $null -eq $prop.Value -or
            ($prop.Value -is [string] -and -not $prop.Value.Trim()) -or
            ($prop.Value -is [array] -and @($prop.Value).Count -eq 0)
        if ($empty) { throw [HubProfileMissingKeyException]::new($k) }
    }
    $p
}

Export-ModuleMember -Function Get-HubProfile
