# PLAYBACK-ATTR-3-CUDA runbook

Within-run refresh-period + prep-region attribution for the CUDA playback path on Bachelor.
Bachelor cannot resolve a timing A/B below about 2x (A/A noise), so this measurement is judged
**within one run**: the share of presented frames landing on 1, 2, or 3+ display refreshes,
plus per-sub-region prep timing (`prep_region_setup/gpu/image/present/finish/total/unattributed`).

**The build is SPLIT across three hosts** (swarm ruling 2026-09-16,
`.claude-state/fleet-runs/swarm-attr3-buildhost-20260916T2150Z/SYNTHESIS.md`). Bachelor -- the
measurement host -- has Visual Studio without the VC tools component (no `cl.exe`) and no CUDA
toolkit, so nothing compiles there; Ultra-Magnus has CUDA 12.6 nvcc and an RTX 4090 but no qmake
or MinGW. So: CUDA DLL pair on Ultra-Magnus, Qt exe on the board host, package staged into the
Bachelor cache, measurement on Bachelor. `playback-attr-3-cuda-compile-job.ps1` (compile on
Bachelor) is **retired and refuses to emit**; installing a toolchain on Bachelor was rejected as
an unnecessary system change to the owner's laptop.

> **TRAP: a precedent script is not a precedent result.** Before generating a job for any remote
> host, cite the precedent's `result.json` `exitCode` 0, and cite a fresh toolchain probe receipt
> from that host. The PR #131 lane prompt called the failed precedent a job that "ran"; nobody
> read its `result.json` (exit 5, `msvcDiscovery`). `safe_to_submit_to_bachelor` certified script
> safety, not toolchain existence.

**Footage (NA-4, id-only route).** `docs/never-authorized.json` NA-4's `CLIP_OR_NONE:`
owner-typed-path mechanism is how OTHER cards admit footage; this card uses NA-4's
id-addressed-consumer route instead (ATTR3-FOOTAGE-BIND-1). Step 4 below takes an owner clip
**id** (e.g. `M16-1243`), never a path: the generator resolves it through
`tools/gates/resolve_consented_clip.py` against the frozen consent table
(`OWNER_CONSENTED_FOOTAGE` in `tools/hooks/mlv-never-authorized.py`), and `-ClipPath` /
`-FixtureSha256` are both refused outright for an owner id. Only step 4 touches footage; steps
1-3 compile, hash and move build artifacts and never name a media file. Adjudication:
`.claude-state/fleet-runs/swarm-footage-route-20260916T2020Z/SYNTHESIS.md` (round 1);
ATTR3-FOOTAGE-BIND-1 (round 2, the current route).

**Submission (NA-7).** Direct hooked writes to `\\bachelor\...` are refused. The tracked
submitter `tools/profiling/um-run.ps1` is the route, and the agents execute only `inbox\*.job.ps1`
while leaving other inbox files alone. Do not route around NA-7 by hiding paths in variables.

## 0. Toolchain probe first

Probe the host you are about to target, read-only, through `tools/profiling/um-run.ps1`, and keep
the receipt: nvcc version and `nvidia-smi` on Ultra-Magnus; `qmake -version` and
`mingw32-make --version` on the board host. A probe older than the current session proves nothing.

## 1. CUDA DLL pair (Ultra-Magnus)

```powershell
pwsh -NoProfile -File tools\profiling\ultramagnus\playback-attr-3-cuda-dll-job.ps1 `
    -SourceCommit <40-hex-sha> -OutDir <staging-dir> [-CudaArchitectures sm_86]
