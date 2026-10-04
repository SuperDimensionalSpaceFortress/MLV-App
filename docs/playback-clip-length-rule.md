# Playback clip-length rule (venue playback never loops, never replays)

Status: **enforced in code, in the app and in the tools**. Owner rule of 2026-09-30
(PLAYBACK-CLIP-LENGTH-ENFORCE-1 closed the argument / mode / launcher class; ENFORCE-2 makes **the app itself the gate**).

> "it did not use a long 20-30 second clip. it looped a super short clip. I have brought this to your
> attention before and it keeps happening again. DURABLY FIX!"

## The rule

Any leg that **plays** the app - speed, pacing, smoothness, a LOOK / contact sheet, a smoke - on any venue
(Bachelor, Ultra-Magnus, CUDA or CPU) uses **at least 20 s of real footage**, over a play window that
**never exceeds the clip**, and **never loops or replays**.

The class this file pins: **every programmatic Play in the app plays >= 20 s of real footage from the effective
range - never less, never looping, never replaying - or is refused BEFORE Play.** Concretely: the window the caller
REQUESTS (the time its own stop timer / hold lets Play run) must itself be >= 20 s (`PLAY_DURATION_TOO_SHORT`), the
footage from the CURRENT position to the cut-out the ENGINE will play must be >= that window, the process admits
ONE programmatic Play, Loop is never enabled by automation, no automation mode stops Play before the 20 s floor,
and every wrap, jump-to-first-frame and restart is counted and invalidates the run.

The tracked fixtures (`tiny_dual_iso`, 2 frames; `large_dual_iso`, 16 frames, about 0.1-0.7 s) are for unit
tests and non-playing checks only. They are never played on a venue.

## THE APP IS THE GATE (ENFORCE-2)

Why ENFORCE-1 was parked: the tool-side gates read the clip's HEADER, but the app starts playback before any
range check. The GUI-smoke Look Assist warm-up forced Loop on and played the receipt's cut range (2-16 frames)
before the cut-range refusal ran, and a direct `MLVApp.exe --profile-playback --exercise-play-action` had no
app-side gate at all. Now `MainWindow::programmaticPlay(site, requestedSeconds)` is the ONLY place the app
triggers Play on its own behalf. It evaluates `playback_frame_range::evaluatePlayableWindow` (pure, unit-tested
in `tests/console/test_playback_frame_range.cpp`): the cut range is normalized exactly as the Play path does,
the window is measured from the live slider position to the cut-out, and it must be `>= max(20 s, requested)` -
and the **requested** window must itself be `>= 20 s` (a caller that asks for 1 s is refused even on a 30 s clip).
The range measured is the one the **engine** plays: with `MLVAPP_F3_DISABLE_CUT_RANGE_REPAIR` set the engine
leaves a collapsed or inverted range alone, so the gate does too (`engineCutRangeForPlay`); that is the only
environment variable in `platform/qt` that touches the cut range, and the tools refuse it as well. The caller's
window is evaluated even when Play is already running (an already-running Play is never adopted).
A `ProgrammaticPlayLedger` then admits the first programmatic Play of the process and refuses every later one
(`REPLAY_REFUSED`: restart, re-Play, stress switch, contact-sheet replay). Refusals are typed, path-free, exit 14,
and happen **before Play** (zero presented frames). User input (`on_actionPlay_triggered`, the Loop menu) is
never gated.

