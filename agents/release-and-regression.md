# Release build verification and output-regression prevention (detail)

Demoted verbatim from [`CLAUDE.md`](../CLAUDE.md) under [docs/22-doc-fragmentation-policy.md](../docs/22-doc-fragmentation-policy.md). Headings are unchanged and remain the stable IDs referenced from the parent index.
## GUI Release Build Verification
- **Playback-quality evidence is fail-closed.** `-AllowZeroPresentedFrames` is valid only with the explicit `-LaunchOnlyProbe` declaration. Launch-only probes may not be used for performance, cadence, lifecycle stress, screenshots, artifact detection, A/B comparison, or product-card completion. The GUI-smoke wrapper rejects those combinations before launch.
- A playback-quality leg must independently pass before it can enter an A/B: at least two frames presented, first/last presented frame ids differ, and the skipped/unpresented ratio stays within the configured bound. A failed baseline is not a comparator. Timeline FPS and the bottom-left GUI status are diagnostic only; neither is playback proof without advancing presented frames.
- Product-card playback review must cite the raw baseline and candidate validation results and state `presented_frames`, first/last presented ids, skipped/unpresented ratio, and presented FPS. For visual/stale-present risk, attach timestamped screenshot or filmstrip evidence and verify displayed content advances. A reviewer may not relabel a zero-present result as a counter blind spot; the only allowed disposition is `CHANGES_REQUESTED` until the presentation path or harness is fixed.
- Fable hub gate-cover may coordinate or reproduce evidence, but must not create an exception to a blocking playback-quality invariant and approve evidence that depends on that same exception. Product-card completion requires a dedicated independent reviewer when the hub has proposed or adjudicated an evidence-policy exception.
- Contract regression test: `pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tools\profiling\test-playback-quality-contract.ps1 -RepoRoot .`.
- After any source, UI, receipt, playback, color, scaling, or processing change that is meant to affect the Windows GUI, rebuild the user-facing release tree before final response:
  - `pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$env:PATH='C:\Qt\Tools\mingw1310_64\bin;C:\Qt\6.10.2\mingw_64\bin;' + $env:PATH; & 'C:\Qt\Tools\mingw1310_64\bin\mingw32-make.exe' -C platform\qt\build-release -B release -j4"`
- In Codex Desktop's PowerShell shell, the safer equivalent is:
  - `$env:PATH='C:\Qt\Tools\mingw1310_64\bin;C:\Qt\6.10.2\mingw_64\bin;' + $env:PATH; & 'C:\Qt\Tools\mingw1310_64\bin\mingw32-make.exe' -C platform\qt\build-release -B release -j4`
- Do **not** treat `.claude-state\build\mlvapp\release\MLVApp.exe` as the user-facing GUI build. That scratch build is useful for validation, but the executable the user normally launches is `platform\qt\build-release\release\MLVApp.exe`.
- After rebuilding, verify and report the actual release executable path, `LastWriteTime`, length, and SHA256:
  - `Get-Item platform\qt\build-release\release\MLVApp.exe | Select-Object FullName, LastWriteTime, Length`
  - `Get-FileHash platform\qt\build-release\release\MLVApp.exe -Algorithm SHA256`
- If the release build is intentionally skipped or cannot run, say so explicitly in the final response and explain what executable remains stale. Never imply a GUI-affecting change is ready for user testing unless the `platform\qt\build-release\release\MLVApp.exe` timestamp has moved after the change.
- For app-backed `MLVApp.exe --profile-playback` or visual smoke runs against the user-facing release tree, use:
  - `pwsh.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tools\profiling\run-release-playback-profile.ps1 -RepoRoot . -Input <clip.mlv> -Output .claude-state\profiling\<run>\<clip>.json`
