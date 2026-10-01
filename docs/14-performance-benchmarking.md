# Performance Benchmarking

Migrated from `tests/perf/README.md` (the full perf-harness reference) and
the perf-specific sections of `tests/README.md` (the `perf_tests` bullets,
`run_runtime_profile.ps1`, `MLVAPP_STAGE_TIMING`, `--profile-playback`
headless profiler details, telemetry key list). Originally written by the
maintainers; edits tracked via git history.

This document covers:

1. The `perf_tests` harness — what it measures, options, and JSON output.
2. The local no-intervention `run_runtime_profile.ps1` wrapper.
3. The app-backed `MLVApp.exe --profile-playback` headless mode.
4. Release-tree CDNG export profiling and matrix A/B bundles.
5. The full telemetry key list emitted by playback-profile JSON.
6. Baselines (`tests/perf/baselines.json`).
7. Interpretation notes (single-thread vs multithread; cold-8bit; LJ92).

## Release CDNG export profile matrix

For Lane A E3 export pipeline work, prefer the release-tree matrix wrapper
instead of hand-running one-off baseline/candidate pairs. It launches the
existing paired A/B runner for each named case and repeat, then writes one
`matrix-summary.json` under `.claude-state/profiling/<run>/`.

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File tools\profiling\run-release-cdng-export-profile-matrix.ps1 `
  -RepoRoot . `
  -CasesPath .claude-state\profiling\cdng-export-cases.json `
  -OutputDir .claude-state\profiling\2026-06-19-cdng-export-matrix `
  -CdngCodec lossless `
  -CandidateUsePayloadHandoff `
  -CandidateUseAsyncWriter `
  -CandidateAsyncWriterQueueDepth 2 `
  -CandidateAsyncWriterThreadCount 2 `
  -CandidateEnableGpuExport `
  -CandidateGpuExportDll C:\path\to\igpu_recon_cuda.dll `
  -CandidateGpuExportBackend cuda `
  -RequireBaselineNoGpuExportAttempt `
  -RequireCandidateGpuExportAttempt `
  -RequireCandidateGpuExportReplacement `
  -RequireDngHashMatch `
  -MaxFrames 16 `
  -Repeats 3 `
  -FailOnRegression