| Programmatic entry (`platform/qt/MainWindow.cpp`) | Gated how |
|---|---|
| autoplay hook, `MLVAPP_AUTOPLAY_*` (constructor lambda, normal GUI only) | Loop forced off, `programmaticPlay("autoplay", seconds)` (< 20 s is `PLAY_DURATION_TOO_SHORT`); `autoplay.refused reason=<typed>`; `MLVAPP_AUTOPLAY_LOOP` ignored. The smoke / profile windows are built with `argv = { appName }` so the hook cannot arm there, and a smoke / profile launch carrying `MLVAPP_AUTOPLAY_SECONDS` is refused up front (`AUTOPLAY_REFUSED_IN_AUTOMATION`, exit 14) |
| profile Look Assist settle (`--exercise-look-assist-settle/-toggle`) | `programmaticPlay("profile-look-assist-settle", 20)`; **Loop is no longer enabled**; Play is held until the engine has CONSUMED ceil(20 x native fps) source frames (ENFORCE-3; it used to stop at 12 s or when the diagnostics settled); a Play that ends earlier is a typed failure; the toggle exercise's recheck settle never asks for a second Play (the process admits one); refusal returns exit 14 |
| profile `--exercise-play-action` | `programmaticPlay("profile-exercise-play-action", 20)`; exit 14 before Play; Play is held until the source frames are consumed (it used to stop at the first advancing frame / 5 s) |
| GUI-smoke Look Assist warm-up (was ~8961-8989) | **REMOVED.** No warm-up Play, no Loop: Look Assist warms during the measured pass |
| GUI-smoke measured Play | `programmaticPlay("gui-smoke-measured", min(--seconds, presented-frames / fps))` (the sooner of the two ends Play, so that is the window); `--seconds` < 20 is refused at parse (`PLAY_DURATION_TOO_SHORT`, exit 14); a `--presented-frames` target under 20 s of frames is `PLAY_WINDOW_TOO_SHORT`; the clock starts once Play has started, so the whole window is played; a Play that ends under the floor is a typed failure, never a pass |
| clip-lifecycle stress | the Play is stopped at the switch, so the switch may only happen after the 20 s floor (`--stress-switch-at-ms` default 20000, below that refused `PLAY_DURATION_TOO_SHORT`; the runner exits 41); the second clip is opened, seeked and closed but **never played**; the **restart Play is REMOVED** (it would be a replay) |
| contact-sheet playback pass (was ~10322, up to 50 re-Plays) | **REMOVED.** Since CONTACT-SHEET-PLAYBACK-PARITY-1 the default sheet is grabbed DURING the measured Play (no second Play; each grab's readback cost is recorded as `grab_ms`, totalled with `fps_excluding_grabs` on `gui_smoke.contact_sheet`). `--contact-sheet-seek-mode` (no Play, `playback_path=false`) is the labelled alternative; `--contact-sheet-seek-dir` adds a paired seek sheet (attr-3 job: `-ContactSheetPairedSeek`) |
| Play pressed on the last frame (`on_actionPlay_triggered`, ~23248) | counted by the wrap recorder (`jump_to_first_count`); the position-aware window already refuses a programmatic Play there |
| any Play start after the first (`on_actionPlay_toggled`, ~27635) | counted (`restart_count`); `wrap_count = engine wraps + jump-to-first + restarts`, any > 0 is `INVALID_LOOPED` |
| Loop at every automation entry | `forceLoopOffForAutomation` (only ever unchecks; Loop is not declared checked in `MainWindow.ui` and is not persisted) |

`tools/profiling/test-app-play-gate-offscreen.ps1 -Exe <MLVApp.exe>` launches the tracked short fixture through
each entry reachable offscreen and asserts the app refuses before Play. The static class test
(`test_playback_clip_length_gate.py`, `AppPlayGateStaticClassTests`) fails on any `actionPlay->trigger()` /
any other use of the Play or Loop action (alias, split line, variable `setChecked`, `invokeMethod`, a synthesized
Space key) outside three PINNED helper bodies - no `allowlisted:` comment can exempt a line - on a programmatic
Play / Stop site or requested-window expression nobody reviewed, and on a gate that triggers Play before it
evaluated the window and admitted it; it is mutation-tested (`tools/repo_hygiene/app_play_scan.py`).

### Every automation path that ends Play on its own clock, and its resolution

| Path | Resolution |
|---|---|
| `--seconds` / `MLVAPP_AUTOPLAY_SECONDS` timer under 20 s | refused before Play (`PLAY_DURATION_TOO_SHORT`) |
| `--presented-frames` early stop | the sooner of timeout and target is the window; refused under 20 s |
| lifecycle stress switch (stopped Play at ~1 s by default) | switch only at >= 20 s, default 20000 ms; second Play still refused (`REPLAY_REFUSED`) |
| profile Look Assist settle (stopped at 12 s) | requests and holds 20 s |
| `--exercise-play-action` (stopped at the first frame / 5 s) | requests and holds 20 s |
| a Play the engine ends early (end of range, jump) | the wait loops fail closed (`PLAY_DURATION_TOO_SHORT`) rather than report a pass |

## Where else it is enforced

| Layer | What | Refusal |
|---|---|---|
| `tools/profiling/gui-smoke-length-gate.ps1` | Reads the 52-byte `MLVI` header: frame count, `fps = nom/denom`; spanned sets are summed and must be complete. The **play window** (and a `-TargetPresentedFrames` early stop) must itself be >= 20 s (`-ClipOnly` is the opt-out for callers that play nothing) | `CLIP_TOO_SHORT`, `PLAY_WINDOW_TOO_SHORT`, `CLIP_LENGTH_UNKNOWN` |
| `run-release-gui-smoke.ps1` (choke point) | Gate first; `--loop` never produced, `-AllowLoop` gone; pass-through (`-AdditionalArgs`, `-ExtraEnvironment`) and an **inherited** `MLVAPP_AUTOPLAY_*` refused; the verdict is applied after the run | exit 41 / 42 / 44 / 43 |
| `run-release-playback-profile.ps1`, `validate-visible-playback.ps1`, `capture-reference-frame.ps1`, `start-release-cuda-playback.ps1` | Same gate and the inherited-autoplay refusal; `capture-reference-frame.ps1` defaults to a pinned frame at `ceil(20 * fps)` presented frames; ENFORCE-3 r2: each (except the interactive `start-release-cuda-playback.ps1`) also applies the receipt oracle | exit 41 / 42 / 44; 43 INVALID |
| export launchers (`run-release-cdng-export-profile.ps1`, `run-release-cuda-dng-export.ps1`) | refuse every play-capable pass-through option | exit 44 |
| `bachelor/playback-attr-3-cuda-job.ps1` | `-PlaySeconds` (floor 20); fixture ids refused at generation; the job summary carries the typed `smokeRefusalReason` | `PLAYBACK_ATTR3_...` |
| runtime backstop | `PlaybackWrapRecorder`; `playback_smoke.summary/.gate` carry `wrapped`, `wrap_count`, `jump_to_first_count`, `restart_count`; the runner's `Get-GuiSmokeLoopVerdict` (executed and mutation-tested, including the runner's application of it) | `INVALID_LOOPED`, exit 43 |
| `test_playback_clip_length_gate.py` scan | fails if any script under `tools/` / `.github/` names a play token (also **composed**: `'a' + 'b'`, `-f`, `-join`, backticks) or launches the exe forwarding caller arguments, without the gate or a listed allowlist entry | CI red |