```

Writes `<jobId>-source.zip` (a `git archive` of the commit) and `<jobId>.job.ps1`
(`jobId = playback-attr-3-cuda-dllpair-<sha12>`). Drop both into the Ultra-Magnus agent inbox
(`G:\Temp\mlv-gpu-profile\agent\inbox`). On that host the job:

0. **verifies the source archive before expanding it.** The generator prints
   `sourceArchiveSha256` and bakes it in; the job refuses at **exit 8** unless the arriving bytes
   hash to it *and* the commit id `git archive` stamps into the zip's EOCD comment equals
   `-SourceCommit`. Everything below is a claim about the extracted source, so the claim is
   pinned first. The verified sha is recorded as `dll-pair-manifest.json`'s
   `sourceArchiveSha256`. `pwsh -File <jobId>.job.ps1 -VerifyOnly` runs exactly this prefix and
   exits, writing nothing -- use it to confirm a drop landed intact before the agent picks it up;
1. builds **both** DLLs with the tracked backend scripts from the extracted source --
   `tools/gpu/backend/build-backend-dll.ps1` (`igpu_recon_cuda.dll`) and
   `tools/gpu/backend/amaze-debayer-dll.ps1` (`igpu_amaze_debayer_cuda.dll`);
2. **targets `sm_86`** and re-proves it on the emitted bytes with `cuobjdump --list-elf`. The
   older Ultra-Magnus jobs target `sm_89`, which Bachelor's GPU cannot run at all; a requested
   flag is not an emitted cubin, so the flag alone is never accepted;
3. copies `cudart64_12.dll` from the same toolkit that compiled the DLLs;
4. records `dumpbin /exports` for both DLLs and computes **`pendingSymbolPresence`** (the
   `igpu_recon_` export test -- this is where it now lives; Bachelor has no tool to run it);
5. publishes into `outbox\<jobId>.artifacts`, transactionally (`.partial` -> verify -> rename),
   with `dll-pair-manifest.json` written **LAST**:
   `{sourceCommit, cudaArch, nvccVersion, files{name: lowercase sha256}, pendingSymbolPresence,
   builtOnHost}`.

Per-step exit codes: 3 source archive missing, **8 source archive does not bind**, 4 backend
sources, 5 MSVC, 6 CUDA toolkit, 7 CUDA runtime,
11 recon build, 12 AMaZE build/exports, 13 parity harness (only with `-RequireParityHarness`),
14 architecture proof, 15 export inspection, 16 recon exports absent, 20-22 publish.

The recon backend script ends by running its LoadLibrary parity harness against oracle vectors
and exits with *its* code; without those vectors on the host that code is nonzero even when the
DLL is fine. It is therefore **recorded** (`reconBackendScript.exitCode`, `parityHarnessPassed`)
and not fatal unless the generator was run with `-RequireParityHarness`. The DLL itself is gated
on artifact presence, architecture and exports instead.

A second reason that harness fails on a default run: `-gencode=arch=compute_86,code=sm_86` emits
SASS for `sm_86` and **no PTX**, so the DLL cannot load on Ultra-Magnus's own `sm_89` 4090 at
all. To exercise the harness on the building host, request PTX as well --
`-CudaArchitectures sm_86,compute_86` (or add `sm_89`) -- and only then is
`-RequireParityHarness` meaningful. `sm_86` stays mandatory either way.

## 2. Exe and package (board host)

Fetch the pair job's artifacts directory, then:

```powershell
pwsh -NoProfile -File tools\profiling\bachelor\playback-attr-3-cuda-assemble.ps1 `
    -SourceCommit <same-40-hex-sha> -DllPairDir <pair-artifacts-dir> -OutDir <build-dir>
```

It verifies the pair manifest (`sourceCommit` equal to `-SourceCommit`, every file's sha256
matching the bytes on disk, a boolean `pendingSymbolPresence`) **before** deploying anything,
then builds the Release exe from a clean `git archive` -- verified through the same
`Assert-AttrCudaSourceArchive` the Ultra-Magnus job uses, so the zip's EOCD comment has to
declare `-SourceCommit` and the verified sha lands in `build.json` as `sourceArchiveSha256`.
Then: `build_buildinfo.h` injected into the
build dir before `qmake` (an archive has no `.git`, so the qmake-time probe would stamp
"unknown"), `qmake`, `mingw32-make -B release`, `windeployqt --release --no-translations
--compiler-runtime`, plus the four MinGW runtime DLLs `--compiler-runtime` misses (`libgomp-1`
above all). A sanitized-PATH `--batch --help` launch probe proves the package is self-contained
before publication. `-RuntimeDonorDir` optionally mirrors the reviewed FFmpeg payload by exact
name; without it `ffmpegRuntimeDeployed=false` is recorded rather than implied.

Published into `<build-dir>`, `build.json` **LAST**:

- `MLVApp-playback-attr-3-cuda-<sha12>-pkg.zip` (the deployed release dir, zipped as-is -- the
  exe inside keeps its unrenamed build name, `MLVApp.exe`)
