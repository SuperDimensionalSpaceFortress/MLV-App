# fold-cadence.ps1 - tracked fold-cadence verdict step (DOCTRINE-FOLD-CADENCE-1, third BOARD-AUTOPILOT-TRACKED-1 slice).
# One read-only step for the autopilot beat. It prices the doctrine fold debt and says whether a fold lane is due. It QUEUES
# NOTHING and writes nothing to the bus: it returns one verdict object (and, with -OutputPath, writes the same JSON), and the
# caller turns DueFold into a card-queue row.
#   1. Reads the acked bus commit from the last-seen.json at -LastSeenPath (key lastSeen, time lastSeenAt).
#   2. Runs `py -3 tools/doctrine/doctrine_recall.py --fold-debt --since <sha>` under a hard deadline (-DeadlineSec, default 60;
#      the child tree is killed at the deadline). The debt scope (TRAPS / RECEIPTS / RULINGS and other projects' cards) is the
#      recall tool's; this script binds it and does not rescan the bus.
#   3. Decides with Get-FoldCadenceDecision (pure, callable by dot-sourcing this file):
#        DueFold = OwedCount > 0 AND UsageLevel == 'ok' AND (now - last fold) >= MinFoldIntervalHours (default 6).
# Verdict schema mlv-app/fold-cadence/v1:
#   schema, meterState, consecutiveUnknown, recallOutcome, recallAttempts, recallStderr, unknownReason, owedCount (null when
#   unknown), oldestOwedCommitUtc, oldestOwedAgeHours, busFetch (the recall tool's fetch status:
#   ok | no-origin | skipped | 'FETCH_FAILED <reason>', in which case the debt was priced against the local HEAD only; null when
#   the listing never came back), ackedSha, ackedAtUtc, lastFoldUtc,
#   lastFoldAgeHours, dueFold, reason, evaluatedAtUtc, elapsedSec.
# Reason (typed): due | no-owed | usage-<level> | fold-recent | timeout | last-seen-missing | last-seen-unreadable |
#   last-seen-no-sha | recall-unknown-sha | recall-failed-exit-<n> | recall-output-unreadable | error | unknown-fail-closed.
# Every failure is a verdict with exit code 0 (a beat step must never fail the beat), and an UNKNOWN debt is never read as "nothing owed"
# (FACTORY-METERS-FAIL-CLOSED-1):
#   - the recall's stderr is kept (verdict recallStderr + the rotating -LogPath log), never deleted unread;
#   - recallOutcome tells the failures apart: ok | timeout (our deadline killed the tree) | git-failure (recall exit 4: git or the
#     bus clone failed or timed out) | unknown-sha (exit 3) | failed (any other non-zero exit) | unreadable (exit 0, bad output);
#   - a timeout or git-failure is retried ONCE with a larger budget (-RetryDeadlineSec, default 45 s; git budget = the smaller of
#     -RetryGitTimeoutSec, default 30 s, and HALF the retry deadline, so 22.5 s by default), because the first failure usually
#     means a loaded host, not a broken bus;
#   - exit-0 output is trusted only if count is a non-negative integer and commits parses; anything else (null or non-integer
#     count, an unparseable commit date) is `unreadable` with owedCount null, i.e. UNKNOWN, never a healthy answer;
#   - meterState is OK | DUE | UNKNOWN. With -StatePath the step counts consecutive UNKNOWN evaluations; from the
#     -UnknownDueAfter-th one on (default 2) an unknown debt is DUE (reason unknown-fail-closed, unknownReason = what failed)
#     under the same usage and 6 h rules as a real debt. Without -StatePath the count is 1 and unknown stays not-due.
# Last fold time: -LastFoldUtc (ISO 8601) wins; else the newest <FoldRunsRoot>\*\fold-*.md write time (a fold lane's fold-report.md);
# else unknown, which counts as "no fold ran in the interval". Only run dirs whose name contains 'fold' are looked into (the real
# fleet-runs root holds thousands of dirs).
# Dot-sourcing this file defines the functions and runs nothing.
param(
    [string]$LastSeenPath,
    [string]$OutputPath,
    [int]$DeadlineSec = 60,
    [string]$UsageLevel = 'ok',
    [double]$MinFoldIntervalHours = 6,
    [string]$FoldRunsRoot,
    [string]$LastFoldUtc,
    [string]$Bus,
    [string]$RecallScript,
    [string]$PythonCommand = 'py',
    [string]$NowUtc,
    [string]$StatePath,
    [string]$LogPath,
    [long]$LogMaxBytes = 262144,
    [int]$RetryGitTimeoutSec = 30,
    [int]$RetryDeadlineSec = 45,
    [int]$UnknownDueAfter = 2
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function ConvertTo-FoldUtc($Value) {
    # JSON dates arrive as DateTime (PowerShell converts ISO strings); text arrives as a string. Both end as UTC.
    if ($null -eq $Value -or "$Value" -eq '') { return $null }
    if ($Value -is [datetime]) {
        if ($Value.Kind -eq [System.DateTimeKind]::Unspecified) { return [datetime]::SpecifyKind($Value, [System.DateTimeKind]::Utc) }
        return $Value.ToUniversalTime()
    }
    [DateTimeOffset]::Parse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal).UtcDateTime
}