```

Cases are JSON objects with repo-relative or absolute paths:

```json
{
  "cases": [
    {
      "name": "m16-1210-master",
      "clipPath": "C:/temp/MLV/M16-1210.MLV",
      "receipt": "C:/temp/MLV/master.marxml",
      "cdngCodec": "lossless",
      "maxFrames": 16,
      "repeats": 3
    }
  ]
}
```

The matrix summary records case/run verdicts, selected CDNG codec,
baseline/candidate frame counts, baseline/candidate wrapper elapsed
milliseconds, elapsed deltas, candidate async queue capacity/max-queued,
candidate async worker count/jobs started/jobs finished/max-active,
baseline/candidate GPU-export intent plus attempt/replacement/trusted/allocation counters,
frame-total avg/p95 deltas, producer-frame deltas, producer-queue-idle deltas,
writer-completion-lag deltas, writer-queue-wait deltas, payload handoff
(`payload_clone_ms`) deltas, key `llrawproc_*` / `dng_compress_ms` bottleneck
deltas, compression substage deltas, compression byte totals, and comparator
failures. Full `llrawproc_*` substage timing remains in each run's export
profile JSON. The per-run profile also records
`dng_compress_encode_ms`, `dng_compress_copy_ms`,
`dng_compress_cleanup_ms`, `dng_compress_bytes_valid_frames`,
`dng_compress_input_bytes_total`, `dng_compress_output_bytes_total`, and
per-frame compression input/output byte counts. Default runs record
`dng_compress_placement=producer_before_payload` and
`async_writer_can_overlap_dng_compress=false`; opt-in async-writer compression
runs (`MLVAPP_CDNG_EXPORT_ASYNC_WRITER_COMPRESS=1` / wrapper
`-UseAsyncWriterCompression`) can instead record
`dng_compress_placement=async_writer_after_payload` and
`async_writer_can_overlap_dng_compress=true`. E3 compression-placement
experiments can therefore compare throughput and placement, not just elapsed
milliseconds. The stage profile comparator emits those root counters under
`compression`, including input/output MiB/s deltas, output-ratio deltas, and
placement/overlap changes; A/B `summary.json` and matrix `runs[]` rows copy the
same fields plus
`dngCompressEncodeAvgDeltaMs`, `dngCompressCopyAvgDeltaMs`, and
`dngCompressCleanupAvgDeltaMs` forward for direct promotion review.
Treat
`candidateAsyncWriterMaxActive=1` on a multi-worker candidate as evidence that
extra workers did not overlap actual writes in that run, even if the configured
thread count is higher. A tiny
checked-in fixture matrix is only a schema/tooling smoke; E3 promotion needs a
bounded real-footage matrix whose clips, receipts, frame caps, and repeats match
the export scenario being judged.

For UltraMagnus CPU-vs-GPU export proof, leave the baseline on CPU and enable
only the candidate with `-CandidateEnableGpuExport` plus
`-CandidateGpuExportDll`. `-CandidateGpuExportBackend <name>` can pin the
loader's requested backend name for candidate proof packets; leaving it unset
preserves the loader default of `cuda`. Add `-RequireBaselineNoGpuExportAttempt`,
`-RequireCandidateGpuExportAttempt`, and
`-RequireCandidateGpuExportReplacement` when the run is meant to promote GPU
export; those gates fail the A/B or matrix verdict if the baseline attempts GPU
work or if the candidate does not attempt/replace every profiled frame. Add
`-RequireDngHashMatch` to fold the DNG SHA256 companion into the wrapper verdict
instead of relying on a separate manual step. The legacy `-EnableGpuExport`
switch remains valid, but it enables both sides and is therefore better suited
to same-mode A/A or GPU-vs-GPU profiling. When a GPU proof gate fails, inspect
`gpuExportSkipReasonCounts` in `summary.json` / `matrix-summary.json`; the
UltraMagnus wrapper also includes a compact `skip_counts=...` rollup in proof
failure text.

For a throughput-only candidate after the scoped RTX 4090 parity proof is
accepted, add `-CandidateGpuExportTrusted` and
`-RequireCandidateGpuExportTrusted`. That sets
`MLVAPP_GPU_EXPORT_TRUSTED=1` only for the candidate leg, bypasses the CPU
Dual-ISO shadow oracle, and requires `gpuExportTrustedFrames == frameCount`.
The default GPU export path stays CPU-authoritative; trusted mode is a proof
surface for measuring the candidate without the shadow-validation cost, not a
default export behavior change.

Use `tools\profiling\invoke-ultramagnus-cdng-export-evidence.ps1` for the
authoritative RTX 4090 export proof path. It refuses dirty local staging,
mirrors the clean repo to `\\ultra-magnus\G\Temp\mlvapp-cdng-export-evidence`,
builds/deploys `igpu_recon_cuda.dll` on UltraMagnus, rebuilds the release tree,
runs the matrix wrapper with the GPU replacement and DNG hash gates above, and
imports a compact JSON/log evidence packet under
`.claude-state\profiling\ultramagnus-cdng-export\`. The wrapper generates an
effective proof receipt from `-Receipt` by forcing the GPU-supported Dual ISO
export gate (`dualIsoInterpolation=1`, alias-map on, full-res blending on, and
chroma-smooth off); use `-UseReceiptAsIs` only when intentionally debugging the
raw source receipt. The packet records both `sourceReceipt` and the effective
receipt. The default proof uses `G:\Temp\mlv-gpu-profile\clips\M16-1327.MLV`,
`receipts\FastProxy.marxml`, both `uncompressed` and `lossless` CDNG,
`maxFrames=4`, and `repeats=1`; widen only when the proof question requires it.
Pass `-TrustedGpuExport` only when deliberately generating the trusted
throughput packet described above; that adds the candidate trusted gate to the
remote matrix and records `trustedGpuExport=true` in the packet summary.
Use `-CandidateGpuExportBackend <name>` when the packet must prove a non-default
or explicitly pinned backend request; the remote summary records the value and
passes it through to the matrix as `MLVAPP_GPU_EXPORT_BACKEND`.
For the E3 lossless/compression-overlap follow-up, the wrapper can also pass
candidate-only async writer controls to the same remote proof path:
`-CandidateUseAsyncWriter`, `-CandidateUseAsyncWriterCompression`,
`-CandidateAsyncWriterQueueDepth`, and `-CandidateAsyncWriterThreadCount`.
Add `-RequireElapsedImprovement` and optionally
`-MinElapsedImprovementPercent <n>` when the packet must fail unless the
candidate improves wrapper wall-clock elapsed time; keep `-FailOnRegression`
for the separate frame-total avg/p95 attribution gate. The baseline remains the
plain CPU export path; use these switches only for bounded candidate
experiments that still require GPU attempt/replacement, trusted-frame, and DNG
hash gates. When passing more than one clip through a native `pwsh.exe -File`
invocation, either use a child `pwsh -Command` with an explicit PowerShell array
or pass a comma-separated `-ClipNames` value; the wrapper normalizes
comma-separated clip names before staging the remote job.
The accepted 2026-06-19 proof packet is
`.claude-state\profiling\ultramagnus-cdng-export\imported\packet-20260619T172721\summary.json`
with local packet
`.claude-state\profiling\ultramagnus-cdng-export\remote-packets\ultra-magnus-20260619T172721-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `68260EFC52BFDF6D372866D0E1119BBD9FEAA1C4271764315E248A82F82243D2`).
It records `ULTRA-MAGNUS`, RTX 4090, release SHA256
`FA8B20D51113B50AA77331E77604852375B5061357017F29CC0669349E4DB8FD`, backend
DLL SHA256 `A63212BDA5C6439257D2100F9EA1A5F490A25F740FD9961325F5683552CE3D65`,
candidate GPU attempts/replacements 4/4 for both uncompressed and lossless,
zero candidate skips, and DNG hash PASS 8/8. Treat this as scoped export
replacement and byte-identity proof, not as a broad throughput claim.
The larger 2026-06-19 throughput probe is
`.claude-state\profiling\ultramagnus-cdng-export\imported\packet-20260619T173809\summary.json`
with local packet
`.claude-state\profiling\ultramagnus-cdng-export\remote-packets\ultra-magnus-20260619T173809-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `F2CF60E1B600D30551AA91B04D3972CF859B0788C4ABE1F1E514602ECCC2C56E`).
It used `maxFrames=16`, `repeats=3`, and DNG hash PASS 96/96, but CPU baseline
was faster in every repeat. Keep it classified as a negative throughput probe,
not an E3 promotion packet; it measured the default shadow validator, which
still pays the CPU Dual-ISO oracle plus the GPU run and byte comparison.
The trusted E3 measurement packet is
`.claude-state\profiling\ultramagnus-cdng-export\imported\packet-20260619T180606\summary.json`
with local packet
`.claude-state\profiling\ultramagnus-cdng-export\remote-packets\ultra-magnus-20260619T180606-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `8B4A455CC4AB2434A9D4B7C37309C516A719183037A746F8D7E6283CF82FB971`).
It used commit `2ba1c2e596fd1b8cda2a8add44d397f3792aafaf`, release SHA256
`E99F592300AC8ACA00F3B238539711D3834DB1260228590A189A9532B00933A6`,
`-TrustedGpuExport`, `maxFrames=16`, `repeats=3`, and DNG hash PASS 96/96.
Candidate trusted/attempted/replaced frames were 96/96/96. Uncompressed
`M16-1327` improved from 5682.102 ms to 4602.206 ms average wall time
(-1079.896 ms, -15.705%), with average `frameTotalAvgDeltaMs=-31.004` and
`llrawprocDualIsoAvgDeltaMs=-22.625`. Lossless stayed byte-identical and
trusted, but moved from 6468.414 ms to 6775.267 ms average wall time
(+306.853 ms, +5.113%) despite `llrawprocDualIsoAvgDeltaMs=-8.563`, so treat
the packet as promotion for the trusted measurement gate and as the next
lossless/compression-overlap bottleneck, not as broad E3 pipeline completion.
The three-clip trusted GPU plus async-writer-compression packet is
`.claude-state\profiling\ultramagnus-cdng-export\imported\packet-20260619T181722\summary.json`
with local packet
`.claude-state\profiling\ultramagnus-cdng-export\remote-packets\ultra-magnus-20260619T181722-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `380717DC753CFD7B01DDFE1E5348DBD402F5CF259DDC00D16D27B4D72AAD2155`).
It used commit `7367ed8bb80e5bbe7105b6ff618ac775bae5ee3c`, release SHA256
`E99F592300AC8ACA00F3B238539711D3834DB1260228590A189A9532B00933A6`, clips
`M16-1210`, `M16-1327`, and `M16-1347`, `lossless`, `maxFrames=16`,
`repeats=3`, queue depth 2, two candidate writer threads, and the trusted GPU
gates. Result: 9/9 runs PASS, DNG hash PASS 144/144, candidate
trusted/attempted/replaced frames 144/144/144, and writer overlap
`candidateAsyncWriterMaxActive=2` in every run. Average wall-clock elapsed
improved from 5706.215 ms to 4445.874 ms (-1260.341 ms, -22.048%); per-clip
elapsed deltas were M16-1210 -1403.277 ms (-22.768%), M16-1327 -1247.415 ms
(-22.598%), and M16-1347 -1130.333 ms (-20.779%). Keep it classified as an
opt-in candidate, not default policy: frame-total attribution still averaged
+19.773 ms and p95 +61.750 ms because the producer-frame win (-102.715 ms) is
paid back as writer-completion lag (+122.487 ms), with writer-side compression
+11.200 ms on average. Promotion needs an explicit async-pipeline gate that
separates wall-clock export throughput from completion-lag attribution.
That gate is now available as `-RequireElapsedImprovement` /
`-MinElapsedImprovementPercent` on the A/B, matrix, and UltraMagnus wrappers.
The gate-enforced refresh is
`.claude-state\profiling\ultramagnus-cdng-export\imported\packet-20260619T182703\summary.json`
with local packet
`.claude-state\profiling\ultramagnus-cdng-export\remote-packets\ultra-magnus-20260619T182703-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `28A5E842F6CB277D0900317DF8B7E1FD5754912C80FE4E06CE0E54906EC32D4A`).
It used commit `b88fb04b465a4bb8af34471afae1130f74031491`, the same release
SHA256 `E99F592300AC8ACA00F3B238539711D3834DB1260228590A189A9532B00933A6`,
the same three lossless clips, and `-RequireElapsedImprovement
-MinElapsedImprovementPercent 10`. Result: 9/9 PASS, DNG hash PASS 144/144,
candidate trusted/attempted/replaced frames 144/144/144, minimum row elapsed
improvement 14.925%, and average wall-clock elapsed improved from 6570.353 ms
to 4934.366 ms (-1635.987 ms, -24.687%). Frame-total attribution remained a
separate review signal (overall avg +4.316 ms, p95 +12.605 ms), so this packet
proves the elapsed gate and candidate throughput, not default-policy promotion.
If UltraMagnus is acting as a runner
without the Qt/MinGW build tree, rebuild `platform\qt\build-release\release`
locally first, let the wrapper stage that release tree, and pass
`-SkipRemoteBuild`; the summary records that the remote build was skipped.

