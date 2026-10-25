#Requires -Version 7.0
using namespace System.Management.Automation.Language
using namespace System.Collections.Generic
<#
Launch-site census scanner for tests/coordination/test_retire_lane_worktree.py (LANE-RETIRE-LAUNCH-SITE-CENSUS-1 r3). Test code: it
parses text and launches nothing.

Reads one JSON object {"<name>": "<PowerShell source>", ...} on stdin and writes {"<name>": [site, ...]} on stdout. A site is
{"kind": "encoded" | "detached" | "unresolved", "line": <1-based line in that source>, "text": "<site text>"}. Sites come from the
PowerShell parser's own AST (Parser::ParseInput), so a statement, a quote, a comment, a here-string or a $( ) means what pwsh says it
means. Every CommandAst at every depth is examined (script blocks, $( ) inside expandable strings, pipelines, && / || chains, if/else
branches), so two launches in one statement are two sites. The site text is the command (or, for a string or a type, its enclosing
statement) with comments and line continuations dropped and whitespace runs collapsed to one space.

Rules, written down because the census depends on them:
 encoded    E1. A command whose target is pwsh / powershell (by name or path, .exe optional, or `& $v` where an assignment to $v in
                the same source names pwsh or powershell), or any `&` / `.` invocation whose target is not a static name, that carries
                a switch pwsh resolves to -EncodedCommand. pwsh's own CLI rule (CommandLineParameterParser): `-ec`, or any prefix of
                `encodedcommand` of one letter or more; `-ex` / `-ep` are -ExecutionPolicy. A switch is a -x parameter or a -x / /x
                string or bare word. On a target that is not known to be pwsh the one-letter `-e` is also winget's and git's switch, so
                there it counts only beside a pwsh-only switch (-NoProfile, -NonInteractive, -NoLogo, -NoExit, -ExecutionPolicy, ...).
            E2. A quoted string that is not an element of an E1 command and whose own code (rule S) holds no site: exactly a switch
                spelling ('-enc', "/ec"), where -ec and spellings of 4+ letters count alone and -e / -en / -enc need a pwsh word or a
                pwsh-only switch within 4 lines; or a longer string holding such a switch word with that context within 4 lines (a
                command line built as text).
 detached   D1. Start-Process, or its aliases saps / start, given WindowStyle Hidden: as `-WindowStyle Hidden`, `-WindowStyle:Hidden`,
                any prefix Start-Process itself resolves to WindowStyle (read from its own parameter and alias table at run time:
                `-wi` is WhatIf's alias, so `-win` is the shortest), or through a splatted variable whose hashtable literal, or a later
                `$h.WindowStyle =` / `$h['WindowStyle'] =`, sets it. A constant value other than Hidden / 1 (Normal, Minimized, ...) is
                not a site; a value that is not a constant is. A splat with no hashtable literal behind it is `unresolved`.
            D2. Win32_Process Create: Invoke-CimMethod / Invoke-WmiMethod with Create in the command; a .Create(...) call on an
                expression naming Win32_Process or wmiclass (or on a variable assigned from Win32_Process); and a [wmiclass] cast or
                type literal that is not already inside such a call.
 S          Every quoted string literal (single- or double-quoted, here-strings included; for an expandable string, its template with
            the $( ) and variable parts blanked, because those parts are AST code already) that mentions a launch word is itself parsed
            as PowerShell and scanned by these same rules, its sites reported at their own line in the outer source. Over-reporting a
            string that is never run is accepted; hiding one that is run is not.
 unresolved X.  The argument of [scriptblock]::Create, .InvokeScript / .NewScriptBlock / .AddScript, Invoke-Expression / iex (or its
                pipeline input, when it has no argument), or -Command / -c of a pwsh command, when it is neither a string or { } literal
                nor a variable that resolves to one (it is built at run time). A source pwsh cannot parse is one `unresolved` site.
            V.  (r4, Dot B1) A variable resolves only through the assignments in the scope that reads it (its nearest script block: the
                file, a function body, a { } literal) or in a script block inside that scope; every one must be a plain `=` to a literal,
                and one must be a top-level statement of that scope before the read. `$script:x` is read against the file's top scope.
                A scope's parameters, automatic variables ($args, $input, $_, ...), foreach variables, `$a, $b =`, [ref], -OutVariable /
                -PipelineVariable style bindings, Set-Variable / New-Variable names, and $global: / $using: reads never resolve. So a
                nested scope that reads an outer literal it does not assign itself is `unresolved` (conservative, by choice), and so is
                a dot-sourced script reading its caller's variable. A splat is D1 `none` only under the same rule.
            V2. (r5, Dot 6392164637, Grok G1) A write lands in the scope its qualifier names; it is never stripped and re-homed. local: and
                private: are the lexical scope. script: is the script scope: it reaches every read of the name, and resolves only a
                `$script:x` read, as a top-level `$script:x =` before it (a dot-sourced file's top level can be a function's scope, so a
                plain `$x =` there never resolves `$script:x`, nor `$script:x =` a plain `$x`). global: and variable: writes cannot be
                placed, so the name is tainted (never resolves). Ordering holds for every read: a literal after it never resolves it.
                Set-Variable / New-Variable / Remove-Variable / Clear-Variable (set, sv, nv, rv, clv) bind like pwsh: -Name, else the first
                positional left once named parameters (-Scope Local, -Option None, switches) take their values; a non-constant name, a
                splat or an unknown parameter taints every name. A constant string anywhere that names the variable: drive (variable:x,
                variable:\x, Variable::x) taints x; with no name or a wildcard it taints every name. An item cmdlet (Set-Item, New-Item,
                Set-Content, Add-Content, Clear-Content, Clear-Item, Remove-Item, Rename-Item, Copy-Item, Move-Item and aliases) whose
                path, destination or new name is not a constant taints every name, as does a location cmdlet given variable: or a path
                that is not a constant. Any use of .PSVariable, .InvokeProvider, .SessionStateProxy or .SetVariable, or of .SessionState
                other than .SessionState.Path, taints every name; so does a `.Value` write or set_Value call in a file that also calls
                Get-Variable without -ValueOnly or a Get-Item / Get-ChildItem command (a PSVariable object's .Value is the variable).
                Code run in this scope writes names too: `.` of anything but a { } literal, Set-Alias / New-Alias, and `& $x` / `. $x` on
                a target that is not a static name in a file that names a variable or item cmdlet as a string (or calls Get-Command)
                taint every name; the text an iex / [scriptblock]::Create / .InvokeScript / .NewScriptBlock runs is parsed and each name
                it writes is tainted, and a text not resolved to literals taints every name. Bound: one file at a time, so a function
                in another file that writes through its caller's SessionState is caught only in its own file.
            V3. (r7, Dot a3d55115, Grok G1; hub ruling HUB-TICK 20261025T0526Z) Rule V is closed by an allowlist. A read of `$x` resolves
                only when every construct in the file that binds or could bind x is one of: (a) `$x = <constant>` in the reading scope
                before the read (rule V); (b) a `foreach ($x in ...)` header (it never resolves the read, it only does not taint); (c) a
                param of a function, of a class method, of the file, or of a { } literal run with `&` (none of these is dot-sourced;
                `. F` and `. $sb` already taint every name). Anything else that names x taints x: a param of any other { } literal (`.`,
                ForEach-Object, a block kept in a variable), the target of ++ / --, an assignment through any number of casts and
                attributes. Anything that could name a variable dynamically taints every name: a member access or call whose member
                name is not a constant string (`.$m`, `.($m)`), a non-constant variable-name argument, a splat whose value is not a
                hashtable literal of this file (a parameter, $PSBoundParameters, @args, a call result), and, in a file with a splat, a
                hashtable key or a member / index write whose key is not constant, or a method call given a binder name. Binder
                parameters: any parameter whose name holds `variable`, an alias or a prefix (2+ letters) of a *Variable* parameter
                of a cmdlet that ships with pwsh ($VarParamNames / $VarParamAliases), and the common -*Variable parameters; the same
                words as hashtable keys or member names in a file with a splat; every positional argument of a cmdlet whose
                *Variable* parameter is positional (Import-LocalizedData); -Name or the first positional of the *-Variable cmdlets.
                Get-Variable's -ValueOnly counts only as the bare switch or `:$true` with no splat. tools/repo_hygiene/
                test_launch_site_census_binders.py walks the live Get-Command parameter metadata and fails on any binder missed here.
                (r8, Dot 6394696125; hub ruling HUB-TICK 20261025T0706Z) Splat sources are on the allowlist too. A splat `@h` has a known
                key set only when a hashtable literal assigned in the splat's own scope reaches it (rule V, with every assignment to h a
                hashtable literal) and every use of h in the file is one of: the splat; `$h = @{ <constant keys> }`; `$h.Add(<constant>,
                ...)` or another keyed Hashtable method (set_Item, get_Item, Remove, ContainsKey, Contains) with a constant key, or Clear /
                ContainsValue / Clone / GetEnumerator; `$h[<constant>]`, read or written; `$h.<constant> = ...`; a read of .Count / .Keys
                / .Values. Anything else that names h (a non-constant key or member name, h passed to or piped into a command, assigned
                to another name, cast or [ref], reached through another member such as .psbase, or qualified) makes the key set unknown,
                and an unknown splat on any command could bind every *Variable* parameter, so it taints every name. An array splat
                (positional binding) never has a known key set here: its value is not a hashtable literal, so it taints every name.
                (r9, CoS/Dot 6397324853 on #669; LAUNCH-SITE-CENSUS-PSDEFAULTPARAMETERVALUES-BINDER-1) $PSDefaultParameterValues gives a
                default to every later command that has the parameter, so it is read with or without a splat in the file. Its key is
                `<command>:<parameter>`; when the parameter part is a binder parameter (above), or holds a wildcard (pwsh binds one that
                matches a single parameter, `*:OutVa*`), the value names a variable that is tainted. Every use of the name (any qualifier)
                must be one of: `= / += @{ ... }`; `[<key>] = v`; `.<key> = v`; `.Add(<key>, v)` / `.set_Item(<key>, v)`; an index read;
                a call of Remove, ContainsKey, Contains, get_Item, Clear, ContainsValue, Clone or GetEnumerator; a read of .Count / .Keys /
                .Values. A key that is not constant, a right side that is not a hashtable literal, any other use (aliased to another
                name, passed to or piped into a command, [ref], cast), and a constant string naming PSDefaultParameterValues (Set-Variable,
                the variable: drive) taint every name. (r9 round 2, #678) So do an entry write with any operator other than `=` (`+=`,
                `??=`, ...: the value it leaves is not read), and a table assignment whose value is taken (`$x = ($PSDefaultParameterValues
                = @{})` aliases the table): only one that is a statement of its own block is read.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 3.0

$MaxDepth = 6
$CtxRx = [regex]::new('pwsh|powershell|(?<![\w-])[-/](?:NoProfile|NoLogo|NonInteractive|NoExit|ExecutionPolicy|Sta|Mta|ConfigurationName)\b', 'IgnoreCase')
$PwshOnly = @('noprofile', 'nologo', 'noninteractive', 'noexit', 'executionpolicy', 'sta', 'mta', 'configurationname')
$LaunchWordRx = [regex]::new('[-/]e|start|saps|pwsh|powershell|win32_process|wmiclass|invoke|iex|scriptblock', 'IgnoreCase')
$SwitchWordRx = [regex]::new('(?:^|(?<=[\s(,@=''"]))[-/](?<w>[A-Za-z]+)(?=[\s,)''":]|$)')
$WmiTypeRx = [regex]::new('^(wmiclass|System\.Management\.ManagementClass)$', 'IgnoreCase')
$SbTypeRx = [regex]::new('^(System\.Management\.Automation\.)?scriptblock$', 'IgnoreCase')

$SpKeys = [List[object]]::new()
foreach ($p in (Get-Command Microsoft.PowerShell.Management\Start-Process).Parameters.Values) {
    $SpKeys.Add(@($p.Name.ToLowerInvariant(), $p.Name))
    foreach ($a in $p.Aliases) { $SpKeys.Add(@($a.ToLowerInvariant(), $p.Name)) }
}

function Resolve-StartProcessParameter([string]$Word) {
    $w = $Word.ToLowerInvariant()
    foreach ($k in $SpKeys) { if ($k[0] -eq $w) { return $k[1] } }
    $names = @($SpKeys | Where-Object { $_[0].StartsWith($w, [StringComparison]::Ordinal) } | ForEach-Object { $_[1] } | Select-Object -Unique)
    if ($names.Count -eq 1) { return $names[0] }
    return $null
}

function Test-EncodedWord([string]$Word, [bool]$AllowE) {
    $w = $Word.ToLowerInvariant()
    if ($w -eq 'ec') { return $true }
    if (-not 'encodedcommand'.StartsWith($w, [StringComparison]::Ordinal)) { return $false }
    return ($w.Length -ge 2 -or $AllowE)
}

function Get-SwitchWord($El) {
    if ($El -is [CommandParameterAst]) { return $El.ParameterName }
    if ($El -is [StringConstantExpressionAst] -and $El.Value -match '^[-/]([A-Za-z]+)(?::.*)?$') { return $Matches[1] }
    return $null
}

# The expression that carries a value: parentheses, one-element pipelines and casts are looked through.
function Get-Inner($Ast) {
    $x = $Ast
    for ($i = 0; $i -lt 16 -and $null -ne $x; $i++) {
        if ($x -is [ParenExpressionAst]) { $x = $x.Pipeline }
        elseif ($x -is [PipelineAst] -and $x.PipelineElements.Count -eq 1) { $x = $x.PipelineElements[0] }
        elseif ($x -is [CommandExpressionAst]) { $x = $x.Expression }
        elseif ($x -is [ConvertExpressionAst]) { $x = $x.Child }
        else { break }
    }
    return $x
}

$QualRx = '^(?i)(script|global|local|private|using|variable):'
function Get-VarKey($V) { return ($V.VariablePath.UserPath -replace $QualRx, '').ToLowerInvariant() }
function Get-VarQualifier($V) { if ($V.VariablePath.UserPath -match $QualRx) { return $Matches[1].ToLowerInvariant() }; return '' }

# r4 (Dot B1): names pwsh binds itself, and the parameters that bind a variable by name. Neither is ever a literal assignment here.
$AutoVars = [HashSet[string]]::new([string[]]@('args', 'input', '_', 'psitem', 'this', 'matches', 'foreach', 'switch', 'sender', 'event', 'eventargs',
        'eventsubscriber', 'myinvocation', 'psboundparameters', 'pscmdlet', 'error', 'lastexitcode'))
$BindParamRx = [regex]::new('^(?i)(ov|ev|wv|iv|pv|outv\w*|errorv\w*|warningv\w*|informationv\w*|pipelinev\w*)$')
# r7 (rule V3): the *Variable* parameters (and their aliases) of the cmdlets that ship with pwsh, listed by hand so the census reads the same
# on every platform. A parameter word that holds `variable`, is one of these aliases, or is a prefix (2+ letters) of one of these names
# binds a variable by name. test_launch_site_census_binders.py walks the live metadata and fails on any binder these words miss.
$VarParamNames = @('variable', 'bindingvariable', 'sessionvariable', 'statuscodevariable', 'responseheadersvariable', 'variablestoexport',
    'variabledefinitions', 'environmentvariables', 'outvariable', 'errorvariable', 'warningvariable', 'informationvariable', 'pipelinevariable')
$VarParamAliases = [HashSet[string]]::new([string[]]@('ov', 'ev', 'wv', 'iv', 'pv', 'rhv', 'sv', 'v'))
$PositionalVarBinders = [HashSet[string]]::new([string[]]@('import-localizeddata'))

function Test-BinderParameter([string]$Word) {
    $w = $Word.ToLowerInvariant()
    if ($BindParamRx.IsMatch($w) -or $w.Contains('variable') -or $VarParamAliases.Contains($w)) { return $true }
    if ($w.Length -lt 2) { return $false }
    foreach ($n in $VarParamNames) { if ($n.StartsWith($w, [StringComparison]::Ordinal)) { return $true } }
    return $false
}

# r5 (rule V2): cmdlets that bind a variable by name, item cmdlets that write through a provider path, and location cmdlets. Aliases are
# listed by hand so the census reads the same on every platform; parameters come from each cmdlet's own metadata at run time.
$VarCmds = @{ 'set-variable' = 'Set-Variable'; 'set' = 'Set-Variable'; 'sv' = 'Set-Variable'; 'new-variable' = 'New-Variable'; 'nv' = 'New-Variable'
    'remove-variable' = 'Remove-Variable'; 'rv' = 'Remove-Variable'; 'clear-variable' = 'Clear-Variable'; 'clv' = 'Clear-Variable'
    'get-variable' = 'Get-Variable'; 'gv' = 'Get-Variable'
}
$ItemCmds = @{ 'set-item' = 'Set-Item'; 'si' = 'Set-Item'; 'new-item' = 'New-Item'; 'ni' = 'New-Item'; 'set-content' = 'Set-Content'; 'sc' = 'Set-Content'
    'add-content' = 'Add-Content'; 'ac' = 'Add-Content'; 'clear-content' = 'Clear-Content'; 'clc' = 'Clear-Content'; 'clear-item' = 'Clear-Item'
    'cli' = 'Clear-Item'; 'remove-item' = 'Remove-Item'; 'ri' = 'Remove-Item'; 'rm' = 'Remove-Item'; 'rmdir' = 'Remove-Item'; 'del' = 'Remove-Item'
    'erase' = 'Remove-Item'; 'rd' = 'Remove-Item'; 'rename-item' = 'Rename-Item'; 'rni' = 'Rename-Item'; 'ren' = 'Rename-Item'; 'copy-item' = 'Copy-Item'
    'cpi' = 'Copy-Item'; 'cp' = 'Copy-Item'; 'copy' = 'Copy-Item'; 'move-item' = 'Move-Item'; 'mi' = 'Move-Item'; 'mv' = 'Move-Item'; 'move' = 'Move-Item'
}
$LocationCmdRx = [regex]::new('^(?i)(set-location|sl|cd|chdir|push-location|pushd)$')
$ItemPathParams = [HashSet[string]]::new([string[]]@('Path', 'LiteralPath', 'Destination', 'NewName', 'Name'), [StringComparer]::OrdinalIgnoreCase)
$VarDriveRx = [regex]::new('^(?i)(?:(?:Microsoft\.PowerShell\.Core\\)?Variable::|Variable:)[\\/]*(?<n>.*)$')
$VarObjectCmdRx = [regex]::new('^(?i)(get-item|gi|get-childitem|gci|dir|ls)$')
$StateMembers = [HashSet[string]]::new([string[]]@('psvariable', 'invokeprovider', 'sessionstateproxy', 'setvariable'))

# One entry per parameter name and alias of a cmdlet: @(lower name, @{ Name; Switch; Pos }); Pos is its lowest position in any set.
$ParamTables = @{}
foreach ($c in @($VarCmds.Values + $ItemCmds.Values | Select-Object -Unique)) {
    $t = [List[object]]::new()
    foreach ($p in (Get-Command -Name $c -CommandType Cmdlet).Parameters.Values) {
        $ps = @($p.ParameterSets.Values | ForEach-Object { $_.Position } | Where-Object { $_ -ge 0 })
        $e = @{ Name = $p.Name; Switch = $p.SwitchParameter; Pos = if ($ps.Count) { ($ps | Measure-Object -Minimum).Minimum } else { -1 } }
        $t.Add(@($p.Name.ToLowerInvariant(), $e))
        foreach ($a in $p.Aliases) { $t.Add(@($a.ToLowerInvariant(), $e)) }
    }
    $ParamTables[$c] = $t
}

function Resolve-CmdletParameter($Table, [string]$Word) {
    $w = $Word.ToLowerInvariant()
    foreach ($k in $Table) { if ($k[0] -eq $w) { return $k[1] } }
    $hits = @($Table | Where-Object { $_[0].StartsWith($w, [StringComparison]::Ordinal) } | ForEach-Object { $_[1] })
    $names = @($hits | ForEach-Object { $_.Name } | Select-Object -Unique)
    if ($names.Count -eq 1) { return $hits[0] }
    return $null
}

# Binds a command's elements as pwsh does for a cmdlet: -P value / -P:value, a switch takes no value, then the positional elements fill
# the unbound parameters in position order. Returns @{ Bound = @{ <param> = [List] values }; Ok = $false } when a parameter is unknown or
# ambiguous, a splat is present, or a positional has no parameter to bind to (the binding is then not decided by this source).
function Get-CmdletBinding($Els, $Table) {
    $bound = @{}; $ok = $true; $positional = [List[object]]::new()
    for ($i = 1; $i -lt $Els.Count; $i++) {
        $el = $Els[$i]
        if ($el -is [CommandParameterAst]) {
            $p = Resolve-CmdletParameter $Table $el.ParameterName
            if ($null -eq $p) { $ok = $false; continue }
            if ($p.Switch) { continue }
            $v = $el.Argument
            if ($null -eq $v -and $i + 1 -lt $Els.Count -and -not ($Els[$i + 1] -is [CommandParameterAst])) { $i++; $v = $Els[$i] }
            if ($null -eq $bound[$p.Name]) { $bound[$p.Name] = [List[object]]::new() }
            if ($null -ne $v) { $bound[$p.Name].Add($v) }
            continue
        }
        if ($el -is [VariableExpressionAst] -and $el.Splatted) { $ok = $false; continue }
        $positional.Add($el)
    }
    $slots = @($Table | ForEach-Object { $_[1] } | Where-Object { $_.Pos -ge 0 } | Sort-Object { $_.Pos }, { $_.Name } -Unique)
    $k = 0
    foreach ($s in $slots) {
        if ($k -ge $positional.Count) { break }
        if ($bound.ContainsKey($s.Name)) { continue }
        $bound[$s.Name] = [List[object]]::new(); $bound[$s.Name].Add($positional[$k]); $k++
    }
    if ($k -lt $positional.Count) { $ok = $false }
    return @{ Bound = $bound; Ok = $ok }
}

# The value of a constant string argument (quoted, bare word, or an expandable string with nothing expanded), else $null.
function Get-ConstString($V) {
    $x = Get-Inner $V
    if ($x -is [StringConstantExpressionAst]) { return $x.Value }
    if ($x -is [ExpandableStringExpressionAst] -and $x.NestedExpressions.Count -eq 0) { return $x.Value }
    return $null
}

# A constant path that names the variable: drive taints that name; with no name, or a wildcard, it taints every name.
function Add-VarDriveTaint($Ctx, [string]$S) {
    $m = $VarDriveRx.Match($S)
    if (-not $m.Success) { return $false }
    $n = $m.Groups['n'].Value
    if ($n -eq '' -or $n -match '[*?\[`]') { $Ctx.TaintAll = $true } else { [void]$Ctx.Tainted.Add(($n -replace $QualRx, '').ToLowerInvariant()) }
    return $true
}

# The scope an AST node runs in: its nearest enclosing script block (a file, a function body, a { } literal).
function Get-Scope($Ast) {
    $p = $Ast.Parent
    while ($null -ne $p -and -not ($p -is [ScriptBlockAst])) { $p = $p.Parent }
    return $p
}

function Test-Within($Inner, $Outer) {
    for ($p = $Inner; $null -ne $p; $p = $p.Parent) { if ([object]::ReferenceEquals($p, $Outer)) { return $true } }
    return $false
}

# The parameter names a scope binds: its param( ) block, and `function F($a)` / class-method parameters.
function Get-ScopeParameters($S) {
    $names = [HashSet[string]]::new()
    $lists = @()
    if ($null -ne $S.ParamBlock) { $lists += , $S.ParamBlock.Parameters }
    if (($S.Parent -is [FunctionDefinitionAst] -or $S.Parent -is [FunctionMemberAst]) -and $null -ne $S.Parent.Parameters) { $lists += , $S.Parent.Parameters }
    foreach ($l in $lists) { foreach ($p in $l) { [void]$names.Add((Get-VarKey $p.Name)) } }
    return , $names
}

# r7 (rule V3): $true for a param that binds in a scope of its own: a function's, a class method's, the file's, or a { } literal's that is
# run with `&`. A { } literal can be dot-sourced (`.`, ForEach-Object, a block kept in a variable), and then its params are the caller's.
function Test-OwnScopeParameter($P) {
    $owner = $P.Parent
    if ($owner -is [FunctionDefinitionAst] -or $owner -is [FunctionMemberAst]) { return $true }
    if (-not ($owner -is [ParamBlockAst])) { return $false }
    $sb = $owner.Parent
    if ($null -eq $sb.Parent -or $sb.Parent -is [FunctionDefinitionAst] -or $sb.Parent -is [FunctionMemberAst]) { return $true }
    $e = $sb.Parent
    $cmd = $e.Parent
    return ($e -is [ScriptBlockExpressionAst] -and $cmd -is [CommandAst] -and $cmd.InvocationOperator -eq [TokenKind]::Ampersand -and
        [object]::ReferenceEquals($cmd.CommandElements[0], $e))
}

function Add-Assign($Ctx, $V, $Op, $Right, $Scope, [bool]$Direct, [int]$End, [bool]$Script = $false) {
    $k = Get-VarKey $V
    if ($null -eq $Ctx.Assign[$k]) { $Ctx.Assign[$k] = [List[object]]::new() }
    $Ctx.Assign[$k].Add(@{ Op = $Op; Right = $Right; Scope = $Scope; Direct = $Direct; End = $End; Script = $Script })
}

# r5 (rule V2): a write lands in the scope its qualifier names. global: / variable: / using: cannot be placed by this source, so the name
# is tainted; script: is kept as a script-scope write; local: / private: / no qualifier are the lexical scope. Another drive ($env:x) is
# its own name.
function Add-Write($Ctx, $V, $Op, $Right, $Scope, [bool]$Direct, [int]$End) {
    $q = Get-VarQualifier $V
    if ($q -eq 'global' -or $q -eq 'variable' -or $q -eq 'using') { [void]$Ctx.Tainted.Add((Get-VarKey $V)); return }
    Add-Assign $Ctx $V $Op $Right $Scope $Direct $End ($q -eq 'script')
}

# A variable named by a constant string (-OutVariable cmd, Set-Variable -Name cmd) is bound at run time; a name that is not a constant
# could be any variable, so then nothing in this source resolves.
function Add-Taint($Ctx, $V) {
    $x = Get-Inner $V
    if ($x -is [ArrayLiteralAst]) { foreach ($e in $x.Elements) { Add-Taint $Ctx $e }; return }
    if ($x -is [StringConstantExpressionAst]) {
        if ($x.Value -match '[*?\[`]') { $Ctx.TaintAll = $true; return }
        [void]$Ctx.Tainted.Add(($x.Value -replace '^\+', '' -replace $QualRx, '').ToLowerInvariant()); return
    }
    $Ctx.TaintAll = $true
}

function Add-CommandBindings($Ctx, $Cmd) {
    $els = $Cmd.CommandElements
    $name = $Cmd.GetCommandName()
    $leaf = if ($name) { ($name -split '\\')[-1].ToLowerInvariant() } else { '' }
    for ($i = 1; $i -lt $els.Count; $i++) {
        $el = $els[$i]
        if ($el -is [CommandParameterAst] -and (Test-BinderParameter $el.ParameterName)) {
            # r7 (V3): a binder with no value binds nothing, except the common ones r4 already read that way
            $v = Get-ParameterValue $els $i
            if ($null -ne $v) { Add-Taint $Ctx $v } elseif ($BindParamRx.IsMatch($el.ParameterName)) { $Ctx.TaintAll = $true }
        }
        if ($el -is [VariableExpressionAst] -and $el.Splatted) { $Ctx.Splats.Add($el) }
        elseif ($PositionalVarBinders.Contains($leaf) -and -not ($el -is [CommandParameterAst])) { Add-Taint $Ctx $el }
        $s = Get-ConstString $el
        if ($null -ne $s -and $LocationCmdRx.IsMatch($leaf) -and $VarDriveRx.IsMatch($s)) { $Ctx.TaintAll = $true }
    }
    if ($LocationCmdRx.IsMatch($leaf)) {
        foreach ($el in @($els | Select-Object -Skip 1 | Where-Object { -not ($_ -is [CommandParameterAst]) })) { if ($null -eq (Get-ConstString $el)) { $Ctx.TaintAll = $true } }
    }
    # Code that runs in this scope from text or from another file can write any name: `. <anything but a { } literal>`, iex, an alias
    # defined here, or `& $x` / `. $x` on a target that is not a static name (checked after the walk against the cmdlet names in this file)
    if ($Cmd.InvocationOperator -eq [TokenKind]::Dot -and -not ((Get-Inner $els[0]) -is [ScriptBlockExpressionAst])) { $Ctx.TaintAll = $true }
    if ($leaf -match '^(?i)(set-alias|new-alias|sal|nal)$') { $Ctx.TaintAll = $true }
    if (-not $name -and -not ((Get-Inner $els[0]) -is [ScriptBlockExpressionAst])) { $Ctx.DynamicCall = $true }
    if ($leaf -eq 'get-command' -or $leaf -eq 'gcm') { $Ctx.CmdletName = $true }
    if ($VarObjectCmdRx.IsMatch($leaf)) { $Ctx.VarObject = $true }
    if ($leaf -eq 'get-variable' -or $leaf -eq 'gv') {
        # r7 (V3): -ValueOnly is on only as the bare switch or -ValueOnly:$true, with no splat that could turn it off
        $valueOnly = $false; $splat = $false
        foreach ($el in $els) {
            if ($el -is [VariableExpressionAst] -and $el.Splatted) { $splat = $true }
            if (-not ($el -is [CommandParameterAst])) { continue }
            $p = Resolve-CmdletParameter $ParamTables['Get-Variable'] $el.ParameterName
            if ($null -eq $p -or $p.Name -ne 'ValueOnly') { continue }
            $arg = $el.Argument
            $valueOnly = $null -eq $arg -or ($arg -is [VariableExpressionAst] -and $arg.VariablePath.UserPath -ieq 'true')
        }
        if (-not $valueOnly -or $splat) { $Ctx.VarObject = $true }
    }
    if ($VarCmds.ContainsKey($leaf)) {
        # Set-Variable -Scope Local cmd <value>: the name is -Name, else the first positional once the named parameters have their values
        $b = Get-CmdletBinding $els $ParamTables[$VarCmds[$leaf]]
        $names = $b.Bound['Name']
        if (-not $b.Ok -or $null -eq $names -or $names.Count -eq 0) { $Ctx.TaintAll = $true } else { foreach ($n in $names) { Add-Taint $Ctx $n } }
    }
    elseif ($ItemCmds.ContainsKey($leaf)) {
        $b = Get-CmdletBinding $els $ParamTables[$ItemCmds[$leaf]]
        $paths = [List[object]]::new()
        if ($b.Ok) { foreach ($k in $b.Bound.Keys) { if ($ItemPathParams.Contains($k)) { foreach ($v in $b.Bound[$k]) { $paths.Add($v) } } } }
        else { foreach ($el in @($els | Select-Object -Skip 1 | Where-Object { -not ($_ -is [CommandParameterAst]) })) { $paths.Add($el) } }
        $consts = [List[string]]::new(); $drive = $false
        foreach ($v in $paths) {
            $x = Get-Inner $v
            $vals = if ($x -is [ArrayLiteralAst]) { @($x.Elements) } else { @($v) }
            foreach ($e in $vals) {
                $s = Get-ConstString $e
                if ($null -eq $s) { $Ctx.TaintAll = $true } else { $consts.Add($s); if ($VarDriveRx.IsMatch($s)) { $drive = $true } }
            }
        }
        # Rename-Item variable:a cmd / Copy-Item variable:a cmd: a bare destination or new name beside a variable: path is a variable too
        if ($drive) { foreach ($s in $consts) { if (-not (Add-VarDriveTaint $Ctx $s)) { [void]$Ctx.Tainted.Add(($s -replace '^[\\/]+', '').ToLowerInvariant()) } } }
    }
}

# The right-hand sides that can give $V its value where it is read, or $null when this source alone does not decide it. A read resolves
# only through assignments in its own scope (or a script block inside it, which `.` or ForEach-Object can run in that scope), every one a
# plain `=`, one of them a top-level statement of that scope before the read; `$script:x` is read against the file's own top scope.
# A parameter, an automatic variable, a foreach / multi-assignment / [ref] / -OutVariable binding, or $global: / $using: never resolves.
# r5 (rule V2): a script: write reaches every read; it is the definite write only for a `$script:x` read, and a plain write only for a
# plain read. The definite write always ends before the read, for a `$script:x` read too.
function Get-LiteralRights($V, $Ctx) {
    $path = $V.VariablePath.UserPath
    if ($path.Contains(':') -and $path -notmatch '^(?i)(script|local|private):') { return $null }
    $key = Get-VarKey $V
    if ($Ctx.TaintAll -or $AutoVars.Contains($key) -or $Ctx.Tainted.Contains($key)) { return $null }
    $isScript = $path -match '^(?i)script:'
    $scope = if ($isScript) { $Ctx.Root } else { Get-Scope $V }
    if ($null -eq $scope -or (Get-ScopeParameters $scope).Contains($key)) { return $null }
    $rights = [List[object]]::new(); $definite = $false
    foreach ($r in @($Ctx.Assign[$key])) {
        if ($null -eq $r -or -not ($r.Script -or (Test-Within $r.Scope $scope))) { continue }
        if ($r.Op -ne [TokenKind]::Equals) { return $null }
        if ($r.Script -eq $isScript -and $r.Direct -and [object]::ReferenceEquals($r.Scope, $scope) -and $r.End -le $V.Extent.StartOffset) { $definite = $true }
        $rights.Add($r.Right)
    }
    if (-not $definite) { return $null }
    return , $rights.ToArray()
}

function Get-Statement($Ast) {
    # The outermost statement around $Ast that is not itself inside an expression: an @( ) or $( ) body is looked through, a branch body
    # of if / foreach / try is not, so each branch of a one-line if/else keeps its own text.
    $best = $Ast
    $p = $Ast
    while ($null -ne $p) {
        if ($p -is [StatementAst]) { $best = $p }
        $parent = $p.Parent
        if ($null -eq $parent -or $parent -is [NamedBlockAst] -or $parent -is [ScriptBlockAst]) { break }
        if ($parent -is [StatementBlockAst] -and -not ($parent.Parent -is [ArrayExpressionAst] -or $parent.Parent -is [SubExpressionAst])) { break }
        $p = $parent
    }
    return $best
}

function Get-CleanText($Extent, $Ctx) {
    $s = $Extent.StartOffset; $e = $Extent.EndOffset
    $sb = [System.Text.StringBuilder]::new()
    $pos = $s
    foreach ($k in $Ctx.Skip) {
        if ($k[1] -le $s) { continue }
        if ($k[0] -ge $e) { break }
        $a = [Math]::Max($k[0], $pos)
        if ($a -gt $pos) { [void]$sb.Append($Ctx.Text, $pos, $a - $pos) }
        [void]$sb.Append(' ')
        $pos = [Math]::Max($pos, [Math]::Min($k[1], $e))
    }
    if ($e -gt $pos) { [void]$sb.Append($Ctx.Text, $pos, $e - $pos) }
    return ([regex]::Replace($sb.ToString(), '\s+', ' ')).Trim()
}

function Add-Site($Ctx, [string]$Kind, $Ast, [string]$Text) {
    if (-not $Text) {
        # a command's text is its whole pipeline (`Start-Process ... | Out-Null`); each && / || element and each branch is its own pipeline
        $span = if ($Ast -is [CommandAst] -and $Ast.Parent -is [PipelineAst]) { $Ast.Parent } else { $Ast }
        $Text = Get-CleanText $span.Extent $Ctx
    }
    $Ctx.Out.Add([pscustomobject]@{
            kind = $Kind; line = $Ctx.LineBase + $Ast.Extent.StartLineNumber - 1; ord = $Ctx.OffBase + $Ast.Extent.StartOffset; text = $Text
        })
}

function Test-CtxNear($Ast, $Ctx) {
    for ($l = $Ast.Extent.StartLineNumber - 4; $l -le $Ast.Extent.EndLineNumber + 4; $l++) { if ($Ctx.CtxLines.Contains($l)) { return $true } }
    return $false
}

# $true when every value $Ast can carry is a string literal in this source (rule S then scans it); $false when it is built at run time.
function Test-ResolvedString($Ast, $Ctx, [int]$Depth = 0) {
    $x = Get-Inner $Ast
    if ($x -is [StringConstantExpressionAst] -or $x -is [ScriptBlockExpressionAst]) { return $true }   # a { } literal is AST code already
    if ($x -is [ExpandableStringExpressionAst]) { return ($x.NestedExpressions.Count -eq 0) }
    if ($x -is [VariableExpressionAst] -and $Depth -lt 4) {
        $rights = Get-LiteralRights $x $Ctx
        if ($null -eq $rights) { return $false }
        foreach ($r in $rights) { if (-not (Test-ResolvedString $r $Ctx ($Depth + 1))) { return $false } }
        return $true
    }
    return $false
}

function Test-HiddenValue($V) {
    if ($null -eq $V) { return $false }
    $x = Get-Inner $V
    if ($x -is [ConstantExpressionAst]) { return (([string]$x.Value).Trim() -match '^(?i)(hidden|1)$') }
    return $true
}

# 'hidden' | 'none' | 'unresolved' for a variable $V splatted into Start-Process. A Hidden set anywhere in this source counts, whatever
# its scope (over-reporting is accepted); 'none' needs every assignment that reaches the splat (Get-LiteralRights) to be a hashtable literal.
function Get-SplatWindowStyle($V, $Ctx) {
    $key = Get-VarKey $V
    foreach ($r in @($Ctx.Assign[$key])) {
        if ($null -eq $r) { continue }
        $x = Get-Inner $r.Right
        if ($x -is [HashtableAst]) {
            foreach ($kv in $x.KeyValuePairs) {
                $k = Get-Inner $kv.Item1
                if ($k -is [StringConstantExpressionAst] -and (Resolve-StartProcessParameter $k.Value) -eq 'WindowStyle' -and (Test-HiddenValue $kv.Item2)) { return 'hidden' }
            }
        }
    }
    foreach ($m in @($Ctx.Member[$key])) {
        if ($null -eq $m) { continue }
        if ((Resolve-StartProcessParameter $m.Key) -eq 'WindowStyle' -and (Test-HiddenValue $m.Right)) { return 'hidden' }
    }
    $rights = Get-LiteralRights $V $Ctx
    if ($null -eq $rights) { return 'unresolved' }
    foreach ($r in $rights) { if (-not ((Get-Inner $r) -is [HashtableAst])) { return 'unresolved' } }
    return 'none'
}

function Get-CommandTarget($Cmd, $Ctx) {
    $name = $Cmd.GetCommandName()
    if ($name) {
        $leaf = (($name -split '[\\/]')[-1] -replace '(?i)\.exe$', '').ToLowerInvariant()
        if ($leaf -eq 'pwsh' -or $leaf -eq 'powershell') { return 'pwsh' }
        return $leaf
    }
    if ($Cmd.InvocationOperator -eq [TokenKind]::Ampersand -or $Cmd.InvocationOperator -eq [TokenKind]::Dot) {
        $first = $Cmd.CommandElements[0]
        if ($first.Extent.Text -match '(?i)pwsh|powershell') { return 'pwsh' }
        $inner = Get-Inner $first
        if ($inner -is [VariableExpressionAst]) {
            foreach ($r in @($Ctx.Assign[(Get-VarKey $inner)])) { if ($null -ne $r -and $null -ne $r.Right -and $r.Right.Extent.Text -match '(?i)pwsh|powershell') { return 'pwsh' } }
        }
        return '?'
    }
    return ''
}

# The argument of a -Name / -Name:value parameter at index $I of $Els.
function Get-ParameterValue($Els, [int]$I) {
    if ($null -ne $Els[$I].Argument) { return $Els[$I].Argument }
    if ($I + 1 -lt $Els.Count -and -not ($Els[$I + 1] -is [CommandParameterAst])) { return $Els[$I + 1] }
    return $null
}

function Get-StringBody($S) {
    $ext = $S.Extent
    if ($S -is [StringConstantExpressionAst]) {
        $here = $S.StringConstantType -eq [StringConstantType]::SingleQuotedHereString -or $S.StringConstantType -eq [StringConstantType]::DoubleQuotedHereString
        if ($here) { return @{ Text = $S.Value; Line = $ext.StartLineNumber + 1; Off = $ext.StartOffset + $ext.Text.IndexOf("`n") + 1 } }
        return @{ Text = $S.Value; Line = $ext.StartLineNumber; Off = $ext.StartOffset + 1 }
    }
    $raw = $ext.Text.ToCharArray()
    foreach ($n in $S.NestedExpressions) {
        for ($i = $n.Extent.StartOffset - $ext.StartOffset; $i -lt $n.Extent.EndOffset - $ext.StartOffset; $i++) {
            if ($raw[$i] -ne "`n" -and $raw[$i] -ne "`r") { $raw[$i] = ' ' }
        }
    }
    $t = -join $raw
    if ($S.StringConstantType -eq [StringConstantType]::DoubleQuotedHereString) {
        $nl = $t.IndexOf("`n")
        $body = $t.Substring($nl + 1) -replace '\r?\n[ \t]*"@$', ''
        return @{ Text = $body; Line = $ext.StartLineNumber + 1; Off = $ext.StartOffset + $nl + 1 }
    }
    return @{ Text = $t.Substring(1, [Math]::Max(0, $t.Length - 2)); Line = $ext.StartLineNumber; Off = $ext.StartOffset + 1 }
}

function New-Ctx($Root) {
    return @{
        Assign = @{}; Member = @{}; Root = $Root; Tainted = [HashSet[string]]::new(); TaintAll = $false; VarObject = $false; ValueWrite = $false
        DynamicCall = $false; CmdletName = $false
        # r7 (V3): splatted variables, every param name, and the keyed writes a splat could carry (checked after the walk)
        Splats = [List[object]]::new(); ParamNames = [HashSet[string]]::new(); KeyedValues = [List[object]]::new(); DynamicKey = $false
        # r8 (V3): every variable reference by name, for the splat-source allowlist (built on first use by Test-SplatSourceUses)
        VarUses = $null
        # r9 (V3): every use of $PSDefaultParameterValues, checked after the walk with or without a splat
        Defaults = [List[object]]::new()
    }
}

# r8 (rule V3, Dot 6394696125): $true when every use of the splatted name $Key in the file is on the splat-source allowlist: the splat
# itself; `$h = @{...}` (Read-Writes already requires every assignment to be a hashtable literal, and a non-constant key there sets
# DynamicKey); a call of an allowlisted method whose key argument is a constant (`$h.Add('k', v)`); an index with a constant key
# (`$h['k']`, read or write); a write to a constant member (`$h.k = v`); or a read of .Count / .Keys / .Values. Anything else (a
# non-constant key, `$h` passed to or piped into a command, assigned to another name, cast, reached through another member such as
# .psbase, or qualified) could add a key this source cannot see, so the splat's key set is unknown.
$SplatKeyedMethods = [HashSet[string]]::new([string[]]@('add', 'set_item', 'get_item', 'remove', 'containskey', 'contains'))
$SplatPlainMethods = [HashSet[string]]::new([string[]]@('clear', 'containsvalue', 'clone', 'getenumerator'))
$SplatReadMembers = [HashSet[string]]::new([string[]]@('count', 'keys', 'values'))

function Test-ConstKey($K) {
    $x = Get-Inner $K
    return ($null -ne $x -and ($x -is [ConstantExpressionAst] -or $null -ne (Get-ConstString $x)))
}

function Test-AssignTarget($U) {
    $p = $U
    while ($p.Parent -is [AttributedExpressionAst]) { $p = $p.Parent }
    return ($p.Parent -is [AssignmentStatementAst] -and [object]::ReferenceEquals($p.Parent.Left, $p))
}

# The outermost node that is only ( ) around $V: what a member, index or call on the name is applied to.
function Get-ParenOuter($V) {
    $u = $V
    while ($true) {
        $p = $u.Parent
        if ($p -is [ParenExpressionAst]) { $u = $p; continue }
        if ($p -is [CommandExpressionAst] -and $p.Parent -is [PipelineAst] -and $p.Parent.PipelineElements.Count -eq 1 -and $p.Parent.Parent -is [ParenExpressionAst]) { $u = $p; continue }
        if ($p -is [PipelineAst] -and $p.PipelineElements.Count -eq 1 -and $p.Parent -is [ParenExpressionAst]) { $u = $p; continue }
        break
    }
    return $u
}

# The assignment whose left side is $Left (looked through casts and attributes).
function Get-Assignment($Left) {
    $p = $Left
    while ($p.Parent -is [AttributedExpressionAst]) { $p = $p.Parent }
    return $p.Parent
}

# r9 (rule V3): a $PSDefaultParameterValues entry `<command>:<parameter>` = $Value binds a variable when its parameter part is a binder
# parameter or holds a wildcard (pwsh binds a wildcard that matches one parameter); a key that is not constant could be any binder.
function Add-DefaultKeyTaint($Ctx, $Key, $Value) {
    $k = Get-ConstString $Key
    if ($null -eq $k) { if (-not ((Get-Inner $Key) -is [ConstantExpressionAst])) { $Ctx.TaintAll = $true }; return }
    $param = if ($k.Contains(':')) { $k.Substring($k.IndexOf(':') + 1).Trim() } else { '' }
    if ($param -match '[*?\[]' -or (Test-BinderParameter $param) -or (Test-BinderParameter $k.Trim())) { Add-Taint $Ctx $Value }
}

# r9 round 2 (sol r1 BLOCKER on #678, CoS dot B1): `[<key>] += v` builds a name its right side does not hold; any operator other than `=`
# taints every name, and the value it leaves is not modelled.
function Add-DefaultEntryWrite($Ctx, $Left, $Key) {
    $a = Get-Assignment $Left
    if ($a.Operator -eq [TokenKind]::Equals) { Add-DefaultKeyTaint $Ctx $Key $a.Right } else { $Ctx.TaintAll = $true }
}

$DefaultsReadMethods = [HashSet[string]]::new([string[]]@('remove', 'containskey', 'contains', 'get_item', 'clear', 'containsvalue', 'clone', 'getenumerator'))

# r9 (rule V3, CoS/Dot 6397324853): every use of $PSDefaultParameterValues must be on the allowlist in the header; its binder entries taint
# their value names, and any use off the allowlist taints every name. Runs with or without a splat in the file.
function Add-DefaultParameterTaints($Ctx) {
    foreach ($v in $Ctx.Defaults) {
        if ($Ctx.TaintAll) { return }
        if ($v.Splatted) { $Ctx.TaintAll = $true; continue }
        if (Test-AssignTarget $v) {
            $a = $v.Parent
            while ($a -is [AttributedExpressionAst]) { $a = $a.Parent }
            $h = Get-Inner $a.Right
            # r9 round 2 (Fable r1 item 2): `$x = ($PSDefaultParameterValues = @{})` hands the table to $x; only a statement of its own block is read
            $own = $a.Parent -is [NamedBlockAst] -or $a.Parent -is [StatementBlockAst]
            if ($own -and ($a.Operator -eq [TokenKind]::Equals -or $a.Operator -eq [TokenKind]::PlusEquals) -and $h -is [HashtableAst]) {
                foreach ($kv in $h.KeyValuePairs) { Add-DefaultKeyTaint $Ctx $kv.Item1 $kv.Item2 }
            }
            else { $Ctx.TaintAll = $true }
            continue
        }
        $u = Get-ParenOuter $v
        $p = $u.Parent
        if ($p -is [InvokeMemberExpressionAst] -and [object]::ReferenceEquals($p.Expression, $u) -and -not $p.Static -and $p.Member -is [StringConstantExpressionAst]) {
            $mn = $p.Member.Value.ToLowerInvariant()
            if ($DefaultsReadMethods.Contains($mn)) { continue }
            if (($mn -eq 'add' -or $mn -eq 'set_item') -and $null -ne $p.Arguments -and $p.Arguments.Count -eq 2) { Add-DefaultKeyTaint $Ctx $p.Arguments[0] $p.Arguments[1]; continue }
            $Ctx.TaintAll = $true; continue
        }
        if ($p -is [MemberExpressionAst] -and -not ($p -is [InvokeMemberExpressionAst]) -and [object]::ReferenceEquals($p.Expression, $u) -and -not $p.Static -and
            $p.Member -is [StringConstantExpressionAst]) {
            if (Test-AssignTarget $p) { Add-DefaultEntryWrite $Ctx $p $p.Member }
            elseif (-not $SplatReadMembers.Contains($p.Member.Value.ToLowerInvariant())) { $Ctx.TaintAll = $true }
            continue
        }
        if ($p -is [IndexExpressionAst] -and [object]::ReferenceEquals($p.Target, $u)) {
            if (Test-AssignTarget $p) { Add-DefaultEntryWrite $Ctx $p $p.Index }
            continue
        }
        $Ctx.TaintAll = $true
    }
}

function Test-SplatSourceUses([string]$Key, $Ctx) {
    if ($null -eq $Ctx.VarUses) {
        # built only for a file with a splat: indexing every variable node of every scanned file costs the census seconds
        $Ctx.VarUses = @{}
        foreach ($n in $Ctx.Root.FindAll({ $args[0] -is [VariableExpressionAst] }, $true)) {
            $nk = Get-VarKey $n
            if ($null -eq $Ctx.VarUses[$nk]) { $Ctx.VarUses[$nk] = [List[object]]::new() }
            $Ctx.VarUses[$nk].Add($n)
        }
    }
    foreach ($v in @($Ctx.VarUses[$Key])) {
        if ($null -eq $v) { continue }
        if ($v.Splatted) { continue }
        if ($v.VariablePath.UserPath.Contains(':')) { return $false }
        if (Test-AssignTarget $v) {
            $a = $v.Parent
            while ($a -is [AttributedExpressionAst]) { $a = $a.Parent }
            if ($a.Operator -eq [TokenKind]::Equals -and (Get-Inner $a.Right) -is [HashtableAst]) { continue }
            return $false
        }
        # look through ( ) around the name
        $u = Get-ParenOuter $v
        $p = $u.Parent
        if ($p -is [InvokeMemberExpressionAst] -and [object]::ReferenceEquals($p.Expression, $u) -and -not $p.Static -and $p.Member -is [StringConstantExpressionAst]) {
            $mn = $p.Member.Value.ToLowerInvariant()
            if ($SplatPlainMethods.Contains($mn)) { continue }
            if ($SplatKeyedMethods.Contains($mn) -and $null -ne $p.Arguments -and $p.Arguments.Count -ge 1 -and (Test-ConstKey $p.Arguments[0])) { continue }
            return $false
        }
        if ($p -is [MemberExpressionAst] -and -not ($p -is [InvokeMemberExpressionAst]) -and [object]::ReferenceEquals($p.Expression, $u) -and -not $p.Static -and
            $p.Member -is [StringConstantExpressionAst]) {
            if ((Test-AssignTarget $p) -or $SplatReadMembers.Contains($p.Member.Value.ToLowerInvariant())) { continue }
            return $false
        }
        if ($p -is [IndexExpressionAst] -and [object]::ReferenceEquals($p.Target, $u) -and (Test-ConstKey $p.Index)) { continue }
        return $false
    }
    return $true
}

# Every write to a variable in $Root's AST, recorded in $Ctx (rule V / V2). $Lists, when given, also collects the nodes the site rules read.
function Read-Writes($Ctx, $Root, $Lists, [int]$Depth) {
    $executed = [List[object]]::new()
    foreach ($a in $Root.FindAll({ $true }, $true)) {
        if ($a -is [MemberExpressionAst] -and $a.Member -is [StringConstantExpressionAst]) {
            # r5 (rule V2): the session-state APIs that write any variable by name, and a PSVariable's .Value (checked after the walk)
            $mn = $a.Member.Value.ToLowerInvariant()
            if ($StateMembers.Contains($mn)) { $Ctx.TaintAll = $true }
            elseif ($mn -eq 'sessionstate') {
                $up = $a.Parent
                if (-not ($up -is [MemberExpressionAst] -and [object]::ReferenceEquals($up.Expression, $a) -and $up.Member -is [StringConstantExpressionAst] -and $up.Member.Value -ieq 'path')) { $Ctx.TaintAll = $true }
            }
            elseif ($mn -eq 'set_value' -and $a -is [InvokeMemberExpressionAst]) { $Ctx.ValueWrite = $true }
            elseif ($mn -eq 'value' -and $a.Parent -is [AssignmentStatementAst] -and [object]::ReferenceEquals($a.Parent.Left, $a)) { $Ctx.ValueWrite = $true }
            if ($a -is [InvokeMemberExpressionAst] -and $mn -in @('create', 'invokescript', 'newscriptblock')) {
                $isSb = $mn -ne 'create' -or ($a.Static -and $a.Expression -is [TypeExpressionAst] -and $SbTypeRx.IsMatch($a.Expression.TypeName.FullName))
                if ($isSb) { $executed.Add($(if ($null -ne $a.Arguments -and $a.Arguments.Count -gt 0) { $a.Arguments[0] } else { $null })) }
            }
            # r7 (V3): $h.Add('OutVariable', ...) can put a binder into a hashtable a splat then carries
            if ($a -is [InvokeMemberExpressionAst] -and $null -ne $a.Arguments) {
                foreach ($g in $a.Arguments) { $gs = Get-ConstString $g; if ($null -ne $gs -and (Test-BinderParameter $gs)) { $Ctx.DynamicKey = $true } }
            }
        }
        # r7 (V3): a member name that is not a constant can name anything (.Value, .set_Value, .PSVariable, ...)
        elseif ($a -is [MemberExpressionAst]) { $Ctx.TaintAll = $true }
        if ($a -is [HashtableAst]) {
            foreach ($kv in $a.KeyValuePairs) {
                $key = Get-Inner $kv.Item1
                if ($key -is [StringConstantExpressionAst]) { if (Test-BinderParameter $key.Value) { $Ctx.KeyedValues.Add($kv.Item2) } }
                elseif (-not ($key -is [ConstantExpressionAst])) { $Ctx.DynamicKey = $true }
            }
        }
        # r9 (V3): $PSDefaultParameterValues (any qualifier) is read after the walk; a constant string naming it binds it by name
        if ($a -is [VariableExpressionAst] -and $a.VariablePath.UserPath -like '*psdefaultparametervalues' -and (Get-VarKey $a) -eq 'psdefaultparametervalues') { $Ctx.Defaults.Add($a) }
        if ($a -is [StringConstantExpressionAst] -and $a.Value -like '*psdefaultparametervalues*') { $Ctx.TaintAll = $true }
        if ($a -is [StringConstantExpressionAst]) {
            [void](Add-VarDriveTaint $Ctx $a.Value)
            $sv = $a.Value.ToLowerInvariant()
            if ($a.StringConstantType -ne [StringConstantType]::BareWord -and ($VarCmds.ContainsKey($sv) -or $ItemCmds.ContainsKey($sv))) { $Ctx.CmdletName = $true }
        }
        if ($a -is [CommandAst]) {
            if ($null -ne $Lists) { $Lists.Commands.Add($a) }
            Add-CommandBindings $Ctx $a
            $n = $a.GetCommandName()
            if ($n -and (($n -split '\\')[-1] -match '^(?i)(invoke-expression|iex)$')) { $executed.Add((Get-IexArgument $a)) }
        }
        elseif ($a -is [InvokeMemberExpressionAst]) { if ($null -ne $Lists) { $Lists.Members.Add($a) } }
        elseif ($a -is [TypeConstraintAst] -or $a -is [TypeExpressionAst]) { if ($null -ne $Lists) { $Lists.Types.Add($a) } }
        elseif (($a -is [StringConstantExpressionAst] -and $a.StringConstantType -ne [StringConstantType]::BareWord) -or $a -is [ExpandableStringExpressionAst]) { if ($null -ne $Lists) { $Lists.Strings.Add($a) } }
        elseif ($a -is [ForEachStatementAst]) { Add-Write $Ctx $a.Variable $null $null (Get-Scope $a) $false 0 }
        elseif ($a -is [ConvertExpressionAst] -and $a.Type.TypeName.Name -ieq 'ref' -and (Get-Inner $a.Child) -is [VariableExpressionAst]) { [void]$Ctx.Tainted.Add((Get-VarKey (Get-Inner $a.Child))) }
        elseif ($a -is [ParameterAst]) {
            # r7 (V3): a param binds in the caller's scope when its { } literal is dot-sourced
            $pk = Get-VarKey $a.Name
            [void]$Ctx.ParamNames.Add($pk)
            if (-not (Test-OwnScopeParameter $a)) { [void]$Ctx.Tainted.Add($pk) }
        }
        elseif ($a -is [UnaryExpressionAst] -and $a.TokenKind -in @([TokenKind]::PlusPlus, [TokenKind]::MinusMinus, [TokenKind]::PostfixPlusPlus, [TokenKind]::PostfixMinusMinus)) {
            $v = Get-Inner $a.Child
            if ($v -is [VariableExpressionAst]) { Add-Write $Ctx $v $a.TokenKind $null (Get-Scope $a) $false 0 }
        }
        elseif ($a -is [AssignmentStatementAst]) {
            # r7 (V3): casts and attributes on the left are peeled whatever their number; a left side of any other shape taints every name
            $left = $a.Left
            while ($left -is [AttributedExpressionAst]) { $left = $left.Child }
            if ($left -is [VariableExpressionAst]) {
                Add-Write $Ctx $left $a.Operator $a.Right (Get-Scope $a) ($a.Parent -is [NamedBlockAst]) $a.Extent.EndOffset
            }
            elseif ($left -is [ArrayLiteralAst]) {
                # `$a, $b = ...` splits a value at run time: each name is bound, none to a literal
                foreach ($el in $left.Elements) {
                    $v = Get-Inner $el
                    while ($v -is [AttributedExpressionAst]) { $v = $v.Child }
                    if ($v -is [VariableExpressionAst]) { Add-Write $Ctx $v $null $null (Get-Scope $a) $false 0 }
                }
            }
            elseif ($left -is [MemberExpressionAst] -or $left -is [IndexExpressionAst]) {
                $target = if ($left -is [MemberExpressionAst]) { Get-Inner $left.Expression } else { Get-Inner $left.Target }
                $member = if ($left -is [MemberExpressionAst]) { $left.Member } else { Get-Inner $left.Index }
                if ($member -is [StringConstantExpressionAst]) { if (Test-BinderParameter $member.Value) { $Ctx.KeyedValues.Add($a.Right) } }
                elseif (-not ($member -is [ConstantExpressionAst])) { $Ctx.DynamicKey = $true }
                if ($target -is [VariableExpressionAst] -and $member -is [StringConstantExpressionAst]) {
                    $k = Get-VarKey $target
                    if ($null -eq $Ctx.Member[$k]) { $Ctx.Member[$k] = [List[object]]::new() }
                    $Ctx.Member[$k].Add(@{ Key = $member.Value; Right = $a.Right })
                }
            }
            else { $Ctx.TaintAll = $true }
        }
    }

    # (Get-Variable cmd).Value = ... / $v = Get-Item variable:cmd; $v.Value = ...: a PSVariable object's .Value is the variable itself
    if ($Ctx.ValueWrite -and $Ctx.VarObject) { $Ctx.TaintAll = $true }
    # $c = 'Set-Variable'; & $c cmd ... / & (Get-Command sv) cmd ...: a dynamic call in a file that names a variable or item cmdlet
    if ($Ctx.DynamicCall -and $Ctx.CmdletName) { $Ctx.TaintAll = $true }
    # r9 (V3): a $PSDefaultParameterValues binder entry binds with no splat in the file, so it is read here, before the splat check
    Add-DefaultParameterTaints $Ctx
    # iex and a script block made from text run that text in this scope (or a dot-sourced one): its writes are this file's writes. Repeat
    # until the taints settle, since a new taint can stop a string from resolving.
    for ($round = 0; $round -lt 4 -and $executed.Count -gt 0; $round++) {
        $before = "$($Ctx.TaintAll) $($Ctx.Tainted.Count)"
        foreach ($x in $executed) { Add-ExecutedWrites $Ctx $x 0 $Depth }
        if ("$($Ctx.TaintAll) $($Ctx.Tainted.Count)" -eq $before) { break }
    }
    # r7 (V3): a splat binds its keys like named parameters. Only a name whose every value in this file is a hashtable literal is read;
    # then every binder key in the file (a hashtable can be aliased or filled by a function) taints what it names, and a key that is not
    # constant taints every name. r8: the literal must reach the splat in its own scope and every use of the name must be allowlisted.
    if ($Ctx.Splats.Count -gt 0) {
        $usesOk = @{}
        foreach ($s in $Ctx.Splats) {
            if ($Ctx.TaintAll) { break }
            $sk = Get-VarKey $s
            $rs = @($Ctx.Assign[$sk] | Where-Object { $null -ne $_ })
            $other = @($rs | Where-Object { $null -eq $_.Right -or -not ((Get-Inner $_.Right) -is [HashtableAst]) }).Count -gt 0
            if ($rs.Count -eq 0 -or $other -or $AutoVars.Contains($sk) -or $Ctx.ParamNames.Contains($sk) -or $Ctx.Tainted.Contains($sk) -or
                $s.VariablePath.UserPath.Contains(':')) { $Ctx.TaintAll = $true }
            # r8 (V3): the splat's key set is known only when a literal reaches it in its own scope and every use of the name is allowlisted
            elseif ($null -eq (Get-LiteralRights $s $Ctx)) { $Ctx.TaintAll = $true }
            else {
                if ($null -eq $usesOk[$sk]) { $usesOk[$sk] = Test-SplatSourceUses $sk $Ctx }
                if (-not $usesOk[$sk]) { $Ctx.TaintAll = $true }
            }
        }
        if ($Ctx.DynamicKey) { $Ctx.TaintAll = $true }
        foreach ($v in $Ctx.KeyedValues) { Add-Taint $Ctx $v }
    }
}

# The argument of an iex / Invoke-Expression command ($null when it takes pipeline input).
function Get-IexArgument($Cmd) {
    $els = $Cmd.CommandElements
    for ($i = 1; $i -lt $els.Count; $i++) {
        if ($els[$i] -is [CommandParameterAst]) {
            if ('command'.StartsWith($els[$i].ParameterName.ToLowerInvariant(), [StringComparison]::Ordinal)) { return Get-ParameterValue $els $i }
        }
        else { return $els[$i] }
    }
    return $null
}

# r5 (rule V2): the names an executed string writes. A string literal (or a variable resolving only to literals) is parsed and its writes
# taint those names here; anything else (built at run time, or pipeline input) taints every name. A { } literal is AST code already.
function Add-ExecutedWrites($Ctx, $Arg, [int]$Hops, [int]$Depth) {
    $x = Get-Inner $Arg
    if ($x -is [ScriptBlockExpressionAst]) { return }
    $s = if ($null -ne $x) { Get-ConstString $x } else { $null }
    if ($null -ne $s) {
        if ($Depth -ge $MaxDepth) { $Ctx.TaintAll = $true; return }
        $t = $null; $e = $null
        $sub = [Parser]::ParseInput($s, [ref]$t, [ref]$e)
        $c = New-Ctx $sub
        Read-Writes $c $sub $null ($Depth + 1)
        # r7 (V3): the executed text runs in this scope, so its own top-level params bind here too
        foreach ($k in (Get-ScopeParameters $sub)) { [void]$Ctx.Tainted.Add($k) }
        if ($c.TaintAll) { $Ctx.TaintAll = $true }
        foreach ($k in $c.Tainted) { [void]$Ctx.Tainted.Add($k) }
        foreach ($k in $c.Assign.Keys) { [void]$Ctx.Tainted.Add($k) }
        return
    }
    if ($x -is [VariableExpressionAst] -and $Hops -lt 4) {
        $rights = Get-LiteralRights $x $Ctx
        if ($null -eq $rights) { $Ctx.TaintAll = $true; return }
        foreach ($r in $rights) { Add-ExecutedWrites $Ctx $r ($Hops + 1) $Depth }
        return
    }
    $Ctx.TaintAll = $true
}

function Get-Sites([string]$Text, [int]$LineBase, [int]$OffBase, [int]$Depth, [bool]$TopLevel, $Out) {
    $tokens = $null; $errors = $null
    $root = [Parser]::ParseInput($Text, [ref]$tokens, [ref]$errors)
    $ctx = New-Ctx $root
    $ctx.Text = $Text; $ctx.LineBase = $LineBase; $ctx.OffBase = $OffBase; $ctx.Out = $Out
    $ctx.Skip = [List[object]]::new(); $ctx.CtxLines = [HashSet[int]]::new()
    if ($TopLevel -and $errors.Count -gt 0) {
        $e0 = $errors[0]
        $Out.Add([pscustomobject]@{ kind = 'unresolved'; line = $LineBase + $e0.Extent.StartLineNumber - 1; ord = $OffBase + $e0.Extent.StartOffset; text = "parse error: $($e0.Message)" })
    }
    foreach ($t in $tokens) {
        if ($t.Kind -eq [TokenKind]::Comment -or $t.Kind -eq [TokenKind]::LineContinuation) { $ctx.Skip.Add(@($t.Extent.StartOffset, $t.Extent.EndOffset)) }
        elseif ($CtxRx.IsMatch($t.Text)) { for ($l = $t.Extent.StartLineNumber; $l -le $t.Extent.EndLineNumber; $l++) { [void]$ctx.CtxLines.Add($l) } }
    }

    $commands = [List[object]]::new(); $members = [List[object]]::new(); $types = [List[object]]::new(); $strings = [List[object]]::new()
    Read-Writes $ctx $root @{ Commands = $commands; Members = $members; Types = $types; Strings = $strings } $Depth

    $consumed = [HashSet[object]]::new()
    foreach ($cmd in $commands) {
        $els = $cmd.CommandElements
        $target = Get-CommandTarget $cmd $ctx
        if ($target -eq 'pwsh' -or $target -eq '?') {
            $words = @(for ($i = 1; $i -lt $els.Count; $i++) { $w = Get-SwitchWord $els[$i]; if ($w) { $w.ToLowerInvariant() } })
            $pwshLike = $target -eq 'pwsh' -or @($words | Where-Object { $_ -in $PwshOnly }).Count -gt 0
            if (@($words | Where-Object { Test-EncodedWord $_ $pwshLike }).Count -gt 0) {
                Add-Site $ctx 'encoded' $cmd
                foreach ($el in $els) { if ($el -is [StringConstantExpressionAst]) { [void]$consumed.Add($el) } }
            }
            if ($pwshLike) {
                for ($i = 1; $i -lt $els.Count; $i++) {
                    $w = Get-SwitchWord $els[$i]
                    if (-not $w -or -not 'command'.StartsWith($w.ToLowerInvariant(), [StringComparison]::Ordinal)) { continue }
                    $v = if ($els[$i] -is [CommandParameterAst]) { Get-ParameterValue $els $i } elseif ($i + 1 -lt $els.Count) { $els[$i + 1] } else { $null }
                    if ($null -ne $v -and -not ((Get-Inner $v) -is [ScriptBlockExpressionAst]) -and -not (Test-ResolvedString $v $ctx)) {
                        Add-Site $ctx 'unresolved' $cmd
                    }
                }
            }
        }
        elseif ($target -eq 'invoke-expression' -or $target -eq 'iex') {
            $arg = $null
            for ($i = 1; $i -lt $els.Count -and $null -eq $arg; $i++) {
                if ($els[$i] -is [CommandParameterAst]) {
                    if ('command'.StartsWith($els[$i].ParameterName.ToLowerInvariant(), [StringComparison]::Ordinal)) { $arg = Get-ParameterValue $els $i }
                }
                else { $arg = $els[$i] }
            }
            # with no argument it runs only what a pipeline feeds it; a bare `iex` word (in a list of names, say) runs nothing
            $fed = $cmd.Parent -is [PipelineAst] -and $cmd.Parent.PipelineElements.IndexOf($cmd) -gt 0
            if (($null -eq $arg -and $fed) -or ($null -ne $arg -and -not (Test-ResolvedString $arg $ctx))) { Add-Site $ctx 'unresolved' $cmd }
        }
        elseif ($target -eq 'start-process' -or $target -eq 'saps' -or $target -eq 'start') {
            $hit = $false; $unresolved = $false
            for ($i = 1; $i -lt $els.Count; $i++) {
                $el = $els[$i]
                if ($el -is [CommandParameterAst] -and (Resolve-StartProcessParameter $el.ParameterName) -eq 'WindowStyle' -and (Test-HiddenValue (Get-ParameterValue $els $i))) { $hit = $true }
                elseif ($el -is [VariableExpressionAst] -and $el.Splatted) {
                    $style = Get-SplatWindowStyle $el $ctx
                    if ($style -eq 'hidden') { $hit = $true } elseif ($style -eq 'unresolved') { $unresolved = $true }
                }
            }
            if ($hit) { Add-Site $ctx 'detached' $cmd } elseif ($unresolved) { Add-Site $ctx 'unresolved' $cmd }
        }
        elseif (($target -eq 'invoke-cimmethod' -or $target -eq 'invoke-wmimethod') -and (Get-CleanText $cmd.Extent $ctx) -match '(?i)\bCreate\b') {
            Add-Site $ctx 'detached' $cmd
        }
    }

    $createTargets = [List[object]]::new()
    foreach ($m in $members) {
        $name = if ($m.Member -is [StringConstantExpressionAst]) { $m.Member.Value.ToLowerInvariant() } else { '' }
        $arg0 = if ($null -ne $m.Arguments -and $m.Arguments.Count -gt 0) { $m.Arguments[0] } else { $null }
        if ($name -eq 'create') {
            if ($m.Static -and $m.Expression -is [TypeExpressionAst] -and $SbTypeRx.IsMatch($m.Expression.TypeName.FullName)) {
                if ($null -eq $arg0 -or -not (Test-ResolvedString $arg0 $ctx)) { $st = Get-Statement $m; Add-Site $ctx 'unresolved' $st }
                continue
            }
            $w32 = $m.Expression.Extent.Text -match '(?i)win32_process|wmiclass'
            $inner = Get-Inner $m.Expression
            if (-not $w32 -and $inner -is [VariableExpressionAst]) {
                foreach ($r in @($ctx.Assign[(Get-VarKey $inner)])) {
                    # a [wmiclass] assignment is a site of its own already
                    if ($null -ne $r -and $null -ne $r.Right -and $r.Right.Extent.Text -match '(?i)win32_process' -and $r.Right.Extent.Text -notmatch '(?i)wmiclass') { $w32 = $true }
                }
            }
            if ($w32) { $createTargets.Add($m.Expression.Extent); $st = Get-Statement $m; Add-Site $ctx 'detached' $st }
        }
        elseif ($name -eq 'invokescript' -or $name -eq 'newscriptblock' -or $name -eq 'addscript') {
            if ($null -eq $arg0 -or -not (Test-ResolvedString $arg0 $ctx)) { $st = Get-Statement $m; Add-Site $ctx 'unresolved' $st }
        }
    }
    foreach ($t in $types) {
        if (-not $WmiTypeRx.IsMatch($t.TypeName.FullName)) { continue }
        $inside = $false
        foreach ($x in $createTargets) { if ($t.Extent.StartOffset -ge $x.StartOffset -and $t.Extent.EndOffset -le $x.EndOffset) { $inside = $true } }
        if (-not $inside) { $st = Get-Statement $t; Add-Site $ctx 'detached' $st }
    }

    foreach ($s in $strings) {
        if ($consumed.Contains($s)) { continue }
        $body = Get-StringBody $s
        if (-not $LaunchWordRx.IsMatch($body.Text)) { continue }
        $before = $Out.Count
        if ($Depth -ge $MaxDepth) { Add-Site $ctx 'unresolved' $s ("string nested deeper than $MaxDepth levels: " + (Get-CleanText $s.Extent $ctx)); continue }
        Get-Sites $body.Text ($LineBase + $body.Line - 1) ($OffBase + $body.Off) ($Depth + 1) $false $Out
        if ($Out.Count -gt $before) { continue }
        $value = $body.Text.Trim()
        if ($value -match '^[-/]([A-Za-z]+)$') {
            $w = $Matches[1].ToLowerInvariant()
            if ((Test-EncodedWord $w $true) -and ($w -eq 'ec' -or $w.Length -ge 4 -or (Test-CtxNear $s $ctx))) { $st = Get-Statement $s; Add-Site $ctx 'encoded' $st }
        }
        elseif (Test-CtxNear $s $ctx) {
            foreach ($mm in $SwitchWordRx.Matches($body.Text)) {
                if (Test-EncodedWord $mm.Groups['w'].Value $true) { $st = Get-Statement $s; Add-Site $ctx 'encoded' $st; break }
            }
        }
    }
}

$sources = [Console]::In.ReadToEnd() | ConvertFrom-Json -AsHashtable
$result = [ordered]@{}
foreach ($name in $sources.Keys) {
    $out = [List[object]]::new()
    Get-Sites ([string]$sources[$name]) 1 0 0 $true $out
    $result[$name] = @($out | Sort-Object -Property ord -Stable | ForEach-Object { [ordered]@{ kind = $_.kind; line = $_.line; text = $_.text } })
}
[Console]::Out.Write((ConvertTo-Json -InputObject $result -Depth 6 -Compress -EscapeHandling EscapeNonAscii))
