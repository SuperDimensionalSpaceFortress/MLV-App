# Test harness for test_quality_lift_adversary_template.py. ASCII only.
# Reads a rendered adversary prompt and takes every double-backticked ``git -C ...`` command it prescribes.
# Each command is parsed first and refused unless it is ONE plain command whose elements are all constant
# strings (a double-quoted string, a variable, a subexpression, ; & | are all refused), so nothing the prompt
# holds can run. It is then run as `git <the arguments PowerShell resolved>` against a git stub that is a real
# child process (quality_lift_echo_args.ps1), so the arguments cross the same native-argument conversion a real
# git.exe would see. The received argument lists are written as UTF-8 JSON to -OutPath. No real git runs.
param([Parameter(Mandatory)][string]$PromptPath, [Parameter(Mandatory)][string]$OutPath)
$ErrorActionPreference = 'Stop'
$text = [IO.File]::ReadAllText($PromptPath, [Text.Encoding]::UTF8)
$commands = @([regex]::Matches($text, '``(git -C .+?)``') | ForEach-Object { $_.Groups[1].Value })
$hostExe = (Get-Process -Id $PID).Path
$echoScript = Join-Path $PSScriptRoot 'quality_lift_echo_args.ps1'
$echoOut = [IO.Path]::GetTempFileName()
$env:QL_ECHO_OUT = $echoOut
function git { & $hostExe -NoProfile -NonInteractive -File $echoScript @args }
foreach ($c in $commands) {
    $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseInput($c, [ref]$null, [ref]$errors)
    if ($errors.Count) { throw "PARSE_ERROR $c" }
    $statements = @($ast.EndBlock.Statements)
    $ok = $statements.Count -eq 1 -and $statements[0] -is [Management.Automation.Language.PipelineAst] -and $statements[0].PipelineElements.Count -eq 1 -and $statements[0].PipelineElements[0] -is [Management.Automation.Language.CommandAst]
    if (-not $ok) { throw "NOT_A_SINGLE_COMMAND $c" }
    $elements = @($statements[0].PipelineElements[0].CommandElements)
    $values = @()
    foreach ($e in $elements) {
        if ($e -is [Management.Automation.Language.StringConstantExpressionAst]) { $values += [string]$e.Value }
        elseif ($e -is [Management.Automation.Language.CommandParameterAst] -and $null -eq $e.Argument) { $values += $e.Extent.Text }
        else { throw "NON_CONSTANT_ELEMENT $c" }
    }
    if ($values[0] -ne 'git') { throw "NOT_GIT $c" }
    $values = @($values | Select-Object -Skip 1)
    git @values
}
# Each echoed line is already a JSON array; join the lines as text (Windows PowerShell 5.1 ConvertFrom-Json wraps arrays).
$lines = @([IO.File]::ReadAllLines($echoOut, [Text.Encoding]::UTF8) | Where-Object { $_ })
Remove-Item -LiteralPath $echoOut -Force
$json = '{"count":' + $commands.Count + ',"calls":[' + ($lines -join ',') + ']}'
[IO.File]::WriteAllText($OutPath, $json, (New-Object Text.UTF8Encoding($false)))
