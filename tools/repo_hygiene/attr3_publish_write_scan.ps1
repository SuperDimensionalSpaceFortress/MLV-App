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
# is not literal is a 'splice' finding, never a silent skip), reads each body with the extractor's own
# regex, and runs R1 R2 R3 R4 R5 R6 over it exactly as over a template, minus the "$Work assigned once
# at top level" half of R5 (a function body is not a job). Findings are reported separately as
# splicedViolations with the function, its module line and the offending token; the template verdict in
# `violations` is unchanged.
#
# MODULE BINDING (ATTR3-SCAN-COVERAGE-LIMITS-1 L2). A call is resolved against the module the extractor
# will read for THAT call, not against every -ModulePath module:
#   - no -ModulePath on the call, or an empty / blank / $null one (the extractor falls back to its default):
#     the -ModulePath module that defines the extractor itself; when no -ModulePath module defines it,
#     every -ModulePath module is a candidate;
#   - a rooted string literal, or exactly `Join-Path $PSScriptRoot '<child>'` (resolved against the
#     directory of the file that holds the call): the -ModulePath module with that FULL path, never one
#     with the same file name elsewhere; no such module is a 'splice' finding;
#   - any other -ModulePath expression (a variable, an index, a sub-expression, however path-like its
#     text): every -ModulePath module is a candidate, so the name must be defined by exactly one of them;
#   - a call that splats, or passes a second positional argument (which binds -ModulePath), or repeats
#     -ModulePath, or gives a relative literal path: a 'splice' finding, because its module is unknown.
# The extractor's parameters are matched the way PowerShell binds them, by any prefix (-M, -N), and a
# -Name that is anything but string constants in @( ) / , / parentheses (a + expression, a variable) is a
# 'splice' finding. Template parameters of New-Item, Expand-Archive, Export-Csv and Start-Process are
# likewise matched by prefix and by the cmdlet's own aliases (-RSO, -RSE, -LP).
# A name the bound module does not define, a name two candidates define, or a module the scan was not
# given is a 'splice' finding (fail closed): the scan never reads a same-named helper from a module the
# extractor will not read. A variable -ModulePath is bound only by uniqueness; the text of the temp file
# a job passes there is not read.
#
# PROPERTY SETS IN BODIES (ATTR3-SCAN-COVERAGE-LIMITS-1 L1). `$receiver.Member = ...` is an R5 finding
# (token = the target text), and in a spliced body each one carries an `origin` for the receiver:
# 'literal' / 'constructed:<Type>' when EVERY assignment to the receiver variable builds a fresh object
# (a hashtable, [ordered] or [pscustomobject] literal; New-Object <Type>; [Type]::new) and nothing else
# binds the variable. Otherwise it lists, ';'-joined: 'other' (a command or member result, a compound
# assignment, a foreach variable, a multi-target assignment, [ref]$v, -OutVariable / -ErrorVariable /
# ... or their aliases and prefixes), 'unbound' (a parameter, or no binding at all), 'undominated' (a
# fresh assignment exists but does not come first in a block that encloses the property set, or lies
# across a function boundary), 'automatic' (the engine binds the name: $_, $PSItem, $args, ...), and for
# a different receiver shape 'nested' (`$a.b.c = ...`), 'scoped' or 'complex'. Casts other than
# [ordered] / [pscustomobject] / [hashtable] around a literal build an instance of that type: 'other'.
# An assignment through another scope (`$script:r = ...`) is not a binding of this body's `$r`: 'other'.
# A setter inside a `trap` body, or inside a script block that is not an argument of a command, is not "after
# the assignment, in line": 'undominated'.
#
# SHADOWED COMMANDS (third adversary pass, A5/A6/A7). The scan evaluates Join-Path (a provable destination, a
# bound module), Split-Path and New-Object by their stock behaviour. A template or body that defines a function
# or Set-Alias / New-Alias with one of those names (or an allowlisted cmdlet's name) is an R1 finding, and a
# splice source that does so (or shadows the extractor itself) is a 'splice' finding, because the scan's own
# evaluation then says nothing about what the script runs. The alias's name is read from every spelling of -Name
# (positional, separated, colon form `-Name:Join-Path`, any unique prefix); a name that is not a string constant
# is a finding of its own kind, and so is a path or variable on the alias: / function: drives that names, or
# could name, such a command (`Set-Item alias:Join-Path`, `${function:Join-Path} = ...`). A rename, move or copy
# operand written as ('Join-Path'), $('Join-Path'), [string]'Join-Path' or 'Join-' + 'Path' is read as the constant
# it is; on those commands an operand the scan cannot reduce to a constant, next to a function: / alias: route or an
# evaluated name, is a finding of its own kind ('computed-rename'); so is an operand built only from literals that
# the scan cannot reduce (`('Join', 'Path') -join '-'`), whatever sits beside it, because the source text fixes its
# value. An operand holding a variable or a command is a run-time-built value and stays a declared limit. Left OPEN: a drive
# path built at run time (`'al' + 'ias:'`), Import-Alias, and a relative path after `Set-Location alias:`.
#
# $PSScriptRoot (r4, adversary r3 A2). `Join-Path $PSScriptRoot '<child>'` is read as the directory of the file that
# holds the call only while the file never rebinds $PSScriptRoot: an assignment (any scope qualifier, typed or compound),
# a multi-target assignment, a foreach variable, [ref], a parameter, -OutVariable and its relatives, Set-Variable /
# New-Variable / Remove-Variable / Clear-Variable (a name the scan cannot read counts), or the variable: drive makes the
# binding a 'splice' finding naming that reason. Parameter and variable names are compared without their scope
# qualifier (`param([T]$local:r)` binds $r).
#
# Provider paths and data sections (r5, Dot B5-B8). Every comparison of an alias: / function: / variable: tail reads it through
# Get-ProviderLeaf, which drops leading whitespace, './', '.\' and doubled separators and scope qualifiers, so
# `function:./Join-Path` and `variable:\\local:PSScriptRoot` name what `function:Join-Path` and `variable:PSScriptRoot` do. A
# route built from constants (`'fun' + 'ction:Join-Path'`) is read the same way; an empty or wildcard tail fails closed. A `data r
# { ... }` section binds $r like an assignment: the receiver origin is unknown, $PSScriptRoot is rebound, and a destination
# variable or $Work is no longer provable.
# Not covered, and left OPEN: a freshness proof that
# crosses a dynamic-scope or deferred-execution route this scan does not model (adversary A2-A5 inputs beyond the
# scoped, trap, deferred-block and shadowed-constructor shapes above).
# The origin is data for the caller; this
# script still decides nothing. Index assignments keep the template rule: any target that is not a plain
# unscoped non-root variable is a finding. The helpers that ARE the
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
#                splicedViolations: [ { source, module, function, line, rule, token, origin, text, reason } ] }.
# `origin` is the receiver origin of an R5 property set (see PROPERTY SETS IN BODIES), '' for every other finding.
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

