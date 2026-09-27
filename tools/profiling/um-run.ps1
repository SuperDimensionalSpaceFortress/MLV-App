# um-run.ps1
# RUN THIS ON THE VM. Submits a PowerShell script to the Ultra-Magnus file-drop
# agent over SMB, waits for the result, and returns it (exitCode/stdout/stderr).
# Requires the host agent to be running (install-ultra-magnus-agent.ps1).
#
# Usage:
#   $r = .\um-run.ps1 -ScriptPath .\some-host-job.ps1
#   $r.exitCode; $r.stdout
#   # or inline:
#   $r = .\um-run.ps1 -Command 'nvidia-smi; nvcc --version'
#   # a generated job that expects side-files next to it in inbox\ (e.g. <jobId>-source.zip):
#   $r = .\um-run.ps1 -ScriptPath .\out\<jobId>.job.ps1 -SideFile '.\out\<jobId>-source.zip' -JobId <jobId>
#
# SIDE-FILES. Generated jobs (tools/profiling/ultramagnus/playback-attr-3-cuda-dll-job.ps1,
# tools/profiling/bachelor/playback-attr-3-cuda-stage-job.ps1) read their inputs from inbox\ by
# name and verify them against hashes baked in at generation time. Placing those inputs is a
# write to the agent share, so it happens HERE, in this tracked submitter, never by hand
# (NA-7 refuses a hooked write to the share, and hiding the destination in a variable is not a
# workaround). The placement rules live in UmRunDrop.psm1 (tested with observing and faulty
# copiers): plain allowlisted names that normalise to themselves, per-submission unique temporary
# names, share-side hash verification before each rename, renames that never overwrite, and every
# side-file in place before the job is dropped. `pwsh -File` cannot pass arrays, so -SideFile also
# accepts a ';'-separated list.
#
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (hub ruling): a client cutoff must never declare
# failure while the agent may still own the job -- rounds 6-10 tried to size a cutoff that
# provably outlasts the agent, but the deployed agent has a silent kill/drain/publish window (no
# heartbeat write between its own deadline and publishing a receipt) that no heartbeat threshold
# can prove past. So every outcome this script can report is now named and made truthful by
# construction instead:
#   - RECEIPT -- $resultFile appeared; returned, as always.
#   - RETRACTED -- the queue-wait ceiling was reached, the job was never claimed, and this client
#     successfully withdrew it from the inbox before the agent could ever see it: a clean refusal,
#     never a failure.
#   - UNRESOLVED -- this client stopped waiting while the agent may still own the job (claimed, no
#     receipt, no proof of death). Not a failure and not retryable on this evidence alone: the
#     agent may still publish $resultFile after this client has already given up.
# This script never synthesizes a FAILED verdict from its own timeout: the only way this function
# ever reports a failure is a receipt that itself says so (an ordinary exitCode != 0), which is
# already the caller's business, not this wait loop's.
#
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 12 (sol BLOCKER, item 3): the four outcomes above are
# meant to be EXHAUSTIVE, but an unexpected share I/O failure after the job was made visible (a
# heartbeat vanishing between Test-Path and Get-Item, a torn share-clock probe, ...) used to
# propagate raw, past every outcome this file's own header promises, for a caller to misclassify
# CLASS=UNKNOWN and treat as an ordinary, safely-retryable failure -- while the agent might still
# own the job. The entire wait loop below is now wrapped in ONE structural try/catch: anything that
# is not already this script's own well-formed RETRACTED:/UNRESOLVED: throw is remapped to
# UNRESOLVED (after one last recheck for a receipt that may have landed despite the error), never
# left to escape raw. This is the boundary, not a per-call patch on each individual share read.