After any matrix or standalone A/B run that claims CDNG output correctness, run
the DNG byte-identity companion before interpreting the timing result:

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File tools\profiling\compare-cdng-dng-output-hashes.ps1 `
  -MatrixDir .claude-state\profiling\<matrix-run> `
  -FailOnMismatch
```

For a single A/B bundle, use the bundle summary directly:

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File tools\profiling\compare-cdng-dng-output-hashes.ps1 `
  -AbSummary .claude-state\profiling\<ab-run>\summary.json `
  -FailOnMismatch
```

`-SummaryJson` is an alias for `-AbSummary`. Matrix mode follows each run's
A/B `summary.json`; standalone A/B mode reads that file directly. Both modes
compare every baseline/candidate DNG pair by relative path, length, and SHA256,
then write `dng-hash-comparison.json` beside the selected summary file.

When raw frame-total gates are noisy, compare a matching identity A/A matrix
against the feature A/B matrix instead of reading the feature matrix in
isolation:

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File tools\profiling\compare-cdng-export-matrices.ps1 `
  -IdentityMatrix .claude-state\profiling\<identity-run>\matrix-summary.json `
  -FeatureMatrix .claude-state\profiling\<feature-run>\matrix-summary.json `
  -Output .claude-state\profiling\<calibration-run>\calibration.json
```

