# Look Assist objective floor + blind judge harness (LOOK-METRICS-JUDGE-1)

Design of record: DUAL-VENUE DESIGN.md AMENDMENT 1 (A3 metrics) and AMENDMENT 2 (B3 objective floor, B4 model
judging, B5 judged matrix). Owner ruling 2026-09-30: models judge aesthetics; the owner is optional; nothing
waits on him. Bus evidence (TRAPS s8) says a model judging a blinded image pair can sit near chance, so
**the metrics GATE and the judges only REFINE**: a frame that fails the floor is not rescued by a judge.

## Layout

| file | job | needs |
|---|---|---|
| `look_floor_config.json` | every threshold, each as `{value, reason}`; provisional until a judged corpus exists | - |
| `look_config.py` | loads/validates the config (a reasonless threshold is rejected); rubric digest + lock | stdlib |
| `look_metrics.py` | per-frame floor, letterbox detection, skin-tone drift, SSIM, CUDA-vs-CPU pair metrics, typed verdicts | numpy, Pillow |
| `judge_rubric.md` + `judge_rubric.lock.json` | the FROZEN rubric (anchored 1-5 x5 criteria, then pairwise) and its sha256 | - |
| `look_pairs.py` | blind pair builder: opaque ids, mid-grey gutter, seed-decided slots, order-swapped twice, zero-effect control | stdlib (+ Pillow to draw) |
| `look_judges.py` | judge-runner interface: `ClaudeCliJudge`, `CodexExecJudge` (`codex exec --image`), probes, producer guard, session runner | stdlib |
| `look_tally.py` | discards order-flipping votes, slot-bias test, control result, `model_verdicts[]` entry, third-judge rule | stdlib |
| `look_cli.py` | `tiles`, `floor`, `pair-metrics`, `verify-rubric`, `probe-codex`, `build-session`, `judge`, `tally` | - |

## The objective floor (gates)

Per frame, on the **active area** (black letterbox bars are detected and excluded, and the exclusion is written
into the verdict; a playback capture can include window bars that would otherwise fail every frame on geometry):

* clipped highlights <= 1 %, crushed shadows <= 2 % -- the definitions are `make-contact-sheet.py`'s `channel_stats`
  (reused, not re-implemented), so a number here matches every existing stats sidecar;
* saturation ceiling: mean HSV saturation and the share of oversaturated pixels;
* skin-tone-region hue drift <= ~5 deg **against a baseline frame** where a region is detectable; otherwise
  `NOT_APPLICABLE` (never a pass) and the sheet verdict says how many frames the check applied to.

Per CUDA/CPU pair: per-channel mean / max / signed delta, mismatch fraction, luma SSIM (numpy implementation:
Gaussian 11x11, sigma 1.5). `SCOPE=shader-subset` is **gated**; `SCOPE=full-look` is **REPORTED** and never
PASS/FAIL until the CUDA display shader carries every stage (A3).

Typed terminals, no partial credit: frame `PASS|FAIL|NOT_EVALUABLE`; sheet `PASS|FAIL|INCOMPLETE`; pair
`PASS|FAIL|REPORTED|INCOMPLETE|GEOMETRY_MISMATCH`. An empty sheet, an unpaired frame or an unreadable image is
`INCOMPLETE`, never a pass. A verdict carries the config's sha256 and any override that was applied.

## The blind judge harness

1. `look_cli.py verify-rubric` -- the rubric still matches `judge_rubric.lock.json` (digest recorded and committed
   BEFORE any judging; any edit is a new rubric version and a re-judge).
2. `look_cli.py build-session --a NAME=DIR --b NAME=DIR --seed S --out-dir SESS` -- crops letterbox bars (they
   would reveal the backend), centre-crops to a common size, composes `left | grey gutter | right` images with
   opaque names and no labels, emits every unit **twice order-swapped**, adds a **zero-effect control** (identical
   images), writes `judge_manifest.json` (judge-facing: ids + image names only), `answer_key.json` (never shown),
   and `session.json` (the rubric digest, seed and image digests recorded before judging).
3. `look_cli.py judge --session-dir SESS --runner claude:MODEL|codex:MODEL --producer-model M ...` -- each item runs
   in a fresh temp dir holding only `pair.png`. The judge may not be a `--producer-model` (the look's producer or
   the hub). Refuses to run if the rubric no longer matches its lock or the session recorded another digest.
4. `look_cli.py tally ...` -- one `model_verdicts[]` entry: `judgeId, model, family, rubricSha256, imageSha256s,
   orderSeed, slotTally, scores, preference` (+ `controlResult`, `usable`). A vote whose two orderings name
   different subjects is a flip and is discarded; a judge that prefers a side of identical images fails the
   control; a lopsided left/right split is flagged as slot bias (exact binomial; `INSUFFICIENT_N` below 6 choices).
   `look_tally.judge_disagreement` says when two judges are > 1 point apart and a third is needed.

### Cross-family (K6)

`look_cli.py probe-codex` reads `codex exec --help` for `--image` (`CROSS_FAMILY_FLAG_PRESENT_NOT_PROVEN`), and with
`--live-vision-dir` makes ONE bounded call on a synthetic image whose answer exists only in its pixels
(`CROSS_FAMILY_PROVEN_LIVE`). `look_judges.select_second_judge` returns a Codex judge only when the probe is
proven; otherwise a second Claude model and `CROSS_FAMILY_UNAVAILABLE`, which the entry carries.

## Honest limits

* Thresholds are provisional; every one says why it is the number it is, and none has been calibrated on a judged corpus.
* The skin check is a colour region, not a face detector (sand can land in it); that is why it is a drift against a
  baseline of the same scene, never an absolute.
* Judging three frame-units is far too few to say a judge is unbiased: the tally reports `INSUFFICIENT_N` rather than "no bias".
* Contact-sheet tiles are 480-px downscales. `look_cli.py tiles` exists to demo the harness on fixture sheets;
  real runs read the full-resolution `frame-NN.png` of `--contact-sheet-dir`.
* Sheets of OWNER footage stay local (DESIGN A2): never pass them to a publishing step.
* The tests that draw pixels need numpy + Pillow, which the hosted repo-hygiene CI image does not install, so there
  they skip (like `test_playback_attr_3_cuda_contact_sheet.py`); the stdlib half (config reasons, rubric lock,
  pair plan, tally, judge guard) runs in CI.
