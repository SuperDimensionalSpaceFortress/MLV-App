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
  whatever note `reason` already holds (`reason` keeps the first note). Each repeat is judged by its own pair's `source_id`s:
  a pair whose ids are equal is the source repeating its picture and is no repeat; a pair with a missing or malformed id is
  counted but proves nothing;
* `repeatedPlayFramesSourceDiffer`: the repeats whose own pair's source ids differ, the only ones that can make `PARTIAL_FREEZE`.

| verdict | when | counted |
|---|---|---|
| `FROZEN_DISPLAY` | the play frames show ONE picture while the app presented more than one frame across them AND the source frames are known to differ (`sourceDifference` `known-differ`) | **no**: the receipt is `INVALID` |
| `FROZEN_DISPLAY_SUSPECTED` | the same one picture, but nothing records whether the source frames differ (`sourceDifference` `unknown`): a static clip shows one picture legitimately | yes, and the detail says so |
| `PARTIAL_FREEZE` | more than one picture, but `repeatedPlayFramesSourceDiffer` is 2 or more AND the source frames are known to differ: the display's own paint stopped landing for part of the tail. One repeat is `OK`; over an `unknown` source the repeats stay a note (`repeatedPlayFramesNote`) on an `OK` block | yes, and the detail says so; a self-heal pre-launch with it is `INVALID` |
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
tracked as `PLAYBACK-SOURCE-FRAME-IDENTITY-1`; a build that carries it writes `source_id` per capture and `source_difference`
(next section). A build without it writes neither, and there a single picture reads `FROZEN_DISPLAY_SUSPECTED`. Missing or invalid
frame evidence (a frame with no id, a display frame that is not a whole number from 0 to 2147483647) is `unknown`, never `known-identical`. A self-heal pre-launch still needs `OK`, so either new verdict there makes the leg `INVALID`.

The divergence threshold (`$script:VisualDivergenceThreshold`, DualVenueRunner.psm1) is calibrated on a known-good leg: 6.0. The
known-good Bachelor leg (M16-1243 windowed-s2, CPU, build `adf6185a`, receipt `a5d3ffbe`) measured 1.099 on its worst channel;
three times that (3.3) is under the 6.0 floor, so the floor is the threshold.

## Source-frame identity: `source_id` and `source_difference` (PLAYBACK-SOURCE-FRAME-IDENTITY-1)

