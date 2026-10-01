# Look Assist objective floor + blind judge harness (LOOK-METRICS-JUDGE-1, hardened by -2)

Design of record: DUAL-VENUE DESIGN.md AMENDMENT 1 (A3 metrics) and AMENDMENT 2 (B3 objective floor, B4 model
judging, B5 judged matrix). Owner ruling 2026-09-30: models judge aesthetics; the owner is optional; nothing
waits on him. Bus evidence (TRAPS s8) says a model judging a blinded image pair can sit near chance, so
**the metrics GATE and the judges only REFINE**: a frame that fails the floor is not rescued by a judge.

**The rule the whole harness is built around:** no path lets an *unjudged, stale, incomplete, biased, self-judged
or hidden* result read as PASS / usable. Round 2 closed this as a class; the sections below say how.

**LOOK-METRICS-JUDGE-2 widens the class to REQUESTED INPUTS:** an input the caller asked for that is absent,
unmatched, unparseable or mis-named (a baseline directory without a subject's frame, a `frame-final.png` with no
index, a frame lost at build time) is never read as agreement, N/A-as-pass, PASS or usable. It is `INCOMPLETE` (or
`unusable`) with a typed reason, the same way on the floor, in pair metrics, in `build-session` and in the tally.
Round 2 adds the last members of the class: a result under **another rubric than the lock verified now**, **one judge
counted twice**, **a judge that could read the answer key**, an **explicitly empty** argument, and a **copy of one
directory** presented as two subjects. Each is `unusable` / not `comparable` / an error with a typed reason.

## Layout

| file | job | needs |
|---|---|---|
| `look_floor_config.json` | every threshold, each as `{value, reason}`, at every depth; provisional until a judged corpus exists | - |
| `look_config.py` | loads/validates the config (a bare number ANYWHERE, a missing required threshold or an empty reason is refused); rubric digest + lock | stdlib |
| `look_metrics.py` | per-frame floor, letterbox policy, skin-tone drift, SSIM, CUDA-vs-CPU pair metrics, typed verdicts | numpy, Pillow |
| `judge_rubric.md` + `judge_rubric.lock.json` | the FROZEN rubric (anchored 1-5 x5 criteria, then pairwise, no defect colour named) and its sha256 | - |
| `look_pairs.py` | blind pair builder: image-bound opaque ids, mid-grey gutter, seed-decided slots, order-swapped twice, negative + positive controls | stdlib (+ Pillow to draw) |
| `look_judges.py` | `ClaudeCliJudge`, `CodexExecJudge` (tools switched off), probes, family-level producer guard, process-tree-bounded runner, session runner (sealed-session gate, env scrub) | stdlib |
| `look_seal.py` | seals the answer key, full session, source frames and degraded sources into `sealed.bin` under a per-session key the judge never gets | stdlib |
| `look_canary.py` | the LIVE isolation canary: the shipped judge command must not obtain a decoy key or source frame by any tool, and an unconfined control must | stdlib |
| `look_tally.py` | image/rubric/key integrity, rubric-lock + seal + isolation gates, flips, slot bias, both controls, completeness, consistent-unit floor, `model_verdicts[]` entry, third-judge rule (two DIFFERENT judges) | stdlib |
| `look_cli.py` | `tiles`, `floor`, `pair-metrics`, `verify-rubric`, `probe-codex`, `build-session`, `judge`, `tally`, `judge-disagreement`, `unseal`, `isolation-canary` | - |

## The objective floor (gates)

Per frame:

* clipped highlights <= 1 %, crushed shadows <= 2 % -- the definitions are `make-contact-sheet.py`'s `channel_stats`
  (reused, not re-implemented), so a number here matches every existing stats sidecar;