Typed verdicts never name the clip path.

## ENFORCE-3 (owner rule 2026-10-01): 20 s means 20 s of SOURCE FRAMES

ENFORCE-2 measured "20 s" three different ways -- admission with the clip's native fps, the engine with
`getFramerate()` (a persisted `fpsOverride` changes it), and every automation stop with a wall clock -- so a venue whose
saved override was 12 fps stopped after 20 s of wall clock having covered ~10 s of footage. The unit is now the SOURCE
FRAME:

- The engine **counts** the distinct source frames it advanced (`SourceFrameAdvanceCounter`, fed from the engine tick in
  `playbackHandling`, every backend). `consumed()` is monotonic and never decreases: a wrap or backward step is never
  counted, a forward jump that is not an engine tick (a seek, a snap at Play start) is never counted *and never erases
  what was counted*, a second `begin()` is ignored.
- A Play must consume `required_source_frames = ceil(max(20, requested) x NATIVE fps)`. Admission requires a window
  holding that many frames **and** an engine pace that can consume them inside the safety budget.
- Every automation stop waits on `programmaticPlayState` (source frames consumed). A wall clock survives only as the
  safety net, and its expiry is a typed failure. The net **scales with the pace the venue must sustain**:
  `requested / kMinSustainedPaceFraction (0.5) + kPlaySafetyMarginMs (15 s)`, i.e. 55 s for a 20 s window. A venue that
  holds under half the native pace is refused up front when its engine pace is known (`PLAY_PACE_TOO_SLOW`, before
  Play) and ends early with the **same** token when the pace only shows once Play runs (the measured pace cannot reach
  the requirement inside the budget; judged after 8 s of Play and two frames) -- never a run that can only time out.
