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

# ONE definition of "a line" for every scan in this file (KERNEL-K4 r3, adversary A5): CRLF, LF, a bare CR,
# NEL (U+0085), LS (U+2028) and PS (U+2029) all end a line. Splitting on LF alone let a bare CR join two
# answer lines into one, so a line that merely sat NEXT TO refusal vocabulary was classified as naming it.
function Split-LaneLines {
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return @() }
    return [regex]::Split($Text, '\r\n|[\r\n' + [string][char]0x85 + [char]0x2028 + [char]0x2029 + ']')
}

# The line split for JSON text (KERNEL-K4 r3b, adversary A1): CRLF, LF and a bare CR only. NEL / LS / PS are legal
# INSIDE a JSON string and are not JSON whitespace, so splitting a JSON candidate on them (Split-LaneLines) would
# corrupt a legal string and let an illegal object parse. A raw CR/LF is never legal inside a JSON string, so the
# normalisation to LF cannot change whether a candidate parses.
function Split-LaneJsonLines {
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return @() }
    return [regex]::Split($Text, '\r\n|[\r\n]')
}

function Find-ClaudeResultEnvelope {
    param([AllowEmptyString()][AllowNull()][string]$Answer)
    if ([string]::IsNullOrWhiteSpace($Answer)) { return $null }
    foreach ($ln in @(Split-LaneJsonLines -Text $Answer)) {
        # JSON whitespace only (r3c): String.Trim() would also strip NBSP / NEL / LS / PS, which are not JSON whitespace.
        $t = $ln.Trim([char[]](9, 10, 13, 32))
        if ($t.Length -eq 0) { continue }
        $cand = ConvertFrom-LaneJsonObject -Text $t
        if ($null -eq $cand) { continue }
        $candNames = @($cand.PSObject.Properties | ForEach-Object { $_.Name })
        # r3c (A4, advisory): type must BE the string "result"; [string]@('result') is also 'result', so an array-typed
        # diagnostic line was taken for the envelope and hid the real one behind it.
        if (-not ($candNames -contains 'type') -or $cand.type -isnot [string] -or $cand.type -ne 'result') { continue }
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
    if ($Cap -lt 1) { $Cap = 1 }
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

# A JSON OBJECT, strictly (KERNEL-K4 r3, adversary A3): System.Text.Json with its defaults (no comments, no
# trailing commas) must accept the text and its root must be an object. PowerShell ConvertFrom-Json is more
# lenient than JSON (it takes a trailing comma and comments), so it only builds the object AFTER that check.
# A non-object root (an array included), a malformed text or a blank one is $null.
function ConvertFrom-LaneJsonObject {
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    # r3c (adversary A1): the object is BUILT from the strict parse, never re-parsed by ConvertFrom-Json, which throws
    # on valid JSON (nested keys that differ only by case, an empty key, a key named PSObject) and so erased a real
    # verdict. Top-level members only (every consumer reads top-level scalars): a nested value is kept as its raw
    # text. A duplicate key: the last one wins; a key that collides with an earlier one only by case is dropped
    # (PowerShell member names are case-insensitive), so the first spelling stays addressable.
    $o = $null
    try {
        $doc = [System.Text.Json.JsonDocument]::Parse($Text)
        try {
            if ($doc.RootElement.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) { return $null }
            $o = New-Object psobject
            foreach ($p in $doc.RootElement.EnumerateObject()) {
                $e = $p.Value
                $v = $null
                switch ($e.ValueKind.ToString()) {
                    'String' { $v = $e.GetString() }
                    'True'   { $v = $true }
                    'False'  { $v = $false }
                    'Null'   { $v = $null }
                    'Number' { $l = 0L; if ($e.TryGetInt64([ref]$l)) { $v = $l } else { $v = $e.GetRawText() } }
                    default  { $v = $e.GetRawText() }
                }
                try {
                    $have = $o.PSObject.Properties[$p.Name]
                    if ($null -ne $have -and $have.Name -cne $p.Name) { continue }
                    Add-Member -InputObject $o -NotePropertyName $p.Name -NotePropertyValue $v -Force -ErrorAction Stop
                } catch { continue }
            }
        } finally { $doc.Dispose() }
    } catch { return $null }
    return $o
}

# True when the text is one JSON value that is neither an object nor an array: null, true, false, a number or a
# string (r3c, adversary A2). Such a final line is a non-object terminal value, not prose.
function Test-LaneJsonScalar {
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) { return $false }
    $scalar = $false
    try {
        $doc = [System.Text.Json.JsonDocument]::Parse($Text)
        try {
            $k = $doc.RootElement.ValueKind
            $scalar = ($k -ne [System.Text.Json.JsonValueKind]::Object -and $k -ne [System.Text.Json.JsonValueKind]::Array)
        } finally { $doc.Dispose() }
    } catch { return $false }
    return $scalar
}

# Characters as Unicode code points: a surrogate pair is one character (r3c, adversary A3). String.Length counts
# UTF-16 units, so a prompt of 205 characters with 195 emoji read as 400 and lost the short-probe exemption.
function Get-LaneCodePointCount {
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return 0 }
    return [regex]::Replace($Text, '[\uD800-\uDBFF][\uDC00-\uDFFF]', 'x').Length
}

