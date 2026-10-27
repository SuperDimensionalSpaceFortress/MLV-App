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
# regex, and runs R1 R2 R3 R4 R5 R6 R8 over it exactly as over a template, minus the "$Work assigned once
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
# The extractor's parameters are read only by their exact names or declared aliases (-Name, -ModulePath, the common
# parameters and -vb / -db / -ea ...); an abbreviation (-M, -Na) binds by prefix against a parameter set this scan does not
# prove, so it is an unproven binding (r9, below). A -Name that is anything but string constants in @( ) / , / parentheses
# (a + expression, a variable) is a 'splice' finding. Template parameters of New-Item, Expand-Archive, Export-Csv and Start-Process are
# likewise matched by prefix and by the cmdlet's own aliases (-RSO, -RSE, -LP).
# A name the bound module does not define, a name two candidates define, or a module the scan was not
# given is a 'splice' finding (fail closed): the scan never reads a same-named helper from a module the
# extractor will not read. A variable -ModulePath is bound only by uniqueness; the text of the temp file
# a job passes there is not read (a declared limit, left OPEN on ATTR3-SCAN-DYNAMIC-PATH-LITERAL-BIND-1).
#
# UNPROVEN BINDING (r9 hub ruling, the CLASS rule; opus r8 B1/B2, sol r8 L2). Eight rounds each closed one construct that
# changes WHICH module or object a name binds to at run time, and the next key found another of the same class. The rule is
# the class, not the instance: when the scan cannot prove a binding statically it reports an 'unproven-binding' finding
# (token 'unproven-binding'; rule 'splice' on a call or module, rule 'unproven-binding' in a template or body) and fails
# closed, instead of scanning the module or trusting the object it would merely assume. Every binding failure of a call above
# carries that token. The routes, one shared model (Get-VariableRouteBinding):
#   1. $PSDefaultParameterValues: ANY reference to it in a splice source, a -ModulePath module (its top-level code runs in the
#      generator's session), a template or a body -- an assignment, an index or member set, .Add(), a second variable holding
#      it, Set-Variable / the variable: drive / Get-Variable naming it (a wildcard or a name built from constants included), a
#      string that names it -- because a default it holds binds a parameter the command does not name
#      ('Get-AttrCudaEmbeddedFunctionSource:ModulePath', '*:ModulePath', 'Join-Path:AdditionalChildPath', 'New-Item:Name').
#   2. A variable bound by Set- / New- / Remove- / Clear- / Get-Variable (and sv, set, nv, rv, clv, gv; a name the scan cannot
#      read, a wildcard that matches, or no name at all), the variable: drive, a variable parameter by any prefix the command's
#      OWN parameter set accepts (Tee-Object -Va, Invoke-WebRequest -Ses), or the session-state API ($ExecutionContext,
#      .SessionState, .SessionStateProxy, .PSVariable, [psvariable]): for $PSScriptRoot (an extractor input) the call is
#      unproven; for an R5 receiver the origin gains 'unproven-binding' (never inert) and a separate finding names the route.
#   3. An extractor call that abbreviates a parameter or splats.
#   4. (r10, sol r9; key attack catalogue row S-06: "a literal source-defined variable-cmdlet alias -> follow its binding or report
#      unproven-binding".) Before any rule reads a command by name, the aliases the scanned file defines -- Set-Alias / New-Alias /
#      sal / nal, the alias: drive (New-Item, Set-Item, ${alias:x} = ...), a rename, copy or move on alias: or function: -- and, for a
#      splice source, the ones the -ModulePath modules' top-level code defines, are resolved, chains followed (Get-AliasContext), and
#      routes 2 and 1 and the shadow rule run on every name a command resolves to, so `Set-Alias Rebind Set-Variable; Rebind
#      PSScriptRoot ...` is judged as Set-Variable. An alias whose NAME is not a literal makes every command also read as its target.
#      An alias the scan cannot resolve (a target that is not a literal or that no command answers to, a splatted alias command,
#      Import-Alias, an alias: drive use or path it does not model, a rename fed a source it cannot read in a file that uses the alias:
#      drive, a function: drive body that is not a script block) makes every variable binding in the file unproven.
#   5. (r11, Dot B11, r10 adversary A1/A2.) A member name built only from literals (.('GetVariableValue'), .('Session'+'State'),
#      .$('PS'+'Variable')) is folded and judged as the literal name in route 2 (.InvokeCommand included), and a name built from
#      literals that folds to PSDefaultParameterValues is a route 1 reference. A member name the scan cannot fold, on a receiver
#      that may be the session ($PSCmdlet, $ExecutionContext, $Host, $MyInvocation, a variable bound from one, a type, a command
#      result; $_ / $input / $args in a tree that names one), makes every binding it could reach unproven. An alias a -ModulePath
#      module defines for a command the scan evaluates (Join-Path, Split-Path, New-Object, the extractor) makes every call in the
#      importing source unproven. OPEN (successor ATTR3-SCAN-IMPORT-FOLLOW-1): the aliases of a module a source imports from
#      OUTSIDE -ModulePath, or by a path the scan cannot read, are not followed.
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
# value; a cast over an array-valued operand (`[string]@('fun', 'ction:Join-Path')`) is never split and fails closed (OFS-dependent),
# on an item command (Set-Item, New-Item, ...) as on a rename. r7: a constant reads $true / $false as 'True' / 'False' after a string,
# and a '+' whose left is not a string (`1 + 2`, `$true + 'x'`) has no string value; an assignment target is stripped of EVERY
# type conversion and attribute (`[object][string]$x = ...`, `[ValidateNotNull()][string]$x = ...`) by Get-AssignmentTarget.
# An operand holding a variable or a command is a run-time-built value and stays a declared limit. Left OPEN: a drive
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
# splices into the emitted job, read with the extractor's own regex. Rules R1 to R6 and R8 above, and the
# UNPROVEN BINDING rule, apply to both; a spliced body skips only the top-level "$Work assigned once" half of R5.
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
# r9: Get-VariableParameterSpellings reads a command's own parameter set with module auto-loading off; the modules that hold the
# cmdlets with variable parameters of their own (Tee-Object, Invoke-WebRequest, ...) are loaded here so those resolve.
Import-Module Microsoft.PowerShell.Utility, Microsoft.PowerShell.Management -ErrorAction SilentlyContinue

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

function Add-Violation([string]$Source, $Ast, [string]$Rule, [string]$Reason, [string]$Origin = '', [string]$Token = '') {
    $text = ($Ast.Extent.Text -replace '\s+', ' ')
    if ($text.Length -gt 160) { $text = $text.Substring(0, 160) }
    $record = [ordered]@{ source = $Source; line = $Ast.Extent.StartLineNumber; rule = $Rule; text = $text; reason = $Reason }
    if ($script:bodyMode) { $record['token'] = if ($Token) { $Token } else { Get-ViolationToken $Ast }; $record['origin'] = $Origin }
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
        'computed-rename' { return "a rename, move, copy or item command with an operand that is not a string constant could install a command (a shadow) named like one this scan evaluates$Consequence" }
        default { return "a function or alias named like a command this scan evaluates shadows it$Consequence" }
    }
}

