---
target: specs/mlv-app/cards.md
kind: trap
source_commit: PENDING
law4: attested
---
## mlv-app/conhost-headless-requotes-command-line
rule: hand `conhost.exe --headless` only quote-free tokens. It re-tokenizes and re-quotes the client command line, so a quoted cmd.exe /c line breaks. Carry the real line inside pwsh -EncodedCommand, whose decoded script gives it verbatim to ProcessStartInfo, waits, and returns the exit code.
mechanism: measured 2026-11-14: a cmd.exe /c line holding a quoted program path with a space, plus a quoted redirect, ran under conhost --headless as "'C:\Program' is not recognized". The same transport is fine for simple quoted arguments, so a smoke test with a quote-free line passes and hides the defect. Only a line shaped like the real launch shows it.
applies: mlv-app
check: board-local, not pushed, PowerShell 7 on Windows only: the board guard hub-autopilot/Test-QueueHeadlessTransport.ps1 (sha256 25fb78deaf6994109ee9f2563eaee905c6df825f512f5ec4031a4037a0f75b5b at its offline green run), check L1 (-LiveConhost) runs a real fallback line (a path with a space and an exclamation mark): exit 0, redirect written, no window in the process tree. It is live-only, needs an interactive desktop, ran green on 2026-11-14 and was not re-run for this card. Its offline checks B1 and B4 pin only the line's shape, not the re-quoting.
supersedes: none
evidence: measured
