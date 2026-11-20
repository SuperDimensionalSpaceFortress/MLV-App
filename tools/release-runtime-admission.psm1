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

# Adversary b5fce5a9-A1: Kill($true) finds descendants by comparing LOCAL start times, so a child started just after a DST
# fall-back fold reads as older than its parent and is left running while the root's exit reads as confirmed. The tree is
# therefore also derived here from parent ids and creation times. Rows: { id; parentId; startUtc } (startUtc $null = unknown).
# Adversary 0f5308ad-A1 (r6): ONE Win32_Process query reads each row's pid, parent id AND creation time from the same snapshot. The
# earlier join of a parent-id read with a later GetProcesses start-time read, by pid, let a pid reused between the two reads inherit a
# stale parent. startUtc is the UTC creation time cut to whole microseconds (the CIM datetime's precision), so it compares equal to the
# same process's creation time read through its own handle (ConvertTo-PinnedPayloadCreationTicks). The query is bounded.
# TimeoutSec bounds the query; the stop passes what is left of its one budget (adversary d7942d7f-A2).
function Get-PinnedPayloadProcessTable {
    param([ValidateRange(1, 30)][int]$TimeoutSec = 30)
    foreach ($row in @(Get-CimInstance -ClassName Win32_Process -Property ProcessId, ParentProcessId, CreationDate -OperationTimeoutSec $TimeoutSec -ErrorAction Stop)) {
        $ticks = ConvertTo-PinnedPayloadCreationTicks $row.CreationDate
        [pscustomobject]@{ id = [int]$row.ProcessId; parentId = [int]$row.ParentProcessId; startUtc = if ($null -eq $ticks) { $null } else { [datetime]::new($ticks, [System.DateTimeKind]::Utc) } }
    }
}

# A creation time as UTC ticks cut to whole microseconds; $null when it is not a readable time. Identity (pid + this value) is compared
# for EQUALITY only; it is never used to order two processes.
function ConvertTo-PinnedPayloadCreationTicks {
    param($Value)
    if ($Value -isnot [datetime]) { return $null }
    $ticks = $Value.ToUniversalTime().Ticks
    return [long]($ticks - ($ticks % 10))
}

# The tree as ONE listing shows it (r6 property 1). The root joins only as the row on its pid whose creation time equals the one read
# through the root's own handle; with no such row nothing is proven (key3 r5 B1: a root that already exited, or a pid reused by another
# process, must not lend its id to a stale parent id). Every row reachable from the root by parent id IN THIS SAME LISTING is then
# tracked, whatever its creation time: a row that reads older than its parent (an older process holding a reused parent id, or a real
# child after a backward clock step: adversary 0f5308ad-A3) cannot be placed outside without the wall clock, and neither can the rows
# under it (adversary d7942d7f-A1). Tracking only ever COUNTS a row; nothing tracked is killed by pid. Returns { rootListed; members
# (id -> creation ticks, or $null when unreadable) }.
function Get-PinnedPayloadTree {
    param([object[]]$Table, [int]$RootId, $RootTicks)
    $members = @{}
    if ($null -ne $RootTicks) {
        foreach ($row in $Table) {
            if ([int]$row.id -eq $RootId -and (ConvertTo-PinnedPayloadCreationTicks $row.startUtc) -eq [long]$RootTicks) { $members[$RootId] = [long]$RootTicks }
        }
    }
    if (-not $members.Count) { return [pscustomobject]@{ rootListed = $false; members = $members } }
    $queue = [System.Collections.Generic.Queue[int]]::new()
    $queue.Enqueue($RootId)
    while ($queue.Count) {
        $parent = $queue.Dequeue()
        foreach ($row in $Table) {
            $id = [int]$row.id
            if ([int]$row.parentId -ne $parent -or $id -eq $parent -or $members.ContainsKey($id)) { continue }
            $members[$id] = ConvertTo-PinnedPayloadCreationTicks $row.startUtc
            $queue.Enqueue($id)
        }
    }
    return [pscustomobject]@{ rootListed = $true; members = $members }
}

