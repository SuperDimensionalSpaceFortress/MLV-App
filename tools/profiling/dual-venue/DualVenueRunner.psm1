# DualVenueRunner.psm1 -- the testable core of Invoke-VenueLeg.ps1 (DUAL-VENUE-EVIDENCE-1, C2).
#
# Everything here is pure or takes its I/O through an explicit parameter, so the receipt, health,
# role, refusal and outcome rules are executed by test_dual_venue_evidence.py WITHOUT hardware.
# The orchestration (probe -> generate -> submit -> read -> receipt) lives in Invoke-VenueLeg.ps1.
#
# Design: .claude-state/fleet-runs/dual-venue-design-20260930/DESIGN.md (P1-P7, AMENDMENT 1/2).
# Nothing in this file writes to a venue share: share writes go ONLY through tools/profiling/
# um-run.ps1 (NA-7). Reads of a share (summary.json, the artifact index) are plain reads.
#
# Round 2 (owner rule 2026-09-30, docs/playback-clip-length-rule.md): this module never plays the app, never opens a
# clip and never names a path. A leg is a CLIP ID; Get-DvClipAdmission refuses a fixture (master's length gate) and a
# clip the venue has no owner-typed consent record for; a PASS/FAIL receipt must carry the receipt oracle's verdict
# (Get-DvPlaybackEvidence / Test-DvReceiptValid) or it is INVALID.

Set-StrictMode -Version Latest

$script:ReceiptSchema = 'mlv-app/dual-venue-receipt/v1'
# P4: the ONLY outcomes a receipt may carry.
# INVALID (round 2): the leg ran or was captured but the receipt oracle's proof is not in it (under 20 s of source
# frames, a wrap, a foreign run, a fixture); it carries no signal, exactly like DEVICE_UNAVAILABLE.
$script:OutcomeEnum = @('PASS', 'FAIL', 'VENUE_UNHEALTHY', 'VENUE_NOT_QUIESCENT', 'VENUE_HOST_MISMATCH', 'DEVICE_UNAVAILABLE', 'UNRESOLVED', 'RETRACTED', 'INVALID')

function Get-DvOutcomeEnum { $script:OutcomeEnum }

# --- canonical JSON / hashing ---------------------------------------------------------------------
function ConvertTo-DvJsonString([string]$Value) {
    # Matches Python's json.dumps(ensure_ascii=False) string escaping, so a digest is recomputable anywhere.
    $sb = [System.Text.StringBuilder]::new()
    [void]$sb.Append('"')
    foreach ($ch in $Value.ToCharArray()) {
        switch ($ch) {
            '"' { [void]$sb.Append('\"') }
            '\' { [void]$sb.Append('\\') }
            "`n" { [void]$sb.Append('\n') }
            "`r" { [void]$sb.Append('\r') }
            "`t" { [void]$sb.Append('\t') }
            "`b" { [void]$sb.Append('\b') }
            "`f" { [void]$sb.Append('\f') }
            default {
                if ([int]$ch -lt 0x20) { [void]$sb.AppendFormat('\u{0:x4}', [int]$ch) } else { [void]$sb.Append($ch) }
            }
        }
    }
    [void]$sb.Append('"')
    $sb.ToString()
}

function ConvertTo-DvCanonicalJson {
    <#
    .SYNOPSIS
    Compact JSON with recursively sorted object keys: the canonical form every subject digest is taken over.
    #>
    param($Value)
    if ($null -eq $Value) { return 'null' }
    if ($Value -is [bool]) { return $(if ($Value) { 'true' } else { 'false' }) }
    if ($Value -is [string]) { return (ConvertTo-DvJsonString $Value) }
    if ($Value -is [System.ValueType] -and $Value -isnot [System.Enum] -and ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal] -or $Value -is [single] -or $Value -is [int16] -or $Value -is [byte] -or $Value -is [uint32] -or $Value -is [uint64])) {
        return [string]::Format([Globalization.CultureInfo]::InvariantCulture, '{0}', $Value)
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $keys = @($Value.Keys | ForEach-Object { [string]$_ } | Sort-Object -Culture ([Globalization.CultureInfo]::InvariantCulture) -CaseSensitive)
        $parts = foreach ($k in $keys) { (ConvertTo-DvJsonString $k) + ':' + (ConvertTo-DvCanonicalJson $Value[$k]) }
        return '{' + ($parts -join ',') + '}'
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $d = [ordered]@{}
        foreach ($p in $Value.PSObject.Properties) { $d[$p.Name] = $p.Value }
        return (ConvertTo-DvCanonicalJson $d)
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $parts = foreach ($item in $Value) { ConvertTo-DvCanonicalJson $item }
        return '[' + (@($parts) -join ',') + ']'
    }
    return (ConvertTo-DvJsonString ([string]$Value))
}