* saturation ceiling: mean HSV saturation and the share of oversaturated pixels;
* skin-tone-region hue drift <= ~5 deg **against a baseline frame**:
  * **no baseline requested** -> `NOT_APPLICABLE` / `NOT_REQUESTED` (never a pass; the sheet says how many frames it applied to);
  * **a baseline requested (`--baseline-dir`) but missing the subject's frame index** -> that frame is `NOT_EVALUABLE`
    (`BASELINE_REQUESTED_BUT_NO_FRAME_FOR_INDEX`) and the sheet `INCOMPLETE` (`BASELINE_MISSING_FRAMES`, exit 2); a
    baseline frame with no subject frame is `INCOMPLETE` too (`BASELINE_FRAMES_WITHOUT_SUBJECT`). Sol r2 found that a
    baseline directory that simply lacked the frame let a moved skin PASS with exit 0;
  * a requested baseline whose frames have **no skin region** *and the subject has none either* -> `NOT_APPLICABLE` for
    those frames, recorded as `skinCheck.status = REQUESTED_NOT_APPLICABLE` (or `APPLIED_PARTIAL`) with
    `notApplicableFrames` and printed on the console line, so it cannot be mistaken for "checked and fine". If the
    **subject has a skin region and the requested baseline does not**, the baseline is not the same scene (or was crushed
    to nothing): that frame is `NOT_EVALUABLE` (`SKIN_IN_SUBJECT_BUT_NOT_IN_BASELINE`) and the sheet `INCOMPLETE`;
  * **`--baseline-dir ""` is a baseline that was requested and is not there** (an unset variable in a wrapper script): an
    error, exit 2. "Given" is `is not None`, never truthiness. An empty `--config ""` is refused the same way;
  * a baseline that HAS a region while the subject lost it, or kept less than `skin_region_retain_fraction` of it ->
    **FAIL** (`SKIN_REGION_LOST_OR_SHRUNK`): a look that pushes skin out of the colour box is the worst drift;
  * otherwise the drift is measured over the **baseline's mask** (the same pixels in both frames) when the two frames
    align after a few-pixel centre crop, so moving only part of the skin still shows; the verdict records `maskBasis`.
    Frames that **cannot** be aligned are `NOT_EVALUABLE` (`SKIN_MASKS_NOT_ALIGNABLE`, sheet `INCOMPLETE`): the old
    fallback to each frame's own region could miss a partial skin move;
  * a baseline that was given but cannot be read makes the frame `NOT_EVALUABLE`, not "no baseline".

**Nothing is hidden from the floor unless the caller says so.** A playback capture can include black window bars (the
UM CUDA fixture tiles do), but pixels alone cannot tell a bar from a crushed region of the scene. So by default the
**full frame is measured** and a barred capture fails "crushed shadows" loudly, with the dark bands it saw listed under
`geometry.letterbox.candidate` and the sheet's `framesWithDarkBandsMeasuredAsScene`. To exclude bars the caller either
**declares** them (`--letterbox-bars top=34,bottom=34`; each declared band must really be dark or it is refused) or
**opts in** to `--letterbox auto-symmetric` (a pair of opposite bands within `symmetry_tolerance_px` of each other).
A band on ONE side is always scene content. The choice (mode, provenance `DECLARED` / `AUTO_SYMMETRIC` / `NONE`,
the share excluded, and `crushed_shadow_pct_full_frame`) is written into every frame verdict.
`build-session` applies the same rule **only if it is given the same flags** (the two commands take their letterbox
flags independently); it records the policy per subject, every frame's letterbox decision and size, any common crop
and the config sha256 under `capture` in `session.json`, `answer_key.json` and the tally entry, so a session built
with exclusion on is never indistinguishable from one built without it.

