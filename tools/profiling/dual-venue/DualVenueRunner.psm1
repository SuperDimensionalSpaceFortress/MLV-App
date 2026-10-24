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

function Test-DvUnderClaudeState {
    # Owner footage (a contact sheet, a receipt that names it) is only ever written where it stays local: a path with a
    # `.claude-state` segment (gitignored; never committed, PR-attached, bus-published or published as an artifact).
    param([Parameter(Mandatory)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    @($full.Split([char[]]@('\', '/')) | Where-Object { $_ -ceq '.claude-state' }).Count -gt 0
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
    if ($Text -match '(?i)[a-z]:[\\/]|\.mlv\b') { return (& $bad 'the consent file carries a path-shaped value') }
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
        $lines = $LogText -split "`r?`n"
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
    when the conform line was PRESENT for the measured session (conformSession == summarySession), conformActive == 1, conformClipFps is
    within 0.5 % of nativeFps, conformPlaybackFps is > 0 and within 0.5 % of paceFps, and conformPlaybackFps is not above conformClipFps.
    Absent / malformed / inactive / another session's / inconsistent -> proven=$false and `reason` says why (absence never helps).
    The same decision as Get-GuiSmokeConformPaceProof (gui-smoke-length-gate.ps1) and Get-AttrCudaConformAwareSourceFramesVerdict (AttrCudaArtifacts.psm1);
    a parity test executes all three on one table.
    #>
    param($Playback)
    $out = [pscustomobject]@{ proven = $false; reason = 'no playback_smoke.conform line for the run'; present = $false }
    if ((Get-DvProp $Playback 'conformLinePresent') -ne $true) { return $out }
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
    if ($null -eq $native -or $native -le 0 -or $clip -le 0 -or [Math]::Abs($clip - $native) -gt (0.005 * $native)) { $out.reason = "conform clip_fps=$clip does not match native_fps=$native"; return $out }
    if ($null -eq $pace -or $pace -le 0 -or $play -le 0 -or [Math]::Abs($play - $pace) -gt (0.005 * $pace)) { $out.reason = "conform playback_fps=$play does not match pace_fps=$pace"; return $out }
    if ($play -gt ($clip * 1.005)) { $out.reason = "conform playback_fps=$play is above clip_fps=$clip; conform never speeds a clip up"; return $out }
    $out.proven = $true
    $out.reason = ''
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
    #>
    param($Playback, [string]$ExpectedClipId)
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
    elseif ($native -le 0) { $reasons.Add("INVALID_SOURCE_FRAMES: native_fps=$native; the native frame rate is unknown, so 20 s of footage cannot be measured") }
    elseif ($null -ne $required -and $required -lt [int64][Math]::Ceiling($floorSeconds * $native - 0.02)) {
        $reasons.Add("INVALID_SOURCE_FRAMES: required_source_frames=$required is under ceil($floorSeconds s x native_fps=$native); the Play was admitted for less than $floorSeconds s of footage")
    }
    if ($null -eq $pace) { $reasons.Add('RECEIPT_FIELD_ABSENT: pace_fps') }
    elseif ($pace -le 0) { $reasons.Add("INVALID_SOURCE_FRAMES: pace_fps=$pace; a present engine pace that is not positive is unknown") }
    elseif ($null -ne $native -and $native -gt 0) {
        # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: only a PROVEN conform (Get-DvConformPaceProof) moves the pace comparison off the native fps.
        $conformProof = Get-DvConformPaceProof -Playback $Playback
        if ($conformProof.proven) {
            $conformPlayback = ConvertTo-DvDouble (Get-DvProp $Playback 'conformPlaybackFps')
            if ([Math]::Abs($pace - $conformPlayback) -gt (0.005 * $conformPlayback)) {
                $reasons.Add("INVALID_SOURCE_FRAMES: the engine paced at pace_fps=$pace but the conformed playback_fps is $conformPlayback; 20 s of wall clock is not 20 s of footage")
            }
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
    #>
    param($Summary, $EvidenceManifest, $ResultJson, [AllowNull()][AllowEmptyString()][string]$LogText, [AllowNull()][AllowEmptyString()][string]$LogSha256,
          [Parameter(Mandatory)][string]$ExpectedClipId)
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
    $problems = @(Get-DvPlaybackProblems -Playback $pb -ExpectedClipId $ExpectedClipId)
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
            foreach ($line in ($LogText -split "`r?`n")) {
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
    $reported = $null
    $verdict = 'NOT_REQUESTED'
    $reason = $null
    if ($requested) {
        $found = $null
        if (-not [string]::IsNullOrEmpty($LogText)) {
            foreach ($line in ($LogText -split "`r?`n")) { if ($line -match '(^|\s)event=gui_smoke\.visual_state(\s|$)') { $found = $line } }
        }
        $verdict = 'NOT_HONOURED'
        if ($null -eq $found) { $reason = 'the run log has no gui_smoke.visual_state line, so nothing shows the app ran with Look Assist on' }
        else {
            $m = [regex]::Match($found, '(^|\s)look_assist_enabled=(?<v>\S+)')
            if (-not $m.Success) { $reason = 'the app''s gui_smoke.visual_state line carries no look_assist_enabled' }
            else {
                $reported = $m.Groups['v'].Value
                if ($reported -cne '1') { $reason = "the app's gui_smoke.visual_state line says look_assist_enabled=$reported (the leg asks for Look Assist on)" }
                else { $verdict = 'HONOURED' }
            }
        }
    }
    [ordered]@{ requested = $requested; reported = $reported; verdict = $verdict; reason = $reason; blocks = ($verdict -ceq 'NOT_HONOURED') }
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
    or an empty table. Marker is e.g. 'pace_summary' or 'cpu_summary'.
    #>
    param([AllowNull()][AllowEmptyString()][string]$LogText, [Parameter(Mandatory)][ValidatePattern('^[a-z_]+$')][string]$Marker)
    $fields = @{}
    if ([string]::IsNullOrEmpty($LogText)) { return $fields }
    $session = (Get-DvSmokeSummaryFields -LogText $LogText).session
    if ($null -eq $session) { return $fields }
    $pattern = 'playback_smoke\.' + $Marker + ' session=' + [regex]::Escape([string]$session) + '(\s|$)'
    $found = $null
    foreach ($line in ($LogText -split "`r?`n")) { if ($line -match $pattern) { $found = $line } }
    if ($null -ne $found) { foreach ($m in [regex]::Matches($found, '(?<k>[A-Za-z0-9_]+)=(?<v>\S+)')) { $fields[$m.Groups['k'].Value] = $m.Groups['v'].Value } }
    $fields
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
    #>
    param([bool]$Requested, [bool]$Captured, [int]$PlayFrames, [int]$DistinctContentHashes, [int]$AppDistinctDisplayFrames, [int]$FlatPlayFrames,
          $PlayVsPausedMeanAbsDiff, [bool]$SamePosition, [double]$Threshold = $script:VisualDivergenceThreshold,
          [ValidateSet('known-differ', 'known-identical', 'unknown')][string]$SourceDifference = 'unknown', [int]$ViewportUnavailablePlayFrames = 0, $LabelBound = $null,
          $PausedLabelBound = $null, [int]$ObserverPaintedPlayFrames = 0, [int]$RepeatedPlayFrames = 0)
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
    [pscustomobject]@{ verdict = 'OK'; counted = $true; reason = $waivedReason }
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
    Never throws for a missing or broken capture: the block says what is missing (a capture is evidence about the leg, not the leg itself).
    #>
    param([bool]$Requested, [string]$OptOutReason, [string]$ArtifactsDir, [string]$EvidenceDir, [Parameter(Mandatory)][string]$LegId, [string]$Header = '')
    $block = [ordered]@{
        schema = $script:VisualSchema; requested = $Requested; optOutReason = $(if ($Requested) { $null } else { $OptOutReason })
        method = $null; forwarded = $null; probe = $null; probeReason = $null; captured = $false
        frames = @(); playFrames = 0; savedPlayFrames = 0; distinctContentHashes = 0; appDistinctDisplayFrames = 0; flatPlayFrames = 0
        sourceDifference = 'unknown'; viewportUnavailablePlayFrames = 0
        stopFrame = $null; pausedSettled = $null; pausedFrame = $null; samePosition = $false
        playVsPausedMeanAbsDiff = $null; playVsPausedMeanAbsDiffPerChannel = $null; divergenceThreshold = $script:VisualDivergenceThreshold
        labelBound = $null; stopUnboundVsPausedMeanAbsDiff = $null; stopUnboundVsStopMeanAbsDiff = $null
        pausedLabelBound = $null; labelBoundBasis = $null; pausedLabelBoundBasis = $null; pausedUnboundVsPausedMeanAbsDiff = $null
        frozenJudgedOn = 'play'; forcedPlayDistinctContentHashes = $null
        observerPaintedPlayFrames = $null; stopUnboundObserverClean = $null; repeatedPlayFrames = $null; repeatedPlayFramesNote = $null
        tailBeginUtc = $null; tailEndUtc = $null; presentMonWindowEndUtc = $null
        stripPath = $null; stripSha256 = $null; stripCopyPath = $null
        verdict = $null; counted = $true; reason = $null; appError = $null
    }
    $notJudgedNote = $null   # set once the frames are read; kept after a verdict reason rather than replaced by it
    $maskNote = $null        # set when the forced play captures moved over unforced ones that did not; kept the same way
    $observerNote = $null    # set when some unforced play captures followed a forced paint and were not judged; kept the same way
    $repeatNote = $null      # set when judged unforced captures repeat their predecessor's picture while the label advanced; kept the same way
    $stripNote = $null      # set when the strip cannot be composed; kept the same way, after the not-judged note
    $reasonBeforeStrip = $null   # the step reason standing when the strip failed (a NOT_CAPTURED leg keeps it between the capture count and the strip failure)
    $finish = {
        $v = Get-DvVisualVerdict -Requested $Requested -Captured ([bool]$block.captured) -PlayFrames ([int]$block.playFrames) -DistinctContentHashes ([int]$block.distinctContentHashes) `
            -AppDistinctDisplayFrames ([int]$block.appDistinctDisplayFrames) -FlatPlayFrames ([int]$block.flatPlayFrames) -PlayVsPausedMeanAbsDiff $block.playVsPausedMeanAbsDiff -SamePosition ([bool]$block.samePosition) `
            -SourceDifference ([string]$block.sourceDifference) -ViewportUnavailablePlayFrames ([int]$block.viewportUnavailablePlayFrames) -LabelBound $block.labelBound `
            -PausedLabelBound $block.pausedLabelBound -ObserverPaintedPlayFrames ([int]$block.observerPaintedPlayFrames) -RepeatedPlayFrames ([int]$block.repeatedPlayFrames)
        # a gate reason replaces a step's reason, except that a NOT_CAPTURED leg keeps the step that says WHY nothing was captured
        # (the verdict reason comes first, then the notes that some saved play frames were not judged, then the masking and repeat notes, then the strip failure;
        # a PARTIAL_FREEZE reason already says what the repeat note says, so the note stays in repeatedPlayFramesNote only)
        $notes = (@($notJudgedNote, $observerNote, $maskNote, $(if ($v.verdict -cne 'PARTIAL_FREEZE') { $repeatNote }), $stripNote) | Where-Object { $_ }) -join '; '
        # a NOT_CAPTURED leg whose strip also failed keeps both: the capture count, any step reason from before the strip, then the strip failure
        if ($v.verdict -ceq 'NOT_CAPTURED' -and $stripNote) { $block.reason = (@($v.reason, $reasonBeforeStrip, $stripNote) | Where-Object { $_ }) -join '; ' }
        elseif ($null -ne $v.reason -and -not ($v.verdict -ceq 'NOT_CAPTURED' -and $null -ne $block.reason)) { $block.reason = $(if ($notes) { "$($v.reason); $notes" } else { $v.reason }) }
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
    # Whether the source frames differ: only the app's own record says so (differ | identical, exactly that case: `identical` waives the gate, so `IDENTICAL`
    # is not the app's word); anything else, or nothing, is unknown -- never a guess.
    $block.sourceDifference = $(switch -CaseSensitive ([string](Get-DvProp $app 'source_difference')) { 'differ' { 'known-differ' } 'identical' { 'known-identical' } default { 'unknown' } })
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
                             displayFrame = Get-DvProp $f 'display_frame'; playActive = Get-DvProp $f 'play_active'; tailElapsedMs = Get-DvProp $f 'tail_elapsed_ms'
                             captureMs = Get-DvProp $f 'capture_ms'; viewport = Get-DvProp $f 'viewport'; saved = [bool](Get-DvProp $f 'saved'); appReason = Get-DvProp $f 'reason'
                             serial = Get-DvProp $f 'serial'; paintedSerial = Get-DvProp $f 'painted_serial'; paintedSerialValid = Get-DvProp $f 'painted_serial_valid'
                             labelBound = Get-DvProp $f 'label_bound'; labelBoundBasis = Get-DvProp $f 'label_bound_basis'; forcedPaintsBefore = Get-DvProp $f 'forced_paints_before' }
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
    $block.distinctContentHashes = @($play | ForEach-Object { $_.contentHash } | Sort-Object -Unique).Count
    $block.appDistinctDisplayFrames = @($play | ForEach-Object { $_.displayFrame } | Where-Object { $null -ne $_ -and [int]$_ -ge 0 } | Sort-Object -Unique).Count
    $block.flatPlayFrames = @($play | Where-Object { $null -ne $_.viewportStd -and [double]$_.viewportStd -lt $script:VisualFlatViewportStd }).Count
    if ($judgeKind -ceq 'play_unbound' -and $block.distinctContentHashes -eq 1 -and [int]$block.forcedPlayDistinctContentHashes -gt 1) {
        $maskNote = "the forced play captures show $($block.forcedPlayDistinctContentHashes) distinct pictures where the unforced ones show one: the force-paint would have masked this"
        if (-not $block.reason) { $block.reason = $maskNote }
    }
    if ($judgeKind -ceq 'play_unbound' -and $block.sourceDifference -cne 'known-identical') {
        # a display whose own paint stopped part-way: a judged unforced picture equal to the one before it while the app's label advanced
        $ordered = @($play | Sort-Object index)
        $block.repeatedPlayFrames = @(for ($i = 1; $i -lt $ordered.Count; $i++) {
            $a = $ordered[$i - 1]; $b = $ordered[$i]
            if ($b.contentHash -ceq $a.contentHash -and $null -ne $a.displayFrame -and $null -ne $b.displayFrame -and [int]$b.displayFrame -ne [int]$a.displayFrame) { $b.index }
        }).Count
        if ($block.repeatedPlayFrames -gt 0 -and $block.distinctContentHashes -gt 1) {
            # the cause is named only over source frames known to differ; over unknown source content a repeat may be the source's own (Dot a5acb3c8)
            $repeatNote = "$($block.repeatedPlayFrames) of the $($play.Count) unforced play captures repeat the picture before them while the app's label advanced: " + $(
                if ($block.sourceDifference -ceq 'known-differ') { "the display's own paint stopped landing for part of the tail" }
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
        $v = Get-DvVisualVerdict -Requested $true -Captured ([bool]$block.captured) -PlayFrames ([int]$block.playFrames) -DistinctContentHashes ([int]$block.distinctContentHashes) `
            -AppDistinctDisplayFrames ([int]$block.appDistinctDisplayFrames) -FlatPlayFrames ([int]$block.flatPlayFrames) -PlayVsPausedMeanAbsDiff $block.playVsPausedMeanAbsDiff -SamePosition ([bool]$block.samePosition) `
            -SourceDifference ([string]$block.sourceDifference) -ViewportUnavailablePlayFrames ([int]$block.viewportUnavailablePlayFrames) -LabelBound $block.labelBound `
            -PausedLabelBound $block.pausedLabelBound -ObserverPaintedPlayFrames ([int]$block.observerPaintedPlayFrames) -RepeatedPlayFrames ([int]$block.repeatedPlayFrames)
        $head ="$Header  visual=$($v.verdict)  distinct=$($block.distinctContentHashes)/$($block.playFrames)  play-vs-paused=$($block.playVsPausedMeanAbsDiff)"
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
    param([Parameter(Mandatory)]$Receipt, [string]$RepoRoot = '', [string]$EvidenceDir = '', [switch]$AllowOfflineTestMode)
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
    if ($ev.complete) {
        $derived = Get-DvPlaybackEvidence -Summary $ev.summary -EvidenceManifest $ev.manifest -ResultJson $ev.result -LogText $ev.logText -LogSha256 $ev.logSha -ExpectedClipId $clipId
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
                $verdict = Test-DvCriteria -Criteria $criteria -Metrics $metrics
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
# Added to an uncapped bound: um-run's clock starts after its drop to the share and it reads the outbox every -PollSeconds (3 s), so the client can still
# be waiting a little past submit + the sum of its waits.
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
      - -LaterHarvest: a LATER call reads the result by name, wants the job RUN, and cannot re-submit its single-use JobId (the stage driver's DLL pair and
        smoke runner). That reader comes at a time this call cannot bound, so the later bound is none: notAfterUtc '' and nothing is passed. Source
        'later-harvest'.
    -MaxAdmissionWaitSec defaults to um-run.ps1's own default; the result echoes the one used so the caller passes the same value by name.
    .OUTPUTS
    [pscustomobject]@{ notAfterUtc ('o' UTC text ending in Z, or ''); source; boundSec; waitSec; longestWaitSec; maxAdmissionWaitSec; why }
    #>
    param(
        [Parameter(Mandatory)][ValidateRange(1, 86400)][int]$TimeoutSec,
        [Parameter(Mandatory)][ValidateRange(0, 86400)][int]$MaxQueueWaitSec,
        [ValidateRange(0, 86400)][int]$MaxAdmissionWaitSec = $script:DvUmRunMaxAdmissionWaitSec,
        [Parameter(Mandatory)][ValidateRange(0, 86400)][int]$MaxClaimedWaitSec,
        [ValidateRange(0, 86400)][int]$CallerCapSec = 0,
        [switch]$LaterHarvest,
        $NowUtc = $null
    )
    $waits = @($TimeoutSec, $MaxQueueWaitSec, $MaxAdmissionWaitSec, $MaxClaimedWaitSec)
    $waitSec = ($waits | Measure-Object -Sum).Sum
    $longest = ($waits | Measure-Object -Maximum).Maximum
    if ($CallerCapSec -gt 0 -and $longest -gt $CallerCapSec) {
        throw "DVE_NOT_AFTER_WAIT_OVER_CAP a $longest s um-run wait is longer than the caller's $CallerCapSec s cap: the caller would kill um-run first"
    }
    $result = { param($text, $source, $bound, $why) [pscustomobject]@{ notAfterUtc = $text; source = $source; boundSec = $bound; waitSec = $waitSec; longestWaitSec = $longest; maxAdmissionWaitSec = $MaxAdmissionWaitSec; why = $why } }
    if ($LaterHarvest) { return (& $result '' 'later-harvest' $null 'a later call harvests this result by name and cannot re-submit its single-use JobId: no notAfter') }
    # (-NowUtc is a test seam: a [DateTime] of any Kind; Local is converted, Unspecified is taken as UTC)
    $now = $(if ($null -eq $NowUtc) { [DateTime]::UtcNow } elseif (([datetime]$NowUtc).Kind -eq [DateTimeKind]::Local) { ([datetime]$NowUtc).ToUniversalTime() } else { [DateTime]::SpecifyKind([datetime]$NowUtc, [DateTimeKind]::Utc) })
    $uncapped = $waitSec + $script:DvNotAfterSlackSec
    if ($CallerCapSec -gt 0 -and $CallerCapSec -lt $uncapped) {
        return (& $result $now.AddSeconds($CallerCapSec).ToString('o') 'caller-cap' $CallerCapSec "the caller kills um-run $CallerCapSec s after submit")
    }
    & $result $now.AddSeconds($uncapped).ToString('o') 'um-run-waits' $uncapped "um-run waits at most $waitSec s (queue $MaxQueueWaitSec + admission $MaxAdmissionWaitSec + timeout $TimeoutSec + claimed $MaxClaimedWaitSec) + $($script:DvNotAfterSlackSec) s slack"
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
    param([Parameter(Mandatory)]$Receipt, [Parameter(Mandatory)][string]$ReceiptRoot, [string]$RepoRoot = '', [switch]$OfflineTestMode)
    if ($Receipt['outcome'] -notin $script:OutcomeEnum) { throw "DVE_RECEIPT_OUTCOME_INVALID '$($Receipt['outcome'])' is not one of: $($script:OutcomeEnum -join ', ')" }
    if ($Receipt['outcome'] -in @('PASS', 'FAIL')) {
        # The writer never records a signal without its proof, whatever the caller believed: the SAME evidence-bearing validator a
        # reader calls re-derives the admission from the committed blobs and the run from the hashed evidence files.
        $validity = Test-DvReceiptValid -Receipt $Receipt -RepoRoot $RepoRoot -AllowOfflineTestMode:$OfflineTestMode
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
    ConvertTo-DvLfBytes, Get-DvLegSpecSha256, Get-DvDerivedBackend, Get-DvBackendNotDerivable, Read-DvContactFrames, Get-DvSubjectDigest,
    Get-DvVisualViewportPixels, Get-DvVisualMeanAbsDiff, Get-DvVisualVerdict, New-DvVisualStrip, Read-DvVisualCaptures, Get-DvVisualCaptureRequest, Read-DvVenueTable, ConvertFrom-DvVenueTableText, Get-DvVenueRole, Get-DvProp, Get-DvCommittedFile, Resolve-DvAdmissionSources,
    Test-DvUnderClaudeState, Read-DvClipConsent, Get-DvClipAdmission, Get-DvSmokeSummaryFields, Get-DvConformPaceProof, Get-DvMinPlaySeconds, Get-DvPlaybackProblems,
    Get-DvPlaybackEvidence, Get-DvScaleEvidence, Get-DvScaleLabel, Get-DvDisplayEvidence, Get-DvUnpacedEvidence, Get-DvLookAssistEvidence, Get-DvLegSetPlan, Get-DvSmokeSessionLineFields, Get-DvMatrixRates, Get-DvMatrixRow, Get-DvMedian, ConvertTo-DvMatrixTable, Get-DvBlobById, Find-DvCommittedLegSpec, Read-DvCommittedLookReceipt, Read-DvEvidenceSet, Test-DvJsonEquivalent, Test-DvReceiptValid, Get-DvJobNotAfter, Get-DvHealthVerdict,
    New-DvHealthProbeJobText, ConvertFrom-DvProbeStdout,
    Get-DvResultToken, Resolve-DvJobOutcome, Test-DvCriteria, Get-DvVerbatimMetrics, New-DvReceipt, Write-DvReceipt
