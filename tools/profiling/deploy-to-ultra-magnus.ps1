# deploy-to-ultra-magnus.ps1
# RUN THIS ON THE VM (Virtual-Ten). Stages the slim Tier 1 GPU profiling bundle
# and (optionally) pushes it to Ultra-Magnus over SMB, then pulls results back.
#
# SAFETY: dry-run by default. Nothing is written to the host unless you pass
# -Execute. Without it, robocopy runs in /L (list-only) mode so you can review
# exactly what would transfer.
#
# P0-4 (verified deploy): a deploy is a *verified promotion*, not a blind copy.
# Before pushing, the source exe's embedded provenance stamp is read and recorded as
# the "requested" SHA (or matched against an explicit -RequestedSha). After copy, the
# mirrored release tree is re-hashed against a source manifest AND the COPIED exe's
# embedded stamp is read back off the host. The deploy HARD-FAILS unless
# embedded == requested == manifest, and a DEPLOYED.json receipt is written at the
# target so the bench records exactly what it is running. This kills the
# "fork/legacy of unknown vintage" problem on the benches.
#
# It also audits the Dell share for stale MLVApp.exe binaries (embedded SHA vs the
# current repo HEAD) so a known-stale bench binary can never masquerade as current.
#
# Transport: SMB share. The default target is Ultra-Magnus' SSD temp share.
# Deployment only needs file copy; LAUNCHING the runner on the host still needs
# a session (console/RDP) or a remote-exec channel (OpenSSH/WinRM).
#
# Typical flow:
#   1) review:   .\deploy-to-ultra-magnus.ps1
#   2) push:     .\deploy-to-ultra-magnus.ps1 -Execute
#   3) (on host, console session) run: G:\Temp\mlv-gpu-profile\run-ultra-magnus-profile.ps1
#   4) pull:     .\deploy-to-ultra-magnus.ps1 -PullResults -Execute
#   audit only:  .\deploy-to-ultra-magnus.ps1 -AuditDellShare

[CmdletBinding()]
param(
    # Ultra-Magnus exposes G:\Temp as a writable SSD staging share.
    [string]$Target = "\\Ultra-Magnus\g\Temp\mlv-gpu-profile",
    [string]$Clip   = "tests\fixtures\clips\large_dual_iso.mlv",
    [switch]$Execute,
    [switch]$PullResults,
    # Operator-declared intent. Empty = trust the source exe's own embedded SHA.
    [string]$RequestedSha = "",
    # Dell bench A/B share to audit for stale binaries.
    [string]$DellShare = "\\bachelor\dell-bench\dell-ab",
    [switch]$AuditDellShare,
    [switch]$SkipDellAudit
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "provenance-stamp.ps1")

$repo = Split-Path (Split-Path $PSScriptRoot)   # tools\profiling -> repo root
$releaseSrc  = Join-Path $repo "platform\qt\build-release\release"
$exeSrc      = Join-Path $releaseSrc "MLVApp.exe"
$receiptsSrc = Join-Path $repo "tests\fixtures\receipts"
$runnerSrc   = $PSScriptRoot
$clipSrc     = if ([System.IO.Path]::IsPathRooted($Clip)) { $Clip } else { Join-Path $repo $Clip }
$listOnly    = if ($Execute) { @() } else { @("/L") }

$head = (& git -C $repo rev-parse HEAD 2>$null)
if ($head) { $head = $head.Trim() }

function Rc($src, $dst, $extra) {
    # The OS robocopy by full path: the Resource Kit copy can sit first on PATH and rejects /BYTES, ignores /MAXAGE.
    $robocopyExe = Join-Path $env:SystemRoot 'System32\robocopy.exe'
    if (-not (Test-Path -LiteralPath $robocopyExe -PathType Leaf)) { throw "robocopy not found at $robocopyExe" }
    $rcArgs = @($src, $dst) + $extra + $listOnly + @("/NFL","/NDL","/NJH","/NP","/R:1","/W:1")
    Write-Host ("robocopy {0} -> {1} {2}" -f $src, $dst, ($extra -join " "))
    & $robocopyExe @rcArgs | Out-Null
    # robocopy exit codes < 8 are success
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE): $src -> $dst" }
}

# Per-file SHA256 manifest of a directory tree, keyed by forward-slashed relative path.
function Get-TreeManifest {
    param([Parameter(Mandatory = $true)][string]$Root)
    $rootFull = (Resolve-Path -LiteralPath $Root).ProviderPath
    $map = [ordered]@{}
    Get-ChildItem -LiteralPath $rootFull -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring($rootFull.Length).TrimStart('\','/').Replace('\','/')
        $map[$rel] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash
    }
    return $map
}

