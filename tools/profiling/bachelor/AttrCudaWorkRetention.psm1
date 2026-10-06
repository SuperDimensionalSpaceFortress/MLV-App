# AttrCudaWorkRetention.psm1 -- keep-newest retention for the assembler's per-head scratch trees.
#
# WHY (LANE-BUILD-WORKDIR-RETENTION-1, ledger 2026-10-06T17:05Z). Every head iteration of a build leg
# leaves <build dir>\.work-<sha12> behind (about 1.2 GiB: the expanded source, the qmake/make tree and
# .job-tmp). The package, exe, DLL and build.json that matter are PUBLISHED into <build dir> itself, so a
# superseded .work-<sha12> is dead weight once its small evidence is kept. Twenty such trees took C: from
# 40 to 30 GiB in an afternoon.
#
# SCOPE. A leg gives each head its own build dir (build-ctl, build-head, build-head2, ...) under one lane-*
# run dir, so the keep-newest rule spans the run's build* dirs (-IncludeSiblingBuildDirs). A tree written
# within -MinAgeMinutes (default 120) is never dropped: a sibling leg may be building into it.
#
# ELIGIBILITY (pr285-r2, sol blockers 1 and 2). Only a FINISHED, PUBLISHED head is a candidate: its build dir
# must hold playback-attr-3-cuda-<sha12>-build.json (the assembler writes it LAST, after every artifact is
# published and hash-verified) naming that sha, assembled after the tree was created. A tree with no such
# manifest failed before staging or is still building: its binaries may exist nowhere else, so it is kept
# whole (kept-unpublished). Before any drop, every file of a candidate is scanned (reparse points are not
# entered): a file with recording/container magic bytes, or one over -MaxFileBytes (2 GiB), keeps the whole
# tree (kept-guard). The one exemption is a TRACKED FIXTURE, identified by git object identity: the file sits
# under the tree's own src\tests\fixtures\ and `git hash-object --no-filters <file>` equals the blob id of
# `<sha12>:<path relative to src\>` in -RepoRoot. Any failed step keeps the tree.
#
# WHAT IT KEEPS. The newest .work-<sha12> (and the one named by -KeepSha) stays whole. For each superseded
# tree the evidence is copied FIRST to <build dir>\evidence-<sha12>\ and the copy is VERIFIED (file count
# and bytes) before anything is dropped; a copy that does not verify keeps the tree. Evidence = every
# *.exe, *.dll, *.receipt.json, *.log, *.txt, *.json and capture (*.png, *.jpg, *.jpeg, *.csv, *.etl)
# found OUTSIDE the top-level 'src' directory. 'src' is the expanded `git archive` of the commit plus the
# object files make wrote beside it: it is reproducible from the commit, and the exe that ran is kept as
# the staged copy under .job-tmp and as the published file in the build dir.
#
# HOW IT DROPS. Never a recursive pathname delete: the drop is Remove-AttrCudaTree with the build dir's
# ownership journal (OWNER-FOOTAGE-NO-HARDLINK-2), so only what the journal proves this build made is
# removed, a name it cannot prove is LEFT and typed, and a tree that is not wholly proven stays standing
# (reported kept-partial / kept-unproven). Requires AttrCudaArtifacts.psm1 to be imported in the session.
#
# NO FOOTAGE. This module never opens, names, globs or resolves a media file.

Set-StrictMode -Version Latest

$script:WorkTreeNameRx = '^\.work-[0-9a-f]{12}$'
$script:EvidenceFilePatterns = @('*.exe', '*.dll', '*.receipt.json', '*.log', '*.txt', '*.json', '*.png', '*.jpg', '*.jpeg', '*.csv', '*.etl')
$script:EvidenceSkipTopLevel = @('src')
# The first four bytes of the raw-video container, as bytes: this module never names a media extension.
$script:ContainerMagic = [byte[]](0x4D, 0x4C, 0x56, 0x49)

function Get-AttrCudaTreeBytes {
    # Bytes of every file under a directory, not descending into or counting a reparse point.
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $options = [IO.EnumerationOptions]::new()
    $options.RecurseSubdirectories = $true
    $options.IgnoreInaccessible = $true
    $options.AttributesToSkip = [IO.FileAttributes]::ReparsePoint
    $sum = [long]0
    try {
        foreach ($file in ([IO.DirectoryInfo]$Path).EnumerateFiles('*', $options)) { $sum += $file.Length }
    } catch { }
    return $sum
}

