# MLV App — User Guide

> Install MLV App, import a Magic Lantern raw clip, apply a grade, and
> export. This guide covers MLV App **1.15.0.0** on Windows, macOS
> (Intel and Apple Silicon), and Linux. Start here if you are an end
> user, colorist, editor, or photographer. For compiling from source
> see [docs/02-developer-guide.md](02-developer-guide.md); for
> architecture internals see
> [docs/03-technical-specification.md](03-technical-specification.md);
> for audit and reproducibility see
> [docs/04-external-auditor-guide.md](04-external-auditor-guide.md).

### How to use this guide

- **First time?** Install per [§3](#3-installation), then run
  [§4.5 Your first 5 minutes](#45-your-first-5-minutes) for a
  download → import → grade → ProRes export round-trip.
- **Working colorist or editor?** Skim [§4](#4-first-launch-and-orientation),
  then [§6](#6-processing-parameters--full-tour) +
  [§11](#11-exporting). Use [§11.0 beginner recipe](#110-beginner-recommended-export-recipe)
  as a baseline.
- **Returning after a break?** Jump to [§18 shortcuts](#18-keyboard-shortcuts-and-power-user-tips)
  and [§17 troubleshooting](#17-troubleshooting).

## Table of contents

1. [Introduction](#1-introduction) · 2. [System requirements](#2-system-requirements) · 3. [Installation](#3-installation) · 4. [First launch and orientation](#4-first-launch-and-orientation) · 5. [Importing clips](#5-importing-clips) · 6. [Processing parameters — full tour](#6-processing-parameters--full-tour) · 7. [Raw corrections](#7-raw-corrections) · 8. [Playback](#8-playback) · 9. [Analysis tools](#9-analysis-tools) · 10. [Session management](#10-session-management) · 11. [Exporting](#11-exporting) · 12. [Resize, aspect ratio, rotation](#12-resize-aspect-ratio-rotation) · 13. [Filters](#13-filters) · 14. [LUTs](#14-luts) · 15. [Batch workflows and CLI](#15-batch-workflows-and-cli) · 16. [Power-user playback overlays](#16-power-user-playback-overlays) · 17. [Troubleshooting](#17-troubleshooting) · 18. [Keyboard shortcuts and power-user tips](#18-keyboard-shortcuts-and-power-user-tips) · 19. [Further reading](#19-further-reading)

---

## 1. Introduction

**MLV App** is a cross-platform editor and transcoder for **Magic
Lantern MLV raw video** — the raw container produced by Magic Lantern
firmware on Canon DSLRs (5D Mark II/III, 6D, 7D, 60D, 70D, 100D, 500D–
700D, EOS-M, and others; for the canonical compatibility list see the
[Magic Lantern downloads page](https://builds.magiclantern.fm/) and the
[ML camera-port forum](https://www.magiclantern.fm/forum/index.php?board=10.0)).
Think *Lightroom, but for raw video*.

### What is an MLV file?

A single-camera-original raw container. Long takes are split into a
lead `.MLV` plus zero-padded continuations (`.M00`, `.M01`, …) in the
same folder. Expect 100–400 MB per second of *recorded* 14-bit
footage. Each file holds raw Bayer frames (no debayer, no WB baked
in), optional PCM audio, and shot metadata (camera, lens, ISO,
shutter, aperture, dual-ISO flag, focus-pixel pattern). MLV App
reads all of that, plus the optional `.MAPP` sidecar (§5).

### Feature summary

- Parametric color grading via a per-clip **receipt** (exposure, WB,
  curves, sharpening, toning, LUTs, filters).
- **Raw-domain corrections** (focus/bad pixels, chroma smoothing,
  pattern noise, vertical stripes, Dual ISO, dark-frame subtraction).
- Eight debayer algorithms: Simple, Bilinear, AMaZE, LMMSE, IGV, AHD,
  RCD, DCB — the last five from
  [librtprocess](https://github.com/CarVac/librtprocess).
- Analysis scopes (histogram with under/over markers, waveform, RGB
  parade, vectorscope) and a white-balance picker.
- Export to ProRes (422 Proxy/LT/Standard/HQ, 4444), Cinema DNG
  (10/12/14/16-bit), H.264, H.265 (8/10/12-bit), TIFF, PNG, DNxHD/HR,
  CineForm, VP9, JPEG2000, MotionJPEG, HuffYUV, MLV, plus audio-only.
- Sessions (`.marsx`) with copy/paste, batch paste, and standalone
  `.marxml` receipt import/export.
- FCPXML round-trip for proxy-edit NLE workflows.

### Where to download

| Platform | Source | Maintainer |
|---|---|---|
| Windows (64-bit), macOS (Intel / Apple Silicon), Linux (AppImage) | <https://mlv.app> and [GitHub releases](https://github.com/ilia3101/MLV-App/releases) | Upstream |
| Debian | <http://sid.ethz.ch/debian/mlv-app/> | @alexmyczko |
| Arch AUR | <https://aur.archlinux.org/packages/mlv.app/> | davvore33 |
| NixOS | <https://search.nixos.org/packages?show=mlv-app> | NixOS maintainers |
| Android (via Winlator) | Install Winlator, then Win64 release inside it | Community |

License: GPL-3.0. Source: <https://github.com/ilia3101/MLV-App>.

---

## 2. System requirements

Pre-built releases bundle Qt, ffmpeg, and all other dependencies.
You do **not** need to install anything else to *run* MLV App. Skip
straight to [§3 Installation](#3-installation) once you have downloaded
the right archive for your OS.

| OS | Minimum | Notes |
|---|---|---|
| macOS (Intel) | 10.8 "Mountain Lion" | Separate `.dmg` from Apple Silicon build |
| macOS (Apple Silicon) | 11.7 "Big Sur" | Native arm64 |
| Windows | Windows 7, 64-bit | 64-bit only |
| Linux | Ubuntu 20.04 / Debian 11 / modern Arch / Fedora 36 | AppImage, no install needed |
| Android | Latest Winlator app | Run Win64 release inside Winlator; expect roughly real-time SD playback or 5–10 fps at 1080p preview, depending on phone SoC |

Expect 100–400 MB per second of *recorded* 14-bit raw on disk. 8 GB+
RAM is recommended. SSE2/SSSE3 is required.

> **Building from source (advanced).** All compile-time dependencies
> (Qt 5.6 – 5.15.2 or Qt 6.4+, MinGW or Clang, ffmpeg, AVX2 opt-in
> flags, Linux `apt-get` packages, macOS Homebrew formulae) live in
> the developer guide. See [02 — Developer Guide](02-developer-guide.md)
> and the per-platform pages
> [10 — Build on Windows](10-build-windows.md) and
> [11 — Build on macOS / Linux](11-build-macos-linux.md). End users do
> not need any of those packages.

---

## 3. Installation

### 3.1 macOS (Intel or Apple Silicon)

1. Download the matching `.dmg` from <https://mlv.app>.
2. Double-click the `.dmg` to mount it.
3. Drag **MLV App.app** onto the `/Applications` shortcut.
4. Eject the disk image; launch from Launchpad or Finder.

First-launch Gatekeeper warning: **System Settings → Privacy &
Security → Open Anyway**. Do *not* install the Intel build on Apple
Silicon unless you explicitly want Rosetta.

### 3.2 Windows

1. Download `MLVApp.Win64.zip` from <https://mlv.app>.
2. Right-click the zip → **Extract All…** → pick a folder (for
   example `C:\Tools\MLVApp\`).
3. Inside the extracted folder you will see `MLVApp.exe` plus a
   collection of Qt DLLs, the `ffmpeg.exe` and `raw2mlv.exe` helper
   binaries, and the `platforms\` and `imageformats\` subfolders.
   Double-click `MLVApp.exe` to launch.

Keep the folder intact: the bundled Qt DLLs, MinGW runtime, ffmpeg,
and raw2mlv binaries must stay next to `MLVApp.exe`. If SmartScreen
warns, choose **More info → Run anyway**. For the
`Qt6Core.dll not found` error see [Section 17](#17-troubleshooting).

### 3.3 Linux (AppImage — recommended)

Download `MLVApp-x86_64.AppImage` from the
[GitHub releases page](https://github.com/ilia3101/MLV-App/releases),
then:

```bash
chmod +x MLVApp.AppImage
./MLVApp.AppImage
```

Requires `libfuse2` on recent Debian/Ubuntu
(`sudo apt install libfuse2`). Desktop integration via `appimaged` or
`appimagelauncher` is optional. To make the app available system-wide,
move the AppImage to `~/Applications/` (or any folder on your PATH).

### 3.4 Linux (Debian, Arch, NixOS)

| Distro | Command |
|---|---|
| Debian | Add the @alexmyczko apt source, then `sudo apt install mlv-app` (see commands below) |
| Arch (AUR) | `yay -S mlv.app` |
| NixOS | `nix-env -iA nixpkgs.mlv-app` or add `mlv-app` to `environment.systemPackages` |

For Debian, the exact apt-source commands are:

```bash
echo "deb [trusted=yes] http://sid.ethz.ch/debian/mlv-app/ ./" \
  | sudo tee /etc/apt/sources.list.d/mlv-app.list
sudo apt update
sudo apt install mlv-app
```

The `[trusted=yes]` flag accepts the unsigned third-party repository.
If you prefer to verify the maintainer key explicitly, see the
@alexmyczko repo notes at <http://sid.ethz.ch/debian/mlv-app/>.

### 3.5 Android via Winlator

MLV App on Android runs unchanged inside Winlator's Wine container.
First-time Winlator users should follow the
[Winlator quick-start](https://github.com/brunodev85/winlator/wiki) to
create a Windows 10 container; allocate at least 4 GB RAM (8 GB if
your phone allows), enable DXVK/VKD3D, and mount your media folder as
drive `Z:\`. Inside the container, copy the extracted
`MLVApp.Win64.zip` contents into `C:\MLVApp\`, then run `MLVApp.exe`.
Use only for ad-hoc review — expect roughly real-time SD playback or
5–10 fps at 1080p, depending on phone SoC.

---

## 4. First launch and orientation

The main window contains five working areas wrapped around the menu
bar (File / Edit / Playback / View / Window / Help):

| Area | Purpose |
|---|---|
| **Preview viewport** (top-left, large) | The video frame; double-click to toggle the WB picker |
| **Editor panel** (right) | Collapsible sections: Profiles, RAW Correction, Transformation, Filter, LUT, Lens Correction, plus the grading sliders |
| **Scopes strip** (under preview) | Histogram / waveform / parade / vectorscope (each independently toggleable) |
| **Session browser** (bottom-left) | Clip list with marks and MAPP status |
| **Clip Information** (bottom-right) | Camera, lens, ISO, resolution, frame count |
| **Timeline + transport** (bottom) | Scrubber, timecode, play / prev / next |

Every panel can be shown/hidden from the **View** menu: session
browser <kbd>S</kbd>, editor panel <kbd>E</kbd>, audio track
<kbd>A</kbd>, scopes <kbd>H</kbd> / <kbd>W</kbd> / <kbd>P</kbd> /
<kbd>V</kbd>.

### What you would see (panel walk-through)

This guide ships without screenshots; the descriptions below are the
textual equivalent. Press <kbd>F1</kbd> (or **Help → Help**) to open
the in-app HTML reference with annotated diagrams.

```
+--------------------------------------------------------------+--------------+
|  File  Edit  View  Playback  Audio  Help                     |  [_][o][x]   |
+--------------------------------------------------------------+--------------+
|                                                              | EDITOR PANEL |
|                                                              | (right dock, |
|                                                              | ~340 px)     |
|                  PREVIEW VIEWPORT                            |              |
|              (current debayered + graded frame;              | [Profiles]   |
|               Bayer-bypass overlay shows when                | [RAW Correct]|
|               WB picker (B) is armed)                        | [Transform]  |
|                                                              | [Filter]     |
|                                                              | [LUT]        |
|                                                              | [Lens Corr]  |
|                                                              |              |
|                                                              | Exposure  []|
|                                                              | Temp/Tint []|
|                                                              | Curves    []|
|                                                              | Hue-vs    []|
|                                                              | Sharpen   []|
|                                                              | Denoise   []|
+--------------------------------------------------------------+ Grain     []|
| |<  <  [PLAY]  >  >|  loop  |    00:00:00 / 00:00:14         | Toning    []|
+----+----+--------+---------------------------------------+---+              |
|    |    |        |  HIST | WAVE | PARADE | VECTORSCOPE   |   |              |
|    |    |        +-------+------+--------+---------------+   |              |
+----+----+--------+----------------------------------------------------------+
| SESSION BROWSER (multi-column: file / marker / MAPP /  | CLIP INFO          |
| Cut In / Cut Out / receipt) — drag-and-drop import.    | (camera, lens,     |
| Right-click context menu for paste / batch-paste /     |  ISO, shutter,     |
| receipt management. View > Session List Preview swaps  |  aperture, res,    |
| this for thumbnail/picture mode.                       |  fps, audio …)     |
+--------------------------------------------------------+--------------------+
```

Panel landmarks (useful when reading the rest of the guide):

- **Preview viewport (top-left, ~60 % of the window).** Black until a
  clip is loaded, then shows the current frame at fit-to-window
  scaling. Double-click toggles the WB eyedropper. When zoomed past
  fit, scrollbars appear and middle-click drag pans. An FPS overlay
  shows during transport.
- **Editor panel (right edge, ~340 px wide).** Vertical stack of
  collapsible group boxes: **Profiles**, **RAW Correction**,
  **Transformation**, **Filter**, **LUT**, **Lens Correction**, plus
  the main grading section (Exposure, Temperature, Tint, Dark/Light,
  Gradation Curves, Hue-vs canvases, Sharpen, Denoise, Grain,
  Toning). Click a group-box title to collapse/expand. Each slider
  has a spin-box; double-click the handle to reset that one
  parameter.
- **Scopes strip (under the preview, ~120 px tall).** Up to four
  side-by-side panels for Histogram, Waveform, RGB Parade, and
  Vectorscope. Each toggles independently. Histogram shows red
  triangle markers for under/over-exposure cut-offs.
- **Session browser dock (bottom-left).** Multi-column list (file
  name, marker color, MAPP indicator, Cut In/Out, receipt name).
  Selected rows highlight; the active clip has a thicker border.
  Right-click for the context menu. Switch to picture/table previews
  with **View → Session List Preview**.
- **Clip Information dock (bottom-right).** Label list: camera,
  lens, focal length, ISO, shutter, aperture, recorded resolution,
  duration, frame count, frame rate, dual-ISO state, bit depth,
  audio presence.
- **Timeline + transport (bottom of preview).** Horizontal scrubber
  with playhead handle. Beneath: current timecode, total duration,
  **|<** first frame, **<** prev, **Play/Pause**, **>** next, loop
  toggle, In/Out cut buttons.

### The receipt concept

MLV App is built around a per-clip **receipt**: the complete set of
processing parameters (exposure, WB, curves, toning, LUT, raw
corrections, transform, stretch, everything). You can copy/paste
it between clips (<kbd>Ctrl</kbd>+<kbd>C</kbd> /
<kbd>Ctrl</kbd>+<kbd>V</kbd>), batch-paste onto multiple selected
clips, reset to defaults
(<kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd>), and export/import a
receipt as a standalone `.marxml` file
(<kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>C</kbd> /
<kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>V</kbd>). The `receipts/` folder
in the repository ships sample presets.

A **session** (`.marsx`) lists every clip path and its receipt.
Save with <kbd>Ctrl</kbd>+<kbd>S</kbd>, open with
<kbd>Ctrl</kbd>+<kbd>O</kbd>.

### 4.5 Your first 5 minutes

Follow these eight steps in order. By the end you have a graded
ProRes 422 HQ MOV ready for your NLE.

1. Download from <https://mlv.app> and install per [§3](#3-installation).
2. Launch MLV App.
3. <kbd>Ctrl</kbd>+<kbd>I</kbd> → pick one `.MLV` file. The first
   frame appears within a second.
4. <kbd>Space</kbd> plays, <kbd>Space</kbd> again pauses, <kbd>X</kbd>
   returns to first frame.
5. In the Editor panel (right), drag **Exposure Correction** until the
   histogram (<kbd>H</kbd>) highlights just touch but do not cross
   the right edge.
6. Drag **Temperature** for natural skin tones, or click the
   eyedropper (<kbd>B</kbd>) and click a neutral grey patch.
7. <kbd>Ctrl</kbd>+<kbd>,</kbd> → set **Codec** = `Apple ProRes 422
   HQ`, leave **Debayer** = `Receipt configuration`, **Resize**
   unchecked, **Export audio** checked. Click **Close**.
8. <kbd>Ctrl</kbd>+<kbd>E</kbd> → choose folder → **Save**. The
   progress bar replaces the timeline; at 100 % the `.mov` is on
   disk.

Everything below is detail and power-user material.

---

## 5. Importing clips

- **File → Import MLV** (<kbd>Ctrl</kbd>+<kbd>I</kbd>) opens the file
  chooser. Drag-and-drop of `.MLV` files onto the preview or session
  browser also works.
- **Spanned MLVs** (`.MLV` + `.M00` + `.M01` + …) are picked up
  automatically as long as all pieces are in the same folder; open the
  lead `.MLV`.
- **Lossless MLVs** are supported transparently — no pre-extract step.
- **File → Fast Open and Import** imports with minimal preflight when
  you trust the clips are well-formed.
- **Audio presence.** Not every MLV carries audio; Magic Lantern's
  WAV recording must be enabled at capture time. To check whether the
  current clip has sound, press <kbd>I</kbd> (**Clip Information**) —
  the panel lists audio sample rate and channel count, or "no audio"
  if the track is empty. **View → Show Audio Track** (<kbd>A</kbd>)
  draws the waveform under the preview if audio is present.

### FCPXML round-trip

**File → FCPXML Import Assistant** reads an FCPXML from your NLE,
lists every MLV it references, asks you where the originals live, and
imports them. Pair with **File → FCPXML Selection Assistant** to
select (or deselect) already-open clips based on whether the NLE used
them. This turns MLV App into the raw backing store for an NLE proxy
workflow: cut on proxies, export FCPXML, round-trip for the grade.

### MAPP — faster re-opens on slow disks

A `.MAPP` sidecar caches all metadata plus video/audio frame indexes,
making slow-disk re-opens nearly instant.

- **Edit → Create MAPP Files** enables generation on future imports.
- **Edit → Create All MAPP Files Now** (<kbd>Alt</kbd>+<kbd>M</kbd>)
  walks the current session and writes every `.MAPP` in one pass.

Deleting a clip from the session also offers to remove its `.MAPP`.

---

## 6. Processing parameters — full tour

All sliders live in the right-hand **Editor panel**. Everything in
this section persists in the receipt and is applied at export time.
Default values listed below are what a fresh receipt starts with;
double-click any slider handle to reset that single parameter.

### 6.1 Profiles

| Control | Options | Default |
|---|---|---|
| **Profile Preset** | Film, Alexa Log-C, Cineon Log, Sony S-Log3, Rec. 709, Fuji F-Log, Canon Log, Panasonic V-Log | Rec. 709 |
| **Gamut** | Rec. 709, Rec. 2020, Canon Cinema | Rec. 709 |
| **Gamma / Transfer** | Rec. 709, Alexa Log C, Cineon Log, Sony S Log | Rec. 709 |
| **Tonemapping** | On / Off | On |

Selecting a **Profile Preset** auto-fills **Gamut**, **Gamma**, and
**Tonemapping** so the four controls behave as a single dropdown for
most users; manually overriding any of the lower three decouples them
and the preset name then reflects that custom mix. To verify you have
a clean log pass for an external colorist, set the preset to (e.g.)
**Sony S-Log3**, leave Exposure / Contrast / Curves at defaults, and
export ProRes 4444 or CDNG. Stacking creative grades on top of a log
profile breaks the log output curve.

### 6.2 Exposure and color

| Parameter | Effect | Default |
|---|---|---|
| **Exposure Correction** | Raw-domain exposure, in stops | 0 stops |
| **Exposure** | Display-referred exposure tweak | 0 |
| **Contrast** | Midtone contrast | 0 |
| **Temperature / Tint** | White balance (Kelvin + green/magenta) | from MLV header |
| **Clarity** | Local contrast | 0 |
| **Vibrance** | Bias saturation to low-sat pixels | 0 |
| **Saturation** | Global saturation | 0 |

Magic Lantern's WB preset (sunny, shade, cloudy, tungsten,
fluorescent, flash, or Kelvin) is auto-loaded on open. Start by
nudging **Exposure Correction** to land highlights in the upper
histogram quartile, then set **Temperature** with the WB picker
(<kbd>B</kbd>), then taste-grade with the rest.

### 6.3 Dark and Light adjustments

| Parameter | Effect | Default |
|---|---|---|
| **Dark Strength / Dark Range** | Pull and reach of the dark adjustment | 0 / 50 |
| **Light Strength / Light Range** | Pull and reach of the light adjustment | 0 / 50 |
| **Lighten** | Global lift that preserves highlights | 0 |

### 6.4 Highlights and Shadows

Classic two-slider highlight/shadow recovery, independent of Dark /
Light above. Both default to 0.

### 6.5 Gradation Curves

The Curves canvas at the top of the section is a parametric tone-curve
editor with two operating modes and four channels, all reachable from
the small **Y / R / G / B** toggle row beneath the canvas.

| Action | How |
|---|---|
| Add a control point | Left-click on the curve line at the desired tonal position |
| Drag a point | Left-click and hold a point, drag |
| Delete a point | Right-click the point |
| Switch channel | Click **Y** (RGB master luminance), **R**, **G**, or **B** |
| Reset one channel | Right-click empty curve space and choose **Reset this curve**, or use the channel's **Reset** button |
| **Reset all curves** | The button below the canvas — restores identity on all four curves |

The mode toggle switches between **RGB mode** (each of R/G/B curves
shapes its channel only — use this for color-grading splits) and **Y
mode** (the master curve drives a luminance-weighted contrast on the
RGB triplet, preserving hue — use this for contrast moves).

**Hue-vs curves.** Below the gradation curves you will find four
additional canvases for **Hue vs. Hue**, **Hue vs. Saturation**,
**Hue vs. Luminance**, and **Luminance vs. Saturation**. They behave
the same way — left-click to add a point, drag to shape, right-click
to delete — but the X-axis is hue (0°–360°) or luminance, and the
Y-axis is the per-zone shift. Use **Hue vs. Sat** to selectively
desaturate green foliage, **Hue vs. Hue** to push a teal-and-orange
look, **Luminance vs. Sat** to crush saturation in the deep shadows.

**Sample workflow — gentle S-curve plus warm shadows:**

1. Open gradation curves, switch to **Y** (master luminance).
2. Click at the 25 % point, drag down 5 % (shadow anchor — pulls
   shadows slightly darker).
3. Click at the 75 % point, drag up 5 % (highlight anchor — pulls
   highlights slightly brighter).
4. Switch to **B** (blue), click at the 20 % point, drag down 10 % —
   removes blue from shadows, warming them.
5. Inspect waveform (<kbd>W</kbd>) and vectorscope (<kbd>V</kbd>);
   adjust until skin tones land between the I and Q lines.
6. Right-click any unwanted point to back out a step.

Curves apply after profile/gamma conversion and before LUT, so a
look-LUT receives the curve-shaped image.

### 6.6 Hue-vs curves

| Curve | Effect |
|---|---|
| **Hue vs. Hue** | Shift one hue to another |
| **Hue vs. Saturation** | Saturation per hue |
| **Hue vs. Luminance** | Brightness per hue |
| **Luminance vs. Saturation** | Saturation per tonal zone |

(Detailed click-and-drag mechanics are described under §6.5 above.)

### 6.7 Sharpen

Single **Sharpen** strength slider applies an unsharp-mask style
high-pass sharpen after debayer. Default 0; useful range 5–25 for
moderate sharpening, beyond 40 starts to ring on edges.

### 6.8 Denoising (stackable)

| Parameter | Effect | Default |
|---|---|---|
| **Median Denoise Window** / **Median Denoise Strength** | 2D median denoiser | 0 / 0 |
| **RBF Denoise Luminance / Chroma / Range** | Recursive bilateral filter | 0 / 0 / 1 |

2D median is cheap; the recursive bilateral filter is higher-quality
but costlier and may also be the trigger that makes
[Use Fast Processing for Playback](#use-fast-processing-for-playback)
auto-disable for the duration of playback. Treat both as polish, not
rescue.

### 6.9 Grain

**Grain Strength** adds synthetic film grain; **Grain Luma Weight**
biases between luma and chroma noise.

### 6.10 Toning

Split-toning for **Highlights** and **Shadows**: Hue chooses the
color, Exposure/Contrast shape the tone.

### 6.11 LUT and 6.12 Filter

LUT loading is covered in [Section 14 — LUTs](#14-luts); film
emulation and other filters in [Section 13 — Filters](#13-filters).

### 6.13 Lens Correction

| Control | Purpose |
|---|---|
| **Vignette Strength / Radius / Shape** | Correct or add vignette |
| **CA Correction Red / Blue** | Per-channel chromatic-aberration correction |
| **Smoothing** | Residual fringing clean-up |

---

## 7. Raw corrections

All raw corrections live in **RAW Correction** in the editor panel
and act on Bayer data *before* debayer. Master toggle: **Enable RAW
Correction**.

### 7.1 Focus pixels

Phase-detect focus pixels show up as hot-pixel-like artefacts.

- **Fix Focus Dots**: on by default for cameras that need it.
- **(Auto Detect)**: uses bundled maps in `pixel_maps/`.
- **Auto Detect Every Frame**: re-runs detection per frame for drifting
  patterns.

Manually install third-party maps via **Edit → Show Installed Focus
Pixel Maps**, or drag any `.fpm` file onto the MLV App window — the
file is copied into the application directory:

| OS | Where `.fpm` files live |
|---|---|
| Windows | Folder containing `MLVApp.exe` (e.g. `C:\Tools\MLVApp\`) |
| macOS | `MLV App.app/Contents/MacOS/` (right-click → **Show Package Contents**) |
| Linux AppImage | Mount path while AppImage is running; for permanent install, `./MLVApp.AppImage --appimage-extract` and place maps in `squashfs-root/usr/bin/` |
| Linux distro packages | The `mlv-app` install prefix, e.g. `/usr/lib/mlv-app/` |

Restart MLV App after installing new maps.

### 7.2 Bad pixels

| Control | Purpose |
|---|---|
| **Fix Bad Pixels** | Master toggle |
| **Use Bad Pixel Map** | Use a bundled or user `.fpm` / `.bpm` |
| **Select Bad Pixels** (<kbd>M</kbd>) | Picker mode — click bad pixels in the preview |
| **Mark Bad Pixel with Cross** | Draw a visible cross where a bad pixel was patched |
| **Delete Current Bad Pixel Map** | Remove the user-generated map |

### 7.3 Chroma smoothing

**Chroma Smooth** suppresses color speckle without blurring luma.
Typical values: 2×2, 3×3, 5×5.

### 7.4 Pattern noise

**Pattern Noise** removes horizontal-banding artefacts common on some
Canon sensors under high ISO.

### 7.5 Vertical stripes

**Vertical Stripes** corrects fixed-pattern columns (5D Mark III and
similar sensors).

### 7.6 Black and white level

**RAW Black Level** and **RAW White Level** override the values stored
in the MLV header. Adjust only if the defaults crush shadows or clip
highlights.

Use **Repair** next to RAW Black Level when shadows look green or pink.
Use **Auto Fix** next to RAW White Level to restore both raw levels for
viewing; double-clicking the white value still resets it to the metadata
default.

### 7.7 Dual ISO

Magic Lantern's Dual ISO alternates scanlines between two ISOs for
extra dynamic range.

| Mode | When to use |
|---|---|
| **Disabled** | Debug / checker-pattern inspection |
| **Preview** | Review / playback — fast reconstruction |
| **Full quality** | Final export — full 20-bit reconstruction |
| **Match Exposures By** | Histogram matching; pick a frame with wide dynamic range |
| **Fullres Blending** | Restore full resolution in blended output |

### 7.8 Dark-frame subtraction

Subtract sensor noise from a dark frame.

| Mode | What it does |
|---|---|
| **Off** | No subtraction. |
| **Internal** | Averages the dark footage carried inside the MLV (only available if the clip was recorded with ML's "Dark Frame" feature). |
| **External** | Uses a separate MLV dark frame shot at the same ISO, Dual ISO mode, and bit depth. After choosing **External**, click the **Dark-frame file** path button to open a file picker; the chosen path is stored in the receipt. |

To produce an external dark frame: open a clip shot lens-capped,
then **File → Export Settings → Codec = MLV → Mode = Averaged frame
(for darkframe creation)** and export. The one-frame `.MLV` is a
valid External dark frame for any clip with matching camera, ISO,
Dual ISO mode, and bit depth.

### 7.9 CropRec

**CropRec** toggles the per-line offset adjustment needed for some
Magic Lantern crop-record modes (1080p crop, 3K, 3.5K, 4K, …) where
the active sensor area starts on a non-default scan line. Enable when
the bundled focus-pixel map for your camera+resolution combination
expects a CropRec-aligned frame and your clip was recorded in one of
those crop-record modes; if you see hot-pixel patterns shifted by one
or two lines after enabling **Fix Focus Dots**, toggle CropRec.

### 7.10 Highlight reconstruction

**Highlight Reconstruction** rebuilds clipped highlight detail.
**Cyan Highlight Fix** removes the cyan cast common in clipped skies.
Enabling Highlight Reconstruction with Cyan Highlight Fix is one of
the receipt combinations that auto-disables
[Use Fast Processing for Playback](#use-fast-processing-for-playback).

### 7.11 Vertical stretch

Vertical stretch is part of **Transformation** and is documented in
full under [§12 — Resize, aspect ratio, rotation](#12-resize-aspect-ratio-rotation).
Most MLV clips encode the correct stretch in metadata and **Height
Stretch → (Auto Detect)** picks it up automatically.

---

## 8. Playback

### Modes

| Mode | Behavior | Use when |
|---|---|---|
| **Show each frame** (default) | Every frame renders; rate may drop below real time | Frame-accurate review |
| **Drop Frame Mode** (Playback menu) | Skip frames to stay real time | Timing / audio review |

> **Note — audio plays only in Drop Frame Mode.** If you press
> <kbd>Space</kbd> in the default mode and hear silence even though
> the clip has audio, this is expected. Switch to **Playback → Drop
> Frame Mode** for audio-synchronous playback. Toggle the waveform
> display with <kbd>A</kbd> (**View → Show Audio Track**).

**Playback → Loop** loops the current clip.

### Navigation

<kbd>Space</kbd> play/pause · <kbd>,</kbd> previous frame ·
<kbd>.</kbd> next frame · <kbd>X</kbd> first frame ·
<kbd>K</kbd> next clip · <kbd>J</kbd> previous clip ·
<kbd>Shift</kbd>+<kbd>I</kbd> / <kbd>Shift</kbd>+<kbd>O</kbd> set
Cut In / Cut Out · drag the timeline handle to scrub.
**Framerate override** in Export Settings changes the export rate only;
it no longer changes playback (see
[High frame rate conform](#high-frame-rate-conform)). There is no
JKL-style reverse playback; previous-frame stepping uses <kbd>,</kbd>.

### High frame rate conform

<a id="high-frame-rate-conform"></a>Clips recorded faster than 30 fps
(for example 59.94 fps) play as slow motion at 24 fps by default, so a
60 fps clip plays at 0.4x speed. Configure it under **Playback → High
Frame Rate Conform**:

| Setting | Default | Choices |
|---|---|---|
| **Conform high frame rate clips** | on | on / off (off plays every clip at its own rate) |
| **Conform to** | 24 fps | 24, 25, 30 fps |
| **For clips above** | 30 fps | 30, 50 fps |

A clip is only ever slowed down, never sped up: a clip at or below the
threshold, or at or below the target rate, plays at its own rate. The
status bar shows the conform, for example `Playback: 24 fps (60 -> 24)`.
The settings are saved as `Playback/ConformEnabled`,
`Playback/ConformTargetFps` and `Playback/ConformThresholdFps`.

Conform is a viewing mode. Timecode, clip info and export keep the
clip's own rate. **Audio is muted while a clip is being conformed**
(its native rate cannot follow slowed-down picture); your **Audio
Output** choice is left as it was and applies again to clips that are
not conformed. Conform is paced through Drop Frame Mode (the default
timing path).

### Zoom and fullscreen

Zoom and fullscreen interactions are detailed in
[§16 Power-user playback overlays](#16-power-user-playback-overlays);
the short version is <kbd>F</kbd> fit, <kbd>Ctrl</kbd>+<kbd>0</kbd>
100 %, mouse wheel free-zoom, <kbd>Ctrl</kbd>+<kbd>F</kbd> fullscreen.

### Debayer for playback

**Playback → Debayer for Playback** chooses the playback debayer:
**Don't Switch Debayer for Playback** uses the receipt's choice;
**AMaZE Cached** waits for cached AMaZE frames and falls back to
bilinear; <a id="use-fast-processing-for-playback"></a>**Use Fast
Processing for Playback** uses the lightweight preview subset when
the receipt is compatible (default-on; auto-disables when the receipt
contains heavy denoise (RBF) above modest thresholds, Highlight
Reconstruction with Cyan Highlight Fix, or any debayer other than
AMaZE / Bilinear; full processing returns the moment playback stops).

**Playback → Playback Quality → Scale Factor** lets you force the
preview size with **Auto**, **x1**, **x2**, or **x4**. Use **x1** for
the sharpest paused view, **x4** for the smoothest preview, and **Auto**
to follow the current quality mode and sampler. The scale control is a
UI equivalent of the old `MLVAPP_PLAYBACK_SCALE_FACTOR` override.

**Playback → Playback Quality → Mode** defaults to **Auto**. Auto starts
from a quality-preserving preview choice, watches the measured playback
cadence, and only sharpens beyond the conservative default after the app has
observed a validated no-readback GPU presentation path in the current playback
run. **Prioritize Quality** keeps the high-quality path and falls back to
software when the scoped GPU path cannot satisfy it. **Prioritize Smoothness**
allows preview-oriented reductions to keep editing responsive, while paused
inspection and export remain full quality unless explicitly changed elsewhere.

---

## 9. Analysis tools

| Scope | Shortcut | What it shows |
|---|---|---|
| **Histogram** | <kbd>H</kbd> | RGB with under/over markers |
| **Waveform** | <kbd>W</kbd> | Luma waveform |
| **RGB Parade** | <kbd>P</kbd> | R/G/B side-by-side |
| **Vectorscope** | <kbd>V</kbd> | Chroma plot with saturation rings |

**White Balance Picker** (<kbd>B</kbd>) enters eyedropper mode — click
a neutral pixel and MLV App computes temperature/tint.
**Clip Information** (<kbd>I</kbd>) lists camera, lens, resolution,
duration, shutter, aperture, ISO, Dual ISO status, bit depth, and
audio-track presence.

---

## 10. Session management

### Session files

Sessions are written as `.marsx` XML and hold every clip path, every
receipt, and the export settings. New session
<kbd>Ctrl</kbd>+<kbd>N</kbd> · Open <kbd>Ctrl</kbd>+<kbd>O</kbd> ·
Save <kbd>Ctrl</kbd>+<kbd>S</kbd> · Save as
<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd>.

An untitled session lives in memory until first **Save** (file dialog
defaults to your platform's Documents folder). Clip paths inside the
`.marsx` are stored as typed on import — typically absolute paths
like `C:\Footage\…\A001.MLV` or `/Volumes/Card/…/A001.MLV` — so a
`.marsx` is not portable across machines unless the destination
mounts the source at the same path. For cross-machine round-trips,
keep clips and the `.marsx` together in a project folder and open the
`.marsx` from there, or use the FCPXML round-trip.

### Selection

Select all <kbd>Ctrl</kbd>+<kbd>A</kbd> · Delete from session
<kbd>Delete</kbd> · Next clip <kbd>K</kbd> · Previous clip <kbd>J</kbd>.
Deleting from the session does not delete the `.MLV` on disk unless
you opt in.

### Receipts

| Action | Shortcut |
|---|---|
| Copy receipt | <kbd>Ctrl</kbd>+<kbd>C</kbd> |
| Paste receipt (acts as **batch paste** when multiple clips are selected) | <kbd>Ctrl</kbd>+<kbd>V</kbd> |
| Reset receipt | <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd> |
| Export receipt to `.marxml` | <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>C</kbd> |
| Import receipt from `.marxml` | <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>V</kbd> |
| Use Default Receipt (user-defined default) | — |

To set up a default receipt: grade one clip how you want new clips
to start, export its receipt with <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>C</kbd>
to a `.marxml` (e.g. `~/MLVApp/default.marxml`). Open **Edit → Use
Default Receipt** — a file picker opens; choose that `.marxml`. New
imports now start from it. Toggle the menu item off to revert to
factory defaults.

### Clip markers

Color-mark clips: <kbd>1</kbd> red · <kbd>2</kbd> yellow · <kbd>3</kbd>
green · <kbd>0</kbd> unmark. The marker filter has its own toggles —
<kbd>Alt</kbd>+<kbd>1</kbd> / <kbd>Alt</kbd>+<kbd>2</kbd> /
<kbd>Alt</kbd>+<kbd>3</kbd> / <kbd>Alt</kbd>+<kbd>0</kbd> each toggle
the visibility of one color class (red / yellow / green / unmarked).
All four are on by default, so the session shows every clip; toggling
<kbd>Alt</kbd>+<kbd>0</kbd> off, for example, hides every clip without
a color marker. Toggle them back on to see those clips again.

### Session list preview

**View → Session List Preview** switches between Disabled, List Mode,
Picture Mode Left, Picture Mode Bottom, and Table Mode Bottom. Per-
clip preview pictures are rendered lazily and cached with the session.

### System integration

**Reveal in Finder / Explorer** (<kbd>Alt</kbd>+<kbd>R</kbd>) and
**Open with External Application** (<kbd>Alt</kbd>+<kbd>A</kbd>). Pick
the external app via **Edit → Select External Application**.
**File → Save Session Metadata** exports a CSV of every clip's
metadata (camera, lens, ISO, duration, receipt name…) for spreadsheet
workflows.

---

## 11. Exporting

Open **File → Export Settings** (<kbd>Ctrl</kbd>+<kbd>,</kbd>), pick
your codec and options, then **File → Export Selected Clips**
(<kbd>Ctrl</kbd>+<kbd>E</kbd>). **File → Export Current Frame**
(<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>E</kbd>) exports a single
still.

### 11.0 Beginner-recommended export recipe

If you have never exported from MLV App before, use these settings
verbatim. They produce an editor-friendly ProRes 422 HQ MOV at the
clip's native resolution, with audio if present, in Rec. 709.

1. **File → Export Settings** (<kbd>Ctrl</kbd>+<kbd>,</kbd>).
2. **Codec** = `Apple ProRes 422 HQ` (10-bit 4:2:2, MOV container).
3. **Debayer** = `Receipt configuration` (AMaZE if untouched).
4. **Resize** = unchecked.
5. **Lock aspect ratio** = leave untouched (irrelevant when Resize
   is off).
6. **Framerate override** = unchecked.
7. **Smooth aliasing** = `Off`.
8. **HDR blending** = unchecked.
9. **Export audio** = checked.
10. **Post Export script** = `None`.
11. **Editor panel → Profiles → Profile Preset** = `Rec. 709`
    (default). Verify with the histogram that highlights are not
    blown.
12. Click **Close**, <kbd>Ctrl</kbd>+<kbd>E</kbd>, pick folder,
    **Save**.

The resulting `.mov` opens in Final Cut Pro, Premiere Pro, DaVinci
Resolve, Avid Media Composer, and ffmpeg-based pipelines without
further conversion.

### 11.1 Cross-cutting options

| Option | Effect | Default |
|---|---|---|
| **Debayer** | Force Bilinear / AMaZE / LMMSE / IGV, or use the receipt | Receipt |
| **Resize** | Target W×H via AVIR; **Lock aspect ratio** auto-fills height | Off (1920×1080 if enabled) |
| **Framerate override** | Change output frame rate | Off (use MLV native) |
| **Smooth aliasing** | Off / 1 pass / 3 pass / 3 pass + unsharp / last resort — ffmpeg-filter moire suppression | Off |
| **HDR blending** | Combine alternate exposures for HDR-shot clips | Off |
| **Export audio** | Include the MLV's audio track | On |
| **Presets** | Save named export presets, one per delivery target | — |

**Smooth aliasing** levels: **Off** = nothing. **1 pass** = single
moiré filter (fast, fixes light moiré). **3 pass** = three passes
for stubborn moiré. **3 pass + unsharp** adds an unsharp-mask to
recover sharpness the moiré pass softened. **Last resort** = heaviest
moiré-removal kernel; it visibly softens fine detail and slows
export by 5–10×, hence the name.

**HDR blending** requires the MLV to have been shot in Magic Lantern's
HDR alternating-exposure mode (one over- and one under-exposed frame
back-to-back at half the recorded frame rate). The HDR flag is read
from the MLV header automatically, so the option is a no-op on
non-HDR clips, but enabling it on a non-HDR clip in a multi-clip
selection can crash the export (see §17.7) — disable unless every
selected clip is an HDR pair.

### 11.2 ffmpeg family

All with optional audio and [Cut In / Cut Out trimming](#117-trimming-cut-in--cut-out).

- **Apple ProRes 422 Proxy / LT / Standard / HQ** (10-bit 4:2:2)
- **Apple ProRes 4444** (10-bit 4:4:4:4)
- **Uncompressed AVI** (YUV420 / V210 / BGR24) — "RAW AVI"
- **H.264** (8-bit)
- **H.265 8-bit 4:2:0 / 10-bit 4:2:0 / 12-bit 4:4:4**
- **TIFF** (16-bit image sequence)
- **PNG Sequence** (8-bit)
- **JPEG2000** (lossless)
- **Motion JPEG**
- **HuffYUV (FFVH)** 10/12/16-bit 4:4:4
- **DNxHD** (8-bit SMPTE VC-3)
- **DNxHR** (10/12-bit VC-3 HR)
- **GoPro CineForm** (10-bit 4:2:2 / 12-bit 4:4:4)
- **VP9** (web delivery)

### 11.3 macOS AVFoundation

Native hardware-accelerated encode, macOS only:

- **12-bit ProRes 422**
- **12-bit ProRes 4444**
- **8-bit H.264**

### 11.4 Cinema DNG

Best format for handing graded raw to Resolve, Baselight, or another
primary color tool.

| Mode | What it does |
|---|---|
| **CinemaDNG Uncompressed** | 10/12/14/16-bit raw samples, no compression |
| **CinemaDNG Lossless** | Lossless JPEG compression — same data, smaller on disk |
| **CinemaDNG Fast Pass** | Raw copied through with **no** RAW correction, processing, or compress/decompress — fastest |

Options: bit depth (10/12/14/16-bit; 20-bit worth of Dual ISO data
packed into 16-bit samples), naming scheme (default or DaVinci
Resolve), per-clip aspect ratio written into the exported DNG header
so downstream tools show the right stretch.

### 11.5 MLV export

Re-wrap or transform to another MLV:

| Mode | Use |
|---|---|
| **Fast pass** | Copy through, trim only |
| **Compressed** | Re-encode losslessly for smaller files |
| **Averaged frame (for darkframe creation)** | Single averaged frame as a future external dark frame |
| **Extract internal darkframe** | Pull out the dark frame the MLV already carries |

### 11.6 Audio only

**WAV (Audio Only)** writes the MLV audio track as a standalone WAV —
useful for NLEs that want side-car audio.

### 11.7 Trimming (Cut In / Cut Out)

Every export mode honors per-clip Cut In and Cut Out. Set them with
the **In** / **Out** buttons above the timeline, or
<kbd>Shift</kbd>+<kbd>I</kbd> / <kbd>Shift</kbd>+<kbd>O</kbd>. Audio
is trimmed in lockstep and the timecode is adjusted so it stays in
sync in an NLE.

### 11.8 Single-frame export

<kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>E</kbd> outputs the playhead
frame in **8-bit PNG**, **CDNG Compressed (lossless)**, or **CDNG
Lossless**.

### 11.9 Post-export scripting (macOS)

**Scripting → Post Export** in Export Settings attaches a `.command`
shell script that runs after each export. macOS only. Scripts ship in
`MLV App.app/Contents/MacOS/bash_scripts/`; any `.command` dropped
there appears in the dropdown after relaunch. Bundled examples:
`HDR_MOV.command`, `PROXY_CLEANER.command`, `TIF_CLEAN.command`,
`enfuse_average.command`.

The script receives **no positional arguments**. Instead, MLV App
writes these files into `/tmp/mlvapp_path/` before launching it:

| File | Contents |
|---|---|
| `app_path.txt` | Absolute path of the export directory |
| `file_names.txt` | One source `.MLV` path per line, one line per exported clip |
| `tif_creation` | Empty file, present only after a TIFF sequence export |

Minimal hook:

```bash
#!/bin/bash
EXPORT_DIR=$(cat /tmp/mlvapp_path/app_path.txt)
echo "Exported to: $EXPORT_DIR"
cat /tmp/mlvapp_path/file_names.txt
osascript -e 'display notification "Export finished" with title "MLV App"'
```

Save as `MyHook.command`, `chmod +x`, drop into `bash_scripts/`,
relaunch, pick **MyHook** in the Post Export dropdown.

### 11.10 Export notification

**Preferences → Notification on Export Finished** fires a system
notification when a batch completes.

### 11.A ffmpeg command-line templates (advanced)

This appendix documents the exact ffmpeg invocations MLV App constructs
for the most-used codecs. Use it when you need to reproduce a MLV App
export from the command line, drive a headless render farm, or feed
the same encoder settings into a Resolve / Premiere round-trip.

The bundled `ffmpeg` lives next to the `MLVApp` binary
(`MLV App.app/Contents/MacOS/ffmpeg` on macOS,
`MLV App\ffmpeg.exe` on Windows, `mlvapp/ffmpeg` on Linux). MLV App
pipes raw 16-bit RGB frames into ffmpeg's stdin via `popen()`
(`platform/qt/MainWindow.cpp:4514` Unix, `:4516` Windows), so every
template below begins `... -f rawvideo -pix_fmt rgb48 -i - ...`.

#### ProRes templates (`MainWindow.cpp:4472-4491`)

| Codec | Profile dropdown | `-c:v` value | `-profile:v` | `-pix_fmt` | Default container |
|---|---|---|---|---|---|
| ProRes 422 Proxy | `CODEC_PRORES422PROXY` (0) | `prores_ks` (or `prores_aw`) | 0 | `yuv422p10` | `.mov` |
| ProRes 422 LT | `CODEC_PRORES422LT` (1) | `prores_ks` (or `prores_aw`) | 1 | `yuv422p10` | `.mov` |
| ProRes 422 Standard | `CODEC_PRORES422ST` (2) | `prores_ks` (or `prores_aw`) | 2 | `yuv422p10` | `.mov` |
| ProRes 422 HQ | `CODEC_PRORES422HQ` (3) | `prores_ks` (or `prores_aw`) | 3 | `yuv422p10` | `.mov` |
| ProRes 4444 | `CODEC_PRORES4444` (4) | `prores_ks` only | 4 | `yuv444p10` | `.mov` |

The `prores_aw` (Anatoliy Wasserman) encoder is selected via
`Encoder = AW` in the ProRes dropdown and is restricted to profiles
0-3 (Proxy through HQ). ProRes 4444 always uses the modern Kostya
Shishkov encoder (`prores_ks`).

Codec defines: `platform/qt/ExportSettingsDialog.h:15-19`. Encoder
selection: `MainWindow.cpp:4474-4476`.

#### Worked example: ProRes 422 HQ at 24p

The full argv MLV App constructs for a ProRes 422 HQ export at
23.976 fps, 1920x1080, with audio (`MainWindow.cpp:4482-4493`):

```bash
ffmpeg \
  -i input_audio.wav -c:a copy \
  -r 23.976 -y -f rawvideo -s 1920x1080 -pix_fmt rgb48 -i - \
  -c:v prores_ks \
  -profile:v 3 \
  -pix_fmt yuv422p10 \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  output.mov
```

Notes:

- `-i - ` reads raw RGB48 frames from stdin (MLV App's pipe).
- `-profile:v` is the integer dropdown index, **not** a string —
  `prores_ks` interprets 0 as Proxy, 1 as LT, 2 as Standard, 3 as HQ,
  4 as 4444 (`MainWindow.cpp:4486` — the integer is reused directly).
- The audio leg `-i input_audio.wav -c:a copy` is inserted *before*
  the `-c:v` token by the build code at `MainWindow.cpp:4493`. The
  audio file path is the temp WAV MLV App writes during export setup.
  Codec choice for the audio leg (`MainWindow.cpp:3697-3700`):
  `aac` for H.264/H.265, `libopus` for VP9, `copy` for everything else
  (including ProRes).
- `-color_primaries bt709 -color_trc bt709 -colorspace bt709` tags the
  output for Rec. 709 display. MLV App also writes Rec. 2020 / DCI-P3
  / SMPTE 170M tags for the matching profiles.

#### DNxHD / DNxHR templates (`MainWindow.cpp:4429-4434`)

```bash
ffmpeg -r <fps> -y -f rawvideo -s <WxH> -pix_fmt rgb48 -i - \
  -c:v dnxhd \
  -vf scale=w=1920:h=1080:in_color_matrix=bt601:out_color_matrix=bt709,fps=24000/1001,format=yuv422p10le \
  -b:v 365M \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  output.mov
```

The exact `-vf` filter string and `-b:v` bitrate vary by resolution
and frame rate (`MainWindow.cpp:4327-4419`); the constants encode the
SMPTE VC-3 spec table.

#### CineForm template (`MainWindow.cpp:4444-4451`)

```bash
ffmpeg -r <fps> -y -f rawvideo -s <WxH> -pix_fmt rgb48 -i - \
  -c:v cfhd \
  -quality 5 \
  -pix_fmt yuv422p10le  # or gbrp12le for 12-bit \
  -color_primaries bt709 -color_trc bt709 -colorspace bt709 \
  output.mov
```

`-quality` 1-5 maps directly to the Quality dropdown.

#### Reproducing a MLV App export from the command line

To get an exact match:

1. Read the MLV with `mlv_dump --dng` (or your own RAW reader) at the
   bit depth MLV App was outputting.
2. Apply your `.marxml` receipt with the headless `--batch` driver
   (see `01 §15` and `02 §9.2`) to write a 16-bit TIFF or rgb48 stream.
3. Pipe the rgb48 stream into one of the ffmpeg templates above.

The 16-bit TIFF route guarantees bit-identical pixels going into
ffmpeg; the rgb48 stdin route avoids the TIFF round-trip and matches
exactly what MLV App pipes in production.

---

## 12. Resize, aspect ratio, rotation

### AVIR resize

The **Resize** block in Export Settings uses the vendored
[AVIR](https://github.com/avaneev/avir) library (sinc-based, SIMD
accelerated) — significantly better than bilinear for 2×+ downscales.
**Lock aspect ratio** auto-fills the other dimension.

### Stretch

Magic Lantern crop-record modes produce non-square pixels. Correct in
**Transformation**:

| Axis | Presets |
|---|---|
| **Width Stretch** | 1.0×, 1.25×, 1.33×, 1.5×, 1.67×, 1.75×, 1.8×, 2.0× |
| **Height Stretch** | 1.0×, 1.67×, 3.0×, 0.33× (autodetected on recent MLVs via **(Auto Detect)** in the dropdown) |

Stretch applies to preview, playback, and export. Manual stretch is
written into the CDNG header on export so downstream tools see the
correct aspect.

**Worked examples — when to pick which preset:**

- 5D Mark III 3K crop-record (1920×1280, binned vertical lines):
  **Width = 1.0×, Height = 1.67×** restores square-pixel aspect.
- EOS-M 1736×976 through a 1.33× anamorphic adapter: **Width = 1.33×,
  Height = 1.0×** for a 2.35:1 wide.
- 5D Mark III line-skip 1920×1080 (one line in three): **Height = 3.0×**
  (legacy preset).
- 50D / 5D2 line-skip prototypes (one line in five): older builds
  recorded a 5.0× factor; modern firmwares write 0.33× / 1.67× and
  Auto Detect handles them.

If autodetect picks the wrong factor, override manually; the override
saves with the receipt and survives reimport.

### Upside-down

**Transformation → Upside Down** flips the frame 180°.

---

## 13. Filters

In the Editor panel, expand **Filter** and toggle **Enable Filter**,
then choose a look from the dropdown:

| Filter | Character |
|---|---|
| **Film "FJ"** | Fujifilm-style |
| **Film "Vis3"** | Kodak Vision 3-style |
| **Film "P400"** | Kodak Portra 400-style |
| **Film "E100"** | Kodak Ektachrome E100-style |
| **Toy Camera** | Exaggerated low-fi |
| **Sepia Tone** | Monochrome sepia |
| **Cinematic 1 / 2 / 3** | Shifted shadows / highlights cinematic looks |

The four **Film** entries are **neural-network-backed**: they are
small CPU-side feed-forward neural networks trained against scans of
the named real film stocks (FJ = Fuji 250D / 500T mix; Vis3 = Kodak
Vision3 250D / 500T mix; P400 = Kodak Portra 400; E100 = Kodak
Ektachrome E100). The non-Film entries (**Toy Camera**, **Sepia**,
**Cinematic 1/2/3**) are deterministic colour transforms, not neural.

Filters that live outside this section but still shape the look:
moiré/smooth aliasing (export only, [Section 11.1](#111-cross-cutting-options)),
2D median + recursive bilateral denoisers
([Section 6.8](#68-denoising-stackable)), vignette + chromatic
aberration correction ([Section 6.13](#613-lens-correction)).

---

## 14. LUTs

In the Editor panel, expand **LUT** and toggle **Enable LUT**, then
**Load LUT** to pick a `.cube` file. Both 1D and 3D `.cube` files are
supported. **Previous LUT** / **Next LUT** cycle through every LUT in
the same folder — handy for scrubbing through a whole pack. The LUT
pass runs after gamma/profile conversion and before output.

---

## 15. Batch workflows and CLI

- **Multi-clip export**: select multiple clips in the session browser,
  then **File → Export Selected Clips** (<kbd>Ctrl</kbd>+<kbd>E</kbd>).
  All export families (ffmpeg, AVFoundation, CDNG, MLV) respect the
  selection.
- **Batch CDNG**: CDNG export creates one folder per clip; naming
  scheme (default / DaVinci Resolve) is honored per clip.
- **Batch paste receipt**: grade a hero clip,
  <kbd>Ctrl</kbd>+<kbd>C</kbd>, <kbd>Ctrl</kbd>+<kbd>A</kbd>,
  <kbd>Ctrl</kbd>+<kbd>V</kbd> — every clip takes the same look in two
  keystrokes.
- **Post-export scripting** (macOS): see
  [Section 11.9](#119-post-export-scripting-macos).

### CLI flags

```bash
MLVApp --batch <session.marsx>
MLVApp --trim-mlv <in.mlv> <out.mlv> <start_frame> <end_frame>
```

`--batch` opens the named session, runs every clip's export per the
saved Export Settings, then exits (no GUI). Progress to STDOUT,
errors to STDERR. Exit 0 on success, non-zero on any failure
(missing file, codec error, disk full). Render-server wrapper:

```bash
MLVApp --batch myjob.marsx > myjob.log 2>&1
if [ $? -eq 0 ]; then echo "OK"; else echo "FAIL"; fi
```

`--trim-mlv` is a zero-copy MLV trim — copies frames `start_frame`
through `end_frame` (inclusive, zero-based) to the output `.MLV`,
preserves metadata. Same exit-code contract. Full flag reference in
[02 — Developer Guide](02-developer-guide.md).

---

## 16. Power-user playback overlays

This section consolidates the zoom, zebra, and live-scope controls
referenced from §8 and §9. Use it as the canonical reference; the
earlier sections only summarise.

**Zoom**: <kbd>F</kbd> fit · <kbd>Ctrl</kbd>+<kbd>0</kbd> 100% ·
mouse wheel free zoom around cursor · scroll bars or middle-click
drag to pan when zoomed past fit. **View → Better Resizer for
Viewer** switches the on-screen scaler to AVIR for higher-quality
preview at modest CPU cost.

**Fullscreen**: <kbd>Ctrl</kbd>+<kbd>F</kbd> (**View → Fullscreen**)
hides everything but the preview. Press again to return.

**Zebras**: <kbd>Z</kbd> (**View → Show Zebras**) overlays a zebra
pattern where the image is at or above clipping. On the experimental
GPU viewport, zebras are drawn via a shader and run free.

**Scopes during playback**: all four scopes update live. On slower
machines, toggling scopes off
(<kbd>H</kbd> / <kbd>W</kbd> / <kbd>P</kbd> / <kbd>V</kbd>) frees up
CPU for real-time playback.

---

## 17. Troubleshooting

### 17.1 "Qt6Core.dll was not found" (Windows)

The `.exe` cannot see the Qt runtime beside it.

- Do not move `MLVApp.exe` out of the extracted zip folder.
- If you built from source, re-run `windeployqt`:

  ```powershell
  C:\Qt\6.10.2\mingw_64\bin\windeployqt.exe MLVApp.exe --release --no-translations --no-compiler-runtime
  ```

- Set `QT_OPENGL=desktop` to force desktop OpenGL over ANGLE.

See the project's `AGENTS.md` at the repo root and
[02 — Developer Guide](02-developer-guide.md) for a deterministic
Windows launch recipe.

### 17.2 Linux playback stutter

- Switch to **Playback → Drop Frame Mode**.
- Toggle off scopes you do not need.
- Use a lighter playback debayer (**Bilinear** or **AMaZE Cached**).
- Enable **Use Fast Processing for Playback**.
- Move clips from network or USB storage to a local SSD.

### 17.3 Dual ISO checkerboard or banding

- Switch **Dual ISO** from **Disabled** to **Preview** (review) or
  **Full quality** (export).
- Try **Match Exposures By → Histogram** on a frame with wide
  dynamic range.
- Enable **Dark-Frame Subtraction** with a dark frame shot at the
  same Dual ISO setting and bit depth.

### 17.4 Focus-pixel autodetect miss

- Set **Fix Focus Dots → Auto Detect Every Frame**.
- Check **Edit → Show Installed Focus Pixel Maps**; install the
  right map for your camera (see §7.1 for per-OS map locations) and
  restart.

### 17.5 macOS: "MLV App.app is damaged and can't be opened"

Gatekeeper. **System Settings → Privacy & Security → Open Anyway**.
Or from Terminal:

```bash
xattr -rd com.apple.quarantine "/Applications/MLV App.app"
```

### 17.6 macOS: cannot read files on an external drive

Grant **Full Disk Access** to MLV App in **System Settings → Privacy
& Security → Full Disk Access**.

### 17.7 Exports crash partway through

- Uncheck **HDR blending** unless every selected clip is an HDR pair.
- Try a different codec (ProRes 422 HQ before H.265 12-bit).
- Reduce or disable **Resize**.
- Make sure the export folder has enough free disk space — CDNG
  uncompressed is large.

### 17.8 The preview is solid black after import

- Make sure **Editor panel → RAW Correction → Enable RAW Correction**
  is on; with it off and an exotic clip, Exposure Correction at 0
  can leave the frame underexposed below visibility.
- Drag **Exposure Correction** up by 2–3 stops to confirm the frame
  carries data.
- Press <kbd>X</kbd> to jump to the first frame in case the clip
  starts with a few black pre-roll frames.
- Try a different debayer in **Playback → Debayer for Playback**;
  rare combinations of GPU debayer + experimental viewport can fall
  back silently.
- If the histogram is empty, the MLV header may be corrupt — open the
  clip in another MLV viewer to confirm, then re-record if possible.

### 17.9 Where else to ask

- Wiki: <https://github.com/ilia3101/MLV-App/wiki>
- Magic Lantern forum thread:
  <https://www.magiclantern.fm/forum/index.php?topic=20025.0>
- Bug tracker: <https://github.com/ilia3101/MLV-App/issues>

---

## 18. Keyboard shortcuts and power-user tips

### 18.1 Shortcut reference

Every shortcut below is verified against `platform/qt/MainWindow.ui`
in the 1.15.0.0 source. Modifiers use the local convention: on macOS,
`Ctrl` reads as `Cmd`.

| Group | Action → Shortcut |
|---|---|
| **Session / files** | New session <kbd>Ctrl</kbd>+<kbd>N</kbd> · Open <kbd>Ctrl</kbd>+<kbd>O</kbd> · Save <kbd>Ctrl</kbd>+<kbd>S</kbd> · Save as <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>S</kbd> · Import MLV <kbd>Ctrl</kbd>+<kbd>I</kbd> · Select all <kbd>Ctrl</kbd>+<kbd>A</kbd> · Delete <kbd>Delete</kbd> · Next/prev clip <kbd>K</kbd>/<kbd>J</kbd> · Clip info <kbd>I</kbd> · Reveal in Finder/Explorer <kbd>Alt</kbd>+<kbd>R</kbd> · Open in external app <kbd>Alt</kbd>+<kbd>A</kbd> · Create All MAPP <kbd>Alt</kbd>+<kbd>M</kbd> · Minimize <kbd>Ctrl</kbd>+<kbd>M</kbd> · Quit <kbd>Ctrl</kbd>+<kbd>Q</kbd> |
| **Export** | Settings <kbd>Ctrl</kbd>+<kbd>,</kbd> · Export selected <kbd>Ctrl</kbd>+<kbd>E</kbd> · Export current frame <kbd>Ctrl</kbd>+<kbd>Shift</kbd>+<kbd>E</kbd> |
| **Receipts** | Copy <kbd>Ctrl</kbd>+<kbd>C</kbd> · Paste (also batch) <kbd>Ctrl</kbd>+<kbd>V</kbd> · Reset <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>R</kbd> · Export <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>C</kbd> · Import <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>V</kbd> |
| **Playback** | Play/pause <kbd>Space</kbd> · Prev/next frame <kbd>,</kbd>/<kbd>.</kbd> · First frame <kbd>X</kbd> · Cut In/Out <kbd>Shift</kbd>+<kbd>I</kbd>/<kbd>Shift</kbd>+<kbd>O</kbd> · Toggle timecode <kbd>T</kbd> |
| **Viewport** | Fit <kbd>F</kbd> · 100% <kbd>Ctrl</kbd>+<kbd>0</kbd> · Fullscreen <kbd>Ctrl</kbd>+<kbd>F</kbd> · Zebras <kbd>Z</kbd> · WB picker <kbd>B</kbd> |
| **Scopes** | Histogram <kbd>H</kbd> · Waveform <kbd>W</kbd> · Parade <kbd>P</kbd> · Vectorscope <kbd>V</kbd> |
| **Panels** | Session area <kbd>S</kbd> · Edit area <kbd>E</kbd> · Audio track <kbd>A</kbd> · Bad-pixel picker edit <kbd>M</kbd> |
| **Marks** | Red/Yellow/Green/Unmark <kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd>/<kbd>0</kbd> · Show/hide red/yellow/green/unmarked <kbd>Alt</kbd>+<kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd>/<kbd>0</kbd> |

Shortcuts not listed above are visible in the **Edit → Preferences**
dialog.

### 18.2 Power-user tips

- **Batch grade in two keystrokes**: grade one hero clip,
  <kbd>Ctrl</kbd>+<kbd>C</kbd>, <kbd>Ctrl</kbd>+<kbd>A</kbd>,
  <kbd>Ctrl</kbd>+<kbd>V</kbd>.
- **Pre-build MAPPs overnight**: <kbd>Alt</kbd>+<kbd>M</kbd> turns a
  folder of 14-bit MLVs on HDD from "slow to open" into "instant".
- **Audio sync check**: switch to Drop Frame Mode + **Show Audio
  Track** to catch dropouts before you notice them visually.
- **Save export presets**: one per delivery (proxy, master,
  CDNG-for-Resolve) instead of retyping fields.
- **Fullscreen loop**: <kbd>Ctrl</kbd>+<kbd>F</kbd> →
  <kbd>Space</kbd> → <kbd>Z</kbd> gives a clean review loop with
  zebras.
- **Per-export debayer**: leave the receipt on AMaZE for finals; the
  Export Settings debayer override lets you force Bilinear for fast
  proxies without touching the receipt.

---

## 19. Further reading

- **Developer Guide — [docs/02-developer-guide.md](02-developer-guide.md)**:
  clone, compile on Windows / macOS / Linux, run the test suite,
  contribute upstream. Full reference for `--batch`, `--trim-mlv`,
  and `--profile-playback`.
- **Build on Windows — [docs/10-build-windows.md](10-build-windows.md)**:
  step-by-step Qt + MinGW + ffmpeg recipe.
- **Build on macOS / Linux — [docs/11-build-macos-linux.md](11-build-macos-linux.md)**:
  Homebrew formulae, `qmake` invocation, AppImage packaging.
- **Technical Specification — [docs/03-technical-specification.md](03-technical-specification.md)**:
  architecture, data flow, threading model, MLV format internals,
  debayer and processing pipeline contracts, experimental GPU paths.
- **External Auditor Guide — [docs/04-external-auditor-guide.md](04-external-auditor-guide.md)**:
  reproduce builds, verify golden outputs, check licensing, navigate
  the codebase cold.
- **Test fixtures and golden corpus — [docs/15-test-fixtures.md](15-test-fixtures.md)**:
  the canonical Dual ISO and lossless MLV fixtures used by the test
  suite, useful as known-good inputs while learning the app.
- **Project Wiki**: <https://github.com/ilia3101/MLV-App/wiki>
- **Magic Lantern forum thread**:
  <https://www.magiclantern.fm/forum/index.php?topic=20025.0>
- **Tutorial videos** (courtesy of Maksim Danilov, supplementary to
  this guide — the §4.5 "Your first 5 minutes" walkthrough above is
  the canonical first-time experience):
  Russian with subtitles <https://www.youtube.com/watch?v=X17jzHjuHOo>,
  English <https://www.youtube.com/watch?v=-mmnG5uBJok>.
- **Bug tracker**: <https://github.com/ilia3101/MLV-App/issues>
- **Official release site**: <https://mlv.app>

---

*Matches MLV App 1.15.0.0 as shipped from `master`. If an option in
your build does not match what is written here, check the commit
history on [GitHub](https://github.com/ilia3101/MLV-App) — MLV App
is under active development.*