- The summary (`playback_smoke.summary`) and the profile receipt carry `source_advanced`, `required_source_frames`,
  `native_fps`, `pace_fps`, `fps_override`; the receipt oracle turns anything short of the requirement into
  `INVALID_SOURCE_FRAMES`, never a pass.

### Typed refusals and exit codes

| Token | Where | Exit |
|---|---|---|
| `PLAY_DURATION_TOO_SHORT`, `CLIP_TOO_SHORT`, `CLIP_LENGTH_UNKNOWN`, `PLAY_WINDOW_TOO_SHORT` | the app's gate, before Play (ENFORCE-2) | 14 |
| `PLAY_PACE_TOO_SLOW` | refused before Play (pace known and under the minimum), or the run ended early because the measured pace cannot reach the requirement | 14 |
| `SOURCE_FRAMES_SHORT` | the engine ended the Play (end of range, jump) before the requirement | 14 |
| `PLAY_SAFETY_TIMEOUT` | the safety net expired; typed, never a pass | 14 |
| `REPLAY_REFUSED` | a second programmatic Play in the process | 14 |
| `AUTOPLAY_REFUSED_IN_AUTOMATION` | a smoke / profile launch carrying `MLVAPP_AUTOPLAY_SECONDS` | 14 |
| `SETTINGS_ISOLATION_FAILED` | the run-scoped settings store could not be created | 14 |
| the autoplay hook (normal GUI) | refused / ended early / timed out: latched and returned as the **process exit code** even without `MLVAPP_AUTOPLAY_EXIT`; a Play that was already running when the hook asked is **reset and measured from now** | 14 |
| `INVALID_SOURCE_FRAMES`, `INVALID_LOOPED` | the runner / launchers' receipt oracle (`Get-GuiSmokeLoopVerdict`, `Get-GuiSmokeEvidencePlayVerdict`) | exit 43 |
| `PLAY_SAFETY_TIMEOUT` (launcher kill), `PLAY_NOT_FINISHED`, `APP_EXIT_NONZERO` | `Get-GuiSmokeEvidencePlayVerdict`: the launcher had to kill the app, the app never reported an exit code, or exited non-zero | exit 43 |
| `SOURCE_FRAMES_INVALID` | the attribution job's oracle (`Get-AttrCudaSourceFramesVerdict`) | exit 29 |

### Pacing isolation: the venue's persisted pacing is never read

`QSettings( UserScope, org, app )` is always the native store, so the app cannot be redirected from outside; every
settings open goes through `automation_settings::openAppSettings()` (`platform/qt/AutomationSettings.h`). An automation
run (`--gui-smoke-playback`, `--profile-playback`, the `MLVAPP_AUTOPLAY_*` hook) calls `automation_settings::isolate()`
**before the first `QSettings` anywhere** (`main.cpp`) and reads and writes an INI file in a directory of its own: a fresh
temp directory removed at exit, or the one named by `MLVAPP_AUTOMATION_SETTINGS_DIR` (the offscreen probe seeds
`fpsOverride=true` / `frameRate=12` there; it never writes the user's real settings and checks they did not change).
A run that cannot create its store does not start (`SETTINGS_ISOLATION_FAILED`). `isolateAutomationPacing()` stays as the
second line of defence: `getFramerate()` ignores the override, drop-frame mode is pinned on, and the pin is never written
back. The run therefore applies the app's compiled defaults, not the venue's other persisted options either, and
`capture-reference-frame.ps1` records `appSettings.store = run_scoped` instead of reading the user's registry hive.
The static class test fails on any raw `QSettings( UserScope|SystemScope, ... )` in `platform/qt` outside that header.

### Launchers that start the app for an evidence Play carry the receipt oracle

No tool may end an evidence Play on a clock of its own or trust the app's exit code alone (a binary that predates
ENFORCE-3 exits 0 after a wall-clock hold and writes no `source_advanced`). Each launcher below WAITS for the app to end
its own Play (its only budget is the app's safety net plus open / settle time; a process still running past it is killed
and reported as `PLAY_SAFETY_TIMEOUT`), then reads the app's receipt and applies the one decision in
`tools/profiling/gui-smoke-length-gate.ps1`:

