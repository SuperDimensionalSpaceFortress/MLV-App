# Test stub of the machine heavy-slot CLI (C:\ProgramData\HeavySlot\bin\heavy-slot.ps1), for test_build_guard.py.
# It never touches a kernel mutex or the real state directory. STUB_HEAVY_SLOT_MODE picks the behaviour:
#   run        print the ticket line, run the command after '--', return its exit code (the real run path)
#   marked75   print the ticket line, write a marker that names this run to --exit-file, exit 75
#   unmarked75 print the ticket line, exit 75 with no marker
#   forged75   print the ticket line, write a marker with another nonce, exit 75
#   marked9    print the ticket line, write a matching marker that says code 9, exit 9 (not a heavy-slot code)
#   marked     print the ticket line, write a matching marker for STUB_HEAVY_SLOT_CODE, exit with it (75/76/77)
#   bare       print the ticket line, exit STUB_HEAVY_SLOT_CODE before any admission, no marker
#   readyexit  stands in for a holder that wrote its ready file and then ended: write the ready file named in the
#              holder's -EncodedCommand, wait STUB_HEAVY_SLOT_HOLD_S seconds (default 0), exit STUB_HEAVY_SLOT_CODE (default 0)
#   pinwarn    print heavy-slot's own PIN MISMATCH warning, then behave as run
#   readyorphan  a holder child writes the ready file and ends (after STUB_HEAVY_SLOT_CHILD_S seconds, default 0), while
#              heavy-slot itself stays up STUB_HEAVY_SLOT_HOLD_S
#              seconds (its release work after the child), then exits STUB_HEAVY_SLOT_CODE (default 0)
#   locklog    hold this run's log open with read access and no read sharing, print the PIN MISMATCH warning, then run
# STUB_HEAVY_SLOT_DELAY_S (run, pinwarn and readyexit) waits that many seconds before starting the child, like a queue wait for the slot.
# Every call appends one JSON line (argv, the two EXPECT variables, and the child's start and end) to STUB_HEAVY_SLOT_LOG.
$argv = @($args)
$mode = if ($env:STUB_HEAVY_SLOT_MODE) { $env:STUB_HEAVY_SLOT_MODE } else { 'run' }
$ticket = 'stub-{0}' -f $PID
$nonce = [Guid]::NewGuid().ToString('N')
[Console]::Error.WriteLine("HEAVY-SLOT TICKET $ticket $nonce")
$exitFile = $null
$command = @()
for ($i = 0; $i -lt $argv.Count; $i++) {
    if ($argv[$i] -eq '--exit-file') { $exitFile = $argv[$i + 1] }
    if ($argv[$i] -eq '--') { $command = @($argv | Select-Object -Skip ($i + 1)); break }
}
$record = [ordered]@{ argv = $argv; expectSha = $env:HEAVY_SLOT_EXPECT_SHA256; expectKeeper = $env:HEAVY_SLOT_EXPECT_KEEPER; mode = $mode; childStart = $null; childEnd = $null }
function Write-StubMarker([string]$Nonce, [int]$Code = 75) {
    $marker = [ordered]@{ source = 'heavy-slot'; code = $Code; reason = 'timed out after 3600s waiting for the slot'; ticket = $ticket; nonce = $Nonce; pid = $PID }
    if ($exitFile) { Set-Content -LiteralPath $exitFile -Value ($marker | ConvertTo-Json -Compress) -Encoding ascii }
}
$code = 0
$stubCode = if ($env:STUB_HEAVY_SLOT_CODE) { [int]$env:STUB_HEAVY_SLOT_CODE } else { 0 }
if ($mode -eq 'locklog') { $held = [IO.File]::Open(($exitFile -replace '\.exit\.json$', '.log'), 'Open', 'Read', 'Write') }
if ($mode -in 'pinwarn', 'locklog') { [Console]::Error.WriteLine('HEAVY-SLOT WARN: PIN MISMATCH: this heavy-slot.ps1 is 00, HEAVY_SLOT_EXPECT_SHA256 is 11') }
function Get-StubReadyPath {
    $encoded = $command[[array]::IndexOf($command, '-EncodedCommand') + 1]
    $holder = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($encoded))
    if ($holder -match "(?m)^Set-Content -LiteralPath '((?:[^']|'')+)'") { return ($Matches[1] -replace "''", "'") }
}
switch ($mode) {
    'marked75' { Write-StubMarker $nonce; $code = 75 }
    'forged75' { Write-StubMarker ([Guid]::NewGuid().ToString('N')); $code = 75 }
    'unmarked75' { $code = 75 }
    'marked9' { Write-StubMarker $nonce 9; $code = 9 }
    'marked' { Write-StubMarker $nonce $stubCode; $code = $stubCode }
    'bare' { $code = $stubCode }
    'readyexit' {
        if ($env:STUB_HEAVY_SLOT_DELAY_S) { Start-Sleep -Seconds ([int]$env:STUB_HEAVY_SLOT_DELAY_S) }
        $ticks = (Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks
        Set-Content -LiteralPath (Get-StubReadyPath) -Value @("${ticket}:$PID", "holder=$PID@$ticks") -Encoding ascii
        $record.childStart = [DateTime]::UtcNow.ToString('o')
        if ($env:STUB_HEAVY_SLOT_HOLD_S) { Start-Sleep -Seconds ([double]$env:STUB_HEAVY_SLOT_HOLD_S) }
        $record.childEnd = [DateTime]::UtcNow.ToString('o')
        $code = $stubCode
    }
    'readyorphan' {
        $ready = (Get-StubReadyPath) -replace "'", "''"
        $childS = if ($env:STUB_HEAVY_SLOT_CHILD_S) { [double]$env:STUB_HEAVY_SLOT_CHILD_S } else { 0 }
        $record.childStart = [DateTime]::UtcNow.ToString('o')
        & $command[0] -NoProfile -NonInteractive -Command "Set-Content -LiteralPath '$ready' -Value @('${ticket}:$PID', ('holder={0}@{1}' -f `$PID, (Get-Process -Id `$PID).StartTime.ToUniversalTime().Ticks)) -Encoding ascii; Start-Sleep -Seconds $childS"
        $record.childEnd = [DateTime]::UtcNow.ToString('o')
        if ($env:STUB_HEAVY_SLOT_HOLD_S) { Start-Sleep -Seconds ([double]$env:STUB_HEAVY_SLOT_HOLD_S) }
        $code = $stubCode
    }
    default {
        if ($env:STUB_HEAVY_SLOT_DELAY_S) { Start-Sleep -Seconds ([int]$env:STUB_HEAVY_SLOT_DELAY_S) }
        $record.childStart = [DateTime]::UtcNow.ToString('o')
        $env:HEAVY_SLOT_HELD = "${ticket}:$PID"
        $exe = $command[0]
        $rest = @(if ($command.Count -gt 1) { $command[1..($command.Count - 1)] })
        & $exe @rest
        $code = $LASTEXITCODE
        $record.childEnd = [DateTime]::UtcNow.ToString('o')
    }
}
if ($env:STUB_HEAVY_SLOT_LOG) { Add-Content -LiteralPath $env:STUB_HEAVY_SLOT_LOG -Value ($record | ConvertTo-Json -Compress) -Encoding ascii }
exit $code
