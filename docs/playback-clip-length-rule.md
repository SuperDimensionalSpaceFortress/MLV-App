# Playback clip-length rule (venue playback never loops, never replays)

Status: **enforced in code, in the app and in the tools**. Owner rule of 2026-09-30
(PLAYBACK-CLIP-LENGTH-ENFORCE-1 closed the argument / mode / launcher class; ENFORCE-2 makes **the app itself the gate**).

> "it did not use a long 20-30 second clip. it looped a super short clip. I have brought this to your
> attention before and it keeps happening again. DURABLY FIX!"

## The rule

Any leg that **plays** the app - speed, pacing, smoothness, a LOOK / contact sheet, a smoke - on any venue
(Bachelor, Ultra-Magnus, CUDA or CPU) uses **at least 20 s of real footage**, over a play window that
**never exceeds the clip**, and **never loops or replays**.

The class this file pins: **no programmatic Play starts unless the window from the CURRENT position to the
receipt's cut-out is >= 20 s AND >= the requested window; the process admits ONE programmatic Play; Loop is
never enabled by automation; and every wrap, jump-to-first-frame and restart is counted and invalidates the run.**

The tracked fixtures (`tiny_dual_iso`, 2 frames; `large_dual_iso`, 16 frames, about 0.1-0.7 s) are for unit
tests and non-playing checks only. They are never played on a venue.

## THE APP IS THE GATE (ENFORCE-2)

Why ENFORCE-1 was parked: the tool-side gates read the clip's HEADER, but the app starts playback before any
range check. The GUI-smoke Look Assist warm-up forced Loop on and played the receipt's cut range (2-16 frames)
before the cut-range refusal ran, and a direct `MLVApp.exe --profile-playback --exercise-play-action` had no
app-side gate at all. Now `MainWindow::programmaticPlay(site, requestedSeconds)` is the ONLY place the app
triggers Play on its own behalf. It evaluates `playback_frame_range::evaluatePlayableWindow` (pure, unit-tested
in `tests/console/test_playback_frame_range.cpp`): the cut range is normalized exactly as the Play path does,
the window is measured from the live slider position to the cut-out, and it must be `>= max(20 s, requested)`.
A `ProgrammaticPlayLedger` then admits the first programmatic Play of the process and refuses every later one
(`REPLAY_REFUSED`: restart, re-Play, stress switch, contact-sheet replay). Refusals are typed, path-free, exit 14,
and happen **before Play** (zero presented frames). User input (`on_actionPlay_triggered`, the Loop menu) is
never gated.

| Programmatic entry (`platform/qt/MainWindow.cpp`) | Gated how |
|---|---|
| autoplay hook, `MLVAPP_AUTOPLAY_*` (constructor lambda, ~2826) | Loop forced off, `programmaticPlay("autoplay", seconds)`; `autoplay.refused reason=<typed>`; `MLVAPP_AUTOPLAY_LOOP` ignored |
| profile Look Assist settle (`--exercise-look-assist-settle/-toggle`, ~7865) | `programmaticPlay("profile-look-assist-settle", 12)`; **Loop is no longer enabled**; refusal returns exit 14 |
| profile `--exercise-play-action` (~8232) | `programmaticPlay("profile-exercise-play-action")`; exit 14 before Play |
| GUI-smoke Look Assist warm-up (was ~8961-8989) | **REMOVED.** No warm-up Play, no Loop: Look Assist warms during the measured pass |
| GUI-smoke measured Play (~9437) | `programmaticPlay("gui-smoke-measured", max(--seconds, presented-frames / fps))`; a `--presented-frames` target under 20 s of frames is `PLAY_WINDOW_TOO_SHORT` |
| clip-lifecycle stress (~9601, ~9673) | the Play is stopped at the switch; the **restart Play is REMOVED** (it would be a replay) |
| contact-sheet playback pass (was ~10322, up to 50 re-Plays) | **REMOVED.** The default pass refuses with `REPLAY_REFUSED`; `--contact-sheet-seek-mode` (no Play) is the only capture, and the attr-3 job now passes it |
| Play pressed on the last frame (`on_actionPlay_triggered`, ~23248) | counted by the wrap recorder (`jump_to_first_count`); the position-aware window already refuses a programmatic Play there |
| any Play start after the first (`on_actionPlay_toggled`, ~27635) | counted (`restart_count`); `wrap_count = engine wraps + jump-to-first + restarts`, any > 0 is `INVALID_LOOPED` |
| Loop at every automation entry | `forceLoopOffForAutomation` (only ever unchecks; Loop is not declared checked in `MainWindow.ui` and is not persisted) |

