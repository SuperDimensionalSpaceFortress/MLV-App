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
| P4 | **Typed terminals, zero partial credit.** The outcome is one of `PASS FAIL VENUE_UNHEALTHY VENUE_NOT_QUIESCENT VENUE_HOST_MISMATCH DEVICE_UNAVAILABLE UNRESOLVED RETRACTED INVALID VENUE_TOOLING SCALE_NOT_HONOURED`. Only PASS/FAIL carry signal, and a PASS/FAIL without the receipt oracle's verdict, and the admission it was admitted on, is `INVALID` (rounds 2-3). Exit code, silence or output size are never completion evidence. | `Write-DvReceipt` rejects any other value and any proofless PASS/FAIL |
| P5 | **Health before timing.** A bounded probe (pwsh cold start, write+hash of a fixed 4 MiB buffer, free disk, commit charge) precedes every leg; unhealthy means the leg is **not submitted**. | `Invoke-VenueLeg.ps1` step 4 |
| P6 | **One source of truth for the venue.** The declared `-Venue` must agree with `Get-AttrCudaMeasurementVenue` on the host that runs the job, else `VENUE_HOST_MISMATCH`. | probe check + in-job guard (exit 29) |
| P7 | **Safety unchanged.** A leg names a consented clip id and runs only on a venue the owner consented it for; every share write goes through `tools/profiling/um-run.ps1` (NA-7). | `Get-DvClipAdmission` + `Submit-VenueJob` |

## Long clips only (round 2; `docs/playback-clip-length-rule.md`)

Every leg here **plays the app** (speed, LOOK / contact sheet, on cuda and cpu), so the owner's rule applies to
all of them: at least 20 s of distinct source frames of real footage, never a loop, a replay or a short clip.
The runner does not re-implement that rule; it routes every playing leg through master's evidence launchers and
receipt oracle and refuses to believe a receipt that does not carry the oracle's proof.

* **A leg is addressed by a consented clip id, never a path** (`clipId: "M16-1243"`; the leg-spec schema pins the
  id shape, and `clipPath` is not a property). The runner passes the id to the generator, which resolves it
  through `tools/gates/resolve_consented_clip.py`; no path appears in a leg, a receipt or a refusal.
* **Fixtures are not legs.** `tiny_dual_iso` (2 frames) and `large_dual_iso` (16 frames) can never satisfy 20 s.
  A leg that names one is refused up front, typed `FIXTURE_REFUSED_CLIP_TOO_SHORT` (the verdict of
  `gui-smoke-length-gate.ps1`, the same gate that refuses a short clip anywhere else), before anything is
  generated or submitted -- not even the health probe. The two fixture legs of round 1 are gone.
* **The play window is the spec's `playSeconds`** (default 25, floor 20; under 20 is `PLAY_WINDOW_TOO_SHORT`).
  The generator refuses a window the clip cannot cover.
* **Per-venue consent, read only as committed.** `tools/profiling/dual-venue/venue-clip-consent.json` is a tracked,
  **owner-written** file. Each record is exactly `venue`, `clipId`, `ownerLine` (the owner's exact typed line,
  `CLIP <venue>: <clip id>`, which contains no path), `ownerLineSha256` (the sha256 of that line), `recordedUtc` and
  `recordedBy: "owner"`; the line must name the record's own venue and clip id and hash to `ownerLineSha256`, so a bare hex
  string is not the owner's words. **One alias:** the line may spell the venue as the venue table's name (`CLIP ultra-magnus: M16-1243`)
  or as that same name with every hyphen removed (`CLIP ultramagnus: M16-1243`, which is how the owner typed it). The match is
  case-sensitive and exact -- `UltraMagnus`, `ultra_magnus`, extra whitespace, a path suffix or another venue's name are all refused --
  the record's `venue` field stays the table name, and `ownerLine` is stored verbatim (agents never alter the owner's words). Agents and producer lanes never write the file; the hub adds the owner's lines in its
  own reviewed commit. **Production reads the consent file and the venue table ONLY as committed at `HEAD`**
  (`git rev-parse HEAD:<path>` + `git cat-file blob`) and refuses when the working copy differs
  (`ADMISSION_SOURCE_DIRTY`) or the file is not in `HEAD` (`ADMISSION_SOURCE_NOT_COMMITTED`): a file a caller writes
  anywhere is never consent, and an uncommitted edit to `venues.json` cannot flip the reviewed switch. The
  `-ConsentPath`, `-VenueTablePath`, `-RepoRoot`, `-WorkDir`, `-GeneratorScript` and `-UmRunScript` overrides are
  **test seams, refused (`DVE_TEST_SEAM_IN_PRODUCTION`) unless `-OfflineTestMode`**; offline test mode can never reach a
  venue (it needs a stub `-UmRunScript` that is not `um-run.ps1` by path or content, and every venue's `agentShare` must be a
  local directory under the OS temp folder), and the receipt it writes says `admission.mode = offline-test` and is not
  evidence (`Test-DvReceiptValid` refuses it). The receipt records what it was admitted on: `admission.consentBlobSha`,
  `admission.venueTableBlobSha`, `admission.headCommit`, `admission.consentLastCommit` and the owner line's sha256. A venue
  with no record for the clip is refused before submitting (`VENUE_CLIP_CONSENT_ABSENT`) -- consent on Bachelor never
  implies Ultra-Magnus, nor the reverse. A missing, empty or malformed file refuses everything
  (`VENUE_CLIP_CONSENT_INVALID`); a record is exactly six keys, so a path cannot ride along. `venues.json`
  `ownerFootage.cleanupClassGone` is a second, reviewed switch, and it is **on** (DVE-OWNER-FOOTAGE-GATE-1): the class it
  named -- a job deleting, hard-linking or relocating a *name* of owner footage -- is gone on master (no tool creates a hard
  link to owner footage, a view is a symbolic link or a verified byte copy, the original is pinned `FileShare.Read` for the
  run, and every delete demands a creator-recorded ownership proof; OWNER-FOOTAGE-NO-HARDLINK-1/-2, PR #214, which
  superseded the closed UM-OWNER-FOOTAGE-CROSS-VOLUME-2). Switched off, an owner leg refuses with
  `OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2` (the token is kept for that case).
  `tools/repo_hygiene/test_dual_venue_owner_footage_gate.py` binds the switch to the guard tests that justify it (it may read
  true only while each named guard test is still defined and not skipped) and censuses the delete / move primitives of the
  sources an owner leg runs. The file
  ships with **no records**: every owner leg refuses until the hub records the owner's lines.
  Honest limit: "committed" is not "reviewed" -- any local commit changes `HEAD`. The receipt names the blob ids and the last
  commit that touched the file so a reader can check them against the reviewed history; the control on *who may write a
  record* is the hub's review of that commit.
