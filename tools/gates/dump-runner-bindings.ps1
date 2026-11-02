[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$RunnerPath,
    [Parameter(Mandatory = $true)][string]$WrapperPath
)

# Static (parse-only) dump of how compare-output-budget.ps1 calls run-release-gui-smoke.ps1:
#   runnerParams : every declared runner parameter, its static type and its ValidateSet (null when it has none)
#   wrapperArgs  : every key of the wrapper's `$runnerArgs = @{ ... }` splat with the source text of its value
#   guardedArgs  : every later `$runnerArgs.<Key> = ...` assignment with the text of the `if` condition guarding it
# Nothing is launched, no smoke runs, and no clip is opened. test_output_budget.py consumes the JSON.

$ErrorActionPreference = 'Stop'

function Read-ScriptAst([string]$Path) {
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $Path).ProviderPath, [ref]$tokens, [ref]$errors)
    if ($errors.Count -gt 0) { throw "parse errors in ${Path}: $($errors[0].Message)" }
    return $ast
}

$runnerAst = Read-ScriptAst $RunnerPath
$runnerParams = [ordered]@{}
$acceptsRemaining = $false
foreach ($parameter in $runnerAst.ParamBlock.Parameters) {
    $name = $parameter.Name.VariablePath.UserPath
    $validateSet = $null
    foreach ($attribute in $parameter.Attributes) {
        if ($attribute -isnot [System.Management.Automation.Language.AttributeAst]) { continue }
        if ($attribute.TypeName.Name -eq 'ValidateSet') {
            $validateSet = @()
            foreach ($argument in $attribute.PositionalArguments) {
                if ($argument -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) {
                    throw "ValidateSet of -$name holds a non-constant value; the static check cannot read it."
                }
                $validateSet += [string]$argument.Value
            }
        }
        if ($attribute.TypeName.Name -eq 'Parameter') {
            foreach ($named in $attribute.NamedArguments) {
                if ($named.ArgumentName -eq 'ValueFromRemainingArguments') { $acceptsRemaining = $true }
            }
        }
    }
    $runnerParams[$name] = [ordered]@{ type = $parameter.StaticType.Name; validateSet = $validateSet }
}

$wrapperAst = Read-ScriptAst $WrapperPath
$wrapperArgs = [ordered]@{}
$guardedArgs = @()
$assignments = $wrapperAst.FindAll({ param($node) $node -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)
foreach ($assignment in $assignments) {
    $left = $assignment.Left
    if ($left -is [System.Management.Automation.Language.VariableExpressionAst] -and $left.VariablePath.UserPath -eq 'runnerArgs') {
        $table = $assignment.Right.Find({ param($node) $node -is [System.Management.Automation.Language.HashtableAst] }, $true)
        if ($null -eq $table) { throw 'the wrapper assigns $runnerArgs from something other than a hashtable literal.' }
        foreach ($pair in $table.KeyValuePairs) {
            $wrapperArgs[[string]$pair.Item1.Value] = $pair.Item2.Extent.Text
        }
    }
    elseif ($left -is [System.Management.Automation.Language.MemberExpressionAst] -and
            $left.Expression -is [System.Management.Automation.Language.VariableExpressionAst] -and
            $left.Expression.VariablePath.UserPath -eq 'runnerArgs') {
        $guard = ''
        for ($ancestor = $assignment.Parent; $null -ne $ancestor; $ancestor = $ancestor.Parent) {
            if ($ancestor -is [System.Management.Automation.Language.IfStatementAst]) {
                $guard = $ancestor.Clauses[0].Item1.Extent.Text
                break
            }
        }
        $guardedArgs += [ordered]@{ key = [string]$left.Member.Value; guard = $guard; value = $assignment.Right.Extent.Text }
    }
}

[ordered]@{
    runnerParams = $runnerParams
    acceptsRemainingArguments = $acceptsRemaining
    wrapperArgs = $wrapperArgs
    guardedArgs = @($guardedArgs)
} | ConvertTo-Json -Depth 8
