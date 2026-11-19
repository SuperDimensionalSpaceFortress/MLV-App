# Debayer quality legs (EXPORT-DEBAYER-QUALITY-1)

These export legs measure what each debayer costs and what it buys, on owner footage, without the footage ever leaving the venue.
They are ProRes export legs (`docs/dual-venue-evidence.md`, "EXPORT legs") with an optional `export.quality` block.

## The legs

`tools/profiling/dual-venue/legs/m16-{1243,1456}-export-prores-debayer-{bilinear,amaze,rcd,dcb,lmmse,igv}.json`, 120 timed frames each. Look
Assist is on, as the owner shapes it. The leg sets are `legsets/debayer-quality-m16-1243.json` and `legsets/debayer-quality-m16-1456.json`.
A quality leg's plan cell is `cpu-export-prores-<legId>`, so the six legs of one clip are six cells.

* **Receipts.** Each `look-receipts/export-owner-hq-{dualiso,nodualiso}-debayer-<name>.marxml` is byte-identical to its base receipt except the one
  `<debayer>N</debayer>` line. The values come from `platform/qt/ReceiptSettings.h:16`: bilinear 2, LMMSE 3, IGV 4, AMaZE 5, RCD 7, DCB 8.
  The batch applies that value through `src/batch/BatchRunner.cpp:398-410`. AMaZE uses the base receipts unchanged, so their hashes do not move.
  The base receipts have sharpen, denoise, chroma smoothing and CA correction at 0, so nothing masks debayer artefacts.
* **Clip class.** Both clips are measured dual-ISO (`KNOWN_CLIP_DUAL_ISO_CLASS`). No consented clip has a measured non-dual-ISO class yet
  (`EXPORT-NODUALISO-CLIP-CLASS-1`).
* **No Look Assist OFF arm.** `S3_debayer` is captured straight out of the debayer, before all processing, so it already isolates the debayer.

## Two runs in one job

`tools/profiling/bachelor/export-stage-job.ps1` runs these steps:

1. **The timed export**, unchanged. No capture variable is set, so `exportWallSeconds` and `stage-timing.log` carry no capture writes.
2. **An untimed quality pass**, only when the timed run captured. It uses the same argv with `--max-frames 24`. The job-owned venue-local capture
   dir (`New-AttrCudaOwnedRoot`) goes in `MLVAPP_PIPELINE_CAPTURE_DIR`. `MLVAPP_PIPELINE_CAPTURE_FRAMES` holds the absolute frame indices:
   `cutIn - 1 + offset`. That is the batch's own cut-in rule (`BatchRunner.cpp:367-389`: a cut-in of 0 reads as 1; the render loop runs
   `frameIndex = cutIn - 1 .. cutOut - 1` at `BatchRunner.cpp:548`). The capture harness keeps at most 64 frames
   (`src/mlv/pipeline_stage_capture.c:19`), and the job scrubs every inherited `MLVAPP_*` variable before it sets its own.
3. **The metric, on the venue.** `tools/profiling/dual-venue/debayer_quality.py` travels inline as base64 with a sha256 baked in at generation.
   The job checks that sha256 before it writes the script and again on disk before it runs it, with `-I`. It finds Python 3 the way
   `Resolve-AttrCudaOwnerPython` does. If the venue has none, the quality verdict is `VENUE_TOOLING` (`EXPORT_QUALITY_TOOLING_ABSENT`) and the timed
   result stands.
4. **Publish and delete.** The job publishes `quality.json`. It holds scalars, tile indices, `isoValue` and the sha256 of each captured S2, S3 and
   S4 buffer. It never holds a capture, a frame, a path or a file name. Then the job deletes the captures and the quality run's `.mov` by
   ownership proof. If `captureDeleted` is false, the leg is `INVALID` (`EXPORT_CAPTURE_NOT_DELETED`).

**Time budget.** The quality pass adds one more export wait plus the metric's 1800 s bound and a 120 s margin. `Get-ExportJobTimeBudget -Quality`
adds this term *before* the agent's 86400 s cap check. The lease batch's preflight passes `-QualityBudget` for a quality leg, so a window that
would fit only without the pass is refused (`DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED`) before a lease is taken.

`summary.json.quality` records the pass, and `Get-DvExportQualityEvidence` (`DualVenueRunner.psm1`) reads it. The receipt writer and
`Test-DvReceiptValid` both use that one reader, so `receipt.export.quality` is re-derived from the re-hashed files, never trusted.
If a requested frame or stage has no capture, the result is `EXPORT_QUALITY_CAPTURE_ABSENT`, which is `INVALID`.