- `MLVApp-playback-attr-3-cuda-<sha12>.exe`
- `igpu_recon_cuda-playback-attr-3-cuda-<sha12>.dll`
- `playback-attr-3-cuda-<sha12>-build.json` -- the layout the attribution job verifies, plus
  `pendingSymbolPresence`, `dllPairManifestSha256`, `sourceArchiveSha256` and `exeBuiltOnHost`.

**Keep the `MANIFEST_SHA256=` value off the `RESULT=ASSEMBLE_OK` line.** It is the lowercase
sha256 of the published `build.json`, and step 4 will not run without it. The staging generator
in step 3 echoes the same value as `buildManifestSha256`, so either receipt will do -- but it
must come from a step that ran, never from re-hashing a file someone dropped into the cache.

## 3. Stage into the Bachelor cache

```powershell
pwsh -NoProfile -File tools\profiling\bachelor\playback-attr-3-cuda-stage-job.ps1 `
    -SourceCommit <same-40-hex-sha> -BuildDir <build-dir> -OutDir <staging-dir>
```

Drop the emitted job into `C:\mlvtmp\mlv-agent\inbox` together with the four published files, by
their own names, as side-files. The job re-hashes each one against values baked in at generation
time, cross-checks the arriving `build.json`, publishes into `C:\mlvtmp\mlv-agent\cache`
transactionally with `build.json` **LAST**, and only then removes the side-files. Exit codes:
3 side-file missing, 4 hash mismatch, 5 manifest binding, **6 artifact name safety**,
20-22 publish.

**The file names are never taken from `build.json`.** Both the generator and the job on the host
derive the canonical basenames from `sourceCommit` through the same convention the assembler and
the attribution job use, and require an exact match; `build.json` supplies hashes only. Every
name must be a plain basename (no separator, no `..`, no colon) and every resolved cache and
inbox path must be a direct child of its root -- checked before any `Copy-Item`, `Move-Item` or
`Remove-Item`, because those two directories are written and deleted in on an unattended host.

`pwsh -File <jobId>.job.ps1 -VerifyOnly` runs that whole read-only prefix -- names, containment,
side-file presence, hashes, manifest binding -- and exits without touching anything. An
interrupted run leaves `build.json` unpublished by construction (it is copied last, after every
artifact has been renamed into place), so the attribution job's requirement that it be present is
what makes a partial stage safe: re-drop the side-files and re-run.

## 4. Attribution job (owner clip, id-only)

Run this for the owner's clip id. **No `-ClipPath` and no `-FixtureSha256`: both are refused
outright for an owner clip id (ATTR3-FOOTAGE-BIND-1).** The generator resolves the id through
`tools/gates/resolve_consented_clip.py` against the frozen consent table, bakes each resolved
part's path (base64 of its UTF-8 bytes), length and lower-case sha256 into the emitted job, and
the job re-verifies every part's content on Bachelor before anything opens. A resolver refusal
throws with the resolver's own typed status and emits nothing -- never a path.

```powershell
pwsh -NoProfile -File tools\profiling\bachelor\playback-attr-3-cuda-job.ps1 `
    -SourceCommit <same-40-hex-sha> -BuildManifestSha256 <64-lowercase-hex> `
    -ClipId <owner id, e.g. M16-1243> -OutFile <staging-dir>\<jobId>.job.ps1
