# attr3-stage-fixture-job.ps1 -- GENERATOR (runs locally; nothing here runs on the measurement
# host). Emits a job that publishes ONE tracked repository fixture clip into the Bachelor agent
# cache, so the ATTR-3 chain can be rehearsed without owner footage (ATTR3-FIXTURE-REHEARSAL-1).
#
# WHY A SEPARATE GENERATOR. playback-attr-3-cuda-stage-job.ps1 derives its four artifact names from
# the build's sourceCommit and matches them exactly; a clip is not one of them and must not weaken
# that check. This generator stages exactly one file, addressed by its exact baked name.
#
# WHAT IS BAKED AND WHAT IS CHECKED. The generator bakes the fixture's basename and sha256. The
# emitted job re-hashes the arriving inbox copy against that value, proves every path is a direct
# child of the directory it belongs to, publishes through the slot-checked helpers (copy to
# <name>.partial, verify, rename), and only then removes the inbox copy. Nothing is enumerated,
# globbed or reconstructed from a string.
#
# FOOTAGE. The only admissible input is a clip fixture TRACKED IN THIS REPOSITORY under
# tests/fixtures/clips, which the generator proves with Test-UmRunTrackedFixtureSource before it
# emits anything: the same predicate tools/profiling/um-run.ps1 uses to place the side-file. No
# owner footage is nameable here, and no consent receipt is involved.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\attr3-stage-fixture-job.ps1 `
#       -ClipStem tiny_dual_iso -FixturePath <repo>\tests\fixtures\clips\<file> -OutDir <staging-dir>
# then submit with tools\profiling\um-run.ps1 -SideFile <the same file> -JobId <jobId>
#   -AgentShare \\bachelor\mlv-agent

