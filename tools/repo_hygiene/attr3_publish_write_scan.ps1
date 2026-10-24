# attr3_publish_write_scan.ps1 -- REGRESSION TRIPWIRE for the emitted PLAYBACK-ATTR-3-CUDA job
# templates. It is NOT a soundness proof, and nothing may rely on it as one.
#
# THE SAFETY BOUNDARY IS AT RUNTIME, in tools/profiling/bachelor/AttrCudaArtifacts.psm1 and
# tools/profiling/bachelor/AttrCudaOwnerFootage.psm1: every publish write goes through
# Publish-AttrCuda* / New-AttrCudaDirectory (slot check + write in one call), every tree delete
# through Remove-AttrCudaTree (trusted-root ancestor chain + no reparse point inside). The job's
# trusted roots (the agent root, C:\mlvtmp) are provisioning boundaries.
#
# WHAT THIS SCRIPT DOES: it rejects the known-dangerous SHAPES a trusted author could introduce by
# accident into the three templates, using an allowlist so that an unfamiliar shape fails loudly
# and gets reviewed instead of passing silently:
#   R1 commands      only allowlisted cmdlets, template-defined functions and module-defined
#                    functions, exact case (raw Set-Content/Copy-Item/Move-Item/Remove-Item/Out-File,
#                    Add-Type, New-Object, Set-Variable, Invoke-Expression, ... are not allowlisted).
#   R2 arguments     New-Item (-Path; -ItemType literally Directory|File; no -Name), Expand-Archive
#                    (-DestinationPath), Export-Csv (-LiteralPath) and Start-Process (each -Redirect*)
#                    name a destination provable under $Work; no positional use; no splatting;
#                    ForEach-Object takes script blocks only; the -TrustedRoot of Remove-AttrCudaTree,
#                    Remove-AttrCudaPartialFile, Remove-AttrCudaInputFileByContent and
#                    New-AttrCudaOwnedRoot is literally $AgentRoot or 'C:\mlvtmp'.
#   R3 dynamic calls `&` only on a literal ending in .exe or an allowlisted child-executable variable;
#                    no dot-sourcing.
#   R4 .NET          allowlisted static members and instance method names only.
#   R5 assignments   plain or index assignments; $script: only for non-root names; $env:TEMP/TMP only
#                    to provable values; $Work assigned exactly once, at top level, canonically.
#   R6 redirections  $null or provable.
#   R7 provable      $Work; Join-Path <provable> <safe literal child>; a variable whose every
#                    assignment is a plain '=' of a provable expression.
#   R8 directives    no `using` statements and no `#requires` in a template.
#
# SPLICED HELPER BODIES (ATTR3-SCANNER-HARDEN-1, CoS Prevention note on #432). A generator also
# splices module helpers into the emitted job (Get-AttrCudaEmbeddedFunctionSource -Name @(...)). The
# template scan sees such a helper by NAME only (it is a known command), so a raw write into $Pub
# inside its body used to pass. With -SpliceSource <files> the scan enumerates every
# Get-AttrCudaEmbeddedFunctionSource call site in those files (every literal -Name; a name list that
# is not literal, or a name no -ModulePath module defines, is a 'splice' finding, never a silent
# skip), reads each body with the extractor's own regex, and runs R1 R2 R3 R4 R5 R6 over it exactly as
# over a template, minus the "$Work assigned once at top level" half of R5 (a function body is not a
# job). Findings are reported separately as splicedViolations with the function, its module line and
# the offending token; the template verdict in `violations` is unchanged. The helpers that ARE the
# runtime boundary (Publish-AttrCuda*, New-AttrCudaDirectory, Remove-AttrCuda*) legitimately hold the
# raw writes and deletes a template may not, so the policy that decides which findings are accepted
# lives in the caller (tools/repo_hygiene/test_attr3_spliced_body_scan.py: its inert-member lists and
# its per-function, per-rule, per-token reviewed allowlist), not here. This script reports every
# finding; it never decides one is acceptable.
#
# GRAMMAR (declared before ATTR3-SCANNER-HARDEN-1 r2). The scan judges the PowerShell language AST,
# System.Management.Automation.Language.Parser ParseInput / ParseFile, over (a) the generator template
# with its placeholders neutralised and (b) each helper body that Get-AttrCudaEmbeddedFunctionSource
# splices into the emitted job, read with the extractor's own regex. Rules R1 to R6 above apply to both;
# a spliced body skips only the top-level "$Work assigned once" half of R5.
#
# EXPLICIT LIMITS / NON-PROMISES (booked as ATTR3-SCANNER-HARDEN-1, sol PR #133 r7; restated r2). A
# counterexample outside this grammar is debt against these lines, not a defect in the scan:
#   - No runtime evaluation. Invoke-Expression or a string built at runtime, `& $var` on a value computed
#     at runtime, and members reached through a variable that holds a type object are judged only by the
#     R3 / R4 name rules.
#   - Code a job dot-sources or imports at runtime is not scanned.
#   - Helpers that reach the job by any route other than Get-AttrCudaEmbeddedFunctionSource (string
#     concatenation, -replace, a here-string literal) are not scanned.
#   - The templates of reference-frame-runner-job.ps1 and the Attr3Footage*Job.psm1 modules (generators
#     not passed as -GeneratorPath) are not template-scanned; only their spliced helper names are
#     enumerated.
#   - Arguments passed to child executables, Start-Process -FilePath / -ArgumentList, and `reg` verbs are
#     not judged. Instance methods are allowlisted by NAME, not by receiver type. Trusted-helper
#     arguments other than the -TrustedRoot ones named under R2 are not constrained.
#   - A spliced body is scanned as a standalone function, so a $Pub / $Work / $Cache variable inside it is
#     judged by the same name-based rules as in a template, not by what the caller binds it to.
#   - The scan reads the template with placeholders neutralised, not the emitted job text.
#   - Allowlist rows in the caller (test_attr3_spliced_body_scan.py) are trusted as reviewed; their
#     reasons are not re-proven here.
# A hostile author can defeat any static lint of PowerShell -- including by editing this file -- so review
# of the template diff remains required.
#
# Output: JSON { templates: [...], violations: [ { source, line, rule, text, reason } ],
#                spliced: [ { module, name, line, bodyLength, bodySha256, callers } ],
#                splicedViolations: [ { source, module, function, line, rule, token, text, reason } ] }.
# spliced / splicedViolations are empty unless -SpliceSource is given.

