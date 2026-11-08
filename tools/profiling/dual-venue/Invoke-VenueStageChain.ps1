# Invoke-VenueStageChain.ps1 -- the TRACKED stage chain a venue driver calls to stage ONE sha before its legs (STAGE-BUILD-HEAVY-SLOT-TIMEOUT-LOSES-TURN-1 r3).
# ASCII only.
#
# A venue driver used to call a lane-local stage-chain.ps1 (a copy of a copy, never committed). Call this instead, with the same arguments plus the
# path of the lane's stage-one script:
#   & pwsh -NoProfile -NonInteractive -File <repo>\tools\profiling\dual-venue\Invoke-VenueStageChain.ps1 -Sha <40> -DeadlineUtc <ISO-8601> -StageOne <run>\stage-one.ps1
# The stage-one script stays the lane's own (it names the venue, the worktree and the build root). It is run once per phase (dll, then stage) as
#   pwsh -File <StageOne> -Sha <40> -Phase <dll|stage> [-DeadlineUtc <o>]
# and its exit code is read as before: 0 done, 3 capacity (retried every -RetrySec seconds until the deadline), anything else a failure. The status file
# <RunDir>\stage-status-<sha8>.txt (RunDir defaults to the stage-one script's folder) gets the same lines the copies wrote, so a driver still finds
# `sha=<40> outcome=STAGE_OK manifest=<64>` there; each attempt's output is kept in <RunDir>\stage-<sha8>-<phase>-<utc>.out.txt.
#
# THE DEADLINE REACHES THE MACHINE HEAVY SLOT. 2026-11-08 (lane-HFR-NATIVE-60FPS-FULLRES-1-r31): the driver passed -DeadlineUtc to its stage-chain copy, the
# copy called stage-one without it, stage-one called the assembler without it, so tools\coordination\build-guard.ps1 waited its 60 min default, was refused
# (heavy-slot 75), and the copy's retry 300 s later was a NEW ticket, the youngest in a queue deeper than 60 min. Here the chain deadline is
#   - set as MLV_HEAVY_SLOT_DEADLINE_UTC (round-trip UTC) in EACH stage-one child's own environment, so a stage-one copy and an assembler that omit the
#     parameter still hand it to the build guard by inheritance, which bounds the first ticket's --wait by it and prints BUILD_HEAVY_SLOT_WAIT_BOUND. The
#     caller's process environment is never written: another runspace or chain in the same host never sees this chain's deadline (adversary 8eb98036-A2/A3);
#   - also passed as -DeadlineUtc to a stage-one script whose param block declares it (read from the script's syntax tree, never guessed: a fixed param
#     list would not bind an extra argument).
# The deadline is an ISO-8601 instant (yyyy-MM-ddTHH:mm:ss[.f to .fffffff][Z|+hh:mm|-hh:mm]; no zone is UTC). Anything else is STAGE_CHAIN_REFUSED
# reason=invalid-deadline, exit 2, before anything runs. No attempt starts once the deadline has passed (STAGE_FAILED deadline-passed, exit 3).
# FAIL CLOSED: a run dir the chain cannot write is STAGE_CHAIN_REFUSED reason=run-dir-unwritable (exit 2) before any child; a child that cannot be started,
# or a status/log write that fails later, is STAGE_CHAIN_FAILED (exit 1). Success is only ever a child's own exit 0 (adversary 8eb98036-A1).
#
# Exit: 0 staged (STAGE_CHAIN_DONE), 3 capacity until the deadline or -MaxAttempts, 1 a stage-one or chain failure, 2 a bad argument.
param(
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-f]{40}$')][string]$Sha,
    [string]$DeadlineUtc = '',
    [Parameter(Mandatory = $true)][string]$StageOne,
    [string]$RunDir = '',
    [ValidateRange(0, 86400)][int]$RetrySec = 300,
    # Attempts per phase; 0 = until the deadline (the copies' rule).
    [ValidateRange(0, 100000)][int]$MaxAttempts = 0
)
$ErrorActionPreference = 'Continue'

function Refuse([string]$Why) {
    [Console]::Out.WriteLine(('STAGE_CHAIN_REFUSED reason={0} sha={1} utc={2}' -f $Why, $Sha, [DateTime]::UtcNow.ToString('o')))
    exit 2
}

# Strict shape first (TryParseExact alone accepts '00:00:00.Z' through the optional F digits; adversary 8eb98036-A4), then the instant itself.
$text = [string]$DeadlineUtc
$parsed = [DateTimeOffset]::MinValue
$formats = [string[]]@('yyyy-MM-ddTHH:mm:ssK', 'yyyy-MM-ddTHH:mm:ss.FFFFFFFK')
if ($text -cnotmatch '\A\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d{1,7})?(Z|[+-]\d\d:\d\d)?\z' -or
    -not [DateTimeOffset]::TryParseExact($text, $formats, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$parsed)) {
    Refuse ("invalid-deadline DeadlineUtc='{0}' is not an ISO-8601 instant" -f ($text -replace '\s+', ' '))
}
$deadline = $parsed.UtcDateTime
if (-not (Test-Path -LiteralPath $StageOne -PathType Leaf)) { Refuse ("stage-one-missing StageOne='{0}'" -f $StageOne) }
$StageOne = (Resolve-Path -LiteralPath $StageOne).ProviderPath
if (-not $RunDir) { $RunDir = Split-Path -Parent $StageOne }
if (-not (Test-Path -LiteralPath $RunDir -PathType Container)) { Refuse ("run-dir-missing RunDir='{0}'" -f $RunDir) }

$errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($StageOne, [ref]$null, [ref]$errs)
$declares = $false
if ($ast.ParamBlock) { $declares = @($ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'DeadlineUtc' }).Count -gt 0 }

$pwsh = Join-Path $PSHOME $(if ($IsWindows -or $env:OS -eq 'Windows_NT') { 'pwsh.exe' } else { 'pwsh' })
$status = Join-Path $RunDir ('stage-status-' + $Sha.Substring(0, 8) + '.txt')
$utf8 = [Text.UTF8Encoding]::new($false)
# Throws on failure (never a silent non-terminating error): every write the driver relies on either lands or ends the chain.
function Log([string]$t) { [IO.File]::AppendAllText($status, ('{0} {1}' -f [DateTime]::UtcNow.ToString('o'), $t) + [Environment]::NewLine, $utf8) }

function Invoke-StageOne([string]$Phase, [string]$LogPath) {
    # One stage-one child. The deadline is in ITS environment only. Returns its exit code; throws when it cannot be started or its log cannot be kept.
    $psi = [Diagnostics.ProcessStartInfo]::new($pwsh)
    foreach ($x in @('-NoProfile', '-NonInteractive', '-File', $StageOne, '-Sha', $Sha, '-Phase', $Phase)) { [void]$psi.ArgumentList.Add($x) }
    if ($declares) { [void]$psi.ArgumentList.Add('-DeadlineUtc'); [void]$psi.ArgumentList.Add($deadlineText) }
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true
    $psi.Environment['MLV_HEAVY_SLOT_DEADLINE_UTC'] = $deadlineText
    [IO.File]::WriteAllText($LogPath, '', $utf8)   # the log must be writable BEFORE the child runs
    $p = [Diagnostics.Process]::Start($psi)
    try {
        $p.StandardInput.Close()
        $o = $p.StandardOutput.ReadToEndAsync(); $e = $p.StandardError.ReadToEndAsync()
        $p.WaitForExit()
        [IO.File]::WriteAllText($LogPath, $o.GetAwaiter().GetResult() + $e.GetAwaiter().GetResult(), $utf8)
        return $p.ExitCode
    } finally { $p.Dispose() }
}

$deadlineText = $deadline.ToString('o', [Globalization.CultureInfo]::InvariantCulture)
try {
    Log ("STAGE_CHAIN_START sha={0} deadline={1} stageOne={2} heavySlotDeadline=child-env{3}" -f $Sha, $deadlineText, $StageOne, $(if ($declares) { '+param' } else { '' }))
} catch { Refuse ("run-dir-unwritable status='{0}' ({1})" -f $status, ($_.Exception.Message -replace '\s+', ' ')) }

$rc = 0
try {
    foreach ($phase in @('dll', 'stage')) {
        $attempt = 0
        $phaseDone = $false
        while (-not $phaseDone) {
            if ([DateTime]::UtcNow -ge $deadline) { Log "STAGE_FAILED deadline-passed phase=$phase"; $rc = 3; break }
            $attempt++
            $log = Join-Path $RunDir ('stage-' + $Sha.Substring(0, 8) + "-$phase-" + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfff') + '.out.txt')
            $code = Invoke-StageOne $phase $log
            $last = @([IO.File]::ReadAllLines($log) | Where-Object { $_ -match '^STAGE_ONE_RESULT ' }) | Select-Object -Last 1
            Log "phase=$phase attempt=$attempt exit=$code $last"
            if ($code -eq 0) { $phaseDone = $true; continue }
            if ($code -ne 3) { Log "STAGE_FAILED phase=$phase"; $rc = 1; break }
            if ($MaxAttempts -gt 0 -and $attempt -ge $MaxAttempts) { Log "STAGE_FAILED capacity-attempts-exhausted phase=$phase attempts=$attempt"; $rc = 3; break }
            if ([DateTime]::UtcNow.AddSeconds($RetrySec) -ge $deadline) { Log "STAGE_FAILED capacity-wait-exhausted phase=$phase attempts=$attempt"; $rc = 3; break }
            if ($RetrySec -gt 0) { Start-Sleep -Seconds $RetrySec }
        }
        if ($rc -ne 0) { break }
    }
    if ($rc -eq 0) { Log 'STAGE_CHAIN_DONE' }
} catch {
    [Console]::Out.WriteLine(('STAGE_CHAIN_FAILED sha={0} reason={1} utc={2}' -f $Sha, ($_.Exception.Message -replace '\s+', ' '), [DateTime]::UtcNow.ToString('o')))
    try { Log ('STAGE_CHAIN_FAILED reason=' + ($_.Exception.Message -replace '\s+', ' ')) } catch { }
    $rc = 1
}
exit $rc
