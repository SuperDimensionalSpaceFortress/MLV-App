# Changelog

Notable project changes will be recorded here. This file starts with current
unreleased work and does not reconstruct release history that was not already
maintained in this format.

## [Unreleased]

### Added

- Fork README guidance for headless `--batch` export, separate `--trim-mlv`
  trimming, `.marxml` settings supplied through `--receipt`, and the Windows
  Qt 6.10.2 / MinGW 13.1 toolchain.
- Contributor and support routes for the protected-branch workflow, product
  evidence, dependency locks, release payload readiness, and private security
  reporting.
- A semantic repository-hygiene check that keeps those routes synchronized with
  their source policies.

### Changed

- Play starts faster: the wait from Play to the first shown frame drops from
  about 0.9 s to about 0.15 s on the CUDA playback route
  (PLAYBACK-STARTUP-WAIT-PREWARM-1).
  - The audio device now runs on its own thread. While paused on a clip with
    audio (audio output and drop-frame mode on), MLV App keeps the output
    stream open and running on silence, so Play never waits for the device.
    The audio is then started where the video will be when it becomes
    audible, so a slow device start delays the sound but never puts it out
    of sync.
  - A held stream that sees no Play for 60 s is suspended (an active audio
    stream can keep the computer awake). Pausing keeps the stream open:
    the sound stops after what is already queued (about 0.2 s), as before.
  - A changed default audio device, or a stream error, while paused re-opens
    the stream on the new device at the next Play.
  - Quitting no longer waits forever on a hung audio device. It waits up to
    5 s, logs a warning, and leaves the audio thread behind.
  - While paused after a clip loads, the CUDA texture playback route is warmed
    once (shader program, one debayer run on scratch textures). This is
    skipped when the window is already presenting that route at the same
    size.
  - `MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY` (default off) restores the old
    synchronous audio start. `MLVAPP_PLAYBACK_ROUTE_PREWARM` (default on)
    turns the warm off. Both take exactly `1`, `true`, `on` or `yes`, and
    `0`, `false`, `off` or `no`, in any case. Any other value keeps the
    default and is logged once.
- Play at full resolution (scale 1) on the CUDA playback route starts sooner
  (PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-SETUP-1).
  - The paused warm now keeps the two textures it warmed, still registered
    with CUDA, and the first frame of Play uses them. Before, the first frame
    allocated and registered a new pair, which took about 100-200 ms at full
    resolution. The kept pair is used only when the size matches and the
    window is still showing the paused picture; otherwise it is freed and
    the first frame allocates as before. A clip change, a window/fullscreen
    change or a lost GL context frees it.
  - The CUDA recon library is loaded while paused, not on the first frame
    of Play.
  - `MLVAPP_PLAYBACK_ROUTE_PREWARM_STAGE` (default on) turns off keeping the
    textures, and `MLVAPP_PLAYBACK_RECON_BACKEND_PRELOAD` (default on) turns
    off the early load. Both take the same values as
    `MLVAPP_PLAYBACK_ROUTE_PREWARM`, which still turns all of it off.
  - Playback smoke logs a `playback_smoke.first_present_split` line saying
    where the first frame's time went.
