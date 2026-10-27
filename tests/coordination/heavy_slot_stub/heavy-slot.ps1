# Test stub of the machine heavy-slot CLI (C:\ProgramData\HeavySlot\bin\heavy-slot.ps1), for test_build_guard.py.
# It never touches a kernel mutex or the real state directory. STUB_HEAVY_SLOT_MODE picks the behaviour:
#   run        print the ticket line, run the command after '--', return its exit code (the real run path)
#   marked75   print the ticket line, write a marker that names this run to --exit-file, exit 75
#   unmarked75 print the ticket line, exit 75 with no marker
#   forged75   print the ticket line, write a marker with another nonce, exit 75
#   marked9    print the ticket line, write a matching marker that says code 9, exit 9 (not a heavy-slot code)
# STUB_HEAVY_SLOT_DELAY_S (run mode) waits that many seconds before starting the child, like a queue wait for the slot.
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
switch ($mode) {
    'marked75' { Write-StubMarker $nonce; $code = 75 }
    'forged75' { Write-StubMarker ([Guid]::NewGuid().ToString('N')); $code = 75 }
    'unmarked75' { $code = 75 }
    'marked9' { Write-StubMarker $nonce 9; $code = 9 }
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
