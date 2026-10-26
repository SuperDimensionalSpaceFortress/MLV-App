# Wait-VenueQuiet.ps1 -- read-only CPU cooldown gate before a venue leg (LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1). RUN THIS ON THE VM.
#
# Two legs that are compared (Classic, then Cinematic) should each start on a venue that is not still busy from the previous one. This gate:
#   1. waits on the read-only queue gate: the venue agent's inbox holds no *.job.ps1 and running\ is empty (GATE_BUSY after -MaxGateSec);
#   2. submits ONE probe job through tools\profiling\um-run.ps1 (the only writer to a venue share), with a unique JobId venue-quiet-probe-<utc>.
#      The job reads 3 samples of \Processor(_Total)\% Processor Time, 12 s apart -- the counter and cadence of the attribution job's own
#      quiescence gate (playback-attr-3-cuda-job.ps1, Get-AttrCudaQuiescenceSample) -- plus the top 5 processes by CPU-seconds over that window;
#      schema v2 (VENUE-QUIET-ATTRIBUTION-1) ADDS attribution to the same job and decides nothing with it: from the same Get-Counter set per sample, the top 10
#      \Process(*)\% Processor Time instances (as percent of the WHOLE machine: counter / logical processors; _Total and Idle excluded from the ranking), the
#      Process _Total / Idle / System values, \Processor(_Total) privileged / user / DPC / interrupt time, and per-core \Processor Information(*) load with
#      P-cores and E-cores apart (core_class unknown where the topology cannot be derived). A counter the host lacks is null with a reason under `notes`.
#      The v1 `samples` and `top` fields are unchanged, and a v1 probe line still parses.
#      ONE AGGREGATION RULE for every per-process and per-core figure (r2, hub ruling):
#        VALID-SAMPLE MEAN: a figure is the mean over the samples whose counter status is valid for THAT instance; an invalid or missing sample is never zero-filled.
#          Every row carries validSamples and totalSamples (the probe's sample sets); validSamples < totalSamples sets `incomplete: true` and a note names the count of
#          incomplete rows; a row with validSamples = 0 is omitted from the attributed sum and the core list and counted in a note.
#        PID-KEYED: process rows are keyed on (instance name, ID Process), never the PDH instance name alone, because "name#n" is reassigned when a process exits. One name
#          under two pids is TWO rows, each with its own valid-sample mean (and, covering only part of the window, each incomplete); a sample whose ID Process cannot be
#          read cannot be keyed and is counted in a note. (Rejected: flag the name pidUnstable and drop it from the sum -- that hides real load from the gap. Known limit: two
#          lives of one name cover disjoint parts of the window, so their means add to more than the window mean of that slot; both rows are flagged incomplete.)
#        GAP: processorTotal (\Processor(_Total)), processActive (Process(_Total) minus Process(Idle), paired within a sample), attributed (sum of the top-10 rows), and
#          unattributedKernel = processorTotal - processActive (kernel time that no process is charged for), computed PER SAMPLE SET from the counters read in the same call and
#          then averaged; processor and process counters are different counter sets read at slightly different instants, so a negative per-sample value is skew, not a
#          measurement: it is INVALID (excluded from the mean, never clamped) and counted in a note, and with no valid sample the field is null with a reason.
#          The acceptance gap is |processActive - attributed| (`attribution_gap_points`, never negative); the 5-point bound is unchanged. All four figures are printed on the `attr gap` line.
#        RAW RETENTION: the raw VENUE_QUIET line is written verbatim (unrounded) to <WorkDir>\<jobId>.json, beside the job file, and its path is printed as `  raw <path>`.
#        PAIR DENOMINATOR (VENUE-QUIET-ACTIVE-PAIR-MISSING-NOTE-1): process_active_dropped_samples and unattributed_kernel_dropped_samples (and the "no non-negative sample" reasons)
#          divide by the sets that HAD the pair (kept + dropped), not by the set count. A set with a missing or invalid member is named in its own note, new keys
#          `process_active_pair_missing` (Process(_Total) or Process(Idle) missing/invalid in that set) and `unattributed_kernel_pair_missing` (Processor(_Total) missing in a
#          set whose process active was valid); neither moves a value. `top` cpuSeconds (after minus before) is keyed on (pid, process start time): a pid whose start time
#          differs between the two snapshots (reused) or cannot be read is left out of `top` and counted in the new note `top_pid_reuse_dropped`, never a negative or foreign figure.
#      None of this feeds the QUIET decision: its threshold, v1 fields and the offline -SamplesJson output are byte-identical to before.
#      PROCESS IDENTITY (VENUE-PROBE-PROCESS-ATTRIBUTION-1): a busy reading names WHO and WHAT, not only `top <name> pid=<n> cpuSeconds=<x>`. The probe line gains ONE more field,
#        `identity` (sub-schema mlv-app/venue-quiet-probe-identity/v1). The top-level schema stays v2 on purpose: identity only ADDS a field, every v2 reader ignores it, and a
#        probe or gate that predates it still parses; a probe WITHOUT identity prints exactly what it printed before.
#        WHEN: collection runs AFTER the sample window and after every v2 figure, never inside the sample loop, with no new sleep. It decides nothing and sits in one try/catch: on
#          any failure the line carries identity = { schema, error } and samples, top and every v2 field are unchanged.
#        WHO: the targets are the top[] pids, the top_processes pids that are not null, and the top 3 pids by GPU. COST BOUND: at most 3 WQL Win32_Process queries (the targets, then
#          their parents, then their grandparents; never a full-table enumeration, never polling) + one GetOwner per TARGET + ONE \GPU Engine(*)\Utilization Percentage read. The
#          GPU read is a separate Get-Counter call, never added to the decision's counter groups, and runs before the CIM batch because its top pids are CIM targets too.
#          Per target row: owner, started_utc (checked against the sampled process start; a start that differs by more than 1 s is start_check mismatch = a reused pid, and that
#          row carries pid, name and start_check ONLY), command line with no argument text, executable, script, project, parent and grandparent (status ok | exited | pid-reused when the
#          parent STARTED AFTER its child | none = no parent pid or nothing to compare), GPU percent and engine type.
#        PROJECT: a pinned table (mlv-app, agent-bridge, adobe-ingester, fleet-doctrine, adversarialllm, virtual-ten, airmypc), first match wins, case-insensitive substring on the
#          UNREDACTED command line and executable path (held in memory only), the process itself first, then its parent, then its grandparent; only the table's name is emitted.
#          A user-profile root is not a project (the owner field covers it). The working directory is NOT collected: Win32_Process does not expose it and reading the process
#          environment block or memory is out of scope. No environment block is ever read.
#        REDACTION IS AN ALLOWLIST (r5): NO ARGUMENT TEXT IS EVER PERSISTED. cmdline is `<exe> args=<count> script=<path|<none>>` and nothing else, on every line: the exe path
#          (ExecutablePath, else the OS image name), the argument count, and a script path taken ONLY from the first argument after the executable, ONLY when it is a drive-rooted
#          .ps1/.psm1/.py/.js/.mjs/.cmd/.bat path with no whitespace, '=', '@', '?', '#' or ':' past the drive letter that EXISTS on a fixed local drive of the probed host. The row's
#          `script` is that same path or null. SCRIPT CHECK (r6): every filesystem call for that slot (drive type, ONE attribute read per path component from the drive root,
#          existence) runs in the bounded identity worker under the call deadline, never on the main path, so a slow or dead volume behind a local-looking path cannot hold the
#          probe; a component that is a reparse point (junction, symlink, mount point) is never followed. Each row's `script_check` says why: ok | notfound | reparse | nonfixed
#          | relative | rejected | timeout | error (null when the row has no readable command line; cmdline_status says why); only ok fills script. NO GHOST SEGMENTS (r7): a
#          slot holding a '.' or '..' segment, a '/', a doubled '\', a trailing separator, a \\?\ or \\.\ prefix or an 8.3 '~<digit>' name is rejected as text, and `script` is
#          the worker-validated full path, persisted only when it equals the slot text case-insensitively (else rejected). The old token scan is only the diagnostic integer cmdline_token_hits and gates nothing. Then a 200-character cap keeps the first 200
#          and appends `...[+N]` (cmdline_truncated_chars = N). The contract is documented at Protect-IdentCmdLine.
#        NON-ELEVATED DEGRADATION: the probe runs as the agent account, not elevated. What it cannot read is a TYPED status, never a guess: owner_status access-denied | exited |
#          error-<ReturnValue> | budget-exceeded, cmdline_status / exe_status unreadable | exited, gpu_status counter-unavailable: <why> | no-gpu-context | disabled: <why>. A null
#          always has its reason. WALL-CLOCK BUDGET: past 25 s of identity time no further GetOwner call is made (owner_status budget-exceeded), so identity can never push the
#          probe job toward its 120 s submission timeout, which would turn a measured busy venue into UNKNOWN. CALL DEADLINE: the budget gates only later GetOwner calls, so every
#          synchronous call (the GPU read, each Win32_Process query, the one script-slot check, each GetOwner) ALSO runs in a worker runspace with a 5 s deadline of its own and is
#          abandoned past it: gpu_status `timeout: ...`, script_check `timeout`, owner_status `timeout`, or (a query) identity = { schema, error }; the measured samples and the
#          quiet decision are never touched.
#        GATE TEXT: each `top` line keeps its prefix byte for byte and appends comma-free, whitespace-free key=value pairs (owner project script script_check parent gparent started
#          gpu); `attr proc` lines append owner, project and script_check; `ident probe` (user, elevated, cost), `ident gpu` (GPU-heavy pids not already in top, with script_check),
#          `ident note` and `ident error` follow the attr lines. script_check is printed only for a row that carries it (an older probe's row prints what it printed before).
#          No command line is ever printed. Summarize-VenueBusyProbes.ps1 (read-only) groups the busy probes' raw files by project using this script's own decision function.
#      HEAT / GPU / LEASE TELEMETRY (VENUE-PROBE-HEAT-GPU-TELEMETRY-1): RECORD-ONLY fields on every probe line (telemetry_schema mlv-app/venue-quiet-probe-telemetry/v1; the
#        top-level schema stays v2), read after the sample window, each block failure-isolated and every read bounded: heat{} = the agent's own heavy-slot feed reading (source,
#        sample_age_s, hottest_core_c, max15_c, mean_c and throttled over the agent's 60-row window, package_w); gpu{} = the dGPU power state read FIRST from
#        DEVPKEY_Device_PowerData, which does not wake it, and nvidia-smi only when it is already D0 (else skipped_wake_risk = true and nothing else is read);
#        seconds_since_last_lease + last_lease{} = the newest quiet-lease event in the agent's logs\agent.log. Every null has a typed reason; none of it reaches the
#        decision. The gate prints three `tele` lines after the ident lines. The contract is documented above $telemetrySchema in the job.
#      BUSIEST FAST CORE (VENUE-QUIET-PER-CORE-VERDICT-1): the mean below is machine-wide, so on a 20-logical-CPU host one foreign process saturating ONE fast core is ~5 % of the
#        total and cannot move it. BACHELOR-HEAT-WHILE-QUIET-DIAG-1 r1 measured exactly that: probes read QUIET (mean < 20 %) while the busiest core sat at 66 % (03a3: 6/60 throttled
#        rows, 87.9 C mean, 44 W at 100 C), 82-92 % (03a4: 6/60 throttled) and 26-47 % where the heat was clear (03a2, 0/60, 62.5 C). A busiest-FAST-core check now sits beside the
#        mean, over the probe's own cores[] rows (core_class P, or uniform where the host has one class; E-cores never count). It is LOGGED FIRST and OFF by default:
#          -PerCoreThresholdPercent <n>   default 60: above the 47 % busiest core of the cool wait and below the 66 % of the first hot one. A core is hot at busiest >= threshold.
#          -PerCoreEnforce                the check blocks: a mean-QUIET probe whose busiest fast core is at or over the threshold is BUSY (the PROBE line ends `note=percore core=<instance>
#                                         percent=<p>%`, the final verdict is COOLDOWN_UNMET and a `  percore blocked core=<instance> percent=<p>% threshold=<t>%` line names the core).
#        EVERY probe that carries cores[] (whatever its schema), every v2 probe, and every probe at all once -PerCoreEnforce is on (an UNKNOWN one included, and an unavailable one:
#        reason no-probe) prints ONE line right after its `  raw` line, whatever the switch says:
#          `  percore enabled=<bool> threshold=<t>% busiest_fast_core=<instance|n/a> busiest_fast_pct=<p>%|n/a would_block=<true|false|null>[ reason=<code>]`
#        <instance> is the probe's `<group>,<core>` with the comma written as `_` (gate text is comma-free, as for every other line the run-local drivers split).
#        would_block is what the check says about that probe; with the switch OFF it changes nothing, so the log measures how often enforcing would have fired before anyone turns it on.
#        UNKNOWN IS NULL, NEVER FALSE, AND NEVER A BLOCK: would_block=null with reason no-probe (the probe itself was unavailable), no-cores (no cores[] rows), invalid-cores (cores is not an array), invalid-core-instance (a fast core whose instance is not a "<group>,<core>" string), core-class-unknown (a row whose class is not P / E / uniform, so it
#        might be a fast core), invalid-core-row (an entry in cores[] that is not a JSON object: null, a scalar, a nested array), incomplete-samples (a fast or unknown-class row flagged incomplete, with
#        validSamples != totalSamples, or with a count that is not a whole number >= 1 or an incomplete flag that is not a boolean), invalid-notes (a notes property that is present but not an object, an explicit null included; an absent property is an older probe and is unchanged), cores-omitted (the probe omitted a core for having no
#        valid sample: notes.cores_omitted) or invalid-core-percent (a non-numeric, array or out-of-range
#        value). The figure compared is the unrounded valid-sample mean (`percent_exact`, which the probe job now emits beside the 2-dp `percent`; an older probe falls back to
#        `percent`), as the mean rule decides on the unrounded mean. A core-level BUSY never replaces UNKNOWN, a measured-busy mean or any exit code. Turning the switch ON for a
#        caller is a later ruling, after the logged line has shown how often it would fire.
#   3. decides: QUIET when the UNROUNDED mean of the 3 samples is <= -ThresholdPercent (the printed mean is rounded to 0.1 for display only). A
#      failed counter read makes the check UNKNOWN, never quiet.
#   4. re-probes every -RecheckSec until QUIET or -MaxWaitSec, then prints `QUIET mean=<x>` or `COOLDOWN_UNMET mean=<x>` (a MEASURED busy venue) and the top
#      processes, or -- when the FINAL decision is UNKNOWN -- `VENUE_QUIET_UNKNOWN mean=UNKNOWN ... reason=<why>` and exit 6.
#      FOREIGN FLOOR EARLY EXIT (BACHELOR-VENUE-FOREIGN-FLOOR-FAST-FAIL-1): a floor owned by other software does not drop by waiting (Bachelor: explorer, TextInputHost, Bloody7, other
#      projects' guardians and sessions; MLV-owned CPU was about 1 point), so waiting the whole -MaxWaitSec only delays a leg that then runs as COOLDOWN_UNMET anyway. When
#      -ForeignFloorProbes (default 3; 0 switches the early exit off) CONSECUTIVE measured BUSY probes (an UNKNOWN or a probe that names no foreign owner resets the count) each
#      carry a v2 top_processes list whose top 3 owners are not MLV's, and the floor shows NO DOWNWARD TREND -- the newest mean is not below the first mean of those N probes by
#      more than -ForeignFloorTrendMarginPoints (default 2.0; a drop of exactly the margin is not a trend) -- the wait ends at once and prints, BEFORE the usual verdict line,
#        FOREIGN_FLOOR owners=<name:pid>,<name:pid>,<name:pid> floorMean=<x>% waitedSec=<n> probes=<n>
#      (floorMean = the mean of those N probe means; owners = the newest probe's top 3 by percent of the whole machine, in rank order, whitespace in a name written as _), then the
#      SAME `COOLDOWN_UNMET mean=<x> threshold=<t>% waitedSec=<n>` line as a full wait, the top and attr lines, and exit 0. It is never QUIET: a caller that parses
#      `^(QUIET|COOLDOWN_UNMET) mean=` classifies the leg as the contended COOLDOWN_UNMET it is. OWNER CLASSIFIER (the smallest pinned one; this script had none): a top_processes
#      row is MLV's when its instance name (lower case, the "#n" suffix stripped) starts with `mlv`, `ultra-magnus` or `um-run`; `pwsh` / `powershell` rows are skipped, because
#      the probe job itself and the venue agent run in them and the name cannot tell them from a foreign script. The top 3 of the REST must contain no MLV row; a probe with no v2
#      top_processes (a v1 line, a null list) names no owner and is never a foreign floor. The early exit changes no other exit code and never replaces VENUE_QUIET_UNKNOWN.
# A measured COOLDOWN_UNMET is recorded and the caller proceeds: it decides nothing. VENUE_QUIET_UNKNOWN means the venue was NOT MEASURED (no um-run.ps1, a failed
# submission, a result without a VENUE_QUIET line, or samples that are not 3 finite values in [0, 100]): the caller must fail or skip its leg loudly, never run
# it as if the venue had been measured. The probe never kills, stops or changes any process.
#
# ONE DEADLINE: -MaxWaitSec runs from the start and bounds every queue-gate wait too (a re-probe does not get a fresh gate clock). A gate wait ends at the
# earlier of -MaxGateSec and that deadline (GATE_BUSY, exit 3). Only a probe already submitted can run past it, bounded by um-run.ps1's own timeouts and by
# -MaxAdmissionWaitSec (default 7200, range 1..86400), which is forwarded to um-run.ps1 as its heavy-slot admission ceiling: the venue chain passes its
# -ProbeAdmissionWaitSec here, and sizes the claim ticket's probe-phase staleAfterSec from that same value, so the bound the ticket assumes is the bound the probe honours.
# A read error on the agent share is retried -ReadAttempts times in all, backing off -ReadBackoffSec * attempt, inside the same deadline, before it is
# GATE_UNREADABLE; each retry prints a `GATE_READ_RETRY` line, never a `queued=0 running=0` line.
#
#   pwsh -NoProfile -File tools\profiling\dual-venue\Wait-VenueQuiet.ps1 -Venue bachelor -WorkDir <dir> [-GateLog <file>]
#   pwsh -NoProfile -File tools\profiling\dual-venue\Wait-VenueQuiet.ps1 -SamplesJson '[12.5, 18.0, 21.0]'     # offline: the decision only
#
# The share and the work dir are BOUND to the named venue (SHARE-SCOPE): -AgentShare must be that venue's own agentShare or agentRoot in venues.json;
# another venue's share is SHARE_VENUE_MISMATCH and a share that is no venue's own is SHARE_NOT_VENUE_OWN unless -AllowShareOverride says the caller
# means it (tests); -WorkDir inside any venue's share or agent root is WORKDIR_IN_VENUE_SHARE (um-run.ps1 is the only writer to a venue share). All three
# exit 2 before anything is created, read or submitted, so a verdict can never be printed for a venue other than the one measured.
# -GateLog is bound the same way: GATELOG_IN_VENUE_SHARE (inside any venue's storage) or GATELOG_OUT_OF_SCOPE (neither under -WorkDir nor a .claude-state
# directory) are exit 2 before anything is created. HOST ECHO: the probe job reports $env:COMPUTERNAME; if it is not the venue's expectedHost in venues.json
# (case-insensitive; a missing host counts) the gate prints `HOST_MISMATCH ...` and exits 5 BEFORE any QUIET / COOLDOWN_UNMET line, so an alias of another
# host's share (acknowledged with -AllowShareOverride) can never yield a verdict for the named venue.
# Exit codes: 0 QUIET or a measured COOLDOWN_UNMET, including one that took the FOREIGN_FLOOR early exit (read stdout); 3 GATE_BUSY (the queue never cleared); 4 GATE_UNREADABLE (the agent share could not be
# read -- typed line `GATE_UNREADABLE ...` then `DECISION UNKNOWN`, never an empty-queue line; no probe is submitted); 5 HOST_MISMATCH (the probe was
# answered by a host that is not the venue's); 6 VENUE_QUIET_UNKNOWN (the venue was not measured: typed line `VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=<t>%
# waitedSec=<n> reason=<probe-failed|no-VENUE_QUIET-line|invalid-samples>`, never a QUIET / COOLDOWN_UNMET line); 2 a usage error (including a -SamplesJson that is
# not JSON).
[CmdletBinding(DefaultParameterSetName = 'Live')]
param(
    [Parameter(ParameterSetName = 'Live')][ValidateSet('bachelor', 'ultra-magnus')][string]$Venue = 'bachelor',
    [Parameter(ParameterSetName = 'Live', Mandatory = $true)][string]$WorkDir,
    [Parameter(ParameterSetName = 'Live')][string]$GateLog = '',
    [Parameter(ParameterSetName = 'Live')][string]$AgentShare = '',
    [Parameter(ParameterSetName = 'Live')][switch]$AllowShareOverride,
    [Parameter(ParameterSetName = 'Live')][int]$RecheckSec = 90,
    [Parameter(ParameterSetName = 'Live')][int]$MaxWaitSec = 1800,
    [Parameter(ParameterSetName = 'Live')][int]$MaxGateSec = 2400,
    [Parameter(ParameterSetName = 'Live')][int]$GatePollSec = 30,
    [Parameter(ParameterSetName = 'Live')][int]$ReadAttempts = 3,
    [Parameter(ParameterSetName = 'Live')][int]$ReadBackoffSec = 2,
    [Parameter(ParameterSetName = 'Live')][ValidateRange(1, 86400)][int]$MaxAdmissionWaitSec = 7200,
    [Parameter(ParameterSetName = 'Live')][ValidateRange(0, 100)][int]$ForeignFloorProbes = 3,
    [Parameter(ParameterSetName = 'Live')][ValidateRange(0.0, 100.0)][double]$ForeignFloorTrendMarginPoints = 2.0,
    [Parameter(ParameterSetName = 'Live')][ValidateRange(0.0, 100.0)][double]$PerCoreThresholdPercent = 60.0,
    [Parameter(ParameterSetName = 'Live')][switch]$PerCoreEnforce,
    [Parameter(ParameterSetName = 'Offline', Mandatory = $true)][string]$SamplesJson,
    [double]$ThresholdPercent = 20.0
)
$ErrorActionPreference = 'Stop'
$t0 = Get-Date

