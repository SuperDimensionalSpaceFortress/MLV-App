# Test-CosOpenAskScan.ps1 - fixture-driven tests for cos-open-ask-scan.ps1 (COS-OPEN-ASK-SCAN-1). SYNTHETIC fixtures only:
# nothing here reads the doctrine bus. Prints one PASS/FAIL line per assertion and a final `TESTS passed=<n> failed=<n>`; exit 0 only
# when every assertion held (and the script exists: an absent script is RED, not a skip).
#   pwsh -NoProfile -File Test-CosOpenAskScan.ps1 [-ScriptPath <path>]   (RED proof: point -ScriptPath at a path that does not exist)
param(
    [Alias('Script')]
    [string]$ScriptPath = (Join-Path $PSScriptRoot 'cos-open-ask-scan.ps1'),
    [string]$WorkRoot = (Join-Path ([IO.Path]::GetTempPath()) ("cos-scan-test-" + [guid]::NewGuid().ToString('N').Substring(0, 8)))
)
$ErrorActionPreference = 'Continue'
$pass = 0; $fail = 0
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    if ($Ok) { $script:pass++; Write-Output "PASS $Name" } else { $script:fail++; Write-Output "FAIL $Name :: $Detail" }
}
# CheckRed: a row that only the r2 scanner passes (MERGE-GATE-TIP-LEGS-1 r2). Run with -ScriptPath <the r1 scanner backup> to see them fail: `RED_ROWS failed` must then equal `total`.
$redTotal = 0; $redFail = 0
function CheckRed([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $script:redTotal++
    if ($Ok) { $script:pass++; Write-Output "PASS [RED] $Name" } else { $script:fail++; $script:redFail++; Write-Output "FAIL [RED] $Name :: $Detail" }
}
# CheckR3: a row that only the r3 scanner passes (MERGE-GATE-TIP-LEGS-1 r3). Run with -ScriptPath <backup-pre-r3\cos-open-ask-scan.ps1> to see them fail: `R3_ROWS failed` must then equal `total`.
$r3Total = 0; $r3Fail = 0
function CheckR3([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $script:r3Total++
    if ($Ok) { $script:pass++; Write-Output "PASS [R3] $Name" } else { $script:fail++; $script:r3Fail++; Write-Output "FAIL [R3] $Name :: $Detail" }
}
# CheckR1 (COS-SCAN-LATEST-ASK-1 r1): a row that only the r1 scanner passes. Run with -ScriptPath <backup-pre-r1 copy> to see them fail: `R1_ROWS failed` must then equal `total`.
# -Shape a|b|c|d groups the rows of one defect shape; the final `SHAPES green=<n>/4` counts shapes whose every row passed.
$r1Total = 0; $r1Fail = 0; $shapeRows = @{ a = 0; b = 0; c = 0; d = 0 }; $shapeFail = @{ a = 0; b = 0; c = 0; d = 0 }
function CheckR1([string]$Name, [bool]$Ok, [string]$Detail = '', [string]$Shape = '') {
    $script:r1Total++
    if ($Shape) { $script:shapeRows[$Shape]++ }
    if ($Ok) { $script:pass++; Write-Output "PASS [R1] $Name" } else { $script:fail++; $script:r1Fail++; if ($Shape) { $script:shapeFail[$Shape]++ }; Write-Output "FAIL [R1] $Name :: $Detail" }
}
# CheckU1 (COS-SCAN-UNDATED-ASK-1 r1b): a row that only the scanner with the bus-commit-latest candidate passes. Run with -ScriptPath <backup\cos-open-ask-scan.ps1> to see them fail: `U1_ROWS failed` must then equal the rows that assert the new behaviour.
$u1Total = 0; $u1Fail = 0
function CheckU1([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $script:u1Total++
    if ($Ok) { $script:pass++; Write-Output "PASS [U1] $Name" } else { $script:fail++; $script:u1Fail++; Write-Output "FAIL [U1] $Name :: $Detail" }
}
# CheckV1 (COS-SCAN-EDITED-COMMENT-ASK-1 r1): a row that only the scanner dating comment asks by updated_at passes. Run with -ScriptPath <backup\cos-open-ask-scan.ps1> to see them fail: `V1_ROWS failed` must then equal `total`.
$v1Total = 0; $v1Fail = 0
function CheckV1([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $script:v1Total++
    if ($Ok) { $script:pass++; Write-Output "PASS [V1] $Name" } else { $script:fail++; $script:v1Fail++; Write-Output "FAIL [V1] $Name :: $Detail" }
}
$Script = $ScriptPath
if (-not (Test-Path -LiteralPath $Script)) {
    # RED: every case below needs the script, so each one is reported as a failed assertion instead of a harness error.
    Write-Output "FAIL script-present :: $Script does not exist"
    foreach ($case in @('unanswered-not-merge-ready', 'answered-by-ACK', 'ack-before-reviewed', 'distinguish-answers', 'green-no-ask',
            'green-with-ask-sentence', 'closed-pr-skip', 'unparsable-header-unknown')) {
        Write-Output "FAIL $case :: scan did not run (script absent)"
    }
    Write-Output 'TESTS passed=0 failed=9'
    exit 1
}
# Every fixture lives under $WorkRoot (a fresh folder in the temp directory). A sandbox that cannot create it used to turn ~40 rows into confusing failures (sol saw 8/43 in a read-only sandbox):
# say so once, clearly, and fail instead.
try { New-Item -ItemType Directory -Path $WorkRoot -Force -ErrorAction Stop | Out-Null } catch { }
if (-not (Test-Path -LiteralPath $WorkRoot -PathType Container)) {
    Write-Output "FAIL fixture-folder-writable :: cannot create the fixture folder $WorkRoot (temp directory $([IO.Path]::GetTempPath()) is read-only here); no case can run. Run this script in a normal shell, or pass -WorkRoot <a writable folder>."
    Write-Output 'TESTS passed=0 failed=1'
    exit 1
}
$utf8 = [Text.UTF8Encoding]::new($false)
$repo = 'SuperDimensionalSpaceFortress/MLV-App'
$reviewed = '2026-10-15T08:12:00Z'

function Feedback([int]$Pr, [string]$Verdict, [string]$Body = "Review body.`n", [string]$Reviewed = $reviewed, [switch]$BadHeader) {
    $rv = if ($BadHeader) { 'sometime yesterday' } else { $Reviewed }
    return "schema: cos-feedback.v1`nproject: mlv-app`nrepo: $repo`npr: $Pr`nhead: 0123456789abcdef0123456789abcdef01234567`nreviewed_utc: $rv`nevent: pr-updated`nverdict: $Verdict`n`n# CoS feedback for PR $Pr`n`n$Body"
}
function Comment([long]$Id,[string]$Created, [string]$Body, [string]$Login = 'layibabalola') {
    return [ordered]@{ id = $Id; user = [ordered]@{ login = $Login }; created_at = $Created; body = $Body }
}
# Builds a fixture dir with one PR and runs the scanner; returns @{ exit; lines; text; outFile }
function Run-Case([string]$Name, [int]$Pr, [string]$Feedback, $Comments, [string]$State = 'OPEN', [switch]$NoPrData) {
    $dir = Join-Path $WorkRoot $Name
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $dir "pr-$Pr.md"), $Feedback, $utf8)
    if (-not $NoPrData) {
        [IO.File]::WriteAllText((Join-Path $dir "$Pr.state.txt"), $State, $utf8)
        [IO.File]::WriteAllText((Join-Path $dir "$Pr.comments.json"), (ConvertTo-Json -InputObject @($Comments) -Depth 6), $utf8)
    }
    return Run-Scan -Dir $dir -Name $Name
}
function Run-Scan([string]$Dir, [string]$Name, [string[]]$Extra = @()) {
    $of = Join-Path $WorkRoot "$Name.out.md"
    $lines = & pwsh -NoProfile -NonInteractive -File $Script -FixtureDir $Dir -OutFile $of -Repo $repo @Extra 2>&1
    $code = $LASTEXITCODE
    return @{ exit = $code; lines = @($lines | ForEach-Object { "$_" }); text = (@($lines) -join "`n"); outFile = $of }
}
function Has([hashtable]$R, [string]$Pattern) { return [bool]($R.lines | Where-Object { $_ -match $Pattern }) }

# 1. unanswered not-merge-ready
$r = Run-Case 'c1-unanswered' 101 (Feedback 101 'not-merge-ready') @()
Check 'unanswered-not-merge-ready: line' (Has $r '^UNANSWERED pr=101 head=01234567 ask_utc=2026-10-15T08:12:00Z deadline=2026-10-16T08:12:00Z verdict=not-merge-ready( ask_src=header)?$') $r.text
Check 'unanswered-not-merge-ready: exit 10' ($r.exit -eq 10) "exit=$($r.exit)"
Check 'unanswered-not-merge-ready: final counts' (Has $r '^COS_OPEN_ASKS unanswered=1 answered=0 skipped=0 unknown=0$') $r.text

# 2. answered by ACK after reviewed_utc
$r = Run-Case 'c2-answered' 102 (Feedback 102 'not-merge-ready') @((Comment 9001 '2026-10-15T09:45:16Z' 'ACK: plan is the parity fix in r2, ETA 2026-10-16'))
Check 'answered-by-ACK: line' (Has $r '^ANSWERED pr=102 by=9001 2026-10-15T09:45:16Z author=layibabalola kind=ACK( ask_src=header)?$') $r.text
Check 'answered-by-ACK: exit 0' ($r.exit -eq 0) "exit=$($r.exit)"

# 3. ACK posted BEFORE reviewed_utc does not count
$r = Run-Case 'c3-ack-before' 103 (Feedback 103 'not-merge-ready') @((Comment 9002 '2026-10-15T08:11:59Z' 'ACK: plan'))
Check 'ack-before-reviewed: still UNANSWERED' (Has $r '^UNANSWERED pr=103 ') $r.text
Check 'ack-before-reviewed: exit 10' ($r.exit -eq 10) "exit=$($r.exit)"

# 4. DISTINGUISH answers
$r = Run-Case 'c4-distinguish' 104 (Feedback 104 'not-merge-ready') @((Comment 9003 '2026-10-15T10:00:00Z' "  `nDISTINGUISH: the ask targets a different head"))
Check 'distinguish-answers: line' (Has $r '^ANSWERED pr=104 by=9003 .* kind=DISTINGUISH( ask_src=header)?$') $r.text
Check 'distinguish-answers: exit 0' ($r.exit -eq 0) "exit=$($r.exit)"

# 5. merge-when-ci-green with no ask text = no ask (and no gh/comment data is needed)
$r = Run-Case 'c5-noask' 105 (Feedback 105 'merge-when-ci-green' "Looks good. Nothing further.`n") @() -NoPrData
Check 'green-no-ask: NOASK line' (Has $r '^NOASK pr=105 verdict=merge-when-ci-green$') $r.text
Check 'green-no-ask: exit 0' ($r.exit -eq 0) "exit=$($r.exit)"

# 6. merge-when-ci-green WITH an ACK-request sentence = ask (both observed wordings)
$r = Run-Case 'c6-green-with-ask' 106 (Feedback 106 'merge-when-ci-green' "Factory: please reply on this PR thread within 24h, starting with ACK: <plan or ETA> or DISTINGUISH: <reason>.`n") @()
Check 'green-with-ask-sentence: UNANSWERED' (Has $r '^UNANSWERED pr=106 .* verdict=merge-when-ci-green( ask_src=header)?$') $r.text
Check 'green-with-ask-sentence: exit 10' ($r.exit -eq 10) "exit=$($r.exit)"
$r = Run-Case 'c6b-green-bold' 116 (Feedback 116 'merge-when-ci-green' "**ACK required** before enqueue.`n") @()
Check 'green-with-bold-ACK-required: UNANSWERED' (Has $r '^UNANSWERED pr=116 ') $r.text
$r = Run-Case 'c6c-green-both-words' 117 (Feedback 117 'merge-when-ci-green' "Answer with ack or distinguish within a day.`n") @()
Check 'green-line-with-ACK-and-DISTINGUISH: UNANSWERED' (Has $r '^UNANSWERED pr=117 ') $r.text
$r = Run-Case 'c6d-green-word-boundary' 118 (Feedback 118 'merge-when-ci-green' "Rebuild the package and track the callback.`n") @() -NoPrData
Check 'substring-ack-in-other-words: NOASK' (Has $r '^NOASK pr=118 ') $r.text

# 7. closed / merged PR carrying an ask is scanned like an open one (MERGE-GATE-TIP-LEGS-1: it used to be SKIP, which hid post-merge asks)
$r = Run-Case 'c7-closed' 107 (Feedback 107 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @() 'CLOSED'
Check 'closed-pr unanswered: UNANSWERED row with its own state= field' (Has $r '^UNANSWERED pr=107 head=01234567 ask_utc=2026-10-15T08:12:00Z deadline=2026-10-16T08:12:00Z verdict=not-merge-ready state=CLOSED( ask_src=header)?$') $r.text
Check 'closed-pr unanswered: never a SKIP row' (-not (Has $r '^SKIP ')) $r.text
Check 'closed-pr unanswered: exit 10' ($r.exit -eq 10) "exit=$($r.exit)"
$r = Run-Case 'c7b-merged' 119 (Feedback 119 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @() 'MERGED'
Check 'merged-pr unanswered: UNANSWERED row with state=MERGED' (Has $r '^UNANSWERED pr=119 .* verdict=not-merge-ready state=MERGED( ask_src=header)?$') $r.text
Check 'merged-pr unanswered: exit 10 and counted as unanswered, not skipped' ($r.exit -eq 10 -and (Has $r '^COS_OPEN_ASKS unanswered=1 answered=0 skipped=0 unknown=0$')) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'c7c-merged-answered' 121 (Feedback 121 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @((Comment 9101 '2026-10-15T09:45:16Z' 'ACK: post-merge plan, fix card filed')) 'MERGED'
Check 'merged-pr answered: ANSWERED row with state=MERGED' (Has $r '^ANSWERED pr=121 by=9101 2026-10-15T09:45:16Z author=layibabalola kind=ACK state=MERGED( ask_src=header)?$') $r.text
Check 'merged-pr answered: exit 0' ($r.exit -eq 0) "exit=$($r.exit)"
$r = Run-Case 'c7d-merged-ack-before' 122 (Feedback 122 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @((Comment 9102 '2026-10-15T08:11:59Z' 'ACK: plan')) 'MERGED'
Check 'merged-pr ACK before reviewed_utc: still UNANSWERED' (Has $r '^UNANSWERED pr=122 .* state=MERGED( ask_src=header)?$') $r.text
$r = Run-Case 'c7e-closed-noask' 123 (Feedback 123 'merge-when-ci-green' "Fine.`n") @() 'CLOSED' -NoPrData
Check 'closed-pr with no ask: NOASK (no state read needed)' (Has $r '^NOASK pr=123 verdict=merge-when-ci-green$') $r.text
$r = Run-Case 'c7f-open-row-shape' 124 (Feedback 124 'not-merge-ready') @((Comment 9103 '2026-10-15T09:45:16Z' 'DISTINGUISH: different head')) 'OPEN'
Check 'open-pr rows carry no state= field (OPEN row shape unchanged)' ((Has $r '^ANSWERED pr=124 by=9103 2026-10-15T09:45:16Z author=layibabalola kind=DISTINGUISH( ask_src=header)?$') -and -not (Has $r 'state=')) $r.text
$r = Run-Case 'c7g-weird-state' 125 (Feedback 125 'not-merge-ready') @() 'DRAFT'
Check 'unrecognised PR state: UNKNOWN, exit 20' ((Has $r '^UNKNOWN pr=125 reason=pr-state-unrecognised state=DRAFT$') -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"

# 7b. MERGE-GATE-TIP-LEGS-1 r2: on a CLOSED / MERGED PR the verdict alone is not an ask; only an explicit ACK request is
$r = Run-Case 'r2a-closed-verdict-only' 130 (Feedback 130 'not-merge-ready') @() 'CLOSED'
CheckRed 'closed-pr, verdict not-merge-ready, no ACK request in the text: NOASK row with its state, exit 0' ((Has $r '^NOASK pr=130 verdict=not-merge-ready state=CLOSED$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'r2b-merged-verdict-merged' 131 (Feedback 131 'merged' "Merged at 17:51Z.`n") @() 'MERGED'
CheckRed 'merged-pr, verdict "merged": NOASK (the 14:28Z false positives on #423 #425)' ((Has $r '^NOASK pr=131 verdict=merged state=MERGED$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'r2c-merged-merge-ready' 132 (Feedback 132 'merge-ready') @() 'MERGED'
CheckRed 'merged-pr, verdict "merge-ready": NOASK (#439 #440)' (Has $r '^NOASK pr=132 verdict=merge-ready state=MERGED$') $r.text
$r = Run-Case 'r2c2-closed-info' 133 (Feedback 133 'info') @() 'CLOSED'
CheckRed 'closed-pr, verdict "info": NOASK (#445)' (Has $r '^NOASK pr=133 verdict=info state=CLOSED$') $r.text
$r = Run-Case 'r2d-merged-loose-only' 134 (Feedback 134 'merge-when-ci-green' "Answer with ack or distinguish within a day.`n") @() 'MERGED'
CheckRed 'merged-pr: a line holding ACK and DISTINGUISH but no explicit ACK request is NOASK (on an OPEN PR it still asks: row c6c)' (Has $r '^NOASK pr=134 verdict=merge-when-ci-green state=MERGED$') $r.text
$r = Run-Case 'r2e-merged-explicit-info' 135 (Feedback 135 'info' "Please note: ACK required before the next merge.`n") @() 'MERGED'
Check 'merged-pr with verdict info BUT an explicit ACK request in the text: still UNANSWERED' ((Has $r '^UNANSWERED pr=135 .* verdict=info state=MERGED( ask_src=header)?$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'r2f-open-placeholder' 136 (Feedback 136 'merge-when-ci-green' "Factory ask: Reply ACK: <owner-decision link or revert plan>.`n") @()
CheckRed 'ACK: <any placeholder> is an explicit ask, not only "ACK: <plan" (the #409 wording)' (Has $r '^UNANSWERED pr=136 ') $r.text
$r = Run-Case 'r2g-prose-colon' 137 (Feedback 137 'merge-when-ci-green' "An ACK: line was posted earlier by the hub.`n") @() -NoPrData
Check 'prose "ACK: line" with no placeholder is no ask' (Has $r '^NOASK pr=137 ') $r.text

# 7c. MERGE-GATE-TIP-LEGS-1 r2 (fable r1 MG-09): a bus file with no reviewed_utc takes its bus commit time as the ask time
function Run-Dir([string]$Name, [hashtable]$Files) {
    $dir = Join-Path $WorkRoot $Name
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    foreach ($k in $Files.Keys) { [IO.File]::WriteAllText((Join-Path $dir $k), [string]$Files[$k], $utf8) }
    return Run-Scan -Dir $dir -Name $Name
}
$noWhen = { param([int]$N) (Feedback $N 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") -replace '(?m)^reviewed_utc: .*\r?\n', '' }
$r = Run-Dir 'r2h-no-reviewed-utc' @{ 'pr-140.md' = (& $noWhen 140); '140.state.txt' = 'OPEN'; '140.comments.json' = '[]'; '140.committed.txt' = '2026-10-14T12:00:00Z' }
CheckRed 'no reviewed_utc, bus commit time known: UNANSWERED with ask_utc = the commit time and ask_src=bus-commit' ((Has $r '^UNANSWERED pr=140 head=01234567 ask_utc=2026-10-14T12:00:00Z deadline=2026-10-15T12:00:00Z verdict=not-merge-ready ask_src=bus-commit$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Dir 'r2i-no-reviewed-utc-answered' @{ 'pr-141.md' = (& $noWhen 141); '141.state.txt' = 'OPEN'; '141.committed.txt' = '2026-10-14T12:00:00Z'
    '141.comments.json' = (ConvertTo-Json -InputObject @((Comment 9201 '2026-10-14T13:00:00Z' 'ACK: plan')) -Depth 6) }
CheckRed 'no reviewed_utc: an ACK after the bus commit time answers it' ((Has $r '^ANSWERED pr=141 by=9201 ') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Dir 'r2j-no-reviewed-utc-early-ack' @{ 'pr-142.md' = (& $noWhen 142); '142.state.txt' = 'OPEN'; '142.committed.txt' = '2026-10-14T12:00:00Z'
    '142.comments.json' = (ConvertTo-Json -InputObject @((Comment 9202 '2026-10-14T11:00:00Z' 'ACK: plan')) -Depth 6) }
CheckRed 'no reviewed_utc: an ACK BEFORE the bus commit time does not answer it' (Has $r '^UNANSWERED pr=142 .* ask_src=bus-commit$') $r.text
$r = Run-Dir 'r2k-no-reviewed-utc-no-commit' @{ 'pr-143.md' = (& $noWhen 143); '143.state.txt' = 'OPEN'; '143.comments.json' = '[]' }
Check 'no reviewed_utc and no commit time: still UNKNOWN header-missing-reviewed_utc (r3: with header_pr=143), exit 20 (unknown is never none)' ((Has $r '^UNKNOWN pr=143 reason=header-missing-reviewed_utc header_pr=143$') -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"
$r = Run-Dir 'r2l-bad-reviewed-utc-with-commit' @{ 'pr-144.md' = (Feedback 144 'not-merge-ready' -BadHeader); '144.state.txt' = 'OPEN'; '144.comments.json' = '[]'; '144.committed.txt' = '2026-10-14T12:00:00Z' }
Check 'a PRESENT but unparsable reviewed_utc stays UNKNOWN (r3: with header_pr=144) even when a commit time exists' ((Has $r '^UNKNOWN pr=144 reason=header-unparsable-reviewed_utc header_pr=144$') -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"

# 7d. MERGE-GATE-TIP-LEGS-1 r2: the eleven bus files the hub ledger listed as UNANSWERED at 14:28:29Z (verbatim copies from the bus, cos-scan-fixtures-bus\; states as on GitHub that day).
# Eight carry no ACK request in their text and are false positives; #409 and #428 carry a real ask, #446 quotes the rule 'CoS asks for ACK: <plan or ETA>' (a text match the heuristic cannot tell from an ask).
$busFx = Join-Path $PSScriptRoot 'cos-scan-fixtures-bus'
$fpDir = Join-Path $WorkRoot 'r2-bus-fixtures'
New-Item -ItemType Directory -Path $fpDir -Force | Out-Null
$fpState = [ordered]@{ 205 = 'CLOSED'; 206 = 'CLOSED'; 409 = 'MERGED'; 413 = 'CLOSED'; 423 = 'MERGED'; 425 = 'MERGED'; 428 = 'MERGED'; 439 = 'MERGED'; 440 = 'MERGED'; 445 = 'CLOSED'; 446 = 'MERGED' }
# an [ordered] table indexes an integer key by POSITION, so rebuild it as a plain hashtable keyed by the PR number
$fpTmp = $fpState; $fpState = @{}; foreach ($ent in $fpTmp.GetEnumerator()) { $fpState[[int]$ent.Key] = $ent.Value }
$fpHave = $true
foreach ($n in @($fpState.Keys | Sort-Object)) {
    $src = Join-Path $busFx "pr-$n.md"
    if (-not (Test-Path -LiteralPath $src)) { $fpHave = $false; Check "bus fixture pr-$n.md present" $false "$src missing"; continue }
    Copy-Item -LiteralPath $src -Destination (Join-Path $fpDir "pr-$n.md") -Force
    [IO.File]::WriteAllText((Join-Path $fpDir "$n.state.txt"), $fpState[$n], $utf8)
    [IO.File]::WriteAllText((Join-Path $fpDir "$n.comments.json"), '[]', $utf8)
}
if ($fpHave) {
    $r = Run-Scan -Dir $fpDir -Name 'r2-bus'
    foreach ($n in 205, 206, 413, 423, 425, 439, 440, 445) {
        CheckRed "bus pr-$n ($($fpState[$n])): no ACK request in its text, so NOASK (was a false UNANSWERED)" (Has $r "^NOASK pr=$n verdict=\S+ state=$($fpState[$n])$") $r.text
    }
    foreach ($n in 409, 428, 446) {
        Check "bus pr-$n ($($fpState[$n])): its text carries an ACK request, so it stays UNANSWERED with state=$($fpState[$n])" (Has $r "^UNANSWERED pr=$n .* state=$($fpState[$n])( ask_src=header)?$") $r.text
    }
    CheckRed 'bus fixtures: exactly the three rows with an ACK request remain (unanswered=3), nothing UNKNOWN, exit 10' (($r.exit -eq 10) -and (Has $r '^COS_OPEN_ASKS unanswered=3 answered=0 skipped=0 unknown=0$')) "exit=$($r.exit) $($r.text)"
}

# 8. unparsable header = UNKNOWN, exit 20
$r = Run-Case 'c8-badheader' 108 (Feedback 108 'not-merge-ready' -BadHeader) @() -NoPrData
Check 'unparsable-header: UNKNOWN line (r3: carries header_pr=108)' (Has $r '^UNKNOWN pr=108 reason=header-unparsable-reviewed_utc header_pr=108$') $r.text
Check 'unparsable-header: exit 20' ($r.exit -eq 20) "exit=$($r.exit)"
Check 'unparsable-header: final unknown=1' (Has $r 'unknown=1$') $r.text
$r = Run-Case 'c8b-noheader' 120 "Just prose, no header at all.`n" @() -NoPrData
Check 'no-header: exit 20' ($r.exit -eq 20) "exit=$($r.exit)"

# 9. edge cases
$r = Run-Case 'c9-at-boundary' 109 (Feedback 109 'not-merge-ready') @((Comment 9004 $reviewed 'ACK: plan'))
Check 'comment-at-exact-reviewed_utc: does NOT count any more (sol gate key r8 SG-09: an ACK must be strictly newer than the ask, for every ask source)' (Has $r '^UNANSWERED pr=109 ') $r.text
$r = Run-Case 'c9b-one-second-after' 110 (Feedback 110 'not-merge-ready') @((Comment 9005 '2026-10-15T08:12:01Z' 'ACK: plan'))   # $reviewed + 1 s
Check 'comment one second after reviewed_utc: counts (SG-09 boundary, the strict side)' (Has $r '^ANSWERED pr=110 by=9005 ') $r.text
$r = Run-Case 'c9b-mid-sentence' 110 (Feedback 110 'not-merge-ready') @((Comment 9005 '2026-10-15T09:00:00Z' 'I will send an ACK: later'))
Check 'ACK-not-at-start-of-body: does not count' (Has $r '^UNANSWERED pr=110 ') $r.text
$r = Run-Case 'c9c-other-author' 111 (Feedback 111 'not-merge-ready') @((Comment 9006 '2026-10-15T09:00:00Z' 'ACK: plan' 'someone-else'))
Check 'r3 CONTROL (was "any-author-accepted-and-recorded" in r2): an ACK: from another account does NOT answer (owner-only, MG-R2-05)' ((Has $r '^UNANSWERED pr=111 ') -and -not (Has $r '^ANSWERED pr=111 ')) $r.text
$r = Run-Case 'c9d-gh-shape' 112 (Feedback 112 'not-merge-ready') @([ordered]@{ author = [ordered]@{ login = 'layibabalola' }; createdAt = '2026-10-15T09:00:00Z'; url = 'https://github.com/x/y/pull/112#issuecomment-777'; body = 'ACK: plan' })
Check 'gh-pr-view-comment-shape: id from url' (Has $r '^ANSWERED pr=112 by=777 ') $r.text
$r = Run-Case 'c9e-repo-mismatch' 113 ((Feedback 113 'not-merge-ready').Replace("repo: $repo", 'repo: someone/else')) @()
Check 'header-repo-mismatch: UNKNOWN' (Has $r '^UNKNOWN pr=113 reason=header-repo-mismatch ') $r.text
$r = Run-Case 'c9f-missing-prdata' 114 (Feedback 114 'not-merge-ready') @() -NoPrData
Check 'ask-without-comment-data (gh blind): UNKNOWN, never none' ((Has $r '^UNKNOWN pr=114 ') -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"

# 10. mixed folder: UNANSWERED + ANSWERED + SKIP + NOASK + UNKNOWN -> counts and precedence
$mix = Join-Path $WorkRoot 'c10-mixed'
New-Item -ItemType Directory -Path $mix -Force | Out-Null
$set = @(
    @{ n = 201; fb = (Feedback 201 'not-merge-ready'); st = 'OPEN'; cm = @() },
    @{ n = 202; fb = (Feedback 202 'not-merge-ready'); st = 'OPEN'; cm = @((Comment 1 '2026-10-15T09:00:00Z' 'ACK: x')) },
    @{ n = 203; fb = (Feedback 203 'not-merge-ready' "Reply ACK: <plan or ETA>.`n"); st = 'MERGED'; cm = @() },
    @{ n = 204; fb = (Feedback 204 'merge-when-ci-green'); st = $null; cm = $null },
    @{ n = 205; fb = (Feedback 205 'not-merge-ready' -BadHeader); st = $null; cm = $null }
)
foreach ($s in $set) {
    [IO.File]::WriteAllText((Join-Path $mix "pr-$($s.n).md"), $s.fb, $utf8)
    if ($null -ne $s.st) {
        [IO.File]::WriteAllText((Join-Path $mix "$($s.n).state.txt"), $s.st, $utf8)
        [IO.File]::WriteAllText((Join-Path $mix "$($s.n).comments.json"), (ConvertTo-Json -InputObject @($s.cm) -Depth 6), $utf8)
    }
}
$r = Run-Scan -Dir $mix -Name 'c10-mixed'
Check 'mixed: final counts line (203 MERGED with an ask is now UNANSWERED, so unanswered=2 skipped=0)' (Has $r '^COS_OPEN_ASKS unanswered=2 answered=1 skipped=0 unknown=1$') $r.text
Check 'mixed: the MERGED PR has its own UNANSWERED row' (Has $r '^UNANSWERED pr=203 .* state=MERGED( ask_src=header)?$') $r.text
Check 'mixed: UNKNOWN beats UNANSWERED in the exit code (20)' ($r.exit -eq 20) "exit=$($r.exit)"
Check 'mixed: UNANSWERED line still printed' (Has $r '^UNANSWERED pr=201 ') $r.text
Check 'mixed: lines sorted by PR number' ((($r.lines | Where-Object { $_ -match 'pr=(\d+)' } | ForEach-Object { [int]([regex]::Match($_, 'pr=(\d+)').Groups[1].Value) }) -join ',') -eq '201,202,203,204,205') $r.text

# 11. OutFile is a self-dating markdown view
$of = $r.outFile
$md = if (Test-Path -LiteralPath $of) { [IO.File]::ReadAllText($of) } else { '' }
Check 'outfile: self-dating header' ($md -match '(?m)^generated_utc: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$') $md
Check 'outfile: carries the final counts line' ($md -match 'COS_OPEN_ASKS unanswered=2 answered=1 skipped=0 unknown=1') $md
Check 'outfile: records exit 20' ($md -match '(?m)^exit: 20 ') $md

# 12. empty fixture folder and missing fixture folder are UNKNOWN, never clean
$empty = Join-Path $WorkRoot 'c12-empty'
New-Item -ItemType Directory -Path $empty -Force | Out-Null
$r = Run-Scan -Dir $empty -Name 'c12-empty'
Check 'no-feedback-files: UNKNOWN exit 20' ($r.exit -eq 20 -and (Has $r 'reason=no-feedback-files')) "exit=$($r.exit) $($r.text)"
$r = Run-Scan -Dir (Join-Path $WorkRoot 'does-not-exist') -Name 'c12-missing'
Check 'fixture-dir-missing: UNKNOWN exit 20' ($r.exit -eq 20) "exit=$($r.exit) $($r.text)"

# 13. git path (no FixtureDir): throwaway bus repo, read with git show / ls-tree only
$bus = Join-Path $WorkRoot 'bus'
New-Item -ItemType Directory -Path (Join-Path $bus 'cos-feedback\mlv-app') -Force | Out-Null
& git -C $bus init -q -b master 2>&1 | Out-Null
[IO.File]::WriteAllText((Join-Path $bus 'cos-feedback\mlv-app\pr-301.md'), (Feedback 301 'merge-when-ci-green' "All good.`n"), $utf8)
& git -C $bus add cos-feedback 2>&1 | Out-Null
& git -C $bus -c user.name=t -c user.email=t@example.invalid commit -q -m 'fixture' 2>&1 | Out-Null
$of = Join-Path $WorkRoot 'c13.out.md'
# r1: the git path asks GitHub for the PR state list; the tests never do, so the r1 scanner is handed an empty list through its -PrStateFile seam (the pre-r1 scanner has no such parameter and is run without it)
$emptyList = Join-Path $WorkRoot 'pr-state-empty.json'
# r2 (HUB-TOOLS-TRACKED-1): an EMPTY list left PR 301 / 303 unnamed, so the scanner read them live with `gh pr view`. On a machine whose gh reaches GitHub they are real, merged PRs of this repo
# (state=MERGED); on a hosted runner without a usable token the read failed (UNKNOWN reason=gh-failed, exit 20) and two rows went red. The list now names both as MERGED, so the git path never calls gh.
# was: [IO.File]::WriteAllText($emptyList, '[]', $utf8)
[IO.File]::WriteAllText($emptyList, '[{"number":301,"state":"MERGED"},{"number":303,"state":"MERGED"}]', $utf8)
$gx = if (Select-String -LiteralPath $Script -Pattern 'PrStateFile' -Quiet) { @('-PrStateFile', $emptyList) } else { @() }
$lines = & pwsh -NoProfile -NonInteractive -File $Script -BusRoot $bus -Ref master -OutFile $of -Repo $repo @gx 2>&1
$code = $LASTEXITCODE
$txt = (@($lines) -join "`n")
Check 'git-path: NOASK from git show' ($txt -match '(?m)^NOASK pr=301 verdict=merge-when-ci-green state=MERGED$' <# SG-16: a MERGED NOASK row carries its state #> -and $code -eq 0) "exit=$code $txt"
Check 'git-path: SCAN line carries bus commit' ($txt -match '(?m)^SCAN bus_ref=master bus_commit=[0-9a-f]{8} ') $txt
$lines = & pwsh -NoProfile -NonInteractive -File $Script -BusRoot $bus -Ref no-such-ref -OutFile $of -Repo $repo 2>&1
$code = $LASTEXITCODE
$txt = (@($lines) -join "`n")
Check 'git-path: missing ref is UNKNOWN exit 20' ($code -eq 20 -and $txt -match 'reason=bus-ref-missing') "exit=$code $txt"
$lines = & pwsh -NoProfile -NonInteractive -File $Script -BusRoot (Join-Path $WorkRoot 'no-such-bus') -Ref master -OutFile $of -Repo $repo 2>&1
Check 'git-path: missing bus clone is UNKNOWN exit 20' ($LASTEXITCODE -eq 20) "exit=$LASTEXITCODE"
# MERGE-GATE-TIP-LEGS-1 r2 (fable r1 MG-09), git path: a file with no reviewed_utc is read (its bus commit time stands in) instead of UNKNOWN header-missing-reviewed_utc
[IO.File]::WriteAllText((Join-Path $bus 'cos-feedback\mlv-app\pr-303.md'), ((Feedback 303 'merge-when-ci-green' "All good.`n") -replace '(?m)^reviewed_utc: .*\r?\n', ''), $utf8)
& git -C $bus add cos-feedback 2>&1 | Out-Null
& git -C $bus -c user.name=t -c user.email=t@example.invalid commit -q -m 'fixture no reviewed_utc' 2>&1 | Out-Null
$lines = & pwsh -NoProfile -NonInteractive -File $Script -BusRoot $bus -Ref master -OutFile $of -Repo $repo @gx 2>&1
$code = $LASTEXITCODE
$txt = (@($lines) -join "`n")
CheckRed 'git-path: a bus file with no reviewed_utc is classified (NOASK) using its bus commit time, not UNKNOWN' ($txt -match '(?m)^NOASK pr=303 verdict=merge-when-ci-green state=MERGED$' <# SG-16: a MERGED NOASK row carries its state #> -and $txt -notmatch 'header-missing-reviewed_utc' -and $code -eq 0) "exit=$code $txt"

# 14. MERGE-GATE-TIP-LEGS-1 r3 (sol r2 MG-R2-04 / MG-R2-05, fable r2 MG-R2-05 / MG-R2-06)
# 14a. header_pr is read BEFORE any other parse check and written on every UNKNOWN row whose header names a PR
$hdrRaw = { param([string]$Pr, [string]$Rev, [string]$Verdict) "schema: cos-feedback.v1`nproject: mlv-app`nrepo: $repo`npr: $Pr`nhead: 0123456789abcdef0123456789abcdef01234567`n$Rev$Verdict`n# CoS feedback`n`nReply ACK: <plan or ETA>.`n" }
$r = Run-Dir 'r3a-unparsable-misnamed' @{ 'pr-72.md' = (& $hdrRaw '437' "reviewed_utc: invalid-time`n" "event: pr-updated`nverdict: not-merge-ready`n") }
CheckR3 'MG-R2-04 sol''s repro: file pr-72.md, header pr 437, unparsable reviewed_utc: the UNKNOWN row carries header_pr=437' ((Has $r '^UNKNOWN pr=72 reason=header-unparsable-reviewed_utc header_pr=437$') -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"
$r = Run-Dir 'r3b-missing-verdict-misnamed' @{ 'pr-72.md' = (& $hdrRaw '437' "reviewed_utc: 2026-10-16T12:34:46Z`n" "event: pr-updated`n") }
CheckR3 'MG-R2-04 fable''s repro: file pr-72.md, header pr 437, no verdict line: header_pr=437' (Has $r '^UNKNOWN pr=72 reason=header-missing-verdict header_pr=437$') $r.text
$r = Run-Dir 'r3c-missing-reviewed-misnamed' @{ 'pr-72.md' = (& $hdrRaw '437' '' "event: pr-updated`nverdict: not-merge-ready`n") }
CheckR3 'MG-R2-04 no reviewed_utc and no commit time, header pr 437: header_pr=437' (Has $r '^UNKNOWN pr=72 reason=header-missing-reviewed_utc header_pr=437$') $r.text
$r = Run-Dir 'r3d-mismatch' @{ 'pr-72.md' = (& $hdrRaw '#437' "reviewed_utc: 2026-10-16T12:34:46Z`n" "event: pr-updated`nverdict: not-merge-ready`n"); '72.state.txt' = 'OPEN'; '72.comments.json' = '[]' }
CheckR3 'MG-R2-04 header-pr-mismatch row: header_pr is the bare number (#437 -> 437)' (Has $r '^UNKNOWN pr=72 reason=header-pr-mismatch header_pr=437$') $r.text
$r = Run-Dir 'r3e-repo-mismatch' @{ 'pr-72.md' = ((& $hdrRaw '72' "reviewed_utc: 2026-10-16T12:34:46Z`n" "event: pr-updated`nverdict: not-merge-ready`n").Replace("repo: $repo", 'repo: someone/else')); '72.state.txt' = 'OPEN'; '72.comments.json' = '[]' }
CheckR3 'MG-R2-04 header-repo-mismatch row (header pr equals the file pr, so the pr check passes and the repo check fires) also carries header_pr=72' (Has $r '^UNKNOWN pr=72 reason=header-repo-mismatch header_repo=someone/else scan_repo=\S+ header_pr=72$') $r.text
$r = Run-Dir 'r3f-no-header' @{ 'pr-72.md' = "Just prose, no header at all.`n" }
Check 'MG-R2-04 control: a file with no header has nothing to carry (no header_pr)' ((Has $r '^UNKNOWN pr=72 reason=no-header$') -and -not (Has $r 'header_pr')) $r.text
# 14b. only the owner account can answer
$r = Run-Case 'r3g-outsider-ack' 150 (Feedback 150 'not-merge-ready') @((Comment 9301 '2026-10-15T09:00:00Z' 'ACK: whatever' 'outside-contributor'))
CheckR3 'MG-R2-05 an outside contributor''s ACK: leaves the ask UNANSWERED (exit 10)' ((Has $r '^UNANSWERED pr=150 ') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'r3h-outsider-then-owner' 151 (Feedback 151 'not-merge-ready') @((Comment 9302 '2026-10-15T09:00:00Z' 'ACK: whatever' 'outside-contributor'), (Comment 9303 '2026-10-15T10:00:00Z' 'ACK: plan, ETA tomorrow'))
CheckR3 'MG-R2-05 an outsider''s ACK first and the owner''s ACK later: ANSWERED by the owner''s comment' ((Has $r '^ANSWERED pr=151 by=9303 2026-10-15T10:00:00Z author=layibabalola kind=ACK( ask_src=header)?$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'r3i-outsider-distinguish' 152 (Feedback 152 'not-merge-ready') @((Comment 9304 '2026-10-15T09:00:00Z' 'DISTINGUISH: not our head' 'outside-contributor'))
CheckR3 'MG-R2-05 an outside contributor''s DISTINGUISH: does not answer either' (Has $r '^UNANSWERED pr=152 ') $r.text
$r = Run-Case 'r3j-owner-param' 153 (Feedback 153 'not-merge-ready') @((Comment 9305 '2026-10-15T09:00:00Z' 'ACK: plan' 'hub-bot')) 'OPEN'
$r2 = Run-Scan -Dir (Join-Path $WorkRoot 'r3j-owner-param') -Name 'r3j2' -Extra @('-OwnerLogin', 'hub-bot')
CheckR3 'MG-R2-05 -OwnerLogin names the answering account (default layibabalola; hub-bot answers only when named)' ((Has $r '^UNANSWERED pr=153 ') -and (Has $r2 '^ANSWERED pr=153 by=9305 .* author=hub-bot ')) "$($r.text) // $($r2.text)"
$r = Run-Case 'r3k-owner-case' 154 (Feedback 154 'not-merge-ready') @((Comment 9306 '2026-10-15T09:00:00Z' 'ACK: plan' 'LayiBabalola'))
Check 'MG-R2-05 control: the owner login compares case-insensitively (GitHub logins are)' (Has $r '^ANSWERED pr=154 by=9306 ') $r.text

# 15. COS-SCAN-LATEST-ASK-1 r1: the NEWEST ask counts (stamped bus section, owner-account PR comment), a body/comment ask counts under a merge-when-ci-green header, legacy repo names are scanned
# Shape a: a later ask in the same bus file (a stamped section) beats an older ACK; shape b: so does a later owner-account PR comment
$sec463 = "## Notes`n- ACK ask posted: https://example.invalid/pull/160#issuecomment-1 (requests ACK: <plan or ETA>).`n`n## Iverson single-model verification (2026-10-16T15:49Z, head 48d3f79b)`n`nPlease derive the label from the landed outcome.`n`nACK: <plan or ETA> requested within 24h. -- Chief of Staff`n"
$r = Run-Case 'r1a1-463-shape' 160 (Feedback 160 'not-merge-ready' $sec463 '2026-10-16T12:12:00Z') @((Comment 9401 '2026-10-16T13:47:00Z' 'ACK: plan, ETA tomorrow')) 'OPEN'
CheckR1 'a1 the #463 shape (header 12:12Z, ACK 13:47Z, stamped Iverson ask 15:49Z): UNANSWERED with ask_utc = the section stamp and ask_src=section' ((Has $r '^UNANSWERED pr=160 head=01234567 ask_utc=2026-10-16T15:49:00Z deadline=2026-10-17T15:49:00Z verdict=not-merge-ready ask_src=section$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)" -Shape a
$r = Run-Case 'r1a2-463-answered-later' 161 (Feedback 161 'not-merge-ready' $sec463 '2026-10-16T12:12:00Z') @((Comment 9401 '2026-10-16T13:47:00Z' 'ACK: plan, ETA tomorrow'), (Comment 9402 '2026-10-16T16:00:00Z' 'ACK: label derivation lands in r4')) 'OPEN'
CheckR1 'a2 an ACK newer than the section ask answers it (ask_src=section on the ANSWERED row)' ((Has $r '^ANSWERED pr=161 by=9402 2026-10-16T16:00:00Z author=layibabalola kind=ACK ask_src=section$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape a
$r = Run-Case 'r1a3-stamped-no-ask' 162 (Feedback 162 'not-merge-ready' "## Update (2026-10-16T15:49Z)`n`nThanks, rebased.`n" '2026-10-16T12:12:00Z') @((Comment 9403 '2026-10-16T13:47:00Z' 'ACK: plan')) 'OPEN'
CheckR1 'a3 control: a stamped section that holds no ACK request does not move the ask time (ANSWERED, ask_src=header)' ((Has $r '^ANSWERED pr=162 by=9403 .* kind=ACK ask_src=header$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape a
$r = Run-Case 'r1a4-nested-heading' 163 (Feedback 163 'not-merge-ready' "## Round 2 (2026-10-16T15:49Z)`n`n### Detail`n`nACK: <plan or ETA> requested.`n" '2026-10-16T12:12:00Z') @((Comment 9404 '2026-10-16T13:47:00Z' 'ACK: plan')) 'OPEN'
CheckR1 'a4 the ask sits in a ### sub-section of a stamped ## section: the stamp of the enclosing section applies' (Has $r '^UNANSWERED pr=163 .* ask_utc=2026-10-16T15:49:00Z .* ask_src=section$') $r.text -Shape a
$r = Run-Case 'r1a5-merged-loose-section' 164 (Feedback 164 'merged' "Reply ACK: <plan or ETA>.`n`n## Late note (2026-10-16T15:49Z)`n`nAnswer with ack or distinguish within a day.`n" '2026-10-16T12:12:00Z') @((Comment 9405 '2026-10-16T13:47:00Z' 'ACK: plan')) 'MERGED'
CheckR1 'a5 MERGED PR: a stamped section holding only the loose "ack ... distinguish" line is not an ask (-ExplicitOnly), so the 13:47Z ACK still answers' ((Has $r '^ANSWERED pr=164 by=9405 .* kind=ACK state=MERGED ask_src=header$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape a
$ownAsk = 'Chief of Staff follow-up: please reply with ACK: <plan or ETA> on the new Blocker.'
$r = Run-Case 'r1b1-comment-ask' 165 (Feedback 165 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @((Comment 9411 '2026-10-15T09:00:00Z' 'ACK: plan'), (Comment 9412 '2026-10-15T10:00:00Z' $ownAsk)) 'OPEN'
CheckR1 'b1 an owner-account comment asking for an ACK at T3, after an ACK at T2: UNANSWERED, ask_utc = T3, ask_src=comment' ((Has $r '^UNANSWERED pr=165 head=01234567 ask_utc=2026-10-15T10:00:00Z deadline=2026-10-16T10:00:00Z verdict=not-merge-ready ask_src=comment$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)" -Shape b
$r = Run-Case 'r1b2-comment-ask-answered' 166 (Feedback 166 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @((Comment 9411 '2026-10-15T09:00:00Z' 'ACK: plan'), (Comment 9412 '2026-10-15T10:00:00Z' $ownAsk), (Comment 9413 '2026-10-15T11:00:00Z' 'ACK: new Blocker fixed in r2')) 'OPEN'
CheckR1 'b2 an ACK newer than the comment ask answers it (ask_src=comment)' ((Has $r '^ANSWERED pr=166 by=9413 2026-10-15T11:00:00Z author=layibabalola kind=ACK ask_src=comment$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape b
$r = Run-Case 'r1b3-ack-quotes-ask' 167 (Feedback 167 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @((Comment 9421 '2026-10-15T10:00:00Z' $ownAsk), (Comment 9422 '2026-10-15T10:30:00Z' 'ACK: plan. The CoS wrote "ACK required" and "ACK: <plan or ETA>" above.')) 'OPEN'
CheckR1 'b3 an owner comment that itself starts with ACK: is never an ask even when it quotes "ACK required": it answers the T3 ask' ((Has $r '^ANSWERED pr=167 by=9422 .* kind=ACK ask_src=comment$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape b
$r = Run-Case 'r1b4-outsider-ask' 168 (Feedback 168 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @((Comment 9431 '2026-10-15T09:00:00Z' 'ACK: plan'), (Comment 9432 '2026-10-15T10:00:00Z' 'ACK required before merge' 'outside-contributor')) 'OPEN'
CheckR1 'b4 an ACK request from a non-owner account is not an ask: the 09:00 ACK still answers (ask_src=header)' ((Has $r '^ANSWERED pr=168 by=9431 .* kind=ACK ask_src=header$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape b
$r = Run-Case 'r1b5-comment-ask-before-header' 169 (Feedback 169 'not-merge-ready' "Reply ACK: <plan or ETA>.`n") @((Comment 9441 '2026-10-15T07:00:00Z' $ownAsk), (Comment 9442 '2026-10-15T09:00:00Z' 'ACK: plan')) 'OPEN'
CheckR1 'b5 a comment ask OLDER than the bus header does not move the ask time (ask_src=header)' ((Has $r '^ANSWERED pr=169 by=9442 .* kind=ACK ask_src=header$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape b
# Shape c: the #466 shape - merge-when-ci-green header, but an ACK request in the body or in an owner comment, on an OPEN PR
$r = Run-Case 'r1c1-green-comment-ask' 170 (Feedback 170 'merge-when-ci-green' "Looks good.`n" '2026-10-16T16:00:00Z') @((Comment 6220773118 '2026-10-16T17:13:00Z' '**ACK required** on the two Improvements before enqueue.')) 'OPEN'
CheckR1 'c1 merge-when-ci-green header, no ask in the bus text, an owner comment at 17:13Z asks for an ACK: UNANSWERED ask_src=comment' ((Has $r '^UNANSWERED pr=170 head=01234567 ask_utc=2026-10-16T17:13:00Z deadline=2026-10-17T17:13:00Z verdict=merge-when-ci-green ask_src=comment$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)" -Shape c
$r = Run-Case 'r1c2-green-body-ask' 171 (Feedback 171 'merge-when-ci-green' "Two Improvements. **ACK required** before enqueue.`n" '2026-10-16T16:00:00Z') @() 'OPEN'
CheckR1 'c2 merge-when-ci-green header, ACK request in the bus text, open PR: UNANSWERED ask_src=header' ((Has $r '^UNANSWERED pr=171 .* verdict=merge-when-ci-green ask_src=header$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)" -Shape c
$r = Run-Case 'r1c3-green-comment-ask-answered' 172 (Feedback 172 'merge-when-ci-green' "Looks good.`n" '2026-10-16T16:00:00Z') @((Comment 6220773118 '2026-10-16T17:13:00Z' 'ACK required on the two Improvements.'), (Comment 6220773119 '2026-10-16T17:40:00Z' 'ACK: both Improvements land in r2')) 'OPEN'
CheckR1 'c3 the same shape, answered by a newer owner ACK: ANSWERED ask_src=comment' ((Has $r '^ANSWERED pr=172 by=6220773119 2026-10-16T17:40:00Z author=layibabalola kind=ACK ask_src=comment$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape c
$r = Run-Case 'r1c4-green-merged-comment-ask' 173 (Feedback 173 'merge-when-ci-green' "Looks good.`n" '2026-10-16T16:00:00Z') @((Comment 6220773118 '2026-10-16T17:13:00Z' 'ACK required on the two Improvements.')) 'MERGED'
Check 'c4 control: the same comment on a MERGED PR is not read (open PRs only): NOASK, exit 0' ((Has $r '^NOASK pr=173 verdict=merge-when-ci-green state=MERGED$' <# SG-15: CLOSED / MERGED NOASK rows carry their state #>) -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'r1c5-green-no-ask-comments' 174 (Feedback 174 'merge-when-ci-green' "Looks good.`n" '2026-10-16T16:00:00Z') @((Comment 9451 '2026-10-16T17:00:00Z' 'Thanks, rebasing now.'), (Comment 9452 '2026-10-16T17:05:00Z' 'ACK required before merge' 'outside-contributor'), (Comment 9453 '2026-10-16T15:00:00Z' 'ACK required (older than the bus header)')) 'OPEN'
# SUPERSEDED (hub 2026-10-17T00:35Z, sol gate key r12 SG-13): a later green header no longer hides an older owner ask; an ask is closed only by an
# ACK / DISTINGUISH strictly newer than it (the response duty), so 9453 now reads UNANSWERED. The outside contributor's 9452 is still not an ask.
Check 'c5 (SG-13 supersedes the old green-header floor) green OPEN PR with an unanswered OWNER ask older than the header: UNANSWERED ask_src=comment, exit 10; the outsider comment is still not the ask' ((Has $r '^UNANSWERED pr=174 .* ask_utc=2026-10-16T15:00:00Z .* ask_src=comment$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
# Shape d: legacy repo name (before the org move) is the same repo
$legacy = { param([string]$Text) $Text.Replace("repo: $repo", 'repo: layibabalola/MLV-App') }
$r = Run-Case 'r1d1-legacy-merged-noask' 180 (& $legacy (Feedback 180 'merged' "Merged.`n")) @() 'MERGED'
CheckR1 'd1 header repo layibabalola/MLV-App on a MERGED PR with no explicit ask: NOASK (not UNKNOWN), exit 0, unknown=0' ((Has $r '^NOASK pr=180 verdict=merged state=MERGED$') -and $r.exit -eq 0 -and (Has $r 'unknown=0$')) "exit=$($r.exit) $($r.text)" -Shape d
$r = Run-Case 'r1d2-legacy-open-ask' 181 (& $legacy (Feedback 181 'not-merge-ready')) @() 'OPEN'
CheckR1 'd2 a legacy-repo row that carries an ask is scanned normally: UNANSWERED, exit 10' ((Has $r '^UNANSWERED pr=181 ') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)" -Shape d
$r = Run-Case 'r1d3-legacy-case' 182 ((Feedback 182 'merged' "Merged.`n").Replace("repo: $repo", 'repo: LayiBabalola/mlv-app')) @() 'MERGED'
CheckR1 'd3 the alias compares case-insensitively' ((Has $r '^NOASK pr=182 ') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)" -Shape d
$r = Run-Case 'r1d4-foreign-repo' 183 ((Feedback 183 'merged' "Merged.`n").Replace("repo: $repo", 'repo: layibabalola/other-project')) @() 'MERGED'
Check 'd4 control: a truly foreign repo header stays UNKNOWN header-repo-mismatch, exit 20' ((Has $r '^UNKNOWN pr=183 reason=header-repo-mismatch ') -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"
$dirD = Join-Path $WorkRoot 'r1d5-alias-param'
$r = Run-Case 'r1d5-alias-param' 184 (& $legacy (Feedback 184 'merged' "Merged.`n")) @() 'MERGED'
$r2 = Run-Scan -Dir $dirD -Name 'r1d5b' -Extra @('-RepoAlias', 'none/none')
$null = Run-Case 'r1d6-alias-named' 185 ((Feedback 185 'merged' "Merged.`n").Replace("repo: $repo", 'repo: someone/else')) @() 'MERGED'
$r3 = Run-Scan -Dir (Join-Path $WorkRoot 'r1d6-alias-named') -Name 'r1d6b' -Extra @('-RepoAlias', 'someone/else')
CheckR1 'd5 -RepoAlias replaces the default list: naming only none/none makes the legacy header UNKNOWN again (exit 20), and naming someone/else makes a someone/else header scan as NOASK (exit 0)' ((Has $r2 '^UNKNOWN pr=184 reason=header-repo-mismatch ') -and $r2.exit -eq 20 -and (Has $r3 '^NOASK pr=185 verdict=merged state=MERGED$') -and $r3.exit -eq 0) "r2 exit=$($r2.exit) $($r2.text) // r3 exit=$($r3.exit) $($r3.text)" -Shape d
# 16. the git path with a PR state list: a non-OPEN PR with no ask is NOASK without any per-PR GitHub call; an OPEN PR is probed for comment asks (here: no gh, so the seam lists it as OPEN and the probe would fail -> UNKNOWN)
if ($gx.Count -gt 0) {
    $stateList = Join-Path $WorkRoot 'pr-state-open-301.json'
    [IO.File]::WriteAllText($stateList, '[{"number":301,"state":"MERGED"},{"number":303,"state":"CLOSED"}]', $utf8)
    $lines = & pwsh -NoProfile -NonInteractive -File $Script -BusRoot $bus -Ref master -OutFile $of -Repo $repo -PrStateFile $stateList 2>&1
    $code = $LASTEXITCODE; $txt = (@($lines) -join "`n")
    CheckR1 'e1 -PrStateFile seam: green PRs listed MERGED / CLOSED are NOASK with no per-PR call, exit 0' (($txt -match '(?m)^NOASK pr=301 verdict=merge-when-ci-green state=(MERGED|CLOSED)$') -and ($txt -match '(?m)^NOASK pr=303 verdict=merge-when-ci-green state=(MERGED|CLOSED)$') -and $code -eq 0) "exit=$code $txt"
    $badList = Join-Path $WorkRoot 'pr-state-bad.json'
    [IO.File]::WriteAllText($badList, 'not json', $utf8)
    $lines = & pwsh -NoProfile -NonInteractive -File $Script -BusRoot $bus -Ref master -OutFile $of -Repo $repo -PrStateFile $badList 2>&1
    $code = $LASTEXITCODE; $txt = (@($lines) -join "`n")
    CheckR1 'e2 an unreadable PR state list is a blind scan: UNKNOWN pr=- reason=pr-state-list-failed, exit 20 (never a clean scan)' (($txt -match '(?m)^UNKNOWN pr=- reason=pr-state-list-failed') -and $code -eq 20) "exit=$code $txt"
} else {
    CheckR1 'e0 the scanner has a -PrStateFile test seam' $false 'cos-open-ask-scan.ps1 has no PrStateFile parameter'
}

# 17. COS-SCAN-UNDATED-ASK-1 r1b (sol SG-08): a follow-up ask appended WITHOUT a stamped heading is dated by the file's NEWEST bus commit (ask_src=bus-commit-latest). Fixture seam: <N>.latest-committed.txt
# (absent = the seam is not exercised, so the rows above are unchanged; present but not a time = the commit time is unreadable -> UNKNOWN; the live path reads `git log -1 --format=%cI <ref> -- <file>` and fails to UNKNOWN too).
$sg08Body = "Reply ACK: <plan or ETA>.`n`n## Follow-up`n`nA new Blocker: ACK required before enqueue.`n"
$sg08Hdr = '2026-10-16T12:00:00Z'
function Run-Latest([string]$Name, [int]$Pr, [string]$Feedback, $Comments, [string]$Latest, [string]$State = 'OPEN') {
    $files = @{ "pr-$Pr.md" = $Feedback; "$Pr.state.txt" = $State; "$Pr.comments.json" = (ConvertTo-Json -InputObject @($Comments) -Depth 6) }
    if ($Latest) { $files["$Pr.latest-committed.txt"] = $Latest }
    return Run-Dir $Name $files
}
$r = Run-Latest 'u1-sg08' 190 (Feedback 190 'not-merge-ready' $sg08Body $sg08Hdr) @((Comment 9501 '2026-10-16T13:00:00Z' 'ACK: plan, ETA tomorrow')) '2026-10-16T15:00:00Z'
CheckU1 'u1 SG-08 (header 12:00Z, owner ACK 13:00Z, newest bus commit 15:00Z carries an unstamped follow-up ask): UNANSWERED, ask_utc = the commit time, ask_src=bus-commit-latest, exit 10' ((Has $r '^UNANSWERED pr=190 head=01234567 ask_utc=2026-10-16T15:00:00Z deadline=2026-10-17T15:00:00Z verdict=not-merge-ready ask_src=bus-commit-latest$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u1b-sg08-answered-later' 195 (Feedback 195 'not-merge-ready' $sg08Body $sg08Hdr) @((Comment 9501 '2026-10-16T13:00:00Z' 'ACK: plan'), (Comment 9502 '2026-10-16T16:00:00Z' 'ACK: follow-up Blocker lands in r2')) '2026-10-16T15:00:00Z'
CheckU1 'u1b an ACK strictly newer than the newest commit answers it (ask_src=bus-commit-latest on the ANSWERED row)' ((Has $r '^ANSWERED pr=195 by=9502 2026-10-16T16:00:00Z author=layibabalola kind=ACK ask_src=bus-commit-latest$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u1c-ack-at-commit-time' 196 (Feedback 196 'not-merge-ready' $sg08Body $sg08Hdr) @((Comment 9503 '2026-10-16T15:00:00Z' 'ACK: plan')) '2026-10-16T15:00:00Z'
CheckU1 'u1c an ACK at exactly the commit time does not answer it (strictly newer, like a section / comment ask)' ((Has $r '^UNANSWERED pr=196 .* ask_src=bus-commit-latest$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u2-commit-before-ack' 191 (Feedback 191 'not-merge-ready' $sg08Body $sg08Hdr) @((Comment 9511 '2026-10-16T13:00:00Z' 'ACK: plan')) '2026-10-16T12:30:00Z'
CheckU1 'u2 newest commit 12:30Z (before the 13:00Z ACK): ANSWERED, exit 0' ((Has $r '^ANSWERED pr=191 by=9511 2026-10-16T13:00:00Z author=layibabalola kind=ACK ask_src=bus-commit-latest$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u3-no-ask-text' 192 (Feedback 192 'not-merge-ready' "Review body, nothing to answer.`n" $sg08Hdr) @((Comment 9521 '2026-10-16T13:00:00Z' 'ACK: plan')) '2026-10-16T15:00:00Z'
CheckU1 'u3 newest commit 15:00Z but the file holds no ACK request text: ANSWERED as before (ask_src=header), no new UNANSWERED' ((Has $r '^ANSWERED pr=192 by=9521 .* kind=ACK ask_src=header$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u3b-green-no-ask-text' 193 (Feedback 193 'merge-when-ci-green' "Looks good.`n" $sg08Hdr) @() '2026-10-16T15:00:00Z'
CheckU1 'u3b green header, no ACK request text, newest commit 15:00Z: NOASK, exit 0' ((Has $r '^NOASK pr=193 verdict=merge-when-ci-green$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u4-unreadable' 194 (Feedback 194 'not-merge-ready' $sg08Body $sg08Hdr) @((Comment 9531 '2026-10-16T13:00:00Z' 'ACK: plan')) 'git-log-failed'
CheckU1 'u4 newest commit time unreadable on a file that holds an ACK request: UNKNOWN reason=latest-commit-time-unreadable (with header_pr=194), exit 20, never ANSWERED' (((Has $r '^UNKNOWN pr=194 reason=latest-commit-time-unreadable header_pr=194$') -and -not (Has $r '^ANSWERED ')) -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u4b-unreadable-no-ask-text' 197 (Feedback 197 'not-merge-ready' "Review body, nothing to answer.`n" $sg08Hdr) @((Comment 9532 '2026-10-16T13:00:00Z' 'ACK: plan')) 'git-log-failed'
CheckU1 'u4b control: an unreadable commit time on a file with no ACK request text is not read at all (ANSWERED as before)' ((Has $r '^ANSWERED pr=197 ') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u5-section-dates-it' 198 (Feedback 198 'not-merge-ready' "## Round 2 (2026-10-16T14:00Z)`n`nACK required before enqueue.`n" $sg08Hdr) @((Comment 9541 '2026-10-16T13:00:00Z' 'ACK: plan')) '2026-10-16T14:00:00Z'
CheckU1 'u5 a stamped section already dates the ask at the commit time (14:00Z): that stamp wins (ask_src=section), no bus-commit-latest' ((Has $r '^UNANSWERED pr=198 .* ask_utc=2026-10-16T14:00:00Z .* ask_src=section$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u6-commit-older-than-header' 199 (Feedback 199 'not-merge-ready' $sg08Body $sg08Hdr) @((Comment 9551 '2026-10-16T13:00:00Z' 'ACK: plan')) '2026-10-16T11:00:00Z'
CheckU1 'u6 a commit time older than the header does not move the ask time (ask_src=header)' ((Has $r '^ANSWERED pr=199 by=9551 .* kind=ACK ask_src=header$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u7-merged-explicit' 200 (Feedback 200 'merged' $sg08Body $sg08Hdr) @((Comment 9561 '2026-10-16T13:00:00Z' 'ACK: plan')) '2026-10-16T15:00:00Z' 'MERGED'
CheckU1 'u7 MERGED PR whose file holds an explicit ACK request and a newer commit: UNANSWERED state=MERGED ask_src=bus-commit-latest' ((Has $r '^UNANSWERED pr=200 .* verdict=merged state=MERGED ask_src=bus-commit-latest$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Latest 'u8-comment-newer-than-commit' 201 (Feedback 201 'not-merge-ready' $sg08Body $sg08Hdr) @((Comment 9571 '2026-10-16T13:00:00Z' 'ACK: plan'), (Comment 9572 '2026-10-16T16:00:00Z' 'Chief of Staff follow-up: please reply with ACK: <plan or ETA> on the new Blocker.')) '2026-10-16T15:00:00Z'
CheckU1 'u8 an owner-comment ask newer than the newest commit still wins (ask_src=comment)' ((Has $r '^UNANSWERED pr=201 .* ask_utc=2026-10-16T16:00:00Z .* ask_src=comment$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"

# 18. COS-SCAN-EDITED-COMMENT-ASK-1 r1 (sol gate key r9 SG-10): an owner comment EDITED to carry a new ACK request is dated by its edit time (updated_at), not its creation time.
# An ANSWER keeps its created time and stays strict, so editing an old ACK can never answer a newer ask. A live comments read that fails or cannot be parsed is UNKNOWN, never ANSWERED.
function CommentU([long]$Id, [string]$Created, [string]$Updated, [string]$Body, [string]$Login = 'layibabalola') {
    return [ordered]@{ id = $Id; user = [ordered]@{ login = $Login }; created_at = $Created; updated_at = $Updated; body = $Body }
}
$sg10Hdr = '2026-10-16T12:00:00Z'
$sg10Fb = "Reply ACK: <plan or ETA>.`n"
$sg10Ask = 'ACK required: new blocker in the r3 keys.'
$r = Run-Case 'v1-sg10' 210 (Feedback 210 'not-merge-ready' $sg10Fb $sg10Hdr) @((CommentU 9601 '2026-10-16T11:00:00Z' '2026-10-16T15:00:00Z' $sg10Ask), (Comment 9602 '2026-10-16T13:00:00Z' 'ACK: plan, ETA tomorrow')) 'OPEN'
CheckV1 'v1 SG-10 (owner comment created 11:00Z, edited 15:00Z to ask; bus ask 12:00Z; owner ACK 13:00Z): UNANSWERED, ask_utc = the edit time, ask_src=comment, exit 10' ((Has $r '^UNANSWERED pr=210 head=01234567 ask_utc=2026-10-16T15:00:00Z deadline=2026-10-17T15:00:00Z verdict=not-merge-ready ask_src=comment$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'v2-edit-before-ack' 211 (Feedback 211 'not-merge-ready' $sg10Fb $sg10Hdr) @((CommentU 9611 '2026-10-16T11:00:00Z' '2026-10-16T12:30:00Z' $sg10Ask), (Comment 9612 '2026-10-16T13:00:00Z' 'ACK: plan, ETA tomorrow')) 'OPEN'
CheckV1 'v2 the same comment edited at 12:30Z (before the 13:00Z ACK): ANSWERED by the ACK, ask_src=comment, exit 0' ((Has $r '^ANSWERED pr=211 by=9612 2026-10-16T13:00:00Z author=layibabalola kind=ACK ask_src=comment$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'v3-edited-ack' 212 (Feedback 212 'not-merge-ready' $sg10Fb $sg10Hdr) @((CommentU 9621 '2026-10-16T13:00:00Z' '2026-10-16T16:00:00Z' 'ACK: plan, ETA tomorrow'), (Comment 9622 '2026-10-16T15:00:00Z' $sg10Ask)) 'OPEN'
CheckV1 'v3 an ACK created 13:00Z and edited 16:00Z never answers an ask made at 15:00Z (its created time is used): UNANSWERED ask_src=comment, exit 10' ((Has $r '^UNANSWERED pr=212 .* ask_utc=2026-10-16T15:00:00Z .* ask_src=comment$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'v4-edit-equals-ack' 213 (Feedback 213 'not-merge-ready' $sg10Fb $sg10Hdr) @((CommentU 9631 '2026-10-16T11:00:00Z' '2026-10-16T13:00:00Z' $sg10Ask), (Comment 9632 '2026-10-16T13:00:00Z' 'ACK: plan')) 'OPEN'
CheckV1 'v4 an edit at exactly the ACK time: the ACK is not strictly newer, so UNANSWERED (SG-09 rule applies to the edit time too)' ((Has $r '^UNANSWERED pr=213 .* ask_utc=2026-10-16T13:00:00Z .* ask_src=comment$') -and $r.exit -eq 10) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'v5-outsider-edit' 214 (Feedback 214 'not-merge-ready' $sg10Fb $sg10Hdr) @((CommentU 9641 '2026-10-16T11:00:00Z' '2026-10-16T15:00:00Z' $sg10Ask 'outside-contributor'), (Comment 9642 '2026-10-16T13:00:00Z' 'ACK: plan')) 'OPEN'
Check 'v5 control: an outside account editing its comment to ask is still not an ask (the owner ACK at 13:00Z answers, ask_src=header)' ((Has $r '^ANSWERED pr=214 by=9642 .* kind=ACK ask_src=header$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'v6-no-updated' 215 (Feedback 215 'not-merge-ready' $sg10Fb $sg10Hdr) @((Comment 9651 '2026-10-16T11:00:00Z' $sg10Ask), (Comment 9652 '2026-10-16T13:00:00Z' 'ACK: plan')) 'OPEN'
Check 'v6 control: a row with no updated_at falls back to created (ask 11:00Z is older than the 12:00Z header): ANSWERED, ask_src=header' ((Has $r '^ANSWERED pr=215 by=9652 .* kind=ACK ask_src=header$') -and $r.exit -eq 0) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'v7-unparsable-row' 216 (Feedback 216 'not-merge-ready' $sg10Fb $sg10Hdr) @((Comment 9661 '2026-10-16T13:00:00Z' 'ACK: plan'), [ordered]@{ id = 9662; user = [ordered]@{ login = 'layibabalola' }; created_at = '2026-10-16T11:00:00Z'; updated_at = 'not a time'; body = $sg10Ask }) 'OPEN'
CheckV1 'v7 a comment row whose updated_at cannot be parsed: UNKNOWN reason=comments-unparsable (with header_pr=216), exit 20, never ANSWERED' (((Has $r '^UNKNOWN pr=216 reason=comments-unparsable header_pr=216$') -and -not (Has $r '^ANSWERED ')) -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"
$r = Run-Case 'v8-no-created' 217 (Feedback 217 'not-merge-ready' $sg10Fb $sg10Hdr) @((Comment 9671 '2026-10-16T13:00:00Z' 'ACK: plan'), [ordered]@{ id = 9672; user = [ordered]@{ login = 'layibabalola' }; body = $sg10Ask }) 'OPEN'
CheckV1 'v8 a comment row with no created time at all: UNKNOWN reason=comments-unparsable, exit 20 (it used to be dropped silently)' (((Has $r '^UNKNOWN pr=217 reason=comments-unparsable header_pr=217$') -and -not (Has $r '^ANSWERED ')) -and $r.exit -eq 20) "exit=$($r.exit) $($r.text)"
# 18b. the live path (git bus + PR state list) reads comments from the REST API; -GhDataDir is the test seam: <N>.view.json = `gh pr view --json state`, <N>.api.json = `gh api .../comments --paginate --slurp`; file text FAIL = the call failed
$liveSeam = if (Select-String -LiteralPath $Script -Pattern 'GhDataDir' -Quiet) { $true } else { $false }
$ghDir = Join-Path $WorkRoot 'gh-data'
New-Item -ItemType Directory -Path $ghDir -Force | Out-Null
$liveList = Join-Path $WorkRoot 'pr-state-live.json'
$liveNums = 310, 311, 312, 313, 314, 315
$liveItems = @($liveNums | ForEach-Object { '{"number":' + $_ + ',"state":"OPEN"}' })
[IO.File]::WriteAllText($liveList, ('[' + ($liveItems -join ',') + ']'), $utf8)
foreach ($ln in $liveNums) {
    [IO.File]::WriteAllText((Join-Path $bus "cos-feedback\mlv-app\pr-$ln.md"), (Feedback $ln 'not-merge-ready' "Review body, nothing to answer.`n" $sg10Hdr), $utf8)
    [IO.File]::WriteAllText((Join-Path $ghDir "$ln.view.json"), '{"state":"OPEN"}', $utf8)
}
# sol gate key r14 SG-15: PR 317 has a GREEN bus file and is MISSING from the state list; its comments hold an unanswered owner ask. It must be read live, not NOASK.
[IO.File]::WriteAllText((Join-Path $bus 'cos-feedback\mlv-app\pr-317.md'), (Feedback 317 'merge-when-ci-green' "Looks good. Nothing further.`n" $sg10Hdr), $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '317.view.json'), '{"state":"OPEN"}', $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '317.api.json'), '[[{"id":9717,"user":{"login":"layibabalola"},"created_at":"2026-10-16T14:00:00Z","updated_at":"2026-10-16T14:00:00Z","body":"ACK required: answer this before merge"}]]', $utf8)
# sol gate key r15 SG-16: green bus files, missing from the state list, read live as CLOSED. 318 holds an unanswered owner ask (must be UNANSWERED state=CLOSED, never NOASK);
# 319 holds no ask (NOASK, but WITH state=CLOSED so the gate can refuse a PR it is merging that this scan read as CLOSED).
[IO.File]::WriteAllText((Join-Path $bus 'cos-feedback\mlv-app\pr-318.md'), (Feedback 318 'merge-when-ci-green' "Looks good. Nothing further.`n" $sg10Hdr), $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '318.view.json'), '{"state":"CLOSED"}', $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '318.api.json'), '[[{"id":9718,"user":{"login":"layibabalola"},"created_at":"2026-10-16T14:00:00Z","updated_at":"2026-10-16T14:00:00Z","body":"ACK required: answer this before merge"}]]', $utf8)
[IO.File]::WriteAllText((Join-Path $bus 'cos-feedback\mlv-app\pr-319.md'), (Feedback 319 'merge-when-ci-green' "Looks good. Nothing further.`n" $sg10Hdr), $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '319.view.json'), '{"state":"CLOSED"}', $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '319.api.json'), '[[]]', $utf8)
& git -C $bus add cos-feedback 2>&1 | Out-Null
& git -C $bus -c user.name=t -c user.email=t@example.invalid commit -q -m 'fixture live comments' 2>&1 | Out-Null
# r1b: the r1 lines below (a nested @(, ...) wrapper and a bracket-trimming slurp) built the wrong shape; kept as comments, replaced by the two plain page lines and the "[$p1,$p2]" slurp that follow
#   $sg10Page1 = ConvertTo-Json -InputObject @(, @((CommentU 9701 ...))) -Depth 6   (old)
#   $slurp = '[' + $sg10Page1.Trim().TrimStart('[').TrimEnd(']').Trim() + ',' + ... + ']'   (old)
$sg10Page1 = ConvertTo-Json -InputObject @((CommentU 9701 '2026-10-16T11:00:00Z' '2026-10-16T15:00:00Z' $sg10Ask)) -Depth 6
$sg10Page2 = ConvertTo-Json -InputObject @((Comment 9702 '2026-10-16T13:00:00Z' 'ACK: plan, ETA tomorrow')) -Depth 6
# a slurped two-page answer (gh api --paginate --slurp): [ [ {...} ], [ {...} ] ]
$slurp = "[$sg10Page1,$sg10Page2]"
[IO.File]::WriteAllText((Join-Path $ghDir '310.api.json'), $slurp, $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '311.api.json'), 'FAIL', $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '312.api.json'), '[[{"id":1,"user":{"login":"layibabalola"},"body":"ACK: x"}]]', $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '313.api.json'), 'not json', $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '314.api.json'), '{"message":"Not Found","status":"404"}', $utf8)
[IO.File]::WriteAllText((Join-Path $ghDir '315.api.json'), '[[{"id":9711,"user":{"login":"layibabalola"},"created_at":"2026-10-16T13:00:00Z","updated_at":"2026-10-16T13:00:00Z","body":"ACK: plan"}]]', $utf8)
$ofL = Join-Path $WorkRoot 'c18-live.out.md'
if ($liveSeam) {
    $lines = & pwsh -NoProfile -NonInteractive -File $Script -BusRoot $bus -Ref master -OutFile $ofL -Repo $repo -PrStateFile $liveList -GhDataDir $ghDir 2>&1
    $txt = (@($lines) -join "`n")
} else { $txt = 'no -GhDataDir seam' }
CheckV1 'l1 live path, two slurped REST pages (edited 11:00Z comment now asks at 15:00Z, ACK 13:00Z): UNANSWERED ask_utc = 15:00Z ask_src=comment' ($txt -match '(?m)^UNANSWERED pr=310 head=01234567 ask_utc=2026-10-16T15:00:00Z deadline=2026-10-17T15:00:00Z verdict=not-merge-ready ask_src=comment$') $txt
CheckV1 'l2 live comments read fails: UNKNOWN pr=311 reason=gh-failed, never ANSWERED' (($txt -match '(?m)^UNKNOWN pr=311 reason=gh-failed') -and $txt -notmatch '(?m)^ANSWERED pr=311 ') $txt
CheckV1 'l3 live REST row with no created_at: UNKNOWN pr=312 reason=comments-unparsable' ($txt -match '(?m)^UNKNOWN pr=312 reason=comments-unparsable header_pr=312$') $txt
CheckV1 'l4 live REST answer that is not JSON: UNKNOWN pr=313 reason=gh-json-unparsable' ($txt -match '(?m)^UNKNOWN pr=313 reason=gh-json-unparsable$') $txt
CheckV1 'l5 live REST answer that is an error object (404 Not Found), not a comment list: UNKNOWN pr=314 reason=comments-unparsable' ($txt -match '(?m)^UNKNOWN pr=314 reason=comments-unparsable header_pr=314$') $txt
CheckV1 'l6 live path, unedited ACK after the 12:00Z header: ANSWERED by the REST row' ($txt -match '(?m)^ANSWERED pr=315 by=9711 2026-10-16T13:00:00Z author=layibabalola kind=ACK ask_src=header$') $txt
CheckV1 'l7 SG-15 a green PR MISSING from the state list is read live, and its unanswered owner ask is UNANSWERED (never NOASK)' (($txt -match '(?m)^UNANSWERED pr=317 .* ask_src=comment$') -and $txt -notmatch '(?m)^NOASK pr=317 ') $txt
CheckV1 'l8 SG-16 a green PR read live as CLOSED with an unanswered explicit owner ask is UNANSWERED with state=CLOSED (never an untagged NOASK)' (($txt -match '(?m)^UNANSWERED pr=318 .* state=CLOSED ask_src=comment$') -and $txt -notmatch '(?m)^NOASK pr=318 ') $txt
CheckV1 'l9 SG-16 a green PR read live as CLOSED with no ask is NOASK and still carries state=CLOSED' ($txt -match '(?m)^NOASK pr=319 verdict=merge-when-ci-green state=CLOSED$') $txt

# 19. COS-SCAN-REVIEW-SURFACE-ASK-1 (sol gate key r10 SG-11): owner ACK requests in review bodies, inline review comments and
# review-thread replies are ask sources too (dated by their newest edit); they never answer. Fixture seam: <N>.reviews.json (REST shape,
# reviews use submitted_at; review comments use created_at/updated_at; a PENDING review has no submitted_at and is ignored).
$s1Total = 0; $s1Fail = 0
function CheckS1([string]$Name, [bool]$Ok, [string]$Detail = '') { $script:s1Total++; if (-not $Ok) { $script:s1Fail++ }; Check $Name $Ok $Detail }
function Run-CaseR([string]$Name, [int]$Pr, [string]$Feedback, $Comments, [string]$ReviewsJson) {
    $dir = Join-Path $WorkRoot $Name
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $dir "pr-$Pr.md"), $Feedback, $utf8)
    [IO.File]::WriteAllText((Join-Path $dir "$Pr.state.txt"), 'OPEN', $utf8)
    [IO.File]::WriteAllText((Join-Path $dir "$Pr.comments.json"), (ConvertTo-Json -InputObject @($Comments) -Depth 6), $utf8)
    [IO.File]::WriteAllText((Join-Path $dir "$Pr.reviews.json"), $ReviewsJson, $utf8)
    return Run-Scan -Dir $dir -Name $Name
}
$ack0900 = @((Comment 9801 '2026-10-15T09:00:00Z' 'ACK: plan'))
$r = Run-CaseR 's1-review-body-ask' 401 (Feedback 401 'not-merge-ready') $ack0900 '[{"id":71,"user":{"login":"layibabalola"},"state":"COMMENTED","submitted_at":"2026-10-15T10:00:00Z","body":"ACK required: new blocker in this review"}]'
CheckS1 's1 SG-11 an owner REVIEW BODY asking after the 09:00Z ACK: UNANSWERED ask_src=review' (Has $r '^UNANSWERED pr=401 .* ask_utc=2026-10-15T10:00:00Z .* ask_src=review$') $r.text
$r = Run-CaseR 's2-review-before-ack' 402 (Feedback 402 'not-merge-ready') $ack0900 '[{"id":72,"user":{"login":"layibabalola"},"state":"COMMENTED","submitted_at":"2026-10-15T08:30:00Z","body":"ACK required: earlier review"}]'
CheckS1 's2 an owner review asking BEFORE the 09:00Z ACK: ANSWERED' (Has $r '^ANSWERED pr=402 by=9801 ') $r.text
$r = Run-CaseR 's3-inline-edited' 403 (Feedback 403 'not-merge-ready') $ack0900 '[{"id":73,"user":{"login":"layibabalola"},"created_at":"2026-10-15T08:30:00Z","updated_at":"2026-10-15T10:30:00Z","body":"ACK required: inline review comment edited later"}]'
CheckS1 's3 SG-11 an owner INLINE review comment edited at 10:30Z to ask: UNANSWERED ask_utc = the edit time, ask_src=review' (Has $r '^UNANSWERED pr=403 .* ask_utc=2026-10-15T10:30:00Z .* ask_src=review$') $r.text
$r = Run-CaseR 's4-outsider-review' 404 (Feedback 404 'not-merge-ready') $ack0900 '[{"id":74,"user":{"login":"someone-else"},"state":"COMMENTED","submitted_at":"2026-10-15T10:00:00Z","body":"ACK required: outsider"}]'
CheckS1 's4 control: an outside account review asking is not an ask: ANSWERED' (Has $r '^ANSWERED pr=404 by=9801 ') $r.text
$r = Run-CaseR 's5-pending-review' 405 (Feedback 405 'not-merge-ready') $ack0900 '[{"id":75,"user":{"login":"layibabalola"},"state":"PENDING","body":"ACK required: draft nobody can see"}]'
CheckS1 's5 control: a PENDING (unsubmitted) owner review is ignored: ANSWERED' (Has $r '^ANSWERED pr=405 by=9801 ') $r.text
$r = Run-CaseR 's6-review-bad-time' 406 (Feedback 406 'not-merge-ready') $ack0900 '[{"id":76,"user":{"login":"layibabalola"},"created_at":"2026-10-15T08:30:00Z","updated_at":"not-a-time","body":"ACK required: x"}]'
CheckS1 's6 a review row with an unparsable updated time: UNKNOWN reason=comments-unparsable, never ANSWERED' ((Has $r '^UNKNOWN pr=406 reason=comments-unparsable') -and -not (Has $r '^ANSWERED pr=406 ')) $r.text
$r = Run-CaseR 's7-review-ack-is-not-an-answer' 407 (Feedback 407 'not-merge-ready') @() '[{"id":77,"user":{"login":"layibabalola"},"state":"COMMENTED","submitted_at":"2026-10-15T10:00:00Z","body":"ACK: answering in a review"}]'
CheckS1 's7 an ACK posted as a review body does not answer (answers are issue comments only): UNANSWERED' (Has $r '^UNANSWERED pr=407 ') $r.text
# sol gate key r11 SG-12: REST gives a review no edit time, so reviews are read through GraphQL (lastEditedAt). The fixture seam accepts
# the GraphQL answer shape too: {"data":{"repository":{"pullRequest":{"reviews":{"pageInfo":{"hasNextPage":..},"nodes":[..]}}}}}.
$r = Run-CaseR 's8-graphql-edited-review' 408 (Feedback 408 'not-merge-ready') $ack0900 '{"data":{"repository":{"pullRequest":{"reviews":{"pageInfo":{"hasNextPage":false},"nodes":[{"databaseId":78,"author":{"login":"layibabalola"},"state":"COMMENTED","submittedAt":"2026-10-15T08:30:00Z","lastEditedAt":"2026-10-15T11:00:00Z","body":"ACK required: added after the ACK by an edit"}]}}}}}'
CheckS1 's8 SG-12 an owner review SUBMITTED 08:30Z (before the 09:00Z ACK) and EDITED 11:00Z to ask: UNANSWERED ask_utc = the edit time, ask_src=review' (Has $r '^UNANSWERED pr=408 .* ask_utc=2026-10-15T11:00:00Z .* ask_src=review$') $r.text
$r = Run-CaseR 's9-graphql-unedited-review' 409 (Feedback 409 'not-merge-ready') $ack0900 '{"data":{"repository":{"pullRequest":{"reviews":{"pageInfo":{"hasNextPage":false},"nodes":[{"databaseId":79,"author":{"login":"layibabalola"},"state":"COMMENTED","submittedAt":"2026-10-15T08:30:00Z","lastEditedAt":null,"body":"ACK required: before the ACK, never edited"}]}}}}}'
CheckS1 's9 control: an owner review submitted 08:30Z and never edited (lastEditedAt null) is answered by the 09:00Z ACK: ANSWERED' (Has $r '^ANSWERED pr=409 by=9801 ') $r.text
$r = Run-CaseR 's10-graphql-truncated' 410 (Feedback 410 'not-merge-ready') $ack0900 '{"data":{"repository":{"pullRequest":{"reviews":{"pageInfo":{"hasNextPage":true},"nodes":[]}}}}}'
CheckS1 's10 a GraphQL review list with more pages than one read: UNKNOWN reason=comments-unparsable, never ANSWERED' ((Has $r '^UNKNOWN pr=410 reason=comments-unparsable') -and -not (Has $r '^ANSWERED pr=410 ')) $r.text
# sol gate key r12 SG-13: a GREEN header (no ask in it) must not hide an owner comment / review ask dated at or before the header time.
$r = Run-CaseR 's11-green-header-same-second-comment' 411 (Feedback 411 'merge-when-ci-green' "Looks good. Nothing further.`n") @((Comment 9811 $reviewed 'ACK required: asked in the same second as the green header')) '[]'
CheckS1 's11 SG-13 green header 08:12:00Z, owner COMMENT ask at the same second, no ACK: UNANSWERED (not NOASK)' (Has $r '^UNANSWERED pr=411 .* ask_src=comment$') $r.text
$r = Run-CaseR 's12-green-header-earlier-review' 412 (Feedback 412 'merge-when-ci-green' "Looks good. Nothing further.`n") @() '[{"id":82,"user":{"login":"layibabalola"},"state":"COMMENTED","submitted_at":"2026-10-15T08:00:00Z","body":"ACK required: review before the green header"}]'
CheckS1 's12 SG-13 green header 08:12:00Z, owner REVIEW ask at 08:00Z, no ACK: UNANSWERED ask_src=review' (Has $r '^UNANSWERED pr=412 .* ask_utc=2026-10-15T08:00:00Z .* ask_src=review$') $r.text
$r = Run-CaseR 's13-green-header-answered' 413 (Feedback 413 'merge-when-ci-green' "Looks good. Nothing further.`n") @((Comment 9813 '2026-10-15T08:00:00Z' 'ACK required: early ask'), (Comment 9814 '2026-10-15T08:05:00Z' 'ACK: plan')) '[]'
CheckS1 's13 control: green header, an owner ask at 08:00Z answered by an ACK at 08:05Z: ANSWERED' (Has $r '^ANSWERED pr=413 by=9814 ') $r.text
$r = Run-CaseR 's14-green-header-no-ask' 414 (Feedback 414 'merge-when-ci-green' "Looks good. Nothing further.`n") @((Comment 9815 '2026-10-15T08:00:00Z' 'Thanks, looks good.')) '[]'
CheckS1 's14 control: green header, an owner comment with no ask text: NOASK' (Has $r '^NOASK pr=414 ') $r.text
Write-Output "S1_ROWS total=$s1Total failed=$s1Fail"

# 20. sol gate key r16 SG-17: on the live path a review read (GraphQL reviews, REST inline comments) that exits non-zero with EMPTY stderr
# was treated as read. -GhExe runs a fake gh (.cmd): pr view -> OPEN, issue comments -> none; graphql / pulls comments per case.
$sg17Root = Join-Path $WorkRoot 'sg17'
$sg17Bus = Join-Path $sg17Root 'bus'
New-Item -ItemType Directory -Path (Join-Path $sg17Bus 'cos-feedback\mlv-app') -Force | Out-Null
& git -C $sg17Bus init -q -b master 2>&1 | Out-Null
foreach ($sn in 330, 331, 332) { [IO.File]::WriteAllText((Join-Path $sg17Bus "cos-feedback\mlv-app\pr-$sn.md"), (Feedback $sn 'merge-when-ci-green' "Looks good.`n"), $utf8) }
& git -C $sg17Bus add cos-feedback 2>&1 | Out-Null
& git -C $sg17Bus -c user.name=t -c user.email=t@example.invalid commit -q -m 'sg17 fixture' 2>&1 | Out-Null
$sg17List = Join-Path $sg17Root 'pr-state.json'
[IO.File]::WriteAllText($sg17List, '[{"number":330,"state":"OPEN"},{"number":331,"state":"OPEN"},{"number":332,"state":"OPEN"}]', $utf8)
$gqlOk = '{"data":{"repository":{"pullRequest":{"reviews":{"pageInfo":{"hasNextPage":false},"nodes":[]}}}}}'
function Sg17Fake([string]$Name, [string]$GqlLine, [string]$PullsLine) {
    $f = Join-Path $sg17Root "$Name.cmd"
    $t = "@echo off`r`nif `"%~1`"==`"pr`" goto view`r`nif `"%~2`"==`"graphql`" goto gql`r`necho %~2| findstr /c:`"/pulls/`" >nul && goto pulls`r`necho [[]]`r`nexit /b 0`r`n:pulls`r`n$PullsLine`r`n:gql`r`n$GqlLine`r`n:view`r`necho {`"state`":`"OPEN`"}`r`nexit /b 0`r`n"
    [IO.File]::WriteAllText($f, $t, [Text.Encoding]::ASCII)
    return $f
}
$fakeA = Sg17Fake 'gh-graphql-silent-fail' 'exit /b 1' "echo [[]]`r`nexit /b 0"
$fakeB = Sg17Fake 'gh-pulls-silent-fail' "echo $gqlOk`r`nexit /b 0" 'exit /b 1'
$fakeC = Sg17Fake 'gh-all-ok' "echo $gqlOk`r`nexit /b 0" "echo [[]]`r`nexit /b 0"
function Sg17Run([string]$Fake, [int]$Pr) {
    $of = Join-Path $sg17Root "out-$Pr.md"
    $lines = & pwsh -NoProfile -NonInteractive -File $Script -BusRoot $sg17Bus -Ref master -OutFile $of -Repo $repo -PrStateFile $sg17List -GhExe $Fake 2>&1
    return (@($lines) -join "`n")
}
$t17a = Sg17Run $fakeA 330
CheckV1 'g1 SG-17 the GraphQL reviews read fails with EMPTY stderr: UNKNOWN reason=gh-failed reviews, never NOASK' (($t17a -match '(?m)^UNKNOWN pr=330 reason=gh-failed reviews ') -and $t17a -notmatch '(?m)^NOASK pr=330 ') $t17a
$t17b = Sg17Run $fakeB 331
CheckV1 'g2 SG-17 the REST inline review-comments read fails with EMPTY stderr: UNKNOWN reason=gh-failed reviews, never NOASK' (($t17b -match '(?m)^UNKNOWN pr=331 reason=gh-failed reviews ') -and $t17b -notmatch '(?m)^NOASK pr=331 ') $t17b
$t17c = Sg17Run $fakeC 332
CheckV1 'g3 SG-17 control: every read succeeds (no ask anywhere): NOASK, so the fake gh itself works' ($t17c -match '(?m)^NOASK pr=332 verdict=merge-when-ci-green$') $t17c

Write-Output "TESTS passed=$pass failed=$fail"
Write-Output "V1_ROWS total=$v1Total failed=$v1Fail"
Write-Output "U1_ROWS total=$u1Total failed=$u1Fail"
Write-Output "RED_ROWS total=$redTotal failed=$redFail"
Write-Output "R3_ROWS total=$r3Total failed=$r3Fail"
Write-Output "R1_ROWS total=$r1Total failed=$r1Fail"
$shapesGreen = @('a', 'b', 'c', 'd' | Where-Object { $shapeRows[$_] -gt 0 -and $shapeFail[$_] -eq 0 }).Count
Write-Output "SHAPES green=$shapesGreen/4 (a rows=$($shapeRows.a) failed=$($shapeFail.a); b rows=$($shapeRows.b) failed=$($shapeFail.b); c rows=$($shapeRows.c) failed=$($shapeFail.c); d rows=$($shapeRows.d) failed=$($shapeFail.d))"
exit $(if ($fail -eq 0) { 0 } else { 1 })
