# Dual-Venue Evidence

How the fleet gets hardware evidence from **both** test machines -- Bachelor (the laptop) and
Ultra-Magnus ("UM", the 4090 tower) -- in one framework, without ever treating them as one machine.
The venues do **not** run at the same time: each works through its own queue at its own pace and files
its own receipt. This page is the methodology for people; the code is under
`tools/profiling/dual-venue/`.

## The seven principles (each is a refusal the code enforces and a test pins)

| | Principle | Where it is enforced |
|---|---|---|
| P1 | **Independent venues, shared subject.** A leg is defined once; "paired" means the same *subject digest*, never the same moment. No cross-venue timing is ever a verdict. | `subject.digest` in every receipt |
| P2 | **No merged verdict.** Each receipt is judged against its own venue. A reconciler shows the two side by side and a *diagnostic* delta only when both are complete. | `Get-VenueEvidence.ps1` (DUAL-VENUE-RECONCILE-1) |
| P3 | **Venue role is data.** `venues.json` gives each venue a role per card (`acceptance` / `supplementary`); nothing relabels a receipt afterwards. | `venues.json` + `Get-DvVenueRole` |
| P4 | **Typed terminals, zero partial credit.** The outcome is one of `PASS FAIL VENUE_UNHEALTHY VENUE_NOT_QUIESCENT VENUE_HOST_MISMATCH DEVICE_UNAVAILABLE UNRESOLVED RETRACTED`. Only PASS/FAIL carry signal. Exit code, silence or output size are never completion evidence. | `Write-DvReceipt` rejects any other value |
| P5 | **Health before timing.** A bounded probe (pwsh cold start, write+hash of a fixed 4 MiB buffer, free disk, commit charge) precedes every leg; unhealthy means the leg is **not submitted**. | `Invoke-VenueLeg.ps1` step 3 |
| P6 | **One source of truth for the venue.** The declared `-Venue` must agree with `Get-AttrCudaMeasurementVenue` on the host that runs the job, else `VENUE_HOST_MISMATCH`. | probe check + in-job guard (exit 29) |
| P7 | **Safety unchanged.** Fixtures only until UM-OWNER-FOOTAGE-CROSS-VOLUME-2 lands; every share write goes through `tools/profiling/um-run.ps1` (NA-7). | clip admission + `Submit-VenueJob` |

## Pieces

* `tools/profiling/dual-venue/venues.json` -- the venue table: share, agent root, scratch root,
  expected host name, health thresholds, per-card roles, `defaultRole`, and the owner-footage gate.
  **Changing a role is a reviewed commit, never a runtime flag.**
* `tools/profiling/bachelor/playback-attr-3-cuda-job.ps1` -- the job generator, now with
  `-Venue bachelor|ultra-magnus`, `-Backend cuda|cpu`, `-ScaleFactor`, `-CpuQuiescenceThresholdPercent`,
  `-ForceLookAssist`, `-LookFlavor`. With default arguments its output is **byte-identical** to before
  (pinned by `tools/repo_hygiene/test_dual_venue_evidence.py` against a pre-card commit).
* `tools/profiling/dual-venue/Invoke-VenueLeg.ps1` -- runs one leg on one venue and **always** writes a
  receipt. `DualVenueRunner.psm1` holds its testable rules.
* `tools/profiling/dual-venue/New-VenueSheetPair.ps1` -- composes the side-by-side cuda|cpu contact sheet
  for a LOOK leg from two receipts (`make-contact-sheet.py --pair-dir`, paired by frame index).
* `tools/profiling/dual-venue/leg-spec.schema.json` and `legs/*.json` -- the leg specs.
* `Get-VenueEvidence.ps1` (the reader; built by DUAL-VENUE-RECONCILE-1) -- reads receipts; acceptance
  reads only `-AcceptanceFor <card>`.

## Backends: every playback leg runs as cuda AND cpu on every venue

A speed or look leg has `backends: ["cuda","cpu"]`. `-Backend cpu` omits every `MLVAPP_GPU_*` /
`MLVAPP_EXPERIMENTAL_GPU_*` environment variable, requires `CPU_FRAMES > 0` and zero GPU frames
(a leg that reached a GPU path is `CPU_BACKEND_PATH_MISMATCH`, exit 28), never fires the CUDA-only
exits 13/14, and treats PresentMon as informational. **CPU frame rate is informational** -- it tracks
cores and storage, not the product's GPU work -- and never gates a card. `backend` is part of the
subject, so a cuda receipt and a cpu receipt are different subjects.

## LOOK legs and contact sheets

A `legType: "look"` leg takes `look.contactSheetFrames` evenly spaced frames on each backend with
**Look Assist forced on** (the venue's persisted "use default receipt" setting is switched off for the run
and the smoke runner is told Look Assist is *required*, so a leg where it did not apply fails closed).
The job composes a one-backend sheet; `New-VenueSheetPair.ps1` then pairs the two backends' raw frames
into one `cuda | cpu` sheet, by frame index. Each receipt's `look` block carries the sheet's sha256 and
path; the pair is its own record (`mlv-app/dual-venue-sheet-pair/v1`) because receipts are never edited.

