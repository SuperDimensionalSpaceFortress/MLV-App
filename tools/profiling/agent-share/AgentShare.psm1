# Shared job-key rules for the venue agent mailbox (Get-AgentShareRecent.ps1, Invoke-AgentShareRotation.ps1).
# The suffixes are the names ultra-magnus-agent.ps1 and UmRunDrop.psm1 write for one job id J:
#   inbox / processed : J.job.ps1, J.meta.json, J.cancel, J.started.<status>.json, J.started.requeue-no-job.json
#   running           : J.started.json, J.started.json.<32 hex>.tmp, J.started.json.tmp (+ .stale-/.corrupt- set-asides)
#   outbox            : J.result.json, J.result.<32 hex>.tmp, J.result.tmp, J.artifacts, J.artifacts.<anything>
# Job ids may contain dots (UmRunDrop: ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$), so a suffix is stripped only when the
# WHOLE tail matches; "J.other.result.json" belongs to job "J.other", never to "J".

$script:SuffixRx = [regex]'\.(job\.ps1|meta\.json|cancel|started(\.[A-Za-z0-9-]+)?\.json|started\.json(\.[0-9a-f]{32})?\.tmp|started\.json.*|result(\.[0-9a-f]{32})?\.(json|tmp)|artifacts(\..*)?)$'

function Get-AgentShareJobKey {
    # The job id an entry name belongs to; a name with no known suffix is its own key.
    param([Parameter(Mandatory = $true)][string]$Name)
    $m = $script:SuffixRx.Match($Name)
    if ($m.Success -and $m.Index -gt 0) { return $Name.Substring(0, $m.Index) }
    return $Name
}

function Test-AgentShareJobEntry {
    # True when entry $Name belongs to exactly job $JobId (case-insensitive, as the share's filesystem is).
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][string]$JobId)
    return [string]::Equals((Get-AgentShareJobKey -Name $Name), $JobId, [StringComparison]::OrdinalIgnoreCase)
}

Export-ModuleMember -Function Get-AgentShareJobKey, Test-AgentShareJobEntry
