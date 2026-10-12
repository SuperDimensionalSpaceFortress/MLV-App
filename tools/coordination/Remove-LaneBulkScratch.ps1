<#
.SYNOPSIS
    Remove the bulk scratch a lane registered, after copying its small text receipts; refuse anything unsafe.

.DESCRIPTION
    Dot-source this file and call Invoke-RemoveLaneBulkScratch. It never throws: every outcome is
    returned as a disposition object so Invoke-Lane.ps1 can put it in the receipt.

    Origin (LANE-RECEIPT-SCRATCH-CLEANUP-1, 2026-10-12): Invoke-Lane.ps1 removes only its own
    -ScratchRoot dir, and Invoke-MlvtmpRetention.ps1 files every other non-worktree dir under
    C:\mlvtmp as kind=other / keep, so lane-authored asm, shadow-build and probe dirs (4-5 GiB in
    one day) were never removed by anything.

    CONTRACT: a lane lists each bulk path it creates, one absolute path per line, in
    <RunDir>\scratch-paths.txt (blank lines and lines starting with # are ignored; the launcher
    hands the path to the child as MLV_LANE_SCRATCH_PATHS). After the lane exits, each listed path
    gets its small text files (*.txt, *.json, *.log, under 1 MiB) copied to
    <RunDir>\scratch-receipts\<n>-<leaf>\ and is then removed.

    HARD REFUSALS (recorded per path, never thrown; every probe that cannot answer refuses):
      relative-path / dotdot / short-name-alias   the path is not a plain absolute path
      outside-root                                not strictly inside -Root (default C:\mlvtmp)
      lane-scratch                                is, contains, or is inside -LaneScratchRoot
      workdir                                     is, contains, or is inside -WorkDir
      git-worktree                                is or contains a git worktree (registered, or a .git marker)
      worktree-list-unavailable                   `git worktree list` could not be read
      reparse-point                               the path, an ancestor below -Root, or anything inside it
      producer-pr-open / pr-state-unknown         the lane's branch has an open PR, or that could not be learned
      copy-failed                                 a small file could not be copied out, so nothing is removed
    A path that is already gone is recorded `absent`. -Keep records `kept` / `opt-out` for every path.
#>