# Index of the bracket that closes the JSON value opened at $Start ('{' or '['), skipping string contents and
# escapes; -1 when the value never closes. It balances brackets only: strictness is ConvertFrom-LaneJsonObject's.
function Find-LaneJsonValueEnd {
    param([string]$Text, [int]$Start)
    # A stack of the closers still owed (r3b, adversary A2): a closer that is not the innermost owed one is a
    # MISMATCH, so the value is malformed and never closes (-1) instead of silently ending an outer value early.
    $stack = New-Object System.Collections.Generic.Stack[char]; $inStr = $false; $esc = $false
    for ($i = $Start; $i -lt $Text.Length; $i++) {
        $ch = $Text[$i]
        if ($inStr) {
            if ($esc) { $esc = $false } elseif ($ch -ceq [char]92) { $esc = $true } elseif ($ch -ceq [char]34) { $inStr = $false }
            continue
        }
        if ($ch -ceq [char]34) { $inStr = $true }
        elseif ($ch -ceq '{') { $stack.Push([char]'}') }
        elseif ($ch -ceq '[') { $stack.Push([char]']') }
        elseif ($ch -ceq '}' -or $ch -ceq ']') {
            if ($stack.Count -eq 0 -or $stack.Pop() -cne $ch) { return -1 }
            if ($stack.Count -eq 0) { return $i }
        }
    }
    return -1
}

# The LAST top-level JSON value of unfenced text, as the text to parse. Top-level values begin on a line-start
# '{' or '[' and run to their balancing bracket, so a nested '{' line inside an unfinished or finished root is
# never a candidate of its own (adversary A4). Anything but whitespace after the last value, an unclosed last
# value, and a non-object last value all yield text that fails the strict object parse. $null: no value.
function Get-LaneLastTopLevelJson {
    param([AllowEmptyString()][AllowNull()][string]$Text)
    if ([string]::IsNullOrEmpty($Text)) { return $null }
    $rx = [regex]'(?m)^[ \t]*[\{\[]'
    $p = 0; $start = -1; $end = -1
    while ($p -lt $Text.Length) {
        $m = $rx.Match($Text, $p)
        if (-not $m.Success) { break }
        $s = $m.Index + $m.Length - 1
        $e = Find-LaneJsonValueEnd -Text $Text -Start $s
        $start = $s; $end = $e
        if ($e -lt 0) { break }
        $p = $e + 1
    }
    if ($start -lt 0) { return $null }
    $tailText = $Text.Substring($start)
    # Only JSON whitespace may follow the value (r3c): a trailing NBSP / NEL / LS / PS is garbage, not a blank tail.
    if ($end -ge 0 -and $Text.Substring($end + 1).Trim([char[]](9, 10, 13, 32)).Length -eq 0) { $tailText = $Text.Substring($start, $end - $start + 1) }
    return [pscustomobject]@{ text = $tailText; offset = $start }
}