[CmdletBinding()]
param(
    [string[]]$GeneratorPath = @(),
    [string[]]$TemplateFile = @(),
    [string[]]$ModulePath = @(),
    [string[]]$SpliceSource = @()
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# `pwsh -File` cannot pass an array, so ';'-separated lists are accepted too.
$GeneratorPath = @($GeneratorPath | ForEach-Object { $_ -split ';' } | Where-Object { $_ })
$TemplateFile = @($TemplateFile | ForEach-Object { $_ -split ';' } | Where-Object { $_ })
$ModulePath = @($ModulePath | ForEach-Object { $_ -split ';' } | Where-Object { $_ })
$SpliceSource = @($SpliceSource | ForEach-Object { $_ -split ';' } | Where-Object { $_ })
if ($ModulePath.Count -eq 0) {
    # ATTR3-FOOTAGE-BIND-1 PR-B round 4b: playback-attr-3-cuda-job.ps1 also embeds functions from
    # AttrCudaOwnerFootage.psm1 (the private owner-footage workspace), moved out of
    # AttrCudaArtifacts.psm1 so that module stays free of any footage knowledge -- see that
    # module's own header. Both are scanned by default so a caller that names neither still gets
    # the templates' full module-function vocabulary.
    $ModulePath = @(
        (Join-Path $PSScriptRoot '..\profiling\bachelor\AttrCudaArtifacts.psm1'),
        (Join-Path $PSScriptRoot '..\profiling\bachelor\AttrCudaOwnerFootage.psm1'),
        # UM-DISPLAY-SELECT-AND-LOG-1 round 3: the shared display-identity parser the job embeds
        # (ConvertFrom-GuiSmokeDisplayLog / Get-GuiSmokeDisplayIdentity), a pinned closure sibling.
        (Join-Path $PSScriptRoot '..\profiling\gui-smoke-display-identity.ps1')
    )
}

$allowedCmdlets = @(
    'ConvertFrom-Json', 'ConvertTo-Json', 'Expand-Archive', 'Export-Csv', 'ForEach-Object', 'Get-ChildItem',
    'Get-CimInstance', 'Get-Content', 'Get-Date', 'Get-FileHash', 'Get-Process', 'Import-Csv', 'Join-Path',
    'Measure-Object', 'New-Item', 'Out-Null', 'Select-Object', 'Select-String', 'Sort-Object', 'Split-Path',
    'Start-Process', 'Start-Sleep', 'Test-Path', 'Where-Object', 'Write-Output', 'reg'
)
$mutatorParams = @{
    'new-item'       = @('Path')
    'expand-archive' = @('DestinationPath')
    'export-csv'     = @('LiteralPath', 'Path')
    'start-process'  = @('RedirectStandardOutput', 'RedirectStandardError')
}
$childExecutables = @('vsLocator', 'psExe', 'cuobjdump', 'exportTool', 'nvcc')
$allowedStatic = @(
    'double::Parse', 'double::TryParse', 'Environment::GetEnvironmentVariable',
    'Globalization.CultureInfo::InvariantCulture', 'Globalization.NumberStyles::Float',
    'Guid::NewGuid', 'int::MaxValue', 'int::MinValue',
    'IO.File::ReadAllText', 'IO.Path::GetFileNameWithoutExtension', 'IO.Path::GetFullPath',
    'math::Ceiling', 'math::Floor', 'math::Pow', 'math::Round', 'math::Sqrt', 'regex::Escape', 'regex::Matches',
    'string::IsNullOrWhiteSpace', 'StringComparison::OrdinalIgnoreCase',
    'System.Collections.Generic.List[object]::new', 'System.Collections.Specialized.OrderedDictionary::new'
)
$allowedInstance = @('Add', 'Contains', 'ContainsKey', 'Kill', 'Replace', 'StartsWith', 'Substring',
    'ToLowerInvariant', 'ToString', 'ToUniversalTime', 'ToUpperInvariant', 'Trim', 'WaitForExit')
$rootVariables = @('Work', 'Pub', 'Cache', 'AgentRoot', 'Root')
$canonicalWork = @("Join-Path `$AgentRoot `"work\`$JobId`"", "Join-Path 'C:\mlvtmp' `$JobId")
$allowedEnv = @('TEMP', 'TMP')

$violations = [System.Collections.Generic.List[object]]::new()
$scanned = [System.Collections.Generic.List[string]]::new()
$script:bodyMode = $false

function Add-Violation([string]$Source, $Ast, [string]$Rule, [string]$Reason) {
    $text = ($Ast.Extent.Text -replace '\s+', ' ')
    if ($text.Length -gt 160) { $text = $text.Substring(0, 160) }
    $record = [ordered]@{ source = $Source; line = $Ast.Extent.StartLineNumber; rule = $Rule; text = $text; reason = $Reason }
    if ($script:bodyMode) { $record['token'] = Get-ViolationToken $Ast }
    $violations.Add($record)
}

function Get-ViolationToken($Ast) {
    # The offending construct in one short stable word, so a reviewed allowlist row can name it
    # (spliced-body findings only): the command name, Type::Member, .Method(), or the assignment target.
    if ($Ast -is [System.Management.Automation.Language.CommandAst]) {
        $first = $Ast.CommandElements[0]
        $name = $Ast.GetCommandName()
        if ($Ast.InvocationOperator -eq 'Dot') { return '. (dot-source)' }
        if ($Ast.InvocationOperator -eq 'Ampersand') { return '& ' + $first.Extent.Text }
        if ([string]::IsNullOrEmpty($name)) { return $first.Extent.Text }
        if ($name -ieq 'New-Object') {
            # New-Object can instantiate any type (a StreamWriter is a file write), so the type is part of the token.
            $typeArgs = Get-NamedArguments $Ast @('TypeName')
            if ($typeArgs.Count -eq 1 -and $null -ne $typeArgs[0]) { return "$name $($typeArgs[0].Extent.Text)" }
            if ($Ast.CommandElements.Count -gt 1 -and $Ast.CommandElements[1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { return "$name $($Ast.CommandElements[1].Extent.Text)" }
        }
        return $name
    }
    if ($Ast -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and -not $Ast.Static) { return '.' + $Ast.Member.Extent.Text + '()' }
    if ($Ast -is [System.Management.Automation.Language.MemberExpressionAst]) {
        if ($Ast.Static -and $Ast.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) { return $Ast.Expression.TypeName.FullName + '::' + $Ast.Member.Extent.Text }
        return $Ast.Extent.Text
    }
    if ($Ast -is [System.Management.Automation.Language.AssignmentStatementAst]) { return $Ast.Left.Extent.Text }
    if ($Ast -is [System.Management.Automation.Language.FileRedirectionAst]) { return 'redirection' }
    return $Ast.GetType().Name
}

function Get-ModuleFunctionNames([string]$Path) {
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $Path).Path, [ref]$tokens, [ref]$errors)
    @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false) | ForEach-Object { $_.Name })
}