function Get-FoldCadenceDecision {
    # Pure: no clock, no disk. DueFold = owed > 0 AND usage ok AND no fold in the last MinFoldIntervalHours.
    # An UNKNOWN debt (OwedCount null) fails closed: after UnknownDueAfter consecutive unknowns it is due under the same two gates.
    param(
        $OwedCount,
        [string]$UsageLevel = 'ok',
        $LastFoldUtc,
        [datetime]$NowUtc,
        [double]$MinFoldIntervalHours = 6,
        [int]$ConsecutiveUnknown = 0,
        [int]$UnknownDueAfter = 2
    )
    $level = if ($UsageLevel -and $UsageLevel.Trim()) { $UsageLevel.Trim().ToLowerInvariant() } else { 'unknown' }
    if ($null -eq $OwedCount) {
        if ($ConsecutiveUnknown -lt $UnknownDueAfter) { return [pscustomobject]@{ DueFold = $false; Reason = 'owed-unknown' } }
        if ($level -ne 'ok') { return [pscustomobject]@{ DueFold = $false; Reason = "usage-$level" } }
        $lastU = ConvertTo-FoldUtc $LastFoldUtc
        if ($null -ne $lastU -and (($NowUtc.ToUniversalTime() - $lastU).TotalHours -lt $MinFoldIntervalHours)) {
            return [pscustomobject]@{ DueFold = $false; Reason = 'fold-recent' }
        }
        return [pscustomobject]@{ DueFold = $true; Reason = 'unknown-fail-closed' }
    }
    if ([int]$OwedCount -le 0) { return [pscustomobject]@{ DueFold = $false; Reason = 'no-owed' } }
    if ($level -ne 'ok') { return [pscustomobject]@{ DueFold = $false; Reason = "usage-$level" } }
    $last = ConvertTo-FoldUtc $LastFoldUtc
    if ($null -ne $last -and (($NowUtc.ToUniversalTime() - $last).TotalHours -lt $MinFoldIntervalHours)) {
        return [pscustomobject]@{ DueFold = $false; Reason = 'fold-recent' }
    }
    [pscustomobject]@{ DueFold = $true; Reason = 'due' }
}

function New-FoldCadenceVerdict([string]$Reason, [datetime]$Now, [double]$ElapsedSec) {
    [ordered]@{
        schema              = 'mlv-app/fold-cadence/v1'
        meterState          = 'UNKNOWN'
        consecutiveUnknown  = 0
        recallOutcome       = $null
        recallAttempts      = @()
        recallStderr        = $null
        unknownReason       = $null
        owedCount           = $null
        oldestOwedCommitUtc = $null
        oldestOwedAgeHours  = $null
        busFetch            = $null
        ackedSha            = $null
        ackedAtUtc          = $null
        lastFoldUtc         = $null
        lastFoldAgeHours    = $null
        dueFold             = $false
        reason              = $Reason
        evaluatedAtUtc      = $Now.ToString('o')
        elapsedSec          = [math]::Round($ElapsedSec, 2)
    }
}