function Get-DvSha256OfBytes([byte[]]$Bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try { ([BitConverter]::ToString($sha.ComputeHash($Bytes)) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
}
function Get-DvSha256OfText([string]$Text) { Get-DvSha256OfBytes ([Text.Encoding]::UTF8.GetBytes($Text)) }
function Get-DvSha256OfFile([string]$Path) {
    $stream = [IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { ([BitConverter]::ToString($sha.ComputeHash($stream)) -replace '-', '').ToLowerInvariant() } finally { $sha.Dispose() }
    } finally { $stream.Dispose() }
}

function Get-DvSubjectDigest {
    <#
    .SYNOPSIS
    sha256 over the canonical JSON of the subject's identity. Design: buildManifestSha256, legSpecSha256,
    clipId, clipContentSha256. Amendment 1 (backend) and Amendment 2 (lookFlavor) extend it: a cuda and
    a cpu receipt of one leg are different subjects, and so are the two flavors of one LOOK leg.
    #>
    param(
        # Untyped on purpose: a [string] parameter turns $null into "" and the digest would then
        # differ from the receipt's own null fields.
        $BuildManifestSha256, $LegSpecSha256, $ClipId, $ClipContentSha256, $Backend, $LookFlavor
    )
    $identity = [ordered]@{
        backend = $Backend
        buildManifestSha256 = $BuildManifestSha256
        clipContentSha256 = $ClipContentSha256
        clipId = $ClipId
        legSpecSha256 = $LegSpecSha256
        lookFlavor = $LookFlavor
    }
    Get-DvSha256OfText (ConvertTo-DvCanonicalJson $identity)
}

# --- venue table (P3) -----------------------------------------------------------------------------
function Read-DvVenueTable([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "DVE_VENUE_TABLE_MISSING no venue table at the given path" }
    $table = [IO.File]::ReadAllText($Path) | ConvertFrom-Json
    foreach ($name in 'venues', 'roles', 'defaultRole') {
        if ($null -eq $table.PSObject.Properties[$name]) { throw "DVE_VENUE_TABLE_INVALID the venue table lacks '$name'" }
    }
    $table
}

function Get-DvVenueRole {
    <#
    .SYNOPSIS
    The role (acceptance | supplementary) of a venue for a card -- read from the tracked venue table,
    never supplied by a caller (P3). A card or venue the table does not name gets defaultRole.
    #>
    param($Table, [string]$Card, [string]$Venue)
    $role = [string]$Table.defaultRole
    $cardRoles = $Table.roles.PSObject.Properties[$Card]
    if ($null -ne $cardRoles) {
        $venueRole = $cardRoles.Value.PSObject.Properties[$Venue]
        if ($null -ne $venueRole) { $role = [string]$venueRole.Value }
    }
    if ($role -notin @('acceptance', 'supplementary')) { throw "DVE_VENUE_TABLE_INVALID role '$role' is not acceptance|supplementary" }
    $role
}

# --- clips (P7) -----------------------------------------------------------------------------------
$script:FixtureClipIds = @('tiny_dual_iso', 'large_dual_iso')

$script:ClipIdPattern = '^[A-Za-z]\d{2}-\d{3,4}$'
$script:ConsentSchema = 'mlv-app/dual-venue-clip-consent/v1'
$script:ConsentRecordKeys = @('venue', 'clipId', 'ownerLineSha256', 'recordedUtc', 'recordedBy')

function Get-DvProp {
    # A property of a hashtable OR an object, $null when absent (StrictMode-safe). `return ,` keeps an empty array an
    # empty array (it is unrolled once by the caller, so a scalar still arrives as a scalar).
    param($Object, [string]$Name)
    if ($null -eq $Object) { return $null }
    if ($Object -is [System.Collections.IDictionary]) {
        if ($Object.Contains($Name)) { return , $Object[$Name] }
        return $null
    }
    $p = $Object.PSObject.Properties[$Name]
    if ($null -ne $p) { return , $p.Value }
    $null
}

function Read-DvClipConsent {
    <#
    .SYNOPSIS
    Read the tracked, OWNER-WRITTEN per-venue consent file (tools/profiling/dual-venue/venue-clip-consent.json). The
    hub records an owner-typed CLIP line there as a record keyed by venue + clip id (the line itself only as its
    sha256); agents never write it and this function only ever reads it. FAIL CLOSED: a missing file, bad JSON, a wrong
    schema or any record that is not exactly the five reviewed keys (so a path cannot ride along in an extra field)
    makes the WHOLE file invalid -- unknown consent is no consent.
    Returns [pscustomobject]@{ ok; reason; records }.
    #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Table)
    $bad = { param($why) [pscustomobject]@{ ok = $false; reason = $why; records = @() } }
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return (& $bad 'the consent file is missing') }
    $text = [IO.File]::ReadAllText($Path)
    # A consent record names a clip by ID. A drive path or a footage file name anywhere in the file is refused outright.
    if ($text -match '(?i)[a-z]:[\\/]|\.mlv\b') { return (& $bad 'the consent file carries a path-shaped value') }
    try { $doc = $text | ConvertFrom-Json } catch { return (& $bad 'the consent file is not valid JSON') }
    if ([string](Get-DvProp $doc 'schema') -ne $script:ConsentSchema) { return (& $bad "the consent file schema is not $($script:ConsentSchema)") }
    if ($null -eq $doc.PSObject.Properties['records']) { return (& $bad 'the consent file has no records array') }
    $venues = @($Table.venues.PSObject.Properties | ForEach-Object { $_.Name })
    $records = @()
    foreach ($r in @($doc.records)) {
        if ($null -eq $r) { return (& $bad 'a consent record is null') }
        $keys = @($r.PSObject.Properties | ForEach-Object { $_.Name })
        if ($keys.Count -ne $script:ConsentRecordKeys.Count -or @($keys | Where-Object { $_ -cnotin $script:ConsentRecordKeys }).Count -gt 0) {
            return (& $bad 'a consent record is not exactly venue, clipId, ownerLineSha256, recordedUtc, recordedBy')
        }
        if ([string]$r.venue -cnotin $venues) { return (& $bad 'a consent record names a venue the venue table does not have') }
        if ([string]$r.clipId -cnotmatch $script:ClipIdPattern) { return (& $bad 'a consent record clipId is not a consented clip id') }
        if ([string]$r.ownerLineSha256 -cnotmatch '^[0-9a-f]{64}$') { return (& $bad 'a consent record ownerLineSha256 is not 64 lowercase hex') }
        if ([string]::IsNullOrWhiteSpace([string]$r.recordedUtc) -or [string]::IsNullOrWhiteSpace([string]$r.recordedBy)) { return (& $bad 'a consent record lacks recordedUtc/recordedBy') }
        $records += $r
    }
    [pscustomobject]@{ ok = $true; reason = $null; records = $records }
}

function Get-DvClipAdmission {
    <#
    .SYNOPSIS
    The ONE gate that decides whether a leg may play a clip on a venue, run BEFORE anything is generated or submitted.
    A leg is addressed by CONSENTED CLIP ID only (never a path). Refusals are typed and path-free:
      FIXTURE_REFUSED_<verdict>            a tracked fixture (2 and 16 frames) can never satisfy 20 s of distinct source
                                           frames; <verdict> is master's own length gate's (CLIP_TOO_SHORT), so a fixture
                                           is refused by the same gate that refuses a short clip anywhere else, never looped
      CLIP_ID_INVALID                      not a consented clip id
      VENUE_CLIP_CONSENT_INVALID           the per-venue consent file is missing/malformed (fail closed)
      VENUE_CLIP_CONSENT_ABSENT            no owner-typed record for THIS venue + this clip id (consent on one venue
                                           never implies the other)
      OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2   the reviewed switch venues.json ownerFootage.cleanupClassGone is false
    #>
    param(
        [Parameter(Mandatory)][string]$ClipId, [Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)]$Table,
        [Parameter(Mandatory)][string]$ConsentPath, [Parameter(Mandatory)][string]$RepoRoot
    )
    $refuse = { param($token) [pscustomobject]@{ admitted = $false; reason = $token } }
    if ($script:FixtureClipIds -ccontains $ClipId) {
        $verdict = 'NOT_A_CONSENTED_CLIP'
        $gate = Join-Path $PSScriptRoot '..\gui-smoke-length-gate.ps1'
        $header = Join-Path (Join-Path (Join-Path (Join-Path $RepoRoot 'tests') 'fixtures') 'clips') ($ClipId + '.' + 'mlv')
        if (Test-Path -LiteralPath $gate -PathType Leaf) {
            . $gate
            $g = Test-GuiSmokeClipLength -Path $header -WindowSeconds 20
            if ($g.verdict -ne 'OK') { $verdict = [string]$g.verdict }
        }
        return (& $refuse "FIXTURE_REFUSED_$verdict")
    }
    if ($ClipId -cnotmatch $script:ClipIdPattern) { return (& $refuse 'CLIP_ID_INVALID') }
    $consent = Read-DvClipConsent -Path $ConsentPath -Table $Table
    if (-not $consent.ok) { return (& $refuse 'VENUE_CLIP_CONSENT_INVALID') }
    $record = $null
    foreach ($r in $consent.records) {
        if ([string]$r.venue -ceq $Venue -and [string]$r.clipId -ceq $ClipId) { $record = $r; break }
    }
    if ($null -eq $record) { return (& $refuse 'VENUE_CLIP_CONSENT_ABSENT') }
    $refusal = 'OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2'
    if ($null -ne $Table.PSObject.Properties['ownerFootage']) { $refusal = [string]$Table.ownerFootage.refusal }
    $cleanupGone = ($null -ne $Table.PSObject.Properties['ownerFootage']) -and [bool]$Table.ownerFootage.cleanupClassGone
    if (-not $cleanupGone) { return (& $refuse $refusal) }
    [pscustomobject]@{ admitted = $true; reason = $null }
}

