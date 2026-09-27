# ultra-magnus-agent.ps1
# Runs ON Ultra-Magnus. A minimal file-drop job runner so the VM can execute
# work on the RTX 4090 host over SMB alone (no SSH/WinRM/admin/open ports).
#
# Protocol (all under <agent root>, which is this script's folder):
#   inbox\<jobId>.job.ps1   - VM drops a PowerShell script here (atomic rename)
#   inbox\<jobId>.meta.json - OPTIONAL, VM-written per-job budget: {"jobId":...,"timeoutSec":N}
#   running\<jobId>.started.json - written the instant this agent claims a job: {"jobId":...,
#                            "startedUtc":...}. Consumed by um-run.ps1's client to switch from its
#                            submission-anchored QUEUED-phase deadline to a claim-anchored CLAIMED-
#                            phase one; removed again once the job's result is published so a later
#                            submission that reuses the same jobId never reads a stale claim time.
#   outbox\<jobId>.result.json - agent writes {exitCode,stdout,stderr,timing}
#   processed\              - consumed job scripts (and their meta.json, if any) are moved here
#   logs\                   - per-job stdout/stderr capture
#   heartbeat.txt           - rewritten every poll so the VM can prove liveness
#
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (fable/sol major 2): this is the repository's ONLY
# tracked agent, and it is a DELIBERATELY MINIMAL double of the richer, untracked agent deployed at
# \\bachelor\mlv-agent\ultra-magnus-agent.ps1 (which additionally has queue-age expiry). This
# tracked copy reads ONLY inbox\<jobId>.meta.json's timeoutSec, in the SAME 1..86400 range and the
# SAME fall-back-to-default-when-missing-or-unparseable rule as the deployed one, so -JobTimeoutSec
# below is an agent-wide FALLBACK, never the effective per-job budget when metadata is present.
#
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 6 (sol major 1): this copy now WRITES the same
# running\<jobId>.started.json claim marker the deployed copy does. Without it, both production
# wrapper call sites (attr3-footage-stage.ps1) set -MaxQueueWaitSec equal to -TimeoutSec, so a
# full-budget job's client never saw a claim signal, stayed on its submission-anchored QUEUED
# deadline for its entire wait, and could throw before the agent -- whose own deadline starts only
# at claim, strictly after submission -- ever published its timeout receipt. Writing the marker
# closes that gap the same way the deployed agent already does, rather than asking every client to
# reason about a queue ceiling that cannot itself outlast an execution ceiling it has no visibility
# into.
#
# Security: this intentionally executes scripts dropped into inbox\. It is a
# private automation channel on the user's own LAN/host/account. Stop it by
# ending the scheduled task / process (see install-ultra-magnus-agent.ps1).

[CmdletBinding()]
param(
    [string]$Root = $PSScriptRoot,
    [int]$PollSeconds = 2,
    [ValidateRange(1, 86400)]
    [int]$JobTimeoutSec = 1800,
    [switch]$SelfTestIdentityGuard
)

$ErrorActionPreference = "Continue"
$inbox     = Join-Path $Root "inbox"
$outbox    = Join-Path $Root "outbox"
$processed = Join-Path $Root "processed"
$running   = Join-Path $Root "running"
$logs      = Join-Path $Root "logs"
$heartbeat = Join-Path $Root "heartbeat.txt"
foreach ($d in @($inbox, $outbox, $processed, $running, $logs)) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}

# Prefer pwsh 7 for running jobs; fall back to Windows PowerShell if absent.
$psExe = (Get-Command pwsh.exe -ErrorAction SilentlyContinue).Source
if (-not $psExe -and (Test-Path "$env:ProgramFiles\PowerShell\7\pwsh.exe")) { $psExe = "$env:ProgramFiles\PowerShell\7\pwsh.exe" }
if (-not $psExe) { $psExe = "powershell.exe" }

function Get-ProcessIdentity {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    try {
        $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        if (-not $process) { throw "process not found" }
        $startUtc = if ($process.CreationDate) {
            ([DateTime]$process.CreationDate).ToUniversalTime().ToString("o")
        } else { $null }
        $startEpochMs = if ($startUtc) { [DateTimeOffset]::Parse($startUtc).ToUnixTimeMilliseconds() } else { $null }
        return [pscustomobject]@{
            exists = $true
            processId = [int]$process.ProcessId
            startUtc = $startUtc
            startEpochMs = $startEpochMs
            imagePath = $process.ExecutablePath
        }
    }
    catch {
        return [pscustomobject]@{
            exists = $false
            processId = $ProcessId
            startUtc = $null
            startEpochMs = $null
            imagePath = $null
        }
    }
}

