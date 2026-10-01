# Look Assist objective floor + blind judge harness (LOOK-METRICS-JUDGE-1)

Design of record: DUAL-VENUE DESIGN.md AMENDMENT 1 (A3 metrics) and AMENDMENT 2 (B3 objective floor, B4 model
judging, B5 judged matrix). Owner ruling 2026-09-30: models judge aesthetics; the owner is optional; nothing
waits on him. Bus evidence (TRAPS s8) says a model judging a blinded image pair can sit near chance, so
**the metrics GATE and the judges only REFINE**: a frame that fails the floor is not rescued by a judge.

**The rule the whole harness is built around:** no path lets an *unjudged, stale, incomplete, biased, self-judged
or hidden* result read as PASS / usable. Round 2 closed this as a class; the sections below say how.

## Layout

| file | job | needs |
|---|---|---|
| `look_floor_config.json` | every threshold, each as `{value, reason}`, at every depth; provisional until a judged corpus exists | - |
| `look_config.py` | loads/validates the config (a bare number ANYWHERE, a missing required threshold or an empty reason is refused); rubric digest + lock | stdlib |
| `look_metrics.py` | per-frame floor, letterbox policy, skin-tone drift, SSIM, CUDA-vs-CPU pair metrics, typed verdicts | numpy, Pillow |
| `judge_rubric.md` + `judge_rubric.lock.json` | the FROZEN rubric (anchored 1-5 x5 criteria, then pairwise, no defect colour named) and its sha256 | - |
| `look_pairs.py` | blind pair builder: image-bound opaque ids, mid-grey gutter, seed-decided slots, order-swapped twice, negative + positive controls | stdlib (+ Pillow to draw) |
| `look_judges.py` | `ClaudeCliJudge`, `CodexExecJudge`, probes, family-level producer guard, process-tree-bounded runner, session runner | stdlib |
| `look_tally.py` | image/rubric/key integrity, flips, slot bias, both controls, completeness, consistent-unit floor, `model_verdicts[]` entry, third-judge rule | stdlib |
| `look_cli.py` | `tiles`, `floor`, `pair-metrics`, `verify-rubric`, `probe-codex`, `build-session`, `judge`, `tally`, `judge-disagreement` | - |

## The objective floor (gates)

Per frame:

* clipped highlights <= 1 %, crushed shadows <= 2 % -- the definitions are `make-contact-sheet.py`'s `channel_stats`
  (reused, not re-implemented), so a number here matches every existing stats sidecar;
* saturation ceiling: mean HSV saturation and the share of oversaturated pixels;
* skin-tone-region hue drift <= ~5 deg **against a baseline frame**:
  * a baseline with no skin region (or no baseline) -> `NOT_APPLICABLE` (never a pass; the sheet says how many frames it applied to);
  * a baseline that HAS a region while the subject lost it, or kept less than `skin_region_retain_fraction` of it ->
    **FAIL** (`SKIN_REGION_LOST_OR_SHRUNK`): a look that pushes skin out of the colour box is the worst drift;
  * otherwise the drift is measured over the **baseline's mask** (the same pixels in both frames) when the two frames
    align after a few-pixel centre crop, so moving only part of the skin still shows; the verdict records `maskBasis`;
  * a baseline that was given but cannot be read makes the frame `NOT_EVALUABLE`, not "no baseline".

**Nothing is hidden from the floor unless the caller says so.** A playback capture can include black window bars (the
UM CUDA fixture tiles do), but pixels alone cannot tell a bar from a crushed region of the scene. So by default the
**full frame is measured** and a barred capture fails "crushed shadows" loudly, with the dark bands it saw listed under
`geometry.letterbox.candidate` and the sheet's `framesWithDarkBandsMeasuredAsScene`. To exclude bars the caller either
**declares** them (`--letterbox-bars top=34,bottom=34`; each declared band must really be dark or it is refused) or
**opts in** to `--letterbox auto-symmetric` (a pair of opposite bands within `symmetry_tolerance_px` of each other).
A band on ONE side is always scene content. The choice (mode, provenance `DECLARED` / `AUTO_SYMMETRIC` / `NONE`,
the share excluded, and `crushed_shadow_pct_full_frame`) is written into every frame verdict.
`build-session` applies the same rule, so the judges see exactly what the floor measured.

Per CUDA/CPU pair: per-channel mean / max / signed delta, mismatch fraction, luma SSIM (numpy implementation:
Gaussian 11x11, sigma 1.5). `SCOPE=shader-subset` is **gated**; `SCOPE=full-look` is **REPORTED** and never
PASS/FAIL until the CUDA display shader carries every stage (A3). Each side has its own letterbox policy.

Typed terminals, no partial credit: frame `PASS|FAIL|NOT_EVALUABLE`; sheet `PASS|FAIL|INCOMPLETE`; pair
`PASS|FAIL|REPORTED|INCOMPLETE|GEOMETRY_MISMATCH`. An empty sheet, an unpaired frame or an unreadable image is
`INCOMPLETE`, never a pass. A verdict carries the config's sha256, its version and any override that was applied.

## The blind judge harness

1. `look_cli.py verify-rubric` -- the rubric still matches `judge_rubric.lock.json` (digest recorded and committed
   BEFORE any judging; any edit is a new rubric version and a re-judge).