# Audit MLVApp.exe binaries on a share: embedded SHA vs the current repo HEAD.
function Invoke-DellShareAudit {
    param([string]$Share, [string]$CurrentHead)

    Write-Host ""
    Write-Host "=== Dell share audit: $Share ===" -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $Share)) {
        Write-Host "  share not reachable from this host (skipping)" -ForegroundColor Yellow
        return $null
    }

    $exes = @(Get-ChildItem -LiteralPath $Share -Recurse -File -Filter "MLVApp.exe" -ErrorAction SilentlyContinue)
    if ($exes.Count -eq 0) {
        Write-Host "  no MLVApp.exe found under the share" -ForegroundColor Yellow
        return @()
    }

    $shareFull = (Resolve-Path -LiteralPath $Share).ProviderPath
    $rows = foreach ($e in $exes) {
        $st = Get-MlvAppBuildStamp -ExePath $e.FullName
        $status =
            if (-not $st.found)                              { "no-stamp" }
            elseif ($CurrentHead -and ($st.sha -eq $CurrentHead)) { "current" }
            else                                             { "stale" }
        [pscustomobject]@{
            slot          = $e.FullName.Substring($shareFull.Length).TrimStart('\','/')
            embeddedSha   = if ($st.found) { $st.sha } else { $null }
            embeddedDirty = $st.dirty
            status        = $status
            lastWriteTime = $e.LastWriteTime.ToString("o")
        }
    }
    $rows | Format-Table slot, status, embeddedSha, embeddedDirty, lastWriteTime -AutoSize |
        Out-String | Write-Host
    $stale = @($rows | Where-Object { $_.status -ne "current" })
    if ($stale.Count -gt 0) {
        Write-Host ("  {0} of {1} bench binaries are NOT current HEAD ({2})" -f `
            $stale.Count, $rows.Count, ($(if ($CurrentHead) { $CurrentHead.Substring(0,12) } else { "HEAD unknown" }))) -ForegroundColor Yellow
    } else {
        Write-Host "  all bench binaries match current HEAD" -ForegroundColor Green
    }
    return $rows
}

# --- Audit-only mode (read-only; no copy) -----------------------------------
if ($AuditDellShare) {
    Invoke-DellShareAudit -Share $DellShare -CurrentHead $head | Out-Null
    exit 0
}

if (-not (Test-Path $releaseSrc)) { throw "Release runtime not found: $releaseSrc (build it first)" }
if (-not (Test-Path $clipSrc))    { throw "Clip not found: $clipSrc" }

if (-not $Execute) {
    Write-Host "*** DRY RUN (list-only). Re-run with -Execute to actually transfer. ***" -ForegroundColor Yellow
}

# --- Pull results back (unchanged) ------------------------------------------
if ($PullResults) {
    $resultsRemote = Join-Path $Target "results"
    $resultsLocal  = Join-Path $repo ".claude-state\profiling\20260613-gpu-lane-x1\ultra-magnus-results"
    if ($Execute) { New-Item -ItemType Directory -Force -Path $resultsLocal | Out-Null }
    Rc $resultsRemote $resultsLocal @("/E")
    Write-Host "Pulled host results -> $resultsLocal"
    exit 0
}

# --- Verified push ----------------------------------------------------------
# Establish the requested identity from the source exe's own embedded stamp.
$srcStamp = Get-MlvAppBuildStamp -ExePath $exeSrc
if (-not $srcStamp.found) {
    Write-Host "ABORT: source exe has no MLVAPP_BUILDSTAMP_v1 provenance field: $exeSrc" -ForegroundColor Red
    Write-Host "  Build a trustworthy, SHA-named release first: pwsh -NoProfile -File tools\build-release.ps1" -ForegroundColor Yellow
    exit 1
}
if ([string]::IsNullOrWhiteSpace($RequestedSha)) {
    $RequestedSha = $srcStamp.sha
} elseif ($srcStamp.sha -ne $RequestedSha) {
    Write-Host "ABORT: source exe embeds $($srcStamp.sha) but you requested $RequestedSha" -ForegroundColor Red
    exit 1
}
Write-Host ("Source exe embedded SHA: {0} (dirty={1}); requested: {2}" -f $srcStamp.sha, $srcStamp.dirty, $RequestedSha)
if ($head -and ($srcStamp.sha -ne $head)) {
    Write-Host ("  NOTE: source exe ({0}) is not current repo HEAD ({1})" -f $srcStamp.sha.Substring(0,12), $head.Substring(0,12)) -ForegroundColor Yellow
}
if ($srcStamp.dirty -ne 0) {
    Write-Host "  NOTE: source exe was built from a DIRTY tree (embedded dirty=1)" -ForegroundColor Yellow
}

# Source manifest (per-file SHA256) used to re-hash the copy after transfer.
$srcManifest = Get-TreeManifest -Root $releaseSrc

# Push the bundle.
Rc $releaseSrc  (Join-Path $Target "release")  @("/MIR")
Rc $receiptsSrc (Join-Path $Target "receipts") @("/E")
Rc (Split-Path $clipSrc) (Join-Path $Target "clips") @((Split-Path $clipSrc -Leaf))
Rc $runnerSrc   $Target @("run-ultra-magnus-profile.ps1")

if (-not $Execute) {
    Write-Host ""
    Write-Host ("Dry run complete. Would deploy {0} release files; requested SHA {1}." -f $srcManifest.Count, $RequestedSha)
    Write-Host "Add -Execute to transfer + verify."
    if (-not $SkipDellAudit) { Invoke-DellShareAudit -Share $DellShare -CurrentHead $head | Out-Null }
    exit 0
}

# Re-hash the mirrored release tree at the target against the source manifest.
$targetReleaseDir = Join-Path $Target "release"
$targetExe = Join-Path $targetReleaseDir "MLVApp.exe"
$mismatches = [System.Collections.Generic.List[string]]::new()
foreach ($rel in $srcManifest.Keys) {
    $tgtFile = Join-Path $targetReleaseDir ($rel -replace '/','\')
    if (-not (Test-Path -LiteralPath $tgtFile -PathType Leaf)) {
        [void]$mismatches.Add("missing on target: $rel")
        continue
    }
    $tgtHash = (Get-FileHash -LiteralPath $tgtFile -Algorithm SHA256).Hash
    if ($tgtHash -ne $srcManifest[$rel]) {
        [void]$mismatches.Add("hash mismatch: $rel (src $($srcManifest[$rel]) != tgt $tgtHash)")
    }
}

# Read the COPIED exe's embedded stamp back off the host.
$copiedStamp = Get-MlvAppBuildStamp -ExePath $targetExe

$embeddedOk = ($copiedStamp.found -and
               $copiedStamp.sha -eq $RequestedSha -and
               $RequestedSha -eq $srcStamp.sha)
$hashOk = ($mismatches.Count -eq 0)
$verified = ($embeddedOk -and $hashOk)

$receipt = [ordered]@{
    schema            = "mlvapp.deploy-receipt.v1"
    status            = if ($verified) { "verified" } else { "failed" }
    deployedAtUtc     = (Get-Date).ToUniversalTime().ToString("o")
    deployHost        = $env:COMPUTERNAME
    target            = $Target
    requestedSha      = $RequestedSha
    sourceEmbeddedSha = $srcStamp.sha
    copiedEmbeddedSha = $copiedStamp.sha
    copiedStampFound  = $copiedStamp.found
    copiedDirty       = $copiedStamp.dirty
    repoHead          = $head
    sourceMatchesHead = ($head -and ($srcStamp.sha -eq $head))
    exeSourceSha256   = $srcManifest["MLVApp.exe"]
    exeTargetSha256   = if (Test-Path -LiteralPath $targetExe -PathType Leaf) { (Get-FileHash -LiteralPath $targetExe -Algorithm SHA256).Hash } else { $null }
    filesVerified     = $srcManifest.Count - $mismatches.Count
    filesTotal        = $srcManifest.Count
    mismatches        = @($mismatches)
}

$receiptPath = Join-Path $Target "DEPLOYED.json"
$receipt | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $receiptPath -Encoding UTF8
Write-Host ""
Write-Host ("Receipt written: {0}" -f $receiptPath)

if (-not $verified) {
    Write-Host "DEPLOY VERIFICATION FAILED:" -ForegroundColor Red
    if (-not $copiedStamp.found) {
        Write-Host "  - copied exe has no MLVAPP_BUILDSTAMP_v1 provenance field" -ForegroundColor Red
    } elseif (-not $embeddedOk) {
        Write-Host ("  - embedded={0} requested={1} source={2} (must all match)" -f `
            $copiedStamp.sha, $RequestedSha, $srcStamp.sha) -ForegroundColor Red
    }
    $mismatches | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    exit 1
}

Write-Host ("Bundle deployed + VERIFIED to {0}" -f $Target) -ForegroundColor Green
Write-Host ("  embedded == requested == manifest: {0} ; {1}/{2} files re-hashed clean" -f `
    $RequestedSha.Substring(0,12), $receipt.filesVerified, $receipt.filesTotal)
# Plain string literal, NOT Join-Path: this is the host-local path on Ultra-Magnus
# (G:\), and Join-Path throws "Cannot find drive 'G'" on any other host (e.g. the VM).
$hostLocal = "G:\Temp\mlv-gpu-profile\run-ultra-magnus-profile.ps1"
Write-Host "Next: on Ultra-Magnus (console session, NOT RDP) run:"
Write-Host "  powershell -ExecutionPolicy Bypass -File $hostLocal"

if (-not $SkipDellAudit) { Invoke-DellShareAudit -Share $DellShare -CurrentHead $head | Out-Null }
exit 0
