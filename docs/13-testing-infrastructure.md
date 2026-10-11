# Testing Infrastructure

Migrated from `tests/README.md` (the harness, golden-hash, backend-parametric,
fuzz-overview, GPU-gating, and current-local-status sections). Performance
material has been split out into
[`docs/14-performance-benchmarking.md`](14-performance-benchmarking.md), and
fuzz-target detail lives in [`docs/16-fuzz-testing.md`](16-fuzz-testing.md).
Originally written by the maintainers; edits tracked via git history.

This document covers:

1. Test-suite layout (subdirectories under `tests/`).
2. Building and running `console_tests`, `pipeline_tests`, and `gui_tests`.
3. The CI invocation in `.github/workflows/tests.yml`.
4. The golden-hash contract (SHA256 manifests, tolerance policy).
5. Backend-parametric shells for debayer and preview-processing.
6. Fuzz-target overview (deeper detail in `docs/16-fuzz-testing.md`).
7. Experimental GPU-related gating (env vars, `--gpu-*` flags).
8. Current local status.

## Test-suite layout

The `tests/` tree contains the regression-safety scaffold for MLV App. Its
subdirectories are:

- `console/` — lightweight non-GUI regression tests.
- `alloc/` — allocation-tracking smoke tests and allocator scaffolding.
- `gui/` — optional Qt Test smoke coverage.
- `common/` — shared helpers.
- `fixtures/` — checked-in test assets and placeholders.
- `fuzz/` — optional fuzz harnesses.
- `perf/` — benchmark harness and baselines.
- `pipeline/` — direct in-process engine goldens against the tiny Dual ISO
  clip.

The initial goal is to make correctness checks runnable in CI without
changing the main app build or requiring large clip fixtures.

## Building and running

The Windows toolchain prerequisites and `qmake`/`mingw32-make` invocation are
documented in [`docs/10-build-windows.md`](10-build-windows.md). The
condensed pattern that mirrors `.github/workflows/tests.yml`:

```powershell
$env:PATH = "C:\Qt\6.10.2\mingw_64\bin;C:\Qt\Tools\mingw1310_64\bin;" + $env:PATH
New-Item -ItemType Directory -Force tests\build-ci-console | Out-Null
Push-Location tests\build-ci-console
& "C:\Qt\6.10.2\mingw_64\bin\qmake.exe" "..\console\console_tests.pro"
& "C:\Qt\Tools\mingw1310_64\bin\mingw32-make.exe" -j2
Pop-Location
```

Replace `console` with `pipeline`, `gui`, or `perf` to build the matching
suite. The `perf_tests` workflow specifically is documented separately in
[`docs/14-performance-benchmarking.md`](14-performance-benchmarking.md).

### `console_tests`

- `console_tests --hash-output <path>` writes the current seed artifact
  hashes.
- `console_tests --check-golden` compares against
  `tests/fixtures/golden/hashes.json`.
- `console_tests --check-golden <path>` compares against an explicit golden
  file.
- `console_tests` also includes a local AVX parity check that builds
  `tests/console/avx_parity_helper.pro` twice (default + `MLVAPP_ENABLE_AVX=1`)
  and asserts both builds render identical frame hashes on the checked-in
  Dual ISO fixtures when Qt/qmake + a make tool are available.
- `tests/console/diso_phase_harness.pro` is a standalone diagnostic (not run by
  any suite). Per frame of a dual-ISO clip it prints the camera frame number,
  the raw mod-4 row-group means and the phase they imply, the resolver's
  cached and resolved pattern with its probe flags, and a row-banding metric
  plus a hash of the processed frame. `--synthetic` drives the resolver with a
  phase-rotating 1920x804 scene at a chosen signal level.

### `pipeline_tests`

- Run the complete pipeline suite without `--check-golden`. It emits diagnostic
  artifact keys beyond the 15 frozen keys, so an unfiltered golden comparison
  reports unexpected keys even when every test passes.
- Run the seven golden producers separately with the exact filter below,
  matching the key-to-producer map in `.github/workflows/tests.yml`. This checks
  the unchanged `tests/fixtures/golden/pipeline_hashes.json`; it is not permission
  to regenerate that file or to ignore missing, mismatched, or unexpected keys.
- `CdngSequenceExport.cpp` and `BatchRunner.cpp` compile into the pipeline target
  with Qt Core, Gui and OpenGL. QtWidgets is excluded. Export prompt tests cover
  batch skip/abort policy and fail closed when a GUI prompt is unavailable;
  the GUI build retains the original dialogs.