* `lookFlavor` (`classic` default | `cinematic`) is in the leg spec and the receipt subject and is passed
  to the app as `MLVAPP_LOOK_ASSIST_FLAVOR`. **The app does not read it yet** (LOOK-ASSIST-FLAVORS-1), so
  every receipt says `lookFlavorHonored: "unknown"` -- never a claim that it applied.
* Aesthetics are **model-judged** (Amendment 2). Receipts carry `owner_verdict: null` (optional, never
  waited on, never written by the runner) and `model_verdicts: []` (filled by the judge card).
* **Fixture sheets may be shown to the owner. Sheets of owner footage stay local under `.claude-state`**
  (never committed, attached to a PR, published to the bus or as an artifact) and need CROSS-VOLUME-2 plus
  the per-venue owner CLIP line.

## How to add a leg

1. Copy `legs/fixture-speed-large.json` (or `fixture-look-large.json`); give it a new `legId` and the
   `card` that needs the evidence. The file validates against `leg-spec.schema.json`.
2. Declare the **roles before the first byte** (kernel K5): add the card to `venues.json` `roles`
   (`bachelor: acceptance`, `ultra-magnus: supplementary` for a card whose acceptance venue is Bachelor).
   A card the table does not name gets `defaultRole` (`supplementary`) on both venues.
3. Put the pass criteria per **role** and per **backend** in `criteria`: `{metric, op, value}` triples
   over the metrics copied verbatim from the job's `summary.json` (`rows`, `gpuFramesTotal`, `cpuFrames`,
   `lookAssistForced`, `presentMon*`, ...). An empty list is informational. A metric the job did not
   write *fails* the criterion -- a missing number is never a passing number.
4. `legSpecSha256` is the sha256 of the file's exact bytes, so editing a leg makes a new subject.

## How to run a leg on a venue

Stage the **same build** on the venue first (assemble once; stage with `playback-attr-3-cuda-stage-job.ps1`
and `attr3-stage-smoke-runner-job.ps1` using `-AgentRoot` from `venues.json`; the fixture must be in the
venue's cache). The runner refuses -- `DEVICE_UNAVAILABLE` -- to substitute a different build.

```powershell
pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueLeg.ps1 `
    -Venue ultra-magnus -LegSpec tools\profiling\dual-venue\legs\fixture-look-large.json `
    -SourceCommit <40-hex> -BuildManifestSha256 <64-hex of the staged build.json> -Backend cuda
```

Add `-HealthOnly` to probe a venue and stop (the receipt is `UNRESOLVED` -- nothing was measured).
The last lines print `DVE_OUTCOME=`, `DVE_DETAIL=` and `DVE_RECEIPT_PATH=`. **Read the receipt, not the
exit code**: exit 0 means "a receipt was written", exit 2 means the receipt itself could not be written.

The runner snapshots the venue's `HKCU\Software\magiclantern.MLVApp` QSettings before the leg and restores
them afterwards (the app rewrites per-user settings, including a "last opened file" path); the receipt's
`registry.restored` says whether the restore verified. Values are never printed.

Receipts land in `<main checkout>\.claude-state\dual-venue\receipts\<card>\<legId>\<venue>\<receiptId>.json`,
append-only (created with `CreateNew`; a repeat is refused, never overwritten). Copied evidence
(`summary.json`, the manifest, the contact sheet) is under `.claude-state\dual-venue\evidence\<receiptId>\`.

## How to read the evidence

* **Acceptance** reads only receipts whose `venue.role` is `acceptance` for the card, and only `PASS`/`FAIL`.
  A venue that was unhealthy, not quiescent, unreachable or a mismatch leaves the card *open*, not failed.
* **Supplementary** evidence informs diagnosis -- fails on both: code; fails only on Bachelor: the venue --
  but never closes a card.
* **Owner-clip legs** need CROSS-VOLUME-2 merged *and* the owner's CLIP line for that venue (consent on one
  venue never implies the other). Until then the runner refuses before submitting anything and writes a
  receipt with `refusal: OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2`.

## Outcome mapping (job `RESULT=` token -> receipt outcome)

| Job result | Outcome |
|---|---|
| `MEASUREMENT_CAPTURED`, `FIXTURE_REHEARSAL_CAPTURED` | `PASS` or `FAIL` from the role/backend criteria |
| `VENUE_NOT_QUIESCENT` | `VENUE_NOT_QUIESCENT` |
| `VENUE_HOST_MISMATCH` | `VENUE_HOST_MISMATCH` |
| `BACKEND_NOT_AVAILABLE` | `DEVICE_UNAVAILABLE` |
| `DISPLAY_ASLEEP`, `KEEPALIVE_FAILED`, `SCREENSAVER_SECURE_OWNER_ONLY`, `DISPLAY_WAKE_DISMISS_FAILED` | `VENUE_UNHEALTHY` (a venue condition, not a product result) |
| anything else (`SMOKE_RUN_FAILED`, `GPU_RECON_FRAMES_ZERO`, `CPU_FALLBACK_DETECTED`, `CPU_BACKEND_PATH_MISMATCH`, `PRESENTMON_UNAVAILABLE`, ...) | `FAIL`, with the token in `outcomeDetail` |
| um-run `RETRACTED` / `UNRESOLVED` | `RETRACTED` / `UNRESOLVED` |