function Get-VariableName($VariableAst) {
    # UserPath carries any scope qualifier ("script:x", "env:TEMP"); split it off.
    $user = $VariableAst.VariablePath.UserPath
    $index = $user.IndexOf(':')
    if ($index -ge 0) { return [pscustomobject]@{ scope = $user.Substring(0, $index); name = $user.Substring($index + 1) } }
    return [pscustomobject]@{ scope = ''; name = $user }
}

function Get-NamedArguments($Command, [string[]]$Names) {
    $found = [System.Collections.Generic.List[object]]::new()
    $elements = $Command.CommandElements
    for ($i = 1; $i -lt $elements.Count; $i++) {
        $element = $elements[$i]
        if ($element -isnot [System.Management.Automation.Language.CommandParameterAst]) { continue }
        foreach ($name in $Names) {
            if ($element.ParameterName.Length -ge 2 -and $name.StartsWith($element.ParameterName, [StringComparison]::OrdinalIgnoreCase)) {
                if ($null -ne $element.Argument) { $found.Add($element.Argument) }
                elseif ($i + 1 -lt $elements.Count -and $elements[$i + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $found.Add($elements[$i + 1]) }
                else { $found.Add($null) }
            }
        }
    }
    return , $found
}

function Get-PositionalCount($Command) {
    # Elements that are neither a parameter nor the value consumed by the preceding parameter. Switch
    # parameters (-Force, -NoTypeInformation) consume nothing, so a value following one is counted
    # as positional -- conservative for the mutators, which never need a positional argument.
    $switchNames = @('Force', 'NoTypeInformation', 'PassThru', 'Wait', 'NoNewWindow', 'Recurse', 'Confirm', 'WhatIf', 'Append', 'NoClobber')
    $elements = $Command.CommandElements
    $count = 0
    for ($i = 1; $i -lt $elements.Count; $i++) {
        $element = $elements[$i]
        if ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
            $isSwitch = @($switchNames | Where-Object { $_.StartsWith($element.ParameterName, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
            if ($null -eq $element.Argument -and -not $isSwitch -and $i + 1 -lt $elements.Count -and $elements[$i + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $i++ }
            continue
        }
        $count++
    }
    return $count
}

function Test-SafeChild($Ast) {
    if ($Ast -is [System.Management.Automation.Language.CommandExpressionAst]) { $Ast = $Ast.Expression }
    if ($Ast -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { return $false }
    $value = [string]$Ast.Value
    if ([string]::IsNullOrWhiteSpace($value)) { return $false }
    if ($value -match '\.\.|:|^[\\/]|[*?\[\]]') { return $false }
    return $true
}

function Get-AssignmentsTo($Root, [string]$Name) {
    @($Root.FindAll({
        param($n)
        if ($n -isnot [System.Management.Automation.Language.AssignmentStatementAst]) { return $false }
        $left = $n.Left
        if ($left -is [System.Management.Automation.Language.ConvertExpressionAst]) { $left = $left.Child }
        ($left -is [System.Management.Automation.Language.VariableExpressionAst]) -and ((Get-VariableName $left).name -ieq $Name)
    }, $true))
}

function Test-Provable($Ast, $Root, $Visiting) {
    if ($null -eq $Ast) { return $false }
    if ($Ast -is [System.Management.Automation.Language.CommandExpressionAst]) { return (Test-Provable $Ast.Expression $Root $Visiting) }
    if ($Ast -is [System.Management.Automation.Language.ParenExpressionAst]) { return (Test-Provable $Ast.Pipeline $Root $Visiting) }
    if ($Ast -is [System.Management.Automation.Language.PipelineAst]) {
        if ($Ast.PipelineElements.Count -ne 1) { return $false }
        return (Test-Provable $Ast.PipelineElements[0] $Root $Visiting)
    }
    if ($Ast -is [System.Management.Automation.Language.CommandAst]) {
        if ($Ast.GetCommandName() -cne 'Join-Path' -or $Ast.InvocationOperator -ne 'Unknown') { return $false }
        $elements = @($Ast.CommandElements | Select-Object -Skip 1)
        # Exactly `Join-Path <base> <child>` (positional), nothing else.
        if ($elements.Count -ne 2) { return $false }
        if (@($elements | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] }).Count -gt 0) { return $false }
        if (-not (Test-SafeChild $elements[1])) { return $false }
        return (Test-Provable $elements[0] $Root $Visiting)
    }
    if ($Ast -is [System.Management.Automation.Language.VariableExpressionAst]) {
        if ($Ast.Splatted) { return $false }
        $v = Get-VariableName $Ast
        if ($v.scope -ne '') { return $false }
        if ($v.name -eq 'null') { return $true }
        if ($v.name -ceq 'Work') { return $true }   # R5 separately proves $Work's single canonical assignment
        if (-not $Visiting.Add($v.name.ToLowerInvariant())) { return $false }
        try {
            $assignments = @(Get-AssignmentsTo $Root $v.name)
            if ($assignments.Count -eq 0) { return $false }
            foreach ($a in $assignments) {
                if ($a.Operator -ne [System.Management.Automation.Language.TokenKind]::Equals) { return $false }
                if ($a.Left -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return $false }
                if ((Get-VariableName $a.Left).scope -ne '') { return $false }
                if (-not (Test-Provable $a.Right $Root $Visiting)) { return $false }
            }
            return $true
        } finally {
            [void]$Visiting.Remove($v.name.ToLowerInvariant())
        }
    }
    return $false
}

function Invoke-Scan([string]$Source, [string]$Text, [string[]]$ModuleFunctions, [switch]$FunctionBody) {
    $tokens = $null; $errors = $null
    $root = [System.Management.Automation.Language.Parser]::ParseInput($Text, [ref]$tokens, [ref]$errors)
    foreach ($err in @($errors)) {
        $violations.Add([ordered]@{ source = $Source; line = $err.Extent.StartLineNumber; rule = 'parse'; text = $err.Message; reason = 'template does not parse' })
    }
    if (-not $FunctionBody) { $scanned.Add($Source) }
    # R8 directives: `using module/assembly/namespace` and `#requires` pull in code the lint never sees.
    foreach ($using in @($root.UsingStatements)) {
        Add-Violation $Source $using 'R8' 'using statements are not allowed in a job template'
    }
    if ($null -ne $root.ScriptRequirements) {
        Add-Violation $Source $root 'R8' '#requires is not allowed in a job template'
    }
    $templateFunctions = @($root.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object { $_.Name })
    $known = @($allowedCmdlets + $templateFunctions + $ModuleFunctions)

    # R1 / R2 / R3
    foreach ($command in $root.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
        foreach ($element in $command.CommandElements) {
            if ($element -is [System.Management.Automation.Language.VariableExpressionAst] -and $element.Splatted) {
                Add-Violation $Source $command 'R2' 'splatting hides parameter names'
            }
        }
        $first = $command.CommandElements[0]
        if ($command.InvocationOperator -eq 'Dot') { Add-Violation $Source $command 'R3' 'dot-sourcing is not allowed'; continue }
        if ($command.InvocationOperator -eq 'Ampersand') {
            $ok = $false
            if ($first -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                $ok = ([string]$first.Value) -match '\.exe$'
            } elseif ($first -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
                $ok = ([string]$first.Value) -match '\.exe$'
            } elseif ($first -is [System.Management.Automation.Language.VariableExpressionAst]) {
                $v = Get-VariableName $first
                $ok = ($v.scope -eq '' -and $childExecutables -ccontains $v.name)
            } elseif ($first -is [System.Management.Automation.Language.MemberExpressionAst] -and -not $first.Static -and
                      $first.Expression -is [System.Management.Automation.Language.VariableExpressionAst]) {
                $v = Get-VariableName $first.Expression
                $ok = ($v.scope -eq '' -and $childExecutables -ccontains $v.name -and $first.Member.Extent.Text -ceq 'FullName')
            }
            if (-not $ok) { Add-Violation $Source $command 'R3' 'call operator on something other than an allowlisted child executable' }
            continue
        }
        $name = $command.GetCommandName()
        if ([string]::IsNullOrEmpty($name) -or $first -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or
            $first.StringConstantType -ne 'BareWord') {
            Add-Violation $Source $command 'R1' 'command name is not a bare literal'
            continue
        }
        if ($known -cnotcontains $name) {
            Add-Violation $Source $command 'R1' "'$name' is not an allowlisted cmdlet, template function or module function (exact case)"
            continue
        }
        $key = $name.ToLowerInvariant()
        if ($key -eq 'foreach-object') {
            # `ForEach-Object Delete` / `-MemberName Delete` invokes a method by NAME, invisible to R4.
            $arguments = @($command.CommandElements | Select-Object -Skip 1)
            $onlyBlocks = @($arguments | Where-Object {
                -not ($_ -is [System.Management.Automation.Language.ScriptBlockExpressionAst])
            }).Count -eq 0
            if (-not $onlyBlocks) {
                Add-Violation $Source $command 'R2' 'ForEach-Object may only take script blocks (no -MemberName, no member-name string)'
            }
        }
        if ($key -eq 'new-item') {
            if ((Get-NamedArguments $command @('Name')).Count -gt 0) {
                Add-Violation $Source $command 'R2' 'New-Item -Name is not allowed (it can traverse out of -Path)'
            }
            # Only a plain directory or file; a SymbolicLink/Junction/HardLink under $Work would let a
            # later provable write land outside it.
            $types = Get-NamedArguments $command @('ItemType')
            $typeOk = $types.Count -eq 1 -and $null -ne $types[0] -and
                ($types[0] -is [System.Management.Automation.Language.StringConstantExpressionAst]) -and
                (@('Directory', 'File') -ccontains [string]$types[0].Value)
            if (-not $typeOk) {
                Add-Violation $Source $command 'R2' "New-Item must name -ItemType 'Directory' or 'File' literally"
            }
        }
        if (@('Remove-AttrCudaTree', 'Remove-AttrCudaPartialFile', 'Remove-AttrCudaInputFileByContent', 'New-AttrCudaOwnedRoot') -ccontains $name) {
            $roots = Get-NamedArguments $command @('TrustedRoot')
            $rootOk = $roots.Count -eq 1 -and $null -ne $roots[0] -and (
                ($roots[0] -is [System.Management.Automation.Language.VariableExpressionAst] -and $roots[0].VariablePath.UserPath -ceq 'AgentRoot') -or
                ($roots[0] -is [System.Management.Automation.Language.StringConstantExpressionAst] -and [string]$roots[0].Value -ceq 'C:\mlvtmp'))
            if (-not $rootOk) {
                Add-Violation $Source $command 'R2' "$name -TrustedRoot must be `$AgentRoot or 'C:\mlvtmp'"
            }
        }
        if ($mutatorParams.ContainsKey($key)) {
            $destinations = Get-NamedArguments $command $mutatorParams[$key]
            if ($key -ne 'start-process') {
                if ((Get-PositionalCount $command) -gt 0) {
                    Add-Violation $Source $command 'R2' "$name has a positional argument; its destination must be named"
                }
                if ($destinations.Count -eq 0) {
                    Add-Violation $Source $command 'R2' "$name has no named destination"
                }
            }
            foreach ($destination in $destinations) {
                $visiting = [System.Collections.Generic.HashSet[string]]::new()
                if (-not (Test-Provable $destination $root $visiting)) {
                    Add-Violation $Source $command 'R2' "$name destination is not provably under `$Work"
                }
            }
        }
    }

    # R4 .NET members
    foreach ($member in $root.FindAll({ param($n) $n -is [System.Management.Automation.Language.MemberExpressionAst] }, $true)) {
        $memberName = $member.Member.Extent.Text
        if ($member.Static) {
            $typeText = if ($member.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) {
                $member.Expression.TypeName.FullName
            } else {
                '<non-type-expression>'
            }
            if ($allowedStatic -cnotcontains "${typeText}::$memberName") {
                Add-Violation $Source $member 'R4' "static member [$typeText]::$memberName is not allowlisted"
            }
        } elseif ($member -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) {
            if ($member.Member -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -or $allowedInstance -cnotcontains $memberName) {
                Add-Violation $Source $member 'R4' "instance method .$memberName() is not allowlisted"
            }
        }
    }

    # R5 assignments
    $workAssignments = @(Get-AssignmentsTo $root 'Work')
    if ($FunctionBody) {
        # A function body is not a job: the "$Work assigned exactly once, at top level" proof belongs to the template.
    } elseif ($workAssignments.Count -ne 1) {
        $anchor = if ($workAssignments.Count -gt 1) { $workAssignments[1] } else { $root }
        Add-Violation $Source $anchor 'R5' "`$Work must be assigned exactly once (found $($workAssignments.Count))"
    } else {
        $a = $workAssignments[0]
        $rhs = ($a.Right.Extent.Text -replace '\s+', ' ').Trim()
        if ($a.Operator -ne [System.Management.Automation.Language.TokenKind]::Equals -or
            $a.Left -isnot [System.Management.Automation.Language.VariableExpressionAst] -or
            (Get-VariableName $a.Left).scope -ne '' -or
            (Get-VariableName $a.Left).name -cne 'Work' -or
            $canonicalWork -cnotcontains $rhs -or
            $a.Parent.Parent -ne $root) {
            Add-Violation $Source $a 'R5' '$Work is not assigned once, at top level, in a canonical shape'
        }
    }
    foreach ($a in $root.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] }, $true)) {
        $left = $a.Left
        if ($left -is [System.Management.Automation.Language.ConvertExpressionAst]) { $left = $left.Child }
        if ($left -is [System.Management.Automation.Language.IndexExpressionAst]) {
            if ($left.Target -isnot [System.Management.Automation.Language.VariableExpressionAst] -or (Get-VariableName $left.Target).scope -ne '' -or
                $rootVariables -contains (Get-VariableName $left.Target).name) {
                Add-Violation $Source $a 'R5' 'index assignment target must be a plain unscoped non-root variable'
            }
            continue
        }
        if ($left -isnot [System.Management.Automation.Language.VariableExpressionAst]) {
            Add-Violation $Source $a 'R5' "assignment to $($left.GetType().Name) is not allowed"
            continue
        }
        $v = Get-VariableName $left
        if ($v.scope -eq '') {
            if ($rootVariables -contains $v.name -and $a.Operator -ne [System.Management.Automation.Language.TokenKind]::Equals) {
                Add-Violation $Source $a 'R5' "compound assignment to root variable `$$($v.name)"
            }
            continue
        }
        if ($v.scope -ieq 'script' -and $rootVariables -notcontains $v.name) { continue }
        if ($v.scope -ieq 'env' -and $allowedEnv -contains $v.name) {
            $visiting = [System.Collections.Generic.HashSet[string]]::new()
            if ($a.Operator -ne [System.Management.Automation.Language.TokenKind]::Equals -or -not (Test-Provable $a.Right $root $visiting)) {
                Add-Violation $Source $a 'R5' "`$env:$($v.name) must be assigned a provable path under `$Work"
            }
            continue
        }
        Add-Violation $Source $a 'R5' "scoped assignment `$$($v.scope):$($v.name) is not allowed"
    }

    # R6 redirections
    foreach ($redirect in $root.FindAll({ param($n) $n -is [System.Management.Automation.Language.FileRedirectionAst] }, $true)) {
        $visiting = [System.Collections.Generic.HashSet[string]]::new()
        if (-not (Test-Provable $redirect.Location $root $visiting)) {
            Add-Violation $Source $redirect 'R6' 'redirection target is not $null or provably under $Work'
        }
    }
}

