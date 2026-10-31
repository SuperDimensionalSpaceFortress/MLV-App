# Invoke-VenueStageNightly.ps1 -- the TRACKED nightly venue stage driver (UM-REGULAR-BASELINE-STAGE-1). RUN THIS ON THE HUB VM (VIRTUAL-TEN), once per night.
#
# Owner directive 2026-10-18 (Layi, hub chat): "I'm curious how playback performs on ultramagnus so id like regular benchmarking there as well when possible".
# Invoke-VenueBaseline.ps1 only FINDS a staged build (exit 22 BUILD_NOT_STAGED when it is not there). This driver puts one commit (default: the local fork/master
# ref; no fetch here, the scheduler fetches) into a venue cache, at most once per night, by the four tracked staging steps every venue lane already used. It adds
# no staging logic of its own:
#   1. DLL pair on Ultra-Magnus: tools\profiling\ultramagnus\playback-attr-3-cuda-dll-job.ps1, submitted with tools\profiling\um-run.ps1 (the DLL pair is always
#      built on Ultra-Magnus). A pair the single-use JobId already built for this sha is harvested from <UM share>\outbox\<jobId>.artifacts, never re-submitted.
#   2. assemble on this host: tools\profiling\bachelor\playback-attr-3-cuda-assemble.ps1 (a `git archive` of the sha, make under Invoke-BuildGuard) into
#      <BuildRoot>\build-<sha12> (short: MAX_PATH; parent leaf lane-* so the assembler's own .work retention prunes superseded trees).
#   3. stage: tools\profiling\bachelor\playback-attr-3-cuda-stage-job.ps1 -AgentRoot <venues.json agentRoot>, submitted with um-run.ps1 -AgentShare <agentShare>.
#   4. smoke runner: tools\profiling\bachelor\attr3-stage-smoke-runner-job.ps1, submitted the same way (Invoke-VenueLeg refuses SMOKE_RUNNER_NOT_STAGED without it,
#      and the baseline preflight checks only build.json).
#
# THE CACHE IS THE TRUTH, READ BY NAME (#508, VENUE-STAGED-BUILD-VANISHED-AFTER-ADMISSION-WAIT-1). A re-assemble of one sha makes a NEW manifest (build.json
# carries assembledAtUtc) and the stage JobId is single-use, so an earlier STAGE_OK result for the default JobId says nothing about what the cache holds. So:
#   - a sha whose <agentShare>\cache\playback-attr-3-cuda-<sha12>-build.json is present and well-formed (right sourceCommit), with its three named artifacts and
#     the smoke-runner dir, is STAGE_SKIP_ALREADY: nothing is re-assembled or submitted (the local smoke-runner generator only names the runner dir);
#   - when the default stage JobId is spent and the cache does not hold the assembled manifest, the build is re-staged under -JobIdSuffix n<yyyyMMddHHmm>;
#   - after every STAGE_OK the cache build.json is re-hashed BY NAME; a hash that is not the assembled MANIFEST_SHA256 is STAGE_VERIFY_MISMATCH, never success;
#   - the recorded buildManifestSha256 is ALWAYS that by-name hash of the cache file (the exact path Invoke-VenueBaseline and Invoke-VenueLeg hash), never a local one.
# The share is probed by name only (never enumerated: CLAUDE.md rule 14), each probe bounded by -ProbeTimeoutSec.
#
# ORDER. resolve sha -> night key (the date of the next 08:00Z) -> the venue lock and the once-per-night check against -ReceiptFile -> venue claim / ticket check ->
# share probe -> idempotency -> disk floor + commit ceiling -> the RESERVATION row -> DLL pair -> (floors again when the DLL-pair wait was long) -> assemble ->
# stage -> smoke-runner stage -> by-name verify -> ONE closing receipt line. The claim is re-checked before every submission: this driver never submits over
# another lane's chain.
#
# THE NIGHT IS RESERVED BEFORE IT IS SPENT (CoS Dot b21fb3be, PR #526). Before the first staging step the driver appends a STAGE_RESERVED row (venue, nightKey,
# sourceCommit, a new reservationId), flushes it and reads it back; every later row of the call carries that reservationId. The once-per-night check counts
# STAGE_OK rows AND reservations: a reservation holds its night for its sha whether its call ended STAGE_OK, failed, or never wrote its closing row (killed, or
# its row was lost). Another sha that night is NIGHT_RESERVED (or NIGHT_ALREADY_BUILT when the holder ended STAGE_OK); the same sha resumes, and the row lists
# every incomplete reservation it reconciled. A receipt that cannot be read, a reservation that cannot be persisted, and a closing row that cannot be appended
# are all RECEIPT_IO_FAILED: an outcome the receipt does not hold is never reported as success, and staging never starts without a persisted reservation.
# ONE DRIVER PER VENUE: the lock is the named mutex Global\MLV-VenueStageNightly-<venue>; no caller-selected path is in its name. With the real venue share
# (no -AgentShare) the only receipt taken is the canonical <main checkout>\.claude-state\dual-venue\stage-nightly.jsonl, so the night cap has one ledger.
#
# FLOORS. The disk floor and the host commit ceiling are the ONE shared board scripts <main checkout>\.claude-state\hub-autopilot\disk-floor.ps1 (Test-DiskFloor) and
# host-commit-gate.ps1 (Test-HostCommitGate), dot-sourced, never copied. A missing or unloadable gate script is GATE_UNAVAILABLE (fail closed, never an admit). A refusal
# prints the gate's own text VERBATIM and exits 3: the hub autopilot re-arms a capacity refusal on exactly that text. A build-guard refusal of the assembler
# (BUILD_MEM_REFUSED / BUILD_SLOT_BUSY / BUILD_HEAVY_SLOT_REQUEUE) is printed verbatim and exits 3 too.
#
# BOUNDED. Every child (generators, um-run, the assembler) runs as a process bounded by what is left of -MaxTotalSec and is killed, with its tree, past it. um-run gets
# the same remaining budget as its queue / admission / claimed waits, so it normally ends itself first; an um-run killed at the cap is STAGE_UNRESOLVED (the agent may
# still own the job), never retried in the same call.
# NOT-AFTER (LEASE-NOT-AFTER-SINGLE-LEG-SUBMITTERS-1). The stage job is submitted with -NotAfterUtc at this call's deadline, so a job still queued or gated when
# um-run is killed never takes a Bachelor lease later (a later call re-stages it under a suffix). The DLL pair and the smoke runner get none: a later call
# harvests them by name and cannot re-submit their single-use JobId (the DLL pair runs on Ultra-Magnus, whose agent ignores the field anyway). The bound is
# the ORIGINAL deadline passed as an instant (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1), so a delay before the bound is computed never moves it later; since
# r2 it is passed drop-relative (-NotAfterBudgetSec, capped by -NotAfterCapUtc = that deadline), so the drop writes min(its own now + the waits, the deadline).
#
#   pwsh -NoProfile -File "<repo>\tools\profiling\dual-venue\Invoke-VenueStageNightly.ps1" [-Venue ultra-magnus] [-SourceCommit <40-hex>] [-WhatIf]
#
# Exit codes (typed; the STAGE_NIGHTLY_EXIT line and the STAGE_NIGHTLY_RESULT json carry the token):
#   0  STAGE_OK (staged and verified by name) | STAGE_SKIP_ALREADY (already in the cache) | NIGHT_ALREADY_BUILT (another sha was staged this night) | PLAN_ONLY (-WhatIf)
#   2  USAGE (bad venue table, a receipt file outside .claude-state, a non-canonical receipt for the real venue share, a build root inside the checkout)
#   3  capacity: DISK_FLOOR_REFUSED | HOST_COMMIT_REFUSED | BUILD_GUARD_REFUSED (the refusal text is printed verbatim)
#   40 AGENT_BUSY (a live claim or queued ticket, another driver holds the venue lock, or um-run RETRACTED a job the agent never claimed)
#   41 SHARE_UNREACHABLE (the share did not answer by name inside -ProbeTimeoutSec, the agent has no heartbeat, or the share stalled through placement:
#      UMRUN_NOT_AFTER_PLACEMENT_STALL, the job never visible)
#   42 SOURCE_COMMIT_UNRESOLVED   43 GATE_UNAVAILABLE   44 DLLPAIR_FAILED   45 ASSEMBLE_FAILED   46 STAGE_FAILED   47 STAGE_VERIFY_MISMATCH
#   48 SMOKE_RUNNER_STAGE_FAILED  49 STAGE_UNRESOLVED (um-run stopped while the agent may still own the job; not retried)  50 STAGE_CAP_EXCEEDED (-MaxTotalSec ran out)
#   51 RECEIPT_IO_FAILED (the receipt could not be read for the night check, the reservation could not be persisted -- staging did not start -- or the closing
#      row could not be appended; the result json names the unpersistedOutcome)
#   52 NIGHT_RESERVED (another sha's reservation holds this night and did not end STAGE_OK: it failed, or it never completed; the hub inspects the venue cache)
# The last two stdout lines are `STAGE_NIGHTLY_EXIT code=<n> reason=<TOKEN> sha=<40|none> manifest=<64|none>` and `STAGE_NIGHTLY_RESULT=<compact json>`.
# Every exit after the plan (except -WhatIf and USAGE) appends ONE closing line (schema mlv-app/venue-stage-nightly/v1) to -ReceiptFile, after the STAGE_RESERVED
# line when the call reached a staging step; only STAGE_OK and STAGE_SKIP_ALREADY lines carry a buildManifestSha256.
#
# NEVER: builds in or writes to the canonical checkout (the assembler builds from a git archive under -BuildRoot); deletes anything (retention is the assembler's own
# journal-proven pass); enumerates the share; registers a task or a pending action; launches MLVApp playback (the assembler's launch probe is `--batch --help`).
# Roles come from venues.json and are never set here. Scheduling is NOT here: UM-REGULAR-BASELINE-SCHEDULE-1 makes one call per night (fetch fork, this driver, then
# Invoke-VenueBaseline.ps1 on exit 0).
# -UmRunScript / -DllJobScript / -AssembleScript / -StageJobScript / -SmokeRunnerJobScript / -GitExe / -AgentShare (replaces every share this driver touches) / -ClaimDir /
# -VenueTablePath / -RepoRoot / -GateDir / -NowUtc / -TestNotAfterSampleDelayMs are seams for the offline tests (tools/repo_hygiene/test_venue_stage_nightly.py);
# production passes none.
[CmdletBinding()]
param(
    [ValidateSet('bachelor', 'ultra-magnus')][string]$Venue = 'ultra-magnus',
    [ValidatePattern('^[0-9a-f]{40}$')][string]$SourceCommit = '',
    [string]$BuildRoot = 'C:\mlvtmp\lane-um-nightly-stage',
    [string]$ReceiptFile = '',
    # Total wall-clock cap for everything after the plan. The default fits a cold night: a DLL-pair compile on Ultra-Magnus, a build-guarded assemble and two stages.
    [ValidateRange(10, 28800)][int]$MaxTotalSec = 7200,
    [ValidateRange(1, 300)][int]$ProbeTimeoutSec = 30,
    [switch]$WhatIf,
    [string]$UmRunScript = '',
    [string]$DllJobScript = '',
    [string]$AssembleScript = '',
    [string]$StageJobScript = '',
    [string]$SmokeRunnerJobScript = '',
    [string]$GitExe = 'git',
    [string]$AgentShare = '',
    [string]$ClaimDir = '',
    [string]$VenueTablePath = '',
    [string]$RepoRoot = '',
    [string]$GateDir = '',
    [string]$NowUtc = '',
    # Test-only (LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1): sleeps this long between each submission's remaining-budget sample and its not-after bound,
    # a scheduling delay test_venue_stage_nightly.py injects to prove the bound never passes the deadline. Production passes none.
    [ValidateRange(0, 60000)][int]$TestNotAfterSampleDelayMs = 0
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
Import-Module (Join-Path $here 'VenueChain.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $here 'DualVenueRunner.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $here 'VenueStageReuse.psm1') -Force -DisableNameChecking
Import-Module (Join-Path $here '..\bachelor\AttrCudaArtifacts.psm1') -Force -DisableNameChecking

$ExitCodes = [ordered]@{
    STAGE_OK = 0; STAGE_SKIP_ALREADY = 0; NIGHT_ALREADY_BUILT = 0; PLAN_ONLY = 0; USAGE = 2
    DISK_FLOOR_REFUSED = 3; HOST_COMMIT_REFUSED = 3; BUILD_GUARD_REFUSED = 3
    AGENT_BUSY = 40; SHARE_UNREACHABLE = 41; SOURCE_COMMIT_UNRESOLVED = 42; GATE_UNAVAILABLE = 43; DLLPAIR_FAILED = 44; ASSEMBLE_FAILED = 45
    STAGE_FAILED = 46; STAGE_VERIFY_MISMATCH = 47; SMOKE_RUNNER_STAGE_FAILED = 48; STAGE_UNRESOLVED = 49; STAGE_CAP_EXCEEDED = 50
    RECEIPT_IO_FAILED = 51; NIGHT_RESERVED = 52
}
$Caller = 'Invoke-VenueStageNightly'
$GateCard = 'venue-stage-nightly'
$FloorRecheckAfterSec = 300       # a DLL-pair wait at least this long re-asks both floors right before the assemble
$startedUtc = [DateTime]::UtcNow
$pwshExe = [Environment]::ProcessPath
$reuseModule = Join-Path $here 'VenueStageReuse.psm1'

function Out-Line([string]$Text) { [Console]::Out.WriteLine($Text) }
function Say([string]$Text) { Out-Line ('STAGE_NIGHTLY_LOG {0} {1}' -f [DateTime]::UtcNow.ToString('HH:mm:ssZ'), $Text) }

# ------------------------------------------------------------------ the row every exit after the plan appends ----------------------------------------------------------
$script:Row = [ordered]@{
    schema = 'mlv-app/venue-stage-nightly/v1'; utc = $null; nightKey = $null; venue = $Venue; sourceCommit = $null; outcome = $null; exitCode = $null
    buildManifestSha256 = $null; smokeRunnerCacheDir = $null; jobIds = [ordered]@{ dllPair = $null; stage = $null; smokeRunner = $null }
    steps = [ordered]@{ dllPair = 'not-run'; assemble = 'not-run'; stage = 'not-run'; smokeRunner = 'not-run' }
    timings = [ordered]@{ dllPairSec = $null; assembleSec = $null; stageSec = $null; runnerSec = $null; totalSec = $null }
    cFreeGiB = [ordered]@{ start = $null; end = $null }; commitGiB = $null; buildDir = $null; detail = $null; reservationId = $null; reconciled = @()
}
$script:WriteRow = $false
$script:Mutex = $null
$script:ReservationId = $null

function Open-ReceiptStream([IO.FileMode]$Mode, [IO.FileAccess]$Access, [IO.FileShare]$Share) {
    # A few short retries ride out another opener's moment (sharing violation); anything else, or a violation that outlasts them, THROWS.
    for ($try = 1; ; $try++) {
        try { return [IO.File]::Open($ReceiptFile, $Mode, $Access, $Share) }
        catch [IO.IOException] { if ($try -ge 5) { throw }; Start-Sleep -Milliseconds 200 }
    }
}

function Add-ReceiptLine([string]$Line) {
    # The ONE receipt write. It throws when the line was not appended; every caller fails closed on that.
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ReceiptFile) | Out-Null
    $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Line + "`n")
    $fs = Open-ReceiptStream ([IO.FileMode]::Append) ([IO.FileAccess]::Write) ([IO.FileShare]::Read)
    try { $fs.Write($bytes, 0, $bytes.Length); $fs.Flush($true) } finally { $fs.Dispose() }
}