The calibration output uses
`release-cdng-export-matrix-calibration.v4`. It fails closed unless the identity
input is `comparisonMode=identity-aa`, the feature input is
`comparisonMode=feature-ab`, both matrices use matching alternate-run-order
settings, and case/repeat/run-order keys match exactly. The JSON records global
and per-case metric envelopes, median/p95/positive-max summaries, and
stage-attribution metrics from each run's `compare.json` when available. The
`stageAttribution` object names both the dominant positive feature-average
stage and the dominant positive-max identity-envelope excess stage, so a
frame-total miss has a machine-readable first explanation before blame is
assigned to a candidate scheduler or handoff. The `schedulerAttribution` object
separates producer-frame / producer-idle deltas from writer-completion-lag /
writer-queue-wait deltas, including p95 metrics, so async-writer runs can show
whether the producer returned faster while completion backlog still failed the
gate. Compression byte and MiB/s deltas are reported separately under
`compressionThroughput`; throughput deltas are positive-good when they improve
and do not participate in the frame-regression verdict. `blockingReasons`
records the decisive gate; only global frame-total average and p95 envelopes
decide `WITHIN_IDENTITY_ENVELOPE` versus `EXCEEDS_IDENTITY_ENVELOPE`.

## `perf_tests` harness

`perf_tests` is a lightweight benchmark harness built on the same tiny Dual
ISO fixture used by the direct pipeline goldens. It also benchmarks the
checked-in `large_dual_iso` fixture in the same run, and can be pointed at
another local fixture when you want to override that default.

### What it measures

- `getMlvProcessedFrame16()` and `getMlvProcessedFrame8()` for both full
  Dual ISO and preview Dual ISO.
- Always benchmarks the checked-in `tiny_dual_iso` fixture.
- Benchmarks the checked-in `large_dual_iso` fixture when its paired receipt
  is present under the conventional path.
- Optionally benchmarks one extra fixture from:
  - `--extra-clip`, `--extra-receipt`, and `--extra-label`
  - or `MLVAPP_PERF_EXTRA_CLIP`, `MLVAPP_PERF_EXTRA_RECEIPT`,
    `MLVAPP_PERF_EXTRA_LABEL`, and `MLVAPP_PERF_EXTRA_SAMPLE_FRAMES`
  - or the conventional checked-in names
    `tests/fixtures/clips/large_dual_iso.mlv` and
    `tests/fixtures/receipts/large_dual_iso_hq.marxml`.

For each measured fixture/mode pair the harness reports `average_ms`,
`median_ms`, `min_ms`, `max_ms`, and derived FPS.

### JSON artifacts

- `--json-output <path>` writes a JSON artifact with metadata, results, and
  check status when provided.
- The JSON includes per-iteration samples, trimmed averages, and
  coefficient-of-variation stats so noisy paths can be diagnosed after the
  run.
- The JSON output now includes per-scenario residual aggregates when
  available; the currently stable field is
  `residuals.llrawproc_shared_lock_ms`, which is measured inside the render
  thread and does not rely on downstream stage-log pairing.
- Newer llrawproc ownership slices also emit:
  - `residuals.llrawproc_dualiso_refine_lock_ms`
  - `residuals.llrawproc_publish_lock_ms`

  These are useful when judging whether additional Dual ISO ownership work
  is still paying off, and they should be compared warm-vs-warm in the same
  build session.

### Stage timing

- `--stage-log <path>` appends per-stage timing output directly to a file
  for later analysis; this also enables `MLVAPP_STAGE_TIMING=1` for the
  run.
- `MLVAPP_STAGE_TIMING=1` (or `--stage-timing`) emits per-stage timing
  lines while rendering through the app, pipeline tests, or perf harness.

### Cache and threading controls

- `--cold-8bit` clears processed preview caches before every 8-bit sample so
  `full8`/`preview8` measure a colder processed-frame path instead of
  exact-cache hits.
- `--raw-cache-mb <n>` enables the MLV raw cache inside the harness for
  local contention profiling.
- `--cache-cpu-cores <n>` chooses how many cache-worker cores the harness
  uses when raw caching is enabled.

### Built-in synthetic scenarios

The built-in tiny correctness fixtures now include:

- `tiny_dual_iso_darkframe` — reuses the tiny checked-in clip and injects
  synthetic dark-frame payload into shared llrawproc state so the worker
  dark-frame snapshot split can be measured on a warm steady path.
- `tiny_dual_iso_stripes` — for the forced stripes compute/publish seam.
  This is also the direct regression seam for the claim-before-unlock
  `compute_stripes` flow, so inspect its JSON residuals even if the overall
  perf harness exits nonzero on an unrelated threshold.

The checked-in perf receipts still run with `darkFrame=0`, so warm
cache-enabled profiling runs measure surrounding llrawproc behavior but do
not directly benchmark the dark-frame snapshot split.

### Baselines

`perf_tests` always applies portable relative checks from
`tests/perf/baselines.json`:

- `tiny_dual_iso.preview16_speedup_vs_full16.min` remains the portable
  cross-machine "preview must stay faster" floor.
- `large_dual_iso.preview16_speedup_vs_full16.min` is ready for the
  optional larger fixture when that fixture exists locally or is checked in
  later.
- `preview8_speedup_vs_full8` is still reported in output/JSON, but it is
  not treated as a portable hard gate because it was too noisy across local
  runs.
- `full8` medians without `--cold-8bit` should be read as a warmed-cache
  metric; use the cold mode when you want to profile the actual
  16-bit-to-8-bit render path instead of the exact processed-frame cache.

`perf_tests` applies explicit local absolute gates when a matching baseline
profile exists in `tests/perf/baselines.json`:

- Local profile checks currently gate `median_ms` for the stable 16-bit
  paths (`full16` and `preview16`) on each fixture.
- 8-bit paths are still reported and stored in the local profile, but they
  are guarded by the absolute watchdog ceilings and relative speedup floors
  rather than the local median gate because their medians were too bursty
  across runs.
