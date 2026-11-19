Set-StrictMode -Version Latest

function Test-ReviewedReleaseRuntimeExtraName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Name
    )

    $exactNames = @(
        'pixel_maps',
        'releases',
        'cudart64_12.dll',
        'igpu_recon_cuda.dll',
        'igpu_recon_cuda.arch.json'
    )
    if ($exactNames -ccontains $Name) {
        return $true
    }
    return [bool]($Name -cmatch '^(?:avcodec|avformat|avutil|swresample|swscale)-[0-9]+\.dll$')
}

# EXPORT-PRORES-FFMPEG-EXE-PACKAGE-1: install the selected members of one sha-pinned vendored payload
# (tools/gates/vendored-native-payloads.json) into OutputDir with the same extractor and flags the hosted
# Windows release uses (.github/workflows/Windows.yml), always with --verify-installed. The extractor
# re-hashes the in-repo archive and every selected member against the manifest before and after the atomic
# install, so the bytes cannot depend on which revision RepoRoot has checked out. RepoRoot must be a git
# checkout (the manifest validator runs git ls-files). The child is synchronous and bounded: past
# TimeoutSec its whole tree is killed and the result says timedOut. Nothing here throws on a failed
# install; the caller reads .ok and stops with its own typed failure.
function Install-PinnedVendoredPayload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$RepoRoot,

        [Parameter(Mandatory)]
        [ValidatePattern('^[A-Za-z0-9._-]+\z')]
        [string]$PayloadId,

        [Parameter(Mandatory)]
        [ValidatePattern('^[A-Za-z0-9._-]+\z')]
        [string]$ArchiveReference,

        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$OutputDir,

        [ValidateRange(1, 3600)]
        [int]$TimeoutSec = 600
    )

    $result = [ordered]@{
        ok        = $false
        reason    = ''
        exitCode  = $null
        timedOut  = $false
        stdout    = ''
        stderr    = ''
        installed = @()
    }
    # A drive-relative (C:dir) or rooted-but-driveless (\dir) path resolves against process state the
    # child does not share, so both paths must be fully qualified.
    foreach ($pair in @(@('RepoRoot', $RepoRoot), @('OutputDir', $OutputDir))) {
        if (-not [System.IO.Path]::IsPathFullyQualified([string]$pair[1])) {
            $result.reason = "$($pair[0]) is not a fully qualified path: $($pair[1])"
            return [pscustomobject]$result
        }
    }
    $RepoRoot = [System.IO.Path]::GetFullPath($RepoRoot)
    $OutputDir = [System.IO.Path]::GetFullPath($OutputDir)
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'tools\repo_hygiene\extract_vendored_native_payload.py') -PathType Leaf)) {
        $result.reason = "RepoRoot holds no tools\repo_hygiene\extract_vendored_native_payload.py: $RepoRoot"
        return [pscustomobject]$result
    }
    $launcher = Get-Command -Name 'py.exe' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $launcher) {
        $result.reason = 'the Python launcher py.exe is not on PATH'
        return [pscustomobject]$result
    }

    $psi = [System.Diagnostics.ProcessStartInfo]::new($launcher.Source)
    # ArgumentList quotes each element itself, so a path with spaces stays one argument. No -I: it would
    # drop the working directory from sys.path and with it the tools package.
    foreach ($argument in @('-3', '-m', 'tools.repo_hygiene.extract_vendored_native_payload', '--repo-root', $RepoRoot, '--payload-id', $PayloadId, '--archive-reference', $ArchiveReference, '--output-dir', $OutputDir, '--verify-installed')) {
        [void]$psi.ArgumentList.Add([string]$argument)
    }
    $psi.WorkingDirectory = $RepoRoot
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    # The tools package must come from RepoRoot (the working directory), never from an inherited search
    # path; and the checkout is not written to (no __pycache__).
    foreach ($name in @('PYTHONPATH', 'PYTHONHOME', 'PYTHONSAFEPATH', 'PYTHONSTARTUP')) { [void]$psi.Environment.Remove($name) }
    $psi.Environment['PYTHONDONTWRITEBYTECODE'] = '1'

    try {
        $process = [System.Diagnostics.Process]::Start($psi)
    } catch {
        $result.reason = "the extractor did not start: $($_.Exception.Message)"
        return [pscustomobject]$result
    }
    try {
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutSec * 1000)) {
            try { $process.Kill($true) } catch { }
            [void]$process.WaitForExit(10000)
            $result.timedOut = $true
        } else {
            $process.WaitForExit()
            $result.exitCode = $process.ExitCode
        }
        if ($stdoutTask.Wait(10000)) { $result.stdout = [string]$stdoutTask.Result }
        if ($stderrTask.Wait(10000)) { $result.stderr = [string]$stderrTask.Result }
    } finally {
        $process.Dispose()
    }
    if ($result.timedOut) {
        $result.reason = "the extractor was still running after $TimeoutSec s and was killed"
        return [pscustomobject]$result
    }
    if ($result.exitCode -ne 0) {
        $result.reason = "the extractor exited $($result.exitCode): $($result.stderr.Trim())"
        return [pscustomobject]$result
    }
    try {
        $report = $result.stdout | ConvertFrom-Json -ErrorAction Stop
    } catch {
        $result.reason = "the extractor exited 0 but printed no JSON report: $($_.Exception.Message)"
        return [pscustomobject]$result
    }
    # StrictMode Latest throws on a missing property, so every report field is read through PSObject.
    $reportedId = if ($report -and $report.PSObject.Properties['payload_id']) { [string]$report.payload_id } else { '' }
    if ($reportedId -cne $PayloadId) {
        $result.reason = "the extractor reported payload '$reportedId', not '$PayloadId'"
        return [pscustomobject]$result
    }
    $rows = if ($report.PSObject.Properties['installed']) { @($report.installed) } else { @() }
    if ($rows.Count -eq 0) {
        $result.reason = 'the extractor exited 0 but verified no installed file (was --verify-installed honoured?)'
        return [pscustomobject]$result
    }
    # Re-read every installed file here: the record is what is on disk now, not what the child said.
    $installed = @()
    $outputDirKey = [System.IO.Path]::TrimEndingDirectorySeparator($OutputDir)
    foreach ($row in $rows) {
        foreach ($field in @('path', 'bytes', 'sha256')) {
            if (-not ($row -and $row.PSObject.Properties[$field])) {
                $result.reason = "an installed-file record from the extractor has no $field"
                return [pscustomobject]$result
            }
        }
        $path = [System.IO.Path]::GetFullPath([string]$row.path)
        if (-not [string]::Equals([System.IO.Path]::TrimEndingDirectorySeparator([System.IO.Path]::GetDirectoryName($path)), $outputDirKey, [StringComparison]::OrdinalIgnoreCase)) {
            $result.reason = "the extractor reported an installed file outside the output directory: $path"
            return [pscustomobject]$result
        }
        $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
        if (-not $item -or $item.PSIsContainer -or $item.LinkType) {
            $result.reason = "the reported installed file is not a plain file: $path"
            return [pscustomobject]$result
        }
        $sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($sha256 -cne ([string]$row.sha256).ToLowerInvariant() -or [long]$item.Length -ne [long]$row.bytes) {
            $result.reason = "the installed file does not match the extractor's verified record: $path"
            return [pscustomobject]$result
        }
        $installed += [pscustomobject][ordered]@{ name = $item.Name; path = $path; bytes = [long]$item.Length; sha256 = $sha256 }
    }
    $result.installed = $installed
    $result.ok = $true
    return [pscustomobject]$result
}

Export-ModuleMember -Function Test-ReviewedReleaseRuntimeExtraName, Install-PinnedVendoredPayload
