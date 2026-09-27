# Attr3FootagePresenceJob.psm1 -- job-TEXT construction for attr3-footage-presence-job.ps1, split
# out of the CLI generator so a test can build a job from SYNTHETIC parts directly, without the
# generator's CLI surface ever offering a way to skip the resolver (ATTR3-FOOTAGE-BIND-1, PR-A,
# round 2, defect 3).
#
# THE DEFECT THIS CLOSES. -InjectResolvedPartsJsonPathForTests used to be a public parameter on
# the CLI generator (attr3-footage-presence-job.ps1): passing it from ANY caller, not just a test,
# skipped the resolver child process entirely -- and with it, the resolver's cross-check of the
# frozen spec against the hook's frozen consent table. New-Attr3FootagePresenceJob below takes
# already-resolved parts as in-memory objects; nothing on the generator's real CLI surface
# (-ClipId/-OutDir/-RepoRoot only -- see that script) can reach this function except by calling
# the resolver first and handing its output here. A test imports THIS MODULE directly and calls
# the function with fabricated parts, which is an ordinary PowerShell function call, not a flag on
# the production entry point.
#
# WHAT THIS FUNCTION DOES NOT DO. It never calls the resolver, never reads git, and never reads
# tools/gates/output-budget.json or the hook's consent table. Whether the parts it is handed
# actually correspond to a real owner-consented clip is entirely the caller's problem; for the
# real CLI path that caller is attr3-footage-presence-job.ps1, which obtains parts from
# tools/gates/resolve_consented_clip.py alone.
#
# WHY BASE64, NOT A CHARACTER ALLOWLIST (round 2, defect 1). Every real consented part path in
# tools/gates/output-budget.json uses FORWARD slashes (drive letter, colon, then `/`-separated
# segments), and a measurement host's own TEMP root can carry an `~` from an 8.3 short name --
# both real, both previously rejected by a backslash-only allowlist regex that this module does
# not carry forward. What actually has to be safe is the embedding: the job template below wraps
# $PartsJson in a SINGLE-QUOTED PowerShell string literal, so a raw path containing a `'` would
# break out of that literal into arbitrary template code once JSON-encoded (JSON escapes `"`, not
# `'`). Encoding each path as base64 of its UTF-8 bytes removes the character class that can do
# that -- base64 output is `[A-Za-z0-9+/=]` only -- so the binding that actually matters (the
# resolver's cross-check against the frozen table, including path_norm_sha256) is what is trusted,
# not a guess at which characters a real path can safely contain.

Set-StrictMode -Version Latest

# ATTR3-FOOTAGE-STAGE-1 round 3: see AttrCudaOwnerFootage.psm1's own header for why an
# unconditional `-Force` reimport here is wrong whenever a caller already imported
# AttrCudaArtifacts.psm1 globally first -- it strips the caller's existing global copy instead
# of reusing it.
if (-not (Get-Command -Name 'Assert-AttrCudaSafeArtifactName' -ErrorAction SilentlyContinue)) {
    # ATTR3-FOOTAGE-STAGE-1 round 11: -Verbose:$false so this fallback import (unreachable from
    # the production CLI, which always loads AttrCudaArtifacts.psm1 first) never depends on a
    # caller's ambient $VerbosePreference either, the same defense-in-depth
    # attr3-footage-stage.ps1's own four Import-Module calls now carry.
    Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Global -ErrorAction Stop -Verbose:$false
}

