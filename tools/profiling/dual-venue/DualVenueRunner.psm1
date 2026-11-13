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
#
# Round 3 (formal keys r1): (1) the verdict is RE-DERIVED from the run's own log + the launcher's result.json and judged against
# the nonce the real launcher mints (32 lowercase hex), with native fps / pace / override / the 20 s floor in the receipt;
# (2) production admission reads the consent file and the venue table ONLY as committed at HEAD (Resolve-DvAdmissionSources),
# each consent record carrying the owner's exact typed line, and the receipt records the blob ids it was admitted on.
#
# DUAL-VENUE-EVIDENCE-2 round 1 (sol r2 BLOCKER on PR #207: the validator believed a hand-built production PASS): a receipt is valid
# ONLY when EVERY claim is re-derived from a COMMITTED blob or a HASHED artifact, and no self-asserted field is ever an input to the verdict
# (a claim no hashed artifact carries is recorded as UNBOUND, never used). Round 2 narrows what "valid" can mean in production: see below.
# Test-DvReceiptValid -RepoRoot -EvidenceDir reads the consent / venue-table / leg-spec blobs from git (they must be real blobs, committed
# at the commit the receipt names), re-hashes the run's evidence files (summary, manifest, launcher result, run log, um-run record) and
# re-derives the playback block, the metrics and the PASS/FAIL outcome from them with the writer's own parser. Absent evidence is
# INCOMPLETE. Write-DvReceipt runs the SAME validator, and so does every reader (New-VenueSheetPair.ps1).
# HONEST LIMIT: this proves a receipt is consistent with committed consent and with the hashed files it names; it cannot prove the
# files came from a venue run (they are local files; there is no venue-side signature), and "committed" is any commit of this repo,
# not "reviewed master" (docs/dual-venue-evidence.md).
#
# DUAL-VENUE-EVIDENCE-2 round 2 (formal keys r1; hub ruling 2026-10-02: THE PRE-COMMITTED NARROWING EXIT IS TAKEN). There is no venue-held
# anchor, so a PRODUCTION receipt is ADVISORY at most: Test-DvReceiptValid returns valid=false, status ADVISORY (when everything re-derives) and
# the typed reason VENUE_ANCHOR_ABSENT; it never returns VERIFIED or a usable PASS for a production receipt. Production PASS verification
# moves to DUAL-VENUE-PASS-PROVENANCE-1 (needs a design step: a venue-signed summary.json, or re-reading \\<venue>\mlv-agent\outbox by jobId).
# DECLARED THREAT MODEL, forward-only. IN scope: our own tools, mislabelling, legacy or other-lane receipts, wrong-leg or wrong-backend pairing,
# edited or stale receipts, line-ending artefacts. OUT of scope (accepted, shown to the owner): a deliberate forger who writes a whole matching
# evidence set. Inside that model: the BACKEND is derived from the hashed summary's own frame counters (never from the receipt), the LEG TYPE and
# look flavor from the same summary, the leg spec / consent / venue table are compared on git-normalised bytes (an autocrlf checkout can neither
# hide a committed spec nor invent one), and the sheet reader takes only frames the hashed contact-frames manifest lists.

Set-StrictMode -Version Latest

$script:ReceiptSchema = 'mlv-app/dual-venue-receipt/v1'
# P4: the ONLY outcomes a receipt may carry.
# INVALID (round 2): the leg ran or was captured but the receipt oracle's proof is not in it (under 20 s of source
# frames, a wrap, a foreign run, a fixture); it carries no signal, exactly like DEVICE_UNAVAILABLE.
# VENUE_TOOLING (round 3): the leg played and its proof is sound, but the VENUE cannot do a post-capture step (a LOOK leg's
# contact sheet needs Python + Pillow on the venue); it is a venue condition, never a product FAIL.
# SCALE_NOT_HONOURED (DVE-SCALE2-LOOK-LEG-1 r2): the leg played and its proof is sound, but the app rendered at a playback scale other than the one the leg's
# spec asked for (or declared it would get), or the scale it rendered at cannot be read: no signal, never a PASS under a scale the run did not render at.
$script:OutcomeEnum = @('PASS', 'FAIL', 'VENUE_UNHEALTHY', 'VENUE_NOT_QUIESCENT', 'VENUE_HOST_MISMATCH', 'DEVICE_UNAVAILABLE', 'UNRESOLVED', 'RETRACTED', 'INVALID', 'VENUE_TOOLING', 'SCALE_NOT_HONOURED')

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

function ConvertTo-DvLfBytes {
    <#
    .SYNOPSIS
    The bytes with every CRLF folded to LF (Latin-1 round trip: no other byte changes). A tracked text file hashes the same on a Windows
    autocrlf checkout (CRLF in the working copy) as it does as the committed blob (LF).
    #>
    param([byte[]]$Bytes)
    $latin1 = [Text.Encoding]::Latin1
    , $latin1.GetBytes($latin1.GetString($Bytes).Replace("`r`n", "`n"))
}

function Get-DvLegSpecSha256 {
    <#
    .SYNOPSIS
    The identity of a leg spec: sha256 of its bytes with line endings normalised (CRLF -> LF). The runner hashes the WORKING copy and
    Find-DvCommittedLegSpec hashes the COMMITTED blob; on this VM's default checkout (system git core.autocrlf=true) those differ only in
    line endings, and a raw-byte comparison would refuse every committed spec (fable r1 B2). Both sides call this one function.
    #>
    param([byte[]]$Bytes)
    Get-DvSha256OfBytes (ConvertTo-DvLfBytes $Bytes)
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
function ConvertFrom-DvVenueTableText([string]$Text) {
    try { $table = $Text | ConvertFrom-Json } catch { throw 'DVE_VENUE_TABLE_INVALID the venue table is not valid JSON' }
    foreach ($name in 'venues', 'roles', 'defaultRole') {
        if ($null -eq $table.PSObject.Properties[$name]) { throw "DVE_VENUE_TABLE_INVALID the venue table lacks '$name'" }
    }
    $table
}

function Read-DvVenueTable([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "DVE_VENUE_TABLE_MISSING no venue table at the given path" }
    ConvertFrom-DvVenueTableText ([IO.File]::ReadAllText($Path))
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
# A record carries the owner's EXACT typed line ("CLIP <venue>: <clip id>": no path, ever), that line's sha256, when it was
# recorded and by whom ('owner' only). The line is checked against the record's own venue + clip id and against its hash.
$script:ConsentRecordKeys = @('venue', 'clipId', 'ownerLine', 'ownerLineSha256', 'recordedUtc', 'recordedBy')
# VENUE-CLIP-CONSENT-BLANKET-1: ONE blanket record kind (consent file top-level `blanketRecords`). It stores the owner's sentence of
# 2026-10-11 (ledger stamp below) verbatim and admits any well-formed clip id on any venue in the table. The sentence's sha256 is PINNED
# here, so only the owner's own words can be a blanket: a record whose sentence does not hash to this value (a one-character edit, a
# re-hashed forgery) makes the WHOLE file invalid -- it admits nothing and there is no fallback to admit. No path, no footage name.
$script:BlanketRecordKeys = @('kind', 'ownerSentence', 'ownerSentenceSha256', 'recordedUtc', 'recordedBy', 'writtenBy', 'source', 'scope')
$script:BlanketOwnerSentenceSha256 = '48dc8dead951aa2eab911db63591b59d238830d2d98c0fd7df346c42b78f9fd6'
$script:BlanketSource = 'ledger 2026-10-11T03:41:42Z'
$script:BlanketScope = 'any clip id, any venue'
$script:ConsentRelativePath = 'tools/profiling/dual-venue/venue-clip-consent.json'
$script:VenueTableRelativePath = 'tools/profiling/dual-venue/venues.json'

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

# --- where admission reads its inputs from (round 3, sol BLOCKER) ----------------------------------------------------
function Get-DvGitTimeoutMilliseconds {
    # CI-DUALVENUE-EVIDENCE-SHARD2-HANG-1: the longest one git call may run. 120 s by default; DV_GIT_TIMEOUT_SECONDS (1..600) overrides it.
    # A shorter bound can only refuse more (a timed-out call is exit -1), never admit anything.
    $seconds = 120
    $raw = [Environment]::GetEnvironmentVariable('DV_GIT_TIMEOUT_SECONDS')
    # r2 (opus P-01): \z, not $: .NET's $ also matches before a final newline, so '30<LF>' was accepted as 30 s.
    if ($raw -match '^[0-9]{1,3}\z' -and [int]$raw -ge 1 -and [int]$raw -le 600) { $seconds = [int]$raw }
    $seconds * 1000
}

$script:DvJobNative = $null
$script:DvJobNativeTried = $false
function Get-DvJobNative {
    # Windows: the four kernel32 job-object calls plus the five that start a process SUSPENDED and read its exit code (CreateProcessW,
    # CreatePipe, SetHandleInformation, ResumeThread, GetExitCodeProcess; Start-DvGitInJob), emitted ONCE per process with Reflection.Emit (about 45 ms; Add-Type's compiler costs 0.5-0.9 s
    # in every pwsh process, which every test process would pay). Every struct is passed as an IntPtr to a raw buffer, so no struct type is emitted.
    # $null elsewhere, or when the emit fails: the caller then has Kill($true) only.
    if (-not $IsWindows) { return $null }
    if ($script:DvJobNativeTried) { return $script:DvJobNative }
    $script:DvJobNativeTried = $true
    try {
        $asm = [Reflection.Emit.AssemblyBuilder]::DefineDynamicAssembly([Reflection.AssemblyName]::new('DvGitJob'), [Reflection.Emit.AssemblyBuilderAccess]::Run)
        $tb = $asm.DefineDynamicModule('DvGitJob').DefineType('DvGitJobNative', 'Public, Class, Abstract, Sealed')
        foreach ($s in @(@('CreateJobObjectW', [IntPtr], @([IntPtr], [IntPtr])), @('AssignProcessToJobObject', [bool], @([IntPtr], [IntPtr])),
                         @('TerminateJobObject', [bool], @([IntPtr], [uint32])), @('CloseHandle', [bool], @([IntPtr])),
                         @('CreateProcessW', [bool], @([IntPtr], [IntPtr], [IntPtr], [IntPtr], [bool], [uint32], [IntPtr], [IntPtr], [IntPtr], [IntPtr])),
                         @('CreatePipe', [bool], @([IntPtr].MakeByRefType(), [IntPtr].MakeByRefType(), [IntPtr], [uint32])),
                         @('SetHandleInformation', [bool], @([IntPtr], [uint32], [uint32])), @('ResumeThread', [uint32], @([IntPtr])),
                         @('GetExitCodeProcess', [bool], @([IntPtr], [uint32].MakeByRefType())))) {
            $m = $tb.DefinePInvokeMethod($s[0], 'kernel32.dll', 'Public, Static, PinvokeImpl', 'Standard', $s[1], [Type[]]$s[2], 'Winapi', 'Unicode')
            $m.SetImplementationFlags('PreserveSig')
        }
        $script:DvJobNative = $tb.CreateType()
    } catch { $script:DvJobNative = $null }
    $script:DvJobNative
}

function Stop-DvGitCall {
    # Kill everything one git call started, including a child whose own parent has already exited. No parent-pid chain leads back to git
    # then (`sh -c 'x & exit 0'`: sh is gone, the background child is not), so Kill($true) alone misses it. The call's job object reaches it
    # on Windows; on Linux its environment tag in /proc/<pid>/environ does (every child inherits the tag unless it scrubs its environment).
    # macOS has neither: it reaches only the direct child and its process tree (Kill($true)), so a child whose parent has already exited
    # survives there. The callers therefore never claim a kill from this function: they claim it only once the call's pipes have closed.
    param([Parameter(Mandatory)]$Proc, [IntPtr]$Job, [string]$Tag)
    try { $Proc.Kill($true) } catch { $null = $_ }
    $native = Get-DvJobNative
    if ($null -ne $native -and $Job -ne [IntPtr]::Zero) { [void]$native::TerminateJobObject($Job, [uint32]1) }
    if ($IsLinux -and -not [string]::IsNullOrEmpty($Tag)) {
        $needle = "`0DV_GIT_CALL_TAG=$Tag`0"
        foreach ($dir in [IO.Directory]::GetDirectories('/proc')) {
            $name = [IO.Path]::GetFileName($dir)
            if ($name -notmatch '^\d{1,9}$' -or [int]$name -eq $PID) { continue }
            try { $envText = "`0" + [Text.Encoding]::Latin1.GetString([IO.File]::ReadAllBytes("$dir/environ")) } catch { continue }
            if ($envText.Contains($needle)) { try { [System.Diagnostics.Process]::GetProcessById([int]$name).Kill() } catch { $null = $_ } }
        }
    }
}

function Get-DvStragglerMessage {
    # The DV_GIT_PIPE_STRAGGLER stderr. "(killed)" is claimed only when the kill is CONFIRMED (the call's pipes closed after Stop-DvGitCall);
    # otherwise "(kill not confirmed)": macOS cannot reach a child whose parent has exited. The refusal (exit -1) is the same either way.
    param([Parameter(Mandatory)][int]$GitExit, [Parameter(Mandatory)][int]$Seconds, [Parameter(Mandatory)][bool]$KillConfirmed, [Parameter(Mandatory)][string[]]$GitArgs)
    $state = $(if ($KillConfirmed) { 'killed' } else { 'kill not confirmed' })
    "DV_GIT_PIPE_STRAGGLER git exited $GitExit but a process it started still held its output open after $Seconds s ($state): git $($GitArgs -join ' ')"
}

function ConvertTo-DvWindowsArg {
    # One argument as CommandLineToArgvW reads it back: quoted when empty or holding a space, tab or quote; a backslash run before a quote
    # (or before the closing quote) is doubled.
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Arg)
    if ($Arg.Length -gt 0 -and $Arg -notmatch '[\s"]') { return $Arg }
    '"' + [regex]::Replace([regex]::Replace($Arg, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

# Test seam, $null in production: runs between the child's creation and its job assignment. The child is suspended there, so however long the
# hook takes it cannot have started anything (a test sets a sleep here to prove the escape window is closed).
$script:DvStartGate = $null

# Test seams, empty in production (DV-GIT-NATIVE-START-TYPED-FAILURE-1). DvNativeFault maps a native call's name (AssignProcessToJobObject,
# ResumeThread, GetExitCodeProcess) to the result to read INSTEAD of the real one; the real call still runs, so the child exists and the
# cleanup is real. DvNewPipeStream builds one parent-side pipe stream from a raw handle; a test swaps in one that throws.
$script:DvNativeFault = @{}
$script:DvNewPipeStream = { param([IntPtr]$h) [System.IO.Pipes.AnonymousPipeClientStream]::new([System.IO.Pipes.PipeDirection]::In, [Microsoft.Win32.SafeHandles.SafePipeHandle]::new($h, $true)) }
# DV-GIT-NATIVE-START-UNASSIGNED-CHILD-CLEANUP-1, also empty in production. DvNativeSkipCall names the native calls (AssignProcessToJobObject,
# CreatePipe, CreateProcessW) whose REAL call is not made when DvNativeFault also names them: the fault then happens BEFORE the call, so a child
# stays genuinely outside the job (the fault alone lets the real assignment run first). DvNativeFaultHits records the names a fault was read
# for. DvCleanupGate, $null in production, is called with (child pid, child process handle, job) as the failure cleanup of Start-DvGitInJob
# begins: it sees the child's state at that moment.
$script:DvNativeSkipCall = @{}
$script:DvNativeFaultHits = [Collections.Generic.List[string]]::new()
$script:DvCleanupGate = $null

function Test-DvNativeSkipped {
    param([Parameter(Mandatory)][string]$Name)
    $script:DvNativeFault.ContainsKey($Name) -and $script:DvNativeSkipCall.ContainsKey($Name)
}

function Resolve-DvNativeResult {
    param([Parameter(Mandatory)][string]$Name, $Real)
    if ($script:DvNativeFault.ContainsKey($Name)) { $script:DvNativeFaultHits.Add($Name); return $script:DvNativeFault[$Name] }
    $Real
}

function Start-DvGitInJob {
    # CI-FLAKE-DV-GIT-PIPE-STRAGGLER-KILL-1: start $Psi's program SUSPENDED, put it in $Job, then resume it, so that nothing git starts can
    # ever be outside the job. Process.Start then AssignProcessToJobObject left a window (measured: 20 ms was enough on an idle host, and
    # `sh -c 'x & exit 0'` was already in the grandchild's hands): a child started inside it escaped TerminateJobObject, its parent exited, and
    # Kill($true) had no parent chain to follow, so the straggler kill was not confirmed on a loaded hosted runner.
    # Returns @{ Failure = $null; Process; Handle; Out; Err } (Handle: the process handle, the caller's to close; Out: stdout stream; Err: stderr
    # reader), or
    #   * $null when NO child exists yet (CreatePipe / SetHandleInformation / CreateProcessW failed, or not 64-bit) with nothing left open: the
    #     caller then takes the Process.Start path, which also produces the typed DV_GIT_START_FAILED message; or
    #   * @{ Failure = <token>; Detail } once a child HAS been created: DV_GIT_START_FAILED (a step before the job assignment threw),
    #     DV_GIT_JOB_ASSIGN_FAILED (AssignProcessToJobObject returned false), DV_GIT_RESUME_FAILED (ResumeThread returned -1 or left the thread
    #     suspended), DV_GIT_STREAM_FAILED (a throw building the pipe streams after the resume). The child is then already terminated (with its job)
    #     and every handle and stream closed, and the caller must NOT start another git: it refuses, exit -1. Each token is its own status and is
    #     never DV_GIT_TIMEOUT. 64-bit only (the buffers below are x64 layouts).
    # LIMIT (F, DV-GIT-NATIVE-START-TYPED-FAILURE-1): CreateProcessW is called with bInheritHandles = TRUE and NO PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
    # so the child inherits every inheritable handle of this process, not only its three pipe ends. The pipe ends are created inheritable (the
    # SECURITY_ATTRIBUTES below) and the parent's ends are made non-inheritable straight after, so today the window is the few calls between
    # CreatePipe and CreateProcessW in ONE thread. Nothing here is thread-safe (the script-scope seams, the counters and the cached $Native are
    # per module), and no caller runs two Invoke-DvGit calls at once. Before a parallel caller exists this MUST get an explicit handle list
    # (InitializeProcThreadAttributeList + UpdateProcThreadAttribute with the three child ends, EXTENDED_STARTUPINFO_PRESENT, STARTUPINFOEXW),
    # or a process-wide lock around create-pipe .. CreateProcessW. Not done here: it needs three more P/Invokes, a STARTUPINFOEXW layout and a
    # test that enumerates the child's handles, and a half-built list is worse than the documented limit.
    param([Parameter(Mandatory)]$Native, [Parameter(Mandatory)][IntPtr]$Job, [Parameter(Mandatory)]$Psi)
    if ([IntPtr]::Size -ne 8) { return $null }
    $m = [Runtime.InteropServices.Marshal]
    $handles = [Collections.Generic.List[IntPtr]]::new()   # every raw handle still ours to close
    $mem = [Collections.Generic.List[IntPtr]]::new()
    $suspendedThread = [IntPtr]::Zero
    $suspendedProcess = [IntPtr]::Zero
    $childPid = 0
    $created = $false
    $step = 'DV_GIT_START_FAILED'   # the token a throw is reported under once the child exists
    $proc = $null; $outStream = $null; $errStream = $null
    try {
        $alloc = { param([int]$n) $p = $m::AllocHGlobal($n); $m::Copy([byte[]]::new($n), 0, $p, $n); $mem.Add($p); $p }
        $sa = & $alloc 24
        $m::WriteInt32($sa, 0, 24); $m::WriteInt32($sa, 16, 1)   # SECURITY_ATTRIBUTES { nLength = 24; bInheritHandle = TRUE }
        $pipe = {
            param([bool]$ParentReads)
            [IntPtr]$r = [IntPtr]::Zero; [IntPtr]$w = [IntPtr]::Zero
            if (-not (Resolve-DvNativeResult 'CreatePipe' $(if (Test-DvNativeSkipped 'CreatePipe') { $false } else { $Native::CreatePipe([ref]$r, [ref]$w, $sa, [uint32]0) }))) { throw 'CreatePipe failed' }
            $handles.Add($r); $handles.Add($w)
            # HANDLE_FLAG_INHERIT off on the parent's end: git inherits its own end only.
            $parent = $(if ($ParentReads) { $r } else { $w })
            if (-not (Resolve-DvNativeResult 'SetHandleInformation' $Native::SetHandleInformation($parent, [uint32]1, [uint32]0))) { throw 'SetHandleInformation failed' }
            @($r, $w)
        }
        $in = & $pipe $false; $out = & $pipe $true; $err = & $pipe $true
        $si = & $alloc 104
        $m::WriteInt32($si, 0, 104); $m::WriteInt32($si, 60, 0x100)   # STARTUPINFOW { cb; dwFlags = STARTF_USESTDHANDLES; hStd* at 80, 88, 96 }
        $m::WriteIntPtr($si, 80, $in[0]); $m::WriteIntPtr($si, 88, $out[1]); $m::WriteIntPtr($si, 96, $err[1])
        $pi = & $alloc 24
        $cmd = (@($Psi.FileName) + @($Psi.ArgumentList | ForEach-Object { $_ }) | ForEach-Object { ConvertTo-DvWindowsArg $_ }) -join ' '
        $keys = [string[]]@($Psi.Environment.Keys); [Array]::Sort($keys, [StringComparer]::OrdinalIgnoreCase)
        $envText = (($keys | ForEach-Object { $_ + '=' + $Psi.Environment[$_] }) -join "`0") + "`0`0"
        $cmdPtr = $m::StringToHGlobalUni($cmd); $mem.Add($cmdPtr)
        $envPtr = $m::StringToHGlobalUni($envText); $mem.Add($envPtr)
        # CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW, the parent's current directory, handles inherited (as Process.Start does).
        if (-not (Resolve-DvNativeResult 'CreateProcessW' $(if (Test-DvNativeSkipped 'CreateProcessW') { $false } else { $Native::CreateProcessW([IntPtr]::Zero, $cmdPtr, [IntPtr]::Zero, [IntPtr]::Zero, $true, [uint32](0x4 -bor 0x400 -bor 0x08000000), $envPtr, [IntPtr]::Zero, $si, $pi) }))) { throw 'CreateProcessW failed' }
        $suspendedProcess = $m::ReadIntPtr($pi, 0); $suspendedThread = $m::ReadIntPtr($pi, 8); $childPid = $m::ReadInt32($pi, 16)
        $created = $true
        $proc = [System.Diagnostics.Process]::GetProcessById($childPid)
        if ($null -ne $script:DvStartGate) { & $script:DvStartGate }
        $step = 'DV_GIT_JOB_ASSIGN_FAILED'
        if (-not (Resolve-DvNativeResult 'AssignProcessToJobObject' $(if (Test-DvNativeSkipped 'AssignProcessToJobObject') { $false } else { $Native::AssignProcessToJobObject($Job, $suspendedProcess) }))) { throw 'AssignProcessToJobObject returned false' }
        $step = 'DV_GIT_RESUME_FAILED'
        # ResumeThread returns the previous suspend count: 1 for the thread CREATE_SUSPENDED made (now running), -1 (0xFFFFFFFF) on failure,
        # and more than 1 when the thread stays suspended.
        $resumed = [uint32](Resolve-DvNativeResult 'ResumeThread' ($Native::ResumeThread($suspendedThread)))
        if ($resumed -eq [uint32]::MaxValue -or $resumed -gt 1) { throw "ResumeThread returned $resumed" }
        $step = 'DV_GIT_STREAM_FAILED'   # from here on the child is RUNNING: any throw terminates it and is typed, never a null
        # The child's ends and the stdin writer close here: the child holds its own copies, and a closed stdin reads EOF at once.
        foreach ($h in @($in[0], $in[1], $out[1], $err[1])) { [void]$Native::CloseHandle($h); [void]$handles.Remove($h) }
        $outStream = & $script:DvNewPipeStream $out[0]
        [void]$handles.Remove($out[0])
        $errStream = & $script:DvNewPipeStream $err[0]
        [void]$handles.Remove($err[0])
        $reader = [System.IO.StreamReader]::new($errStream, [Text.UTF8Encoding]::new($false), $true)
        # The process handle stays open for the caller (Handle): Process.ExitCode does not read for a process opened by id, GetExitCodeProcess does.
        $handle = $suspendedProcess; $suspendedProcess = [IntPtr]::Zero
        [pscustomobject]@{ Failure = $null; Process = $proc; Handle = $handle; Out = $outStream; Err = $reader }
    } catch {
        if ($created) {
            # One cleanup for every failure after the child exists (before: some paths killed the suspended child, the stream path left a running
            # one unwatched): terminate it and everything in its job, close the streams and the Process, and report a typed failure. Never a null,
            # which would make the caller start another git beside this one.
            $detail = $_.Exception.Message
            if ($null -ne $script:DvCleanupGate) { try { & $script:DvCleanupGate $childPid $suspendedProcess $Job } catch { $null = $_ } }
            try { if ($null -ne $proc) { $proc.Kill($true) } else { [System.Diagnostics.Process]::GetProcessById($childPid).Kill($true) } } catch { $null = $_ }
            [void]$Native::TerminateJobObject($Job, [uint32]1)
            try { if ($null -ne $proc) { [void]$proc.WaitForExit(5000) } } catch { $null = $_ }
            foreach ($s in @($errStream, $outStream)) { if ($null -ne $s) { try { $s.Dispose() } catch { $null = $_ } } }
            if ($null -ne $proc) { try { $proc.Dispose() } catch { $null = $_ } }
            [pscustomobject]@{ Failure = $step; Detail = $detail }
        } else {
            $null
        }
    } finally {
        foreach ($h in $handles) { [void]$Native::CloseHandle($h) }
        if ($suspendedThread -ne [IntPtr]::Zero) { [void]$Native::CloseHandle($suspendedThread) }
        if ($suspendedProcess -ne [IntPtr]::Zero) { [void]$Native::CloseHandle($suspendedProcess) }
        foreach ($p in $mem) { $m::FreeHGlobal($p) }
    }
}

function Get-DvExitCode {
    # The exit code of an exited git: Process.ExitCode, except for a process Start-DvGitInJob started (its Process was opened by id, whose
    # ExitCode does not read), where it is GetExitCodeProcess on the handle the call kept. A signed int, as Process.ExitCode is.
    # $null when GetExitCodeProcess fails (DV-GIT-NATIVE-START-TYPED-FAILURE-1 I3: the code starts at 0, so ignoring the return read a failure
    # as git exiting 0); the callers refuse typed (DV_GIT_EXITCODE_FAILED), never admit.
    param([Parameter(Mandatory)]$Proc, $Started, $Native)
    if ($null -eq $Started) { return $Proc.ExitCode }
    [uint32]$code = 0
    if (-not (Resolve-DvNativeResult 'GetExitCodeProcess' ($Native::GetExitCodeProcess($Started.Handle, [ref]$code)))) { return $null }
    [BitConverter]::ToInt32([BitConverter]::GetBytes($code), 0)
}

# The program Invoke-DvGit starts. Module-private on purpose and NOT an environment variable: the caller's environment must never be able to
# swap the git that admission reads from. A test sets it inside the module scope to make Process.Start throw.
$script:DvGitExecutable = 'git'
# Job objects opened and closed by Invoke-DvGit (Windows). Equal after every call; a test reads them to prove no handle leaks.
$script:DvJobsOpened = 0
$script:DvJobsClosed = 0
# Where Invoke-DvGit collects git's stdout. Module-private seam: a test swaps in a sink that faults, to prove a faulted pipe read is typed.
$script:DvGitNewSink = { [IO.MemoryStream]::new() }

function Invoke-DvGit {
    # Run git in $RepoRoot; returns exit code, stdout BYTES (a blob must not be re-encoded or newline-translated) and stderr.
    # Bounded (Get-DvGitTimeoutMilliseconds): a git that does not return is killed with its children and reported as exit -1 with a
    # DV_GIT_TIMEOUT stderr, so the caller refuses typed instead of waiting out the CI job cap. git gets a closed stdin, no prompt and no pager.
    # A git that exits while a process it started still holds its stdout/stderr open is killed the same way and is exit -1 with a
    # DV_GIT_PIPE_STRAGGLER stderr: every bounded failure is exit -1 with a typed stderr, never git's exit code with partial bytes.
    param([Parameter(Mandatory)][string]$RepoRoot, [Parameter(Mandatory)][string[]]$GitArgs)
    $psi = [System.Diagnostics.ProcessStartInfo]::new($script:DvGitExecutable)
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
    $psi.Environment['GIT_TERMINAL_PROMPT'] = '0'; $psi.Environment['GIT_PAGER'] = 'cat'
    # The repo is the one at $RepoRoot and nothing else: a GIT_DIR / GIT_WORK_TREE / GIT_INDEX_FILE / object-store override in the
    # caller's environment would point git at a scratch repo whose "committed" files are whatever the caller wrote, and a replace
    # ref would swap a blob for another one under the same id (--no-replace-objects).
    foreach ($name in @($psi.Environment.Keys | Where-Object { $_ -match '^GIT_(DIR|WORK_TREE|INDEX_FILE|OBJECT_DIRECTORY|ALTERNATE_OBJECT_DIRECTORIES|COMMON_DIR|NAMESPACE|REPLACE_REF_BASE|NO_REPLACE_OBJECTS)$' })) { [void]$psi.Environment.Remove($name) }
    foreach ($a in (@('--no-replace-objects', '-C', $RepoRoot) + $GitArgs)) { [void]$psi.ArgumentList.Add($a) }
    $limit = Get-DvGitTimeoutMilliseconds
    # A fresh tag per call, inherited by everything git starts: how Stop-DvGitCall finds those children on Linux once their parent is gone.
    $tag = [guid]::NewGuid().ToString('N')
    $psi.Environment['DV_GIT_CALL_TAG'] = $tag
    $native = Get-DvJobNative
    $job = [IntPtr]::Zero
    $proc = $null
    $outTask = $null; $errTask = $null
    $pipes = $null   # the raw pipe streams of the suspended-start path: closed here once both reads have completed (Process.Dispose closes the others)
    try {
        # r3 (CoS N3): the job object is created inside this try, so the finally below always closes it, and a Start that throws (git not on
        # PATH) is a typed refusal like every other bounded failure, exit -1, never an exception the caller has not handled. r2 created the job
        # first and started the process outside the try, which leaked the job handle when Start threw.
        if ($null -ne $native) {
            $job = $native::CreateJobObjectW([IntPtr]::Zero, [IntPtr]::Zero)
            if ($job -ne [IntPtr]::Zero) { $script:DvJobsOpened++ }
        }
        # CI-FLAKE-DV-GIT-PIPE-STRAGGLER-KILL-1: on Windows git is started suspended and resumed only once it is in the job (Start-DvGitInJob),
        # so everything it starts is in the job. Process.Start and then Assign (the fallback below) left a window in which a child escaped it.
        $started = $(if ($job -ne [IntPtr]::Zero) { Start-DvGitInJob -Native $native -Job $job -Psi $psi })
        if ($null -ne $started -and $null -ne $started.Failure) {
            # DV-GIT-NATIVE-START-TYPED-FAILURE-1: a child was created and has been terminated and cleaned up. Refuse typed; starting another git
            # here (the Process.Start path below) is exactly what a null used to cause.
            return [pscustomobject]@{ exitCode = -1; bytes = [byte[]]@(); stderr = "$($started.Failure) git was created but could not be started cleanly ($($started.Detail)): git $($GitArgs -join ' ')" }
        }
        if ($null -ne $started) {
            $proc = $started.Process
            $pipes = $started
            $outStream = $started.Out
            $errTask = $started.Err.ReadToEndAsync()
        } else {
            try { $proc = [System.Diagnostics.Process]::Start($psi) }
            catch { return [pscustomobject]@{ exitCode = -1; bytes = [byte[]]@(); stderr = "DV_GIT_START_FAILED git could not be started: $($_.Exception.Message)" } }
            if ($null -ne $script:DvStartGate) { & $script:DvStartGate }
            # Everything git starts from here on is in the job. A child git started in the window before this call would escape it, but the
            # refusals below do not depend on the kill: a timeout or a straggler is exit -1 whether or not its processes could be killed.
            if ($job -ne [IntPtr]::Zero) { [void]$native::AssignProcessToJobObject($job, $proc.Handle) }
            $proc.StandardInput.Close()
            $errTask = $proc.StandardError.ReadToEndAsync()
            $outStream = $proc.StandardOutput.BaseStream
        }
        $ms = & $script:DvGitNewSink
        $outTask = $outStream.CopyToAsync($ms)
        if (-not $proc.WaitForExit($limit)) {
            # try { $proc.Kill($true) } catch { $null = $_ }   (r1; Stop-DvGitCall does this and also reaches children whose parent exited)
            Stop-DvGitCall -Proc $proc -Job $job -Tag $tag
            [void]$proc.WaitForExit(5000)
            return [pscustomobject]@{ exitCode = -1; bytes = [byte[]]@(); stderr = "DV_GIT_TIMEOUT git did not return within $($limit / 1000) s: git $($GitArgs -join ' ')" }
        }
        # r1: "the pipes close when git (and anything it started) exits; a straggler holding one open must not outlast a bounded wait"
        # r1: [void]$outTask.Wait(10000), then git's own exit code with $ms.ToArray(): the wait's result was dropped (CoS I1, PR #472).
        # git has exited, but its pipes close only when every process holding them has exited too. A child that outlives git with stdout or
        # stderr still open is a typed failure, exit -1 (DV_GIT_PIPE_STRAGGLER, a sibling of DV_GIT_TIMEOUT): never git's own exit code with
        # the bytes that happened to arrive, and never ToArray() while CopyToAsync may still be writing. The drain shares the git bound.
        $drain = [Math]::Min(10000, $limit)
        # HARDENING-2 (2): a faulted pipe read makes WaitAll throw (AggregateException). That is a typed failure too, exit -1 DV_GIT_PIPE_FAULT.
        $drained = $false; $fault = $null
        try { $drained = [System.Threading.Tasks.Task]::WaitAll([System.Threading.Tasks.Task[]]@($outTask, $errTask), $drain) } catch { $fault = $_.Exception.GetBaseException() }
        if ($null -ne $fault) {
            Stop-DvGitCall -Proc $proc -Job $job -Tag $tag
            return [pscustomobject]@{ exitCode = -1; bytes = [byte[]]@(); stderr = "DV_GIT_PIPE_FAULT reading git's output failed ($($fault.GetType().Name): $($fault.Message)): git $($GitArgs -join ' ')" }
        }
        if (-not $drained) {
            $gitExit = Get-DvExitCode -Proc $proc -Started $pipes -Native $native
            Stop-DvGitCall -Proc $proc -Job $job -Tag $tag
            if ($null -eq $gitExit) { return [pscustomobject]@{ exitCode = -1; bytes = [byte[]]@(); stderr = "DV_GIT_EXITCODE_FAILED GetExitCodeProcess failed for an exited git (and a process it started held its output open): git $($GitArgs -join ' ')" } }
            # r3 (CoS N4): the kill is confirmed by the evidence that matters, the pipes closing. Closed within 5 s = every holder of the pipe
            # closed its copy of it (KILL-NOT-CONFIRMED-ROW-1: a holder that ended on its own in that window counts, as does one that was killed).
            $confirmed = $false
            try { $confirmed = [System.Threading.Tasks.Task]::WaitAll([System.Threading.Tasks.Task[]]@($outTask, $errTask), 5000) } catch { $null = $_ }
            return [pscustomobject]@{ exitCode = -1; bytes = [byte[]]@(); stderr = (Get-DvStragglerMessage -GitExit $gitExit -Seconds ($drain / 1000) -KillConfirmed $confirmed -GitArgs $GitArgs) }
        }
        $gitExit = Get-DvExitCode -Proc $proc -Started $pipes -Native $native
        if ($null -eq $gitExit) { return [pscustomobject]@{ exitCode = -1; bytes = [byte[]]@(); stderr = "DV_GIT_EXITCODE_FAILED GetExitCodeProcess failed for an exited git: git $($GitArgs -join ' ')" } }
        [pscustomobject]@{ exitCode = $gitExit; bytes = $ms.ToArray(); stderr = $errTask.Result }
    } finally {
        # Only when both reads are done: closing a pipe handle under a blocked read can block, and an unconfirmed straggler still holds it.
        if ($null -ne $pipes -and $null -ne $outTask -and $null -ne $errTask -and $outTask.IsCompleted -and $errTask.IsCompleted) {
            $pipes.Out.Dispose(); $pipes.Err.Dispose()
        }
        if ($null -ne $pipes) { [void]$native::CloseHandle($pipes.Handle) }
        if ($null -ne $proc) { $proc.Dispose() }
        # r2 (opus): the counter moves only on CloseHandle's own true return, so a build that drops the call can no longer pass the open = closed pin.
        if ($job -ne [IntPtr]::Zero -and $native::CloseHandle($job)) { $script:DvJobsClosed++ }
    }
}

function Get-DvCommittedFile {
    <#
    .SYNOPSIS
    The bytes of a tracked file AS COMMITTED at HEAD of $RepoRoot (git rev-parse HEAD:<path> + git cat-file blob), refused
    when the working copy differs from that blob. Returns [pscustomobject]@{ ok; reason; text; blobSha; headSha; lastCommit }.
    reason is a typed token: ADMISSION_SOURCE_NOT_COMMITTED (no HEAD, or the path is not in HEAD) or
    ADMISSION_SOURCE_DIRTY (the working copy is not byte-identical to the committed blob, after git's own normalisation).
    #>
    param([Parameter(Mandatory)][string]$RepoRoot, [Parameter(Mandatory)][string]$RelativePath)
    $bad = { param($token) [pscustomobject]@{ ok = $false; reason = $token; text = $null; blobSha = $null; headSha = $null; lastCommit = $null } }
    $head = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('rev-parse', '--verify', 'HEAD')
    if ($head.exitCode -ne 0) { return (& $bad 'ADMISSION_SOURCE_NOT_COMMITTED') }
    $headSha = ([Text.Encoding]::ASCII.GetString($head.bytes)).Trim()
    $blob = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('rev-parse', '--verify', "HEAD:$RelativePath")
    if ($blob.exitCode -ne 0) { return (& $bad 'ADMISSION_SOURCE_NOT_COMMITTED') }
    $blobSha = ([Text.Encoding]::ASCII.GetString($blob.bytes)).Trim()
    if ($blobSha -cnotmatch '^[0-9a-f]{40}$') { return (& $bad 'ADMISSION_SOURCE_NOT_COMMITTED') }
    $workFile = Join-Path $RepoRoot ($RelativePath -replace '/', '\')
    if (-not (Test-Path -LiteralPath $workFile -PathType Leaf)) { return (& $bad 'ADMISSION_SOURCE_DIRTY') }
    # hash-object applies the same clean filters git applies on commit, so an unchanged checkout hashes to the committed blob.
    $work = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('hash-object', '--', $workFile)
    if ($work.exitCode -ne 0 -or ([Text.Encoding]::ASCII.GetString($work.bytes)).Trim() -cne $blobSha) { return (& $bad 'ADMISSION_SOURCE_DIRTY') }
    $content = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('cat-file', 'blob', $blobSha)
    if ($content.exitCode -ne 0) { return (& $bad 'ADMISSION_SOURCE_NOT_COMMITTED') }
    $last = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('log', '-1', '--format=%H', 'HEAD', '--', $RelativePath)
    $text = [Text.UTF8Encoding]::new($false).GetString($content.bytes)
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    [pscustomobject]@{ ok = $true; reason = $null; text = $text; blobSha = $blobSha; headSha = $headSha
                       lastCommit = $(if ($last.exitCode -eq 0) { ([Text.Encoding]::ASCII.GetString($last.bytes)).Trim() } else { $null }) }
}

function ConvertTo-DvText([byte[]]$Bytes) {
    $text = [Text.UTF8Encoding]::new($false).GetString($Bytes)
    if ($text.Length -gt 0 -and $text[0] -eq [char]0xFEFF) { $text = $text.Substring(1) }
    $text
}

function Get-DvBlobById {
    <#
    .SYNOPSIS
    The bytes of a git blob NAMED BY ITS ID in the repo at $RepoRoot (git cat-file -t must say blob, then git cat-file blob).
    A receipt's consentBlobSha / venueTableBlobSha are resolved here, so an id that is not a real blob of this repo (a placeholder
    hash, a commit, a tree, a blob that was never committed) is refused. Returns [pscustomobject]@{ ok; reason; bytes; text }.
    #>
    param([Parameter(Mandatory)][string]$RepoRoot, [Parameter(Mandatory)][AllowEmptyString()][string]$BlobSha)
    $bad = { param($why) [pscustomobject]@{ ok = $false; reason = $why; bytes = $null; text = $null } }
    if ($BlobSha -cnotmatch '^[0-9a-f]{40}$') { return (& $bad 'not a 40-hex git object id') }
    $type = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('cat-file', '-t', $BlobSha)
    if ($type.exitCode -ne 0 -or ([Text.Encoding]::ASCII.GetString($type.bytes)).Trim() -cne 'blob') { return (& $bad 'not a blob of this repository') }
    $content = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('cat-file', 'blob', $BlobSha)
    if ($content.exitCode -ne 0) { return (& $bad 'the blob could not be read') }
    [pscustomobject]@{ ok = $true; reason = $null; bytes = $content.bytes; text = (ConvertTo-DvText $content.bytes) }
}

function Get-DvBlobIdAtCommit {
    # `git rev-parse <commit>:<path>` -> the blob id, or $null when $Commit is not a commit of this repo or the path is not in it.
    param([Parameter(Mandatory)][string]$RepoRoot, [Parameter(Mandatory)][string]$Commit, [Parameter(Mandatory)][string]$RelativePath)
    if ($Commit -cnotmatch '^[0-9a-f]{40}$') { return $null }
    $kind = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('cat-file', '-t', $Commit)
    if ($kind.exitCode -ne 0 -or ([Text.Encoding]::ASCII.GetString($kind.bytes)).Trim() -cne 'commit') { return $null }
    $r = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('rev-parse', '--verify', "${Commit}:$RelativePath")
    if ($r.exitCode -ne 0) { return $null }
    $id = ([Text.Encoding]::ASCII.GetString($r.bytes)).Trim()
    if ($id -cmatch '^[0-9a-f]{40}$') { $id } else { $null }
}

$script:LegsRelativeDir = 'tools/profiling/dual-venue/legs'
# Ordinal keys (HARDENING-2 (3)): @{} compares case-insensitively, so /tmp/Repo and /tmp/repo (two repos on a case-sensitive filesystem) shared an entry.
$script:LegBlobCache = [hashtable]::new([StringComparer]::Ordinal)
$script:LookReceiptPattern = '^look-receipts/[a-z0-9-]+\.marxml$'

function Read-DvCommittedLookReceipt {
    <#
    .SYNOPSIS
    LOOK-ASSIST-FILM-FLAVOR-2 r2: the optional receipt a look leg names (look.receipt + look.receiptSha256), read AS COMMITTED at $Commit
    (tools/profiling/dual-venue/<receipt>), never from the working copy. ok only when both fields are well formed and the committed bytes hash
    to receiptSha256; a look block naming neither is ok with present = $false. Returns [pscustomobject]@{ ok; present; bytes; sha256 }.
    #>
    param([Parameter(Mandatory)][string]$RepoRoot, [Parameter(Mandatory)][AllowEmptyString()][string]$Commit, $Look)
    $rel = [string](Get-DvProp $Look 'receipt'); $want = [string](Get-DvProp $Look 'receiptSha256')
    if ([string]::IsNullOrEmpty($rel) -and [string]::IsNullOrEmpty($want)) { return [pscustomobject]@{ ok = $true; present = $false; bytes = $null; sha256 = $null } }
    $bad = [pscustomobject]@{ ok = $false; present = $true; bytes = $null; sha256 = $null }
    if ($rel -cnotmatch $script:LookReceiptPattern -or $want -cnotmatch '^[0-9a-f]{64}$') { return $bad }
    $id = Get-DvBlobIdAtCommit -RepoRoot $RepoRoot -Commit $Commit -RelativePath ('tools/profiling/dual-venue/' + $rel)
    if ($null -eq $id) { return $bad }
    $blob = Get-DvBlobById -RepoRoot $RepoRoot -BlobSha $id
    if (-not $blob.ok) { return $bad }
    $sha = Get-DvSha256OfBytes $blob.bytes
    if ($sha -cne $want) { return $bad }
    [pscustomobject]@{ ok = $true; present = $true; bytes = $blob.bytes; sha256 = $sha }
}

function Find-DvCommittedLegSpec {
    <#
    .SYNOPSIS
    The leg spec a receipt names, FOUND IN THE COMMITTED TREE: the blob under tools/profiling/dual-venue/legs/ at $Commit whose
    bytes hash (sha256, line endings normalised: Get-DvLegSpecSha256) to $LegSpecSha256. A spec nobody committed (a hand-written criteria list that always passes) is never found.
    Returns [pscustomobject]@{ ok; relativePath; blobSha; text }.
    #>
    param([Parameter(Mandatory)][string]$RepoRoot, [Parameter(Mandatory)][AllowEmptyString()][string]$Commit, [Parameter(Mandatory)][AllowEmptyString()][string]$LegSpecSha256)
    $none = [pscustomobject]@{ ok = $false; relativePath = $null; blobSha = $null; text = $null }
    if ($Commit -cnotmatch '^[0-9a-f]{40}$' -or $LegSpecSha256 -cnotmatch '^[0-9a-f]{64}$') { return $none }
    if ((Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('cat-file', '-t', $Commit)).exitCode -ne 0) { return $none }
    # `ls-tree -r` names every blob of the directory with its object id: one git call for the listing instead of three per file.
    # `-z` ends each record with NUL and leaves the path unquoted: without it core.quotepath (default true) prints a non-ASCII name as "caf\303\251.json", which fails the EndsWith('.json') test below.
    $tree = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('ls-tree', '-r', '-z', $Commit, '--', $script:LegsRelativeDir)
    if ($tree.exitCode -ne 0) { return $none }
    foreach ($row in ([Text.Encoding]::UTF8.GetString($tree.bytes) -split "`0" | Where-Object { $_.Trim() -ne '' })) {
        # <mode> SP <type> SP <object> TAB <path>
        if ($row -cnotmatch '^[0-7]{6} blob (?<id>[0-9a-f]{40})\t(?<path>.+)$') { continue }
        $id = $Matches['id']; $path = $Matches['path'].Trim()
        if (-not $path.EndsWith('.json')) { continue }
        # A blob id names its bytes for good, so a blob read once is not read again by the next lookup of this process: every lookup used to
        # re-read every blob of the directory (two git spawns each), which made finding all N shipped legs quadratic in N.
        # Keyed by repo AND blob id (CoS N1): a hit must not skip Get-DvBlobById's "a blob of this repository" check for a second repo
        # that one process looks at (as the tests do). r1 keyed by $id alone.
        # HARDENING-2 (3): the root is normalised, so 'C:\r', 'C:\r\' and 'C:\x\..\r' are one repo and one entry per blob.
        $cacheKey = [IO.Path]::TrimEndingDirectorySeparator([IO.Path]::GetFullPath($RepoRoot)) + [char]0 + $id
        $blob = $script:LegBlobCache[$cacheKey]
        if ($null -eq $blob) {
            $blob = Get-DvBlobById -RepoRoot $RepoRoot -BlobSha $id
            if ($blob.ok) { $script:LegBlobCache[$cacheKey] = $blob }
        }
        if ($blob.ok -and (Get-DvLegSpecSha256 $blob.bytes) -ceq $LegSpecSha256) {
            return [pscustomobject]@{ ok = $true; relativePath = $path; blobSha = $id; text = $blob.text }
        }
    }
    $none
}

function Resolve-DvAdmissionSources {
    <#
    .SYNOPSIS
    Where the venue table and the per-venue consent come from. PRODUCTION reads ONLY the tracked files at the COMMITTED
    revision (HEAD) of the repo, and refuses when the working copy differs: a file an agent or a caller writes anywhere
    is never consent. A path override (-ConsentPath / -VenueTablePath) exists for tests only and is refused unless
    -OfflineTestMode is set (and an offline-test receipt is never evidence: Test-DvReceiptValid).
    Returns [pscustomobject]@{ ok; reason; mode; tableText; consentText; consentBlobSha; venueTableBlobSha; headCommit; consentLastCommit }.
    #>
    param([Parameter(Mandatory)][string]$RepoRoot, [string]$ConsentPath = '', [string]$VenueTablePath = '', [switch]$OfflineTestMode)
    $res = [ordered]@{ ok = $false; reason = $null; mode = $(if ($OfflineTestMode) { 'offline-test' } else { 'production' })
                       tableText = $null; consentText = $null; consentBlobSha = $null; venueTableBlobSha = $null; headCommit = $null; consentLastCommit = $null }
    if ($OfflineTestMode) {
        $consentFile = $(if ([string]::IsNullOrWhiteSpace($ConsentPath)) { Join-Path $RepoRoot ($script:ConsentRelativePath -replace '/', '\') } else { $ConsentPath })
        $tableFile = $(if ([string]::IsNullOrWhiteSpace($VenueTablePath)) { Join-Path $RepoRoot ($script:VenueTableRelativePath -replace '/', '\') } else { $VenueTablePath })
        if (-not (Test-Path -LiteralPath $tableFile -PathType Leaf)) { $res.reason = 'VENUE_TABLE_MISSING'; return [pscustomobject]$res }
        $res.tableText = [IO.File]::ReadAllText($tableFile)
        # A missing consent file reads as the empty text, which Read-DvClipConsent refuses (fail closed).
        $res.consentText = $(if (Test-Path -LiteralPath $consentFile -PathType Leaf) { [IO.File]::ReadAllText($consentFile) } else { '' })
        $res.ok = $true
        return [pscustomobject]$res
    }
    if (-not [string]::IsNullOrWhiteSpace($ConsentPath) -or -not [string]::IsNullOrWhiteSpace($VenueTablePath)) {
        $res.reason = 'TEST_SEAM_IN_PRODUCTION'; return [pscustomobject]$res
    }
    $table = Get-DvCommittedFile -RepoRoot $RepoRoot -RelativePath $script:VenueTableRelativePath
    if (-not $table.ok) { $res.reason = $table.reason; return [pscustomobject]$res }
    $consent = Get-DvCommittedFile -RepoRoot $RepoRoot -RelativePath $script:ConsentRelativePath
    if (-not $consent.ok) { $res.reason = $consent.reason; return [pscustomobject]$res }
    $res.tableText = $table.text; $res.consentText = $consent.text
    $res.venueTableBlobSha = $table.blobSha; $res.consentBlobSha = $consent.blobSha
    $res.headCommit = $consent.headSha; $res.consentLastCommit = $consent.lastCommit
    $res.ok = $true
    [pscustomobject]$res
}

# r9 B2: the case-insensitive option every PATH-refusal regex uses. PowerShell's -match / -imatch / -like and an inline (?i) follow the PROCESS culture (tr-TR: `I` does not fold onto `i`), so a refusal rule
# that must hold on every host builds its regex with this option instead (IgnoreCase + CultureInvariant).
$script:DvCultureFreeIgnoreCase = [System.Text.RegularExpressions.RegexOptions]'IgnoreCase,CultureInvariant'

function Test-DvUnderClaudeState {
    # Owner footage (a contact sheet, a receipt that names it) is only ever written where it stays local: a path with a
    # `.claude-state` segment (gitignored; never committed, PR-attached, bus-published or published as an artifact).
    param([Parameter(Mandatory)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    @($full.Split([char[]]@('\', '/')) | Where-Object { [string]::Equals($_, '.claude-state', [StringComparison]::Ordinal) }).Count -gt 0
}

function Test-DvLocalReceiptRoot {
    # LEASE-BATCH-RECEIPT-ROOT-1 r3/r4/r5 (Dot A2 6542192087; adversary A1-A4, ADV-A1, ADV-A2; Grok G5): the ONE admission rule for a directory or file that owner-footage evidence is WRITTEN to (a receipt root and the
    # evidence directory beside it, a sheet/strip copy directory, a pair/flavor-pair output directory, a trend file, a baseline or nightly state file).
    # RETURNS the normalised full path it certified, or $null for a refusal. EVERY CALLER WRITES TO THE RETURNED PATH, never to the raw value: the value checked is the value written (r4 A2).
    # r5 G5: the returned path is the FINAL, link-resolved path that the locality judgement ran on (not the spelling that reached it). A refusal is $null (falsy), so `if (-not (Test-DvLocalReceiptRoot ...))` still reads as a guard.
    # The path must be (1) under a `.claude-state` segment (Test-DvUnderClaudeState) AND (2) a LOCAL file-system path:
    #   - the RAW text is judged first, before any normalisation (r5 ADV-A1): two leading separators of EITHER slash is a UNC and is refused; so is every device spelling (`\\.\`, `//?/`, `\/?\`, `\??\`,
    #     `\\?\UNC\`, GLOBALROOT, Volume GUID) except the one backslash-only form `\\?\C:\...`, which is certified and RETURNED as `C:\...` (r4 A4);
    #   - a relative value is resolved ONCE, against the POWERSHELL location (the one every later cmdlet resolves it against; r4 A2), and only when that location is on the FileSystem provider; a provider-qualified
    #     (`HKLM:\`, `Env:\`, `Provider::`) or drive-relative (`C:rel`) value is refused. The file-system drive of a drive-letter value must be a plain local root (a PowerShell drive that names a UNC is refused);
    #   - a path equal to or under a venue agentShare from venues.json is refused (OrdinalIgnoreCase, full-path normalised, trailing separators trimmed) BOTH as spelled and after every symbolic link / junction on the way is
    #     followed to its final target (r4 A3), AND the same canonical form is applied to each share (r5 ADV-A2): an extended-prefix share, and a share that is itself reached through a link, match the candidate that sits under them;
    #   - a path on a mapped NETWORK drive ([IO.DriveInfo].DriveType), as spelled and as resolved.
    # r6 CONVERGENCE (Grok G7 / Dot 6546717175 / ADV-A3, ADV-A1, ADV-A2, ADV-A4): ONE invariant, judged on the spelling AND on the FINAL link-resolved path (the value returned and written):
    #   it is under an exact `.claude-state` component, it is local ($isLocal: no UNC/device, no agentShare, no network drive, and its PowerShell drive -- whatever spelling reached it, `\\?\X:\` included --
    #   is a plain file-system drive rooted at its own letter), and every component is WELL-FORMED: no invalid file-name character (`<>:"|?*`, controls), no trailing space or dot (Win32 strips
    #   them, so such a name is an alias of another directory), no DOS device name (NUL, CON, COM1, ...). GetFullPath expands an existing 8.3 short name, so the canonical long form is what is
    #   compared. A drive root is never a certificate. The result is idempotent: Test-DvLocalReceiptRoot (Test-DvLocalReceiptRoot x) returns the same path, so a re-judging caller accepts its own certificate.
    # r8 (sol B1/B2/B3, A3): a device name is refused with any run of spaces before its extension, on every component; the PowerShell drive's Root must equal `<letter>:\` exactly (the alternative, certifying the
    # provider-resolved path, was not taken: one literal rule is judged on the spelling and on the final path alike); no component exceeds the volume's maximum component length (GetVolumeInformationW, 255 when unreadable); a link chain is bounded per link (16).
    # FAIL CLOSED: an unreadable venues.json, an unparsable path or a link that cannot be read is a refusal. Nothing is created, opened or written here; reading drive types, attributes and link targets is the only I/O.
    # This function reaches no engine-session API (the attr3 splice scan, test_attr3_spliced_body_scan, counts any such reference in this file as an unprovable extractor binding): the location comes from Get-Location, the drive from Get-PSDrive.
    param([Parameter(Mandatory)][string]$Path, [string[]]$AgentShares)
    try {
        $rawSpelling = $null; $here = $null
        $badNameChars = [IO.Path]::GetInvalidFileNameChars()
        # r8 B3: the longest name one path component may have on the volume of a drive letter, in UTF-16 code units (GetVolumeInformationW, read once per letter). FAIL CLOSED at 255 -- the limit of every
        # local file system this tool writes to (NTFS, ReFS, exFAT, FAT) -- when the volume cannot be read; a volume that reports less than 255 is held to its own figure.
        $volMax = @{}
        $maxComponent = {
            param($letter)
            $key = ([string]$letter).ToUpperInvariant()
            if (-not $volMax.ContainsKey($key)) {
                $lim = 255
                try {
                    if (-not ('DvR8Vol.Native' -as [type])) {
                        Add-Type -Namespace DvR8Vol -Name Native -ErrorAction Stop -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)] public static extern bool GetVolumeInformationW(string root, System.Text.StringBuilder volName, uint volNameSize, out uint serial, out uint maxComponent, out uint flags, System.Text.StringBuilder fsName, uint fsNameSize);'
                    }
                    $serial = [uint32]0; $mc = [uint32]0; $fl = [uint32]0
                    if ([DvR8Vol.Native]::GetVolumeInformationW($key + ':\', $null, 0, [ref]$serial, [ref]$mc, [ref]$fl, $null, 0) -and $mc -gt 0 -and $mc -lt 255) { $lim = [int]$mc }
                } catch { $lim = 255 }
                $volMax[$key] = $lim
            }
            return $volMax[$key]
        }
        # r11 (Dot pr-729 / sol 6559605502 / adversary A1): the ONE component rule, applied to the ORIGINAL spelling's components (before any GetFullPath, which removes `bad\..` and so would hide it) AND to the
        # final path's components ($isLocal). $true = a name the file system writes as spelled: no longer than $maxLen, no invalid file-name character (`<>:"|?*`, NUL, controls), no trailing space or dot (Win32 strips them, so
        # such a name is an alias of another directory), no DOS device name. The `.` and `..` navigation tokens are the caller's to skip (they are resolved, never written).
        # r9 B2: culture-INVARIANT (under tr-TR an inline `(?i:...)` folds `i` to U+0130, so lowercase `conin$` was not a device): RegexOptions.IgnoreCase -bor CultureInvariant, never the process culture.
        # r7 G9: COM/LPT with a superscript 1/2/3 are devices too, written as regex escapes so this file stays ASCII. r8 B1: Win32 drops any run of spaces between a device stem and its extension, so the stem is followed by `[ ]*`.
        # r12 (sol r11 INV-5): the digit class starts at 1. RtlIsDosDeviceName_U returns 0 for COM0, LPT0, COM0.txt and `LPT0 .txt` (measured on 10.0.19045), so a COM0 or LPT0 directory is an ordinary name and stays usable.
        $componentWellFormed = {
            param($c, $maxLen)
            if ($c.Length -eq 0) { return $true }
            if ($c.Length -gt $maxLen) { return $false }
            if ($c.IndexOfAny($badNameChars) -ge 0) { return $false }
            if ($c.EndsWith(' ') -or $c.EndsWith('.')) { return $false }
            if ([regex]::IsMatch($c, '^(?:CON|PRN|AUX|NUL|COM[1-9\u00B9\u00B2\u00B3]|LPT[1-9\u00B9\u00B2\u00B3]|CONIN\$|CONOUT\$)[ ]*(\..*)?$', $script:DvCultureFreeIgnoreCase)) { return $false }
            return $true
        }
        # (1) the RAW text, either slash. Only `\\?\C:\...` (backslashes) is a local device spelling; anything else that starts with two separators (UNC, \\.\, //?/, \/?\, \\?\UNC\) is refused here.
        if ($Path -match '^[\\/]{2}') {
            if ($Path -cnotmatch '^\\\\\?\\[A-Za-z]:\\[^/]*$') { return $null }
            $full = $Path.Substring(4)
        }
        elseif ($Path -match '^[\\/]\?\?[\\/]') { return $null }   # the NT object-manager prefix \??\
        elseif ($Path -match '^[A-Za-z]:[\\/]') { $full = $Path }   # its PowerShell drive is judged by $isLocal below, like every other drive-letter spelling (r6 ADV-A2)
        elseif ($Path.Contains(':')) { return $null }   # provider-qualified (HKLM:\, Env:\, Module\Provider::path) or drive-relative (C:rel): a colon anywhere in a value that is not a drive-letter path
        else {
            # rooted-on-the-current-drive (`\x`, `/x`) or relative: resolved once against the POWERSHELL location, on the FileSystem provider only
            $here = Get-Location
            if ($here.Provider.Name -cne 'FileSystem') { return $null }
            $full = [IO.Path]::GetFullPath($Path, $here.ProviderPath)
            $rawSpelling = $Path
        }
        # r9 B1 (sol r8): Win32 strips a trailing dot / space from a component, so GetFullPath turns `receipts.` into `receipts` and the normalised-path check below never sees it. The RAW spelling is judged first, on EVERY
        # component (terminal and middle, either slash): a component ending in `.` or ` ` is refused, the `.` and `..` navigation tokens excepted (those are resolved by GetFullPath, not written).
        if ($null -eq $rawSpelling) { $rawSpelling = $full }
        # r11: the same RAW pass judges EVERY original component by the whole component rule (invalid character, NUL, control, length, DOS device name, trailing dot/space), not only the trailing dot/space: GetFullPath
        # (below) removes `bad<root>\..`, `NUL\..`, a 256-character or control-character name followed by `..`, so judged on the normalised path alone they were certified while the same name without the `..` refused.
        # The drive spec (`C:`) of a drive-letter spelling is not a name; the length limit is the volume's of that letter (or of the PowerShell location's drive for a relative value; 255 when there is none).
        $rawLetter = $(if ($rawSpelling -match '^([A-Za-z]):[\\/]') { $Matches[1] } elseif ($here -and ([string]$here.ProviderPath) -match '^([A-Za-z]):') { $Matches[1] } else { 'C' })
        $rawMax = & $maxComponent $rawLetter
        $rawComponents = @($rawSpelling.Split([char[]]@('\', '/')))
        $rawFrom = $(if ($rawSpelling -match '^[A-Za-z]:[\\/]') { 1 } else { 0 })
        for ($rawIdx = $rawFrom; $rawIdx -lt $rawComponents.Count; $rawIdx++) {
            $rc = $rawComponents[$rawIdx]
            if ([string]::Equals($rc, '.', [StringComparison]::Ordinal) -or [string]::Equals($rc, '..', [StringComparison]::Ordinal)) { continue }
            if (-not (& $componentWellFormed $rc $rawMax)) { return $null }
        }
        if ($full -match '^[\\/]{2}') { return $null }
        $plain = [IO.Path]::GetFullPath($full)
        if ($plain -notmatch '^[A-Za-z]:\\') { return $null }
        if (-not (Test-DvUnderClaudeState -Path $plain)) { return $null }
        if ($null -eq $AgentShares) {
            $doc = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'venues.json')) | ConvertFrom-Json
            $AgentShares = @($doc.venues.PSObject.Properties | ForEach-Object { [string]$_.Value.agentShare } | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        }
        $norm = { param($x) ([IO.Path]::GetFullPath(($x -replace '/', '\'))).TrimEnd('\') }
        # a share is judged in the SAME canonical form as the candidate: extended prefix removed (\\?\UNC\h\s -> \\h\s, \\?\C:\x -> C:\x), slashes folded, full-path normalised
        $shareForm = {
            param($x)
            $y = $x -replace '/', '\'
            if ($y -match '^\\\\[?.]\\UNC\\') { $y = '\\' + $y.Substring(8) }
            elseif ($y -match '^\\\\[?.]\\') { $y = $y.Substring(4) }
            & $norm $y
        }
        # Follow every symbolic link / junction among the EXISTING components of $start (the rest does not exist yet and is created under the resolved parent). A link whose target
        # cannot be read, or a chain of more than 16 LINKS, is a refusal (returns $null). r8 A3: ONE link is resolved per hop (ResolveLinkTarget($false)), so $hop counts links, not outer rewrites:
        # 16 links settle, a 17th is refused. With $judge, each hop is also judged by $isLocal (UNC / agentShare / network drive).
        $walk = {
            param($start, $judge)
            $cur = $start
            for ($hop = 0; ; $hop++) {
                $parts = @($cur.Substring(3).Split([char[]]@('\'), [StringSplitOptions]::RemoveEmptyEntries))
                $acc = $cur.Substring(0, 3).TrimEnd('\')
                $moved = $false
                for ($i = 0; $i -lt $parts.Count; $i++) {
                    $acc = $acc + '\' + $parts[$i]
                    if (-not [IO.Directory]::Exists($acc) -and -not [IO.File]::Exists($acc)) { break }
                    if (([IO.File]::GetAttributes($acc) -band [IO.FileAttributes]::ReparsePoint) -eq 0) { continue }
                    $info = $(if ([IO.Directory]::Exists($acc)) { [IO.DirectoryInfo]::new($acc) } else { [IO.FileInfo]::new($acc) })
                    $target = $info.ResolveLinkTarget($false)
                    if ($null -eq $target) { continue }   # a reparse point that is not a link (a cloud placeholder, a dedup stub): not a redirect
                    $tail = @($(if ($i + 1 -lt $parts.Count) { $parts[($i + 1)..($parts.Count - 1)] } else { @() }))
                    $t = [string]$target.FullName
                    if ($t.StartsWith('\\?\', [StringComparison]::Ordinal)) {
                        if ($t -cnotmatch '^\\\\\?\\[A-Za-z]:\\') { return $null }
                        $t = $t.Substring(4)
                    }
                    if ($t -match '^[\\/]{2}') { return $null }
                    $cur = [IO.Path]::GetFullPath($(if ($tail.Count -gt 0) { Join-Path $t ($tail -join '\') } else { $t }))
                    $moved = $true
                    break
                }
                if (-not $moved) { return $cur }
                if ($hop -ge 16) { return $null }   # this was the 17th link on the way
                if ($judge -and -not (& $isLocal $cur)) { return $null }
            }
        }
        # the share keys: each share as spelled (canonical form) AND, for a drive-letter share, after its own links are followed (a share reached through a link is the same share)
        $shareKeys = @()
        foreach ($s in @($AgentShares)) {
            $k = & $shareForm $s
            $shareKeys += $k
            if ($k -match '^[A-Za-z]:\\') {
                $kr = & $walk $k $false
                if ($null -eq $kr) { return $null }
                $shareKeys += (& $norm $kr)
            }
        }
        $isLocal = {
            # judges ONE concrete spelling: well-formed components / UNC / device / PowerShell drive / agentShare / network drive. Returns $true when it is a local, non-share path.
            param($p)
            if ($p -match '^[\\/]{2}') { return $false }
            if ($p -notmatch '^[A-Za-z]:\\') { return $false }
            # r6 ADV-A4 / ADV-A1 / r8 B1 / r8 B3 / r11: every component of the concrete path is well formed ($componentWellFormed: the same rule the ORIGINAL spelling's components were judged by above),
            # at the volume's maximum component length (such a directory cannot be created).
            $maxLen = & $maxComponent $p.Substring(0, 1)
            foreach ($c in @($p.Substring(3).Split([char]92))) {
                if (-not (& $componentWellFormed $c $maxLen)) { return $false }
            }
            # r6 ADV-A2 / r8 B2: the PowerShell drive of this letter (what New-Item / Copy-Item resolve it through), if any, is a file-system drive rooted EXACTLY at `<letter>:\` -- not `New-PSDrive X -Root \\host\share`, and not
            # `C` re-rooted at `C:\Windows` (the cmdlets would write under that directory while this judgement ran on the literal spelling)
            $psd = Get-PSDrive -Name $p.Substring(0, 1) -ErrorAction SilentlyContinue
            if ($null -ne $psd -and ($psd.Provider.Name -cne 'FileSystem' -or -not [string]::IsNullOrEmpty([string]$psd.DisplayRoot) -or -not [string]::Equals([string]$psd.Root, $p.Substring(0, 1) + ':\', [StringComparison]::OrdinalIgnoreCase))) { return $false }
            $k = & $norm $p
            foreach ($s in $shareKeys) {
                if ($k.Equals($s, [StringComparison]::OrdinalIgnoreCase) -or $k.StartsWith($s + '\', [StringComparison]::OrdinalIgnoreCase)) { return $false }
            }
            if ([IO.DriveInfo]::new($p.Substring(0, 1)).DriveType -eq [IO.DriveType]::Network) { return $false }
            return $true
        }
        if (-not (& $isLocal $plain)) { return $null }
        $final = & $walk $plain $true
        if ($null -eq $final) { return $null }
        # G5: the path that was judged is the path that is returned (the link-resolved one; $plain itself when no component is a link).
        # r6 G7 / Dot 6546717175 / ADV-A3: the FINAL path is judged by the WHOLE rule again -- the `.claude-state` marker too, not only locality -- so a `.claude-state` component that is a
        # junction onto a directory outside every `.claude-state` is refused, and the certificate re-judges to itself (idempotent). A drive root is never a certificate.
        $final = $final.TrimEnd([char]92)
        if ($final -notmatch '^[A-Za-z]:\\.') { return $null }
        if (-not (Test-DvUnderClaudeState -Path $final)) { return $null }
        if (-not (& $isLocal $final)) { return $null }
        return $final
    } catch { return $null }
}

function Read-DvClipConsent {
    <#
    .SYNOPSIS
    Parse the OWNER-WRITTEN per-venue consent file text (tools/profiling/dual-venue/venue-clip-consent.json, read at its
    COMMITTED revision by Resolve-DvAdmissionSources). The hub records the owner's typed CLIP line there; agents never
    write it and this function only ever reads text it is given. A record carries the owner's EXACT line
    ("CLIP <venue>: <clip id>"), its sha256, recordedUtc and recordedBy = 'owner'; the line must name the record's own
    venue (or that venue name with every hyphen removed, the one admitted alias) and clip id and hash to ownerLineSha256,
    so a bare hex string cannot stand in for the owner's words.
    FAIL CLOSED: empty text, bad JSON, a wrong schema, or any record that is not exactly the six reviewed keys (so a path
    cannot ride along in an extra field) makes the WHOLE file invalid -- unknown consent is no consent.
    BLANKET (VENUE-CLIP-CONSENT-BLANKET-1): an optional top-level `blanketRecords` array. Each record is exactly kind='blanket',
    ownerSentence (the owner's words, verbatim), ownerSentenceSha256, recordedUtc, recordedBy='owner', writtenBy='owner',
    source (the ledger stamp) and scope; the sentence must hash to its stored sha256 AND to the sha256 pinned in this module.
    A blanket that fails any check invalidates the WHOLE file (admits nothing, no fallback).
    Returns [pscustomobject]@{ ok; reason; records; blankets }.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text, [Parameter(Mandatory)]$Table)
    $bad = { param($why) [pscustomobject]@{ ok = $false; reason = $why; records = @(); blankets = @() } }
    if ([string]::IsNullOrWhiteSpace($Text)) { return (& $bad 'the consent file is missing or empty') }
    # A consent record names a clip by ID. A drive path or a footage file name anywhere in the file is refused outright.
    # r9 B2: culture-invariant (tr-TR does not fold the `I` of `I:\` onto `i`)
    if ([regex]::IsMatch($Text, '[a-z]:[\\/]|\.mlv\b', $script:DvCultureFreeIgnoreCase)) { return (& $bad 'the consent file carries a path-shaped value') }
    try { $doc = $Text | ConvertFrom-Json } catch { return (& $bad 'the consent file is not valid JSON') }
    if ([string](Get-DvProp $doc 'schema') -ne $script:ConsentSchema) { return (& $bad "the consent file schema is not $($script:ConsentSchema)") }
    if ($null -eq $doc.PSObject.Properties['records']) { return (& $bad 'the consent file has no records array') }
    $venues = @($Table.venues.PSObject.Properties | ForEach-Object { $_.Name })
    $records = @()
    foreach ($r in @($doc.records)) {
        if ($null -eq $r) { return (& $bad 'a consent record is null') }
        $keys = @($r.PSObject.Properties | ForEach-Object { $_.Name })
        if ($keys.Count -ne $script:ConsentRecordKeys.Count -or @($keys | Where-Object { $_ -cnotin $script:ConsentRecordKeys }).Count -gt 0) {
            return (& $bad 'a consent record is not exactly venue, clipId, ownerLine, ownerLineSha256, recordedUtc, recordedBy')
        }
        if ([string]$r.venue -cnotin $venues) { return (& $bad 'a consent record names a venue the venue table does not have') }
        if ([string]$r.clipId -cnotmatch $script:ClipIdPattern) { return (& $bad 'a consent record clipId is not a consented clip id') }
        if ([string]$r.ownerLineSha256 -cnotmatch '^[0-9a-f]{64}$') { return (& $bad 'a consent record ownerLineSha256 is not 64 lowercase hex') }
        $line = [string]$r.ownerLine
        # The owner's words are never altered. Besides the table's venue name, the one spelling admitted is that same name with every
        # hyphen removed ("CLIP ultramagnus: M16-1243" for ultra-magnus), case-sensitive and exact; the record's venue stays the table name.
        $spellings = @([string]$r.venue, ([string]$r.venue).Replace('-', ''))
        if ($line -cnotin @($spellings | ForEach-Object { 'CLIP ' + $_ + ': ' + [string]$r.clipId })) { return (& $bad "a consent record's ownerLine is not the owner's 'CLIP <venue>: <clip id>' line for its own venue and clip") }
        if ((Get-DvSha256OfText $line) -cne [string]$r.ownerLineSha256) { return (& $bad "a consent record's ownerLineSha256 is not the sha256 of its ownerLine") }
        if ([string]$r.recordedBy -cne 'owner') { return (& $bad "a consent record's recordedBy is not 'owner'") }
        $when = [DateTime]::MinValue
        if (-not [DateTime]::TryParse([string]$r.recordedUtc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$when)) {
            return (& $bad 'a consent record recordedUtc is not a timestamp')
        }
        $records += $r
    }
    $blankets = @()
    if ($null -ne $doc.PSObject.Properties['blanketRecords']) {
        foreach ($b in @($doc.blanketRecords)) {
            if ($null -eq $b) { return (& $bad 'a blanket consent record is null') }
            $bkeys = @($b.PSObject.Properties | ForEach-Object { $_.Name })
            if ($bkeys.Count -ne $script:BlanketRecordKeys.Count -or @($bkeys | Where-Object { $_ -cnotin $script:BlanketRecordKeys }).Count -gt 0) {
                return (& $bad 'a blanket consent record is not exactly kind, ownerSentence, ownerSentenceSha256, recordedUtc, recordedBy, writtenBy, source, scope')
            }
            if ([string]$b.kind -cne 'blanket') { return (& $bad "a blanket consent record's kind is not 'blanket'") }
            if ([string]$b.ownerSentenceSha256 -cnotmatch '^[0-9a-f]{64}$') { return (& $bad 'a blanket consent record ownerSentenceSha256 is not 64 lowercase hex') }
            if ((Get-DvSha256OfText ([string]$b.ownerSentence)) -cne [string]$b.ownerSentenceSha256) { return (& $bad "a blanket consent record's ownerSentenceSha256 is not the sha256 of its ownerSentence") }
            if ([string]$b.ownerSentenceSha256 -cne $script:BlanketOwnerSentenceSha256) { return (& $bad "a blanket consent record's sentence is not the owner's recorded blanket sentence") }
            if ([string]$b.recordedBy -cne 'owner') { return (& $bad "a blanket consent record's recordedBy is not 'owner'") }
            if ([string]$b.writtenBy -cne 'owner') { return (& $bad "a blanket consent record's writtenBy is not 'owner'") }
            if ([string]$b.source -cne $script:BlanketSource) { return (& $bad "a blanket consent record's source is not the owner's ledger stamp") }
            if ([string]$b.scope -cne $script:BlanketScope) { return (& $bad "a blanket consent record's scope is not '$($script:BlanketScope)'") }
            $bwhen = [DateTime]::MinValue
            if (-not [DateTime]::TryParse([string]$b.recordedUtc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal, [ref]$bwhen)) {
                return (& $bad 'a blanket consent record recordedUtc is not a timestamp')
            }
            $blankets += $b
        }
    }
    [pscustomobject]@{ ok = $true; reason = $null; records = $records; blankets = $blankets }
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
      VENUE_CLIP_CONSENT_ABSENT            no owner-typed record for THIS venue + this clip id and no blanket record (consent
                                           on one venue never implies the other; a blanket record admits any clip id on any venue)
      OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2   the reviewed switch venues.json ownerFootage.cleanupClassGone is false
    #>
    param(
        [Parameter(Mandatory)][string]$ClipId, [Parameter(Mandatory)][string]$Venue, [Parameter(Mandatory)]$Table,
        [Parameter(Mandatory)][AllowEmptyString()][string]$ConsentText, [Parameter(Mandatory)][string]$RepoRoot
    )
    $refuse = { param($token) [pscustomobject]@{ admitted = $false; reason = $token; ownerLineSha256 = $null; recordedUtc = $null; consentKind = $null; consentSha256Prefix = $null } }
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
    $consent = Read-DvClipConsent -Text $ConsentText -Table $Table
    if (-not $consent.ok) { return (& $refuse 'VENUE_CLIP_CONSENT_INVALID') }
    $record = $null
    foreach ($r in $consent.records) {
        if ([string]$r.venue -ceq $Venue -and [string]$r.clipId -ceq $ClipId) { $record = $r; break }
    }
    # Per-clip records are checked FIRST; the blanket record (the owner's sentence of 2026-10-11) admits any well-formed clip id on any venue in the table.
    $kind = 'clip'
    if ($null -eq $record -and @($consent.blankets).Count -gt 0 -and $null -ne $Table.venues.PSObject.Properties[$Venue]) { $kind = 'blanket'; $record = @($consent.blankets)[0] }
    if ($null -eq $record) { return (& $refuse 'VENUE_CLIP_CONSENT_ABSENT') }
    $refusal = 'OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2'
    if ($null -ne $Table.PSObject.Properties['ownerFootage']) { $refusal = [string]$Table.ownerFootage.refusal }
    $cleanupGone = ($null -ne $Table.PSObject.Properties['ownerFootage']) -and [bool]$Table.ownerFootage.cleanupClassGone
    if (-not $cleanupGone) { return (& $refuse $refusal) }
    if ($kind -ceq 'blanket') {
        $sha = [string](Get-DvProp $record 'ownerSentenceSha256')
        return [pscustomobject]@{ admitted = $true; reason = $null; ownerLineSha256 = $sha; recordedUtc = [string](Get-DvProp $record 'recordedUtc'); consentKind = 'blanket'; consentSha256Prefix = $sha.Substring(0, [Math]::Min(12, $sha.Length)) }
    }
    $lineSha = [string](Get-DvProp $record 'ownerLineSha256')
    [pscustomobject]@{ admitted = $true; reason = $null; ownerLineSha256 = $lineSha; recordedUtc = [string](Get-DvProp $record 'recordedUtc'); consentKind = 'clip'; consentSha256Prefix = $lineSha.Substring(0, [Math]::Min(12, $lineSha.Length)) }
}

# --- the receipt oracle's verdict, carried in the receipt (round 2) and RE-DERIVED from the run's own log (round 3) -------
# The run nonce the REAL launcher mints is [Guid]::NewGuid().ToString("N") (tools/profiling/run-release-gui-smoke.ps1:
# `$runNonce = ...`): 32 lowercase hex, no prefix. The rule accepts exactly that and nothing else; test_dual_venue_evidence.py
# evaluates the launcher's own expression and feeds it to this rule, so a producer-format change turns a test red.
$script:RunNoncePattern = '^[0-9a-f]{32}$'
# PLAYBACK-CLIP-LENGTH-HFR-10S-1 (owner amendment 2026-10-15): the floor is a function of the footage's NATIVE fps -- 10 s at
# >= 50 fps (HFR), 20 s below, 20 s when unknown (fail closed). Mirrors platform/qt/PlaybackFrameRange.h minPlayWindowSeconds
# and gui-smoke-length-gate.ps1 Get-GuiSmokeMinPlaySeconds (this module is loaded without that file); a parity test compares them.
$script:StandardMinPlaySeconds = 20.0
$script:HfrMinPlaySeconds = 10.0
$script:HfrNativeFpsThreshold = 50.0

function Get-DvMinPlaySeconds {
    # The play floor of footage at -NativeFps; -LowestAnyClip: the floor where the footage is not known yet (a leg spec names a
    # clip id, never its fps), i.e. only what NO clip could admit -- the venue's runner and the app apply the clip's own floor.
    param([AllowNull()]$NativeFps, [switch]$LowestAnyClip)
    if ($LowestAnyClip) { return $script:HfrMinPlaySeconds }
    $fps = ConvertTo-DvDouble $NativeFps
    if ($null -ne $fps -and -not [double]::IsNaN($fps) -and -not [double]::IsInfinity($fps) -and $fps -ge $script:HfrNativeFpsThreshold) { return $script:HfrMinPlaySeconds }
    $script:StandardMinPlaySeconds
}

function ConvertTo-DvInt64 {
    # $null for absent / a bool / not an integer: an unparsable number is ABSENT, never zero.
    param($Value)
    $parsed = 0L
    if ($null -ne $Value -and $Value -isnot [bool] -and [int64]::TryParse([string]$Value, [Globalization.NumberStyles]::Integer, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) { return $parsed }
    $null
}

function ConvertTo-DvDouble {
    param($Value)
    $parsed = 0.0
    if ($null -ne $Value -and $Value -isnot [bool] -and [double]::TryParse([string]$Value, [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$parsed)) { return $parsed }
    $null
}

function Get-DvSmokeSummaryFields {
    <#
    .SYNOPSIS
    The measured session's `playback_smoke.summary` line out of the app's own run log, as a key/value hashtable, plus whether
    every `automation.pacing_isolated` line says the run used its run-scoped settings store. The session is chosen exactly the
    way the job chooses it (the `playback_smoke.measured_session` marker, else the first summary line); the LAST summary line
    of that session wins. HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: `conformLine` is the LAST `playback_smoke.conform` line of the same
    session ($null when the log has none), for the conform proof (Get-DvConformPaceProof).
    #>
    param([AllowNull()][AllowEmptyString()][string]$LogText)
    $fields = @{}
    $session = $null
    $isolated = $false
    $conformLine = $null
    if (-not [string]::IsNullOrEmpty($LogText)) {
        # PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r4 (r3 adversary A2 and advisory A3 on #726): every line is read as its record body, so a build path in a Qt
        # location suffix never selects the session or a line, and never adds a summary or conform field.
        $lines = Get-DvLogRecordBodies -LogText $LogText
        foreach ($line in $lines) { if ($line -match 'playback_smoke\.measured_session id=(?<s>\d+)') { $session = $Matches['s']; break } }
        if ($null -eq $session) { foreach ($line in $lines) { if ($line -match 'playback_smoke\.summary session=(?<s>\d+)') { $session = $Matches['s']; break } } }
        if ($null -ne $session) {
            $summaryLine = $null
            foreach ($line in $lines) { if ($line -match ('playback_smoke\.summary session=' + [regex]::Escape($session) + '(\s|$)')) { $summaryLine = $line } }
            if ($null -ne $summaryLine) {
                foreach ($m in [regex]::Matches($summaryLine, '(?<k>[A-Za-z0-9_]+)=(?<v>\S+)')) { $fields[$m.Groups['k'].Value] = $m.Groups['v'].Value }
            }
            foreach ($line in $lines) { if ($line -match ('playback_smoke\.conform session=' + [regex]::Escape($session) + '(\s|$)')) { $conformLine = $line } }
        }
        # Master isolates an automation run's settings store (the venue's persisted settings are never read or rewritten).
        # A build that predates that, or a line that says otherwise, leaves the venue's QSettings exposed: not evidence.
        $iso = @($lines | Where-Object { $_ -match 'interaction_trace event=automation\.pacing_isolated(\s|$)' })
        $isolated = ($iso.Count -gt 0) -and (@($iso | Where-Object { $_ -notmatch 'settings_store=run_scoped(\s|$)' }).Count -eq 0)
    }
    [pscustomobject]@{ session = $session; fields = $fields; settingsIsolated = $isolated; conformLine = $conformLine }
}

function Get-DvConformPaceProof {
    <#
    .SYNOPSIS
    HFR-CONFORM-AWARE-RECEIPT-ORACLES-1. Did the run PROVE it was conformed? Judged from the receipt `playback` block's own fields (the
    conform line's values, recorded from the run log by Get-DvPlaybackEvidence), so a reader re-derives it without the log. Proven only
    when the conform line was PRESENT for the measured session (conformSession == summarySession; conformLinePresent a real boolean $true),
    conformActive == 1, conformClipFps EQUALS nativeFps at print precision (r8: both print one value to 3 decimals), conformPlaybackFps is > 0 and
    within 0.5 % of paceFps, conformPlaybackFps is not above conformClipFps, conformPlaybackFps is BELOW conformClipFps by more than the reduction
    margin (r7), and the engine pace paceFps is BELOW nativeFps by more than the same margin (r8: native engine pace is never a conform, however
    the clip / playback tolerances stack). Absent / malformed / inactive / another session's / inconsistent -> proven=$false and `reason` says why.
    The same decision as Get-GuiSmokeConformPaceProof (gui-smoke-length-gate.ps1) and Get-AttrCudaConformAwareSourceFramesVerdict (AttrCudaArtifacts.psm1);
    a parity test executes all three on one table, and another pins their two named margins equal.
    #>
    param($Playback)
    # MERGE-MASTER r8 (sol r7 B1, key3 CONFORM-PROOF-CLIP-NATIVE-EXACT-1): the conform proof's two named margins, mirrored verbatim in
    # Get-GuiSmokeConformPaceProof and Get-AttrCudaConformAwareSourceFramesVerdict (ConformAwareReceiptOracleTests pins the three equal).
    $conformReductionMargin = 0.005   # a conform lowers playback_fps below clip_fps AND the engine pace below native_fps by more than this fraction
    $conformPrintTolerance = 0.0005   # clip_fps and native_fps print one value to 3 decimals, so they agree within half a print unit
    $out = [pscustomobject]@{ proven = $false; reason = 'no playback_smoke.conform line for the run'; present = $false }
    # MERGE-MASTER r8 (fable r7 / adversary A3): only a real boolean $true is a present line; an array or a string is absent, never present.
    $linePresent = Get-DvProp $Playback 'conformLinePresent'
    if (-not (($linePresent -is [bool]) -and $linePresent)) { return $out }
    $out.present = $true
    $summarySession = [string](Get-DvProp $Playback 'summarySession')
    $conformSession = [string](Get-DvProp $Playback 'conformSession')
    $active = ConvertTo-DvInt64 (Get-DvProp $Playback 'conformActive')
    $clip = ConvertTo-DvDouble (Get-DvProp $Playback 'conformClipFps')
    $play = ConvertTo-DvDouble (Get-DvProp $Playback 'conformPlaybackFps')
    $native = ConvertTo-DvDouble (Get-DvProp $Playback 'nativeFps')
    $pace = ConvertTo-DvDouble (Get-DvProp $Playback 'paceFps')
    $absent = @(@('session', $conformSession), @('conform_active', $active), @('clip_fps', $clip), @('playback_fps', $play) | Where-Object { $null -eq $_[1] -or "$($_[1])" -eq '' } | ForEach-Object { $_[0] })
    if ($absent.Count -gt 0) { $out.reason = "the playback_smoke.conform line is malformed (absent or unreadable: $($absent -join ' / '))"; return $out }
    if ($summarySession -eq '' -or $conformSession -cne $summarySession) { $out.reason = "the playback_smoke.conform line is for session $conformSession, not the measured session $summarySession"; return $out }
    if ($active -ne 1) { $out.reason = "conform_active=$active; the run was not conformed"; return $out }
    # MERGE-MASTER r3 (adversary A4): NaN / Infinity parse as doubles and fail every ordered comparison, so they are rejected first.
    if ($null -eq $native -or -not [double]::IsFinite($native) -or -not [double]::IsFinite($clip) -or $native -le 0 -or $clip -le 0 -or [Math]::Abs($clip - $native) -gt $conformPrintTolerance) { $out.reason = "conform clip_fps=$clip does not match native_fps=$native"; return $out }
    if ($null -eq $pace -or -not [double]::IsFinite($pace) -or -not [double]::IsFinite($play) -or $pace -le 0 -or $play -le 0 -or [Math]::Abs($play - $pace) -gt (0.005 * $pace)) { $out.reason = "conform playback_fps=$play does not match pace_fps=$pace"; return $out }
    if ($play -gt ($clip * 1.005)) { $out.reason = "conform playback_fps=$play is above clip_fps=$clip; conform never speeds a clip up"; return $out }
    # MERGE-MASTER r7 (sol r6 B1, Dot ask 6545453593): conform_active=1 with playback_fps at the clip rate is native-rate playback, not a conform. The app
    # conforms only by LOWERING the rate (PlaybackConformPolicy.h conformApplies: target < clip), so a proof needs playback_fps below clip_fps by more than
    # the reduction margin; within it the run is indistinguishable from native pace. r8 (key3 CONFORM-PROOF-NEAR-NATIVE-REASON-1): the reason says exactly that.
    if ($play -ge ($clip * (1 - $conformReductionMargin))) { $out.reason = "conform active but playback_fps=$play is within $($conformReductionMargin * 100) % of clip_fps=${clip}: indistinguishable from native pace, so no conform is proven"; return $out }
    # MERGE-MASTER r8 (sol r7 B1): the clip / native and playback / pace tolerances are independent, so stacked they let native engine pace prove a
    # conform (native = pace = 59.94, clip 60, playback 59.69). The engine pace itself must sit below the native rate by more than the margin.
    if ($pace -ge ($native * (1 - $conformReductionMargin))) { $out.reason = "conform active but pace_fps=$pace is within $($conformReductionMargin * 100) % of native_fps=${native}: the engine ran at native pace, so no conform is proven"; return $out }
    $out.proven = $true
    $out.reason = ''
    $out
}

function Get-DvConformArmVerdict {
    <#
    .SYNOPSIS
    HFR-NATIVE-60FPS-FULLRES-1. Did the run honour the conform arm the leg REQUESTED (generatorArgs.playbackConform, passed to the app as
    MLVAPP_PLAYBACK_CONFORM)? Judged from the receipt `playback` block's conform fields (re-derived from the run log by Get-DvPlaybackEvidence).
      'off' (the native-HFR arm): the measured session's playback_smoke.conform line is present, conform_active=0, conform_env_override=0 and
            playback_fps is within 0.5 % of clip_fps.
      'on':  the measured session's conform line is present, conform_active=1 and Get-DvConformPaceProof proves the conformed pace (playback_fps
            below clip_fps and pace_fps below native_fps by more than 0.5 %, so native-rate playback is never an honoured conform arm).
    A leg that requests nothing is never blocked (requested = $null). `blocks` is true when the arm was requested and not proven; `reason`
    carries the requested and the observed values, for the receipt's outcomeDetail (CONFORM_ARM_NOT_HONOURED). Pure: the runner gates on it
    and Test-DvReceiptValid re-derives it from the hashed run log and the committed spec.
    #>
    param($Spec, $Playback)
    $requested = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'playbackConform'
    $out = [pscustomobject]@{ requested = $(if ($null -ne $requested) { [string]$requested } else { $null }); honoured = $true; blocks = $false; reason = $null }
    if ($null -eq $requested) { return $out }
    # MERGE-MASTER r8 (fable r7 / adversary A3): a non-boolean flag (an array, a string) is absent, never present and never a throw at [int]$present.
    $linePresent = Get-DvProp $Playback 'conformLinePresent'
    $present = [bool](($linePresent -is [bool]) -and $linePresent)
    $summarySession = [string](Get-DvProp $Playback 'summarySession')
    $conformSession = [string](Get-DvProp $Playback 'conformSession')
    $active = ConvertTo-DvInt64 (Get-DvProp $Playback 'conformActive')
    $envOverride = ConvertTo-DvInt64 (Get-DvProp $Playback 'conformEnvOverride')
    $clip = ConvertTo-DvDouble (Get-DvProp $Playback 'conformClipFps')
    $play = ConvertTo-DvDouble (Get-DvProp $Playback 'conformPlaybackFps')
    $observed = "conform_line_present=$([int]$present) session=$conformSession measured_session=$summarySession conform_active=$active conform_env_override=$envOverride clip_fps=$clip playback_fps=$play"
    $why = $null
    if (-not $present) { $why = 'no playback_smoke.conform line for the run' }
    elseif ($summarySession -eq '' -or $conformSession -cne $summarySession) { $why = 'the conform line is not the measured session''s' }
    elseif ([string]$requested -ceq 'off') {
        if ($active -ne 0) { $why = 'conform was active' }
        elseif ($envOverride -ne 0) { $why = 'the app did not report MLVAPP_PLAYBACK_CONFORM=0' }
        elseif ($null -eq $clip -or $null -eq $play -or -not [double]::IsFinite($clip) -or -not [double]::IsFinite($play) -or $clip -le 0 -or [Math]::Abs($play - $clip) -gt (0.005 * $clip)) { $why = 'playback_fps is not within 0.5 % of clip_fps' }
        else {
            # HFR-NATIVE-60FPS-FULLRES-1 r26 (adversary A2 on be20bc59): the conform line's rates must be the engine's own, or a line that contradicts the summary
            # (native = pace = 59.94, clip = playback = 24) proves nothing. clip_fps IS native_fps at print precision (Get-DvConformPaceProof's tolerance) and
            # playback_fps is within 0.5 % of pace_fps.
            $native = ConvertTo-DvDouble (Get-DvProp $Playback 'nativeFps')
            $pace = ConvertTo-DvDouble (Get-DvProp $Playback 'paceFps')
            if ($null -eq $native -or -not [double]::IsFinite($native) -or $native -le 0 -or [Math]::Abs($clip - $native) -gt 0.0005) { $why = "clip_fps=$clip is not the summary's native_fps=$native" }
            elseif ($null -eq $pace -or -not [double]::IsFinite($pace) -or $pace -le 0 -or [Math]::Abs($play - $pace) -gt (0.005 * $pace)) { $why = "playback_fps=$play is not within 0.5 % of the engine's pace_fps=$pace" }
        }
    }
    elseif ([string]$requested -ceq 'on') {
        # MERGE-MASTER r4 (ADV-A1): conform_active=1 alone is not proof. A requested conform arm needs the conformed pace PROVEN (finite, positive,
        # same-session rates; Get-DvConformPaceProof), or it fails closed; it never falls through to the native pace rule.
        if ($active -ne 1) { $why = 'conform was not active' }
        else {
            $proof = Get-DvConformPaceProof -Playback $Playback
            if (-not $proof.proven) { $why = "the conformed pace is not proven: $($proof.reason)" }
        }
    }
    else { $why = "the requested arm '$requested' is not on/off" }
    if ($null -ne $why) {
        $out.honoured = $false
        $out.blocks = $true
        $out.reason = "requested playbackConform=$requested; observed $observed ($why)"
    }
    $out
}

function Get-DvPlaybackProblems {
    <#
    .SYNOPSIS
    Judge the play-proof block of a receipt. EVERY field must be PRESENT (an absent field is INVALID, never "no wrap" or
    "pace unchecked"); the verdict is re-derived from the fields, never taken from a stored `valid`. Returns the list of
    problems (empty = valid). This repeats, at the receipt and from the fields the receipt carries, the rules master's job
    oracle (Get-AttrCudaSourceFramesVerdict) applies: source_advanced >= required_source_frames >= ceil(20 s x native fps),
    paced at the native fps (or, only on a PROVEN conform line, the conform playback fps; Get-DvConformPaceProof) with no fps override,
    no wrap, and the nonce the app echoed on its summary line IS the nonce the
    launcher generated for this run (both the real launcher's 32-hex format).
    -RequestedConform is the leg's generatorArgs.playbackConform: 'on' means the native pace rule is NEVER a fallback (MERGE-MASTER r4, ADV-A1);
    an unproven conform is CONFORM_PACE_NOT_PROVEN. A non-finite native_fps / pace_fps is unknown, never a pass (NaN fails every comparison).
    #>
    param($Playback, [string]$ExpectedClipId, [AllowNull()][AllowEmptyString()][string]$RequestedConform = '')
    $reasons = [System.Collections.Generic.List[string]]::new()
    if ($null -eq $Playback) { $reasons.Add('RECEIPT_FIELD_ABSENT: the receipt carries no source-frame verdict (playback)'); return $reasons.ToArray() }
    $advanced = ConvertTo-DvInt64 (Get-DvProp $Playback 'sourceAdvanced')
    $required = ConvertTo-DvInt64 (Get-DvProp $Playback 'requiredSourceFrames')
    $native = ConvertTo-DvDouble (Get-DvProp $Playback 'nativeFps')
    $pace = ConvertTo-DvDouble (Get-DvProp $Playback 'paceFps')
    $override = ConvertTo-DvInt64 (Get-DvProp $Playback 'fpsOverride')
    $wrapCount = ConvertTo-DvInt64 (Get-DvProp $Playback 'wrapCount')
    if ($null -eq $advanced) { $reasons.Add('RECEIPT_FIELD_ABSENT: source_advanced') }
    if ($null -eq $required) { $reasons.Add('RECEIPT_FIELD_ABSENT: required_source_frames') }
    elseif ($required -lt 20) { $reasons.Add("INVALID_SOURCE_FRAMES: required_source_frames=$required is under the 20-frame floor; the admitted Play was shorter than 20 s of footage") }
    # The floor, derived here from the footage's own frame rate: ceil(floor x native fps) frames (HFR-10S-1: Get-DvMinPlaySeconds,
    # 10 s at >= 50 fps, else 20 s).
    $floorSeconds = Get-DvMinPlaySeconds -NativeFps $native
    if ($null -ne $advanced -and $null -ne $required) {
        if ($advanced -lt $required) { $reasons.Add("INVALID_SOURCE_FRAMES: source_advanced=$advanced is under required_source_frames=$required; under $floorSeconds s of real footage is never evidence") }
    }
    if ($null -eq $native) { $reasons.Add('RECEIPT_FIELD_ABSENT: native_fps') }
    elseif (-not [double]::IsFinite($native) -or $native -le 0) { $reasons.Add("INVALID_SOURCE_FRAMES: native_fps=$native; the native frame rate is unknown, so 20 s of footage cannot be measured") }
    elseif ($null -ne $required -and $required -lt [int64][Math]::Ceiling($floorSeconds * $native - 0.02)) {
        $reasons.Add("INVALID_SOURCE_FRAMES: required_source_frames=$required is under ceil($floorSeconds s x native_fps=$native); the Play was admitted for less than $floorSeconds s of footage")
    }
    if ($null -eq $pace) { $reasons.Add('RECEIPT_FIELD_ABSENT: pace_fps') }
    elseif (-not [double]::IsFinite($pace) -or $pace -le 0) { $reasons.Add("INVALID_SOURCE_FRAMES: pace_fps=$pace; a present engine pace that is not positive is unknown") }
    elseif ($null -ne $native -and [double]::IsFinite($native) -and $native -gt 0) {
        # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: only a PROVEN conform (Get-DvConformPaceProof) moves the pace comparison off the native fps.
        $conformProof = Get-DvConformPaceProof -Playback $Playback
        if ($conformProof.proven) {
            $conformPlayback = ConvertTo-DvDouble (Get-DvProp $Playback 'conformPlaybackFps')
            if ([Math]::Abs($pace - $conformPlayback) -gt (0.005 * $conformPlayback)) {
                $reasons.Add("INVALID_SOURCE_FRAMES: the engine paced at pace_fps=$pace but the conformed playback_fps is $conformPlayback; 20 s of wall clock is not 20 s of footage")
            }
        } elseif ($RequestedConform -ceq 'on') {
            # MERGE-MASTER r4 (ADV-A1): the leg ASKED for conform, so the native rule is no fallback; an unproven conform fails closed.
            $reasons.Add("CONFORM_PACE_NOT_PROVEN: the leg requested playbackConform=on but the run did not prove a conformed pace ($($conformProof.reason)); the native pace rule is never a fallback for a requested conform arm")
        } elseif ([Math]::Abs($pace - $native) -gt (0.005 * $native)) {
            $why = ''
            if ($conformProof.present) { $why = " (conform relaxation not applied: $($conformProof.reason))" }
            $reasons.Add("INVALID_SOURCE_FRAMES: the engine paced at pace_fps=$pace but the footage native fps is $native; 20 s of wall clock is not 20 s of footage$why")
        }
    }
    if ($null -eq $override) { $reasons.Add('RECEIPT_FIELD_ABSENT: fps_override') }
    elseif ($override -ne 0) { $reasons.Add('INVALID_SOURCE_FRAMES: the run was paced by a persisted fps override; evidence is paced at the footage native fps') }
    $wrapped = Get-DvProp $Playback 'wrapped'
    if ($wrapped -isnot [bool]) { $reasons.Add('RECEIPT_FIELD_ABSENT: wrapped') }
    elseif ($wrapped) { $reasons.Add('INVALID_LOOPED: the timeline wrapped, jumped to the first frame or restarted') }
    if ($null -eq $wrapCount) { $reasons.Add('RECEIPT_FIELD_ABSENT: wrap_count') }
    elseif ($wrapCount -gt 0) { $reasons.Add("INVALID_LOOPED: wrap_count=$wrapCount") }
    # THIS run: the nonce the launcher generated (the expected one) must be well-formed (the real launcher's format) and the
    # app must have echoed exactly it on its summary line (the observed one).
    $expectedNonce = [string](Get-DvProp $Playback 'expectedRunNonce')
    $observedNonce = [string](Get-DvProp $Playback 'observedRunNonce')
    if ($expectedNonce -cnotmatch $script:RunNoncePattern) { $reasons.Add('RECEIPT_NOT_THIS_RUN: the receipt carries no well-formed expected run nonce (the 32-hex nonce the launcher generates), so it cannot be shown to be this run''s') }
    elseif ($observedNonce -cnotmatch $script:RunNoncePattern) { $reasons.Add('RECEIPT_NOT_THIS_RUN: the app echoed no well-formed run nonce on its summary line') }
    elseif ($observedNonce -cne $expectedNonce) { $reasons.Add('RECEIPT_NOT_THIS_RUN: the nonce the app echoed is not the nonce the launcher generated for this run; an earlier run wrote the receipt') }
    $manifestNonce = [string](Get-DvProp $Playback 'manifestRunNonce')
    if (-not [string]::IsNullOrEmpty($manifestNonce) -and $manifestNonce -cne $expectedNonce) { $reasons.Add('RECEIPT_NOT_THIS_RUN: the evidence manifest binds a different run nonce than the launcher result') }
    if ((Get-DvProp $Playback 'logShaBound') -ne $true) { $reasons.Add('RECEIPT_FIELD_ABSENT: the run log is not bound to the launcher result by its sha256 (logShaBound)') }
    if ((Get-DvProp $Playback 'settingsIsolated') -ne $true) { $reasons.Add('SETTINGS_NOT_ISOLATED: the app did not report its run-scoped settings store, so the venue''s persisted settings may have been read or rewritten') }
    # The job's own oracle block, when the job wrote one, must agree with the log and report no failures.
    $jobAdvanced = ConvertTo-DvInt64 (Get-DvProp $Playback 'jobSourceAdvanced')
    $jobRequired = ConvertTo-DvInt64 (Get-DvProp $Playback 'jobRequiredSourceFrames')
    if (($null -ne $jobAdvanced -and $jobAdvanced -ne $advanced) -or ($null -ne $jobRequired -and $jobRequired -ne $required)) {
        $reasons.Add('JOB_DISAGREES_WITH_LOG: the job''s source-frame block does not match the playback_smoke.summary line of the run log')
    }
    foreach ($f in @(Get-DvProp $Playback 'jobFailures')) { if ($null -ne $f -and [string]$f -ne '') { $reasons.Add("the job's own oracle reported: $f") } }
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
    Build the receipt's `playback` block. The proof is taken from the RUN'S OWN published records, not from a summary the
    job wrote about itself: the launcher's result.json (the nonce it generated, the sha256 of the run log it snapshotted) and
    the app's run log (its `playback_smoke.summary` line: source_advanced, required_source_frames, native_fps, pace_fps,
    fps_override, wrapped, wrap_count and the nonce it echoed). The job's own summary.json / manifest supply the clip id, the
    rehearsal flag and a cross-check. Present on every job terminal that published the run log, so a product failure after a
    sound 20 s run (GPU_RECON_FRAMES_ZERO, CPU_FALLBACK_DETECTED, ...) can be a FAIL while a run with no log stays INVALID.
    -RequestedConform: the leg spec's generatorArgs.playbackConform (Get-DvPlaybackProblems; 'on' never falls back to the native pace rule).
    #>
    param($Summary, $EvidenceManifest, $ResultJson, [AllowNull()][AllowEmptyString()][string]$LogText, [AllowNull()][AllowEmptyString()][string]$LogSha256,
          [Parameter(Mandatory)][string]$ExpectedClipId, [AllowNull()][AllowEmptyString()][string]$RequestedConform = '')
    $sf = Get-DvProp $Summary 'sourceFrames'
    $manifestLog = Get-DvProp $EvidenceManifest 'smokeRunLog'
    $evidence = Get-DvProp $ResultJson 'evidence'
    $declaredSha = [string](Get-DvProp (Get-DvProp $evidence 'runLogSnapshot') 'sha256')
    $manifestSha = [string](Get-DvProp $manifestLog 'sha256')
    $parsed = Get-DvSmokeSummaryFields -LogText $LogText
    $f = $parsed.fields
    $conformFields = @{}
    if ($null -ne $parsed.conformLine) { foreach ($m in [regex]::Matches([string]$parsed.conformLine, '(?<k>[A-Za-z0-9_]+)=(?<v>\S+)')) { $conformFields[$m.Groups['k'].Value] = $m.Groups['v'].Value } }
    $conformPlaybackField = ConvertTo-DvDouble $conformFields['playback_fps']
    $conformClipField = ConvertTo-DvDouble $conformFields['clip_fps']
    $wrappedField = ConvertTo-DvInt64 $f['wrapped']
    $bound = (-not [string]::IsNullOrEmpty($LogSha256)) -and ($declaredSha.ToLowerInvariant() -ceq $LogSha256) -and ([string]::IsNullOrEmpty($manifestSha) -or $manifestSha.ToLowerInvariant() -ceq $LogSha256)
    $pb = [ordered]@{
        oracle = 'source_advanced >= required_source_frames >= ceil(20 s x native fps), paced at native fps (or the proven conform playback fps), no fps override, no wrap, the app echoed the nonce the launcher generated; all re-derived from the run log'
        sourceAdvanced = (ConvertTo-DvInt64 $f['source_advanced'])
        requiredSourceFrames = (ConvertTo-DvInt64 $f['required_source_frames'])
        nativeFps = (ConvertTo-DvDouble $f['native_fps'])
        paceFps = (ConvertTo-DvDouble $f['pace_fps'])
        paceRule = 'native'
        conformPlaybackFps = $conformPlaybackField
        conformClipFps = $conformClipField
        conformLinePresent = ($null -ne $parsed.conformLine)
        summarySession = $(if ($null -ne $parsed.session) { [string]$parsed.session } else { $null })
        conformSession = $(if ($conformFields.ContainsKey('session')) { [string]$conformFields['session'] } else { $null })
        conformActive = (ConvertTo-DvInt64 $conformFields['conform_active'])
        conformEnvOverride = (ConvertTo-DvInt64 $conformFields['conform_env_override'])
        # HFR-NATIVE-60FPS-FULLRES-1 r19 (sol r18 B2): the measured-session conform line's MEASURED acceptance inputs (the timeline's source frames per wall
        # second, the largest jump between presented source frames, the audio sync requests while conformed); the conform leg specs' criteria judge them
        # through Get-DvCriteriaMetrics. $null when the line or the field is absent; a non-finite rate stays non-finite (it then fails every bound).
        conformSourceFps = (ConvertTo-DvDouble $conformFields['source_fps'])
        conformMaxJump = (ConvertTo-DvInt64 $conformFields['max_jump'])
        conformAudioSyncRequests = (ConvertTo-DvInt64 $conformFields['audio_sync_requests'])
        # r28 fix iteration 1 (adversary A2 on e052947c): the source frames the PRESENTED frames covered (the conform line's source_advanced), so a
        # frozen presentation tail is seen against the engine's own source_advanced (Get-DvCriteriaMetrics). $null when the line does not carry it.
        conformSourceAdvanced = (ConvertTo-DvInt64 $conformFields['source_advanced'])
        # r29 (adversary A2 on cf403dba): the painted coverage's first and last frame, and the engine's last target in the summary's own
        # coordinates (MainWindow.cpp playback_smoke.conform first_frame / last_frame / engine_target). $null when the line does not carry them.
        conformFirstFrame = (ConvertTo-DvInt64 $conformFields['first_frame'])
        conformLastFrame = (ConvertTo-DvInt64 $conformFields['last_frame'])
        conformEngineTarget = (ConvertTo-DvInt64 $conformFields['engine_target'])
        # r30 (Dot D2 on 71104385): how many times the painted frames stepped backward (a seek back; the conform line's backward). Get-DvCriteriaMetrics
        # judges no frozen tail across one: engine_target is the engine's high-water, not its final target. $null when the line does not carry it.
        conformBackward = (ConvertTo-DvInt64 $conformFields['backward'])
        fpsOverride = (ConvertTo-DvInt64 $f['fps_override'])
        wrapped = $(if ($null -eq $wrappedField) { $null } else { $wrappedField -ne 0 })
        wrapCount = (ConvertTo-DvInt64 $f['wrap_count'])
        expectedRunNonce = $(if ($null -ne (Get-DvProp $evidence 'runNonce')) { [string](Get-DvProp $evidence 'runNonce') } else { $null })
        observedRunNonce = $(if ($f.ContainsKey('run_nonce')) { [string]$f['run_nonce'] } else { $null })
        manifestRunNonce = $(if ($null -ne (Get-DvProp $manifestLog 'runNonce')) { [string](Get-DvProp $manifestLog 'runNonce') } else { $null })
        logSha256 = $(if ([string]::IsNullOrEmpty($LogSha256)) { $null } else { $LogSha256 })
        logShaBound = $bound
        settingsIsolated = [bool]$parsed.settingsIsolated
        jobOracleBlockPresent = ($null -ne $sf)
        jobSourceAdvanced = (Get-DvProp $sf 'sourceAdvanced')
        jobRequiredSourceFrames = (Get-DvProp $sf 'requiredSourceFrames')
        jobFailures = (Get-DvProp $sf 'failures')
        fixtureRehearsal = (Get-DvProp $Summary 'fixtureRehearsal')
        clipId = (Get-DvProp $Summary 'clipId')
        valid = $false
        invalidReasons = @()
    }
    # Which rule judged the pace, for a key to see (re-derived by Get-DvPlaybackProblems from the same recorded fields, never trusted).
    if ((Get-DvConformPaceProof -Playback $pb).proven) { $pb['paceRule'] = 'conform' }
    $problems = @(Get-DvPlaybackProblems -Playback $pb -ExpectedClipId $ExpectedClipId -RequestedConform $RequestedConform)
    $pb['valid'] = ($problems.Count -eq 0)
    $pb['invalidReasons'] = $problems
    $pb
}

function Get-DvScaleLabel {
    <#
    .SYNOPSIS
    The words a person reads for a playback scale FACTOR (SCALE-LABEL-CLARITY-1): 1 -> 'Full res (1x)', 2 -> 'Half res (0.5x)', 4 -> 'Quarter res (0.25x)', else 'Custom res (1/N)'.
    Mirrors PlaybackScaleLabel::resolutionLabel in platform/qt/PlaybackScaleLabel.h; a null or non-numeric factor (scale not yet known) gives 'unknown res'. The factor stays in receipt keys and leg ids.
    #>
    [CmdletBinding()]
    param([AllowNull()][object]$Factor)
    $n = 0
    if ($null -eq $Factor -or -not [int]::TryParse([string]$Factor, [ref]$n) -or $n -lt 1) { return 'unknown res' }
    switch ($n) {
        1 { 'Full res (1x)' }
        2 { 'Half res (0.5x)' }
        4 { 'Quarter res (0.25x)' }
        default { "Custom res (1/$n)" }
    }
}

function Get-DvScaleEvidence {
    <#
    .SYNOPSIS
    The receipt's `scale` block (DVE-SCALE2-LOOK-LEG-1 r2): the playback scale the leg REQUESTED (the spec's scaleFactor), the scale the app actually RENDERED at, and the
    verdict. The effective scale is read from the app's own run log: `scale_active_last` on the measured session's playback_smoke.summary line, else the effective= of the app's
    one-time `playback_scale_clamped_for_gpu_texture_route` line; absent from both it is the string 'UNKNOWN' (never the request, never 1).

    The CUDA texture route clamps every requested scale other than 1 to 1 (MainWindowGpuPreviewPolicy.h), so the two can differ. A leg's spec may DECLARE the effective scale a
    backend renders at (acceptedEffectiveScale.<backend>); otherwise the accepted effective scale is the request. Verdicts:
      HONOURED       effective == requested == accepted
      DECLARED_CLAMP effective == accepted != requested (the spec says this backend renders at accepted; the receipt still carries both numbers)
      NOT_HONOURED   effective is known and is not the accepted scale
      UNKNOWN        the app's rendered scale cannot be read
    `honoured` is true for HONOURED and DECLARED_CLAMP only. Pure and shared: the runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log.
    #>
    param([Parameter(Mandatory)]$Spec, [Parameter(Mandatory)][string]$Backend, [AllowNull()][AllowEmptyString()][string]$LogText)
    $requested = [int]$Spec.scaleFactor
    $accepted = $requested
    if ($null -ne $Spec.PSObject.Properties['acceptedEffectiveScale'] -and $null -ne $Spec.acceptedEffectiveScale -and $null -ne $Spec.acceptedEffectiveScale.PSObject.Properties[$Backend]) {
        $accepted = [int]$Spec.acceptedEffectiveScale.$Backend
    }
    $effective = $null
    $source = 'absent from the run log'
    $appRequest = $null
    $clampActive = $null
    if (-not [string]::IsNullOrEmpty($LogText)) {
        $f = (Get-DvSmokeSummaryFields -LogText $LogText).fields
        $active = ConvertTo-DvInt64 $f['scale_active_last']
        if ($null -ne $active -and $active -ge 1) { $effective = $active; $source = 'playback_smoke.summary scale_active_last' }
        $appRequest = ConvertTo-DvInt64 $f['scale_request_last']
        $clampFlag = ConvertTo-DvInt64 $f['gpu_texture_route_scale_clamp_active']
        if ($null -ne $clampFlag) { $clampActive = ($clampFlag -ne 0) }
        if ($null -eq $effective) {
            foreach ($line in (Get-DvLogRecordBodies -LogText $LogText)) {
                if ($line -match 'playback_scale_clamped_for_gpu_texture_route requested=(?<r>\d+) effective=(?<e>\d+)') { $effective = [int64]$Matches['e']; $source = 'playback_scale_clamped_for_gpu_texture_route effective (clamp line)'; $clampActive = $true; break }
            }
        }
    }
    $verdict = 'NOT_HONOURED'
    if ($null -eq $effective) { $verdict = 'UNKNOWN' }
    elseif ($effective -eq $accepted -and $accepted -eq $requested) { $verdict = 'HONOURED' }
    elseif ($effective -eq $accepted) { $verdict = 'DECLARED_CLAMP' }
    [ordered]@{
        requestedScale = $requested
        effectiveScale = $(if ($null -eq $effective) { 'UNKNOWN' } else { [int]$effective })
        acceptedEffectiveScale = $accepted
        appRequestScaleLast = $appRequest
        clampActive = $clampActive
        effectiveScaleSource = $source
        verdict = $verdict
        honoured = ($verdict -in @('HONOURED', 'DECLARED_CLAMP'))
    }
}

function Get-DvDisplayEvidence {
    <#
    .SYNOPSIS
    The receipt's `display` block (DUAL-VENUE-DISPLAY-MATRIX-1): the display mode the leg REQUESTED (the spec's optional `displayMode`; a spec that names none is a
    full-screen leg), the display mode the app actually RAN in, and the window it presented in -- all read from the app's own `gui_smoke.window_placement` line in the run log
    (the shared parser ConvertFrom-GuiSmokeDisplayLog), never from the spec. `observedMode` is 'UNKNOWN' when the run log has no readable placement line.
    `windowWidth/Height` is the app window and `previewWidth/Height` the preview area in it (logical pixels, as the app logs them); `presentationPhysical*` is the screen it sat on.

    FAIL CLOSED: `blocks` is true when the run is not a valid measurement under the mode the leg named. A requested windowed leg that ran full screen blocks (and the reverse).
    A leg whose spec NAMES a displayMode also blocks when the app's mode is unreadable or its placement was not verified. A legacy spec (no displayMode) keeps the behaviour it
    had before this block existed: an unreadable placement does not block it, so no existing leg changes. Pure and shared: the runner gates on it and Test-DvReceiptValid
    re-derives it from the hashed run log and the committed spec.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText)
    $named = Get-DvProp $Spec 'displayMode'
    $explicit = ($null -ne $named)
    $requested = $(if ($explicit) { [string]$named } else { 'fullscreen' })
    $observed = 'UNKNOWN'
    $verified = $null
    $placement = $null
    $parseFailure = $null
    if (-not [string]::IsNullOrEmpty($LogText)) {
        # The one parser (gui-smoke-display-identity.ps1: the smoke runner and the job use it too). Dot-sourced the way AttrCudaArtifacts does, once per call.
        # A parser that cannot be loaded is UNKNOWN (never a throw: a leg that ran always ends in a receipt), and UNKNOWN blocks a leg that names a mode.
        try {
            if (-not (Get-Command ConvertFrom-GuiSmokeDisplayLog -CommandType Function -ErrorAction SilentlyContinue)) {
                . (Join-Path $PSScriptRoot '..\gui-smoke-display-identity.ps1')
            }
            $placement = (ConvertFrom-GuiSmokeDisplayLog -LogText $LogText).placement
        } catch {
            $placement = $null
            $parseFailure = 'the display-identity parser could not read the run log'
        }
        if ($null -ne $placement) {
            if ([string]$placement.mode -cin @('fullscreen', 'windowed')) { $observed = [string]$placement.mode }
            $verified = [bool]$placement.verified
        }
    }
    $verdict = 'HONOURED'
    $reason = $null
    if ($observed -ceq 'UNKNOWN') {
        $verdict = 'UNKNOWN'
        $reason = $(if ($null -ne $parseFailure) { $parseFailure } else { 'the run log has no readable gui_smoke.window_placement line' }) + ', so the display mode the app ran in is unknown'
    } elseif ($observed -cne $requested) {
        $verdict = 'NOT_HONOURED'
        $reason = "requested $requested but the app ran $observed"
    } elseif ($explicit -and $verified -ne $true) {
        $verdict = 'NOT_HONOURED'
        $reason = "the app ran $observed as requested but did not verify the placement (gui_smoke.window_placement verified=0)"
    }
    $blocks = ($verdict -ceq 'NOT_HONOURED') -or ($verdict -ceq 'UNKNOWN' -and $explicit)
    [ordered]@{
        requestedMode = $requested
        explicit = $explicit
        observedMode = $observed
        observedSource = $(if ($null -ne $placement) { 'gui_smoke.window_placement' } else { 'absent from the run log' })
        placementVerified = $verified
        windowWidth = $(if ($null -ne $placement) { $placement.windowWidth } else { $null })
        windowHeight = $(if ($null -ne $placement) { $placement.windowHeight } else { $null })
        previewWidth = $(if ($null -ne $placement) { $placement.previewWidth } else { $null })
        previewHeight = $(if ($null -ne $placement) { $placement.previewHeight } else { $null })
        presentationPhysicalWidth = $(if ($null -ne $placement) { $placement.presentationPhysicalWidth } else { $null })
        presentationPhysicalHeight = $(if ($null -ne $placement) { $placement.presentationPhysicalHeight } else { $null })
        verdict = $verdict
        reason = $reason
        blocks = $blocks
    }
}

function Get-DvUnpacedEvidence {
    <#
    .SYNOPSIS
    CUDA-HEADROOM-1: did an unpaced leg (generatorArgs.unpaced = true) actually run unpaced? Read from the app's own `playback_smoke.pace_unpaced` line for the
    MEASURED session (Get-DvSmokeSessionLineFields), never from the spec. FAIL CLOSED: `blocks` is true when the leg asked for it and the line is absent, or says
    active other than 1, or drop_frame other than 0 -- a build without MLVAPP_PLAYBACK_UNPACED runs at the clip's own pace, and that pace wall is not throughput.
    A leg that does not ask for it never blocks. Pure and shared: the runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText)
    $requested = ((Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'unpaced') -eq $true)
    $active = $null
    $dropFrame = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    if ($requested) {
        $f = Get-DvSmokeSessionLineFields -LogText $LogText -Marker 'pace_unpaced'
        $active = ConvertTo-DvInt64 $f['active']
        $dropFrame = ConvertTo-DvInt64 $f['drop_frame']
        $verdict = 'NOT_HONOURED'
        if ($f.Count -eq 0) { $reason = 'the run log has no playback_smoke.pace_unpaced line for the measured session, so the app never lifted its pace guard' }
        elseif ($active -ne 1) { $reason = "playback_smoke.pace_unpaced says active=$($f['active']) (the app did not lift its pace guard)" }
        elseif ($dropFrame -ne 0) { $reason = "playback_smoke.pace_unpaced says drop_frame=$($f['drop_frame']) (drop-frame mode skips frames instead of rendering them)" }
        else { $verdict = 'HONOURED' }
    }
    [ordered]@{ requested = $requested; active = $active; dropFrame = $dropFrame; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED') }
}

function Get-DvLookLabelEvidence {
    <#
    .SYNOPSIS
    LOOK-FULLSCREEN-PARITY-1: the receipt's `lookLabel` block -- the "current look" text the app showed on screen, read from its own `gui_smoke.look_label` line
    (the LAST one in the run log), never from the spec. `shownText` is what the surface actually drew (the fullscreen corner label) or the status-bar label shows
    (windowed); `expectedText` is what the app derived from the look in force; `surface` is fullscreen_gpu_window / fullscreen_cpu_viewport / windowed_status_bar / none.
    `lookLabelShown` is true only when the surface is not none and it shows the expected, non-empty text. Informational: it never blocks a leg (a build without the
    line reports lookLabelShown=false with source 'absent from the run log'). Pure.
    #>
    param([AllowNull()][AllowEmptyString()][string]$LogText)
    $m = $null
    if (-not [string]::IsNullOrEmpty($LogText)) {
        foreach ($line in ($LogText -split "`r?`n")) {
            if ($line -match 'gui_smoke\.look_label shown="(?<shown>[^"]*)" expected="(?<expected>[^"]*)" surface=(?<surface>\S+) show_in_fullscreen=(?<sif>[01]) wait_ms=(?<wait>\d+)') {
                $m = $Matches.Clone()
            }
        }
    }
    if ($null -eq $m) {
        return [ordered]@{ source = 'absent from the run log'; lookLabelShown = $false; shownText = $null; expectedText = $null; surface = $null
                           showInFullscreen = $null; waitMs = $null; reason = 'the run log has no gui_smoke.look_label line' }
    }
    $shown = [string]$m['shown']
    $expected = [string]$m['expected']
    $surface = [string]$m['surface']
    $reason = $null
    if ($surface -ceq 'none') { $reason = 'no surface showed the look label (fullscreen with the label switched off, or no visible status bar)' }
    elseif ([string]::IsNullOrEmpty($shown)) { $reason = "the $surface surface drew no look label before the timed play" }
    elseif ($shown -cne $expected) { $reason = "the $surface surface shows '$shown' but the look in force is '$expected'" }
    [ordered]@{
        source = 'gui_smoke.look_label'
        lookLabelShown = ($null -eq $reason)
        shownText = $shown
        expectedText = $expected
        surface = $surface
        showInFullscreen = ([int]$m['sif'] -eq 1)
        waitMs = [int]$m['wait']
        reason = $reason
    }
}

function Get-DvInheritedWorkerOverrides {
    <#
    .SYNOPSIS
    CPU-S1-RECON-CHAIN-ATTRIBUTION-1 r4 (B1 and B2 on 70d55f42; env census in the r4 run dir): the thread-count variables the measured session's start line logs (`env_<name lower case>=`,
    "unset" when unset or empty, log_line_field::envFieldValue) whose value the leg's spec does not name. Returns @{ overrides = 'NAME=value' list; unreadable = names whose field is duplicated;
    reason = text or $null }. A field a build does not write is not read.
    Accepted values, per variable: exact "unset" (absent) and exact "0" (src/batch/WorkerThreadCount.h's int reads, mlvappEnvFlagEnabled, ForceSingleThread.cpp's envTruthy, libgomp and
    mlv_env_value_is_truthy all leave 0 inert). Nothing wider: "00", "+0", "-0" are ON for MLVAPP_FORCE_SINGLETHREAD (adversary A1 on 165d4a30). "0" is not accepted for the decoder count (video_mlv.c
    reads it as one decoder). The spec's own rawPrefetchDecoders sets what the two prefetch fields must say. A capped leg (-CapRequested) is not read for the four worker variables: its worker_threads=
    proves what they would change, and T12 under MLVAPP_FORCE_THREADS=12 is a run at 12. It IS read for the three its worker_threads= does not show.
    Literal MLVAPP_FORCE_SINGLETHREAD=unset logs its own field as "unset", but ForceSingleThread.cpp:55 then writes OMP_NUM_THREADS=1 at main.cpp:1868, so env_omp_num_threads=1 refuses it.
    #>
    param([Parameter(Mandatory)]$StartRead, [Parameter(Mandatory)]$Spec, [switch]$CapRequested)
    $unsetOrZero = @('unset', '0')
    $decoders = ConvertTo-DvInt64 (Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'rawPrefetchDecoders')
    $rows = [System.Collections.Generic.List[object]]::new()
    if (-not $CapRequested) {
        $rows.Add(@('env_playback_max_threads', 'MLVAPP_PLAYBACK_MAX_THREADS', $unsetOrZero))
        $rows.Add(@('env_force_threads', 'MLVAPP_FORCE_THREADS', $unsetOrZero))
        $rows.Add(@('env_force_singlethread', 'MLVAPP_FORCE_SINGLETHREAD', $unsetOrZero))
        $rows.Add(@('env_disable_playback_thread_cap', 'MLVAPP_DISABLE_PLAYBACK_THREAD_CAP', $unsetOrZero))
    }
    $rows.Add(@('env_omp_num_threads', 'OMP_NUM_THREADS', $unsetOrZero))
    # generatorArgs.rawPrefetchDecoders: 0 = the job sets MLVAPP_DISABLE_RAW_UINT16_PREFETCH=1, k = MLVAPP_RAW_UINT16_PREFETCH_DECODERS=k, absent = neither (playback-attr-3-cuda-job.ps1)
    $rows.Add(@('env_disable_raw_uint16_prefetch', 'MLVAPP_DISABLE_RAW_UINT16_PREFETCH', $(if ($null -ne $decoders -and $decoders -eq 0) { @('1') } else { $unsetOrZero })))
    $rows.Add(@('env_raw_uint16_prefetch_decoders', 'MLVAPP_RAW_UINT16_PREFETCH_DECODERS', $(if ($decoders -ge 1) { @([string]$decoders) } else { @('unset') })))
    $overrides = [System.Collections.Generic.List[string]]::new()
    $unreadable = [System.Collections.Generic.List[string]]::new()
    foreach ($row in $rows) {
        if ($StartRead.duplicates -contains $row[0]) { $unreadable.Add($row[1]); continue }
        $value = $StartRead.fields[$row[0]]
        if ($null -ne $value -and $value -cnotin $row[2]) { $overrides.Add("$($row[1])=$value") }
    }
    $parts = [System.Collections.Generic.List[string]]::new()
    if ($overrides.Count -gt 0) { $parts.Add("the leg's spec does not name $($overrides -join ', '), yet the app saw it in its environment (worker_threads=$($StartRead.fields['worker_threads'])); the venue's variable, not the leg, set a thread count, so it is not the arm its spec describes") }
    if ($unreadable.Count -gt 0) { $parts.Add("the measured session's playback_smoke.start line carries the field for $($unreadable -join ', ') more than once, so whether the leg ran under an inherited thread-count variable is unreadable") }
    @{ overrides = $overrides; unreadable = $unreadable; reason = $(if ($parts.Count -gt 0) { 'INHERITED_WORKER_OVERRIDE: ' + ($parts -join '; ') } else { $null }) }
}

function Get-DvWorkerThreadsEvidence {
    <#
    .SYNOPSIS
    VENUE-LEG-PLAYBACK-MAX-THREADS-ARG-1: did a leg that asks for a playback worker-thread cap (generatorArgs.playbackMaxThreads = n) run at n workers? Read from
    the app's own `playback_smoke.start` line for the MEASURED session (worker_threads=, Get-DvSmokeSessionLineFields) and from the job's summary.json proof
    fields: playbackMaxThreadsRequested (the cap the job was generated for), playbackMaxThreadsSmoke (the smoke runner's result.json playbackMaxThreads) and
    playbackWorkerThreadsRead (the job's own worker_threads= read). FAIL CLOSED: `blocks` is true when the leg asked for n and the run log's count is absent,
    unparsable or not n, when there is no summary, when a summary lacks a proof field and its result is not one of the runner's own product-failure
    terminals ($script:FailureTerminals, compared exactly), or when any proof field a summary states is not n. Only such a terminal's summary may omit
    the proof (only the success summary gains it), and it is then judged on the run log; a summary with no result or any other result must carry it
    (adversary r1 A1). A leg that does not ask blocks only when its start line shows an inherited thread-count variable (INHERITED_WORKER_OVERRIDE, Get-DvInheritedWorkerOverrides); a leg
    that asks is NOT_HONOURED under the same refusal. Pure and shared: the runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log and summary.json.
    PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2 (adversary A2 on #726): a start line that carries worker_threads= more than once is refused
    (DUPLICATE_START_FIELD), never last-key-wins; and a failure terminal that omits a proof field is UNPROVEN (blocks = false, so its FAIL stands), never
    HONOURED, because the run log alone was forgeable by a launcher env value on a build before r2.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText, [AllowNull()]$Summary)
    $raw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'playbackMaxThreads'
    $requested = ConvertTo-DvInt64 $raw
    $observed = $null
    $inheritedOverrides = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    if ($null -ne $raw) {
        $verdict = 'NOT_HONOURED'
        $read = Get-DvSmokeSessionLineRead -LogText $LogText -Marker 'start'
        $f = $read.fields
        $observed = ConvertTo-DvInt64 $f['worker_threads']
        $resultToken = Get-DvProp $Summary 'result'
        $mayOmit = ($resultToken -is [string]) -and ($resultToken -cin $script:FailureTerminals)
        $omitted = [System.Collections.Generic.List[string]]::new()
        $problems = [System.Collections.Generic.List[string]]::new()
        if ($null -eq $requested -or $requested -lt 1 -or $requested -gt 16) { $problems.Add("the spec's playbackMaxThreads '$raw' is not an integer from 1 to 16") }
        elseif (-not $read.found) { $problems.Add('the worker count is unreadable: the run log has no playback_smoke.start line for the measured session') }
        elseif ($read.duplicates -contains 'worker_threads') { $problems.Add("DUPLICATE_START_FIELD: the measured session's playback_smoke.start line carries worker_threads= more than once, so the worker count is unreadable (a second one can only come from a launcher env value)") }
        elseif ($null -eq $observed) { $problems.Add("the worker count is unreadable: the measured session's playback_smoke.start line has no integer worker_threads= ('$($f['worker_threads'])')") }
        elseif ($observed -ne $requested) { $problems.Add("the measured session's playback_smoke.start says worker_threads=$observed, not the $requested the leg names") }
        if ($null -eq $Summary) { $problems.Add('the worker count is unreadable: the job published no summary.json to carry its proof') }
        else {
            foreach ($name in @('playbackMaxThreadsRequested', 'playbackMaxThreadsSmoke', 'playbackWorkerThreadsRead')) {
                $has = ($Summary -is [System.Collections.IDictionary] -and $Summary.Contains($name)) -or ($Summary -isnot [System.Collections.IDictionary] -and $null -ne $Summary.PSObject.Properties[$name])
                $value = Get-DvProp $Summary $name
                if (-not $has -and $mayOmit) { $omitted.Add($name); continue }
                if ($null -eq $value) { $problems.Add("the worker count is unreadable: summary.json $name is $(if ($has) { 'null' } else { 'absent' })"); continue }
                $parsed = ConvertTo-DvInt64 $value
                if ($null -eq $parsed -or $parsed -ne $requested) { $problems.Add("summary.json $name is '$value', not $requested") }
            }
        }
        if ($read.found) {
            $inherited = Get-DvInheritedWorkerOverrides -StartRead $read -Spec $Spec -CapRequested
            if ($inherited.overrides.Count -gt 0) { $inheritedOverrides = $inherited.overrides -join ' ' }
            if ($null -ne $inherited.reason) { $problems.Add($inherited.reason) }
        }
        if ($problems.Count -gt 0) { $reason = $problems -join '; ' }
        elseif ($omitted.Count -gt 0) {
            $verdict = 'UNPROVEN'
            $reason = "WORKER_PROOF_ABSENT: the job ended $resultToken and its summary.json omits $($omitted -join ', '); the run log alone does not prove the worker count, so it is not honoured (the failure terminal's own FAIL stands)"
        }
        else { $verdict = 'HONOURED' }
    }
    else {
        # CPU-S1-RECON-CHAIN-ATTRIBUTION-1 r3 (sol A2 on #754, adversary A1 on 6ffe18f8): a leg that names NO cap still runs at a count its venue chose when the
        # venue's environment holds a variable the app reads for its worker count (the smoke runner sets MLVAPP_PLAYBACK_MAX_THREADS only for -Threads and never
        # removes an inherited one; nothing removes the others), so a control meant to differ from a capped arm in worker count can run at the arm's count and
        # compare CLEAN. A value the spec does not name is INHERITED_WORKER_OVERRIDE and blocks (Get-DvInheritedWorkerOverrides); a duplicated field is unreadable and blocks too. A start
        # line without the field (a build before it existed) or no start line at all stays NOT_REQUESTED, so a leg that legitimately runs uncapped is unchanged. No tool in tools/ or
        # .github sets any of these variables for a dual-venue leg, except the job's own rawPrefetchDecoders route.
        $startRead = Get-DvSmokeSessionLineRead -LogText $LogText -Marker 'start'
        if ($startRead.found) {
            $inherited = Get-DvInheritedWorkerOverrides -StartRead $startRead -Spec $Spec
            if ($null -ne $inherited.reason) {
                $verdict = 'INHERITED_WORKER_OVERRIDE'
                $observed = ConvertTo-DvInt64 $startRead.fields['worker_threads']
                $inheritedOverrides = ($inherited.overrides -join ' ')
                $reason = $inherited.reason
            }
        }
    }
    [ordered]@{ requested = $requested; observed = $observed; inheritedOverrides = $inheritedOverrides; verdict = $verdict; reason = $reason; blocks = ($verdict -cin @('NOT_HONOURED', 'INHERITED_WORKER_OVERRIDE')) }
}

function ConvertTo-DvSessionKey {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r12 (r9 keys on 4a61e684, adversary A1): the canonical text of a session integer, so equal integers compare equal
    whatever their spelling (`3`, `03`, `+3`, `003`, a full-width `3`). A negative non-zero integer keeps its minus; `-0` is `0`. Digits only (no culture, no overflow);
    a Unicode decimal digit is read as its value.
    #>
    param([string]$Sign, [Parameter(Mandatory)][string]$Digits)
    if ($Digits -cnotmatch '^[0-9]+$') { $Digits = -join ($Digits.ToCharArray() | ForEach-Object { [string][int][char]::GetNumericValue($_) }) }
    $d = $Digits.TrimStart('0')
    if ($d -eq '') { return '0' }
    if ($Sign -ceq '-') { return '-' + $d }
    return $d
}

# HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r14: the optional log prefix a canonical session record may carry ([utc] [LEVEL] [0xthread] ), shared by
# every whole-line record pattern Get-DvCanonicalRecordPattern builds.
$script:DvLogRecordPrefix = '(\[[^\]]*\] \[[A-Za-z]+\] (\[0x[0-9A-Fa-f]+\] )?)?'

function Get-DvCanonicalRecordPattern {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r14: the whole-line pattern of a canonical session record, `<optional log prefix><marker> session=<Session>
    <Fields>` and nothing else. Session and Fields are regex text. One builder, so a reader's own record pattern and the "another session's record" test in
    Test-DvRecordMentionsSession can never disagree on what canonical means.
    #>
    param([Parameter(Mandatory)][string]$Marker, [Parameter(Mandatory)][string]$Session, [Parameter(Mandatory)][string]$Fields)
    '^' + $script:DvLogRecordPrefix + [regex]::Escape($Marker) + ' session=' + $Session + ' ' + $Fields + '$'
}

function Test-DvRecordMentionsSession {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r14 (r12 keys opus and fable key3 on e19e383d; fable's ruling, adopted by the hub): is this record body a
    mention of the measured session? The test is INVERTED. A body that carries the marker (case-sensitive, not followed by a word character) is a mention UNLESS
    the whole body is a canonical record (Get-DvCanonicalRecordPattern with the reader's own Fields) whose session is a plain decimal integer with at most one
    sign, in ASCII digits, that differs from the measured session as an integer (ConvertTo-DvSessionKey: `03`, `+3` and `-0`/`0` compare by value). So a
    mention needs no particular spelling of the session at all: any separator (`session -> 3`, `session = + 3`), a quote, a hex or decimal-point value, a digit
    outside ASCII, a key in another case, a session cut off with the body at its first ` (`, or no session field at all. Anything that is neither the canonical
    record of another session nor a body without the marker counts for the measured session, so it makes the state unreadable rather than being skipped while
    a conflicting canonical record passes (fail closed). The only body skipped is the app's own canonical record of another session (`session=4 ...`). Before
    r14 the helper listed the session spellings it would recognise, and the sixth hole in that list (a + or - in the separator) is why it no longer does.
    Pure; shared by the recon_release and prep_route readers.
    #>
    param([AllowNull()][AllowEmptyString()][string]$Body, [Parameter(Mandatory)][string]$Marker, [Parameter(Mandatory)][string]$Session,
          [Parameter(Mandatory)][string]$Fields)
    if ([string]::IsNullOrEmpty($Body)) { return $false }
    if ($Body -cnotmatch ('^(?=.*' + [regex]::Escape($Marker) + '(?!\w))')) { return $false }
    $other = [regex]::Match($Body, (Get-DvCanonicalRecordPattern -Marker $Marker -Session '(?<dvsign>[+\-]?)(?<dvdigits>[0-9]+)' -Fields $Fields))
    if (-not $other.Success) { return $true }
    $want = $(if ($Session -cmatch '^\d+$') { ConvertTo-DvSessionKey -Sign '' -Digits $Session } else { $Session })
    $key = ConvertTo-DvSessionKey -Sign $other.Groups['dvsign'].Value -Digits $other.Groups['dvdigits'].Value
    return ($key -ceq $want)
}

function Get-DvPrepConflatedMoveEvidence {
    <#
    .SYNOPSIS
    PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1: did a leg that asks for the #397 prep-conflated-move switch (generatorArgs.prepConflatedMove = true, which sets
    MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE=1) run with the switch active? Read ONLY from the app's own `playback_smoke.prep_route session=<n>
    prep_conflated_move_active=<0|1>` record for the MEASURED session (the app's decision, on a line that carries no env-derived text), never from the start line,
    the spec, the launcher's env block or the job's summary: a switch value in a receipt is a request, not engagement. FAIL CLOSED: `blocks` is true when the leg
    asked and there is no such record, or it says anything but a clean 1. A binary that emits no prep_route record (#397 5fb4b801 and every build before r2 of
    this card) therefore never counts; its reason says ROUTE_NOT_ENGAGED. A leg that does NOT ask blocks only when the app reports the switch on (the OFF arm
    inherited the variable: PREP_CONFLATED_MOVE_INHERITED) or a prep_route record is present but unreadable; with no record, or a clean 0, it is unchanged.
    Pure and shared: the runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText)
    $raw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'prepConflatedMove'
    $requested = ($raw -is [bool]) -and $raw
    $observed = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    # adversary r1 A1 (r2): the start line carries launcher env values, and an app before r2 let CR/LF in one of them become a separator, so a field read there
    # can be forged. Engagement is read from the prep_route record only, matched against the WHOLE line: an optional log prefix ([utc] [LEVEL] [0xthread]),
    # the marker, the measured session, the one field with the value 0 or 1, an optional Qt location suffix, and nothing else. A prep_route text embedded in
    # another record (after a separator an old app wrote) never matches, and a second record for the session is unreadable, never a PASS.
    # r3 (sol ADV-A1 on #726) and r4 (r3 adversary A1 and A2, Grok G1): every line is read as its record body (Get-DvLogRecordBodies strips the Qt location
    # suffix from its first ` (`, whatever the path holds) BEFORE a mention is counted or the record matched, so a build path like `C:/build (x/` keeps the
    # reported state and a path holding `playback_smoke.prep_route session=<n>` adds no mention. A mention inside another record's BODY still counts.
    $records = 0
    $mentions = 0
    if (-not [string]::IsNullOrEmpty($LogText)) {
        $session = (Get-DvSmokeSummaryFields -LogText $LogText).session
        if ($null -ne $session) {
            $fields = 'prep_conflated_move_active=(?<v>[01])'
            $whole = Get-DvCanonicalRecordPattern -Marker 'playback_smoke.prep_route' -Session ([regex]::Escape([string]$session)) -Fields $fields
            # r14 (r12 keys on e19e383d): a mention is any record body that carries the marker and is not the whole-line canonical record of ANOTHER session
            # (Test-DvRecordMentionsSession, inverted), so no spelling of the measured session, and no record without one, is ever skipped.
            foreach ($l in (Get-DvLogRecordBodies -LogText $LogText)) {
                if (-not (Test-DvRecordMentionsSession -Body $l -Marker 'playback_smoke.prep_route' -Session ([string]$session) -Fields $fields)) { continue }
                $mentions++
                $m = [regex]::Match($l, $whole)
                if ($m.Success) { $records++; $observed = [int]$m.Groups['v'].Value }
            }
        }
    }
    if ($records -ne 1 -or $mentions -ne 1) { $observed = $null }
    $unreadable = 'the switch state is unreadable: the measured session''s playback_smoke.prep_route record is not exactly one whole line of the form playback_smoke.prep_route session=<n> prep_conflated_move_active=<0|1>, or there is more than one'
    if ($requested) {
        $verdict = 'NOT_HONOURED'
        if ($mentions -eq 0) { $reason = 'ROUTE_NOT_ENGAGED: the run log has no playback_smoke.prep_route record for the measured session, so the binary does not report which prep queue ran' }
        elseif ($null -eq $observed) { $reason = $unreadable }
        elseif ($observed -ne 1) { $reason = "ROUTE_NOT_ENGAGED: the measured session's playback_smoke.prep_route says prep_conflated_move_active=$observed, not the 1 the leg names" }
        else { $verdict = 'HONOURED' }
    }
    elseif ($mentions -gt 0) {
        # adversary r1 A1: an absent key sets nothing, so a launcher that already carries the variable runs the OFF arm ON. A leg that does not ask counts only if the
        # app does not report the switch on (a binary that emits no record, or says 0, is unchanged).
        if ($null -eq $observed) { $verdict = 'NOT_HONOURED'; $reason = $unreadable }
        elseif ($observed -eq 1) { $verdict = 'NOT_HONOURED'; $reason = 'PREP_CONFLATED_MOVE_INHERITED: the leg does not ask for the switch but the app reports prep_conflated_move_active=1 (the variable came from the launcher''s environment), so this run is not the OFF arm' }
    }
    [ordered]@{ requested = $requested; observed = $observed; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED') }
}

function Get-DvReconReleaseDeferredEvidence {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r7: did a leg that asks for the deferred-release kill switch (generatorArgs.disableReconReleaseDeferred =
    true, which sets MLVAPP_GPU_RECON_RELEASE_DEFERRED=0) run with the lever OFF? Read ONLY from the app's own `playback_smoke.recon_release session=<n>
    deferred_enabled=<0|1> deferred=<n> sync=<n> drained=<n> max_depth=<n> outstanding_at_stop=<n>` record for the MEASURED session, matched as a whole line
    (an optional log prefix and Qt location suffix, nothing else), never from the start line, the spec or the launcher's env block. FAIL CLOSED: `blocks` is
    true when the leg asked and there is no such record (a binary before r7 emits none), the record is unreadable or not unique, or it says deferred_enabled=1.
    A leg that does not ask blocks only when the app reports deferred_enabled=0 (the head arm inherited the variable: RECON_RELEASE_DEFERRED_INHERITED) or a
    record is present but unreadable; with no record (a control binary before r7), or a clean deferred_enabled=1, it is unchanged. The counts (deferred, sync,
    drained, max_depth, outstanding_at_stop) are returned for the reader's own gates (the head arm's deferred > 0 is a gate there, not here: a cpu leg
    retains no token and reports deferred=0 legitimately). Pure and shared: the runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText)
    $raw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'disableReconReleaseDeferred'
    $requested = ($raw -is [bool]) -and $raw
    $observed = $null
    $counts = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    $records = 0
    $mentions = 0
    if (-not [string]::IsNullOrEmpty($LogText)) {
        $session = (Get-DvSmokeSummaryFields -LogText $LogText).session
        if ($null -ne $session) {
            $fields = 'deferred_enabled=(?<v>[01]) deferred=(?<deferred>0|[1-9][0-9]{0,18}) sync=(?<sync>0|[1-9][0-9]{0,18}) drained=(?<drained>0|[1-9][0-9]{0,18})' +
                ' max_depth=(?<depth>0|[1-9][0-9]{0,8}) outstanding_at_stop=(?<outstanding>0|[1-9][0-9]{0,8})'
            $whole = Get-DvCanonicalRecordPattern -Marker 'playback_smoke.recon_release' -Session ([regex]::Escape([string]$session)) -Fields $fields
            # r14 (r12 keys opus and fable key3 on e19e383d): a mention is ANY record body that carries the marker, whatever it says about the session (any
            # separator, sign, quote, field order, the session before the marker, a cut body, no session at all), UNLESS the whole body is the canonical record
            # of ANOTHER session (`session=31 ...`, `session=4 ...`): Test-DvRecordMentionsSession is inverted, so a noncanonical duplicate is counted and makes
            # the state unreadable rather than being skipped while a conflicting canonical record passes. Only the canonical whole line ($whole) is ever read.
            foreach ($l in (Get-DvLogRecordBodies -LogText $LogText)) {
                if (-not (Test-DvRecordMentionsSession -Body $l -Marker 'playback_smoke.recon_release' -Session ([string]$session) -Fields $fields)) { continue }
                $mentions++
                $m = [regex]::Match($l, $whole)
                if ($m.Success) {
                    $records++
                    $observed = [int]$m.Groups['v'].Value
                    $counts = [ordered]@{ deferred = [decimal]$m.Groups['deferred'].Value; sync = [decimal]$m.Groups['sync'].Value; drained = [decimal]$m.Groups['drained'].Value
                                          maxDepth = [int]$m.Groups['depth'].Value; outstandingAtStop = [int]$m.Groups['outstanding'].Value }
                }
            }
        }
    }
    if ($records -ne 1 -or $mentions -ne 1) { $observed = $null; $counts = $null }
    $unreadable = 'the lever state is unreadable: the measured session''s playback_smoke.recon_release record is not exactly one whole line of the form playback_smoke.recon_release session=<n> deferred_enabled=<0|1> deferred=<n> sync=<n> drained=<n> max_depth=<n> outstanding_at_stop=<n>, or there is more than one'
    if ($requested) {
        $verdict = 'NOT_HONOURED'
        if ($mentions -eq 0) { $reason = 'ROUTE_NOT_ENGAGED: the run log has no playback_smoke.recon_release record for the measured session, so the binary does not report whether the release was deferred' }
        elseif ($null -eq $observed) { $reason = $unreadable }
        elseif ($observed -ne 0) { $reason = "ROUTE_NOT_ENGAGED: the measured session's playback_smoke.recon_release says deferred_enabled=$observed, not the 0 the leg names" }
        else { $verdict = 'HONOURED' }
    }
    elseif ($mentions -gt 0) {
        # An absent key sets nothing, so a launcher that already carries MLVAPP_GPU_RECON_RELEASE_DEFERRED=0 runs the head arm with the lever off.
        if ($null -eq $observed) { $verdict = 'NOT_HONOURED'; $reason = $unreadable }
        elseif ($observed -eq 0) { $verdict = 'NOT_HONOURED'; $reason = 'RECON_RELEASE_DEFERRED_INHERITED: the leg does not ask for the kill switch but the app reports deferred_enabled=0 (the variable came from the launcher''s environment), so this run is not the head arm' }
    }
    [ordered]@{ requested = $requested; observed = $observed; counts = $counts; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED') }
}

function Get-DvDueWakeDeliveryEvidence {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r1: did a leg run with the due-wake delivery its arm names? A leg that asks for the posted wake
    (generatorArgs.dueWakeDeliveryPost = true, which sets MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY=post) must report delivery_kind=post; any other leg is the head
    arm and must not report a kind the variable selects. Read ONLY from the MEASURED session's own `playback_due_wake armed=<n> ...` record (the one after
    its playback_smoke.pace_summary; the delivery_kind field r1 appends, cross-checked with the same line's delivery= mechanisms), never from another Play's
    line, the start line, the spec or the launcher's env block. FAIL CLOSED: `blocks` is true when the leg asked and the measured line carries no
    delivery_kind (a binary before r1 emits none, or the measured Play printed no line), the field is unreadable or contradicts delivery=, or the value is not
    post. A leg that does not ask blocks when the measured line reports post or notifier (DUE_WAKE_DELIVERY_INHERITED: the variable came from the launcher)
    or is unreadable; send, none (no wake: an unpaced leg), qtimer and mixed (a fallback), and no delivery_kind at all (a control binary before r1) leave it
    unchanged. Pure and shared: the runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText)
    $raw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'dueWakeDeliveryPost'
    $requested = ($raw -is [bool]) -and $raw
    $observed = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    $mentions = 0
    $unreadableSeen = $false
    # r1 adversary pass 1 A3: the MEASURED Play's own line only. The app prints playback_due_wake once per smoke session, directly after that session's
    # playback_smoke.pace_summary (MainWindow.cpp, the smoke summary path), so the measured line is the first playback_due_wake record after the measured
    # session's pace_summary and before any other pace_summary; another Play's line never stands in for it, and a measured Play with no line has none.
    $candidates = New-Object System.Collections.Generic.List[string]
    $paces = 0
    if (-not [string]::IsNullOrEmpty($LogText)) {
        $session = (Get-DvSmokeSummaryFields -LogText $LogText).session
        if ($null -ne $session) {
            $paceRx = '(?:^|\s)playback_smoke\.pace_summary session=' + [regex]::Escape([string]$session) + '(?:\s|$)'
            $open = $false
            foreach ($l in (Get-DvLogRecordBodies -LogText $LogText)) {
                if ($l -match '(?:^|\s)playback_smoke\.pace_summary session=') { $open = ($l -match $paceRx); if ($open) { $paces++ }; continue }
                # r1 adversary pass 2 A4: the window stays open to the next pace_summary, so a second line in it is seen (and is unreadable)
                if ($open -and $l -match '(?:^|\s)playback_due_wake armed=') { $candidates.Add($l) }
            }
        }
    }
    # delivery_kind is the SELECTED delivery (send / post / notifier; qtimer when the wake was built on a QTimer; none with no wake), never rewritten by a
    # fallback arm; delivery= is what this Play's arms used (native-msg, qtimer, notifier, "+"-joined when they differed, none with no arm)
    $mechanism = $null
    $withKind = @($candidates | Where-Object { $_ -match '(?:^|\s)delivery_kind=' })
    if ($withKind.Count -gt 0) {
        $mentions = $withKind.Count
        if ($paces -ne 1 -or $candidates.Count -ne 1) { $unreadableSeen = $true }
        else {
            $l = $candidates[0]
            $fields = [regex]::Matches($l, '(?:^|\s)delivery_kind=(?<k>\S*)')
            $mech = [regex]::Matches($l, '(?:^|\s)delivery=(?<m>\S*)')
            if ($fields.Count -ne 1 -or $fields[0].Groups['k'].Value -cnotmatch '^(send|post|notifier|qtimer|none)$' -or $mech.Count -ne 1 -or $mech[0].Groups['m'].Value -cnotmatch '^(native-msg|qtimer|notifier|none)(\+(native-msg|qtimer|notifier))*$') { $unreadableSeen = $true }
            else {
                # r1 adversary pass 3 A1: the selection and the arms must agree -- none only with no arm (delivery=none), qtimer only on QTimer arms, notifier
                # only on notifier (or fallback QTimer) arms, send / post only on native-message (or fallback QTimer) arms; any other pair is contradictory
                $k = $fields[0].Groups['k'].Value
                $parts = @($mech[0].Groups['m'].Value -split '\+')
                $allowed = switch ($k) { 'none' { @('none') } 'qtimer' { @('qtimer', 'none') } 'notifier' { @('notifier', 'qtimer', 'none') } default { @('native-msg', 'qtimer', 'none') } }
                if (@($parts | Where-Object { $_ -cnotin $allowed }).Count -gt 0) { $unreadableSeen = $true }
                else { $observed = $k; $mechanism = $mech[0].Groups['m'].Value }
            }
        }
    }
    $unreadable = 'the due-wake delivery is unreadable: the measured session''s playback_due_wake line (after its playback_smoke.pace_summary, before the next) is not exactly one line carrying one delivery_kind of send, post, notifier, qtimer, none and one delivery= of native-msg, qtimer, notifier, none or a "+"-join (MainWindow::playbackDueWakeSummaryLine)'
    if ($requested) {
        $verdict = 'NOT_HONOURED'
        if ($mentions -eq 0) { $reason = 'ROUTE_NOT_ENGAGED: the measured session has no playback_due_wake line with a delivery_kind (after its playback_smoke.pace_summary), so the binary does not report how its wake was delivered' }
        elseif ($null -eq $observed) { $reason = $unreadable }
        elseif ($observed -cne 'post') { $reason = "ROUTE_NOT_ENGAGED: the measured session's playback_due_wake says delivery_kind=$observed, not the post the leg names" }
        # r1 adversary pass 1 A2: the posted arm counts only when every arm of the measured Play went by the native message
        elseif ($mechanism -cne 'native-msg') { $reason = "DUE_WAKE_DELIVERY_FELL_BACK: the measured Play's arms used delivery=$mechanism, not only the native message the posted arm names" }
        else { $verdict = 'HONOURED' }
    }
    elseif ($mentions -gt 0) {
        # r1 adversary passes 1 A1 / 2 A1: a selection the variable makes (post, notifier) is inherited whatever the arms used; none (no wake: an
        # unpaced leg) and qtimer (no native wake built) are the app's own; send is the head arm, and counts only when no arm fell back (pass 1 A2)
        if ($null -eq $observed) { $verdict = 'NOT_HONOURED'; $reason = $unreadable }
        elseif ($observed -cin @('post', 'notifier')) { $verdict = 'NOT_HONOURED'; $reason = "DUE_WAKE_DELIVERY_INHERITED: the leg does not ask for the posted wake but the app reports delivery_kind=$observed (MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY came from the launcher), so this run is not the head arm" }
        elseif ($observed -ceq 'send' -and $mechanism -cnotin @('native-msg', 'none')) { $verdict = 'NOT_HONOURED'; $reason = "DUE_WAKE_DELIVERY_FELL_BACK: the head arm's measured Play used delivery=$mechanism, not only the sent native message" }
    }
    [ordered]@{ requested = $requested; observed = $observed; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED') }
}

# The flavors the app can apply (src/batch/LookAssistAnalysis.cpp lookAssistFlavorName). The app's `look_assist_flavor=none` (nothing applied) is not among them.
$script:DvAppliedLookFlavors = @('classic', 'cinematic', 'film', 'vision50d')

function Get-DvLookAssistEvidence {
    <#
    .SYNOPSIS
    PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 (D4b): did a leg whose spec says Look Assist ON (a LOOK leg, or generatorArgs.forceLookAssist = true) actually
    run with it on? Read from the app's own LAST `gui_smoke.visual_state` line in the run log (look_assist_enabled), never from the spec or the job's
    summary. FAIL CLOSED: `blocks` is true when the leg asked for it and the line is absent, carries no look_assist_enabled, or says anything but 1.
    A leg that does not ask for it never blocks.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText)
    $requested = ($null -ne (Get-DvProp $Spec 'look')) -or ((Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'forceLookAssist') -eq $true)
    # VENUE-NATIVE-LEG-LOOKLEG-LABEL-1 r2 (adversary pass 4 A3): `asked` is what Invoke-VenueLeg actually FORWARDS -- a look leg, or a speed leg with generatorArgs.forceLookAssist true.
    # An inert `look` object on a speed spec trips the gate's older `requested` predicate (left as it was) but forwards nothing, so it never backs the lookAssistOn claim.
    $legTypeRaw = Get-DvProp $Spec 'legType'; $forceRaw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'forceLookAssist'
    $asked = (($legTypeRaw -is [string]) -and ($legTypeRaw -ceq 'look')) -or (($forceRaw -is [bool]) -and $forceRaw)
    $reported = $null
    $flavor = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    if ($requested) {
        $found = $null
        # VENUE-NATIVE-LEG-LOOKLEG-LABEL-1 r2 (Dot bd11a92): every flavor token of every visual_state record, so duplicate records that disagree can be told from one that is repeated
        $flavorsSeen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        if (-not [string]::IsNullOrEmpty($LogText)) {
            # r4 (r3 adversary A2 on #726): matched on the record body, so a Qt location path never selects (or answers for) the visual_state line
            foreach ($line in (Get-DvLogRecordBodies -LogText $LogText)) {
                if ($line -match '(^|\s)event=gui_smoke\.visual_state(\s|$)') {
                    $found = $line
                    foreach ($ft in [regex]::Matches($line, '(^|\s)look_assist_flavor=(?<f>\S*)')) { [void]$flavorsSeen.Add($ft.Groups['f'].Value) }
                }
            }
        }
        $verdict = 'NOT_HONOURED'
        if ($null -eq $found) { $reason = 'the run log has no gui_smoke.visual_state line, so nothing shows the app ran with Look Assist on' }
        else {
            $m = [regex]::Match($found, '(^|\s)look_assist_enabled=(?<v>\S+)')
            if (-not $m.Success) { $reason = 'the app''s gui_smoke.visual_state line carries no look_assist_enabled' }
            else {
                $reported = $m.Groups['v'].Value
                # VENUE-NATIVE-LEG-LOOKLEG-LABEL-1 r1 (adversary A1): the flavor the app applied, from the SAME record body (a Qt location suffix never answers for it)
                # r2: ONE flavor or none. (1) the flavor comes from the SAME record that says look_assist_enabled (adversary pass 3 A1: an earlier record's flavor is never borrowed by a
                # later record that names none); (2) two different flavor values anywhere in the visual_state records mean the app did not say which one ran, so none is proven (never
                # "the first" or "the last"); (3) it must be one the app can apply (adversary pass 3 A2: the app prints `none` when it applied no flavor, and `unknown` is a placeholder).
                # pass 4 A1: an EMPTY token (`look_assist_flavor=`) is a token too (`\S*`), so a duplicate that names nothing makes the record ambiguous instead of being skipped.
                # pass 4 A2: two contradictory look_assist_enabled tokens in the answering record are no unambiguous `on` -- no flavor is proven from that record.
                $lastFlavors = @([regex]::Matches($found, '(^|\s)look_assist_flavor=(?<f>\S*)') | ForEach-Object { $_.Groups['f'].Value } | Select-Object -Unique)
                $enabledValues = @([regex]::Matches($found, '(^|\s)look_assist_enabled=(?<v>\S*)') | ForEach-Object { $_.Groups['v'].Value } | Select-Object -Unique)
                if ($flavorsSeen.Count -eq 1 -and $lastFlavors.Count -eq 1 -and $enabledValues.Count -eq 1 -and ($script:DvAppliedLookFlavors -ccontains $lastFlavors[0])) { $flavor = $lastFlavors[0] }
                if ($reported -cne '1') { $reason = "the app's gui_smoke.visual_state line says look_assist_enabled=$reported (the leg asks for Look Assist on)" }
                else { $verdict = 'HONOURED' }
            }
        }
    }
    [ordered]@{ requested = $requested; asked = $asked; reported = $reported; flavor = $flavor; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED') }
}

# --- leg sets and the display matrix (DUAL-VENUE-DISPLAY-MATRIX-1) -------------------------------------------------------
# A LEG SET is a tracked list of committed leg specs that one run executes together. The set does not define legs: every cell is still an ordinary committed leg spec (so every receipt
# is bound to a committed spec by its sha256, exactly as for a single leg), and the set only says which specs make up the matrix, and in what order a run walks them.
$script:LegSetSchema = 'mlv-app/dual-venue-legset/v1'
$script:LegSetsRelativeDir = 'tools/profiling/dual-venue/legsets'

function Get-DvLegSetPlan {
    <#
    .SYNOPSIS
    Expand a leg set into the planned legs of a run: one entry per (leg spec x backend the spec lists) cell and repeat. Pure: reads the set file and its specs, runs nothing.
    .DESCRIPTION
    Cell order is backend-major in the order backends first appear, then the set's own leg order (so a full-screen cell and its windowed twin are neighbours). The cells are INTERLEAVED
    across repeats the way the 2026-10-03 matrix was run: repeat 1 walks the cells forward, repeat 2 walks them back, repeat 3 forward again, so a slow drift of the host (thermal, a
    neighbour's job) lands on both ends of every comparison instead of on one arm. Every spec of a set must name the set's clip and card (fail closed: a set that mixed clips would
    not be one matrix), and the cell ids must be unique.
    Returns [pscustomobject]@{ legSet; card; clipId; repeats; cells (the forward list); plan (the walk, in run order) }.
    #>
    param([Parameter(Mandatory)][string]$LegSetPath, [ValidateRange(1, 20)][int]$Repeats = 1, [string]$Backend = '')
    if (-not (Test-Path -LiteralPath $LegSetPath -PathType Leaf)) { throw "DVE_LEGSET_INVALID no leg set at the given path" }
    $setDir = Split-Path -Parent (Resolve-Path -LiteralPath $LegSetPath).Path
    $dvDir = Split-Path -Parent $setDir
    $set = [IO.File]::ReadAllText($LegSetPath) | ConvertFrom-Json
    if ((Get-DvProp $set 'schema') -cne $script:LegSetSchema) { throw "DVE_LEGSET_INVALID schema is not $script:LegSetSchema" }
    $setName = [string](Get-DvProp $set 'legSet')
    if ($setName -cnotmatch '^[a-z0-9][a-z0-9-]{1,62}$') { throw 'DVE_LEGSET_INVALID legSet is not a leg-set name' }
    $card = [string](Get-DvProp $set 'card'); $clipId = [string](Get-DvProp $set 'clipId')
    $legRefs = Get-DvProp $set 'legs'   # (assigned, not wrapped in @(): Get-DvProp returns an array as ONE object)
    $legRefs = @($legRefs)
    if ($legRefs.Count -eq 0 -or $null -eq $legRefs[0]) { throw 'DVE_LEGSET_INVALID the set lists no legs' }
    $legsDir = [IO.Path]::GetFullPath((Join-Path $dvDir 'legs')).TrimEnd('\') + '\'
    $specs = [System.Collections.Generic.List[object]]::new()
    foreach ($ref in $legRefs) {
        $full = [IO.Path]::GetFullPath((Join-Path $dvDir ([string]$ref)))
        # a leg named by a set is always one of the committed specs under legs/ (production finds the spec a receipt names ONLY there)
        if (-not $full.StartsWith($legsDir, [StringComparison]::OrdinalIgnoreCase) -or -not $full.EndsWith('.json') -or -not (Test-Path -LiteralPath $full -PathType Leaf)) {
            throw "DVE_LEGSET_INVALID '$ref' is not a leg spec under legs/"
        }
        $spec = [IO.File]::ReadAllText($full) | ConvertFrom-Json
        if ([string]$spec.clipId -cne $clipId -or [string]$spec.card -cne $card) { throw "DVE_LEGSET_INVALID leg '$($spec.legId)' does not name the set's clip and card" }
        $specs.Add([pscustomobject]@{ path = $full; spec = $spec })
    }
    $backends = [System.Collections.Generic.List[string]]::new()
    foreach ($s in $specs) { foreach ($b in @($s.spec.backends)) { if ($b -notin $backends) { $backends.Add([string]$b) } } }
    $cells = [System.Collections.Generic.List[object]]::new()
    $seen = @{}
    foreach ($b in $backends) {
        foreach ($s in $specs) {
            if ($b -notin @($s.spec.backends)) { continue }
            $mode = $(if ($null -ne (Get-DvProp $s.spec 'displayMode')) { [string]$s.spec.displayMode } else { 'fullscreen' })
            $cellId = "$b-$mode-s$([int]$s.spec.scaleFactor)"
            if ($seen.ContainsKey($cellId)) { throw "DVE_LEGSET_INVALID two legs of the set make the cell $cellId" }
            $seen[$cellId] = $true
            $cells.Add([pscustomobject]@{ cellId = $cellId; legId = [string]$s.spec.legId; specPath = $s.path; backend = $b; displayMode = $mode; scaleFactor = [int]$s.spec.scaleFactor })
        }
    }
    $cellList = @($cells | Where-Object { [string]::IsNullOrEmpty($Backend) -or $_.backend -ceq $Backend })
    if ($cellList.Count -eq 0) { throw "DVE_LEGSET_INVALID the set has no cell for backend '$Backend'" }
    $plan = [System.Collections.Generic.List[object]]::new()
    $seq = 0
    for ($r = 1; $r -le $Repeats; $r++) {
        $walk = $(if ($r % 2 -eq 1) { $cellList } else { @($cellList[($cellList.Count - 1)..0]) })
        foreach ($c in $walk) {
            $seq++
            $plan.Add([pscustomobject]@{ seq = $seq; repeat = $r; cellId = $c.cellId; legId = $c.legId; specPath = $c.specPath; backend = $c.backend; displayMode = $c.displayMode; scaleFactor = $c.scaleFactor })
        }
    }
    [pscustomobject]@{ legSet = $setName; card = $card; clipId = $clipId; repeats = $Repeats; cells = @($cellList); plan = @($plan) }
}

function Get-DvSmokeSessionLineFields {
    <#
    .SYNOPSIS
    The key=value fields of the LAST `playback_smoke.<Marker> session=<n>` line of the MEASURED session in the app's run log (the session Get-DvSmokeSummaryFields picks),
    or an empty table. Marker is e.g. 'pace_summary' or 'cpu_summary'. A key the line carries more than once is left out (Get-DvSmokeSessionLineRead), so it
    reads as absent: fail closed, never last-key-wins.
    #>
    param([AllowNull()][AllowEmptyString()][string]$LogText, [Parameter(Mandatory)][ValidatePattern('^[a-z_]+$')][string]$Marker)
    (Get-DvSmokeSessionLineRead -LogText $LogText -Marker $Marker).fields
}

function Get-DvSmokeSessionLineRead {
    <#
    .SYNOPSIS
    PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2 (adversary A2 on #726): the LAST `playback_smoke.<Marker> session=<n>` line of the MEASURED session, read as
    key=value tokens separated by single ASCII spaces. Returns @{ found = the line was there; fields = each key seen ONCE; duplicates = the keys seen more than
    once }. A launcher env value that an app before r2 let through with a CR/LF (or a Unicode space a \S+ reader splits on) could add a second worker_threads=
    after the genuine one, and the old reader kept the last; a duplicated key is now dropped from `fields` and named in `duplicates` so a caller can refuse it.
    #>
    param([AllowNull()][AllowEmptyString()][string]$LogText, [Parameter(Mandatory)][ValidatePattern('^[a-z_]+$')][string]$Marker)
    $fields = @{}
    # keys compare case-insensitively, like the @{} table every caller indexes
    $duplicates = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
    $found = $null
    if (-not [string]::IsNullOrEmpty($LogText)) {
        $session = (Get-DvSmokeSummaryFields -LogText $LogText).session
        if ($null -ne $session) {
            $pattern = 'playback_smoke\.' + $Marker + ' session=' + [regex]::Escape([string]$session) + '(\s|$)'
            # r4 (r3 adversary A2 on #726): the marker is matched on each line's record body, so a later line whose build path holds
            # `playback_smoke.<Marker> session=<n>` is never selected
            foreach ($line in (Get-DvLogRecordBodies -LogText $LogText)) { if ($line -match $pattern) { $found = $line } }
        }
    }
    if ($null -ne $found) {
        $seen = @{}
        # r3 (sol ADV-A2 on #726): a key counts whatever its value, EMPTY included, so `worker_threads=8 worker_threads=` (either order, either case) is a
        # duplicate too; $found is already the record body (no Qt location suffix), so a build path never adds a token.
        foreach ($m in [regex]::Matches($found, '(?<![^ ])(?<k>[A-Za-z0-9_]+)=(?<v>[^ ]*)')) {
            $k = $m.Groups['k'].Value
            if ($seen.ContainsKey($k)) { [void]$duplicates.Add($k) }
            else { $seen[$k] = $m.Groups['v'].Value }
        }
        foreach ($k in $seen.Keys) { if (-not $duplicates.Contains($k)) { $fields[$k] = $seen[$k] } }
    }
    @{ found = ($null -ne $found); fields = $fields; duplicates = @($duplicates | Sort-Object) }
}

# r5 (sol r4 B1 on #726): what Get-DvLogRecordBody puts in place of a location it cannot read. It holds no `=` and no record marker, so no field or
# marker is read from it, and a whole-line match (the prep_route record) fails on it: the record is unreadable, never absent and never forged.
$script:DvLocationUnreadable = ' (LOCATION_UNREADABLE)'

# r10 (ADV-A1, Dot 6550442583; r8 adversary on 894ef15c): the ONE parenthesis the app writes inside a record body. PlaybackConformPolicy.h
# playbackFpsStatusText appends ` (<clip> -> <rate>)` (each number QString::number(x, 'g', 5)) to the status text when a clip is conformed, and the
# playback_smoke.summary line prints it quoted: gui_fps_status_text="Playback: <n> fps (59.94 -> 24)", BEFORE the evidence fields. The group is
# recognised only in that exact place -- directly after `gui_fps_status_text="Playback: <n> fps` and closed by `)"` -- so a path or message that merely
# looks like it (after any other text) is still a location. Every length is bounded, so a call costs O(1) and the scan stays linear.
$script:DvConformStatusGroup = [regex]::new('\G \((?:[0-9]+(?:\.[0-9]+)?(?:e[+-][0-9]+)?) -> (?:[0-9]+(?:\.[0-9]+)?(?:e[+-][0-9]+)?)\)"', [System.Text.RegularExpressions.RegexOptions]::CultureInvariant)
$script:DvConformStatusKey = 'gui_fps_status_text="Playback: '
$script:DvConformStatusMeasured = [regex]::new('^[0-9]+(?:\.[0-9])? fps$', [System.Text.RegularExpressions.RegexOptions]::CultureInvariant)

function Test-DvConformStatusGroupAt {
    <#
    .SYNOPSIS
    r10 (ADV-A1): is the ` (` at $Index of $Line the conform group of the app's own status field (see $script:DvConformStatusGroup above), not a Qt location?
    True only when the group's grammar matches there AND the text just before it is `gui_fps_status_text="Playback: <n> fps`, the key at the start of the
    line or after a space. The look-back is bounded (96 characters), so the call is O(1).
    #>
    param([Parameter(Mandatory)][string]$Line, [Parameter(Mandatory)][int]$Index)
    if (-not $script:DvConformStatusGroup.Match($Line, $Index).Success) { return $false }
    $from = [Math]::Max(0, $Index - 96)
    $before = $Line.Substring($from, $Index - $from)
    $k = $before.LastIndexOf($script:DvConformStatusKey, [StringComparison]::Ordinal)
    if ($k -lt 0) { return $false }
    if ($from + $k -gt 0 -and ($k -eq 0 -or $before[$k - 1] -cne [char]' ')) { return $false }
    $script:DvConformStatusMeasured.IsMatch($before.Substring($k + $script:DvConformStatusKey.Length))
}

function Get-DvLogRecordBody {
    <#
    .SYNOPSIS
    PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 (#726): one run-log line without the Qt location suffix CrashForensics::writeLogLine appends after the message
    (` (<file>:<line>)`, only when the message context carries a file; CrashForensics.cpp writeLogLine). r4 (r3 adversary A1 = Grok G1): the suffix starts at
    the FIRST ` (` of the line and runs to a closing `:<digits>)` at its very end; nothing is paired, so the path may hold spaces and parentheses, balanced or
    not (`C:/build (x/`, `C:/build x)/`), and any text at all, record markers included. That is exact because a record body holds ` (` in ONE place only: the
    prefix is `[utc] [LEVEL] [0x<thread>] `, record keys are [A-Za-z0-9_]+, every value from outside the app goes through log_line_field::fieldValue (which
    maps every space), and the one app value with spaces (summary gui_fps_status_text, "Playback: <n> fps") carries, on a conformed run only, the group
    ` (<clip> -> <rate>)` (r10, ADV-A1: see Test-DvConformStatusGroupAt, which recognises that group by its place and grammar and the scan steps over it once).
    The location is written last, so what precedes its first other ` (` is the body the app wrote. A line with no such ` (` is returned unchanged; a line whose tail is not a readable location
    is refused as described under r5 below. Every dual-venue reader of the app's run log strips the
    suffix through this helper (or Get-DvLogRecordBodies) BEFORE it matches a record marker or a field.
    r5 (sol r4 B1 and H1 on #726): a writer before r5 built the location with chained QString::arg calls, so a path holding `%1` took the line number and
    the location ended `:%2)`; that suffix was left in the body, where marker text in the path selected the session, the start line, the visual state and
    the summary fields. The rule now: the body is the text before the first ` (` that is not the status group above, always (the location is written last and
    the body holds no other ` (`, so that text is the app's own and the path's text never reaches a reader). The tail decides whether the record is readable: `:<digits>)` (this
    writer) or `:%2)` (the old writer's only other ending: its path held a lower-numbered marker, and the body before the location is unaffected) with a
    path of at least one character gives the body; any other tail (no line number, text after the `)`, an empty path, a message that held a marker on the
    old writer) gives the body plus ' (LOCATION_UNREADABLE)', which refuses a whole-line match and supplies no field or marker. It may refuse; it never
    forges. The scan is linear: one IndexOf and one right-to-left walk over the digits, no regex (the r4 regex was super-linear on repeated ` (`).
    #>
    param([AllowNull()][AllowEmptyString()][string]$Line)
    if ([string]::IsNullOrEmpty($Line)) { return $Line }
    $open = $Line.IndexOf(' (', [StringComparison]::Ordinal)
    # r10 (ADV-A1, Dot 6550442583): the first ` (` is the location's only when it is not the conform group the app writes INSIDE its own status field
    # (see the r10 note above). That group is skipped once (one gui_fps_status_text per summary line); the next ` (` is the location.
    if ($open -ge 0 -and (Test-DvConformStatusGroupAt -Line $Line -Index $open)) {
        $open = $Line.IndexOf(' (', $open + 2, [StringComparison]::Ordinal)
    }
    if ($open -lt 0) { return $Line }
    $body = $Line.Substring(0, $open)
    $end = $Line.Length - 1
    $colon = -1
    if ($Line[$end] -ceq [char]')') {
        $i = $end - 1
        while ($i -gt $open + 1 -and $Line[$i] -ge [char]'0' -and $Line[$i] -le [char]'9') { $i-- }
        if ($i -lt $end - 1 -and $Line[$i] -ceq [char]':') { $colon = $i }
        elseif ($Line.EndsWith(':%2)', [StringComparison]::Ordinal)) { $colon = $end - 3 }
    }
    # the path is everything between `(` (at $open + 1) and the colon, and must not be empty
    if ($colon -gt $open + 2) { return $body }
    $body + $script:DvLocationUnreadable
}

function Get-DvLogRecordBodies {
    <#
    .SYNOPSIS
    PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r4 (r3 adversary A2 on #726): every line of a run log as its record body (Get-DvLogRecordBody), in order, so a
    reader matches record markers on the body only and a build path that holds `playback_smoke.<marker> session=<n>` never adds or selects a record.
    #>
    param([AllowNull()][AllowEmptyString()][string]$LogText)
    if ([string]::IsNullOrEmpty($LogText)) { return , [string[]]@() }
    $lines = $LogText -split "`r?`n"
    # r5: only a line that holds ` (` can carry a location; the rest are returned as they are without a call
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].IndexOf(' (', [StringComparison]::Ordinal) -ge 0) { $lines[$i] = Get-DvLogRecordBody -Line $lines[$i] } }
    , [string[]]$lines
}

function Get-DvMatrixRates {
    <#
    .SYNOPSIS
    The numbers one display-matrix cell reports, read from the app's own run log (the measured session): presented fps (summary presented_fps), timeline fps after the first present
    (pace_summary timeline_fps_after_first_present; the plain timeline_fps counts the wait for the first frame as playback), render_work ms (summary avg_render_work_ms) and dual-ISO ms
    (cpu_summary avg_llrawproc_dual_iso_ms). An absent or unparsable field is $null (never zero).
    #>
    param([AllowNull()][AllowEmptyString()][string]$LogText)
    $sum = (Get-DvSmokeSummaryFields -LogText $LogText).fields
    $pace = Get-DvSmokeSessionLineFields -LogText $LogText -Marker 'pace_summary'
    $cpu = Get-DvSmokeSessionLineFields -LogText $LogText -Marker 'cpu_summary'
    [ordered]@{
        presentedFps = (ConvertTo-DvDouble $sum['presented_fps'])
        timelineFpsAfterFirstPresent = (ConvertTo-DvDouble $pace['timeline_fps_after_first_present'])
        renderWorkMs = (ConvertTo-DvDouble $sum['avg_render_work_ms'])
        dualIsoMs = (ConvertTo-DvDouble $cpu['avg_llrawproc_dual_iso_ms'])
    }
}

function Get-DvMatrixRow {
    <#
    .SYNOPSIS
    One planned leg's row of the matrix summary, from the receipt the leg wrote. Rates are read only when the outcome is a measurement (PASS or FAIL: every other outcome is no signal,
    its row carries the outcome and no numbers) AND the hashed run log still hashes to receipt.evidence.logSha256 (a swapped log is no measurement: rates stay absent and `note` says so).
    #>
    param([Parameter(Mandatory)]$PlanEntry, $Receipt)
    $row = [ordered]@{
        seq = $PlanEntry.seq; repeat = $PlanEntry.repeat; cellId = $PlanEntry.cellId; backend = $PlanEntry.backend
        requestedDisplay = $PlanEntry.displayMode; observedDisplay = 'UNKNOWN'; requestedScale = $PlanEntry.scaleFactor; effectiveScale = 'UNKNOWN'
        windowSize = $null; previewSize = $null; outcome = 'NO_RECEIPT'; receiptId = $null
        presentedFps = $null; timelineFpsAfterFirstPresent = $null; renderWorkMs = $null; dualIsoMs = $null; note = $null
    }
    if ($null -eq $Receipt) { $row.note = 'the leg wrote no receipt'; return [pscustomobject]$row }
    $row.outcome = [string](Get-DvProp $Receipt 'outcome'); $row.receiptId = [string](Get-DvProp $Receipt 'receiptId')
    $display = Get-DvProp $Receipt 'display'
    if ($null -ne $display) {
        $row.observedDisplay = [string](Get-DvProp $display 'observedMode')
        $w = Get-DvProp $display 'windowWidth'; $h = Get-DvProp $display 'windowHeight'
        if ($null -ne $w -and $null -ne $h) { $row.windowSize = "${w}x${h}" }
        $pw = Get-DvProp $display 'previewWidth'; $ph = Get-DvProp $display 'previewHeight'
        if ($null -ne $pw -and $null -ne $ph) { $row.previewSize = "${pw}x${ph}" }
    }
    $scale = Get-DvProp $Receipt 'scale'
    if ($null -ne $scale) { $row.effectiveScale = Get-DvProp $scale 'effectiveScale' }
    if ($row.outcome -cnotin @('PASS', 'FAIL')) { $row.note = [string](Get-DvProp $Receipt 'outcomeDetail'); return [pscustomobject]$row }
    $evidence = Get-DvProp $Receipt 'evidence'
    $dir = [string](Get-DvProp $evidence 'localEvidenceDir'); $claimed = [string](Get-DvProp $evidence 'logSha256')
    $logPath = $(if ($dir) { Join-Path $dir 'logs\smoke-run.log' } else { '' })
    if (-not $logPath -or -not (Test-Path -LiteralPath $logPath -PathType Leaf)) { $row.note = 'the run log is not in the local evidence'; return [pscustomobject]$row }
    $bytes = [IO.File]::ReadAllBytes($logPath)
    if ((Get-DvSha256OfBytes $bytes) -cne $claimed) { $row.note = 'the run log does not hash to the receipt (not a measurement)'; return [pscustomobject]$row }
    $rates = Get-DvMatrixRates -LogText ([Text.Encoding]::UTF8.GetString($bytes))
    foreach ($k in $rates.Keys) { $row[$k] = $rates[$k] }
    [pscustomobject]$row
}

function Get-DvMedian {
    # The median of the numbers given ($null for none); an even count averages the middle two.
    param($Values)
    $v = @($Values | Where-Object { $null -ne $_ } | ForEach-Object { [double]$_ } | Sort-Object)
    if ($v.Count -eq 0) { return $null }
    if ($v.Count % 2 -eq 1) { return $v[[int](($v.Count - 1) / 2)] }
    ($v[$v.Count / 2 - 1] + $v[$v.Count / 2]) / 2
}

function ConvertTo-DvMatrixTable {
    <#
    .SYNOPSIS
    The ONE summary table of a display-matrix run (markdown): a row per cell, in the set's cell order, aggregated over the repeats: how many repeats were a valid measurement of how many
    ran, presented fps (median, with min-max), timeline fps after the first present, render_work ms, dual-ISO ms (medians), requested -> effective scale (the CUDA clamp stays visible), the
    observed display mode and the observed window size (the app window, with the preview area in it), and the outcomes the repeats ended in. A cell with no measurement shows dashes.
    #>
    param([Parameter(Mandatory)][object[]]$Rows, [Parameter(Mandatory)][object[]]$Cells)
    $f = { param($x, $d) if ($null -eq $x) { '-' } else { ([double]$x).ToString("F$d", [Globalization.CultureInfo]::InvariantCulture) } }
    $lines = [System.Collections.Generic.List[string]]::new()
    $lines.Add('| cell | backend | display (requested / observed) | scale (requested -> effective) | window (preview) | measured / ran | presented fps median (min-max) | timeline fps after first present | render_work ms | dual-ISO ms | outcomes |')
    $lines.Add('|---|---|---|---|---|---|---|---|---|---|---|')
    foreach ($c in $Cells) {
        $mine = @($Rows | Where-Object { $_.cellId -ceq $c.cellId })
        $meas = @($mine | Where-Object { $_.outcome -cin @('PASS', 'FAIL') -and $null -ne $_.presentedFps })
        $pf = @($meas | ForEach-Object { $_.presentedFps })
        $range = $(if ($pf.Count -gt 0) { '(' + (& $f ($pf | Measure-Object -Minimum).Minimum 2) + '-' + (& $f ($pf | Measure-Object -Maximum).Maximum 2) + ')' } else { '' })
        $presented = ((& $f (Get-DvMedian $pf) 2) + ' ' + $range).Trim()
        $observed = @($mine | ForEach-Object { $_.observedDisplay } | Sort-Object -Unique) -join ','
        # The words a person reads (SCALE-LABEL-CLARITY-1 r2), from the same mapping as the plan line and outcomeDetail; the row's numeric requested/effective fields are unchanged.
        $eff = @($mine | ForEach-Object { "$(Get-DvScaleLabel -Factor $c.scaleFactor)->$(Get-DvScaleLabel -Factor $_.effectiveScale)" } | Sort-Object -Unique) -join ','
        $win = @($mine | Where-Object { $_.windowSize } | ForEach-Object { if ($_.previewSize) { "$($_.windowSize) ($($_.previewSize))" } else { $_.windowSize } } | Sort-Object -Unique) -join ', '
        $outcomes = @($mine | ForEach-Object { $_.outcome } | Group-Object | Sort-Object Name | ForEach-Object { "$($_.Name) x$($_.Count)" }) -join ', '
        $lines.Add("| $($c.cellId) | $($c.backend) | $($c.displayMode) / $(if ($observed) { $observed } else { '-' }) | $eff | $(if ($win) { $win } else { '-' }) | $($meas.Count)/$($mine.Count) | $presented | $(& $f (Get-DvMedian @($meas | ForEach-Object { $_.timelineFpsAfterFirstPresent })) 2) | $(& $f (Get-DvMedian @($meas | ForEach-Object { $_.renderWorkMs })) 1) | $(& $f (Get-DvMedian @($meas | ForEach-Object { $_.dualIsoMs })) 1) | $(if ($outcomes) { $outcomes } else { '-' }) |")
    }
    $lines -join "`n"
}

# --- the evidence a receipt is RE-DERIVED from (DUAL-VENUE-EVIDENCE-2 round 1) -----------------------------------------
# CLASS: a receipt is ADVISORY (production) only when every claim in it is re-derived from a COMMITTED blob or a HASHED artifact; no field the
# receipt asserts about itself is ever an input. The receipt names its evidence directory and the sha256 of each file in it; the
# validator re-hashes the files and re-parses them with the SAME parser the writer used, so a hand-built receipt, a placeholder
# hash, a self-asserted boolean, an empty consent blob or a missing artifact cannot become a PASS/FAIL.
# claim field in receipt.evidence -> file under the local evidence directory
$script:EvidenceFileMap = [ordered]@{
    summaryJsonSha256 = 'summary.json'
    evidenceManifestSha256 = 'evidence-manifest.json'
    resultJsonSha256 = 'result.json'
    logSha256 = 'logs\smoke-run.log'
    umRunJsonSha256 = 'um-run.json'
    contactFramesJsonSha256 = 'contact-frames.json'
}
$script:UmRunSchema = 'mlv-app/dual-venue-um-run/v1'
# The receipt's playback block fields that must equal what is re-derived from the files (a stored valid / invalidReasons is not one).
$script:PlaybackComparedFields = @('sourceAdvanced', 'requiredSourceFrames', 'nativeFps', 'paceFps', 'fpsOverride', 'wrapped', 'wrapCount',
    'expectedRunNonce', 'observedRunNonce', 'manifestRunNonce', 'logSha256', 'logShaBound', 'settingsIsolated', 'fixtureRehearsal', 'clipId')

function Test-DvNumber($Value) {
    ($Value -is [int] -or $Value -is [long] -or $Value -is [double] -or $Value -is [decimal] -or $Value -is [single] -or $Value -is [int16] -or $Value -is [byte] -or $Value -is [uint32] -or $Value -is [uint64])
}

function Get-DvKeys($Object) {
    if ($Object -is [System.Collections.IDictionary]) { return @($Object.Keys | ForEach-Object { [string]$_ }) }
    @($Object.PSObject.Properties | ForEach-Object { $_.Name })
}

function Test-DvJsonEquivalent {
    # Structural equality of two parsed-JSON values: numbers compare as doubles (a JSON round trip may turn 900.0 into 900), strings
    # case-sensitively, objects by key set and member, arrays by order. $null equals only $null.
    param($A, $B)
    if ($null -eq $A -or $null -eq $B) { return ($null -eq $A -and $null -eq $B) }
    if (Test-DvNumber $A) { return ((Test-DvNumber $B) -and ([double]$A -eq [double]$B)) }
    if ($A -is [bool]) { return (($B -is [bool]) -and $A -eq $B) }
    if ($A -is [string]) { return (($B -is [string]) -and $A -ceq $B) }
    $aMap = ($A -is [System.Collections.IDictionary]) -or ($A -is [System.Management.Automation.PSCustomObject])
    $bMap = ($B -is [System.Collections.IDictionary]) -or ($B -is [System.Management.Automation.PSCustomObject])
    if ($aMap -or $bMap) {
        if (-not ($aMap -and $bMap)) { return $false }
        $ak = @(Get-DvKeys $A | Sort-Object -CaseSensitive); $bk = @(Get-DvKeys $B | Sort-Object -CaseSensitive)
        if ($ak.Count -ne $bk.Count) { return $false }
        for ($i = 0; $i -lt $ak.Count; $i++) { if ($ak[$i] -cne $bk[$i]) { return $false } }
        foreach ($k in $ak) { if (-not (Test-DvJsonEquivalent (Get-DvProp $A $k) (Get-DvProp $B $k))) { return $false } }
        return $true
    }
    if ($A -is [System.Collections.IEnumerable]) {
        if ($B -isnot [System.Collections.IEnumerable] -or $B -is [string]) { return $false }
        $al = @($A); $bl = @($B)
        if ($al.Count -ne $bl.Count) { return $false }
        for ($i = 0; $i -lt $al.Count; $i++) { if (-not (Test-DvJsonEquivalent $al[$i] $bl[$i])) { return $false } }
        return $true
    }
    ([string]$A) -ceq ([string]$B)
}

function Read-DvEvidenceSet {
    <#
    .SYNOPSIS
    Re-read the hashed evidence a receipt names. $EvidenceDir (the caller's) wins over the receipt's own evidence.localEvidenceDir; either
    way the directory is only WHERE to look: every file's sha256 must equal the one the receipt claims, so a swapped file is caught
    and a directory without the files is INCOMPLETE. Returns [pscustomobject]@{ complete; summary; manifest; result; umRun; logText; logSha;
    dir; incomplete; invalid } where `complete` means all five files were present, matched their claimed hash and parsed.
    #>
    param([Parameter(Mandatory)]$Receipt, [string]$EvidenceDir = '')
    $incomplete = [System.Collections.Generic.List[string]]::new()
    $invalid = [System.Collections.Generic.List[string]]::new()
    $evidence = Get-DvProp $Receipt 'evidence'
    $dir = $EvidenceDir
    if ([string]::IsNullOrWhiteSpace($dir)) { $dir = [string](Get-DvProp $evidence 'localEvidenceDir') }
    $res = [ordered]@{ complete = $false; summary = $null; manifest = $null; result = $null; umRun = $null; logText = $null; logSha = $null; dir = $dir; incomplete = @(); invalid = @() }
    if ([string]::IsNullOrWhiteSpace($dir)) {
        $incomplete.Add('EVIDENCE_ABSENT: the receipt names no local evidence directory (evidence.localEvidenceDir) and none was given; nothing can be re-derived')
    } elseif (-not (Test-Path -LiteralPath $dir -PathType Container)) {
        $incomplete.Add('EVIDENCE_ABSENT: the local evidence directory the receipt names is not present')
    } else {
        $bytes = @{}
        foreach ($claimKey in $script:EvidenceFileMap.Keys) {
            $rel = $script:EvidenceFileMap[$claimKey]
            $claim = [string](Get-DvProp $evidence $claimKey)
            # The job writes no evidence manifest on a product-failure terminal (GPU_RECON_FRAMES_ZERO, ...): the manifest is optional here and
            # REQUIRED by Test-DvReceiptValid whenever the job's result is a capture. A claim that IS made must still match its file.
            # (contact-frames.json exists only on a LOOK leg that captured raw frames; Read-DvContactFrames requires it where it matters.)
            if ($claimKey -cin @('evidenceManifestSha256', 'contactFramesJsonSha256') -and [string]::IsNullOrEmpty($claim)) { continue }
            if ($claim -cnotmatch '^[0-9a-f]{64}$') { $incomplete.Add("EVIDENCE_ABSENT: evidence.$claimKey is absent or not 64 lowercase hex"); continue }
            $file = Join-Path $dir $rel
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { $incomplete.Add("EVIDENCE_ABSENT: $($rel -replace '\\', '/') is not in the evidence directory"); continue }
            $b = [IO.File]::ReadAllBytes($file)
            if ((Get-DvSha256OfBytes $b) -cne $claim) { $invalid.Add("EVIDENCE_HASH_MISMATCH: $($rel -replace '\\', '/') does not hash to the sha256 the receipt claims"); continue }
            $bytes[$claimKey] = $b
        }
        if ($incomplete.Count -eq 0 -and $invalid.Count -eq 0) {
            $parsed = @{}
            foreach ($pair in @(@('summaryJsonSha256', 'summary'), @('evidenceManifestSha256', 'manifest'), @('resultJsonSha256', 'result'), @('umRunJsonSha256', 'umRun'), @('contactFramesJsonSha256', 'contactFrames'))) {
                if (-not $bytes.ContainsKey($pair[0])) { continue }
                try { $parsed[$pair[1]] = (ConvertTo-DvText $bytes[$pair[0]]) | ConvertFrom-Json -ErrorAction Stop }
                catch { $invalid.Add("EVIDENCE_UNPARSABLE: $($script:EvidenceFileMap[$pair[0]]) is not valid JSON") }
            }
            if ($invalid.Count -eq 0) {
                $res.summary = $parsed['summary']; $res.result = $parsed['result']; $res.umRun = $parsed['umRun']
                $res.manifest = $(if ($parsed.ContainsKey('manifest')) { $parsed['manifest'] } else { $null })
                $res.logText = [Text.Encoding]::UTF8.GetString($bytes['logSha256'])
                $res.logSha = (Get-DvSha256OfBytes $bytes['logSha256'])
                $res.complete = $true
            }
        }
    }
    $res.incomplete = $incomplete.ToArray(); $res.invalid = $invalid.ToArray()
    [pscustomobject]$res
}

# The product-failure terminals the job writes AFTER its run log is published, as playback-attr-3-cuda-job.ps1 writes them (DUAL-VENUE-EVIDENCE-3,
# sol r2 H1 / fable r2 hardening 1). Their summary.json is NOT the success summary: the frame counters live only inside the nested `gpuSummary`
# (Get-LastGpuSummary's keys; there is no top-level gpuFramesTotal / cpuFrames), and the variant's backend / lookLeg fields (written into the SUCCESS
# summary only) are absent. GPU_RECON_FRAMES_ZERO and CPU_FALLBACK_DETECTED (exits 13 / 14) are guarded `$Backend -ne 'cpu'` in the cpu variant, so
# they can only come from a cuda leg; CPU_BACKEND_PATH_MISMATCH (exit 28) fires only when `$Backend -eq 'cpu'`.
# PRESENTMON_UNAVAILABLE (exit 23) has three shapes: the wait-failure branch carries gpuFramesTotal at top level and gpuSummary beside it (counters that are null
# when the run log has no summary line), the display-report branch carries gpuFramesTotal, and the spawn-failure branch carries neither. Its backend reads from the
# plain counters; it is listed here only because its summary, too, states no leg type.
$script:FailureTerminals = @('GPU_RECON_FRAMES_ZERO', 'CPU_FALLBACK_DETECTED', 'CPU_BACKEND_PATH_MISMATCH', 'PRESENTMON_UNAVAILABLE')

# The frame counters of a hashed summary.json, from where the job put them: the top-level gpuFramesTotal / cpuFrames when the key is there, else the
# nested gpuSummary (gpuFramesTotal is the sum the job computes: recon readback + texture readback + texture no-readback; gpu_preview frames are not
# CUDA reconstruction and never count). A top-level key that is present but not an integer is NOT rescued by the nested block. $null = not derivable.
function Get-DvFrameCounters {
    param($Summary)
    $topGpu = Get-DvProp $Summary 'gpuFramesTotal'; $topCpu = Get-DvProp $Summary 'cpuFrames'
    $nested = Get-DvProp $Summary 'gpuSummary'
    $gpu = ConvertTo-DvInt64 $topGpu; $cpu = ConvertTo-DvInt64 $topCpu
    if ($null -eq $topGpu -and $null -ne $nested) {
        $parts = @(foreach ($k in 'gpuReconReadbackFrames', 'gpuTextureReadbackFrames', 'gpuTextureNoReadbackFrames') { ConvertTo-DvInt64 (Get-DvProp $nested $k) })
        if ($parts.Count -eq 3 -and @($parts | Where-Object { $null -eq $_ }).Count -eq 0) { $gpu = [int64]($parts[0] + $parts[1] + $parts[2]) }
    }
    if ($null -eq $topCpu -and $null -ne $nested) { $cpu = ConvertTo-DvInt64 (Get-DvProp $nested 'cpuFrames') }
    [pscustomobject]@{ gpu = $gpu; cpu = $cpu }
}

# The backend a run ACTUALLY used, from the hashed summary.json's own frame counters (fable r1 B1 / sol r1 B2). The receipt's subject.backend is
# a claim; this is the evidence. cuda: gpuFramesTotal > 0. cpu: cpuFrames > 0 and gpuFramesTotal == 0. Anything else (absent, both zero) is not derivable.
# A product-failure terminal (see $script:FailureTerminals) is read by what that terminal means: the leg's backend is the one only that terminal can come
# from, and its counters must be the shape the job writes it under (a terminal whose counters contradict it is not derivable). That is how an all-cpu
# GPU_RECON_FRAMES_ZERO run (gpu 0, cpu > 0) stays a cuda leg's FAIL instead of reading as a cpu run.
function Get-DvDerivedBackend {
    param($Summary)
    $c = Get-DvFrameCounters -Summary $Summary
    $gpu = $c.gpu; $cpu = $c.cpu
    $terminal = [string](Get-DvProp $Summary 'result')
    if ($terminal -ceq 'GPU_RECON_FRAMES_ZERO') { if ($null -ne $gpu -and $gpu -eq 0) { return 'cuda' }; return $null }
    if ($terminal -ceq 'CPU_FALLBACK_DETECTED') { if ($null -ne $gpu -and $gpu -gt 0 -and $null -ne $cpu -and $cpu -gt 0) { return 'cuda' }; return $null }
    if ($terminal -ceq 'CPU_BACKEND_PATH_MISMATCH') { if ($null -ne $gpu -and $null -ne $cpu -and ($cpu -le 0 -or $gpu -gt 0)) { return 'cpu' }; return $null }
    if ($null -ne $gpu -and $gpu -gt 0) { return 'cuda' }
    if ($null -ne $gpu -and $gpu -eq 0 -and $null -ne $cpu -and $cpu -gt 0) { return 'cpu' }
    $null
}

$script:BackendNotDerivableMessage = 'BACKEND_NOT_DERIVABLE: the hashed summary.json''s frame counters (gpuFramesTotal / cpuFrames, or the nested gpuSummary of a product-failure terminal) do not say which backend ran (cuda: gpu frames > 0; cpu: cpu frames > 0 and gpu frames 0; a failure terminal: the counters its own result token implies)'
$script:CpuBackendFieldMissingMessage = 'BACKEND_NOT_DERIVABLE: a cpu run''s hashed summary.json carries no backend field'

# DVE-LEG-TERMINALS-1 item 1: why a PASS/FAIL receipt for this summary cannot name its backend, or $null when it can. Test-DvReceiptValid refuses such a receipt (and
# Write-DvReceipt then refuses the write: no receipt at all, DVE_RECEIPT_WRITE_FAILED -- a leg that had played, on Ultra-Magnus, 2026-10-02). The runner asks the same
# question BEFORE it chooses an outcome and ends such a leg as a typed no-signal (INVALID) receipt that keeps its evidence. -RequireCpuBackendField is the production
# rule (a cpu run's backend is read from the summary's own field); the runner's offline test mode does not enforce it.
function Get-DvBackendNotDerivable {
    param($Summary, [string]$Backend, [bool]$RequireCpuBackendField = $false)
    if ($null -eq (Get-DvDerivedBackend -Summary $Summary)) { return $script:BackendNotDerivableMessage }
    if ($RequireCpuBackendField -and $Backend -ceq 'cpu' -and $null -eq (Get-DvProp $Summary 'backend')) { return $script:CpuBackendFieldMissingMessage }
    $null
}

$script:ContactFramesSchema = 'mlv-app/dual-venue-contact-frames/v1'
$script:ContactFrameNamePattern = '^[A-Za-z0-9][A-Za-z0-9._-]*\.(png|json)$'

function Read-DvContactFrames {
    <#
    .SYNOPSIS
    The raw contact-sheet frames of a LOOK receipt, accepted ONLY as the hashed manifest lists them (sol r1 B4). The receipt names
    contact-frames.json by sha256 (evidence.contactFramesJsonSha256); the manifest lists every PNG frame and JSON sidecar with its own sha256
    (the runner writes it at capture time, in the same step that copies the frames). A file in <evidence>\contact-sheet\raw that the manifest
    does not list, a listed file that is absent or does not hash to its entry, a name that is not a plain *.png / *.json file name, a
    directory or a reparse point: all refused. Returns [pscustomobject]@{ ok; reasons; rawDir; files = @(@{ name; sha256; bytes }) } where
    `bytes` are the bytes that were hashed, so a caller stages exactly what was verified.
    #>
    param([Parameter(Mandatory)]$Receipt, [Parameter(Mandatory)][string]$EvidenceDir)
    $reasons = [System.Collections.Generic.List[string]]::new()
    $files = [System.Collections.Generic.List[object]]::new()
    $rawDir = Join-Path $EvidenceDir 'contact-sheet\raw'
    $done = { [pscustomobject]@{ ok = ($reasons.Count -eq 0); reasons = $reasons.ToArray(); rawDir = $rawDir; files = $files.ToArray() } }
    $claim = [string](Get-DvProp (Get-DvProp $Receipt 'evidence') 'contactFramesJsonSha256')
    if ($claim -cnotmatch '^[0-9a-f]{64}$') { $reasons.Add('CONTACT_FRAMES_UNLISTED: the receipt names no contact-frames manifest (evidence.contactFramesJsonSha256), so no raw frame is a listed frame'); return (& $done) }
    $manifestFile = Join-Path $EvidenceDir 'contact-frames.json'
    if (-not (Test-Path -LiteralPath $manifestFile -PathType Leaf)) { $reasons.Add('CONTACT_FRAMES_UNLISTED: contact-frames.json is not in the evidence directory'); return (& $done) }
    $mbytes = [IO.File]::ReadAllBytes($manifestFile)
    if ((Get-DvSha256OfBytes $mbytes) -cne $claim) { $reasons.Add('CONTACT_FRAMES_UNLISTED: contact-frames.json does not hash to the sha256 the receipt claims'); return (& $done) }
    try { $doc = (ConvertTo-DvText $mbytes) | ConvertFrom-Json -ErrorAction Stop } catch { $reasons.Add('CONTACT_FRAMES_UNLISTED: contact-frames.json is not valid JSON'); return (& $done) }
    if ([string](Get-DvProp $doc 'schema') -cne $script:ContactFramesSchema) { $reasons.Add("CONTACT_FRAMES_UNLISTED: contact-frames.json schema is not $($script:ContactFramesSchema)"); return (& $done) }
    $listed = [ordered]@{}
    $entries = Get-DvProp $doc 'files'   # (assigned, then @()-wrapped: Get-DvProp's `return ,` would otherwise nest an array inside @())
    foreach ($e in @($entries)) {
        $name = [string](Get-DvProp $e 'name'); $sha = [string](Get-DvProp $e 'sha256')
        if ($name -cnotmatch $script:ContactFrameNamePattern -or $sha -cnotmatch '^[0-9a-f]{64}$') { $reasons.Add('CONTACT_FRAMES_UNLISTED: a manifest entry is not a plain *.png / *.json name with a 64-hex sha256'); continue }
        if ($listed.Contains($name.ToLowerInvariant())) { $reasons.Add('CONTACT_FRAMES_UNLISTED: the manifest lists one file name twice'); continue }
        $listed[$name.ToLowerInvariant()] = [pscustomobject]@{ name = $name; sha256 = $sha }
    }
    if ($listed.Count -eq 0) { $reasons.Add('CONTACT_FRAMES_UNLISTED: the manifest lists no frame'); return (& $done) }
    if (-not (Test-Path -LiteralPath $rawDir -PathType Container)) { $reasons.Add('CONTACT_FRAMES_UNLISTED: the evidence directory has no contact-sheet\raw directory'); return (& $done) }
    foreach ($actual in @(Get-ChildItem -LiteralPath $rawDir -Force)) {
        $isReparse = (($actual.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
        if ($actual.PSIsContainer -or $isReparse -or -not $listed.Contains($actual.Name.ToLowerInvariant()) -or $listed[$actual.Name.ToLowerInvariant()].name -cne $actual.Name) {
            $reasons.Add("CONTACT_FRAME_UNLISTED: contact-sheet\raw holds '$($actual.Name)', which the hashed manifest does not list as a plain file")
        }
    }
    if ($reasons.Count -gt 0) { return (& $done) }
    foreach ($entry in $listed.Values) {
        $path = Join-Path $rawDir $entry.name
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { $reasons.Add("CONTACT_FRAME_UNLISTED: the manifest lists '$($entry.name)' but the raw directory does not hold it"); continue }
        $bytes = [IO.File]::ReadAllBytes($path)
        if ((Get-DvSha256OfBytes $bytes) -cne $entry.sha256) { $reasons.Add("CONTACT_FRAME_HASH_MISMATCH: '$($entry.name)' does not hash to the sha256 the manifest lists"); continue }
        $files.Add([pscustomobject]@{ name = $entry.name; sha256 = $entry.sha256; bytes = $bytes })
    }
    # A hash-listed sidecar names its frame's image (`path`); the composer used to follow that name wherever it pointed -- an absolute or parent-relative path
    # reached a PNG OUTSIDE the listed files, so an edited or stale external image was composed under the receipt's labels with no listed hash changing
    # (DUAL-VENUE-EVIDENCE-3, sol r2 blocker, PR #223). A sidecar may name no image but its own listed `<stem>.png` (the app writes exactly that name).
    foreach ($f in @($files | Where-Object { $_.name -clike '*.json' })) {
        $stem = $f.name.Substring(0, $f.name.Length - '.json'.Length)
        try { $side = (ConvertTo-DvText $f.bytes) | ConvertFrom-Json -ErrorAction Stop } catch { $reasons.Add("CONTACT_FRAME_SIDECAR_UNPARSABLE: the listed sidecar '$($f.name)' is not valid JSON"); continue }
        $refPath = [string](Get-DvProp $side 'path')
        if ($refPath -ne '') {
            if ($refPath -cne ($stem + '.png')) { $reasons.Add("CONTACT_FRAME_SIDECAR_PATH: the listed sidecar '$($f.name)' names an image other than its own '$stem.png' (an absolute, parent-relative, nested or other-frame path); a sheet shows only the frames the manifest lists") }
        }
    }
    if ($reasons.Count -eq 0 -and @($files | Where-Object { $_.name -clike '*.png' }).Count -eq 0) { $reasons.Add('CONTACT_FRAMES_UNLISTED: the manifest lists no PNG frame') }
    & $done
}

# --- CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 r6: the leg-spec SHAPE refusals, ONE predicate for the leg and the lease batch's preflight --------------
# (adversary r5 A1) Invoke-VenueLeg refuses these before its terminal try; the batch preflight (Get-LegSpecRefusal) calls this same function, so a spec the
# leg would refuse is refused before a lease opens, never after it (LEG_FAILED, no receipt). A control container the runner reads members of must be ONE
# JSON object when present (r5, sol r4 B1: an array reads through member enumeration but has no PSObject property, so its controls were dropped), and
# look.pairedSeekFrames must be distinct non-negative integers, one per contact-sheet frame, on a look.pairedSeek leg (r14: and, as the generator's
# -ContactSheetSeekFrames they are now forwarded as, strictly ascending and at most 999999999). Returns the refusal text, or $null.
$script:LegTypes = @('speed', 'look')   # leg-spec.schema.json properties.legType.enum
# the generatorArgs members leg-spec.schema.json types boolean (prepConflatedMove: const true); a test pins this list to the schema
$script:GeneratorArgsBooleanSwitches = @('forceLookAssist', 'corePartitionTelemetry', 'disableLookAssist', 'presentTrace', 'firstPresentCapture', 'warmReuseMissDump', 'unpaced', 'disablePrepPointwiseFast', 'prepConflatedMove', 'utilSample', 'disableReconReleaseDeferred', 'dueWakeDeliveryPost')
function Get-DvLegSpecShapeRefusal {
    param($Spec)
    if ($Spec -isnot [System.Management.Automation.PSCustomObject]) { return 'the leg spec must be a single JSON object' }
    foreach ($container in 'look', 'generatorArgs', 'acceptedEffectiveScale', 'timeouts', 'criteria') {
        $present = $Spec.PSObject.Properties[$container]
        if ($null -ne $present -and $present.Value -isnot [System.Management.Automation.PSCustomObject]) {
            $shape = $(if ($null -eq $present.Value) { 'null' } elseif ($present.Value -is [array]) { 'an array' } else { "a $($present.Value.GetType().Name)" })
            return "$container must be a single JSON object (it is $shape)"
        }
    }
    # DUAL-VENUE-RUNNER-BOOL-ARGS-STRICT-1: every generatorArgs switch is a JSON boolean (leg-spec.schema.json types each one boolean), refused here otherwise.
    # Invoke-VenueLeg reads most of them with a plain [bool] cast, which is true for ANY non-empty string: "disableLookAssist": "false" ran a Look-Assist-OFF leg,
    # the failure of the owner's 2026-10-20 objection (hub rule D4b). One check here covers the leg and the lease batch's preflight, so no cast is patched
    # one by one. A present-but-null, number, array or object value is refused too (a cast read null and 0 as false and 1 as true). Absent stays "not asked for".
    $genArgsProp = $Spec.PSObject.Properties['generatorArgs']
    if ($null -ne $genArgsProp) {
        foreach ($switchName in $script:GeneratorArgsBooleanSwitches) {
            $switchProp = $genArgsProp.Value.PSObject.Properties[$switchName]
            if ($null -ne $switchProp -and $switchProp.Value -isnot [bool]) {
                return "generatorArgs.$switchName must be JSON true or false (it is $(ConvertTo-Json -InputObject $switchProp.Value -Compress -Depth 3))"
            }
        }
    }
    # r9 (adversary r8 cc8061a2 A1, Dot pr-721, hub ACK 6564543250): legType is ONE string of the schema's enum, compared case-sensitively as the validator
    # compares it. [string] of the array ["look","speed"] is 'look speed', so that spec with no look passed this predicate, the batch opened its lease, and the
    # leg's -eq against the array was truthy and indexed the absent look before any receipt. An array, object, number, null, empty or other-case value is
    # refused here, for the leg and every batch preflight alike. An ABSENT legType stays a speed leg, as the runner has always read it.
    $legTypeProp = $Spec.PSObject.Properties['legType']
    if ($null -ne $legTypeProp -and -not ($legTypeProp.Value -is [string] -and $legTypeProp.Value -cin $script:LegTypes)) {
        return "legType must be one of $(($script:LegTypes | ForEach-Object { '"' + $_ + '"' }) -join ', ') (one JSON string; it is $(ConvertTo-Json -InputObject $legTypeProp.Value -Compress -Depth 3))"
    }
    $isLookLeg = ($null -ne $legTypeProp -and $legTypeProp.Value -ceq 'look')
    # r7 (adversary r6 A2): a look leg reads look.* before its receipt exists, so a look leg with NO look is refused here too (it threw
    # "Cannot index into a null array" after the batch had opened its lease).
    if ($isLookLeg -and $null -eq $Spec.PSObject.Properties['look']) { return 'a look leg needs look (one JSON object); it is absent' }
    $look = Get-DvProp $Spec 'look'
    # r9 fix 1 (adversary r9 a0936637 A2): a look leg's look.contactSheetFrames is a whole number from 1 to 60 -- the schema's range and the generator's
    # ValidateRange(1,60) -- refused here, before a lease opens. 61 (with a 61-frame list) and 0 (with []) passed this predicate and the generator refused
    # them after the lease; 1.4 was generated as [int] 1 and then refused as no whole number after collection; 6.0 likewise. (A JSON number with a
    # fraction parses to a double, never an int or long.)
    if ($isLookLeg -and $null -ne $look) {
        $countValue = Get-DvProp $look 'contactSheetFrames'
        if (-not (($countValue -is [int] -or $countValue -is [long]) -and $countValue -ge 1 -and $countValue -le 60)) {
            return "look.contactSheetFrames must be a whole number from 1 to 60 (it is $(if ($null -eq $look.PSObject.Properties['contactSheetFrames']) { 'absent' } else { ConvertTo-Json -InputObject $countValue -Compress -Depth 3 }))"
        }
    }
    if (-not $isLookLeg -or $null -eq $look -or $null -eq $look.PSObject.Properties['pairedSeekFrames']) { return $null }
    $listed = $look.pairedSeekFrames
    $items = @($listed)
    $bad = $(if ($listed -isnot [array]) { 'is not an array' }
        elseif (-not ($look.PSObject.Properties['pairedSeek'] -and $look.pairedSeek -is [bool] -and $look.pairedSeek)) { 'needs look.pairedSeek true' }
        elseif (@($items | Where-Object { -not ($_ -is [int] -or $_ -is [long]) -or $_ -lt 0 }).Count -gt 0) { 'must hold non-negative integers' }
        elseif (@($items | Sort-Object -Unique).Count -ne $items.Count) { 'must not repeat a frame' }
        elseif ($items.Count -ne [int]$look.contactSheetFrames) { "names $($items.Count) frame(s) but look.contactSheetFrames is $($look.contactSheetFrames)" }
        # r14: Invoke-VenueLeg hands the list to the generator's -ContactSheetSeekFrames, whose contract (and the app's) is strictly ascending, each
        # 0..999999999 (CONTACT_SHEET_SEEK_FRAMES_INVALID, untyped to the leg and after the batch's lease). A list it would refuse is refused here instead.
        elseif (@($items | Where-Object { $_ -gt 999999999 }).Count -gt 0) { 'must hold frames 0..999999999 (the generator''s -ContactSheetSeekFrames range)' }
        elseif (@(for ($i = 1; $i -lt $items.Count; $i++) { if ($items[$i] -le $items[$i - 1]) { $i } }).Count -gt 0) { 'must be strictly ascending (the generator''s -ContactSheetSeekFrames order)' }
        else { $null })
    if ($null -ne $bad) { return "look.pairedSeekFrames $bad" }
    $null
}

# --- CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 r10: is a paired-comparison image a PNG at all? ONE check for both members of every pair ------------
# (sol r9 key B1, r9 adversary successor ADV-A1 DualVenueRunner.psm1:2048) A paused still or an in-pass partner whose file hash-matches its listing
# was honoured whatever its bytes were: a zero-byte frame-00.png (sha256 e3b0c442...b855), a truncated file, a non-PNG renamed .png. A hash binds the
# bytes to the listing; it says nothing about whether they are an image. This checks the PNG container and its image data with the .NET base library
# only (no image-decoder dependency): the 8-byte signature; a first chunk IHDR of length 13 with width and height 1..2^31-1, a valid bit depth / colour
# type pair, compression 0, filter 0 and interlace 0 or 1; every chunk length inside the file, every chunk type four ASCII letters and every chunk's
# CRC-32 (IDAT included since r12); a PLTE before the image data of a palette image; at least one IDAT; an IEND that ends the chunk sequence (a file cut short has none);
# and -- r10 fix 1, adversary r10 77fc99fa A1: a well-formed container whose IDAT inflated to 0 bytes passed -- the concatenated IDAT data must be ONE
# valid zlib stream (its Adler-32 checked by the inflater) that inflates to exactly the scanline bytes IHDR implies (Adam7 passes included), each
# scanline led by a filter type 0..4. Returns $null for a decodable PNG, otherwise the reason. Get-DvPairedSeekVerdict calls it for every paused still
# and every in-pass partner image, so collection (Invoke-VenueLeg) and final validation (Test-DvReceiptValid) refuse the same files.
$script:PngSignature = [byte[]](0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A)
$script:PngMinBytes = 8 + 25 + 12 + 12   # signature + IHDR chunk (12 + 13) + an IDAT chunk header + IEND chunk
$script:PngBitDepths = @{ 0 = @(1, 2, 4, 8, 16); 2 = @(8, 16); 3 = @(1, 2, 4, 8); 4 = @(8, 16); 6 = @(8, 16) }   # colour type -> allowed bit depths
$script:PngChannels = @{ 0 = 1; 2 = 3; 3 = 1; 4 = 2; 6 = 4 }                                                     # colour type -> samples per pixel
$script:PngAdam7 = @(@(0, 0, 8, 8), @(4, 0, 8, 8), @(0, 4, 4, 8), @(2, 0, 4, 4), @(0, 2, 2, 4), @(1, 0, 2, 2), @(0, 1, 1, 2))   # xStart, yStart, xStep, yStep
# r12 (adversary r12 e68b3f74 A1): EVERY chunk's CRC-32 is checked, IDAT included (an IDAT with a wrong CRC passed: only the zlib Adler-32 covered its
# bytes, and Adler-32 does not cover the chunk's own CRC field). A PowerShell byte loop runs about 2 s per MB, too slow for a full-size still's IDAT, so
# the CRC-32 (ISO 3309, the PNG polynomial) is a small C# type compiled once per process on first use (C# 5 syntax, so Windows PowerShell 5.1 builds it too).
$script:PngCrc32TypeName = 'DvPngCrc32R12'
$script:PngCrc32Source = @'
public static class DvPngCrc32R12 {
    private static readonly uint[] Table = MakeTable();
    private static uint[] MakeTable() {
        uint[] t = new uint[256];
        for (uint k = 0; k < 256; k++) { uint c = k; for (int j = 0; j < 8; j++) { c = ((c & 1u) != 0u) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1); } t[k] = c; }
        return t;
    }
    public static uint Compute(byte[] bytes, int offset, int count) {
        uint c = 0xFFFFFFFFu;
        int end = offset + count;
        for (int i = offset; i < end; i++) { c = Table[(c ^ bytes[i]) & 0xFFu] ^ (c >> 8); }
        return c ^ 0xFFFFFFFFu;
    }
}
'@
function Get-DvPngCrc32 {
    param([byte[]]$Bytes, [int]$Offset, [int]$Count)
    $t = $script:PngCrc32TypeName -as [type]
    if ($null -eq $t) { Add-Type -TypeDefinition $script:PngCrc32Source -Language CSharp -ErrorAction Stop; $t = $script:PngCrc32TypeName -as [type] }
    [long]$t::Compute($Bytes, $Offset, $Count)
}
# r13 (sol r12 key B1, r12 adversary successor ADV-A1): the chunk ALLOWLIST. Through r12 a chunk this check did not know was SKIPPED, so a PNG with
# valid CRCs and image data but a malformed ancillary payload (a zTXt with compression method 1, an empty iCCP; Pillow refuses both) passed, and a
# hash-matching pair of them was honoured. The check now fails CLOSED: a chunk type not on this list is refused, critical or ancillary, and every
# ancillary on it has its length AND its fields checked to the PNG spec. The list is what the leg's stills and sheets actually carry (survey of the 8853
# evidence PNGs under .claude-state\dual-venue\evidence and the r1 sheets, r13 run dir png-chunk-survey.txt: IHDR, pHYs, sRGB, gAMA, IDAT, IEND and
# nothing else). No text, compressed or colour-profile chunk (tEXt, zTXt, iTXt, iCCP) and no private chunk is on it: the leg writes none.
# r13 fix 1 (adversary r13 ebd1da64 A1 / A2): nor PLTE or tRNS -- the leg writes neither, and both opened a class this check does not decode (a palette
# index past the palette's end in the pixel data; a tRNS before a suggested palette). So a palette image (colour type 3, which needs a PLTE) is refused
# at its IHDR, and a PLTE or tRNS anywhere is refused as off the allowlist.
$script:PngAllowedChunks = @('IHDR', 'IDAT', 'IEND', 'sRGB', 'gAMA', 'pHYs')
# r11 fix 2: the allowlisted ancillary chunks' length, place and count (PNG 1.2 / ISO 15948 section 11.3). $null when the chunk keeps them, else why not.
$script:PngAncillaryLength = @{ gAMA = 4; sRGB = 1; pHYs = 9 }   # (the same for every colour type)
function Get-DvPngAncillaryProblem {
    param([string]$Name, [int]$Length, [bool]$SawIdat, [System.Collections.Generic.HashSet[string]]$Seen, [byte[]]$Bytes, [int]$DataAt)
    if ($Name -cin @('IHDR', 'IEND')) { return $null }   # (checked by the caller: IHDR first and 13 bytes, IEND last and empty)
    if ($Name -cnotin @($script:PngAncillaryLength.Keys)) { return "is not on the PNG chunk allowlist ($($script:PngAllowedChunks -join ', '))" }   # (the caller refuses it first)
    if ($Seen.Contains($Name)) { return 'repeats (PNG allows one)' }
    if ($SawIdat) { return 'comes after the image data (PNG requires it before IDAT)' }
    if ($Length -ne $script:PngAncillaryLength[$Name]) { return "is $Length bytes (PNG fixes it at $($script:PngAncillaryLength[$Name]))" }
    # r13: the enumerated and ranged fields (PNG 1.2 sections 4.2.x; a PNG four-byte unsigned integer is at most 2^31-1)
    $u32 = { param([int]$o) ([long]$Bytes[$DataAt + $o] -shl 24) -bor ([long]$Bytes[$DataAt + $o + 1] -shl 16) -bor ([long]$Bytes[$DataAt + $o + 2] -shl 8) -bor [long]$Bytes[$DataAt + $o + 3] }
    if ($Name -ceq 'sRGB' -and $Bytes[$DataAt] -gt 3) { return "has rendering intent $($Bytes[$DataAt]) (PNG defines 0 .. 3)" }
    if ($Name -ceq 'gAMA') { $g = & $u32 0; if ($g -lt 1 -or $g -gt 0x7FFFFFFF) { return "has gamma $g (PNG requires 1 .. 2^31-1; zero is meaningless)" } }
    if ($Name -ceq 'pHYs') {
        $px = & $u32 0; $py = & $u32 4
        if ($px -gt 0x7FFFFFFF -or $py -gt 0x7FFFFFFF) { return "has pixels per unit $px x $py (PNG allows at most 2^31-1)" }
        if ($Bytes[$DataAt + 8] -gt 1) { return "has unit specifier $($Bytes[$DataAt + 8]) (PNG defines 0 unknown and 1 metre)" }
    }
    $null
}
function Get-DvPngImageProblem {
    param([AllowNull()][byte[]]$Bytes)
    if ($null -eq $Bytes -or $Bytes.Length -eq 0) { return 'it is empty (0 bytes)' }
    $n = $Bytes.Length
    for ($i = 0; $i -lt [Math]::Min(8, $n); $i++) { if ($Bytes[$i] -ne $script:PngSignature[$i]) { return "it does not start with the 8-byte PNG signature ($n bytes)" } }
    if ($n -lt $script:PngMinBytes) { return "it is $n bytes, shorter than the smallest PNG ($($script:PngMinBytes) bytes: signature, IHDR, IDAT and IEND), so it is truncated" }
    $u32 = { param([int]$at) ([long]$Bytes[$at] -shl 24) -bor ([long]$Bytes[$at + 1] -shl 16) -bor ([long]$Bytes[$at + 2] -shl 8) -bor [long]$Bytes[$at + 3] }
    $type = { param([int]$at) [Text.Encoding]::ASCII.GetString($Bytes, $at, 4) }   # (a byte over 0x7F decodes to '?', which no chunk type matches)
    if ((& $u32 8) -ne 13 -or (& $type 12) -cne 'IHDR') { return 'its first chunk is not a 13-byte IHDR' }
    $width = & $u32 16; $height = & $u32 20
    if ($width -lt 1 -or $width -gt 0x7FFFFFFF -or $height -lt 1 -or $height -gt 0x7FFFFFFF) { return "its IHDR says $width x $height (width and height must be 1 .. 2^31-1)" }
    $depth = [int]$Bytes[24]; $colour = [int]$Bytes[25]
    if (-not ($script:PngBitDepths.ContainsKey($colour) -and $depth -in $script:PngBitDepths[$colour])) { return "its IHDR bit depth $depth with colour type $colour is not a PNG pixel format" }
    if ($Bytes[26] -ne 0 -or $Bytes[27] -ne 0 -or $Bytes[28] -gt 1) { return 'its IHDR compression, filter or interlace method is not one PNG defines' }
    # r13 fix 1 (adversary r13 ebd1da64 A1): a palette image needs a PLTE, which is off the allowlist (the leg writes no palette image)
    if ($colour -eq 3) { return "its IHDR colour type 3 is a palette image, which needs a PLTE chunk, and PLTE is not on the PNG chunk allowlist ($($script:PngAllowedChunks -join ', '))" }
    $interlaced = ($Bytes[28] -eq 1)
    $at = 8; $sawIdat = $false; $idatEnded = $false
    $seenChunks = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    $idat = [IO.MemoryStream]::new()
    while ($true) {
        if ($at + 12 -gt $n) { return 'its chunks end without an IEND chunk, so it is truncated' }
        $length = & $u32 $at; $name = & $type ($at + 4)
        if ($name -cnotmatch '^[A-Za-z]{4}$') { return "the chunk at byte $at has no PNG chunk type" }
        if ($length -gt 0x7FFFFFFF -or $at + 12 + $length -gt $n) { return "its $name chunk at byte $at runs past the end of the file ($n bytes), so it is truncated" }
        # r11 fix 1 (adversary r11 b4a4147f A1 and its class): the chunk sequence a decoder accepts. IHDR only first (a second, contradicting IHDR was
        # ignored); no critical chunk (upper-case first letter) other than IHDR, PLTE, IDAT and IEND; at most one PLTE, and never after the image data;
        # the IDAT chunks consecutive.
        if ($name -ceq 'IHDR' -and $at -ne 8) { return "it has a second IHDR chunk at byte $at" }
        # r13 (sol r12 B1): fail CLOSED on the chunk type. A chunk not on the allowlist is refused, critical or ancillary (it was skipped through r12).
        # (r13 fix 1: PLTE is off the list too, so a second PLTE or a PLTE after the image data is refused here, as is any PLTE)
        if ($name -cnotin $script:PngAllowedChunks) {
            $why = $(if ($name -ceq 'PLTE') { ': the leg writes no palette' } elseif ($name -cmatch '^[A-Z]') { ': a critical chunk PNG does not define' } else { '' })
            return "its $name chunk at byte $at is not on the PNG chunk allowlist ($($script:PngAllowedChunks -join ', '))$why"
        }
        if ($name -cne 'IDAT' -and $sawIdat) { $idatEnded = $true }
        # r12 A1: the CRC-32 of EVERY chunk (type + data), IDAT included. r12 A2 (advisory): IEND carries no data.
        if ((Get-DvPngCrc32 -Bytes $Bytes -Offset ($at + 4) -Count (4 + [int]$length)) -ne (& $u32 ($at + 8 + [int]$length))) { return "its $name chunk at byte $at fails its CRC-32" }
        if ($name -ceq 'IEND' -and $length -ne 0) { return "its IEND chunk at byte $at holds $length bytes (PNG fixes IEND at 0)" }
        if ($name -ceq 'IDAT') {
            if ($idatEnded) { return "its IDAT chunk at byte $at is not consecutive with the earlier IDAT chunks" }
            $sawIdat = $true; $idat.Write($Bytes, $at + 8, [int]$length)
        }
        else {
            # r11 fix 2 (adversary r11 5a79915c A1 and its class): a chunk PNG defines with a size, a place or a count fixed by the image is refused when
            # it breaks them (a decoder rejects it: a 1-byte tRNS on an RGB image, an empty gAMA, sRGB or pHYs passed): its length, before IDAT, and
            # not repeated. r13: and every allowlisted chunk's fields (sRGB intent, gAMA value, pHYs units), from its bytes.
            $problem = Get-DvPngAncillaryProblem -Name $name -Length ([int]$length) -SawIdat $sawIdat -Seen $seenChunks -Bytes $Bytes -DataAt ($at + 8)
            if ($null -ne $problem) { return "its $name chunk at byte $at $problem" }
            [void]$seenChunks.Add($name)
        }
        if ($name -ceq 'IEND') { break }
        $at += 12 + [int]$length
    }
    if (-not $sawIdat) { return 'it has no IDAT chunk, so it holds no image data' }
    # The scanlines IHDR implies: one segment per non-empty pass (one pass when not interlaced), each row a filter byte plus ceil(w * bits / 8) bytes
    $bits = $depth * $script:PngChannels[$colour]
    $passes = $script:PngAdam7
    if (-not $interlaced) { $passes = @(, @(0, 0, 1, 1)) }   # (a direct assignment: a $(...) or if-expression would unroll the one pass into four numbers)
    # r11 fix 1 (adversary r11 b4a4147f A3): the byte count IHDR implies, bounded in floating point BEFORE any [long] arithmetic. A 2^31-1 square 16-bit
    # RGBA header implied ~3.7e19 bytes and threw an Int64 overflow out of this function (collection UNRESOLVED) instead of a refusal. No still this
    # check reads is near 1 GiB inflated (an 8K 16-bit RGBA frame is ~265 MB), so a larger header is refused as no image this check reads.
    $impliedBytes = [double]$height * (1.0 + [Math]::Ceiling([double]$width * [double]$bits / 8.0))
    if ($impliedBytes -gt 1GB) { return "its IHDR says $width x $height at $bits bits per pixel, about $([Math]::Round($impliedBytes / 1GB, 1)) GiB of image data, larger than any still this check reads (1 GiB)" }
    $segments = [System.Collections.Generic.List[long[]]]::new()
    foreach ($p in $passes) {
        $pw = $(if ($width -gt $p[0]) { [Math]::Floor(($width - $p[0] + $p[2] - 1) / $p[2]) } else { 0 })
        $ph = $(if ($height -gt $p[1]) { [Math]::Floor(($height - $p[1] + $p[3] - 1) / $p[3]) } else { 0 })
        if ($pw -gt 0 -and $ph -gt 0) { $segments.Add([long[]]@([long]$ph, [long](1 + [Math]::Floor(($pw * $bits + 7) / 8)))) }
    }
    [long]$expected = 0; foreach ($s in $segments) { $expected += $s[0] * $s[1] }
    # r13 fix 2 (adversary r13 8db7736e A2): the 1 GiB cap applies to the EXACT byte count too. The estimate above counts one pass; Adam7 adds a filter
    # byte per row of every pass and rounds each pass's rows up, so a 2 x 2^29 1-bit interlaced header estimated 1 GiB (admitted) but implies 1.5 GiB,
    # which was then inflated, twice. (The estimate bounds $expected well inside [long], so this sum cannot overflow.)
    if ($expected -gt 1GB) { return "its IHDR says $width x $height at $bits bits per pixel$(if ($interlaced) { ', interlaced' }), $expected bytes of image data, larger than any still this check reads (1 GiB)" }
    # r11 fix 1 (adversary r11 b4a4147f A2): the IDAT data must be ONE complete zlib stream whose Adler-32 is checked. The inflater stops without an
    # error when its input runs out, so a stream with no Adler-32 trailer, or whose deflate data never reaches a final block, inflated to the right byte
    # count and passed. The inflater now reads the IDAT data followed by a 64-byte 0xFF tail. A deflate stream that never reaches its final block is fed
    # the tail as more deflate data (0xFF codes literals, or a reserved block type), so it errors or inflates past the image's byte count. One that does
    # end is followed by the 4 bytes ZLibStream (.NET 6+) checks as its Adler-32: the real trailer passes, while a missing one (the tail is read instead)
    # or a misplaced one (whatever follows the deflate data is read instead) fails with InvalidDataException.
    # r13 fix 2 (adversary r13 8db7736e A1 / A3): the .NET-before-6 fallback (a raw DeflateStream after the 2-byte header, its Adler-32 compared with the
    # LAST four bytes of the IDAT data) is gone. A raw DeflateStream does not say where the deflate data ends, so junk between the deflate data and a
    # trailer at the end passed (A3), and the header's window size was never checked (A1). Without ZLibStream (PowerShell 7 / .NET 6+, which the leg runs
    # on) the image cannot be verified, so it is refused, fail closed. The header check now also requires CINFO <= 7 (a window of at most 32 KiB, RFC 1950).
    $zType = 'System.IO.Compression.ZLibStream' -as [type]
    if ($null -eq $zType) { return 'its IDAT data cannot be verified here: this runtime has no System.IO.Compression.ZLibStream (.NET 6+, PowerShell 7), which the image check needs to find the zlib trailer' }
    $idatLength = $idat.Length
    if ($idatLength -lt 6) { return "its IDAT data is $idatLength bytes, too short for a zlib stream (2-byte header, deflate data, 4-byte Adler-32)" }
    $idatBytes = $idat.ToArray(); $idat.Dispose()
    $cmf = [int]$idatBytes[0]; $flg = [int]$idatBytes[1]
    if (($cmf -band 0x0F) -ne 8 -or ($cmf -shr 4) -gt 7 -or (($cmf * 256 + $flg) % 31) -ne 0 -or ($flg -band 0x20) -ne 0) { return 'its IDAT data does not start with a zlib header (deflate, a window of at most 32 KiB, no preset dictionary)' }
    # r12 fix 2 (adversary r12 0f6daf3a A1): a stream whose Adler-32 trailer was cut short by ONE byte passed, because the 0xFF tail supplied the
    # missing byte (the real trailer ended in 0xFF). So the stream is inflated TWICE, once with a 0xFF tail and once with a 0x00 tail, and must pass
    # both: a trailer byte (or deflate data) borrowed from the tail matches at most one of the two fills, so the verdict cannot depend on bytes that are
    # not in the file. (A 0x00 tail read as deflate data opens a stored block whose LEN / NLEN do not complement, so a stream that never ends errors.)
    foreach ($tailByte in @(0xFF, 0x00)) {
    $fed = [IO.MemoryStream]::new()
    $fed.Write($idatBytes, 0, $idatLength)
    $fed.Write([byte[]](@($tailByte) * 64), 0, 64)
    $fed.Position = 0
    $z = $null
    try {
        $z = $zType::new($fed, [IO.Compression.CompressionMode]::Decompress)
        $buf = [byte[]]::new(65536); [long]$total = 0
        $seg = 0; [long]$rowsLeft = $segments[0][0]; [long]$nextFilter = 0
        while (($got = $z.Read($buf, 0, $buf.Length)) -gt 0) {
            if ($total + $got -gt $expected) { return "its IDAT data inflates to more than the $expected bytes a $width x $height image holds (or its deflate data never ends)" }
            while ($seg -lt $segments.Count -and $nextFilter -lt $total + $got) {
                $f = $buf[$nextFilter - $total]
                if ($f -gt 4) { return "a scanline of its image data has filter type $f (PNG defines 0 .. 4), so it is not image data" }
                $nextFilter += $segments[$seg][1]; $rowsLeft--
                if ($rowsLeft -eq 0) { $seg++; if ($seg -lt $segments.Count) { $rowsLeft = $segments[$seg][0] } }
            }
            $total += $got
        }
    }
    catch { return "its IDAT data is not a valid zlib stream ($($_.Exception.GetType().Name))" }
    finally { if ($null -ne $z) { $z.Dispose() }; $fed.Dispose() }
    if ($total -ne $expected) { return "its IDAT data inflates to $total bytes but a $width x $height image needs $expected, so it is truncated" }
    }   # (foreach tail byte)
    $null
}

# --- CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 r6: the paused stills of a look.pairedSeekFrames leg, ONE reader for the runner and the validator ---------
# (adversary r5 A2 / A3, Dot 5c6bcd98, sol r5 key B1 / B2) Still i (frame-<ii>) counts only when (1) its png AND its sidecar are in the hash listing;
# (2) EACH is read back now and its bytes hash to the listed sha256 -- a file missing, replaced or truncated after the listing is a mismatch naming it
# (r5 took the png's presence from the listing alone); (3) the sidecar, parsed from those same bytes, says the still was captured PAUSED through the full
# reconstruction path, as the app writes a seek capture (capture_mode "seek", playback_path false; a field that is missing or of another type is a
# mismatch); and (4) its display_frame is the requested frame. The runner records this at collection; Test-DvReceiptValid re-derives it with this function.
# r7 (adversary r6 A1, Dot 6558329764, sol r6 key B1): it runs for EVERY look.pairedSeek leg. With no RequestedFrames (a leg that names no
# look.pairedSeekFrames -- all four shipped paired legs) the stills are the ones the listing holds (each png/json stem), (1)-(3) bind each of them, a
# png or a sidecar without its partner is a mismatch naming the absent file, and a leg that collected no still at all is not honoured; only (4) is skipped.
# r8 (adversary r7 A2 / A1 / A3, Dot on #721 16:25:15Z): the stills a leg must hold come from the COMMITTED spec, never from what was collected or what a
# receipt claims. -StillCount is the committed look.contactSheetFrames: the stills are frame-00 .. frame-<StillCount-1> (the app's own numbering, which
# -RequestedFrames, when listed, also follows), so one valid still no longer satisfies a six-frame leg. Every expected still must be present and paused;
# every hash-listed file must belong to one of them (an extra still -- a playing one included -- is a mismatch naming it, never ignored); a StillCount
# that is not a positive integer, or a frame list of another length, is a mismatch. A sidecar whose top level is not ONE JSON object (a one-element
# array, which ConvertFrom-Json would unwrap) is no sidecar.
# r8 fix iteration 1 (adversary r8 A1 / A2): every entry ON DISK in -SeekDir must be a plain file the listing names (an extra still a receipt leaves out of
# its listing is a mismatch, not invisible); and with no frame list, still i must show the display_frame of its IN-PASS partner frame-<ii>.json
# (-InPassFiles: the hash-verified contact-sheet\raw files, Read-DvContactFrames' .files) -- the app seeks to exactly those frames (MainWindow.cpp
# contactSheetTargetFrames = inPassDisplayFrames), so a paused still of another frame is no same-frame comparison.
function Get-DvPairedSeekVerdict {
    param([string]$SeekDir, $Files, [long[]]$RequestedFrames, $StillCount, $InPassFiles)
    $listedSha = @{}
    foreach ($f in @($Files)) { if ($null -ne $f) { $listedSha[[string](Get-DvProp $f 'name')] = [string](Get-DvProp $f 'sha256') } }
    $reported = [System.Collections.Generic.List[object]]::new()
    $mismatches = [System.Collections.Generic.List[string]]::new()
    $show = { param($v) $(if ($null -eq $v) { 'absent' } else { ConvertTo-Json -InputObject $v -Compress }) }
    # r9 (adversary r8 cc8061a2 A2 / A3): the in-pass partner sidecar is read whole -- its display_frame AND its provenance -- not only the frame number
    $readSidecar = { param([byte[]]$b) $o = $null
        try { $o = ([Text.Encoding]::UTF8.GetString($b).TrimStart([char]0xFEFF)) | ConvertFrom-Json -NoEnumerate } catch { $o = $null }
        $(if ($o -is [System.Management.Automation.PSCustomObject]) { $o } else { $null }) }
    $inPassBytes = @{}
    foreach ($f in @($InPassFiles)) { if ($null -ne $f) { $inPassBytes[[string](Get-DvProp $f 'name')] = (Get-DvProp $f 'bytes') } }
    if (-not [string]::IsNullOrEmpty($SeekDir) -and (Test-Path -LiteralPath $SeekDir -PathType Container)) {
        foreach ($actual in @(Get-ChildItem -LiteralPath $SeekDir -Force | Sort-Object Name)) {
            $isReparse = (($actual.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
            if ($actual.PSIsContainer -or $isReparse -or -not $listedSha.ContainsKey($actual.Name)) { $mismatches.Add("'$($actual.Name)' is in paired-seek\contact-sheet\raw but the paired-still listing does not name it as a plain file (every collected still is listed and checked)") }
        }
    }
    $frameList = $null -ne $RequestedFrames
    $count = $(if (($StillCount -is [int] -or $StillCount -is [long]) -and $StillCount -ge 1 -and $StillCount -le 999) { [int]$StillCount } else { 0 })
    if ($count -eq 0) { $mismatches.Add("the committed look.contactSheetFrames ($(& $show $StillCount)) is not a positive whole number, so the paired stills this leg must hold are unknown") }
    elseif ($frameList -and $RequestedFrames.Count -ne $count) { $mismatches.Add("look.pairedSeekFrames names $($RequestedFrames.Count) frame(s) but the committed look.contactSheetFrames is $count") }
    $expectedCount = $(if ($frameList) { $RequestedFrames.Count } else { $count })
    # (@(...) around the whole loop: $(...) would unroll a one-still list into a bare string, and [0] of a string is its first character)
    [string[]]$stills = @(for ($i = 0; $i -lt $expectedCount; $i++) { 'frame-{0:D2}' -f $i })
    if (-not $frameList -and $listedSha.Count -eq 0) { $mismatches.Add('no paused still was collected (look.pairedSeek asks for paused stills, each a png with its sidecar)') }
    for ($i = 0; $i -lt $stills.Count; $i++) {
        $stillName = $stills[$i]
        $requested = $(if ($frameList) { $RequestedFrames[$i] } else { $null })
        $tail = $(if ($frameList) { ", requested $requested" } else { '' })
        $shown = $null; $problem = $null; $sidecarBytes = $null
        if (-not ($listedSha.ContainsKey("$stillName.json") -and $listedSha.ContainsKey("$stillName.png"))) {
            $problem = $(if ($frameList) { "$stillName reported no display_frame$tail" }
                elseif (-not ($listedSha.ContainsKey("$stillName.json") -or $listedSha.ContainsKey("$stillName.png"))) { "$stillName was not collected (the committed spec asks for $expectedCount paired stills, frame-00 .. $($stills[-1]), each a png with its sidecar)" }
                else { "$stillName.$(if ($listedSha.ContainsKey("$stillName.png")) { 'json' } else { 'png' }) is not hash-listed (a paired still needs its png and its sidecar)" })
        }
        else {
            foreach ($ext in 'png', 'json') {
                $file = $(if ([string]::IsNullOrEmpty($SeekDir)) { $null } else { Join-Path $SeekDir "$stillName.$ext" })
                if ($null -eq $file -or -not (Test-Path -LiteralPath $file -PathType Leaf)) { $problem = "$stillName.$ext is missing after it was hash-listed$tail"; break }
                [byte[]]$bytes = [IO.File]::ReadAllBytes($file)
                if ((Get-DvSha256OfBytes $bytes) -cne $listedSha["$stillName.$ext"]) { $problem = "$stillName.$ext changed after it was hash-listed (its bytes no longer hash to the listed sha256)$tail"; break }
                # r10 (sol r9 key B1, adversary successor ADV-A1): a hash-matching png is still no paused image unless it IS a PNG (Get-DvPngImageProblem)
                if ($ext -eq 'png') {
                    $imageProblem = Get-DvPngImageProblem $bytes
                    if ($null -ne $imageProblem) { $problem = "$stillName.png is not a decodable PNG image: $imageProblem (a paired still is a paused image, not a file that merely hash-matches its listing)$tail"; break }
                }
                if ($ext -eq 'json') { $sidecarBytes = $bytes }
            }
        }
        if ($null -eq $problem) {
            $parsed = $null
            # (-NoEnumerate keeps a top-level array an array, so a one-element array sidecar is refused below instead of read as its element)
            try { $parsed = ([Text.Encoding]::UTF8.GetString($sidecarBytes).TrimStart([char]0xFEFF)) | ConvertFrom-Json -NoEnumerate } catch { $parsed = $null }
            if ($parsed -isnot [System.Management.Automation.PSCustomObject]) { $parsed = $null }
            $value = Get-DvProp $parsed 'display_frame'
            # r9 fix 2 (adversary r9 7240a133 A1): a display_frame is a frame NUMBER, so a negative one identifies no frame -- -1 on a still and on its in-pass
            # partner used to compare equal and stay honoured. Both readers accept only a non-negative integer (the frame list's own domain).
            if (($value -is [int] -or $value -is [long]) -and $value -ge 0) { $shown = [long]$value }
            $mode = Get-DvProp $parsed 'capture_mode'; $playbackPath = Get-DvProp $parsed 'playback_path'
            if ($null -eq $shown) { $problem = "$stillName reported no display_frame (its sidecar $stillName.json is not a JSON object carrying a non-negative integer display_frame)$tail" }
            elseif (-not ($mode -is [string] -and $mode -ceq 'seek' -and $playbackPath -is [bool] -and -not $playbackPath)) {
                $problem = "$stillName was not captured paused through the full reconstruction path (its sidecar $stillName.json says capture_mode $(& $show $mode), playback_path $(& $show $playbackPath); a paired still needs capture_mode ""seek"" and playback_path false)$tail"
            }
            elseif ($frameList -and $shown -ne $requested) { $problem = "$stillName reported display_frame $shown$tail" }
            # fix 2 (adversary r8 fix-1 A2): the in-pass partner binds a listed-frame leg too -- the list adds the requested frame, it never removes the pairing
            # r9 (adversary r8 cc8061a2 A2 / A3, Dot pr-721 21:52:55Z, hub ACK 6564543250): the partner must be a frame the measured pass PRESENTED -- its
            # sidecar says capture_mode "in_pass" and playback_path Boolean true, exactly as the app writes an in-pass grab (MainWindow.cpp in-pass sidecar
            # writer; not the string "true", not 1) -- and its own image contact-sheet\raw\<still>.png must be among the hash-verified in-pass files. cc8061a2
            # read only the partner's display_frame, so paused-versus-paused pairs, and six partner sidecars beside a single png, stayed honoured.
            else {
                $partnerSide = $(if ($inPassBytes.ContainsKey("$stillName.json")) { & $readSidecar ([byte[]]$inPassBytes["$stillName.json"]) } else { $null })
                $partnerValue = Get-DvProp $partnerSide 'display_frame'
                $partner = $(if (($partnerValue -is [int] -or $partnerValue -is [long]) -and $partnerValue -ge 0) { [long]$partnerValue } else { $null })
                $partnerMode = Get-DvProp $partnerSide 'capture_mode'; $partnerPath = Get-DvProp $partnerSide 'playback_path'
                if ($null -eq $partner) { $problem = "$stillName has no in-pass partner to pair with (contact-sheet\raw\$stillName.json is not a hash-listed JSON object carrying a non-negative integer display_frame), so its paused still cannot be shown to be the same frame" }
                elseif (-not ($partnerMode -is [string] -and $partnerMode -ceq 'in_pass' -and $partnerPath -is [bool] -and $partnerPath)) {
                    $problem = "$stillName's in-pass partner $stillName.json was not grabbed while playing (it says capture_mode $(& $show $partnerMode), playback_path $(& $show $partnerPath); an in-pass partner needs capture_mode ""in_pass"" and playback_path true), so its paused still is no playing-versus-paused comparison"
                }
                elseif (-not $inPassBytes.ContainsKey("$stillName.png")) { $problem = "$stillName's in-pass partner has no image: contact-sheet\raw\$stillName.png is not among the hash-verified in-pass files, so there is no playing frame to compare its paused still with" }
                # r10 (sol r9 key B1): the partner's hash-verified bytes must be a PNG too -- both members of a pair are images, or neither is compared
                elseif ($null -ne ($partnerImageProblem = Get-DvPngImageProblem ([byte[]]$inPassBytes["$stillName.png"]))) {
                    $problem = "$stillName's in-pass partner image contact-sheet\raw\$stillName.png is not a decodable PNG image: $partnerImageProblem, so there is no playing frame to compare its paused still with"
                }
                # r14 (SAMEFRAME r1, sol r13 B3): only a LISTLESS still must show its partner's frame -- the app replays the in-pass frames there. A listed leg
                # hands the list to the app (--contact-sheet-seek-frames), which seeks to it, so its still is bound to the requested frame above and its partner
                # is the frame THIS arm's measured pass grabbed (r1: H 249 for 250, C 44 for 50); the partner's provenance and image checks above still bind it.
                elseif (-not $frameList -and $shown -ne $partner) { $problem = "$stillName reported display_frame $shown but its in-pass partner $stillName.json shows $partner (a paired still is the paused rendition of the SAME frame)" }
            }
        }
        $reported.Add($shown)
        if ($null -ne $problem) { $mismatches.Add($problem) }
    }
    $expectedNames = @{}
    foreach ($s in $stills) { $expectedNames["$s.png"] = $true; $expectedNames["$s.json"] = $true }
    foreach ($name in @($listedSha.Keys | Sort-Object)) {
        if (-not $expectedNames.ContainsKey($name)) { $mismatches.Add("'$name' is hash-listed among the paired stills but is not one of the $expectedCount the committed spec asks for (frame-00 .. frame-$('{0:D2}' -f [Math]::Max(0, $expectedCount - 1)) png and json); every listed still is checked, none is ignored") }
    }
    [pscustomobject]@{ reportedFrames = $reported.ToArray(); framesHonoured = ($mismatches.Count -eq 0); frameMismatches = $mismatches.ToArray() }
}

# --- PLAYBACK-VISUAL-INSPECTION-1: the window captures every speed leg keeps --------------------------------------------------------------
# The app (--visual-capture-dir) captures the WINDOW with PrintWindow AFTER the measured session has closed: 6 frames across ~3 s of untimed
# play, the frame on screen the instant Play is paused ('stop'), and that frame re-rendered by the paused full-quality path ('paused'). The
# runner copies them into the leg's evidence, hashes them, and derives two numbers from the VIDEO VIEWPORT only (the window's timecode and
# fps chrome change on a frozen picture too): how many distinct pictures the play frames show, and how far the playback-path frame is from
# its paused full-quality re-render. Both are computed here, from the copied files, so they re-derive from the evidence.
$script:VisualSchema = 'mlv-app/dual-venue-visual/v1'
# Per-channel mean absolute difference (0..255, viewport downscaled to 160x90) above which a leg is flagged VISUAL_DIVERGENCE. Calibrated on a
# known-good leg (PLAYBACK-VISUAL-INSPECTION-1 r2, Bachelor M16-1243 windowed-s2 CPU, receipt a5d3ffbe): measured max channel 1.099
# (R 1.099 G 0.551 B 0.800); x3 is 3.3, under the 6.0 floor, so the floor holds.
$script:VisualDivergenceThreshold = 6.0
# A viewport whose pixel standard deviation is under this is flat (all black, all one colour): a blank capture, which says nothing about the display.
$script:VisualFlatViewportStd = 1.0
$script:VisualHashSize = @(64, 36)
$script:VisualDiffSize = @(160, 90)

function Get-DvVisualCaptureRequest {
    <#
    .SYNOPSIS
    Does this leg keep window captures? Every SPEED leg does (an absent visualCapture means on) unless its spec says visualCapture false
    WITH a visualCaptureOptOutReason; a look leg never runs this capture (applies = false: its contact sheet is the look). An opt-out
    without a reason, or a reason without the opt-out, is a malformed spec: DVE_LEG_SPEC_INVALID.
    #>
    param([Parameter(Mandatory)]$Spec)
    $applies = ([string](Get-DvProp $Spec 'legType') -cne 'look')
    $flag = $Spec.PSObject.Properties['visualCapture']
    $reason = $Spec.PSObject.Properties['visualCaptureOptOutReason']
    if ($null -ne $flag -and $flag.Value -isnot [bool]) { throw 'DVE_LEG_SPEC_INVALID visualCapture must be a boolean' }
    if ($null -ne $flag -and -not $flag.Value) {
        if ($null -eq $reason -or [string]::IsNullOrWhiteSpace([string]$reason.Value)) { throw 'DVE_LEG_SPEC_INVALID visualCapture false needs a visualCaptureOptOutReason' }
        return [pscustomobject]@{ applies = $applies; requested = $false; optOutReason = [string]$reason.Value }
    }
    if ($null -ne $reason) { throw 'DVE_LEG_SPEC_INVALID visualCaptureOptOutReason is only for visualCapture false' }
    [pscustomobject]@{ applies = $applies; requested = $applies; optOutReason = $null }
}

function ConvertTo-DvVisualUtcText($Value) {
    # ConvertFrom-Json turns an ISO date string into a DateTime on some pwsh versions and leaves it a string on others: one round-trip UTC text either way.
    if ($null -eq $Value -or [string]::IsNullOrWhiteSpace([string]$Value)) { return $null }
    if ($Value -is [datetime]) { return $(if ($Value.Kind -eq [DateTimeKind]::Local) { $Value.ToUniversalTime() } else { [datetime]::SpecifyKind($Value, [DateTimeKind]::Utc) }).ToString('o') }
    try { return [datetime]::Parse([string]$Value, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime().ToString('o') } catch { return [string]$Value }
}

function Get-DvVisualViewportPixels {
    <#
    .SYNOPSIS
    The video viewport of one captured window PNG, downscaled (high-quality bilinear) to -Width x -Height, as packed RGB bytes, plus their sha256
    (the frame's CONTENT hash: equal pictures hash equal whatever the chrome around them shows) and the pixel mean / standard deviation. The
    viewport rectangle is the app's own (visual.json, in the saved image's pixels). Absent or empty it is UNUSABLE: with -RequireViewport the
    function returns $null (the window's timecode and chrome change over a frozen picture, so the whole image never stands in for the picture);
    without it the whole image is used (callers that only want pixels).
    #>
    param([Parameter(Mandatory)][string]$Path, $Viewport, [int]$Width = 64, [int]$Height = 36, [switch]$RequireViewport)
    Add-Type -AssemblyName System.Drawing
    $src = [System.Drawing.Bitmap]::new($Path)
    try {
        $rect = [System.Drawing.Rectangle]::new(0, 0, $src.Width, $src.Height)
        $viewportUsable = $false
        if ($null -ne $Viewport) {
            $vx = [int](Get-DvProp $Viewport 'x'); $vy = [int](Get-DvProp $Viewport 'y'); $vw = [int](Get-DvProp $Viewport 'w'); $vh = [int](Get-DvProp $Viewport 'h')
            if ($vw -gt 0 -and $vh -gt 0) { $rect = [System.Drawing.Rectangle]::Intersect($rect, [System.Drawing.Rectangle]::new($vx, $vy, $vw, $vh)) }
            $viewportUsable = ($vw -gt 0 -and $vh -gt 0 -and $rect.Width -gt 0 -and $rect.Height -gt 0)
        }
        if (-not $viewportUsable) {
            if ($RequireViewport) { return $null }
            $rect = [System.Drawing.Rectangle]::new(0, 0, $src.Width, $src.Height)
        }
        # The viewport is CLONED out first and scaled with edge mirroring: a filtered scale of a sub-rectangle reads the pixels just outside it,
        # which are the window chrome -- and the chrome (timecode, fps) changes over a frozen picture.
        $crop = $src.Clone($rect, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
        $small = [System.Drawing.Bitmap]::new($Width, $Height, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
        $attrs = [System.Drawing.Imaging.ImageAttributes]::new()
        try {
            $attrs.SetWrapMode([System.Drawing.Drawing2D.WrapMode]::TileFlipXY)
            $g = [System.Drawing.Graphics]::FromImage($small)
            try {
                $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBilinear
                $g.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::Half
                $g.DrawImage($crop, [System.Drawing.Rectangle]::new(0, 0, $Width, $Height), 0, 0, $crop.Width, $crop.Height, [System.Drawing.GraphicsUnit]::Pixel, $attrs)
            } finally { $g.Dispose() }
            $data = $small.LockBits([System.Drawing.Rectangle]::new(0, 0, $Width, $Height), [System.Drawing.Imaging.ImageLockMode]::ReadOnly, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
            try {
                $raw = [byte[]]::new($data.Stride * $Height)
                [System.Runtime.InteropServices.Marshal]::Copy($data.Scan0, $raw, 0, $raw.Length)
                $stride = $data.Stride
            } finally { $small.UnlockBits($data) }
        } finally { $small.Dispose(); $crop.Dispose(); $attrs.Dispose() }
    } finally { $src.Dispose() }
    # Packed R,G,B (GDI rows are B,G,R and padded to a 4-byte stride).
    $rgb = [byte[]]::new($Width * $Height * 3)
    $sum = 0.0; $sumSq = 0.0
    for ($y = 0; $y -lt $Height; $y++) {
        for ($x = 0; $x -lt $Width; $x++) {
            $s = $y * $stride + $x * 3; $d = ($y * $Width + $x) * 3
            $rgb[$d] = $raw[$s + 2]; $rgb[$d + 1] = $raw[$s + 1]; $rgb[$d + 2] = $raw[$s]
        }
    }
    foreach ($v in $rgb) { $sum += $v; $sumSq += [double]$v * $v }
    $n = $rgb.Length
    $mean = $sum / $n
    [pscustomobject]@{ rgb = $rgb; contentHash = (Get-DvSha256OfBytes $rgb); mean = [math]::Round($mean, 3); std = [math]::Round([math]::Sqrt([math]::Max(0.0, $sumSq / $n - $mean * $mean)), 3) }
}

function Get-DvVisualMeanAbsDiff {
    <# Per-channel (R, G, B) mean absolute difference of two equally sized packed-RGB pixel arrays, on the 0..255 scale. #>
    param([Parameter(Mandatory)][byte[]]$A, [Parameter(Mandatory)][byte[]]$B)
    if ($A.Length -ne $B.Length -or $A.Length % 3 -ne 0 -or $A.Length -eq 0) { throw 'DVE_VISUAL_DIFF_SHAPE the two pixel arrays differ in size' }
    $sums = [double[]]::new(3)
    for ($i = 0; $i -lt $A.Length; $i++) { $sums[$i % 3] += [math]::Abs([int]$A[$i] - [int]$B[$i]) }
    $px = $A.Length / 3
    @([math]::Round($sums[0] / $px, 3), [math]::Round($sums[1] / $px, 3), [math]::Round($sums[2] / $px, 3))
}

# VISUAL-GATE-EXPOSURE-FLOOR-1: the gate used to ask only whether the pictures move, tear and match across the A/B arms. A picture that is equally
# wrong in both arms passed (M16-1457: "visual OK 6/6" at mean luma 6.9/255, 84% near-black). The capture step now MEASURES exposure on the picture
# area and the verdict flags a leg whose median numbers leave a band. A measurement, not a look: nothing here changes a pixel the app renders.
$script:ExposureNearBlackLuma = 16      # luma (0..255) under which a pixel is near-black
$script:ExposureClippedLuma = 235       # luma (0..255) over which a pixel is clipped
$script:ExposureLetterboxRowMean = 2    # a viewport row whose full-width mean luma is under this is a dark row; dark rows are letterbox only as a symmetric pair of edge bars
$script:ExposureLetterboxMaxEdgeFraction = 0.25   # ... and at most this fraction of the rows per edge: a deeper bar pair is mostly picture
# The absolute band used when a clip has no row in exposure-bands.json (or the file is missing / empty / malformed): median mean luma >= 16, median
# near-black fraction <= 0.60, median clipped fraction <= 0.05. The upper luma limit is the scale's own end, so it never trips by default.
$script:ExposureFallbackBand = [pscustomobject]@{ minMedianLuma = 16.0; maxMedianLuma = 255.0; maxNearBlackFraction = 0.60; maxClippedFraction = 0.05 }
$script:ExposureBandsPath = Join-Path $PSScriptRoot 'exposure-bands.json'

function Get-DvExposureMedian {
    <# The median of a list of frame measurements (the mean of the middle two when the count is even); $null for an empty list. (Not Get-DvMedian: that is the
       matrix table's helper, which skips missing values; a frame without a measurement is never counted as a zero here either, the caller filters it.) #>
    param([double[]]$Values = @())
    $v = @($Values | Sort-Object)
    if ($v.Count -eq 0) { return $null }
    $mid = [int][math]::Floor($v.Count / 2)
    $median = $(if ($v.Count % 2 -eq 1) { [double]$v[$mid] } else { ([double]$v[$mid - 1] + [double]$v[$mid]) / 2.0 })
    # r4 (adversary 099c3eac A1, 288559f1 A1): the verdict's limits are inclusive, and a median that sits exactly ON one must compare equal to it. Binary floating
    # point cannot do that unaided: (0.4 + 0.8) / 2.0 is 0.6000000000000001, and the middle two of 389/1920 and 1915/1920 average to 0.6000000000000005 (an
    # exact 0.6), a luma median of exactly 40 to 39.99999999999998. The median rounds to 12 decimal places, which absorbs that noise (a few 1e-16 on a fraction,
    # a few 1e-14 on a luma) and keeps every real difference: a limit has at most 6 (fraction) or 4 (luma) places, so a median that is not ON a limit is
    # at least 1/(2 * pixels * 10^6) from it, far above 1e-12 for any picture the gate measures (a cap of 0.00042 still flags one clipped pixel in 2304).
    # NaN, infinity and a magnitude outside 1e-12..1e12 (other than zero) stay as they are, so the verdict's 'a median is not a finite number' still fires.
    if (-not [double]::IsNaN($median) -and -not [double]::IsInfinity($median) -and ($median -eq 0.0 -or ([math]::Abs($median) -ge 1e-12 -and [math]::Abs($median) -le 1e12))) {
        return [math]::Round($median, 12)
    }
    $median
}

function Get-DvVisualExposureStats {
    <#
    .SYNOPSIS
    The exposure numbers of one frame's picture area (pure): from packed R,G,B bytes (-Width x -Height, the viewport already cut out), the BT.709 mean
    luma (0..255), the near-black fraction (luma < 16) and the clipped fraction (luma > 235). A letterbox is a run of dark rows (full-width mean luma
    under 2) at BOTH the top and the bottom edge: the matching depth of the two runs, at most a quarter of the rows per edge, is left out
    (letterboxRowsDropped). Every other dark row is picture: a dark band in the middle, a dark run at only one edge, or the part of a deeper bar beyond
    the cap is a crushed picture (r2, fable B1 / Dot 796-I1: dropping every dark row wherever it sat measured a banded dark frame on its bright rows
    only and read it OK). When EVERY row is that dark the picture itself is black, so all rows are measured (a black frame must read as black, never as
    "no picture to judge"). Returns $null for an empty or mis-sized array. The numbers are NOT rounded: the
    verdict compares them to a band whose caps are the owner's to choose (a cap of 0.00042 must flag one clipped pixel in 2304), so only the
    reason text rounds, for display.
    #>
    param([Parameter(Mandatory)][byte[]]$Rgb, [Parameter(Mandatory)][int]$Width, [Parameter(Mandatory)][int]$Height)
    if ($Width -le 0 -or $Height -le 0 -or $Rgb.Length -ne $Width * $Height * 3) { return $null }
    # r3 (adversary 6256c193 A2): luma is held in whole ten-thousandths (2126 R + 7152 G + 722 B, the BT.709 weights x 10000), so every threshold compare is
    # EXACT integer arithmetic. In floating point a row whose true mean is exactly 2 summed to 1.9999999999999996, read as a dark bar and was dropped, and
    # a pixel of luma exactly 16 or 235 could fall either side of its own limit.
    $scale = 10000L
    $rowSum = [long[]]::new($Height)
    $luma = [long[]]::new($Width * $Height)
    for ($y = 0; $y -lt $Height; $y++) {
        [long]$sumRow = 0
        for ($x = 0; $x -lt $Width; $x++) {
            $i = ($y * $Width + $x) * 3
            [long]$l = 2126L * $Rgb[$i] + 7152L * $Rgb[$i + 1] + 722L * $Rgb[$i + 2]
            $luma[$y * $Width + $x] = $l; $sumRow += $l
        }
        $rowSum[$y] = $sumRow
    }
    # a row is dark when its full-width mean is under the limit: rowSum / (Width * scale) < limit  <=>  rowSum < limit * Width * scale
    $darkBelow = [long]$script:ExposureLetterboxRowMean * $Width * $scale
    $top = 0
    while ($top -lt $Height -and $rowSum[$top] -lt $darkBelow) { $top++ }
    $drop = 0
    if ($top -lt $Height) {   # (an all-dark frame is a black picture: nothing is a bar)
        $bottom = 0
        while ($rowSum[$Height - 1 - $bottom] -lt $darkBelow) { $bottom++ }   # ($top < $Height, so a bright row ends this walk)
        $drop = [math]::Min([math]::Min($top, $bottom), [int][math]::Floor($Height * $script:ExposureLetterboxMaxEdgeFraction))
    }
    $keep = @($drop..($Height - 1 - $drop))
    [long]$sum = 0; $nearBlack = 0; $clipped = 0
    $nearBlackBelow = [long]$script:ExposureNearBlackLuma * $scale; $clippedAbove = [long]$script:ExposureClippedLuma * $scale
    foreach ($y in $keep) {
        for ($x = 0; $x -lt $Width; $x++) {
            $l = $luma[$y * $Width + $x]
            $sum += $l
            if ($l -lt $nearBlackBelow) { $nearBlack++ }
            if ($l -gt $clippedAbove) { $clipped++ }
        }
    }
    $n = $keep.Count * $Width
    [pscustomobject]@{
        lumaMean = [double]$sum / ([double]$n * $scale); nearBlackFraction = $nearBlack / $n; clippedFraction = $clipped / $n
        rows = $Height; letterboxRowsDropped = $Height - $keep.Count
    }
}

function Test-DvOrdinalEqual($A, $B) {
    # exact (code-unit) string equality: PowerShell's -ceq is a culture comparison that ignores embedded NULs and zero-width characters
    [string]::Equals([string]$A, [string]$B, [System.StringComparison]::Ordinal)
}

function Get-DvJsonRepeatedKey($Element) {
    # the first property name (ordinal) that a System.Text.Json object element gives twice, else $null
    $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    foreach ($p in $Element.EnumerateObject()) { if (-not $seen.Add([string]$p.Name)) { return [string]$p.Name } }
    $null
}

function ConvertTo-DvExposureLimit($Element, [double]$Min, [double]$Max, [int]$MaxPlaces = 6) {
    # -MaxPlaces: the most decimal places a limit may carry (r3, adversary c9ac4917 A1/A2). A measurement is a ratio of whole numbers (pixel counts over at most
    # 64 x 36 = 2304 pixels, a luma sum over 10000ths of that, medians of two such): it is a double rounded from that ratio, so a limit finer than the
    # measurement can resolve (0.00043402777777777775 against 1/2304, 100.00043402777777 against a luma mean) rounds to the SAME double as an outside
    # measurement and reads in band. A limit of at most 6 places (fractions) or 4 (luma) differs from every possible measurement by far more than a double's
    # rounding (>= 2.2e-10 and >= 2.2e-12 against errors under 1e-13), so a tie in doubles is a tie in the numbers. Finer is refused, never rounded.
    # a System.Text.Json element that is a real JSON number inside [Min, Max] -> that double; anything else (a string "16", a bool, null, out of range) -> $null
    if ($Element.ValueKind -ne [System.Text.Json.JsonValueKind]::Number) { return $null }
    $d = 0.0
    if (-not $Element.TryGetDouble([ref]$d)) { return $null }
    # (IsNegative, not "-lt 0": TryGetDouble reads -1e-400 as -0.0, which is not under 0 but is a negative limit; adversary fc152c48 A1)
    if ([double]::IsNaN($d) -or [double]::IsInfinity($d) -or [double]::IsNegative($d) -or $d -lt $Min -or $d -gt $Max) { return $null }
    # r3 (adversary 6256c193 A1): the double must carry EXACTLY what the file says. TryGetDouble rounds 1.0000000000000001 to 1.0 (in range) and
    # 16.000000000000001 to 16.0 (so min <= max held), though the file states a value above its maximum; a limit whose double reads back as another number
    # is refused, never silently rounded into range. (The shortest round-trip text of the double names the same number as the file's text, or it is refused.)
    $rawKey = ConvertTo-DvDecimalKey $Element.GetRawText()
    if ($rawKey -cne (ConvertTo-DvDecimalKey $d.ToString('R', [System.Globalization.CultureInfo]::InvariantCulture))) { return $null }
    # ...and carry no more places than a measurement can resolve (a key is "sign+digits|exponent": a negative exponent counts places)
    $exponent = [System.Numerics.BigInteger]::Parse($rawKey.Split('|')[1], [System.Globalization.CultureInfo]::InvariantCulture)
    if ($exponent -lt -$MaxPlaces) { return $null }
    $d
}

function ConvertTo-DvDecimalKey([string]$Text) {
    # the exact value of a JSON / .NET number literal ([-]digits[.digits][e[+-]digits]) as a normalised "sign+digits|exponent" key (no leading or trailing zeros in the
    # digits, so 1.0, 1 and 10e-1 are one key); $null when it is not such a literal. Pure string and BigInteger arithmetic: no rounding anywhere.
    $m = [regex]::Match($Text, '^(-?)([0-9]+)(?:\.([0-9]+))?(?:[eE]([+-]?[0-9]+))?$')
    if (-not $m.Success) { return $null }
    $fraction = $m.Groups[3].Value
    $exponent = [System.Numerics.BigInteger]::Zero
    if ($m.Groups[4].Success) { $exponent = [System.Numerics.BigInteger]::Parse($m.Groups[4].Value, [System.Globalization.CultureInfo]::InvariantCulture) }
    $exponent = $exponent - $fraction.Length
    $digits = ($m.Groups[2].Value + $fraction).TrimStart('0')
    if ($digits.Length -eq 0) { return '0|0' }
    $trimmed = $digits.TrimEnd('0')
    $exponent = $exponent + ($digits.Length - $trimmed.Length)
    "$($m.Groups[1].Value)$trimmed|$exponent"
}

function ConvertTo-DvExposureRow($Row, $Fallback) {
    # one clip row (a System.Text.Json element) -> [pscustomobject]@{ band; why }: band is the row's band with a left-out field taking the fallback's value,
    # why is $null; or band is $null and why says what is wrong with the row (not an object, a repeated or unknown field, an unusable value, none of the
    # four fields, min above max)
    $fail = { param($why) [pscustomobject]@{ band = $null; why = $why } }
    if ($Row.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) { return (& $fail 'not an object') }
    $repeated = Get-DvJsonRepeatedKey $Row
    if ($null -ne $repeated) { return (& $fail "the row repeats the field '$repeated'") }
    $limits = [ordered]@{ minMedianLuma = @(0.0, 255.0, 4); maxMedianLuma = @(0.0, 255.0, 4); maxNearBlackFraction = @(0.0, 1.0, 6); maxClippedFraction = @(0.0, 1.0, 6) }   # (range min, range max, most decimal places)
    # a field this gate does not know (a misspelt cap beside a valid one) must not silently take the fallback's value (names compare ordinally)
    foreach ($property in $Row.EnumerateObject()) {
        if (@($limits.Keys | Where-Object { Test-DvOrdinalEqual $_ $property.Name }).Count -eq 0) { return (& $fail "the row has the unknown field '$($property.Name)'") }
    }
    $band = [ordered]@{}; $named = 0
    foreach ($name in $limits.Keys) {
        $fields = @($Row.EnumerateObject() | Where-Object { Test-DvOrdinalEqual $_.Name $name })
        if ($fields.Count -eq 0) { $band[$name] = $Fallback.$name; continue }
        $value = ConvertTo-DvExposureLimit $fields[0].Value $limits[$name][0] $limits[$name][1] $limits[$name][2]
        if ($null -eq $value) { return (& $fail "$name must be a number in $($limits[$name][0])..$($limits[$name][1]) with at most $($limits[$name][2]) decimal places") }
        $band[$name] = [double]$value; $named++
    }
    if ($named -eq 0) { return (& $fail 'it names none of minMedianLuma, maxMedianLuma, maxNearBlackFraction, maxClippedFraction') }
    if ($band.minMedianLuma -gt $band.maxMedianLuma) { return (& $fail 'minMedianLuma is above maxMedianLuma') }
    [pscustomobject]@{ band = [pscustomobject]$band; why = $null }
}

function Get-DvExposureBand {
    <#
    .SYNOPSIS
    The exposure band a clip's legs are judged against: its row in exposure-bands.json, else the absolute fallback band. Returns
    [pscustomobject]@{ band; source; note }. source: clip (the clip's row) | fallback-no-row (the file is valid and has no row for the clip, or no clip
    id was given) | fallback-empty (the file is empty) | fallback-missing (absent, or gone after it was found) | fallback-unreadable (it exists but cannot be
    probed or read: access denied, locked, an unusable path; r3, sol r2 B1: never an escaped exception under $ErrorActionPreference = 'Stop') | fallback-malformed
    (read, but not a usable band file). note is $null unless the file could not be used (missing, unreadable, empty or malformed): that is REPORTED, never a silent OK. A row names any of minMedianLuma, maxMedianLuma, maxNearBlackFraction,
    maxClippedFraction and nothing else; a field it leaves out takes the fallback's value. A row with an unusable field (not a number, out of range, min
    above max), a field it repeats or does not know, or with none of the four, is malformed and the fallback band judges instead. Clip ids match exactly
    (case-sensitive). r2 (sol B1, Dot 796-D1): the file is validated WHOLE, every row, before any row is selected (and whether or not the clip has one):
    a malformed row anywhere makes the file malformed, so a relaxed row can never be picked from a file that has an ambiguous row beside it.
    #>
    param([string]$ClipId, [string]$Path = $script:ExposureBandsPath)
    # r5 (adversary 288559f1/a7424c18 A1): a FRESH band per call, copied field by field from the constant. Handing back the script-scope object itself let a
    # caller that edited one result's .band (a floor of 0, a ceiling of 255) change the fallback every later clip is judged against. A row from the file is
    # already built per call (ConvertTo-DvExposureRow); every fallback-* result below returns this copy, never the constant.
    $fallback = [pscustomobject]@{
        minMedianLuma = [double]$script:ExposureFallbackBand.minMedianLuma; maxMedianLuma = [double]$script:ExposureFallbackBand.maxMedianLuma
        maxNearBlackFraction = [double]$script:ExposureFallbackBand.maxNearBlackFraction; maxClippedFraction = [double]$script:ExposureFallbackBand.maxClippedFraction
    }
    $result ={ param($source, $note, $band) [pscustomobject]@{ band = $band; source = $source; note = $note } }
    $unreadable = { param($e) (& $result 'fallback-unreadable' "the exposure band file cannot be read ($($e.GetType().Name)): the absolute fallback band judges" $fallback) }
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return (& $result 'fallback-missing' "the exposure band file is missing ($Path): the absolute fallback band judges" $fallback)
    }
    # r3 (sol r2 B1): the existence probe is inside the guard. The runner runs under $ErrorActionPreference = 'Stop', where Test-Path throws on a path it cannot
    # check (access denied, an embedded NUL, ...): that escaped this function, so the measured leg ended as RUNNER_ERROR instead of the reported fallback.
    try { $isFile = [bool](Test-Path -LiteralPath $Path -PathType Leaf -ErrorAction Stop) } catch { return (& $unreadable $_.Exception) }
    if (-not $isFile) {
        return (& $result 'fallback-missing' "the exposure band file is missing ($Path): the absolute fallback band judges" $fallback)
    }
    # a relative path resolves the way Test-Path just did (against the PowerShell location), not against the process directory ReadAllText uses (adversary fc152c48 A2)
    try { $Path = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).ProviderPath; $text = [IO.File]::ReadAllText($Path) } catch {
        # r3 (fable N1): a file that went away between the probe and the read is missing (not malformed); one that exists but cannot be read (access denied, locked) is unreadable
        $e = $_.Exception
        # (PowerShell wraps a .NET method's exception in a MethodInvocationException: the real cause is inside)
        while ($e -is [System.Management.Automation.MethodInvocationException] -and $null -ne $e.InnerException) { $e = $e.InnerException }
        if ($e -is [System.Management.Automation.ItemNotFoundException] -or $e -is [System.IO.FileNotFoundException] -or $e -is [System.IO.DirectoryNotFoundException]) {
            return (& $result 'fallback-missing' "the exposure band file is missing ($Path; it went away after it was found): the absolute fallback band judges" $fallback)
        }
        return (& $unreadable $e)
    }
    if ([string]::IsNullOrWhiteSpace($text)) { return (& $result 'fallback-empty' "the exposure band file is empty ($Path): the absolute fallback band judges" $fallback) }
    # System.Text.Json, not ConvertFrom-Json: that unwraps a one-element array root ([{"clips": ...}] reads as the object, so a non-object file would pick a
    # row), and rejects keys that differ only in case ({"M16-1457": ..., "m16-1457": ...} is valid JSON and clip ids match exactly)
    $json = $null
    try { $json = [System.Text.Json.JsonDocument]::Parse($text) } catch { return (& $result 'fallback-malformed' 'the exposure band file is not valid JSON: the absolute fallback band judges' $fallback) }
    try {
        $root = $json.RootElement
        $bad = { param($why) (& $result 'fallback-malformed' "the exposure band file is unusable ($why): the absolute fallback band judges" $fallback) }
        if ($root.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) { return (& $bad 'the root is not a JSON object') }
        # (names are compared ORDINALLY, never with -ceq: that is a culture comparison, and "M16-1457<NUL>" -ceq "M16-1457" is true)
        # a key given twice at the root or in clips is ambiguous whatever its name: the file is malformed, not "the first one" and not "the other rows"
        $repeated = Get-DvJsonRepeatedKey $root
        if ($null -ne $repeated) { return (& $bad "the root repeats the key '$repeated'") }
        $clipsProps = @($root.EnumerateObject() | Where-Object { Test-DvOrdinalEqual $_.Name 'clips' })
        if ($clipsProps.Count -eq 0) { return (& $result 'fallback-no-row' $null $fallback) }
        $clips = $clipsProps[0].Value
        if ($clips.ValueKind -eq [System.Text.Json.JsonValueKind]::Null) { return (& $result 'fallback-no-row' $null $fallback) }
        if ($clips.ValueKind -ne [System.Text.Json.JsonValueKind]::Object) { return (& $bad 'clips is not a JSON object') }
        $repeated = Get-DvJsonRepeatedKey $clips
        if ($null -ne $repeated) { return (& $bad "clips repeats the clip id '$repeated'") }
        # every row is checked BEFORE one is chosen (and also when the clip has none): a malformed row anywhere is a malformed file
        $selected = $null
        foreach ($rowProperty in $clips.EnumerateObject()) {
            $parsed = ConvertTo-DvExposureRow $rowProperty.Value $fallback
            if ($null -ne $parsed.why) { return (& $bad "the row for clip '$($rowProperty.Name)' is malformed: $($parsed.why)") }
            if (-not [string]::IsNullOrWhiteSpace($ClipId) -and (Test-DvOrdinalEqual $rowProperty.Name $ClipId)) { $selected = $parsed.band }
        }
        if ($null -eq $selected) { return (& $result 'fallback-no-row' $null $fallback) }
        & $result 'clip' $null $selected
    } catch {
        # a document the walk above cannot read (a key that is not valid text, say) is malformed, never a throw that ends the leg
        return (& $result 'fallback-malformed' "the exposure band file cannot be read ($($_.Exception.GetType().Name)): the absolute fallback band judges" $fallback)
    } finally { $json.Dispose() }
}

function Get-DvVisualVerdict {
    <#
    .SYNOPSIS
    The visual gate, from numbers only (pure). FROZEN_DISPLAY: every play frame shows ONE picture while the app says it presented more than
    one frame across them AND the source frames are KNOWN to differ (-SourceDifference known-differ) -- the measured trap where every cadence metric
    reads perfect over a frozen display; such a leg is NOT counted. The same picture over known-identical source frames is a static clip, not frozen
    (OK); over an unknown source (the default: nothing records it, so it is never guessed) it is FROZEN_DISPLAY_SUSPECTED, counted and surfaced.
    -PlayFrames is the number of JUDGED play frames (those with a usable viewport); -ViewportUnavailablePlayFrames are the saved ones without. The judged
    frames are judged first: two or more of them are enough for FROZEN_DISPLAY and the other verdicts, whatever the rest lack.
    VISUAL_ASSESSMENT_UNAVAILABLE: play frames lack a usable viewport rectangle AND fewer than two judged ones remain, so nothing is judged (counted, surfaced; never the whole window).
    VISUAL_DIVERGENCE: the playback-path frame is further than the threshold from its paused full-quality re-render; counted for speed and
    surfaced. CAPTURE_BLANK: every play frame's viewport is flat (a blank capture judges nothing). NOT_CAPTURED / NOT_REQUESTED: no captures.
    CAPTURE_UNBOUND (PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1): the app says the paused-instant capture's pixels are not the frame its label names
    (-LabelBound $false), so its difference from the paused re-render compares two moments; counted, judged before VISUAL_DIVERGENCE. A
    -LabelBound of $null (a build that predates the binding) changes nothing.
    PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2: the divergence compares TWO captures, so both must be bound. -PausedLabelBound is the paused capture's
    label_bound. When either side carries a binding (a binding build), anything but $true on BOTH -- $false, or a side that is missing -- is
    CAPTURE_UNBOUND (fail closed); only when neither carries one (a pre-binding build) does the gate judge as before.
    PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r3 (Dot 7dc103f1 B2): -ObserverPaintedPlayFrames are the unforced play frames NOT judged because a forced
    capture's paint may be what they show (a forced paint stays on screen when the display's own paint is stuck); when they leave fewer than two
    judged, it is VISUAL_ASSESSMENT_UNAVAILABLE (fail closed: such pictures cannot tell a moving display from a frozen one).
    PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r5 (hub ruling, ACK 6388020454): PARTIAL_FREEZE -- two or more judged play frames repeat the picture
    before them while the app's label advanced (-RepeatedPlayFrames) AND the source frames are known to differ: the display's own paint stopped
    landing for part of the tail. Counted and surfaced, never OK; the whole-tail FROZEN_DISPLAY outranks it. Over an unknown source a repeat may be
    the source's own picture, so it stays a note on an OK block (the reader's repeatedPlayFramesNote); one repeat alone is OK.
    VISUAL-GATE-EXPOSURE-FLOOR-1: EXPOSURE_OUT_OF_RANGE -- the leg's MEDIAN picture-area mean luma, near-black fraction or clipped fraction (-ExposureMedianLuma,
    -ExposureMedianNearBlackFraction, -ExposureMedianClippedFraction; Get-DvVisualExposureStats) is outside -ExposureBand (Get-DvExposureBand: the clip's row,
    else the absolute fallback band). Counted and surfaced like VISUAL_DIVERGENCE, never an exclusion, and judged after every other verdict: a picture that
    is equally wrong in both A/B arms (M16-1457: mean luma 6.9/255, 84% near-black) used to be a plain OK. The reason quotes the three numbers and the band
    limits they broke. Without -ExposureBand, or without the three medians, nothing is judged on exposure and every verdict is what it was.
    #>
    param([bool]$Requested, [bool]$Captured, [int]$PlayFrames, [int]$DistinctContentHashes, [int]$AppDistinctDisplayFrames, [int]$FlatPlayFrames,
          $PlayVsPausedMeanAbsDiff, [bool]$SamePosition, [double]$Threshold = $script:VisualDivergenceThreshold,
          [ValidateSet('known-differ', 'known-identical', 'unknown')][string]$SourceDifference = 'unknown', [int]$ViewportUnavailablePlayFrames = 0, $LabelBound = $null,
          $PausedLabelBound = $null, [int]$ObserverPaintedPlayFrames = 0, [int]$RepeatedPlayFrames = 0,
          $ExposureMedianLuma = $null, $ExposureMedianNearBlackFraction = $null, $ExposureMedianClippedFraction = $null, $ExposureBand = $null, [string]$ExposureBandSource = '')
    if (-not $Requested) { return [pscustomobject]@{ verdict = 'NOT_REQUESTED'; counted = $true; reason = 'the leg spec opts out of window captures' } }
    if ($ViewportUnavailablePlayFrames -gt 0 -and $PlayFrames -lt 2) { return [pscustomobject]@{ verdict = 'VISUAL_ASSESSMENT_UNAVAILABLE'; counted = $true; reason = "$ViewportUnavailablePlayFrames play frame(s) have no usable viewport rectangle (visual.json), leaving $PlayFrames judged, so the picture cannot be told from the window's timecode and chrome: no visual assessment" } }
    if ($ObserverPaintedPlayFrames -gt 0 -and $PlayFrames -lt 2) { return [pscustomobject]@{ verdict = 'VISUAL_ASSESSMENT_UNAVAILABLE'; counted = $true; reason = "$ObserverPaintedPlayFrames unforced play frame(s) were taken after a forced paint their predecessor did not see, or do not record how many forced paints preceded them (forced_paints_before), leaving $PlayFrames judged: a forced paint stays on screen when the display's own paint is stuck, so those pictures cannot tell a moving display from a frozen one: no visual assessment" } }
    if (-not $Captured -or $PlayFrames -lt 2) { return [pscustomobject]@{ verdict = 'NOT_CAPTURED'; counted = $true; reason = "only $PlayFrames play frame(s) were captured" } }
    if ($FlatPlayFrames -ge $PlayFrames) { return [pscustomobject]@{ verdict = 'CAPTURE_BLANK'; counted = $true; reason = 'every play frame''s viewport is flat: the capture shows no picture, so it judges nothing' } }
    if ($DistinctContentHashes -eq 1 -and $AppDistinctDisplayFrames -gt 1 -and $SourceDifference -ceq 'known-differ') {
        return [pscustomobject]@{ verdict = 'FROZEN_DISPLAY'; counted = $false; reason = "the $PlayFrames play frames show ONE picture while the app presented $AppDistinctDisplayFrames distinct frames across them and the source frames are known to differ" }
    }
    if ($DistinctContentHashes -eq 1 -and $AppDistinctDisplayFrames -gt 1 -and $SourceDifference -ceq 'unknown') {
        return [pscustomobject]@{ verdict = 'FROZEN_DISPLAY_SUSPECTED'; counted = $true; reason = "the $PlayFrames play frames show ONE picture while the app presented $AppDistinctDisplayFrames distinct frames across them, but nothing records whether the source frames differ (a static clip shows one picture legitimately): suspected, not excluded" }
    }
    if ($RepeatedPlayFrames -ge 2 -and $SourceDifference -ceq 'known-differ') {
        return [pscustomobject]@{ verdict = 'PARTIAL_FREEZE'; counted = $true; reason = "$RepeatedPlayFrames of the $PlayFrames judged play frames repeat the picture before them while the app's label advanced and the source frames are known to differ: the display's own paint stopped landing for part of the tail" }
    }
    if (($null -ne $LabelBound -or $null -ne $PausedLabelBound) -and -not ($LabelBound -eq $true -and $PausedLabelBound -eq $true)) {
        $unbound = @(if ($LabelBound -ne $true) { "the paused-instant capture (label_bound $(if ($null -eq $LabelBound) { 'missing' } else { 'false' }))" }
                     if ($PausedLabelBound -ne $true) { "the paused re-render capture (label_bound $(if ($null -eq $PausedLabelBound) { 'missing' } else { 'false' }))" }) -join ' and '
        return [pscustomobject]@{ verdict = 'CAPTURE_UNBOUND'; counted = $true; reason = "$unbound is not bound to its label (the frame painted when it was taken is not shown to be the frame the label names), so the paused-instant vs paused re-render difference ($PlayVsPausedMeanAbsDiff) compares two moments and judges nothing" }
    }
    if ($SamePosition -and $null -ne $PlayVsPausedMeanAbsDiff -and [double]$PlayVsPausedMeanAbsDiff -gt $Threshold) {
        return [pscustomobject]@{ verdict = 'VISUAL_DIVERGENCE'; counted = $true; reason = "the playback-path frame is $PlayVsPausedMeanAbsDiff (per-channel mean absolute difference, 0..255) from its paused full-quality re-render; the threshold is $Threshold" }
    }
    # one picture over source frames the app itself records as identical: the frozen check was waived on its word, so the receipt says so
    $waivedReason = $(if ($DistinctContentHashes -eq 1 -and $AppDistinctDisplayFrames -gt 1 -and $SourceDifference -ceq 'known-identical') { 'one picture; the app records the source frames as identical: a static clip' } else { $null })
    # VISUAL-GATE-EXPOSURE-FLOOR-1: last, so every other verdict keeps its precedence; counted, a flag and never an exclusion
    if ($null -ne $ExposureBand -and $null -ne $ExposureMedianLuma -and $null -ne $ExposureMedianNearBlackFraction -and $null -ne $ExposureMedianClippedFraction) {
        # compared UNROUNDED; the reason rounds to 6 places for display (a median 0.000434 against a cap of 0.00042 is over, and reads as 0.000434)
        $show = { param($n) ([double]$n).ToString('0.######', [System.Globalization.CultureInfo]::InvariantCulture) }
        $lumaMedian = [double]$ExposureMedianLuma; $nearBlackMedian = [double]$ExposureMedianNearBlackFraction; $clippedMedian = [double]$ExposureMedianClippedFraction
        $notNumbers = @($lumaMedian, $nearBlackMedian, $clippedMedian | Where-Object { [double]::IsNaN($_) -or [double]::IsInfinity($_) })
        $broken = @(
            if ($notNumbers.Count -gt 0) { 'a median is not a finite number' }
            if ($lumaMedian -lt [double]$ExposureBand.minMedianLuma) { "median luma $(& $show $lumaMedian) is under the band minimum $(& $show $ExposureBand.minMedianLuma)" }
            if ($lumaMedian -gt [double]$ExposureBand.maxMedianLuma) { "median luma $(& $show $lumaMedian) is over the band maximum $(& $show $ExposureBand.maxMedianLuma)" }
            if ($nearBlackMedian -gt [double]$ExposureBand.maxNearBlackFraction) { "median near-black fraction $(& $show $nearBlackMedian) is over the band maximum $(& $show $ExposureBand.maxNearBlackFraction)" }
            if ($clippedMedian -gt [double]$ExposureBand.maxClippedFraction) { "median clipped fraction $(& $show $clippedMedian) is over the band maximum $(& $show $ExposureBand.maxClippedFraction)" })
        if ($broken.Count -gt 0) {
            $bandSource = $(if ($ExposureBandSource) { " [band: $ExposureBandSource]" } else { '' })
            $exposureReason = "the picture area is outside its exposure band over the $PlayFrames judged play frames (median luma $(& $show $lumaMedian)/255, near-black fraction $(& $show $nearBlackMedian), clipped fraction $(& $show $clippedMedian); band luma $(& $show $ExposureBand.minMedianLuma)..$(& $show $ExposureBand.maxMedianLuma), near-black <= $(& $show $ExposureBand.maxNearBlackFraction), clipped <= $(& $show $ExposureBand.maxClippedFraction))${bandSource}: $($broken -join '; ')"
            return [pscustomobject]@{ verdict = 'EXPOSURE_OUT_OF_RANGE'; counted = $true; reason = $(if ($waivedReason) { "$exposureReason; $waivedReason" } else { $exposureReason }) }
        }
    }
    [pscustomobject]@{ verdict = 'OK'; counted = $true; reason = $waivedReason }
}

function Get-DvFrameNumber {
    <#
    .SYNOPSIS
    A display frame number as the app writes it (a JSON integer, 0..Int32.MaxValue) -> [int]; anything else -> $null (pure, never throws).
    PLAYBACK-SOURCE-FRAME-IDENTITY-1 r5 (sol r3 key): [int] on -0.4 is 0 and on 1.5 is 2, on 2147483648 or an object it throws; none is a frame.
    #>
    param($Value)
    if ($null -eq $Value) { return $null }
    $integral = $Value -is [int] -or $Value -is [long] -or $Value -is [int16] -or $Value -is [byte] -or $Value -is [sbyte] -or
                $Value -is [uint16] -or $Value -is [uint32] -or $Value -is [uint64] -or $Value -is [System.Numerics.BigInteger]
    if (-not $integral) { return $null }
    if ($Value -lt 0 -or $Value -gt [int]::MaxValue) { return $null }
    [int]$Value
}

function Get-DvVisualSourceDifference {
    <#
    .SYNOPSIS
    Whether the source frames behind the JUDGED play frames differ (pure): known-differ | known-identical | unknown, from each frame's own `sourceId`
    (the app's per-frame source_id: exactly 16 lowercase hex digits; anything else is no id) and `displayFrame`. The app's rule
    (visual_capture_source_difference::decide, platform/qt/VisualCaptureSourceDifference.h), over the frames passed: unknown with fewer than two frames,
    any frame without an id or with a display frame below 0, or fewer than two distinct display frames; else known-differ when two ids differ, and
    known-identical when all are equal. PLAYBACK-SOURCE-FRAME-IDENTITY-1 r4 (acting-cos Dot B1): the caller passes the frames the picture is judged
    over, so the two numbers the frozen gate compares come from ONE population.
    #>
    param([object[]]$Frames = @())
    # PLAYBACK-SOURCE-FRAME-IDENTITY-1 r5 (sol r3 key on 27da457e): missing or invalid evidence is unknown, never known-identical and never a
    # throw. A $null frame is a judged frame with no evidence (it is not filtered away), and a display frame that is not a whole number in
    # 0..Int32.MaxValue (-0.4, 1.5, 2147483648, an object) is no display frame (Get-DvFrameNumber).
    $judged = @($Frames)
    if ($judged.Count -lt 2) { return 'unknown' }
    $ids = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::Ordinal)
    $displayFrames = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($f in $judged) {
        if ($null -eq $f) { return 'unknown' }
        $id = Get-DvProp $f 'sourceId'; $displayFrame = Get-DvFrameNumber (Get-DvProp $f 'displayFrame')
        if (-not ($id -is [string] -and $id -cmatch '^[0-9a-f]{16}\z')) { return 'unknown' }   # \z: a trailing newline is not the app's form either
        if ($null -eq $displayFrame) { return 'unknown' }
        [void]$ids.Add($id); [void]$displayFrames.Add($displayFrame)
    }
    if ($displayFrames.Count -lt 2) { return 'unknown' }
    if ($ids.Count -gt 1) { return 'known-differ' }
    'known-identical'
}

function New-DvVisualStrip {
    <#
    .SYNOPSIS
    One contact strip: the play frames in a row, then the paused-instant frame and its paused full-quality re-render, each tile the frame's
    video viewport, labelled with its kind and the frame the app says it showed; a header names the leg and the verdict. Returns the path.
    #>
    param([Parameter(Mandatory)][object[]]$Tiles, [Parameter(Mandatory)][string]$OutPath, [string]$Header = '', [int]$TileWidth = 480)
    Add-Type -AssemblyName System.Drawing
    $tileHeight = [int][math]::Round($TileWidth * 9 / 16)
    $labelHeight = 26; $headerHeight = 34
    $width = $TileWidth * [math]::Max(1, $Tiles.Count)
    $strip = [System.Drawing.Bitmap]::new($width, $headerHeight + $labelHeight + $tileHeight, [System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
    try {
        $g = [System.Drawing.Graphics]::FromImage($strip)
        $font = [System.Drawing.Font]::new('Arial', 12)
        $headFont = [System.Drawing.Font]::new('Arial', 14, [System.Drawing.FontStyle]::Bold)
        try {
            $g.Clear([System.Drawing.Color]::FromArgb(24, 24, 24))
            $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $g.DrawString($Header, $headFont, [System.Drawing.Brushes]::White, 8, 7)
            for ($i = 0; $i -lt $Tiles.Count; $i++) {
                $t = $Tiles[$i]
                $x = $i * $TileWidth
                $g.DrawString([string]$t.label, $font, [System.Drawing.Brushes]::Gainsboro, $x + 6, $headerHeight + 4)
                $dest = [System.Drawing.Rectangle]::new($x + 2, $headerHeight + $labelHeight, $TileWidth - 4, $tileHeight - 2)
                if ($t.path -and (Test-Path -LiteralPath $t.path -PathType Leaf)) {
                    $img = [System.Drawing.Bitmap]::new([string]$t.path)
                    try {
                        $rect = [System.Drawing.Rectangle]::new(0, 0, $img.Width, $img.Height)
                        $vp = $t.viewport
                        if ($null -ne $vp -and [int](Get-DvProp $vp 'w') -gt 0 -and [int](Get-DvProp $vp 'h') -gt 0) {
                            $rect = [System.Drawing.Rectangle]::Intersect($rect, [System.Drawing.Rectangle]::new([int](Get-DvProp $vp 'x'), [int](Get-DvProp $vp 'y'), [int](Get-DvProp $vp 'w'), [int](Get-DvProp $vp 'h')))
                        }
                        # letterbox the viewport into the tile, aspect kept
                        $scale = [math]::Min($dest.Width / [double]$rect.Width, $dest.Height / [double]$rect.Height)
                        $dw = [int]($rect.Width * $scale); $dh = [int]($rect.Height * $scale)
                        $fit = [System.Drawing.Rectangle]::new($dest.X + [int](($dest.Width - $dw) / 2), $dest.Y + [int](($dest.Height - $dh) / 2), $dw, $dh)
                        $g.DrawImage($img, $fit, $rect, [System.Drawing.GraphicsUnit]::Pixel)
                    } finally { $img.Dispose() }
                } else {
                    $g.DrawString('not captured', $font, [System.Drawing.Brushes]::OrangeRed, $x + 6, $headerHeight + $labelHeight + 6)
                }
            }
        } finally { $font.Dispose(); $headFont.Dispose(); $g.Dispose() }
        $strip.Save($OutPath, [System.Drawing.Imaging.ImageFormat]::Png)
    } finally { $strip.Dispose() }
    $OutPath
}

function Read-DvVisualCaptures {
    <#
    .SYNOPSIS
    The receipt's `visual` block for one leg. Copies the published captures (<artifacts>\visual) into <evidence>\visual under the leg's names
    (<legId>-play-<k>.png, <legId>-stop.png, <legId>-paused.png; the app's visual.json and the job's request record beside them), hashes every
    file, derives the content hashes and the play-vs-paused difference from the copies, composes <legId>-strip.png, and applies the gate.
    PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1: a build that binds its captures also keeps the unforced paused-instant capture (<legId>-stop-unbound.png,
    what a pre-binding build captured) and stamps the stop capture's label_bound; labelBound is $null for a build that predates it.
    PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2: such a build also takes unforced play captures (<legId>-play-unbound-<k>.png; r3: all of them after
    the forced ones, see below). When the app published any, the frozen check is judged on the UNFORCED play captures (frozenJudgedOn
    'play_unbound'): the force-paint shows the frame the label names even when the display's own paint never lands, so judging the forced
    ones would mask a frozen display. The forced set is reported beside it (forcedPlayDistinctContentHashes) and a forced set that moved over an
    unforced one that did not is said in the reason. (r5: no index pairing of forced k with unforced k is reported: under the r3 order they are
    half a tail apart, so on a healthy display every such pair disagrees.) The paused capture gets its unforced twin too
    (<legId>-paused-unbound.png; pausedUnboundVsPausedMeanAbsDiff). pausedLabelBound and labelBoundBasis /
    pausedLabelBoundBasis carry the paused capture's binding and where each painted serial came from (label_bound_basis:
    pixmap_item_by_construction holds by construction and proves only that the forced repaint ran).
    PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r3 (Dot 7dc103f1 B2): a forced paint stays on screen when the display's own paint is stuck, so it is what
    the NEXT unforced capture shows (r2's per-slice pairing let unforced k+1 show forced k: six moving pictures over a frozen display). Every
    capture records forced_paints_before (forcedPaintsBefore); the frozen check is judged only on the unforced play captures that record the SAME
    count as the first of them (no forced paint between any two). Those that record another count, or none (an r2 build), are not judged
    (observerPaintedPlayFrames) -- fewer than two left is VISUAL_ASSESSMENT_UNAVAILABLE. stopUnboundObserverClean says whether stop_unbound also
    follows them with no forced paint between ($null when unknown); repeatedPlayFrames counts judged unforced captures that repeat the picture before
    them while the app's label advanced (a display whose paint stopped part-way when the source frames are known to differ, a possible one when
    nothing records them; $null over source frames the app records as identical). r5: repeatedPlayFramesNote carries the repeat note whatever
    reason was set first (reason keeps its first-wins rule), and two or more repeats over known-differ source frames are PARTIAL_FREEZE.
    r7 (adversary r6 pass 3 A1): each repeat is judged by its own pair's source ids; repeatedPlayFramesSourceDiffer counts those whose ids differ,
    and only it reaches the gate's -RepeatedPlayFrames. A pair with equal ids is not a repeat; one with a missing id is counted, never decisive.
    Never throws for a missing or broken capture: the block says what is missing (a capture is evidence about the leg, not the leg itself).
    #>
    param([bool]$Requested, [string]$OptOutReason, [string]$ArtifactsDir, [string]$EvidenceDir, [Parameter(Mandatory)][string]$LegId, [string]$Header = '',
          [string]$ClipId = '', [string]$ExposureBandPath = $script:ExposureBandsPath)
    $block = [ordered]@{
        schema = $script:VisualSchema; requested = $Requested; optOutReason = $(if ($Requested) { $null } else { $OptOutReason })
        method = $null; forwarded = $null; probe = $null; probeReason = $null; captured = $false
        frames = @(); playFrames = 0; savedPlayFrames = 0; distinctContentHashes = 0; appDistinctDisplayFrames = 0; flatPlayFrames = 0
        sourceDifference = 'unknown'; viewportUnavailablePlayFrames = 0
        appSourceDifference = $null; sourceDifferenceCrossCheck = $null
        stopFrame = $null; pausedSettled = $null; pausedFrame = $null; samePosition = $false
        playVsPausedMeanAbsDiff = $null; playVsPausedMeanAbsDiffPerChannel = $null; divergenceThreshold = $script:VisualDivergenceThreshold
        labelBound = $null; stopUnboundVsPausedMeanAbsDiff = $null; stopUnboundVsStopMeanAbsDiff = $null
        pausedLabelBound = $null; labelBoundBasis = $null; pausedLabelBoundBasis = $null; pausedUnboundVsPausedMeanAbsDiff = $null
        frozenJudgedOn = 'play'; forcedPlayDistinctContentHashes = $null
        observerPaintedPlayFrames = $null; stopUnboundObserverClean = $null; repeatedPlayFrames = $null; repeatedPlayFramesSourceDiffer = $null; repeatedPlayFramesNote = $null
        tailBeginUtc = $null; tailEndUtc = $null; presentMonWindowEndUtc = $null
        exposureFrames = 0; exposureMedianLuma = $null; exposureMedianNearBlackFraction = $null; exposureMedianClippedFraction = $null
        exposureBand = $null; exposureBandSource = $null; exposureBandNote = $null
        stripPath = $null; stripSha256 = $null; stripCopyPath = $null
        verdict = $null; counted = $true; reason = $null; appError = $null
    }
    $notJudgedNote = $null   # set once the frames are read; kept after a verdict reason rather than replaced by it
    $crossCheckNote = $null  # set when the app's source_difference is not what the judged frames' source ids show; kept the same way
    $maskNote = $null        # set when the forced play captures moved over unforced ones that did not; kept the same way
    $observerNote = $null    # set when some unforced play captures followed a forced paint and were not judged; kept the same way
    $repeatNote = $null      # set when judged unforced captures repeat their predecessor's picture while the label advanced; kept the same way
    $stripNote = $null      # set when the strip cannot be composed; kept the same way, after the not-judged note
    $reasonBeforeStrip = $null   # the step reason standing when the strip failed (a NOT_CAPTURED leg keeps it between the capture count and the strip failure)
    $exposureNote = $null    # set when the exposure band file could not be used (missing / malformed): reported beside the verdict, never silently OK
    # the exposure inputs of the verdict, read from the block when the verdict is taken (all $null until the judged frames are measured)
    $exposureArgs = { @{ ExposureMedianLuma = $block.exposureMedianLuma; ExposureMedianNearBlackFraction = $block.exposureMedianNearBlackFraction
                         ExposureMedianClippedFraction = $block.exposureMedianClippedFraction; ExposureBand = $block.exposureBand; ExposureBandSource = [string]$block.exposureBandSource } }
    $finish = {
        $exArgs = & $exposureArgs
        $v = Get-DvVisualVerdict -Requested $Requested -Captured ([bool]$block.captured) -PlayFrames ([int]$block.playFrames) -DistinctContentHashes ([int]$block.distinctContentHashes) `
            -AppDistinctDisplayFrames ([int]$block.appDistinctDisplayFrames) -FlatPlayFrames ([int]$block.flatPlayFrames) -PlayVsPausedMeanAbsDiff $block.playVsPausedMeanAbsDiff -SamePosition ([bool]$block.samePosition) `
            -SourceDifference ([string]$block.sourceDifference) -ViewportUnavailablePlayFrames ([int]$block.viewportUnavailablePlayFrames) -LabelBound $block.labelBound `
            -PausedLabelBound $block.pausedLabelBound -ObserverPaintedPlayFrames ([int]$block.observerPaintedPlayFrames) -RepeatedPlayFrames ([int]$block.repeatedPlayFramesSourceDiffer) @exArgs
        # a gate reason replaces a step's reason, except that a NOT_CAPTURED leg keeps the step that says WHY nothing was captured
        # (the verdict reason comes first, then the notes that some saved play frames were not judged, then the masking and repeat notes, then the strip failure;
        # a PARTIAL_FREEZE reason already says what the repeat note says, so the note stays in repeatedPlayFramesNote only)
        # PLAYBACK-SOURCE-FRAME-IDENTITY-1: the source-difference cross-check note follows the not-judged note.
        $notes = (@($notJudgedNote, $crossCheckNote, $observerNote, $maskNote, $(if ($v.verdict -cne 'PARTIAL_FREEZE') { $repeatNote }), $exposureNote, $stripNote) | Where-Object { $_ }) -join '; '
        # a NOT_CAPTURED leg whose strip also failed keeps both: the capture count, any step reason from before the strip, then the strip failure
        if ($v.verdict -ceq 'NOT_CAPTURED' -and $stripNote) { $block.reason = (@($v.reason, $reasonBeforeStrip, $stripNote) | Where-Object { $_ }) -join '; ' }
        elseif ($null -ne $v.reason -and -not ($v.verdict -ceq 'NOT_CAPTURED' -and $null -ne $block.reason)) { $block.reason = $(if ($notes) { "$($v.reason); $notes" } else { $v.reason }) }
        elseif ($null -eq $v.reason -and $crossCheckNote -and (-not $block.reason -or $block.reason -ceq $notJudgedNote)) { $block.reason = (@($notJudgedNote, $crossCheckNote) | Where-Object { $_ }) -join '; ' }   # no gate reason: the cross-check note stands beside the not-judged note
        # an unusable exposure band file is always said in the reason, beside whatever note or verdict reason is already there (the first-wins rule above would drop it)
        if ($exposureNote -and -not ([string]$block.reason).Contains($exposureNote)) { $block.reason = (@($block.reason, $exposureNote) | Where-Object { $_ }) -join '; ' }
        $block.verdict = $v.verdict; $block.counted = $v.counted
        $block
    }
    if (-not $Requested) { return (& $finish) }
    $share = $(if ($ArtifactsDir) { Join-Path $ArtifactsDir 'visual' } else { $null })
    if (-not $share -or -not (Test-Path -LiteralPath $share -PathType Container)) { $block.reason = 'the job published no visual directory'; return (& $finish) }
    $dir = Join-Path $EvidenceDir 'visual'
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $requestPath = Join-Path $share 'visual-capture-request.json'
    if (Test-Path -LiteralPath $requestPath -PathType Leaf) {
        Copy-Item -LiteralPath $requestPath -Destination (Join-Path $dir 'request.json') -Force
        try {
            $request = [IO.File]::ReadAllText((Join-Path $dir 'request.json')) | ConvertFrom-Json -ErrorAction Stop
            $block.forwarded = [bool](Get-DvProp $request 'forwarded'); $block.probe = [string](Get-DvProp $request 'probe'); $block.probeReason = Get-DvProp $request 'reason'
            $block.presentMonWindowEndUtc = ConvertTo-DvVisualUtcText (Get-DvProp $request 'presentMonWindowEndUtc')
        } catch { $block.reason = 'the job''s visual-capture request record is not valid JSON' }
    }
    $appJsonShare = Join-Path $share 'visual.json'
    if (-not (Test-Path -LiteralPath $appJsonShare -PathType Leaf)) {
        if (-not $block.reason) { $block.reason = $(if ($block.forwarded -eq $false) { "the capture was not forwarded to the app (probe $($block.probe): $($block.probeReason))" } else { 'the app wrote no visual.json' }) }
        return (& $finish)
    }
    Copy-Item -LiteralPath $appJsonShare -Destination (Join-Path $dir 'app-visual.json') -Force
    try { $app = [IO.File]::ReadAllText((Join-Path $dir 'app-visual.json')) | ConvertFrom-Json -ErrorAction Stop } catch { $block.reason = 'the app''s visual.json is not valid JSON'; return (& $finish) }
    $block.method = [string](Get-DvProp $app 'method'); $block.appError = [string](Get-DvProp $app 'error')
    # Whether the source frames differ: only the app's own records say so -- never a guess. Its top-level source_difference (differ | identical, exactly that
    # case: `identical` waives the gate, so `IDENTICAL` is not the app's word; anything else, or nothing, is unknown) covers EVERY play capture, a usable
    # picture or not, so it is kept as written and only CROSS-CHECKS the decision (PLAYBACK-SOURCE-FRAME-IDENTITY-1 r4, acting-cos Dot B1). The decision
    # is taken below from the judged frames' own source_id (Get-DvVisualSourceDifference).
    $block.appSourceDifference = Get-DvProp $app 'source_difference'
    $appSourceDifference = $(switch -CaseSensitive ([string]$block.appSourceDifference) { 'differ' { 'known-differ' } 'identical' { 'known-identical' } default { 'unknown' } })
    $block.tailBeginUtc = ConvertTo-DvVisualUtcText (Get-DvProp $app 'tail_begin_utc'); $block.tailEndUtc = ConvertTo-DvVisualUtcText (Get-DvProp $app 'tail_end_utc')
    $frames = [System.Collections.Generic.List[object]]::new()
    $pixels = @{}
    $appFrames = Get-DvProp $app 'frames'   # (assigned, then @()-wrapped: see Read-DvContactFrames)
    foreach ($f in @($appFrames)) {
        if ($null -eq $f) { continue }
        $kind = [string](Get-DvProp $f 'kind'); $index = [int](Get-DvProp $f 'index'); $file = [string](Get-DvProp $f 'file')
        if ($kind -cnotin @('play', 'play_unbound', 'stop_unbound', 'stop', 'paused_unbound', 'paused') -or $file -cnotmatch '^(play-\d+|play-unbound-\d+|stop-unbound|stop|paused-unbound|paused)\.png$') { continue }
        $isPlayKind = $kind -cin @('play', 'play_unbound')
        $legName = $(if ($isPlayKind) { "$LegId-$($kind.Replace('_', '-'))-$index.png" } else { "$LegId-$($kind.Replace('_', '-')).png" })
        $entry = [ordered]@{ kind = $kind; index = $index; file = $legName; sha256 = $null; contentHash = $null; viewportMean = $null; viewportStd = $null; viewportAvailable = $true
                             exposureLumaMean = $null; exposureNearBlackFraction = $null; exposureClippedFraction = $null; exposureLetterboxRowsDropped = $null
                             displayFrame = Get-DvProp $f 'display_frame'; playActive = Get-DvProp $f 'play_active'; tailElapsedMs = Get-DvProp $f 'tail_elapsed_ms'
                             captureMs = Get-DvProp $f 'capture_ms'; viewport = Get-DvProp $f 'viewport'; saved = [bool](Get-DvProp $f 'saved'); appReason = Get-DvProp $f 'reason'
                             serial = Get-DvProp $f 'serial'; paintedSerial = Get-DvProp $f 'painted_serial'; paintedSerialValid = Get-DvProp $f 'painted_serial_valid'
                             labelBound = Get-DvProp $f 'label_bound'; labelBoundBasis = Get-DvProp $f 'label_bound_basis'; forcedPaintsBefore = Get-DvProp $f 'forced_paints_before'
                             sourceId = Get-DvProp $f 'source_id' }
        $src = Join-Path $share $file
        if ($entry.saved -and (Test-Path -LiteralPath $src -PathType Leaf)) {
            $local = Join-Path $dir $legName
            Copy-Item -LiteralPath $src -Destination $local -Force
            $entry.sha256 = Get-DvSha256OfFile $local
            try {
                $hashPx = Get-DvVisualViewportPixels -Path $local -Viewport $entry.viewport -Width $script:VisualHashSize[0] -Height $script:VisualHashSize[1] -RequireViewport
                if ($null -eq $hashPx) { $entry.viewportAvailable = $false }   # no usable viewport rectangle: the whole window is never hashed in its place
                else {
                    $entry.contentHash = $hashPx.contentHash; $entry.viewportMean = $hashPx.mean; $entry.viewportStd = $hashPx.std
                    # VISUAL-GATE-EXPOSURE-FLOOR-1: the picture-area exposure of the same viewport pixels the picture is hashed on
                    $exposure = Get-DvVisualExposureStats -Rgb $hashPx.rgb -Width $script:VisualHashSize[0] -Height $script:VisualHashSize[1]
                    if ($null -ne $exposure) {
                        $entry.exposureLumaMean = $exposure.lumaMean; $entry.exposureNearBlackFraction = $exposure.nearBlackFraction
                        $entry.exposureClippedFraction = $exposure.clippedFraction; $entry.exposureLetterboxRowsDropped = $exposure.letterboxRowsDropped
                    }
                    if (-not $isPlayKind) { $pixels[$kind] = (Get-DvVisualViewportPixels -Path $local -Viewport $entry.viewport -Width $script:VisualDiffSize[0] -Height $script:VisualDiffSize[1] -RequireViewport).rgb }
                }
            } catch { $entry.appReason = "the runner could not read the image: $($_.Exception.GetType().Name)" }
        }
        if ($kind -ceq 'paused') {
            $block.pausedSettled = Get-DvProp $f 'settled'; $block.pausedFrame = Get-DvProp $f 'settled_frame'
            if ($null -ne $entry.labelBound) { $block.pausedLabelBound = [bool]$entry.labelBound }
            $block.pausedLabelBoundBasis = $entry.labelBoundBasis
        }
        if ($kind -ceq 'stop') {
            $block.stopFrame = Get-DvProp $f 'display_frame'
            if ($null -ne $entry.labelBound) { $block.labelBound = [bool]$entry.labelBound }
            $block.labelBoundBasis = $entry.labelBoundBasis
        }
        $frames.Add([pscustomobject]$entry)
    }
    $block.frames = $frames.ToArray()
    # r2: the frozen check is judged on the UNFORCED play captures whenever the app published any (a binding build); a build without them is judged
    # on its play captures as before. The choice follows what the app NAMED, never what could be read: an unreadable unforced set is not swapped
    # for the forced one (that swap is exactly the masking).
    $judgeKind = $(if (@($frames | Where-Object { $_.kind -ceq 'play_unbound' }).Count -gt 0) { 'play_unbound' } else { 'play' })
    $block.frozenJudgedOn = $judgeKind
    # r3 (Dot 7dc103f1 B2): of the unforced play captures, only those no forced paint reached are judged -- the ones recording the same
    # forced_paints_before as the first of them. One recording another count, or none (an r2 build), may show a forced paint, not the display's own.
    $isOrdinal = { param($v) ($v -is [int] -or $v -is [long]) -and $v -ge 0 }
    $pool = @($frames | Where-Object { $_.kind -ceq $judgeKind })
    $baseOrdinal = $null
    if ($judgeKind -ceq 'play_unbound') {
        $first = @($pool | Sort-Object index) | Select-Object -First 1
        if ($null -ne $first -and (& $isOrdinal $first.forcedPaintsBefore)) { $baseOrdinal = [long]$first.forcedPaintsBefore }
        $clean = @($pool | Where-Object { $null -ne $baseOrdinal -and (& $isOrdinal $_.forcedPaintsBefore) -and [long]$_.forcedPaintsBefore -eq $baseOrdinal })
        $block.observerPaintedPlayFrames = $pool.Count - $clean.Count
        $pool = $clean
        $stopUnbound = @($frames | Where-Object { $_.kind -ceq 'stop_unbound' }) | Select-Object -First 1
        if ($null -ne $stopUnbound -and $null -ne $baseOrdinal -and (& $isOrdinal $stopUnbound.forcedPaintsBefore)) {
            $block.stopUnboundObserverClean = ([long]$stopUnbound.forcedPaintsBefore -eq $baseOrdinal)
        }
    }
    # `playFrames` counts the JUDGED play frames (those with a usable viewport); `savedPlayFrames` the play PNGs the app saved and the runner copied;
    # `captured` is true when any was saved, so a leg whose every viewport is missing still says six pictures were captured and none judged.
    $play = @($pool | Where-Object { $_.contentHash })
    $block.playFrames = $play.Count
    $block.savedPlayFrames = @($frames | Where-Object { $_.kind -ceq $judgeKind -and $_.sha256 }).Count
    $block.captured = ($block.savedPlayFrames -gt 0)
    $block.viewportUnavailablePlayFrames = @($pool | Where-Object { $_.sha256 -and -not $_.viewportAvailable }).Count
    if ([int]$block.observerPaintedPlayFrames -gt 0 -and $play.Count -ge 2) {
        $observerNote = "$($block.observerPaintedPlayFrames) unforced play frame(s) followed a forced paint the others did not and were not judged; the other $($play.Count) were"
        if (-not $block.reason) { $block.reason = $observerNote }
    }
    if ($judgeKind -ceq 'play_unbound') {
        # the forced twins, reported beside the judged set: if forced and unforced disagree, the receipt says so rather than hiding it
        $forced = @($frames | Where-Object { $_.kind -ceq 'play' -and $_.contentHash })
        $block.forcedPlayDistinctContentHashes = @($forced | ForEach-Object { $_.contentHash } | Sort-Object -Unique).Count
    }
    if ($block.viewportUnavailablePlayFrames -gt 0 -and $play.Count -ge 2) {
        $notJudgedNote = "$($block.viewportUnavailablePlayFrames) saved play frame(s) have no usable viewport rectangle and were not judged; the other $($play.Count) were"
        if (-not $block.reason) { $block.reason = $notJudgedNote }
    }
    # The source difference is judged over the SAME frames as the picture ($play: the play frames with a content hash), so FROZEN_DISPLAY fires only
    # when the judged frames themselves show differing source ids under one picture (acting-cos Dot B1 on PR #477). A disagreement with the app's
    # all-captures scalar is recorded (sourceDifferenceCrossCheck, and a note in the reason); it never decides.
    $block.sourceDifference = Get-DvVisualSourceDifference -Frames $play
    $block.sourceDifferenceCrossCheck = $(if ($appSourceDifference -ceq $block.sourceDifference) { 'agree' } else { 'disagree' })
    if ($block.sourceDifferenceCrossCheck -ceq 'disagree') {
        $appWord = $(if ($null -eq $block.appSourceDifference) { 'no source_difference' } else { "source_difference '$($block.appSourceDifference)'" })
        $crossCheckNote = "the app's $appWord (over every play capture) is not the $($block.sourceDifference) the $($play.Count) judged frames' source ids show; the judged frames decide"
    }
    $block.distinctContentHashes = @($play | ForEach-Object { $_.contentHash } | Sort-Object -Unique).Count
    # r5 (sol r3 key class sweep): the same display frames reach here; a value that is not a frame number (Get-DvFrameNumber) is not counted, never a throw
    $block.appDistinctDisplayFrames = @($play | ForEach-Object { Get-DvFrameNumber $_.displayFrame } | Where-Object { $null -ne $_ } | Sort-Object -Unique).Count
    $block.flatPlayFrames = @($play | Where-Object { $null -ne $_.viewportStd -and [double]$_.viewportStd -lt $script:VisualFlatViewportStd }).Count
    # VISUAL-GATE-EXPOSURE-FLOOR-1: the leg's exposure is the MEDIAN over the judged play frames (the same population as the picture), judged against the
    # clip's band (exposure-bands.json) or the absolute fallback. An unusable band file is reported (exposureBandNote, and the reason), never a silent OK.
    $measured = @($play | Where-Object { $null -ne $_.exposureLumaMean })
    if ($measured.Count -gt 0) {
        $block.exposureFrames = $measured.Count
        # (unrounded: the verdict compares these to the band's caps; the reason text rounds for display)
        $block.exposureMedianLuma = Get-DvExposureMedian ([double[]]@($measured | ForEach-Object { [double]$_.exposureLumaMean }))
        $block.exposureMedianNearBlackFraction = Get-DvExposureMedian ([double[]]@($measured | ForEach-Object { [double]$_.exposureNearBlackFraction }))
        $block.exposureMedianClippedFraction = Get-DvExposureMedian ([double[]]@($measured | ForEach-Object { [double]$_.exposureClippedFraction }))
        $bandInfo = Get-DvExposureBand -ClipId $ClipId -Path $ExposureBandPath
        $block.exposureBand = $bandInfo.band; $block.exposureBandSource = $bandInfo.source; $block.exposureBandNote = $bandInfo.note
        # (never pre-set as block.reason, r2 fable H2: that displaced a NOT_CAPTURED verdict's own reason; the finish clause appends it to every reason)
        if ($bandInfo.note) { $exposureNote = $bandInfo.note }
    }
    if ($judgeKind -ceq 'play_unbound' -and $block.distinctContentHashes -eq 1 -and [int]$block.forcedPlayDistinctContentHashes -gt 1) {
        $maskNote = "the forced play captures show $($block.forcedPlayDistinctContentHashes) distinct pictures where the unforced ones show one: the force-paint would have masked this"
        if (-not $block.reason) { $block.reason = $maskNote }
    }
    if ($judgeKind -ceq 'play_unbound' -and $block.sourceDifference -cne 'known-identical') {
        # a display whose own paint stopped part-way: a judged unforced picture equal to the one before it while the app's label advanced
        $ordered = @($play | Sort-Object index)
        # r7 (adversary r6 pass 3 A1): each repeat is judged by ITS OWN pair's source ids (Get-DvVisualSourceDifference over the two frames), never by
        # the set-wide result: ids [aa,bb,bb,bb,bb,bb] under pictures [X,Y,Y,Y,Y,Y] are four repeats of the source's own picture, not a stopped paint.
        # A pair whose ids are equal is no repeat; a pair whose ids differ is a display repeat (repeatedPlayFramesSourceDiffer, the only count that can
        # make PARTIAL_FREEZE); a pair with a missing or malformed id is UNKNOWN: it stays in repeatedPlayFrames and its note, never in the verdict.
        $sourceDifferRepeats = 0
        $block.repeatedPlayFrames = @(for ($i = 1; $i -lt $ordered.Count; $i++) {
            $a = $ordered[$i - 1]; $b = $ordered[$i]
            $aFrame = Get-DvFrameNumber $a.displayFrame; $bFrame = Get-DvFrameNumber $b.displayFrame   # r5: no frame number, no advance (never a throw)
            if ($b.contentHash -ceq $a.contentHash -and $null -ne $aFrame -and $null -ne $bFrame -and $bFrame -ne $aFrame) {
                $pairSource = Get-DvVisualSourceDifference -Frames @($a, $b)
                if ($pairSource -cne 'known-identical') {
                    if ($pairSource -ceq 'known-differ') { $sourceDifferRepeats++ }
                    $b.index
                }
            }
        }).Count
        $block.repeatedPlayFramesSourceDiffer = $sourceDifferRepeats
        if ($block.repeatedPlayFrames -gt 0 -and $block.distinctContentHashes -gt 1) {
            # the cause is named only when every repeat's own pair is known to differ; over unknown source content a repeat may be the source's own (Dot a5acb3c8)
            $repeatNote = "$($block.repeatedPlayFrames) of the $($play.Count) unforced play captures repeat the picture before them while the app's label advanced: " + $(
                if ($block.sourceDifference -ceq 'known-differ' -and $sourceDifferRepeats -eq $block.repeatedPlayFrames) { "the display's own paint stopped landing for part of the tail" }
                elseif ($sourceDifferRepeats -gt 0) { "$sourceDifferRepeats of them over pairs whose own source ids differ, but a judged frame has no usable source id, so this is a possible partial freeze or repeated source pictures, not a proven cause" }
                else { "nothing records whether the source frames differ, so this is a possible partial freeze or repeated source pictures, not a proven cause" })
            $block.repeatedPlayFramesNote = $repeatNote   # its own field: an earlier note in reason never hides it (r5, Grok G4)
            if (-not $block.reason) { $block.reason = $repeatNote }
        }
    }
    $block.samePosition = ($null -ne $block.stopFrame -and $null -ne $block.pausedFrame -and [int]$block.stopFrame -ge 0 -and [int]$block.stopFrame -eq [int]$block.pausedFrame -and $block.pausedSettled -eq $true)
    if ($pixels.ContainsKey('stop') -and $pixels.ContainsKey('paused')) {
        $mad = Get-DvVisualMeanAbsDiff -A $pixels['stop'] -B $pixels['paused']
        $block.playVsPausedMeanAbsDiffPerChannel = $mad
        $block.playVsPausedMeanAbsDiff = ($mad | Measure-Object -Maximum).Maximum
    }
    if ($pixels.ContainsKey('stop_unbound') -and $pixels.ContainsKey('paused')) {
        $block.stopUnboundVsPausedMeanAbsDiff = (Get-DvVisualMeanAbsDiff -A $pixels['stop_unbound'] -B $pixels['paused'] | Measure-Object -Maximum).Maximum
    }
    if ($pixels.ContainsKey('stop_unbound') -and $pixels.ContainsKey('stop')) {
        $block.stopUnboundVsStopMeanAbsDiff = (Get-DvVisualMeanAbsDiff -A $pixels['stop_unbound'] -B $pixels['stop'] | Measure-Object -Maximum).Maximum
    }
    if ($pixels.ContainsKey('paused_unbound') -and $pixels.ContainsKey('paused')) {
        $block.pausedUnboundVsPausedMeanAbsDiff = (Get-DvVisualMeanAbsDiff -A $pixels['paused_unbound'] -B $pixels['paused'] | Measure-Object -Maximum).Maximum
    }
    $tiles = @($frames | Sort-Object @{ Expression = { @{ play_unbound = 0; play = 1; stop_unbound = 2; stop = 3; paused_unbound = 4; paused = 5 }[$_.kind] } }, index | ForEach-Object {
        $label = $(if ($_.kind -ceq 'play') { "play $($_.index)  f=$($_.displayFrame)" } elseif ($_.kind -ceq 'play_unbound') { "play $($_.index), unforced  f=$($_.displayFrame)" }
                   elseif ($_.kind -ceq 'stop_unbound') { "paused instant, unforced  f=$($_.displayFrame)" }
                   elseif ($_.kind -ceq 'paused_unbound') { "paused full quality, unforced  f=$($block.pausedFrame)" }
                   elseif ($_.kind -ceq 'stop') { "paused instant  f=$($_.displayFrame)$(if ($null -ne $_.labelBound) { "  bound=$([bool]$_.labelBound)" })" } else { "paused full quality  f=$($block.pausedFrame)" })
        [pscustomobject]@{ path = $(if ($_.sha256) { Join-Path $dir $_.file } else { $null }); viewport = $_.viewport; label = $label } })
    if ($tiles.Count -gt 0) {
        $exArgs = & $exposureArgs
        $v = Get-DvVisualVerdict -Requested $true -Captured ([bool]$block.captured) -PlayFrames ([int]$block.playFrames) -DistinctContentHashes ([int]$block.distinctContentHashes) `
            -AppDistinctDisplayFrames ([int]$block.appDistinctDisplayFrames) -FlatPlayFrames ([int]$block.flatPlayFrames) -PlayVsPausedMeanAbsDiff $block.playVsPausedMeanAbsDiff -SamePosition ([bool]$block.samePosition) `
            -SourceDifference ([string]$block.sourceDifference) -ViewportUnavailablePlayFrames ([int]$block.viewportUnavailablePlayFrames) -LabelBound $block.labelBound `
            -PausedLabelBound $block.pausedLabelBound -ObserverPaintedPlayFrames ([int]$block.observerPaintedPlayFrames) -RepeatedPlayFrames ([int]$block.repeatedPlayFramesSourceDiffer) @exArgs
        $head ="$Header  visual=$($v.verdict)  distinct=$($block.distinctContentHashes)/$($block.playFrames)  play-vs-paused=$($block.playVsPausedMeanAbsDiff)$(if ($null -ne $block.exposureMedianLuma) { "  luma=$([math]::Round([double]$block.exposureMedianLuma, 2))  near-black=$([math]::Round([double]$block.exposureMedianNearBlackFraction, 4))  clipped=$([math]::Round([double]$block.exposureMedianClippedFraction, 4))" })"
        try {
            $block.stripPath = New-DvVisualStrip -Tiles $tiles -OutPath (Join-Path $dir "$LegId-strip.png") -Header $head.Trim()
            $block.stripSha256 = Get-DvSha256OfFile $block.stripPath
        } catch {
            $stripNote = "the strip could not be composed: $($_.Exception.GetType().Name)"
            $reasonBeforeStrip = $block.reason
            $block.reason = $(if ($block.reason) { "$($block.reason); $stripNote" } else { $stripNote })   # a plain OK keeps the not-judged note beside it
        }
    }
    & $finish
}

function Test-DvReceiptValid {
    <#
    .SYNOPSIS
    Is this receipt valid EVIDENCE? In PRODUCTION never: the best it can be is ADVISORY (see below). Every claim a PASS/FAIL receipt makes is RE-DERIVED from a committed blob or a hashed artifact; no
    field the receipt asserts about itself is an input. Returns [pscustomobject]@{ valid; status; reasons; unbound } where status is
    ADVISORY (a PRODUCTION receipt whose every claim re-derives: valid=false, reason VENUE_ANCHOR_ABSENT -- no venue-held anchor exists, so a
    production receipt is never VERIFIED and is never a usable PASS), VERIFIED_OFFLINE_TEST (offline harness only), NO_SIGNAL (any other
    outcome: carries no signal, needs no proof), INCOMPLETE (a piece of evidence is absent: not valid, never a PASS/FAIL) or INVALID (a claim
    does not re-derive). `unbound` lists the claims no hashed artifact carries (typed UNBOUND): they are never verdict inputs.

    PRODUCTION admission (admission.mode = production; -RepoRoot required):
      * admission.consentBlobSha / venueTableBlobSha must be real BLOBS of the repo ($RepoRoot) and be the files committed at
        admission.headCommit; the venue table parses; the consent blob parses with Read-DvClipConsent and holds a record for THIS
        venue + clip id whose line sha256 equals admission.ownerLineSha256 and whose recordedBy is 'owner'; the committed
        cleanup switch is on; the venue's role is the table's;
      * the leg spec named by subject.legSpecSha256 is a blob COMMITTED under tools/profiling/dual-venue/legs/ at headCommit, and
        the outcome (PASS vs FAIL) is re-derived from the evidence's job result, exit code, verbatim metrics and that spec's criteria.
    EVIDENCE (every mode that can be evidence): evidence.summaryJsonSha256 / evidenceManifestSha256 / resultJsonSha256 / logSha256 /
    umRunJsonSha256 are re-hashed from the local evidence directory (-EvidenceDir, else evidence.localEvidenceDir); the whole playback block
    (source frames, native / pace fps, override, wrap, nonce expected / observed / manifest, log binding, settings isolation, clip,
    rehearsal) is re-derived from those files by Get-DvPlaybackEvidence -- the writer's own parser -- and the receipt's copy must equal it;
    the verbatim metrics must equal summary.json's; the evidence's own clip id, venue and build must be the receipt's.
    A receipt written in offline test mode is never evidence (OFFLINE_TEST_RECEIPT) unless -AllowOfflineTestMode, which only the
    writer's own offline test path and the test harness pass; that mode still re-derives the evidence but has no committed consent to read.
    #>
    # -OfflineLegSpecBytes (r8): offline test mode only -- the leg spec bytes the runner ran, so a paired-seek block is checked against a spec contract there
    # too (bound to subject.legSpecSha256); production ignores it and reads the committed spec.
    param([Parameter(Mandatory)]$Receipt, [string]$RepoRoot = '', [string]$EvidenceDir = '', [switch]$AllowOfflineTestMode, [byte[]]$OfflineLegSpecBytes = $null)
    $incomplete = [System.Collections.Generic.List[string]]::new()
    $invalid = [System.Collections.Generic.List[string]]::new()
    $outcome = [string](Get-DvProp $Receipt 'outcome')
    # Case-sensitive on purpose: 'pass' / 'PASS ' is not an outcome the writer can produce, so it is neither a signal nor "no signal".
    if ($outcome -cnotin $script:OutcomeEnum) { return [pscustomobject]@{ valid = $false; status = 'INVALID'; reasons = @('OUTCOME_UNKNOWN: the receipt outcome is not one of the typed outcomes'); unbound = @() } }
    if ($outcome -cnotin @('PASS', 'FAIL')) { return [pscustomobject]@{ valid = $true; status = 'NO_SIGNAL'; reasons = @(); unbound = @() } }
    # Claims the receipt makes that NO hashed artifact carries (fable r1 B1 / sol r1 B3). They are recorded, typed, and never used as a verdict input.
    $unbound = @('CLIP_CONTENT_UNBOUND: subject.clipContentSha256 is computed by the local generator and written to no artifact the venue returns; it is recorded, never trusted',
                 'LEG_IDENTITY_UNBOUND: legId / legSpecSha256 name a committed spec, but no hashed artifact carries them; only the leg TYPE (summary.lookLeg), look flavor, backend, venue, clip and build are bound')

    $subject = Get-DvProp $Receipt 'subject'
    $clipId = [string](Get-DvProp $subject 'clipId')
    $venueName = [string](Get-DvProp (Get-DvProp $Receipt 'venue') 'name')
    $card = [string](Get-DvProp $Receipt 'card')
    $legId = [string](Get-DvProp $Receipt 'legId')
    $backend = [string](Get-DvProp $subject 'backend')
    if ([string]::IsNullOrWhiteSpace($clipId)) { $invalid.Add('subject.clipId absent') }
    elseif ($clipId -cnotmatch $script:ClipIdPattern) { $invalid.Add('subject.clipId is not a consented clip id') }
    # (subject.clipContentSha256 is NOT checked: it is UNBOUND -- see $unbound. It still enters the subject digest below, which only proves the receipt agrees with itself.)
    if ([string]::IsNullOrWhiteSpace($venueName)) { $invalid.Add('venue.name absent') }
    $digest = Get-DvSubjectDigest -BuildManifestSha256 (Get-DvProp $subject 'buildManifestSha256') -LegSpecSha256 (Get-DvProp $subject 'legSpecSha256') -ClipId (Get-DvProp $subject 'clipId') `
        -ClipContentSha256 (Get-DvProp $subject 'clipContentSha256') -Backend (Get-DvProp $subject 'backend') -LookFlavor (Get-DvProp $subject 'lookFlavor')
    if ([string](Get-DvProp $subject 'digest') -cne $digest) { $invalid.Add('SUBJECT_DIGEST_MISMATCH: subject.digest is not the sha256 of the subject fields it carries') }

    # ---- admission: re-derived from COMMITTED blobs ----------------------------------------------------------------------------
    $admission = Get-DvProp $Receipt 'admission'
    $mode = [string](Get-DvProp $admission 'mode')
    $table = $null; $spec = $null; $production = $false
    if ($mode -ceq 'production') {
        $production = $true
        $consentSha = [string](Get-DvProp $admission 'consentBlobSha'); $tableSha = [string](Get-DvProp $admission 'venueTableBlobSha')
        $headCommit = [string](Get-DvProp $admission 'headCommit'); $lineSha = [string](Get-DvProp $admission 'ownerLineSha256')
        $formatOk = $true
        foreach ($pair in @(@('consentBlobSha', $consentSha, '^[0-9a-f]{40}$'), @('venueTableBlobSha', $tableSha, '^[0-9a-f]{40}$'), @('headCommit', $headCommit, '^[0-9a-f]{40}$'), @('ownerLineSha256', $lineSha, '^[0-9a-f]{64}$'))) {
            if ($pair[1] -cnotmatch $pair[2]) { $invalid.Add("ADMISSION_UNPROVEN: admission.$($pair[0]) is absent or malformed"); $formatOk = $false }
        }
        if ($formatOk -and [string]::IsNullOrWhiteSpace($RepoRoot)) {
            $incomplete.Add('ADMISSION_UNVERIFIABLE: no -RepoRoot was given, so the consent and venue-table blobs cannot be read; a production receipt is not verified by its hash formats')
        } elseif ($formatOk) {
            $tb = Get-DvBlobById -RepoRoot $RepoRoot -BlobSha $tableSha
            if (-not $tb.ok) { $invalid.Add("ADMISSION_UNPROVEN: admission.venueTableBlobSha is $($tb.reason)") }
            else { try { $table = ConvertFrom-DvVenueTableText $tb.text } catch { $invalid.Add('ADMISSION_UNPROVEN: the committed venue-table blob does not parse') } }
            $cb = Get-DvBlobById -RepoRoot $RepoRoot -BlobSha $consentSha
            if (-not $cb.ok) { $invalid.Add("ADMISSION_UNPROVEN: admission.consentBlobSha is $($cb.reason)") }
            elseif ($null -ne $table) {
                $consent = Read-DvClipConsent -Text $cb.text -Table $table
                if (-not $consent.ok) { $invalid.Add("ADMISSION_UNPROVEN: the committed consent blob is not a valid consent file ($($consent.reason))") }
                else {
                    $consentKind = [string](Get-DvProp $admission 'consentKind')
                    $prefix = [string](Get-DvProp $admission 'consentSha256Prefix')
                    if ($consentKind -cnotin @('', 'clip', 'blanket')) { $invalid.Add('ADMISSION_UNPROVEN: admission.consentKind is neither clip nor blanket') }
                    if ($prefix -cne '' -and -not $lineSha.StartsWith($prefix, [StringComparison]::Ordinal)) { $invalid.Add('CONSENT_LINE_MISMATCH: admission.consentSha256Prefix is not the prefix of admission.ownerLineSha256') }
                    $ownRecords = @($consent.records | Where-Object { [string]$_.venue -ceq $venueName -and [string]$_.clipId -ceq $clipId })
                    if ($consentKind -ceq 'blanket') {
                        $ownBlankets = @(@(Get-DvProp $consent 'blankets') | Where-Object { $null -ne $_ -and [string]$_.ownerSentenceSha256 -ceq $lineSha -and [string]$_.recordedBy -ceq 'owner' -and [string]$_.writtenBy -ceq 'owner' })
                        if ($ownBlankets.Count -eq 0) { $invalid.Add('CONSENT_NOT_IN_BLOB: the committed consent blob holds no owner blanket record whose sentence sha256 equals admission.ownerLineSha256') }
                    }
                    elseif ($ownRecords.Count -eq 0) { $invalid.Add('CONSENT_NOT_IN_BLOB: the committed consent blob holds no owner record for this venue and clip id') }
                    else {
                        $record = $ownRecords[0]
                        if ([string]$record.ownerLineSha256 -cne $lineSha) { $invalid.Add('CONSENT_LINE_MISMATCH: admission.ownerLineSha256 is not the line sha256 of the committed consent record for this venue and clip id') }
                        if ([string]$record.recordedBy -cne 'owner') { $invalid.Add("CONSENT_NOT_OWNER: the committed consent record is not recordedBy 'owner'") }
                    }
                }
            }
            if ($null -ne $table) {
                if ($null -eq $table.venues.PSObject.Properties[$venueName]) { $invalid.Add('ADMISSION_UNPROVEN: the committed venue table has no entry for this venue') }
                $gone = ($null -ne $table.PSObject.Properties['ownerFootage']) -and ((Get-DvProp $table.ownerFootage 'cleanupClassGone') -eq $true)
                if (-not $gone) { $invalid.Add('ADMISSION_UNPROVEN: the committed venue table has the owner-footage cleanup switch off (ownerFootage.cleanupClassGone), so no owner clip could be admitted') }
                try {
                    if ((Get-DvVenueRole -Table $table -Card $card -Venue $venueName) -cne [string](Get-DvProp (Get-DvProp $Receipt 'venue') 'role')) { $invalid.Add('ROLE_MISMATCH: venue.role is not the role the committed venue table gives this venue for this card') }
                } catch { $invalid.Add('ROLE_MISMATCH: the committed venue table gives no valid role for this card') }
            }
            # the blobs are the ones committed at the commit the receipt names (a consent blob nobody ever committed is not consent)
            foreach ($pair in @(@('venue-table', $script:VenueTableRelativePath, $tableSha), @('consent', $script:ConsentRelativePath, $consentSha))) {
                $at = Get-DvBlobIdAtCommit -RepoRoot $RepoRoot -Commit $headCommit -RelativePath $pair[1]
                if ($null -eq $at) { $invalid.Add("ADMISSION_UNPROVEN: admission.headCommit is not a commit of this repository that holds the $($pair[0]) file") }
                elseif ($at -cne $pair[2]) { $invalid.Add("ADMISSION_UNPROVEN: the $($pair[0]) blob in the receipt is not the one committed at admission.headCommit") }
            }
            # the commit is part of THIS checkout's history (a receipt from another lane's branch, or a dangling commit, is not), and the consent the
            # receipt was admitted on is STILL committed at HEAD (a record the owner has since revoked leaves no usable receipt behind)
            $curHead = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('rev-parse', '--verify', 'HEAD')
            $curHeadSha = ([Text.Encoding]::ASCII.GetString($curHead.bytes)).Trim()
            if ($curHead.exitCode -ne 0 -or $curHeadSha -cnotmatch '^[0-9a-f]{40}$') { $invalid.Add('ADMISSION_COMMIT_NOT_IN_HISTORY: this repository has no HEAD to check admission.headCommit against') }
            else {
                $anc = Invoke-DvGit -RepoRoot $RepoRoot -GitArgs @('merge-base', '--is-ancestor', $headCommit, $curHeadSha)
                if ($anc.exitCode -ne 0) { $invalid.Add('ADMISSION_COMMIT_NOT_IN_HISTORY: admission.headCommit is not an ancestor of (or equal to) this checkout''s HEAD: another lane''s receipt, or a commit that was never part of this history') }
                else {
                    $nowId = Get-DvBlobIdAtCommit -RepoRoot $RepoRoot -Commit $curHeadSha -RelativePath $script:ConsentRelativePath
                    $stillConsented = $false
                    if ($nowId -ceq $consentSha) { $stillConsented = $true }
                    elseif ($null -ne $nowId -and $null -ne $table) {
                        $nowBlob = Get-DvBlobById -RepoRoot $RepoRoot -BlobSha $nowId
                        $now = $(if ($nowBlob.ok) { Read-DvClipConsent -Text $nowBlob.text -Table $table } else { $null })
                        if ($null -ne $now -and $now.ok) {
                            if ([string](Get-DvProp $admission 'consentKind') -ceq 'blanket') { $stillConsented = (@(@(Get-DvProp $now 'blankets') | Where-Object { $null -ne $_ -and [string]$_.ownerSentenceSha256 -ceq $lineSha -and [string]$_.recordedBy -ceq 'owner' }).Count -gt 0) }
                            else { $stillConsented = (@($now.records | Where-Object { [string]$_.venue -ceq $venueName -and [string]$_.clipId -ceq $clipId -and [string]$_.ownerLineSha256 -ceq $lineSha -and [string]$_.recordedBy -ceq 'owner' }).Count -gt 0) }
                        }
                    }
                    if (-not $stillConsented) { $invalid.Add('CONSENT_REVOKED: the owner record this receipt was admitted on is no longer committed at HEAD') }
                }
            }
            # the leg spec (criteria) is a committed blob too
            $leg = Find-DvCommittedLegSpec -RepoRoot $RepoRoot -Commit $headCommit -LegSpecSha256 ([string](Get-DvProp $subject 'legSpecSha256'))
            if (-not $leg.ok) { $invalid.Add('LEG_SPEC_NOT_COMMITTED: subject.legSpecSha256 is not a leg spec committed under tools/profiling/dual-venue/legs/ at admission.headCommit') }
            else {
                try { $spec = $leg.text | ConvertFrom-Json -ErrorAction Stop } catch { $invalid.Add('LEG_SPEC_NOT_COMMITTED: the committed leg spec does not parse') }
                if ($null -ne $spec) {
                    if ([string](Get-DvProp $spec 'legId') -cne $legId -or [string](Get-DvProp $spec 'card') -cne $card -or [string](Get-DvProp $spec 'clipId') -cne $clipId) { $invalid.Add('LEG_SPEC_MISMATCH: the committed leg spec is for another card, leg or clip than the receipt names') }
                    $specBackends = @()
                    if ($null -ne $spec.PSObject.Properties['backends']) { $specBackends = @($spec.backends | ForEach-Object { [string]$_ }) }
                    if ($backend -cnotin $specBackends) { $invalid.Add('LEG_SPEC_MISMATCH: the receipt backend is not one of the committed leg spec backends') }
                }
            }
        }
    }
    elseif ($mode -ceq 'offline-test') {
        if (-not $AllowOfflineTestMode) { $invalid.Add('OFFLINE_TEST_RECEIPT: a receipt written in offline test mode (caller-supplied consent / venue table / um-run) is never evidence') }
    }
    else { $invalid.Add('ADMISSION_UNPROVEN: the receipt records no admission (mode, consent and venue-table blob ids)') }

    # ---- the run: re-derived from HASHED files ---------------------------------------------------------------------------------
    $ev = Read-DvEvidenceSet -Receipt $Receipt -EvidenceDir $EvidenceDir
    foreach ($m in $ev.incomplete) { $incomplete.Add($m) }
    foreach ($m in $ev.invalid) { $invalid.Add($m) }
    # CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 r6 (adversary r5 A2, Dot 5c6bcd98, sol r5 key B1): the look.pairedSeek block is RE-DERIVED from its files.
    # Every file it lists must still be in <evidence>\paired-seek\contact-sheet\raw with its listed bytes, and a look.pairedSeekFrames verdict must be what
    # Get-DvPairedSeekVerdict -- the runner's own reader -- derives from them now, so a paired still deleted or replaced after collection cannot leave a
    # PASS/FAIL. (Production: the requested frames are the committed spec's, and a committed paired leg whose receipt has no block is incomplete.)
    $pairedSeek = Get-DvProp (Get-DvProp $Receipt 'look') 'pairedSeek'
    # r8 (adversary r7 A1 / A2, Dot on #721 16:25:15Z): the paired-still CONTRACT -- how many stills, and which frames when listed -- is read from the
    # committed leg spec only, never from the receipt (r7 took receipt requestedFrames when the spec listed none, so a receipt naming one frame narrowed
    # validation to frame-00). Production: the spec committed at admission.headCommit. Offline test mode: the spec bytes the runner ran
    # (-OfflineLegSpecBytes), bound to subject.legSpecSha256. No contract and a paired block = a typed mismatch, never a fallback to the receipt.
    $contractSpec = $(if ($production) { $spec } else { $null })
    $offlineSpecRefused = $null
    if (-not $production -and $AllowOfflineTestMode -and $null -ne $OfflineLegSpecBytes) {
        if ((Get-DvLegSpecSha256 $OfflineLegSpecBytes) -cne [string](Get-DvProp $subject 'legSpecSha256')) { $offlineSpecRefused = 'the leg spec the runner ran does not hash to subject.legSpecSha256' }
        else { try { $contractSpec = [Text.Encoding]::UTF8.GetString($OfflineLegSpecBytes).TrimStart([char]0xFEFF) | ConvertFrom-Json -ErrorAction Stop } catch { $offlineSpecRefused = 'the leg spec the runner ran does not parse' } }
    }
    $specLook = Get-DvProp $contractSpec 'look'
    $specFrames = Get-DvProp $specLook 'pairedSeekFrames'
    # r9 fix 1 (adversary r9 a0936637 A1): the spec asks for paired stills exactly when the RUNNER asks the generator for them (Invoke-VenueLeg: a look leg,
    # look.pairedSeek Boolean true). This used `-eq $true`, which a speed leg carrying look.pairedSeek true and a look leg whose pairedSeek is the string
    # "true" both satisfy; the runner asks neither, so an otherwise good run ended INVALID (RECEIPT_FIELD_ABSENT). A paired block on a receipt whose spec
    # asks for none is no runner's record either.
    $specLegType = Get-DvProp $contractSpec 'legType'
    $specPairedSeek = Get-DvProp $specLook 'pairedSeek'
    $specAsksPaired = ($specLegType -is [string] -and $specLegType -ceq 'look' -and $specPairedSeek -is [bool] -and $specPairedSeek)
    if ($null -eq $pairedSeek) {
        if ($specAsksPaired) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the committed leg spec asks for look.pairedSeek but the receipt carries no look.pairedSeek block') }
    } elseif ($null -eq $specLook) {
        $invalid.Add("PAIRED_SEEK_NOT_FROM_EVIDENCE: the receipt carries a look.pairedSeek block but no committed leg spec look names the paired stills it must hold$(if ($offlineSpecRefused) { " ($offlineSpecRefused)" })")
    } elseif (-not $specAsksPaired) {
        $invalid.Add('PAIRED_SEEK_NOT_FROM_EVIDENCE: the receipt carries a look.pairedSeek block but the committed leg spec asks for no paired stills (a look leg with look.pairedSeek true)')
    } elseif (-not [string]::IsNullOrWhiteSpace($ev.dir) -and (Test-Path -LiteralPath $ev.dir -PathType Container)) {
        $seekDir = Join-Path $ev.dir 'paired-seek\contact-sheet\raw'
        $listedSeek = Get-DvProp $pairedSeek 'files'   # (assigned, then @()-wrapped: see Read-DvContactFrames)
        $seekEntries = @(@($listedSeek) | Where-Object { $null -ne $_ })
        if ((ConvertTo-DvInt64 (Get-DvProp $pairedSeek 'count')) -ne $seekEntries.Count) { $invalid.Add('PAIRED_SEEK_NOT_FROM_EVIDENCE: look.pairedSeek.count is not the number of files it lists') }
        foreach ($entry in $seekEntries) {
            $name = [string](Get-DvProp $entry 'name'); $sha = [string](Get-DvProp $entry 'sha256')
            if ($name -cnotmatch '^[^\\/:*?"<>|]+$' -or $name -cin @('.', '..') -or $sha -cnotmatch '^[0-9a-f]{64}$') { $invalid.Add('PAIRED_SEEK_UNLISTED: a look.pairedSeek entry is not a plain file name with a 64-hex sha256'); continue }
            $file = Join-Path $seekDir $name
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { $invalid.Add("PAIRED_SEEK_FILE_ABSENT: look.pairedSeek lists '$name' but paired-seek\contact-sheet\raw does not hold it"); continue }
            if ((Get-DvSha256OfBytes ([IO.File]::ReadAllBytes($file))) -cne $sha) { $invalid.Add("PAIRED_SEEK_HASH_MISMATCH: '$name' does not hash to the sha256 look.pairedSeek lists") }
        }
        $requestedSeek = Get-DvProp $pairedSeek 'requestedFrames'
        if ($null -ne $specFrames -and -not (Test-DvJsonEquivalent $requestedSeek $specFrames)) { $invalid.Add('PAIRED_SEEK_NOT_FROM_EVIDENCE: look.pairedSeek.requestedFrames is not the committed leg spec''s look.pairedSeekFrames') }
        elseif ($null -eq $specFrames -and $null -ne $requestedSeek) { $invalid.Add('PAIRED_SEEK_NOT_FROM_EVIDENCE: look.pairedSeek.requestedFrames is present but the committed leg spec names no look.pairedSeekFrames (a receipt cannot narrow or add to the committed stills)') }
        # r7 (adversary r6 A1, Dot 6558329764): the verdict is re-derived for EVERY paired block. r8 (adversary r7 A1 / A2): over the committed spec's
        # stills only -- look.contactSheetFrames of them, at look.pairedSeekFrames when listed -- so every expected still and every listed file is checked.
        # (fix iteration 1, adversary r8 A2: the in-pass partners are the hash-verified contact-sheet\raw frames, read the way every reader reads them)
        $inPass = Read-DvContactFrames -Receipt $Receipt -EvidenceDir $ev.dir
        $verdictArgs = @{ SeekDir = $seekDir; Files = $seekEntries; StillCount = (Get-DvProp $specLook 'contactSheetFrames'); InPassFiles = @($inPass.files) }
        $framesUsable = $true
        if ($null -ne $specFrames) {
            try { $verdictArgs['RequestedFrames'] = [long[]]@($specFrames) } catch { $framesUsable = $false; $invalid.Add('PAIRED_SEEK_NOT_FROM_EVIDENCE: the committed look.pairedSeekFrames is not a list of frame numbers') }
        }
        if ($framesUsable) {
            $seekVerdict = Get-DvPairedSeekVerdict @verdictArgs
            foreach ($f in 'reportedFrames', 'framesHonoured', 'frameMismatches') {
                if (-not (Test-DvJsonEquivalent (Get-DvProp $pairedSeek $f) $seekVerdict.$f)) { $invalid.Add("PAIRED_SEEK_NOT_FROM_EVIDENCE: look.pairedSeek.$f is not what the paired stills re-derive") }
            }
            if (-not $seekVerdict.framesHonoured) { $invalid.Add("PAIRED_SEEK_FRAMES_NOT_HONOURED: $($seekVerdict.frameMismatches -join '; ')") }
        }
    }
    if ($ev.complete) {
        $derived = Get-DvPlaybackEvidence -Summary $ev.summary -EvidenceManifest $ev.manifest -ResultJson $ev.result -LogText $ev.logText -LogSha256 $ev.logSha -ExpectedClipId $clipId `
            -RequestedConform ([string](Get-DvProp (Get-DvProp $spec 'generatorArgs') 'playbackConform'))
        $stored = Get-DvProp $Receipt 'playback'
        if ($null -eq $stored) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the receipt carries no playback block (the source-frame verdict)') }
        else {
            foreach ($f in $script:PlaybackComparedFields) {
                if (-not (Test-DvJsonEquivalent (Get-DvProp $stored $f) (Get-DvProp $derived $f))) { $invalid.Add("PLAYBACK_NOT_FROM_EVIDENCE: playback.$f is not what the hashed run log and result re-derive") }
            }
        }
        foreach ($p in @($derived.invalidReasons)) { if ($null -ne $p -and [string]$p -ne '') { $invalid.Add([string]$p) } }
        # the evidence's own identity: the clip, the venue and the build the receipt says it is about
        $jobVenue = [string](Get-DvProp (Get-DvProp $ev.summary 'display') 'venue')
        if ([string]::IsNullOrWhiteSpace($jobVenue)) { $incomplete.Add('EVIDENCE_ABSENT: summary.json carries no display.venue') }
        elseif ($jobVenue -cne $venueName) { $invalid.Add('VENUE_MISMATCH: the job''s own summary names a different venue than the receipt') }
        if ($null -ne $ev.manifest) {
            $manClip = [string](Get-DvProp $ev.manifest 'clipId')
            if ([string]::IsNullOrWhiteSpace($manClip)) { $incomplete.Add('EVIDENCE_ABSENT: evidence-manifest.json carries no clipId') }
            elseif ($manClip -cne $clipId) { $invalid.Add('CLIP_MISMATCH: the evidence manifest names a different clip id than the receipt') }
            $manBuild = [string](Get-DvProp (Get-DvProp $ev.manifest 'buildManifest') 'sha256')
            if ([string]::IsNullOrWhiteSpace($manBuild)) { $incomplete.Add('EVIDENCE_ABSENT: evidence-manifest.json carries no buildManifest.sha256') }
            elseif ($manBuild.ToLowerInvariant() -cne [string](Get-DvProp $subject 'buildManifestSha256')) { $invalid.Add('BUILD_MISMATCH: the evidence manifest binds a different build manifest than the receipt subject') }
        }
        $metrics = Get-DvVerbatimMetrics -Summary $ev.summary -EvidenceManifest $ev.manifest
        if (-not (Test-DvJsonEquivalent (Get-DvProp $Receipt 'metrics') $metrics)) { $invalid.Add('METRICS_NOT_FROM_EVIDENCE: the receipt metrics are not the verbatim metrics of the hashed summary.json / manifest') }
        if ([string](Get-DvProp $ev.umRun 'schema') -cne $script:UmRunSchema) { $invalid.Add('EVIDENCE_UNPARSABLE: um-run.json is not a dual-venue um-run record') }
        $exit = ConvertTo-DvInt64 (Get-DvProp $ev.umRun 'exitCode')
        if ($null -eq $exit) { $invalid.Add('EVIDENCE_UNPARSABLE: um-run.json carries no integer exitCode') }

        # ---- the run IS the backend and the leg the receipt names: re-derived from the HASHED summary, never from a receipt field -------
        $derivedBackend = $null
        if ($production) {
            $derivedBackend = Get-DvDerivedBackend -Summary $ev.summary
            $summaryBackend = Get-DvProp $ev.summary 'backend'
            if ($null -eq $derivedBackend) { $invalid.Add($script:BackendNotDerivableMessage) }
            elseif ($derivedBackend -cne $backend) { $invalid.Add("BACKEND_MISMATCH: the hashed summary's frame counters say the run used the $derivedBackend backend but the receipt says $backend") }
            if ($null -ne $summaryBackend -and [string]$summaryBackend -cne $backend) { $invalid.Add('BACKEND_MISMATCH: the hashed summary.json names another backend than the receipt') }
            elseif ($null -eq $summaryBackend -and $backend -ceq 'cpu') { $invalid.Add($script:CpuBackendFieldMissingMessage) }
            $summaryVenue = Get-DvProp $ev.summary 'declaredVenue'
            if ($null -ne $summaryVenue -and [string]$summaryVenue -cne $venueName) { $invalid.Add('VENUE_MISMATCH: the hashed summary.json was declared for another venue than the receipt') }
            if ($null -ne $spec) {
                $specLook = ([string](Get-DvProp $spec 'legType') -ceq 'look')
                $runLook = ((Get-DvProp $ev.summary 'lookLeg') -eq $true)
                # a product-failure terminal's summary.json states no lookLeg / lookAssistForced / lookFlavor (the variant edit reaches the SUCCESS summary only), so the
                # leg type is unstated there, typed and recorded -- not a mismatch. It decides nothing: such a receipt can only be a FAIL derived from the terminal's own
                # result token (a PASS needs a captured summary, which states all three and is checked below).
                $failureTerminal = ([string](Get-DvProp $ev.summary 'result') -cin $script:FailureTerminals)
                if ($failureTerminal -and $null -eq (Get-DvProp $ev.summary 'lookLeg')) { $unbound += 'LEG_TYPE_UNSTATED: a product-failure terminal''s summary.json carries no lookLeg, so the leg type is taken from the committed spec only; the receipt is a FAIL derived from the terminal''s result token' }
                elseif ($specLook -ne $runLook) { $invalid.Add('LEG_TYPE_MISMATCH: the hashed summary.json says this run was ' + $(if ($runLook) { 'a LOOK leg' } else { 'not a LOOK leg' }) + ' but the committed leg spec the receipt names is ' + $(if ($specLook) { 'a look leg' } else { 'not a look leg' })) }
                elseif ($specLook) {
                    if ((Get-DvProp $ev.summary 'lookAssistForced') -ne $true) { $invalid.Add('LEG_TYPE_MISMATCH: a look leg''s hashed summary.json does not say lookAssistForced') }
                    $specFlavor = [string](Get-DvProp (Get-DvProp $spec 'look') 'lookFlavor'); if ([string]::IsNullOrWhiteSpace($specFlavor)) { $specFlavor = 'classic' }
                    if ([string](Get-DvProp $ev.summary 'lookFlavor') -cne $specFlavor -or [string](Get-DvProp $subject 'lookFlavor') -cne $specFlavor) { $invalid.Add('LEG_TYPE_MISMATCH: the look flavor in the hashed summary.json / receipt subject is not the committed spec''s') }
                    # LOOK-ASSIST-FILM-FLAVOR-2 r2: a run's lookReceiptSha256 is accepted only when the committed spec names a look receipt with that hash (both absent is the old leg).
                    if ([string](Get-DvProp $ev.summary 'lookReceiptSha256') -cne [string](Get-DvProp (Get-DvProp $spec 'look') 'receiptSha256')) { $invalid.Add('LOOK_RECEIPT_MISMATCH: the hashed summary.json''s lookReceiptSha256 is not the committed spec''s look.receiptSha256') }
                }
                # VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: a speed leg's lookReceiptSha256 is accepted only when the committed spec names a pace-leg receipt with that hash
                # (generatorArgs.lookReceiptSha256; both absent is every speed leg before this round). Outside the chain above, so a product-failure terminal is held
                # to it too: a hash its summary states must be the spec's (its summary may state none: the variant edit reaches the SUCCESS summary only).
                if (-not $specLook) {
                    $paceRunReceipt = [string](Get-DvProp $ev.summary 'lookReceiptSha256')
                    if ([string](Get-DvProp (Get-DvProp $spec 'generatorArgs') 'lookReceiptSha256') -cne $paceRunReceipt -and -not ($failureTerminal -and [string]::IsNullOrEmpty($paceRunReceipt))) {
                        $invalid.Add('LOOK_RECEIPT_MISMATCH: the hashed summary.json''s lookReceiptSha256 is not the committed spec''s generatorArgs.lookReceiptSha256')
                    }
                }
            }
        }

        # ---- the SCALE the run rendered at: re-derived from the HASHED run log and the COMMITTED spec, never from a receipt field (DVE-SCALE2-LOOK-LEG-1 r2) ----
        $scaleVerdict = $null
        if ($production -and $null -ne $spec -and $null -ne $derivedBackend) {
            $scaleVerdict = Get-DvScaleEvidence -Spec $spec -Backend $derivedBackend -LogText $ev.logText
            $storedScale = Get-DvProp $Receipt 'scale'
            if ($null -eq $storedScale) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the receipt carries no scale block (requestedScale / effectiveScale)') }
            else {
                foreach ($f in @('requestedScale', 'effectiveScale', 'acceptedEffectiveScale')) {
                    if (-not (Test-DvJsonEquivalent (Get-DvProp $storedScale $f) $scaleVerdict[$f])) { $invalid.Add("SCALE_NOT_FROM_EVIDENCE: scale.$f is not what the hashed run log and the committed leg spec derive") }
                }
            }
        }

        # ---- the DISPLAY MODE the run ran in: re-derived from the HASHED run log and the COMMITTED spec (DUAL-VENUE-DISPLAY-MATRIX-1) ----
        # A requested-windowed leg that ran full screen (or the reverse) is not a valid measurement; neither is a leg that NAMES a mode the log cannot confirm.
        if ($production -and $null -ne $spec) {
            $displayVerdict = Get-DvDisplayEvidence -Spec $spec -LogText $ev.logText
            $storedDisplay = Get-DvProp $Receipt 'display'
            if ($null -eq $storedDisplay) {
                if ($displayVerdict.explicit) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the receipt carries no display block (requestedMode / observedMode) and its committed spec names a displayMode') }
            } else {
                foreach ($f in @('requestedMode', 'observedMode')) {
                    if (-not (Test-DvJsonEquivalent (Get-DvProp $storedDisplay $f) $displayVerdict[$f])) { $invalid.Add("DISPLAY_NOT_FROM_EVIDENCE: display.$f is not what the hashed run log and the committed leg spec derive") }
                }
            }
            if ($displayVerdict.blocks) { $invalid.Add("DISPLAY_MODE_NOT_HONOURED: $($displayVerdict.reason); a leg that did not run in the display mode it names is not a valid measurement") }
            # CUDA-HEADROOM-1: an unpaced leg the app did not run unpaced is the pace wall, not throughput (Get-DvUnpacedEvidence).
            $unpacedVerdict = Get-DvUnpacedEvidence -Spec $spec -LogText $ev.logText
            if ($unpacedVerdict.blocks) { $invalid.Add("UNPACED_NOT_HONOURED: $($unpacedVerdict.reason); a paced run is no throughput measurement") }
            # VENUE-NATIVE-LEG-LOOKLEG-LABEL-1 r1: a receipt that CLAIMS the look ran (lookAssistOn true) must be backed by the hashed evidence -- the app's own run log says
            # look_assist_enabled=1, the hashed summary.json says the look was forced and the flavor the app reported is the one requested, and lookAssistFlavor is that flavor.
            # Only the claim is re-derived (a receipt that predates the field, or says false, claims nothing); the leg TYPE binding above is untouched.
            $claimOnRaw = Get-DvProp $Receipt 'lookAssistOn'
            if ($null -ne $claimOnRaw -and $claimOnRaw -isnot [bool]) { $invalid.Add('LOOK_ASSIST_CLAIM_UNBACKED: the receipt''s lookAssistOn is not a boolean') }
            elseif ($claimOnRaw -is [bool] -and $claimOnRaw) {
                $claimVerdict = Get-DvLookAssistEvidence -Spec $spec -LogText $ev.logText
                $claimForcedRaw = Get-DvProp $ev.summary 'lookAssistForced'   # a real boolean only: `-eq $true` on an array is a filter, and on a string a cast (pass 2 A1)
                $claimRequestedRaw = Get-DvProp $ev.summary 'lookFlavor'; $claimReportedRaw = Get-DvProp $ev.summary 'lookFlavorReported'; $claimStoredRaw = Get-DvProp $Receipt 'lookAssistFlavor'
                $claimRequested = [string]$claimRequestedRaw; $claimLogFlavor = [string]$claimVerdict.flavor
                # strings only (adversary A2: a singleton array casts to its element); the flavor is the one the app's own record body names (adversary A1)
                if (-not ($claimVerdict.asked -and $claimVerdict.verdict -ceq 'HONOURED' -and $claimForcedRaw -is [bool] -and $claimForcedRaw -and $claimRequestedRaw -is [string] -and $claimReportedRaw -is [string] -and $claimStoredRaw -is [string] -and $claimRequested.Length -gt 0 -and $claimReportedRaw -ceq $claimRequested -and $claimLogFlavor -ceq $claimRequested -and $claimStoredRaw -ceq $claimLogFlavor)) {
                    $invalid.Add('LOOK_ASSIST_CLAIM_UNBACKED: the receipt says Look Assist ran (lookAssistOn) but the hashed run log / summary.json do not show it ran with the flavor lookAssistFlavor names')
                }
            }
            # VENUE-LEG-PLAYBACK-MAX-THREADS-ARG-1: a capped leg the app did not run at the cap (or whose count is unreadable) is no measurement under it.
            $workerThreadsVerdict = Get-DvWorkerThreadsEvidence -Spec $spec -LogText $ev.logText -Summary $ev.summary
            if ($workerThreadsVerdict.blocks) { $invalid.Add("WORKER_THREADS_NOT_HONOURED: $($workerThreadsVerdict.reason); a run at another worker count is no measurement of the worker count its leg specifies") }
            # adversary r2b A1: the receipt's own workerThreads block is re-derived like the display block, so a receipt cannot state a count (or a verdict) the
            # hashed evidence does not; the writer emits the block exactly when the spec asks for a cap (Invoke-VenueLeg.ps1), so the same condition decides here.
            $storedWorkerThreads = Get-DvProp $Receipt 'workerThreads'
            if ($workerThreadsVerdict.requested) {
                if ($null -eq $storedWorkerThreads) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the receipt carries no workerThreads block (requested / observed / verdict) and its committed spec names a playbackMaxThreads') }
                else {
                    foreach ($f in @('requested', 'observed', 'verdict')) {
                        if (-not (Test-DvJsonEquivalent (Get-DvProp $storedWorkerThreads $f) $workerThreadsVerdict[$f])) { $invalid.Add("WORKER_THREADS_NOT_FROM_EVIDENCE: workerThreads.$f is not what the hashed run log, summary.json and the committed leg spec derive") }
                    }
                }
            }
            elseif ($null -ne $storedWorkerThreads) { $invalid.Add('WORKER_THREADS_NOT_FROM_EVIDENCE: the receipt carries a workerThreads block but its committed leg spec names no playbackMaxThreads') }
            # PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1: a leg that asks for the prep-conflated-move switch the app did not report active (or whose report is unreadable, or absent
            # because the binary emits none) is no measurement of that arm; the receipt's own prepConflatedMove block is re-derived like the workerThreads block.
            $prepConflatedMoveVerdict = Get-DvPrepConflatedMoveEvidence -Spec $spec -LogText $ev.logText
            if ($prepConflatedMoveVerdict.blocks) { $invalid.Add("PREP_CONFLATED_MOVE_NOT_HONOURED: $($prepConflatedMoveVerdict.reason); a run whose prep-conflated-move state is not the one its leg names (ON when it asks, otherwise not on) is no measurement of that arm") }
            $storedPrepConflatedMove = Get-DvProp $Receipt 'prepConflatedMove'
            if ($prepConflatedMoveVerdict.requested) {
                if ($null -eq $storedPrepConflatedMove) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the receipt carries no prepConflatedMove block (requested / observed / verdict) and its committed spec names a prepConflatedMove') }
                else {
                    foreach ($f in @('requested', 'observed', 'verdict')) {
                        if (-not (Test-DvJsonEquivalent (Get-DvProp $storedPrepConflatedMove $f) $prepConflatedMoveVerdict[$f])) { $invalid.Add("PREP_CONFLATED_MOVE_NOT_FROM_EVIDENCE: prepConflatedMove.$f is not what the hashed run log and the committed leg spec derive") }
                    }
                }
            }
            elseif ($null -ne $storedPrepConflatedMove) { $invalid.Add('PREP_CONFLATED_MOVE_NOT_FROM_EVIDENCE: the receipt carries a prepConflatedMove block but its committed leg spec names no prepConflatedMove') }
            # HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r7: a leg that asks for the deferred-release kill switch the app did not report off (or whose
            # report is unreadable, or absent because the binary emits none), or a head-arm leg whose app reports it off, is no measurement of its arm; the
            # receipt's own reconReleaseDeferred block is re-derived like the prepConflatedMove block.
            $reconReleaseVerdict = Get-DvReconReleaseDeferredEvidence -Spec $spec -LogText $ev.logText
            if ($reconReleaseVerdict.blocks) { $invalid.Add("RECON_RELEASE_DEFERRED_NOT_HONOURED: $($reconReleaseVerdict.reason); a run whose deferred-release state is not the one its leg names (off when it asks, otherwise not off) is no measurement of that arm") }
            $storedReconRelease = Get-DvProp $Receipt 'reconReleaseDeferred'
            if ($reconReleaseVerdict.requested) {
                if ($null -eq $storedReconRelease) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the receipt carries no reconReleaseDeferred block (requested / observed / verdict) and its committed spec names a disableReconReleaseDeferred') }
                else {
                    foreach ($f in @('requested', 'observed', 'verdict')) {
                        if (-not (Test-DvJsonEquivalent (Get-DvProp $storedReconRelease $f) $reconReleaseVerdict[$f])) { $invalid.Add("RECON_RELEASE_DEFERRED_NOT_FROM_EVIDENCE: reconReleaseDeferred.$f is not what the hashed run log and the committed leg spec derive") }
                    }
                }
            }
            elseif ($null -ne $storedReconRelease) { $invalid.Add('RECON_RELEASE_DEFERRED_NOT_FROM_EVIDENCE: the receipt carries a reconReleaseDeferred block but its committed leg spec names no disableReconReleaseDeferred') }
            # HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r1: a leg whose due-wake delivery is not the one its arm names (post when it asks, otherwise
            # send when reported) is no measurement of its arm; the receipt's own dueWakeDelivery block is re-derived like the reconReleaseDeferred block.
            $dueWakeDeliveryVerdict = Get-DvDueWakeDeliveryEvidence -Spec $spec -LogText $ev.logText
            if ($dueWakeDeliveryVerdict.blocks) { $invalid.Add("DUE_WAKE_DELIVERY_NOT_HONOURED: $($dueWakeDeliveryVerdict.reason); a run whose due-wake delivery is not the one its leg names (post when it asks, otherwise send) is no measurement of that arm") }
            $storedDueWakeDelivery = Get-DvProp $Receipt 'dueWakeDelivery'
            if ($dueWakeDeliveryVerdict.requested) {
                if ($null -eq $storedDueWakeDelivery) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the receipt carries no dueWakeDelivery block (requested / observed / verdict) and its committed spec names a dueWakeDeliveryPost') }
                else {
                    foreach ($f in @('requested', 'observed', 'verdict')) {
                        if (-not (Test-DvJsonEquivalent (Get-DvProp $storedDueWakeDelivery $f) $dueWakeDeliveryVerdict[$f])) { $invalid.Add("DUE_WAKE_DELIVERY_NOT_FROM_EVIDENCE: dueWakeDelivery.$f is not what the hashed run log and the committed leg spec derive") }
                    }
                }
            }
            elseif ($null -ne $storedDueWakeDelivery) { $invalid.Add('DUE_WAKE_DELIVERY_NOT_FROM_EVIDENCE: the receipt carries a dueWakeDelivery block but its committed leg spec names no dueWakeDeliveryPost') }
        }

        # ---- the CONFORM ARM the run played under: re-derived from the HASHED run log and the COMMITTED spec (HFR-NATIVE-60FPS-FULLRES-1) ----
        if ($production -and $null -ne $spec -and $null -ne $derived) {
            $conformArm = Get-DvConformArmVerdict -Spec $spec -Playback $derived
            if ($conformArm.blocks) { $invalid.Add("CONFORM_ARM_NOT_HONOURED: $($conformArm.reason); a leg that did not play under the conform arm it names is not a valid measurement") }
        }

        # ---- the PRESENTMON GAP: re-derived from the VERIFIED verbatim metrics, the writer's own verdict (PRESENTMON-BACHELOR-CUDA-INTERNAL-GAP-1) --------
        # PR #788 r1 sol B2: a reader must never count what the writer would refuse. A receipt whose job published presentMonTemporalWindowSource (this card's
        # job) is judged by the same Get-DvPresentMonGapVerdict Invoke-VenueLeg applies: a blocking verdict is INVALID here (so every reader that gates on this
        # status -- Invoke-VenueBaseline, New-VenueSheetPair, New-VenueFlavorPair -- counts no figure), and an APP_SWAP_STALL derives FAIL below. A receipt
        # that predates the field keeps the classification it had (its gap was never judged by the writer either).
        $presentMonGap = $null
        if ($null -ne (Get-DvProp $metrics 'presentMonTemporalWindowSource')) {
            # adversary r4 A1: the backend the HASHED summary derives in production (never a receipt field); a receipt with none is INVALID above
            $presentMonGap = Get-DvPresentMonGapVerdict -Metrics $metrics -Backend $(if ($production) { $derivedBackend } else { $backend })
            if ($presentMonGap.blocks) { $invalid.Add("$($presentMonGap.reason). A leg whose PresentMon gap the app's own swap record does not explain is not counted") }
        }

        # ---- the OUTCOME: re-derived from the job's result, its exit code, the verbatim metrics and the COMMITTED criteria -----------
        $resolved = $null
        if ($null -ne $exit) {
            $resolved = Resolve-DvJobOutcome -ResultToken ([string](Get-DvProp $ev.umRun 'resultToken')) -ExitCode ([int]$exit) -SmokeRefusalReason ([string](Get-DvProp $ev.summary 'smokeRefusalReason'))
            if ($resolved.outcome -eq 'CAPTURED') {
                if ($null -eq $ev.manifest) { $incomplete.Add('EVIDENCE_ABSENT: the job captured but evidence-manifest.json (evidence.evidenceManifestSha256) is not in the evidence') }
                if ($exit -ne 0) { $invalid.Add('OUTCOME_NOT_DERIVABLE: the job printed a capture but exited non-zero; a capture that disagrees with its own exit code is not evidence') }
                elseif (-not $derived.jobOracleBlockPresent) { $invalid.Add('RECEIPT_FIELD_ABSENT: the captured job''s summary.json carries no sourceFrames block (its own oracle did not run)') }
            }
            elseif ($resolved.outcome -ne 'FAIL') { $invalid.Add("OUTCOME_NOT_DERIVABLE: the job's result derives $($resolved.outcome), which is not a PASS/FAIL signal") }
        }
        if ($production -and $null -ne $spec -and $null -ne $resolved -and $invalid.Count -eq 0) {
            $expected = $resolved.outcome
            if ($expected -eq 'CAPTURED') {
                $role = [string](Get-DvProp (Get-DvProp $Receipt 'venue') 'role')
                # the criteria are selected by the backend the HASHED run used (== the receipt's, or this receipt is already INVALID above)
                $criteria = Get-DvProp (Get-DvProp (Get-DvProp $spec 'criteria') $role) $derivedBackend
                # r19 (sol r18 B2): the criteria also see the measured conform fields re-derived from the hashed run log; the receipt's metrics stay verbatim
                $verdict = Test-DvCriteria -Criteria $criteria -Metrics (Get-DvCriteriaMetrics -Metrics $metrics -Playback $derived)
                $sheetOk = $true
                if ([string](Get-DvProp $spec 'legType') -ceq 'look') {
                    $sheet = Join-Path $ev.dir 'contact-sheet\sheet.png'
                    $claimed = [string](Get-DvProp (Get-DvProp (Get-DvProp $Receipt 'look') 'contactSheet') 'sha256')
                    if (-not ((Test-Path -LiteralPath $sheet -PathType Leaf) -and (Get-DvSha256OfFile $sheet) -ceq $claimed)) { $sheetOk = $false }
                    else {
                        # the raw frames the sheet pair would compose are accepted only as the hashed contact-frames manifest lists them
                        $frames = Read-DvContactFrames -Receipt $Receipt -EvidenceDir $ev.dir
                        foreach ($m in @($frames.reasons)) { $invalid.Add([string]$m) }
                    }
                }
                $expected = $(if (-not $sheetOk) { 'FAIL' } elseif ($verdict.pass) { 'PASS' } else { 'FAIL' })
                # PR #788 r1 opus B3: the app's own swap stall is counted, as a FAIL (the writer's order: after the criteria, before the scale override)
                if ($expected -ceq 'PASS' -and $null -ne $presentMonGap -and $presentMonGap.failsLeg) { $expected = 'FAIL' }
                # a capture that did not render at the scale the leg asked for (or declared) is no signal either way (the runner's own order: scale first)
                if (-not $scaleVerdict.honoured) { $expected = 'SCALE_NOT_HONOURED' }
            }
            if ($expected -cne $outcome) { $invalid.Add("OUTCOME_NOT_DERIVED: the evidence and the committed criteria derive $expected but the receipt says $outcome") }
        }
    }

    $reasons = @($invalid.ToArray()) + @($incomplete.ToArray())
    # NARROWING EXIT (hub ruling 2026-10-02): no venue-held anchor exists (there is no venue-side signature, and um-run.json is written by the runner
    # itself), so a production receipt is ADVISORY at most. This is the ONE line that decides it: re-enabling production PASS means changing it, and
    # test_dual_venue_evidence.py pins both the result and a mutation that does.
    $venueAnchorAbsent = $production
    if ($venueAnchorAbsent) { $reasons += 'VENUE_ANCHOR_ABSENT: no venue-held anchor (a venue-signed summary.json, or a re-read of the venue outbox by jobId) exists, so a production receipt is advisory: it is never VERIFIED and never a usable PASS (DUAL-VENUE-PASS-PROVENANCE-1)' }
    # An offline-test receipt can only be valid with -AllowOfflineTestMode (the writer's offline path and the test harness): it is never
    # reported as VERIFIED, so nothing that checks `status -eq 'VERIFIED'` can ever take a stub-run receipt for evidence.
    $status = $(if ($invalid.Count -gt 0) { 'INVALID' } elseif ($incomplete.Count -gt 0) { 'INCOMPLETE' } elseif ($mode -ceq 'offline-test') { 'VERIFIED_OFFLINE_TEST' } elseif ($venueAnchorAbsent) { 'ADVISORY' } else { 'VERIFIED' })
    [pscustomobject]@{ valid = ($status -in @('VERIFIED', 'VERIFIED_OFFLINE_TEST')); status = $status; reasons = $reasons; unbound = $unbound; evidenceDir = $ev.dir; legType = $(if ($null -ne $spec) { [string](Get-DvProp $spec 'legType') } else { $null }) }
}

# --- the job's notAfterUtc (LEASE-NOT-AFTER-SINGLE-LEG-SUBMITTERS-1) --------------------------------
# um-run.ps1's own -MaxAdmissionWaitSec default. A submitter that does not choose one passes this by name, so the wait it bounds is the wait um-run uses
# (test_venue_not_after.py reads the default out of um-run.ps1's param block and pins the two equal).
$script:DvUmRunMaxAdmissionWaitSec = 7200
# Added to an uncapped bound: um-run reads the outbox every -PollSeconds (3 s), so the client can still be waiting a little past its waits.
# LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 r2 (hub ruling on sol's r1 blocker): the uncapped bound is no longer counted from this helper's clock. The
# submitter passes it as um-run.ps1 -NotAfterBudgetSec (budgetSec below), and UmRunDrop.psm1 counts it from its own clock read immediately before the claim
# is written, so a slow setup or share before the drop is absorbed a priori, whatever its length. r3 (sol r2 blocker): the drop reads its clock AGAIN
# immediately before the job's rename into view, after every side-file and the job's own copy, and re-publishes the value it re-derives, so slow placement
# is absorbed too. The slack is then only the room between that last read and um-run's 'submitted' line (a fixed few small share operations: the
# metadata re-publish and the job's rename, whatever the files' sizes) plus the poll above. It is no longer a tolerance for a late drop.
# r5 (Dot 180721e3 on r4): um-run.ps1 now ends its queue and admission waits at absolute deadlines counted from the drop's sampledUtc, so time after the job
# is visible (the drop's return, um-run's own lines) no longer comes out of the slack either: what the slack still covers is the drop's measured placement
# bound (UmRunDrop.psm1 UmRunNotAfterPlacementStallSec) and um-run's poll.
# r6 (Grok G7): so the measured witness (Test-DvNotAfterCoversDrop) judges a drop-relative value from that same sampledUtc, the instant um-run counts its
# waits from, whenever the drop reports one; its DVE_NOT_AFTER_DROP_LATE then fires on the uncapped path only for a drop that did not count the budget from
# the clock read it reports (a defect), never for a stall after visibility, however long.
$script:DvNotAfterSlackSec = 60

function Get-DvJobNotAfter {
    <#
    .SYNOPSIS
    Pure. The notAfterUtc a venue submitter passes um-run.ps1 -NotAfterUtc: the instant after which no caller reads the job's result any more. Past it the
    Bachelor agent never leases or launches the job (LEASE-REQUEST-CHILD-NOT-AFTER-1); a launched job is never stopped by it, and the Ultra-Magnus agent
    ignores the field. The bound is never earlier than a wait the caller passes (a job must not be refused while its submitter still waits) and never later
    than the last read:
      - no kill bound of its own (-CallerCapSec 0; Invoke-VenueLeg): um-run waits at most Queue + Admission + Timeout + Claimed after submit (QUEUED, then
        held in the gate, then CLAIMED to its outer ceiling), so the bound is that sum plus the slack. Source 'um-run-waits'. VenueChain's later by-name read
        of a killed leg's job (Get-VenueRemoteJobState) is PRESENCE only, and an expired job publishes a result, so it needs no later bound.
      - a caller that kills its um-run at now + -CallerCapSec (Invoke-VenueStageNightly's remaining -MaxTotalSec): nobody waits past the cap, so the bound
        is the cap when that is lower. Source 'caller-cap'. A wait over the cap is a caller bug (it would be cut short): DVE_NOT_AFTER_WAIT_OVER_CAP.
      - a caller that kills its um-run at an ABSOLUTE instant passes it as -CallerDeadlineUtc (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1; the stage
        driver's $script:Deadline). The bound is then that instant when it is lower than now + the uncapped bound, source 'caller-deadline', and
        -CallerCapSec (the remaining seconds the caller sized its waits from) only feeds the over-cap check. Counting a sampled remaining budget again from
        a later now would move the bound past the deadline by however long the caller was delayed between the sample and this call; the deadline itself
        can only be approached, never passed. A deadline that is not later than now is refused: DVE_NOT_AFTER_DEADLINE_PASSED.
      - -LaterHarvest: a LATER call reads the result by name, wants the job RUN, and cannot re-submit its single-use JobId (the stage driver's DLL pair and
        smoke runner). That reader comes at a time this call cannot bound, so the later bound is none: notAfterUtc '' and nothing is passed. Source
        'later-harvest'.
    -MaxAdmissionWaitSec defaults to um-run.ps1's own default; the result echoes the one used so the caller passes the same value by name.
    WHERE THE BOUND COUNTS FROM (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1): anchorUtc, the instant this call ran. um-run starts its waits only once the
    job file is on the share, after this call. r2 (hub ruling): the bound is therefore passed DROP-RELATIVE wherever it is not an absolute kill instant:
    budgetSec (boundSec for 'um-run-waits'; the uncapped bound for 'caller-deadline') goes to um-run.ps1 -NotAfterBudgetSec and capUtc (the
    -CallerDeadlineUtc instant, or '') to -NotAfterCapUtc, and the drop writes min(its own now + budgetSec, capUtc). notAfterUtc stays this call's estimate
    of that value (the drop at the anchor). 'caller-cap' counts from the caller's own start, so it stays absolute (budgetSec null: pass -NotAfterUtc). For
    'um-run-waits' dropToleranceSec (the slack) is how much later than the drop's own clock read the job may become visible with every um-run wait still
    inside the bound; a caller that kills um-run at the bound ('caller-cap', 'caller-deadline') waits no longer than it whenever the job appears: null.
    .OUTPUTS
    [pscustomobject]@{ notAfterUtc ('o' UTC text ending in Z, or ''); source; boundSec; waitSec; longestWaitSec; maxAdmissionWaitSec; anchorUtc ('o');
    dropToleranceSec (or null); budgetSec (or null); capUtc ('o' or ''); why }
    #>
    param(
        [Parameter(Mandatory)][ValidateRange(1, 86400)][int]$TimeoutSec,
        [Parameter(Mandatory)][ValidateRange(0, 86400)][int]$MaxQueueWaitSec,
        [ValidateRange(0, 86400)][int]$MaxAdmissionWaitSec = $script:DvUmRunMaxAdmissionWaitSec,
        [Parameter(Mandatory)][ValidateRange(0, 86400)][int]$MaxClaimedWaitSec,
        [ValidateRange(0, 86400)][int]$CallerCapSec = 0,
        $CallerDeadlineUtc = $null,
        [switch]$LaterHarvest,
        $NowUtc = $null
    )
    $waits = @($TimeoutSec, $MaxQueueWaitSec, $MaxAdmissionWaitSec, $MaxClaimedWaitSec)
    $waitSec = ($waits | Measure-Object -Sum).Sum
    $longest = ($waits | Measure-Object -Maximum).Maximum
    if ($CallerCapSec -gt 0 -and $longest -gt $CallerCapSec) {
        throw "DVE_NOT_AFTER_WAIT_OVER_CAP a $longest s um-run wait is longer than the caller's $CallerCapSec s cap: the caller would kill um-run first"
    }
    # (-NowUtc is a test seam, -CallerDeadlineUtc a caller's instant: a [DateTime] of any Kind; Local is converted, Unspecified is taken as UTC)
    $toUtc = { param($v) $(if (([datetime]$v).Kind -eq [DateTimeKind]::Local) { ([datetime]$v).ToUniversalTime() } else { [DateTime]::SpecifyKind([datetime]$v, [DateTimeKind]::Utc) }) }
    $now = $(if ($null -eq $NowUtc) { [DateTime]::UtcNow } else { & $toUtc $NowUtc })
    $result = { param($text, $source, $bound, $tolerance, $budget, $cap, $why) [pscustomobject]@{ notAfterUtc = $text; source = $source; boundSec = $bound; waitSec = $waitSec; longestWaitSec = $longest; maxAdmissionWaitSec = $MaxAdmissionWaitSec; anchorUtc = $now.ToString('o'); dropToleranceSec = $tolerance; budgetSec = $budget; capUtc = $cap; why = $why } }
    if ($LaterHarvest) { return (& $result '' 'later-harvest' $null $null $null '' 'a later call harvests this result by name and cannot re-submit its single-use JobId: no notAfter') }
    $uncapped = $waitSec + $script:DvNotAfterSlackSec
    $capText = ''
    if ($null -ne $CallerDeadlineUtc) {
        $deadline = & $toUtc $CallerDeadlineUtc
        if ($deadline -le $now) { throw "DVE_NOT_AFTER_DEADLINE_PASSED the caller's deadline $($deadline.ToString('o')) is not later than now ($($now.ToString('o'))): nothing can be submitted before it" }
        $capText = $deadline.ToString('o')
        if ($deadline -lt $now.AddSeconds($uncapped)) {
            return (& $result $capText 'caller-deadline' ([Math]::Round(($deadline - $now).TotalSeconds, 3)) $null $uncapped $capText "the caller kills um-run at its deadline $capText; the drop writes min(its own now + $uncapped s, that deadline)")
        }
    } elseif ($CallerCapSec -gt 0 -and $CallerCapSec -lt $uncapped) {
        return (& $result $now.AddSeconds($CallerCapSec).ToString('o') 'caller-cap' $CallerCapSec $null $null '' "the caller kills um-run $CallerCapSec s after submit")
    }
    & $result $now.AddSeconds($uncapped).ToString('o') 'um-run-waits' $uncapped $script:DvNotAfterSlackSec $uncapped $capText "um-run waits at most $waitSec s (queue $MaxQueueWaitSec + admission $MaxAdmissionWaitSec + timeout $TimeoutSec + claimed $MaxClaimedWaitSec) from its drop + $($script:DvNotAfterSlackSec) s slack; the drop counts the $uncapped s from its own clock$(if ($capText) { ", capped at $capText" })"
}

function Get-DvUmRunNotAfterArgs {
    <#
    .SYNOPSIS
    Pure. The um-run.ps1 arguments that carry a Get-DvJobNotAfter result (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 r4, Grok G1, card
    NOT-AFTER-BUDGET-NULL-FAIL-CLOSED-1): a budgetSec >= 1 -> NotAfterBudgetSec (plus NotAfterCapUtc when capUtc is set); else a notAfterUtc -> NotAfterUtc;
    else source 'later-harvest' -> none, by design. Anything else would bind -NotAfterBudgetSec to [int] 0, which um-run reads as 'not given', and drop the
    bound silently: DVE_NOT_AFTER_BUDGET_MISSING, before anything is submitted.
    .OUTPUTS
    [hashtable] to splat onto um-run.ps1
    #>
    param([Parameter(Mandatory)]$NotAfter)
    if ($null -ne $NotAfter.budgetSec) {
        if ([int]$NotAfter.budgetSec -lt 1) { throw "DVE_NOT_AFTER_BUDGET_MISSING budgetSec $($NotAfter.budgetSec) would reach um-run as 'not given' and drop the bound (source '$($NotAfter.source)')" }
        $a = @{ NotAfterBudgetSec = [int]$NotAfter.budgetSec }
        if ($NotAfter.capUtc) { $a.NotAfterCapUtc = [string]$NotAfter.capUtc }
        return $a
    }
    if ($NotAfter.notAfterUtc) { return @{ NotAfterUtc = [string]$NotAfter.notAfterUtc } }
    if ($NotAfter.source -eq 'later-harvest') { return @{} }
    throw "DVE_NOT_AFTER_BUDGET_MISSING the bound (source '$($NotAfter.source)') has neither budgetSec nor notAfterUtc; um-run would get no notAfterUtc"
}

function Get-DvUmRunDropVisibleUtc {
    <#
    .SYNOPSIS
    Pure. When a job became visible on the share, read from um-run.ps1's own output (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1): um-run prints
    UmRunDrop.psm1's 'submitted <id> -> <path>' line (Write-Host, the information stream) only after Invoke-UmRunDrop has returned, i.e. after the job file
    is in place, and before it starts its own waits. So the first such information record for -JobId is never earlier than the drop, and judging the bound
    against its time is conservative. A plain output string, another id's line and a missing line are not this job's drop: $null (unmeasured).
    .OUTPUTS
    [DateTime] (Kind Utc) or $null
    #>
    param([object[]]$Records = @(), [Parameter(Mandatory)][string]$JobId)
    $pattern = '^submitted ' + [regex]::Escape($JobId) + ' -> '
    foreach ($r in @($Records)) {
        if ($r -isnot [System.Management.Automation.InformationRecord]) { continue }
        if ([string]$r.MessageData -cmatch $pattern) { return $r.TimeGenerated.ToUniversalTime() }
    }
    $null
}

function Get-DvUmRunDropNotAfter {
    <#
    .SYNOPSIS
    Pure. The notAfterUtc the drop actually wrote into the claim, read from um-run.ps1's own output (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1 r2): for
    the drop-relative form (-NotAfterBudgetSec) UmRunDrop.psm1 returns 'job not-after: <id>.meta.json notAfterUtc=<o> sampledUtc=<o> budgetSec=<n>
    capUtc=<o|none>' and um-run echoes it on the information stream, like the 'submitted' line. The first such record for -JobId; sampledUtc, budgetSec
    and capUtc are $null / '' when the line does not carry them (the absolute form prints notAfterUtc only). Anything else: $null (unreported).
    .OUTPUTS
    [pscustomobject]@{ notAfterUtc ([DateTime] Utc); sampledUtc ([DateTime] Utc or $null); budgetSec ([int] or $null); capUtc ('o', '' for none) } or $null
    #>
    param([object[]]$Records = @(), [Parameter(Mandatory)][string]$JobId)
    $pattern = '^job not-after: ' + [regex]::Escape($JobId) + '\.meta\.json notAfterUtc=(\S+)(?: sampledUtc=(\S+) budgetSec=(\d+) capUtc=(\S+))?$'
    $parse = { param($t) [DateTime]::Parse([string]$t, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime() }
    foreach ($r in @($Records)) {
        if ($r -isnot [System.Management.Automation.InformationRecord]) { continue }
        $m = [regex]::Match([string]$r.MessageData, $pattern)
        if (-not $m.Success) { continue }
        return [pscustomobject]@{
            notAfterUtc = (& $parse $m.Groups[1].Value)
            sampledUtc = $(if ($m.Groups[2].Success) { & $parse $m.Groups[2].Value } else { $null })
            budgetSec = $(if ($m.Groups[3].Success) { [int]$m.Groups[3].Value } else { $null })
            capUtc = $(if ($m.Groups[4].Success -and $m.Groups[4].Value -cne 'none') { (& $parse $m.Groups[4].Value).ToString('o') } else { '' })
        }
    }
    $null
}

function Test-DvNotAfterCoversDrop {
    <#
    .SYNOPSIS
    Pure. Judges a Get-DvJobNotAfter bound against the instant the job actually became visible (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1). A post-hoc
    WITNESS: since r2 the bound is set a priori at the drop (-NotAfterBudgetSec), so on the uncapped path covered must be true, and
    DVE_NOT_AFTER_DROP_LATE there is a DEFECT signal (the drop did not count the budget from the clock read it reports), never an expected outcome of a slow
    drop or of a stall after visibility. The value judged is the one the agent got: for the drop-relative form (budgetSec set) the drop's own reported
    notAfterUtc (-DropNotAfter, from Get-DvUmRunDropNotAfter), else the bound's notAfterUtc. The waits are judged from the instant um-run counts them from:
    r6 (Grok G7), for a drop-relative value that carries sampledUtc, that sampledUtc (um-run.ps1 r5 anchors its queue and admission deadlines there), else
    the drop (um-run's 'submitted' line, never earlier than visibility: conservative). For an uncapped bound ('um-run-waits') covered is exactly notAfterUtc
    >= that instant + every um-run wait (waitSec); marginSec is notAfterUtc - (that instant + waitSec): negative is how much earlier than um-run's last wait
    the agent stops taking the job, positive is how long past it the agent may still take it (at most the slack). dropDelaySec is still the drop's delay after
    the anchor. An unmeasured drop, or a drop-relative value the drop did not report, is covered = $null: unknown, never covered. A drop-relative
    value equal to its cap, and a bound the caller kills um-run at ('caller-cap', 'caller-deadline'), are covered whatever the drop time: nobody waits past
    them. A later harvest passes no bound: nothing to cover. A bound that is not covered says DVE_NOT_AFTER_DROP_LATE in why.
    .OUTPUTS
    [pscustomobject]@{ jobId; notAfterUtc (the value judged, '' if unreported); form ('drop-relative', 'absolute', 'none'); budgetSec; capUtc; source;
    anchorUtc; waitSec; dropToleranceSec; dropSampledUtc ('o' or ''); dropVisibleUtc ('o' or ''); dropDelaySec; marginSec; covered; why }
    #>
    param([Parameter(Mandatory)]$NotAfter, $DropVisibleUtc = $null, $DropNotAfter = $null, [string]$JobId = '')
    $parse = { param($t) [DateTime]::Parse([string]$t, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::RoundtripKind).ToUniversalTime() }
    $anchor = & $parse $NotAfter.anchorUtc
    $drop = $(if ($null -eq $DropVisibleUtc) { $null } elseif (([datetime]$DropVisibleUtc).Kind -eq [DateTimeKind]::Local) { ([datetime]$DropVisibleUtc).ToUniversalTime() } else { [DateTime]::SpecifyKind([datetime]$DropVisibleUtc, [DateTimeKind]::Utc) })
    $delay = $(if ($null -eq $drop) { $null } else { [Math]::Round(($drop - $anchor).TotalSeconds, 3) })
    $margin = $null
    $relative = $null -ne $NotAfter.budgetSec
    $form = $(if ($NotAfter.source -ceq 'later-harvest') { 'none' } elseif ($relative) { 'drop-relative' } else { 'absolute' })
    $judged = $(if (-not $relative) { if ($NotAfter.notAfterUtc) { & $parse $NotAfter.notAfterUtc } else { $null } }
        elseif ($null -ne $DropNotAfter) { [DateTime]::SpecifyKind([datetime]$DropNotAfter.notAfterUtc, [DateTimeKind]::Utc) } else { $null })
    $sampled = $(if ($relative -and $null -ne $DropNotAfter -and $null -ne $DropNotAfter.sampledUtc) { [DateTime]::SpecifyKind([datetime]$DropNotAfter.sampledUtc, [DateTimeKind]::Utc).ToString('o') } else { '' })
    $capped = $relative -and $null -ne $judged -and [string]$NotAfter.capUtc -and $judged -eq (& $parse $NotAfter.capUtc)
    if ($NotAfter.source -ceq 'later-harvest') {
        $covered = $true; $why = 'no notAfterUtc was passed: the agent has nothing to refuse the job on'
    } elseif ($NotAfter.source -cin @('caller-cap', 'caller-deadline')) {
        $covered = $true; $why = 'the caller kills um-run at notAfterUtc: nobody waits past it, whenever the job became visible (a late drop shortens the waits, never the bound)'
    } elseif ($null -eq $judged) {
        $covered = $null; $why = "the drop reported no notAfterUtc for $JobId (no 'job not-after:' line from um-run): the bound is not proven to cover um-run's waits"
    } elseif ($capped) {
        $covered = $true; $why = "the drop capped notAfterUtc at the caller's deadline $($NotAfter.capUtc): the caller kills um-run there, so nobody waits past it"
    } elseif ($null -eq $drop) {
        $covered = $null; $why = "the drop is unmeasured (um-run reported no drop line for $JobId): the bound is not proven to cover um-run's waits"
    } else {
        # r6 (Grok G7): um-run counts a drop-relative value's waits from the drop's sampledUtc, so a stall after visibility moves no wait
        $waitsFrom = $(if ($sampled) { & $parse $sampled } else { $drop })
        $margin = [Math]::Round(($judged - $waitsFrom.AddSeconds([double]$NotAfter.waitSec)).TotalSeconds, 3)
        $covered = $margin -ge 0
        $why = $(if ($relative -and $covered) { "the drop counted notAfterUtc from its own clock$(if ($sampled) { " ($sampled), which um-run counts its waits from" }), the job visible $delay s after the bound's anchor: every um-run wait ends before notAfterUtc" }
            elseif ($relative) { "DVE_NOT_AFTER_DROP_LATE (defect: the drop did not count the budget from $(if ($sampled) { "the clock read it reports ($sampled)" } else { "its own clock" })) the drop reported notAfterUtc $($judged.ToString('o')), $(-$margin) s short of $(if ($sampled) { 'that read' } else { "the job's visibility ($delay s after the anchor)" }) + every um-run wait" }
            elseif ($covered) { "the job was visible $delay s after the bound's anchor, inside its $($NotAfter.dropToleranceSec) s tolerance: every um-run wait ends before notAfterUtc" }
            else { "DVE_NOT_AFTER_DROP_LATE the job was visible $delay s after the bound's anchor, past its $($NotAfter.dropToleranceSec) s tolerance: the agent stops taking it $(-$margin) s before um-run's last wait ends" })
    }
    [pscustomobject]@{
        jobId = $JobId; notAfterUtc = $(if ($null -eq $judged) { '' } else { $judged.ToString('o') }); form = $form; budgetSec = $NotAfter.budgetSec; capUtc = [string]$NotAfter.capUtc
        source = $NotAfter.source; anchorUtc = $NotAfter.anchorUtc; waitSec = $NotAfter.waitSec; dropToleranceSec = $NotAfter.dropToleranceSec; dropSampledUtc = $sampled
        dropVisibleUtc = $(if ($null -eq $drop) { '' } else { $drop.ToString('o') }); dropDelaySec = $delay; marginSec = $margin; covered = $covered; why = $why
    }
}

# --- health (P5) ----------------------------------------------------------------------------------
function Get-DvHealthVerdict {
    <#
    .SYNOPSIS
    Judge a health probe against a venue's thresholds. A missing or unparsable measurement is UNHEALTHY --
    unknown is never healthy. VENUE-SESSION-LOCKED-REFUSAL-1: a console session that is locked, or whose lock state
    is unknown (sessionLocked absent, null, or anything but a JSON false), is UNHEALTHY with .detail SESSION_LOCKED,
    so the leg is never submitted. Signing in is an owner action, so a retry cannot clear it.
    #>
    param($Probe, $Thresholds)
    $reasons = [System.Collections.Generic.List[string]]::new()
    $detail = $null
    if ($null -eq $Probe) {
        $reasons.Add('health probe returned no measurements')
    } else {
        $locked = $Probe.PSObject.Properties['sessionLocked']
        if ($null -eq $locked -or -not ($locked.Value -is [bool]) -or $locked.Value) {
            $detail = 'SESSION_LOCKED'
            $state = $(if ($null -ne $locked -and $locked.Value -is [bool]) { 'locked' } else { 'unknown' })
            $reasons.Add("SESSION_LOCKED: the venue console session is $state (owner-only: signing in is an owner action)")
        }
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
    [pscustomobject]@{ healthy = ($reasons.Count -eq 0); reasons = @($reasons); detail = $detail }
}

function New-DvHealthProbeJobText {
    <#
    .SYNOPSIS
    The job that runs ON the venue for the health probe: pwsh cold start, write+hash of a fixed 4 MiB
    buffer inside the agent root, free disk of the agent-root drive, commit charge, host identity, GPU,
    and the PresentMon digest. Prints ONE line `DVE_PROBE=<json>`. Touches only the agent root; it
    enumerates no drive root and no directory. VENUE-SESSION-LOCKED-REFUSAL-1: also records sessionLocked
    ($true/$false/$null) from the SAME read the attribution job gates on -- Get-AttrCudaSessionLocked's text is
    embedded verbatim from AttrCudaArtifacts.psm1. Read-only; it sends no input.
    #>
    param([Parameter(Mandatory)][string]$AgentRoot)
    $root = $AgentRoot.Replace("'", "''")
    Import-Module (Join-Path $PSScriptRoot '..\bachelor\AttrCudaArtifacts.psm1')
    $sessionLockedSource = Get-AttrCudaEmbeddedFunctionSource -Name @('Get-AttrCudaSessionLocked')
    $body = @"
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
"@
    # The embedded function is concatenated, never interpolated: its own `$` must reach the venue as written.
    $body + "`r`n" + $sessionLockedSource + "`r`n" + '$probe.sessionLocked = Get-AttrCudaSessionLocked' + "`r`n" +
        'Write-Output (''DVE_PROBE='' + ($probe | ConvertTo-Json -Compress -Depth 4))' + "`r`n"
}

function ConvertFrom-DvProbeStdout([string]$Stdout) {
    foreach ($line in ($Stdout -split "`r?`n")) {
        if ($line.StartsWith('DVE_PROBE=')) {
            try { return ($line.Substring('DVE_PROBE='.Length) | ConvertFrom-Json) } catch { return $null }
        }
    }
    $null
}

# --- registry protection: REMOVED (round 3, fable hardening) --------------------------------------
# Round 2 snapshotted and restored the venue's HKCU\Software\magiclantern.MLVApp QSettings around every leg (two extra um-run
# submissions, a .reg export of the venue's persisted settings left on the agent share). Master (PLAYBACK-CLIP-LENGTH-ENFORCE-4,
# platform/qt/main.cpp automation_settings::isolate) now gives an automation run its OWN run-scoped settings store, so the app
# neither reads nor rewrites the venue's registry. The snapshot protected nothing master does not already guarantee and itself
# left venue settings on the share. In its place the RECEIPT refuses a run whose log does not say it used the run-scoped store
# (Get-DvSmokeSummaryFields -> playback.settingsIsolated -> SETTINGS_NOT_ISOLATED), which also covers a build that predates the
# isolation -- a build the snapshot would have "protected" only after the fact.

# --- job result -> typed outcome (P4) -------------------------------------------------------------
$script:VenueConditionResults = @('SCREENSAVER_SECURE_OWNER_ONLY', 'DISPLAY_WAKE_DISMISS_FAILED', 'KEEPALIVE_FAILED', 'DISPLAY_ASLEEP', 'SESSION_LOCKED_OWNER_ONLY')
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
    # DVE-LEG-TERMINALS-1 item 3: the smoke run itself failed (a validation gate, a launch failure). With the run log now published the playback proof may be sound, but a failed
    # run is still no product result in either backend (and carries no counters): INVALID, with the typed cause in the detail.
    if ($ResultToken -eq 'SMOKE_RUN_FAILED') { return [pscustomobject]@{ outcome = 'INVALID'; detail = ('SMOKE_RUN_FAILED' + $reasonSuffix) } }
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
    FAILS the criterion -- a missing number is never a passing number; when the metrics carry a
    non-empty <metric>CouldNotCheck, that typed reason is the failure text. An empty list is "no gating
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
        if ($null -eq $prop -or $null -eq $prop.Value) {
            # GPU-DEVICE-LOSS-SELF-HEAL-1 r10: a producer that could not read a metric names why in <metric>CouldNotCheck; the verdict carries that typed reason.
            $why = $(if ($null -ne $Metrics) { $Metrics.PSObject.Properties["$($c.metric)CouldNotCheck"] } else { $null })
            if ($null -ne $why -and -not [string]::IsNullOrWhiteSpace([string]$why.Value)) { $failures.Add("$($c.metric): $($why.Value)") }
            else { $failures.Add("$($c.metric): metric absent") }
            continue
        }
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

# PRESENTMON-BACHELOR-CUDA-INTERNAL-GAP-1: the receipt-level PresentMon gap bound. It is the job's own temporal ceiling
# (playback-attr-3-cuda-job.ps1 $presentMonSufficiencyMaxGapMs, PRESENTMON-HARNESS-ROBUSTNESS-2 r1b; test_presentmon_playback_window_gap.py pins
# the two equal). Measured inside the app's swap window, 12 sampled Bachelor CUDA legs read 70-184 ms (worst bracket end) and the app's
# own largest swap gap there and on the r4 stalled leg was 96.4 ms, so the bound never trips on a healthy leg. A gap past it is EITHER the app's own
# playback stall (PR #788 r1 opus B3: 13 of 2,889 local run logs carry an app max_gap_ms >= 5000 inside the swap window, e.g. 21359.466 ms) OR lost
# PresentMon rows. The same swap record's max_gap_ms tells them apart: a gap it accounts for is a real stall that is counted (the leg FAILs);
# only a gap it does not account for is PresentMon's, and then GPU timing on the presents in it is unreachable, so the leg is no measurement.
$script:PresentMonGapBoundMs = 5000.0
# How far the PresentMon gap may sit from the app's own max_gap_ms and still be the same stall. PresentMon's rows here are the DISPLAYED presents
# (msBetweenDisplayChange > 0), so a present beside the stall that was not displayed widens the PresentMon gap by one frame interval (41.7 ms at
# 24 fps), and a present call's timestamp sits a few ms from the app's swap stamp. 250 ms (six 24 fps frames) is the job's own tolerance for
# matching PresentMon present rows to the app's swap stamps (playback-attr-3-cuda-job.ps1 $presentMonJobStopMaxEdgeGapMs), 5% of the bound.
$script:PresentMonAppStallToleranceMs = 250.0

function Get-DvPresentMonGapVerdict {
    <#
    .SYNOPSIS
    PRESENTMON-BACHELOR-CUDA-INTERNAL-GAP-1: does this leg's PresentMon capture have a gap longer than the bound? Read from the receipt's verbatim
    metrics only: presentMonStatus, presentMonTemporalMaxGapMs, presentMonTemporalGapKind, presentMonTemporalWindowSource.
    NOT_APPLICABLE (no block) when presentMonStatus is not 'ok' or 'degraded'. A CPU leg's PresentMon is informational and reads 'unavailable',
    and the DISPLAY_ASLEEP override reads 'unavailable' too. Otherwise FAIL CLOSED:
      PRESENTMON_METRICS_UNTYPED (PR #788 r2 sol A2) presentMonStatus, presentMonTemporalGapKind or presentMonTemporalWindowSource present but not a
                                 [string], or presentMonTemporalOverBoundGapAtAppStall present but not a [bool]: nothing is compared, it blocks;
      PRESENTMON_GAP_UNMEASURED  a usable status with no finite non-negative gap;
      APP_SWAP_STALL             (PR #788 r1 opus B3) a gap over the bound inside the app's swap window that the app's own swap record accounts for:
                                 the gap is internal (between two presents; a head or tail gap is never an inter-swap gap), it is the only gap
                                 over the bound at every permitted origin (presentMonTemporalOverBoundGapCount = 1: one max_gap_ms explains one
                                 gap), it is within $script:PresentMonAppStallToleranceMs of presentMonTemporalAppSwapMaxGapMs, that app gap is
                                 itself over the bound (CoS G8), and presentMonTemporalOverBoundGapAtAppStall is not $false (the gap sits at the
                                 app's own longest-gap swap ordinal when the record carries one; $null is size-only). A real
                                 playback stall: blocks = $false, failsLeg = $true (the leg is counted, as a FAIL);
      PRESENTMON_INTERNAL_GAP    a gap over the bound inside the app's swap window that the app's record does not account for (no, or an
                                 unparseable, app max gap fails closed here: it is never assumed to explain the gap);
      PRESENTMON_GAP_UNLOCATED   a gap over the bound measured over the process lifetime (no swap window, or a job before this card).
                                 The gap may be the app's own pre-roll, but nothing places it outside playback, so it is not counted;
      WITHIN_BOUND               no block.
    Pure: the runner gates on it after the verbatim metrics are copied, and Test-DvReceiptValid re-derives it from the verified metrics.
    adversary r4 (78b6b9b2) A1: -Backend is mandatory. On the cpu backend PresentMon is informational and never gating, whatever status the job
    published (its cpu override rewrites only a non-OK display report, so a 'degraded' capture can reach here): NOT_APPLICABLE. Any other
    backend, including a missing one, is judged (fail closed). The writer passes the leg's backend; the validator the one the hashed summary derives.
    #>
    param(
        [AllowNull()]$Metrics,
        [Parameter(Mandatory = $true)][AllowNull()][AllowEmptyString()][string]$Backend
    )
    $bound = $script:PresentMonGapBoundMs
    $tolerance = $script:PresentMonAppStallToleranceMs
    $rawStatus = Get-DvProp $Metrics 'presentMonStatus'
    $status = [string]$rawStatus
    $rawGap = Get-DvProp $Metrics 'presentMonTemporalMaxGapMs'
    $kind = Get-DvProp $Metrics 'presentMonTemporalGapKind'
    $source = Get-DvProp $Metrics 'presentMonTemporalWindowSource'
    $rawAppGap = Get-DvProp $Metrics 'presentMonTemporalAppSwapMaxGapMs'
    $rawOverCount = Get-DvProp $Metrics 'presentMonTemporalOverBoundGapCount'
    $atAppStall = Get-DvProp $Metrics 'presentMonTemporalOverBoundGapAtAppStall'
    # PR #788 r2 sol A2 (fable hardening DV-PRESENTMON-GAP-VERDICT-STRING-TYPED-OPERANDS-1): a Boolean or number on the LEFT of -ceq converts the
    # right operand to its own type, so a JSON true in source or kind read as 'app_swap_window' / 'internal'. Every string field this verdict reads
    # must be a [string] (or absent), and the placement field a [bool] (or absent); anything else is a blocking typed refusal, never compared.
    $untyped = @(foreach ($field in @(@('presentMonStatus', $rawStatus), @('presentMonTemporalGapKind', $kind), @('presentMonTemporalWindowSource', $source))) {
        if ($null -ne $field[1] -and -not ($field[1] -is [string])) { "$($field[0]) is a $($field[1].GetType().Name) ('$($field[1])'), not a string" }
    })
    if ($null -ne $atAppStall -and -not ($atAppStall -is [bool])) { $untyped += "presentMonTemporalOverBoundGapAtAppStall is a $($atAppStall.GetType().Name) ('$atAppStall'), not a Boolean" }
    $gap = $null
    if ($rawGap -is [double] -or $rawGap -is [int] -or $rawGap -is [long] -or $rawGap -is [decimal]) {
        $d = [double]$rawGap
        if (-not [double]::IsNaN($d) -and -not [double]::IsInfinity($d) -and $d -ge 0) { $gap = $d }
    }
    $appGap = $null
    if ($rawAppGap -is [double] -or $rawAppGap -is [int] -or $rawAppGap -is [long] -or $rawAppGap -is [decimal]) {
        $d = [double]$rawAppGap
        if (-not [double]::IsNaN($d) -and -not [double]::IsInfinity($d) -and $d -ge 0) { $appGap = $d }
    }
    $overCount = $null
    if ($rawOverCount -is [int] -or $rawOverCount -is [long]) { $overCount = [long]$rawOverCount }
    $verdict = 'WITHIN_BOUND'
    $reason = $null
    if ($Backend -ceq 'cpu') {
        $verdict = 'NOT_APPLICABLE'
        $reason = "the leg ran on the cpu backend, where PresentMon is informational and never gating (presentMonStatus '$status')"
    }
    elseif ($untyped.Count -gt 0) {
        $verdict = 'PRESENTMON_METRICS_UNTYPED'
        $reason = "PRESENTMON_METRICS_UNTYPED: $($untyped -join '; '), so the gap's window and kind are not established and it is not judged"
    }
    elseif ($status -cnotin @('ok', 'degraded')) {
        $verdict = 'NOT_APPLICABLE'
        $reason = "presentMonStatus is '$status', not ok or degraded: no PresentMon cadence to bound"
    }
    elseif ($null -eq $gap) {
        $verdict = 'PRESENTMON_GAP_UNMEASURED'
        $reason = "PRESENTMON_GAP_UNMEASURED: presentMonStatus is '$status' but presentMonTemporalMaxGapMs is '$rawGap', not a finite non-negative number"
    }
    elseif ($gap -gt $bound -and $source -ceq 'app_swap_window' -and $kind -ceq 'internal' -and $overCount -eq 1 -and $null -ne $appGap -and $appGap -gt $bound -and [math]::Abs($gap - $appGap) -le $tolerance -and $atAppStall -ne $false) {
        # CoS G8: the app's own gap must itself be over the bound (a sub-bound app stall is not the one over-bound gap, whatever its size), and when the
        # record places it (max_gap_before_serial) PresentMon's gap must sit there ($false refuses; $null, a record without the ordinal, is size-only).
        $verdict = 'APP_SWAP_STALL'
        $reason = "APP_SWAP_STALL: a $([math]::Round($gap, 1)) ms internal PresentMon gap inside the app's swap window is the app's own stall: its swap record's max_gap_ms is $([math]::Round($appGap, 1)) ms (over the bound, within $([math]::Round($tolerance)) ms)$(if ($atAppStall -eq $true) { ' at the same swap ordinal' } else { '' }) and it is the only gap over the $([math]::Round($bound)) ms bound, so PresentMon lost nothing and the leg is counted as a playback stall"
    }
    elseif ($gap -gt $bound) {
        $appNote = $(if ($source -cne 'app_swap_window') { '' } elseif ($null -eq $appGap) { "; the app's swap record carries no usable max_gap_ms ('$rawAppGap'), so nothing explains it" } else { "; the app's own swap record does not account for it (max_gap_ms $([math]::Round($appGap, 1)) ms$(if ($appGap -le $bound) { ' is not over the bound' } else { '' }), gap kind '$kind', $(if ($null -eq $overCount) { 'no' } else { $overCount }) gap(s) over the bound$(if ($atAppStall -eq $false) { ', not at the app''s own longest-gap swap ordinal' } else { '' }))" })
        $where = $(if ($source -ceq 'app_swap_window') { "inside the app's swap window" } else { "over the MLVApp process lifetime (window source '$source'), so nothing places it outside playback" })
        $verdict = $(if ($source -ceq 'app_swap_window') { 'PRESENTMON_INTERNAL_GAP' } else { 'PRESENTMON_GAP_UNLOCATED' })
        $reason = "${verdict}: a $([math]::Round($gap, 1)) ms $kind PresentMon gap $where exceeds the $([math]::Round($bound)) ms bound$appNote; GPU timing on the presents in it is unreachable"
    }
    [ordered]@{ verdict = $verdict; blocks = ($verdict -cin @('PRESENTMON_METRICS_UNTYPED', 'PRESENTMON_GAP_UNMEASURED', 'PRESENTMON_INTERNAL_GAP', 'PRESENTMON_GAP_UNLOCATED')); failsLeg = ($verdict -ceq 'APP_SWAP_STALL')
        boundMs = $bound; gapMs = $gap; gapKind = $kind; windowSource = $source; appSwapMaxGapMs = $appGap; overBoundGapCount = $overCount; overBoundGapAtAppStall = $atAppStall; reason = $reason }
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

# HFR-NATIVE-60FPS-FULLRES-1 r19 (sol r18 B2): the measured conform fields a leg's criteria may name, and the re-derived playback block field each comes from.
# r29 (adversary A2 on cf403dba): the frozen-tail allowance is no longer a constant (r28's 10); Get-DvCriteriaMetrics derives it from the conform line's ends.
$script:ConformCriteriaMetrics = [ordered]@{ conformSourceFps = 'conformSourceFps'; conformMaxJump = 'conformMaxJump'; conformAudioSyncRequests = 'conformAudioSyncRequests' }

function Get-DvCriteriaMetrics {
    <#
    .SYNOPSIS
    HFR-NATIVE-60FPS-FULLRES-1 r19 (sol r18 B2). The metrics a leg's criteria are judged against: the verbatim summary.json / manifest metrics
    (Get-DvVerbatimMetrics; the receipt's `metrics` stays exactly that) plus the measured session's conform line values from the playback block
    Get-DvPlaybackEvidence re-derived from the run log: conformSourceFps (source_fps), conformMaxJump (max_jump), conformAudioSyncRequests
    (audio_sync_requests). Before r19 the criteria saw only summary.json, so a conformed run at source_fps=33.6, max_jump=2, audio_sync_requests=1
    passed its conform leg. A value the run log does not carry (no conform line for the measured session, or a build that does not print the field)
    is left absent with a typed <metric>CouldNotCheck, so a criterion naming it FAILS (Test-DvCriteria). A summary.json key of the same name is never
    overwritten by the playback value, and never trusted in its place: the playback value or the CouldNotCheck reason replaces it.
    #>
    param($Metrics, $Playback)
    $m = [ordered]@{}
    if ($null -ne $Metrics) { foreach ($p in $Metrics.PSObject.Properties) { $m[$p.Name] = $p.Value } }
    # HFR-NATIVE-60FPS-FULLRES-1 r24 (r23 adversary A1, DV-CONFORM-CRITERIA-METRICS-PRESENCE-GUARD-1): a conform value counts only when the block
    # says its conform line is PRESENT (a real boolean $true, as Get-DvConformPaceProof requires) and is the MEASURED session's (conformSession
    # equals summarySession, both non-empty). Before r24 a block with conformLinePresent=$false or another session's line still passed its values.
    $linePresent = $(if ($null -ne $Playback) { Get-DvProp $Playback 'conformLinePresent' } else { $null })
    $present = ($linePresent -is [bool]) -and $linePresent
    $summarySession = $(if ($null -ne $Playback) { [string](Get-DvProp $Playback 'summarySession') } else { '' })
    $conformSession = $(if ($null -ne $Playback) { [string](Get-DvProp $Playback 'conformSession') } else { '' })
    $sameSession = ($summarySession -ne '') -and ($conformSession -ceq $summarySession)
    foreach ($name in $script:ConformCriteriaMetrics.Keys) {
        $value = $(if ($null -ne $Playback -and $present -and $sameSession) { Get-DvProp $Playback $script:ConformCriteriaMetrics[$name] } else { $null })
        $m[$name] = $value
        if ($null -eq $value) {
            $why = $(if ($null -eq $Playback) { 'no playback block was derived from the run log' }
                     elseif (-not $present) { 'no playback_smoke.conform line for the measured session in the run log' }
                     elseif (-not $sameSession) { "the playback_smoke.conform line is for session $conformSession, not the measured session $summarySession" }
                     else { 'the measured session''s playback_smoke.conform line does not carry the field' })
            $m["${name}CouldNotCheck"] = $why
        }
        elseif ($m.Contains("${name}CouldNotCheck")) { $m.Remove("${name}CouldNotCheck") }
    }
    # HFR-NATIVE-60FPS-FULLRES-1 r28 fix iteration 1 (adversary A2 on e052947c): a frozen presentation tail never passes. max_jump and source_fps are
    # measured between the first and the LAST presented frame, so a run that stops presenting at frame 1400 while the engine (the debt cap skipping)
    # reaches 1499 reads max_jump=1, source_fps=24. The conform line's source_advanced is what the presented frames covered; when the engine's
    # source_advanced leads it by more than the allowance below, the frames past the last present were never shown: one jump the tracker could
    # not see, and conformMaxJump becomes that shortfall (conformFrozenTailFrames names it). Within the allowance nothing changes (r28's
    # carried-debt-cap tolerance is gone since r29, below). Fix iteration 2 (adversary A4 on 4201a437): coverage the line does not carry, or carries unreadable, is
    # a missing number, never a passing one -- conformMaxJump is then left absent with a typed CouldNotCheck, so its criterion FAILS (the app prints
    # source_advanced on every conform line, MainWindow.cpp playback_smoke.conform).
    #
    # r29 (adversary A2 on cf403dba): the tolerance of 10 assumed the carried debt (up to 8) was in the engine's source_advanced; it is not
    # (the debt is granted time the engine has not advanced by), so a tail of 9 or 10 frames never shown passed. The allowance is now the
    # exact figure the conform line's ends give: engine_target (H, the summary's own coordinates, source_advanced = H - start + 1) and the
    # painted first_frame (F) and last_frame (L). A fully shown run falls short by exactly 1 (source_advanced counts the start frame), plus
    # 1 when the engine holds one target not yet painted (H > L: conform never moves past an unpainted target), plus 1 when the first
    # painted frame is the one after the start (the start frame was on screen before Play). Every frame short beyond that was never shown.
    # Any of the three absent, unreadable or negative is a missing number: conformMaxJump is left absent, so its criterion FAILS.
    #
    # r30 (Dot D2 on 71104385): engine_target is the engine's HIGH-WATER target, and a seek back does not lower it, so a healthy Loop-off run that
    # paints 0..1498, seeks back to 1398 and paints through 1448 (E=1499, H=1498, F=0, L=1448, C=1548) read 49 frozen frames that were all shown;
    # and a run that froze at 1448 while the engine reached 1498, then sought back, paints the same ends. Neither the ends nor the count can tell
    # them apart, so evidence with a backward step in the painted frames (backward > 0) is not judged: conformMaxJump is left absent with a typed
    # CouldNotCheck and its criterion FAILS. A backward count absent, unreadable or negative is a missing number the same way. No conform leg seeks.
    $backward = $(if ($null -ne $Playback -and $present -and $sameSession) { ConvertTo-DvInt64 (Get-DvProp $Playback 'conformBackward') } else { $null })
    $coverage =$(if ($null -ne $Playback -and $present -and $sameSession) { ConvertTo-DvInt64 (Get-DvProp $Playback 'conformSourceAdvanced') } else { $null })
    $engineAdvanced = $(if ($null -ne $Playback) { ConvertTo-DvInt64 (Get-DvProp $Playback 'sourceAdvanced') } else { $null })
    $ends = [ordered]@{}
    foreach ($e in @(@('conformFirstFrame', 'first_frame'), @('conformLastFrame', 'last_frame'), @('conformEngineTarget', 'engine_target'))) {
        $v = $(if ($null -ne $Playback -and $present -and $sameSession) { ConvertTo-DvInt64 (Get-DvProp $Playback $e[0]) } else { $null })
        $ends[$e[1]] = $(if ($null -ne $v -and $v -ge 0) { $v } else { $null })
    }
    $missingEnd = @($ends.Keys | Where-Object { $null -eq $ends[$_] } | Select-Object -First 1)
    $maxJump = ConvertTo-DvInt64 $m['conformMaxJump']
    $backwardMissing = ($null -eq $backward) -or ($backward -lt 0)
    if ($null -ne $maxJump -and ($null -eq $coverage -or $null -eq $engineAdvanced -or $missingEnd.Count -gt 0 -or $backwardMissing -or $backward -gt 0)) {
        $m['conformMaxJump'] = $null
        $m['conformMaxJumpCouldNotCheck'] = $(if ($null -eq $coverage) { 'the measured session''s playback_smoke.conform line carries no readable source_advanced (the presented coverage), so a frozen presentation tail cannot be ruled out' }
                                              elseif ($null -eq $engineAdvanced) { 'the run log''s summary carries no readable source_advanced (the engine''s advance), so a frozen presentation tail cannot be ruled out' }
                                              elseif ($missingEnd.Count -gt 0) { "the measured session's playback_smoke.conform line carries no readable non-negative $($missingEnd[0]) (the painted coverage's ends and the engine's last target), so a frozen presentation tail cannot be ruled out" }
                                              elseif ($backwardMissing) { 'the measured session''s playback_smoke.conform line carries no readable non-negative backward (the painted frames'' backward steps), so a seek back cannot be ruled out and the tail cannot be judged' }
                                              else { "the painted frames stepped backward $backward time(s) (a seek back): engine_target is the engine's high-water target, not its final one, and frames painted before the seek re-count, so a frozen tail cannot be told from a shown one; backward-seek evidence is not judged" })
    }
    elseif ($null -ne $maxJump) {
        $first = [int64]$ends['first_frame']; $last = [int64]$ends['last_frame']; $target = [int64]$ends['engine_target']
        $engineStart = $target - [int64]$engineAdvanced + 1
        $held = [Math]::Min([Math]::Max($target - $last, 0L), 1L)
        $startShown = [Math]::Min([Math]::Max($first - $engineStart, 0L), 1L)
        $allowed = 1L + $held + $startShown
        $shortfall = [int64]$engineAdvanced - [int64]$coverage
        # Fix iteration 1 (adversary A1 on 88eea505): the coverage counts forward steps, so a seek back re-counts the frames painted again (0..1448,
        # back to 1398, 1398..1448 reads 1498) and the count alone hid 49 unshown tail frames. The ends judge the tail and the head directly: the
        # engine's last target past the last painted frame, beyond the one held target, and the first painted frame past the engine's start,
        # beyond the start frame. The larger of the two readings stands.
        $byEnds = [Math]::Max($target - $last - 1L, 0L) + [Math]::Max($first - $engineStart - 1L, 0L)
        $unshown = [Math]::Max($shortfall - $allowed, $byEnds)
        if ($unshown -gt 0 -and ($unshown + 1) -gt $maxJump) {
            $m['conformMaxJump'] = $unshown + 1
            $m['conformFrozenTailFrames'] = $unshown
            $m['conformTailAllowanceFrames'] = $allowed
        }
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
        health = [ordered]@{ outcome = $null; pwshColdStartMs = $null; smallHashMs = $null; freeDiskGiB = $null; commitUsedGiB = $null; commitLimitGiB = $null; sessionLocked = $null }
        outcome = $null
        outcomeDetail = $null
        # Round 1 of DUAL-VENUE-EVIDENCE-2: the receipt names its local evidence directory and the sha256 of EACH file in it
        # (summary.json, evidence-manifest.json, result.json, logs/smoke-run.log, um-run.json); Test-DvReceiptValid re-hashes them and
        # re-derives the playback block and the outcome from them. A receipt whose evidence is absent is INCOMPLETE, never PASS/FAIL.
        evidence = [ordered]@{ summaryJsonSha256 = $null; evidenceManifestSha256 = $null; resultJsonSha256 = $null; logSha256 = $null; umRunJsonSha256 = $null
                               contactFramesJsonSha256 = $null; smokeStderrSha256 = $null; smokeStdoutSha256 = $null; localEvidenceDir = $null; artifactIndexPath = $null; umRunOutcome = $null }
        metrics = $null
        # Round 2: the receipt oracle's verdict (source_advanced / required_source_frames / run nonce / wrap / clip id).
        # A receipt that says PASS or FAIL without a valid one is INVALID (Test-DvReceiptValid).
        playback = $null
        # DVE-SCALE2-LOOK-LEG-1 r2: the playback scale the leg requested and the scale the app actually rendered at (Get-DvScaleEvidence). Present on EVERY receipt
        # that names a leg spec (effectiveScale 'UNKNOWN' until a run log says); a PASS/FAIL without it is INCOMPLETE and one that misstates it is INVALID.
        scale = $null
        # DUAL-VENUE-DISPLAY-MATRIX-1: the display mode the leg requested, the one the app ran in and the window it presented in (Get-DvDisplayEvidence), next to the scale block.
        # Present on EVERY receipt that names a leg spec (observedMode 'UNKNOWN' until a run log says). A leg that requested a mode the app did not run in is never a PASS/FAIL.
        display = $null
        # LOOK-FULLSCREEN-PARITY-1: the "current look" label the app showed (Get-DvLookLabelEvidence), next to the display block. Informational, never a gate.
        lookLabel = $null
        # VENUE-NATIVE-LEG-LOOKLEG-LABEL-1 r1: did Look Assist actually run in this leg, and with which flavor? FAIL CLOSED: false / null until Invoke-VenueLeg shows the
        # app's own log AND the job's summary agree (unknown is not on). SEMANTICS (Dot bd11a92, r2): false / null mean NOT PROVEN ON -- never "observed off"; a consumer
        # must not read lookAssistOn=false as "Look Assist was off" (the shipped default is on, and an unasked-for, ambiguous or unreported leg also says false).
        # A receipt claiming true is re-derived by Test-DvReceiptValid; false / null / absent claim nothing and are not. Separate from metrics.lookLeg / subject.lookFlavor, which stay the leg TYPE binding
        # (a native HFR pace leg forces the look yet is a speed leg, so those two say false / null for it by design).
        lookAssistOn = $false
        lookAssistFlavor = $null
        look = $null
        # PLAYBACK-VISUAL-INSPECTION-1: a speed leg's window captures (Read-DvVisualCaptures): requested / opted out, the frames and their hashes,
        # the distinct-picture count, the play-vs-paused difference, the strip, and the gate verdict. Null on a look leg (its contact sheet is the look).
        visual = $null
        # Retired in round 3 (no registry snapshot is taken any more: master isolates an automation run's settings store; the
        # receipt's playback.settingsIsolated is the proof). The key stays null so a reader written against round 2 still parses.
        registry = $null
        # Round 3: what the leg was admitted ON (mode production|offline-test, the committed consent-file and venue-table blob
        # ids, the committing HEAD, the owner line's sha256). A PASS/FAIL without it is INVALID (Test-DvReceiptValid).
        admission = $null
        # Stamped by Write-DvReceipt on a PASS/FAIL: what the validator said when the receipt was written (ADVISORY for production). Never an input.
        verification = $null
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
    param([Parameter(Mandatory)]$Receipt, [Parameter(Mandatory)][string]$ReceiptRoot, [string]$RepoRoot = '', [switch]$OfflineTestMode, [byte[]]$LegSpecBytes = $null)
    if ($Receipt['outcome'] -notin $script:OutcomeEnum) { throw "DVE_RECEIPT_OUTCOME_INVALID '$($Receipt['outcome'])' is not one of: $($script:OutcomeEnum -join ', ')" }
    if ($Receipt['outcome'] -in @('PASS', 'FAIL')) {
        # The writer never records a signal without its proof, whatever the caller believed: the SAME evidence-bearing validator a
        # reader calls re-derives the admission from the committed blobs and the run from the hashed evidence files.
        # CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1 r5 (sol r4 B4): the validator RE-READS the hashed evidence. A throw there (a file held with FileShare.None
        # after collection, one that vanished or turned unreadable) is an evidence-validation failure, never a receipt-destination write failure: it is
        # re-thrown tagged DVE_EVIDENCE_VALIDATION_IO|<path>|<reason> (the path the .NET message quotes), and the runner writes the leg as a typed UNRESOLVED.
        # (r8: -LegSpecBytes, the spec the runner ran, is the paired-still contract in offline test mode only; production reads the committed spec)
        try { $validity = Test-DvReceiptValid -Receipt $Receipt -RepoRoot $RepoRoot -AllowOfflineTestMode:$OfflineTestMode -OfflineLegSpecBytes $(if ($OfflineTestMode) { $LegSpecBytes } else { $null }) }
        catch {
            $reason = ([string]$_.Exception.Message) -replace '\s+', ' '
            $where = $(if ($reason -match "'((?:[A-Za-z]:\\|\\\\)[^']+)'") { $Matches[1] } else { '(validator)' })
            throw [System.Management.Automation.RuntimeException]::new("DVE_EVIDENCE_VALIDATION_IO|$where|$reason")
        }
        # A production receipt that re-derives is ADVISORY (valid=false, VENUE_ANCHOR_ABSENT): it is written as the local diagnostic record, stamped as such,
        # and no reader treats it as a PASS. Anything that does not re-derive (INVALID / INCOMPLETE) is refused outright.
        if (-not ($validity.valid -or $validity.status -ceq 'ADVISORY')) { throw "DVE_RECEIPT_INVALID a $($Receipt['outcome']) receipt that does not re-derive from committed consent and hashed run evidence is refused ($($validity.status)): $($validity.reasons -join '; ')" }
        if ($validity.status -ceq 'ADVISORY') { $Receipt['outcomeDetail'] = 'ADVISORY (VENUE_ANCHOR_ABSENT; never a usable ' + [string]$Receipt['outcome'] + '): ' + [string]$Receipt['outcomeDetail'] }
        $Receipt['verification'] = [ordered]@{ status = $validity.status; venueAnchor = $(if ($validity.status -ceq 'ADVISORY') { 'ABSENT' } else { 'NOT_APPLICABLE' }); unbound = @($validity.unbound) }
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
    ConvertTo-DvLfBytes, Get-DvLegSpecSha256, Get-DvDerivedBackend, Get-DvBackendNotDerivable, Read-DvContactFrames, Get-DvLegSpecShapeRefusal, Get-DvPngImageProblem, Get-DvPairedSeekVerdict, Get-DvSubjectDigest,
    Get-DvVisualViewportPixels, Get-DvVisualMeanAbsDiff, Get-DvExposureMedian, Get-DvVisualExposureStats, Get-DvExposureBand, Get-DvVisualVerdict, Get-DvVisualSourceDifference, Get-DvFrameNumber, New-DvVisualStrip, Read-DvVisualCaptures, Get-DvVisualCaptureRequest, Read-DvVenueTable, ConvertFrom-DvVenueTableText, Get-DvVenueRole, Get-DvProp, Get-DvCommittedFile, Resolve-DvAdmissionSources,
    Test-DvUnderClaudeState, Test-DvLocalReceiptRoot, Read-DvClipConsent, Get-DvClipAdmission, Get-DvSmokeSummaryFields, Get-DvConformPaceProof, Get-DvConformArmVerdict, Get-DvMinPlaySeconds, Get-DvPlaybackProblems,
    Get-DvPlaybackEvidence, Get-DvScaleEvidence, Get-DvScaleLabel, Get-DvDisplayEvidence, Get-DvUnpacedEvidence, Get-DvLookLabelEvidence, Get-DvWorkerThreadsEvidence, Get-DvPrepConflatedMoveEvidence, Get-DvReconReleaseDeferredEvidence, Get-DvDueWakeDeliveryEvidence, Get-DvLookAssistEvidence, Get-DvLegSetPlan, Get-DvSmokeSessionLineFields, Get-DvSmokeSessionLineRead, Get-DvLogRecordBody, Get-DvMatrixRates, Get-DvMatrixRow, Get-DvMedian, ConvertTo-DvMatrixTable, Get-DvBlobById, Find-DvCommittedLegSpec, Read-DvCommittedLookReceipt, Read-DvEvidenceSet, Test-DvJsonEquivalent, Test-DvReceiptValid, Get-DvJobNotAfter, Get-DvUmRunNotAfterArgs, Get-DvUmRunDropVisibleUtc, Get-DvUmRunDropNotAfter, Test-DvNotAfterCoversDrop, Get-DvHealthVerdict,
    New-DvHealthProbeJobText, ConvertFrom-DvProbeStdout,
    Get-DvResultToken, Resolve-DvJobOutcome, Test-DvCriteria, Get-DvPresentMonGapVerdict, Get-DvVerbatimMetrics, Get-DvCriteriaMetrics, New-DvReceipt, Write-DvReceipt
