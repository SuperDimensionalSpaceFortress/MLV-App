# VenueStageReuse.psm1 -- the single-use um-run JobId reuse checks, PROMOTED into a tracked module (UM-REGULAR-BASELINE-STAGE-1).
#
# ORIGIN. The three functions below are copied, logic unchanged, from the untracked lane helper tools\venue-reuse-check.ps1, which lanes copied word for
# word from run dir to run dir (LOOK-FULLSCREEN-PARITY-1 r3/r4 -> RECON-WARM-RUN-1 -> FIRST-AMAZE-AT-PLAY-1 -> FIRST-PRESENT-WARM-OUTPUT-REUSE-1 ->
# PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 r1, .claude-state\fleet-runs\lane-PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1-r1-20261018T1115Z\tools\
# venue-reuse-check.ps1 lines 7-75). Only this header is new; the bodies are byte-for-byte the lane copy.
#
# WHAT THEY ANSWER, READ-ONLY. A stage / DLL-pair JobId is single-use (UmRunDrop refuses a reused one with UMRUN_JOBID_IN_USE) and is derived from the build sha
# only. These functions read ONE outbox result.json (and, for a DLL pair, its artifacts manifest) BY NAME and say whether that earlier result was a clean run
# for this sha. They never submit, write or enumerate anything.
#
# WHAT THEY DO NOT ANSWER (#508, VENUE-STAGED-BUILD-VANISHED-AFTER-ADMISSION-WAIT-1). A STAGE_OK result for the default stage JobId does NOT prove the venue cache
# holds the manifest a caller expects: a re-assemble of the same sha makes a new manifest (build.json carries assembledAtUtc), and the cache keeps whatever was
# staged first. Get-StageReuse is therefore evidence about a RESULT, never about the CACHE; Invoke-VenueStageNightly.ps1 hashes the cache build.json by name.

function Test-UmRunJobIdInUseMessage {
    param([string]$Message, [string]$JobId)
    if ([string]::IsNullOrEmpty($Message)) { return $false }
    return ($Message.Trim() -cmatch ('^UMRUN_JOBID_IN_USE outbox already holds ' + [regex]::Escape($JobId) + '\.result\.json$'))
}

function Get-StageReuse {
    param(
        [Parameter(Mandatory = $true)][string]$ResultPath,
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Sha,
        [Parameter(Mandatory = $true)][ValidateSet('Stage', 'SmokeRunner')][string]$Kind
    )
    $no = { param($why) [pscustomobject]@{ Staged = $false; Reason = $why; EndedUtc = $null; Line = $null } }
    if (-not (Test-Path -LiteralPath $ResultPath -PathType Leaf)) { return (& $no 'result-missing') }
    try { $raw = Get-Content -LiteralPath $ResultPath -Raw -ErrorAction Stop; $r = $raw | ConvertFrom-Json -ErrorAction Stop } catch { return (& $no 'result-unreadable') }
    if ([string]$r.jobId -cne $JobId) { return (& $no "jobId-mismatch ($([string]$r.jobId))") }
    $code = 0
    if (-not [int]::TryParse([string]$r.exitCode, [ref]$code) -or $code -ne 0) { return (& $no "exitCode-not-0 ($([string]$r.exitCode))") }
    foreach ($flag in 'timedOut', 'cancelled', 'queueExpired', 'orphaned') {
        if ($r.$flag -eq $true) { return (& $no "$flag-true") }
    }
    $stdout = [string]$r.stdout
    $line = $null
    if ($Kind -eq 'Stage') {
        $m = [regex]::Match($stdout, '(?m)^RESULT=STAGE_OK SOURCE=([0-9a-f]{40})(?=\s|$)')
        if (-not $m.Success) { return (& $no 'no-RESULT=STAGE_OK-line') }
        if ($m.Groups[1].Value -cne $Sha) { return (& $no "SOURCE-mismatch ($($m.Groups[1].Value))") }
        $line = $m.Value
    } else {
        $jm = [regex]::Match($JobId, '^attr3-stage-smoke-runner-([0-9a-f]{12})-([0-9a-f]{12})$')
        if (-not $jm.Success) { return (& $no 'smoke-jobId-shape') }
        if ($jm.Groups[1].Value -cne $Sha.Substring(0, 12)) { return (& $no "smoke-jobId-sha12-mismatch ($($jm.Groups[1].Value))") }
        $m = [regex]::Match($stdout, '(?m)^RESULT=SMOKE_RUNNER_STAGE_OK CACHE_DIR=(smoke-runner-[0-9a-f]{16})(?=\s|$)')
        if (-not $m.Success) { return (& $no 'no-RESULT=SMOKE_RUNNER_STAGE_OK-line') }
        if (-not $m.Groups[1].Value.StartsWith('smoke-runner-' + $jm.Groups[2].Value, [StringComparison]::Ordinal)) { return (& $no "CACHE_DIR-closure-mismatch ($($m.Groups[1].Value))") }
        $line = $m.Value
    }
    $em = [regex]::Match($raw, '"endedUtc"\s*:\s*"([^"]+)"')
    [pscustomobject]@{ Staged = $true; Reason = 'staged-already'; EndedUtc = $(if ($em.Success) { $em.Groups[1].Value } else { 'unknown' }); Line = $line }
}