- The separate `--cold-8bit` profile variant now gates the colder `full8`
  and `preview8` medians directly, so the warmed exact-cache path and the
  colder tone-map/render path are both pinned without making the cold run
  also own the 16-bit median gate.
- `average_ms` is still reported and stored for context, but not used as the
  hard local gate because it is more sensitive to one-off outliers.
- The selected profile defaults to an auto-generated local machine key.

Baseline lifecycle:

- `--update-baseline` refreshes the auto-selected local perf baseline
  profile in `tests/perf/baselines.json`. Update mode still prints any
  failing checks, but it exits successfully after writing the refreshed
  profile so the baseline can be intentionally advanced.
- `--require-baseline` turns the local baseline profile from a warning into
  a hard gate.
- The warmed local baseline profile continues to gate the stable 16-bit
  medians plus the warmed 8-bit medians; the `--cold-8bit` profile variant
  now gates the colder 8-bit medians specifically so the processed-frame
  exact-cache path and the true 16->8 render path are both covered without
  making 16-bit cold runs responsible for the extra `--cold-8bit` noise.

### Useful options

- `--baseline <path>` overrides the baseline file location.
- `--baseline-profile <id>` lets you pin a named profile instead of the
  auto local key.
- `--extra-clip <path>` overrides the conventional larger fixture with a
  different local clip.
- `--extra-receipt <path>` points the extra fixture at a receipt.
- `--extra-label <id>` controls the result/baseline key prefix for the extra
  fixture.
- `--extra-sample-frames <n>` controls how many distinct frames the extra
  fixture cycles through during measurement.
- `--regression-pct <pct>` overrides the allowed slowdown versus the
  selected local baseline.
- `--stage-log <path>` appends stage-timing output directly to a file for
  later analysis.
- `--cold-8bit` clears processed preview caches before each 8-bit sample so
  cache hits do not dominate the reported `full8` median.
- `--raw-cache-mb <n>` enables the MLV raw cache inside the harness for
  local contention profiling.
- `--cache-cpu-cores <n>` chooses how many cache-worker cores the harness
  uses when raw caching is enabled.
- `--help` prints the local harness usage.

### Typical local runs

Standard 10-iteration run with JSON output:

```powershell
$env:PATH='C:\Qt\Tools\mingw1310_64\bin;C:\Qt\6.10.2\mingw_64\bin;' + $env:PATH
.\tests\build-all\perf\release\perf_tests.exe --iterations 10 --json-output .\tests\build-all\perf\perf-results.json
```

Benchmark tiny plus one larger local fixture without checking the larger
clip into the repo:

```powershell
$env:PATH='C:\Qt\Tools\mingw1310_64\bin;C:\Qt\6.10.2\mingw_64\bin;' + $env:PATH
.\tests\build-all\perf\release\perf_tests.exe `
  --iterations 10 `
  --extra-clip C:\bench\large_dual_iso.mlv `
  --extra-receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml `
  --extra-label large_dual_iso `
  --extra-sample-frames 8
```

The extra fixture is intended for broader playback/perf coverage, so it
cycles across more distinct frames than the tiny correctness fixture by
default.

Seed or refresh the current machine baseline profile:

```powershell
$env:PATH='C:\Qt\Tools\mingw1310_64\bin;C:\Qt\6.10.2\mingw_64\bin;' + $env:PATH
.\tests\build-all\perf\release\perf_tests.exe --iterations 10 --update-baseline
```

Require a matching local profile and fail if it is missing or stale:

```powershell
$env:PATH='C:\Qt\Tools\mingw1310_64\bin;C:\Qt\6.10.2\mingw_64\bin;' + $env:PATH
.\tests\build-all\perf\release\perf_tests.exe --iterations 10 --require-baseline
```

## Runtime-profile wrapper (`run_runtime_profile.ps1`)

`tests/perf/run_runtime_profile.ps1` is the local no-intervention profiling
wrapper. It:

- Discovers or builds `perf_tests.exe` (preferring the current
  `tests/perf/debug/perf_tests.exe` before scanning older build trees).
- Runs a thread-count matrix.
- Writes JSON + stage-log artifacts into `.claude-state/profiling/<timestamp>/`
  (per the workspace policy in [02-developer-guide.md §15.4](02-developer-guide.md#154-profiling-artifacts)
  and [AGENTS.md](../AGENTS.md): `.claude-state/` for ephemeral scratch,
  never `.claude/`). Older runs may have leaked into `.claude/profiling/`;
  the wrapper as currently shipped writes only to `.claude-state/profiling/`.
- Keeps those artifacts even if perf assertions fail (useful when the point
  is diagnosis, not gating).

Usage:

```powershell
.\tests\perf\run_runtime_profile.ps1 -Iterations 10
```

Pass `-Cold8bit` when you want a colder 8-bit processed-frame measurement:

```powershell
.\tests\perf\run_runtime_profile.ps1 -Iterations 10 -Cold8bit
```

Pass `-RawCacheMB / -CacheCpuCores` when you want the raw cache left on for
contention profiling:

```powershell
.\tests\perf\run_runtime_profile.ps1 -Iterations 10 -RawCacheMB 128 -CacheCpuCores 4
```

The wrapper now prefers the repo-relative
[`tests/perf/debug/perf_tests.exe`](../tests/perf/debug/) before searching
older build trees.

## App-backed `--profile-playback` mode

The main app supports a headless Qt playback profiler via:

```
MLVApp.exe --profile-playback --input <clip> --output <json>
```

That mode steps frames through the real
`MainWindow` / render-thread / `drawFrameReady()` path and can optionally
apply a receipt, enable scopes/zebras, leave raw caching on, and capture a
stage-timing log.

Capture the real Qt playback path instead of the engine-only perf harness. On
the Windows release tree, prefer the repo wrapper because it pins
`QT_QPA_PLATFORM=windows` and the deployed `platforms\qwindows.dll` path; do
not force `offscreen` unless you have explicitly deployed an offscreen platform
plugin:

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File tools\profiling\run-release-playback-profile.ps1 `
  -RepoRoot . `
  -Input tests\fixtures\clips\large_dual_iso.mlv `
  -Receipt tests\fixtures\receipts\large_dual_iso_hq.marxml `
  -Frames 8 `
  -Output .claude-state\profiling\playback-ui\large-step-cache.json `
  -AdditionalArgs @('--stage-log', '.claude-state\profiling\playback-ui\large-step-cache-stage.log', '--raw-cache-mb', '128', '--cache-cpu-cores', '4')
```

