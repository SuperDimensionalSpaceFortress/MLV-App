# Look Assist objective floor + blind judge harness (LOOK-METRICS-JUDGE-1, hardened by -2 and -3)

Design of record: DUAL-VENUE DESIGN.md AMENDMENT 1 (A3 metrics) and AMENDMENT 2 (B3 objective floor, B4 model
judging, B5 judged matrix). Owner ruling 2026-09-30: models judge aesthetics; the owner is optional; nothing
waits on him. Bus evidence (TRAPS s8) says a model judging a blinded image pair can sit near chance, so
**the metrics GATE and the judges only REFINE**: a frame that fails the floor is not rescued by a judge.

**The rule the whole harness is built around: nothing is usable unless the harness itself proved it.** A result is
never accepted because something *said* it was fine, and an input is never accepted with part of it silently gone.
LOOK-METRICS-JUDGE-3 enforces that by DELETING the surfaces where a claim could stand in for a proof:

* **Isolation is derived, never read.** `run_session` records which runner CLASS ran; the tally derives identity and
  isolation from that class (only `ClaudeCliJudge` / `CodexExecJudge` can ever be usable) and never reads an
  `isolation`, `enforced` or `sealSha256` from a results file. The seal is verified against `sealed.bin` and the key;
  the isolation canary is **bound to the CLI version and command** of the run it vouches for.
* **Frame directories are all-or-nothing.** Every entry is a numbered PNG that decodes or a text sidecar; anything
  else refuses the whole directory. There is no list of image extensions or signatures to fall behind a new format.
* **One model resolver** (`look_judges.canonical_model`) feeds every identity check; an unrecognised name is refused.
* **judge-disagreement needs the same subject x criterion grid** in both entries, so a missing criterion cannot hide a
  disagreement.

## Layout

| file | job | needs |
|---|---|---|
| `look_floor_config.json` | every threshold, each as `{value, reason}`, at every depth; provisional until a judged corpus exists | - |
| `look_config.py` | loads/validates the config (a bare number ANYWHERE, a missing required threshold or an empty reason is refused); rubric digest + lock | stdlib |
| `look_metrics.py` | per-frame floor, letterbox policy, skin-tone drift, SSIM, CUDA-vs-CPU pair metrics, typed verdicts, the all-or-nothing frame index | numpy, Pillow |
| `judge_rubric.md` + `judge_rubric.lock.json` | the FROZEN rubric (anchored 1-5 x5 criteria, then pairwise, no defect colour named) and its sha256 | - |
| `look_pairs.py` | blind pair builder: image-bound opaque ids, mid-grey gutter, seed-decided slots, order-swapped twice, negative + positive controls | stdlib (+ Pillow to draw) |
| `look_judges.py` | the model resolver, the two shipped runners (`ClaudeCliJudge`, `CodexExecJudge`, tools off / confined), the closed runner set and its derivations, probes, process-tree-bounded runner, session runner | stdlib |
| `look_seal.py` | seals the answer key, full session, source frames and degraded sources into `sealed.bin` under a per-session key the judge never gets; the one path comparison | stdlib |
| `look_canary.py` | the LIVE isolation canary: the shipped judge command must not obtain a decoy key or source frame by any tool, an unconfined control must read BOTH; a report is re-judged from its raw evidence and bound to class, CLI version and command | stdlib |
| `look_tally.py` | image/rubric/key integrity, rubric-lock + seal + derived-isolation gates, flips, slot bias, both controls, completeness, `model_verdicts[]` entry, third-judge rule (two DIFFERENT judges, one score grid) | stdlib |
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

