# THIRD-PARTY-PRODUCER-LANES-1 r1: the producer-only engines of Invoke-Lane.ps1 (rows `neon` and `kimi`).
#
# Dot-sourced by Invoke-Lane.ps1 and by the tests. ASCII-only by project convention.
#
# WHAT THESE ROWS ARE: trial IMPLEMENTERS, measured against sonnet by the review rounds the unchanged
# sol + opus/fable keys give their PRs. They are NEVER a review key and appear in no seat map the merge
# gate reads (claude = fable/opus/sonnet, codex = sol/luna). Nothing here may widen that.
#
# WHY A SEPARATE FILE: the claude and codex launch paths in Invoke-Lane.ps1 are pinned line-by-line by
# tests, so a third and fourth engine are delegated here from ONE place (see "THIRD-PARTY PRODUCER
# DELEGATION" in Invoke-Lane.ps1) and those paths stay byte-identical. This file defines functions only
# and runs nothing on dot-source.
#
# NEVER here: a credential, an interactive sign-in, --dangerously-skip-permissions, or a write to the
# user-global ~/.gemini/antigravity-cli/settings.json. agy takes a per-session --settings file instead
# (agy 1.5.17 --help: "Settings for this session only, never saved: a JSON object of settings.json keys,
# inline or as a file path"), so the grant is scoped to the lane worktree and sits in the run dir.

$THIRD_PARTY_PRODUCER_ENGINES = @('antigravity', 'kimicode')
# CreateProcess caps a command line at 32767 UTF-16 units; the prompt is ONE argument, so bound it with headroom.
$THIRD_PARTY_MAX_PROMPT_CHARS = 28000
# agy exits 0 on a soft-deny and prints a notice on stderr that "names the tool and explains how to allow it"
# (docs/cli/headless). The exact text is not published, so the notice is recognised STRUCTURALLY: a line that
# names a tool-ish word AND a deny-ish word. Over-matching fails closed (refused, never complete).
$AGY_SOFT_DENY_DENY_RE = '(?i)\b(denied|blocked|not\s+(?:allowed|approved|permitted)|requires?\s+(?:approval|permission)|needs?\s+(?:approval|permission)|permission\s+(?:denied|required)|disallowed|auto-?denied)\b'
$AGY_SOFT_DENY_TOOL_RE = '(?i)\b(tool|command|write_file|read_file|read_url|edit_file|action|permission|permissions\.allow)\b'

function Test-ThirdPartyProducerEngine([string]$Engine) {
    return ($THIRD_PARTY_PRODUCER_ENGINES -contains $Engine)
}

# Absolute launcher paths. Neither CLI is guaranteed on PATH, and a bare name resolves differently per shell.
function Resolve-ThirdPartyProducerExe([string]$Engine) {
    $path = switch ($Engine) {
        'antigravity' { Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe' }
        'kimicode'    { Join-Path $env:USERPROFILE '.kimi-code\bin\kimi.exe' }
        default       { throw "producer-engine-unknown: $Engine" }
    }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "producer-exe-missing: $path" }
    return [string]$path
}

# The prompt travels as ONE argv element (ProcessStartInfo.ArgumentList quotes it). A bare multi-word argv made
# agy exit 2 "unexpected argument"; plain-text stdin for `-p` is not documented, so it is not relied on.
# A leading '-' would read as a flag, so it is shielded by a leading newline.
function Get-ThirdPartyProducerPromptArg([string]$Prompt) {
    if ([string]::IsNullOrWhiteSpace($Prompt)) { throw 'producer-prompt-empty: nothing to send' }
    if ($Prompt.Contains([string][char]0)) { throw 'producer-prompt-nul: the prompt contains a NUL character' }
    if ($Prompt.Length -gt $THIRD_PARTY_MAX_PROMPT_CHARS) {
        throw "producer-prompt-too-long: $($Prompt.Length) characters exceeds $THIRD_PARTY_MAX_PROMPT_CHARS (the prompt is one command-line argument)"
    }
    if ($Prompt.StartsWith('-')) { return ("`n" + $Prompt) }
    return $Prompt
}