`tools/profiling/test-app-play-gate-offscreen.ps1 -Exe <MLVApp.exe>` launches the tracked short fixture through
each entry reachable offscreen and asserts the app refuses before Play. The static class test
(`test_playback_clip_length_gate.py`, `AppPlayGateStaticClassTests`) fails on any `actionPlay->trigger()` /
Loop-enable outside the allowlisted lines, on a programmatic Play site nobody reviewed, and on a gate that
triggers Play before it evaluated the window and admitted it; it is mutation-tested.

## Where else it is enforced

| Layer | What | Refusal |
|---|---|---|
| `tools/profiling/gui-smoke-length-gate.ps1` | Reads the 52-byte `MLVI` header: frame count, `fps = nom/denom`; spanned sets are summed and must be complete. The **play window** (and a `-TargetPresentedFrames` early stop) must itself be >= 20 s (`-ClipOnly` is the opt-out for callers that play nothing) | `CLIP_TOO_SHORT`, `PLAY_WINDOW_TOO_SHORT`, `CLIP_LENGTH_UNKNOWN` |
| `run-release-gui-smoke.ps1` (choke point) | Gate first; `--loop` never produced, `-AllowLoop` gone; pass-through (`-AdditionalArgs`, `-ExtraEnvironment`) and an **inherited** `MLVAPP_AUTOPLAY_*` refused; the verdict is applied after the run | exit 41 / 42 / 44 / 43 |
| `run-release-playback-profile.ps1`, `validate-visible-playback.ps1`, `capture-reference-frame.ps1`, `start-release-cuda-playback.ps1` | Same gate and the inherited-autoplay refusal; `capture-reference-frame.ps1` defaults to a pinned frame at `ceil(20 * fps)` presented frames | exit 41 / 42 / 44 |
| export launchers (`run-release-cdng-export-profile.ps1`, `run-release-cuda-dng-export.ps1`) | refuse every play-capable pass-through option | exit 44 |
| `bachelor/playback-attr-3-cuda-job.ps1` | `-PlaySeconds` (floor 20); fixture ids refused at generation; the job summary carries the typed `smokeRefusalReason` | `PLAYBACK_ATTR3_...` |
| runtime backstop | `PlaybackWrapRecorder`; `playback_smoke.summary/.gate` carry `wrapped`, `wrap_count`, `jump_to_first_count`, `restart_count`; the runner's `Get-GuiSmokeLoopVerdict` (executed and mutation-tested, including the runner's application of it) | `INVALID_LOOPED`, exit 43 |
| `test_playback_clip_length_gate.py` scan | fails if any script under `tools/` / `.github/` names a play token (also **composed**: `'a' + 'b'`, `-f`, `-join`, backticks) or launches the exe forwarding caller arguments, without the gate or a commented allowlist entry | CI red |

Typed verdicts never name the clip path.

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
- `--presented-frames` runs and `--exercise-play-action` stop early by design; the rule is on the window the
  Play MAY cover (>= 20 s from the position), and for `--presented-frames` also on the frames requested.

## Allowlist of legitimate `--loop`

None. There is no tracked caller and no parameter that can produce it.
