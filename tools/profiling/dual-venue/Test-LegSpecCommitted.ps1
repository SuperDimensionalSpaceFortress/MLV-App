<#
.SYNOPSIS
VENUE-LEASE-BATCH-LEGSPEC-COMMITTED-PREFLIGHT-1: asks the leg's OWN committed-spec question (Find-DvCommittedLegSpec) of the HEAD of one repo, for the lease batch's preflight.

.DESCRIPTION
Invoke-VenueLeaseBatch.ps1 runs this as a BOUNDED child (Invoke-VenueBoundedProcess, the room left before -DeadlineUtc), as it runs the export budget generator: the lookup reads every blob under
tools/profiling/dual-venue/legs/ on a cold cache (two git spawns each, about 15 s measured), which no in-process bound can interrupt, and a child that overruns is killed with its tree.
Every git call here is the module's own hardened Invoke-DvGit, the one Invoke-VenueLeg.ps1's admission uses (Resolve-DvAdmissionSources -> Get-DvCommittedFile -> HEAD): it drops an inherited
GIT_DIR / GIT_WORK_TREE / GIT_OBJECT_DIRECTORY / replace-ref override and every GIT_TRACE* target (so the commit asked about is the repo's own HEAD, and git writes nothing the caller names),
is bounded per call, and passes --no-replace-objects. A raw `git rev-parse HEAD` in the batch was none of those.

Prints ONE line and exits: `DV_LEGSPEC_COMMITTED FOUND path=<relative path> head=<sha>` (exit 0), or `DV_LEGSPEC_COMMITTED NOT_FOUND head=<sha or empty>` (exit 3: no spec under
tools/profiling/dual-venue/legs/ at HEAD hashes to -LegSpecSha256, or HEAD cannot be read). Any other end (a throw, a kill) is NOT an answer: the caller refuses it as unchecked.
-LegSpecSha256 is the line-ending-normalised hash (Get-DvLegSpecSha256), the one the leg computes from the spec it runs.
#>
param(
    [Parameter(Mandatory)][string]$RepoRoot,
    [Parameter(Mandatory)][string]$LegSpecSha256
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'DualVenueRunner.psm1') -Force -DisableNameChecking
$head = & (Get-Module DualVenueRunner) {
    param($root)
    $r = Invoke-DvGit -RepoRoot $root -GitArgs @('rev-parse', '--verify', 'HEAD')
    if ($r.exitCode -eq 0) { ([Text.Encoding]::ASCII.GetString($r.bytes)).Trim() } else { '' }
} $RepoRoot
$found = Find-DvCommittedLegSpec -RepoRoot $RepoRoot -Commit ([string]$head) -LegSpecSha256 $LegSpecSha256
if ($found.ok) { Write-Output "DV_LEGSPEC_COMMITTED FOUND path=$($found.relativePath) head=$head"; exit 0 }
Write-Output "DV_LEGSPEC_COMMITTED NOT_FOUND head=$head"
exit 3