# The producer tool set granted to agy: file writes inside the lane worktree, git, pwsh (which also runs the
# build guard). Both slash styles of the worktree are listed because the target syntax for a Windows path is not
# published and "The CLI ignores rules it can't parse" (a rule that does not parse can only cause a soft-deny,
# which the receipt then books as refused -- it can never widen anything).
function Get-AgyProducerAllowRules([string]$WorkDir) {
    $fwd = (($WorkDir -replace '\\', '/').TrimEnd('/')) + '/'
    $back = (($WorkDir -replace '/', '\').TrimEnd('\')) + '\'
    return @('command(git)', 'command(pwsh)', "write_file($fwd)", "write_file($back)")
}

function New-AgyProducerSettings {
    param([Parameter(Mandatory = $true)][string]$WorkDir, [Parameter(Mandatory = $true)][string]$Path)
    $allow = @(Get-AgyProducerAllowRules -WorkDir $WorkDir)
    $json = ([ordered]@{ permissions = [ordered]@{ allow = $allow } } | ConvertTo-Json -Depth 4)
    [System.IO.File]::WriteAllText($Path, $json, [System.Text.UTF8Encoding]::new($false))
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { $hash = ([BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($json))) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
    return [ordered]@{ path = $Path; allow = $allow; sha256 = $hash; scope = 'per-session --settings file in the run dir; ~/.gemini/antigravity-cli/settings.json is never written' }
}

function Get-ThirdPartyProducerArgv {
    param(
        [Parameter(Mandatory = $true)][string]$Engine,
        [Parameter(Mandatory = $true)][string]$Model,
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][int]$TimeoutSec,
        [string]$SettingsPath = ''
    )
    if ([string]::IsNullOrWhiteSpace($Model)) { throw 'producer-model-unpinned: a producer lane always pins its model' }
    $promptArg = Get-ThirdPartyProducerPromptArg -Prompt $Prompt
    switch ($Engine) {
        'antigravity' {
            # The effort lives in the model id (gemini-4-neon-high); the owner rule is high, never xhigh/max.
            if ($Model -notmatch '-high$') { throw "producer-model-not-high: '$Model' is not a -high model id (owner effort rule)" }
            if ([string]::IsNullOrWhiteSpace($SettingsPath)) { throw 'producer-settings-missing: agy needs the per-session --settings file' }
            if ($TimeoutSec -lt 1) { throw 'producer-timeout-invalid' }
            return @('-p', $promptArg, '--model', $Model, '--output-format', 'json', '--print-timeout', ('{0}s' -f $TimeoutSec), '--settings', $SettingsPath)
        }
        'kimicode' {
            # ALWAYS pin -m: the CLI default is "K2.8 Preview" at effort max. kimi 2.8.0 has no effort flag (its
            # --help lists none), so effort is the pinned model's default (high, measured 2026-11-25) and the
            # receipt says so. -p runs in auto permission and cannot combine with --yolo/--auto; the cwd is the
            # lane worktree and nothing else (no --add-dir).
            return @('-p', $promptArg, '-m', $Model, '--output-format', 'stream-json')
        }
        default { throw "producer-engine-unknown: $Engine" }
    }
}

function Get-ProducerJsonProp($Object, [string]$Name) {
    if ($null -eq $Object) { return $null }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -eq $p) { return $null }
    return $p.Value
}

# One JSON object from agy's stdout: the whole text, else the last line that starts with '{'.
function ConvertFrom-ProducerJsonObject([string]$Text) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $candidates = @($Text.Trim())
    $lines = @($Text -split "`r?`n" | Where-Object { $_.TrimStart().StartsWith('{') })
    if ($lines.Count -gt 0) { $candidates += $lines[$lines.Count - 1] }
    foreach ($c in $candidates) {
        try {
            $o = $c | ConvertFrom-Json -ErrorAction Stop
            if ($null -ne $o -and $o -isnot [string] -and $o -isnot [array]) { return $o }
        } catch { }
    }
    return $null
}

function Get-AgySoftDenyLine([string]$StderrText) {
    if ([string]::IsNullOrWhiteSpace($StderrText)) { return $null }
    foreach ($line in ($StderrText -split "`r?`n")) {
        if ($line -match $AGY_SOFT_DENY_DENY_RE -and $line -match $AGY_SOFT_DENY_TOOL_RE) {
            $t = $line.Trim()
            return $(if ($t.Length -gt 300) { $t.Substring(0, 300) } else { $t })
        }
    }
    return $null
}

