<#
.SYNOPSIS
  Fill adversary-pass-prompt.md for one exact head and write the result as UTF-8 (no BOM).

.DESCRIPTION
  ASCII-only on purpose: Windows PowerShell 5.1 reads a BOM-less script as ANSI, so every non-ASCII character
  below is built from its code point.

  Replaces every @@NAME@@ token in ONE pass, so text inside a value (a worktree path or a commit subject that
  looks like a token) is never expanded again. Whole <!-- ... --> comment blocks are dropped first.

  Plain tokens (@@WORKDIR@@, @@HEAD@@, @@BASE@@, @@DIFF@@, @@INTENT@@, @@CARD@@, @@ROUND@@) are prose. A token
  that lands inside a command the reviewer is told to run has a _PS twin that the renderer fills with a
  PowerShell single-quoted literal (@@WORKDIR_PS@@, @@RANGE_PS@@ = base...head): every quote character
  PowerShell accepts (' and the typographic U+2018 U+2019 U+201A U+201B) is doubled, so $name, $(...), a
  backtick or a semicolon in the value reaches git unchanged. A token inside the JSON example has a _JSON twin
  (@@BASE_JSON@@, a complete JSON string). Quoting lives here, never in the template text.
  An unknown token, a path or base that is blank or holds a control or line-separator character or a double quote,
  a path ending in a backslash, a head that is not exactly 40 lowercase hex, a base that starts with '-', or a
  path holding two backticks in a row (it would end the markdown code span the command sits in) is refused and
  nothing is written.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$TemplatePath,
    [Parameter(Mandatory)][string]$OutPath,
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$Head,
    [Parameter(Mandatory)][string]$Base,
    [string]$Diff = '',
    [string]$Intent = '',
    [string]$Card = '',
    [string]$Round = ''
)
$ErrorActionPreference = 'Stop'

function ConvertTo-PsSingleQuoted([string]$Value) {
    foreach ($code in 0x27, 0x2018, 0x2019, 0x201A, 0x201B) {
        $q = [string][char]$code
        $Value = $Value.Replace($q, $q + $q)
    }
    return "'" + $Value + "'"
}

# \z, not $: $ also matches before a final line feed. \p{Cc} is every control (C0, DEL and C1), \p{Zl}\p{Zp} the
# line and paragraph separators. A double quote, and a trailing backslash on a value with a space, are mangled when
# Windows PowerShell 5.1 hands the argument to a native git.exe, so both are refused rather than passed on altered.
$unsafe = '[\p{Cc}\p{Zl}\p{Zp}]'
if ([string]::IsNullOrWhiteSpace($WorkDir) -or $WorkDir -match $unsafe -or $WorkDir.Contains('"') -or $WorkDir.EndsWith('\')) { throw "WORKDIR_INVALID blank, control or separator character, double quote, or trailing backslash in -WorkDir" }
if ($WorkDir.Contains('``')) { throw "WORKDIR_INVALID two backticks in a row in -WorkDir" }
if ($Head -cnotmatch '\A[0-9a-f]{40}\z') { throw "HEAD_INVALID -Head must be exactly 40 lowercase hex" }
if ([string]::IsNullOrWhiteSpace($Base) -or $Base -match $unsafe -or $Base.Contains('"') -or $Base.StartsWith('-') -or $Base.Contains('``')) { throw "BASE_INVALID blank, control or separator character, double quote, leading '-' or two backticks in -Base" }

$tpl = Get-Content -LiteralPath $TemplatePath -Raw
$tpl = [regex]::Replace($tpl, '(?s)<!--.*?-->[ \t]*\r?\n?', '')
$map = @{
    HEAD = $Head; BASE = $Base; CARD = $Card; ROUND = $Round; WORKDIR = $WorkDir; DIFF = $Diff; INTENT = $Intent
    WORKDIR_PS = (ConvertTo-PsSingleQuoted $WorkDir); RANGE_PS = (ConvertTo-PsSingleQuoted "$Base...$Head")
    BASE_JSON = (ConvertTo-Json -InputObject $Base -Compress)
}
$out = [regex]::Replace($tpl, '@@([A-Z][A-Z_]*)@@', {
        param($m)
        $k = $m.Groups[1].Value
        if (-not $map.ContainsKey($k)) { throw "TOKEN_UNKNOWN @@${k}@@ is not a template token" }
        [string]$map[$k]
    })
[IO.File]::WriteAllText($OutPath, $out, (New-Object Text.UTF8Encoding($false)))
