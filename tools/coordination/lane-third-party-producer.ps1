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
#
# KNOWN LIMIT (stated, not hidden): neither CLI loads the Claude PreToolUse hooks (NA-1..NA-10), so a producer
# lane is governed by the worktree it runs in, the agy permissions.allow file and the launcher's own gates, not
# by mlv-never-authorized.py. That is why -AllowEdits (and its worktree-hook receipt check) is REQUIRED: the
# lane has write authority, so the caller must say so, and a lane without the flag is refused, never run.

$THIRD_PARTY_PRODUCER_ENGINES = @('antigravity', 'kimicode')
# CreateProcess caps a command line at 32767 UTF-16 units; the prompt is ONE argument, so it is bounded twice: by
# characters (here) and by the SERIALIZED command line (Get-ProducerCommandLineLength), because every quote in the
# prompt is escaped when ArgumentList is serialized.
$THIRD_PARTY_MAX_PROMPT_CHARS = 28000
$THIRD_PARTY_MAX_COMMAND_LINE = 32000
# agy exits 0 on a soft-deny and prints a notice on stderr that "names the tool and explains how to allow it"
# (docs/cli/headless). The exact text is not published, so the notice is recognised STRUCTURALLY: a line that
# names a tool-ish word AND a deny-ish word. Over-matching fails closed (refused, never complete).
$AGY_SOFT_DENY_DENY_RE = '(?i)\b(denied|blocked|declined|rejected|refus(?:ed|al|es)|forbidden|prohibited|unauthori[sz]ed|disallowed|auto-?denied|not\s+(?:been\s+)?(?:allowed|approved|permitted|granted|authori[sz]ed)|(?:requires?|needs?|awaiting|pending|lacks?)\b[^\r\n]{0,40}\b(?:approval|permission|authori[sz]ation|consent)|(?:approval|permission|authori[sz]ation)\b[^\r\n]{0,20}\b(?:required|needed|denied)|permission\s+(?:denied|required)|skipp?ed\b[^\r\n]{0,30}\b(?:permission|approval))'
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

