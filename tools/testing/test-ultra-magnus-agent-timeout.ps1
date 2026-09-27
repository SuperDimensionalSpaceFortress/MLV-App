[CmdletBinding()]
param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
)

# ROADMAP (owner: Codex; trigger: next gate-family/tooling-baseline block):
# Register this script in toolingBaseline.requiredSymbols. The CDX-3 correction
# attempted that registration, but closeout.config.json is unmapped by the
# sanctioned dual-lane commit path, so changing it in this work block fails
# closed instead of silently broadening ownership.

$ErrorActionPreference = 'Stop'
$agentScript = Join-Path $RepoRoot 'tools\profiling\ultra-magnus-agent.ps1'
$runnerScript = Join-Path $RepoRoot 'tools\profiling\um-run.ps1'
$runId = 'agent-timeout-{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss'), $PID
$root = Join-Path $RepoRoot ".claude-state\testing\$runId"
$probeRoot = Join-Path $root 'identity-probe'
$payloadPath = Join-Path $root 'tracked-descendant-timeout.ps1'
$intermediatePath = Join-Path $root 'tracked-intermediate.ps1'
$grandchildPath = Join-Path $root 'tracked-grandchild.ps1'
$grandchildIdentityPath = Join-Path $root 'tracked-grandchild.json'
$agent = $null
$grandchildIdentity = $null