function Get-VenueQuietDecision {
    # PURE: the decision from the samples alone. Exactly 3 finite samples in [0, 100] are needed; anything else (a $null = a failed read) is UNKNOWN.
    param([object[]]$Samples, [double]$Threshold)
    $vals = @($Samples)
    $ok = ($vals.Count -eq 3)
    foreach ($v in $vals) {
        if ($null -eq $v -or -not ($v -is [double] -or $v -is [int] -or $v -is [long] -or $v -is [decimal])) { $ok = $false; continue }
        $d = [double]$v
        if (-not [double]::IsFinite($d) -or $d -lt 0.0 -or $d -gt 100.0) { $ok = $false }
    }
    if (-not $ok) { return [pscustomobject]@{ state = 'UNKNOWN'; mean = $null } }
    # The UNROUNDED mean decides; Format-Mean rounds for display only (three 20.04% samples are BUSY at 20 although they print as 20.0%).
    $mean = (($vals | ForEach-Object { [double]$_ }) | Measure-Object -Average).Average
    [pscustomobject]@{ state = $(if ($mean -le $Threshold) { 'QUIET' } else { 'BUSY' }); mean = $mean }
}

function Format-Mean($m) { if ($null -eq $m) { 'UNKNOWN' } else { '{0:0.0}%' -f $m } }

if ($PSCmdlet.ParameterSetName -eq 'Offline') {
    try { $samples = @(ConvertFrom-Json -InputObject $SamplesJson -NoEnumerate) } catch {
        [Console]::Error.WriteLine("USAGE -SamplesJson is not valid JSON: $($_.Exception.Message)")
        exit 2
    }
    if ($samples.Count -eq 1 -and $samples[0] -is [array]) { $samples = @($samples[0]) }
    $d = Get-VenueQuietDecision -Samples $samples -Threshold $ThresholdPercent
    Write-Output "DECISION $($d.state) mean=$(Format-Mean $d.mean)"
    exit 0
}

$here = $PSScriptRoot
$table = [IO.File]::ReadAllText((Join-Path $here 'venues.json')) | ConvertFrom-Json
$agentShare = $(if ($AgentShare) { $AgentShare } else { [string]$table.venues.$Venue.agentShare })

