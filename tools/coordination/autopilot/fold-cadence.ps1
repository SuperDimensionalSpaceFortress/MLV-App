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
#   schema, owedCount (null when unknown), oldestOwedCommitUtc, oldestOwedAgeHours, ackedSha, ackedAtUtc, lastFoldUtc,
#   lastFoldAgeHours, dueFold, reason, evaluatedAtUtc, elapsedSec.
# Reason (typed): due | no-owed | usage-<level> | fold-recent | timeout | last-seen-missing | last-seen-unreadable |
#   last-seen-no-sha | recall-unknown-sha | recall-failed-exit-<n> | recall-output-unreadable | error.
# Every failure is a verdict with DueFold false and exit code 0: a beat step must never fail the beat.
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
    [string]$NowUtc
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
    param(
        $OwedCount,
        [string]$UsageLevel = 'ok',
        $LastFoldUtc,
        [datetime]$NowUtc,
        [double]$MinFoldIntervalHours = 6
    )
    $level = if ($UsageLevel -and $UsageLevel.Trim()) { $UsageLevel.Trim().ToLowerInvariant() } else { 'unknown' }
    if ($null -eq $OwedCount) { return [pscustomobject]@{ DueFold = $false; Reason = 'owed-unknown' } }
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
        owedCount           = $null
        oldestOwedCommitUtc = $null
        oldestOwedAgeHours  = $null
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

function Invoke-FoldCadence {
    param($Opts)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $now = if ($Opts.NowUtc) { ConvertTo-FoldUtc $Opts.NowUtc } else { [datetime]::UtcNow }
    $v = New-FoldCadenceVerdict 'error' $now 0
    $tmpOut = $null; $tmpErr = $null
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
        $gitBudget = [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', [math]::Min(10.0, [math]::Max(1.0, $deadline * 0.8)))
        $argList = @('-3', "`"$script`"", '--fold-debt', '--since', $sha, '--git-timeout', $gitBudget)
        if ($Opts.Bus) { $argList += @('--bus', "`"$($Opts.Bus)`"") }
        $stem = Join-Path ([IO.Path]::GetTempPath()) ('fold-cadence-' + [guid]::NewGuid().ToString('N'))
        $tmpOut = "$stem.out"; $tmpErr = "$stem.err"
        $p = Start-Process -FilePath $Opts.PythonCommand -ArgumentList $argList -NoNewWindow -PassThru -RedirectStandardOutput $tmpOut -RedirectStandardError $tmpErr
        $null = $p.Handle
        if (-not $p.WaitForExit($deadline * 1000)) {
            try { $p.Kill($true) } catch {}
            $v.reason = 'timeout'; return $v
        }
        $code = $p.ExitCode
        if ($code -eq 3) { $v.reason = 'recall-unknown-sha'; return $v }
        if ($code -ne 0) { $v.reason = "recall-failed-exit-$code"; return $v }
        $debt = try { Get-Content -LiteralPath $tmpOut -Raw -Encoding UTF8 | ConvertFrom-Json } catch { $null }
        if ($null -eq $debt -or -not $debt.PSObject.Properties['count'] -or -not $debt.PSObject.Properties['commits']) { $v.reason = 'recall-output-unreadable'; return $v }
        $owed = [int]$debt.count
        $v.owedCount = $owed
        $dates = @(@($debt.commits) | ForEach-Object { ConvertTo-FoldUtc $_.date } | Where-Object { $null -ne $_ } | Sort-Object)
        if ($dates.Count -gt 0) {
            $v.oldestOwedCommitUtc = $dates[0].ToString('o')
            $v.oldestOwedAgeHours = [math]::Round(($now - $dates[0]).TotalHours, 2)
        }
        $d = Get-FoldCadenceDecision -OwedCount $owed -UsageLevel $Opts.UsageLevel -LastFoldUtc $lastFold -NowUtc $now -MinFoldIntervalHours $Opts.MinFoldIntervalHours
        $v.dueFold = [bool]$d.DueFold
        $v.reason = $d.Reason
        return $v
    } catch {
        $v.dueFold = $false
        $v.reason = 'error'
        return $v
    } finally {
        foreach ($f in @($tmpOut, $tmpErr)) { if ($f) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue } }
        $v.elapsedSec = [math]::Round($sw.Elapsed.TotalSeconds, 2)
    }
}

# Dot-sourced (by a test or the beat): functions only.
if ($MyInvocation.InvocationName -eq '.') { return }

$verdict = $null
try {
    $verdict = Invoke-FoldCadence ([pscustomobject]@{
            LastSeenPath = $LastSeenPath; DeadlineSec = $DeadlineSec; UsageLevel = $UsageLevel
            MinFoldIntervalHours = $MinFoldIntervalHours; FoldRunsRoot = $FoldRunsRoot; LastFoldUtc = $LastFoldUtc
            Bus = $Bus; RecallScript = $RecallScript; PythonCommand = $PythonCommand; NowUtc = $NowUtc
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
