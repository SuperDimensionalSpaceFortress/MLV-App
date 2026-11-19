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
# Windows release uses (.github/workflows/Windows.yml), always with --verify-installed, plus --record-only: only the
# named record is validated, never the repository-wide tracked-file census (adversary ae560106-A2). The extractor
# re-hashes the in-repo archive and every selected member against the manifest before and after the atomic
# install, so the bytes cannot depend on which revision RepoRoot has checked out. RepoRoot must be a git
# checkout (the archive's Git tracking is checked with git ls-files on that one path). The child is synchronous and bounded: past
# TimeoutSec its whole tree is killed and the result says timedOut (and killConfirmed, whether the exit was
# seen). Nothing here throws on a failed install, including a locked installed file (adversary 32bf39a9-A2);
# the caller reads .ok and stops with its own typed failure.

# Kills a timed-out child's whole tree and reports whether its exit was SEEN; a Kill that throws (access
# denied) or a wait that times out is never reported as killed (adversary 32bf39a9-A3).
function Stop-PinnedPayloadChild {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        $Process,

        [ValidateRange(0, 600000)]
        [int]$ConfirmMs = 10000
    )
    $killError = ''
    try { $null = $Process.Kill($true) } catch { $killError = $_.Exception.Message }
    $exited = $false
    try { $exited = [bool]$Process.WaitForExit($ConfirmMs) } catch { if (-not $killError) { $killError = "the exit could not be awaited: $($_.Exception.Message)" } }
    # The ROOT's exit is not the tree's: a Kill(true) that threw (a descendant it could not end) leaves the tree
    # unconfirmed even when the root exited (adversary 6627beef-A1).
    return [pscustomobject][ordered]@{ exited = $exited; killError = $killError; confirmed = ($exited -and -not $killError) }
}

# Puts back every original a killed extractor left in a transaction folder it created (extract_vendored_native_payload.py
# _publish_staged_transaction moves each existing destination into .vendored-payload-transaction-*\<name> before it installs the
# replacement). Only folders NOT in -Preexisting are touched. Moves only, never a delete: the backup replaces whatever now sits at
# the destination (all-or-nothing, as the Python rollback does), and the emptied folder is left for the failed build to report.
# Never throws; returns { restored, errors }.
function Restore-PinnedPayloadTransaction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$OutputDir,

        [AllowEmptyCollection()]
        [string[]]$Preexisting = @()
    )
    $restored = @()
    $errors = @()
    try {
        $folders = @(Get-ChildItem -LiteralPath $OutputDir -Force -Directory -Filter '.vendored-payload-transaction-*' -ErrorAction Stop |
            Where-Object { $Preexisting -notcontains $_.Name -and -not $_.LinkType })
    } catch {
        return [pscustomobject][ordered]@{ restored = @(); errors = @("the output folder could not be listed: $($_.Exception.Message)") }
    }
    foreach ($folder in $folders) {
        foreach ($backup in @(Get-ChildItem -LiteralPath $folder.FullName -Force -File -ErrorAction SilentlyContinue)) {
            if ($backup.LinkType) { $errors += "$($backup.FullName) is a link; not restored"; continue }
            try {
                [System.IO.File]::Move($backup.FullName, (Join-Path $OutputDir $backup.Name), $true)
                $restored += $backup.Name
            } catch {
                $errors += "$($backup.Name): $($_.Exception.Message)"
            }
        }
    }
    return [pscustomobject][ordered]@{ restored = $restored; errors = $errors }
}

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
        ok            = $false
        reason        = ''
        exitCode      = $null
        timedOut      = $false
        killConfirmed = $null
        stdout        = ''
        stderr        = ''
        installed     = @()
        transactionRestored = @()
    }
    # Every step fills $result; anything that throws (a locked installed file, an unreadable report) is a
    # typed ok=false here, never an exception past the caller's own failure branch.
    try {
        $null = Invoke-PinnedPayloadInstall -Result $result -RepoRoot $RepoRoot -PayloadId $PayloadId -ArchiveReference $ArchiveReference -OutputDir $OutputDir -TimeoutSec $TimeoutSec
    } catch {
        $result.ok = $false
        $result.installed = @()
        $result.reason = "the install could not be completed or verified: $($_.Exception.Message)"
    }
    return [pscustomobject]$result
}