function Get-NewestFoldReportUtc([string]$Root) {
    if (-not $Root -or -not (Test-Path -LiteralPath $Root -PathType Container)) { return $null }
    # Filter the run-dir NAMES first: the real fleet-runs root holds thousands of dirs and a '<root>\*\fold-*.md' glob took 14 s.
    $files = foreach ($d in (Get-ChildItem -LiteralPath $Root -Directory -Filter '*fold*' -ErrorAction SilentlyContinue)) {
        Get-ChildItem -LiteralPath $d.FullName -File -Filter 'fold-*.md' -ErrorAction SilentlyContinue
    }
    $f = @($files) | Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($f) { $f.LastWriteTimeUtc } else { $null }
}

function Read-SharedText([string]$Path) {
    # Read a file another process may still hold open (a killed recall tree releases its redirect handles a moment late).
    try {
        $fs = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        try { $sr = [IO.StreamReader]::new($fs, [Text.Encoding]::UTF8); return $sr.ReadToEnd() } finally { $fs.Dispose() }
    } catch { return '' }
}

function Format-StderrTail([string]$Text, [int]$Max = 600) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $t = (($Text -replace '\s+', ' ') -replace '[^\x20-\x7E]', '?').Trim()
    if ($t.Length -gt $Max) { $t = '...' + $t.Substring($t.Length - ($Max - 3)) }
    $t
}

function Invoke-RecallAttempt {
    # One run of `doctrine_recall.py --fold-debt` under a hard deadline; the child tree is killed at the deadline.
    # outcome: ok (exit 0) | timeout (our deadline) | unknown-sha (exit 3) | git-failure (exit 4) | failed (any other exit).
    # A launcher that cannot start throws (the caller types that as 'error').
    param([string]$PythonCommand, [string]$Script, [string]$Sha, [string]$Bus, [int]$DeadlineSec, [double]$GitSec, [double]$FetchSec)
    $inv = [Globalization.CultureInfo]::InvariantCulture
    $argList = @('-3', "`"$Script`"", '--fold-debt', '--since', $Sha, '--git-timeout', [string]::Format($inv, '{0}', $GitSec), '--fetch-timeout', [string]::Format($inv, '{0}', $FetchSec))
    if ($Bus) { $argList += @('--bus', "`"$Bus`"") }
    $stem = Join-Path ([IO.Path]::GetTempPath()) ('fold-cadence-' + [guid]::NewGuid().ToString('N'))
    $tmpOut = "$stem.out"; $tmpErr = "$stem.err"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $r = [ordered]@{ outcome = 'ok'; exitCode = $null; elapsedSec = 0; gitTimeoutSec = $GitSec; stdout = ''; stderr = $null }
    try {
        $p = Start-Process -FilePath $PythonCommand -ArgumentList $argList -NoNewWindow -PassThru -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
        $null = $p.Handle
        if (-not $p.WaitForExit($DeadlineSec * 1000)) {
            try { $p.Kill($true) } catch {}
            $r.outcome = 'timeout'
        } else {
            $r.exitCode = $p.ExitCode
            $r.outcome = switch ($p.ExitCode) { 0 { 'ok' } 3 { 'unknown-sha' } 4 { 'git-failure' } default { 'failed' } }
        }
        $r.stderr = Format-StderrTail (Read-SharedText $tmpErr)
        if ($r.outcome -eq 'ok') { $r.stdout = Read-SharedText $tmpOut }
    } finally {
        foreach ($f in @($tmpOut, $tmpErr)) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }
        $r.elapsedSec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
    }
    [pscustomobject]$r
}