**The S3 sidecar's `debayer_mode` label is never read.** `src/mlv/frame_caching.c:1253-1263` maps it wrong, so the debayer is bound from the
leg's receipt value instead. Successor: `DEBAYER-CAPTURE-LABEL-1`.

## Metrics (`debayer_quality.py`, Python 3 stdlib, deterministic)

* **Tiles**: `tileSize` px tiles inside a 32 px border. They are classified from `S2_post_dualiso` only, so the sets are the same for every
  debayer of a clip. Per tile: the mean `|dG|` between neighbouring 2x2-quad green means on both axes, the mean level, and the max.
  **FLAT** is the `tilesPerClass` lowest-energy tiles whose mean lies in [10%, 80%] of the black-to-white range. **EDGE** is the `tilesPerClass`
  highest-energy tiles whose max is under 98%. Ties break by tile index. The black and white levels come from the clip's RAWI block. If the S2
  samples run past the RAWI white level, the levels come from the frame's own range instead (`levelSource: observed`).
* **Luma** `Y = (R + 2G + B) / 4`; **chroma** `C1 = R - G`, `C2 = B - G`.
* **DETAIL** `NCGE` = mean over EDGE pixels of `(Y[x+1]-Y[x-1])^2 + (Y[y+1]-Y[y-1])^2`, minus the same mean over FLAT pixels.
* **NOISE** `sigmaY = sqrt(mean over FLAT tiles of var(Y - box3(Y)))`. `sigmaC1` and `sigmaC2` use C1 and C2 the same way, and
  `sigmaC = sqrt` of their mean variance.
* **FALSE COLOUR** `fc`: in each EDGE tile, take its top 10% `|grad Y|` pixels and compute
  `mean(|C1 - box5(C1)| + |C2 - box5(C2)|) / mean(Y of the tile)`. Then average over the EDGE tiles.
* **ISO**: `isoValue` from the clip's first `EXPO` block, read-only from the job's private view.
* **TIME** comes from the timed run only: `exportWallSeconds` (`summary.json`), and the mean, p10 and p90 of `debayered_frame` ms
  (`stage-timing.log`, nearest-rank).

## The rule table (hub-side, offline)

`tools/profiling/dual-venue/debayer_rule_table.py --receipt <r.json> ... --out tools/profiling/dual-venue/debayer-rule-table.json`. It reads only
the published receipts and re-hashes every input against `receipt.export.files`. The schema is `debayer-rule-table.schema.json`
(`mlv-app/debayer-rule-table/v1`).

* **Reference: the bilinear floor** of the same clip and stage. AMaZE is not the reference: there is no ground truth, and AMaZE is the owner
  default, so an AMaZE reference would make the advisor rule confirm itself.
* **recommended**: the highest `detailRatio` among the non-bilinear debayers whose `falseColourRatio` and `noiseRatio` are each at most 1.10 x the
  class minimum. When two are within 2% on detail, the lower `debayerMsMean` wins. **fastestAcceptable**: the lowest `debayerMsMean` whose three
  ratios are each within 5% of the recommended one's. The rule is computed on `S4_processing`. `s3AgreesWithS4` records whether S3 picks the same
  debayer.
* NCGE is signed (EDGE minus FLAT energy). A non-positive bilinear NCGE gives no `detailRatio`, and the 2% tie band is measured down from
  `|best|`, so it always holds the best.
* **Integrity**: a clip's six debayers are compared only when they measured the same way: the same `stages`, `tileSize`, `tilesPerClass`,
  `captureOffsets`, `frames` and `border` (`DEBAYER_QUALITY_PARAMS_DIVERGED`). The S2 sha256s must be identical (`DEBAYER_INPUT_DIVERGED`), as must
  the FLAT/EDGE tile sets on every frame (`DEBAYER_TILES_DIVERGED`). The S3 sha256s (S4 when no leg captured S3) must be present
  (`DEBAYER_CAPTURE_INCOMPLETE`) and pairwise distinct on every frame (`DEBAYER_NOT_APPLIED`). A class is `measured` only when all six debayers PASS with a captured quality pass.
  The low and high non-dual-ISO bands are emitted as `unmeasured`. `DEBAYER-ADVISOR-1` must never extrapolate into an unmeasured row.
* `debayer-rule-table.json` is committed only from real PASS receipts.