**A frames directory is all or nothing.** `index_frames` accepts a directory only when EVERY entry is either a text
sidecar (`.json`, `.txt`, `.md`; no NUL, valid UTF-8, at most 1 MiB, recorded under `inputs.*.sidecars` with its sha256) or
a regular file that **decodes completely as a PNG** and carries a numeric index (the last run of digits in its name,
unique in the directory). Anything else -- a sub-directory, a JPEG / PCX / TGA / anything that is not a PNG, a truncated
PNG, a PNG with no digits, a second file for one index, a `.csv`, a picture renamed `f-01.txt` -- refuses the **whole**
directory (exit 2, no verdict written) with a typed reason per offender (`NOT_A_PNG`, `PNG_DOES_NOT_DECODE`,
`NO_NUMERIC_INDEX`, `DUPLICATE_FRAME_INDEX`, `NOT_A_REGULAR_FILE`, `SIDECAR_NOT_TEXT`, `SIDECAR_TOO_LARGE`, `NO_FRAME`),
all named in one message. Sol r2 B3 found a valid `f-01.pcx` ignored while the sheet PASSed; the fix is not a longer
extension list, it is that no entry can be ignored. The same rule governs `--frames-dir`, `--baseline-dir`, both
`pair-metrics` sides and both `build-session` subjects. A directory that is missing or yields **zero** frames is refused,
and so is comparing a directory with itself (`floor --baseline-dir` equal to `--frames-dir`, `pair-metrics` with `--a-dir`
equal to `--b-dir`, `build-session` with `--a` and `--b` naming one directory): that always "agrees".

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
   (`droppedFrameAllowance`). A subject directory the all-or-nothing index refuses stops the build.
   **`capture`** (config sha256 + version, letterbox policy per subject, each frame's source/prepared size and
   letterbox decision, every common crop) is written to `session.json` and `answer_key.json` and copied into the entry.
   **Sealed by default:** the answer key, the full session record, the prepared source frames and the degraded
   sources go into `sealed.bin` (see *Judge isolation* below) and `session.json` is rewritten as a minimal public
   record (rubric digest, image digests: no subject names and **no seed**, from which kind, ordering and slot could be
   re-derived). The per-session **seal key is written to the file you
   name with `--seal-key-file`** (required; a file that does not exist yet, never inside the session, written before
   anything is sealed and removed again if sealing fails) **and is never printed**, so it cannot land in a log or a
   transcript; `--no-seal` leaves the secrets in the clear for debugging, and then no real judge will run and the tally
   is unusable. `unseal` extracts a sealed session for audit (never into the session directory).
3. `look_cli.py judge --session-dir SESS --runner claude:MODEL|codex:MODEL --producer-model M ...` -- each item runs
   in a fresh temp dir holding only `pair.png`, in a subprocess whose **whole process tree** is killed on timeout (the
   Windows `claude` / `codex` npm shims are `.cmd` files). Only the two runner classes in `look_judges.RUNNER_CLASSES`
   exist for the CLI; there is no `--judge-id` and no way to pass the CLI extra arguments, so the judge id is
   `<kind>:<canonical model>` and the confinement is the one that ships. The judge may not be the **model** of any
   `--producer-model` (the resolver: `sonnet` = `claude-sonnet-5-5` = any sonnet; a name it cannot place is refused; the
   Codex default is resolved to the real model from `config.toml`, else it counts as any OpenAI model). It refuses a
   changed rubric, a session whose images differ from its recorded digests, and a results file of another judge; it
   **re-judges** a stored verdict made against another image or rubric **or by another runner class, CLI version or
   command**. A shipped runner **refuses a session that is not sealed or still holds a plaintext secret (any letter
   case), and refuses to start when `LOOK_SEAL_KEY` is in its environment**. The results file
   (`mlv-app/look-judge-results/v2`) records the runner CLASS, model, the measured CLI version and the command digest, and
   no claim about isolation, enforcement or the seal: there is nothing there for a tally to believe.
