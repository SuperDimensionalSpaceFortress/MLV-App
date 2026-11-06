# Get-ProviderRefusal: decide whether a lane run was REFUSED BY THE PROVIDER, structurally.
#
# WHY THIS EXISTS (incident 2026-09-07T01:28Z, receipt fleet-runs\20260907T012821Z):
# the codex lane `sol` was dispatched for round 3 of the PR #79 review and the provider
# refused it in 7.7 s -- "You've hit your usage limit ... try again at Sep 10th". The
# receipt recorded exitCode=1, failure=null, complete=true, state=complete. Every
# downstream reader (the heartbeat snapshot, a hub reading receipts) saw a FINISHED
# review whose verdict happened to be non-zero, not a review that NEVER RAN. The hub
# then idled for an hour. The same shape was measured 2026-09-05 on the claude engine
# (HTTP 429 "You've hit your session limit", two of twelve dispatches).
#
# A refusal is a third outcome beside "ran" and "threw": the process launched and exited
# cleanly, but the provider did no work. It is NOT a failure of the lane script (failure
# stays null) and it is NOT completion (complete becomes false). The remedy is outside
# the board -- for codex it is an account rotation the owner performs -- so the receipt
# has to say so in a field a machine can branch on.
#
# HOW IT DECIDES -- and why it does NOT scan the transcript for phrases (sol PR #80 R1+R2, both BLOCKER):
# a lane's transcript legitimately CONTAINS the phrases (codex echoes the prompt; a reviewer reading this
# repo prints "api_error_status 429" comments). Text is never evidence. Structure is:
#   codex : NO ANSWER (empty stdout) AND, after the echoed prompt BLOCK is removed from stderr, an
#           `ERROR:`-framed line carrying the vocabulary.
#   claude: the JSON envelope's own is_error / api_error_status fields decide; vocabulary names the KIND.
# The prompt is removed as a WHOLE BLOCK, never line by line, so a genuine refusal identical to an
# echoed line is still seen (sol R2 finding 2).
#
# Dot-sourced by Invoke-Lane.ps1; exercised directly by test_coordination_guardrails.py
# against the real 2026-09-07 stderr, a known-good transcript, and prose that only QUOTES a refusal.
# ASCII-only by project convention.

function Get-RefusalKind {
    param([AllowEmptyString()][AllowNull()][string]$Line)
    if ([string]::IsNullOrWhiteSpace($Line)) { return $null }
    $patterns = @(
        @{ kind = 'provider-usage-limit'; rx = "you'?ve hit your (usage|session|weekly|monthly) limit" },
        @{ kind = 'provider-usage-limit'; rx = 'purchase more credits' },
        @{ kind = 'provider-usage-limit'; rx = 'usage[ _-]limit[ _-]reached' },
        @{ kind = 'provider-rate-limit';  rx = '\brate[ _-]limit(ed|s)?\b' },
        @{ kind = 'provider-rate-limit';  rx = '\b429\b' },
        @{ kind = 'provider-auth';        rx = 'not logged in|invalid api key|authentication failed' },
        # Measured 2026-09-15 (fleet-runs\ws-PLAY-COUNTERS-CPU-B-20260915T080318Z): the claude CLI's
        # token expired after an account rotation; envelope is_error=true, api_error_status=null.
        @{ kind = 'provider-auth';        rx = 'failed to authenticate|oauth session expired' }
    )
    foreach ($p in $patterns) { if ($Line -imatch $p.rx) { return $p.kind } }
    return $null
}

function New-RefusalRecord {
    param([string]$Kind, [string]$Engine, [string]$Match)
    $retry = $null
    if ($Match -imatch 'try again (at|after|in) ([^."]+)') { $retry = $Matches[2].Trim() }
    $remedy = switch ($Kind) {
        'provider-usage-limit' { 'owner rotates the provider account; re-dispatch the same prompt after a probe' }
        'provider-rate-limit'  { 'wait and re-dispatch the same prompt; no rotation implied' }
        'provider-auth'        { 'owner re-authenticates the provider CLI; never an agent keystroke' }
        default                { 'unclassified provider error; read the matched line' }
    }
    return [ordered]@{ kind = $Kind; engine = $Engine; match = $Match.Trim(); retryAfter = $retry; remedy = $remedy }
}

function Find-ClaudeResultEnvelope {
    param([AllowEmptyString()][AllowNull()][string]$Answer)
    if ([string]::IsNullOrWhiteSpace($Answer)) { return $null }
    foreach ($ln in ($Answer -split "`r?`n")) {
        $t = $ln.Trim()
        if ($t.Length -eq 0) { continue }
        $cand = $null
        try { $cand = $t | ConvertFrom-Json -ErrorAction Stop } catch { continue }
        if ($null -eq $cand -or $cand -isnot [System.Management.Automation.PSCustomObject]) { continue }
        $candNames = @($cand.PSObject.Properties | ForEach-Object { $_.Name })
        if (-not ($candNames -contains 'type') -or [string]$cand.type -ne 'result') { continue }
        return $cand
    }
    return $null
}