function Test-AttrCudaWorkPublished {
    # $null when the head <sha12> of this build dir is published, otherwise the reason it is not.
    # Published = the manifest the assembler writes last exists, parses, names this sha as its
    # sourceCommit, and was assembled after the .work tree was created (a stale manifest of an earlier run
    # of the same sha does not vouch for a later, failed re-run of it).
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BuildDir,
        [Parameter(Mandatory = $true)][string]$Sha,
        [Parameter(Mandatory = $true)][DateTime]$TreeCreatedUtc
    )

    $manifestPath = Join-Path $BuildDir "playback-attr-3-cuda-$Sha-build.json"
    if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) { return "no published build.json for $Sha" }
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
        $commit = [string]$manifest.sourceCommit
        if ($commit -notmatch '^[0-9a-f]{40}$' -or -not $commit.StartsWith($Sha, [StringComparison]::Ordinal)) {
            return "build.json names sourceCommit '$commit', not $Sha"
        }
        # PowerShell 7 hands an ISO timestamp back as a [DateTime] already; Windows PowerShell 5.1 as text.
        $stamp = $manifest.assembledAtUtc
        if ($stamp -is [DateTime]) {
            $assembled = $stamp.ToUniversalTime()
        } else {
            $styles = [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::AdjustToUniversal
            $assembled = [DateTime]::Parse([string]$stamp, [Globalization.CultureInfo]::InvariantCulture, $styles)
        }
        if ($assembled -lt $TreeCreatedUtc) { return "build.json (assembled $($assembled.ToString('o'))) predates this tree" }
    } catch {
        return "build.json unreadable: $($_.Exception.Message)"
    }
    return $null
}