function Test-ShadowedName([string]$Value, [string[]]$Names) {
    # A name with its scope prefix (global:Join-Path) removed, compared case-insensitively, whole or (r12c adversary A3) after any
    # module qualifier: an alias named 'AttrCudaArtifacts\Get-AttrCudaEmbeddedFunctionSource' runs in place of that spelling.
    $plain = $Value -replace '^[A-Za-z]+:', ''
    $bare = $plain.Substring($plain.LastIndexOf('\') + 1)
    @($Names | Where-Object { $_ -ieq $plain -or $_ -ieq $bare }).Count -gt 0
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
        $text = $text -replace '^(?:\s|\.{1,2}[\\/]|[\\/])+', ''
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
    # Nothing after the drive, a wildcard, a computed part or a separator still inside the tail ('x/../Join-Path'): the name comes
    # from somewhere else, so it is unproven (fails closed).
    return ($Leaf -eq '' -or $Leaf -match '[*?\[\]$(\\/]' -or (Test-ShadowedName $Leaf $Names))
}

function Get-UnwrappedOperand($Ast, [ref]$PassedCast) {
    # The expression inside any parentheses, one-expression sub-expression or @( ), single-statement pipeline and [string] cast
    # around it; the node itself when it is none of those (or holds more than one statement). @('x') binds a string parameter as
    # 'x'; more elements, or none, are not one value. $PassedCast (optional) is set when a cast sat on the way in.
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
        if ($Ast -is [System.Management.Automation.Language.ConvertExpressionAst] -and $Ast.Type.TypeName.FullName -ieq 'string') {
            if ($null -ne $PassedCast) { $PassedCast.Value = $true }
            $Ast = $Ast.Child; continue
        }
        break
    }
    return $Ast
}

function Expand-Operands($Ast) {
    # An operand as the separate values it carries: a comma list (`'a', 'b'`, @('a', 'b')) is its elements, anything else itself.
    # A cast over a comma list is NOT split: `[string]@('fun', 'ction:Join-Path')` is one string, joined by $OFS at run time, so its
    # elements are not the values the command receives. It is returned whole; Get-ConstantString has no value for it, and the
    # callers read an operand built only from literals that has none as a fail-closed one. $OFS is never evaluated.
    $passedCast = $false
    $inner = Get-UnwrappedOperand $Ast ([ref]$passedCast)
    # An ELEMENT that is itself a comma list is not split either (r7, adversary A3): `(('variable:PSScript','Root'),'x')` binds to a
    # string[] whose first item is the inner list converted to ONE string (joined by $OFS), so it comes back whole.
    if ($inner -is [System.Management.Automation.Language.ArrayLiteralAst] -and -not $passedCast) {
        return @($inner.Elements | ForEach-Object {
            if ((Get-UnwrappedOperand $_) -is [System.Management.Automation.Language.ArrayLiteralAst]) { $_ } else { Expand-Operands $_ }
        })
    }
    return @($Ast)
}

function Get-ConstantTyped($Ast) {
    # A constant operand as @{ value = <its string>; kind = string | number | bool | null }, or $null when the scan cannot prove one.
    # The kind matters for '+': a string on the left concatenates the string form of the right ($true is 'True', $false 'False',
    # $null ''), but a number or a boolean on the left is ARITHMETIC (`1 + 2` is 3, not '12') or an error (`$true + 'x'`), and no
    # string comes out of either, so a '+' whose left is not a string has no constant value.
    $castToString = $false
    $Ast = Get-UnwrappedOperand $Ast ([ref]$castToString)
    $typed = $null
    if ($Ast -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $typed = @{ value = [string]$Ast.Value; kind = 'string' } }
    elseif ($Ast -is [System.Management.Automation.Language.ConstantExpressionAst]) { $typed = @{ value = [string]$Ast.Value; kind = 'number' } }
    elseif ($Ast -is [System.Management.Automation.Language.VariableExpressionAst] -and -not $Ast.Splatted) {
        switch ($Ast.VariablePath.UserPath.ToLowerInvariant()) {
            'true' { $typed = @{ value = 'True'; kind = 'bool' } }
            'false' { $typed = @{ value = 'False'; kind = 'bool' } }
            'null' { $typed = @{ value = ''; kind = 'null' } }
        }
    }
    elseif ($Ast -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
        if ($Ast.NestedExpressions.Count -eq 0) { $typed = @{ value = [string]$Ast.Value; kind = 'string' } }
    }
    elseif ($Ast -is [System.Management.Automation.Language.BinaryExpressionAst] -and $Ast.Operator -eq [System.Management.Automation.Language.TokenKind]::Plus) {
        $left = Get-ConstantTyped $Ast.Left
        $right = Get-ConstantTyped $Ast.Right
        if ($null -ne $left -and $null -ne $right -and $left.kind -eq 'string') { $typed = @{ value = $left.value + $right.value; kind = 'string' } }
    }
    if ($null -ne $typed -and $castToString) { $typed.kind = 'string' }
    return $typed
}

function Get-ConstantString($Ast) {
    # The string a command operand always evaluates to, or $null when the scan cannot prove one. Parentheses, a one-expression
    # sub-expression, @( ) of one element, a [string] cast, a quoted string with nothing to expand, a number, $true / $false /
    # $null (as 'True' / 'False' / '') and a '+' of such parts that starts with a string are all constants (Get-ConstantTyped).
    $typed = Get-ConstantTyped $Ast
    if ($null -eq $typed) { return $null }
    return $typed.value
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

$script:itemCommands = @('set-item', 'si', 'new-item', 'ni', 'clear-item', 'cli', 'remove-item', 'ri', 'rm', 'del', 'erase', 'rd', 'rmdir',
                         'set-itemproperty', 'sp', 'new-itemproperty', 'clear-itemproperty', 'clp', 'remove-itemproperty', 'rp')

function Test-DataLiteralOperand($Ast) {
    # A literal hashtable (not cast to a string) is a value handed to a command, never a name or a path it resolves. A cast over a
    # comma list is NOT one: it is a string, the very operand the r6 rule refuses to split.
    $cast = $false
    $inner = Get-UnwrappedOperand $Ast ([ref]$cast)
    return ($inner -is [System.Management.Automation.Language.HashtableAst] -and -not $cast)
}

function Get-ShadowKind($Node, [string[]]$Names) {
    # '' when $Node shadows nothing, otherwise the kind Get-ShadowingDefinitions reports.
    $aliasCommands = @('set-alias', 'new-alias', 'sal', 'nal')
    if ($Node -is [System.Management.Automation.Language.FunctionDefinitionAst]) {
        if (Test-ShadowedName $Node.Name $Names) { return 'definition' }
        return ''
    }
    if ($Node -is [System.Management.Automation.Language.CommandAst]) {
        $commandName = Get-LiteralCommandName $Node
        if ($null -eq $commandName) { return '' }
        # A module-qualified name (Microsoft.PowerShell.Utility\Set-Alias) is the same command, and so is an alias the source defines
        # for one (r10: `Set-Alias MakeAlias Set-Alias; MakeAlias Join-Path Redirect`): every name the command resolves to is read.
        $resolvedNames = @(Get-ResolvedCommandNames $Node)
        if (@($resolvedNames | Where-Object { $aliasCommands -contains $_ }).Count -eq 0) {
            # `Rename-Item -LiteralPath function:Redirect -NewName Join-Path`, `Move-Item function:Redirect New-Object`: the
            # alias: / function: path is the SOURCE and a constant elsewhere in the same command is the new name. An operand
            # written as ('Join-Path'), $('Join-Path') or 'Join-' + 'Path' is read as the constant it is; on a rename, move or
            # copy, an operand the scan cannot read as a constant, next to a route or an evaluated name, fails closed.
            $renameCommands = @('rename-item', 'rni', 'ren', 'move-item', 'mi', 'move', 'mv', 'copy-item', 'cpi', 'copy', 'cp')
            $routed = $false; $named = $false; $unreadable = $false; $fixedButUnread = $false
            # r9 adversary A2: pipeline input (`Get-Item function:Redirect | Rename-Item -NewName Join-Path`) is a source the scan
            # cannot read, so a constant new name next to it fails closed like one next to an unreadable operand.
            if ($Node.Parent -is [System.Management.Automation.Language.PipelineAst] -and $Node.Parent.PipelineElements.IndexOf($Node) -gt 0) { $unreadable = $true }
            foreach ($element in @($Node.CommandElements | Select-Object -Skip 1)) {
                $operandRoot = if ($element -is [System.Management.Automation.Language.CommandParameterAst]) { $element.Argument } else { $element }
                if ($null -eq $operandRoot) { continue }
                foreach ($operand in @(Expand-Operands $operandRoot)) {
                    $constant = Get-ConstantString $operand
                    if ($null -eq $constant) {
                        $unreadable = $true
                        if ((Test-BuiltFromLiteralsOnly $operand) -and -not (Test-DataLiteralOperand $operand)) { $fixedButUnread = $true }
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
            if (@($resolvedNames | Where-Object { $renameCommands -contains $_ }).Count -gt 0 -and ($fixedButUnread -or ($unreadable -and ($routed -or $named)))) { return 'computed-rename' }
            # An item command writes the alias: / function: drive directly (`Set-Item ([string]@('function:Join','-Path')) -Value {...}`),
            # so a path built only from literals that the scan cannot read fails closed here too, as it does on a rename.
            if (@($resolvedNames | Where-Object { $script:itemCommands -contains $_ }).Count -gt 0 -and $fixedButUnread) { return 'computed-rename' }
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
    if ($Ast -is [System.Management.Automation.Language.CommandExpressionAst]) {
        if ($Ast.Redirections.Count -gt 0) { return $false }
        $Ast = $Ast.Expression
    }
    if ($Ast -isnot [System.Management.Automation.Language.StringConstantExpressionAst]) { return $false }
    $value = [string]$Ast.Value
    if ([string]::IsNullOrWhiteSpace($value)) { return $false }
    if ($value -match '\.\.|:|^[\\/]|[*?\[\]]') { return $false }
    return $true
}

function Get-AssignmentTarget($Left) {
    # What an assignment writes to, with EVERY type conversion and attribute stripped: `[object][string]$x = ...` and
    # `[ValidateNotNull()][string]$x = ...` nest one wrapper per bracket, and each one is an AttributedExpressionAst (a
    # ConvertExpressionAst is the kind that carries a type). Stripping only the outermost leaves the next wrapper in place, and the
    # target then looks like something that is not a variable.
    while ($Left -is [System.Management.Automation.Language.AttributedExpressionAst]) { $Left = $Left.Child }
    return $Left
}

function Get-AssignmentConversionTypes($Left) {
    # The type names of every conversion wrapped around an assignment target, outermost first ([ValidateNotNull()] is not one).
    $types = [System.Collections.Generic.List[string]]::new()
    while ($Left -is [System.Management.Automation.Language.AttributedExpressionAst]) {
        if ($Left -is [System.Management.Automation.Language.ConvertExpressionAst]) { $types.Add($Left.Type.TypeName.FullName) }
        $Left = $Left.Child
    }
    return $types.ToArray()
}

function Get-AssignmentsTo($Root, [string]$Name) {
    @($Root.FindAll({
        param($n)
        if ($n -isnot [System.Management.Automation.Language.AssignmentStatementAst]) { return $false }
        $left = Get-AssignmentTarget $n.Left
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
    foreach ($type in (Get-AssignmentConversionTypes $Assignment.Left)) {
        if ($literalTypes -notcontains $type) { return 'other' }
    }
    # r9 adversary A2: an attribute on the target ([Borrow()]$r = @{}) is attached to the variable, and an
    # ArgumentTransformationAttribute replaces the value assigned (a class in the body can return Get-Item's FileInfo), so a target that
    # carries any attribute other than a conversion is not provably the literal on the right.
    for ($left = $Assignment.Left; $left -is [System.Management.Automation.Language.AttributedExpressionAst]; $left = $left.Child) {
        if ($left -isnot [System.Management.Automation.Language.ConvertExpressionAst]) { return 'other' }
    }
    $node = $Assignment.Right
    while ($true) {
        if ($node -is [System.Management.Automation.Language.PipelineAst] -and $node.PipelineElements.Count -eq 1) { $node = $node.PipelineElements[0]; continue }
        if ($node -is [System.Management.Automation.Language.CommandExpressionAst]) {
            if ($node.Redirections.Count -gt 0) { return 'other' }   # `@{} > $null` binds nothing
            $node = $node.Expression; continue
        }
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
    #   unproven-binding               a route this scan cannot follow may bind the variable (Get-VariableRouteBinding)
    $script:receiverRouteReason = ''
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
        $left = Get-AssignmentTarget $_.Left
        @('', 'local') -contains (Get-VariableName $left).scope.ToLowerInvariant()
    })
    $origins = @($assignments | ForEach-Object { Get-AssignedObjectOrigin $_ }) + @(Get-OtherBindings $Root $v.name)
    if ($allAssignments.Count -gt $assignments.Count) { $origins += 'other' }
    # r9 (opus r8 B1, the unproven-binding class rule): Set-Variable / New-Variable / Remove-Variable / Clear-Variable / Get-Variable,
    # the variable: drive, a variable parameter and the session-state API bind the receiver by a route this origin cannot follow, so
    # the receiver is unproven, never 'literal'. The reason is kept for the separate unproven-binding finding Invoke-Scan reports.
    $script:receiverRouteReason = Get-VariableRouteBinding $Root $v.name
    if ($script:receiverRouteReason) { $origins += 'unproven-binding' }
    if ($origins.Count -eq 0) { return 'unbound' }
    # A receiver is fresh only if an assignment of a fresh object comes first, in a block that encloses the property
    # set and not across a function boundary; a later, conditional or nested-function assignment says nothing about the
    # object the property set reaches (it may be a caller's, found by dynamic scope).
    $freshAssignments = @($assignments | Where-Object { (Get-AssignedObjectOrigin $_) -ne 'other' })
    if ($freshAssignments.Count -gt 0 -and @($freshAssignments | Where-Object { Test-DominatesSetter $_ $Target }).Count -eq 0) { $origins += 'undominated' }
    # A trap that continues resumes after a throwing statement: `trap { continue }; $r = @{ x = $(throw 'bad') }; $r.Value = 99`
    # reaches the setter although the assignment never ran, so with a trap in the body no assignment proves the receiver fresh.
    elseif ($freshAssignments.Count -gt 0 -and @($Root.FindAll({ param($n) $n -is [System.Management.Automation.Language.TrapStatementAst] }, $true)).Count -gt 0) { $origins += 'undominated' }
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
    $variableParameters = @('OutVariable', 'ErrorVariable', 'WarningVariable', 'InformationVariable', 'PipelineVariable', 'ov', 'ev', 'wv', 'iv', 'pv',
                            'Variable', 'SessionVariable', 'ResponseHeadersVariable', 'StatusCodeVariable')
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
            $left = Get-AssignmentTarget $node.Left
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

# ---- r10: source-defined aliases (sol r9 blocker; key attack catalogue row S-06) ----
# A literal alias the scanned source defines itself (`Set-Alias Rebind Set-Variable`) makes `Rebind` run Set-Variable, and every rule
# below that reads a command by its NAME (the variable cmdlets, the variable parameters, the item and rename commands, the alias commands
# Get-ShadowKind reads) would see `Rebind`. So every command name is first resolved through the aliases the source defines -- Set-Alias /
# New-Alias / sal / nal, the alias: drive (New-Item, Set-Item, ${alias:x} = ...), a rename, copy or move on alias: or function: -- chains
# followed to their end, and each rule runs on every name the command resolves to. An alias the scan cannot resolve makes every variable
# binding in the file unproven (Get-VariableRouteBinding), which is how the call, receiver and defaults rules fail closed.
$script:aliasDefiningCommands = @('set-alias', 'new-alias', 'sal', 'nal')
$script:aliasRoutePattern = '^\s*(?:[A-Za-z0-9_.]+\\)?alias::?'
$script:functionRoutePattern = '^\s*(?:[A-Za-z0-9_.]+\\)?function::?'
$script:aliasContexts = [System.Collections.Generic.Dictionary[object, object]]::new([System.Collections.Generic.ReferenceEqualityComparer]::Instance)
$script:importedAliases = @{ map = @{}; wildcard = [System.Collections.Generic.List[string]]::new(); problem = '' }
$script:sessionCommands = @{}

function Get-BareCommandName([string]$Name) {
    # 'Microsoft.PowerShell.Utility\Set-Variable' and 'global:Rebind' name the commands 'set-variable' and 'rebind'.
    return (($Name.Substring($Name.LastIndexOf('\') + 1)) -replace '^(?i:global|script|local|private):', '').Trim().ToLowerInvariant()
}

function Get-SessionCommand([string]$Name) {
    # The command this scan's own session answers to $Name (module auto-loading off), or $null.
    $key = $Name.ToLowerInvariant()
    if (-not $script:sessionCommands.ContainsKey($key)) {
        $PSModuleAutoLoadingPreference = 'None'
        $script:sessionCommands[$key] = Get-Command -Name ([WildcardPattern]::Escape($Name)) -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    return $script:sessionCommands[$key]
}

function Resolve-CommandNames([string]$Name, $Map, $Wildcard) {
    # Every name $Name may run as: itself, each target the source's aliases give it (chains followed), the session's own alias
    # resolution of each, and every target of an alias whose name the scan cannot read (any command may be that alias).
    $seen = [System.Collections.Generic.HashSet[string]]::new()
    $queue = [System.Collections.Generic.Queue[string]]::new()
    $queue.Enqueue((Get-BareCommandName $Name))
    foreach ($target in @($Wildcard)) { $queue.Enqueue((Get-BareCommandName $target)) }
    while ($queue.Count -gt 0) {
        $next = $queue.Dequeue()
        if ([string]::IsNullOrEmpty($next) -or -not $seen.Add($next)) { continue }
        if ($Map.ContainsKey($next)) { foreach ($target in @($Map[$next])) { $queue.Enqueue((Get-BareCommandName $target)) } }
        $session = Get-SessionCommand $next
        if ($null -ne $session -and [string]$session.CommandType -eq 'Alias' -and $session.ResolvedCommandName) { $queue.Enqueue((Get-BareCommandName $session.ResolvedCommandName)) }
    }
    return @($seen)
}

function Get-BoundOperands($Command, [string]$Cmdlet) {
    # How PowerShell binds $Command's elements to $Cmdlet's parameters: @{ named = @{ <parameter> = <ast> }; problem = '' }. A parameter is
    # read by its exact name, an alias or a unique prefix, a switch takes no value, and the positional values go to the positional
    # parameters (in Position order) of the parameter set that holds every named one. problem is set when the command splats, names no
    # unique parameter, repeats one, or passes more positional values than the set takes.
    $result = @{ named = @{}; problem = '' }
    $info = Get-SessionCommand $Cmdlet
    if ($null -eq $info -or $null -eq $info.Parameters) { $result.problem = "the scan cannot read the parameters of $Cmdlet"; return $result }
    $parameters = @($info.Parameters.Values)
    $positional = [System.Collections.Generic.List[object]]::new()
    $elements = $Command.CommandElements
    for ($i = 1; $i -lt $elements.Count; $i++) {
        $element = $elements[$i]
        if ($element -is [System.Management.Automation.Language.VariableExpressionAst] -and $element.Splatted) { $result.problem = 'it splats its arguments'; return $result }
        if ($element -isnot [System.Management.Automation.Language.CommandParameterAst]) { $positional.Add($element); continue }
        $spelling = $element.ParameterName
        $match = @($parameters | Where-Object { $_.Name -ieq $spelling -or @($_.Aliases) -icontains $spelling })
        if ($match.Count -eq 0) {
            $match = @($parameters | Where-Object { $_.Name.StartsWith($spelling, [StringComparison]::OrdinalIgnoreCase) -or
                                                    @($_.Aliases | Where-Object { $_.StartsWith($spelling, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0 })
        }
        if ($match.Count -ne 1) { $result.problem = "-$spelling does not name exactly one parameter of $Cmdlet"; return $result }
        $parameter = $match[0]
        $value = $element.Argument
        if ($parameter.SwitchParameter) { continue }
        if ($null -eq $value) {
            if ($i + 1 -ge $elements.Count -or $elements[$i + 1] -is [System.Management.Automation.Language.CommandParameterAst]) { $result.problem = "-$spelling has no value"; return $result }
            $i++
            $value = $elements[$i]
        }
        if ($result.named.ContainsKey($parameter.Name)) { $result.problem = "-$($parameter.Name) is repeated"; return $result }
        $result.named[$parameter.Name] = $value
    }
    $chosen = $null
    foreach ($set in @($info.ParameterSets)) {
        $setNames = @($set.Parameters | ForEach-Object { $_.Name })
        if (@($result.named.Keys | Where-Object { $setNames -notcontains $_ }).Count -gt 0) { continue }
        if ($null -eq $chosen -or $set.IsDefault) { $chosen = $set }
    }
    if ($null -eq $chosen) { $result.problem = "no parameter set of $Cmdlet holds every parameter it names"; return $result }
    $slots = @($chosen.Parameters | Where-Object { $_.Position -ge 0 -and -not $result.named.ContainsKey($_.Name) } | Sort-Object Position)
    if ($positional.Count -gt $slots.Count) { $result.problem = "it passes more positional values than $Cmdlet takes"; return $result }
    for ($k = 0; $k -lt $positional.Count; $k++) { $result.named[$slots[$k].Name] = $positional[$k] }
    return $result
}

function Get-SingleConstant($Ast) {
    # The one string an operand binds to a [string] parameter, or $null when it is not exactly one constant.
    if ($null -eq $Ast) { return $null }
    $operands = @(Expand-Operands $Ast)
    if ($operands.Count -ne 1) { return $null }
    return (Get-ConstantString $operands[0])
}

function Test-RouteOperand($Ast, [string]$Pattern, [string]$Fragment) {
    # True when an operand names, or could name, a path on the drive $Pattern matches: its constant does, a string inside it does
    # ("alias:$name"), or it is built only from literals the scan cannot reduce whose fragments, put together, spell $Fragment.
    if ($null -eq $Ast -or (Test-DataLiteralOperand $Ast)) { return $false }
    foreach ($operand in @(Expand-Operands $Ast)) {
        $constant = Get-ConstantString $operand
        if ($null -ne $constant) { if ($constant -match $Pattern) { return $true }; continue }
        foreach ($s in @($operand.FindAll({ param($x) $x -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $x -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true))) {
            if (([string]$s.Value) -match $Pattern) { return $true }
        }
        $fragments = -join @($operand.FindAll({ param($x) $x -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | ForEach-Object { [string]$_.Value })
        if ((Test-BuiltFromLiteralsOnly $operand) -and $fragments -match "(?i)$Fragment") { return $true }
    }
    return $false
}

function Get-RouteLeafName([string]$Value, [string]$Pattern) {
    # The item a constant drive path names ('alias:./Rebind' -> 'Rebind'), $null when it is not on that drive.
    if ($Value -notmatch $Pattern) { return $null }
    return (Get-ProviderLeaf ($Value -replace $Pattern, ''))
}

function Get-AliasDefinitions($Top, $InitialMap, $InitialWildcard, [switch]$TopLevelOnly) {
    # One pass over $Top: @{ map; wildcard; problem } with the definitions it finds added to $InitialMap / $InitialWildcard (copied).
    # -TopLevelOnly skips function bodies (a module's aliases that run when the module is imported).
    $map = @{}
    foreach ($key in @($InitialMap.Keys)) { $map[$key] = [System.Collections.Generic.List[string]]::new([string[]]@($InitialMap[$key])) }
    $wildcard = [System.Collections.Generic.List[string]]::new([string[]]@($InitialWildcard | Where-Object { $_ }))
    $problems = [System.Collections.Generic.List[string]]::new()
    $nodes = @($Top.FindAll({ param($x) $x -is [System.Management.Automation.Language.CommandAst] -or $x -is [System.Management.Automation.Language.AssignmentStatementAst] -or
                                        $x -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $x -is [System.Management.Automation.Language.ExpandableStringExpressionAst] }, $true))
    if ($TopLevelOnly) {
        $nodes = @($nodes | Where-Object {
            $inside = $false
            for ($p = $_.Parent; $null -ne $p; $p = $p.Parent) { if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst]) { $inside = $true; break } }
            -not $inside
        })
    }
    $addAlias = {
        param([string]$AliasName, [string]$Target)
        if ([string]::IsNullOrEmpty($AliasName) -or $AliasName -match '[*?\[\]]') { $wildcard.Add($Target); return }
        $key = Get-BareCommandName $AliasName
        if (-not $map.ContainsKey($key)) { $map[$key] = [System.Collections.Generic.List[string]]::new() }
        if (-not $map[$key].Contains($Target)) { $map[$key].Add($Target) }
    }
    $hasAliasRoute = $false
    $renamesWithoutSource = [System.Collections.Generic.List[string]]::new()
    foreach ($n in $nodes) {
        if ($n -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
            # An alias: path anywhere but an operand of a command (`$p = 'alias:Rebind'`) reaches a command the scan does not see.
            if (([string]$n.Value) -notmatch $script:aliasRoutePattern) { continue }
            $hasAliasRoute = $true
            # Its owner is the nearest command whose operand holds it; an assignment or a block reached first means it has none.
            $owner = $n.Parent
            while ($null -ne $owner -and $owner -isnot [System.Management.Automation.Language.CommandAst] -and $owner -isnot [System.Management.Automation.Language.AssignmentStatementAst] -and
                   $owner -isnot [System.Management.Automation.Language.StatementBlockAst] -and $owner -isnot [System.Management.Automation.Language.NamedBlockAst] -and
                   -not ($owner -is [System.Management.Automation.Language.PipelineAst] -and $owner.PipelineElements.Count -gt 1)) { $owner = $owner.Parent }
            if ($owner -isnot [System.Management.Automation.Language.CommandAst]) { $problems.Add("the alias: drive path '$($n.Value)' outside a command the scan models") }
            continue
        }
        if ($n -is [System.Management.Automation.Language.AssignmentStatementAst]) {
            $left = Get-AssignmentTarget $n.Left
            if ($left -isnot [System.Management.Automation.Language.VariableExpressionAst]) { continue }
            $user = $left.VariablePath.UserPath
            if ($user -match '^(?i:alias):(.*)$') {
                $hasAliasRoute = $true
                $leaf = Get-ProviderLeaf $Matches[1]
                $target = if ($n.Operator -eq [System.Management.Automation.Language.TokenKind]::Equals) { Get-SingleConstant $n.Right } else { $null }
                if ($null -eq $target) { $problems.Add("``$($left.Extent.Text) = ...`` with a target that is not a string constant") }
                else { & $addAlias $leaf $target }
            }
            elseif ($user -match '^(?i:function):(.*)$') {
                $leaf = Get-ProviderLeaf $Matches[1]
                $value = Get-UnwrappedOperand $n.Right
                if ($value -is [System.Management.Automation.Language.VariableExpressionAst] -and $value.VariablePath.UserPath -match '^(?i:function):(.*)$') { & $addAlias $leaf (Get-ProviderLeaf $Matches[1]) }
                elseif ($value -isnot [System.Management.Automation.Language.ScriptBlockExpressionAst]) { $problems.Add("``$($left.Extent.Text) = ...`` with a body that is not a script block in the source") }
            }
            continue
        }
        $commandName = Get-LiteralCommandName $n
        if ([string]::IsNullOrEmpty($commandName)) { continue }
        $names = Resolve-CommandNames $commandName $map $wildcard
        if (@($names | Where-Object { @('import-alias', 'ipal') -contains $_ }).Count -gt 0) { $problems.Add('Import-Alias defines aliases from a file the scan does not read'); continue }
        if (@($names | Where-Object { $script:aliasDefiningCommands -contains $_ }).Count -gt 0) {
            $bound = Get-BoundOperands $n 'Set-Alias'
            if ($bound.problem) { $problems.Add("an alias command the scan cannot read ($($bound.problem))"); continue }
            $target = Get-SingleConstant $bound.named['Value']
            if ($null -eq $target) { $problems.Add('an alias command whose target is not a string constant'); continue }
            & $addAlias (Get-SingleConstant $bound.named['Name']) $target
            continue
        }
        $operands = @($n.CommandElements | Select-Object -Skip 1 | ForEach-Object { if ($_ -is [System.Management.Automation.Language.CommandParameterAst]) { $_.Argument } else { $_ } } | Where-Object { $null -ne $_ })
        $aliasOperand = @($operands | Where-Object { Test-RouteOperand $_ $script:aliasRoutePattern 'alias' }).Count -gt 0
        $functionOperand = @($operands | Where-Object { Test-RouteOperand $_ $script:functionRoutePattern 'function' }).Count -gt 0
        if ($aliasOperand) { $hasAliasRoute = $true }
        $isItem = @($names | Where-Object { @('new-item', 'ni', 'set-item', 'si') -contains $_ }).Count -gt 0
        $renameKind = @($names | Where-Object { @('rename-item', 'rni', 'ren') -contains $_ }).Count -gt 0
        $copyKind = @($names | Where-Object { @('copy-item', 'cpi', 'copy', 'cp', 'move-item', 'mi', 'move', 'mv') -contains $_ }).Count -gt 0
        if ($renameKind -or $copyKind) {
            $cmdlet = if ($renameKind) { 'Rename-Item' } elseif (@($names | Where-Object { @('move-item', 'mi', 'move', 'mv') -contains $_ }).Count -gt 0) { 'Move-Item' } else { 'Copy-Item' }
            $bound = Get-BoundOperands $n $cmdlet
            $source = if ($bound.named.ContainsKey('Path')) { $bound.named['Path'] } else { $bound.named['LiteralPath'] }
            $destination = if ($renameKind) { $bound.named['NewName'] } else { $bound.named['Destination'] }
            $sourceText = Get-SingleConstant $source
            # A source the scan cannot read (pipeline input, a variable) may be an alias; it is judged once the whole file is read.
            if ($null -eq $sourceText) { $renamesWithoutSource.Add($n.Extent.Text); if (-not ($aliasOperand -or $functionOperand)) { continue } }
            if (-not ($aliasOperand -or $functionOperand)) { continue }
            if ($bound.problem) { $problems.Add("a rename or copy on the alias: / function: drive the scan cannot read ($($bound.problem))"); continue }
            $pattern = if ($null -ne $sourceText -and $sourceText -match $script:functionRoutePattern) { $script:functionRoutePattern } else { $script:aliasRoutePattern }
            $sourceLeaf = if ($null -ne $sourceText) { Get-RouteLeafName $sourceText $pattern } else { $null }
            if ($null -eq $sourceLeaf -or $sourceLeaf -eq '' -or $sourceLeaf -match '[*?\[\]$(\\/]') {
                if ($aliasOperand) { $problems.Add("a rename or copy on the alias: drive whose source the scan cannot read as one alias") }
                continue
            }
            $destinationText = Get-SingleConstant $destination
            $newName = if ($null -eq $destinationText) { $null } elseif ($destinationText -match $script:providerRoutePattern) { Get-ProviderLeaf ($destinationText -replace $script:providerRoutePattern, '') } else { Get-ProviderLeaf $destinationText }
            & $addAlias $newName $sourceLeaf
            continue
        }
        if ($isItem -and ($aliasOperand -or $functionOperand)) {
            $cmdlet = if (@($names | Where-Object { @('new-item', 'ni') -contains $_ }).Count -gt 0) { 'New-Item' } else { 'Set-Item' }
            $bound = Get-BoundOperands $n $cmdlet
            if ($bound.problem) { $problems.Add("$cmdlet on the alias: / function: drive the scan cannot read ($($bound.problem))"); continue }
            $path = if ($bound.named.ContainsKey('Path')) { $bound.named['Path'] } else { $bound.named['LiteralPath'] }
            $pathText = Get-SingleConstant $path
            $leaf = $null
            if ($null -ne $pathText) {
                $onAlias = $pathText -match $script:aliasRoutePattern
                $leaf = Get-RouteLeafName $pathText $(if ($onAlias) { $script:aliasRoutePattern } else { $script:functionRoutePattern })
                if ($bound.named.ContainsKey('Name')) { $leaf = if ($leaf -eq '') { Get-SingleConstant $bound.named['Name'] } else { $null } }
            }
            else { $onAlias = $aliasOperand }
            if ($onAlias) {
                $target = Get-SingleConstant $bound.named['Value']
                if ($null -eq $target) { $problems.Add("$cmdlet on the alias: drive with a target that is not a string constant"); continue }
                & $addAlias $leaf $target
            }
            else {
                $value = Get-UnwrappedOperand $bound.named['Value']
                if ($null -eq $value -or $value -is [System.Management.Automation.Language.ScriptBlockExpressionAst]) { continue }
                if ($value -is [System.Management.Automation.Language.VariableExpressionAst] -and $value.VariablePath.UserPath -match '^(?i:function):(.*)$' -and $null -ne $leaf) { & $addAlias $leaf (Get-ProviderLeaf $Matches[1]) }
                else { $problems.Add("$cmdlet on the function: drive with a body that is not a script block in the source") }
            }
            continue
        }
        # Reading or removing an alias binds nothing; any other command handed an alias: path is a use the scan does not model.
        if ($aliasOperand -and @($names | Where-Object { @('get-item', 'gi', 'get-childitem', 'gci', 'ls', 'dir', 'test-path', 'get-content', 'gc', 'remove-item', 'ri', 'rm', 'del', 'erase') -contains $_ }).Count -eq 0) {
            $problems.Add("$commandName on the alias: drive, a use the scan does not model")
        }
    }
    if ($hasAliasRoute -and $renamesWithoutSource.Count -gt 0) { $problems.Add("a rename, copy or move whose source the scan cannot read, in a file that uses the alias: drive ($($renamesWithoutSource[0]))") }
    return @{ map = $map; wildcard = $wildcard; problem = $(if ($problems.Count -gt 0) { $problems[0] } else { '' }) }
}

function Get-AliasContext($Root, [switch]$Imported) {
    # The aliases the tree that holds $Root defines (Get-AliasDefinitions, repeated until an alias of an alias command adds nothing),
    # cached per tree. -Imported (a splice source) starts from the aliases the -ModulePath modules' top-level code defines. A target
    # no command answers to -- not a session command, a function the tree or a -ModulePath module defines, or another alias -- is a
    # problem: the scan cannot say what the alias runs.
    $top = $Root
    while ($null -ne $top.Parent) { $top = $top.Parent }
    if ($script:aliasContexts.ContainsKey($top)) { return $script:aliasContexts[$top] }
    $map = if ($Imported) { $script:importedAliases.map } else { @{} }
    $wildcard = @(if ($Imported) { $script:importedAliases.wildcard })
    $context = $null
    for ($pass = 0; $pass -lt 8; $pass++) {
        $context = Get-AliasDefinitions $top $map $wildcard
        $size = @($context.map.Values | ForEach-Object { $_.Count } | Measure-Object -Sum).Sum + $context.wildcard.Count
        $before = @($map.Values | ForEach-Object { @($_).Count } | Measure-Object -Sum).Sum + @($wildcard).Count
        if ($size -eq $before) { break }
        $map = $context.map; $wildcard = $context.wildcard
        if ($pass -eq 7) { $context.problem = 'aliases of alias commands the scan stopped following after eight passes' }
    }
    if (-not $context.problem -and $Imported -and $script:importedAliases.problem) { $context.problem = "a -ModulePath module's $($script:importedAliases.problem)" }
    if (-not $context.problem) {
        $defined = @($top.FindAll({ param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object { Get-BareCommandName $_.Name }) +
                   @($moduleFunctions | ForEach-Object { Get-BareCommandName $_ })
        foreach ($target in @(@($context.map.Values | ForEach-Object { $_ }) + @($context.wildcard))) {
            $answered = @(Resolve-CommandNames $target $context.map @() | Where-Object { $defined -contains $_ -or ($null -ne (Get-SessionCommand $_) -and [string](Get-SessionCommand $_).CommandType -ne 'Alias') })
            if ($answered.Count -eq 0) { $context.problem = "an alias whose target '$target' is not a command the scan can resolve"; break }
        }
    }
    $script:aliasContexts[$top] = $context
    return $context
}

function Get-LiteralCommandName($Command) {
    # The name a CommandAst invokes when the source text fixes it: GetCommandName(), or (r10 adversary A2) the constant after `&` or
    # `.` (`& ('Rebind')`, `& 'Set-Variable'`), which GetCommandName() does not read. $null when the name is computed at run time.
    $name = $Command.GetCommandName()
    if (-not [string]::IsNullOrEmpty($name)) { return $name }
    if ($Command.InvocationOperator -eq 'Unknown' -or $Command.CommandElements.Count -eq 0) { return $null }
    return (Get-ConstantString $Command.CommandElements[0])
}

function Get-ResolvedCommandNames($Command) {
    # Every name a CommandAst may run as (Resolve-CommandNames through its tree's aliases); empty when it has no literal name.
    $name = Get-LiteralCommandName $Command
    if ([string]::IsNullOrEmpty($name)) { return @() }
    $context = Get-AliasContext $Command
    return @(Resolve-CommandNames $name $context.map $context.wildcard)
}

# ---- r9: the unproven-binding class rule (opus r8 B1/B2, sol r8 L2) ----
# Eight rounds each closed one construct that changes WHICH module or object a name binds to at run time, and the next key found
# another of the same class. The rule is the class, not the instance: a route this scan cannot evaluate statically makes the
# binding UNPROVEN, and the scan reports that (fails closed) instead of reading the module or object it would merely assume.
$script:variableCommands = @('set-variable', 'sv', 'set', 'new-variable', 'nv', 'remove-variable', 'rv', 'clear-variable', 'clv', 'get-variable', 'gv')
$script:variableRoute = '^\s*(?:[A-Za-z0-9_.]+\\)?variable::?\s*[\\/]?(.*)$'
$script:variableParameterSpellings = @{}

function Test-NameMatchesPattern([string]$Name, [string]$Pattern) {
    # $Name -like $Pattern; a pattern PowerShell cannot parse as a wildcard counts as a match (fails closed).
    try { return ($Name -like $Pattern) } catch { return $true }
}

function Test-VariableTailReaches([string]$Leaf, [string]$Name) {
    # A variable: tail that names $Name or could: empty, computed, a separator still inside it, or a wildcard that matches it.
    return ($Leaf -eq '' -or $Leaf -match '[$(\\/]' -or (Test-NameMatchesPattern $Name $Leaf))
}

function Get-MemberNameValue($Member) {
    # The name a member access reads, or $null when the source text does not fix it. A quoted name is its parsed value; a name
    # built only from literals (.('Get' + 'VariableValue'), .$('Session' + 'State')) is folded as an operand is (Get-ConstantString).
    if ($Member -is [System.Management.Automation.Language.StringConstantExpressionAst]) { return [string]$Member.Value }
    return (Get-ConstantString $Member)
}

# The engine-bound variables that are, or can hand back, the session itself ($PSCmdlet.SessionState, $ExecutionContext.SessionState,
# $MyInvocation.MyCommand.Module.SessionState, $Host.Runspace.SessionStateProxy). The engine-bound inputs ($_, $PSItem, $input, $args)
# carry whatever was handed in, so they count only in a tree that names a session root somewhere (`$PSCmdlet | % { $_.$m }`).
$script:sessionRootVariables = @('PSCmdlet', 'ExecutionContext', 'Host', 'MyInvocation', 'this')
$script:engineInputVariables = @('_', 'PSItem', 'input', 'args')
$script:sessionMemberNames = @('SessionState', 'SessionStateProxy', 'PSVariable', 'GetVariableValue', 'InvokeCommand')
# r12 adversary A2: the engine commands whose result is, or hands back, a variable, a module (.SessionState), a command (.Module) or a
# host or runspace (.Runspace.SessionStateProxy), and the engine types such a value has.
$script:sessionCarrierCommands = @('get-variable', 'gv', 'get-module', 'gmo', 'import-module', 'ipmo', 'new-module', 'nmo', 'get-command', 'gcm',
                                   'get-host', 'get-runspace', 'get-pscallstack')
$script:sessionCarrierTypePattern = '(?i)runspace|powershell|SessionState|psvariable|Management\.Automation|psmoduleinfo|commandinfo|functioninfo|scriptblock|^host$|pshost'

function Test-SessionCarrierCommand($Command) {
    # r12 adversary A2: a command whose result may carry the session. Import-Module hands back the module only with -PassThru (any
    # prefix of it, or a splat that may hold it).
    $names = @(Get-ResolvedCommandNames $Command | Where-Object { $script:sessionCarrierCommands -contains $_ })
    if ($names.Count -eq 0) { return $false }
    # Get-Command on one constant executable name selected as an application only (`Get-Command python.exe -CommandType Application`)
    # hands back an ApplicationInfo, which carries no module. r12b (Dot B13): the name alone proves nothing, since a supplied module
    # can export `function python.exe`, whose FunctionInfo .Module reaches its session state; so the type set must be exactly one
    # constant Application (-CommandType or its alias -Type, any prefix, once), else the selection is unproven.
    # r12c: and the arguments prove only what the builtin selects, so the command must be the builtin (Test-BuiltinGetCommand).
    if (@($names | Where-Object { @('get-command', 'gcm') -notcontains $_ }).Count -eq 0) {
        if (@($Command.CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.VariableExpressionAst] -and $_.Splatted }).Count -gt 0) { return $true }
        $types = Get-NamedArguments $Command @('CommandType') 'Get-Command'
        if (@($types).Count -ne 1 -or $null -eq $types[0] -or (Get-SingleConstant $types[0]) -ine 'Application') { return $true }
        $named = Get-NamedArguments $Command @('Name')
        $operand = if (@($named).Count -eq 1) { $named[0] } elseif (@($named).Count -eq 0 -and $Command.CommandElements.Count -gt 1 -and $Command.CommandElements[1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $Command.CommandElements[1] } else { $null }
        $commandName = if ($null -eq $operand) { $null } elseif ($operand -is [System.Management.Automation.Language.StringConstantExpressionAst]) { [string]$operand.Value } else { Get-SingleConstant $operand }
        if ($null -ne $commandName -and $commandName -match '(?i)^[^*?\[\]]+\.(exe|com|cmd|bat)$' -and (Test-BuiltinGetCommand $Command)) { return $false }
        return $true
    }
    if (@($names | Where-Object { @('import-module', 'ipmo') -notcontains $_ }).Count -gt 0) { return $true }
    return (@($Command.CommandElements | Where-Object {
        ($_ -is [System.Management.Automation.Language.CommandParameterAst] -and $_.ParameterName.Length -ge 2 -and 'passthru'.StartsWith($_.ParameterName.ToLowerInvariant())) -or
        ($_ -is [System.Management.Automation.Language.VariableExpressionAst] -and $_.Splatted) }).Count -gt 0)
}

# r12c (Dot on 8bb454b1, sol r12b S-07): a -ModulePath module can define, alias, or export under an Import-Module -Prefix, a
# replacement Get-Command that hands back a FunctionInfo whatever -CommandType says. Its identity is proven only for
# Microsoft.PowerShell.Core\Get-Command, or for a plain Get-Command / gcm that runs as nothing else and that no shadow
# (Get-ShadowingDefinitions, the model the extractor and Join-Path are judged by) in the calling tree or a -ModulePath module
# defines, under every name a constant -Prefix there turns into Get-Command or gcm. Anything else is unproven (fails closed).
$script:getCommandTrees = [System.Collections.Generic.Dictionary[object, object]]::new([System.Collections.Generic.ReferenceEqualityComparer]::Instance)
$script:suppliedModuleAsts = $null

function Get-SuppliedModuleAsts {
    # The -ModulePath modules' trees: the splice pass's own ($moduleAsts) once it has parsed them, else parsed here once.
    $built = Get-Variable -Name moduleAsts -Scope Script -ValueOnly -ErrorAction SilentlyContinue
    if ($null -ne $built) { return @($built.Values) }
    if ($null -eq $script:suppliedModuleAsts) {
        $script:suppliedModuleAsts = @($ModulePath | ForEach-Object {
            $tokens = $null; $errors = $null
            [System.Management.Automation.Language.Parser]::ParseFile((Resolve-Path -LiteralPath $_).Path, [ref]$tokens, [ref]$errors) })
    }
    return $script:suppliedModuleAsts
}

function Get-PrefixRenamedNames($Trees, [string[]]$Targets) {
    # $Targets and every name a chain of Import-Module -Prefix renames in $Trees turns into one of them, or $null when an
    # Import-Module there splats or names a prefix that is not a string constant. -Prefix P renames Verb-Noun to Verb-PNoun (a name
    # with no noun to PName), and (r12c adversary A2) a module that imports another with a prefix and exports the result composes
    # the two, so the names are unprefixed until nothing is added (each step is shorter, so it ends).
    $prefixes = [System.Collections.Generic.List[string]]::new()
    foreach ($tree in $Trees) {
        $imports = @($tree.FindAll({ param($x) $x -is [System.Management.Automation.Language.CommandAst] -and
                                     @(Get-ResolvedCommandNames $x | Where-Object { @('import-module', 'ipmo') -contains $_ }).Count -gt 0 }, $true))
        foreach ($import in $imports) {
            if (@($import.CommandElements | Where-Object { $_ -is [System.Management.Automation.Language.VariableExpressionAst] -and $_.Splatted }).Count -gt 0) { return $null }
            $operands = Get-NamedArguments $import @('Prefix') 'Import-Module'
            foreach ($operand in $operands) {
                $prefix = Get-SingleConstant $operand
                if ($null -eq $prefix) { return $null }
                if ($prefix.Length -gt 0 -and -not $prefixes.Contains($prefix)) { $prefixes.Add($prefix) }
            }
        }
    }
    $names = [System.Collections.Generic.List[string]]::new()
    $queue = [System.Collections.Generic.Queue[string]]::new()
    foreach ($target in $Targets) { $queue.Enqueue($target) }
    while ($queue.Count -gt 0) {
        $name = $queue.Dequeue()
        if (@($names | Where-Object { $_ -ieq $name }).Count -gt 0) { continue }
        $names.Add($name)
        $dash = $name.IndexOf('-')
        foreach ($prefix in $prefixes) {
            if ($dash -ge 0 -and $name.Substring($dash + 1).StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { $queue.Enqueue($name.Substring(0, $dash + 1) + $name.Substring($dash + 1 + $prefix.Length)) }
            if ($name.Length -gt $prefix.Length -and $name.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { $queue.Enqueue($name.Substring($prefix.Length)) }
        }
    }
    return , $names.ToArray()
}

function Test-BuiltinGetCommand($Command) {
    $name = Get-LiteralCommandName $Command
    if ([string]::IsNullOrEmpty($name)) { return $false }
    $base = @('Get-Command', 'gcm')
    if ($name.Contains('\')) {
        $qualifier = $name.Substring(0, $name.LastIndexOf('\'))
        if ($qualifier -ine 'Microsoft.PowerShell.Core' -or (Get-BareCommandName $name) -ne 'get-command' -or
            @($ModulePath | Where-Object { [IO.Path]::GetFileNameWithoutExtension($_) -ieq $qualifier }).Count -gt 0) { return $false }
        # r12c adversary A3: an alias or function named by the qualified spelling itself runs in the builtin's place.
        $base = @($name)
    }
    if (@(Get-ResolvedCommandNames $Command | Where-Object { @('get-command', 'gcm') -notcontains $_ }).Count -gt 0) { return $false }
    $top = $Command
    while ($null -ne $top.Parent) { $top = $top.Parent }
    if (-not $script:getCommandTrees.ContainsKey($top)) { $script:getCommandTrees[$top] = @{} }
    $key = $base -join '|'
    if (-not $script:getCommandTrees[$top].ContainsKey($key)) { $script:getCommandTrees[$top][$key] = Test-GetCommandUnshadowed $top $base }
    return $script:getCommandTrees[$top][$key]
}

function Test-GetCommandUnshadowed($Top, [string[]]$Base) {
    $trees = @($Top) + @(Get-SuppliedModuleAsts)
    $names = Get-PrefixRenamedNames $trees $Base
    if ($null -eq $names) { return $false }
    foreach ($tree in $trees) { if (@(Get-ShadowingDefinitions $tree $names).Count -gt 0) { return $false } }
    return $true
}

function Test-TreeNamesSessionCarrier($Root) {
    # r11 adversary pass 2 (A1), widened by r12 adversary pass 2 (A1): a tree that names a session root variable, calls a command whose
    # result may carry the session (Get-Module, Get-Command, ...) or names an engine type anywhere can hand that value to any receiver
    # through a foreach variable, the pipeline's $_, a multi-target assignment or a function result
    # (`foreach ($module in @(Get-Module Extractor)) { $module.$sn... }`), so every receiver of an unfoldable member counts there.
    # A carrier command counts here only where its value flows on (a foreach source, a pipeline stage that is not the last, a block's
    # output); one assigned to a variable is that variable's (Test-VariableMayHoldSession), one tested in place (`if (Get-Command X)`)
    # hands nothing on. Only the engine types that are or reach the session count, not every System.Management.Automation type.
    if (-not $script:sessionCarrierTrees.ContainsKey($Root)) {
        $script:sessionCarrierTrees[$Root] = @($Root.FindAll({ param($x)
            ($x -is [System.Management.Automation.Language.VariableExpressionAst] -and $script:sessionRootVariables -icontains (Get-VariableName $x).name) -or
            ($x -is [System.Management.Automation.Language.CommandAst] -and (Test-SessionCarrierCommand $x) -and (Test-CarrierValueFlowsOn $x)) -or
            (($x -is [System.Management.Automation.Language.TypeExpressionAst] -or $x -is [System.Management.Automation.Language.TypeConstraintAst]) -and $x.TypeName.FullName -match $script:sessionTreeTypePattern) }, $true)).Count -gt 0
    }
    return $script:sessionCarrierTrees[$Root]
}
$script:sessionCarrierTrees = @{}
$script:sessionTreeTypePattern = '(?i)(^|\.)(runspace|runspacefactory|powershell|SessionState|SessionStateProxy|psvariable|PSVariableIntrinsics|EngineIntrinsics|psmoduleinfo|commandinfo|functioninfo|scriptblock|pshost|PSCmdlet)$'

function Test-CarrierValueFlowsOn($Command) {
    $pipe = $Command.Parent
    if ($pipe -isnot [System.Management.Automation.Language.PipelineAst]) { return $true }
    if ($pipe.PipelineElements.IndexOf($Command) -lt $pipe.PipelineElements.Count - 1) { return $true }
    if ($pipe.Parent -is [System.Management.Automation.Language.StatementBlockAst] -or $pipe.Parent -is [System.Management.Automation.Language.NamedBlockAst]) { return $true }
    for ($p = $pipe.Parent; $null -ne $p; $p = $p.Parent) {
        if ($p -is [System.Management.Automation.Language.ForEachStatementAst] -and $Command.Extent.StartOffset -ge $p.Condition.Extent.StartOffset -and $Command.Extent.EndOffset -le $p.Condition.Extent.EndOffset) { return $true }
        if ($p -is [System.Management.Automation.Language.StatementAst] -and $p -isnot [System.Management.Automation.Language.PipelineBaseAst] -and $p -isnot [System.Management.Automation.Language.CommandBaseAst]) { break }
    }
    return $false
}

function Test-MemberReceiverMayBeSession($Member, $Root) {
    # r11: true when the receiver of a member access whose name the scan cannot fold may be (or reach) the session: it starts at a
    # session root variable, a variable: path, a type, a command result or any other value that is not a plain variable, or a
    # variable an assignment binds from a session root, a variable command (Get-Variable) or an engine type ([runspace],
    # [powershell], System.Management.Automation.*). Only a plain variable none of whose assignments names one is not one
    # (`$table = ... | ConvertFrom-Json; $table.venues.$name`).
    # r11 adversary pass 2 (A1): in a tree that names a session root anywhere, a foreach variable, a multi-target assignment or a
    # function's result can carry it to any receiver (`foreach ($s in @($PSCmdlet)) { $s.$m... }`), so every receiver counts there.
    if (Test-TreeNamesSessionCarrier $Root) { return $true }
    $node = $Member.Expression
    while ($true) {
        if ($node -is [System.Management.Automation.Language.MemberExpressionAst]) { $node = $node.Expression; continue }
        if ($node -is [System.Management.Automation.Language.IndexExpressionAst]) { $node = $node.Target; continue }
        break
    }
    if ($node -isnot [System.Management.Automation.Language.VariableExpressionAst] -or $node.Splatted) { return $true }
    $user = $node.VariablePath.UserPath
    if ($user -match '^(?i:variable):') { return $true }
    $name = (Get-VariableName $node).name
    if ($script:sessionRootVariables -icontains $name) { return $true }
    if ($script:engineInputVariables -icontains $name) {
        return (Test-TreeNamesSessionCarrier $Root)
    }
    return (Test-VariableMayHoldSession $Root $name ([System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)))
}

function Test-VariableMayHoldSession($Root, [string]$Name, $Visiting) {
    # r11 adversary A1: an assignment binds $Name from a session root, a variable command, an engine type, or (followed to the end of
    # the chain: `$a = $PSCmdlet; $b = $a`) another variable that may hold one.
    # r12 adversary A2: or from a module, command, host or runspace command (`$module = Get-Module Extractor; $module.$sn.$vn.Set(...)`),
    # or a cast to an engine type (`[psmoduleinfo]$x`).
    if (-not $Visiting.Add($Name)) { return $false }
    foreach ($assignment in @(Get-AssignmentsTo $Root $Name)) {
        foreach ($x in @($assignment.Right.FindAll({ param($n)
                $n -is [System.Management.Automation.Language.VariableExpressionAst] -or $n -is [System.Management.Automation.Language.TypeExpressionAst] -or
                $n -is [System.Management.Automation.Language.TypeConstraintAst] -or $n -is [System.Management.Automation.Language.CommandAst] }, $true))) {
            if ($x -is [System.Management.Automation.Language.TypeExpressionAst] -or $x -is [System.Management.Automation.Language.TypeConstraintAst]) {
                if ($x.TypeName.FullName -match $script:sessionCarrierTypePattern) { return $true }
                continue
            }
            if ($x -is [System.Management.Automation.Language.CommandAst]) {
                if (@(Get-ResolvedCommandNames $x | Where-Object { $script:sessionCarrierCommands -contains $_ }).Count -gt 0) { return $true }
                continue
            }
            if ($x.VariablePath.UserPath -match '^(?i:variable):') { return $true }
            $source = (Get-VariableName $x).name
            if ($script:sessionRootVariables -icontains $source) { return $true }
            if ($script:engineInputVariables -icontains $source) {
                if (Test-TreeNamesSessionCarrier $Root) { return $true }
                continue
            }
            if (Test-VariableMayHoldSession $Root $source $Visiting) { return $true }
        }
    }
    return $false
}

function Get-VariableParameterSpellings([string]$CommandName) {
    # The names and aliases of every parameter ending in 'Variable' on the command as this session resolves it (Tee-Object
    # -Variable, Invoke-WebRequest -SessionVariable, the common -OutVariable ...), so a prefix is read against the command's OWN
    # parameter set: PowerShell binds any prefix unique for that command (`Tee-Object -Va`, `Invoke-WebRequest -Ses`, opus r8 B2).
    # Empty when the name does not resolve to a cmdlet or function here. Module auto-loading is off, so no module is imported.
    $key = $CommandName.ToLowerInvariant()
    if (-not $script:variableParameterSpellings.ContainsKey($key)) {
        $spellings = [System.Collections.Generic.List[string]]::new()
        $PSModuleAutoLoadingPreference = 'None'
        $info = Get-Command -Name $CommandName -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $info -and [string]$info.CommandType -eq 'Alias') { $info = $info.ResolvedCommand }
        if ($null -ne $info -and @('Cmdlet', 'Function', 'Filter') -contains [string]$info.CommandType -and $null -ne $info.Parameters) {
            foreach ($parameter in @($info.Parameters.Values)) {
                if ($parameter.Name -notmatch 'Variable$') { continue }
                $spellings.Add($parameter.Name)
                foreach ($alias in @($parameter.Aliases)) { $spellings.Add($alias) }
            }
        }
        $script:variableParameterSpellings[$key] = $spellings.ToArray()
    }
    return $script:variableParameterSpellings[$key]
}

function Get-VariableRouteBinding($Root, [string]$Name) {
    # '' when nothing in $Root can bind the variable $Name by a route other than the assignments, parameters, loop variables,
    # multi-target assignments, [ref] and data sections the callers model themselves; otherwise a short description of the first
    # route that could. Routes: a variable parameter (-OutVariable and its relatives, Tee-Object -Variable, -SessionVariable, by name,
    # alias or any prefix the command's own parameter set accepts); Set- / New- / Remove- / Clear- / Get-Variable (a name the scan
    # cannot read, a wildcard that matches, or no name at all counts); the variable: drive; and the session-state API
    # ($ExecutionContext, .SessionState, .SessionStateProxy, .PSVariable, [psvariable]), which reaches a variable by a name the scan
    # never sees. Shared by $PSScriptRoot (an extractor input), $PSDefaultParameterValues and an R5 receiver.
    # -OutVariable / -ErrorVariable / -WarningVariable / -InformationVariable / -PipelineVariable by name, alias or unique prefix
    # (-OutV, -ErrorV, -ev). A one-letter prefix of a native tool's switch (`git cat-file -e`) is not one of them, so the
    # shortest prefix read is the shortest unique among the common parameters; -p is read only when it names the variable.
    $variableParameters = @(@{ Full = 'ErrorVariable'; Alias = 'ev'; Min = 6 }, @{ Full = 'WarningVariable'; Alias = 'wv'; Min = 8 }, @{ Full = 'InformationVariable'; Alias = 'iv'; Min = 12 },
                            @{ Full = 'OutVariable'; Alias = 'ov'; Min = 4 }, @{ Full = 'PipelineVariable'; Alias = 'pv'; Min = 1 },
                            # r7 (adversary A2): the cmdlets with a variable parameter of their own. -Var is the shortest -Variable (Tee-Object); -Va is
                            # also -Value, so it is not read as one -- unless the command's own parameter set says it is (r9, below).
                            @{ Full = 'Variable'; Alias = ''; Min = 3 }, @{ Full = 'SessionVariable'; Alias = ''; Min = 8 },
                            @{ Full = 'ResponseHeadersVariable'; Alias = ''; Min = 16 }, @{ Full = 'StatusCodeVariable'; Alias = ''; Min = 11 })
    # r10 (sol r9, catalogue S-06): an alias the source defines and the scan cannot resolve may run any command, so it may bind $Name.
    $aliasContext = Get-AliasContext $Root
    if ($aliasContext.problem) { return "an alias the scan cannot resolve ($($aliasContext.problem)), which may run a command that binds $Name" }
    foreach ($command in @($Root.FindAll({ param($x) $x -is [System.Management.Automation.Language.CommandAst] }, $true))) {
        $elements = $command.CommandElements
        # r10: a command is read as every name it resolves to through the source's own aliases (`Set-Alias T Tee-Object; T -Va x`).
        $ownSpellings = @(foreach ($resolvedName in @(Get-ResolvedCommandNames $command)) { Get-VariableParameterSpellings $resolvedName })
        for ($i = 1; $i -lt $elements.Count; $i++) {
            $element = $elements[$i]
            if ($element -isnot [System.Management.Automation.Language.CommandParameterAst]) { continue }
            $spelling = $element.ParameterName
            # r9 (opus r8 B2): a prefix the command's own parameter set binds to one of its variable parameters counts at any length.
            $ownBinds = @($ownSpellings | Where-Object { $_.StartsWith($spelling, [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
            $binds = $ownBinds -or @($variableParameters | Where-Object {
                $spelling -ieq $_.Alias -or ($spelling.Length -ge $_.Min -and $_.Full.StartsWith($spelling, [StringComparison]::OrdinalIgnoreCase))
            }).Count -gt 0
            if (-not $binds) { continue }
            $value = $element.Argument
            if ($null -eq $value -and $i + 1 -lt $elements.Count -and $elements[$i + 1] -isnot [System.Management.Automation.Language.CommandParameterAst]) { $value = $elements[$i + 1] }
            $constant = if ($null -ne $value) { Get-ConstantString $value } else { $null }
            if ($null -ne $constant) {
                if (($constant.TrimStart('+') -replace '^[A-Za-z]+:', '') -ieq $Name) { return "-$spelling $Name" }
            } elseif ($ownBinds -or $spelling.Length -gt 1) {
                return "-$spelling with a variable name the scan cannot read as a string constant"
            }
        }
    }
    foreach ($n in @($Root.FindAll({ param($x) $true }, $true))) {
        if ($n -is [System.Management.Automation.Language.CommandAst]) {
            # A route built from constants ('vari' + 'able:PSScriptRoot') has no string node of its own; a plain string is read below.
            $resolvedNames = @(Get-ResolvedCommandNames $n)
            $isItemCommand = @($resolvedNames | Where-Object { $script:itemCommands -contains $_ }).Count -gt 0
            $renameName = @($resolvedNames | Where-Object { @('rename-item', 'rni', 'ren', 'move-item', 'mi', 'move', 'mv', 'copy-item', 'cpi', 'copy', 'cp') -contains $_ }) | Select-Object -First 1
            # r9 adversary A1: a rename, move or copy on the variable: drive makes its NEW name a variable (`Rename-Item variable:x
            # PSScriptRoot`), so the tail of the route is not the only name it binds. An operand naming the variable next to a route or
            # a source the scan cannot read (an operand, or pipeline input), a route next to an operand the scan cannot read, or an
            # operand built only from literals the scan cannot reduce, binds it (the same reading Get-ShadowKind gives function:).
            if ($null -ne $renameName) {
                $routed = $false; $named = $false; $unreadable = $false; $fixedButUnread = $false
                if ($n.Parent -is [System.Management.Automation.Language.PipelineAst] -and $n.Parent.PipelineElements.IndexOf($n) -gt 0) { $unreadable = $true }
                foreach ($element in @($n.CommandElements | Select-Object -Skip 1)) {
                    $operandRoot = if ($element -is [System.Management.Automation.Language.CommandParameterAst]) { $element.Argument } else { $element }
                    if ($null -eq $operandRoot) { continue }
                    foreach ($operand in @(Expand-Operands $operandRoot)) {
                        $constant = Get-ConstantString $operand
                        if ($null -eq $constant) {
                            if (Test-DataLiteralOperand $operand) { continue }
                            $unreadable = $true
                            # Fixed by the source text but unreadable here: it fails closed when its literal fragments, put together, can
                            # spell the drive or the variable's name ('vari' + 'able:x', ('PSScript', 'Root') -join '').
                            $fragments = -join @($operand.FindAll({ param($x) $x -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | ForEach-Object { [string]$_.Value })
                            if ((Test-BuiltFromLiteralsOnly $operand) -and ($fragments -match '(?i)variable' -or $fragments.IndexOf($Name, [StringComparison]::OrdinalIgnoreCase) -ge 0)) { $fixedButUnread = $true }
                            continue
                        }
                        if ($constant -match $script:variableRoute) { $routed = $true }
                        # A function: / alias: path names a command, never a variable.
                        elseif ($constant -notmatch $script:providerRoutePattern -and (Test-NameMatchesPattern $Name ($constant -replace '^[A-Za-z]+:', ''))) { $named = $true }
                    }
                }
                if ($fixedButUnread -or ($routed -and ($named -or $unreadable)) -or ($named -and $unreadable)) {
                    return "$renameName on the variable: drive (a new name that is, or could be, $Name)"
                }
            }
            foreach ($element in @($n.CommandElements | Select-Object -Skip 1)) {
                $operandRoot = if ($element -is [System.Management.Automation.Language.CommandParameterAst]) { $element.Argument } else { $element }
                if ($null -eq $operandRoot) { continue }
                foreach ($operand in @(Expand-Operands $operandRoot)) {
                    $plain = Get-UnwrappedOperand $operand
                    if ($plain -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $plain -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) { continue }
                    $constant = Get-ConstantString $operand
                    if ($null -ne $constant -and $constant -match $script:variableRoute) {
                        $reducedRest = Get-ProviderLeaf $Matches[1]
                        if (Test-VariableTailReaches $reducedRest $Name) { return 'the variable: drive' }
                    }
                    # An item command's path built only from literals the scan cannot reduce (a cast over a comma list, a -join) is
                    # decided by the source text but unreadable here: it could name variable:<the variable>, so it fails closed.
                    # Only when its literal fragments, put together, can spell the drive name ('vari', 'able:' in any order of joining).
                    if ($isItemCommand -and $null -eq $constant -and (Test-BuiltFromLiteralsOnly $operand) -and -not (Test-DataLiteralOperand $operand) -and
                        (-join @($operand.FindAll({ param($x) $x -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | ForEach-Object { [string]$_.Value })) -match '(?i)variable') {
                        return 'the variable: drive (an item command path the scan cannot read as a string constant)'
                    }
                }
            }
            $commandName = @($resolvedNames | Where-Object { $script:variableCommands -contains $_ }) | Select-Object -First 1
            if ($null -eq $commandName) { continue }
            # r9: Get-Variable hands back the variable object itself (`(Get-Variable r).Value = ...`), and -Include selects by wildcard;
            # a variable command with no name argument at all reaches every variable.
            $nameArguments = [System.Collections.Generic.List[object]]::new()
            foreach ($argument in (Get-AliasNameArguments $n 'Set-Variable')) { $nameArguments.Add($argument) }
            foreach ($argument in (Get-NamedArguments $n @('Include') 'Get-Variable')) { if ($null -ne $argument) { $nameArguments.Add($argument) } }
            if ($nameArguments.Count -eq 0) { return "$commandName with no variable name (it reaches every variable)" }
            foreach ($argument in $nameArguments) {
                foreach ($operand in @(Expand-Operands $argument)) {
                    $constant = Get-ConstantString $operand
                    if ($null -eq $constant) { return "$commandName with a variable name the scan cannot read as a string constant" }
                    $bare = $constant -replace '^[A-Za-z]+:', ''
                    if (Test-NameMatchesPattern $Name $bare) { return "$commandName naming $Name" }
                }
            }
        }
        elseif ($n -is [System.Management.Automation.Language.StringConstantExpressionAst] -or $n -is [System.Management.Automation.Language.ExpandableStringExpressionAst]) {
            if (([string]$n.Value) -match $script:variableRoute) {
                # variable:local:PSScriptRoot / variable:\script:PSScriptRoot: the drive takes its own scope qualifier.
                # './PSScriptRoot', '.\PSScriptRoot' and '\\PSScriptRoot' name it too (Get-ProviderLeaf).
                $rest = Get-ProviderLeaf $Matches[1]
                if (Test-VariableTailReaches $rest $Name) { return 'the variable: drive' }
            }
            # r11 adversary pass 2 (A2): reflection reaches a member by a string (.PSObject.Properties['SessionState'], .Methods['GetVariableValue']).
            if ($script:sessionMemberNames -icontains ([string]$n.Value).Trim()) { return "a string naming the session-state API ('$($n.Value)'), which reflection reaches by name" }
        }
        elseif ($n -is [System.Management.Automation.Language.BinaryExpressionAst]) {
            $folded = Get-ConstantString $n
            if ($null -ne $folded -and $script:sessionMemberNames -icontains $folded.Trim()) { return "a string built from literals naming the session-state API ('$folded'), which reflection reaches by name" }
        }
        elseif ($n -is [System.Management.Automation.Language.VariableExpressionAst]) {
            if ($n.VariablePath.UserPath -match '^(?i:variable):(.*)$' -and (Test-VariableTailReaches (Get-ProviderLeaf $Matches[1]) $Name)) { return 'the variable: drive' }
            if ((Get-ProviderLeaf $n.VariablePath.UserPath) -ieq 'ExecutionContext') { return 'the session-state API ($ExecutionContext)' }
        }
        elseif ($n -is [System.Management.Automation.Language.MemberExpressionAst]) {
            # r10 adversary A3: a quoted member name is read by its parsed value ("GetVariab`leValue" is GetVariableValue).
            # r11 (Dot B11, r10 adversary A1): so is a member name built from literals (.('GetVariableValue'), .('Session'+'State')).
            $memberName = Get-MemberNameValue $n.Member
            if ($null -eq $memberName) {
                # A member name the scan cannot fold may be any of them, on a receiver that may be the session itself.
                if (Test-MemberReceiverMayBeSession $n $Root) { return "a member name the scan cannot read as a string constant ($($n.Member.Extent.Text)), on a receiver that may reach the session-state API" }
                continue
            }
            # r9 adversary A3: $PSCmdlet.GetVariableValue(<name>) hands back a variable's value (the defaults dictionary itself) by a name.
            # r11: .InvokeCommand runs a script or resolves a command in the caller's session (InvokeScript, GetCommand).
            if ($script:sessionMemberNames -icontains $memberName) { return "the session-state API (.$memberName)" }
        }
        elseif ($n -is [System.Management.Automation.Language.TypeExpressionAst] -or $n -is [System.Management.Automation.Language.TypeConstraintAst]) {
            if ($n.TypeName.FullName -match '(?i)(^|\.)(psvariable|SessionState|SessionStateProxy|PSVariableIntrinsics)$') { return "the session-state API ([$($n.TypeName.FullName)])" }
        }
    }
    return ''
}

function Get-DefaultParameterValuesReference($Root) {
    # $null when nothing in $Root can read or change $PSDefaultParameterValues, otherwise @{ Ast; Reason }. A default it holds binds a
    # parameter the call does not name ('Get-AttrCudaEmbeddedFunctionSource:ModulePath', '*:ModulePath', 'Join-Path:AdditionalChildPath',
    # 'New-Item:Name'), so no command in the file binds the way its text reads (r9, sol r8). Any reference counts, not only an
    # assignment: the dictionary is changed in place ([...] =, .Add(), a member set) and can be reached through a second variable.
    foreach ($n in @($Root.FindAll({ param($x) $x -is [System.Management.Automation.Language.VariableExpressionAst] -or
                                               $x -is [System.Management.Automation.Language.StringConstantExpressionAst] -or
                                               $x -is [System.Management.Automation.Language.ExpandableStringExpressionAst] -or
                                               $x -is [System.Management.Automation.Language.BinaryExpressionAst] -or
                                               $x -is [System.Management.Automation.Language.MemberExpressionAst] }, $true))) {
        # r11 (Dot B11, r10 adversary A1): a name built from literals is read by the string it folds to ('PS'+'Default'+'Parameter'+'Values',
        # 'PSDef'+'aultPara'+'meterValues'), whether it is an operand or a member name.
        if ($n -is [System.Management.Automation.Language.BinaryExpressionAst]) {
            $folded = Get-ConstantString $n
            if ($null -ne $folded -and $folded -match '(?i)PSDefault|ParameterValues') { return [pscustomobject]@{ Ast = $n; Reason = "a name built from literals that folds to '$folded'" } }
            continue
        }
        if ($n -is [System.Management.Automation.Language.MemberExpressionAst]) {
            if ((Get-MemberNameValue $n.Member) -ieq 'PSDefaultParameterValues') { return [pscustomobject]@{ Ast = $n; Reason = 'a member named PSDefaultParameterValues' } }
            continue
        }
        if ($n -is [System.Management.Automation.Language.VariableExpressionAst]) {
            $user = $n.VariablePath.UserPath
            if ($user -match '^(?i:variable):(.*)$') {
                if (Test-VariableTailReaches (Get-ProviderLeaf $Matches[1]) 'PSDefaultParameterValues') { return [pscustomobject]@{ Ast = $n; Reason = 'the variable: drive' } }
            }
            elseif ((Get-ProviderLeaf $user) -ieq 'PSDefaultParameterValues') { return [pscustomobject]@{ Ast = $n; Reason = 'a reference to $PSDefaultParameterValues' } }
        }
        # r9 adversary A3: a name built from fragments ('PSDefault' + 'ParameterValues') has no string node of its own, so a fragment counts.
        elseif (([string]$n.Value) -match '(?i)PSDefault|ParameterValues') { return [pscustomobject]@{ Ast = $n; Reason = 'a string that names, or is a fragment of, PSDefaultParameterValues' } }
    }
    $route = Get-VariableRouteBinding $Root 'PSDefaultParameterValues'
    if ($route) { return [pscustomobject]@{ Ast = $Root; Reason = $route } }
    return $null
}

function Test-Provable($Ast, $Root, $Visiting) {
    if ($null -eq $Ast) { return $false }
    # A redirection (`Join-Path $Work 'x' > $null`) discards the value, so the expression is not the path it names.
    if ($Ast -is [System.Management.Automation.Language.CommandExpressionAst]) {
        if ($Ast.Redirections.Count -gt 0) { return $false }
        return (Test-Provable $Ast.Expression $Root $Visiting)
    }
    if ($Ast -is [System.Management.Automation.Language.ParenExpressionAst]) { return (Test-Provable $Ast.Pipeline $Root $Visiting) }
    if ($Ast -is [System.Management.Automation.Language.PipelineAst]) {
        if ($Ast.PipelineElements.Count -ne 1) { return $false }
        return (Test-Provable $Ast.PipelineElements[0] $Root $Visiting)
    }
    if ($Ast -is [System.Management.Automation.Language.CommandAst]) {
        if ($Ast.GetCommandName() -cne 'Join-Path' -or $Ast.InvocationOperator -ne 'Unknown' -or $Ast.Redirections.Count -gt 0) { return $false }
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
        # r9 adversary A3 (advisory, same class): a foreach variable, a multi-target assignment, [ref], a variable parameter or a variable
        # route replaces the value an assignment proved (`foreach ($dest in @('C:\outside'))`), for $Work as for any destination. A
        # parameter of a nested function binds only inside it, so it counts when it encloses THIS use (r9 adversary A4: `function Mutate
        # { param([string]$d) New-Item -Path $d }` writes wherever the caller says, whatever the outer `$d = Join-Path $Work 'ok'` holds).
        if (@(Get-OtherBindings $Root $v.name | Where-Object { $_ -ne 'unbound' }).Count -gt 0 -or (Get-VariableRouteBinding $Root $v.name)) { return $false }
        for ($scope = $Ast.Parent; $null -ne $scope; $scope = $scope.Parent) {
            $parameters = @()
            if ($scope -is [System.Management.Automation.Language.ScriptBlockAst] -and $null -ne $scope.ParamBlock) { $parameters += @($scope.ParamBlock.Parameters) }
            if ($scope -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $null -ne $scope.Parameters) { $parameters += @($scope.Parameters) }
            if (@($parameters | Where-Object { (Get-VariableName $_.Name).name -ieq $v.name }).Count -gt 0) { return $false }
        }
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
    # r9 rule 1: a $PSDefaultParameterValues entry binds parameters a command does not name ('New-Item:Name' hands New-Item the -Name
    # R2 forbids), so with any reference to it the scan's reading of every command here is unproven.
    $defaults = Get-DefaultParameterValuesReference $root
    if ($null -ne $defaults) {
        Add-Violation $Source $defaults.Ast 'unproven-binding' "`$PSDefaultParameterValues is reachable here ($($defaults.Reason)); a default it holds binds parameters no command names, so this scan's reading of every command is unproven" '' 'unproven-binding'
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
        $left = Get-AssignmentTarget $a.Left
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
            if ($left -is [System.Management.Automation.Language.MemberExpressionAst] -and $script:receiverRouteReason) {
                Add-Violation $Source $a 'unproven-binding' "the receiver may be bound by $($script:receiverRouteReason), a route this scan cannot follow, so the object this property set reaches is unproven" '' 'unproven-binding'
            }
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

function Add-SpliceFinding([string]$Source, [int]$Line, [string]$Function, [string]$Text, [string]$Reason, [string]$Token = 'splice') {
    # -Token 'unproven-binding' marks the r9 class: the module (or default) a call binds to cannot be proven statically.
    $splicedViolations.Add([ordered]@{ source = $Source; module = ''; function = $Function; line = $Line; rule = 'splice'; token = $Token; origin = ''; text = $Text; reason = $Reason })
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
    # r9: every other route (a variable parameter by any prefix its command accepts, the variable cmdlets including Get-Variable, the
    # variable: drive, the session-state API) is one shared model, the one an R5 receiver and $PSDefaultParameterValues use too.
    return (Get-VariableRouteBinding $Root 'PSScriptRoot')
}

function Get-SpliceModuleBinding($Call, [string]$SourceDir, [string]$RootRebinding = '', [string]$DefaultsReference = '') {
    # Which module the extractor will read for this call: kind 'default' (no -ModulePath, or an empty / blank /
    # $null one: the extractor's own module), 'path' (a rooted literal or Join-Path $PSScriptRoot '<child>': bound
    # by FULL path), 'dynamic' (any other expression) or 'unsupported' (a call shape whose -ModulePath cannot be
    # read, so the binding is unknown: reported as an unproven-binding finding).
    # r9 rule 1 (sol r8): a $PSDefaultParameterValues entry binds -ModulePath, -Name or Join-Path's arguments that the call does not
    # name ('Get-AttrCudaEmbeddedFunctionSource:ModulePath', '*:ModulePath', 'Join-Path:AdditionalChildPath'), so with any reference to
    # it in the file no call there binds the way its text reads.
    # r10: the call's own shape is read first and its reason kept, so a defaults reference adds to it ('relative', 'module-qualified')
    # instead of hiding it.
    $shapeBinding = Get-SpliceCallShapeBinding $Call $SourceDir $RootRebinding
    if (-not $DefaultsReference) { return $shapeBinding }
    # One route (a computed Set-Variable name, the session-state API) can reach both variables; the reason names each one it reaches.
    $alsoRoot = if ($RootRebinding -and $shapeBinding.reason -notmatch 'PSScriptRoot is rebound') { "; `$PSScriptRoot may be rebound too (via $RootRebinding)" } else { '' }
    $shape = if ($shapeBinding.kind -eq 'unsupported') { "$($shapeBinding.reason); and " } else { '' }
    return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = "${shape}the file that holds the call refers to `$PSDefaultParameterValues ($DefaultsReference), whose defaults can bind -ModulePath, -Name or Join-Path's arguments at run time$alsoRoot" }
}

function Get-SpliceCallShapeBinding($Call, [string]$SourceDir, [string]$RootRebinding = '') {
    # Get-SpliceModuleBinding without the $PSDefaultParameterValues rule: what the call's own text binds.
    # r9 rule 3: PowerShell binds an abbreviated parameter by prefix against the extractor's parameter set as it is at run time, which
    # this scan does not prove (a module-qualified or replaced extractor, or one more parameter, changes what -Mod or -Na selects).
    # Only the exact name or a declared alias of a parameter the extractor has is read; anything else is an unproven binding.
    $exactSpellings = @('Name', 'ModulePath', 'Verbose', 'vb', 'Debug', 'db', 'ErrorAction', 'ea', 'WarningAction', 'wa', 'InformationAction', 'infa',
                        'ErrorVariable', 'ev', 'WarningVariable', 'wv', 'InformationVariable', 'iv', 'OutVariable', 'ov', 'OutBuffer', 'ob',
                        'PipelineVariable', 'pv', 'ProgressAction', 'proga')
    foreach ($element in $Call.CommandElements) {
        if ($element -is [System.Management.Automation.Language.CommandParameterAst] -and $exactSpellings -inotcontains $element.ParameterName) {
            return [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = "the call names -$($element.ParameterName), which is not the exact name or an alias of an extractor parameter, so the parameter it binds by prefix is unproven" }
        }
    }
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
        # A redirection (`'C:\x\Clean.psm1' > $null`) discards the value: the extractor receives nothing, so it is not that path.
        if ($node -is [System.Management.Automation.Language.CommandExpressionAst] -and $node.Redirections.Count -eq 0) { $node = $node.Expression; continue }
        break
    }
    $dynamic =[pscustomobject]@{ kind = 'dynamic'; path = ''; reason = '' }
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
    if ($node -is [System.Management.Automation.Language.CommandAst] -and $node.GetCommandName() -ieq 'Join-Path' -and $node.InvocationOperator -eq 'Unknown' -and $node.Redirections.Count -eq 0) {
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

function Test-FlatNameList($Ast, [bool]$Top) {
    # True when $Ast is a string constant, or (only at the top, where @( ) of one statement is the list itself) a comma list or
    # an @( ) of statements whose every element is a string constant. Parentheses around a string are transparent; a collection
    # inside the list is not, because PowerShell converts it to ONE string for a [string[]] parameter.
    while ($true) {
        if ($Ast -is [System.Management.Automation.Language.ParenExpressionAst]) { $Ast = $Ast.Pipeline; continue }
        if ($Ast -is [System.Management.Automation.Language.PipelineAst] -and $Ast.PipelineElements.Count -eq 1) { $Ast = $Ast.PipelineElements[0]; continue }
        if ($Ast -is [System.Management.Automation.Language.CommandExpressionAst] -and $Ast.Redirections.Count -eq 0) { $Ast = $Ast.Expression; continue }
        break
    }
    if ($Ast -is [System.Management.Automation.Language.StringConstantExpressionAst]) { return $true }
    if (-not $Top) { return $false }
    if ($Ast -is [System.Management.Automation.Language.ArrayExpressionAst]) {
        $statements = @($Ast.SubExpression.Statements)
        if ($statements.Count -eq 1) { return (Test-FlatNameList $statements[0] $true) }
        return (@($statements | Where-Object { -not (Test-FlatNameList $_ $false) }).Count -eq 0)
    }
    if ($Ast -is [System.Management.Automation.Language.ArrayLiteralAst]) {
        return (@($Ast.Elements | Where-Object { -not (Test-FlatNameList $_ $false) }).Count -eq 0)
    }
    return $false
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
    # r10: the source's aliases, and the ones the -ModulePath modules' top-level code defines, before any rule reads a command name.
    $null = Get-AliasContext $ast -Imported
    # The binding below evaluates Join-Path, and the call itself is Get-AttrCudaEmbeddedFunctionSource: a source that
    # defines its own version of either (or of Split-Path / New-Object) makes the scan's evaluation say nothing about it.
    foreach ($shadow in @(Get-ShadowingDefinitions $ast @('Join-Path', 'Split-Path', 'New-Object', 'Get-AttrCudaEmbeddedFunctionSource'))) {
        $shadowText = ($shadow.Ast.Extent.Text -replace '\s+', ' ')
        if ($shadowText.Length -gt 160) { $shadowText = $shadowText.Substring(0, 160) }
        Add-SpliceFinding $source $shadow.Ast.Extent.StartLineNumber '' $shadowText (Get-ShadowReason $shadow.Kind ', so the module binding it computed cannot be trusted')
    }
    # r11 (r10 adversary A2): an alias a -ModulePath module's top-level code defines (`Set-Alias Join-Path Redirect -Scope Global`)
    # replaces, in the importing source, the command the scan evaluates (Join-Path for the binding, the extractor itself), so every
    # call there binds the way the replacement says, not the way its text reads. The source's own aliases are the shadow rule above.
    $importedReplacement = ''
    foreach ($evaluated in @('Join-Path', 'Split-Path', 'New-Object', 'Get-AttrCudaEmbeddedFunctionSource')) {
        $runsAs = @(Resolve-CommandNames $evaluated $script:importedAliases.map $script:importedAliases.wildcard | Where-Object { $_ -ne $evaluated.ToLowerInvariant() })
        if ($runsAs.Count -gt 0) { $importedReplacement = "an alias a -ModulePath module defines makes $evaluated run as $($runsAs -join ', ')"; break }
    }
    # r11 adversary pass 2 (A3): so does a function a -ModulePath module defines under that name (`function Join-Path { ... }`, exported).
    # The extractor's own definition is the extractor module itself, which the default-module rule already judges. A function defined
    # inside another function is local to it when it runs (a spliced body's own shadow, judged by R1), so only a top-level one counts.
    # r12 adversary A1: and so does one it defines through the function: drive (`Set-Item function:global:Join-Path -Value { ... }`,
    # `$function:Join-Path = { ... }`): the module's top level is judged by the same shadow model as the source's own code.
    # r12c adversary A1: and so does one an Import-Module -Prefix renames into it (`function Join-ath` imported with -Prefix P).
    $evaluatedNames = Get-PrefixRenamedNames (@($ast) + @($moduleAsts.Values)) @('Join-Path', 'Split-Path', 'New-Object', 'Get-AttrCudaEmbeddedFunctionSource')
    if (-not $importedReplacement -and $null -eq $evaluatedNames) {
        $importedReplacement = "an Import-Module that splats or names a -Prefix that is not a string constant may rename a command into one the scan evaluates"
    }
    if (-not $importedReplacement) {
        foreach ($moduleKey in @($moduleAsts.Keys)) {
            # r12 adversary pass 2 (A2): the extractor too (`Set-Item function:global:Get-AttrCudaEmbeddedFunctionSource -Value { ... }`);
            # only its own `function` definition is the extractor module, judged by the default-module rule.
            $replaced = @(Get-ShadowingDefinitions $moduleAsts[$moduleKey] $evaluatedNames | Where-Object {
                if ($_.Ast -is [System.Management.Automation.Language.FunctionDefinitionAst] -and (Get-BareCommandName $_.Ast.Name) -eq 'get-attrcudaembeddedfunctionsource') { return $false }
                for ($p = $_.Ast.Parent; $null -ne $p; $p = $p.Parent) { if ($p -is [System.Management.Automation.Language.FunctionDefinitionAst]) { return $false } }
                return $true } | Select-Object -First 1)
            if ($replaced.Count -gt 0) {
                $replacedText = ($replaced[0].Ast.Extent.Text -replace '\s+', ' ')
                if ($replacedText.Length -gt 120) { $replacedText = $replacedText.Substring(0, 120) }
                $importedReplacement = "an alias or function a -ModulePath module defines ($moduleKey) replaces a command the scan evaluates ($replacedText)"
                break
            }
        }
    }
    $rootRebinding = Get-PsScriptRootRebinding $ast
    $defaults = Get-DefaultParameterValuesReference $ast
    $defaultsReference = if ($null -ne $defaults) { $defaults.Reason } else { '' }
    # Command names are case-insensitive and a module-qualified name (<Module>\Get-AttrCudaEmbeddedFunctionSource)
    # resolves to the same command: compare the part after the last backslash, ignoring case.
    $extractorCalls = $ast.FindAll({
        param($n)
        if ($n -isnot [System.Management.Automation.Language.CommandAst]) { return $false }
        # r10 (adversary A2 sibling, A4): `& ('Get-AttrCudaEmbeddedFunctionSource')` and an alias the source defines for it are calls too.
        $commandName = Get-LiteralCommandName $n
        if ($null -eq $commandName) { return $false }
        if ($commandName.Substring($commandName.LastIndexOf('\') + 1) -ieq 'Get-AttrCudaEmbeddedFunctionSource') { return $true }
        return (@(Get-ResolvedCommandNames $n) -contains 'get-attrcudaembeddedfunctionsource')
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
        # A comma list inside a comma list (`(('Dan','ger'),'Safe')`) binds its inner list as ONE joined string, not as its elements.
        if (-not $dynamic -and @($argument.FindAll({ param($n) $n -is [System.Management.Automation.Language.ArrayLiteralAst] }, $true)).Count -gt 1) { $dynamic = $true }
        $names = @()
        if (-not $dynamic) {
            $names = @($argument.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) | ForEach-Object { [string]$_.Value })
            if ($argument -is [System.Management.Automation.Language.StringConstantExpressionAst]) { $names = @([string]$argument.Value) }
        }
        # r9 adversary A1: a collection inside the list (`@(@('Dan'; 'ger'), 'Safe')`) binds as ONE string joined by $OFS, whatever
        # node builds it, so only a string, or a flat list whose every element is a string, is a list of names.
        if (-not $dynamic -and -not (Test-FlatNameList $argument $true)) { $dynamic = $true }
        if ($dynamic -or $names.Count -eq 0) {
            Add-SpliceFinding $source $call.Extent.StartLineNumber '' $text 'the -Name list is not a literal list of function names, so the spliced bodies cannot be enumerated'
            continue
        }
        $callName = Get-LiteralCommandName $call
        $qualifier = if ($callName.Contains('\')) { $callName.Substring(0, $callName.LastIndexOf('\')) } else { '' }
        $bind = Get-SpliceModuleBinding $call $sourceDir $rootRebinding $defaultsReference
        if ($callName.Substring($callName.LastIndexOf('\') + 1) -ine 'Get-AttrCudaEmbeddedFunctionSource') {
            # Reached through an alias the source defines: which extractor (and so which module) it runs is not what the text names.
            $bind = [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = "the extractor is called through the alias '$callName' the source defines, so the extractor it runs is unproven" }
        }
        else {
            # r12c adversary A3 sibling: an alias named by the extractor's own spelling, qualified or not
            # (`Set-Alias -Name 'AttrCudaArtifacts\Get-AttrCudaEmbeddedFunctionSource' -Value Redirect`), runs in its place.
            $runsAs = @(Get-ResolvedCommandNames $call | Where-Object { $_ -ne 'get-attrcudaembeddedfunctionsource' })
            if ($runsAs.Count -gt 0) {
                $bind = [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = "an alias named '$callName' makes the call run as $($runsAs -join ', '), so the extractor it runs is unproven" }
            }
        }
        if ($importedReplacement) {
            $prior = if ($bind.kind -eq 'unsupported') { "$($bind.reason); and " } else { '' }
            $bind = [pscustomobject]@{ kind = 'unsupported'; path = ''; reason = "${prior}$importedReplacement, so the module this call reads is unproven" }
        }
        [pscustomobject]@{ source = $source; line = $call.Extent.StartLineNumber; text = $text; names = $names; qualifier = $qualifier; bind = $bind }
    }
}

if ($SpliceSource.Count -gt 0) {
    $moduleTexts = [ordered]@{}
    $moduleFullPaths = @{}
    $moduleAsts = [ordered]@{}
    foreach ($modulePath in $ModulePath) {
        $key = Get-RepoRelativePath $modulePath
        $full = (Resolve-Path -LiteralPath $modulePath).Path
        $moduleTexts[$key] = [IO.File]::ReadAllText($full)
        $moduleFullPaths[$key] = [IO.Path]::GetFullPath($full)
        # r9 rule 1: a generator imports these modules, and their top-level code runs in its session, so a reference to
        # $PSDefaultParameterValues in one rebinds an extractor call as one in the generator does.
        $moduleTokens = $null; $moduleErrors = $null
        $moduleAst = [System.Management.Automation.Language.Parser]::ParseInput($moduleTexts[$key], [ref]$moduleTokens, [ref]$moduleErrors)
        # r10: the aliases its top-level code defines are the generator's aliases too (Get-AliasContext -Imported).
        $moduleAsts[$key] = $moduleAst
    }
    # r10: the aliases the modules' top-level code defines, read together until nothing is added (adversary A1: an alias of Set-Alias
    # one module defines can define the alias another module uses), so a chain across modules is followed like one inside a file.
    for ($pass = 0; $pass -lt 8; $pass++) {
        $before = @($script:importedAliases.map.Values | ForEach-Object { @($_).Count } | Measure-Object -Sum).Sum + $script:importedAliases.wildcard.Count
        foreach ($moduleKey in @($moduleAsts.Keys)) {
            $moduleAliases = Get-AliasDefinitions $moduleAsts[$moduleKey] $script:importedAliases.map @($script:importedAliases.wildcard) -TopLevelOnly
            $script:importedAliases.map = $moduleAliases.map
            $script:importedAliases.wildcard = $moduleAliases.wildcard
            if ($moduleAliases.problem -and -not $script:importedAliases.problem) { $script:importedAliases.problem = "$($moduleAliases.problem) ($moduleKey)" }
        }
        $after = @($script:importedAliases.map.Values | ForEach-Object { @($_).Count } | Measure-Object -Sum).Sum + $script:importedAliases.wildcard.Count
        if ($after -eq $before) { break }
        if ($pass -eq 7 -and -not $script:importedAliases.problem) { $script:importedAliases.problem = 'aliases of alias commands the scan stopped following after eight passes' }
    }
    # r11 adversary A2: a module's reference to $PSDefaultParameterValues is judged only after every module's aliases are known, and
    # through them (`Set-Alias Fetch Get-Variable` in one module, `(Fetch P*Values).Value[...]` in another), so the module's alias
    # context is built -Imported before anything reads (and caches) it.
    foreach ($key in @($moduleAsts.Keys)) {
        $null = Get-AliasContext $moduleAsts[$key] -Imported
        $moduleDefaults = Get-DefaultParameterValuesReference $moduleAsts[$key]
        if ($null -ne $moduleDefaults) {
            Add-SpliceFinding $key $moduleDefaults.Ast.Extent.StartLineNumber '' $key "the module refers to `$PSDefaultParameterValues ($($moduleDefaults.Reason)), whose defaults can bind any extractor call's -ModulePath or -Name at run time" 'unproven-binding'
        }
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
            # r10: read even when the binding already failed, so the reason names the qualifier too.
            if ($site.qualifier -and $site.qualifier -ine 'AttrCudaArtifacts' -and
                -not ($candidates.Count -eq 1 -and [IO.Path]::GetFileNameWithoutExtension($candidates[0]) -ieq $site.qualifier)) {
                $qualified = "the call is module-qualified by '$($site.qualifier)', which is not the module this scan binds"
                $bindingFailure = if ($bindingFailure) { "$qualified; $bindingFailure" } else { $qualified }
            }
            if ($bindingFailure) {
                Add-SpliceFinding $site.source $site.line '' $site.text "the extractor module this call reads cannot be bound: $bindingFailure" 'unproven-binding'
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
                    Add-SpliceFinding $site.source $site.line $functionName $site.text "'$functionName' is defined by more than one candidate module ($($definers -join ', ')) and the call does not bind it to one" 'unproven-binding'
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