# Get-LaneWorkEvidence: POSITIVE evidence that the lane finished its work, never "the process ended".
# Incident 2026-09-14 (fleet-runs\ws-PLAY-COUNTERS-CPU-20260914T151951Z): exitCode 1, envelope
# is_error=true subtype=error_max_turns terminal_reason=max_turns -- and the receipt said
# state=complete, complete=true, because both fields only recorded that the child had exited.
# workCompleted is $true only when: exit code 0 AND (codex: a non-blank answer; reason
# codex-empty-answer otherwise) or (claude: a result envelope exists with is_error=false, subtype=success and terminal_reason
# absent or 'completed'). A missing envelope on the claude engine is NOT evidence of completion.
# Get-CodexLaneAnswer: the answer a codex lane gave, for completion evidence. It is the `-o` last-message
# file, else stdout (codex prints the final message to both). NEVER stderr: that is working narration, so
# counting it would let a silent exit 0 read as an answer. KERNEL-K4-CODEX-SILENT-EXIT0-COMPLETED-1 r1
# (adversary A1): Invoke-Lane used to hand Get-LaneWorkEvidence stdout alone, which would call a real answer
# that reached only the last-message file "empty".
function Get-CodexLaneAnswer {
    param(
        [AllowEmptyString()][AllowNull()][string]$LastMessage = '',
        [AllowEmptyString()][AllowNull()][string]$Stdout = ''
    )
    if (Test-LaneAnswerHasSubstance -Answer $LastMessage) { return $LastMessage }
    if (Test-LaneAnswerHasSubstance -Answer $Stdout) { return $Stdout }
    return ''
}

# KERNEL-K4-CODEX-SILENT-EXIT0-COMPLETED-1 r2 (sol r1 B1). The codex answer must have substance, not only
# bytes. SAMPLE (2026-11-06, every fleet-runs receipt with engine=codex, state=complete, exitCode=0 whose
# answer file still exists: 2379 receipts; 90 more were archived with their answers and are excluded):
# with whitespace and Unicode Cf characters removed, 7 answers are shorter than 120 characters. All 7 are
# smoke or acceptance probes whose prompt is under 200 bytes ("Reply with exactly: OK", "run git
# --version"). Every receipt whose prompt is 400 bytes or more (2372) answered with at least 231 such
# characters. So the floor is 120 for any prompt of 400+ characters (rejects 0 of 2372 real answers) and
# a prompt under 400 characters is a probe that may answer in one word (rejects 0 of the 7). Below 400 an
# empty answer is still empty.
$script:CODEX_ANSWER_FLOOR_CHARS = 120
$script:CODEX_PROBE_PROMPT_CHARS = 400

# Characters that count toward the floor: whitespace and Unicode category Cf (U+200B zero-width space,
# U+FEFF, U+2060, and the supplementary-plane tag characters U+E0001..U+E007F) are removed first, because
# .NET IsNullOrWhiteSpace does not treat Cf as blank. Counted by code point (a surrogate pair is one
# character, and a Cf pair is none) and capped: callers only need "0", "under the floor" or "enough".
function Get-LaneAnswerSubstanceLength {
    param([AllowEmptyString()][AllowNull()][string]$Answer, [int]$Cap = 2147483647)
    if ([string]::IsNullOrEmpty($Answer)) { return 0 }
    $rest = [regex]::Replace($Answer, '[\s\p{Cf}]', '')
    if ($rest.Length -eq 0) { return 0 }
    if (-not [regex]::IsMatch($rest, '[\uD800-\uDFFF]')) { return [Math]::Min($rest.Length, $Cap) }
    $n = 0
    foreach ($r in $rest.EnumerateRunes()) {
        $cat = [System.Text.Rune]::GetUnicodeCategory($r)
        if ($cat -eq [System.Globalization.UnicodeCategory]::Format -or [System.Text.Rune]::IsWhiteSpace($r)) { continue }
        $n++
        if ($n -ge $Cap) { break }
    }
    return $n
}

# True when the answer has any substance (a blank, whitespace-only or Cf-only answer has none).
function Test-LaneAnswerHasSubstance {
    param([AllowEmptyString()][AllowNull()][string]$Answer)
    return ((Get-LaneAnswerSubstanceLength -Answer $Answer -Cap 1) -gt 0)
}