# kimi 2.8.0 stream-json is NDJSON, one object per line, measured 2026-11-25 (the live smoke is the fixture in the
# tests): {"role":"meta","type":"system.version",...}, {"role":"assistant","tool_calls":[...]},
# {"role":"tool","tool_call_id":...,"content":...}, {"role":"assistant","content":"<answer>"} and a final
# {"role":"meta","type":"session.resume_hint","content":"To resume this session: ..."}. The ANSWER is the last
# assistant event with string content; meta and tool events are never the answer (the resume hint comes last and
# carries a `content` too). No such event falls back to the raw stdout.
function Get-KimiStreamAnswer($Events, [string]$RawStdout) {
    $answer = ''
    foreach ($e in $Events) {
        $role = Get-ProducerJsonProp $e 'role'
        if ($role -ne 'assistant') { continue }
        $v = Get-ProducerJsonProp $e 'content'
        if ($v -is [string] -and -not [string]::IsNullOrWhiteSpace($v)) { $answer = $v }
    }
    if ([string]::IsNullOrWhiteSpace($answer)) { return $RawStdout }
    return $answer
}

# Classify one finished producer run into the receipt taxonomy. PURE: no process, no file. The rules are
# deliberately one-sided: anything not positively SUCCESS is not complete.
#   complete         exit 0 and positive evidence (agy: status SUCCESS + a response; kimi: a clean stream)
#   refused          the provider/CLI declined to do the work (agy tool soft-deny; kimi exit 4 session held)
#   failed           typed, fail closed (unknown model, unauthenticated, no model configured)
#   ended-incomplete everything else (timeout, other exit, unparseable output, status other than SUCCESS)
function Get-ThirdPartyProducerOutcome {
    param(
        [Parameter(Mandatory = $true)][string]$Engine,
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [bool]$TimedOut = $false,
        [string]$Stdout = '',
        [string]$Stderr = ''
    )
    $o = [ordered]@{
        state = 'ended-incomplete'; complete = $false; failure = $null; reason = $null
        providerRefusal = $null; final = ''; status = $null; error = $null
        usage = $null; numTurns = $null; durationSeconds = $null; events = $null
    }
    $combined = "$Stdout`n$Stderr"
    switch ($Engine) {
        'antigravity' {
            $j = ConvertFrom-ProducerJsonObject -Text $Stdout
            if ($null -ne $j) {
                $o.status = Get-ProducerJsonProp $j 'status'
                $o.error = Get-ProducerJsonProp $j 'error'
                $o.usage = Get-ProducerJsonProp $j 'usage'
                $o.numTurns = Get-ProducerJsonProp $j 'num_turns'
                $o.durationSeconds = Get-ProducerJsonProp $j 'duration_seconds'
                $resp = Get-ProducerJsonProp $j 'response'
                if ($resp -is [string]) { $o.final = $resp }
            }
            if ($TimedOut) { $o.reason = 'timed-out'; return $o }
            $errText = "$($o.error)`n$Stderr"
            if ($ExitCode -ne 0) {
                if ($ExitCode -eq 1 -and ($errText -match '(?i)(unknown|invalid|unsupported|unrecogni[sz]ed|no\s+such|not\s+found)[^\r\n]{0,60}model|model[^\r\n]{0,60}(unknown|invalid|unsupported|not\s+found|not\s+available|does\s+not\s+exist)')) {
                    $o.state = 'failed'; $o.failure = 'producer-model-unknown'; $o.reason = 'producer-model-unknown'; return $o
                }
                if ($errText -match '(?i)authenticat|unauthori[sz]ed|sign[\s-]?in\b|log[\s-]?in\b|credential') {
                    $o.state = 'failed'; $o.failure = 'producer-auth-required'; $o.reason = 'producer-auth-required'; return $o
                }
            }
            $deny = Get-AgySoftDenyLine -StderrText $Stderr
            if ($null -ne $deny) {
                $o.state = 'refused'; $o.reason = 'tool-soft-deny'
                $o.providerRefusal = [ordered]@{
                    kind = 'tool-soft-deny'; engine = 'antigravity'; match = $deny; retryAfter = $null
                    remedy = 'agy exited as if it had worked but a tool was soft-denied; widen permissions.allow in the lane --settings file (producer tool set only) and re-dispatch. A soft-denied run is never complete.'
                }
                return $o
            }
            if ($ExitCode -ne 0) { $o.reason = "agy-exit-$ExitCode"; return $o }
            if ($null -eq $j) { $o.reason = 'json-unparseable'; return $o }
            if ([string]$o.status -ne 'SUCCESS') {
                $o.reason = if ($null -eq $o.status) { 'status-missing' } else { "status-$($o.status)" }
                return $o
            }
            if ([string]::IsNullOrWhiteSpace($o.final)) { $o.reason = 'empty-response'; return $o }
            $o.state = 'complete'; $o.complete = $true; $o.reason = 'status-SUCCESS'
            return $o
        }
        'kimicode' {
            $events = @()
            foreach ($line in ($Stdout -split "`r?`n")) {
                $t = $line.Trim()
                if (-not $t.StartsWith('{')) { continue }
                try { $e = $t | ConvertFrom-Json -ErrorAction Stop; if ($null -ne $e) { $events += , $e } } catch { }
            }
            $o.events = $events.Count
            $o.final = Get-KimiStreamAnswer -Events $events -RawStdout $Stdout
            if ($TimedOut) { $o.reason = 'timed-out'; return $o }
            if ($ExitCode -eq 4) {
                $o.state = 'refused'; $o.reason = 'session-held'
                $o.providerRefusal = [ordered]@{
                    kind = 'session-held'; engine = 'kimicode'; match = 'kimi exit 4'; retryAfter = $null
                    remedy = 'another kimi session holds the working directory; end it or use a fresh lane worktree, then re-dispatch.'
                }
                return $o
            }
            if ($ExitCode -ne 0) {
                if ($combined -match '(?i)no\s+model\s+configured') {
                    $o.state = 'failed'; $o.failure = 'producer-model-unconfigured'; $o.reason = 'producer-model-unconfigured'; return $o
                }
                $o.reason = "kimi-exit-$ExitCode"; return $o
            }
            if ($events.Count -eq 0) { $o.reason = 'no-stream-json-events'; return $o }
            foreach ($e in $events) {
                $type = Get-ProducerJsonProp $e 'type'
                $err = Get-ProducerJsonProp $e 'error'
                $isErr = Get-ProducerJsonProp $e 'is_error'
                if (($type -is [string] -and $type -match '(?i)^error$') -or ($null -ne $err -and "$err" -ne '') -or $isErr -eq $true) {
                    $o.error = if ($null -ne $err) { "$err" } else { 'error event' }
                    $o.reason = 'stream-error-event'; return $o
                }
            }
            $o.state = 'complete'; $o.complete = $true; $o.reason = 'clean-stream-exit-0'
            return $o
        }
        default { throw "producer-engine-unknown: $Engine" }
    }
}