function Add-Violation([string]$Source, $Ast, [string]$Rule, [string]$Reason, [string]$Origin = '') {
    $text = ($Ast.Extent.Text -replace '\s+', ' ')
    if ($text.Length -gt 160) { $text = $text.Substring(0, 160) }
    $record = [ordered]@{ source = $Source; line = $Ast.Extent.StartLineNumber; rule = $Rule; text = $text; reason = $Reason }
    if ($script:bodyMode) { $record['token'] = Get-ViolationToken $Ast; $record['origin'] = $Origin }
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
    if ($Ast -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return 'function ' + $Ast.Name }
    if ($Ast -is [System.Management.Automation.Language.FileRedirectionAst]) { return 'redirection' }
    return $Ast.GetType().Name
}

function Get-ShadowingDefinitions($Root, [string[]]$Names) {
    # Every function definition, and every Set-Alias / New-Alias, that gives one of $Names (case-insensitively, and
    # whatever its scope prefix) a meaning of the scanned script's own. The scan evaluates Join-Path, Split-Path and
    # New-Object by their stock behaviour (a provable destination, a bound module, a fresh object); a script that
    # redefines one of them makes that evaluation say nothing about what the script runs.
    # Each result is { Ast; Kind }: 'definition' (a function, or an alias whose name argument is a string constant naming
    # one of $Names), 'computed-alias' (an alias command whose name argument the scan cannot read as a string constant,
    # so it could name any command) or 'provider-path' (an alias: / function: drive path or variable that could define
    # one). Get-ShadowReason words each kind for the caller.
    $found = [System.Collections.Generic.List[object]]::new()
    $seen = @{}
    foreach ($n in @($Root.FindAll({ param($x) $true }, $true))) {
        $kind = Get-ShadowKind $n $Names
        if (-not $kind) { continue }
        $ast = $n
        if ($kind -eq 'provider-path') {
            # Report the statement that uses the path, not the bare string inside it.
            while ($null -ne $ast.Parent -and $ast -isnot [System.Management.Automation.Language.CommandAst] -and $ast -isnot [System.Management.Automation.Language.AssignmentStatementAst]) { $ast = $ast.Parent }
            if ($ast -isnot [System.Management.Automation.Language.CommandAst] -and $ast -isnot [System.Management.Automation.Language.AssignmentStatementAst]) { $ast = $n }
        }
        $key = "$kind|$($ast.Extent.StartOffset)|$($ast.Extent.EndOffset)"
        if ($seen.ContainsKey($key)) { continue }
        $seen[$key] = $true
        $found.Add([pscustomobject]@{ Ast = $ast; Kind = $kind })
    }
    return $found.ToArray()
}

function Get-ShadowReason([string]$Kind, [string]$Consequence) {
    switch ($Kind) {
        'computed-alias' { return "an alias command whose name is not a string constant could shadow a command this scan evaluates$Consequence" }
        'provider-path' { return "an alias: or function: path could define a command (a shadow) named like one this scan evaluates$Consequence" }
        'computed-rename' { return "a rename, move or copy with an operand that is not a string constant could install a command (a shadow) named like one this scan evaluates$Consequence" }
        default { return "a function or alias named like a command this scan evaluates shadows it$Consequence" }
    }
}

function Test-ShadowedName([string]$Value, [string[]]$Names) {
    # A name with its scope prefix (global:Join-Path) removed, compared case-insensitively.
    @($Names | Where-Object { $_ -ieq ($Value -replace '^[A-Za-z]+:', '') }).Count -gt 0
}