function Get-StartedProcessIdentity {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)

    # Diagnostic fallback only. A .NET Process object cannot supply the CIM
    # image-path credential used by the kill guard, so it must never authorize
    # monitoring or termination on its own.
    try {
        $startUtc = $Process.StartTime.ToUniversalTime().ToString("o")
        return [pscustomobject]@{
            exists = $false
            processId = [int]$Process.Id
            startUtc = $startUtc
            startEpochMs = [DateTimeOffset]::Parse($startUtc).ToUnixTimeMilliseconds()
            imagePath = $null
            reason = "cim-identity-unavailable"
        }
    }
    catch {
        return [pscustomobject]@{
            exists = $false
            processId = [int]$Process.Id
            startUtc = $null
            startEpochMs = $null
            imagePath = $null
            reason = "identity-unavailable"
        }
    }
}

function Test-EpochMillisEqual {
    param($ActualEpochMs, $ExpectedEpochMs)

    try {
        if ($null -eq $ActualEpochMs -or $null -eq $ExpectedEpochMs) { return $false }
        # Both values come from Win32_Process.CreationDate and are normalized
        # to integer milliseconds, so no cross-source tolerance is required.
        return ([Int64]$ActualEpochMs -eq [Int64]$ExpectedEpochMs)
    }
    catch { return $false }
}

function Test-ProcessIdentityMatch {
    param(
        [Parameter(Mandatory = $true)][int]$ProcessId,
        [string]$ExpectedStartUtc,
        $ExpectedStartEpochMs,
        [string]$ExpectedImagePath
    )

    $identity = Get-ProcessIdentity -ProcessId $ProcessId
    if (-not $identity.exists) {
        return [pscustomobject]@{ matches = $false; identity = $identity; reason = "not-running" }
    }
    $hasExpectedStart = ($null -ne $ExpectedStartEpochMs) -or (-not [string]::IsNullOrWhiteSpace($ExpectedStartUtc))
    $hasExpectedImage = -not [string]::IsNullOrWhiteSpace($ExpectedImagePath)
    if (-not $hasExpectedStart -and -not $hasExpectedImage) {
        return [pscustomobject]@{
            matches = $false
            identity = $identity
            reason = "missing-expected-identity-credentials"
            startMatches = $false
            imageMatches = $false
        }
    }

    $startMatches = $true
    if ($null -ne $ExpectedStartEpochMs) {
        $startMatches = Test-EpochMillisEqual -ActualEpochMs $identity.startEpochMs -ExpectedEpochMs $ExpectedStartEpochMs
    }
    elseif (-not [string]::IsNullOrWhiteSpace($ExpectedStartUtc)) {
        $startMatches = ([string]$identity.startUtc -eq [string]$ExpectedStartUtc)
    }
    $imageMatches = if ($hasExpectedImage) {
        [string]::Equals($identity.imagePath, $ExpectedImagePath, [StringComparison]::OrdinalIgnoreCase)
    } else { $true }
    $reason = if ($startMatches -and $imageMatches) { "matched" } elseif (-not $startMatches) { "start-mismatch" } else { "image-mismatch" }
    return [pscustomobject]@{
        matches = ($startMatches -and $imageMatches)
        identity = $identity
        reason = $reason
        startMatches = $startMatches
        imageMatches = $imageMatches
    }
}

function Test-ProcessCreationAfterParent {
    param([Parameter(Mandatory = $true)]$ChildIdentity, [Parameter(Mandatory = $true)]$ParentIdentity)

    try {
        if ($null -eq $ChildIdentity.startEpochMs -or $null -eq $ParentIdentity.startEpochMs) { return $false }
        return ([Int64]$ChildIdentity.startEpochMs -ge [Int64]$ParentIdentity.startEpochMs)
    }
    catch { return $false }
}

function Get-DescendantProcessIdentities {
    param([Parameter(Mandatory = $true)]$ParentIdentity)

    $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($ParentIdentity.processId)" -ErrorAction SilentlyContinue)
    foreach ($child in $children) {
        $childIdentity = Get-ProcessIdentity -ProcessId ([int]$child.ProcessId)
        if (-not $childIdentity.exists) { continue }
        if (-not (Test-ProcessCreationAfterParent -ChildIdentity $childIdentity -ParentIdentity $ParentIdentity)) {
            continue
        }
        Get-DescendantProcessIdentities -ParentIdentity $childIdentity
        $childIdentity
    }
}