One picture across the play frames is FROZEN only if the SOURCE frames behind them differ; a static clip shows one picture
too. Display-side hashes cannot tell the two apart (a frozen display freezes them), so the app records the source itself. After
Play has stopped (step (4) of the capture block, never in the timed window or the tail), for each distinct `display_frame` of
the play captures it reads the bytes the decoder reads for that frame -- the encoded VIDF payload at
`video_index[frame].frame_offset`: `frame_size` bytes for LJ92 and JPEG2K, the packed RAW size otherwise -- under the file's
own mutex, and hashes them with FNV-1a 64 (`getMlvFrameSourceHash64`, `src/mlv/video_mlv.c`). A frame whose VIDF header at
`video_index[frame].block_offset` cannot be read has no id, because the display refuses it (r11). For LJ92 (r5) it decodes the
payload with liblj92, as the display decodes it, and hashes the decoded picture (`mlvLj92FrameSourceHash64`). For JPEG2K (r9) it
decodes the four declared channel spans through the display's own OpenJPH decode into private buffers and hashes the decoded
samples as the display stores them (uint16, channel 0..3, row-major); codestream bytes are never the id, so an equal-length COM
comment OpenJPH skips, bytes outside the spans or where a span lies change nothing (`mlvJpeg2kFrameSourceHash64`; a frame that
decode refuses, and every frame on a build without `ENABLE_JPEG2K`, whose display refuses JPEG2K, gives no identity). Every
identity is taken over the samples as the display stores them AFTER the clip's CURV linearisation table (r10: two frames the
table maps to one picture share an id; uncompressed RAW with a table hashes its unpacked samples, without one its packed bytes). The ids are attached only when the clip was HELD from its open, before any frame of it was decoded, until the hashing
ends (r8: a `--visual-capture-dir` run opens every clip under a share-read `SourceWriteGuard`, so no writer can change its bytes,
and the capture's guard must match the open-time stamp), so a frame a cache decoded never gets an id read from other bytes.
Only the Windows guard can be held (r10): its share-read reopen was measured (r11, on NTFS, locally and through loopback SMB) to
fail with a sharing violation while a handle has the file open for write, while a writable view outlives its file handle, or while a
writable section handle does, with or without a view. A store through such a view can leave the file's size and write time alone,
so only that refusal keeps it from an id. Other file systems and a writer on another host were not measured. On every other
platform the guard excludes no writer -- a `MAP_SHARED` writable mapping taken before the open can store into the clip without
moving its size, mtime or ctime -- so it is never held (`SourceWriteGuard::excludesExistingWriters()` is false), no source id is
attached, and `source_difference` is absent (unknown) on every non-Windows run. Mappings are not detected. Two frames that
decode to the same picture always share an identity, and two that decode differently never do (up to a 64-bit collision):
bytes the decoder never uses -- COM/APPn segments, ignored header fields, a table symbol the bitstream never selects, bytes
after the scan -- cannot make a static clip read "known to differ", and no byte the decoder uses can make two pictures read
"known identical". (Rounds r6-r9 replayed the decoder's marker walk instead, and each replay missed a byte it reads
differently: the DHT class/id byte liblj92 zeroes in place, and a table symbol the bitstream never selects.)

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

## 2026-11-04: E-01 strips for PLAYBACK-SOURCE-FRAME-IDENTITY-1 (PR 477, r13)

Build `7dc59c48b01cc186ab2b706f9d2ddbe378e32d8d`, manifest `21a2269e063c5311d5f831d8e742d85ffe6eb433f7de6c41597d87207f632433`,
Bachelor, clip M16-1243. Each strip was opened and read. Paths are under `.claude-state\dual-venue\` on the hub host; those files
are not tracked. This section changes no source, so the strips apply to the build named here and to any commit whose product
source equals it.

| Leg | Backend | Counted | Visual | `source_difference` | Play ids | Seen in the strip |
|---|---|---|---|---|---|---|
| fullscreen-s4 | cuda | yes | OK | `"differ"` | 12 of 12 distinct | real footage, frames 602-668 advancing in order, no visible defect |
| fullscreen-s4 | cpu | yes | OK | `"differ"` | 12 of 12 distinct | real footage, frames 601-667 advancing in order, no visible defect |
| windowed-s2 | cuda | yes | OK | `"differ"` | 12 of 12 distinct | real footage, frames 602-668 advancing in order, no visible defect |
| windowed-s2 | cpu | yes | OK | `"differ"` | 12 of 12 distinct | real footage, frames 601-664 advancing in order, no visible defect |

Every leg: `sourceDifference` `known-differ`, `sourceDifferenceCrossCheck` `agree`, `repeatedPlayFrames` 0, `look_assist_enabled=1`,
verification `ADVISORY` (`VENUE_ANCHOR_ABSENT`). The windowed CPU leg's unforced stop capture differs from its bound stop by
7.954, above the 6.0 threshold; its bound reading is 0.684, and CPU divergence is judged only on a bound capture.

Not proved: the `identical` branch (no leg had a repeated frame), decode equality with base (no base arm ran), JPEG2K or
uncompressed RAW at the venue, any speed claim, or the venue volume's file system (not recorded, so B4-UNMEASURED stays open).

| Leg | Receipt (`receipts\PLAYBACK-BACHELOR-PRESENT-JITTER-1\<leg>\bachelor\`) | Strip (`sheets\`) |
|---|---|---|
| fullscreen-s4 cuda | `e22ef07b-85fb-4045-aa53-2395b87cf314.json` | `strip-m16-1243-pace-cinematic-fullscreen-s4-bachelor-cuda-7dc59c48-20261104201615.png` |
| fullscreen-s4 cpu | `11645a15-4320-420d-9ce5-ba32db0af40f.json` | `strip-m16-1243-pace-cinematic-fullscreen-s4-bachelor-cpu-7dc59c48-20261104202152.png` |
| windowed-s2 cuda | `f305fa1f-4673-4762-a612-ed5e5c72369b.json` | `strip-m16-1243-pace-cinematic-windowed-s2-bachelor-cuda-7dc59c48-20261104202552.png` |
| windowed-s2 cpu | `78c6b219-ff1b-4d0a-9ef3-8657db593d38.json` | `strip-m16-1243-pace-cinematic-windowed-s2-bachelor-cpu-7dc59c48-20261104203116.png` |

`<leg>` is `m16-1243-pace-cinematic-fullscreen-s4` or `m16-1243-pace-cinematic-windowed-s2`.

## 2026-11-05: same-batch A/B for HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1 (PR 743, r10 legs, r11 reading)

Arm H is build `3801227dc191a50a5eaf52929f9806e8f6dcd95a`, manifest `eb37ec54a8960488c15923a08ad874a8a95ee69221828e345627c35b9fe25945`.
Arm C is its merge base `03b9ea28143cd08ff3020bb3a63d9538b95d289f`, manifest `75681b0a635aeaf39ad54270c7231af6c2d631a56beb82e7f18737a3750814e6`.
Twelve untraced CUDA legs ran on Bachelor, H C C H in each cell. Paths are under `.claude-state\dual-venue\` on the hub host; those
files are not tracked. Each strip was opened and read.

**These legs apply to those two builds only.** The merge just before this section brought master's `src/` and `platform/` changes into
the PR branch, so no later commit of the branch has the product source these legs measured. This section changes no source itself.

`presented` is `presented_fps` from the leg's `playback_smoke.summary` line, which is `presented_frames` / `elapsed_ms`. `timeline` is
`timeline_fps` from that line. `fired` is from the leg's `playback_due_wake` line; arm C has no due wake. Look Assist is the receipt's
`lookAssist.reported`, HONOURED on every leg.

| # | arm | cell | presented | timeline | fired | Look Assist | visual | receipt | strip stamp |
|---|---|---|---|---|---|---|---|---|---|
| 01 | H | M16-1457 windowed s2 | 59.154 | 59.392 | 409 | 1 | OK | `CUDA-HEADROOM-1\m16-1457-native-windowed-s2\bachelor\be735c0e-3fac-48ab-8e5e-d24f5ad83c5f.json` | 20261105101730 |
| 02 | C | M16-1457 windowed s2 | 45.627 | 59.486 | n/a | 1 | OK | `CUDA-HEADROOM-1\m16-1457-native-windowed-s2\bachelor\702542a4-c77f-4451-a6ad-f0c7a8218fee.json` | 20261105102134 |
| 03 | C | M16-1457 windowed s2 | 45.164 | 59.503 | n/a | 1 | OK | `CUDA-HEADROOM-1\m16-1457-native-windowed-s2\bachelor\0c42e971-b407-4fc8-a227-01999eab1959.json` | 20261105102525 |
| 04 | H | M16-1457 windowed s2 | 59.138 | 59.575 | 416 | 1 | OK | `CUDA-HEADROOM-1\m16-1457-native-windowed-s2\bachelor\57f23e51-3c87-454e-aedb-1ebaf6997c48.json` | 20261105102923 |
| 05 | H | M16-1457 fullscreen s2 | 50.890 | 59.691 | 83 | 1 | OK | `CUDA-HEADROOM-1\m16-1457-native-fullscreen-s2\bachelor\2adab724-dd9f-4bc2-8128-f6bdd2e6979b.json` | 20261105103335 |
| 06 | C | M16-1457 fullscreen s2 | 50.230 | 59.482 | n/a | 1 | OK | `CUDA-HEADROOM-1\m16-1457-native-fullscreen-s2\bachelor\cc17d25b-babe-4427-8168-3cbbc1e4cda8.json` | 20261105103726 |
| 07 | C | M16-1457 fullscreen s2 | 50.689 | 59.508 | n/a | 1 | OK | `CUDA-HEADROOM-1\m16-1457-native-fullscreen-s2\bachelor\7d82ae91-d8c7-4650-b940-0434d11bce0a.json` | 20261105104120 |
| 08 | H | M16-1457 fullscreen s2 | 50.645 | 59.503 | 74 | 1 | OK | `CUDA-HEADROOM-1\m16-1457-native-fullscreen-s2\bachelor\81bdbae1-bf98-438d-9d9f-5583811a6514.json` | 20261105104639 |
| 09 | H | owner shape M16-1243 fullscreen s4 | 23.912 | 23.992 | 593 | 1 | OK | `PLAYBACK-BACHELOR-PRESENT-JITTER-1\m16-1243-pace-cinematic-fullscreen-s4\bachelor\de625d9b-977f-47e2-b0e0-83fcf70c8ab0.json` | 20261105105148 |
| 10 | C | owner shape M16-1243 fullscreen s4 | 23.899 | 23.979 | n/a | 1 | OK | `PLAYBACK-BACHELOR-PRESENT-JITTER-1\m16-1243-pace-cinematic-fullscreen-s4\bachelor\38e3f762-f566-4904-8301-0253bb804738.json` | 20261105110132 |
| 11 | C | owner shape M16-1243 fullscreen s4 | 23.936 | 23.976 | n/a | 1 | OK | `PLAYBACK-BACHELOR-PRESENT-JITTER-1\m16-1243-pace-cinematic-fullscreen-s4\bachelor\ff31d0dd-8175-4604-943a-00c0803d9a8f.json` | 20261105110701 |
| 12 | H | owner shape M16-1243 fullscreen s4 | 23.901 | 23.981 | 593 | 1 | OK | `PLAYBACK-BACHELOR-PRESENT-JITTER-1\m16-1243-pace-cinematic-fullscreen-s4\bachelor\e2fdc743-b042-4d27-942f-b598573236f4.json` | 20261105111318 |

A strip is `sheets\strip-<leg id>-bachelor-cuda-<first 8 of the arm's commit>-<strip stamp>.png`; `<leg id>` is the folder after the card
in the receipt path.

Land condition, as the hub set it before the legs ran (medians of two runs per arm):

| Clause | H | C | Result | Verdict |
|---|---|---|---|---|
| Windowed s2: H - C >= +4 fps, the arms' runs do not overlap | 59.146 | 45.396 | +13.751 fps; 59.138-59.154 vs 45.164-45.627 | met |
| Fullscreen s2: H no more than 2 % below C | 50.768 | 50.460 | +0.308 fps, +0.61 % | met |
| Owner shape: H - C >= -0.3 fps, and no more than 2 % below C | 23.907 | 23.918 | -0.011 fps, -0.05 % | met |
| Timeline max <= 60.04 | | | 59.691 | met |
| Fired <= 3x presents | | | largest ratio 0.993 | met |
| Look Assist on where the spec says on | | | 12 of 12 | met |
| Every strip opened and clean | | | 12 of 12 | met |

The claim is a windowed improvement, with no measured harm in the fullscreen and owner cells, at n=2 per arm, CUDA, on these three cells.

Not proved: the card's wake bars, which r9 measured as failed on the trace legs (`late_p50` 12.594 / 3.781 / 3.387 / 3.242 ms against
< 1.0; first advance p50 10.76 / 10.79 / 1.44 / 10.89 ms against <= 7.5) and which stay not met; any fullscreen speedup, moved to
`HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1`; a same-batch or untraced reading of any other display matrix cell (s1, s4 on
M16-1457, and every CPU cell rest on r9's trace legs, whose fps is not speed); any venue but Bachelor. Every receipt is `ADVISORY`
(`VENUE_ANCHOR_ABSENT`).