$moduleFunctions = @($ModulePath | ForEach-Object { Get-ModuleFunctionNames $_ })

foreach ($generator in $GeneratorPath) {
    $text = [IO.File]::ReadAllText($generator)
    $match = [regex]::Match($text, "(?s)\`$template = @'\r?\n(.*?)\r?\n'@")
    if (-not $match.Success) {
        $violations.Add([ordered]@{ source = $generator; line = 0; rule = 'template'; text = ''; reason = 'no $template here-string found' })
        continue
    }
    # Placeholders (__NAME__) are substituted by the generator, some outside quotes; a neutral
    # variable keeps the template parseable without granting it any proof.
    # DUAL-VENUE-EVIDENCE-1: __SCRATCH_ROOT__ is the job's trusted provisioning boundary (C:\mlvtmp
    # for bachelor, the venue table's scratchRoot for another venue -- the generator validates it
    # against the same drive-path allowlist as -AgentRoot). The scan proves the template's SHAPE, so it
    # reads that token as the literal boundary the R2/R5 rules name, not as an unproven placeholder.
    $shapeText = $match.Groups[1].Value.Replace('__SCRATCH_ROOT__', 'C:\mlvtmp')
    Invoke-Scan $generator ([regex]::Replace($shapeText, '__[A-Z0-9_]+__', '$attrCudaPlaceholder')) $moduleFunctions
}
foreach ($file in $TemplateFile) {
    Invoke-Scan $file ([IO.File]::ReadAllText($file)) $moduleFunctions
}