function ConvertFrom-ProducerBuffer($Buffer) {
    try { return [System.Text.UTF8Encoding]::new($false).GetString($Buffer.ToArray()) } catch { return '' }
}

# Run one producer child, bounded. The tree is killed at the deadline; a descendant that keeps a pipe open
# after the child exits cannot hold the lane past a bounded drain. -KillGraceSec lets a CLI with its own
# timeout flag (agy --print-timeout) end first and report it, before this watchdog fires.
function Invoke-ThirdPartyProducerProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Argv,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][int]$TimeoutSec,
        [int]$KillGraceSec = 0,
        [hashtable]$EnvSet = @{},
        [string[]]$EnvRemove = @(),
        [int]$DrainGraceMs = 10000
    )
    $psi = [System.Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $Exe
    foreach ($a in $Argv) { [void]$psi.ArgumentList.Add([string]$a) }
    $psi.WorkingDirectory = $WorkDir
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    foreach ($n in $EnvRemove) { [void]$psi.Environment.Remove($n) }
    foreach ($k in $EnvSet.Keys) { $psi.Environment[[string]$k] = [string]$EnvSet[$k] }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $proc = [System.Diagnostics.Process]::Start($psi)
    $procId = $proc.Id
    $outStream = $proc.StandardOutput.BaseStream
    $errStream = $proc.StandardError.BaseStream
    $outBuf = [System.IO.MemoryStream]::new()
    $errBuf = [System.IO.MemoryStream]::new()
    $outTask = $outStream.CopyToAsync($outBuf)
    $errTask = $errStream.CopyToAsync($errBuf)
    try { $proc.StandardInput.Close() } catch { }
    $limitMs = [math]::Min(2147483647.0, ([double]$TimeoutSec + [double]$KillGraceSec) * 1000.0)
    $timedOut = $false
    $killOutcome = $null
    if (-not $proc.WaitForExit([int]$limitMs)) {
        $timedOut = $true
        try {
            $proc.Kill($true)
            $killOutcome = if ($proc.WaitForExit(15000)) { 'killed-tree' } else { 'kill-wait-timeout' }
        } catch { $killOutcome = 'kill-threw: ' + $_.Exception.Message }
    }
    $exitCode = if ($timedOut) { -1 } else { $proc.ExitCode }
    $both = [System.Threading.Tasks.Task[]]@($outTask, $errTask)
    try { [void][System.Threading.Tasks.Task]::WaitAll($both, $DrainGraceMs) } catch { }
    $drainTimedOut = (-not $outTask.IsCompleted) -or (-not $errTask.IsCompleted)
    if ($drainTimedOut) {
        foreach ($s in @($outStream, $errStream)) { try { $s.Dispose() } catch { } }
        try { [void][System.Threading.Tasks.Task]::WaitAll($both, 2000) } catch { }
    }
    $sw.Stop()
    return [pscustomobject]@{
        pid = $procId; exitCode = $exitCode; timedOut = $timedOut; killOutcome = $killOutcome
        stdout = (ConvertFrom-ProducerBuffer $outBuf)
        stderr = (ConvertFrom-ProducerBuffer $errBuf)
        drainTimedOut = $drainTimedOut; elapsedSec = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    }
}

