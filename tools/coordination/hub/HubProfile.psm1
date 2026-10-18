# HubProfile.psm1 - loads and validates a hub profile (see README.md in this folder).
# Get-HubProfile [-Path <file>] returns the parsed profile object, or throws a typed error:
#   HubProfileMissingKeyException   a required key is absent or empty (message: "hub-profile-missing-key: <key>")
#   HubProfileUnreadableException   the file is absent or is not valid JSON (message: "hub-profile-unreadable: <path> (<why>)")
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
    try { $p = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw [HubProfileUnreadableException]::new($Path, 'not valid JSON') }
    if ($null -eq $p -or $p -isnot [pscustomobject]) { throw [HubProfileUnreadableException]::new($Path, 'not a JSON object') }

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
