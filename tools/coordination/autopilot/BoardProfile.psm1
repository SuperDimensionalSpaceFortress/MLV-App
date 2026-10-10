# BoardProfile.psm1 - loads and validates a board profile (see README.md in this folder).
# Get-BoardProfile [-Path <file>] returns the parsed profile object, or throws a typed error:
#   BoardProfileMissingKeyException   a required key is absent or empty (message: "board-profile-missing-key: <key>")
#   BoardProfileUnreadableException   the file is absent or is not valid JSON (message: "board-profile-unreadable: <path>")
# The default path is board-profile.json beside this module.
Set-StrictMode -Version Latest

if (-not ('BoardProfileMissingKeyException' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
public class BoardProfileMissingKeyException : Exception {
    public string Key { get; private set; }
    public BoardProfileMissingKeyException(string key) : base("board-profile-missing-key: " + key) { Key = key; }
}
public class BoardProfileUnreadableException : Exception {
    public BoardProfileUnreadableException(string path, string why) : base("board-profile-unreadable: " + path + " (" + why + ")") { }
}
'@
}

$script:RequiredKeys = @('schema', 'project', 'boardRoot', 'remote', 'branch', 'productPaths', 'roadmap', 'doctrineOutboxTarget', 'venues', 'boardOwnedPaths')

function Get-BoardProfile {
    [CmdletBinding()]
    param([string]$Path = (Join-Path $PSScriptRoot 'board-profile.json'))

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw [BoardProfileUnreadableException]::new($Path, 'file not found') }
    try { $p = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw [BoardProfileUnreadableException]::new($Path, 'not valid JSON') }
    if ($null -eq $p -or $p -isnot [pscustomobject]) { throw [BoardProfileUnreadableException]::new($Path, 'not a JSON object') }

    foreach ($k in $script:RequiredKeys) {
        $prop = $p.PSObject.Properties[$k]
        $empty = $null -eq $prop -or $null -eq $prop.Value -or
            ($prop.Value -is [string] -and -not $prop.Value.Trim()) -or
            ($prop.Value -is [array] -and @($prop.Value).Count -eq 0)
        if ($empty) { throw [BoardProfileMissingKeyException]::new($k) }
    }
    $p
}

Export-ModuleMember -Function Get-BoardProfile