function New-Attr3FootagePresenceJob {
    <#
    .SYNOPSIS
    Build and write a <jobId>.job.ps1 footage-presence probe from already-resolved parts.
    .DESCRIPTION
    Each part's path is embedded in the emitted job as base64 of its UTF-8 bytes, decoded on
    Bachelor via the module's own Read-AttrCudaBase64Payload (embedded verbatim, the same helper
    tools/profiling/bachelor/attr3-stage-smoke-runner-job.ps1 uses for its inline payload) --
    never as an interpolated string. The structural sanity check below (drive letter, colon,
    separator, no control characters) is defense in depth over the resolver's own cross-check
    against the frozen consent table; it is NOT what makes the embedding injection-safe -- the
    base64 encoding is, regardless of what this check would have allowed through.
    Throws a distinguishable ATTR3_PRESENCE_* token on any refusal; returns a pscustomobject
    describing the emitted job otherwise.
    On Bachelor, the emitted job checks each part with every filesystem call wrapped in its own
    try/catch (round 4), reporting an honest per-part status -- PASS, NOT_FOUND, ACCESS_DENIED,
    UNREADABLE, LENGTH_MISMATCH, SHA256_MISMATCH or TARGET_PATH_UNSAFE (round 4: the same
    target-chain reparse-point check Attr3FootageStageJob.psm1's own emitted job applies, run
    here before a single byte of the part is ever read) -- and an overall result of
    FOOTAGE_PRESENT (exit 0), FOOTAGE_ABSENT (exit 1), FOOTAGE_MISMATCH (exit 2) or
    FOOTAGE_INDETERMINATE (exit 3); see the mapping documented above the overall-result block in
    the job template below for the exact rule. No exception's own text ever reaches this job's
    output, since it can contain the part's real path.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
        [string]$ClipId,

        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [object[]]$Parts,

        [Parameter(Mandatory = $true)]
        [string]$OutDir
    )

    if ($Parts.Count -eq 0) {
        throw "ATTR3_PRESENCE_NO_PARTS zero parts supplied for '$ClipId'"
    }

    foreach ($part in $Parts) {
        $path = [string]$part.path
        if ([string]::IsNullOrEmpty($path)) {
            throw "ATTR3_PRESENCE_PART_PATH_INVALID part $($part.index) has an empty path"
        }
        # Drive letter, colon, then a forward OR back slash -- both separators are real (see
        # module header); anything after that, including `~`, is unconstrained here.
        if ($path -notmatch '^[A-Za-z]:[\\/]') {
            throw "ATTR3_PRESENCE_PART_PATH_INVALID part $($part.index) does not start with a drive letter, colon and separator"
        }
        if ($path -match '[\x00-\x1f]') {
            throw "ATTR3_PRESENCE_PART_PATH_INVALID part $($part.index) contains a control character"
        }
        if ($part.sha256 -notmatch '^[0-9a-f]{64}$') {
            throw "ATTR3_PRESENCE_PART_SHA_INVALID part $($part.index) sha256 is not 64 lowercase hex"
        }
        if ($part.length -isnot [long] -and $part.length -isnot [int]) {
            throw "ATTR3_PRESENCE_PART_LENGTH_INVALID part $($part.index) length is not an integer"
        }
    }

    # Re-serialised, compact and key-ordered, so the embedded literal is deterministic and never
    # carries the caller's own incidental whitespace or key order. `path` never appears here --
    # only `pathBase64` does.
    $partsForJob = @($Parts | Sort-Object { [int]$_.index } | ForEach-Object {
        [ordered]@{
            index = [int]$_.index
            pathBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]$_.path))
            length = [int64]$_.length
            sha256 = [string]$_.sha256
        }
    })
    $partsJson = $partsForJob | ConvertTo-Json -Compress -Depth 5
    # ConvertTo-Json -Compress on a single-element array still yields a bare object, never
    # `[...]`, unless coerced -- the emitted job always expects a JSON array.
    if ($partsForJob.Count -eq 1) { $partsJson = "[$partsJson]" }

    # A content-derived audit key -- not a security control (the resolver's cross-check is).
    $canonicalPayload = ([ordered]@{ clipId = $ClipId; parts = $partsForJob }) | ConvertTo-Json -Compress -Depth 5
    $sha256Alg = [Security.Cryptography.SHA256]::Create()
    try {
        $sourceSha256 = [BitConverter]::ToString(
            $sha256Alg.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonicalPayload))
        ).Replace('-', '').ToLowerInvariant()
    } finally {
        $sha256Alg.Dispose()
    }

    # ATTR3-FOOTAGE-STAGE-1 round 3: a purely content-derived id meant a repeated presence check
    # for the SAME clip and parts (e.g. a caller re-probing before every retry of a transfer)
    # always submitted the SAME id, so a retained result receipt from an earlier probe refused
    # every later one with UMRUN_JOBID_IN_USE -- indistinguishable, from the caller's side, from a
    # submission failure. A fresh random component makes every call's id unique regardless of
    # content; $sourceSha256 (still returned) remains the stable audit/dedup key.
    $attemptNonce = [guid]::NewGuid().ToString('N').Substring(0, 10)
    $jobId = "attr3-footage-presence-$ClipId-$($sourceSha256.Substring(0, 12))-$attemptNonce"
    [void](Assert-AttrCudaSafeArtifactName -Name "$jobId.job.ps1")

    # ATTR3-FOOTAGE-BIND-1 PR-B: Test-AttrCudaFootagePart is the ONE shared per-part content
    # verifier, also embedded (byte-identically) in playback-attr-3-cuda-job.ps1's owner-clip
    # content gate -- see that function's own header in AttrCudaArtifacts.psm1.
    # ATTR3-FOOTAGE-STAGE-1 round 4 (astra 3): Assert-AttrCudaNoLinkBelowRoot is the SAME
    # target-chain link check Attr3FootageStageJob.psm1's own emitted job applies to its target
    # path -- embedded here too so this probe never hashes/reads a part through an unproven chain.
    $embeddedFunctions = Get-AttrCudaEmbeddedFunctionSource -Name @('Read-AttrCudaBase64Payload', 'Test-AttrCudaFootagePart', 'Assert-AttrCudaNoLinkBelowRoot')

    # --- job body template (placeholders are substituted below; the body itself never touches
    #     this function's variables directly, so there is no accidental capture of this process's
    #     environment into the emitted script) -------------------------------------------------
    $template = @'
