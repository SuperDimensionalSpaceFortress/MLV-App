# GPU Lane — Playback & Export Roadmap + UX (detail history)

> **DETAIL-ONLY / NOT QUEUE AUTHORITY.** This document preserves GPU design,
> measurements, and proof history. Current priority and status live only in
> [the implementation roadmap](roadmap.md). If the two conflict, follow the
> canonical roadmap and use this file only as evidence for its selected item.

Status: 2026-06-19. Lane A E0-E2 export, the scoped Lane A E3 GPU
replacement proof packet plus trusted/lossless throughput gates, P-pre GPU
AMaZE/processing parity, and Lane B P1-P3/P4 are now through their scoped proof
gates. P3/P4 are honest-scoped, not universal:
the RTX 4090 FastProxy proof validates the raw-fixes-enabled HQ Dual ISO
no-readback CUDA-to-GL R16 texture path with GL/backend/oracle parity;
unsupported states still fail closed to CPU readback or CPU presentation. The
remaining historical priority order was Lane A E4 rendered export, then Lane C portable GPU
backends. Lane C now has named-backend loader selection plus export/playback
proof-wrapper plumbing; actual Vulkan/Metal backend implementation remains the
next portable-backend product step. Future P4 default-promotion work requires a
widened no-readback proof scope, for example a non-Dual-ISO `GPU Tex NR` proof
packet, before Auto may sharpen beyond the current capability gate.

Update 2026-06-19: P-pre **processing parity** has progressed beyond the
original curve-first `allow_creative_adjustments` plan: creative slices 1-6 are
ported, and the later scoping pass records the creative family plus tractable
non-creative per-pixel stages as RTX 4090 validated (see §8.1-§8.2). Remaining
P-pre work is the explicitly scoped spatial/sequential stage decision path, not
the old Slice 1 contrast-curve start. Note also that Lane A **E1** is currently
realized as an off-by-default *shadow validator*
(`MLVAPP_GPU_EXPORT`): `llrawproc` runs the CUDA recon into a scratch buffer and
copies it over the CPU output only when byte-identical, so the CPU path stays
authoritative until the E2 parity gate promotes it.

Update 2026-06-19 Lane A E3 throughput gate: export now has a second, explicitly
opt-in measurement gate, `MLVAPP_GPU_EXPORT_TRUSTED=1`, exposed through
`-CandidateGpuExportTrusted` / `-RequireCandidateGpuExportTrusted` on the
release CDNG A/B and matrix wrappers and `-TrustedGpuExport` on the UltraMagnus
evidence wrapper. The default `MLVAPP_GPU_EXPORT` path remains the
CPU-authoritative shadow validator above. The trusted gate skips the CPU
Dual-ISO oracle for the candidate only after the already-scoped CUDA parity
shape is requested, writes GPU output into the final export buffer, and records
`gpu_export_trusted` / `gpu_export_trusted_frames` so throughput packets cannot
silently pass via the old shadow path. This is a profiling/proof surface, not a
default export behavior change. The first trusted UltraMagnus packet for commit
`2ba1c2e596fd1b8cda2a8add44d397f3792aafaf` passed on RTX 4090 with release
SHA256 `E99F592300AC8ACA00F3B238539711D3834DB1260228590A189A9532B00933A6`,
DNG hash PASS 96/96, and candidate trusted/attempted/replaced frames 96/96/96:
`.claude-state/profiling/ultramagnus-cdng-export/imported/packet-20260619T180606/summary.json`
with packet SHA256 `8B4A455CC4AB2434A9D4B7C37309C516A719183037A746F8D7E6283CF82FB971`.
It promotes the trusted measurement gate for the scoped Dual ISO shape, while
leaving default export CPU-authoritative. Throughput outcome: uncompressed
`M16-1327` improved from 5682.102 ms to 4602.206 ms average wall time
(-1079.896 ms, -15.705%), with average frame-total -31.004 ms/frame and
Dual-ISO -22.625 ms/frame. Lossless stayed byte-identical and used the trusted
GPU path, but overall wall time regressed from 6468.414 ms to 6775.267 ms
(+306.853 ms, +5.113%) despite average Dual-ISO -8.563 ms/frame, so the next E3
work is lossless/compression pipeline overlap rather than more shadow-validator
tuning.

Update 2026-06-18: Lane B **P1/P2** has an experimental readback bridge behind
`MLVAPP_GPU_PLAYBACK_RECON=1`. Playback render/recon threads opt in explicitly,
then `llrawproc` prepares only the CPU-side Dual-ISO match/LUT state and runs
`igpu_recon` to `IGPU_OUT_CPU16` in a temporary output buffer that is copied
back only after success; on missing DLL, unsupported config, invalid state, or
backend error it falls back to the existing CPU `diso_get_full20bit`.
The bridge is limited to the already-proven v1 recon shape
(`MEAN23 + alias_map ON + fullres ON + chroma OFF`) and is telemetry-only: it
does not add a GUI quality claim, no-readback present path, or adaptive mode.
Unlike export's CPU-authoritative shadow replacement, playback P2 deliberately
trusts a successful backend `rc==0` and does not run a per-frame CPU memcmp;
that trust boundary is accepted only for this experimental, env-gated bridge
after the 4090 parity pass, with any future shadow-verify mode tracked as a
canary/follow-up rather than the normal playback path.

Update 2026-06-18 P3 surface: `MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT=1`
is now an explicit request surface. In the narrow experimental x1 GPU preview
processing + `Decode/Reconstruct/Process` playback shape, the Qt GL viewport can
present the P2 reconstructed Bayer frame through a `GL_R16`/Bayer16 texture and
shader-side bilinear debayer/display processing. Telemetry labels this as
`source=cpu16_readback_reconstructed_bayer` and records
`gpu_playback_recon_texture_present_no_readback_active=false`; it is a presenter
surface, not the final P3 win. Activating true P3 still requires a GUI-thread-safe
CUDA `IGPU_OUT_GL_TEXTURE` producer (or final RGB CUDA-to-GL backend) so the
recon output reaches the display without the CPU16 readback.

Update 2026-06-18 P4 status/telemetry slice: the existing Playback Quality
UI/status surface now classifies the presented pipeline as `CPU`, `GPU Preview`,
`GPU RB`, `GPU Tex RB`, or `GPU Tex NR`. Playback smoke logs emit matching
machine-readable tokens (`cpu`, `gpu_preview`, `gpu_recon_readback`,
`gpu_texture_readback`, `gpu_texture_no_readback`) on per-frame GPU telemetry
and a session summary. The 2026-06-19 scoped P3 producer may report `GPU Tex NR`
/ `gpu_texture_no_readback` only when the validated CUDA-to-GL no-readback path
actually presents the frame; readback or unsupported fallback remains reported
as `GPU Tex RB`, `GPU RB`, or `CPU`.

Update 2026-06-19 P3 proof: `MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT=1`
can now present the proven raw-fixes-enabled HQ Dual ISO shape through
`source=cuda_gl_r16_texture` with
`gpu_playback_recon_texture_present_no_readback_active=true`. The accepted
UltraMagnus RTX 4090 run (`20260619T002209`, release executable SHA256
`F70CE56F8418E4107D1AF502F31A3B94399E92253016EC4950E145AB59922CAE`) reported
`correctnessValidated=true`, `gpu_texture_no_readback_frames=94`,
`glParityMatchCount=10`, `glMismatchTotal=0`, and advancing GL texture hashes.
The raw-fixes-off control receipt remains non-proof by design and must not arm
no-readback.
`tools/profiling/invoke-ultramagnus-p3-evidence.ps1` now treats the backend DLL
as part of that proof surface: before validation, the UltraMagnus agent builds
`tools/gpu/backend/igpu_recon_cuda.dll`, verifies it through `dll_test.exe`, and
deploys the DLL plus CUDA runtime beside the staged release executable. Use
`-SkipBackendBuild` only when deliberately reusing an already-current deployed
backend; a staged release tree without `igpu_recon_cuda.dll` is expected to fall
back before GL no-readback proof can occur.

Update 2026-06-19 P3 proof refresh: the current UltraMagnus packet for source
head `1010ed4542f5cabbd8cc30165b1fa80f2fc15dad` on branch
`codex/work-block/wb-2904e97a363e4da7` imported successfully from
`.claude-state/profiling/ultramagnus-p3-texture-present/remote-packets/ultra-magnus-20260619T153332-mlvapp-p3-evidence-latest.zip`
(SHA256 `517914A1A1F26860A5843CA40F5FCEFA3BF2B08A3B758C12E6A4B6A76573BFF0`).
The packet records release executable SHA256
`B976D96B1F7A61931A1C84202A51685AF99ADC8998E3BDD73BC7B46409B4C7AC`,
renderer `NVIDIA GeForce RTX 4090/PCIe/SSE2`, backend DLL SHA256
`AE2983C3D3BAE069C4B094366F83DDA08DFD037C5F0B64844D1A56EA03282C9F`,
`correctnessValidated=true`, `gpu_texture_no_readback_frames=136`,
`fallbackFrameCount=0`, `glParityCheckedCount=14`, `glMismatchTotal=0`, and
`glScreenshotMethod=app_internal_gl_viewport_grab`. VM-local playback remains
tooling/fallback smoke only; P3 no-readback proof is UltraMagnus-backed.

Update 2026-06-20 ASAP CUDA build packaging: the CUDA recon backend build script
now defaults to a portable fat-DLL target set (`sm_61`, `sm_75`, `sm_86`,
`sm_89`, plus `compute_89` PTX) instead of a 4090-only `sm_89` binary, while
still accepting `-Arch sm_89` for narrow UltraMagnus-only rebuilds. This makes
the same `igpu_recon_cuda.dll` build path more suitable for Dell/NVIDIA laptop
smoke runs without weakening the proof rules: Dell playback/export still has to
show backend load, GPU frame/output telemetry, and fallback-free behavior on the
actual laptop before claiming realtime CUDA support there. The backend build now
also emits a hash-bound `igpu_recon_cuda.arch.json` sidecar from `cuobjdump`, so
dogfood/proof kits can verify portable `sm_86`/`sm_89` coverage on machines that
have only the NVIDIA driver installed.

Update 2026-06-20 UltraMagnus CUDA/DNG proof refresh: source head
`ce4f77cb9e83bd25abf05c542632171ac7baf51b` now has a longer scoped RTX 4090
playback packet and a fresh CDNG export packet. The playback packet
`.claude-state/profiling/ultramagnus-p3-texture-present/remote-packets/ultra-magnus-20260620T144939-mlvapp-p3-evidence-latest.zip`
(Length `1406138`, SHA256
`04E46B087B17C725B2CF9CE50525D35707EE1AD75DD078EF202F340DD47F22CA`) used
`M16-1327.MLV`, FastProxy, 30 seconds, and 1000 ms settle against release exe
SHA256 `007619AB21594FCB27553E08A2434A1BC0636CFFC51FDE914F4E9EE0A63CE4D1`.
It imported with `correctnessValidated=true`, `presentedFrames=140`,
`presented_fps=5.432`, `timeline_fps=5.509`, `gpu_texture_no_readback_frames=140`,
`fallbackFrameCount=0`, renderer `NVIDIA GeForce RTX 4090/PCIe/SSE2`, backend
DLL SHA256 `8F36E963E29D1909C7681BE734C4B39CF9CED572D4F3DFFAD32526D0EC71C3C4`,
15 active GL probes, 15/15 GL parity matches, 15 distinct texture hashes, and
0 GL mismatches. This is a scoped UltraMagnus/RTX 4090 FastProxy proof, not a
Dell RTX 3060 Laptop claim.

The matching CDNG export packet
`.claude-state/profiling/ultramagnus-cdng-export/remote-packets/ultra-magnus-20260620T145054-mlvapp-cdng-export-evidence-latest.zip`
(Length `35741`, SHA256
`46BFB6F95D1FD87301C46380CC94C15FFFD00C256496DFCAA0CCA8DADFB30747`) passed on
`NVIDIA GeForce RTX 4090, 596.36, 24564 MiB` with source head `ce4f77cb...`,
release SHA256 `007619AB21594FCB27553E08A2434A1BC0636CFFC51FDE914F4E9EE0A63CE4D1`,
embedded app `build_sha=b2b953a489aa08cadb8e516c334f16435e2ba6d6`, and deployed
backend SHA256 `C8A775BCA0E83664D533F144C8EED19B60E097094A3A2F4F24D2637C53344285`.
With `MaxFrames=4`, `uncompressed` and `lossless` both reported PASS, candidate
GPU export attempted/replaced/trusted 4/4 frames per codec, candidate skips 0,
and DNG hash PASS 8/8 with 0 mismatches. Throughput classification remains
evidence-only: uncompressed improved from 4085.458 ms to 2752.335 ms
(`wall_clock_improved`, 32.631%) and stayed `recon-bound`; lossless improved
from 3105.536 ms to 2852.209 ms (`wall_clock_improved_completion_lag_shift`,
8.157%) but stayed `compress-bound`, with suggested optimization
`review_per_run_throughput_classification_before_promotion` /
`parallelize_or_offload_dng_compress`. This supports the scoped DNG proof path
and E3 triage, not default export promotion.

Follow-up 2026-06-20 E3 promotion-gate proof: the larger UltraMagnus matrix
`.claude-state/profiling/ultramagnus-cdng-export/remote-packets/ultra-magnus-20260620T145731-mlvapp-cdng-export-evidence-latest.zip`
(Length `102369`, SHA256
`D53A68A985A334A5C81314C6B5FC310F628B61799AAFD3B6F5B9B95E853A685D`) deliberately
used a fail-closed promotion shape: `MaxFrames=16`, `Repeats=3`, `uncompressed`
plus `lossless`, trusted GPU export, async writer compression, and
`RequireElapsedImprovement` with `MinElapsedImprovementPercent=5`. DNG identity
still passed (`96/96` matched, `0` mismatches) and candidate GPU export
attempted/replaced/trusted `16/16` frames in every run, but the matrix verdict was
FAIL because uncompressed repeat 2 improved only `2.957%` and repeat 3 regressed
`-0.939%`. The rollup status was `not_promotable` with suggested optimization
`improve_export_wall_clock_before_scheduler_promotion`. This is the current
default/all-codec E3 promotion boundary.

The focused lossless-only gate
`.claude-state/profiling/ultramagnus-cdng-export/remote-packets/ultra-magnus-20260620T145917-mlvapp-cdng-export-evidence-latest.zip`
(Length `57896`, SHA256
`F32EE83F8F2C869C8241514A1A8BEF8EC1DF002F594A2EE5E4DF7A197A3562F1`) passed the
same 16-frame/3-repeat/5% elapsed-improvement gate for `lossless` only. DNG
identity passed (`48/48` matched, `0` mismatches), candidate GPU
attempted/replaced/trusted `16/16` frames on each repeat, and elapsed improvements
were `31.514%`, `28.071%`, and `26.829%`. The token remained
`wall_clock_improved_completion_lag_shift`, not `promotion_candidate`, because
producer work moved earlier while writer completion lag moved later. Treat this
as scoped lossless-DNG async-compress evidence and an E3 policy/implementation
input, not a default export promotion.

Update 2026-06-20 E3 scoped throughput rollups: the CDNG matrix now keeps the
existing top-level `throughputClassification` as the fail-closed all-codec/default
promotion gate, and adds grouped rollups under
`throughputClassificationByCodec` and `throughputClassificationByCase`. The
local CUDA proof summary, packet manifest, and report writer carry those grouped
rollups through so a packet can say "lossless has wall-clock-improved scoped
evidence" while still saying "all-codec/default is not promotable." Local tooling
smoke
`.claude-state/profiling/2026-06-20-e3-codec-rollup-smoke/matrix/matrix-summary.json`
validated the split on one-frame `M16-1327.MLV` cases: top-level
`not_promotable`; `cdngCodec=lossless` `mixed` with
`wall_clock_improved_completion_lag_shift=1`; `cdngCodec=uncompressed`
`not_promotable`. The synthetic local proof-report smoke emitted the matching
`DNG_THROUGHPUT_CODEC_SCOPED_SIGNAL` info diagnostic while remaining
`NOT_QUOTABLE`, so the new guidance does not relax DNG hash, GPU replacement, or
hardware proof gates.

Update 2026-06-20 Dell/local proof wrapper: `tools/profiling/run-local-cuda-playback-dng-smoke.ps1`
now composes the existing P3 no-readback validator and CDNG matrix/DNG-hash
gate into one host-local smoke. It checks the release exe, deployed CUDA backend,
and CUDA runtime, records `nvidia-smi`, runs the forced AMaZE/subset no-readback
playback proof, runs a paired same-release CPU-vs-CUDA playback A/B speed
capture, and runs candidate GPU CDNG export against CPU baseline DNG hashes for
the requested codecs. The wrapper now also sets the Windows per-app
high-performance GPU preference for `MLVApp.exe` unless disabled and searches
the usual Windows NVSMI locations when `nvidia-smi.exe` is not on `PATH`, so the
same entrypoint is suitable for the supplied hybrid Dell RTX 3060 Laptop target.
This is the preferred Dell laptop proof entrypoint: one passing Dell packet can
carry correctness, GL/backend/fallback proof, DNG hash parity, and a local
CPU-vs-CUDA playback speed comparison. A passing UltraMagnus packet still proves
RTX 4090 only, and a passing Dell packet is required before claiming realtime
CUDA support on that machine.
The top-level local-smoke summary also now lifts the CDNG matrix wall-clock and
stage timing deltas into `proof.cdng.speed`, so DNG export speed evidence sits
beside the playback A/B speed evidence instead of requiring manual inspection of
nested matrix files. Those export deltas are host/clip/codec/max-frame scoped,
not universal throughput claims.

Update 2026-06-20 ASAP DNG export runner:
`tools/profiling/run-release-cuda-dng-export.ps1` now provides a direct
release-tree front door for user-facing CUDA DNG export trials. It runs
`MLVApp.exe --batch --input <clip> --output <dir> --cdng-codec <codec>` with
`MLVAPP_EXPORT_STAGE_PROFILER=1`, the deployed CUDA backend, and either trusted
or shadow GPU export mode, then writes a manifest, stdout/stderr, export stage
profile, release/backend/cudart hashes, and DNG output summary under
`.claude-state/profiling/*-cuda-dng-export/`. This is an operator convenience
surface for UltraMagnus/Dell trials; DNG hash parity and CPU-vs-GPU proof still
come from `run-local-cuda-playback-dng-smoke.ps1` or the CDNG matrix wrappers.