[CmdletBinding(DefaultParameterSetName = 'Script')]
param(
    [Parameter(Mandatory, ParameterSetName = 'Script')]
    [string]$ScriptPath,
    [Parameter(Mandatory, ParameterSetName = 'Command')]
    [string]$Command,
    [string]$AgentShare = "\\Ultra-Magnus\g\Temp\mlv-gpu-profile\agent",
    [int]$TimeoutSec = 1800,
    [int]$PollSeconds = 3,
    [int]$MaxHeartbeatAgeSec = 30,
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (sol major 3): how long the client waits to see the
    # job CLAIMED (running\<id>.started.json, written by a compatible agent) before giving up on it
    # ever running at all. Jobs are processed sequentially, so a job ahead in the queue can delay a
    # claim by up to its own budget -- as much as 86400s, the same max any caller can request -- so
    # that is the default. Once claimed, patience is governed by -TimeoutSec + the grace below
    # instead, measured from the claim, not from submission.
    [int]$MaxQueueWaitSec = 86400,
    [string[]]$SideFile = @(),
    # Keep a generator's own job id (so outbox\<JobId>.result.json and its artifacts line up).
    [string]$JobId = '',
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 10: once past claimedAt + -TimeoutSec, this client
    # keeps waiting as long as the agent may still own the job (proof of liveness, OR -- round 11 --
    # its own started marker still sitting in running\, proof it has not yet finished with this job
    # either way). This is the ABSOLUTE outer ceiling on top of that: even a job the agent may still
    # own is only trusted for this much longer than its own requested budget before this client
    # stops regardless (UNRESOLVED, never FAILED -- see this file's own header). Default mirrors
    # -MaxQueueWaitSec's own reasoning (a bound sized to outlast a lot of legitimate extra work, not
    # a guessed round number).
    [int]$MaxClaimedWaitSec = 86400,
    # Test-only: invoked with no arguments the instant the queue deadline is judged reached and no
    # claim has been seen yet, immediately BEFORE the recheck that follows it -- lets a test land a
    # claim marker deterministically inside what is otherwise a sub-millisecond window between
    # this script's last (negative) marker check and its final diagnosis (round 7, sol blocker).
    # Production never passes this.
    [scriptblock]$TestHookAtQueueDeadline = $null,
    # Test-only: invoked with no arguments immediately after this client's own retraction rename
    # attempt (round 11, item 2), whether it succeeded or failed, BEFORE the marker recheck that
    # follows it -- lets a test land a claim marker deterministically inside the retraction race,
    # the same way -TestHookAtQueueDeadline already does for the marker-appears-first race.
    # Production never passes this.
    [scriptblock]$TestHookAfterRetractionRename = $null,
    # Test-only: invoked with the retracted job's own renamed-out temporary path, immediately after
    # that rename SUCCEEDS and BEFORE this client attempts to remove it (round 12, item 5) -- lets a
    # test lock that exact, GUID-named path (unknowable in advance to an external caller) so the
    # removal that follows can be made to fail deterministically, proving the failure is surfaced
    # rather than silently swallowed. Production never passes this.
    [scriptblock]$TestHookAfterRetractionJobRenamed = $null,
    # Test-only: invoked with no arguments immediately BEFORE the receipt recheck that guards the
    # absolute-outer-ceiling throw -- lets a test plant a receipt in that exact window to prove the
    # recheck is load-bearing (round 11, fable minor). Production never passes this.
    [scriptblock]$TestHookBeforeOuterCeilingReceiptRecheck = $null,
    # Test-only: same, for the receipt recheck that guards the liveness-lost throw. Production
    # never passes this.
    [scriptblock]$TestHookBeforeLivenessLostReceiptRecheck = $null,
    # Test-only: invoked with no arguments at the very top of every wait-loop iteration, before
    # even the ordinary receipt check -- lets a test inject an arbitrary, unanticipated exception
    # (round 12, item 3) to prove the structural catch around the whole loop converts it to
    # UNRESOLVED rather than letting it propagate raw or be misclassified CLASS=UNKNOWN by a
    # caller. Production never passes this.
    [scriptblock]$TestHookAtLoopTop = $null
)

$ErrorActionPreference = "Stop"
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (sol major 3): 0 is a sentinel INSIDE UmRunDrop.psm1
# ("write no metadata"; an internal contract used directly by tests), but forwarding it here from a
# public caller collided with the grace formula below -- a caller asking for "no budget" got a
# 5-SECOND client patience while a compatible agent quietly fell back to ITS OWN default (which can
# be 1800s or more). The public contract is therefore explicit: 0 has no defined per-job budget and
# is refused here, before it ever reaches the module.
if ($TimeoutSec -le 0) {
    throw "UMRUN_TIMEOUT_SEC_INVALID -TimeoutSec must be a positive integer (1..86400); 0 has no defined client patience"
}
Import-Module (Join-Path $PSScriptRoot 'UmRunDrop.psm1') -Force
$inbox   = Join-Path $AgentShare "inbox"
$outbox  = Join-Path $AgentShare "outbox"
$running = Join-Path $AgentShare "running"
$hb      = Join-Path $AgentShare "heartbeat.txt"

