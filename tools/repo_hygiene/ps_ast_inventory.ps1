# ps_ast_inventory.ps1 - LANE-HUBCHAT-DETACHED-DISPATCH-1 r15 (folds LANE-DIRTY-SAVE-CENSUS-PS-AST-1).
# Reads a JSON array of PowerShell source texts on stdin and writes a JSON array of inventories on stdout, one per text, in order.
# It only PARSES ([System.Management.Automation.Language.Parser]::ParseInput); nothing in a text is run, imported or compiled.
# The parser has already resolved backticks, whitespace, quoting and escapes, so a name read here is the name PowerShell binds, whatever its spelling.
# POLICY LIVES IN THE CALLER (tools/repo_hygiene/test_owner_footage_no_hardlink_class.py, lane_dirty_save_violations): this file only reports what is there.
# Each inventory:
#   errors      [{line, message}]                      parse errors (the caller fails closed on any)
#   commands    [{line, name, text, operator, head, head_var, pipeline, elements}]
#               name is the literal command name, or null when it is computed (a variable, an expression, an expandable string);
#               head is the AST type of the first element; head_var the unqualified variable name when that element is a variable;
#               elements (after the head): [{kind, param, param_arg, value, string_type, splatted}] (value: a string's text, an expandable string unexpanded)
#   members     [{line, name, kind, static, on, invoke, args}]  name null when the member is computed; on = the type literal a static member is read from;
#               args = AST types of the invoked arguments
#   functions   [{line, name}]
#   types       [{line, name, where}]                  type literals, type constraints and attributes
#   drive_vars  [{line, path, drive}]                  ${drive:name} references (function:, alias:, env:, variable:, ...)
#   assignments [{line, name, names, operator, right}] name = the one variable assigned (null otherwise); names = every variable on the left;
#                                                      right = AST type of the assigned expression
#   foreach_vars [{line, name}]
#   params      [{line, name, types}]
#   ref_vars    [{line, name}]                         [ref]$name
#   using       [{line, text}]                         using module / assembly / namespace statements
#   requires    {modules, assemblies, snapins}         #Requires entries that load code
#   other       [{line, kind}]                         class / enum / configuration / dynamic keyword definitions
# ASCII only. PowerShell 7.
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-UnqualifiedName($VariablePath) {
    $path = [string]$VariablePath.UserPath
    if ($VariablePath.IsDriveQualified) { return $path }
    return ($path -replace '^(?i)(global|script|local|private|using|workflow):', '')
}

function Get-ExpressionKind($Statement) {
    # the AST type a statement evaluates to, looking through a one-element pipeline and a command expression
    $node = $Statement
    if ($node -is [System.Management.Automation.Language.PipelineAst] -and $node.PipelineElements.Count -eq 1) { $node = $node.PipelineElements[0] }
    if ($node -is [System.Management.Automation.Language.CommandExpressionAst]) { $node = $node.Expression }
    return $node.GetType().Name
}