# --- the receipt oracle's verdict, carried in the receipt (round 2) -------------------------------------
function Get-DvPlaybackProblems {
    <#
    .SYNOPSIS
    Judge the play-proof block of a receipt. EVERY field must be PRESENT (an absent field is INVALID, never "no wrap" or
    "pace unchecked"); the verdict is re-derived from the fields, never taken from a stored `valid`. Returns the list of
    problems (empty = valid). This repeats, at the receipt, the rules master's job oracle
    (Get-AttrCudaSourceFramesVerdict) already applied: source_advanced >= required_source_frames, no wrap, THIS run's
    nonce, and a clip that is the leg's clip and not a fixture rehearsal.
    #>
    param($Playback, [string]$ExpectedClipId)
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($null -eq $Playback) { $reasons.Add('RECEIPT_FIELD_ABSENT: the receipt carries no source-frame verdict (playback)'); return $reasons.ToArray() }
    $advanced = $null; $required = $null
    $parsed = 0L
    $rawAdvanced = Get-DvProp $Playback 'sourceAdvanced'
    $rawRequired = Get-DvProp $Playback 'requiredSourceFrames'
    if ($null -ne $rawAdvanced -and $rawAdvanced -isnot [bool] -and [int64]::TryParse([string]$rawAdvanced, [ref]$parsed)) { $advanced = $parsed }
    $parsed = 0L
    if ($null -ne $rawRequired -and $rawRequired -isnot [bool] -and [int64]::TryParse([string]$rawRequired, [ref]$parsed)) { $required = $parsed }
    if ($null -eq $advanced) { $reasons.Add('RECEIPT_FIELD_ABSENT: source_advanced') }
    if ($null -eq $required) { $reasons.Add('RECEIPT_FIELD_ABSENT: required_source_frames') }
    elseif ($required -lt 20) { $reasons.Add("INVALID_SOURCE_FRAMES: required_source_frames=$required is under the 20-frame floor; the admitted Play was shorter than 20 s of footage") }
    if ($null -ne $advanced -and $null -ne $required) {
        if ($advanced -lt $required) { $reasons.Add("INVALID_SOURCE_FRAMES: source_advanced=$advanced is under required_source_frames=$required; under 20 s of real footage is never evidence") }
    }
    $wrapped = Get-DvProp $Playback 'wrapped'
    if ($wrapped -isnot [bool]) { $reasons.Add('RECEIPT_FIELD_ABSENT: wrapped') }
    elseif ($wrapped) { $reasons.Add('INVALID_LOOPED: the timeline wrapped, jumped to the first frame or restarted') }
    $failures = Get-DvProp $Playback 'failures'
    if ($null -eq $failures) { $reasons.Add('RECEIPT_FIELD_ABSENT: failures') }
    else { foreach ($f in @($failures)) { if ($null -ne $f -and [string]$f -ne '') { $reasons.Add("the job's own oracle reported: $f") } } }
    $nonce = [string](Get-DvProp $Playback 'runNonce')
    if ($nonce -notmatch '^n[0-9a-f]{32}$') { $reasons.Add('RECEIPT_NOT_THIS_RUN: the receipt carries no well-formed run nonce, so it cannot be shown to be this run''s') }
    $rehearsal = Get-DvProp $Playback 'fixtureRehearsal'
    if ($rehearsal -ne $false) { $reasons.Add('FIXTURE_REHEARSAL: the job did not report fixtureRehearsal=false; a fixture rehearsal is never venue playback evidence') }
    $jobClip = [string](Get-DvProp $Playback 'clipId')
    if ([string]::IsNullOrWhiteSpace($jobClip)) { $reasons.Add('RECEIPT_FIELD_ABSENT: clipId') }
    elseif ($jobClip -cne $ExpectedClipId) { $reasons.Add('CLIP_MISMATCH: the job reports a different clip id than the leg names') }
    $reasons.ToArray()
}