4. `look_cli.py tally --session-dir SESS --results R.json --producer-model M --canary C.json --seal-key K --out T.json` --
   one `model_verdicts[]` entry; the key opens `sealed.bin` from memory (no key, no entry). **`usable` is true only if
   no `unusableReasons` entry applies**, among them: the session was frozen under the rubric lock verified now
   (`SESSION_RUBRIC_DIFFERS_FROM_LOCK`); the seal verifies with no plaintext secret beside it (`SEAL_NOT_VERIFIED`,
   `PLAINTEXT_SECRETS_BESIDE_THE_SEAL`); the runner class is in the closed set, its model resolves, its recorded command
   digest equals the one computed here and a CLI version was measured (`RUNNER_NOT_ALLOWLISTED`, `JUDGE_MODEL_UNRECOGNISED`,
   `RUNNER_COMMAND_DIFFERS_FROM_SHIPPED`, `CLI_VERSION_NOT_RECORDED`); a `--canary` report re-judges as HELD and was made
   for the same class, CLI version and command (`ISOLATION_CANARY_MISSING`, `..._NOT_HELD`, `..._FOR_ANOTHER_RUNNER` /
   `_CLI_VERSION` / `_COMMAND`); the judge's identity (`judgeId`, `family`) is derived, never read; every key item has a
   valid verdict bound to the key's image digest (`INCOMPLETE_ITEMS`, `IMAGE_CHANGED_SINCE_BUILD`,
   `ITEM_ID_NOT_BOUND_TO_IMAGE`); **every** negative-control item was answered `tie` and **every** positive-control item
   preferred the original (`CONTROL_FAILED`, `POSITIVE_CONTROL_FAILED`: an always-`tie` judge is unusable); no slot bias
   (exact binomial; `INSUFFICIENT_N` below 6 choices); at least `judge_validity.min_consistent_units` (4) real units agree
   across both orderings and no more than `max_discarded_unit_fraction` (0.34) were discarded as flips or tie-splits;
   the `capture` record is present, identical in key and session, and built under the config the tally runs with
   (`CAPTURE_*`, `CONFIG_DIFFERS_FROM_SESSION`); every lost frame has a recorded allowance
   (`UNACKNOWLEDGED_DROPPED_FRAMES`); and the producer guard was applied and passed. **A winner is shown only on a usable
   entry** (otherwise `winner` is `UNUSABLE` and the computed one sits in `withheldWinner`), the CLI exits 2 for an
   unusable entry (JSON still written), and a results file that is not the v2 shape is a typed refusal, not a traceback.
5. `look_cli.py judge-disagreement --entries T1.json T2.json` -- a third judge is needed when two judges are more than
   `judge_disagreement.third_judge_points` (config) apart. A comparison is `comparable` (exit 0) only when **all** hold:
   every entry is a usable tally entry with scores; the entries share `rubricSha256`, `orderSeed` and image set
   (`sessionMismatches`); every entry's rubric equals the current rubric lock (`rubricLockProblems`); they are **two
   different judges** (`identityProblems`): **every** model goes through `canonical_model`, always -- an unrecognised
   model is `MODEL_UNRECOGNISED`, two spellings of one model (any version of a family) are `DUPLICATE_MODEL`, each
   entry's `family` and `judgeId` must be the ones the resolver derives, `resultsSha256` must differ, an entry that
   says `usable` must carry the facts that make it so, and `--require-cross-family` needs two derived families (no
   typed-in cross-family status exists); and they cover **the same subject x criterion grid** (`coverageProblems`:
   `COVERAGE_DIFFERS`, `SUBJECT_GRID_DIFFERS`, `SCORES_NOT_THE_FULL_GRID`). A comparison that is not one answers
   `thirdJudgeNeeded: null`, never a reassuring `false`.

### Judge isolation: removed, derived, and proved live

A judge CLI is a general agent: it can run a shell, view images by absolute path and read anything its user can read.
Asking it not to is policy. The harness makes the secrets **unreachable at judge time** and then **derives** what it
concludes from facts it can check, instead of reading what a runner or a results file says:

1. **Sealed artifact.** `build-session` writes the answer key, the full session record (capture, drops, subject names, the
   order seed), the prepared source frames and the degraded sources into one file, `sealed.bin` (HMAC-SHA256-CTR, stdlib,
   encrypt-then-MAC, fresh nonce) under a random per-session key written to a file you name (never printed, never in the
   session directory). Beside it stay only the pair images, the judge-facing manifest and a minimal public `session.json`.
2. **The key never reaches a judge**: the judge commands refuse to start with `LOOK_SEAL_KEY` set, and the judge process
   gets an environment without it (and without any `LOOK_*` variable).
3. **Tools off, not asked off.** The Codex judge runs with every tool that could read a file disabled
   (`CODEX_DISABLED_FEATURES`), `--strict-config` and `--ignore-user-config --ignore-rules`. The Claude judge keeps only
   `Read`, confined to its scratch directory by `--restricted --safe-mode --strict-mcp-config`; any `permission_denials`
   entry (or a reply without the list) rejects the verdict.