* **The receipt carries a verdict it can re-derive.** The proof is taken from the run's OWN records, not from a summary the job
  wrote about itself: the launcher's `result.json` (the nonce it generated, the sha256 of the run log it snapshotted) and the
  app's run log (`logs/smoke-run.log`, the measured session's `playback_smoke.summary` line). The receipt's `playback` block
  carries `sourceAdvanced`, `requiredSourceFrames`, `nativeFps`, `paceFps`, `fpsOverride`, `wrapped`, `wrapCount`,
  `expectedRunNonce` (the launcher's), `observedRunNonce` (the one the app echoed), `manifestRunNonce`, `logSha256` /
  `logShaBound`, `settingsIsolated`, the job's own block for cross-checking, `fixtureRehearsal` and the job's clip id, and
  `Get-DvPlaybackProblems` re-derives the verdict from those fields alone (`Test-DvReceiptValid` does it for a reader; a
  stored `valid: true` is never believed): `source_advanced >= required_source_frames >= ceil(20 s x native_fps)`, paced at
  the native fps (within 0.5 %), no fps override, no wrap, **the app echoed exactly the nonce the launcher generated**, the
  run log is the snapshot the launcher hashed, the app used its run-scoped settings store, a non-rehearsal run of **this**
  leg's clip. **The nonce is the one the real launcher mints** -- `[Guid]::NewGuid().ToString("N")` in
  `tools/profiling/run-release-gui-smoke.ps1`, 32 lowercase hex -- and the tests evaluate that very expression rather than a
  hand-made constant. **Every field must be present** -- an absent field is `INVALID`, never "no wrap". A PASS/FAIL that
  fails this is `INVALID` (`Invoke-VenueLeg.ps1`) and `Write-DvReceipt` refuses to write it a second time; a printed capture
  whose job exited non-zero, or a captured job that wrote no `sourceFrames` block of its own, is `INVALID` too. A job smoke
  refusal of the length class (`PLAY_WINDOW_TOO_SHORT`, `INVALID_LOOPED`, ...) is `INVALID`, never a product `FAIL`.
  Readers (`Get-VenueEvidence`) must call `Test-DvReceiptValid -RepoRoot <repo> [-EvidenceDir <dir>]` (next bullet).
* **PRODUCTION RECEIPTS ARE ADVISORY (DUAL-VENUE-EVIDENCE-2 round 2; the narrowing exit, hub ruling 2026-10-02).** There is no
  venue-held anchor: nothing the venue signs reaches this repo, and `um-run.json` is written by the runner itself, so a receipt
  can show that it is consistent with committed consent and with the hashed files it names, but never that those files came from
  a venue run. `Test-DvReceiptValid` therefore returns, for a production PASS/FAIL receipt that re-derives, `valid = false`,
  `status = ADVISORY` and the typed reason `VENUE_ANCHOR_ABSENT`; it **never returns `VERIFIED`** for a production receipt and
  nothing here produces a usable PASS. A receipt that does not re-derive is `INVALID` / `INCOMPLETE` (and also carries the reason).
  A test pins the advisory result and a mutation that re-enables production PASS fails it. Offline test mode (never evidence)
  keeps `VERIFIED_OFFLINE_TEST` for the harness. **Production PASS verification is a later card, `DUAL-VENUE-PASS-PROVENANCE-1`,
  which needs a design step first**: a venue-agent signature over `summary.json`, or a validator that re-reads
  `\\<venue>\mlv-agent\outbox` by `jobId`. **Declared threat model (forward-only).** *In scope:* our own tools, mislabelling,
  legacy or other-lane receipts, a wrong-leg or wrong-backend pairing, an edited or stale receipt, a line-ending artefact.
  *Out of scope, accepted and shown to the owner:* a deliberate forger who writes a whole matching evidence set.
* **A receipt is advisory only when every claim in it is re-derived from a committed blob or a hashed artifact; no field the
  receipt asserts about itself is an input to the verdict** (DUAL-VENUE-EVIDENCE-2; PR #207's validator believed hash *formats* and
  self-asserted booleans, so a hand-built production PASS over an empty consent blob validated). `Test-DvReceiptValid` returns
  `{ valid; status; reasons; unbound; evidenceDir }`, `status` being `ADVISORY` (production, everything re-derives: **not valid**) |
  `VERIFIED_OFFLINE_TEST` (offline harness only) | `INCOMPLETE` (a piece of evidence is absent: never a PASS/FAIL) |
  `INVALID` (a claim does not re-derive) | `NO_SIGNAL` (any outcome other than PASS/FAIL); `VERIFIED` exists in the contract but no
  production receipt can reach it. `Write-DvReceipt` runs the **same** validator before it writes a PASS/FAIL (an advisory receipt is
  written and stamped `verification.status = ADVISORY`; anything that does not re-derive is refused), and so does every reader
  (`New-VenueSheetPair.ps1` accepts exactly `ADVISORY` and says so in its record, and `Get-VenueEvidence` when RECONCILE-1 builds
  it; a test pins that every in-tree caller passes `-RepoRoot`).
  * *Admission, from git:* `admission.consentBlobSha` and `venueTableBlobSha` must be real **blobs** of the repo (`git cat-file
    -t` says blob; a placeholder hash, a commit or a tree is refused) and be the files committed at `admission.headCommit`;
    the table parses; the consent blob parses with `Read-DvClipConsent` and holds an owner record (`recordedBy: owner`, the
    exact `CLIP <venue>: <clip>` line, the venue spelled as the table name or hyphen-free) **for this venue and clip id** whose line sha256 equals `admission.ownerLineSha256`;
    the committed cleanup switch is on; `venue.role` is the table's. `git` runs with `GIT_DIR`, `GIT_WORK_TREE`,
    `GIT_INDEX_FILE` and the object-store overrides scrubbed and `--no-replace-objects`, so the environment cannot point it at
    another repo. A production receipt validated without `-RepoRoot` is `INCOMPLETE`, never VERIFIED.
  * *The leg, from git:* `subject.legSpecSha256` must name a leg spec **committed under `tools/profiling/dual-venue/legs/`**
    at `headCommit` (the runner refuses `LEG_SPEC_NOT_COMMITTED` up front in production), and PASS vs FAIL is re-derived
    from that committed spec's criteria for the venue's role and backend over the evidence's verbatim metrics.
  * *The run, from hashed files:* the receipt names its `evidence.localEvidenceDir` (only a hint: `-EvidenceDir` wins) and the
    sha256 of each file there -- `summaryJsonSha256`, `evidenceManifestSha256`, `resultJsonSha256`, `logSha256`,
    `umRunJsonSha256` (`um-run.json` records the job's exit code and RESULT token). The validator re-hashes every file (an
    edited one is `INVALID`, an absent one `INCOMPLETE`), re-derives the whole `playback` block with the writer's own parser
    (`Get-DvPlaybackEvidence`) and requires the receipt's copy to equal it, field by field -- `logShaBound` and
    `settingsIsolated` are **derived, never read** -- and checks the evidence's own clip id, venue (`summary.display.venue`) and
    build (`manifest.buildManifest.sha256`), the verbatim `metrics`, the `subject.digest`, and the job's exit code. The
    manifest is optional only on a product-failure terminal (the job writes none); a capture without it is `INCOMPLETE`.
  * *What the hashed summary binds (round 2):* the **backend** is derived from `summary.json`'s own frame counters, never from
    `subject.backend` -- `cuda` needs `gpuFramesTotal > 0`; `cpu` needs `cpuFrames > 0` and `gpuFramesTotal == 0` and a `backend`
    field in the summary (a cpu run is always a variant job) -- a receipt whose backend disagrees is `INVALID`
    (`BACKEND_MISMATCH` / `BACKEND_NOT_DERIVABLE`), and the leg's criteria are selected by the **derived** backend. A product-failure
    terminal is read where the job *actually* writes its counters: `GPU_RECON_FRAMES_ZERO`, `CPU_FALLBACK_DETECTED` and
    `CPU_BACKEND_PATH_MISMATCH` carry them only in the nested `gpuSummary` (`gpuFramesTotal` is the sum of its recon-readback,
    texture-readback and texture-no-readback counts; preview frames never count) and no `backend` / `lookLeg` field, so the validator
    takes the leg's backend from the terminal itself (the two cuda exits 13 / 14 exist only on a cuda leg, exit 28 only on a cpu leg)
    and requires the counters to be the shape that terminal is written under -- a terminal whose counters contradict it is
    `BACKEND_NOT_DERIVABLE`. Its leg type is then *unstated* (`LEG_TYPE_UNSTATED` in `unbound`; the receipt is a `FAIL` derived from
    the terminal's own result token, never a PASS). `PRESENTMON_UNAVAILABLE` has **three** summary shapes: the display-report failure
    (a parse failure after the run) carries a top-level `gpuFramesTotal` and the nested `gpuSummary`; the **wait failure** (PresentMon did not
    exit within 35 s after playback) carries the same two fields since DVE-LEG-TERMINALS-1 (it carried neither before, so a leg that had
    played got **no receipt at all**: `BACKEND_NOT_DERIVABLE`, the runner exited 2); and the spawn failure (PresentMon never started, before
    the smoke run) has no run and so no counters. A summary that names no backend can never become an advisory `FAIL`, but it is no longer a
    refused write either: the runner asks `Get-DvBackendNotDerivable` before it chooses an outcome and ends the leg as a typed `INVALID`
    (no-signal) receipt that keeps its evidence. Tests use the exact summary shapes the job writes (`real_failure_summary`) and the
    Ultra-Magnus shapes of 2026-10-02. The **leg type**
    comes from `summary.lookLeg` (a look-run's evidence can no longer verify as the speed leg, nor the reverse: `LEG_TYPE_MISMATCH`),
    plus `lookAssistForced`, the look flavor and `declaredVenue` when the job wrote them.
  * *Typed UNBOUND claims:* `subject.clipContentSha256` is computed by the local generator and written to **no** artifact the venue
    returns, and `legId` / `legSpecSha256` name a committed spec that no hashed artifact carries (only the leg *type* is bound). They
    are recorded -- the validator's `unbound` list (`CLIP_CONTENT_UNBOUND`, `LEG_IDENTITY_UNBOUND`), `verification.unbound` in a
    written receipt, `unbound` in a sheet-pair record -- and are **never verdict inputs**: they are neither believed nor compared
    when pairing (the pair is keyed on the bound clip id, build and look flavor).
  * *Line endings:* the leg spec is identified by the sha256 of its bytes with CRLF folded to LF (`Get-DvLegSpecSha256`), on **both**
    sides of the committed-spec lookup (the runner hashes the working copy, the lookup hashes the committed blob). On this VM's
    default checkout (system git `core.autocrlf=true`) the working copy is CRLF while the blob is LF, and a raw-byte comparison would
    refuse every committed spec as `LEG_SPEC_NOT_COMMITTED`. `.gitattributes` also pins `legs/*.json`, `venue-clip-consent.json` and
    `venues.json` to `text eol=lf`; the consent file and the venue table are compared through git's own clean filters
    (`git hash-object`). Tests check out the real shipped files under `core.autocrlf=true` with and without the pin.
  * *Honest limits:* (1) the evidence files are local; nothing signs them on the venue, so a forger who can both commit a
    consent record **and** write a hash-consistent evidence directory (or just the evidence directory, once consent is legitimately
    committed) can still make a receipt re-derive -- which is exactly why a production receipt is advisory, never VERIFIED.
    (2) "Committed" is any commit of the repo, not "reviewed master" (`admission.headCommit` may be a dangling commit; fable's
    hardening `DVE-CONSENT-COMMIT-MUST-BE-REVIEWED-1`). (3) `clipContentSha256` and the leg id are UNBOUND (above). (4) A
    product-failure `FAIL` has no manifest, so its build binding is the job's own `summary.json`. A hand-built receipt, a placeholder
    hash, a self-asserted boolean, an empty consent blob, an uncommitted leg spec, a relabelled backend or leg, a line-ending
    difference and missing evidence never *re-derive*, and nothing re-derived is more than advisory.
* **Raw contact-sheet frames are listed too (round 2).** At capture time the runner hashes every raw frame and sidecar into
  `contact-frames.json` (`mlv-app/dual-venue-contact-frames/v1`, named in the receipt as `evidence.contactFramesJsonSha256`).
  A LOOK receipt's validation and `New-VenueSheetPair.ps1` take **only** the files that manifest lists (`Read-DvContactFrames`): an
  unlisted PNG or sidecar, a listed file that is missing or does not hash to its entry, a non-`*.png`/`*.json` name, a
  subdirectory or a reparse point is refused (`CONTACT_FRAME_UNLISTED`, `CONTACT_FRAME_HASH_MISMATCH`, `CONTACT_FRAMES_UNLISTED`),
  and the pair is composed from a staging copy of exactly the bytes that were hashed. Two receipts that share one evidence
  directory are refused. The pair record says `advisory: true`.
* **Queued, not done here:** `DUAL-VENUE-PASS-PROVENANCE-1` (venue-held provenance for a production PASS: design first -- a
  venue-agent signature over `summary.json`, or the validator re-reading `\\<venue>\mlv-agent\outbox` by `jobId`).
* Limit, stated plainly: a venue sitting is still the observation that a real run reaches its frames; nothing here plays
  the app, and no venue run has happened. Until DUAL-VENUE-PASS-PROVENANCE-1 lands, no production receipt counts as a PASS.

## Pieces

* `tools/profiling/dual-venue/venues.json` -- the venue table: share, agent root, scratch root,
  expected host name, health thresholds, per-card roles, `defaultRole`, and the owner-footage gate.
  **Changing a role is a reviewed commit, never a runtime flag.**
* `tools/profiling/bachelor/playback-attr-3-cuda-job.ps1` -- the job generator, now with
  `-Venue bachelor|ultra-magnus`, `-Backend cuda|cpu`, `-ScaleFactor`, `-CpuQuiescenceThresholdPercent`,
  `-ForceLookAssist`, `-LookFlavor`. With default arguments its output is **byte-identical** to before
  (pinned by `tools/repo_hygiene/test_dual_venue_evidence.py` against master's generator at 38ed2d8f, the
  merge that carried the clip-length enforcement). It also takes `-PlaySeconds` (master's) and returns
  `clipContentSha256` for the receipt subject.
* `tools/profiling/dual-venue/Invoke-VenueLeg.ps1` -- runs one leg on one venue and **always** writes a
  receipt. `DualVenueRunner.psm1` holds its testable rules.
* `tools/profiling/dual-venue/New-VenueSheetPair.ps1` -- composes the side-by-side cuda|cpu contact sheet
  for a LOOK leg from two **advisory** receipts (`make-contact-sheet.py --pair-dir`, paired by frame index; a diagnostic sheet,
  never a PASS). **A sheet shows only the frames this run captured and the manifest lists** (DUAL-VENUE-EVIDENCE-3): a listed sidecar
  may name no image but its own listed `<stem>.png` (`CONTACT_FRAME_SIDECAR_PATH` at validation; the app writes exactly that name), the
  pair stages the hashed bytes and hands the composer their sha256 (`--left-listed` / `--right-listed`), and the composer reads only
  those staged files, verifies each hash at read time (`PAIR_FRAME_HASH_MISMATCH`), refuses an unlisted one (`PAIR_FRAME_NOT_LISTED`)
  and refuses an absolute, parent-relative or other-frame `path` (`PAIR_SIDECAR_PATH_OUTSIDE_STAGING`) -- it never falls back to a file
  outside the staging directory, so an edited or stale PNG elsewhere on the VM cannot appear under a receipt's backend label.
* `tools/profiling/dual-venue/leg-spec.schema.json` and `legs/*.json` -- the leg specs.
* `Get-VenueEvidence.ps1` (the reader; built by DUAL-VENUE-RECONCILE-1) -- reads receipts; acceptance
  reads only `-AcceptanceFor <card>`.

## Backends: every playback leg runs as cuda AND cpu on every venue

A speed or look leg has `backends: ["cuda","cpu"]`. `-Backend cpu` omits every `MLVAPP_GPU_*` /
`MLVAPP_EXPERIMENTAL_GPU_*` environment variable, requires `CPU_FRAMES > 0` and zero GPU frames
(a leg that reached a GPU path is `CPU_BACKEND_PATH_MISMATCH`, exit 28), never fires the CUDA-only
exits 13/14, and treats PresentMon as informational (a PresentMon wait failure included: see *CPU legs and a PresentMon wait failure* below). **CPU frame rate is informational** -- it tracks
cores and storage, not the product's GPU work -- and never gates a card. That includes the smoke runner's own
playback-quality limit on the **skipped/unpresented-frame ratio** (default 50 %): on Ultra-Magnus the cpu leg of 2026-10-02 was
failed by it (`SMOKE_RUN_FAILED`, exit 18, 56.17 % > 50 %) with no frame and no sheet. A cpu job passes the runner's own
`-MaxSkippedOrUnpresentedRatio` at its ceiling (1: the ratio cannot exceed it) so the run is never failed on that ratio, and the
ratio is recorded as the measured `skippedOrUnpresentedRatio` field of its success summary (a leg spec's cpu criteria may then
read it, informationally). A cuda job's command is unchanged: its 50 % gate holds. Nothing else is relaxed -- clip length,
no-loop / no-replay, the run nonce, settings isolation and the backend check are enforced by the runner and the receipt oracle,
and the job passes no switch that could loosen them. `backend` is part of the
subject, so a cuda receipt and a cpu receipt are different subjects.

## LOOK legs and contact sheets

*Contact frames after a PresentMon wait failure (DVE-LEG-TERMINALS-1).* The app captures the contact-sheet frames in a **seek** pass
(`--contact-sheet-seek-mode`: it never plays) inside the smoke child, after the measured session's own summary line, and PresentMon is
only waited on once that child has returned. A PresentMon wait failure therefore leaves the captured frames on the venue with nothing to
re-run; the job now publishes them (`contact-sheet\raw`) plus a `compose-status.txt` marker (`CONTACT_SHEET_COMPOSE_UNAVAILABLE ...`: the sheet
is not composed on the venue, whose labels need the eligibility verdict this branch exits before). The runner keeps the frames and
lists them by sha256 in `contact-frames.json`, so `New-VenueSheetPair.ps1` composes the cuda|cpu pair locally from an advisory `FAIL`
receipt. Nothing measured changes: the publish runs after the smoke child, after the run log and counters are read, and writes only
under `contact-sheet\`. The other failure terminals (`GPU_RECON_FRAMES_ZERO`, `CPU_FALLBACK_DETECTED`, ...) still publish raw frames
**without** the marker, so the runner keeps none of them: their frames were drawn by a path the leg's backend label does not describe.
The wait-failure branch applies the same rule to its own frames (DVE-WAIT-FAILURE-FRAMES-BACKEND-GATE-1): it publishes them, and the marker, only when the run's
own counters do not contradict the leg -- a cuda leg needs gpu frames and no cpu frame, a cpu leg the inverse -- so a leg that fell back (partly or wholly) never
lists frames that `New-VenueSheetPair.ps1` would label with a backend the run did not use. Counters that are unavailable do not contradict the leg (that receipt
is a typed `INVALID`, never a labelled PASS/FAIL).
*Contact frames after a display-report failure (DVE-PRESENTMON-EVIDENCE-1).* The display-report failure of `PRESENTMON_UNAVAILABLE` (PresentMon's CSV could not be
parsed or does not exist -- Ultra-Magnus, 2026-10-03: "PresentMon output does not exist" after a clean, immediate stop) now publishes the frames the app already
wrote, and the same `compose-status.txt` marker, under the **same counter gate** as the wait-failure branch (a cuda leg needs gpu frames and no cpu frame; the
cpu variant the inverse). Before it, the UM-2 owner CUDA leg had six frames on the venue and none on the leg. Only the typed `PRESENTMON_UNAVAILABLE` terminal
publishes; `DISPLAY_ASLEEP` (a verified-zero display result) keeps its own receipt shape and lists no frames.

*A PresentMon failure says why (DVE-PRESENTMON-EVIDENCE-1).* PresentMon used to run hidden with no captured streams. Its stdout and stderr are now redirected to
files beside its CSV and published with the artifacts as `presentmon-stdout.txt` / `presentmon-stderr.txt` (bounded: a stream over 64 KiB publishes its tail under
one first line that says so; an empty stream is published empty, a missing one is reported missing). `presentmon-capture.json` records `csvEverExisted` (the CSV was
seen while the launch waited for trace readiness, or exists at stop), `csvSizeAtStop` (null when absent) and a `streams` record (exists, bytes, published, truncated,
error), so "the stop was clean and the CSV never appeared" is a fact in the evidence. A PresentMon that exits at startup (rc=6, ETW access denied) publishes the
same streams from the spawn-failure summary (`presentMonStreams`).

*A leftover PresentMon session is swept before a capture (UM-PRESENTMON-ORPHAN-SWEEP-1; that `--terminate_existing_session` ends a consumer-less orphan is not yet proven on the venue -- the logman fallback and `remaining` cover a miss).* On Ultra-Magnus an orphaned default-named `PresentMon` ETW session made every
other-named session lose all of its events (15-17k "ETW events were lost" per 12 s), so no CSV was written; the per-job `--session_name` meant `--stop_existing_session` no
longer cleared it. Before the spawn the job now terminates the default `PresentMon` session and every `MLVAttr3-*` session that `logman query -ets` lists, through the pinned
PresentMon's own `--terminate_existing_session` (`logman stop <name> -ets` only for a session still listed afterwards) -- but only when no PresentMon process is alive on the host
(checked before the listing and again right after it); otherwise it records that it did not run. A job-issued `Kill()` is followed by the terminate of the job's own named
session (both stop paths). `presentmon-capture.json` carries `orphanSweep` (ran, skippedReason, live pids, listed, matching listing lines, every action with its exit code,
what remains) and `eventsLost` (PresentMon's stderr reported lost events: message count and largest count); the failure terminals' PresentMon reason gains a typed
`PRESENTMON_EVENTS_LOST` detail, so a recurrence reads as its cause instead of "output does not exist".

The sweep can never stop a live capture or a session it did not list as an orphan (round 2): it acts only on the names listed before its second liveness check -- a name a
later listing shows (another job's capture starting meanwhile) is recorded as `newlyListed` and left alone -- and it re-checks that no PresentMon process exists immediately
before **each** terminate and **each** `logman stop`; the first process that appears ends the sweep (`abortedReason` / `abortedBefore`, with `remaining` still derived from a
final listing). A failing `logman query -ets` is asked for once more (`listAttempts`), a failed middle listing is a recorded verification failure, and every `logman` call runs
under a 15 s deadline (`timedOut` in the action record). The post-`Kill()` session terminate is published, not only run: `presentMonPostKillSessionTerminate` (exit code,
timed out, error) in the KEEPALIVE_FAILED, SMOKE_RUN_FAILED and SMOKE_LOG_UNAVAILABLE summaries, in the trace, and as `postKillTerminate=` in the `PRESENTMON_TIMEOUT` reason,
so a cleanup that failed is visible on the job that caused it. PresentMon's redirected stderr is UTF-16LE with a BOM (the diagnostic's real files); the lost-events scan reads
UTF-8, UTF-16LE and UTF-16BE, with or without a BOM, and keeps its 256 KB tail on a code-unit boundary.

*CPU legs and a PresentMon wait failure (DVE-PRESENTMON-EVIDENCE-1, hardening DVE-CPU-PRESENTMON-WAIT-GATES-1).* PresentMon is informational on a cpu leg, so a cpu job
does **not** take the wait-failure terminal: the run continues through its own gates (backend, source frames, playback), the summary carries `presentMonStatus: "unavailable"`
with the wait failure's reason, and the outcome comes from the leg's own gates. A cuda job is unchanged: its wait failure is still the typed `PRESENTMON_UNAVAILABLE` terminal.

A `legType: "look"` leg takes `look.contactSheetFrames` evenly spaced frames on each backend with
**Look Assist forced on** (the smoke runner is told Look Assist is *required*, so a leg where it did not
apply fails closed; an automation run reads a run-scoped settings store, so the venue's persisted settings are
never inherited). The contact-sheet capture is seek-mode (no Play of its own): the leg's single measured Play
is the one that covers >= 20 s.
The job composes a one-backend sheet; `New-VenueSheetPair.ps1` then pairs the two backends' raw frames
into one `cuda | cpu` sheet, by frame index. Each receipt's `look` block carries the sheet's sha256 and
path; the pair is its own record (`mlv-app/dual-venue-sheet-pair/v1`) because receipts are never edited.

* `lookFlavor` (`classic` default | `cinematic`) is in the leg spec and the receipt subject and is passed
  to the app as `MLVAPP_LOOK_ASSIST_FLAVOR`. The app reads it (LOOK-ASSIST-FLAVORS-1,
  [docs/look-assist-flavors.md](look-assist-flavors.md)) and reports the flavor it applied on
  `gui_smoke.visual_state` (`look_assist_flavor`). The job copies that report into its summary as
  `lookFlavorReported`; the receipt's `look` block carries `lookFlavorReported` and
  `lookFlavorHonored` = the report equals the requested flavor (`true`), anything else is `false` (another
  flavor, or an app that reported nothing = `none`). `"unknown"` is only for a job that never ran. The sheet
  pair's `lookFlavorHonored` is `true` only when both legs' receipts say `true`.
  **No cinematic leg spec ships yet** (DVE-SCALE2-LOOK-LEG-1 held it back until the app could select and report a flavor);
  with the app now reporting the flavor, a cinematic leg is a follow-up spec card, not part of this change.
* Shipped look legs: `m16-1243-look` (Classic, requests scale 4, cuda and cpu) and `m16-1243-look-scale2` (the Classic leg at
  `scaleFactor` 2, **cpu only**). See "Requested scale and rendered scale" below for why the scale-2 leg has no CUDA backend.
* The sheet copy (`-SheetCopyDir`) and the pair files carry the **leg id** --
  `sheet-<legId>-<venue>-<backend>-<flavor>.png`, `sheet-<legId>-<venue>-cuda-vs-cpu-<flavor>.png`,
  `sheet-pair-<legId>-<venue>-<flavor>.json` -- so the scale-4 and scale-2 look legs of one venue cannot overwrite
  or refuse each other. A pair record also carries `cudaScale` / `cpuScale` (requested and rendered) and `scalesDiffer`.
* Aesthetics are **model-judged** (Amendment 2). Receipts carry `owner_verdict: null` (optional, never
  waited on, never written by the runner) and `model_verdicts: []` (filled by the judge card).
* **Sheets of owner footage stay local under `.claude-state`** (never committed, attached to a PR, published to
  the bus or as an artifact) and need the per-venue owner CLIP line. Since fixtures are
  never venue playback clips, there are no fixture sheets any more.

## Requested scale and rendered scale

A leg's `scaleFactor` is the playback scale it **requests**. It is not necessarily the scale the app **renders** at: the CUDA texture route
clamps every requested scale other than 1 to 1 (`platform/qt/MainWindowGpuPreviewPolicy.h`, pinned by `tests/gui/test_gui_smoke.cpp`), so a
CUDA run of a scale-4 or scale-2 leg renders at scale 1. Three production receipts of `m16-1243-look` on CUDA show it: the app logs
`playback_scale_clamped_for_gpu_texture_route requested=4 effective=1` and `scale_active_last=1`; the cpu runs show `scale_active_last=4`.

* **Every receipt carries a `scale` block** (also on a refusal, where the rendered scale is `UNKNOWN`): `requestedScale` (the spec's), `effectiveScale`
  (what the app rendered at, read from its own run log: `scale_active_last` on the measured session's summary line, else the effective= of the clamp
  line, else the string `UNKNOWN` -- never the request, never 1), `acceptedEffectiveScale`, `effectiveScaleSource` and a `verdict`.
* **A leg cannot PASS or FAIL under a scale it did not render at.** A capture whose rendered scale is not the accepted one -- or cannot be read -- ends
  `SCALE_NOT_HONOURED` (no signal, like `INVALID`), and `outcomeDetail` says both numbers. `Test-DvReceiptValid` re-derives the same verdict from the hashed
  run log and the committed spec, so a hand-written PASS over a clamped run is `INVALID` (`OUTCOME_NOT_DERIVED`).
* **A spec may declare the clamp**: `acceptedEffectiveScale.<backend>` (schema: optional, per backend) names the scale that backend is known to render at when it
  is not `scaleFactor`. `m16-1243-look` and `m16-1243-speed` declare `cuda: 1`, so their CUDA legs stay runnable: they pass only when the app rendered at exactly 1, the
  verdict is `DECLARED_CLAMP` (never `HONOURED`), and the outcome detail reads "requested scale 4, rendered at 1". A CUDA look sheet of the scale-4 leg is therefore a scale-1 sheet,
  labelled as such, beside a scale-4 cpu sheet; the pair record's `scalesDiffer` says so. If the route changes and cuda renders 4, the declaration is stale and the leg ends
  `SCALE_NOT_HONOURED` until the spec is edited -- it never silently passes. (The speed leg's declaration is derived from the code and the look-leg receipts: no speed-leg
  receipt was available to measure.)
* **`m16-1243-look-scale2` is cpu only** and declares nothing: the owner's display-meter exposure path runs only when the *effective* playback scale is 2
  (`MainWindow.cpp`, `effectivePlaybackScaleFactorForRequest() == 2`), which the CPU backend reaches and the CUDA texture route cannot. The leg shows the owner's scale-2 playback
  look on the CPU backend only; it gets a CUDA backend when the CUDA route honours scale 2 (a unit test trips when the clamp is removed, so the declarations and this backend list are revisited).
* The job is generated with `-ExpectedScaleRequest <accepted scale>` so the smoke runner's own scale check is live (`-UsePersistedPlaybackSettings` leaves it at -1 otherwise). The
  runner compares the app's request after the clamp, so the value is the accepted *effective* scale, not the spec's request.

## How to add a leg

1. Copy `legs/m16-1243-speed.json` (or `m16-1243-look.json`, `m16-1243-look-scale2.json`); give it a new `legId`, the `card` that needs the
   evidence and a **consented clip id**. The file validates against `leg-spec.schema.json`. The id needs an
   owner-typed record for each venue that will run it (see "Long clips only").
2. Declare the **roles before the first byte** (kernel K5): add the card to `venues.json` `roles`
   (`bachelor: acceptance`, `ultra-magnus: supplementary` for a card whose acceptance venue is Bachelor).
   A card the table does not name gets `defaultRole` (`supplementary`) on both venues.
   A `scaleFactor` other than 1 on a `cuda` backend needs `acceptedEffectiveScale.cuda: 1` while the texture-route clamp exists (a test enforces it); otherwise list `cpu` only.
3. Put the pass criteria per **role** and per **backend** in `criteria`: `{metric, op, value}` triples
   over the metrics copied verbatim from the job's `summary.json` (`rows`, `gpuFramesTotal`, `cpuFrames`,
   `lookAssistForced`, `presentMon*`, ...). An empty list is informational. A metric the job did not
   write *fails* the criterion -- a missing number is never a passing number.
4. `legSpecSha256` is the sha256 of the file's bytes with line endings normalised (CRLF -> LF), so editing a leg makes a new
   subject and a CRLF checkout does not.

## How to run a leg on a venue

Stage the **same build** on the venue first (assemble once; stage with `playback-attr-3-cuda-stage-job.ps1`
and `attr3-stage-smoke-runner-job.ps1` using `-AgentRoot` from `venues.json`). The runner refuses --
`DEVICE_UNAVAILABLE` -- to substitute a different build. The consented clip is resolved and verified by the job
at the venue, by id; the runner never stages, opens or names it.

```powershell
pwsh -NoProfile -File tools\profiling\dual-venue\Invoke-VenueLeg.ps1 `
    -Venue ultra-magnus -LegSpec tools\profiling\dual-venue\legs\m16-1243-look.json `
    -SourceCommit <40-hex> -BuildManifestSha256 <64-hex of the staged build.json> -Backend cuda
```

A refused leg (fixture id, no consent record for this venue, window under 20 s, an unresolvable id) writes a
receipt with `refusal: <TOKEN>` and `DEVICE_UNAVAILABLE` (no signal) and submits nothing.

Add `-HealthOnly` to probe a venue and stop (the receipt is `UNRESOLVED` -- nothing was measured).
The last lines print `DVE_OUTCOME=`, `DVE_DETAIL=` and `DVE_RECEIPT_PATH=`. **Read the receipt, not the
exit code**: exit 0 means "a receipt was written", exit 2 means the receipt itself could not be written.

The runner no longer snapshots the venue's registry (round 3): master gives an automation run its own run-scoped settings
store, so the app neither reads nor rewrites the venue's `HKCU\Software\magiclantern.MLVApp`. The receipt proves it from the
run log (`playback.settingsIsolated`; a log that does not say `settings_store=run_scoped` is `INVALID`,
`SETTINGS_NOT_ISOLATED`, which also refuses a build that predates the isolation). `registry` is kept as a null field so a
round-2 reader still parses. `-ReceiptRoot` (production) and `-SheetCopyDir` must sit under a `.claude-state` directory: a
receipt, its evidence and a contact sheet name or show an owner clip's run and stay local.

Receipts land in `<main checkout>\.claude-state\dual-venue\receipts\<card>\<legId>\<venue>\<receiptId>.json`,
append-only (created with `CreateNew`; a repeat is refused, never overwritten). Copied evidence
(`summary.json`, `evidence-manifest.json`, the launcher's `result.json`, `logs\smoke-run.log`, `um-run.json` -- each sha256 named in
the receipt -- the contact sheet, and, on a failed smoke run, `smoke-stderr.txt` / `smoke-stdout.txt`) is under `.claude-state\dual-venue\evidence\<receiptId>\`, which in production must sit under
`.claude-state` too (the run log and result name an owner clip's run; they stay local like the receipt).

## How to read the evidence

* **Acceptance** reads only receipts whose `venue.role` is `acceptance` for the card, and only `PASS`/`FAIL`.
  A venue that was unhealthy, not quiescent, unreachable or a mismatch leaves the card *open*, not failed.
* **Supplementary** evidence informs diagnosis -- fails on both: code; fails only on Bachelor: the venue --
  but never closes a card.
* **Every leg runs an owner clip** (fixtures are refused), so every leg needs the owner's CLIP line for that venue
  (consent on one venue never implies the other); the owner-footage cleanup switch is already on. Until the line is
  committed the runner refuses before submitting anything and writes a receipt with
  `refusal: VENUE_CLIP_CONSENT_ABSENT` (or `OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2` should the switch ever be turned
  back off).
* **`INVALID` is not a failure of the product**: the run could not show >= 20 s of distinct source frames (or
  the proof is absent). It carries no signal; the card stays open. Fix the venue or the clip and re-run.

## Outcome mapping (job `RESULT=` token -> receipt outcome)

| Job result | Outcome |
|---|---|
| `MEASUREMENT_CAPTURED` (exit 0, valid oracle verdict, the job's own `sourceFrames` block) | `PASS` or `FAIL` from the role/backend criteria |
| `MEASUREMENT_CAPTURED` whose rendered playback scale is not the leg's accepted one, or cannot be read from the run log (checked before everything below) | `SCALE_NOT_HONOURED` (no signal; both numbers in `outcomeDetail`) |
| `MEASUREMENT_CAPTURED` for a LOOK leg whose venue could not compose the contact sheet (`CONTACT_SHEET_COMPOSE_UNAVAILABLE`: no Python + Pillow) | `VENUE_TOOLING` (raw frames kept locally; a venue condition) |
| `MEASUREMENT_CAPTURED` with a non-zero exit, `FIXTURE_REHEARSAL_CAPTURED`, `SOURCE_FRAMES_INVALID`, or a smoke refusal of the length class (`PLAY_WINDOW_TOO_SHORT`, `CLIP_TOO_SHORT`, `INVALID_LOOPED`, ...); or a PASS/FAIL whose receipt lacks a valid oracle verdict | `INVALID` |
| `VENUE_NOT_QUIESCENT` | `VENUE_NOT_QUIESCENT` |
| `VENUE_HOST_MISMATCH` | `VENUE_HOST_MISMATCH` |
| `BACKEND_NOT_AVAILABLE` | `DEVICE_UNAVAILABLE` |
| `DISPLAY_ASLEEP`, `KEEPALIVE_FAILED`, `SCREENSAVER_SECURE_OWNER_ONLY`, `DISPLAY_WAKE_DISMISS_FAILED` | `VENUE_UNHEALTHY` (a venue condition, not a product result) |
| a product failure the job reaches AFTER it published the run log (`GPU_RECON_FRAMES_ZERO`, `CPU_FALLBACK_DETECTED`, `CPU_BACKEND_PATH_MISMATCH`, `PRESENTMON_UNAVAILABLE`) **with a valid receipt-oracle verdict re-derived from that log** | `FAIL`, with the token in `outcomeDetail` (a production receipt for it is written as an **advisory** `FAIL`: its backend is derived from the summary's nested `gpuSummary` counters and its leg type is `LEG_TYPE_UNSTATED`) |
| any terminal with no run log or a log that does not prove >= 20 s (`SMOKE_LOG_UNAVAILABLE`, or one of the failures above on a short, wrapped, foreign or overridden run) | `INVALID` (no proof, no signal) |
| a PASS/FAIL whose hashed `summary.json` names no backend (e.g. a `PRESENTMON_UNAVAILABLE` whose log has no gpu summary line, or the spawn failure) | `INVALID` (`BACKEND_NOT_DERIVABLE`: the leg ran, so it ends in a typed no-signal receipt, never a refused write) |
| any PASS/FAIL the production validator refuses for any other reason (e.g. `BACKEND_MISMATCH`: a cuda leg whose run fell back entirely to the cpu path and then lost its PresentMon wait, or a cpu leg whose run reached a gpu path) | `INVALID`, written by the runner (`Complete-Receipt`) with the validator's reasons in `outcomeDetail` and the evidence kept: a leg that ran never ends without a receipt. Only a write that fails for another reason (an unwritable path, a repeat receipt id) still ends as `DVE_RECEIPT_WRITE_FAILED`, exit 2 |
| `SMOKE_RUN_FAILED` (the smoke runner exited non-zero: a validation gate, a launch failure) -- **in either backend, with or without a run log** | `INVALID`, with `smoke exit <n>` and which pieces of evidence are kept in `outcomeDetail`. The job publishes the runner's **full** `smoke-stdout.txt` / `smoke-stderr.txt`, the launcher's `result.json` (its `validation` block lists every failed check) and the run log (`logs\smoke-run.log`, or `logs\smoke-failed-app.log` when the run wrote no `result.json`); the runner keeps them beside the other evidence and names the two streams by sha256 in `evidence.smokeStderrSha256` / `smokeStdoutSha256`. `summary.smokeEvidence` says which pieces exist and why one does not |
| um-run `RETRACTED` / `UNRESOLVED` | `RETRACTED` / `UNRESOLVED` |