Update 2026-06-20 ASAP interactive launcher:
`tools/profiling/start-release-cuda-playback.ps1` now starts the user-facing
release executable with the scoped CUDA playback environment requested:
desktop OpenGL, experimental GL viewport, GPU preview processing, CUDA playback
recon, CUDA-to-GL texture presentation, Phase 3 unattended, Phase 3 HQ, x1
scale, and the deployed `igpu_recon_cuda.dll`. On Windows it also sets the
per-app high-performance GPU preference unless disabled, which matters on
hybrid Dell/NVIDIA laptops where CUDA and GL must land on the same adapter. The
launcher writes a manifest under `.claude-state/profiling/*-cuda-playback-launch`
and updates `.claude-state/profiling/cuda-playback-launch-latest.json`. This is
an ASAP usability surface, not a proof surface: a started process does not prove
realtime playback, no-readback presentation, or Dell support. Use the local
smoke wrapper above for backend load, `GPU Tex NR`/fallback counts, FPS, and DNG
hash evidence.

Update 2026-06-20 playback speed A/B wrapper:
`tools/profiling/run-release-cuda-playback-ab.ps1` now runs a paired
same-release-executable CPU baseline versus scoped CUDA candidate GUI-smoke
comparison. The baseline uses the same FastProxy/scoped AMaZE/subset/x1/HQ
recipe with CPU preview processing; the candidate adds GPU preview processing,
CUDA playback recon, CUDA-to-GL texture presentation, GL parity sampling, and
the deployed `igpu_recon_cuda.dll`. The wrapper writes
`.claude-state/profiling/*-cuda-playback-ab/summary.json` plus
`.claude-state/profiling/cuda-playback-ab-latest.json` with presented FPS,
GUI/status FPS, timeline FPS, render/draw/recon timing deltas, validation
failures, and candidate GL proof fields. This is the correct surface for
answering "GPU playback speed gain versus legacy CPU" on UltraMagnus or the Dell
laptop; prior P3 proof packets prove no-readback correctness, not a speedup.
The follow-up hybrid-GPU hardening makes real A/B runs set the Windows per-app
high-performance GPU preference for `MLVApp.exe` unless disabled, and both the
A/B and DNG runners now look for `nvidia-smi.exe` in the usual Windows NVSMI
locations when it is not on PATH. `run-local-cuda-playback-dng-smoke.ps1` now
invokes this A/B runner by default with a 30-second/2500-ms settled window
(`-SkipPlaybackAb` disables it), so the preferred Dell proof packet includes the
speed comparison unless deliberately suppressed. The A/B summary now also writes
a versioned `mlvapp.playback_ab_analysis.v1` block, and the cross-machine
comparator infers the same block for older A/B packets. The current UltraMagnus
CPU-vs-CUDA packet is deliberately still a failed proof because the CUDA
candidate hit cadence jitter, but its stage deltas classify the regression as
`present-bound` with suggested optimization
`reduce_gpu_present_draw_queue_sync`: recon/render-work got slightly cheaper
while queue wait, draw total, and prep-before-finish grew. The next A/B tooling
slice separates the CUDA candidate into a GL-parity proof leg and a speed leg:
the proof leg keeps `MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=1` but does not
run the temporal artifact detector, while the speed leg omits the heavy
GL-vs-oracle readback and carries cadence/FPS validation. The combined local
CUDA proof wrapper now requests this separated speed leg by default (use
`-SkipPlaybackAbSpeedRun` to return to the old two-leg A/B shape). Treat this as
measurement isolation for the next engine optimization target, not as a CUDA
playback speed claim.

Update 2026-06-20 present-bound optimization slice: the first engine response to
`reduce_gpu_present_draw_queue_sync` removes the proof-only recon-output Bayer
oracle copy from normal no-readback playback. When
`MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=1` is set, the render thread still
snapshots the recon Bayer, requires it for the no-readback candidate, and the
GUI GL-vs-CPU parity probe remains fail-closed. When validation is off (the
separated A/B speed leg and normal experimental playback), the no-readback path
can stay armed with only the required prepared input Bayer + CUDA state, and
telemetry records `gpu_playback_recon_texture_present_no_readback_oracle_*` plus
the owned-byte counters so UltraMagnus can verify the proof buffer is absent
from speed timing. This is a CPU/memory-copy reduction in the presentation
handoff, not a claim that CUDA playback is now faster; quote speed only from a
fresh same-host A/B packet built from this source.

Update 2026-06-20 cadence-attribution telemetry slice: playback profiles and
the opt-in interactive perf field log now promote queue-wait and present-pacing
sub-stages beside the existing decode/recon/process/present timing fields:
`stage_queue_wait_ms`, `stage_present_ui_signal_latency_ms`,
`stage_present_draw_ms`, `stage_present_overlays_scopes_ms`,
`stage_present_slot_release_ms`, `stage_present_pacing_ms`, and
`stage_presentation_overhead_ms` where available. The CUDA playback A/B wrapper
also lifts the existing `playback_smoke.stage_split_summary` into compare
metrics, and the comparator can refine generic `cadence-bound` packets into
`queue-wait-bound` or `frame-pacing-bound` when those deltas dominate. This is
measurement/actionability work only: it does not change default playback,
claim CUDA speedup, or weaken the 30s screenshot/GL parity proof gate.

Update 2026-06-20 Dell laptop target note: the supplied NVIDIA rig screenshot
identifies the laptop as Windows 11 Pro with driver `591.74`, Intel
i9-12900HK, 64 GB RAM, and an `NVIDIA GeForce RTX 3060 Laptop GPU`. That is an
Ampere laptop target, so the portable CUDA backend's `sm_86` slice is the right
binary path to test. The screenshot also reports "No display connected" under
the NVIDIA GPU while the internal Dell XPS 17 SHP1517 display is listed as an
other adapter at 3840 x 2400 / 59 Hz. Treat Dell playback proof as hybrid-GPU
proof, not just CUDA proof: the run must show backend load, the GL renderer, no
readback/fallback counts, and FPS on that machine before claiming laptop
realtime support.

Update 2026-06-20 Dell hybrid-GPU executable hint: `platform/qt/main.cpp` now
exports the standard `NvOptimusEnablement=1` and
`AmdPowerXpressRequestHighPerformance=1` symbols from `MLVApp.exe`. This gives
NVIDIA Optimus / AMD switchable-graphics drivers an app-level high-performance
GPU request even when the app is launched outside the proof wrappers. The
wrappers no longer write the Windows per-app `GpuPreference=2;` registry value
(NA-8); they record the GL renderer the app actually used. This is a Dell/UltraMagnus launch hardening
step only: support and speed claims still require a host-local proof packet with
backend load, GL renderer/probe evidence, no-readback/fallback counts, DNG hash
PASS, and playback/export timing metrics.

Update 2026-06-20 CUDA dogfood kit: `tools/profiling/export-release-cuda-dogfood-kit.ps1`
can now package the exact release tree, deployed CUDA backend/runtime, receipts,
and profiling wrappers into a repo-shaped dogfood kit with a manifest and
`RUN-CUDA-DOGFOOD.ps1` helper. This is the practical "copy this ready build to a
NVIDIA host" surface for Dell/UltraMagnus trials: it preserves the paths the
proof scripts expect and records release/backend hashes up front. The kit is
packaging evidence only; Dell or UltraMagnus support and speed claims still come
only from a passing host-local proof summary produced by the bundled scripts.
`tools/profiling/summarize-local-cuda-proof.ps1` now reads that top-level local
proof summary and emits an operator-facing Markdown/JSON report that separates
playback no-readback proof, playback CPU-vs-CUDA speed, DNG hash/GPU replacement
proof, DNG export speed, artifact hashes, blockers, and quote boundaries. The
dogfood launcher writes `proof-report.md` beside a completed proof summary and
also exposes `-SummaryOnly -SummaryPath <summary.json>` for reading an imported
Dell/UltraMagnus packet. The summarizer is deliberately fail-closed:
`QUOTABLE_PASS` requires a real non-dry-run host success with NVIDIA discovery,
playback correctness/no-readback/no-fallback/GL-parity proof, clean playback A/B,
and CDNG/DNG-hash/GPU-replacement proof; otherwise it reports `NOT_QUOTABLE`
with blockers.
`tools/profiling/package-local-cuda-proof-result.ps1` now packages a host-local
proof run root into `mlvapp-local-cuda-proof-<host>-<stamp>.zip` with a
`local-cuda-proof-packet-manifest.json`, raw `summary.json`, Markdown/JSON proof
reports, child playback/A-B/CDNG artifacts, hashes, host metadata, and the same
quote boundaries. `RUN-CUDA-DOGFOOD.ps1` creates this packet by default after a
proof run (`-NoProofPacket` opts out). The packet is transport/integrity
evidence only: `proofReport.status=QUOTABLE_PASS` is still the required reader
gate before claiming Dell/UltraMagnus support or speed.
`tools/profiling/import-local-cuda-proof-result.ps1` is the return path for
those packets: it expands a copied `mlvapp-local-cuda-proof-*.zip`, validates
the manifest schema, checks every listed artifact hash, optionally enforces host
and repo-head matches, and writes `import-summary.json`. Import success proves
packet integrity, not GPU success; `-StrictQuotable` additionally requires
`proofReport.status=QUOTABLE_PASS`.
The proof report now also emits fail-closed diagnostic classification
(`diagnosticSummary` plus per-code `diagnostics`) for the first Dell/UltraMagnus
packet triage. Codes distinguish dry-runs, missing NVIDIA discovery, hybrid
GL-renderer proof gaps, no-readback/fallback playback failures, missing playback
A/B metrics, DNG hash/export failures, and E3-relevant compression/writer
bottlenecks. Packet manifests and import summaries carry those diagnostics
forward so a returned `NOT_QUOTABLE` packet immediately points at the next
implementation slice. These diagnostics are triage metadata only; they do not
weaken the `QUOTABLE_PASS` support/speed gate.
The follow-up action-plan slice derives ordered `actionPlan.steps` from those
diagnostic codes and carries the plan through proof reports, packet manifests,
and imports. A dry-run packet now points first at a real Dell/UltraMagnus proof
run, hybrid GL/CUDA diagnostics point at adapter-selection proof, playback speed
gaps point at the paired A/B runner, DNG hash/export gaps point at the CDNG
matrix/hash gate, and DNG speed regressions point at E3 compression/writer
overlap. `actionPlan` is still advisory triage only: a `NOT_QUOTABLE` packet
stays fail-closed, and a `QUOTABLE_PASS` packet remains host/clip/hash scoped
rather than a universal support claim.
The kit exporter now also writes `README-CUDA-DOGFOOD.md` and advertises it as
`operatorGuide` in `cuda-dogfood-manifest.json`. The README records source and
release/backend hashes, the full proof command, launch/DNG/summary modes,
return-packet import, expected artifacts, Dell hybrid-GPU proof requirements,
and the same non-claim boundaries. This makes the copied kit self-describing on
the NVIDIA host without changing the hardware proof gate.

Update 2026-06-20 per-machine perf telemetry: playback/export telemetry now
emits a shared `machine-fingerprint.v1` block with build SHA, GPU, CPU, RAM,
and OS identity so Dell RTX 3060 Laptop and UltraMagnus RTX 4090 results can be
compared only when their provenance is visible. Playback profile frames carry
canonical `playback_pipeline` tokens, fallback reasons, first-class stage timing
fields, and a bottleneck/suggested-optimization rollup; export profiles carry
the same fingerprint and bottleneck rollup. The opt-in field log
(`mlvapp.perf-field-log.v1`) appends compact playback/export summaries for
normal dogfooding, and the GUI exposes that through
`Playback -> Performance Profiling...` rather than requiring users to set env
vars by hand. `tools/profiling/compare-machine-perf.ps1` now accepts playback
profile JSON, GUI field-log JSONL, dogfood proof `summary.json` files, and the
imported UltraMagnus P3/CDNG validation summaries, then prints side-by-side
presented FPS, no-readback percentage, fallback percentage, fallback count,
bottleneck, and suggested-optimization rows. This is still a
measurement surface only: Dell/UltraMagnus support and speed claims require
real same-clip host packets with fallback/no-readback/DNG hash/timing evidence.
The same comparator can also emit a versioned
`mlvapp.compare-machine-perf.v1` JSON document via `-Json` or `-OutputJson`,
so cross-machine results can be archived without scraping the console table.
The comparator also accepts valid export field-log rows and, for dogfood proof
summaries, shows CDNG verdict, DNG hash verdict, GPU export replacement/trusted
coverage, DNG wall-time delta, throughput classification, and the export-side
suggested optimization alongside the playback columns. This keeps one
cross-machine table useful for both realtime playback and DNG export triage
without loosening the host-local proof gate.
Imported UltraMagnus validator summaries are treated as scoped proof-summary
inputs: P3 rows carry the validated no-readback/fallback/presented-FPS proof
fields available in the validation packet, while CDNG rows carry the matrix
hash/export/speed fields and the full machine fingerprint from the matrix.
The dogfood kit README advertises both the GUI profiling path and the
cross-machine comparator so returned proof packets and ad-hoc interactive
field logs can be reviewed with the same table.
The same dialog now also exposes two off-by-default dogfood presets: a scoped
CUDA playback profiling preset that requests the validated `GPU Tex NR`
telemetry path (GL viewport, GPU preview processing, CUDA playback recon,
texture present, Phase 3 HQ, and x1 scale), and a DNG async-compress profiling
preset with queue/thread controls that requests export-stage profiling plus the
async writer/compression path. These GUI presets persist through QSettings and
only manage environment values marked as GUI-owned, so script-driven proof
wrappers remain authoritative and default playback/export behavior stays
unchanged. The CUDA preset is still a request surface, not proof: Dell/UltraMagnus
claims require the same host-local no-readback/fallback/FPS/DNG hash/timing
evidence as the wrapper path.
The dialog also shows the current `machine-fingerprint.v1` identity block
directly in the GUI (GPU/driver/compute/VRAM, CPU cores/threads, RAM, OS, and
embedded build SHA) with a copy-JSON command, so interactive Dell/UltraMagnus
field-log runs can be visually matched to the machine/build provenance already
written into playback/export telemetry.
The dogfood/proof front doors now also record CUDA backend architecture proof
from a hash-matched `igpu_recon_cuda.arch.json` sidecar or host `cuobjdump`, then
compare those tokens to the host `nvidia-smi` compute capability before real
proof runs. ASCII string scanning is only an advisory fallback because embedded
cubins can omit visible `sm_*` strings. This makes the important distinction
explicit: an `sm_89`-only backend is UltraMagnus/RTX 4090-shaped but not Dell
RTX 3060 Laptop-ready; the Dell path requires reliable `sm_86` proof before
runtime playback/export proof is meaningful. The gate is a preflight only, not a
support claim: even an architecture-compatible DLL still needs the same
backend-load, GL/no-readback, fallback, DNG hash, and timing packet before Dell
or UltraMagnus speed/support can be quoted.

Update 2026-06-20 E3 dogfood async-compress surface: the simple CUDA DNG
runner now exposes the same lossless compression-overlap knobs as the CDNG
profile matrix: `tools/profiling/run-release-cuda-dng-export.ps1` accepts
`-UseAsyncWriter`, `-UseAsyncWriterCompression`, `-AsyncWriterQueueDepth`, and
`-AsyncWriterThreadCount`, while
`tools/profiling/run-local-cuda-playback-dng-smoke.ps1` forwards candidate
`-CandidateUseAsyncWriter*` settings into its CDNG matrix child and records
those knobs in `summary.json`. The dogfood kit advertises direct
`RUN-CUDA-DOGFOOD.ps1 -DngOnly -DngAsyncWriter -DngAsyncWriterCompression`
and full-proof async-compress commands, so Dell/UltraMagnus can measure whether
moving LJ92 compression into writer workers helps the lossless DNG path without
changing default export behavior. This remains measurement/proof plumbing, not
promotion: keep DNG hash PASS, release/backend hashes, codec, frame count,
async-writer active/queue counters, bottleneck fields, and host/clip identity
attached to any E3 speed claim.

Update 2026-06-20 E3 throughput classification: the CDNG A/B and matrix proof
stack now emits `mlvapp.cdng_throughput_classification.v1` / rollup JSON that
separates wall-clock elapsed improvement from writer-completion/frame-total
attribution holds. Local Dell/UltraMagnus proof summaries, proof reports, and
proof-packet manifests carry the same classification plus a machine-readable
`suggestedOptimization`, so an async-compress result can be triaged as
`wall_clock_improved`, `wall_clock_improved_attribution_hold`,
`not_promotable`, or unclassified without changing any existing fail-closed
verdict. This is still proof/reporting policy only: default DNG export remains
legacy/CPU-authoritative unless a later reviewed promotion explicitly accepts
the throughput and attribution gates.
Codec-scoped rollups now also feed the operator-facing action plan: when a
summary emits `DNG_THROUGHPUT_CODEC_SCOPED_SIGNAL`, proof reports recommend a
lossless-only async-compress rerun before treating the signal as an E3
optimization target. The dogfood launcher accepts `-DngCodecs lossless` and the
kit README advertises the full lossless-only proof command, keeping this as a
clean GUI/kit workflow instead of an env-var-only path. This does not promote
default/all-codec export speed; it only makes the next scoped measurement easier
to run on Dell and UltraMagnus.