function Get-Inventory([string]$Text) {
    $tokens = $null
    $errors = $null
    $root = [System.Management.Automation.Language.Parser]::ParseInput($Text, [ref]$tokens, [ref]$errors)
    $inv = [ordered]@{
        errors = @(); commands = @(); members = @(); functions = @(); types = @(); drive_vars = @(); assignments = @()
        foreach_vars = @(); params = @(); ref_vars = @(); using = @(); requires = [ordered]@{ modules = 0; assemblies = 0; snapins = 0 }; other = @()
    }
    foreach ($e in @($errors)) { $inv.errors += [ordered]@{ line = $e.Extent.StartLineNumber; message = $e.Message } }
    foreach ($u in @($root.UsingStatements)) { $inv.using += [ordered]@{ line = $u.Extent.StartLineNumber; text = $u.Extent.Text } }
    $req = $root.ScriptRequirements
    if ($null -ne $req) {
        $inv.requires.modules = @($req.RequiredModules).Count
        $inv.requires.assemblies = @($req.RequiredAssemblies).Count
        $snapins = $req.PSObject.Properties['RequiresPSSnapIns']  # Windows PowerShell only
        if ($null -ne $snapins) { $inv.requires.snapins = @($snapins.Value).Count }
    }
    foreach ($node in $root.FindAll({ $true }, $true)) {
        $line = $node.Extent.StartLineNumber
        switch ($node) {
            { $_ -is [System.Management.Automation.Language.CommandAst] } {
                $head = $node.CommandElements[0]
                $headVar = $null
                if ($head -is [System.Management.Automation.Language.VariableExpressionAst] -and -not $head.Splatted) { $headVar = Get-UnqualifiedName $head.VariablePath }
                $elements = @()
                foreach ($el in @($node.CommandElements | Select-Object -Skip 1)) {
                    $row = [ordered]@{ kind = $el.GetType().Name; param = $null; param_arg = $null; value = $null; string_type = $null; splatted = $false }
                    if ($el -is [System.Management.Automation.Language.CommandParameterAst]) {
                        $row.param = $el.ParameterName
                        if ($null -ne $el.Argument) {
                            $row.param_arg = $el.Argument.GetType().Name
                            if ($el.Argument -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $row.value = $el.Argument.Value }
                        }
                    } elseif ($el -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                        $row.value = $el.Value
                        $row.string_type = $el.StringConstantType.ToString()
                    } elseif ($el -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
                        $row.value = $el.Value
                        $row.string_type = $el.StringConstantType.ToString()
                    } elseif ($el -is [System.Management.Automation.Language.VariableExpressionAst]) {
                        $row.splatted = [bool]$el.Splatted
                    }
                    $elements += $row
                }
                $pipeline = 0
                if ($node.Parent -is [System.Management.Automation.Language.PipelineAst]) { $pipeline = $node.Parent.PipelineElements.Count }
                $inv.commands += [ordered]@{
                    line = $line; name = $node.GetCommandName(); text = $head.Extent.Text; operator = $node.InvocationOperator.ToString()
                    head = $head.GetType().Name; head_var = $headVar; pipeline = $pipeline; elements = $elements
                }
                break
            }
            { $_ -is [System.Management.Automation.Language.MemberExpressionAst] } {
                $name = $null
                if ($node.Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $name = $node.Member.Value }
                $invoke = $node -is [System.Management.Automation.Language.InvokeMemberExpressionAst]
                $on = $null
                if ($node.Static -and $node.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) { $on = $node.Expression.TypeName.FullName }
                $argKinds = @()
                if ($invoke) { foreach ($a in @($node.Arguments)) { if ($null -ne $a) { $argKinds += $a.GetType().Name } } }
                $inv.members += [ordered]@{ line = $line; name = $name; kind = $node.Member.GetType().Name; static = [bool]$node.Static; on = $on; invoke = $invoke; args = $argKinds }
                break
            }
            { $_ -is [System.Management.Automation.Language.FunctionDefinitionAst] } { $inv.functions += [ordered]@{ line = $line; name = $node.Name }; break }
            { $_ -is [System.Management.Automation.Language.TypeExpressionAst] } { $inv.types += [ordered]@{ line = $line; name = $node.TypeName.FullName; where = 'literal' }; break }
            { $_ -is [System.Management.Automation.Language.TypeConstraintAst] } { $inv.types += [ordered]@{ line = $line; name = $node.TypeName.FullName; where = 'constraint' }; break }
            { $_ -is [System.Management.Automation.Language.AttributeAst] } { $inv.types += [ordered]@{ line = $line; name = $node.TypeName.FullName; where = 'attribute' }; break }
            { $_ -is [System.Management.Automation.Language.VariableExpressionAst] } {
                if ($node.VariablePath.IsDriveQualified) { $inv.drive_vars += [ordered]@{ line = $line; path = $node.VariablePath.UserPath; drive = $node.VariablePath.DriveName } }
                break
            }
            { $_ -is [System.Management.Automation.Language.AssignmentStatementAst] } {
                $left = $node.Left
                while ($left -is [System.Management.Automation.Language.ConvertExpressionAst]) { $left = $left.Child }
                $name = $null
                if ($left -is [System.Management.Automation.Language.VariableExpressionAst]) { $name = Get-UnqualifiedName $left.VariablePath }
                # every variable named anywhere on the left ($a, $b = ..., $x.P = ..., $h[$k] = ...), so a multiple assignment cannot hide a target
                $names = @($node.Left.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] }, $true) | ForEach-Object { Get-UnqualifiedName $_.VariablePath })
                $inv.assignments += [ordered]@{ line = $line; name = $name; names = $names; operator = $node.Operator.ToString(); right = (Get-ExpressionKind $node.Right) }
                break
            }
            { $_ -is [System.Management.Automation.Language.ForEachStatementAst] } { $inv.foreach_vars += [ordered]@{ line = $line; name = (Get-UnqualifiedName $node.Variable.VariablePath) }; break }
            { $_ -is [System.Management.Automation.Language.ParameterAst] } {
                $types = @($node.Attributes | Where-Object { $_ -is [System.Management.Automation.Language.TypeConstraintAst] } | ForEach-Object { $_.TypeName.FullName })
                $inv.params += [ordered]@{ line = $line; name = (Get-UnqualifiedName $node.Name.VariablePath); types = $types }
                break
            }
            { $_ -is [System.Management.Automation.Language.ConvertExpressionAst] } {
                if ($node.Type.TypeName.FullName -eq 'ref' -and $node.Child -is [System.Management.Automation.Language.VariableExpressionAst]) {
                    $inv.ref_vars += [ordered]@{ line = $line; name = (Get-UnqualifiedName $node.Child.VariablePath) }
                }
                break
            }
            { $_ -is [System.Management.Automation.Language.TypeDefinitionAst] -or $_ -is [System.Management.Automation.Language.DynamicKeywordStatementAst] -or $_ -is [System.Management.Automation.Language.ConfigurationDefinitionAst] } {
                $inv.other += [ordered]@{ line = $line; kind = $node.GetType().Name }
                break
            }
        }
    }
    return $inv
}

$texts = @([Console]::In.ReadToEnd() | ConvertFrom-Json -NoEnumerate)
if ($texts.Count -eq 1 -and $texts[0] -is [System.Array]) { $texts = @($texts[0]) }
$out = @(foreach ($t in $texts) {
    # one text that the inventory cannot read fails itself (an error row the caller reports), never the batch
    try { Get-Inventory ([string]$t) } catch { [ordered]@{ errors = @([ordered]@{ line = 0; message = "inventory failed: $($_.Exception.Message)" }); commands = @(); members = @(); functions = @(); types = @(); drive_vars = @(); assignments = @(); foreach_vars = @(); params = @(); ref_vars = @(); using = @(); requires = [ordered]@{ modules = 0; assemblies = 0; snapins = 0 }; other = @() } }
})
[Console]::Out.Write((ConvertTo-Json -InputObject $out -Depth 8 -Compress -EscapeHandling EscapeNonAscii))
