# Look Assist flavor: Kodak Vision3 50D

The fourth Look Assist flavor (LOOK-ASSIST-KODAK-VISION-50D-1). The owner picked look C, "Vision3 50D clean", from the
2026-10-15 preview sheet: fine-grain daylight with crisp contrast, a neutral colour balance and richer colour. The other
three flavors are in [look-assist-flavors.md](look-assist-flavors.md).

Kodak and Vision3 are trademarks of Eastman Kodak Company. This mode is an original approximation: its control points
are original, and no Kodak LUT or data is used. It is not affiliated with or endorsed by Kodak.

* **GUI label:** *Kodak Vision3 50D* (owner ruling, 2026-10-15). It is the 4th item of the *Look Assist flavor*
  selector. The tooltip says it is a look, so "50D" is not read as the Canon body.
* **Machine spelling:** `vision50d`. It is used by `MLVAPP_LOOK_ASSIST_FLAVOR`, the receipt's `<lookAssistFlavor>`, the
  app setting `lookAssistFlavor` and every `flavor=` log field. It is not `film50d`, so a prefix grep for `flavor=film`
  does not match it.
* **Grade id:** `vision50d-v1`. It is reported as ` grade=vision50d-v1|skipped_user_curve` after `flavor=`, on the same
  three lines as Film's grade, and as the venue's `presetGrade`.

## The recipe

**Kodak Vision3 50D = Cinematic's tone + a vibrance delta + its own gradation curve.** As with Film, there is no new
engine stage, no LUT, no hue curve and no profile change. The exposure and the white balance are Classic's by
construction, exactly as for Cinematic.

* **Tone:** Cinematic's table (`kCinematicFlavorDeltas`), unchanged, plus `kVision50dVibranceDelta` on vibrance. It is
  added inside `lookAssistApplyFlavorDeltas()` before the clamp, so `presetForLookAssistScene()` and the GUI's overlay
  get it from one place.
* **Curve:** one builder, `lookAssistVision50dGradationCurve()`. Each control point is `y = x + s * (y_owner - x)` on
  the owner's knots, at the scene's strength `s` (`kVision50dGradeStrength`, the one table). It is written through
  Film's line writer, so the string is in the Curves widget's own format.

<!-- vision50d-table:begin -->
| Scene | s | vibrance delta |
|---|---|---|
| Night | 1.0 | +12 |
| ArtificialLights | 1.0 | +12 |
| Shade | 1.0 | +12 |
| BrightSun | 1.0 | +12 |

| Line | knots (x, y_owner), display-referred 0..1 |
|---|---|
| Y | (1e-5, 0.005) (0.18, 0.15) (0.50, 0.51) (0.82, 0.86) (1, 0.985) |
| R | (1e-5, 1e-5) (0.50, 0.505) (1, 1) |
| G | (1e-5, 1e-5) (0.50, 0.50) (1, 1) |
| B | (1e-5, 1e-5) (0.50, 0.495) (1, 1) |
<!-- vision50d-table:end -->

A console test pins this table against the code (`LookAssistFlavors.TheDocumentedVision50dTableIsTheCodeTable`). At
`s = 1` the receipt's `gradationCurve` is:

`1e-05;0.005;0.18;0.15;0.5;0.51;0.82;0.86;1;0.985;?1e-05;1e-05;0.5;0.505;1;1;?1e-05;1e-05;0.5;0.5;1;1;?1e-05;1e-05;0.5;0.495;1;1;`

* **Y** steepens the mid-tones (0.18 -> 0.15 and 0.82 -> 0.86), with a slight toe (0.005) and shoulder (0.985).
* **R / B** are a mirror about the identity at one knot (+0.005 / -0.005 at 0.5), and **G** is the identity. On a
  neutral that is a faint warm lift in the mid-tones and nothing in the shadows or highlights.

**Strength is constant.** The preview was constant and the owner judged one curve. The table exists so a later card
can lower one scene (Night, say) without touching the builder.

### The vibrance ladder (pre-registered)