[CmdletBinding()]
param(
    # The two clip fixtures, by stem -- the same set UmRunDrop.psm1 admits and the attribution
    # generator accepts as -ClipId. ValidateSet is case-insensitive, so the exact-case check below
    # is what actually pins it (sol, PR #137 r2: ValidatePattern's case-insensitivity).
    [Parameter(Mandatory = $true)]
    [ValidateSet('tiny_dual_iso', 'large_dual_iso')]
    [string]$ClipStem,

    [Parameter(Mandatory = $true)]
    [string]$FixturePath,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [ValidatePattern('^[A-Za-z]:\\[A-Za-z0-9 _.\\-]+$')]
    [string]$AgentRoot = 'C:\mlvtmp\mlv-agent'
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot '..\UmRunDrop.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force

if (@('tiny_dual_iso', 'large_dual_iso') -cnotcontains $ClipStem) {
    throw "ATTR3_FIXTURE_STEM_INVALID '$ClipStem' is not one of the clip fixture stems (exact case)"
}
if (-not (Test-Path -LiteralPath $FixturePath -PathType Leaf)) {
    throw "ATTR3_FIXTURE_MISSING $FixturePath"
}
if (-not (Test-UmRunTrackedFixtureSource -SourcePath $FixturePath)) {
    throw "ATTR3_FIXTURE_NOT_TRACKED $FixturePath is not a tracked clip fixture of this repository"
}
$fixture = Get-Item -LiteralPath $FixturePath -Force
if ([IO.Path]::GetFileNameWithoutExtension($fixture.Name) -cne $ClipStem) {
    throw "ATTR3_FIXTURE_STEM_MISMATCH '$($fixture.Name)' does not carry the stem '$ClipStem'"
}
[void](Assert-AttrCudaSafeArtifactName -Name $fixture.Name)

$fixtureSha = (Get-FileHash -LiteralPath $fixture.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
$jobId = "attr3-stage-fixture-$ClipStem-$($fixtureSha.Substring(0, 12))"

$template = @'
# -VerifyOnly runs the read-only prefix (containment, name safety, side-file presence and hash)
# and exits WITHOUT copying, publishing or deleting anything.
param([switch]$VerifyOnly)

$ErrorActionPreference = 'Stop'
$JobId = '__JOB_ID__'
$AgentRoot = '__AGENT_ROOT__'
$FixtureName = '__FIXTURE_NAME__'
$FixtureSha256 = '__FIXTURE_SHA256__'
$ClipStem = '__CLIP_STEM__'
$Cache = Join-Path $AgentRoot 'cache'
$Inbox = Join-Path $AgentRoot 'inbox'
$Work = Join-Path $AgentRoot "work\$JobId"
$Pub = Join-Path $AgentRoot "outbox\$JobId.artifacts"

function Say([string]$Message) { Write-Output "[$JobId] $Message" }
function Get-ShaLower([string]$Path) { (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }

# --- verifiers, embedded VERBATIM from tools/profiling/bachelor/AttrCudaArtifacts.psm1 --------
__EMBEDDED_FUNCTIONS__
# --- end embedded verifiers -------------------------------------------------------------------

$StepLog = [System.Collections.Specialized.OrderedDictionary]::new()
$PubReady = $false

function Complete-Failed([int]$Code, [string]$Step, [string]$Message) {
    $StepLog[$Step] = $Code
    Say "FAIL step=$Step exit=$Code $Message"
    if ($PubReady -and (Test-Path -LiteralPath $Pub)) {
        $partial = [ordered]@{
            schema = 'mlvapp.attr3-stage-fixture.v1'
            jobId = $JobId
            clipStem = $ClipStem
            fixtureRehearsal = $true
            exitCode = $Code
            failedStep = $Step
            message = $Message
            stagedOnHost = $env:COMPUTERNAME
            steps = $StepLog
        }
        try {
            [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value ($partial | ConvertTo-Json -Depth 10))
        } catch {
            Say "RESULT_JSON_NOT_WRITTEN $($_.Exception.Message)"
        }
    }
    Write-Output "RESULT=FIXTURE_STAGE_FAILED STEP=$Step EXIT=$Code"
    exit $Code
}

function Assert-UnderAgentRoot([string]$Path, [string]$Label) {
    $full = [IO.Path]::GetFullPath($Path)
    $root = [IO.Path]::GetFullPath($AgentRoot)
    if ($full -ne $root -and -not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "job-owned path '$Label' resolves outside the agent root: $full"
    }
}
foreach ($check in @(
    @{ path = $Cache; label = 'Cache' },
    @{ path = $Inbox; label = 'Inbox' },
    @{ path = $Work; label = 'Work' },
    @{ path = $Pub; label = 'Pub' }
)) { Assert-UnderAgentRoot $check.path $check.label }

Say "START fixture stem=$ClipStem name=$FixtureName"

try {
    [void](Assert-AttrCudaSafeArtifactName -Name $FixtureName)
    if ([IO.Path]::GetFileNameWithoutExtension($FixtureName) -cne $ClipStem) {
        throw "baked name '$FixtureName' does not carry the stem '$ClipStem'"
    }
    $side = Assert-AttrCudaDirectChild -Root $Inbox -Path (Join-Path $Inbox $FixtureName) -Label "inbox/$FixtureName"
    $cachePath = Assert-AttrCudaDirectChild -Root $Cache -Path (Join-Path $Cache $FixtureName) -Label "cache/$FixtureName"
    $partialPath = Assert-AttrCudaDirectChild -Root $Cache -Path (Join-Path $Cache "$FixtureName.partial") -Label "cache/$FixtureName.partial"
} catch {
    Complete-Failed 6 'artifactNameSafety' $_.Exception.Message
}
$StepLog['artifactNameSafety'] = 0

if (-not (Test-Path -LiteralPath $side -PathType Leaf)) {
    Complete-Failed 3 'inboxSideFile' "expected fixture missing from the inbox: $FixtureName"
}
$StepLog['inboxSideFile'] = 0

$arrivedSha = Get-ShaLower $side
if ($arrivedSha -ne $FixtureSha256) {
    Complete-Failed 4 'fixtureHash' "sha256 mismatch for ${FixtureName}: baked $FixtureSha256, arrived $arrivedSha"
}
$StepLog['fixtureHash'] = 0

if ($VerifyOnly) {
    Write-Output "RESULT=VERIFY_ONLY_OK FIXTURE=$FixtureName SHA256=$FixtureSha256"
    exit 0
}

Remove-AttrCudaTree -TrustedRoot $AgentRoot -Path $Work
# Each container is created explicitly, parent first (never as a side effect of -Force on a deeper
# path), so every one of them is link-checked like the directories this job publishes into.
[void](New-AttrCudaDirectory -Path (Join-Path $AgentRoot 'work'))
[void](New-AttrCudaDirectory -Path $Work)
[void](New-AttrCudaDirectory -Path (Join-Path $AgentRoot 'outbox'))
[void](New-AttrCudaDirectory -Path $Pub)
[void](New-AttrCudaDirectory -Path $Cache)
$PubReady = $true

$Scratch = Join-Path $Work '.job-tmp'
[void](New-AttrCudaDirectory -Path $Scratch)
$env:TEMP = $Scratch
$env:TMP = $Scratch

# Already staged with identical bytes: nothing to do, and re-staging must not churn the cache.
if (Test-Path -LiteralPath $cachePath -PathType Leaf) {
    if ((Get-ShaLower $cachePath) -eq $FixtureSha256) {
        $StepLog['publish'] = 0
        [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value (([ordered]@{
            schema = 'mlvapp.attr3-stage-fixture.v1'; jobId = $JobId; clipStem = $ClipStem
            fixtureRehearsal = $true; exitCode = 0; alreadyStaged = $true
            fixture = [ordered]@{ name = $FixtureName; sha256 = $FixtureSha256; cachePath = $cachePath }
            stagedOnHost = $env:COMPUTERNAME; steps = $StepLog
        }) | ConvertTo-Json -Depth 10))
        Write-Output "RESULT=FIXTURE_STAGE_OK ALREADY=1 FIXTURE=$FixtureName CACHE=$cachePath"
        exit 0
    }
    Complete-Failed 21 'publishRename' "cache already holds $FixtureName with DIFFERENT bytes"
}

try {
    [void](Publish-AttrCudaFileCopy -Source $side -Destination $partialPath)
    if ((Get-ShaLower $partialPath) -ne $FixtureSha256) { throw "sha256 did not round-trip into the cache for $FixtureName" }
} catch {
    [void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path $partialPath)
    Complete-Failed 20 'publishPartial' $_.Exception.Message
}
$StepLog['publishPartial'] = 0

try {
    [void](Publish-AttrCudaFileMove -Source $partialPath -Destination $cachePath)
} catch {
    [void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path $partialPath)
    Complete-Failed 21 'publishRename' $_.Exception.Message
}
$StepLog['publishRename'] = 0

# The inbox copy is removed only after the cache publish is complete, so an interrupted run leaves
# the inbox intact and is simply re-runnable.
[void](Remove-AttrCudaPartialFile -TrustedRoot $AgentRoot -Path $side)
$StepLog['inboxCleanup'] = 0

try { [void](Publish-AttrCudaText -Path (Join-Path $Pub 'result.json') -Value (([ordered]@{
    schema = 'mlvapp.attr3-stage-fixture.v1'; jobId = $JobId; clipStem = $ClipStem
    fixtureRehearsal = $true; exitCode = 0; alreadyStaged = $false
    fixture = [ordered]@{ name = $FixtureName; sha256 = $FixtureSha256; cachePath = $cachePath }
    stagedOnHost = $env:COMPUTERNAME; steps = $StepLog
}) | ConvertTo-Json -Depth 10)) } catch { Complete-Failed 23 'publishResult' $_.Exception.Message }
Write-Output "RESULT=FIXTURE_STAGE_OK FIXTURE=$FixtureName SHA256=$FixtureSha256 CACHE=$cachePath"
exit 0
'@

$embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @(
    'Assert-AttrCudaSafeArtifactName',
    'Assert-AttrCudaDirectChild',
    'Assert-AttrCudaNoLinkBelowRoot',
    'Assert-AttrCudaWritableFileSlot',
    'Publish-AttrCudaText',
    'Publish-AttrCudaFileCopy',
    'Publish-AttrCudaFileMove',
    'New-AttrCudaDirectory',
    'Remove-AttrCudaPartialFile',
    'Remove-AttrCudaTree'
)

if (-not (Test-Path -LiteralPath $OutDir)) { [void](New-Item -ItemType Directory -Path $OutDir -Force) }
$OutDir = (Resolve-Path -LiteralPath $OutDir).Path
$jobPath = Join-Path $OutDir "$jobId.job.ps1"

$text = $template.
    Replace('__EMBEDDED_FUNCTIONS__', $embeddedFunctions).
    Replace('__JOB_ID__', $jobId).
    Replace('__AGENT_ROOT__', $AgentRoot).
    Replace('__FIXTURE_NAME__', $fixture.Name).
    Replace('__FIXTURE_SHA256__', $fixtureSha).
    Replace('__CLIP_STEM__', $ClipStem)
[IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))

[pscustomobject]@{
    jobFile = $jobPath
    jobId = $jobId
    clipStem = $ClipStem
    fixtureName = $fixture.Name
    fixtureSha256 = $fixtureSha
    sideFile = $fixture.FullName
    agentRoot = $AgentRoot
    cachePath = (Join-Path (Join-Path $AgentRoot 'cache') $fixture.Name)
}