Update 2026-06-19 P4 default slice: clean playback settings now default to
`Auto` instead of `Fast`, matching the user-facing mode plan below while still
round-tripping explicit `Fast` selections. This is only the first adaptive
quality polish step. The Auto sampler also keeps headroom-based sharpening at
HQ x4 until the caller has observed a validated no-readback presentation path;
capability-aware promotion/demotion remains scoped by the P3 proof gate and
must keep unsupported states on readback/CPU paths. Paired GUI-smoke A/B review
now has a durable comparator (`tools/profiling/compare-release-gui-smoke-ab.ps1`)
that reports screenshot pixel deltas, GUI/presented/timeline FPS deltas, and an
optional screenshot-drift failure verdict from two `run-release-gui-smoke.ps1`
JSON outputs. The comparator also carries `visualQuality.autoDecision` deltas,
including Auto reason, target/budget/average cadence in milliseconds and
FPS-equivalent form, sample count, capability latches, and capability-failure
arrays. This is a tooling-only review path over existing smoke JSON; it does
not create a local VM playback proof, and P3/P4 no-readback promotion evidence
still has to come from the UltraMagnus proof path.
The Auto tooltip and playback smoke summary now report both milliseconds and
FPS-equivalent cadence for the latest Auto decision (`auto_avg_fps_equivalent`
and `auto_budget_fps_equivalent`), so adaptive decisions can be read without
manual conversion.
The visible playback quality menu/status now uses the roadmap vocabulary:
`Auto`, `Prioritize Quality`, and `Prioritize Smoothness`. The underlying mode
ids and legacy `fast`/`hq` automation names remain supported; the parser also
accepts `prioritize-smoothness` and `prioritize_quality`.
The GUI smoke wrapper now defaults its validation gate to deterministic Auto
mode (`quality_mode=2`) and x4 scale request by forcing those env selectors,
so persisted GUI settings cannot create stale false failures. Smokes that
intentionally exercise saved GUI state can pass `-UsePersistedPlaybackSettings`,
and explicit forced-mode probes can still pass `-QualityMode`, `-ScaleFactor`,
and `-Expected*` overrides.
The wrapper now also lifts the Auto cadence/capability fields from
`playback_smoke.summary` into `visualQuality.autoDecision` and fails the default
Auto gate if those fields are missing. That keeps P4 smoke artifacts
self-contained: reviewers can see target FPS, last Auto reason, ms and
FPS-equivalent cadence, sample count, headroom capability, and validated
no-readback latch/demotion state without hand-parsing raw log lines.
The P4 proof wrapper now also derives boolean Auto capability fields and fails
default Auto smokes when the capability summary is internally inconsistent:
`headroom_non_dual_iso_sharper_hq` must carry an active validated no-readback
capability latch, `auto_headroom_capability_last` cannot be true while
`auto_validated_no_readback_capability_observed` is false, and observed/demoted
cannot both be true in the same summary. The derived
`visualQuality.autoDecision.capabilityConsistent` and
`validation.autoDecisionCapabilityConsistent` fields make this fail-closed gate
reviewable without widening the scoped P3 no-readback claim.
The same wrapper now fails default GUI smokes when `presented_frames` is missing
or zero; `-AllowZeroPresentedFrames` is reserved for deliberate launch-only
probes. This closes a proof gap where a smoke could previously report
`validation.ok=true` with Auto telemetry present but no rendered/presented frame
sample.
The visible status tooltip now defines the full P4 pipeline vocabulary,
including `GPU Preview`, and
`GuiSmoke.mainWindowGpuPreviewPolicyClassifiesPlaybackPipelineStatus` pins its
token, label, and description so the UI text cannot silently drift from the
telemetry enum.

Update 2026-06-19 P4 Look Assist safety slice: the profile/playback harness now
has an explicit `--exercise-look-assist-settle` path plus metadata for settled
diagnostics and safety fallback state. Floor-lifted Look Assist now fails closed
when the post-applied processed-color oracle is invalid under an original-raw-
white, auto-chroma-smoothed state, covering the `M16-1243` control without
over-falling back on the flatter `M16-1446` night clip. Rebuilt console guards
cover the standard M16 Look Assist set plus the optional 1243 control:
`ClipGolden.LocalM16LookAssistRejectsExtremeGreenAutoWbWhenAvailable`,
`ClipGolden.LocalM16LookAssistRejectsBrightNeutralGreenClampWhenAvailable`, and
`ClipGolden.LocalM16LookAssistCapsOnlyFlatNoiseFloorNightWhenAvailable`.
Headless release-tree profile evidence is under
`.claude-state/profiling/2026-06-19-p4-lookassist-headless-final/`: `M16-1327`
and `M16-1243` fall back, while `M16-1347` and `M16-1446` remain enabled. This
is local VM build/profile proof only; CUDA/GL, no-readback, and UltraMagnus RTX
4090 claims remain governed by the UltraMagnus proof gate.

Update 2026-06-19 P4 capability-telemetry slice: Auto sampler decisions now
carry the exact `sharperHeadroomScaleAllowed` gate that decides whether
headroom may promote a non-Dual-ISO clip from `HQ x4` to `HQ x2`. The status
tooltip, playback-profile frame JSON, and playback smoke summary expose this as
`auto_headroom_capability_last`, so reviewers can tell whether Auto sharpened
because the scoped true no-readback texture path was actually observed or held
back for lack of capability proof. This is evidence/control polish only; it
does not widen the P3 no-readback scope.
The follow-on P4 control slice latches a session-scoped
`auto_validated_no_readback_capability_observed` flag only after an accepted
presented frame reports the validated `GPU Tex NR` pipeline. Auto headroom
promotion now uses that latched capability rather than the current frame alone,
so a real no-readback proof can inform later Auto decisions in the same session
without treating mere FPS headroom or GPU presence as proof.
If a later frame is still a no-readback candidate but falls back before
presenting `GPU Tex NR`, the latch is cleared and
`auto_validated_no_readback_capability_demoted_last` reports the demotion. That
keeps Auto's capability-aware promotion/demotion tied to actual presentation
truth instead of stale optimism.
The latch is playback-run scoped: clip opens, play stop/start, quality-mode,
preview-mode, preview-resolution, scale override, Dual ISO/raw-fix context, and
Auto target-FPS changes all reset it, so a later context must present
`GPU Tex NR` again before Auto uses no-readback capability to sharpen quality
decisions. The headroom permission is stricter than the general visible latch:
a Dual ISO `GPU Tex NR` observation may keep
`auto_validated_no_readback_capability_observed=true` for telemetry, but it does
not arm non-Dual-ISO `HQ x2` promotion; that promotion requires a non-Dual-ISO
no-readback observation in the current run.
The same reset path now also reseeds the active Auto scale/HQ decision to the
current mode's initial state, and clip open reseeds after the new object becomes
current, so stale x2/headroom or Fast-demotion decisions cannot leak into a new
run before the sampler and capability gate observe that context.
The default/fallback path now uses the same configured `Auto` default for
missing, invalid, corrupt, or unavailable hidden Phase3 quality settings instead
of silently falling back to legacy `Fast`, with console coverage in
`PlaybackQualitySettings.RoundTripQualityMode`.
The adjacent preview-mode override parser now accepts the same natural
case-insensitive forms for `MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW` and
`MLVAPP_PLAYBACK_PREVIEW_MODE` (`Aggressive`, `AGGRESSIVE-PERFORMANCE`,
`Sharp-Smooth`, `ON`, `Off`, etc.) while preserving the existing
`MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW` precedence and invalid-value fallback, with
console coverage in `PlaybackPreviewModeOverride.ParsesCaseInsensitiveEnvNames`.
The advanced preview-mode and preview-resolution persisted controls now also
have QSettings default/round-trip/invalid-value guards in
`PlaybackQualitySettings.RoundTripPreviewMode`,
`PlaybackQualitySettings.RoundTripPreviewResolution`, and
`PlaybackQualitySettings.PreviewResolutionProxyLevelMapping`; the shared
settings cleanup helper clears those keys so P4 control-surface tests do not
inherit stale GUI state from earlier local runs.
This is the current scoped P4 closeout: Auto may only promote quality from
observed presentation truth, and under the accepted P3 proof scope the app must
not default-promote non-Dual-ISO headroom to `HQ x2` until a matching
non-Dual-ISO no-readback proof packet exists.