$ErrorActionPreference = 'Stop'
# ATTR3-FOOTAGE-STAGE-1 round 11 (astra major: inherited verbose diagnostics disclose full
# paths). This probe runs on Bachelor under the submitting agent's OWN ambient preferences, not
# this generator's -- pinned here, first, for the same reason and by the same mechanism as
# attr3-footage-stage.ps1's own top-of-file pin and Attr3FootageStageJob.psm1's own template pin
# (see either's own comment): every cmdlet and embedded function call below resolves these five
# preference variables by scope lookup from this job's own top-level scope unless overridden
# again, and the embedded verifiers below read real footage-path files before this probe ever
# reports a PASS/NOT_FOUND/etc token, where an ambient Continue diagnostic stream is exactly what
# could carry one of those paths out.
$VerbosePreference = 'SilentlyContinue'
$DebugPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'
$JobId = '__JOB_ID__'
$ClipId = '__CLIP_ID__'
$PartsJson = '__PARTS_JSON__'

function Say([string]$Message) { Write-Output "[$JobId] $Message" }

# --- verifiers, embedded VERBATIM from tools/profiling/bachelor/AttrCudaArtifacts.psm1 --------
__EMBEDDED_FUNCTIONS__
# --- end embedded verifiers -------------------------------------------------------------------

