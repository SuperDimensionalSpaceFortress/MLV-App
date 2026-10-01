# Playback clip-length rule (venue playback never loops)

Status: **enforced in code**, owner rule of 2026-09-30 (PLAYBACK-CLIP-LENGTH-ENFORCE-1, round 2 completes the class).

> "it did not use a long 20-30 second clip. it looped a super short clip. I have brought this to your
> attention before and it keeps happening again. DURABLY FIX!"

## The rule

Any leg that **plays** the app - speed, pacing, smoothness, a LOOK / contact sheet captured during
playback, a smoke - on any venue (Bachelor, Ultra-Magnus, CUDA or CPU) uses **at least 20 s of real
footage**, over a play window that **never exceeds the clip**, and **never loops**.

The class this file pins: **no code path can make a venue PLAY a clip under 20 s or LOOP, whatever
arguments are passed, and every wrap that happens is detected.**

The tracked fixtures (`tiny_dual_iso`, 2 frames; `large_dual_iso`, 16 frames, both about 0.1-0.7 s) are
for unit tests and non-playing checks only. They are never played on a venue.

The 2026-06-26 rule ("loop short clips so they fill `-Seconds`") is **superseded**, and `-AllowLoop`
(round 1's only-with-`-LaunchOnlyProbe` exemption) is **removed**: no evidence run can ever loop.

## Where it is enforced

| Layer | What | Refusal |
|---|---|---|
| `tools/profiling/gui-smoke-length-gate.ps1` | Reads the 52-byte `MLVI` header (`mlv_file_hdr_t`, `src/mlv/mlv.h`): frame count, `fps = nom/denom`; spanned sets are summed and must be complete | `CLIP_TOO_SHORT (clip=<s> window=<s>)`, `CLIP_LENGTH_UNKNOWN (reason=<token>)` |
| `tools/profiling/run-release-gui-smoke.ps1` (the choke point) | Applies the gate before anything is hashed or launched, and also to a lifecycle-stress switch clip. The runner never produces `--loop`; `-AllowLoop` no longer exists (an unrecognised parameter is an error, not a silent positional) | exit **41** too short, exit **42** unknown |
| the same runner, pass-through | `-AdditionalArgs` and `-ExtraEnvironment` go through `Test-GuiSmokePassThroughArguments` / `Test-GuiSmokeEnvironmentEntries` first: `--loop` in any spelling (`-loop`, `/loop`, `--LOOP`, `--loop=1`, several tokens in one element), any clip or play-window control (`--input`, `--seconds`, `--start-frame`, `--presented-frames`), any other playback mode, the lifecycle-stress options and `MLVAPP_AUTOPLAY_*` are refused; an option the policy has never classified is refused too (fail closed) | `PASS_THROUGH_REFUSED (option=<name> reason=<token>)`, exit **44** |
| the option class | `$GuiSmokeOptionPolicy` / `$PlaybackProfileOptionPolicy` classify **every** option `platform/qt/main.cpp` declares for the two playback parsers; `test_playback_clip_length_gate.py` compares them with the real declarations, so a NEW app option fails CI until it is classified, and pins the refused set | CI red |
| a launch-only probe | `-LaunchOnlyProbe` passes `--launch-only`: the app opens the clip and returns without ever calling Play (before the Look Assist warm-up too). The runner fails the run if any playback session was logged or a frame presented | `LAUNCH_ONLY_PROBE_PLAYED` |
| `platform/qt/main.cpp` / `MainWindow.cpp` | `--loop` is refused by the app itself (exit 2); the app's own gate refuses a clip under 20 s or shorter than the window from `--start-frame`, and a short or spanned stress-switch clip (exit 14), so a launch that bypassed the runner is covered; Loop is forced OFF before the measured Play, so a persisted GUI Loop setting cannot loop it; the `MLVAPP_AUTOPLAY_*` hook ignores `MLVAPP_AUTOPLAY_LOOP` and refuses a short clip | exit 2 / 14 |
| runtime wrap detection | `playback_frame_range::PlaybackWrapRecorder` is incremented in the engine's own wrap branches in `playbackHandling` (the Loop jump back to cut-in, and `advanceDropFrameTick`'s `wrapped`), process-cumulative so a warm-up wrap counts. The old presented-frame heuristic only adds a second signal (it misses a wrap after dropped frames: 700 -> 0 over 0..719). `playback_smoke.summary` / `.gate` carry `wrapped`, `wrap_count`, `total_frames`, `clip_seconds` | - |
| the runner, after the run | `Get-GuiSmokeLoopVerdict` (the decision, in the gate file, run against the app's real summary format and mutation-tested): `wrapped=1`, `wrap_count>0`, or a reported clip under the floor is never a PASS | `INVALID_LOOPED`, exit **43** |
| `tools/profiling/run-release-playback-profile.ps1` | Can PLAY (`--exercise-play-action`, and the Look Assist settle's warm-up play via `--exercise-look-assist-toggle/-settle`), so those options are allowed only after the 20 s floor; loop / mode / clip changes and `MLVAPP_AUTOPLAY_*` are refused. A pure decode benchmark (no play-capable option) presents no playback and is not gated | exit 41 / 42 / 44 |
| `run-release-cdng-export-profile.ps1`, `run-release-cuda-dng-export.ps1`, `start-release-cuda-playback.ps1` | Forward `-AdditionalArgs` to the exe, so every play-capable or mode-changing option is refused (`-Context 'launcher'`) | exit 44 |
| `tools/profiling/bachelor/playback-attr-3-cuda-job.ps1` | `-PlaySeconds` (default 25, floor 20) replaces the hard-coded `-Seconds 40`; a fixture id is refused at generation | `PLAYBACK_ATTR3_CLIP_TOO_SHORT (...)` |
| `validate-visible-playback.ps1`, `capture-reference-frame.ps1` | Launch the app directly, so they run the same gate and no longer pass `--loop` | exit 41 / 42 |
| `tools/repo_hygiene/test_playback_clip_length_gate.py` | Class test, including a scan of `tools/**` that fails if any script names a play token (`--gui-smoke-playback`, `--profile-playback`, `--exercise-play-action`, `--exercise-look-assist-*`, `MLVAPP_AUTOPLAY_*`) or launches the exe while forwarding caller arguments, without the gate or an explicit, commented allowlist entry (the decode-only entries are checked to really have no pass-through and no play option) | CI red |

Typed verdicts never name the clip path (owner footage is never named in a message).

## Limits, stated plainly

- The gate trusts the header's frame count. A header that lies is caught at run time: the app reports
  its own `total_frames`/`clip_seconds`, and the runner refuses a report under the floor.
- The job generator cannot open an owner clip (and the consent resolver records no frame count), so for
  an owner id the gate runs at the venue, inside the emitted job's runner call, before launch.
- The headless `--profile-playback` code path itself (CI golden tests drive `--exercise-play-action` on the
  tracked fixtures from `tests/console/test_clip_golden.cpp`) keeps no app-side length gate: that is a
  unit-test use, not a venue, and the venue-facing launchers above are gated. The Look Assist warm-up
  play inside `runGuiPlaybackSmoke` still forces Loop on for up to 15 s on a clip the gate guarantees is
  at least 20 s; if it ever wraps, the process-cumulative recorder reports it and the run is INVALID_LOOPED.
- A binary that predates the summary fields reports none of them: not a wrap (the pre-launch gate is
  its control).

## Allowlist of legitimate `--loop`

None. There is no tracked caller and no parameter that can produce it.