Update 2026-06-19 Lane A E3 prep: the export-stage profiler now records
`queue_idle_ms` as a supported stage. The first frame has no prior handoff gap,
while later frames measure the elapsed time between the previous profiled frame
finishing and the next `saveDngFrame` call beginning. This keeps current serial
exports byte-inert while giving future pipelined export experiments a scheduler
starvation/overlap signal before CPU decode workers, the single GPU recon queue,
or CPU compress/write workers are promoted. Async-writer experiments now also
record `producer_frame_ms`, the caller-side time from frame-save entry until the
serial path finishes or the async path enqueues the immutable payload, plus
`producer_queue_idle_ms`, the gap between the previous caller-side return and
the next frame-save entry. The older `queue_idle_ms` remains previous profiled
frame completion to next frame-save entry, which means async profiles can show
writer-thread completion lag there even after the producer has returned.
`writer_completion_lag_ms` is derived as `frame_total_ms - producer_frame_ms`,
making that post-producer writer lag explicit in release-tree profiles. Real
release-tree baseline and candidate runs should use
`tools/profiling/run-release-cdng-export-profile.ps1`, which launches the
current `MLVApp.exe --batch` CDNG export path with
`MLVAPP_EXPORT_STAGE_PROFILER=1` and writes the profile JSON next to the exported
DNG output bundle. The runner can pass a bounded `-MaxFrames` cap through to
batch `--max-frames`, so real-footage probes can avoid unbounded DNG output
before full benchmark matrices are intentional. Headless batch export now also
accepts `--cdng-codec uncompressed|lossless|fast-pass`, and the profiling
wrappers expose that as `-CdngCodec` / per-case `cdngCodec`, so E3 can run a
representative lossless-DNG writer-heavy matrix without changing the default
uncompressed batch path. `tools/profiling/compare-export-stage-profiles.ps1` now
summarizes frame-total avg/p95 deltas plus queue-idle avg/p95 deltas in stdout,
and `-FailOnRegression` gates both avg and p95 frame-total regressions.
`tools/profiling/run-release-cdng-export-profile-matrix.ps1` wraps the paired
A/B runner across named cases and repeats, writing a single
`matrix-summary.json` with per-run frame-total, producer-frame, queue-idle,
writer-completion-lag, payload handoff (`payload_clone_ms`), writer-queue-wait, wrapper wall-clock
elapsed-time deltas, async queue capacity, and async max-queued fields. Tiny
fixture matrix runs are smoke tests only; E3 promotion still requires a bounded
real-footage matrix with receipts/frame caps that match the export scenario
under review. `tools/profiling/compare-cdng-dng-output-hashes.ps1` now turns
the baseline-vs-candidate DNG SHA256 sweep into a reusable companion for both
matrix summaries and standalone A/B summaries: it follows each run's A/B
`summary.json` in matrix mode or reads a single A/B `summary.json` with
`-AbSummary`/`-SummaryJson`, compares DNG files by relative path, length, and
SHA256, writes `dng-hash-comparison.json`, and fails closed under
`-FailOnMismatch`. Validation reran matrix mode against
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-matrix-16x3-current/matrix-summary.json`
for 144/144 matched pairs, and the deliberate mismatch smoke under
`.claude-state/profiling/2026-06-19-cdng-hash-tool-negative-smoke/` exited 1.
A/B and matrix summaries now also label the comparison as
`feature-ab` or `identity-aa`, so baseline-vs-baseline calibration runs cannot
be mistaken for feature promotion or regression evidence. The A/B runner also
supports explicit `BaselineFirst` or `CandidateFirst` execution, and the matrix
runner can alternate run order across repeats with `-AlternateRunOrder`, so
future promotion packets can reduce warm-cache/order bias without changing the
baseline/candidate output locations or comparator contract.

Update 2026-06-19 Lane A E3 payload contract: `dngFramePayload_t`,
`buildDngFramePayload`, `writeDngFramePayload`, `saveDngFrameViaPayload`, and
`freeDngFramePayload` now provide an immutable header+image handoff for a built
DNG frame. The default GUI/batch export path remains serial through
`saveDngFrame`, but setting `MLVAPP_CDNG_EXPORT_PAYLOAD_HANDOFF=1` or passing
`-UsePayloadHandoff` to `tools/profiling/run-release-cdng-export-profile.ps1`
routes CDNG export through a serial build-payload/write-payload boundary. The
same payload contract now also has an opt-in writer-worker path behind
`MLVAPP_CDNG_EXPORT_ASYNC_WRITER=1` / `-UseAsyncWriter`; it defaults to one
worker and one in-flight payload so current behavior remains bounded, but
`MLVAPP_CDNG_EXPORT_ASYNC_WRITER_QUEUE_DEPTH` / `-AsyncWriterQueueDepth` can
raise that bounded depth and `MLVAPP_CDNG_EXPORT_ASYNC_WRITER_THREADS` /
`-AsyncWriterThreadCount` can raise the worker count for release-tree
experiments. Multi-worker runs may complete files out of frame order and remain
experimental until byte identity plus scheduler benefit are proven. The profiler JSON
records `payload_handoff_env_enabled`, `async_writer_env_enabled`,
`async_writer_thread_count`, `async_writer_queue_capacity`, and
`async_writer_max_queued`, and the pipeline
test suite verifies byte-for-byte parity for uncompressed and compressed tiny
Dual-ISO DNG exports, including the async writer path. `tools/profiling/run-release-cdng-export-profile-ab.ps1` runs
paired release-tree baseline/candidate exports and writes both profiles plus
`compare.json`/`summary.json`, so E3 experiments have one repeatable promotion
packet. Payload-handoff profiles now expose `payload_clone_ms`, the historical
field name for the small header copy plus large image-buffer ownership
handoff/replacement cost, and async writer profiles expose
`writer_queue_wait_ms`, which separates bounded writer-queue backpressure from
real `disk_write_ms`. Paired A/B summaries include producer-frame and
producer-queue-idle deltas plus writer-completion-lag deltas so larger
decode/GPU/write scheduler work is not promoted on a blended completion signal.
This is scheduler prep, not a throughput claim.

Update 2026-06-19 Lane A E3 matrix finding: the first bounded real-footage
matrix with wrapper elapsed timing is
`.claude-state/profiling/2026-06-19-cdng-e3-real-matrix-elapsed/matrix-summary.json`.
It used `C:\temp\MLV\M16-1210.MLV`, `M16-1327.MLV`, and `M16-1347.MLV` with
`C:\temp\MLV\master.marxml`, `maxFrames=8`, `repeats=2`, payload handoff,
async writer, queue depth 2, and frame-total regression gates enabled. Verdict:
FAIL, with 4/6 runs passing and 2/6 failing. Overall wrapper elapsed delta
averaged -118.551 ms, but `m16-1210-master` averaged +160.28 ms, and the matrix
still recorded frame-total avg/p95 gate failures on `m16-1210-master` repeat 1
and `m16-1327-master` repeat 1. The async writer therefore remains
non-promoted: queue wait stayed 0.0 ms, async max queued stayed 1, average
writer-completion lag was 3.379 ms, and average payload handoff cost was
2.089 ms. Next E3 work should either reduce/avoid the payload copy or run a
serial payload-only matrix to separate payload overhead from writer-thread
overlap before any broader scheduler rewrite.

Follow-up payload-only matrix:
`.claude-state/profiling/2026-06-19-cdng-e3-payload-only-matrix/matrix-summary.json`
used the same three clips, receipt, `maxFrames=8`, and `repeats=2`, but enabled
only `-CandidateUsePayloadHandoff` without async writer. Verdict: FAIL, with
4/6 runs passing and 2/6 failing. Overall wrapper elapsed delta averaged
-38.813 ms, but `m16-1210-master` averaged +204.40 ms and still produced one
avg/p95 frame-total gate failure; `m16-1327-master` produced one p95-only gate
failure despite elapsed improvement. Writer-completion lag was effectively zero
(0.000208 ms average), while payload handoff cost averaged 2.143 ms. This
pointed the next E3 implementation step at avoiding the extra large image
payload copy, while retaining the small header copy, instead of widening async
writer concurrency.

Image-buffer handoff follow-up:
`.claude-state/profiling/2026-06-19-cdng-payload-move-final-two-frame/profile.json`
proved the real compressed-input/uncompressed-output two-frame crash repro clean
on committed build `c5d92baf` after the payload boundary was narrowed to a small
header copy plus large image-buffer ownership move. The profile wrote two
`M16-1210` DNGs at 8,202,254 bytes each and reported `payload_clone_ms`
averaging 0.0147 ms. The committed-build bounded real-footage payload-only
matrix
`.claude-state/profiling/2026-06-19-cdng-e3-payload-move-payload-only-matrix-final/matrix-summary.json`
then completed without crashes on the same three clips, receipt, `maxFrames=8`,
and `repeats=2`; all 6/6 runs passed, all six baseline/candidate run outputs
matched with zero DNG SHA256 mismatches, payload handoff cost averaged
0.01515 ms, wrapper elapsed averaged -182.774 ms, and frame-total average delta
averaged -11.402 ms. This promotes the serial payload handoff as the bounded E3
candidate slice, while preserving the scope boundary: it is still a 3-clip,
8-frame, 2-repeat release-tree matrix with one receipt set, not a broad export
benchmark. The matching committed-build async-writer matrix
`.claude-state/profiling/2026-06-19-cdng-e3-payload-move-async-matrix-final/matrix-summary.json`
also completed with zero DNG SHA256 mismatches and 0.01469 ms average payload
handoff cost, but remained non-promoted at 3/6 PASS and 3/6 FAIL. Queue wait
stayed 0.0 ms, async max queued stayed 1, and writer-completion lag averaged
3.291 ms. Next E3 work should keep serial payload handoff gated for broader
promotion proof, and investigate why the async path never exceeds one queued
payload and does not convert writer lag into stable frame-total gains before any
async scheduler rewrite is promoted.

Broader serial-payload promotion-gate follow-up:
`.claude-state/profiling/2026-06-19-cdng-e3-payload-promotion-matrix/matrix-summary.json`
reran the committed serial payload handoff on the same three clips and receipt
with `maxFrames=24` and `repeats=3`. The matrix completed byte-correctly: all
nine baseline/candidate DNG output sets matched with zero SHA256 mismatches, the
average payload handoff cost stayed at 0.015337 ms, wrapper elapsed averaged
-120.410 ms, writer-completion lag averaged 0.000141 ms, and writer queue wait
stayed 0.0 ms. Verdict: FAIL, with 7/9 runs passing and two narrow timing-gate
misses: `m16-1210-master` repeat 3 tripped the frame-total p95 gate by 10.371%
despite -269.677 ms elapsed improvement, and `m16-1327-master` repeat 3 tripped
the frame-total average gate by 5.402% despite -107.357 ms elapsed improvement.
This keeps the serial payload handoff as a correct, low-cost E3 candidate, but
does not promote it beyond the earlier bounded 8-frame gate. Next E3 work should
calibrate the promotion gate with an A/A timing-variance matrix or equivalent
methodology proof before treating marginal 24-frame frame-total deltas as a
feature blocker or broad promotion signal.

A/A gate-calibration follow-up:
`.claude-state/profiling/2026-06-19-cdng-e3-aa-promotion-matrix/matrix-summary.json`
reran the same 24-frame, 3-repeat, 3-clip matrix with identical serial export
settings on both sides (`comparisonMode=identity-aa`, no payload handoff, no
async writer). The outputs again matched with zero DNG SHA256 mismatches, but
the strict 5% average / 10% p95 frame-total gates still reported Verdict: FAIL,
with 5/9 runs passing and 4/9 runs failing. Overall elapsed averaged -82.335 ms,
frame-total average delta averaged +0.897 ms, frame-total p95 delta averaged
+8.060 ms, writer-completion lag averaged 0.000070 ms, and writer queue wait
stayed 0.0 ms. The identity failures were
`m16-1210-master` repeat 1 p95 +10.952%,
`m16-1327-master` repeat 1 p95 +15.254%,
`m16-1347-master` repeat 1 average +5.827%, and
`m16-1347-master` repeat 2 average +8.389% plus p95 +13.915%. This confirms the
24-frame gate is currently a timing-variance detector, not a reliable standalone
promotion/blocker oracle for the serial payload handoff. Future E3 promotion
should either use a variance-adjusted criterion, longer/stratified samples, or
an explicit A/A companion threshold before broadening the serial payload handoff
claim.

A/A-envelope serial-payload rerun:
`.claude-state/profiling/2026-06-19-cdng-e3-payload-aa-calibrated-matrix/matrix-summary.json`
then reran the feature comparison with rounded companion thresholds from the
identity matrix (`maxFrameTotalRegressionPercent=8.5`,
`maxFrameTotalP95RegressionPercent=15.5`). The comparison metadata correctly
reported `comparisonMode=feature-ab`, all nine DNG output sets again matched
with zero SHA256 mismatches, average payload handoff cost was 0.015769 ms,
wrapper elapsed averaged -110.241 ms, frame-total average delta averaged
-0.639 ms, frame-total p95 delta averaged -0.886 ms, writer-completion lag
averaged 0.000178 ms, and writer queue wait stayed 0.0 ms. Verdict still
remained FAIL at 7/9 PASS and 2/9 FAIL: `m16-1210-master` repeat 1 exceeded the
p95 companion gate at +22.514%, and `m16-1347-master` repeat 2 exceeded both
average (+9.638%) and p95 (+20.599%) companion gates. This does not point back
to payload-copy cost; it shows the current short real-footage matrix is still
order/noise sensitive enough that broad serial-payload promotion should remain
held at the earlier bounded 8-frame claim until E3 has a stronger methodology
such as longer samples, randomized ordering, or paired A/A-per-feature runs.

Alternating-order methodology follow-up:
`tools/profiling/run-release-cdng-export-profile-ab.ps1` and
`tools/profiling/run-release-cdng-export-profile-matrix.ps1` now record
`runOrder` and can alternate baseline/candidate execution across matrix repeats.
The committed-tooling A/A alternating matrix
`.claude-state/profiling/2026-06-19-cdng-e3-aa-alternating-matrix/matrix-summary.json`
reported `comparisonMode=identity-aa`, `alternateRunOrder=true`, zero DNG
SHA256 mismatches, and Verdict: FAIL at 5/9 PASS and 4/9 FAIL. Aggregate elapsed
delta averaged +58.827 ms, frame-total average delta averaged -0.033 ms, and
frame-total p95 delta averaged +7.724 ms; strict-gate failures still reached
avg +10.638%, p95 +11.543%, avg +10.308% plus p95 +14.586%, and avg +7.841%
plus p95 +48.743%. The matching serial-payload alternating matrix
`.claude-state/profiling/2026-06-19-cdng-e3-payload-alternating-matrix/matrix-summary.json`
reported `comparisonMode=feature-ab`, `alternateRunOrder=true`, zero DNG SHA256
mismatches, and Verdict: FAIL at 7/9 PASS and 2/9 FAIL. Aggregate elapsed
averaged -64.279 ms, frame-total average delta averaged -1.700 ms, frame-total
p95 delta averaged -0.284 ms, and payload handoff averaged 0.016170 ms, but two
candidate-first repeats still exceeded the p95 gate (+14.454% and +18.477%).
This reinforces the current E3 status: serial payload handoff is correct and
low-cost, but broad promotion still needs longer/stratified sampling or a
statistical matrix comparator; alternating order alone is a useful control, not
a sufficient promotion oracle.
That comparator now exists as
`tools/profiling/compare-cdng-export-matrices.ps1`. The first calibration packet
at
`.claude-state/profiling/2026-06-19-cdng-e3-payload-alternating-calibration/calibration.json`
compared the alternating identity matrix against the alternating serial-payload
matrix and reported `verdict=WITHIN_IDENTITY_ENVELOPE` while preserving
`identityRawGateUnstable=true`. Frame-total average stayed inside the identity
positive envelope (identity max +12.456658 ms, feature max +6.042370 ms), and
frame-total p95 also stayed inside it (identity max +64.821900 ms, feature max
+27.284600 ms). Payload handoff and tiny writer-lag deltas are expected feature
costs, not frame-envelope regressions. This is enough to say the serial payload
handoff's 24-frame alternating evidence is not worse than measured A/A jitter,
but the roadmap still does not promote it as a broad export-throughput win until
the sampling strategy itself is stronger.
The comparator now writes
`release-cdng-export-matrix-calibration.v4`, fails closed on non-identity
calibration inputs, non-feature candidate inputs, alternate-run-order mismatch,
or case/repeat/run-order key mismatch, and includes per-case envelopes plus
median/p95/positive-max summaries and stage-attribution metrics from each
run's `compare.json`. Its `stageAttribution` object also names the dominant
positive feature-average stage and the dominant positive-max identity-envelope
excess stage, so E3 promotion/blocker packets can cite the leading stage driver
without hand-parsing the metrics array. Its `schedulerAttribution` block also
separates producer-frame / producer-idle deltas from writer-completion-lag /
writer-queue-wait deltas, including p95 metrics, so async evidence can
distinguish caller-side overlap from completion backlog. Validation against the
same alternating A/A and serial-payload matrices produced
`.claude-state/profiling/2026-06-19-cdng-e3-payload-alternating-calibration-v4/calibration.json`
with `verdict=WITHIN_IDENTITY_ENVELOPE`, `compatible_keys=True`, `modes_ok=True`,
and `dominant_scheduler_stage=producerQueueIdleAvgDeltaMs`; the v4
identity-as-feature negative smoke at
`.claude-state/profiling/2026-06-19-cdng-e3-calibration-negative-smoke-v4/identity-as-feature.json`
exited 1 as `INCOMPATIBLE_MATRICES`. This upgrades the E3 methodology guardrail:
future promotion packets must compare matching identity and feature matrices
before a noisy raw gate is interpreted as either a blocker or a throughput win.

Async-writer queue-depth investigation:
the current real-footage matrices exercise the batch CDNG receipt/default path,
where producer-frame work is dominated by decode/recon/pack at roughly
hundred-millisecond scale while `disk_write_ms` is only a few milliseconds. The
single writer worker therefore drains each payload before the next payload is
usually ready, so `async_writer_max_queued=1` is expected even when the configured
queue capacity is 2. That makes the async writer non-promotable for this
workload, but it is not by itself a queue-capacity bug. Next async E3 proof
should use a deliberately writer-heavy/compressed-output scenario or a targeted
stress harness before changing scheduler policy; the current matrices mainly
show that serial payload handoff is cheap and that write overlap is not the
bottleneck for the measured default path.

Targeted async-writer stress control:
`MLVAPP_CDNG_EXPORT_ASYNC_WRITER_DEBUG_DELAY_MS` and
`-AsyncWriterDebugDelayMs` now provide a test/profiling-only writer-thread delay
that is inert unless explicitly opted in. The pipeline regression
`DualIsoPipeline.DngFrameAsyncWriterDebugDelayCanFillConfiguredQueue` verifies
that a queue depth of 2 can report `async_writer_max_queued=2`, and the
release-tree smoke packet at
`.claude-state/profiling/2026-06-19-cdng-async-delay-release-smoke-18118e78/profile.json`
exported two `M16-1210` frames on build
`18118e7893f43174ffc275020a53c27c0e1fbc87` with
`async_writer_queue_capacity=2`,
`async_writer_debug_delay_ms=2000`, and `async_writer_max_queued=2`. This
validates queue-capacity mechanics under synthetic writer backpressure, not a
real throughput win; async writer promotion still needs a representative
writer-heavy workload or scheduler result that beats the serial payload boundary
without DNG mismatches.

Batch CDNG codec selector:
`MLVApp --batch` still defaults to uncompressed CDNG, but `--cdng-codec
lossless` now selects the existing lossless-JPEG DNG path and `--cdng-codec
fast-pass` selects the existing pass-through mode. The release profiling
wrappers carry the same selector into single, A/B, and matrix runs. This is
tooling for the next E3 proof: it makes a representative lossless-output,
writer/heavier-compress matrix possible without broadening the production
default or changing GUI export behavior.
The committed-build lossless smoke at
`.claude-state/profiling/2026-06-19-cdng-codec-lossless-smoke-037d3d59/profile.json`
ran build `037d3d5940e4989eac5ba6f3d74b289225819af0`, logged
`cdng-codec=lossless`, exported two `compressed_raw` frames, and recorded two
`dng_compress_ms` samples (average 58.84905 ms). The matching lossless
payload-handoff A/B smoke at
`.claude-state/profiling/2026-06-19-cdng-codec-lossless-payload-ab-037d3d59/summary.json`
reported `cdngCodec=lossless`, `verdict=PASS`, and the two baseline/candidate
DNG pairs matched byte-for-byte
(`A4356D40D811982CF08D0900F5AC68B5C7A76B0918B614AA71E63DAD9F9838A1` and
`7D6D00C078EAE41326F1801DFBCD3A5872D7DFDADE962CBC90E71616EF758D51`). This
proves the lossless profiling surface and serial payload boundary work together
on a two-frame smoke; it is not a throughput promotion or scheduler claim.
The follow-up lossless real-footage matrix at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-payload-matrix-74b52e39/matrix-summary.json`
ran build `74b52e3900d7230ba806165937c3489057df2b5c` on the same three clips
and receipt with `--cdng-codec lossless`, `maxFrames=8`, `repeats=2`,
`-AlternateRunOrder`, and serial payload handoff. Verdict: PASS, 6/6 runs. The
separate hash sweep at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-payload-matrix-74b52e39/dng-hash-comparison.json`
reported 48/48 baseline-vs-payload DNG pairs matched by length and SHA256, with
0 mismatches and 0 missing files. Aggregate payload handoff cost averaged
0.011327 ms, while wrapper elapsed averaged +515.825 ms and frame-total average
delta averaged +61.088788 ms (p95 delta averaged -133.297083 ms). This broadens
the serial payload boundary's correctness evidence to lossless-output export,
but it still does not promote a throughput win: the matrix was short, timings
remain order/noise sensitive, and `dng_compress_ms` still runs producer-side
before any async writer handoff. Next E3 promotion work should therefore focus
on stronger sampling/statistical methodology or an explicit writer-heavy
scheduler result, not on treating this lossless PASS as broad export throughput
proof.
The matching lossless identity A/A matrix at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-identity-matrix-e41e7de1/matrix-summary.json`
also passed 6/6, and its hash sweep reported 48/48 DNG pairs matched with 0
mismatches. The v2 calibration packet at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-calibration-e41e7de1/calibration.json`
first compared that identity matrix against the earlier lossless payload matrix
and reported `verdict=EXCEEDS_IDENTITY_ENVELOPE`; profile review showed that
earlier payload matrix carried multi-second `disk_write_ms` outliers in both
baseline and candidate runs, while the fresh identity run stayed near 1-2 ms.
A same-condition payload rerun at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-payload-matrix-rerun-e41e7de1/matrix-summary.json`
passed 6/6 and its hash sweep again reported 48/48 matched DNG pairs with 0
mismatches. The calibrated rerun at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-calibration-rerun-e41e7de1/calibration.json`
still reported `verdict=EXCEEDS_IDENTITY_ENVELOPE`, but the boundary narrowed:
frame-total average positive max was inside the identity envelope
(33.859437 ms feature versus 35.658738 ms identity), while frame-total p95
positive max still exceeded it (67.1932 ms feature versus 43.6239 ms identity).
Payload handoff cost averaged 0.01451 ms. This keeps the lossless payload result
strictly in the correctness/tooling bucket: byte output is stable and the
handoff itself is tiny, but the current short lossless timing envelope still
blocks throughput promotion for that path.
The broader lossless 16-frame x 3-repeat matrices at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-identity-matrix-16x3-e41e7de1/matrix-summary.json`
and
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-payload-matrix-16x3-e41e7de1/matrix-summary.json`
both passed 9/9; their hash sweeps each reported 144/144 matched DNG pairs with
0 mismatches. The v3 calibration with stage attribution at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-calibration-16x3-e41e7de1/calibration-with-stage-attribution.json`
still reported `verdict=EXCEEDS_IDENTITY_ENVELOPE`: frame-total p95 positive
max was inside identity (81.4423 ms feature versus 95.7494 ms identity), but
frame-total average positive max exceeded it (43.329938 ms feature versus
23.927175 ms identity). Stage attribution puts the feature average miss mostly
in `llrawproc_ms` (+8.180042 ms average), while `dng_compress_ms` and
`disk_write_ms` stayed inside identity and payload handoff averaged 0.016103 ms.
This strengthens the hold without pointing at payload-copy cost: the lossless
payload boundary is byte-correct, but current timing evidence still needs either
a stronger same-stage/noise model or a real scheduler win before promotion.
The matching current-release async-writer lossless matrix at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-matrix-16x3-current/matrix-summary.json`
used serial payload handoff plus `-CandidateUseAsyncWriter`,
`-CandidateAsyncWriterQueueDepth 2`, `--cdng-codec lossless`, the same three
clips/receipt, 16 frames, and 3 repeats. Its hash sweep at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-matrix-16x3-current/dng-hash-comparison.json`
reported 144/144 baseline-vs-candidate DNG pairs matched by length and SHA256,
with 0 mismatches and 0 missing files, and all cases reached
`async_writer_max_queued=2`. The v4 calibrated identity-vs-async packet at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-calibration-16x3-current-v4/calibration.json`
reported `verdict=EXCEEDS_IDENTITY_ENVELOPE` with
`blockingReasons=feature_exceeds_identity_frameTotalAvgDeltaMs,feature_exceeds_identity_frameTotalP95DeltaMs`.
Wrapper elapsed improved strongly (`elapsedDeltaMs` feature average
-7214.264222 ms, inside the identity envelope), but frame-total average and p95
deltas failed the envelope (`frameTotalAvgDeltaMs` feature average
+1043.311510 ms, feature positive max +3852.859176 ms; `frameTotalP95DeltaMs`
feature average +1612.453544 ms, feature positive max +6837.739600 ms). Stage
attribution names `writerCompletionLagAvgDeltaMs` as both the dominant positive
feature-average stage (+1565.818632 ms) and dominant positive-max excess stage
(+4281.253201 ms), with `writerQueueWaitAvgDeltaMs` also high (+876.454149 ms
feature average). The new scheduler attribution makes the split explicit:
`producerFrameAvgDeltaMs` improved by -522.507122 ms on average and
`producerQueueIdleAvgDeltaMs` stayed inside the identity envelope, while
`writerCompletionLagP95DeltaMs` was the dominant scheduler hotspot
(+3077.546722 ms feature average, +9118.773400 ms positive-max excess). This is
a useful async result but still a HOLD, not a promotion: the bytes are stable
and the writer can overlap enough to improve wrapper elapsed, but the current
completion gate says backlog/lag is part of the frame cost. Next E3 async work
should reduce queue wait/completion lag with scheduler policy before considering
any separately justified async-aware promotion gate; it should not claim success
from elapsed-only improvement.

Update 2026-06-19 Lane A E3 writer-parallel experiment: the async writer now has
a bounded opt-in worker-count knob, `MLVAPP_CDNG_EXPORT_ASYNC_WRITER_THREADS`
(`-AsyncWriterThreadCount`, surfaced through the A/B and matrix runners). The
default remains one worker. Profiler JSON records the effective
`async_writer_thread_count` plus utilization counters
(`async_writer_jobs_started`, `async_writer_jobs_finished`,
`async_writer_max_active`), and the pipeline suite includes a two-worker tiny
DNG byte-identity test before any real-footage matrix can use the knob. This is
the next measurement step for the `writerCompletionLagP95DeltaMs` blocker above,
not a promoted scheduler policy; `async_writer_max_active=1` on a multi-worker
run means the extra configured workers did not overlap actual writes.

Update 2026-06-19 Lane A E3 writer-parallel matrix: the bounded lossless
real-footage matrix at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-threads2-matrix-16x3-f4140eaa/matrix-summary.json`
used serial baseline versus payload-handoff plus async writer, queue depth 2,
writer threads 2, three 16-frame M16 cases, three repeats, alternating run
order, and frame-total regression gates. The DNG hash comparison at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-threads2-matrix-16x3-f4140eaa/dng-hash-comparison.json`
passed with 144/144 matched pairs and zero missing or mismatched DNGs. The raw
feature gate was still FAIL (8 pass / 1 fail): `m16-1347-master-lossless`
repeat 2 regressed `frame_total_ms` average by 9.076% and p95 by 39.807%.
Calibration against the same-build alternating identity matrix at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-threads2-calibration-16x3-f4140eaa/calibration.json`
reported `verdict=EXCEEDS_IDENTITY_ENVELOPE`, `identityRawGateUnstable=True`,
and `blockingReasons=feature_exceeds_identity_frameTotalAvgDeltaMs`. The
positive-max miss was small on frame-total average (`featurePositiveMax`
17.254725 ms versus identity 16.493232 ms; margin -0.761493 ms), and p95 plus
producer-frame metrics were inside the identity envelope, but writer completion
lag remained outside (`writerCompletionLagAvgDeltaMs` feature average
+2.328465 ms, positive max +4.481581 ms; `writerCompletionLagP95DeltaMs`
feature average +2.835678 ms). `candidateAsyncWriterMaxQueued` stayed 1 in all
9 runs, so the second worker did not materially engage on this workload. This
keeps two-worker async at HOLD: byte-correct and instrumented, but not a
promoted throughput policy. Next E3 work should stop chasing worker count on
this default/lossless M16 set unless a representative writer-heavy workload
actually fills the queue; prioritize either writer-utilization instrumentation
or the next higher-roadmap export bottleneck.

