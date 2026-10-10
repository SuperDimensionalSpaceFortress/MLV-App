# New-VenueFlavorPair.ps1 -- compose the Classic | Cinematic look-flavor diff for ONE venue's pair of look legs
# (LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1). RUN THIS ON THE VM.
#
# Takes the two receipts Invoke-VenueLeg.ps1 wrote for the same leg run twice from one build -- once Classic (e.g. m16-1243-look-scale2), once
# Cinematic (m16-1243-look-scale2-cinematic) -- refuses unless they are the same venue / build / clip / backend / card / effective scale and differ
# in flavor exactly as named (classic first, cinematic second), then hands the hashed frames and the run's own sliders to
# tools\profiling\look-flavor-diff.py. Receipts are append-only and are not edited: the pair is written as its OWN record
# (mlv-app/dual-venue-flavor-pair/v1, CreateNew) naming both receipt ids, the sheet's sha256 and the frame-match claims.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\New-VenueFlavorPair.ps1 `
#       -ClassicReceipt <classic receipt.json> -CinematicReceipt <cinematic receipt.json> -OutDir <dir under .claude-state>
#
# New-VenueSheetPair.ps1 is the model, and the same rules hold:
# * every leg plays a CONSENTED OWNER CLIP, so the sheet is owner footage: -OutDir must sit under a `.claude-state` directory (never committed,
#   PR-attached, bus-published or published as an artifact);
# * only a receipt that is itself valid evidence is paired: Test-DvReceiptValid -RepoRoot re-derives its admission from the committed blobs and its run
#   from the hashed evidence; a production receipt is ADVISORY at best (VENUE_ANCHOR_ABSENT), so the pair is a DIAGNOSTIC sheet and its record says so;
# * frames are staged from the very bytes Read-DvContactFrames hashed, and the composer re-verifies each at read time;
# * the sliders are read from the VALIDATED evidence directory -- result.json visualQuality.lookAssist, else the run log's look_assist.apply.result
#   line -- each file re-hashed against the receipt before it is read; never from a receipt's free text. lookFlavorReported is read from the hashed
#   summary.json.
# The two legs' specs differ (legId, flavor, the applied-flavor criterion), so subject.legSpecSha256 is NOT compared; subject.lookFlavor is, and must differ.
# Cheap structural checks (backend, flavor, subject equality) run before the evidence validation, so a mismatched pair is refused without reading evidence.
# One pair per OutDir, append-only: an OutDir already holding a flavor-pair-*.json record (PAIR_RECORD_EXISTS) or a sheet / metrics / table
# (PAIR_OUTPUT_EXISTS) is refused with exit code 16 before anything is staged or composed; every other refusal stays a thrown error (exit 1).
# STAGING lives OUTSIDE OutDir, in a sibling .pair-staging-<guid> directory (still under .claude-state: it holds owner footage), so a refused attempt
# really does write nothing into OutDir. It is never deleted by this script (no delete primitive is allowed on the owner-leg route beyond the one marker
# line): each attempt leaves its own fresh-GUID directory beside OutDir, inert, for the run's owner to retire; a FLAVOR_INERT refusal's message names the
# slider files in it.
# ATTEMPT MARKER: look-flavor-diff.py creates OutDir\.pair-in-progress.json before its first output and (--keep-marker) leaves it until this script has
# written the record. An attempt that dies in between leaves a marker, and a retry into that directory exits 17 (PAIR_INCOMPLETE_ATTEMPT) with the
# diagnosis instead of the silent PAIR_OUTPUT_EXISTS block. -RecoverIncomplete moves the dead attempt's marker and unrecorded outputs into
# OutDir\incomplete-<utc>\ (nothing is deleted) and pairs again; a directory that holds a pair OR a trio record is never recovered (16). The record is created
# exclusively and then written, so a crash can leave a marker beside an empty / truncated record: that is the same incomplete attempt (17, the half record
# named and its bytes untouched; -RecoverIncomplete does not move a record), never PAIR_RECORD_EXISTS. (The record is not written under a temporary name and
# renamed into place: the owner-footage route guards allow no move primitive in this script, so the half record is detected instead.)
# OWNER FOOTAGE ROOT (exit 18, PAIR_OUTDIR_HOLDS_OWNER_FOOTAGE): -OutDir, anything under it, or an ancestor up to the nearest .claude-state directory that holds
# a clip (or a numbered continuation part) is refused before anything is staged, written or deleted -- the marker delete never runs inside a footage root.
#
# LOOK-ASSIST-FILM-FLAVOR-1: an optional third receipt, -FilmReceipt (lookFlavor `film`), makes it a TRIO: the same equality checks across all
# three, the composer's three-side mode (sheet-classic-cinematic-film.png, metrics v2), and its own record mlv-app/dual-venue-flavor-trio/v1
# (flavor-trio-*.json, CreateNew). Without -FilmReceipt everything above is the two-side pair, unchanged. The trio shares the staging-beside-OutDir
# rule, the footage-root refusal (18), the append-only guards (16) and the ATTEMPT MARKER (FILM-TRIO-INCOMPLETE-ATTEMPT-MARKER-1): the three-side
# composer creates it before its first output and (--keep-marker) leaves it until the trio record is written, so a trio that dies in between, the record
# write included, is the typed exit 17 with the half record named, never PAIR_RECORD_EXISTS. -RecoverIncomplete is refused with -FilmReceipt (the
# composer quarantines only the pair's outputs), so a dead trio's way forward is a new -OutDir.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$ClassicReceipt,
    [Parameter(Mandatory = $true)][string]$CinematicReceipt,
    [string]$FilmReceipt = '',
    [Parameter(Mandatory = $true)][string]$OutDir,
    [switch]$RecoverIncomplete
)
$trio = -not [string]::IsNullOrWhiteSpace($FilmReceipt)
if ($trio -and $RecoverIncomplete) { throw 'PAIR_RECOVER_NOT_FOR_TRIO -RecoverIncomplete applies to the two-side pair only: the composer quarantines only the pair''s outputs, so a dead trio attempt (exit 17) is left as it is; use a new -OutDir' }
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'DualVenueRunner.psm1') -Force