# ---- spliced helper bodies (-SpliceSource) ----
$spliced = [System.Collections.Generic.List[object]]::new()
$splicedViolations = [System.Collections.Generic.List[object]]::new()
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path.TrimEnd('\') + '\'

function Get-RepoRelativePath([string]$Path) {
    $full = (Resolve-Path -LiteralPath $Path).Path
    if ($full.StartsWith($repoRoot, [StringComparison]::OrdinalIgnoreCase)) { $full = $full.Substring($repoRoot.Length) }
    return ($full -replace '\\', '/')
}

function Add-SpliceFinding([string]$Source, [int]$Line, [string]$Function, [string]$Text, [string]$Reason) {
    $splicedViolations.Add([ordered]@{ source = $Source; module = ''; function = $Function; line = $Line; rule = 'splice'; token = 'splice'; text = $Text; reason = $Reason })
}

function Get-SpliceCallSites([string]$Path) {
    # Every Get-AttrCudaEmbeddedFunctionSource call in the file, with the literal function names it asks for.
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $Path).Path, [ref]$tokens, [ref]$errors)
    $source = Get-RepoRelativePath $Path
    foreach ($err in @($errors)) {
        Add-SpliceFinding $source $err.Extent.StartLineNumber '' $err.Message 'splice source does not parse; its splice call sites cannot be enumerated'
    }
    # Command names are case-insensitive and a module-qualified name (<Module>\Get-AttrCudaEmbeddedFunctionSource)
    # resolves to the same command: compare the part after the last backslash, ignoring case.
    $extractorCalls = $ast.FindAll({
        param($n)
        if ($n -isnot [System.Management.Automation.Language.CommandAst]) { return $false }
        $commandName = $n.GetCommandName()
        if ($null -eq $commandName) { return $false }
        return $commandName.Substring($commandName.LastIndexOf('\') + 1) -ieq 'Get-AttrCudaEmbeddedFunctionSource'
    }, $true)
    foreach ($call in $extractorCalls) {
        $text = ($call.Extent.Text -replace '\s+', ' ')
        if ($text.Length -gt 160) { $text = $text.Substring(0, 160) }
        $named = Get-NamedArguments $call @('Name')
        $argument = $null
        if ($named.Count -eq 1) { $argument = $named[0] }
        elseif ($named.Count -eq 0 -and $call.CommandElements.Count -gt 1 -and $call.CommandElements[1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $argument = $call.CommandElements[1] }
        $dynamic = $null -eq $argument -or @($argument.FindAll({
            param($n) ($n -is [System.Management.Automation.Language.VariableExpressionAst]) -or ($n -is [System.Management.Automation.Language.SubExpressionAst]) -or
                      ($n -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) -or ($n -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) -or
                      ($n -is [System.Management.Automation.Language.CommandAst])
        }, $true)).Count -gt 0
        $names = @()
        if (-not $dynamic) {
            $names = @($argument.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | ForEach-Object { [string]$_.Value })
            if ($argument -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $names = @([string]$argument.Value) }
        }
        if ($dynamic -or $names.Count -eq 0) {
            Add-SpliceFinding $source $call.Extent.StartLineNumber '' $text 'the -Name list is not a literal list of function names, so the spliced bodies cannot be enumerated'
            continue
        }
        [pscustomobject]@{ source = $source; line = $call.Extent.StartLineNumber; text = $text; names = $names }
    }
}

if ($SpliceSource.Count -gt 0) {
    $moduleTexts = [ordered]@{}
    foreach ($modulePath in $ModulePath) { $moduleTexts[(Get-RepoRelativePath $modulePath)] = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $modulePath).Path) }
    $entries = [ordered]@{}
    foreach ($spliceSource in $SpliceSource) {
        foreach ($site in @(Get-SpliceCallSites $spliceSource)) {
            foreach ($functionName in $site.names) {
                # The extractor's own pattern (Get-AttrCudaEmbeddedFunctionSource in AttrCudaArtifacts.psm1).
                $pattern = '(?ms)^function\s+' + [regex]::Escape($functionName) + '\s*\{.*?^\}'
                $found = 0
                foreach ($module in $moduleTexts.Keys) {
                    $match = [regex]::Match($moduleTexts[$module], $pattern)
                    if (-not $match.Success) { continue }
                    $found++
                    $key = "$module::$functionName"
                    if (-not $entries.Contains($key)) {
                        $line = 1 + ([regex]::Matches($moduleTexts[$module].Substring(0, $match.Index), "`n")).Count
                        $entries[$key] = [ordered]@{ module = $module; name = $functionName; line = $line; body = $match.Value; callers = [System.Collections.Generic.List[string]]::new() }
                    }
                    if (-not $entries[$key].callers.Contains($site.source)) { $entries[$key].callers.Add($site.source) }
                }
                if ($found -eq 0) {
                    Add-SpliceFinding $site.source $site.line $functionName $site.text "no -ModulePath module defines an extractable function named '$functionName'"
                }
            }
        }
    }
    foreach ($entry in $entries.Values) {
        $bytes = [Text.Encoding]::UTF8.GetBytes([string]$entry.body)
        $sha = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace('-', '').ToLowerInvariant()
        $spliced.Add([ordered]@{ module = $entry.module; name = $entry.name; line = $entry.line; bodyLength = $bytes.Length; bodySha256 = $sha; callers = @($entry.callers) })
        $before = $violations.Count
        $script:bodyMode = $true
        try { Invoke-Scan "$($entry.module)::$($entry.name)" ([string]$entry.body) $moduleFunctions -FunctionBody } finally { $script:bodyMode = $false }
        foreach ($finding in @($violations.GetRange($before, $violations.Count - $before))) {
            $token = if ($finding.Contains('token')) { $finding.token } else { [string]$finding.rule }
            $absolute = if ($finding.line -ge 1) { $entry.line + $finding.line - 1 } else { $entry.line }
            $splicedViolations.Add([ordered]@{ source = $finding.source; module = $entry.module; function = $entry.name; line = $absolute; rule = $finding.rule; token = $token; text = $finding.text; reason = $finding.reason })
        }
        $violations.RemoveRange($before, $violations.Count - $before)
    }
}

[ordered]@{ templates = @($scanned); violations = @($violations); spliced = @($spliced); splicedViolations = @($splicedViolations) } | ConvertTo-Json -Depth 6