The rungs were fixed before any measurement: **D = +18 -> +12 -> +8**. The curve never changes. Rung 1 (+18) is built
first. A rung fails only on a decisive gate. A failure that comes from vibrance (F3's upper bound, a safety fallback,
too garish, a cast) steps to the next rung. A curve or table failure stops the card. The rung shipped is the one in the
table above.

**Rung 1 (+18) failed F3's upper bound** on the tracked fixtures: saturation rose +0.404, +0.406 and +0.366 over
Cinematic, against a ceiling of +0.35. F1 and F2 passed. That failure comes from vibrance, so the ladder stepped to
**rung 2 (+12)**, which passes F1..F3 (numbers below). No venue round was spent on rung 1.

### Why vibrance runs before the curves

In the engine, vibrance is applied to 16-bit data **before** the gradation curves (`raw_processing.c`, the vibrance
stage, then the curve loop `pix = T_ch[T_Y[pix]]`). The owner's preview applied it after the curves, to 8-bit data, as
a flat x1.18 chroma scale. The engine's slider is not a flat scale either. It maps `p` to `((p + 100) / 100)^1.848` and
weights that by a saturation term, so it lifts low-chroma pixels more than saturated ones.

So the engine's vibrance delta amplifies a scene's **own** colour on near-neutral pixels a little more than the preview did. It
never colours a **true neutral**: for `r = g = b` the vibrance stage reads `Y1 = r` and its curve is 0 there, so the
pixel is unchanged (`src/processing/raw_processing.c:4313-4319`, `:4338-4340`). That is why the decisive engine
warm-cool gate (E5) is balance-relative and the raw lean change (E6) is only reported.

## Ownership, the user's curve and the baseline

Kodak Vision3 50D's curve is laid exactly where and how Film's is: by the GUI's `applyLookAssistFilmGrade()` and the
headless `applyHeadlessLookAssistFilmGrade()` (the names are kept for their call sites), only over a default curve. Its
baseline is the same receipt element, `lookAssistBaselineGradationCurve`.

* `lookAssistVision50dOwnsGradationCurve()` matches the curve it lays for each scene, point for point within 1e-6, as
  Film's ownership does.
* `lookAssistGradeOwnsGradationCurve()` is Film's ownership OR Vision50d's. `lookAssistGradationCurveAfterFilmRestore()`
  uses it, so either grade's curve goes back to the baseline and a user's edit of either is kept.
* `lookAssistFilmOwnsGradationCurve()` and `lookAssistFilmGradeDecision()` keep their Film-only meaning; Film receipts
  are byte-identical to before.
* A Film receipt re-run as Kodak Vision3 50D, or the other way round, restores the baseline first and then lays the
  other grade. Neither reports `skipped_user_curve`.

The generic helpers are `lookAssistFlavorLaysGradationCurve()`, `lookAssistGradeIdForFlavor()`,
`lookAssistGradeDecision()` (Film delegates to its own decision) and `lookAssistGradationCurveForFlavor()`.

## Gates and measured numbers

The gates, bounds and ladder are the design's section 4 (`B = 1.5 / 255`), fixed before any table was built.

### Table gates (C++, `tests/pipeline`, through `processingSetGCurve`, every scene)

<!-- vision50d-gates:table -->
| ID | Gate | Bound | Measured |
|---|---|---|---|
| T1 PIN | curve string = the literal above; tables pinned by sha256 and samples | exact | pass: the four scenes build byte-identical tables, sha256 `b3ace9fc...043badb` |
| T2 WC-RAMP | neutral-ramp lean, mean((R+G)/2 - B), all 65536 codes | \|lean\| <= B | +0.004320 (+1.102 / 255), every scene |
| T3 GM-NEUTRAL | every grey level, \|G - (R+B)/2\| | <= 128 table steps | 0.5 steps |
| T4 MIRROR | \|r + b - 2 def\| above the floor clamp | <= 2 steps | 1 step (8 entries floor-clamped) |
| T5 TONE-SHAPE | (Y[0.82] - Y[0.18]); Y[0]; Y[1]; max(v - Y[v]); monotonic | >= 0.69; [0.004, 0.006]; [0.983, 0.987]; <= 0.035 | 0.7100 (default 0.6400); 0.00499; 0.98499; 0.0317 at x = 0.224; all four monotonic |
| T6 SKIN | Film's skin patches: graded hue; magenta-ward move | [12, 32] deg; <= 3.5 deg | [20.46, 26.65]; 1.36 |
| T7 PRESET | preset = Cinematic's, vibrance = clamp(cin + D) | exact | pass on the whole grid |
| T8 DISTINCT | not default; Film does not own it; it owns none of Film's 12; the union owns both | exact | pass |