function Stop-ProcessTree {
    param(
        [Parameter(Mandatory = $true)]$RootIdentity,
        [AllowEmptyCollection()][object[]]$KnownIdentities = @()
    )

    $rootMatch = Test-ProcessIdentityMatch -ProcessId ([int]$RootIdentity.processId) -ExpectedStartUtc $RootIdentity.startUtc -ExpectedStartEpochMs $RootIdentity.startEpochMs -ExpectedImagePath $RootIdentity.imagePath
    if (-not $rootMatch.matches) { return @() }

    $identities = [System.Collections.Generic.List[object]]::new()
    $seen = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($expected in @($KnownIdentities) + @(Get-DescendantProcessIdentities -ParentIdentity $rootMatch.identity)) {
        if ($null -eq $expected -or -not $expected.exists) { continue }
        $key = "$($expected.processId):$($expected.startEpochMs):$($expected.startUtc)"
        if ($seen.Add($key)) { [void]$identities.Add($expected) }
    }
    [void]$identities.Add($rootMatch.identity)

    $killed = [System.Collections.Generic.List[int]]::new()
    foreach ($expected in $identities) {
        $match = Test-ProcessIdentityMatch -ProcessId ([int]$expected.processId) -ExpectedStartUtc $expected.startUtc -ExpectedStartEpochMs $expected.startEpochMs -ExpectedImagePath $expected.imagePath
        if (-not $match.matches) { continue }
        try {
            Stop-Process -Id ([int]$expected.processId) -Force -ErrorAction Stop
            [void]$killed.Add([int]$expected.processId)
        }
        catch {
            # The process may already have exited while the verified set was stopped.
        }
    }
    return @($killed)
}

if ($SelfTestIdentityGuard) {
    $probe = Test-ProcessIdentityMatch -ProcessId $PID
    $probe | ConvertTo-Json -Compress
    if ($probe.matches -or $probe.reason -ne "missing-expected-identity-credentials") { exit 1 }
    exit 0
}

function Quote-ProcessArgument {
    param([Parameter(Mandatory = $true)][string]$Value)
    return '"' + ($Value -replace '"', '\"') + '"'
}

function Get-JobEffectiveTimeoutSec {
    <#
    ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (fable/sol major 2). Mirrors the deployed agent's
    Get-JobMetadata timeoutSec handling: 1..86400, falling back to $DefaultTimeoutSec when the file
    is missing, unreadable, or the value is absent/unparseable/out of range. Never throws -- a
    malformed metadata file must degrade to the agent's own default, not abort the job.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$MetaPath,
        [Parameter(Mandatory = $true)][int]$DefaultTimeoutSec,
        [string]$ErrFile = $null
    )

    if (-not (Test-Path -LiteralPath $MetaPath -PathType Leaf)) { return $DefaultTimeoutSec }
    try {
        $meta = Get-Content -LiteralPath $MetaPath -Raw | ConvertFrom-Json
    } catch {
        if ($ErrFile) {
            "failed to parse job metadata '$MetaPath': $($_.Exception.Message)" | Add-Content -Encoding ASCII $ErrFile
        }
        return $DefaultTimeoutSec
    }
    $parsed = 0
    if ($null -eq $meta.timeoutSec -or -not [int]::TryParse([string]$meta.timeoutSec, [ref]$parsed) -or $parsed -lt 1 -or $parsed -gt 86400) {
        return $DefaultTimeoutSec
    }
    return $parsed
}

function Write-AgentHeartbeat {
    param([string]$Activity = "")

    $line = "alive $((Get-Date).ToString('o')) pid=$PID host=$env:COMPUTERNAME"
    if (![string]::IsNullOrWhiteSpace($Activity)) {
        $line += " $Activity"
    }
    $line | Set-Content -Encoding ASCII $heartbeat
}

"agent start $((Get-Date).ToString('o')) pid=$PID host=$env:COMPUTERNAME root=$Root shell=$psExe" |
    Add-Content -Encoding ASCII (Join-Path $logs "agent.log")