Update 2026-06-19 Lane A E3 writer-utilization rerun: the committed release
build `40b096942f1017b7ed4d0e80e0a2adea385fb301` first passed a one-frame
release smoke at
`.claude-state/profiling/2026-06-19-cdng-async-util-release-smoke-40b09694/profile.json`
with `async_writer_thread_count=2`, `async_writer_queue_capacity=2`,
`async_writer_jobs_started=1`, `async_writer_jobs_finished=1`, and
`async_writer_max_active=1`. The follow-on lossless M16 16-frame x 3-repeat
matrix at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-threads2-util-matrix-16x3-40b09694/matrix-summary.json`
used serial baseline versus payload-handoff plus async writer, queue depth 2,
writer threads 2, alternating run order, and frame-total regression gates.
The DNG hash comparison at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-threads2-util-matrix-16x3-40b09694/dng-hash-comparison.json`
passed with 144/144 matched pairs and zero missing or mismatched DNGs. The raw
feature gate still failed (6 pass / 3 fail): candidate jobs started/finished
were 144/144, but `candidateAsyncWriterMaxActive=1` and
`candidateAsyncWriterMaxQueued=1` in every run, writer queue wait stayed
0.000 ms (no finite FPS-equivalent), average frame total regressed
+5.039792 ms (198.421 FPS-equivalent), p95 frame total regressed
+16.953133 ms (58.986 FPS-equivalent), writer-completion lag averaged
+2.333110 ms (428.612 FPS-equivalent), writer-completion p95 averaged
+2.750689 ms (363.545 FPS-equivalent), and payload clone averaged
+0.011989 ms (83,410.565 FPS-equivalent). A same-build identity matrix at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-identity-matrix-16x3-40b09694/matrix-summary.json`
was itself strict-gate noisy (5 pass / 4 fail) but byte-identical
(`.claude-state/profiling/2026-06-19-cdng-e3-lossless-identity-matrix-16x3-40b09694/dng-hash-comparison.json`
also passed 144/144). Calibration at
`.claude-state/profiling/2026-06-19-cdng-e3-lossless-async-threads2-util-calibration-16x3-40b09694/calibration.json`
reported `verdict=EXCEEDS_IDENTITY_ENVELOPE`, `identityRawGateUnstable=True`,
and blocking reasons `feature_exceeds_identity_frameTotalAvgDeltaMs` plus
`feature_exceeds_identity_frameTotalP95DeltaMs`: frame-total average positive
max was 30.980219 ms (32.279 FPS-equivalent) versus identity 20.566243 ms
(48.624 FPS-equivalent), and frame-total p95 positive max was 100.972900 ms
(9.904 FPS-equivalent) versus identity 85.160000 ms (11.743 FPS-equivalent).
The dominant positive feature-average stage was `llrawprocAvgDeltaMs` at
+4.108379 ms (243.405 FPS-equivalent), and the dominant scheduler positive-max
excess was `producerFrameAvgDeltaMs` at +8.041832 ms (124.350
FPS-equivalent). This supersedes the earlier "add utilization counters" next
step: two-worker async remains HOLD, not a promoted throughput policy, and the
M16 lossless/default workload does not keep a second writer active. Next E3
work should stop widening writer count on this set and either use a genuinely
writer-heavy representative output scenario or move to the next export
bottleneck.

Update 2026-06-19 Lane A E3 larger-clip async probe: the largest local MLV,
`C:\temp\MLV\M29-1756.MLV`, was used as a bounded four-frame lossless-output
probe on committed release build `54787f8f` to see whether a bigger local source
creates real writer pressure. Baseline-first output lives at
`.claude-state/profiling/2026-06-19-cdng-e3-m29-lossless-async-probe-54787f8f/`;
candidate-first output lives at
`.claude-state/profiling/2026-06-19-cdng-e3-m29-lossless-async-probe-candidatefirst-54787f8f/`.
`tools/profiling/compare-cdng-dng-output-hashes.ps1 -AbSummary` and its
`-SummaryJson` alias validated both bundles as byte-identical baseline/candidate
DNGs for all 4/4 frames, writing `dng-hash-comparison.json` beside each
standalone summary. The async candidate used payload handoff, queue depth 2, and
writer threads 2, but both run orders still reported
`async_writer_max_active=1` and `async_writer_max_queued=1`. Elapsed deltas were
order-sensitive (`-1454.373 ms` baseline-first versus `-102.329 ms`
candidate-first), while frame-total averages regressed (`+10.4989 ms` and
`+19.045225 ms`) and writer-completion lag remained small (`+2.278675 ms` and
`+1.9589 ms`). This keeps async writer at HOLD on available local footage: M29
is useful coverage, but it still does not supply the representative writer-heavy
workload needed to justify scheduler-policy work. Next E3 work should either
find or construct a true writer-dominant export scenario, or move to the next
measured export bottleneck rather than widening async writer count again.

Update 2026-06-19 Lane A E3 bottleneck breakdown: export stage profiles now
include the same `llrawproc_*` substage split used by playback telemetry,
including `llrawproc_total_ms`, dark-frame/stripe/focus/bad/pattern fixes,
pre-dual-ISO fix, dual-ISO, chroma smoothing, shared/refine/publish lock time,
and `llrawproc_other_ms`. Profiles also record per-frame GPU export attempt,
return-code, replacement, and allocation telemetry, while A/B and matrix
summaries surface candidate GPU-export attempt/replacement/allocation counters
for future UltraMagnus proof packets. A rebuilt release-tree smoke on
`platform/qt/build-release/release/MLVApp.exe` at commit `3fc78aee` wrote
`.claude-state/profiling/2026-06-19-cdng-e3-gpu-telemetry-profile-smoke-3fc78aee/profile.json`
from `C:\temp\MLV\M29-1756.MLV` with two lossless-output frames. On this VM the
GPU-export counters correctly stayed inactive:
`gpu_export_attempted_frames=0`, `gpu_export_replaced_frames=0`, and
`gpu_export_max_allocated_bytes=0`; this is local fallback telemetry, not
UltraMagnus GPU proof. The measured `frame_total_ms` average was 361.06 ms
(2.77 FPS-equivalent). The dominant local bottleneck was `llrawproc_total_ms`
at 265.50 ms (3.77 FPS-equivalent), almost entirely `llrawproc_dual_iso_ms` on
this clip, followed by `dng_compress_ms` at 76.17 ms (13.13 FPS-equivalent).
`disk_write_ms` stayed small at 1.48 ms (674.90 FPS-equivalent). A one-frame
release A/B wrapper smoke at
`.claude-state/profiling/2026-06-19-cdng-e3-gpu-telemetry-ab-smoke-3fc78aee/`
reported the same zero GPU-attempt/replacement counters, preserved the new
summary fields, and passed standalone DNG hash comparison 1/1. This reinforces
the async-writer HOLD decision on available local footage: the next E3
implementation should target dual-ISO/recon scheduling and/or compression
placement/parallelism, not disk write overlap alone.

Update 2026-06-19 Lane A E3 GPU-candidate proof packet: the A/B and matrix
profiling wrappers now support per-side GPU export controls. The legacy
`-EnableGpuExport` switch still enables both baseline and candidate for
same-mode runs, while `-BaselineEnableGpuExport` and
`-CandidateEnableGpuExport` allow a clean CPU-baseline versus GPU-candidate
UltraMagnus proof packet. The local VM validation used the existing release
tree and working-tree wrapper changes only: A/B
`.claude-state/profiling/2026-06-19-cdng-e3-candidate-gpu-switch-local-2f0cade8/`
and matrix
`.claude-state/profiling/2026-06-19-cdng-e3-candidate-gpu-switch-matrix-local-2f0cade8/`
both recorded `baseline.enableGpuExport=false`,
`candidate.enableGpuExport=true`, zero local GPU attempts/replacements, and
DNG hash PASS 1/1. This is VM-local fallback/tooling proof only; real export GPU
promotion still needs an UltraMagnus run whose candidate attempts and replaces
the expected frames with matching DNG hashes.

Update 2026-06-19 Lane A E3 GPU proof gates: the same A/B and matrix wrappers
now have opt-in promotion gates for CPU-baseline/GPU-candidate runs:
`-RequireBaselineNoGpuExportAttempt`, `-RequireCandidateGpuExportAttempt`, and
`-RequireCandidateGpuExportReplacement`. The ordinary local fallback A/B
`.claude-state/profiling/2026-06-19-cdng-e3-proof-gate-pass-local-51e19138/`
still passed and its DNG hash check matched 1/1. The intentionally gated local
A/B
`.claude-state/profiling/2026-06-19-cdng-e3-proof-gate-expected-fail-local-51e19138/`
and matrix
`.claude-state/profiling/2026-06-19-cdng-e3-proof-gate-matrix-expected-fail-local-51e19138/`
failed closed because the VM candidate attempted 0/1 frames and replaced 0/1
frames. This is the desired local behavior: a real UltraMagnus export promotion
packet must pass those gates, then pass the DNG hash companion.

Update 2026-06-19 Lane A E3 UltraMagnus export wrapper:
`tools\profiling\invoke-ultramagnus-cdng-export-evidence.ps1` now stages a clean
repo over the existing SMB agent channel, builds/deploys `igpu_recon_cuda.dll`
on UltraMagnus, rebuilds the release tree there, runs the CDNG matrix wrapper as
CPU baseline versus GPU candidate, and requires baseline no-attempt, candidate
attempt/replacement, and DNG hash identity before `gpuExportValidated=true`.
The compact imported evidence packet lives under
`.claude-state\profiling\ultramagnus-cdng-export\` and carries matrix summaries,
hash comparison, release/backend hashes, and remote host/GPU context without
zipping bulky DNG payloads. This is the correct proof path for export GPU
promotion; VM-local runs remain fallback/tooling checks. If UltraMagnus lacks
the Qt/MinGW release-build tree, the wrapper fails with a durable recovery path:
rebuild the release tree locally, stage it, and rerun with `-SkipRemoteBuild`
while still building/deploying the CUDA backend and running proof gates on the
4090 host.

Update 2026-06-19 Lane A E3 GPU export skip diagnostics: export profiles now
distinguish "candidate did not attempt GPU export" from why it did not attempt.
The root profile has `gpu_export_skipped_frames` and
`gpu_export_skip_reason_counts`, and each frame records
`gpu_export_skip_code` / `gpu_export_skip_reason`. A/B summaries copy those
counts to baseline/candidate, matrix rows preserve them, and UltraMagnus proof
failures include a compact `skip_counts=...` rollup. Release-tree local smoke
`.claude-state/profiling/2026-06-19-gpu-export-skip-telemetry-smoke/` used an
existing non-DLL file as `-CandidateGpuExportDll`; the candidate stayed
byte-inert with `gpuExportAttemptedFrames=0`, `gpuExportSkippedFrames=1`, and
`backend_unavailable=1`, while the CPU baseline reported `disabled=1`. This is
still VM-local tooling proof. The next UltraMagnus packet must use the same
fields to explain any candidate 0/N attempt gate before changing receipts or
promotion criteria.

Update 2026-06-19 Lane A E3 UltraMagnus proof receipt gate: the first
skip-diagnostic UltraMagnus rerun at
`.claude-state/profiling/ultramagnus-cdng-export/imported/packet-20260619T171930/`
proved the host/backend/hash sides but failed the GPU replacement gate with
`skip_counts=missing_recon_state=4` for both uncompressed and lossless runs.
The source receipt had `dualIsoInterpolation=0`, which is outside the exporter
GPU state publisher's supported gate. The UltraMagnus wrapper now generates an
effective proof receipt from the source receipt by forcing
`dualIsoInterpolation=1`, alias-map on, full-res blending on, and chroma-smooth
off; `-UseReceiptAsIs` preserves the old raw-receipt behavior for debugging.

Update 2026-06-19 Lane A E3 UltraMagnus export proof packet: rerun
`.claude-state/profiling/ultramagnus-cdng-export/imported/packet-20260619T172721/`
passed with status `success` and `gpuExportValidated=true`. The packet ZIP is
`.claude-state/profiling/ultramagnus-cdng-export/remote-packets/ultra-magnus-20260619T172721-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `68260EFC52BFDF6D372866D0E1119BBD9FEAA1C4271764315E248A82F82243D2`).
It records host `ULTRA-MAGNUS`, `NVIDIA GeForce RTX 4090, 596.36, 24564 MiB`,
source head `538f0fe5b2268d02e801e420d752acd8503b4a40`, release executable
SHA256 `FA8B20D51113B50AA77331E77604852375B5061357017F29CC0669349E4DB8FD`,
and deployed backend DLL SHA256
`A63212BDA5C6439257D2100F9EA1A5F490A25F740FD9961325F5683552CE3D65`. Both
`uncompressed` and `lossless` cases passed: the CPU baseline attempted 0 GPU
frames and reported four `disabled` skips, the GPU candidate attempted and
replaced 4/4 frames with zero candidate skips, and the DNG hash companion passed
8/8 matched pairs. This closes the scoped 4090 export replacement/byte-identity
proof for the generated FastProxy proof receipt; it does not claim a general
throughput win. The 4-frame, single-repeat packet reported uncompressed elapsed
delta +87.802 ms (+2.466%) and lossless elapsed delta -247.457 ms (-5.627%),
so E3 throughput/pipeline promotion still needs the separate real-footage
matrix and scheduler/compression bottleneck work.

Update 2026-06-19 Lane A E3 UltraMagnus throughput probe: the larger follow-up
`.claude-state/profiling/ultramagnus-cdng-export/imported/packet-20260619T173809/`
used the same 4090 proof path on committed source head
`94f79915f62b76ef8e09b8ff9603cd7ae5379eb4`, release executable SHA256
`4B9405DED35B15A33972AF4844B11455484F806A222F527C40ACCE44634630F2`, generated
FastProxy proof receipt, `M16-1327.MLV`, both `uncompressed` and `lossless`
CDNG, `maxFrames=16`, and `repeats=3`. The packet ZIP is
`.claude-state/profiling/ultramagnus-cdng-export/remote-packets/ultra-magnus-20260619T173809-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `F2CF60E1B600D30551AA91B04D3972CF859B0788C4ABE1F1E514602ECCC2C56E`).
Correctness stayed green: 6/6 matrix runs passed, CPU baseline attempted 0 GPU
frames, the GPU candidate attempted and replaced 16/16 frames in every repeat
with zero candidate skips, and the DNG hash companion matched 96/96 pairs. The
throughput result is a blocker, not a promotion: elapsed time regressed in every
repeat. Uncompressed averaged +384.453 ms elapsed (+8.209%) and +18.359 ms
frame-total average, with the added time concentrated in `llrawproc_total_ms`
/ `llrawproc_dual_iso_ms` (+20.479 ms average). Lossless averaged +800.083 ms
elapsed (+13.759%) and +32.465 ms frame-total average, again dominated by
`llrawproc_total_ms` / `llrawproc_dual_iso_ms` (+28.917 ms / +28.896 ms
average) while compression was roughly neutral. This keeps GPU export as a
scoped replacement/parity proof, not an E3 throughput win; this packet measured
the default shadow validator, which still pays the CPU Dual-ISO oracle plus the
GPU run and byte comparison. Next E3 work should use the trusted GPU export
gate above to measure the candidate without shadow-validation cost, then either
promote only if the trusted UltraMagnus packet wins with DNG hashes green or
move to a different measured export bottleneck.

Update 2026-06-19 Lane A E3 DNG hash gate: A/B and matrix wrappers now accept
`-RequireDngHashMatch`, run the existing DNG SHA256 companion, and fold its
verdict into `summary.json` / `matrix-summary.json` as `dngHash`. Local VM
validation stayed headless and fallback-only: A/B
`.claude-state/profiling/2026-06-19-cdng-e3-dng-hash-gate-ab-pass-local-08c15d25/`
and matrix
`.claude-state/profiling/2026-06-19-cdng-e3-dng-hash-gate-matrix-pass-local-08c15d25/`
both reported `dngHash.verdict=PASS` with 1/1 matched pairs. Promotion packets
can now require candidate GPU replacement and DNG byte identity in one wrapper
verdict instead of relying on a follow-up manual hash sweep.

Update 2026-06-19 Lane A E3 compression telemetry: export profiles now record
compressed-DNG byte counts alongside `dng_compress_ms`: root
`dng_compress_bytes_valid_frames`, `dng_compress_input_bytes_total`, and
`dng_compress_output_bytes_total`, plus per-frame
`dng_compress_bytes_valid`, `dng_compress_input_bytes`, and
`dng_compress_output_bytes`. This keeps the next compression-placement or
parallelism experiment tied to byte throughput rather than timing alone.
Release-tree headless validation at commit `b3cabaf6` wrote
`.claude-state/profiling/2026-06-19-cdng-e3-compress-byte-telemetry-b3cabaf6/profile.json`
from `C:\temp\MLV\M29-1756.MLV` with two lossless-output frames:
`dng_compress_bytes_valid_frames=2`,
`dng_compress_input_bytes_total=16402176`, and
`dng_compress_output_bytes_total=7996908`. The measured `dng_compress_ms`
average was 74.98 ms (13.34 FPS-equivalent), while `frame_total_ms` averaged
387.86 ms (2.58 FPS-equivalent). This is VM-local batch telemetry only, not an
UltraMagnus GPU proof packet.

Update 2026-06-19 Lane A E3 compression summary plumbing: the export-stage
comparator now emits a `compression` object for the compressed-DNG root byte
counters, input/output MiB/s, and output ratio. A/B `summary.json` copies those
values onto `baseline`, `candidate`, and `compare`, and matrix `runs[]` rows
carry the same compression totals and throughput deltas. Future E3 proof
packets can now filter `matrix-summary.json` directly instead of opening every
raw profile to compute compression throughput by hand. Headless VM-local
tooling validation passed with DNG hash PASS 1/1 for A/B
`.claude-state/profiling/2026-06-19-cdng-e3-compression-summary-ab-smoke/`
and matrix
`.claude-state/profiling/2026-06-19-cdng-e3-compression-summary-matrix-smoke/`;
both carried `dngCompressOutputBytesTotalDelta=0` and populated
`dngCompressOutputMiBPerSecondDelta` in the generated summaries.

Update 2026-06-19 Lane A E3 compression calibration: the matrix calibration
tool now carries compression byte, output-ratio, and input/output MiB/s deltas
under a non-blocking `compressionThroughput` section. These metrics are
positive-good when throughput improves and are intentionally kept out of the
frame-regression verdict path; frame timing remains governed by the identity
envelope gates. Headless validation used one-frame identity and feature
matrices with DNG hash PASS 1/1:
`.claude-state/profiling/2026-06-19-cdng-e3-compression-calibration-identity-smoke/`
and
`.claude-state/profiling/2026-06-19-cdng-e3-compression-calibration-feature-smoke/`.
The final calibration artifact
`.claude-state/profiling/2026-06-19-cdng-e3-compression-calibration-smoke-final/calibration.json`
reported `verdict=WITHIN_IDENTITY_ENVELOPE`,
`compressionThroughput.participatesInFrameRegressionVerdict=false`, and no
blocking reasons.

Update 2026-06-19 Lane A E3 compression substage telemetry: the export profile
keeps `dng_compress_ms` as the rollup and now splits it into
`dng_compress_encode_ms`, `dng_compress_copy_ms`, and
`dng_compress_cleanup_ms`; A/B summaries, matrix rows, and calibration
attribution carry the matching `dngCompress*AvgDeltaMs` fields. Headless
VM-local schema validation, not GPU proof, wrote
`.claude-state/profiling/2026-06-19-cdng-compress-substage-smoke/profile.json`
from the checked-in tiny Dual ISO fixture with one lossless-output frame:
`dng_compress_ms=62.6934` ms (15.95 FPS-equivalent),
`dng_compress_encode_ms=61.8549` ms (16.17 FPS-equivalent),
`dng_compress_copy_ms=0.4372` ms (2287.46 FPS-equivalent), and
`dng_compress_cleanup_ms=0.3967` ms (2520.81 FPS-equivalent). Wrapper smoke
artifacts also passed DNG identity checks 1/1:
`.claude-state/profiling/2026-06-19-cdng-compress-substage-ab-smoke/summary.json`,
`.claude-state/profiling/2026-06-19-cdng-compress-substage-matrix-smoke/matrix-summary.json`,
and
`.claude-state/profiling/2026-06-19-cdng-compress-substage-calibration-smoke/calibration.json`
with `verdict=WITHIN_IDENTITY_ENVELOPE`.

Update 2026-06-19 Lane A E3 compression placement guard: export profiles now
make the current scheduler boundary explicit with
`dng_compress_placement=producer_before_payload` and
`async_writer_can_overlap_dng_compress=false`. The export-stage comparator,
A/B summaries, and matrix rows carry those fields forward, so future async
writer packets cannot infer writer-side compression overlap from elapsed-time
improvements while LJ92 encode still runs before payload enqueue. This is a
guardrail and methodology improvement only; it does not move compression to a
worker thread or claim throughput promotion.

Update 2026-06-19 Lane A E3 async-writer compression experiment: an opt-in
`MLVAPP_CDNG_EXPORT_ASYNC_WRITER_COMPRESS=1` path, surfaced by
`-UseAsyncWriterCompression` in the profiling runners, can now move
`COMPRESSED_RAW` LJ92 compression after payload enqueue onto the async writer.
The default producer-side path is unchanged. The writer-side path carries the
processed uncompressed payload, compresses it before disk write, patches the
DNG `StripByteCounts` header field, and profiles the run as
`dng_compress_placement=async_writer_after_payload` with
`async_writer_can_overlap_dng_compress=true`. This is an experimental E3
candidate surface, not a promoted throughput policy; promotion still requires
release-tree byte identity plus a bounded calibrated real-footage matrix.

The bounded calibrated matrix at
`.claude-state/profiling/2026-06-19-cdng-async-writer-compress-master-matrix-8x2-calibration/calibration.json`
used the established three M16 cases (`M16-1210`, `M16-1327`, `M16-1347`),
`C:\temp\MLV\master.marxml`, `--cdng-codec lossless`, `maxFrames=8`,
`repeats=2`, alternating order, serial baseline, and async-writer-compression
candidate with queue depth 2. The feature matrix raw wrapper passed 6/6 runs,
and both identity/feature DNG hash sweeps passed 48/48 matched pairs with zero
missing or mismatched DNGs. The candidate correctly reported
`dng_compress_placement=async_writer_after_payload`,
`async_writer_can_overlap_dng_compress=true`, and
`async_writer_compress_env_enabled=true`, but calibration still reported
`verdict=EXCEEDS_IDENTITY_ENVELOPE` with
`feature_exceeds_identity_frameTotalAvgDeltaMs` and
`feature_exceeds_identity_frameTotalP95DeltaMs`. The candidate improved average
producer-frame time by 77.433 ms, but shifted compression to the completion
gate: `writerCompletionLagAvgDeltaMs` averaged +90.833 ms and
`writerCompletionLagP95DeltaMs` averaged +103.722 ms, while feature positive
max frame-total deltas exceeded identity (`avg` +70.217 ms vs +27.914 ms;
`p95` +179.314 ms vs +23.339 ms). Async queue depth stayed effectively unused
(`candidateAsyncWriterMaxQueued=1`, `candidateAsyncWriterMaxActive=1`). This
keeps async-writer compression byte-correct but non-promoted; next E3 work
should either find a workload that actually fills the writer queue, or move to
the next export bottleneck instead of treating producer-time improvement alone
as throughput proof.

Update 2026-06-19 Lane A E3 combined proof surface: now that the trusted GPU
export measurement gate removes the CPU shadow-oracle cost, the UltraMagnus
CDNG export evidence wrapper can run candidate-only async-writer compression
through the same remote proof path using `-CandidateUseAsyncWriter`,
`-CandidateUseAsyncWriterCompression`, `-CandidateAsyncWriterQueueDepth`, and
`-CandidateAsyncWriterThreadCount`. Use this to test whether trusted GPU recon
plus writer-side LJ92 compression changes the older non-promoted lossless
result while still requiring baseline no-GPU, candidate GPU
attempt/replacement, candidate trusted frames, and DNG hash match.

The three-clip UltraMagnus combined packet at
`.claude-state/profiling/ultramagnus-cdng-export/imported/packet-20260619T181722/summary.json`
with local packet
`.claude-state/profiling/ultramagnus-cdng-export/remote-packets/ultra-magnus-20260619T181722-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `380717DC753CFD7B01DDFE1E5348DBD402F5CF259DDC00D16D27B4D72AAD2155`)
passed on `ULTRA-MAGNUS` for commit
`7367ed8bb80e5bbe7105b6ff618ac775bae5ee3c`, release SHA256
`E99F592300AC8ACA00F3B238539711D3834DB1260228590A189A9532B00933A6`.
It used `M16-1210`, `M16-1327`, and `M16-1347`, `lossless`,
`maxFrames=16`, `repeats=3`, `-TrustedGpuExport`, queue depth 2, and two
candidate writer threads. Matrix result: 9/9 PASS, DNG hash PASS 144/144, and
candidate trusted/attempted/replaced frames 144/144/144. Average wall-clock
elapsed improved from 5706.215 ms to 4445.874 ms (-1260.341 ms, -22.048%);
per-clip wall-clock deltas were M16-1210 -1403.277 ms (-22.768%), M16-1327
-1247.415 ms (-22.598%), and M16-1347 -1130.333 ms (-20.779%). The candidate
also proved actual writer overlap (`candidateAsyncWriterMaxActive=2` in every
run, max queue 1-2). Keep the candidate opt-in for now: frame-total attribution
still averaged +19.773 ms and p95 +61.750 ms because producer-frame time
improved by -102.715 ms while writer-completion lag rose +122.487 ms and
writer-side compression averaged +11.200 ms. Next E3 work is to make the
promotion gate distinguish export wall-clock throughput from async
completion-lag attribution, then decide whether this lossless candidate can
move beyond proof/experiment mode. The first gate now exists as
`-RequireElapsedImprovement` / `-MinElapsedImprovementPercent` on the A/B,
matrix, and UltraMagnus wrappers; frame-total avg/p95 regression remains a
separate attribution gate via `-FailOnRegression`.

