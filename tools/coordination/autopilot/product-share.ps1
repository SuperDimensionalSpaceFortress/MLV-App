# product-share.ps1 - tracked product-share verdict step for the hub tick (PRODUCT-SHARE-BIND-1).
# Binds the existing Test-ProductRatioGuard.ps1 (no new ratio harness) into a beat step that can never block the beat:
#   * the guard runs as a child under a hard deadline (-DeadlineSec, default 90); a timeout or error is verdict UNKNOWN, never RED;
#   * the verdict is cached in a small JSON file (schema mlv-app/product-share-verdict/v1); a GREEN/RED verdict younger than
#     -CacheMinutes (default 60) is returned without re-running the guard;
#   * -Detach (what the beat should use): on a stale cache the guard is refreshed by ONE detached hidden worker with its own
#     longer deadline (-WorkerDeadlineSec, default 900) and this call returns at once with the last definitive verdict marked
#     stale (up to -MaxStaleMinutes, default 1440), else UNKNOWN. The guard was measured at 529 s on the board, so a foreground
#     90 s run always times out there; the detached refresh is what makes the verdict visible every tick.
# Bus rule B: the verdict NEVER changes a caller's exit code. This script exits 0 for GREEN, RED and UNKNOWN alike and never
# throws to its caller. It reads the repo and writes only its own verdict/attempt/lock files (outside the repo by default).
# Output (one JSON object on stdout): the verdict record plus cache (run|hit|backoff|stale|refreshing|none), ageSec, stale, line
# ("Product share (7 d): <verdict> <share> of <threshold>") and, with -CardsPath, plan (see Get-ProductSlotPlan).
# Dot-sourcing this file defines the functions and runs nothing (the tests and the beat's D step use Get-ProductSlotPlan that way).
param(
    [string]$RepoRoot,
    [string]$SourceRef,
    [string]$GuardPath,
    [string]$VerdictPath,
    [string]$ProfilePath = (Join-Path $PSScriptRoot 'board-profile.json'),
    [string]$CardsPath,
    [int]$DeadlineSec = 90,
    [int]$WorkerDeadlineSec = 900,
    [int]$CacheMinutes = 60,
    [int]$UnknownCacheMinutes = 15,
    [int]$MaxStaleMinutes = 1440,
    [switch]$Detach,
    [switch]$Worker,
    [switch]$Force
)
Set-StrictMode -Version Latest

$script:VerdictSchema = 'mlv-app/product-share-verdict/v1'
$script:HubToolingKinds = @('hub-tooling', 'incident')

function Get-RowValue($Row, [string]$Name) {
    if ($null -eq $Row) { return $null }
    if ($Row -is [System.Collections.IDictionary]) { if ($Row.Contains($Name)) { return $Row[$Name] } else { return $null } }
    $p = $Row.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}

function Get-ProductSlotPlan {
    # Pure D-step rule. Verdict: a verdict string or any object with a .verdict. Cards: rows with state (queued|running),
    # kind and touchesProduct (true when the card's paths touch src/ or platform/; the label alone is not trusted).
    # reserveProductSlot is true only on RED while a queued card touches product. maxHubToolingLanes is 1 in that case
    # (at most one concurrent incident or hub-tooling lane while product is due), else $null (unbounded).
    param($Verdict, [object[]]$Cards = @())
    $v = 'UNKNOWN'
    if ($Verdict -is [string]) { $v = $Verdict } elseif ($null -ne $Verdict) { $v = [string](Get-RowValue $Verdict 'verdict') }
    $v = $v.Trim().ToUpperInvariant()
    if ($v -ne 'GREEN' -and $v -ne 'RED') { $v = 'UNKNOWN' }
    $productDue = $false
    $toolingRunning = 0
    foreach ($row in @($Cards)) {
        $state = ([string](Get-RowValue $row 'state')).ToLowerInvariant()
        $kind = ([string](Get-RowValue $row 'kind')).ToLowerInvariant()
        if ($state -eq 'queued' -and (Get-RowValue $row 'touchesProduct') -eq $true) { $productDue = $true }
        if ($state -eq 'running' -and $script:HubToolingKinds -contains $kind) { $toolingRunning++ }
    }
    $reserve = ($v -eq 'RED') -and $productDue
    $max = if ($reserve) { 1 } else { $null }
    [pscustomobject]@{
        verdict = $v
        productDue = $productDue
        reserveProductSlot = $reserve
        maxHubToolingLanes = $max
        hubToolingRunning = $toolingRunning
        mayStartHubTooling = ($null -eq $max) -or ($toolingRunning -lt $max)
        reason = $(if ($reserve) { 'RED with a product card due' } elseif ($v -eq 'RED') { 'RED, no product card due' } else { "$v (no reservation)" })
    }
}

