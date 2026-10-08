# Look Assist flavors: Classic | Cinematic | Film grade

Look Assist analyses a clip (scene verdict, exposure, white balance) and writes a handful of receipt sliders.
Since LOOK-ASSIST-FLAVORS-1 it can do so in one of two flavors, and since LOOK-ASSIST-FILM-FLAVOR-1 in a third
(Film grade, below). The owner's directive (2026-09-26): *video shouldn't look raw or unprocessed with look
assist active; it should have an aesthetic cinematic color grade.*

* **Classic** is Look Assist exactly as it was: the same sliders, the same receipt, the same picture, in every
  state. It is the default.
* **Cinematic** is the same analysis, the same scene verdict and the same white-balance decision, with one table
  of deltas laid over the same sliders. It is applied **only** through the sliders Look Assist already owns:
  contrast, pivot, shadows, highlights, vibrance. It never touches the exposure (Classic's, exactly, including the
  scene limits Classic applied) or the white balance (temperature / tint).
  Saturation and the tone curve are not Look Assist sliders (no preset field, no baseline to restore), so they are
  not used; the modest extra colour comes through vibrance.

The code is one table, `kCinematicFlavorDeltas` in `src/batch/LookAssistAnalysis.cpp`, read through
`lookAssistCinematicDeltasForScene()`. `presetForLookAssistScene()` takes a trailing `flavor` argument that
defaults to Classic; Classic returns before any flavor code runs. The flavor is laid over a preset by
`lookAssistApplyFlavorDeltas()`, which `presetForLookAssistScene()` itself ends in.

## The Cinematic table

Additive deltas over the Classic preset the scene and the statistics produced, then clamped to the slider range.
Exposure is not re-clamped: whatever Classic's exposure is (Night's floor, BrightSun's ceiling, a display-meter
lift), Cinematic's is that value. A test pins this table against the code
(`LookAssistFlavors.TheDocumentedTableIsTheCodeTable`).

<!-- cinematic-table:begin -->
| Scene | Exposure | Contrast | Pivot | Shadows | Highlights | Vibrance |
|---|---|---|---|---|---|---|
| Night | 0 | +20 | -3 | -10 | -10 | +4 |
| ArtificialLights | 0 | +32 | -5 | -14 | -12 | +5 |
| Shade | 0 | +40 | -5 | -20 | -15 | +6 |
| BrightSun | 0 | +30 | -5 | -12 | -10 | +5 |
<!-- cinematic-table:end -->

Intent, column by column:

* **Contrast +**: the S-curve. The app's contrast curve darkens the mid-tones and rolls the top off, so the picture
  is lower-key and richer than Classic's.
* **Pivot -**: gives back a little of the mid-tone brightness the contrast takes (measured on the fixtures: a
  lower pivot brightens, a higher one darkens).
* **Shadows -**: lifted-but-not-milky blacks. Classic lifts the shadows (that is what makes a dark clip visible);
  Cinematic lifts them less. Night keeps most of its rescue lift.
* **Highlights -**: highlights rolled off harder than Classic: controlled, not clipped-looking.
* **Vibrance +**: modest. Richer colour, never a saturation push.
* **Exposure 0**: deliberately held at Classic's. The white-balance refinement renders the picture at the preset's
  exposure, so an exposure delta would move the balance; with none, the headless white balance is Classic's by
  construction (a test pins temperature, tint and the headless picture byte for byte).

### The GUI's white-balance walk

The GUI's post-balance walk renders the picture through the live processing object, so the grade the sliders
hold would steer the temperature and tint it steps to. It therefore runs on the **Classic** preset whatever the
flavor; only then are the flavor's tone sliders laid over the finished balance (`lookAssistApplyFlavorDeltas()`),
and the picture the user will see is measured once more for the diagnostics and the safety guard only. The
balance the GUI settles on is the Classic one by construction, and a test pins that the overlay is exactly the
preset the one call makes. (The opt-in async worker, `MLVAPP_LOOK_ASSIST_ASYNC=1`, measures the picture through
the same live object after applying a preset; it is not the default path and is not covered by this claim.)

### What it does to the tracked fixtures

Frames of `tests/fixtures/clips/large_dual_iso.mlv`, sliders applied the way the app applies them (luma 0..255,
percentiles over the frame; the fixture is a flat, dim dual-ISO test clip, so the absolute picture is plain):

| Frame | Flavor | p5 | median | p95 | mean saturation |
|---|---|---|---|---|---|
| 0 | raw (Look Assist off) | 41 | 63 | 98 | 0.395 |
| 0 | Classic | 100 | 128 | 162 | 0.218 |
| 0 | Cinematic | 85 | 115 | 153 | 0.258 |
| 10 | Classic | 27 | 126 | 162 | 0.264 |
| 10 | Cinematic | 18 | 112 | 152 | 0.309 |

The contrast, shadows and highlights sliders move the picture less than their numbers suggest (the app's curve is
gentle), which is why the deltas are larger than the Classic presets they sit on.