| Launcher | Receipt it reads | On INVALID |
|---|---|---|
| `run-release-gui-smoke.ps1` (the choke point; its process budget is `Get-GuiSmokePlaySafetyMs` + open / settle) | `playback_smoke.summary` | exit 43 (`validation.ok = false`) |
| `validate-visible-playback.ps1` (the filmstrip capturer; it used to kill the app after `SettleMs + Captures x IntervalMs`, ~14 s) | `playback_smoke.summary` in its log dir | exit 43 |
| `capture-reference-frame.ps1` | `playback_smoke.summary` in `-OutDir`; the manifest records `sourceFrames` | exit 43 (a non-zero app exit stays exit 4) |
| `run-release-playback-profile.ps1` (a play-capable option) | `metadata` of the `--output` profile receipt: `programmatic_play_admitted` must be PRESENT and > 0 (ENFORCE-4: an absent field is INVALID, never "nothing was played") plus every oracle field | exit 43 |
| `lookassist-wb-determinism.ps1`, `lookassist-wb-multiclip-probe.ps1`, `measure-wb-solve-distribution.ps1` (consumers; see ENFORCE-4) | the launcher's exit code | an invalid run is excluded and the SCRIPT exits 43 |
| `bachelor/playback-attr-3-cuda-job.ps1` | the measured session's summary line | exit 29 |

