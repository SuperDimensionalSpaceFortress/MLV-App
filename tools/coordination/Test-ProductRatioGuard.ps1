[CmdletBinding()]
param(
    [string]$RepoRoot = $(if ($env:MLV_BOARD_ROOT) { $env:MLV_BOARD_ROOT } else { Split-Path -Parent (Split-Path -Parent $PSScriptRoot) }),
    [string]$SourceRef = 'fork/master',
    [long]$AsOfEpoch = 0,
    [string]$ReservationsPath = '',
    [string]$LegacyDispatchPath = '',
    [string]$FleetRunsPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$WindowSeconds = 7 * 24 * 60 * 60
$EmptyTreeSha = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
# Token both ledger writers carry once they append under the shared mutex. Enforcement is bound to it.
$LedgerWriterMarker = 'MLV-DISPATCH-LEDGER-WRITER-V3-SERIALIZED'

function New-Decision {
    param(
        [AllowNull()]$ProductShare,
        [AllowNull()]$DispatchRate,
        [string]$Coverage,
        [bool]$EvidenceAvailable,
        [bool]$ProvenanceComplete,
        [bool]$HasProductLandings,
        [int]$MalformedRows = 0,
        [string[]]$CoverageReasons = @()
    )

    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($null -eq $ProductShare -or [double]$ProductShare -lt 0.50) { $reasons.Add('RED_PRODUCT_SHARE') }
    if ($Coverage -ne 'COMPLETE') {
        $reasons.Add('RED_DISPATCH_COVERAGE_PARTIAL')
        foreach ($code in $CoverageReasons) { $reasons.Add($code) }
    }
    if (-not $EvidenceAvailable) { $reasons.Add('UNAVAILABLE_DISPATCH_EVIDENCE') }
    if (-not $ProvenanceComplete) { $reasons.Add('UNAVAILABLE_LANDING_PROVENANCE') }
    elseif (-not $HasProductLandings) { $reasons.Add('NO_PRODUCT_LANDINGS') }
    elseif ($null -ne $DispatchRate -and [double]$DispatchRate -gt 4.0) { $reasons.Add('RED_DISPATCH_RATE') }
    if ($MalformedRows -gt 0) { $reasons.Add('ERROR_DISPATCH_EVIDENCE_MALFORMED') }

    $green = $null -ne $ProductShare -and
        [double]$ProductShare -ge 0.50 -and
        $Coverage -eq 'COMPLETE' -and
        $EvidenceAvailable -and
        $ProvenanceComplete -and
        $HasProductLandings -and
        $null -ne $DispatchRate -and
        [double]$DispatchRate -le 4.0 -and
        $MalformedRows -eq 0

    return [pscustomobject]@{
        verdict = $(if ($green) { 'GREEN' } else { 'RED' })
        reasons = @($reasons)
    }
}

function Invoke-GitLines {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $lines = @(& git -C $RepoRoot @Arguments 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'git command failed' }
    return @($lines | ForEach-Object { [string]$_ })
}

function Get-CommitInfo {
    param([Parameter(Mandatory)][string]$Line)
    $parts = $line -split "`t", 4
    if ($parts.Count -ne 4 -or $parts[0] -notmatch '^[0-9a-fA-F]{40}$' -or $parts[1] -notmatch '^\d+$') {
        throw 'malformed git history'
    }
    return [pscustomobject]@{
        sha = $parts[0].ToLowerInvariant()
        epoch = [long]$parts[1]
        parents = @($parts[2] -split ' ' | Where-Object { $_ })
        subject = $parts[3]
    }
}

function Test-ProductDiff {
    param([Parameter(Mandatory)][string]$Commit, [Parameter(Mandatory)][string]$Base)
    & git -C $RepoRoot diff --quiet --no-ext-diff --no-textconv $Base $Commit -- src/ platform/ 2>$null
    $diffExit = $LASTEXITCODE
    if ($diffExit -gt 1 -or $diffExit -lt 0) { throw 'git product diff failed' }
    return $diffExit -eq 1
}

function Read-DispatchEvidence {
    param([long]$StartEpoch, [long]$EndEpoch, [AllowNull()]$EnforcementLandedEpoch = $null)

    $path = $null
    $source = 'none'
    $reservationMode = $false
    try {
        if (Test-Path -LiteralPath $ReservationsPath -ErrorAction Stop) {
            $path = $ReservationsPath
            $source = 'dispatch-reservations'
            $reservationMode = $true
        } elseif (Test-Path -LiteralPath $LegacyDispatchPath -ErrorAction Stop) {
            $path = $LegacyDispatchPath
            $source = 'legacy-dispatch-log'
        }
    } catch {
        return [pscustomobject]@{ source = 'unavailable'; coverage = 'PARTIAL'; coverageReasons = @('COVERAGE_EVIDENCE_UNAVAILABLE'); available = $false; observed = 0; malformed = 0 }
    }

    if ($null -eq $path) {
        return [pscustomobject]@{ source = 'none'; coverage = 'PARTIAL'; coverageReasons = @('COVERAGE_EVIDENCE_UNAVAILABLE'); available = $false; observed = 0; malformed = 0 }
    }

    # Selection is final: once reservations win, any read failure is unavailable evidence.
    # It must never fall through to or combine with the legacy log.
    try {
        $lines = @(Get-Content -LiteralPath $path -ErrorAction Stop)
    } catch {
        return [pscustomobject]@{ source = 'unavailable'; coverage = 'PARTIAL'; coverageReasons = @('COVERAGE_EVIDENCE_UNAVAILABLE'); available = $false; observed = 0; malformed = 0 }
    }

    $observed = 0
    $malformed = 0
    # COVERAGE (plan 0.6: seven days of version-enforced all-venue accounting). A row with
    # schemaVersion >= 2 comes from a launcher that records EVERY launch: Invoke-Workstream writes
    # 'reserved' then 'charged'/'refunded'; Invoke-Lane writes 'reserved' for a direct launch and
    # 'linked' when Invoke-Workstream handed it a reservation id. Rows without schemaVersion are
    # legacy: they count toward the rate as before, but they are never evidence of coverage.
    $rows = [System.Collections.Generic.List[object]]::new()
    foreach ($line in $lines) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        try {
            # Preserve ISO timestamp offsets and precision across pwsh versions.
            $document = [System.Text.Json.JsonDocument]::Parse($line)
            try {
                $row = $document.RootElement
                $stamp = $row.GetProperty($(if ($reservationMode) { 'recordedUtc' } else { 'dispatchedUtc' })).GetString()
                if ($stamp -notmatch '(Z|[+-]\d{2}:\d{2})$') { throw 'timestamp timezone missing' }
                $epoch = [datetimeoffset]::Parse($stamp, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUnixTimeSeconds()
                $inWindow = $epoch -ge $StartEpoch -and $epoch -le $EndEpoch
                $version = 0L
                $state = $null; $reservationId = $null; $venue = $null; $receiptPath = $null
                if ($reservationMode) {
                    $property = [System.Text.Json.JsonElement]::new()
                    if ($row.TryGetProperty('schemaVersion', [ref]$property) -and -not $property.TryGetInt64([ref]$version)) { throw 'schemaVersion is not an integer' }
                    # A row outside the window is not evidence either way, so a field this reader only
                    # needs inside the window never makes it malformed.
                    if ($inWindow) { $state = $row.GetProperty('state').GetString() }
                    elseif ($row.TryGetProperty('state', [ref]$property) -and $property.ValueKind -eq [System.Text.Json.JsonValueKind]::String) { $state = $property.GetString() }
                    if ($row.TryGetProperty('reservationId', [ref]$property) -and $property.ValueKind -eq [System.Text.Json.JsonValueKind]::String) { $reservationId = $property.GetString() }
                    if ($row.TryGetProperty('venue', [ref]$property) -and $property.ValueKind -eq [System.Text.Json.JsonValueKind]::String) { $venue = $property.GetString() }
                    if ($row.TryGetProperty('receiptPath', [ref]$property) -and $property.ValueKind -eq [System.Text.Json.JsonValueKind]::String) { $receiptPath = $property.GetString() }
                }
            } finally { $document.Dispose() }
            $rows.Add([pscustomobject]@{ epoch = $epoch; inWindow = $inWindow; version = $version; state = $state; reservationId = $reservationId; venue = $venue; receiptPath = $receiptPath })
        } catch {
            $malformed++
        }
    }

    if (-not $reservationMode) {
        $observed = @($rows | Where-Object { $_.inWindow }).Count
        return [pscustomobject]@{ source = $source; coverage = 'PARTIAL'; coverageReasons = @('COVERAGE_LEGACY_SOURCE'); available = $true; observed = $observed; malformed = $malformed }
    }

    # A 'linked' row is honoured only when it names a dispatcher 'reserved' row written no later than
    # itself, and only once per reservation. Anything else is counted as a launch of its own, so a
    # caller cannot hide a launch from the rate by claiming someone else's reservation.
    $dispatcherReservations = @{}
    foreach ($row in $rows) {
        if ($row.state -eq 'reserved' -and $row.venue -eq 'invoke-workstream' -and $row.reservationId -and -not $dispatcherReservations.ContainsKey($row.reservationId)) {
            $dispatcherReservations[$row.reservationId] = $row.epoch
        }
    }
    $linksHonoured = [System.Collections.Generic.HashSet[string]]::new()
    $unmatchedLinks = 0
    foreach ($row in $rows) {
        if ($row.inWindow -and $row.state -eq 'reserved') { $observed++ }
        if ($row.state -ne 'linked') { continue }
        $honoured = $row.reservationId -and $dispatcherReservations.ContainsKey($row.reservationId) -and
            $dispatcherReservations[$row.reservationId] -le $row.epoch -and $linksHonoured.Add($row.reservationId)
        if (-not $honoured -and $row.inWindow) { $observed++; $unmatchedLinks++ }
    }

    $coverageReasons = [System.Collections.Generic.List[string]]::new()
    # The clock starts at the first versioned row written AT OR AFTER the ledger writer landed on the
    # source ref. Rows from an unmerged candidate never start it.
    $firstVersionedEpoch = $null
    if ($null -ne $EnforcementLandedEpoch) {
        foreach ($row in $rows) {
            if ($row.version -ge 2 -and $row.epoch -ge $EnforcementLandedEpoch -and ($null -eq $firstVersionedEpoch -or $row.epoch -lt $firstVersionedEpoch)) { $firstVersionedEpoch = $row.epoch }
        }
    }
    if ($null -eq $firstVersionedEpoch) {
        $coverageReasons.Add('COVERAGE_NOT_ENFORCED')
    } else {
        if ($firstVersionedEpoch -gt $StartEpoch) { $coverageReasons.Add('COVERAGE_WINDOW_PREDATES_ENFORCEMENT') }
        if (@($rows | Where-Object { $_.inWindow -and $_.version -lt 2 -and $_.epoch -ge $firstVersionedEpoch }).Count -gt 0) {
            $coverageReasons.Add('COVERAGE_UNVERSIONED_ROW_AFTER_ENFORCEMENT')
        }
        if ($unmatchedLinks -gt 0) { $coverageReasons.Add('COVERAGE_LINK_UNMATCHED') }
        $ledgerReceipts = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
        $windowRowReceipts = [System.Collections.Generic.List[string]]::new()
        foreach ($row in $rows) {
            if ([string]::IsNullOrWhiteSpace($row.receiptPath)) { continue }
            $full = Resolve-LedgerReceiptPath $row.receiptPath
            [void]$ledgerReceipts.Add($full)
            if ($row.inWindow -and $row.version -ge 2) { $windowRowReceipts.Add($full) }
        }
        foreach ($code in (Test-ReceiptCoverage -StartEpoch $StartEpoch -EndEpoch $EndEpoch -LedgerReceipts $ledgerReceipts -WindowRowReceipts $windowRowReceipts)) {
            $coverageReasons.Add($code)
        }
    }

    return [pscustomobject]@{
        source = $source
        coverage = $(if ($coverageReasons.Count -eq 0) { 'COMPLETE' } else { 'PARTIAL' })
        coverageReasons = @($coverageReasons)
        available = $true
        observed = $observed
        malformed = $malformed
    }
}

function Resolve-LedgerReceiptPath {
    param([Parameter(Mandatory)][string]$Path)
    # Invoke-Workstream records receipt paths relative to the board root; Invoke-Lane records them
    # absolute. Compare both as full, case-insensitive Windows paths.
    $candidate = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $RepoRoot $Path }
    return [System.IO.Path]::GetFullPath($candidate)
}

