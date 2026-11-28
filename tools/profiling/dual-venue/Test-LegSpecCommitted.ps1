<#
.SYNOPSIS
VENUE-LEASE-BATCH-LEGSPEC-COMMITTED-PREFLIGHT-1: asks the leg's OWN committed-spec question (Find-DvCommittedLegSpec) of the HEAD of one repo, for the lease batch's preflight.

.DESCRIPTION
The lease batch script runs this as a BOUNDED child (Invoke-VenueBoundedProcess, the room left before -DeadlineUtc), as it runs the export budget generator: the lookup reads every blob under
tools/profiling/dual-venue/legs/ on a cold cache (two git spawns each, about 15 s measured), which no in-process bound can interrupt, and a child that overruns is killed with its tree.
Every git call here is the module's own hardened Invoke-DvGit, the one the leg script's admission uses (Resolve-DvAdmissionSources -> Get-DvCommittedFile -> HEAD): it drops an inherited
GIT_DIR / GIT_WORK_TREE / GIT_OBJECT_DIRECTORY / replace-ref override and every GIT_TRACE* target (so the commit asked about is the repo's own HEAD, and git writes nothing the caller names),
is bounded per call, and passes --no-replace-objects. A raw `git rev-parse HEAD` in the batch was none of those.

THE ROOT IS RESOLVED HERE, not by the batch (r2, adversary A2): the batch hands over -LegScript (the leg script it will run) and, only for the offline seam, -RepoRoot as spelled. The repo is the one the
leg that runs resolves for itself (its own <dir>\..\..\.., the leg script's -RepoRoot default); a -RepoRoot, when given, replaces it. Resolving it touches the file system (a stalled share would
stall the call), which is why it runs in this bounded child and never in the batch before -DeadlineUtc exists.

Prints ONE line and exits: `DV_LEGSPEC_COMMITTED FOUND path=<relative path> head=<sha> root=<resolved root>` (exit 0), or `DV_LEGSPEC_COMMITTED NOT_FOUND head=<sha or empty> root=<resolved root>`
(exit 3: no spec under tools/profiling/dual-venue/legs/ at HEAD hashes to -LegSpecSha256, or HEAD cannot be read), or `DV_LEGSPEC_COMMITTED NO_ROOT candidate=<what was tried>` (exit 4: the root does not
resolve to a directory). Any other end (a throw, a kill) is NOT an answer: the caller refuses it as unchecked.
-LegSpecSha256 is the line-ending-normalised hash (Get-DvLegSpecSha256), the one the leg computes from the spec it runs.
#>
param(
    [Parameter(Mandatory)][string]$LegSpecSha256,
    [string]$LegScript = '',
    [string]$RepoRoot = ''
)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'DualVenueRunner.psm1') -Force -DisableNameChecking
# r4 (adversary r3 pass 9 A1): the leg's own resolution (Resolve-DvLegRepoRoot, the one Invoke-VenueLeg.ps1 calls), not a second spelling of it
$legDir = $(if ($LegScript -and -not $RepoRoot) { Split-Path -Parent ([IO.Path]::GetFullPath($LegScript)) } else { '' })
$candidate = $(if ($RepoRoot) { $RepoRoot } elseif ($legDir) { Join-Path $legDir '..\..\..' } else { '' })
$root = ''
try {
    if ($RepoRoot) { $root = (Resolve-Path -LiteralPath $RepoRoot -ErrorAction Stop).Path }
    elseif ($legDir) { $root = Resolve-DvLegRepoRoot -ScriptDirectory $legDir }
} catch { $root = '' }
if (-not $root -or -not (Test-Path -LiteralPath $root -PathType Container)) { Write-Output "DV_LEGSPEC_COMMITTED NO_ROOT candidate=$candidate"; exit 4 }
$head = & (Get-Module DualVenueRunner) {
    param($root)
    $r = Invoke-DvGit -RepoRoot $root -GitArgs @('rev-parse', '--verify', 'HEAD')
    if ($r.exitCode -eq 0) { ([Text.Encoding]::ASCII.GetString($r.bytes)).Trim() } else { '' }
} $root
$found = Find-DvCommittedLegSpec -RepoRoot $root -Commit ([string]$head) -LegSpecSha256 $LegSpecSha256
if ($found.ok) { Write-Output "DV_LEGSPEC_COMMITTED FOUND path=$($found.relativePath) head=$head root=$root"; exit 0 }
Write-Output "DV_LEGSPEC_COMMITTED NOT_FOUND head=$head root=$root"
exit 3
