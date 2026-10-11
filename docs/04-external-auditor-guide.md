# 04 — External Auditor Guide

## 1. Purpose of this guide

This document is written for a **stranger auditor** — a security reviewer,
due-diligence engineer, downstream packager, or independent technical reviewer
who has **never seen this repository before** and may be acting under a
compliance, supply-chain, acquisition, or distribution engagement. The guide
**assumes zero prior context**. A reader should be able to (a) understand what
MLV App is, (b) verify our claims independently, (c) reproduce builds and
tests, and (d) form a defensible risk assessment without opening any document
except the ones explicitly linked from here.

Provenance: MLV App's canonical upstream is
<https://github.com/ilia3101/MLV-App>. The working tree this guide is written
against is a feature branch — `codex/festive-boyd-integration` — forked off
`master`. Where a claim is branch-specific rather than upstream-stable we call
that out explicitly. Unless stated otherwise, every command in this document
is intended to be run from the repository root.

## 2. What MLV App is (under 300 words)

MLV App is a cross-platform Qt 5 / Qt 6 desktop application for ingesting,
colour-grading, and transcoding **Magic Lantern MLV** raw video files — the
native container emitted by the third-party Magic Lantern firmware hack for
Canon DSLRs. The tagline from [README.md](../README.md) describes it as
"Lightroom, but for Magic Lantern MLV video". It reads `.mlv` containers
(including spanned `.m00`/`.m01`/... segment files), decompresses lossless
LJ92-encoded frames, applies raw-domain corrections (focus-pixel and
bad-pixel remap, vertical-stripe removal, Dual ISO reconstruction, chroma
smoothing, pattern-noise removal, dark-frame subtraction), demosaics (None,
Bilinear, AMaZE, AHD, plus algorithms wrapped from the vendored
`librtprocess` library: LMMSE, DCB, RCD, IGV, Markesteijn), then runs a
9-stage colour-processing pipeline (white balance, exposure, tone curves,
denoise, sharpen, chromatic-aberration correction, gamma/tonemap/gamut).
Output is either displayed in the GUI or exported — either to a Cinema DNG
sequence (uncompressed or lossless) via the in-tree DNG writer, or to a
video file via bundled FFmpeg binaries (ProRes 422/4444, H.264, H.265, VP9,
CineForm, DNxHD/DNxHR, JPEG2000, HuffYUV, PNG, TIFF, and more).

**Target users**: independent filmmakers, colourists, hobbyists,
hacker-camera users, and downstream packagers on Debian / Arch / NixOS.

**License**: **GPL-3.0-or-later** — see [LICENSE](../LICENSE).