# Bind the share and the work dir to the named venue BEFORE anything is created, read or submitted (see the header).
function ConvertTo-PathKey([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    $full = try { [IO.Path]::GetFullPath($Path) } catch { $Path }
    $full.TrimEnd([char[]]@('\', '/')).ToLowerInvariant()
}
function Test-UnderOrEqual([string]$Key, [string]$Root) { $Root -ne '' -and ($Key -ceq $Root -or $Key.StartsWith($Root + '\', [StringComparison]::Ordinal)) }
function Stop-Usage([string]$Message) { [Console]::Error.WriteLine($Message); exit 2 }
$ownKeys = @(@($table.venues.$Venue.agentShare, $table.venues.$Venue.agentRoot) | ForEach-Object { ConvertTo-PathKey ([string]$_) } | Where-Object { $_ })
$otherKeys = [ordered]@{}
foreach ($v in $table.venues.PSObject.Properties) {
    foreach ($p in @($v.Value.agentShare, $v.Value.agentRoot)) { $k = ConvertTo-PathKey ([string]$p); if ($k) { $otherKeys[$k] = $v.Name } }
}
if ($AgentShare) {
    $shareKey = ConvertTo-PathKey $AgentShare
    if ($ownKeys -notcontains $shareKey) {
        $foreign = @($otherKeys.Keys | Where-Object { $otherKeys[$_] -cne $Venue -and (Test-UnderOrEqual $shareKey $_) })
        if ($foreign.Count -gt 0) { Stop-Usage "SHARE_VENUE_MISMATCH -AgentShare '$AgentShare' is venue '$($otherKeys[$foreign[0]])' agent storage, not '$Venue': a verdict printed for '$Venue' would describe another host" }
        if (-not $AllowShareOverride) { Stop-Usage "SHARE_NOT_VENUE_OWN -AgentShare '$AgentShare' is not '$Venue' agentShare or agentRoot in venues.json; pass -AllowShareOverride only for a test share" }
    }
}
$workKey = ConvertTo-PathKey $WorkDir
foreach ($k in $otherKeys.Keys) {
    if (Test-UnderOrEqual $workKey $k) { Stop-Usage "WORKDIR_IN_VENUE_SHARE -WorkDir '$WorkDir' is inside venue '$($otherKeys[$k])' agent storage; um-run.ps1 is the only writer to a venue share" }
}
# -GateLog is appended to by this script, so it is bound too: never inside a venue's agent storage (um-run.ps1 is the only writer there), and only under the
# run's own -WorkDir or a .claude-state directory.
if ($GateLog) {
    $logKey = ConvertTo-PathKey $GateLog
    foreach ($k in $otherKeys.Keys) {
        if (Test-UnderOrEqual $logKey $k) { Stop-Usage "GATELOG_IN_VENUE_SHARE -GateLog '$GateLog' is inside venue '$($otherKeys[$k])' agent storage; um-run.ps1 is the only writer to a venue share" }
    }
    $logInScope = (Test-UnderOrEqual $logKey $workKey) -or (@($logKey.Split([char[]]@('\', '/')) | Where-Object { $_ -ceq '.claude-state' }).Count -gt 0)
    if (-not $logInScope) { Stop-Usage "GATELOG_OUT_OF_SCOPE -GateLog '$GateLog' is neither under -WorkDir nor under a .claude-state directory" }
}
$expectedHost = [string]$table.venues.$Venue.expectedHost
if (-not $expectedHost) { Stop-Usage "VENUE_HOST_UNKNOWN venues.json names no expectedHost for '$Venue': a probe answer could not be attributed to it" }
$umRun = Join-Path $here '..\um-run.ps1'
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

function Write-Gate([string]$Line) {
    Write-Output $Line
    if ($GateLog) { $Line | Add-Content -LiteralPath $GateLog }
}

# One directory count, or $null when the directory cannot be read: an unreachable share is NOT an empty queue. The inbox must exist; running\ may
# legitimately be absent while the share itself is readable (nothing has run), so only a read error on an existing running\ is unreadable.
# VENUE-QUIET-SETASIDE-RECORD-1: -ExcludePattern drops names the agent has already RESOLVED. The agent sets a superseded marker tmp aside IN running\ as
# '<name>.stale-<ticks>' or '<name>.corrupt-<ticks>' and never lists it again (ultra-magnus-agent.ps1 Get-MarkerTempFiles), so it is not a running job.
# Counting it held the gate at running=1 for every lane after the job had ended (2026-10-13 23:00Z). A live '<id>.started.json' and an unresolved tmp still count.
$setAsideRecordPattern = '\.(stale|corrupt)-\d+$'
function Get-GateCount([string]$Dir, [string]$Filter, [bool]$MustExist, [string]$ExcludePattern = '') {
    if (-not (Test-Path -LiteralPath $Dir -PathType Container)) {
        if ($MustExist -or -not (Test-Path -LiteralPath $agentShare -PathType Container)) { return $null }
        return 0
    }
    try {
        if ($Filter) { return @(Get-ChildItem -LiteralPath $Dir -Filter $Filter -ErrorAction Stop).Count }
        return @(Get-ChildItem -LiteralPath $Dir -ErrorAction Stop | Where-Object { -not $ExcludePattern -or $_.Name -notmatch $ExcludePattern }).Count
    } catch { return $null }
}

# Sets $script:GateState to 'CLEAR', 'BUSY' (the queue never cleared) or 'UNREADABLE' (the share could not be read: nothing is known, the decision
# is UNKNOWN). The state is not the function's output: its GATE lines are written to the pipeline and must reach stdout.
#
# ONE DEADLINE: the clock is the script's own $t0, never restarted. A gate wait ends at t0 + min(-MaxGateSec, -MaxWaitSec), so a re-probe that finds the
# queue busy again cannot stretch the whole wait past -MaxWaitSec. The share is read up to -ReadAttempts times (see Read-GateCounts) before it is unreadable.
$deadline = $t0.AddSeconds($MaxWaitSec)
$gateEnd = $t0.AddSeconds([Math]::Min($MaxGateSec, $MaxWaitSec))

# Both directory counts, retried on a read error. Results go to $script:GateQueued / $script:GateRunning ($null = still unreadable after the last attempt):
# like GateState they are not the function's output, because its GATE_READ_RETRY lines are written to the pipeline.
function Read-GateCounts([string]$Name) {
    for ($attempt = 1; $true; $attempt++) {
        $script:GateQueued = Get-GateCount "$agentShare\inbox" '*.job.ps1' $true
        $script:GateRunning = Get-GateCount "$agentShare\running" '' $false $setAsideRecordPattern
        if (($null -ne $script:GateQueued -and $null -ne $script:GateRunning) -or $attempt -ge $ReadAttempts) { return }
        $delay = $ReadBackoffSec * $attempt
        if ((Get-Date).AddSeconds($delay) -gt $deadline) { return }   # no time left for another try: report what is known
        Write-Gate "GATE_READ_RETRY $Name $Venue attempt=$($attempt + 1)/$ReadAttempts inboxReadable=$($null -ne $script:GateQueued) runningReadable=$($null -ne $script:GateRunning) $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
        if ($delay -gt 0) { Start-Sleep -Seconds $delay }
    }
}

function Wait-QueueGate([string]$Name) {
    while ($true) {
        Read-GateCounts $Name
        $q = $script:GateQueued; $r = $script:GateRunning
        if ($null -eq $q -or $null -eq $r) {
            Write-Gate "GATE_UNREADABLE $Name $Venue share=$agentShare inboxReadable=$($null -ne $q) runningReadable=$($null -ne $r) $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
            $script:GateState = 'UNREADABLE'; return
        }
        Write-Gate "GATE $Name $Venue queued=$q running=$r $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
        if ($q -eq 0 -and $r -eq 0) { $script:GateState = 'CLEAR'; return }
        $now = Get-Date
        if ($now -gt $gateEnd) { Write-Gate "GATE_BUSY $Name waitedSec=$([int]($now - $t0).TotalSeconds)"; $script:GateState = 'BUSY'; return }
        # (the poll never sleeps past the end of the gate, so the wait ends when the deadline does)
        Start-Sleep -Milliseconds ([int][Math]::Max(10, [Math]::Min($GatePollSec * 1000, ($gateEnd - $now).TotalMilliseconds + 10)))
    }
}

# The probe job: read-only. Prints ONE line VENUE_QUIET=<json>, schema v2 (v1 `samples` and `top` are unchanged; v2 only ADDS the attribution fields below).
# Each of the 3 probe samples is ONE Get-Counter call over every counter, so the extra counters cost one read, not one per counter; a counter the host lacks
# is recorded as null with a reason in `notes` and never fails the probe (if the combined call throws, each counter group is read on its own).
# PERCENT BASIS: every \Process(*) percent below is the counter value divided by cpu_count (logical processors), i.e. percent of the WHOLE machine, so it is
# comparable with \Processor(_Total)\% Processor Time (which the gate decides on). All v2 values are means over the samples that are VALID for that instance (see the gate header).
$probeText = @'
$ErrorActionPreference = 'Stop'
$cpuCount = [Environment]::ProcessorCount
$groups = [ordered]@{
    processor = @('\Processor(_Total)\% Processor Time', '\Processor(_Total)\% Privileged Time', '\Processor(_Total)\% User Time', '\Processor(_Total)\% DPC Time', '\Processor(_Total)\% Interrupt Time')
    process   = @('\Process(*)\% Processor Time', '\Process(*)\ID Process')
    cores     = @('\Processor Information(*)\% Processor Time')
}
$allPaths = @($groups.Values | ForEach-Object { $_ })
function Get-GoodValue($s) {
    $st = $s.PSObject.Properties['Status']
    if ($null -eq $st -or ($st.Value -ne 0 -and $st.Value -ne 1) -or $null -eq $s.CookedValue) { return $null }
    $v = [double]$s.CookedValue
    if ([double]::IsNaN($v) -or [double]::IsInfinity($v) -or $v -lt 0) { return $null }
    return $v
}
# -ErrorAction SilentlyContinue, not Stop: with Stop, one process that exits mid-sample turns \Process(*) into "data ... not valid" for the WHOLE read (measured: 8 of 8
# reads failed on a busy host). The per-sample Status is checked in Get-GoodValue instead; a missing counter path still throws and is caught by the caller.
function Invoke-Counter($paths) { @((Get-Counter -Counter $paths -ErrorAction SilentlyContinue).CounterSamples) }
function Read-CounterSet {
    $raw = @(); $errs = @{}
    try { $raw = @(Invoke-Counter $allPaths) } catch { }
    if ($raw.Count -eq 0) {
        foreach ($g in $groups.Keys) {
            try { $part = @(Invoke-Counter $groups[$g]); if ($part.Count -eq 0) { $errs[$g] = 'Get-Counter returned no samples' } else { $raw += $part } } catch { $errs[$g] = [string]$_.Exception.Message }
        }
    }
    $set = [pscustomobject]@{ v1 = $null; cpu = @{}; proc = @{}; pids = @{}; core = @{}; errs = $errs }
    foreach ($s in $raw) {
        # the instance key comes from the PATH: CounterSample.InstanceName drops the "#n" suffix, which would merge every chrome#1, chrome#2 ... into one
        $lp = ([string]$s.Path).ToLowerInvariant()
        if ($lp -notmatch '^\\\\[^\\]*\\([^\\(]+)\(') { continue }
        $obj = $Matches[1]; $leaf = $lp.Substring($lp.LastIndexOf('\') + 1)
        $inst = $(if ($lp -match '\((.*)\)\\[^\\]*$') { $Matches[1] } else { '' })
        # an instance whose status is invalid is RECORDED with a $null value (seen, not valid): the aggregation counts valid samples per instance and never zero-fills
        $val = Get-GoodValue $s
        if ($obj -eq 'processor') { if ($inst -eq '_total' -and $null -ne $val) { $set.cpu[$leaf] = $val } }
        elseif ($obj -eq 'process') {
            if ($leaf -eq '% processor time') { $set.proc[$inst] = $val } elseif ($leaf -eq 'id process' -and $null -ne $val) { $set.pids[$inst] = [int64]$val }
        }
        elseif ($obj -eq 'processor information') { if ($leaf -eq '% processor time' -and $inst -notmatch '_total') { $set.core[$inst] = $val } }
    }
    $t = $set.cpu['% processor time']
    if ($null -ne $t -and $t -le 100) { $set.v1 = $t }
    $set
}
function Get-CpuSnapshot { $h = @{}; foreach ($p in @(Get-Process -ErrorAction SilentlyContinue)) { try { if ($p.TotalProcessorTime) { $st = $null; try { $st = $p.StartTime.ToFileTimeUtc() } catch { }; $h[$p.Id] = @($p.Name, $p.TotalProcessorTime.TotalSeconds, $st) } } catch { } }; $h }
function Get-Mean($values) { $v = @($values); if ($v.Count -eq 0) { return $null }; [math]::Round(($v | Measure-Object -Average).Average, 2) }
# Core topology: GetLogicalProcessorInformationEx(RelationAll) reports each physical core's EfficiencyClass and its logical processors (a mask per processor group),
# plus the NUMA nodes. The highest class is a P-core, a lower class an E-core; one class only is "uniform". The \Processor Information instance "a,b" is read as
# NUMA node a, processor b within that node when the host has several NUMA nodes in ONE processor group (measured: a 16-vCPU VM shows 0,0-0,7 and 1,0-1,7 while the API
# reports one group), and as processor group a, bit b otherwise; a counter instance the map does not contain is core_class "unknown", never guessed.
# Returns @{ map = @{ 'a,b' = class }; reason = $null } or an empty map with the reason.
function Get-CoreTopology {
    $map = @{}
    try {
        if ([IntPtr]::Size -ne 8) { return @{ map = $map; reason = 'not a 64-bit process' } }
        Add-Type -Namespace MlvQuiet -Name Cpu -MemberDefinition '[System.Runtime.InteropServices.DllImport("kernel32.dll", SetLastError = true)] public static extern bool GetLogicalProcessorInformationEx(int RelationshipType, System.IntPtr Buffer, ref uint ReturnedLength);'
        $len = [uint32]0
        [void][MlvQuiet.Cpu]::GetLogicalProcessorInformationEx(0xFFFF, [IntPtr]::Zero, [ref]$len)
        if ($len -lt 40) { return @{ map = $map; reason = "GetLogicalProcessorInformationEx reported $len bytes" } }
        $buf = [Runtime.InteropServices.Marshal]::AllocHGlobal([int]$len)
        try {
            if (-not [MlvQuiet.Cpu]::GetLogicalProcessorInformationEx(0xFFFF, $buf, [ref]$len)) { return @{ map = $map; reason = 'GetLogicalProcessorInformationEx failed' } }
            $m = [Runtime.InteropServices.Marshal]
            $byGroupBit = @{}; $nodes = @{}; $groupsSeen = @{}
            $o = 0
            while ($o + 8 -le $len) {
                $size = $m::ReadInt32($buf, $o + 4)
                if ($size -lt 8) { break }
                $rel = $m::ReadInt32($buf, $o)
                if (($rel -eq 0 -or $rel -eq 1) -and $size -ge 48) {
                    $groupCount = [int]$m::ReadInt16($buf, $o + 30); if ($groupCount -lt 1) { $groupCount = 1 }
                    for ($g = 0; $g -lt $groupCount; $g++) {
                        $at = $o + 32 + 16 * $g
                        $mask = $m::ReadInt64($buf, $at); $grp = [int]$m::ReadInt16($buf, $at + 8)
                        for ($bit = 0; $bit -lt 64; $bit++) {
                            if (-not (($mask -shr $bit) -band 1)) { continue }
                            if ($rel -eq 0) { $byGroupBit["$grp,$bit"] = [int]$m::ReadByte($buf, $o + 9); $groupsSeen[$grp] = $true }
                            else { $node = [int]$m::ReadInt32($buf, $o + 8); if (-not $nodes.ContainsKey($node)) { $nodes[$node] = @() }; $nodes[$node] += ,@($grp, $bit) }
                        }
                    }
                }
                $o += $size
            }
            if ($byGroupBit.Count -eq 0) { return @{ map = $map; reason = 'GetLogicalProcessorInformationEx returned no cores' } }
            if ($nodes.Count -gt 1 -and $groupsSeen.Count -eq 1) {
                foreach ($n in $nodes.Keys) {
                    $ord = 0
                    foreach ($gb in @($nodes[$n] | Sort-Object { $_[0] * 1000 + $_[1] })) { $map["$n,$ord"] = $byGroupBit["$($gb[0]),$($gb[1])"]; $ord++ }
                }
            } else { $map = $byGroupBit }
        } finally { [Runtime.InteropServices.Marshal]::FreeHGlobal($buf) }
        return @{ map = $map; reason = $null }
    } catch { return @{ map = @{}; reason = [string]$_.Exception.Message } }
}
$before = Get-CpuSnapshot
$samples = @(); $sets = @()
for ($i = 0; $i -lt 3; $i++) { $set = Read-CounterSet; $sets += ,$set; $samples += ,$set.v1; if ($i -lt 2) { Start-Sleep -Seconds 12 } }
$after = Get-CpuSnapshot
# top[] is keyed on (pid, process start time): a pid that exited and was reused between the two snapshots is a different process, so a start time that differs or cannot be read drops the entry
$topKept = @(); $topDropped = 0
foreach ($id in @($after.Keys | Where-Object { $before.ContainsKey($_) })) {
    $b = $before[$id]; $a = $after[$id]
    if ($null -eq $b[2] -or $null -eq $a[2] -or $b[2] -ne $a[2]) { $topDropped++; continue }
    $topKept += [pscustomobject]@{ name = $a[0]; pid = $id; cpuSeconds = [math]::Round($a[1] - $b[1], 2) }
}
$top = @($topKept | Sort-Object cpuSeconds -Descending | Select-Object -First 5)
$notes = [ordered]@{}
if ($topDropped -gt 0) { $notes['top_pid_reuse_dropped'] = "$topDropped process(es) present in both snapshots were left out of top: the pid's process start time differs (pid reused) or could not be read, so its cpuSeconds would not be one process's" }
function Get-GroupWhy([string]$Group) {
    $e = @($sets | Where-Object { $_.errs.ContainsKey($Group) } | ForEach-Object { $_.errs[$Group] }) | Select-Object -First 1
    if ($e) { "counter group '$Group' unreadable: $e" } else { "counter group '$Group' returned no valid sample" }
}
# processor-wide breakdown
$cpuNames = [ordered]@{ total_percent = '% processor time'; privileged_percent = '% privileged time'; user_percent = '% user time'; dpc_percent = '% dpc time'; interrupt_percent = '% interrupt time' }
$processor = [ordered]@{}
foreach ($k in $cpuNames.Keys) {
    $processor[$k] = Get-Mean @($sets | Where-Object { $_.cpu.ContainsKey($cpuNames[$k]) } | ForEach-Object { $_.cpu[$cpuNames[$k]] })
    if ($null -eq $processor[$k]) { $notes["processor.$k"] = Get-GroupWhy 'processor' }
}
# per-process: VALID-SAMPLE MEAN keyed on (instance, ID Process), as percent of the whole machine (counter / cpuCount); see the gate's header for the rule
$nSets = $sets.Count
$acc = @{}; $seen = @{}; $keyed = @{}; $noPid = @{}
$specialSum = @{}; $specialN = @{}
$activeSum = 0.0; $activeN = 0; $activeNeg = 0
$kernSum = 0.0; $kernN = 0; $kernNeg = 0
$activeMissing = 0; $kernMissing = 0
foreach ($set in $sets) {
    foreach ($k in $set.proc.Keys) {
        $v = $set.proc[$k]
        if ($k -ceq '_total' -or $k -ceq 'idle') {
            if ($null -ne $v) { $specialSum[$k] = [double]$specialSum[$k] + $v; $specialN[$k] = 1 + [int]$specialN[$k] }
            continue
        }
        $seen[$k] = $true
        if ($null -eq $v) { continue }
        $pidv = $set.pids[$k]
        if ($null -eq $pidv) { $noPid[$k] = $true; continue }
        $key = "$k|$pidv"
        if (-not $acc.ContainsKey($key)) { $acc[$key] = @{ instance = $k; pid = [int64]$pidv; sum = 0.0; n = 0 } }
        $acc[$key].sum += $v; $acc[$key].n++
        $keyed[$k] = $true
    }
    if ($null -ne $set.proc['_total'] -and $null -ne $set.proc['idle']) {
        # VENUE-QUIET-PROCESS-ACTIVE-NEGATIVE-1: Process(_Total) and Process(Idle) are read at slightly different instants, so on a near-idle host (_Total - Idle) can be negative for a sample.
        # A negative sample is INVALID (excluded from the mean and counted in $activeNeg, never clamped to 0 and never averaged in); no valid sample left -> null with a reason.
        $d = $set.proc['_total'] - $set.proc['idle']
        if ($d -ge 0) {
            $activeSum += $d; $activeN++
            # unattributedKernel is a cross-counter-set difference (Processor(_Total) minus Process active), taken per sample set from the one Get-Counter call; a negative one is skew, so INVALID (not clamped)
            $cpuT = $set.cpu['% processor time']
            if ($null -ne $cpuT) { $kv = $cpuT - $d / $cpuCount; if ($kv -ge 0) { $kernSum += $kv; $kernN++ } else { $kernNeg++ } } else { $kernMissing++ }
        } else { $activeNeg++ }
    } else { $activeMissing++ }
}
# the notes' denominators are the sets that HAD the pair (kept + dropped); a set missing a member is named on its own, never counted as a pair read
$activePairs = $activeN + $activeNeg; $kernPairs = $kernN + $kernNeg
function Get-SpecialMean([string]$Name) { if ($specialN.ContainsKey($Name)) { $specialSum[$Name] / $specialN[$Name] / $cpuCount } else { $null } }
$rows = @($acc.Values | ForEach-Object { [pscustomobject]@{ instance = $_.instance; pid = $_.pid; mean = $_.sum / $_.n / $cpuCount; validSamples = $_.n; totalSamples = $nSets; incomplete = ($_.n -lt $nSets) } } |
    Sort-Object @{ Expression = 'mean'; Descending = $true }, instance, pid)
$pTotal = $null; $pIdle = $null; $pSystem = $null; $pActive = $null; $ranked = @(); $attributed = $null; $gap = $null; $unattrKernel = $null
if ($specialN.Count -gt 0 -or $rows.Count -gt 0) {
    $m = Get-SpecialMean '_total'; if ($null -ne $m) { $pTotal = [math]::Round($m, 2) }
    $m = Get-SpecialMean 'idle'; if ($null -ne $m) { $pIdle = [math]::Round($m, 2) }
    $sysRow = @($rows | Where-Object { $_.instance -ceq 'system' } | Sort-Object validSamples -Descending | Select-Object -First 1)
    if ($sysRow.Count -gt 0) { $pSystem = [math]::Round($sysRow[0].mean, 2) }
    $topRows = @($rows | Select-Object -First 10)
    $ranked = @($topRows | ForEach-Object { [pscustomobject]@{ instance = $_.instance; pid = $_.pid; percent = [math]::Round($_.mean, 2); validSamples = $_.validSamples; totalSamples = $_.totalSamples; incomplete = $_.incomplete } })
    $attrRaw = $null; $activeRaw = $null
    if ($topRows.Count -gt 0) { $attrRaw = ($topRows | ForEach-Object { $_.mean } | Measure-Object -Sum).Sum; $attributed = [math]::Round($attrRaw, 2) }
    if ($activeN -gt 0) { $activeRaw = $activeSum / $activeN / $cpuCount; $pActive = [math]::Round($activeRaw, 2) }
    if ($null -ne $activeRaw -and $null -ne $attrRaw) { $gap = [math]::Round([math]::Abs($activeRaw - $attrRaw), 2) }
    if ($kernN -gt 0) { $unattrKernel = [math]::Round($kernSum / $kernN, 2) }
    $incAll = @($rows | Where-Object { $_.incomplete }).Count
    if ($incAll -gt 0) { $notes['process_incomplete_rows'] = "$incAll of $($rows.Count) process rows have fewer valid samples than the $nSets sample sets (ranked: $(@($topRows | Where-Object { $_.incomplete }).Count) of $($topRows.Count)); each is the mean of its valid samples" }
    $omitted = @($seen.Keys | Where-Object { -not $keyed.ContainsKey($_) -and -not $noPid.ContainsKey($_) }).Count
    if ($omitted -gt 0) { $notes['process_rows_omitted'] = "$omitted process instance(s) had no valid sample and are omitted from the attributed sum" }
    $unkeyed = @($noPid.Keys | Where-Object { -not $keyed.ContainsKey($_) }).Count
    if ($unkeyed -gt 0) { $notes['process_rows_unkeyed'] = "$unkeyed process instance(s) had a valid percent but no readable ID Process and are omitted from the attributed sum" }
    if ($null -eq $pTotal) { $notes['process_total_percent'] = 'no valid _total sample in the process counters' }
    if ($null -eq $pIdle) { $notes['idle_percent'] = 'no valid idle sample in the process counters' }
    if ($null -eq $pSystem) { $notes['system_percent'] = 'no valid system sample in the process counters' }
    if ($null -eq $attributed) { $notes['attributed_percent'] = 'no process row had a valid sample' }
    if ($null -eq $pActive) { $notes['process_active_percent'] = $(if ($activeNeg -gt 0) { "no non-negative sample: Process(Idle) read above Process(_Total) in $activeNeg of $activePairs sample sets, which are invalid and not clamped" } else { 'no sample had a valid Process(_Total) and Process(Idle) together' }) }
    elseif ($activeNeg -gt 0) { $notes['process_active_dropped_samples'] = "$activeNeg of $activePairs sample sets read Process(Idle) above Process(_Total) and are excluded from process_active_percent (mean of the other $activeN)" }
    if ($activeMissing -gt 0) { $notes['process_active_pair_missing'] = "$activeMissing set(s) had no valid Process(Idle)/Process(_Total) pair (one member missing or invalid), so they are neither kept nor dropped in process_active_percent" }
    if ($null -eq $gap) { $notes['attribution_gap_points'] = 'needs both process_active_percent and attributed_percent' }
    if ($null -eq $unattrKernel) { $notes['unattributed_kernel_percent'] = $(if ($kernNeg -gt 0) { "no non-negative sample: Processor(_Total) read below Process(_Total) minus Process(Idle) in $kernNeg of $kernPairs sample sets (different counter sets read at slightly different instants), which are invalid and not clamped" } elseif ($null -eq $pActive -or $null -eq $processor.total_percent) { 'needs both process_active_percent and processor.total_percent' } else { 'no sample set had Processor(_Total) and a valid process active together' }) }
    elseif ($kernNeg -gt 0) { $notes['unattributed_kernel_dropped_samples'] = "$kernNeg of $kernPairs sample sets read Processor(_Total) below Process(_Total) minus Process(Idle) and are excluded from unattributed_kernel_percent (mean of the other $kernN)" }
    if ($kernMissing -gt 0) { $notes['unattributed_kernel_pair_missing'] = "$kernMissing set(s) had no valid Processor(_Total)/Process active pair (Processor(_Total) missing in a set whose process active was valid), so they are neither kept nor dropped in unattributed_kernel_percent" }
} else {
    foreach ($f in 'process_total_percent', 'idle_percent', 'system_percent', 'top_processes', 'attributed_percent', 'process_active_percent', 'attribution_gap_points', 'unattributed_kernel_percent') { $notes[$f] = Get-GroupWhy 'process' }
}
# per-core, P-cores and E-cores apart: the same valid-sample mean per instance
$topo = Get-CoreTopology
$coreAcc = @{}; $coreSeen = @{}
foreach ($set in $sets) {
    foreach ($k in $set.core.Keys) {
        $coreSeen[$k] = $true
        $v = $set.core[$k]
        if ($null -eq $v) { continue }
        if (-not $coreAcc.ContainsKey($k)) { $coreAcc[$k] = @{ sum = 0.0; n = 0 } }
        $coreAcc[$k].sum += $v; $coreAcc[$k].n++
    }
}
$classes = @($topo.map.Values | Sort-Object -Unique)
$cores = @(); $classAgg = [ordered]@{}
if ($coreAcc.Count -eq 0) { $notes['cores'] = Get-GroupWhy 'cores' }
if ($topo.map.Count -eq 0 -and $topo.reason) { $notes['core_class'] = "core_class unknown: $($topo.reason)" }
foreach ($k in @($coreAcc.Keys | Sort-Object { $a = $_ -split ','; ([int]$a[0]) * 100000 + ([int]$a[1]) })) {
    $eff = $(if ($topo.map.ContainsKey($k)) { [int]$topo.map[$k] } else { $null })
    $cls = $(if ($null -eq $eff) { 'unknown' } elseif ($classes.Count -le 1) { 'uniform' } elseif ($eff -eq ($classes | Measure-Object -Maximum).Maximum) { 'P' } else { 'E' })
    $pct = [math]::Round($coreAcc[$k].sum / $coreAcc[$k].n, 2)
    $cores += [pscustomobject]@{ instance = $k; core_class = $cls; efficiency_class = $eff; percent = $pct; percent_exact = ($coreAcc[$k].sum / $coreAcc[$k].n); validSamples = $coreAcc[$k].n; totalSamples = $nSets; incomplete = ($coreAcc[$k].n -lt $nSets) }
}
$coreInc = @($cores | Where-Object { $_.incomplete }).Count
if ($coreInc -gt 0) { $notes['cores_incomplete'] = "$coreInc of $($cores.Count) core rows have fewer valid samples than the $nSets sample sets; each is the mean of its valid samples" }
$coreOmitted = @($coreSeen.Keys | Where-Object { -not $coreAcc.ContainsKey($_) }).Count
if ($coreOmitted -gt 0) { $notes['cores_omitted'] = "$coreOmitted core(s) had no valid sample and are omitted" }
foreach ($c in ($cores | Group-Object core_class)) {
    $vals = @($c.Group | ForEach-Object { $_.percent })
    $classAgg[$c.Name] = [ordered]@{ count = $vals.Count; mean_percent = Get-Mean $vals; max_percent = ($vals | Measure-Object -Maximum).Maximum }
}
# HEAT / GPU / LEASE TELEMETRY (VENUE-PROBE-HEAT-GPU-TELEMETRY-1): RECORD-ONLY. It runs after the sample window and every v2 figure, and nothing reads it but the line.
# BACHELOR-HEAT-WHILE-QUIET-DIAG-1 r1: two long quiet-lease waits fit the heat gate while this probe saw nothing. Three blocks, each in its own try/catch, every read in a worker
# runspace with a deadline of its own ($TelemetryCallMs, abandoned past it):
#   heat{}  the agent's OWN reading of the heavy-slot feed (ultra-magnus-agent.ps1 Read-HeavySlotFeedMetrics + ConvertFrom-HeavySlotFeedRows, copied rather than re-derived: this
#           job is text run on the venue and cannot import the agent): HEAVY_SLOT_FEED, else C:\temp\hwinfo-log-adapter.csv; the last 150 rows; column 3 PackageC (the HOTTEST
#           core), 4 package W, 5 / 6 throttling "Yes". The last 60 rows (GateSustainedRows): throttled and mean_c; the last 15: max15_c and package_w (the agent's watts30);
#           hottest_core_c is the newest row. Older than 120 s (GateFeedStaleSec), fewer than 15 rows, or a row that does not parse: every value null, typed.
#   gpu{}   the dGPU (display-class PCI\VEN_10DE) POWER STATE FIRST, from DEVPKEY_Device_PowerData (CM_POWER_DATA; bytes 4-7 = PD_MostRecentPowerState, 1 = D0 .. 4 = D3). That
#           property is the PnP manager's record of the device's last power state: reading it sends the device no I/O and does not wake it (project memory
#           bachelor-dgpu-nvidia-smi-wake-trap-20261011: nvidia-smi / NVML WAKE a D3 dGPU and read a bogus state). ONLY in D0 (already awake) is nvidia-smi run, once; in D1-D3 or
#           unknown nothing else is read and skipped_wake_risk is true. No NVIDIA display adapter: state absent. MORE THAN ONE (r2, CoS B1): nvidia-smi has no device selector here and
#           would read every adapter, including ones whose power state was never checked, so nothing more is read: every field null, reason multi-dgpu-ambiguous. With exactly one,
#           nvidia-smi output of other than one non-blank row is null, reason nvidia-smi-ambiguous. One PnP power-state read precedes one nvidia-smi read; a D0->D3 change between
#           them is a known race, not excluded, and actual wake behaviour is not measured here.
#   seconds_since_last_lease + last_lease{}: the newest quiet-lease event in the agent's own log, <job working directory>\logs\agent.log (the agent starts every job in its root),
#           tail only: `quiet-lease: active`, `ended ... active=True`, `batch-opened|batch-reused|batch-parked|batch-ended`, `stale lease`. A request that never held a lease
#           (ended active=False) is not a lease.
# A null always has a typed reason in its block's `reasons` (field -> code); 0 is never an unknown. COST: 4 bounded calls (feed, log, power state; nvidia-smi only in D0), at most
# 4 x $TelemetryCallMs = 16 s, so 24 s sampling + 16 s + the process-attribution worst case below (55 s) = 95 s, inside the 120 s submission timeout. An abandoned worker
# ends the job after its line is written, as an abandoned attribution worker does.
# $TelemetryWorkerPrelude and $TelemetryReadDelayMs are TEST SEAMS (empty / 0 in production; only a test rewrites the literal in its own copy of the job).
$telemetrySchema = 'mlv-app/venue-quiet-probe-telemetry/v1'
$HeatFeedPath = $(if ($env:HEAVY_SLOT_FEED) { $env:HEAVY_SLOT_FEED } else { 'C:\temp\hwinfo-log-adapter.csv' })
$HeatWindowRows = 60
$HeatStaleSec = 120
$LeaseLogPath = ''
$TelemetryCallMs = 4000
$TelemetryWorkerPrelude = ''
$TelemetryReadDelayMs = 0
# the tail of one local file: $args[0] path, $args[1] max bytes, $args[2] the test seam delay. { ok, v = { exists, ageSec, text, partial }, msg }; never throws.
$TelTailScript = {
    try {
        if ([int]$args[2] -gt 0) { [Threading.Thread]::Sleep([int]$args[2]) }
        $fs = $null
        try { $fs = [IO.File]::Open([string]$args[0], [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete) }
        catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] { return [pscustomobject]@{ ok = $true; v = [pscustomobject]@{ exists = $false; ageSec = $null; text = $null; partial = $false }; msg = $null } }
        try {
            $age = ([DateTime]::UtcNow - [IO.File]::GetLastWriteTimeUtc([string]$args[0])).TotalSeconds
            $start = [Math]::Max([long]0, $fs.Length - [long]$args[1])
            [void]$fs.Seek($start, [IO.SeekOrigin]::Begin)
            $text = [IO.StreamReader]::new($fs, [Text.Encoding]::UTF8).ReadToEnd()
        } finally { $fs.Dispose() }
        [pscustomobject]@{ ok = $true; v = [pscustomobject]@{ exists = $true; ageSec = $age; text = $text; partial = ($start -gt 0) }; msg = $null }
    } catch { [pscustomobject]@{ ok = $false; v = $null; msg = [string]$_.Exception.Message } }
}.ToString()
# the dGPU power state, never anything that touches the device: { ok, v = { id, data }, msg }
$TelGpuStateScript = {
    $id = $null
    try {
        $all = @(Get-PnpDevice -Class Display -PresentOnly -ErrorAction Stop)
        $d = @($all | Where-Object { [string]$_.InstanceId -like 'PCI\VEN_10DE*' } | Sort-Object InstanceId)
        if ($d.Count -eq 0) { return [pscustomobject]@{ ok = $true; v = [pscustomobject]@{ id = $null; data = $null; count = 0 }; msg = $null } }
        # more than one NVIDIA adapter: nvidia-smi (no device selector) would read all of them, so no one adapter's power state can vouch for the reading. Read nothing more.
        if ($d.Count -gt 1) { return [pscustomobject]@{ ok = $true; v = [pscustomobject]@{ id = $null; data = $null; count = $d.Count }; msg = $null } }
        $id = [string]$d[0].InstanceId
        $p = Get-PnpDeviceProperty -InstanceId $id -KeyName 'DEVPKEY_Device_PowerData' -ErrorAction Stop
        [pscustomobject]@{ ok = $true; v = [pscustomobject]@{ id = $id; data = [byte[]]@($p.Data); count = 1 }; msg = $null }
    } catch { [pscustomobject]@{ ok = $false; v = [pscustomobject]@{ id = $id; data = $null }; msg = [string]$_.Exception.Message } }
}.ToString()
# ONLY called for a dGPU already in D0: one nvidia-smi query. { ok, v = { rc, lines }, msg }
$TelSmiScript = {
    try {
        $out = @(& 'nvidia-smi' '--query-gpu=utilization.gpu,temperature.gpu,power.draw,pstate' '--format=csv,noheader,nounits' 2>&1 | ForEach-Object { [string]$_ })
        [pscustomobject]@{ ok = $true; v = [pscustomobject]@{ rc = $(if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }); lines = $out }; msg = $null }
    } catch { [pscustomobject]@{ ok = $false; v = $null; msg = [string]$_.Exception.Message } }
}.ToString()
$script:TelRs = $null
$script:TelAbandoned = 0
# one call with a deadline of its own: { timeout, ok, v, msg }. A call past the deadline is ABANDONED (its worker dies with the process; the next call gets a fresh one).
function Invoke-TelBounded([string]$Script, [object[]]$ArgList, [int]$TimeoutMs) {
    $ps = [powershell]::Create()
    try {
        if ($null -eq $script:TelRs) {
            $rs = [runspacefactory]::CreateRunspace(); $rs.Open()
            if ($TelemetryWorkerPrelude) { $pp = [powershell]::Create(); try { $pp.Runspace = $rs; [void]$pp.AddScript($TelemetryWorkerPrelude); [void]$pp.Invoke() } finally { $pp.Dispose() } }
            $script:TelRs = $rs
        }
        $ps.Runspace = $script:TelRs
        [void]$ps.AddScript($Script)
        foreach ($a in $ArgList) { [void]$ps.AddArgument($a) }
        $ar = $ps.BeginInvoke()
        if (-not $ar.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            $script:TelRs = $null; $script:TelAbandoned++
            try { [void]$ps.BeginStop($null, $null) } catch { }
            $ps = $null
            return [pscustomobject]@{ timeout = $true; ok = $false; v = $null; msg = "timed out after $TimeoutMs ms" }
        }
        $o = @($ps.EndInvoke($ar)) | Select-Object -Last 1
        if ($null -eq $o) { return [pscustomobject]@{ timeout = $false; ok = $false; v = $null; msg = 'the worker returned nothing' } }
        [pscustomobject]@{ timeout = $false; ok = [bool]$o.ok; v = $o.v; msg = [string]$o.msg }
    } catch {
        [pscustomobject]@{ timeout = $false; ok = $false; v = $null; msg = [string]$_.Exception.Message }
    } finally { if ($null -ne $ps) { $ps.Dispose() } }
}
# every still-null field of a block gets the code (a field that has a value never does)
function Set-TelNulls($Block, [string[]]$Fields, [string]$Code) { foreach ($f in $Fields) { if ($null -eq $Block[$f] -and -not $Block.reasons.Contains($f)) { $Block.reasons[$f] = $Code } } }
# PURE, the agent's ConvertFrom-HeavySlotFeedRows: @{ m; why }. m = $null when a data row does not parse (feed-unparseable) or fewer than 15 data rows remain (feed-too-few-rows).
function ConvertFrom-TelFeedRows([string[]]$Lines) {
    $inv = [Globalization.CultureInfo]::InvariantCulture; $num = [Globalization.NumberStyles]::Float
    $recs = [Collections.Generic.List[object]]::new()
    foreach ($r in @($Lines | Select-Object -Last 150)) {
        if ([string]::IsNullOrEmpty($r)) { continue }
        $c = $r.Split(',')
        if ($c.Count -lt 7 -or $c[0].Length -lt 1 -or -not [char]::IsDigit($c[0], 0)) { continue }
        $t = 0.0; $w = 0.0
        if (-not [double]::TryParse($c[3], $num, $inv, [ref]$t)) { return @{ m = $null; why = 'feed-unparseable' } }
        if (-not [double]::TryParse($c[4], $num, $inv, [ref]$w)) { return @{ m = $null; why = 'feed-unparseable' } }
        $recs.Add([pscustomobject]@{ c = $t; w = $w; thr = ($c[5].Trim() -eq 'Yes' -or $c[6].Trim() -eq 'Yes') })
    }
    if ($recs.Count -lt 15) { return @{ m = $null; why = 'feed-too-few-rows' } }
    $win = @($recs | Select-Object -Last $HeatWindowRows); $last15 = @($recs | Select-Object -Last 15)
    @{ why = $null; m = [pscustomobject]@{ window = $win.Count; throttled = @($win | Where-Object { $_.thr }).Count; meanC = ($win | Measure-Object -Property c -Average).Average
        max15C = ($last15 | Measure-Object -Property c -Maximum).Maximum; watts30 = [Math]::Round(($last15 | Measure-Object -Property w -Average).Average, 1); lastC = $recs[$recs.Count - 1].c } }
}
$heatFields = @('sample_age_s', 'hottest_core_c', 'max15_c', 'mean_c', 'throttled', 'window_rows', 'package_w')
$telHeat = [ordered]@{ source = 'unavailable'; sample_age_s = $null; hottest_core_c = $null; max15_c = $null; mean_c = $null; throttled = $null; window_rows = $null; package_w = $null
    window_rule_rows = $HeatWindowRows; stale_after_s = $HeatStaleSec; reasons = [ordered]@{}; why = $null }
try {
    $w = Invoke-TelBounded $TelTailScript @($HeatFeedPath, (80 * 150 + 400), $TelemetryReadDelayMs) $TelemetryCallMs
    $code = $null
    if ($w.timeout) { $code = 'timeout'; $telHeat.why = "feed read exceeded $TelemetryCallMs ms ($HeatFeedPath)" }
    elseif (-not $w.ok) { $code = 'feed-read-failed'; $telHeat.why = ([string]$w.msg -replace '\s+', ' ') }
    elseif (-not $w.v.exists) { $code = 'feed-missing'; $telHeat.why = "no feed at $HeatFeedPath" }
    else {
        $telHeat.source = $HeatFeedPath; $telHeat.sample_age_s = [math]::Round([double]$w.v.ageSec, 1)
        if ([double]$w.v.ageSec -gt $HeatStaleSec) { $code = 'feed-stale'; $telHeat.why = "feed older than $HeatStaleSec s" }
        else {
            $lines = @(([string]$w.v.text) -split "`r?`n")
            if ($w.v.partial) { $lines = @($lines | Select-Object -Skip 1) }   # the first line read mid-row is partial
            $r = ConvertFrom-TelFeedRows $lines
            if ($null -eq $r.m) { $code = $r.why }
            else {
                $telHeat.hottest_core_c = [double]$r.m.lastC; $telHeat.max15_c = [double]$r.m.max15C; $telHeat.mean_c = [math]::Round([double]$r.m.meanC, 2)
                $telHeat.throttled = [int]$r.m.throttled; $telHeat.window_rows = [int]$r.m.window; $telHeat.package_w = [double]$r.m.watts30
            }
        }
    }
    if ($code) { Set-TelNulls $telHeat $heatFields $code }
} catch { $telHeat.why = ([string]$_.Exception.Message -replace '\s+', ' '); Set-TelNulls $telHeat $heatFields 'telemetry-error' }
$gpuFields = @('utilization_percent', 'temp_c', 'power_w', 'pstate')
$telGpu = [ordered]@{ method = 'DEVPKEY_Device_PowerData'; vendor_filter = 'VEN_10DE'; device = $null; state = 'unknown'; power_state_raw = $null; state_reason = $null
    skipped_wake_risk = $true; utilization_percent = $null; temp_c = $null; power_w = $null; pstate = $null; reasons = [ordered]@{}; why = $null }
try {
    $w = Invoke-TelBounded $TelGpuStateScript @() $TelemetryCallMs
    if ($w.timeout) { $telGpu.state_reason = 'timeout'; $telGpu.why = "power state read exceeded $TelemetryCallMs ms" }
    elseif (-not $w.ok) {
        $telGpu.device = $(if ($null -ne $w.v) { $w.v.id } else { $null }); $telGpu.why = ([string]$w.msg -replace '\s+', ' ')
        if ($null -eq $telGpu.device -and [string]$w.msg -match '(?i)no matching') { $telGpu.state = 'absent' } else { $telGpu.state_reason = 'pnp-read-failed' }
    }
    elseif ($null -eq $w.v.id -and [int]$w.v.count -gt 1) { $telGpu.state_reason = 'multi-dgpu-ambiguous'; $telGpu.why = "$($w.v.count) NVIDIA display adapters; nvidia-smi cannot be bound to one" }
    elseif ($null -eq $w.v.id) { $telGpu.state = 'absent' }
    else {
        $telGpu.device = [string]$w.v.id
        $data = [byte[]]@($w.v.data)
        $raw = $(if ($data.Count -ge 8) { [BitConverter]::ToInt32($data, 4) } else { $null })
        $telGpu.power_state_raw = $raw
        if ($null -ne $raw -and $raw -ge 1 -and $raw -le 4) { $telGpu.state = "D$($raw - 1)" } else { $telGpu.state_reason = 'unspecified-power-state' }
    }
    if ($telGpu.state -ceq 'absent') { $telGpu.skipped_wake_risk = $false; $telGpu.reasons['device'] = 'no-dgpu'; Set-TelNulls $telGpu $gpuFields 'no-dgpu' }
    elseif ($telGpu.state_reason -ceq 'multi-dgpu-ambiguous') { Set-TelNulls $telGpu $gpuFields 'multi-dgpu-ambiguous' }
    elseif ($telGpu.state -cne 'D0') { Set-TelNulls $telGpu $gpuFields 'skipped-wake-risk' }
    else {
        # D0 was the last recorded PnP power state, so nvidia-smi is read once; a D0->D3 change between the two reads is a known race (see header), not excluded
        $telGpu.skipped_wake_risk = $false
        $s = Invoke-TelBounded $TelSmiScript @() $TelemetryCallMs
        $code = $null
        if ($s.timeout) { $code = 'timeout' }
        elseif (-not $s.ok) { $code = $(if ([string]$s.msg -match '(?i)not recognized|not found') { 'nvidia-smi-missing' } else { 'nvidia-smi-failed' }); $telGpu.why = ([string]$s.msg -replace '\s+', ' ') }
        elseif ([int]$s.v.rc -ne 0) { $code = 'nvidia-smi-failed'; $telGpu.why = "nvidia-smi exit $($s.v.rc)" }
        else {
            $rows = @($s.v.lines | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
            $parts = @(([string]($rows | Select-Object -First 1)).Split(',') | ForEach-Object { $_.Trim() })
            if ($rows.Count -ne 1) { $code = 'nvidia-smi-ambiguous'; $telGpu.why = "nvidia-smi returned $($rows.Count) rows for the one checked adapter" }
            elseif ($parts.Count -lt 4) { $code = 'nvidia-smi-unparseable' }
            else {
                $inv = [Globalization.CultureInfo]::InvariantCulture
                for ($i = 0; $i -lt 3; $i++) {
                    $f = $gpuFields[$i]; $v = 0.0
                    if ([double]::TryParse($parts[$i], [Globalization.NumberStyles]::Float, $inv, [ref]$v)) { $telGpu[$f] = $v }
                    else { $telGpu.reasons[$f] = $(if ($parts[$i] -match '^\[.*\]$') { 'not-supported' } else { 'nvidia-smi-unparseable' }) }
                }
                if ($parts[3] -cmatch '^P\d{1,2}$') { $telGpu.pstate = $parts[3] } else { $telGpu.reasons['pstate'] = 'not-supported' }
            }
        }
        if ($code) { Set-TelNulls $telGpu $gpuFields $code }
    }
} catch { $telGpu.why = ([string]$_.Exception.Message -replace '\s+', ' '); Set-TelNulls $telGpu $gpuFields 'telemetry-error' }
$secondsSinceLease = $null
$leaseLog = $(if ($LeaseLogPath) { $LeaseLogPath } else { Join-Path ([Environment]::CurrentDirectory) 'logs\agent.log' })
$telLease = [ordered]@{ source = $leaseLog; event = $null; utc = $null; reason = $null; why = $null }
try {
    $w = Invoke-TelBounded $TelTailScript @($leaseLog, 262144, $TelemetryReadDelayMs) $TelemetryCallMs
    if ($w.timeout) { $telLease.reason = 'timeout' }
    elseif (-not $w.ok) { $telLease.reason = 'log-read-failed'; $telLease.why = ([string]$w.msg -replace '\s+', ' ') }
    elseif (-not $w.v.exists) { $telLease.reason = 'log-missing' }
    else {
        $lines = @(([string]$w.v.text) -split "`r?`n")
        if ($w.v.partial) { $lines = @($lines | Select-Object -Skip 1) }
        $hit = $null
        for ($i = $lines.Count - 1; $i -ge 0 -and $null -eq $hit; $i--) {
            if ($lines[$i] -notmatch '^(\d{4}-\d\d-\d\dT\S+) quiet-lease: (active|ended|batch-opened|batch-reused|batch-parked|batch-ended|stale lease) (.*)$') { continue }
            $ts = $Matches[1]; $ev = $Matches[2]; $rest = $Matches[3]
            if ($ev -ceq 'ended' -and $rest -notmatch '(^| )active=True( |$)') { continue }
            $hit = @{ ts = $ts; event = $ev }
        }
        if ($null -eq $hit) { $telLease.reason = 'no-lease-record'; $telLease.why = "no quiet-lease event in the last $($lines.Count) line(s)" }
        else {
            $telLease.event = $hit.event
            $dto = [DateTimeOffset]::MinValue
            if (-not [DateTimeOffset]::TryParse($hit.ts, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$dto)) { $telLease.reason = 'unparseable-timestamp' }
            else {
                $telLease.utc = $dto.UtcDateTime.ToString('o')
                $age = ([DateTime]::UtcNow - $dto.UtcDateTime).TotalSeconds
                if ($age -lt -5) { $telLease.reason = 'future-timestamp' } else { $secondsSinceLease = [math]::Round([math]::Max(0.0, $age), 1) }
            }
        }
    }
} catch { $telLease.reason = 'telemetry-error'; $telLease.why = ([string]$_.Exception.Message -replace '\s+', ' ') }
if ($null -ne $script:TelRs) { try { $script:TelRs.Dispose() } catch { }; $script:TelRs = $null }
# IDENTITY (VENUE-PROBE-PROCESS-ATTRIBUTION-1): WHO (owner, project) and WHAT (script, a command line with no argument text, parent chain, start time, GPU) for the processes named above.
# It runs AFTER the sample window and after every v2 figure (never inside the sample loop, no new sleep), decides nothing, and sits in ONE try/catch: on any failure the line
# carries identity = { schema, error } and every other field is unchanged. The GPU read comes FIRST here, not last, because its top pids are CIM targets too (one extra WQL
# query would break the 3-query bound). Bound: at most 3 WQL queries (targets, their parents, their grandparents) + GetOwner per target + ONE GPU Engine read.
$IdentityGpu = $true
$IdentityGpuDisabledWhy = ''
# WALL-CLOCK BUDGET: identity must never push the probe job toward its 120 s submission timeout (a timed-out probe is UNKNOWN, a changed decision). Past this many ms of identity
# time no further GetOwner call is made: the remaining rows say owner_status budget-exceeded. Command line, parent chain and project do not need a call and are still filled.
$IdentityBudgetMs = 25000
# CALL DEADLINE: the budget above gates only LATER GetOwner calls, so each synchronous call (the GPU Engine read, every Win32_Process query, every GetOwner) also has a deadline of its own.
# Each runs in a WORKER RUNSPACE and is abandoned after this many ms: a GPU read becomes gpu_status `timeout: ...`, a GetOwner becomes owner_status `timeout`, the script-slot check
# leaves every unanswered path script_check `timeout`, a Win32_Process query becomes identity = { schema, error } (the same typed degradation as any other identity failure). The
# measured samples, top and every v2 figure are read BEFORE any of this and never change.
# Worst case: GPU + 3 queries + the script check + the budget-gated GetOwner calls + one more = 5 x 5 s + 25 s + 5 s = 55 s on top of the 24 s sample window, inside the 120 s
# submission timeout. A worker thread stuck in a native call is a FOREGROUND thread, so the job ends with [Environment]::Exit after its line is written when any call was abandoned.
# $IdentityWorkerPrelude is PowerShell run once in each new worker (empty in production): the workers are separate runspaces, so it is the seam a test uses to re-declare its shadows there.
# $IdentityScriptCheckDelayMs is a TEST SEAM (0 in production; only a test rewrites this literal in its own copy of the job): the worker sleeps that long before each path's
# metadata reads, standing in for a slow or dead volume. Nothing on the host (no environment variable, no file) can set it.
$IdentityCallMs = 5000
$IdentityWorkerPrelude = ''
$IdentityScriptCheckDelayMs = 0
$identitySchema = 'mlv-app/venue-quiet-probe-identity/v1'
# the pinned project table: first match wins, case-insensitive substring on the UNREDACTED command line + executable path (held in memory only; only the name is emitted)
$identityProjects = @(
    @('\mlvtmp\', 'mlv-app'), @('\mlv-agent\', 'mlv-app'), @('MLV-App', 'mlv-app'), @('ultra-magnus-agent', 'mlv-app'), @('um-run', 'mlv-app'),
    @('agent-bridge', 'agent-bridge'), @('Adobe Document Cloud Ingester', 'adobe-ingester'), @('softwarefactory-fleet-doctrine', 'fleet-doctrine'),
    @('AdversarialLLM', 'adversarialllm'), @('VIRTUAL-TEN', 'virtual-ten'), @('AirMyPC', 'airmypc')
)
# REDACTION IS AN ALLOWLIST (r5): NO ARGUMENT TEXT IS PERSISTED. The r4 token denylist still leaked (`--signature x`, `?sig=x`, `-u user:x`, and a quoted value promoted to the
# script slot), so no argument text is kept on ANY line, whatever the line holds:
#   1. cmdline = `<exe> args=<n> script=<path|<none>>` and nothing else. exe is ExecutablePath, else the process image Name (both come from the OS, never from the argument text),
#      else <unknown-exe>; n counts the arguments after argv[0] (Windows argv rules, Split-IdentArgv).
#   2. script comes ONLY from the FIRST argument after the executable, never a later one (for pwsh / powershell whose first argument is a literal -File, the argument after it: the
#      one host switch whose next argument is a script by definition). It is filled only when that argument is a drive-rooted path (so it starts with neither '-' nor '/'; a
#      relative path cannot be checked, the working directory is not collected) holding no whitespace, '=', '@', '?', '#', quote or ':' past the drive letter, ending in
#      .ps1 .psm1 .py .js .mjs .cmd .bat, on a FIXED local drive, reached through NO reparse point, and naming a file that EXISTS on this host. The text test runs here; every
#      filesystem call runs in the bounded worker (Test-IdentScriptPaths; nothing is written). Otherwise <none>, and script_check says why.
#      r7: the text test also rejects a '.' or '..' segment, any '/', a doubled '\' after the drive root, a trailing separator, a \\?\ or \\.\ prefix and an 8.3 '~<digit>'
#      name, and what is persisted is the WORKER's validated full path, only when it equals the slot text case-insensitively: the slot text itself is never persisted.
#   3. the row's `script` field is that same path or null; then the 200-character cap. The token scan survives ONLY as the diagnostic integer cmdline_token_hits and gates
#      nothing. Project attribution reads the unredacted line in memory only (Get-IdentProject) and emits a table name, never text.
$identSecretTokens = @('pass', 'pwd', 'pw', 'secret', 'token', 'key', 'auth', 'credential', 'bearer', 'cookie', 'session')
$identSecretShapes = '(?i)[a-z][a-z0-9+.-]*://[^/@\s]+@|(?:sk-|ghp_|gho_|ghs_|github_pat_|xox[abps]-|AKIA)[A-Za-z0-9_-]{10,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.|[A-Za-z0-9+/=]{40,}|(?-i:(?<![\w-])-p)'
# diagnostic only: how many of the tokens and shapes the RAW line holds (case-insensitive substring scan). It decides nothing about what is written.
function Measure-IdentTokenHits($Text) {
    $s = [string]$Text; $n = 0
    foreach ($t in $identSecretTokens) { if ($s.IndexOf($t, [StringComparison]::OrdinalIgnoreCase) -ge 0) { $n++ } }
    $n + [regex]::Matches($s, $identSecretShapes).Count
}
# the 200-character cap for a path or a name (no rule rewrites it): { text, truncated }
function Limit-IdentText($Text) {
    if ($null -eq $Text) { return [pscustomobject]@{ text = $null; truncated = 0 } }
    $s = [string]$Text; $cut = 0
    if ($s.Length -gt 200) { $cut = $s.Length - 200; $s = $s.Substring(0, 200) + "...[+$cut]" }
    [pscustomobject]@{ text = $s; truncated = $cut }
}
# Windows argv rules (CommandLineToArgvW): argv[0] runs to the next quote when quoted, else to whitespace; later arguments honour 2n / 2n+1 backslashes before a quote and "" inside
# quotes. Returns { argv, rest } where rest is the raw text after argv[0] (held in memory; never persisted).
function Split-IdentArgv([string]$s) {
    $argv = New-Object System.Collections.Generic.List[string]
    $q = [char]34; $bsl = [char]92; $i = 0; $n = $s.Length
    while ($i -lt $n -and [char]::IsWhiteSpace($s[$i])) { $i++ }
    $sb = New-Object Text.StringBuilder
    if ($i -lt $n -and $s[$i] -eq $q) { $i++; while ($i -lt $n -and $s[$i] -ne $q) { [void]$sb.Append($s[$i]); $i++ }; if ($i -lt $n) { $i++ } }
    else { while ($i -lt $n -and -not [char]::IsWhiteSpace($s[$i])) { [void]$sb.Append($s[$i]); $i++ } }
    $argv.Add($sb.ToString())
    $rest = $s.Substring($i).Trim()
    while ($true) {
        while ($i -lt $n -and [char]::IsWhiteSpace($s[$i])) { $i++ }
        if ($i -ge $n) { break }
        $sb = New-Object Text.StringBuilder; $inQ = $false
        while ($i -lt $n) {
            $c = $s[$i]
            if (-not $inQ -and [char]::IsWhiteSpace($c)) { break }
            if ($c -eq $bsl) {
                $nb = 0; while ($i -lt $n -and $s[$i] -eq $bsl) { $nb++; $i++ }
                if ($i -lt $n -and $s[$i] -eq $q) { [void]$sb.Append($bsl, [int][math]::Floor($nb / 2)); if ($nb % 2 -eq 1) { [void]$sb.Append($q); $i++ } }
                else { [void]$sb.Append($bsl, $nb) }
                continue
            }
            if ($c -eq $q) {
                if ($inQ -and $i + 1 -lt $n -and $s[$i + 1] -eq $q) { [void]$sb.Append($q); $i += 2; continue }
                $inQ = -not $inQ; $i++; continue
            }
            [void]$sb.Append($c); $i++
        }
        $argv.Add($sb.ToString())
    }
    [pscustomobject]@{ argv = $argv.ToArray(); rest = $rest }
}
# rule 2 above, as text: drive-rooted, no whitespace = @ ? # quote or ':' past the drive letter, one of the seven script extensions
$identScriptShape = '(?i)^[a-z]:[\\/][^\s=@?#:"''*<>|]+\.(?:ps1|psm1|py|js|mjs|cmd|bat)$'
# the same, NOT drive-rooted (no drive letter, so no ':' at all): a relative path (the working directory is not collected) or, with a leading '\\' or '//', a UNC path
$identScriptRelShape = '(?i)^[^\s=@?#:"''*<>|-][^\s=@?#:"''*<>|]*\.(?:ps1|psm1|py|js|mjs|cmd|bat)$'
# r7: spellings that name a file other than the text (GetFullPath would rewrite them), each rejected before any worker call: a \\?\ or \\.\ prefix, any '/', a '.' or '..'
# segment, a doubled '\' after the drive root, a trailing separator, an 8.3 short name ('~' then a digit)
$identScriptGhostShape = '^[\\/]{2}[?.][\\/]|/|(?:^|\\)\.{1,2}(?:\\|$)|^[A-Za-z]:.*\\\\|[\\/]$|~[0-9]'
# rule 2 as TEXT ONLY (no filesystem call here): ONLY the first argument after the executable (or, for pwsh / powershell, the one after a first-slot -File), never a later one.
# Returns { path, check }: path is the candidate the bounded worker must check (check $null), else path is $null and check is final: rejected (no slot argument, or its text fails
# the allowlist or matches a ghost shape), relative, or nonfixed (a UNC path is never a fixed local drive, so it is never touched).
function Get-IdentScriptSlot($Argv, [string]$ExeLeaf) {
    $j = 1
    if ($ExeLeaf -match '(?i)^(pwsh|powershell)(\.exe)?$' -and $Argv.Count -gt 2 -and [string]$Argv[1] -ieq '-File') { $j = 2 }
    if ($Argv.Count -le $j) { return @{ path = $null; check = 'rejected' } }
    $p = [string]$Argv[$j]
    if ($p -match $identScriptGhostShape) { return @{ path = $null; check = 'rejected' } }
    if ($p -match $identScriptShape) { return @{ path = $p; check = $null } }
    if ($p -match $identScriptRelShape) { return @{ path = $null; check = $(if ($p -match '^[\\/]{2}') { 'nonfixed' } else { 'relative' }) } }
    @{ path = $null; check = 'rejected' }
}
# argv, exe and the rule-2 slot of one command line, as text
function Read-IdentCmdLine($CmdLine, $ExePath, $ImageName) {
    $argv = @((Split-IdentArgv ([string]$CmdLine)).argv)
    $exe = $(if (-not [string]::IsNullOrEmpty([string]$ExePath)) { [string]$ExePath } elseif (-not [string]::IsNullOrEmpty([string]$ImageName)) { [string]$ImageName } else { '<unknown-exe>' })
    $leaf = $(if ($exe -cne '<unknown-exe>') { $exe } else { [string]$argv[0] }) -replace '^.*[\\/]', ''
    [pscustomobject]@{ argv = $argv; exe = $exe; slot = (Get-IdentScriptSlot $argv $leaf) }
}
# the persisted command line (contract above): { text, script, script_check, redactions, truncated, args, token_hits }. No argument text reaches `text` except the rule-2 script
# path, and only when the worker's check of it ($ScriptChecks: path -> { check, full }, from Test-IdentScriptPaths) is ok; a candidate the worker never answered is error.
# r7: the slot text is only a lookup key and a comparand here. What is persisted is the worker's validated full path, and only when it equals the slot text case-insensitively.
function Protect-IdentCmdLine($CmdLine, $ExePath, $ImageName, $ScriptChecks) {
    $raw = [string]$CmdLine
    $rc = Read-IdentCmdLine $raw $ExePath $ImageName
    $check = $rc.slot.check; $script = $null
    if ($null -eq $check) {
        $v = $(if ($null -ne $ScriptChecks -and $ScriptChecks.ContainsKey($rc.slot.path)) { $ScriptChecks[$rc.slot.path] } else { $null })
        $check = $(if ($null -ne $v) { [string]$v.check } else { 'error' })
        if ($check -ceq 'ok') { if ([string]$v.full -ieq $rc.slot.path) { $script = [string]$v.full } else { $check = 'rejected' } }
    }
    $nArgs = $rc.argv.Count - 1
    $lim = Limit-IdentText ("$($rc.exe) args=$nArgs script=" + $(if ($script) { $script } else { '<none>' }))
    [pscustomobject]@{
        text = $lim.text; script = $(if ($script) { (Limit-IdentText $script).text } else { $null }); script_check = $check; redactions = $nArgs - $(if ($script) { 1 } else { 0 })
        truncated = $lim.truncated; args = $nArgs; token_hits = (Measure-IdentTokenHits $raw)
    }
}
function Get-IdentProject($Row) {
    if ($null -eq $Row) { return $null }
    $t = "$($Row.CommandLine) $($Row.ExecutablePath)"
    foreach ($e in $identityProjects) { if ($t.IndexOf($e[0], [StringComparison]::OrdinalIgnoreCase) -ge 0) { return $e[1] } }
    $null
}
# The three bounded calls, as the text each worker runs ($args[0] is the call's one argument). Each returns { ok, v, msg } and never throws.
$IdentGpuScript = {
    try { [pscustomobject]@{ ok = $true; v = @((Get-Counter -Counter '\GPU Engine(*)\Utilization Percentage' -MaxSamples 1 -ErrorAction SilentlyContinue).CounterSamples | Where-Object { $null -ne $_ }); msg = $null } }
    catch { [pscustomobject]@{ ok = $false; v = $null; msg = [string]$_.Exception.Message } }
}.ToString()
$IdentCimScript = {
    try { [pscustomobject]@{ ok = $true; v = @(Get-CimInstance -ClassName Win32_Process -Filter $args[0] -ErrorAction Stop); msg = $null } }
    catch { [pscustomobject]@{ ok = $false; v = $null; msg = [string]$_.Exception.Message } }
}.ToString()
$IdentOwnerScript = {
    try { $o = Invoke-CimMethod -InputObject $args[0] -MethodName GetOwner -ErrorAction Stop; [pscustomobject]@{ ok = $true; v = [pscustomobject]@{ ReturnValue = [int]$o.ReturnValue; Domain = [string]$o.Domain; User = [string]$o.User }; msg = $null } }
    catch { [pscustomobject]@{ ok = $false; v = $null; msg = [string]$_.Exception.Message } }
}.ToString()
# The script-slot check: the ONLY place a script candidate touches the filesystem. $args[0] = the candidate paths, $args[1] = a synchronized table it fills
# path -> { check, full } AS IT GOES (so a deadline keeps what was already answered), $args[2] = the test seam delay. Per path: the full path must equal the candidate
# case-insensitively (r7: GetFullPath expands 8.3 names, so it runs here; else rejected), the drive must be Fixed (else nonfixed); then each component from the drive root to
# the file has its attributes read ONCE (a GetAttributes call does not follow the component it names): a ReparsePoint stops the walk unfollowed (reparse), a missing
# component, a non-directory before the end or a directory at the end is notfound, anything else that throws is error. full is the validated path, set only for ok.
$IdentScriptCheckScript = {
    $paths = @($args[0]); $out = $args[1]; $delayMs = [int]$args[2]
    try {
        foreach ($p in $paths) {
            $r = 'error'; $full = $null
            try {
                if ($delayMs -gt 0) { [Threading.Thread]::Sleep($delayMs) }
                $full = [IO.Path]::GetFullPath([string]$p)
                if ($full -notmatch '^[A-Za-z]:\\' -or $full -ine [string]$p) { $r = 'rejected' }
                elseif (([IO.DriveInfo]::new($full.Substring(0, 1))).DriveType -ne [IO.DriveType]::Fixed) { $r = 'nonfixed' }
                else {
                    $parts = @($full.Substring(3).Split([char[]]@('\'), [StringSplitOptions]::RemoveEmptyEntries))
                    $r = $(if ($parts.Count -gt 0) { 'ok' } else { 'notfound' })
                    $cur = $full.Substring(0, 2)
                    for ($i = 0; $i -lt $parts.Count; $i++) {
                        $cur = $cur + '\' + $parts[$i]
                        try { $a = [IO.File]::GetAttributes($cur) } catch [IO.FileNotFoundException], [IO.DirectoryNotFoundException] { $r = 'notfound'; break }
                        if ($a -band [IO.FileAttributes]::ReparsePoint) { $r = 'reparse'; break }
                        if ([bool]($a -band [IO.FileAttributes]::Directory) -ne ($i -lt $parts.Count - 1)) { $r = 'notfound'; break }
                    }
                }
            } catch { $r = 'error' }
            $out[[string]$p] = @{ check = $r; full = $(if ($r -ceq 'ok') { $full } else { $null }) }
        }
        [pscustomobject]@{ ok = $true; v = $null; msg = $null }
    } catch { [pscustomobject]@{ ok = $false; v = $null; msg = [string]$_.Exception.Message } }
}.ToString()
$script:IdentRs = $null
$script:IdentAbandoned = 0
function Get-IdentWorker {
    if ($null -eq $script:IdentRs) {
        $rs = [runspacefactory]::CreateRunspace()
        $rs.Open()
        if ($IdentityWorkerPrelude) { $pp = [powershell]::Create(); try { $pp.Runspace = $rs; [void]$pp.AddScript($IdentityWorkerPrelude); [void]$pp.Invoke() } finally { $pp.Dispose() } }
        $script:IdentRs = $rs
    }
    $script:IdentRs
}
# one call with a deadline of its own: { timeout, ok, v, msg }. A call past the deadline is ABANDONED (its worker is left to die with the process and the next call gets a fresh one).
function Invoke-IdentBounded([string]$Script, [object[]]$ArgList, [int]$TimeoutMs) {
    $ps = [powershell]::Create()
    try {
        $ps.Runspace = Get-IdentWorker
        [void]$ps.AddScript($Script)
        foreach ($a in $ArgList) { [void]$ps.AddArgument($a) }
        $ar = $ps.BeginInvoke()
        if (-not $ar.AsyncWaitHandle.WaitOne($TimeoutMs)) {
            $script:IdentRs = $null; $script:IdentAbandoned++
            try { [void]$ps.BeginStop($null, $null) } catch { }
            $ps = $null
            return [pscustomobject]@{ timeout = $true; ok = $false; v = $null; msg = "timed out after $TimeoutMs ms" }
        }
        $o = @($ps.EndInvoke($ar)) | Select-Object -Last 1
        if ($null -eq $o) { return [pscustomobject]@{ timeout = $false; ok = $false; v = $null; msg = 'the worker returned nothing' } }
        [pscustomobject]@{ timeout = $false; ok = [bool]$o.ok; v = $o.v; msg = [string]$o.msg }
    } catch {
        [pscustomobject]@{ timeout = $false; ok = $false; v = $null; msg = [string]$_.Exception.Message }
    } finally { if ($null -ne $ps) { $ps.Dispose() } }
}
function Close-IdentWorker { if ($null -ne $script:IdentRs) { try { $script:IdentRs.Dispose() } catch { }; $script:IdentRs = $null } }
# ONE WQL query for a set of pids (validated integers, so nothing but digits reaches the filter); never a full-table enumeration
function Get-IdentCim($Ids) {
    $h = @{}
    $ids = @($Ids | ForEach-Object { [int64]$_ } | Sort-Object -Unique)
    if ($ids.Count -eq 0) { return $h }
    $filter = ($ids | ForEach-Object { "ProcessId=$_" }) -join ' OR '
    $csw = [Diagnostics.Stopwatch]::StartNew()
    try { $w = Invoke-IdentBounded $IdentCimScript @($filter) $IdentityCallMs } finally { $csw.Stop(); $script:IdentCimMs += $csw.ElapsedMilliseconds; $script:IdentCimQueries++ }
    if ($w.timeout) { throw "Win32_Process query timed out after $IdentityCallMs ms" }
    if (-not $w.ok) { throw [string]$w.msg }
    foreach ($r in @($w.v)) { if ($null -ne $r) { $h["$([int64]$r.ProcessId)"] = $r } }
    $h
}
function Get-IdentOwner($Row) {
    $osw = [Diagnostics.Stopwatch]::StartNew()
    try {
        $w = Invoke-IdentBounded $IdentOwnerScript @($Row) $IdentityCallMs
        if ($w.timeout) { return @{ owner = $null; status = 'timeout' } }
        if (-not $w.ok) {
            $msg = [string]$w.msg
            if ($msg -match '(?i)access is denied|access denied|0x80041003') { return @{ owner = $null; status = 'access-denied' } }
            if ($msg -match '(?i)not found|0x80041002|invalid object') { return @{ owner = $null; status = 'exited' } }
            return @{ owner = $null; status = 'error-exception' }
        }
        $r = $w.v
        $rv = [int]$r.ReturnValue
        if ($rv -eq 0 -and -not [string]::IsNullOrEmpty([string]$r.User)) { return @{ owner = $(if ([string]$r.Domain) { "$($r.Domain)\$($r.User)" } else { [string]$r.User }); status = 'ok' } }
        if ($rv -eq 2) { return @{ owner = $null; status = 'access-denied' } }
        return @{ owner = $null; status = "error-$rv" }
    } finally { $osw.Stop(); $script:IdentOwnerMs += $osw.ElapsedMilliseconds }
}
# ONE bounded worker call for every script candidate: path -> { check, full }. As the call returns or times out, the worker's table is copied ONCE under its lock (the lock its
# writes take), and only that copy is read: an answer the abandoned worker writes after the copy is never seen. A path missing from the copy is timeout after a deadline, else
# error (the worker failed before reaching it).
function Test-IdentScriptPaths($Paths) {
    $res = @{}
    $ps = @($Paths | Where-Object { $_ } | ForEach-Object { [string]$_ } | Sort-Object -Unique)
    if ($ps.Count -eq 0) { return $res }
    $out = [hashtable]::Synchronized(@{})
    $w = Invoke-IdentBounded $IdentScriptCheckScript @($ps, $out, $IdentityScriptCheckDelayMs) $IdentityCallMs
    $got = @{}
    [Threading.Monitor]::Enter($out.SyncRoot)
    try { foreach ($k in @($out.Keys)) { $got[$k] = $out[$k] } } finally { [Threading.Monitor]::Exit($out.SyncRoot) }
    foreach ($p in $ps) {
        $v = $got[$p]
        $res[$p] = $(if ($null -ne $v) { @{ check = [string]$v.check; full = $(if ($null -ne $v.full) { [string]$v.full } else { $null }) } } else { @{ check = $(if ($w.timeout) { 'timeout' } else { 'error' }); full = $null } })
    }
    $res
}
# parent / grandparent: no CIM row = exited; a parent that STARTED AFTER its child is a reused pid (never named); no parent pid or no dates to compare = none
function Get-IdentAncestor($Child, $Cim) {
    $none = @{ info = [ordered]@{ pid = $null; name = $null; started_utc = $null; status = 'none' }; row = $null }
    if ($null -eq $Child) { return $none }
    $ppid = [int64]0
    try { $ppid = [int64]$Child.ParentProcessId } catch { }
    if ($ppid -le 0 -or "$ppid" -ceq "$($Child.ProcessId)") { return $none }
    $pr = $Cim["$ppid"]
    if ($null -eq $pr) { return @{ info = [ordered]@{ pid = $ppid; name = $null; started_utc = $null; status = 'exited' }; row = $null } }
    $cd = $Child.CreationDate; $pd = $pr.CreationDate
    if ($null -eq $cd -or $null -eq $pd) { $none.info.pid = $ppid; return $none }
    if ($pd.ToUniversalTime() -gt $cd.ToUniversalTime()) { return @{ info = [ordered]@{ pid = $ppid; name = $null; started_utc = $null; status = 'pid-reused' }; row = $null } }
    $nm = (Limit-IdentText (([string]$pr.Name) -replace '(?i)\.exe$', '')).text
    if (-not $nm) { $nm = 'unknown' }
    @{ info = [ordered]@{ pid = $ppid; name = $nm; started_utc = $pd.ToUniversalTime().ToString('o'); status = 'ok' }; row = $pr }
}
$identity = $null
try {
    $idSw = [Diagnostics.Stopwatch]::StartNew()
    $script:IdentCimMs = 0L; $script:IdentOwnerMs = 0L; $script:IdentCimQueries = 0
    $identNotes = [ordered]@{}
    $curId = [Security.Principal.WindowsIdentity]::GetCurrent()
    $probeUser = [string]$curId.Name
    $elevated = [bool]([Security.Principal.WindowsPrincipal]::new($curId)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    # GPU: ONE read, kept out of $groups / $allPaths so the decision read above is untouched
    $gpuMs = 0L; $gpuStatus = 'ok'; $gpuBest = @{}; $gpuTop = @()
    if (-not $IdentityGpu) { $gpuStatus = $IdentityGpuDisabledWhy }
    else {
        $gsw = [Diagnostics.Stopwatch]::StartNew()
        try {
            $gw = Invoke-IdentBounded $IdentGpuScript @() $IdentityCallMs
            if ($gw.timeout) { $gpuStatus = "timeout: GPU Engine read exceeded $IdentityCallMs ms" }
            elseif (-not $gw.ok) { throw [string]$gw.msg }
            else {
                $gs = @($gw.v | Where-Object { $null -ne $_ })
                $engSum = @{}; $engType = @{}; $gpuInstances = 0
                foreach ($s in $gs) {
                    $lp = ([string]$s.Path).ToLowerInvariant()
                    if ($lp -notmatch '\(pid_(\d+)_luid_(0x[0-9a-f]+)_(0x[0-9a-f]+)_phys_(\d+)_eng_(\d+)_engtype_([^)]*)\)\\utilization percentage$') { continue }
                    $gPid = $Matches[1]; $gEng = "$gPid|$($Matches[2])_$($Matches[3])_$($Matches[4])_$($Matches[5])"; $gType = $Matches[6]
                    $gpuInstances++
                    $gv = Get-GoodValue $s
                    if ($null -eq $gv) { continue }
                    $engSum[$gEng] = [double]$engSum[$gEng] + $gv; $engType[$gEng] = $gType
                }
                if ($gpuInstances -eq 0) { $gpuStatus = 'counter-unavailable: no GPU Engine instance' }
                foreach ($e in $engSum.Keys) {
                    $gPid = $e.Substring(0, $e.IndexOf('|'))
                    if (-not $gpuBest.ContainsKey($gPid) -or $engSum[$e] -gt $gpuBest[$gPid].percent) { $gpuBest[$gPid] = @{ percent = $engSum[$e]; engtype = $engType[$e] } }
                }
                $gpuTop = @($gpuBest.Keys | Where-Object { $gpuBest[$_].percent -gt 0 } | Sort-Object @{ Expression = { $gpuBest[$_].percent }; Descending = $true }, @{ Expression = { [int64]$_ } } | Select-Object -First 3 | ForEach-Object { [int64]$_ })
            }
        } catch { $gpuStatus = 'counter-unavailable: ' + (([string]$_.Exception.Message) -replace '\s+', ' ') }
        $gsw.Stop(); $gpuMs = $gsw.ElapsedMilliseconds
    }
    # targets: top, then top_processes (pid not null), then the top GPU pids; each row records where it came from
    $targets = [ordered]@{}
    $nameOf = @{}
    foreach ($t in $top) { $targets["$($t.pid)"] = @('top'); $nameOf["$($t.pid)"] = [string]$t.name }
    foreach ($t in $ranked) {
        if ($null -eq $t.pid) { continue }
        $k = "$($t.pid)"
        if ($targets.Contains($k)) { $targets[$k] += 'top_processes' } else { $targets[$k] = @('top_processes') }
        if (-not $nameOf.ContainsKey($k)) { $nameOf[$k] = ([string]$t.instance) -replace '#\d+$', '' }
    }
    foreach ($g in $gpuTop) { $k = "$g"; if ($targets.Contains($k)) { $targets[$k] += 'gpu_top' } else { $targets[$k] = @('gpu_top') } }
    $cim = Get-IdentCim @($targets.Keys)
    $parentIds = @($targets.Keys | Where-Object { $cim.ContainsKey($_) } | ForEach-Object { $a = Get-IdentAncestor $cim[$_] $cim; if ($a.info.pid) { $a.info.pid } } | Where-Object { -not $cim.ContainsKey("$_") })
    $cim2 = Get-IdentCim $parentIds
    foreach ($k in $cim2.Keys) { $cim[$k] = $cim2[$k] }
    $gpIds = @($targets.Keys | Where-Object { $cim.ContainsKey($_) } | ForEach-Object {
        $a = Get-IdentAncestor $cim[$_] $cim
        if ($a.info.status -ceq 'ok') { $a2 = Get-IdentAncestor $a.row $cim; if ($a2.info.pid) { $a2.info.pid } }
    } | Where-Object { -not $cim.ContainsKey("$_") })
    $cim3 = Get-IdentCim $gpIds
    foreach ($k in $cim3.Keys) { $cim[$k] = $cim3[$k] }
    # the script slot of every target, checked in ONE bounded worker call before the rows are built: nothing below touches the filesystem for it
    $scriptCands = @($targets.Keys | Where-Object { $cim.ContainsKey($_) -and -not [string]::IsNullOrEmpty([string]$cim[$_].CommandLine) } |
        ForEach-Object { (Read-IdentCmdLine $cim[$_].CommandLine $cim[$_].ExecutablePath $cim[$_].Name).slot.path } | Where-Object { $_ })
    $scriptChecks = Test-IdentScriptPaths $scriptCands
    $procRows = @(); $ownerBad = 0; $cmdBad = 0; $scriptUnresolved = 0
    foreach ($k in $targets.Keys) {
        $snap = $after[[int]$k]
        $row = $cim[$k]
        $name = $nameOf[$k]
        if (-not $name -and $null -ne $snap) { $name = [string]$snap[0] }
        if (-not $name -and $null -ne $row) { $name = ([string]$row.Name) -replace '(?i)\.exe$', '' }
        if (-not $name) { $name = 'unknown' }
        $startCheck = 'unverified'; $startedUtc = $null
        if ($null -ne $row -and $null -ne $row.CreationDate) {
            $startedUtc = $row.CreationDate.ToUniversalTime().ToString('o')
            if ($null -ne $snap -and $null -ne $snap[2]) {
                $startCheck = $(if ([math]::Abs(($row.CreationDate.ToUniversalTime() - [datetime]::FromFileTimeUtc([int64]$snap[2])).TotalSeconds) -gt 1) { 'mismatch' } else { 'match' })
            }
        }
        if ($startCheck -ceq 'mismatch') {
            # the pid now belongs to a different process than the one sampled: no owner, command line or project, never another process's identity
            $procRows += [ordered]@{ pid = [int64]$k; name = $name; start_check = 'mismatch' }
            continue
        }
        $gpuPct = $null; $gpuEng = $null; $gpuRowStatus = $gpuStatus
        if ($gpuStatus -ceq 'ok') {
            if ($gpuBest.ContainsKey($k)) { $gpuPct = [math]::Round($gpuBest[$k].percent, 2); $gpuEng = [string]$gpuBest[$k].engtype; $gpuRowStatus = 'ok' }
            else { $gpuPct = 0.0; $gpuRowStatus = 'no-gpu-context' }
        }
        if ($null -eq $row) {
            $procRows += [ordered]@{
                pid = [int64]$k; name = $name; source = @($targets[$k]); started_utc = $null; start_check = $startCheck
                owner = $null; owner_status = 'exited'; cmdline = $null; cmdline_status = 'exited'; cmdline_redactions = 0; cmdline_truncated_chars = 0
                cmdline_args = $null; args_redacted = $null; cmdline_token_hits = $null
                exe = $null; exe_status = 'exited'; script = $null; script_check = $null; project = 'unknown'; project_from = 'none'
                parent = [ordered]@{ pid = $null; name = $null; started_utc = $null; status = 'none' }; grandparent = [ordered]@{ pid = $null; name = $null; started_utc = $null; status = 'none' }
                gpu_percent = $gpuPct; gpu_engtype = $gpuEng; gpu_status = $gpuRowStatus
            }
            $ownerBad++; $cmdBad++
            continue
        }
        $own = $(if ($idSw.ElapsedMilliseconds -gt $IdentityBudgetMs) { @{ owner = $null; status = 'budget-exceeded' } } else { Get-IdentOwner $row })
        if ($null -eq $own.owner) { $ownerBad++ }
        $cmdText = $null; $cmdRed = 0; $cmdCut = 0; $cmdStatus = 'unreadable'; $cmdArgs = $null; $cmdArgsRed = $null; $cmdScript = $null; $cmdCheck = $null; $cmdHits = $null
        if (-not [string]::IsNullOrEmpty([string]$row.CommandLine)) {
            $pc = Protect-IdentCmdLine $row.CommandLine $row.ExecutablePath $row.Name $scriptChecks
            $cmdText = $pc.text; $cmdRed = $pc.redactions; $cmdCut = $pc.truncated; $cmdArgs = $pc.args; $cmdArgsRed = 'all'; $cmdScript = $pc.script; $cmdCheck = $pc.script_check; $cmdHits = $pc.token_hits; $cmdStatus = 'ok'
            if ($cmdCheck -ceq 'timeout' -or $cmdCheck -ceq 'error') { $scriptUnresolved++ }
        } else { $cmdBad++ }
        $exeText = $null; $exeStatus = 'unreadable'
        if (-not [string]::IsNullOrEmpty([string]$row.ExecutablePath)) { $exeText = (Limit-IdentText $row.ExecutablePath).text; $exeStatus = 'ok' }
        $par = Get-IdentAncestor $row $cim
        $gpar = $(if ($par.info.status -ceq 'ok') { Get-IdentAncestor $par.row $cim } else { @{ info = [ordered]@{ pid = $null; name = $null; started_utc = $null; status = 'none' }; row = $null } })
        $project = 'unknown'; $projectFrom = 'none'
        $hit = Get-IdentProject $row
        if ($hit) { $project = $hit; $projectFrom = 'self' }
        elseif ($par.info.status -ceq 'ok' -and ($hit = Get-IdentProject $par.row)) { $project = $hit; $projectFrom = 'parent' }
        elseif ($gpar.info.status -ceq 'ok' -and ($hit = Get-IdentProject $gpar.row)) { $project = $hit; $projectFrom = 'grandparent' }
        $procRows += [ordered]@{
            pid = [int64]$k; name = $name; source = @($targets[$k]); started_utc = $startedUtc; start_check = $startCheck
            owner = $own.owner; owner_status = $own.status; cmdline = $cmdText; cmdline_status = $cmdStatus; cmdline_redactions = $cmdRed; cmdline_truncated_chars = $cmdCut
            cmdline_args = $cmdArgs; args_redacted = $cmdArgsRed; cmdline_token_hits = $cmdHits
            exe = $exeText; exe_status = $exeStatus; script = $cmdScript; script_check = $cmdCheck; project = $project; project_from = $projectFrom
            parent = $par.info; grandparent = $gpar.info
            gpu_percent = $gpuPct; gpu_engtype = $gpuEng; gpu_status = $gpuRowStatus
        }
    }
    $identNotes['cim_queries'] = "$($script:IdentCimQueries) WQL queries for $($targets.Count) target pid(s) (bound: 3); GetOwner once per target"
    if ($ownerBad -gt 0) { $identNotes['owner_unreadable'] = "$ownerBad of $($targets.Count) target(s) have no owner: see owner_status per row (a non-elevated probe cannot read other users' processes)" }
    if ($cmdBad -gt 0) { $identNotes['cmdline_unreadable'] = "$cmdBad of $($targets.Count) target(s) have no command line: see cmdline_status per row" }
    if ($scriptUnresolved -gt 0) { $identNotes['script_check_unresolved'] = "$scriptUnresolved of $($targets.Count) target(s) have script_check timeout or error: their script slot was not confirmed within $IdentityCallMs ms, so script is null" }
    if ($script:IdentAbandoned -gt 0) { $identNotes['calls_abandoned'] = "$($script:IdentAbandoned) identity call(s) passed their $IdentityCallMs ms deadline and were abandoned: see the *_status fields" }
    $idSw.Stop()
    $identity = [ordered]@{
        schema = $identitySchema; probe_user = $probeUser; elevated = $elevated; project_roots = 'r1'
        gpu_basis = 'per-process max over GPU engines of summed \GPU Engine(*)\Utilization Percentage (0-100 of that engine)'
        gpu_status = $gpuStatus
    }
    if ($IdentityGpu) { $identity['gpu_top'] = @($gpuTop) }
    $identity['processes'] = @($procRows)
    $identity['cost_ms'] = [ordered]@{ cim = [int64]$script:IdentCimMs; owner = [int64]$script:IdentOwnerMs; gpu = [int64]$gpuMs; total = [int64]$idSw.ElapsedMilliseconds }
    $identity['notes'] = $identNotes
} catch {
    $identity = [ordered]@{ schema = $identitySchema; error = (([string]$_.Exception.Message) -replace '\s+', ' ') }
} finally { Close-IdentWorker }
Write-Output ('VENUE_QUIET=' + ([ordered]@{
    schema = 'mlv-app/venue-quiet-probe/v2'; host = $env:COMPUTERNAME; samples = $samples; top = $top
    cpu_count = $cpuCount
    percent_basis = 'per-process percents are percent of the whole machine (counter / cpu_count)'
    aggregation = 'valid-sample mean per instance; process rows keyed on (instance, pid); gap = |process_active - attributed|; unattributed_kernel = per-sample-set (processor.total - process_active), negative samples invalid and excluded'
    processor = $processor
    process_total_percent = $pTotal; idle_percent = $pIdle; system_percent = $pSystem
    process_active_percent = $pActive; unattributed_kernel_percent = $unattrKernel
    top_processes = $ranked; attributed_percent = $attributed; attribution_gap_points = $gap
    core_topology = $(if ($topo.map.Count -gt 0) { 'logical-processor-information-ex' } else { 'unknown' })
    core_classes = $classAgg; cores = $cores
    notes = $notes
    telemetry_schema = $telemetrySchema; heat = $telHeat; gpu = $telGpu; seconds_since_last_lease = $secondsSinceLease; last_lease = $telLease
    identity = $identity
} | ConvertTo-Json -Compress -Depth 6))
# an abandoned worker may still be inside a native call, and a runspace thread is a foreground thread: end the process now that the line is written
if ($script:IdentAbandoned -gt 0 -or $script:TelAbandoned -gt 0) { [Console]::Out.Flush(); [Environment]::Exit(0) }
'@

# v2 attribution, printed AFTER the verdict and top lines so the gate log says what kept the venue busy. Display only: nothing here feeds the decision, and a
# probe line that is not v2 (or whose v2 fields are malformed) prints nothing / one `attr` note and never changes the verdict or the exit code.
function Format-Pct($v) { if ($null -eq $v) { 'n/a' } else { '{0:0.0}%' -f [double]$v } }

# IDENTITY text (VENUE-PROBE-PROCESS-ATTRIBUTION-1), display only. A probe with no identity (v1, v2, malformed, a future schema) prints exactly what it printed before. Every value
# written here has whitespace and commas replaced by '_' (the run-local drivers split top lines on whitespace and commas), and no command line is ever written to a text line.
$identitySchemaV1 = 'mlv-app/venue-quiet-probe-identity/v1'
function ConvertTo-IdentToken($v) { $s = [string]$v; if ([string]::IsNullOrEmpty($s)) { return 'n/a' }; $s -replace '[\s,]', '_' }
# PURE: pid -> identity row for a probe that carries identity schema v1, else $null
function Get-IdentityRowMap($Probe) {
    try {
        if ([string]$Probe.identity.schema -cne $identitySchemaV1) { return $null }
        $map = @{}
        foreach ($r in @($Probe.identity.processes)) { if ($null -ne $r -and $null -ne $r.pid) { $map["$($r.pid)"] = $r } }
        return $map
    } catch { return $null }
}
function Format-IdentOwner($Row) { if (-not [string]::IsNullOrEmpty([string]$Row.owner)) { ConvertTo-IdentToken $Row.owner } else { 'unreadable(' + (ConvertTo-IdentToken $Row.owner_status) + ')' } }
function Format-IdentWhen($v) {
    if ($null -eq $v) { return 'n/a' }
    try {
        $d = $(if ($v -is [datetime]) { $v.ToUniversalTime() } else { [datetime]::Parse([string]$v, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AdjustToUniversal -bor [Globalization.DateTimeStyles]::AssumeUniversal) })
        return $d.ToString('yyyy-MM-ddTHH:mm:ssZ', [Globalization.CultureInfo]::InvariantCulture)
    } catch { return 'n/a' }
}
function Format-IdentAncestor($A) { if ($null -eq $A) { return 'n/a:n/a|none' }; '{0}:{1}|{2}' -f (ConvertTo-IdentToken $A.name), (ConvertTo-IdentToken $A.pid), (ConvertTo-IdentToken $A.status) }
# r6: ` script_check=<value>` for a row that carries the field; a row from an older probe has none and prints exactly what it printed before
function Format-IdentScriptCheck($Row) { if ($null -ne $Row.PSObject.Properties['script_check']) { ' script_check=' + (ConvertTo-IdentToken $Row.script_check) } else { '' } }
# the suffix of a `top` line; $Full = $false is the shorter `attr proc` suffix (owner, project and script_check only)
function Get-IdentSuffix($Row, [bool]$Full) {
    if ($null -eq $Row) { return '' }
    try {
        if ([string]$Row.start_check -ceq 'mismatch') { return ' ident=pid-reused' }
        $s = ' owner={0} project={1}' -f (Format-IdentOwner $Row), (ConvertTo-IdentToken $Row.project)
        if ($Full) { $s += (' script={0}' -f (ConvertTo-IdentToken $Row.script)) + (Format-IdentScriptCheck $Row) + (' parent={0} gparent={1} started={2} gpu={3}' -f (Format-IdentAncestor $Row.parent), (Format-IdentAncestor $Row.grandparent), (Format-IdentWhen $Row.started_utc), (Format-Pct $Row.gpu_percent)) }
        else { $s += Format-IdentScriptCheck $Row }
        return $s
    } catch { return '' }
}
function Write-IdentityLines($Probe) {
    try {
        $id = $Probe.identity
        if ($null -eq $id -or [string]$id.schema -cne $identitySchemaV1) { return }
        if ($null -ne $id.error) { Write-Gate ("  ident error {0}" -f ((([string]$id.error) -replace '\s+', ' '))); return }
        $c = $id.cost_ms
        Write-Gate ("  ident probe user={0} elevated={1} cost cim={2} owner={3} gpu={4} total={5}" -f (ConvertTo-IdentToken $id.probe_user), $(if ($id.elevated -eq $true) { 'true' } else { 'false' }), (ConvertTo-IdentToken $c.cim), (ConvertTo-IdentToken $c.owner), (ConvertTo-IdentToken $c.gpu), (ConvertTo-IdentToken $c.total))
        $map = Get-IdentityRowMap $Probe
        $inTop = @{}; foreach ($t in @($Probe.top)) { if ($null -ne $t.pid) { $inTop["$($t.pid)"] = $true } }
        foreach ($g in @($id.gpu_top)) {
            $k = "$g"
            if ($inTop.ContainsKey($k)) { continue }
            $row = $(if ($null -ne $map -and $map.ContainsKey($k)) { $map[$k] } else { $null })
            if ($null -eq $row) { Write-Gate ("  ident gpu n/a pid={0}" -f (ConvertTo-IdentToken $g)); continue }
            if ([string]$row.start_check -ceq 'mismatch') { Write-Gate ("  ident gpu {0} pid={1} ident=pid-reused" -f (ConvertTo-IdentToken $row.name), (ConvertTo-IdentToken $row.pid)); continue }
            Write-Gate (("  ident gpu {0} pid={1} gpu={2} engtype={3} owner={4} project={5}" -f (ConvertTo-IdentToken $row.name), (ConvertTo-IdentToken $row.pid), (Format-Pct $row.gpu_percent), (ConvertTo-IdentToken $row.gpu_engtype), (Format-IdentOwner $row), (ConvertTo-IdentToken $row.project)) + (Format-IdentScriptCheck $row))
        }
        foreach ($n in @($id.notes.PSObject.Properties)) { Write-Gate ("  ident note {0}: {1}" -f $n.Name, $n.Value) }
    } catch { Write-Gate ("  ident UNPRINTABLE {0}" -f $_.Exception.Message) }
}
# TELEMETRY text (VENUE-PROBE-HEAT-GPU-TELEMETRY-1), display only: three `tele` lines after the ident lines for a probe that carries telemetry schema v1, nothing otherwise.
$telemetrySchemaV1 = 'mlv-app/venue-quiet-probe-telemetry/v1'
function Format-TelNum($v, [string]$Unit, [string]$Fmt = '{0:0.0}') { if ($null -eq $v -or -not ($v -is [ValueType])) { 'n/a' } else { ($Fmt -f [double]$v) + $Unit } }
function Get-TelReasonSuffix($Block) {
    $codes = @(@($Block.reasons.PSObject.Properties) | ForEach-Object { ConvertTo-IdentToken $_.Value } | Sort-Object -Unique)
    if ($codes.Count) { ' reason=' + ($codes -join '+') } else { '' }
}
function Write-TelemetryLines($Probe) {
    try {
        if ([string]$Probe.telemetry_schema -cne $telemetrySchemaV1) { return }
        $h = $Probe.heat
        Write-Gate (("  tele heat source={0} age={1} hottest={2} max15={3} mean={4} throttled={5}/{6} W={7}" -f (ConvertTo-IdentToken $h.source), (Format-TelNum $h.sample_age_s 's'),
            (Format-TelNum $h.hottest_core_c 'C'), (Format-TelNum $h.max15_c 'C'), (Format-TelNum $h.mean_c 'C'), (ConvertTo-IdentToken $h.throttled), (ConvertTo-IdentToken $h.window_rows),
            (Format-TelNum $h.package_w '')) + (Get-TelReasonSuffix $h))
        $g = $Probe.gpu
        Write-Gate (("  tele gpu state={0} skipped_wake_risk={1} util={2} temp={3} W={4} pstate={5}" -f (ConvertTo-IdentToken $g.state), $(if ($g.skipped_wake_risk -eq $true) { 'true' } elseif ($g.skipped_wake_risk -eq $false) { 'false' } else { 'n/a' }),
            (Format-TelNum $g.utilization_percent '%'), (Format-TelNum $g.temp_c 'C'), (Format-TelNum $g.power_w ''), (ConvertTo-IdentToken $g.pstate)) + $(if ($g.state_reason) { ' state_reason=' + (ConvertTo-IdentToken $g.state_reason) } else { '' }))
        $l = $Probe.last_lease
        Write-Gate (("  tele lease since={0} event={1}" -f (Format-TelNum $Probe.seconds_since_last_lease 's' '{0:0}'), (ConvertTo-IdentToken $l.event)) + $(if ($l.reason) { ' reason=' + (ConvertTo-IdentToken $l.reason) } else { '' }))
    } catch { Write-Gate ("  tele UNPRINTABLE {0}" -f $_.Exception.Message) }
}
function Write-AttributionLines($Probe) {
    if ([string]$Probe.schema -cne 'mlv-app/venue-quiet-probe/v2') { return }
    try {
        $pr = $Probe.processor
        Write-Gate ("  attr cpus={0} processor total={1} privileged={2} user={3} dpc={4} interrupt={5}" -f $Probe.cpu_count, (Format-Pct $pr.total_percent), (Format-Pct $pr.privileged_percent), (Format-Pct $pr.user_percent), (Format-Pct $pr.dpc_percent), (Format-Pct $pr.interrupt_percent))
        Write-Gate ("  attr process total={0} idle={1} system={2} attributed={3}" -f (Format-Pct $Probe.process_total_percent), (Format-Pct $Probe.idle_percent), (Format-Pct $Probe.system_percent), (Format-Pct $Probe.attributed_percent))
        Write-Gate ("  attr gap processorTotal={0} processActive={1} attributed={2} unattributedKernel={3} gap={4}" -f (Format-Pct $pr.total_percent), (Format-Pct $Probe.process_active_percent), (Format-Pct $Probe.attributed_percent), (Format-Pct $Probe.unattributed_kernel_percent), $(if ($null -eq $Probe.attribution_gap_points) { 'n/a' } else { '{0:0.0} points' -f [double]$Probe.attribution_gap_points }))
        $idMap = Get-IdentityRowMap $Probe
        foreach ($p in @($Probe.top_processes)) { Write-Gate ("  attr proc {0} pid={1} pct={2}{3}{4}" -f $p.instance, $(if ($null -eq $p.pid) { 'n/a' } else { $p.pid }), (Format-Pct $p.percent), $(if ($p.incomplete -eq $true) { " valid=$($p.validSamples)/$($p.totalSamples) incomplete" } else { '' }), $(if ($null -ne $idMap -and $null -ne $p.pid -and $idMap.ContainsKey("$($p.pid)")) { Get-IdentSuffix $idMap["$($p.pid)"] $false } else { '' })) }
        $cls = @($Probe.core_classes.PSObject.Properties | ForEach-Object { "{0} n={1} mean={2} max={3}" -f $_.Name, $_.Value.count, (Format-Pct $_.Value.mean_percent), (Format-Pct $_.Value.max_percent) })
        Write-Gate ("  attr cores topology={0} {1}" -f $Probe.core_topology, $(if ($cls.Count) { $cls -join ' | ' } else { 'none' }))
        foreach ($n in @($Probe.notes.PSObject.Properties)) { Write-Gate ("  attr note {0}: {1}" -f $n.Name, $n.Value) }
    } catch { Write-Gate ("  attr UNPRINTABLE {0}" -f $_.Exception.Message) }
}

# FOREIGN FLOOR (see the header). PURE: the top-3 owners ("name:pid") of one probe when none of them is MLV's, else an empty list. Rows come from the v2 top_processes (already
# ranked by percent of the whole machine); pwsh / powershell rows are skipped (the probe job and the agent run in them). Anything malformed names no owner.
function Get-ForeignFloorOwners($Probe) {
    try {
        if ([string]$Probe.schema -cne 'mlv-app/venue-quiet-probe/v2') { return @() }
        $rows = @(@($Probe.top_processes) | Where-Object { $null -ne $_ -and -not [string]::IsNullOrWhiteSpace([string]$_.instance) } |
            Where-Object { ([string]$_.instance).ToLowerInvariant() -replace '#\d+$', '' -notmatch '^(pwsh|powershell)$' } | Select-Object -First 3)
        if ($rows.Count -eq 0) { return @() }
        if (@($rows | Where-Object { ([string]$_.instance).ToLowerInvariant() -replace '#\d+$', '' -match '^(mlv|ultra-magnus|um-run)' }).Count -gt 0) { return @() }
        return @($rows | ForEach-Object { ('{0}:{1}' -f (([string]$_.instance) -replace '\s+', '_'), $(if ($null -eq $_.pid) { 'n/a' } else { $_.pid })) })
    } catch { return @() }
}

# PURE: no downward trend over the window of means, oldest first (the newest is not below the first by more than the margin; the epsilon only absorbs float noise).
function Test-NoDownwardTrend([double[]]$Means, [double]$MarginPoints) { ($Means[0] - $Means[-1]) -le ($MarginPoints + 1e-9) }

# BUSIEST FAST CORE (see the header). PURE: what the per-core check says about one probe. would_block is $true / $false only when every core row that could be a fast core is
# readable; anything else is $null with a typed reason, never a silent $false. E-cores never count; a row whose class is not P / E / uniform might be a fast core, so it makes the
# answer unknown rather than being skipped, and so does a core the probe omitted for having no valid sample (notes.cores_omitted). The figure is percent_exact (the unrounded
# valid-sample mean), else the probe's 2-dp percent.
function Test-PerCoreNumber($v) { $v -is [double] -or $v -is [int] -or $v -is [long] -or $v -is [decimal] }
function Get-VenuePerCoreVerdict($Probe, [double]$Threshold, [bool]$Enabled) {
    $r = [ordered]@{ enabled = $Enabled; threshold = $Threshold; busiest_fast_core = $null; busiest_fast_percent = $null; would_block = $null; reason = $null }
    try {
        if ($null -eq $Probe -or $Probe -isnot [pscustomobject]) { $r.reason = 'no-probe'; return [pscustomobject]$r }
        if ($null -eq $Probe.cores) { $r.reason = 'no-cores'; return [pscustomobject]$r }
        # cores[] is an array of rows; one object (which @() would wrap as a single row), a string or a number is no list of cores
        if ($Probe.cores -isnot [array]) { $r.reason = 'invalid-cores'; return [pscustomobject]$r }
        $all = @($Probe.cores)
        if ($all.Count -eq 0) { $r.reason = 'no-cores'; return [pscustomobject]$r }
        $fast = @(); $reasons = @()
        # a whole count as an exact [long] (never a double: 2^53 and 2^53+1 are the same double), else $null
        $wholeCount = { param($v)
            if ($v -is [int] -or $v -is [long]) { return [long]$v }
            if ($v -is [double] -and [double]::IsFinite($v) -and [Math]::Floor($v) -eq $v -and [Math]::Abs($v) -lt 9007199254740992.0) { return [long]$v }
            return $null
        }
        foreach ($row in $all) {
            # a row that is not a JSON object (null, a scalar, an array nested in cores[]) could not be read, and it might have been a fast core: unknown, never dropped or enumerated
            if ($row -isnot [pscustomobject]) { $reasons += 'invalid-core-row'; continue }
            # a class that is not a plain string (an array would stringify to its first element) is no class
            $cls = $row.core_class; if ($cls -isnot [string]) { $cls = '' }
            $isFast = ($cls -ceq 'P' -or $cls -ceq 'uniform')
            $isUnknown = -not ($isFast -or $cls -ceq 'E')
            if (-not ($isFast -or $isUnknown)) { continue }
            if ($isUnknown) { $reasons += 'core-class-unknown' }
            # sample counts: when the row carries either count, both must be whole numbers with valid == total >= 1; anything else (a string, a fraction, zero, a negative, valid > total) is not evidence of completeness
            $short = $false
            if ($null -ne $row.PSObject.Properties['validSamples'] -or $null -ne $row.PSObject.Properties['totalSamples']) {
                $vs = & $wholeCount $row.validSamples; $ts = & $wholeCount $row.totalSamples
                if ($null -eq $vs -or $null -eq $ts -or $ts -lt 1 -or $vs -ne $ts) { $short = $true }
            }
            # the flag, when the row carries it, is a boolean; a non-boolean (an object, a string, a number, a null) is not evidence of completeness
            $flag = $row.PSObject.Properties['incomplete']
            if ($null -ne $flag -and $flag.Value -isnot [bool]) { $short = $true }
            if ($row.incomplete -eq $true -or $short) { $reasons += 'incomplete-samples' }
            # percent_exact when the row carries the property (even a null or bad one: no fallback to the rounded figure), else the older probe's 2-dp percent; plain assignment keeps a one-element array an array
            if ($null -ne $row.PSObject.Properties['percent_exact']) { $v = $row.percent_exact } else { $v = $row.percent }
            if (-not (Test-PerCoreNumber $v) -or -not [double]::IsFinite([double]$v) -or [double]$v -lt 0.0 -or [double]$v -gt 100.0) { $reasons += 'invalid-core-percent'; continue }
            # a fast core is named by its "<group>,<core>" instance (a missing, array, numeric or free-text name would be a made-up core in the blocked line)
            if ($isFast) {
                if ($row.instance -isnot [string] -or $row.instance -cnotmatch '^\d+,\d+$') { $reasons += 'invalid-core-instance'; continue }
                $fast += [pscustomobject]@{ instance = [string]$row.instance; percent = [double]$v }
            }
        }
        if ($fast.Count -gt 0) {
            $top = $fast | Sort-Object -Property @{ Expression = { $_.percent }; Descending = $true } | Select-Object -First 1
            $r.busiest_fast_core = $top.instance; $r.busiest_fast_percent = $top.percent
        }
        # notes is an object; any other shape (an array, a string, an explicit null) cannot say that nothing was omitted, so it is unknown rather than silently clean. Only an ABSENT
        # property (an older probe that never carried notes) is skipped: "present with a null value" is told apart from "absent" by the property itself, not by its value.
        $notesProp = $Probe.PSObject.Properties['notes']
        if ($null -ne $notesProp) {
            if ($notesProp.Value -isnot [pscustomobject]) { $reasons += 'invalid-notes' }
            elseif ($null -ne $notesProp.Value.PSObject.Properties['cores_omitted']) { $reasons += 'cores-omitted' }
        }
        $reasons = @($reasons | Select-Object -Unique)
        if ($reasons.Count -gt 0) { $r.reason = $reasons -join '+' }
        elseif ($fast.Count -eq 0) { $r.reason = 'no-fast-cores' }
        else { $r.would_block = ($r.busiest_fast_percent -ge $Threshold) }
    } catch { $r.would_block = $null; $r.reason = 'no-cores' }
    [pscustomobject]$r
}
# invariant-culture percent text ('82.0%', never '82,0%': gate text is comma-free)
function Format-PerCorePct($v) { if ($null -eq $v) { 'n/a' } else { ([double]$v).ToString('0.0', [Globalization.CultureInfo]::InvariantCulture) + '%' } }
# One probe sample as log text, TOTAL: a number prints '{0:0.0}', null prints 'null', anything else a typed token (never a cast, so a malformed sample the decision already called UNKNOWN
# cannot stop the PROBE line, the percore lines or the VENUE_QUIET_UNKNOWN exit from being written).
function Format-VenueSample($v) {
    try {
        if ($null -eq $v) { return 'null' }
        if (Test-PerCoreNumber $v) { return ('{0:0.0}' -f [double]$v) }
        if ($v -is [string]) { return 'bad:string' }
        if ($v -is [bool]) { return 'bad:bool' }
        if ($v -is [array]) { return 'bad:array' }
        if ($v -is [pscustomobject]) { return 'bad:object' }
    } catch { }
    'bad:other'
}
# Logging only: a failing -GateLog sink (a lock, a full disk) loses this line from the log and nothing else. The line is written to stdout first by Write-Gate, and the catch does
# NOT write through the same failing sink again, so the verdict and every exit code stay what they were with the check off.
function Write-PerCoreLines($Pc, [bool]$Blocked) {
    # each line has its own guard: a sink that fails on the observation line must not also swallow the blocked-core line
    try {
        $wb = $(if ($null -eq $Pc.would_block) { 'null' } elseif ($Pc.would_block) { 'true' } else { 'false' })
        Write-Gate (("  percore enabled={0} threshold={1} busiest_fast_core={2} busiest_fast_pct={3} would_block={4}" -f $Pc.enabled.ToString().ToLowerInvariant(), (Format-PerCorePct $Pc.threshold), (ConvertTo-IdentToken $Pc.busiest_fast_core), (Format-PerCorePct $Pc.busiest_fast_percent), $wb) + $(if ($Pc.reason) { ' reason=' + (ConvertTo-IdentToken $Pc.reason) } else { '' }))
    } catch { try { [Console]::Error.WriteLine("percore line not logged: $($_.Exception.Message)") } catch { } }
    try {
        if ($Blocked) { Write-Gate ("  percore blocked core={0} percent={1} threshold={2}" -f (ConvertTo-IdentToken $Pc.busiest_fast_core), (Format-PerCorePct $Pc.busiest_fast_percent), (Format-PerCorePct $Pc.threshold)) }
    } catch { try { [Console]::Error.WriteLine("percore line not logged: $($_.Exception.Message)") } catch { } }
}

$last = $null
$floorMeans = @(); $foreignFloor = $null
while ($true) {
    Wait-QueueGate 'quiet-probe'
    if ($script:GateState -ceq 'UNREADABLE') { Write-Gate 'DECISION UNKNOWN mean=UNKNOWN'; exit 4 }
    if ($script:GateState -cne 'CLEAR') { exit 3 }
    $jobId = 'venue-quiet-probe-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    $jobFile = Join-Path $WorkDir "$jobId.job.ps1"
    [IO.File]::WriteAllText($jobFile, $probeText, [Text.UTF8Encoding]::new($false))
    $probe = $null; $why = $null; $whyKind = $null; $rawPath = $null; $rawErr = $null
    try {
        $out = @(& $umRun -ScriptPath $jobFile -JobId $jobId -AgentShare $agentShare -TimeoutSec 120 -MaxQueueWaitSec 300 -MaxClaimedWaitSec 120 -MaxAdmissionWaitSec $MaxAdmissionWaitSec 6>$null)
        $r = if ($out.Count -gt 0) { $out[-1] } else { $null }
        $line = @(([string]$r.stdout) -split "`r?`n" | Where-Object { $_ -like 'VENUE_QUIET=*' }) | Select-Object -Last 1
        if ($line) {
            # RAW RETENTION: the probe's line, verbatim, before it is parsed (an unparseable line is evidence too); a write failure never changes the verdict
            try { $rawPath = Join-Path $WorkDir "$jobId.json"; [IO.File]::WriteAllText($rawPath, $line.Substring('VENUE_QUIET='.Length), [Text.UTF8Encoding]::new($false)) } catch { $rawErr = [string]$_.Exception.Message; $rawPath = $null }
            $probe = $line.Substring('VENUE_QUIET='.Length) | ConvertFrom-Json
        } else { $why = "no VENUE_QUIET line (exit $($r.exitCode))"; $whyKind = 'no-VENUE_QUIET-line' }
    } catch { $why = [string]$_.Exception.Message; $whyKind = 'probe-failed' }
    # HOST ECHO: the probe reports the machine it ran on. An acknowledged alias of ANOTHER host's share (or any share that is not this venue's) would measure that
    # host, so the answer must come from this venue's expectedHost (case-insensitive) before any verdict is printed for it.
    if ($null -ne $probe -and [string]$probe.host -ne $expectedHost) {
        Write-Gate "HOST_MISMATCH $jobId $Venue probe host='$([string]$probe.host)' is not expectedHost '$expectedHost': no verdict is printed for '$Venue' $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
        exit 5
    }
    $samples = $(if ($null -ne $probe) { @($probe.samples) } else { @() })
    $d =Get-VenueQuietDecision -Samples $samples -Threshold $ThresholdPercent
    # BUSIEST FAST CORE: always evaluated for a v2 probe (or any probe once enforcing) so the line can be logged; it changes the decision only under -PerCoreEnforce, only for a
    # mean-QUIET probe, and only on a measured would_block = $true (null is unknown and never blocks).
    $pc = $null; $pcBlocked = $false; $pcNote = ''
    # Eligible = the probe carries cores[] (whatever its schema) or is v2, or enforcement is on (an unavailable probe then gets one typed-null line). The schema version is not the gate.
    if ($PerCoreEnforce -or ($null -ne $probe -and ($null -ne $probe.cores -or [string]$probe.schema -ceq 'mlv-app/venue-quiet-probe/v2'))) { $pc = Get-VenuePerCoreVerdict $probe $PerCoreThresholdPercent ([bool]$PerCoreEnforce) }
    if ($PerCoreEnforce -and $d.state -ceq 'QUIET' -and $null -ne $pc -and $pc.would_block -eq $true) {
        $d = [pscustomobject]@{ state = 'BUSY'; mean = $d.mean }
        $pcBlocked = $true
        $pcNote = " note=percore core=$(ConvertTo-IdentToken $pc.busiest_fast_core) percent=$(Format-PerCorePct $pc.busiest_fast_percent)"
    }
    $last = [pscustomobject]@{ decision = $d; probe = $probe; why = $why; whyKind = $(if ($whyKind) { $whyKind } else { 'invalid-samples' }); percore = $pc }
    $sampleText = $(if ($samples.Count) { (@($samples | ForEach-Object { Format-VenueSample $_ })) -join '/' } else { 'none' })
    Write-Gate "PROBE $jobId $Venue samples=$sampleText mean=$(Format-Mean $d.mean) state=$($d.state)$(if ($why) { " note=$why" })$pcNote $([DateTime]::UtcNow.ToString('HH:mm:ssZ'))"
    if ($rawPath) { Write-Gate "  raw $rawPath" } elseif ($rawErr) { Write-Gate "  raw UNWRITABLE $rawErr" }
    if ($null -ne $pc) { Write-PerCoreLines $pc $pcBlocked }
    if ($d.state -ceq 'QUIET') { break }
    # the run of consecutive MEASURED busy probes that name foreign owners; anything else (UNKNOWN, an MLV owner, no owner named) starts it again
    $owners = $(if ($d.state -ceq 'BUSY') { @(Get-ForeignFloorOwners $probe) } else { @() })
    if ($owners.Count -gt 0) { $floorMeans += [double]$d.mean } else { $floorMeans = @() }
    if ($ForeignFloorProbes -gt 0 -and $floorMeans.Count -ge $ForeignFloorProbes) {
        $window = [double[]]@($floorMeans[($floorMeans.Count - $ForeignFloorProbes)..($floorMeans.Count - 1)])
        if (Test-NoDownwardTrend $window $ForeignFloorTrendMarginPoints) {
            $foreignFloor = [pscustomobject]@{ owners = $owners; floorMean = (($window | Measure-Object -Average).Average); probes = $ForeignFloorProbes }
            break
        }
    }
    if ((Get-Date).AddSeconds($RecheckSec) -gt $deadline) { break }
    Start-Sleep -Seconds $RecheckSec
}
# UNKNOWN is not a measured busy venue: it gets its own typed verdict and exit code (6), so a caller can never read it as COOLDOWN_UNMET (exit 0, proceed).
if ($last.decision.state -ceq 'UNKNOWN') {
    Write-Gate "VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=$ThresholdPercent% waitedSec=$([int]((Get-Date) - $t0).TotalSeconds) reason=$($last.whyKind)$(if ($last.why) { " note=$($last.why)" })"
    exit 6
}
if ($null -ne $foreignFloor) { Write-Gate "FOREIGN_FLOOR owners=$($foreignFloor.owners -join ',') floorMean=$(Format-Mean $foreignFloor.floorMean) waitedSec=$([int]((Get-Date) - $t0).TotalSeconds) probes=$($foreignFloor.probes)" }
$verdict = $(if ($last.decision.state -ceq 'QUIET') { 'QUIET' } else { 'COOLDOWN_UNMET' })
Write-Gate "$verdict mean=$(Format-Mean $last.decision.mean) threshold=$ThresholdPercent% waitedSec=$([int]((Get-Date) - $t0).TotalSeconds)"
$topIdMap = $(if ($null -ne $last.probe) { Get-IdentityRowMap $last.probe } else { $null })
if ($null -ne $last.probe) { foreach ($p in @($last.probe.top)) { Write-Gate (("  top {0} pid={1} cpuSeconds={2}" -f $p.name, $p.pid, $p.cpuSeconds) + $(if ($null -ne $topIdMap -and $null -ne $p.pid -and $topIdMap.ContainsKey("$($p.pid)")) { Get-IdentSuffix $topIdMap["$($p.pid)"] $true } else { '' })) } }
if ($null -ne $last.probe) { Write-AttributionLines $last.probe }
if ($null -ne $last.probe) { Write-IdentityLines $last.probe }
if ($null -ne $last.probe) { Write-TelemetryLines $last.probe }
exit 0
