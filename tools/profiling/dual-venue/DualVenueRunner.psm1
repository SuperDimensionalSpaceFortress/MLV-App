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
    $sessionUnreadable = $false
    $isolated = $false
    $conformLine = $null
    if (-not [string]::IsNullOrEmpty($LogText)) {
        # PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r4 (r3 adversary A2 and advisory A3 on #726): every line is read as its record body, so a build path in a Qt
        # location suffix never selects the session or a line, and never adds a summary or conform field.
        $lines = Get-DvLogRecordBodies -LogText $LogText
        # HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r3 (r2 adversary pass 7 A2): each marker counts only where it STARTS the record body
        # ($script:DvRecordStart), so a run_metadata command line (or any other record) holding `playback_smoke.measured_session id=<n>` never picks the session
        $rs = $script:DvRecordStart
        # r10 (sol r9 B1): the id is the WHOLE token up to whitespace or the end of the line, and must be one plain decimal [0-9]+; before r10 `id=3x`,
        # `3.5` or `3/old` selected session 3 by its numeric prefix (and another Play's wake was honoured). A malformed first marker (or, with no marker,
        # a malformed first summary) makes the measured session UNREADABLE: session stays $null and sessionUnreadable is $true, never a later record's
        # r11 (r10 adversary A1 on 205a5b6c): the FIRST record that starts with the marker's name (`playback_smoke.measured_session` ending at a non-name
        # character, any case) is the marker, read whole (case-sensitive) or not at all: no `id=` right after it (`... id`, a doubled space, `ID=`) is the
        # empty token, so unreadable, never skipped for a later Play's marker. The summary fallback reads the first `playback_smoke.summary` the same way.
        $token = $null
        # r11 pass 11 (A3, `id=3 id=4`): the marker names the word id exactly once and the summary the word session exactly once (bounded by any
        # non-name character, so a quoted or repeated one counts); a second mention is the empty token, so unreadable
        # r12 (r12 adversary B3 on 7e66a3af, its input: a Kelvin sign U+212A before `id=4`): a name character is ASCII [A-Za-z0-9_] matched CASE-SENSITIVELY
        # ($script:DvNameChar); under (?i) .NET folds U+212A / U+0130 into [A-Za-z], which hid the mention. r12 (B2, its input: a first marker
        # `id=3 (id=4)`): a body that ends in the LOCATION_UNREADABLE sentinel lost its tail, so it is not read whole (the empty token, unreadable)
        # r12 (pass 13 A2): case-insensitive reads are culture-invariant ($script:DvCi), never -match or (?i)
        $wordCount = { param([string]$x, [string]$w) [regex]::Matches($x, '(?<!' + $script:DvNameChar + ')' + $w + '(?!' + $script:DvNameChar + ')', $script:DvCi).Count }
        $located = { param([string]$x) -not $x.EndsWith($script:DvLocationUnreadable, [StringComparison]::Ordinal) }
        foreach ($line in $lines) { if ([regex]::IsMatch($line, ($rs + 'playback_smoke\.measured_session(?!' + $script:DvNameChar + ')'), $script:DvCi)) { $token = $(if ((& $located $line) -and (& $wordCount $line 'id') -eq 1 -and (& $wordCount $line 'session') -eq 0 -and $line -cmatch ($rs + 'playback_smoke\.measured_session id=(?<s>\S*)')) { $Matches['s'] } else { '' }); break } }
        if ($null -eq $token) { foreach ($line in $lines) { if ([regex]::IsMatch($line, ($rs + 'playback_smoke\.summary(?!' + $script:DvNameChar + ')'), $script:DvCi)) { $token = $(if ((& $located $line) -and (& $wordCount $line 'session') -eq 1 -and $line -cmatch ($rs + 'playback_smoke\.summary session=(?<s>\S*)')) { $Matches['s'] } else { '' }); break } } }
        if ($null -ne $token) { if ($token -cmatch '^[0-9]+$') { $session = $token } else { $sessionUnreadable = $true } }
        if ($null -ne $session) {
            $summaryLine = $null
            foreach ($line in $lines) { if ([regex]::IsMatch($line, ($rs + 'playback_smoke\.summary session=' + [regex]::Escape($session) + '(\s|$)'), $script:DvCi)) { $summaryLine = $line } }
            # HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r24 (opus key3 r23 blocker; r24 adversary A1 on c1b10b4f): a summary record that holds a
            # MASKED `=` (a `<key>=` inside a quoted span, ConvertTo-DvQuoteMaskedBody) is refused whole: no field is read from it, so the quoted text never
            # supplies a field (the key3 repro's `source_advanced=600`) and a stray quote never hides a real field while the rest still reads (fields empty,
            # so every proof field is absent and the evidence is invalid). The app's own summary values hold no `=` inside a quote (real-log-mask-check)
            # r24 (r24 adversary pass 2 A1 on d89dc086, its input: an =-free quoted ` (` first, then the proof fields, then `diagnostic='prefix (
            # fps_override=1`): a summary record that lost its tail to the LOCATION_UNREADABLE sentinel ($located false) is refused whole too, as the
            # session readers already refuse it, so a field in the cut tail is never hidden while the fields before it still read
            if ($null -ne $summaryLine -and $summaryLine.IndexOf($script:DvQuotedEqualsMask) -lt 0 -and (& $located $summaryLine)) {
                foreach ($m in [regex]::Matches($summaryLine, '(?<k>[A-Za-z0-9_]+)=(?<v>\S+)')) { $fields[$m.Groups['k'].Value] = $m.Groups['v'].Value }
            }
            foreach ($line in $lines) { if ([regex]::IsMatch($line, ($rs + 'playback_smoke\.conform session=' + [regex]::Escape($session) + '(\s|$)'), $script:DvCi)) { $conformLine = $line } }
        }
        # Master isolates an automation run's settings store (the venue's persisted settings are never read or rewritten).
        # A build that predates that, or a line that says otherwise, leaves the venue's QSettings exposed: not evidence.
        # r13 (r12 adversary A1 sibling): culture-invariant ($script:DvCi), so a tr-TR `INTERACTION_TRACE` record is read as every other culture reads it
        $iso = @($lines | Where-Object { [regex]::IsMatch($_, ($rs + 'interaction_trace event=automation\.pacing_isolated(\s|$)'), $script:DvCi) })
        # r13 (r13 adversary A2 on 04c1337b): each record names settings_store as one whole token, and that one token is run_scoped. A longer key that
        # ends in it (`not_settings_store=run_scoped`) is no settings_store, and two settings_store tokens (`settings_store=venue settings_store=run_scoped`)
        # do not say which store ran, so either refuses isolation
        # r15 (r15 adversary A2 on d991b0af, its input: `settings_store=run_scoped "settings_store=venue"`): a mention is the WORD settings_store bounded
        # by any non-name character ($script:DvNameChar), so a quoted or punctuated declaration counts; exactly one mention, and it is that whole token
        # r16 (r15 adversary A1 on c24d5ee5, its input: `settings_store=run_scoped (settings_store=venue)`): a record that ends in the LOCATION_UNREADABLE
        # sentinel lost its tail, and the tail may have named the store, so such a record is never read whole: not isolated (as the session readers do)
        $runScoped = {
            param([string]$x)
            if ($x.EndsWith($script:DvLocationUnreadable, [StringComparison]::Ordinal)) { return $false }
            $mentions =[regex]::Matches($x, '(?<!' + $script:DvNameChar + ')settings_store(?!' + $script:DvNameChar + ')', $script:DvCi).Count
            $stores = [regex]::Matches($x, '(?<!\S)settings_store=(?<v>\S*)', $script:DvCi)
            ($mentions -eq 1) -and ($stores.Count -eq 1) -and [string]::Equals($stores[0].Groups['v'].Value, 'run_scoped', [StringComparison]::OrdinalIgnoreCase)
        }
        $isolated = ($iso.Count -gt 0) -and (@($iso | Where-Object { -not (& $runScoped $_) }).Count -eq 0)
    }
    [pscustomobject]@{ session = $session; sessionUnreadable = $sessionUnreadable; fields = $fields; settingsIsolated = $isolated; conformLine = $conformLine }
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
    # r24 (r24 adversary pass 2 A1 sibling): a conform record that holds a masked `=` or lost its tail to the LOCATION_UNREADABLE sentinel supplies no
    # conform field (refused whole, like the summary record), so the conform proof reads it as absent and is not proven
    $conformReadable = ($null -ne $parsed.conformLine) -and ([string]$parsed.conformLine).IndexOf($script:DvQuotedEqualsMask) -lt 0 -and -not ([string]$parsed.conformLine).EndsWith($script:DvLocationUnreadable, [StringComparison]::Ordinal)
    if ($conformReadable) { foreach ($m in [regex]::Matches([string]$parsed.conformLine, '(?<k>[A-Za-z0-9_]+)=(?<v>\S+)')) { $conformFields[$m.Groups['k'].Value] = $m.Groups['v'].Value } }
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
                # r3 (r2 adversary pass 6 A3): the clamp record only where it starts the record body
                # r13 (r12 adversary A1 sibling): culture-invariant ($script:DvCi), so a tr-TR upper-case clamp record is read as every other culture reads it
                $clampMatch = [regex]::Match($line, ($script:DvRecordStart + 'playback_scale_clamped_for_gpu_texture_route requested=(?<r>\d+) effective=(?<e>\d+)'), $script:DvCi)
                if ($clampMatch.Success) { $effective = [int64]$clampMatch.Groups['e'].Value; $source = 'playback_scale_clamped_for_gpu_texture_route effective (clamp line)'; $clampActive = $true; break }
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
        # HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r24 (opus key3 r23 class sweep): the record bodies, quote-masked (Get-DvLogRecordBodies), so a
        # label record spelled inside another record's quoted span (`note='gui_smoke.look_label shown="a" ... wait_ms=0'`) has no `shown=` and answers
        # nothing; the app's own label text holds no `=`, so its shown / expected values read as before. Before r24 this reader split the raw text.
        foreach ($line in (Get-DvLogRecordBodies -LogText $LogText)) {
            # r13 (r12 adversary A1 sibling): culture-invariant ($script:DvCi), so a tr-TR upper-case look_label record is read as every other culture reads it
            $labelMatch = [regex]::Match($line, 'gui_smoke\.look_label shown="(?<shown>[^"]*)" expected="(?<expected>[^"]*)" surface=(?<surface>\S+) show_in_fullscreen=(?<sif>[01]) wait_ms=(?<wait>\d+)', $script:DvCi)
            if ($labelMatch.Success) {
                $m = @{}
                foreach ($g in 'shown', 'expected', 'surface', 'sif', 'wait') { $m[$g] = $labelMatch.Groups[$g].Value }
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
    # CPU-S1-FS-SPIKE-TEAM-PARTITION-1: generatorArgs.reconOmpThreads / prepOmpThreads = n -> the job sets the variable to n; absent = unset (or 0, which the
    # app reads as unset: WorkerThreadCount.h mlvappPlaybackTeamRequestFromValue). A build before this card writes neither field, so neither is read.
    $ga = Get-DvProp $Spec 'generatorArgs'
    foreach ($team in @(@('reconOmpThreads', 'env_playback_recon_omp_threads', 'MLVAPP_PLAYBACK_RECON_OMP_THREADS'), @('prepOmpThreads', 'env_playback_prep_omp_threads', 'MLVAPP_PLAYBACK_PREP_OMP_THREADS'))) {
        $want = ConvertTo-DvInt64 (Get-DvProp $ga $team[0])
        $rows.Add(@($team[1], $team[2], $(if ($null -ne $want -and $want -ge 1) { @([string]$want) } else { $unsetOrZero })))
    }
    # generatorArgs.rawPrefetchDecoders: 0 = the job sets MLVAPP_DISABLE_RAW_UINT16_PREFETCH=1, k = MLVAPP_RAW_UINT16_PREFETCH_DECODERS=k, absent = neither (playback-attr-3-cuda-job.ps1)
    $rows.Add(@('env_disable_raw_uint16_prefetch', 'MLVAPP_DISABLE_RAW_UINT16_PREFETCH', $(if ($null -ne $decoders -and $decoders -eq 0) { @('1') } else { $unsetOrZero })))
    $rows.Add(@('env_raw_uint16_prefetch_decoders', 'MLVAPP_RAW_UINT16_PREFETCH_DECODERS', $(if ($decoders -ge 1) { @([string]$decoders) } else { @('unset') })))
    $overrides = [System.Collections.Generic.List[string]]::new()
    $unreadable = [System.Collections.Generic.List[string]]::new()
    foreach ($row in $rows) {
        # r24 (r24 adversary pass 2 A1 sibling): a start record refused whole (Get-DvSmokeSessionLineRead unreadable) makes every row unreadable
        if ($StartRead.unreadable -eq $true -or $StartRead.duplicates -contains $row[0]) { $unreadable.Add($row[1]); continue }
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

# HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r3 (r2 adversary pass 7 A2, pass 6 advisory A3): where a record marker must sit to count -- at the START
# of the record body, after at most the log prefix's bracket groups ([utc] [LEVEL] [0x<thread>] , or a shorter fixture prefix), never as text inside
# another record (a run_metadata command line, a path, a field value). Every reader that selects the measured session or reads a session record anchors on it.
$script:DvRecordStart = '^(?:\[[^\]]*\] ){0,3}'

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

function Get-DvDueWakeDelivered {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r8 (B1, sol + key3 opus r7): does ONE measured playback_due_wake record show a DELIVERED wake?
    fired= counts the wakes the GUI thread took (MainWindow::onPlaybackDueWake); armed= counts arms, and an arm replaced or cancelled before delivery
    (stranded=) delivers nothing, so arming is not delivery. r8 adversary pass 3 A1: fired= also counts a LOST post (the queue refused the native
    message past its retry budget) that the poll tick delivered (PlaybackDueWake::deliverLostPost, counted in post_rescued=), which is no native
    delivery; so the wakes the arm's own route delivered are fired= minus post_rescued=. Returns 'delivered' (exactly one whole-number fired= and
    exactly one whole-number post_rescued=, and fired - post_rescued >= 1), 'none' (both readable, difference < 1) or 'unreadable' (either field
    missing, repeated, empty, signed or not a whole count). FAIL CLOSED: only 'delivered' is delivery evidence.
    r9 (Grok CoS G10): post_rescued= is a settled count only when the same line says post_counts_settled=1. The app prints 0 when the wait thread
    did not answer in time at the line or at the Play's baseline (MainWindow::playbackDueWakeSummaryLine, postCountsSettled), which leaves the post
    differences open to another Play's late count, so a rescue that would cancel the one wake may not be counted yet. Anything but exactly one
    post_counts_settled=1 (0, missing, repeated, empty, any other value) is 'unreadable', never 'delivered'.
    advanced= is not read (decided r8): a delivered wake on a busy renderer hands its advance to the frameReady path (busy_deferred=), so advanced=0
    with a delivered wake is a measured outcome of the arm, not a route that did not run.
    The one predicate for "a wake was delivered": Get-DvDueWakeDeliveryEvidence calls it for both the requested and the head path, and the runner's
    gate (Invoke-VenueLeg) and Test-DvReceiptValid reach it only through that reader.
    #>
    param([AllowNull()][AllowEmptyString()][string]$Line)
    $n = @{}
    foreach ($name in @('fired', 'post_rescued')) {
        $f = [regex]::Matches([string]$Line, '(?:^|\s)' + $name + '=(?<f>\S*)')
        if ($f.Count -ne 1 -or $f[0].Groups['f'].Value -cnotmatch '^[0-9]+$') { return 'unreadable' }
        $n[$name] = [System.Numerics.BigInteger]::Parse($f[0].Groups['f'].Value, [Globalization.CultureInfo]::InvariantCulture)
    }
    # r9 (G10): the post counts must be settled, as exactly one post_counts_settled=1
    $settled = [regex]::Matches([string]$Line, '(?:^|\s)post_counts_settled=(?<s>\S*)')
    # r24 (r24 adversary pass 2 A2 on d89dc086, its input: post_counts_settled=<U+0661>): ORDINAL, never -cne (a linguistic compare equates U+0661 /
    # U+06F1 with ASCII 1 and ignores U+00AD / U+200B / U+FEFF), so only the ASCII token 1 is settled
    if ($settled.Count -ne 1 -or -not [string]::Equals($settled[0].Groups['s'].Value, '1', [StringComparison]::Ordinal)) { return 'unreadable' }
    if (($n['fired'] - $n['post_rescued']) -lt [System.Numerics.BigInteger]::One) { return 'none' }
    'delivered'
}

function Get-DvWindowToggleRequestEvidence {
    <#
    .SYNOPSIS
    VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1 r1 (adversary A5 on ead7bbdb): a leg that does NOT name generatorArgs.midPlayWindowToggles must have driven no window
    toggle. The app honours MLVAPP_GUI_SMOKE_MID_PLAY_WINDOW_TOGGLES from any process environment, so a variable inherited from the launcher would move the window
    of an ordinary windowed leg (and its job, plain, publishes no windowToggles* scalar to show it). `blocks` is true when such a leg's run log carries a
    gui_smoke.window_toggle or gui_smoke.window_toggles record, read from the app's record body only (Get-DvLogRecordBodies cuts the log prefix and the Qt
    location suffix). A leg that asks is judged by its own windowToggles* criteria, not here. Pure and shared: the runner gates on it and Test-DvReceiptValid
    re-derives it from the hashed run log.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText)
    $raw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'midPlayWindowToggles'
    $requested = ($raw -is [bool]) -and $raw
    $mentions = 0
    foreach ($line in (Get-DvLogRecordBodies -LogText $LogText)) {
        # (the body still starts with the optional `[utc] [LEVEL] [0x<thread>] ` prefix; a record counts only where it starts the body after it)
        # r4 (adversary A2 on 0b915c19): the letter i is the class [Ii], so a host culture that does not fold an upper-case I (tr-TR) still counts GUI_SMOKE.WINDOW_TOGGLE
        # r7 (r7 adversary A1 on be8fb7b6, its inputs: `gui_smoke.window_toggles<TAB>requested=4 ...` and the bare name): the name ends at ANY non-name character or the
        # line end, never only at a literal space, so a toggle-named record in any spelling is a mention (fail closed: an ordinary leg blocks)
        # r8 (keys r7 B1, input: `gui_smoke.window_toggles<U+212A> requested=4 driven=4 reason=ok` on an ordinary leg): the terminator is (?!$script:DvNameChar), ASCII
        # matched case-sensitively, so a U+212A / U+0130 glued to the name ends it and the record is a mention (under -match a bare [A-Za-z0-9_] folded them in)
        if ($line -match ('^\s*(?:\[[^\]]*\]\s*)*gu[Ii]_smoke\.w[Ii]ndow_toggles?(?!' + $script:DvNameChar + ')')) { $mentions++ }
    }
    $blocks = (-not $requested) -and ($mentions -gt 0)
    [pscustomobject]@{
        requested = $requested
        mentions = $mentions
        blocks = $blocks
        reason = $(if ($blocks) { "WINDOW_TOGGLES_INHERITED: the run log carries $mentions gui_smoke.window_toggle record(s) but the leg does not name midPlayWindowToggles (MLVAPP_GUI_SMOKE_MID_PLAY_WINDOW_TOGGLES came from the launcher's environment)" } else { $null })
    }
}