function Invoke-FoldCadenceCore {
    # One evaluation: the typed verdict, owedCount null whenever the debt could not be priced. State, retry bookkeeping and the
    # fail-closed rule live in Invoke-FoldCadence, which wraps this.
    param($Opts)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $now = if ($Opts.NowUtc) { ConvertTo-FoldUtc $Opts.NowUtc } else { [datetime]::UtcNow }
    $v = New-FoldCadenceVerdict 'error' $now 0
    $parsing = $false
    try {
        # Last fold time first: it is known whatever happens to the debt listing.
        $lastFold = if ($Opts.LastFoldUtc) { ConvertTo-FoldUtc $Opts.LastFoldUtc } else { Get-NewestFoldReportUtc $Opts.FoldRunsRoot }
        if ($null -ne $lastFold) {
            $v.lastFoldUtc = $lastFold.ToString('o')
            $v.lastFoldAgeHours = [math]::Round(($now - $lastFold).TotalHours, 2)
        }

        if (-not $Opts.LastSeenPath -or -not (Test-Path -LiteralPath $Opts.LastSeenPath -PathType Leaf)) { $v.reason = 'last-seen-missing'; return $v }
        $seen = try { Get-Content -LiteralPath $Opts.LastSeenPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $null }
        if ($null -eq $seen -or $seen -isnot [pscustomobject]) { $v.reason = 'last-seen-unreadable'; return $v }
        $sha = if ($seen.PSObject.Properties['lastSeen']) { [string]$seen.lastSeen } else { '' }
        if ($sha -notmatch '^[0-9a-fA-F]{4,40}$') { $v.reason = 'last-seen-no-sha'; return $v }
        $v.ackedSha = $sha
        if ($seen.PSObject.Properties['lastSeenAt']) {
            $at = try { ConvertTo-FoldUtc $seen.lastSeenAt } catch { $null }
            if ($null -ne $at) { $v.ackedAtUtc = $at.ToString('o') }
        }

        $script = if ($Opts.RecallScript) { $Opts.RecallScript } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\doctrine\doctrine_recall.py')) }
        $deadline = [math]::Max(1, [int]$Opts.DeadlineSec)
        # The recall tool fetches the bus first (DOCTRINE-BUS-CLONE-CURRENT-1); give that fetch its own slice of the deadline so
        # a slow remote costs a FETCH_FAILED note in the verdict, not a 'timeout' verdict that hides the debt.
        $gitSec = [math]::Min(10.0, [math]::Max(1.0, $deadline * 0.8))
        $fetchSec = [math]::Min(30.0, [math]::Max(1.0, $deadline * 0.4))
        $a = Invoke-RecallAttempt -PythonCommand $Opts.PythonCommand -Script $script -Sha $sha -Bus $Opts.Bus -DeadlineSec $deadline -GitSec $gitSec -FetchSec $fetchSec
        $attempts = @($a)
        if ($a.outcome -in 'timeout', 'git-failure') {
            # A timeout or a git failure usually means a loaded host, not a broken bus: retry ONCE with a larger budget.
            $rd = [math]::Max(1, [int]$Opts.RetryDeadlineSec)
            $rg = [math]::Min([double]$Opts.RetryGitTimeoutSec, [math]::Max(1.0, $rd * 0.5))
            $rf = [math]::Min(30.0, [math]::Max(1.0, $rd * 0.4))
            $a = Invoke-RecallAttempt -PythonCommand $Opts.PythonCommand -Script $script -Sha $sha -Bus $Opts.Bus -DeadlineSec $rd -GitSec $rg -FetchSec $rf
            $attempts += $a
        }
        $v.recallAttempts = @($attempts | ForEach-Object { [ordered]@{ outcome = $_.outcome; exitCode = $_.exitCode; elapsedSec = $_.elapsedSec; gitTimeoutSec = $_.gitTimeoutSec } })
        $v.recallOutcome = $a.outcome
        $withErr = @($attempts | Where-Object { $_.stderr })
        if ($withErr.Count) { $v.recallStderr = $withErr[-1].stderr }
        if ($a.outcome -eq 'timeout') { $v.reason = 'timeout'; return $v }
        if ($a.outcome -eq 'unknown-sha') { $v.reason = 'recall-unknown-sha'; return $v }
        if ($a.outcome -ne 'ok') { $v.reason = "recall-failed-exit-$($a.exitCode)"; return $v }
        $parsing = $true
        $debt = try { $a.stdout | ConvertFrom-Json } catch { $null }
        # The count is TYPE-checked, not presence-checked: {"count":null} casts to 0 and would read as "nothing owed".
        $shapeOk = ($debt -is [pscustomobject]) -and $debt.PSObject.Properties['count'] -and $debt.PSObject.Properties['commits'] -and
            (($debt.count -is [int]) -or ($debt.count -is [long])) -and ($debt.count -ge 0)
        if (-not $shapeOk) { $v.recallOutcome = 'unreadable'; $v.reason = 'recall-output-unreadable'; return $v }
        # Everything is parsed into locals first; the verdict takes owedCount only after the whole parse and decision succeeded,
        # so an exception half way leaves the debt UNKNOWN (null), never a retained count read as a healthy answer.
        $owed = [int]$debt.count
        $fetch = if ($debt.PSObject.Properties['fetch']) { [string]$debt.fetch } else { $null }
        $dates = @(@($debt.commits) | ForEach-Object { ConvertTo-FoldUtc $_.date } | Where-Object { $null -ne $_ } | Sort-Object)
        $oldest = $null; $oldestAge = $null
        if ($dates.Count -gt 0) {
            $oldest = $dates[0].ToString('o')
            $oldestAge = [math]::Round(($now - $dates[0]).TotalHours, 2)
        }
        $d = Get-FoldCadenceDecision -OwedCount $owed -UsageLevel $Opts.UsageLevel -LastFoldUtc $lastFold -NowUtc $now -MinFoldIntervalHours $Opts.MinFoldIntervalHours
        $v.busFetch = $fetch
        $v.oldestOwedCommitUtc = $oldest
        $v.oldestOwedAgeHours = $oldestAge
        $v.owedCount = $owed
        $v.dueFold = [bool]$d.DueFold
        $v.reason = $d.Reason
        return $v
    } catch {
        $v.owedCount = $null
        $v.dueFold = $false
        $v.reason = 'error'
        # A throw while reading the recall's own output is an unreadable answer, typed as such.
        if ($parsing) { $v.recallOutcome = 'unreadable'; $v.reason = 'recall-output-unreadable' }
        return $v
    } finally {
        $v.elapsedSec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
    }
}