# The sheet is owner footage: it may only be written where it stays local.
$outFull = [IO.Path]::GetFullPath($OutDir)
if (@($outFull.Split([char[]]@('\', '/')) | Where-Object { $_ -ceq '.claude-state' }).Count -eq 0) {
    throw 'PAIR_OWNER_SHEET_MUST_STAY_LOCAL -OutDir must be under a .claude-state directory; a sheet of owner footage is never committed, attached or published'
}

# -OutDir is caller-controlled, and the one delete this script owns (the attempt marker, at the end) must never run inside a directory that holds owner footage.
# Decided before ANY write, stage or delete: -OutDir (and everything under it) and every ancestor up to and including the nearest .claude-state directory are
# scanned for an owner-footage NAME -- the clip's composed base extension and its numbered continuation parts, the rule Get-AttrCudaOwnerFootageNeutralName
# (tools\profiling\bachelor\AttrCudaOwnerFootage.psm1) and DualVenueRunner.psm1 compose. The extension is composed, never spelled as one token (NA-4).
function Test-OwnerFootageName([string]$Name) {
    $ext = [IO.Path]::GetExtension($Name)
    ($ext -ieq ('.' + 'mlv')) -or ($ext -imatch '^\.m\d{2}$')
}
$footageScan = $outFull.TrimEnd('\', '/')
$footageHolder = $null
while ($footageScan -and -not $footageHolder) {
    if (Test-Path -LiteralPath $footageScan -PathType Container) {
        $isOutDir = ($footageScan -ceq $outFull.TrimEnd('\', '/'))
        $clipHere = @(Get-ChildItem -LiteralPath $footageScan -File -Force -Recurse:$isOutDir -ErrorAction Stop | Where-Object { Test-OwnerFootageName $_.Name } | Select-Object -First 1)
        if ($clipHere.Count -gt 0) { $footageHolder = $footageScan }
    }
    if ([IO.Path]::GetFileName($footageScan) -ceq '.claude-state') { break }
    $footageScan = [IO.Path]::GetDirectoryName($footageScan)
}
if ($footageHolder) {
    [Console]::Error.WriteLine("PAIR_OUTDIR_HOLDS_OWNER_FOOTAGE ${footageHolder} holds an owner-footage file and is -OutDir or an ancestor of it (up to .claude-state): refused before anything was staged, written or removed; use an -OutDir under a .claude-state directory that holds no clips")
    exit 18
}

# Append-only, decided BEFORE anything is read, staged or composed: an OutDir that already holds a pair record, a sheet, metrics or a table belongs to an
# earlier pair, and writing a different pair beside it would overwrite the evidence that record names. Refused with its own exit code (16); compose into a
# new directory. look-flavor-diff.py repeats the check and creates every output exclusively, so a concurrent composer cannot slip past this one.
$markerName = '.pair-in-progress.json'   # look-flavor-diff.py's MARKER_NAME
function Test-PairRecordComplete([string]$Path) {
    # A finished record is one JSON object. An empty file, a truncated one or anything unreadable is a half-written record (fail toward "incomplete").
    try {
        $parsed = [IO.File]::ReadAllText($Path) | ConvertFrom-Json -ErrorAction Stop
        return ($null -ne $parsed -and $parsed -is [pscustomobject])
    } catch { return $false }
}
if (Test-Path -LiteralPath $outFull -PathType Container) {
    $recordsHere = @(Get-ChildItem -LiteralPath $outFull -Filter 'flavor-pair-*.json' -File -ErrorAction Stop)
    # FLAVOR-TRIO-RECOVERY-PROTECT-1: a completed trio's record names the trio's sheet / metrics / table / rows, so pair-mode -RecoverIncomplete must see it too.
    if ($trio -or $RecoverIncomplete) { $recordsHere += @(Get-ChildItem -LiteralPath $outFull -Filter 'flavor-trio-*.json' -File -ErrorAction Stop) }
    if ($recordsHere.Count -gt 0) {
        # A marker beside a record that does not parse as a JSON object is an attempt that died while the record was being written (it is created
        # exclusively, then filled): the incomplete diagnosis, with the half record's bytes untouched. -RecoverIncomplete does not apply to it -- a record
        # file is never moved aside -- so the way forward is a new -OutDir. A complete record beside a marker, or any record with no marker, is still 16.
        if (Test-Path -LiteralPath (Join-Path $outFull $markerName) -PathType Leaf) {
            $halfRecords = @($recordsHere | Where-Object { -not (Test-PairRecordComplete $_.FullName) })
            if ($halfRecords.Count -gt 0) {
                [Console]::Error.WriteLine("PAIR_INCOMPLETE_ATTEMPT ${outFull} holds $markerName and a pair record that is not complete ($(@($halfRecords | ForEach-Object { $_.Name }) -join ', ')): the attempt died while the record was being written. Nothing was changed, the record's bytes are preserved, and -RecoverIncomplete does not apply (a record file is never moved aside). Use a new -OutDir")
                exit 17
            }
        }
        [Console]::Error.WriteLine("PAIR_RECORD_EXISTS $($recordsHere[0].Name) is already in ${outFull}: an earlier pair's record and evidence are never overwritten; use a new -OutDir")
        exit 16
    }
    if (Test-Path -LiteralPath (Join-Path $outFull $markerName) -PathType Leaf) {
        # An unfinished attempt (no record). Without -RecoverIncomplete: the typed diagnosis. With it: the composer moves the unrecorded files aside.
        if (-not $RecoverIncomplete) {
            $left = @(Get-ChildItem -LiteralPath $outFull -File -ErrorAction Stop | Where-Object { $_.Name -ne $markerName } | ForEach-Object { $_.Name })
            $way = $(if ($trio) { 'Use a new -OutDir (a trio is not recovered in place)' } else { 'Use a new -OutDir, or re-run with -RecoverIncomplete to MOVE the marker and those files into incomplete-<utc>\ (nothing is deleted) -- only when no composer is still running there' })
            [Console]::Error.WriteLine("PAIR_INCOMPLETE_ATTEMPT ${outFull} holds $markerName from an attempt that did not finish; it left $(if ($left.Count) { $left -join ', ' } else { 'no outputs' }), none of it recorded evidence. Nothing was changed. $way")
            exit 17
        }
    } else {
        $guarded = @('sheet-classic-vs-cinematic.png', 'metrics.json', 'table.md')
        if ($trio) { $guarded += 'sheet-classic-cinematic-film.png' }
        foreach ($name in $guarded) {
            if (Test-Path -LiteralPath (Join-Path $outFull $name)) {
                [Console]::Error.WriteLine("PAIR_OUTPUT_EXISTS $name is already in ${outFull}: an earlier pair's evidence is never overwritten; use a new -OutDir")
                exit 16
            }
        }
    }
}
# Staging sits beside OutDir, never in it; it holds owner footage, so its parent must be under .claude-state too.
$stageParent = [IO.Path]::GetDirectoryName($outFull.TrimEnd('\', '/'))
if (-not $stageParent -or @($stageParent.Split([char[]]@('\', '/')) | Where-Object { $_ -ceq '.claude-state' }).Count -eq 0) {
    throw 'PAIR_OUTDIR_NEEDS_LOCAL_PARENT -OutDir must be a subdirectory of a .claude-state path (its staging is a sibling directory, and owner footage stays local)'
}
$classic = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $ClassicReceipt).Path) | ConvertFrom-Json
$cinematic = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $CinematicReceipt).Path) | ConvertFrom-Json

if ([string]$classic.subject.lookFlavor -cne 'classic' -or [string]$cinematic.subject.lookFlavor -cne 'cinematic') {
    throw "PAIR_FLAVORS_WRONG the first receipt must be the classic flavor and the second the cinematic flavor (got '$($classic.subject.lookFlavor)' and '$($cinematic.subject.lookFlavor)')"
}
foreach ($f in 'buildManifestSha256', 'clipId', 'backend') {
    if ([string]$classic.subject.$f -cne [string]$cinematic.subject.$f) { throw "PAIR_SUBJECT_DIFFERS the receipts differ in subject.$f" }
}
if ([string]$classic.subject.backend -cne 'cpu') { throw "PAIR_BACKEND_NOT_CPU the flavor pair is a cpu benchmark; the receipts are backend '$($classic.subject.backend)'" }
if ([string]$classic.metrics.sourceCommit -cne [string]$cinematic.metrics.sourceCommit) { throw 'PAIR_SUBJECT_DIFFERS the receipts differ in metrics.sourceCommit' }
if ([string]$classic.venue.name -cne [string]$cinematic.venue.name -or [string]$classic.card -cne [string]$cinematic.card) { throw 'PAIR_VENUE_OR_CARD_DIFFERS the receipts are not the same venue/card' }
if ([string]$classic.scale.effectiveScale -cne [string]$cinematic.scale.effectiveScale) { throw "PAIR_SCALE_DIFFERS the receipts rendered at different effective scales ($($classic.scale.effectiveScale) vs $($cinematic.scale.effectiveScale))" }
$film = $null
if ($trio) {
    $film = [IO.File]::ReadAllText((Resolve-Path -LiteralPath $FilmReceipt).Path) | ConvertFrom-Json
    if ([string]$film.subject.lookFlavor -cne 'film') { throw "PAIR_FLAVORS_WRONG the third receipt must be the film flavor (got '$($film.subject.lookFlavor)')" }
    foreach ($f in 'buildManifestSha256', 'clipId', 'backend') {
        if ([string]$classic.subject.$f -cne [string]$film.subject.$f) { throw "PAIR_SUBJECT_DIFFERS the film receipt differs in subject.$f" }
    }
    if ([string]$classic.metrics.sourceCommit -cne [string]$film.metrics.sourceCommit) { throw 'PAIR_SUBJECT_DIFFERS the film receipt differs in metrics.sourceCommit' }
    if ([string]$classic.venue.name -cne [string]$film.venue.name -or [string]$classic.card -cne [string]$film.card) { throw 'PAIR_VENUE_OR_CARD_DIFFERS the film receipt is not the same venue/card' }
    if ([string]$classic.scale.effectiveScale -cne [string]$film.scale.effectiveScale) { throw "PAIR_SCALE_DIFFERS the film receipt rendered at a different effective scale ($($classic.scale.effectiveScale) vs $($film.scale.effectiveScale))" }
}

# The repo whose COMMITTED consent and venue table a receipt is verified against is the one this script lives in (never a caller's).
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path

function Read-HashedEvidenceFile([string]$Dir, [string]$Rel, [string]$Claim, [string]$What) {
    $path = Join-Path $Dir $Rel
    if ($Claim -cnotmatch '^[0-9a-f]{64}$') { throw "PAIR_EVIDENCE_UNBOUND the receipt names no sha256 for $What" }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "PAIR_EVIDENCE_MISSING $What is not in the evidence directory" }
    $bytes = [IO.File]::ReadAllBytes($path)
    if ((Get-DvSha256OfBytes $bytes) -cne $Claim) { throw "PAIR_EVIDENCE_HASH_MISMATCH $What does not hash to the sha256 its receipt names" }
    [Text.Encoding]::UTF8.GetString($bytes).TrimStart([char]0xFEFF)
}

$sliderNames = [ordered]@{ presetExposure = 'preset_exp'; presetContrast = 'preset_contrast'; presetPivot = 'preset_pivot'; presetShadows = 'preset_shadows'
    presetHighlights = 'preset_highlights'; presetVibrance = 'preset_vibrance'; presetTemperatureDelta = 'preset_temp_delta'; presetTintDelta = 'preset_tint_delta'
    finalTemperature = 'final_temp'; finalTint = 'final_tint'; scene = 'scene' }
$flavorOwned = @('presetContrast', 'presetPivot', 'presetShadows', 'presetHighlights', 'presetVibrance')

function Read-Sliders($Receipt, [string]$EvDir) {
    $result = (Read-HashedEvidenceFile $EvDir 'result.json' ([string]$Receipt.evidence.resultJsonSha256) 'result.json') | ConvertFrom-Json
    $la = $result.visualQuality.lookAssist
    $out = [ordered]@{}
    $complete = $null -ne $la
    if ($complete) { foreach ($k in $flavorOwned) { if ($null -eq $la.$k) { $complete = $false } } }
    if ($complete) {
        $out.source = 'result.json visualQuality.lookAssist'
        foreach ($k in $sliderNames.Keys) { $out[$k] = $la.$k }
        # LOOK-ASSIST-FILM-FLAVOR-1: the Film grade the apply laid; a build before it (or a Classic / Cinematic apply) has none.
        if ($trio) { $out.presetGrade = $(if ([string]::IsNullOrEmpty([string]$la.presetGrade)) { 'none' } else { [string]$la.presetGrade }) }
        return $out
    }
    # Fallback: the hashed run log's last look_assist.apply.result line (the line result.json's block is built from).
    $log = Read-HashedEvidenceFile $EvDir 'logs\smoke-run.log' ([string]$Receipt.evidence.logSha256) 'logs\smoke-run.log'
    $line = @($log -split "`r?`n" | Where-Object { $_ -match 'event=look_assist\.apply\.result(\s|$)' }) | Select-Object -Last 1
    if (-not $line) { throw "PAIR_SLIDERS_MISSING receipt $($Receipt.receiptId): neither result.json visualQuality.lookAssist nor a look_assist.apply.result log line carries the sliders" }
    $kv = @{}
    foreach ($m in [regex]::Matches($line, '(\w+)=(\S+)')) { $kv[$m.Groups[1].Value] = $m.Groups[2].Value }
    $out.source = 'logs\smoke-run.log look_assist.apply.result'
    foreach ($k in $sliderNames.Keys) {
        $v = $kv[$sliderNames[$k]]
        $n = 0
        $out[$k] = $(if ($null -eq $v) { $null } elseif ($k -ne 'scene' -and [int]::TryParse($v, [ref]$n)) { $n } else { $v })
    }
    if ($trio) { $out.presetGrade = $(if ([string]::IsNullOrEmpty([string]$kv['grade'])) { 'none' } else { [string]$kv['grade'] }) }
    $out
}

$sides = [ordered]@{ classic = $classic; cinematic = $cinematic }
if ($trio) { $sides.film = $film }
$evidenceDirs = @{}
$listedFrames = @{}
$sliders = @{}
$reported = @{}
foreach ($label in $sides.Keys) {
    $r = $sides[$label]
    if ($r.outcome -ne 'PASS' -and $r.outcome -ne 'FAIL') { throw "PAIR_RECEIPT_INCOMPLETE receipt $($r.receiptId) is $($r.outcome), not PASS/FAIL" }
    # The evidence-bearing validator: admission re-derived from the committed blobs, the run from the hashed evidence. Nothing the receipt says is believed.
    $validity = Test-DvReceiptValid -Receipt $r -RepoRoot $repoRoot
    if ($validity.status -cne 'ADVISORY') { throw "PAIR_RECEIPT_$($validity.status) receipt $($r.receiptId) is not valid evidence: $(@($validity.reasons) -join '; ')" }
    if ([string]$validity.legType -cne 'look') { throw "PAIR_NOT_A_LOOK_LEG receipt $($r.receiptId) is not a LOOK leg per its committed leg spec" }
    if ([string]$r.subject.clipId -cnotmatch '^[A-Za-z]\d{2}-\d{3,4}$') { throw 'PAIR_NOT_A_CONSENTED_CLIP only a consented clip id (never a fixture or a path) can be paired into a sheet' }
    $evDir = [string]$validity.evidenceDir
    $listed = Read-DvContactFrames -Receipt $r -EvidenceDir $evDir
    if (-not $listed.ok) { throw "PAIR_FRAMES_NOT_LISTED receipt $($r.receiptId): $(@($listed.reasons) -join '; ')" }
    $evidenceDirs[$label] = [IO.Path]::GetFullPath($evDir).TrimEnd('\').ToLowerInvariant()
    $listedFrames[$label] = $listed
    $sliders[$label] = Read-Sliders $r $evDir
    $summary = (Read-HashedEvidenceFile $evDir 'summary.json' ([string]$r.evidence.summaryJsonSha256) 'summary.json') | ConvertFrom-Json
    $reported[$label] = $(if ($null -ne $summary.lookFlavorReported) { [string]$summary.lookFlavorReported } else { 'none' })
}
if ($evidenceDirs['classic'] -ceq $evidenceDirs['cinematic']) { throw 'PAIR_SHARED_EVIDENCE the two receipts name one evidence directory; two legs have separate evidence' }
if ($trio -and ($evidenceDirs['film'] -ceq $evidenceDirs['classic'] -or $evidenceDirs['film'] -ceq $evidenceDirs['cinematic'])) { throw 'PAIR_SHARED_EVIDENCE the film receipt shares an evidence directory with another leg; three legs have separate evidence' }

$utf8 = [Text.UTF8Encoding]::new($false)
# Stage exactly the bytes that were hashed, one fresh directory per pair, OUTSIDE OutDir; the listings sit beside the staging directories, never inside them.
$stageRoot = Join-Path $stageParent ('.pair-staging-' + [guid]::NewGuid().ToString('N'))
$stageDirs = @{}; $stageListings = @{}; $sliderFiles = @{}
foreach ($label in $sides.Keys) {
    $dir = Join-Path $stageRoot $label
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    foreach ($file in $listedFrames[$label].files) { [IO.File]::WriteAllBytes((Join-Path $dir $file.name), [byte[]]$file.bytes) }
    $stageDirs[$label] = $dir
    $listing = @($listedFrames[$label].files | ForEach-Object { [ordered]@{ name = $_.name; sha256 = $_.sha256 } })
    $stageListings[$label] = Join-Path $stageRoot "$label.listed.json"
    [IO.File]::WriteAllBytes($stageListings[$label], $utf8.GetBytes(([ordered]@{ files = $listing } | ConvertTo-Json -Depth 4)))
    # The sliders as read from the validated evidence: kept in this attempt's own staging directory (a fresh GUID, so no other pair's file is ever
    # overwritten), beside OutDir; an INERT refusal keeps the directory, so its message names files that exist.
    $sliderFiles[$label] = Join-Path $stageRoot "sliders-$label.json"
    $sliderDoc = [ordered]@{ receiptId = $sides[$label].receiptId; lookFlavorReported = $reported[$label] }
    foreach ($k in $sliders[$label].Keys) { $sliderDoc[$k] = $sliders[$label][$k] }
    [IO.File]::WriteAllBytes($sliderFiles[$label], $utf8.GetBytes(($sliderDoc | ConvertTo-Json -Depth 4)))
}

$composer = Join-Path $PSScriptRoot '..\look-flavor-diff.py'
$pyArgs = @('-3', $composer,
    '--classic-frames', $stageDirs['classic'], '--classic-listed', $stageListings['classic'], '--classic-sliders', $sliderFiles['classic'],
    '--classic-flavor-reported', $reported['classic'], '--classic-receipt-id', [string]$classic.receiptId,
    '--cinematic-frames', $stageDirs['cinematic'], '--cinematic-listed', $stageListings['cinematic'], '--cinematic-sliders', $sliderFiles['cinematic'],
    '--cinematic-flavor-reported', $reported['cinematic'], '--cinematic-receipt-id', [string]$cinematic.receiptId,
    '--clip-id', [string]$classic.subject.clipId, '--venue', [string]$classic.venue.name,
    '--build-sha', ([string]$classic.subject.buildManifestSha256).Substring(0, 12), '--out-dir', $OutDir)
if ($trio) {
    # The three-side composer keeps the attempt marker for this script's trio record, as the pair does; it has no --recover-incomplete.
    $pyArgs += @('--film-frames', $stageDirs['film'], '--film-listed', $stageListings['film'], '--film-sliders', $sliderFiles['film'],
        '--film-flavor-reported', $reported['film'], '--film-receipt-id', [string]$film.receiptId, '--keep-marker')
} else {
    $pyArgs += '--keep-marker'
    if ($RecoverIncomplete) { $pyArgs += '--recover-incomplete' }
}
& py @pyArgs
$code = $LASTEXITCODE
# The staging directory is never deleted here: it stays beside OutDir after every outcome (10 names its slider files in the message). Refusals 11-17 are
# raised by the composer before it writes anything into OutDir; an exit it does not define (a crash) also keeps whatever marker the composer left.
if ($code -eq 10 -and $trio) { throw "PAIR_FLAVOR_INERT look-flavor-diff.py refused (FLAVOR_INERT): the cinematic or film flavor was not honoured; sliders in $($sliderFiles['film']), $($sliderFiles['cinematic']) and $($sliderFiles['classic'])" }
if ($code -eq 10) { throw "PAIR_FLAVOR_INERT look-flavor-diff.py refused (FLAVOR_INERT): the cinematic flavor was not honoured; sliders in $($sliderFiles['cinematic']) and $($sliderFiles['classic'])" }
if ($code -eq 16 -or $code -eq 17) {
    $what = $(if ($code -eq 17) { 'PAIR_INCOMPLETE_ATTEMPT look-flavor-diff.py refused (an unfinished attempt owns this -OutDir)' } else { 'PAIR_OUTPUT_EXISTS look-flavor-diff.py refused (another composer or an earlier pair owns this -OutDir)' })
    [Console]::Error.WriteLine("$what; nothing of this attempt was written there")
    exit $code
}
if ($code -ne 0) { throw "PAIR_COMPOSE_FAILED look-flavor-diff.py exited $code" }

$metricsPath = Join-Path $OutDir 'metrics.json'
$metrics = [IO.File]::ReadAllText($metricsPath) | ConvertFrom-Json
if ($trio) {
    $sheet = Join-Path $OutDir 'sheet-classic-cinematic-film.png'
    $scaleOf = { param($r) [ordered]@{ requestedScale = $r.scale.requestedScale; effectiveScale = $r.scale.effectiveScale; verdict = $r.scale.verdict } }
    $record = [ordered]@{
        schema = 'mlv-app/dual-venue-flavor-trio/v1'
        venue = [string]$classic.venue.name; card = $classic.card
        legIds = [ordered]@{ classic = $classic.legId; cinematic = $cinematic.legId; film = $film.legId }
        receiptIds = [ordered]@{ classic = $classic.receiptId; cinematic = $cinematic.receiptId; film = $film.receiptId }
        buildManifestSha256 = $classic.subject.buildManifestSha256; sourceCommit = $classic.metrics.sourceCommit
        sameBuild = $true
        scale = [ordered]@{ classic = (& $scaleOf $classic); cinematic = (& $scaleOf $cinematic); film = (& $scaleOf $film) }
        lookFlavorReported = [ordered]@{ classic = $reported['classic']; cinematic = $reported['cinematic']; film = $reported['film'] }
        lookFlavorHonored = [ordered]@{ classic = $classic.look.lookFlavorHonored; cinematic = $cinematic.look.lookFlavorHonored; film = $film.look.lookFlavorHonored }
        presetGrade = [ordered]@{ classic = $sliders['classic'].presetGrade; cinematic = $sliders['cinematic'].presetGrade; film = $sliders['film'].presetGrade }
        flavorLive = [bool]$metrics.flavorLive; filmLive = [bool]$metrics.filmLive
        frameMatched = [bool]$metrics.frameMatched; maxFrameDelta = $metrics.maxFrameDelta
        means = $metrics.means
        ownerFootage = $true
        advisory = $true
        evidenceStatus = 'ADVISORY'
        advisoryNote = 'all three receipts re-derive from committed consent and hashed run evidence, but no venue-held anchor exists (VENUE_ANCHOR_ABSENT): a diagnostic sheet, never a PASS'
        localOnly = 'never committed, attached to a PR, published to the bus or as an artifact'
        sheet = [ordered]@{ path = $sheet; sha256 = (Get-DvSha256OfFile $sheet); metricsPath = $metricsPath; metricsSha256 = (Get-DvSha256OfFile $metricsPath) }
        pairedBy = 'tile_index'
        createdUtc = [DateTime]::UtcNow.ToString('o')
        owner_verdict = $null
        model_verdicts = @()
    }
    $recordPath = Join-Path $OutDir "flavor-trio-$($classic.legId)-$($cinematic.legId)-$($film.legId)-$($classic.venue.name).json"
    $stream = [IO.File]::Open($recordPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)   # append-only: never overwrite
    try { $bytes = $utf8.GetBytes(($record | ConvertTo-Json -Depth 8) + "`n"); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
    # The trio record is written: the attempt is complete. (A crash before this line leaves the marker, and the staging, in place.)
    Remove-Item -LiteralPath (Join-Path $OutDir $markerName) -Force
    Write-Output "DVE_FLAVOR_TRIO=$sheet"
    Write-Output "DVE_FLAVOR_TRIO_RECORD=$recordPath"
    exit 0
}
$sheet = Join-Path $OutDir 'sheet-classic-vs-cinematic.png'
$record = [ordered]@{
    schema = 'mlv-app/dual-venue-flavor-pair/v1'
    venue = [string]$classic.venue.name; card = $classic.card
    classicLegId = $classic.legId; cinematicLegId = $cinematic.legId
    classicReceiptId = $classic.receiptId; cinematicReceiptId = $cinematic.receiptId
    buildManifestSha256 = $classic.subject.buildManifestSha256; sourceCommit = $classic.metrics.sourceCommit
    sameBuild = $true
    scale = [ordered]@{
        classic = [ordered]@{ requestedScale = $classic.scale.requestedScale; effectiveScale = $classic.scale.effectiveScale; verdict = $classic.scale.verdict }
        cinematic = [ordered]@{ requestedScale = $cinematic.scale.requestedScale; effectiveScale = $cinematic.scale.effectiveScale; verdict = $cinematic.scale.verdict }
    }
    lookFlavorReported = [ordered]@{ classic = $reported['classic']; cinematic = $reported['cinematic'] }
    lookFlavorHonored = [ordered]@{ classic = $classic.look.lookFlavorHonored; cinematic = $cinematic.look.lookFlavorHonored }
    flavorLive = [bool]$metrics.flavorLive
    frameMatched = [bool]$metrics.frameMatched; sameFrames = [bool]$metrics.sameFrames; maxFrameDelta = $metrics.maxFrameDelta
    ownerFootage = $true
    advisory = $true
    evidenceStatus = 'ADVISORY'
    advisoryNote = 'both receipts re-derive from committed consent and hashed run evidence, but no venue-held anchor exists (VENUE_ANCHOR_ABSENT): a diagnostic sheet, never a PASS'
    localOnly = 'never committed, attached to a PR, published to the bus or as an artifact'
    sheet = [ordered]@{ path = $sheet; sha256 = (Get-DvSha256OfFile $sheet); metricsPath = $metricsPath; metricsSha256 = (Get-DvSha256OfFile $metricsPath) }
    pairedBy = 'tile_index'
    createdUtc = [DateTime]::UtcNow.ToString('o')
    owner_verdict = $null
    model_verdicts = @()
}
$recordPath = Join-Path $OutDir "flavor-pair-$($classic.legId)-vs-$($cinematic.legId)-$($classic.venue.name).json"
$stream = [IO.File]::Open($recordPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)   # append-only: never overwrite
try { $bytes = $utf8.GetBytes(($record | ConvertTo-Json -Depth 6) + "`n"); $stream.Write($bytes, 0, $bytes.Length); $stream.Flush($true) } finally { $stream.Dispose() }
# The record is written: the attempt is complete. (A crash before this line leaves the marker, and the staging, in place.)
Remove-Item -LiteralPath (Join-Path $OutDir $markerName) -Force
Write-Output "DVE_FLAVOR_PAIR=$sheet"
Write-Output "DVE_FLAVOR_PAIR_RECORD=$recordPath"
