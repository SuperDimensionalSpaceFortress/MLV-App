# MLV App — Developer Guide

This is the contributor-facing guide: how to clone the tree, set up a build on
each supported OS, run the test suites, and land a change. A persistent reader
with the prerequisites in [§3](#3-prerequisites) installed should be able to go
from clone to a passing-CI pull request in under four hours using only this
document — no source reading, no chasing other docs.

Pair it with [03-technical-specification.md](03-technical-specification.md) when
you need the architectural detail behind the engine.

---

## 1. Who this guide is for

- **Contributors** adding features, fixing bugs, or refactoring the engine and
  Qt GUI layer.
- **Packagers** producing Windows `.zip`, macOS `.dmg`, or Linux `.AppImage`
  artifacts.
- **Downstream integrators** embedding the `src/` engine into headless
  pipelines, scripting hosts, or custom tooling that reuses the MLV decode /
  processing / DNG writer stack.

End users who only want to install a pre-built release should read
[01-user-guide.md](01-user-guide.md) instead — this document assumes you have
a compiler, Qt, and git on your machine and are comfortable on the command
line. If you are completely new, read [00-overview.md](00-overview.md) first
for the 5-minute tour, then come back here.

**This guide is the canonical index.** Where it defers to a 10-series doc
(`10-build-windows.md`, `11-build-macos-linux.md`,
`12-gpu-viewport-architecture.md`, `13-testing-infrastructure.md`,
`14-performance-benchmarking.md`, `15-test-fixtures.md`, `16-fuzz-testing.md`)
the linked file is authoritative; this document carries the minimum needed
to build, test, and contribute without leaving the page.

---

## 2. Source layout

MLV App is a cross-platform Qt 5/6 application. The `src/` tree contains the
portable C/C++ engine (MLV I/O, low-level raw corrections, demosaic, processing
pipeline, DNG writer, batch CLI glue). The `platform/` tree contains the Qt
GUI, the deprecated Cocoa GUI, and optional Blender/binning utilities. Tests,
fixtures, installers, and CI live at the repo root.

| Directory | Contents |
|-----------|----------|
| `src/mlv/` | MLV file I/O, frame indexing, raw caching, prefetch worker; subfolders `llrawproc/` (focus pixels, bad pixels, dual ISO, stripes, chroma smoothing), `liblj92/` (lossless JPEG codec), `mcraw/` (MCRAW format), `camid/` (camera ID tables). |
| `src/processing/` | Nine-stage post-demosaic pipeline (WB, exposure, curves, denoise, sharpen, gamma…). Houses vendored `rbfilter/`, `denoiser/`, `interpolation/`, `cafilter/`, `sobel/`, `tinyexpr/`, and the `filter/genann` neural-net film-emulation filters. |
| `src/debayer/` | Demosaic dispatcher (None, Basic/Bilinear, AHD, AMaZe, DCB, RCD, IGV, LMMSE). |
| `src/librtprocess/` | Vendored high-quality demosaic library (GPL3). |
| `src/dng/` | DNG TIFF writer + bit packing + LJPEG encode. |
| `src/batch/` | Headless CLI: `BatchRunner`, `BatchContext`, `BatchLogger`, `MlvTrim`, `ReceiptLoader`, `ReceiptApplier`. |
| `src/matrix/`, `src/ca_correct/`, `src/debug/`, `src/icon/` | 3x3 matrix utilities, CA correction helpers, thread-local telemetry/stage timing, application icon source. |
| `platform/qt/` | Primary Qt 5/6 GUI (~80 `.cpp` files), build project `MLVApp.pro`, vendored `avir/` (image resizing) and `maddy/` (in-app Markdown viewer), `FFmpeg/` and `raw2mlv/` prebuilt codec bundles. |
| `platform/cocoa/` | Deprecated Objective-C/Cocoa GUI; kept building for historical reference. |
| `platform/mlv_blender/`, `platform/inning_test/` | Optional Blender integration; downscaling test harness. |
| `tests/` | `console/`, `pipeline/`, `gui/`, `perf/`, `alloc/`, `fuzz/`, shared `common/` helpers, checked-in `fixtures/` (2 golden MLVs + receipts + hash goldens). Per-suite **build directories** (`build-*`) are created on demand and are `.gitignore`d. |
| `.github/workflows/` | GitHub Actions: `tests.yml`, `Windows.yml`, `Linux.yml`, `macOS-Intel.yml`, `macOS-Arm64.yml`. |
| `.claude/` | **Tracked** curated analysis notes (`analysis/`, `ANALYSIS_LOG.md`, `profiling/`). Read freely; do not create new files here. |
| `.claude-state/` | **`.gitignore`d** scratch (profiling JSON, throwaway logs, helper scripts you do not want to commit). |
| `osx_installer/` | `BuildInstaller.sh` — wraps `create-dmg` to produce the signed macOS installer after `macdeployqt`. |
| `pixel_maps/` | Camera-specific focus-pixel maps (`.fpm`) consumed by `src/mlv/llrawproc/`. |
| `receipts/` | Example `.marxml` receipts (`CanonLog.marxml`, `FastAlexaRCD.marxml`, `FastProxy.marxml`, etc.) usable as starting points for grades and proxies. |
| `docs/` | Documentation tree. Index files (`00-overview.md` … `04-external-auditor-guide.md`) plus the 10-series deep dives (`10-build-windows.md`, `11-build-macos-linux.md`, `12-gpu-viewport-architecture.md`, `13-testing-infrastructure.md`, `14-performance-benchmarking.md`, `15-test-fixtures.md`, `16-fuzz-testing.md`) and `diagrams/`. |

All vendored dependencies (`rbfilter/`, `denoiser/`, `librtprocess/`, `avir/`,
`maddy/`, `tinyexpr/`, `liblj92/`) are copied into the tree directly. There
are **no git submodules and no Git LFS objects** — a plain `git clone`
produces a buildable working copy.

The `src/mlv_include.h` umbrella header is the single entry point when
embedding the engine; it pulls in `video_mlv.h`, `audio_mlv.h`,
`raw_processing.h`, `debayer.h`, `llrawproc.h`, and `dng.h`. See
[03-technical-specification.md](03-technical-specification.md) for a deep dive
into every subsystem.

---

## 3. Prerequisites

All four supported targets share the same conceptual requirement: a C/C++
toolchain, Qt 5 or Qt 6, and `git`. The deployment helpers and OS plumbing
differ. Follow the per-OS list.

**Resource baseline (all platforms).** ~5 GB free disk for a checkout plus
one Qt build (toolchain installs add 3–6 GB). Builds peak at ~4 GB RAM with
`-j8`; drop to `-j2` on 8 GB machines.

**Qt baseline.** Both Qt 5 and Qt 6 are supported. Minimums: **Qt 5.13.2 /
Qt 6.5+** on Windows (older Qt 5s lack the MinGW bundle Chocolatey ships),
**Qt 5.6 / Qt 6.4+** on macOS and Linux. CI builds Qt 6.10.2 + MinGW 13.1
on `tests.yml` and Qt 5.x on the release workflows; both work, and §5 pins
the per-OS recipe.

### 3.1 Windows

- **Qt**: Qt 6.10.2 with MinGW 13.1 (recommended — used by `tests.yml` and the
  rest of this guide), or Qt 5.15.2 with MinGW 8.1 (used by the
  `Windows.yml` release workflow). Install via
  [`aqtinstall`](https://github.com/miurahr/aqtinstall) (Qt 6 path, see §5.2)
  or Chocolatey (`choco install qt5-default`, Qt 5 path).
- **Compiler**: MinGW 13.1 (recommended) or MinGW 8.1+ from Chocolatey. Do not
  mix multiple MinGW runtimes; see
  [Windows runtime rules](#6-windows-runtime-rules-critical) below.
- **Python 3.10–3.12**: required to drive `aqtinstall`. Install from
  [python.org](https://www.python.org/downloads/) **and tick "Add Python to
  PATH"** during install (the unconfigured `python` alias on Windows 10/11
  opens the Microsoft Store stub instead of running Python). Choco/winget
  alternatives: `choco install python --version=3.12.7` or
  `winget install Python.Python.3.12`. Verify in a fresh shell that
  `python --version` prints `Python 3.10.x`+ and does *not* open the Store.
- **7-Zip**: required to unpack `platform/qt/FFmpeg/ffmpegWin64.zip` and
  `platform/qt/raw2mlv/raw2mlvWin64.zip`. The default 7-Zip installer does
  **not** add `7z.exe` to PATH. Get it on PATH with `choco install
  7zip.commandline`, or `winget install 7zip.7zip` plus a manual PATH append
  for `C:\Program Files\7-Zip\`, or call it via the absolute path
  `& 'C:\Program Files\7-Zip\7z.exe'`.
- **Chocolatey** (optional but recommended for the Qt 5 path):
  `choco install qt5-default openssl` is the shortcut used by
  `.github/workflows/Windows.yml`.
- **git**: required for clone and for the build-time SHA capture. The `.pro`
  captures `git rev-parse HEAD` at qmake time
  (`platform/qt/MLVApp.pro:106-112`). Tarball builds and `git`-unresolvable
  trees fall back to `"unknown"`. The captured SHA reflects the latest commit
  whether the tree is clean or dirty — there is no `+dirty` suffix, so for
  trustworthy crash forensics (§15.2), build from a clean tree.

### 3.2 macOS (Intel)

- **Xcode Command Line Tools** (`xcode-select --install`).
- **Homebrew** — required for OpenMP, OpenSSL, and `qt@5`. Install once if
  needed:
  ```bash
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  ```
- **Qt**: Qt 5 (5.6 – 5.15.2) or Qt 6 (6.4+). Install dependencies:
  ```bash
  brew install llvm qt@5 openssl pcre2 harfbuzz freetype
  ```
  > **Formula name.** Modern Homebrew names the Qt 5 formula **`qt@5`**, not
  > `qt5`. CI uses `brew install qt5` (Homebrew aliases it), but the qmake
  > binary always lands at `/usr/local/opt/qt@5/bin/qmake`. Use `qt@5`
  > everywhere in your shell to avoid alias confusion.

  The `llvm` formula provides OpenMP (`libomp`) — Apple's `clang` does not.
  The `.pro` pins `QMAKE_CC = /usr/local/opt/llvm/bin/clang` and links
  `-lomp -lssl` on x86_64.
- **Verify** before building: `/usr/local/opt/qt@5/bin/qmake -v` should
  print `Using Qt version 5.15.x in /usr/local/opt/qt@5/lib`.
- **Deployment target**: `QMAKE_MACOSX_DEPLOYMENT_TARGET = 10.8`
  (`platform/qt/MLVApp.pro:64`).

### 3.3 macOS (Apple Silicon)

Two supported paths; the **Qt 6** path is recommended.

**Preferred: Qt 5 or Qt 6 from Homebrew** — same recipe as Intel:
```bash
brew install llvm qt@5 openssl pcre2 harfbuzz freetype     # Qt 5
# or for Qt 6:
brew install llvm qt openssl pcre2 harfbuzz freetype
```
The `.pro` file detects `QT_ARCH = arm64` automatically and switches the
toolchain paths to `/opt/homebrew/opt/llvm/`
(`platform/qt/MLVApp.pro:67-77`). **Nothing in `MLVApp.pro` needs to be
edited** for the Homebrew Qt path — the Apple Silicon block is *already
active*. Verify with `/opt/homebrew/opt/qt@5/bin/qmake -v` and
`file /opt/homebrew/opt/qt@5/bin/qmake` (must report `Mach-O 64-bit
executable arm64`).

**Legacy: Qt 5 from source** — required only if you cannot use Homebrew Qt.
Budget **~1 hour** for the Qt build itself on Apple Silicon (`make -j15`),
plus 10–20 minutes for dependency installs. If you only need a debug build,
prefer the Homebrew Qt 5 path above; Qt-from-source pays off only when you
need a Qt revision Homebrew has dropped or when bisecting a Qt regression:

1. Install Command Line Tools (SDK 11.3 is the known-good build).
2. Install Homebrew, then `brew install llvm@11 pcre2 harfbuzz freetype`.
3. Install Qt Creator: `brew install --cask qt-creator`.
4. Clone Qt 5 and build ARM64 release:
   ```bash
   git clone git://code.qt.io/qt/qt5.git
   cd qt5 && git checkout 5.15 && ./init-repository
   cd .. && mkdir qt5-5.15-macOS-release && cd qt5-5.15-macOS-release
   ../qt5/configure -release -prefix ./qtbase -nomake examples -nomake tests \
       QMAKE_APPLE_DEVICE_ARCHS=arm64 -opensource -confirm-license
   make -j15
   ```
5. Configure a Qt Creator kit pointing at this compiled Qt and the `llvm@11`
   toolchain. **No edit to `MLVApp.pro` is required** — the
   `equals(QT_ARCH, arm64)` block at `platform/qt/MLVApp.pro:67-77` is already
   active and will engage as soon as `qmake` reports `QT_ARCH = arm64`.
   Confirm by running `qmake -query QT_ARCH` inside your build directory after
   running `qmake`. If it reports anything other than `arm64`, you picked the
   wrong qmake — re-check your kit.

**Deployment target** (ARM64): `QMAKE_MACOSX_DEPLOYMENT_TARGET = 11.7`
(`platform/qt/MLVApp.pro:75`).

### 3.4 Linux

Install the same package list CI uses — verbatim from
`.github/workflows/Linux.yml:22-26`:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends \
    make g++ qt5-qmake qtbase5-dev qtmultimedia5-dev \
    libqt5multimedia5 libqt5multimedia5-plugins \
    libqt5opengl5-dev libqt5designer5 libqt5svg5-dev \
    libfuse2 libxkbcommon-x11-0 appstream
```

- **Qt**: Qt 5 (5.6 – 5.15.2) or Qt 6 (6.4+). On Ubuntu 22.04 the system Qt 5
  packages above are the simplest path.
- **`libfuse2`** is required only by the AppImage runtime — drop it if you do
  not plan to test the bundled AppImage and your distribution does not
  pre-install it.
- **`libxkbcommon-x11-0`** is required by Qt at runtime on headless CI runners
  (offscreen platform plugin uses it).

For other distributions, see the community tutorial in the README
([sternenkarten.com](https://sternenkarten.com/tutorial-englisch/)).

---

## 4. Cloning

For **read-only checkouts and packagers**, clone upstream:

```bash
git clone https://github.com/ilia3101/MLV-App.git
cd MLV-App
```

For **contributors who intend to submit a PR** (most of you), fork first then
clone your fork. This avoids a confusing `git push → 403` later:

```bash
gh repo fork ilia3101/MLV-App --clone --remote
cd MLV-App
git remote -v
# origin    https://github.com/<your-handle>/MLV-App.git (push)
# upstream  https://github.com/ilia3101/MLV-App.git (push)
```

Without `gh`, do it manually:

```bash
# 1) Fork ilia3101/MLV-App in the GitHub UI.
git clone https://github.com/<your-handle>/MLV-App.git
cd MLV-App
git remote add upstream https://github.com/ilia3101/MLV-App.git
git fetch upstream
```

The repository has **no submodules and no Git LFS objects** — the clone you
just made is complete. The two large MLV golden fixtures under
`tests/fixtures/clips/` are stored directly in git history.

The repository uses two curated directories with different write policies:

- `.claude/` is **tracked and curated**. Anything committed there (analysis
  notes, worktrees, CI helper scripts) was put there deliberately. **Read
  freely** — every file is checked out by the clone above. Do not *create new*
  files under `.claude/`; editing existing tracked notes under
  `.claude/analysis/` is fine. New agent scratch, profiling runs, or throwaway
  logs go to…
- `.claude-state/` — this directory is `.gitignore`d (see
  [`.gitignore` line 39](../.gitignore)) and is the correct home for all
  ephemeral agent/developer scratch (profiling artifacts, smoke-test logs,
  helper scripts). Files placed here will not appear in `git status`.

See [AGENTS.md](../AGENTS.md) for the full policy.

---

## 5. Building

The same `.pro` file (`platform/qt/MLVApp.pro`) drives every OS. On any
platform the mantra is:

1. Open `platform/qt/MLVApp.pro` in Qt Creator and hit **Build & Run**, or
2. From the command line, run `qmake MLVApp.pro` in a build directory, then
   `make` (or `mingw32-make` on Windows) with the appropriate `-j` count.

### 5.1 Windows — Qt Creator

1. Install Qt + MinGW (see [§3.1 Windows](#31-windows)).
2. Launch Qt Creator. On first run, configure a **Kit** that pairs the
   installed Qt version with its matching MinGW compiler (for example
   `Qt 6.10.2 MinGW 64-bit` + `MinGW 13.1 64-bit`). Do **not** mix different
   MinGW runtimes.
3. Open `platform/qt/MLVApp.pro`.
4. Pick **Release** (default), and press **Build & Run**.

### 5.2 Windows — command line (mirrors CI)

This is the exact recipe the `tests.yml` workflow runs
(`.github/workflows/tests.yml:38-64`) with `aqtinstall`:

```powershell
# 0. PREREQS: Python on PATH (see §3.1) and 7-Zip on PATH (or use absolute
#    7z path below). Verify:
python --version           # should print Python 3.10.x or newer
7z                         # should print 7-Zip banner

# 1. Install aqtinstall (once)
python -m pip install --upgrade pip
python -m pip install "aqtinstall==3.3.*"

# 2. Install Qt 6.10.2 and the matching MinGW 13.1 toolchain
$QT_OUTPUT_DIR = "C:\Qt"
python -m aqt install-qt --outputdir $QT_OUTPUT_DIR windows desktop 6.10.2 win64_mingw
python -m aqt install-tool --outputdir $QT_OUTPUT_DIR windows desktop tools_mingw1310 qt.tools.win64_mingw1310

# 3. Put Qt and MinGW on PATH for this shell
$env:QT_ROOT_DIR = "$QT_OUTPUT_DIR\6.10.2\mingw_64"
$env:MINGW_ROOT  = "$QT_OUTPUT_DIR\Tools\mingw1310_64"
$env:PATH = "$env:QT_ROOT_DIR\bin;$env:MINGW_ROOT\bin;$env:PATH"

# 4. Build out of tree
New-Item -ItemType Directory -Force platform\qt\build | Out-Null
Push-Location platform\qt\build
& "$env:QT_ROOT_DIR\bin\qmake.exe" ..\MLVApp.pro
& "$env:MINGW_ROOT\bin\mingw32-make.exe" -j2
Pop-Location
```

After the build completes, deploy Qt runtime dependencies next to the exe and
unpack the bundled FFmpeg + raw2mlv:

```powershell
Push-Location platform\qt\build\release
& "$env:QT_ROOT_DIR\bin\windeployqt.exe" MLVApp.exe --release --no-translations --no-compiler-runtime

# Unpack the bundled ffmpeg + raw2mlv binaries next to MLVApp.exe.
# (If 7z is not on PATH, replace `7z` with `& 'C:\Program Files\7-Zip\7z.exe'`.)
7z x ..\..\FFmpeg\ffmpegWin64.zip
7z x ..\..\raw2mlv\raw2mlvWin64.zip
Pop-Location
```

The release workflow (`.github/workflows/Windows.yml:32-57`) follows the same
sequence with Chocolatey-installed Qt 5.15.2 + MinGW 8.1, and additionally
copies `libgomp-1.dll` and the OpenSSL runtime into the release folder. If you
are using the Chocolatey path, see
[10-build-windows.md](10-build-windows.md) for the full Qt 5 walkthrough.

#### Build verification checklist (Windows)

- [ ] `platform\qt\build\release\MLVApp.exe` exists and is non-empty.
- [ ] `Qt6Core.dll`, `Qt6Gui.dll`, `Qt6Widgets.dll`, `Qt6Network.dll`,
      `platforms\qwindows.dll`, `ffmpeg.exe`, `raw2mlv.exe` are present
      next to the exe.
- [ ] `MLVApp.exe --help` prints CLI usage and exits with code 0.
- [ ] Launch via the canonical PowerShell wrapper in [§6](#6-windows-runtime-rules-critical)
      — splash + main window appear within 5 seconds, no `Qt6Core.dll`
      modal.
- [ ] `console_tests --check-golden` runs green (smoke test;
      [§13.1](#131-console_tests---check-golden)).

### 5.3 macOS — Intel

From Qt Creator, open `platform/qt/MLVApp.pro` and build. From the command
line (matches `.github/workflows/macOS-Intel.yml:26-35`):

```bash
mkdir -p platform/build && cd platform/build
/usr/local/opt/qt@5/bin/qmake -r ../qt/MLVApp.pro
make -j8
/usr/local/opt/qt@5/bin/macdeployqt "MLV App.app" -dmg
```

`macdeployqt` copies the Qt frameworks into the `.app` bundle and builds the
`.dmg`. The `.pro`'s `QMAKE_POST_LINK` step also unpacks
`platform/qt/FFmpeg/ffmpegOSX.zip` into
`MLV App.app/Contents/MacOS/` so shelled-out exports work offline (see
`platform/qt/MLVApp.pro:486-491`).

For a signed installer, run `osx_installer/BuildInstaller.sh` after copying
`MLV App.app` into `osx_installer/App/` — it wraps
[`create-dmg`](https://github.com/andreyvit/create-dmg) with the repo's
window/layout template.

#### Build verification checklist (macOS Intel)

- [ ] `MLV App.app/Contents/MacOS/MLV App` is executable.
- [ ] `MLV App.app/Contents/MacOS/ffmpeg` and `.../raw2mlv` exist
      (post-link unzip).
- [ ] `MLV App.app/Contents/Frameworks/QtCore.framework/` exists
      (macdeployqt staged it).
- [ ] `open "platform/build/MLV App.app"` shows the GUI within 5 seconds.
- [ ] `"./platform/build/MLV App.app/Contents/MacOS/MLV App" --help` exits 0.
- [ ] `console_tests --check-golden` runs green.

### 5.4 macOS — Apple Silicon

If you are on **Qt 5 or Qt 6 from Homebrew**, the recipe is identical to Intel
— `qmake` detects `QT_ARCH = arm64` and picks the `/opt/homebrew` toolchain
automatically (`platform/qt/MLVApp.pro:67-77`). **No `.pro` edit required.**

Command line from `.github/workflows/macOS-Arm64.yml:26-35`:

```bash
mkdir -p platform/build && cd platform/build
/opt/homebrew/opt/qt@5/bin/qmake -r ../qt/MLVApp.pro
make -j8
/opt/homebrew/opt/qt@5/bin/macdeployqt "MLV App.app" -dmg
```

If you are on **Qt 5 from source** (the legacy path in §3.3), the same recipe
applies — substitute your custom-built `qmake`/`macdeployqt` for the Homebrew
ones above. Confirm `qmake -query QT_ARCH` reports `arm64` before running
`make`.

For reference, the Apple Silicon block in `MLVApp.pro` (lines 67–77, **already
active in master**) is exactly:

```qmake
#Qt5 on Apple Silicon with openMP: install llvm and openssl via brew, build Qt5 from source
equals(QT_ARCH, arm64) {
    QMAKE_CC = /opt/homebrew/opt/llvm/bin/clang
    QMAKE_CXX = /opt/homebrew/opt/llvm/bin/clang++
    QMAKE_LINK = /opt/homebrew/opt/llvm/bin/clang++
    QMAKE_CFLAGS += -fopenmp -ftree-vectorize
    QMAKE_CXXFLAGS += -fopenmp -std=c++15 -ftree-vectorize
    INCLUDEPATH += -I/opt/homebrew/opt/llvm/include
    LIBS += -L/opt/homebrew/opt/llvm/lib -lomp -L/opt/homebrew/opt/llvm/lib/unwind -lunwind -L/opt/homebrew/opt/openssl/lib -lssl -L/opt/homebrew/opt/llvm/lib/c++ -lc++ -lc++abi
    QMAKE_MACOSX_DEPLOYMENT_TARGET = 11.7
    QMAKE_APPLE_DEVICE_ARCHS = arm64
}
```

Earlier doc revisions told readers to "uncomment" this block — that is no
longer true and has not been for several releases.

#### Build verification checklist (macOS Apple Silicon)

- [ ] `qmake -query QT_ARCH` (run inside `platform/build/`) prints `arm64`.
- [ ] `file "platform/build/MLV App.app/Contents/MacOS/MLV App"` reports
      `Mach-O 64-bit executable arm64` (no `x86_64` slice).
- [ ] `otool -L "platform/build/MLV App.app/Contents/MacOS/MLV App" |
      head -5` shows `@rpath/QtCore.framework/...` references.
- [ ] `platform/build/MLV App.app/Contents/MacOS/ffmpeg` and `.../raw2mlv`
      exist.
- [ ] `open "platform/build/MLV App.app"` launches the GUI.
- [ ] `console_tests --check-golden` runs green.

### 5.5 Linux

From README + `.github/workflows/Linux.yml:36-42`:

```bash
cd platform/qt
qmake MLVApp.pro
make -j"$(nproc)"
ls -al mlvapp || ls -al release/mlvapp   # find the binary
./mlvapp                                  # in-tree build
# or:
./release/mlvapp                          # if your kit produces a release/ subdir
```

In an in-tree `qmake; make` build the binary lands directly at
`platform/qt/mlvapp` (lowercase target name set at `MLVApp.pro:22`). Shadow
builds (Qt Creator default) put it under your build directory. If
`./mlvapp` reports `No such file or directory`, run `find . -maxdepth 3 -name
mlvapp -type f` to locate it and adapt the command.

To produce an AppImage, use the CI recipe (extract the bundled ffmpeg +
raw2mlv, copy Qt multimedia plugins into an `image/usr/` layout, then run
[`linuxdeploy`](https://github.com/linuxdeploy/linuxdeploy) with the Qt and
AppImage plugins):

```bash
mkdir image && cd image
cp ../../qt/RetinaIMG/MLVAPP.png . && cp ../../qt/mlvapp.desktop .
mkdir -p usr/bin
tar -C . -xvJf ../../qt/FFmpeg/ffmpegLinux.tar.xz --strip=1 --wildcards '*/ffmpeg'
tar -C . -xvJf ../../qt/raw2mlv/raw2mlvLinux.tar.xz --strip=1 --wildcards '*/raw2mlv'
chmod a+x ffmpeg raw2mlv && mv ffmpeg raw2mlv usr/bin/ && cd ..
linuxdeploy-x86_64.AppImage --desktop-file=image/mlvapp.desktop \
    --executable=mlvapp --appdir=image --plugin=qt --output=appimage \
    --icon-file=image/MLVAPP.png
```

#### Build verification checklist (Linux)

- [ ] `platform/qt/mlvapp` (or `platform/qt/release/mlvapp`) exists,
      executable, ELF 64-bit.
- [ ] `ldd platform/qt/mlvapp | grep -i qt` lists `libQt5Core`, `libQt5Gui`,
      `libQt5Widgets`, `libQt5Multimedia`.
- [ ] `./mlvapp --help` exits 0 (after `cd platform/qt`).
- [ ] If you built the AppImage: `./MLVApp-*.AppImage --help` exits 0.
- [ ] `console_tests --check-golden` runs green.

### 5.6 Cocoa app (deprecated — skip on first contribution)

> **Skip this section unless you are explicitly working on the Cocoa
> front-end.** It is here for completeness; new work should target the Qt
> build above.

The original Cocoa/Objective-C macOS front-end still builds (`cd platform/cocoa
&& make app -j4`) but is unmaintained ("very very deprecated" per README).
Use the Qt build for anything new; bug reports against the Cocoa app are not
prioritised. See [`platform/cocoa/README.md`](../platform/cocoa/README.md)
for the historical build/run notes.

---

## 6. Windows runtime rules (critical)

Windows launches frequently fail with `Qt6Core.dll` / `Qt6Network.dll`
entry-point errors or GL crashes unless the Qt runtime, the MinGW runtime, and
the exe directory are all on `PATH` in the right order. Follow these rules
(reproduced from [AGENTS.md](../AGENTS.md) §"Runtime Execution Rules
(Windows)"):

1. Before launching, force `QT_OPENGL=desktop`. This skips the ANGLE wrapper
   and uses the system OpenGL driver — ANGLE is unstable on several Windows
   Qt builds and triggers silent crashes in the GPU viewport.
2. Prepend to `PATH`, in this order:
   1. `C:\Qt\6.10.2\mingw_64\bin`
   2. `C:\Qt\Tools\mingw1310_64\bin`
   3. the directory that contains `MLVApp.exe`
3. Launch from the exe's directory (or use absolute paths).
4. Do **not** mix `C:\Qt\6.10.2\mingw_64` runtime binaries with a different Qt
   runtime in the same launch session.
5. If Windows reports missing `Qt6Core.dll` / `Qt6Network.dll` or an
   entry-point lookup failure, `Set-Location` to the directory that contains
   `MLVApp.exe` and re-run
   `C:\Qt\6.10.2\mingw_64\bin\windeployqt.exe MLVApp.exe --release --no-translations --no-compiler-runtime`.

The canonical PowerShell launch:

```powershell
Set-Location <build-root>\release
$env:QT_OPENGL = 'desktop'
$env:PATH = 'C:\Qt\6.10.2\mingw_64\bin;C:\Qt\Tools\mingw1310_64\bin;' `
    + (Get-Location) + ';' + $env:PATH
.\MLVApp.exe
```

### 6.1 Optional helper script

For repeatable launches (especially for profiling or headless test runs), the
analysis notes reference `.claude-state\scripts\run-mlvapp.ps1`. **That file
is not checked into git** — `.claude-state/` is gitignored. The canonical
PowerShell launch above does the same job in 4 lines and is the supported
mechanism. If you want to script the launch, copy the 4 lines into your own
`.ps1` (gitignored under `.claude-state/`) and parameterise `$exe`,
`$QtBin`, and `$MingwBin` to taste.

---

## 7. AVX2 opt-in

By default, MLV App emits **no AVX2 code** so binaries run on every x86 CPU
the project supports. You can opt in to an AVX2 fast path at `qmake` time;
the runtime then dispatches to it via `processingFastPathAvx2Active()` only on
CPUs that report AVX2 support. Packagers shipping a single binary should
leave AVX2 off; developers who only target their own machine, or build both a
baseline and an AVX2-optimised binary, can enable it.

Enable at `qmake` time by either:

```bash
qmake CONFIG+=mlvapp_enable_avx2 MLVApp.pro
# or
MLVAPP_ENABLE_AVX2=1 qmake MLVApp.pro
```

The opt-in logic lives in
[`platform/qt/avx_optin.pri`](../platform/qt/avx_optin.pri). When requested on
an x86 target it appends `-mavx2` to the C/C++ flags and defines
`MLVAPP_BUILD_AVX=1 MLVAPP_BUILD_AVX2=1`. On non-x86 targets (Apple Silicon,
ARM64 Linux) it warns and ignores the request.

`console_tests` includes a local AVX-parity check (`tests/console/
test_avx_golden.cpp`) that builds a small helper binary
(`tests/console/avx_parity_helper.{cpp,pro}`) twice — once with the default
flags and once with `MLVAPP_ENABLE_AVX=1` — and asserts both configurations
render identical frame hashes against the checked-in Dual ISO fixtures.

---

## 8. FFmpeg and raw2mlv bundles

MLV App shells out to `ffmpeg` for ProRes/H.264/H.265/DNxHD/JPEG2000/CineForm
encoding, and to `raw2mlv` for the MLV writer path. Precompiled binaries live
under `platform/qt/FFmpeg/` and `platform/qt/raw2mlv/`:

| Platform | FFmpeg archive | raw2mlv archive |
|----------|----------------|-----------------|
| Windows 64 | `ffmpegWin64.zip` | `raw2mlvWin64.zip` |
| Windows 32 | `ffmpegWin32.zip` | `raw2mlvWin32.zip` |
| macOS Intel | `ffmpegOSX.zip` | `raw2mlvOSX.zip` |
| macOS Apple Silicon | `ffmpegOSX.zip` | `raw2mlvMacOsArm.zip` |
| Linux | `ffmpegLinux.tar.xz` | `raw2mlvLinux.tar.xz` |

Unpack rules:

- **Windows**: unpack `ffmpegWin64.zip` and `raw2mlvWin64.zip` next to
  `MLVApp.exe` in your build directory (e.g. `platform\qt\build\release\`).
  `.github/workflows/Windows.yml:48-57` uses 7-Zip for this.
- **macOS**: the `.pro`'s `QMAKE_POST_LINK` step
  (`platform/qt/MLVApp.pro:486-491`) unpacks `ffmpegOSX.zip` and the
  arch-correct `raw2mlv` zip into the `.app` bundle at build time — no manual
  step needed.
- **Linux**: the AppImage workflow unpacks `ffmpegLinux.tar.xz` and
  `raw2mlvLinux.tar.xz` into `image/usr/bin/` before invoking `linuxdeploy`
  (see §5.5 above).

**Licensing for redistribution**: the bundled ffmpeg and raw2mlv binaries are
upstream FFmpeg and Magic Lantern raw2mlv builds. FFmpeg is LGPL by default
with optional GPL components; raw2mlv is GPL-licensed. Packagers redistributing
MLV App must mirror those upstream notices alongside the binary
(`ffmpeg-license.txt`, `raw2mlv-LICENSE`, etc., as shipped in the archives).
See the upstream projects for the exact text.

---

## 9. Running the app

### 9.1 GUI mode (default)

Launch `MLVApp.exe` (Windows), `MLV App.app` (macOS), or `./mlvapp` (Linux);
with no arguments the app opens the main session window.

### 9.2 CLI subcommands

`platform/qt/main.cpp:26-62` scans `argv` before `QApplication` construction
and dispatches to one of three headless modes if the relevant flag is present.

#### `--batch` — headless CDNG export

Drives a full session through `BatchRunner::run()` in `src/batch/`:

```bash
MLVApp --batch --input <file-or-folder> --output <dir> [--receipt <file.marxml>] [--default-receipt] [--cdng-codec <uncompressed|lossless|fast-pass>] [--skip-errors] [--resume] [--max-frames <count>] [--log <file>] [--verbose]
```

`--input`/`-i` is a single `.mlv` or a folder (recursed for MLVs);
`--output`/`-o` is the DNG sequence output dir; `--receipt`/`-r` applies a
`.marxml` to every clip; `--default-receipt` uses the GUI-configured default;
`--skip-errors` continues past corrupt frames; `--resume` skips clips whose
DNG output already matches the expected frame count; `--max-frames` clamps each
clip to a bounded export window after receipt cut-in/cut-out resolution.
`--cdng-codec` selects the existing CDNG output mode for headless export:
`uncompressed` (default), `lossless`, or `fast-pass`. Exit codes: `0` success,
`2` missing/invalid arguments, non-zero on runtime errors.

When the effective receipt has `lookAssistEnabled`, batch export copies the
receipt per input clip and runs headless Look Assist before writing the DNG
sequence. This produces clip-local DNG defaults for raw black/white,
`BaselineExposure`, `AsShotNeutral`, and related raw-fix/chroma-smooth state.
The DNG frame data remains full-quality raw output; real-time playback scale
reductions do not apply to this export path. Disable the headless auto look by
turning off `lookAssistEnabled` in the receipt or setting
`MLVAPP_NO_LOOK_ASSIST=1`.

When `MLVAPP_GPU_EXPORT=1` causes the CUDA shadow export path to run and the
loaded backend supports the optional VRAM query, batch stdout includes an
additive line such as `[BATCH] GPU <clip> frame=<n> rc=<rc> replaced=<0|1> vramAllocatedMB=<mb>`.
The reported MB is the backend's per-worker VRAM
working-set budget (tracked device buffers plus a conservative CUDA context
reserve), intended for external batch schedulers; CPU-only and older-DLL runs do
not emit the line.

#### `--trim-mlv` — cut a clip

Runs `src/batch/MlvTrim` to write a new `.mlv` containing a frame range from
the input — useful for test fixtures or short repros:

```bash
MLVApp --trim-mlv --input clip.mlv --output trimmed.mlv --cut-in <frame> --cut-out <frame>
```

See `src/batch/MlvTrim.{h,cpp}` for the full option list (audio sync,
timecode preservation, …).

#### `--profile-playback` — headless playback profiler

Steps frames through the real Qt `MainWindow`, `RenderFrameThread`, and
`drawFrameReady()` path, and writes a JSON report with per-frame timings and
stage telemetry. This is the preferred tool for measuring playback
performance reproducibly.

```bash
MLVApp --profile-playback --input clip.mlv --output results.json \
    --frames 16 --threads 4 --raw-cache-mb 512 --cache-cpu-cores 2 \
    --gpu-viewport auto --gpu-preview-processing auto \
    --gpu-bilinear-debayer auto --playback-processing auto
```

Key options (parsed in `platform/qt/main.cpp:349-478`):

| Flag | Default | Purpose |
|------|---------|---------|
| `--input` / `-i` | (required) | Clip to profile (`.mlv` or spanned clip). |
| `--output` / `-o` | (required) | JSON output path. |
| `--receipt` / `-r` | (none — receipt-less open) | Optional `.marxml` receipt applied before profiling. |
| `--frames N` | `16` | Number of frames to step. |
| `--start-frame N` | `0` | Zero-based first frame. |
| `--threads N` | `auto` (= core count) | Forces worker count via `MLVAPP_FORCE_THREADS`. |
| `--raw-cache-mb N` | `0` (cache disabled) | Enable the raw cache with this many MiB. |
| `--cache-cpu-cores N` | `auto` | Cache worker core count when raw caching is on. |
| `--gpu-viewport` | off (CPU presenter) | Boolean flag — enable the experimental OpenGL viewport path while profiling. No value. (Use `--gpu-preview-processing` and `--gpu-bilinear-debayer` for the `auto\|cpu\|gpu` enum.) |
| `--gpu-preview-processing <auto\|cpu\|gpu>` | `auto` (CPU) | 16-bit GPU preview-processing backend. |
| `--gpu-bilinear-debayer <auto\|cpu\|gpu>` | `auto` (CPU) | GPU bilinear debayer backend. |
| `--playback-processing <auto\|receipt\|subset>` | `auto` | Processing-stage scope for profiling. |
| `--playback-debayer <auto\|receipt\|none\|simple\|bilinear\|lmmse\|igv\|amaze\|ahd\|rcd\|dcb\|amaze-cached>` | `auto` | Debayer policy. |
| `--scope <histogram\|waveform\|parade\|vectorscope\|none>` | `none` | Enable a live scope while profiling. |
| `--zebras` | off | Enable the zebra overlay. |
| `--wait-for-paint` | off | After each `frameReady()`, wait for the viewport paint and record paint latency. Implies `--show-window`. |
| `--show-window` | off (offscreen) | Show the main window instead of keeping it hidden. |
| `--fast-open` | off (full open) | Use the preview/open-for-preview path instead of a full open. |
| `--stage-log <file>` | none | Append `MLVAPP_STAGE_TIMING` output to a file. |

The emitted JSON contains per-frame timings (decode, llrawproc, debayer,
processing, scopes, paint), stage-level milliseconds, GPU backend flags, and
cache-hit telemetry. See
[14-performance-benchmarking.md](14-performance-benchmarking.md) for how to
interpret the fields and integrate the output into profiling runs.

---

## 10. Code style

The [README](../README.md#a-note-about-code-style) documents the base rule:

> You may notice a strange mixture of these styles:
> 1. `thisNameStyle`
> 2. `this_name_style`
>
> The rule I have used in the libraries is: public functions use the
> `thisNameStyle`, and private functions use this one.

Modern additions from `01-src-architecture.md §7`:

- **Public types** use PascalCase with a `_t` suffix: `mlvObject_t`,
  `processingObject_t`, `dngObject_t`, `filterObject_t`, `llrawprocObject_t`.
- **Public functions** (library entry points) use `thisNameStyle`:
  `initMlvObject`, `openMlvClip`, `getMlvProcessedFrame16`,
  `applyProcessingObject`, `debayerAmaze`.
- **Private / static functions** use `this_name_style`:
  `mlv_reset_last_raw_stage_telemetry`, `file_set_pos`,
  `ensure_processing_u16_scratch`.
- **Thread-local globals** are prefixed `g_` — e.g.
  `static MLV_STAGE_THREAD_LOCAL double g_mlv_last_raw_uint16_ms`. The `g_`
  prefix makes it easy to spot globals at a call site.
- **Error handling**: functions that can fail return an `int` status
  (`MLV_ERR_NONE`, `MLV_ERR_OPEN`, `MLV_ERR_IO`, `MLV_ERR_CORRUPTED`,
  `MLV_ERR_INVALID`) and take a `char *error_message` out-parameter for the
  human-readable description.
- **Memory ownership**: the engine uses a manual allocator pattern of
  `initXxx()` / `freeXxx()` pairs (`initMlvObject` / `freeMlvObject`,
  `initProcessingObject` / `freeProcessingObject`, `initDngObject` /
  `freeDngObject`, `initLLRawProcObject` / `freeLLRawProcObject`). Every
  `init*` has exactly one matching `free*`; there is no `malloc` without a
  corresponding `free`.
- **Documentation**: C-style `/* … */` block comments on public functions;
  struct members documented inline.
- **Formatting**: the project does **not** ship a `clang-format` or
  `.editorconfig` file — match the surrounding file's existing indentation
  (4-space soft tabs in C/C++, no hard tabs) and brace style. CI does **not**
  enable `-Werror`; warnings will not fail the build, but reviewers ask for
  warning-clean PRs touching warning-clean files.

---

## 11. Key engine entry points for new contributors

These are the functions a new contributor will touch first when fixing a bug
or adding a feature. Details for each live in
[03-technical-specification.md](03-technical-specification.md); this list is
just the "where do I start" breadcrumb.

| Function | Header | When to call it |
|----------|--------|-----------------|
| `initMlvObject` | `src/mlv/video_mlv.h` | Allocates a fresh `mlvObject_t` to hold file handles, indices, and processing state. Always paired with `freeMlvObject`. |
| `openMlvClip` | `src/mlv/video_mlv.h` | Opens a clip (and spanned `.m00`/`.m01` segments), parses headers, and builds `video_index`/`audio_index`. Returns `MLV_ERR_*` and writes a human message to `error_message`. |
| `freeMlvObject` | `src/mlv/video_mlv.h` | Releases caches, prefetch slots, file handles, and the object itself. Required for every successful `initMlvObject`. |
| `setMlvProcessing` | `src/mlv/video_mlv.h` | Links a `processingObject_t` receipt to the MLV object so `getMlvProcessedFrame*` can apply it. |
| `getMlvProcessedFrame16` | `src/mlv/video_mlv.h` | Primary playback/export entry: produces a uint16 RGB frame with receipt fully applied. Respects the raw-cache and the prefetch worker. |
| `getMlvProcessedFrame8` | `src/mlv/video_mlv.h` | 8-bit direct path for GUI display. Both 8-bit and 16-bit entries dispatch to the AVX2 fast path when `processingFastPathAvx2Active()` returns true; the 8-bit path additionally fuses the 16→8 reduce step. |
| `applyProcessingObject` | `src/processing/raw_processing.h` | Runs the 9-stage 16-bit pipeline on a debayered frame. Call this when you want control over the debayer step (e.g. tests or CLI tooling). |
| `applyProcessingObject8` | `src/processing/raw_processing.h` | Direct 16→8-bit fast path that skips the intermediate 16-bit store. |
| `debayerEasy` | `src/debayer/debayer.h` | Dispatches to the selected demosaic algorithm (`None`, `Basic`, `AHD`, `AMaZe`, `DCB`, `RCD`, `IGV`, `LMMSE`). |
| `applyLLRawProcObject` | `src/mlv/llrawproc/llrawproc.h` | Runs the in-place low-level raw corrections (dark frame, focus/bad pixels, vertical stripes, dual ISO, pattern noise, chroma smoothing) on a uint16 Bayer buffer before debayer. |
| `saveDngFrame` | `src/dng/dng.h` | Writes a DNG frame (TIFF header + optional LJPEG-compressed or uncompressed raw payload) using a prepared `dngObject_t`. Used by batch CDNG export. |

All of these have manual `initXxx` / `freeXxx` counterparts where applicable;
see `src/mlv_include.h` for the umbrella include.

### 11.1 Minimum embedding sequence

Integrators embedding the engine in a headless host follow this sequence
(pseudo-C, omits error handling):

```c
#include "mlv_include.h"
char err[256] = {0};
mlvObject_t        *clip = initMlvObject();
openMlvClip(clip, "shot.mlv", MLV_OPEN_FULL, err);
processingObject_t *proc = initProcessingObject();
setMlvProcessing(clip, proc);
llrawprocObject_t  *llr  = initLLRawProcObject();
/* configure focus pixels, dual ISO, stripes, etc. on llr */
uint16_t *rgb16 = malloc(width * height * 3 * sizeof(uint16_t));
for (uint32_t f = 0; f < getMlvFrames(clip); ++f) {
    getMlvProcessedFrame16(clip, f, rgb16, /*threads=*/4);
    /* hand rgb16 to your encoder, scope, or DNG writer */
}
free(rgb16);
freeLLRawProcObject(llr); freeProcessingObject(proc); freeMlvObject(clip);
```

For DNG export use `initDngObject` + `saveDngFrame` instead of (or alongside)
the `getMlvProcessedFrame*` loop.

---

## 12. Working with receipts

Receipts are the portable serialisation format for all processing parameters.
They are XML documents with the `.marxml` extension and round-trip every
slider/setting in `processingObject_t` and `llrawprocObject_t`. A minimal
example looks like:

```xml
<mlv_rawtherapee_receipt version="1">
  <exposure>0.10</exposure>
  <wb_temp>5600</wb_temp>
  <wb_tint>0</wb_tint>
  <saturation>1.05</saturation>
  <debayer>amaze</debayer>
  <!-- … many more setter/getter-addressable fields … -->
</mlv_rawtherapee_receipt>
```

The full schema is whatever `ReceiptSettings::write*()` emits — open any file
under `receipts/` to see the canonical layout.

- **Loader**: `src/batch/ReceiptLoader.{h,cpp}` parses `.marxml` into a
  `ReceiptSettings` structure.
- **Applier**: `src/batch/ReceiptApplier.{h,cpp}` pushes a parsed
  `ReceiptSettings` into a live `processingObject_t`/`llrawprocObject_t` pair.
- **Writer**: the GUI's `Session → Save Receipt` action and the batch exporter
  both emit `.marxml` via the setters/getters on `ReceiptSettings`.

Example receipts checked into the repo live under `receipts/` and cover useful
starting points:

- `CanonLog.marxml` — Canon-log transfer function baseline.
- `FastAlexaRCD.marxml`, `FastProxyRCD.marxml` — fast proxy grades with RCD
  debayer.
- `FastProxy.marxml` — minimal fast-playback proxy grade.
- `WarmGlow.marxml`, `bluetonewedding.marxml` — stylised example grades.

Load a receipt from the CLI via `--receipt <path.marxml>` on `--batch`, or
via `Session → Import Receipt` in the GUI. Receipts are the intended
automation surface for plugins and LLM-generated callers because every
parameter is setter/getter-addressable; anything the GUI produces is also
reproducible from a batch run against the same receipt.

---

## 13. Testing

The five test suites are listed in `tests/tests.pro` as `SUBDIRS`
(`console alloc pipeline perf gui`). Building `tests/tests.pro` will *attempt*
to build them all in the listed order, but the suites have different runtime
requirements (Qt Test, GUI Test, custom drivers) and most contributors
**build each suite individually** in its own directory. The recipes below
follow the per-suite pattern so you can iterate on one suite without paying
for the others.

> **Build directories.** Each suite builds in its own `tests/build-<suite>`
> directory which **does not exist on a fresh checkout** — `mkdir -p` it
> first. The build dirs are matched by `*build*` in `.gitignore` (line 24)
> and can be deleted at any time.
>
> Set them all up at once with:
> ```bash
> mkdir -p tests/build-console tests/build-pipeline tests/build-gui tests/build-perf
> ```
>
> CI uses slightly different names (`tests/build-ci-*`, see
> `.github/workflows/tests.yml:60`) to keep CI and local artefacts separate.
> Either is fine; pick a name and stick with it.

**Cross-platform paths.** The recipes below use `./release/<binary>` which
works on Linux/macOS. On Windows the binary is `release\<binary>.exe`. Where
a single command shows both forms, run the one that matches your shell.

**Selective test execution.** The `minitest` runner (`tests/common/minitest.h`)
iterates the entire registered-test list — there is **no name-filter command
line argument**. To run a single test in isolation, comment out the others'
`TEST(...)` macros locally or split the suite. Each `--check-golden` invocation
runs the full registered set against the bundled golden manifest. (The
`gui_tests` suite uses Qt's `QtTest` which *does* accept test selectors via
positional arguments — e.g. `./release/gui_tests TestHistogram` — but the
console and pipeline suites do not.)

### 13.1 `console_tests --check-golden`

Lightweight non-GUI regression suite. Scope: frame hashing against the Dual
ISO fixtures, receipt loader/applier, cache behaviour, AVX parity, worker
thread count.

```bash
mkdir -p tests/build-console
cd tests/build-console
qmake ../console/console_tests.pro
make -j
./release/console_tests --check-golden        # Linux/macOS
# Windows: .\release\console_tests.exe --check-golden
```

Golden file: `tests/fixtures/golden/hashes.json` (binary SHA256 match — no
numeric tolerance). Use `--hash-output <path>` to write current hashes for
diff review.

> **Smoke test.** A green
> `./release/console_tests --check-golden` is the canonical "your build is
> functional" check across all platforms. Run it after every fresh build.

### 13.2 `pipeline_tests --check-golden`

Direct in-process engine goldens against the tiny Dual ISO clip. Scope:
debayer and processing pipelines, GPU preview processing, CrashForensics,
dual ISO full20bit + autodetect + AMaZE helpers, chroma-smooth / median / RBF
reuse, LJ92 decode, Sobel scratch parity, cache invalidation.

```bash
mkdir -p tests/build-pipeline
cd tests/build-pipeline
qmake ../pipeline/pipeline_tests.pro
make -j
./release/pipeline_tests --check-golden        # Linux/macOS
# Windows: .\release\pipeline_tests.exe --check-golden
```

Golden file: `tests/fixtures/golden/pipeline_hashes.json`. Backend-parametric
tests use a PSNR tolerance (`max_abs_diff=3`, `mismatch=0.1%`) rather than a
strict hash.

### 13.3 `gui_tests`

Qt widget smoke coverage (histogram, waveform, parade, vector scope,
presenter seams for the experimental GPU viewport, scope regression hashes).
Pass `QT_QPA_PLATFORM=offscreen` on headless runners; `gui_tests` also
defaults to `offscreen` when the variable is unset.

```bash
mkdir -p tests/build-gui
cd tests/build-gui
qmake ../gui/gui_tests.pro
make -j
QT_QPA_PLATFORM=offscreen ./release/gui_tests -o gui_tests_output.txt,txt
# Windows: $env:QT_QPA_PLATFORM='offscreen'; .\release\gui_tests.exe -o gui_tests_output.txt,txt
```

On Windows CI, the workflow also sets `QT_OPENGL=desktop`,
`QT_QPA_PLATFORM_PLUGIN_PATH`, and `QT_PLUGIN_PATH` (see
`.github/workflows/tests.yml:104-114`).

### 13.4 `perf_tests --iterations N --require-baseline`

Benchmark harness for full/preview 16-bit and 8-bit paths. Measures the tiny
and `large_dual_iso` fixtures plus checked-in synthetic scenarios
(darkframe, forced stripes) and gates against the machine-specific profile in
`tests/perf/baselines.json`.

```bash
mkdir -p tests/build-perf
cd tests/build-perf
qmake ../perf/perf_tests.pro
make -j
./release/perf_tests --iterations 10 --require-baseline
```

Useful flags: `--cold-8bit` (flush processed preview cache per 8-bit
sample), `--raw-cache-mb N --cache-cpu-cores N` (leave the raw cache on),
`--stage-log <file>` (append stage timings), `--update-baseline` (refresh
the baseline), `--extra-clip / --extra-receipt / --extra-label` (custom
scenarios). `perf_tests` is intentionally **not** in CI because VM noise
crosses thresholds spuriously; see
[14-performance-benchmarking.md](14-performance-benchmarking.md). It runs
locally only.

### 13.5 `fuzz_*` targets (not in CI)

Opt-in fuzz harnesses under `tests/fuzz/`:

```bash
./fuzz_receipt_loader tests/fixtures/receipts
./fuzz_lj92          tests/fixtures/clips/tiny_dual_iso.mlv
./fuzz_mlv_open      tests/fixtures/clips/tiny_dual_iso.mlv
```

Run locally or on nightly hardware; they are not currently wired into GitHub
Actions. See [13-testing-infrastructure.md](13-testing-infrastructure.md) for
deeper coverage and the fuzz corpus layout.

---

## 14. CI overview

All workflows live under `.github/workflows/`.

| Workflow | Trigger | What it does |
|----------|---------|--------------|
| [`tests.yml`](../.github/workflows/tests.yml) | `workflow_dispatch`, every `pull_request`, and `push` to `master`. **No `paths` filter** — see the note below. | Seven jobs. `protected-check-route` (ubuntu) derives a fail-closed route; `repo-hygiene-python` (ubuntu), `repo-hygiene-python-windows-shard` (five parallel windows runners) and `batch-compile` (windows) run independently of it; `repo-hygiene-python-windows` (ubuntu) is an aggregator that needs every shard and keeps the required name `Repo Hygiene Python (windows-latest)`; `windows-product-oracles` and `windows-gui-pilot` (both windows) `needs: protected-check-route` and run `console_tests`/`pipeline_tests --check-golden` and `gui_tests` respectively. `factory-bridge-regressions` lives in `factory-bridge.yml`, not here. |
| [`Windows.yml`](../.github/workflows/Windows.yml) | `workflow_dispatch` on `master` | Release artifact. Chocolatey-installs Qt 5.15.2 + MinGW 8.1 + OpenSSL, runs `qmake` + `make` + `windeployqt`, unpacks `ffmpegWin64.zip` and `raw2mlvWin64.zip` with 7-Zip, uploads `MLVApp.Win64.zip`. |
| [`Linux.yml`](../.github/workflows/Linux.yml) | `workflow_dispatch` on `master` | Ubuntu 22.04 runner. Installs the apt dependencies listed in [§3.4](#34-linux), runs `qmake` + `make -j8`, unpacks the bundled ffmpeg/raw2mlv, wraps it all with `linuxdeploy` + the Qt plugin to produce `MLVApp.AppImage`. |
| [`macOS-Intel.yml`](../.github/workflows/macOS-Intel.yml) | `workflow_dispatch` on `master` | macOS 13 runner. `brew install llvm qt5 openssl` (alias to `qt@5`), `qmake -r`, `make -j8`, `macdeployqt -dmg` from `/usr/local/opt/qt@5/bin`. Artifact: `MLV App.dmg`. |
| [`macOS-Arm64.yml`](../.github/workflows/macOS-Arm64.yml) | `workflow_dispatch` on `master` | macOS 14 runner, same recipe as Intel but `QTDIR=/opt/homebrew/opt/qt@5/bin`. Artifact: `MLV App.dmg`. |

### 14.1 What actually blocks a merge

`tests.yml` reports more check runs than there are required checks: the Windows hygiene
suite runs as five `Repo Hygiene Python windows shard N/5` jobs (not required by name),
and the `Repo Hygiene Python (windows-latest)` aggregator that needs all five carries the
required name. **Five check names are required status checks** on `master`:
`Repo Hygiene Python (ubuntu-latest)`, `Repo Hygiene Python (windows-latest)`,
`Batch Compile`, `Windows Product Oracles`, `Windows GUI Pilot`. (`Factory Bridge
Regressions` runs from `factory-bridge.yml` and is no longer required.)
`Protected Check Route` is deliberately **not** required — the two jobs that
depend on it already fail closed when it does not produce a usable answer, so
requiring it as well would add a second way to say the same thing. Protection is
`strict` (the branch must be up to date), `enforce_admins` is on, and
`required_approving_review_count` is `0` — the merge bar is five green checks on
an up-to-date ref, not a second reviewer. One repository ruleset also applies to
`master`: `CodeQL high-severity merge protection`, which is `code_scanning`-only
and adds no status check of its own.

**There is no `continue-on-error` anywhere in `tests.yml`, and no pilot tier.**
`gui_tests` was promoted out of pilot status; `Windows GUI Pilot` keeps the old
name but is a required check like the rest.

`perf_tests` is intentionally **not** in CI because VM noise makes the
thresholds flaky; it runs locally only.

### 14.2 Why there is no `paths` filter

Selective skipping is done by the `protected-check-route` job, **not** by a
workflow-level `paths:` key. The route runs
[`tools/repo_hygiene/protected_check_router.py`](../tools/repo_hygiene/protected_check_router.py)
over the base..head range and emits `product` and `gui` booleans; the two Windows
jobs run with `if: ${{ always() }}`, fail closed if the route did not produce a
usable answer, and otherwise either do the real work or record an explicit N/A.

The distinction is load-bearing. A `paths:` filter makes the job **not run at
all**, and a required status check that never runs is never satisfied — so a
docs-only PR would hang instead of merging, and worse, path filtering makes a
required check *suppressible by the shape of the diff*. Routing inside a job that
always runs keeps the check reportable while still skipping the expensive build.

This is enforced, not merely preferred:
[`tools/repo_hygiene/test_repo_hygiene.py`](../tools/repo_hygiene/test_repo_hygiene.py)
(`test_ci_workflow_hardening_is_fail_closed_and_coordination_aware`) asserts that
the trigger block contains no `paths:` or `paths-ignore:` key, with the reason
*"required PR and master CI must not be suppressible by path filtering"*, and pins
the job set and each job's `timeout-minutes`. **Reintroducing a path filter, or
renaming a job key, reds that test.** Renaming a job's display `name:` is the more
dangerous edit: no test catches it, and it silently orphans the required status
check that carries that name in branch protection.

Historical note: this section previously described a `paths` filter, a blocking
`windows-scaffold` job, and a `windows-gui-pilot` running under
`continue-on-error`. All three are retired. The first two are actively forbidden
by the test above; job-level `continue-on-error` is simply absent, and is not
separately asserted against at the job level. Background on the original pilot
rollout is in
[`.claude/analysis/testing-scaffold-implementation.md`](../.claude/analysis/testing-scaffold-implementation.md),
which is kept as history and does not describe current CI.

---

## 15. Debugging

### 15.1 Attaching a debugger

- **Qt Creator**: open `platform/qt/MLVApp.pro`, pick the **Debug**
  configuration, and click the bug icon. Qt Creator will launch under GDB
  (Linux/MinGW) or LLDB (macOS). On Windows, ensure the debug build inherits
  the [Windows runtime rules](#6-windows-runtime-rules-critical) `PATH` /
  `QT_OPENGL` environment — Qt Creator does this automatically if your kit
  is configured right, but a shell launch needs it manually.
- **Command line**:
  - Windows (MinGW): `gdb release\MLVApp.exe`
  - macOS: `lldb 'platform/build/MLV App.app/Contents/MacOS/MLV App'`
  - Linux: `gdb ./mlvapp`
  - Headless repro: `gdb --args ./mlvapp --batch --input clip.mlv --output /tmp/dng-out`

### 15.2 Crash forensics (Windows)

On Windows, `platform/qt/CrashForensics.cpp` installs an unhandled-exception
filter at startup and writes a minidump via `MiniDumpWriteDump` from
`dbghelp.lib` (linked in `platform/qt/MLVApp.pro:94-96`). The minidump is
stamped with the build-time git SHA captured via `MLVAPP_GIT_SHA` and written
to **`%TEMP%`** — typically
`C:\Users\<you>\AppData\Local\Temp\MLVApp_<sha>_<timestamp>.dmp`. Open
PowerShell and `Get-ChildItem $env:TEMP\MLVApp_*.dmp | Sort LastWriteTime`
to find the most recent one. `tests/pipeline/test_crash_forensics.cpp`
regression-tests the handler.

The SHA stamped in the filename is `git rev-parse HEAD` at build time and
**does not flag a dirty working tree**. If you crash on a build with local
modifications, the SHA points at the parent commit, not your work-in-progress
— rebuild from a clean tree before triaging a customer crash.

### 15.3 Stage timing

Set `MLVAPP_STAGE_TIMING=1` to emit per-stage timing lines while rendering
through the GUI, the pipeline tests, or the perf harness:

```bash
# PowerShell
$env:MLVAPP_STAGE_TIMING = '1'
# bash
MLVAPP_STAGE_TIMING=1 ./release/perf_tests --stage-log stages.log --iterations 3
```

The `--profile-playback` mode accepts `--stage-log <file>` which implies
`MLVAPP_STAGE_TIMING=1` and appends the stream to a file instead of relying
on stdout capture.

### 15.4 Profiling artifacts

Durable profiling outputs (`perf_tests` runtime wrapper, playback profiler
JSONs, stage logs) are written under `.claude-state/profiling/<date>-<topic>/`
by convention. The wrapper
`tests/perf/run_runtime_profile.ps1` picks up that location automatically.
Keep new artifacts under `.claude-state/` — never commit new files to
`.claude/`.

### 15.5 Known gotchas

- **Prefetch worker hides thread-local telemetry**. The default-on decode
  worker masks LJ92 thread-local signals; when profiling LJ92-sensitive
  paths, set `MLVAPP_DISABLE_RAW_UINT16_PREFETCH=1`.
- **`StageTiming.h` snapshots are translation-unit-local**. Reusing a
  snapshot across TUs returns zeros; use the `video_mlv.h` getters
  (`getMlvLastRawUint16Milliseconds`,
  `getMlvLastRawUint16DecompressMilliseconds`, etc.) instead.

---

## 16. Contributing workflow

1. **Fork** the repo on GitHub (`ilia3101/MLV-App`) — see [§4](#4-cloning) for
   the fork-and-clone command.
2. **Branch** from `master` using the conventional prefixes:
   - `feature/<short-description>` — new functionality.
   - `fix/<short-description>` — bug fixes.
   - `refactor/<short-description>` — non-behavioural refactors.
   - `docs/<short-description>` — documentation-only changes.
3. **Build + run tests locally** before pushing:
   - `console_tests --check-golden` (blocking)
   - `pipeline_tests --check-golden` (blocking)
   - `gui_tests` (pilot; nice-to-have)
4. **Open a PR against `master`**. CI will run the Windows scaffold
   (`tests.yml`). The two blocking suites must pass. `gui_tests` is the
   independent non-blocking pilot — its job runs with
   `continue-on-error: true`.
5. **Review**: maintainers look for style conformance (§10), no regressions
   in the golden fixtures, and that the perf harness baseline was refreshed
   if you touched a hot path. *Hot paths* are anything under
   `src/processing/`, `src/debayer/`, `src/mlv/llrawproc/`,
   `src/mlv/frame_caching.c`, `src/mlv/video_mlv.c`,
   `src/mlv/liblj92/lj92.c`, or the GPU surfaces under
   `platform/qt/Gpu*.cpp`. If your diff touches one of those files, refresh
   the local baseline (`./release/perf_tests --update-baseline`) and include
   the new `tests/perf/baselines.json` rows in your commit.
6. **Merge**: squash-merge is the default; commit subject should mention the
   subsystem (`Playback: …`, `Processing: …`, `Docs: …`).
7. **Release artifacts** are built on demand via the per-OS
   `workflow_dispatch` triggers; contributors do not need to run them.

---

## 17. Directory policies

Repeating the AGENTS.md policy because it matters:

- **`.claude/` is curated and tracked**. Files here are checked out by `git
  clone` and readable to everyone. Editing existing tracked notes under
  `.claude/analysis/` is fine; only *creating new files* is forbidden — new
  scratch goes under `.claude-state/`.
- **`.claude-state/` is `.gitignore`d** and is the correct home for all
  ephemeral scratch — profiling JSON, smoke-test logs, stashed artifacts,
  summary scripts.
- **Binaries**: do not commit binaries. The only large checked-in binaries
  are the two golden MLV fixtures under `tests/fixtures/clips/`
  (`tiny_dual_iso.mlv`, `large_dual_iso.mlv`) and the prebuilt
  ffmpeg/raw2mlv archives under `platform/qt/`.

---

## 18. Troubleshooting

### 18.1 Build / launch (all platforms)

| Symptom | Fix |
|---------|-----|
| `qmake: command not found` (Windows) | Add `C:\Qt\<version>\mingw_64\bin` to PATH before running `qmake`. |
| `mingw32-make: command not found` (Windows) | Add `C:\Qt\Tools\mingw1310_64\bin` (or your installed MinGW) to PATH. |
| Qt Creator "No kits for MLVApp.pro" | Configure a kit that pairs Qt + MinGW of matching bitness. Do not mix a 64-bit Qt with a 32-bit MinGW. |
| `qmake ../console/console_tests.pro` fails with `chdir: No such file or directory` | You forgot `mkdir -p tests/build-console` first. See [§13](#13-testing) preamble. |
| `qmake` re-runs are confused after a previous build | Delete the per-suite `tests/build-*` directory and start over (`rm -rf tests/build-pipeline && mkdir -p tests/build-pipeline`). qmake caches the previous configuration in `Makefile`. |
| macOS build fails: `clang: error: unsupported option '-fopenmp'` | Apple clang does not ship OpenMP. `brew install llvm`, and verify `QMAKE_CC = /usr/local/opt/llvm/bin/clang` (Intel) or `/opt/homebrew/opt/llvm/bin/clang` (ARM). Do **not** fall back to Apple clang. |
| macOS link error: `library not found for -lomp` | Install with `brew install llvm`; the formula installs `libomp` into `/usr/local/opt/llvm/lib` (Intel) or `/opt/homebrew/opt/llvm/lib` (ARM). |
| macOS: `brew install qt5` does nothing on a fresh box | Modern Homebrew aliases `qt5` to `qt@5`. Use `brew install qt@5` to get the canonical name and the `/usr/local/opt/qt@5/` (Intel) or `/opt/homebrew/opt/qt@5/` (ARM) symlink. |
| Linux AppImage fails at runtime: `failed to load fuse` | Install `libfuse2` (`sudo apt-get install libfuse2`). The AppImage runtime requires it. |
| Linux build fails: `qt5-qmake: not found` | Run the full `apt-get install` line from [§3.4](#34-linux); `qtbase5-dev` alone is not enough. |
| `perf_tests` reports regression on clean build | Refresh the local baseline: `./release/perf_tests --update-baseline`. VM noise occasionally crosses the gate; repeat the run before committing a baseline bump. |
| `git push` returns `403 (write access denied)` | You cloned upstream instead of your fork. Reclone via [§4](#4-cloning) `gh repo fork ilia3101/MLV-App --clone --remote`, or rewrite the `origin` remote to your fork. |

### 18.2 Windows: top 5 fresh-contributor errors

| Symptom (typical message) | Cause | Fix |
|---------|-------|-----|
| **`Qt6Core.dll` was not found** modal at launch | `windeployqt` was not run, or PATH does not include the Qt `bin/`. | From the directory containing `MLVApp.exe`, run `C:\Qt\6.10.2\mingw_64\bin\windeployqt.exe MLVApp.exe --release --no-translations --no-compiler-runtime`. Ensure the canonical PATH order in [§6](#6-windows-runtime-rules-critical). |
| **`mingw32-make.exe` is not recognised as an internal or external command** | MinGW `bin/` not on PATH. | `$env:PATH = 'C:\Qt\Tools\mingw1310_64\bin;' + $env:PATH`. Verify with `mingw32-make --version`. |
| **`qmake: command not found`** or **"no Qt found"** in Qt Creator | Wrong / unconfigured kit, or no `Desktop Qt 6.10.2 MinGW 64-bit` kit. | Tools → Options → Kits → Add. Pair Qt 6.10.2 MinGW 64-bit with the MinGW 13.1 64-bit toolchain. Re-run `qmake` from PowerShell: `& "$env:QT_ROOT_DIR\bin\qmake.exe" --version`. |
| **Plugin `qwindows.dll` is missing** at launch (sometimes silent black window) | `windeployqt` ran but `platforms\qwindows.dll` was deleted, or you launched from a directory that has a stale `platforms\` copy. | Re-run `windeployqt` (see top row); confirm `release\platforms\qwindows.dll` exists. As a workaround, `$env:QT_QPA_PLATFORM_PLUGIN_PATH = 'C:\Qt\6.10.2\mingw_64\plugins\platforms'`. |
| **Black / garbled viewport, or instant crash** when opening a clip with the GPU viewport enabled | ANGLE wrapper picked instead of desktop OpenGL. | `$env:QT_OPENGL = 'desktop'` before launch (see [§6](#6-windows-runtime-rules-critical) rule 1). If a hardware GL driver is missing, fall back to `software` only as a last resort — performance will be poor. |

---

## 19. Environment variable reference

All MLV App environment variables in one place. **B** = build-time
(qmake/make), **R** = runtime.

| Variable | Scope | Default | Purpose |
|----------|-------|---------|---------|
| `MLVAPP_ENABLE_AVX` / `MLVAPP_ENABLE_AVX2` | B | unset | Opt in to the AVX2 fast path (§7). Same as `CONFIG+=mlvapp_enable_avx2`. |
| `MLVAPP_GIT_SHA` | B (`-D` define injected by qmake) | `git rev-parse HEAD`, fallback `unknown` | Stamped into the binary for crash-forensics filename and About dialog. |
| `MLVAPP_FORCE_THREADS` | R | unset (auto-detect) | Override worker count. Also set by `--profile-playback --threads N`. |
| `MLVAPP_STAGE_TIMING` | R | unset | Emit per-stage timing lines on stdout. `--stage-log` implies this. |
| `MLVAPP_DISABLE_RAW_UINT16_PREFETCH` | R | unset | Disable the default-on raw uint16 prefetch worker (needed when profiling LJ92 thread-local telemetry — see §15.5). |
| `QT_OPENGL` | R | `dynamic` | **Set to `desktop` on Windows** (see §6). Picks desktop GL over ANGLE. |
| `QT_QPA_PLATFORM` | R | platform default | `offscreen` for headless `gui_tests` runs (§13.3). |
| `QT_QPA_PLATFORM_PLUGIN_PATH` | R | next to exe | Override platform-plugin search dir if `qwindows.dll` is missing. |
| `QT_PLUGIN_PATH` | R | next to exe | Override generic plugin search dir (audio, mediaservice, etc.). |
| `QTDIR` | B | unset | macOS Arm64 release pin: `Macos-Arm64.yml` exports `QTDIR=/opt/homebrew/opt/qt@5/bin` so `qmake`/`macdeployqt` resolve to the Homebrew Qt 5 prefix. Set explicitly when scripting an Arm64 release outside the workflow. |
| `QT_QPA_PLATFORMTHEME` | R | desktop default | Linux only — `qt5ct` enables the system Qt theme integrator if installed; harmless to leave unset. |

CLI flags map to many of these — see §9.2 for the playback profiler.

---

## 20. Further reading

- [03-technical-specification.md](03-technical-specification.md) — deep dive
  into architecture, data flow, threading model, per-API details.
- [04-external-auditor-guide.md](04-external-auditor-guide.md) — for
  reviewers and security auditors reproducing builds and test runs cold.
- [10-build-windows.md](10-build-windows.md) — extended Windows build
  walkthrough, including the Chocolatey + Qt 5 path used by `Windows.yml`.
- [11-build-macos-linux.md](11-build-macos-linux.md) — extended macOS + Linux
  build walkthrough.
- [12-gpu-viewport-architecture.md](12-gpu-viewport-architecture.md) —
  experimental OpenGL viewport, GPU debayer, and GPU preview-processing seams.
- [13-testing-infrastructure.md](13-testing-infrastructure.md) — full test
  harness reference, fixture generation, golden manifests.
- [14-performance-benchmarking.md](14-performance-benchmarking.md) — perf
  harness, baselines, playback profiler interpretation.
- [15-test-fixtures.md](15-test-fixtures.md) — committed MLV fixtures,
  receipts, and golden hash manifests.
- [16-fuzz-testing.md](16-fuzz-testing.md) — fuzz targets for the receipt
  loader, LJ92 codec, and MLV file opener.