- For screenshot-backed playback/color/scaling smoke runs, use `tools\profiling\run-release-gui-smoke.ps1` against `platform\qt\build-release\release\MLVApp.exe` with `-CaptureScreenshot`, `-FrameTelemetry`, and `-ScreenshotOutputDir .claude-state\profiling\<run>\screenshots`.
- When benchmarking playback GUI changes, always inspect both pre-settle/in-flight and settled screenshot-backed smoke captures for color artifacts. Treat new magenta/pink/green bars, tinted blocks, or other frame-local color corruption as a validation failure even if FPS telemetry improves.
- The standard local M16 visual smoke set is `C:\temp\MLV\M16-1327.MLV` for the hot/warm green-cast regression, `C:\temp\MLV\M16-1347.MLV` for bright neutral/green-clamp behavior, and `C:\temp\MLV\M16-1446.MLV` for the flatter night/noise-floor case. Use `C:\temp\MLV\M16-1243.MLV` as the optional control clip for Look Assist cap behavior when the change touches receipt, default processing, or Look Assist policy.
- For visual verification, prefer the app-internal presented-frame screenshot rather than a viewport/window screenshot with letterbox bars. Viewport screenshots are acceptable only as a fallback artifact and must not be treated as an aspect-ratio or color oracle.
- When aspect ratio is under review, report `stretch_x`, `stretch_y`, `h_stretch_index`, `v_stretch_index`, and whether the screenshot is a presented-playback geometry check or a neutral source-aspect check. The M16 smoke set may intentionally preserve receipt/app stretch, so do not call a screenshot correct or wrong without the active stretch state.
- `tools\profiling\run-release-gui-smoke.ps1` records screenshot `width`, `height`, `aspect`, `sha256`, and `visualQuality.aspectEvidence`; use that object as the first-line aspect evidence before making a visual judgment.
- When reporting playback smoke metrics, always include both milliseconds and FPS or FPS-equivalent (`1000 / ms`) for every timing number mentioned. Do this even when the timing is not part of a direct A/B comparison, so readers never have to convert ms to cadence mentally.
- Keep playback FPS labels explicit: `GUI FPS` is the bottom-left `Playback: ... fps` status value, `smoke presented FPS` is `presented_fps`, `timeline FPS` is `timeline_fps`, and per-stage timing conversions are only `FPS-equivalent` (`1000 / ms`).
- Do **not** force `QT_QPA_PLATFORM=offscreen` when profiling or smoking `platform\qt\build-release\release\MLVApp.exe`. That release tree deploys `platforms\qwindows.dll`; forcing `offscreen` can trigger the Qt platform-plugin popup even though the normal GUI launch path is healthy. `offscreen` remains appropriate only for `gui_tests` or for an explicitly deployed offscreen platform-plugin tree.


## Output-Regression Prevention -- "behavior-preserving" is the HIGHEST-risk class

Three output regressions (per-clip color casts, dark/flat exposure, jerky playback) shipped over
weeks because each was reviewed as a "behavior-preserving / byte-identical / perf / refactor /
scheduling / proxy" change, and every gate checked internals/aggregates -- never the rendered
OUTPUT on the user's real footage versus a frozen known-good build. Full program + per-regression
gates + residual risks: `docs/regression-prevention-program.md`. Binding rules:

- A change whose intent or subject is **behavior-preserving / byte-identical / refactor / perf /
  scheduling / proxy / no-op** is the **highest-risk** class. It may not merge without an attached
  **baseline-A/B PASS** on the real named clips (M16-1327 museum, M16-1347 atrium, M17-1207 street,
  M15-1320 shuttle, M16-1210 crowd, M16-1243/M02-1344 pool) versus the pinned known-good build --
  output-equivalence **proven, not asserted**. This automates the manual June-9 A/B that was the
  only thing that ever caught these.