function Test-AttrCudaTrackedFixture {
    # $true ONLY for a byte-identical tracked fixture; any doubt is $false (the tree stays).
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Tree,
        [Parameter(Mandatory = $true)][string]$FullName,
        [string]$RepoRoot = ''
    )

    try {
        if ($RepoRoot -eq '') { return $false }
        $root = $Tree.TrimEnd('\')
        $leaf = Split-Path -Leaf $root
        if ($leaf -notmatch $script:WorkTreeNameRx) { return $false }
        if (-not $FullName.StartsWith($root + '\src\tests\fixtures\', [StringComparison]::OrdinalIgnoreCase)) { return $false }
        $sha = $leaf.Substring(6)
        $rel = $FullName.Substring($root.Length + 5) -replace '\\', '/'
        $blob = @(& git.exe -C $RepoRoot rev-parse --verify --quiet "${sha}:${rel}" 2>$null)
        if ($LASTEXITCODE -ne 0 -or $blob.Count -ne 1 -or "$($blob[0])".Trim() -notmatch '^[0-9a-f]{40}([0-9a-f]{24})?$') { return $false }
        $id = "$($blob[0])".Trim()
        $kind = @(& git.exe -C $RepoRoot cat-file -t $id 2>$null)
        if ($LASTEXITCODE -ne 0 -or $kind.Count -ne 1 -or "$($kind[0])".Trim() -ne 'blob') { return $false }
        $hash = @(& git.exe -C $RepoRoot hash-object --no-filters -- $FullName 2>$null)
        if ($LASTEXITCODE -ne 0 -or $hash.Count -ne 1) { return $false }
        return ("$($hash[0])".Trim() -eq $id)
    } catch { return $false }
}

function Test-AttrCudaContainerMagic {
    # $true = the first four bytes are the container magic OR the file cannot be read (keep); reads 4 bytes only.
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$Path)

    $stream = $null
    try {
        $stream = [IO.FileStream]::new($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $head = New-Object byte[] 4
        if ($stream.Read($head, 0, 4) -lt 4) { return $false }
        for ($k = 0; $k -lt 4; $k++) { if ($head[$k] -ne $script:ContainerMagic[$k]) { return $false } }
        return $true
    } catch { return $true } finally { if ($stream) { $stream.Dispose() } }
}

function Get-AttrCudaTreeGuardReason {
    # $null when no file of the tree needs the tree kept; otherwise why it is kept. Every file is looked at
    # whatever its name or place; reparse points are not entered.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Tree,
        [long]$MaxFileBytes = 2GB,
        [string]$RepoRoot = ''
    )

    $root = $Tree.TrimEnd('\')
    $pending = [System.Collections.Generic.Stack[string]]::new()
    $pending.Push($root)
    while ($pending.Count -gt 0) {
        $dir = $pending.Pop()
        try {
            $entries = @(([IO.DirectoryInfo]$dir).EnumerateFileSystemInfos())
        } catch {
            return "unreadable directory $dir"
        }
        foreach ($entry in $entries) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            if ($entry.Attributes -band [IO.FileAttributes]::Directory) { $pending.Push($entry.FullName); continue }
            if ($entry.Length -gt $MaxFileBytes) { return "file over $MaxFileBytes bytes: $($entry.FullName)" }
            if ($entry.Length -lt 4) { continue }
            if ((Test-AttrCudaContainerMagic -Path $entry.FullName) -and
                -not (Test-AttrCudaTrackedFixture -Tree $root -FullName $entry.FullName -RepoRoot $RepoRoot)) {
                return "container magic or unreadable file: $($entry.FullName)"
            }
        }
    }
    return $null
}

function Get-AttrCudaWorkEvidenceFiles {
    # The evidence set of one .work-<sha12> tree: [pscustomobject]@{ Rel; Full; Length }.
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][string]$WorkDir)

    $root = (Get-Item -LiteralPath $WorkDir -Force).FullName.TrimEnd('\')
    $options = [IO.EnumerationOptions]::new()
    $options.RecurseSubdirectories = $true
    $options.IgnoreInaccessible = $true
    $options.AttributesToSkip = [IO.FileAttributes]::ReparsePoint
    $found = [System.Collections.Generic.List[object]]::new()
    foreach ($file in ([IO.DirectoryInfo]$root).EnumerateFiles('*', $options)) {
        $rel = $file.FullName.Substring($root.Length + 1)
        $top = $rel.Split('\')[0]
        if ($rel.Contains('\') -and ($script:EvidenceSkipTopLevel -contains $top)) { continue }
        $isEvidence = $false
        foreach ($pattern in $script:EvidenceFilePatterns) { if ($file.Name -like $pattern) { $isEvidence = $true; break } }
        if (-not $isEvidence) { continue }
        [void]$found.Add([pscustomobject]@{ Rel = $rel; Full = $file.FullName; Length = [long]$file.Length })
    }
    return $found.ToArray()
}

function Copy-AttrCudaWorkEvidence {
    <#
    .SYNOPSIS
    Copy the evidence of one .work-<sha12> tree to an evidence directory and VERIFY the copy.
    .DESCRIPTION
    Returns { Ok; Files; Bytes; Missing; EvidenceDir }. Ok is true only when every evidence file has a
    destination of the same length and the destination file count and byte total equal the source's. A
    tree with no evidence files is Ok with Files = 0 and creates no directory. Nothing is deleted here.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$WorkDir,
        [Parameter(Mandatory = $true)][string]$EvidenceDir
    )

    $files = @(Get-AttrCudaWorkEvidenceFiles -WorkDir $WorkDir)
    $expectedBytes = [long]0
    foreach ($file in $files) { $expectedBytes += $file.Length }
    if ($files.Count -eq 0) {
        return [pscustomobject]@{ Ok = $true; Files = 0; Bytes = [long]0; Missing = @(); EvidenceDir = $EvidenceDir }
    }

    $missing = [System.Collections.Generic.List[string]]::new()
    $copiedFiles = 0
    $copiedBytes = [long]0
    foreach ($file in $files) {
        $destination = Join-Path $EvidenceDir $file.Rel
        try {
            $parent = [IO.Path]::GetDirectoryName($destination)
            if (-not (Test-Path -LiteralPath $parent)) { [void](New-Item -ItemType Directory -Path $parent -Force) }
            Copy-Item -LiteralPath $file.Full -Destination $destination -Force -ErrorAction Stop
            $copy = Get-Item -LiteralPath $destination -Force -ErrorAction Stop
            if ($copy.Length -ne $file.Length) { [void]$missing.Add("$($file.Rel) (length $($copy.Length) != $($file.Length))"); continue }
            $copiedFiles++
            $copiedBytes += $copy.Length
        } catch {
            [void]$missing.Add("$($file.Rel) ($($_.Exception.Message))")
        }
    }
    $ok = ($missing.Count -eq 0 -and $copiedFiles -eq $files.Count -and $copiedBytes -eq $expectedBytes)
    return [pscustomobject]@{ Ok = $ok; Files = $copiedFiles; Bytes = $copiedBytes; Missing = @($missing); EvidenceDir = $EvidenceDir }
}

function Invoke-AttrCudaWorkRetention {
    <#
    .SYNOPSIS
    Keep the newest .work-<sha12> tree; for each superseded one, copy and verify its evidence, then
    drop the tree through the ownership journal.
    .DESCRIPTION
    A build leg gives every head iteration its OWN build dir (build-ctl, build-head, build-head2, ...) under
    one run directory, so a keep-newest rule scoped to a single build dir would never prune anything.
    -IncludeSiblingBuildDirs widens the scope to every build* sibling of -BuildDir when -BuildDir is itself
    a build* directory directly under a lane-* or review-* run directory; the newest tree of that whole run
    is kept.
    Protected, never dropped: the newest tree, the tree named by -KeepSha (the head just built), and any
    tree written within -MinAgeMinutes (another leg of the same run may be building into it right now).
    Each build dir's own journal is <that dir>\<leaf of -OwnedJournal>; -OwnedJournal is the proof for
    -BuildDir itself. -DryRun reports what would happen and changes nothing, not even the evidence copy.
    Returns one { Build; Sha; Action; WorkBytes; FreedBytes; EvidenceFiles; EvidenceBytes; Detail } per
    superseded tree. Action is one of: dropped, would-drop, kept-unpublished, kept-guard,
    kept-copy-failed, kept-partial, kept-error.
    Only a PUBLISHED head is a candidate (Test-AttrCudaWorkPublished); an unpublished tree is kept whole
    (kept-unpublished). A candidate whose files carry container magic, or exceed -MaxFileBytes, is kept
    whole (kept-guard) unless the file is a tracked fixture of -RepoRoot (Test-AttrCudaTrackedFixture;
    without -RepoRoot there is no exemption).
    The drop is Remove-AttrCudaTree -OwnedJournal: it deletes only what the journal proves, never
    recurses by pathname, and leaves the tree standing when any entry is unproven.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$BuildDir,
        [Parameter(Mandatory = $true)][string]$OwnedJournal,
        [ValidatePattern('^([0-9a-f]{12})?$')][string]$KeepSha = '',
        [switch]$IncludeSiblingBuildDirs,
        [int]$MinAgeMinutes = 120,
        [long]$MaxFileBytes = 2GB,
        [string]$RepoRoot = '',
        [switch]$DryRun
    )

    $build = Get-Item -LiteralPath $BuildDir -Force -ErrorAction Stop
    if (-not $build.PSIsContainer -or ($build.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "ATTRCUDA_RETENTION_BAD_BUILD_DIR $BuildDir is not a plain directory"
    }
    $buildFull = $build.FullName.TrimEnd('\')
    $journalLeaf = Split-Path -Leaf $OwnedJournal
    $scope = @($buildFull)
    if ($IncludeSiblingBuildDirs) {
        $runDir = Split-Path -Parent $buildFull
        if ((Split-Path -Leaf $buildFull) -like 'build*' -and (Split-Path -Leaf $runDir) -match '^(lane|review)-') {
            $scope = @(Get-ChildItem -LiteralPath $runDir -Directory -Force |
                Where-Object { $_.Name -like 'build*' -and -not ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) } |
                ForEach-Object { $_.FullName.TrimEnd('\') })
        }
    }
    $found = [System.Collections.Generic.List[object]]::new()
    foreach ($dir in $scope) {
        foreach ($candidate in Get-ChildItem -LiteralPath $dir -Directory -Force) {
            if ($candidate.Name -match $script:WorkTreeNameRx -and -not ($candidate.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                [void]$found.Add($candidate)
            }
        }
    }
    $trees = @($found | Sort-Object LastWriteTimeUtc -Descending)
    $results = [System.Collections.Generic.List[object]]::new()
    if ($trees.Count -le 1) { return $results.ToArray() }

    $newestFull = $trees[0].FullName
    $youngest = [DateTime]::UtcNow.AddMinutes(-$MinAgeMinutes)
    foreach ($tree in $trees) {
        if ($tree.FullName -eq $newestFull) { continue }
        if ($KeepSha -ne '' -and $tree.Name -eq ".work-$KeepSha") { continue }
        if ($tree.LastWriteTimeUtc -gt $youngest) { continue }
        $sha = $tree.Name.Substring(6)
        $treeBuildDir = (Split-Path -Parent $tree.FullName).TrimEnd('\')
        $treeJournal = if ($treeBuildDir -eq $buildFull) { $OwnedJournal } else { Join-Path $treeBuildDir $journalLeaf }
        $before = Get-AttrCudaTreeBytes -Path $tree.FullName
        $result = [ordered]@{ Build = (Split-Path -Leaf $treeBuildDir); Sha = $sha; Action = ''; WorkBytes = $before; FreedBytes = [long]0; EvidenceFiles = 0; EvidenceBytes = [long]0; Detail = '' }
        try {
            $notPublished = Test-AttrCudaWorkPublished -BuildDir $treeBuildDir -Sha $sha -TreeCreatedUtc $tree.CreationTimeUtc
            $guard = if ($notPublished) { $null } else { Get-AttrCudaTreeGuardReason -Tree $tree.FullName -MaxFileBytes $MaxFileBytes -RepoRoot $RepoRoot }
            if ($notPublished) {
                $result.Action = 'kept-unpublished'
                $result.Detail = $notPublished
            } elseif ($guard) {
                $result.Action = 'kept-guard'
                $result.Detail = $guard
            } elseif ($DryRun) {
                $planned = @(Get-AttrCudaWorkEvidenceFiles -WorkDir $tree.FullName)
                $result.EvidenceFiles = $planned.Count
                $result.EvidenceBytes = [long](($planned | Measure-Object -Property Length -Sum).Sum)
                $result.Action = 'would-drop'
            } else {
                $copy = Copy-AttrCudaWorkEvidence -WorkDir $tree.FullName -EvidenceDir (Join-Path $treeBuildDir "evidence-$sha")
                $result.EvidenceFiles = $copy.Files
                $result.EvidenceBytes = $copy.Bytes
            }
            if ($result.Action -ne '') {
                # decided without touching the tree: kept-unpublished, kept-guard, or a dry run's would-drop
            } elseif (-not $copy.Ok) {
                $result.Action = 'kept-copy-failed'
                $result.Detail = (@($copy.Missing) | Select-Object -First 3) -join '; '
            } else {
                $drop = Remove-AttrCudaTree -TrustedRoot $treeBuildDir -Path $tree.FullName -OwnedJournal $treeJournal
                $after = if (Test-Path -LiteralPath $tree.FullName) { Get-AttrCudaTreeBytes -Path $tree.FullName } else { [long]0 }
                $result.FreedBytes = [Math]::Max([long]0, $before - $after)
                if ($drop.TreeRemoved) {
                    $result.Action = 'dropped'
                } else {
                    $result.Action = 'kept-partial'
                    $result.Detail = (@($drop.Left | ForEach-Object { $_.Token } | Group-Object | ForEach-Object { "$($_.Name)=$($_.Count)" })) -join ','
                }
            }
        } catch {
            $result.Action = 'kept-error'
            $result.Detail = $_.Exception.Message
        }
        $line = [pscustomobject]$result
        [void]$results.Add($line)
        Write-Host "[attr3-retention] $($line.Action) $($line.Build)\.work-$sha work=$($line.WorkBytes) freed=$($line.FreedBytes) evidenceFiles=$($line.EvidenceFiles) evidenceBytes=$($line.EvidenceBytes) $($line.Detail)"
    }
    return $results.ToArray()
}

Export-ModuleMember -Function `
    Get-AttrCudaTreeBytes, `
    Test-AttrCudaWorkPublished, `
    Test-AttrCudaTrackedFixture, `
    Test-AttrCudaContainerMagic, `
    Get-AttrCudaTreeGuardReason, `
    Get-AttrCudaWorkEvidenceFiles, `
    Copy-AttrCudaWorkEvidence, `
    Invoke-AttrCudaWorkRetention
