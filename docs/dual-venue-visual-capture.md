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
   * 6 forced, bound play captures, `play-<k>.png`, across the FIRST half of a ~3 s tail (the centres of six equal slices of it);
   * then 6 unforced play captures, `play-unbound-<k>.png`, across the SECOND half (the centres of six equal slices of it): what the
     display itself shows, and what the frozen check judges;
   * 2 captures the instant Play is paused, before any event is processed (still the playback-path frame): first unforced,
     `stop-unbound.png` (what a build before PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1 captured), then bound, `stop.png`;
   * 2 captures after the paused full-quality path re-renders that same frame (`seekAndSettleLoadedClip`): unforced
     `paused-unbound.png`, then bound `paused.png`.

   Why the unforced captures exist (PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2, Dot `6d0e710f` B2): the forced paint draws the frame the
   label names even when the display's own deferred paint never lands, so a forced capture alone can never show a frozen display. Why
   they come after all the forced ones (r3, Dot `7dc103f1` B2): a forced paint stays on screen when the display's own paint is stuck,
   so it is what the NEXT unforced capture shows. r2 took an unforced capture then a forced one in each slice, and over a frozen
   display unforced k+1 showed forced k's paint: six moving pictures. Now no forced paint falls between two unforced play captures, or
   between the last of them and `stop-unbound.png`. Every capture records `forced_paints_before` (how many forced paints preceded it),
   and the runner judges only the unforced play captures that record the same count as the first of them. Forced `play-<k>` and
   unforced `play-unbound-<k>` are therefore about half a tail apart: they are NOT twins of one instant, and on a healthy display they
   show different frames.

   Each capture is the WHOLE window via `PrintWindow(hwnd, dc, PW_RENDERFULLCONTENT)` -- the compositor's view of the window,
   GL child windows included, never an in-app readback -- saved at most 1920 wide after Play has stopped, with the video
   viewport rectangle (in the saved image's pixels) and the frame the app says it showed in `visual.json`. A capture failure is
   recorded there and never changes the run's exit code.

   **Binding (PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1).** PrintWindow reads the LAST PAINTED surface, but the CPU route paints a
   presented frame later (the pixmap item's or GL viewport's `update()`), while its label (`display_frame`, `serial`) is stamped
   at once -- so a capture could show frame N-1 labelled N. CUDA paints and swaps before the label is stamped. Every capture
   except the `*-unbound` kinds therefore paints the pending frame synchronously first (`paintPresentedFrameForCapture`: the GL
   window's paintGL plus ONE swap through the window's single manual swap site, `swapOutsidePaintEventAndRecord`, which the
   swap recorder sees; else the viewport's `repaint()`), then reads the label, then prints the window, with no event handled between.
   Each entry carries `painted_serial` (the serial of the frame painted: the GL window's / GL viewport's own record, or on the
   QGraphicsPixmapItem route -- the Bachelor CPU legs -- the serial the item holds once `repaint()` drew it),
   `painted_serial_valid`, `label_bound` (= valid and `painted_serial == serial`), and `label_bound_basis`: `gl_window_paint` /
   `gl_viewport_paint` (the paintGL() that drew the frame recorded its serial), `pixmap_item_by_construction` (the pixmap route: the
   item's serial is the same request serial the label is stamped from, so `label_bound` holds by construction there and proves only
   that the forced repaint ran), or `none`. The schema stays `mlv-app/visual-capture/v1`; the fields are additive.
2. **Job** (`playback-attr-3-cuda-job.ps1 -VisualCapture`, three bracketed `PLAYBACK-VISUAL-INSPECTION-1` regions; the default job
   is unchanged): the staged binary's smoke help is probed for `--visual-capture-dir` before PresentMon starts (an older build is
   never handed the option; the request record says why not), the option joins the smoke runner's one `-AdditionalArgs`, the
   PresentMon display report's window ENDS where the tail began (`tail_begin_utc`; the cut only ever shortens the window), and the
   captures publish under `visual\`.
3. **Runner** (`Invoke-VenueLeg.ps1`, `Read-DvVisualCaptures`): copies them into the leg's evidence as
   `visual/<legId>-play-<k>.png`, `visual/<legId>-stop-unbound.png` (when the build keeps it), `visual/<legId>-stop.png`,
   `visual/<legId>-paused.png`, and `visual/<legId>-play-unbound-<k>.png` / `visual/<legId>-paused-unbound.png` (when the build
   pairs them) (plus `app-visual.json`, `request.json`),
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
* `labelBound`: the stop capture's `label_bound`; `null` for a build that predates the binding; `pausedLabelBound`: the paused
  capture's; `labelBoundBasis` / `pausedLabelBoundBasis`: their `label_bound_basis`;
* `stopUnboundVsPausedMeanAbsDiff` / `stopUnboundVsStopMeanAbsDiff`: the same difference for `stop-unbound.png` against the
  paused re-render and against the bound stop (the strip adds its tile, labelled "unforced"); `pausedUnboundVsPausedMeanAbsDiff`
  for `paused-unbound.png` against `paused.png`;
* `frozenJudgedOn`: `play_unbound` when the app published unforced play captures (the play-frame numbers above are then computed
  from THEM: the frozen check never judges a force-painted set), else `play`. Beside it, `forcedPlayDistinctContentHashes`; a
  forced set that moved over an unforced one that did not is said in the reason ("the force-paint would have masked this"). (r5:
  no forced-k vs unforced-k pairing is reported; under the r3 order such a pair compares two moments half a tail apart.)
* `observerPaintedPlayFrames`: unforced play captures not judged because they record another `forced_paints_before` than the
  first (or none: an r2 build); `stopUnboundObserverClean`: whether `stop-unbound.png` also follows them with no forced paint;
* `repeatedPlayFrames`: judged unforced captures that show the same picture as the one before them while the app's label
  advanced (`null` over source frames the app records as identical); `repeatedPlayFramesNote` says so in words, and is set
  whatever note `reason` already holds (`reason` keeps the first note).

| verdict | when | counted |
|---|---|---|
| `FROZEN_DISPLAY` | the play frames show ONE picture while the app presented more than one frame across them AND the source frames are known to differ (`sourceDifference` `known-differ`) | **no**: the receipt is `INVALID` |
| `FROZEN_DISPLAY_SUSPECTED` | the same one picture, but nothing records whether the source frames differ (`sourceDifference` `unknown`): a static clip shows one picture legitimately | yes, and the detail says so |
| `PARTIAL_FREEZE` | more than one picture, but `repeatedPlayFrames` is 2 or more AND the source frames are known to differ: the display's own paint stopped landing for part of the tail. One repeat is `OK`; over an `unknown` source the repeats stay a note (`repeatedPlayFramesNote`) on an `OK` block | yes, and the detail says so; a self-heal pre-launch with it is `INVALID` |
| `VISUAL_ASSESSMENT_UNAVAILABLE` | play frames have no usable viewport rectangle in `visual.json` (absent, empty or outside the image) AND fewer than two play frames with one remain: the whole window, with its timecode and chrome, is never judged in its place. With two or more usable frames those are judged as above (a known-differ leg with one frame lacking a rectangle is still `FROZEN_DISPLAY`) | yes, and the detail says so |
| `CAPTURE_UNBOUND` | a capture the divergence compares is not shown bound to its label: on a binding build (either side carries `label_bound`), the stop OR the paused capture's `label_bound` is false or missing; the stop-vs-paused difference then compares two moments | yes, and the detail says so |
| `VISUAL_DIVERGENCE` | the playback frame is further than the threshold from its paused full-quality re-render | yes, and the detail says so |
| `CAPTURE_BLANK` | every play frame's viewport is flat (std < 1): the capture judges nothing | yes |
| `NOT_CAPTURED` / `NOT_REQUESTED` | nothing published (an old build, a failed run) / the spec opts out | yes |
| `OK` | none of the above | yes |

`sourceDifference` is derived from the app's own per-frame records over the SAME frames the picture is judged over (the play
frames with a content hash): `Get-DvVisualSourceDifference` applies the app's rule (next section) to those frames' `source_id`, so
`known-differ` means the judged frames themselves show differing source ids. The app's top-level `source_difference` covers EVERY
play capture, a usable picture or not, so it is kept as written (`appSourceDifference`) and only cross-checks the decision
(`sourceDifferenceCrossCheck`: `agree` or `disagree`; a disagreement also adds a note to the reason, and never decides).
PLAYBACK-SOURCE-FRAME-IDENTITY-1 r4 (acting-cos Dot B1 on PR #477): five frozen frames over one source beside an unusable sixth over
another read `differ` in the app's record, but the judged frames are a static clip. The app's words map `differ` -> `known-differ`,
`identical` -> `known-identical`; absent or anything else (the wrong case included: `IDENTICAL`) is `unknown`, never a guess. One picture over
`known-identical` source frames is a static clip, not frozen (`OK`, with the reason "one picture; the app records the source frames as
identical: a static clip", so a wrong `identical` from a future build shows in the receipt). `playFrames` counts the judged play
frames (those with a viewport); `savedPlayFrames` the play PNGs saved; `captured` is true when any was saved. The app side is
tracked as `PLAYBACK-SOURCE-FRAME-IDENTITY-1`. No app build writes the field yet, so until one does a single picture reads
`FROZEN_DISPLAY_SUSPECTED`. A self-heal pre-launch still needs `OK`, so either new verdict there makes the leg `INVALID`.

The divergence threshold (`$script:VisualDivergenceThreshold`, DualVenueRunner.psm1) is calibrated on a known-good leg: 6.0. The
known-good Bachelor leg (M16-1243 windowed-s2, CPU, build `adf6185a`, receipt `a5d3ffbe`) measured 1.099 on its worst channel;
three times that (3.3) is under the 6.0 floor, so the floor is the threshold.

## Source-frame identity: `source_id` and `source_difference` (PLAYBACK-SOURCE-FRAME-IDENTITY-1)

One picture across the play frames is FROZEN only if the SOURCE frames behind them differ; a static clip shows one picture
too. Display-side hashes cannot tell the two apart (a frozen display freezes them), so the app records the source itself. After
Play has stopped (step (4) of the capture block, never in the timed window or the tail), for each distinct `display_frame` of
the play captures it reads the bytes the decoder reads for that frame -- the encoded VIDF payload at
`video_index[frame].frame_offset`: `frame_size` bytes for LJ92 and JPEG2K, the packed RAW size otherwise -- under the file's
own mutex, without decoding, and hashes them with FNV-1a 64 (`getMlvFrameSourceHash64`, `src/mlv/video_mlv.c`). For LJ92 it
hashes only the fields liblj92 reads (`mlvLj92FrameSourceHash64`): the last DHT's table, the last SOF3's precision and size, the
SOS component count and predictor, then every byte from the end of the SOS header to the end of the payload. COM, APPn and every
other skipped segment, header bytes the decoder ignores and filler between markers are left out, so those never make two frames
"known to differ". The tail is never cut at an EOI, because liblj92 steps over an `FF D9` inside the entropy data. "known
identical" is therefore never false. Unread bytes after the scan can still read "differ", the loud direction: a FROZEN_DISPLAY
someone looks at, never a frozen display waived as a static clip.

* `frames[].source_id` (play entries only, both the forced `play` and the unforced `play_unbound` captures -- the frozen check is
  judged on the unforced ones whenever they exist): that hash as 16 lowercase hex digits. Absent when it could not be read.
* `source_difference` (top level), a JSON string, exactly one of:
  * `"differ"`: at least two play frames' source ids differ;
  * `"identical"`: every play frame has a source id, they span at least two distinct `display_frame` values, and all are equal;
  * absent otherwise: fewer than two play frames, any frame without a source id (an unreadable frame, a frame index of -1),
    or one repeated `display_frame` (a stuck playback clock is not evidence of a static clip).

The runner decides from `frames[].source_id` of the judged play frames (exactly 16 lowercase hex digits; anything else is no id)
by the same rule, and reads the top-level field case-sensitively, accepting only those two strings, as a cross-check; anything
else is unknown. `"identical"` waives the
frozen gate, which is why it is written only on that positive evidence. Scope: MLV containers only; an MCRAW or CinemaDNG-folder
clip has no source id and the field is omitted. The policy is `platform/qt/VisualCaptureSourceDifference.h`; the
`gui_smoke.visual_capture` log line ends with `source_difference=<differ|identical|omitted>`.
**A CPU `VISUAL_DIVERGENCE` from a build without the binding (`labelBound` null) is not a reliable verdict**: its stop capture
may show an older frame than its label, and the measured CPU stop-vs-paused difference swung 0.969-8.266 run to run on such
builds (CUDA, which paints before labelling, read 0.65-1.69). Judge CPU divergence only on a bound capture (`labelBound` true).
On the pixmap route (`labelBoundBasis` `pixmap_item_by_construction`) a bound capture shows the forced repaint ran, not an
independent paint-path record; an acceptance that counts `label_bound` there must say so.

**Look Assist (D4b, `Get-DvLookAssistEvidence`).** A leg whose spec says Look Assist ON (a LOOK leg, or `generatorArgs.forceLookAssist`)
counts only when the app's own last `gui_smoke.visual_state` line says `look_assist_enabled=1`; `0`, a missing field or a missing
line is `INVALID` (`LOOK_ASSIST_NOT_HONOURED`), and the receipt's `lookAssist` block records what the app reported.

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