# Get-LaneTerminalCandidate: the ONE selector of an answer's terminal JSON candidate, by POSITION across
# both forms (KERNEL-K4 r3, Dot items 1 and 3). The candidates are the fenced blocks and the unfenced
# top-level values; the LAST one in the answer decides and an earlier one never stands in for it.
#   - A fence opens on a line of 3+ backticks or 3+ tildes (any info string) and closes ONLY on a line of the
#     SAME character, at least as long as the opening, and nothing else. A mismatched or short close is
#     content. A fence that never closes runs to the end of the answer and counts ONLY when that body is one
#     complete strict JSON object (adversary A1, narrowed by the real-receipt replay: see the note below).
#   - An unfenced candidate is the last top-level '{' or '[' value after the last fenced block (see
#     Get-LaneLastTopLevelJson). It outranks the fenced block before it, whatever its shape.
# Lines are split by Split-LaneJsonLines. Returns [pscustomobject]@{ form = 'fenced'|'unfenced'|'none'; text; startLine }.
function Get-LaneTerminalCandidate {
    param([AllowEmptyString()][AllowNull()][string]$Answer)
    $none = [pscustomobject]@{ form = 'none'; text = $null; startLine = -1 }
    if ([string]::IsNullOrWhiteSpace($Answer)) { return $none }
    $lines = @(Split-LaneJsonLines -Text $Answer)
    $inBlock = $false; $fenceChar = ''; $fenceLen = 0
    $cur = $null; $curStart = -1
    $lastFence = $null
    for ($i = 0; $i -lt $lines.Count; $i++) {
        $ln = $lines[$i]
        if (-not $inBlock) {
            if ($ln -match '^[ \t]*(`{3,}|~{3,})') {
                $inBlock = $true; $fenceChar = $Matches[1].Substring(0, 1); $fenceLen = $Matches[1].Length
                $cur = New-Object System.Collections.Generic.List[string]; $curStart = $i
            }
            continue
        }
        if ($ln -match '^[ \t]*(`{3,}|~{3,})[ \t]*$' -and $Matches[1].Substring(0, 1) -ceq $fenceChar -and $Matches[1].Length -ge $fenceLen) {
            $inBlock = $false
            $lastFence = [pscustomobject]@{ text = ($cur -join "`n"); startLine = $curStart; endLine = $i }
            continue
        }
        $cur.Add($ln)
    }
    # An open final fence runs to the end of the answer. Its body is the candidate text, so the strict object
    # parse decides: a complete object whose closing fence the model dropped is a finished verdict (two REAL
    # complete codex receipts end that way), while truncated JSON, trailing prose, or a close of another
    # character or a shorter length (all of which stay in the body) fail the parse and are never a completion.
    if ($inBlock) { return [pscustomobject]@{ form = 'fenced'; text = ($cur -join "`n"); startLine = $curStart } }
    $firstTail = if ($null -eq $lastFence) { 0 } else { $lastFence.endLine + 1 }
    if ($firstTail -lt $lines.Count) {
        # r3c (adversary A2): when the LAST non-blank line is a lone JSON scalar (null / false / 0 / "text") it is the
        # terminal value, a non-object, so no earlier fenced or unfenced approval stands in for it.
        for ($li = $lines.Count - 1; $li -ge $firstTail; $li--) {
            $lt = $lines[$li].Trim([char[]](9, 10, 13, 32))
            if ($lt.Length -eq 0) { continue }
            if (Test-LaneJsonScalar -Text $lt) { return [pscustomobject]@{ form = 'unfenced'; text = $lt; startLine = $li } }
            break
        }
        $tailText = ($lines[$firstTail..($lines.Count - 1)] -join "`n")
        $c = Get-LaneLastTopLevelJson -Text $tailText
        if ($null -ne $c) {
            $before = $tailText.Substring(0, $c.offset)
            return [pscustomobject]@{ form = 'unfenced'; text = $c.text; startLine = $firstTail + ($before.Split("`n").Count - 1) }
        }
    }
    if ($null -ne $lastFence) { return [pscustomobject]@{ form = 'fenced'; text = $lastFence.text; startLine = $lastFence.startLine } }
    return $none
}