```

**`-BuildManifestSha256` is mandatory, and the hub passes the sha the assembler printed**
(`MANIFEST_SHA256=` on its `RESULT=ASSEMBLE_OK` line, echoed by the staging generator as
`buildManifestSha256`). The Bachelor cache is mutable and the job does not own it: without this
value the job would believe whichever same-named `build.json` is sitting there, and replacing it
together with matching artifacts forges `pendingSymbolPresence` and the whole DLL association in
one move. The job hashes the cached manifest and refuses **before `ConvertFrom-Json`** -- a
parsed field is already a trusted field -- and then requires its `sourceCommit` to equal
`-SourceCommit`, its `dllPairManifestSha256` to be present and well formed, and its
`pendingSymbolPresence` to be a real boolean. Both hashes land in `evidence-manifest.json` under
`buildManifest`.

On Bachelor the emitted job re-verifies each resolved part against the live filesystem, AT ITS
OWN RESOLVER PATH -- exists, readable, length, and sha256 (case-insensitive) -- through the
shared `Test-AttrCudaFootagePart` verifier. Once every part passes, the job builds a PRIVATE,
neutrally-named directory under its own work tree (one hard link per verified part, contiguous,
never spelled from the caller's real names) and re-verifies each link's identity and content
against its source before opening anything: playback opens only that private link's part 0
(neutral base name `owner-clip`), never the owner's real path. Nothing this job publishes --
stdout, `summary.json`, `evidence-manifest.json`, or any other artifact -- ever names a source
path; every reader-facing output carries only the part's index and status. Any non-`PASS` part,
or a failure creating/opening/re-verifying a private link, fails closed (`OWNER_FOOTAGE_NOT_VERIFIED`
exit 19, `OWNER_FOOTAGE_LINK_CROSS_VOLUME` exit 21, or `OWNER_FOOTAGE_LINK_FAILED` exit 22),
closing whatever handles were already held and removing whatever private links were already
created, before PresentMon starts, before deploy, and before the smoke child. (The fixture route in
section 4b instead requires the clip path to sit directly in the agent cache, name that clip id,
and exist, then hashes it against `-FixtureSha256`, failing closed at
`FIXTURE_CONTENT_MISMATCH`, exit 17, on a mismatch.) The job also verifies all three package
cache artifacts (exe, DLL pair, PresentMon) against the authenticated `build.json` and **reads
`pendingSymbolPresence` from it**. PresentMon runs as a direct child,
inheriting the job-owned TEMP; if the agent account lacks ETW trace rights the job fails
`PRESENTMON_FAILED rc=6` and never falls back to the elevated scheduled task.

**Which log the gate reads.** `run-release-gui-smoke.ps1` does not write into
`<output dir>\logs`; it creates a GUID-nonced `logs-<stem>-<nonce>` directory and publishes the
per-run snapshot as `"$outputPath.run.log"`, exposed as `result.json` `log.path` and bound by
`evidence.runLogSnapshot.sha256`. The job reads **that** file -- resolved from the result of the
run it just executed, required to sit inside its own work tree, and required to hash to the
declared value -- and exits **16 `SMOKE_LOG_UNAVAILABLE`** if it is absent, unbound or
elsewhere. The aggregate rotating app log is recorded as `aggregateSourcePath` and never read:
the runner's own comment says it may grow after the run and carries no comparison authority.

Before any verdict the job takes the run's own `gpu_playback_recon.eligibility` line from that
snapshot and exits **15 `BACKEND_NOT_AVAILABLE`** unless `cuda_backend_available=1` **and**
`r16_available=1`, recording both plus `r16_reason`. A log with no eligibility line at all is
refused the same way: absence of the diagnostic is not evidence of eligibility. Other outcomes:
12 `VENUE_NOT_QUIESCENT`, 13 `GPU_RECON_FRAMES_ZERO`, 14 `CPU_FALLBACK_DETECTED`, 18
`SMOKE_RUN_FAILED` (the smoke run itself never produced a passing `result.json`), 23
`PRESENTMON_UNAVAILABLE` (PresentMon never produced a usable capture for the MLVApp process --
covers a missing/unreadable/columnless csv, a wait timeout, and a nonzero PresentMon exit code
alike, all typed and never destroying the smoke evidence already published; a wait timeout or
nonzero exit also publishes `presentmon.csv`, if it exists, and `presentmon-capture.json` before
exiting, the same evidence a parsing failure already left behind), 24 `DISPLAY_ASLEEP` (the
MLVApp chain presented frames but displayed none), 0 `MEASUREMENT_CAPTURED`. Display rates
(`presentedFps`/`displayedFps`) are for the MLVApp process id, summed across every swap chain
address it used inside the playback window -- a mid-run swap chain recreation (e.g. a resize) is
one logical preview, not two. It publishes `presentmon-series.csv`, `presentmon-capture.json`
(the PresentMon clock anchor, bracketed by a pre-spawn/post-spawn wall-clock pair and the
OS-reported process start, with the residual uncertainty in ms), `logs\smoke-run.log`,
`evidence-manifest.json`, `provenance.json` and `artifact-index.json`.

**Clock-bracket windowing.** PresentMon's own `TimeInMs=0` origin cannot be pinned to a single
instant, so display/presented rows are windowed under BOTH endpoints of the capture-start bracket
-- the OS-reported process start (or the pre-spawn wall clock, if the OS reported none) and the
post-spawn wall clock -- never just the earlier one. `summary.json`/`evidence-manifest.json`
carry the result as `clockBracket`: `earliest`/`latest` (each with `presentedCount`,
`displayedCount`, `presentedFps`, `displayedFps`), `rowsDifferingInWindowMembership` (how many
rows, any process, disagree on window membership between the two endpoints), and
`headline`/`headlineReason` naming which endpoint the reported `chains`/`selectedChain`/
`selectedChainRows` actually come from -- the endpoint admitting more DISPLAYED MLVApp rows,
then more presented rows, ties to the earlier endpoint. Neither endpoint is exact; ranking by
displayed rows first means `DISPLAY_ASLEEP` is reported only when neither endpoint admits a
displayed MLVApp row.

**Interval statistics exclude interval-less rows.** A row displayed only via `MsUntilDisplayed`
(observed on the very first present of a capture, when `MsBetweenDisplayChange` reads `NA`) has
no display-change interval to report -- `evidence-manifest.json`'s `presentMonStats`
(`meanMs`/`sdMs`/`cvPct`/percentiles/`fpsEquivalentMean`/`count`) and `presentMon.positiveSamples`
are computed only from rows with a positive `msBetweenDisplayChange`, never a `[double]$null`
coerced to `0.0`. `presentmon-series.csv` and `presentMonSamples` still carry every displayed row
(including the interval-less one, with an empty `msBetweenDisplayChange` cell);
`refresh_period_histogram.py` already skips blank/non-positive cells reading that csv.

The owner's consent for the clip on this card is recorded at
`.claude-state/coordination/dual-lane/receipts/owner-footage-consent-20260916.json`, with its
`-correction.json`. That receipt is evidence of consent, never an authorization -- the
authorization is the resolver's own cross-check of the id against the frozen consent table in
`tools/hooks/mlv-never-authorized.py` (`OWNER_CONSENTED_FOOTAGE`), re-verified against the live
filesystem by the job itself before anything opens. The job cites the receipt's file name in
`evidence-manifest.json`.

## 4b. Fixture rehearsal (no footage)

The chain above can be rehearsed end to end WITHOUT any owner footage, and should be, before a real
clip is ever opened. Pass `-ClipId tiny_dual_iso` or `-ClipId large_dual_iso`: the two clips tracked
in this repository under `tests/fixtures/clips`. NA-4 admits those already, with no `CLIP_OR_NONE`
line and no consent receipt, because they are repository fixtures rather than the owner's footage.

`tools/profiling/um-run.ps1` does not place a side-file into the agent CACHE by itself -- it drops
it into the agent INBOX (the same route every other side-file uses), and NA-4 does not admit a
bare Bachelor cache path either. The fixture has to be moved from inbox to cache through the
guarded route below, and the attribution job then has to open it FROM the cache without ever
naming a cache path on the command line. Four steps:

```powershell
# (i) emit the job that moves the fixture from the inbox into the cache through the guarded
#     route (tools/profiling/bachelor/attr3-stage-fixture-job.ps1). Note its own printed
#     RESULT=... FIXTURE_SHA256=<hex> JOB=<path> line -- steps (ii) and (iii) need it: the
#     staging job id is the JOB= file's basename with ".job.ps1" removed.
pwsh -NoProfile -File "tools\profiling\bachelor\attr3-stage-fixture-job.ps1" `
    -ClipStem tiny_dual_iso -FixturePath "<repo>\tests\fixtures\clips\<name>" -OutDir "<staging-dir>"

# (ii) submit the stage job, with the tracked fixture as its side-file. -AgentShare is
#     MANDATORY: um-run.ps1 defaults to the Ultra-Magnus share, not Bachelor's. -JobId is
#     <stageJobId> from step (i) -- DISTINCT from step (iv)'s <attrJobId> below; reusing one
#     id for both submissions is rejected with UMRUN_JOBID_IN_USE.
#     ATTR3-FOOTAGE-STAGE-SUBMIT-RETRY-1 round 11: a JobId's claim is never reclaimed by age any
#     more -- it is held until the submission that made it finishes (success or its own rollback)
#     or an operator removes it by hand. Retrying THIS SAME <stageJobId> while an earlier attempt's
#     claim is still sitting in the inbox (e.g. after killing a stuck `large_dual_iso` transfer) is
#     refused outright with "choose a new -JobId" -- pick a fresh <stageJobId> for the retry, or,
#     only if you are certain the earlier attempt is dead, remove
#     \\bachelor\mlv-agent\inbox\<stageJobId>.meta.json by hand first.
pwsh -NoProfile -File "tools\profiling\um-run.ps1" `
    -ScriptPath "<staging-dir>\<stageJobId>.job.ps1" -SideFile "<repo>\tests\fixtures\clips\<name>" `
    -JobId <stageJobId> -AgentShare \\bachelor\mlv-agent

# (iii) generate the attribution job for the fixture id, WITHOUT -ClipPath: the generator
#     derives the cache path itself and bakes in the hash step (i) printed. Choose an
#     <attrJobId> DIFFERENT from <stageJobId> and name -OutFile after it: that file's basename
#     is the attribution job id step (iv) submits with.
pwsh -NoProfile -File "tools\profiling\bachelor\playback-attr-3-cuda-job.ps1" `
    -SourceCommit <same-40-hex-sha> -BuildManifestSha256 <64-lowercase-hex> `
    -ClipId tiny_dual_iso -FixtureSha256 <the FIXTURE_SHA256= step (i) printed> `
    -OutFile "<staging-dir>\<attrJobId>.job.ps1"

