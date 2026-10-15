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

- High frame rate clips now conform to 24 fps playback by default: a clip
  above 30 fps plays as slow motion at 24 fps, so every recorded frame is
  shown. Configure it under **Playback → High Frame Rate Conform** (enable,
  conform to 24/25/30 fps, for clips above 30/50 fps). Export, metadata and
  timecode keep the clip's own rate. The environment variable
  `MLVAPP_PLAYBACK_CONFORM=0|1` overrides the setting for one process (the
  native-HFR measurement arm) and is never saved.
- The Export Settings frame rate override now changes the export rate only;
  interactive playback paces at the clip's own (or conformed) rate.
- The release GUI smoke's pre-launch clip-length gate assumes the default
  conform settings (on, 24 fps, clips above 30 fps); a different conform target
  saved in the interactive settings store is not read by that gate.
