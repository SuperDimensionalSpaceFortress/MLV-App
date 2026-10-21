# Shared job-key rules for the venue agent mailbox (Get-AgentShareRecent.ps1, Invoke-AgentShareRotation.ps1).
# The suffixes are the names ultra-magnus-agent.ps1, UmRunDrop.psm1 and the job scripts write for one job id J:
#   inbox             : J.job.ps1, J.meta.json, J.<32 hex>.meta.tmp, J.<32 hex>.job.tmp (UmRunDrop nonce temps)
#   processed         : J.job.ps1, J.meta.json, J.cancel, J.started.<status>.json, J.started.requeue-no-job.json
#   running           : J.started.json, J.started.json.<32 hex>.tmp, J.started.json.tmp (+ .stale-/.corrupt- set-asides)
#   outbox            : J.result.json, J.result.<32 hex>.tmp, J.result.tmp, J.artifacts, J.artifacts.<anything>,
#                       J.provenance.json
#   logs              : J.out.txt, J.err.txt, J.<12 hex>.heavy-slot.out.txt, J.<12 hex>.heavy-slot.err.txt (agent
#                       heavy-slot holder, one pair per holder start; see HeavySlotNonceRx below)
# Job ids may contain dots (UmRunDrop: ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$), including dots followed by a suffix word
# ("J.artifacts", "J.started"). So the SHORTEST matching tail wins: the key is the name up to the RIGHTMOST dot whose
# tail is a whole suffix. "J.artifacts.result.json" is job "J.artifacts" (its tail ".result.json" matches before
# ".artifacts.result.json" is ever considered), and "J.other.result.json" is job "J.other", never "J".

$script:SuffixRx = [regex]('^\.(job\.ps1|meta\.json|cancel|[0-9a-f]{32}\.(meta|job)\.tmp' +
    '|started(\.[A-Za-z0-9-]+)?\.json|started\.json(\.[0-9a-f]{32})?\.tmp|started\.json\..+' +
    '|result(\.[0-9a-f]{32})?\.(json|tmp)|artifacts(\..+)?|provenance\.json' +
    '|out\.txt|err\.txt)$')
# The agent's heavy-slot holder logs are a multi-segment suffix the rightmost rule would misread. The real producer
# (ultra-magnus-agent.ps1 Start-HeavySlotHolder) names them "$JobId.<12 hex>.heavy-slot.out.txt" / ".err.txt", where the
# 12 hex chars are a fresh nonce ([guid]::NewGuid().ToString('N').Substring(0, 12), lowercase) per holder start. The
# disambiguation policy, in order (these logs are only ever looked up, never rotated):
#   1. NONCE FORM  "<id>.<12 lowercase hex>.heavy-slot.(out|err).txt" -> key <id>. The producer always writes the nonce,
#      so this is the real shape, and it wins even when <id> itself ends in ".heavy-slot" or in 12 hex chars:
#      "J.heavy-slot.<nonce>.heavy-slot.out.txt" is job "J.heavy-slot"; "J.<nonce>.heavy-slot.out.txt" is job "J".
#   2. NONCE-FREE  "<id>.heavy-slot.(out|err).txt" -> key <id>. The producer never writes this; it is kept as the
#      earlier documented precedence (hand-made or older names), so a job id that itself ends in ".heavy-slot" and
#      has a nonce-free name is read as the holder log of the id before it.
# A 12-hex segment that is uppercase or not exactly 12 long is not the nonce and stays part of the id (rule 2).
$script:HeavySlotNonceRx = [regex]'^(?<id>.+)\.[0-9a-f]{12}\.heavy-slot\.(out|err)\.txt$'
$script:HeavySlotRx = [regex]'^(?<id>.+)\.heavy-slot\.(out|err)\.txt$'

function Get-AgentShareJobKey {
    # The job id an entry name belongs to; a name with no known suffix is its own key.
    param([Parameter(Mandatory = $true)][string]$Name)
    $hs = $script:HeavySlotNonceRx.Match($Name)
    if (-not $hs.Success) { $hs = $script:HeavySlotRx.Match($Name) }
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