function Read-FoldCadenceState([string]$Path) {
    $s = [ordered]@{ consecutiveUnknown = 0; lastHealthyUtc = $null }
    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $s }
    try {
        $j = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($j.PSObject.Properties['consecutiveUnknown']) { $s.consecutiveUnknown = [math]::Max(0, [int]$j.consecutiveUnknown) }
        if ($j.PSObject.Properties['lastHealthyUtc'] -and $j.lastHealthyUtc) { $s.lastHealthyUtc = ([string]$j.lastHealthyUtc) }
    } catch {}
    $s
}

function Write-FoldCadenceLog([string]$Path, [long]$MaxBytes, $Verdict) {
    # One JSON line per evaluation that was not a clean first-try answer; rotated by rename (keeps .1 .2 .3), never unbounded.
    if (-not $Path) { return }
    $failed = @($Verdict.recallAttempts | Where-Object { $_.outcome -ne 'ok' })
    if ($null -ne $Verdict.owedCount -and -not $failed.Count) { return }
    try {
        $dir = Split-Path -Parent $Path
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        if ((Test-Path -LiteralPath $Path) -and (Get-Item -LiteralPath $Path).Length -gt $MaxBytes) {
            Remove-Item -LiteralPath "$Path.3" -Force -ErrorAction SilentlyContinue
            foreach ($n in 2, 1) { if (Test-Path -LiteralPath "$Path.$n") { Move-Item -LiteralPath "$Path.$n" -Destination ("$Path." + ($n + 1)) -Force } }
            Move-Item -LiteralPath $Path -Destination "$Path.1" -Force
        }
        $line = [ordered]@{ utc = $Verdict.evaluatedAtUtc; meterState = $Verdict.meterState; reason = $Verdict.reason; recallOutcome = $Verdict.recallOutcome
            consecutiveUnknown = $Verdict.consecutiveUnknown; dueFold = $Verdict.dueFold; attempts = $Verdict.recallAttempts; stderr = $Verdict.recallStderr }
        Add-Content -LiteralPath $Path -Value (ConvertTo-Json -InputObject $line -Compress -Depth 5) -Encoding ascii
    } catch {}
}