Gate-enforced refresh: the UltraMagnus packet at
`.claude-state/profiling/ultramagnus-cdng-export/imported/packet-20260619T182703/summary.json`
with local packet
`.claude-state/profiling/ultramagnus-cdng-export/remote-packets/ultra-magnus-20260619T182703-mlvapp-cdng-export-evidence-latest.zip`
(SHA256 `28A5E842F6CB277D0900317DF8B7E1FD5754912C80FE4E06CE0E54906EC32D4A`)
reran the same three clips on commit
`b88fb04b465a4bb8af34471afae1130f74031491` with
`-RequireElapsedImprovement -MinElapsedImprovementPercent 10`. Result: 9/9
PASS, DNG hash PASS 144/144, candidate trusted/attempted/replaced frames
144/144/144, and every row cleared the 10% elapsed-improvement gate (minimum
row improvement 14.925%). Average wall-clock elapsed improved from 6570.353 ms
to 4934.366 ms (-1635.987 ms, -24.687%). Per-clip elapsed deltas were
M16-1210 -1426.806 ms (-21.689%), M16-1327 -1499.537 ms (-23.121%), and
M16-1347 -1981.617 ms (-29.251%). Frame-total attribution is now mixed but
bounded for review (overall avg +4.316 ms, p95 +12.605 ms; M16-1347 improved
frame-total while M16-1327 still carried positive completion lag), so default
promotion remains a separate policy decision.

Evidence (detail): `.claude-state/profiling/20260614-tier2-cuda/` (SUMMARY, tier2-findings,
recon-algorithm-map, recon-exact-constants, parity / parity-breadth / amaze-parity /
glinterop / optimization / full-pipeline results, integration-blueprint) and
`.claude-state/profiling/20260613-gpu-lane-x1/findings.md` (Tier 1 + x1 CPU breakdown).
Code: `tools/gpu/` (probes, parity, oracle, `igpu_recon.h`, `igpu_recon_cuda.dll`).

Proven so far: recon 0-LSB (mean23 + AMaZE dual-ISO logic), bilinear debayer 0-LSB,
CUDA AMaZE debayer parity through the DLL-gated/non-default production seam,
zero-readback CUDA->GL present (~0.1 ms), deployable ABI-validated `igpu_recon_cuda.dll`,
full pipeline ~1 ms/frame @ 4.1 MP (~988 fps) / ~9 ms @ 8K, parity across 8K/clipped/ISO.

---

## 1. Goals and non-goals