**Frame directories are never skipped silently.** `index_frames` lists every entry it did not place under `skipped`
(and in the verdict's `inputs`). An image file that could have been a frame but was not indexed (a `.png` with no
digits in its name, a `.jpg` / `.tga` / `.jp2` / `.heic` / `.avif` / `.jxl` / `.hdr` / `.tif` / `.exr` / `.webp` / `.bmp` /
camera-raw file, any other image-like extension, **or a file whose first bytes are an image container whatever it is
called**, a second file claiming an index already taken) is *blocking*: the sheet is `INCOMPLETE`
(`FRAME_FILES_NOT_INDEXED`, `BASELINE_FILES_NOT_INDEXED`, `A_FILES_NOT_INDEXED`, `B_FILES_NOT_INDEXED`). A sidecar such
as `notes.txt` is only listed. A directory that is missing or yields **zero** frames is an error (exit 2), and so is comparing a directory with itself
(`floor --baseline-dir` equal to `--frames-dir`, `pair-metrics` with `--a-dir` equal to `--b-dir`, `build-session` with
`--a` and `--b` naming one directory): that always "agrees".

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
   **Lost is not chosen:** frames left out by `--frame-ids` / `--max-frames` are a *selection*; every other drop (an
   unshared frame, outside the crop tolerance, a requested id no subject has) is a *loss*. `session.json` carries
   `droppedFramePolicy` (`lossFrameIds`, `selectionFrameIds`, `allowance`). A build with a loss and no
   `--allow-dropped-frames REASON` writes the session, marks it, and **exits 2**; the tally calls it
   `UNACKNOWLEDGED_DROPPED_FRAMES`. With the allowance the reason is recorded in the session and the tally entry
   (`droppedFrameAllowance`). A subject directory with an image file that cannot be indexed is refused outright.
   **`capture`** (config sha256 + version, letterbox policy per subject, each frame's source/prepared size and
   letterbox decision, every common crop) is written to `session.json` and `answer_key.json` and copied into the entry.
   **Sealed by default:** the answer key, the full session record, the prepared source frames and the degraded
   sources go into `sealed.bin` (see *Enforced judge isolation* below) and `session.json` is rewritten as a minimal public
   record (rubric digest, seed, image digests: no subject names). The per-session **seal key is printed once** (or written
   to `--seal-key-file`, which may not sit inside the session); `--no-seal` leaves the secrets in the clear for debugging,
   and then no real judge will run and the tally is unusable. `unseal` extracts a sealed session for audit (never into
   the session directory).
3. `look_cli.py judge --session-dir SESS --runner claude:MODEL|codex:MODEL --producer-model M ...` -- each item runs
   in a fresh temp dir holding only `pair.png`, in a subprocess whose **whole process tree** is killed on timeout
   (the Windows `claude` / `codex` npm shims are `.cmd` files; a plain kill left the real CLI running). The judge may
   not be in the **model family** of any `--producer-model` (one alias table: `sonnet` = `claude-sonnet-5-5` = any
   sonnet; names that cannot be placed fail closed; the Codex default is resolved to the real model from
   `config.toml`, and an unresolvable one counts as any OpenAI model). It refuses to run if the rubric changed after
   the lock, if the session recorded another digest, or if the images on disk differ from the session's digests; it
   **re-judges** any stored verdict made against another image or rubric **or made before the session was sealed / under
   another confinement**, and refuses a results file from another judge. A real runner **refuses a session that is not
   sealed or still holds a plaintext secret, and refuses to start when `LOOK_SEAL_KEY` is in its environment**. The
   results file records the seal digest, whether the judging was under the seal, and the runner's isolation record.
4. `look_cli.py tally --session-dir SESS --results R.json --producer-model M --seal-key K --out T.json` -- one
   `model_verdicts[]` entry. The key opens `sealed.bin` from memory; without it, or with a wrong key, there is no entry
   (exit 2). **`usable` is true only if every one of these holds** (each is a named `unusableReasons` entry):
   **the session was frozen under the rubric lock verified now** -- the shipped lock, or `--rubric` / `--rubric-lock`
   named explicitly, whose digest the session AND every verdict must equal (`RUBRIC_LOCK_NOT_VERIFIED`,
   `SESSION_RUBRIC_DIFFERS_FROM_LOCK`); **the seal verified and the verdicts were made under that same seal**
   (`SEAL_NOT_VERIFIED`, `JUDGED_WITHOUT_OR_AFTER_SEAL`); **the runner's isolation was enforced by the harness**
   (`JUDGE_ISOLATION_NOT_ENFORCED`; a plain callable judge never claims it); a null or malformed `capture.configSha256`
   is `CAPTURE_CONFIG_SHA_NOT_RECORDED`;
   every key item has a valid verdict (`INCOMPLETE_ITEMS`); every verdict quotes the key's image digest, the image on
   disk still hashes to it and the item id is derived from it (`IMAGE_CHANGED_SINCE_BUILD`, `ITEM_ID_NOT_BOUND_TO_IMAGE`);
   the results name no unknown item; **every** negative-control item was answered `tie` (a missing or invalid one is
   `CONTROL_INCOMPLETE`); **every** positive-control item preferred the original (`POSITIVE_CONTROL_FAILED` -- so an
   always-`tie` judge is unusable); no slot bias (exact binomial; `INSUFFICIENT_N` below 6 choices);
   at least `judge_validity.min_consistent_units` (4, from the config) real units agree across both orderings and no
   more than `judge_validity.max_discarded_unit_fraction` (0.34) of the judged real units were discarded as flips or
   tie-splits (`TOO_MANY_DISCARDED_UNITS`); the session's `capture` is on record, identical in the key, and was built
   under the config the tally runs with (`CAPTURE_NOT_RECORDED`, `CAPTURE_SESSION_KEY_MISMATCH`,
   `CONFIG_DIFFERS_FROM_SESSION`); every lost frame has a recorded allowance (`UNACKNOWLEDGED_DROPPED_FRAMES`,
   `DROPPED_FRAME_POLICY_MISMATCH`); and the producer guard was applied and passed. Order-flipping votes are discarded. **A winner is shown only on a usable
   entry** (otherwise `winner` is `UNUSABLE` and the computed one sits in `withheldWinner`), and **the CLI exits 2
   whenever the entry is unusable** (the JSON is still written).