function Get-DvPlaybackEvidence {
    <#
    .SYNOPSIS
    Build the receipt's `playback` block from the job's summary.json (`sourceFrames`, `fixtureRehearsal`, `clipId`) and
    evidence manifest (`smokeRunLog.runNonce`), copied verbatim, plus the derived verdict.
    #>
    param($Summary, $EvidenceManifest, [Parameter(Mandatory)][string]$ExpectedClipId)
    $sf = Get-DvProp $Summary 'sourceFrames'
    $log = Get-DvProp $EvidenceManifest 'smokeRunLog'
    $pb = [ordered]@{
        oracle = 'the job receipt oracle (Get-AttrCudaSourceFramesVerdict): source_advanced >= required_source_frames, wrapped=0, native pace, no fps override, this run''s nonce'
        sourceAdvanced = (Get-DvProp $sf 'sourceAdvanced')
        requiredSourceFrames = (Get-DvProp $sf 'requiredSourceFrames')
        wrapped = (Get-DvProp $sf 'wrapped')
        failures = (Get-DvProp $sf 'failures')
        runNonce = (Get-DvProp $log 'runNonce')
        fixtureRehearsal = (Get-DvProp $Summary 'fixtureRehearsal')
        clipId = (Get-DvProp $Summary 'clipId')
        valid = $false
        invalidReasons = @()
    }
    $problems = @(Get-DvPlaybackProblems -Playback $pb -ExpectedClipId $ExpectedClipId)
    $pb['valid'] = ($problems.Count -eq 0)
    $pb['invalidReasons'] = $problems
    $pb
}

function Test-DvReceiptValid {
    <#
    .SYNOPSIS
    Is this receipt a well-formed signal? A receipt that says PASS or FAIL must carry the clip id, the clip's content
    hash, a um-run RECEIPT and the receipt oracle's verdict (source_advanced, required_source_frames, run nonce, wrap,
    clip); without them it is INVALID, not PASS/FAIL. Every other outcome carries no signal and needs no proof. Readers
    (Get-VenueEvidence) call this too; the verdict is re-derived from the fields, never from a stored flag.
    #>
    param([Parameter(Mandatory)]$Receipt)
    $reasons = [System.Collections.Generic.List[string]]::new()
    $outcome = [string](Get-DvProp $Receipt 'outcome')
    if ($outcome -in @('PASS', 'FAIL')) {
        $subject = Get-DvProp $Receipt 'subject'
        $clipId = [string](Get-DvProp $subject 'clipId')
        if ([string]::IsNullOrWhiteSpace($clipId)) { $reasons.Add('subject.clipId absent') }
        if ([string](Get-DvProp $subject 'clipContentSha256') -cnotmatch '^[0-9a-f]{64}$') { $reasons.Add('subject.clipContentSha256 absent or not 64 lowercase hex') }
        if ([string](Get-DvProp (Get-DvProp $Receipt 'evidence') 'umRunOutcome') -ne 'RECEIPT') { $reasons.Add('evidence.umRunOutcome is not RECEIPT') }
        foreach ($p in @(Get-DvPlaybackProblems -Playback (Get-DvProp $Receipt 'playback') -ExpectedClipId $clipId)) { $reasons.Add($p) }
    }
    [pscustomobject]@{ valid = ($reasons.Count -eq 0); reasons = $reasons.ToArray() }
}