`tools/repo_hygiene/test_playback_launcher_receipt_oracle.py` EXECUTES the oracle and each direct launcher against a fake
app (exit 0 with a short count, no summary, a pre-ENFORCE-3 summary, a wrap, an override all come out INVALID),
mutation-tests the launchers (take the oracle out and the scenario that proved it changes), and scans `tools/` and
`.github/`: every script that can make the app play carries the oracle (acted on, not merely named) or is on a reasoned
exemption list, and a script that kills the app must pass the kill into the verdict. `app_play_scan.py` pins that
`m_sourceAdvance` and `m_playRequiredSourceFrames` are WRITTEN only at the reviewed sites, by function and count (the
three engine ticks, the first Play start, the gate's reset), and that `main.cpp` isolates the settings store before the
first `QSettings` and returns the autoplay verdict as the exit code; all of it is mutation-tested.

## ENFORCE-4: evidence is valid only when every receipt field is PRESENT and every verdict is CONSUMED

PR #217 (ENFORCE-3) was parked after its second key round for one root cause: its scans tested the PRESENCE of a gate,
not the CONSUMPTION of a verdict or the ABSENCE of a field. Three ways to report success on < 20 s of source footage got
through, each closed here and each pinned by a test that uses the TRUE legacy receipt shape (the fields a master-era
binary writes are omitted, never zeroed):

| Hole | Closed by | Pinned in |
|---|---|---|
| an absent `programmatic_play_admitted` read as "no Play happened", so a master-era profile receipt skipped the oracle and exited 0 | the profile reader never infers `play_performed`; `Get-GuiSmokeEvidencePlayVerdict -RequireAdmission` (the wrapper) fails an absent admission `RECEIPT_FIELD_ABSENT` and an admission of 0 `PLAY_NOT_ADMITTED`; both exit 43 | `test_playback_evidence_completeness.py` (`AbsentFieldIsInvalidOnTheProfileReceiptTests`) |
| `native_fps` / `pace_fps` / `fps_override` / `wrapped` / `wrap_count` absent read as "pace unchecked" / "no override" / "no wrap"; the runner only WARNED on absent wrap fields | `Get-GuiSmokeAbsentReceiptFields`: every field the oracle judges must be present or the receipt is `INVALID_SOURCE_FRAMES` / `INVALID_LOOPED` (`RECEIPT_FIELD_ABSENT`); the runner's `BACKSTOP_FIELDS_MISSING` warning is gone; the attribution job's embedded copy follows the same rule | one test per field in each reader (log summary, profile receipt, the job's copy, the runner's application) |
| `measure-wb-solve-distribution.ps1` ignored `capture-reference-frame.ps1`'s exit code, counted runs by `capture.err.txt`, published statistics from INVALID captures and pointed at a staged copy of the launcher | reads `$LASTEXITCODE` for every run; a non-zero exit (or no trace file) is an invalid run, excluded, counted in `invalidRuns`, and the script exits 43 (a missing build also fails); it uses the tracked launcher and names no footage (`-ClipPath` is the caller's) | `MeasureWbSolveDistributionTests` (executed against a fake launcher) |
| `lookassist-wb-multiclip-probe.ps1` only labelled a row; `lookassist-wb-determinism.ps1` published a verdict from the reps that happened to pass | the probe exits 43 after writing its labelled matrix; the determinism verdict is `INVALID` (exit 43) when any rep was rejected | `WbProbeConsumersFailTheScriptTests` (executed against a fake runner) |
| an autoplay closed mid-Play exited 0 (`m_automationVerdictExitCode` started at 0 and was set only by a poll timer) | `playback_frame_range::AutomationVerdictLatch`: armed FAILING (14) when `MLVAPP_AUTOPLAY_SECONDS` is requested, before anything can end the process, cleared only by `resolve( Reached )` after the engine consumed the window; `main.cpp` returns it as the exit code | `AutomationVerdictLatch.*` in `tests/console/test_playback_frame_range.cpp`; `AppVerdictLatchPinTests` |

### Every consumer of an evidence launcher acts on its exit code (scan)

`test_playback_evidence_completeness.py` derives every script under `tools/` that names an evidence launcher
(`run-release-gui-smoke.ps1`, `capture-reference-frame.ps1`, `validate-visible-playback.ps1`,
`run-release-playback-profile.ps1`) or a consumer that already acts on one (so a caller of a caller is found too). Each is
either **pinned** with the statement that reads and acts on the callee's exit code -- deleting that statement makes the scan
fail (mutation-tested, one mutation per consumer) -- or **exempt** with a written reason (a text mention, a refusal-only
self-test). A new consumer is neither, so it fails until a reviewer classifies it.

| Consumer | Acts on the exit code by |
|---|---|
| `tools/gates/compare-output-budget.ps1` | `$LASTEXITCODE -ne 0` after the runner: INDETERMINATE report, exit 5 |
| `lookassist-wb-determinism.ps1` | a rejected rep: the verdict is `INVALID`, exit 43 |
| `lookassist-wb-multiclip-probe.ps1` | a non-zero runner exit: row `RUN_INVALID`, script exit 43 |
| `measure-wb-solve-distribution.ps1` | a non-zero capture exit: run excluded, script exit 43 |
| `review-dualiso-fullres-recon.ps1` | `Assert-GuiSmokeChildEvidenceReady -ExitCode`: exit 2 |
| `run-non-dual-iso-guard-smoke.ps1` | a non-zero smoke exit is a failure; status `failed`, exit 2 |
| `run-release-cuda-playback-ab.ps1` | baseline / candidate exit codes are proof failures |
| `run-ultramagnus-p3-validation.ps1` | a non-zero smoke exit is a clip failure |
| `bachelor/playback-attr-3-cuda-job.ps1` | a non-zero runner exit is `SMOKE_RUN_FAILED` |
| `run-shipping-guard-smoke.ps1`, `run-local-cuda-playback-dng-smoke.ps1`, `invoke-ultramagnus-p3-evidence.ps1`, `export-release-cuda-dogfood-kit.ps1` | callers of the consumers above: each turns a child's exit code into its own status / exit |
| `dual-venue/Invoke-VenueLeg.ps1` (DUAL-VENUE-EVIDENCE-1 r2) | submits the attribution job through um-run and reads the job's exit code: a printed capture with a non-zero exit is `INVALID`; a PASS/FAIL receipt also needs the job oracle's verdict (`source_advanced`, `required_source_frames`, run nonce, wrap, clip id) or it is `INVALID`. Legs name a clip id, never a path; a fixture is refused by the length gate before anything is generated; a venue without an owner-typed record in `venue-clip-consent.json` refuses before submitting (`docs/dual-venue-evidence.md`) |