# A prompt that spells out a {"verdict": ...} json object is a review key (or an adversary pass): the
# prompt text is the one signal Invoke-Lane already holds, and it names the contract the answer owes.
# Measured on the same sample: all 1031 key prompts and their answers carried the field.
function Test-PromptDemandsVerdict {
    param([AllowEmptyString()][AllowNull()][string]$Prompt)
    if ([string]::IsNullOrEmpty($Prompt)) { return $false }
    return [regex]::IsMatch($Prompt, '"verdict"\s*:')
}

# The FINAL json object of an answer: the LAST fenced block (an unterminated last fence runs to the end of
# the answer), else the last line-start '{' that parses to the end. A malformed or non-object final block
# is $null: an earlier provisional block never stands in for it. Returns the object, or $null.
function Get-LaneFinalJsonObject {
    param([AllowEmptyString()][AllowNull()][string]$Answer)
    if ([string]::IsNullOrWhiteSpace($Answer)) { return $null }
    $fenced = [regex]::Matches($Answer, '(?ms)^[ \t]*```[A-Za-z]*[ \t]*\r?\n(.*?)(?:^[ \t]*```[ \t]*\r?$|\z)')
    if ($fenced.Count -gt 0) {
        try { $o = $fenced[$fenced.Count - 1].Groups[1].Value | ConvertFrom-Json -ErrorAction Stop } catch { return $null }
        if ($o -is [System.Management.Automation.PSCustomObject]) { return $o }
        return $null
    }
    $starts = [regex]::Matches($Answer, '(?m)^\s*\{')
    for ($i = $starts.Count - 1; $i -ge 0; $i--) {
        $tail = $Answer.Substring($starts[$i].Index).Trim()
        try { $o = $tail | ConvertFrom-Json -ErrorAction Stop } catch { continue }
        if ($o -is [System.Management.Automation.PSCustomObject]) { return $o }
    }
    return $null
}

function Get-LaneWorkEvidence {
    param(
        [string]$Engine = 'unknown',
        [AllowEmptyString()][AllowNull()][string]$Answer = '',
        [int]$ExitCode = -999,
        # Characters in the prompt the lane was given; -1 = unknown, which applies the answer floor.
        [int]$PromptChars = -1,
        # Set by Invoke-Lane when the prompt demands a {"verdict": ...} final json object.
        [switch]$RequireVerdict
    )
    $rec = [ordered]@{ workCompleted = $false; reason = $null; subtype = $null; terminalReason = $null; isError = $null }
    if ($Engine -eq 'claude') {
        $j = Find-ClaudeResultEnvelope -Answer $Answer
        if ($null -ne $j) {
            $names = @($j.PSObject.Properties.Name)
            if ($names -contains 'subtype') { $rec.subtype = [string]$j.subtype }
            if ($names -contains 'terminal_reason') { $rec.terminalReason = [string]$j.terminal_reason }
            if ($names -contains 'is_error') { $rec.isError = [bool]$j.is_error }
        }
    }
    if ($ExitCode -ne 0) { $rec.reason = "exit-code-$ExitCode"; return $rec }
    if ($Engine -eq 'claude') {
        if ($null -eq $rec.isError -and $null -eq $rec.subtype) { $rec.reason = 'no-result-envelope'; return $rec }
        if ($null -eq $rec.isError) { $rec.reason = 'envelope-is-error-absent'; return $rec }
        if ($rec.isError -ne $false) { $rec.reason = 'envelope-is-error'; return $rec }
        if ($rec.subtype -ne 'success') { $rec.reason = "subtype-$($rec.subtype)"; return $rec }
        if ($rec.terminalReason -and $rec.terminalReason -ne 'completed') { $rec.reason = "terminal-reason-$($rec.terminalReason)"; return $rec }
    }
    # KERNEL-K4-CODEX-SILENT-EXIT0-COMPLETED-1: codex has no envelope, so its positive evidence is
    # the answer itself. Exit 0 with an empty answer is a lane that said nothing, not a lane that
    # finished (a key that answered nothing must not read as a key that gave no verdict).
    #   1. substance 0 after whitespace and Cf removal            -> codex-empty-answer
    #   2. substance under the floor (120), prompt 400+ chars      -> codex-answer-too-short
    #   3. a verdict-demanding prompt and no final json "verdict"  -> codex-no-verdict
    # NARROWED PROMISE (r2): "is this answer about the prompt" is not decidable, so a LONG off-topic answer
    # to a prompt that demands no verdict is still booked complete; telling that apart is the consumer's
    # parse. The floor and the verdict shape are evidence-bound (sample above), not a relevance judgement.
    # The state this yields is 'ended-incomplete', the same one a claude 'no-result-envelope' gets.
    if ($Engine -eq 'codex') {
        $substance = Get-LaneAnswerSubstanceLength -Answer $Answer -Cap $script:CODEX_ANSWER_FLOOR_CHARS
        if ($substance -eq 0) { $rec.reason = 'codex-empty-answer'; return $rec }
        if ($substance -lt $script:CODEX_ANSWER_FLOOR_CHARS -and ($PromptChars -lt 0 -or $PromptChars -ge $script:CODEX_PROBE_PROMPT_CHARS)) {
            $rec.reason = 'codex-answer-too-short'; return $rec
        }
        if ($RequireVerdict) {
            $fin = Get-LaneFinalJsonObject -Answer $Answer
            $vp = if ($null -ne $fin) { $fin.PSObject.Properties['verdict'] } else { $null }
            $v = if ($null -ne $vp -and $null -ne $vp.Value) { [string]$vp.Value } else { '' }
            if ([string]::IsNullOrWhiteSpace($v)) { $rec.reason = 'codex-no-verdict'; return $rec }
        }
    }
    $rec.workCompleted = $true
    return $rec
}