function Read-ReceiptLines {
    # The receipt's non-blank lines (none when it does not exist yet). An unreadable receipt THROWS: an unknown night never admits.
    if (-not (Test-Path -LiteralPath $ReceiptFile -PathType Leaf)) { return }
    $fs = Open-ReceiptStream ([IO.FileMode]::Open) ([IO.FileAccess]::Read) ([IO.FileShare]::ReadWrite)
    try { $text = [IO.StreamReader]::new($fs, [Text.UTF8Encoding]::new($false)).ReadToEnd() } finally { $fs.Dispose() }
    $text -split "`r?`n" | Where-Object { $_.Trim() }
}

function Format-RowUtc($Value) {
    # ConvertFrom-Json turns the row's ISO utc into a DateTime on PowerShell 7; print it back as the row wrote it.
    if ($Value -isnot [DateTime]) { return [string]$Value }
    $u = $(if ($Value.Kind -eq [DateTimeKind]::Unspecified) { [DateTime]::SpecifyKind($Value, [DateTimeKind]::Utc) } else { $Value.ToUniversalTime() })
    $u.ToString('yyyy-MM-ddTHH:mm:ssZ')
}

function Get-CFree { try { [math]::Round((Get-PSDrive C).Free / 1GB, 1) } catch { $null } }
function Get-CommitGiB {
    try { $os = Get-CimInstance -ClassName Win32_OperatingSystem -OperationTimeoutSec 10 -ErrorAction Stop; [math]::Round(($os.TotalVirtualMemorySize - $os.FreeVirtualMemory) / 1MB, 1) } catch { $null }
}