# Liveness gate: never submit into a dead agent (mirrors the bridge Monitor lesson).
if (-not (Test-Path $hb)) { throw "No agent heartbeat at $hb - run install-ultra-magnus-agent.ps1 on the host first." }
$age = [math]::Abs(((Get-Date).ToUniversalTime() - (Get-Item $hb).LastWriteTimeUtc).TotalSeconds)
if ($age -gt $MaxHeartbeatAgeSec) { Write-Warning ("agent heartbeat is {0:N0}s old - it may be down" -f $age) }

# Build the job script body.
if ($PSCmdlet.ParameterSetName -eq 'Command') {
    $tmpSrc = [System.IO.Path]::GetTempFileName() + ".ps1"
    Set-Content -Encoding ASCII $tmpSrc $Command
    $ScriptPath = $tmpSrc
}
if (-not (Test-Path $ScriptPath)) { throw "Script not found: $ScriptPath" }

$dropLines = @(Invoke-UmRunDrop -Inbox $inbox -Outbox $outbox -ScriptPath $ScriptPath -JobId $JobId -SideFile $SideFile -JobTimeoutSec $TimeoutSec)
$jobId = $null
$nonce = $null
foreach ($line in $dropLines) {
    if ($line -like 'UMRUN_JOBID=*') { $jobId = $line.Substring('UMRUN_JOBID='.Length) }
    elseif ($line -like 'UMRUN_NONCE=*') { $nonce = $line.Substring('UMRUN_NONCE='.Length) }
    else { Write-Host $line }
}
if (-not $jobId) { throw "UmRunDrop returned no job id" }
if (-not $nonce) { throw "UmRunDrop returned no claim nonce" }

$jobFile       = Join-Path $inbox "$jobId.job.ps1"
$resultFile    = Join-Path $outbox "$jobId.result.json"
$startedMarker = Join-Path $running "$jobId.started.json"

# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 2 (fable/sol major 3): the agent's own deadline starts
# when it CLAIMS the job, not when this client submitted it, and jobs on a shared agent are
# processed sequentially -- a job ahead in the queue can delay the claim by up to its own budget.
# Anchoring the client's deadline to submission time (as before) makes it race and lose against a
# merely-queued job, then throw a message that affirmatively misdiagnoses a healthy, still-running
# job as a dead agent. So there are two phases, and the client always knows which one it is in:
#   - QUEUED: waiting to see running\<jobId>.started.json, a marker a compatible agent writes the
#     instant it claims the job (read here rather than guessed at). Ceiling: $MaxQueueWaitSec, then
#     RETRACTED (round 11).
#   - CLAIMED: once the marker appears, patience past the job's own -TimeoutSec no longer comes
#     from a constant -- the liveness wait below, ending in UNRESOLVED, never FAILED.
#
# ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (sol MAJOR + fable MAJOR, item 5c): every deadline
# below is a [DateTimeOffset], compared only against other [DateTimeOffset]s. [DateTimeOffset]
# comparisons are defined on the absolute instant represented, regardless of the value's own
# offset -- unlike plain [DateTime], whose comparison operators compare raw ticks and silently
# ignore Kind, so a Local-kind "now" and a Utc-kind "now" for the very same instant compare as
# UNEQUAL by exactly the host's own UTC offset. That ambiguity is what let a hypothetical
# regression (building a deadline from the CLAIM MARKER's own agent-host timestamp instead of this
# client's own observation -- the exact round-7 bug class) escape detection on some host
# timezones and not others (round 10/11 sol+fable minor). Using [DateTimeOffset] throughout removes
# the ambiguity at its source rather than trying to out-guess it in a test.
$submittedAt    = [DateTimeOffset]::Now
$queueDeadline  = $submittedAt.AddSeconds($MaxQueueWaitSec)
$claimedAt      = $null
$budgetDeadline = $null