# Every row of ONE later listing that may still be this tree's (r6 property 2: no wall-clock ordering ever places a row outside):
# a member still listed with its own creation time, or with one that cannot be read; then, transitively, every row whose parent id is a
# member's or an already counted row's, whatever its creation time (a child started after the first listing, a row the first listing
# could not place, or a child of a process that reused a member's id: no clock-free fact tells them apart). A row on a member's id with
# a DIFFERENT readable creation time is another process (183fe59d-A1) and is not itself counted.
function Get-PinnedPayloadSurvivors {
    param([object[]]$Table, [hashtable]$Members)
    $survivors = [System.Collections.Generic.List[object]]::new()
    $counted = [System.Collections.Generic.HashSet[int]]::new()
    $parents = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($id in @($Members.Keys)) { [void]$parents.Add([int]$id) }
    foreach ($row in $Table) {
        $id = [int]$row.id
        if (-not $Members.ContainsKey($id)) { continue }
        $ticks = ConvertTo-PinnedPayloadCreationTicks $row.startUtc
        # Either creation time unreadable: nothing proves it is another process, so it counts.
        if ($null -eq $ticks -or $null -eq $Members[$id] -or $ticks -eq $Members[$id]) { if ($counted.Add($id)) { $survivors.Add($row) } }
    }
    do {
        $grew = $false
        foreach ($row in $Table) {
            $id = [int]$row.id
            if ($counted.Contains($id) -or $id -eq [int]$row.parentId) { continue }
            if ($parents.Contains([int]$row.parentId) -or $counted.Contains([int]$row.parentId)) {
                [void]$counted.Add($id)
                $survivors.Add($row)
                $grew = $true
            }
        }
    } while ($grew)
    # Unrolled on purpose: the caller wraps the output in @(), so an empty result is an empty array, never one empty element.
    return $survivors.ToArray()
}

# Kills a timed-out child's whole tree and reports whether its exit was SEEN; a Kill that throws (access
# denied) or a wait that times out is never reported as killed (adversary 32bf39a9-A3).
function Stop-PinnedPayloadChild {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        $Process,

        [ValidateRange(0, 600000)]
        [int]$ConfirmMs = 10000,

        # Called with one argument: the listing's time bound in whole seconds (1..30), what is left of ConfirmMs, rounded down.
        [scriptblock]$ProcessTable = { param([int]$TimeoutSec) Get-PinnedPayloadProcessTable -TimeoutSec $TimeoutSec }
    )
    # One monotonic clock bounds every wait of this stop (r6 property 2); no wall-clock reading decides anything here. A listing starts
    # only while at least one whole second (the CIM bound's granularity) is left, and is bounded to the whole seconds left, rounded DOWN
    # (adversary df7e3c66-A2); a tree seen gone only after the budget is spent is not confirmed (adversary d7942d7f-A2).
    $clock = [System.Diagnostics.Stopwatch]::StartNew()
    $listingSec = { [int][math]::Min(30, [math]::Floor(($ConfirmMs - $clock.ElapsedMilliseconds) / 1000.0)) }
    # The root's identity is read through the handle this caller holds, never from a listing (key3 r5 B1, psm1:108 sibling).
    $treeError = ''
    $rootTicks = $null
    try { $rootTicks = ConvertTo-PinnedPayloadCreationTicks $Process.StartTime } catch { $rootTicks = $null }
    $members = @{}
    if ($null -eq $rootTicks) {
        $treeError = "the root's creation time could not be read through its handle, so no listing can prove its tree"
    } else {
        # The tree as ONE listing proves it while the root is alive (adversary b5fce5a9-A1, 0f5308ad-A1): a listing that fails, or one
        # without the root's own row, leaves the kill unconfirmed.
        $bound = & $listingSec
        if ($bound -lt 1) {
            $treeError = "no whole second of the $ConfirmMs ms confirmation budget is left to list the tree before the kill"
        } else {
            try {
                $tree = Get-PinnedPayloadTree -Table @(& $ProcessTable $bound) -RootId ([int]$Process.Id) -RootTicks $rootTicks
                $members = $tree.members
                if (-not $tree.rootListed) { $treeError = "pid $($Process.Id) with its own creation time is not in the listing taken before the kill, so its tree is unproven" }
            } catch { $treeError = "the process tree could not be listed before the kill: $($_.Exception.Message)" }
        }
    }
    # The ONLY kill: Kill($true) through the root's own held handle. Nothing is ever killed by a pid read from a listing (adversary
    # 0f5308ad-A1/A2: a listing can join one process's parent id to another's identity, and an open by pid can reach a replacement); a
    # descendant Kill($true) misses is counted below and leaves the kill unconfirmed, so no recovery runs while it may live.
    $killError = ''
    try { $null = $Process.Kill($true) } catch { $killError = $_.Exception.Message }
    $exited = $false
    try { $exited = [bool]$Process.WaitForExit([int][math]::Max(0, $ConfirmMs - $clock.ElapsedMilliseconds)) } catch { if (-not $killError) { $killError = "the exit could not be awaited: $($_.Exception.Message)" } }
    if (-not $treeError) {
        try {
            # Re-listed until nothing that may be the tree's is listed or the one budget is spent (a terminated process can stay
            # listed for a moment); whatever is still listed then leaves the tree unconfirmed.
            # Every row a poll counts joins the tracked set with its identity, so a later poll still counts its subtree after it exits
            # (adversary df7e3c66-A1). No listing starts with less than one whole second left.
            $left = @()
            $listed = $false
            while ($true) {
                $bound = & $listingSec
                if ($bound -lt 1) { break }
                $left = @(Get-PinnedPayloadSurvivors -Table @(& $ProcessTable $bound) -Members $members)
                $listed = $true
                foreach ($row in $left) { if (-not $members.ContainsKey([int]$row.id)) { $members[[int]$row.id] = ConvertTo-PinnedPayloadCreationTicks $row.startUtc } }
                if (-not $left.Count) { break }
                [System.Threading.Thread]::Sleep([int][math]::Max(0, [math]::Min(100, $ConfirmMs - $clock.ElapsedMilliseconds)))
            }
            if (-not $listed) { $treeError = "no whole second of the $ConfirmMs ms confirmation budget was left to list the tree after the kill" }
            elseif ($left.Count) { $treeError = "pid(s) $(@($left | ForEach-Object { $_.id }) -join ', ') still running after the tree kill (a member, or a row on a member's parent id that no clock-free fact places outside the tree)" }
            elseif ($clock.ElapsedMilliseconds -gt $ConfirmMs) { $treeError = "the tree was seen gone only after the $ConfirmMs ms confirmation budget was spent ($($clock.ElapsedMilliseconds) ms)" }
        } catch { $treeError = "the process tree could not be listed after the kill: $($_.Exception.Message)" }
    }
    if ($treeError) { $killError = if ($killError) { "$killError; $treeError" } else { $treeError } }
    # The ROOT's exit is not the tree's: a Kill(true) that threw (a descendant it could not end) leaves the tree
    # unconfirmed even when the root exited (adversary 6627beef-A1).
    return [pscustomobject][ordered]@{ exited = $exited; killError = $killError; confirmed = ($exited -and -not $killError) }
}

