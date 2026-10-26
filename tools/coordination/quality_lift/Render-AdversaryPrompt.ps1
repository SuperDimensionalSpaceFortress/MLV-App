<#
.SYNOPSIS
  Fill adversary-pass-prompt.md for one exact head and write the result as UTF-8 (no BOM).

.DESCRIPTION
  Replaces every @@NAME@@ token in ONE pass, so text inside a value (a worktree path or a commit subject that
  looks like a token) is never expanded again. Whole <!-- ... --> comment blocks are dropped first.

  Plain tokens (@@WORKDIR@@, @@HEAD@@, @@BASE@@, @@DIFF@@, @@INTENT@@, @@CARD@@, @@ROUND@@) are prose. A token
  that lands inside a command the reviewer is told to run has a _PS twin that the renderer fills with a
  PowerShell single-quoted literal (@@WORKDIR_PS@@, @@RANGE_PS@@ = base...head): every quote character
  PowerShell accepts (' and the typographic U+2018 U+2019 U+201A U+201B) is doubled, so $name, $(...), a
  backtick or a semicolon in the value reaches git unchanged. Quoting lives here, never in the template text.
  An unknown token, a blank or control-character path, a head that is not 40 lowercase hex, a base that is
  blank or starts with '-', or a path holding two backticks in a row (it would end the markdown code span the
  command sits in) is refused and nothing is written.
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
    return "'" + [regex]::Replace($Value, "['‘’‚‛]", { param($m) $m.Value + $m.Value }) + "'"
}

if ([string]::IsNullOrWhiteSpace($WorkDir) -or $WorkDir -match '[\x00-\x1F\x7F]') { throw "WORKDIR_INVALID blank or control character in -WorkDir" }
if ($WorkDir.Contains('``')) { throw "WORKDIR_INVALID two backticks in a row in -WorkDir" }
if ($Head -cnotmatch '^[0-9a-f]{40}$') { throw "HEAD_INVALID -Head must be 40 lowercase hex" }
if ([string]::IsNullOrWhiteSpace($Base) -or $Base -match '[\x00-\x1F\x7F]' -or $Base.StartsWith('-') -or $Base.Contains('``')) { throw "BASE_INVALID blank, control character, leading '-' or two backticks in -Base" }

$tpl = Get-Content -LiteralPath $TemplatePath -Raw
$tpl = [regex]::Replace($tpl, '(?s)<!--.*?-->[ \t]*\r?\n?', '')
$map = @{
    HEAD = $Head; BASE = $Base; CARD = $Card; ROUND = $Round; WORKDIR = $WorkDir; DIFF = $Diff; INTENT = $Intent
    WORKDIR_PS = (ConvertTo-PsSingleQuoted $WorkDir); RANGE_PS = (ConvertTo-PsSingleQuoted "$Base...$Head")
}
$out = [regex]::Replace($tpl, '@@([A-Z][A-Z_]*)@@', {
        param($m)
        $k = $m.Groups[1].Value
        if (-not $map.ContainsKey($k)) { throw "TOKEN_UNKNOWN @@${k}@@ is not a template token" }
        [string]$map[$k]
    })
[IO.File]::WriteAllText($OutPath, $out, (New-Object Text.UTF8Encoding($false)))