### Fixture gates (`LookAssistFlavorsFixture.Vision50dIsCrisperAndRicherThanCinematic`)

| ID | Gate | Bound | Measured |
|---|---|---|---|
| F1 SAME-SOLVE | exposure, temperature, tint = Cinematic's | exact | pass (vibrance 11 -> 23) |
| F2 CRISP | spread_V - spread_C (luma p95 - p05), every fixture frame | >= +2.0 codes | +8.70 / +8.85 / +6.56 |
| F3 RICH / NOT-GARISH | sat_V / sat_C - 1, every fixture frame | [+0.04, +0.35] | +0.321 / +0.322 / +0.287 |

Fixture frames, in order: `tiny_dual_iso.mlv` 0, `large_dual_iso.mlv` 0, `large_dual_iso.mlv` 10. The numbers are at
rung 2 (+12). At rung 1 (+18) F3 read +0.404 / +0.406 / +0.366.

### Re-grade and venue gates (M16-1243, Bachelor)

The re-grade gates (R1 histogram lean, R2 frame-locked re-grade lean, R3 crispness) and the engine gates on the venue
trio (E0..E8) are recorded in the card's PR. Engine metrics are taken only on frame-matched tiles.

## Tests

* `tests/console/test_look_assist_flavors.cpp`: the selector accepts `vision50d` in any case and padding and maps a
  receipt to the selector's value (`Vision50dIsOneMoreSpellingOfTheSameSelector`); the receipt element round-trips
  (`TheVision50dReceiptElementIsWrittenAndReadBack`); the table above is the code's
  (`TheDocumentedVision50dTableIsTheCodeTable`); T7 on the Cinematic grid
  (`Vision50dPresetIsCinematicsPlusTheVibranceDeltaOnTheWholeGrid`); the decisions, with Film's unchanged
  (`TheVision50dGradeIsLaidOnlyOverADefaultCurveAndFilmsDecisionsAreUnchanged`); T8 and the restore
  (`TheVision50dCurveIsDistinctAndOwnedOnlyByItsGrade`); the GUI selector (`TheGuiSelectorOffersKodakVision3_50D`); the
  result lines (`TheVision50dGradeIsReportedOnTheHeadlessAndGuiResultLines`).
* `tests/pipeline/test_look_assist_flavors.cpp`: suite `LookAssistVision50dGrade` (T1..T6, curve-only);
  `LookAssistFilmGrade.DumpTablesWhenAsked` also writes `vision50d-v1-<scene>.u16`; suite `LookAssistFlavorsFixture`
  (`Vision50dIsCrisperAndRicherThanCinematic`, `AUserCurveIsKeptAndReportedForVision50d`,
  `AFilmReceiptReRunAsVision50dLaysVision50d`, `AVision50dReceiptReRunAsFilmLaysFilmV3`,
  `AUserEditOfTheVision50dCurveIsKeptAndRetiresOwnership`, `TheVision50dBaselineRoundTripLeavesNoTrace`).
* `tests/pipeline/test_gpu_preview_processing.cpp`: the CUDA display shader and the CPU direct8 route take the Vision50d
  preset and curve (`EngineAnchoredLookAssistFlavorsMatchEngineWithReceiptSCurve`, cases
  `look_assist_vision50d_night_real_frame` and `look_assist_vision50d_shade_real_frame`).

Licence: the control points are original and are evaluated by the already-vendored `tk::spline`; no third-party LUT
or colour-science maths is borrowed.