function Stop-Nightly([string]$Reason, [string]$Detail = '', [string[]]$Verbatim = @(), [string]$Manifest = '') {
    # The ONE exit: the verbatim refusal text (when a gate refused), one detail line, the receipt row, then the two typed lines.
    foreach ($v in $Verbatim) { if ($v) { Out-Line $v } }
    $code = [int]$ExitCodes[$Reason]
    if ($Detail) { Out-Line "STAGE_NIGHTLY_DETAIL $Detail" }
    $r = $script:Row
    $r.utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'); $r.outcome = $Reason; $r.exitCode = $code; $r.detail = $(if ($Detail) { $Detail } else { $null })
    $r.buildManifestSha256 = $(if ($Reason -cin @('STAGE_OK', 'STAGE_SKIP_ALREADY') -and $Manifest) { $Manifest } else { $null })
    $r.timings.totalSec = [math]::Round(([DateTime]::UtcNow - $startedUtc).TotalSeconds, 1)
    if ($script:WriteRow) {
        $r.cFreeGiB.end = Get-CFree
        $r.reservationId = $script:ReservationId
        try { Add-ReceiptLine ($r | ConvertTo-Json -Compress -Depth 6) }
        catch {
            # FAIL CLOSED (CoS Dot b21fb3be): an outcome the receipt does not hold is never reported, least of all as STAGE_OK / 0.
            $lost = $Reason
            $Reason = 'RECEIPT_IO_FAILED'; $code = [int]$ExitCodes[$Reason]
            $held = $(if ($script:ReservationId) { "reservation $($script:ReservationId) for $($r.sourceCommit) is in the receipt and holds night $($r.nightKey)" } else { 'this call persisted no reservation' })
            $msg = "the closing $lost row was not appended to $ReceiptFile ($($_.Exception.Message)); $held"
            Out-Line "STAGE_NIGHTLY_DETAIL $msg"
            $r.outcome = $Reason; $r.exitCode = $code; $r.buildManifestSha256 = $null; $r['unpersistedOutcome'] = $lost
            $r.detail = $(if ($r.detail) { "$msg | $($r.detail)" } else { $msg })
        }
    }
    if ($script:Mutex) { try { $script:Mutex.ReleaseMutex() } catch { }; $script:Mutex.Dispose() }
    $sha = $(if ($r.sourceCommit) { $r.sourceCommit } else { 'none' })
    Out-Line "STAGE_NIGHTLY_EXIT code=$code reason=$Reason sha=$sha manifest=$(if ($r.buildManifestSha256) { $r.buildManifestSha256 } else { 'none' })"
    Out-Line ('STAGE_NIGHTLY_RESULT=' + ($r | ConvertTo-Json -Compress -Depth 6))
    exit $code
}

function Invoke-Bounded([scriptblock]$Script, [object[]]$ArgList, [int]$Sec) {
    # The Invoke-VenueBaseline.ps1 pattern: run a by-name share read on a worker thread and give up after $Sec seconds (a UNC path of a host that is down can block
    # for minutes). A worker still stuck when we give up is abandoned (BeginStop, no wait), never joined.
    $ps = [powershell]::Create()
    [void]$ps.AddScript($Script.ToString())
    foreach ($a in $ArgList) { [void]$ps.AddArgument($a) }
    $async = $ps.BeginInvoke()
    if (-not $async.AsyncWaitHandle.WaitOne([TimeSpan]::FromSeconds($Sec))) {
        try { [void]$ps.BeginStop($null, $null) } catch { }
        return [pscustomobject]@{ ok = $false; timedOut = $true; value = $null; error = "no answer in $Sec s" }
    }
    try { $v = @($ps.EndInvoke($async)); [pscustomobject]@{ ok = $true; timedOut = $false; value = $(if ($v.Count -gt 0) { $v[0] } else { $null }); error = $null } }
    catch { [pscustomobject]@{ ok = $false; timedOut = $false; value = $null; error = $_.Exception.Message } }
    finally { $ps.Dispose() }
}

function Read-Share([string]$What, [scriptblock]$Script, [object[]]$ArgList) {
    # A bounded by-name read; a share that does not answer ends the call SHARE_UNREACHABLE.
    $b = Invoke-Bounded $Script $ArgList $ProbeTimeoutSec
    if ($b.timedOut) { Stop-Nightly 'SHARE_UNREACHABLE' "reading $What : $($b.error)" }
    if (-not $b.ok) { Stop-Nightly 'SHARE_UNREACHABLE' "reading $What failed: $($b.error)" }
    $b.value
}