```powershell
$pipelineExe = 'tests\build-ci-pipeline\release\pipeline_tests.exe'
& $pipelineExe
if ($LASTEXITCODE -ne 0) { throw 'Full pipeline suite failed' }
$goldenProducers = @(
    'BackendParametricDebayerShell.CpuRenderDebayerAmazeProducesStableHash'
    'BackendParametricDebayerShell.CpuRenderDebayerBilinearProducesStableHash'
    'DualIsoPipeline.TinyDualIsoFullFramesMatchGolden'
    'GpuPreviewProcessing.ExposureStopsChangesSubsetConfigAndStableOutput'
    'GpuPreviewProcessing.TinyDualIsoReceiptSubsetGoldenOutputIsStable'
    'DualIsoPipeline.TinyDualIsoPreviewFramesMatchGoldenAndStayCloseToFull'
    'BackendParametricShell.CpuRenderPreviewProcessingSubsetProducesStableHash'
) -join ':'
New-Item -ItemType Directory -Force .claude-state\validation | Out-Null
& $pipelineExe "--gtest_filter=$goldenProducers" --check-golden `
    --hash-output .claude-state\validation\pipeline-golden-actual.json
if ($LASTEXITCODE -ne 0) { throw 'Pipeline golden producer check failed' }
```

Use a fresh artifact path for each validation run and retain stdout, stderr,
exit code and executable identity. A filter containing only CDNG helper tests
does not execute the golden producers and must not be combined with
`--check-golden`. The direct-frame golden check also does not replace the required
known-good application comparison on real footage for an export refactor.

- `pipeline_tests` also pins current processing-reuse behavior: Dual ISO
  exact-frame reuse after runtime solve, chroma-smooth scratch reuse,
  median/RBF reuse stability, Sobel scratch parity, and no-reset cache
  invalidation.
- `pipeline_tests` also pins Dual ISO full20bit helper scratch reuse for the
  active autodetect, exposure-match, and AMaZE helper paths, including the
  persistent autodetect histogram scratch in
  [`src/mlv/llrawproc/dualiso.c`](../src/mlv/llrawproc/dualiso.c).
- `pipeline_tests` now also guards two ownership/reuse contracts in
  llrawproc: repeated renders with unchanged pixel maps stop recopies on the
  steady path, and the new dark-frame worker snapshot reuses its copied
  payload across frames.
- The full `pipeline_tests` suite also includes a forced-rerender
  llrawproc guard,
  `DualIsoPipeline.StablePixelMapsReuseWorkerCopiesAcrossForcedReprocess`,
  which invalidates processed preview caches between renders and verifies
  the worker pixel-map copies stay warm across the real rerender.

### `gui_tests`

- `gui_tests` defaults `QT_QPA_PLATFORM` to `offscreen` when the variable is
  unset, and on Windows it suppresses native crash dialogs before
  constructing `QApplication`; this keeps direct local launches from
  surfacing modal fail-fast popups in this workspace.
- Known issue (2026-06-15): local Windows `gui_tests.exe -functions` can hang
  before QtTest prints the function list. The hang reproduces on both the
  current GPU AMaZE playback branch and clean baseline `ae895390`, so it is
  tracked as pre-existing test-infra debt rather than a runtime regression.
  Fix direction: make the wrapper launch deterministic for the offscreen Qt
  platform/plugin tree and ensure timed-out GUI test children are killed before
  retrying deployment.
- `gui_tests` covers the current GPU presenter seam only:
  environment-gated install, CPU fallback visibility when the presenter is
  absent, fallback hide/show when a texture-backed presentation is active,
  and the new 16-bit RGB presenter handoff used by the experimental
  viewport.
- `gui_tests` also covers the extracted `MainWindow` GPU preview decision
  matrix: when the 16-bit presenter path is allowed, when scopes force the
  app back to the 8-bit GPU image path, when shader-side zebra processing
  is allowed, and how sampler/zebra presentation options are derived from
  UI state.
- `gui_tests` also covers pixel-exact presenter hashes for RGB888/RGB16
  uploads, scope image regression hashes (histogram/vector scope/waveform),
  and zebra-processing parity seams on both the 8-bit and 16-bit presenter
  paths.
- `gui_tests` now includes live `ScopesLabel::setScope()` regressions for
  raw histogram, waveform, parade, and vector scope dispatch.
  Histogram/vector scope remain exact widget-output goldens; waveform/parade
  use a coarsened widget signature to stay stable on the software-rendered
  Qt path in this workspace.
- The zebra parity check is intentionally skipped on the local `llvmpipe`
  software GL renderer, which does not produce stable shader-processed
  output in this workspace.
- There is intentionally no GPU compute parity coverage yet because current
  OpenGL support is presentation-only, not image processing.

### Playback-profile smoke coverage

- `console_tests` includes a real subprocess smoke test for
  `MLVApp.exe --profile-playback`.
- That test injects the local Qt runtime via `QLibraryInfo` and forces the
  `windows` platform plugin; do not force `QT_QPA_PLATFORM=offscreen` for
  this seam in this workspace. If Linux CI is added later, this seam will
  need either an `xvfb`-backed run or explicit offscreen-plugin deployment
  there.
- Manual release-tree playback profiles should use
  `tools\profiling\run-release-playback-profile.ps1`, which pins
  `QT_QPA_PLATFORM=windows` and `QT_QPA_PLATFORM_PLUGIN_PATH` to the deployed
  `platforms\qwindows.dll` directory.

## CI invocation (`tests.yml`)

`.github/workflows/tests.yml` exists as the initial CI scaffold for the test
tree. Properties:

- Windows-only for now.
- Provisions Qt 6.10.2 + MinGW 13.1 via `aqtinstall`.
- Runs `console_tests --check-golden`, the full pipeline suite, and the separate
  filtered pipeline golden producers described above.
- Runs `gui_tests` in the independent `windows-gui-pilot` job. The current
  promotion record and hosted evidence live in the CI-1 section of
  [`docs/roadmap.md`](roadmap.md); this guide intentionally does not duplicate
  that record.

## Golden-hash contract

Goldens are stable SHA256 hashes captured against the checked-in Dual ISO
fixtures and pinned receipts. Manifests live under `tests/fixtures/golden/`:

- `hashes.json` — `console_tests` seed artifact hashes.
- `pipeline_hashes.json` — direct in-process engine frame hashes (full
  pipeline plus the backend-parametric shells described below).
- `gui_hashes.json` — Qt-test pixel-exact presenter and scope hashes.
- `tiny_dual_iso_hq_dng_hashes.json` — DNG export sequence hashes from the
  clip-backed `--batch` golden test.

Tolerance policy:

- 16-bit pipeline outputs: **exact** SHA256 match across runs.
- GPU backends in the parametric shells: tolerance check (CPU vs GPU within
  a known epsilon), or a runtime skip on unsupported software-only GL
  hosts. See "Backend-parametric shells" below.
- 8-bit perf paths: not used as exact-hash gates because medians are too
  bursty across runs (perf coverage uses the relative speedup floor and the
  local baseline profile in `tests/perf/baselines.json`; see
  [`docs/14-performance-benchmarking.md`](14-performance-benchmarking.md)).

A failing golden indicates either a real engine drift or an intentional
behavior change; bumping the manifest is the explicit way to advance the
pinned baseline.

## Backend-parametric shells (debayer / preview-processing)

The pipeline tests carry two parametric shells that exercise the same input
under different backends.

### Preview-processing shell

The experimental GPU preview-processing subset has fixture-backed pipeline
goldens in `tests/fixtures/golden/pipeline_hashes.json`. It is still
display-side processing on already-debayered 16-bit RGB, but the subset is
no longer tested only for determinism.

The backend-parametric pipeline shell now has a real offscreen GPU execution
path for that same subset:

- CPU backend always runs and records
  `tiny_dual_iso.preview_processing.cpu.frame0`.
- GPU backend now probes runtime availability and either executes with a
  CPU-vs-GPU tolerance check or skips on known unsupported/software GL
  conditions such as `QOffscreenSurface` / `QOpenGLContext` setup failure or
  `llvmpipe`.
- The pipeline golden runner now treats expected `.gpu.` keys as optional
  when the GPU backend is skipped at runtime, so one flat manifest can stay
  valid across both GPU-capable and software-only hosts.

That subset now tracks `exposure_stops` through the copied precomputed LUTs;
the pipeline test records exact subset-output hashes plus config-signature
drift when exposure changes. There is still intentionally no claim of full
CPU/GPU image-processing parity for that subset; it is a stable drift
detector for the current display-side processing path, not a proof that the
subset matches the full CPU pipeline.

### Debayer shell

`pipeline_tests` includes a backend-parametric debayer shell:

- CPU bilinear frame-0 goldens under
  `tiny_dual_iso.debayer.bilinear.cpu.frame0`.
- CPU AMaZE frame-0 goldens under
  `tiny_dual_iso.debayer.amaze.cpu.frame0`.
- A GPU-debayer skip tripwire that stays green on this host until a real
  raw-CFA GPU backend exists.

This is a regression shell for the current CPU debayer boundary, not
evidence of a working GPU debayer backend yet. The future production GPU
debayer seam is still the raw-debayer boundary in
`src/mlv/video_mlv.c` / `src/mlv/frame_caching.c`, not the already-landed
post-debayer preview-processing shader path.

The latest current-tree focused verification run of the debayer shell via
`tests/build-debayer-shell/release/pipeline_tests.exe --check-golden`
passed with `42 tests / 485 assertions / 3 skips / 0 failures`.

#### GPU debayer update (2026-04-22)

- The debayer shell now uses the existing production-side
  `platform/qt/GpuDebayer.{h,cpp}` backend rather than a duplicate
  test-only bilinear implementation.
- Bilinear GPU debayer now behaves like the preview-processing shell:
  - If a supported OpenGL backend is present, the test compares CPU vs GPU
    within tolerance.
  - If the host is unsupported / software-only, the test skips with a
    known runtime reason.
- AMaZE GPU debayer is now covered by the same backend fixture and, on hosts
  with the CUDA AMaZE DLL, compares CPU vs GPU within the documented P-pre
  tolerance envelope. Unsupported hosts still skip with a known runtime reason.
- The latest current-tree focused verification run of the converged debayer
  shell via `tests/build-gpu-debayer/release/pipeline_tests.exe --check-golden`
  passed with `43 tests / 491 assertions / 4 skips / 0 failures`.
- No `.gpu.` debayer golden is checked in yet; on a hardware-backed local
  host, the first successful bilinear GPU run should intentionally force a
  manifest update by recording `tiny_dual_iso.debayer.bilinear.gpu.frame0`.

## Fuzz-target overview

The opt-in fuzz tree under `tests/fuzz/` builds three local file-fed fuzz
executables: `fuzz_receipt_loader`, `fuzz_lj92`, and `fuzz_mlv_open`. They
are intentionally not part of the default test tree and are not in CI; they
exist for local/nightly parser hardening rather than every quick regression
run.

Build and run details live in [`docs/16-fuzz-testing.md`](16-fuzz-testing.md).

## Experimental GPU-related gating

The experimental GPU presenter and preview-processing surface is exposed
through environment variables and headless playback-profile flags. The
canonical reference is
[`docs/12-gpu-viewport-architecture.md`](12-gpu-viewport-architecture.md);
the test-relevant surface is summarized here.

### GPU presenter and preview processing

- The environment-gated OpenGL viewport (`MLVAPP_EXPERIMENTAL_GL_VIEWPORT=1`)
  can now accept either 8-bit `QImage` frames or direct 16-bit RGB frame
  buffers.
- When the viewport is installed and zebras/scopes are off, the main
  preview path can hand the processed 16-bit frame directly to the
  presenter and skip the CPU-side 16->8 reduction for that display path.
- Zebra overlays now stay in the fragment shader on both the 8-bit image
  presenter path and the 16-bit presenter path; the CPU still computes the
  lightweight under/over flags needed by scopes.

### Headless `--profile-playback` selectors

Playback-profile mode now supports:

- `--gpu-preview-processing <auto|cpu|gpu>` alongside `--gpu-viewport` so
  the production preview-policy seam can be exercised without depending
  only on the environment variable.
- `--gpu-bilinear-debayer <auto|cpu|gpu>` for the existing experimental
  bilinear GPU debayer path.
- `--gpu-amaze-debayer <auto|cpu|gpu>` for the explicit Full Quality AMaZE GPU
  path. It is opt-in; `gpu` implies the GPU preview-processing seam and falls
  back to CPU AMaZE with telemetry when the CUDA backend is unavailable or an
  unsupported case such as CA correction is active.

`--profile-playback` JSON now reports the selector and the real per-frame
seam.

For preview processing:

- Metadata:
  - `gpu_preview_processing_backend_request`
  - `gpu_preview_processing_environment_requested`
- Per-frame:
  - `gpu16_preview_active`
  - `gpu_preview_processing_active`

For the bilinear debayer, "requested" vs "active" means:

- Requested:
  - `metadata.gpu_bilinear_debayer_backend_request`
  - `metadata.gpu_bilinear_debayer_environment_requested`
- Active:
  - per-frame `gpu_bilinear_debayer_active`
  - plus optional `gpu_bilinear_debayer_renderer` and
    `gpu_bilinear_debayer_fallback_reason`

Playback-profile output also records a one-time probe:

- `gpu_bilinear_debayer_probe_available`
- `gpu_bilinear_debayer_probe_reason`
- `gpu_bilinear_debayer_probe_renderer`

For the AMaZE debayer, "requested" vs "active" means:

- Requested:
  - `metadata.gpu_amaze_debayer_backend_request`
  - `metadata.gpu_amaze_debayer_environment_requested`
- Active:
  - per-frame `gpu_amaze_debayer_active`
  - plus optional `gpu_amaze_debayer_renderer` and
    `gpu_amaze_debayer_fallback_reason`

Playback-profile output also records a one-time AMaZE probe:

- `gpu_amaze_debayer_probe_available`
- `gpu_amaze_debayer_probe_reason`
- `gpu_amaze_debayer_probe_renderer`

Expected behavior on `llvmpipe` or another software renderer:

- The probe reports unavailable.
- The debayer path falls back to CPU.
- `gpu_bilinear_debayer_active` stays `false`.
- `gpu_amaze_debayer_active` stays `false` unless the CUDA AMaZE backend is
  available and the explicit AMaZE GPU selector is active.

### Console-test contracts for the selectors

`console_tests` includes explicit playback-profile selector contracts for:

- Default `auto` metadata.
- `--gpu-preview-processing cpu`.
- Invalid `--gpu-preview-processing` rejection.

Local suites covering the bilinear GPU debayer feature now include:

- App-backed `console_tests --check-golden`.
- `gui_tests`.
- `pipeline_tests --check-golden` debayer shell / tripwire coverage.

Hardware-backed local validation is still required for:

- Real bilinear GPU parity confirmation.
- The first `.gpu.` debayer golden capture.
- Meaningful GPU debayer performance numbers.

### Windows GL backend selection

On Windows local GPU/profile entrypoints now prefer `QT_OPENGL=desktop` when
the caller has not already set `QT_OPENGL`:

- `MLVApp --profile-playback`
- App-backed `console_tests`
- `pipeline_tests`
- `gui_tests`

Playback-profile metadata now records:

- `qt_opengl_environment`
- `qt_qpa_platform_environment`

So host/VM runs can prove which Qt GL backend choice was actually exercised.

### Dual ISO playback selector and stage telemetry

Playback-profile metadata now also records the Dual ISO playback/runtime
split:

- `dual_iso_mode_selected`
- `dual_iso_mode_effective`
- `dual_iso_preview_runtime_active`

Playback-profile samples also expose CPU stage telemetry for local
diagnosis. The full key list is documented in
[`docs/14-performance-benchmarking.md`](14-performance-benchmarking.md).

## Current local status

- `console_tests --check-golden`: pass.
- `alloc_tests`: pass.
- `pipeline_tests --check-golden`: pass.
- `gui_tests`: pass, with the zebra parity seam skipped on `llvmpipe`.
- `perf_tests --iterations 10 --require-baseline`: pass when run in
  isolation.
- Cache-enabled profiling runs are intentionally measurement-first; they
  may preserve artifacts even when preview-speedup assertions fail.
- `perf_tests --iterations 10 --cold-8bit --require-baseline`: pass.
- `fuzz_receipt_loader tests/fixtures/receipts`: pass.
- `fuzz_lj92 tests/fixtures/clips/tiny_dual_iso.mlv`: pass.
- `fuzz_mlv_open tests/fixtures/clips/tiny_dual_iso.mlv`: pass.

The latest current-tree focused verification run of `pipeline_tests
--check-golden` passed with `33 tests / 432 assertions / 0 failures`.

GPU support is still limited to display-side processing; debayer, llrawproc,
and the main color-processing pipeline remain CPU work today.

## Cross-references

- [`docs/10-build-windows.md`](10-build-windows.md) — Windows build
  instructions, runtime rules, and `windeployqt` recovery.
- [`docs/11-build-macos-linux.md`](11-build-macos-linux.md) — macOS and
  Linux build instructions.
- [`docs/12-gpu-viewport-architecture.md`](12-gpu-viewport-architecture.md)
  — the experimental OpenGL viewport that the GPU gating exercises.
- [`docs/14-performance-benchmarking.md`](14-performance-benchmarking.md) —
  `perf_tests`, the runtime-profile wrapper, and the headless
  `--profile-playback` mode (including the full telemetry-key list).
- [`docs/15-test-fixtures.md`](15-test-fixtures.md) — committed Dual ISO
  clips, receipts, and golden manifests.
- [`docs/16-fuzz-testing.md`](16-fuzz-testing.md) — fuzz-target build and
  run details.
- [`.github/workflows/tests.yml`](../.github/workflows/tests.yml) — the CI
  workflow that mirrors this guide.

## GUI CI policy

The independent `windows-gui-pilot` job and its current blocking policy are
defined by `.github/workflows/tests.yml`. The promotion record, hosted run
identifiers, and deliberate coverage scope are maintained only in the CI-1
section of [`docs/roadmap.md`](roadmap.md); this guide is a pointer rather than
a second source of truth.