function Invoke-FoldCadence {
    # The evaluation plus the fail-closed bookkeeping: an unknown debt is counted, kept visible (meterState UNKNOWN) and, from the
    # UnknownDueAfter-th consecutive unknown on, treated as due. Never throws.
    param($Opts)
    $v = Invoke-FoldCadenceCore $Opts
    try {
        $now = ConvertTo-FoldUtc $v.evaluatedAtUtc
        $state = Read-FoldCadenceState $Opts.StatePath
        $unknown = ($null -eq $v.owedCount)
        $n = if ($unknown) { [int]$state.consecutiveUnknown + 1 } else { 0 }
        $v.consecutiveUnknown = $n
        if ($unknown) {
            $v.meterState = 'UNKNOWN'
            $v.unknownReason = $v.reason
            $after = if ($Opts.UnknownDueAfter -gt 0) { [int]$Opts.UnknownDueAfter } else { 2 }
            $d = Get-FoldCadenceDecision -OwedCount $null -UsageLevel $Opts.UsageLevel -LastFoldUtc $v.lastFoldUtc -NowUtc $now -MinFoldIntervalHours $Opts.MinFoldIntervalHours -ConsecutiveUnknown $n -UnknownDueAfter $after
            if ($d.DueFold) { $v.dueFold = $true; $v.reason = $d.Reason }
        } else {
            $v.meterState = if ($v.dueFold) { 'DUE' } else { 'OK' }
        }
        if ($Opts.StatePath) {
            $healthy = if ($unknown) { $state.lastHealthyUtc } else { $v.evaluatedAtUtc }
            $st = [ordered]@{ schema = 'mlv-app/fold-cadence-state/v1'; consecutiveUnknown = $n; lastHealthyUtc = $healthy; lastReason = $(if ($unknown) { $v.unknownReason } else { $v.reason })
                lastRecallOutcome = $v.recallOutcome; updatedUtc = $v.evaluatedAtUtc }
            $dir = Split-Path -Parent $Opts.StatePath
            if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
            $tmp = "$($Opts.StatePath).tmp"
            (ConvertTo-Json -InputObject $st -Depth 3) | Set-Content -LiteralPath $tmp -Encoding ascii
            Move-Item -LiteralPath $tmp -Destination $Opts.StatePath -Force
        }
        Write-FoldCadenceLog $Opts.LogPath $Opts.LogMaxBytes $v
    } catch {}
    $v
}

# Dot-sourced (by a test or the beat): functions only.
if ($MyInvocation.InvocationName -eq '.') { return }

$verdict = $null
try {
    $verdict = Invoke-FoldCadence ([pscustomobject]@{
            LastSeenPath = $LastSeenPath; DeadlineSec = $DeadlineSec; UsageLevel = $UsageLevel
            MinFoldIntervalHours = $MinFoldIntervalHours; FoldRunsRoot = $FoldRunsRoot; LastFoldUtc = $LastFoldUtc
            Bus = $Bus; RecallScript = $RecallScript; PythonCommand = $PythonCommand; NowUtc = $NowUtc
            StatePath = $StatePath; LogPath = $LogPath; LogMaxBytes = $LogMaxBytes; RetryGitTimeoutSec = $RetryGitTimeoutSec
            RetryDeadlineSec = $RetryDeadlineSec; UnknownDueAfter = $UnknownDueAfter
        })
} catch {
    $verdict = New-FoldCadenceVerdict 'error' ([datetime]::UtcNow) 0
}
if ($OutputPath) {
    try {
        $dir = Split-Path -Parent $OutputPath
        if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        $tmp = "$OutputPath.tmp"
        ($verdict | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $tmp -Encoding ascii
        Move-Item -LiteralPath $tmp -Destination $OutputPath -Force
    } catch {}
}
[pscustomobject]$verdict
exit 0