**Goals**
- Full-quality **x1 realtime** playback (the original moonshot) — no forced scale/quality compromises on capable hardware.
- **Faster CDNG export** (the repo's primary mission) with byte-exact output.
- One **backend contract** (`igpu_recon` C-ABI) so CUDA never leaks into the app; portable backends slot behind it.
- **Trustworthy UX**: quality labels mean exactly what they say; the app always shows its true active path.
- A permanent **CPU floor** — the app is fully usable with no GPU.

**Non-goals**
- Replacing the CPU path (it is the floor, not deprecated).
- Requiring a GPU.
- OpenGL-compute as the strategic portable target (it's frozen/deprecated on macOS).
- Rendered-video GPU export before processing parity lands.
- Bit-exactness *beyond* the engine's own float tolerance (its SSE2-vs-scalar variance is the reference, not a stricter bar).

## 2. Backend ladder and fallback rules

Ladder behind the single `igpu_recon` ABI:
```
CUDA      NVIDIA fast path (proven reference)
Vulkan    Win/Linux all-vendors + macOS via MoltenVK   (later)
Metal     macOS / Apple Silicon native                  (later)
CPU       universal fallback (permanent floor)
```
**Capability query** gates GPU use: *can this device do full recon + texture present with no per-frame readback?* — not merely "is a GPU present" (a weak iGPU can pass the latter and still not be worth it; the Tier 1 GL seam had a GPU and lost on readback).

**Fallback rules (all non-fatal):** missing DLL → CPU · no NVIDIA → CPU · GPU init failure → CPU · device/parity mismatch → CPU · **user selects Software → never touch the GPU** · **export always has a CPU correctness path**. No alarming modal unless the user explicitly requested GPU-only.

## 3. Lane A — GPU CDNG export (first; lowest risk, on-mission)

CDNG stores **post-recon Bayer** (debayer/processing happen later in the user's NLE), so only the **recon** stage needs the GPU — exactly the proven, ABI-wrapped, 0-LSB stage. Offline + readback-tolerant + file-diff-verifiable ⇒ the safest first production integration.
- **E0** export-stage profiler: decode / Dual-ISO recon / DNG pack / DNG compress / disk write / queue idle. Also includes an intentional CPU-export focal-plane resolution stability guard for same-process multi-frame DNG exports: frame 1 remains legacy byte-identical, while affected frames 2+ stop compounding the EXIF focal-plane denominator.
- **E1** GPU CDNG recon: CPU decode/unpack → CUDA recon (`IGPU_OUT_CPU16`) → read back Bayer16 → existing DNG writer unchanged. Behind `MLVAPP_GPU_EXPORT` / setting.
- **E2** export parity gate: CPU vs GPU exported DNGs match image payload + metadata (Look Assist defaults, resume, Dual-ISO pattern mapping, compressed + uncompressed). CPU fallback always.
- **E2 batch telemetry:** when a batch export actually attempts the CUDA shadow
  path and the backend exposes its optional VRAM query, stdout emits
  `[BATCH] GPU ... vramAllocatedMB=...` once per clip/resolution. The value is a
  backend working-set budget (tracked CUDA buffers plus the measured context
  reserve), not a WDDM per-PID reading; CPU-only and old-DLL runs stay silent.
- **E3** pipelined export: CPU decode workers → one GPU recon queue → CPU compress/write workers (never N processes fighting one GPU). A comparator for E0 export-stage profile JSONs now exists at `tools/profiling/compare-export-stage-profiles.ps1`, the profiler emits supported `queue_idle_ms`, `producer_queue_idle_ms`, `producer_frame_ms`, and `writer_completion_lag_ms` samples, `tools/profiling/run-release-cdng-export-profile.ps1` produces release-tree batch export profiles, `tools/profiling/run-release-cdng-export-profile-ab.ps1` bundles paired baseline/candidate profiles with a compare summary, `tools/profiling/run-release-cdng-export-profile-matrix.ps1` repeats those paired profiles across named cases into one matrix summary, and `dngFramePayload_t` now backs both the opt-in `MLVAPP_CDNG_EXPORT_PAYLOAD_HANDOFF=1` serial boundary and the opt-in `MLVAPP_CDNG_EXPORT_ASYNC_WRITER=1` writer-worker boundary. Async writer queue depth and worker count both default to 1 and can be raised only by opt-in env/script parameters for bounded release-tree experiments, while the profiling-only async-writer debug delay can prove queue-capacity mechanics under synthetic writer backpressure. Candidate pipeline experiments can report per-stage avg/p50/p95 deltas, scheduler idle/gap avg/p95 deltas, caller-side producer occupancy, post-producer writer lag, payload handoff cost, wrapper wall-clock elapsed-time deltas, writer thread count, queue capacity/max-queued/debug-delay, and avg/p95 frame-total regression gates across a real-footage matrix before any scheduler rewrite is promoted.
- **E4** rendered-video export: later, only after processing parity; hardware encoders (NVENC/AMF/QSV) a separate lane. The headless batch CLI now exposes an explicit `--export-format` selector so automation can request the current `cdng` path deliberately; rendered-video aliases such as `rendered-video`, `video`, `mp4`, or `prores` are recognized as the E4 request class, and `--rendered-codec` / `--rendered-container` preserve explicit codec/container intent in `BatchContext` for future runner work. The request layer also resolves one-sided rendered intent into a deterministic future target (`h264`/`h265` default to `.mp4`, `prores` and `mov` default to `.mov`, and `mp4`/`mkv` default to `h264`) while a bare `rendered-video` request fails as an incomplete target before future runner work. Complete rendered targets now also resolve to a GUI-compatible encoder preset contract (`h264` high-quality ffmpeg, `h265-8` high-quality ffmpeg, or ProRes 422 HQ ffmpeg Kostya) carrying the actual GUI codec profile/option IDs from a lightweight export-ID header, plus a conservative output-path plan that treats `--output` as a directory unless it is an explicit file whose extension matches the rendered target; explicit-file extension mismatches fail before future runner work, and once the runner has enumerated inputs, an explicit rendered output file is rejected for multi-clip jobs so directory output remains the only planned multi-clip shape. `BatchRenderedVideoJobPlan` now centralizes that future job shape by combining the request, resolved target, encoder preset, non-widget ffmpeg video-argument plan, output path plan, render settings, and current runner prerequisites; CLI validation and the batch runner both read from that plan, whose request-only preflight can be complete while `runnable=false`. The extracted ffmpeg plan covers only the already-exposed software encoder choices from the GUI path (`libx264 -crf 14`, `libx265 -crf 18 -tag:v hvc1`, and `prores_ks -profile:v 3`) and deliberately excludes filters, audio, stabilization, frame processing, pipe execution, and hardware encoders. A standalone `BatchRenderedVideoFfmpegFramePlan` now mirrors the GUI path's fps string and frame-size planning for explicit metadata inputs, including resize-height-lock math, vertical-third stretch handling, and H.264/H.265 even-dimension adjustment; `BatchRenderedVideoSourceMetadata`, `BatchRenderedVideoRenderSettings`, and a metadata-overlay job-plan helper make that second planning phase explicit without forcing early CLI validation to open the clip or invent source geometry. Render settings now carry provenance (`batch-defaults` versus future explicit headless ownership) and validation so resize-derived frame planning can fail closed without implying the runner already owns GUI dialog state. Complete rendered requests now reach a runner-side metadata preflight after static validation: the runner enumerates input MLVs, rejects multi-clip explicit-file output shapes, opens the first planned clip, overlays real width/height/fps plus receipt stretch defaults onto the job plan, reports `source-metadata-*` and `ffmpeg-frame-*` fields, and then fails closed on the existing runner prerequisite before output-directory creation or frame processing. The future headless runner therefore has explicit codec/profile/option, ffmpeg video-argument, frame-rate/frame-size, render-settings provenance, source-metadata, input cardinality, and output-file intent without pulling in the dialog widget or opting into hardware encoders. The batch runner also carries a direct fail-closed guard for non-CDNG requests, so rendered-video cannot be reached by bypassing CLI validation, and incompatible rendered request shapes such as `prores` plus `mp4` fail before runner work. All complete rendered-video requests still fail with a prerequisite blocker until rendered processing parity and a headless rendered-export runner land.

_Current E4 implementation note (2026-06-19): `BatchRenderedVideoJobPlan` now also carries a plan-only `BatchRenderedVideoFfmpegFilterPlan` for the GUI path's always-on base color-scale wrapper (`-vf scale=in_color_matrix=bt601:out_color_matrix=bt709`), narrowing the earlier blanket filter exclusion to optional filters. Optional moiree, HDR blend, stabilization, audio, frame processing, pipe execution, and hardware encoders remain outside the current headless runner contract; rendered-video requests still fail closed on the runner prerequisite._

_Current E4 implementation note (2026-06-19 resize CLI slice): the headless batch parser now accepts plan-only `--rendered-resize-width`, `--rendered-resize-height`, and `--rendered-resize-height-locked` options for rendered-video requests, validates them before runner work, and stores the resulting `BatchRenderedVideoRenderSettings` in `BatchContext` so the runner-side `BatchRenderedVideoJobPlan` can combine request, target, encoder preset, ffmpeg video/filter plans, output path, source metadata, explicit resize intent, and runner prerequisites. These switches are rejected for explicit CDNG requests; if no export format is provided, the rendered option follows the existing rendered-intent path and still fails closed before output creation or frame processing because processing parity and the headless rendered-export runner remain unimplemented._

_Current E4 implementation note (2026-06-19 prerequisite decomposition slice): `BatchRenderedVideoRunnerPrerequisites` now reports the remaining runner gates separately: processing parity, frame processing, audio muxing, ffmpeg execution, output verification, and the headless runner itself. All of those gates remain false in the current build, so complete rendered-video requests can still reach metadata/frame planning but remain `runnable=false`; this is audio/output proof-boundary scaffolding only, not audio support, ffmpeg process execution, output verification, or real rendered export._

_Current E4 implementation note (2026-06-20 command-shape slice): the central job plan now carries a plan-only `BatchRenderedVideoFfmpegCommandPlan` once real source metadata is overlaid. It mirrors the existing GUI ffmpeg rawvideo pipe shape (`ffmpeg -r ... -y -f rawvideo -s ... -pix_fmt rgb48 -i -`), the planned codec arguments, the base BT.601-to-BT.709 filter, Rec.709 color tags, and the resolved output path, while reporting audio input ownership, ffmpeg execution ownership, and output-verification ownership as false. This is command construction scaffolding only: it does not discover an ffmpeg binary, launch a process, pipe frames, mux/extract audio, verify an output file, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 ffmpeg binary-plan slice): the central job plan now also carries a `BatchRenderedVideoFfmpegBinaryPlan`. Pure planning helpers keep the deterministic default executable name (`ffmpeg`), while the live batch runner performs a PATH search with `QStandardPaths::findExecutable` and reports whether an ffmpeg binary was found plus the executable that the future command plan would use. Missing ffmpeg discovery is reported but still does not launch or block on a process because `runner-ffmpeg-execution-ready=false` remains the fail-closed execution gate. This is binary discovery/reporting scaffolding only: it does not execute ffmpeg, pipe frames, mux audio, verify outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification contract slice): the central job plan now carries a `BatchRenderedVideoOutputVerificationPlan` for the planned rendered artifact. It records the expected output path/extension and a contract-ready flag while explicitly reporting that file-existence checks, non-empty checks, media probing, codec/container validation, frame-count validation, receipt/hash proof, and verification execution are not owned yet. This is output-proof boundary scaffolding only: it does not touch the filesystem, run ffprobe, verify a rendered file, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 audio-contract slice): the central job plan now carries a `BatchRenderedVideoFfmpegAudioPlan` for the future rendered ffmpeg command. The current contract is deliberately video-only (`-an`) and reports source-audio discovery, extraction, ffmpeg audio input, muxing, and sync ownership as false while `runner-audio-mux-ready=false` remains a blocking runner prerequisite. This is audio proof-boundary scaffolding only: it does not discover source audio, extract audio, mux audio, validate sync, execute ffmpeg, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 ffmpeg override slice): the batch CLI now accepts a plan-only `--rendered-ffmpeg <path-or-name>` option and stores the trimmed request in `BatchContext`. Static rendered-plan validation preserves the requested executable, while the runner uses it for the existing ffmpeg discovery/reporting preflight so failure logs can distinguish the future command's requested binary from the default `ffmpeg` name. This remains fail-closed scaffolding: the option does not execute ffmpeg, create output directories, process frames, mux audio, verify media, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 source-audio discovery contract slice): the central rendered job plan now carries a separate `BatchRenderedVideoSourceAudioPlan` before the ffmpeg audio command contract. Static request-only planning still reports source audio as `unknown`, discovery/extraction/mux-input/sync validation as unowned, and video-only fallback as ready, which keeps the existing `-an` command shape honest before the runner opens a clip. This remains fail-closed scaffolding: static planning does not parse MLV audio blocks, extract audio, mux audio, validate sync, execute ffmpeg, verify media, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 audio-mux prerequisite contract slice): the central rendered job plan now carries `BatchRenderedVideoAudioMuxPrerequisitesPlan` between source-audio discovery and the ffmpeg audio command. It records the current video-only fallback as a ready contract while reporting source-audio discovery ownership, extraction ownership, ffmpeg audio-input ownership, mux ownership, sync validation ownership, and `audio-mux-ready` as false; `runner-audio-mux-ready=false` remains the blocking runner prerequisite. This is prerequisite-boundary scaffolding only: it does not discover source audio, extract audio, mux audio, validate sync, execute ffmpeg, verify output, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 source-audio metadata discovery slice): after the rendered runner opens the first planned MLV for source metadata, it now also overlays open-MLV audio metadata onto the central job plan. The reported `BatchRenderedVideoSourceAudioPlan` records whether source audio is present plus sample rate, channel count, bit depth, and byte count when available; the downstream audio-mux and ffmpeg-audio plans propagate source-audio discovery ownership while keeping extraction, ffmpeg audio input, muxing, sync validation, and `audio-mux-ready` false, so the planned ffmpeg command remains video-only (`-an`). This is metadata discovery/reporting scaffolding only: it does not extract audio samples, create audio temp files or ffmpeg inputs, mux audio, validate sync, execute ffmpeg, verify output media, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 source-audio extraction prerequisite slice): the central rendered job plan now carries `BatchRenderedVideoSourceAudioExtractionPlan` between source-audio discovery and audio-mux prerequisites. It derives a deterministic future `.source-audio.wav` sidecar path from the planned rendered output, records the intended PCM WAV extraction format, and reports the required source contract, sample-rate, channel-layout, and format prerequisites while keeping extraction-process ownership, temp-file ownership, cleanup ownership, and extraction readiness false. This is extraction-prerequisite/reporting scaffolding only: it does not read audio sample blocks, write temp audio files, create ffmpeg audio inputs, mux audio, validate sync, execute ffmpeg, verify output media, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 source-audio extraction execution contract slice): the central rendered job plan now carries `BatchRenderedVideoSourceAudioExtractionExecutionPlan` between source-audio extraction prerequisites and ffmpeg audio-input planning. It marks future source-audio sample reads, PCM WAV header/sample writes, temp-file lifecycle, and cleanup as planned when source audio is known/present and a sidecar path is ready, while all sample/WAV/temp/cleanup ownership and extraction readiness remain false. This is execution-boundary scaffolding only: it does not parse audio sample blocks, write WAV headers or samples, open/finalize/cleanup temp files, hand an owned input to ffmpeg, mux audio, validate sync, execute ffmpeg, verify output media, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 ffmpeg audio-input contract slice): the central rendered job plan now carries `BatchRenderedVideoFfmpegAudioInputPlan` after source-audio extraction execution planning and before audio-mux prerequisites. It reports the future planned ffmpeg audio input (`-i "<planned .source-audio.wav>"`) only when source audio is known and present, while the active ffmpeg audio command remains video-only (`-an`) and audio-input ownership/readiness, audio-mux readiness, and runner audio readiness remain false. This is input-boundary scaffolding only: it does not read audio samples, write temp audio files, hand ownership of a temp input to ffmpeg, mux audio, validate sync, execute ffmpeg, verify output media, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 ffmpeg audio-input handoff contract slice): `BatchRenderedVideoJobPlan` now inserts `BatchRenderedVideoFfmpegAudioInputHandoffPlan` between source-audio extraction execution and the ffmpeg audio-input plan. The handoff plan consumes extraction execution readiness, records the future argument handoff shape for a planned sidecar WAV when source audio is known/present, and keeps the active ffmpeg audio arguments video-only (`-an`) while extraction-output ownership, audio-input ownership, argument-handoff ownership, handoff readiness, mux readiness, ffmpeg execution, and output verification remain false. This is ownership-boundary scaffolding only: it does not parse audio samples, write WAV/temp files, transfer an owned temp input to ffmpeg, mux audio, validate sync, run ffmpeg, verify rendered output, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 audio mux/sync execution contract slice): `BatchRenderedVideoJobPlan` now carries a `BatchRenderedVideoAudioMuxExecutionPlan` after the audio-input handoff and mux-prerequisite plans. The new contract records whether source audio is known/present, whether a future mux and sync validation are planned, and whether the temp audio input handoff is ready, while keeping temp-input ownership, mux argument handoff ownership, mux execution readiness, sync validation readiness, and runner audio readiness false. This is mux/sync execution-boundary scaffolding only: it does not parse audio samples, write temp files, transfer input ownership to ffmpeg, mux audio, validate sync, run ffmpeg, verify rendered output, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 ffmpeg audio transition contract slice): `BatchRenderedVideoFfmpegAudioPlan` now consumes the mux/sync execution contract rather than only the older mux prerequisites. It keeps the active ffmpeg audio arguments video-only (`-an`) while reporting the future transition to muxed audio as planned when source audio is known/present and the handoff/mux/sync contracts are shaped. Audio input ownership, mux argument handoff ownership, mux execution readiness, muxed-command readiness, runner audio readiness, and rendered export remain false. This is command-transition scaffolding only: it does not switch ffmpeg off `-an`, parse audio samples, write temp files, transfer audio input ownership, mux audio, validate sync, run ffmpeg, verify rendered output, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 ffmpeg command audio-transition surface slice): `BatchRenderedVideoFfmpegCommandPlan` now carries forward the audio transition state from `BatchRenderedVideoFfmpegAudioPlan`, including whether muxed-audio command construction is planned and why it is still not ready. The active command arguments and command line remain video-only with `-an`, command readiness remains command-shape readiness only, and audio input ownership remains false. This is command reporting scaffolding only: it does not hand ffmpeg an owned audio input, mux audio, validate sync, execute ffmpeg, verify rendered output, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 ffmpeg execution contract slice): the central rendered job plan now carries a `BatchRenderedVideoFfmpegExecutionPlan` after the command-shape phase. Once source metadata makes the command concrete, the plan reports bound executable/command readiness and explicitly keeps process launch, stdin pipe ownership, raw-frame feeding, stderr/progress capture, exit-code validation, timeout handling, and cleanup ownership false. This is execution-boundary scaffolding only: it does not start an ffmpeg process, feed frames, create outputs, verify media, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 ffmpeg execution audio-transition surface slice): `BatchRenderedVideoFfmpegExecutionPlan` now also carries forward the command-level audio transition state, including active `-an` audio arguments, future mux transition arguments, mux/sync planning flags, and muxed-command readiness. Process launch, stdin pipe ownership, raw-frame feed ownership, audio input ownership, muxed-command readiness, execution readiness, output verification, and rendered export remain false. This is execution reporting scaffolding only: it does not start ffmpeg, hand ffmpeg an owned audio input, mux audio, validate sync, feed frames, verify rendered output, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 frame-processing contract slice): the central rendered job plan now carries a `BatchRenderedVideoFrameProcessingPlan` after source metadata and frame geometry are known. The plan reports the future RGB48 frame-processing contract, planned output size, per-phase contract readiness for receipt application, debayer/rendering, preview processing, resize processing, RGB48 frame buffering, and frame iteration, plus explicit false ownership for those phases and for processing-parity validation. Processing parity now requires that parity-validation owner in addition to the processing phases, so phase planning cannot accidentally imply parity proof. This is processing-boundary scaffolding only: it does not apply receipts, render/debayer frames, allocate or feed RGB48 frames, validate processing parity, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 receipt-application contract slice): the central rendered job plan now carries a separate `BatchRenderedVideoReceiptApplicationPlan` between rendered frame geometry and frame processing. The plan records the intended input state (`open-mlv-runtime-plus-batch-receipt`), output state (`receipt-applied-mlv-processing-state`), metadata/frame-geometry contract readiness, and explicit false ownership for `apply_to_mlv`, processing-object mutation, cache invalidation, cut/stretch state, Look Assist application, and receipt validation. `BatchRenderedVideoFrameProcessingPlan` now consumes that contract instead of treating receipt readiness as implicit. This is receipt-boundary scaffolding only: it does not parse or apply receipts, mutate `mlvObject_t`/processing state, invalidate caches, validate receipt parity, render/debayer frames, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification execution contract slice): the central rendered job plan now carries a `BatchRenderedVideoOutputVerificationExecutionPlan` after the planned output contract and ffmpeg execution contract. The plan reports the future post-ffmpeg verification source, expected output path/extension, planned `ffprobe` executable, and explicit false ownership for file-existence checks, non-empty checks, media-probe execution, codec/container validation, frame-count validation, receipt/hash validation, and verification execution readiness. This is verification-execution scaffolding only: it does not inspect the filesystem, run `ffprobe`, validate codec/container/frame counts, compare receipts or hashes, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification audio-transition surface slice): `BatchRenderedVideoOutputVerificationExecutionPlan` now carries forward the ffmpeg execution audio-transition state, including active `-an` arguments, mux-transition arguments, mux/sync planning flags, muxed-command readiness, and audio-input ownership. File existence checks, non-empty checks, media probing, codec/container validation, frame-count validation, receipt/hash validation, verification execution readiness, output verification, and rendered export remain false. This is verification reporting scaffolding only: it does not inspect the filesystem, run `ffprobe`, switch the command to muxed audio, validate sync, execute ffmpeg, verify output media, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification ffprobe binary-plan slice): the central rendered job plan now carries a `BatchRenderedVideoMediaProbeBinaryPlan` beside the planned output-verification execution contract. Static helpers keep the deterministic default probe name (`ffprobe`), while the live batch runner performs a PATH search with `QStandardPaths::findExecutable` and reports requested/resolved executable, path-search ownership/attempt state, found state, and command-contract readiness. Missing `ffprobe` discovery is reported but remains non-blocking because this slice owns only discovery/reporting; file existence checks, non-empty checks, probe execution, codec/container validation, frame-count validation, receipt/hash validation, verification execution readiness, output verification, and rendered export remain false._

_Current E4 implementation note (2026-06-20 output-verification ffprobe command-shape slice): the central rendered job plan now also carries a `BatchRenderedVideoMediaProbeCommandPlan` that derives the future `ffprobe -v error -show_format -show_streams -of json <planned-output>` command shape from the planned rendered output and probe binary plan. The contract reports command planned/ready state and mirrors that shape into `BatchRenderedVideoOutputVerificationExecutionPlan`, while probe execution ownership/readiness remains false. This is invocation-shape scaffolding only: it does not inspect the filesystem, run `ffprobe`, validate codec/container metadata, validate frame counts, compare receipts or hashes, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output file-check planning slice): the output-verification contract now explicitly records that a future rendered artifact must pass file-existence and non-empty checks, including a one-byte minimum-size threshold, and mirrors those planned checks into `BatchRenderedVideoOutputVerificationExecutionPlan`. Filesystem inspection, check ownership, check readiness, probe execution, codec/container validation, frame-count validation, receipt/hash validation, verification execution readiness, output verification, and rendered export remain false. This is file-check planning scaffolding only: it does not touch the filesystem, inspect or create an output file, run `ffprobe`, execute ffmpeg, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 codec/container validation-input slice): the output-verification contract now also derives expected probe-facing codec/container inputs from the resolved rendered target and mirrors them into `BatchRenderedVideoOutputVerificationExecutionPlan` beside the planned `ffprobe` command. This makes future codec/container validation explicit (`h265` requests expect an `hevc` probe codec, and `mkv` expects the `matroska` probe container token), while `codec-container-validation-ready`, validation ownership, probe execution, frame-count validation, receipt/hash validation, verification readiness, output verification, and rendered export all remain false. This is validation-input scaffolding only: it does not run `ffprobe`, parse media metadata, inspect files, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 frame-count/duration validation-input slice): the rendered source metadata contract now carries the opened MLV's source frame count and derived duration, and the output-verification contract mirrors those as future expected frame-count/duration inputs after metadata overlay. Request-only plans keep `expected-frame-count=0` and frame-count planning false; opened-MLV plans can report `output-verification-frame-count-planned=true` and matching execution-plan input readiness while `frame-count-validation-ready`, validation ownership, probe execution, filesystem inspection, receipt/hash validation, verification execution readiness, output verification, and rendered export remain false. This is validation-input scaffolding only: it does not run `ffprobe`, inspect files, count encoded output frames, compare output duration, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 parsed probe-result contract slice): the central rendered job plan now also carries a `BatchRenderedVideoMediaProbeResultPlan` beside the planned `ffprobe` command and output-verification execution contract. It records the future parsed-result schema for codec/container plus optional frame-count/duration fields once source metadata is available, and mirrors that shape into `BatchRenderedVideoOutputVerificationExecutionPlan`. Probe command execution ownership, JSON parsing ownership, parsed-result readiness, codec/container result readiness, frame-count/duration result readiness, filesystem inspection, validation ownership, receipt/hash validation, output verification readiness, and rendered export remain false. This is parsed-result scaffolding only: it does not inspect files, run `ffprobe`, parse media JSON, validate encoded metadata, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 probe JSON ownership/error-reporting slice): the central rendered job plan now carries a separate `BatchRenderedVideoMediaProbeJsonPlan` between the planned `ffprobe` command and parsed result contract. It defines the future raw JSON capture boundary, stdout/stderr capture, exit-code validation, timeout handling, JSON document planning, and error-reporting fields, then mirrors those into `BatchRenderedVideoOutputVerificationExecutionPlan`. Probe command execution, stdout/stderr capture ownership, exit-code validation ownership, timeout ownership, raw JSON ownership, JSON parsing ownership, raw JSON readiness, parse readiness, error-free status, filesystem inspection, encoded metadata validation, output verification readiness, and rendered export remain false. This is probe-output ownership/error-boundary scaffolding only: it does not run `ffprobe`, capture or parse JSON, inspect files, validate encoded metadata, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 optional-filter ownership contract slice): the central rendered job plan now carries a separate `BatchRenderedVideoOptionalFilterPlan` derived from the base ffmpeg filter plan. It reports the current base color-scale filter contract plus explicit false ownership for optional filter graph construction, moiree filtering, HDR blend, stabilization, optional filter ordering, optional filter parity validation, and optional filter execution readiness. This is optional-filter proof-boundary scaffolding only: it does not add headless optional-filter CLI controls, run moiree/HDR/stabilization processing, prove optional-filter parity, alter the base ffmpeg color-scale wrapper, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 parsed probe-validation boundary slice): the central rendered job plan now carries a `BatchRenderedVideoMediaProbeValidationPlan` after the parsed probe-result contract. It records future parsed-result ingestion and codec/container, frame-count, and duration comparison readiness while keeping parsed-result ingestion ownership, comparison readiness, match status, validation ownership, output verification readiness, filesystem inspection, `ffprobe` execution, receipt/hash validation, and rendered export all false. This is validation-boundary scaffolding only: it does not inspect files, run `ffprobe`, parse media JSON, compare encoded metadata, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification decision/report boundary slice): the central rendered job plan now carries a `BatchRenderedVideoOutputVerificationDecisionPlan` after the output-verification execution contract. It combines the planned output path/codec/container/frame-count expectations, file-check planning, parsed probe-validation planning, receipt/hash validation placeholder, and final accept/reject flags into one reportable boundary while keeping filesystem inspection ownership, file-check readiness, probe-validation ownership/readiness, receipt/hash ownership/readiness, verification execution readiness, decision readiness, accepted output, output verification readiness, and rendered export all false. This is decision/report scaffolding only: it does not inspect files, run `ffprobe`, parse media JSON, compare encoded metadata, validate receipts or hashes, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 receipt/hash validation contract slice): the central rendered job plan now carries a dedicated `BatchRenderedVideoReceiptHashValidationPlan` between output-verification execution and the final decision report. It records the future rendered receipt/hash comparison source, expected output path/codec/container/frame-count/duration, and planned receipt/hash comparison checks, then mirrors that contract into the output-verification execution and decision plans while keeping receipt reads, output-hash reads, comparison ownership, receipt/hash readiness, verification execution readiness, decision readiness, accepted output, output verification readiness, and rendered export all false. This is receipt/hash proof-boundary scaffolding only: it does not read sidecar receipts, compute hashes, compare metadata, inspect output files, run `ffprobe`, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification result/report contract slice): the central rendered job plan now carries `BatchRenderedVideoOutputVerificationResultPlan` after the decision contract. It consumes the planned file-check, parsed probe-validation, receipt/hash validation, verification execution, and accept/reject decision state, then reports a deterministic blocked result plus a planned failure-report boundary while keeping result-report ownership/readiness, accepted-output reporting, output verification readiness, and rendered export all false. This is result/report scaffolding only: it does not inspect files, run `ffprobe`, parse media JSON, compare encoded metadata, validate receipts or hashes, write result reports, execute ffmpeg, create outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification report artifact/path contract slice): the central rendered job plan now carries `BatchRenderedVideoOutputVerificationReportPlan` after the result/report contract. It derives the future JSON verification report identity, schema token, report kind, and sidecar path from the planned rendered output and blocked result state, while keeping report file creation, report writes, report readiness, output verification readiness, and rendered export all false. This is report-artifact scaffolding only: it does not create report files, write JSON, inspect rendered outputs, run `ffprobe`, parse media JSON, compare encoded metadata, validate receipts or hashes, execute ffmpeg, create media outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification report content/schema contract slice): the central rendered job plan now carries `BatchRenderedVideoOutputVerificationReportContentPlan` after the report artifact/path contract. It names the future JSON report top-level keys, expected-output keys, verification-check keys, artifact keys, planned error categories, and the active blocked failure category derived from the current result/report state, while keeping JSON serialization, report writes, report readiness, output verification readiness, and rendered export all false. This is content/schema-detail scaffolding only: it does not serialize JSON, create or write report files, inspect rendered outputs, run `ffprobe`, parse media JSON, compare encoded metadata, validate receipts or hashes, execute ffmpeg, create media outputs, or make rendered export runnable._