function Get-DvWindowToggleCriteriaGap {
    <#
    .SYNOPSIS
    VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1 r11 (r11 adversary A1 on b82002ec, its input: an fstoggle spec with windowToggleEvidenceReason removed from both role
    criteria arrays, over an honest reason=summary-reason-incomplete): a leg that names generatorArgs.midPlayWindowToggles relies on its CRITERIA to fail an honest
    non-ok toggle proof (the reconcile gate passes it as "what the log derives"), so criteria.acceptance and criteria.supplementary must both be present, each
    must hold an array for every backend the spec names, and every array must bind all five: windowTogglesRequested / Driven / KeptPlaying / Advanced eq 4 (the job's schedule) and
    windowToggleEvidenceReason eq "ok". Returns the gap text, or $null. A leg that does not ask returns $null.
    #>
    param([Parameter(Mandatory)]$Spec)
    $raw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'midPlayWindowToggles'
    if (-not (($raw -is [bool]) -and $raw)) { return $null }
    $required = @(@('windowTogglesRequested', '4'), @('windowTogglesDriven', '4'), @('windowTogglesKeptPlaying', '4'), @('windowTogglesAdvanced', '4'), @('windowToggleEvidenceReason', 'ok'))
    $criteria = Get-DvProp $Spec 'criteria'
    # r11b (r11 adversary A1 on 7d6aea0a, inputs: criteria.supplementary = {}, and both sets keyed cpu while backends = ["cuda"]): BOTH sets must be present, and
    # each must hold a role array for EVERY backend the spec names (the array Test-DvCriteria judges; a missing one passes vacuously), each binding all five
    # r12 (sol r11 B1 on d3b176e3, input: backends ["cpu","cuda"] with complete cpu and cuda arrays in both sets): Get-DvProp hands the backends array over as ONE
    # pipeline object, so piping it straight into a [string] cast joined it into 'cpu cuda' and refused a valid spec. The value is held first and @() enumerates its
    # elements; each is one backend NAME (a JSON string, the schema's enum), and an entry that is not a string is refused by name, never cast
    # r12 adversary A1 on 3f7bf31b (input: backends [null, "cuda"] with complete cuda arrays): a null or empty entry is REFUSED, never skipped; the leg's default
    # backend is the FIRST entry (Invoke-VenueLeg.ps1), so a skipped null admitted a spec at preflight that failed late
    $backendsValue = Get-DvProp $Spec 'backends'
    if ($null -eq $backendsValue) { return 'the spec names no backend, so no criteria array is judged' }
    $backends = [System.Collections.Generic.List[string]]::new()
    foreach ($entry in @($backendsValue)) {
        if ($entry -isnot [string] -or $entry.Length -eq 0) {
            return "the spec's backends holds an entry that is not a backend name ($(ConvertTo-Json -InputObject $entry -Compress -Depth 3)), so no criteria array is named by it"
        }
        $backends.Add($entry)
    }
    if ($backends.Count -lt 1) { return 'the spec names no backend, so no criteria array is judged' }
    foreach ($setName in 'acceptance', 'supplementary') {
        $set = Get-DvProp $criteria $setName
        if ($null -eq $set) { return "criteria.$setName is absent, so nothing fails a non-ok toggle proof" }
        if ($set -isnot [System.Management.Automation.PSCustomObject]) { return "criteria.$setName is not one JSON object" }
        foreach ($backend in $backends) {
            if ($null -eq $set.PSObject.Properties[$backend]) { return "criteria.$setName has no $backend array (a backend the spec names), so nothing fails a non-ok toggle proof" }
        }
        # r14 (r14 adversary A1 on 1376a142, input: windowTogglesDriven's value 4 -> [4] in both cuda arrays over a healthy log): a [string] cast read [4] (and ["4"],
        # ["ok"]) as the bound value, while Test-DvCriteria compares the RAW value (4 -eq @(4) is false), so an unbound spec passed preflight and FAILED a healthy
        # capture late. A row binds only when its value is the JSON scalar the evaluator matches: a number for the four counts (as the framesExported binding in
        # Get-DvExportOutcome reads one) and a string for the reason; any other value (array, object, boolean, null, a numeric string) is no binding
        foreach ($role in $set.PSObject.Properties) {
            $rows = @($role.Value)
            foreach ($need in $required) {
                $bound = @($rows | Where-Object { $null -ne $_ -and [string](Get-DvProp $_ 'metric') -ceq $need[0] -and [string](Get-DvProp $_ 'op') -ceq 'eq' -and $(if ($need[1] -ceq 'ok') { (Get-DvProp $_ 'value') -is [string] } else { (Get-DvProp $_ 'value') -is [int] -or (Get-DvProp $_ 'value') -is [long] -or (Get-DvProp $_ 'value') -is [double] }) -and [string](Get-DvProp $_ 'value') -ceq $need[1] })
                if ($bound.Count -lt 1) { return "criteria.$setName.$($role.Name) does not bind $($need[0]) eq $($need[1])" }
            }
        }
    }
    $null
}

function Get-DvWindowToggleReconcileEvidence {
    <#
    .SYNOPSIS
    VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1 r3 (sol B4 on 11def8e4): a leg that NAMES generatorArgs.midPlayWindowToggles must have its windowToggles* scalars
    (windowTogglesRequested / Driven / KeptPlaying / Advanced and windowToggleEvidenceReason) backed by the hashed run log. They are re-derived here from the
    log with the job's own reader (Get-AttrCudaWindowToggleEvidence, over the measured session Get-DvSmokeSummaryFields names; r11: an UNREADABLE one blocks with
    WINDOW_TOGGLES_SESSION_UNREADABLE and is never handed to the reader as no id) and
    compared field for field with the metrics; `blocks` is true when any of the five is absent or differs, so a summary whose toggle records were removed from
    the log (and the log-hash references refreshed) is INVALID, never a PASS. An honest failure (the scalars are exactly what the log derives, reason not ok)
    does not block: the criteria FAIL it. A leg that does not ask is not judged here (Get-DvWindowToggleRequestEvidence is its gate). Pure and shared: the
    runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText, $Metrics)
    $raw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'midPlayWindowToggles'
    $requested = ($raw -is [bool]) -and $raw
    if (-not $requested) { return [pscustomobject]@{ requested = $false; blocks = $false; reason = $null; derived = $null } }
    # r11 (r11 adversary A1 on b82002ec): an honest non-ok proof passes this gate and is failed by the criteria, so a requested leg whose criteria do not bind the
    # five scalars is INVALID here (the leg writer and Test-DvReceiptValid both call this), never a PASS of a non-ok proof
    $criteriaGap = Get-DvWindowToggleCriteriaGap -Spec $Spec
    if ($null -ne $criteriaGap) {
        return [pscustomobject]@{ requested = $true; blocks = $true; reason = "WINDOW_TOGGLES_CRITERIA_UNBOUND: the leg names midPlayWindowToggles but $criteriaGap"; derived = $null }
    }
    $sessionRead = Get-DvSmokeSummaryFields -LogText $LogText
    # r11 (sol r10 A2, opus key3 r10 H1 on 7fc447b9, input: the healthy log with id=3x / session=3x): an UNREADABLE measured session (a malformed first marker, or
    # with no marker a malformed first summary) names no session, so it REFUSES requested-toggle proof with a typed reason; before r11 it became the empty id and the
    # reader's automatic selection picked the only marker (3x) and derived ok. The derived evidence is the refusal itself, never a reader result
    if ($sessionRead.sessionUnreadable -eq $true) {
        return [pscustomobject]@{
            requested = $true
            blocks = $true
            reason = 'WINDOW_TOGGLES_SESSION_UNREADABLE: the leg names midPlayWindowToggles but the run log''s measured playback_smoke session is unreadable (Get-DvSmokeSummaryFields sessionUnreadable), so no toggle record is the measured Play''s; requested-toggle proof is refused, never derived by automatic session selection'
            derived = [pscustomobject]@{ requested = 0; driven = 0; keptPlaying = 0; advanced = 0; reason = 'measured-session-unreadable' }
        }
    }
    $session = $(if ($null -ne $sessionRead.session) { [string]$sessionRead.session } else { '' })
    $module = Import-Module (Join-Path $PSScriptRoot '..\bachelor\AttrCudaArtifacts.psm1') -PassThru -DisableNameChecking
    $derived = & $module { param([string]$t, [string]$s) Get-AttrCudaWindowToggleEvidence -LogText $t -MeasuredSessionId $s } ([string]$LogText) $session
    $pairs = @(@('windowTogglesRequested', $derived.requested), @('windowTogglesDriven', $derived.driven), @('windowTogglesKeptPlaying', $derived.keptPlaying),
        @('windowTogglesAdvanced', $derived.advanced), @('windowToggleEvidenceReason', $derived.reason))
    $mismatches = [System.Collections.Generic.List[string]]::new()
    foreach ($pair in $pairs) {
        $claimed = Get-DvProp $Metrics $pair[0]
        if ($null -eq $claimed -or -not [string]::Equals([string]$claimed, [string]$pair[1], [StringComparison]::Ordinal)) {
            $mismatches.Add("$($pair[0])=$(if ($null -eq $claimed) { '<absent>' } else { [string]$claimed }) but the run log derives $([string]$pair[1])")
        }
    }
    $blocks = $mismatches.Count -gt 0
    [pscustomobject]@{
        requested = $true
        blocks = $blocks
        reason = $(if ($blocks) { "WINDOW_TOGGLES_NOT_FROM_LOG: the leg names midPlayWindowToggles but its windowToggles* scalars are not what the hashed run log derives ($($mismatches -join '; ')); a toggle claim the raw records do not back is no evidence" } else { $null })
        derived = $derived
    }
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
    or is unreadable; send, none (no wake: an unpaced leg) and qtimer, and no delivery_kind at all (a control binary before r1) leave it unchanged (any
    other kind, "mixed" included, is unreadable). r2 adversary pass 6: a record counts only where it starts its record body, and the measured window closes
    at the next pace_summary or at any playback_smoke record of another session. r4 (r4 adversary pass 9 A1): a leg that does not ask also blocks when the
    measured line says disabled=1 (DUE_WAKE_DISABLED_INHERITED: MLVAPP_PLAYBACK_DUE_WAKE=0 came from the launcher; a control binary's line included), and
    when a PACED leg's measured Play armed no wake (delivery=none: DUE_WAKE_NOT_ENGAGED); "none" is the app's own only on a generatorArgs.unpaced = true leg.
    r5 (Grok G8, r4 adversary pass 10 A2/A3): a disabled= that is not exactly one 0 or 1 across the measured window's wake records blocks such a leg on a
    control binary's line too (DUE_WAKE_DISABLED_UNREADABLE), and a playback_smoke.measured_session id= of another session closes the window.
    r5 (pass 12 A1): on a control binary's line (no delivery_kind) a paced leg's delivery=none is DUE_WAKE_NOT_ENGAGED too, and a repeated or
    out-of-grammar delivery= is unreadable; a posted leg whose line says disabled=1 is ROUTE_NOT_ENGAGED (pass 11 A2).
    r8 (B1, sol + key3 opus r7): on a line with a delivery_kind, the posted leg and every PACED leg that does not ask count only when the measured
    Play DELIVERED a wake by its route (Get-DvDueWakeDelivered: fired= minus post_rescued= of one or more); none, or a missing or unreadable count, is
    DUE_WAKE_NOT_ENGAGED. r9 (Grok G10): post_rescued= counts only beside exactly one post_counts_settled=1 on the same line; an unsettled, missing,
    repeated or malformed flag is unreadable there, so DUE_WAKE_NOT_ENGAGED on both paths. r10 (sol r9 B1): a malformed measured-session id (id=3x, 3.5,
    3/old; Get-DvSmokeSummaryFields sessionUnreadable) blocks every leg, exempt ones included (DUE_WAKE_SESSION_UNREADABLE). r11 (r10 adversary A1/A2): so
    does a first marker not read whole (no id=, a doubled space), and a session boundary inside the measured window not read whole ends the measured Play
    and blocks every leg too (DUE_WAKE_SESSION_BOUNDARY_UNREADABLE). EXEMPT: a control binary's line (no delivery_kind; its binary is not under test) and an unpaced leg (it never arms).
    Pure and shared: the runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log.
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
    $sessionUnreadable = $false
    # r11 (r10 adversary A2): a session boundary inside the measured window that is not read whole (see the loop below)
    $boundaryUnreadable = $false
    if (-not [string]::IsNullOrEmpty($LogText)) {
        $picked = Get-DvSmokeSummaryFields -LogText $LogText
        $session = $picked.session
        # r10 (sol r9 B1): a malformed measured-session id names no session, so no line is the measured Play's; it blocks every leg below
        $sessionUnreadable = ($picked.sessionUnreadable -eq $true)
        if ($null -ne $session) {
            # r2 adversary pass 6 A1: a record is read only where it STARTS the record body (after the optional `[utc] [LEVEL] [0x<thread>] ` prefix,
            # Get-DvLogRecordBody), never as text inside another record's line. r3: the one shared anchor ($script:DvRecordStart), which the session
            # selector above (Get-DvSmokeSummaryFields) now uses too (r2 adversary pass 7 A2)
            $start = $script:DvRecordStart
            # r15 (r14 adversary A2): session ids are compared as canonical decimals (ConvertTo-DvSessionKey), never as strings, so a boundary that
            # spells the measured session `03` is that session (it neither closes its window nor fails to open it). r15 (r15 adversary A1 on d991b0af):
            # the module's one ConvertTo-DvSessionKey (-Sign/-Digits), never a second definition; every id here is one ASCII [0-9]+
            $sessionKey = ConvertTo-DvSessionKey -Sign '' -Digits ([string]$session)
            $paceIdRx = $start + 'playback_smoke\.pace_summary session=(?<s>[0-9]+)(?:\s|$)'
            $isMeasuredPace = { param([string]$x) ($x -cmatch $paceIdRx) -and ((ConvertTo-DvSessionKey -Sign '' -Digits $Matches['s']) -ceq $sessionKey) }
            $open = $false
            # r11 (r10 adversary A2 on 205a5b6c): a session boundary inside the open window that is not read whole ENDS the measured Play and marks
            # the read unreadable (DUE_WAKE_SESSION_BOUNDARY_UNREADABLE below, every leg), so the wake after it is never credited to the measured Play.
            # A boundary is: a measured_session marker (read whole: ` id=<[0-9]+>`), a start / summary / conform / pace_summary record, or any
            # playback_smoke record whose first field is session (read whole: ` session=<[0-9]+>`); the empty `id=` / `session=`, no `=`, a doubled
            # space, `4x`, or a start with no session first are each unreadable. A boundary read whole of another session still just closes it.
            # r12 (r12 adversary B3): every name boundary is the ASCII, case-sensitive $script:DvNameChar, inside (?i) and -match too
            $nameEnd = '(?!' + $script:DvNameChar + ')'
            $nameStart = '(?<!' + $script:DvNameChar + ')'
            # r12 (r12 adversary B2 on 7e66a3af, its input: `playback_smoke.gpu_summary (session=4)` in the window): a body that ends in the
            # LOCATION_UNREADABLE sentinel lost its tail, and with it any session or key mention there, so such a record is never read whole
            $located = { param([string]$x) -not $x.EndsWith($script:DvLocationUnreadable, [StringComparison]::Ordinal) }
            # r11 (r11 adversary pass 10 A1 on 93fa8621, its input: `playback_smoke.gpu_summary frames=600 session=4` was skipped): a record that names a
            # session ANYWHERE (a `session` token, with or without =) is a boundary, read whole only as ONE ` session=<[0-9]+>` that is its first field
            # r11 pass 11 (A1, its input: a quoted `"session=4"`): a mention is the WORD session bounded by any non-name character (a quote, a comma,
            # a bracket, whitespace), not only by whitespace; `measured_session=` / `x_session=` are other names
            # r12 (pass 13 A2): every case-insensitive read here is culture-invariant ($ci / $script:DvCi), never -match or (?i)
            $ci = { param([string]$x, [string]$p) [regex]::IsMatch($x, $p, $script:DvCi) }
            $sessionTokens = { param([string]$x) [regex]::Matches($x, $nameStart + 'session' + $nameEnd, $script:DvCi).Count }
            # r11 pass 11 (A3, its input: `playback_smoke.measured_session id=3 id=4`): a marker names its id exactly once (the word id, bounded the same way)
            $idTokens = { param([string]$x) [regex]::Matches($x, $nameStart + 'id' + $nameEnd, $script:DvCi).Count }
            foreach ($l in (Get-DvLogRecordBodies -LogText $LogText)) {
                if (& $ci $l ($start + 'playback_smoke\.pace_summary' + $nameEnd)) {
                    $paceWhole = (& $located $l) -and ($l -cmatch ($start + 'playback_smoke\.pace_summary session=[0-9]+(?:\s|$)')) -and ((& $sessionTokens $l) -eq 1)
                    # r12 (B2, its input: the sole pace_summary `session=3 (session=4)`): a pace_summary not read whole whose visible text opens the
                    # measured session's window is unreadable too, not only one inside an open window
                    # r12 (pass 13 A1 on bbfe69d1, its input: `pace_summary  session=3` then `pace_summary session=3`): so is one BEFORE the measured
                    # window was first seen ($paces 0), whatever its text; it may have been the measured Play's own pace, so the later one cannot be
                    # shown to be the only measured window. Only a pace after the window closed is another Play's.
                    if (-not $paceWhole -and ($open -or $paces -eq 0 -or (& $isMeasuredPace $l))) { $boundaryUnreadable = $true }
                    $open = $paceWhole -and (& $isMeasuredPace $l); if ($open) { $paces++ }; continue
                }
                # r2 adversary pass 6 A2: any playback_smoke record of ANOTHER session (its start, summary, ...) closes the window, so another Play's
                # wake line never stands in for the measured Play's missing one. r5 (r4 adversary pass 10 advisory A3): measured_session names its
                # session with id=, not session=, and closes the window the same way
                if ($open) {
                    $bid = $null; $isBoundary = $false
                    if (& $ci $l ($start + 'playback_smoke\.measured_session' + $nameEnd)) {
                        $isBoundary = $true; if ((& $located $l) -and (& $sessionTokens $l) -eq 0 -and (& $idTokens $l) -eq 1 -and $l -cmatch ($start + 'playback_smoke\.measured_session id=(?<s>[0-9]+)(?:\s|$)')) { $bid = $Matches['s'] }
                    }
                    # r12 (B2): any playback_smoke record whose body lost its tail to the sentinel may have named a session there, so it is a boundary
                    # not read whole
                    elseif ((& $ci $l ($start + 'playback_smoke\.(?:start|summary|conform)' + $nameEnd)) -or ((& $ci $l ($start + 'playback_smoke\.' + $script:DvNameChar + '+' + $nameEnd)) -and ((& $sessionTokens $l) -gt 0 -or -not (& $located $l)))) {
                        $isBoundary = $true; if ((& $located $l) -and (& $sessionTokens $l) -eq 1 -and $l -cmatch ($start + 'playback_smoke\.[A-Za-z0-9_]+ session=(?<s>[0-9]+)(?:\s|$)')) { $bid = $Matches['s'] }
                    }
                    if ($isBoundary -and $null -eq $bid) { $boundaryUnreadable = $true; $open = $false; continue }
                    if ($isBoundary -and (ConvertTo-DvSessionKey -Sign '' -Digits $bid) -cne $sessionKey) { $open = $false; continue }
                }
                # r1 adversary pass 2 A4: the window stays open to the next pace_summary, so a second line in it is seen (and is unreadable).
                # r2 adversary pass 6 A1: ANY playback_due_wake record (the app prints exactly one shape, starting armed=) is a candidate, so a second
                # one without armed= makes the read unreadable instead of being skipped
                # r11 pass 11 advisory A4 (`playback_due_wake: armed=0 ... disabled=1` was skipped): the record name ends at any non-name character
                if ($open -and (& $ci $l ($start + 'playback_due_wake' + $nameEnd))) { $candidates.Add($l) }
            }
        }
    }
    # delivery_kind is the SELECTED delivery (send / post / notifier; qtimer when the wake was built on a QTimer; none with no wake), never rewritten by a
    # fallback arm; delivery= is what this Play's arms used (native-msg, qtimer, notifier, "+"-joined when they differed, none with no arm)
    $mechanism = $null
    # r8 (B1): 'delivered' / 'none' / 'unreadable' for the one measured line the delivery_kind path accepts ($null when it accepts none)
    $delivered = $null
    # r4 (r4 adversary pass 9 A1): disabled= is the app's MLVAPP_PLAYBACK_DUE_WAKE kill switch (MainWindow::playbackDueWakeSummaryLine prints 0 or 1), read
    # from the measured line whether or not it carries a delivery_kind (a control binary's line has none); a repeated or non-0/1 value is unreadable.
    # r5 (Grok G8 = r4 adversary pass 10 advisory A2): the repeat is counted across every wake record in the measured window (a second control line
    # repeats it), and an unreadable value blocks a leg that does not ask on a line with no delivery_kind too (DUE_WAKE_DISABLED_UNREADABLE below)
    $disabledSeen = $false
    $disabledUnreadable = $false
    $dis = @(foreach ($c in $candidates) { [regex]::Matches($c, '(?:^|\s)disabled=(?<d>\S*)') })
    if ($dis.Count -gt 1 -or ($dis.Count -eq 1 -and $dis[0].Groups['d'].Value -cnotmatch '^[01]$')) { $disabledUnreadable = $true }
    elseif ($dis.Count -eq 1 -and $dis[0].Groups['d'].Value -ceq '1') { $disabledSeen = $true }
    $unpacedRaw = Get-DvProp (Get-DvProp $Spec 'generatorArgs') 'unpaced'
    $unpacedLeg = ($unpacedRaw -is [bool]) -and $unpacedRaw
    # r11 (r11 adversary pass 10 A2 on 93fa8621, its input: a paced head line ending in a bare `delivery_kind` took the control-line exemption): a line
    # NAMES delivery_kind with the key in any shape (no `=`, `delivery_kind post`, another case), and every key this reader reads (delivery_kind,
    # delivery, disabled, armed, fired, post_rescued, post_counts_settled) must appear only as `<key>=`; a key named any other way makes the read
    # unreadable on both paths (never skipped, never the control exemption)
    # r11 pass 11 (A2, its input: a quoted `"delivery_kind=post"`): a mention is the key's WORD bounded by any non-name character, so a quoted or
    # punctuated key is a mention that is not `<key>=` after whitespace, hence unreadable
    # r12 (r12 adversary B3, its input: `delivery=native-msg <U+212A>delivery_kind=post`): the key boundary is the ASCII, case-sensitive $script:DvNameChar
    # r12 (pass 13 A2): culture-invariant ($script:DvCi), so a tr-TR `DELIVERY_KIND=post` is a mention too
    $withKind = @($candidates | Where-Object { [regex]::IsMatch($_, ('(?<!' + $script:DvNameChar + ')delivery_kind(?!' + $script:DvNameChar + ')'), $script:DvCi) })
    $keyShapeUnreadable = $false
    foreach ($c in $candidates) {
        # r12 (r12 adversary B2, its input: a sent-head wake line ending ` (delivery_kind=1)`): a wake record whose body lost its tail to the
        # LOCATION_UNREADABLE sentinel may have named a key there, so its key shape is unreadable on both paths
        if ($c.EndsWith($script:DvLocationUnreadable, [StringComparison]::Ordinal)) { $keyShapeUnreadable = $true }
        # r23 (r23 adversary A1 on 9b78199f, its input: `playback_due_wake armed=1 note="diagnostic fired=1 ... delivery_kind=post end"` was HONOURED):
        # the app's wake record is numeric and enum fields only (MainWindow::playbackDueWakeSummaryLine writes no quote), so a wake record whose body
        # holds a double quote is not the app's shape and its key shape is unreadable on both paths; text inside a quote never supplies a field here
        # r24 (opus key3 r23 hardening = sol r23 INV-6, its input: `note='prefix fired=1 ... delivery_kind=post end'` was HONOURED): a single quote
        # too. The candidates are quote-masked bodies (Get-DvLogRecordBodies), so the quoted `=` already supplies no field; the refusal names the shape
        if ($c.IndexOfAny($script:DvQuoteChars) -ge 0) { $keyShapeUnreadable = $true }
        foreach ($key in @('delivery_kind', 'delivery', 'disabled', 'armed', 'fired', 'post_rescued', 'post_counts_settled')) {
            if ([regex]::Matches($c, '(?<!' + $script:DvNameChar + ')' + $key + '(?!' + $script:DvNameChar + ')', $script:DvCi).Count -ne [regex]::Matches($c, '(?:^|\s)' + $key + '=').Count) { $keyShapeUnreadable = $true }
        }
    }
    # r5 (r5 adversary pass 12 A1): a control binary's line (one wake record, no delivery_kind) still carries delivery= (master's
    # playbackDueWakeSummaryLine prints it), so a paced leg whose measured Play armed no wake (delivery=none) is not engaged there either; a
    # repeated or out-of-grammar delivery= on that line is unreadable. A line with no delivery= at all (an older binary) is left unchanged.
    # r6 (r5 adversary pass 13 A1): the app prints one wake record per Play, so a measured window with more than one and no delivery_kind is unreadable
    # too; before r6 a second record (a bare `playback_due_wake`) skipped the one-line read and removed the paced no-arm refusal below
    # r7 (Grok G9): and so is one with the measured session's pace_summary more than once, as on the delivery_kind path below ($paces -ne 1): a line
    # from a later pace window never stands in for the measured Play
    $controlMech = $null
    $controlMechUnreadable = ($withKind.Count -eq 0 -and $candidates.Count -gt 0 -and ($candidates.Count -gt 1 -or $paces -ne 1 -or $keyShapeUnreadable))
    # r7 (r7 adversary pass 1 A2): the app's delivery= is "none" alone (reset, no arm this Play) or a "+"-join of distinct mechanisms (the first arm
    # replaces "none", and a later arm appends its mechanism only when the join lacks it; MainWindow.cpp, the arm path), so none+qtimer or a repeated
    # mechanism is unreadable on both paths
    $mechReadable = { param([string]$m) ($m -cmatch '^(?:none|(?:native-msg|qtimer|notifier)(?:\+(?:native-msg|qtimer|notifier))*)$') -and
                      (@($m -split '\+' | Sort-Object -Unique).Count -eq @($m -split '\+').Count) }
    # r7 (r7 adversary pass 1 A1): a control binary prints the same `playback_due_wake armed=<n>` start (46d3472b's playbackDueWakeSummaryLine), so a
    # control line without one plain armed count is unreadable as on the delivery_kind path below
    $armedReadable = { param([string]$l) ($l -cmatch ($script:DvRecordStart + 'playback_due_wake armed=[0-9]+(?:\s|$)')) -and
                       ([regex]::Matches($l, '(?:^|\s)armed=(?<a>\S*)').Count -eq 1) }
    if ($candidates.Count -eq 1 -and $withKind.Count -eq 0) {
        $cm = [regex]::Matches($candidates[0], '(?:^|\s)delivery=(?<m>\S*)')
        if (-not (& $armedReadable $candidates[0]) -or $cm.Count -gt 1 -or ($cm.Count -eq 1 -and -not (& $mechReadable $cm[0].Groups['m'].Value))) { $controlMechUnreadable = $true }
        elseif ($cm.Count -eq 1) { $controlMech = $cm[0].Groups['m'].Value }
    }
    if ($withKind.Count -gt 0) {
        $mentions = $withKind.Count
        if ($paces -ne 1 -or $candidates.Count -ne 1 -or $keyShapeUnreadable) { $unreadableSeen = $true }
        else {
            $l = $candidates[0]
            $fields = [regex]::Matches($l, '(?:^|\s)delivery_kind=(?<k>\S*)')
            $mech = [regex]::Matches($l, '(?:^|\s)delivery=(?<m>\S*)')
            # r3 (r2 adversary pass 7 advisory A1): the app's record starts `playback_due_wake armed=<n>` (MainWindow::playbackDueWakeSummaryLine) with one
            # non-negative decimal count; a record without it, or with an empty, signed, fractional or repeated armed=, is unreadable
            # r8 (r8 adversary pass 4 A1): a binary that prints delivery_kind always prints disabled=, so a line with no disabled= cannot show the
            # kill switch off and is unreadable too ($dis counts this one candidate's disabled= tokens; a control line with none stays as r5 left it)
            if (-not (& $armedReadable $l) -or $disabledUnreadable -or $dis.Count -ne 1) { $unreadableSeen = $true }
            elseif ($fields.Count -ne 1 -or $fields[0].Groups['k'].Value -cnotmatch '^(send|post|notifier|qtimer|none)$' -or $mech.Count -ne 1 -or -not (& $mechReadable $mech[0].Groups['m'].Value)) { $unreadableSeen = $true }
            else {
                # r1 adversary pass 3 A1: the selection and the arms must agree -- none only with no arm (delivery=none), qtimer only on QTimer arms, notifier
                # only on notifier (or fallback QTimer) arms, send / post only on native-message (or fallback QTimer) arms; any other pair is contradictory
                $k = $fields[0].Groups['k'].Value
                $parts = @($mech[0].Groups['m'].Value -split '\+')
                $allowed = switch ($k) { 'none' { @('none') } 'qtimer' { @('qtimer', 'none') } 'notifier' { @('notifier', 'qtimer', 'none') } default { @('native-msg', 'qtimer', 'none') } }
                # r24 (r24 adversary pass 2 A2 sibling): each part is one of the allowed tokens ORDINALLY (-cnotin is linguistic: native-msg<U+00AD> matched)
                if (@($parts | Where-Object { $p = [string]$_; @($allowed | Where-Object { [string]::Equals($p, $_, [StringComparison]::Ordinal) }).Count -eq 0 }).Count -gt 0) { $unreadableSeen = $true }
                else { $observed = $k; $mechanism = $mech[0].Groups['m'].Value; $delivered = Get-DvDueWakeDelivered -Line $l }
            }
        }
    }
    $unreadable = 'the due-wake delivery is unreadable: the measured session''s playback_due_wake line (after its playback_smoke.pace_summary, before the next) is not exactly one line carrying one delivery_kind of send, post, notifier, qtimer, none and one delivery= of native-msg, qtimer, notifier, none or a "+"-join (MainWindow::playbackDueWakeSummaryLine)'
    # r8 (B1): the reason both paths give when the measured line shows no delivered wake
    $notDelivered = "DUE_WAKE_NOT_ENGAGED: the measured session's playback_due_wake line shows $(if ($delivered -ceq 'none') { 'no wake delivered by its route (fired= minus post_rescued= is below 1)' } else { 'no readable fired= or post_rescued= count (exactly one whole number each, with exactly one post_counts_settled=1: an unsettled post count may still move)' }), so no armed wake was delivered to the GUI thread by the route the leg names (fired= counts the wakes it took, a lost post the poll tick rescued included; an arm replaced or cancelled first delivers nothing) and the run measured no delivered wake"
    if ($sessionUnreadable) {
        # r10 (sol r9 B1, its input: `playback_smoke.measured_session id=3x` before session 3's posted wake and a valid id=4 was HONOURED): FAIL CLOSED for
        # every leg, the posted one and the head arm alike; never the numeric prefix's wake and never a later session's
        $verdict = 'NOT_HONOURED'; $reason = 'DUE_WAKE_SESSION_UNREADABLE: the first playback_smoke.measured_session id= (or, with none, the first playback_smoke.summary session=) is not one whole decimal id up to whitespace or the end of the line, so no playback_due_wake line can be read as the measured Play''s'
    }
    elseif ($boundaryUnreadable) {
        # r11 (r10 adversary A2, its input: an empty `playback_smoke.measured_session id=` or `playback_smoke.start session=` after session 3's pace_summary,
        # then a posted wake, was HONOURED): the boundary may be another Play's, so no wake after it is the measured Play's; FAIL CLOSED for every leg
        $verdict = 'NOT_HONOURED'; $reason = 'DUE_WAKE_SESSION_BOUNDARY_UNREADABLE: inside the measured session''s window (after its playback_smoke.pace_summary) a playback_smoke session boundary (measured_session, start, summary, conform, pace_summary, or a record whose first field is session) is not read whole as id=<n> / session=<n>, so the measured Play''s end is unknown and no later playback_due_wake line can be read as its own'
    }
    elseif ($requested) {
        $verdict = 'NOT_HONOURED'
        if ($mentions -eq 0) { $reason = 'ROUTE_NOT_ENGAGED: the measured session has no playback_due_wake line with a delivery_kind (after its playback_smoke.pace_summary), so the binary does not report how its wake was delivered' }
        elseif ($null -eq $observed) { $reason = $unreadable }
        elseif ($observed -cne 'post') { $reason = "ROUTE_NOT_ENGAGED: the measured session's playback_due_wake says delivery_kind=$observed, not the post the leg names" }
        # r1 adversary pass 1 A2: the posted arm counts only when every arm of the measured Play went by the native message
        elseif ($mechanism -cne 'native-msg') { $reason = "DUE_WAKE_DELIVERY_FELL_BACK: the measured Play's arms used delivery=$mechanism, not only the native message the posted arm names" }
        # r5 (r5 adversary pass 11 advisory A2): the kill switch on contradicts any engagement the same line claims
        elseif ($disabledSeen) { $reason = 'ROUTE_NOT_ENGAGED: the measured session''s playback_due_wake line says disabled=1 (MLVAPP_PLAYBACK_DUE_WAKE=0), which contradicts the posted wake it reports' }
        # r8 (B1, sol + key3 opus r7, their input: armed=1200 fired=0 ... delivery_kind=post was HONOURED): a posted arm that delivered no wake
        elseif ($delivered -cne 'delivered') { $reason = $notDelivered }
        else { $verdict = 'HONOURED' }
    }
    elseif ($disabledSeen) {
        # r4 (r4 adversary pass 9 A1, its input: the committed paced head cell with an inherited MLVAPP_PLAYBACK_DUE_WAKE=0): the kill switch is never a
        # leg's own arm (no leg spec sets it), and it also changes how an unpaced Play advances, so disabled=1 blocks every leg that does not ask, a control
        # binary's line included
        $verdict = 'NOT_HONOURED'; $reason = 'DUE_WAKE_DISABLED_INHERITED: the measured session''s playback_due_wake line says disabled=1 (MLVAPP_PLAYBACK_DUE_WAKE=0 came from the launcher), so this run is not the arm its leg names'
    }
    elseif ($disabledUnreadable) {
        # r5 (Grok G8 = r4 adversary pass 10 advisory A2): a control binary's line (no delivery_kind, so no mention) with disabled=yes, an empty disabled=
        # or a repeated disabled= cannot show the kill switch off, so it blocks like a line with a delivery_kind does (the $unreadableSeen path above)
        $verdict = 'NOT_HONOURED'; $reason = 'DUE_WAKE_DISABLED_UNREADABLE: the kill switch is unreadable: the measured session''s playback_due_wake records carry a disabled= that is not exactly one 0 or 1 (MainWindow::playbackDueWakeSummaryLine prints one), so the kill switch is not shown off'
    }
    elseif ($controlMechUnreadable) {
        # r5 (r5 adversary pass 12 A1): the control line's delivery= cannot show which arms the measured Play used
        $verdict = 'NOT_HONOURED'; $reason = 'the due-wake delivery is unreadable: the measured session''s playback_due_wake records carry no delivery_kind and are more than one or follow more than one of its pace_summary lines, or the one line does not start with one plain armed= count, or its delivery= is repeated or is not none alone or a "+"-join of distinct native-msg, qtimer, notifier'
    }
    elseif ($controlMech -ceq 'none' -and -not $unpacedLeg) {
        # r5 (r5 adversary pass 12 A1, its input: the committed paced cell with a control line saying delivery=none): the r4 rule below, on a line
        # with no delivery_kind
        $verdict = 'NOT_HONOURED'; $reason = 'DUE_WAKE_NOT_ENGAGED: the leg is paced (no generatorArgs.unpaced) but its measured Play armed no due wake (delivery=none, no delivery_kind), so it measured no wake; only an unpaced leg runs without one'
    }
    elseif ($mentions -gt 0) {
        # r1 adversary passes 1 A1 / 2 A1: a selection the variable makes (post, notifier) is inherited whatever the arms used; qtimer (no native wake
        # built) is the app's own; send is the head arm, and counts only when no arm fell back (pass 1 A2). r4 (pass 9 A1): no arm in the measured Play
        # (delivery=none, delivery_kind none or not) is the app's own only on an unpaced leg (generatorArgs.unpaced = true, which never arms); a paced leg
        # that armed no wake measured no sent wake
        if ($null -eq $observed) { $verdict = 'NOT_HONOURED'; $reason = $unreadable }
        elseif ($observed -cin @('post', 'notifier')) { $verdict = 'NOT_HONOURED'; $reason = "DUE_WAKE_DELIVERY_INHERITED: the leg does not ask for the posted wake but the app reports delivery_kind=$observed (MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY came from the launcher), so this run is not the head arm" }
        elseif ($mechanism -ceq 'none' -and -not $unpacedLeg) { $verdict = 'NOT_HONOURED'; $reason = "DUE_WAKE_NOT_ENGAGED: the leg is paced (no generatorArgs.unpaced) but its measured Play armed no due wake (delivery=none, delivery_kind=$observed), so it measured no sent wake; only an unpaced leg runs without one" }
        elseif ($observed -ceq 'send' -and $mechanism -cnotin @('native-msg', 'none')) { $verdict = 'NOT_HONOURED'; $reason = "DUE_WAKE_DELIVERY_FELL_BACK: the head arm's measured Play used delivery=$mechanism, not only the sent native message" }
        # r8 (B1): a paced leg's measured Play that armed but delivered no wake (fired=0, or no readable fired=) measured no wake, the sent head arm
        # included (its sent-head twin of the B1 input was unblocked); only an unpaced leg (it never arms) is exempt
        elseif (-not $unpacedLeg -and $delivered -cne 'delivered') { $verdict = 'NOT_HONOURED'; $reason = $notDelivered }
    }
    [ordered]@{ requested = $requested; observed = $observed; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED') }
}

function Get-DvTeamPartitionEvidence {
    <#
    .SYNOPSIS
    CPU-S1-FS-SPIKE-TEAM-PARTITION-1: did a leg that asks for the asymmetric playback OpenMP team partition (generatorArgs.reconOmpThreads = r and
    generatorArgs.prepOmpThreads = p, both or neither, integers 1..16; the job sets MLVAPP_PLAYBACK_RECON_OMP_THREADS=r and MLVAPP_PLAYBACK_PREP_OMP_THREADS=p)
    run partitioned at exactly r+p? Read ONLY from the app's own `playback_smoke.omp_teams session=<n> partition=<off|r+p> recon_omp=<n> recon_cpu_cores=<n>
    prep_omp=<n> prep_pointwise=<n> prefetch_decoders=<n> logical_cpus=<n> thread_limit=<n> max_active_levels=<n> dynamic=<0|1> override=<0|1>` record for the MEASURED session (integers the app derived, no env
    text), matched as a whole line, never from the start line, the spec or the launcher's env block. FAIL CLOSED: `blocks` is true when the leg asked and there
    is no such record (a binary before this card emits none), the record is unreadable, not unique or internally inconsistent, or it reports anything but
    partition=r+p, recon_omp=r, prep_omp=p and override=0; or when the spec names only one of the two. A leg that does not ask blocks only when the app reports
    a partition (the control arm inherited a variable: TEAM_PARTITION_INHERITED) or a record is present but unreadable; with no record (a control binary
    before this card, which reads neither variable), or a clean partition=off, it is unchanged. observed is the record's partition text ('off' or 'r+p') or
    $null. Pure and shared: the runner gates on it and Test-DvReceiptValid re-derives it from the hashed run log.
    #>
    param([Parameter(Mandatory)]$Spec, [AllowNull()][AllowEmptyString()][string]$LogText)
    $ga = Get-DvProp $Spec 'generatorArgs'
    # adversary A3 on 84d66a4b: a PRESENT key, null included, names the partition (absent is the only "not asked for"), so a malformed spec is refused,
    # never read as a control leg. adversary A4: the leg's shape check (Get-DvLegSpecShapeRefusal) refuses anything but a JSON int 1..16 before a lease
    # opens; this reader applies the same rule, so the two never disagree.
    $hasKey = {
        param($o, [string]$k)
        if ($null -eq $o) { return $false }
        if ($o -is [System.Collections.IDictionary]) { return $o.Contains($k) }
        return ($null -ne $o.PSObject.Properties[$k])
    }
    $rawRecon = Get-DvProp $ga 'reconOmpThreads'
    $rawPrep = Get-DvProp $ga 'prepOmpThreads'
    $isTeam = { param($v) ($v -is [int] -or $v -is [long]) -and $v -ge 1 -and $v -le 16 }
    $named = (& $hasKey $ga 'reconOmpThreads') -or (& $hasKey $ga 'prepOmpThreads')
    $requested = $named
    $requestedText = $(if ((& $isTeam $rawRecon) -and (& $isTeam $rawPrep)) { "$rawRecon+$rawPrep" } else { $null })
    $observed = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    $records = 0
    $mentions = 0
    $consistent = $false
    $rec = $null
    if (-not [string]::IsNullOrEmpty($LogText)) {
        $session = (Get-DvSmokeSummaryFields -LogText $LogText).session
        if ($null -ne $session) {
            $n = '(?:0|[1-9][0-9]{0,8})'
            $p = '(?:[1-9][0-9]{0,8})'
            $fields = "partition=(?<part>off|(?<pr>$n)\+(?<pp>$n)) recon_omp=(?<r>$n) recon_cpu_cores=(?<rc>$p) prep_omp=(?<q>$n) prep_pointwise=(?<qp>$p)" +
                " prefetch_decoders=$n logical_cpus=$p thread_limit=(?<tl>[1-9][0-9]{0,9}) max_active_levels=(?<ml>(?:0|[1-9][0-9]{0,9})) dynamic=(?<dy>[01]) override=(?<o>[01])"
            $whole = Get-DvCanonicalRecordPattern -Marker 'playback_smoke.omp_teams' -Session ([regex]::Escape([string]$session)) -Fields $fields
            foreach ($l in (Get-DvLogRecordBodies -LogText $LogText)) {
                if (-not (Test-DvRecordMentionsSession -Body $l -Marker 'playback_smoke.omp_teams' -Session ([string]$session) -Fields $fields)) { continue }
                $mentions++
                $m = [regex]::Match($l, $whole)
                if ($m.Success) {
                    $records++
                    $rec = [ordered]@{ part = $m.Groups['part'].Value; reconReq = $m.Groups['pr'].Value; prepReq = $m.Groups['pp'].Value
                                       recon = [long]$m.Groups['r'].Value; prep = [long]$m.Groups['q'].Value; cores = [long]$m.Groups['rc'].Value
                                       pointwise = [long]$m.Groups['qp'].Value; limit = [decimal]$m.Groups['tl'].Value; levels = [decimal]$m.Groups['ml'].Value
                                       dynamic = [int]$m.Groups['dy'].Value; override = [int]$m.Groups['o'].Value }
                    # adversary A2 on 84d66a4b: the record is read whole. off = no team effective (0 / 0) and both counts the one playback count; r+p = each
                    # effective team at most its request and each named side's count exactly its request. Pass 2 on 95d5498e (A1, A3): each effective team
                    # is also at most thread_limit, and at most 1 when max_active_levels=0 (OpenMP then runs every region on one thread).
                    $cap = $(if ($rec.levels -eq 0) { [decimal]1 } else { $rec.limit })
                    $consistent = $(if ($rec.part -ceq 'off') { $rec.recon -eq 0 -and $rec.prep -eq 0 -and $rec.cores -eq $rec.pointwise }
                                    else {
                                        $rr = [long]$rec.reconReq; $pq = [long]$rec.prepReq
                                        ($rr -gt 0 -or $pq -gt 0) -and $rec.recon -le $rr -and $rec.prep -le $pq -and ($rr -eq 0 -or $rec.cores -eq $rr) -and ($pq -eq 0 -or $rec.pointwise -eq $pq) -and
                                            $rec.recon -le $cap -and $rec.prep -le $cap
                                    })
                }
            }
        }
    }
    if ($records -ne 1 -or $mentions -ne 1 -or -not $consistent) { $rec = $null }
    # observed is the EFFECTIVE split ("off", or "<recon_omp>+<prep_omp>"), never the request text (adversary A1 on 84d66a4b)
    if ($null -ne $rec) { $observed = $(if ($rec.part -ceq 'off') { 'off' } else { "$($rec.recon)+$($rec.prep)" }) }
    $unreadable = 'the team partition is unreadable: the measured session''s playback_smoke.omp_teams record is not exactly one whole, self-consistent line of the form playback_smoke.omp_teams session=<n> partition=<off|r+p> recon_omp=<n> recon_cpu_cores=<n> prep_omp=<n> prep_pointwise=<n> prefetch_decoders=<n> logical_cpus=<n> thread_limit=<n> max_active_levels=<n> dynamic=<0|1> override=<0|1>, or there is more than one'
    if ($requested) {
        $verdict = 'NOT_HONOURED'
        if ($null -eq $requestedText) { $reason = "TEAM_PARTITION_SPEC_INVALID: the spec's reconOmpThreads ($(ConvertTo-Json -InputObject $rawRecon -Compress)) and prepOmpThreads ($(ConvertTo-Json -InputObject $rawPrep -Compress)) are not both whole numbers from 1 to 16 (both or neither)" }
        elseif ($mentions -eq 0) { $reason = 'ROUTE_NOT_ENGAGED: the run log has no playback_smoke.omp_teams record for the measured session, so the binary does not report its OpenMP teams' }
        elseif ($null -eq $rec) { $reason = $unreadable }
        elseif ($rec.override -ne 0) { $reason = 'ROUTE_NOT_ENGAGED: the measured session''s playback_smoke.omp_teams says override=1 (MLVAPP_FORCE_THREADS / MLVAPP_FORCE_SINGLETHREAD make the partition inert)' }
        # r7 pass A1 on 25262f79: dynamic=1 and max_active_levels=0 can only take a team DOWN TO one thread, so they refuse only a request wider than one on a side
        # (a 1+1 request reported as 1+1 is the split the leg names; the partition text, effective teams and counts below still have to match it exactly).
        elseif ($rec.dynamic -ne 0 -and ([long]$rawRecon -gt 1 -or [long]$rawPrep -gt 1)) { $reason = 'ROUTE_NOT_ENGAGED: the measured session''s playback_smoke.omp_teams says dynamic=1 (OpenMP may shrink any team below its request, so the split is not the one the leg names)' }
        elseif ($rec.levels -eq 0 -and ([long]$rawRecon -gt 1 -or [long]$rawPrep -gt 1)) { $reason = 'ROUTE_NOT_ENGAGED: the measured session''s playback_smoke.omp_teams says max_active_levels=0 (OpenMP serializes every team to one thread, so the split is not the one the leg names)' }
        elseif ($rec.part -cne $requestedText) { $reason = "ROUTE_NOT_ENGAGED: the measured session's playback_smoke.omp_teams says partition=$($rec.part), not the $requestedText the leg names" }
        elseif ($observed -cne $requestedText -or $rec.cores -ne [long]$rawRecon -or $rec.pointwise -ne [long]$rawPrep) { $reason = "ROUTE_NOT_ENGAGED: the measured session's effective teams are $observed (recon_cpu_cores=$($rec.cores) prep_pointwise=$($rec.pointwise) thread_limit=$($rec.limit)), not the $requestedText the leg names" }
        else { $verdict = 'HONOURED' }
    }
    elseif ($mentions -gt 0) {
        if ($null -eq $observed) { $verdict = 'NOT_HONOURED'; $reason = $unreadable }
        elseif ($observed -cne 'off') { $verdict = 'NOT_HONOURED'; $reason = "TEAM_PARTITION_INHERITED: the leg names no partition but the app reports partition=$observed (a variable came from the launcher's environment), so this run is not the control arm" }
    }
    [ordered]@{ requested = $requestedText; observed = $observed; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED'); named = $named }
}

# The flavors the app can apply (src/batch/LookAssistAnalysis.cpp lookAssistFlavorName). The app's `look_assist_flavor=none` (nothing applied) is not among them.
$script:DvAppliedLookFlavors = [string[]]@('classic', 'cinematic', 'film', 'vision50d')
# HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r26 (sol r25 B2 on 620182b3, its inputs: `look_assist_flavor=cinematic` followed by U+200B, U+00AD or
# U+FEFF): a flavor token is ASCII-canonical (only [a-z0-9_], matched case-sensitively and culture-free) or it is UNREADABLE. PowerShell's -ceq / -ccontains
# compare linguistically, where those characters are ignorable, so a ten-character `cinematic<U+200B>` equalled `cinematic` in the whitelist and in both
# claim predicates. Every flavor comparison goes through Test-DvAppliedLookFlavor / Test-DvLookFlavorSame: canonical, then ordinal.
$script:DvCanonicalFlavorToken = [regex]::new('\A[a-z0-9_]+\z', [System.Text.RegularExpressions.RegexOptions]::CultureInvariant)
# r26 adversary A1 + A2 on 2f131c49: where a look_assist key STARTS for the ambiguity counts -- after any character that is not an ASCII name character,
# matched case-sensitively inside a (?i) / $script:DvCi pattern (as $script:DvNameChar), or at the start of the body. Under IgnoreCase a bare [A-Za-z0-9_]
# folds U+212A (Kelvin sign) to `k`, which hid a masked conflicting key; a whitespace-only `(^|\s)` start missed `,look_assist_flavor=classic`.
$script:DvLookKeyStart = '(?<!(?-i:[A-Za-z0-9_]))'

function Get-DvLookKeyTokens {
    <#
    .SYNOPSIS
    r26 adversary pass 2 A1 on eed44c40: every token one look_assist key names in a record body, for the AMBIGUITY counts of Get-DvLookAssistEvidence.
    A mention is the key in any case ($script:DvCi) bounded on both sides by non-name characters (ASCII, case-sensitive: $script:DvLookKeyStart and the
    lookahead below). A mention followed by a plain `=` yields its value (`\S*`, empty included); any other mention -- a masked `=` (U+001A), a space,
    a colon, nothing -- yields the mask token, UNREADABLE and never an applied flavor. So a malformed key is a token, never absent; it never proves one.
    #>
    param([AllowEmptyString()][string]$Body, [Parameter(Mandatory)][ValidatePattern('^[a-z_]+$')][string]$Key)
    $mask = [string]$script:DvQuotedEqualsMask
    foreach ($m in [regex]::Matches($Body, ($script:DvLookKeyStart + $Key + '(?!(?-i:[A-Za-z0-9_]))(?<eq>=(?<v>\S*))?'), $script:DvCi)) {
        if ($m.Groups['eq'].Success) { $m.Groups['v'].Value } else { $mask }
    }
}

function Test-DvLookFlavorSame {
    <#
    .SYNOPSIS
    r26 (sol r25 B2): are A and B the same look flavor? True only when both are strings, A is ASCII-canonical ([a-z0-9_]+, nothing else) and B equals A
    ORDINALLY. An empty, non-string or non-canonical value is the same as nothing, so it never backs a flavor claim.
    #>
    param([AllowNull()]$A, [AllowNull()]$B)
    ($A -is [string]) -and ($B -is [string]) -and $script:DvCanonicalFlavorToken.IsMatch($A) -and [string]::Equals($A, $B, [StringComparison]::Ordinal)
}

function Test-DvAppliedLookFlavor {
    # r26 (sol r25 B2): is the token one of the flavors the app can apply, canonical and compared ordinally (Array.IndexOf on string[] is ordinal)?
    param([AllowNull()]$Token)
    ($Token -is [string]) -and $script:DvCanonicalFlavorToken.IsMatch($Token) -and ([Array]::IndexOf($script:DvAppliedLookFlavors, $Token) -ge 0)
}

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
    $flavorReason = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    if ($requested) {
        $found = $null
        # r27 (sol r26 B1 on af784b7b): whether the answering record's DISCARDED text (Get-DvLogRecordDiscardedText) mentions look_assist_enabled
        $foundEnabledDiscarded = $false
        # VENUE-NATIVE-LEG-LOOKLEG-LABEL-1 r2 (Dot bd11a92): every flavor token of every visual_state record, so duplicate records that disagree can be told from one that is repeated
        $flavorsSeen = [System.Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
        if (-not [string]::IsNullOrEmpty($LogText)) {
            # r4 (r3 adversary A2 on #726): matched on the record body, so a Qt location path never selects (or answers for) the visual_state line
            # r27: the raw lines, index for index with the bodies (Get-DvLogRecordBodies splits the same way and keeps every line), for the discarded text
            $rawLines = $LogText -split $script:DvLogLineBreak
            $bodies = Get-DvLogRecordBodies -LogText $LogText
            for ($li = 0; $li -lt $bodies.Count; $li++) {
                $line = $bodies[$li]
                # r3 (r2 adversary pass 6 A3): the visual_state record only where it starts the record body (logInteractionEvent writes
                # `interaction_trace event=<name> ...`), never an `event=gui_smoke.visual_state` text inside another record
                # r13 (r12 adversary A1): culture-invariant ($script:DvCi), so a tr-TR `INTERACTION_TRACE event=gui_smoke.visual_state look_assist_enabled=0`
                # record answers (and refuses the leg) as it does in every other culture, never skipped
                if ([regex]::IsMatch($line, ($script:DvRecordStart + 'interaction_trace event=gui_smoke\.visual_state(\s|$)'), $script:DvCi)) {
                    $found = $line
                    # HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r25 (r25 adversary A1 on 217e0726, its input: `look_assist_flavor=cinematic
                    # diagnostic=' look_assist_flavor=classic`): a flavor key with a MASKED `=` (ConvertTo-DvQuoteMaskedBody) is UNREADABLE, never absent: it
                    # is a flavor token of its own (the mask character, never an applied flavor), so the record names more than one flavor and none is proven.
                    # r25 pass 2 A1 (a37b59c9, its input: `... diagnostic=' LOOK_ASSIST_FLAVOR=classic`): the key is counted in ANY case ($script:DvCi), plain
                    # or masked, so a differently cased flavor key is a token too (counting only ever adds tokens: it never proves a flavor, see $exactFlavor below)
                    # r26 adversary A1 + A2 on 2f131c49 (inputs: `... diagnostic=' <U+212A>look_assist_flavor=classic` and `... ,look_assist_flavor=classic`):
                    # a key counts wherever it is bounded by a NON-name character ($script:DvLookKeyStart: ASCII, case-sensitive even under $script:DvCi, so the
                    # Kelvin sign is no name character and a comma, quote or any other punctuation starts a key), never only after whitespace
                    # r26 adversary pass 2 A1 on eed44c40 (inputs: `look_assist_flavor =classic`, a bare `look_assist_flavor`, `LOOK_ASSIST_FLAVOR:classic`,
                    # `diagnostic='look_assist_flavor classic end'`): EVERY bounded mention of the key is a token (Get-DvLookKeyTokens); one not followed by
                    # a plain `=` is UNREADABLE (the mask token), never absent
                    $lineFlavorTokens = @(Get-DvLookKeyTokens -Body $line -Key 'look_assist_flavor')
                    foreach ($ft in $lineFlavorTokens) { [void]$flavorsSeen.Add($ft) }
                    # r26 (sol r25 B1 on 620182b3, its input: an earlier `... diagnostic="harmless (text)" x (look_assist_flavor=classic` record, cut to
                    # LOCATION_UNREADABLE, then an intact cinematic record): a visual_state record that LOST ITS TAIL may have named any flavor there, so it
                    # adds an UNREADABLE flavor token ($script:DvLocationUnreadable holds spaces, so no `\S*` token ever equals it) and no flavor is proven,
                    # exactly as an intact conflicting record. Lost means (a) the LOCATION_UNREADABLE sentinel ends it, or (b) it carries no look_assist_flavor
                    # key at all (plain or masked, any case): the app writes that key LAST (MainWindow.cpp gui_smoke.visual_state, %48), so a record without
                    # it was cut short -- opus key3 r25 DV-VISUAL-STATE-UNQUOTED-PAREN-VALUE-1: the UNQUOTED reject reason `transform (upside down) enabled`
                    # (GpuPreviewProcessing.cpp) opens a ` (` that Get-DvLocationOpenIndex takes for the location, and with a readable `:<line>)` tail the
                    # flavor vanished with no sentinel at all. An app build that writes no flavor key proves no flavor either way (exactFlavor below).
                    if ($line.EndsWith($script:DvLocationUnreadable, [StringComparison]::Ordinal) -or $lineFlavorTokens.Count -eq 0) { [void]$flavorsSeen.Add($script:DvLocationUnreadable) }
                    # r27 (sol r26 B1 on af784b7b, its input: `look_assist_flavor=cinematic gpu_preview_processing_reject_reason=transform (upside down)
                    # enabled look_assist_flavor=classic (C:/b/MainWindow.cpp:10011)`): the cut at the unquoted ` (` kept the earlier cinematic token, the
                    # READABLE `:<line>)` ending left no sentinel, and the conflicting classic was discarded, so cinematic was proven. INV-1: ANY bounded
                    # mention of the key in the text the cut discarded (Get-DvLogRecordDiscardedText, the location included) may be a discarded value, so it
                    # adds the UNREADABLE token and no flavor is proven, in this record or any other. The same for look_assist_enabled in the answering record.
                    $discarded = Get-DvLogRecordDiscardedText -Line $rawLines[$li]
                    $foundEnabledDiscarded = $false
                    if ($discarded.Length -gt 0) {
                        if (@(Get-DvLookKeyTokens -Body $discarded -Key 'look_assist_flavor').Count -gt 0) { [void]$flavorsSeen.Add($script:DvLocationUnreadable) }
                        $foundEnabledDiscarded = (@(Get-DvLookKeyTokens -Body $discarded -Key 'look_assist_enabled').Count -gt 0)
                    }
                }
            }
        }
        $verdict = 'NOT_HONOURED'
        if ($null -eq $found) { $reason = 'the run log has no gui_smoke.visual_state line, so nothing shows the app ran with Look Assist on' }
        # r24 (r24 adversary pass 2 A1 sibling): a visual_state record that lost its tail to the LOCATION_UNREADABLE sentinel may have said
        # look_assist_enabled=0 there, so it is unreadable whole, never read from the fields before the cut
        elseif ($found.EndsWith($script:DvLocationUnreadable, [StringComparison]::Ordinal)) { $reason = "the app's last gui_smoke.visual_state line lost its tail to an unreadable location, so whether Look Assist was on is unreadable" }
        # r27 (sol r26 B1 sibling): the answering record's discarded text names look_assist_enabled, which may have said 0 there: unreadable whole, as r24
        elseif ($foundEnabledDiscarded) { $reason = "the app's last gui_smoke.visual_state line names look_assist_enabled in the text cut at its location (an unquoted ' (' before a readable location ending), so whether Look Assist was on is unreadable" }
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
                # r25 (r25 adversary A1 on 217e0726): a flavor or enabled key with a MASKED `=` in the answering record is a token of its own (UNREADABLE,
                # never absent), so it makes the record name two values and no flavor is proven from it, as a second plain token does (pass 4 A1 / A2)
                # r25 pass 2 A1 (a37b59c9): the ambiguity counts take the key in ANY case ($script:DvCi), plain or masked; the flavor itself is still proven
                # only by the app's own lower-case `look_assist_flavor=` token ($exactFlavor), so a differently cased key adds a token and never supplies one
                $maskToken = [string]$script:DvQuotedEqualsMask
                $exactFlavor = @([regex]::Matches($found, '(^|\s)look_assist_flavor=(?<f>\S*)') | ForEach-Object { $_.Groups['f'].Value })
                # r26 adversary A1 + A2 on 2f131c49: every ambiguity count takes a key bounded by any non-name character ($script:DvLookKeyStart), plain
                # or masked; the proof ($exactFlavor) stays the app's own whitespace-bounded lower-case token, so a wider count only ever adds tokens
                # r26 adversary pass 2 A1 on eed44c40: every bounded mention of either key, whatever follows it (Get-DvLookKeyTokens)
                $flavorTokens = @(Get-DvLookKeyTokens -Body $found -Key 'look_assist_flavor')
                $enabledTokens = @(Get-DvLookKeyTokens -Body $found -Key 'look_assist_enabled')
                # r26 (sol r25 B2): distinct ORDINALLY (Select-Object -Unique compares linguistically, so `cinematic` and `cinematic<U+200B>` were one
                # value), and the one value must be an ASCII-canonical applied flavor (Test-DvAppliedLookFlavor), never a -ccontains match
                $lastFlavors = @([System.Collections.Generic.HashSet[string]]::new([string[]]@($flavorTokens), [StringComparer]::Ordinal))
                $enabledValues = @([System.Collections.Generic.HashSet[string]]::new([string[]]@($enabledTokens), [StringComparer]::Ordinal))
                if ($exactFlavor.Count -ge 1 -and $flavorsSeen.Count -eq 1 -and $lastFlavors.Count -eq 1 -and $enabledValues.Count -eq 1 -and (Test-DvAppliedLookFlavor $lastFlavors[0])) { $flavor = $lastFlavors[0] }
                # r26 (opus key3 r25 DV-VISUAL-STATE-UNQUOTED-PAREN-VALUE-1): an unproven flavor is TYPED, never a silent null
                elseif ($flavorsSeen.Contains($script:DvLocationUnreadable)) { $flavorReason = 'FLAVOR_UNREADABLE: a gui_smoke.visual_state record lost its tail (an unreadable location, a cut before look_assist_flavor, which the app writes last, or a look_assist_flavor mention in the text cut at the location), so the applied flavor is unreadable' }
                elseif ($flavorsSeen.Count -gt 1 -or $lastFlavors.Count -gt 1) { $flavorReason = 'FLAVOR_AMBIGUOUS: the gui_smoke.visual_state records name more than one look_assist_flavor token, so no applied flavor is proven' }
                elseif ($exactFlavor.Count -eq 0) { $flavorReason = 'FLAVOR_ABSENT: the answering gui_smoke.visual_state record carries no lower-case look_assist_flavor= token' }
                elseif ($enabledValues.Count -ne 1) { $flavorReason = 'FLAVOR_UNPROVEN: the answering gui_smoke.visual_state record names look_assist_enabled more than once' }
                else { $flavorReason = 'FLAVOR_NOT_APPLIED: the look_assist_flavor token is not an ASCII-canonical flavor the app can apply (classic, cinematic, film, vision50d)' }
                # r24 (r24 adversary pass 2 A2 sibling): ORDINAL, so look_assist_enabled=<U+0661> or 1<U+200B> is never on
                if (-not [string]::Equals($reported, '1', [StringComparison]::Ordinal)) { $reason = "the app's gui_smoke.visual_state line says look_assist_enabled=$reported (the leg asks for Look Assist on)" }
                else { $verdict = 'HONOURED' }
            }
        }
    }
    [ordered]@{ requested = $requested; asked = $asked; reported = $reported; flavor = $flavor; flavorReason = $flavorReason; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED') }
}