# Puts back every original a killed extractor left in ITS transaction folder (extract_vendored_native_payload.py
# _publish_staged_transaction moves each existing destination into .vendored-payload-transaction-<TransactionId>\<name> before it
# installs the replacement). Only that exact folder, named by the caller's --transaction-id, is touched: another install sharing
# the output folder keeps its own (adversary 78d626ed-A1). Moves only, never a delete and never an overwrite: a backup goes back
# only where the destination is absent; one whose destination is occupied stays in the folder and is named in .errors
# (adversary 32501f50-A1). The folder is left for the failed build to report.
# A folder or file that cannot be listed or moved is an error in the result, never silence (adversary 78d626ed-A3).
# Never throws; returns { restored, errors, found }.
function Restore-PinnedPayloadTransaction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)]
        [ValidateNotNullOrEmpty()]
        [string]$OutputDir,

        [Parameter(Mandatory)]
        [ValidatePattern('^[0-9a-f]{32}\z')]
        [string]$TransactionId
    )
    $restored = @()
    $errors = @()
    $folder = "$OutputDir\.vendored-payload-transaction-$TransactionId"
    try {
        # Inside the try: a path Join-Path cannot resolve (a missing drive) is a typed error, never a throw (adversary 9bd36192-A4).
        $folder = Join-Path $OutputDir ".vendored-payload-transaction-$TransactionId" -ErrorAction Stop
        $item = Get-Item -LiteralPath $folder -Force -ErrorAction Stop
    } catch [System.Management.Automation.ItemNotFoundException] {
        # Killed before its transaction began: no original was moved.
        return [pscustomobject][ordered]@{ restored = @(); errors = @(); found = $false }
    } catch {
        return [pscustomobject][ordered]@{ restored = @(); errors = @("$folder could not be read: $($_.Exception.Message)"); found = $true }
    }
    if (-not $item.PSIsContainer -or $item.LinkType) {
        return [pscustomobject][ordered]@{ restored = @(); errors = @("$folder is not a plain folder; nothing restored"); found = $true }
    }
    try {
        $backups = @(Get-ChildItem -LiteralPath $folder -Force -ErrorAction Stop)
    } catch {
        return [pscustomobject][ordered]@{ restored = @(); errors = @("$folder could not be listed: $($_.Exception.Message)"); found = $true }
    }
    foreach ($backup in $backups) {
        if ($backup.PSIsContainer -or $backup.LinkType) { $errors += "$($backup.FullName) is not a plain file; not restored"; continue }
        $destination = Join-Path $OutputDir $backup.Name
        # Adversary 32501f50-A1: a backup goes back ONLY onto an absent destination. A file there now is this install's own
        # replacement or a concurrent install's verified file; either way it is left alone and the original stays in the backup.
        # The non-overwriting Move is the atomic arbiter (it refuses a destination that appears after the check).
        if ([System.IO.File]::Exists($destination) -or [System.IO.Directory]::Exists($destination)) {
            $errors += "$($backup.Name): destination present; backup kept at $($backup.FullName)"
            continue
        }
        try {
            [System.IO.File]::Move($backup.FullName, $destination, $false)
            $restored += $backup.Name
        } catch {
            if ([System.IO.File]::Exists($destination) -or [System.IO.Directory]::Exists($destination)) {
                $errors += "$($backup.Name): destination present; backup kept at $($backup.FullName)"
            } else {
                $errors += "$($backup.Name): $($_.Exception.Message); backup kept at $($backup.FullName)"
            }
        }
    }
    return [pscustomobject][ordered]@{ restored = $restored; errors = $errors; found = $true }
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
    # --transaction-id (adversary 78d626ed-A1): this child's publication transaction folder is named by this call, so a timeout
    # recovery below restores exactly this child's originals and never another install's sharing the folder.
    $transactionId = [guid]::NewGuid().ToString('N')
    foreach ($argument in @('-3', '-m', 'tools.repo_hygiene.extract_vendored_native_payload', '--repo-root', $RepoRoot, '--payload-id', $PayloadId, '--archive-reference', $ArchiveReference, '--output-dir', $OutputDir, '--transaction-id', $transactionId, '--record-only', '--verify-installed')) {
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
            $restore = Restore-PinnedPayloadTransaction -OutputDir $OutputDir -TransactionId $transactionId
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
        # A relative path means the PowerShell location, as it does for the cmdlets that installed and read the file; .NET alone
        # would resolve it against the process directory (adversary 78d626ed-A2).
        $Path = $PSCmdlet.GetUnresolvedProviderPathFromPSPath($Path)
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
        # Relative = the PowerShell location, as for Compress-Archive that wrote it (adversary 78d626ed-A2).
        $ZipPath = $PSCmdlet.GetUnresolvedProviderPathFromPSPath($ZipPath)
        $stream = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        $check.zipSha256 = [System.Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($stream)).ToLowerInvariant()
        $null = $stream.Seek(0, [System.IO.SeekOrigin]::Begin)
        $zip = [System.IO.Compression.ZipArchive]::new($stream, [System.IO.Compression.ZipArchiveMode]::Read, $true)
        # Adversary 9bd36192-A1: Expand-Archive resolves an entry name the way Windows resolves a path, so ./ffmpeg.exe,
        # sub/../ffmpeg.exe, ffmpeg.exe. or an 8.3 alias extracts OVER the root ffmpeg.exe. Every entry name must therefore be
        # canonical (both separators accepted; no empty, ., .. or trailing-dot/space segment, no ~<digit> short-name form, no
        # stream or reserved character), and no two entries may name one extracted path (case-insensitive). Only then does
        # "exactly one entry named MemberName" mean the extracted file is that entry.
        $entries = @()
        $seen = [System.Collections.Generic.Dictionary[string, string]]::new([StringComparer]::OrdinalIgnoreCase)
        foreach ($entry in $zip.Entries) {
            $raw = [string]$entry.FullName
            $name = $raw.Replace('\', '/')
            $isDirectory = $name.EndsWith('/')
            $path = if ($isDirectory) { $name.Substring(0, $name.Length - 1) } else { $name }
            $badSegments = @($path.Split('/') | Where-Object { $_ -eq '' -or $_ -eq '.' -or $_ -eq '..' -or $_ -match '[. ]\z' -or $_ -match '~[0-9]' -or $_ -match '[:*?"<>|\x00-\x1f]' })
            if ($path.Length -eq 0 -or $badSegments.Count -gt 0) {
                $check.reason = "$ZipPath entry '$raw' is not a canonical entry name (it could extract over another entry)"
                return [pscustomobject]$check
            }
            if ($seen.ContainsKey($path)) {
                $check.reason = "$ZipPath entries '$($seen[$path])' and '$raw' name one extracted path"
                return [pscustomobject]$check
            }
            $seen[$path] = $raw
            if (-not $isDirectory -and [string]::Equals($path, $MemberName, [StringComparison]::OrdinalIgnoreCase)) { $entries += $entry }
        }
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