# (iv) submit the attribution job the same way, again with -AgentShare explicit and its own
#     <attrJobId> from step (iii)'s -OutFile basename.
pwsh -NoProfile -File "tools\profiling\um-run.ps1" `
    -ScriptPath "<staging-dir>\<attrJobId>.job.ps1" -JobId <attrJobId> -AgentShare \\bachelor\mlv-agent
```

- **-ClipPath is OPTIONAL for a fixture id (ATTR3-FIXTURE-STAGE-1).** An owner clip id refuses
  `-ClipPath` outright (section 4): its footage path is resolved, never typed by a caller.
  Omitted for a fixture id, the generator derives it as the
  agent cache path for `-ClipId`; supplied, it must still resolve into the agent cache with
  `BaseName -ceq $ClipId`. `-FixtureSha256` is MANDATORY for a fixture id and REFUSED for an
  owner clip id: the emitted job hashes the cached clip before it is ever opened and fails closed
  at `FIXTURE_CONTENT_MISMATCH` (exit 17) on a mismatch -- a cache file name proves nothing about
  its bytes.
- **Unchanged for a fixture run.** `-BuildManifestSha256` still authenticates the staged manifest;
  the eligibility gate still exits 15 unless `cuda_backend_available=1` and `r16_available=1`.
- **What it establishes.** That staging, backend load, the eligibility gate, PresentMon capture and
  artifact publication all work on the measurement host, against the exact package under test.