# --- leg sets and the display matrix (DUAL-VENUE-DISPLAY-MATRIX-1) -------------------------------------------------------
# A LEG SET is a tracked list of committed leg specs that one run executes together. The set does not define legs: every cell is still an ordinary committed leg spec (so every receipt
# is bound to a committed spec by its sha256, exactly as for a single leg), and the set only says which specs make up the matrix, and in what order a run walks them.
$script:LegSetSchema = 'mlv-app/dual-venue-legset/v1'
$script:LegSetsRelativeDir = 'tools/profiling/dual-venue/legsets'

function Test-DvVisualTwinName {
    <#
    .SYNOPSIS
    M16-1457-VENUE-BASELINE-DRIFT-1 r6 (sol r5 B1, luna r5 B1): is this leg id or spec file name a VISUAL TWIN (its id carries the word `visual` between hyphens)? A twin is the
    capture-on copy of a speed arm: its fps is observer-perturbed (the capture tail keeps playing unpaced past the measured window), so it is never a measured matrix speed cell.
    Judged on the RESOLVED spec's own identity (its legId and its file leaf), never on the spelling of the reference that reached it, and case-folded (Windows resolves
    `LEGS\X.JSON` to the file `legs/x.json`).
    #>
    param([AllowNull()][AllowEmptyString()]$Name)
    ([string]$Name) -match '(?i)-visual-'
}

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
        # r7 (sol r6 INV-3): ONE read of the file's bytes serves both the judgment below and the plan entry's specSha256 (RAW sha256, no line-ending normalisation: the hash
        # Invoke-VenueLeg's -ExpectSpecSha256 takes), so the child that runs this entry is bound to the exact bytes the planner approved
        $specBytes = [IO.File]::ReadAllBytes($full)
        $specSha256 = Get-DvSha256OfBytes $specBytes
        $spec = [Text.Encoding]::UTF8.GetString($specBytes).TrimStart([char]0xFEFF) | ConvertFrom-Json
        if ([string]$spec.clipId -cne $clipId -or [string]$spec.card -cne $card) { throw "DVE_LEGSET_INVALID leg '$($spec.legId)' does not name the set's clip and card" }
        # r6: refused by the RESOLVED spec (its legId and its file leaf), so no spelling of the reference gets a visual twin into a speed-cell matrix
        if ((Test-DvVisualTwinName $spec.legId) -or (Test-DvVisualTwinName ([IO.Path]::GetFileName($full)))) {
            throw "DVE_LEGSET_INVALID leg '$($spec.legId)' is a visual twin: its fps is capture-perturbed and is never a measured matrix speed cell"
        }
        $specs.Add([pscustomobject]@{ path = $full; spec = $spec; sha256 = $specSha256 })
    }
    $backends = [System.Collections.Generic.List[string]]::new()
    foreach ($s in $specs) { foreach ($b in @($s.spec.backends)) { if ($b -notin $backends) { $backends.Add([string]$b) } } }
    $cells = [System.Collections.Generic.List[object]]::new()
    $seen = @{}
    foreach ($b in $backends) {
        foreach ($s in $specs) {
            if ($b -notin @($s.spec.backends)) { continue }
            $mode = $(if ($null -ne (Get-DvProp $s.spec 'displayMode')) { [string]$s.spec.displayMode } else { 'fullscreen' })
            # EXPORT-VENUE-LEG-1: an export leg plays nothing and has no display mode; its cell is its format, so both formats of one clip are two cells
            $cellId = $(if ([string](Get-DvProp $s.spec 'legType') -ceq 'export') { "$b-export-$([string]$s.spec.export.format)" } else { "$b-$mode-s$([int]$s.spec.scaleFactor)" })
            if ($seen.ContainsKey($cellId)) { throw "DVE_LEGSET_INVALID two legs of the set make the cell $cellId" }
            $seen[$cellId] = $true
            $cells.Add([pscustomobject]@{ cellId = $cellId; legId = [string]$s.spec.legId; specPath = $s.path; specSha256 = $s.sha256; backend = $b; displayMode = $mode; scaleFactor = [int]$s.spec.scaleFactor })
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
            $plan.Add([pscustomobject]@{ seq = $seq; repeat = $r; cellId = $c.cellId; legId = $c.legId; specPath = $c.specPath; specSha256 = $c.specSha256; backend = $c.backend; displayMode = $c.displayMode; scaleFactor = $c.scaleFactor })
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
    $unreadableRecord = $false
    $found = $null
    if (-not [string]::IsNullOrEmpty($LogText)) {
        $session = (Get-DvSmokeSummaryFields -LogText $LogText).session
        if ($null -ne $session) {
            # r3 (r2 adversary pass 6 A3): the record only where it starts the record body
            $pattern = $script:DvRecordStart + 'playback_smoke\.' + $Marker + ' session=' + [regex]::Escape([string]$session) + '(\s|$)'
            # r4 (r3 adversary A2 on #726): the marker is matched on each line's record body, so a later line whose build path holds
            # `playback_smoke.<Marker> session=<n>` is never selected
            # r13 (r12 adversary A1 sibling): culture-invariant ($script:DvCi)
            foreach ($line in (Get-DvLogRecordBodies -LogText $LogText)) { if ([regex]::IsMatch($line, $pattern, $script:DvCi)) { $found = $line } }
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
        # HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r24 (r24 adversary A1 on c1b10b4f, its input: a start line whose
        # env_playback_scope_interval_ms value is a lone `'`, so the mask hid the real env_omp_num_threads=1 after it and the inherited override read
        # absent, HONOURED): a key written with a MASKED `=` (inside a quoted span, ConvertTo-DvQuoteMaskedBody) is UNREADABLE, never absent. It joins
        # the duplicates, which every consumer already refuses (Get-DvInheritedWorkerOverrides names it unreadable; Get-DvSmokeSessionLineFields leaves
        # it out, fail closed). The key is bounded by any non-name character, so a quote-glued key counts too.
        foreach ($m in [regex]::Matches($found, '(?<![A-Za-z0-9_])(?<k>[A-Za-z0-9_]+)\x1A')) { [void]$duplicates.Add($m.Groups['k'].Value) }
        # r24 (r24 adversary pass 2 A1 sibling): a record that lost its tail to the LOCATION_UNREADABLE sentinel may have named any key there, so it is
        # UNREADABLE whole: every key it shows is a duplicate (no field is read) and `unreadable` tells a reader that treats absence as clean
        # (Get-DvInheritedWorkerOverrides) that every key it needs is unreadable too
        if ($found.EndsWith($script:DvLocationUnreadable, [StringComparison]::Ordinal)) { $unreadableRecord = $true; foreach ($k in @($seen.Keys)) { [void]$duplicates.Add($k) } }
        foreach ($k in $seen.Keys) { if (-not $duplicates.Contains($k)) { $fields[$k] = $seen[$k] } }
    }
    @{ found = ($null -ne $found); fields = $fields; duplicates = @($duplicates | Sort-Object); unreadable = [bool]$unreadableRecord }
}

# r5 (sol r4 B1 on #726): what Get-DvLogRecordBody puts in place of a location it cannot read. It holds no `=` and no record marker, so no field or
# marker is read from it, and a whole-line match (the prep_route record) fails on it: the record is unreadable, never absent and never forged.
$script:DvLocationUnreadable = ' (LOCATION_UNREADABLE)'
# HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r12 (r12 adversary B3 on 7e66a3af): ONE name character of a record name, key or identity word, ASCII and
# matched case-sensitively even inside a (?i) pattern or a PowerShell -match. Under IgnoreCase .NET folds U+212A (Kelvin sign) and U+0130 into [A-Za-z],
# so `<U+212A>session=4` hid the session mention; the inline (?-i:...) keeps every non-ASCII neighbour a non-name character.
$script:DvNameChar = '(?-i:[A-Za-z0-9_])'
# r12 (r12 adversary pass 13 A2 on bbfe69d1, its input: tr-TR culture and a first `PLAYBACK_SMOKE.MEASURED_SESSION id=3`): every case-insensitive
# read of a record name, an identity word or a read key uses these options, never -match or (?i). Those take the CURRENT culture, where tr-TR folds
# `I` to the dotless i (U+0131), so an upper-case marker or key was skipped (a later Play's marker chosen, DELIVERY_KIND= / DISABLED= ignored).
# The same options as the PATH-refusal rule's $script:DvCultureFreeIgnoreCase (r9 B2 above), under a short name for these readers.
$script:DvCi = $script:DvCultureFreeIgnoreCase

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

function Get-DvLocationOpenIndex {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r21 (bench HFR-NATIVE-M16-1457-VENUE-EVIDENCE-MINI3-1 r3, receipt a4b1e24c-ce2a-4c8a-8633-4c0c43916f50:
    every cuda s1 leg's `playback_smoke.gpu_dualiso_recon_scale_summary ... session_reason="preview scale 1 (full resolution)" ...` lost its tail to
    LOCATION_UNREADABLE, so the due-wake reader refused it as DUE_WAKE_SESSION_BOUNDARY_UNREADABLE): the index of the ` (` that starts the Qt location,
    or -1 when the line has none. By CLASS, a ` (` inside a double-quoted value is never a location: the app writes free text with spaces and
    parentheses only as `<key>="<text>"` (MainWindow.cpp: session_reason, fallback_reason_last, gui_fps_status_text, screen, model, ...). A quoted value
    opens at `="` whose key ([A-Za-z0-9_]+) starts the line or follows a space, and closes at the FIRST `"` after it, which must be followed by a space
    or end the line; a `"` followed by anything else closes nothing, and then the opener is not a quoted value (its ` (` stays a location candidate, the
    reading before r21). An opener with no closer is not a quoted value either. The r10 conform group (Test-DvConformStatusGroupAt) is still stepped over
    once in its place. The scan is linear: each IndexOf result is reused until the scan passes it.
    #>
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Line)
    $n = $Line.Length
    $i = 0
    $p = -2
    $q = -2
    $conformSkipped = $false
    while ($i -lt $n) {
        if ($p -ne -1 -and $p -lt $i) { $p = $Line.IndexOf(' (', $i, [StringComparison]::Ordinal) }
        if ($p -lt 0) { return -1 }
        if ($q -ne -1 -and $q -lt $i) { $q = $Line.IndexOf('="', $i, [StringComparison]::Ordinal) }
        if ($q -ge 0 -and $q -lt $p) {
            # is $q a value opener: a non-empty ASCII key that starts the line or follows a space
            $k = $q - 1
            while ($k -ge 0 -and (($Line[$k] -ge [char]'A' -and $Line[$k] -le [char]'Z') -or ($Line[$k] -ge [char]'a' -and $Line[$k] -le [char]'z') -or ($Line[$k] -ge [char]'0' -and $Line[$k] -le [char]'9') -or $Line[$k] -ceq [char]'_')) { $k-- }
            if ($k -lt $q - 1 -and ($k -lt 0 -or $Line[$k] -ceq [char]' ')) {
                $c = $Line.IndexOf('"', $q + 2, [StringComparison]::Ordinal)
                # r21 adversary A1 on bdb3ec8c (its input: `playback_due_wake armed=1 note="diagnostic ( fired=1 ... delivery_kind=post end)"` was
                # HONOURED): a span that holds an `=` is never stepped over here, so its ` (` stays the location candidate and the record is refused as
                # before r21. r24 (opus key3 r23 blocker on b8f8e6e6): this rule alone does NOT keep quoted text from supplying a field (a later quoted
                # `source_advanced=600` after an =-free quoted ` (` reached the summary reader); that guarantee is ConvertTo-DvQuoteMaskedBody's, which
                # Get-DvLogRecordBodies applies to every record body before any reader sees it
                if ($c -ge 0 -and ($c -eq $n - 1 -or $Line[$c + 1] -ceq [char]' ') -and $Line.IndexOf('=', $q + 2, $c - $q - 2, [StringComparison]::Ordinal) -lt 0) { $i = $c + 1; continue }
            }
            $i = $q + 2
            continue
        }
        # r10 (ADV-A1, Dot 6550442583): the conform group the app writes INSIDE its own status field, stepped over once
        if (-not $conformSkipped -and (Test-DvConformStatusGroupAt -Line $Line -Index $p)) { $conformSkipped = $true; $i = $p + 2; continue }
        return $p
    }
    -1
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
    r21 (bench MINI3-1 r3 FINDING): the app also writes ` (` inside its own double-quoted values (session_reason="preview scale 1 (full resolution)" on
    playback_smoke.gpu_dualiso_recon_scale_summary), so the location starts at the first ` (` OUTSIDE a `<key>="<text>"` value (Get-DvLocationOpenIndex).
    #>
    param([AllowNull()][AllowEmptyString()][string]$Line)
    if ([string]::IsNullOrEmpty($Line)) { return $Line }
    $open = Get-DvLocationOpenIndex -Line $Line
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

function Get-DvLogRecordDiscardedText {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r27 (sol r26 B1 on af784b7b; opus key3 r25 DV-VISUAL-STATE-UNQUOTED-PAREN-VALUE-1): the RAW text
    Get-DvLogRecordBody cuts from one run-log line (from the location open index to the end of the line, the Qt location included), or '' when it cuts
    nothing. The cut stays at the FIRST ` (` outside a quoted value (r4/r5: a location path may hold ` (`, so a later ` (` is never trusted to be the
    location, and path text never reaches a body). But the app writes one UNQUOTED value with ` (` (GpuPreviewProcessing.cpp: reject reason
    `transform (upside down) enabled`), so with a readable `:<line>)` ending the cut can discard body fields with NO sentinel. A reader that proves a value
    from one key checks this text for that key: a mention here may be a discarded conflicting value, so it proves nothing (INV-1). It never forges: it
    only ever makes a reader refuse more.
    #>
    param([AllowNull()][AllowEmptyString()][string]$Line)
    if ([string]::IsNullOrEmpty($Line) -or $Line.IndexOf(' (', [StringComparison]::Ordinal) -lt 0) { return '' }
    $open = Get-DvLocationOpenIndex -Line $Line
    if ($open -lt 0) { return '' }
    $Line.Substring($open)
}

# HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r24 (opus key3 r23 blocker on b8f8e6e6, sol r23 blocker, r23 adversary A1): what an `=` inside a quoted
# span of a record body becomes (ConvertTo-DvQuoteMaskedBody). U+001A is no name character, no whitespace, no quote and no `=`, and the app never writes it
# (log_line_field::fieldValue maps every C0 control to '_'), so a masked `<key>=` is no `<key>=` for any reader, and a value stays one token.
$script:DvQuotedEqualsMask = [char]0x1A
$script:DvQuoteChars = [char[]]@([char]'"', [char]"'")
# VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1 r7 (r6 adversary A1 on 1ee5e122, its input: `playback_smoke.frame session=3 n=1<CR>gui_smoke.window_toggles ...`): the ONE
# rule for where a run-log record ends: CRLF, a bare CR or a bare LF (the job's Get-AttrCudaWindowToggleEvidence splits the same way). An LF-only split left a
# CR-joined record behind the body before it, so no reader saw it. Get-DvLogRecordBodies and every raw-line list kept index for index with it split by this.
$script:DvLogLineBreak = '\r\n|\r|\n'

function ConvertTo-DvQuoteMaskedBody {
    <#
    .SYNOPSIS
    HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r24 (opus key3 r23 INV-1 blocker): one record body with every `=` inside a quoted span replaced by
    $script:DvQuotedEqualsMask, so text inside a quote never supplies a `<key>=` field to any reader. A quoted span opens at ANY `"` or `'` of the body
    (whatever precedes it: `k="v"`, `render "free text"`, `x'y`) and closes at the next quote of the SAME kind; the other kind inside it is plain text;
    a span with no closer runs to the end of the body. Everything else is unchanged (the quotes themselves, the words inside a span, so a quoted
    `session` or `settings_store` is still a MENTION for the readers that refuse a second mention). It may refuse; it never forges, and it never
    HIDES a field: a lone apostrophe in an unquoted value (log_line_field::fieldValue maps no quote, so a launcher env value can hold one) masks every
    later `=`, and a key with a masked `=` is UNREADABLE, never absent (r24 adversary A1 on c1b10b4f): Get-DvSmokeSessionLineRead counts it as a
    duplicate and Get-DvSmokeSummaryFields refuses the whole summary record. Linear: IndexOf / IndexOfAny only, each position visited once.
    #>
    param([AllowNull()][AllowEmptyString()][string]$Body)
    if ([string]::IsNullOrEmpty($Body) -or $Body.IndexOf([char]'=') -lt 0) { return $Body }
    $i = $Body.IndexOfAny($script:DvQuoteChars)
    if ($i -lt 0) { return $Body }
    $chars = $Body.ToCharArray()
    # r24 (r24 adversary advisory A3 on c1b10b4f, its input: 4000 =-free quoted spans before one `=`): the next `=` is found once and reused until the
    # scan passes it, so an =-free span never rescans the rest of the body (linear, as the synopsis says)
    $j = -2
    while ($i -ge 0) {
        $close = $Body.IndexOf($Body[$i], $i + 1)
        $end = $(if ($close -lt 0) { $Body.Length } else { $close })
        if ($j -ne -1 -and $j -lt $i) { $j = $Body.IndexOf([char]'=', $i + 1) }
        while ($j -ge 0 -and $j -lt $end) { $chars[$j] = $script:DvQuotedEqualsMask; $j = $Body.IndexOf([char]'=', $j + 1) }
        if ($close -lt 0) { break }
        $i = $Body.IndexOfAny($script:DvQuoteChars, $close + 1)
    }
    [string]::new($chars)
}

function Get-DvLogRecordBodies {
    <#
    .SYNOPSIS
    PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r4 (r3 adversary A2 on #726): every line of a run log as its record body (Get-DvLogRecordBody), in order, so a
    reader matches record markers on the body only and a build path that holds `playback_smoke.<marker> session=<n>` never adds or selects a record.
    HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r24 (opus key3 r23 blocker): and quote-masked (ConvertTo-DvQuoteMaskedBody), AFTER the location cut
    (the cut reads the raw line; a quote in a location path never reaches the mask). This is the one place every DualVenueRunner field reader takes its
    records from (Get-DvSmokeSummaryFields, Get-DvSmokeSessionLineRead, the due-wake, look-assist, prep-route, recon-release, team-partition, worker-threads
    and look-label readers), so no `<key>=` inside a quoted span supplies a field on any path.
    #>
    param([AllowNull()][AllowEmptyString()][string]$LogText)
    if ([string]::IsNullOrEmpty($LogText)) { return , [string[]]@() }
    # r7 (r6 adversary A1): a bare CR ends a record too ($script:DvLogLineBreak)
    $lines = $LogText -split $script:DvLogLineBreak
    # r5: only a line that holds ` (` can carry a location; the rest are returned as they are without a call
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].IndexOf(' (', [StringComparison]::Ordinal) -ge 0) { $lines[$i] = Get-DvLogRecordBody -Line $lines[$i] } }
    # r24: only a body that holds a quote can hold a quoted span; the rest are returned as they are without a call
    for ($i = 0; $i -lt $lines.Count; $i++) { if ($lines[$i].IndexOfAny($script:DvQuoteChars) -ge 0) { $lines[$i] = ConvertTo-DvQuoteMaskedBody -Body $lines[$i] } }
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
    # r6 (sol r5 B1): a visual twin's rates are never read, whichever path its plan entry or receipt came through (the entry's leg id and the receipt's leg id both count)
    if ((Test-DvVisualTwinName (Get-DvProp $PlanEntry 'legId')) -or (Test-DvVisualTwinName (Get-DvProp $Receipt 'legId'))) {
        $row.note = 'a visual twin (capture-perturbed fps) is never a measured speed cell'; return [pscustomobject]$row
    }
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

function Test-DvMatrixRowMeasured {
    <#
    .SYNOPSIS
    M16-1457-VENUE-BASELINE-DRIFT-1 r7 (sol r6 INV-2): is this matrix row a MEASUREMENT? Only a PASS or FAIL row that carries a presented-fps value is (the reader leaves the rates null for a
    visual twin, a missing or swapped run log and every non-measurement outcome). The ONE predicate the table's `measured / ran` column and the summary header's `N measured` both use.
    #>
    param([Parameter(Mandatory)]$Row)
    ($Row.outcome -cin @('PASS', 'FAIL')) -and ($null -ne $Row.presentedFps)
}

function Get-DvMatrixMeasuredCount {
    # How many of these matrix rows are measurements (Test-DvMatrixRowMeasured): the summary header's count, equal to the sum of the table's per-cell numerators.
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Rows)
    @($Rows | Where-Object { Test-DvMatrixRowMeasured -Row $_ }).Count
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
        $meas = @($mine | Where-Object { Test-DvMatrixRowMeasured -Row $_ })
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
$script:LegTypes = @('speed', 'look', 'export')   # leg-spec.schema.json properties.legType.enum
# EXPORT-VENUE-LEG-1: the export block's fields and formats (leg-spec.schema.json properties.export), and the only generatorArgs key an export leg takes
$script:ExportFormats = @('cdng-lossless', 'prores')
$script:ExportFields = @('format', 'maxFrames', 'receipt', 'receiptSha256', 'dualIsoExpected')
$script:ExportExcludedKeys = @('look', 'displayMode', 'visualCapture', 'visualCaptureOptOutReason', 'acceptedEffectiveScale', 'presentTearingExpectedReason')
$script:ExportGeneratorArgs = @('cpuQuiescenceThresholdPercent')
# the generatorArgs members leg-spec.schema.json types boolean (prepConflatedMove: const true); a test pins this list to the schema
$script:GeneratorArgsBooleanSwitches = @('forceLookAssist', 'corePartitionTelemetry', 'disableLookAssist', 'presentTrace', 'firstPresentCapture', 'warmReuseMissDump', 'unpaced', 'disablePrepPointwiseFast', 'prepConflatedMove', 'utilSample', 'disableReconReleaseDeferred', 'dueWakeDeliveryPost', 'midPlayWindowToggles')
# EXPORT-VENUE-LEG-1: an export leg's typed shape (the schema's export block and its allOf exclusions), and `export` refused on any other leg. Called by
# Get-DvLegSpecShapeRefusal, so the leg and the lease batch's preflight refuse the same specs before a lease opens. Returns the refusal text, or $null.
function Get-DvExportShapeRefusal {
    param($Spec, [bool]$IsExportLeg)
    $exportProp = $Spec.PSObject.Properties['export']
    if (-not $IsExportLeg) {
        if ($null -ne $exportProp) { return 'export is only for an export leg (legType "export"); this leg is not one' }
        return $null
    }
    if ($null -eq $exportProp) { return 'an export leg needs export (one JSON object); it is absent' }
    $backends = Get-DvProp $Spec 'backends'
    if (-not ($backends -is [array] -and $backends.Count -eq 1 -and $backends[0] -is [string] -and $backends[0] -ceq 'cpu')) {
        return "an export leg runs backends [""cpu""] only (backends is $(ConvertTo-Json -InputObject $backends -Compress -Depth 3))"
    }
    $scale = Get-DvProp $Spec 'scaleFactor'
    if (-not (($scale -is [int] -or $scale -is [long]) -and $scale -eq 1)) { return "an export leg runs scaleFactor 1 (it is $(ConvertTo-Json -InputObject $scale -Compress -Depth 3))" }
    foreach ($key in $script:ExportExcludedKeys) {
        if ($null -ne $Spec.PSObject.Properties[$key]) { return "an export leg takes no $key (it plays nothing and captures no window)" }
    }
    $genArgs = Get-DvProp $Spec 'generatorArgs'
    if ($null -ne $genArgs) {
        foreach ($p in $genArgs.PSObject.Properties) {
            if ($p.Name -cnotin $script:ExportGeneratorArgs) { return "generatorArgs.$($p.Name) is not an export leg argument (an export leg takes only $($script:ExportGeneratorArgs -join ', '))" }
            # adversary r2 pass 2 A6: the threshold the generator casts to a double is a number in (0, 100] here, never an object / bool / string the cast would throw on later
            if ($p.Name -ceq 'cpuQuiescenceThresholdPercent') {
                $tv = $p.Value
                if (-not (($tv -is [int] -or $tv -is [long] -or $tv -is [double] -or $tv -is [decimal]) -and [double]::IsFinite([double]$tv) -and [double]$tv -gt 0 -and [double]$tv -le 100)) {
                    return 'generatorArgs.cpuQuiescenceThresholdPercent must be a number greater than 0 and at most 100'
                }
            }
        }
    }
    $ex = $exportProp.Value
    foreach ($p in $ex.PSObject.Properties) {
        if ($p.Name -cnotin $script:ExportFields) { return "export.$($p.Name) is not an export field ($($script:ExportFields -join ', '))" }
    }
    $format = Get-DvProp $ex 'format'
    if (-not ($format -is [string] -and $format -cin $script:ExportFormats)) { return "export.format must be one of $($script:ExportFormats -join ', ') (it is $(ConvertTo-Json -InputObject $format -Compress -Depth 3))" }
    $maxFrames = Get-DvProp $ex 'maxFrames'
    if (-not (($maxFrames -is [int] -or $maxFrames -is [long]) -and $maxFrames -ge 24 -and $maxFrames -le 100000)) {
        return "export.maxFrames must be a whole number from 24 to 100000 (it is $(ConvertTo-Json -InputObject $maxFrames -Compress -Depth 3))"
    }
    $receipt = Get-DvProp $ex 'receipt'
    if (-not ($receipt -is [string] -and $receipt -cmatch '^look-receipts/[a-z0-9-]+\.marxml(?![\s\S])')) { return "export.receipt must name a committed receipt under look-receipts/ (it is $(ConvertTo-Json -InputObject $receipt -Compress -Depth 3))" }
    $receiptSha = Get-DvProp $ex 'receiptSha256'
    if (-not ($receiptSha -is [string] -and $receiptSha -cmatch '^[0-9a-f]{64}(?![\s\S])')) { return 'export.receiptSha256 must be 64 lowercase hex digits' }
    $dualIso = Get-DvProp $ex 'dualIsoExpected'
    if ($dualIso -isnot [bool]) { return "export.dualIsoExpected must be JSON true or false (it is $(ConvertTo-Json -InputObject $dualIso -Compress -Depth 3))" }
    $null
}

function Get-DvLegSpecShapeRefusal {
    param($Spec)
    if ($Spec -isnot [System.Management.Automation.PSCustomObject]) { return 'the leg spec must be a single JSON object' }
    foreach ($container in 'look', 'generatorArgs', 'acceptedEffectiveScale', 'timeouts', 'criteria', 'export') {
        $present = $Spec.PSObject.Properties[$container]
        if ($null -ne $present -and $present.Value -isnot [System.Management.Automation.PSCustomObject]) {
            $shape = $(if ($null -eq $present.Value) { 'null' } elseif ($present.Value -is [array]) { 'an array' } else { "a $(Get-DvTypeName $present.Value)" })
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
        # VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1 r1 (adversary A4 on ead7bbdb): the mid-play window toggles are a WINDOWED leg's instrument (the app clears
        # them on any other leg and drives nothing). The schema's allOf rule says so for a committed spec; this is the live check, for the leg and every batch
        # preflight, so a spec that names the key on a fullscreen leg (or no display mode at all, the fullscreen default) never reaches a lease.
        $midPlayToggleProp = $genArgsProp.Value.PSObject.Properties['midPlayWindowToggles']
        if ($null -ne $midPlayToggleProp -and $midPlayToggleProp.Value -is [bool] -and $midPlayToggleProp.Value) {
            $displayModeProp = $Spec.PSObject.Properties['displayMode']
            if ($null -eq $displayModeProp -or $displayModeProp.Value -isnot [string] -or $displayModeProp.Value -cne 'windowed') {
                return 'generatorArgs.midPlayWindowToggles needs displayMode "windowed" (the app drives the toggles on a windowed leg only)'
            }
            # r12 (fable r11 note on d3b176e3, input: criteria.supplementary = {}): the criteria that fail an honest non-ok toggle proof are checked here too, by the
            # reconciler's own Get-DvWindowToggleCriteriaGap and with its typed reason, so an unbound leg is refused before a lease opens, not only after its run
            $toggleCriteriaGap = Get-DvWindowToggleCriteriaGap -Spec $Spec
            if ($null -ne $toggleCriteriaGap) { return "WINDOW_TOGGLES_CRITERIA_UNBOUND: the leg names midPlayWindowToggles but $toggleCriteriaGap" }
        }
        # CPU-S1-FS-SPIKE-TEAM-PARTITION-1 (adversary A3 and A4 on 84d66a4b): a PRESENT team-partition key is a whole number from 1 to 16 -- a JSON int, the
        # convention look.contactSheetFrames uses (a JSON number with a fraction, 12.0 included, parses to a double and is no whole number here), never null --
        # and the two keys come together. Refused here, before a lease opens, for the leg and every batch preflight alike; absent stays "not asked for".
        $teamProps = @($genArgsProp.Value.PSObject.Properties['reconOmpThreads'], $genArgsProp.Value.PSObject.Properties['prepOmpThreads'])
        foreach ($teamProp in $teamProps) {
            if ($null -ne $teamProp -and -not (($teamProp.Value -is [int] -or $teamProp.Value -is [long]) -and $teamProp.Value -ge 1 -and $teamProp.Value -le 16)) {
                return "generatorArgs.$($teamProp.Name) must be a whole number from 1 to 16 (it is $(ConvertTo-Json -InputObject $teamProp.Value -Compress -Depth 3))"
            }
        }
        if (($null -eq $teamProps[0]) -ne ($null -eq $teamProps[1])) {
            return 'generatorArgs.reconOmpThreads and generatorArgs.prepOmpThreads are both set or neither (the team partition names both teams)'
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
    $exportRefusal = Get-DvExportShapeRefusal -Spec $Spec -IsExportLeg ($null -ne $legTypeProp -and $legTypeProp.Value -ceq 'export')
    if ($null -ne $exportRefusal) { return $exportRefusal }
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
    # EXPORT-VENUE-LEG-1: an export leg plays nothing, so it never captures (and the shape refusal refuses visualCapture on it)
    $applies = ([string](Get-DvProp $Spec 'legType') -cnotin @('look', 'export'))
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

# --- EXPORT-VENUE-LEG-1: the export leg's evidence, outcome and receipt check ---------------------------------------------------------------------------------
# The CDNG stage profiler (src/dng/dng.c, MLVAPP_EXPORT_STAGE_PROFILER) writes one row per frame. The PRODUCER side is the denominator producer_frame_ms; the
# writer side runs on the async writer and is reported apart. llrawproc_ms is the sum of its llrawproc_* children, so it is reported once, not as a row.
$script:ExportCdngProducerStages = @('raw_read', 'raw_decode', 'raw_unpack', 'llrawproc_dark_frame', 'llrawproc_vertical_stripes', 'llrawproc_focus_pixels', 'llrawproc_bad_pixels',
    'llrawproc_pattern_noise', 'llrawproc_pre_dualiso_fix', 'llrawproc_dual_iso', 'llrawproc_chroma_smooth', 'llrawproc_shared_lock', 'llrawproc_dualiso_refine_lock',
    'llrawproc_publish_lock', 'llrawproc_other', 'dng_header', 'dng_pack', 'dng_compress_encode', 'payload_clone')
$script:ExportCdngWriterStages = @('disk_write', 'writer_queue_wait', 'writer_completion_lag')
# The rendered (ProRes) path's MLVAPP_STAGE_TIMING stages (src/mlv/video_mlv.c). raw_uint16 (with its disk_read and decompress) and llrawproc are timed inside the
# raw fetch getMlvRawFrameDebayered makes, so they NEST inside debayered_frame; debayered_frame and processing are the disjoint children of processed16_total.
# The residual (loop wall per frame - processed16_total) is the pipe write to ffmpeg and its back-pressure. Drain and verify are not timed per stage.
$script:ExportRenderedStages = @('raw_uint16', 'raw_uint16_disk_read', 'raw_uint16_decompress', 'llrawproc', 'debayered_frame', 'processing', 'processed16_total')
$script:ExportRenderedTopLevel = @('debayered_frame', 'processing')
$script:ExportRenderedNesting = 'raw_uint16 (and its disk_read, decompress) and llrawproc nest inside debayered_frame; debayered_frame and processing are disjoint children of processed16_total, the share denominator'
$script:ExportSuccessor = 'EXPORT-RENDER-PIPE-TIMING-1'
$script:ExportEvidenceFiles = @('summary.json', 'batch.log', 'export-profile.json', 'stage-timing.log', 'output-manifest.json')

# r8 fix 2 (adversary r8 A1): the PARENT/CHILD half of the stage schema. A published share is of work the stage's parent contains: a child whose mean exceeds its parent's is
# no measurement of this run (raw_uint16 at 900 ms inside a 40 ms debayered_frame read a 16x share and PASSed). Pairs are child -> parent, over the stage rows' means, with the
# slack two clocks and rounding can honestly show (5 % + 1 ms); a pair whose parent or child is wholly absent is skipped (a stage the app did not run is permitted).
# (EXPORT-CDNG-TOTAL-CONTAINMENT-1, adversary A2: a figure AT the limit is inside it; the binary product 40.00046 * 1.05 + 1 is 43.000482999999996, a hair under the decimal 43.000483, which is why
# the comparison is in [decimal] below; the one epsilon, $script:ExportStageContainEpsilonD, is documented with it)
# (EXPORT-CDNG-TOTAL-CONTAINMENT-1 r2 fix 1, adversary pass A2 + A3 on 62dff0fa: every containment comparison is made in [decimal], so (A2) a figure exactly at the decimal limit is inside it at ANY
# magnitude (a binary double product is several ulps past the decimal at 2e7 ms, and a double mean of repeated decimals is a hair off), and (A3) a mean a sub-precision hair past the limit is
# accepted. r3 B1: that hair is now ONE documented epsilon on the unrounded comparison (below), not a rounding of both sides, which hid a full emitted increment.)
$script:ExportStageContainToleranceD = [decimal]1.05
$script:ExportStageContainSlackD = [decimal]1.0
# (EXPORT-CDNG-TOTAL-CONTAINMENT-1 r3 B1, sol r2 INV-4 on f755f502: rounding BOTH sides to 6 decimals before the compare let a mean over the limit by one FULL emitted increment pass, because
# the limit and the mean rounded to the same figure (limit 101.8984375, mean 101.8984385, both 101.898438). The comparison is now on the UNROUNDED decimals against the exact limit plus ONE
# epsilon, 4e-7 ms = four tenths of an emitted increment, the same on every side of every gate. The epsilon is a POLICY choice, not a correction of arithmetic: the means and the limits are exact
# decimals and are not off at all. It says an overrun of less than four tenths of an emitted increment is within what the 6-decimal figures can show (the r2 controls: 3e-7 over passes), and an
# overrun of half an increment or more is a real one (5e-7 and 1e-6 over fail). A figure exactly AT the limit is inside it at any magnitude, as decimal arithmetic keeps it exact. The wall
# gates' limit is built from the DONE elapsed as the log wrote it (r4 B1), not from the 6-decimal published wallSeconds.)
$script:ExportStageContainEpsilonD = [decimal]0.0000004

function Test-DvExportBeyondTolerance {
    # $true when $Child is past $Parent * 5 % + 1 ms + the epsilon above: unrounded, in decimal arithmetic (never both sides rounded to the emitted precision first).
    param($Child, $Parent)
    $limit = [decimal]$Parent * $script:ExportStageContainToleranceD + $script:ExportStageContainSlackD + $script:ExportStageContainEpsilonD
    [decimal]$Child -gt $limit
}

function Get-DvTypeName {
    # The .NET type name of a value read from JSON, for a reason that names it. A JSON object whose key is `GetType` (PSCustomObject property) makes `$Value.GetType()` throw "does not contain a method",
    # so the call is guarded: whatever the value, naming its type never throws.
    param($Value)
    if ($null -eq $Value) { return 'null' }
    try { $Value.GetType().Name } catch { 'JSON object' }
}
# (adversary r11 A1: raw_uint16 is a stage the app may not publish, which skipped both of its child pairs; the transitive pairs hold disk_read and decompress inside debayered_frame whether or not
# raw_uint16 is present, as the stage tree above nests them)
$script:ExportRenderedContainment = @(@('raw_uint16', 'debayered_frame'), @('raw_uint16_disk_read', 'raw_uint16'), @('raw_uint16_decompress', 'raw_uint16'), @('llrawproc', 'debayered_frame'),
    @('raw_uint16_disk_read', 'debayered_frame'), @('raw_uint16_decompress', 'debayered_frame'), @('debayered_frame', 'processed16_total'), @('processing', 'processed16_total'),
    # (adversary r13 A1 sibling: the same transitive pairs to the denominator, for a run that does not publish debayered_frame)
    @('raw_uint16', 'processed16_total'), @('llrawproc', 'processed16_total'), @('raw_uint16_disk_read', 'processed16_total'), @('raw_uint16_decompress', 'processed16_total'))
$script:ExportCdngLlrawprocChildren = @('llrawproc_dark_frame', 'llrawproc_vertical_stripes', 'llrawproc_focus_pixels', 'llrawproc_bad_pixels', 'llrawproc_pattern_noise', 'llrawproc_pre_dualiso_fix',
    'llrawproc_dual_iso', 'llrawproc_chroma_smooth', 'llrawproc_shared_lock', 'llrawproc_dualiso_refine_lock', 'llrawproc_publish_lock', 'llrawproc_other')

# r17 (EXPORT-VENUE-LEG-1-OPEN-16-1 = adversary pass16 A1; CDNG-STAGE-WINDOW-TABLE-1): ONE table of every CDNG producer and llrawproc timer and the source window(s) its value accrues in. The
# disjoint groups held together inside each parent are DERIVED from it (Get-DvExportCdngDisjointGroups), never hand-listed: five rounds (r11, r13, r13-2, r15, r16) each found one more disjoint
# member a hand list had missed (r16's lists left shared_lock out: dual_iso 80 + shared_lock 80 inside a 100 ms llrawproc read PASS).
# A window is <file>:<first>-<last> (source lines of the timer's start .. its accrual; a callee's lines count when its only call sites are inside the window) or <file>#<token> (a bracket
# named once). Two rows with ONE parent overlap when a window of each meets (same file, the line ranges intersect, or the same token). A row with remainderOf is its parent less those
# siblings (dng.c:579-604 sets llrawproc_other = total - the seven timed children), so it overlaps exactly the siblings that accrue on a line outside every remainderOf window. A row
# overlaps its ancestors; rows under different parents compare the ancestors that share a parent. Branch-exclusive windows on one line range are listed apart (the union over branches).
$script:ExportCdngStageWindowTable = @(
    # producer_frame (dng.c:666, :3619) holds these in sequence; each is one export_profile_stage_begin/end bracket and none is inside another (the r11 partition, dng.c:2567-2923)
    [ordered]@{ name = 'raw_read'; parent = 'producer_frame'; windows = @('dng.c#raw_read') }                         # dng.c:2661-2699, :2754-2858
    [ordered]@{ name = 'raw_decode'; parent = 'producer_frame'; windows = @('dng.c#raw_decode') }                     # dng.c:2608-2616, :2701-2708, :2789-2797
    [ordered]@{ name = 'raw_unpack'; parent = 'producer_frame'; windows = @('dng.c#raw_unpack') }                     # dng.c:2869-2875
    [ordered]@{ name = 'llrawproc'; parent = 'producer_frame'; windows = @('dng.c#llrawproc') }                       # dng.c:2619-2622, :2719-2721, :2800-2802, :2878-2880 (the llrawproc TOTAL)
    [ordered]@{ name = 'dng_header'; parent = 'producer_frame'; windows = @('dng.c#dng_header') }                     # dng.c:2921-2923
    [ordered]@{ name = 'dng_pack'; parent = 'producer_frame'; windows = @('dng.c#dng_pack') }                         # dng.c:2643-2650, :2741-2748, :2820-2836, :2863-2865, :2898-2914
    [ordered]@{ name = 'dng_compress_encode'; parent = 'producer_frame'; windows = @('dng.c#dng_compress_encode') }   # dng.c:2426-2440 (dng_compress_image_profiled, called at :2567-2578 / :3138-3149)
    [ordered]@{ name = 'payload_clone'; parent = 'producer_frame'; windows = @('dng.c#payload_clone') }               # dng.c:3726-3728, :3781-3783
    # llrawproc's children (src/mlv/llrawproc/llrawproc.c). EXPORT-CDNG-STAGE-CONTAINMENT-1: ONLY the code path CDNG export runs. dng.c:103 calls applyLLRawProcObject, which is
    # applyLLRawProcObjectWorker(..., NULL, 0) (:6411-6413; body :5008-6331, every timer reset at :5027-5039). The scaled path llrawproc_apply_with_dims_internal (:6598-7076) is
    # playback-only, so none of its lines is a window here; and the literal 0 is stop_before_dual_iso, so the block at :5461-5472 never runs on export. The test Guard
    # (CdngStageContainmentGateTests) re-derives every window below from the C sources by call-site reachability from dng.c's call. The seven timed children (dng.c:571-577):
    [ordered]@{ name = 'llrawproc_dark_frame'; parent = 'llrawproc'; windows = @('llrawproc.c:5168-5188', 'llrawproc.c:5385-5392') }
    [ordered]@{ name = 'llrawproc_vertical_stripes'; parent = 'llrawproc'; windows = @('llrawproc.c:5290-5300', 'llrawproc.c:5397-5404') }
    [ordered]@{ name = 'llrawproc_focus_pixels'; parent = 'llrawproc'; windows = @('llrawproc.c:5321-5341', 'llrawproc.c:5409-5420', 'llrawproc.c:6140-6151') }
    # (bad_pixels calls llrawproc_reset_force_bad_pixel_search, :4688-4696, only from inside its windows, :5438 and :6174)
    [ordered]@{ name = 'llrawproc_bad_pixels'; parent = 'llrawproc'; windows = @('llrawproc.c:5346-5372', 'llrawproc.c:5425-5440', 'llrawproc.c:6161-6176', 'llrawproc.c:4688-4696') }
    [ordered]@{ name = 'llrawproc_pattern_noise'; parent = 'llrawproc'; windows = @('llrawproc.c:5445-5458') }
    # (dual_iso: :5560-5586, :5591-6019, :6210-6219)
    [ordered]@{ name = 'llrawproc_dual_iso'; parent = 'llrawproc'; windows = @('llrawproc.c:5560-5586', 'llrawproc.c:5591-6019', 'llrawproc.c:6210-6219') }
    [ordered]@{ name = 'llrawproc_chroma_smooth'; parent = 'llrawproc'; windows = @('llrawproc.c:6258-6271') }
    # The lock times. shared_lock: the first lock :5142-5288 (or :5142-5376 when the stripes block :5285-5317 does not run; both lie inside the two windows listed), the stripes publish lock
    # :5302-5312 and the relock :5314-5376, the reset lock :4688-4696 (:4696), AND the refine and publish lock times added to it (:6131, :6293).
    # (adversary pass17 A1: because shared_lock ACCUMULATES the refine and publish lock times, it CONTAINS them: each, and their disjoint sum, is inside shared_lock; contains = those rows)
    [ordered]@{ name = 'llrawproc_shared_lock'; parent = 'llrawproc'; windows = @('llrawproc.c:5142-5288', 'llrawproc.c:5302-5376', 'llrawproc.c:4688-4696',
        'llrawproc.c:6063-6130', 'llrawproc.c:6288-6292'); contains = @('llrawproc_dualiso_refine_lock', 'llrawproc_publish_lock') }
    [ordered]@{ name = 'llrawproc_dualiso_refine_lock'; parent = 'llrawproc'; windows = @('llrawproc.c:6063-6130') }
    [ordered]@{ name = 'llrawproc_publish_lock'; parent = 'llrawproc'; windows = @('llrawproc.c:6288-6292') }
    # pre_dualiso_fix: the worker resets it (:5036) and writes it only inside `if (stop_before_dual_iso)` (:5461-5463), which export's literal 0 never enters, so on export it accrues
    # NOWHERE and has no window (r17 key3 B1: the row carried the scaled call :6606-7076, which met dual_iso's scaled windows and kept it out of every group). A row with no window meets
    # no other row, so it joins every disjoint group: any value export publishes for it is work outside every timed window of llrawproc, and is held inside llrawproc with the rest.
    [ordered]@{ name = 'llrawproc_pre_dualiso_fix'; parent = 'llrawproc'; windows = @() }
    # llrawproc_other is the total less the seven timed children (dng.c:579-604), so it retains pre_dualiso_fix and the lock times
    [ordered]@{ name = 'llrawproc_other'; parent = 'llrawproc'; windows = @(); remainderOf = @('llrawproc_dark_frame', 'llrawproc_vertical_stripes', 'llrawproc_focus_pixels', 'llrawproc_bad_pixels',
        'llrawproc_pattern_noise', 'llrawproc_dual_iso', 'llrawproc_chroma_smooth') }
)

function Get-DvExportWindowsMeet {
    # $true when any window in $A meets any in $B: the same <file>#<token>, or the same file with intersecting line ranges.
    param([string[]]$A, [string[]]$B)
    foreach ($wa in $A) {   # (not $a / $b: PowerShell names are case-blind, so they would overwrite $A / $B)
        foreach ($wb in $B) {
            if ($wa -ceq $wb) { return $true }
            $ma = [regex]::Match($wa, '^([^:#]+):(\d+)-(\d+)$'); $mb = [regex]::Match($wb, '^([^:#]+):(\d+)-(\d+)$')
            if ($ma.Success -and $mb.Success -and $ma.Groups[1].Value -ceq $mb.Groups[1].Value -and [int]$ma.Groups[2].Value -le [int]$mb.Groups[3].Value -and [int]$mb.Groups[2].Value -le [int]$ma.Groups[3].Value) { return $true }
        }
    }
    $false
}

function Get-DvExportCdngDisjointGroups {
    # Every MAXIMAL pairwise-disjoint set (two or more rows) of each parent's descendants in $Table, as (members, parent) groups for Get-DvExportContainmentProblem's -SumGroups.
    param($Table = $script:ExportCdngStageWindowTable)
    $rows = @{}; foreach ($r in $Table) { $rows[[string]$r.name] = $r }
    $names = [System.Collections.Generic.List[string]]::new(); foreach ($row in $Table) { $names.Add([string]$row.name) }
    $chain = @{}   # name -> the name and its ancestors, nearest first
    foreach ($n in $names) {
        $c = [System.Collections.Generic.List[string]]::new(); $c.Add($n)
        $up = [string]$rows[$n].parent
        while ($up) { $c.Add($up); $up = $(if ($rows.ContainsKey($up)) { [string]$rows[$up].parent } else { '' }) }
        $chain[$n] = $c
    }
    $overlap = @{}
    foreach ($nx in $names) {
        foreach ($ny in $names) {
            if ($nx -ceq $ny) { continue }
            if ($chain[$nx].Contains($ny) -or $chain[$ny].Contains($nx)) { $overlap["$nx|$ny"] = $true; continue }
            $hit = $null   # lift each to the ancestor that shares a parent with the other's
            foreach ($u in $chain[$nx]) {
                foreach ($v in $chain[$ny]) {
                    if ($null -eq $hit -and $rows.ContainsKey($u) -and $rows.ContainsKey($v) -and [string]$rows[$u].parent -ceq [string]$rows[$v].parent) { $hit = @($u, $v) }
                }
            }
            $overlap["$nx|$ny"] = $(if ($null -eq $hit) { $true } else { Get-DvExportSiblingsMeet -Rows $rows -First $hit[0] -Second $hit[1] })
        }
    }
    $groups = [System.Collections.Generic.List[object]]::new()
    $parents = [System.Collections.Generic.List[string]]::new()
    foreach ($n in $names) { $p = [string]$rows[$n].parent; if ($p -and -not $parents.Contains($p)) { $parents.Add($p) } }
    # (pass17 A1, pass18 A1: a row that CONTAINS sibling timers (Get-DvExportCdngContains) is a parent of those rows too, for the groups; the pairs are Get-DvExportCdngContainedPairs)
    $contains = Get-DvExportCdngContains -Table $Table
    foreach ($n in $names) { if ($contains.ContainsKey($n) -and -not $parents.Contains($n)) { $parents.Add($n) } }
    foreach ($parent in $parents) {
        $desc = [string[]]@($(if ($contains.ContainsKey($parent)) { @($contains[$parent]) }
                              else { @(foreach ($n in $names) { if ($chain[$n].IndexOf($parent) -gt 0) { $n } }) }))
        $nbr = @{}
        foreach ($v in $desc) { $s = [System.Collections.Generic.HashSet[string]]::new(); foreach ($w in $desc) { if ($w -cne $v -and -not $overlap["$v|$w"]) { [void]$s.Add($w) } }; $nbr[$v] = $s }
        $cliques = [System.Collections.Generic.List[object]]::new()
        Add-DvExportMaximalCliques -Neighbours $nbr -Cliques $cliques -R ([string[]]@()) -P $desc -Excluded ([string[]]@())
        foreach ($clique in $cliques) {
            if (@($clique).Count -lt 2) { continue }
            $ordered = [string[]]@(foreach ($n in $names) { if (@($clique) -ccontains $n) { $n } })   # (table order)
            $groups.Add(@(, $ordered) + @($parent))
        }
    }
    , $groups.ToArray()
}

function Add-DvExportMaximalCliques {
    # Bron-Kerbosch with a pivot: every maximal set of pairwise-adjacent names (here: pairwise DISJOINT stages) is added to $Cliques. (Without the pivot one 16-member set costs 2^16 calls.)
    param($Neighbours, $Cliques, [string[]]$R, [string[]]$P, [string[]]$Excluded)
    if (@($P).Count -eq 0 -and @($Excluded).Count -eq 0) { $Cliques.Add([string[]]@($R)); return }
    $pending = [System.Collections.Generic.List[string]]::new(); foreach ($v in @($P)) { $pending.Add($v) }
    $done = [System.Collections.Generic.List[string]]::new(); foreach ($v in @($Excluded)) { $done.Add($v) }
    $pivot = $null; $best = -1
    foreach ($u in @(@($P) + @($Excluded))) { $k = 0; foreach ($w in @($P)) { if ($Neighbours[$u].Contains($w)) { $k++ } }; if ($k -gt $best) { $best = $k; $pivot = $u } }
    foreach ($v in @($P)) {
        if ($null -ne $pivot -and $Neighbours[$pivot].Contains($v)) { continue }
        $nb = $Neighbours[$v]
        Add-DvExportMaximalCliques -Neighbours $Neighbours -Cliques $Cliques -R ([string[]](@($R) + $v)) -P ([string[]]@(foreach ($w in $pending) { if ($nb.Contains($w)) { $w } })) `
            -Excluded ([string[]]@(foreach ($w in $done) { if ($nb.Contains($w)) { $w } }))
        [void]$pending.Remove($v); $done.Add($v)
    }
}

function Get-DvExportSiblingsMeet {
    # $true when two rows with one parent overlap (see the table's header): a remainderOf row meets a sibling that accrues on a line outside every remainderOf window.
    param($Rows, [string]$First, [string]$Second)
    foreach ($pair in @(@($First, $Second), @($Second, $First))) {
        $rem = $Rows[$pair[0]]; $sib = $Rows[$pair[1]]
        if (-not ($rem.Contains('remainderOf') -and @($rem.remainderOf).Count)) { continue }
        if (@($rem.remainderOf) -ccontains [string]$sib.name) { return $false }
        $covered = [string[]]@(foreach ($s in $rem.remainderOf) { @($Rows[[string]$s].windows) })
        foreach ($w in @($sib.windows)) {
            $m = [regex]::Match([string]$w, '^([^:#]+):(\d+)-(\d+)$')
            if (-not $m.Success) { if (-not ($covered -ccontains [string]$w)) { return $true }; continue }
            for ($line = [int]$m.Groups[2].Value; $line -le [int]$m.Groups[3].Value; $line++) {
                if (-not (Get-DvExportWindowsMeet -A @("$($m.Groups[1].Value):$line-$line") -B $covered)) { return $true }
            }
        }
        return $false
    }
    Get-DvExportWindowsMeet -A @($Rows[$First].windows) -B @($Rows[$Second].windows)
}
function Get-DvExportCdngContains {
    # name -> the sibling rows it CONTAINS: the explicit contains column (a timer that accumulates others, pass17 A1), and, for a remainderOf row, every sibling that accrues on no line of a
    # remainderOf window (pass18 A1: llrawproc_other = total - the seven timed children, so the refine and publish lock times, which lie outside every child window, are inside it).
    param($Table = $script:ExportCdngStageWindowTable)
    $out = @{}
    foreach ($row in $Table) {
        $set = [System.Collections.Generic.List[string]]::new()
        if ($row.Contains('contains')) { foreach ($m in @($row.contains)) { if (-not $set.Contains([string]$m)) { $set.Add([string]$m) } } }
        if ($row.Contains('remainderOf') -and @($row.remainderOf).Count) {
            $covered = [string[]]@(foreach ($s in $row.remainderOf) { foreach ($r2 in $Table) { if ([string]$r2.name -ceq [string]$s) { @($r2.windows) } } })
            foreach ($sib in $Table) {
                if ([string]$sib.parent -cne [string]$row.parent -or [string]$sib.name -ceq [string]$row.name -or @($row.remainderOf) -ccontains [string]$sib.name -or @($sib.windows).Count -eq 0) { continue }
                if (-not (Get-DvExportWindowsMeet -A @($sib.windows) -B $covered) -and -not $set.Contains([string]$sib.name)) { $set.Add([string]$sib.name) }
            }
        }
        if ($set.Count) { $out[[string]$row.name] = [string[]]$set.ToArray() }
    }
    $out
}

function Get-DvExportCdngContainedPairs {
    # (adversary pass17 A1, pass18 A1) child -> container pairs for each row another row contains: refine 10 + publish 10 beside a 0 ms shared_lock, or a 0 ms llrawproc_other, read PASS.
    param($Table = $script:ExportCdngStageWindowTable)
    $contains = Get-DvExportCdngContains -Table $Table
    , [object[]]@(foreach ($row in $Table) { $n = [string]$row.name; if ($contains.ContainsKey($n)) { foreach ($m in $contains[$n]) { , [string[]]@([string]$m, $n) } } })
}
$script:ExportCdngSumGroups = Get-DvExportCdngDisjointGroups   # (after every function it calls is defined)
$script:ExportCdngContainedPairs = Get-DvExportCdngContainedPairs

# r8 fix 3 (adversary pass 10 A2): DISJOINT siblings together are inside their parent too: each below its parent is no proof that the pair is (10 + 10 ms inside a 10 ms parent).
# Each group is (the disjoint children, the parent); the rendered tree's groups below.
$script:ExportRenderedSumGroups = @(@(@('debayered_frame', 'processing'), 'processed16_total'), @(@('raw_uint16', 'llrawproc'), 'debayered_frame'), @(@('raw_uint16_disk_read', 'raw_uint16_decompress'), 'raw_uint16'),
    @(@('raw_uint16_disk_read', 'raw_uint16_decompress'), 'debayered_frame'),
    # (adversary r13 A1: the groups above need an intermediate total to be published (raw_uint16 beside llrawproc, debayered_frame beside processing); a run that omits it left llrawproc + disk_read +
    # decompress (65 ms) inside a 40 ms debayered_frame unjudged. These hold the same disjoint leaves inside the nearest ancestor that IS published, whichever totals are absent: raw_uint16 is
    # disk_read + decompress + its own remainder, so a leaf set uses raw_uint16 OR its two children, never both.)
    @(@('raw_uint16_disk_read', 'raw_uint16_decompress', 'llrawproc'), 'debayered_frame'),
    @(@('raw_uint16', 'llrawproc', 'processing'), 'processed16_total'), @(@('raw_uint16_disk_read', 'raw_uint16_decompress', 'llrawproc', 'processing'), 'processed16_total'))

function Get-DvExportContainmentProblem {
    # $null, or the EXPORT_STAGE_CONTAINMENT_MISMATCH problem naming the first child (and the count) whose mean exceeds its parent's, or the first disjoint group whose summed means do.
    # $Means: stage name -> mean ms ($null = absent).
    param($Means, $Pairs, $SumGroups = @())
    foreach ($group in $SumGroups) {
        $present = @($group[0] | Where-Object { $null -ne $Means[$_] })
        $parent = $Means[[string]$group[1]]
        if ($present.Count -lt 2 -or $null -eq $parent) { continue }
        $sum = [decimal]0; foreach ($n in $present) { $sum += [decimal]$Means[$n] }
        if (Test-DvExportBeyondTolerance $sum $parent) {
            return "EXPORT_STAGE_CONTAINMENT_MISMATCH: the disjoint stages $($present -join ' + ') total $([math]::Round($sum, 6)) ms (mean) inside $($group[1]) mean $(Get-DvExportRound $parent) ms, so their shares are of work this run's frames do not hold"
        }
    }
    $bad = [System.Collections.Generic.List[string[]]]::new()   # (a list of pairs: a pipeline would unroll a one-pair result into its two names)
    foreach ($pair in $Pairs) {
        $c = $Means[$pair[0]]; $p = $Means[$pair[1]]
        if ($null -ne $c -and $null -ne $p -and (Test-DvExportBeyondTolerance $c $p)) { $bad.Add([string[]]@($pair[0], $pair[1])) }
    }
    if ($bad.Count -eq 0) { return $null }
    "EXPORT_STAGE_CONTAINMENT_MISMATCH: $($bad.Count) stage(s) are timed longer than the stage that contains them (first: $($bad[0][0]) mean $(Get-DvExportRound $Means[$bad[0][0]]) ms inside $($bad[0][1]) mean $(Get-DvExportRound $Means[$bad[0][1]]) ms), so their shares are of work this run's frames do not hold"
}

# r11 (sol r9 B2 (a), fable r9 B2; EXPORT-VENUE-LEG-1): the OUTERMOST containment: the measured per-frame work is inside the batch's own DONE elapsed. A DONE elapsed shorter than the
# work the stage log / profile measured (elapsed=0.0 beside 480 frames of 56 ms) is two records of one run that disagree: it read PASS with a negative untimedPerFrameMs and an inflated
# exportFps. The same tolerance as every other containment (5 % + 1 ms per frame: elapsed is logged to one decimal). $StageMeanMs is the per-frame work the format's denominator measures
# (ProRes processed16_total, CDNG producer_frame); null when the wall, the frame count or the stage is not readable (those are typed elsewhere), so this adds no second reason for them.
function Get-DvExportWallContainmentProblem {
    param($WallSeconds, $Frames, [string]$StageName, $StageMeanMs)
    if ($null -eq $WallSeconds -or $null -eq $Frames -or [int64]$Frames -le 0 -or $null -eq $StageMeanMs) { return $null }
    # $WallSeconds is the DONE elapsed exactly as the log wrote it ([decimal], up to 9 decimals), never the 6-decimal published wallSeconds (r4 B1, opus r3): the limit is exact, in decimal like the means (sol r1 B3), compared unrounded (Test-DvExportBeyondTolerance)
    $wallPerFrame = [decimal]$WallSeconds * [decimal]1000 / [decimal]$Frames
    Get-DvExportContainmentProblem -Means @{ batch_wall_per_frame = $wallPerFrame; $StageName = $StageMeanMs } -Pairs @(, @($StageName, 'batch_wall_per_frame'))
}

# EXPORT-CDNG-TOTAL-CONTAINMENT-1 (Dot I5-EXPORT-CDNG-TOTAL-CONTAINMENT on #838, ask 6967301971): the llrawproc TOTAL (llrawproc_ms, the producer's bracket around the llrawproc call: dng.c:2619-2622)
# is published as its own figure (stages.llrawprocTotal and its share), so it is held to the two things it is: inside producer_frame_ms, and the sum of the llrawproc children it brackets.
# dng.c:571-604 writes llrawproc_other_ms = max(0, llrawproc_total_ms - the seven timed children), so the seven timed children plus llrawproc_other_ms are the worker's own total, and that
# worker is the call the bracket wraps (applyLLRawProcObject -> applyLLRawProcObjectWorker, llrawproc.c:5008, :6329); the lock times are not in the sum (llrawproc_other retains them).
# Per frame, only what holds by NESTING is judged, so one scheduler hiccup cannot void a leg: the bracket is inside producer_frame_ms (the pair the mean check had, now on every frame: a total of
# 130 ms beside 63 ms of producer work is a mean of 43 ms inside a mean 63 ms and was never seen), and the children are inside the bracket. The inverse (a bracket much longer than its children) is
# bracket entry/exit time the worker's own clock does not see, so it is judged as a SYSTEMATIC gap: more than a tenth of the frames (adversary A1: a mean let ONE 2000 ms scheduler pause in the
# bracket and the producer void a 480-frame run whose children were exact; a pause is a few frames, a forged or mis-wired total is most of them). Same tolerance as every pair (5 % + 1 ms).
# The worker's own total (llrawproc_total_ms, dng.c:588, adversary A4) is the exact identity, on the worker's one clock, so when a frame carries it, it agrees with the children either way, per frame.
# A frame that does not carry the total is not judged against it: llrawproc_ms is an optional total and a stage the app did not run is permitted absent. A frame that lacks some of the eight children is still
# held to what it DOES carry (r2, sol r1 B2): the present children are a lower bound on the sum, so they must still fit inside llrawproc_ms and inside the worker's llrawproc_total_ms; only the
# "total longer than its children" direction needs all eight.
$script:ExportCdngLlrawprocTimedChildren = @('llrawproc_dark_frame', 'llrawproc_vertical_stripes', 'llrawproc_focus_pixels', 'llrawproc_bad_pixels', 'llrawproc_pattern_noise', 'llrawproc_dual_iso', 'llrawproc_chroma_smooth')

function Get-DvExportCdngTotalFrameProblems {
    # $Frames: the successful profile frame records. Returns the typed problems (zero to four): EXPORT_STAGE_CONTAINMENT_MISMATCH (the total past producer_frame_ms on some frame, or the worker's llrawproc_total_ms past the bracket that wraps it) and
    # EXPORT_STAGE_SUM_MISMATCH (the seven timed children plus llrawproc_other_ms past llrawproc_ms on some frame, llrawproc_ms past that sum on more than a tenth of the frames, or the worker's
    # llrawproc_total_ms apart from that sum on some frame).
    param($Frames)
    $past = [System.Collections.Generic.List[string]]::new(); $beyond = [System.Collections.Generic.List[string]]::new(); $longer = [System.Collections.Generic.List[string]]::new()
    $worker = [System.Collections.Generic.List[string]]::new(); $workerOut = [System.Collections.Generic.List[string]]::new()
    $judged = 0
    # (every comparison is Test-DvExportBeyondTolerance: unrounded decimal arithmetic against the exact limit plus one epsilon; the figures are [decimal] throughout. Adversary pass A2 on 62dff0fa; sol r2 B1.)
    $timing = { param($frame, $field) $v = Get-DvProp $frame $field; if ($null -ne $frame.PSObject.Properties[$field] -and $null -eq (Get-DvExportTimingProblem $v)) { [decimal][double]$v } else { $null } }
    foreach ($frame in @($Frames)) {
        $at = Get-DvProp $frame 'frame_index'
        $total = & $timing $frame 'llrawproc_ms'
        $producer = & $timing $frame 'producer_frame_ms'
        if ($null -ne $total -and $null -ne $producer -and (Test-DvExportBeyondTolerance $total $producer)) {
            $past.Add("frame_index $at llrawproc_ms $(Get-DvExportRound $total) ms inside producer_frame_ms $(Get-DvExportRound $producer) ms")
        }
        # (r2, sol r1 B2 / INV-2: $sum is the sum of the children this frame PRESENTS, so it is a LOWER BOUND on the full sum when one is absent: a child omitted from the frame is work of 0 or more ms,
        # never less. Only the over-the-container directions are judged on a lower bound; "the bracket is longer than its children" needs the whole sum, so it still waits for $whole.)
        $sum = [decimal]0; $whole = $true   # (a figure that is absent or not a conforming timing leaves the sum underivable on this frame; the record schema types that elsewhere)
        foreach ($n in @($script:ExportCdngLlrawprocTimedChildren) + 'llrawproc_other') { $part = & $timing $frame "${n}_ms"; if ($null -eq $part) { $whole = $false } else { $sum += $part } }
        $what = $(if ($whole) { 'the seven timed children and llrawproc_other_ms' } else { 'the present llrawproc children (a lower bound: not every one is published on this frame)' })
        $workerTotal = & $timing $frame 'llrawproc_total_ms'
        if ($null -ne $workerTotal) {
            # (adversary pass A4 on 62dff0fa: the worker is the call the llrawproc_ms bracket wraps (dng.c:2619-2622), so its total is inside the bracket, and inside producer_frame_ms when there is no
            # bracket; with the remainder omitted the present children cannot refute it, and with all eight present they agree with it, so only this containment can see a worker total past what wraps it)
            if ($null -ne $total -and (Test-DvExportBeyondTolerance $workerTotal $total)) { $workerOut.Add("frame_index $at llrawproc_total_ms $(Get-DvExportRound $workerTotal) ms inside the llrawproc_ms bracket $(Get-DvExportRound $total) ms") }
            elseif ($null -ne $producer -and (Test-DvExportBeyondTolerance $workerTotal $producer)) { $workerOut.Add("frame_index $at llrawproc_total_ms $(Get-DvExportRound $workerTotal) ms inside producer_frame_ms $(Get-DvExportRound $producer) ms") }
            if (($whole -and (Test-DvExportBeyondTolerance $workerTotal $sum)) -or (Test-DvExportBeyondTolerance $sum $workerTotal)) {
                $worker.Add("frame_index $at llrawproc_total_ms $(Get-DvExportRound $workerTotal) ms beside $what totalling $(Get-DvExportRound $sum) ms")
            }
        }
        if ($null -eq $total) { continue }
        if ($whole) { $judged++ }
        if (Test-DvExportBeyondTolerance $sum $total) {
            $beyond.Add("frame_index $at $what total $(Get-DvExportRound $sum) ms inside llrawproc_ms $(Get-DvExportRound $total) ms")
        }
        if ($whole -and (Test-DvExportBeyondTolerance $total $sum)) {
            $longer.Add("frame_index $at llrawproc_ms $(Get-DvExportRound $total) ms beside the seven timed children and llrawproc_other_ms totalling $(Get-DvExportRound $sum) ms")
        }
    }
    if ($past.Count -gt 0) { "EXPORT_STAGE_CONTAINMENT_MISMATCH: $($past.Count) frame(s) time llrawproc_ms longer than the producer_frame_ms that contains it (first: $($past[0])), so the published llrawproc total is of work this run's frames do not hold" }
    if ($workerOut.Count -gt 0) { "EXPORT_STAGE_CONTAINMENT_MISMATCH: $($workerOut.Count) frame(s) publish a worker llrawproc_total_ms longer than the llrawproc_ms bracket (or producer_frame_ms) that wraps the worker call (first: $($workerOut[0])), so the published worker total is of work this run's frames do not hold" }
    if ($beyond.Count -gt 0) { "EXPORT_STAGE_SUM_MISMATCH: $($beyond.Count) frame(s) time the llrawproc children longer than the llrawproc_ms that contains them (first: $($beyond[0])), so the published llrawproc total is not the work its children measured" }
    elseif ($longer.Count * 10 -gt $judged) {
        "EXPORT_STAGE_SUM_MISMATCH: $($longer.Count) of $judged frame(s) publish an llrawproc_ms longer than the seven timed children plus llrawproc_other_ms it totals (first: $($longer[0])), so the published llrawproc total is not the work its children measured"
    }
    if ($worker.Count -gt 0) { "EXPORT_STAGE_SUM_MISMATCH: $($worker.Count) frame(s) publish a worker llrawproc_total_ms that is not the sum of the llrawproc children it totals (first: $($worker[0])), so the published llrawproc timings are not one measurement" }
}

function Get-DvExportInt64($Value) {
    # r8 (adversary r7 pass A3): the export writers emit these figures as JSON integers; ConvertTo-DvInt64 stringifies first, so [1] and "1" read as 1. Only a JSON integer is
    # one here, anything else is $null (absent), which every caller already types.
    if ($Value -is [int] -or $Value -is [long]) { return [int64]$Value }
    $null
}

function Get-DvExportRound($Value) { if ($null -eq $Value) { return $null }; [math]::Round([double]$Value, 6) }

function Get-DvExportManifestEndpointProblem($Manifest, [int64]$Count, [int64]$TotalBytes) {
    # EXPORT-VENUE-LEG-1 r19 (sol r18 B3 = pass20-A3): totalBytes is the published outputBytes, and the job writes it beside the first and last file it hashed
    # (export-stage-job.ps1: first/last = { name, bytes, sha256 } of the sorted outputs). The total was checked for type and sign only, so count=1 totalBytes=1 beside a 1000-byte
    # .mov, or 30 .dng files totalling 1 byte beside two 1000-byte endpoints, read PASS with outputBytes=1. The total is reconciled with its parts: one file is the whole total (first
    # and last are that one file); more than one file are two distinct endpoints that fit inside the total; none is a zero total and no endpoint. Anything else is the text of the
    # problem ($null when the manifest agrees with itself). The writer and the receipt re-derivation read it through Get-DvExportEvidence, so all three agree.
    $first = Get-DvProp $Manifest 'first'; $last = Get-DvProp $Manifest 'last'
    if ($Count -eq 0) {
        if ($TotalBytes -ne 0) { return "output-manifest.json lists no file but totalBytes $TotalBytes" }
        if ($null -ne $first -or $null -ne $last) { return 'output-manifest.json lists no file but names a first or last file' }
        return $null
    }
    if ($first -isnot [System.Management.Automation.PSCustomObject] -or $last -isnot [System.Management.Automation.PSCustomObject]) { return "output-manifest.json lists $Count file(s) but its first and last records are not both objects, so totalBytes $TotalBytes cannot be reconciled with them" }
    $fn = Get-DvProp $first 'name'; $ln = Get-DvProp $last 'name'
    $fb = Get-DvExportInt64 (Get-DvProp $first 'bytes'); $lb = Get-DvExportInt64 (Get-DvProp $last 'bytes')
    if ($fn -isnot [string] -or $ln -isnot [string] -or [string]::IsNullOrEmpty($fn) -or [string]::IsNullOrEmpty($ln)) { return 'output-manifest.json first and last records do not both carry a file name' }
    if ($null -eq $fb -or $null -eq $lb -or $fb -lt 0 -or $lb -lt 0) { return 'output-manifest.json first and last records do not both carry a non-negative integer byte size' }
    # adversary pass 22 A2 (on d8ad4c8d): the content hash is part of the record too: each is a sha256, and one file has one hash
    $fh = Get-DvProp $first 'sha256'; $lh = Get-DvProp $last 'sha256'
    if ($fh -isnot [string] -or $lh -isnot [string] -or $fh -cnotmatch '^[0-9a-fA-F]{64}\z' -or $lh -cnotmatch '^[0-9a-fA-F]{64}\z') { return 'output-manifest.json first and last records do not both carry a sha256 (64 hex)' }
    if ($Count -eq 1) {
        if ($fn -cne $ln) { return "output-manifest.json lists one file but its first ('$fn') and last ('$ln') records name different files" }
        if (-not [string]::Equals($fh, $lh, [StringComparison]::OrdinalIgnoreCase)) { return "output-manifest.json lists one file '$fn' but its first and last records carry different sha256 ($fh, $lh)" }
        if ($fb -ne $TotalBytes -or $lb -ne $TotalBytes) { return "output-manifest.json lists one file of $fb bytes (last record $lb) but totalBytes $TotalBytes" }
        return $null
    }
    if ($fn -ceq $ln) { return "output-manifest.json lists $Count files but its first and last records name the same file '$fn'" }
    # adversary pass 23 A1 (on 18592ea9): every produced file has bytes (the reader's own rule for the total), so the endpoints are at least 1 byte each, two files ARE the two
    # endpoints, and N files leave at least one byte for each of the N - 2 between them: 24 DNG files totalling 2000 bytes beside two 1000-byte endpoints is no output
    if ($fb -lt 1 -or $lb -lt 1) { return "output-manifest.json lists $Count files but its first ($fb bytes) or last ($lb bytes) record is an empty file" }
    # EXPORT-VENUE-LEG-1 r20 (adversary pass 25 A1 on d65eec9b): the sums are [decimal], never Int64: two endpoints near Int64.MaxValue overflowed $fb + $lb into a DOUBLE, whose
    # rounding read 9223372036854775806 + 1 + 478 as not past totalBytes 9223372036854775807, and a manifest whose endpoints use the whole total passed. A decimal holds any sum here exactly.
    $endpointBytes = [decimal]$fb + [decimal]$lb
    if ($Count -eq 2 -and $endpointBytes -ne [decimal]$TotalBytes) { return "output-manifest.json lists two files of $fb and $lb bytes but totalBytes $TotalBytes" }
    if (($endpointBytes + ([decimal]$Count - 2)) -gt [decimal]$TotalBytes) { return "output-manifest.json lists $Count files totalling $TotalBytes bytes, too few for its first ($fb) and last ($lb) files and one byte for each of the $($Count - 2) between them" }
    $null
}

# --- r7 (adversary r6 A1-A5, Dot I2): ONE record schema for everything the export reader reads, not one check per finding ----------------------------------------
# Every profile frame and every stage-log record is validated against these tables BEFORE a count, a mean or a share is taken, and a record that does not conform
# is a typed problem (EXPORT_RECORD_INVALID: type / null / finite / range; EXPORT_STAGE_WINDOW_MISMATCH: a stage not timed on exactly the frames its denominator is),
# never a value that is silently dropped. A timing is a JSON number, finite, 0 .. ExportTimingMaxMs (24 h, the agent's own cap on a whole job), so no mean or share
# the reader computes can be infinite or negative.
$script:ExportTimingMaxMs = 86400000.0
# CDNG frame record: success is a boolean and frame_index a non-negative integer on every row; every timing below is, WHEN PRESENT, a conforming timing, and a timing
# present on any successful frame is present on all of them (its share is of the denominator's window). These two are required on every successful frame.
$script:ExportCdngTimingFields = @(@($script:ExportCdngProducerStages) + @($script:ExportCdngWriterStages) + @('llrawproc', 'producer_frame') | ForEach-Object { "${_}_ms" })
$script:ExportCdngRequiredTimingFields = @('producer_frame_ms', 'llrawproc_dual_iso_ms')
# (EXPORT-CDNG-TOTAL-CONTAINMENT-1 r2, sol r1 B1 / INV-4: the worker's own total is read by Get-DvExportCdngTotalFrameProblems, so a PRESENT one is schema-checked like every timing above: a string, null,
# array or over-ceiling value is EXPORT_RECORD_INVALID, never treated as absent. It is not a published stage row, so it joins no window rule: only the value domain is checked, on every frame that carries it.)
$script:ExportCdngSchemaOnlyTimingFields = @('llrawproc_total_ms')
# Stage-log record: one line, exactly this shape. Every line that carries the tag must match it (the digits are bounded so no value can overflow a cast).
$script:ExportStageLogTag = '[mlv-stage-timing]'
$script:ExportStageLogRecord = '^\[mlv-stage-timing\] frame=(?<f>[0-9]{1,12}) stage=(?<s>[a-z0-9_]{1,64}) ms=(?<ms>[0-9]{1,12}(?:\.[0-9]{1,9})?)$'

function Get-DvExportTimingProblem {
    # $null when $Value is a conforming timing, else the reason it is not. Never throws, whatever the value is.
    param($Value)
    if ($null -eq $Value) { return 'is JSON null' }
    if (-not ($Value -is [double] -or $Value -is [int] -or $Value -is [long] -or $Value -is [decimal])) { return "is a $(Get-DvTypeName $Value), not a JSON number" }
    $d = [double]$Value
    if (-not [double]::IsFinite($d)) { return 'is not finite' }
    if ($d -lt 0) { return 'is negative' }
    if ($d -gt $script:ExportTimingMaxMs) { return "is past the $($script:ExportTimingMaxMs) ms ceiling" }
    $null
}

function Get-DvExportExactMean {
    # EXPORT-CDNG-TOTAL-CONTAINMENT-1 r2 (sol r1 B3 / INV-5): the UNROUNDED mean of the conforming timings ($null when there is none). The published stage rows round their means to 6 places
    # (Get-DvExportStageRow); a containment gate reads this instead, so 29 frames at the documented 5 % + 1 ms tolerance and one a hair inside it are not turned INVALID by a mean rounded before it is compared.
    param($Values)
    # (a [decimal], summed exactly: a double mean of repeated decimals is a hair off the decimal it equals; adversary pass A2 on 62dff0fa)
    $v = @($Values | Where-Object { $null -eq (Get-DvExportTimingProblem $_) } | ForEach-Object { [decimal][double]$_ })
    if ($v.Count -eq 0) { return $null }
    $sum = [decimal]0; foreach ($x in $v) { $sum += $x }
    $sum / [decimal]$v.Count
}

function Get-DvExportStageRow {
    param($Values, $Denominator)
    # only a conforming timing is a sample (Get-DvExportTimingProblem); the record validators have already refused every other value, so this never drops one silently
    $v = @($Values | Where-Object { $null -eq (Get-DvExportTimingProblem $_) } | ForEach-Object { [double]$_ })
    if ($v.Count -eq 0) { return [ordered]@{ samples = 0; meanMs = $null; medianMs = $null; share = $null } }
    $mean = ($v | Measure-Object -Sum).Sum / $v.Count
    [ordered]@{ samples = $v.Count; meanMs = (Get-DvExportRound $mean); medianMs = (Get-DvExportRound (Get-DvMedian $v))
                share = $(if ($null -ne $Denominator -and [double]$Denominator -gt 0) { Get-DvExportRound ($mean / [double]$Denominator) } else { $null }) }
}

function Test-DvExportCdngProfileRecords {
    # The CDNG profile's `frames` against the tables above. Returns [pscustomobject]@{ rows; ok; problems }: rows = the object records, ok = the successful ones.
    param($Profile)
    $problems = [System.Collections.Generic.List[string]]::new()
    $list = Get-DvProp $Profile 'frames'
    $rows = @(); $ok = @()
    if ($null -ne $list -and $list -isnot [array]) { $problems.Add('EXPORT_RECORD_INVALID: export-profile.json frames is not a JSON array of frame records (one object or a scalar is no list)') }
    elseif ($null -ne $list) {
        $notObject = [System.Collections.Generic.List[string]]::new(); $rows = [System.Collections.Generic.List[object]]::new()
        for ($i = 0; $i -lt $list.Count; $i++) { if ($list[$i] -is [System.Management.Automation.PSCustomObject]) { $rows.Add($list[$i]) } else { $notObject.Add([string]$i) } }
        if ($notObject.Count -gt 0) { $problems.Add("EXPORT_RECORD_INVALID: $($notObject.Count) profile frame record(s) are not JSON objects (first at index $($notObject[0]))") }
        $rows = @($rows)
        $declared = Get-DvProp $Profile 'frame_count'
        if ($null -ne $declared -and (Get-DvExportInt64 $declared) -ne [int64]$list.Count) { $problems.Add("EXPORT_RECORD_INVALID: export-profile.json frame_count is $(ConvertTo-Json -InputObject $declared -Compress -Depth 2) but frames holds $($list.Count) record(s)") }
    }
    if ($rows.Count -eq 0) { $problems.Add('EXPORT_PROFILE_ABSENT: export-profile.json lists no frame, so there is nothing to read a stage share from') }
    # the per-record schema
    $broken = [ordered]@{}
    $note = { param($field, $why, $at) $k = "$field $why"; if (-not $broken.Contains($k)) { $broken[$k] = [System.Collections.Generic.List[int]]::new() }; $broken[$k].Add($at) }
    for ($i = 0; $i -lt $rows.Count; $i++) {
        $r = $rows[$i]
        if ((Get-DvProp $r 'success') -isnot [bool]) { & $note 'success' 'is not a JSON boolean' $i }
        $fi = Get-DvProp $r 'frame_index'
        if (-not (($fi -is [int] -or $fi -is [long]) -and [int64]$fi -ge 0)) { & $note 'frame_index' 'is not a non-negative integer' $i }
        foreach ($field in @($script:ExportCdngTimingFields) + @($script:ExportCdngSchemaOnlyTimingFields)) {
            if ($null -eq $r.PSObject.Properties[$field]) { continue }
            $why = Get-DvExportTimingProblem (Get-DvProp $r $field)
            if ($null -ne $why) { & $note $field $why $i }
        }
    }
    foreach ($k in $broken.Keys) { $problems.Add("EXPORT_RECORD_INVALID: profile frame record(s) at index $(@($broken[$k] | Select-Object -First 3) -join ', ')$(if ($broken[$k].Count -gt 3) { " and $($broken[$k].Count - 3) more" }): $k") }
    $ok = @($rows | Where-Object { $s = Get-DvProp $_ 'success'; $s -is [bool] -and $s })
    # frame identity: each successful frame has its own integer frame_index
    $ids = @($ok | ForEach-Object { $fi = Get-DvProp $_ 'frame_index'; if ($fi -is [int] -or $fi -is [long]) { [int64]$fi } else { 'x' } })
    if (@($ids | Where-Object { $_ -is [string] }).Count -gt 0 -or @($ids | Select-Object -Unique).Count -ne $ok.Count) {
        $problems.Add('EXPORT_OUTPUT_MISMATCH: the successful profile frames do not carry one distinct integer frame_index each')
    }
    # the window rule: a timing present on any successful frame is present on every one (a share is over the denominator's frames); two are required outright
    if ($ok.Count -gt 0) {
        $partial = @($script:ExportCdngTimingFields | Where-Object { $f = $_; $n = @($ok | Where-Object { $null -ne $_.PSObject.Properties[$f] }).Count; $n -gt 0 -and $n -ne $ok.Count })
        if ($partial.Count -gt 0) { $problems.Add("EXPORT_STAGE_WINDOW_MISMATCH: stage(s) $($partial -join ', ') are timed on some but not all of the $($ok.Count) successful frames the producer_frame_ms denominator covers, so their shares are of another window") }
        $missing = @($script:ExportCdngRequiredTimingFields | Where-Object { $f = $_; @($ok | Where-Object { $null -ne $_.PSObject.Properties[$f] }).Count -ne $ok.Count })
        if ($missing.Count -gt 0) { $problems.Add("EXPORT_PROFILE_ABSENT: a successful profile frame carries no $($missing -join ' / '), so the stage share and the clip's dual-ISO class cannot be read") }
    }
    [pscustomobject]@{ rows = @($rows); ok = @($ok); problems = @($problems) }
}

function Test-DvExportStageLogRecords {
    # The rendered path's stage-timing log against the table above. Returns [pscustomobject]@{ perStage; problems }: perStage[stage][frame] = ms for each stage in
    # $script:ExportRenderedStages. A line that carries the tag and is not exactly one conforming record is a problem; a stage the reader does not know is checked
    # for shape and then ignored (it is another stage's record, not this one's).
    param([string]$Text)
    $problems = [System.Collections.Generic.List[string]]::new()
    $perStage = @{}
    foreach ($name in $script:ExportRenderedStages) { $perStage[$name] = @{} }
    $bad = [System.Collections.Generic.List[string]]::new()
    $lineNo = 0
    foreach ($line in ($Text -split "`r?`n")) {
        $lineNo++
        if (-not $line.Contains($script:ExportStageLogTag)) { continue }
        $hit = [regex]::Match($line, $script:ExportStageLogRecord)
        $why = $null
        if (-not $hit.Success) { $why = 'is not one record of the form frame=N stage=NAME ms=NUMBER' }
        else {
            $ms = [double]::Parse($hit.Groups['ms'].Value, [Globalization.CultureInfo]::InvariantCulture)
            $why = Get-DvExportTimingProblem $ms
            if ($null -eq $why) {
                $s = $hit.Groups['s'].Value
                if (-not $perStage.ContainsKey($s)) { continue }
                $f = $hit.Groups['f'].Value.TrimStart('0'); if ($f -eq '') { $f = '0' }
                $perStage[$s][$f] = $(if ($perStage[$s].ContainsKey($f)) { $perStage[$s][$f] + $ms } else { $ms })
            }
        }
        if ($null -ne $why) { $bad.Add("line ${lineNo} $why") }
    }
    if ($bad.Count -gt 0) { $problems.Add("EXPORT_RECORD_INVALID: $($bad.Count) stage-timing record(s) do not conform (first: $($bad[0]))") }
    # r8 (adversary r7 A2, Dot): a frame's repeated records for one stage are SUMMED above, and the sum is the value every mean, median and share reads, so it is a timing too:
    # two lines that each conform can add up past the ceiling, and Get-DvExportStageRow would then drop that frame without a word (a stage with no sample, a share of null,
    # and the frame still counted by the window check). Each accumulated per-frame value is held to the same domain as one record, per stage that carries it.
    $overflow = [System.Collections.Generic.List[string]]::new()
    foreach ($name in $script:ExportRenderedStages) {
        foreach ($f in @($perStage[$name].Keys | Sort-Object { [int64]$_ })) {
            $why = Get-DvExportTimingProblem $perStage[$name][$f]
            if ($null -ne $why) { $overflow.Add("stage $name frame $f (the sum of its records) $why") }
        }
    }
    if ($overflow.Count -gt 0) { $problems.Add("EXPORT_RECORD_INVALID: $($overflow.Count) accumulated per-frame stage timing(s) do not conform (first: $($overflow[0]))") }
    [pscustomobject]@{ perStage = $perStage; problems = @($problems) }
}

function Get-DvExportEvidence {
    <#
    .SYNOPSIS
    EXPORT-VENUE-LEG-1: the pure reader of an export job's published TEXT -- the batch log, the CDNG stage profile (export-profile.json) or the rendered path's
    stage-timing log, and the output manifest. Runs nothing and opens nothing. Returns [pscustomobject]@{ metrics; stages; problems; classMismatch; classDetail }:
      metrics   flat scalars the leg's criteria read: framesRequested, framesExported, exportFramesSkipped, exportFramesFailed, profileFramesSucceeded (CDNG),
                profileFramesFailed (CDNG), stageFramesSeen (ProRes), dualIsoActiveFrames, dualIsoClassAgrees, outputFileCount, outputBytes, wallSeconds,
                exportFps, largestStage, largestStageShare; a metric the format cannot carry is null with <metric>CouldNotCheck naming why;
      stages    the per-stage table (denominator, rows with samples / meanMs / medianMs / share, writer rows for CDNG, nesting and residual for ProRes);
      problems  typed reasons the run is no measurement (EXPORT_PROFILE_ABSENT -- also a profile / stage log with no stage figure, EXPORT_OUTPUT_MISMATCH, EXPORT_OUTPUT_NOT_DELETED, EXPORT_PROFILE_FOREIGN, EXPORT_BATCH_LOG_UNREADABLE);
      classMismatch  the run's own dual-ISO evidence disagrees with the leg's dualIsoExpected (CLIP_CLASS_MISMATCH).
    #>
    param([Parameter(Mandatory)][string]$Format, [Parameter(Mandatory)][int]$MaxFrames, [Parameter(Mandatory)][bool]$DualIsoExpected, [string]$ExpectedBuildId = '',
          [AllowNull()][AllowEmptyString()][string]$ProfileText = '', [AllowNull()][AllowEmptyString()][string]$StageLogText = '',
          [AllowNull()][AllowEmptyString()][string]$BatchLogText = '', [AllowNull()][AllowEmptyString()][string]$OutputManifestText = '')
    $problems = [System.Collections.Generic.List[string]]::new()
    $m = [ordered]@{ framesRequested = $MaxFrames; framesExported = $null; exportFramesSkipped = $null; exportFramesFailed = $null; profileFramesSucceeded = $null
                     profileFramesFailed = $null; stageFramesSeen = $null; dualIsoActiveFrames = $null; dualIsoClassAgrees = $null; outputFileCount = $null; outputBytes = $null
                     wallSeconds = $null; exportFps = $null; largestStage = $null; largestStageShare = $null }
    $classMismatch = $false; $classDetail = $null
    $wallExact = $null   # the DONE elapsed exactly as the log wrote it, [decimal]; null when there is no single DONE line (the wall gates then have nothing to compare)
    $containmentAdvisory = @()   # (r18's advisory channel, kept EMPTY for the receipt shape the leg runner writes (receipt.export.containmentAdvisory): EXPORT-CDNG-STAGE-CONTAINMENT-1 r1 made the check gate again)
    # -- the batch log: exactly one DONE line for the one clip
    # (r7 A6: the counts are bounded to 15 digits, so a count that does not fit an int64 is no DONE line, typed below, and never an Int64 cast that throws)
    $done = @([regex]::Matches([string]$BatchLogText, '(?m)^\[BATCH\] DONE (?<base>.+?) exported=(?<e>[0-9]{1,15}) skipped=(?<s>[0-9]{1,15}) elapsed=(?<t>[0-9]{1,12}(?:\.[0-9]{1,9})?)\r?$'))
    if ([string]::IsNullOrEmpty($BatchLogText)) { $problems.Add('EXPORT_BATCH_LOG_UNREADABLE: batch.log is absent') }
    elseif ($done.Count -ne 1) { $problems.Add("EXPORT_BATCH_LOG_UNREADABLE: batch.log carries $($done.Count) DONE line(s); one clip exports exactly one") }
    else {
        $m.framesExported = [int64]$done[0].Groups['e'].Value
        $m.exportFramesSkipped = [int64]$done[0].Groups['s'].Value
        $m.exportFramesFailed = $m.exportFramesSkipped
        # (EXPORT-CDNG-TOTAL-CONTAINMENT-1 r4 B1, opus r3: wallSeconds is the PUBLISHED figure, 6 decimals; the wall gates' limit is built from the exact matched elapsed, up to 9 decimals, never from it)
        $wallExact = [decimal]::Parse($done[0].Groups['t'].Value, [Globalization.NumberStyles]::AllowDecimalPoint, [Globalization.CultureInfo]::InvariantCulture)
        $m.wallSeconds = Get-DvExportRound ([double]::Parse($done[0].Groups['t'].Value, [Globalization.CultureInfo]::InvariantCulture))
        $m.exportFps = $(if ($m.wallSeconds -gt 0) { Get-DvExportRound ($m.framesExported / $m.wallSeconds) } else { $null })
    }
    # -- the output manifest the job wrote before it deleted the exported files
    $manifest = $null
    if ([string]::IsNullOrEmpty($OutputManifestText)) { $problems.Add('EXPORT_OUTPUT_MISMATCH: output-manifest.json is absent') }
    else {
        $parsed = $false
        try { $manifest = $OutputManifestText | ConvertFrom-Json -NoEnumerate -ErrorAction Stop; $parsed = $true } catch { $problems.Add('EXPORT_OUTPUT_MISMATCH: output-manifest.json does not parse') }
        # adversary r1 A2: JSON null (or an array, a scalar) parses without an exception and is no manifest
        if ($parsed -and $manifest -isnot [System.Management.Automation.PSCustomObject]) { $problems.Add('EXPORT_OUTPUT_MISMATCH: output-manifest.json is not one JSON object'); $manifest = $null }
        if ($null -ne $manifest) {
            $count = Get-DvExportInt64 (Get-DvProp $manifest 'count'); $bytes = Get-DvExportInt64 (Get-DvProp $manifest 'totalBytes')
            $kind = [string](Get-DvProp $manifest 'kind'); $wantKind = $(if ($Format -ceq 'prores') { 'mov' } else { 'dng' })
            $m.outputFileCount = $count; $m.outputBytes = $bytes
            if ($null -eq $count -or $null -eq $bytes) { $problems.Add('EXPORT_OUTPUT_MISMATCH: output-manifest.json carries no integer count / totalBytes') }
            # adversary r2 pass 2 A5: the byte domain too: neither figure is negative, and files that exist have bytes
            elseif ($count -lt 0 -or $bytes -lt 0 -or ($count -gt 0 -and $bytes -eq 0)) { $problems.Add("EXPORT_OUTPUT_MISMATCH: output-manifest.json carries count $count / totalBytes $bytes, which no produced output can have") }
            elseif ($kind -cne $wantKind) { $problems.Add("EXPORT_OUTPUT_MISMATCH: output-manifest.json lists '$kind' output, a $Format export writes $wantKind") }
            elseif ($Format -ceq 'prores') { if ($count -ne 1) { $problems.Add("EXPORT_OUTPUT_MISMATCH: a ProRes export writes one .mov; the manifest lists $count") } }
            elseif ($null -ne $m.framesExported -and $count -ne $m.framesExported) { $problems.Add("EXPORT_OUTPUT_MISMATCH: the manifest lists $count .dng file(s) but the batch exported $($m.framesExported)") }
            # r19 (sol r18 B3): the published total agrees with the endpoint records it was written beside, or the manifest is no evidence of outputBytes
            if ($null -ne $count -and $null -ne $bytes -and $count -ge 0 -and $bytes -ge 0) {
                $endpointProblem = Get-DvExportManifestEndpointProblem -Manifest $manifest -Count $count -TotalBytes $bytes
                if ($endpointProblem) { $problems.Add("EXPORT_OUTPUT_MISMATCH: $endpointProblem") }
            }
            # adversary r2 A3: the exported frames / .mov are derived from the owner's footage and must be gone from the venue; a job whose delete failed (a reader held
            # a file open) says deleted=false, and a manifest with no boolean true is no proof of the delete either
            $deleted = Get-DvProp $manifest 'deleted'
            if (-not ($deleted -is [bool] -and $deleted)) { $problems.Add('EXPORT_OUTPUT_NOT_DELETED: output-manifest.json does not record the exported footage as deleted from the venue (deleted is not true), so derived owner footage may remain there') }
        }
    }
    $stages = $null
    if ($Format -ceq 'cdng-lossless') {
        $m.stageFramesSeen = $null; $m['stageFramesSeenCouldNotCheck'] = 'a CDNG export is timed by the export stage profiler, not the stage-timing log'
        $profile = $null
        if ([string]::IsNullOrEmpty($ProfileText)) { $problems.Add('EXPORT_PROFILE_ABSENT: export-profile.json is absent') }
        else {
            $parsed = $false
            try { $profile = $ProfileText | ConvertFrom-Json -NoEnumerate -ErrorAction Stop; $parsed = $true } catch { $problems.Add('EXPORT_PROFILE_ABSENT: export-profile.json does not parse') }
            if ($parsed -and $profile -isnot [System.Management.Automation.PSCustomObject]) { $problems.Add('EXPORT_PROFILE_ABSENT: export-profile.json is not one JSON object'); $profile = $null }
            if ($null -ne $profile -and [string](Get-DvProp $profile 'schema') -cne 'mlvapp.export_stage_profile.v1') { $problems.Add('EXPORT_PROFILE_ABSENT: export-profile.json is not an mlvapp.export_stage_profile.v1 document'); $profile = $null }
        }
        if ($null -ne $profile) {
            # adversary r2 pass 2 A3: no expected build id (a summary without sourceCommit) is not a pass of this check, it is a profile that cannot be bound to the build
            if ([string]::IsNullOrEmpty($ExpectedBuildId)) { $problems.Add('EXPORT_PROFILE_FOREIGN: no expected build id (the job summary names no sourceCommit), so export-profile.json cannot be bound to the build the job ran') }
            elseif ([string](Get-DvProp $profile 'build_id') -cne $ExpectedBuildId) { $problems.Add('EXPORT_PROFILE_FOREIGN: export-profile.json build_id is not the build the job ran') }
            # r7: ONE schema (Test-DvExportCdngProfileRecords) validates the container, every frame record, every timing and the frame windows before anything is counted
            $records = Test-DvExportCdngProfileRecords -Profile $profile
            foreach ($p in $records.problems) { $problems.Add($p) }
            $rows = @($records.rows); $ok = @($records.ok)
            $m.profileFramesSucceeded = [int64]$ok.Count
            $m.profileFramesFailed = [int64]($rows.Count - $ok.Count)
            # r8 (adversary r7 pass A1): a profile row that says a frame failed is a frame the batch must have counted as skipped; a batch that says skipped=0 beside it is two
            # records of one run that disagree (the shipped criterion exportFramesFailed eq 0 read the batch alone, so the run passed). A skipped frame the profile never logged
            # is not a contradiction (it has no row), so the rule is one-directional: no more failed rows than skipped frames.
            if ($null -ne $m.exportFramesSkipped -and $m.profileFramesFailed -gt $m.exportFramesSkipped) { $problems.Add("EXPORT_OUTPUT_MISMATCH: the profile records $($m.profileFramesFailed) failed frame(s) but the batch skipped $($m.exportFramesSkipped)") }
            # adversary r6 A2: a profile whose every frame failed measured nothing, whatever the leg's criteria say
            if ($rows.Count -gt 0 -and $ok.Count -eq 0) { $problems.Add("EXPORT_PROFILE_ABSENT: the profile records no successful frame (all $($rows.Count) failed), so there is no stage share to read") }
            # r7: the successful frames ARE the exported frames (shipped criteria say so too: profileFramesSucceeded eq the window), so a different count is no measurement of this export
            if ($null -ne $m.framesExported -and $ok.Count -gt 0 -and $ok.Count -ne $m.framesExported) { $problems.Add("EXPORT_OUTPUT_MISMATCH: the profile records $($ok.Count) successful frame(s) but the batch exported $($m.framesExported)") }
            $dualIsoValues = @($ok | ForEach-Object { $dx = Get-DvProp $_ 'llrawproc_dual_iso_ms'; if ($null -eq (Get-DvExportTimingProblem $dx)) { $dx } else { $null } })
            if ($ok.Count -gt 0 -and @($dualIsoValues | Where-Object { $null -ne $_ }).Count -ne $ok.Count) {
                # (the reason is already typed by the record schema above; the class is simply not readable)
                $m.dualIsoClassAgrees = $null
            }
            elseif ($ok.Count -gt 0) {
                $active = @($dualIsoValues | Where-Object { [double]$_ -gt 0 }).Count
                $m.dualIsoActiveFrames = [int64]$active
                $agrees = $(if ($DualIsoExpected) { $active -eq $ok.Count } else { $active -eq 0 })
                $m.dualIsoClassAgrees = $agrees
                if (-not $agrees) { $classMismatch = $true; $classDetail = "CLIP_CLASS_MISMATCH: dualIsoExpected is $($DualIsoExpected.ToString().ToLowerInvariant()) but $active of $($ok.Count) exported frame(s) ran dual-ISO processing" }
            }
            $producer = @($ok | ForEach-Object { Get-DvProp $_ 'producer_frame_ms' })
            $denRow = Get-DvExportStageRow -Values $producer -Denominator $null
            $rowsOut = [ordered]@{}; $writer = [ordered]@{}
            foreach ($name in $script:ExportCdngProducerStages) { $rowsOut[$name] = Get-DvExportStageRow -Values @($ok | ForEach-Object { Get-DvProp $_ "${name}_ms" }) -Denominator $denRow.meanMs }
            foreach ($name in $script:ExportCdngWriterStages) { $writer[$name] = Get-DvExportStageRow -Values @($ok | ForEach-Object { Get-DvProp $_ "${name}_ms" }) -Denominator $denRow.meanMs }
            $largest = @($rowsOut.Keys | Where-Object { $null -ne $rowsOut[$_].meanMs } | Sort-Object { -[double]$rowsOut[$_].meanMs } | Select-Object -First 1)
            $stages = [ordered]@{ denominator = 'producer_frame_ms'; denominatorMeanMs = $denRow.meanMs; denominatorMedianMs = $denRow.medianMs; rows = $rowsOut; writerRows = $writer
                                  llrawprocTotal = (Get-DvExportStageRow -Values @($ok | ForEach-Object { Get-DvProp $_ 'llrawproc_ms' }) -Denominator $denRow.meanMs)
                                  dngCompressPlacement = (Get-DvProp $profile 'dng_compress_placement'); asyncWriterCanOverlapDngCompress = (Get-DvProp $profile 'async_writer_can_overlap_dng_compress')
                                  largestStage = $(if ($largest.Count) { [string]$largest[0] } else { $null }) }
            if ($largest.Count) { $m.largestStage = [string]$largest[0]; $m.largestStageShare = $rowsOut[[string]$largest[0]].share }
            # r8 fix 2 (adversary r8 A1): every producer stage is inside producer_frame_ms and every llrawproc_* child is inside llrawproc_ms
            # (r2, sol r1 B3: the gates read the UNROUNDED means, so a profile at the documented tolerance on every frame is not turned INVALID by a mean rounded before it is compared; the published rows keep their rounded means)
            $exactDen = Get-DvExportExactMean $producer
            $means = @{ producer_frame = $exactDen; llrawproc = (Get-DvExportExactMean @($ok | ForEach-Object { Get-DvProp $_ 'llrawproc_ms' })) }
            foreach ($name in $script:ExportCdngProducerStages) { $means[$name] = Get-DvExportExactMean @($ok | ForEach-Object { Get-DvProp $_ "${name}_ms" }) }
            # (r8 fix 3, adversary pass 10 A1: the llrawproc TOTAL is published as a row too, so it is held inside producer_frame_ms like every producer stage)
            $pairs = @(foreach ($name in $script:ExportCdngProducerStages) { , @($name, 'producer_frame') }) + @(foreach ($name in $script:ExportCdngLlrawprocChildren) { , @($name, 'llrawproc') }) + @(, @('llrawproc', 'producer_frame'))
            # r11 (fable r9 B2): the producer stages are the DISJOINT partition of producer_frame (the llrawproc TOTAL is not in that list, so it is not counted twice), so they are held TOGETHER
            # inside it, as the rendered call below holds its disjoint siblings; each below the parent was no proof that their sum is (40 + 30 + 30 + 40 ms inside a 41 ms producer_frame).
            # The llrawproc_* children are the partition of llrawproc (llrawproc_other is its remainder; dng.c clamps it at 0, which the 5 % + 1 ms slack covers), held together inside it the same way.
            # Replayed over the eight real receipts (638671c5, 0fc26d37, 2fcd3cf4, 64185ab8, dbd2fc28, f23fa981, 1e9153b6, 01905cfe): all still pass both groups.
            # (adversary r11 A2: the two groups are the PARTITIONS dng.c emits, not every stage with a name: llrawproc_other is total - the seven timed children, so it RETAINS pre_dualiso_fix and the
            # three lock times, which are published beside it and must not be added to it again; and the llrawproc children are inside the llrawproc TOTAL, which the producer partition counts once)
            # (adversary r13 A2: llrawproc_ms is an optional total; without it the producer partition had one member short of two and was skipped, leaving 80 ms of disjoint work inside a 50 ms producer
            # unjudged. The third group is the producer partition with the llrawproc TOTAL replaced by its own partition, which holds whether or not the total is published.)
            # (adversary r13-2 A1: llrawproc_other is total less the seven timed children, so the lock times it RETAINS are not in the partition, and a profile may publish one beside a too-small
            # llrawproc_other (dual_iso 30 + publish_lock 90 inside a 100 ms total read PASS).)
            # r17 (pass16 A1, CDNG-STAGE-WINDOW-TABLE-1): every one of these groups, and every other maximal disjoint group, is DERIVED from $script:ExportCdngStageWindowTable
            # (Get-DvExportCdngDisjointGroups): each llrawproc group inside llrawproc and, with the producer's other members, inside producer_frame, whether or not llrawproc_ms is published.
            # EXPORT-CDNG-STAGE-CONTAINMENT-1 r1: that DISJOINT-STAGE check -- the table-derived sum groups and the table-derived contained pairs (pass17 A1: refine and publish inside the
            # shared_lock they accumulate into; pass18 A1: inside llrawproc_other) -- GATES again (r18 had made it advisory while the table carried windows export never runs). The table now
            # holds only the code path CDNG export reaches, so a finding is an EXPORT_STAGE_CONTAINMENT_MISMATCH problem; this branch is the CDNG profile's only, so it gates CDNG legs only.
            $containment = Get-DvExportContainmentProblem -Means $means -Pairs $pairs
            if ($null -ne $containment) { $problems.Add($containment) }
            $disjoint = Get-DvExportContainmentProblem -Means $means -Pairs $script:ExportCdngContainedPairs -SumGroups $script:ExportCdngSumGroups
            if ($null -ne $disjoint) { $problems.Add($disjoint) }
            # EXPORT-CDNG-TOTAL-CONTAINMENT-1 (Dot I5): the llrawproc TOTAL inside producer_frame_ms, the children inside it and (when published) the worker total equal to them, per frame (the mean pair above cannot see one frame; the longer-than-its-children direction is a more-than-a-tenth-of-the-frames rule)
            foreach ($totalProblem in @(Get-DvExportCdngTotalFrameProblems -Frames $ok)) { $problems.Add($totalProblem) }
            # r11 (sol r9 B2 (a)): and the producer's per-frame work is inside the batch's own elapsed
            $wallContainment = Get-DvExportWallContainmentProblem -WallSeconds $wallExact -Frames $m.framesExported -StageName 'producer_frame' -StageMeanMs $exactDen
            if ($null -ne $wallContainment) { $problems.Add($wallContainment) }
            # adversary r2 A2: a capture is a stage-share measurement; successful rows with no producer_frame_ms (or no producer stage figure at all) measured none
            if ($ok.Count -gt 0 -and ($denRow.samples -ne $ok.Count -or $null -eq $denRow.meanMs -or [double]$denRow.meanMs -le 0 -or $largest.Count -eq 0)) {
                $problems.Add("EXPORT_PROFILE_ABSENT: the profile carries no producer_frame_ms and per-stage timings for its $($ok.Count) successful frame(s) ($($denRow.samples) with a denominator, $($largest.Count) producer stage row(s)), so no stage share can be read")
            }
            # (the stage windows -- every timed stage on every successful frame, none on a subset -- and the value domain are the record schema's: Test-DvExportCdngProfileRecords)
        }
    }
    else {
        $m.profileFramesSucceeded = $null; $m['profileFramesSucceededCouldNotCheck'] = 'a ProRes export is timed by the stage-timing log, not the export stage profiler'
        $why = "$($script:ExportSuccessor): the rendered export path records no dual-ISO stage, so a ProRes leg cannot read its clip's dual-ISO class"
        $m['dualIsoActiveFramesCouldNotCheck'] = $why; $m['dualIsoClassAgreesCouldNotCheck'] = $why
        $verified = @([regex]::Matches([string]$BatchLogText, '(?m)^\[BATCH\] RENDER_VERIFIED \S+ decoded_frames=(?<n>[0-9]{1,15})(?=\s|$)'))   # r15 (pass14 A2): the token ends at whitespace or the line end, so 480.5, 480-1 and 480/2 verify nothing (a word boundary also ends it at '.')
        if ($verified.Count -ne 1) { $problems.Add("EXPORT_OUTPUT_MISMATCH: batch.log carries $($verified.Count) RENDER_VERIFIED line(s); the batch's own verification of the .mov must pass exactly once") }
        elseif ($null -ne $m.framesExported -and [int64]$verified[0].Groups['n'].Value -ne $m.framesExported) { $problems.Add("EXPORT_OUTPUT_MISMATCH: the batch verified $($verified[0].Groups['n'].Value) decoded frame(s) but exported $($m.framesExported)") }
        if ([string]::IsNullOrEmpty($StageLogText)) { $problems.Add('EXPORT_PROFILE_ABSENT: stage-timing.log is absent') }
        else {
            # r7: ONE schema (Test-DvExportStageLogRecords) -- every line that carries the tag is one conforming record, or the log is INVALID with a typed reason
            $records = Test-DvExportStageLogRecords -Text $StageLogText
            foreach ($p in $records.problems) { $problems.Add($p) }
            $perStage = $records.perStage
            $m.stageFramesSeen = [int64]$perStage['processed16_total'].Count
            if ($m.stageFramesSeen -eq 0) { $problems.Add('EXPORT_PROFILE_ABSENT: stage-timing.log records no processed16_total frame') }
            # r7: processed16_total is timed once per exported frame (the shipped criterion stageFramesSeen eq the window says the same), so a log of another frame count is another window
            elseif ($null -ne $m.framesExported -and $m.stageFramesSeen -ne $m.framesExported) { $problems.Add("EXPORT_STAGE_WINDOW_MISMATCH: stage-timing.log times $($m.stageFramesSeen) frame(s) but the batch exported $($m.framesExported)") }
            # adversary r1 A4: the batch's DONE elapsed starts before the clip opens and also times finish, decode-verify and probe, so wall / frames is no
            # per-frame loop time and never the share denominator. The shares are of the timed per-frame work (processed16_total); what the batch wall adds per
            # frame is reported apart as untimed (open, pipe write, ffmpeg back-pressure, finish, verify), never as a stage.
            $batchWall = $(if ($null -ne $m.framesExported -and $m.framesExported -gt 0 -and $null -ne $m.wallSeconds) { Get-DvExportRound ($m.wallSeconds * 1000.0 / $m.framesExported) } else { $null })
            $p16 = Get-DvExportStageRow -Values @($perStage['processed16_total'].Values) -Denominator $null
            $rowsOut = [ordered]@{}
            foreach ($name in $script:ExportRenderedStages) { $rowsOut[$name] = Get-DvExportStageRow -Values @($perStage[$name].Values) -Denominator $p16.meanMs }
            $untimed = $(if ($null -ne $batchWall -and $null -ne $p16.meanMs) { Get-DvExportRound ($batchWall - $p16.meanMs) } else { $null })
            # r11 (sol r9 B2 (a)): the measured per-frame work is inside the batch's elapsed (typed INVALID otherwise); a residual the logging precision made slightly negative is 0, never a negative stage
            $wallContainment = Get-DvExportWallContainmentProblem -WallSeconds $wallExact -Frames $m.framesExported -StageName 'processed16_total' -StageMeanMs (Get-DvExportExactMean @($perStage['processed16_total'].Values))
            if ($null -ne $wallContainment) { $problems.Add($wallContainment) }
            elseif ($null -ne $untimed -and $untimed -lt 0) { $untimed = 0.0 }
            $disjoint = $(if ($null -ne $rowsOut['debayered_frame'].meanMs -and $null -ne $rowsOut['processing'].meanMs) { Get-DvExportRound ($rowsOut['debayered_frame'].meanMs + $rowsOut['processing'].meanMs) } else { $null })
            $within = $(if ($null -ne $disjoint -and $null -ne $p16.meanMs -and $p16.meanMs -gt 0) { [math]::Abs($disjoint - $p16.meanMs) / $p16.meanMs -le 0.05 } else { $null })
            $largest = @($script:ExportRenderedTopLevel | Where-Object { $null -ne $rowsOut[$_].meanMs } | Sort-Object { -[double]$rowsOut[$_].meanMs } | Select-Object -First 1)
            $stages = [ordered]@{ denominator = 'processed16_total'; denominatorMeanMs = $p16.meanMs; denominatorMedianMs = $p16.medianMs; rows = $rowsOut; nesting = $script:ExportRenderedNesting
                                  disjointSumMs = $disjoint; disjointWithin5Percent = $within; batchWallPerFrameMs = $batchWall; untimedPerFrameMs = $untimed
                                  untimed = "$($script:ExportSuccessor): batch wall per frame minus processed16_total (clip open, pipe write, ffmpeg back-pressure, finish and verify) is not timed per stage by the app"
                                  drainVerify = "$($script:ExportSuccessor): the rendered path times no drain or verify stage"
                                  largestStage = $(if ($largest.Count) { [string]$largest[0] } else { $null }) }
            if ($largest.Count) { $m.largestStage = [string]$largest[0]; $m.largestStageShare = $rowsOut[[string]$largest[0]].share }
            # adversary r2 A2: processed16_total alone is no breakdown; both disjoint children (debayered_frame, processing) must be present and the denominator positive
            # (pass 2 A1: each child must cover exactly the frames processed16_total covers, or its share is of another window)
            # adversary r6 A1: equal sample COUNTS are not the same window; each child must also be timed on exactly the frames processed16_total is timed on
            # r7 (A1 + Dot I2): that holds for EVERY stage the table publishes a share for, at every depth: a disjoint child or nested stage (raw_uint16, its disk_read and
            # decompress, llrawproc) is timed on exactly processed16_total's frames or on none at all (a stage the app did not run is absent, never partial)
            # r8 fix 2 (adversary r8 A1): and each stage's mean is inside the stage that contains it (the rendered stage tree in $script:ExportRenderedContainment)
            $means = @{}
            foreach ($name in $script:ExportRenderedStages) { $means[$name] = Get-DvExportExactMean @($perStage[$name].Values) }   # (unrounded: sol r1 B3)
            $containment = Get-DvExportContainmentProblem -Means $means -Pairs $script:ExportRenderedContainment -SumGroups $script:ExportRenderedSumGroups
            if ($null -ne $containment) { $problems.Add($containment) }
            $p16Frames = $perStage['processed16_total']
            $missingChild = @($script:ExportRenderedTopLevel | Where-Object { $rowsOut[$_].samples -ne $p16.samples })
            if ($m.stageFramesSeen -gt 0 -and ($null -eq $p16.meanMs -or [double]$p16.meanMs -le 0 -or $missingChild.Count -gt 0)) {
                $problems.Add("EXPORT_PROFILE_ABSENT: stage-timing.log times $($missingChild -join ' / ') on fewer frames than processed16_total's $($p16.samples) (mean $($p16.meanMs) ms), so no stage share can be read")
            }
            $otherWindow = @($script:ExportRenderedStages | Where-Object { $_ -cne 'processed16_total' } | Where-Object {
                $frames = $perStage[$_]
                $frames.Count -gt 0 -and ($frames.Count -ne $p16Frames.Count -or @($frames.Keys | Where-Object { -not $p16Frames.ContainsKey($_) }).Count -gt 0) })
            if ($m.stageFramesSeen -gt 0 -and $otherWindow.Count -gt 0) {
                $problems.Add("EXPORT_STAGE_WINDOW_MISMATCH: stage-timing.log times $($otherWindow -join ' / ') on frames other than the $($p16Frames.Count) processed16_total is timed on, so their shares are of another window")
            }
        }
    }
    [pscustomobject]@{ metrics = [pscustomobject]$m; stages = $stages; problems = @($problems); classMismatch = $classMismatch; classDetail = $classDetail; containmentAdvisory = [string[]]@($containmentAdvisory) }
}

function Get-DvExportOutcome {
    <#
    .SYNOPSIS
    EXPORT-VENUE-LEG-1: the ONE export outcome rule, shared by Invoke-VenueLeg (writer) and Test-DvReceiptValid (re-derivation). A failure terminal maps as
    Resolve-DvJobOutcome maps it. EXPORT_CAPTURED becomes PASS/FAIL only when the job exited 0, the run's dual-ISO class is the leg's, and the evidence has no
    problem; otherwise INVALID (a capture that disagrees with its exit code; CLIP_CLASS_MISMATCH; unbound evidence). A playback token is never an export result.
    #>
    param([string]$ResultToken, [int]$ExitCode, [Parameter(Mandatory)]$Evidence, $Criteria)
    $base = Resolve-DvJobOutcome -ResultToken $ResultToken -ExitCode $ExitCode
    # Only the export's own failure terminals are a product FAIL. A clip-length refusal (the job's RESULT is the gate's verdict), an owner-footage or work-dir
    # refusal, a missing RESULT line or any unknown token falls through Resolve-DvJobOutcome to FAIL; for an export leg that is no measurement: INVALID.
    if ($base.outcome -eq 'FAIL' -and $ResultToken -cnotin $script:ExportFailResults) {
        $why = $(if ($ResultToken -cin $script:PlayLengthRefusalReasons) { 'the clip failed the export length gate' } else { 'not an export failure terminal' })
        return [pscustomobject]@{ outcome = 'INVALID'; detail = "$(if ([string]::IsNullOrEmpty($ResultToken)) { 'no RESULT line' } else { $ResultToken }) (job exit ${ExitCode}): $why, so it is no product result"; criteria = $null }
    }
    if ($base.outcome -ne 'CAPTURED') { return [pscustomobject]@{ outcome = $base.outcome; detail = $base.detail; criteria = $null } }
    if ($ResultToken -cne 'EXPORT_CAPTURED') { return [pscustomobject]@{ outcome = 'INVALID'; detail = "$ResultToken is not an export job's result"; criteria = $null } }
    if ($ExitCode -ne 0) { return [pscustomobject]@{ outcome = 'INVALID'; detail = "EXPORT_CAPTURED with job exit ${ExitCode}: a capture that disagrees with its own exit code is not evidence"; criteria = $null } }
    if ($Evidence.classMismatch) { return [pscustomobject]@{ outcome = 'INVALID'; detail = [string]$Evidence.classDetail; criteria = $null } }
    if (@($Evidence.problems).Count -gt 0) { return [pscustomobject]@{ outcome = 'INVALID'; detail = (@($Evidence.problems) -join '; '); criteria = $null } }
    # r7 (adversary r6 A3): EXPORT_CAPTURED asserts the batch exported the requested window, whatever criteria the leg carries. A count that is not the request is INVALID
    # (the evidence disagrees with the token) unless the leg's own criteria GATE that same window: r8 (adversary r7 A1) it is no longer enough that a criterion NAMES
    # framesExported (`framesExported ge 0` named it and switched the check off, so 24 of 30 frames read PASS). Only `framesExported eq <the request>` hands the verdict
    # to the criteria, because only that criterion fails every other count (the shipped legs' criterion reads FAIL); any other criterion on it leaves the count to this rule.
    $requested = $Evidence.metrics.framesRequested; $exported = $Evidence.metrics.framesExported
    $gatesTheWindow = @(@($Criteria) | Where-Object {
        if ($null -eq $_ -or [string](Get-DvProp $_ 'metric') -cne 'framesExported' -or [string](Get-DvProp $_ 'op') -cne 'eq') { return $false }
        $want = Get-DvProp $_ 'value'
        ($want -is [int] -or $want -is [long] -or $want -is [double]) -and $null -ne $requested -and [double]$want -eq [double]$requested }).Count -gt 0
    if (-not $gatesTheWindow -and $null -ne $requested -and ($null -eq $exported -or [int64]$exported -ne [int64]$requested)) {
        return [pscustomobject]@{ outcome = 'INVALID'; detail = "EXPORT_FRAMES_MISMATCH: the batch exported $(if ($null -eq $exported) { 'no countable number of' } else { $exported }) of the $requested requested frame(s) yet the job reported EXPORT_CAPTURED, and no criterion of the leg sets another expectation"; criteria = $null }
    }
    $verdict = Test-DvCriteria -Criteria $Criteria -Metrics $Evidence.metrics
    $detail = $(if ($verdict.pass) { "EXPORT_CAPTURED: $(if ($verdict.informational) { 'no gating criteria' } else { 'all criteria met' }); largest stage $($Evidence.metrics.largestStage)" } else { 'EXPORT_CAPTURED: ' + ($verdict.failures -join '; ') })
    [pscustomobject]@{ outcome = $(if ($verdict.pass) { 'PASS' } else { 'FAIL' }); detail = $detail; criteria = $verdict }
}

function Test-DvExportReceiptValid {
    # EXPORT-VENUE-LEG-1: the export branch of Test-DvReceiptValid. $null when the receipt is not an export receipt (no export block, and its contract spec is not
    # an export leg). Otherwise the verdict, re-derived from the hashed published files: um-run.json, summary.json and every file receipt.export.files lists (under
    # <evidence>\export), the export metrics and stage table by Get-DvExportEvidence (the writer's reader), and the outcome by Get-DvExportOutcome against the
    # contract spec's criteria (production: the committed spec; offline test: the bytes the runner ran). The playback-only blocks are not required here.
    param($Receipt, [bool]$Production, $CommittedSpec, [bool]$AllowOffline, [byte[]]$OfflineLegSpecBytes, [string]$EvidenceDir, $Invalid, $Incomplete, $Unbound, [string]$Outcome, [string]$Mode)
    $exportBlock = Get-DvProp $Receipt 'export'
    $subject = Get-DvProp $Receipt 'subject'
    $contract = $null; $contractWhy = $null
    if ($Production) { $contract = $CommittedSpec; if ($null -eq $contract) { $contractWhy = 'the committed leg spec is not readable' } }
    elseif ($AllowOffline -and $null -ne $OfflineLegSpecBytes) {
        if ((Get-DvLegSpecSha256 $OfflineLegSpecBytes) -cne [string](Get-DvProp $subject 'legSpecSha256')) { $contractWhy = 'the leg spec the runner ran does not hash to subject.legSpecSha256' }
        else { try { $contract = [Text.Encoding]::UTF8.GetString($OfflineLegSpecBytes).TrimStart([char]0xFEFF) | ConvertFrom-Json -ErrorAction Stop } catch { $contractWhy = 'the leg spec the runner ran does not parse' } }
    }
    else { $contractWhy = 'no leg spec was given to re-derive the export from' }
    $specIsExport = ([string](Get-DvProp $contract 'legType') -ceq 'export')
    if ($null -eq $exportBlock -and -not $specIsExport) { return $null }
    if ($null -eq $contract) { $Invalid.Add("EXPORT_CONTRACT_ABSENT: $contractWhy, so the export receipt is not re-derivable") }
    elseif (-not $specIsExport) { $Invalid.Add('EXPORT_LEG_MISMATCH: the receipt carries an export block but its leg spec is not an export leg') }
    else {
        $shape = Get-DvLegSpecShapeRefusal -Spec $contract
        if ($null -ne $shape) { $Invalid.Add("EXPORT_LEG_MISMATCH: the leg spec is not a valid export leg ($shape)") }
    }
    if ($null -eq $exportBlock) { $Incomplete.Add('RECEIPT_FIELD_ABSENT: an export leg''s receipt carries no export block') }
    if ([string](Get-DvProp $subject 'backend') -cne 'cpu') { $Invalid.Add('BACKEND_MISMATCH: an export leg runs the cpu backend only') }
    $specExport = Get-DvProp $contract 'export'
    if ($null -ne $exportBlock -and $null -ne $specExport) {
        foreach ($k in 'format', 'maxFrames', 'dualIsoExpected', 'receiptSha256') {
            if (-not (Test-DvJsonEquivalent (Get-DvProp $exportBlock $k) (Get-DvProp $specExport $k))) { $Invalid.Add("EXPORT_BLOCK_MISMATCH: export.$k is not the leg spec's") }
        }
    }
    # ---- the hashed files
    $evidence = Get-DvProp $Receipt 'evidence'
    $dir = $(if (-not [string]::IsNullOrWhiteSpace($EvidenceDir)) { $EvidenceDir } else { [string](Get-DvProp $evidence 'localEvidenceDir') })
    $texts = @{}; $umRun = $null; $files = $null   # (adversary r1 A3: $files is read below even when the evidence directory is absent)
    if ([string]::IsNullOrWhiteSpace($dir) -or -not (Test-Path -LiteralPath $dir -PathType Container)) { $Incomplete.Add('EVIDENCE_ABSENT: the local evidence directory the receipt names is not present') }
    else {
        $umClaim = [string](Get-DvProp $evidence 'umRunJsonSha256')
        $umFile = Join-Path $dir 'um-run.json'
        if ($umClaim -cnotmatch '^[0-9a-f]{64}$' -or -not (Test-Path -LiteralPath $umFile -PathType Leaf)) { $Incomplete.Add('EVIDENCE_ABSENT: um-run.json (evidence.umRunJsonSha256) is not in the evidence') }
        elseif ((Get-DvSha256OfFile $umFile) -cne $umClaim) { $Invalid.Add('EVIDENCE_HASH_MISMATCH: um-run.json does not hash to the sha256 the receipt claims') }
        else {
            $parsed = $false
            try { $umRun = (ConvertTo-DvText ([IO.File]::ReadAllBytes($umFile))) | ConvertFrom-Json -NoEnumerate -ErrorAction Stop; $parsed = $true } catch { $Invalid.Add('EVIDENCE_UNPARSABLE: um-run.json is not valid JSON') }
            # adversary r1 A2: JSON null parses without an exception and is no um-run record
            if ($parsed -and $umRun -isnot [System.Management.Automation.PSCustomObject]) { $Invalid.Add('EVIDENCE_UNPARSABLE: um-run.json is not one JSON object'); $umRun = $null }
            if ($null -ne $umRun -and [string](Get-DvProp $umRun 'schema') -cne $script:UmRunSchema) { $Invalid.Add('EVIDENCE_UNPARSABLE: um-run.json is not a dual-venue um-run record'); $umRun = $null }
        }
        $files = Get-DvProp $exportBlock 'files'
        if ($null -eq $files) { $Incomplete.Add('EVIDENCE_ABSENT: export.files lists no published file') }
        else {
            foreach ($name in @(Get-DvKeys $files)) {
                if ($name -cnotin $script:ExportEvidenceFiles) { $Invalid.Add("EVIDENCE_UNPARSABLE: export.files names '$name', which an export job does not publish"); continue }
                $claim = [string](Get-DvProp $files $name)
                $file = Join-Path (Join-Path $dir 'export') $name
                if ($claim -cnotmatch '^[0-9a-f]{64}$' -or -not (Test-Path -LiteralPath $file -PathType Leaf)) { $Incomplete.Add("EVIDENCE_ABSENT: export/$name is not in the evidence"); continue }
                $b = [IO.File]::ReadAllBytes($file)
                if ((Get-DvSha256OfBytes $b) -cne $claim) { $Invalid.Add("EVIDENCE_HASH_MISMATCH: export/$name does not hash to the sha256 the receipt claims"); continue }
                $texts[$name] = ConvertTo-DvText $b
            }
        }
    }
    $summary = $null
    if ($texts.ContainsKey('summary.json')) {
        $parsed = $false
        try { $summary = $texts['summary.json'] | ConvertFrom-Json -NoEnumerate -ErrorAction Stop; $parsed = $true } catch { $Invalid.Add('EVIDENCE_UNPARSABLE: export/summary.json is not valid JSON') }
        if ($parsed -and $summary -isnot [System.Management.Automation.PSCustomObject]) { $Invalid.Add('EVIDENCE_UNPARSABLE: export/summary.json is not one JSON object'); $summary = $null }
    }
    elseif ($null -ne $files) { $Incomplete.Add('EVIDENCE_ABSENT: export/summary.json is not listed in export.files') }
    if ($null -ne $summary -and $null -ne $specExport) {
        $venueName = [string](Get-DvProp (Get-DvProp $Receipt 'venue') 'name')
        foreach ($pair in @(@('schema', 'export-stage-job.v1'), @('legType', 'export'), @('format', [string](Get-DvProp $specExport 'format')), @('clipId', [string](Get-DvProp $subject 'clipId')), @('backend', 'cpu'),
                            @('declaredVenue', $venueName), @('venue', $venueName), @('buildManifestSha256', [string](Get-DvProp $subject 'buildManifestSha256')), @('receiptSha256', [string](Get-DvProp $specExport 'receiptSha256')))) {
            if ([string](Get-DvProp $summary $pair[0]) -cne $pair[1]) { $Invalid.Add("EXPORT_SUMMARY_MISMATCH: export/summary.json $($pair[0]) is not the receipt's or the leg spec's") }
        }
        if (-not (Test-DvJsonEquivalent (Get-DvProp $summary 'maxFrames') (Get-DvProp $specExport 'maxFrames'))) { $Invalid.Add('EXPORT_SUMMARY_MISMATCH: export/summary.json maxFrames is not the leg spec''s') }
        if ($null -ne $umRun -and [string](Get-DvProp $summary 'result') -cne [string](Get-DvProp $umRun 'resultToken')) { $Invalid.Add('EXPORT_SUMMARY_MISMATCH: export/summary.json result is not the RESULT the job printed') }
    }
    # ---- the metrics, the stage table and the outcome, re-derived
    if ($Invalid.Count -eq 0 -and $Incomplete.Count -eq 0 -and $null -ne $summary -and $null -ne $umRun -and $null -ne $specExport) {
        $exit = Get-DvExportInt64 (Get-DvProp $umRun 'exitCode')
        if ($null -eq $exit) { $Invalid.Add('EVIDENCE_UNPARSABLE: um-run.json carries no integer exitCode') }
        # r8 (adversary r7 pass A4): the Int64 parse admits 2147483648, and the [int] narrowing below threw before this function could return its typed INVALID
        elseif ($exit -lt [int64][int]::MinValue -or $exit -gt [int64][int]::MaxValue) { $Invalid.Add('EVIDENCE_UNPARSABLE: um-run.json exitCode is outside the 32-bit range a process exit code has') }
        else {
            $ev = Get-DvExportEvidence -Format ([string]$specExport.format) -MaxFrames ([int]$specExport.maxFrames) -DualIsoExpected ([bool]$specExport.dualIsoExpected) -ExpectedBuildId ([string](Get-DvProp $summary 'sourceCommit')) `
                -ProfileText ([string]$texts['export-profile.json']) -StageLogText ([string]$texts['stage-timing.log']) -BatchLogText ([string]$texts['batch.log']) -OutputManifestText ([string]$texts['output-manifest.json'])
            if (-not (Test-DvJsonEquivalent (Get-DvProp $Receipt 'metrics') (ConvertFrom-Json (ConvertTo-Json $ev.metrics -Depth 8 -Compress)))) { $Invalid.Add('METRICS_NOT_FROM_EVIDENCE: the receipt metrics are not the export metrics of the hashed files') }
            if (-not (Test-DvJsonEquivalent (Get-DvProp $exportBlock 'stages') $(if ($null -eq $ev.stages) { $null } else { ConvertFrom-Json (ConvertTo-Json $ev.stages -Depth 8 -Compress) }))) { $Invalid.Add('METRICS_NOT_FROM_EVIDENCE: export.stages is not the stage table of the hashed files') }
            $role = [string](Get-DvProp (Get-DvProp $Receipt 'venue') 'role')
            $criteria = Get-DvProp (Get-DvProp (Get-DvProp $contract 'criteria') $role) 'cpu'
            $derived = Get-DvExportOutcome -ResultToken ([string](Get-DvProp $umRun 'resultToken')) -ExitCode ([int]$exit) -Evidence $ev -Criteria $criteria
            if ($derived.outcome -cnotin @('PASS', 'FAIL')) { $Invalid.Add("OUTCOME_NOT_DERIVABLE: the export evidence derives $($derived.outcome) ($($derived.detail)), which is not a PASS/FAIL signal") }
            elseif ($derived.outcome -cne $Outcome) { $Invalid.Add("OUTCOME_NOT_DERIVED: the export evidence and the leg's criteria derive $($derived.outcome) but the receipt says $Outcome") }
        }
    }
    elseif ($Invalid.Count -eq 0 -and $Incomplete.Count -eq 0) {
        # adversary r1 A2: no guard above may be the only thing between a missing input and a verified PASS/FAIL
        $Invalid.Add('OUTCOME_NOT_DERIVABLE: the export summary, um-run record or leg spec export block is not readable, so the outcome cannot be re-derived')
    }
    $reasons = @($Invalid.ToArray()) + @($Incomplete.ToArray())
    if ($Production) { $reasons += 'VENUE_ANCHOR_ABSENT: no venue-held anchor (a venue-signed summary.json, or a re-read of the venue outbox by jobId) exists, so a production receipt is advisory: it is never VERIFIED and never a usable PASS (DUAL-VENUE-PASS-PROVENANCE-1)' }
    $status = $(if ($Invalid.Count -gt 0) { 'INVALID' } elseif ($Incomplete.Count -gt 0) { 'INCOMPLETE' } elseif ($Mode -ceq 'offline-test') { 'VERIFIED_OFFLINE_TEST' } elseif ($Production) { 'ADVISORY' } else { 'VERIFIED' })
    [pscustomobject]@{ valid = ($status -in @('VERIFIED', 'VERIFIED_OFFLINE_TEST')); status = $status; reasons = $reasons; unbound = $Unbound; evidenceDir = $dir; legType = 'export' }
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

    # ---- EXPORT-VENUE-LEG-1: an export receipt carries no playback blocks; its export block is re-derived from its own hashed files ----------------
    $exportCheck = Test-DvExportReceiptValid -Receipt $Receipt -Production $production -CommittedSpec $spec -AllowOffline ([bool]$AllowOfflineTestMode) -OfflineLegSpecBytes $OfflineLegSpecBytes `
        -EvidenceDir $EvidenceDir -Invalid $invalid -Incomplete $incomplete -Unbound $unbound -Outcome $outcome -Mode $mode
    if ($null -ne $exportCheck) { return $exportCheck }

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
        # r8 (adversary r7 pass A4, sibling): the same Int64 -> [int] narrowing below (Resolve-DvJobOutcome) throws on an out-of-range code; it is a typed INVALID instead
        elseif ($exit -lt [int64][int]::MinValue -or $exit -gt [int64][int]::MaxValue) { $invalid.Add('EVIDENCE_UNPARSABLE: um-run.json exitCode is outside the 32-bit range a process exit code has'); $exit = $null }

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
                    # r26 (sol r25 B2 sibling): ordinal and canonical (Test-DvLookFlavorSame), so a `cinematic<U+200B>` summary or subject is a mismatch
                    if (-not (Test-DvLookFlavorSame $specFlavor ([string](Get-DvProp $ev.summary 'lookFlavor'))) -or -not (Test-DvLookFlavorSame $specFlavor ([string](Get-DvProp $subject 'lookFlavor')))) { $invalid.Add('LEG_TYPE_MISMATCH: the look flavor in the hashed summary.json / receipt subject is not the committed spec''s') }
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
                # HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1 r26 (sol r25 B2): ordinal and canonical (Test-DvLookFlavorSame), never the linguistic -ceq
                if (-not ($claimVerdict.asked -and $claimVerdict.verdict -ceq 'HONOURED' -and $claimForcedRaw -is [bool] -and $claimForcedRaw -and $claimRequestedRaw -is [string] -and $claimReportedRaw -is [string] -and $claimStoredRaw -is [string] -and $claimRequested.Length -gt 0 -and (Test-DvLookFlavorSame $claimReportedRaw $claimRequested) -and (Test-DvLookFlavorSame $claimLogFlavor $claimRequested) -and (Test-DvLookFlavorSame $claimStoredRaw $claimLogFlavor))) {
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
            # VENUE-LEG-FULLSCREEN-TOGGLE-DURING-PLAY-1 r1 (adversary A5): a leg that names no midPlayWindowToggles but whose run log shows window toggles was moved by an inherited env
            $windowToggleRequestVerdict = Get-DvWindowToggleRequestEvidence -Spec $spec -LogText $ev.logText
            if ($windowToggleRequestVerdict.blocks) { $invalid.Add("WINDOW_TOGGLES_INHERITED: $($windowToggleRequestVerdict.reason)") }
            # r3 (sol B4): a leg that names the toggles has its windowToggles* scalars re-derived from the hashed run log; a claim the raw records do not back is INVALID
            $windowToggleReconcileVerdict = Get-DvWindowToggleReconcileEvidence -Spec $spec -LogText $ev.logText -Metrics $metrics
            if ($windowToggleReconcileVerdict.blocks) { $invalid.Add($windowToggleReconcileVerdict.reason) }
            # CPU-S1-FS-SPIKE-TEAM-PARTITION-1: a leg that asks for the team partition the app did not report at exactly r+p (or whose report is unreadable, or
            # absent because the binary emits none), or a control leg whose app reports a partition, is no measurement of its arm; the receipt's own
            # teamPartition block is re-derived like the reconReleaseDeferred block.
            $teamPartitionVerdict = Get-DvTeamPartitionEvidence -Spec $spec -LogText $ev.logText
            if ($teamPartitionVerdict.blocks) { $invalid.Add("TEAM_PARTITION_NOT_HONOURED: $($teamPartitionVerdict.reason); a run whose OpenMP team partition is not the one its leg names (r+p when it asks, otherwise off) is no measurement of that arm") }
            $storedTeamPartition = Get-DvProp $Receipt 'teamPartition'
            if ($teamPartitionVerdict.named) {
                if ($null -eq $storedTeamPartition) { $incomplete.Add('RECEIPT_FIELD_ABSENT: the receipt carries no teamPartition block (requested / observed / verdict) and its committed spec names a reconOmpThreads / prepOmpThreads') }
                else {
                    foreach ($f in @('requested', 'observed', 'verdict')) {
                        if (-not (Test-DvJsonEquivalent (Get-DvProp $storedTeamPartition $f) $teamPartitionVerdict[$f])) { $invalid.Add("TEAM_PARTITION_NOT_FROM_EVIDENCE: teamPartition.$f is not what the hashed run log and the committed leg spec derive") }
                    }
                }
            }
            elseif ($null -ne $storedTeamPartition) { $invalid.Add('TEAM_PARTITION_NOT_FROM_EVIDENCE: the receipt carries a teamPartition block but its committed leg spec names no reconOmpThreads / prepOmpThreads') }
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
            # EXPORT-VENUE-LEG-1: an export job's capture is never playback evidence (an export receipt is validated by the export branch above)
            if ([string](Get-DvProp $ev.umRun 'resultToken') -ceq 'EXPORT_CAPTURED') { $invalid.Add('OUTCOME_NOT_DERIVABLE: EXPORT_CAPTURED is an export job''s capture, not playback evidence') }
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
$script:CapturedResults = @('MEASUREMENT_CAPTURED', 'EXPORT_CAPTURED')
# EXPORT-VENUE-LEG-1: an export job's terminals (tools/profiling/bachelor/export-stage-job.ps1). A run that exported too few frames, none, or did not finish
# is a product FAIL; one whose stage evidence or output does not bind is no measurement (INVALID); a venue package without ffmpeg.exe is VENUE_TOOLING.
$script:ExportFailResults = @('EXPORT_EXIT_NONZERO', 'EXPORT_TIMEOUT', 'EXPORT_NO_FRAMES', 'EXPORT_FRAMES_SHORT')
$script:ExportInvalidResults = @('EXPORT_PROFILE_ABSENT', 'EXPORT_OUTPUT_MISMATCH')
$script:ExportToolingResults = @('EXPORT_TOOLING_ABSENT')
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
    if ($ResultToken -cin $script:ExportFailResults) { return [pscustomobject]@{ outcome = 'FAIL'; detail = "$ResultToken (job exit $ExitCode)" } }
    if ($ResultToken -cin $script:ExportInvalidResults) { return [pscustomobject]@{ outcome = 'INVALID'; detail = "$ResultToken (job exit $ExitCode): the export's stage evidence or output does not bind, so it is no measurement" } }
    if ($ResultToken -cin $script:ExportToolingResults) { return [pscustomobject]@{ outcome = 'VENUE_TOOLING'; detail = "$ResultToken (job exit $ExitCode): the staged package carries no ffmpeg.exe for the rendered export" } }
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
        if ($null -ne $field[1] -and -not ($field[1] -is [string])) { "$($field[0]) is a $(Get-DvTypeName $field[1]) ('$($field[1])'), not a string" }
    })
    if ($null -ne $atAppStall -and -not ($atAppStall -is [bool])) { $untyped += "presentMonTemporalOverBoundGapAtAppStall is a $(Get-DvTypeName $atAppStall) ('$atAppStall'), not a Boolean" }
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
        # EXPORT-VENUE-LEG-1: an export leg's block (format, maxFrames, dualIsoExpected, receiptSha256, the sha256 of each published file under <evidence>\export,
        # the stage table, the problems and the verdict). Null on a playback leg. A PASS/FAIL export receipt is re-derived from it by Test-DvReceiptValid.
        export = $null
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
    Get-DvPlaybackEvidence, Get-DvScaleEvidence, Get-DvScaleLabel, Get-DvDisplayEvidence, Get-DvUnpacedEvidence, Get-DvLookLabelEvidence, Get-DvWorkerThreadsEvidence, Get-DvPrepConflatedMoveEvidence, Get-DvReconReleaseDeferredEvidence, Get-DvDueWakeDeliveryEvidence, Get-DvWindowToggleRequestEvidence, Get-DvWindowToggleReconcileEvidence, Get-DvTeamPartitionEvidence, Get-DvLookAssistEvidence, Test-DvLookFlavorSame, Test-DvAppliedLookFlavor, Get-DvLegSetPlan, Get-DvSmokeSessionLineFields, Get-DvSmokeSessionLineRead, Get-DvLogRecordBody, Get-DvMatrixRates, Get-DvMatrixRow, Test-DvMatrixRowMeasured, Get-DvMatrixMeasuredCount, Get-DvMedian, ConvertTo-DvMatrixTable, Get-DvBlobById, Find-DvCommittedLegSpec, Read-DvCommittedLookReceipt, Read-DvEvidenceSet, Test-DvJsonEquivalent, Test-DvReceiptValid, Get-DvJobNotAfter, Get-DvUmRunNotAfterArgs, Get-DvUmRunDropVisibleUtc, Get-DvUmRunDropNotAfter, Test-DvNotAfterCoversDrop, Get-DvHealthVerdict,
    New-DvHealthProbeJobText, ConvertFrom-DvProbeStdout,
    Get-DvResultToken, Resolve-DvJobOutcome, Get-DvExportEvidence, Get-DvExportOutcome, Test-DvCriteria, Get-DvPresentMonGapVerdict, Get-DvVerbatimMetrics, Get-DvCriteriaMetrics, New-DvReceipt, Write-DvReceipt