function Get-ProviderRefusal {
    [CmdletBinding()]
    param(
        [AllowEmptyString()][AllowNull()][string]$Text,          # stderr
        [string]$Engine = 'unknown',
        [AllowEmptyString()][AllowNull()][string]$Prompt = '',   # echoed by codex into stderr; removed as a block
        [AllowEmptyString()][AllowNull()][string]$Answer = ''    # stdout: the answer (codex) / the envelope (claude)
    )
    if ($Engine -eq 'claude') {
        if ([string]::IsNullOrWhiteSpace($Answer)) { return $null }
        # sol PR #80 R3 MAJOR: whole-string ConvertFrom-Json fails open (silently returns null) the
        # instant stdout carries ANY non-whitespace diagnostic line before or after the envelope.
        # Invoke-Lane harvests raw, unfiltered stdout, so a contaminated stream must not eat a real
        # refusal. --output-format json emits the envelope as ONE LINE; scan lines and use the first
        # one that parses as a JSON object, skipping any diagnostic lines around it.
        # sol PR #80 R4 MAJOR: the first-parseable-object rule trusted ANY JSON-shaped line, so a
        # JSON-shaped diagnostic line with its own is_error/api_error_status fields (printed before
        # the real envelope) was picked instead -- a false refusal, or the mirror false negative if
        # the diagnostic came first and hid a later real one. --output-format json's envelope always
        # carries "type":"result" (see REAL_CLAUDE_429_ENVELOPE / CLAUDE_SUCCESS_QUOTING_ENVELOPE in
        # the tests); require that exact shape so an unrelated JSON blob is never mistaken for it.
        $j = Find-ClaudeResultEnvelope -Answer $Answer
        if ($null -eq $j) { return $null }
        $names = @($j.PSObject.Properties.Name)
        $isError = ($names -contains 'is_error') -and ($j.is_error -eq $true)
        $status  = if ($names -contains 'api_error_status') { [string]$j.api_error_status } else { '' }
        if (-not $isError -and $status -eq '') { return $null }
        $msg  = if ($names -contains 'result') { [string]$j.result } else { '' }
        $kind = Get-RefusalKind -Line $msg
        # Result TEXT never classifies a successful envelope (sol PR #117 BLOCKER): with is_error=false only
        # the numeric 429 status counts. 134 recorded envelopes never pair is_error=false with a status.
        if (-not $isError) { $kind = $null }
        if ($null -eq $kind -and $status -eq '429') { $kind = 'provider-rate-limit' }
        if ($null -eq $kind) { return $null }
        return New-RefusalRecord -Kind $kind -Engine $Engine -Match ("api_error_status=$status " + $msg)
    }
    # It answered: not refused, and stderr narration (a recovered 429, say) never overturns that. "Answered"
    # uses the same emptiness test as the completion evidence (K4 r2: a Cf-only answer is not an answer).
    # Residual, named: a refusal whose text arrives AS the answer is not detected here, as before K4; an
    # answer made only of ERROR lines may be exactly what the prompt asked for (adversary A3).
    if (Test-LaneAnswerHasSubstance -Answer $Answer) { return $null }
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $scan = $Text
    if (-not [string]::IsNullOrEmpty($Prompt)) {
        $needle = $Prompt -replace "`r`n", "`n"
        $hay    = $scan   -replace "`r`n", "`n"
        $i = $hay.IndexOf($needle, [System.StringComparison]::Ordinal)
        if ($i -ge 0) { $scan = $hay.Remove($i, $needle.Length) } else { $scan = $hay }
    }
    foreach ($line in ($scan -split "`r?`n")) {
        if ($line -notmatch '^\s*ERROR\b') { continue }
        $kind = Get-RefusalKind -Line $line
        if ($null -ne $kind) { return New-RefusalRecord -Kind $kind -Engine $Engine -Match $line }
    }
    return $null
}