# --- health (P5) ----------------------------------------------------------------------------------
function Get-DvHealthVerdict {
    <#
    .SYNOPSIS
    Judge a health probe against a venue's thresholds. A missing or unparsable measurement is UNHEALTHY --
    unknown is never healthy.
    #>
    param($Probe, $Thresholds)
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($null -eq $Probe) {
        $reasons.Add('health probe returned no measurements')
    } else {
        $cold = $Probe.PSObject.Properties['pwshColdStartMs']; $hash = $Probe.PSObject.Properties['smallHashMs']
        $disk = $Probe.PSObject.Properties['freeDiskGiB'];     $commit = $Probe.PSObject.Properties['commitUsedGiB']
        $limit = $Probe.PSObject.Properties['commitLimitGiB']
        if ($null -eq $cold -or $null -eq $cold.Value) { $reasons.Add('pwshColdStartMs unknown') }
        elseif ([double]$cold.Value -gt [double]$Thresholds.maxPwshColdStartMs) { $reasons.Add("pwshColdStartMs $($cold.Value) > $($Thresholds.maxPwshColdStartMs)") }
        if ($null -eq $hash -or $null -eq $hash.Value) { $reasons.Add('smallHashMs unknown') }
        elseif ([double]$hash.Value -gt [double]$Thresholds.maxSmallHashMs) { $reasons.Add("smallHashMs $($hash.Value) > $($Thresholds.maxSmallHashMs)") }
        if ($null -eq $disk -or $null -eq $disk.Value) { $reasons.Add('freeDiskGiB unknown') }
        elseif ([double]$disk.Value -lt [double]$Thresholds.minFreeDiskGiB) { $reasons.Add("freeDiskGiB $($disk.Value) < $($Thresholds.minFreeDiskGiB)") }
        if ($null -eq $commit -or $null -eq $commit.Value) { $reasons.Add('commitUsedGiB unknown') }
        elseif ($null -eq $limit -or $null -eq $limit.Value -or [double]$limit.Value -le 0) { $reasons.Add('commitLimitGiB unknown') }
        elseif (([double]$commit.Value / [double]$limit.Value) -gt [double]$Thresholds.maxCommitUsedFraction) { $reasons.Add("commit used $($commit.Value) of $($limit.Value) GiB exceeds fraction $($Thresholds.maxCommitUsedFraction)") }
    }
    [pscustomobject]@{ healthy = ($reasons.Count -eq 0); reasons = @($reasons) }
}

function New-DvHealthProbeJobText {
    <#
    .SYNOPSIS
    The job that runs ON the venue for the health probe: pwsh cold start, write+hash of a fixed 4 MiB
    buffer inside the agent root, free disk of the agent-root drive, commit charge, host identity, GPU,
    and the PresentMon digest. Prints ONE line `DVE_PROBE=<json>`. Touches only the agent root; it
    enumerates no drive root and no directory.
    #>
    param([Parameter(Mandatory)][string]$AgentRoot)
    $root = $AgentRoot.Replace("'", "''")
    @"
`$ErrorActionPreference = 'Stop'
`$AgentRoot = '$root'
`$probe = [ordered]@{ schema = 'mlv-app/dual-venue-health-probe/v1' }
`$sw = [Diagnostics.Stopwatch]::StartNew()
& "`$env:ProgramFiles\PowerShell\7\pwsh.exe" -NoLogo -NoProfile -Command '1' | Out-Null
`$probe.pwshColdStartMs = [int]`$sw.ElapsedMilliseconds
`$fixed = New-Object byte[] (4MB)
for (`$i = 0; `$i -lt `$fixed.Length; `$i += 4096) { `$fixed[`$i] = [byte](`$i / 4096 % 251) }
`$scratch = Join-Path `$AgentRoot ('dve-health-' + [guid]::NewGuid().ToString('N') + '.bin')
`$sw.Restart()
[IO.File]::WriteAllBytes(`$scratch, `$fixed)
`$null = (Get-FileHash -LiteralPath `$scratch -Algorithm SHA256).Hash
`$probe.smallHashMs = [int]`$sw.ElapsedMilliseconds
Remove-Item -LiteralPath `$scratch -Force
`$drive = (Split-Path -Qualifier `$AgentRoot).TrimEnd(':')
`$probe.freeDiskGiB = [math]::Round((Get-PSDrive -Name `$drive).Free / 1GB, 1)
`$os = Get-CimInstance -ClassName Win32_OperatingSystem
`$probe.commitUsedGiB = [math]::Round((`$os.TotalVirtualMemorySize - `$os.FreeVirtualMemory) / 1MB, 1)
`$probe.commitLimitGiB = [math]::Round(`$os.TotalVirtualMemorySize / 1MB, 1)
`$probe.hostName = [string]`$env:COMPUTERNAME
`$gpus = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction SilentlyContinue)
`$probe.gpuNames = @(`$gpus | ForEach-Object { [string]`$_.Name })
`$probe.driverVersion = `$(if (`$gpus.Count -gt 0) { [string](`$gpus | Select-Object -First 1).DriverVersion } else { `$null })
`$probe.displayDevice = `$null
try { Add-Type -AssemblyName System.Windows.Forms; `$probe.displayDevice = [string][System.Windows.Forms.Screen]::PrimaryScreen.DeviceName } catch { }
`$pm = Join-Path `$AgentRoot 'cache\PresentMon-2.5.1-x64.exe'
`$probe.presentmonSha256 = `$(if (Test-Path -LiteralPath `$pm -PathType Leaf) { (Get-FileHash -LiteralPath `$pm -Algorithm SHA256).Hash.ToLowerInvariant() } else { `$null })
Write-Output ('DVE_PROBE=' + (`$probe | ConvertTo-Json -Compress -Depth 4))
"@
}