while ($true) {
    # Liveness heartbeat (VM checks this file's age before submitting).
    Write-AgentHeartbeat

    $jobs = Get-ChildItem $inbox -Filter *.job.ps1 -File -ErrorAction SilentlyContinue | Sort-Object Name
    foreach ($job in $jobs) {
        $jobId   = $job.BaseName -replace '\.job$', ''
        $outFile = Join-Path $logs "$jobId.out.txt"
        $errFile = Join-Path $logs "$jobId.err.txt"
        $started = (Get-Date).ToUniversalTime().ToString("o")
        $startedMarkerPath = Join-Path $running "$jobId.started.json"
        # sol round 6 major 1: written the instant this job is claimed (dequeued off the inbox),
        # before any per-job setup below -- um-run.ps1's client switches to a claim-anchored
        # deadline the moment this file appears, so claiming later than this line would leave the
        # same submission-anchored race window open for whatever runs between here and the write.
        [pscustomobject]@{ jobId = $jobId; startedUtc = $started } |
            ConvertTo-Json -Compress | Set-Content -Encoding ASCII $startedMarkerPath
        $exit    = $null
        $timedOut = $false
        $killedProcessIds = @()
        $rootIdentitySource = $null
        $rootIdentityHasImage = $false
        $rangeHeadSha = $null
        $llrawprocBlobId = $null
        $pendingSymbolPresence = $null
        $dllSha256 = $null
        Remove-Item -LiteralPath $outFile, $errFile -Force -ErrorAction SilentlyContinue

        # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (fable/sol major 2): honour a per-job budget
        # from inbox\<jobId>.meta.json (written by um-run.ps1 BEFORE the job becomes visible, so it
        # is always here by the time this agent claims the job), falling back to this agent's own
        # -JobTimeoutSec when the file is absent or the value is unparseable/out of range.
        $metaPath = Join-Path $inbox "$jobId.meta.json"
        $effectiveTimeoutSec = Get-JobEffectiveTimeoutSec -MetaPath $metaPath -DefaultTimeoutSec $JobTimeoutSec -ErrFile $errFile

        # Run the dropped script in a child PowerShell with a wall-clock timeout
        # so one hung job cannot freeze the agent. Output goes to files (no pipe
        # deadlock). On timeout, kill the verified root plus descendants seen
        # while their ancestry is observable. A future Windows Job Object pass
        # is needed to cover children that spawn and orphan within one poll gap.
        $jobProcess = $null
        $jobIdentity = $null
        $trackedDescendants = @{}
        try {
            $jobPathArgument = Quote-ProcessArgument -Value $job.FullName
            $jobProcess = Start-Process -FilePath $psExe `
                -ArgumentList @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $jobPathArgument) `
                -RedirectStandardOutput $outFile `
                -RedirectStandardError $errFile `
                -WindowStyle Hidden `
                -PassThru

            $jobIdentity = Get-ProcessIdentity -ProcessId $jobProcess.Id
            if (-not $jobIdentity.exists -or $null -eq $jobIdentity.startEpochMs -or [string]::IsNullOrWhiteSpace($jobIdentity.imagePath)) {
                $fallbackIdentity = Get-StartedProcessIdentity -Process $jobProcess
                $fallbackStopped = $jobProcess.HasExited
                try {
                    if (-not $fallbackStopped) {
                        $jobProcess.Kill($true)
                        [void]$jobProcess.WaitForExit(5000)
                        $fallbackStopped = $jobProcess.HasExited
                    }
                } catch { }
                if (-not $fallbackStopped) {
                    throw "Failed closed: CIM root identity is incomplete and handle-based termination was not confirmed ($($fallbackIdentity.reason))."
                }
                throw "Failed closed: CIM root identity is incomplete ($($fallbackIdentity.reason))."
            }
            $rootIdentitySource = "Win32_Process"
            $rootIdentityHasImage = $true

            $deadline = (Get-Date).AddSeconds($effectiveTimeoutSec)
            $waitSliceMs = [Math]::Max(250, [Math]::Min(5000, $PollSeconds * 1000))
            while (!$jobProcess.HasExited -and (Get-Date) -lt $deadline) {
                foreach ($descendant in @(Get-DescendantProcessIdentities -ParentIdentity $jobIdentity)) {
                    $trackedDescendants["$($descendant.processId):$($descendant.startEpochMs)"] = $descendant
                }
                Write-AgentHeartbeat -Activity "job=$jobId"
                $remainingMs = [Math]::Max(1, [int][Math]::Min($waitSliceMs, ($deadline - (Get-Date)).TotalMilliseconds))
                [void]$jobProcess.WaitForExit($remainingMs)
            }

            if ($jobProcess.HasExited) {
                $exit = $jobProcess.ExitCode
            }
            else {
                $timedOut = $true
                $exit = 124
                $killedProcessIds = @(Stop-ProcessTree -RootIdentity $jobIdentity -KnownIdentities @($trackedDescendants.Values))
                "timed out after ${effectiveTimeoutSec}s; killed process tree pids=$($killedProcessIds -join ',')" |
                    Add-Content -Encoding ASCII $errFile
            }
        }
        catch {
            $exit = 1
            "agent failed to launch or monitor job: $($_.Exception.Message)" |
                Add-Content -Encoding ASCII $errFile
        }
        finally {
            if ($null -ne $jobProcess) {
                $jobProcess.Dispose()
            }
        }

        $ended  = (Get-Date).ToUniversalTime().ToString("o")

        # Optional per-job provenance sidecar. The agent has no knowledge of what a
        # job script does - it is an opaque .ps1 - so it cannot derive source-sha or
        # build-artifact bindings itself. A job that wants its result to self-bind
        # (rather than being trusted by filename adjacency to a submit-time claim it
        # never proved) writes these fields itself, mid-run, to
        # outbox\<jobId>.provenance.json using a path baked in by its own submitter
        # (which knows $AgentRoot at submit time). Absent or unparseable is $null on
        # every field, never a guess.
        $provenanceFile = Join-Path $outbox "$jobId.provenance.json"
        if (Test-Path -LiteralPath $provenanceFile) {
            try {
                $provenance = Get-Content -LiteralPath $provenanceFile -Raw | ConvertFrom-Json
                $rangeHeadSha = $provenance.rangeHeadSha
                $llrawprocBlobId = $provenance.llrawprocBlobId
                $pendingSymbolPresence = $provenance.pendingSymbolPresence
                $dllSha256 = $provenance.dllSha256
            }
            catch {
                "failed to parse job provenance sidecar '$provenanceFile': $($_.Exception.Message)" |
                    Add-Content -Encoding ASCII $errFile
            }
            Remove-Item -LiteralPath $provenanceFile -Force -ErrorAction SilentlyContinue
        }

        $stdout = if (Test-Path $outFile) { Get-Content $outFile -Raw } else { "" }
        $stderr = if (Test-Path $errFile) { Get-Content $errFile -Raw } else { "" }

        $result = [pscustomobject]@{
            jobId      = $jobId
            exitCode   = $exit
            startedUtc = $started
            endedUtc   = $ended
            host       = $env:COMPUTERNAME
            timeoutSec = $effectiveTimeoutSec
            timedOut   = $timedOut
            killedProcessIds = @($killedProcessIds)
            rootIdentitySource = $rootIdentitySource
            rootIdentityHasImage = $rootIdentityHasImage
            rootIdentityToleranceMs = 0
            rangeHeadSha = $rangeHeadSha
            llrawprocBlobId = $llrawprocBlobId
            pendingSymbolPresence = $pendingSymbolPresence
            dllSha256 = $dllSha256
            stdout     = $stdout
            stderr     = $stderr
        }
        # Atomic publish: write temp then rename so the VM never reads a partial file.
        $tmp = Join-Path $outbox "$jobId.result.tmp"
        $fin = Join-Path $outbox "$jobId.result.json"
        $result | ConvertTo-Json -Depth 6 | Set-Content -Encoding ASCII $tmp
        Move-Item -Force $tmp $fin
        # sol round 6 major 1: the claim marker's lifetime matches the job's -- removed once the
        # result is published (the instant that retires this JobId in UmRunDrop.psm1's own
        # outbox-result check), never left behind. A deterministic job id reused by a later retry
        # must never read THIS run's claim time as its own, which a stale marker would cause: the
        # client would treat it as already claimed long ago and give up almost immediately.
        Remove-Item -LiteralPath $startedMarkerPath -Force -ErrorAction SilentlyContinue
        Move-Item -Force $job.FullName (Join-Path $processed $job.Name)
        # fable/sol major 1/2: the metadata's lifetime matches the job's -- move it out of inbox\
        # alongside the job it governed, whether or not it was actually honoured this round.
        # Leaving it behind (a) accumulates unconsumed files in inbox\ forever, and (b) would brick
        # a retry that reuses this job id (UmRunDrop.psm1 refuses to place metadata over an existing
        # file).
        if (Test-Path -LiteralPath $metaPath -PathType Leaf) {
            Move-Item -Force -LiteralPath $metaPath -Destination (Join-Path $processed "$jobId.meta.json")
        }
    }

    Start-Sleep -Seconds $PollSeconds
}
