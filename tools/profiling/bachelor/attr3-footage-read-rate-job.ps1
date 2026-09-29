# attr3-footage-read-rate-job.ps1 -- GENERATOR (runs locally; nothing here runs on Bachelor and
# nothing here is submitted to Bachelor -- the hub does both). Emits a job that MEASURES how fast
# the measurement host reads owner-consented clip <id> (BACHELOR-OWNER-CLIP-STAGE-STALL-1): block
# read vs Get-FileHash, cold vs repeat, with the host's RAM, free space and real-time-scanner CPU.
# See Attr3FootageReadRateJob.psm1 for what each pass is.
#
# The clip is RESOLVED, never handed a path: tools/gates/resolve_consented_clip.py runs as a child
# process writing only into a private temp file, this generator reads it in-process and deletes
# it, and the parts reach the job as base64. Exactly the route attr3-footage-presence-job.ps1
# takes; there is no parameter that skips the resolver, and no path reaches stdout.
#
# Usage:
#   pwsh -NoProfile -File tools\profiling\bachelor\attr3-footage-read-rate-job.ps1 `
#       -ClipId M16-1243 -OutDir <staging-dir>
# then submit the emitted <jobId>.job.ps1 with tools\profiling\um-run.ps1 -AgentShare '\\bachelor\mlv-agent'.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$')]
    [string]$ClipId,

    [Parameter(Mandatory = $true)]
    [string]$OutDir,

    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path,

    [string]$AgentRoot = 'C:\mlvtmp\mlv-agent'
)

$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'AttrCudaArtifacts.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'Attr3FootageReadRateJob.psm1') -Force

$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
$resolverPath = Join-Path $RepoRoot 'tools\gates\resolve_consented_clip.py'
if (-not (Test-Path -LiteralPath $resolverPath -PathType Leaf)) {
    throw "ATTR3_READRATE_RESOLVER_MISSING resolver not found at $resolverPath"
}

$python = Get-Command python.exe -ErrorAction SilentlyContinue
$py = $null
if ($null -ne $python) {
    $major = @(& $python.Source -c 'import sys; print(sys.version_info[0])' 2>$null)
    if ($LASTEXITCODE -eq 0 -and $major.Count -eq 1 -and $major[0] -eq '3') {
        $py = [pscustomobject]@{ Exe = $python.Source; PrefixArgs = @() }
    }
}
if ($null -eq $py) {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($null -eq $launcher) { throw 'ATTR3_READRATE_NO_PYTHON no Python 3 interpreter is available to run the resolver' }
    $py = [pscustomobject]@{ Exe = $launcher.Source; PrefixArgs = @('-3') }
}

$emitPath = Join-Path ([IO.Path]::GetTempPath()) ("attr3-footage-read-rate-resolve-$([guid]::NewGuid().ToString('N')).json")
try {
    $summaryLines = @(& $py.Exe @(@($py.PrefixArgs) + @($resolverPath, '--clip-id', $ClipId, '--repo-root', $RepoRoot, '--emit-json', $emitPath)) 2>&1)
    if ($LASTEXITCODE -ne 0) {
        # The resolver's own summary line is path-free by contract.
        throw "ATTR3_READRATE_RESOLVE_REFUSED clip '$ClipId' was refused by the resolver (exit $LASTEXITCODE): $($summaryLines -join ' ')"
    }
    if (-not (Test-Path -LiteralPath $emitPath -PathType Leaf)) {
        throw 'ATTR3_READRATE_EMIT_MISSING resolver exited 0 but wrote no --emit-json file'
    }
    $emitBytes = [IO.File]::ReadAllBytes($emitPath)
} finally {
    if (Test-Path -LiteralPath $emitPath) { Remove-Item -LiteralPath $emitPath -Force }
}

$resolved = [Text.Encoding]::UTF8.GetString($emitBytes) | ConvertFrom-Json
if ($resolved.clipId -cne $ClipId) {
    throw "ATTR3_READRATE_CLIP_ID_MISMATCH resolver emitted clipId '$($resolved.clipId)' for requested '$ClipId'"
}

# Only the module's own path-free RESULT= line is echoed (its returned object names a local file).
New-Attr3FootageReadRateJob -ClipId $ClipId -Parts @($resolved.parts) -OutDir $OutDir -AgentRoot $AgentRoot | Where-Object { $_ -is [string] }
