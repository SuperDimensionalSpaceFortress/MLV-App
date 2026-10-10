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
big-studio film look. **Film = Cinematic's tone (the same table, reused) + a colour grade in the gradation curves.**
There is no new engine stage, no LUT, no hue curve, no toning and no profile change. The selector label is *Film grade*,
not *Film*: the Profile preset *Film* is a different thing.

The grade in force is **film-v3** (LOOK-ASSIST-FILM-FLAVOR-3). It replaced film-v2 (#328), which replaced film-v1
(#319). The owner found v1 too subtle, so v2 made a stronger teal-shadow / warm-highlight split through the same
per-channel R, G, B curves, plus a gentle black lift and a soft highlight shoulder in the Y curve and a skin-hue guard
built into the curve shape. The owner then found Film warm (2026-10-09). v3 keeps v2's split and Y line and takes out
its blue-amber lean.

### Why v2 was warm

v2's warm half (peak at 0.52, a 0.3 w tail to 0.85) covered about three quarters of the tonal range and its teal half
a fifth. On a neutral ramp the lean, mean((R + G) / 2 - B), was +5.9 / 255 amber at Shade: an evenly exposed picture
went amber by construction (LOOK-ASSIST-WARMTH-MEASURE-1). v3 moves the warm peak down to 0.48, onto M16-1243's
highlight band (p70..p95, about 0.41..0.585), and drops the upper tail: the split is back to identity at 0.75. The
upper tail added about 3.2 / 255 of v2's ramp lean and nothing to its split on M16-1243.

**The white balance is not this.** Look Assist's exposure and white-balance solve are flavor-blind and unchanged; the
amber of the M16-1243 picture common to every flavor is mostly the scene, AgX and the white balance, not Film. That is
a separate card.

### Why v1 was subtle

v1 split R and B about a pivot at 0.45, with knots at 0.18 and 0.72. On the benchmark clip (M16-1243) the trio's
highlight band, BT.601 luma p70..p95, is about 80..145 codes (0.31..0.57). That band straddles the 0.45 pivot, where R
goes negative below it and positive above it, so v1's warm half netted about zero there. Its whole split came from the
shadow knot, about 6 codes of B-R. v2 moves the knots onto the tonal bands real footage grades in.

### The recipe

Control points, display-referred 0..1, per scene strength `s`. With `t = 0.038 s` (teal), `w = 0.024 s` (warm),
`k = 0.15` (G's share, not scaled), `L = 0.020 s` (black lift), `h = 0.010 s` (shoulder), `c = 0.030 s` (white roll):

* Y: (1e-5, L) (0.50, 0.50) (0.80, 0.80 - h) (1, 1 - c). This gives a gentle black lift (about 5 codes at Shade) and
  a soft shoulder that rolls white to about 0.97. It is v2's line, unchanged.
* R: (1e-5,1e-5) (0.10, 0.10 - t) (0.20, 0.20) (0.28, 0.28) (0.48, 0.48 + w) (0.75, 0.75) (1,1)
* B: R's mirror, (0.10, 0.10 + t) .. (0.48, 0.48 - w)
* G: the same x, (0.10, 0.10 + k t) .. (0.48, 0.48 + k w)

On a neutral, that gives teal shadows and gold highlights, with 0.20..0.28 neutral and the split back at identity from
0.75. The code is `kFilmGradeStrength` (the one strength table) and the v3 constants in
`src/batch/LookAssistAnalysis.cpp`, read through `lookAssistFilmGradeForScene()`. `lookAssistFilmGradationCurve()` writes
the receipt's `gradationCurve` string in the Curves widget's own format, so a widget round trip is byte-stable. A console
test pins this table against the code (`LookAssistFlavors.TheDocumentedFilmTableIsTheCodeTable`).

<!-- film-table:begin -->
| Scene | s | R@0.10 | G@0.10 | B@0.10 | R@0.48 | G@0.48 | B@0.48 | Y@0 | Y@0.80 | Y@1 |
|---|---|---|---|---|---|---|---|---|---|---|
| Night | 0.5 | 0.081 | 0.10285 | 0.119 | 0.492 | 0.4818 | 0.468 | 0.01 | 0.795 | 0.985 |
| ArtificialLights | 0.75 | 0.0715 | 0.104275 | 0.1285 | 0.498 | 0.4827 | 0.462 | 0.015 | 0.7925 | 0.9775 |
| Shade | 1.0 | 0.062 | 0.1057 | 0.138 | 0.504 | 0.4836 | 0.456 | 0.02 | 0.79 | 0.97 |
| BrightSun | 0.9 | 0.0658 | 0.10513 | 0.1342 | 0.5016 | 0.48324 | 0.4584 | 0.018 | 0.791 | 0.973 |
<!-- film-table:end -->

**How t and w were chosen (pre-registered, no retune).** The bound and four (t, w) candidates were fixed before any
table was built: C1 (0.042, 0.024), C2 (0.042, 0.020), C3 (0.038, 0.024), C4 (0.046, 0.027). The first one, in that
order, that passed every table gate ships. The gates: the neutral-ramp lean within 1.5 / 255 at every scene; on the
M16-1243 AgX-off Cinematic capture, the histogram-weighted lean and the frame-locked re-grade's lean within 1.5 / 255;
the re-grade's split at least 0.70x v2's and 2x v1's; dS at most 40 and MAD at most 6; the re-grade's green axis at
least -1; and the skin and green-axis guards below. C1, C2 and C4 leaned the M16-1243 re-grade blue by 1.89, 2.35 and
1.98 / 255 (M16-1243's histogram is dark, so it sits on the teal half); C3, at -1.47 / 255, passed every gate.

**Lean, measured** (`look-flavor-diff.py warmcool`, in 8-bit codes). Neutral ramp, v3 / v2: Night +0.32 / +2.89,
ArtificialLights +0.48 / +4.39, Shade +0.66 / +5.92, BrightSun +0.59 / +5.30 (`FilmV3RampLeanIsNeutral` asserts
|v3| <= 1.5 at every scene and v2 > 1.5 at Shade). On M16-1243 at Shade: histogram-weighted -0.53 (v2 +2.38) and
frame-locked re-grade -1.47 (v2 +1.32). The guard for any later grade is `warmcool --max-abs-lean`, which exits 20
(`WARMCOOL_BOUND_EXCEEDED`) when any of the three leans of any table is beyond the bound; the bound used here is the
tool's `WC_NEUTRAL_BOUND`.

**Strength, measured.** Through the engine's tables on a neutral 8-bit ramp, the split S (mean B-R over input codes
[8, 40] minus mean B-R over [85, 140], the #319 trio's tile bands, less the default curve's) is, for v1 / v2 / v3:
Night 3.24 / 14.48 / 12.16, ArtificialLights 4.86 / 21.68 / 18.19, Shade 6.49 / 28.80 / 24.14, BrightSun
5.84 / 25.97 / 21.77. v3 is about 3.7x v1 and 0.84x v2 at every scene (`FilmV3IsMateriallyStrongerThanV1` asserts at
least 3x v1 and 0.70x v2 at Shade). On the M16-1243 re-grade, dS is 9.28 / 30.29 / 23.32.

### The skin-hue guard

The guard is built into the curve shape. There is no hue curve and no extra stage.

* The teal lift ends at 0.20 (v1's ended at 0.45), so the B channel of a dark skin patch (about 0.2) is not lifted.
* 0.20..0.28 is a neutral zone (v2: 0.20..0.26).
* G takes the share `k`, which partly matches skin's R push.

Measured through the engine's stage (Y then the channel table, hue as `fromRGBtoHSV` computes it, against the default
curve). The patches are R:G:B = 1:0.72:0.56 at R in {0.35, 0.50, 0.65, 0.80} and 1:0.80:0.68 at R in {0.55, 0.75, 0.90}:

| Scene | dh v3, 1:0.72:0.56 (R 0.35 / 0.50 / 0.65 / 0.80) | dh v3, 1:0.80:0.68 (R 0.55 / 0.75 / 0.90) | largest magenta-ward v3 / v2 |
|---|---|---|---|
| Night | -0.39 / -1.02 / +0.23 / +1.56 | -0.18 / +2.29 / +1.74 | 1.02 / 1.64 |
| ArtificialLights | -0.49 / -1.51 / +0.35 / +2.32 | -0.25 / +3.36 / +2.62 | 1.51 / 2.35 |
| Shade | -0.53 / -1.97 / +0.47 / +3.08 | -0.29 / +4.38 / +3.53 | 1.97 / 3.01 |
| BrightSun | -0.52 / -1.79 / +0.42 / +2.78 | -0.27 / +3.98 / +3.17 | 1.79 / 2.76 |

Every graded skin hue stays in [19.8, 26.9] degrees. No patch moves more than 1.97 degrees toward magenta (dh < 0),
and at every scene v3's largest magenta-ward move is smaller than v2's (and so than v1's). Light skin yellows by up to
about 4.4 degrees at Shade (v2: 6.2). `FilmV3SkinPatchesKeepTheirHue` asserts hue in [12, 32], a magenta-ward move of
at most 3.5, and v3 <= v2 at Shade. These numbers hold for these patches through the gradation stage alone; they say
nothing about other inputs.

### What it does to the green-magenta axis

R and B move by equal and opposite amounts at every knot, and the engine's spline (`tk::spline`, natural cubic,
`src/processing/interpolation/spline_helper.cpp`) is linear in y. So `gcurve_r[v] + gcurve_b[v] == 2 * default[v]` to
within one table step on every entry the stage can reach. The engine's 0.0001 floor clamps R's dip on a few entries
below `Y[0]`, which the Y line never indexes. G takes `k` of each offset; between knots the natural spline dips it
below the default by at most 42 table steps (0.16 of an 8-bit code, at Shade, near 0.23).

A pixel's green-magenta move through the stage is `dG(g) - (dR(r) + dB(b)) / 2`. Each term depends on one channel
only, so the tables bound it for every pixel. All numbers are in 8-bit codes, measured by
`FilmV3NeutralLeansGreenNeverMagenta`:

* **Neutral (R = G = B):** the move is G's share, so a neutral leans **toward green, never toward magenta**: every
  grey level stays within [-0.08, +0.73] Night, [-0.13, +1.10] ArtificialLights, [-0.17, +1.47] Shade and
  [-0.15, +1.32] BrightSun. The test asserts [-0.5, +2.5], with the lean at least 60% of `k w`.
* **Any pixel at all:** [-8.9, +6.7] Night, [-13.4, +10.0] ArtificialLights, [-17.9, +13.4] Shade and
  [-16.1, +12.1] BrightSun. The extremes need channels sitting on opposite ends of the tone line, for example G near
  white with R and B in the lifted shadows. Most of the bound is the Y line alone, which, like any tone curve, moves
  chroma: Film's Y line with default R, G, B gives ±6.4 / ±9.6 / ±12.7 / ±11.5. The test pins the bound below
  [-9.0, +6.7], [-13.4, +10.1], [-17.9, +13.5] and [-16.1, +12.1].
* **#319's coloured set, at Shade:** amber (0.72, 0.45, 0.18) -0.40 and sodium (0.90, 0.50, 0.10) -2.51 move toward
  magenta. Teal (0.18, 0.45, 0.72) +1.27, sky (0.30, 0.55, 0.85) +1.00 and cyan (0.10, 0.50, 0.90) +5.32 move toward
  green, and skin (0.80, 0.55, 0.35) moves +2.27, also toward green (v1 moved skin toward magenta). Each direction is
  v2's; the other scenes scale with `s`, and the test pins each direction at every scene.

The card's no-magenta tolerance is judged on the **venue picture mean** (over the mid-luma band of real footage), plus
an eyeball check for any magenta, purple, mauve, mint or olive cast. It is not judged per saturated pixel.

**Why it costs nothing per pixel.** The gradation curves are applied unconditionally on every route (identity
tables when unused): the CPU 16-bit loop, both direct8 kernels, and the CUDA display shader, which composes them
into its one creative-curve texture, the Y line included. Direct8 eligibility and the display shader's refused stages
do not look at them. A Film grade is a one-time spline rebuild and one texture upload when it is applied.

**The user's curve wins.** If the receipt's gradation curve is not the default (numerically: four lines, each
exactly the two identity points within 1e-6; an empty string counts as default), Film does **not** overwrite it: it
lays the tone deltas only and reports `grade=skipped_user_curve`.

**The baseline.** The curve Film replaced is kept in the receipt element `lookAssistBaselineGradationCurve`, written
**only while it is non-empty** (a Classic or Cinematic receipt is byte-identical to before). Look Assist's baseline
restore (GUI `restoreLookAssistBaseline`, the safety fallback, headless `restoreHeadlessLookAssistBaseline`) puts
that curve back and clears the element, so every analysis measures at the user's own curve. Capturing a fresh
baseline does the same first. Switching Look Assist off, or re-running as Classic or Cinematic, therefore leaves no
trace of the grade.

The curve goes back only while Film still **owns** it (`lookAssistFilmOwnsGradationCurve`): while it is, point for
point (within 1e-6), a curve Film lays for one of the scenes. That covers the v3 curves and the **legacy v2 and v1
curves** (12 curves in all). A receipt saved under #328 with a v2 curve, or under #319 with a v1 curve, is still put
back by Look Assist off, and a Film re-run over it lays v3 (`AV2LaidCurveIsStillRestoredByLookAssistOff`,
`AV2ReceiptReRunAsFilmLaysV3`, `AV1LaidCurveIsStillRestoredByLookAssistOff`, `AV1ReceiptReRunAsFilmLaysV3`). The v2 and
v1 builders, `lookAssistFilmGradationCurveV2()` and `lookAssistFilmGradationCurveV1()`, are kept byte for byte for that
reason and for the frame-locked comparison below. Neither is selectable anywhere.

If the user edited the laid curve (say, added a Y point), the edit is the user's curve. The restore keeps it and still
clears the element, which retires Film's ownership, and a later Film run reports `grade=skipped_user_curve` over it.
The GUI judges ownership on the curve the widget shows, headless on the receipt's. The white balance stays Classic's
by construction, as for Cinematic. Headless batch exports CDNG (raw), so the grade never reaches a CDNG; headless only
keeps the receipt consistent.

### Frame-locked v1 / v2 / v3 comparison

On CPU, the venue's flavor legs present different frames at a tile, so a v1 column from another run is not
like-for-like. Instead, `LookAssistFilmGrade.DumpTablesWhenAsked` (pipeline tests, only when
`MLVAPP_FILM_TABLE_DUMP_DIR` is set) writes the engine-built tables of v1, v2 and v3 for every scene, and of the four
pre-registered v3 candidates (with their C++ table gates printed). Each table is `film-<v1|v2|v3>-<scene>.u16` (or
`film-v3-<C1..C4>-<scene>.u16`), 4 x 65536 uint16 LE, in Y, R, G, B order.

`tools/profiling/look-flavor-diff.py regrade` lays them over ONE Cinematic capture. For each 8-bit value `c` it
computes `T_ch[T_Y[c * 257]]` and divides by 257, rounding, and writes a Cinematic | v1 re-grade | v2 re-grade strip
plus `regrade-metrics.json` with S, GA and MAD per tile. With `--v3-table` it adds a v3 column (four 960 px columns,
`regrade-cinematic-v1-v2-v3.png`), the v3 metrics, `dSRatioV3OverV2` and the warm-cool lean change `dLean` of each
re-grade; without it the outputs are byte-identical to before. Gradation is the last stage before AgX, LUT and filter, so the
re-grade is the engine's picture only when the Cinematic render had all three **off**; otherwise the tool refuses
(`REGRADE_INVALID`). The app's default receipt has AgX on (`ReceiptSettings`), so a venue capture without a receipt is
invalid for this. `--illustrative` still composes an eyeball-only strip, marked `valid=false`.

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
* On those three lines a Film apply appends ` grade=<film-v3|skipped_user_curve|none>` after `flavor=`; Classic and
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
  cinematic renders of the tracked fixtures (fixture renders only) for model judging. A Film column there was
  deferred (not shipped); Film's fixture picture is covered by `FilmIsADifferentGradedPictureFromCinematic` instead.
* Film grade (film-v3): the documented table is the code's and Film's tone is Cinematic's; the selector accepts `film`;
  the curve built through `processingSetGCurve` keeps `|r + b - 2 default| <= 2` on every reachable entry, G never a
  whole code below the default, and Y the lift / shoulder line (`FilmV3CurveIsTealGoldWithANeutralLean`); the neutral
  ramp's blue-amber lean is within 1.5 / 255 at every scene (`FilmV3RampLeanIsNeutral`); v3, v2 and v1 are three
  different curves (`FilmV3IsNotV2`); a neutral leans toward green, never magenta, and every pixel moves within the
  documented bound (`FilmV3NeutralLeansGreenNeverMagenta`); v3's split is at least 3x v1's and 0.70x v2's
  (`FilmV3IsMateriallyStrongerThanV1`); skin keeps its hue (`FilmV3SkinPatchesKeepTheirHue`); blacks lift and whites
  roll, every table monotonic (`FilmV3LiftsBlacksAndRollsWhites`); the v3 tables and the legacy v2 and v1 tables are
  pinned by sha256 and sampled values (`FilmCurveIsPinned`, `FilmV2CurveIsPinned`, `FilmV1CurveIsPinned`); a v2 or v1
  curve on an old receipt is still Film's own (`AV2LaidCurveIsStillRestoredByLookAssistOff`, `AV2ReceiptReRunAsFilmLaysV3`,
  `AV1LaidCurveIsStillRestoredByLookAssistOff`, `AV1ReceiptReRunAsFilmLaysV3`); a user curve is kept and reported; the baseline
  round trip leaves a re-run-as-Classic receipt identical to a Classic-only one; a user's edit of the laid curve
  survives a Classic re-run and Look Assist off, with the element gone (`AUserEditOfTheFilmCurveIsKeptAndRetiresFilmsOwnership`,
  `AUserEditAfterTheFilmGradeIsKeptByAClassicReRunAndByLookAssistOff`); Look Assist off changes nothing;
  the white balance is Classic's on the fixtures; the CUDA display shader and the CPU direct8 route both take the
  Film curves, Y line included (`EngineAnchoredLookAssistFlavorsMatchEngineWithReceiptSCurve`, cases
  `look_assist_film_v3_night_real_frame` and `look_assist_film_v3_shade_real_frame`).
