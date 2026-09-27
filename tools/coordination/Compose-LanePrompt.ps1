<#
.SYNOPSIS
    The permanent composer named by plan step 0.35 (S80): fields-*.md + product-card-TEMPLATE.md,
    or a full card-*.md standing alone, becomes a dispatch-ready lane prompt.

.DESCRIPTION
    Tracked and hashed in every execution-control receipt from 0.35 onward. Invoke-Workstream.ps1
    dot-sources the pure logic in compose-lane-prompt-core.ps1 directly (it needs the resolved
    branch name before it can create the lane's worktree, not just the prompt text) - this script
    is the standalone CLI form: run it by hand, or from a test, to compose one prompt and either
    print it or write it to -OutFile.

    Refuses (exit 3, one line on stdout starting REFUSED:) on unknown-field (a fields-*.md line
    matching ^[A-Z][A-Z0-9_]*: at column 0 whose label is not in the COMPOSER CONTRACT) or
    composer-incomplete (a {{PLACEHOLDER}} survives substitution - the no-{{ assertion runs last).

.NOTES
    ASCII-only by project convention.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ProcedurePath,
    [string]$TemplatePath = '',
    [Parameter(Mandatory)][string]$WorkDir,
    [Parameter(Mandatory)][string]$BaseSha,
    [Parameter(Mandatory)][string]$RunDir,
    [string]$Ts = '',
    [Parameter(Mandatory)][string]$GhCapability,
    [string]$OutFile = '',
    # Test-only: honoured by get_doctrine_brief.py only under pytest and only inside
    # tools/coordination/fixtures/; refused everywhere else. There is no parameter that hands
    # in brief text: the only brief a composed prompt can carry is the live fetch (or, under
    # pytest, the checked-in fixture), and the brief records which.
    [string]$DoctrineFixtureRoot = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

. (Join-Path $PSScriptRoot 'compose-lane-prompt-core.ps1')

if (-not $TemplatePath) {
    $TemplatePath = Join-Path $PSScriptRoot '..\..\docs\lane-prompts\v2\product-card-TEMPLATE.md'
}

try {
    $result = Get-ComposedLanePrompt -ProcedurePath $ProcedurePath -TemplatePath $TemplatePath `
        -WorkDir $WorkDir -BaseSha $BaseSha -RunDir $RunDir -Ts $Ts -GhCapability $GhCapability -DoctrineFixtureRoot $DoctrineFixtureRoot
} catch {
    Write-Output ("REFUSED: {0}" -f $_.Exception.Message)
    exit 3
}

if ($OutFile) {
    [System.IO.File]::WriteAllText($OutFile, $result.Text, [System.Text.UTF8Encoding]::new($false))
    Write-Output "COMPOSED: $OutFile branch=$($result.Branch) card=$($result.CardId)"
} else {
    Write-Output $result.Text
}
exit 0