function Test-LaneScratchSame([string]$A, [string]$B) {
    return ($A.TrimEnd('\') -ieq $B.TrimEnd('\'))
}
function Test-LaneScratchInside([string]$Child, [string]$Parent) {
    $p = $Parent.TrimEnd('\') + '\'
    return $Child.StartsWith($p, [StringComparison]::OrdinalIgnoreCase)
}
function Test-LaneScratchReparse([string]$Path) {
    try { return [bool]((Get-Item -LiteralPath $Path -Force -ErrorAction Stop).Attributes -band [IO.FileAttributes]::ReparsePoint) }
    catch { return $true }   # cannot read attributes -> treat as unsafe
}

function Get-LaneScratchWorktreePath([string]$RepoDir) {
    # $null when git cannot answer; callers refuse in that case.
    try {
        $out = @(& git -C $RepoDir worktree list --porcelain 2>$null)
        if ($LASTEXITCODE -ne 0) { return $null }
        return @($out | Where-Object { $_ -like 'worktree *' } | ForEach-Object { ([string]$_ -replace '^worktree ', '').Replace('/', '\').TrimEnd('\') })
    } catch { return $null }
}

function Get-LaneScratchPrState([string]$WorkDir) {
    # 'open' | 'none' | 'unknown'
    try {
        $branch = @(& git -C $WorkDir branch --show-current 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $branch.Count -or -not [string]$branch[0]) { return 'none' }   # detached / no branch: no PR to protect
        $json = & gh pr list --repo SuperDimensionalSpaceFortress/MLV-App --head ([string]$branch[0]) --state open --json number 2>$null
        if ($LASTEXITCODE -ne 0) { return 'unknown' }
        $rows = @($json | ConvertFrom-Json)
        if ($rows.Count -gt 0) { return 'open' } else { return 'none' }
    } catch { return 'unknown' }
}

function Copy-LaneScratchReceipts([string]$Path, [string]$Dest) {
    # Small text files only; bounded so a 13k-file tree cannot stall the lane exit.
    $maxFiles = 200; $maxTotal = 20MB; $maxEach = 1MB
    $copied = 0; $total = [int64]0; $truncated = $false
    foreach ($f in Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction Stop |
                   Where-Object { $_.Extension -in '.txt', '.json', '.log' -and $_.Length -lt $maxEach }) {
        if ($copied -ge $maxFiles -or ($total + $f.Length) -gt $maxTotal) { $truncated = $true; break }
        $rel = $f.FullName.Substring($Path.TrimEnd('\').Length).TrimStart('\')
        $target = Join-Path $Dest $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $f.FullName -Destination $target -Force -ErrorAction Stop
        $copied++; $total += $f.Length
    }
    return [ordered]@{ files = $copied; bytes = $total; truncated = $truncated }
}

function Invoke-RemoveLaneBulkScratch {
    param(
        [Parameter(Mandatory)][string]$RunDir,
        [Parameter(Mandatory)][string]$WorkDir,
        [string]$Root = 'C:\mlvtmp',
        [string]$LaneScratchRoot = 'C:\mlvtmp\lane-scratch',
        [string]$ManifestName = 'scratch-paths.txt',
        # Test seam: a scriptblock returning 'open' | 'none' | 'unknown'. Default asks gh about the lane's branch.
        [scriptblock]$PrProbe = $null,
        [switch]$Keep
    )
    $manifest = Join-Path $RunDir $ManifestName
    $result = [ordered]@{ manifest = $manifest; optOut = [bool]$Keep; entries = @(); error = $null }
    try {
        if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { return $null }
        $lines = @(Get-Content -LiteralPath $manifest -Encoding utf8 -ErrorAction Stop |
                   ForEach-Object { ([string]$_).Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') } | Select-Object -Unique -First 200)
        $rootFull = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        $workFull = [IO.Path]::GetFullPath($WorkDir).TrimEnd('\')
        $laneScratchFull = if ($LaneScratchRoot) { [IO.Path]::GetFullPath($LaneScratchRoot).TrimEnd('\') } else { $null }
        $worktrees = $null; $worktreesRead = $false; $pr = $null
        $receiptsRoot = Join-Path $RunDir 'scratch-receipts'
        $entries = New-Object System.Collections.Generic.List[object]
        $n = 0
        foreach ($raw in $lines) {
            $n++
            $e = [ordered]@{ path = $raw; action = 'refused'; reason = $null; bytes = $null; copiedFiles = $null; copyTruncated = $null; error = $null }
            $entries.Add($e)
            try {
                if (-not [IO.Path]::IsPathRooted($raw) -or $raw -notmatch '^[A-Za-z]:\\') { $e.reason = 'relative-path'; continue }
                if ($raw -match '(^|\\)\.\.(\\|$)') { $e.reason = 'dotdot'; continue }
                if ($raw -match '~\d') { $e.reason = 'short-name-alias'; continue }
                $full = [IO.Path]::GetFullPath($raw).TrimEnd('\')
                if (-not (Test-LaneScratchInside $full $rootFull)) { $e.reason = 'outside-root'; continue }
                if ($laneScratchFull -and ((Test-LaneScratchSame $full $laneScratchFull) -or (Test-LaneScratchInside $full $laneScratchFull) -or (Test-LaneScratchInside $laneScratchFull $full))) { $e.reason = 'lane-scratch'; continue }
                if ((Test-LaneScratchSame $full $workFull) -or (Test-LaneScratchInside $full $workFull) -or (Test-LaneScratchInside $workFull $full)) { $e.reason = 'workdir'; continue }
                if (-not (Test-Path -LiteralPath $full)) { $e.action = 'absent'; $e.reason = 'not-found'; continue }
                # Ancestors strictly between -Root and the path must not be reparse points either.
                $anc = Split-Path -Parent $full; $ancBad = $false
                while ($anc -and (Test-LaneScratchInside $anc $rootFull)) { if (Test-LaneScratchReparse $anc) { $ancBad = $true; break }; $anc = Split-Path -Parent $anc }
                if ($ancBad -or (Test-LaneScratchReparse $full)) { $e.reason = 'reparse-point'; continue }
                if (-not (Test-Path -LiteralPath $full -PathType Container)) { $e.reason = 'not-a-directory'; continue }
                # Worktree: marker at the root, any registered worktree equal/inside/containing it, or one nested below.
                if (Test-Path -LiteralPath (Join-Path $full '.git')) { $e.reason = 'git-worktree'; continue }
                if (-not $worktreesRead) { $worktrees = Get-LaneScratchWorktreePath $WorkDir; $worktreesRead = $true }
                if ($null -eq $worktrees) { $e.reason = 'worktree-list-unavailable'; continue }
                $isWt = $false
                foreach ($w in $worktrees) { if ((Test-LaneScratchSame $full $w) -or (Test-LaneScratchInside $full $w) -or (Test-LaneScratchInside $w $full)) { $isWt = $true; break } }
                if ($isWt) { $e.reason = 'git-worktree'; continue }
                # One walk: size, reparse points anywhere inside, and nested .git markers. Nothing is followed.
                $bytes = [int64]0; $bad = $null
                foreach ($it in Get-ChildItem -LiteralPath $full -Recurse -Force -ErrorAction Stop) {
                    if ($it.Attributes -band [IO.FileAttributes]::ReparsePoint) { $bad = 'reparse-point'; break }
                    if ($it.Name -eq '.git') { $bad = 'git-worktree'; break }
                    if (-not $it.PSIsContainer) { $bytes += $it.Length }
                }
                $e.bytes = $bytes
                if ($bad) { $e.reason = $bad; continue }
                if ($Keep) { $e.action = 'kept'; $e.reason = 'opt-out'; continue }
                if ($null -eq $pr) { $pr = if ($PrProbe) { [string](& $PrProbe) } else { Get-LaneScratchPrState $WorkDir } }
                if ($pr -eq 'open') { $e.reason = 'producer-pr-open'; continue }
                if ($pr -ne 'none') { $e.reason = 'pr-state-unknown'; continue }
                $dest = Join-Path $receiptsRoot ('{0}-{1}' -f $n, (Split-Path -Leaf $full))
                try {
                    $c = Copy-LaneScratchReceipts $full $dest
                    $e.copiedFiles = $c.files; $e.copyTruncated = $c.truncated
                } catch { $e.reason = 'copy-failed'; $e.error = $_.Exception.Message; continue }
                Remove-Item -LiteralPath $full -Recurse -Force -ErrorAction Stop
                $e.action = 'removed'; $e.reason = $null
            } catch {
                $e.action = 'failed'; $e.reason = 'remove-failed'; $e.error = $_.Exception.Message
            }
        }
        $result['entries'] = $entries.ToArray()
    } catch {
        $result.error = "cannot-determine: $($_.Exception.Message)"
    }
    return $result
}