- **The reference is the known-good BUILD, never a same-codebase proxy.** A behavioral proxy you
  assume is equivalent-and-correct — an alternate mode/path (a "sync" fallback, a "reference"
  render), or a scalar (FrameGreenAxis, FPS, a determinism spread) — can carry the **same** root
  defect, so "make the candidate match the proxy" converges on the bug, not the fix. At the START of
  any regression hunt (color, exposure, smoothness, or unknown-shape), build/locate the last-known-good
  baseline and A/B the real OUTPUT against it FIRST, before forming a root-cause theory; "matches the
  known-good build" is the gate, not "matches a proxy"; and treat any oracle not independently grounded
  against that build as a suspect. (2026-06-27: a full night was lost converging the async auto-WB onto
  `MLVAPP_LOOK_ASSIST_SYNC=1`, which shared the same isolated-render dual-ISO defect and was itself
  magenta — the real cause only surfaced once we A/B'd against the Jun-9 build directly.)
- Three altitudes: **VALUES** (applied WB + presented color + luma percentiles; the WB-locked
  `--no-look-assist` + committed-receipt legs are BLOCKING, the Look-Assist-ON legs ADVISORY at
  ~3x measured spread) -- **PIXELS** (settled-frame perceptual diff, human-visible backstop) --
  **CADENCE** (p90/p99/hitch DELTA vs the frozen distribution, median-of-N, **ADVISORY only, never
  allowed to mute the blocking color/exposure legs** -- a muted cadence gate is how the jerky
  regression shipped).
- Any change to a playback/processing **DEFAULT** (e.g. Preview Resolution = Auto) is a behavior
  change and must fail a tracked `tools/gates/shipping-defaults.json` equality test until the golden
  is re-blessed.
- The exposure A/B must read the **8-bit present buffer** (`getMlvProcessedFrame8Scaled` /
  `rgb8DisplaySource`), NOT `MlvPipelineFixture.renderFrame16Scaled` (it renders full-res regardless
  of scale and is blind to the half-res proxy). Recon same-build ratio gates are blind to
  equal-both-legs darkening -- assert an ABSOLUTE luma-mean vs golden.
- The one corpus-independent absolute (deterministic, BLOCKING, no clips): an auto-WB
  fixed-point property test -- a near-neutral target renders neutral AND a second measure is already
  neutral (single-pass code cannot satisfy it; it fails the dropped refinement loop directly).
- Bless tooling refuses dirty/unstamped exes and requires `-Reason`; re-blessing a golden needs a
  reviewed before/after. **Human approval remains mandatory today.** It may be replaced by the exact
  autonomous quorum defined by `docs/autonomous-golden-authority.md` only after a separate reviewed
  activation commit pins and proves the installed fail-closed verifier, signer registry and
  revocation state, immutable baseline trust root, one-use receipt ledger, recoverable broker
  transaction, and hosted shadow evidence. The proposing lane is always recused, objective hosted
  output evidence has an unconditional veto, and ambiguous changes without a standing bounded
  policy are rejected while preserving the old golden. Human intervention may reject, safety-close,
  or roll back; it cannot promote after an objective veto. Re-bless laundering
  (greenlighting a regressed build by regenerating the golden) is the worst failure mode -- guard it
  structurally.

### September 8 synthetic GUI reference adjudication

The owner explicitly delegated the prepared two-reference decision to the hub
and adversarial low-inference agents, directing that execution continue without
another owner question. Two independent Luna reviewers approved the bounded
correction; the Fable hub returned `APPLY_EXACT_TWO_HASHES`. This owner instruction
supersedes the human-approval requirement above for this exact proposal only.

- Keys: `gpu.viewport.rgb888.pattern_nearest` and
  `gpu.viewport.rgb16.pattern_nearest` in `tests/fixtures/golden/gui_hashes.json`.
- Old value for both: `48758a9c5b87ce14ef78aab9e69e8624a8a9ed841af80df84c352f837f6dfe63`.
- Approved value for both: `817ca20ee0f7bc67e467002d38a3f4c9cd915035c4a5a4a38e038e6230b08ae6`.
- Independent derivation: the old hash encodes vertical inversion and a 4x4 to
  5x5 inclusive-edge expansion followed by trimming; the new hash encodes the
  original 4x4 pixels with the unchanged hash header. Exact pixel comparisons
  precede these hashes. Geometry and U16 endpoint regressions have preserved RED
  evidence; the corrected native suite failed only these two old hashes.
- Only the prepared patch was applied. The unchanged native executable then
  passed all 42 tests with no failures or skips; offscreen passed 33 with nine
  native GL skips. A clean app build and Bachelor playback validation remain
  required afterward. Failed outputs, proposal and adjudication receipt are
  preserved.
- No other golden, tolerance, shipping default, known-good real-footage baseline
  requirement, or merge gate changes. This does not activate the general
  autonomous golden-authority infrastructure.

Evidence: `.claude-state/project-audit/20260907/pr72-synthetic-golden-proposal.md`,
the adjacent patch and adjudication receipt, and
`.claude-state/fleet-runs/pr72-synthetic-hub-adjudication-fable-20260908-01/fable-001.last.txt`
(SHA-256 `4a6d08a2daa82353b469b6cdc392b5470b50a66ee3c87f6533ad7ca866b8edb1`).