# Length of the command line CreateProcess will receive for $Exe + $Argv, serialized the way .NET's ArgumentList does
# (quote an argument holding whitespace or a quote; escape each quote; double the backslashes before a quote or the
# closing quote). A prompt of 20,000 quote characters is 20,000 characters but over 40,000 serialized.
function Get-ProducerCommandLineLength([string]$Exe, [string[]]$Argv) {
    # The program is ALWAYS quoted by CreateProcess's command line ("exe" arg arg), whatever the path holds; arguments
    # are separated by one space, with none after the last.
    $total = ([string]$Exe).Length + 2
    foreach ($a in @($Argv)) {
        $s = [string]$a
        $needsQuote = ($s.Length -eq 0) -or ($s -match '[\s"]')
        if (-not $needsQuote) { $total += $s.Length + 1; continue }
        $len = 2; $backslashes = 0
        foreach ($ch in $s.ToCharArray()) {
            if ($ch -eq [char]'\') { $backslashes++; $len++; continue }
            if ($ch -eq [char]'"') { $len += $backslashes + 2; $backslashes = 0; continue }
            $backslashes = 0; $len++
        }
        $len += $backslashes
        $total += $len + 1
    }
    return $total
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
    return [ordered]@{ path = $Path; allow = $allow; sha256 = $hash; scope = 'per-session --settings file in the run dir; the global agy settings.json is never written' }
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
    if ($TimeoutSec -lt 1) { throw "producer-timeout-invalid: -TimeoutSec $TimeoutSec (a producer lane needs a budget of at least 1 second)" }
    $promptArg = Get-ThirdPartyProducerPromptArg -Prompt $Prompt
    switch ($Engine) {
        'antigravity' {
            # The effort lives in the model id (gemini-4-neon-high); the owner rule is high, never xhigh/max.
            # \z, not $: a trailing newline must not pass as a model id.
            if ($Model -cnotmatch '\A[A-Za-z0-9._-]+-high\z') { throw "producer-model-not-high: '$Model' is not a -high model id (owner effort rule)" }
            if ([string]::IsNullOrWhiteSpace($SettingsPath)) { throw 'producer-settings-missing: agy needs the per-session --settings file' }
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

# ---- strict JSON ----------------------------------------------------------------------------------------------
# ConvertFrom-Json unrolls single-element arrays, turns ISO-looking strings into DateTime and accepts duplicate
# keys, so completion evidence is never read through it. System.Text.Json gives exact kinds: an object becomes an
# ordered dictionary, an array a List[object], a string stays a string. A duplicate key is refused.
function ConvertFrom-ProducerJsonElement($El) {
    switch ($El.ValueKind.ToString()) {
        'Object' {
            # Ordinal keys: an [ordered] dictionary compares string keys case-INSENSITIVELY, so {"STATUS":..} would have
            # answered a lookup of `status`. Wire field names are exact.
            $d = [System.Collections.Generic.Dictionary[string, object]]::new([System.StringComparer]::Ordinal)
            foreach ($p in $El.EnumerateObject()) {
                if ($d.ContainsKey($p.Name)) { throw "duplicate-key: $($p.Name)" }
                $d[$p.Name] = (ConvertFrom-ProducerJsonElement $p.Value)
            }
            return , $d
        }
        'Array' {
            $l = [System.Collections.Generic.List[object]]::new()
            foreach ($i in $El.EnumerateArray()) { $l.Add((ConvertFrom-ProducerJsonElement $i)) }
            return , $l
        }
        'String' { return [string]$El.GetString() }
        'Number' { $n = [long]0; if ($El.TryGetInt64([ref]$n)) { return $n }; return [double]$El.GetDouble() }
        'True'   { return $true }
        'False'  { return $false }
        default  { return $null }
    }
}

# One JSON OBJECT, or $null (not JSON, not an object, a duplicate key, trailing text).
function ConvertFrom-ProducerJsonText([string]$Text) {
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $doc = $null
    try {
        $doc = [System.Text.Json.JsonDocument]::Parse($Text.Trim())
        if ($doc.RootElement.ValueKind.ToString() -ne 'Object') { return $null }
        return , (ConvertFrom-ProducerJsonElement $doc.RootElement)
    } catch { return $null } finally { if ($null -ne $doc) { $doc.Dispose() } }
}

# The value of one key of a parsed object, exactly as parsed ($null when absent). The leading comma keeps a
# one-element array an array instead of letting the pipeline unroll it to its element.
function Get-ProducerJsonProp($Object, [string]$Name) {
    if ($Object -is [System.Collections.Generic.Dictionary[string, object]] -and $Object.ContainsKey($Name)) { return , $Object[$Name] }
    return $null
}

# agy --output-format json prints ONE JSON object. Stdout is that object and nothing else: a rejected array, trailing
# text or a second value is never "repaired" by looking for an object line inside it (that read an array's inner
# element, or an object followed by `null`, as a clean result).
function Get-AgyJsonResult([string]$Text) {
    return , (ConvertFrom-ProducerJsonText -Text $Text)
}

# A notice may wrap, so the tool word and the deny word are looked for in a window of up to 3 consecutive non-blank
# stderr lines, not only on one line.
function Get-AgySoftDenyLine([string]$StderrText) {
    if ([string]::IsNullOrWhiteSpace($StderrText)) { return $null }
    $lines = @($StderrText -split "`r?`n" | Where-Object { $_.Trim() -ne '' })
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $last = [math]::Min($i + 2, $lines.Count - 1)
        $window = (@($lines[$i..$last]) | ForEach-Object { $_.Trim() }) -join ' '
        if ($window -match $AGY_SOFT_DENY_DENY_RE -and $window -match $AGY_SOFT_DENY_TOOL_RE) {
            return $(if ($window.Length -gt 300) { $window.Substring(0, 300) } else { $window })
        }
    }
    return $null
}

# kimi 2.8.0 stream-json is NDJSON, one object per line, measured 2026-11-25 (the live smoke is the fixture in the
# tests): {"role":"meta","type":"system.version",...}, {"role":"assistant","tool_calls":[...]},
# {"role":"tool","tool_call_id":...,"content":...}, {"role":"assistant","content":"<answer>"} and a final
# {"role":"meta","type":"session.resume_hint","content":"To resume this session: ..."}. The ANSWER is the last
# assistant event with string content; meta and tool events are never the answer (the resume hint comes last and
# carries a `content` too). Returns $null when there is no such event.
function Get-KimiStreamAnswer($Events) {
    $answer = $null
    foreach ($e in $Events) {
        $role = Get-ProducerJsonProp $e 'role'
        if ($role -isnot [string] -or $role -cne 'assistant') { continue }
        $v = Get-ProducerJsonProp $e 'content'
        if ($v -is [string] -and -not [string]::IsNullOrWhiteSpace($v)) { $answer = $v }
    }
    return $answer
}

# Classify one finished producer run into the receipt taxonomy. PURE: no process, no file. The rules are
# deliberately one-sided: anything not positively SUCCESS is not complete.
#   complete         exit 0 and positive evidence (agy: a JSON object, status "SUCCESS", a string response; kimi: a
#                    stream of JSON objects only, holding an assistant answer and no error event) read from output
#                    that was drained in full
#   refused          the provider/CLI declined to do the work (agy tool soft-deny; kimi exit 4 session held)
#   failed           typed, fail closed (unknown model, unauthenticated, no model configured)
#   ended-incomplete everything else (timeout, other exit, unparseable output, status other than SUCCESS)
function Get-ThirdPartyProducerOutcome {
    param(
        [Parameter(Mandatory = $true)][string]$Engine,
        [Parameter(Mandatory = $true)][int]$ExitCode,
        [bool]$TimedOut = $false,
        [string]$Stdout = '',
        [string]$Stderr = '',
        # The runner could not read all of the child's output (a descendant held a pipe, or a copy faulted).
        [bool]$OutputIncomplete = $false
    )
    $o = [ordered]@{
        state = 'ended-incomplete'; complete = $false; failure = $null; reason = $null
        providerRefusal = $null; final = ''; status = $null; error = $null
        usage = $null; numTurns = $null; durationSeconds = $null; events = $null
    }
    $combined = "$Stdout`n$Stderr"
    switch ($Engine) {
        'antigravity' {
            $j = Get-AgyJsonResult -Text $Stdout
            if ($null -ne $j) {
                $st = Get-ProducerJsonProp $j 'status'; if ($st -is [string]) { $o.status = $st }
                $er = Get-ProducerJsonProp $j 'error'; if ($er -is [string]) { $o.error = $er }
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
            if ($null -eq $o.status) { $o.reason = 'status-missing'; return $o }
            if ($o.status -cne 'SUCCESS') { $o.reason = "status-$($o.status)"; return $o }
            if ([string]::IsNullOrWhiteSpace($o.final)) { $o.reason = 'empty-response'; return $o }
            if ($OutputIncomplete) { $o.reason = 'output-incomplete'; return $o }
            $o.state = 'complete'; $o.complete = $true; $o.reason = 'status-SUCCESS'
            return $o
        }
        'kimicode' {
            $events = [System.Collections.Generic.List[object]]::new()
            $badLines = 0
            foreach ($line in ($Stdout -split "`r?`n")) {
                $t = $line.Trim()
                if ($t -eq '') { continue }
                $e = ConvertFrom-ProducerJsonText -Text $t
                if ($null -eq $e) { $badLines++ } else { $events.Add($e) }
            }
            $o.events = $events.Count
            $answer = Get-KimiStreamAnswer -Events $events
            $o.final = if ($null -ne $answer) { $answer } else { $Stdout }
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
            if ($badLines -gt 0) { $o.reason = 'unparseable-stream-line'; return $o }
            if ($events.Count -eq 0) { $o.reason = 'no-stream-json-events'; return $o }
            foreach ($e in $events) {
                $type = Get-ProducerJsonProp $e 'type'
                $err = Get-ProducerJsonProp $e 'error'
                $isErr = Get-ProducerJsonProp $e 'is_error'
                $errPresent = ($null -ne $err) -and (($err -isnot [string]) -or ($err -ne ''))
                # `is_error` present and anything but the boolean false (a string "true", 1, ...) is an error indicator.
                $isErrPresent = ($null -ne $isErr) -and -not ($isErr -is [bool] -and -not $isErr)
                if (($type -is [string] -and $type -match '(?i)^error$') -or $errPresent -or $isErrPresent) {
                    $o.error = if ($err -is [string]) { $err } else { 'error event' }
                    $o.reason = 'stream-error-event'; return $o
                }
            }
            if ($null -eq $answer) { $o.reason = 'no-assistant-answer'; return $o }
            if ($OutputIncomplete) { $o.reason = 'output-incomplete'; return $o }
            $o.state = 'complete'; $o.complete = $true; $o.reason = 'clean-stream-exit-0'
            return $o
        }
        default { throw "producer-engine-unknown: $Engine" }
    }
}

function ConvertFrom-ProducerBuffer($Buffer) {
    try { return [System.Text.UTF8Encoding]::new($false).GetString($Buffer.ToArray()) } catch { return '' }
}

# Stop a producer child's process tree and say what was OBSERVED, never what was attempted.
#   'killed'                              the root and every descendant the pre-kill walk saw were each seen to exit
#   'killed-root-descendants-unverified'  the root exited; no walker was available (a standalone test harness), so
#                                         nothing is claimed about descendants
#   'uncertain:<reason>'                  not known to be gone (already-exited | kill-failed | wait-timeout |
#                                         tree-unreadable | any reason the walk itself reports)
# The walker is [MlvLaneJob]::SnapshotDescendants, the same one the direct Codex path uses (Stop-CodexDirectChildTree);
# the runtime's own Kill($true) compares local start times and can miss a descendant across the end of daylight time,
# so every member the walk showed is killed and waited for as well. Never throws.
function Stop-ProducerProcessTree {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Proc, [double]$LimitMs = 15000)
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    try { if ($Proc.HasExited) { return 'uncertain:already-exited' } } catch { return 'uncertain:tree-unreadable' }
    $haveWalker = ($null -ne ('MlvLaneJob' -as [type]))
    $walk = $null
    if ($haveWalker) { try { $walk = [MlvLaneJob]::SnapshotDescendants($Proc) } catch { $walk = $null } }
    try { $Proc.Kill($true) } catch { return 'uncertain:kill-failed' }
    $members = if ($null -ne $walk) { @($walk.Members) } else { @() }
    foreach ($m in $members) {
        try { if (-not $m.HasExited) { $m.Kill() } } catch { return 'uncertain:kill-failed' }
    }
    foreach ($m in (@($Proc) + $members)) {
        $leftMs = [int][math]::Max(0.0, [math]::Floor($LimitMs - $clock.Elapsed.TotalMilliseconds))
        try { if (-not $m.WaitForExit($leftMs)) { return 'uncertain:wait-timeout' } } catch { return 'uncertain:tree-unreadable' }
    }
    if (-not $haveWalker) { return 'killed-root-descendants-unverified' }
    if ($null -eq $walk) { return 'uncertain:tree-unreadable' }
    if ($walk.Uncertain) { return "uncertain:$($walk.Uncertain)" }
    return 'killed'
}

# Run one producer child, bounded. -TimeoutSec is what is LEFT of the lane's budget (fractions allowed). The tree is
# killed at the deadline; a descendant that keeps a pipe open after the child exits cannot hold the lane past a bounded
# drain, and a drain that did not finish (or faulted) is reported as outputIncomplete so it is never read as a complete
# answer. -KillGraceSec lets a CLI with its own timeout flag (agy --print-timeout) end first and report it. Anything
# that throws after the child exists stops the child first.
function Invoke-ThirdPartyProducerProcess {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [Parameter(Mandatory = $true)][string[]]$Argv,
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][double]$TimeoutSec,
        # The lane's own stopwatch (started at reservation). Every wait is what is LEFT of -TimeoutSec on THIS clock,
        # measured again after the child exists, and a child that finishes after the deadline is a timeout whatever it
        # printed. Absent (a standalone call) a clock starts here.
        [System.Diagnostics.Stopwatch]$Clock = $null,
        [hashtable]$EnvSet = @{},
        [string[]]$EnvRemove = @(),
        [ValidateRange(1, 600000)][int]$DrainGraceMs = 10000
    )
    # `-not (x -gt 0)` also refuses NaN, which passes `x -le 0`.
    if (-not ($TimeoutSec -gt 0) -or [double]::IsInfinity($TimeoutSec)) { throw "producer-timeout-invalid: $TimeoutSec seconds; the child is not started" }
    if ($null -eq $Clock) { $Clock = [System.Diagnostics.Stopwatch]::StartNew() }
    $budgetMs = [math]::Min(2147483000.0, $TimeoutSec * 1000.0)
    if ($Clock.Elapsed.TotalMilliseconds -ge $budgetMs) { throw [System.TimeoutException]::new('launch-budget-exhausted') }
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
    $outBuf = [System.IO.MemoryStream]::new()
    $errBuf = [System.IO.MemoryStream]::new()
    $timedOut = $false; $killOutcome = $null; $exitCode = -999; $finishedAfterDeadline = $false
    $outStream = $null; $errStream = $null; $outTask = $null; $errTask = $null
    try {
        $outStream = $proc.StandardOutput.BaseStream
        $errStream = $proc.StandardError.BaseStream
        $outTask = $outStream.CopyToAsync($outBuf)
        $errTask = $errStream.CopyToAsync($errBuf)
        try { $proc.StandardInput.Close() } catch { }
        # What is left NOW, after Process.Start and the pipe setup spent some of it (not the remainder measured before them).
        $leftMs = $budgetMs - $Clock.Elapsed.TotalMilliseconds
        if ($leftMs -le 0 -or -not $proc.WaitForExit([int][math]::Min(2147483647.0, [math]::Max(1.0, [math]::Ceiling($leftMs))))) {
            $timedOut = $true
            $killOutcome = Stop-ProducerProcessTree -Proc $proc -LimitMs 15000
        }
        $exitCode = if ($timedOut) { -1 } else { $proc.ExitCode }
        # A child that exited, but only after the deadline, did not finish inside the lane's budget: its result is not used.
        if (-not $timedOut -and $Clock.Elapsed.TotalMilliseconds -gt $budgetMs) { $timedOut = $true; $exitCode = -1; $finishedAfterDeadline = $true }
    } catch {
        # Anything that threw with a child alive must not leave it running.
        try { $null = Stop-ProducerProcessTree -Proc $proc -LimitMs 5000 } catch { }
        throw
    }
    $both = [System.Threading.Tasks.Task[]]@($outTask, $errTask)
    try { [void][System.Threading.Tasks.Task]::WaitAll($both, $DrainGraceMs) } catch { }
    $drainTimedOut = (-not $outTask.IsCompleted) -or (-not $errTask.IsCompleted)
    if ($drainTimedOut) {
        foreach ($s in @($outStream, $errStream)) { try { $s.Dispose() } catch { } }
        try { [void][System.Threading.Tasks.Task]::WaitAll($both, 2000) } catch { }
    }
    $drainFaulted = $outTask.IsFaulted -or $errTask.IsFaulted -or $outTask.IsCanceled -or $errTask.IsCanceled
    $sw.Stop()
    $rootExited = try { [bool]$proc.HasExited } catch { $false }
    return [pscustomobject]@{
        pid = $procId; exitCode = $exitCode; timedOut = $timedOut; killOutcome = $killOutcome; rootExited = $rootExited
        finishedAfterDeadline = $finishedAfterDeadline
        stdout = (ConvertFrom-ProducerBuffer $outBuf)
        stderr = (ConvertFrom-ProducerBuffer $errBuf)
        drainTimedOut = $drainTimedOut; drainFaulted = $drainFaulted
        outputIncomplete = ($drainTimedOut -or $drainFaulted); elapsedSec = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    }
}

function Get-ProducerSha256([string]$Text) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes([string]$Text))) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
}