## Film grade

The owner (2026-10-08): Classic and Cinematic look too similar; a third flavor should read as a colour-graded,
big-studio film look. **Film = Cinematic's tone (the same table, reused) + a blue-amber split-tone in the R and B
gradation curves.** There is no new engine stage, no LUT, no hue curve, no toning and no profile change; Y and G
stay identity. Cool, slightly teal shadows and warm highlights, with the mid-tones pinned at 0.45 (about Look
Assist's display median). The selector label is *Film grade*, not *Film*: the Profile preset *Film* is a different
thing.

Control points, display-referred 0..1, per scene strength `s`, with `a = 0.022 s` and `b = 0.030 s`:

* R: (1e-5,1e-5) (0.18, 0.18 - a) (0.45, 0.45) (0.72, 0.72 + b) (1,1)
* B: (1e-5,1e-5) (0.18, 0.18 + a) (0.45, 0.45) (0.72, 0.72 - b) (1,1)
* Y and G: the default two points (1e-5,1e-5) (1,1).

The code is one table, `kFilmGradeStrength` in `src/batch/LookAssistAnalysis.cpp`, read through
`lookAssistFilmGradeForScene()`; `lookAssistFilmGradationCurve()` writes the receipt's `gradationCurve` string in the
Curves widget's own format, so a widget round trip is byte-stable. A test pins this table against the code
(`LookAssistFlavors.TheDocumentedFilmTableIsTheCodeTable`).

<!-- film-table:begin -->
| Scene | s | R@0.18 | B@0.18 | R@0.72 | B@0.72 |
|---|---|---|---|---|---|
| Night | 0.5 | 0.169 | 0.191 | 0.735 | 0.705 |
| ArtificialLights | 0.75 | 0.1635 | 0.1965 | 0.7425 | 0.6975 |
| Shade | 1.0 | 0.158 | 0.202 | 0.750 | 0.690 |
| BrightSun | 0.9 | 0.1602 | 0.1998 | 0.747 | 0.693 |
<!-- film-table:end -->

**Why it cannot tint magenta.** R and B move by equal and opposite amounts at every knot, and the engine's spline
(`tk::spline`, natural cubic, `src/processing/interpolation/spline_helper.cpp`) is linear in y, so
`gcurve_r[v] + gcurve_b[v] == 2 * gcurve_g[v]` to within rounding: the green-magenta axis `G - (R+B)/2` is untouched.
A test pins this on all 65536 entries.

**Why it costs nothing per pixel.** The gradation curves are applied unconditionally on every route (identity
tables when unused): the CPU 16-bit loop, both direct8 kernels, and the CUDA display shader, which composes them
into its one creative-curve texture. Direct8 eligibility and the display shader's refused stages do not look at
them. A Film grade is a one-time spline rebuild and one texture upload when it is applied.

**The user's curve wins.** If the receipt's gradation curve is not the default (numerically: four lines, each
exactly the two identity points within 1e-6; an empty string counts as default), Film does **not** overwrite it: it
lays the tone deltas only and reports `grade=skipped_user_curve`.

**The baseline.** The curve Film replaced is kept in the receipt element `lookAssistBaselineGradationCurve`, written
**only while it is non-empty** (a Classic or Cinematic receipt is byte-identical to before). Look Assist's baseline
restore (GUI `restoreLookAssistBaseline`, the safety fallback, headless `restoreHeadlessLookAssistBaseline`) puts
that curve back and clears the element, so every analysis measures at the user's own curve; capturing a fresh
baseline does the same first. Switching Look Assist off, or re-running as Classic or Cinematic, therefore leaves no
trace of the grade. The white balance stays Classic's by construction, as for Cinematic. Headless batch exports
CDNG (raw), so the grade never reaches a CDNG; headless only keeps the receipt consistent.

Licence: the control points are original and are evaluated by the already-vendored `tk::spline`; no third-party
LUT or colour-science maths is borrowed.

## Choosing the flavor

Layers, first non-empty one wins (`lookAssistSelectFlavor()`):

1. `MLVAPP_LOOK_ASSIST_FLAVOR=classic|cinematic|film`: for runs (the venue legs set it). Case and whitespace do not matter.
2. The receipt's `<lookAssistFlavor>` element, which the headless / batch path reads. It is **written only for a
   non-Classic flavor**, so a Classic receipt is byte-identical to what it always was; absent means Classic.
3. The GUI's *Look Assist flavor* selector next to *Auto Look Assist*, persisted as the app setting
   `lookAssistFlavor` (default `classic`). Changing it with Look Assist on **re-runs the analysis** with the new
   flavor: the "already applied" marker (`LookAssistAppliedMarker::flavorChanged`) forgets the clip so the
   toggle's frame-ready step does not skip it, and the result line logs `flavor=<new>`. A receipt that declares
   a flavor shows it in the selector when it is loaded.

An unknown value is **Classic**, never a fall through to a lower layer, and is logged: the headless applier prints
`[BATCH] WARNING LOOK_ASSIST unknown flavor '<value>' from <env|receipt>; using classic`, the GUI logs
`look_assist.flavor.unknown_value` (for the environment, the app setting **and** a receipt element, through the one
rule `lookAssistSelectorValueForReceipt()` / `lookAssistSelectFlavor()`; an unknown receipt value shows Classic in
the selector and is corrected on the receipt).

## Reporting

The flavor applied is always reported, appended to the end of the existing lines (never inserted):

* GUI `look_assist.apply.result`: `... next_serial=<n> <decision trace fields> flavor=<classic|cinematic|film>` (the trace is LOOK-ASSIST-DIAG-LOGGING-1's, `has_ev100=` .. `playback_scale=`; flavor comes after it).
* GUI `look_assist.apply.async_dispatch`: `... floor_lifted=<0|1> flavor=<...>`.
* Headless `[BATCH] LOOK_ASSIST applied ...`: `... initialPatchFinalChroma=<x> <decision trace fields> flavor=<...>`.
* On those three lines a Film apply appends ` grade=<film-v1|skipped_user_curve|none>` after `flavor=`; Classic and
  Cinematic lines carry no `grade=` field (they stay byte-identical), and a reader takes its absence as `none`. The
  venue's GUI-smoke result carries it as `visualQuality.lookAssist.presetGrade`.
* `gui_smoke.visual_state`: `... gpu_preview_processing_reject_reason=<r> look_assist_flavor=<...|none>`. It
  names a flavor only for a look that is on screen: `none` until an analysis lands, and `none` again after the
  safety fallback restored the baseline (a sheet is then never labelled with a grade it does not show). This is
  what the venue job reads: its summary carries `lookFlavorReported`, and the leg receipt's
  `look.lookFlavorHonored` is `true` only when the app's own report equals the flavor the leg asked for
  (see [dual-venue-evidence.md](dual-venue-evidence.md)).
* The receipt: `lookAssistFlavor` (non-Classic only). It records the flavor that was **applied**, never the
  merely selected one: `setReceipt` and the venue telemetry read the same holder (`LookAssistFlavorOutcome`),
  which an analysis fills when it lands. After a **safety fallback** restored the baseline sliders (for example
  the global-green-cast guard) the receipt names Classic (the element is then left out of the file), so a
  Cinematic selector never exports a Cinematic label over baseline sliders. A clip with no recorded outcome (Look
  Assist off, or the analysis still pending) keeps the selector's value, which is the clip's setting. Switching
  Look Assist off, or an analysis starting, clears the outcome.

## Tests

* `tests/console/test_look_assist_flavors.cpp`: Classic equals master's preset on a 5808-line grid (golden hash
  dumped from an unchanged master tree); the table; Cinematic = Classic + table (clamped), exposure exactly
  Classic's (including a BrightSun display-meter lift), deterministic, never the white balance; laying the flavor
  over a Classic preset is the Cinematic preset; the selector's layers and the unknown-value rule; an unknown
  receipt value resolves to Classic plus a warning; **behavioural**: after a Classic apply, a flavor change makes
  the marker forget the clip so the analysis runs again and grades Cinematic (a mutation that does not clear the
  marker fails it); **behavioural**: a Cinematic selection that ends in the safety fallback records no Cinematic
  flavor on the receipt, a normal Cinematic success records it (a mutation that stamps the selector fails the
  source pin on `setReceipt`; one that records a fallback as applied fails the holder test); the receipt element is read and written only for a non-Classic flavor; every preset call in
  both consumers passes the flavor; the scene verdict stays flavor-blind; the GUI selector and the environment
  both reach the analysis.
* `tests/pipeline/test_look_assist_flavors.cpp`: on the tracked fixture frames, Classic reproduces master's
  receipt sliders, applied line (but for the appended field) and rendered-picture sha256
  (`tests/fixtures/look_assist_flavor_classic_baseline.txt`); Cinematic changes only the documented sliders, keeps
  the white balance and the headless picture, and is a different, deterministic picture once the sliders are
  applied; an unknown environment value is Classic with a warning; the receipt element sits below the
  environment. With `MLVAPP_FLAVOR_SHEET_DIR` set, `LookAssistFlavorsFixture.ContactSheets` writes raw | classic |
  cinematic | film renders of the tracked fixtures (fixture renders only) for model judging.
* Film grade: the documented table is the code's and Film's tone is Cinematic's; the selector accepts `film`; the
  curve built through `processingSetGCurve` leaves Y and G exactly default and keeps `|r + b - 2g| <= 2` on every
  entry; the four tables are pinned by sha256 and sampled values; a user curve is kept and reported; the baseline
  round trip leaves a re-run-as-Classic receipt identical to a Classic-only one; Look Assist off changes nothing;
  the white balance is Classic's on the fixtures; the CUDA display shader and the CPU direct8 route both take the
  Film curves (`EngineAnchoredLookAssistFlavorsMatchEngineWithReceiptSCurve`, case `look_assist_film_night_real_frame`).
