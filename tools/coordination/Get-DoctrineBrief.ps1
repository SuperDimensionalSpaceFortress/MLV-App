<#
.SYNOPSIS
    Fail-closed doctrine brief for lane prompt injection (Compose calls this).

.DESCRIPTION
    Read-only fetch of fleet doctrine via gh Contents API (implemented in
    get_doctrine_brief.py). Law 1: doctrine is data, not executable. Lanes never
    browse the bus; hubs PULL-DIFF-FOLD via doctrine-sync on the machine.

    Exit 0: brief markdown on stdout (or -OutFile).
    Exit 2: one line starting REFUSED: on stdout (fetch/fixture failure).

.NOTES
    ASCII-only by project convention. Does not write the bus. Does not set or
    reintroduce MLV_FLEET_BUS_ROOT.
#>
[CmdletBinding()]
param(
    [string]$DoctrineRepo = $(if ($env:MLV_DOCTRINE_REPO) { $env:MLV_DOCTRINE_REPO } else { 'layibabalola/softwarefactory-fleet-doctrine' }),
    [string]$Ref = $(if ($env:MLV_DOCTRINE_REF) { $env:MLV_DOCTRINE_REF } else { 'master' }),
    [string]$FixtureRoot = $(if ($env:MLV_DOCTRINE_FIXTURE_ROOT) { $env:MLV_DOCTRINE_FIXTURE_ROOT } else { '' }),
    [switch]$NoCandidateFallback,
    [string]$OutFile = ''
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$py = Join-Path $PSScriptRoot 'get_doctrine_brief.py'
if (-not (Test-Path -LiteralPath $py)) {
    Write-Output "REFUSED: doctrine-brief-missing: $py"
    exit 2
}

$python = $null
foreach ($cand in @('python', 'python3', 'py')) {
    $cmd = Get-Command $cand -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source; break }
}
if (-not $python) {
    Write-Output 'REFUSED: python-not-found-for-doctrine-brief'
    exit 2
}

$argList = [System.Collections.Generic.List[string]]::new()
[void]$argList.Add($py)
[void]$argList.Add('--repo'); [void]$argList.Add($DoctrineRepo)
[void]$argList.Add('--ref'); [void]$argList.Add($Ref)
if ($FixtureRoot) {
    [void]$argList.Add('--fixture-root'); [void]$argList.Add($FixtureRoot)
}
if ($NoCandidateFallback) {
    [void]$argList.Add('--no-candidate-fallback')
}
if ($OutFile) {
    [void]$argList.Add('-o'); [void]$argList.Add($OutFile)
}

& $python @argList
exit $LASTEXITCODE
