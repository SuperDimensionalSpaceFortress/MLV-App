# Test harness for test_quality_lift_adversary_template.py. ASCII only.
# Reads a rendered adversary prompt, takes every double-backticked ``git -C ...`` command it prescribes, evaluates each
# in this PowerShell session against a git argument-capture stub (no real git), and writes the captured
# argument lists as UTF-8 JSON to -OutPath.
param([Parameter(Mandatory)][string]$PromptPath, [Parameter(Mandatory)][string]$OutPath)
$ErrorActionPreference = 'Stop'
$text = [IO.File]::ReadAllText($PromptPath, [Text.Encoding]::UTF8)
$commands = @([regex]::Matches($text, '``(git -C .+?)``') | ForEach-Object { $_.Groups[1].Value })
$script:captured = New-Object System.Collections.ArrayList
function git { [void]$script:captured.Add(@($args | ForEach-Object { [string]$_ })) }
foreach ($c in $commands) { Invoke-Expression $c }
$out = [ordered]@{ count = $commands.Count; calls = @($script:captured | ForEach-Object { , @($_) }) }
[IO.File]::WriteAllText($OutPath, (ConvertTo-Json -InputObject $out -Depth 5 -Compress), (New-Object Text.UTF8Encoding($false)))