4. **The tally derives the isolation** (`derive_isolation`) from the runner class `run_session` recorded: an exact type
   check against `RUNNER_CLASSES` (a look-alike or subclass is `UNLISTED:...`), the command digest recomputed, and the
   canary below. A runner that merely *asserts* isolation -- sol r2's unconfined callable judge -- cannot be usable: it is
   not in the production tree, and its class would not be in the set.
5. **Live proof, both runners:** `look_cli.py isolation-canary --runner claude|codex [--out C.json]` builds decoys named
   like the real secrets (a token-bearing `answer_key.json`, a plainly red `source-frames/cuda-00.png`) plus a real sealed
   session and asks the **shipped** judge command to obtain them by every tool it has. It must fail *after being seen to
   try*: only the CLI's own record counts (Claude's `permission_denials`, a refused tool call in stderr), not the model's
   own "CANNOT" (the prompt tells it to write that); a judge that does not attempt is retried, then
   `INCONCLUSIVE_JUDGE_DID_NOT_TRY`. A **control** with the confinement removed (always run; no `--no-control`) must read
   **both** decoys, each on its own, or the outcome is `INCONCLUSIVE`. The decoys are made with the default ACL, not
   `mkdtemp`'s owner-only one, so the CLI's own sandbox user can read them. The report carries the raw evidence plus
   `runnerClass`, `cliVersion` and `commandSha256`; `judge_canary` re-derives the outcome from it and `verify_canary`
   requires it to match the run being tallied, so a canary measured on one CLI version does not vouch for another. The
   scoring is unit-tested in CI; the live call runs by hand (hosted CI has neither CLI), transcript in the PR.

Not covered, stated plainly: (a) the *operator's own* capture folders are not the harness's to seal; they are out of the
Codex judge's reach (no file tool) and the Claude judge's (`--restricted`), and a future CLI that changes what its flags do
is caught only by re-running the canary, which the version binding now forces; (b) the pair images and manifest are visible
to the judge by design (that affects the flip test, not which subject is which); (c) an out-of-process judge lane is not in
the closed set, so its entry is never usable; (d) the canary report and tally entries are files the harness writes:
whoever can edit them can forge them, as they could edit this code. What is verified is internal consistency and the
binding to the run, not provenance.

### Cross-family (K6)

`look_cli.py probe-codex` reads `codex exec --help` for `--image` (`CROSS_FAMILY_FLAG_PRESENT_NOT_PROVEN`), and with
`--live-vision-dir` makes ONE bounded call on a synthetic image whose answer exists only in its pixels
(`CROSS_FAMILY_PROVEN_LIVE`). It also requires every tool switch the judge relies on (`codex features list`) plus
`--strict-config`: a Codex that cannot be confined is `CROSS_FAMILY_UNAVAILABLE` with the reason. Whether two judges are
cross-family is **derived from their models** at `judge-disagreement` time.

## Honest limits

* Thresholds are provisional; every one says why it is the number it is, and none has been calibrated on a judged corpus.
* The skin check is a colour region, not a face detector (sand and deck can land in it); that is why it is a drift
  against a baseline of the same scene, never an absolute.
* A judge's model is the one the operator asked the CLI for, resolved by `canonical_model`; the tally cannot prove which
  model actually answered, only that it was not a producer/hub model. Sealing hides the key from a judge; anyone who holds
  the seal key AND can edit the results and images can forge a session (consistency, not provenance).
* Judge isolation is **enforced** (sealed secrets, tools switched off, key scrubbed from the environment) and **derived**
  by the tally from the runner class and a canary bound to the CLI version and command it measured. A CLI upgrade
  therefore makes every older canary unusable until `isolation-canary` is re-run. The operator's own capture folders are
  outside what the harness seals (see *Judge isolation*).
* An explicitly given but **empty** `--baseline-dir`, `--config`, `--rubric`, `--rubric-lock`, `--canary`, `--seal-key` or
  `--seal-key-file` is an error, never "use the default". A folder copied and passed as a second subject is refused only
  when it is the same directory; a byte-identical copy cannot be told from a genuine bit-exact parity, so the entry lists
  those units under `integrity.realUnitsWithIdenticalSources` (they cannot show a preference) instead of guessing.
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