### Selectors and processing controls

- `--gpu-preview-processing <auto|cpu|gpu>` exercises the production
  preview-policy seam without depending only on the environment variable.
- `--gpu-bilinear-debayer <auto|cpu|gpu>` is the production-adjacent control
  for the existing experimental bilinear GPU debayer path.
- `--gpu-amaze-debayer <auto|cpu|gpu>` is the explicit Full Quality AMaZE GPU
  selector. It remains off by default; `gpu` implies the preview-processing GPU
  seam and falls back to CPU AMaZE with telemetry if the CUDA DLL path is
  unavailable or unsupported.
- `--playback-processing <auto|receipt|subset>` controls playback-processing
  mode. Current local semantics:
  - `auto` defaults to the receipt path.
  - `subset` is an explicit opt-in.
  - This is intentional because the subset path currently loses to the
    receipt path on the tested Dual ISO CPU-only VM workload.
- Playback preview mode is a separate quality/performance policy from scale:
  - `Sharp / Smooth Preview` is the default quality-first policy.
  - `Aggressive Performance Preview` opts into faster preview contracts, such
    as x4 HQ mean23 early reconstruction where gates allow and Dual ISO raw
    decode-ahead overlap for compatible reduced x4/x8 previews.
  - For scripted profiles, use `MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW=1` or
    `MLVAPP_PLAYBACK_PREVIEW_MODE=aggressive_performance`. Use `0` or
    `sharp_smooth` for the default behavior.

### GUI smoke Auto proof

`tools/profiling/run-release-gui-smoke.ps1` defaults to deterministic Auto
playback validation (`-QualityMode auto`, `-ExpectedQualityMode 2`,
`-ScaleFactor 4`, `-ExpectedScaleRequest 4`) unless
`-UsePersistedPlaybackSettings` or explicit overrides are supplied. For default
Auto smokes, the wrapper now requires the app's `playback_smoke.summary` Auto
telemetry and exposes it as `visualQuality.autoDecision`: target FPS, last
decision reason, average/budget milliseconds, FPS-equivalent cadence, sample
count, headroom capability, validated no-readback latch state, and demotion
state. Treat `visualQuality.autoDecision.fieldsPresent=true` as the first-line
proof that an Auto smoke is carrying the P4 capability evidence, then inspect
the specific values before claiming a capability-aware quality decision. The
wrapper also emits `visualQuality.autoDecision.capabilityConsistent` and
`validation.autoDecisionCapabilityConsistent`; default Auto smokes fail when
headroom sharpening is reported without an active validated no-readback latch,
when headroom capability is true but validated capability is false, or when
validated and demoted capability are both true in the same summary. The headroom
capability is shape-scoped: Dual ISO `GPU Tex NR` telemetry does not arm
non-Dual-ISO `HQ x2` promotion; that promotion requires a non-Dual-ISO
no-readback observation in the current Auto run.
Default GUI smokes require at least two presented frames, distinct first/last
presented frame ids, and a bounded skipped/unpresented ratio. Timeline movement
and the GUI FPS label are diagnostic only when these presentation invariants
pass. `-AllowZeroPresentedFrames` now requires the explicit `-LaunchOnlyProbe`
declaration; launch-only mode is mechanically incompatible with frame telemetry,
screenshots, artifact detection, and lifecycle stress. Such runs must not enter
an A/B or be cited as visual playback, Auto sampler, FPS, cadence, color, or
product-card completion proof. Baseline and candidate must each pass validation
independently before comparison.
The wrapper also owns a fail-closed process boundary. `-ProcessTimeoutMs` may
set an explicit bounded wall limit; otherwise the wrapper derives one from the
requested playback, settling, and capture windows. A hung app is terminated as
a full process tree, stdout/stderr are drained with bounded waits, and the
result records timeout exit `124` plus kill/drain evidence. Exact
`-TargetPresentedFrames` runs cannot use `-LegacyGuiSmokeOptions`, and the
target frame is included in timing accumulators and the timeline is pinned to
that actually presented frame before synchronous stop/final telemetry. This
prevents an already-advanced timeline from redrawing a later paused frame into
the screenshot. Missing screenshots and nonzero child exits are written into the
durable result before the wrapper returns failure.
For playback backend-selection evidence, pass `-GpuPlaybackReconBackend <name>`
to `run-release-playback-profile.ps1`, `run-release-gui-smoke.ps1`, or the
UltraMagnus P3 wrapper; the wrappers set `MLVAPP_GPU_PLAYBACK_RECON_BACKEND`.
GUI smoke also clears stale inherited backend values by default when the switch
is omitted.
`tools/profiling/compare-release-gui-smoke-ab.ps1` preserves those Auto fields
under `autoDecision` when comparing two smoke JSON files. Its stdout and JSON
include Auto reason changes plus average milliseconds and FPS-equivalent deltas,
so P4 reviews can compare sampler behavior without replaying the clip on the
local VM; UltraMagnus remains the proof source for GPU no-readback promotion.

