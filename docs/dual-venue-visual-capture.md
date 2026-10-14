# Window captures for every speed leg (PLAYBACK-VISUAL-INSPECTION-1)

Owner, 2026-10-13: *"you should be inspecting how each run looks. I thought that was being done via screnshots"*. Until this
card a LOOK leg kept a contact sheet and a SPEED leg (every playback perf leg) kept no image at all, so a visible regression had no
artefact to answer it. Now every speed leg keeps window captures, the receipt judges them, and a reviewer opens them.

Two measured traps shape the design (`.claude-state` memory `playback_visible_validation`, `playback_fps_measurement_reliability`):
a capture taken DURING a timed window roughly doubles render cost and contaminates the fps numbers; and every cadence metric can
read perfect while the display is frozen. So the capture runs after the measured window, and its gate is about the picture.

## What runs

1. **App** (`--visual-capture-dir <dir>`, off by default; `MainWindow::runGuiPlaybackSmoke`). Only after the measured Play has
   REACHED its own stop condition: `finishPlaybackSmokeTelemetry("visual-capture")` closes the measured session (the
   `playback_smoke.summary` / `gate` lines, and every oracle number, are written there), while the SAME Play keeps running -- no
   new Play, no restart, no loop (the clip-length gate's stop-site pin counts this one pause). Then, untimed:
   * 6 captures evenly spaced across a ~3 s tail (the centres of six 500 ms slices);
   * 1 capture the instant Play is paused, before any event is processed (still the playback-path frame): `stop.png`;
   * 1 capture after the paused full-quality path re-renders that same frame (`seekAndSettleLoadedClip`): `paused.png`.

   Each capture is the WHOLE window via `PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)` -- the compositor's view of the window,
   GL child windows included, never an in-app readback -- saved at most 1920 wide after Play has stopped, with the video
   viewport rectangle (in the saved image's pixels) and the frame the app says it showed in `visual.json`. A capture failure is
   recorded there and never changes the run's exit code.
2. **Job** (`playback-attr-3-cuda-job.ps1 -VisualCapture`, three bracketed `PLAYBACK-VISUAL-INSPECTION-1` regions; the default job
   is unchanged): the staged binary's smoke help is probed for `--visual-capture-dir` before PresentMon starts (an older build is
   never handed the option; the request record says why not), the option joins the smoke runner's one `-AdditionalArgs`, the
   PresentMon display report's window ENDS where the tail began (`tail_begin_utc`; the cut only ever shortens the window), and the
   captures publish under `visual\`.
3. **Runner** (`Invoke-VenueLeg.ps1`, `Read-DvVisualCaptures`): copies them into the leg's evidence as
   `visual/<legId>-play-<k>.png`, `visual/<legId>-stop.png`, `visual/<legId>-paused.png` (plus `app-visual.json`, `request.json`),
   hashes every file, derives the numbers below from the copies, composes `visual/<legId>-strip.png` (the six play frames, then
   the paused-instant and paused full-quality frames, each tile the video viewport, labelled with the frame the app showed) and
   copies it next to the LOOK sheets: `-SheetCopyDir`, else `.claude-state\dual-venue\sheets\strip-<legId>-<venue>-<backend>-<sha8>-<stamp>.png`.
   The chain log line is `DVE_VISUAL=<verdict> STRIP=<path>`. Owner footage: everything stays under `.claude-state`.

## The receipt's `visual` block and the gate

Numbers are computed from the VIDEO VIEWPORT only -- the window's timecode and fps chrome change over a frozen picture too:

* `distinctContentHashes`: distinct sha256 values of the play frames' viewports, downscaled to 64x36 (a frozen display has 1);
* `appDistinctDisplayFrames`: distinct frames the app says it presented across those captures;
* `playVsPausedMeanAbsDiff`: the largest per-channel mean absolute difference (0..255, viewports at 160x90) between the
  paused-instant frame and its paused full-quality re-render, judged only when both show the same timeline position;
  `playVsPausedMeanAbsDiffPerChannel` keeps R, G, B.

| verdict | when | counted |
|---|---|---|
| `FROZEN_DISPLAY` | the play frames show ONE picture while the app presented more than one frame across them | **no**: the receipt is `INVALID` |
| `VISUAL_DIVERGENCE` | the playback frame is further than the threshold from its paused full-quality re-render | yes, and the detail says so |
| `CAPTURE_BLANK` | every play frame's viewport is flat (std < 1): the capture judges nothing | yes |
| `NOT_CAPTURED` / `NOT_REQUESTED` | nothing published (an old build, a failed run) / the spec opts out | yes |
| `OK` | none of the above | yes |

The divergence threshold (`$script:VisualDivergenceThreshold`, DualVenueRunner.psm1) is calibrated on a known-good leg: 6.0. The
known-good Bachelor leg (M16-1243 windowed-s2, CPU, build `adf6185a`, receipt `a5d3ffbe`) measured 1.099 on its worst channel;
three times that (3.3) is under the 6.0 floor, so the floor is the threshold.

## Opting out

Absent means on for every speed leg. A spec opts out with `"visualCapture": false` AND a `"visualCaptureOptOutReason"` (at least
12 characters, recorded in the receipt); either without the other is refused (schema and `Get-DvVisualCaptureRequest`). A look
leg never runs this capture (its contact sheet is the look; `visual` is null).

## Review rule

Before approving a playback PR, the review keys AND the hub OPEN (Read) the strip of at least one leg per arm and state in one line
what they saw (`VISUAL <arm>: <strip> -- <what was seen>`). Reading the receipt's numbers is not opening the strip; an arm without
a strip is a finding. The rule is enforced, not merely proposed: the hub's key prompt composer (`hub-tools\new-review-keys.ps1`
plus `key-prompt-visual.md`, installed 2026-10-14; board-only, untracked tooling) adds a `## VISUAL INSPECTION` block and a
`visual_inspection` field to every key prompt for a PR touching `platform/qt/`, `src/` or `tools/profiling/`.