function Get-UmRunResultIfPresent {
    param([string]$Path)
    if (Test-Path -LiteralPath $Path) {
        Start-Sleep -Milliseconds 400   # let the atomic rename settle
        return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    }
    return $null
}

try {
while ($true) {
    if ($TestHookAtLoopTop) { & $TestHookAtLoopTop }
    $r = Get-UmRunResultIfPresent -Path $resultFile
    if ($null -ne $r) { return $r }

    if ($null -eq $claimedAt -and (Test-Path -LiteralPath $startedMarker)) {
        $claimedAt      = [DateTimeOffset]::Now
        $budgetDeadline = $claimedAt.AddSeconds($TimeoutSec)
    }

    if ($null -ne $claimedAt) {
        # CLAIMED phase. THE THREE OUTCOMES from here: (1) a receipt appears -- returned above on
        # the next iteration; (2) UNRESOLVED because liveness is lost AND this job's own started
        # marker is also gone; (3) UNRESOLVED because the absolute outer ceiling is reached. Neither
        # (2) nor (3) is ever reported as a failure -- see this file's own header.
        if ([DateTimeOffset]::Now -ge $budgetDeadline) {
            $outerCeiling = $claimedAt.AddSeconds($TimeoutSec + $MaxClaimedWaitSec)
            if ([DateTimeOffset]::Now -ge $outerCeiling) {
                if ($TestHookBeforeOuterCeilingReceiptRecheck) { & $TestHookBeforeOuterCeilingReceiptRecheck }
                $r = Get-UmRunResultIfPresent -Path $resultFile
                if ($null -ne $r) { return $r }
                throw "UNRESOLVED: $jobId was claimed by the agent and has kept proving liveness past its own budget (${TimeoutSec}s) for the full -MaxClaimedWaitSec (${MaxClaimedWaitSec}s) -- this client is stopping at its absolute outer ceiling, not failing: the agent may still own $jobId and its receipt may still land at $resultFile after this; not retryable on this evidence alone"
            }
            $liveness = Get-UmRunAgentLiveness -HeartbeatPath $hb -Inbox $inbox -JobId $jobId -MaxHeartbeatAgeSec $MaxHeartbeatAgeSec
            if (-not $liveness.Fresh) {
                if (Test-Path -LiteralPath $startedMarker) {
                    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (item 3): the deployed agent
                    # writes NO heartbeat between its own deadline and publishing a receipt
                    # (Stop-ProcessTree, stream drain, and Publish-JobResult are all silent) -- a
                    # window that can outlast a stale or job-mismatched heartbeat on its own.
                    # running\<jobId>.started.json is proof the agent still owns this job
                    # regardless: the ONLY code path that ever removes it (Complete-StartedMarker,
                    # in both the deployed agent and the tracked double) runs strictly AFTER a
                    # receipt is durably published to $resultFile -- so the marker's mere presence
                    # is still "may still own the job", never lost liveness. This never overrides
                    # the absolute outer ceiling above; it only means THIS particular cutoff does
                    # not fire THIS particular poll.
                } else {
                    if ($TestHookBeforeLivenessLostReceiptRecheck) { & $TestHookBeforeLivenessLostReceiptRecheck }
                    $r = Get-UmRunResultIfPresent -Path $resultFile
                    if ($null -ne $r) { return $r }
                    if ($liveness.JobMismatch) {
                        throw "UNRESOLVED: $jobId was claimed by the agent, but heartbeat.txt now names a DIFFERENT job ($($liveness.OtherJobId)) and its own started marker is gone too -- the agent has moved on without a receipt for $jobId that this client can see; this client is stopping, not failing: $resultFile may still land later if this is a stale read; not retryable on this evidence alone"
                    }
                    if ($null -eq $liveness.HeartbeatUtc) {
                        throw "UNRESOLVED: $jobId was claimed by the agent, but $hb no longer exists and its own started marker is gone too -- this client is stopping, not failing: the agent may still own $jobId and its receipt may still land at $resultFile after this; not retryable on this evidence alone"
                    }
                    throw "UNRESOLVED: $jobId was claimed by the agent, but it stopped proving liveness: $hb was last written at $($liveness.HeartbeatUtc.ToString('o')) (the share's own clock), now $([int]$liveness.AgeSec)s old -- past the -MaxHeartbeatAgeSec $MaxHeartbeatAgeSec s freshness threshold, and its own started marker is gone too -- this client is stopping, not failing: the agent may still own $jobId and its receipt may still land at $resultFile after this; not retryable on this evidence alone"
                }
            }
        }
    } else {
        # QUEUED phase.
        if ([DateTimeOffset]::Now -ge $queueDeadline) {
            # round 7 (sol blocker): the comment above has always promised a receipt-OR-claim
            # recheck, but a plain break was the only path out of the loop and it never rechecked
            # the claim marker -- so a claim landing after the Test-Path above found nothing, but
            # before this recheck completes, was still reported as "never claimed" for a job the
            # agent had, in fact, just started. One more look at the marker right here: if it has
            # now appeared, switch onto the CLAIMED phase (one more loop iteration) instead of
            # ending the loop.
            if ($TestHookAtQueueDeadline) { & $TestHookAtQueueDeadline }
            if (Test-Path -LiteralPath $startedMarker) {
                $claimedAt      = [DateTimeOffset]::Now
                $budgetDeadline = $claimedAt.AddSeconds($TimeoutSec)
                continue
            }

            # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11 (sol BLOCKER, item 2): the queue ceiling
            # used to just end the loop here, leaving the job sitting in the inbox for the agent to
            # claim and execute LATER -- after this client had already reported failure to ITS OWN
            # caller, a receipt or side effect the caller never saw coming. Instead, RETRACT:
            # atomically rename the job file itself out of the inbox. A compatible agent never
            # renames or moves a job file except at completion (Move-JobArtifacts / the tracked
            # double's own final Move-Item), long after claiming it -- so if this rename succeeds
            # AND the marker still never appears, the job really was never claimed, and this run
            # never happened from the agent's point of view. If the rename fails, or the marker
            # appears anyway (the agent won the race in the instant between this client's last
            # negative check and its own rename), this client cannot prove retraction -- so it
            # never asserts one, and falls through to the claimed wait instead.
            $retractedTmp = Join-Path $inbox "$jobId.$([guid]::NewGuid().ToString('N')).retracted.tmp"
            $renamed = $false
            $jobTempCleanupNote = ''
            try {
                Move-Item -LiteralPath $jobFile -Destination $retractedTmp -ErrorAction Stop   # no -Force
                $renamed = $true
            } catch {
                $renamed = $false
            }
            if ($renamed) {
                # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 12 (sol MAJOR, item 5): a failure to
                # remove this client's OWN renamed-out temporary copy used to be silently swallowed
                # (SilentlyContinue) -- surfaced below the same way the module's own rollback
                # already surfaces an equivalent cleanup failure ("may outlive this refused
                # submission"). -TestHookAfterRetractionJobRenamed hands a test the exact,
                # GUID-named path (unknowable to an external caller in advance) so it can be locked
                # deterministically to prove this.
                if ($TestHookAfterRetractionJobRenamed) { & $TestHookAfterRetractionJobRenamed $retractedTmp }
                try {
                    Remove-Item -LiteralPath $retractedTmp -Force -ErrorAction Stop
                } catch {
                    $jobTempCleanupNote = " -- additionally, the withdrawn job's own renamed-out temporary copy could not be removed and may outlive this refused submission"
                }
            }
            if ($TestHookAfterRetractionRename) { & $TestHookAfterRetractionRename }
            $stillClaimed = Test-Path -LiteralPath $startedMarker
            if ($renamed -and -not $stillClaimed) {
                # Genuinely never claimed (as of this recheck): clean up this submission's own claim
                # metadata, by nonce -- never a blind delete (the same invariant UmRunDrop.psm1's own
                # rollback now enforces on its post-claim failure path). round 12 (sol MAJOR + fable
                # MINOR, item 5): a failed nonce read or a failed delete used to be silently
                # swallowed (an empty catch, then SilentlyContinue) -- both are now surfaced, exactly
                # mirroring the module rollback's own read-then-delete shape and its own comment on
                # the narrow window a failed read still leaves open.
                $metaCleanupNote = ''
                $metaFinal = Join-Path $inbox "$jobId.meta.json"
                if (Test-Path -LiteralPath $metaFinal) {
                    $ownsClaim = $true
                    $readSucceeded = $false
                    try {
                        $currentMeta = (Get-Content -LiteralPath $metaFinal -Raw -Encoding ascii) | ConvertFrom-Json
                        $readSucceeded = $true
                        $ownsClaim = ($null -ne $currentMeta) -and ($currentMeta.nonce -eq $nonce)
                    } catch {
                        $readSucceeded = $false
                    }
                    if ($readSucceeded -and -not $ownsClaim) {
                        # Not this submission's own claim (an operator manually cleared it and
                        # resubmitted the same JobId) -- left untouched, never a blind delete.
                    } else {
                        try {
                            Remove-Item -LiteralPath $metaFinal -Force -ErrorAction Stop
                        } catch {
                            $metaCleanupNote = " -- additionally, inbox\$jobId.meta.json could not be removed during retraction and may outlive this refused submission"
                        }
                    }
                }
                $queuedElapsedSec = [int]([DateTimeOffset]::Now - $submittedAt).TotalSeconds
                # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 12 (sol BLOCKER / fable MINOR, item 1):
                # this client's own rename proves the job's CONTENT can never execute now (its
                # bytes are gone from the path any agent would open by name), but it does NOT prove
                # the agent never saw this JobId at all -- a compatible agent lists its inbox once
                # per poll and processes that snapshot sequentially (verified directly against both
                # the tracked and deployed agents), so a claim marker for this id, landing from a
                # snapshot taken before this rename, can still appear afterward, and the agent can
                # still publish an honest launch-failure receipt for it. Disclosed here rather than
                # asserting a stronger "nothing was submitted" than this client can actually prove.
                throw "RETRACTED: $jobId reached its queue wait ceiling (-MaxQueueWaitSec ${MaxQueueWaitSec}s, ${queuedElapsedSec}s elapsed) with no $startedMarker marker ever appearing as of this client's own recheck, so this client withdrew it from the inbox before it could prove a claim -- the job's own script content can never execute now (its bytes are already withdrawn), but the agent's own per-poll inbox listing can be stale: a late claim marker and an honest launch-failure receipt for $jobId can still appear afterward, which does not contradict this message; not a failure, retry with a new -JobId if desired${jobTempCleanupNote}${metaCleanupNote}"
            }
            # Either the rename failed (something else already has this path -- almost certainly
            # the agent, mid-claim), or it succeeded but the marker appeared anyway (the agent
            # claimed it in the very same instant; this client's own rename may now make the
            # agent's own launch fail, but that becomes the agent's own honestly-reported receipt,
            # never this client's assertion). Either way: never declare retraction or failure while
            # the job might already be running -- switch to the claimed wait.
            $claimedAt      = [DateTimeOffset]::Now
            $budgetDeadline = $claimedAt.AddSeconds($TimeoutSec)
            continue
        }
    }
    Start-Sleep -Seconds $PollSeconds
}
} catch {
    $loopErrorMessage = [string]$_.Exception.Message
    if ($loopErrorMessage -match '^(RETRACTED|UNRESOLVED):') {
        # Already one of this script's own well-formed, exhaustive outcomes -- pass through
        # unchanged, never re-wrapped.
        throw
    }
    # ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 12 (sol BLOCKER, item 3): an unanticipated share I/O
    # failure after the job was made visible (see this file's own header) -- one last look for a
    # receipt that may have landed despite it (itself guarded: a torn read while checking must not
    # crash this fallback), then UNRESOLVED, never left to escape raw or be misclassified
    # CLASS=UNKNOWN by a caller while the agent may still own the job.
    try {
        $r = Get-UmRunResultIfPresent -Path $resultFile
    } catch {
        $r = $null
    }
    if ($null -ne $r) { return $r }
    throw "UNRESOLVED: $jobId -- this client hit an unexpected error while waiting for its outcome ($($_.Exception.GetType().Name)) and is stopping, not failing: the agent may still own $jobId and its receipt may still land at $resultFile after this; not retryable on this evidence alone"
}