**Maturity**: tagged releases on GitHub; current version **1.15.0.0**
([platform/qt/MLVApp.pro:450-453](../platform/qt/MLVApp.pro)); active
development (public issue tracker at
<https://github.com/ilia3101/MLV-App/issues>); packaged for Debian, Arch,
NixOS; download site <https://mlv.app>.

## 3. How to confirm what you are looking at

Run these commands from the repository root **before** building anything.
They let an auditor ground the identity and shape of the tree without
trusting any text file in the repo.

```bash
# What commits are we sitting on right now?
git log --oneline -20

# Are there any uncommitted modifications relative to those commits?
git status

# What branch? (expect codex/festive-boyd-integration on this working copy)
git rev-parse --abbrev-ref HEAD

# Roughly how big is the engine?
wc -l src/mlv/*.{c,h} src/processing/*.{c,h} src/debayer/*.{c,h}

# Enumerate every LICENSE-named file. Expect at least:
#   ./LICENSE
#   ./src/librtprocess/LICENSE.txt
#   ./src/processing/rbfilter/LICENSE
#   ./src/processing/sobel/LICENSE
#   ./platform/qt/avir/LICENSE
#   ./platform/qt/maddy/LICENSE
find . -name "LICENSE*" -not -path "./.git/*"

# Confirm the Qt modules, compile flags, and version this tree builds as.
# In the output, expect:
#   - "QT       += core gui multimedia opengl"          near line 7
#   - "DEFINES += STDOUT_SILENT"                        near line 40
#   - "VERSION_MAJOR = 1"                               near line 450
#   - "VERSION_MINOR = 15"
#   - "VERSION_PATCH = 0"
#   - "VERSION_BUILD = 0"
#   - "VERSION = $${VERSION_MAJOR}.$${VERSION_MINOR}..." near line 460
head -60 platform/qt/MLVApp.pro
grep -n "^VERSION" platform/qt/MLVApp.pro
```

Expected shape, not exact match. If the `VERSION` line is missing, or if the
file no longer begins with the shown Qt module list, you are not looking at
a sanctioned MLV App 1.15.x tree.

Cross-check against the `docs/00-overview.md` "Status at a glance" table: if
anything there claims behaviour that contradicts the code you see, treat the
code as authoritative and raise a finding.

## 4. Licensing and third-party code

MLV App itself is **GPL-3.0** ([LICENSE:1](../LICENSE) — "GNU GENERAL PUBLIC
LICENSE / Version 3, 29 June 2007"). Because one of its vendored
dependencies (`librtprocess`) is also GPL-3.0, **the binary as shipped is a
GPL-3.0 work**. Every downstream packager inherits this obligation: the
redistribution package must preserve GPL-3.0 terms, carry the full license
text, and make corresponding source available.

| Vendored tree | Path | License | Purpose | Verify |
|---|---|---|---|---|
| librtprocess | [src/librtprocess/](../src/librtprocess/) | **GPL-3.0** | High-quality demosaic (LMMSE, DCB, RCD, IGV, Markesteijn, AMaZE), CA correction, highlight recovery | `head -5 src/librtprocess/LICENSE.txt` |
| liblj92 | [src/mlv/liblj92/](../src/mlv/liblj92/) | MIT (header in `lj92.h`) | Lossless JPEG decode (Pred1 fast path, Pred6, generic) | `head -15 src/mlv/liblj92/lj92.h` |
| genann | [src/processing/filter/genann/](../src/processing/filter/genann/) | zlib (inline notice) | Neural-net film-emulation filters | `head -30 src/processing/filter/genann/genann.h` |
| tinyexpr | [src/processing/tinyexpr/](../src/processing/tinyexpr/) | zlib (inline notice) | Expression parser for user-defined curves | `head -25 src/processing/tinyexpr/tinyexpr.h` |
| cJSON | [src/mlv/mcraw/cJSON.h](../src/mlv/mcraw/cJSON.h) | MIT (inline notice) | MCRAW metadata JSON | `head -10 src/mlv/mcraw/cJSON.h` |
| rbfilter | [src/processing/rbfilter/](../src/processing/rbfilter/) | MIT (file `LICENSE`) | Recursive bilateral filter | `head -5 src/processing/rbfilter/LICENSE` |
| sobel | [src/processing/sobel/](../src/processing/sobel/) | **Apache-2.0** (file `LICENSE`) | Sobel edge detector | `head -5 src/processing/sobel/LICENSE` |
| AVIR | [platform/qt/avir/](../platform/qt/avir/) | MIT (file `LICENSE`) | High-quality image resize | `head -5 platform/qt/avir/LICENSE` |
| maddy | [platform/qt/maddy/](../platform/qt/maddy/) | MIT (file `LICENSE`) | Markdown renderer for in-app docs viewer | `head -5 platform/qt/maddy/LICENSE` |
| camid | [src/mlv/camid/](../src/mlv/camid/) | follows in-tree (GPL-3.0) | Canon camera ID tables | read header notices |

**Bundled pre-built binaries** (treat as independent upstream software):

| Blob | Path | Typical license | Auditor obligation |
|---|---|---|---|
| FFmpeg (Win32 / Win64 / macOS / Linux) | [platform/qt/FFmpeg/](../platform/qt/FFmpeg/) (`ffmpegWin32.zip`, `ffmpegWin64.zip`, `ffmpegOSX.zip`, `ffmpegLinux.tar.xz`) | **LGPL** or **GPL** depending on how upstream was compiled | Unzip, run `ffmpeg -version`, confirm the configure-string matches the LGPL-compatible build upstream publishes; check NOTICE against upstream FFmpeg release |
| raw2mlv (Win32/Win64/macOS/macOS-Arm/Linux) | [platform/qt/raw2mlv/](../platform/qt/raw2mlv/) | inherits upstream `raw2mlv` | Pull source from upstream, compare binary hashes |

These binary blobs are **not** linked into the MLV App process — they are
invoked as child processes via stdio pipes (see §7). Nonetheless a reviewer
assessing supply-chain risk should verify each blob against its upstream
release by checksum before accepting the package.

Additional third-party **runtime** dependencies (not vendored, linked or
loaded at runtime) are listed in §14.

## 5. Repository structure, bird's-eye

```
MLV-App/
  README.md                    project overview + feature list + compile steps
  AGENTS.md                    workspace-policy (agent workflow; NOT product)
  LICENSE                      GPL-3.0
  docs/                        canonical documentation (you are in /04)
    00-overview.md             reading-order index
    01-user-guide.md           user-facing guide
    02-developer-guide.md      contributor + packager guide
    03-technical-specification.md  architecture, APIs, threading
    04-external-auditor-guide.md   ← this document
    10-build-windows.md        supporting: Windows build specifics
    11-build-macos-linux.md    supporting: macOS and Linux build specifics
    12-gpu-viewport-architecture.md  experimental GPU presenter
    13-testing-infrastructure.md     test-suite structure
    14-performance-benchmarking.md   perf harness + baselines
    15-test-fixtures.md        fixture clips, receipts, goldens
    16-fuzz-testing.md         fuzz targets
    diagrams/                  ASCII + Mermaid diagrams
  src/                         CORE ENGINE (C with small C++ in batch/)
    mlv/                         MLV I/O, frame index, prefetch cache, LJ92, MCRAW
      liblj92/                   vendored LJ92 codec (MIT)
      llrawproc/                 raw-domain corrections (focus pixels, bad pixels, dual ISO, ...)
      mcraw/                     MCRAW container + cJSON
      camid/                     camera ID tables
    processing/                  post-demosaic colour pipeline (9 stages)
      cube_lut.{c,h}             1D/3D .cube LUT parser and applier
      denoiser/                  2D median + RBF denoisers
      rbfilter/                  vendored recursive bilateral filter (MIT)
      cafilter/                  chromatic-aberration filter
      filter/genann/             vendored neural-net film filter (zlib)
      tinyexpr/                  vendored expression parser (zlib)
      sobel/                     vendored Sobel (Apache-2.0)
      interpolation/             spline interpolation for curves
    debayer/                     demosaic dispatcher
    librtprocess/                vendored librtprocess (GPL-3.0)
    dng/                         Cinema DNG TIFF writer + bit-packing + LJ92 encode
    batch/                       headless CLI batch CDNG export (C++)
                                   ReceiptLoader.{h,cpp}   standalone .marxml parser
                                   ReceiptApplier.{h,cpp}  applies parsed settings
                                   BatchRunner.{h,cpp}     drives --batch
                                   MlvTrim.{h,cpp}         drives --trim-mlv
    matrix/                      3x3 linear algebra
    ca_correct/                  chromatic-aberration helpers
    debug/                       stage-timing thread-local telemetry
    icon/                        application icon source
    mlv_include.h                umbrella header
  platform/
    qt/                          Qt 5/6 GUI (PRIMARY)
      MLVApp.pro                 qmake project file (target of build step)
      main.cpp                   entrypoint; pre-QApplication CLI flag parsing
      MainWindow.cpp             top-level UI orchestration
      RenderFrameThread.{h,cpp}  off-main-thread frame production
      GpuDisplayViewport.{h,cpp} experimental GL presenter
      GpuPreviewProcessing.*     experimental GPU preview-processing
      GpuDebayer.*               experimental GPU bilinear debayer
      CrashForensics.{h,cpp}     rotating-log sink + Windows minidump handler
      Updater/                   in-app update checker
      FocusPixelMapManager.*     focus-pixel table handling
      FpmInstaller.h             focus-pixel map installation (header-only; static inline methods)
      DownloadManager.{h,cpp}    HTTP downloads (updates, FPM)
      FFmpeg/                    pre-built FFmpeg binary bundles
      raw2mlv/                   pre-built raw2mlv binary bundles
      avir/                      vendored AVIR (MIT)
      maddy/                     vendored maddy (MIT)
      avx_optin.pri              AVX2 opt-in (MLVAPP_ENABLE_AVX2=1)
    cocoa/                       legacy Cocoa/Obj-C app (DEPRECATED)
    mlv_blender/                 optional Blender integration
    binning_test/                downscaling experiment (not shipped)
  tests/                         regression, pipeline, perf, fuzz, GUI smoke
    console/                     command-line regression tests
    pipeline/                    direct in-process engine goldens
    gui/                         Qt Test widget smoke coverage
    perf/                        benchmark harness + baselines
    fuzz/                        opt-in fuzz targets (not in CI)
    alloc/                       allocator smoke tests
    common/                      shared test helpers, minitest.h harness
    fixtures/                    checked-in test clips, receipts, goldens
    tests.pro                    master qmake SUBDIRS
  .github/workflows/
    tests.yml                    CI: console + pipeline + (non-blocking) gui
    Windows.yml                  release: Win64 zip
    macOS-Intel.yml              release: .dmg (Intel)
    macOS-Arm64.yml              release: .dmg (Apple Silicon)
    Linux.yml                    release: .AppImage
  osx_installer/                 create-dmg wrapper script
  pixel_maps/                    focus-pixel and bad-pixel tables shipped with app
  receipts/                      sample .marxml receipt presets
  .claude/                       AGENT WORKSPACE — NOT PART OF SHIPPED PRODUCT  ⚠
  .claude-state/                 AGENT SCRATCH — NOT PART OF SHIPPED PRODUCT    ⚠
```

**Sensitive / non-product directories** — `.claude/` and `.claude-state/`
are **agent workspace state**. They are curated investigation notes and
ephemeral scratch used by the LLM-assisted development workflow described in
[AGENTS.md](../AGENTS.md). They are **not part of the compiled product**,
**not distributed in release tarballs**, and **should not be considered when
assessing the shipped binary**. An auditor performing a distribution review
should independently confirm that their packaging pipeline excludes these
directories (see §13, "known risks").

## 6. Architecture summary

This is a condensed version of [docs/03-technical-specification.md](03-technical-specification.md)
§§2–7. The full reference is the code; this summary is load-bearing only
for orientation. **If this prose, the diagrams in `docs/diagrams/`, and the
code disagree about pipeline ordering, threading model, or struct layout,
the code wins** — the same instruction is given in §3 and applies to every
narrative section of every doc in `docs/`.

MLV App has three layers:

1. **`src/`** — the portable C engine. No Qt; links only the C standard
   library plus OpenMP. This is what performs all I/O, decompression,
   corrections, demosaic, and image processing.
2. **`platform/qt/`** — the Qt 5/6 GUI that wraps the engine plus
   cross-platform plumbing: menus, viewports, export dialogs, auto-update,
   crash forensics, FCPXML import, scripting, GPU presenter.
3. **`tests/`** — a qmake `SUBDIRS` tree aggregating console-level
   regression, pipeline-level goldens, GUI smoke tests, perf benchmarks, and
   opt-in fuzz targets (see §9).

### Frame pipeline (disk → RGB)

```
  +-------------------------------------------------------------+
  |  getMlvProcessedFrame8/16(mlvObject_t*, frameIdx, out, n)   |
  |  — primary playback entry (src/mlv/video_mlv.c)             |
  +-------------------------------------------------------------+
                                |
             (1) Frame lookup in video_index[] → frame_index_t
                                |
             (2) Raw-uint16 prefetch-slot check (4 slots)
                   hit  → reuse decoded uint16 directly
                   miss → read VIDF block → lj92_decode() if compressed
                                |
             (3) applyLLRawProcObject  (src/mlv/llrawproc/llrawproc.c)
                   - dark-frame subtraction
                   - focus-pixel remap (per-camera maps)
                   - bad-pixel remap (user + auto)
                   - vertical-stripe correction
                   - Dual ISO reconstruction (20-bit full OR preview)
                   - pattern-noise removal
                   - chroma smoothing (2x2 / 3x3 / 5x5)
                                |
             (4) debayerEasy     (src/debayer/debayer.c)
                   - None / Bilinear / AMaZE / AHD / DCB / RCD / IGV / LMMSE
                   - per-strip worker threads
                                |
             (5) applyProcessingObject  (src/processing/raw_processing.c)
                   Stage 1: LUT setup (65 536-entry gradation curves)
                   Stage 2: Shadows/highlights prep (blur image)
                   Stage 3: Highest-green highlight recovery
                   Stage 4: Core — levels → WB/colour matrix → curves → output matrix
                   Stage 5: 2D median denoise
                   Stage 6: RBF edge-aware blur / clarity / sharpen
                   Stage 7: Chromatic-aberration correction
                   Stage 8: Gamma / tonemap / gamut / transfer function
                   Stage 9: (optional AVX2) direct 8-bit fast path
                                |
             (6) Output — uint8 QImage for display, uint16 for DNG/FFmpeg
```

Mermaid versions of this and the threading model are kept in
[docs/diagrams/](diagrams/) (frame-pipeline, threading-model, build-and-ci).

### Threading model (quick reference)

- **Prefetch worker**: one pthread per `mlvObject_t`, owns 4 decode-ahead
  slots (`MLV_RAW_UINT16_PREFETCH_SLOTS`), gated off by
  `MLVAPP_DISABLE_RAW_UINT16_PREFETCH=1`. Default-on as of commit `00091b62`.
- **LLRAWPROC workers**: pooled workers, thread-local pixelmaps and
  dark-frame copies to minimize lock contention.
- **Processing**: OpenMP `#pragma omp parallel for` (libgomp on Windows/Linux,
  libomp via Homebrew LLVM on macOS); pthread fallback if compiled without
  OpenMP. Demosaic uses a vertical-strip split.
- **Rendering**: `RenderFrameThread` (Qt thread-pool worker) calls into the
  engine and emits `drawFrameReady(ReadyFrame)` back to `MainWindow`.
- **GUI thread**: consumes a `PresentationContext` delivered alongside each
  `ReadyFrame` — see commit `244c03a1` and §13 for the current contract.

## 7. Security-relevant surfaces (for a reviewer)

An adversary cannot realistically become a peer on an editor-and-transcoder's
network, but they **can** control the input files and, if they compromise a
DNS/HTTP path, the update check. The surfaces below enumerate the places we
expect a reviewer to look.

### 7.1 Untrusted file parsers

Attacker-controlled byte streams reach:

- **MLV container parser** — [src/mlv/video_mlv.c](../src/mlv/video_mlv.c);
  sibling [src/mlv/mlv.h](../src/mlv/mlv.h) declares the on-disk block
  types (`MLVI`, `VIDF`, `AUDF`, `RAWI`, `IDNT`, `VERS`, etc.).
- **LJ92 lossless-JPEG decoder** — [src/mlv/liblj92/lj92.c](../src/mlv/liblj92/lj92.c)
  (vendored MIT). Reached from the MLV parser on any compressed clip.
- **MCRAW container** — [src/mlv/mcraw/mcraw.c](../src/mlv/mcraw/mcraw.c)
  and cJSON metadata in [src/mlv/mcraw/cJSON.c](../src/mlv/mcraw/cJSON.c).
- **Receipt XML** — `.marxml` files describe processing parameters and are
  shareable between users. **Two parser entry points exist** because the GUI
  and the headless batch path were extracted separately:
  - **GUI path** — `MainWindow::readXmlElementsFromFile()` at
    [platform/qt/MainWindow.cpp:5660](../platform/qt/MainWindow.cpp). Reached
    via `on_actionImportReceipt_triggered`, copy/paste of receipt XML, and
    session-load (`SESSION_LAST_CLIP`). Sibling literal `*.marxml` extension
    filters appear at `MainWindow.cpp:5571`, `5629`, `5633`, and `12974`.
  - **Batch path** — `ReceiptLoader::loadFromFile()` in
    [src/batch/ReceiptLoader.cpp](../src/batch/ReceiptLoader.cpp) (731
    lines) with header [src/batch/ReceiptLoader.h](../src/batch/ReceiptLoader.h)
    (42 lines). Reached from `BatchRunner` via `--batch` invocations
    (`MainWindow.cpp:1997` calls `ReceiptLoader::loadFromFile`). The header
    comment explicitly notes it was *"extracted from
    `MainWindow::readXmlElementsFromFile()` to avoid MainWindow dependency"*
    so the parsing logic is duplicated by design and must be hardened in both
    places.
- **LUT / `.cube`** — [src/processing/cube_lut.c](../src/processing/cube_lut.c)
  parses user-supplied text `.cube` LUT files.
- **Focus-pixel / bad-pixel tables** — [pixel_maps/](../pixel_maps/) files
  are simple text tables parsed by `BadPixelFileHandler` /
  `FocusPixelMapManager`.

Fuzz targets exist but are **not** wired to CI:

- `fuzz_mlv_open` — [tests/fuzz/fuzz_mlv_open.cpp](../tests/fuzz/fuzz_mlv_open.cpp)
- `fuzz_lj92` — [tests/fuzz/fuzz_lj92.cpp](../tests/fuzz/fuzz_lj92.cpp)
- `fuzz_receipt_loader` — [tests/fuzz/fuzz_receipt_loader.cpp](../tests/fuzz/fuzz_receipt_loader.cpp)

Build per-target via the `.pro` files in the same directory and run against
any file or directory of files; see [tests/fuzz/README.md](../tests/fuzz/README.md)
and [docs/16-fuzz-testing.md](16-fuzz-testing.md). Expected reviewer action:
build, seed with the checked-in fixtures
([tests/fixtures/clips/](../tests/fixtures/clips/)), run for a meaningful
wall-clock budget, and inspect crash/timeout outputs.

### 7.2 Network surface

- **Auto-update checker** — [platform/qt/Updater/](../platform/qt/Updater/)
  (`Updater.*`, `updaterUI/cupdaterdialog.*`) fetches release-manifest
  metadata and optionally the installer over HTTP(S). Reviewer should
  confirm the exact host, protocol (TLS expected), and trust model. Qt's
  `QNetworkAccessManager` / `QSslSocket` is used; no custom TLS stack is
  shipped.
- **Focus-pixel map downloads** — [platform/qt/DownloadManager.{h,cpp}](../platform/qt/DownloadManager.h)
  fetches per-camera focus-pixel tables on demand. After download, the
  installer copies the validated `.fpm` file into the app directory via the
  **header-only** [platform/qt/FpmInstaller.h](../platform/qt/FpmInstaller.h)
  (62 lines, no `.cpp` — both `installFpm` overloads are `static` inline
  methods on the `FpmInstaller` class so the compilation unit is the
  including translation unit, not a separate `.cpp`). Auditors looking for
  `FpmInstaller.cpp` will not find one; the implementation is the header
  itself.

Both paths are **opt-in** (user clicks "Check for updates" or "Install focus
pixel maps") and the payloads flow into `pixel_maps/`-shaped text files
(FPM) or platform-native installers (updater) — not into the main decode
path at run time.

### 7.3 Process boundary — FFmpeg

FFmpeg is **not dynamically linked** into MLV App. The Qt layer spawns
FFmpeg as a child process via `QProcess`. The actual invocation sites are
**inline in MainWindow.cpp**, not in a dedicated wrapper class:

- `QProcess *ffmpegProc = new QProcess(this);` at
  [platform/qt/MainWindow.cpp:5234](../platform/qt/MainWindow.cpp), with
  the audio-merge command construction immediately following at line 5237
  (`ffmpegProc->execute(ffmpegAudioCommand, ffmpegAudioCommandArguments)`).
- Video-encode command strings are built at `MainWindow.cpp:3937`
  (`ffmpegCommand = program;`) and `MainWindow.cpp:4490`/`4496` (the
  multi-pass `libx264` and `boxblur`/`blend` filter strings) and shipped to
  FFmpeg via stdin pipes.

A separate file [platform/qt/ffmpegWrapper.h](../platform/qt/ffmpegWrapper.h)
exists in the tree but is **misleadingly named for an auditor** — it is a
17-line `extern "C"` shim that wraps `<libavcodec/avcodec.h>` with a
hard-coded `/usr/local/include/...` path. It does **not** drive a
`QProcess` and is **not** the FFmpeg child-process boundary. (The
hard-coded include path is itself a portability/hygiene smell worth noting
during a code-review pass, though it is not a security finding.)

Consequences for review:

- A vulnerability in FFmpeg does **not** give code execution in the MLV App
  process space.
- FFmpeg binaries are shipped pre-built inside the zip/tar.xz archives in
  `platform/qt/FFmpeg/` and unpacked at build time.
- Version pinning is by upstream archive. Auditors should confirm the
  upstream archive's configure string and version matches their
  tolerance before packaging.

On Windows, MLV App additionally links `dbghelp` for minidump production.
No other dynamically-loaded DLLs are used beyond Qt's own deployment.

### 7.4 Crash minidumps and log files

On Windows, [platform/qt/CrashForensics.h](../platform/qt/CrashForensics.h)
installs a `SetUnhandledExceptionFilter` handler that calls
`MiniDumpWriteDump`. Dumps are written under `<AppDataLocation>/logs/` as
`mlvapp-YYYYMMDD-HHMMSS.dmp`, and a rotating plain-text log
(`mlvapp-YYYYMMDD.log`) is also maintained. **Minidumps may contain process
memory** (stack, heap, module list, thread state) and therefore may contain
fragments of any open file. An auditor should verify via reading the file
on disk:

- Dumps are written **locally** to the user's `AppDataLocation` only —
  there is no upload or telemetry endpoint in this code path.
- Dumps are not encrypted on disk.
- The rotating-log retention policy (5 most recent date-stamped files) is
  enforced at startup.

Read [platform/qt/CrashForensics.cpp](../platform/qt/CrashForensics.cpp) end
to end to confirm; the header comment block at `CrashForensics.h:1-20`
summarises the policy.

### 7.5 Shell-script execution (macOS)

macOS builds include a "Post Export Scripting" feature (credited
`@dannephoto` in [README.md:72](../README.md)) that invokes a user-supplied
shell script after export. This is code execution by design, under user
consent, but reviewers should note it: a malicious receipt-sharing workflow
that also embeds a post-export script path could chain into arbitrary
execution. The user-facing description lives in
[docs/01-user-guide.md §11.9](01-user-guide.md#119-post-export-scripting-macos);
the implementation lives in
[platform/qt/Scripting.h](../platform/qt/Scripting.h) and
[platform/qt/Scripting.cpp](../platform/qt/Scripting.cpp).

### 7.6 Experimental GPU paths

All GPU paths are **environment-gated and off by default**:

- `MLVAPP_EXPERIMENTAL_GL_VIEWPORT=1` — GPU display viewport
  (`GpuDisplayViewport`).
- `MLVAPP_EXPERIMENTAL_GPU_PROCESSING=1` — GPU preview processing
  (`GpuPreviewProcessing`).
- `MLVAPP_EXPERIMENTAL_GPU_DEBAYER=1` — GPU bilinear debayer (`GpuDebayer`).
- `MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER=1` — explicit GPU AMaZE debayer
  routing (`GpuDebayer` + `igpu_amaze_debayer_cuda.dll`); in auto/env mode it
  is used with `MLVAPP_EXPERIMENTAL_GPU_PROCESSING=1`.
- `MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT=1` — explicit AMaZE
  CUDA-to-GL texture presentation request nested under GPU preview processing
  and GPU AMaZE. It remains non-default, requires an AMaZE backend preflight
  before becoming a present candidate, and must preserve CPU fallback paths.

The production default is the CPU path in every case. A reviewer looking at
the shipped binary behaviour should confirm these env vars are unset when
evaluating "typical use".

For the AMaZE texture-present path, reviewers should use the GUI smoke wrapper
selector flags instead of relying on persisted GUI state. A proof run should
force AMaZE, subset playback processing, GPU preview processing, and GPU AMaZE
selection, then compare a CPU-readback run against a texture-present run on the
same RTX 4090 machine:

```powershell
pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
  -File tools\profiling\run-release-gui-smoke.ps1 -RepoRoot . `
  -Input C:\temp\MLV\M16-1327.MLV `
  -Output .claude-state\profiling\gpu-amaze-present-proof\texture-present.json `
  -CaptureScreenshot -FrameTelemetry -PlaybackDebayer amaze `
  -PlaybackProcessing subset -GpuPreviewProcessing gpu -GpuAmazeDebayer gpu `
  -GpuAmazeTexturePresent `
  -StageLog .claude-state\profiling\gpu-amaze-present-proof\texture-present-stage.log `
  -ExtraEnvironment 'MLVAPP_GPU_AMAZE_DEBAYER_DLL=C:\path\to\igpu_amaze_debayer_cuda.dll'
```

Under `pwsh -File` pass one `-ExtraEnvironment` string; join several as `'KEY1=a,KEY2=b'` (every part must be `KEY=VALUE`).
A second separate token is refused rather than bound to another parameter.

The resulting JSON includes `visualQuality.playbackPolicy`, which records the
forced debayer/processing/GPU selector state. A passing selector smoke does not
by itself prove the no-readback path; approval still requires user-interactive
RTX 4090 screenshot/pixel evidence that the texture-present run and the
CPU-readback run are identical or that any differences are justified. The
`gui_smoke.visual_state` line now also surfaces
`gpu_amaze_texture_present_requested`, `gpu_amaze_texture_present_candidate`,
`gpu_amaze_texture_present_active`, `gpu_amaze_texture_present_renderer`, and
`gpu_amaze_texture_present_fallback_reason`, plus the upstream gate flags
`gpu_viewport_installed`, `gpu_preview_processing_env_requested`,
`gpu_preview_processing_compatible`, `render_thread_using_gpu_preview_processing`,
`render_thread_using_gpu_amaze_debayer`,
`gpu_amaze_debayer_environment_requested`, `gpu_amaze_debayer_compatible`,
`gpu_amaze_texture_present_environment_requested`, `does_mlv_always_use_amaze`,
`gpu16_preview_active`, `playback_output_mode`, and
`playback_processing_reason` so an auditor can see which gate blocked the
no-readback path or whether it fell back before the pixel comparison.

## 8. Reproducing a build from scratch

For full detail see [docs/02-developer-guide.md](02-developer-guide.md) and
the Windows/macOS/Linux supporting docs. The minimum bootstraps below are
reproduced here so an auditor can act without leaving this document.

### 8.1 Linux (easiest)

```bash
# Ubuntu 22.04 or Debian 11+
sudo apt-get update
sudo apt-get install --no-install-recommends \
    make g++ qt5-qmake qtbase5-dev qtmultimedia5-dev \
    libqt5multimedia5 libqt5multimedia5-plugins libqt5opengl5-dev \
    libqt5designer5 libqt5svg5-dev libfuse2 libxkbcommon-x11-0 appstream

cd platform/qt
qmake MLVApp.pro
make -j"$(nproc)"

# Unpack bundled FFmpeg before first launch
tar xJf FFmpeg/ffmpegLinux.tar.xz -C .

./mlvapp --help
```

Expected outputs: a single binary `platform/qt/mlvapp` and an `ffmpeg`
binary sitting alongside it. A `make install` step is **not** provided;
distribution integration uses `linuxdeploy`/`AppImage` (see
`.github/workflows/Linux.yml`).

### 8.2 Windows (the CI recipe)

The exact steps CI runs are in
[.github/workflows/tests.yml:50-83](../.github/workflows/tests.yml).
PowerShell 5.1:

```powershell
# Provision Qt + MinGW exactly like CI does
python -m pip install "aqtinstall==3.3.*"
python -m aqt install-qt    --outputdir C:\qt windows desktop 6.10.2 win64_mingw
python -m aqt install-tool  --outputdir C:\qt windows desktop tools_mingw1310 qt.tools.win64_mingw1310

$env:PATH = "C:\qt\6.10.2\mingw_64\bin;C:\qt\Tools\mingw1310_64\bin;$env:PATH"
$env:QT_OPENGL = "desktop"

# Build the app (from a clean checkout)
Push-Location platform\qt
& qmake MLVApp.pro
& mingw32-make -j2
Pop-Location

# Deploy runtime DLLs next to the exe
& "C:\qt\6.10.2\mingw_64\bin\windeployqt.exe" `
    platform\qt\release\MLVApp.exe --release --no-translations --no-compiler-runtime
```

The deterministic launch helper `.claude-state/scripts/run-mlvapp.ps1`
(documented in [AGENTS.md:30-56](../AGENTS.md)) handles the PATH, QT_OPENGL,
and optional windeployqt steps in one call. It is auxiliary, not required
for the build itself.

### 8.3 macOS

```bash
# Intel
brew install llvm qt5 openssl pcre2 harfbuzz freetype
cd platform/qt
qmake MLVApp.pro
make -j"$(sysctl -n hw.ncpu)"
# Apple Silicon: uncomment the arm64 section near MLVApp.pro:66-78 or use
# pre-built Homebrew Qt 6.4+.
```

Post-build the `.pro` file's `QMAKE_POST_LINK` steps unpack the bundled
FFmpeg / raw2mlv archives into the app bundle (see
[platform/qt/MLVApp.pro:486-491](../platform/qt/MLVApp.pro)).

## 9. Reproducing the tests from scratch

The tests are the primary behavioural evidence for a reviewer. Both
blocking suites are SHA-256 golden-hash comparisons against checked-in
reference hashes.

```bash
# ---- Linux / macOS bash example ----
cd tests && mkdir -p build-audit && cd build-audit

# console_tests: regression + receipt + frame-cache + AVX parity
qmake ../console/console_tests.pro
make -j"$(nproc)"
./release/console_tests --check-golden

# pipeline_tests: in-process engine goldens against the tiny Dual ISO clip
cd .. && mkdir -p build-audit-pipeline && cd build-audit-pipeline
qmake ../pipeline/pipeline_tests.pro
make -j"$(nproc)"
./release/pipeline_tests --check-golden
```

On Windows the commands are identical but `nproc` becomes `-j2` (the CI
recipe) and the `.exe` extension is present. Full Windows recipe:
[.github/workflows/tests.yml:57-83](../.github/workflows/tests.yml).

### Expected pass counts

The blocking pass criterion is **`0 failures`** on each `--check-golden`
invocation, signalled by **process exit code 0**. Assertion and test
counts drift as new assertions land in the harness, so the doc deliberately
does not pin them to a specific integer.

The test-runner is the in-tree `minitest` micro-harness
([tests/common/minitest.h](../tests/common/minitest.h)), which:

- counts and prints assertions/tests/skips on stderr at the end of each run;
- exits with code `1` on any test failure, `2` on a hash-output write error,
  and `3` on a `--check-golden` mismatch (see
  [tests/console/test_main.cpp:71-87](../tests/console/test_main.cpp) and
  [tests/pipeline/test_main.cpp:108-126](../tests/pipeline/test_main.cpp));
- exits with code `0` on a clean run.

| Command | Pass criterion | What to observe |
|---|---|---|
| `console_tests --check-golden` | exit code 0 | Final stderr line of the form `<n> tests / <m> assertions / <k> skips / 0 failures`; observed locally on commit `970bc389` as ~41 tests / ~160 assertions / ~17 skips with the stub build, and ~750 assertions when an `MLVApp.exe` is on `PATH` for the app-backed subprocess steps. Numbers will differ as the harness grows. |
| `pipeline_tests --check-golden` | exit code 0 | Same final-line format; observed locally as ~46 tests / ~526 assertions / ~4 skips. The stricter Dual-ISO-only subset (`MINITEST_FILTER=DualIso`) drops to ~33 tests / ~432 assertions. |
| `alloc_tests` | exit code 0 | Allocator tracking smoke. |
| `gui_tests` (offscreen) | exit code 0 (**independent non-blocking CI job** — job-level `continue-on-error: true`) | Skips the zebra parity seam on `llvmpipe` software GL hosts. |
| `fuzz_receipt_loader tests/fixtures/receipts` | no crashes, no UBSan/ASan findings | Run for a meaningful wall-clock budget; check for `crash-*`/`leak-*`/`timeout-*` outputs in the working dir. |
| `fuzz_lj92 tests/fixtures/clips/tiny_dual_iso.mlv` | no crashes | Same protocol. |
| `fuzz_mlv_open tests/fixtures/clips/tiny_dual_iso.mlv` | no crashes | Same protocol. |

The "observed locally" numbers above are anchored to commit `970bc389`
(HEAD of `codex/festive-boyd-integration` at time of writing) and are
provided so an auditor running the same commit can sanity-check that they
are running the **same suite** rather than a stripped-down subset. They are
**not** part of the pass criterion. To re-establish the current numbers
on any commit, run the suite once and read the final summary line — this
is the only authoritative source. The previous draft cited
`tests/README.md:187-188` for these counts; that file is **31 lines** of
directory-map text and contains no assertion counts (it was rewritten on
this branch — `git diff tests/README.md` shows the deletion). Do not
re-introduce that citation.

If either blocking suite reports any failure on an unmodified master
checkout, treat that as an audit finding.

### Golden comparison method

- **Binary SHA-256** match on frame output — no floating-point tolerance
  for the headline goldens.
- **Backend-parametric** tests (CPU vs GPU paths) use PSNR with
  `max_abs_diff=3`, `mismatch_rate<=0.1%`.

See [docs/13-testing-infrastructure.md](13-testing-infrastructure.md) for
the full test-suite inventory and
[docs/15-test-fixtures.md](15-test-fixtures.md) for the fixture/golden-hash
inventory. (Auditors may notice an internal `.claude-state/docs-audit/`
working directory — that is **agent scratch**, not part of the shipped
documentation, and is excluded from release tarballs per §13.7.)

## 10. What CI gates

Condensed from
[.github/workflows/tests.yml](../.github/workflows/tests.yml) and the
release workflows.

| Aspect | Value |
|---|---|
| Tests runner | `windows-latest` |
| Qt version provisioned | **6.10.2** (via `aqtinstall==3.3.*`) |
| Compiler provisioned | **MinGW 13.1** (`tools_mingw1310`) |
| Blocking jobs | `console_tests --check-golden`, `pipeline_tests --check-golden` |
| Non-blocking pilot | independent `windows-gui-pilot` job, offscreen, with job-level `continue-on-error: true` |
| Intentionally **not** in CI | `perf_tests` (machine-sensitive), `fuzz_*` (local/nightly), full `gui_tests` gating |
| Release workflows | `Windows.yml` (Win64 zip), `macOS-Intel.yml` (.dmg, `macos-13`), `macOS-Arm64.yml` (.dmg, `macos-14`), `Linux.yml` (.AppImage, `ubuntu-22.04`) |
| Release trigger | `workflow_dispatch` on `master` only |

**Important**: Linux and macOS **tests** are not currently gating on
master. Only Windows runs the blocking regression suite; Linux and macOS
receive release-build workflows but no test-suite equivalent. To close
that gap during an audit, run the §9 commands locally on Linux and macOS
hosts before signing off — the qmake/`make` flow is portable and the
golden hashes are the same on every platform. See §13.3 for the broader
list of CI gaps.

## 11. How to verify specific product claims independently

Every headline feature advertised in [README.md](../README.md) maps to code
and, where applicable, to a test. A reviewer can walk this table to build
their own confidence that each claim is grounded.

| README claim | Code to read | Test to run |
|---|---|---|
| Import MLV and spanned MLV (`.m00`, `.m01`, ...) | [src/mlv/video_mlv.c](../src/mlv/video_mlv.c) (`openMlvClip`), `frame_index_t` in [src/mlv/mlv_object.h](../src/mlv/mlv_object.h) | `console_tests` clip-golden subpath |
| Support for lossless MLV (LJ92) | [src/mlv/liblj92/lj92.c](../src/mlv/liblj92/lj92.c) | `fuzz_lj92 tests/fixtures/clips/tiny_dual_iso.mlv` (hardening); `pipeline_tests --check-golden` round-trips the tiny Dual ISO fixture |
| Bit depths 10/12/14 | [src/mlv/mlv.h](../src/mlv/mlv.h) (`RAWI` block `black_level`, `white_level`, `bits_per_pixel`); [src/dng/dng.c](../src/dng/dng.c) | `pipeline_tests --check-golden` |
| Demosaic algorithms (None / Simple / AHD / AMaZE bilinear / LMMSE / DCB / RCD / IGV) | [src/debayer/debayer.c](../src/debayer/debayer.c); algorithm bodies in `src/debayer/` + [src/librtprocess/src/](../src/librtprocess/src/) | [tests/pipeline/test_backend_parametric_debayer_shell.cpp](../tests/pipeline/test_backend_parametric_debayer_shell.cpp) (bilinear+AMaZE goldens) |
| Processing pipeline (exposure/contrast/WB/clarity/vibrance/sat/curves/...) | [src/processing/raw_processing.c](../src/processing/raw_processing.c); [src/processing/processing.c](../src/processing/processing.c); 150+ setters on `processingObject_t` | `console_tests` receipt-applier suite; `pipeline_tests` full16/preview16 goldens |
| Film-emulation filters (genann neural net) | [src/processing/filter/genann/](../src/processing/filter/genann/); `filter/` front-end | [tests/pipeline/test_processing_filters.cpp](../tests/pipeline/test_processing_filters.cpp) |
| sRGB / LOG transfer functions | [src/processing/raw_processing.c](../src/processing/raw_processing.c) gamma/tonemap stage | `pipeline_tests --check-golden` |
| Raw corrections (focus/bad pixels, chroma smoothing, pattern noise, vertical stripes) | [src/mlv/llrawproc/](../src/mlv/llrawproc/) | `pipeline_tests` forced-rerender llrawproc guard |
| Dual ISO | [src/mlv/llrawproc/dualiso.c](../src/mlv/llrawproc/dualiso.c) | [tests/pipeline/test_dual_iso_pipeline.cpp](../tests/pipeline/test_dual_iso_pipeline.cpp) (1,398 LOC, primary) |
| Dark-frame subtraction | [src/mlv/llrawproc/](../src/mlv/llrawproc/) (`darkframe`), `saveMlvAVFrame` averaged-frame mode | `pipeline_tests`; synthetic `tiny_dual_iso_darkframe` perf scenario |
| HDR blending (on FFmpeg export) | `platform/qt/ExportSettingsDialog.*` + ffmpeg child-process pipe | manual export test |
| 1D/3D LUT (`.cube`) | [src/processing/cube_lut.c](../src/processing/cube_lut.c) | `pipeline_tests` receipt-applier when a LUT-bearing receipt is loaded |
| Scopes (histogram / waveform / parade / vectorscope) | `platform/qt/Histogram.*`, `WaveFormMonitor.*`, `VectorScope.*`, `ScopesLabel.*` | `gui_tests` ScopesLabel regressions |
| Zebras | `platform/qt/GraphicsPickerScene.*`, GPU-shader overlay in `GpuDisplayViewport.*` | `gui_tests` zebra-parity seam (skipped on `llvmpipe`) |
| Auto focus-pixel detection | [src/mlv/llrawproc/](../src/mlv/llrawproc/) (auto detection); [pixel_maps/](../pixel_maps/) tables; [platform/qt/FocusPixelMapManager.*](../platform/qt/FocusPixelMapManager.h) | `pipeline_tests` pixel-map guards |
| Vertical stretch autodetect | [src/mlv/](../src/mlv/) (aspect-ratio metadata) | manual with stretched fixture |
| MAPP (on-disk frame index) | [src/mlv/](../src/mlv/) (`.MAPP` reader/writer) | unit-tested via `console_tests` clip-golden |
| FCPXML import / selection | `platform/qt/FcpxmlAssistantDialog.*`, `FcpxmlSelectDialog.*` | GUI smoke only |
| Batch export / CLI (`--batch`) | [platform/qt/main.cpp:26-33](../platform/qt/main.cpp) (`hasBatchFlag`); `src/batch/BatchRunner.{h,cpp}` | `console_tests` batch smoke |
| MLV Trim (`--trim-mlv`) | [platform/qt/main.cpp:35-42](../platform/qt/main.cpp) (`hasTrimMlvFlag`); [src/batch/MlvTrim.h](../src/batch/MlvTrim.h) + [.cpp](../src/batch/MlvTrim.cpp) | manual |
| Post-export scripting (macOS) | `platform/qt/Scripting.{h,cpp}` | GUI-only; not in CI |
| Update checker | [platform/qt/Updater/](../platform/qt/Updater/) | manual |
| AVIR resize | [platform/qt/avir/](../platform/qt/avir/); invoked via `ExportSettingsDialog` + `RenderFrameThread` preview-scale seam | `pipeline_tests` preview16 goldens |

For each, the reviewer can (a) read the listed file(s) to confirm the
feature exists, (b) run the listed test to confirm it behaves, or (c) run
the application against a fixture in [tests/fixtures/clips/](../tests/fixtures/clips/)
to confirm end-to-end.

## 12. Documentation coverage map

This maps each public-visible feature category to the document(s) that
cover it. Reviewers use this to spot doc gaps: a feature with no dev-doc
coverage is a coverage risk; a feature with no test is a correctness risk
(see §11).

| Feature area | User-facing | Dev / architectural | Tests | CI |
|---|---|---|---|---|
| Opening / importing MLV | 01 | 03 §§2–3; [src/mlv/](../src/mlv/) | `console_tests` clip-golden | Yes |
| LJ92 decompression | — | 03 §2; [src/mlv/liblj92/](../src/mlv/liblj92/) | `pipeline_tests`; `fuzz_lj92` | Yes (pipeline); no (fuzz) |
| MCRAW support | — | 03; [src/mlv/mcraw/](../src/mlv/mcraw/) | covered via `console_tests` | Yes |
| Dual ISO | 01; 02 | 03 §3; [src/mlv/llrawproc/dualiso.c](../src/mlv/llrawproc/dualiso.c) | [tests/pipeline/test_dual_iso_pipeline.cpp](../tests/pipeline/test_dual_iso_pipeline.cpp) | Yes |
| Demosaic | 01 | 03 §3; [src/debayer/](../src/debayer/) + [src/librtprocess/](../src/librtprocess/) | [tests/pipeline/test_backend_parametric_debayer_shell.cpp](../tests/pipeline/test_backend_parametric_debayer_shell.cpp) | Yes |
| Processing pipeline | 01 | 03 §4; [src/processing/](../src/processing/) | `pipeline_tests` full16/preview16 | Yes |
| LUT / `.cube` | 01 | 03; [src/processing/cube_lut.c](../src/processing/cube_lut.c) | `console_tests` receipt-applier | Yes |
| Export (FFmpeg) | 01 | 02; `QProcess` driver inline at [platform/qt/MainWindow.cpp:5234](../platform/qt/MainWindow.cpp); command-string assembly at `MainWindow.cpp:3937,4490,4496` | — (child-process driven) | No |
| Export (Cinema DNG) | 01 | 03 §3; [src/dng/](../src/dng/) | `console_tests` batch CDNG | Yes |
| Batch / CLI | 01; 02 | [platform/qt/main.cpp:26-62](../platform/qt/main.cpp) (the four `has*Flag()` argv pre-scanners); [src/batch/](../src/batch/) (BatchRunner, ReceiptLoader, ReceiptApplier, MlvTrim) | `console_tests` batch path | Yes |
| GPU presenter (experimental) | — | 12 (`docs/12-gpu-viewport-architecture.md`) | `gui_tests` presenter seams | Non-blocking |
| Update checker | — | [platform/qt/Updater/](../platform/qt/Updater/) | — | No |
| Focus-pixel maps | 01 | [platform/qt/FocusPixelMapManager.h](../platform/qt/FocusPixelMapManager.h) | `pipeline_tests` pixel-map guards | Yes |
| Crash forensics | — | [platform/qt/CrashForensics.h](../platform/qt/CrashForensics.h) | [tests/pipeline/test_crash_forensics.cpp](../tests/pipeline/test_crash_forensics.cpp) | Yes |
| Playback profiling (`--profile-playback`) | — | [docs/14-performance-benchmarking.md](14-performance-benchmarking.md); [platform/qt/main.cpp:44-50](../platform/qt/main.cpp) (`hasPlaybackProfileFlag`) | `console_tests` subprocess smoke | Yes |
| Perf harness | — | 14 | `perf_tests` | **No** (local only) |
| Fuzz targets | — | 16; [tests/fuzz/README.md](../tests/fuzz/README.md) | `fuzz_mlv_open`, `fuzz_lj92`, `fuzz_receipt_loader` | **No** (local/nightly) |
| Post-export scripting (macOS) | 01 §8 | `platform/qt/Scripting.h` | — | No |

Column meaning:

- **User-facing**: document number within `docs/` where the feature is
  surfaced to end-users.
- **Dev / architectural**: the doc-set section *plus* the authoritative
  source path in `src/` or `platform/qt/`.
- **Tests**: the test file or suite that covers the feature.
- **CI**: "Yes" means the feature's test is in the blocking CI path;
  "Non-blocking" means it runs with `continue-on-error: true`; "No" means
  CI is intentionally not configured.

## 13. Known risks and open questions (for a reviewer to evaluate)

A defensible audit is explicit about gaps. Below is what we believe to be
a complete list, based on a 2026-04-24 audit sweep against branch
`codex/festive-boyd-integration` at HEAD `970bc389`. The internal scratch
directory used during that sweep (`.claude-state/docs-audit/`) is **agent
working state** and is not part of the shipped documentation; an auditor
should rely on the bullets below and the cross-referenced source files,
not on the scratch directory.

### 13.1 License mixing and copyleft propagation

`src/librtprocess/` is GPL-3.0 (see [src/librtprocess/LICENSE.txt](../src/librtprocess/LICENSE.txt)).
Because MLV App links against it, the **shipped binary is GPL-3.0**.
Downstream redistributors must:

- preserve the full `LICENSE` text in the redistributed package;
- offer or provide corresponding source;
- not impose additional restrictions inconsistent with GPL-3.0.

The other vendored trees (MIT / zlib / Apache-2.0) are GPL-compatible and
do not introduce additional constraints.

### 13.2 Binary blobs vendored in-tree

`platform/qt/FFmpeg/*.{zip,tar.xz}` and `platform/qt/raw2mlv/*` are
pre-built. A reviewer should:

- extract each archive and record the FFmpeg configure string
  (`ffmpeg -version`);
- compare checksums against FFmpeg's official release for the same version;
- confirm the compiled-in set of codecs/muxers matches what the app
  actually invokes.

This is **a distribution-boundary risk**, not an in-process risk: FFmpeg is
exec'd as a child, not linked.

### 13.3 Test coverage gaps

- **Linux and macOS CI do not run the blocking test suite.** Only Windows
  runs `console_tests` and `pipeline_tests`. Release workflows on the other
  platforms build and package but do not gate on correctness. A reviewer
  evaluating Linux or macOS binaries should run the tests locally on those
  platforms before signing off.
- **`gui_tests` is non-blocking.** The independent `windows-gui-pilot` job
  carries job-level `continue-on-error: true`; its build and run steps still
  fail normally. The graduation record is in
  [docs/13-testing-infrastructure.md](13-testing-infrastructure.md).
- **Perf tests are not in CI.** They are intentionally local-only because
  of runner noise; an auditor wanting a perf baseline must run
  `perf_tests --iterations 10 --require-baseline` locally (see
  [tests/perf/README.md](../tests/perf/README.md)).
- **Fuzz targets are not in CI.** Reviewer should seed and run locally
  (see §7.1).
- **GPU compute parity has no image-processing coverage.** Current
  OpenGL support is presentation-only; the `tests/pipeline` `gpu_preview_subset`
  goldens pin the drift-detection surface only, not full CPU/GPU parity.

### 13.4 Experimental paths off by default

`MLVAPP_EXPERIMENTAL_GL_VIEWPORT`, `MLVAPP_EXPERIMENTAL_GPU_PROCESSING`,
`MLVAPP_EXPERIMENTAL_GPU_DEBAYER`,
`MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER`, and
`MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT` all default OFF. The
production surface an auditor evaluates for "default behaviour" is the CPU
path. Confirm these env vars are unset in the runtime environment you audit.

### 13.5 Mutable state during playback (and the broader WIP surface)

Historically the playback path had GUI-writes on the render thread. The
current tree has moved to an **immutable `PresentationContext`** delivered
alongside each `ReadyFrame` (commit `244c03a1` — "Playback: pass immutable
presentation context with ready frame"). A subsequent WIP commit
(`970bc389` — "Playback: presentation-split async-prep WIP and investigation
notes") continues that effort. Reviewer should read:

- [platform/qt/RenderFrameThread.h](../platform/qt/RenderFrameThread.h) +
  [platform/qt/RenderFrameThread.cpp](../platform/qt/RenderFrameThread.cpp)
- `MainWindow.cpp` (search `drawFrameReady`)

to confirm the current `PresentationContext` contract matches the prose in
03 and this guide.

#### Working-tree modifications (full enumeration)

`git status --short` on the branch this guide was written against
(`codex/festive-boyd-integration` at HEAD `970bc389`) shows **6 modified
files plus one untracked directory**. Re-run `git status --short` to verify
— the live output is authoritative; this list is the snapshot at the time
of writing:

```
$ git status --short
 M README.md
 M platform/qt/GPUDisplayFoundation.md
 M tests/README.md
 M tests/fixtures/clips/README.md
 M tests/fuzz/README.md
 M tests/perf/README.md
?? docs/
```

One-line description per change (use `git diff <path>` to inspect):

| Path | Change | Why it's modified |
|---|---|---|
| `README.md` | +1 line | Adds a `> Full documentation: see [docs/](docs/)` pointer near the top so first-time readers find the canonical doc set. |
| `platform/qt/GPUDisplayFoundation.md` | rewrite to a 4-line stub | Content migrated to [docs/12-gpu-viewport-architecture.md](12-gpu-viewport-architecture.md); the in-tree `.md` is now a "moved to" pointer. |
| `tests/README.md` | rewrite to a ~31-line index | Pruned the prior 393-line test snapshot (which had drifted) down to a directory map. Canonical content now lives under [docs/13-testing-infrastructure.md](13-testing-infrastructure.md), [docs/14-performance-benchmarking.md](14-performance-benchmarking.md), [docs/15-test-fixtures.md](15-test-fixtures.md), and [docs/16-fuzz-testing.md](16-fuzz-testing.md). |
| `tests/fixtures/clips/README.md` | rewrite to a 4-line stub | Same pattern — content migrated to [docs/15-test-fixtures.md](15-test-fixtures.md). |
| `tests/fuzz/README.md` | rewrite to a 4-line stub | Same pattern — content migrated to [docs/16-fuzz-testing.md](16-fuzz-testing.md). |
| `tests/perf/README.md` | rewrite to a 4-line stub | Same pattern — content migrated to [docs/14-performance-benchmarking.md](14-performance-benchmarking.md). |
| `docs/` (untracked) | new directory | The canonical documentation tree this guide is part of. Once the branch lands on `master`, the directory becomes tracked. |

Two categories of change are visible:

1. **Documentation rewrites** — five legacy `*.md` files (`tests/README.md`,
   `tests/fixtures/clips/README.md`, `tests/fuzz/README.md`,
   `tests/perf/README.md`, `platform/qt/GPUDisplayFoundation.md`) all
   collapse to short pointers into the new canonical `docs/` tree.
2. **README pointer** — `README.md` now links into the new docs tree.

**No engine source files are modified on this branch.** The WIP referenced
in commit `970bc389`'s subject line ("Playback: presentation-split async-prep
WIP and investigation notes") is **investigation scaffolding only** — see the
`.claude/analysis/` notes that document the planned design — and has not
landed in `src/` or `platform/qt/` source files. An auditor should treat
this branch as a **documentation-only delta** for the purpose of code review.
None of these changes introduce new external dependencies or new attack
surface.

If `git status` on a fresh checkout shows engine-source modifications that
this table does not mention, the branch has advanced beyond the snapshot
above; re-derive the table by running `git status --short` and `git diff
--stat` against the current HEAD before relying on this enumeration.

### 13.6 Crash minidump destinations

See §7.4. The dump and log destinations are local-only in the code we've
read; a reviewer should confirm this by inspecting
[platform/qt/CrashForensics.cpp](../platform/qt/CrashForensics.cpp) for any
outbound network call. None is expected.

### 13.7 `.claude/` and `.claude-state/` directories

These contain curated agent investigation notes and ephemeral scratch.
They are explicitly **not part of the shipped product**
([AGENTS.md:3-8](../AGENTS.md)). A reviewer auditing a distribution tarball
should confirm the tarball excludes both directories. Neither directory is
referenced from the build (`platform/qt/MLVApp.pro`) and neither appears in
any release workflow.

For a reviewer assessing change-control governance (not product code):
`.claude-state/coordination/` holds the **content-review-gate** ledger that the
repo's brokered closeout uses to gate work-block *finalize* (the merge of a
work block onto the target branch). When
`contentReviewGate.requireClaudeApprovalForFinalize` is enabled in
[`closeout.config.json`](../closeout.config.json), the broker
([`tools/repo_hygiene/brokered_closeout.py`](../tools/repo_hygiene/brokered_closeout.py),
`validate_content_review_approval_for_finalize`) refuses to finalize unless the
ledger records a handoff entry from one agent and a later approving review entry
from a *different* agent, each pinned to the exact full-length
`startHead..featureHead` commit range and carrying an exact `Verdict:` token.
This is a development-workflow integrity control between the paired agents; like
the rest of these directories it ships in **no** release artifact and is not
referenced by the build.

### 13.8 Default-on decode-ahead prefetch

As of commit `00091b62`, the raw-uint16 decode-ahead prefetch worker is
on by default. Gate it off with `MLVAPP_DISABLE_RAW_UINT16_PREFETCH=1` if
you are profiling thread-local telemetry (see
[src/mlv/video_mlv.c](../src/mlv/video_mlv.c) around the prefetch block);
the old `MLVAPP_EXPERIMENTAL_RAW_UINT16_PREFETCH` env var is inert and
setting it has no effect.

## 14. External dependencies to audit separately

For each dependency, the auditor should independently confirm the upstream
source, version, and license at the time of packaging.

| Dependency | Source | Typical license | Notes |
|---|---|---|---|
| **Qt 5** (5.6 – 5.15.2) | <https://www.qt.io/> | LGPL-3.0 (open-source edition) | Linked dynamically; deployed via `windeployqt` / `macdeployqt` / `linuxdeploy` |
| **Qt 6** (6.4+; 6.5+ on Windows; CI uses 6.10.2) | <https://www.qt.io/> | LGPL-3.0 | Same |
| **FFmpeg** (binary) | <https://ffmpeg.org/> | LGPL-2.1+ or GPL depending on build | Bundled; exec'd as child process. **Verify the upstream build's configure string.** |
| **raw2mlv** (binary) | upstream project | follow upstream | Bundled; exec'd as child process |
| **OpenMP runtime** — `libgomp` (Windows, Linux) or Homebrew `libomp` (macOS) | GCC / LLVM | GPL with runtime exception (libgomp) / Apache 2.0 with LLVM exception (libomp) | Linked via `-lgomp` / `-lomp` |
| **MinGW runtime** (Windows) | mingw-w64 | mixed (zlib/MIT/GPL with runtime-library exception) | Shipped DLLs: `libgcc_s_*`, `libstdc++-6`, `libwinpthread-1` |
| **librtprocess** | vendored under `src/librtprocess/` | **GPL-3.0** | **Copyleft; controls the binary licence** |
| **liblj92** | vendored under `src/mlv/liblj92/` | MIT | |
| **cJSON** | vendored under `src/mlv/mcraw/cJSON.{c,h}` | MIT | |
| **tinyexpr** | vendored under `src/processing/tinyexpr/` | zlib | |
| **genann** | vendored under `src/processing/filter/genann/` | zlib | |
| **rbfilter** | vendored under `src/processing/rbfilter/` | MIT | |
| **sobel** | vendored under `src/processing/sobel/` | Apache-2.0 | |
| **AVIR** | vendored under `platform/qt/avir/` | MIT | |
| **maddy** | vendored under `platform/qt/maddy/` | MIT | |
| **dbghelp.dll** (Windows) | Microsoft platform DLL | Microsoft SDK | System-provided; used for `MiniDumpWriteDump` |
| **Homebrew LLVM** (macOS builds) | <https://brew.sh> / LLVM upstream | Apache 2.0 with LLVM exception | Used to build macOS binaries; **Apple-supplied clang is not used** because it lacks OpenMP |
| **Chocolatey `qt5-default` or `aqtinstall`** (Windows provisioning) | <https://chocolatey.org/> / pip | provisioning only | Not in shipped binary |
| **linuxdeploy / linuxdeployqt / AppImage tools** (Linux packaging) | <https://github.com/linuxdeploy> | mixed | Packaging-only, not in binary |

## 15. Traceability matrix

Short matrix mapping the most-referenced artefacts to their location and
the command an auditor uses to confirm them.

| Artifact | Location | Verified by |
|---|---|---|
| Version string `1.15.0.0` | [platform/qt/MLVApp.pro:450-460](../platform/qt/MLVApp.pro) | `grep -n "^VERSION" platform/qt/MLVApp.pro` (expect `VERSION_MAJOR = 1`, `VERSION_MINOR = 15`, `VERSION_PATCH = 0`, `VERSION_BUILD = 0`, and the aggregate `VERSION = ...` line near 460) |
| License GPL-3.0 | [LICENSE](../LICENSE) | `head -3 LICENSE` (first line: `                    GNU GENERAL PUBLIC LICENSE`) |
| Qt modules | [platform/qt/MLVApp.pro:7-19](../platform/qt/MLVApp.pro) | `grep -nE "^QT\\s*\+=|^greaterThan\\(QT_MAJOR" platform/qt/MLVApp.pro` |
| Git SHA embedded at build | `MLVAPP_GIT_SHA` define ([platform/qt/MLVApp.pro:106-112](../platform/qt/MLVApp.pro)) | After building on Windows: `strings MLVApp.exe \| grep -E '^[0-9a-f]{40}$'` |
| Pipeline goldens | [tests/fixtures/golden/pipeline_hashes.json](../tests/fixtures/golden/pipeline_hashes.json) | `pipeline_tests --check-golden` |
| Console goldens | [tests/fixtures/golden/hashes.json](../tests/fixtures/golden/hashes.json) | `console_tests --check-golden` |
| GUI goldens | [tests/fixtures/golden/gui_hashes.json](../tests/fixtures/golden/gui_hashes.json) | `gui_tests` (offscreen; non-blocking in CI) |
| CI policy | [.github/workflows/tests.yml](../.github/workflows/tests.yml) | read file end-to-end; lines 57-83 are the blocking build+run steps |
| Release policy (Windows) | [.github/workflows/Windows.yml](../.github/workflows/Windows.yml) | read; triggers on `workflow_dispatch` only |
| Release policy (macOS Intel) | [.github/workflows/macOS-Intel.yml](../.github/workflows/macOS-Intel.yml) | read |
| Release policy (macOS ARM) | [.github/workflows/macOS-Arm64.yml](../.github/workflows/macOS-Arm64.yml) | read |
| Release policy (Linux AppImage) | [.github/workflows/Linux.yml](../.github/workflows/Linux.yml) | read |
| CLI flag inventory | [platform/qt/main.cpp:26-62](../platform/qt/main.cpp) | `grep -nE "\"--" platform/qt/main.cpp` |
| Experimental GPU env vars | `grep -rn "MLVAPP_EXPERIMENTAL_" platform/qt/` is the **primary** evidence. Hits include `platform/qt/GpuDisplayViewport.cpp`, `GpuPreviewProcessing.cpp`, `GpuDebayer.cpp`, and `platform/qt/main.cpp`. Cross-referenced from [docs/00-overview.md](00-overview.md) "Status at a glance". | run the grep; expect 5 env vars (`MLVAPP_EXPERIMENTAL_GL_VIEWPORT`, `MLVAPP_EXPERIMENTAL_GPU_PROCESSING`, `MLVAPP_EXPERIMENTAL_GPU_DEBAYER`, `MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER`, `MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT`) |
| Prefetch disable env var | [src/mlv/video_mlv.c](../src/mlv/video_mlv.c) | `grep -n "MLVAPP_DISABLE_RAW_UINT16_PREFETCH" src/mlv/video_mlv.c` |

## 16. Citation freshness — how to spot-check this guide

Every claim in this guide that names a file path or a line number was
written against branch `codex/festive-boyd-integration` at HEAD
`970bc389`. Code lines drift between releases as functions are added,
re-ordered, refactored, or merged. **Any specific line number cited here
should be treated as an anchor that may have moved** — the file path,
function name, and surrounding context are the durable parts; the integer
line number is the convenience.

### 16.1 Anatomy of a citation in this guide

A typical citation in this guide looks like:

> `MainWindow.cpp:5234` (`QProcess *ffmpegProc = new QProcess(this);`)

The three pieces:

- **File path** — durable; rename-tracked by `git log --follow`.
- **Line number** — anchor only; may drift on every commit that adds or
  removes lines above it.
- **Symbol or literal in parentheses** — the **stable** identifier. Use
  this with `grep -n` if the line number no longer resolves.

### 16.2 Spot-check procedure

For an auditor verifying a sample of claims (recommended sample size: at
least 10 of the citations in this guide, biased toward the security and
test-coverage sections):

```bash
# 1. Pin the commit you are auditing.
git rev-parse HEAD

# 2. For each citation of the form "path/to/file.ext:NNN":
#    a) confirm the file exists.
ls path/to/file.ext

#    b) read the cited line and ~5 lines before/after.
sed -n '$((NNN-5)),$((NNN+5))p' path/to/file.ext

#    c) if the cited symbol/literal is no longer there, find its current
#       location. Most citations include a parenthetical symbol or string;
#       grep for it.
grep -n "QProcess \*ffmpegProc" platform/qt/MainWindow.cpp
grep -n "VERSION_MAJOR" platform/qt/MLVApp.pro
grep -n "MLVAPP_DISABLE_RAW_UINT16_PREFETCH" src/mlv/video_mlv.c
```

If a citation no longer resolves and the symbol it described no longer
appears anywhere in the named file, raise a finding under "documentation
debt". If the symbol moved to a different file, treat it as a doc-update
candidate (not a security finding) and note the new location.

### 16.3 Known anchor commits

These commits are the time-of-writing anchor for the integer line numbers
in this guide:

| Anchor | Commit | What to expect |
|---|---|---|
| Repo HEAD | `970bc389` | The branch tip this guide was written against. |
| Last engine commit before this branch | `e159424d` | Useful as a sanity baseline if the engine has not been touched. |
| Default-on prefetch | `00091b62` | Introduces `MLVAPP_DISABLE_RAW_UINT16_PREFETCH`. |
| Immutable `PresentationContext` | `244c03a1` | Frame-pipeline contract called out in §6 / §13.5. |

Run `git log --oneline 970bc389..HEAD` to enumerate everything that has
landed since this guide was written; the bigger the diff, the more likely
that integer line numbers have drifted.

### 16.4 Provenance audit script

For an auditor wanting to mechanically re-run the sample audit that
graded this doc, the high-value spot-checks are:

```bash
# License + version spine
head -3 LICENSE
grep -n "^VERSION" platform/qt/MLVApp.pro

# CI policy
grep -nE "windows-latest|6.10.2|tools_mingw1310|continue-on-error" \
  .github/workflows/tests.yml

# Receipt parser (both paths)
grep -n "readXmlElementsFromFile\b" platform/qt/MainWindow.cpp
ls src/batch/ReceiptLoader.h src/batch/ReceiptLoader.cpp
grep -n "ReceiptLoader::loadFromFile" src/batch/ReceiptLoader.cpp \
  platform/qt/MainWindow.cpp

# FFmpeg child process
grep -n "QProcess.*ffmpeg\|ffmpegProc\|ffmpegCommand" \
  platform/qt/MainWindow.cpp

# FpmInstaller (header-only)
ls platform/qt/FpmInstaller.*
grep -n "static bool installFpm" platform/qt/FpmInstaller.h

# Crash-forensics destination + no telemetry
grep -n "AppDataLocation\|MiniDumpWriteDump\|mlvapp-" \
  platform/qt/CrashForensics.cpp
grep -nE "QNetworkAccessManager|http://|https://" \
  platform/qt/CrashForensics.cpp   # expected: no matches

# Prefetch env var
grep -n "MLVAPP_DISABLE_RAW_UINT16_PREFETCH\|MLVAPP_EXPERIMENTAL_RAW_UINT16_PREFETCH" \
  src/mlv/video_mlv.c

# Experimental GPU env vars
grep -rn "MLVAPP_EXPERIMENTAL_" platform/qt/

# WIP file enumeration
git status --short
```

If any of these returns an unexpected result, the corresponding section
of this guide needs an update before the audit closes out.

## 17. How to report findings

- **Defects / correctness / functional bugs** — open an issue at
  <https://github.com/ilia3101/MLV-App/issues>. Include: reproducer MLV
  fixture (or synthetic scenario), exact commit hash, platform and Qt
  version, and expected-vs-actual output.
- **Security disclosures** — the project does not yet publish a formal
  security-contact mailbox. **Recommended path** (in order of preference):
  1. **GitHub Private Vulnerability Reporting**, if enabled on the
     upstream repo. Verify by visiting
     <https://github.com/ilia3101/MLV-App/security/advisories> and clicking
     "Report a vulnerability". If the form loads, this is the embargo-safe
     channel. As of the time of writing this doc, an auditor should
     **confirm enablement themselves** before relying on it — repo owners
     can disable the feature.
  2. If Private Vulnerability Reporting is **not** enabled, route
     correspondence through the public issue tracker at
     <https://github.com/ilia3101/MLV-App/issues> with `[security]` in the
     title and tag the maintainer (`@ilia3101`) requesting embargoed
     handling. **This route is non-confidential by design** — anyone watching
     the repo will see the issue title; sensitive findings should be
     redacted to a stub ("see private channel for details") until a
     private channel is established.
  3. The lack of a documented embargo path is itself a finding for an
     acquirer's process review and should be raised in the audit report's
     "Process gaps" section.
- **Community** — the user community is on the Magic Lantern forum thread
  at <https://www.magiclantern.fm/forum/index.php?topic=20025.0>; this is
  the right place to confirm reproducer steps with other users before
  filing an issue.

For any audit output, include the SHA of the checkout you reviewed
(`git rev-parse HEAD`). On the branch this guide was written against:
`git log -1 --format=%H` returns `970bc389` (the HEAD of
`codex/festive-boyd-integration` at time of writing).

## 18. Glossary

Stranger-friendly one-line definitions for every domain term used in this
guide.

- **MLV** — "Magic Lantern Video"; a raw-video container format emitted
  by the Magic Lantern firmware hack for Canon DSLRs. Stores bayer raw
  sensor samples in numbered blocks (`VIDF`, `AUDF`, `VERS`, etc.).
- **Magic Lantern** — a third-party open-source firmware add-on for Canon
  EOS cameras that enables raw video recording, among other features.
- **Spanned MLV** — a recording that was split across multiple files as
  `.mlv`, `.m00`, `.m01`, ... because of the camera's filesystem limits.
- **Dual ISO** — a Magic Lantern technique that alternates ISO rows within
  a single sensor readout to extend dynamic range. MLV App reconstructs a
  single HDR image from the interleaved rows.
- **Debayer / demosaic** — converting a single-channel Bayer sensor
  pattern (one of R, G, or B per pixel) into a full three-channel RGB
  image. Multiple algorithms exist with different quality/speed tradeoffs.
- **Receipt** — MLV App's word for the bag of processing settings (exposure,
  WB, curves, LUT, denoise, etc.) applied to a clip. Serialised as XML with
  the extension `.marxml`.
- **MAPP** — MLV App's on-disk frame-index cache file: includes the video
  and audio frame indexes so re-opening a clip is fast. Optional.
- **LJ92** — "Lossless JPEG 1992"; a predictive entropy coder used by
  Magic Lantern to losslessly compress MLV frames. Decoder lives in
  `src/mlv/liblj92/`.
- **Pred1 / Pred6** — LJ92 predictor modes. MLV App has a fast path for
  predictor 1 and a separate path for predictor 6 (see the
  per-frame `stageTimingTelemetry` keys emitted by `RenderFrameThread`
  and the LJ92-related telemetry getters in
  [src/mlv/video_mlv.h](../src/mlv/video_mlv.h)).
- **CDNG** (Cinema DNG) — Adobe's raw-image sequence format (one `.dng`
  per frame). A Cinema DNG export is compatible with DaVinci Resolve and
  most professional grading tools.
- **AMaZE** — a Bayer demosaic algorithm (high quality, single-pass).
  Reference implementation in `src/debayer/amaze_demosaic.c`; alternate
  wrapped via librtprocess.
- **AHD / LMMSE / DCB / RCD / IGV / Markesteijn** — other Bayer demosaic
  algorithms, wrapped via `src/librtprocess/`.
- **librtprocess** — a GPL-3.0 raw-processing library (demosaic, CA
  correction, highlight recovery) extracted from RawTherapee and vendored
  under `src/librtprocess/`.
- **AVIR** — an MIT-licensed high-quality image-resize library used for
  preview downscaling and export resize. Vendored under
  `platform/qt/avir/`.
- **Prefetch slot** — one of 4 decode-ahead buffers maintained by a
  background worker thread so that the next few frames' decompressed raw
  is ready by the time playback asks for them.
- **Cadence** — the rate at which the playback loop asks for frames. MLV
  App offers two modes: "show each frame" (every frame is rendered) and
  "drop frame" (realtime; render as many frames as the host can).
- **Golden hash** — the SHA-256 of a reference output, checked in under
  `tests/fixtures/golden/`. A new run hashes its output and compares bit-
  exactly; any divergence fails the test.
- **PSNR** — peak signal-to-noise ratio; used in backend-parametric tests
  where CPU and GPU outputs are allowed to differ within a tolerance.
- **Qt 5 / Qt 6** — the cross-platform GUI framework MLV App is built on.
  Qt 5 branches 5.6 through 5.15.2 are supported; Qt 6.4+ is supported
  (6.5+ on Windows; CI provisions 6.10.2).
- **`windeployqt`** — Qt's Windows deployment tool. Copies runtime DLLs
  next to a `.exe` so it can be launched outside a Qt Creator shell.
- **`macdeployqt`** — the macOS equivalent; populates the `.app` bundle
  with needed dylibs / frameworks.
- **`linuxdeploy` / AppImage** — Linux equivalent producing a
  self-contained `.AppImage` file.
- **OpenMP** — a compiler-native shared-memory parallelism annotation
  system (`#pragma omp parallel for`). Runtime: `libgomp` (GCC) or
  `libomp` (LLVM). On macOS, Apple-supplied clang does **not** ship
  OpenMP, which is why Homebrew LLVM is required.
- **`MLVAPP_GIT_SHA`** — a compile-time define set from
  `git rev-parse HEAD`, embedded into the binary. Surfaced in the
  `CrashForensics` run-metadata JSON. Falls back to the literal string
  `unknown` for tarball builds where git is unavailable.

## 19. Appendix — every doc at a glance

One-line summary of every document in [docs/](.) so the reviewer can scan
the documentation footprint without opening each file.

| # | File | One-line summary |
|---|---|---|
| 00 | [00-overview.md](00-overview.md) | Entry-point index: reading order, conventions, status at a glance |
| 01 | [01-user-guide.md](01-user-guide.md) | End-user walkthrough: install, import clip, grade, export |
| 02 | [02-developer-guide.md](02-developer-guide.md) | Contributor and packager guide: clone, build, test, contribute |
| 03 | [03-technical-specification.md](03-technical-specification.md) | Full architecture: modules, data flow, APIs, threading, telemetry |
| 04 | [04-external-auditor-guide.md](04-external-auditor-guide.md) | **This document** — independent-review self-contained guide |
| 10 | [10-build-windows.md](10-build-windows.md) | Windows build specifics: MinGW / Qt Creator / `windeployqt` |
| 11 | [11-build-macos-linux.md](11-build-macos-linux.md) | macOS and Linux build specifics: Homebrew, `apt`, Qt sources, AppImage |
| 12 | [12-gpu-viewport-architecture.md](12-gpu-viewport-architecture.md) | Experimental GPU presenter and debayer seams |
| 13 | [13-testing-infrastructure.md](13-testing-infrastructure.md) | Test-suite structure: `console_tests`, `pipeline_tests`, `gui_tests`, fixtures |
| 14 | [14-performance-benchmarking.md](14-performance-benchmarking.md) | `perf_tests`, `--profile-playback`, baselines |
| 15 | [15-test-fixtures.md](15-test-fixtures.md) | Committed MLVs, receipts, golden manifests |
| 16 | [16-fuzz-testing.md](16-fuzz-testing.md) | Opt-in fuzz targets: `fuzz_receipt_loader`, `fuzz_lj92`, `fuzz_mlv_open` |
| — | [diagrams/](diagrams/) | ASCII + Mermaid diagrams referenced from 00–04 |

Non-docs tree documentation — kept in place because they are tightly
coupled to the directory they describe, or belong to an upstream vendored
project:

| Path | Scope |
|---|---|
| [README.md](../README.md) | Project overview + feature list + compile instructions |
| [AGENTS.md](../AGENTS.md) | Workspace-policy for the agent-assisted development workflow (not product) |
| [LICENSE](../LICENSE) | GPL-3.0 (project-wide license) |
| [tests/README.md](../tests/README.md) | 31-line directory map; canonical test-suite content lives in `docs/13`–`docs/16` |
| [tests/fuzz/README.md](../tests/fuzz/README.md) | Fuzz harness how-to |
| [tests/perf/README.md](../tests/perf/README.md) | Perf harness how-to |
| [tests/fixtures/README.md](../tests/fixtures/README.md) | Fixture directory placeholder |
| [tests/fixtures/clips/README.md](../tests/fixtures/clips/README.md) | Fixture clips inventory |
| [platform/qt/GPUDisplayFoundation.md](../platform/qt/GPUDisplayFoundation.md) | In-tree architectural note for the GPU presenter (migrated into `docs/12`) |
| [platform/cocoa/README.md](../platform/cocoa/README.md) | Deprecated Cocoa app notes |
| [platform/binning_test/README.md](../platform/binning_test/README.md) | Binning experiment README |
| [src/librtprocess/README.md](../src/librtprocess/README.md) | Upstream librtprocess README (vendored; GPL-3.0) |
| [src/processing/rbfilter/README.md](../src/processing/rbfilter/README.md) | Upstream rbfilter README (vendored; MIT) |
| [platform/qt/avir/README.md](../platform/qt/avir/README.md) | Upstream AVIR README (vendored; MIT) |
| [platform/qt/maddy/README.md](../platform/qt/maddy/README.md) | Upstream maddy README (vendored; MIT) |

---

**End of guide.** A reviewer completing §3 (confirm what you're looking
at), §8 (build from scratch), §9 (tests from scratch), §10 (what CI
gates), §11 (independent feature verification), §15 (traceability), and
§16 (citation freshness) has a defensible ground-truth picture of MLV App
at `1.15.0.0`. Findings route through §17. Open questions — especially
on Linux/macOS CI gating, minidump destinations, and bundled FFmpeg
provenance — are captured in §13.