function Invoke-ProductAct {
    # Runs the verdict step (never throws, never alters the act) and then the product act; returns the ACT's own exit code.
    # Bus rule B: a RED verdict reserves a slot, it never changes the exit code of a product act.
    param([Parameter(Mandatory)][string[]]$ActCommand, [hashtable]$ShareArgs = @{})
    try { $null = & $PSCommandPath @ShareArgs } catch { }
    $global:LASTEXITCODE = 0
    $rest = @($ActCommand | Select-Object -Skip 1)
    & $ActCommand[0] @rest
    return $LASTEXITCODE
}

function ConvertTo-AsciiJson($Object) { ($Object | ConvertTo-Json -Depth 6 -Compress) -replace '[^\x00-\x7F]', '?' }

function Write-FileAtomic([string]$Path, [string]$Text) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $tmp = "$Path.$PID.tmp"
    [System.IO.File]::WriteAllText($tmp, $Text, [System.Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

function Read-JsonFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    # ISO timestamps must stay strings: the default parse turns them into local DateTime values and a re-emit would shift them.
    try {
        $raw = Get-Content -LiteralPath $Path -Raw -ErrorAction Stop
        if ((Get-Command ConvertFrom-Json).Parameters.ContainsKey('DateKind')) { return ($raw | ConvertFrom-Json -DateKind String -ErrorAction Stop) }
        return ($raw | ConvertFrom-Json -ErrorAction Stop)
    } catch { return $null }
}

function Get-AgeSec([string]$Stamp) {
    try { return [int64]([datetimeoffset]::UtcNow - [datetimeoffset]::Parse($Stamp, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind)).TotalSeconds } catch { return [int64]::MaxValue }
}

function New-VerdictRecord {
    param([string]$Verdict, $ProductShare, $Threshold, [double]$ElapsedSec, [string]$SourceRef, [string]$Reason, [string[]]$GuardReasons = @(), $GuardExit = $null)
    [ordered]@{
        schema = $script:VerdictSchema
        verdict = $Verdict
        productShare = $ProductShare
        threshold = $Threshold
        evaluatedAtUtc = [datetimeoffset]::UtcNow.ToString('o')
        elapsedSec = [math]::Round($ElapsedSec, 1)
        sourceRef = $SourceRef
        reason = $Reason
        guardReasons = @($GuardReasons)
        guardExit = $GuardExit
    }
}

function Invoke-GuardWithDeadline {
    # Runs the guard as a child process; on deadline the whole process tree is killed. Returns Exit, Out, TimedOut, Error.
    param([string]$Guard, [string]$Repo, [string]$Ref, [int]$Deadline, [string]$ScratchDir)
    $outF = Join-Path $ScratchDir "guard-$PID.out.txt"
    $errF = Join-Path $ScratchDir "guard-$PID.err.txt"
    $exe = (Get-Process -Id $PID).Path
    $argList = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$Guard`"", '-RepoRoot', "`"$Repo`"")
    if ($Ref) { $argList += @('-SourceRef', "`"$Ref`"") }
    try {
        $p = Start-Process -FilePath $exe -ArgumentList $argList -NoNewWindow -PassThru -RedirectStandardOutput $outF -RedirectStandardError $errF
        if (-not $p.WaitForExit($Deadline * 1000)) {
            try { $p.Kill($true) } catch { }
            return [pscustomobject]@{ Exit = $null; Out = ''; TimedOut = $true; Error = '' }
        }
        $p.WaitForExit()
        $o = if (Test-Path -LiteralPath $outF) { [System.IO.File]::ReadAllText($outF) } else { '' }
        $e = if (Test-Path -LiteralPath $errF) { [System.IO.File]::ReadAllText($errF) } else { '' }
        return [pscustomobject]@{ Exit = $p.ExitCode; Out = $o; TimedOut = $false; Error = $e }
    } catch {
        return [pscustomobject]@{ Exit = $null; Out = ''; TimedOut = $false; Error = "start-failed: $($_.Exception.Message)" }
    } finally {
        foreach ($f in @($outF, $errF)) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
    }
}

function Get-GuardVerdictRecord {
    # Maps one guard run to a verdict record. GREEN and RED pass through; ERROR, a timeout, bad JSON or a missing verdict are UNKNOWN.
    param($Run, [double]$Elapsed, [string]$Ref, [int]$Deadline)
    if ($Run.TimedOut) { return (New-VerdictRecord 'UNKNOWN' $null 0.5 $Elapsed $Ref "guard-timeout (killed after ${Deadline}s)") }
    if ($Run.Error) { return (New-VerdictRecord 'UNKNOWN' $null 0.5 $Elapsed $Ref "guard-error: $($Run.Error)" -GuardExit $Run.Exit) }
    $doc = $null
    try { $doc = ($Run.Out | ConvertFrom-Json -ErrorAction Stop) } catch { }
    if ($null -eq $doc -or $null -eq $doc.PSObject.Properties['verdict']) {
        return (New-VerdictRecord 'UNKNOWN' $null 0.5 $Elapsed $Ref "guard-output-unreadable (exit $($Run.Exit))" -GuardExit $Run.Exit)
    }
    $gv = ([string]$doc.verdict).ToUpperInvariant()
    $share = if ($doc.PSObject.Properties['productShare7d']) { $doc.productShare7d } else { $null }
    $thr = if ($doc.PSObject.Properties['productShareThreshold']) { $doc.productShareThreshold } else { 0.5 }
    $reasons = if ($doc.PSObject.Properties['reasons']) { @($doc.reasons | ForEach-Object { [string]$_ }) } else { @() }
    $gref = if ($doc.PSObject.Properties['sourceRef'] -and $doc.sourceRef) { [string]$doc.sourceRef } else { $Ref }
    if ($gv -eq 'GREEN' -or $gv -eq 'RED') {
        return (New-VerdictRecord $gv $share $thr $Elapsed $gref "guard-$($gv.ToLowerInvariant())" $reasons $Run.Exit)
    }
    $code = if ($doc.PSObject.Properties['errorCode'] -and $doc.errorCode) { [string]$doc.errorCode } else { $gv }
    return (New-VerdictRecord 'UNKNOWN' $share $thr $Elapsed $gref "guard-error: $code" $reasons $Run.Exit)
}

function Invoke-ShareEvaluation {
    # One guarded guard run. A GREEN/RED result replaces the verdict file; an UNKNOWN one leaves the last definitive verdict in
    # place and only records the attempt (so a failure can back off without erasing what is known).
    param([string]$Guard, [string]$Repo, [string]$Ref, [int]$Deadline, [string]$VerdictFile)
    $dir = Split-Path -Parent $VerdictFile
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $run = Invoke-GuardWithDeadline -Guard $Guard -Repo $Repo -Ref $Ref -Deadline $Deadline -ScratchDir $dir
    $sw.Stop()
    $rec = Get-GuardVerdictRecord -Run $run -Elapsed $sw.Elapsed.TotalSeconds -Ref $Ref -Deadline $Deadline
    if ($rec.verdict -eq 'UNKNOWN') {
        Write-FileAtomic "$VerdictFile.attempt.json" (ConvertTo-AsciiJson ([ordered]@{ attemptedAtUtc = $rec.evaluatedAtUtc; outcome = 'UNKNOWN'; reason = $rec.reason; elapsedSec = $rec.elapsedSec }))
    } else {
        Write-FileAtomic $VerdictFile (ConvertTo-AsciiJson $rec)
        Remove-Item -LiteralPath "$VerdictFile.attempt.json" -Force -ErrorAction SilentlyContinue
    }
    return $rec
}

function Test-WorkerLive([string]$LockFile, [int]$MaxSec) {
    $lock = Read-JsonFile $LockFile
    if ($null -eq $lock) { return $false }
    if ((Get-AgeSec ([string]$lock.startedAtUtc)) -gt $MaxSec) { return $false }
    return $null -ne (Get-Process -Id ([int]$lock.pid) -ErrorAction SilentlyContinue)
}

function Resolve-ProductShare {
    param([string]$Guard, [string]$Repo, [string]$Ref, [string]$VerdictFile, [int]$Deadline, [int]$WorkerDeadline,
          [int]$CacheMin, [int]$UnknownCacheMin, [int]$MaxStaleMin, [bool]$DetachMode, [bool]$ForceRun, [string]$ProfileFile)
    $def = Read-JsonFile $VerdictFile
    if ($null -ne $def -and ($null -eq $def.PSObject.Properties['schema'] -or $def.schema -ne $script:VerdictSchema)) { $def = $null }
    $defAge = if ($null -ne $def) { Get-AgeSec ([string]$def.evaluatedAtUtc) } else { [int64]::MaxValue }
    $note = { param($rec, [string]$cache, [bool]$stale, [int64]$age)
        $o = [ordered]@{}
        foreach ($p in $rec.PSObject.Properties) { $o[$p.Name] = $p.Value }
        $o['cache'] = $cache; $o['stale'] = $stale; $o['ageSec'] = $(if ($age -eq [int64]::MaxValue) { $null } else { $age })
        $o }
    if (-not $ForceRun -and $null -ne $def -and $defAge -lt ($CacheMin * 60)) { return (& $note $def 'hit' $false $defAge) }

    $fallback = {
        param([string]$cache, [string]$why)
        if ($null -ne $def -and $defAge -lt ($MaxStaleMin * 60)) { return (& $note $def $cache $true $defAge) }
        $u = New-VerdictRecord 'UNKNOWN' $null 0.5 0 $Ref $why
        $o = [ordered]@{}; foreach ($k in $u.Keys) { $o[$k] = $u[$k] }
        $o['cache'] = $cache; $o['stale'] = $false; $o['ageSec'] = $null
        $o
    }

    $attempt = Read-JsonFile "$VerdictFile.attempt.json"
    if (-not $ForceRun -and $null -ne $attempt -and (Get-AgeSec ([string]$attempt.attemptedAtUtc)) -lt ($UnknownCacheMin * 60)) {
        return (& $fallback 'backoff' ("recent-failed-attempt: " + [string]$attempt.reason))
    }

    if ($DetachMode) {
        $lockFile = "$VerdictFile.lock"
        if (Test-WorkerLive $lockFile ($WorkerDeadline + 120)) { return (& $fallback 'refreshing' 'refresh-in-progress (no definitive verdict yet)') }
        $exe = (Get-Process -Id $PID).Path
        $argList = @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"", '-Worker',
            '-GuardPath', "`"$Guard`"", '-RepoRoot', "`"$Repo`"", '-VerdictPath', "`"$VerdictFile`"", '-ProfilePath', "`"$ProfileFile`"",
            '-WorkerDeadlineSec', "$WorkerDeadline")
        if ($Ref) { $argList += @('-SourceRef', "`"$Ref`"") }
        try {
            Start-Process -FilePath $exe -ArgumentList $argList -WindowStyle Hidden | Out-Null
            return (& $fallback 'refreshing' 'refresh-started (no definitive verdict yet)')
        } catch {
            return (& $fallback 'none' "refresh-start-failed: $($_.Exception.Message)")
        }
    }

    $rec = Invoke-ShareEvaluation -Guard $Guard -Repo $Repo -Ref $Ref -Deadline $Deadline -VerdictFile $VerdictFile
    $o = [ordered]@{}; foreach ($k in $rec.Keys) { $o[$k] = $rec[$k] }
    $o['cache'] = 'run'; $o['stale'] = $false; $o['ageSec'] = 0
    return $o
}

function Invoke-ShareWorker {
    # The detached refresh: take the lock (one worker at a time), run the guard with the long deadline, release the lock.
    param([string]$Guard, [string]$Repo, [string]$Ref, [string]$VerdictFile, [int]$WorkerDeadline)
    $lockFile = "$VerdictFile.lock"
    $dir = Split-Path -Parent $VerdictFile
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (Test-Path -LiteralPath $lockFile) {
        if (Test-WorkerLive $lockFile ($WorkerDeadline + 120)) { return }
        Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue
    }
    try { $fs = [System.IO.File]::Open($lockFile, [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::Read) } catch { return }
    try {
        $b = [System.Text.Encoding]::ASCII.GetBytes((ConvertTo-AsciiJson ([ordered]@{ pid = $PID; startedAtUtc = [datetimeoffset]::UtcNow.ToString('o') })))
        $fs.Write($b, 0, $b.Length); $fs.Dispose()
        $null = Invoke-ShareEvaluation -Guard $Guard -Repo $Repo -Ref $Ref -Deadline $WorkerDeadline -VerdictFile $VerdictFile
    } finally {
        try { $fs.Dispose() } catch { }
        Remove-Item -LiteralPath $lockFile -Force -ErrorAction SilentlyContinue
    }
}

if ($MyInvocation.InvocationName -eq '.') { return }

# ---- main: never throws, always exits 0 (bus rule B) ----
$ErrorActionPreference = 'Stop'
try {
    $prof = $null
    try { Import-Module (Join-Path $PSScriptRoot 'BoardProfile.psm1') -Force; $prof = Get-BoardProfile -Path $ProfilePath } catch { }
    if (-not $RepoRoot) { $RepoRoot = if ($prof) { $prof.boardRoot } elseif ($env:MLV_BOARD_ROOT) { $env:MLV_BOARD_ROOT } else { Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)) } }
    if (-not $SourceRef -and $prof) { $SourceRef = "$($prof.remote)/$($prof.branch)" }
    if (-not $GuardPath) { $GuardPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'Test-ProductRatioGuard.ps1' }
    if (-not $VerdictPath) {
        $proj = if ($prof) { $prof.project } else { 'board' }
        $base = if ($env:PRODUCT_SHARE_DIR) { $env:PRODUCT_SHARE_DIR } else { Join-Path ([IO.Path]::GetTempPath()) "product-share-$proj" }
        $VerdictPath = Join-Path $base 'product-share-verdict.json'
    }
    if ($Worker) { Invoke-ShareWorker -Guard $GuardPath -Repo $RepoRoot -Ref $SourceRef -VerdictFile $VerdictPath -WorkerDeadline $WorkerDeadlineSec; exit 0 }

    $res = Resolve-ProductShare -Guard $GuardPath -Repo $RepoRoot -Ref $SourceRef -VerdictFile $VerdictPath -Deadline $DeadlineSec `
        -WorkerDeadline $WorkerDeadlineSec -CacheMin $CacheMinutes -UnknownCacheMin $UnknownCacheMinutes -MaxStaleMin $MaxStaleMinutes `
        -DetachMode $Detach.IsPresent -ForceRun $Force.IsPresent -ProfileFile $ProfilePath
    $shareText = if ($null -ne $res['productShare']) { ([double]$res['productShare']).ToString('0.00', [Globalization.CultureInfo]::InvariantCulture) } else { 'n/a' }
    $thrText = if ($null -ne $res['threshold']) { ([double]$res['threshold']).ToString('0.00', [Globalization.CultureInfo]::InvariantCulture) } else { '0.50' }
    $res['line'] = "Product share (7 d): $($res['verdict']) $shareText of $thrText$(if ($res['stale']) { ' [stale]' })"
    if ($CardsPath) {
        $cards = @()
        $doc = Read-JsonFile $CardsPath
        if ($null -ne $doc) { $cards = @($doc) }
        $res['plan'] = Get-ProductSlotPlan -Verdict $res['verdict'] -Cards $cards
    }
    ConvertTo-AsciiJson $res
} catch {
    ConvertTo-AsciiJson ([ordered]@{ schema = $script:VerdictSchema; verdict = 'UNKNOWN'; productShare = $null; threshold = 0.5
            evaluatedAtUtc = [datetimeoffset]::UtcNow.ToString('o'); elapsedSec = 0; sourceRef = "$SourceRef"; reason = "step-error: $($_.Exception.Message)"
            cache = 'none'; stale = $false; ageSec = $null; line = 'Product share (7 d): UNKNOWN n/a of 0.50' })
}
exit 0