2. `look_cli.py build-session --a NAME=DIR --b NAME=DIR --seed S --out-dir SESS` -- composes `left | grey gutter |
   right` images with opaque names and no labels, emits every unit **twice order-swapped**, adds a **negative control**
   (identical images) and a **positive control** (a frame against a copy with a large known degradation: flattened
   contrast + a strong colour wash), and writes `judge_manifest.json` (judge-facing: ids + image names only),
   `answer_key.json` (never shown) and `session.json` (rubric digest, seed, image digests, crop tolerance and **every
   dropped frame with its reason**). **Item ids are bound to the pair image digest** (`sha256(seed|unit|order|image)`),
   so rebuilding over different frames changes every id. A frame that is not judged (unshared, outside the crop
   tolerance, not in `--frame-ids`, beyond `--max-frames`) is listed on stderr, in the key and in the session.
3. `look_cli.py judge --session-dir SESS --runner claude:MODEL|codex:MODEL --producer-model M ...` -- each item runs
   in a fresh temp dir holding only `pair.png`, in a subprocess whose **whole process tree** is killed on timeout
   (the Windows `claude` / `codex` npm shims are `.cmd` files; a plain kill left the real CLI running). The judge may
   not be in the **model family** of any `--producer-model` (one alias table: `sonnet` = `claude-sonnet-5-5` = any
   sonnet; names that cannot be placed fail closed; the Codex default is resolved to the real model from
   `config.toml`, and an unresolvable one counts as any OpenAI model). It refuses to run if the rubric changed after
   the lock, if the session recorded another digest, or if the images on disk differ from the session's digests; it
   **re-judges** any stored verdict made against another image or rubric and refuses a results file from another judge.
4. `look_cli.py tally --session-dir SESS --results R.json --producer-model M --out T.json` -- one `model_verdicts[]`
   entry. **`usable` is true only if every one of these holds** (each is a named `unusableReasons` entry):
   every key item has a valid verdict (`INCOMPLETE_ITEMS`); every verdict quotes the key's image digest, the image on
   disk still hashes to it and the item id is derived from it (`IMAGE_CHANGED_SINCE_BUILD`, `ITEM_ID_NOT_BOUND_TO_IMAGE`);
   the results name no unknown item; **every** negative-control item was answered `tie` (a missing or invalid one is
   `CONTROL_INCOMPLETE`); **every** positive-control item preferred the original (`POSITIVE_CONTROL_FAILED` -- so an
   always-`tie` judge is unusable); no slot bias (exact binomial; `INSUFFICIENT_N` below 6 choices);
   at least `judge_validity.min_consistent_units` (4, from the config) real units agree across both orderings; and the
   producer guard was applied and passed. Order-flipping votes are discarded. **A winner is shown only on a usable
   entry** (otherwise `winner` is `UNUSABLE` and the computed one sits in `withheldWinner`), and **the CLI exits 2
   whenever the entry is unusable** (the JSON is still written).
5. `look_cli.py judge-disagreement --entries T1.json T2.json` -- a third judge is needed when two judges are more than
   `judge_disagreement.third_judge_points` (config) apart; a comparison that includes an unusable judge is not `comparable`.

### Cross-family (K6)

`look_cli.py probe-codex` reads `codex exec --help` for `--image` (`CROSS_FAMILY_FLAG_PRESENT_NOT_PROVEN`), and with
`--live-vision-dir` makes ONE bounded call on a synthetic image whose answer exists only in its pixels
(`CROSS_FAMILY_PROVEN_LIVE`). `look_judges.select_second_judge` returns a Codex judge only when the probe is
proven; otherwise a second Claude model and `CROSS_FAMILY_UNAVAILABLE`, which the entry carries.

## Honest limits

* Thresholds are provisional; every one says why it is the number it is, and none has been calibrated on a judged corpus.
* The skin check is a colour region, not a face detector (sand and deck can land in it); that is why it is a drift
  against a baseline of the same scene, never an absolute.
* A judge's identity in the results file is what the runner recorded; the tally cannot prove which model actually
  answered, only that it was not a producer/hub family. Judges run in a neutral temp dir with the Read tool, but the
  CLIs inherit the user's settings and the Codex sandbox can read the disk: isolation is by convention (the manifest
  names no subject, and the answer key is not in the temp dir).
* `min_consistent_units` is a floor against degenerate sessions, not a significance claim (`votePValue` is reported).
* Contact-sheet tiles are 480-px downscales. `look_cli.py tiles` exists to demo the harness on fixture sheets;
  real runs read the full-resolution `frame-NN.png` of `--contact-sheet-dir`.
* Sheets of OWNER footage stay local (DESIGN A2): never pass them to a publishing step.

## CI

numpy and Pillow are pinned **with hashes** in `.github/requirements/repo-hygiene.txt` (regenerated with
`tools/dependencies/update-python-locks.ps1`), so the pixel tests and the mutants' targets run in hosted Repo Hygiene
Python on both OSes. `test_look_judge_harness.CiPinsTests` fails if numpy/Pillow are missing under `GITHUB_ACTIONS`,
and everything that can be tested without drawing (sessions are built with byte-level fake drawers) is. A developer host
without the packages skips only the pixel classes.