function ConvertFrom-DvProbeStdout([string]$Stdout) {
    foreach ($line in ($Stdout -split "`r?`n")) {
        if ($line.StartsWith('DVE_PROBE=')) {
            try { return ($line.Substring('DVE_PROBE='.Length) | ConvertFrom-Json) } catch { return $null }
        }
    }
    $null
}

# --- registry protection (LESSON from UM-HFR-SUPPLEMENTARY-SMOKE-1) --------------------------------
function New-DvRegSnapshotJobText {
    param([Parameter(Mandatory)][string]$AgentRoot, [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9._-]+$')][string]$Tag)
    $root = $AgentRoot.Replace("'", "''")
    @"
`$ErrorActionPreference = 'Stop'
`$dir = Join-Path '$root' 'dve-reg'
New-Item -ItemType Directory -Force -Path `$dir | Out-Null
`$snap = Join-Path `$dir '$Tag.reg'
`$absent = Join-Path `$dir '$Tag.absent'
reg query 'HKCU\Software\magiclantern.MLVApp' 2>&1 | Out-Null
if (`$LASTEXITCODE -eq 0) {
    reg export 'HKCU\Software\magiclantern.MLVApp' `$snap /y 2>&1 | Out-Null
    if (`$LASTEXITCODE -ne 0) { Write-Output 'DVE_REG_SNAPSHOT ok=False'; exit 3 }
    Write-Output ('DVE_REG_SNAPSHOT ok=True existed=True sha256=' + (Get-FileHash -LiteralPath `$snap -Algorithm SHA256).Hash.ToLowerInvariant())
} else {
    Set-Content -LiteralPath `$absent -Value 'absent' -Encoding ascii
    Write-Output 'DVE_REG_SNAPSHOT ok=True existed=False sha256=none'
}
"@
}

function New-DvRegRestoreJobText {
    <# Restores the snapshot taken by New-DvRegSnapshotJobText and verifies it WITHOUT ever printing a value. #>
    param([Parameter(Mandatory)][string]$AgentRoot, [Parameter(Mandatory)][ValidatePattern('^[A-Za-z0-9._-]+$')][string]$Tag)
    $root = $AgentRoot.Replace("'", "''")
    @"
`$ErrorActionPreference = 'Stop'
`$dir = Join-Path '$root' 'dve-reg'
`$snap = Join-Path `$dir '$Tag.reg'
`$absent = Join-Path `$dir '$Tag.absent'
`$key = 'HKCU\Software\magiclantern.MLVApp'
if (Test-Path -LiteralPath `$absent) {
    reg delete `$key /f 2>&1 | Out-Null
    reg query `$key 2>&1 | Out-Null
    Write-Output ('DVE_REG_RESTORE restored=' + (`$LASTEXITCODE -ne 0))
    exit 0
}
if (-not (Test-Path -LiteralPath `$snap)) { Write-Output 'DVE_REG_RESTORE restored=False reason=no-snapshot'; exit 4 }
reg delete `$key /f 2>&1 | Out-Null
reg import `$snap 2>&1 | Out-Null
`$check = Join-Path `$dir ('$Tag' + '.verify.reg')
reg export `$key `$check /y 2>&1 | Out-Null
`$same = ((Get-FileHash -LiteralPath `$check -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath `$snap -Algorithm SHA256).Hash)
Remove-Item -LiteralPath `$check -Force -ErrorAction SilentlyContinue
Write-Output ('DVE_REG_RESTORE restored=' + `$same)
"@
}

function ConvertFrom-DvMarkerLine {
    # 'DVE_REG_RESTORE restored=True' -> @{ restored = 'True' }
    param([string]$Stdout, [string]$Marker)
    foreach ($line in ($Stdout -split "`r?`n")) {
        if ($line.StartsWith($Marker + ' ')) {
            $h = @{}
            foreach ($m in [regex]::Matches($line, '(?<k>[A-Za-z0-9_]+)=(?<v>\S+)')) { $h[$m.Groups['k'].Value] = $m.Groups['v'].Value }
            return $h
        }
    }
    $null
}

# --- job result -> typed outcome (P4) -------------------------------------------------------------
$script:VenueConditionResults = @('SCREENSAVER_SECURE_OWNER_ONLY', 'DISPLAY_WAKE_DISMISS_FAILED', 'KEEPALIVE_FAILED', 'DISPLAY_ASLEEP')
$script:DeviceUnavailableResults = @('BACKEND_NOT_AVAILABLE')
# A FIXTURE_REHEARSAL_CAPTURED job is NOT a capture of venue playback (fixtures are never played on a venue); only
# MEASUREMENT_CAPTURED can become PASS/FAIL. A rehearsal result falls through to FAIL ... and then to INVALID below.
$script:CapturedResults = @('MEASUREMENT_CAPTURED')
# The job's typed smoke refusals (generator: $smokeRefusalReason) that mean "this was not >= 20 s of real footage".
$script:PlayLengthRefusalReasons = @('PLAY_WINDOW_TOO_SHORT', 'PLAY_DURATION_TOO_SHORT', 'PLAY_PACE_TOO_SLOW', 'CLIP_TOO_SHORT', 'CLIP_LENGTH_UNKNOWN',
    'INVALID_SOURCE_FRAMES', 'INVALID_LOOPED', 'SOURCE_FRAMES_SHORT', 'PLAY_SAFETY_TIMEOUT', 'REPLAY_REFUSED', 'PASS_THROUGH_REFUSED')

function Get-DvResultToken([string]$Stdout) {
    foreach ($line in ($Stdout -split "`r?`n")) {
        if ($line -match '^RESULT=(?<t>[A-Z0-9_]+)') { return $Matches['t'] }
    }
    $null
}

function Resolve-DvJobOutcome {
    <#
    .SYNOPSIS
    Map a job's RESULT token to a P4 outcome. Captured -> PASS/FAIL is decided by the caller from the
    leg's criteria (this returns 'CAPTURED'). Venue conditions are venue outcomes, not product FAILs.
    #>
    param([string]$ResultToken, [int]$ExitCode, [string]$SmokeRefusalReason = '')
    if ([string]::IsNullOrEmpty($ResultToken)) { return [pscustomobject]@{ outcome = 'FAIL'; detail = "job exited $ExitCode with no RESULT line" } }
    if ($ResultToken -in $script:CapturedResults) { return [pscustomobject]@{ outcome = 'CAPTURED'; detail = $ResultToken } }
    # Round 2: the job's own play-length / source-frame refusals are not product results. A run that could not show >= 20 s
    # of distinct source frames (short, looped, replayed, foreign, too slow) is INVALID evidence, never a FAIL.
    $reasonSuffix = $(if ([string]::IsNullOrEmpty($SmokeRefusalReason) -or $SmokeRefusalReason -eq 'NONE') { '' } else { " $SmokeRefusalReason" })
    if ($ResultToken -eq 'FIXTURE_REHEARSAL_CAPTURED') { return [pscustomobject]@{ outcome = 'INVALID'; detail = 'FIXTURE_REHEARSAL_CAPTURED: a fixture rehearsal is never venue playback evidence' } }
    if ($ResultToken -eq 'SOURCE_FRAMES_INVALID') { return [pscustomobject]@{ outcome = 'INVALID'; detail = ($ResultToken + $(if ($reasonSuffix) { $reasonSuffix } else { ' INVALID_SOURCE_FRAMES' })) } }
    if ($reasonSuffix -and $SmokeRefusalReason -in $script:PlayLengthRefusalReasons -and $ResultToken -notin $script:VenueConditionResults -and $ResultToken -notin $script:DeviceUnavailableResults) {
        return [pscustomobject]@{ outcome = 'INVALID'; detail = ($ResultToken + $reasonSuffix) }
    }
    if ($ResultToken -eq 'VENUE_NOT_QUIESCENT') { return [pscustomobject]@{ outcome = 'VENUE_NOT_QUIESCENT'; detail = $ResultToken } }
    if ($ResultToken -eq 'VENUE_HOST_MISMATCH') { return [pscustomobject]@{ outcome = 'VENUE_HOST_MISMATCH'; detail = $ResultToken } }
    if ($ResultToken -in $script:DeviceUnavailableResults) { return [pscustomobject]@{ outcome = 'DEVICE_UNAVAILABLE'; detail = $ResultToken } }
    if ($ResultToken -in $script:VenueConditionResults) { return [pscustomobject]@{ outcome = 'VENUE_UNHEALTHY'; detail = $ResultToken } }
    [pscustomobject]@{ outcome = 'FAIL'; detail = $ResultToken }
}

function Test-DvCriteria {
    <#
    .SYNOPSIS
    Evaluate a list of {metric, op, value} criteria against the verbatim-copied metrics. An absent metric
    FAILS the criterion -- a missing number is never a passing number. An empty list is "no gating
    criteria" (informational), reported as such.
    #>
    param($Criteria, $Metrics)
    # (An empty JSON array reaches here as $null; @($null) would be ONE null criterion.)
    $criteriaList = @($Criteria | Where-Object { $null -ne $_ })
    if ($criteriaList.Count -eq 0) { return [pscustomobject]@{ pass = $true; informational = $true; failures = @() } }
    $failures = [System.Collections.Generic.List[string]]::new()
    foreach ($c in $criteriaList) {
        $prop = $null
        if ($null -ne $Metrics) { $prop = $Metrics.PSObject.Properties[[string]$c.metric] }
        if ($null -eq $prop -or $null -eq $prop.Value) { $failures.Add("$($c.metric): metric absent"); continue }
        $actual = $prop.Value; $want = $c.value; $ok = $false
        switch ([string]$c.op) {
            'eq' { $ok = ($actual -eq $want) }
            'ne' { $ok = ($actual -ne $want) }
            'gt' { $ok = ([double]$actual -gt [double]$want) }
            'ge' { $ok = ([double]$actual -ge [double]$want) }
            'lt' { $ok = ([double]$actual -lt [double]$want) }
            'le' { $ok = ([double]$actual -le [double]$want) }
            default { $failures.Add("$($c.metric): unknown op '$($c.op)'"); continue }
        }
        if (-not $ok) { $failures.Add("$($c.metric) $($c.op) $want failed (actual $actual)") }
    }
    [pscustomobject]@{ pass = ($failures.Count -eq 0); informational = $false; failures = @($failures) }
}

function Get-DvVerbatimMetrics {
    <#
    .SYNOPSIS
    Copy metrics VERBATIM from a job's summary.json: its top-level scalar fields, and (when given) the
    evidence manifest's presentMonStats. Nothing is recomputed, renamed or rounded.
    #>
    param($Summary, $EvidenceManifest)
    $m = [ordered]@{}
    if ($null -ne $Summary) {
        foreach ($p in $Summary.PSObject.Properties) {
            $v = $p.Value
            if ($null -eq $v -or $v -is [string] -or $v -is [bool] -or $v -is [int] -or $v -is [long] -or $v -is [double] -or $v -is [decimal]) { $m[$p.Name] = $v }
        }
    }
    if ($null -ne $EvidenceManifest -and $null -ne $EvidenceManifest.PSObject.Properties['presentMonStats']) {
        $m['presentMonStats'] = $EvidenceManifest.presentMonStats
    }
    [pscustomobject]$m
}

# --- receipts (append-only) -----------------------------------------------------------------------
function New-DvReceipt {
    <#
    .SYNOPSIS
    A receipt skeleton with EVERY schema field present (null where not yet known), so a receipt written on
    any terminal path has the same shape. owner_verdict is optional and never waited on (Amendment 2);
    model_verdicts starts empty (shape per B4; filled by the judge card, never by this runner).
    #>
    param(
        [Parameter(Mandatory)][string]$Card, [Parameter(Mandatory)][string]$LegId,
        [Parameter(Mandatory)][string]$DeclaredVenue, [string]$Role, [string]$Actor,
        [string]$Method, [string]$MethodBlobId, $Backend, $LookFlavor   # untyped: a null flavor stays null, never ""
    )
    [ordered]@{
        schema = $script:ReceiptSchema
        receiptId = [guid]::NewGuid().ToString()
        card = $Card
        legId = $LegId
        subject = [ordered]@{
            digest = $null; buildManifestSha256 = $null; legSpecSha256 = $null; clipId = $null; clipContentSha256 = $null
            backend = $Backend; lookFlavor = $LookFlavor
        }
        venue = [ordered]@{
            name = $DeclaredVenue; role = $Role; declared = $DeclaredVenue; detected = $null; hostName = $null
            gpuNames = @(); driverVersion = $null; displayDevice = $null
            instrumentDigests = [ordered]@{ presentmon = $null; scorer = $null }
        }
        actor = $Actor
        method = [ordered]@{ script = $Method; gitBlobId = $MethodBlobId }
        startedUtc = [DateTime]::UtcNow.ToString('o')
        finishedUtc = $null
        health = [ordered]@{ outcome = $null; pwshColdStartMs = $null; smallHashMs = $null; freeDiskGiB = $null; commitUsedGiB = $null; commitLimitGiB = $null }
        outcome = $null
        outcomeDetail = $null
        evidence = [ordered]@{ summaryJsonSha256 = $null; evidenceManifestSha256 = $null; artifactIndexPath = $null; umRunOutcome = $null }
        metrics = $null
        # Round 2: the receipt oracle's verdict (source_advanced / required_source_frames / run nonce / wrap / clip id).
        # A receipt that says PASS or FAIL without a valid one is INVALID (Test-DvReceiptValid).
        playback = $null
        look = $null
        registry = $null
        refusal = $null
        owner_verdict = $null
        model_verdicts = @()
    }
}

function Write-DvReceipt {
    <#
    .SYNOPSIS
    Finalise and write a receipt: stamps finishedUtc, validates the outcome against the P4 enum, and creates
    the file with CreateNew -- an existing receipt is NEVER overwritten (append-only; a repeat receiptId throws).
    Returns the path written.
    #>
    param([Parameter(Mandatory)]$Receipt, [Parameter(Mandatory)][string]$ReceiptRoot)
    if ($Receipt['outcome'] -notin $script:OutcomeEnum) { throw "DVE_RECEIPT_OUTCOME_INVALID '$($Receipt['outcome'])' is not one of: $($script:OutcomeEnum -join ', ')" }
    if ($Receipt['outcome'] -in @('PASS', 'FAIL')) {
        # The writer never records a signal without its proof, whatever the caller believed.
        $validity = Test-DvReceiptValid -Receipt $Receipt
        if (-not $validity.valid) { throw "DVE_RECEIPT_INVALID a $($Receipt['outcome']) receipt without a valid receipt-oracle verdict is refused: $($validity.reasons -join '; ')" }
    }
    if (-not $Receipt['finishedUtc']) { $Receipt['finishedUtc'] = [DateTime]::UtcNow.ToString('o') }
    $dir = Join-Path (Join-Path (Join-Path $ReceiptRoot $Receipt['card']) $Receipt['legId']) $Receipt['venue']['name']
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $path = Join-Path $dir ($Receipt['receiptId'] + '.json')
    $json = ($Receipt | ConvertTo-Json -Depth 12)
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($json + "`n")
    $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)   # CreateNew: never overwrite
    try { $stream.Write($bytes, 0, $bytes.Length) } finally { $stream.Dispose() }
    $path
}

Export-ModuleMember -Function Get-DvOutcomeEnum, ConvertTo-DvCanonicalJson, Get-DvSha256OfBytes, Get-DvSha256OfText, Get-DvSha256OfFile,
    Get-DvSubjectDigest, Read-DvVenueTable, Get-DvVenueRole, Get-DvProp, Read-DvClipConsent, Get-DvClipAdmission, Get-DvPlaybackProblems,
    Get-DvPlaybackEvidence, Test-DvReceiptValid, Get-DvHealthVerdict,
    New-DvHealthProbeJobText, ConvertFrom-DvProbeStdout, New-DvRegSnapshotJobText, New-DvRegRestoreJobText, ConvertFrom-DvMarkerLine,
    Get-DvResultToken, Resolve-DvJobOutcome, Test-DvCriteria, Get-DvVerbatimMetrics, New-DvReceipt, Write-DvReceipt