`app_play_scan.py` also pins every WRITE of the autoplay latch (one arm in the constructor, one fail, one resolve; the
retired `m_automationVerdictExitCode` int cannot return), of the safety budget `m_playRequestedSeconds` and of the engine
pace `m_playPaceFps` (only the two reviewed `programmaticPlay` sites, from the gate's own values); mutation-tested.

The attribution job no longer seeds the venue's registry (`reg add HKCU\Software\magiclantern.MLVApp ...`): an automation
run reads a run-scoped settings store, so the seeds were dead, and every value they wrote is the app's own default
(`AttributionJobRegistrySeedTests` pins that against the compiled defaults).

### ENFORCE-4 round 2: a receipt counts only if THIS invocation of the app wrote it, for THIS run

sol's round-1 key found the next member of the same family: `run-release-playback-profile.ps1` read `-Output` and judged
whatever it found, so a receipt a PREVIOUS run left behind (valid, every field present) was accepted when the app exited 0
without playing (`--exercise-play-action --help` returns 0 before it writes anything). The class is closed in every
launcher that judges a receipt, with three independent layers (each one alone rejects the repro; the tests remove each in
turn and a stale receipt is accepted only with all removed):

1. **Set aside.** A pre-existing receipt or output (`-Output`, `reference-frame.png` and its candidate manifest, last run's
   `cap-NNN.png`, the runner's result JSON) is renamed `STALE-<utc>-<name>` BEFORE the launch (`Move-GuiSmokeStaleReceiptAside`;
   never deleted, and a stale file that cannot be moved stops the launch with exit 43).
2. **Nonce.** The launcher generates `New-GuiSmokeRunNonce` (`n` + a GUID), hands it to the app as `MLVAPP_RUN_NONCE`
   (set after `-ExtraEnvironment`, so a caller cannot choose it), the app echoes it on every receipt it writes
   (`playback_smoke.summary` `run_nonce=`, the profile JSON `metadata.run_nonce`; unset or malformed is written as `none`,
   which no launcher generates), and the oracle accepts a receipt only when it carries the nonce THIS launch generated
   (`RECEIPT_NOT_THIS_RUN`, exit 43). A missing or mismatched nonce, a launcher that bound none, and an app that exited 0 and
   wrote no receipt are all INVALID. Both oracle copies (the gate's and the attribution job's embedded one, which compares
   against the nonce of the run log it read) carry the rule.
3. **Freshness.** A receipt FILE last written before the launch is INVALID (`Get-GuiSmokeReceiptStaleFailure`).

On any INVALID the rejected receipt is renamed `<name>.INVALID<ext>` (a JSON one is also stamped `"invalid": true` with
`invalid_reasons`), and a directory of captures gets an `INVALID.json` marker, so an offline reader cannot mistake it for
evidence; the runner's own result JSON carries the same stamp. `RunBindingScanTests` derives every launcher that starts the
app for an evidence Play and requires all three layers in each (a new launcher with the oracle but no binding fails).

Also closed in round 2: a Look Assist settle / toggle profile without Auto playback quality mode (the only mode in which its
warm-up Play is admitted) is refused up front, typed `PASS_THROUGH_REFUSED ... needs_auto_quality_mode_to_admit_a_play`
(exit 44), instead of ending in exit 43 after the whole run; a PRESENT `pace_fps` <= 0 is INVALID in both oracle copies;
every consumer pin now matches the failing action in the BODY of its exit check (`CONSUMER_FAILING_ACTION`: emptying a body
fails the scan, which the header-only pins of round 1 survived).

**The autoplay hook now exits observably.** The offscreen probe used to kill the two autoplay entries at its bound (exit -1,
latched code never observed). Qt 6's `qApp->quit()` sends the close event and `closeEvent` blocked on the "save the
session?" prompt; an automation run (the latch is sticky: `armed()`) never prompts any more, and the refusal exits with
`qApp->exit( 14 )`. The probe asserts exit 14 for both entries (observed live), plus that a profile receipt echoes the
launcher's nonce, and `none` when it is unset or malformed. Exit 0 is only for `Reached`, which needs a >= 20 s clip.

Disclosed, not closed (ENFORCE-4): the four historical binaries `measure-wb-solve-distribution.ps1` bisects across all
predate the receipt fields, so today every run of them is INVALID by design (the script is a bisect list, not
evidence, until they are rebuilt with the receipt). `compare-output-budget.ps1` still asks the runner for a 1 s window,
which the runner refuses (INDETERMINATE: fail closed; card OUTPUT-BUDGET-WINDOW-20S-1).

Disclosed, not closed: a **human** pressing Play in the interactive app is outside the class (it is not automation, and
its Play is never evidence). In drop-frame mode a tick after a GUI-thread stall advances the timeline by wall clock and
those unpresented frames count as consumed -- the owner's "source frames advanced" definition. No allowed automation run
has been observed consuming its frames (the tracked fixtures are 16 and 2 frames; no other clip may be opened here): that
is covered by the engine simulation, the pinned wiring and the executed oracle, and a venue sitting is the observation.
The Look Assist toggle exercise's recheck settle could not be run offscreen for the same reason; it is fixed by
construction (`lookAssistSettleNeedsOwnPlay`: the process admits one Play, so a second settle waits for the diagnostics
of the first instead of asking for another, which would be `REPLAY_REFUSED`) and unit-tested.

## Limits, stated plainly

- The tool-side gate trusts the header's frame count (and `fileCount` of a spanned set, which it checks is
  complete and one recording). A header that lies - or a truncated file - is caught at run time: the app's
  gate counts the frames it actually indexed (`getMlvFrames`), the app reports `total_frames`/`clip_seconds`,
  and the runner refuses a report under the floor. The app-side gate, not the header, is the authority.
- A play verb assembled through VARIABLES (not literals) escapes the tool scan; it does not escape the app.
- The job generator cannot open an owner clip, so for an owner id the gate runs at the venue, before launch.
- The headless `--profile-playback` **decode benchmark** (no play-capable option) presents no playback and is
  not gated; every Play-capable option is. The CI golden tests that used to Play the 2-frame fixture now assert
  the refusal (`tests/console/test_clip_golden.cpp`).
- The Auto-quality Look Assist warm-up is no longer a separate Play, so in Auto the measured pass includes the
  `WarmupHq` samples; contact sheets are seek-mode (`playback_path=false`) until a capture-during-the-measured-pass
  path exists. Both are the price of "no replay".
- No automation mode stops Play on a clock any more: each requests >= 20 s and waits for the engine's source-frame count (ENFORCE-3 below). The
  live offscreen proof (`test-app-play-gate-offscreen.ps1`) can only drive the refused paths, because the
  tracked fixtures are shorter than 20 s and no other clip may be created or opened; the allowed paths (each
  plays >= 20 s) are covered by the pure unit tests and the static pins, and need a venue sitting to observe.

## Allowlist of legitimate `--loop`

None. There is no tracked caller and no parameter that can produce it.