function Get-ProducerSha256([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes([string]$Text))) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
}

# The whole producer lane: reserve the receipt slot, write the ledger row, build argv, run, classify, write the
# receipt. Returns { Receipt; ExitCode }. Called from Invoke-Lane.ps1 after every shared gate (hub-host
# local-playback lint, -AllowEdits allowlist + worktree hook receipt, run dir) has already run.
# -LedgerWriter / -SnapshotDirty / -ReadHead are scriptblocks so this file stays standalone for the tests.
function Invoke-ThirdPartyProducerLane {
    param(
        [Parameter(Mandatory = $true)][string]$Lane,
        [Parameter(Mandatory = $true)]$Cfg,
        [Parameter(Mandatory = $true)][string]$Prompt,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][string]$RunDir,
        [Parameter(Mandatory = $true)][int]$TimeoutSec,
        [bool]$AllowEdits = $false,
        [string]$AllowedTools = '',
        [string]$Card = '',
        [string]$LanePromptPath = '',
        [string]$DispatchReservationId = '',
        [string]$BaseSha = '',
        [string]$HookSha256 = '',
        [string]$ModelOverride = '',
        [scriptblock]$LedgerWriter = $null,
        [scriptblock]$SnapshotDirty = $null,
        [scriptblock]$ReadHead = $null,
        # Test seam only: a fixture executable replaces the real CLI. NEVER set by Invoke-Lane.ps1.
        [string]$ExeForTest = ''
    )
    # Reserve the slot atomically (same scheme as the claude/codex path: CreateNew decides the winner).
    $base = $null
    for ($n = 1; $n -le 999; $n++) {
        $candidate = Join-Path $RunDir ("{0}-{1:d3}" -f $Lane, $n)
        try {
            $fs = [System.IO.File]::Open("$candidate.receipt.json", [System.IO.FileMode]::CreateNew, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
            $fs.Dispose(); $base = $candidate; break
        } catch [System.IO.IOException] { continue }
    }
    if ($null -eq $base) { throw "Could not reserve a receipt slot in $RunDir after 999 attempts." }
    $rcptPath = "$base.receipt.json"; $promptPath = "$base.prompt.txt"; $outPath = "$base.out.txt"
    $errPath = "$base.err.txt"; $lastPath = "$base.last.txt"; $settingsPath = "$base.agy-settings.json"
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($rcptPath, (([ordered]@{
        schema = 'mlv-app/fleet-lane-receipt/v1'; state = 'reserved'; complete = $false; lane = $Lane; engine = [string]$Cfg.engine; card = $Card
        reservedUtc = (Get-Date).ToUniversalTime().ToString('o')
    }) | ConvertTo-Json -Depth 4), $utf8)

    $startedUtc = (Get-Date).ToUniversalTime()
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $engine = [string]$Cfg.engine
    $model = if ($ModelOverride) { $ModelOverride } else { [string]$Cfg.model }
    $failure = $null; $exitCode = -999; $timedOut = $false; $run = $null; $outcome = $null
    $stdout = ''; $stderrText = ''; $final = ''
    $authority = [ordered]@{ permissionMode = 'unset'; allowedTools = 'unset'; sandbox = 'unset'; writableRoot = $null }
    $settingsInfo = $null; $scratchDir = $null; $scratchDisposition = $null; $exeInfo = $null
    $dispatchState = $null; $dispatchId = $null; $argvRecord = $null
    $dirtyCheck = 'not-applicable'; $dirtyCheckReason = $null; $initialDirty = $null; $initialDirtyReason = $null
    $effortNote = $null
    try {
        # Owner rule: every lane runs at high effort. The table row is high; an override is refused here too.
        if ([string]$Cfg.effort -ne 'high') { throw "lane-effort-must-be-high: a producer lane may only run at 'high' effort (owner ruling 2026-09-26); refusing effort '$($Cfg.effort)' for lane '$Lane'." }
        $dispatchState = if ($DispatchReservationId) { 'linked' } else { 'reserved' }
        $dispatchId = if ($DispatchReservationId) { $DispatchReservationId } else { [guid]::NewGuid().ToString() }
        if ($null -ne $LedgerWriter) {
            try {
                & $LedgerWriter ([ordered]@{
                    schemaVersion = 2; venue = 'invoke-lane'; reservationId = $dispatchId; state = $dispatchState; card = $Card
                    lane = $Lane; allowEdits = [bool]$AllowEdits; runDir = $RunDir; receiptPath = $rcptPath
                    recordedUtc = (Get-Date).ToUniversalTime().ToString('o')
                })
            } catch { throw "dispatch-ledger-write-failed: $($_.Exception.Message)" }
        }
        [System.IO.File]::WriteAllText($promptPath, $Prompt, $utf8)
        $exe = if ($ExeForTest) { $ExeForTest } else { Resolve-ThirdPartyProducerExe -Engine $engine }
        $exeInfo = [ordered]@{ path = $exe; version = $null }
        try { $exeInfo.version = (Get-Item -LiteralPath $exe).VersionInfo.ProductVersion } catch { }
        if ($engine -eq 'antigravity') {
            $settingsInfo = New-AgyProducerSettings -WorkDir $WorkDir -Path $settingsPath
            $authority = [ordered]@{
                permissionMode = "agy -p (auto when agentMode is unset, per docs/cli/headless); deny rules apply in every mode"
                allowedTools = ($settingsInfo.allow -join ', '); sandbox = 'none (no --sandbox flag; scoped by permissions.allow)'
                writableRoot = if ($AllowEdits) { $WorkDir } else { $null }; settings = $settingsInfo
                callerAllowedToolsIgnored = $AllowedTools
            }
        } else {
            $authority = [ordered]@{
                permissionMode = 'kimi -p = auto permission (cannot combine with --yolo/--auto); confined by cwd = the lane worktree, no --add-dir'
                allowedTools = 'kimi auto permission (no allowlist flag)'; sandbox = 'none'
                writableRoot = if ($AllowEdits) { $WorkDir } else { $null }; callerAllowedToolsIgnored = $AllowedTools
            }
            $effortNote = 'kimi 2.8.0 has no effort flag; effort is the pinned model default (high, measured 2026-11-25)'
        }
        $argv = Get-ThirdPartyProducerArgv -Engine $engine -Model $model -Prompt $Prompt -TimeoutSec $TimeoutSec -SettingsPath $(if ($settingsInfo) { $settingsInfo.path } else { '' })
        $argvRecord = @($argv | ForEach-Object { if ($_ -eq $argv[1]) { '<prompt: see promptPath>' } else { $_ } })
        if ($AllowEdits -and $null -ne $SnapshotDirty -and $BaseSha) {
            $pre = & $SnapshotDirty
            if ($pre.ok) { $initialDirty = $pre.map } else { $initialDirtyReason = $pre.reason }
        }
        # Per-run scratch under the worktree's git-ignored .claude-state (short leaf: git fails past MAX_PATH).
        $leaf = (Get-ProducerSha256 (("{0}-{1}" -f (Split-Path $RunDir -Leaf), (Split-Path $base -Leaf)).ToLowerInvariant())).Substring(0, 8)
        $scratchDir = Join-Path (Join-Path $WorkDir '.claude-state\ls') $leaf
        New-Item -ItemType Directory -Force -Path $scratchDir | Out-Null
        $envSet = @{
            TEMP = $scratchDir; TMP = $scratchDir; TMPDIR = $scratchDir
            MLV_LANE_RUN_DIR = $RunDir; MLV_LANE_SCRATCH_MANIFEST = (Join-Path $RunDir 'scratch-paths.txt')
            GIT_CEILING_DIRECTORIES = (Join-Path $WorkDir '.claude-state\ls')
        }
        if ($LanePromptPath) { $envSet['MLV_LANE_PROMPT'] = $LanePromptPath }
        $envRemove = @('MLV_DISPATCH_RESERVATION_ID'); if (-not $LanePromptPath) { $envRemove += 'MLV_LANE_PROMPT' }
        $grace = if ($engine -eq 'antigravity') { 15 } else { 0 }
        $run = Invoke-ThirdPartyProducerProcess -Exe $exe -Argv $argv -WorkDir $WorkDir -TimeoutSec $TimeoutSec -KillGraceSec $grace -EnvSet $envSet -EnvRemove $envRemove
        $exitCode = [int]$run.exitCode; $timedOut = [bool]$run.timedOut
        $stdout = [string]$run.stdout; $stderrText = [string]$run.stderr
        $outcome = Get-ThirdPartyProducerOutcome -Engine $engine -ExitCode $exitCode -TimedOut $timedOut -Stdout $stdout -Stderr $stderrText
        $final = [string]$outcome.final
        if ($outcome.failure) { $failure = [string]$outcome.failure }
    } catch {
        $failure = $_.Exception.Message
    } finally {
        $sw.Stop()
        if ($scratchDir) {
            try {
                $sb = [int64]0
                if (Test-Path -LiteralPath $scratchDir) { Get-ChildItem -LiteralPath $scratchDir -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object { $sb += $_.Length } }
                Remove-Item -LiteralPath $scratchDir -Recurse -Force -ErrorAction Stop
                $scratchDisposition = [ordered]@{ path = $scratchDir; bytes = $sb; removed = $true; error = $null }
            } catch { $scratchDisposition = [ordered]@{ path = $scratchDir; bytes = $null; removed = $false; error = $_.Exception.Message } }
        }
    }
    # Work evidence: an implementer that edited tracked files and committed nothing has not finished (same rule as the
    # claude path's dirty-worktree-no-commit). Only for -AllowEdits, and only when git could be read both times.
    $complete = ($null -eq $failure -and $null -ne $outcome -and $outcome.complete -eq $true)
    if ($AllowEdits -and $complete) {
        if (-not $BaseSha -or $null -eq $SnapshotDirty -or $null -eq $ReadHead) { $dirtyCheck = 'not-applicable'; $dirtyCheckReason = 'no base sha or no git readers supplied' }
        elseif ($initialDirtyReason) { $dirtyCheck = 'unavailable'; $dirtyCheckReason = "pre-launch snapshot: $initialDirtyReason" }
        else {
            try {
                $headAfter = [string](& $ReadHead)
                if ($headAfter.Trim() -ne $BaseSha) { $dirtyCheck = 'not-applicable'; $dirtyCheckReason = 'HEAD moved: a commit landed' }
                else {
                    $post = & $SnapshotDirty
                    if (-not $post.ok) { $dirtyCheck = 'unavailable'; $dirtyCheckReason = "post-exit snapshot: $($post.reason)" }
                    else {
                        $introduced = @($post.map.Keys | Where-Object { -not $initialDirty.Contains($_) -or $initialDirty[$_] -ne $post.map[$_] })
                        if ($introduced.Count -gt 0) {
                            $dirtyCheck = 'dirty'; $dirtyCheckReason = "lane-introduced tracked dirt: $($introduced -join ', ')"
                            $complete = $false; $outcome.complete = $false; $outcome.state = 'ended-incomplete'
                            $outcome.reason = if ($outcome.reason) { "$($outcome.reason) | dirty-worktree-no-commit" } else { 'dirty-worktree-no-commit' }
                        } else { $dirtyCheck = 'clean' }
                    }
                }
            } catch { $dirtyCheck = 'unavailable'; $dirtyCheckReason = "cannot-determine: $($_.Exception.Message)" }
        }
    }
    [System.IO.File]::WriteAllText($outPath, $stdout, $utf8)
    [System.IO.File]::WriteAllText($errPath, $stderrText, $utf8)
    [System.IO.File]::WriteAllText($lastPath, $final, $utf8)
    $providerRefusal = if ($null -ne $outcome) { $outcome.providerRefusal } else { $null }
    $state = if ($null -ne $failure) { 'failed' }
             elseif ($null -ne $providerRefusal) { 'refused' }
             elseif ($complete) { 'complete' }
             elseif ($exitCode -ne -999) { 'ended-incomplete' }
             else { 'incomplete' }
    # resolvedModel is proof, not intent: neither CLI reports a model id, so a clean run records the honest
    # statement that the model was PINNED (an unknown model exits non-zero on agy) but not REPORTED.
    $resolved = if ($complete) { 'pinned, not reported' } else { 'unknown' }
    $usage = if ($null -ne $outcome) { $outcome.usage } else { $null }
    $receipt = [ordered]@{
        schema = 'mlv-app/fleet-lane-receipt/v1'
        state = $state; lane = $Lane; role = [string]$Cfg.role; engine = $engine
        requestedModel = $model; selectedModel = $model; resolvedModel = $resolved
        auxiliaryModels = @(); pinnedByOverride = [bool]$ModelOverride
        effort = [string]$Cfg.effort; effortNote = $effortNote
        card = $Card; workDir = $WorkDir; allowEdits = [bool]$AllowEdits
        allowedTools = [string]$authority.allowedTools
        baseSha = $(if ($BaseSha) { $BaseSha } else { $null }); hookSha256 = $(if ($HookSha256) { $HookSha256 } else { $null })
        authority = $authority
        producerExe = $exeInfo; producerArgv = $argvRecord
        startedUtc = $startedUtc.ToString('o'); endedUtc = (Get-Date).ToUniversalTime().ToString('o')
        durationSec = [math]::Round($sw.Elapsed.TotalSeconds, 1)
        timedOut = $timedOut; timeoutSec = $TimeoutSec; exitCode = $exitCode
        outputDrainTimedOut = $(if ($null -ne $run) { [bool]$run.drainTimedOut } else { $false })
        killOutcome = $(if ($null -ne $run) { $run.killOutcome } else { $null })
        promptSha256 = (Get-ProducerSha256 $Prompt); promptBytes = $utf8.GetByteCount($Prompt)
        outputSha256 = (Get-ProducerSha256 $final); outputBytes = $utf8.GetByteCount($final)
        promptPath = $promptPath; outputPath = $lastPath; stdoutPath = $outPath; stderrPath = $errPath
        failure = $failure
        dispatchLedger = [ordered]@{ state = $dispatchState; reservationId = $dispatchId }
        providerRefusal = $providerRefusal
        containment = [ordered]@{ kind = 'process-tree-kill-at-deadline'; killOutcome = $(if ($null -ne $run) { $run.killOutcome } else { $null }) }
        scratch = $scratchDisposition; bulkScratch = $null
        worktreeDisposition = [ordered]@{ action = 'kept'; reason = 'producer engines never retire a worktree (-RetireWorktree is refused)' }
        worktreeSweep = [ordered]@{ skipped = 'producer-engine' }
        processEnded = ($exitCode -ne -999)
        complete = $complete
        workEvidence = [ordered]@{
            workCompleted = $complete; reason = $(if ($null -ne $outcome) { $outcome.reason } else { $failure })
            status = $(if ($null -ne $outcome) { $outcome.status } else { $null })
            error = $(if ($null -ne $outcome) { $outcome.error } else { $null })
            streamEvents = $(if ($null -ne $outcome) { $outcome.events } else { $null })
        }
        dirtyCheck = $dirtyCheck; dirtyCheckReason = $dirtyCheckReason
        spend = [ordered]@{
            costUsd = $null; costReported = $false
            costUnavailableWhy = $(if ($engine -eq 'antigravity') { 'agy json reports token usage, not USD' } else { 'kimi stream-json carries no usage or cost' })
            numTurns = $(if ($null -ne $outcome) { $outcome.numTurns } else { $null })
            usage = $usage; durationSeconds = $(if ($null -ne $outcome) { $outcome.durationSeconds } else { $null })
        }
    }
    $tmp = "$rcptPath.$([guid]::NewGuid().ToString('N')).tmp"
    [System.IO.File]::WriteAllText($tmp, ($receipt | ConvertTo-Json -Depth 8), $utf8)
    [System.IO.File]::Move($tmp, $rcptPath, $true)
    # Exit code: 125 refused, 124 timed out, 127 never ran; a run that is NOT complete never exits 0.
    $propagated = if ($null -ne $providerRefusal) { 125 }
                  elseif ($timedOut) { 124 }
                  elseif ($exitCode -eq -999) { 127 }
                  elseif ($exitCode -ge 0 -and $exitCode -le 255) { $exitCode }
                  else { 1 }
    if ($propagated -eq 0 -and -not $complete) { $propagated = 1 }
    return [pscustomobject]@{ Receipt = [pscustomobject]$receipt; ExitCode = $propagated }
}
