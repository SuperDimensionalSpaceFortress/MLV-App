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
                nor a variable whose every assignment in the same source is one (it is built at run time). A source pwsh cannot parse
                is one `unresolved` site.
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

function Get-VarKey($V) { return ($V.VariablePath.UserPath -replace '^(?i)(script|global|local|private|using):', '').ToLowerInvariant() }

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
        $list = $Ctx.Assign[(Get-VarKey $x)]
        if ($null -eq $list) { return $false }
        foreach ($r in $list) {
            if ($r.Op -ne [TokenKind]::Equals -or -not (Test-ResolvedString $r.Right $Ctx ($Depth + 1))) { return $false }
        }
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

# 'hidden' | 'none' | 'unresolved' for a variable splatted into Start-Process.
function Get-SplatWindowStyle([string]$Key, $Ctx) {
    $literal = $false; $other = $false
    foreach ($r in @($Ctx.Assign[$Key])) {
        if ($null -eq $r) { continue }
        $x = Get-Inner $r.Right
        if ($x -is [HashtableAst] -and $r.Op -eq [TokenKind]::Equals) {
            $literal = $true
            foreach ($kv in $x.KeyValuePairs) {
                $k = Get-Inner $kv.Item1
                if ($k -is [StringConstantExpressionAst] -and (Resolve-StartProcessParameter $k.Value) -eq 'WindowStyle' -and (Test-HiddenValue $kv.Item2)) { return 'hidden' }
            }
        }
        else { $other = $true }
    }
    foreach ($m in @($Ctx.Member[$Key])) {
        if ($null -eq $m) { continue }
        if ((Resolve-StartProcessParameter $m.Key) -eq 'WindowStyle' -and (Test-HiddenValue $m.Right)) { return 'hidden' }
    }
    if (-not $literal -or $other) { return 'unresolved' }
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
            foreach ($r in @($Ctx.Assign[(Get-VarKey $inner)])) { if ($null -ne $r -and $r.Right.Extent.Text -match '(?i)pwsh|powershell') { return 'pwsh' } }
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

function Get-Sites([string]$Text, [int]$LineBase, [int]$OffBase, [int]$Depth, [bool]$TopLevel, $Out) {
    $tokens = $null; $errors = $null
    $root = [Parser]::ParseInput($Text, [ref]$tokens, [ref]$errors)
    $ctx = @{
        Text = $Text; LineBase = $LineBase; OffBase = $OffBase; Out = $Out
        Skip = [List[object]]::new(); CtxLines = [HashSet[int]]::new(); Assign = @{}; Member = @{}
    }
    if ($TopLevel -and $errors.Count -gt 0) {
        $e0 = $errors[0]
        $Out.Add([pscustomobject]@{ kind = 'unresolved'; line = $LineBase + $e0.Extent.StartLineNumber - 1; ord = $OffBase + $e0.Extent.StartOffset; text = "parse error: $($e0.Message)" })
    }
    foreach ($t in $tokens) {
        if ($t.Kind -eq [TokenKind]::Comment -or $t.Kind -eq [TokenKind]::LineContinuation) { $ctx.Skip.Add(@($t.Extent.StartOffset, $t.Extent.EndOffset)) }
        elseif ($CtxRx.IsMatch($t.Text)) { for ($l = $t.Extent.StartLineNumber; $l -le $t.Extent.EndLineNumber; $l++) { [void]$ctx.CtxLines.Add($l) } }
    }

    $all = $root.FindAll({ $true }, $true)
    $commands = [List[object]]::new(); $members = [List[object]]::new(); $types = [List[object]]::new(); $strings = [List[object]]::new()
    foreach ($a in $all) {
        if ($a -is [CommandAst]) { $commands.Add($a) }
        elseif ($a -is [InvokeMemberExpressionAst]) { $members.Add($a) }
        elseif ($a -is [TypeConstraintAst] -or $a -is [TypeExpressionAst]) { $types.Add($a) }
        elseif (($a -is [StringConstantExpressionAst] -and $a.StringConstantType -ne [StringConstantType]::BareWord) -or $a -is [ExpandableStringExpressionAst]) { $strings.Add($a) }
        elseif ($a -is [AssignmentStatementAst]) {
            $left = $a.Left
            if ($left -is [ConvertExpressionAst]) { $left = $left.Child }
            if ($left -is [VariableExpressionAst]) {
                $k = Get-VarKey $left
                if ($null -eq $ctx.Assign[$k]) { $ctx.Assign[$k] = [List[object]]::new() }
                $ctx.Assign[$k].Add(@{ Op = $a.Operator; Right = $a.Right })
            }
            elseif ($left -is [MemberExpressionAst] -or $left -is [IndexExpressionAst]) {
                $target = if ($left -is [MemberExpressionAst]) { Get-Inner $left.Expression } else { Get-Inner $left.Target }
                $member = if ($left -is [MemberExpressionAst]) { $left.Member } else { Get-Inner $left.Index }
                if ($target -is [VariableExpressionAst] -and $member -is [StringConstantExpressionAst]) {
                    $k = Get-VarKey $target
                    if ($null -eq $ctx.Member[$k]) { $ctx.Member[$k] = [List[object]]::new() }
                    $ctx.Member[$k].Add(@{ Key = $member.Value; Right = $a.Right })
                }
            }
        }
    }

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
                    $style = Get-SplatWindowStyle (Get-VarKey $el) $ctx
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
                    if ($null -ne $r -and $r.Right.Extent.Text -match '(?i)win32_process' -and $r.Right.Extent.Text -notmatch '(?i)wmiclass') { $w32 = $true }
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
