# Test helper for quality_lift_run_prescribed_git.ps1. ASCII only, deliberately no param() block.
# A native process (a PowerShell host started by the harness) that appends the arguments it received, as one
# UTF-8 JSON array per line, to the file named by the QL_ECHO_OUT environment variable. Reaching it through a
# real process boundary reproduces how Windows PowerShell 5.1 converts arguments for a native git.exe.
$ErrorActionPreference = 'Stop'
$line = ConvertTo-Json -InputObject @($args | ForEach-Object { [string]$_ }) -Compress
[IO.File]::AppendAllText($env:QL_ECHO_OUT, $line + "`n", (New-Object Text.UTF8Encoding($false)))
