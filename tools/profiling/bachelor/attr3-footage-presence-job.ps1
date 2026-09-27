# attr3-footage-presence-job.ps1 -- GENERATOR (runs locally; nothing here runs on Bachelor and
# nothing here is submitted to Bachelor -- the hub does both). Emits a job that answers exactly
# one question, safely, on the measurement host: does Bachelor already hold owner-consented clip
# <id>, byte-exact, at the path the frozen spec names for it (ATTR3-FOOTAGE-BIND-1, PR-A).
#
# WHY A SEPARATE GENERATOR, NOT A FOURTH ARTIFACT IN A STAGE JOB. This is a PROBE, not a publish
# job: it copies nothing, renames nothing and touches no cache. It exists so the hub can learn
# "is the corpus already there" BEFORE any playback attempt, without ever putting a real footage
# path in a command, a log, or this generator's own stdout.
#
# WHAT IS BAKED AND WHAT IS CHECKED. This generator calls tools/gates/resolve_consented_clip.py
# as a CHILD PROCESS with --emit-json into a private temp file: the resolver alone proves that
# the frozen spec's parts for this clip id (length, sha256, and the hook's own norm() of the
# path) agree with the hook's frozen consent table. This generator then reads that temp file
# IN-PROCESS, deletes it, and hands the resolved parts to
# Attr3FootagePresenceJob.psm1's New-Attr3FootagePresenceJob, which bakes each part's path as
# BASE64 of its UTF-8 bytes (never an interpolated literal -- see that module's header) plus its
# length and sha256 into the emitted job. The emitted job, on Bachelor, re-checks each part
# against the LIVE filesystem: existence, length, and a streaming sha256 -- never trusting that
# "the resolver approved it" means the bytes are still there.
#
# THE RESOLVER CHILD PROCESS IS THE ONLY WAY THIS GENERATOR OBTAINS PARTS (round 2, defect 3).
# There used to be a public -InjectResolvedPartsJsonPathForTests parameter that let ANY caller
# skip the resolver and hand this generator synthetic parts directly -- and with it, skip the
# resolver's cross-check against the hook's frozen consent table. That parameter is gone. Tests
# that need synthetic parts now import Attr3FootagePresenceJob.psm1 directly and call
# New-Attr3FootagePresenceJob themselves; that module never touches git, the spec, or the
# consent table, so calling it is not a way to forge a presence job for a real clip id -- it only
# ever runs against parts a test fabricated itself.
#
# NO PATH EVER REACHES THIS GENERATOR'S OWN OUTPUT. The resolver's own stdout (captured here only
# to fold into a refusal message) is already path-free by contract (its CLI never prints a part
# path). This generator's own Write-Output lines name the clip id, the part count and the local
# job file path it just wrote -- never a footage path.
#
# NO PUBLISH, NO PLAYBACK. The emitted job opens nothing for playback, deploys nothing, and writes
# nothing to disk except its own stdout: a per-part PASS/NOT_FOUND/ACCESS_DENIED/UNREADABLE/
# LENGTH_MISMATCH/SHA256_MISMATCH line, one RESULT= line (FOOTAGE_PRESENT/FOOTAGE_ABSENT/
# FOOTAGE_MISMATCH/FOOTAGE_INDETERMINATE -- see Attr3FootagePresenceJob.psm1's job template for
# the exact mapping and exit codes 0-3), and one path-free JSON summary line.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\attr3-footage-presence-job.ps1 `
#       -ClipId M16-1243 -OutDir <staging-dir>
# then submit the emitted <jobId>.job.ps1 to Bachelor the same way any other job in this route is
# submitted -- it needs no side file at all; the resolved parts travel inside the job.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
    [string]$ClipId,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'Attr3FootagePresenceJob.psm1') -Force

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path

function Resolve-Attr3PresencePython {
    <#
    .SYNOPSIS
    Prefer `python.exe` only after proving it is Python 3; else fall back to the `py` launcher
    pinned to -3. Mirrors tools/coordination/board-health-sweep.ps1's interpreter probe -- this
    machine still carries a Python 2 `python.exe` on some hosts, so simply assuming `py -3` (or
    assuming `python.exe` is 3.x) is not a valid availability check.
    #>
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($null -ne $python) {
        $major = @(& $python.Source -c 'import sys; print(sys.version_info[0])' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $major.Count -eq 1 -and $major[0] -eq '3') {
            return [pscustomobject]@{ Exe = $python.Source; PrefixArgs = @() }
        }
    }
    $pyLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -ne $pyLauncher) {
        return [pscustomobject]@{ Exe = $pyLauncher.Source; PrefixArgs = @('-3') }
    }
    throw 'ATTR3_PRESENCE_NO_PYTHON no Python 3 interpreter is available to run the resolver'
}

$ResolverPath = Join-Path $RepoRoot 'tools\gates\resolve_consented_clip.py'
if (-not (Test-Path -LiteralPath $ResolverPath -PathType Leaf)) {
    throw "ATTR3_PRESENCE_RESOLVER_MISSING resolver not found at $ResolverPath"
}
$py = Resolve-Attr3PresencePython

# The resolver is invoked as a CHILD PROCESS that writes paths only into this private temp
# file; this generator process reads it IN-PROCESS (never Write-Output-ing its content) and
# deletes it in a `finally` before returning. A real footage path may flow only through
# process memory or through a file a child process writes -- never into a tool call or onto
# this generator's own stdout (hard rules 1-3 of the card this generator implements). This is
# the generator's ONLY path to obtaining parts -- there is no parameter that skips it.
$emitPath = Join-Path ([IO.Path]::GetTempPath()) ("attr3-footage-presence-resolve-$([guid]::NewGuid().ToString('N')).json")
try {
    $resolverArgs = @($py.PrefixArgs) + @($ResolverPath, '--clip-id', $ClipId, '--repo-root', $RepoRoot, '--emit-json', $emitPath)
    $summaryLines = @(& $py.Exe @resolverArgs 2>&1)
    $resolverExit = $LASTEXITCODE
    if ($resolverExit -ne 0) {
        # $summaryLines is the resolver's own path-free JSON summary line (its CLI contract
        # never prints a part path), so folding it into this message is safe.
        throw "ATTR3_PRESENCE_RESOLVE_REFUSED clip '$ClipId' was refused by the resolver (exit $resolverExit): $($summaryLines -join ' ')"
    }
    if (-not (Test-Path -LiteralPath $emitPath -PathType Leaf)) {
        throw 'ATTR3_PRESENCE_EMIT_MISSING resolver exited 0 but wrote no --emit-json file'
    }
    $emitBytes = [IO.File]::ReadAllBytes($emitPath)
} finally {
    if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }
}

$resolved = [Text.Encoding]::UTF8.GetString($emitBytes) | ConvertFrom-Json
if ($resolved.clipId -cne $ClipId) {
    throw "ATTR3_PRESENCE_CLIP_ID_MISMATCH resolver emitted clipId '$($resolved.clipId)' for requested '$ClipId'"
}
$parts = @($resolved.parts)

# Job-TEXT construction lives entirely in Attr3FootagePresenceJob.psm1 (round 2, defect 3): this
# is the only call in this generator that builds or writes the emitted job, and it is also the
# only call a test invokes directly, with fabricated parts, to exercise the same logic.
New-Attr3FootagePresenceJob -ClipId $ClipId -Parts $parts -OutDir $OutDir