function Get-DllPairReuse {
    param(
        [Parameter(Mandatory = $true)][string]$Outbox,
        [Parameter(Mandatory = $true)][string]$JobId,
        [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Sha
    )
    $no = { param($why) [pscustomobject]@{ Reusable = $false; Reason = $why; EndedUtc = $null; Line = $null; ArtifactsDir = $null } }
    $resultPath = Join-Path $Outbox "$JobId.result.json"
    $art = Join-Path $Outbox "$JobId.artifacts"
    if (-not (Test-Path -LiteralPath $resultPath -PathType Leaf)) { return (& $no 'result-missing') }
    try { $raw = Get-Content -LiteralPath $resultPath -Raw -ErrorAction Stop; $r = $raw | ConvertFrom-Json -ErrorAction Stop } catch { return (& $no 'result-unreadable') }
    if ([string]$r.jobId -cne $JobId) { return (& $no "jobId-mismatch ($([string]$r.jobId))") }
    $code = 0
    if (-not [int]::TryParse([string]$r.exitCode, [ref]$code) -or $code -ne 0) { return (& $no "exitCode-not-0 ($([string]$r.exitCode))") }
    foreach ($flag in 'timedOut', 'cancelled', 'queueExpired', 'orphaned') {
        if ($r.$flag -eq $true) { return (& $no "$flag-true") }
    }
    $m = [regex]::Match([string]$r.stdout, '(?m)^RESULT=DLL_PAIR_OK SOURCE=([0-9a-f]{40})(?=\s|$)[^\r\n]*')
    if (-not $m.Success) { return (& $no 'no-RESULT=DLL_PAIR_OK-line') }
    if ($m.Groups[1].Value -cne $Sha) { return (& $no "SOURCE-mismatch ($($m.Groups[1].Value))") }
    $mp = Join-Path $art 'dll-pair-manifest.json'
    if (-not (Test-Path -LiteralPath $mp -PathType Leaf)) { return (& $no 'artifacts-manifest-missing') }
    try { $man = Get-Content -LiteralPath $mp -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop } catch { return (& $no 'artifacts-manifest-unreadable') }
    if ([string]$man.sourceCommit -cne $Sha) { return (& $no "manifest-sourceCommit-mismatch ($([string]$man.sourceCommit))") }
    if ([string]$man.jobId -cne $JobId) { return (& $no "manifest-jobId-mismatch ($([string]$man.jobId))") }
    [pscustomobject]@{ Reusable = $true; Reason = 'dll-pair-already-built'; EndedUtc = [string]$r.endedUtc; Line = $m.Value.TrimEnd(); ArtifactsDir = $art }
}

Export-ModuleMember -Function Test-UmRunJobIdInUseMessage, Get-StageReuse, Get-DllPairReuse