# The FINAL json object of an answer: whatever Get-LaneTerminalCandidate selects by position. A malformed,
# unterminated or non-object terminal candidate is $null: an earlier provisional block never stands in for it.
function Get-LaneFinalJsonObject {
    param([AllowEmptyString()][AllowNull()][string]$Answer)
    $cand = Get-LaneTerminalCandidate -Answer $Answer
    if ($cand.form -eq 'none') { return $null }
    return (ConvertFrom-LaneJsonObject -Text $cand.text)
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
            # A real JSON boolean only (r3b A3): [bool]$null, [bool]0 and [bool]@() are $false, so a null or
            # non-boolean is_error must read as absent, never as "not an error".
            if ($names -contains 'is_error' -and $j.is_error -is [bool]) { $rec.isError = $j.is_error }
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
            $vp = if ($null -ne $fin) { @($fin.PSObject.Properties | Where-Object { $_.Name -ceq 'verdict' }) | Select-Object -First 1 } else { $null }
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
        $isError = ($names -contains 'is_error') -and ($j.is_error -is [bool]) -and ($j.is_error -eq $true)
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
    # Residual, named: a refusal whose text arrives AS the answer is detected only under the exception below;
    # an answer made only of ERROR lines may be exactly what the prompt asked for (adversary A3).
    # Exception (r3, sol r2 B1 + Dot item 2): an answer bypasses the "it answered" guard ONLY when EVERY
    # answer line is ERROR-framed AND itself names a provider-refusal kind, AND an ERROR-framed stderr line
    # names one of THOSE SAME kinds (adversary A6: the base saw it on stderr with a blank stdout). So
    # unrelated recovered stderr ("ERROR: upstream probe returned 429; recovered") never invalidates a
    # legitimate answer such as `ERROR: file not found`, and neither does a refusal-vocabulary answer whose
    # stderr names a different kind. With nothing corroborating on stderr it is what the prompt asked for (A3).
    $answerKinds = $null
    if (Test-LaneAnswerHasSubstance -Answer $Answer) {
        $ansLines = @((Split-LaneLines -Text $Answer) | Where-Object { Test-LaneAnswerHasSubstance -Answer $_ })
        $answerKinds = New-Object System.Collections.Generic.List[string]
        foreach ($al in $ansLines) {
            $ak = if ($al -match '^\s*ERROR\b') { Get-RefusalKind -Line $al } else { $null }
            if ($null -eq $ak) { return $null }
            if (-not $answerKinds.Contains($ak)) { $answerKinds.Add($ak) }
        }
    }
    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $scan = $Text
    if (-not [string]::IsNullOrEmpty($Prompt)) {
        $needle = $Prompt -replace "`r`n", "`n"
        $hay    = $scan   -replace "`r`n", "`n"
        $i = $hay.IndexOf($needle, [System.StringComparison]::Ordinal)
        if ($i -ge 0) { $scan = $hay.Remove($i, $needle.Length) } else { $scan = $hay }
    }
    foreach ($line in @(Split-LaneLines -Text $scan)) {
        if ($line -notmatch '^\s*ERROR\b') { continue }
        $kind = Get-RefusalKind -Line $line
        if ($null -eq $kind) { continue }
        if ($null -ne $answerKinds -and -not $answerKinds.Contains($kind)) { continue }
        return New-RefusalRecord -Kind $kind -Engine $Engine -Match $line
    }
    return $null
}