function Get-ReceiptRoots {
    # The board's fleet-runs tree, plus the fleet-runs tree of every registered worktree: a runner
    # from an older checkout writes no ledger row and, by default, keeps its receipts inside its own
    # worktree, so that is where a stale venue shows up. An unreadable worktree list is cannot-determine.
    $roots = [System.Collections.Generic.List[string]]::new()
    $roots.Add([System.IO.Path]::GetFullPath($FleetRunsPath).TrimEnd('\'))
    foreach ($worktree in (Get-RegisteredWorktreePaths)) {
        $candidate = [System.IO.Path]::GetFullPath((Join-Path $worktree '.claude-state\fleet-runs')).TrimEnd('\')
        if ((Test-Path -LiteralPath $candidate -PathType Container) -and -not ($roots -contains $candidate)) { $roots.Add($candidate) }
    }
    return @($roots)
}

function Get-RegisteredWorktreePaths {
    $listing = @(& git -C $RepoRoot worktree list --porcelain 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'git worktree list failed' }
    return @($listing | Where-Object { [string]$_ -match '^worktree (.+)$' } | ForEach-Object { ([string]$_).Substring(9) -replace '/', '\' })
}

function Get-StaleRunnerCodes {
    # A receipt can only be scanned where it lands, and -RunDir can point anywhere. So coverage also
    # requires that no registered checkout can START an unaccounted launch: every checkout's
    # Invoke-Lane.ps1 must carry the ledger writer, and must not have been replaced inside the window
    # (it may have been a stale runner earlier in it). A checkout without the file cannot launch a lane.
    param([long]$StartEpoch)
    $codes = [System.Collections.Generic.List[string]]::new()
    $startUtc = [datetimeoffset]::FromUnixTimeSeconds($StartEpoch).UtcDateTime
    $stale = 0; $updated = 0
    foreach ($worktree in (Get-RegisteredWorktreePaths)) {
        # BOTH ledger writers must carry the serialized-writer marker: a runner that merely has the
        # writer function may predate the mutex and lose rows under contention.
        foreach ($writer in @('tools\coordination\Invoke-Lane.ps1', 'tools\coordination\Invoke-Workstream.ps1')) {
            $file = Join-Path $worktree $writer
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { continue }
            if (-not ([System.IO.File]::ReadAllText($file)).Contains($LedgerWriterMarker)) { $stale++ }
            elseif ([System.IO.File]::GetLastWriteTimeUtc($file) -ge $startUtc) { $updated++ }
        }
    }
    if ($stale -gt 0) { $codes.Add('COVERAGE_STALE_RUNNER_PRESENT') }
    if ($updated -gt 0) { $codes.Add('COVERAGE_RUNNER_UPDATED_IN_WINDOW') }
    return @($codes)
}

function Test-ReceiptCoverage {
    # Both directions, three outcomes each. MATCH: a lane receipt started in the window has a ledger
    # row naming it, and a versioned in-window row's receipt exists. MISMATCH: either side is missing.
    # CANNOT-DETERMINE: a receipt tree or a receipt cannot be read, or a receipt slot is still empty
    # (Invoke-Lane creates it an instant before writing its row). CANNOT-DETERMINE is never COMPLETE.
    # Every receipt is parsed: its startedUtc decides the window, never its mutable file time.
    param(
        [long]$StartEpoch,
        [long]$EndEpoch,
        [System.Collections.Generic.HashSet[string]]$LedgerReceipts,
        [System.Collections.Generic.List[string]]$WindowRowReceipts
    )
    $codes = [System.Collections.Generic.List[string]]::new()
    if (-not (Test-Path -LiteralPath $FleetRunsPath -PathType Container)) {
        $codes.Add('COVERAGE_RECEIPTS_UNAVAILABLE')
        return @($codes)
    }
    $unreserved = 0; $unreadable = 0; $inFlight = 0; $missing = 0
    try {
        $roots = Get-ReceiptRoots
        foreach ($code in (Get-StaleRunnerCodes -StartEpoch $StartEpoch)) { $codes.Add($code) }
        $files = [System.Collections.Generic.List[string]]::new()
        foreach ($root in $roots) {
            foreach ($file in [System.IO.Directory]::EnumerateFiles($root, '*.receipt.json', [System.IO.SearchOption]::AllDirectories)) { $files.Add($file) }
        }
    } catch {
        $codes.Add('COVERAGE_RECEIPTS_UNAVAILABLE')
        return @($codes)
    }
    foreach ($file in $files) {
        try {
            $text = [System.IO.File]::ReadAllText($file)
            if ([string]::IsNullOrWhiteSpace($text)) { $inFlight++; continue }
            $document = [System.Text.Json.JsonDocument]::Parse($text)
            try {
                $root = $document.RootElement
                $property = [System.Text.Json.JsonElement]::new()
                if (-not $root.TryGetProperty('schema', [ref]$property) -or $property.ValueKind -ne [System.Text.Json.JsonValueKind]::String -or $property.GetString() -ne 'mlv-app/fleet-lane-receipt/v1') { continue }
                if (-not ($root.TryGetProperty('startedUtc', [ref]$property) -or $root.TryGetProperty('reservedUtc', [ref]$property))) { throw 'receipt has no start stamp' }
                $startStamp = $property.GetString()
                $failureText = if ($root.TryGetProperty('failure', [ref]$property) -and $property.ValueKind -eq [System.Text.Json.JsonValueKind]::String) { $property.GetString() } else { '' }
                $receiptState = if ($root.TryGetProperty('state', [ref]$property) -and $property.ValueKind -eq [System.Text.Json.JsonValueKind]::String) { $property.GetString() } else { '' }
            } finally { $document.Dispose() }
            $startEpochOfReceipt = [datetimeoffset]::Parse($startStamp, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUnixTimeSeconds()
            if ($startEpochOfReceipt -lt $StartEpoch -or $startEpochOfReceipt -gt $EndEpoch) { continue }
            # Invoke-Lane refuses to launch when it cannot write its ledger row; that receipt is not a launch.
            if ($failureText.StartsWith('dispatch-ledger-write-failed')) { continue }
            if (-not $LedgerReceipts.Contains([System.IO.Path]::GetFullPath($file))) {
                # A bare slot marker precedes its ledger row by an instant: cannot-determine, not a mismatch.
                if ($receiptState -eq 'reserved') { $inFlight++ } else { $unreserved++ }
            }
        } catch {
            $unreadable++
        }
    }
    foreach ($path in $WindowRowReceipts) {
        $underRoot = @($roots | Where-Object { $path.StartsWith($_ + '\', [StringComparison]::OrdinalIgnoreCase) }).Count -gt 0
        if ($underRoot -and -not [System.IO.File]::Exists($path)) { $missing++ }
    }
    if ($unreserved -gt 0) { $codes.Add('COVERAGE_RECEIPT_UNRESERVED') }
    if ($missing -gt 0) { $codes.Add('COVERAGE_RESERVATION_RECEIPT_MISSING') }
    if ($unreadable -gt 0) { $codes.Add('COVERAGE_RECEIPT_UNREADABLE') }
    if ($inFlight -gt 0) { $codes.Add('COVERAGE_RECEIPT_IN_FLIGHT') }
    return @($codes)
}

function Write-ErrorResult {
    param([string]$Code, [string]$ResolvedSha = '')
    $endEpoch = if ($AsOfEpoch -gt 0) { $AsOfEpoch } else { [datetimeoffset]::UtcNow.ToUnixTimeSeconds() }
    $startEpoch = $endEpoch - $WindowSeconds
    [ordered]@{
        schema = 'mlv-app/product-ratio-guard/v1'
        asOfUtc = [datetimeoffset]::FromUnixTimeSeconds($endEpoch).UtcDateTime.ToString('o')
        windowStartUtc = [datetimeoffset]::FromUnixTimeSeconds($startEpoch).UtcDateTime.ToString('o')
        windowEndUtc = [datetimeoffset]::FromUnixTimeSeconds($endEpoch).UtcDateTime.ToString('o')
        sourceRef = $SourceRef
        sourceSha = $ResolvedSha
        commitPopulation = 0
        productCommitCount = 0
        productShare7d = $null
        productShareThreshold = 0.50
        recognizedProductPrCount = 0
        recognizedProductPrIds = @()
        unrecognizedProductLandings = @()
        landingProvenanceComplete = $false
        hasProductLandings = $false
        dispatchEvidenceSource = 'unavailable'
        dispatchCoverage = 'PARTIAL'
        dispatchEvidenceAvailable = $false
        dispatchesObserved = 0
        malformedDispatchRows = 0
        dispatchesPerLandedProductPr7dLowerBound = $null
        dispatchRateThreshold = 4.0
        verdict = 'ERROR'
        reasons = @($Code)
        errorCode = $Code
    } | ConvertTo-Json -Depth 6 -Compress
    exit 3
}

if (-not $ReservationsPath) {
    $ReservationsPath = Join-Path $RepoRoot '.claude-state\coordination\dual-lane\receipts\dispatch-reservations.jsonl'
}
if (-not $LegacyDispatchPath) {
    $LegacyDispatchPath = Join-Path $RepoRoot '.claude-state\coordination\dual-lane\workstream-dispatch-log.jsonl'
}
if (-not $FleetRunsPath) {
    $FleetRunsPath = Join-Path $RepoRoot '.claude-state\fleet-runs'
}
if ($AsOfEpoch -le 0) { $AsOfEpoch = [datetimeoffset]::UtcNow.ToUnixTimeSeconds() }
$windowStart = $AsOfEpoch - $WindowSeconds

$sourceSha = ''
try {
    $resolved = @(& git -C $RepoRoot rev-parse --verify "$SourceRef^{commit}" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $resolved.Count -ne 1 -or $resolved[0] -notmatch '^[0-9a-fA-F]{40}$') {
        Write-ErrorResult 'ERROR_REF_UNRESOLVED'
    }
    $sourceSha = ([string]$resolved[0]).ToLowerInvariant()
} catch {
    Write-ErrorResult 'ERROR_REF_UNRESOLVED'
}

try {
    # Read metadata in two bounded process invocations, not one child per historic
    # commit. Only commits inside the window need a diff process.
    $allLines = Invoke-GitLines @('log','--no-show-signature','--no-color','--format=%H%x09%ct%x09%P%x09%s',$sourceSha)
    $population = 0
    $productCount = 0
    foreach ($line in $allLines) {
        $info = Get-CommitInfo $line
        if ($info.epoch -gt $AsOfEpoch) { continue }
        if ($info.epoch -lt $windowStart) { continue }
        if ($info.parents.Count -gt 1) { continue }
        $population++
        $base = if ($info.parents.Count -eq 0) { $EmptyTreeSha } else { $info.parents[0] }
        if (Test-ProductDiff -Commit $info.sha -Base $base) { $productCount++ }
    }

    $firstParentLines = Invoke-GitLines @('log','--first-parent','--no-show-signature','--no-color','--format=%H%x09%ct%x09%P%x09%s',$sourceSha)
    $recognized = @{}
    $unknown = [System.Collections.Generic.List[string]]::new()
    foreach ($line in $firstParentLines) {
        $info = Get-CommitInfo $line
        if ($info.epoch -gt $AsOfEpoch -or $info.epoch -lt $windowStart) { continue }
        $base = if ($info.parents.Count -eq 0) { $EmptyTreeSha } else { $info.parents[0] }
        $productLanding = Test-ProductDiff -Commit $info.sha -Base $base
        if (-not $productLanding) { continue }

        $pr = $null
        if ($info.parents.Count -eq 2 -and $info.subject -match '^Merge pull request #(\d+)\b') {
            $pr = $Matches[1]
        } elseif ($info.parents.Count -eq 1 -and $info.subject -match '\(#(\d+)\)$') {
            $pr = $Matches[1]
        }

        if ($null -eq $pr) { $unknown.Add($info.sha) }
        else { $recognized[$pr] = $true }
    }
} catch {
    Write-ErrorResult 'ERROR_GIT_HISTORY' $sourceSha
}

# ENFORCEMENT LANDED: the committer time of the oldest first-parent commit on the source ref from
# which Invoke-Lane.ps1 continuously carries the ledger writer. Ledger rows written before that
# moment (for example by an unmerged candidate) never start the seven-day clock.
$enforcementLandedEpoch = $null
try {
    $laneHistory = Invoke-GitLines @('log','--first-parent','--no-show-signature','--no-color','--format=%H%x09%ct',$sourceSha,'--','tools/coordination/Invoke-Lane.ps1')
    foreach ($line in $laneHistory) {
        $parts = $line -split "`t"
        $carries = @(& git -C $RepoRoot grep -l -F $LedgerWriterMarker $parts[0] -- 'tools/coordination/Invoke-Lane.ps1' 2>$null).Count -gt 0
        if (-not $carries) { break }
        $enforcementLandedEpoch = [long]$parts[1]
    }
} catch {
    $enforcementLandedEpoch = $null
}

$evidence = Read-DispatchEvidence -StartEpoch $windowStart -EndEpoch $AsOfEpoch -EnforcementLandedEpoch $enforcementLandedEpoch
$productShare = if ($population -eq 0) { $null } else { [double]$productCount / [double]$population }
$prIds = @($recognized.Keys | ForEach-Object { [int]$_ } | Sort-Object)
$provenanceComplete = $unknown.Count -eq 0
$hasProductLandings = ($prIds.Count + $unknown.Count) -gt 0
$rate = $null
if ($evidence.available -and $provenanceComplete -and $prIds.Count -gt 0) {
    $rate = [double]$evidence.observed / [double]$prIds.Count
}
$decision = New-Decision -ProductShare $productShare -DispatchRate $rate -Coverage $evidence.coverage -EvidenceAvailable $evidence.available -ProvenanceComplete $provenanceComplete -HasProductLandings $hasProductLandings -MalformedRows $evidence.malformed -CoverageReasons @($evidence.coverageReasons)

[ordered]@{
    schema = 'mlv-app/product-ratio-guard/v1'
    asOfUtc = [datetimeoffset]::FromUnixTimeSeconds($AsOfEpoch).UtcDateTime.ToString('o')
    windowStartUtc = [datetimeoffset]::FromUnixTimeSeconds($windowStart).UtcDateTime.ToString('o')
    windowEndUtc = [datetimeoffset]::FromUnixTimeSeconds($AsOfEpoch).UtcDateTime.ToString('o')
    sourceRef = $SourceRef
    sourceSha = $sourceSha
    commitPopulation = $population
    productCommitCount = $productCount
    productShare7d = $productShare
    productShareThreshold = 0.50
    recognizedProductPrCount = $prIds.Count
    recognizedProductPrIds = $prIds
    unrecognizedProductLandings = @($unknown)
    landingProvenanceComplete = $provenanceComplete
    hasProductLandings = $hasProductLandings
    dispatchEvidenceSource = $evidence.source
    dispatchCoverage = $evidence.coverage
    dispatchEvidenceAvailable = $evidence.available
    dispatchesObserved = $evidence.observed
    malformedDispatchRows = $evidence.malformed
    dispatchesPerLandedProductPr7dLowerBound = $rate
    dispatchRateThreshold = 4.0
    verdict = $decision.verdict
    reasons = @($decision.reasons)
    errorCode = $null
} | ConvertTo-Json -Depth 6 -Compress
exit 0