$HashByName = {
    param($p)
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { return [pscustomobject]@{ present = $false; hash = $null; text = $null } }
    $bytes = [IO.File]::ReadAllBytes($p)
    [pscustomobject]@{ present = $true; hash = [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant(); text = [Text.Encoding]::UTF8.GetString($bytes) }
}
$PathByName = { param($p, $kind) if ($kind -eq 'dir') { Test-Path -LiteralPath $p -PathType Container } else { Test-Path -LiteralPath $p -PathType Leaf } }
$DllReuseByName = { param($mod, $outbox, $id, $sha) Import-Module -Name $mod -Force; Get-DllPairReuse -Outbox $outbox -JobId $id -Sha $sha }
$StageReuseByName = { param($mod, $path, $id, $sha, $kind) Import-Module -Name $mod -Force; Get-StageReuse -ResultPath $path -JobId $id -Sha $sha -Kind $kind }

function Get-RemainingSec { [int][Math]::Floor(($script:Deadline - [DateTime]::UtcNow).TotalSeconds) }

function Invoke-Child([string]$Label, [string]$Script, [string[]]$ArgList, [string]$CommandText = '') {
    # One child process, bounded by what is left of -MaxTotalSec, killed with its tree past it. Its output is kept in the call's work dir.
    # -CommandText runs a script text (-EncodedCommand) instead of -File <Script> <ArgList>.
    $remain = Get-RemainingSec
    if ($remain -le 2) { return [pscustomobject]@{ label = $Label; exit = $null; timedOut = $true; started = $false; out = ''; err = '' } }
    $psi = [Diagnostics.ProcessStartInfo]::new($pwshExe)
    $argv = $(if ($CommandText) { @('-NoProfile', '-NonInteractive', '-EncodedCommand', [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($CommandText))) } else { @('-NoProfile', '-NonInteractive', '-File', $Script) + $ArgList })
    foreach ($a in $argv) { [void]$psi.ArgumentList.Add([string]$a) }
    $psi.UseShellExecute = $false; $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true; $psi.RedirectStandardInput = $true; $psi.CreateNoWindow = $true
    $p = [Diagnostics.Process]::Start($psi)
    $p.StandardInput.Close()
    $outTask = $p.StandardOutput.ReadToEndAsync(); $errTask = $p.StandardError.ReadToEndAsync()
    $timedOut = -not $p.WaitForExit([int][Math]::Max(1000, $remain * 1000))
    if ($timedOut) { try { $p.Kill($true) } catch { }; [void]$p.WaitForExit(15000) }
    [void]$outTask.Wait(15000); [void]$errTask.Wait(15000)
    $out = $(if ($outTask.IsCompleted) { $outTask.Result } else { '' }); $err = $(if ($errTask.IsCompleted) { $errTask.Result } else { '' })
    try { [IO.File]::WriteAllText((Join-Path $script:WorkDir "$Label.log"), "exit=$(if ($timedOut) { 'killed-at-cap' } else { $p.ExitCode })`n$out$(if ($err) { "`n--- stderr ---`n$err" })") } catch { }
    [pscustomobject]@{ label = $Label; exit = $(if ($timedOut) { $null } else { $p.ExitCode }); timedOut = $timedOut; started = $true; out = $out; err = $err }
}

function Get-LastLine([string]$Text, [string]$Pattern) { @($Text -split "`r?`n" | Where-Object { $_ -match $Pattern }) | Select-Object -Last 1 }

# ------------------------------------------------------------------ 1. the plan ----------------------------------------------------------------------------------------
$repo = $(if ($RepoRoot) { $RepoRoot } else { (Resolve-Path (Join-Path $here '..\..\..')).Path })
$mainRoot = Get-VenueMainRoot -From $repo
$tablePath = $(if ($VenueTablePath) { $VenueTablePath } else { Join-Path $here 'venues.json' })
$canonicalReceipt = [IO.Path]::GetFullPath((Join-Path $mainRoot '.claude-state\dual-venue\stage-nightly.jsonl'))
if (-not $ReceiptFile) { $ReceiptFile = $canonicalReceipt }
if (-not $GateDir) { $GateDir = Join-Path $mainRoot '.claude-state\hub-autopilot' }
$profiling = [IO.Path]::GetFullPath((Join-Path $here '..'))
$UmRun = $(if ($UmRunScript) { $UmRunScript } else { Join-Path $profiling 'um-run.ps1' })
$DllGen = $(if ($DllJobScript) { $DllJobScript } else { Join-Path $profiling 'ultramagnus\playback-attr-3-cuda-dll-job.ps1' })
$Assembler = $(if ($AssembleScript) { $AssembleScript } else { Join-Path $profiling 'bachelor\playback-attr-3-cuda-assemble.ps1' })
$StageGen = $(if ($StageJobScript) { $StageJobScript } else { Join-Path $profiling 'bachelor\playback-attr-3-cuda-stage-job.ps1' })
$SmokeGen = $(if ($SmokeRunnerJobScript) { $SmokeRunnerJobScript } else { Join-Path $profiling 'bachelor\attr3-stage-smoke-runner-job.ps1' })

$table = try { Read-DvVenueTable $tablePath } catch { Stop-Nightly 'USAGE' $_.Exception.Message }
$venueEntry = $table.venues.PSObject.Properties[$Venue]
if ($null -eq $venueEntry) { Stop-Nightly 'USAGE' "venues.json has no venue '$Venue'" }
$umEntry = $table.venues.PSObject.Properties['ultra-magnus']
if ($null -eq $umEntry) { Stop-Nightly 'USAGE' "venues.json has no venue 'ultra-magnus' (the DLL pair is always built there)" }
$share = $(if ($AgentShare) { $AgentShare } else { [string]$venueEntry.Value.agentShare }).TrimEnd('\')
$dllShare = $(if ($AgentShare) { $AgentShare } else { [string]$umEntry.Value.agentShare }).TrimEnd('\')
$agentRoot = [string]$venueEntry.Value.agentRoot
if (-not (Test-DvLocalReceiptRoot -Path $ReceiptFile)) { Stop-Nightly 'USAGE' "-ReceiptFile must be under a .claude-state directory (board-local state): $ReceiptFile" }
if (-not $AgentShare -and [IO.Path]::GetFullPath($ReceiptFile) -ine $canonicalReceipt) {
    Stop-Nightly 'USAGE' "the real $Venue share takes only the canonical receipt $canonicalReceipt (the once-per-night reservations live there and nowhere else); -ReceiptFile '$ReceiptFile' is refused"
}
$buildRootFull = [IO.Path]::GetFullPath($BuildRoot).TrimEnd('\')
foreach ($root in @($mainRoot, $repo)) {
    $rf = [IO.Path]::GetFullPath($root).TrimEnd('\')
    if ($buildRootFull -ieq $rf -or $buildRootFull.StartsWith($rf + '\', [StringComparison]::OrdinalIgnoreCase)) { Stop-Nightly 'USAGE' "-BuildRoot '$BuildRoot' is inside the checkout '$rf'; this driver never builds in a checkout" }
}

$now = $(if ($NowUtc) { [DateTime]::Parse($NowUtc, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal) } else { $startedUtc })
$nightEdge = $now.Date.AddHours(8)
if ($now -ge $nightEdge) { $nightEdge = $nightEdge.AddDays(1) }
$nightKey = $nightEdge.ToString('yyyy-MM-dd')
$script:Row.nightKey = $nightKey
$script:WriteRow = -not $WhatIf

$commit = $SourceCommit
if (-not $commit) {
    $ref = (& $GitExe -C $repo rev-parse --verify --quiet 'refs/remotes/fork/master^{commit}' 2>$null)
    if ($LASTEXITCODE -ne 0 -or [string]$ref -cnotmatch '^[0-9a-f]{40}$') { Stop-Nightly 'SOURCE_COMMIT_UNRESOLVED' 'refs/remotes/fork/master does not resolve; pass -SourceCommit <40-hex> or fetch fork first (this driver never fetches)' }
    $commit = [string]$ref
}
$script:Row.sourceCommit = $commit
$names = Get-AttrCudaArtifactNames -SourceCommit $commit
$s12 = $names.shortSha
$cache = "$share\cache"
$cacheBuildJson = Join-Path $cache $names.buildManifestName
$buildDir = Join-Path $BuildRoot "build-$s12"
$dllId = "playback-attr-3-cuda-dllpair-$s12"
$stageDefaultId = "playback-attr-3-cuda-stage-$s12"
# The ONLY leaf names this driver ever hands um-run as side files: this sha's build package (the DLL-pair source zip and the four artifacts the stage job verifies).
$PackageSideNames = @("$dllId-source.zip", $names.packageZipName, $names.exeName, $names.reconName, $names.buildManifestName)

if ($WhatIf) {
    Out-Line "STAGE_NIGHTLY_PLAN venue=$Venue sourceCommit=$commit nightKey=$nightKey agentShare=$share agentRoot=$agentRoot buildRoot=$BuildRoot receiptFile=$ReceiptFile maxTotalSec=$MaxTotalSec (plan only: nothing is built, submitted, probed or written)"
    Out-Line "STAGE_NIGHTLY_PLAN_STEP 1/4 dll-pair harvest $dllShare\outbox\$dllId.artifacts when that single-use JobId already built this sha, else $DllGen -SourceCommit $commit then $UmRun -JobId $dllId -AgentShare $dllShare"
    Out-Line "STAGE_NIGHTLY_PLAN_STEP 2/4 assemble $Assembler -SourceCommit $commit -DllPairDir <the DLL-pair artifacts> -OutDir $buildDir (build-guard; skipped when the cache already holds this sha)"
    Out-Line "STAGE_NIGHTLY_PLAN_STEP 3/4 stage $StageGen -AgentRoot $agentRoot [-JobIdSuffix n<yyyyMMddHHmm> when $stageDefaultId is spent and the cache does not hold the manifest] then $UmRun -AgentShare $share; verify $cacheBuildJson by name"
    Out-Line "STAGE_NIGHTLY_PLAN_STEP 4/4 smoke-runner $SmokeGen -AgentRoot $agentRoot then $UmRun -AgentShare $share (skipped when $cache\<CACHE_DIR_NAME> exists)"
    Out-Line "STAGE_NIGHTLY_PLAN_GATES $GateDir\disk-floor.ps1 $GateDir\host-commit-gate.ps1; once per night: $ReceiptFile"
    Stop-Nightly 'PLAN_ONLY'
}

# ------------------------------------------------------------------ 2. once per night, one driver at a time --------------------------------------------------------------
$script:Deadline = $startedUtc.AddSeconds($MaxTotalSec)
$script:Row.cFreeGiB.start = Get-CFree
$script:Row.commitGiB = Get-CommitGiB
# One driver per VENUE. No caller-selected path is in the name (CoS Dot b21fb3be): two -ReceiptFile values can no longer stage one venue side by side. Only the
# offline -AgentShare seam adds a hash of that share, so a temp-dir test never contends with a real nightly on the board host (production passes no -AgentShare).
$seamTag = $(if ($AgentShare) { '-seam-' + [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes(([IO.Path]::GetFullPath($share)).ToLowerInvariant()))).Substring(0, 12).ToLowerInvariant() } else { '' })
$mutexName = 'Global\MLV-VenueStageNightly-' + $Venue + $seamTag
$mtx = [Threading.Mutex]::new($false, $mutexName)
$got = $false
try { $got = $mtx.WaitOne(0) } catch [Threading.AbandonedMutexException] { $got = $true }
if (-not $got) { $mtx.Dispose(); Stop-Nightly 'AGENT_BUSY' "another Invoke-VenueStageNightly for $Venue holds $mutexName" }
$script:Mutex = $mtx

# Once per night counts RESERVATIONS, not only successes: a STAGE_RESERVED row holds its night for its sha whether its call ended STAGE_OK, failed, or never
# wrote its closing row. A reservation no row closes is reconciled here (listed in this call's row); it still holds the night unless it is this sha's, which resumes.
$receiptLines = try { @(Read-ReceiptLines) } catch { Stop-Nightly 'RECEIPT_IO_FAILED' "the receipt $ReceiptFile could not be read for the once-per-night check ($($_.Exception.Message)); an unknown night never admits" }
$reservations = [ordered]@{}
$built = $null
$n = 0
foreach ($line in $receiptLines) {
    $n++
    $prev = try { $line | ConvertFrom-Json } catch { $null }
    if ($null -eq $prev -or [string]$prev.nightKey -cne $nightKey -or [string]$prev.venue -cne $Venue) { continue }
    $rid = [string]$prev.reservationId
    if ([string]$prev.outcome -ceq 'STAGE_RESERVED') {
        if (-not $rid) { $rid = "unnamed-line-$n" }
        $reservations[$rid] = [ordered]@{ reservationId = $rid; sourceCommit = [string]$prev.sourceCommit; utc = (Format-RowUtc $prev.utc); endedAs = $null }
    } elseif ($rid -and $reservations.Contains($rid)) {
        $reservations[$rid].endedAs = [string]$prev.outcome
    }
    if ([string]$prev.outcome -ceq 'STAGE_OK' -and [string]$prev.sourceCommit -cne $commit -and -not $built) { $built = $prev }
}
$script:Row.reconciled = @(foreach ($x in @($reservations.Values | Where-Object { -not $_.endedAs })) {
    [ordered]@{ reservationId = $x.reservationId; sourceCommit = $x.sourceCommit; utc = $x.utc; state = 'incomplete' }
})
if ($built) {
    Stop-Nightly 'NIGHT_ALREADY_BUILT' "night $nightKey already staged $([string]$built.sourceCommit) on $Venue at $(Format-RowUtc $built.utc); at most one build per night"
}
$holder = @($reservations.Values | Where-Object { $_.sourceCommit -cne $commit }) | Select-Object -First 1
if ($holder) {
    $how = $(if ($holder.endedAs) { "ended $($holder.endedAs)" } else { 'never completed (no row closes it: its call died, or its closing row was lost)' })
    Stop-Nightly 'NIGHT_RESERVED' "night $nightKey on $Venue is held by reservation $($holder.reservationId) for $($holder.sourceCommit) at $($holder.utc), which $how; at most one commit per night, so $commit is not staged tonight (the hub inspects the $Venue cache)"
}
if ($script:Row.reconciled.Count -gt 0) { Say "resuming $commit over its incomplete reservation(s) $(($script:Row.reconciled | ForEach-Object { $_.reservationId }) -join ', ')" }

# ------------------------------------------------------------------ 3. the venue is free and the share answers ------------------------------------------------------------
function Assert-VenueFree([string]$Before) {
    $status = Get-VenueClaimStatus -Venue $Venue -ClaimDir $ClaimDir
    $liveTickets = @($status.tickets | Where-Object { $_.state -cne 'STALE' })
    if ($status.claim.state -cin @('LIVE', 'BUSY') -or $liveTickets.Count -gt 0) {
        $holder = $(if ($status.claim.claim) { "pid=$($status.claim.claim.ownerPid) runDir=$($status.claim.claim.runDir)" } else { "claimState=$($status.claim.state)" })
        Stop-Nightly 'AGENT_BUSY' "the $Venue agent is in use ($holder liveTickets=$($liveTickets.Count)) $Before; this driver never builds or submits over another lane's chain"
    }
}
Assert-VenueFree 'before anything was built'
$reach = Invoke-Bounded { param($p) Test-Path -LiteralPath $p -PathType Container } @($cache) $ProbeTimeoutSec
if (-not $reach.ok -or $reach.value -ne $true) { Stop-Nightly 'SHARE_UNREACHABLE' "the agent share cache '$cache' did not answer as a directory ($(if ($reach.error) { $reach.error } else { 'not found' }))" }

# ------------------------------------------------------------------ 4. idempotency: what the cache already holds, by name --------------------------------------------------
$script:WorkDir = Join-Path $BuildRoot ('runs\' + $startedUtc.ToString('yyyyMMddTHHmmssZ') + "-$s12")
New-Item -ItemType Directory -Force -Path $script:WorkDir | Out-Null
Say "work dir $($script:WorkDir); venue $Venue share $share; sha $commit; night $nightKey"
$smokeOut = Join-Path $script:WorkDir 'smoke'
$g = Invoke-Child 'smoke-runner-gen' $SmokeGen @('-SourceCommit', $commit, '-OutDir', $smokeOut, '-AgentRoot', $agentRoot)
if ($g.timedOut) { Stop-Nightly 'STAGE_CAP_EXCEEDED' 'the smoke-runner generator outlived the cap' }
$sm = [regex]::Match($g.out, '(?m)^RESULT=SMOKE_RUNNER_STAGE_JOB_EMITTED .*CACHE_DIR_NAME=(smoke-runner-[0-9a-f]{16}) JOB=(.+?\.job\.ps1)\s*$')
if ($g.exit -ne 0 -or -not $sm.Success) { Stop-Nightly 'SMOKE_RUNNER_STAGE_FAILED' "the smoke-runner generator exit $($g.exit) printed no CACHE_DIR_NAME (see $($script:WorkDir)\smoke-runner-gen.log)" }
$runnerDirName = $sm.Groups[1].Value
$runnerJob = $sm.Groups[2].Value
$runnerId = [IO.Path]::GetFileName($runnerJob) -replace '\.job\.ps1$', ''
$script:Row.smokeRunnerCacheDir = $runnerDirName

$cached = Read-Share $cacheBuildJson $HashByName @($cacheBuildJson)
$cachedWellFormed = $false
if ($cached.present) {
    $cj = try { $cached.text | ConvertFrom-Json } catch { $null }
    $cachedWellFormed = ($null -ne $cj) -and ([string]$cj.sourceCommit -ceq $commit)
}
$missingArtifacts = @(foreach ($n in @($names.exeName, $names.reconName, $names.packageZipName)) { if (-not (Read-Share "$cache\$n" $PathByName @((Join-Path $cache $n), 'file'))) { $n } })
$runnerPresent = [bool](Read-Share "$cache\$runnerDirName" $PathByName @((Join-Path $cache $runnerDirName), 'dir'))
$buildInCache = $cachedWellFormed -and $missingArtifacts.Count -eq 0
if ($buildInCache -and $runnerPresent) {
    $script:Row.steps.dllPair = 'skipped'; $script:Row.steps.assemble = 'skipped'; $script:Row.steps.stage = 'in-cache'; $script:Row.steps.smokeRunner = 'in-cache'
    Stop-Nightly 'STAGE_SKIP_ALREADY' "$cacheBuildJson (sourceCommit $commit) and its three artifacts and $runnerDirName are already in the $Venue cache; nothing was built or submitted" -Manifest $cached.hash
}
if ($cachedWellFormed -and $missingArtifacts.Count -gt 0) {
    Stop-Nightly 'STAGE_FAILED' "the $Venue cache holds $($names.buildManifestName) for $commit but not $($missingArtifacts -join ', '); not re-assembled (a re-assemble makes a new manifest); the hub inspects the cache"
}

# ------------------------------------------------------------------ 5. the floors (the shared board gate scripts, never copied) ---------------------------------------------
$gateFiles = @((Join-Path $GateDir 'disk-floor.ps1'), (Join-Path $GateDir 'host-commit-gate.ps1'))
foreach ($gf in $gateFiles) { if (-not (Test-Path -LiteralPath $gf -PathType Leaf)) { Stop-Nightly 'GATE_UNAVAILABLE' "gate script missing: $gf (fail closed: a missing gate never admits)" } }
try { . $gateFiles[0]; . $gateFiles[1] } catch { Stop-Nightly 'GATE_UNAVAILABLE' "a gate script did not load: $($_.Exception.Message)" }
foreach ($fn in 'Test-DiskFloor', 'Test-HostCommitGate') { if (-not (Get-Command $fn -CommandType Function -ErrorAction SilentlyContinue)) { Stop-Nightly 'GATE_UNAVAILABLE' "the gate scripts in $GateDir define no $fn" } }

function Assert-Floors([string]$When) {
    $d = try { Test-DiskFloor -Caller $Caller -Card $GateCard } catch { Stop-Nightly 'GATE_UNAVAILABLE' "Test-DiskFloor threw $When : $($_.Exception.Message)" }
    if (-not $d.Ok) { Stop-Nightly 'DISK_FLOOR_REFUSED' "the disk floor refused $When" -Verbatim @([string]$d.Message) }
    $c = try { Test-HostCommitGate -Caller $Caller -Card $GateCard } catch { Stop-Nightly 'GATE_UNAVAILABLE' "Test-HostCommitGate threw $When : $($_.Exception.Message)" }
    if (Get-Command Write-HostCommitGateLog -CommandType Function -ErrorAction SilentlyContinue) { Write-HostCommitGateLog $c }
    if (-not $c.Ok) { Stop-Nightly 'HOST_COMMIT_REFUSED' "the host commit ceiling refused $When" -Verbatim @([string]$c.Message) }
    if ([string]$c.Code -ceq 'HOST_COMMIT_PROBE_FAILED') { Out-Line ([string]$c.Message) }
    if ($c.PSObject.Properties['CommitGiB'] -and [double]$c.CommitGiB -ge 0) { $script:Row.commitGiB = [double]$c.CommitGiB }
    Say "floors $When : $($d.Message) | $($c.Message)"
}
Assert-Floors 'before the build'

# ------------------------------------------------------------------ 5b. the reservation: persisted (and read back) BEFORE the first staging step ------------------------
# From here on the venue cache can change, so the night is reserved first. A reservation that is not in the receipt afterwards is RECEIPT_IO_FAILED and nothing is staged.
$rid = [guid]::NewGuid().ToString('N')
$reservation = [ordered]@{
    schema = 'mlv-app/venue-stage-nightly/v1'; utc = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'); nightKey = $nightKey; venue = $Venue; sourceCommit = $commit
    outcome = 'STAGE_RESERVED'; exitCode = $null; reservationId = $rid; resumes = @($script:Row.reconciled | ForEach-Object { $_.reservationId })
}
$why = 'not found when the receipt was read back'
$persisted = $false
try {
    Add-ReceiptLine ($reservation | ConvertTo-Json -Compress -Depth 4)
    $persisted = @(Read-ReceiptLines | Where-Object { $_.Contains('"reservationId":"' + $rid + '"') }).Count -eq 1
} catch { $why = $_.Exception.Message }
if (-not $persisted) { Stop-Nightly 'RECEIPT_IO_FAILED' "the reservation of night $nightKey on $Venue for $commit could not be persisted to $ReceiptFile ($why); staging did not start" }
$script:ReservationId = $rid
Say "reserved night $nightKey on $Venue for $commit (reservation $rid)"

# ------------------------------------------------------------------ um-run, classified ------------------------------------------------------------------------------------
function Assert-StagePackageSideFile([string[]]$Side, [string]$JobId, [string]$FailToken) {
    # The owner-footage gate (test_dual_venue_owner_footage_gate.py): a side file is copied through the venue share, so this driver hands um-run ONLY this sha's
    # build package from under -BuildRoot. Any other leaf name, or a package name outside -BuildRoot, is refused before um-run is called. This driver takes no clip.
    foreach ($s in @($Side)) {
        $full = [IO.Path]::GetFullPath([string]$s)
        if ([IO.Path]::GetFileName($full) -cnotin $PackageSideNames -or -not $full.StartsWith($buildRootFull + '\', [StringComparison]::OrdinalIgnoreCase)) {
            Stop-Nightly $FailToken "refused to hand um-run the side file '$s' for $JobId : only this sha's build package under $BuildRoot travels as a side file; nothing was submitted"
        }
    }
}

function Invoke-UmRun([string]$Step, [string]$JobFile, [string[]]$Side, [string]$JobId, [string]$Share, [int]$JobTimeoutSec, [string]$FailToken, [switch]$LaterHarvest) {
    # Returns 'OK' (um-run returned; the caller reads the outbox result BY NAME) or 'IN_USE' (the outbox already holds this JobId). Every other end is a typed exit:
    # UNRESOLVED (or killed at the cap) -> STAGE_UNRESOLVED, never retried here; RETRACTED (never claimed) -> AGENT_BUSY; no heartbeat -> SHARE_UNREACHABLE.
    # LEASE-NOT-AFTER-SINGLE-LEG-SUBMITTERS-1: the job's -NotAfterUtc is this call's deadline (um-run is killed at it: the helper's -CallerDeadlineUtc), except
    # for -LaterHarvest: a job a LATER call harvests by name and cannot re-submit (single-use JobId, no suffix) gets none.
    Assert-VenueFree "before the $Step submission"
    Assert-StagePackageSideFile -Side $Side -JobId $JobId -FailToken $FailToken
    # LEASE-NOT-AFTER-SUBMITTER-DEADLINE-TIMING-1: the waits are sized from a SAMPLE of what is left ($remain), but the bound is the ORIGINAL deadline
    # (-CallerDeadlineUtc $script:Deadline), never $remain counted again from the helper's later now: a delay between the sample and the call can then only
    # shorten what um-run is given, never move the bound past the deadline. ($remain still proves the waits fit: -CallerCapSec.) Invoke-Child kills um-run
    # at the cap from its own sample just before the child starts, so the kill can land up to the child's start-up time after the deadline; a job the
    # agent refuses in that sub-second window was going to be cut off anyway.
    $remain = Get-RemainingSec
    if ($remain -lt 30) { Stop-Nightly 'STAGE_CAP_EXCEEDED' "only $remain s of the $MaxTotalSec s cap left before the $Step submission; nothing was submitted" }
    $timeoutSec = [Math]::Min($JobTimeoutSec, $remain); $admissionSec = [Math]::Min(7200, $remain)
    if ($TestNotAfterSampleDelayMs -gt 0) { Start-Sleep -Milliseconds $TestNotAfterSampleDelayMs }
    # r3 (STAGE-NOTAFTER-THROW-TYPED-EXIT-1): a deadline that passes between the sample and the bound is the cap running out, so it is the typed cap exit,
    # never an untyped throw out of the driver.
    try {
        $notAfter = Get-DvJobNotAfter -TimeoutSec $timeoutSec -MaxQueueWaitSec $remain -MaxAdmissionWaitSec $admissionSec -MaxClaimedWaitSec $remain -CallerCapSec $remain -CallerDeadlineUtc $script:Deadline -LaterHarvest:$LaterHarvest
    } catch {
        if ([string]$_.Exception.Message -match '^DVE_NOT_AFTER_DEADLINE_PASSED') { Stop-Nightly 'STAGE_CAP_EXCEEDED' "the $MaxTotalSec s deadline passed before the $Step submission's bound was computed; nothing was submitted ($($_.Exception.Message))" }
        throw
    }
    # um-run.ps1 reports every non-receipt end by THROWING; pwsh's own error view wraps and decorates that text, so the child catches it and prints the message
    # on ONE line (UMRUN_ERROR <message>). The returned result object is discarded: the outbox result.json is read by name afterwards.
    $lit = { param($s) "'" + ([string]$s).Replace("'", "''") + "'" }
    $call = '& ' + (& $lit $UmRun) + ' -ScriptPath ' + (& $lit $JobFile) + ' -JobId ' + (& $lit $JobId) + ' -AgentShare ' + (& $lit $Share) +
        " -TimeoutSec $timeoutSec -MaxQueueWaitSec $remain -MaxAdmissionWaitSec $admissionSec -MaxClaimedWaitSec $remain"
    # r2 (hub ruling on sol's r1 blocker): the bound is passed DROP-RELATIVE, capped by the ORIGINAL deadline: um-run's drop writes min(its own now +
    # budgetSec, $script:Deadline), so a slow start or side-file share before the drop never leaves the bound short of the waits, and never past the deadline.
    if ($null -ne $notAfter.budgetSec) {
        $call += " -NotAfterBudgetSec $($notAfter.budgetSec)"
        if ($notAfter.capUtc) { $call += ' -NotAfterCapUtc ' + (& $lit $notAfter.capUtc) }
    } elseif ($notAfter.notAfterUtc) { $call += ' -NotAfterUtc ' + (& $lit $notAfter.notAfterUtc) }
    if (@($Side).Count -gt 0) { $call += ' -SideFile @(' + ((@($Side) | ForEach-Object { & $lit $_ }) -join ', ') + ')' }
    $text = "try { `$null = $call; [Console]::Out.WriteLine('UMRUN_RETURNED'); exit 0 } catch { [Console]::Out.WriteLine('UMRUN_ERROR ' + (`$_.Exception.Message -replace '\s+', ' ')); exit 1 }"
    $bound = $(if ($null -ne $notAfter.budgetSec) { "drop+$($notAfter.budgetSec)s cap=$(if ($notAfter.capUtc) { $notAfter.capUtc } else { 'none' })" } elseif ($notAfter.notAfterUtc) { $notAfter.notAfterUtc } else { 'none' })
    Say "submit $Step $JobId -> $Share notAfter=$bound ($($notAfter.source)) deadline=$($script:Deadline.ToString('o'))"
    $c = Invoke-Child "umrun-$Step" '' @() -CommandText $text
    if ($c.timedOut) { Stop-Nightly 'STAGE_UNRESOLVED' "um-run for $JobId was still waiting at the $MaxTotalSec s cap and was killed; the agent may still own $JobId (not retried)" }
    if ($c.exit -eq 0 -and $c.out -match '(?m)^UMRUN_RETURNED\s*$') { return 'OK' }
    $errLine = Get-LastLine $c.out '^UMRUN_ERROR '
    $msg = $(if ($errLine) { $errLine.Substring('UMRUN_ERROR '.Length).Trim() } else { '' })
    if (Test-UmRunJobIdInUseMessage -Message $msg -JobId $JobId) { return 'IN_USE' }
    if ($msg -match '^UNRESOLVED:') { Stop-Nightly 'STAGE_UNRESOLVED' "um-run $Step ($JobId): $msg (not retried in this call)" }
    if ($msg -match '^RETRACTED:') { Stop-Nightly 'AGENT_BUSY' "um-run $Step ($JobId) was never claimed: $msg" }
    if ($msg -match '^No agent heartbeat') { Stop-Nightly 'SHARE_UNREACHABLE' "um-run $Step : $msg" }
    # r2: the drop refuses a cap (this call's deadline) that passed before its claim, before any share write; r3/r4: or during placement, before the job's
    # rename. r5 (Grok G5): the text says what holds in every case: the job was never made visible.
    if ($msg -match '^UMRUN_NOT_AFTER_CAP_PASSED') { Stop-Nightly 'STAGE_CAP_EXCEEDED' "um-run $Step ($JobId): the $MaxTotalSec s deadline passed before the job was made visible; nothing was submitted ($msg)" }
    # r5 (Grok G5): r4's placement-stall refusal means the share stalled through every re-derivation and the drop rolled its claim back, the job never visible.
    # That is the share not answering in time with nothing submitted, so SHARE_UNREACHABLE, never the step's generic failure.
    if ($msg -match '^UMRUN_NOT_AFTER_PLACEMENT_STALL') { Stop-Nightly 'SHARE_UNREACHABLE' "um-run $Step ($JobId): the share stalled while the job was being placed; the job was never made visible, nothing was submitted ($msg)" }
    $last = $(if ($msg) { $msg } else { Get-LastLine ($c.out + "`n" + $c.err) '\S' })
    Stop-Nightly $FailToken "um-run $Step ($JobId) exit $($c.exit): $(if ($last) { ([string]$last).Trim() })"
}

# ------------------------------------------------------------------ 6. DLL pair (Ultra-Magnus) ----------------------------------------------------------------------------
$expected = $null                       # the manifest sha256 the stage must leave in the cache
if ($buildInCache) {
    # the build is in the cache but its runner is not: no build, no stage; only the runner is staged below
    $script:Row.steps.dllPair = 'skipped'; $script:Row.steps.assemble = 'skipped'; $script:Row.steps.stage = 'in-cache'
    $expected = $cached.hash
    $script:Row.jobIds.stage = $null
} else {
    $t0 = [DateTime]::UtcNow
    $script:Row.jobIds.dllPair = $dllId
    $dllOutbox = "$dllShare\outbox"
    $pair = Read-Share "$dllOutbox\$dllId.result.json" $DllReuseByName @($reuseModule, $dllOutbox, $dllId, $commit)
    if ($pair.Reusable) {
        $script:Row.steps.dllPair = 'harvested'
        Say "DLL pair harvested: $($pair.Line) (ended $($pair.EndedUtc)); not re-submitted"
    } elseif ($pair.Reason -cne 'result-missing') {
        Stop-Nightly 'DLLPAIR_FAILED' "the single-use JobId $dllId already has a result that is not a clean DLL pair for $commit ($($pair.Reason)); not re-submitted"
    } else {
        $dllOut = Join-Path $script:WorkDir 'dllpair'
        $gen = Invoke-Child 'dllpair-gen' $DllGen @('-SourceCommit', $commit, '-OutDir', $dllOut)
        if ($gen.timedOut) { Stop-Nightly 'STAGE_CAP_EXCEEDED' 'the DLL-pair generator outlived the cap' }
        $dllJob = Join-Path $dllOut "$dllId.job.ps1"; $dllZip = Join-Path $dllOut "$dllId-source.zip"
        if ($gen.exit -ne 0 -or -not (Test-Path -LiteralPath $dllJob -PathType Leaf) -or -not (Test-Path -LiteralPath $dllZip -PathType Leaf)) { Stop-Nightly 'DLLPAIR_FAILED' "the DLL-pair generator exit $($gen.exit) did not emit $dllId.job.ps1 + -source.zip (see $($script:WorkDir)\dllpair-gen.log)" }
        $u = Invoke-UmRun 'dllpair' $dllJob @($dllZip) $dllId $dllShare 3600 'DLLPAIR_FAILED' -LaterHarvest
        if ($u -ceq 'IN_USE') { Stop-Nightly 'DLLPAIR_FAILED' "$dllId became in use between the by-name check and the submission; not re-submitted" }
        $pair = Read-Share "$dllOutbox\$dllId.result.json" $DllReuseByName @($reuseModule, $dllOutbox, $dllId, $commit)
        if (-not $pair.Reusable) { Stop-Nightly 'DLLPAIR_FAILED' "the DLL-pair job ended but its result is not a clean pair for $commit ($($pair.Reason))" }
        $script:Row.steps.dllPair = 'built'
        Say "DLL pair built: $($pair.Line)"
    }
    $dllDir = [string]$pair.ArtifactsDir
    $script:Row.timings.dllPairSec = [math]::Round(([DateTime]::UtcNow - $t0).TotalSeconds, 1)
    if ($script:Row.timings.dllPairSec -ge $FloorRecheckAfterSec) { Assert-Floors "after a $($script:Row.timings.dllPairSec) s DLL-pair wait, before the assemble" }

    # -------------------------------------------------------------- 7. assemble (this host, build-guard) --------------------------------------------------------------
    $t0 = [DateTime]::UtcNow
    $localJson = Join-Path $buildDir $names.buildManifestName
    $local = $null
    if (Test-Path -LiteralPath $localJson -PathType Leaf) {
        $lj = try { [IO.File]::ReadAllText($localJson) | ConvertFrom-Json } catch { $null }
        $allLocal = @($names.exeName, $names.reconName, $names.packageZipName | Where-Object { -not (Test-Path -LiteralPath (Join-Path $buildDir $_) -PathType Leaf) }).Count -eq 0
        if ($lj -and [string]$lj.sourceCommit -ceq $commit -and $allLocal) { $local = (Get-FileHash -LiteralPath $localJson -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
    if ($local) {
        # an earlier call assembled this sha here but never got it into the cache: stage those bytes, do not assemble again
        $expected = $local
        $script:Row.steps.assemble = 'local-build-present'
        Say "assemble not needed: $localJson already holds $commit (sha256 $local)"
    } else {
        if (Test-Path -LiteralPath $buildDir) { $buildDir = Join-Path $BuildRoot ("build-$s12-r" + [DateTime]::UtcNow.ToString('yyyyMMddHHmmss')) }
        Say "assemble $commit -> $buildDir (build-guard)"
        $a = Invoke-Child 'assemble' $Assembler @('-SourceCommit', $commit, '-DllPairDir', $dllDir, '-OutDir', $buildDir)
        if ($a.timedOut) { Stop-Nightly 'STAGE_CAP_EXCEEDED' "the assembler was still running at the $MaxTotalSec s cap and was killed (see $($script:WorkDir)\assemble.log)" }
        # A machine heavy-slot REQUEUE (heavy-slot code 75/76/77, or its holder gone before the action) is a capacity refusal
        # like the other two, never an assembly failure (MLV-VM-HEAVY-SLOT-ADOPT-1 r13, sol r12 B1).
        $guard = @($a.out -split "`r?`n" | Where-Object { $_ -match '^(BUILD_MEM_REFUSED|BUILD_SLOT_BUSY|BUILD_HEAVY_SLOT_REQUEUE)\b' })
        if ($a.exit -eq 3 -and $guard.Count -gt 0) { Stop-Nightly 'BUILD_GUARD_REFUSED' 'the build guard refused the assemble' -Verbatim $guard }
        $ok = [regex]::Match($a.out, '(?m)^RESULT=ASSEMBLE_OK SOURCE=([0-9a-f]{40}) .*MANIFEST_SHA256=([0-9a-f]{64})')
        if ($a.exit -ne 0 -or -not $ok.Success -or $ok.Groups[1].Value -cne $commit) {
            $fail = Get-LastLine $a.out '^RESULT=ASSEMBLE_FAILED'
            Stop-Nightly 'ASSEMBLE_FAILED' "the assembler exit $($a.exit) $(if ($fail) { $fail.Trim() } else { 'printed no RESULT=ASSEMBLE_OK for this sha' }) (see $($script:WorkDir)\assemble.log)"
        }
        $expected = $ok.Groups[2].Value
        $lh = $(if (Test-Path -LiteralPath (Join-Path $buildDir $names.buildManifestName) -PathType Leaf) { (Get-FileHash -LiteralPath (Join-Path $buildDir $names.buildManifestName) -Algorithm SHA256).Hash.ToLowerInvariant() } else { '' })
        if ($lh -cne $expected) { Stop-Nightly 'ASSEMBLE_FAILED' "the assembler printed MANIFEST_SHA256=$expected but $buildDir holds $(if ($lh) { $lh } else { 'no build.json' })" }
        $script:Row.steps.assemble = 'built'
    }
    $script:Row.buildDir = $buildDir
    $script:Row.timings.assembleSec = [math]::Round(([DateTime]::UtcNow - $t0).TotalSeconds, 1)

    # -------------------------------------------------------------- 8. stage (suffix when the default JobId is spent and the cache does not match) ---------------------
    $t0 = [DateTime]::UtcNow
    $suffix = ''
    $submit = $true
    if (Read-Share "$share\outbox\$stageDefaultId.result.json" $PathByName @("$share\outbox\$stageDefaultId.result.json", 'file')) {
        $prior = Read-Share "$share\outbox\$stageDefaultId.result.json" $StageReuseByName @($reuseModule, "$share\outbox\$stageDefaultId.result.json", $stageDefaultId, $commit, 'Stage')
        $now2 = Read-Share $cacheBuildJson $HashByName @($cacheBuildJson)
        if ($now2.present -and $now2.hash -ceq $expected) {
            $submit = $false
            $script:Row.steps.stage = 'cache-holds-expected'
            $script:Row.jobIds.stage = $stageDefaultId
            Say "the cache already holds manifest $expected (verified by name); $stageDefaultId not submitted"
        } else {
            $suffix = 'n' + [DateTime]::UtcNow.ToString('yyyyMMddHHmm')
            Say "default stage JobId $stageDefaultId is spent (its result: $($prior.Reason)) but the cache holds $(if ($now2.present) { $now2.hash } else { 'NO build.json' }), not $expected; re-staging under $stageDefaultId-$suffix (an earlier STAGE_OK is never taken as proof of the cache)"
        }
    }
    $attempt = 0
    while ($submit) {
        $attempt++
        $stageId = $(if ($suffix) { "$stageDefaultId-$suffix" } else { $stageDefaultId })
        $stageOut = Join-Path $script:WorkDir "stage-$attempt"
        $sgArgs = @('-SourceCommit', $commit, '-BuildDir', $buildDir, '-OutDir', $stageOut, '-AgentRoot', $agentRoot)
        if ($suffix) { $sgArgs += @('-JobIdSuffix', $suffix) }
        $sg = Invoke-Child "stage-gen-$attempt" $StageGen $sgArgs
        if ($sg.timedOut) { Stop-Nightly 'STAGE_CAP_EXCEEDED' 'the stage generator outlived the cap' }
        $stageJob = Join-Path $stageOut "$stageId.job.ps1"
        if ($sg.exit -ne 0 -or -not (Test-Path -LiteralPath $stageJob -PathType Leaf)) { Stop-Nightly 'STAGE_FAILED' "the stage generator exit $($sg.exit) did not emit $stageId.job.ps1 (see $($script:WorkDir)\stage-gen-$attempt.log)" }
        $sides = @($names.packageZipName, $names.exeName, $names.reconName, $names.buildManifestName | ForEach-Object { Join-Path $buildDir $_ })
        $script:Row.jobIds.stage = $stageId
        $u = Invoke-UmRun 'stage' $stageJob $sides $stageId $share 3600 'STAGE_FAILED'
        if ($u -ceq 'IN_USE') {
            if (-not $suffix -and $attempt -eq 1) { $suffix = 'n' + [DateTime]::UtcNow.ToString('yyyyMMddHHmm'); Say "$stageDefaultId turned out spent at submission; re-staging under $stageDefaultId-$suffix"; continue }
            Stop-Nightly 'STAGE_FAILED' "$stageId is already in use; not re-submitted"
        }
        $sr = Read-Share "$share\outbox\$stageId.result.json" $StageReuseByName @($reuseModule, "$share\outbox\$stageId.result.json", $stageId, $commit, 'Stage')
        if (-not $sr.Staged) { Stop-Nightly 'STAGE_FAILED' "the stage job $stageId ended without RESULT=STAGE_OK for $commit ($($sr.Reason))" }
        $script:Row.steps.stage = $(if ($suffix) { 'staged-suffix' } else { 'staged' })
        Say "stage result: $($sr.Line)"
        $submit = $false
    }
    $v = Read-Share $cacheBuildJson $HashByName @($cacheBuildJson)
    if (-not $v.present -or $v.hash -cne $expected) {
        Stop-Nightly 'STAGE_VERIFY_MISMATCH' "after STAGE_OK the cache $cacheBuildJson hashes $(if ($v.present) { $v.hash } else { 'MISSING' }), not the assembled $expected; never recorded as staged"
    }
    $script:Row.timings.stageSec = [math]::Round(([DateTime]::UtcNow - $t0).TotalSeconds, 1)
}

# ------------------------------------------------------------------ 9. smoke runner ----------------------------------------------------------------------------------------
$t0 = [DateTime]::UtcNow
$script:Row.jobIds.smokeRunner = $runnerId
if ($runnerPresent) {
    $script:Row.steps.smokeRunner = 'in-cache'
} else {
    $runnerResult = "$share\outbox\$runnerId.result.json"
    if (Read-Share $runnerResult $PathByName @($runnerResult, 'file')) {
        $prior = Read-Share $runnerResult $StageReuseByName @($reuseModule, $runnerResult, $runnerId, $commit, 'SmokeRunner')
        if (-not (Read-Share "$cache\$runnerDirName" $PathByName @((Join-Path $cache $runnerDirName), 'dir'))) {
            Stop-Nightly 'SMOKE_RUNNER_STAGE_FAILED' "the single-use JobId $runnerId is spent ($($prior.Reason)) but $runnerDirName is not in the $Venue cache; the runner generator has no suffix, so the hub re-stages it"
        }
        $script:Row.steps.smokeRunner = 'in-cache'
    } else {
        $u = Invoke-UmRun 'smoke-runner' $runnerJob @() $runnerId $share 1800 'SMOKE_RUNNER_STAGE_FAILED' -LaterHarvest
        if ($u -ceq 'IN_USE') { Stop-Nightly 'SMOKE_RUNNER_STAGE_FAILED' "$runnerId became in use between the by-name check and the submission; not re-submitted" }
        $sr = Read-Share $runnerResult $StageReuseByName @($reuseModule, $runnerResult, $runnerId, $commit, 'SmokeRunner')
        if (-not $sr.Staged) { Stop-Nightly 'SMOKE_RUNNER_STAGE_FAILED' "the smoke-runner job $runnerId ended without RESULT=SMOKE_RUNNER_STAGE_OK ($($sr.Reason))" }
        if (-not (Read-Share "$cache\$runnerDirName" $PathByName @((Join-Path $cache $runnerDirName), 'dir'))) { Stop-Nightly 'SMOKE_RUNNER_STAGE_FAILED' "$runnerId reported $($sr.Line) but $runnerDirName is not in the $Venue cache" }
        $script:Row.steps.smokeRunner = 'staged'
        Say "smoke-runner result: $($sr.Line)"
    }
}
$script:Row.timings.runnerSec = [math]::Round(([DateTime]::UtcNow - $t0).TotalSeconds, 1)

# ------------------------------------------------------------------ 10. the by-name verify the receipt records --------------------------------------------------------------
$final = Read-Share $cacheBuildJson $HashByName @($cacheBuildJson)
if (-not $final.present -or $final.hash -cne $expected) {
    Stop-Nightly 'STAGE_VERIFY_MISMATCH' "the final by-name read of $cacheBuildJson hashes $(if ($final.present) { $final.hash } else { 'MISSING' }), not $expected; never recorded as staged"
}
Stop-Nightly 'STAGE_OK' "$cacheBuildJson sha256 $($final.hash) (read by name) and $runnerDirName are in the $Venue cache; Invoke-VenueBaseline finds this manifest" -Manifest $final.hash