# ATTR3-FOOTAGE-STAGE-1 round 7 (class b: outer boundary). Everything from here through this
# job's own final `exit $exitCode` runs inside ONE try/catch: every per-part failure this probe
# can anticipate already maps to a typed PART=/RESULT= token below, so this is the backstop for
# anything it cannot -- a caught exception's own .Message is NEVER forwarded (it can carry a real
# footage path), only the one fixed, path-free token in the catch at the bottom of this template.
try {
$RawParts = @($PartsJson | ConvertFrom-Json)
$PartCount = $RawParts.Count

Say "START clip=$ClipId parts=$PartCount"

$results = New-Object System.Collections.Generic.List[object]
foreach ($rawPart in $RawParts) {
    # The path never travels as a literal: it is decoded from base64 IN THIS PROCESS, on this
    # host, and used only through -LiteralPath calls below -- never re-embedded into a string
    # that PowerShell parses as code.
    $decoded = Read-AttrCudaBase64Payload -Base64 $rawPart.pathBase64
    $partPath = [Text.Encoding]::UTF8.GetString($decoded.bytes)

    # ATTR3-FOOTAGE-STAGE-1 round 4 (astra 3, link checks on read paths): before this probe reads
    # or hashes a single byte, prove every EXISTING ancestor of $partPath, down to and including
    # $partPath itself, carries no reparse point -- the same target-chain check
    # Attr3FootageStageJob.psm1's own emitted job applies to $targetPath before it reads it.
    # Nothing here ever writes $_, $_.Exception or its .Message.
    $driveRoot = [IO.Path]::GetPathRoot($partPath)
    $pathSafe = $true
    try {
        [void](Assert-AttrCudaNoLinkBelowRoot -TrustedRoot $driveRoot -Path $partPath)
    } catch {
        $pathSafe = $false
    }

    # ATTR3-FOOTAGE-BIND-1 PR-B: the per-part content check is now the ONE shared function
    # Test-AttrCudaFootagePart (AttrCudaArtifacts.psm1), embedded verbatim -- never a
    # re-implementation. Nothing below ever writes $_, $_.Exception or its .Message; the function
    # itself returns only a fixed status TOKEN, never exception text.
    $status = if ($pathSafe) {
        Test-AttrCudaFootagePart -Path $partPath -ExpectedLength ([int64]$rawPart.length) -ExpectedSha256 ([string]$rawPart.sha256)
    } else {
        'TARGET_PATH_UNSAFE'
    }
    Write-Output "PART=$($rawPart.index) STATUS=$status"
    $results.Add([ordered]@{
        index = $rawPart.index
        status = $status
        length = $rawPart.length
        sha256_12 = ([string]$rawPart.sha256).Substring(0, 12)
    })
}

# Overall-result mapping (round 4, defect 5). Each per-part status above is honest about WHY a
# part failed, so the overall result can distinguish "definitely not there" from "could not
# tell" instead of folding every non-PASS reason into MISSING/UNREADABLE as before:
#   FOOTAGE_PRESENT       exit 0  every part is PASS.
#   FOOTAGE_ABSENT        exit 1  every part is NOT_FOUND.
#   FOOTAGE_MISMATCH      exit 2  at least one part is LENGTH_MISMATCH or SHA256_MISMATCH (a
#                                 byte difference was actually OBSERVED on a readable part) AND
#                                 no part is ACCESS_DENIED or UNREADABLE.
#   FOOTAGE_INDETERMINATE exit 3  anything else -- e.g. any ACCESS_DENIED/UNREADABLE part, or a
#                                 mix of PASS and NOT_FOUND with no part actually differing --
#                                 cannot honestly be called PRESENT, ABSENT or MISMATCH.
$statuses = @($results | ForEach-Object { $_.status })
$diffStatuses = @('LENGTH_MISMATCH', 'SHA256_MISMATCH')
# TARGET_PATH_UNSAFE (round 4) never observed a byte, honest or otherwise -- it belongs in the
# same "could not tell" bucket as ACCESS_DENIED/UNREADABLE, never folded into ABSENT or MISMATCH.
$uncertainStatuses = @('ACCESS_DENIED', 'UNREADABLE', 'TARGET_PATH_UNSAFE')
if (($statuses | Where-Object { $_ -ne 'PASS' }).Count -eq 0) {
    $overall = 'FOOTAGE_PRESENT'; $exitCode = 0
} elseif (($statuses | Where-Object { $_ -ne 'NOT_FOUND' }).Count -eq 0) {
    $overall = 'FOOTAGE_ABSENT'; $exitCode = 1
} elseif (
    ($statuses | Where-Object { $diffStatuses -contains $_ }).Count -ge 1 -and
    ($statuses | Where-Object { $uncertainStatuses -contains $_ }).Count -eq 0
) {
    $overall = 'FOOTAGE_MISMATCH'; $exitCode = 2
} else {
    $overall = 'FOOTAGE_INDETERMINATE'; $exitCode = 3
}

Write-Output "RESULT=$overall CLIP=$ClipId PARTS=$PartCount"
Write-Output (([ordered]@{
    schema = 'mlvapp.attr3-footage-presence.v1'
    jobId = $JobId
    clipId = $ClipId
    result = $overall
    partCount = $PartCount
    parts = $results
}) | ConvertTo-Json -Compress -Depth 5)
exit $exitCode
} catch {
    # ATTR3-FOOTAGE-STAGE-1 round 7 (class b: outer boundary): a fixed, path-free token only --
    # see the opening comment on this try block. Distinct exit code (4) from the ordinary
    # FOOTAGE_PRESENT/ABSENT/MISMATCH/INDETERMINATE (0-3) so a caller can tell "every part got an
    # honest status" from "this probe itself hit something it never anticipated" apart.
    Write-Output "RESULT=FOOTAGE_PRESENCE_JOB_ERROR CLIP=$ClipId"
    exit 4
}
'@

    # ATTR3-FOOTAGE-BIND-1 PR-B round 3 (STRUCTURAL): a single-pass substitution over the WHOLE
    # token map at once -- see Expand-AttrCudaTemplate's own header in AttrCudaArtifacts.psm1. A
    # -ClipId shaped like 'a__PARTS_JSON__b' passes this function's own ValidatePattern (it is
    # alnum/underscore/dot/hyphen only) and used to collide with a later .Replace() call in the
    # old chained substitution; a single regex pass over the original template never rescans a
    # substituted value, so that collision class cannot occur here regardless of which token a
    # caller-controlled value happens to spell.
    $text = Expand-AttrCudaTemplate -Template $template -Tokens ([ordered]@{
        JOB_ID = $jobId
        CLIP_ID = $ClipId
        PARTS_JSON = $partsJson
        EMBEDDED_FUNCTIONS = $embeddedFunctions
    })

    if (-not (Test-Path -LiteralPath $OutDir)) { [void](New-Item -ItemType Directory -Path $OutDir -Force) }
    $OutDir = (Resolve-Path -LiteralPath $OutDir).Path
    $jobPath = Join-Path $OutDir "$jobId.job.ps1"
    [IO.File]::WriteAllText($jobPath, $text, [Text.UTF8Encoding]::new($false))

    # ATTR3-FOOTAGE-STAGE-1 round 3 (no path in any branch): JOB= now names the opaque job id,
    # never the local job FILE path.
    Write-Output "RESULT=FOOTAGE_PRESENCE_JOB_EMITTED CLIP=$ClipId PARTS=$($partsForJob.Count) SOURCE_SHA256=$sourceSha256 JOB=$jobId"

    [pscustomobject]@{
        jobFile = $jobPath
        jobId = $jobId
        clipId = $ClipId
        partCount = $partsForJob.Count
        sourceSha256 = $sourceSha256
    }
}