function Get-AliasNameArguments($Command, [string]$Cmdlet = 'Set-Alias') {
    # What Set-Alias / New-Alias binds to -Name: every -Name argument (any unique prefix, separated or colon form), or,
    # when none, the first positional element -- skipping the value each preceding parameter consumes. A splat is returned
    # as itself (it can carry a name the scan cannot read).
    # -vb and -db are the common switches' aliases; read as value parameters they would swallow the name that follows.
    $switches = @('PassThru', 'Force', 'Confirm', 'WhatIf', 'Verbose', 'Debug', 'vb', 'db')
    $arguments = [System.Collections.Generic.List[object]]::new()
    $elements = $Command.CommandElements
    foreach ($element in @($elements | Select-Object -Skip 1)) {
        if ($element -is [System.Management.Automation.Language.VariableExpressionAst] -and $element.Splatted) { $arguments.Add($element) }
    }
    $named = Get-NamedArguments $Command @('Name') $Cmdlet
    foreach ($argument in $named) { if ($null -ne $argument) { $arguments.Add($argument) } }
    if ($named.Count -eq 0) {
        for ($i = 1; $i -lt $elements.Count; $i++) {
            $element = $elements[$i]
            if ($element -is [System.Management.Automation.Language.CommandParameterAst]) {
                $isSwitch = @($switches | Where-Object { $_.StartsWith($element.ParameterName, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
                if ($null -eq $element.Argument -and -not $isSwitch -and $i + 1 -lt $elements.Count -and $elements[$i + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $i++ }
                continue
            }
            if ($element -is [System.Management.Automation.Language.VariableExpressionAst] -and $element.Splatted) { continue }
            $arguments.Add($element)
            break
        }
    }
    return , $arguments
}

$script:providerRoutePattern = '^\s*(?:[A-Za-z0-9_.]+\\)?(?:alias|function)::?'

function Get-ProviderLeaf([string]$Path) {
    # What a provider path names once its drive or provider prefix is gone. PowerShell's globber drops leading whitespace, '.\' and
    # './' segments and doubled separators, and a scope qualifier (local: script: private: global:) may sit before or between
    # them ('./Join-Path', '\\Join-Path', '\\local:Join-Path', 'local:./Join-Path'). Every comparison of an alias: / function: /
    # variable: tail goes through here, so none of them reads './Join-Path' as a different name from 'Join-Path'.
    $text = $Path
    do {
        $previous = $text
        $text = $text -replace '^(?:\s|\.?[\\/])+', ''
        $text = $text -replace '^[A-Za-z]+:', ''
    } while ($text -cne $previous)
    return $text.Trim()
}

function Get-RoutedLeaf([string]$Value) {
    # $null when $Value is not an alias: / function: route, otherwise the normalized name the route ends in.
    if ($Value -notmatch $script:providerRoutePattern) { return $null }
    return (Get-ProviderLeaf ($Value -replace $script:providerRoutePattern, ''))
}

function Test-RouteTailUnproven([string]$Leaf, [string[]]$Names) {
    # Nothing after the drive, a wildcard or a computed part: the name comes from somewhere else, so it is unproven (fails closed).
    return ($Leaf -eq '' -or $Leaf -match '[*?\[\]$(]' -or (Test-ShadowedName $Leaf $Names))
}

function Get-UnwrappedOperand($Ast) {
    # The expression inside any parentheses, one-expression sub-expression or @( ), single-statement pipeline and [string] cast
    # around it; the node itself when it is none of those (or holds more than one statement). @('x') binds a string parameter as
    # 'x'; more elements, or none, are not one value.
    while ($true) {
        if ($Ast -is [System.Management.Automation.Language.ParenExpressionAst]) { $Ast = $Ast.Pipeline; continue }
        if ($Ast -is [System.Management.Automation.Language.SubExpressionAst] -or $Ast -is [System.Management.Automation.Language.ArrayExpressionAst]) {
            if ($Ast.SubExpression.Statements.Count -ne 1) { break }
            $Ast = $Ast.SubExpression.Statements[0]; continue
        }
        if ($Ast -is [System.Management.Automation.Language.PipelineAst]) {
            if ($Ast.PipelineElements.Count -ne 1) { break }
            $Ast = $Ast.PipelineElements[0]; continue
        }
        if ($Ast -is [System.Management.Automation.Language.CommandExpressionAst]) {
            if ($Ast.Redirections.Count -gt 0) { break }
            $Ast = $Ast.Expression; continue
        }
        if ($Ast -is [System.Management.Automation.Language.ConvertExpressionAst] -and $Ast.Type.TypeName.FullName -ieq 'string') { $Ast = $Ast.Child; continue }
        break
    }
    return $Ast
}

function Expand-Operands($Ast) {
    # An operand as the separate values it carries: a comma list (`'a', 'b'`, @('a', 'b')) is its elements, anything else itself.
    $inner = Get-UnwrappedOperand $Ast
    if ($inner -is [System.Management.Automation.Language.ArrayLiteralAst]) { return @($inner.Elements | ForEach-Object { Expand-Operands $_ }) }
    return @($Ast)
}

function Get-ConstantString($Ast) {
    # The string a command operand always evaluates to, or $null when the scan cannot prove one. Parentheses, a one-expression
    # sub-expression, @( ) of one element, a [string] cast, a quoted string with nothing to expand, a number, $true / $false /
    # $null and a '+' of such parts are all constants.
    $Ast = Get-UnwrappedOperand $Ast
    if ($Ast -is [System.Management.Automation.Language.ConstantExpressionAst]) { return [string]$Ast.Value }
    if ($Ast -is [System.Management.Automation.Language.VariableExpressionAst] -and -not $Ast.Splatted -and @('true', 'false', 'null') -contains $Ast.VariablePath.UserPath.ToLowerInvariant()) { return '' }
    if ($Ast -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
        if ($Ast.NestedExpressions.Count -eq 0) { return [string]$Ast.Value }
        return $null
    }
    if ($Ast -is [System.Management.Automation.Language.BinaryExpressionAst] -and $Ast.Operator -eq [System.Management.Automation.Language.TokenKind]::Plus) {
        $left = Get-ConstantString $Ast.Left
        if ($null -eq $left) { return $null }
        $right = Get-ConstantString $Ast.Right
        if ($null -eq $right) { return $null }
        return $left + $right
    }
    return $null
}

function Test-BuiltFromLiteralsOnly($Ast) {
    # True when nothing in the expression is decided at run time: no variable (other than $true / $false / $null), no command and
    # no sub-expression holding one. Its value is fixed by the source text, so a scan that cannot read that value fails closed;
    # an operand that DOES hold a variable or a command is a run-time-built value, a declared limit of this scan.
    foreach ($n in @($Ast.FindAll({ param($x) $x -is [System.Management.Automation.Language.VariableExpressionAst] -or $x -is [System.Management.Automation.Language.CommandAst] -or
                                           $x -is [System.Management.Automation.Language.ScriptBlockExpressionAst] -or $x -is [System.Management.Automation.Language.UsingExpressionAst] }, $true))) {
        if ($n -is [System.Management.Automation.Language.VariableExpressionAst] -and @('true', 'false', 'null') -contains $n.VariablePath.UserPath.ToLowerInvariant()) { continue }
        return $false
    }
    return $true
}

function Get-ShadowKind($Node, [string[]]$Names) {
    # '' when $Node shadows nothing, otherwise the kind Get-ShadowingDefinitions reports.
    $aliasCommands = @('set-alias', 'new-alias', 'sal', 'nal')
    if ($Node -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        if (Test-ShadowedName $Node.Name $Names) { return 'definition' }
        return ''
    }
    if ($Node -is [System.Management.Automation.Language.CommandAst]) {
        $commandName = $Node.GetCommandName()
        if ($null -eq $commandName) { return '' }
        # A module-qualified name (Microsoft.PowerShell.Utility\Set-Alias) is the same command.
        $commandName = $commandName.Substring($commandName.LastIndexOf('\') + 1)
        if ($aliasCommands -notcontains $commandName.ToLowerInvariant()) {
            # `Rename-Item -LiteralPath function:Redirect -NewName Join-Path`, `Move-Item function:Redirect New-Object`: the
            # alias: / function: path is the SOURCE and a constant elsewhere in the same command is the new name. An operand
            # written as ('Join-Path'), $('Join-Path') or 'Join-' + 'Path' is read as the constant it is; on a rename, move or
            # copy, an operand the scan cannot read as a constant, next to a route or an evaluated name, fails closed.
            $renameCommands = @('rename-item', 'rni', 'ren', 'move-item', 'mi', 'move', 'mv', 'copy-item', 'cpi', 'copy', 'cp')
            $routed = $false; $named = $false; $unreadable = $false; $fixedButUnread = $false
            foreach ($element in @($Node.CommandElements | Select-Object -Skip 1)) {
                $operandRoot = if ($element -is [System.Management.Automation.Language.CommandParameterAst]) { $element.Argument } else { $element }
                if ($null -eq $operandRoot) { continue }
                foreach ($operand in @(Expand-Operands $operandRoot)) {
                    $constant = Get-ConstantString $operand
                    if ($null -eq $constant) {
                        $unreadable = $true
                        if (Test-BuiltFromLiteralsOnly $operand) { $fixedButUnread = $true }
                        continue
                    }
                    if ($constant -match $script:providerRoutePattern) {
                        $routed = $true
                        # A plain string is its own node's finding; a reduced one ('fun'+'ction:Join-Path') has no node of its
                        # own, so its tail is read here (empty, wildcard or computed fails closed).
                        $plain = Get-UnwrappedOperand $operand
                        if ($plain -isnot [System.Management.Automation.Language.StringConstantExpressionAst] -and
                            $plain -isnot [System.Management.Automation.Language.ExpandableStringExpressionAst] -and
                            (Test-RouteTailUnproven (Get-RoutedLeaf $constant) $Names)) { $named = $true }
                    }
                    elseif (Test-ShadowedName $constant $Names) { $named = $true }
                }
            }
            if ($routed -and $named) { return 'provider-path' }
            if ($renameCommands -contains $commandName.ToLowerInvariant() -and ($fixedButUnread -or ($unreadable -and ($routed -or $named)))) { return 'computed-rename' }
            return ''
        }
        # Any constant element naming a shadowed command counts, whichever parameter it follows (the conservative reading).
        foreach ($element in @($Node.CommandElements | Select-Object -Skip 1)) {
            if ($element -is [System.Management.Automation.Language.StringConstantExpressionAst] -and (Test-ShadowedName ([string]$element.Value) $Names)) { return 'definition' }
        }
        $computed = $false
        foreach ($argument in (Get-AliasNameArguments $Node)) {
            if ($argument -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                if (Test-ShadowedName ([string]$argument.Value) $Names) { return 'definition' }
            }
            else { $computed = $true }
        }
        if ($computed) { return 'computed-alias' }
        return ''
    }
    # `Set-Item alias:Join-Path ...`, `New-Item -Path function: -Name Join-Path`, `${function:Join-Path} = ...`: the
    # alias: and function: drives (or the Alias:: / Function:: providers) define commands without naming Set-Alias.
    $routePattern = $script:providerRoutePattern
    if ($Node -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $Node -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
        $value = [string]$Node.Value
        $rest = Get-RoutedLeaf $value
        if ($null -eq $rest) { return '' }
        if (Test-RouteTailUnproven $rest $Names) { return 'provider-path' }
        return ''
    }
    if ($Node -is [System.Management.Automation.Language.VariableExpressionAst]) {
        $user = $Node.VariablePath.UserPath
        # ${function:\Join-Path} and ${function:/Join-Path} name the same command as ${function:Join-Path}.
        # A wildcard or empty tail (${function:Get-AttrCudaEmbeddedFunctionSourc?}) names a command the scan cannot read: it fails closed.
        if ($user -match '^(?i:alias|function):(.*)$' -and (Test-RouteTailUnproven (Get-ProviderLeaf $Matches[1]) $Names)) { return 'provider-path' }
    }
    return ''
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

function Get-NamedArguments($Command, [string[]]$Names, [string]$Cmdlet = '') {
    # PowerShell binds a parameter by any unique PREFIX of its name or of one of its aliases (-M for -ModulePath,
    # -RSO for Start-Process -RedirectStandardOutput), so a one-character prefix and the cmdlet's own aliases
    # count. -Cmdlet names the cmdlet whose aliases to add; a script function has none to look up.
    $found = [System.Collections.Generic.List[object]]::new()
    $elements = $Command.CommandElements
    $cmdletInfo = if ($Cmdlet) { Get-Command -Name $Cmdlet -CommandType Cmdlet, Function -ErrorAction SilentlyContinue | Select-Object -First 1 } else { $null }
    $spellings = @{}
    foreach ($name in $Names) {
        $spellings[$name] = @($name)
        if ($null -ne $cmdletInfo -and $cmdletInfo.Parameters.ContainsKey($name)) { $spellings[$name] = @($name) + @($cmdletInfo.Parameters[$name].Aliases) }
    }
    for ($i = 1; $i -lt $elements.Count; $i++) {
        $element = $elements[$i]
        if ($element -isnot [System.Management.Automation.Language.CommandParameterAst]) { continue }
        foreach ($name in $Names) {
            if (@($spellings[$name] | Where-Object { $_.StartsWith($element.ParameterName, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0 -and $element.ParameterName.Length -ge 1) {
                if ($null -ne $element.Argument) { $found.Add($element.Argument) }
                elseif ($i + 1 -lt $elements.Count -and $elements[$i + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $found.Add($elements[$i + 1]) }
                else { $found.Add($null) }
            }
        }
    }
    return , $found
}

function Get-PositionalCount($Command, [string[]]$SwitchNames = @()) {
    # Elements that are neither a parameter nor the value consumed by the preceding parameter. Switch
    # parameters (-Force, -NoTypeInformation) consume nothing, so a value following one is counted
    # as positional -- conservative for the mutators, which never need a positional argument.
    # -SwitchNames replaces the mutators' list with the switches of another command (the extractor's own are
    # Verbose and Debug), because a short prefix such as -N would otherwise be read as -NoNewWindow.
    $switchNames = if ($SwitchNames.Count -gt 0) { $SwitchNames } else { @('Force', 'NoTypeInformation', 'PassThru', 'Wait', 'NoNewWindow', 'Recurse', 'Confirm', 'WhatIf', 'Append', 'NoClobber', 'Verbose', 'Debug', 'vb', 'db') }
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

function Get-AssignedObjectOrigin($Assignment) {
    # What one assignment to a local variable binds it to: 'literal' (a hashtable / [ordered] /
    # [pscustomobject] literal, an in-memory object nothing else holds), 'constructed:<Type>' (New-Object or
    # [Type]::new), else 'other' (a command result, a parameter default, a compound assignment, ...).
    if ($Assignment.Operator -ne [System.Management.Automation.Language.TokenKind]::Equals) { return 'other' }
    # Only these casts keep a hashtable literal an in-memory PowerShell object; any other target type (`[EventLog]@{...}`)
    # builds an instance of that type, on the left (`[T]$v = @{...}`) as well as on the right.
    $literalTypes = @('ordered', 'pscustomobject', 'hashtable')
    if ($Assignment.Left -is [System.Management.Automation.Language.ConvertExpressionAst] -and
        $literalTypes -notcontains $Assignment.Left.Type.TypeName.FullName) { return 'other' }
    $node = $Assignment.Right
    while ($true) {
        if ($node -is [System.Management.Automation.Language.PipelineAst] -and $node.PipelineElements.Count -eq 1) { $node = $node.PipelineElements[0]; continue }
        if ($node -is [System.Management.Automation.Language.CommandExpressionAst]) { $node = $node.Expression; continue }
        break
    }
    if ($node -is [System.Management.Automation.Language.ConvertExpressionAst]) {
        if ($literalTypes -notcontains $node.Type.TypeName.FullName) { return 'other' }
        $node = $node.Child
    }
    if ($node -is [System.Management.Automation.Language.HashtableAst]) { return 'literal' }
    if ($node -is [System.Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Static -and
        $node.Member.Extent.Text -ceq 'new' -and $node.Expression -is [System.Management.Automation.Language.TypeExpressionAst]) {
        return 'constructed:' + $node.Expression.TypeName.FullName
    }
    if ($node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -ieq 'New-Object') {
        $typeArgs = Get-NamedArguments $node @('TypeName')
        $typeElement = $null
        if ($typeArgs.Count -eq 1 -and $null -ne $typeArgs[0]) { $typeElement = $typeArgs[0] }
        elseif ($typeArgs.Count -eq 0 -and $node.CommandElements.Count -gt 1 -and $node.CommandElements[1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $typeElement = $node.CommandElements[1] }
        if ($null -ne $typeElement -and $typeElement -is [System.Management.Automation.Language.StringConstantExpressionAst]) { return 'constructed:' + [string]$typeElement.Value }
    }
    return 'other'
}

function Get-ReceiverOrigin($Target, $Root) {
    # Where the object on the left of `$receiver.Member = ...` comes from, so the CALLER can tell a property set
    # on an in-memory object this body just built from one on a foreign object (`$f = Get-Item $Pub; $f.Attributes = ...`).
    # The scan reports the origin; it does not decide that one is acceptable.
    #   literal | constructed:<Type>   every assignment to the variable builds a fresh object (';'-joined if mixed)
    #   other                          some assignment binds a command / member / parameter-derived value
    #   unbound                        no assignment in the body: a parameter, loop variable or captured variable
    #   nested | scoped | complex      `$a.b.c = ...`, `$script:a.b = ...`, a receiver that is not a plain variable
    $node = $Target
    $depth = 0
    while ($node -is [System.Management.Automation.Language.MemberExpressionAst]) {
        if ($node -is [System.Management.Automation.Language.InvokeMemberExpressionAst]) { return 'complex' }
        $depth++
        $node = $node.Expression
    }
    if ($node -isnot [System.Management.Automation.Language.VariableExpressionAst]) { return 'complex' }
    $v = Get-VariableName $node
    if ($v.scope -ne '') { return 'scoped' }
    if ($depth -gt 1) { return 'nested' }
    # Variables the engine itself binds (the pipeline's $_ / $PSItem, $args, $input, ...) are never a fresh object.
    if (@('_', 'PSItem', 'input', 'args', 'this', 'matches', 'Error', 'foreach', 'switch', 'PSBoundParameters', 'MyInvocation',
          'PSCmdlet', 'ExecutionContext', 'Host', 'PWD', 'PID', 'null', 'true', 'false', 'PSScriptRoot', 'PSCommandPath',
          'Event', 'EventArgs', 'Sender', 'SourceArgs', 'SourceEventArgs', 'StackTrace', 'LASTEXITCODE') -contains $v.name) { return 'automatic' }
    # Only an UNSCOPED assignment (or `local:`) binds this body's variable; `$script:r = ...` / `$global:r = ...` binds
    # another scope's, so the receiver may still be a caller's: it counts as a binding of unknown origin.
    $allAssignments = @(Get-AssignmentsTo $Root $v.name)
    $assignments = @($allAssignments | Where-Object {
        $left = $_.Left
        if ($left -is [System.Management.Automation.Language.ConvertExpressionAst]) { $left = $left.Child }
        @('', 'local') -contains (Get-VariableName $left).scope.ToLowerInvariant()
    })
    $origins = @($assignments | ForEach-Object { Get-AssignedObjectOrigin $_ }) + @(Get-OtherBindings $Root $v.name)
    if ($allAssignments.Count -gt $assignments.Count) { $origins += 'other' }
    if ($origins.Count -eq 0) { return 'unbound' }
    # A receiver is fresh only if an assignment of a fresh object comes first, in a block that encloses the property
    # set and not across a function boundary; a later, conditional or nested-function assignment says nothing about the
    # object the property set reaches (it may be a caller's, found by dynamic scope).
    $freshAssignments = @($assignments | Where-Object { (Get-AssignedObjectOrigin $_) -ne 'other' })
    if ($freshAssignments.Count -gt 0 -and @($freshAssignments | Where-Object { Test-DominatesSetter $_ $Target }).Count -eq 0) { $origins += 'undominated' }
    return (@($origins | Sort-Object -Unique) -join ';')
}

function Test-DominatesSetter($Assignment, $Setter) {
    $block = $Assignment.Parent
    if ($block -isnot [System.Management.Automation.Language.StatementBlockAst] -and $block -isnot [System.Management.Automation.Language.NamedBlockAst]) { return $false }
    if ($Assignment.Extent.EndOffset -gt $Setter.Extent.StartOffset) { return $false }
    for ($cursor = $Setter.Parent; $null -ne $cursor; $cursor = $cursor.Parent) {
        if ([object]::ReferenceEquals($cursor, $block)) { return $true }
        if ($cursor -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $false }
        # A trap body runs when an error fires, possibly before the assignment; a script block that is not an argument
        # of a command is deferred until a caller invokes it. Neither is "after the assignment, in line".
        if ($cursor -is [System.Management.Automation.Language.TrapStatementAst]) { return $false }
        if ($cursor -is [System.Management.Automation.Language.ScriptBlockExpressionAst] -and
            $cursor.Parent -isnot [System.Management.Automation.Language.CommandAst]) { return $false }
    }
    return $false
}

function Test-DataStatementBinds($Node, [string]$Name) {
    # `data r { ... }` and `data r -SupportedCommand Get-Item { ... }` bind $r to the section's result, like an assignment does.
    if ($Node -isnot [System.Management.Automation.Language.DataStatementAst] -or [string]::IsNullOrEmpty($Node.Variable)) { return $false }
    return ([string]$Node.Variable -ieq $Name)
}

function Get-DataStatementsBinding($Root, [string]$Name) {
    @($Root.FindAll({ param($n) Test-DataStatementBinds $n $Name }, $true))
}

function Get-OtherBindings($Root, [string]$Name, [switch]$SkipVariableParameters) {
    # Every way a variable gets a value besides `$v = <expr>`: a parameter ('unbound'), and a foreach variable, a
    # multi-target assignment, [ref], and -OutVariable / -ErrorVariable / ... (including their aliases and
    # prefixes), which all bind a value of unknown origin ('other'). A -XxxVariable whose argument is not a
    # literal name could name any variable, so it counts against every receiver.
    $tags = [System.Collections.Generic.List[string]]::new()
    $variableParameters = @('OutVariable', 'ErrorVariable', 'WarningVariable', 'InformationVariable', 'PipelineVariable', 'ov', 'ev', 'wv', 'iv', 'pv')
    foreach ($node in $Root.FindAll({ param($n) $true }, $true)) {
        if ($node -is [System.Management.Automation.Language.ParameterAst]) {
            # `param([T]$local:r)` binds the same variable as `$r`: compare the name without its scope qualifier.
            if ((Get-VariableName $node.Name).name -ieq $Name) { $tags.Add('unbound') }
        } elseif ($node -is [System.Management.Automation.Language.ForEachStatementAst]) {
            if ((Get-VariableName $node.Variable).name -ieq $Name) { $tags.Add('other') }
        } elseif ($node -is [System.Management.Automation.Language.DataStatementAst]) {
            # `data r -SupportedCommand Get-Item { ... }` assigns the section's result to $r: a binding of unknown origin.
            if (Test-DataStatementBinds $node $Name) { $tags.Add('other') }
        } elseif ($node -is [System.Management.Automation.Language.AssignmentStatementAst]) {
            $left = $node.Left
            if ($left -is [System.Management.Automation.Language.ArrayLiteralAst] -or $left -is [System.Management.Automation.Language.ParenExpressionAst]) {
                $bound = @($left.FindAll({ param($n) $n -is [System.Management.Automation.Language.VariableExpressionAst] -and (Get-VariableName $n).name -ieq $Name }, $true))
                if ($bound.Count -gt 0) { $tags.Add('other') }
            }
        } elseif ($node -is [System.Management.Automation.Language.ConvertExpressionAst]) {
            if ($node.Type.TypeName.FullName -ieq 'ref' -and $node.Child -is [System.Management.Automation.Language.VariableExpressionAst] -and
                (Get-VariableName $node.Child).name -ieq $Name) { $tags.Add('other') }
        } elseif ($node -is [System.Management.Automation.Language.CommandAst] -and -not $SkipVariableParameters) {
            $elements = $node.CommandElements
            for ($i = 1; $i -lt $elements.Count; $i++) {
                $element = $elements[$i]
                if ($element -isnot [System.Management.Automation.Language.CommandParameterAst]) { continue }
                if (@($variableParameters | Where-Object { $_.StartsWith($element.ParameterName, [StringComparison]::OrdinalIgnoreCase) }).Count -eq 0) { continue }
                $value = $element.Argument
                if ($null -eq $value -and $i + 1 -lt $elements.Count -and $elements[$i + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $value = $elements[$i + 1] }
                if ($value -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
                    # -PipelineVariable local:r / -OutVariable +r name the variable without its scope or its append marker.
                    if ((([string]$value.Value).TrimStart('+') -replace '^[A-Za-z]+:', '') -ieq $Name) { $tags.Add('other') }
                } else {
                    $tags.Add('other')
                }
            }
        }
    }
    return $tags.ToArray()
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
            # A data section binds the variable to a value this proof cannot read.
            if (@(Get-DataStatementsBinding $Root $v.name).Count -gt 0) { return $false }
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
    foreach ($shadow in @(Get-ShadowingDefinitions $root @($allowedCmdlets + 'New-Object'))) {
        Add-Violation $Source $shadow.Ast 'R1' (Get-ShadowReason $shadow.Kind ', so the scan cannot trust its own evaluation of that command')
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
            if ((Get-NamedArguments $command @('Name') $name).Count -gt 0) {
                Add-Violation $Source $command 'R2' 'New-Item -Name is not allowed (it can traverse out of -Path)'
            }
            # Only a plain directory or file; a SymbolicLink/Junction/HardLink under $Work would let a
            # later provable write land outside it.
            $types = Get-NamedArguments $command @('ItemType') $name
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
            $destinations = Get-NamedArguments $command $mutatorParams[$key] $name
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
    foreach ($data in @(Get-DataStatementsBinding $root 'Work')) {
        Add-Violation $Source $data 'R5' '$Work is bound by a data statement, not assigned once in a canonical shape'
    }
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
            $origin = if ($left -is [System.Management.Automation.Language.MemberExpressionAst]) { Get-ReceiverOrigin $left $root } else { '' }
            Add-Violation $Source $a 'R5' "assignment to $($left.GetType().Name) is not allowed" $origin
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
    $splicedViolations.Add([ordered]@{ source = $Source; module = ''; function = $Function; line = $Line; rule = 'splice'; token = 'splice'; origin = ''; text = $Text; reason = $Reason })
}

function Get-PsScriptRootRebinding($Root) {
    # '' when $PSScriptRoot is provably the automatic value in this file, otherwise a short description of the first thing that
    # could rebind it. Any scope qualifier counts (`$script:PSScriptRoot = ...`), and so does every way a variable gets a value:
    # an assignment (plain, typed, compound), a multi-target assignment, a foreach variable, [ref], -OutVariable and its
    # relatives, a parameter, Set-Variable / New-Variable / Remove-Variable / Clear-Variable (a computed name could be it), and
    # the variable: drive. `Join-Path $PSScriptRoot '<child>'` is the generator's directory only while none of these appears.
    if (@(Get-AssignmentsTo $Root 'PSScriptRoot').Count -gt 0) { return 'an assignment' }
    if (@(Get-DataStatementsBinding $Root 'PSScriptRoot').Count -gt 0) { return 'a data statement' }
    if (@(Get-OtherBindings $Root 'PSScriptRoot' -SkipVariableParameters).Count -gt 0) { return 'a parameter, loop variable, multi-target assignment or [ref]' }
    # -OutVariable / -ErrorVariable / -WarningVariable / -InformationVariable / -PipelineVariable by name, alias or unique prefix
    # (-OutV, -ErrorV, -ev). A one-letter prefix of a native tool's switch (`git cat-file -e`) is not one of them, so the
    # shortest prefix read is the shortest unique among the common parameters; -p is read only when it names PSScriptRoot.
    $variableParameters = @(@{ Full = 'ErrorVariable'; Alias = 'ev'; Min = 6 }, @{ Full = 'WarningVariable'; Alias = 'wv'; Min = 8 }, @{ Full = 'InformationVariable'; Alias = 'iv'; Min = 12 },
                            @{ Full = 'OutVariable'; Alias = 'ov'; Min = 4 }, @{ Full = 'PipelineVariable'; Alias = 'pv'; Min = 1 })
    foreach ($command in @($Root.FindAll({ param($x) $x -is [System.Management.Automation.Language.CommandAst] }, $true))) {
        $elements = $command.CommandElements
        for ($i = 1; $i -lt $elements.Count; $i++) {
            $element = $elements[$i]
            if ($element -isnot [System.Management.Automation.Language.CommandParameterAst]) { continue }
            $spelling = $element.ParameterName
            $binds = @($variableParameters | Where-Object {
                $spelling -ieq $_.Alias -or ($spelling.Length -ge $_.Min -and $_.Full.StartsWith($spelling, [StringComparison]::OrdinalIgnoreCase))
            }).Count -gt 0
            if (-not $binds) { continue }
            $value = $element.Argument
            if ($null -eq $value -and $i + 1 -lt $elements.Count -and $elements[$i + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $value = $elements[$i + 1] }
            $constant = if ($null -ne $value) { Get-ConstantString $value } else { $null }
            if ($null -ne $constant) {
                if (($constant.TrimStart('+') -replace '^[A-Za-z]+:', '') -ieq 'PSScriptRoot') { return "-$spelling PSScriptRoot" }
            } elseif ($spelling.Length -gt 1) {
                return "-$spelling with a variable name the scan cannot read as a string constant"
            }
        }
    }
    $variableCommands = @('set-variable', 'sv', 'new-variable', 'nv', 'remove-variable', 'rv', 'clear-variable', 'clv')
    $variableRoute = '^\s*(?:[A-Za-z0-9_.]+\\)?variable::?\s*[\\/]?(.*)$'
    foreach ($n in @($Root.FindAll({ param($x) $true }, $true))) {
        if ($n -is [System.Management.Automation.Language.CommandAst]) {
            # A route built from constants ('vari' + 'able:PSScriptRoot') has no string node of its own; a plain string is read below.
            foreach ($element in @($n.CommandElements | Select-Object -Skip 1)) {
                $operandRoot = if ($element -is [System.Management.Automation.Language.CommandParameterAst]) { $element.Argument } else { $element }
                if ($null -eq $operandRoot) { continue }
                foreach ($operand in @(Expand-Operands $operandRoot)) {
                    $plain = Get-UnwrappedOperand $operand
                    if ($plain -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $plain -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) { continue }
                    $constant = Get-ConstantString $operand
                    if ($null -ne $constant -and $constant -match $variableRoute) {
                        $reducedRest = Get-ProviderLeaf $Matches[1]
                        if ($reducedRest -eq '' -or $reducedRest -match '[$(]' -or 'PSScriptRoot' -like $reducedRest) { return 'the variable: drive' }
                    }
                }
            }
            $commandName = $n.GetCommandName()
            if ($null -eq $commandName) { continue }
            $commandName = $commandName.Substring($commandName.LastIndexOf('\') + 1).ToLowerInvariant()
            if ($variableCommands -notcontains $commandName) { continue }
            foreach ($argument in (Get-AliasNameArguments $n 'Set-Variable')) {
                $constant = Get-ConstantString $argument
                if ($null -eq $constant) { return "$commandName with a variable name the scan cannot read as a string constant" }
                $bare = $constant -replace '^[A-Za-z]+:', ''
                if ('PSScriptRoot' -like $bare) { return "$commandName naming PSScriptRoot" }
            }
        }
        elseif ($n -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
            if (([string]$n.Value) -match $variableRoute) {
                # variable:local:PSScriptRoot / variable:\script:PSScriptRoot: the drive takes its own scope qualifier.
                # './PSScriptRoot', '.\PSScriptRoot' and '\\PSScriptRoot' name it too (Get-ProviderLeaf).
                $rest = Get-ProviderLeaf $Matches[1]
                if ($rest -eq '' -or $rest -match '[$(]' -or 'PSScriptRoot' -like $rest) { return 'the variable: drive' }
            }
        }
        elseif ($n -is [System.Management.Automation.Language.VariableExpressionAst]) {
            if ($n.VariablePath.UserPath -match '^(?i:variable):(.*)$' -and ('PSScriptRoot' -like (Get-ProviderLeaf $Matches[1]))) { return 'the variable: drive' }
        }
    }
    return ''
}

function Get-SpliceModuleBinding($Call, [string]$SourceDir, [string]$RootRebinding = '') {
    # Which module the extractor will read for this call: kind 'default' (no -ModulePath, or an empty / blank /
    # $null one: the extractor's own module), 'path' (a rooted literal or Join-Path $PSScriptRoot '<child>': bound
    # by FULL path), 'dynamic' (any other expression) or 'unsupported' (a call shape whose -ModulePath cannot be
    # read, so the binding is unknown).
    foreach ($element in $Call.CommandElements) {
        if ($element -is [System.Management.Automation.Language.VariableExpressionAst] -and $element.Splatted) {
            return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = 'the call splats arguments, which can hide a -ModulePath' }
        }
    }
    # -Name is position 0 and -ModulePath position 1, so a second positional argument binds -ModulePath.
    $expectedPositional = if ((Get-NamedArguments $Call @('Name')).Count -eq 0) { 1 } else { 0 }
    # The aliases -vb and -db are switches too; read as value parameters they would swallow the positional path.
    if ((Get-PositionalCount $Call @('Verbose', 'Debug', 'vb', 'db')) -gt $expectedPositional) {
        return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = 'the call passes a positional argument after -Name, which binds -ModulePath by position' }
    }
    $modulePaths = Get-NamedArguments $Call @('ModulePath')
    if ($modulePaths.Count -eq 0) { return [pscustomobject]@{ kind = 'default'; path = ''; reason = '' } }
    if ($modulePaths.Count -gt 1 -or $null -eq $modulePaths[0]) {
        return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = '-ModulePath is repeated or has no value' }
    }
    # Only two exact shapes are read as a path: a rooted string literal, and `Join-Path $PSScriptRoot '<child>'`
    # (resolved against the directory of the file that holds the call). Anything else, however path-like its
    # contents, is a computed value. An empty, blank or $null value makes the extractor use its default module.
    $node = $modulePaths[0]
    while ($true) {
        if ($node -is [System.Management.Automation.Language.ParenExpressionAst]) { $node = $node.Pipeline; continue }
        if ($node -is [System.Management.Automation.Language.PipelineAst] -and $node.PipelineElements.Count -eq 1) { $node = $node.PipelineElements[0]; continue }
        if ($node -is [System.Management.Automation.Language.CommandExpressionAst]) { $node = $node.Expression; continue }
        break
    }
    $dynamic = [pscustomobject]@{ kind = 'dynamic'; path = ''; reason = '' }
    if ($node -is [System.Management.Automation.Language.VariableExpressionAst] -and -not $node.Splatted -and $node.VariablePath.UserPath -ieq 'null') {
        return [pscustomobject]@{ kind = 'default'; path = ''; reason = '' }
    }
    $child = $null
    if ($node -is [System.Management.Automation.Language.StringConstantExpressionAst]) {
        if ([string]::IsNullOrWhiteSpace([string]$node.Value)) { return [pscustomobject]@{ kind = 'default'; path = ''; reason = '' } }
        # Fully qualified, not merely rooted: 'C:Clean.psm1' (drive-relative) and '\Clean.psm1' (root-relative) are rooted
        # yet resolve against the working directory of whoever runs them.
        if (-not [IO.Path]::IsPathFullyQualified([string]$node.Value)) {
            return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = '-ModulePath is a relative literal path, which depends on the working directory' }
        }
        try { return [pscustomobject]@{ kind = 'path'; path = [IO.Path]::GetFullPath([string]$node.Value); reason = '' } }
        catch { return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = '-ModulePath is not a valid path' } }
    }
    if ($node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -ieq 'Join-Path' -and $node.InvocationOperator -eq 'Unknown') {
        $parts = @($node.CommandElements | Select-Object -Skip 1)
        if ($parts.Count -eq 2 -and @($parts | Where-Object { $_ -is [System.Management.Automation.Language.CommandParameterAst] }).Count -eq 0 -and
            $parts[0] -is [System.Management.Automation.Language.VariableExpressionAst] -and $parts[0].VariablePath.UserPath -ieq 'PSScriptRoot' -and
            $parts[1] -is [System.Management.Automation.Language.StringConstantExpressionAst] -and -not [string]::IsNullOrWhiteSpace([string]$parts[1].Value)) {
            $child = [string]$parts[1].Value
        }
    }
    if ($null -ne $child -and $RootRebinding) {
        $rebound = '$PSScriptRoot is rebound in the file that holds the call (via ' + $RootRebinding + '), so Join-Path $PSScriptRoot ''' + $child + ''' is not the generator''s own directory'
        return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = $rebound }
    }
    if ($null -ne $child -and $SourceDir) {
        try { return [pscustomobject]@{ kind = 'path'; path = [IO.Path]::GetFullPath((Join-Path $SourceDir $child)); reason = '' } }
        catch { return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = '-ModulePath is not a valid path' } }
    }
    return $dynamic
}

function Get-SpliceCallSites([string]$Path) {
    # Every Get-AttrCudaEmbeddedFunctionSource call in the file, with the literal function names it asks for.
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $Path).Path, [ref]$tokens, [ref]$errors)
    $source = Get-RepoRelativePath $Path
    $sourceDir = Split-Path -Parent (Resolve-Path -LiteralPath $Path).Path
    foreach ($err in @($errors)) {
        Add-SpliceFinding $source $err.Extent.StartLineNumber '' $err.Message 'splice source does not parse; its splice call sites cannot be enumerated'
    }
    # The binding below evaluates Join-Path, and the call itself is Get-AttrCudaEmbeddedFunctionSource: a source that
    # defines its own version of either (or of Split-Path / New-Object) makes the scan's evaluation say nothing about it.
    foreach ($shadow in @(Get-ShadowingDefinitions $ast @('Join-Path', 'Split-Path', 'New-Object', 'Get-AttrCudaEmbeddedFunctionSource'))) {
        $shadowText = ($shadow.Ast.Extent.Text -replace '\s+', ' ')
        if ($shadowText.Length -gt 160) { $shadowText = $shadowText.Substring(0, 160) }
        Add-SpliceFinding $source $shadow.Ast.Extent.StartLineNumber '' $shadowText (Get-ShadowReason $shadow.Kind ', so the module binding it computed cannot be trusted')
    }
    $rootRebinding = Get-PsScriptRootRebinding $ast
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
        # A literal list is built only from string constants, @( ) / , and parentheses; any other node (a variable, a
        # + or -f expression, an index, a sub-expression, a command) computes the names, so they cannot be enumerated.
        $dynamic = $null -eq $argument -or @($argument.FindAll({
            param($n) -not (($n -is [System.Management.Automation.Language.StringConstantExpressionAst]) -or ($n -is [System.Management.Automation.Language.ArrayLiteralAst]) -or
                      ($n -is [System.Management.Automation.Language.ArrayExpressionAst]) -or ($n -is [System.Management.Automation.Language.StatementBlockAst]) -or
                      ($n -is [System.Management.Automation.Language.PipelineAst]) -or ($n -is [System.Management.Automation.Language.CommandExpressionAst]) -or
                      ($n -is [System.Management.Automation.Language.ParenExpressionAst]))
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
        $callName = $call.GetCommandName()
        $qualifier = if ($callName.Contains('\')) { $callName.Substring(0, $callName.LastIndexOf('\')) } else { '' }
        [pscustomobject]@{ source = $source; line = $call.Extent.StartLineNumber; text = $text; names = $names; qualifier = $qualifier; bind = (Get-SpliceModuleBinding $call $sourceDir $rootRebinding) }
    }
}

if ($SpliceSource.Count -gt 0) {
    $moduleTexts = [ordered]@{}
    $moduleFullPaths = @{}
    foreach ($modulePath in $ModulePath) {
        $key = Get-RepoRelativePath $modulePath
        $full = (Resolve-Path -LiteralPath $modulePath).Path
        $moduleTexts[$key] = [IO.File]::ReadAllText($full)
        $moduleFullPaths[$key] = [IO.Path]::GetFullPath($full)
    }
    $entries = [ordered]@{}
    # The module the extractor reads when a call names no -ModulePath is the module that defines the extractor
    # ($script:AttrCudaModulePath). When no -ModulePath module defines it (a fixture), every module is a candidate.
    # Found by parsing, not by text, so a comment or a here-string that merely contains the definition does not count.
    $extractorModules = @($moduleTexts.Keys | Where-Object { @(Get-ModuleFunctionNames $moduleFullPaths[$_]) -contains 'Get-AttrCudaEmbeddedFunctionSource' })
    foreach ($spliceSource in $SpliceSource) {
        foreach ($site in @(Get-SpliceCallSites $spliceSource)) {
            # Bind the call to the module(s) the extractor will really read, so a same-named helper in another
            # module is never scanned in its place. A call whose binding cannot be proven is a finding.
            $candidates = @($moduleTexts.Keys)
            $bindingFailure = ''
            switch ($site.bind.kind) {
                'unsupported' { $bindingFailure = $site.bind.reason }
                'default' {
                    if ($extractorModules.Count -eq 1) { $candidates = @($extractorModules) }
                    elseif ($extractorModules.Count -gt 1) { $bindingFailure = 'more than one -ModulePath module defines the extractor, so the default module is ambiguous' }
                }
                'path' {
                    # By FULL path: a same-named file in another directory is a different module.
                    $candidates = @($moduleTexts.Keys | Where-Object { $moduleFullPaths[$_] -ieq $site.bind.path })
                    if ($candidates.Count -ne 1) { $bindingFailure = "-ModulePath names '$($site.bind.path)', which is not a scanner -ModulePath module" }
                }
            }
            # <Module>\Get-AttrCudaEmbeddedFunctionSource runs THAT module's extractor. Only the real extractor module
            # (AttrCudaArtifacts) or the one module this call was bound to is the extractor this scan reads; any other
            # qualifier may run an extractor, and read a module, the scan never saw.
            if (-not $bindingFailure -and $site.qualifier -and $site.qualifier -ine 'AttrCudaArtifacts' -and
                -not ($candidates.Count -eq 1 -and [IO.Path]::GetFileNameWithoutExtension($candidates[0]) -ieq $site.qualifier)) {
                $bindingFailure = "the call is module-qualified by '$($site.qualifier)', which is not the module this scan binds"
            }
            if ($bindingFailure) {
                Add-SpliceFinding $site.source $site.line '' $site.text "the extractor module this call reads cannot be bound: $bindingFailure"
                continue
            }
            foreach ($functionName in $site.names) {
                # The extractor's own pattern (Get-AttrCudaEmbeddedFunctionSource in AttrCudaArtifacts.psm1).
                $pattern = '(?ms)^function\s+' + [regex]::Escape($functionName) + '\s*\{.*?^\}'
                $definers = @($candidates | Where-Object { [regex]::IsMatch($moduleTexts[$_], $pattern) })
                if ($definers.Count -eq 0) {
                    $where = if ($site.bind.kind -eq 'path' -or ($site.bind.kind -eq 'default' -and $extractorModules.Count -eq 1)) { "the module this call reads ($($candidates[0]))" } else { 'no -ModulePath module' }
                    Add-SpliceFinding $site.source $site.line $functionName $site.text "$where defines an extractable function named '$functionName'"
                    continue
                }
                if ($definers.Count -gt 1) {
                    Add-SpliceFinding $site.source $site.line $functionName $site.text "'$functionName' is defined by more than one candidate module ($($definers -join ', ')) and the call does not bind it to one"
                    continue
                }
                $module = $definers[0]
                $match = [regex]::Match($moduleTexts[$module], $pattern)
                $key = "$module::$functionName"
                if (-not $entries.Contains($key)) {
                    $line = 1 + ([regex]::Matches($moduleTexts[$module].Substring(0, $match.Index), "`n")).Count
                    $entries[$key] = [ordered]@{ module = $module; name = $functionName; line = $line; body = $match.Value; callers = [System.Collections.Generic.List[string]]::new() }
                }
                if (-not $entries[$key].callers.Contains($site.source)) { $entries[$key].callers.Add($site.source) }
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
            $origin = if ($finding.Contains('origin')) { [string]$finding.origin } else { '' }
            $splicedViolations.Add([ordered]@{ source = $finding.source; module = $entry.module; function = $entry.name; line = $absolute; rule = $finding.rule; token = $token; origin = $origin; text = $finding.text; reason = $finding.reason })
        }
        $violations.RemoveRange($before, $violations.Count - $before)
    }
}

[ordered]@{ templates = @($scanned); violations = @($violations); spliced = @($spliced); splicedViolations = @($splicedViolations) } | ConvertTo-Json -Depth 6
