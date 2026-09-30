# Playback clip-length rule (venue playback never loops)

Status: **enforced in code**, owner rule of 2026-09-30 (PLAYBACK-CLIP-LENGTH-ENFORCE-1).

> "it did not use a long 20-30 second clip. it looped a super short clip. I have brought this to your
> attention before and it keeps happening again. DURABLY FIX!"

## The rule

Any leg that **plays** the app - speed, pacing, smoothness, a LOOK / contact sheet captured during
playback, a smoke - on any venue (Bachelor, Ultra-Magnus, CUDA or CPU) uses **at least 20 s of real
footage**, over a play window that **never exceeds the clip**, and **never loops**.

The tracked fixtures (`tiny_dual_iso`, 2 frames; `large_dual_iso`, 16 frames, both about 0.1-0.7 s) are
for unit tests and non-playing checks only. They are never played on a venue.

The 2026-06-26 rule ("loop short clips so they fill `-Seconds`") is **superseded**. The 2026-09-22
edition of this rule lived only in prose and its enforcement card was never built; that is why it
kept recurring.

## Where it is enforced

| Layer | What | Refusal |
|---|---|---|
| `tools/profiling/gui-smoke-length-gate.ps1` | Reads the 52-byte `MLVI` header (`mlv_file_hdr_t`, `src/mlv/mlv.h`): frame count, `fps = nom/denom`; spanned sets are summed and must be complete | `CLIP_TOO_SHORT (clip=<s> window=<s>)`, `CLIP_LENGTH_UNKNOWN (reason=<token>)` |
| `tools/profiling/run-release-gui-smoke.ps1` (the choke point) | Applies the gate before anything is hashed or launched. `--loop` is **not** in the default argument list | exit **41** too short, exit **42** unknown |
| the same runner, `-AllowLoop` | The only way to pass `--loop`; refused unless `-LaunchOnlyProbe` (which can never be playback evidence). `-NoLoop` still binds and is a no-op | `-AllowLoop is refused` |
| `platform/qt/MainWindow.cpp` | Prints `wrapped=<0\|1> total_frames=<n> clip_seconds=<s>` on `playback_smoke.summary` and `playback_smoke.gate` | - |
| the runner, after the run | `wrapped=1`, or a reported clip length under the floor, is never a PASS | `INVALID_LOOPED`, exit **43** |
| `tools/profiling/bachelor/playback-attr-3-cuda-job.ps1` | `-PlaySeconds` (default 25, floor 20) replaces the hard-coded `-Seconds 40`; a fixture id is refused at generation | `PLAYBACK_ATTR3_CLIP_TOO_SHORT (...)` |
| `validate-visible-playback.ps1`, `capture-reference-frame.ps1` | Launch the app directly, so they run the same gate and no longer pass `--loop` | exit 41 / 42 |
| `tools/repo_hygiene/test_playback_clip_length_gate.py` | Class test, including a scan of `tools/**` that fails if any script launches `--gui-smoke-playback` without the gate or an explicit, commented allowlist entry | CI red |

Typed verdicts never name the clip path (owner footage is never named in a message).

## Limits, stated plainly

- The gate trusts the header's frame count. A header that lies is caught at run time: the app reports
  its own `total_frames`/`clip_seconds`, and the runner refuses a report under the floor.
- The job generator cannot open an owner clip (and the consent resolver records no frame count), so for
  an owner id the gate runs at the venue, inside the emitted job's runner call, before launch.
- `--profile-playback` (a decode benchmark over N frames, not a played window) is outside this gate.

## Allowlist of legitimate `--loop`

None tracked. `-AllowLoop` with `-LaunchOnlyProbe` is the only permitted combination and exists for a
launch probe that produces no playback evidence.