5. `look_cli.py judge-disagreement --entries T1.json T2.json` -- a third judge is needed when two judges are more than
   `judge_disagreement.third_judge_points` (config) apart. A comparison is `comparable` (exit 0) only when **all** hold:
   every entry is a usable tally entry with scores; the entries share the same `rubricSha256`, `orderSeed` and image set
   (`sessionMismatches`); every entry's rubric equals the **current rubric lock** (`rubricLockProblems`); and they are
   **two different judges** (`identityProblems`): distinct `judgeId`s, distinct `model`s (compared trimmed and
   case-folded) and distinct `resultsSha256` -- the same file passed twice, one judge under two names, or a copied
   results file is `DUPLICATE_*`, never agreement. When cross-family is claimed (an entry says
   `CROSS_FAMILY_PROVEN_LIVE`, or `--require-cross-family`) at least two model families must take part, each family
   derived from the model rather than trusted from the entry. A comparison that is not one answers
   `thirdJudgeNeeded: null`, never a reassuring `false`. Every config value is type- and range-checked at load (a
   reasoned string `"false"` is refused).

### Enforced judge isolation (sol B3 / fable H2)

A judge CLI is a general agent: it can run a shell, view images by absolute path and read anything its user can read.
Asking it not to (flags, a prompt, "read-only") is policy. Round 2 makes the secrets **unreachable at judge time**:

1. **Sealed artifact.** `build-session` writes the answer key, the full session record (capture, drops, subject names), the
   prepared source frames (`cuda-00.png`...) and the degraded sources into one file, `sealed.bin`: HMAC-SHA256-CTR
   (stdlib; encrypt-then-MAC, fresh nonce) under a random per-session key that is printed once and **never written into
   the session directory**. What stays beside it is what a judge may see: the pair images, the judge-facing manifest and
   a minimal public `session.json`. A tampered, truncated or swapped file, or the wrong key, opens nothing.
2. **The key never reaches a judge.** The judge commands refuse to start when `LOOK_SEAL_KEY` is in their environment; the
   judge process gets an environment without it (and without any `LOOK_*` variable). `tally` takes the key.
3. **Tools off, not asked off.** The Codex judge runs with `shell_tool`, `unified_exec`, `view_image`, `code_mode_host`,
   `browser_use`, `computer_use`, `apps`, `plugins`, `skill_search`, `hooks`, `memories`, `multi_agent(_v2)`, `goals`,
   `sleep_tool` and web search disabled, `--strict-config` (a CLI that no longer knows one of them fails instead of running
   with the tool on) and `--ignore-user-config --ignore-rules`: it can only look at the attached picture. `probe-codex`
   reports `CROSS_FAMILY_UNAVAILABLE` (recorded in the entry) for a Codex that cannot be confined, so it is not run
   unblinded. The Claude judge keeps only `Read`, confined to its scratch directory by `--restricted --safe-mode
   --strict-mcp-config`, and **any `permission_denials` entry (or a reply without the list) rejects the verdict** (recorded
   as an item error, never scored).
4. **The tally refuses a judge that could have read the key.** Verdicts made before the session was sealed, under another
   seal, or by a runner whose isolation the harness did not enforce are unusable; `run_session` also throws away any stored
   verdict from before a seal or confinement change and judges it again.