function Invoke-PinnedPayloadInstall {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [System.Collections.Specialized.OrderedDictionary]$Result,

        [Parameter(Mandatory)]
        [string]$RepoRoot,

        [Parameter(Mandatory)]
        [string]$PayloadId,

        [Parameter(Mandatory)]
        [string]$ArchiveReference,

        [Parameter(Mandatory)]
        [string]$OutputDir,

        [Parameter(Mandatory)]
        [int]$TimeoutSec
    )
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
    # --record-only (adversary ae560106-A2): validate the named record alone. The full manifest validation walks every
    # tracked file and opens a prefix of each, owner clip fixtures included; the hosted workflows keep that full check.
    foreach ($argument in @('-3', '-m', 'tools.repo_hygiene.extract_vendored_native_payload', '--repo-root', $RepoRoot, '--payload-id', $PayloadId, '--archive-reference', $ArchiveReference, '--output-dir', $OutputDir, '--record-only', '--verify-installed')) {
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
    # No inherited GIT_* variable reaches the manifest validator's git children (adversary 32bf39a9-A1): GIT_TRACE2_EVENT
    # naming a tracked file appends to it, and GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE would aim git ls-files at another
    # repository. git then reads only RepoRoot, the working directory.
    foreach ($name in @($psi.Environment.Keys)) {
        if ([string]$name -like 'GIT_*') { [void]$psi.Environment.Remove([string]$name) }
    }

    # The extractor's own transaction folders already in OutputDir before this child starts; only a NEW one is this child's
    # (adversary f92303b7-A1, reconciled below after a confirmed timeout kill).
    $preexistingTransactions = @(Get-ChildItem -LiteralPath $OutputDir -Force -Directory -Filter '.vendored-payload-transaction-*' -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
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
            $result.timedOut = $true
            $stop = Stop-PinnedPayloadChild -Process $process
            $result.killConfirmed = [bool]$stop.confirmed
            $childPid = $process.Id
            $killError = [string]$stop.killError
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
        $result.reason = if ($result.killConfirmed) {
            "the extractor was still running after $TimeoutSec s; its process tree was killed and its exit seen"
        } else {
            "the extractor was still running after $TimeoutSec s and the kill was NOT confirmed (pid $childPid or a descendant may still be running$(if ($killError) { "; kill error: $killError" }))"
        }
        # Adversary f92303b7-A1: a kill between the extractor's move of an original into its transaction backup and the install of
        # the replacement bypasses the Python rollback and leaves the destination missing. Once the kill is CONFIRMED (no child can
        # still move files) the originals are put back; with an unconfirmed kill nothing is touched, since the child may still run.
        if ($result.killConfirmed) {
            $restore = Restore-PinnedPayloadTransaction -OutputDir $OutputDir -Preexisting $preexistingTransactions
            $result.transactionRestored = @($restore.restored)
            $result.reason += "; transaction reconciled: restored [$(@($restore.restored) -join ', ')]$(if (@($restore.errors).Count) { ", errors [$(@($restore.errors) -join '; ')]" })"
        } else {
            $result.reason += '; its transaction was NOT reconciled (the extractor may still be running)'
        }
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

# Adversary ae560106-A1: a delete or a rewrite of the installed ffmpeg.exe after its readback reached the zip or the manifest
# unnoticed. These two checks read the bytes AT the point of publication. Neither throws; the caller reads .ok.

# Opens Path for reading with FileShare.Read (no writer and no delete can open it while it is held), hashes it through that
# handle and compares size and sha256. On ok the OPEN stream is returned in .stream: the caller writes its record while
# holding it and disposes it afterwards, so the file the record describes cannot change in between.
function Open-PinnedFileHold {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$Path,

        [Parameter(Mandatory)]
        [ValidatePattern('^[0-9a-f]{64}\z')]
        [string]$ExpectedSha256,

        [Parameter(Mandatory)]
        [long]$ExpectedBytes
    )
    $hold = [ordered]@{ ok = $false; reason = ''; bytes = $null; sha256 = ''; stream = $null }
    $stream = $null
    try {
        $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        $hold.bytes = [long]$stream.Length
        $hold.sha256 = [System.Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($stream)).ToLowerInvariant()
    } catch {
        if ($stream) { $stream.Dispose() }
        $hold.reason = "$Path could not be opened and hashed: $($_.Exception.Message)"
        return [pscustomobject]$hold
    }
    if ($hold.sha256 -cne $ExpectedSha256 -or $hold.bytes -ne $ExpectedBytes) {
        $stream.Dispose()
        $hold.reason = "$Path is $($hold.bytes) bytes sha256 $($hold.sha256), not the recorded $ExpectedBytes bytes sha256 $ExpectedSha256"
        return [pscustomobject]$hold
    }
    $hold.stream = $stream
    $hold.ok = $true
    return [pscustomobject]$hold
}

# Proves a published zip carries exactly one root entry MemberName whose bytes are the recorded ones. The zip is opened once
# with FileShare.Read; the whole-file sha256 (.zipSha256) and the entry check read that same handle, so the hash the caller
# publishes describes the very bytes whose entry was verified.
function Test-PinnedZipMember {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$ZipPath,

        [Parameter(Mandatory)]
        [ValidatePattern('^[A-Za-z0-9._-]+\z')]
        [string]$MemberName,

        [Parameter(Mandatory)]
        [ValidatePattern('^[0-9a-f]{64}\z')]
        [string]$ExpectedSha256,

        [Parameter(Mandatory)]
        [long]$ExpectedBytes
    )
    $check = [ordered]@{ ok = $false; reason = ''; zipSha256 = ''; memberBytes = $null; memberSha256 = '' }
    $stream = $null
    $zip = $null
    try {
        Add-Type -AssemblyName System.IO.Compression
        $stream = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        $check.zipSha256 = [System.Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($stream)).ToLowerInvariant()
        $null = $stream.Seek(0, [System.IO.SeekOrigin]::Begin)
        $zip = [System.IO.Compression.ZipArchive]::new($stream, [System.IO.Compression.ZipArchiveMode]::Read, $true)
        $entries = @($zip.Entries | Where-Object { [string]::Equals(([string]$_.FullName).Replace('\', '/'), $MemberName, [StringComparison]::OrdinalIgnoreCase) })
        if ($entries.Count -ne 1) {
            $check.reason = "$ZipPath holds $($entries.Count) root entries named $MemberName, not exactly one"
            return [pscustomobject]$check
        }
        $entryStream = $entries[0].Open()
        try {
            $check.memberSha256 = [System.Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($entryStream)).ToLowerInvariant()
        } finally { $entryStream.Dispose() }
        $check.memberBytes = [long]$entries[0].Length
    } catch {
        $check.reason = "$ZipPath could not be read as a zip: $($_.Exception.Message)"
        return [pscustomobject]$check
    } finally {
        if ($zip) { $zip.Dispose() }
        if ($stream) { $stream.Dispose() }
    }
    if ($check.memberSha256 -cne $ExpectedSha256 -or $check.memberBytes -ne $ExpectedBytes) {
        $check.reason = "$ZipPath entry $MemberName is $($check.memberBytes) bytes sha256 $($check.memberSha256), not the recorded $ExpectedBytes bytes sha256 $ExpectedSha256"
        return [pscustomobject]$check
    }
    $check.ok = $true
    return [pscustomobject]$check
}

Export-ModuleMember -Function Test-ReviewedReleaseRuntimeExtraName, Install-PinnedVendoredPayload, Stop-PinnedPayloadChild, Open-PinnedFileHold, Test-PinnedZipMember