function Get-Attr3FootagePresentPartIndexes {
    <#
    .SYNOPSIS
    Parse a footage-presence job's own stdout and return the part INDEXES it reported PASS for a
    given -ClipId, as a sorted int array -- never anything else out of that text.
    .DESCRIPTION
    ATTR3-FOOTAGE-STAGE-1 round 4 (sol minor / astra 5: per-part resume). Split out of
    attr3-footage-stage.ps1 so a test can exercise the parsing directly against fabricated stdout,
    without driving the real CLI (whose only path to parts is the resolver -- see that script's
    own header). Only the LAST line matching the presence job's own
    mlvapp.attr3-footage-presence.v1 schema is trusted (a real job's stdout carries exactly one);
    a clip id mismatch, a missing or malformed payload, or no matching line at all all return an
    EMPTY set -- never a guess that could skip transferring a part that is not actually there.
    ATTR3-FOOTAGE-STAGE-1 round 5 (hub-reproduced blocker, one clear return shape): every branch
    returns a PLAIN array via `@(...)`, never the `,@(...)` unrolling-suppression idiom. The
    production caller (attr3-footage-stage.ps1) always wraps this function's own call in its own
    `@(...)` -- exactly like every test in this file already does -- and `@(Get-Foo)` already
    forces array-ness (0, 1 or many elements alike) around whatever this function enumerates onto
    the pipeline. Returning `,@(...)` here used to make this function itself ALSO emit a single
    already-array-shaped object; the caller's own `@(...)` then wrapped THAT one object again,
    producing a 1-element array whose lone element was an Object[] -- so `[int]$presentIndex` in
    attr3-footage-stage.ps1's own foreach threw for zero, one AND many PASS indexes alike, every
    time, outside any try/catch. One shape, no double-wrapping: this function returns a plain
    array; callers that need array-ness from a possibly-empty/singleton result supply their own
    `@(...)`, exactly once.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][AllowNull()][string]$Stdout,
        [Parameter(Mandatory = $true)][string]$ClipId
    )

    $present = New-Object 'System.Collections.Generic.HashSet[int]'
    if ([string]::IsNullOrEmpty($Stdout)) { return @() }

    $lines = @($Stdout -split "`r?`n" | Where-Object { $_ })
    $jsonLine = $lines | Where-Object { $_ -match '"schema"\s*:\s*"mlvapp\.attr3-footage-presence\.v1"' } | Select-Object -Last 1
    if (-not $jsonLine) { return @() }

    try {
        $payload = $jsonLine | ConvertFrom-Json
    } catch {
        return @()
    }
    if ($payload.clipId -cne $ClipId) { return @() }

    foreach ($part in @($payload.parts)) {
        if ([string]$part.status -eq 'PASS') { [void]$present.Add([int]$part.index) }
    }
    return @($present | Sort-Object)
}

Export-ModuleMember -Function New-Attr3FootagePresenceJob, Get-Attr3FootagePresentPartIndexes