_Current E4 implementation note (2026-06-20 output-verification report writer/preflight contract slice): the central rendered job plan now carries `BatchRenderedVideoOutputVerificationReportWriterPlan` after the report content/schema-detail contract. It consumes the planned report content contract, derives the deterministic `.verification-report.json.tmp` atomic-write sidecar path, and records report encoding, write mode, serialization input readiness, parent-directory/file-creation/write/cleanup preflights, and the active blocked failure category while keeping JSON serialization, parent-directory ownership, report file creation, report writes, report readiness, output verification readiness, and rendered export all false. This is writer-preflight scaffolding only: it does not serialize JSON, create directories or files, write reports, inspect rendered outputs, run `ffprobe`, parse media JSON, compare encoded metadata, validate receipts or hashes, execute ffmpeg, create media outputs, or make rendered export runnable._

## 4. Lane B — CUDA playback

- **P-pre (quality completion):** GPU **AMaZE debayer** parity (landed behind the
  DLL gate) + GPU **processing** parity + clean x1 CPU-vs-GPU frame diff. Required
  before the GUI may claim "GPU Full Quality AMaZE" (see §8).
- **P1** loader/fallback: load `igpu_recon_cuda.dll` if present + capable, else CPU. No hard dependency. Experimental playback bridge present behind `MLVAPP_GPU_PLAYBACK_RECON=1`.
- **P2** GPU recon + CPU readback: CUDA recon → Bayer16 readback → existing CPU debayer/process/present. Integration bridge, not final UX. Implemented for the v1 proven config only; missing/unsupported backend falls back to CPU.
- **P3** no-readback playback: CPU decode/prefetch -> CUDA recon -> CUDA-to-GL R16 texture present (no per-frame CPU readback for the displayed Bayer frame) is implemented and RTX 4090-validated for the scoped raw-fixes-enabled HQ Dual ISO shape. Readback-backed Bayer16 GL presentation remains the fallback presenter for P2 output; unsupported or non-proof states stay CPU/readback.
- **P4** adaptive quality + polish: hardware-capability-driven auto quality/scale, visible A/B + frame diffs, status UI, telemetry. Status/telemetry now distinguishes CPU, GPU preview, GPU recon readback, readback-backed texture present, and true no-readback texture present; the remaining adaptive work is to promote capability-aware defaults and quality decisions without exceeding the scoped P3 gate.
- Decode (LJ92, CPU, overlapped via prefetch ~7-9 ms @ 4.1 MP) is the steady-state gate once recon is on GPU — tune the overlap.

## 5. Lane C — portable GPU (later)

CUDA stays the reference. Add backends behind the same ABI: **Vulkan** (strategic Win/Linux all-vendors + Mac via MoltenVK), **Metal** (strategic macOS). **OpenGL** = presentation (the viewport is GL; CUDA→GL present proven) + optional *tactical* Win/Linux compute bridge — not the strategic compute target. Sequenced after Lanes A/B so effort isn't fragmented; the CPU oracle validates every new backend identically (0-LSB). The runtime loader now requests a named backend instead of hardcoding `"cuda"` at the ABI call site: `MLVAPP_GPU_PLAYBACK_RECON_BACKEND`, `MLVAPP_GPU_RECON_BACKEND`, and `MLVAPP_GPU_EXPORT_BACKEND` can select a future backend name, while the default remains `cuda` and missing/unsupported names fail closed to the CPU fallback path with telemetry reporting the requested name.
The CDNG export profiling stack now carries that request through proof tooling:
`tools/profiling/run-release-cdng-export-profile.ps1`,
`tools/profiling/run-release-cdng-export-profile-ab.ps1`,
`tools/profiling/run-release-cdng-export-profile-matrix.ps1`, and the
UltraMagnus CDNG evidence wrapper accept candidate/backend-name parameters and
record the selected export backend in plan/summary JSON.
The playback proof stack mirrors this for P3 smoke/evidence:
`tools/profiling/run-release-playback-profile.ps1`,
`tools/profiling/run-release-gui-smoke.ps1`,
`tools/profiling/run-ultramagnus-p3-validation.ps1`, and
`tools/profiling/invoke-ultramagnus-p3-evidence.ps1` accept
`-GpuPlaybackReconBackend <name>` and record the requested playback recon
backend in smoke/validation/remote summaries.

## 6. Quality modes and Expert controls

**Default UI — three modes (no jargon):**
```
Playback Mode:  Auto  |  Prioritize Quality  |  Prioritize Smoothness
```
- **Auto** (default): CUDA full-quality when validated + available; CPU full-quality when paused/scrubbing/exporting if needed; reduced-scale preview only when necessary to hold cadence; shows a small status (`GPU` / `CPU` / `Preview`).
- **Prioritize Quality:** true x1, selected/Advanced debayer, **no substitution**; if CUDA can't satisfy it, fall back to **software and say so** — never silently drop to bilinear or x4.
- **Prioritize Smoothness:** reduced-scale / faster debayer allowed to keep editing responsive; clearly preview-only — paused inspection and export stay full quality unless explicitly opted out.

**Expert Playback Settings (advanced, opt-in):**
```
Playback Engine:    Auto / GPU / Software
Preview Resolution: Auto / Full 1x / 1/2 / 1/4 / 1/8
Debayer Quality:    Auto / AMaZE / RCD / Bilinear / Basic   (labeled by intent — see §7)
Dual ISO Preview:   Auto / Full HQ / Fast Preview
Fallback Behavior:  Allow automatic fallback · Warn when quality is reduced
```
Advanced users can force `Full 1x · AMaZE` and accept dropped frames (the status shows the honest fps). The main UI never forces anyone to understand CUDA, recon, readback, or x4/x8.

## 7. Debayer / scale policy

Rank by **quality tier**, not a fake exact speed ladder (the advanced demosaics are quality *tradeoffs*). Label by intent:
```
Advanced  — AMaZE (Maximum detail) · RCD/LMMSE/AHD (High quality)
Fast      — Bilinear (Fast preview)
Minimal   — Basic / None (Fastest, last-resort cadence rescue)
```
- **Auto** moves between *tiers* by sustained cadence; it does **not** micromanage AMaZE-vs-RCD (no evidence to; revisit only if that changes).
- **Prioritize Quality / Expert-forced algorithm:** use it, or fall back to **CPU** for that algorithm — never silent-substitute bilinear under a "Full Quality" label.
- **Dual-ISO interpolation** folds into the same intent: Full Quality → AMaZE/HQ dual-ISO; Performance → mean23 / reduced. Don't expose "mean23" to normal users.
- **Scale** is **graceful auto-degradation** (invisible rungs Auto uses to hold cadence on weak HW), not a chore — though Expert can pin it. On capable GPUs the cost gap nearly vanishes (even AMaZE debayer ~1-2 ms), so the tradeoff usually disappears and full quality just plays.

## 8. Validation gates

- **Recon:** 0 LSB vs CPU oracle (mean23 + AMaZE dual-ISO logic) — done. AMaZE dual-ISO's shared float demosaic core is ±1-2 LSB = the engine's own SSE2-vs-scalar variance (policy: keep that subpath on CPU for "legacy-exact," or require explicit tolerance opt-in).
- **Debayer:** bilinear 0 LSB — done; **AMaZE debayer parity** landed as a
  DLL-gated/non-default production seam and remains non-GUI until the rest of
  P-pre is reviewed.
- **Processing:** **parity = P-pre**. The current gate covers the supported
  preview-processing subset (levels / matrix / camera matrix / gamut compression
  / gamma LUT path) through CPU-vs-GPU frame diffs; broader unsupported features
  still fail closed instead of silently using GPU.
- **Export (Lane A E2):** per-frame DNG image-payload + metadata byte-diff, compressed
  + uncompressed — **implemented + RTX-4090-validated** (CUDA `igpu_recon`). The GPU
  export shadow path engages *only* for the base HQ dual-ISO config (MEAN23 + alias-map
  ON + full-res ON + chroma OFF): there it replaces the CPU output byte-identically
  (`replaced==1`, SHA256 match) across {tiny,large} × {Look-Assist off/on} ×
  {uncompressed,compressed}. Every other config (alias/full-res OFF, AMAZE, chroma-smooth
  ON) is GPU-ineligible and falls back to CPU cleanly (`run_attempted==0`, `replaced==0`,
  CPU authoritative, DNG still byte-equal). Resume/partial export is byte-identical to a
  full run (per-frame export carries no cross-frame state). Tests: `DualIsoPipeline.
  GpuExport*` in `tests/pipeline/test_dual_iso_pipeline.cpp` — eligible matrix, ineligible
  fallback, resume proxy, and missing-DLL byte-inert; the GPU-engaging cases are gated on
  `MLVAPP_GPU_EXPORT_TEST_DLL` (skip on llvmpipe, run on the 4090).
- **Playback truth:** validate by *pixels* (PrintWindow / frame diff), never FPS alone — cadence can read perfect over a frozen frame.

**Parity-coupling (the honesty linchpin):** a quality option appears in the GUI **only after its parity gate passes** — so the UX rollout is staged with the engineering:

| Stage | Engineering done | GUI may honestly offer |
|---|---|---|
| 1 | P2 / E1 (recon + bilinear, 0-LSB) | `GPU` engine; Full-Quality-AMaZE routes to **CPU**; GPU bilinear only under a labeled *Performance* mode |
| 2 | P-pre passes (AMaZE debayer + processing parity) | `GPU · Full Quality · AMaZE` becomes a true explicit path, with CPU AMaZE fallback reported instead of silent bilinear substitution |

P-pre is therefore both the engineering gate and the GUI-claim gate — it's what prevents a "Full Quality" toggle that silently isn't.

### 8.1 Processing-parity slices (extending P-pre to `allow_creative_adjustments`)

The GPU preview-processing shader today reproduces only the levels / matrix /
camera-matrix WB / gamut / gamma subset and fails closed on
`allow_creative_adjustments` (the creative grade). Extending it to full parity —
so a normally-graded clip can use the GPU path instead of falling back to CPU —
is staged as curve-first slices, because at the default image profile the
creative **contrast curve** (`pre_calc_curve_r`, built from the non-zero base
contrast params) is the only creative-family stage that is both active and
non-identity; gradation and toning execute but are identity, and
shadows/highlights and clarity are inert by default.

The post-gamma creative pipeline is ported as bit-aligned slices, in the exact
order `raw_processing.c` applies them (gamma → hue-vs → vibrance → saturation →
toning → contrast curve → gradation):

- **Slice 1 (DONE, `d77a26c6`):** creative contrast-curve LUT (`pre_calc_curve_r`)
  + gradation curves (`gcurve_*`) — 1D 16-bit LUT lookups, no spatial pass.
- **Slice 2 (DONE, `7c59d699`):** toning (per-channel `toning_dry + toning_wet`).
- **Slice 3 (DONE, `2e04516b`):** saturation (`Y1 + trunc((pix-Y1)*sat)`).
- **Slice 4 (DONE, `6ee4b4f3`):** vibrance (saturation-weighted blend).
- **Slice 5 (DONE):** hue-vs / luma-vs curves — RGB→HSV, four signed-`float[36000]`
  curve adjustments indexed by hue (`H*100`) and luma (`V*36000`), HSV→RGB. The
  curves are carried as **R32F** textures (units 11-14) so the GPU, the CPU
  reference and the production `float` curves stay bit-aligned (the uint16 LUT
  path would quantize the [-1,1] curve to ~3e-5 and break parity).
  - **Parity caveat (OOB clamp):** `hue_vs_luma` can push `V` to ≥ 1.0, after
    which `(uint16)(V*36000)` indexes `luma_vs_saturation[]` (exactly 36000
    entries) out of bounds — `V == 1.0` alone already yields index 36000. That
    read is undefined on the production CPU path, so both the GPU shader and the
    CPU reference **clamp the luma index to 35999** (the last valid sample)
    instead of reproducing undefined behaviour. This diverges from production
    only for boosted highlights (`V ≥ 1.0`) when a non-neutral
    `luma_vs_saturation` curve is set; clamping is the correct, deterministic
    behaviour and the production CPU path should adopt the same clamp (tracked
    separately — out of GPU-lane scope).
- **Slice 6 (DONE):** in-loop **simple-contrast factor** (`processing->contrast`,
  a per-pixel luma-dependent exposure multiply via `contrast_curve[cval]`,
  `raw_processing.c:2941-2954`). `cval` is the integer luma `(4R+11G+B)>>4` of the
  matrix-applied (pre camera-WB) pixel; the value is multiplied by
  `contrast_curve[cval]`. Because the factor is luma-dependent it cannot be folded
  into the per-channel matrix/gamma LUTs, so it is applied in-shader after the
  matrix sample and before the camera matrix/gamma (the scalar commutes with the
  linear WB). `contrast_curve` (`double[65536]`) is narrowed to `float` and carried
  as an R32F texture (unit 15).
- **Later slices (non-creative features, gated independently of the creative flag):**
  shadows/highlights + clarity (spatial RBF blur-mask pre-pass), then 1D/3D LUT,
  creative filter, AgX, median/RBF denoise, grain, CA correction, sharpen,
  vignette, non-Rec709 gamut.

**After slices 1-6 the creative-adjustments family is fully ported.**
`gpuPreviewProcessingIsSupported` no longer rejects `allow_creative_adjustments`
on its own — it accepts any grade (default or hand-graded: in-loop contrast +
hue-vs/luma-vs + vibrance + saturation + toning + contrast curve + gradation) and
fails closed only on the non-creative features listed above, which are gated
independently of the creative flag. The P-pre creative-parity goal (a normally
graded clip uses the GPU path instead of the CPU fallback) is met for the
creative-grade family, pending the RTX 4090 GL frame-diff that validates the
shader against this CPU reference.

Each slice keeps the CPU reference (`applyPreviewProcessingPixel`) and the GPU
subset shader bit-aligned, adds unit parity tests (the CPU reference is the local
bit-exact oracle), and is validated by a CPU-vs-GPU frame diff on the RTX 4090
before the support gate relaxes for that stage. P-pre — and the honest GUI
"GPU · Full Quality · AMaZE" claim per §8 stage 2 — completes when every creative
stage reaches parity.

### 8.2 Spatial-stages phase (completing P-pre beyond the creative family)

Update 2026-06-17: the creative family + the tractable non-creative per-pixel
stages (gamut / AgX / vignette / 1D-3D LUT) are ported and 4090-validated. The
remaining gate rejects were recon'd stage-by-stage and scoped in
[`gpu-lane-spatial-stages-scoping.md`](gpu-lane-spatial-stages-scoping.md). Key
correction to the earlier "the rest all need a blur pre-pass" assumption: most
remaining rejects are actually **per-pixel** (highlight reconstruction, gradient,
grain, creative filter) and only **chroma blur / sharpen / median** genuinely
need a neighborhood pass; **shadows/highlights, clarity, RBF denoise and CA** are
inherently **sequential** (a recursive bilateral filter / edge-window scan) and
are recommended to stay **CPU-fallback by design** (roadmap §9 honesty), not
bit-exact GPU ports. Two hard constraints drive the ranking: the shader is
**GLSL 110** (no bitwise ops/`uint`/`%` — all integer math float-emulated, which
blocks bit-exact grain and the NN sigmoid) and the **strict parity gate**
(≤16 LSB). The box blur (`blur_image`) is a separable integer box, bit-exactly
reproducible and validated in isolation before any consumer is wired. Order:
A) per-pixel bit-exact (highlight-recon → gradient); B) box-blur FBO infra +
chroma/sharpen/median; C) explicit CPU-fallback decision for the recursive
stages. See the scoping doc for the per-stage primitive table, apply-order map,
and the box-blur bit-exactness analysis.

## 9. UI truth / status language

Always surface the active path, quietly (no fake green lights, no alarms unless the user asked for GPU-only):
```
Playback: Full Quality · GPU · 1x · AMaZE
Playback: Full Quality · Software · 1x · AMaZE
Playback: Performance Preview · GPU · 1/4 · Bilinear
GPU unavailable: using software
```
Principles: **(1)** quality labels mean what they say; **(2)** preview compromises are allowed but *named* (reduced-scale / bilinear / mean23 = preview/performance); **(3)** Auto is smart but not mysterious — the status reveals the chosen path; **(4)** **export is sacred** — default deterministic + legacy-equivalent, any tolerance-based GPU path opt-in until proven; **(5)** fallback is a feature, not a failure (accelerator, not requirement); **(6)** Advanced controls exist without cluttering the main workflow.

North star: *"I can trust what I'm seeing and exporting,"* while the app quietly uses every bit of GPU speed it can safely use.

---

## Execution order (recommended)
1. **Lane A E0–E2** (GPU CDNG export, byte-exact) — lowest-risk first production use; serves the batch-export mission.
2. **P-pre** quality completion (AMaZE debayer + processing parity) — unlocks honest "GPU Full Quality" routing.
3. **Lane B P1–P4** (CUDA playback) — first explicit AMaZE playback routing, then the no-readback/user-facing realtime wins.
4. **Lane A E3–E4** (export pipeline + rendered/NVENC) and **Lane C** (Vulkan/Metal) as parallel/later tracks.

Supervised items (touch shipping `src/` or protected branch): all `src/` wiring in §3-4, and merging the work-block branch. The backend, parity harness, and this plan are ready.