New-Item -ItemType Directory -Force -Path $root | Out-Null
@'
Start-Sleep -Seconds 30
'@ | Set-Content -LiteralPath $grandchildPath -Encoding ASCII
@"
`$shell = (Get-Command pwsh.exe -ErrorAction Stop).Source
`$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
`$startInfo.FileName = `$shell
`$startInfo.UseShellExecute = `$false
`$startInfo.CreateNoWindow = `$true
foreach (`$argument in @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', '$grandchildPath')) { [void]`$startInfo.ArgumentList.Add(`$argument) }
`$grandchild = [System.Diagnostics.Process]::Start(`$startInfo)
[pscustomobject]@{ processId = `$grandchild.Id; startUtc = `$grandchild.StartTime.ToUniversalTime().ToString('o') } | ConvertTo-Json -Compress | Set-Content -LiteralPath '$grandchildIdentityPath' -Encoding ASCII
Start-Sleep -Milliseconds 2500
"@ | Set-Content -LiteralPath $intermediatePath -Encoding ASCII
@"
`$shell = (Get-Command pwsh.exe -ErrorAction Stop).Source
`$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
`$startInfo.FileName = `$shell
`$startInfo.UseShellExecute = `$false
`$startInfo.CreateNoWindow = `$true
foreach (`$argument in @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', '$intermediatePath')) { [void]`$startInfo.ArgumentList.Add(`$argument) }
[void][System.Diagnostics.Process]::Start(`$startInfo)
Start-Sleep -Seconds 30
"@ | Set-Content -LiteralPath $payloadPath -Encoding ASCII

try {
    $shell = (Get-Command pwsh.exe -ErrorAction Stop).Source
    $identityProbeJson = & $shell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $agentScript -Root $probeRoot -SelfTestIdentityGuard
    if ($LASTEXITCODE -ne 0) { throw "Identity fail-closed probe exited $LASTEXITCODE." }
    $identityProbe = $identityProbeJson | ConvertFrom-Json
    if ([bool]$identityProbe.matches -or $identityProbe.reason -ne 'missing-expected-identity-credentials') {
        throw "Null expected identity credentials did not fail closed: $identityProbeJson"
    }

    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $shell
    $startInfo.WorkingDirectory = $RepoRoot
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($argument in @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', $agentScript, '-Root', $root, '-PollSeconds', '1', '-JobTimeoutSec', '4')) {
        [void]$startInfo.ArgumentList.Add($argument)
    }
    $agent = [System.Diagnostics.Process]::Start($startInfo)
    $agentStartUtc = $agent.StartTime.ToUniversalTime()

    $heartbeatPath = Join-Path $root 'heartbeat.txt'
    $heartbeatDeadline = (Get-Date).AddSeconds(15)
    do {
        if (Test-Path -LiteralPath $heartbeatPath -PathType Leaf) { break }
        Start-Sleep -Milliseconds 250
    } while ((Get-Date) -lt $heartbeatDeadline)
    if (-not (Test-Path -LiteralPath $heartbeatPath -PathType Leaf)) { throw "Timed out waiting for $heartbeatPath" }

    # The agent is started with its own -JobTimeoutSec 4 (below); the client asks for 20s. Before
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 this agent had no meta.json handling at all, so it
    # always killed at its own 4s regardless of what the client asked for -- this test passed, but
    # for the wrong reason (fable/sol major 2). It now must kill at the REQUESTED 20s instead.
    $result = & $runnerScript -ScriptPath $payloadPath -AgentShare $root -TimeoutSec 20 -PollSeconds 1
    if ([int]$result.exitCode -ne 124 -or -not [bool]$result.timedOut) {
        throw "Expected agent timeout result; got exitCode=$($result.exitCode) timedOut=$($result.timedOut)."
    }
    if ([int]$result.timeoutSec -ne 20) {
        throw "Expected the agent to honour the job's metadata budget (20s) rather than its own -JobTimeoutSec default (4s); got timeoutSec=$($result.timeoutSec)."
    }
    $metaInboxPath = Join-Path $root ("inbox\{0}.meta.json" -f $result.jobId)
    $metaProcessedPath = Join-Path $root ("processed\{0}.meta.json" -f $result.jobId)
    # fable round 6 minor: this honour-path E2E used to assert both facts the instant um-run.ps1
    # returned, with no settle allowance at all -- the exact flake class round 5 fixed for
    # test_ultra_magnus_agent_metadata.py's own archival assertion (the agent publishes
    # outbox\<id>.result.json, which is what retires the JobId and what um-run.ps1 polls for,
    # BEFORE it archives inbox\<id>.job.ps1/meta.json to processed\ -- a few more of the agent's own
    # instructions past the instant the result becomes visible, not simultaneous with it). Protected
    # only incidentally, until now, by um-run.ps1's 400ms post-result settle sleep and 1s poll
    # cadence. Same bounded 5s poll, same two assertions.
    $metaSettleDeadline = (Get-Date).AddSeconds(5)
    while ((Test-Path -LiteralPath $metaInboxPath) -and -not (Test-Path -LiteralPath $metaProcessedPath) -and (Get-Date) -lt $metaSettleDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (Test-Path -LiteralPath $metaInboxPath) { throw "Job metadata was left behind in inbox: $metaInboxPath" }
    if (-not (Test-Path -LiteralPath $metaProcessedPath)) { throw "Job metadata was not moved to processed alongside its job: $metaProcessedPath" }
    if (-not (Test-Path -LiteralPath $grandchildIdentityPath -PathType Leaf)) { throw 'Tracked descendant did not publish its identity.' }
    $grandchildIdentity = Get-Content -LiteralPath $grandchildIdentityPath -Raw | ConvertFrom-Json
    $survivor = Get-Process -Id ([int]$grandchildIdentity.processId) -ErrorAction SilentlyContinue
    if ($survivor) {
        $sameStart = [Math]::Abs(($survivor.StartTime.ToUniversalTime() - [DateTimeOffset]::Parse($grandchildIdentity.startUtc).UtcDateTime).TotalSeconds) -lt 1
        if ($sameStart) { throw "Tracked descendant PID $($grandchildIdentity.processId) survived after its intermediate exited." }
    }
    if ([int]$grandchildIdentity.processId -notin @($result.killedProcessIds)) {
        throw "Timeout result did not report tracked descendant PID $($grandchildIdentity.processId) as killed."
    }
    if ($result.rootIdentitySource -ne 'Win32_Process' -or -not [bool]$result.rootIdentityHasImage -or [int]$result.rootIdentityToleranceMs -ne 0) {
        throw "Root identity was not guarded by exact CIM creation time plus image path: $($result | ConvertTo-Json -Compress)."
    }

    [pscustomobject]@{
        status = 'PASS'
        exitCode = $result.exitCode
        timedOut = $result.timedOut
        trackedDescendantPid = [int]$grandchildIdentity.processId
        trackedDescendantKilled = $true
        rootIdentitySource = $result.rootIdentitySource
        rootIdentityHasImage = [bool]$result.rootIdentityHasImage
        rootIdentityToleranceMs = [int]$result.rootIdentityToleranceMs
        nullCredentialGuard = $identityProbe.reason
        identityContracts = @('exact CIM root identity', 'creation-order guard', 'known-descendant cleanup', 'null-credential fail-closed')
    } | ConvertTo-Json -Depth 5
}
finally {
    if ($null -ne $grandchildIdentity) {
        try {
            $remaining = Get-Process -Id ([int]$grandchildIdentity.processId) -ErrorAction SilentlyContinue
            if ($remaining) {
                $sameStart = [Math]::Abs(($remaining.StartTime.ToUniversalTime() - [DateTimeOffset]::Parse($grandchildIdentity.startUtc).UtcDateTime).TotalSeconds) -lt 1
                if ($sameStart) { Stop-Process -Id $remaining.Id -Force -ErrorAction SilentlyContinue }
            }
        } catch { }
    }
    if ($null -ne $agent) {
        try {
            $actualStartUtc = $agent.StartTime.ToUniversalTime()
            if (-not $agent.HasExited -and [Math]::Abs(($actualStartUtc - $agentStartUtc).TotalSeconds) -lt 1) {
                $agent.Kill($true)
                [void]$agent.WaitForExit(5000)
            }
        }
        finally { $agent.Dispose() }
    }
    Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
}