## Telemetry key list (playback-profile JSON)

The headless `--profile-playback` mode emits a structured JSON document with
the following families of keys.

### Playback scale and per-stage resolution

- `render_thread_playback_scale_factor_request`
- `render_thread_playback_scale_factor_effective`
- `render_thread_playback_scale_factor_clamped`
- `render_thread_phase4b_path`
- `render_thread_phase4b_path_label`
- `render_thread_phase4b_y_crop_rows`
- `render_thread_phase4b_fallback_reason`
- `render_thread_preview_mode`
- `render_thread_aggressive_preview`
- `render_thread_playback_scale_sensor_pixels`
- `render_thread_playback_scale_output_pixels`
- `render_thread_playback_scale_pixel_retention_ratio`

Per-stage resolution telemetry uses the prefix
`render_thread_stage_<stage>_` with `domain`, `width`, `height`, `pixels`,
`pixel_retention_ratio`, and `preview_resolution` fields. Current stages are:

- `raw_decode` — raw Bayer decode/unpack resolution; currently full source
  resolution for x1/x2/x4/x8 when the stage runs.
- `bayer_reduction_input` and `bayer_reduction_output` — the early
  Bayer-domain preview-reduction contract for Phase 4B paths 2, 3, and 8.
- `llrawproc` — active LLRawProc/Dual ISO resolution. This is the field that
  proves whether a reduced preview still paid full reconstruction cost.
- `rgb_input` and `rgb_output` — reconstructed Bayer entering debayer or
  Bayer-to-RGB block averaging, and the RGB output from that combined stage.
- `processing` — processed RGB resolution for raw-processing and 16-to-8 work.
- `presentation_input` — rendered RGB dimensions before viewport presentation.

### Metadata

- `qt_opengl_environment`
- `qt_qpa_platform_environment`
- `gpu_preview_processing_backend_request`
- `gpu_preview_processing_environment_requested`
- `gpu_bilinear_debayer_backend_request`
- `gpu_bilinear_debayer_environment_requested`
- `gpu_amaze_debayer_backend_request`
- `gpu_amaze_debayer_environment_requested`
- `dual_iso_mode_selected`
- `dual_iso_mode_effective`
- `dual_iso_preview_runtime_active`
- `playback_preview_mode`
- `playback_aggressive_preview`

### One-time GPU bilinear debayer probe

- `gpu_bilinear_debayer_probe_available`
- `gpu_bilinear_debayer_probe_reason`
- `gpu_bilinear_debayer_probe_renderer`

### One-time GPU AMaZE debayer probe

- `gpu_amaze_debayer_probe_available`
- `gpu_amaze_debayer_probe_reason`
- `gpu_amaze_debayer_probe_renderer`

### Per-frame GPU activation

- `gpu16_preview_active`
- `gpu_preview_processing_active`
- `gpu_bilinear_debayer_active`
- optional `gpu_bilinear_debayer_renderer`
- optional `gpu_bilinear_debayer_fallback_reason`
- `gpu_amaze_debayer_active`
- optional `gpu_amaze_debayer_renderer`
- optional `gpu_amaze_debayer_fallback_reason`
- optional `gpu_amaze_debayer_upload_ms`
- optional `gpu_amaze_debayer_kernel_ms`
- optional `gpu_amaze_debayer_download_ms`
- optional `gpu_amaze_debayer_total_ms`

The `gpu_amaze_debayer_*_ms` fields are emitted only when the explicit CUDA
AMaZE backend actually renders the frame. They come from the DLL's backend
timing API and split the current CPU-visible path into host-to-device upload,
kernel work, device-to-host download, and backend total time; fallback frames do
not synthesize these fields.

### Per-frame outer CPU stages

- `raw_uint16_ms`
- `llrawproc_ms`
- `debayered_frame_ms`
- `processing_ms`
- `processed16_total_ms`
- `processed16_to_8bit_ms`
- `processed16_cache_store_ms`
- `processed8_total_ms`
- `processed8_cache_store_ms`

### Dual ISO preview internals

- `dual_iso_preview_histogram_ms`
- `dual_iso_preview_regression_ms`
- `dual_iso_preview_rowscale_ms`
- `dual_iso_preview_override_active`

### Exclusive debayer internals

- `raw_float_convert_ms`
- `debayer_exclusive_ms`
- `debayer_kernel_ms`
- `debayer_pipeline_other_ms`

### Processing internals

- `processing_core_ms`
- `processing_core_color_ms`
- `processing_highest_green_ms`

### Raw uint16 internals

- `raw_uint16_disk_read_ms`
- `raw_uint16_decompress_ms`
- `raw_uint16_decompress_prepare_ms`
- `raw_uint16_decompress_execute_ms`
- `raw_uint16_unpack_ms`
- `raw_uint16_copy_ms`
- `raw_uint16_other_ms`

### Decode-ahead prefetch (default-on)

- The raw-`uint16` decode-ahead worker is **default-on**; it hides
  foreground decode time on hosts with spare CPU headroom and produces
  `raw_uint16_prefetch_hit=true` on warm samples for compressed raw.
- Set `MLVAPP_DISABLE_RAW_UINT16_PREFETCH=1` (values `1`, `true`, `yes`,
  `on`) to disable the worker and fall back to foreground decode — use this
  if the host regresses on end-to-end playback latency.
- The older `MLVAPP_EXPERIMENTAL_RAW_UINT16_PREFETCH` env var is inert;
  setting it has no effect (removed from the enable gate).