# Publish a file by temp + replace. A reader that holds the destination open without delete sharing (a poller, an AV
# scan) makes the replace throw a sharing violation for a moment, so it is retried for at most 2 s (the same bound as
# Write-Utf8NoBomAtomic in Invoke-Lane.ps1) and the temp file is removed on every failure.
function Write-ProducerFileAtomic([string]$Path, [string]$Content) {
    $utf8 = [System.Text.UTF8Encoding]::new($false)
    $tmp = "$Path.$([guid]::NewGuid().ToString('N')).tmp"
    try {
        [System.IO.File]::WriteAllText($tmp, $Content, $utf8)
        $clock = [System.Diagnostics.Stopwatch]::StartNew()
        while ($true) {
            try { [System.IO.File]::Move($tmp, $Path, $true); return } catch [System.IO.IOException] {
                if ($clock.ElapsedMilliseconds -ge 2000) { throw }
                Start-Sleep -Milliseconds 20
            } catch [System.UnauthorizedAccessException] {
                if ($clock.ElapsedMilliseconds -ge 2000) { throw }
                Start-Sleep -Milliseconds 20
            }
        }
    } finally { if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue } }
}

# The whole producer lane: reserve the receipt slot, write the ledger row, build argv, run, classify, write the
# receipt. Returns { Receipt; ExitCode }. Called from Invoke-Lane.ps1 after every shared gate (hub-host
# local-playback lint, -AllowEdits allowlist + worktree hook receipt, run dir) has already run.
# -LedgerWriter / -SnapshotDirty / -ReadHead / -AfterGit / -BulkScratchCleanup are scriptblocks so this file stays
# standalone for the tests.
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
        [string]$BaseShaFailureReason = '',
        [string]$HookSha256 = '',
        # '' = no scratch (the legacy -ScratchRoot ''); unbound = <WorkDir>\.claude-state\ls, like the claude/codex path.
        [string]$ScratchBase = '',
        [bool]$KeepScratch = $false,
        [scriptblock]$LedgerWriter = $null,
        [scriptblock]$SnapshotDirty = $null,
        [scriptblock]$ReadHead = $null,
        # param($baseSha, $head) -> $true only when $head descends from $baseSha (git merge-base --is-ancestor).
        [scriptblock]$GitAncestor = $null,
        [scriptblock]$AfterGit = $null,
        [scriptblock]$BulkScratchCleanup = $null,
        # The run dir's scratch-paths.txt. The cleanup runs only for a manifest THIS lane wrote or appended to: that file
        # is shared by every lane that uses the run dir, and an earlier lane's keep decision (-KeepBulkScratch) must not be
        # overridden by a later lane that registered nothing.
        [string]$BulkManifestPath = '',
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
    $startedUtc = (Get-Date).ToUniversalTime()
    # The marker is cosmetic (the slot file already exists, empty); a reader that holds the slot open must not turn the
    # reservation into a throw with no terminal receipt, so a failed write is skipped and the final publish writes it.
    # `startedUtc` is the same instant as `reservedUtc`: the currency reader keys receipts on it.
    try {
        [System.IO.File]::WriteAllText($rcptPath, (([ordered]@{
            schema = 'mlv-app/fleet-lane-receipt/v1'; state = 'reserved'; complete = $false; lane = $Lane; engine = [string]$Cfg.engine; card = $Card
            reservedUtc = $startedUtc.ToString('o'); startedUtc = $startedUtc.ToString('o')
        }) | ConvertTo-Json -Depth 4), $utf8)
    } catch { }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $engine = [string]$Cfg.engine
    $model = [string]$Cfg.model
    $failure = $null; $exitCode = -999; $timedOut = $false; $run = $null; $outcome = $null
    $stdout = ''; $stderrText = ''; $final = ''
    $authority = [ordered]@{ permissionMode = 'unset'; allowedTools = 'unset'; sandbox = 'unset'; writableRoot = $null }
    $settingsInfo = $null; $scratchDir = $null; $scratchDisposition = $null; $exeInfo = $null; $bulkScratch = $null
    $dispatchState = $null; $dispatchId = $null; $argvRecord = $null; $fsmonitorStop = $null
    $dirtyCheck = 'not-applicable'; $dirtyCheckReason = $null; $initialDirty = $null; $initialDirtyReason = $null
    $effortNote = $null; $artifactErrors = @()
    try {
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
        # AFTER the ledger row, so a receipt that records one of these deliberate pre-provider refusals is never an
        # unreserved launch to the product-ratio coverage reader (which exempts only dispatch-ledger-write-failed).
        # Owner rule: every lane runs at high effort. The table row is high; an override is refused here too.
        if ([string]$Cfg.effort -ne 'high') { throw "lane-effort-must-be-high: a producer lane may only run at 'high' effort (owner ruling 2026-09-26); refusing effort '$($Cfg.effort)' for lane '$Lane'." }
        # The lane has write authority and no Claude hook governs it, so the caller must have asked for it.
        if (-not $AllowEdits) { throw "producer-allow-edits-required: -Lane $Lane is a write-capable producer; run it with -AllowEdits (and its -AllowedTools) or not at all" }
        if ($TimeoutSec -lt 1) { throw "producer-timeout-invalid: -TimeoutSec $TimeoutSec (a producer lane needs a budget of at least 1 second)" }
        [System.IO.File]::WriteAllText($promptPath, $Prompt, $utf8)
        $exe = if ($ExeForTest) { $ExeForTest } else { Resolve-ThirdPartyProducerExe -Engine $engine }
        $exeInfo = [ordered]@{ path = $exe; version = $null }
        try { $exeInfo.version = (Get-Item -LiteralPath $exe).VersionInfo.ProductVersion } catch { }
        if ($engine -eq 'antigravity') {
            $settingsInfo = New-AgyProducerSettings -WorkDir $WorkDir -Path $settingsPath
            $authority = [ordered]@{
                permissionMode = "agy -p (auto when agentMode is unset, per docs/cli/headless); deny rules apply in every mode"
                allowedTools = ($settingsInfo.allow -join ', '); sandbox = 'none (no --sandbox flag; scoped by permissions.allow)'
                writableRoot = $WorkDir; settings = $settingsInfo
                callerAllowedToolsIgnored = $AllowedTools
                hookGovernance = 'none: agy does not load the Claude PreToolUse hooks (NA-1..NA-10 do not govern this lane)'
            }
        } else {
            $authority = [ordered]@{
                permissionMode = 'kimi -p = auto permission (cannot combine with --yolo/--auto); confined by cwd = the lane worktree, no --add-dir'
                allowedTools = 'kimi auto permission (no allowlist flag)'; sandbox = 'none'
                writableRoot = $WorkDir; callerAllowedToolsIgnored = $AllowedTools
                hookGovernance = 'none: kimi does not load the Claude PreToolUse hooks (NA-1..NA-10 do not govern this lane)'
            }
            $effortNote = "kimi 2.8.0 has no effort flag; effort is the default of the pinned model $model (high, measured 2026-11-25)"
        }
        # The lane budget is ONE stopwatch started at reservation: setup (ledger, prompt, settings) spends it, and the
        # child gets what is LEFT, never a fresh budget. Nothing is started once it is gone.
        $remainingSec = $TimeoutSec - $sw.Elapsed.TotalSeconds
        if ($remainingSec -le 0) { throw [System.TimeoutException]::new('launch-budget-exhausted') }
        $argv = Get-ThirdPartyProducerArgv -Engine $engine -Model $model -Prompt $Prompt -TimeoutSec ([int][math]::Ceiling($remainingSec)) -SettingsPath $(if ($settingsInfo) { $settingsInfo.path } else { '' })
        $cmdLen = Get-ProducerCommandLineLength -Exe $exe -Argv $argv
        if ($cmdLen -gt $THIRD_PARTY_MAX_COMMAND_LINE) { throw "producer-command-line-too-long: the serialized command line is $cmdLen characters (limit $THIRD_PARTY_MAX_COMMAND_LINE); every quote in the prompt is escaped" }
        # Only the prompt argument (index 1, after -p) is redacted; every other argument is recorded as launched.
        $argvRecord = @(for ($i = 0; $i -lt $argv.Count; $i++) { if ($i -eq 1) { '<prompt: see promptPath>' } else { $argv[$i] } })
        if ($BaseShaFailureReason) { $initialDirtyReason = $BaseShaFailureReason }
        elseif ($null -ne $SnapshotDirty -and $BaseSha) {
            $pre = & $SnapshotDirty
            if ($pre.ok) { $initialDirty = $pre.map } else { $initialDirtyReason = $pre.reason }
        }
        # Per-run scratch: a UNIQUE directory (a hash of the run dir and slot alone repeated across run dirs, so two
        # runs in one worktree shared a TEMP and the first to finish removed the second's). Honors -ScratchRoot '' (none),
        # -ScratchRoot <dir> and -KeepScratch like the claude/codex path.
        $scratchRootUsed = if ($PSBoundParameters.ContainsKey('ScratchBase')) { $ScratchBase } else { Join-Path $WorkDir '.claude-state\ls' }
        $envSet = @{ MLV_LANE_RUN_DIR = $RunDir; MLV_LANE_SCRATCH_MANIFEST = (Join-Path $RunDir 'scratch-paths.txt') }
        if ($scratchRootUsed) {
            for ($try = 0; $try -lt 8 -and -not $scratchDir; $try++) {
                $leaf = (Get-ProducerSha256 ("{0}|{1}|{2}" -f $RunDir.ToLowerInvariant(), (Split-Path $base -Leaf), [guid]::NewGuid().ToString('N'))).Substring(0, 8)
                $cand = Join-Path $scratchRootUsed $leaf
                if (-not (Test-Path -LiteralPath $cand)) { New-Item -ItemType Directory -Force -Path $cand | Out-Null; $scratchDir = $cand }
            }
            if (-not $scratchDir) { throw 'producer-scratch-unavailable: no unused scratch directory name after 8 tries' }
            foreach ($v in 'TEMP', 'TMP', 'TMPDIR') { $envSet[$v] = $scratchDir }
            $envSet['GIT_CEILING_DIRECTORIES'] = $scratchRootUsed
        }
        if ($LanePromptPath) { $envSet['MLV_LANE_PROMPT'] = $LanePromptPath }
        $envRemove = @('MLV_DISPATCH_RESERVATION_ID'); if (-not $LanePromptPath) { $envRemove += 'MLV_LANE_PROMPT' }
        # No grace beyond the budget: the child gets what is LEFT of the lane's own clock, and the runner re-measures it
        # after the child exists. (agy's --print-timeout is the remaining seconds, rounded up, so it never outlives it.)
        $run = Invoke-ThirdPartyProducerProcess -Exe $exe -Argv $argv -WorkDir $WorkDir -TimeoutSec $TimeoutSec -Clock $sw -EnvSet $envSet -EnvRemove $envRemove
        $exitCode = [int]$run.exitCode; $timedOut = [bool]$run.timedOut
        $stdout = [string]$run.stdout; $stderrText = [string]$run.stderr
        $outcome = Get-ThirdPartyProducerOutcome -Engine $engine -ExitCode $exitCode -TimedOut $timedOut -Stdout $stdout -Stderr $stderrText -OutputIncomplete ([bool]$run.outputIncomplete)
        $final = [string]$outcome.final
        if ($outcome.failure) { $failure = [string]$outcome.failure }
        # A kill that is not known to have worked is a failure, whatever the child's exit code says.
        if ($timedOut -and ([string]$run.killOutcome -like 'uncertain:*' -or [string]$run.killOutcome -like 'kill-threw*')) {
            $failure = "producer-kill-uncertain: $($run.killOutcome) -- the child or a descendant may have outlived this lane"
        }
    } catch [System.TimeoutException] {
        $timedOut = $true; $exitCode = -1; $failure = $null
        if ($null -eq $outcome) { $outcome = [ordered]@{ state = 'ended-incomplete'; complete = $false; failure = $null; reason = $_.Exception.Message; providerRefusal = $null; final = ''; status = $null; error = $null; usage = $null; numTurns = $null; durationSeconds = $null; events = $null } }
    } catch {
        $failure = $_.Exception.Message
    } finally {
        $sw.Stop()
        if ($scratchDir) {
            try {
                $sb = [int64]0
                if (Test-Path -LiteralPath $scratchDir) { Get-ChildItem -LiteralPath $scratchDir -Recurse -File -Force -ErrorAction SilentlyContinue | ForEach-Object { $sb += $_.Length } }
                $removed = $false
                if (-not $KeepScratch -and (Test-Path -LiteralPath $scratchDir)) { Remove-Item -LiteralPath $scratchDir -Recurse -Force -ErrorAction Stop; $removed = $true }
                $scratchDisposition = [ordered]@{ path = $scratchDir; bytes = $sb; removed = $removed; error = $null }
            } catch { $scratchDisposition = [ordered]@{ path = $scratchDir; bytes = $null; removed = $false; error = $_.Exception.Message } }
        }
    }
    # Bulk scratch the lane registered outside the scratch root (the same cleanup the claude/codex path runs). Never throws.
    # Only a manifest written by THIS lane (modified since it started) is cleaned: the file is shared per run dir.
    if ($null -ne $BulkScratchCleanup) {
        try {
            $ownManifest = $true
            if ($BulkManifestPath) {
                $ownManifest = (Test-Path -LiteralPath $BulkManifestPath -PathType Leaf) -and ((Get-Item -LiteralPath $BulkManifestPath).LastWriteTimeUtc -ge $startedUtc)
            }
            $bulkScratch = if ($ownManifest) { & $BulkScratchCleanup } else { [ordered]@{ entries = @(); error = $null; skipped = 'no manifest written by this lane (an earlier lane in the same run dir owns scratch-paths.txt)' } }
        } catch { $bulkScratch = [ordered]@{ entries = @(); error = "cannot-determine: $($_.Exception.Message)" } }
    }
    # Work evidence: an implementer that edited tracked files and committed nothing has not finished (same rule as the
    # claude path's dirty-worktree-no-commit). Only when git could be read both times.
    $complete = ($null -eq $failure -and $null -ne $outcome -and $outcome.complete -eq $true)
    if ($AllowEdits -and $complete) {
        if ($initialDirtyReason) { $dirtyCheck = 'unavailable'; $dirtyCheckReason = "pre-launch snapshot: $initialDirtyReason" }
        elseif (-not $BaseSha -or $null -eq $SnapshotDirty -or $null -eq $ReadHead) { $dirtyCheck = 'not-applicable'; $dirtyCheckReason = 'no base sha or no git readers supplied' }
        else {
            try {
                # An implementer is judged on what it LEFT: tracked dirt it introduced fails the lane whether or not HEAD
                # moved (a producer that commits part of its work and leaves the rest edited has not finished), and a HEAD
                # that is not a descendant of the base (a checkout of an existing commit) is not "a commit landed".
                $headAfter = ([string](& $ReadHead)).Trim()
                $headProblem = $null
                if ($headAfter -ne $BaseSha) {
                    $descends = if ($null -ne $GitAncestor) { [bool](& $GitAncestor $BaseSha $headAfter) } else { $false }
                    if (-not $descends) { $headProblem = "HEAD $headAfter is not a descendant of the base $BaseSha" }
                }
                $post = & $SnapshotDirty
                if (-not $post.ok) { $dirtyCheck = 'unavailable'; $dirtyCheckReason = "post-exit snapshot: $($post.reason)" }
                else {
                    $introduced = @($post.map.Keys | Where-Object { -not $initialDirty.Contains($_) -or $initialDirty[$_] -ne $post.map[$_] })
                    if ($introduced.Count -gt 0) {
                        $dirtyCheck = 'dirty'; $dirtyCheckReason = "lane-introduced tracked dirt: $($introduced -join ', ')"
                        $complete = $false; $outcome.complete = $false; $outcome.state = 'ended-incomplete'
                        $outcome.reason = if ($outcome.reason) { "$($outcome.reason) | dirty-worktree-no-commit" } else { 'dirty-worktree-no-commit' }
                    } elseif ($headProblem) {
                        $dirtyCheck = 'head-not-descended'; $dirtyCheckReason = $headProblem
                        $complete = $false; $outcome.complete = $false; $outcome.state = 'ended-incomplete'
                        $outcome.reason = if ($outcome.reason) { "$($outcome.reason) | head-not-descended" } else { 'head-not-descended' }
                    } else { $dirtyCheck = 'clean' }
                }
            } catch { $dirtyCheck = 'unavailable'; $dirtyCheckReason = "cannot-determine: $($_.Exception.Message)" }
        }
    }
    # After the LAST git read (the pre-launch snapshot and the dirty check above): a `git status` can restart the
    # worktree's fsmonitor daemon, which would outlive the lane. Fail-soft and recorded.
    if ($null -ne $AfterGit) {
        try { $fsmonitorStop = & $AfterGit } catch { $fsmonitorStop = [ordered]@{ action = 'failed'; reason = "cannot-determine: $($_.Exception.Message)" } }
    }
    foreach ($artifact in @(@($outPath, $stdout), @($errPath, $stderrText), @($lastPath, $final))) {
        try { [System.IO.File]::WriteAllText($artifact[0], [string]$artifact[1], $utf8) } catch { $artifactErrors += "$($artifact[0]): $($_.Exception.Message)" }
    }
    if ($artifactErrors.Count -gt 0) {
        $artifactProblem = "producer-artifact-write-failed: $($artifactErrors -join ' | ')"
        $failure = if ($failure) { "$failure | $artifactProblem" } else { $artifactProblem }
        $complete = $false
    }
    $providerRefusal = if ($null -ne $outcome) { $outcome.providerRefusal } else { $null }
    $rootExited = if ($null -ne $run) { [bool]$run.rootExited } else { $false }
    # Observed, never assumed: true only when a child existed and its root was seen to exit. A run that never started
    # (budget spent in setup, a refusal) has no process to have ended.
    $processEnded = ($null -ne $run) -and $rootExited
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
        auxiliaryModels = @(); pinnedByOverride = $false
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
        outputIncomplete = $(if ($null -ne $run) { [bool]$run.outputIncomplete } else { $false })
        killOutcome = $(if ($null -ne $run) { $run.killOutcome } else { $null })
        promptSha256 = (Get-ProducerSha256 $Prompt); promptBytes = $utf8.GetByteCount($Prompt)
        outputSha256 = (Get-ProducerSha256 $final); outputBytes = $utf8.GetByteCount($final)
        receiptPath = $rcptPath; promptPath = $promptPath; outputPath = $lastPath; stdoutPath = $outPath; stderrPath = $errPath
        failure = $failure
        dispatchLedger = [ordered]@{ state = $dispatchState; reservationId = $dispatchId }
        providerRefusal = $providerRefusal
        containment = [ordered]@{ kind = 'process-tree-kill-at-deadline'; killOutcome = $(if ($null -ne $run) { $run.killOutcome } else { $null }); rootExited = $rootExited }
        scratch = $scratchDisposition; bulkScratch = $bulkScratch
        worktreeDisposition = [ordered]@{ action = 'kept'; reason = 'producer engines never retire a worktree (-RetireWorktree is refused)' }
        worktreeSweep = [ordered]@{ skipped = 'producer-engine' }
        fsmonitorStop = $fsmonitorStop
        processEnded = $processEnded
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
    # A reader that holds the receipt open past the replace's 2 s retry must not erase the typed exit or the result: the
    # terminal receipt is then written beside the slot (a path nobody holds) and the exit code is still returned.
    try {
        Write-ProducerFileAtomic -Path $rcptPath -Content ($receipt | ConvertTo-Json -Depth 8)
    } catch {
        $receipt['publishError'] = "producer-receipt-publish-failed: $($_.Exception.Message)"
        $receipt['receiptPath'] = "$base.receipt.terminal.json"
        try { [System.IO.File]::WriteAllText("$base.receipt.terminal.json", ($receipt | ConvertTo-Json -Depth 8), $utf8) } catch { $receipt['publishError'] += " | terminal copy also failed: $($_.Exception.Message)" }
    }
    # Exit code: 125 refused, 124 timed out, 127 never ran; a run that is NOT complete never exits 0.
    $propagated = if ($null -ne $providerRefusal) { 125 }
                  elseif ($timedOut) { 124 }
                  elseif ($exitCode -eq -999) { 127 }
                  elseif ($exitCode -ge 0 -and $exitCode -le 255) { $exitCode }
                  else { 1 }
    if ($propagated -eq 0 -and -not $complete) { $propagated = 1 }
    return [pscustomobject]@{ Receipt = [pscustomobject]$receipt; ExitCode = $propagated }
}