- **What it does NOT establish.** Anything about performance. Both fixtures are small dual-ISO files;
  their timing is a plumbing proof, never a measurement. A fixture run records
  `fixtureRehearsal: true` in `summary.json` and `evidence-manifest.json` so it cannot be read as an
  attribution result, and a fixture run's numbers never enter a verdict in section 6.

## 5. Extract the histogram

```powershell
py -3 tools\profiling\refresh_period_histogram.py `
    --artifacts-dir <artifacts-dir> `
    --refresh-period-ms <Bachelor's panel refresh period in ms> `
    --out <artifacts-dir>\refresh-period-histogram.json
```

`--artifacts-dir` is the leg's own published artifact root (`evidence-manifest.json`'s
`artifactRoot`). It does three things at once: `--presentmon-csv`/`--frame-log` default to the
standard paths inside it (`presentmon-series.csv`, `logs\smoke-run.log`), and
the leg's `presentMonStatus` (and reason) is ALWAYS read from its `summary.json` (preferred) or
`evidence-manifest.json` (`presentMon.status`/`statusReason`) and is authoritative.

**PRESENTMON-HARNESS-ROBUSTNESS-3: one leg, one status.** With `--artifacts-dir`, an explicit
`--presentmon-status` may only AGREE with the leg's published status (a contradiction is refused, exit 1), and
explicit `--presentmon-csv`/`--frame-log` must resolve INSIDE that directory (another leg's data is refused), so one
leg's `ok` can never authorize another leg's capture. The emitted report always carries `presentMonStatus`
and `presentMonStatusReason`.

**PRESENTMON-HARNESS-ROBUSTNESS-2 (sol BLOCKER, pre-review): the status is not optional.** This
tool refuses to compute a histogram at all -- before reading either input file -- unless it knows
the leg's own `presentMonStatus`: pass `--artifacts-dir` (preferred, above) so it is read
automatically, or pass `--presentmon-status`/`--presentmon-status-reason` explicitly (e.g. against
a bare csv with no published artifacts dir). Omitting BOTH is a refusal (exit 1), never a
histogram computed from evidence whose sufficiency was never checked. A non-`'ok'` status (e.g.
`degraded`, `verified_zero_displayed`) also refuses, naming the status and reason -- the job's own
sufficiency gate already found the evidence too thin/absent to present as measured (see section
5a below for what that gate now checks).

`--refresh-period-ms` is Bachelor's actual panel refresh period and must be supplied. Without it
the tool estimates the period from the interval distribution's near-minimum cluster -- an
estimate that cannot distinguish a healthy all-1-refresh capture from a uniformly-stalled
all-2-refresh one, so it fails closed (`"ambiguous"`) whenever too little of the distribution
falls outside that cluster to cross-check the assumption. `refreshPeriodSource` records which
path was taken: `"nominal"` when supplied, `"estimated-min-interval"` when not.

## 5a. The sufficiency gate (presentMonStatus)

`playback-attr-3-cuda-job.ps1` tags every leg's PresentMon evidence `ok` or `degraded` in
`evidence-manifest.json`'s `presentMon.sufficiency`/`presentMon.status` and `summary.json`'s
top-level `presentMonStatus`, before this histogram ever runs. `'ok'` requires ALL THREE,
independently:

- **count** -- at least `minIntervalCount` (30) positive-`MsBetweenDisplayChange` rows.
- **app-swap coverage** -- `positiveSamples / appSwapCount >= minCoverageFraction` (0.5), where
  `appSwapCount` is an APP-SIDE count PresentMon never produced: the MLVApp log's own
  `playback_smoke.gpu_window_swaps` line (real confirmed on-screen swaps) when swap telemetry is
  active, falling back to `playback_smoke.gate`'s `frames_presented` when it is not. (r1's own
  `coverageFraction` divided by PresentMon's own `presentedCount` instead -- numerator and
  denominator from the same csv, so a capture that lost the tail of a leg after a short healthy
  prefix still read 100% coverage of its own truncated rows; fixed in r1b.)
- **temporal** -- no gap between positive-interval rows, including the head/tail gaps to the
  playback window's own bounds, exceeds `maxGapMs` (5000ms). This is what catches a captured
  PREFIX followed by silence: count and app-swap coverage alone can both clear even though the
  capture measured almost none of the actual leg.

A `degraded` leg's `presentMonStatusReason` names every arm that failed, with its own numbers.
`DISPLAY_ASLEEP` and `PRESENTMON_UNAVAILABLE` tag `presentMonStatus` too (`verified_zero_displayed`
/ `unavailable`) before the gate above even runs, since PresentMon never produced a chain to
measure at all.

## 6. Reading the within-run verdict

The output JSON (`schema: mlvapp.refresh-period-histogram.v1`) has two sections:

- **`presentMon.buckets`** -- the share of presented frames landing on `"1"`, `"2"` or `"3+"`
  display refreshes. A healthy CUDA playback run should show `"1"` dominating `share`; a
  meaningful `"2"` or `"3+"` share is evidence of missed-refresh stalls.
- **`frameLog.regions`** -- `p50Ms`/`p95Ms` per `prep_region_*`. Whichever of
  `prep_region_setup/gpu/image/present/finish` has the largest share of `prep_region_total_ms`
  is the dominant cost; a large `prep_region_unattributed_ms` means the probes don't yet cover
  where the time is going.

Run the test suites before trusting a new extraction or a new job:

```powershell
py -3 -m pytest -q tools\profiling\test_refresh_period_histogram.py
py -3 -m pytest -q tools\repo_hygiene\test_playback_attr_3_cuda_split_route.py
```
