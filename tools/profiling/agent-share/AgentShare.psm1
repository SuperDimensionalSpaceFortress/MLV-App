# Shared job-key rules for the venue agent mailbox (Get-AgentShareRecent.ps1, Invoke-AgentShareRotation.ps1).
# The suffixes are the names ultra-magnus-agent.ps1, UmRunDrop.psm1 and the job scripts write for one job id J:
#   inbox             : J.job.ps1, J.meta.json, J.<32 hex>.meta.tmp, J.<32 hex>.job.tmp (UmRunDrop nonce temps)
#   processed         : J.job.ps1, J.meta.json, J.cancel, J.started.<status>.json, J.started.requeue-no-job.json
#   running           : J.started.json, J.started.json.<32 hex>.tmp, J.started.json.tmp (+ .stale-/.corrupt- set-asides)
#   outbox            : J.result.json, J.result.<32 hex>.tmp, J.result.tmp, J.artifacts, J.artifacts.<anything>,
#                       J.provenance.json
#   logs              : J.out.txt, J.err.txt, J.heavy-slot.out.txt, J.heavy-slot.err.txt (agent heavy-slot holder)
# Job ids may contain dots (UmRunDrop: ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$), including dots followed by a suffix word
# ("J.artifacts", "J.started"). So the SHORTEST matching tail wins: the key is the name up to the RIGHTMOST dot whose
# tail is a whole suffix. "J.artifacts.result.json" is job "J.artifacts" (its tail ".result.json" matches before
# ".artifacts.result.json" is ever considered), and "J.other.result.json" is job "J.other", never "J".

$script:SuffixRx = [regex]('^\.(job\.ps1|meta\.json|cancel|[0-9a-f]{32}\.(meta|job)\.tmp' +
    '|started(\.[A-Za-z0-9-]+)?\.json|started\.json(\.[0-9a-f]{32})?\.tmp|started\.json\..+' +
    '|result(\.[0-9a-f]{32})?\.(json|tmp)|artifacts(\..+)?|provenance\.json' +
    '|out\.txt|err\.txt)$')
# The agent's heavy-slot holder logs (ultra-magnus-agent.ps1: "$id.heavy-slot.out.txt"/".err.txt") are a two-segment
# suffix the rightmost rule would read as job "J.heavy-slot". They take precedence: a job id that itself ends in
# ".heavy-slot" is not distinguishable from them by name, and these logs are only ever looked up, never rotated.
$script:HeavySlotRx = [regex]'^(?<id>.+)\.heavy-slot\.(out|err)\.txt$'

function Get-AgentShareJobKey {
    # The job id an entry name belongs to; a name with no known suffix is its own key.
    param([Parameter(Mandatory = $true)][string]$Name)
    $hs = $script:HeavySlotRx.Match($Name)
    if ($hs.Success) { return $hs.Groups['id'].Value }
    for ($i = $Name.LastIndexOf('.'); $i -gt 0; $i = $Name.LastIndexOf('.', $i - 1)) {
        if ($script:SuffixRx.IsMatch($Name.Substring($i))) { return $Name.Substring(0, $i) }
    }
    return $Name
}

function Test-AgentShareJobEntry {
    # True when entry $Name belongs to exactly job $JobId (case-insensitive, as the share's filesystem is).
    param([Parameter(Mandatory = $true)][string]$Name, [Parameter(Mandatory = $true)][string]$JobId)
    return [string]::Equals((Get-AgentShareJobKey -Name $Name), $JobId, [StringComparison]::OrdinalIgnoreCase)
}

Export-ModuleMember -Function Get-AgentShareJobKey, Test-AgentShareJobEntry
