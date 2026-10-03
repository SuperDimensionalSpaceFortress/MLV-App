# Look Assist flavors: Classic | Cinematic

Look Assist analyses a clip (scene verdict, exposure, white balance) and writes a handful of receipt sliders.
Since LOOK-ASSIST-FLAVORS-1 it can do so in one of two flavors. The owner's directive (2026-09-26): *video
shouldn't look raw or unprocessed with look assist active; it should have an aesthetic cinematic color grade.*

* **Classic** is Look Assist exactly as it was: the same sliders, the same receipt, the same picture, in every
  state. It is the default.
* **Cinematic** is the same analysis, the same scene verdict and the same white-balance decision, with one table
  of deltas laid over the same sliders. It is applied **only** through the sliders Look Assist already owns:
  exposure, contrast, pivot, shadows, highlights, vibrance. It never touches the white balance (temperature / tint).
  Saturation and the tone curve are not Look Assist sliders (no preset field, no baseline to restore), so they are
  not used; the modest extra colour comes through vibrance.

The code is one table, `kCinematicFlavorDeltas` in `src/batch/LookAssistAnalysis.cpp`, read through
`lookAssistCinematicDeltasForScene()`. `presetForLookAssistScene()` takes a trailing `flavor` argument that
defaults to Classic; Classic returns before any flavor code runs.

## The Cinematic table

Additive deltas over the Classic preset the scene and the statistics produced (then clamped to the slider range;
Night never goes below 0 exposure and BrightSun never above 0, as before). A test pins this table against the code
(`LookAssistFlavors.DocsTableMatchesTheCodeTable`).

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
  exposure, so an exposure delta would move the balance; with none, the white balance is Classic's by
  construction (a test pins temperature, tint and the headless picture byte for byte).

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

## Choosing the flavor

Layers, first non-empty one wins (`lookAssistSelectFlavor()`):

1. `MLVAPP_LOOK_ASSIST_FLAVOR=classic|cinematic`: for runs (the venue legs set it). Case and whitespace do not matter.
2. The receipt's `<lookAssistFlavor>` element, which the headless / batch path reads. It is **written only for a
   non-Classic flavor**, so a Classic receipt is byte-identical to what it always was; absent means Classic.
3. The GUI's *Look Assist flavor* selector next to *Auto Look Assist*, persisted as the app setting
   `lookAssistFlavor` (default `classic`). Changing it re-runs Look Assist the way switching it on does. A
   receipt that declares a flavor shows it in the selector when it is loaded.

An unknown value is **Classic**, never a fall through to a lower layer, and is logged: the headless applier prints
`[BATCH] WARNING LOOK_ASSIST unknown flavor '<value>' from <env|receipt>; using classic`, the GUI logs
`look_assist.flavor.unknown_value`.

## Reporting

The flavor applied is always reported, appended to the end of the existing lines (never inserted):

* GUI `look_assist.apply.result`: `... next_serial=<n> <decision trace fields> flavor=<classic|cinematic>` (the trace is LOOK-ASSIST-DIAG-LOGGING-1's, `has_ev100=` .. `playback_scale=`; flavor comes after it).
* GUI `look_assist.apply.async_dispatch`: `... floor_lifted=<0|1> flavor=<...>`.
* Headless `[BATCH] LOOK_ASSIST applied ...`: `... initialPatchFinalChroma=<x> <decision trace fields> flavor=<...>`.
* `gui_smoke.visual_state`: `... gpu_preview_processing_reject_reason=<r> look_assist_flavor=<...|none>`. This is
  what the venue job reads: its summary carries `lookFlavorReported`, and the leg receipt's
  `look.lookFlavorHonored` is `true` only when the app's own report equals the flavor the leg asked for
  (see [dual-venue-evidence.md](dual-venue-evidence.md)).
* The receipt: `lookAssistFlavor` (non-Classic only), and the in-memory receipt always carries the flavor applied.

## Tests

* `tests/console/test_look_assist_flavors.cpp`: Classic equals master's preset on a 5808-line grid (golden hash
  dumped from an unchanged master tree); the table; Cinematic = Classic + table (clamped), deterministic, never
  the white balance; the selector's layers and the unknown-value rule; the receipt element is read and written only
  for a non-Classic flavor; every preset call in both consumers passes the flavor; the scene verdict stays
  flavor-blind; the GUI selector and the environment both reach the analysis.
* `tests/pipeline/test_look_assist_flavors.cpp`: on the tracked fixture frames, Classic reproduces master's
  receipt sliders, applied line (but for the appended field) and rendered-picture sha256
  (`tests/fixtures/look_assist_flavor_classic_baseline.txt`); Cinematic changes only the documented sliders, keeps
  the white balance and the headless picture, and is a different, deterministic picture once the sliders are
  applied; an unknown environment value is Classic with a warning; the receipt element sits below the
  environment. With `MLVAPP_FLAVOR_SHEET_DIR` set, `LookAssistFlavorsFixture.ContactSheets` writes raw | classic |
  cinematic renders of the tracked fixtures (fixture renders only) for model judging.