- `raw_uint16_prefetch_decode_failures` surfaces the count of background
  decode failures per clip (mutex-protected read of the worker counter); it
  should be `0` on healthy runs, and remains `0` when the worker is
  disabled.

### LJ92 predictor profiling

LJ92 profiling fields now distinguish "requested" from "applicable":

- `raw_uint16_lj92_pred6_split_requested` — the profiling env asked for the
  pred6 split.
- `raw_uint16_lj92_predictor` — actual JPEG predictor used by the frame
  decode.
- `raw_uint16_lj92_pred6_split_active` — true only when the frame really
  decoded through predictor 6.

Generic predictor-path profiling fields:

- `raw_uint16_lj92_generic_split_requested`
- `raw_uint16_lj92_generic_split_active`
- `raw_uint16_lj92_generic_total_ms`
- `raw_uint16_lj92_generic_bitstream_ms`
- `raw_uint16_lj92_generic_predictor_ms`
- `raw_uint16_lj92_generic_other_ms`

Important interpretation note for the generic LJ92 split:

- It instruments inside the per-sample decode loop.
- That makes it useful for relative shape, but too intrusive for honest
  absolute decoder `ms`.
- Use a split-disabled run for sustained playback benchmarking.

### Play-start preroll metrics

- `play_start_preroll_active` — actual preroll request issued for this
  run.
- `play_start_preroll_eligible` — cached-playback mode made preroll
  possible for this run.
- `play_start_preroll_disabled_by_environment` — local profiling env
  disabled the preroll request for this run.
- `play_to_first_frame_measured` — whether a first requested render
  completed after the metric was armed.
- `play_to_first_frame_ms` — time from arming to the first requested frame
  reaching `drawFrameReady`.

Important interpretation note: `play_to_first_frame_ms` is an app-level
readiness metric. It is measured before guaranteed OS-window paint unless
`wait_for_paint` is enabled. Use it for A/B trend comparisons, not as a
literal "button to visible pixel" metric.

### Llrawproc ownership / contention

- `residuals.llrawproc_shared_lock_ms`
- `residuals.llrawproc_dualiso_refine_lock_ms`
- `residuals.llrawproc_publish_lock_ms`

The current llrawproc ownership work has reached a practical decision point
on the ordinary warm Dual ISO path: the direct perf JSON now includes
`llrawproc_dualiso_refine_lock_ms` and `llrawproc_publish_lock_ms`, and the
latest warm same-build reruns show those medians at `0.0 ms` on the
standard warm Dual ISO scenarios. Future CPU work should focus on
colder/contention seams rather than assuming more publish/refine slicing
will keep paying off.

## Interpretation notes

### Single-thread vs multithread on the local VM

Current local Dual ISO preview thread matrix:

- `--threads 4` is the clean fastest point measured (`111.84 ms` warm).
- `--threads 8` regresses materially on this VM (`158.73 ms` warm).
- The raw split shows the remaining bottleneck is compressed decode
  (`raw_uint16_decompress_ms`), not disk I/O or bit-unpack.

### Cold 8-bit measurement

- `full8` medians without `--cold-8bit` should be read as a warmed-cache
  metric.
- Use the cold mode when you want to profile the actual 16-bit-to-8-bit
  render path instead of the exact processed-frame cache.

### Dual ISO mode interpretation

Expected interpretation of `dual_iso_mode_*`:

- HQ receipt during playback: `selected=1`, `effective=2`,
  `runtime_active=true`, `override_active=true`.
- Explicit preview receipt during playback: `selected=2`, `effective=2`,
  `runtime_active=true`, `override_active=false`.

### Processed-frame cache layers

- Exact-current 16-bit cache for repeated same-frame requests.
- Small 16-bit revisit cache for nearby replays / short loops.
- Existing multi-slot 8-bit cache for preview output.

Current revisit-specific proof is functional, not benchmarked:

- `DualIsoPipeline.ProcessedFrame16CacheKeepsNearbyFramesWarm`.
- If you want a wall-clock number for this seam, use a revisit-aware harness
  rather than the normal forward-only `--profile-playback` path.

### AVX2 build-time opt-in

Local AVX2 builds are already supported for x86 hosts via:

- `platform/qt/avx_optin.pri`
- Set `MLVAPP_ENABLE_AVX2=1` before running `qmake`.
- Parity coverage for the helper build already exists in
  `tests/console/test_avx_golden.cpp`.

### Play-start cache preroll

- Clicking play now primes the existing raw cache window with a small
  non-blocking 2-frame lookahead.
- This only applies when playback is already using cached AMaZE debayer.
- It is a play-start UX hint, not a measured sustained-FPS optimization.

## Cross-references

- [`docs/10-build-windows.md`](10-build-windows.md) — Windows runtime path
  required for `MLVApp.exe --profile-playback`.
- [`docs/12-gpu-viewport-architecture.md`](12-gpu-viewport-architecture.md)
  — the experimental viewport behind the `--gpu-*` selectors.
- [`docs/13-testing-infrastructure.md`](13-testing-infrastructure.md) —
  `console_tests`/`pipeline_tests`/`gui_tests` and the GPU gating contracts
  these telemetry fields originate from.
- [`docs/15-test-fixtures.md`](15-test-fixtures.md) — the
  `tiny_dual_iso.mlv` and `large_dual_iso.mlv` fixtures the perf harness
  benchmarks.
- [`docs/playback-clip-length-rule.md`](playback-clip-length-rule.md) — the venue playback rule: >= 20 s
  of real footage, a window that never exceeds the clip, never a loop (typed refusals, exit 41/42/43).
- `tests/perf/baselines.json` — the local baselines file consumed by
  `--require-baseline` and `--update-baseline`.
- `tests/perf/run_runtime_profile.ps1` — the local profiling wrapper.
