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
    superseded tree. Action is one of: dropped, would-drop, kept-copy-failed, kept-partial, kept-error.
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
            if ($DryRun) {
                $planned = @(Get-AttrCudaWorkEvidenceFiles -WorkDir $tree.FullName)
                $result.EvidenceFiles = $planned.Count
                $result.EvidenceBytes = [long](($planned | Measure-Object -Property Length -Sum).Sum)
                $result.Action = 'would-drop'
            } else {
                $copy = Copy-AttrCudaWorkEvidence -WorkDir $tree.FullName -EvidenceDir (Join-Path $treeBuildDir "evidence-$sha")
                $result.EvidenceFiles = $copy.Files
                $result.EvidenceBytes = $copy.Bytes
            }
            if ($DryRun) {
                # nothing is copied or dropped in a dry run
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
    Get-AttrCudaWorkEvidenceFiles, `
    Copy-AttrCudaWorkEvidence, `
    Invoke-AttrCudaWorkRetention