5. **Live proof, both runners:** `look_cli.py isolation-canary --runner claude|codex` builds decoys named like the real
   secrets (an `answer_key.json` holding a random token, a plainly red `source-frames/cuda-00.png`) plus a real sealed
   session, asks the **shipped** judge command to obtain them by every tool it has, and requires it to fail; a **control**
   run with the confinement removed must succeed (a control that did not read the decoys proves nothing:
   `INCONCLUSIVE`, not held). The sealed artifact is checked too (no plaintext beside it; neither the token nor a PNG
   header in its bytes). The transcripts are in the PR; the scoring logic and orchestration are unit-tested in CI, the
   live call is not (hosted CI has neither CLI).

What this does **not** cover, stated plainly: (a) the *operator's own* capture folders (the directories you passed to
`--a` / `--b`) are not the harness's to seal; they are out of the Codex judge's reach because it has no tool that can open
a file, and out of the Claude judge's reach because `--restricted` confines `Read` to the scratch directory, both proved
live, but a future CLI that changes what its flags do would need the canary re-run; (b) the pair images and manifest are
visible to the judge by design (a judge able to list the session directory would see sibling orderings of the same
unit: that affects the flip test, not which subject is which); (c) a judge that can run arbitrary code *outside* these CLIs
is outside this guarantee (an out-of-process `CallableJudge` lane records `enforced: false` and its tally is unusable).

### Cross-family (K6)

`look_cli.py probe-codex` reads `codex exec --help` for `--image` (`CROSS_FAMILY_FLAG_PRESENT_NOT_PROVEN`), and with
`--live-vision-dir` makes ONE bounded call on a synthetic image whose answer exists only in its pixels
(`CROSS_FAMILY_PROVEN_LIVE`). `look_judges.select_second_judge` returns a Codex judge only when the probe is
proven; otherwise a second Claude model and `CROSS_FAMILY_UNAVAILABLE`, which the entry carries. The probe also lists
`codex features list` and requires every tool switch the judge relies on (`CODEX_DISABLED_FEATURES`) plus `--strict-config`:
a Codex that cannot be confined is `CROSS_FAMILY_UNAVAILABLE` with the reason, never run unblinded.

## Honest limits

* Thresholds are provisional; every one says why it is the number it is, and none has been calibrated on a judged corpus.
* The skin check is a colour region, not a face detector (sand and deck can land in it); that is why it is a drift
  against a baseline of the same scene, never an absolute.
* A judge's identity in the results file is what the runner recorded; the tally cannot prove which model actually
  answered, only that it was not a producer/hub family.
* Judge isolation is **enforced** (sealed secrets, tools switched off, key scrubbed from the environment) and proved live
  by `isolation-canary` for both runners on this host, with a control that shows the decoys ARE readable without the
  confinement. It is proved for the CLI versions measured (claude 2.1.286, codex-cli 0.159.3); re-run the canary after
  upgrading either. The operator's own capture folders are outside what the harness seals (see *Enforced judge isolation*).
* Sealing hides the key from a judge; it does not prove which model answered. A results file is still what the runner
  recorded, and anyone who holds the seal key AND can edit the results and images can forge a session (consistency, not
  provenance, as before).
* A requested baseline whose frames contain **no skin region** is recorded (`REQUESTED_NOT_APPLICABLE`), not failed:
  there is nothing to drift. Read the status, not just PASS.
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

The four dispatch-only build workflows (`Windows.yml`, `Linux.yml`, `macOS-Intel.yml`, `macOS-Arm64.yml`) install the
same lock with `--only-binary=:all: --require-hashes`, so each runner needs cp313 wheels of both pins. `ci_wheel_proof.json`
lists them (read from PyPI's JSON API, bounded, 2026-10-01): numpy 2.5.3 and Pillow 12.3.0 both ship `win_amd64`,
`manylinux_2_27/2_28 x86_64`, `macosx x86_64` and `macosx arm64` cp313 wheels, and the lock carries a hash for every
file of both versions (66 and 87). `CiPinsTests` ties the proof to the workflows' runner labels and to the lock's hash
counts, so a new runner or a re-lock that drops files fails the test instead of a dispatch run.
