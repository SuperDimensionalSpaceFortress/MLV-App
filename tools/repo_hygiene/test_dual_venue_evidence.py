"""Tests for DUAL-VENUE-EVIDENCE-1: the venue half of the Dual-Venue Evidence framework.

WHAT IS PINNED (design: .claude-state/fleet-runs/dual-venue-design-20260930/DESIGN.md)
  * C1  the generator's DEFAULT output (bachelor / cuda) is byte-identical to master's generator;
        non-default venue / backend / Look Assist forcing are explicit, parseable variants.
  * C2  Invoke-VenueLeg.ps1 ALWAYS writes a typed receipt: VENUE_UNHEALTHY (leg NOT submitted),
        VENUE_HOST_MISMATCH, RETRACTED / UNRESOLVED from um-run, the typed refusals, and PASS.
        Roles come from venues.json; receipts are never overwritten.
  * A2  make-contact-sheet.py's side-by-side (cuda|cpu) mode pairs by frame index.
  * the leg-spec schema accepts the shipped legs and rejects a malformed one.

ROUND 2 (owner rule 2026-09-30, docs/playback-clip-length-rule.md) -- the CLIP-LENGTH CLASS:
  * a leg that PLAYS the app is addressed by a CONSENTED CLIP ID only (never a path), and the tracked
    fixtures (2 and 16 frames) are refused up front by master's own length gate, typed, before anything
    is generated or submitted -- no leg can loop, replay or play a short clip;
  * a leg whose VENUE lacks an owner-typed consent record for that clip id (venue-clip-consent.json,
    keyed by venue + clip id) refuses before submitting: consent on one venue never implies the other;
  * a receipt that says PASS or FAIL must carry the launcher's receipt-oracle verdict (source_advanced,
    required_source_frames, the run nonce, the clip id); without them it is INVALID, never PASS/FAIL.

The runner is executed for real (pwsh) with a STUB um-run.ps1 and a stub generator, so the receipt
rules run without hardware; the generator is executed for real for the byte-identity and variant tests.
Every rule has a MUTATION test: the guarding statement is taken out of a COPY of the runner and the
scenario must change, so a rule's test cannot pass with the rule removed.
Windows-only: the job generator and runner are PowerShell-on-Windows tools (the siblings gate the same way).
"""

from __future__ import annotations

import ast
import concurrent.futures
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.synthetic_mlv import FRAMES_30S_AT_23976, write_synthetic_mlv

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"
GENERATOR = ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
COMPOSER = ROOT / "tools" / "profiling" / "make-contact-sheet.py"

# The commit whose generator the DEFAULT emitted job must stay byte-identical to. UM-PRESENTMON-STOP-1 changed the
# default job on purpose (per-job PresentMon session name, clean named stop, one capture ceiling for every leg; r2:
# content-judged CSV tail repair, job-stop sufficiency arms, failed-terminate labelling, named terminate on failure
# paths); UM-PRESENTMON-STOP-2 changed it again on purpose (position-based job-stop sufficiency arms, alive-before-
# terminate / alive-before-kill exit attribution; r2: the capture-start bracket bounded by verified trace readiness,
# kill_fallback judged by .NET Kill's exit code; the probe lives in AttrCudaArtifacts.psm1), so this is that card's own
# generator commit (4c58c328: the r2 generator on the branch BEFORE the #231 merge, so it carries none of the DVE
# regions); it was 558143b1 (r2, before the probe moved into the module), 6567793196bf
# (UM-PRESENTMON-STOP-2 r1), abc10451ae2c (UM-PRESENTMON-STOP-1 r2), 8c19f442603c (r1) and before that
# PLAYBACK-CLIP-LENGTH-ENFORCE-4's merge (38ed2d8f96c2).
# DVE-PRESENTMON-EVIDENCE-1 r1 moves it again, on purpose: its item 1 (PresentMon's stdout/stderr redirected to files and published bounded, the CSV-existence record in
# presentmon-capture.json, the spawn-failure stream record) edits baseline lines of the default job IN PLACE (the Start-Process call, the two capture-json statements, the
# spawn-failure summary), which no bracketed region can express. The pin is therefore that card's own item-1 commit (2f91486c: the DVE-LEG-TERMINALS-1 generator plus item 1
# and nothing else); the card's remaining default-job text (item 2) is bracketed by its own DVE-PRESENTMON-EVIDENCE-1 sentinels and counted below. Item 1 is pinned by its own
# tests (test_dve_presentmon_evidence.py), not by byte identity; DVE-LEG-TERMINALS-1's four regions exist in the baseline too, so both sides are stripped of both families.
# UM-PRESENTMON-ORPHAN-SWEEP-1 r1 moves it again, honestly: the pin is now MASTER itself (a29a1ee4, the #233 merge this card branched from), not a card-internal commit. Every
# line the card adds to the default job (the pre-spawn orphan sweep, the post-Kill() session terminate in both stop paths, the lost-events scan and reason detail, the
# two capture-json fields, and the module helpers it splices in) is bracketed by its own UM-PRESENTMON-ORPHAN-SWEEP-1 sentinels and counted below, so NO in-place edit of
# master's text was needed: the DEFAULT job outside the brackets is byte-identical to master's, DVE-LEG-TERMINALS-1 and DVE-PRESENTMON-EVIDENCE-1 included (their regions
# exist in the baseline now, so both sides are stripped of all three families). The baseline moved from 2f91486c only because master's own DVE-PRESENTMON-EVIDENCE-1
# items 2-3 (merged since, bracketed) now sit in it.
# KEEPALIVE-FAILURE-PUBLISH-LAST-1 r2 moves it again, on purpose: that card REORDERS three baseline lines of the keep-alive body (Start-AttrCudaDisplayWakeKeepAlive,
# spliced into the default job verbatim): `$NudgeState.failureCount = ... + 1` moves from before to after the lastError / lastFailureUtc writes in the secure-screensaver,
# tick-error and catch branches. A reorder edits baseline lines IN PLACE, which no bracketed region can express (the baseline line would have to stay verbatim outside it),
# so the pin is that card's own generator commit (fc12fe8c: master e72dcdb6 plus the reorder and nothing else). That commit already carries every bracketed family, so the
# baseline counts below equal the pinned counts (they were 0 against a29a1ee4). The reorder is pinned by its own tests (test_playback_attr_3_cuda_behaviour.py), not by byte identity.
# CI-FLAKE-ATTR3-ROOT-GUARD-BEFORE-LOCK-READ-1 r2 moves it again, on purpose: that card MOVES the job's Assert-UnderMlvTmp definition and its Root/Work/Pub check from after the
# session-lock refusal (exit 30) to right after $Pub is named, before the first trace line, the display wake and the lock probe, so an outside -AgentRoot is refused (exit 1)
# whatever the host's console-lock state. A move edits baseline lines IN PLACE, which no bracketed region can express, so the pin is that card's own generator commit
# (cb795f9e: fork/master 6723cd5b plus the move and its comment, nothing else). It already carries every bracketed family, so the baseline counts below are unchanged. The move is
# pinned by its own test (test_the_root_guard_refuses_an_outside_root_before_the_session_lock_probe in test_playback_attr_3_cuda_behaviour.py), not by byte identity.
# GPU-PREF-REGISTRY-STOP-AND-PRUNE-1 r1b moves it again, on purpose: that card DELETES the job's per-exe DirectX GPU-preference registry-write line (NA-8: the owner's graphics
# state) and puts a comment block in its place. A deletion edits a baseline line IN PLACE, which no bracketed region can express, so the pin is that card's own generator commit
# (e2e4c148: master b1a4da5a plus that one edit, nothing else -- the generator's diff is exactly that hunk). It already carries every bracketed family, so the baseline counts
# for PLAYBACK-VSYNC-DEFAULT-1 and HFR-CONFORM-AWARE-RECEIPT-ORACLES-1 below now equal the pinned counts (they were 0 against cb795f9e). The absence of the registry write is
# pinned by its own test (test_na8_gpu_preference_writers), not by byte identity.
BASELINE_COMMIT = "e2e4c14830e498b98fdd0e012afb97f6fa41ba1f"

PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")
FIXTURE_IDS = ("tiny_dual_iso", "large_dual_iso")
OWNER_CLIP = "M16-1243"   # a consented clip ID (an id is not footage); the runner never sees a path
# Every leg spec shipped under legs/ (DVE-SCALE2-LOOK-LEG-1 added the scale-2 look leg); the tracked-spec tests loop over all of them.
# DUAL-VENUE-DISPLAY-MATRIX-1 added the six display-matrix legs ({fullscreen, windowed} x {scale 1, 2, 4}); legsets/display-matrix.json names them.
DISPLAY_MATRIX_LEGS = tuple(f"legs/m16-1243-display-{mode}-s{scale}.json" for scale in (1, 2, 4) for mode in ("fullscreen", "windowed"))
# PLAYBACK-BACHELOR-PRESENT-JITTER-1 added three capture-free PACE legs: speed legs that force Look Assist (cinematic) through generatorArgs and take no
# contact sheet, so no GUI-thread framebuffer grab lands inside the timed Play.
PACE_LEGS = ("legs/m16-1243-pace-cinematic-fullscreen-s4.json", "legs/m16-1243-pace-cinematic-fullscreen-s2.json", "legs/m16-1243-pace-cinematic-windowed-s4.json",
             # LOOK-LEG-PACE-TWIN-COVERAGE-1: the three display-matrix cells that had no pace twin (fullscreen s1, windowed s1, windowed s2).
             "legs/m16-1243-pace-cinematic-fullscreen-s1.json", "legs/m16-1243-pace-cinematic-windowed-s1.json", "legs/m16-1243-pace-cinematic-windowed-s2.json")
# LOOK-LEG-PACE-TWIN-COVERAGE-1: the capture-free pace twins of the four cpu-reduced look legs (playbackProcessing receipt, cpu only).
CPU_REDUCED_PACE_LEGS = tuple(f"legs/m16-1243-pace-cinematic-cpu-reduced-{mode}-s{scale}.json" for scale in (2, 4) for mode in ("fullscreen", "windowed"))
# ... and the lookahead A/B arm: the owner-shape pace leg at MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=3 (the other arm is the pace leg itself, unset).
LOOKAHEAD_LEGS = ("legs/m16-1243-pace-cinematic-fullscreen-s4-la3.json",)
# ... and (r2) the stall-stage diagnostic: the owner-shape pace leg at telemetryArm HEAVY, which keeps the per-frame playback_smoke.frame log
# (LIGHT disables it), so the present that ends a >= 250 ms interval shows its own stage times. Diagnostic only: never a pace number.
HEAVY_LEGS = ("legs/m16-1243-pace-cinematic-fullscreen-s4-heavy.json",)
# CPU-DUALISO-REDUCED-ISO-NOTCH-1: cpu-only cinematic look legs at playbackProcessing receipt, the only legs that reach the CPU reduced dual-ISO recon.
CPU_REDUCED_LEGS = tuple(f"legs/m16-1243-cpu-reduced-{mode}-s{scale}.json" for scale in (2, 4) for mode in ("fullscreen", "windowed"))
# PLAYBACK-LJ92-DECODE-THROUGHPUT-1: the K=1 arm of the raw-uint16 prefetch decoder A/B, one twin per leg it pairs with (the other arm is the leg
# itself at the app's default K=2). Each differs from its twin only in its legId and generatorArgs.rawPrefetchDecoders 1.
DECODER_LEGS = {"legs/m16-1243-pace-cinematic-fullscreen-s4-dec1.json": "legs/m16-1243-pace-cinematic-fullscreen-s4.json",
                "legs/m16-1243-pace-cinematic-fullscreen-s4-heavy-dec1.json": "legs/m16-1243-pace-cinematic-fullscreen-s4-heavy.json"}
# UPSTREAM-CPU-BASELINE-1: the fork arm of the upstream 877dea2c CPU A/B -- cpu-only SPEED legs at scale 1 with Look Assist OFF (generatorArgs.disableLookAssist),
# one per display mode upstream shares (it has no preview-scale equivalent, so no s2/s4 twin).
UPSTREAM_BASELINE_LEGS = tuple(f"legs/m16-1243-upstream-baseline-cpu-{mode}-s1.json" for mode in ("fullscreen", "windowed"))
# PLAYBACK-STARTUP-CATCHUP-BURST-1 r7: the present-trace twin of a display-matrix cell, differing only in its legId and generatorArgs.presentTrace true.
PTRACE_LEGS = {"legs/m16-1243-display-fullscreen-s4-ptrace.json": "legs/m16-1243-display-fullscreen-s4.json"}
# PLAYBACK-STARTUP-WAIT-PREWARM-1: the first-present identity twins of two display-matrix cells, differing only in their legId and
# generatorArgs.firstPresentCapture true.
IDENTITY_LEGS = {f"legs/m16-1243-display-{cell}-identity.json": f"legs/m16-1243-display-{cell}.json" for cell in ("windowed-s1", "fullscreen-s4")}
# PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 F-diag: the warm-reuse miss-dump twin of the windowed s1 cell, differing only in its legId and
# generatorArgs.warmReuseMissDump true.
MISSDUMP_LEGS = {"legs/m16-1243-display-windowed-s1-missdump.json": "legs/m16-1243-display-windowed-s1.json"}
# PLAYBACK-VISUAL-INSPECTION-1: the window-capture proof leg, a SPEED leg (windowed, scale 2) that names no visualCapture: absent means on.
VISUAL_LEGS = ("legs/m16-1243-speed-windowed-s2.json",)
# PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1: the display matrix cells are LOOK legs, which never capture; these speed twins of the proof leg
# (only displayMode and scaleFactor differ) carry the window capture to every {fullscreen, windowed} x {s1, s2, s4} cell.
VISUAL_TWIN_LEGS = tuple(f"legs/m16-1243-speed-{mode}-s{scale}.json" for mode in ("fullscreen", "windowed") for scale in (1, 2, 4)
                         if (mode, scale) != ("windowed", 2))
# PLAYBACK-SCOPES-OPTION-1: the windowed scale-1 no-look scope matrix, per backend: N (no scope), T (histogram at the app's default throttle) and
# F (histogram at scopeIntervalMs 0, upstream's every frame), plus one optional waveform F arm on cuda. Each scope arm differs from its backend's N
# leg only in its legId and the scope fields of generatorArgs. r4 (PSO-01): every scope arm pins scopesDuringPlayback throttled, the mode the
# r1-r3 arms measured on the pre-setting staged build (the fork throttle; i0 is that throttle at base 0), so a Hide-default build cannot turn a
# T or F arm into N.
SCOPE_ARMS = {"histogram": {"scope": "histogram", "scopesDuringPlayback": "throttled"},
              "histogram-i0": {"scope": "histogram", "scopeIntervalMs": 0, "scopesDuringPlayback": "throttled"}}
SCOPE_LEGS = {f"legs/m16-1243-scopes-{backend}-windowed-s1-{arm}.json": (backend, extra)
              for backend in ("cpu", "cuda") for arm, extra in (("none", {}), *SCOPE_ARMS.items())}
SCOPE_LEGS["legs/m16-1243-scopes-cuda-windowed-s1-waveform-i0.json"] = (
    "cuda", {"scope": "waveform", "scopeIntervalMs": 0, "scopesDuringPlayback": "throttled"})
# PLAYBACK-SCOPES-OPTION-1 r3: the Look-Assist-ON twins of the N and T arms (the shipping default is Look Assist on): the no-look twin with
# generatorArgs.forceLookAssist true (a capture-free pace leg at the default Classic flavor) in place of disableLookAssist true.
SCOPE_LA_LEGS = tuple(f"legs/m16-1243-scopes-{backend}-windowed-s1-{arm}-la.json" for backend in ("cpu", "cuda") for arm in ("none", "histogram"))
# CUDA-HEADROOM-1: the unpaced throughput legs (both clips, the D4 cells) and the stage-attribution legs (HEAVY telemetry + CUDA launch timing).
HEADROOM_LEGS = (*(f"legs/{clip}-headroom-unpaced-{mode}-s{scale}.json" for clip in ("m16-1243", "m16-1457") for mode in ("fullscreen", "windowed") for scale in (1, 2, 4)),
                 *(f"legs/m16-1243-headroom-stage-{mode}-s{scale}.json" for mode in ("fullscreen", "windowed") for scale in (1, 2, 4)),
                 # CUDA-HEADROOM-HFR-1 r2: the M16-1457 stage leg at s2/s4 (the s1 spec with only legId and scaleFactor changed).
                 *(f"legs/m16-1457-headroom-stage-fullscreen-s{scale}.json" for scale in (1, 2, 4)))
# BACHELOR-CORE-PARTITION-AB-1: the core-partition legs (A whole machine, B1 P-cores, B2 P-cores + E-core load): the headroom unpaced windowed s1 shape
# (a Cinematic Look-Assist pace leg at swap interval 0, so they carry the same presentTearingExpectedReason) plus the core-partition generatorArgs.
PARTITION_LEGS = tuple(f"legs/m16-1243-partition-{cell}-unpaced-windowed-s1.json" for cell in ("a", "b1-pcore", "b2-pcore-eload",
                                                                                              # VENUE-LEG-PLAYBACK-MAX-THREADS-ARG-1: A and B1 at a worker-thread cap of 12
                                                                                              "a12", "b1-12-pcore"))
# LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1 added the scale-2 Cinematic twin of the scale-2 look leg (the Bachelor CPU Classic | Cinematic look pair).
SHIPPED_LEGS = ("legs/m16-1243-speed.json", "legs/m16-1243-look.json", "legs/m16-1243-look-scale2.json", "legs/m16-1243-look-cinematic.json", *DISPLAY_MATRIX_LEGS, *PACE_LEGS,
                *LOOKAHEAD_LEGS, *HEAVY_LEGS, *DECODER_LEGS, "legs/m16-1243-look-scale2-cinematic.json", "legs/m16-1243-look-film.json",
                "legs/m16-1243-look-scale2-film.json", "legs/m16-1243-look-scale2-vision50d.json",
                *(f"legs/m16-1243-look-scale2-{flavor}-agxoff.json" for flavor in ("cinematic", "film", "vision50d")),
                *(f"legs/m16-1243-display-{mode}-s{scale}-vsync1.json" for scale in (1, 2, 4) for mode in ("fullscreen", "windowed")),
                *CPU_REDUCED_LEGS, *CPU_REDUCED_PACE_LEGS, *UPSTREAM_BASELINE_LEGS, *PTRACE_LEGS, *VISUAL_LEGS, *VISUAL_TWIN_LEGS, *SCOPE_LEGS, *SCOPE_LA_LEGS, *HEADROOM_LEGS, *PARTITION_LEGS,
                *IDENTITY_LEGS, *MISSDUMP_LEGS)
# PLAYBACK-VSYNC-DEFAULT-1: the swap-interval-1 twins of the six display-matrix legs (the vsync A/B; the originals stay at the app's default 0).
VSYNC1_LEGS = tuple(rel.replace(".json", "-vsync1.json") for rel in DISPLAY_MATRIX_LEGS)
# LOOK-ASSIST-FILM-FLAVOR-2 r2: the AgX-off twins of the scale-2 Cinematic and Film legs carry a committed look receipt (look-receipts/agx-off.marxml).
AGXOFF_LEGS = {f"m16-1243-look-scale2-{flavor}-agxoff": f"m16-1243-look-scale2-{flavor}" for flavor in ("cinematic", "film", "vision50d")}
LOOK_RECEIPT_BASE = "6b6f66f52d5344a97de5068b2e7cae1a61edf295"   # the #328 head whose generator, legs and runner this round extends
# GPU-DEVICE-LOSS-SELF-HEAL-1 r2: the L1..L4 self-heal proof, ONE cuda speed leg whose job runs three pre-launches and the measured run on one settings store.
SELF_HEAL_LEG = "legs/m16-1243-gpu-self-heal-l1-l4.json"
SHIPPED_LEGS = (*SHIPPED_LEGS, SELF_HEAL_LEG)
# VENUE-LEG-AGXOFF-UNPACED-HEADROOM-1: the s2 Cinematic AgX-off look leg run unpaced at swap interval 0 (the s2 headroom under the pace cap), keyed to its
# paced source. A look leg, because only a look leg carries a look receipt; not in AGXOFF_LEGS, whose members are byte copies of their source.
AGXOFF_UNPACED_LEGS = {"legs/m16-1243-look-scale2-cinematic-agxoff-unpaced.json": "legs/m16-1243-look-scale2-cinematic-agxoff.json"}
SHIPPED_LEGS = (*SHIPPED_LEGS, *AGXOFF_UNPACED_LEGS)
# VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: the capture-free twin of the unpaced AgX-off look leg (a pace leg carrying the same look receipt in
# generatorArgs), and the #680 fast-path kill-switch arm of each (generatorArgs.disablePrepPointwiseFast), keyed to the leg each one twins or kills.
AGXOFF_UNPACED_PACE_LEG = "legs/m16-1243-pace-cinematic-agxoff-unpaced.json"
AGXOFF_UNPACED_NOFAST_LEGS = {"legs/m16-1243-look-scale2-cinematic-agxoff-unpaced-nofast.json": "legs/m16-1243-look-scale2-cinematic-agxoff-unpaced.json",
                              "legs/m16-1243-pace-cinematic-agxoff-unpaced-nofast.json": AGXOFF_UNPACED_PACE_LEG}
AGXOFF_REMEASURE_LEGS = (AGXOFF_UNPACED_PACE_LEG, *AGXOFF_UNPACED_NOFAST_LEGS)
SHIPPED_LEGS = (*SHIPPED_LEGS, *AGXOFF_REMEASURE_LEGS)
# PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1: the one leg that BOTH routes a cpu leg's frames down the Processed8 branch (playbackProcessing receipt) AND keeps the
# per-frame playback_smoke.frame rows (telemetryArm HEAVY): the cpu-reduced pace twin at fullscreen s4 with the frame log kept.
CPU_RECEIPT_HEAVY_LEG = "legs/m16-1243-pace-cinematic-cpu-receipt-fullscreen-s4-heavy.json"
SHIPPED_LEGS = (*SHIPPED_LEGS, CPU_RECEIPT_HEAVY_LEG)
# PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1: the ON arm of the #397 prep-conflated-move A/B (generatorArgs.prepConflatedMove true -> MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE=1),
# one per capture-free pace cell the CPU route runs at s2 and s4 (a speed leg, so the post-play visual strips are on by default), keyed to the cell it twins. The
# OFF arm is the twinned cell itself (env unset).
PREP_CONFLATED_MOVE_LEGS = {f"legs/m16-1243-pace-cinematic-{mode}-s{scale}-pcm.json": f"legs/m16-1243-pace-cinematic-{mode}-s{scale}.json"
                            for mode in ("fullscreen", "windowed") for scale in (2, 4)}
SHIPPED_LEGS = (*SHIPPED_LEGS, *PREP_CONFLATED_MOVE_LEGS)
SHIPPED_LEGS_PS =", ".join(f"'{rel}'" for rel in SHIPPED_LEGS)
OTHER_CLIP = "Z99-9999"
MLV_EXT = "." + "mlv"  # never spelled as one literal token (the NA-4 gate trips on fixture basenames)
LAUNCHER = ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"


def run_pwsh(args: list[str], env_extra: dict | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def run_pwsh_many(arg_lists: list[list[str]], env_extra: dict | None = None, timeout: int = 600) -> list[subprocess.CompletedProcess]:
    """run_pwsh for several independent invocations at once (a few pwsh at a time, never more than the host has cores), results in input order.
    Only for runs that touch nothing in common: each is its own process with its own output file."""
    import os
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(4, os.cpu_count() or 1))) as pool:
        return list(pool.map(lambda args: run_pwsh(args, env_extra, timeout), arg_lists))


def launcher_nonce_expression(launcher_text: str) -> str:
    """The right-hand side of the launcher's `$runNonce = ...` line: the REAL producer of every run nonce the app echoes
    (round 3, fable BLOCKER: the rule was written against a hand-made constant and rejected what the launcher mints)."""
    found = re.findall(r"(?m)^\$runNonce = (.+?)\s*$", launcher_text)
    if len(found) != 1:
        raise AssertionError(f"the launcher must mint its run nonce on exactly one `$runNonce = ...` line, found {len(found)}")
    return found[0]


def mint_run_nonce(launcher_text: str | None = None) -> str:
    """Evaluate the launcher's own nonce expression (or that of a mutated COPY of the launcher text)."""
    text = launcher_text if launcher_text is not None else LAUNCHER.read_text(encoding="utf-8")
    proc = run_pwsh(["-Command", launcher_nonce_expression(text)])
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        raise AssertionError("the launcher's nonce expression did not evaluate: " + proc.stdout + proc.stderr)
    return out


NONCE = mint_run_nonce() if (PWSH and sys.platform == "win32") else "0" * 32


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def lf(text: str) -> str:
    return text.replace("\r\n", "\n")


def good_block(**over) -> dict:
    """A VALID receipt `playback` block (the shape Get-DvPlaybackEvidence writes), for tests of the receipt rules themselves."""
    block = {"sourceAdvanced": 960, "requiredSourceFrames": 600, "nativeFps": 23.976, "paceFps": 23.976, "fpsOverride": 0, "wrapped": False,
             "wrapCount": 0, "expectedRunNonce": NONCE, "observedRunNonce": NONCE, "manifestRunNonce": NONCE, "logSha256": "ab" * 32,
             "logShaBound": True, "settingsIsolated": True, "jobSourceAdvanced": 960, "jobRequiredSourceFrames": 600, "jobFailures": [],
             "fixtureRehearsal": False, "clipId": OWNER_CLIP}
    block.update(over)
    return block


# The same valid block as a PowerShell literal (for tests that build a receipt inside pwsh).
PS_GOOD_BLOCK = ("[ordered]@{ sourceAdvanced = 960; requiredSourceFrames = 600; nativeFps = 23.976; paceFps = 23.976; fpsOverride = 0; wrapped = $false; "
                 "wrapCount = 0; expectedRunNonce = '" + NONCE + "'; observedRunNonce = '" + NONCE + "'; manifestRunNonce = '" + NONCE + "'; "
                 "logShaBound = $true; settingsIsolated = $true; jobFailures = @(); fixtureRehearsal = $false; clipId = '" + OWNER_CLIP + "' }")


# ---------------------------------------------------------------------------------------------------
@requires_windows_pwsh
class GeneratorByteIdentityAndVariantTests(unittest.TestCase):
    """The generator refuses a fixture id whose tracked header is under 20 s (master, ENFORCE-1), so these tests run
    it against a sparse CLONE whose two fixture files are header-only ~30 s stand-ins (never real footage), the same
    device master's own generator tests use."""

    warm_first = True   # the first `generate` of the class runs every request on `warm_requests` at once; read from the class's OWN __dict__ (see `generate`), so a subclass or a borrower never inherits it

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="dve-gen-")
        cls.tmp = Path(cls._tmp.name)
        cls._generated = {}   # (script, arguments) -> (job file, the generator's CompletedProcess); see generate / warm_generated
        cls._parse_cache = {}   # job file -> parse error count
        cls._warmed = False
        cls.head = git("rev-parse", "HEAD")
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        cls.baseline_available = subprocess.run(
            ["git", "-C", str(ROOT), "cat-file", "-e", f"{BASELINE_COMMIT}^{{commit}}"], capture_output=True).returncode == 0
        cls.baseline_root = cls.tmp / "baseline"
        if cls.baseline_available:
            tar = cls.tmp / "baseline.tar"
            subprocess.run(["git", "-C", str(ROOT), "archive", BASELINE_COMMIT, "--format=tar", "-o", str(tar),
                            "tools/profiling", "tools/gates"], check=True)
            cls.baseline_root.mkdir()
            subprocess.run(["tar", "-xf", str(tar), "-C", str(cls.baseline_root)], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    @classmethod
    def generator_command(cls, script: Path, out: Path, extra: list[str]) -> list[str]:
        return ["-File", str(script), "-SourceCommit", cls.head, "-BuildManifestSha256", "ab" * 32,
                "-ClipId", FIXTURE_IDS[0], "-FixtureSha256", "cd" * 32, "-RepoRoot", str(cls.repo),
                "-OutFile", str(out), *extra]

    @classmethod
    def warm_requests(cls) -> list[tuple[Path, list[str]]]:
        """Every distinct (script, arguments) the tests below ask `generate` for. A generator run is ~5 s (a pwsh start plus a 560 KB job to emit), so the first
        `generate` of the class runs all of these at once, a few pwsh at a time, and each test then takes its job from the cache. This list is only an accelerator:
        a request that is not on it is generated when a test asks for it (and cached), so a test added without extending it is correct, just not warmed."""
        cs4 = ["-ContactSheet", "-ContactSheetFrames", "4"]
        look = ["-ForceLookAssist", "-ContactSheet", "-LookFlavor", "cinematic"]
        pace = ["-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic"]
        heavy = ["-TelemetryArm", "HEAVY"]
        store = [*heavy, "-AutomationSettingsDir", "gpu-self-heal-settings"]
        cpu1 = ["-Backend", "cpu", "-ScaleFactor", "1"]
        scope_base = [*cpu1, "-DisplayMode", "windowed", "-DisableLookAssist"]
        byte_identical = [[], [*cs4, "-TelemetryArm", "LIGHT", "-DisablePaintPerSubmit"], ["-PlaySeconds", "30"]]
        requests: list[tuple[Path, list[str]]] = [(GENERATOR, extra) for extra in byte_identical]
        if cls.baseline_available:
            old = cls.baseline_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
            requests += [(old, extra) for extra in byte_identical]
        for extra in (["-Venue", "bachelor", "-Backend", "cuda", "-ScaleFactor", "4", "-CpuQuiescenceThresholdPercent", "20.0"], ["-ExpectedScaleRequest", "1"],
                      cs4, [*cs4, "-DisplayMode", "fullscreen"], [*cs4, "-DisplayMode", "windowed"], ["-DisplayMode", "windowed"],
                      ["-ContactSheet", "-ContactSheetPairedSeek", "-DisplayMode", "windowed"], ["-Venue", "ultra-magnus"], ["-Backend", "cpu"], look, pace,
                      [*pace, "-PlaybackRenderLookaheadFrames", "3"],
                      *([*extra, "-SwapInterval", interval] for interval in ("0", "1") for extra in ([], look, [*look, "-DisplayMode", "windowed"])),
                      *(["-CudaLaunchTimingMinMs", ms] for ms in ("0", "2", "2.5")), ["-SwapInterval", "1", "-CudaLaunchTimingMinMs", "2"],
                      ["-PresentTrace"], ["-GpuFaultInject", "702@120"], ["-GpuSafeStart", "clear"], heavy, store,
                      [*store, "-GpuPreLaunches", "702@120,702@120,none", "-GpuSafeStart", "clear"], ["-FirstPresentCapture"], ["-Unpaced"],
                      ["-Unpaced", "-PresentTrace", "-SwapInterval", "0"], ["-Backend", "cpu", *cs4], ["-Backend", "cpu", *cs4, "-PlaybackProcessing", "receipt"],
                      ["-Backend", "cpu", *cs4, "-PlaybackProcessing", ""], cpu1, [*cpu1, "-DisableLookAssist"],
                      [*cpu1, "-DisplayMode", "windowed", "-DisableLookAssist"], scope_base, [*scope_base, "-Scope", "none"], [*scope_base, "-Scope", "histogram"],
                      [*scope_base, "-Scope", "histogram", "-ScopeIntervalMs", "0"],
                      ["-ScaleFactor", "1", "-DisplayMode", "windowed", "-DisableLookAssist", "-Scope", "waveform"],
                      [*scope_base, "-Scope", "histogram"], [*scope_base, "-Scope", "histogram", "-ScopesDuringPlayback", "throttled"],
                      ["-RawPrefetchDecoders", "0"], ["-RawPrefetchDecoders", "2"], [*pace, "-RawPrefetchDecoders", "1"],
                      ["-ScaleFactor", "1", "-CpuQuiescenceThresholdPercent", "35.5"], ["-Venue", "ultra-magnus", "-PlaySeconds", "28"],
                      ["-Backend", "cpu", "-PlaySeconds", "28"]):
            requests.append((GENERATOR, extra))
        return requests

    @classmethod
    def warm_generated(cls) -> None:
        """Run `warm_requests` concurrently (every run is its own pwsh writing its own file) and parse-check the jobs in one more pwsh. The outcomes are kept
        unasserted: `generate` asserts the exit code of the run its test asked for, exactly as before, so a failure is still reported by that test."""
        unique: dict[tuple[str, tuple[str, ...]], tuple[Path, list[str]]] = {}
        for script, extra in cls.warm_requests():
            unique.setdefault((str(script), tuple(extra)), (script, extra))
        jobs = [(key, script, extra, cls.tmp / f"warm-{n}.job.ps1") for n, (key, (script, extra)) in enumerate(unique.items())]
        procs = run_pwsh_many([cls.generator_command(script, out, extra) for _key, script, extra, out in jobs])
        for (key, _script, _extra, out), proc in zip(jobs, procs):
            cls._generated[key] = (out, proc)
        ok = [str(out) for (_k, _s, _e, out), proc in zip(jobs, procs) if proc.returncode == 0 and out.exists()]
        script = ("foreach ($f in ($env:DVE_PARSE_FILES -split '\\|')) { $t=$null;$e=$null;"
                  "[void][System.Management.Automation.Language.Parser]::ParseFile($f,[ref]$t,[ref]$e);Write-Output ($f + '|' + $e.Count) }")
        parsed = run_pwsh(["-Command", script], env_extra={"DVE_PARSE_FILES": "|".join(ok)}) if ok else None
        if parsed is not None and parsed.returncode == 0:
            for line in parsed.stdout.splitlines():
                path, _sep, count = line.rpartition("|")
                if path and count.strip().isdigit():
                    cls._parse_cache[path] = int(count)

    def generate(self, script: Path, out_name: str, extra: list[str]) -> Path:
        """The same script and arguments emit the same bytes whatever the output file is called, so a request is run once per class (see `warm_requests`).
        The warm-up is opt-in per class: only a class that sets `warm_first = True` in its own body runs it. A subclass (CpuGeneratedJobs.Boot asks for ~5 jobs) or a
        borrower that did not opt in would otherwise pay the whole ~60-job warm-up on its first request, and so would any future subclass."""
        cls = type(self)
        if cls.__dict__.get("warm_first", False) and not cls._warmed:
            cls._warmed = True
            cls.warm_generated()
        key = (str(script), tuple(extra))
        if key not in cls._generated:
            out = self.tmp / f"gen-{len(cls._generated)}-{out_name}"   # (unique: a cached job is never overwritten by a later request that reuses a name)
            cls._generated[key] = (out, run_pwsh(self.generator_command(script, out, extra)))
        out, proc = cls._generated[key]
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out

    # DVE-LEG-TERMINALS-1: the ONLY text that card adds to the default job is bracketed by these two sentinel lines in the generator (the PresentMon wait-failure
    # counters and frame publish, and the SMOKE_RUN_FAILED evidence publish: four regions). The default job is byte-identical to the baseline's once exactly those
    # regions are removed -- so every other byte is still master's, and a region cannot grow past its brackets unnoticed (the count and the shape are pinned below).
    LEG_TERMINALS_OPEN = "DVE-LEG-TERMINALS-1 >>>"
    LEG_TERMINALS_CLOSE = "DVE-LEG-TERMINALS-1 <<<"
    # DVE-PRESENTMON-EVIDENCE-1: the text that card adds to the default job's decision flow beyond the baseline's in-place item 1 (one region: the display-report-failure
    # branch's frame publish). The count is pinned so a region cannot silently multiply.
    PRESENTMON_EVIDENCE_OPEN = "DVE-PRESENTMON-EVIDENCE-1 >>>"
    PRESENTMON_EVIDENCE_CLOSE = "DVE-PRESENTMON-EVIDENCE-1 <<<"
    PRESENTMON_EVIDENCE_REGIONS = 1
    # UM-PRESENTMON-ORPHAN-SWEEP-1: every line that card adds to the default job (and the module helpers it splices in) is bracketed by these sentinels; the count is pinned so a
    # region cannot silently multiply or grow outside its brackets (test_um_presentmon_orphan_sweep.py pins what the regions hold).
    ORPHAN_SWEEP_OPEN = "UM-PRESENTMON-ORPHAN-SWEEP-1 >>>"
    ORPHAN_SWEEP_CLOSE = "UM-PRESENTMON-ORPHAN-SWEEP-1 <<<"
    # 23 = 22 in the job template plus the one module-helper splice; r1 had 16 (15 + 1): r2 adds the per-action liveness helpers and listing retry (one region), the logman-path/timeout variables (inside the existing init region),
    # the PRESENTMON_TIMEOUT reason detail, and the post-Kill terminate trace + summary field at each of the three Stop call sites.
    ORPHAN_SWEEP_REGIONS = 23
    # CONTACT-SHEET-PLAYBACK-PARITY-1: the in-pass contact sheet (the $ContactSheetPairedSeek variable, the AdditionalArgs override that drops
    # --contact-sheet-seek-mode, and the paired-seek publish). Three regions; the baseline lines they supersede stay verbatim outside them.
    CONTACT_SHEET_PARITY_OPEN = "CONTACT-SHEET-PLAYBACK-PARITY-1 >>>"
    CONTACT_SHEET_PARITY_CLOSE = "CONTACT-SHEET-PLAYBACK-PARITY-1 <<<"
    CONTACT_SHEET_PARITY_REGIONS = 3
    # CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1: the one line (and its comment) that card adds to Stop-AttrCudaDisplayWakeKeepAlive, which the generator splices into the default job
    # verbatim: after a timed-out wait the stuck pipeline's handle is dropped so the baseline's Dispose/Close lines (left verbatim outside the brackets) become a no-op. One region.
    KEEPALIVE_HUNG_PROBE_OPEN = "CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1 >>>"
    KEEPALIVE_HUNG_PROBE_CLOSE = "CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1 <<<"
    KEEPALIVE_HUNG_PROBE_REGIONS = 1
    # VENUE-SESSION-LOCKED-REFUSAL-1: the console-lock refusal. 14 regions: the Get-AttrCudaSessionLocked splice (1); in Start-AttrCudaDisplayWake the help text,
    # the lock read, the open and the close of the wrapper around the unchanged nudge branches, the dismissAttempted override, the method/dismissFailed
    # override and the sessionLocked field (7); in the keep-alive the help text, the per-tick read, the wrapper's open and close, and the runspace entry (5);
    # and the job's exit-30 gate (1). Every baseline line stays verbatim outside them.
    # r2 (sol r1b blockers) adds 18: the native in-thread lock read (P/Invokes, result fields, the read before SendInput: 3); the two result fields on
    # Invoke-AttrCudaInputDesktopNudge's three branches (3); in Start-AttrCudaDisplayWake the dedicated-thread refusal and the read before the plain
    # SendInput (2); in the keep-alive the two nudgeState declarations and the in-thread refusal (3); sessionLockReason in the health read's three
    # shapes and its computation (4); and the owner-only branch at the job's three keep-alive checkpoints (3).
    SESSION_LOCKED_OPEN = "VENUE-SESSION-LOCKED-REFUSAL-1 >>>"
    SESSION_LOCKED_CLOSE = "VENUE-SESSION-LOCKED-REFUSAL-1 <<<"
    SESSION_LOCKED_REGIONS = 32
    # PLAYBACK-VSYNC-DEFAULT-1: the PresentMon SyncInterval/AllowsTearing counts Get-AttrCudaPresentMonDisplayReport adds (the generator splices the function into the
    # default job verbatim). Four regions: the column flags, the two parsed-row fields, the per-chain counts and the selected-chain counts. Absent from the baseline.
    PLAYBACK_VSYNC_OPEN = "PLAYBACK-VSYNC-DEFAULT-1 >>>"
    PLAYBACK_VSYNC_CLOSE = "PLAYBACK-VSYNC-DEFAULT-1 <<<"
    PLAYBACK_VSYNC_REGIONS = 4
    # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: the conform-aware source-frame verdict (Get-AttrCudaConformAwareSourceFramesVerdict, a wrapper that calls the unchanged baseline
    # oracle). Three regions: the module-helper splice, the job's conform-line pick and its override call, and the three receipt fields. Absent from the baseline.
    HFR_CONFORM_OPEN = "HFR-CONFORM-AWARE-RECEIPT-ORACLES-1 >>>"
    HFR_CONFORM_CLOSE = "HFR-CONFORM-AWARE-RECEIPT-ORACLES-1 <<<"
    HFR_CONFORM_REGIONS = 3
    # PLAYBACK-CLIP-LENGTH-HFR-10S-1 (owner amendment 2026-10-15): the HFR (>= 50 fps native) 10 s floor branch the embedded source-frame
    # oracle (Get-AttrCudaSourceFramesVerdict) gains ahead of the unchanged 20 s branch, plus the HFR-only 10 s wording of the
    # "under N s of real footage" message. Two regions; the baseline's 20 s lines stay verbatim outside them.
    # The sentinel is the short card id: AttrCudaArtifacts.psm1 may not carry the full one (NoFootageTokensTests forbids its "clip" token there).
    HFR_10S_OPEN = "HFR-10S-1 >>>"
    HFR_10S_CLOSE = "HFR-10S-1 <<<"
    HFR_10S_REGIONS = 2
    # PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1 r2: the one HEAVY-arm environment entry (MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=0) and its comment, so an
    # inherited 1 cannot suppress the per-frame rows. One region in the template's HEAVY branch; the LIGHT branch and every baseline line stay verbatim outside it.
    HEAVY_FRAME_LOG_OPEN = "HEAVY-FRAME-LOG-1 >>>"
    HEAVY_FRAME_LOG_CLOSE = "HEAVY-FRAME-LOG-1 <<<"
    HEAVY_FRAME_LOG_REGIONS = 1
    # PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1 r6: the r5 cpu block (the GL viewport/window env clear plus the Processed8 route gate and its summary fields) is three
    # regions in every cpu job (the env-clear edit, the route-gate edit, the summary-field edit); a cuda job carries none, and no baseline commit carries any.
    CPU_GL_ENV_CLEAR_OPEN = "PROCESSED8-CPU-LEG-GL-VIEWPORT-ENV-CLEAR-1 >>>"
    CPU_GL_ENV_CLEAR_CLOSE = "PROCESSED8-CPU-LEG-GL-VIEWPORT-ENV-CLEAR-1 <<<"
    CPU_GL_ENV_CLEAR_REGIONS = 3

    @classmethod
    def strip_regions(cls, text: str) -> tuple[str, dict[str, int]]:
        """Remove every bracketed region of either sentinel family; return the kept text and the number of regions per family."""
        families = {"leg-terminals": (cls.LEG_TERMINALS_OPEN, cls.LEG_TERMINALS_CLOSE), "presentmon-evidence": (cls.PRESENTMON_EVIDENCE_OPEN, cls.PRESENTMON_EVIDENCE_CLOSE),
                    "orphan-sweep": (cls.ORPHAN_SWEEP_OPEN, cls.ORPHAN_SWEEP_CLOSE),
                    "contact-sheet-parity": (cls.CONTACT_SHEET_PARITY_OPEN, cls.CONTACT_SHEET_PARITY_CLOSE),
                    "keepalive-hung-probe": (cls.KEEPALIVE_HUNG_PROBE_OPEN, cls.KEEPALIVE_HUNG_PROBE_CLOSE),
                    "session-locked": (cls.SESSION_LOCKED_OPEN, cls.SESSION_LOCKED_CLOSE),
                    "playback-vsync": (cls.PLAYBACK_VSYNC_OPEN, cls.PLAYBACK_VSYNC_CLOSE),
                    "hfr-conform-oracles": (cls.HFR_CONFORM_OPEN, cls.HFR_CONFORM_CLOSE),
                    "hfr-10s-floor": (cls.HFR_10S_OPEN, cls.HFR_10S_CLOSE),
                    "heavy-frame-log": (cls.HEAVY_FRAME_LOG_OPEN, cls.HEAVY_FRAME_LOG_CLOSE),
                    "cpu-gl-env-clear": (cls.CPU_GL_ENV_CLEAR_OPEN, cls.CPU_GL_ENV_CLEAR_CLOSE)}
        kept: list[str] = []
        inside: str | None = None
        counts = {name: 0 for name in families}
        for line in lf(text).split("\n"):
            opened = next((n for n, (o, _c) in families.items() if o in line), None)
            closed = next((n for n, (_o, c) in families.items() if c in line), None)
            if opened is not None:
                assert inside is None, f"nested {opened} region inside {inside}"
                inside = opened
                counts[opened] += 1
                continue
            if closed is not None:
                assert inside == closed, f"unopened or mismatched {closed} close (inside {inside})"
                inside = None
                continue
            if inside is None:
                kept.append(line)
        assert inside is None, f"unclosed {inside} region"
        return "\n".join(kept), counts

    @classmethod
    def strip_leg_terminals_regions(cls, text: str) -> tuple[str, int]:
        stripped, counts = cls.strip_regions(text)
        return stripped, counts["leg-terminals"]

    def assertByteIdenticalToBaseline(self, name: str, extra: list[str]) -> None:
        if not self.baseline_available:
            self.skipTest(f"baseline commit {BASELINE_COMMIT[:12]} is not in this clone")
        new = self.generate(GENERATOR, f"new-{name}.job.ps1", extra)
        old = self.generate(self.baseline_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1", f"old-{name}.job.ps1", extra)
        stripped, new_counts = self.strip_regions(new.read_text(encoding="utf-8"))
        old_stripped, old_counts = self.strip_regions(old.read_text(encoding="utf-8"))
        self.assertEqual(new_counts["leg-terminals"], 4, "the default job carries exactly the four bracketed DVE-LEG-TERMINALS-1 regions (the wait-failure block and its two summary fields, the smoke-failure block and its one summary field)")
        self.assertEqual(old_counts["leg-terminals"], 4, "the baseline carries the same four")
        self.assertEqual(new_counts["presentmon-evidence"], self.PRESENTMON_EVIDENCE_REGIONS, "the default job carries exactly the pinned number of bracketed DVE-PRESENTMON-EVIDENCE-1 regions")
        self.assertEqual(old_counts["presentmon-evidence"], self.PRESENTMON_EVIDENCE_REGIONS, "the baseline (master) carries the same bracketed DVE-PRESENTMON-EVIDENCE-1 region")
        self.assertEqual(new_counts["orphan-sweep"], self.ORPHAN_SWEEP_REGIONS, "the default job carries exactly the pinned number of bracketed UM-PRESENTMON-ORPHAN-SWEEP-1 regions")
        self.assertEqual(old_counts["orphan-sweep"], self.ORPHAN_SWEEP_REGIONS, "the baseline (the pinned KEEPALIVE-FAILURE-PUBLISH-LAST-1 generator commit) carries the same bracketed regions")
        self.assertEqual(new_counts["contact-sheet-parity"], self.CONTACT_SHEET_PARITY_REGIONS, "the default job carries exactly the pinned number of bracketed CONTACT-SHEET-PLAYBACK-PARITY-1 regions")
        self.assertEqual(old_counts["contact-sheet-parity"], self.CONTACT_SHEET_PARITY_REGIONS, "the baseline (the pinned KEEPALIVE-FAILURE-PUBLISH-LAST-1 generator commit) carries the same bracketed regions")
        self.assertEqual(new_counts["keepalive-hung-probe"], self.KEEPALIVE_HUNG_PROBE_REGIONS, "the default job carries exactly the pinned number of bracketed CI-FLAKE-KEEPALIVE-HUNG-PROBE-SLEEP-1 regions")
        self.assertEqual(old_counts["keepalive-hung-probe"], self.KEEPALIVE_HUNG_PROBE_REGIONS, "the baseline (the pinned KEEPALIVE-FAILURE-PUBLISH-LAST-1 generator commit) carries the same bracketed regions")
        self.assertEqual(new_counts["session-locked"], self.SESSION_LOCKED_REGIONS, "the default job carries exactly the pinned number of bracketed VENUE-SESSION-LOCKED-REFUSAL-1 regions")
        self.assertEqual(old_counts["session-locked"], self.SESSION_LOCKED_REGIONS, "the baseline (the pinned KEEPALIVE-FAILURE-PUBLISH-LAST-1 generator commit) carries the same bracketed regions")
        self.assertEqual(new_counts["playback-vsync"], self.PLAYBACK_VSYNC_REGIONS, "the default job carries exactly the pinned number of bracketed PLAYBACK-VSYNC-DEFAULT-1 regions")
        self.assertEqual(old_counts["playback-vsync"], self.PLAYBACK_VSYNC_REGIONS, "the baseline (the pinned GPU-PREF-REGISTRY-STOP-AND-PRUNE-1 generator commit) carries the same bracketed regions")
        self.assertEqual(new_counts["hfr-conform-oracles"], self.HFR_CONFORM_REGIONS, "the default job carries exactly the pinned number of bracketed HFR-CONFORM-AWARE-RECEIPT-ORACLES-1 regions")
        self.assertEqual(old_counts["hfr-conform-oracles"], self.HFR_CONFORM_REGIONS, "the baseline (the pinned GPU-PREF-REGISTRY-STOP-AND-PRUNE-1 generator commit) carries the same bracketed regions")
        self.assertEqual(new_counts["hfr-10s-floor"], self.HFR_10S_REGIONS, "the default job carries exactly the pinned number of bracketed PLAYBACK-CLIP-LENGTH-HFR-10S-1 regions")
        self.assertEqual(old_counts["hfr-10s-floor"], 0, "the baseline predates the owner's HFR 10 s amendment")
        self.assertEqual(new_counts["heavy-frame-log"], self.HEAVY_FRAME_LOG_REGIONS, "the default (HEAVY) job carries exactly the pinned number of bracketed HEAVY-FRAME-LOG-1 regions")
        self.assertEqual(old_counts["heavy-frame-log"], 0, "the baseline predates the HEAVY frame-log opt-out")
        self.assertEqual((new_counts["cpu-gl-env-clear"], old_counts["cpu-gl-env-clear"]), (0, 0), "a cuda job carries no cpu GL-viewport env-clear region, and neither does the baseline")
        self.assertEqual(stripped, old_stripped,
                         "the DEFAULT (bachelor/cuda) emitted job changed outside the bracketed regions -- it must stay byte-identical to the pinned baseline")

    def test_default_arguments_emit_a_byte_identical_job(self) -> None:
        self.assertByteIdenticalToBaseline("default", [])

    def test_the_other_existing_switches_are_still_byte_identical(self) -> None:
        self.assertByteIdenticalToBaseline("switches", ["-ContactSheet", "-ContactSheetFrames", "4", "-TelemetryArm", "LIGHT", "-DisablePaintPerSubmit"])

    def test_a_longer_play_window_is_still_byte_identical(self) -> None:
        self.assertByteIdenticalToBaseline("playseconds", ["-PlaySeconds", "30"])

    def test_explicit_defaults_of_the_new_parameters_change_nothing(self) -> None:
        default = self.generate(GENERATOR, "explicit-default.job.ps1", [])
        explicit = self.generate(GENERATOR, "explicit-args.job.ps1", ["-Venue", "bachelor", "-Backend", "cuda", "-ScaleFactor", "4",
                                                                      "-CpuQuiescenceThresholdPercent", "20.0"])
        self.assertEqual(default.read_bytes(), explicit.read_bytes())

    def test_an_expected_scale_request_arms_the_smoke_runners_scale_check_only_when_asked(self) -> None:
        """DVE-SCALE2-LOOK-LEG-1 r2 item 1d: -UsePersistedPlaybackSettings leaves the smoke runner's ExpectedScaleRequest at -1 (no check). The runner passes the scale the
        route is declared to render at; the job then carries -ExpectedScaleRequest (and -ExpectedVisualScaleRequest -1, so only the summary-line check goes live). Without
        it the emitted job is the unchanged default text (the byte-identity tests above)."""
        default = self.generate(GENERATOR, "no-expected-scale.job.ps1", []).read_text(encoding="utf-8")
        armed = self.generate(GENERATOR, "expected-scale.job.ps1", ["-ExpectedScaleRequest", "1"])
        self.assertEqual(self.parse_errors(armed), 0)
        self.assertNotIn("-ExpectedScaleRequest", default)
        self.assertIn("-ScaleFactor 4 -UsePersistedPlaybackSettings", default)
        self.assertIn("-ScaleFactor 4 -ExpectedScaleRequest 1 -ExpectedVisualScaleRequest -1 -UsePersistedPlaybackSettings", armed.read_text(encoding="utf-8"))
        self.assertIn("[int]$ExpectedScaleRequest = -1", GENERATOR.read_text(encoding="utf-8"))

    def test_a_windowed_leg_passes_the_apps_windowed_option_through_the_one_additional_args_and_fullscreen_changes_nothing(self) -> None:
        """DUAL-VENUE-DISPLAY-MATRIX-1: -DisplayMode windowed appends --windowed to the smoke runner's ONE -AdditionalArgs (a second -AdditionalArgs would be a parameter-binding
        error): inside the contact-sheet array when a sheet is on, as an array of its own when not. The default and an explicit fullscreen emit the unchanged job."""
        default = self.generate(GENERATOR, "dm-default.job.ps1", ["-ContactSheet", "-ContactSheetFrames", "4"])
        fullscreen = self.generate(GENERATOR, "dm-fullscreen.job.ps1", ["-ContactSheet", "-ContactSheetFrames", "4", "-DisplayMode", "fullscreen"])
        self.assertEqual(default.read_bytes(), fullscreen.read_bytes(), "an explicit fullscreen is the default job, byte for byte")
        self.assertNotIn("--windowed", default.read_text(encoding="utf-8"))
        windowed = self.generate(GENERATOR, "dm-windowed.job.ps1", ["-ContactSheet", "-ContactSheetFrames", "4", "-DisplayMode", "windowed"])
        self.assertEqual(self.parse_errors(windowed), 0)
        text = windowed.read_text(encoding="utf-8")
        # the windowed job is the default job plus exactly the added statements (the template itself is untouched)
        self.assertEqual(lf(text).replace(self.WINDOWED_ADDITION, "").replace(self.WINDOWED_NO_SHEET, ""), lf(default.read_text(encoding="utf-8")))
        bare = self.generate(GENERATOR, "dm-windowed-bare.job.ps1", ["-DisplayMode", "windowed"])
        self.assertEqual(self.parse_errors(bare), 0)
        self.assertNotIn("--windowed", self.generate(GENERATOR, "dm-bare-default.job.ps1", []).read_text(encoding="utf-8"))
        # RUNTIME behaviour: the real emitted statements are executed with stub inputs, and $cmd carries exactly ONE -AdditionalArgs with --windowed in it
        on = self.eval_cmd(text, enabled=True, frames=4)
        self.assertEqual(on.count("-AdditionalArgs"), 1, on)
        self.assertRegex(on, r"-AdditionalArgs @\('--contact-sheet-dir', '[^']*', '--contact-sheet-frames', '4', '--windowed'\)$")
        paired = self.eval_cmd(self.generate(GENERATOR, "dm-windowed-paired.job.ps1", ["-ContactSheet", "-ContactSheetPairedSeek", "-DisplayMode", "windowed"]).read_text(encoding="utf-8"),
                               enabled=True, frames=4, paired_seek=True)
        self.assertEqual(paired.count("-AdditionalArgs"), 1, paired)
        self.assertRegex(paired, r"'--contact-sheet-seek-dir', '[^']*', '--windowed'\)$")
        off = self.eval_cmd(text, enabled=False, frames=4)
        self.assertTrue(off.endswith("-AdditionalArgs @('--windowed')"), off)
        self.assertEqual(off.count("-AdditionalArgs"), 1, off)
        self.assertEqual(self.eval_cmd(bare.read_text(encoding="utf-8"), enabled=False, frames=4).count("-AdditionalArgs"), 1)
        # ... and the DEFAULT job, evaluated the same way, never carries it
        self.assertNotIn("--windowed", self.eval_cmd(default.read_text(encoding="utf-8"), enabled=True, frames=4))
        self.assertNotIn("AdditionalArgs", self.eval_cmd(default.read_text(encoding="utf-8"), enabled=False, frames=4))

    # What a windowed job adds to the default job's text: one statement before the contact-sheet $cmd append, and the guarded no-sheet append after its closing brace.
    WINDOWED_ADDITION = "    $contactSheetAdditionalArgs = $contactSheetAdditionalArgs.Substring(0, $contactSheetAdditionalArgs.Length - 1) + \", '--windowed')\"\n"
    WINDOWED_NO_SHEET = "\nif (-not $ContactSheetEnabled) { $cmd = \"$cmd -AdditionalArgs @('--windowed')\" }"

    def eval_cmd(self, job_text: str, enabled: bool, frames: int, paired_seek: bool = False) -> str:
        """Execute the emitted job's own $cmd-construction statements (ConvertTo-PsSingleQuoted through the end of the contact-sheet / windowed append) with stub inputs."""
        job_text = lf(job_text)
        start = job_text.index("function ConvertTo-PsSingleQuoted")
        tail = '$cmd = "$cmd -AdditionalArgs $contactSheetAdditionalArgs"\n}'
        end = job_text.index(tail) + len(tail)
        if job_text.startswith(self.WINDOWED_NO_SHEET, end):
            end += len(self.WINDOWED_NO_SHEET)
        work = self.tmp / f"work-eval-{enabled}-{frames}-{paired_seek}"
        probe = self.tmp / f"probe-cmd-{enabled}-{frames}-{paired_seek}.ps1"
        probe.write_text("$ErrorActionPreference = 'Stop'\n" f"$Work = '{work}'\n" "$smoke = 'smoke.ps1'\n$exePath = 'exe.exe'\n$clipPath = 'clip-stub'\n$resultPath = 'result.json'\n"
                         "$envList = \"'A=1','B=2'\"\n" f"$ContactSheetEnabled = ${'true' if enabled else 'false'}\n" f"$ContactSheetFrameCount = {frames}\n"
                         f"$ContactSheetPairedSeek = ${'true' if paired_seek else 'false'}\n" + job_text[start:end] + "\nWrite-Output \"CMD=$cmd\"\n", encoding="utf-8")
        proc = run_pwsh(["-File", str(probe)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return next(l for l in proc.stdout.splitlines() if l.startswith("CMD="))[len("CMD="):]

    def test_an_unknown_display_mode_is_refused_before_emitting(self) -> None:
        out = self.tmp / "dm-refused.job.ps1"
        proc = run_pwsh(["-File", str(GENERATOR), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0], "-FixtureSha256", "cd" * 32,
                         "-RepoRoot", str(self.repo), "-OutFile", str(out), "-DisplayMode", "maximized"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(out.exists())

    def parse_errors(self, job: Path) -> int:
        if str(job) in self._parse_cache:   # (a job `warm_generated` parsed: a generated file is never rewritten)
            return self._parse_cache[str(job)]
        script = ("$t=$null;$e=$null;[void][System.Management.Automation.Language.Parser]::ParseFile("
                  f"'{job}',[ref]$t,[ref]$e);Write-Output $e.Count")
        proc = run_pwsh(["-Command", script])
        return int(proc.stdout.strip().splitlines()[-1])

    def test_ultra_magnus_variant_is_keyed_from_the_venue_table_and_guards_the_host(self) -> None:
        job = self.generate(GENERATOR, "um.job.ps1", ["-Venue", "ultra-magnus"])
        text = job.read_text(encoding="utf-8")
        self.assertEqual(self.parse_errors(job), 0)
        self.assertIn("$Root = 'G:\\Temp\\mlv-gpu-profile\\agent'", text)
        self.assertIn("'G:\\Temp\\mlv-gpu-profile'", text)
        self.assertNotIn("C:\\mlvtmp\\", text.split("# --- verifiers, embedded VERBATIM")[0])
        self.assertIn("VENUE_HOST_MISMATCH", text)
        self.assertIn("exit 29", text)
        self.assertIn("$ExpectedHostName = 'ULTRA-MAGNUS'", text)
        # master's creator-recorded owner-footage sweep follows the venue's scratch root, not the bachelor literal
        self.assertIn("-TrustedRoot 'G:\\Temp\\mlv-gpu-profile' -Path $Work -OwnedJournal $OwnerJournal", text)

    def test_cpu_backend_omits_every_gpu_env_and_never_fires_exit_13_or_14(self) -> None:
        job = self.generate(GENERATOR, "cpu.job.ps1", ["-Backend", "cpu"])
        text = job.read_text(encoding="utf-8")
        self.assertEqual(self.parse_errors(job), 0)
        self.assertNotIn("'MLVAPP_GPU_PLAYBACK_RECON=1'", text)
        self.assertNotIn("'MLVAPP_EXPERIMENTAL_GPU_PROCESSING=1'", text)
        self.assertIn("-notlike 'MLVAPP_GPU_*'", text)  # the diag env is filtered out too
        self.assertIn("if ($Backend -ne 'cpu' -and $gpuFramesTotal -le 0) {", text)
        self.assertIn("if ($Backend -ne 'cpu' -and $gpuSummary.cpuFrames -gt 0) {", text)
        self.assertIn("CPU_BACKEND_PATH_MISMATCH", text)
        # ... and the default cuda job still carries them (nothing was removed for everyone).
        cuda = self.generate(GENERATOR, "cuda.job.ps1", []).read_text(encoding="utf-8")
        self.assertIn("'MLVAPP_GPU_PLAYBACK_RECON=1'", cuda)
        self.assertNotIn("CPU_BACKEND_PATH_MISMATCH", cuda)

    def test_look_leg_forces_look_assist_and_passes_the_flavor(self) -> None:
        job = self.generate(GENERATOR, "look.job.ps1", ["-ForceLookAssist", "-ContactSheet", "-LookFlavor", "cinematic"])
        text = job.read_text(encoding="utf-8")
        self.assertEqual(self.parse_errors(job), 0)
        self.assertIn("-RequireLookAssist:`$true -Scope none", text)
        self.assertNotIn("-RequireLookAssist:`$false -Scope none", text)
        # master's ENFORCE-4 isolates an automation run's settings store, so the job seeds no registry (a venue's persisted
        # "use default receipt" is never read) -- Look Assist is forced by -RequireLookAssist alone.
        self.assertNotIn("defaultReceiptEnabled", text)
        self.assertNotIn("reg add", text.split("# --- verifiers, embedded VERBATIM")[0])
        self.assertIn("('MLVAPP_LOOK_ASSIST_FLAVOR=' + $LookFlavor)", text)
        # LOOK-ASSIST-FLAVORS-1: the app reads the variable and reports the flavor it applied; the job records that report
        # (gui_smoke.visual_state look_assist_flavor, via the runner's result.log.visualState) instead of a fixed 'unknown'.
        self.assertIn("lookFlavorReported = $(if ($LookLeg) { $lfReported = try { [string]$resultJson.log.visualState.look_assist_flavor } catch { '' }", text)
        self.assertIn("lookFlavorHonored = $(if ($LookLeg) { $lfReported -ceq $LookFlavor } else { $null })", text)
        self.assertNotIn("lookFlavorHonored = $(if ($LookLeg) { 'unknown' } else { $null })", text)
        self.assertNotIn("--no-look-assist", text)

    def test_a_look_pace_leg_forces_look_assist_without_a_contact_sheet(self) -> None:
        """PLAYBACK-BACHELOR-PRESENT-JITTER-1: -ForceLookAssist -LookPaceLeg is the look leg's Look Assist and flavor with NO contact sheet, so no
        GUI-thread framebuffer grab lands inside the timed Play."""
        job = self.generate(GENERATOR, "pace.job.ps1", ["-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic"])
        text = job.read_text(encoding="utf-8")
        self.assertEqual(self.parse_errors(job), 0)
        self.assertIn("$LookLeg = $true", text)
        self.assertIn("$LookPaceLeg = $true", text)
        # a pace leg is a SPEED leg: summary.json says lookLeg false (the receipt checks it against the spec's legType) while
        # lookAssistForced and the flavor fields still report the forced look
        self.assertIn("lookLeg = ($LookLeg -and -not $LookPaceLeg)", text)
        self.assertIn("lookAssistForced = $LookLeg", text)
        self.assertIn("$LookFlavor = 'cinematic'", text)
        self.assertIn("-RequireLookAssist:`$true -Scope none", text)
        self.assertIn("('MLVAPP_LOOK_ASSIST_FLAVOR=' + $LookFlavor)", text)
        self.assertIn("$ContactSheetEnabled = $false", text)
        look = self.generate(GENERATOR, "look2.job.ps1", ["-ForceLookAssist", "-ContactSheet", "-LookFlavor", "cinematic"]).read_text(encoding="utf-8")
        self.assertIn("$ContactSheetEnabled = $true", look)
        # the lookahead A/B arm: only a pace leg passes the depth to the app; without it the job sets nothing
        self.assertNotIn("MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES", text)
        la3 = self.generate(GENERATOR, "pace-la3.job.ps1", ["-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic",
                                                            "-PlaybackRenderLookaheadFrames", "3"])
        self.assertEqual(self.parse_errors(la3), 0)
        self.assertIn("'MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=3',", la3.read_text(encoding="utf-8"))

    def test_a_swap_interval_reaches_the_apps_env_only_when_asked(self) -> None:
        """PLAYBACK-VSYNC-DEFAULT-1: -SwapInterval 0|1 adds MLVAPP_SWAP_INTERVAL to the app's env list, for a look leg and the default job alike;
        without it the job sets nothing (the app keeps its default 1)."""
        plain = self.generate(GENERATOR, "plain.job.ps1", []).read_text(encoding="utf-8")
        self.assertNotIn("MLVAPP_SWAP_INTERVAL", plain)
        for interval in ("0", "1"):
            for name, extra in (("default", []), ("look", ["-ForceLookAssist", "-ContactSheet", "-LookFlavor", "cinematic"]),
                                ("windowed", ["-ForceLookAssist", "-ContactSheet", "-LookFlavor", "cinematic", "-DisplayMode", "windowed"])):
                job = self.generate(GENERATOR, f"swap{interval}-{name}.job.ps1", [*extra, "-SwapInterval", interval])
                self.assertEqual(self.parse_errors(job), 0, name)
                text = job.read_text(encoding="utf-8")
                self.assertEqual(text.count(f"'MLVAPP_SWAP_INTERVAL={interval}',"), 1, name)
                self.assertIn(f"'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n    'MLVAPP_SWAP_INTERVAL={interval}',", text.replace("\r\n", "\n"), name)

    def test_the_cuda_launch_timer_reaches_the_apps_env_only_when_asked(self) -> None:
        """GPU-EVENT153-ROOTCAUSE-2: -CudaLaunchTimingMinMs <ms> adds MLVAPP_CUDA_LAUNCH_TIMING=1 and its line threshold (an
        invariant-culture decimal) to the app's env list; absent, the job is the text it was. The runner maps the leg's
        generatorArgs.cudaLaunchTimingMinMs for any leg, and the schema bounds it."""
        plain = self.generate(GENERATOR, "lt-plain.job.ps1", []).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("MLVAPP_CUDA_LAUNCH_TIMING", plain)
        for ms, rendered in (("0", "0"), ("2", "2"), ("2.5", "2.5")):
            job = self.generate(GENERATOR, f"lt-{ms}.job.ps1", ["-CudaLaunchTimingMinMs", ms])
            self.assertEqual(self.parse_errors(job), 0, ms)
            text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
            pair = f"    'MLVAPP_CUDA_LAUNCH_TIMING=1',\n    'MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS={rendered}',\n"
            self.assertEqual(text.count("'MLVAPP_CUDA_LAUNCH_TIMING=1',"), 1, ms)
            self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + pair, text, ms)
            self.assertEqual(text.replace(pair, ""), plain, f"{ms}: the pair is the only difference from the plain job")
        both = self.generate(GENERATOR, "lt-swap.job.ps1", ["-SwapInterval", "1", "-CudaLaunchTimingMinMs", "2"])
        self.assertEqual(self.parse_errors(both), 0)
        both_text = both.read_text(encoding="utf-8")
        self.assertEqual(both_text.count("'MLVAPP_SWAP_INTERVAL=1',"), 1)
        self.assertEqual(both_text.count("'MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS=2',"), 1)
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = "if ($spec.generatorArgs.PSObject.Properties['cudaLaunchTimingMinMs']) { $gen['CudaLaunchTimingMinMs'] = [double]$spec.generatorArgs.cudaLaunchTimingMinMs }"
        self.assertIn(mapping, runner)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        prop = schema["properties"]["generatorArgs"]["properties"]["cudaLaunchTimingMinMs"]
        self.assertEqual((prop["type"], prop["minimum"], prop["maximum"]), ("number", 0, 10000))

    def test_the_present_trace_reaches_the_apps_env_only_when_asked(self) -> None:
        """PLAYBACK-STARTUP-CATCHUP-BURST-1 r7: -PresentTrace adds MLVAPP_PLAYBACK_PRESENT_TRACE=1 to the app's env list; absent, the job is
        the text it was. The runner maps a leg's generatorArgs.presentTrace true for any leg."""
        plain = self.generate(GENERATOR, "pt-plain.job.ps1", []).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("MLVAPP_PLAYBACK_PRESENT_TRACE", plain)
        job = self.generate(GENERATOR, "pt-on.job.ps1", ["-PresentTrace"])
        self.assertEqual(self.parse_errors(job), 0)
        text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
        line = "    'MLVAPP_PLAYBACK_PRESENT_TRACE=1',\n"
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, text)
        self.assertEqual(text.replace(line, ""), plain, "the flag is the only difference from the plain job")
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = "if ($spec.generatorArgs.PSObject.Properties['presentTrace'] -and [bool]$spec.generatorArgs.presentTrace) { $gen['PresentTrace'] = $true }"
        self.assertIn(mapping, runner)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")

    # GPU-DEVICE-LOSS-SELF-HEAL-1 r2: the self-heal proof's job text (the generator's -GpuFaultInject / -GpuSafeStart / -AutomationSettingsDir / -GpuPreLaunches).
    SELF_HEAL_PRE_OPEN = "# GPU-DEVICE-LOSS-SELF-HEAL-1 r2: the shared run-scoped settings store and the self-heal pre-launches"
    SELF_HEAL_OWNER_ANCHOR = "# OWNER-FOOTAGE-NO-HARDLINK-1: the last look before the app can read anything."
    SELF_HEAL_POST_OPEN = "# GPU-DEVICE-LOSS-SELF-HEAL-1 r2: publish the measured run's GPU field log"
    SELF_HEAL_LAUNCH_DONE = 'Write-JobTrace "step smoke-launch done rc=$smokeRc"\n'

    def test_the_gpu_fault_inject_and_safe_start_reach_the_apps_env_only_when_asked(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r2: -GpuFaultInject <code>@<n> and -GpuSafeStart clear each add exactly one line at the env-list site;
        absent, the job is the text it was. The runner maps the leg's generatorArgs for any leg."""
        plain = self.generate(GENERATOR, "sh-plain.job.ps1", []).read_text(encoding="utf-8").replace("\r\n", "\n")
        for token in ("MLVAPP_GPU_FAULT_INJECT", "MLVAPP_GPU_SAFE_START", "MLVAPP_AUTOMATION_SETTINGS_DIR", "self-heal", "function New-AttrCudaOwnedRoot"):
            self.assertNotIn(token, plain)
        for name, extra, line in (("inject", ["-GpuFaultInject", "702@120"], "    'MLVAPP_GPU_FAULT_INJECT=702@120',\n"),
                                  ("clear", ["-GpuSafeStart", "clear"], "    'MLVAPP_GPU_SAFE_START=clear',\n")):
            job = self.generate(GENERATOR, f"sh-{name}.job.ps1", extra)
            self.assertEqual(self.parse_errors(job), 0, name)
            text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
            self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, text, name)
            self.assertEqual(text.replace(line, ""), plain, f"{name}: the line is the only difference from the plain job")
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        branch = runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']")
        for mapping in ("if ($spec.generatorArgs.PSObject.Properties['gpuFaultInject']) { $gen['GpuFaultInject'] = [string]$spec.generatorArgs.gpuFaultInject }",
                        "if ($spec.generatorArgs.PSObject.Properties['gpuSafeStart']) { $gen['GpuSafeStart'] = [string]$spec.generatorArgs.gpuSafeStart }",
                        "if ($spec.generatorArgs.PSObject.Properties['automationSettingsDir']) { $gen['AutomationSettingsDir'] = [string]$spec.generatorArgs.automationSettingsDir }",
                        "if ($spec.generatorArgs.PSObject.Properties['gpuPreLaunches']) { $gen['GpuPreLaunches'] = [string[]]@($spec.generatorArgs.gpuPreLaunches) }"):
            self.assertIn(mapping, runner)
            self.assertLess(runner.index(mapping), branch, "the mapping must sit outside the forceLookAssist branch")
        self.assertIn("Copy-Item -LiteralPath $selfHealShare -Destination (Join-Path $evidenceDir 'self-heal') -Recurse", runner)

    def test_the_gpu_self_heal_pre_launches_share_one_store_and_delete_it_after(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r2: -AutomationSettingsDir with -GpuPreLaunches adds the store's env line, the pre-launch block just before the
        owner-footage check and the publish-and-delete block right after the measured smoke launch -- and nothing else: with those three removed the
        job is the plain one, byte for byte. The pre-launches are refused without the store, on a contact-sheet leg and with a bad entry."""
        plain = self.generate(GENERATOR, "shp-plain.job.ps1", ["-TelemetryArm", "HEAVY"]).read_text(encoding="utf-8").replace("\r\n", "\n")
        job = self.generate(GENERATOR, "shp.job.ps1", ["-TelemetryArm", "HEAVY", "-AutomationSettingsDir", "gpu-self-heal-settings",
                                                       "-GpuPreLaunches", "702@120,702@120,none", "-GpuSafeStart", "clear"])
        self.assertEqual(self.parse_errors(job), 0)
        text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
        env_lines = "    ('MLVAPP_AUTOMATION_SETTINGS_DIR=' + (Join-Path $Work 'gpu-self-heal-settings')),\n    'MLVAPP_GPU_SAFE_START=clear',\n"
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + env_lines, text)
        self.assertEqual(text.count("function New-AttrCudaOwnedRoot"), 1)
        self.assertEqual(text.count("$gpuPreLaunches = @('702@120', '702@120', 'none')\n"), 1)
        self.assertIn("-Path (Join-Path $Work 'gpu-self-heal-settings') -OwnedJournal $OwnerJournal).Path\n", text)
        self.assertIn("Remove-AttrCudaTree -TrustedRoot ", text[text.index(self.SELF_HEAL_POST_OPEN):])
        pre = text.index(self.SELF_HEAL_PRE_OPEN)
        self.assertLess(text.index("$cmd = \"& $(ConvertTo-PsSingleQuoted $smoke)"), pre, "the pre-launches reuse the measured run's $cmd")
        self.assertLess(pre, text.index(self.SELF_HEAL_OWNER_ANCHOR))
        self.assertLess(text.index(self.SELF_HEAL_OWNER_ANCHOR), text.index("$presentMonProc = Start-PresentMonCapture"))
        post = text.index(self.SELF_HEAL_POST_OPEN)
        self.assertEqual(text.index(self.SELF_HEAL_LAUNCH_DONE) + len(self.SELF_HEAL_LAUNCH_DONE), post)
        text, r6_regions = self.SELF_HEAL_R6.subn("", text)
        self.assertEqual(r6_regions, 2, "r6: the metrics computation and the success summary's metric lines")
        stripped = text.replace(env_lines, "", 1)
        stripped = stripped[:stripped.index(self.SELF_HEAL_PRE_OPEN)] + stripped[stripped.index(self.SELF_HEAL_OWNER_ANCHOR):]
        post = stripped.index(self.SELF_HEAL_POST_OPEN)
        post_end = stripped.index("\n}\n", stripped.index("} catch {", post)) + 3
        self.assertEqual(stripped[:post] + stripped[post_end:], plain)
        cases = ((["-GpuPreLaunches", "702@120"], "DUAL_VENUE_GPU_PRE_LAUNCH_NEEDS_SETTINGS_DIR"),
                 (["-AutomationSettingsDir", "s", "-GpuPreLaunches", "702@120", "-ForceLookAssist", "-ContactSheet"], "DUAL_VENUE_GPU_PRE_LAUNCH_NEEDS_SPEED_LEG"),
                 (["-AutomationSettingsDir", "s", "-GpuPreLaunches", "701@1"], "DUAL_VENUE_GPU_PRE_LAUNCH_INVALID"),
                 (["-AutomationSettingsDir", "s", "-GpuPreLaunches", "none,none,none,none,none"], "DUAL_VENUE_GPU_PRE_LAUNCH_INVALID"),
                 (["-GpuFaultInject", "701@1"], "GpuFaultInject"),
                 (["-AutomationSettingsDir", "..\\x"], "AutomationSettingsDir"))
        outs = [self.tmp / f"sh-refused-{n}.job.ps1" for n in range(len(cases))]
        procs = run_pwsh_many([self.generator_command(GENERATOR, out, extra) for out, (extra, _token) in zip(outs, cases)])   # (independent runs, one pwsh each)
        for out, (extra, token), proc in zip(outs, cases, procs):
            self.assertNotEqual(proc.returncode, 0, token)
            self.assertIn(token, proc.stdout + proc.stderr)
            self.assertFalse(out.exists(), token)

    def test_the_gpu_self_heal_leg_is_the_l1_to_l4_proof(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r2: the one tracked self-heal leg is a cuda SPEED leg: L1 and L2 inject 702 (two losses inside the breaker's
        window set the flag), L3 injects nothing (it must start on the CPU), and the measured run L4 clears the flag (it must render on CUDA again)."""
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        args = schema["properties"]["generatorArgs"]["properties"]
        self.assertEqual(args["gpuFaultInject"]["pattern"], "^(700|702|719|999)@[1-9][0-9]*$")
        self.assertEqual(args["gpuSafeStart"]["enum"], ["clear"])
        self.assertEqual((args["gpuPreLaunches"]["maxItems"], args["gpuPreLaunches"]["items"]["pattern"]), (4, "^((700|702|719|999)@[1-9][0-9]*|none)$"))
        leg = json.loads((DV / SELF_HEAL_LEG).read_text(encoding="utf-8"))
        import jsonschema
        jsonschema.validate(leg, schema)
        self.assertEqual((leg["legId"], leg["card"], leg["legType"], leg["backends"]), (Path(SELF_HEAL_LEG).stem, "GPU-DEVICE-LOSS-SELF-HEAL-1", "speed", ["cuda"]))
        self.assertEqual(leg["generatorArgs"], {"telemetryArm": "HEAVY", "automationSettingsDir": "gpu-self-heal-settings",
                                                "gpuPreLaunches": ["702@120", "702@120", "none"], "gpuSafeStart": "clear"})
        for role in ("acceptance", "supplementary"):
            self.assertIn({"metric": "gpuFramesTotal", "op": "gt", "value": 0}, leg["criteria"][role]["cuda"], role)
            self.assertIn({"metric": "cpuFrames", "op": "eq", "value": 0}, leg["criteria"][role]["cuda"], role)
        for bad in ({"gpuFaultInject": "702@0"}, {"gpuSafeStart": "set"}, {"automationSettingsDir": "a/b"}, {"gpuPreLaunches": []}, {"gpuPreLaunches": ["none"] * 5}):
            broken = json.loads(json.dumps(leg))
            broken["generatorArgs"].update(bad)
            with self.assertRaises(jsonschema.ValidationError, msg=str(bad)):
                jsonschema.validate(broken, schema)

    SELF_HEAL_R6 = re.compile(r"(?ms)^[ ]*# GPU-DEVICE-LOSS-SELF-HEAL-1 r6 >>>.*?^[ ]*# GPU-DEVICE-LOSS-SELF-HEAL-1 r6 <<<\n")

    def test_the_gpu_self_heal_job_writes_its_l1_to_l4_metrics_into_the_success_summary(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r6 (CoS #437): with pre-launches the job computes the L1..L4 metrics (Get-AttrCudaSelfHealMetrics, embedded)
        after the measured run published self-heal\\main and its run log is read, and writes one line per metric into the SUCCESS summary.json -- the
        file whose scalars are the receipt's verbatim metrics, so the leg's criteria gate them and the validator re-derives them. Every metric the
        tracked leg's criteria name is one the job writes. Without pre-launches neither region exists."""
        job = self.generate(GENERATOR, "shm.job.ps1", ["-TelemetryArm", "HEAVY", "-AutomationSettingsDir", "gpu-self-heal-settings",
                                                       "-GpuPreLaunches", "702@120,702@120,none", "-GpuSafeStart", "clear"])
        self.assertEqual(self.parse_errors(job), 0)
        text = lf(job.read_text(encoding="utf-8"))
        self.assertEqual(len(self.SELF_HEAL_R6.findall(text)), 2)
        self.assertEqual(text.count("function Get-AttrCudaSelfHealMetrics {"), 1)
        self.assertEqual(text.count("function Get-AttrCudaSelfHealMetricNames {"), 1)
        compute = text.index("$selfHealMetrics = Get-AttrCudaSelfHealMetrics -SelfHealDir $selfHealPub -PreLaunches $gpuPreLaunches "
                             "-MeasuredLogText $rawLog -MeasuredSessionId $measuredSmokeSessionId\n")
        success = text.index("# A SUCCESS summary.json, which the failure paths above all write")
        self.assertLess(text.index(self.SELF_HEAL_POST_OPEN), compute, "after the measured run's self-heal\\main is published")
        self.assertLess(text.index("$measuredSmokeSessionId = Get-MeasuredSmokeSessionId $rawLog\n"), compute)
        self.assertLess(compute, success)
        summary = text[success:text.index("\n})", success)]
        proc = run_pwsh(["-Command", f"Import-Module '{ATTR_MODULE}' -Force; Get-AttrCudaSelfHealMetricNames -PreLaunchCount 3"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        names = proc.stdout.split()
        self.assertEqual(len(names), 2 + 3 * (12 + 6) + 2)
        for name in names:
            self.assertEqual(summary.count(f"\n    {name} = $selfHealMetrics['{name}']\n"), 1, name)
        leg = json.loads((DV / SELF_HEAL_LEG).read_text(encoding="utf-8"))
        for role in ("acceptance", "supplementary"):
            gated = {c["metric"] for c in leg["criteria"][role]["cuda"]}
            self.assertEqual(gated - set(names), {"rows", "gpuFramesTotal", "cpuFrames"}, role)
        store_only = lf(self.generate(GENERATOR, "shm-store.job.ps1", ["-TelemetryArm", "HEAVY", "-AutomationSettingsDir", "gpu-self-heal-settings"])
                        .read_text(encoding="utf-8"))
        self.assertNotIn("GPU-DEVICE-LOSS-SELF-HEAL-1 r6", store_only)
        self.assertNotIn("Get-AttrCudaSelfHealMetrics", store_only)

    def test_the_first_present_capture_reaches_the_apps_env_only_when_asked(self) -> None:
        """PLAYBACK-STARTUP-WAIT-PREWARM-1: -FirstPresentCapture adds MLVAPP_SMOKE_FIRST_PRESENT_CAPTURE=1 to the app's env list; absent, the
        job is the text it was. The runner maps a leg's generatorArgs.firstPresentCapture true for any leg."""
        plain = self.generate(GENERATOR, "fpc-plain.job.ps1", []).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("MLVAPP_SMOKE_FIRST_PRESENT_CAPTURE", plain)
        job = self.generate(GENERATOR, "fpc-on.job.ps1", ["-FirstPresentCapture"])
        self.assertEqual(self.parse_errors(job), 0)
        text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
        line = "    'MLVAPP_SMOKE_FIRST_PRESENT_CAPTURE=1',\n"
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, text)
        self.assertEqual(text.replace(line, ""), plain, "the flag is the only difference from the plain job")
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = ("if ($spec.generatorArgs.PSObject.Properties['firstPresentCapture'] -and [bool]$spec.generatorArgs.firstPresentCapture) "
                   "{ $gen['FirstPresentCapture'] = $true }")
        self.assertIn(mapping, runner)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")

    def test_the_warm_reuse_miss_dump_reaches_the_apps_env_only_when_asked(self) -> None:
        """PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 F-diag: -WarmReuseMissDump adds MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR (a directory
        under the job's published artifacts) to the app's env list; absent, the job is the text it was. The runner maps a leg's
        generatorArgs.warmReuseMissDump true for any leg."""
        plain = self.generate(GENERATOR, "wrmd-plain.job.ps1", []).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR", plain)
        job = self.generate(GENERATOR, "wrmd-on.job.ps1", ["-WarmReuseMissDump"])
        self.assertEqual(self.parse_errors(job), 0)
        text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
        line = "    ('MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR=' + (Join-Path $Pub 'warm-reuse-miss')),\n"
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, text)
        self.assertEqual(text.replace(line, ""), plain, "the dump dir is the only difference from the plain job")
        self.assertLess(text.index("$Pub = Join-Path $Root"), text.index(line), "$Pub is defined before the env list reads it")
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = ("if ($spec.generatorArgs.PSObject.Properties['warmReuseMissDump'] -and [bool]$spec.generatorArgs.warmReuseMissDump) "
                   "{ $gen['WarmReuseMissDump'] = $true }")
        self.assertIn(mapping, runner)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")

    def test_an_unpaced_leg_reaches_the_apps_env_and_the_smoke_only_when_asked(self) -> None:
        """CUDA-HEADROOM-1: -Unpaced adds MLVAPP_PLAYBACK_UNPACED=1 to the app's env list and -DropFrameMode off to the smoke command, each
        exactly once; absent, the job has neither. The runner maps a leg's generatorArgs.unpaced true for any leg; the schema declares it."""
        plain = self.generate(GENERATOR, "up-plain.job.ps1", []).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("MLVAPP_PLAYBACK_UNPACED", plain)
        self.assertNotIn("-DropFrameMode off", plain)
        job = self.generate(GENERATOR, "up-on.job.ps1", ["-Unpaced"])
        self.assertEqual(self.parse_errors(job), 0)
        text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
        line = "    'MLVAPP_PLAYBACK_UNPACED=1',\n"
        self.assertEqual(text.count("MLVAPP_PLAYBACK_UNPACED"), 1)
        self.assertEqual(text.count("-DropFrameMode off"), 1)
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, text)
        self.assertIn(" -DropFrameMode off -UsePersistedPlaybackSettings ", text)
        self.assertEqual(text.replace(line, "").replace(" -DropFrameMode off -UsePersistedPlaybackSettings ", " -UsePersistedPlaybackSettings "), plain,
                         "the flag and the drop-frame switch are the only differences from the plain job")
        both = self.generate(GENERATOR, "up-pt.job.ps1", ["-Unpaced", "-PresentTrace", "-SwapInterval", "0"])
        self.assertEqual(self.parse_errors(both), 0)
        both_text = both.read_text(encoding="utf-8")
        for token in ("'MLVAPP_PLAYBACK_UNPACED=1',", "'MLVAPP_PLAYBACK_PRESENT_TRACE=1',", "'MLVAPP_SWAP_INTERVAL=0',", "-DropFrameMode off"):
            self.assertEqual(both_text.count(token), 1, token)
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = "if ($spec.generatorArgs.PSObject.Properties['unpaced'] -and [bool]$spec.generatorArgs.unpaced) { $gen['Unpaced'] = $true }"
        self.assertIn(mapping, runner)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")
        # the runner fails closed on the app's own line, after the display gate and before the proof gate
        gate = "if ($outcome -in @('PASS', 'FAIL') -and $unpacedVerdict.blocks) {"
        self.assertIn(gate, runner)
        self.assertLess(runner.index("DISPLAY_MODE_NOT_HONOURED"), runner.index(gate))
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["generatorArgs"]["properties"]["unpaced"]["type"], "boolean")

    def test_the_pointwise_fast_kill_switch_reaches_the_apps_env_only_when_asked(self) -> None:
        """VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: -DisablePrepPointwiseFast adds MLVAPP_DISABLE_PREP_POINTWISE_FAST=1 to the app's env list
        exactly once, beside an unpaced leg's own line too; absent, the job is the text it was. The runner maps generatorArgs.disablePrepPointwiseFast
        true for any leg (outside the forceLookAssist branch); the schema declares it."""
        plain = self.generate(GENERATOR, "pk-plain.job.ps1", []).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("MLVAPP_DISABLE_PREP_POINTWISE_FAST", plain)
        job = self.generate(GENERATOR, "pk-on.job.ps1", ["-DisablePrepPointwiseFast"])
        self.assertEqual(self.parse_errors(job), 0)
        text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
        line = "    'MLVAPP_DISABLE_PREP_POINTWISE_FAST=1',\n"
        self.assertEqual(text.count("MLVAPP_DISABLE_PREP_POINTWISE_FAST"), 1)
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, text)
        self.assertEqual(text.replace(line, ""), plain, "the kill switch is the only difference from the plain job")
        for name, extra in (("unpaced", ["-Unpaced", "-SwapInterval", "0"]),
                            ("unpaced-look", ["-Unpaced", "-SwapInterval", "0", "-ForceLookAssist", "-ContactSheet", "-ContactSheetFrames", "1", "-LookFlavor", "cinematic"]),
                            ("unpaced-pace", ["-Unpaced", "-SwapInterval", "0", "-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic"])):
            with self.subTest(variant=name):
                off = self.generate(GENERATOR, f"pk-{name}-off.job.ps1", extra).read_text(encoding="utf-8").replace("\r\n", "\n")
                on_job = self.generate(GENERATOR, f"pk-{name}-on.job.ps1", [*extra, "-DisablePrepPointwiseFast"])
                self.assertEqual(self.parse_errors(on_job), 0, name)
                on = on_job.read_text(encoding="utf-8").replace("\r\n", "\n")
                self.assertEqual(on.count("'MLVAPP_DISABLE_PREP_POINTWISE_FAST=1',"), 1, name)
                self.assertEqual(on.count("'MLVAPP_PLAYBACK_UNPACED=1',"), 1, name)
                self.assertEqual(on.replace(line, ""), off, f"{name}: the kill switch is the only difference from the same job without it")
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = ("if ($spec.generatorArgs.PSObject.Properties['disablePrepPointwiseFast'] -and $spec.generatorArgs.disablePrepPointwiseFast -is [bool] "
                   "-and $spec.generatorArgs.disablePrepPointwiseFast) { $gen['DisablePrepPointwiseFast'] = $true }")
        self.assertEqual(runner.count(mapping), 1)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["generatorArgs"]["properties"]["disablePrepPointwiseFast"]["type"], "boolean")

    def test_the_prep_conflated_move_switch_reaches_the_apps_env_only_when_asked(self) -> None:
        """PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1: -PrepConflatedMove adds MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE=1 to the app's env list exactly once;
        absent, the job is the text it was (the OFF arm sets nothing: the app reads the variable as set-and-not-0). The runner maps
        generatorArgs.prepConflatedMove true for any leg (outside the forceLookAssist branch); the schema declares it as the constant true."""
        plain = self.generate(GENERATOR, "pcm-plain.job.ps1", []).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE", plain)
        job = self.generate(GENERATOR, "pcm-on.job.ps1", ["-PrepConflatedMove"])
        self.assertEqual(self.parse_errors(job), 0)
        text = job.read_text(encoding="utf-8").replace("\r\n", "\n")
        line = "    'MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE=1',\n"
        self.assertEqual(text.count("MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE"), 1)
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, text)
        self.assertEqual(text.replace(line, ""), plain, "the switch is the only difference from the plain job")
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = ("if ($spec.generatorArgs.PSObject.Properties['prepConflatedMove'] -and $spec.generatorArgs.prepConflatedMove -is [bool] "
                   "-and $spec.generatorArgs.prepConflatedMove) { $gen['PrepConflatedMove'] = $true }")
        self.assertEqual(runner.count(mapping), 1)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["generatorArgs"]["properties"]["prepConflatedMove"]["const"], True)

    def test_the_runner_passes_a_legs_swap_interval_to_the_generator(self) -> None:
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("if ($spec.generatorArgs.PSObject.Properties['swapInterval']) { $gen['SwapInterval'] = [int]$spec.generatorArgs.swapInterval }", runner)
        # not nested under the speed-leg forced-look branch: a look leg's swapInterval reaches the generator too
        self.assertLess(runner.index("$gen['SwapInterval']"), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"))

    def test_the_runner_passes_a_speed_legs_forced_look_to_the_generator(self) -> None:
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist'] -and [bool]$spec.generatorArgs.forceLookAssist) {", runner)
        self.assertIn("$gen['ForceLookAssist'] = $true; $gen['LookPaceLeg'] = $true", runner)
        self.assertIn("if ($spec.generatorArgs.PSObject.Properties['lookFlavor']) { $gen['LookFlavor'] = [string]$spec.generatorArgs.lookFlavor }", runner)
        self.assertIn("if ($spec.generatorArgs.PSObject.Properties['playbackRenderLookaheadFrames']) { $gen['PlaybackRenderLookaheadFrames'] = [int]$spec.generatorArgs.playbackRenderLookaheadFrames }", runner)
        # CPU-DUALISO-REDUCED-ISO-NOTCH-1: a leg's playbackProcessing reaches the generator as -PlaybackProcessing.
        self.assertIn("if ($spec.generatorArgs.PSObject.Properties['playbackProcessing']) { $gen['PlaybackProcessing'] = [string]$spec.generatorArgs.playbackProcessing }", runner)

    def test_playback_processing_reaches_the_smoke_command_and_absent_changes_nothing(self) -> None:
        """CPU-DUALISO-REDUCED-ISO-NOTCH-1: -PlaybackProcessing receipt adds exactly '-PlaybackProcessing receipt' to the smoke runner's command (it forwards
        --playback-processing, so a cpu leg renders processed8 and reaches the CPU reduced dual-ISO recon); an absent argument emits the default job byte for byte."""
        default = self.generate(GENERATOR, "pp-default.job.ps1", ["-Backend", "cpu", "-ContactSheet", "-ContactSheetFrames", "4"])
        receipt = self.generate(GENERATOR, "pp-receipt.job.ps1", ["-Backend", "cpu", "-ContactSheet", "-ContactSheetFrames", "4", "-PlaybackProcessing", "receipt"])
        self.assertEqual(self.parse_errors(receipt), 0)
        self.assertNotIn("-PlaybackProcessing", default.read_text(encoding="utf-8"))
        text = receipt.read_text(encoding="utf-8")
        self.assertEqual(text.count("-UsePersistedPlaybackSettings -PlaybackProcessing receipt -RequireLookAssist:"), 1)
        self.assertEqual(text.replace("-PlaybackProcessing receipt ", ""), default.read_text(encoding="utf-8"))
        explicit_empty = self.generate(GENERATOR, "pp-empty.job.ps1", ["-Backend", "cpu", "-ContactSheet", "-ContactSheetFrames", "4", "-PlaybackProcessing", ""])
        self.assertEqual(explicit_empty.read_bytes(), default.read_bytes(), "an empty -PlaybackProcessing is the default job, byte for byte")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["generatorArgs"]["properties"]["playbackProcessing"]["enum"], ["receipt", "subset"])

    def test_disable_look_assist_reaches_the_smoke_command_and_absent_changes_nothing(self) -> None:
        """UPSTREAM-CPU-BASELINE-1: -DisableLookAssist adds exactly '-DisableLookAssist' to the smoke runner's command (it passes --no-look-assist and fails
        the run closed if the app still reports Look Assist enabled); absent, the job is the default byte for byte. The runner maps the leg's
        generatorArgs.disableLookAssist for any leg (outside the forced-look branch), and the schema types it boolean."""
        default = self.generate(GENERATOR, "nola-default.job.ps1", ["-Backend", "cpu", "-ScaleFactor", "1"])
        nola = self.generate(GENERATOR, "nola.job.ps1", ["-Backend", "cpu", "-ScaleFactor", "1", "-DisableLookAssist"])
        self.assertEqual(self.parse_errors(nola), 0)
        self.assertNotIn("-DisableLookAssist", default.read_text(encoding="utf-8"))
        text = nola.read_text(encoding="utf-8")
        self.assertEqual(text.count("-RequireLookAssist:`$false -DisableLookAssist -Scope none"), 1)
        self.assertEqual(text.replace("-DisableLookAssist ", ""), default.read_text(encoding="utf-8"))
        windowed = self.generate(GENERATOR, "nola-windowed.job.ps1", ["-Backend", "cpu", "-ScaleFactor", "1", "-DisplayMode", "windowed", "-DisableLookAssist"])
        self.assertEqual(self.parse_errors(windowed), 0)
        self.assertEqual(windowed.read_text(encoding="utf-8").count("-DisableLookAssist -Scope none"), 1)
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = "if ($spec.generatorArgs.PSObject.Properties['disableLookAssist'] -and [bool]$spec.generatorArgs.disableLookAssist) { $gen['DisableLookAssist'] = $true }"
        self.assertIn(mapping, runner)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["generatorArgs"]["properties"]["disableLookAssist"]["type"], "boolean")

    def test_the_scope_and_its_interval_reach_the_job_only_when_asked(self) -> None:
        """PLAYBACK-SCOPES-OPTION-1: -Scope <name> swaps the smoke command's one '-Scope none' for the named scope, and -ScopeIntervalMs <n> adds
        MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS=<n> to the app's env list; absent (or -Scope none) the job is the default byte for byte. The runner maps a
        leg's generatorArgs.scope / scopeIntervalMs for any leg, and the schema bounds both."""
        base = ["-Backend", "cpu", "-ScaleFactor", "1", "-DisplayMode", "windowed", "-DisableLookAssist"]
        default = self.generate(GENERATOR, "sc-default.job.ps1", base)
        plain = default.read_text(encoding="utf-8")
        self.assertEqual(plain.count(" -Scope none "), 1)
        self.assertNotIn("MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS", plain)
        explicit_none = self.generate(GENERATOR, "sc-none.job.ps1", [*base, "-Scope", "none"])
        self.assertEqual(explicit_none.read_bytes(), default.read_bytes(), "-Scope none is the default job, byte for byte")
        hist = self.generate(GENERATOR, "sc-hist.job.ps1", [*base, "-Scope", "histogram"])
        self.assertEqual(self.parse_errors(hist), 0)
        text = hist.read_text(encoding="utf-8")
        self.assertEqual(text.count("-DisableLookAssist -Scope histogram "), 1)
        self.assertEqual(text.replace(" -Scope histogram ", " -Scope none "), plain, "the scope is the only difference from the default job")
        both = self.generate(GENERATOR, "sc-hist-i0.job.ps1", [*base, "-Scope", "histogram", "-ScopeIntervalMs", "0"])
        self.assertEqual(self.parse_errors(both), 0)
        both_text = both.read_text(encoding="utf-8").replace("\r\n", "\n")
        line = "    'MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS=0',\n"
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, both_text)
        self.assertEqual(both_text.replace(line, ""), text.replace("\r\n", "\n"), "the interval line is the only difference from the scope-only job")
        cuda = self.generate(GENERATOR, "sc-cuda.job.ps1", ["-ScaleFactor", "1", "-DisplayMode", "windowed", "-DisableLookAssist", "-Scope", "waveform"])
        self.assertEqual(self.parse_errors(cuda), 0)
        self.assertEqual(cuda.read_text(encoding="utf-8").count(" -Scope waveform -FrameTelemetry"), 1)
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        for mapping in ("if ($spec.generatorArgs.PSObject.Properties['scope']) { $gen['Scope'] = [string]$spec.generatorArgs.scope }",
                        "if ($spec.generatorArgs.PSObject.Properties['scopeIntervalMs']) { $gen['ScopeIntervalMs'] = [int]$spec.generatorArgs.scopeIntervalMs }"):
            self.assertIn(mapping, runner)
            self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                            "the mapping must sit outside the forceLookAssist branch")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        props = schema["properties"]["generatorArgs"]["properties"]
        self.assertEqual(props["scope"]["enum"], ["none", "histogram", "waveform", "parade", "vectorscope"])
        self.assertEqual((props["scopeIntervalMs"]["type"], props["scopeIntervalMs"]["minimum"], props["scopeIntervalMs"]["maximum"]), ("integer", 0, 1000))

    def test_the_scopes_mode_reaches_the_job_only_when_asked(self) -> None:
        """PLAYBACK-SCOPES-OPTION-1 r4 (PSO-01): -ScopesDuringPlayback <mode> adds MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK=<mode> to the app's env
        list (the app honours it only in an automation run, which the smoke is); absent the job is the default byte for byte. The runner maps a
        leg's generatorArgs.scopesDuringPlayback for any leg, and the schema admits only the three modes."""
        base = ["-Backend", "cpu", "-ScaleFactor", "1", "-DisplayMode", "windowed", "-DisableLookAssist", "-Scope", "histogram"]
        scope_only = self.generate(GENERATOR, "sm-scope.job.ps1", base).read_text(encoding="utf-8").replace("\r\n", "\n")
        self.assertNotIn("MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK", scope_only)
        pinned = self.generate(GENERATOR, "sm-throttled.job.ps1", [*base, "-ScopesDuringPlayback", "throttled"])
        self.assertEqual(self.parse_errors(pinned), 0)
        pinned_text = pinned.read_text(encoding="utf-8").replace("\r\n", "\n")
        line = "    'MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK=throttled',\n"
        self.assertIn("'MLVAPP_PLAYBACK_PHASE3_UNATTENDED=1',\n" + line, pinned_text)
        self.assertEqual(pinned_text.replace(line, ""), scope_only, "the mode line is the only difference from the scope-only job")
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = ("if ($spec.generatorArgs.PSObject.Properties['scopesDuringPlayback']) "
                   "{ $gen['ScopesDuringPlayback'] = [string]$spec.generatorArgs.scopesDuringPlayback }")
        self.assertIn(mapping, runner)
        self.assertLess(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"),
                        "the mapping must sit outside the forceLookAssist branch")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["generatorArgs"]["properties"]["scopesDuringPlayback"]["enum"], ["hide", "throttled", "live"])

    def test_the_raw_prefetch_decoder_count_reaches_the_job_only_when_asked(self) -> None:
        """PLAYBACK-LJ92-DECODE-THROUGHPUT-1: generatorArgs.rawPrefetchDecoders maps to -RawPrefetchDecoders for any leg (not only a forced-look
        pace leg); 0 is the no-prefetch arm, k the decoder count, and absent leaves the job's text exactly as it was."""
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = "if ($spec.generatorArgs.PSObject.Properties['rawPrefetchDecoders']) { $gen['RawPrefetchDecoders'] = [int]$spec.generatorArgs.rawPrefetchDecoders }"
        self.assertIn(mapping, runner)
        forced = runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']")
        self.assertLess(runner.index(mapping), forced, "the mapping must sit outside the forceLookAssist branch")
        absent = self.generate(GENERATOR, "rawpf-absent.job.ps1", []).read_text(encoding="utf-8")
        self.assertNotIn("MLVAPP_RAW_UINT16_PREFETCH_DECODERS", absent)
        self.assertNotIn("MLVAPP_DISABLE_RAW_UINT16_PREFETCH", absent)
        off = self.generate(GENERATOR, "rawpf-0.job.ps1", ["-RawPrefetchDecoders", "0"])
        self.assertEqual(self.parse_errors(off), 0)
        off_text = off.read_text(encoding="utf-8")
        self.assertIn("'MLVAPP_DISABLE_RAW_UINT16_PREFETCH=1',", off_text)
        self.assertNotIn("MLVAPP_RAW_UINT16_PREFETCH_DECODERS", off_text)
        two = self.generate(GENERATOR, "rawpf-2.job.ps1", ["-RawPrefetchDecoders", "2"])
        self.assertEqual(self.parse_errors(two), 0)
        two_text = two.read_text(encoding="utf-8")
        self.assertIn("'MLVAPP_RAW_UINT16_PREFETCH_DECODERS=2',", two_text)
        self.assertNotIn("MLVAPP_DISABLE_RAW_UINT16_PREFETCH", two_text)
        pace = self.generate(GENERATOR, "rawpf-pace-1.job.ps1", ["-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic", "-RawPrefetchDecoders", "1"])
        self.assertIn("'MLVAPP_RAW_UINT16_PREFETCH_DECODERS=1',", pace.read_text(encoding="utf-8"))
        # The smoke launcher clears the variable unless the job passes it, so a stray value never leaks into a leg.
        self.assertIn('"MLVAPP_RAW_UINT16_PREFETCH_DECODERS",', LAUNCHER.read_text(encoding="utf-8"))

    def test_scale_and_quiescence_parameters_reach_the_job(self) -> None:
        text = self.generate(GENERATOR, "scale.job.ps1", ["-ScaleFactor", "1", "-CpuQuiescenceThresholdPercent", "35.5"]).read_text(encoding="utf-8")
        self.assertIn("-ScaleFactor 1 -UsePersistedPlaybackSettings", text)
        self.assertIn("$cpuThresholdPercent = 35.5", text)

    def test_play_seconds_still_reaches_the_smoke_command_on_every_variant(self) -> None:
        for name, extra in (("um", ["-Venue", "ultra-magnus", "-PlaySeconds", "28"]), ("cpu", ["-Backend", "cpu", "-PlaySeconds", "28"])):
            text = self.generate(GENERATOR, f"play-{name}.job.ps1", extra).read_text(encoding="utf-8")
            self.assertIn("$PlaySeconds = 28", text, name)

    def test_refused_combinations_throw_before_emitting(self) -> None:
        cases = ((["-Backend", "cpu", "-DisablePaintPerSubmit"], "DUAL_VENUE_CPU_BACKEND_CONFLICT"),
                 (["-ForceLookAssist"], "DUAL_VENUE_LOOK_REQUIRES_CONTACT_SHEET"),
                 (["-LookPaceLeg"], "DUAL_VENUE_LOOK_PACE_LEG_SHAPE"),
                 (["-ForceLookAssist", "-ContactSheet", "-LookPaceLeg"], "DUAL_VENUE_LOOK_PACE_LEG_SHAPE"),
                 (["-PlaybackRenderLookaheadFrames", "3"], "DUAL_VENUE_LOOKAHEAD_NEEDS_LOOK_PACE_LEG"),
                 (["-ForceLookAssist", "-ContactSheet", "-DisableLookAssist"], "DUAL_VENUE_LOOK_ASSIST_CONFLICT"),
                 (["-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic", "-DisableLookAssist"], "DUAL_VENUE_LOOK_ASSIST_CONFLICT"))
        outs = [self.tmp / f"refused-{n}.job.ps1" for n in range(len(cases))]
        procs = run_pwsh_many([self.generator_command(GENERATOR, out, extra) for out, (extra, _token) in zip(outs, cases)])   # (independent runs, one pwsh each)
        for out, (extra, token), proc in zip(outs, cases, procs):
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn(token, proc.stdout + proc.stderr)
            self.assertFalse(out.exists())

    def test_master_still_refuses_a_fixture_at_its_tracked_length_and_a_short_window(self) -> None:
        # The tracked 2/16-frame fixtures are refused AT GENERATION (master's gate, kept intact by the merge) ...
        out = self.tmp / "short.job.ps1"
        proc = run_pwsh(["-File", str(GENERATOR), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                         "-FixtureSha256", "cd" * 32, "-RepoRoot", str(ROOT), "-OutFile", str(out)])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CLIP_TOO_SHORT", proc.stdout + proc.stderr)
        self.assertFalse(out.exists())
        # ... and a play window under the lowest floor (PLAYBACK-CLIP-LENGTH-HFR-10S-1: an HFR clip's 10 s) never binds.
        proc = run_pwsh(["-File", str(GENERATOR), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                         "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), "-PlaySeconds", "9"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(out.exists())

    def test_a_venue_table_root_outside_the_allowlist_is_refused(self) -> None:
        table = json.loads((DV / "venues.json").read_text(encoding="utf-8"))
        table["venues"]["ultra-magnus"]["scratchRoot"] = "G:\\Temp\\x'; Remove-Item C:\\ #"
        bad = self.tmp / "bad-venues.json"
        bad.write_text(json.dumps(table), encoding="utf-8")
        out = self.tmp / "bad-table.job.ps1"
        proc = run_pwsh(["-File", str(GENERATOR), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                         "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), "-Venue", "ultra-magnus",
                         "-VenueTablePath", str(bad)])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DUAL_VENUE_TABLE_INVALID", proc.stdout + proc.stderr)
        self.assertFalse(out.exists())

    def test_venues_json_bachelor_entry_equals_the_generator_defaults(self) -> None:
        table = json.loads((DV / "venues.json").read_text(encoding="utf-8"))
        self.assertEqual(table["venues"]["bachelor"]["agentRoot"], "C:\\mlvtmp\\mlv-agent")
        self.assertEqual(table["venues"]["bachelor"]["scratchRoot"], "C:\\mlvtmp")
        src = GENERATOR.read_text(encoding="utf-8")
        self.assertIn("[string]$AgentRoot = 'C:\\mlvtmp\\mlv-agent'", src)
        self.assertIn("$DefaultScratchRoot = 'C:\\mlvtmp'", src)

    def test_the_generator_returns_the_clip_content_hash_for_the_receipt_subject(self) -> None:
        out = self.tmp / "ret.job.ps1"
        script = (f"$r = & '{GENERATOR}' -SourceCommit '{self.head}' -BuildManifestSha256 '{'ab' * 32}' -ClipId '{FIXTURE_IDS[0]}' "
                  f"-FixtureSha256 '{'CD' * 32}' -RepoRoot '{self.repo}' -OutFile '{out}' -PlaySeconds 27\n"
                  "Write-Output ('CONTENT=' + $r.clipContentSha256); Write-Output ('PLAY=' + $r.playSeconds)")
        proc = run_pwsh(["-Command", script])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("CONTENT=" + "cd" * 32, proc.stdout)
        self.assertIn("PLAY=27", proc.stdout)


# ---------------------------------------------------------------------------------------------------
STUB_UM_RUN = r"""
param([string]$ScriptPath,[string]$JobId,[string]$AgentShare,[int]$TimeoutSec,[int]$MaxQueueWaitSec,[int]$MaxClaimedWaitSec)
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
Add-Content -LiteralPath $cfg.log -Value $JobId
if ($JobId -like '*-health') {
    if ($cfg.healthMode -eq 'unresolved') { throw 'UNRESOLVED: stub health probe never returned' }
    return [pscustomobject]@{ exitCode = 0; stdout = ('DVE_PROBE=' + ($cfg.probe | ConvertTo-Json -Compress)) }
}
# DUAL-VENUE-DISPLAY-MATRIX-1: a leg-set run submits many legs through one stub, each with its own artifacts (artifactsByLeg: a job-id fragment -> that leg's artifacts path)
if ($cfg.PSObject.Properties['artifactsByLeg']) { foreach ($p in $cfg.artifactsByLeg.PSObject.Properties) { if ($JobId -like ('*' + $p.Name + '*')) { $cfg.artifactsAgentPath = $p.Value } } }
switch ($cfg.mainMode) {
    'token'      { return [pscustomobject]@{ exitCode = [int]$cfg.exitCode; stdout = ('RESULT=' + $cfg.token + ' ARTIFACTS=' + $cfg.artifactsAgentPath) } }
    'retracted'  { throw 'RETRACTED: stub queue ceiling reached; withdrawn from the inbox' }
    'unresolved' { throw 'UNRESOLVED: stub agent may still own the job' }
    'backend'    { return [pscustomobject]@{ exitCode = 13; stdout = 'RESULT=BACKEND_NOT_AVAILABLE ARTIFACTS=' + $cfg.artifactsAgentPath } }
    'captured-nonzero' { return [pscustomobject]@{ exitCode = 1; stdout = 'RESULT=MEASUREMENT_CAPTURED ARTIFACTS=' + $cfg.artifactsAgentPath } }
    'source-frames-invalid' { return [pscustomobject]@{ exitCode = 29; stdout = 'RESULT=SOURCE_FRAMES_INVALID SOURCE_ADVANCED=12 REQUIRED_SOURCE_FRAMES=600 WRAPPED=True ARTIFACTS=' + $cfg.artifactsAgentPath } }
    default      { return [pscustomobject]@{ exitCode = 0; stdout = 'RESULT=MEASUREMENT_CAPTURED ARTIFACTS=' + $cfg.artifactsAgentPath } }
}
"""

# The stub records every named argument it was handed (so a test can see that the runner passed a clip ID and a play
# window, and never a path), and can be told to refuse the way the real generator does.
STUB_GENERATOR = r"""
param($SourceCommit,$BuildManifestSha256,$ClipId,$FixtureSha256,$OutFile,$RepoRoot,$Venue,$Backend,$ScaleFactor,$TelemetryArm,$CpuQuiescenceThresholdPercent,[switch]$ContactSheet,$ContactSheetFrames,[switch]$ForceLookAssist,$LookFlavor,$VenueTablePath,$PlaySeconds,$ExpectedScaleRequest,$DisplayMode,[switch]$VisualCapture,$AutomationSettingsDir,$GpuPreLaunches)
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
Add-Content -LiteralPath $cfg.genLog -Value (($PSBoundParameters.Keys | Sort-Object | ForEach-Object { $_ + '=' + $PSBoundParameters[$_] }) -join ';')
if ($cfg.genRefusal) { throw $cfg.genRefusal }
Set-Content -LiteralPath $OutFile -Value "# stub job Venue=$Venue Backend=$Backend Look=$ForceLookAssist"
[pscustomobject]@{ outFile = $OutFile; recommendedJobTimeoutSec = 600; smokeRunnerClosureDirName = 'smoke-runner-stub'
                   clipContentSha256 = $cfg.clipContentSha256; playSeconds = $PlaySeconds; fixtureRehearsal = $false }
"""

HEALTHY_PROBE = {"pwshColdStartMs": 500, "smallHashMs": 40, "freeDiskGiB": 600.0, "commitUsedGiB": 40.0, "commitLimitGiB": 128.0,
                 "hostName": "ULTRA-MAGNUS", "gpuNames": ["NVIDIA GeForce RTX 4090"], "driverVersion": "32.0.1", "displayDevice": "\\\\.\\DISPLAY2",
                 "presentmonSha256": "9b" * 32,
                 # VENUE-SESSION-LOCKED-REFUSAL-1: the probe reports the console lock state; only a JSON false is healthy
                 "sessionLocked": False}
BACHELOR_PROBE = dict(HEALTHY_PROBE, hostName="BACHELOR")
CLIP_CONTENT_SHA = "ab" * 32


def consent_record(venue: str, clip: str) -> dict:
    """A FAKE owner-typed consent record (tests only): the owner's exact line ("CLIP <venue>: <clip id>", no path) and its sha256."""
    line = f"CLIP {venue}: {clip}"
    return {"venue": venue, "clipId": clip, "ownerLine": line, "ownerLineSha256": hashlib.sha256(line.encode()).hexdigest(),
            "recordedUtc": "2026-10-01T22:10:00Z", "recordedBy": "owner"}


def consent_record_line(venue: str, clip: str, line: str) -> dict:
    """A FAKE consent record whose ownerLine is exactly `line` (tests only), with the matching sha256: for spellings other than the table's."""
    return dict(consent_record(venue, clip), ownerLine=line, ownerLineSha256=hashlib.sha256(line.encode()).hexdigest())


def consent_file(*records: dict, blankets: list[dict] | None = None) -> dict:
    doc = {"schema": "mlv-app/dual-venue-clip-consent/v1", "records": list(records)}
    if blankets is not None:
        doc["blanketRecords"] = list(blankets)
    return doc


# VENUE-CLIP-CONSENT-BLANKET-1: the owner's blanket sentence (ledger stamp 2026-10-11T03:41:42Z), byte-exact -- note: no final period.
BLANKET_SENTENCE = "You have my blanket permission to use any MLV clip you like going forward. No need to ask for permission"
BLANKET_SENTENCE_SHA256 = "48dc8dead951aa2eab911db63591b59d238830d2d98c0fd7df346c42b78f9fd6"   # a literal, never recomputed here


def blanket_record(**overrides) -> dict:
    """A blanket consent record exactly as the owner's sentence is recorded; overrides replace fields WITHOUT re-hashing (a forgery stays a forgery)."""
    return dict({"kind": "blanket", "ownerSentence": BLANKET_SENTENCE, "ownerSentenceSha256": BLANKET_SENTENCE_SHA256,
                 "recordedUtc": "2026-10-11T03:41:42Z", "recordedBy": "owner", "writtenBy": "owner",
                 "source": "ledger 2026-10-11T03:41:42Z", "scope": "any clip id, any venue"}, **overrides)


class RunnerHarness:
    """Shared setup: a temp share, venue table, stub um-run/generator, a fake consent file, and a runner invocation
    that can use a MUTATED COPY of the runner directory (for the mutation tests)."""

    def make_harness(self, cleanup_class_gone: bool = True) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dve-run-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.share = self.tmp / "share"
        (self.share / "cache").mkdir(parents=True)
        self.receipts = self.tmp / "dv" / "receipts"
        self.log = self.tmp / "stub.log"
        self.gen_log = self.tmp / "gen.log"
        self.um = self.tmp / "um-run-stub.ps1"
        self.um.write_text(STUB_UM_RUN, encoding="utf-8")
        self.gen = self.tmp / "gen-stub.ps1"
        self.gen.write_text(STUB_GENERATOR, encoding="utf-8")
        self.sha = "e" * 40
        self.build_json = self.share / "cache" / f"playback-attr-3-cuda-{self.sha[:12]}-build.json"
        self.build_json.write_text('{"stub": true}', encoding="utf-8")
        (self.share / "cache" / "smoke-runner-stub").mkdir()
        self.build_sha = hashlib.sha256(self.build_json.read_bytes()).hexdigest()
        # A temp venue table: real thresholds/roles shape, share pointing at the temp dir.
        real = json.loads((DV / "venues.json").read_text(encoding="utf-8"))
        for name in ("ultra-magnus", "bachelor"):
            real["venues"][name]["agentShare"] = str(self.share)
            real["venues"][name]["agentRoot"] = "X:\\stub\\agent"
        real["ownerFootage"]["cleanupClassGone"] = cleanup_class_gone
        self.table = self.tmp / "venues.json"
        self.table.write_text(json.dumps(real), encoding="utf-8")
        self.consent = self.tmp / "venue-clip-consent.json"
        self.write_consent(consent_record("ultra-magnus", OWNER_CLIP), consent_record("bachelor", OWNER_CLIP))
        self.stub_cfg = self.tmp / "stub.json"
        self.artifacts = self.share / "outbox" / "unit.artifacts"

    def write_consent(self, *records: dict, blankets: list[dict] | None = None) -> None:
        self.consent.write_text(json.dumps(consent_file(*records, blankets=blankets)), encoding="utf-8")

    def write_spec(self, card: str = "DUAL-VENUE-EVIDENCE-1", clip: str = OWNER_CLIP, leg_type: str = "speed", play_seconds: int | None = 25,
                   flavor: str = "classic", scale: int = 4, accepted: dict | None = None, leg_id: str = "unit-leg", display_mode: str | None = None) -> Path:
        spec = {
            "schema": "mlv-app/dual-venue-leg/v1", "legId": leg_id, "card": card, "legType": leg_type, "clipId": clip,
            "backends": ["cuda", "cpu"], "scaleFactor": scale,
            "timeouts": {"queueWaitSec": 60, "extraClaimedWaitSec": 60},
            "criteria": {"acceptance": {"cuda": [{"metric": "rows", "op": "gt", "value": 0}], "cpu": []},
                         "supplementary": {"cuda": [{"metric": "rows", "op": "gt", "value": 0}], "cpu": []}},
        }
        if play_seconds is not None:
            spec["playSeconds"] = play_seconds
        if leg_type == "look":
            spec["look"] = {"contactSheetFrames": 2, "lookFlavor": flavor}
        if accepted is not None:
            spec["acceptedEffectiveScale"] = accepted
        if display_mode is not None:
            spec["displayMode"] = display_mode
        suffix = ("" if display_mode is None else f"-dm{display_mode}") + (("" if flavor == "classic" else f"-{flavor}") + ("" if scale == 4 else f"-s{scale}") + ("" if leg_id == "unit-leg" else f"-{leg_id}")
                  + ("" if accepted is None else "-acc" + "".join(f"{k}{v}" for k, v in sorted(accepted.items()))))
        path = self.tmp / f"spec-{card}-{clip}-{leg_type}-{play_seconds}{suffix}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def write_artifacts(self, summary: dict | None = None, manifest: dict | None = None, source_frames: dict | None | bool = True,
                        nonce: str | None = NONCE, sheet: bool = False, line: dict | None = None, observed_nonce: str | None | bool = True,
                        manifest_nonce: str | None | bool = True, log: bool = True, result: bool = True, isolated: str | None = "run_scoped",
                        declared_log_sha: str | None = None, compose_marker: str | None = None, raw_frames: bool = False,
                        exact_summary: dict | None = None, extra_log_lines: list[str] | None = None,
                        look_assist_state: str | None = "look_assist_enabled=1") -> None:
        """Write the job's artifacts the way master's generator does, INCLUDING the run's own records the receipt is re-derived
        from: the launcher's result.json (evidence.runNonce = the nonce it generated, the sha256 of the log snapshot) and
        logs/smoke-run.log (the app's playback_smoke.summary line carrying source_advanced / required_source_frames / native_fps /
        pace_fps / fps_override / wrapped / wrap_count and the run_nonce it echoed). `line` overrides summary-line fields;
        `nonce` is the launcher's (expected) nonce, `observed_nonce` what the app echoed (True = the same one, None = none).
        `look_assist_state` is the app's gui_smoke.visual_state fields (Look Assist on by default, as the app ships; None = no visual_state line)."""
        self.artifacts.mkdir(parents=True, exist_ok=True)
        for stale in ("logs/smoke-run.log", "result.json", "contact-sheet/sheet.png", "contact-sheet/compose-status.txt"):
            (self.artifacts / stale).unlink(missing_ok=True)   # a case that omits a record must not inherit the previous case's
        fields = {"source_advanced": 960, "required_source_frames": 600, "native_fps": "23.976", "pace_fps": "23.976", "fps_override": 0,
                  "wrapped": 0, "wrap_count": 0, "scale_request_last": 4, "scale_active_last": 4}
        fields.update(line or {})
        observed = nonce if observed_nonce is True else observed_nonce
        summary_line = ("playback_smoke.summary session=3 reason=play-stop elapsed_ms=30000.000 presented_frames=900 "
                        + " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
                        + (f" run_nonce={observed}" if observed is not None else ""))
        log_lines = []
        if isolated is not None:
            log_lines.append(f"interaction_trace event=automation.pacing_isolated site=gui-smoke-entry persisted_fps_override=0 persisted_frame_rate=24.000 drop_frame=1 settings_store={isolated}")
        if look_assist_state is not None:
            log_lines.append(f"interaction_trace event=gui_smoke.visual_state {look_assist_state}".rstrip())
        # a DECOY earlier session with another nonce: the measured session (the marker) is the one that counts
        log_lines += list(extra_log_lines or [])
        log_lines += ["playback_smoke.summary session=1 reason=warmup source_advanced=1 required_source_frames=1 native_fps=23.976 pace_fps=23.976 fps_override=0 wrapped=0 wrap_count=0 run_nonce=" + "0" * 32,
                      "playback_smoke.measured_session id=3", summary_line]
        log_bytes = ("\n".join(log_lines) + "\n").encode("utf-8")
        if log:
            (self.artifacts / "logs").mkdir(exist_ok=True)
            (self.artifacts / "logs" / "smoke-run.log").write_bytes(log_bytes)
        log_sha = hashlib.sha256(log_bytes).hexdigest()
        if result:
            evidence = {"runLogSnapshot": {"sha256": declared_log_sha or log_sha, "length": len(log_bytes)}}
            if nonce is not None:
                evidence["runNonce"] = nonce
            (self.artifacts / "result.json").write_text(json.dumps({"schema": "mlvapp-gui-smoke-result.v2", "evidence": evidence}), encoding="utf-8")
        body = {"result": "MEASUREMENT_CAPTURED", "fixtureRehearsal": False, "clipId": OWNER_CLIP, "rows": 900, "gpuFramesTotal": 900,
                "cpuFrames": 0, "artifactRoot": "X:\\stub"}
        if source_frames is True:
            body["sourceFrames"] = {"oracle": "source_advanced >= required_source_frames, wrapped=0, native pace, no fps override",
                                    "sourceAdvanced": fields["source_advanced"], "requiredSourceFrames": fields["required_source_frames"],
                                    "wrapped": bool(fields["wrapped"]), "failures": []}
        elif isinstance(source_frames, dict):
            body["sourceFrames"] = source_frames
        elif source_frames is None:
            body["sourceFrames"] = None
        body.update(summary or {})
        if exact_summary is not None:
            body = dict(exact_summary)   # a terminal's summary.json is NOT the capture defaults plus a result: it is exactly what the job emits there
        (self.artifacts / "summary.json").write_text(json.dumps(body), encoding="utf-8")
        man_nonce = nonce if manifest_nonce is True else manifest_nonce
        man = {"smokeRunLog": ({"runNonce": man_nonce, "sha256": log_sha} if man_nonce is not None else {"sha256": log_sha}), "presentMonStats": {"p50": 16.6}}
        man.update(manifest or {})
        (self.artifacts / "evidence-manifest.json").write_text(json.dumps(man), encoding="utf-8")
        if sheet:
            (self.artifacts / "contact-sheet").mkdir(exist_ok=True)
            (self.artifacts / "contact-sheet" / "sheet.png").write_bytes(b"\x89PNG\r\n\x1a\nstub")
        if compose_marker is not None:
            (self.artifacts / "contact-sheet").mkdir(exist_ok=True)
            (self.artifacts / "contact-sheet" / "compose-status.txt").write_text(compose_marker, encoding="utf-8")
        if raw_frames:
            raw = self.artifacts / "artifacts" / "contact-sheet" / "raw"
            raw.mkdir(parents=True, exist_ok=True)
            (raw / "frame-00.png").write_bytes(b"\x89PNG\r\n\x1a\nraw")

    def stamp_identity(self, venue: str, build_sha: str) -> None:
        """What the real job writes about WHO the run was for: summary.json's display.venue and the evidence manifest's clipId and
        buildManifest.sha256 (the receipt validator cross-checks all three). A value a test set on purpose is left alone."""
        summary_path = self.artifacts / "summary.json"
        if summary_path.exists():
            body = json.loads(summary_path.read_text(encoding="utf-8"))
            display = body.get("display") if isinstance(body.get("display"), dict) else {}
            # (a venue a test chose on purpose is kept; one WE stamped for an earlier leg on the same artifacts is re-stamped)
            if display.get("venue") is None or display.get("venue") in getattr(self, "_stamped_venues", set()):
                display["venue"] = venue
                self._stamped_venues = getattr(self, "_stamped_venues", set()) | {venue}
            body["display"] = display
            summary_path.write_text(json.dumps(body), encoding="utf-8")
        manifest_path = self.artifacts / "evidence-manifest.json"
        if manifest_path.exists():
            man = json.loads(manifest_path.read_text(encoding="utf-8"))
            man.setdefault("clipId", OWNER_CLIP)
            man.setdefault("buildManifest", {"name": "build.json", "sha256": build_sha})
            manifest_path.write_text(json.dumps(man), encoding="utf-8")

    def run_leg(self, venue: str, spec: Path, probe: dict | None = None, main_mode: str = "capture", health_mode: str = "ok",
                extra: list[str] | None = None, gen_refusal: str | None = None, dv: Path = DV, consent: Path | None = None,
                token: tuple[str, int] | None = None) -> tuple[subprocess.CompletedProcess, dict | None, list[str]]:
        self.stamp_identity(venue, self.build_sha)
        if token is not None:
            main_mode = "token"
        if probe is None:
            probe = BACHELOR_PROBE if venue == "bachelor" else HEALTHY_PROBE
        cfg = {"log": str(self.log), "genLog": str(self.gen_log), "probe": probe, "mainMode": main_mode, "healthMode": health_mode,
               "artifactsAgentPath": "X:\\stub\\agent\\outbox\\unit.artifacts", "genRefusal": gen_refusal, "clipContentSha256": CLIP_CONTENT_SHA,
               "token": token[0] if token else None, "exitCode": token[1] if token else 0}
        self.stub_cfg.write_text(json.dumps(cfg), encoding="utf-8")
        self.log.write_text("", encoding="utf-8")
        self.gen_log.write_text("", encoding="utf-8")
        before = set(self.receipts.rglob("*.json")) if self.receipts.exists() else set()
        proc = run_pwsh(["-File", str(dv / "Invoke-VenueLeg.ps1"), "-Venue", venue, "-LegSpec", str(spec), "-SourceCommit", self.sha,
                         "-BuildManifestSha256", self.build_sha, "-VenueTablePath", str(self.table), "-ReceiptRoot", str(self.receipts),
                         "-OfflineTestMode", "-UmRunScript", str(self.um), "-GeneratorScript", str(self.gen), "-WorkDir", str(self.tmp / "work"),
                         "-ConsentPath", str(consent or self.consent), "-RepoRoot", str(ROOT), "-Actor", "unit-test", *(extra or [])],
                        env_extra={"DVE_STUB": str(self.stub_cfg)})
        after = sorted((set(self.receipts.rglob("*.json")) if self.receipts.exists() else set()) - before, key=lambda f: f.stat().st_mtime_ns)
        receipt = json.loads(after[-1].read_text(encoding="utf-8")) if after else None
        submitted = [l.strip() for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]
        return proc, receipt, submitted

    def generator_calls(self) -> list[str]:
        return [l for l in self.gen_log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def mutated_runner(self, mutations: list[tuple[str, str, str]]) -> Path:
        """A COPY of the runner directory (plus the two siblings it loads) with each (file, old, new) edit applied. The
        edit MUST apply (the old text must be present exactly once) so a refactor cannot silently void a mutation test."""
        root = self.tmp / ("mut-" + hashlib.sha1(json.dumps(mutations).encode()).hexdigest()[:8])
        dv = root / "tools" / "profiling" / "dual-venue"
        shutil.copytree(DV, dv)
        (root / "tools" / "profiling" / "bachelor").mkdir(parents=True)
        shutil.copyfile(ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1", root / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1")
        shutil.copyfile(ROOT / "tools" / "profiling" / "gui-smoke-length-gate.ps1", root / "tools" / "profiling" / "gui-smoke-length-gate.ps1")
        # the ONE display-identity parser the runner reads the app's window placement with (DUAL-VENUE-DISPLAY-MATRIX-1)
        shutil.copyfile(ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1", root / "tools" / "profiling" / "gui-smoke-display-identity.ps1")
        for name, old, new in mutations:
            path = dv / name
            text = path.read_text(encoding="utf-8")
            self.assertEqual(text.count(old), 1, f"mutation anchor must occur exactly once in {name}: {old!r}")
            path.write_text(text.replace(old, new), encoding="utf-8")
        return dv


@requires_windows_pwsh
class RunnerReceiptTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        self.make_harness()

    # -- each terminal writes a receipt ----------------------------------------------------------
    def test_unhealthy_venue_writes_a_receipt_and_the_leg_is_not_submitted(self) -> None:
        sick = dict(HEALTHY_PROBE, pwshColdStartMs=9000)
        proc, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), probe=sick)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")
        self.assertIn("pwshColdStartMs", receipt["outcomeDetail"])
        self.assertEqual(receipt["health"]["outcome"], "UNHEALTHY")
        self.assertEqual(receipt["health"]["pwshColdStartMs"], 9000)
        self.assertEqual(len(submitted), 1, f"only the health probe may be submitted, got {submitted}")
        self.assertTrue(submitted[0].endswith("-health"))

    def test_an_unanswered_health_probe_is_unhealthy_not_healthy(self) -> None:
        proc, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), health_mode="unresolved")
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")
        self.assertEqual(receipt["evidence"]["umRunOutcome"], "UNRESOLVED")
        self.assertEqual(len(submitted), 1)

    def test_host_mismatch_is_a_typed_terminal_not_a_note(self) -> None:
        wrong = dict(HEALTHY_PROBE, hostName="BACHELOR")  # a bachelor host answering for a declared ultra-magnus
        proc, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), probe=wrong)
        self.assertEqual(receipt["outcome"], "VENUE_HOST_MISMATCH")
        self.assertEqual(receipt["venue"]["declared"], "ultra-magnus")
        self.assertEqual(receipt["venue"]["detected"], "bachelor")
        self.assertEqual(len(submitted), 1)
        # ... and the reverse: an UM host answering for a declared bachelor venue.
        proc, receipt, submitted = self.run_leg("bachelor", self.write_spec(), probe=HEALTHY_PROBE)
        self.assertEqual(receipt["outcome"], "VENUE_HOST_MISMATCH")
        self.assertEqual(receipt["venue"]["detected"], "ultra-magnus")

    def test_retracted_and_unresolved_from_um_run_are_receipted_as_such(self) -> None:
        for mode, expected in (("retracted", "RETRACTED"), ("unresolved", "UNRESOLVED")):
            proc, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), main_mode=mode)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(receipt["outcome"], expected)
            self.assertEqual(receipt["evidence"]["umRunOutcome"], expected)
            # round 3: no registry snapshot / restore job is submitted (master isolates an automation run's settings store)
            self.assertIsNone(receipt["registry"])
            self.assertEqual(len(submitted), 2, f"only the health probe and the leg are submitted, got {submitted}")
            self.assertTrue(submitted[0].endswith("-health"))
            self.assertFalse(any("reg" in s.rsplit("-", 1)[-1] for s in submitted))

    def test_a_build_that_is_not_staged_is_device_unavailable_never_a_different_build(self) -> None:
        self.build_json.write_text('{"stub": "different bytes"}', encoding="utf-8")
        proc, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(receipt["outcome"], "DEVICE_UNAVAILABLE")
        self.assertIn("BUILD_NOT_STAGED", receipt["outcomeDetail"])
        self.assertEqual(len(submitted), 1)

    def test_a_typed_job_refusal_maps_to_a_venue_outcome_and_a_capture_to_pass_or_fail(self) -> None:
        self.write_artifacts()
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        # metrics are copied VERBATIM from summary.json
        self.assertEqual(receipt["metrics"]["rows"], 900)
        self.assertEqual(receipt["metrics"]["gpuFramesTotal"], 900)
        self.assertEqual(receipt["evidence"]["summaryJsonSha256"], hashlib.sha256((self.artifacts / "summary.json").read_bytes()).hexdigest())
        # the same capture on the informational cpu backend passes with no gating criteria ...
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS")
        self.assertIn("informational", receipt["outcomeDetail"])
        self.assertEqual(receipt["subject"]["backend"], "cpu")
        # ... and a failing criterion is a FAIL that names it
        self.write_artifacts(summary={"rows": 0})
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(receipt["outcome"], "FAIL")
        self.assertIn("rows gt 0", receipt["outcomeDetail"])
        # a typed job refusal is a venue outcome, not a product FAIL
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), main_mode="backend")
        self.assertEqual(receipt["outcome"], "DEVICE_UNAVAILABLE")

    # -- roles are data ------------------------------------------------------------------------------
    def test_role_comes_from_venues_json_acceptance_on_bachelor_supplementary_on_um(self) -> None:
        spec = self.write_spec(card="PLAYBACK-HFR-CONFORM-DEFAULT-1")
        _, bachelor, _ = self.run_leg("bachelor", spec, extra=["-HealthOnly"])
        _, um, _ = self.run_leg("ultra-magnus", spec, extra=["-HealthOnly"])
        self.assertEqual(bachelor["venue"]["role"], "acceptance")
        self.assertEqual(um["venue"]["role"], "supplementary")
        _, other, _ = self.run_leg("bachelor", self.write_spec(card="SOME-UNLISTED-CARD"), extra=["-HealthOnly"])
        self.assertEqual(other["venue"]["role"], "supplementary", "an unlisted card gets defaultRole")
        # the shipped table itself (not the temp copy)
        table = json.loads((DV / "venues.json").read_text(encoding="utf-8"))
        self.assertEqual(table["roles"]["PLAYBACK-HFR-CONFORM-DEFAULT-1"], {"bachelor": "acceptance", "ultra-magnus": "supplementary"})
        self.assertEqual(table["defaultRole"], "supplementary")

    def test_health_only_never_submits_the_leg_and_is_not_a_verdict(self) -> None:
        proc, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), extra=["-HealthOnly"])
        self.assertEqual(receipt["outcome"], "UNRESOLVED")
        self.assertIn("HEALTH_ONLY_NO_LEG_RUN", receipt["outcomeDetail"])
        self.assertEqual(len(submitted), 1)

    # -- append-only ------------------------------------------------------------------------------
    def test_receipts_are_never_overwritten(self) -> None:
        for _ in range(2):
            self.run_leg("ultra-magnus", self.write_spec(), extra=["-HealthOnly"])
        files = sorted(self.receipts.rglob("*.json"))
        self.assertEqual(len(files), 2, "two runs are two receipts")
        self.assertEqual(len({f.name for f in files}), 2)
        module = DV / "DualVenueRunner.psm1"
        direct = self.tmp / "direct"
        script = (f"Import-Module '{module}' -Force\n"
                  "$r = New-DvReceipt -Card 'C' -LegId 'l' -DeclaredVenue 'ultra-magnus' -Role 'supplementary' -Actor 'a'\n"
                  "$r['outcome'] = 'UNRESOLVED'\n"
                  f"$p = Write-DvReceipt -Receipt $r -ReceiptRoot '{direct}'\n"
                  f"try {{ Write-DvReceipt -Receipt $r -ReceiptRoot '{direct}' | Out-Null; 'OVERWRITTEN' }} catch {{ 'REFUSED' }}\n"
                  "$r2 = New-DvReceipt -Card 'C' -LegId 'l' -DeclaredVenue 'ultra-magnus'; $r2['outcome'] = 'GREAT'\n"
                  f"try {{ Write-DvReceipt -Receipt $r2 -ReceiptRoot '{direct}' | Out-Null; 'ACCEPTED_BAD_ENUM' }} catch {{ 'ENUM_REFUSED' }}\n")
        proc = run_pwsh(["-Command", script])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REFUSED", proc.stdout.split())
        self.assertNotIn("OVERWRITTEN", proc.stdout)
        self.assertIn("ENUM_REFUSED", proc.stdout.split())

    def test_every_receipt_has_the_full_schema_shape_and_the_amendment_fields(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), probe=dict(HEALTHY_PROBE, pwshColdStartMs=9000))
        for key in ("schema", "receiptId", "card", "legId", "subject", "venue", "actor", "method", "startedUtc", "finishedUtc",
                    "health", "outcome", "outcomeDetail", "evidence", "metrics", "playback", "admission", "owner_verdict", "model_verdicts"):
            self.assertIn(key, receipt)
        self.assertEqual(receipt["schema"], "mlv-app/dual-venue-receipt/v1")
        self.assertIsNone(receipt["owner_verdict"], "the runner never writes an owner verdict")
        self.assertEqual(receipt["model_verdicts"], [])
        for key in ("digest", "buildManifestSha256", "legSpecSha256", "clipId", "clipContentSha256", "backend", "lookFlavor"):
            self.assertIn(key, receipt["subject"])
        for key in ("name", "role", "declared", "detected", "hostName", "gpuNames", "driverVersion", "displayDevice", "instrumentDigests"):
            self.assertIn(key, receipt["venue"])
        for key in ("summaryJsonSha256", "evidenceManifestSha256", "artifactIndexPath", "umRunOutcome"):
            self.assertIn(key, receipt["evidence"])

    def test_subject_digest_is_recomputable_and_separates_backends(self) -> None:
        spec = self.write_spec()
        _, cuda, _ = self.run_leg("ultra-magnus", spec, extra=["-HealthOnly", "-Backend", "cuda"])
        _, cpu, _ = self.run_leg("ultra-magnus", spec, extra=["-HealthOnly", "-Backend", "cpu"])
        self.assertNotEqual(cuda["subject"]["digest"], cpu["subject"]["digest"])
        s = cuda["subject"]
        identity = {"backend": s["backend"], "buildManifestSha256": s["buildManifestSha256"], "clipContentSha256": s["clipContentSha256"],
                    "clipId": s["clipId"], "legSpecSha256": s["legSpecSha256"], "lookFlavor": s["lookFlavor"]}
        expected = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        self.assertEqual(s["digest"], expected, "the digest must be recomputable from the canonical JSON by any reader")
        # the same subject on the OTHER venue has the same digest (P1: paired means same subject digest)
        self.write_artifacts()
        _, um_pass, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cuda"])
        _, bachelor_pass, _ = self.run_leg("bachelor", spec, extra=["-Backend", "cuda"])
        self.assertEqual(um_pass["subject"]["digest"], bachelor_pass["subject"]["digest"])
        self.assertEqual(um_pass["subject"]["clipContentSha256"], CLIP_CONTENT_SHA)

    # -- LOOK legs: flavor passthrough and the advisory model verdicts (judge harness SHELVED) ----------
    def test_a_look_leg_records_the_flavor_the_app_reported(self) -> None:
        # LOOK-ASSIST-FLAVORS-1: honoured = the app's own report equals the flavor the leg asked for; anything else (another
        # flavor, or no report from the app = 'none') is False. A job that never reported at all stays 'unknown'.
        for reported, expected_honored in (("classic", True), ("cinematic", False), ("none", False)):
            with self.subTest(reported=reported):
                self.write_artifacts(sheet=True, summary={"lookFlavorReported": reported, "lookFlavorHonored": expected_honored})
                spec = self.write_spec(leg_type="look")
                _, receipt, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"])
                self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
                self.assertEqual(receipt["look"]["lookFlavor"], "classic")
                self.assertEqual(receipt["look"]["lookFlavorReported"], reported)
                self.assertIs(receipt["look"]["lookFlavorHonored"], expected_honored)

    def test_a_look_leg_passes_the_flavor_and_leaves_the_advisory_fields_untouched(self) -> None:
        self.write_artifacts(sheet=True)
        spec = self.write_spec(leg_type="look")
        proc, receipt, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["look"]["lookFlavor"], "classic")
        self.assertEqual(receipt["look"]["lookFlavorHonored"], "unknown")
        self.assertTrue(receipt["look"]["lookAssistForced"])
        self.assertIsNotNone(receipt["look"]["contactSheet"])
        self.assertEqual(receipt["model_verdicts"], [])
        self.assertIsNone(receipt["owner_verdict"])
        call = self.generator_calls()[-1]
        for expected in ("ForceLookAssist=True", "ContactSheet=True", "LookFlavor=classic", "Backend=cpu"):
            self.assertIn(expected, call)

    def test_a_spec_asking_for_cinematic_never_claims_the_flavor_was_honoured_because_the_spec_asked_for_it(self) -> None:
        """DVE-SCALE2-LOOK-LEG-1: the receipt's lookFlavor is what the SPEC asked for; lookFlavorHonored may only come from the app.
        The harness's artifacts carry no app report (no lookFlavorReported in the job summary), so the receipt of a leg asking for
        cinematic (a synthetic spec here; no cinematic leg spec ships yet) must stay 'unknown' -- never True, never a string that
        echoes the spec. The report-driven values are pinned in test_a_look_leg_records_the_flavor_the_app_reported."""
        self.write_artifacts(sheet=True)
        spec = self.write_spec(leg_type="look", flavor="cinematic")
        proc, receipt, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["look"]["lookFlavor"], "cinematic")
        self.assertEqual(receipt["look"]["lookFlavorHonored"], "unknown")
        self.assertEqual(receipt["subject"]["lookFlavor"], "cinematic")
        self.assertIn("LookFlavor=cinematic", self.generator_calls()[-1])

    def test_the_honoured_field_is_never_assigned_from_the_spec_in_the_runner_or_the_job(self) -> None:
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        job = (ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        pair = (DV / "New-VenueSheetPair.ps1").read_text(encoding="utf-8")
        for name, text in (("Invoke-VenueLeg.ps1", runner), ("playback-attr-3-cuda-job.ps1", job), ("New-VenueSheetPair.ps1", pair)):
            for line in text.splitlines():
                if re.match(r"\s*lookFlavorHonored\s*=", line):
                    # LOOK-ASSIST-FLAVORS-1 landed the app report: a value is acceptable when it is 'unknown', null, a variable the runner
                    # fills only from lookFlavorReported / the receipts (pinned below), or the job's reported-vs-requested comparison.
                    self.assertRegex(line, r"'unknown'|\$null|\$lfReported -ceq|=\s*\$(lookFlavorHonored|pairHonored)\s*$", f"{name}: lookFlavorHonored must come from the app's report (or be 'unknown' / null), never from the spec: {line.strip()}")
        # the runner's one non-literal assignment sits inside the guard that requires the app's report in the job summary
        self.assertRegex(runner, r"if \(\$null -ne \$summary -and \$summary\.PSObject\.Properties\['lookFlavorReported'\]\) \{\s*\$lookFlavorReported = \[string\]\$summary\.lookFlavorReported\s*\$lookFlavorHonored = \(\$lookFlavorReported -ceq \$lookFlavor\)")

    def test_the_app_reads_the_flavor_env_var_and_the_job_records_what_it_reports(self) -> None:
        """LOOK-ASSIST-FLAVORS-1 landed the app-side reader this test used to forbid. The job must therefore take lookFlavorHonored from the
        flavor the APP reports on gui_smoke.visual_state, never from the spec: the reader is one function, both consumers call it, and the
        job's honoured field compares the app's report with the flavor the leg asked for."""
        analysis = (ROOT / "src" / "batch" / "LookAssistAnalysis.cpp").read_text(encoding="utf-8", errors="replace")
        self.assertIn('qEnvironmentVariable( "MLVAPP_LOOK_ASSIST_FLAVOR" )', analysis)
        for consumer in ("src/batch/ReceiptApplier.cpp", "platform/qt/MainWindow.cpp"):
            text = (ROOT / consumer).read_text(encoding="utf-8", errors="replace")
            self.assertIn("lookAssistFlavorEnvironmentValue()", text, consumer)
        job = (ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        self.assertIn("resultJson.log.visualState.look_assist_flavor", job)
        self.assertIn("lookFlavorHonored = $(if ($LookLeg) { $lfReported -ceq $LookFlavor } else { $null })", job)


@requires_windows_pwsh
class FixtureLegsAreRefusedUpFrontTests(RunnerHarness, unittest.TestCase):
    """ROUND 2 item 1. The tracked fixtures (2 and 16 frames) can never satisfy 20 s of distinct source frames, so a leg
    that names one is refused by the same admission gate (master's gui-smoke-length-gate), typed, before the leg is
    generated or submitted -- not played, not looped."""

    def setUp(self) -> None:
        self.make_harness()
        self.write_artifacts()

    def assertRefusedUpFront(self, receipt, submitted, venue: str, backend: str) -> None:
        self.assertIsNotNone(receipt, "a refusal is still a receipt")
        self.assertEqual(submitted, [], f"{venue}/{backend}: a refused fixture leg must not reach the venue (not even the health probe)")
        self.assertEqual(self.generator_calls(), [], "a refused fixture leg is not even generated")
        self.assertTrue(str(receipt["refusal"]).startswith("FIXTURE_REFUSED"), receipt["refusal"])
        self.assertIn("CLIP_TOO_SHORT", receipt["refusal"], "the typed reason is master's own length-gate verdict")
        self.assertNotIn(receipt["outcome"], ("PASS", "FAIL"), "a refusal carries no signal")
        self.assertIsNone(receipt["playback"])

    def test_every_fixture_on_every_venue_and_backend_is_refused_before_anything_is_submitted(self) -> None:
        for clip in FIXTURE_IDS:
            for leg_type in ("speed", "look"):
                spec = self.write_spec(clip=clip, leg_type=leg_type)
                for venue in ("bachelor", "ultra-magnus"):
                    for backend in ("cuda", "cpu"):
                        proc, receipt, submitted = self.run_leg(venue, spec, extra=["-Backend", backend])
                        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                        self.assertRefusedUpFront(receipt, submitted, venue, backend)

    def test_a_fixture_refusal_comes_before_the_consent_check(self) -> None:
        # Even a (fake) consent record that names a fixture id cannot admit it.
        self.write_consent(consent_record("ultra-magnus", FIXTURE_IDS[1]))
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=FIXTURE_IDS[1]))
        self.assertRefusedUpFront(receipt, submitted, "ultra-magnus", "cuda")

    def test_mutation_without_the_fixture_gate_the_fixture_is_no_longer_refused_as_one(self) -> None:
        # Take the fixture refusal out of a COPY of the module: the typed fixture refusal must disappear (the id then
        # falls through to the later clip-id check, which is defence in depth) -- so this test DOES guard the gate.
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($script:FixtureClipIds -ccontains $ClipId) {", "if ($false) {")])
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(clip=FIXTURE_IDS[0]), dv=mutated)
        self.assertFalse(str(receipt["refusal"]).startswith("FIXTURE_REFUSED"), receipt["refusal"])
        self.assertEqual(receipt["refusal"], "CLIP_ID_INVALID", "the second layer still refuses it")

    def test_mutation_without_the_length_gate_the_refusal_loses_its_typed_verdict(self) -> None:
        # The fixture refusal's reason is master's own length-gate verdict. With the gate's verdict taken out the token
        # degrades to the generic NOT_A_CONSENTED_CLIP, so the CLIP_TOO_SHORT assertion above is guarding the gate call.
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($g.verdict -ne 'OK') {", "if ($false) {")])
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(clip=FIXTURE_IDS[1]), dv=mutated)
        self.assertEqual(receipt["refusal"], "FIXTURE_REFUSED_NOT_A_CONSENTED_CLIP")


@requires_windows_pwsh
class PerVenueConsentGateTests(RunnerHarness, unittest.TestCase):
    """ROUND 2 item 3. A leg is addressed by consented clip ID; the runner reads the tracked, owner-written per-venue
    consent file (venue-clip-consent.json, keyed by venue + clip id) and REFUSES before submitting without a record for
    THIS venue. Consent on one venue never implies the other. Agents never write the file."""

    def setUp(self) -> None:
        self.make_harness()
        self.write_artifacts()

    def assertRefusedBeforeSubmitting(self, receipt, submitted, token: str) -> None:
        self.assertEqual(submitted, [], "a refused leg must not reach the venue at all")
        self.assertEqual(self.generator_calls(), [], "a refused leg is not generated")
        self.assertEqual(receipt["refusal"], token)
        self.assertIn(token, receipt["outcomeDetail"])
        self.assertNotIn(receipt["outcome"], ("PASS", "FAIL"))

    def test_consent_on_one_venue_never_implies_the_other(self) -> None:
        self.write_consent(consent_record("bachelor", OWNER_CLIP))
        _, um, um_sub = self.run_leg("ultra-magnus", self.write_spec())
        self.assertRefusedBeforeSubmitting(um, um_sub, "VENUE_CLIP_CONSENT_ABSENT")
        _, bachelor, b_sub = self.run_leg("bachelor", self.write_spec())
        self.assertEqual(bachelor["refusal"], None)
        self.assertEqual(bachelor["outcome"], "PASS", bachelor["outcomeDetail"])
        self.write_consent(consent_record("ultra-magnus", OWNER_CLIP))
        _, bachelor, b_sub = self.run_leg("bachelor", self.write_spec())
        self.assertRefusedBeforeSubmitting(bachelor, b_sub, "VENUE_CLIP_CONSENT_ABSENT")
        _, um, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(um["outcome"], "PASS", um["outcomeDetail"])

    def test_consent_for_a_different_clip_id_does_not_admit_this_one(self) -> None:
        self.write_consent(consent_record("ultra-magnus", OTHER_CLIP), consent_record("bachelor", OTHER_CLIP))
        for venue in ("ultra-magnus", "bachelor"):
            _, receipt, submitted = self.run_leg(venue, self.write_spec(clip=OWNER_CLIP))
            self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_ABSENT")

    def test_an_empty_consent_file_refuses_every_owner_clip_on_every_venue(self) -> None:
        self.write_consent()
        for venue in ("ultra-magnus", "bachelor"):
            for backend in ("cuda", "cpu"):
                _, receipt, submitted = self.run_leg(venue, self.write_spec(), extra=["-Backend", backend])
                self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_ABSENT")

    def test_a_missing_or_malformed_consent_file_fails_closed(self) -> None:
        bodies = {
            "missing": None,
            "not json": "{ not json",
            "wrong schema": json.dumps({"schema": "something/else", "records": [consent_record("ultra-magnus", OWNER_CLIP)]}),
            "no records key": json.dumps({"schema": "mlv-app/dual-venue-clip-consent/v1"}),
            "record without a sha": json.dumps(consent_file({"venue": "ultra-magnus", "clipId": OWNER_CLIP})),
            "sha not hex": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), ownerLineSha256="not-a-sha"))),
            "unknown venue": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), venue="laptop"))),
            "a path-shaped value": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), note="C:/footage/clip.mlv"))),
            "a path in the clip id": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), clipId="C:\\x\\" + OWNER_CLIP))),
            # round 3 (sol BLOCKER): a bare hex string is not the owner's words
            "no owner line": json.dumps(consent_file({k: v for k, v in consent_record("ultra-magnus", OWNER_CLIP).items() if k != "ownerLine"})),
            "a hash that is not the line's": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), ownerLineSha256="a" * 64))),
            "a line for the other venue": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), ownerLine=f"CLIP bachelor: {OWNER_CLIP}"))),
            "a line for another clip": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), ownerLine=f"CLIP ultra-magnus: {OTHER_CLIP}"))),
            "a line with extra words": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), ownerLine=f"please CLIP ultra-magnus: {OWNER_CLIP}"))),
            "recorded by the hub": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), recordedBy="hub"))),
            "recorded by a producer": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), recordedBy="producer"))),
            "recorded at no time": json.dumps(consent_file(dict(consent_record("ultra-magnus", OWNER_CLIP), recordedUtc="whenever"))),
        }
        for name, body in bodies.items():
            path = self.tmp / "bad-consent.json"
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(body, encoding="utf-8")
            _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), consent=path)
            self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_INVALID")

    # DVE-CONSENT-RECORDS-1: the owner typed "CLIP ultramagnus: M16-1243" (no hyphen) for the venue the table names "ultra-magnus".
    # The one alias admitted besides the table name is that same name with every hyphen removed, case-sensitive and exact.
    ALIAS_LINE = f"CLIP ultramagnus: {OWNER_CLIP}"

    def test_the_owners_hyphen_free_ultra_magnus_spelling_is_admitted_for_ultra_magnus_only(self) -> None:
        alias = consent_record_line("ultra-magnus", OWNER_CLIP, self.ALIAS_LINE)
        self.assertEqual(alias["venue"], "ultra-magnus", "the record's venue stays the table name")
        self.write_consent(alias)
        _, um, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(um["refusal"], None)
        self.assertEqual(um["outcome"], "PASS", um["outcomeDetail"])
        self.assertEqual(um["admission"]["ownerLineSha256"], alias["ownerLineSha256"], "the owner's line is recorded verbatim, by its own hash")
        _, bachelor, b_sub = self.run_leg("bachelor", self.write_spec())
        self.assertRefusedBeforeSubmitting(bachelor, b_sub, "VENUE_CLIP_CONSENT_ABSENT")

    def test_the_table_name_spelling_is_still_admitted(self) -> None:
        self.write_consent(consent_record_line("ultra-magnus", OWNER_CLIP, f"CLIP ultra-magnus: {OWNER_CLIP}"))
        _, um, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(um["outcome"], "PASS", um["outcomeDetail"])

    def test_every_other_spelling_of_the_line_is_refused(self) -> None:
        clip = OWNER_CLIP
        cases = [
            ("ultra-magnus", f"CLIP UltraMagnus: {clip}"),
            ("ultra-magnus", f"CLIP Ultramagnus: {clip}"),
            ("ultra-magnus", f"CLIP ULTRAMAGNUS: {clip}"),
            ("ultra-magnus", f"CLIP ultra_magnus: {clip}"),
            ("ultra-magnus", f"CLIP ultra magnus: {clip}"),
            ("ultra-magnus", f"CLIP ultra--magnus: {clip}"),
            ("ultra-magnus", f"CLIP ultramagnu: {clip}"),
            ("ultra-magnus", f"clip ultramagnus: {clip}"),
            ("ultra-magnus", f"CLIP  ultramagnus: {clip}"),
            ("ultra-magnus", f"CLIP ultramagnus:  {clip}"),
            ("ultra-magnus", f"CLIP ultramagnus : {clip}"),
            ("ultra-magnus", f"CLIP ultramagnus:{clip}"),
            ("ultra-magnus", f" CLIP ultramagnus: {clip}"),
            ("ultra-magnus", f"CLIP ultramagnus: {clip} "),
            ("ultra-magnus", f"CLIP ultramagnus: {clip}\n"),
            ("ultra-magnus", f"CLIP ultramagnus: {clip}\\extra"),
            ("ultra-magnus", f"CLIP ultramagnus: {clip}/extra"),
            ("ultra-magnus", f"CLIP ultramagnus: {OTHER_CLIP}"),
            ("ultra-magnus", f"CLIP bachelor: {clip}"),
            ("bachelor", f"CLIP ultramagnus: {clip}"),
            ("bachelor", f"CLIP ultra-magnus: {clip}"),
            ("bachelor", f"CLIP Bachelor: {clip}"),
        ]
        for venue, line in cases:
            self.write_consent(consent_record_line(venue, clip, line))
            _, receipt, submitted = self.run_leg(venue, self.write_spec())
            self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_INVALID")

    def test_mutation_without_the_alias_the_owners_hyphen_free_line_is_refused(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "$spellings = @([string]$r.venue, ([string]$r.venue).Replace('-', ''))", "$spellings = @([string]$r.venue)")])
        self.write_consent(consent_record_line("ultra-magnus", OWNER_CLIP, self.ALIAS_LINE))
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["refusal"], "VENUE_CLIP_CONSENT_INVALID", "without the alias the admit test goes red -- so it guards the alias")

    def test_mutation_a_case_insensitive_line_check_admits_a_wrongly_cased_line(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($line -cnotin @($spellings", "if ($line -notin @($spellings")])
        self.write_consent(consent_record_line("ultra-magnus", OWNER_CLIP, f"CLIP UltraMagnus: {OWNER_CLIP}"))
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertNotEqual(submitted, [], "case-insensitive matching admits a mis-cased line -- so the refuse test guards case-sensitivity")

    def test_the_owner_clip_still_needs_the_cleanup_class_to_be_gone(self) -> None:
        # Consent is necessary, not sufficient: venues.json ownerFootage.cleanupClassGone stays a reviewed switch.
        self.make_harness(cleanup_class_gone=False)
        self.write_artifacts()
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec())
        self.assertRefusedBeforeSubmitting(receipt, submitted, "OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2")

    def test_a_consented_leg_is_addressed_by_clip_id_only(self) -> None:
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        call = self.generator_calls()[-1]
        self.assertIn(f"ClipId={OWNER_CLIP}", call)
        for forbidden in ("ClipPath", "FixtureSha256", "OwnerParts", "ConsentLine"):
            self.assertNotIn(forbidden, call, "the runner hands the generator a clip ID, never a path or a hash of one")
        # Nothing in the receipt or its evidence names a path-shaped clip.
        text = json.dumps(receipt)
        self.assertNotRegex(text, r"(?i)\.mlv\b")
        self.assertEqual(receipt["subject"]["clipId"], OWNER_CLIP)

    def test_a_generator_refusal_is_a_typed_receipt_before_any_submission(self) -> None:
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), gen_refusal="PLAYBACK_ATTR3_OWNER_NOT_CONSENTED the table has no such clip")
        self.assertEqual(submitted, [], "the generator runs before the health probe, so a resolver refusal submits nothing")
        self.assertEqual(receipt["refusal"], "GENERATOR_REFUSED_PLAYBACK_ATTR3_OWNER_NOT_CONSENTED")
        self.assertNotIn(receipt["outcome"], ("PASS", "FAIL"))
        self.assertNotIn("the table has no such clip", json.dumps(receipt), "only the token is recorded, never the message body")

    def test_an_input_too_large_for_a_bounded_leg_is_a_typed_receipt_refusal_not_a_runner_error(self) -> None:
        # CPU-LEG-SMOKE-CEILING-1: the generator's time-budget refusal (ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING) is typed like every other generator refusal.
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), gen_refusal="ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING the smoke ceiling of 3600 s leaves 1 s")
        self.assertEqual(submitted, [])
        self.assertEqual(receipt["refusal"], "GENERATOR_REFUSED_ATTRCUDA_TIMEBUDGET_EXCEEDS_SMOKE_CEILING")
        self.assertNotIn("RUNNER_ERROR", receipt["outcomeDetail"])
        self.assertNotIn("leaves 1 s", json.dumps(receipt), "only the token is recorded, never the message body")

    def test_an_untyped_generator_failure_is_still_a_runner_error(self) -> None:
        _, receipt, _submitted = self.run_leg("ultra-magnus", self.write_spec(), gen_refusal="SOME_OTHER_TOKEN a message")
        self.assertIsNone(receipt.get("refusal"))
        self.assertIn("RUNNER_ERROR", receipt["outcomeDetail"])

    def test_the_tracked_consent_file_is_owner_written_shape_only_and_names_no_path(self) -> None:
        tracked = json.loads((DV / "venue-clip-consent.json").read_text(encoding="utf-8"))
        self.assertEqual(tracked["schema"], "mlv-app/dual-venue-clip-consent/v1")
        self.assertIsInstance(tracked["records"], list)
        text = (DV / "venue-clip-consent.json").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"(?i)[a-z]:[\\/]")
        self.assertNotRegex(text, r"(?i)\.mlv\b")
        for record in tracked["records"]:
            self.assertEqual(set(record), {"venue", "clipId", "ownerLine", "ownerLineSha256", "recordedUtc", "recordedBy"})
            self.assertRegex(record["ownerLineSha256"], r"^[0-9a-f]{64}$")
            self.assertIn(record["ownerLine"], [f"CLIP {spelling}: {record['clipId']}" for spelling in (record["venue"], record["venue"].replace("-", ""))])
            self.assertEqual(hashlib.sha256(record["ownerLine"].encode()).hexdigest(), record["ownerLineSha256"])
            self.assertEqual(record["recordedBy"], "owner")
        # The runner and its module only ever READ the file: no cmdlet or .NET call that writes names it.
        writers = re.compile(r"(Set-Content|Add-Content|Out-File|WriteAllText|WriteAllBytes|Remove-Item|Move-Item|Copy-Item|New-Item)[^\n]*(consent|Consent)", re.I)
        for name in ("Invoke-VenueLeg.ps1", "DualVenueRunner.psm1"):
            for line in (DV / name).read_text(encoding="utf-8").splitlines():
                if line.lstrip().startswith("#"):
                    continue
                self.assertIsNone(writers.search(line), f"{name} must never write the consent file: {line.strip()}")

    def test_mutation_without_the_consent_gate_an_unconsented_leg_is_submitted(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($null -eq $record) {", "if ($false) {")])
        self.write_consent()
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertNotEqual(submitted, [], "with the consent lookup removed the leg reaches the venue -- so this test DOES guard it")

    def test_mutation_a_consent_record_for_the_other_venue_would_admit_if_venue_were_not_matched(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "[string]$r.venue -ceq $Venue -and ", "")])
        self.write_consent(consent_record("bachelor", OWNER_CLIP))
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertNotEqual(submitted, [], "without matching the venue, bachelor's consent would admit ultra-magnus -- so the venue match is guarded")


@requires_windows_pwsh
class ReceiptOracleVerdictTests(RunnerHarness, unittest.TestCase):
    """ROUND 2 item 2. A receipt carries the launcher's source_advanced / required_source_frames / run-nonce verdict and
    the clip id; a PASS/FAIL receipt without them is INVALID. Master's job already refuses short, looped or foreign
    receipts (exit 29, RESULT=SOURCE_FRAMES_INVALID); this runner never believes a capture without the proof in it."""

    def setUp(self) -> None:
        self.make_harness()

    def run_capture(self, venue: str = "ultra-magnus", **artifact_args):
        self.write_artifacts(**artifact_args)
        return self.run_leg(venue, self.write_spec())

    def test_a_proven_capture_carries_the_verdict_and_the_clip_id(self) -> None:
        proc, receipt, _ = self.run_capture()
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        playback = receipt["playback"]
        self.assertEqual(playback["sourceAdvanced"], 960)
        self.assertEqual(playback["requiredSourceFrames"], 600)
        # the nonce the launcher generated, and the one the app echoed, are both recorded and equal
        self.assertEqual(playback["expectedRunNonce"], NONCE)
        self.assertEqual(playback["observedRunNonce"], NONCE)
        self.assertEqual(playback["manifestRunNonce"], NONCE)
        self.assertEqual(playback["nativeFps"], 23.976)
        self.assertEqual(playback["paceFps"], 23.976)
        self.assertEqual(playback["fpsOverride"], 0)
        self.assertEqual(playback["wrapCount"], 0)
        self.assertTrue(playback["logShaBound"])
        self.assertTrue(playback["settingsIsolated"])
        self.assertEqual(playback["clipId"], OWNER_CLIP)
        self.assertFalse(playback["wrapped"])
        self.assertTrue(playback["valid"])
        self.assertEqual(playback["invalidReasons"], [])
        self.assertEqual(receipt["subject"]["clipId"], OWNER_CLIP)

    def test_a_capture_without_the_source_frame_proof_is_invalid_not_pass_or_fail(self) -> None:
        cases = {
            "no sourceFrames block (the job's own oracle did not run)": dict(source_frames=False),
            "sourceFrames null": dict(source_frames=None),
            "no run log published": dict(log=False),
            "no launcher result published": dict(result=False),
            "advanced field absent from the app's summary line": dict(line={"source_advanced": None}, source_frames=False),
            "required field absent": dict(line={"required_source_frames": None}, source_frames=False),
            "advanced short of required": dict(line={"source_advanced": 599}),
            "required under 20 frames": dict(line={"source_advanced": 16, "required_source_frames": 16}),
            "required under ceil(20 s x native fps)": dict(line={"source_advanced": 600, "required_source_frames": 479}),
            "native fps absent": dict(line={"native_fps": None}, source_frames=False),
            "paced off the native fps": dict(line={"pace_fps": "30.000"}),
            "pace absent": dict(line={"pace_fps": None}, source_frames=False),
            "paced by an fps override": dict(line={"fps_override": 1}),
            "fps override absent": dict(line={"fps_override": None}, source_frames=False),
            "wrapped": dict(line={"wrapped": 1}),
            "wrapped field absent": dict(line={"wrapped": None}, source_frames=False),
            "wrap count": dict(line={"wrap_count": 2}),
            "oracle failures present": dict(source_frames={"sourceAdvanced": 960, "requiredSourceFrames": 600, "wrapped": False, "failures": ["INVALID_SOURCE_FRAMES: x"]}),
            "the job's block disagrees with the log": dict(source_frames={"sourceAdvanced": 5000, "requiredSourceFrames": 600, "wrapped": False, "failures": []}),
            "no launcher nonce": dict(nonce=None, observed_nonce=None),
            "no nonce echoed by the app": dict(observed_nonce=None),
            "an earlier run's nonce echoed by the app": dict(observed_nonce="0123456789abcdef0123456789abcdef"),
            "the manifest binds another nonce": dict(manifest_nonce="0123456789abcdef0123456789abcdef"),
            "a nonce not in the launcher's format": dict(nonce="n" + NONCE, observed_nonce="n" + NONCE, manifest_nonce="n" + NONCE),
            "a log that is not the snapshot the launcher hashed": dict(declared_log_sha="f" * 64),
            "settings not isolated": dict(isolated="venue_NOT_ISOLATED"),
            "no isolation line at all": dict(isolated=None),
            "ran a fixture rehearsal": dict(summary={"fixtureRehearsal": True}),
            "fixtureRehearsal field absent": dict(summary={"fixtureRehearsal": None}),
            "another clip's summary": dict(summary={"clipId": OTHER_CLIP}),
        }
        for name, args in cases.items():
            proc, receipt, _ = self.run_capture(**args)
            self.assertEqual(proc.returncode, 0, name + proc.stdout + proc.stderr)
            self.assertEqual(receipt["outcome"], "INVALID", f"{name}: {receipt['outcomeDetail']}")
            self.assertNotIn(receipt["outcome"], ("PASS", "FAIL"))
            if "job's own oracle did not run" in name or name == "sourceFrames null":
                continue  # the RUNNER (not the receipt block) refuses a capture whose job wrote no oracle block of its own
            self.assertFalse(receipt["playback"]["valid"], name)
            self.assertTrue(receipt["playback"]["invalidReasons"], name)

    def test_a_captured_job_without_its_own_oracle_block_is_invalid_even_with_a_sound_log(self) -> None:
        proc, receipt, _ = self.run_capture(source_frames=False)
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertTrue(receipt["playback"]["valid"], "the run log itself is sound: the refusal is the missing job block")
        self.assertIn("sourceFrames", receipt["outcomeDetail"])

    def test_a_failing_criterion_on_an_unproven_run_is_invalid_not_a_fail(self) -> None:
        proc, receipt, _ = self.run_capture(summary={"rows": 0}, line={"source_advanced": 100})
        self.assertEqual(receipt["outcome"], "INVALID", "a FAIL on footage that cannot be shown to be >= 20 s is not a product signal")

    def test_a_failing_criterion_on_a_proven_run_is_still_a_fail(self) -> None:
        proc, receipt, _ = self.run_capture(summary={"rows": 0})
        self.assertEqual(receipt["outcome"], "FAIL")
        self.assertTrue(receipt["playback"]["valid"])

    def test_the_jobs_own_source_frames_refusal_is_invalid_with_the_typed_reason(self) -> None:
        self.write_artifacts(source_frames=False, summary={"result": "SOURCE_FRAMES_INVALID", "smokeRefusalReason": "INVALID_LOOPED"})
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), main_mode="source-frames-invalid")
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("SOURCE_FRAMES_INVALID", receipt["outcomeDetail"])
        self.assertIn("INVALID_LOOPED", receipt["outcomeDetail"])

    def test_a_capture_that_contradicts_its_own_exit_code_is_invalid(self) -> None:
        # The runner ACTS on the job's exit code (master's consumer scan pins this statement): a printed capture with a
        # non-zero exit is not evidence.
        self.write_artifacts()
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), main_mode="captured-nonzero")
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("exited 1", receipt["outcomeDetail"])
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($exitCode -ne 0 -and $resolved.outcome -eq 'CAPTURED') {", "if ($false) {")])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), main_mode="captured-nonzero", dv=mutated)
        # With only the RUNNER's check removed the evidence-bearing writer still refuses: the exit code is a hashed artifact
        # (um-run.json), so the validator re-derives it. (DVE-LEG-TERMINALS-1 r2: Complete-Receipt writes that refusal as a typed INVALID, not exit 2 with no receipt.)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("OUTCOME_NOT_DERIVABLE", proc.stdout)
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("the receipt writer refuses it as a PASS", receipt["outcomeDetail"])
        both = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($exitCode -ne 0 -and $resolved.outcome -eq 'CAPTURED') {", "if ($false) {"),
                                    ("DualVenueRunner.psm1", "if ($exit -ne 0) { $invalid.Add('OUTCOME_NOT_DERIVABLE: the job printed a capture but exited non-zero;", "if ($false) { $invalid.Add('OUTCOME_NOT_DERIVABLE: the job printed a capture but exited non-zero;")])
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), main_mode="captured-nonzero", dv=both)
        self.assertEqual(receipt["outcome"], "PASS", "with both exit-code checks removed the contradictory capture passes -- so each is guarded")

    def test_a_smoke_refusal_of_the_play_window_is_invalid_not_a_product_fail(self) -> None:
        # The job ended in its own length/pace refusal (exit 28-ish, RESULT=SMOKE_RUN_FAILED, smokeRefusalReason=...).
        self.write_artifacts(source_frames=False, summary={"result": "SMOKE_RUN_FAILED", "smokeRefusalReason": "PLAY_WINDOW_TOO_SHORT"})
        stub = self.um.read_text(encoding="utf-8").replace("'source-frames-invalid' {", "'smoke-refused' { return [pscustomobject]@{ exitCode = 30; stdout = 'RESULT=SMOKE_RUN_FAILED ARTIFACTS=' + $cfg.artifactsAgentPath } }\n    'source-frames-invalid' {")
        self.um.write_text(stub, encoding="utf-8")
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), main_mode="smoke-refused")
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("PLAY_WINDOW_TOO_SHORT", receipt["outcomeDetail"])

    def test_the_reader_validator_flags_a_receipt_without_the_verdict(self) -> None:
        # Test-DvReceiptValid is what a reader (Get-VenueEvidence) calls: PASS/FAIL need the proof, other outcomes need none.
        module = DV / "DualVenueRunner.psm1"
        script = (f"Import-Module '{module}' -Force\n"
                  "$p = [pscustomobject]@{ outcome = 'PASS'; subject = [pscustomobject]@{ clipId = 'M16-1243'; clipContentSha256 = '" + CLIP_CONTENT_SHA + "' }; playback = $null; evidence = [pscustomobject]@{ umRunOutcome = 'RECEIPT' } }\n"
                  "(Test-DvReceiptValid -Receipt $p).valid\n"
                  "$p.outcome = 'UNRESOLVED'; (Test-DvReceiptValid -Receipt $p).valid\n")
        proc = run_pwsh(["-Command", script])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([l.strip() for l in proc.stdout.splitlines() if l.strip()], ["False", "True"])

    # -- mutations: each rule's test must be able to fail --------------------------------------------
    def test_mutation_without_the_playback_check_a_short_run_passes(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($advanced -lt $required) {", "if ($false) {")])
        self.write_artifacts(line={"source_advanced": 100})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS", "the mutated runner believes a short run -- so the short-run test DOES guard the rule")

    def test_mutation_without_the_wrap_check_a_looped_run_passes(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "elseif ($wrapped) {", "elseif ($false) {")])
        self.write_artifacts(line={"wrapped": 1}, source_frames={"sourceAdvanced": 960, "requiredSourceFrames": 600, "wrapped": True, "failures": []})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS")

    def test_mutation_without_the_nonce_check_a_foreign_receipt_passes(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "elseif ($observedNonce -cne $expectedNonce) {", "elseif ($false) {")])
        self.write_artifacts(observed_nonce="0123456789abcdef0123456789abcdef")
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS")

    def test_mutation_without_the_rehearsal_check_a_fixture_run_passes(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($rehearsal -ne $false) {", "if ($false) {")])
        self.write_artifacts(summary={"fixtureRehearsal": True})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS")

    def test_mutation_without_the_writer_validation_a_proofless_pass_is_written(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($Receipt['outcome'] -in @('PASS', 'FAIL')) {", "if ($false) {"),
                                       ("Invoke-VenueLeg.ps1", "if ($outcome -in @('PASS', 'FAIL') -and $proofProblems.Count -gt 0) {", "if ($false) {")])
        self.write_artifacts(line={"source_advanced": 100})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS", "with both layers removed a proofless capture is a PASS -- each layer is needed")

    def test_mutation_the_runner_layer_alone_still_stops_a_proofless_pass(self) -> None:
        # Defence in depth: removing only the WRITER's check, the runner's own downgrade still yields INVALID ...
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($Receipt['outcome'] -in @('PASS', 'FAIL')) {", "if ($false) {")])
        self.write_artifacts(line={"source_advanced": 100})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "INVALID")
        # ... and removing only the RUNNER's downgrade, the writer refuses the PASS and Complete-Receipt writes that refusal as a typed INVALID that names the reason
        # (DVE-LEG-TERMINALS-1 r2: it used to be exit 2 with no receipt file for the leg).
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($outcome -in @('PASS', 'FAIL') -and $proofProblems.Count -gt 0) {", "if ($false) {")])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("DVE_RECEIPT_WRITE_FAILED", proc.stdout)
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("the receipt writer refuses it as a PASS", receipt["outcomeDetail"])
        self.assertIn("INVALID_SOURCE_FRAMES", receipt["outcomeDetail"])


@requires_windows_pwsh
class PlayWindowTests(RunnerHarness, unittest.TestCase):
    """The leg's play window comes from the tracked spec and is floored at 20 s before anything is submitted."""

    def setUp(self) -> None:
        self.make_harness()
        self.write_artifacts()

    def test_the_play_window_reaches_the_generator(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(play_seconds=33))
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertIn("PlaySeconds=33", self.generator_calls()[-1])

    def test_a_spec_without_a_window_gets_the_generators_default_of_25(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(play_seconds=None))
        self.assertIn("PlaySeconds=25", self.generator_calls()[-1])

    def test_a_window_under_the_lowest_floor_is_refused_before_submission(self) -> None:
        # PLAYBACK-CLIP-LENGTH-HFR-10S-1: a leg names a clip id, never its fps, so it refuses what NO clip could admit (under the
        # HFR clip's 10 s); a window under a non-HFR clip's 20 s is refused at the venue by the runner and the app (the clip's header).
        for window in (1, 9):
            _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(play_seconds=window))
            self.assertEqual(submitted, [])
            self.assertEqual(self.generator_calls(), [])
            self.assertEqual(receipt["refusal"], "PLAY_WINDOW_TOO_SHORT")

    def test_a_ten_second_window_reaches_the_generator_for_an_hfr_clip(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(play_seconds=10))
        self.assertNotEqual(receipt.get("refusal"), "PLAY_WINDOW_TOO_SHORT", receipt)
        self.assertIn("PlaySeconds=10", self.generator_calls()[-1])

    def test_mutation_without_the_window_floor_a_one_second_play_reaches_the_venue(self) -> None:
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($playSeconds -lt (Get-DvMinPlaySeconds -LowestAnyClip)) {", "if ($false) {")])
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(play_seconds=1), dv=mutated)
        self.assertNotEqual(submitted, [])


# ---------------------------------------------------------------------------------------------------
# ROUND 3 (formal keys r1: fable blocker "the nonce rule rejects every real run", sol blocker "consent is forgeable", sol H1,
# fable hardening). CLASS: a receipt is judged on proof that came from THIS real run on THIS venue, and admission rests on
# consent the OWNER gave -- nothing an agent or a caller can write substitutes for either.
def _ps_json(script_body: str, module: Path, env: dict) -> subprocess.CompletedProcess:
    return run_pwsh(["-Command", f"Import-Module '{module}' -Force\n" + script_body], env_extra=env)


def ps_problems(block: dict, clip: str = OWNER_CLIP, module: Path | None = None) -> list[str]:
    """Get-DvPlaybackProblems over a receipt `playback` block (the receipt's own fields, nothing else)."""
    proc = _ps_json(f"$b = $env:DVE_BLOCK | ConvertFrom-Json\n@(Get-DvPlaybackProblems -Playback $b -ExpectedClipId '{clip}') | ForEach-Object {{ Write-Output $_ }}\n",
                    module or DV / "DualVenueRunner.psm1", {"DVE_BLOCK": json.dumps(block)})
    if proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    return [l for l in proc.stdout.splitlines() if l.strip()]


def ps_receipt_status(receipt: dict, allow_offline: bool = False, module: Path | None = None, repo_root: Path | None = None,
                      evidence_dir: Path | None = None) -> tuple[str, bool, list[str]]:
    """Test-DvReceiptValid exactly as a reader calls it: (status, valid, reasons). status is VERIFIED | INCOMPLETE | INVALID | NO_SIGNAL."""
    flags = (" -AllowOfflineTestMode" if allow_offline else "") + (f" -RepoRoot '{repo_root}'" if repo_root else "") + (f" -EvidenceDir '{evidence_dir}'" if evidence_dir else "")
    proc = _ps_json(f"$r = $env:DVE_RECEIPT | ConvertFrom-Json\n$v = Test-DvReceiptValid -Receipt $r{flags}\nWrite-Output ('STATUS=' + $v.status)\nWrite-Output ('VALID=' + $v.valid)\n"
                    "$v.reasons | ForEach-Object { Write-Output ('REASON=' + $_) }\n",
                    module or DV / "DualVenueRunner.psm1", {"DVE_RECEIPT": json.dumps(receipt)})
    if proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    return lines[0][len("STATUS="):], lines[1] == "VALID=True", [l[len("REASON="):] for l in lines[2:]]


def ps_receipt_valid(receipt: dict, allow_offline: bool = False, module: Path | None = None, repo_root: Path | None = None,
                     evidence_dir: Path | None = None) -> tuple[bool, list[str]]:
    status, valid, reasons = ps_receipt_status(receipt, allow_offline, module, repo_root, evidence_dir)
    return valid, reasons


class ModuleMutationMixin:
    def mutated_module(self, mutations: list[tuple[str, str]]) -> Path:
        """A COPY of DualVenueRunner.psm1 with each (old, new) edit applied; the anchor must occur exactly once."""
        text = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        for old, new in mutations:
            self.assertEqual(text.count(old), 1, f"mutation anchor must occur exactly once: {old!r}")
            text = text.replace(old, new)
        tmp = tempfile.TemporaryDirectory(prefix="dve-mut-")
        self.addCleanup(tmp.cleanup)
        # laid out like the tree (the module reads the app's display placement with ..\gui-smoke-display-identity.ps1, DUAL-VENUE-DISPLAY-MATRIX-1)
        path = Path(tmp.name) / "tools" / "profiling" / "dual-venue" / "DualVenueRunner.psm1"
        path.parent.mkdir(parents=True)
        path.write_text(text, encoding="utf-8")
        shutil.copyfile(ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1", path.parent.parent / "gui-smoke-display-identity.ps1")
        return path


@requires_windows_pwsh
class RunNonceIsTheRealLaunchersTests(ModuleMutationMixin, unittest.TestCase):
    """fable BLOCKER: the receipt's nonce rule demanded ^n[0-9a-f]{32}$ but the launcher the job runs mints
    [Guid]::NewGuid().ToString("N") (32 lowercase hex, no prefix), so every real run was INVALID. The rule now accepts exactly
    what the launcher mints, and these tests drive the launcher's OWN expression, never a hand-made constant."""

    def test_the_nonce_the_real_launcher_mints_is_accepted_and_bound_to_the_expected_run(self) -> None:
        for _ in range(3):
            nonce = mint_run_nonce()
            self.assertRegex(nonce, r"^[0-9a-f]{32}$")
            self.assertEqual(ps_problems(good_block(expectedRunNonce=nonce, observedRunNonce=nonce, manifestRunNonce=nonce)), [])

    def test_the_old_hand_made_n_prefixed_nonce_is_not_what_the_launcher_mints_and_is_not_accepted(self) -> None:
        old = "n" + "a1" * 16
        problems = ps_problems(good_block(expectedRunNonce=old, observedRunNonce=old, manifestRunNonce=old))
        self.assertTrue(any("RECEIPT_NOT_THIS_RUN" in p for p in problems), problems)

    def test_the_nonce_the_app_echoed_must_be_the_one_the_launcher_generated(self) -> None:
        other = mint_run_nonce()
        self.assertNotEqual(other, NONCE)
        problems = ps_problems(good_block(observedRunNonce=other))
        self.assertTrue(any("not the nonce the launcher generated" in p for p in problems), problems)
        problems = ps_problems(good_block(manifestRunNonce=other))
        self.assertTrue(any("manifest binds a different run nonce" in p for p in problems), problems)
        for absent in ({"observedRunNonce": None}, {"expectedRunNonce": None}):
            self.assertTrue(any("RECEIPT_NOT_THIS_RUN" in p for p in ps_problems(good_block(**absent))), absent)
        self.assertTrue(any("RECEIPT_NOT_THIS_RUN" in p for p in ps_problems(good_block(expectedRunNonce=NONCE.upper(), observedRunNonce=NONCE.upper()))),
                        "the launcher mints lowercase hex; anything else is not its nonce")

    def test_mutation_a_changed_producer_format_is_refused_by_the_rule_so_the_real_producer_test_would_go_red(self) -> None:
        text = LAUNCHER.read_text(encoding="utf-8")
        expression = launcher_nonce_expression(text)
        self.assertIn('.ToString("N")', expression)
        for name, mutated_expression in (("a hyphenated GUID", expression.replace('.ToString("N")', '.ToString("D")')),
                                         ("an n-prefixed GUID", '"n" + ' + expression)):
            mutated = text.replace("$runNonce = " + expression, "$runNonce = " + mutated_expression)
            self.assertEqual(mutated.count("$runNonce = " + mutated_expression), 1)
            nonce = mint_run_nonce(mutated)
            self.assertNotRegex(nonce, r"^[0-9a-f]{32}$", name)
            problems = ps_problems(good_block(expectedRunNonce=nonce, observedRunNonce=nonce, manifestRunNonce=nonce))
            self.assertTrue(any("RECEIPT_NOT_THIS_RUN" in p for p in problems), f"{name}: {problems}")

    def test_mutation_without_the_expected_format_check_any_string_is_this_run(self) -> None:
        mutated = self.mutated_module([("if ($expectedNonce -cnotmatch $script:RunNoncePattern) {", "if ($false) {"),
                                       ("elseif ($observedNonce -cnotmatch $script:RunNoncePattern) {", "elseif ($false) {")])
        junk = "none"
        self.assertEqual([p for p in ps_problems(good_block(expectedRunNonce=junk, observedRunNonce=junk, manifestRunNonce=junk), module=mutated) if "THIS_RUN" in p], [],
                         "with the format check removed a junk nonce binds -- so the format check is guarded")
        self.assertNotEqual([p for p in ps_problems(good_block(expectedRunNonce=junk, observedRunNonce=junk, manifestRunNonce=junk)) if "THIS_RUN" in p], [])


@requires_windows_pwsh
class ReceiptRederivesTheTwentySecondFloorTests(ModuleMutationMixin, unittest.TestCase):
    """sol H1: the receipt carries native_fps, pace_fps, fps_override and the expected / observed nonce, so a reader
    re-derives the 20 s floor (required >= ceil(20 x native_fps)) and the nonce binding from the receipt alone."""

    # (sol's 20-frame repro, the valid=true repro and the floor mutation run through the evidence-bearing path:
    #  EvidenceBoundReceiptTests.test_sols_20_frame_repro_..., test_a_stored_valid_true_..., test_mutation_without_the_floor_...)
    def test_the_floor_is_ceil_20_seconds_times_the_native_fps(self) -> None:
        # PLAYBACK-CLIP-LENGTH-HFR-10S-1 (owner amendment 2026-10-15): footage of >= 50 fps native needs 10 s, ceil(10 x native fps).
        for native, floor, seconds in ((24.0, 480, 20), (23.976, 480, 20), (25.0, 500, 20), (30.0, 600, 20), (49.0, 980, 20),
                                       (50.0, 500, 10), (59.94, 600, 10), (60.0, 600, 10)):
            ok = ps_problems(good_block(nativeFps=native, paceFps=native, requiredSourceFrames=floor, sourceAdvanced=floor + 10, jobRequiredSourceFrames=floor, jobSourceAdvanced=floor + 10))
            self.assertEqual(ok, [], f"native {native}: {floor} frames is exactly the floor")
            short = ps_problems(good_block(nativeFps=native, paceFps=native, requiredSourceFrames=floor - 1, sourceAdvanced=floor + 10, jobRequiredSourceFrames=floor - 1, jobSourceAdvanced=floor + 10))
            self.assertTrue(any(f"ceil({seconds} s x native_fps" in p for p in short), f"native {native}: {floor - 1} frames is under {seconds} s: {short}")

    def test_the_hfr_floor_mirrors_the_apps_one_derivation(self) -> None:
        # PLAYBACK-CLIP-LENGTH-HFR-10S-1: this module cannot dot-source the gate, so it mirrors minPlayWindowSeconds; the constants
        # agree with platform/qt/PlaybackFrameRange.h and the function agrees with the gate's on one table, unknown = 20 s.
        header = (ROOT / "platform" / "qt" / "PlaybackFrameRange.h").read_text(encoding="utf-8")
        module = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        for cpp, ps in (("kStandardMinPlayWindowSeconds", "StandardMinPlaySeconds"), ("kHfrMinPlayWindowSeconds", "HfrMinPlaySeconds"),
                        ("kHfrNativeFpsThreshold", "HfrNativeFpsThreshold")):
            value = re.search(rf"constexpr double {cpp} = ([0-9.]+);", header).group(1)
            self.assertIn(f"$script:{ps} = {value}", module)
        table = "@(59.94, 60, 50, 49.999, 29.97, 23.976, 0, -60, 'NaN', 'Infinity', 'abc', $null)"
        mod = _ps_json(f"{table} | ForEach-Object {{ Get-DvMinPlaySeconds -NativeFps $_ }}; Get-DvMinPlaySeconds -LowestAnyClip\n",
                       DV / "DualVenueRunner.psm1", {})
        self.assertEqual(mod.returncode, 0, mod.stdout + mod.stderr)
        self.assertEqual(mod.stdout.split(), ["10", "10", "10", "20", "20", "20", "20", "20", "20", "20", "20", "20", "10"])

    def test_pace_override_wrap_and_absent_fields_are_judged_from_the_receipts_own_fields(self) -> None:
        cases = {
            "paced off the native fps": (dict(paceFps=30.0), "paced at pace_fps=30"),
            "pace not positive": (dict(paceFps=0.0), "pace_fps=0"),
            "fps override": (dict(fpsOverride=1), "fps override"),
            "wrap count": (dict(wrapCount=1), "wrap_count=1"),
            "wrapped": (dict(wrapped=True), "INVALID_LOOPED"),
            "native fps unknown": (dict(nativeFps=0.0), "native_fps=0"),
            "native fps absent": (dict(nativeFps=None), "RECEIPT_FIELD_ABSENT: native_fps"),
            "pace absent": (dict(paceFps=None), "RECEIPT_FIELD_ABSENT: pace_fps"),
            "override absent": (dict(fpsOverride=None), "RECEIPT_FIELD_ABSENT: fps_override"),
            "wrap count absent": (dict(wrapCount=None), "RECEIPT_FIELD_ABSENT: wrap_count"),
            "log not bound": (dict(logShaBound=False), "logShaBound"),
            "settings not isolated": (dict(settingsIsolated=False), "SETTINGS_NOT_ISOLATED"),
            "job disagrees with the log": (dict(jobSourceAdvanced=5), "JOB_DISAGREES_WITH_LOG"),
        }
        for name, (over, needle) in cases.items():
            problems = ps_problems(good_block(**over))
            self.assertTrue(any(needle in p for p in problems), f"{name}: {problems}")
        self.assertEqual(ps_problems(good_block()), [])

    def test_mutation_without_the_pace_check_a_fast_paced_run_is_valid(self) -> None:
        # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1: the native-pace arm now sits under the conform proof (the else of "a proven conform judges the pace").
        anchor = "} elseif ([Math]::Abs($pace - $native) -gt (0.005 * $native)) {"
        mutated = self.mutated_module([(anchor, "} elseif ($false) {")])
        self.assertEqual([p for p in ps_problems(good_block(paceFps=30.0), module=mutated) if "paced at" in p], [])
        self.assertNotEqual([p for p in ps_problems(good_block(paceFps=30.0)) if "paced at" in p], [])

    def test_mutation_without_the_override_check_an_overridden_run_is_valid(self) -> None:
        mutated = self.mutated_module([("elseif ($override -ne 0) {", "elseif ($false) {")])
        self.assertEqual([p for p in ps_problems(good_block(fpsOverride=1), module=mutated) if "override" in p], [])


@requires_windows_pwsh
class OfflineTestReceiptsAreNotEvidenceTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        self.make_harness()

    def test_a_receipt_written_with_a_caller_supplied_consent_and_stub_um_run_is_not_valid_evidence(self) -> None:
        self.write_artifacts()
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["admission"]["mode"], "offline-test")
        self.assertIsNone(receipt["admission"]["consentBlobSha"])
        valid, reasons = ps_receipt_valid(receipt)
        self.assertFalse(valid)
        self.assertTrue(any("OFFLINE_TEST_RECEIPT" in r for r in reasons), reasons)
        self.assertTrue(ps_receipt_valid(receipt, allow_offline=True)[0], "the test harness alone may read its own receipts")

    def test_offline_test_mode_can_never_use_the_real_um_run_or_a_real_share(self) -> None:
        spec = self.write_spec()
        base = ["-File", str(DV / "Invoke-VenueLeg.ps1"), "-Venue", "ultra-magnus", "-LegSpec", str(spec), "-SourceCommit", self.sha,
                "-BuildManifestSha256", self.build_sha, "-OfflineTestMode", "-VenueTablePath", str(self.table), "-ConsentPath", str(self.consent),
                "-ReceiptRoot", str(self.receipts), "-WorkDir", str(self.tmp / "work")]
        proc = run_pwsh(base)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DVE_OFFLINE_TEST_REQUIRES_STUB_UMRUN", proc.stdout + proc.stderr)
        copy = self.tmp / "um-run-copy.ps1"
        shutil.copyfile(ROOT / "tools" / "profiling" / "um-run.ps1", copy)
        for real in (ROOT / "tools" / "profiling" / "um-run.ps1", copy):
            proc = run_pwsh(base + ["-UmRunScript", str(real)])
            self.assertNotEqual(proc.returncode, 0, str(real))
            self.assertIn("DVE_OFFLINE_TEST_REFUSES_REAL_UMRUN", proc.stdout + proc.stderr)
        table = json.loads(self.table.read_text(encoding="utf-8"))
        table["venues"]["ultra-magnus"]["agentShare"] = "\\\\dve-no-such-host.invalid\\mlv-agent"
        unc = self.tmp / "venues-unc.json"
        unc.write_text(json.dumps(table), encoding="utf-8")
        args = [a if a != str(self.table) else str(unc) for a in base]
        proc = run_pwsh(args + ["-UmRunScript", str(self.um)])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DVE_OFFLINE_TEST_SHARE_NOT_LOCAL", proc.stdout + proc.stderr)
        self.assertFalse(self.receipts.exists(), "a refused offline invocation writes nothing and submits nothing")

    def test_mutation_without_the_local_share_check_an_offline_run_can_name_a_real_share(self) -> None:
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($share.StartsWith('\\\\') -or ", "if ($false -and ")])
        table = json.loads(self.table.read_text(encoding="utf-8"))
        table["venues"]["ultra-magnus"]["agentShare"] = "\\\\dve-no-such-host.invalid\\mlv-agent"
        self.table.write_text(json.dumps(table), encoding="utf-8")
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertIsNotNone(receipt, "with the check removed the offline run proceeds toward a share that is not local -- so the check is guarded")


class ProductionRepo:
    """A throwaway git repo holding a COPY of the dual-venue tree with the venue table and consent COMMITTED as given, so the
    runner is exercised exactly as production runs it (no test seam), against the committed-revision rule."""

    def __init__(self, test: unittest.TestCase, records: list[dict], cleanup_gone: bool = False, mutations: list[tuple[str, str, str]] | None = None,
                 blankets: list[dict] | None = None, shipped_legs: bool = False) -> None:
        self.test = test
        tmp = tempfile.TemporaryDirectory(prefix="dve-prod-")
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.dv = self.root / "tools" / "profiling" / "dual-venue"
        # The validator reads EVERY committed leg spec (two git spawns each) to find the one a receipt names by sha, so the ~70 shipped legs cost ~15 s
        # per validation (the bulk of this file's runtime). A throwaway repo therefore carries only the legs a test commits itself (commit_leg);
        # `shipped_legs=True` keeps the whole shipped legs/ directory for the tests that exercise the lookup against the real tree.
        shutil.copytree(DV, self.dv, ignore=None if shipped_legs else lambda directory, names: [n for n in names if Path(directory) == DV / "legs" and n.endswith(".json")])
        for name, old, new in mutations or []:
            path = self.dv / name
            text = path.read_text(encoding="utf-8")
            test.assertEqual(text.count(old), 1, f"mutation anchor must occur exactly once in {name}: {old!r}")
            path.write_text(text.replace(old, new), encoding="utf-8")
        table = json.loads((self.dv / "venues.json").read_text(encoding="utf-8"))
        table["ownerFootage"]["cleanupClassGone"] = cleanup_gone
        (self.dv / "venues.json").write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8", newline="\n")
        self.write_consent(*records, blankets=blankets)
        shutil.copyfile(COMPOSER, self.root / "tools" / "profiling" / "make-contact-sheet.py")   # the pair script's composer (a sibling of dual-venue/)
        shutil.copyfile(ROOT / "tools" / "profiling" / "gui-smoke-display-identity.ps1", self.root / "tools" / "profiling" / "gui-smoke-display-identity.ps1")   # the module's display-placement parser
        self.git("init", "-q")
        self.git("config", "user.email", "unit@example.invalid")
        self.git("config", "user.name", "unit")
        self.git("config", "core.autocrlf", "false")
        self.git("add", "tools")
        self.git("commit", "-q", "-m", "committed dual-venue tree")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True).stdout.strip()

    def commit_leg(self, spec: Path, name: str | None = None) -> None:
        """Commit a leg spec under tools/profiling/dual-venue/legs/ (production finds the spec the receipt names ONLY there)."""
        target = self.dv / "legs" / (name or spec.name)
        shutil.copyfile(spec, target)
        self.git("add", "tools")
        self.git("commit", "-q", "-m", f"commit leg spec {target.name}")

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")

    def write_consent(self, *records: dict, blankets: list[dict] | None = None) -> None:
        (self.dv / "venue-clip-consent.json").write_text(json.dumps(consent_file(*records, blankets=blankets), indent=2) + "\n", encoding="utf-8", newline="\n")

    def blob(self, name: str) -> str:
        return self.git("rev-parse", f"HEAD:tools/profiling/dual-venue/{name}")

    def receipts(self) -> list[dict]:
        root = self.root / ".claude-state" / "dual-venue" / "receipts"
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.rglob("*.json"), key=lambda f: f.stat().st_mtime_ns)] if root.exists() else []

    def run(self, spec: Path, sha: str, build_sha: str, venue: str = "ultra-magnus", extra: list[str] | None = None) -> subprocess.CompletedProcess:
        return run_pwsh(["-File", str(self.dv / "Invoke-VenueLeg.ps1"), "-Venue", venue, "-LegSpec", str(spec), "-SourceCommit", sha,
                         "-BuildManifestSha256", build_sha, *(extra or [])])


@requires_windows_pwsh
class BlanketConsentTests(RunnerHarness, unittest.TestCase):
    """VENUE-CLIP-CONSENT-BLANKET-1. OWNER RULING (ledger stamp 2026-10-11T03:41:42Z): the owner's blanket sentence admits any clip id on any
    venue. ONE blanket record kind (consent file top-level `blanketRecords`); per-clip records stay valid and are checked first; the runner pins
    the sentence's sha256, so a changed sentence, a non-owner writer, a path, or an uncommitted edit admits NOTHING."""

    def setUp(self) -> None:
        self.make_harness()
        self.write_artifacts()

    def assertRefusedBeforeSubmitting(self, receipt, submitted, token: str) -> None:
        self.assertEqual(submitted, [], "a refused leg must not reach the venue at all")
        self.assertEqual(self.generator_calls(), [], "a refused leg is not generated")
        self.assertEqual(receipt["refusal"], token)
        self.assertNotIn(receipt["outcome"], ("PASS", "FAIL"))

    def test_the_verbatim_sentence_hash_is_pinned_as_a_literal_and_the_tracked_record_carries_it(self) -> None:
        # (a) the literal, the sentence bytes (no final period, ASCII) and the tracked record all agree
        self.assertEqual(hashlib.sha256(BLANKET_SENTENCE.encode("utf-8")).hexdigest(), BLANKET_SENTENCE_SHA256)
        self.assertEqual(BLANKET_SENTENCE_SHA256, "48dc8dead951aa2eab911db63591b59d238830d2d98c0fd7df346c42b78f9fd6")
        self.assertFalse(BLANKET_SENTENCE.endswith("."))
        self.assertTrue(BLANKET_SENTENCE.isascii())
        tracked = json.loads((DV / "venue-clip-consent.json").read_text(encoding="utf-8"))
        self.assertEqual(tracked["blanketRecords"], [blanket_record()])
        self.assertEqual(hashlib.sha256(tracked["blanketRecords"][0]["ownerSentence"].encode("utf-8")).hexdigest(), BLANKET_SENTENCE_SHA256)
        # the runner pins the same literal, and the real tracked file parses with the real venue table
        proc = _ps_json("$t = ConvertFrom-DvVenueTableText ([IO.File]::ReadAllText($env:DVE_TABLE))\n$c = Read-DvClipConsent -Text ([IO.File]::ReadAllText($env:DVE_CONSENT)) -Table $t\n"
                        "Write-Output ('OK=' + $c.ok + ' ' + $c.reason + ' blankets=' + @($c.blankets).Count + ' records=' + @($c.records).Count)\n"
                        "Write-Output ('PIN=' + (& (Get-Module DualVenueRunner) { $script:BlanketOwnerSentenceSha256 }))\n",
                        DV / "DualVenueRunner.psm1", {"DVE_TABLE": str(DV / "venues.json"), "DVE_CONSENT": str(DV / "venue-clip-consent.json")})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"OK=True  blankets=1 records={len(tracked['records'])}", proc.stdout)
        self.assertIn(f"PIN={BLANKET_SENTENCE_SHA256}", proc.stdout)

    def test_a_blanket_admits_an_unlisted_clip_on_both_venues_and_the_receipt_says_which_rule_admitted_it(self) -> None:
        # (b)
        self.write_consent(blankets=[blanket_record()])                   # NO per-clip record at all
        for venue in ("ultra-magnus", "bachelor"):
            for clip in (OWNER_CLIP, OTHER_CLIP):                         # (the stub job's own clip id is OWNER_CLIP, so only that one reaches PASS)
                _, receipt, submitted = self.run_leg(venue, self.write_spec(clip=clip))
                self.assertIsNone(receipt["refusal"], (venue, clip))
                if clip == OWNER_CLIP:
                    self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
                self.assertNotEqual(submitted, [], "an admitted leg reaches the venue")
                self.assertEqual(receipt["admission"]["consentKind"], "blanket", (venue, clip))
                self.assertEqual(receipt["admission"]["consentSha256Prefix"], BLANKET_SENTENCE_SHA256[:12])
                self.assertEqual(receipt["admission"]["ownerLineSha256"], BLANKET_SENTENCE_SHA256)

    def test_a_per_clip_record_is_still_checked_first_and_the_receipt_says_clip(self) -> None:
        self.write_consent(consent_record("ultra-magnus", OWNER_CLIP), blankets=[blanket_record()])
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["admission"]["consentKind"], "clip")
        self.assertEqual(receipt["admission"]["ownerLineSha256"], hashlib.sha256(f"CLIP ultra-magnus: {OWNER_CLIP}".encode()).hexdigest())

    def test_without_a_blanket_the_unlisted_clip_is_still_refused(self) -> None:
        self.write_consent(consent_record("ultra-magnus", OWNER_CLIP), blankets=[])
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=OTHER_CLIP))
        self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_ABSENT")

    def test_a_blanket_never_admits_a_fixture_id_or_a_venue_with_the_cleanup_switch_off(self) -> None:
        self.write_consent(blankets=[blanket_record()])
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=FIXTURE_IDS[1]))
        self.assertEqual(submitted, [])
        self.assertTrue(str(receipt["refusal"]).startswith("FIXTURE_REFUSED_"), receipt["refusal"])
        self.make_harness(cleanup_class_gone=False)
        self.write_artifacts()
        self.write_consent(blankets=[blanket_record()])
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=OTHER_CLIP))
        self.assertRefusedBeforeSubmitting(receipt, submitted, "OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2")

    def test_a_one_character_change_to_the_sentence_refuses_and_admits_nothing(self) -> None:
        # (c) -- with the stored hash left alone, and with the hash recomputed to match
        changed = {
            "a letter": BLANKET_SENTENCE.replace("any MLV clip", "any MLV clop"),
            "a trailing period": BLANKET_SENTENCE + ".",
            "a dropped word": BLANKET_SENTENCE.replace("blanket ", ""),
        }
        for name, sentence in changed.items():
            for label, record in (("stale sha", blanket_record(ownerSentence=sentence)),
                                  ("re-hashed", blanket_record(ownerSentence=sentence, ownerSentenceSha256=hashlib.sha256(sentence.encode("utf-8")).hexdigest()))):
                # even WITH a valid per-clip record for the clip, the file is invalid: it fails loudly, no fallback to admit
                self.write_consent(consent_record("ultra-magnus", OWNER_CLIP), blankets=[record])
                _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec())
                self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_INVALID")
                self.write_consent(blankets=[record])
                _, receipt, submitted = self.run_leg("bachelor", self.write_spec(clip=OTHER_CLIP))
                self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_INVALID")

    def test_a_blanket_written_by_anyone_other_than_the_owner_is_refused(self) -> None:
        # (d)
        for name, record in {"writtenBy hub": blanket_record(writtenBy="hub"), "writtenBy producer": blanket_record(writtenBy="producer"),
                             "recordedBy hub": blanket_record(recordedBy="hub"), "writtenBy Owner": blanket_record(writtenBy="Owner")}.items():
            self.write_consent(blankets=[record])
            _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=OTHER_CLIP))
            self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_INVALID")

    def test_a_blanket_record_that_is_not_exactly_the_reviewed_shape_or_carries_a_path_is_refused(self) -> None:
        # (e) -- drive-letter paths; the file-name branch of the path rule is pinned by the existing malformed-file test
        path_sentence = BLANKET_SENTENCE + " C:/ledger/notes.txt"
        cases = {
            "an extra field with a path": dict(blanket_record(), note="C:/ledger/notes.txt"),
            "a path in the sentence even re-hashed": blanket_record(ownerSentence=path_sentence, ownerSentenceSha256=hashlib.sha256(path_sentence.encode("utf-8")).hexdigest()),
            "a path in the source": blanket_record(source="C:\\ledger"),
            "a missing field": {k: v for k, v in blanket_record().items() if k != "writtenBy"},
            "another scope": blanket_record(scope="this clip only"),
            "another kind": blanket_record(kind="clip"),
            "another source": blanket_record(source="ledger 2026-10-11T03:41:43Z"),
            "no timestamp": blanket_record(recordedUtc="whenever"),
        }
        for name, record in cases.items():
            self.write_consent(blankets=[record])
            _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=OTHER_CLIP))
            self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_INVALID")

    def test_an_uncommitted_blanket_admits_nothing(self) -> None:
        # (f) production reads the consent file ONLY as committed at HEAD
        spec = self.write_spec(clip=OTHER_CLIP)
        repo = ProductionRepo(self, records=[], cleanup_gone=True)
        repo.write_consent(blankets=[blanket_record()])                       # a perfectly valid blanket -- but not committed
        repo.run(spec, self.sha, "ab" * 32)
        [receipt] = repo.receipts()
        self.assertEqual(receipt["refusal"], "ADMISSION_SOURCE_DIRTY")
        self.assertIsNone(receipt["evidence"]["umRunOutcome"], "nothing was submitted")
        self.assertIsNone(receipt["admission"]["ownerLineSha256"])
        self.assertIsNone(receipt["admission"]["consentKind"])
        repo.git("add", "tools")                                              # staged is still not committed
        repo.run(spec, self.sha, "ab" * 32)
        self.assertEqual(repo.receipts()[-1]["refusal"], "ADMISSION_SOURCE_DIRTY")

    def test_a_committed_blanket_admits_an_unlisted_clip_on_both_venues_in_production_admission(self) -> None:
        repo = ProductionRepo(self, records=[], cleanup_gone=True, blankets=[blanket_record()])
        proc = _ps_json(
            f"$s = Resolve-DvAdmissionSources -RepoRoot '{repo.root}'\n$t = ConvertFrom-DvVenueTableText $s.tableText\n"
            f"$a = Get-DvClipAdmission -ClipId '{OTHER_CLIP}' -Venue 'ultra-magnus' -Table $t -ConsentText $s.consentText -RepoRoot '{repo.root}'\n"
            f"$b = Get-DvClipAdmission -ClipId '{OTHER_CLIP}' -Venue 'bachelor' -Table $t -ConsentText $s.consentText -RepoRoot '{repo.root}'\n"
            f"$c = Get-DvClipAdmission -ClipId '{OTHER_CLIP}' -Venue 'laptop' -Table $t -ConsentText $s.consentText -RepoRoot '{repo.root}'\n"
            "Write-Output ('UM=' + $a.admitted + ' ' + $a.consentKind + ' ' + $a.consentSha256Prefix + ' ' + $a.ownerLineSha256)\n"
            "Write-Output ('B=' + $b.admitted + ' ' + $b.consentKind)\nWrite-Output ('LAPTOP=' + $c.admitted + ' ' + $c.reason)\n",
            repo.dv / "DualVenueRunner.psm1", {})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"UM=True blanket {BLANKET_SENTENCE_SHA256[:12]} {BLANKET_SENTENCE_SHA256}", proc.stdout)
        self.assertIn("B=True blanket", proc.stdout)
        self.assertIn("LAPTOP=False VENUE_CLIP_CONSENT_ABSENT", proc.stdout, "a venue the table does not have is not 'any venue'")

    def test_the_runner_and_leg_script_never_write_the_blanket_record(self) -> None:
        writers = re.compile(r"(Set-Content|Add-Content|Out-File|WriteAllText|WriteAllBytes|Remove-Item|Move-Item|Copy-Item|New-Item)[^\n]*(blanket|Blanket)", re.I)
        for name in ("Invoke-VenueLeg.ps1", "DualVenueRunner.psm1"):
            for line in (DV / name).read_text(encoding="utf-8").splitlines():
                if not line.lstrip().startswith("#"):
                    self.assertIsNone(writers.search(line), f"{name} must never write the blanket record: {line.strip()}")

    # -- mutations: each rule's test must be able to fail ----------------------------------------------------------------
    def test_mutation_without_the_pinned_sentence_hash_a_re_hashed_forgery_would_admit(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ([string]$b.ownerSentenceSha256 -cne $script:BlanketOwnerSentenceSha256) {", "if ($false) {")])
        forged = "You have my blanket permission to use any clip at all."
        self.write_consent(blankets=[blanket_record(ownerSentence=forged, ownerSentenceSha256=hashlib.sha256(forged.encode("utf-8")).hexdigest())])
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=OTHER_CLIP), dv=mutated)
        self.assertNotEqual(submitted, [], "with the pin removed a re-hashed forgery admits -- so the pin is guarded")

    def test_mutation_without_the_owner_check_a_hub_written_blanket_would_admit(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ([string]$b.writtenBy -cne 'owner') {", "if ($false) {")])
        self.write_consent(blankets=[blanket_record(writtenBy="hub")])
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=OTHER_CLIP), dv=mutated)
        self.assertNotEqual(submitted, [], "with the writtenBy check removed a hub-written blanket admits -- so the check is guarded")

    def test_mutation_without_the_stored_hash_check_an_edited_sentence_would_admit(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ((Get-DvSha256OfText ([string]$b.ownerSentence)) -cne [string]$b.ownerSentenceSha256) {", "if ($false) {")])
        self.write_consent(blankets=[blanket_record(ownerSentence=BLANKET_SENTENCE + ".")])
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(clip=OTHER_CLIP), dv=mutated)
        self.assertNotEqual(submitted, [], "with the sentence-vs-hash check removed an edited sentence admits -- so the check is guarded")


@requires_windows_pwsh
class CommittedConsentOnlyTests(RunnerHarness, unittest.TestCase):
    """sol BLOCKER: -ConsentPath / -VenueTablePath were unrestricted production parameters and ownerLineSha256 was checked only
    for hex shape, so an agent-written consent file and venue table admitted a leg with no owner-typed line. Production now
    reads consent and the venue table ONLY from the tracked files at the COMMITTED revision, refuses when the working copy
    differs, and refuses every test seam without -OfflineTestMode."""

    def setUp(self) -> None:
        self.make_harness()
        self.spec = self.write_spec()
        self.build = "ab" * 32

    def forged_files(self) -> tuple[Path, Path]:
        """The sol repro: an agent-written consent record (a bare 64-hex 'hash', recordedBy 'producer') and a venue-table copy with the
        reviewed cleanup switch flipped."""
        consent = self.tmp / "forged-consent.json"
        consent.write_text(json.dumps(consent_file({"venue": "ultra-magnus", "clipId": OWNER_CLIP, "ownerLineSha256": "a" * 64,
                                                     "recordedUtc": "2026-10-01T00:00:00Z", "recordedBy": "producer"})), encoding="utf-8")
        table = json.loads((DV / "venues.json").read_text(encoding="utf-8"))
        table["ownerFootage"]["cleanupClassGone"] = True
        forged_table = self.tmp / "forged-venues.json"
        forged_table.write_text(json.dumps(table), encoding="utf-8")
        return consent, forged_table

    def test_the_forged_consent_and_table_of_the_sol_repro_are_refused_in_production(self) -> None:
        repo = ProductionRepo(self, records=[])
        consent, table = self.forged_files()
        for extra in (["-ConsentPath", str(consent)], ["-VenueTablePath", str(table)], ["-ConsentPath", str(consent), "-VenueTablePath", str(table)],
                      ["-GeneratorScript", str(self.gen)], ["-UmRunScript", str(self.um)], ["-RepoRoot", str(self.tmp)], ["-WorkDir", str(self.tmp / "w")]):
            proc = repo.run(self.spec, self.sha, self.build, extra=extra)
            self.assertNotEqual(proc.returncode, 0, extra)
            self.assertIn("DVE_TEST_SEAM_IN_PRODUCTION", proc.stdout + proc.stderr, extra)
        self.assertEqual(repo.receipts(), [], "a refused seam writes no receipt and submits nothing")

    def test_an_uncommitted_edit_to_the_consent_file_is_refused(self) -> None:
        repo = ProductionRepo(self, records=[])
        repo.write_consent(consent_record("ultra-magnus", OWNER_CLIP))        # a perfectly valid record -- but not committed
        proc = repo.run(self.spec, self.sha, self.build)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        [receipt] = repo.receipts()
        self.assertEqual(receipt["refusal"], "ADMISSION_SOURCE_DIRTY")
        self.assertEqual(receipt["outcome"], "DEVICE_UNAVAILABLE")
        self.assertIsNone(receipt["evidence"]["umRunOutcome"], "nothing was submitted, not even the health probe")
        self.assertIsNone(receipt["admission"]["ownerLineSha256"])
        repo.git("add", "tools")                                              # staged is still not committed
        proc = repo.run(self.spec, self.sha, self.build)
        self.assertEqual(repo.receipts()[-1]["refusal"], "ADMISSION_SOURCE_DIRTY")

    def test_an_uncommitted_edit_to_the_venue_table_is_refused(self) -> None:
        repo = ProductionRepo(self, records=[consent_record("ultra-magnus", OWNER_CLIP)], cleanup_gone=False)
        table = json.loads((repo.dv / "venues.json").read_text(encoding="utf-8"))
        table["ownerFootage"]["cleanupClassGone"] = True                       # the reviewed switch, flipped by hand
        (repo.dv / "venues.json").write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8", newline="\n")
        repo.run(self.spec, self.sha, self.build)
        [receipt] = repo.receipts()
        self.assertEqual(receipt["refusal"], "ADMISSION_SOURCE_DIRTY")
        self.assertIsNone(receipt["evidence"]["umRunOutcome"])

    def test_a_consent_file_that_was_never_committed_is_refused(self) -> None:
        repo = ProductionRepo(self, records=[])
        repo.git("rm", "-q", "--cached", "tools/profiling/dual-venue/venue-clip-consent.json")
        repo.git("commit", "-q", "-m", "drop the consent file from HEAD")
        repo.write_consent(consent_record("ultra-magnus", OWNER_CLIP))
        repo.run(self.spec, self.sha, self.build)
        [receipt] = repo.receipts()
        self.assertEqual(receipt["refusal"], "ADMISSION_SOURCE_NOT_COMMITTED")

    def test_committed_owner_consent_passes_the_consent_check_and_the_receipt_records_the_blob_ids(self) -> None:
        # Consent is necessary, not sufficient: with the reviewed cleanup switch off the leg still refuses -- AFTER the consent
        # gate, so the refusal token proves the committed record was accepted -- and the receipt names what it was admitted on.
        repo = ProductionRepo(self, records=[consent_record("ultra-magnus", OWNER_CLIP)], cleanup_gone=False)
        repo.run(self.spec, self.sha, self.build)
        [receipt] = repo.receipts()
        self.assertEqual(receipt["refusal"], "OWNER_CLIP_REFUSED_PENDING_CROSS_VOLUME_2")
        admission = receipt["admission"]
        self.assertEqual(admission["mode"], "production")
        self.assertEqual(admission["consentBlobSha"], repo.blob("venue-clip-consent.json"))
        self.assertEqual(admission["venueTableBlobSha"], repo.blob("venues.json"))
        self.assertEqual(admission["headCommit"], repo.git("rev-parse", "HEAD"))
        self.assertEqual(admission["consentLastCommit"], repo.git("log", "-1", "--format=%H", "--", "tools/profiling/dual-venue/venue-clip-consent.json"))

    def test_committed_consent_and_a_committed_cleanup_switch_admit_with_the_owners_line_recorded(self) -> None:
        repo = ProductionRepo(self, records=[consent_record("ultra-magnus", OWNER_CLIP)], cleanup_gone=True)
        module = repo.dv / "DualVenueRunner.psm1"
        proc = _ps_json(
            f"$s = Resolve-DvAdmissionSources -RepoRoot '{repo.root}'\n"
            "$t = ConvertFrom-DvVenueTableText $s.tableText\n"
            f"$a = Get-DvClipAdmission -ClipId '{OWNER_CLIP}' -Venue 'ultra-magnus' -Table $t -ConsentText $s.consentText -RepoRoot '{repo.root}'\n"
            f"$b = Get-DvClipAdmission -ClipId '{OWNER_CLIP}' -Venue 'bachelor' -Table $t -ConsentText $s.consentText -RepoRoot '{repo.root}'\n"
            "Write-Output ('SRC=' + $s.ok + ' ' + $s.mode + ' ' + $s.consentBlobSha + ' ' + $s.venueTableBlobSha)\n"
            "Write-Output ('UM=' + $a.admitted + ' ' + $a.ownerLineSha256)\nWrite-Output ('B=' + $b.admitted + ' ' + $b.reason)\n", module, {})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout
        line_sha = hashlib.sha256(f"CLIP ultra-magnus: {OWNER_CLIP}".encode()).hexdigest()
        self.assertIn(f"SRC=True production {repo.blob('venue-clip-consent.json')} {repo.blob('venues.json')}", out)
        self.assertIn(f"UM=True {line_sha}", out)
        self.assertIn("B=False VENUE_CLIP_CONSENT_ABSENT", out, "consent on one venue never admits the other")

    def test_the_production_receipt_root_must_stay_under_claude_state(self) -> None:
        repo = ProductionRepo(self, records=[])
        proc = repo.run(self.spec, self.sha, self.build, extra=["-ReceiptRoot", str(self.tmp / "published-receipts")])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DVE_RECEIPT_ROOT_MUST_STAY_LOCAL", proc.stdout + proc.stderr)
        self.assertFalse((self.tmp / "published-receipts").exists())

    # -- mutations: each rule's test must be able to fail ----------------------------------------------------------------
    def test_mutation_without_the_script_level_seam_check_the_function_level_check_still_refuses_with_a_receipt(self) -> None:
        mutated = ProductionRepo(self, records=[], mutations=[("Invoke-VenueLeg.ps1", "if ($PSBoundParameters.ContainsKey($seam) -and -not $OfflineTestMode) {", "if ($false) {")])
        consent, table = self.forged_files()
        mutated.run(self.spec, self.sha, self.build, extra=["-ConsentPath", str(consent), "-VenueTablePath", str(table)])
        [receipt] = mutated.receipts()
        self.assertEqual(receipt["refusal"], "TEST_SEAM_IN_PRODUCTION", "the second layer (Resolve-DvAdmissionSources) refuses a path override outside offline test mode")
        self.assertIsNone(receipt["evidence"]["umRunOutcome"])

    def test_mutation_without_either_seam_check_the_forged_files_are_still_never_read_in_production(self) -> None:
        mutated = ProductionRepo(self, records=[], mutations=[("Invoke-VenueLeg.ps1", "if ($PSBoundParameters.ContainsKey($seam) -and -not $OfflineTestMode) {", "if ($false) {"),
                                                               ("DualVenueRunner.psm1", "if (-not [string]::IsNullOrWhiteSpace($ConsentPath) -or -not [string]::IsNullOrWhiteSpace($VenueTablePath)) {", "if ($false) {")])
        consent = self.tmp / "forged-consent.json"
        consent.write_text(json.dumps(consent_file(consent_record("ultra-magnus", OWNER_CLIP))), encoding="utf-8")
        _, forged_table = self.forged_files()
        mutated.run(self.spec, self.sha, self.build, extra=["-ConsentPath", str(consent), "-VenueTablePath", str(forged_table)])
        [receipt] = mutated.receipts()
        self.assertEqual(receipt["refusal"], "VENUE_CLIP_CONSENT_ABSENT",
                         "production admission reads the committed blobs; a forged path is ignored even with both seam checks removed")

    def test_mutation_without_the_script_level_seam_check_a_callers_generator_replaces_the_real_one_in_production(self) -> None:
        mutated = ProductionRepo(self, records=[consent_record("ultra-magnus", OWNER_CLIP)], cleanup_gone=True,
                                 mutations=[("Invoke-VenueLeg.ps1", "if ($PSBoundParameters.ContainsKey($seam) -and -not $OfflineTestMode) {", "if ($false) {")])
        mutated.commit_leg(self.spec)   # production also needs the leg spec COMMITTED (LEG_SPEC_NOT_COMMITTED otherwise)
        stub = self.tmp / "caller-generator.ps1"
        stub.write_text("param($SourceCommit,$BuildManifestSha256,$ClipId,$OutFile,$RepoRoot,$PlaySeconds,$Venue,$Backend,$ScaleFactor,$TelemetryArm,"
                        "$CpuQuiescenceThresholdPercent,[switch]$ContactSheet,$ContactSheetFrames,[switch]$ForceLookAssist,$LookFlavor,$VenueTablePath)\n"
                        "throw 'DUAL_VENUE_STUB_STOP the caller generator ran'\n", encoding="utf-8")
        mutated.run(self.spec, self.sha, self.build, extra=["-GeneratorScript", str(stub)])
        [receipt] = mutated.receipts()
        self.assertEqual(receipt["refusal"], "GENERATOR_REFUSED_DUAL_VENUE_STUB_STOP",
                         "without the seam check a caller's generator runs in production (it stops before anything is submitted) -- so the check is what prevents it")
        self.assertIsNone(receipt["evidence"]["umRunOutcome"])

    def test_mutation_without_the_dirty_check_the_refusal_changes_but_the_forged_working_copy_still_cannot_admit(self) -> None:
        anchor = "if ($work.exitCode -ne 0 -or ([Text.Encoding]::ASCII.GetString($work.bytes)).Trim() -cne $blobSha) { return (& $bad 'ADMISSION_SOURCE_DIRTY') }"
        mutated = ProductionRepo(self, records=[], mutations=[("DualVenueRunner.psm1", anchor, "")])
        mutated.write_consent(consent_record("ultra-magnus", OWNER_CLIP))
        mutated.run(self.spec, self.sha, self.build)
        [receipt] = mutated.receipts()
        self.assertNotEqual(receipt["refusal"], "ADMISSION_SOURCE_DIRTY", "the dirty check is what produced that token -- so the test guards it")
        self.assertEqual(receipt["refusal"], "VENUE_CLIP_CONSENT_ABSENT", "defence in depth: the text admission reads is the committed blob's, never the working copy's")

    def test_mutation_without_the_seam_check_an_uncommitted_seam_value_is_not_refused_as_a_seam(self) -> None:
        mutated = ProductionRepo(self, records=[], mutations=[("Invoke-VenueLeg.ps1", "if ($PSBoundParameters.ContainsKey($seam) -and -not $OfflineTestMode) {", "if ($false) {")])
        proc = mutated.run(self.spec, self.sha, self.build, extra=["-WorkDir", str(self.tmp / "w2")])
        self.assertNotIn("DVE_TEST_SEAM_IN_PRODUCTION", proc.stdout + proc.stderr, "with the check removed the seam is accepted -- so the seam test guards it")

    def test_mutation_without_the_owner_line_checks_a_bare_hash_admits(self) -> None:
        hollow = {"venue": "ultra-magnus", "clipId": OWNER_CLIP, "ownerLine": "whatever", "ownerLineSha256": "a" * 64,
                  "recordedUtc": "2026-10-01T00:00:00Z", "recordedBy": "producer"}
        self.write_consent(hollow)
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(receipt["refusal"], "VENUE_CLIP_CONSENT_INVALID")
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($line -cnotin @($spellings | ForEach-Object { 'CLIP ' + $_ + ': ' + [string]$r.clipId })) {", "if ($false) {"),
                                       ("DualVenueRunner.psm1", "if ((Get-DvSha256OfText $line) -cne [string]$r.ownerLineSha256) {", "if ($false) {"),
                                       ("DualVenueRunner.psm1", "if ([string]$r.recordedBy -cne 'owner') {", "if ($false) {")])
        self.write_artifacts()
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertNotEqual(submitted, [], "with the three owner-line checks removed the hollow record admits the leg -- each is guarded")

    def test_each_owner_line_check_is_needed_on_its_own(self) -> None:
        base = consent_record("ultra-magnus", OWNER_CLIP)
        cases = [
            ("the line", dict(base, ownerLine="CLIP bachelor: " + OWNER_CLIP, ownerLineSha256=hashlib.sha256(("CLIP bachelor: " + OWNER_CLIP).encode()).hexdigest()), "if ($line -cnotin @($spellings | ForEach-Object { 'CLIP ' + $_ + ': ' + [string]$r.clipId })) {"),
            ("the hash", dict(base, ownerLineSha256="a" * 64), "if ((Get-DvSha256OfText $line) -cne [string]$r.ownerLineSha256) {"),
            ("the recorder", dict(base, recordedBy="hub"), "if ([string]$r.recordedBy -cne 'owner') {"),
        ]
        self.write_artifacts()
        for name, record, anchor in cases:
            self.write_consent(record)
            _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec())
            self.assertEqual(receipt["refusal"], "VENUE_CLIP_CONSENT_INVALID", name)
            mutated = self.mutated_runner([("DualVenueRunner.psm1", anchor, "if ($false) {")])
            _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
            self.assertNotEqual(submitted, [], f"without the {name} check this record admits the leg")


@requires_windows_pwsh
class JobTerminalsAndHardeningTests(RunnerHarness, unittest.TestCase):
    """fable hardening: a product failure after a sound 20 s run is a FAIL (the proof comes from the run log the job published),
    a terminal with no run log is INVALID; sheets of owner footage stay under .claude-state; no registry snapshot is taken any
    more; a venue that cannot compose the LOOK sheet is VENUE_TOOLING, not a product FAIL."""

    def setUp(self) -> None:
        self.make_harness()

    def test_product_failures_after_a_sound_run_are_fail_with_the_proof_in_the_receipt(self) -> None:
        # exactly what the job writes on these terminals (real_failure_summary: key for key): no sourceFrames block, no evidence manifest, and the
        # frame counters only inside gpuSummary
        # (DVE-LEG-TERMINALS-1: the counters are the ones each terminal means -- a CPU_FALLBACK_DETECTED with no gpu frame and no cpu frame names no backend, and the
        # runner now ends such a leg as a typed no-signal receipt instead of a FAIL the production writer would refuse)
        gpu_counters = {"CPU_FALLBACK_DETECTED": {"gpuReconReadbackFrames": 888, "cpuFrames": 12}}
        for token, code in (("GPU_RECON_FRAMES_ZERO", 13), ("CPU_FALLBACK_DETECTED", 14), ("CPU_BACKEND_PATH_MISMATCH", 28)):
            self.write_artifacts(source_frames=False, exact_summary=real_failure_summary(token, **gpu_counters.get(token, {})), manifest=None)
            (self.artifacts / "evidence-manifest.json").unlink()
            proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=(token, code))
            self.assertEqual(receipt["outcome"], "FAIL", f"{token}: {receipt['outcomeDetail']}")
            self.assertIn(token, receipt["outcomeDetail"])
            self.assertTrue(receipt["playback"]["valid"], token)
            self.assertFalse(receipt["playback"]["jobOracleBlockPresent"])

    def test_a_failure_whose_run_proves_less_than_20_seconds_is_invalid_not_a_product_fail(self) -> None:
        self.write_artifacts(source_frames=False, summary={"result": "CPU_FALLBACK_DETECTED"}, line={"source_advanced": 40})
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("CPU_FALLBACK_DETECTED", 14))
        self.assertEqual(receipt["outcome"], "INVALID")

    def test_terminals_with_no_run_log_have_no_proof_and_are_invalid(self) -> None:
        for token, code in (("SMOKE_RUN_FAILED", 18), ("SMOKE_LOG_UNAVAILABLE", 16)):
            self.write_artifacts(source_frames=False, summary={"result": token}, log=False, result=False)
            proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=(token, code))
            self.assertEqual(receipt["outcome"], "INVALID", f"{token}: {receipt['outcomeDetail']}")
            self.assertFalse(receipt["playback"]["valid"])

    def test_mutation_demanding_the_jobs_block_on_every_terminal_turns_the_product_failures_invalid(self) -> None:
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($resolved.outcome -eq 'CAPTURED' -and -not $playback.jobOracleBlockPresent) {", "if (-not $playback.jobOracleBlockPresent) {")])
        self.write_artifacts(source_frames=False, summary={"result": "CPU_FALLBACK_DETECTED"})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), token=("CPU_FALLBACK_DETECTED", 14), dv=mutated)
        self.assertEqual(receipt["outcome"], "INVALID", "the old behaviour (a product failure read as 'no signal') -- so the FAIL test above guards the fix")

    def test_the_docs_outcome_table_names_every_terminal_the_way_the_runner_maps_it(self) -> None:
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        table = doc.split("## Outcome mapping", 1)[1]
        for token in ("GPU_RECON_FRAMES_ZERO", "CPU_FALLBACK_DETECTED", "CPU_BACKEND_PATH_MISMATCH", "SMOKE_RUN_FAILED", "SMOKE_LOG_UNAVAILABLE", "VENUE_TOOLING"):
            self.assertIn(token, table)
        row = next(r for r in table.splitlines() if "GPU_RECON_FRAMES_ZERO" in r)
        self.assertIn("`FAIL`", row)
        row = next(r for r in table.splitlines() if "SMOKE_RUN_FAILED" in r)
        self.assertIn("`INVALID`", row)

    # -- -SheetCopyDir stays local ---------------------------------------------------------------------------------------
    def test_the_sheet_copy_dir_must_be_under_claude_state(self) -> None:
        self.write_artifacts(sheet=True)
        published = self.tmp / "published"
        proc, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), extra=["-SheetCopyDir", str(published)])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DVE_SHEET_COPY_MUST_STAY_LOCAL", proc.stdout + proc.stderr)
        self.assertFalse(published.exists())
        self.assertEqual(submitted, [], "refused before anything is submitted")
        local = self.tmp / ".claude-state" / "sheets"
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), extra=["-SheetCopyDir", str(local), "-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertTrue((local / "sheet-unit-leg-ultra-magnus-cpu-classic.png").exists())

    def test_mutation_without_the_sheet_copy_guard_an_owner_sheet_is_copied_anywhere(self) -> None:
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "    $SheetCopyDir = Test-DvLocalReceiptRoot -Path $SheetCopyDir", "    $null = Test-DvLocalReceiptRoot -Path $SheetCopyDir")])
        self.write_artifacts(sheet=True)
        published = self.tmp / "published"
        self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), extra=["-SheetCopyDir", str(published), "-Backend", "cpu"], dv=mutated)
        self.assertTrue(any(published.glob("sheet-*.png")), "with the guard removed the sheet lands outside .claude-state -- so the guard is tested")

    # -- the registry snapshot is gone ------------------------------------------------------------------------------------
    def test_no_registry_snapshot_is_taken_or_left_on_the_share(self) -> None:
        self.write_artifacts()
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual([s.rsplit("-", 1)[-1] for s in submitted][0], "health")
        self.assertEqual(len(submitted), 2)
        self.assertIsNone(receipt["registry"])
        self.assertFalse(list(self.share.rglob("dve-reg")), "no .reg export is ever written to the agent share")
        for name in ("DualVenueRunner.psm1", "Invoke-VenueLeg.ps1"):
            code = "\n".join(l for l in (DV / name).read_text(encoding="utf-8").splitlines() if not l.lstrip().startswith("#"))
            self.assertNotIn("reg export", code)
            self.assertNotIn("reg import", code)
            self.assertNotIn("dve-reg", code)
            self.assertNotIn("New-DvReg", code)

    def test_a_run_that_did_not_use_the_run_scoped_settings_store_is_invalid(self) -> None:
        for isolated in ("venue_NOT_ISOLATED", None):
            self.write_artifacts(isolated=isolated)
            _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec())
            self.assertEqual(receipt["outcome"], "INVALID", isolated)
            self.assertIn("SETTINGS_NOT_ISOLATED", receipt["outcomeDetail"])

    # -- a venue that cannot compose the sheet ----------------------------------------------------------------------------
    def test_a_look_leg_whose_venue_cannot_compose_the_sheet_is_venue_tooling_not_a_product_fail(self) -> None:
        marker = "CONTACT_SHEET_COMPOSE_UNAVAILABLE no python+Pillow interpreter was found"
        self.write_artifacts(sheet=False, compose_marker=marker, raw_frames=True)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "VENUE_TOOLING", receipt["outcomeDetail"])
        self.assertIn("CONTACT_SHEET_COMPOSE_UNAVAILABLE", receipt["outcomeDetail"])
        self.assertIsNone(receipt["look"]["contactSheet"])
        self.assertTrue(receipt["look"]["composeStatus"].startswith("CONTACT_SHEET_COMPOSE_UNAVAILABLE"))
        self.assertTrue(Path(receipt["look"]["rawFramesDir"]).is_dir(), "the raw frames are kept locally for composition elsewhere")
        self.assertTrue(receipt["playback"]["valid"], "the play proof is sound; only the venue's tooling is missing")
        self.assertIn("VENUE_TOOLING", subprocess.run([PWSH, "-NoProfile", "-Command", f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force; Get-DvOutcomeEnum"],
                                                       capture_output=True, text=True).stdout)

    def test_a_look_leg_with_no_sheet_and_no_compose_marker_is_still_a_product_fail(self) -> None:
        self.write_artifacts(sheet=False)
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "FAIL")
        self.assertIn("no contact sheet", receipt["outcomeDetail"])

    def test_mutation_without_the_tooling_branch_a_venue_without_pillow_is_a_product_fail(self) -> None:
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($sheetMissing -and $null -ne $composeUnavailable) {", "if ($false) {")])
        self.write_artifacts(sheet=False, compose_marker="CONTACT_SHEET_COMPOSE_UNAVAILABLE x", raw_frames=True)
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), extra=["-Backend", "cpu"], dv=mutated)
        self.assertEqual(receipt["outcome"], "FAIL")

# ---------------------------------------------------------------------------------------------------
# DUAL-VENUE-EVIDENCE-2 round 1 (successor of PR #207; sol r2 BLOCKER: Test-DvReceiptValid validated a hand-built production PASS).
# CLASS: a receipt is valid ONLY when EVERY claim is re-derived from a COMMITTED blob or a HASHED artifact; no self-asserted field
# is ever an input. Every receipt below is built ONLY through the evidence-bearing path: a real consent blob COMMITTED in a temp git
# repo (ProductionRepo) and real, hashed run-evidence files (written by the same harness the runner tests use), with the claims the
# receipt makes computed from those files by an independent Python mirror -- never a placeholder hash.
EVIDENCE_REL_FILES = ("summary.json", "evidence-manifest.json", "result.json", "logs/smoke-run.log", "um-run.json")
EVIDENCE_CLAIM_KEYS = {"summary.json": "summaryJsonSha256", "evidence-manifest.json": "evidenceManifestSha256", "result.json": "resultJsonSha256",
                       "logs/smoke-run.log": "logSha256", "um-run.json": "umRunJsonSha256"}


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def redigest(receipt: dict) -> dict:
    """Recompute subject.digest from the subject fields (the public formula): what a hand-editor does after changing a subject field."""
    s = receipt["subject"]
    identity = {k: s[k] for k in ("backend", "buildManifestSha256", "clipContentSha256", "clipId", "legSpecSha256", "lookFlavor")}
    s["digest"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return receipt


def write_contact_frames_manifest(ev: Path) -> str:
    """What the runner writes at capture time (Invoke-VenueLeg.ps1): contact-frames.json listing every file of contact-sheet/raw with its sha256."""
    raw = ev / "contact-sheet" / "raw"
    files = [{"name": p.name, "sha256": sha256_of(p)} for p in sorted(raw.iterdir()) if p.is_file()]
    (ev / "contact-frames.json").write_bytes((json.dumps({"schema": "mlv-app/dual-venue-contact-frames/v1", "files": files}, indent=2) + "\n").encode("utf-8"))
    return sha256_of(ev / "contact-frames.json")


def stub_raw_frames() -> dict[str, bytes]:
    return {"frame-00.png": b"\x89PNG\r\n\x1a\nraw0", "frame-00.json": json.dumps({"index": 0, "saved": True, "path": "frame-00.png"}).encode("utf-8")}


ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")


def real_failure_summary(token: str, venue: str = "ultra-magnus", **gpu: int) -> dict:
    """The summary.json the REAL job writes on a product-failure terminal, key for key (playback-attr-3-cuda-job.ps1: GPU_RECON_FRAMES_ZERO
    ~2557, CPU_FALLBACK_DETECTED ~2568, CPU_BACKEND_PATH_MISMATCH ~3143; the display block is cut to the one field the validator reads).
    The frame counters live ONLY in the nested gpuSummary (Get-LastGpuSummary's five keys); there is no top-level gpuFramesTotal / cpuFrames, no
    rows, no sourceFrames, no backend / lookLeg (the variant edit only reaches the success summary) -- except CPU_BACKEND_PATH_MISMATCH, which the
    cpu variant writes with backend and a top-level gpuFramesTotal (recon + texture readback + texture no-readback)."""
    gpu_summary = {"cpuFrames": 0, "gpuPreviewFrames": 0, "gpuReconReadbackFrames": 0, "gpuTextureReadbackFrames": 0, "gpuTextureNoReadbackFrames": 0}
    gpu_summary.update(gpu)
    body: dict = {"schema": "playback-attr-3-cuda-venue.v1", "result": token, "fixtureRehearsal": False, "displayWake": None}
    if token == "CPU_BACKEND_PATH_MISMATCH":
        body["backend"] = "cpu"
    body["gpuSummary"] = gpu_summary
    if token == "CPU_BACKEND_PATH_MISMATCH":
        body["gpuFramesTotal"] = gpu_summary["gpuReconReadbackFrames"] + gpu_summary["gpuTextureReadbackFrames"] + gpu_summary["gpuTextureNoReadbackFrames"]
    if token == "PRESENTMON_UNAVAILABLE":   # the display-failure branch (~2662): both a top-level gpuFramesTotal and the nested gpuSummary, no backend / lookLeg
        body.update({"reason": "unit", "presentMonStatus": "unavailable", "chains": [], "presentMonCaptureStartUtc": "2026-10-02T00:00:00.0000000Z", "clockBracket": {},
                     "diagnostics": {}, "gpuFramesTotal": gpu_summary["gpuReconReadbackFrames"] + gpu_summary["gpuTextureReadbackFrames"] + gpu_summary["gpuTextureNoReadbackFrames"],
                     "frameRows": 900, "regions": {}})
    body.update({"display": {"venue": venue}, "sourceCommit": "e" * 40, "clipId": OWNER_CLIP, "artifactRoot": "X:\\stub"})
    return body


class EvidenceFactory(RunnerHarness):
    """Builds evidence-bearing receipts. Mixed into a TestCase whose setUp calls make_harness()."""

    def prod_repo(self, venue: str = "ultra-magnus", records: list[dict] | None = None, cleanup_gone: bool = True, leg_type: str = "speed",
                  spec: Path | None = None, mutations: list[tuple[str, str, str]] | None = None, blankets: list[dict] | None = None,
                  shipped_legs: bool = False) -> ProductionRepo:
        recs = [consent_record(venue, OWNER_CLIP)] if records is None else records
        repo = ProductionRepo(self, records=recs, cleanup_gone=cleanup_gone, mutations=mutations, blankets=blankets, shipped_legs=shipped_legs)
        self.spec_path = spec or self.write_spec(leg_type=leg_type)
        repo.commit_leg(self.spec_path, "unit-leg.json")
        return repo

    def evidence(self, name: str, venue: str = "ultra-magnus", exit_code: int = 0, token: str | None = "MEASUREMENT_CAPTURED",
                 backend: str = "cuda", leg_type: str = "speed", raw_frames: dict[str, bytes] | None = None, **artifact_opts) -> Path:
        """Real run-evidence files in the layout the runner copies locally (summary, manifest, launcher result, run log, um-run record).
        `backend` / `leg_type` shape summary.json the way the real job does: a cpu run's frame counters are cpuFrames > 0 / gpuFramesTotal 0, and
        the generator's `$isVariant` job (any venue but bachelor, any cpu run, any look leg) also writes backend / declaredVenue / lookLeg /
        lookAssistForced / lookFlavor. A look leg that kept its contact sheet also keeps its raw frames (`raw_frames`: name -> bytes)."""
        summary = dict(artifact_opts.pop("summary", None) or {})
        if artifact_opts.get("exact_summary") is not None:
            summary = {}   # (the exact summary replaces the body whole: no defaults, no variant fields -- the failure branches write none)
        elif backend == "cpu":
            summary.setdefault("gpuFramesTotal", 0)
            summary.setdefault("cpuFrames", 900)
        if venue != "bachelor" or backend == "cpu" or leg_type == "look":
            summary.setdefault("backend", backend)
            summary.setdefault("declaredVenue", venue)
            summary.setdefault("lookLeg", leg_type == "look")
            summary.setdefault("lookAssistForced", leg_type == "look")
            summary.setdefault("lookFlavor", "classic" if leg_type == "look" else None)
        art = self.tmp / name / "artifacts"
        ev = self.tmp / name / "evidence"
        saved = self.artifacts
        self.artifacts = art
        try:
            self.write_artifacts(summary=summary, **artifact_opts)
            self.stamp_identity(venue, self.build_sha)
        finally:
            self.artifacts = saved
        (ev / "logs").mkdir(parents=True)
        for rel in ("summary.json", "evidence-manifest.json", "result.json", "logs/smoke-run.log"):
            if (art / rel).exists():
                shutil.copyfile(art / rel, ev / rel)
        if (art / "contact-sheet").exists():
            shutil.copytree(art / "contact-sheet", ev / "contact-sheet")
            if leg_type == "look" and (ev / "contact-sheet" / "sheet.png").exists():
                (ev / "contact-sheet" / "raw").mkdir(exist_ok=True)
                for fname, data in (raw_frames if raw_frames is not None else stub_raw_frames()).items():
                    (ev / "contact-sheet" / "raw" / fname).write_bytes(data)
        (ev / "um-run.json").write_text(json.dumps({"schema": "mlv-app/dual-venue-um-run/v1", "exitCode": exit_code, "resultToken": token}), encoding="utf-8")
        return ev

    @staticmethod
    def derive_block(ev: Path) -> dict:
        """An INDEPENDENT mirror of Get-DvPlaybackEvidence: the playback block a sound receipt carries, computed from the evidence files."""
        log = (ev / "logs" / "smoke-run.log").read_bytes()
        lines = log.decode("utf-8").splitlines()
        session = next(re.search(r"measured_session id=(\d+)", l).group(1) for l in lines if "playback_smoke.measured_session" in l)
        summary_line = [l for l in lines if re.search(rf"playback_smoke\.summary session={session}(\s|$)", l)][-1]
        f = dict(re.findall(r"([A-Za-z0-9_]+)=(\S+)", summary_line))
        result = json.loads((ev / "result.json").read_text(encoding="utf-8"))
        summary = json.loads((ev / "summary.json").read_text(encoding="utf-8"))
        manifest = json.loads((ev / "evidence-manifest.json").read_text(encoding="utf-8")) if (ev / "evidence-manifest.json").exists() else {}
        log_sha = hashlib.sha256(log).hexdigest()
        declared = result.get("evidence", {}).get("runLogSnapshot", {}).get("sha256", "").lower()
        man_sha = manifest.get("smokeRunLog", {}).get("sha256", "")
        iso = [l for l in lines if re.search(r"interaction_trace event=automation\.pacing_isolated(\s|$)", l)]
        return {"sourceAdvanced": int(f["source_advanced"]), "requiredSourceFrames": int(f["required_source_frames"]),
                "nativeFps": float(f["native_fps"]), "paceFps": float(f["pace_fps"]), "fpsOverride": int(f["fps_override"]),
                "wrapped": int(f["wrapped"]) != 0, "wrapCount": int(f["wrap_count"]),
                "expectedRunNonce": result.get("evidence", {}).get("runNonce"), "observedRunNonce": f.get("run_nonce"),
                "manifestRunNonce": manifest.get("smokeRunLog", {}).get("runNonce"), "logSha256": log_sha,
                "logShaBound": declared == log_sha and (not man_sha or man_sha.lower() == log_sha),
                "settingsIsolated": bool(iso) and all(re.search(r"settings_store=run_scoped(\s|$)", l) for l in iso),
                "fixtureRehearsal": summary.get("fixtureRehearsal"), "clipId": summary.get("clipId")}

    @staticmethod
    def derive_scale(ev: Path, spec_path: Path, backend: str) -> dict:
        """An INDEPENDENT mirror of Get-DvScaleEvidence (DVE-SCALE2-LOOK-LEG-1 r2): the scale block a sound receipt carries -- what the leg asked for and what
        the app's own summary line says it rendered at."""
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        lines = (ev / "logs" / "smoke-run.log").read_text(encoding="utf-8").splitlines()
        session = next(re.search(r"measured_session id=(\d+)", l).group(1) for l in lines if "playback_smoke.measured_session" in l)
        f = dict(re.findall(r"([A-Za-z0-9_]+)=(\S+)", [l for l in lines if re.search(rf"playback_smoke\.summary session={session}(\s|$)", l)][-1]))
        requested = int(spec["scaleFactor"])
        accepted = int(spec.get("acceptedEffectiveScale", {}).get(backend, requested))
        effective = int(f["scale_active_last"]) if "scale_active_last" in f else "UNKNOWN"
        return {"requestedScale": requested, "effectiveScale": effective, "acceptedEffectiveScale": accepted}

    def receipt_for(self, repo: ProductionRepo, ev: Path, venue: str = "ultra-magnus", backend: str = "cuda", outcome: str = "PASS",
                    leg_type: str = "speed", spec_path: Path | None = None) -> dict:
        spec_bytes = (spec_path or self.spec_path).read_bytes()
        subject = {"buildManifestSha256": self.build_sha, "legSpecSha256": hashlib.sha256(spec_bytes).hexdigest(), "clipId": OWNER_CLIP,
                   "clipContentSha256": CLIP_CONTENT_SHA, "backend": backend, "lookFlavor": "classic" if leg_type == "look" else None}
        identity = {k: subject[k] for k in ("backend", "buildManifestSha256", "clipContentSha256", "clipId", "legSpecSha256", "lookFlavor")}
        subject["digest"] = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
        summary = json.loads((ev / "summary.json").read_text(encoding="utf-8"))
        # (an independent mirror of Get-DvVerbatimMetrics: PowerShell 7's ConvertFrom-Json turns an ISO-8601 string into a DateTime, which is not a metric it copies)
        metrics = {k: v for k, v in summary.items() if not isinstance(v, (dict, list)) and not (isinstance(v, str) and ISO_DATETIME.match(v))}
        if (ev / "evidence-manifest.json").exists():
            metrics["presentMonStats"] = json.loads((ev / "evidence-manifest.json").read_text(encoding="utf-8")).get("presentMonStats")
        evidence = {"localEvidenceDir": str(ev), "umRunOutcome": "RECEIPT", "artifactIndexPath": None}
        for rel in EVIDENCE_REL_FILES:
            evidence[EVIDENCE_CLAIM_KEYS[rel]] = sha256_of(ev / rel) if (ev / rel).exists() else None
        if (ev / "contact-sheet" / "raw").is_dir():
            evidence["contactFramesJsonSha256"] = write_contact_frames_manifest(ev)
        line = f"CLIP {venue}: {OWNER_CLIP}"
        receipt = {
            "schema": "mlv-app/dual-venue-receipt/v1", "receiptId": "r-" + hashlib.sha1(f"{ev}{backend}{outcome}".encode()).hexdigest()[:12],
            "card": "DUAL-VENUE-EVIDENCE-1", "legId": "unit-leg", "subject": subject, "scale": self.derive_scale(ev, spec_path or self.spec_path, backend),
            "venue": {"name": venue, "role": "supplementary", "declared": venue, "hostName": venue.upper(), "gpuNames": ["RTX 4090"]},
            "outcome": outcome, "outcomeDetail": "unit", "evidence": evidence, "metrics": metrics, "playback": self.derive_block(ev), "look": None,
            "admission": {"mode": "production", "consentBlobSha": repo.blob("venue-clip-consent.json"), "venueTableBlobSha": repo.blob("venues.json"),
                          "headCommit": repo.head(), "consentLastCommit": repo.git("log", "-1", "--format=%H", "--", "tools/profiling/dual-venue/venue-clip-consent.json"),
                          "ownerLineSha256": hashlib.sha256(line.encode()).hexdigest(), "ownerRecordedUtc": "2026-10-01T22:10:00Z"},
        }
        if leg_type == "look":
            sheet = ev / "contact-sheet" / "sheet.png"
            info = {"path": str(sheet), "sha256": sha256_of(sheet), "frames": 2, "backend": backend, "rawFramesDir": str(ev / "contact-sheet" / "raw")} if sheet.exists() else None
            receipt["look"] = {"legType": "look", "lookFlavor": "classic", "contactSheet": info}
        return receipt

    def status(self, receipt: dict, repo: ProductionRepo | None, evidence_dir: Path | None = None, module: Path | None = None, env: dict | None = None,
               repo_root: Path | None = None) -> tuple[str, bool, list[str]]:
        flags = (f" -RepoRoot '{repo_root or repo.root}'" if (repo_root or repo) else "") + (f" -EvidenceDir '{evidence_dir}'" if evidence_dir else "")
        proc = _ps_json(f"$r = $env:DVE_RECEIPT | ConvertFrom-Json\n$v = Test-DvReceiptValid -Receipt $r{flags}\nWrite-Output ('STATUS=' + $v.status)\n"
                        "Write-Output ('VALID=' + $v.valid)\n$v.reasons | ForEach-Object { Write-Output ('REASON=' + $_) }\n"
                        "$v.unbound | ForEach-Object { Write-Output ('UNBOUND=' + $_) }\n",
                        module or DV / "DualVenueRunner.psm1", dict(env or {}, DVE_RECEIPT=json.dumps(receipt)))
        if proc.returncode != 0:
            raise AssertionError(proc.stdout + proc.stderr)
        lines = [l for l in proc.stdout.splitlines() if l.strip()]
        self.last_unbound = [l[len("UNBOUND="):] for l in lines if l.startswith("UNBOUND=")]
        lines = [l for l in lines if not l.startswith("UNBOUND=")]
        return lines[0][len("STATUS="):], lines[1] == "VALID=True", [l[len("REASON="):] for l in lines[2:]]

    def assertAdvisory(self, receipt: dict, repo: ProductionRepo, **kw) -> None:
        """A production receipt whose every claim re-derives is ADVISORY: valid=false, the single typed reason VENUE_ANCHOR_ABSENT -- never VERIFIED."""
        status, valid, reasons = self.status(receipt, repo, **kw)
        self.assertEqual((status, valid), ("ADVISORY", False), reasons)
        self.assertEqual(len(reasons), 1, reasons)
        self.assertTrue(reasons[0].startswith("VENUE_ANCHOR_ABSENT"), reasons)

    def status_batch(self, repo: ProductionRepo | None, cases: list[tuple[dict, Path | None]], module: Path | None = None, env: dict | None = None) -> list[tuple[str, bool, list[str], list[str]]]:
        """Test-DvReceiptValid over several receipts in ONE pwsh process (a pwsh start dominates the cost of a check, and the hosted Windows hygiene
        job has a 75 min cap): cases = [(receipt, evidence_dir | None), ...] -> [(status, valid, reasons, unbound), ...] in order."""
        out: list[tuple[str, bool, list[str], list[str]]] = []
        for start in range(0, len(cases), 4):
            chunk = cases[start:start + 4]
            payload = json.dumps([{"receipt": r, "evidenceDir": str(d) if d else None} for r, d in chunk])
            script = ("$cases = $env:DVE_CASES | ConvertFrom-Json\nforeach ($c in $cases) {\n  $p = @{ Receipt = $c.receipt }\n"
                      "  if ($env:DVE_REPO) { $p.RepoRoot = $env:DVE_REPO }\n  if ($c.evidenceDir) { $p.EvidenceDir = $c.evidenceDir }\n"
                      "  $v = Test-DvReceiptValid @p\n  Write-Output 'CASE'\n  Write-Output ('STATUS=' + $v.status)\n  Write-Output ('VALID=' + $v.valid)\n"
                      "  $v.reasons | ForEach-Object { Write-Output ('REASON=' + $_) }\n  $v.unbound | ForEach-Object { Write-Output ('UNBOUND=' + $_) }\n}\n")
            proc = _ps_json(script, module or DV / "DualVenueRunner.psm1", dict(env or {}, DVE_CASES=payload, DVE_REPO=str(repo.root) if repo else ""))
            if proc.returncode != 0:
                raise AssertionError(proc.stdout + proc.stderr)
            blocks = proc.stdout.replace("\r\n", "\n").split("CASE\n")[1:]
            self.assertEqual(len(blocks), len(chunk), proc.stdout)
            for block in blocks:
                lines = [l for l in block.splitlines() if l.strip()]
                out.append((lines[0][len("STATUS="):], lines[1] == "VALID=True", [l[len("REASON="):] for l in lines[2:] if l.startswith("REASON=")],
                            [l[len("UNBOUND="):] for l in lines[2:] if l.startswith("UNBOUND=")]))
        return out

    def expect(self, got: tuple[str, bool, list[str], list[str]], kind: str, label: str = "", needle: str | None = None) -> None:
        """kind: ADVISORY (valid=false, the single typed reason VENUE_ANCHOR_ABSENT) | INVALID / INCOMPLETE (+ needle in a reason) | NO_SIGNAL | VERIFIED."""
        status, valid, reasons, _ = got
        if kind == "ADVISORY":
            self.assertEqual((status, valid), ("ADVISORY", False), (label, reasons))
            self.assertEqual(len(reasons), 1, (label, reasons))
            self.assertTrue(reasons[0].startswith("VENUE_ANCHOR_ABSENT"), (label, reasons))
        elif kind in ("INVALID", "INCOMPLETE"):
            self.assertFalse(valid, f"{label}: a receipt that does not re-derive was believed")
            self.assertEqual(status, kind, (label, reasons))
            if needle:
                self.assertTrue(any(needle in r for r in reasons), f"{label}: {needle!r} not in {reasons}")
        else:
            self.assertEqual(status, kind, (label, reasons))
            self.assertEqual(valid, kind in ("NO_SIGNAL", "VERIFIED", "VERIFIED_OFFLINE_TEST"), (label, reasons))

    def assertNotValid(self, receipt: dict, repo: ProductionRepo | None, needle: str, status: str | None = None, **kw) -> list[str]:
        got, valid, reasons = self.status(receipt, repo, **kw)
        self.assertFalse(valid, f"{needle}: a receipt that does not re-derive was believed")
        self.assertIn(got, ("INVALID", "INCOMPLETE"), needle)
        if status:
            self.assertEqual(got, status, f"{needle}: {reasons}")
        self.assertTrue(any(needle in r for r in reasons), f"{needle!r} not in {reasons}")
        return reasons


@requires_windows_pwsh
class EvidenceBoundReceiptTests(EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    """sol r2 BLOCKER: a hand-built production PASS (format-only hashes, self-asserted booleans, a consent blob with ZERO records, no
    evidence at all) validated. Now nothing validates unless it re-derives from a committed blob or a hashed file."""

    def setUp(self) -> None:
        self.make_harness()

    # -- the sound path -------------------------------------------------------------------------------------------------
    def test_a_receipt_built_through_the_evidence_path_is_verified_and_pass_vs_fail_is_derived_from_the_committed_criteria(self) -> None:
        repo = self.prod_repo(shipped_legs=True)   # the one test that keeps the whole shipped legs/ tree, so the validator's scan of every committed leg stays covered
        ev = self.evidence("ok")
        self.assertAdvisory(self.receipt_for(repo, ev), repo)
        # the criteria come from the COMMITTED leg spec (supplementary/cuda: rows gt 0): a run whose rows are 0 is a derived FAIL
        bad = self.evidence("bad", summary={"rows": 0})
        self.assertAdvisory(self.receipt_for(repo, bad, outcome="FAIL"), repo)
        self.assertNotValid(self.receipt_for(repo, bad, outcome="PASS"), repo, "OUTCOME_NOT_DERIVED", "INVALID")
        self.assertNotValid(self.receipt_for(repo, ev, outcome="FAIL"), repo, "OUTCOME_NOT_DERIVED", "INVALID")

    def test_a_product_failure_after_a_sound_run_is_a_verified_fail_without_an_evidence_manifest(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("pf", source_frames=False, exact_summary=real_failure_summary("CPU_FALLBACK_DETECTED", gpuReconReadbackFrames=888, cpuFrames=12),
                           token="CPU_FALLBACK_DETECTED", exit_code=14)
        (ev / "evidence-manifest.json").unlink()   # the job writes none on a product-failure terminal
        self.assertAdvisory(self.receipt_for(repo, ev, outcome="FAIL"), repo)
        # ... but a capture without its manifest is INCOMPLETE, never a PASS
        cap = self.evidence("cap")
        receipt = self.receipt_for(repo, cap)
        (cap / "evidence-manifest.json").unlink()
        receipt["evidence"]["evidenceManifestSha256"] = None
        self.assertNotValid(receipt, repo, "evidence-manifest.json")   # incomplete (and its stored manifest claims no longer re-derive)

    def test_the_evidence_dir_argument_wins_and_the_receipts_own_path_is_only_a_hint(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("hint")
        receipt = self.receipt_for(repo, ev)
        receipt["evidence"]["localEvidenceDir"] = str(self.tmp / "nowhere")
        self.assertNotValid(receipt, repo, "EVIDENCE_ABSENT", "INCOMPLETE")
        self.assertAdvisory(receipt, repo, evidence_dir=ev)
        good = self.receipt_for(repo, ev)
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertNotValid(good, repo, "EVIDENCE_ABSENT", "INCOMPLETE", evidence_dir=empty)

    # -- item 1: the consent is the COMMITTED blob's records -------------------------------------------------------------------
    def test_sols_repro_a_hand_built_production_pass_over_a_consent_blob_with_zero_records_is_not_valid(self) -> None:
        repo = self.prod_repo(records=[])          # the pinned shape: a real committed consent blob that holds NO record
        ev = self.evidence("repro")
        receipt = self.receipt_for(repo, ev)       # every other claim re-derives, and the admission names the REAL blob ids
        self.assertNotValid(receipt, repo, "CONSENT_NOT_IN_BLOB", "INVALID")

    def test_consent_for_another_venue_or_another_clip_in_the_blob_is_not_this_legs_consent(self) -> None:
        ev = self.evidence("other")
        for name, records in (("the other venue", [consent_record("bachelor", OWNER_CLIP)]), ("another clip", [consent_record("ultra-magnus", OTHER_CLIP)])):
            repo = self.prod_repo(records=records)
            self.assertNotValid(self.receipt_for(repo, ev), repo, "CONSENT_NOT_IN_BLOB", "INVALID")

    def test_placeholder_hashes_and_blobs_that_are_not_the_committed_consent_file_are_not_valid(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("ph")
        receipt = self.receipt_for(repo, ev)
        for key, fake, needle in (("consentBlobSha", "c" * 40, "is not a blob of this repository"), ("venueTableBlobSha", "d" * 40, "is not a blob of this repository"),
                                  ("consentBlobSha", repo.head(), "is not a blob of this repository"),       # a COMMIT id is not a blob
                                  ("consentBlobSha", repo.blob("legs/unit-leg.json"), "not a valid consent file")):   # a real blob that is not the consent file
            forged = json.loads(json.dumps(receipt))
            forged["admission"][key] = fake
            self.assertNotValid(forged, repo, needle, "INVALID")
        for key, fake in (("headCommit", "f" * 40), ("consentBlobSha", "xyz"), ("ownerLineSha256", None)):
            forged = json.loads(json.dumps(receipt))
            forged["admission"][key] = fake
            status, valid, reasons = self.status(forged, repo)
            self.assertFalse(valid, key)
            self.assertEqual(status, "INVALID", key)

    def test_a_consent_blob_that_was_written_to_the_object_store_but_never_committed_is_not_consent(self) -> None:
        repo = self.prod_repo(records=[])
        ev = self.evidence("loose")
        loose = self.tmp / "loose-consent.json"
        loose.write_text(json.dumps(consent_file(consent_record("ultra-magnus", OWNER_CLIP)), indent=2) + "\n", encoding="utf-8", newline="\n")
        blob = repo.git("hash-object", "-w", str(loose))
        receipt = self.receipt_for(repo, ev)
        receipt["admission"]["consentBlobSha"] = blob
        self.assertNotValid(receipt, repo, "is not the one committed at admission.headCommit", "INVALID")

    def test_the_owner_line_the_venue_table_switch_and_the_role_all_come_from_the_committed_blobs(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("blobs")
        receipt = self.receipt_for(repo, ev)
        forged = json.loads(json.dumps(receipt))
        forged["admission"]["ownerLineSha256"] = "a" * 64
        self.assertNotValid(forged, repo, "CONSENT_LINE_MISMATCH", "INVALID")
        forged = json.loads(json.dumps(receipt))
        forged["venue"]["role"] = "acceptance"
        self.assertNotValid(forged, repo, "ROLE_MISMATCH", "INVALID")
        off = self.prod_repo(cleanup_gone=False)
        self.assertNotValid(self.receipt_for(off, ev), off, "cleanup switch off", "INVALID")
        producer = self.prod_repo(records=[dict(consent_record("ultra-magnus", OWNER_CLIP), recordedBy="producer")])
        self.assertNotValid(self.receipt_for(producer, ev), producer, "not a valid consent file", "INVALID")

    def test_a_production_receipt_without_a_repo_to_read_the_blobs_from_is_incomplete_not_valid(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("norepo")
        self.assertNotValid(self.receipt_for(repo, ev), None, "ADMISSION_UNVERIFIABLE", "INCOMPLETE")

    def test_the_leg_spec_and_its_criteria_are_a_committed_blob(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("spec")
        # a spec nobody committed (weaker criteria, same ids) is never found
        weak = self.tmp / "weak-spec.json"
        spec = json.loads(self.spec_path.read_text(encoding="utf-8"))
        spec["criteria"]["supplementary"]["cuda"] = []
        weak.write_text(json.dumps(spec), encoding="utf-8")
        self.assertNotValid(self.receipt_for(repo, ev, spec_path=weak), repo, "LEG_SPEC_NOT_COMMITTED", "INVALID")
        # the COMMITTED criteria decide: a committed spec demanding rows > 5000 makes the same run a derived FAIL
        strict = self.tmp / "strict-spec.json"
        spec["criteria"]["supplementary"]["cuda"] = [{"metric": "rows", "op": "gt", "value": 5000}]
        strict.write_text(json.dumps(spec), encoding="utf-8")
        srepo = self.prod_repo(spec=strict)
        self.assertNotValid(self.receipt_for(srepo, ev), srepo, "OUTCOME_NOT_DERIVED", "INVALID")
        self.assertAdvisory(self.receipt_for(srepo, ev, outcome="FAIL"), srepo)

    # -- item 2: every evidence claim is re-hashed, the playback block is re-derived ---------------------------------------------
    def test_absent_evidence_is_incomplete_never_a_pass(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("abs")
        receipt = self.receipt_for(repo, ev)
        none = json.loads(json.dumps(receipt))
        none["evidence"] = {"umRunOutcome": "RECEIPT"}
        self.assertNotValid(none, repo, "EVIDENCE_ABSENT", "INCOMPLETE")
        for rel in EVIDENCE_REL_FILES:
            dropped = json.loads(json.dumps(receipt))
            dropped["evidence"][EVIDENCE_CLAIM_KEYS[rel]] = None
            # (a capture with no manifest claim is incomplete AND its stored manifest nonce no longer re-derives: either way not valid)
            self.assertNotValid(dropped, repo, "EVIDENCE_ABSENT", None if rel == "evidence-manifest.json" else "INCOMPLETE")
            moved = self.tmp / ("gone-" + rel.replace("/", "_"))
            shutil.copytree(ev, moved)
            (moved / rel).unlink()
            self.assertNotValid(receipt, repo, "is not in the evidence directory", "INCOMPLETE", evidence_dir=moved)

    def test_a_swapped_or_edited_evidence_file_is_invalid(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("hash")
        receipt = self.receipt_for(repo, ev)
        for rel in EVIDENCE_REL_FILES:
            edited = self.tmp / ("edited-" + rel.replace("/", "_"))
            shutil.copytree(ev, edited)
            with open(edited / rel, "ab") as handle:
                handle.write(b"\n")                     # still parses; only the hash can tell
            self.assertNotValid(receipt, repo, "EVIDENCE_HASH_MISMATCH", "INVALID", evidence_dir=edited)

    def test_self_asserted_playback_fields_are_never_inputs(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("lies")
        receipt = self.receipt_for(repo, ev)
        for field, lie in (("sourceAdvanced", 5000), ("requiredSourceFrames", 599), ("nativeFps", 30.0), ("paceFps", 30.0), ("fpsOverride", 1), ("wrapped", True), ("wrapCount", 3),
                           ("expectedRunNonce", "0123456789abcdef0123456789abcdef"), ("observedRunNonce", "0123456789abcdef0123456789abcdef"),
                           ("manifestRunNonce", "0123456789abcdef0123456789abcdef"), ("logSha256", "f" * 64), ("logShaBound", False), ("settingsIsolated", False),
                           ("fixtureRehearsal", True), ("clipId", OTHER_CLIP)):
            forged = json.loads(json.dumps(receipt))
            forged["playback"][field] = lie
            self.assertNotValid(forged, repo, f"playback.{field} is not what the hashed run log and result re-derive", "INVALID")
        # a stored valid / invalidReasons is not believed either
        forged = json.loads(json.dumps(receipt))
        forged["playback"].update(valid=True, invalidReasons=[])
        self.assertAdvisory(forged, repo)   # (they are ignored, not trusted: the verdict comes from the files)

    def test_a_logShaBound_or_settingsIsolated_true_the_evidence_does_not_support_is_invalid(self) -> None:
        repo = self.prod_repo()
        for name, opts, field in (("unbound", dict(declared_log_sha="f" * 64), "logShaBound"), ("notisolated", dict(isolated="venue_NOT_ISOLATED"), "settingsIsolated"),
                                  ("noiso", dict(isolated=None), "settingsIsolated")):
            ev = self.evidence(name, **opts)
            receipt = self.receipt_for(repo, ev)
            self.assertFalse(receipt["playback"][field], name)
            receipt["playback"][field] = True       # the lie
            self.assertNotValid(receipt, repo, f"playback.{field} is not what", "INVALID")
            honest = self.receipt_for(repo, ev)     # even honestly recorded, the run is not evidence
            status, valid, reasons = self.status(honest, repo)
            self.assertFalse(valid, name)

    def test_sols_20_frame_repro_and_a_fabricated_nonce_are_not_valid_even_when_the_receipt_agrees_with_its_evidence(self) -> None:
        repo = self.prod_repo()
        short = self.evidence("short", line={"source_advanced": 20, "required_source_frames": 20, "native_fps": "24.000", "pace_fps": "24.000"})
        self.assertNotValid(self.receipt_for(repo, short), repo, "ceil(20 s x native_fps=24)", "INVALID")
        # fresh GUID-N nonce strings in the receipt, while the hashed log carries another one
        ev = self.evidence("nonce")
        receipt = self.receipt_for(repo, ev)
        fresh = "0123456789abcdef0123456789abcdef"
        receipt["playback"].update(expectedRunNonce=fresh, observedRunNonce=fresh, manifestRunNonce=fresh)
        self.assertNotValid(receipt, repo, "playback.expectedRunNonce is not what", "INVALID")
        foreign = self.evidence("foreign", observed_nonce="0123456789abcdef0123456789abcdef")
        self.assertNotValid(self.receipt_for(repo, foreign), repo, "RECEIPT_NOT_THIS_RUN", "INVALID")

    def test_the_evidence_must_be_about_this_venue_clip_build_and_metrics(self) -> None:
        repo = self.prod_repo()
        wrong_venue = self.evidence("wv", venue="bachelor")
        self.assertNotValid(self.receipt_for(repo, wrong_venue), repo, "VENUE_MISMATCH", "INVALID")
        wrong_clip = self.evidence("wc", manifest={"clipId": OTHER_CLIP})
        self.assertNotValid(self.receipt_for(repo, wrong_clip), repo, "CLIP_MISMATCH", "INVALID")
        wrong_build = self.evidence("wb", manifest={"buildManifest": {"sha256": "cd" * 32}})
        self.assertNotValid(self.receipt_for(repo, wrong_build), repo, "BUILD_MISMATCH", "INVALID")
        ev = self.evidence("wm")
        forged = self.receipt_for(repo, ev)
        forged["metrics"]["rows"] = 123456
        self.assertNotValid(forged, repo, "METRICS_NOT_FROM_EVIDENCE", "INVALID")
        forged = self.receipt_for(repo, ev)
        forged["subject"]["digest"] = "0" * 64
        self.assertNotValid(forged, repo, "SUBJECT_DIGEST_MISMATCH", "INVALID")

    def test_the_outcome_is_derived_from_the_jobs_exit_code_and_result_not_from_the_receipt(self) -> None:
        repo = self.prod_repo()
        nonzero = self.evidence("nz", exit_code=1)
        self.assertNotValid(self.receipt_for(repo, nonzero), repo, "OUTCOME_NOT_DERIVABLE", "INVALID")
        quiescent = self.evidence("q", token="VENUE_NOT_QUIESCENT")
        self.assertNotValid(self.receipt_for(repo, quiescent), repo, "OUTCOME_NOT_DERIVABLE", "INVALID")
        rehearsal = self.evidence("rh", token="FIXTURE_REHEARSAL_CAPTURED")
        self.assertNotValid(self.receipt_for(repo, rehearsal), repo, "OUTCOME_NOT_DERIVABLE", "INVALID")

    def test_a_look_pass_needs_the_contact_sheet_the_receipt_hashed(self) -> None:
        repo = self.prod_repo(leg_type="look")
        ev = self.evidence("look", sheet=True, backend="cpu", leg_type="look")
        self.assertAdvisory(self.receipt_for(repo, ev, leg_type="look", backend="cpu"), repo)
        receipt = self.receipt_for(repo, ev, leg_type="look", backend="cpu")
        receipt["look"]["contactSheet"]["sha256"] = "a" * 64
        self.assertNotValid(receipt, repo, "OUTCOME_NOT_DERIVED", "INVALID")
        # a LOOK leg with no contact sheet in its evidence is a derived FAIL, never a PASS
        nosheet = self.evidence("nosheet", sheet=False, backend="cpu", leg_type="look")
        self.assertAdvisory(self.receipt_for(repo, nosheet, backend="cpu", outcome="FAIL", leg_type="look"), repo)
        self.assertNotValid(self.receipt_for(repo, nosheet, backend="cpu", outcome="PASS", leg_type="look"), repo, "OUTCOME_NOT_DERIVED", "INVALID")

    # -- the offline-to-production promotion of sol's repro --------------------------------------------------------------------
    def test_an_offline_test_receipt_promoted_to_production_is_not_valid(self) -> None:
        self.write_artifacts()
        proc, offline, _ = self.run_leg("ultra-magnus", self.write_spec())
        self.assertEqual(offline["outcome"], "PASS", proc.stdout + proc.stderr)
        self.assertEqual(offline["admission"]["mode"], "offline-test")
        self.assertTrue(ps_receipt_valid(offline, allow_offline=True)[0], "the harness may read its own offline receipt")
        self.assertFalse(ps_receipt_valid(offline)[0], "and nobody else may")
        line = f"CLIP ultra-magnus: {OWNER_CLIP}"
        uncommitted = ProductionRepo(self, records=[consent_record("ultra-magnus", OWNER_CLIP)], cleanup_gone=True)   # consent committed, but NOT the leg spec
        for name, repo, needle in (("sol's repro: the pinned shape, a committed consent blob with zero records", self.prod_repo(records=[]), "CONSENT_NOT_IN_BLOB"),
                                   ("a record for the other venue only", self.prod_repo(records=[consent_record("bachelor", OWNER_CLIP)]), "CONSENT_NOT_IN_BLOB"),
                                   ("a committed record but the offline run's leg spec was never committed", uncommitted, "LEG_SPEC_NOT_COMMITTED")):
            promoted = json.loads(json.dumps(offline))
            promoted["admission"] = {"mode": "production", "consentBlobSha": repo.blob("venue-clip-consent.json"), "venueTableBlobSha": repo.blob("venues.json"),
                                     "headCommit": repo.head(), "consentLastCommit": repo.head(), "ownerLineSha256": hashlib.sha256(line.encode()).hexdigest()}
            status, valid, reasons = self.status(promoted, repo)
            self.assertFalse(valid, name)
            self.assertEqual(status, "INVALID", name)
            self.assertTrue(any(needle in r for r in reasons), f"{name}: {reasons}")

    # -- GIT_* in the environment cannot redirect the blobs ------------------------------------------------------------------------
    def test_a_git_dir_in_the_environment_cannot_point_the_validator_at_a_forged_repo(self) -> None:
        forged_repo = self.prod_repo()                          # the forger's scratch repo: a committed record, the cleanup switch on
        ev = self.evidence("gitenv")
        receipt = self.receipt_for(forged_repo, ev)
        real = self.prod_repo(records=[])                       # the repo the validator is told to use: no record
        env = {"GIT_DIR": str(forged_repo.root / ".git")}
        self.assertNotValid(receipt, real, "is not a blob of this repository", "INVALID", env=env)
        mutated = self.mutated_module([("[void]$psi.Environment.Remove($name)", "$null = $name")])
        status, valid, reasons = self.status(receipt, real, module=mutated, env=env)
        self.assertEqual((status, valid), ("ADVISORY", False), "with the scrub removed GIT_DIR makes the forger's repo the committed one -- so the scrub is guarded")

    # -- the writer runs the same validator (production) ------------------------------------------------------------------------
    def test_the_writer_refuses_a_proofless_pass_and_writes_a_receipt_that_re_derives(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("writer")
        good = self.receipt_for(repo, ev)
        bare = json.loads(json.dumps(good))
        bare["evidence"] = {"umRunOutcome": "RECEIPT"}
        bare["playback"] = None
        invalid = json.loads(json.dumps(bare))
        invalid["outcome"] = "INVALID"
        invalid["receiptId"] = "r-invalid-terminal"     # (receipts are append-only: the good one already took its own id)
        out = self.tmp / "written"
        script = (f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n"
                  "function Try-Write($json) { $r = $json | ConvertFrom-Json -AsHashtable; try { Write-DvReceipt -Receipt $r -ReceiptRoot '" + str(out) + f"' -RepoRoot '{repo.root}' | Out-Null; 'WRITTEN' }} catch {{ 'REFUSED:' + $_.Exception.Message.Split(' ')[0] }} }}\n"
                  "Write-Output ('good=' + (Try-Write $env:DVE_GOOD))\nWrite-Output ('bare=' + (Try-Write $env:DVE_BARE))\nWrite-Output ('invalid=' + (Try-Write $env:DVE_INVALID))\n")
        proc = run_pwsh(["-Command", script], env_extra={"DVE_GOOD": json.dumps(good), "DVE_BARE": json.dumps(bare), "DVE_INVALID": json.dumps(invalid)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("good=WRITTEN", proc.stdout)
        self.assertIn("bare=REFUSED:DVE_RECEIPT_INVALID", proc.stdout)
        self.assertIn("invalid=WRITTEN", proc.stdout, "a terminal that carries no signal needs no proof")
        # DVE-PRODUCTION-WRITER-ROUNDTRIP-TEST-1: read back the file the real writer wrote in production mode and judge it as a reader would
        written = json.loads(next(out.rglob(good["receiptId"] + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(written["verification"]["status"], "ADVISORY")
        self.assertEqual(written["verification"]["venueAnchor"], "ABSENT")
        self.assertTrue(written["outcomeDetail"].startswith("ADVISORY (VENUE_ANCHOR_ABSENT; never a usable PASS)"), written["outcomeDetail"])
        self.assertTrue(any(u.startswith("CLIP_CONTENT_UNBOUND") for u in written["verification"]["unbound"]))
        status, valid, reasons = self.status(written, repo)
        self.assertEqual((status, valid), ("ADVISORY", False), "the receipt the production writer wrote re-derives, and is advisory: " + str(reasons))

    def test_the_reader_validator_refuses_a_bare_pass_and_asks_no_proof_of_other_outcomes(self) -> None:
        repo = self.prod_repo()
        bare = {"outcome": "PASS", "subject": {"clipId": OWNER_CLIP, "clipContentSha256": CLIP_CONTENT_SHA}, "playback": None, "evidence": {"umRunOutcome": "RECEIPT"}}
        status, valid, reasons = self.status(bare, repo)
        self.assertEqual((status, valid), ("INVALID", False))
        self.assertTrue(any("EVIDENCE_ABSENT" in r for r in reasons), reasons)
        self.assertEqual(self.status(dict(bare, outcome="UNRESOLVED"), repo)[:2], ("NO_SIGNAL", True))

    def test_the_outcome_must_be_a_typed_outcome_spelled_exactly(self) -> None:
        repo = self.prod_repo()
        receipt = self.receipt_for(repo, self.evidence("case"))
        for spelling in ("pass", "Pass", "PASS ", "", "OK"):
            forged = json.loads(json.dumps(receipt))
            forged["outcome"] = spelling
            status, valid, reasons = self.status(forged, repo)
            self.assertEqual((status, valid), ("INVALID", False), repr(spelling))

    def test_an_offline_test_receipt_is_never_reported_as_verified(self) -> None:
        self.write_artifacts()
        _, offline, _ = self.run_leg("ultra-magnus", self.write_spec())
        status, valid, _ = ps_receipt_status(offline, allow_offline=True)
        self.assertEqual((status, valid), ("VERIFIED_OFFLINE_TEST", True))

    # -- item 3: every reader calls the evidence-bearing validator ------------------------------------------------------------------
    @staticmethod
    def readers_without_repo_root(texts: dict[str, str]) -> list[str]:
        bad = []
        for name, text in texts.items():
            for n, line in enumerate(text.splitlines(), 1):
                if re.search(r"Test-DvReceiptValid\s+-", line) and not line.lstrip().startswith("#") and "-RepoRoot" not in line:
                    bad.append(f"{name}:{n}")
        return bad

    def test_every_in_tree_caller_of_the_validator_passes_the_repo_root_and_the_only_receipt_readers_are_the_known_ones(self) -> None:
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "*.ps1", "*.psm1", "*.py"], capture_output=True, text=True, check=True).stdout.split()
        texts = {name: (ROOT / name).read_text(encoding="utf-8", errors="replace") for name in tracked if (ROOT / name).is_file()}
        callers = {n for n, t in texts.items() if re.search(r"Test-DvReceiptValid\s+-", t) and n != "tools/repo_hygiene/test_dual_venue_evidence.py"}
        # LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1 added New-VenueFlavorPair.ps1 (the Classic | Cinematic pair), which validates both receipts the same way.
        self.assertEqual(callers, {"tools/profiling/dual-venue/DualVenueRunner.psm1", "tools/profiling/dual-venue/New-VenueSheetPair.ps1",
                                   "tools/profiling/dual-venue/New-VenueFlavorPair.ps1", "tools/profiling/dual-venue/Invoke-VenueBaseline.ps1"})
        self.assertEqual(self.readers_without_repo_root({n: texts[n] for n in callers}), [])
        # nothing else in the tree reads a receipt file: a new reader must call the validator (and be added to this list in review)
        readers = {n for n, t in texts.items() if ("dual-venue-receipt/v1" in t or "dual-venue\\receipts" in t or "dual-venue/receipts" in t)
                   # (test files hold receipt-shaped fixtures they never read back; test_venue_baseline.py's stub chain writes one for the baseline runner, which IS a caller above)
                   and n not in ("tools/repo_hygiene/test_dual_venue_evidence.py", "tools/repo_hygiene/test_mlv_never_authorized.py", "tools/repo_hygiene/test_venue_baseline.py")}
        self.assertEqual(readers, {"tools/profiling/dual-venue/DualVenueRunner.psm1", "tools/profiling/dual-venue/Invoke-VenueLeg.ps1"},
                         "an unknown reader of dual-venue receipts must call Test-DvReceiptValid -RepoRoot and be named here")
        mutated = texts["tools/profiling/dual-venue/New-VenueSheetPair.ps1"].replace("Test-DvReceiptValid -Receipt $r -RepoRoot $repoRoot", "Test-DvReceiptValid -Receipt $r")
        self.assertNotEqual(mutated, texts["tools/profiling/dual-venue/New-VenueSheetPair.ps1"])
        self.assertEqual(self.readers_without_repo_root({"New-VenueSheetPair.ps1": mutated}), ["New-VenueSheetPair.ps1:" + str(next(
            i for i, l in enumerate(mutated.splitlines(), 1) if "Test-DvReceiptValid" in l and not l.lstrip().startswith("#")))],
            "a reader that drops -RepoRoot is caught by the scan -- so the scan guards item 3")

    # -- mutations: each rule's test can fail ----------------------------------------------------------------------------------------
    def test_mutation_trusting_the_consent_blobs_format_a_zero_record_blob_validates(self) -> None:
        stub = "$consent = [pscustomobject]@{ ok = $true; reason = $null; records = @([pscustomobject]@{ venue = $venueName; clipId = $clipId; ownerLineSha256 = $lineSha; recordedBy = 'owner' }) }"
        mutated = self.mutated_module([("$consent = Read-DvClipConsent -Text $cb.text -Table $table", stub)])
        repo = self.prod_repo(records=[])
        receipt = self.receipt_for(repo, self.evidence("m1"))
        self.assertEqual(self.status(receipt, repo, module=mutated)[:2], ("ADVISORY", False), "with the records read skipped a zero-record blob validates -- so reading them is what refuses it")

    def test_mutation_without_the_owner_line_comparison_a_foreign_line_hash_validates(self) -> None:
        mutated = self.mutated_module([("if ([string]$record.ownerLineSha256 -cne $lineSha) {", "if ($false) {")])
        repo = self.prod_repo()
        receipt = self.receipt_for(repo, self.evidence("m2"))
        receipt["admission"]["ownerLineSha256"] = "a" * 64
        self.assertEqual(self.status(receipt, repo, module=mutated)[:2], ("ADVISORY", False))

    def test_mutation_without_the_committed_at_head_check_a_loose_consent_blob_validates(self) -> None:
        # (a loose blob is refused twice over: it is not the blob committed at headCommit, AND HEAD no longer holds its record -- both are taken out)
        mutated = self.mutated_module([("elseif ($at -cne $pair[2]) {", "elseif ($false) {"), ("if (-not $stillConsented) {", "if ($false) {")])
        repo = self.prod_repo(records=[])
        loose = self.tmp / "loose2.json"
        loose.write_text(json.dumps(consent_file(consent_record("ultra-magnus", OWNER_CLIP)), indent=2) + "\n", encoding="utf-8", newline="\n")
        receipt = self.receipt_for(repo, self.evidence("m3"))
        receipt["admission"]["consentBlobSha"] = repo.git("hash-object", "-w", str(loose))
        self.assertEqual(self.status(receipt, repo, module=mutated)[:2], ("ADVISORY", False))

    def test_mutation_without_the_leg_spec_lookup_an_uncommitted_spec_validates(self) -> None:
        mutated = self.mutated_module([("$invalid.Add('LEG_SPEC_NOT_COMMITTED: subject.legSpecSha256 is not a leg spec", "$null = ('LEG_SPEC_NOT_COMMITTED: subject.legSpecSha256 is not a leg spec")])
        repo = self.prod_repo()
        weak = self.tmp / "weak2.json"
        spec = json.loads(self.spec_path.read_text(encoding="utf-8"))
        spec["criteria"]["supplementary"]["cuda"] = []
        weak.write_text(json.dumps(spec), encoding="utf-8")
        receipt = self.receipt_for(repo, self.evidence("m4"), spec_path=weak)
        self.assertEqual(self.status(receipt, repo, module=mutated)[:2], ("ADVISORY", False))

    def test_mutation_without_the_evidence_hash_recheck_an_edited_file_validates(self) -> None:
        mutated = self.mutated_module([("if ((Get-DvSha256OfBytes $b) -cne $claim) {", "if ($false) {")])
        repo = self.prod_repo()
        ev = self.evidence("m5")
        receipt = self.receipt_for(repo, ev)
        edited = self.tmp / "m5-edited"
        shutil.copytree(ev, edited)
        with open(edited / "summary.json", "ab") as handle:
            handle.write(b"\n")
        self.assertEqual(self.status(receipt, repo, evidence_dir=edited, module=mutated)[:2], ("ADVISORY", False))

    def test_mutation_without_the_playback_comparison_a_lying_block_validates(self) -> None:
        mutated = self.mutated_module([("if (-not (Test-DvJsonEquivalent (Get-DvProp $stored $f) (Get-DvProp $derived $f))) {", "if ($false) {")])
        repo = self.prod_repo()
        receipt = self.receipt_for(repo, self.evidence("m6"))
        receipt["playback"]["sourceAdvanced"] = 5000
        self.assertEqual(self.status(receipt, repo, module=mutated)[:2], ("ADVISORY", False))

    def test_mutation_without_the_outcome_derivation_a_pass_over_failing_criteria_validates(self) -> None:
        mutated = self.mutated_module([("if ($expected -cne $outcome) {", "if ($false) {")])
        repo = self.prod_repo()
        receipt = self.receipt_for(repo, self.evidence("m7", summary={"rows": 0}), outcome="PASS")
        self.assertEqual(self.status(receipt, repo, module=mutated)[:2], ("ADVISORY", False))

    def test_mutation_without_the_absent_evidence_rule_a_receipt_with_no_evidence_is_verified(self) -> None:
        mutated = self.mutated_module([("$incomplete.Add('EVIDENCE_ABSENT: the receipt names no local evidence directory", "$null = ('EVIDENCE_ABSENT: the receipt names no local evidence directory")])
        repo = self.prod_repo()
        receipt = self.receipt_for(repo, self.evidence("m8"))
        receipt["evidence"] = {"umRunOutcome": "RECEIPT"}
        receipt["playback"] = None
        self.assertEqual(self.status(receipt, repo, module=mutated)[:2], ("ADVISORY", False))


# ---------------------------------------------------------------------------------------------------
@requires_windows_pwsh
class SheetPairStaysLocalTests(EvidenceFactory, unittest.TestCase):
    """Every leg now plays an owner clip, so a paired sheet is a sheet of OWNER footage: it is written only under a .claude-state
    directory and only from receipts that are themselves valid, evidence-bearing receipts (the reader calls the SAME validator the
    writer does, so a receipt whose evidence is absent is INCOMPLETE and cannot be paired)."""

    def setUp(self) -> None:
        self.make_harness()
        self.repo = self.prod_repo(leg_type="look")
        self.receipts_by_backend = self.build_pair_receipts("pair", real_images=False)

    def build_pair_receipts(self, tag: str, real_images: bool, cuda_path_for=None) -> dict[str, dict]:
        """Both backends' LOOK receipts over evidence whose raw frames are hash-listed in contact-frames.json BEFORE the receipt names it.
        `cuda_path_for(i)` overrides the `path` the cuda side's sidecar i carries (the hash-listed sidecar is what the manifest lists, edited or not)."""
        out: dict[str, dict] = {}
        for backend, colour in (("cuda", (200, 40, 40)), ("cpu", (40, 40, 200))):
            frames = None
            if real_images:
                from PIL import Image
                frames = {}
                for i in (0, 1):
                    buf = io.BytesIO()
                    Image.new("RGB", (64, 36), colour).save(buf, format="PNG")
                    frames[f"frame-{i:02d}.png"] = buf.getvalue()
                    frames[f"frame-{i:02d}.json"] = json.dumps({"index": i, "saved": True, "display_frame": i * 3, "elapsed_ms": i * 40.0,
                                                                "path": (cuda_path_for(i) if cuda_path_for and backend == "cuda" else f"frame-{i:02d}.png"),
                                                                "look_assist_enabled": True, "look_assist_scene": "night"}).encode("utf-8")
            ev = self.evidence(f"{tag}-{backend}", sheet=True, backend=backend, leg_type="look", raw_frames=frames)
            out[backend] = self.receipt_for(self.repo, ev, backend=backend, leg_type="look")
        return out

    def pair(self, out: Path, receipts: dict[str, dict] | None = None, script: Path | None = None):
        paths = []
        for backend in ("cuda", "cpu"):
            path = self.tmp / f"{backend}.json"
            path.write_text(json.dumps((receipts or self.receipts_by_backend)[backend]), encoding="utf-8")
            paths.append(path)
        return run_pwsh(["-File", str(script or self.repo.dv / "New-VenueSheetPair.ps1"), "-CudaReceipt", str(paths[0]), "-CpuReceipt", str(paths[1]), "-OutDir", str(out)])

    def test_a_sheet_outside_a_claude_state_directory_is_refused(self) -> None:
        proc = self.pair(self.tmp / "published")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_OWNER_SHEET_MUST_STAY_LOCAL", proc.stdout + proc.stderr)
        self.assertFalse((self.tmp / "published").exists(), "nothing is written for a refused pair")

    def test_a_receipt_whose_evidence_is_absent_is_incomplete_and_cannot_be_paired(self) -> None:
        bare = json.loads(json.dumps(self.receipts_by_backend))
        bare["cuda"]["evidence"] = {"umRunOutcome": "RECEIPT"}
        proc = self.pair(self.tmp / ".claude-state" / "sheets", receipts=bare)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_RECEIPT_INCOMPLETE", proc.stdout + proc.stderr)
        self.assertIn("EVIDENCE_ABSENT", proc.stdout + proc.stderr)
        gone = json.loads(json.dumps(self.receipts_by_backend))
        gone["cpu"]["evidence"]["localEvidenceDir"] = str(self.tmp / "nowhere")
        proc = self.pair(self.tmp / ".claude-state" / "sheets", receipts=gone)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_RECEIPT_INCOMPLETE", proc.stdout + proc.stderr)
        self.assertIn("EVIDENCE_ABSENT", proc.stdout + proc.stderr)

    def test_a_hand_built_receipt_with_placeholder_hashes_cannot_be_paired(self) -> None:
        hand = {b: {"receiptId": f"r-{b}", "card": "DUAL-VENUE-EVIDENCE-1", "legId": "unit-leg", "outcome": "PASS",
                    "subject": {"backend": b, "buildManifestSha256": "ab" * 32, "legSpecSha256": "cd" * 32, "clipId": OWNER_CLIP, "clipContentSha256": CLIP_CONTENT_SHA, "lookFlavor": "classic"},
                    "venue": {"name": "ultra-magnus", "hostName": "ULTRA-MAGNUS", "gpuNames": ["RTX 4090"]}, "evidence": {"umRunOutcome": "RECEIPT"},
                    "admission": {"mode": "production", "consentBlobSha": "c" * 40, "venueTableBlobSha": "d" * 40, "ownerLineSha256": "e" * 64},
                    "look": {"contactSheet": {"rawFramesDir": str(self.tmp)}}, "playback": good_block()} for b in ("cuda", "cpu")}
        proc = self.pair(self.tmp / ".claude-state" / "sheets", receipts=hand)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_RECEIPT_INVALID", proc.stdout + proc.stderr)

    def test_a_tampered_evidence_file_cannot_be_paired(self) -> None:
        ev = Path(self.receipts_by_backend["cuda"]["evidence"]["localEvidenceDir"])
        with open(ev / "logs" / "smoke-run.log", "ab") as handle:
            handle.write(b"\n")
        proc = self.pair(self.tmp / ".claude-state" / "sheets")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_RECEIPT_INVALID", proc.stdout + proc.stderr)
        self.assertIn("EVIDENCE_HASH_MISMATCH", proc.stdout + proc.stderr)

    def test_a_fixture_is_not_a_consented_clip_for_a_sheet(self) -> None:
        for clip in FIXTURE_IDS:
            forged = json.loads(json.dumps(self.receipts_by_backend))
            for r in forged.values():
                r["subject"]["clipId"] = clip
            proc = self.pair(self.tmp / ".claude-state" / "sheets", receipts=forged)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("consented clip id", proc.stdout + proc.stderr)

    def test_a_valid_pair_under_claude_state_is_composed_and_marked_local(self) -> None:
        try:
            import PIL, numpy  # noqa: F401
        except ImportError:
            self.skipTest("Pillow + numpy are required")
        self.receipts_by_backend = self.build_pair_receipts("real", real_images=True)
        out = self.tmp / ".claude-state" / "sheets"
        proc = self.pair(out)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        record_path = next(out.glob("sheet-pair-*.json"))
        record = json.loads(record_path.read_text(encoding="utf-8"))
        # DVE-SCALE2-LOOK-LEG-1 r2 (fable H4): the pair's files carry the leg id (two Classic look legs share one venue and flavor), and the record
        # says the scale each side asked for and rendered at (a pair of unequal scales is a scale difference, not a backend difference).
        self.assertEqual(record_path.name, "sheet-pair-unit-leg-ultra-magnus-classic.json")
        self.assertEqual(sorted(p.name for p in out.glob("sheet-*.png")), ["sheet-unit-leg-ultra-magnus-cuda-vs-cpu-classic.png"])
        self.assertEqual(record["cudaScale"], {"requestedScale": 4, "effectiveScale": 4})
        self.assertEqual(record["cpuScale"], {"requestedScale": 4, "effectiveScale": 4})
        self.assertFalse(record["scalesDiffer"])
        self.assertTrue(record["ownerFootage"])
        self.assertTrue(record["advisory"], "production receipts are advisory, so the pair is a diagnostic sheet")
        self.assertEqual(record["evidenceStatus"], "ADVISORY")
        self.assertIn("subject.clipContentSha256", record["unbound"])
        self.assertIn("never committed", record["localOnly"])
        self.assertIsNone(record["owner_verdict"])
        self.assertEqual(record["model_verdicts"], [])

    def test_the_pair_records_the_flavor_honoured_only_when_both_legs_say_so(self) -> None:
        # LOOK-ASSIST-FLAVORS-1: true only when BOTH legs' receipts say the app applied the requested flavor; false when either
        # says it did not; 'unknown' for a receipt that predates the app's report.
        try:
            import PIL, numpy  # noqa: F401
        except ImportError:
            self.skipTest("Pillow + numpy are required")
        self.receipts_by_backend = self.build_pair_receipts("flavor", real_images=True)
        cases = ((True, True, True), (True, False, False), (False, True, False), ("unknown", "unknown", "unknown"), (True, "unknown", "unknown"))
        for n, (cuda_honored, cpu_honored, expected) in enumerate(cases):
            with self.subTest(cuda=cuda_honored, cpu=cpu_honored):
                receipts = json.loads(json.dumps(self.receipts_by_backend))
                receipts["cuda"]["look"]["lookFlavorHonored"] = cuda_honored
                receipts["cpu"]["look"]["lookFlavorHonored"] = cpu_honored
                out = self.tmp / ".claude-state" / f"sheets-flavor-{n}"
                proc = self.pair(out, receipts=receipts)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                record = json.loads(next(out.glob("sheet-pair-*.json")).read_text(encoding="utf-8"))
                self.assertEqual(record["lookFlavor"], "classic")
                self.assertEqual(record["lookFlavorHonored"], expected)

    def test_an_unlisted_or_replaced_raw_frame_cannot_be_paired(self) -> None:
        # sol r1 B4: the reader composed whatever PNGs sat under the evidence directory; it now takes only what the hashed manifest lists
        raw = Path(self.receipts_by_backend["cuda"]["evidence"]["localEvidenceDir"]) / "contact-sheet" / "raw"
        (raw / "extra.png").write_bytes(b"\x89PNG\r\n\x1a\nunrelated")
        proc = self.pair(self.tmp / ".claude-state" / "sheets")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CONTACT_FRAME_UNLISTED", proc.stdout + proc.stderr)
        (raw / "extra.png").unlink()
        (raw / "frame-00.png").write_bytes(b"\x89PNG\r\n\x1a\nswapped for an unrelated image")   # same name, other bytes (the sol repro)
        proc = self.pair(self.tmp / ".claude-state" / "sheets")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("CONTACT_FRAME_HASH_MISMATCH", proc.stdout + proc.stderr)
        self.assertFalse((self.tmp / ".claude-state" / "sheets").exists(), "nothing is written for a refused pair")

    # -- DUAL-VENUE-EVIDENCE-3 (sol r2 blocker, PR #223): a sheet shows ONLY frames this run captured and the manifest lists ---------------------
    # sol's repro: a hash-listed sidecar whose `path` names a PNG OUTSIDE staging. The listed PNG is untouched, no listed hash changes, and yet the
    # composer opened the external file -- so an edited or stale external PNG was composed under the receipt's backend labels.
    @staticmethod
    def need_imaging() -> None:
        try:
            import PIL, numpy  # noqa: F401
        except ImportError:
            raise unittest.SkipTest("Pillow + numpy are required")

    def external_png(self, path: Path, colour: tuple[int, int, int]) -> Path:
        from PIL import Image
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 36), colour).save(path)
        return path

    def pair_with_cuda_sidecar_path(self, tag: str, path_for, out_name: str = "sheets"):
        receipts = self.build_pair_receipts(tag, real_images=True, cuda_path_for=path_for)
        out = self.tmp / ".claude-state" / out_name
        return out, self.pair(out, receipts=receipts)

    def test_a_hash_listed_sidecar_that_names_an_absolute_png_outside_staging_is_refused(self) -> None:
        self.need_imaging()
        for luma in (20, 220):   # sol's repro: only the external PNG's pixels change; no listed artifact does
            external = self.external_png(self.tmp / "legacy-captures" / "frame-00.png", (luma, luma, luma))
            out, proc = self.pair_with_cuda_sidecar_path(f"abs-{luma}", lambda i, p=str(external): p if i == 0 else f"frame-{i:02d}.png", f"sheets-abs-{luma}")
            self.assertNotEqual(proc.returncode, 0, f"an external PNG (luma {luma}) was composed under the receipt's backend labels: {proc.stdout}{proc.stderr}")
            self.assertIn("CONTACT_FRAME_SIDECAR_PATH", proc.stdout + proc.stderr)
            self.assertFalse(out.exists(), "nothing is written for a refused pair")

    def test_a_hash_listed_sidecar_with_a_parent_relative_path_is_refused(self) -> None:
        self.need_imaging()
        out = self.tmp / ".claude-state" / "sheets-rel"
        # the staging dir is <out>\.pair-staging\<guid>\cuda, so three levels up is <out> itself: an external PNG a relative path can reach
        self.external_png(out / "ext-00.png", (30, 30, 30))
        for n, ref in enumerate(("../../../ext-00.png", "..\\..\\..\\ext-00.png", "sub/../../../../ext-00.png")):
            receipts = self.build_pair_receipts(f"rel{n}", real_images=True, cuda_path_for=lambda i, r=ref: r if i == 0 else f"frame-{i:02d}.png")
            proc = self.pair(out, receipts=receipts)
            self.assertNotEqual(proc.returncode, 0, f"{ref!r} reached an external PNG: {proc.stdout}{proc.stderr}")
            self.assertIn("CONTACT_FRAME_SIDECAR_PATH", proc.stdout + proc.stderr)
            self.assertEqual(list(out.glob("sheet-*")), [], "no sheet is written for a refused pair")

    def test_a_sidecar_may_only_name_its_own_listed_png(self) -> None:
        self.need_imaging()
        for n, ref in enumerate(("frame-01.png", "frame-00.PNG", "./frame-00.png", "raw/frame-00.png", "C:frame-00.png", "\\\\host\\share\\frame-00.png")):
            out, proc = self.pair_with_cuda_sidecar_path(f"own{n}", lambda i, r=ref: r if i == 0 else f"frame-{i:02d}.png", f"sheets-own{n}")
            self.assertNotEqual(proc.returncode, 0, f"{ref!r} was accepted as the image of frame-00.json: {proc.stdout}{proc.stderr}")
            self.assertIn("CONTACT_FRAME_SIDECAR_PATH", proc.stdout + proc.stderr)

    def test_the_composer_refuses_what_the_reader_missed_and_the_reader_what_the_composer_missed(self) -> None:
        # two layers, each enough alone: the reader (Read-DvContactFrames) refuses at validation; the composer refuses at read time. Take one
        # layer out and the pair is still refused (by the other); take BOTH out and the guard-less pair composes.
        self.need_imaging()
        external = self.external_png(self.tmp / "legacy-captures" / "frame-00.png", (90, 90, 90))
        path_for = lambda i, p=str(external): p if i == 0 else f"frame-{i:02d}.png"   # noqa: E731
        reader = self.repo.dv / "DualVenueRunner.psm1"
        composer = self.repo.root / "tools" / "profiling" / "make-contact-sheet.py"
        reader_text, composer_text = reader.read_text(encoding="utf-8"), composer.read_text(encoding="utf-8")
        reader_anchor = "if ($refPath -cne ($stem + '.png')) {"
        composer_anchor = 'if raw not in ("", None) and raw != own:'
        self.assertEqual(reader_text.count(reader_anchor), 1)
        self.assertEqual(composer_text.count(composer_anchor), 1)
        reader.write_text(reader_text.replace(reader_anchor, "if ($false) {"), encoding="utf-8")
        _, proc = self.pair_with_cuda_sidecar_path("lay1", path_for, "sheets-l1")
        self.assertNotEqual(proc.returncode, 0, "the composer alone refuses the external reference: " + proc.stdout + proc.stderr)
        self.assertIn("PAIR_SIDECAR_PATH_OUTSIDE_STAGING", proc.stdout + proc.stderr)
        reader.write_text(reader_text, encoding="utf-8")
        composer.write_text(composer_text.replace(composer_anchor, "if False:"), encoding="utf-8")
        _, proc = self.pair_with_cuda_sidecar_path("lay2", path_for, "sheets-l2")
        self.assertNotEqual(proc.returncode, 0, "the reader alone refuses the external reference: " + proc.stdout + proc.stderr)
        self.assertIn("CONTACT_FRAME_SIDECAR_PATH", proc.stdout + proc.stderr)
        reader.write_text(reader_text.replace(reader_anchor, "if ($false) {"), encoding="utf-8")
        out, proc = self.pair_with_cuda_sidecar_path("lay3", path_for, "sheets-l3")
        self.assertEqual(proc.returncode, 0, "with both guards taken out the reference is followed -- so the two guards are what refuse it: " + proc.stdout + proc.stderr)

    def test_the_pair_script_hands_the_composer_the_hashes_it_must_verify_at_read_time(self) -> None:
        text = (DV / "New-VenueSheetPair.ps1").read_text(encoding="utf-8")
        for needle in ("'--left-listed'", "'--right-listed'"):
            self.assertIn(needle, text)
        composer = COMPOSER.read_text(encoding="utf-8")
        self.assertIn("--left-listed", composer)
        self.assertIn("--right-listed", composer)

    def test_a_speed_leg_cannot_be_paired_as_a_sheet(self) -> None:
        speed_repo = self.prod_repo(leg_type="speed")
        speed_spec = self.spec_path
        receipts = {b: self.receipt_for(speed_repo, self.evidence(f"spd-{b}", backend=b), backend=b, spec_path=speed_spec) for b in ("cuda", "cpu")}
        proc = self.pair(self.tmp / ".claude-state" / "sheets", receipts=receipts, script=speed_repo.dv / "New-VenueSheetPair.ps1")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_NOT_A_LOOK_LEG", proc.stdout + proc.stderr)

    def test_a_relabelled_backend_cannot_be_paired_against_its_own_frames(self) -> None:
        # fable r1 B1: a cuda receipt copied with subject.backend = cpu (digest recomputed) verified, and the pair then composed one set of frames as 'cuda | cpu'
        relabelled = json.loads(json.dumps(self.receipts_by_backend))
        twin = json.loads(json.dumps(relabelled["cuda"]))
        twin["subject"]["backend"] = "cpu"
        relabelled["cpu"] = redigest(twin)
        proc = self.pair(self.tmp / ".claude-state" / "sheets", receipts=relabelled)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_RECEIPT_INVALID", proc.stdout + proc.stderr)
        self.assertIn("BACKEND_MISMATCH", proc.stdout + proc.stderr)

    def test_the_unbound_clip_content_claim_is_neither_trusted_nor_a_pairing_input(self) -> None:
        try:
            import PIL, numpy  # noqa: F401
        except ImportError:
            self.skipTest("Pillow + numpy are required")
        receipts = self.build_pair_receipts("unbound", real_images=True)
        receipts["cpu"]["subject"]["clipContentSha256"] = "b" * 64    # a claim no hashed artifact carries
        redigest(receipts["cpu"])
        out = self.tmp / ".claude-state" / "sheets"
        proc = self.pair(out, receipts=receipts)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        record = json.loads(next(out.glob("sheet-pair-*.json")).read_text(encoding="utf-8"))
        self.assertIn("subject.clipContentSha256", record["unbound"])
        self.assertNotIn("hostName", json.dumps(record["sheet"]), "the sheet carries no self-asserted host")

    def test_mutation_without_the_validity_gate_a_receipt_with_no_evidence_is_paired(self) -> None:
        # the mutated reader sits BESIDE the real one (same repo, same committed history), so only the validity gate differs
        anchor = "if ($validity.status -cne 'ADVISORY') {"
        text = (self.repo.dv / "New-VenueSheetPair.ps1").read_text(encoding="utf-8")
        self.assertEqual(text.count(anchor), 1)
        mutated_script = self.repo.dv / "New-VenueSheetPair.mutated.ps1"
        mutated_script.write_text(text.replace(anchor, "if ($false) {"), encoding="utf-8")
        try:
            import PIL, numpy  # noqa: F401
        except ImportError:
            self.skipTest("Pillow + numpy are required")
        bare = json.loads(json.dumps(self.build_pair_receipts("mut", real_images=True)))
        for r in bare.values():
            r["evidence"] = dict(r["evidence"], summaryJsonSha256=None)   # the evidence is incomplete, but its frames are there
        refused = self.pair(self.tmp / ".claude-state" / "sheets-real", receipts=bare)
        self.assertNotEqual(refused.returncode, 0, "the real reader refuses an incomplete receipt")
        self.assertIn("PAIR_RECEIPT_INCOMPLETE", refused.stdout + refused.stderr)
        proc = self.pair(self.tmp / ".claude-state" / "sheets-mut", receipts=bare, script=mutated_script)
        self.assertEqual(proc.returncode, 0, "with the gate removed the same receipt is paired -- so the reader's validity call is what refuses it: " + proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------------------------------
# DUAL-VENUE-EVIDENCE-2 round 2 (formal keys r1 of PR #223; hub ruling 2026-10-02). DECLARED THREAT MODEL, forward-only:
#   IN scope   our own tools, mislabelling, legacy / other-lane receipts, wrong-leg or wrong-backend pairing, edited or stale receipts, line endings;
#   OUT of scope (accepted, shown to the owner)   a deliberate forger who writes a whole matching evidence set -- there is no venue-held anchor
#   (DualVenueRunner.psm1: "there is no venue-side signature"), so PRODUCTION receipts are ADVISORY and production PASS verification moves to
#   DUAL-VENUE-PASS-PROVENANCE-1 (design first: a venue-signed summary.json, or a re-read of \\<venue>\mlv-agent\outbox by jobId).
@requires_windows_pwsh
class NarrowedProductionReceiptsAndBoundBackendTests(EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    def setUp(self) -> None:
        self.make_harness()

    def relabel(self, receipt: dict, backend: str) -> dict:
        forged = json.loads(json.dumps(receipt))
        forged["subject"]["backend"] = backend
        return redigest(forged)

    # -- item 1: the narrowing ----------------------------------------------------------------------------------------------------------------
    def test_a_production_receipt_is_advisory_never_verified_and_never_valid(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("adv")
        bad = self.evidence("advf", summary={"rows": 0})
        sound = self.receipt_for(repo, ev)
        got = self.status_batch(repo, [(sound, None), (self.receipt_for(repo, bad, outcome="FAIL"), None),
                                       (self.receipt_for(repo, ev, outcome="FAIL"), None), (dict(sound, outcome="UNRESOLVED"), None)])
        self.expect(got[0], "ADVISORY", "PASS")
        self.expect(got[1], "ADVISORY", "FAIL")
        # a receipt that does not re-derive is INVALID and carries the same typed reason (a production receipt is never valid, whatever else is wrong)
        self.expect(got[2], "INVALID", "wrong outcome", "OUTCOME_NOT_DERIVED")
        self.assertTrue(any(r.startswith("VENUE_ANCHOR_ABSENT") for r in got[2][2]), got[2][2])
        # outcomes that carry no signal are untouched
        self.expect(got[3], "NO_SIGNAL", "unresolved")

    def test_mutation_re_enabling_production_pass_is_caught_by_the_advisory_pin(self) -> None:
        mutated = self.mutated_module([("$venueAnchorAbsent = $production", "$venueAnchorAbsent = $false")])
        repo = self.prod_repo()
        receipt = self.receipt_for(repo, self.evidence("reenable"))
        # (the same receipt is ADVISORY / valid=false in test_a_production_receipt_is_advisory_never_verified_and_never_valid)
        self.assertEqual(self.status_batch(repo, [(receipt, None)], module=mutated)[0][:2], ("VERIFIED", True),
                         "with the line changed a production PASS verifies -- which the advisory pin refuses")

    def test_no_reader_or_writer_treats_advisory_as_valid(self) -> None:
        text = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        self.assertEqual(text.count("$venueAnchorAbsent = $production"), 1)
        self.assertIn("valid = ($status -in @('VERIFIED', 'VERIFIED_OFFLINE_TEST'))", text, "ADVISORY is not in the valid set")
        pair = (DV / "New-VenueSheetPair.ps1").read_text(encoding="utf-8")
        self.assertIn("$validity.status -cne 'ADVISORY'", pair, "the pair reader accepts exactly ADVISORY (a diagnostic sheet), nothing else")
        docs = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        for needle in ("VENUE_ANCHOR_ABSENT", "ADVISORY", "DUAL-VENUE-PASS-PROVENANCE-1"):
            self.assertIn(needle, docs)

    # -- stale and other-lane receipts (inside the declared model) ------------------------------------------------------------------------------
    def commit_consent(self, repo: ProductionRepo, *records: dict) -> None:
        repo.write_consent(*records)
        repo.git("add", "tools")
        repo.git("commit", "-q", "-m", "consent change")

    def test_a_receipt_whose_consent_was_revoked_or_whose_commit_is_another_lanes_is_not_carried_forward(self) -> None:
        repo = self.prod_repo()
        ev = self.evidence("stale")
        receipt = self.receipt_for(repo, ev)
        self.expect(self.status_batch(repo, [(receipt, None)])[0], "ADVISORY")
        # an unrelated later change to the consent file (another record) leaves this receipt's own record in place
        self.commit_consent(repo, consent_record("ultra-magnus", OWNER_CLIP), consent_record("bachelor", OWNER_CLIP))
        self.expect(self.status_batch(repo, [(receipt, None)])[0], "ADVISORY", "another record added")
        # ... but once the record is gone from HEAD the receipt is stale
        self.commit_consent(repo)
        self.expect(self.status_batch(repo, [(receipt, None)])[0], "INVALID", "revoked", "CONSENT_REVOKED")
        # another lane's receipt: its commit is not part of the history of the checkout that validates it
        other = self.prod_repo()
        foreign = self.receipt_for(other, ev)
        self.expect(self.status_batch(other, [(foreign, None)])[0], "ADVISORY", "same history")
        other.git("checkout", "-q", "--detach", "HEAD~1")
        self.expect(self.status_batch(other, [(foreign, None)])[0], "INVALID", "other lane", "ADMISSION_COMMIT_NOT_IN_HISTORY")

    def blanket_receipt(self, repo: ProductionRepo, ev: Path, **admission) -> dict:
        receipt = self.receipt_for(repo, ev)
        receipt["admission"].update(dict(ownerLineSha256=BLANKET_SENTENCE_SHA256, consentKind="blanket", consentSha256Prefix=BLANKET_SENTENCE_SHA256[:12]), **admission)
        return receipt

    def test_a_blanket_admitted_receipt_re_derives_from_the_committed_blanket_and_is_revoked_with_it(self) -> None:
        repo = self.prod_repo(records=[], blankets=[blanket_record()])
        ev = self.evidence("blanket")
        receipt = self.blanket_receipt(repo, ev)
        self.expect(self.status_batch(repo, [(receipt, None)])[0], "ADVISORY", "blanket in the committed blob")
        # a kind or prefix that the blob does not back is not valid
        self.assertNotValid(self.blanket_receipt(repo, ev, consentSha256Prefix="0" * 12), repo, "consentSha256Prefix is not the prefix", "INVALID")
        self.assertNotValid(self.blanket_receipt(repo, ev, consentKind="whatever"), repo, "consentKind is neither clip nor blanket", "INVALID")
        # a receipt that claims 'clip' over a blob holding only the blanket is not valid (no per-clip record)
        self.assertNotValid(self.blanket_receipt(repo, ev, consentKind="clip", ownerLineSha256=hashlib.sha256(b"CLIP ultra-magnus: " + OWNER_CLIP.encode()).hexdigest(),
                                                 consentSha256Prefix=""), repo, "CONSENT_NOT_IN_BLOB", "INVALID")
        # once the blanket is gone from HEAD the receipt is stale
        self.commit_consent(repo)
        self.expect(self.status_batch(repo, [(receipt, None)])[0], "INVALID", "blanket revoked", "CONSENT_REVOKED")
        # a repo whose blob has no blanket never backs a blanket receipt
        empty = self.prod_repo(records=[])
        self.assertNotValid(self.blanket_receipt(empty, ev), empty, "CONSENT_NOT_IN_BLOB", "INVALID")

    def test_mutation_without_the_history_and_revocation_checks_a_stale_receipt_is_advisory(self) -> None:
        mutated = self.mutated_module([("if ($anc.exitCode -ne 0) {", "if ($false) {"), ("if (-not $stillConsented) {", "if ($false) {")])
        repo = self.prod_repo()
        ev = self.evidence("mstale")
        receipt = self.receipt_for(repo, ev)
        self.commit_consent(repo)
        # (the same two receipts are INVALID -- CONSENT_REVOKED / ADMISSION_COMMIT_NOT_IN_HISTORY -- in the test above)
        self.assertEqual(self.status_batch(repo, [(receipt, None)], module=mutated)[0][:2], ("ADVISORY", False))
        other = self.prod_repo()
        foreign = self.receipt_for(other, ev)
        other.git("checkout", "-q", "--detach", "HEAD~1")
        self.assertEqual(self.status_batch(other, [(foreign, None)], module=mutated)[0][:2], ("ADVISORY", False))

    # -- item 3: the backend is derived from the hashed summary --------------------------------------------------------------------------------
    def test_the_backend_is_derived_from_the_hashed_summary_not_from_the_receipt(self) -> None:
        repo = self.prod_repo()
        cuda_ev = self.evidence("bk-cuda")
        cpu_ev = self.evidence("bk-cpu", backend="cpu")
        lying = self.evidence("bk-lie", summary={"backend": "cpu"})                              # the summary's own field disagrees with its counters
        nofield = self.evidence("bk-nofield", backend="cpu", summary={"backend": None})          # a cpu run must say so
        zero = self.evidence("bk-zero", summary={"gpuFramesTotal": 0, "cpuFrames": 0})           # counters that do not say which backend ran
        junk = self.evidence("bk-junk", summary={"gpuFramesTotal": "n/a"})
        got = self.status_batch(repo, [
            (self.receipt_for(repo, cuda_ev, backend="cuda"), None), (self.receipt_for(repo, cpu_ev, backend="cpu"), None),
            # the fable / sol repro: a genuine cuda run's receipt relabelled cpu, digest recomputed (the speed leg's cpu criteria are empty), and the reverse
            (self.relabel(self.receipt_for(repo, cuda_ev), "cpu"), None), (self.relabel(self.receipt_for(repo, cpu_ev, backend="cpu"), "cuda"), None),
            (self.receipt_for(repo, lying), None), (self.receipt_for(repo, nofield, backend="cpu"), None),
            (self.receipt_for(repo, zero), None), (self.receipt_for(repo, junk), None)])
        self.expect(got[0], "ADVISORY", "cuda")
        self.expect(got[1], "ADVISORY", "cpu")
        self.expect(got[2], "INVALID", "cuda relabelled cpu", "BACKEND_MISMATCH")
        self.expect(got[3], "INVALID", "cpu relabelled cuda", "BACKEND_MISMATCH")
        self.expect(got[4], "INVALID", "summary names another backend", "BACKEND_MISMATCH")
        self.expect(got[5], "INVALID", "cpu run without a backend field", "BACKEND_NOT_DERIVABLE")
        self.expect(got[6], "INVALID", "zero counters", "BACKEND_NOT_DERIVABLE")
        self.expect(got[7], "INVALID", "junk counters", "BACKEND_NOT_DERIVABLE")

    def test_the_criteria_are_selected_by_the_derived_backend(self) -> None:
        spec = json.loads(self.write_spec().read_text(encoding="utf-8"))
        spec["criteria"]["supplementary"]["cpu"] = [{"metric": "rows", "op": "gt", "value": 5000}]
        strict = self.tmp / "strict-cpu-spec.json"
        strict.write_text(json.dumps(spec), encoding="utf-8")
        repo = self.prod_repo(spec=strict)
        cpu_ev = self.evidence("crit-cpu", backend="cpu")          # rows 900: the committed cpu criterion fails
        got = self.status_batch(repo, [(self.receipt_for(repo, cpu_ev, backend="cpu", outcome="PASS"), None), (self.receipt_for(repo, cpu_ev, backend="cpu", outcome="FAIL"), None),
                                       # relabelling the same cpu evidence cuda to dodge that criterion is refused before any criterion is read
                                       (self.relabel(self.receipt_for(repo, cpu_ev, backend="cpu", outcome="PASS"), "cuda"), None)])
        self.expect(got[0], "INVALID", "cpu PASS over a failing cpu criterion", "OUTCOME_NOT_DERIVED")
        self.expect(got[1], "ADVISORY", "cpu FAIL")
        self.expect(got[2], "INVALID", "relabelled to dodge the criterion", "BACKEND_MISMATCH")

    def test_mutation_without_the_backend_derivation_a_relabelled_receipt_is_advisory(self) -> None:
        mutated = self.mutated_module([
            ("elseif ($derivedBackend -cne $backend) {", "elseif ($false) {"),
            ("if ($null -ne $summaryBackend -and [string]$summaryBackend -cne $backend) {", "if ($false) {")])
        repo = self.prod_repo()
        relabelled = self.relabel(self.receipt_for(repo, self.evidence("mbk")), "cpu")     # (INVALID / BACKEND_MISMATCH in the backend test above)
        self.assertEqual(self.status_batch(repo, [(relabelled, None)], module=mutated)[0][:2], ("ADVISORY", False),
                         "with the derivation removed the relabel passes -- so it is what refuses it")

    # -- item 4: the leg identity is bound where an artifact carries it, and UNBOUND (typed) where none does ---------------------------------------
    def test_the_leg_type_look_flavor_and_declared_venue_come_from_the_hashed_summary(self) -> None:
        speed_repo = self.prod_repo(leg_type="speed")
        speed_spec = self.spec_path
        look_repo = self.prod_repo(leg_type="look")
        look_spec = self.spec_path
        speed_ev = self.evidence("id-speed")
        look_ev = self.evidence("id-look", sheet=True, backend="cpu", leg_type="look")
        flavored = self.evidence("id-flavor", sheet=True, backend="cpu", leg_type="look", summary={"lookFlavor": "night"})
        elsewhere = self.evidence("id-venue", summary={"declaredVenue": "bachelor"})
        sp = self.status_batch(speed_repo, [
            (self.receipt_for(speed_repo, speed_ev, spec_path=speed_spec), None),
            # look-run evidence presented as the speed leg (same clip, build and venue)
            (self.receipt_for(speed_repo, look_ev, backend="cpu", spec_path=speed_spec), None),
            (self.receipt_for(speed_repo, elsewhere, spec_path=speed_spec), None)])
        lk = self.status_batch(look_repo, [
            (self.receipt_for(look_repo, look_ev, backend="cpu", leg_type="look", spec_path=look_spec), None),
            # speed-run evidence presented as the look leg
            (self.receipt_for(look_repo, speed_ev, leg_type="look", spec_path=look_spec), None),
            # a look flavor the committed spec does not name
            (self.receipt_for(look_repo, flavored, backend="cpu", leg_type="look", spec_path=look_spec), None)])
        self.expect(sp[0], "ADVISORY", "speed")
        self.expect(sp[1], "INVALID", "look evidence as the speed leg", "LEG_TYPE_MISMATCH")
        self.expect(sp[2], "INVALID", "summary declared for another venue", "VENUE_MISMATCH")
        self.expect(lk[0], "ADVISORY", "look")
        self.expect(lk[1], "INVALID", "speed evidence as the look leg", "LEG_TYPE_MISMATCH")
        self.expect(lk[2], "INVALID", "look flavor", "look flavor")

    def test_the_clip_content_and_leg_id_claims_are_recorded_unbound_and_are_not_verdict_inputs(self) -> None:
        repo = self.prod_repo()
        receipt = self.receipt_for(repo, self.evidence("unb"))
        forged = json.loads(json.dumps(receipt))          # sol r1 B3: the all-'a' content hash with a recomputed digest
        forged["subject"]["clipContentSha256"] = "a" * 64
        redigest(forged)
        wrong_leg = json.loads(json.dumps(receipt))       # a leg id the committed spec does not carry is still refused
        wrong_leg["legId"] = "some-other-leg"
        got = self.status_batch(repo, [(receipt, None), (forged, None), (wrong_leg, None), (dict(receipt, outcome="UNRESOLVED"), None)])
        self.expect(got[0], "ADVISORY", "sound")
        for i in (0, 1):
            self.assertTrue(any(u.startswith("CLIP_CONTENT_UNBOUND") for u in got[i][3]), got[i][3])
            self.assertTrue(any(u.startswith("LEG_IDENTITY_UNBOUND") for u in got[i][3]), got[i][3])
        # the forged content hash changes nothing the verdict reads: it is recorded UNBOUND, never trusted
        self.expect(got[1], "ADVISORY", "forged clip content hash")
        self.expect(got[2], "INVALID", "wrong leg id", "LEG_SPEC_MISMATCH")
        self.assertEqual(got[3][3], [], "a no-signal receipt records nothing unbound (it is not a verdict)")

    def test_mutation_without_the_leg_type_check_look_evidence_verifies_as_the_speed_leg(self) -> None:
        mutated = self.mutated_module([("if ($specLook -ne $runLook) {", "if ($false) {")])
        speed_repo = self.prod_repo(leg_type="speed")
        look_ev = self.evidence("mlt", sheet=True, backend="cpu", leg_type="look")
        receipt = self.receipt_for(speed_repo, look_ev, backend="cpu")     # (INVALID / LEG_TYPE_MISMATCH in the leg-type test above)
        self.assertEqual(self.status_batch(speed_repo, [(receipt, None)], module=mutated)[0][:2], ("ADVISORY", False))


@requires_windows_pwsh
class ProductFailureTerminalsAreReceiptableTests(EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    """DUAL-VENUE-EVIDENCE-3 (sol r2 hardening H1 / fable r2 hardening 1, DVE-REAL-PRODUCT-FAILURE-SUMMARY-CONTRACT-1): the backend of a product-failure
    terminal is derived where the REAL job puts its frame counters (the nested gpuSummary), so the documented advisory FAIL is written, not lost to
    BACKEND_NOT_DERIVABLE / DVE_RECEIPT_WRITE_FAILED. Every summary here is the job's exact shape (real_failure_summary), not the capture defaults plus a result."""

    def setUp(self) -> None:
        self.make_harness()

    TERMINALS = (("GPU_RECON_FRAMES_ZERO", 13, "cuda", {}),
                 ("GPU_RECON_FRAMES_ZERO", 13, "cuda", {"cpuFrames": 900}),                                     # every frame fell to the cpu path
                 ("CPU_FALLBACK_DETECTED", 14, "cuda", {"gpuReconReadbackFrames": 888, "cpuFrames": 12}),
                 ("CPU_FALLBACK_DETECTED", 14, "cuda", {"gpuTextureNoReadbackFrames": 5, "cpuFrames": 1}),       # every counter the job sums counts
                 ("CPU_BACKEND_PATH_MISMATCH", 28, "cpu", {"gpuReconReadbackFrames": 900}),                      # the cpu leg ran the gpu path
                 ("CPU_BACKEND_PATH_MISMATCH", 28, "cpu", {}),                                                   # ... or produced no cpu frame
                 ("PRESENTMON_UNAVAILABLE", 23, "cuda", {"gpuReconReadbackFrames": 900}))                        # top-level gpuFramesTotal AND the nested block

    def failure_evidence(self, name: str, token: str, code: int, backend: str = "cuda", leg_type: str = "speed", **gpu: int) -> Path:
        ev = self.evidence(name, backend=backend, leg_type=leg_type, source_frames=False, exact_summary=real_failure_summary(token, **gpu), token=token, exit_code=code)
        (ev / "evidence-manifest.json").unlink()   # the job writes none on a product-failure terminal
        return ev

    def derived_backend(self, summaries: dict[str, dict]) -> dict[str, str]:
        script = ("$cases = $env:DVE_SUMMARIES | ConvertFrom-Json\nforeach ($p in $cases.PSObject.Properties) {\n"
                  "  $b = Get-DvDerivedBackend -Summary $p.Value\n  Write-Output ($p.Name + '=' + $(if ($null -eq $b) { '<none>' } else { $b }))\n}\n")
        proc = _ps_json(script, DV / "DualVenueRunner.psm1", {"DVE_SUMMARIES": json.dumps(summaries)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return dict(l.rsplit("=", 1) for l in proc.stdout.splitlines() if "=" in l)

    def test_the_backend_is_derived_where_the_real_failure_summaries_put_the_counters(self) -> None:
        cases = {f"{t}/{n}": (real_failure_summary(t, **g), b) for n, (t, _c, b, g) in enumerate(self.TERMINALS)}
        captured = {"result": "MEASUREMENT_CAPTURED", "gpuFramesTotal": 900, "cpuFrames": 0}
        cases.update({
            "captured cuda": (captured, "cuda"),
            "captured cpu": (dict(captured, gpuFramesTotal=0, cpuFrames=900), "cpu"),
            "captured zero counters": (dict(captured, gpuFramesTotal=0, cpuFrames=0), "<none>"),
            "top-level junk is not rescued by a nested block": ({"result": "PRESENTMON_UNAVAILABLE", "gpuFramesTotal": "n/a", "gpuSummary": {"cpuFrames": 0, "gpuReconReadbackFrames": 5, "gpuTextureReadbackFrames": 0, "gpuTextureNoReadbackFrames": 0}}, "<none>"),
            # a terminal whose counters contradict what that terminal means is not derivable
            "GPU_RECON_FRAMES_ZERO with gpu frames": (real_failure_summary("GPU_RECON_FRAMES_ZERO", gpuReconReadbackFrames=3), "<none>"),
            "CPU_FALLBACK_DETECTED with no cpu frames": (real_failure_summary("CPU_FALLBACK_DETECTED", gpuReconReadbackFrames=900), "<none>"),
            "CPU_FALLBACK_DETECTED with no gpu frames": (real_failure_summary("CPU_FALLBACK_DETECTED", cpuFrames=900), "<none>"),
            "a counter the job did not write": ({"result": "CPU_FALLBACK_DETECTED", "gpuSummary": {"cpuFrames": 4}}, "<none>"),
            "CPU_BACKEND_PATH_MISMATCH that did run the cpu path": (real_failure_summary("CPU_BACKEND_PATH_MISMATCH", cpuFrames=900), "<none>")})
        got = self.derived_backend({k: v[0] for k, v in cases.items()})
        for label, (_summary, want) in cases.items():
            self.assertEqual(got.get(label), want, label)

    def test_a_real_failure_terminal_is_an_advisory_fail_for_a_speed_leg_and_a_look_leg(self) -> None:
        speed_repo = self.prod_repo(leg_type="speed")
        speed_spec = self.spec_path
        look_repo = self.prod_repo(leg_type="look")
        look_spec = self.spec_path
        cases, labels = [], []
        for n, (token, code, backend, gpu) in enumerate(self.TERMINALS):
            for leg, repo, spec in (("speed", speed_repo, speed_spec), ("look", look_repo, look_spec)):
                ev = self.failure_evidence(f"ft-{leg}-{n}", token, code, backend=backend, leg_type=leg, **gpu)
                cases.append((repo, self.receipt_for(repo, ev, backend=backend, outcome="FAIL", leg_type=leg, spec_path=spec)))
                labels.append(f"{leg} {token} {gpu}")
        for repo in (speed_repo, look_repo):
            mine = [(i, c) for i, (r, c) in enumerate(cases) if r is repo]
            for (i, _), got in zip(mine, self.status_batch(repo, [(c, None) for _, c in mine])):
                self.expect(got, "ADVISORY", labels[i])

    def test_a_failure_terminal_cannot_be_relabelled_contradicted_or_promoted(self) -> None:
        repo = self.prod_repo()
        spec = self.spec_path
        zero = self.failure_evidence("rl-zero", "GPU_RECON_FRAMES_ZERO", 13)
        mismatch = self.failure_evidence("rl-mm", "CPU_BACKEND_PATH_MISMATCH", 28, backend="cpu", gpuReconReadbackFrames=900)
        fallback_no_cpu = self.failure_evidence("rl-nocpu", "CPU_FALLBACK_DETECTED", 14, gpuReconReadbackFrames=900)
        got = self.status_batch(repo, [
            (self.relabel(self.receipt_for(repo, zero, outcome="FAIL", spec_path=spec), "cpu"), None),
            (self.relabel(self.receipt_for(repo, mismatch, backend="cpu", outcome="FAIL", spec_path=spec), "cuda"), None),
            (self.receipt_for(repo, fallback_no_cpu, outcome="FAIL", spec_path=spec), None),
            (self.receipt_for(repo, zero, outcome="PASS", spec_path=spec), None)])
        self.expect(got[0], "INVALID", "a cuda terminal relabelled cpu", "BACKEND_MISMATCH")
        self.expect(got[1], "INVALID", "the cpu terminal relabelled cuda", "BACKEND_MISMATCH")
        self.expect(got[2], "INVALID", "counters that contradict the terminal", "BACKEND_NOT_DERIVABLE")
        self.expect(got[3], "INVALID", "a failure terminal claimed as a PASS", "OUTCOME_NOT_DERIVED")

    def relabel(self, receipt: dict, backend: str) -> dict:
        forged = json.loads(json.dumps(receipt))
        forged["subject"]["backend"] = backend
        return redigest(forged)

    def test_the_production_writer_writes_the_failure_receipt_the_docs_promise(self) -> None:
        # before: BACKEND_NOT_DERIVABLE -> DVE_RECEIPT_INVALID -> the runner printed DVE_RECEIPT_WRITE_FAILED and exited 2 with no receipt at all
        repo = self.prod_repo()
        receipts = [self.receipt_for(repo, self.failure_evidence(f"wr{n}", token, code, backend=backend, **gpu), backend=backend, outcome="FAIL")
                    for n, (token, code, backend, gpu) in enumerate(self.TERMINALS)]
        out = self.tmp / "written"
        script = (f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n$rs = $env:DVE_RECEIPTS | ConvertFrom-Json -AsHashtable\n"
                  f"foreach ($r in $rs) {{ try {{ Write-DvReceipt -Receipt $r -ReceiptRoot '{out}' -RepoRoot '{repo.root}' | Out-Null; 'WRITTEN' }} catch {{ 'REFUSED:' + $_.Exception.Message }} }}\n")
        proc = run_pwsh(["-Command", script], env_extra={"DVE_RECEIPTS": json.dumps(receipts)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([l for l in proc.stdout.splitlines() if l.strip()], ["WRITTEN"] * len(receipts), proc.stdout)
        for r in receipts:
            written = json.loads(next(out.rglob(r["receiptId"] + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(written["outcome"], "FAIL")
            self.assertEqual(written["verification"]["status"], "ADVISORY")
            self.assertEqual(self.status(written, repo)[:2], ("ADVISORY", False), "the written failure receipt re-derives, advisory")

    def test_the_docs_say_where_the_failure_terminals_counters_and_leg_type_come_from(self) -> None:
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        for needle in ("gpuSummary", "GPU_RECON_FRAMES_ZERO", "CPU_FALLBACK_DETECTED", "CPU_BACKEND_PATH_MISMATCH", "LEG_TYPE_UNSTATED"):
            self.assertIn(needle, doc)

    def test_mutation_without_the_nested_counters_the_failure_terminals_are_not_derivable(self) -> None:
        mutated = self.mutated_module([("$nested = Get-DvProp $Summary 'gpuSummary'", "$nested = $null")])
        repo = self.prod_repo()
        got = self.status_batch(repo, [(self.receipt_for(repo, self.failure_evidence(f"mn{n}", token, code, backend=backend, **gpu), backend=backend, outcome="FAIL"), None)
                                       for n, (token, code, backend, gpu) in enumerate(self.TERMINALS[:6])], module=mutated)   # (the six that carry ONLY the nested block)
        for n, g in enumerate(got):
            self.expect(g, "INVALID", f"terminal {n}", "BACKEND_NOT_DERIVABLE")   # (all ADVISORY in the test above)

    def test_mutation_without_the_terminal_rule_a_cuda_run_that_used_the_cpu_path_is_mislabelled(self) -> None:
        mutated = self.mutated_module([("if ($terminal -ceq 'GPU_RECON_FRAMES_ZERO') {", "if ($false) {")])
        repo = self.prod_repo()
        ev = self.failure_evidence("mt", "GPU_RECON_FRAMES_ZERO", 13, cpuFrames=900)
        got = self.status_batch(repo, [(self.receipt_for(repo, ev, outcome="FAIL"), None)], module=mutated)[0]
        self.expect(got, "INVALID", "plain counters read an all-cpu cuda failure as a cpu run", "BACKEND_MISMATCH")

    def test_mutation_without_the_unstated_leg_type_rule_a_look_failure_is_a_leg_type_mismatch(self) -> None:
        mutated = self.mutated_module([("if ($failureTerminal -and $null -eq (Get-DvProp $ev.summary 'lookLeg')) {", "if ($false) {")])
        repo = self.prod_repo(leg_type="look")
        ev = self.failure_evidence("ml", "CPU_FALLBACK_DETECTED", 14, leg_type="look", gpuReconReadbackFrames=888, cpuFrames=12)
        got = self.status_batch(repo, [(self.receipt_for(repo, ev, outcome="FAIL", leg_type="look"), None)], module=mutated)[0]
        self.expect(got, "INVALID", "a look leg's failure terminal states no lookLeg", "LEG_TYPE_MISMATCH")


@requires_windows_pwsh
class ContactFramesAreHashListedTests(EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    """sol r1 B4: a LOOK receipt's raw frames and sidecars are accepted only as the hashed contact-frames manifest lists them."""

    def setUp(self) -> None:
        self.make_harness()
        self.repo = self.prod_repo(leg_type="look")
        self.ev = self.evidence("cf", sheet=True, backend="cpu", leg_type="look")
        self.receipt = self.receipt_for(self.repo, self.ev, backend="cpu", leg_type="look")

    def edited(self, name: str) -> Path:
        copy = self.tmp / name
        shutil.copytree(self.ev, copy)
        return copy

    def test_the_listed_frames_are_accepted_and_anything_else_in_the_raw_directory_is_refused(self) -> None:
        self.assertTrue((self.ev / "contact-frames.json").is_file())
        self.assertRegex(self.receipt["evidence"]["contactFramesJsonSha256"], r"^[0-9a-f]{64}$")
        cases = []
        for label, needle, mutate in (
                ("unlisted-png", "CONTACT_FRAME_UNLISTED", lambda raw: (raw / "extra.png").write_bytes(b"\x89PNG\r\n\x1a\nx")),
                ("unlisted-sidecar", "CONTACT_FRAME_UNLISTED", lambda raw: (raw / "extra.json").write_text("{}", encoding="utf-8")),
                ("unlisted-other", "CONTACT_FRAME_UNLISTED", lambda raw: (raw / "notes.txt").write_text("x", encoding="utf-8")),
                ("replaced", "CONTACT_FRAME_HASH_MISMATCH", lambda raw: (raw / "frame-00.png").write_bytes(b"\x89PNG\r\n\x1a\nunrelated image")),
                ("missing", "raw directory does not hold it", lambda raw: (raw / "frame-00.json").unlink()),
                ("subdirectory", "CONTACT_FRAME_UNLISTED", lambda raw: (raw / "nested").mkdir())):
            copy = self.edited("cf-" + label)
            mutate(copy / "contact-sheet" / "raw")
            cases.append((label, needle, copy))
        got = self.status_batch(self.repo, [(self.receipt, None)] + [(self.receipt, c) for _, _, c in cases])
        self.expect(got[0], "ADVISORY", "the listed frames")
        for (label, needle, _), result in zip(cases, got[1:]):
            self.expect(result, "INVALID", label, needle)

    def test_the_manifest_itself_is_hashed_must_be_claimed_and_may_list_only_plain_frame_names(self) -> None:
        tampered = self.edited("cf-manifest")
        with open(tampered / "contact-frames.json", "ab") as handle:
            handle.write(b"\n")
        unclaimed = json.loads(json.dumps(self.receipt))
        unclaimed["evidence"]["contactFramesJsonSha256"] = None
        cases = [(self.receipt, tampered), (unclaimed, None)]
        for label, name in (("traversal", "..\\evil.png"), ("slash", "sub/frame.png"), ("exe", "frame-00.exe")):
            copy = self.edited("cf-name-" + label)
            doc = json.loads((copy / "contact-frames.json").read_text(encoding="utf-8"))
            doc["files"].append({"name": name, "sha256": "c" * 64})
            (copy / "contact-frames.json").write_text(json.dumps(doc), encoding="utf-8")
            receipt = json.loads(json.dumps(self.receipt))
            receipt["evidence"]["contactFramesJsonSha256"] = sha256_of(copy / "contact-frames.json")
            cases.append((receipt, copy))
        got = self.status_batch(self.repo, cases)
        self.expect(got[0], "INVALID", "manifest edited", "EVIDENCE_HASH_MISMATCH")
        self.expect(got[1], "INVALID", "manifest not claimed", "CONTACT_FRAMES_UNLISTED")
        for result in got[2:]:
            self.expect(result, "INVALID", "a path or a non-frame name", "CONTACT_FRAMES_UNLISTED")

    def test_mutation_without_the_unlisted_check_an_extra_png_is_accepted(self) -> None:
        mutated = self.mutated_module([("if ($actual.PSIsContainer -or $isReparse -or -not $listed.Contains($actual.Name.ToLowerInvariant()) -or $listed[$actual.Name.ToLowerInvariant()].name -cne $actual.Name) {", "if ($false) {")])
        copy = self.edited("cf-mut")
        (copy / "contact-sheet" / "raw" / "extra.png").write_bytes(b"\x89PNG\r\n\x1a\nx")     # (INVALID / CONTACT_FRAME_UNLISTED in the test above)
        self.assertEqual(self.status_batch(self.repo, [(self.receipt, copy)], module=mutated)[0][:2], ("ADVISORY", False))

    def test_the_runner_hashes_every_captured_frame_into_the_manifest_the_receipt_names(self) -> None:
        # the offline harness drives the REAL Invoke-VenueLeg.ps1 capture path (stub um-run): it must write contact-frames.json and claim it
        self.write_artifacts(sheet=True, raw_frames=True)
        # a directory inside raw/ (hosted CI regression: the names were once cut from a path PREFIX, and a runner temp dir is an 8.3 short path)
        nested = self.artifacts / "artifacts" / "contact-sheet" / "raw" / "nested"
        nested.mkdir()
        (nested / "deep.png").write_bytes(b"\x89PNG\r\n\x1a\ndeep")
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        claim = receipt["evidence"]["contactFramesJsonSha256"]
        self.assertRegex(claim, r"^[0-9a-f]{64}$")
        manifest = Path(receipt["evidence"]["localEvidenceDir"]) / "contact-frames.json"
        self.assertEqual(sha256_of(manifest), claim)
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], "mlv-app/dual-venue-contact-frames/v1")
        raw = Path(receipt["evidence"]["localEvidenceDir"]) / "contact-sheet" / "raw"
        self.assertEqual(sorted(e["name"] for e in doc["files"]), sorted(p.name + ("\\" if p.is_dir() else "") for p in raw.iterdir()),
                         "top-level entries by NAME; a directory is listed as '<name>\\' (not a plain frame name, so the validator refuses it)")
        for e in doc["files"]:
            if not e["name"].endswith("\\"):
                self.assertEqual(e["sha256"], sha256_of(raw / e["name"]))
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertNotIn(".Substring($rawLocal.Length)", runner, "no file name is derived by cutting a path prefix")


@requires_windows_pwsh
class NonAsciiLegSpecNameIsFoundTests(ModuleMutationMixin, unittest.TestCase):
    """DG-GIT-PATHLIST-LSTREE-LONGFORM-1: `ls-tree -r` (long form) prints a non-ASCII path quoted under the default core.quotepath=true, so the
    path ended in a quote, failed EndsWith('.json'), and a committed `legs/caf<e-acute>.json` was never found (a silent refusal)."""

    NAME = "café.json"
    SPEC = b'{"legId": "cafe-probe"}\n'

    def commit_leg(self) -> tuple[Path, str]:
        tmp = tempfile.TemporaryDirectory(prefix="dve-quotepath-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "repo"
        legs = root / "tools" / "profiling" / "dual-venue" / "legs"
        legs.mkdir(parents=True)
        (legs / self.NAME).write_bytes(self.SPEC)

        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout
        git("init", "-q")
        git("config", "user.email", "unit@example.invalid")
        git("config", "user.name", "unit")
        git("config", "core.quotepath", "true")
        git("add", "tools")
        git("commit", "-q", "-m", "committed")
        self.assertIn("\\303\\251", git("ls-tree", "-r", "--name-only", "HEAD"), "the premise: git prints this name quoted by default")
        return root, git("rev-parse", "HEAD").strip()

    def probe(self, root: Path, head: str, module: Path | None = None) -> list[str]:
        script = (f"$root = '{root}'\n"
                  f"$sha = Get-DvLegSpecSha256 ([byte[]]({','.join(str(b) for b in self.SPEC)}))\n"
                  f"$r = Find-DvCommittedLegSpec -RepoRoot $root -Commit '{head}' -LegSpecSha256 $sha\n"
                  "Write-Output ('FOUND ' + $r.ok)\n"
                  "if ($r.ok) { Write-Output ('PATH ' + [BitConverter]::ToString([Text.Encoding]::UTF8.GetBytes($r.relativePath))) }\n")
        proc = _ps_json(script, module or DV / "DualVenueRunner.psm1", {})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return [l.strip() for l in proc.stdout.splitlines() if l.strip()]

    def test_a_committed_leg_spec_with_a_non_ascii_name_is_found_by_its_hash(self) -> None:
        root, head = self.commit_leg()
        out = self.probe(root, head)
        want = "tools/profiling/dual-venue/legs/" + self.NAME
        self.assertEqual(out, ["FOUND True", "PATH " + "-".join(f"{b:02X}" for b in want.encode("utf-8"))])

    def test_mutation_listing_without_z_loses_the_non_ascii_spec(self) -> None:
        mutated = self.mutated_module([("'ls-tree', '-r', '-z', $Commit", "'ls-tree', '-r', $Commit"),
                                       ('-split "`0"', '-split "`n"')])
        root, head = self.commit_leg()
        self.assertEqual(self.probe(root, head, module=mutated), ["FOUND False"], "the quoted name fails EndsWith('.json'): the old, silent refusal")


@requires_windows_pwsh
class LineEndingsCannotHideACommittedFileTests(ModuleMutationMixin, unittest.TestCase):
    """fable r1 B2: on this VM's default checkout (system git core.autocrlf=true) the working copy of every tracked text file is CRLF while the
    committed blob is LF, and the runner hashed the working bytes while Find-DvCommittedLegSpec hashed the blob bytes, so every committed leg spec was
    refused LEG_SPEC_NOT_COMMITTED. Both sides now hash line-ending-normalised bytes, and .gitattributes pins the three files to LF."""

    FILES = (*SHIPPED_LEGS, "venue-clip-consent.json", "venues.json")

    def checkout(self, with_attributes: bool) -> Path:
        """A temp repo holding the REAL shipped leg specs, consent file and venue table, committed (LF) and then CHECKED OUT under core.autocrlf=true."""
        tmp = tempfile.TemporaryDirectory(prefix="dve-crlf-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "repo"
        dv = root / "tools" / "profiling" / "dual-venue"
        (dv / "legs").mkdir(parents=True)
        for rel in self.FILES:
            (dv / rel).write_bytes((DV / rel).read_bytes().replace(b"\r\n", b"\n"))
        paths = ["tools"]
        if with_attributes:
            shutil.copyfile(ROOT / ".gitattributes", root / ".gitattributes")
            paths.append(".gitattributes")

        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
        git("init", "-q")
        git("config", "user.email", "unit@example.invalid")
        git("config", "user.name", "unit")
        git("config", "core.autocrlf", "true")
        git("add", *paths)
        git("commit", "-q", "-m", "committed")
        for rel in self.FILES:
            (dv / rel).unlink()
        git("checkout", "--", "tools")        # a fresh checkout under autocrlf=true: CRLF unless the attributes pin LF
        return root

    def probe(self, root: Path, module: Path | None = None) -> list[str]:
        script = (f"$root = '{root}'\n$head = (git -C $root rev-parse HEAD)\n"
                  f"foreach ($rel in {SHIPPED_LEGS_PS}) {{\n"
                  "  $bytes = [IO.File]::ReadAllBytes((Join-Path $root ('tools/profiling/dual-venue/' + $rel)))\n"
                  "  $r = Find-DvCommittedLegSpec -RepoRoot $root -Commit $head -LegSpecSha256 (Get-DvLegSpecSha256 $bytes)\n"
                  "  Write-Output ('LEG ' + $rel + ' found=' + $r.ok + ' crlf=' + ($bytes -contains 13))\n}\n"
                  "foreach ($rel in 'venue-clip-consent.json', 'venues.json') {\n"
                  "  $c = Get-DvCommittedFile -RepoRoot $root -RelativePath ('tools/profiling/dual-venue/' + $rel)\n"
                  "  Write-Output ('FILE ' + $rel + ' ok=' + $c.ok + ' reason=' + $c.reason)\n}\n"
                  "$s = Resolve-DvAdmissionSources -RepoRoot $root\nWrite-Output ('SRC ok=' + $s.ok + ' reason=' + $s.reason)\n")
        proc = _ps_json(script, module or DV / "DualVenueRunner.psm1", {})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return [l.strip() for l in proc.stdout.splitlines() if l.strip()]

    def test_the_default_windows_checkout_is_crlf_and_the_committed_specs_consent_and_table_are_still_found(self) -> None:
        root = self.checkout(with_attributes=False)
        out = self.probe(root)
        for rel in SHIPPED_LEGS:
            self.assertIn(f"LEG {rel} found=True crlf=True", out, "the premise: the working copy IS crlf, and the committed spec is still found")
        self.assertIn("FILE venue-clip-consent.json ok=True reason=", out)
        self.assertIn("FILE venues.json ok=True reason=", out)
        self.assertIn("SRC ok=True reason=", out)

    def test_with_the_real_gitattributes_the_checkout_is_lf_and_the_specs_are_found(self) -> None:
        root = self.checkout(with_attributes=True)
        for rel in self.FILES:
            self.assertNotIn(b"\r", (root / "tools" / "profiling" / "dual-venue" / rel).read_bytes(), f"{rel} must check out LF even under autocrlf=true")
        out = self.probe(root)
        for rel in SHIPPED_LEGS:
            self.assertIn(f"LEG {rel} found=True crlf=False", out)
        self.assertIn("SRC ok=True reason=", out)

    def test_each_shipped_leg_is_found_at_head_of_the_real_repo(self) -> None:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
        # one listing of the tracked files instead of one `git ls-files --error-unmatch` per leg (same assertion: every shipped leg is tracked)
        tracked = set(subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "tools/profiling/dual-venue/legs"], capture_output=True, text=True,
                                     check=True, stdin=subprocess.DEVNULL, timeout=120).stdout.split("\0"))
        for rel in SHIPPED_LEGS:
            self.assertIn("tools/profiling/dual-venue/" + rel, tracked, rel)
        proc = _ps_json(f"$root = '{ROOT}'\n"
                        f"foreach ($rel in {SHIPPED_LEGS_PS}) {{\n"
                        "  $bytes = [IO.File]::ReadAllBytes((Join-Path $root ('tools/profiling/dual-venue/' + $rel)))\n"
                        f"  $r = Find-DvCommittedLegSpec -RepoRoot $root -Commit '{head}' -LegSpecSha256 (Get-DvLegSpecSha256 $bytes)\n"
                        "  Write-Output ('LEG ' + $rel + ' found=' + $r.ok)\n}\n", DV / "DualVenueRunner.psm1", {})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for rel in SHIPPED_LEGS:
            self.assertIn(f"LEG {rel} found=True", proc.stdout)

    def test_the_runner_hashes_the_spec_with_the_same_function_the_lookup_uses(self) -> None:
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("$legSpecSha256 = Get-DvLegSpecSha256 $specBytes", runner)
        self.assertNotIn("Get-DvSha256OfBytes $specBytes", runner)
        module = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        self.assertIn("(Get-DvLegSpecSha256 $blob.bytes) -ceq $LegSpecSha256", module)

    def test_mutation_without_the_line_ending_normalisation_the_committed_spec_is_not_found_on_a_crlf_checkout(self) -> None:
        mutated = self.mutated_module([("Get-DvSha256OfBytes (ConvertTo-DvLfBytes $Bytes)", "Get-DvSha256OfBytes $Bytes")])
        root = self.checkout(with_attributes=False)
        out = self.probe(root, module=mutated)
        self.assertIn("LEG legs/m16-1243-speed.json found=False crlf=True", out, "a raw-byte comparison refuses the committed spec -- fable's B2")
        for rel in SHIPPED_LEGS:
            self.assertIn(f"LEG {rel} found=False crlf=True", out)


class GitattributesPinTheDualVenueFilesToLfTests(unittest.TestCase):
    def test_the_leg_specs_the_consent_file_and_the_venue_table_are_pinned_eol_lf(self) -> None:
        for rel in (*("tools/profiling/dual-venue/" + leg for leg in SHIPPED_LEGS),
                    "tools/profiling/dual-venue/venue-clip-consent.json", "tools/profiling/dual-venue/venues.json"):
            out = subprocess.run(["git", "-C", str(ROOT), "check-attr", "eol", "text", "--", rel], capture_output=True, text=True, check=True).stdout
            self.assertIn("eol: lf", out, rel)
            self.assertIn("text: set", out, rel)


# ---------------------------------------------------------------------------------------------------
class SideBySideSheetTests(unittest.TestCase):
    def make_dir(self, base: Path, name: str, indices: list[int], colour: tuple[int, int, int]) -> Path:
        from PIL import Image
        d = base / name
        d.mkdir()
        for i in indices:
            Image.new("RGB", (64, 36), colour).save(d / f"frame-{i:02d}.png")
            (d / f"frame-{i:02d}.json").write_text(json.dumps({
                "index": i, "saved": True, "display_frame": i * 3, "elapsed_ms": i * 40.0, "path": f"frame-{i:02d}.png",
                "look_assist_enabled": True, "look_assist_scene": "night"}), encoding="utf-8")
        return d

    def test_pairs_by_frame_index_and_flags_an_unpaired_index(self) -> None:
        try:
            import PIL, numpy  # noqa: F401
        except ImportError:
            self.skipTest("Pillow + numpy are required")
        with tempfile.TemporaryDirectory(prefix="dve-sheet-") as tmp:
            base = Path(tmp)
            left = self.make_dir(base, "cuda", [0, 1, 2], (200, 40, 40))
            right = self.make_dir(base, "cpu", [0, 1, 3], (40, 40, 200))
            sheet, stats = base / "pair.png", base / "pair.json"
            proc = subprocess.run([sys.executable, str(COMPOSER), "--frames-dir", str(left), "--pair-dir", str(right),
                                   "--sheet-out", str(sheet), "--stats-out", str(stats), "--clip-id", "unit", "--cols", "1"],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            doc = json.loads(stats.read_text(encoding="utf-8"))
            self.assertEqual(doc["schema"], "contact-sheet-stats-pair.v1")
            self.assertEqual(doc["paired_by"], "frame_index")
            self.assertEqual(doc["unpaired_indices"], [2, 3])
            self.assertEqual([t["index"] for t in doc["left"]["tiles"]], [0, 1, 2])
            self.assertEqual([t["index"] for t in doc["right"]["tiles"]], [0, 1, 3])
            self.assertEqual(doc["left"]["label"], "cuda")
            self.assertEqual(doc["right"]["label"], "cpu")
            from PIL import Image
            with Image.open(sheet) as image:
                # four rows (indices 0,1,2,3): taller than one row of two tiles
                self.assertGreater(image.height, image.width // 2)

    def test_single_backend_mode_is_unchanged(self) -> None:
        try:
            import PIL, numpy  # noqa: F401
        except ImportError:
            self.skipTest("Pillow + numpy are required")
        with tempfile.TemporaryDirectory(prefix="dve-sheet1-") as tmp:
            base = Path(tmp)
            left = self.make_dir(base, "cuda", [0, 1], (10, 200, 10))
            proc = subprocess.run([sys.executable, str(COMPOSER), "--frames-dir", str(left), "--sheet-out", str(base / "s.png"),
                                   "--stats-out", str(base / "s.json")], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(json.loads((base / "s.json").read_text(encoding="utf-8"))["schema"], "contact-sheet-stats.v1")


# ---------------------------------------------------------------------------------------------------
class PairComposerReadsOnlyStagedListedFramesTests(unittest.TestCase):
    """DUAL-VENUE-EVIDENCE-3 (sol r2 blocker, PR #223), at the composer: in pair mode a side reads ONLY the plain files of its own staging directory;
    a sidecar `path` that is absolute, parent-relative or anything but its own `<stem>.png` is refused (typed), there is no fallback to an external
    file, and when the caller lists hashes (--left-listed / --right-listed) every file read is verified against its hash AT READ TIME."""

    def setUp(self) -> None:
        try:
            import PIL, numpy  # noqa: F401
        except ImportError:
            self.skipTest("Pillow + numpy are required")
        self._tmp = tempfile.TemporaryDirectory(prefix="dve-confine-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def side(self, name: str, colour: tuple[int, int, int], path_for=None, indices=(0, 1)) -> Path:
        from PIL import Image
        d = self.base / name
        d.mkdir()
        for i in indices:
            Image.new("RGB", (64, 36), colour).save(d / f"frame-{i:02d}.png")
            (d / f"frame-{i:02d}.json").write_text(json.dumps({
                "index": i, "saved": True, "display_frame": i * 3, "elapsed_ms": i * 40.0, "look_assist_enabled": True,
                "path": path_for(i) if path_for else f"frame-{i:02d}.png"}), encoding="utf-8")
        return d

    def listing(self, d: Path, name: str) -> Path:
        files = [{"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(d.iterdir()) if p.is_file()]
        path = self.base / name
        path.write_text(json.dumps({"files": files}), encoding="utf-8")
        return path

    def compose(self, left: Path, right: Path, listed: bool = True, composer: Path | None = None, tag: str = "out"):
        args = [sys.executable, str(composer or COMPOSER), "--frames-dir", str(left), "--pair-dir", str(right), "--sheet-out", str(self.base / f"{tag}.png"),
                "--stats-out", str(self.base / f"{tag}.json"), "--clip-id", "unit", "--cols", "1"]
        if listed:
            args += ["--left-listed", str(self.listing(left, f"{tag}-l.json")), "--right-listed", str(self.listing(right, f"{tag}-r.json"))]
        return subprocess.run(args, capture_output=True, text=True)

    def external(self, colour: tuple[int, int, int], name: str = "frame-00.png") -> Path:
        from PIL import Image
        p = self.base / "legacy-captures" / name
        p.parent.mkdir(exist_ok=True)
        Image.new("RGB", (64, 36), colour).save(p)
        return p

    def test_a_listed_pair_of_plain_staged_frames_composes(self) -> None:
        proc = self.compose(self.side("l", (200, 40, 40)), self.side("r", (40, 40, 200)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.base / "out.png").is_file())

    def test_an_absolute_sidecar_path_is_refused_in_pair_mode_listed_or_not(self) -> None:
        for listed in (True, False):
            for luma in (20, 220):   # the external PNG changes; composing it would change the tile (sol's repro)
                ext = self.external((luma, luma, luma))
                left = self.base / f"l-{listed}-{luma}"
                left = self.side(left.name, (200, 40, 40), path_for=lambda i, p=str(ext): p if i == 0 else f"frame-{i:02d}.png")
                proc = self.compose(left, self.side(f"r-{listed}-{luma}", (40, 40, 200)), listed=listed, tag=f"abs-{listed}-{luma}")
                self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
                self.assertIn("PAIR_SIDECAR_PATH_OUTSIDE_STAGING", proc.stderr)
                self.assertFalse((self.base / f"abs-{listed}-{luma}.png").exists(), "no sheet is written when a side names an external image")

    def test_a_parent_relative_sidecar_path_is_refused_on_either_side(self) -> None:
        self.external((50, 50, 50), "ext.png")
        n = 0
        for ref in ("../legacy-captures/ext.png", "..\\legacy-captures\\ext.png", "sub/../../legacy-captures/ext.png"):
            for side_name in ("left", "right"):
                n += 1
                bad = lambda i, r=ref: r if i == 0 else f"frame-{i:02d}.png"   # noqa: E731
                left = self.side(f"pl{n}", (200, 40, 40), path_for=bad if side_name == "left" else None)
                right = self.side(f"pr{n}", (40, 40, 200), path_for=bad if side_name == "right" else None)
                proc = self.compose(left, right, tag=f"rel{n}")
                self.assertEqual(proc.returncode, 3, f"{ref!r} on the {side_name}: {proc.stdout}{proc.stderr}")
                self.assertIn("PAIR_SIDECAR_PATH_OUTSIDE_STAGING", proc.stderr)

    def test_a_missing_staged_png_is_never_replaced_by_an_external_file(self) -> None:
        # the old resolver fell back to <frames_dir>/<stem>.png and, before that, to any path the sidecar named: with the staged PNG gone nothing else may stand in
        ext = self.external((60, 60, 60))
        left = self.side("l", (200, 40, 40), path_for=lambda i, p=str(ext): p if i == 0 else f"frame-{i:02d}.png")
        (left / "frame-00.png").unlink()
        proc = self.compose(left, self.side("r", (40, 40, 200)))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("PAIR_SIDECAR_PATH_OUTSIDE_STAGING", proc.stderr)
        # a staged PNG that is simply absent is an UNPAIRED / image-missing tile (as before), never another file's pixels
        plain = self.side("l2", (200, 40, 40))
        (plain / "frame-01.png").unlink()   # (frame 0 is the geometry probe: without it the compose has always refused)
        proc = self.compose(plain, self.side("r2", (40, 40, 200)), listed=False, tag="gone")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        doc = json.loads((self.base / "gone.json").read_text(encoding="utf-8"))
        self.assertEqual([t["index"] for t in doc["left"]["tiles"]], [0])
        self.assertEqual([t["index"] for t in doc["right"]["tiles"]], [0, 1])

    def test_a_staged_file_edited_after_it_was_listed_is_refused_at_read_time(self) -> None:
        from PIL import Image
        left, right = self.side("l", (200, 40, 40)), self.side("r", (40, 40, 200))
        lists = (self.listing(left, "ll.json"), self.listing(right, "rr.json"))
        Image.new("RGB", (64, 36), (1, 2, 3)).save(left / "frame-00.png")   # swapped after the hash was taken (same name)
        args = [sys.executable, str(COMPOSER), "--frames-dir", str(left), "--pair-dir", str(right), "--sheet-out", str(self.base / "o.png"),
                "--stats-out", str(self.base / "o.json"), "--left-listed", str(lists[0]), "--right-listed", str(lists[1])]
        proc = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("PAIR_FRAME_HASH_MISMATCH", proc.stderr)
        self.assertFalse((self.base / "o.png").exists())

    def test_an_unlisted_file_in_staging_is_refused(self) -> None:
        left, right = self.side("l", (200, 40, 40)), self.side("r", (40, 40, 200))
        lists = (self.listing(left, "ll.json"), self.listing(right, "rr.json"))
        self.external((9, 9, 9)).replace(right / "frame-07.png")   # a PNG that no manifest line lists, dropped into staging
        args = [sys.executable, str(COMPOSER), "--frames-dir", str(left), "--pair-dir", str(right), "--sheet-out", str(self.base / "o.png"),
                "--stats-out", str(self.base / "o.json"), "--left-listed", str(lists[0]), "--right-listed", str(lists[1])]
        proc = subprocess.run(args, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("PAIR_FRAME_NOT_LISTED", proc.stderr)

    def test_a_listing_is_all_or_nothing(self) -> None:
        left, right = self.side("l", (200, 40, 40)), self.side("r", (40, 40, 200))
        args = [sys.executable, str(COMPOSER), "--frames-dir", str(left), "--pair-dir", str(right), "--sheet-out", str(self.base / "o.png"),
                "--stats-out", str(self.base / "o.json"), "--left-listed", str(self.listing(left, "ll.json"))]
        proc = subprocess.run(args, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0, "a listing for one side only would leave the other side's reads unverified")

    def test_single_backend_mode_still_honours_an_absolute_sidecar_path(self) -> None:
        # (the single-capture sheet is the app's own capture dir read by the owner: unchanged; only the evidence PAIR is confined)
        ext = self.external((70, 70, 70))
        d = self.side("solo", (10, 200, 10), path_for=lambda i, p=str(ext): p if i == 0 else f"frame-{i:02d}.png")
        proc = subprocess.run([sys.executable, str(COMPOSER), "--frames-dir", str(d), "--sheet-out", str(self.base / "s.png"), "--stats-out", str(self.base / "s.json")],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    # -- mutations: each guard taken out of a COPY of the composer, and the scenario above must change ----------------------------------------
    def mutated_composer(self, old: str, new: str) -> Path:
        text = COMPOSER.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"mutation anchor must occur exactly once: {old!r}")
        path = self.base / "mutated-composer.py"
        path.write_text(text.replace(old, new), encoding="utf-8")
        return path

    def test_mutation_without_the_path_confinement_an_external_reference_is_composed(self) -> None:
        mutated = self.mutated_composer('if raw not in ("", None) and raw != own:', "if False:")
        ext = self.external((20, 20, 20))
        left = self.side("l", (200, 40, 40), path_for=lambda i, p=str(ext): p if i == 0 else f"frame-{i:02d}.png")
        proc = self.compose(left, self.side("r", (40, 40, 200)), composer=mutated)
        self.assertEqual(proc.returncode, 0, "with the check removed the reference is not refused -- so the real run's refusal is the check: " + proc.stdout + proc.stderr)

    def test_mutation_without_the_read_time_hash_a_swapped_staged_file_is_composed(self) -> None:
        from PIL import Image
        mutated = self.mutated_composer("if self.listed is not None and hashlib.sha256(data).hexdigest() != self.listed[name]:", "if False:")
        left, right = self.side("l", (200, 40, 40)), self.side("r", (40, 40, 200))
        lists = (self.listing(left, "ll.json"), self.listing(right, "rr.json"))
        Image.new("RGB", (64, 36), (1, 2, 3)).save(left / "frame-00.png")
        proc = subprocess.run([sys.executable, str(mutated), "--frames-dir", str(left), "--pair-dir", str(right), "--sheet-out", str(self.base / "o.png"),
                               "--stats-out", str(self.base / "o.json"), "--left-listed", str(lists[0]), "--right-listed", str(lists[1])], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, "with the hash check removed a file swapped after listing is composed: " + proc.stdout + proc.stderr)


# ---------------------------------------------------------------------------------------------------
CLAMPED_TO_1 = {"scale_request_last": 1, "scale_active_last": 1, "gpu_texture_route_scale_clamp_active": 1, "gpu_texture_route_scale_clamp_requested_scale": 2}
CLAMP_LINE = "playback_scale_clamped_for_gpu_texture_route requested=2 effective=1"


@requires_windows_pwsh
class PlaybackScaleIsRequestedAndEffectiveTests(EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    """DVE-SCALE2-LOOK-LEG-1 r2 (fable + sol r1, the SAME blocker): the CUDA texture route clamps every requested playback scale other than 1 to 1
    (MainWindowGpuPreviewPolicy.h; pinned by tests/gui/test_gui_smoke.cpp), so a leg that names scale 2 and runs on CUDA rendered at scale 1 and still ended
    PASS. CLASS: no venue leg can PASS under a scale it did not render at, and every receipt says the scale requested and the scale actually rendered.
    These run the production route shape: the run log is the app's own (`scale_active_last` on its playback_smoke.summary line, the one-time
    `playback_scale_clamped_for_gpu_texture_route` line)."""

    def setUp(self) -> None:
        self.make_harness()

    def leg(self, backend: str, scale: int, line: dict | None = None, accepted: dict | None = None, extra_log_lines: list[str] | None = None,
            leg_type: str = "speed"):
        self.write_artifacts(line=line, extra_log_lines=extra_log_lines, sheet=(leg_type == "look"))
        spec = self.write_spec(scale=scale, accepted=accepted, leg_type=leg_type)
        return self.run_leg("ultra-magnus", spec, extra=["-Backend", backend])

    # -- item 1b / 1e: the receipt says both numbers and the leg cannot PASS under a scale it did not render at ----------------------------
    def test_a_cuda_run_clamped_from_scale_2_to_1_is_not_a_pass_and_the_receipt_says_both_numbers(self) -> None:
        proc, receipt, _ = self.leg("cuda", 2, line=CLAMPED_TO_1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "SCALE_NOT_HONOURED", receipt["outcomeDetail"])
        self.assertEqual((receipt["scale"]["requestedScale"], receipt["scale"]["effectiveScale"]), (2, 1))
        self.assertIn("2", receipt["outcomeDetail"])
        self.assertIn("rendered at 1", receipt["outcomeDetail"])
        self.assertIn("DVE_OUTCOME=SCALE_NOT_HONOURED", proc.stdout)

    def test_a_cpu_run_at_2_over_2_passes_and_carries_both_fields(self) -> None:
        proc, receipt, _ = self.leg("cpu", 2, line={"scale_request_last": 2, "scale_active_last": 2})
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual((receipt["scale"]["requestedScale"], receipt["scale"]["effectiveScale"]), (2, 2))
        self.assertEqual(receipt["scale"]["verdict"], "HONOURED")

    def test_a_declared_clamp_passes_but_is_labelled_with_both_numbers(self) -> None:
        """The existing scale-4 look leg's CUDA backend renders at 1 (three production receipts). Its spec DECLARES that, so the leg stays runnable; the
        receipt and the outcome detail still say requested 4 / rendered 1, and the verdict is DECLARED_CLAMP -- never HONOURED."""
        proc, receipt, _ = self.leg("cuda", 4, line={"scale_request_last": 1, "scale_active_last": 1}, accepted={"cuda": 1})
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual((receipt["scale"]["requestedScale"], receipt["scale"]["effectiveScale"], receipt["scale"]["acceptedEffectiveScale"]), (4, 1, 1))
        self.assertEqual(receipt["scale"]["verdict"], "DECLARED_CLAMP")
        self.assertIn("requested scale 4", receipt["outcomeDetail"])
        self.assertIn("rendered at 1", receipt["outcomeDetail"])

    def test_the_outcome_detail_names_the_resolution_next_to_each_scale_number(self) -> None:
        """SCALE-LABEL-CLARITY-1: "scale 4" / "rendered at 1" read backwards; the number stays (asserted above) and the words follow it."""
        _, receipt, _ = self.leg("cuda", 4, line={"scale_request_last": 1, "scale_active_last": 1}, accepted={"cuda": 1})
        detail = receipt["outcomeDetail"]
        self.assertIn("requested scale 4 [Quarter res (0.25x)]", detail)
        self.assertIn("rendered at 1 [Full res (1x)]", detail)
        self.assertIn("renders at 1 [Full res (1x)]", detail)
        _, receipt, _ = self.leg("cuda", 2, line={"scale_request_last": 1, "scale_active_last": 1})
        self.assertIn("requested scale 2 [Half res (0.5x)]", receipt["outcomeDetail"])

    def test_a_declaration_does_not_excuse_a_run_that_rendered_at_neither_the_request_nor_the_declared_scale(self) -> None:
        # the declaration is a claim about the route; if the route changes (cuda now renders 4) the spec is stale and the leg stops passing until it is edited
        _, receipt, _ = self.leg("cuda", 4, line={"scale_request_last": 4, "scale_active_last": 4}, accepted={"cuda": 1})
        self.assertEqual(receipt["outcome"], "SCALE_NOT_HONOURED", receipt["outcomeDetail"])
        _, receipt, _ = self.leg("cuda", 4, line={"scale_request_last": 2, "scale_active_last": 2}, accepted={"cuda": 1})
        self.assertEqual(receipt["outcome"], "SCALE_NOT_HONOURED", receipt["outcomeDetail"])
        # ... and the same clamp with NO declaration is not a pass either
        _, receipt, _ = self.leg("cuda", 4, line={"scale_request_last": 1, "scale_active_last": 1})
        self.assertEqual(receipt["outcome"], "SCALE_NOT_HONOURED", receipt["outcomeDetail"])

    def test_an_unknown_effective_scale_never_passes(self) -> None:
        _, receipt, _ = self.leg("cpu", 4, line={"scale_request_last": None, "scale_active_last": None})
        self.assertEqual(receipt["outcome"], "SCALE_NOT_HONOURED", receipt["outcomeDetail"])
        self.assertEqual(receipt["scale"]["effectiveScale"], "UNKNOWN")
        self.assertEqual(receipt["scale"]["requestedScale"], 4)
        self.assertIn("UNKNOWN", receipt["outcomeDetail"])

    def test_the_clamp_line_supplies_the_effective_scale_when_the_summary_field_is_absent(self) -> None:
        _, receipt, _ = self.leg("cuda", 2, line={"scale_request_last": None, "scale_active_last": None}, extra_log_lines=[CLAMP_LINE])
        self.assertEqual(receipt["outcome"], "SCALE_NOT_HONOURED", receipt["outcomeDetail"])
        self.assertEqual(receipt["scale"]["effectiveScale"], 1)
        self.assertIn("clamp", receipt["scale"]["effectiveScaleSource"])

    def test_a_look_leg_that_rendered_at_the_wrong_scale_has_no_sheet_credit(self) -> None:
        _, receipt, _ = self.leg("cuda", 2, line=CLAMPED_TO_1, leg_type="look")
        self.assertEqual(receipt["outcome"], "SCALE_NOT_HONOURED", receipt["outcomeDetail"])

    def test_every_receipt_says_the_scale_requested_even_a_refusal_with_no_run_log(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(scale=2), probe=dict(HEALTHY_PROBE, pwshColdStartMs=9000))
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")
        self.assertEqual((receipt["scale"]["requestedScale"], receipt["scale"]["effectiveScale"]), (2, "UNKNOWN"))

    # -- item 1d: the smoke runner's own scale check is live where the route allows ------------------------------------------------------
    def test_the_job_is_generated_with_the_scale_the_route_is_declared_to_render_at(self) -> None:
        self.write_artifacts()
        self.run_leg("ultra-magnus", self.write_spec(), extra=["-Backend", "cuda"])
        self.assertIn("ExpectedScaleRequest=4", self.generator_calls()[-1])
        self.run_leg("ultra-magnus", self.write_spec(accepted={"cuda": 1}), extra=["-Backend", "cuda"])
        self.assertIn("ExpectedScaleRequest=1", self.generator_calls()[-1], "the CUDA route's request is already clamped when the smoke runner reads it")
        self.run_leg("ultra-magnus", self.write_spec(accepted={"cuda": 1}), extra=["-Backend", "cpu"])
        self.assertIn("ExpectedScaleRequest=4", self.generator_calls()[-1])

    # -- the production validator derives the same verdict from the HASHED log, never from the receipt ---------------------------------
    def production_receipt(self, scale: int, line: dict | None, backend: str = "cuda", accepted: dict | None = None, **ev_opts):
        spec = self.write_spec(scale=scale, accepted=accepted)
        repo = self.prod_repo(spec=spec)
        ev = self.evidence("scale-" + hashlib.sha1(json.dumps([scale, line, backend, accepted]).encode()).hexdigest()[:8], backend=backend, line=line, **ev_opts)
        return repo, ev, self.receipt_for(repo, ev, backend=backend)

    def test_a_pass_receipt_for_a_clamped_cuda_run_does_not_re_derive(self) -> None:
        repo, ev, receipt = self.production_receipt(2, CLAMPED_TO_1)
        self.assertEqual((receipt["scale"]["requestedScale"], receipt["scale"]["effectiveScale"]), (2, 1))
        self.assertNotValid(receipt, repo, "OUTCOME_NOT_DERIVED", status="INVALID")
        # the same run is a valid NO_SIGNAL record when it is receipted for what it is
        honest = dict(receipt, outcome="SCALE_NOT_HONOURED")
        self.assertEqual(self.status(honest, repo)[:2], ("NO_SIGNAL", True))

    def test_a_cpu_pass_at_2_over_2_re_derives_as_advisory(self) -> None:
        repo, ev, receipt = self.production_receipt(2, {"scale_request_last": 2, "scale_active_last": 2}, backend="cpu")
        self.assertAdvisory(receipt, repo)

    def test_a_declared_clamp_pass_re_derives_as_advisory(self) -> None:
        repo, ev, receipt = self.production_receipt(4, {"scale_request_last": 1, "scale_active_last": 1}, accepted={"cuda": 1})
        self.assertAdvisory(receipt, repo)

    def test_a_receipt_that_misstates_the_scale_it_names_does_not_re_derive(self) -> None:
        repo, ev, receipt = self.production_receipt(2, CLAMPED_TO_1)
        lying = json.loads(json.dumps(receipt))
        lying["scale"]["effectiveScale"] = 2
        self.assertNotValid(lying, repo, "SCALE_NOT_FROM_EVIDENCE", status="INVALID")
        bare = json.loads(json.dumps(receipt))
        del bare["scale"]
        self.assertNotValid(bare, repo, "RECEIPT_FIELD_ABSENT: the receipt carries no scale block")

    # -- one mutation per rule --------------------------------------------------------------------------------------------------------
    # -- item 2 (fable H4): two look legs on one venue and backend never overwrite each other's sheet ----------------------------------------
    def two_look_legs_into_one_directory(self, dv: Path = DV) -> list[str]:
        out = self.tmp / ".claude-state" / "sheets"
        self.write_artifacts(sheet=True)
        for leg_id, scale in (("unit-look", 4), ("unit-look-scale2", 2)):
            self.write_artifacts(sheet=True, line={"scale_request_last": scale, "scale_active_last": scale})
            self.run_leg("ultra-magnus", self.write_spec(leg_type="look", scale=scale, leg_id=leg_id), extra=["-Backend", "cpu", "-SheetCopyDir", str(out)], dv=dv)
        return sorted(p.name for p in out.iterdir())

    def test_the_sheet_copy_name_carries_the_leg_id_so_two_look_legs_cannot_overwrite_each_other(self) -> None:
        self.assertEqual(self.two_look_legs_into_one_directory(), ["sheet-unit-look-scale2-ultra-magnus-cpu-classic.png", "sheet-unit-look-ultra-magnus-cpu-classic.png"])

    def test_mutation_without_the_leg_id_in_the_sheet_name_the_second_leg_overwrites_the_first(self) -> None:
        dv = self.mutated_runner([("Invoke-VenueLeg.ps1", '"sheet-$($spec.legId)-$Venue-$Backend-$lookFlavor.png"', '"sheet-$Venue-$Backend-$lookFlavor.png"')])
        self.assertEqual(self.two_look_legs_into_one_directory(dv), ["sheet-ultra-magnus-cpu-classic.png"])

    def test_mutation_without_the_runner_gate_the_clamped_run_passes(self) -> None:
        dv = self.mutated_runner([("Invoke-VenueLeg.ps1", "if (-not $scale.honoured -and $null -ne $runLogText) {", "if ($false) {")])
        self.write_artifacts(line=CLAMPED_TO_1)
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(scale=2), extra=["-Backend", "cuda"], dv=dv)
        self.assertEqual(receipt["outcome"], "PASS", "with the gate removed the clamped run passes; so the gate is what stops it")

    def test_mutation_without_the_validator_derivation_a_forged_pass_is_believed(self) -> None:
        repo, ev, receipt = self.production_receipt(2, CLAMPED_TO_1)
        mutated = self.mutated_module([("if (-not $scaleVerdict.honoured) { $expected = 'SCALE_NOT_HONOURED' }", "")])
        got = self.status_batch(repo, [(receipt, ev)], module=mutated)[0]
        self.assertEqual(got[0], "ADVISORY", "with the derivation removed the forged PASS re-derives; so the derivation is what refuses it")

    def test_mutation_without_the_expected_scale_argument_the_job_carries_none(self) -> None:
        dv = self.mutated_runner([("Invoke-VenueLeg.ps1", "$gen['ExpectedScaleRequest'] = [int]$scale.acceptedEffectiveScale", "$null = 0")])
        self.write_artifacts()
        self.run_leg("ultra-magnus", self.write_spec(), extra=["-Backend", "cuda"], dv=dv)
        self.assertNotIn("ExpectedScaleRequest", self.generator_calls()[-1])

    def test_mutation_without_the_unknown_rule_an_absent_scale_passes(self) -> None:
        dv = self.mutated_runner([("DualVenueRunner.psm1", "if ($null -eq $effective) { $verdict = 'UNKNOWN' }", "if ($null -eq $effective) { $verdict = 'HONOURED' }")])
        self.write_artifacts(line={"scale_request_last": None, "scale_active_last": None})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), extra=["-Backend", "cpu"], dv=dv)
        self.assertEqual(receipt["outcome"], "PASS", "with the UNKNOWN rule removed an unmeasured scale passes; so that rule is what stops it")


# ---------------------------------------------------------------------------------------------------
# DUAL-VENUE-DISPLAY-MATRIX-1 (owner 2026-10-03 and 2026-10-05: playback benchmarks must not be full screen only). The app's own window-placement lines, as MainWindow.cpp
# logs them (a windowed run is the normal maximized window with chrome; a full-screen run covers the screen).
PLACEMENT_TAIL = 'target_screen="\\\\.\\DISPLAY1" presentation_screen="\\\\.\\DISPLAY1" presentation_physical=3840x2400'
WINDOWED_PLACEMENT = 'gui_smoke.window_placement mode=windowed screen="\\\\.\\DISPLAY1" verified=1 window=0,23 2560x1529 preview=2380x1373 ' + PLACEMENT_TAIL
FULLSCREEN_PLACEMENT = 'gui_smoke.window_placement mode=fullscreen screen="\\\\.\\DISPLAY1" verified=1 window=0,0 2560x1600 preview=2560x1600 ' + PLACEMENT_TAIL
UNVERIFIED_WINDOWED_PLACEMENT = WINDOWED_PLACEMENT.replace("verified=1", "verified=0")
# the twelve cells of the standard display matrix, in the order a run walks them (backend-major; a full-screen cell and its windowed twin are neighbours)
MATRIX_CELLS = [f"{backend}-{mode}-s{scale}" for backend in ("cuda", "cpu") for scale in (1, 2, 4) for mode in ("fullscreen", "windowed")]


@requires_windows_pwsh
class DisplayModeIsRequestedAndObservedTests(EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    """A leg's optional `displayMode` (default full screen) reaches the generator as the app's --windowed, the receipt carries the display mode REQUESTED and the one the
    app OBSERVED (its own gui_smoke.window_placement line) plus the window it presented in, and the leg FAILS CLOSED: a requested-windowed leg that ran full screen is
    not a valid measurement. A legacy spec (no displayMode) keeps the behaviour it had, so no existing leg changes."""

    def setUp(self) -> None:
        self.make_harness()

    def leg(self, display_mode: str | None, placement: str | None, backend: str = "cpu", **opts):
        self.write_artifacts(extra_log_lines=[placement] if placement else [])
        return self.run_leg("ultra-magnus", self.write_spec(display_mode=display_mode), extra=["-Backend", backend], **opts)

    # -- the schema default -----------------------------------------------------------------------------------------------------------
    def test_a_spec_without_a_display_mode_is_a_full_screen_leg_and_every_legacy_leg_names_none(self) -> None:
        for name in ("m16-1243-speed", "m16-1243-look", "m16-1243-look-scale2", "m16-1243-look-cinematic", "m16-1243-look-scale2-cinematic",
                     "m16-1243-look-film", "m16-1243-look-scale2-film"):
            self.assertNotIn("displayMode", json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8")), f"{name}: an existing leg must not change")
        _, receipt, _ = self.leg(None, FULLSCREEN_PLACEMENT)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual((receipt["display"]["requestedMode"], receipt["display"]["explicit"]), ("fullscreen", False))
        self.assertNotIn("DisplayMode=", self.generator_calls()[-1], "a full-screen leg passes the generator nothing: its job is the text it always was")

    def test_a_legacy_leg_whose_log_has_no_placement_line_is_unchanged_and_says_unknown(self) -> None:
        _, receipt, _ = self.leg(None, None)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual((receipt["display"]["observedMode"], receipt["display"]["verdict"], receipt["display"]["blocks"]), ("UNKNOWN", "UNKNOWN", False))

    def test_a_legacy_leg_that_ran_windowed_is_not_a_valid_full_screen_measurement(self) -> None:
        _, receipt, _ = self.leg(None, WINDOWED_PLACEMENT)
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertEqual(receipt["display"]["verdict"], "NOT_HONOURED")

    # -- the plumbing: windowed -> --windowed -----------------------------------------------------------------------------------------
    def test_a_windowed_leg_asks_the_generator_for_the_windowed_job_and_a_fullscreen_leg_asks_for_nothing(self) -> None:
        self.leg("windowed", WINDOWED_PLACEMENT)
        self.assertIn("DisplayMode=windowed", self.generator_calls()[-1])
        self.leg("fullscreen", FULLSCREEN_PLACEMENT)
        self.assertNotIn("DisplayMode", self.generator_calls()[-1])

    def test_the_generator_has_the_parameter_and_turns_it_into_the_apps_windowed_option(self) -> None:
        src = GENERATOR.read_text(encoding="utf-8")
        self.assertRegex(src, r"\[ValidateSet\('fullscreen', 'windowed'\)\]\s+\[string\]\$DisplayMode = 'fullscreen'")
        self.assertIn("DUAL_VENUE_DISPLAY_ANCHOR_MISSING", src, "a template refactor must fail loudly, never let a windowed leg run full screen")
        self.assertIn("'--windowed'", src)
        # the smoke runner's own argument gate lets the app's --windowed through (a refused option would end the leg before it plays)
        self.assertRegex((ROOT / "tools" / "profiling" / "gui-smoke-length-gate.ps1").read_text(encoding="utf-8"), r"'windowed' = 'allow'")

    # -- the receipt: requested, observed and the presented window --------------------------------------------------------------------
    def test_a_windowed_leg_that_ran_windowed_passes_and_the_receipt_says_requested_observed_and_the_window(self) -> None:
        proc, receipt, _ = self.leg("windowed", WINDOWED_PLACEMENT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        d = receipt["display"]
        self.assertEqual((d["requestedMode"], d["explicit"], d["observedMode"], d["verdict"], d["blocks"]), ("windowed", True, "windowed", "HONOURED", False))
        self.assertEqual((d["windowWidth"], d["windowHeight"]), (2560, 1529))
        self.assertEqual((d["previewWidth"], d["previewHeight"]), (2380, 1373))
        self.assertEqual((d["presentationPhysicalWidth"], d["presentationPhysicalHeight"]), (3840, 2400))
        self.assertTrue(d["placementVerified"])
        self.assertEqual(d["observedSource"], "gui_smoke.window_placement")
        self.assertIn("scale", receipt, "the display block sits next to the scale block")
        self.assertIn(receipt["scale"]["verdict"], ("HONOURED", "DECLARED_CLAMP"))

    def test_a_fullscreen_leg_that_ran_full_screen_passes_and_records_the_screen_it_covered(self) -> None:
        _, receipt, _ = self.leg("fullscreen", FULLSCREEN_PLACEMENT)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual((receipt["display"]["observedMode"], receipt["display"]["windowWidth"], receipt["display"]["windowHeight"]), ("fullscreen", 2560, 1600))

    def test_every_receipt_says_the_display_mode_requested_even_a_refusal_with_no_run_log(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(display_mode="windowed"), probe=dict(HEALTHY_PROBE, pwshColdStartMs=9000))
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")
        self.assertEqual((receipt["display"]["requestedMode"], receipt["display"]["observedMode"]), ("windowed", "UNKNOWN"))

    # -- FAIL CLOSED ------------------------------------------------------------------------------------------------------------------
    def test_a_windowed_leg_that_ran_full_screen_is_invalid_not_a_pass(self) -> None:
        proc, receipt, _ = self.leg("windowed", FULLSCREEN_PLACEMENT)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("DISPLAY_MODE_NOT_HONOURED", receipt["outcomeDetail"])
        self.assertIn("requested windowed but the app ran fullscreen", receipt["outcomeDetail"])
        self.assertEqual((receipt["display"]["verdict"], receipt["display"]["blocks"], receipt["display"]["observedMode"]), ("NOT_HONOURED", True, "fullscreen"))
        self.assertIn("DVE_OUTCOME=INVALID", proc.stdout)

    def test_a_fullscreen_leg_that_ran_windowed_is_invalid_too(self) -> None:
        _, receipt, _ = self.leg("fullscreen", WINDOWED_PLACEMENT)
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("DISPLAY_MODE_NOT_HONOURED", receipt["outcomeDetail"])

    def test_a_leg_that_names_a_mode_the_log_cannot_confirm_is_invalid(self) -> None:
        _, receipt, _ = self.leg("windowed", None)
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertEqual((receipt["display"]["observedMode"], receipt["display"]["verdict"]), ("UNKNOWN", "UNKNOWN"))
        self.assertIn("unknown", receipt["outcomeDetail"])

    def test_a_windowed_leg_whose_placement_was_not_verified_is_invalid(self) -> None:
        _, receipt, _ = self.leg("windowed", UNVERIFIED_WINDOWED_PLACEMENT)
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("verified=0", receipt["outcomeDetail"])

    def test_a_product_failure_of_a_leg_that_ran_in_the_wrong_mode_is_invalid_too(self) -> None:
        self.write_artifacts(extra_log_lines=[FULLSCREEN_PLACEMENT], summary={"rows": 0})   # the spec's criteria need rows > 0 on cuda: a FAIL on a sound run
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(display_mode="windowed"), extra=["-Backend", "cuda"])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("job result was FAIL", receipt["outcomeDetail"])

    # -- the production validator derives the same verdict from the HASHED log and the COMMITTED spec ----------------------------------
    def production(self, display_mode: str | None, placement: str | None, with_block: bool = True):
        spec = self.write_spec(display_mode=display_mode)
        repo = self.prod_repo(spec=spec)
        ev = self.evidence("dm-" + hashlib.sha1(json.dumps([display_mode, placement, with_block]).encode()).hexdigest()[:8], backend="cuda",
                           extra_log_lines=[placement] if placement else [])
        receipt = self.receipt_for(repo, ev, backend="cuda")
        if with_block:
            receipt["display"] = self.derive_display(ev, spec)
        return repo, ev, receipt

    @staticmethod
    def derive_display(ev: Path, spec_path: Path) -> dict:
        """An INDEPENDENT mirror of Get-DvDisplayEvidence's two compared fields (the spec's mode, else full screen; the app's own placement line, else UNKNOWN)."""
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        found = re.findall(r"gui_smoke\.window_placement mode=(\w+) ", (ev / "logs" / "smoke-run.log").read_text(encoding="utf-8"))
        return {"requestedMode": spec.get("displayMode", "fullscreen"), "observedMode": found[-1] if found else "UNKNOWN"}

    def test_a_windowed_pass_over_a_full_screen_run_does_not_re_derive(self) -> None:
        repo, ev, receipt = self.production("windowed", FULLSCREEN_PLACEMENT)
        self.assertNotValid(receipt, repo, "DISPLAY_MODE_NOT_HONOURED", status="INVALID")

    def test_a_windowed_pass_over_a_windowed_run_re_derives_as_advisory(self) -> None:
        repo, ev, receipt = self.production("windowed", WINDOWED_PLACEMENT)
        self.assertAdvisory(receipt, repo)

    def test_a_receipt_that_misstates_the_mode_the_app_ran_in_does_not_re_derive(self) -> None:
        repo, ev, receipt = self.production("windowed", FULLSCREEN_PLACEMENT)
        receipt["display"]["observedMode"] = "windowed"
        self.assertNotValid(receipt, repo, "DISPLAY_NOT_FROM_EVIDENCE", status="INVALID")

    def test_a_receipt_of_a_leg_that_names_a_mode_but_carries_no_display_block_is_incomplete(self) -> None:
        repo, ev, receipt = self.production("windowed", WINDOWED_PLACEMENT, with_block=False)
        self.assertNotValid(receipt, repo, "the receipt carries no display block", status="INCOMPLETE")

    def test_a_legacy_leg_re_derives_exactly_as_before_with_or_without_a_display_block(self) -> None:
        repo, ev, receipt = self.production(None, None, with_block=False)
        self.assertAdvisory(receipt, repo)

    # -- one mutation per rule ----------------------------------------------------------------------------------------------------------
    def test_mutation_without_the_runner_gate_a_windowed_leg_that_ran_full_screen_passes(self) -> None:
        dv = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($outcome -in @('PASS', 'FAIL') -and $display.blocks) {", "if ($false) {")])
        _, receipt, _ = self.leg("windowed", FULLSCREEN_PLACEMENT, dv=dv)
        self.assertEqual(receipt["outcome"], "PASS", "with the gate removed the wrong-mode run passes; so the gate is what stops it")

    def test_mutation_without_the_plumbing_the_generator_is_never_asked_for_windowed(self) -> None:
        dv = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($display.requestedMode -ceq 'windowed') { $gen['DisplayMode'] = 'windowed' }", "$null = 0")])
        self.leg("windowed", WINDOWED_PLACEMENT, dv=dv)
        self.assertNotIn("DisplayMode", self.generator_calls()[-1])

    def test_mutation_without_the_unknown_rule_an_unconfirmed_mode_passes(self) -> None:
        dv = self.mutated_runner([("DualVenueRunner.psm1", "$blocks = ($verdict -ceq 'NOT_HONOURED') -or ($verdict -ceq 'UNKNOWN' -and $explicit)", "$blocks = ($verdict -ceq 'NOT_HONOURED')")])
        _, receipt, _ = self.leg("windowed", None, dv=dv)
        self.assertEqual(receipt["outcome"], "PASS", "with the UNKNOWN rule removed an unconfirmed mode passes; so that rule is what stops it")

    def test_mutation_without_the_validator_derivation_a_wrong_mode_pass_is_believed(self) -> None:
        repo, ev, receipt = self.production("windowed", FULLSCREEN_PLACEMENT)
        mutated = self.mutated_module([("if ($displayVerdict.blocks) { $invalid.Add(", "if ($false) { $invalid.Add(")])
        self.assertEqual(self.status_batch(repo, [(receipt, ev)], module=mutated)[0][0], "ADVISORY",
                         "with the derivation removed the forged PASS re-derives; so the derivation is what refuses it")


def run_pwsh_json(script: str, **env: str):
    """Run a PowerShell snippet that imports the runner module and prints one JSON document; returns (CompletedProcess, parsed)."""
    proc = run_pwsh(["-Command", f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n" + script], env_extra=env)
    try:
        return proc, json.loads(proc.stdout)
    except ValueError:
        return proc, None


class _ScaleLabelHeaderMixin:
    """Reads the one mapping (PlaybackScaleLabel::resolutionLabel) out of the C++ header for both mirror test classes below."""

    HEADER = ROOT / "platform" / "qt" / "PlaybackScaleLabel.h"

    def header_labels(self) -> dict:
        body = self.HEADER.read_text(encoding="utf-8").split("static QString resolutionLabel", 1)[1].split("static QString shortLabel", 1)[0]
        labels = {int(n): text for n, text in re.findall(r'case (\d+):\s*return QCoreApplication::translate\(\s*"PlaybackScaleLabel",\s*"([^"]+)"\s*\)', body)}
        self.assertEqual(sorted(labels), [1, 2, 4], "the header maps exactly the three presets by name")
        custom = re.search(r'default:\s*return QCoreApplication::translate\(\s*"PlaybackScaleLabel",\s*"([^"]+)"', body)
        self.assertIsNotNone(custom)
        labels[0] = custom.group(1)   # the template for any other factor, %1 = N
        return labels


@requires_windows_pwsh
class ScaleLabelMirrorTests(_ScaleLabelHeaderMixin, unittest.TestCase):
    """SCALE-LABEL-CLARITY-1 r2: Get-DvScaleLabel (PowerShell) mirrors PlaybackScaleLabel::resolutionLabel; the C++ header is the one mapping, so a reworded label there
    fails here instead of drifting silently into receipts. The contact-sheet mirror is ungated, in ContactSheetScaleLabelMirrorTests."""

    def test_the_powershell_mirror_equals_the_cpp_header(self) -> None:
        want = self.header_labels()
        proc, out = run_pwsh_json("(@(1, 2, 4, 8, 3) | ForEach-Object { [pscustomobject]@{ n = $_; label = (Get-DvScaleLabel -Factor $_) } }) | ConvertTo-Json -Compress")
        self.assertIsNotNone(out, proc.stderr)
        got = {row["n"]: row["label"] for row in out}
        for n in (1, 2, 4):
            self.assertEqual(got[n], want[n])
        for n in (3, 8):
            self.assertEqual(got[n], want[0].replace("%1", str(n)))


class ContactSheetScaleLabelMirrorTests(_ScaleLabelHeaderMixin, unittest.TestCase):
    """SCALE-LABEL-CLARITY-FOLLOWUP-1 (b): scale_label (make-contact-sheet.py) mirrors PlaybackScaleLabel::resolutionLabel. Needs no pwsh (pure Python, header text only), so it is
    NOT behind requires_windows_pwsh and the Ubuntu hygiene shards pin it too."""

    def test_the_contact_sheet_mirror_equals_the_cpp_header(self) -> None:
        want = self.header_labels()
        # The hosted Repo Hygiene runner has no numpy and make-contact-sheet.py imports it at module level, so the module is never executed: only the
        # scale_label function is lifted out of the parsed source and run on its own (it uses builtins only).
        source = (ROOT / "tools" / "profiling" / "make-contact-sheet.py").read_text(encoding="utf-8")
        funcs = [node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "scale_label"]
        self.assertEqual(len(funcs), 1, "make-contact-sheet.py defines exactly one top-level scale_label")
        namespace: dict = {}
        exec(compile(ast.Module(body=funcs, type_ignores=[]), "make-contact-sheet.py:scale_label", "exec"), namespace)
        scale_label = namespace["scale_label"]
        for n in (1, 2, 4):
            self.assertEqual(scale_label(str(n)), want[n])
        for n in (3, 8):
            self.assertEqual(scale_label(str(n)), want[0].replace("%1", str(n)))


@requires_windows_pwsh
class UnpacedEvidenceTests(unittest.TestCase):
    """CUDA-HEADROOM-1, FAIL CLOSED: an unpaced leg counts only when the app's own pace_unpaced line for the MEASURED session says active=1 drop_frame=0."""

    UNPACED = {"generatorArgs": {"unpaced": True}}

    def verdict(self, spec: dict, log: str) -> dict:
        proc, out = run_pwsh_json("$s = $env:DVE_SPEC | ConvertFrom-Json\n(Get-DvUnpacedEvidence -Spec $s -LogText $env:DVE_LOG) | ConvertTo-Json -Compress",
                                  DVE_SPEC=json.dumps(spec), DVE_LOG=log)
        self.assertIsNotNone(out, proc.stderr)
        return out

    @staticmethod
    def log(*extra: str) -> str:
        return "\n".join(["playback_smoke.measured_session id=3", "playback_smoke.summary session=3 reason=reached presented_frames=600", *extra]) + "\n"

    def test_honoured_only_with_active_1_and_drop_frame_0(self) -> None:
        v = self.verdict(self.UNPACED, self.log("playback_smoke.pace_unpaced session=3 active=1 drop_frame=0 run_nonce=n1"))
        self.assertEqual((v["verdict"], v["blocks"], v["active"], v["dropFrame"]), ("HONOURED", False, 1, 0))

    def test_a_log_without_the_line_blocks(self) -> None:
        v = self.verdict(self.UNPACED, self.log())
        self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True))
        self.assertIn("no playback_smoke.pace_unpaced line", v["reason"])

    def test_active_0_or_drop_frame_1_blocks(self) -> None:
        for line in ("playback_smoke.pace_unpaced session=3 active=0 drop_frame=0 run_nonce=n1",
                     "playback_smoke.pace_unpaced session=3 active=1 drop_frame=1 run_nonce=n1"):
            v = self.verdict(self.UNPACED, self.log(line))
            self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), line)

    def test_another_sessions_line_does_not_count(self) -> None:
        v = self.verdict(self.UNPACED, self.log("playback_smoke.pace_unpaced session=2 active=1 drop_frame=0 run_nonce=n1"))
        self.assertTrue(v["blocks"])

    def test_a_leg_that_does_not_ask_never_blocks(self) -> None:
        for spec in ({}, {"generatorArgs": {"presentTrace": True}}, {"generatorArgs": {"unpaced": False}}):
            v = self.verdict(spec, self.log())
            self.assertEqual((v["verdict"], v["blocks"]), ("NOT_REQUESTED", False), spec)

    def test_the_validator_re_derives_it(self) -> None:
        module = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        self.assertIn("$unpacedVerdict = Get-DvUnpacedEvidence -Spec $spec -LogText $ev.logText", module)
        self.assertIn('if ($unpacedVerdict.blocks) { $invalid.Add("UNPACED_NOT_HONOURED:', module)
        roles = json.loads((DV / "venues.json").read_text(encoding="utf-8"))["roles"]
        self.assertEqual(roles["CUDA-HEADROOM-1"], {"bachelor": "acceptance", "ultra-magnus": "supplementary"})


@requires_windows_pwsh
class LookAssistEvidenceTests(unittest.TestCase):
    """PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 (D4b), FAIL CLOSED: a leg whose spec says Look Assist ON counts only when the app's own LAST
    gui_smoke.visual_state line says look_assist_enabled=1."""

    def verdict(self, spec: dict, log: str) -> dict:
        proc, out = run_pwsh_json("$s = $env:DVE_SPEC | ConvertFrom-Json\n(Get-DvLookAssistEvidence -Spec $s -LogText $env:DVE_LOG) | ConvertTo-Json -Compress",
                                  DVE_SPEC=json.dumps(spec), DVE_LOG=log)
        self.assertIsNotNone(out, proc.stderr)
        return out

    @staticmethod
    def log(*state: str) -> str:
        return "\n".join(["playback_smoke.measured_session id=3", *(f"[INFO] interaction_trace event=gui_smoke.visual_state {s}" for s in state),
                          "playback_smoke.summary session=3 reason=reached presented_frames=600"]) + "\n"

    def test_a_forced_or_look_leg_is_honoured_only_with_look_assist_enabled_1(self) -> None:
        for spec in ({"generatorArgs": {"forceLookAssist": True}}, {"legType": "look", "look": {"contactSheetFrames": 2}}):
            with self.subTest(spec=spec):
                v = self.verdict(spec, self.log("look_assist_enabled=1 look_assist_diagnostics_valid=1 look_assist_flavor=classic"))
                self.assertEqual((v["requested"], v["verdict"], v["blocks"], v["reported"]), (True, "HONOURED", False, "1"))

    def test_off_missing_field_or_missing_line_blocks(self) -> None:
        spec = {"generatorArgs": {"forceLookAssist": True}}
        for log, needle in ((self.log("look_assist_enabled=0 look_assist_flavor=none"), "look_assist_enabled=0"),
                            (self.log("look_assist_diagnostics_valid=1"), "carries no look_assist_enabled"),
                            (self.log(), "no gui_smoke.visual_state line"), ("", "no gui_smoke.visual_state line")):
            with self.subTest(needle=needle):
                v = self.verdict(spec, log)
                self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True))
                self.assertIn(needle, v["reason"])

    def test_the_last_visual_state_line_is_the_one_that_counts(self) -> None:
        spec = {"generatorArgs": {"forceLookAssist": True}}
        self.assertTrue(self.verdict(spec, self.log("look_assist_enabled=1", "look_assist_enabled=0"))["blocks"])
        self.assertFalse(self.verdict(spec, self.log("look_assist_enabled=0", "look_assist_enabled=1"))["blocks"])

    def test_a_leg_that_does_not_ask_never_blocks(self) -> None:
        for spec in ({}, {"generatorArgs": {"forceLookAssist": False}}, {"generatorArgs": {"disableLookAssist": True}}):
            v = self.verdict(spec, self.log("look_assist_enabled=0"))
            self.assertEqual((v["requested"], v["verdict"], v["blocks"]), (False, "NOT_REQUESTED", False), spec)

    def test_the_runner_gates_on_it(self) -> None:
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("$lookAssistVerdict = Get-DvLookAssistEvidence -Spec $spec -LogText $runLogText", runner)
        self.assertIn("if ($outcome -in @('PASS', 'FAIL') -and $lookAssistVerdict.blocks) {", runner)
        self.assertIn("LOOK_ASSIST_NOT_HONOURED", runner)


@requires_windows_pwsh
class DisplayMatrixLegSetTests(unittest.TestCase):
    """The standard display matrix: the shipped leg set expands to the twelve cells, interleaved across repeats, every cell is an ordinary committed leg spec, and
    the leg set refuses what would not be one matrix."""

    SET = DV / "legsets" / "display-matrix.json"

    def plan(self, repeats: int = 1, backend: str = "", set_path: Path | None = None):
        proc, plan = run_pwsh_json(f"Get-DvLegSetPlan -LegSetPath '{set_path or self.SET}' -Repeats {repeats}" + (f" -Backend {backend}" if backend else "")
                                   + " | ConvertTo-Json -Depth 5 -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return plan

    def test_the_six_shipped_legs_make_the_full_grid_for_the_owner_clip(self) -> None:
        grid = set()
        for rel in DISPLAY_MATRIX_LEGS:
            spec = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.assertEqual((spec["card"], spec["clipId"], spec["legType"], spec["look"]["lookFlavor"]), ("DUAL-VENUE-DISPLAY-MATRIX-1", OWNER_CLIP, "look", "cinematic"), rel)
            self.assertEqual(spec["backends"], ["cuda", "cpu"], rel)
            self.assertEqual(spec["legId"], Path(rel).stem, rel)
            self.assertIn(spec["displayMode"], ("fullscreen", "windowed"))
            if spec["scaleFactor"] != 1:
                self.assertNotIn("acceptedEffectiveScale", spec, f"{rel}: the CUDA texture route honours scales 2 and 4 (PLAYBACK-CUDA-HONOUR-SCALE-1); a CUDA cell at 1 must not pass")
            grid.add((spec["displayMode"], spec["scaleFactor"]))
        self.assertEqual(grid, {(m, s) for m in ("fullscreen", "windowed") for s in (1, 2, 4)})

    def test_the_shipped_set_expands_to_twelve_cells_in_the_documented_order(self) -> None:
        plan = self.plan()
        self.assertEqual((plan["legSet"], plan["card"], plan["clipId"], len(plan["cells"])), ("display-matrix", "DUAL-VENUE-DISPLAY-MATRIX-1", OWNER_CLIP, 12))
        self.assertEqual([c["cellId"] for c in plan["cells"]], MATRIX_CELLS)
        self.assertEqual([e["cellId"] for e in plan["plan"]], MATRIX_CELLS)
        self.assertEqual([e["seq"] for e in plan["plan"]], list(range(1, 13)))

    def test_repeats_are_interleaved_forward_then_back_and_every_cell_runs_once_per_repeat(self) -> None:
        plan = self.plan(repeats=3)
        walk = [e["cellId"] for e in plan["plan"]]
        self.assertEqual(len(walk), 36)
        self.assertEqual(walk[:12], MATRIX_CELLS)
        self.assertEqual(walk[12:24], MATRIX_CELLS[::-1], "repeat 2 walks the cells back, so a drift of the host lands on both ends of every comparison")
        self.assertEqual(walk[24:], MATRIX_CELLS)
        self.assertEqual([e["repeat"] for e in plan["plan"]], [1] * 12 + [2] * 12 + [3] * 12)
        for r in (1, 2, 3):
            self.assertEqual(sorted(e["cellId"] for e in plan["plan"] if e["repeat"] == r), sorted(MATRIX_CELLS))

    def test_a_backend_filter_keeps_only_that_backends_cells(self) -> None:
        plan = self.plan(repeats=2, backend="cpu")
        self.assertEqual([c["cellId"] for c in plan["cells"]], [c for c in MATRIX_CELLS if c.startswith("cpu-")])
        self.assertEqual(len(plan["plan"]), 12)

    def test_the_planned_legs_name_committed_specs_the_receipt_is_bound_to(self) -> None:
        plan = self.plan()
        for entry in plan["plan"]:
            self.assertTrue(Path(entry["specPath"]).is_file(), entry)
            self.assertEqual(Path(entry["specPath"]).parent, DV / "legs")
            self.assertEqual(entry["legId"], Path(entry["specPath"]).stem)

    def broken_set(self, mutate) -> Path:
        tmp = tempfile.TemporaryDirectory(prefix="dve-set-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "dual-venue"
        shutil.copytree(DV, root)
        doc = json.loads((root / "legsets" / "display-matrix.json").read_text(encoding="utf-8"))
        mutate(doc, root)
        path = root / "legsets" / "broken.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def assertRefused(self, set_path: Path) -> None:
        proc, _ = run_pwsh_json(f"Get-DvLegSetPlan -LegSetPath '{set_path}' | ConvertTo-Json -Compress")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DVE_LEGSET_INVALID", proc.stdout + proc.stderr)

    def test_a_set_that_would_not_be_one_matrix_is_refused(self) -> None:
        def other_clip(doc, root):
            spec_path = root / "legs" / "m16-1243-display-windowed-s2.json"
            spec_path.write_text(spec_path.read_text(encoding="utf-8").replace('"clipId": "M16-1243"', '"clipId": "M16-9999"'), encoding="utf-8")
        self.assertRefused(self.broken_set(other_clip))
        self.assertRefused(self.broken_set(lambda doc, root: doc.update(card="SOME-OTHER-CARD-1")))
        self.assertRefused(self.broken_set(lambda doc, root: doc["legs"].append(doc["legs"][0])))            # two legs make the cell cuda-fullscreen-s1
        self.assertRefused(self.broken_set(lambda doc, root: doc.update(legs=[])))
        self.assertRefused(self.broken_set(lambda doc, root: doc.update(schema="mlv-app/dual-venue-legset/v0")))

    def test_a_set_may_name_only_committed_leg_specs_under_legs(self) -> None:
        self.assertRefused(self.broken_set(lambda doc, root: doc["legs"].append("venues.json")))
        self.assertRefused(self.broken_set(lambda doc, root: doc["legs"].append("legs/../venues.json")))
        self.assertRefused(self.broken_set(lambda doc, root: doc["legs"].append("legs/no-such-leg.json")))

    # -- the entry point: Invoke-VenueLeg -LegSet display-matrix -Repeats N ---------------------------------------------------------------
    def test_plan_only_prints_the_planned_legs_and_needs_no_venue_commit_or_build(self) -> None:
        proc = run_pwsh(["-File", str(DV / "Invoke-VenueLeg.ps1"), "-LegSet", "display-matrix", "-PlanOnly", "-Repeats", "2"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        plan_lines = [l for l in proc.stdout.splitlines() if l.startswith("DVE_PLAN ")]
        self.assertEqual(len(plan_lines), 24)
        self.assertIn("DVE_LEGSET=display-matrix CARD=DUAL-VENUE-DISPLAY-MATRIX-1 CLIP=M16-1243 CELLS=12 REPEATS=2 LEGS=24", proc.stdout)
        self.assertIn("DVE_PLAN 1/24 repeat=1 cell=cuda-fullscreen-s1 leg=m16-1243-display-fullscreen-s1 backend=cuda display=fullscreen scale=1", proc.stdout)
        self.assertIn("DVE_PLAN 2/24 repeat=1 cell=cuda-windowed-s1 leg=m16-1243-display-windowed-s1 backend=cuda display=windowed scale=1", proc.stdout)
        self.assertIn("display=windowed scale=1 resolution=Full res (1x)", proc.stdout, "SCALE-LABEL-CLARITY-1: the plan line says the resolution beside the factor")
        self.assertNotIn("DVE_RECEIPT_PATH", proc.stdout, "a plan runs nothing: no leg, so no receipt")

    def test_a_leg_set_that_runs_needs_a_venue_a_commit_and_a_build(self) -> None:
        proc = run_pwsh(["-File", str(DV / "Invoke-VenueLeg.ps1"), "-LegSet", "display-matrix"])
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("DVE_LEGSET_REQUIRES_VENUE", proc.stdout + proc.stderr)

    def test_a_leg_spec_and_a_leg_set_cannot_be_mixed_and_a_single_leg_still_needs_its_arguments(self) -> None:
        proc = run_pwsh(["-File", str(DV / "Invoke-VenueLeg.ps1"), "-LegSet", "display-matrix", "-LegSpec", str(DV / "legs" / "m16-1243-look.json")])
        self.assertNotEqual(proc.returncode, 0, "-LegSet and -LegSpec are different parameter sets")
        proc = run_pwsh(["-File", str(DV / "Invoke-VenueLeg.ps1"), "-LegSpec", str(DV / "legs" / "m16-1243-look.json")])
        self.assertNotEqual(proc.returncode, 0, "a single leg still needs -Venue, -SourceCommit and -BuildManifestSha256")

    # -- the one summary table ------------------------------------------------------------------------------------------------------------
    def table(self, rows: list[dict], cells: list[dict]) -> str:
        proc = run_pwsh(["-Command", f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n$rows = $env:DVE_ROWS | ConvertFrom-Json\n$cells = $env:DVE_CELLS | ConvertFrom-Json\n"
                                    "ConvertTo-DvMatrixTable -Rows @($rows) -Cells @($cells)"], env_extra={"DVE_ROWS": json.dumps(rows), "DVE_CELLS": json.dumps(cells)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.replace("\r\n", "\n")

    @staticmethod
    def row(cell: str, outcome: str = "PASS", fps: float | None = 12.0, **over) -> dict:
        backend, mode, scale = cell.split("-")
        base = {"cellId": cell, "backend": backend, "outcome": outcome, "observedDisplay": mode, "effectiveScale": 1 if backend == "cuda" else int(scale[1:]),
                "windowSize": "2560x1529" if mode == "windowed" else "2560x1600", "previewSize": "2380x1373" if mode == "windowed" else "2560x1600",
                "presentedFps": fps, "timelineFpsAfterFirstPresent": 19.5, "renderWorkMs": 28.0, "dualIsoMs": 25.0}
        base.update(over)
        return base

    def test_the_table_has_a_row_per_cell_with_the_owner_numbers_and_the_clamp_visible(self) -> None:
        cells = [{"cellId": "cuda-windowed-s2", "backend": "cuda", "displayMode": "windowed", "scaleFactor": 2},
                 {"cellId": "cpu-fullscreen-s4", "backend": "cpu", "displayMode": "fullscreen", "scaleFactor": 4}]
        rows = [self.row("cuda-windowed-s2", fps=11.8, timelineFpsAfterFirstPresent=19.2, renderWorkMs=29.5, dualIsoMs=24.2),
                self.row("cuda-windowed-s2", fps=12.1, timelineFpsAfterFirstPresent=19.6, renderWorkMs=27.5, dualIsoMs=26.0),
                self.row("cuda-windowed-s2", fps=12.0, timelineFpsAfterFirstPresent=19.4, renderWorkMs=28.5, dualIsoMs=25.0),
                self.row("cpu-fullscreen-s4", fps=4.0), self.row("cpu-fullscreen-s4", outcome="INVALID", fps=None, presentedFps=None)]
        lines = self.table(rows, cells).splitlines()
        self.assertEqual(len(lines), 2 + 2)
        self.assertIn("presented fps median (min-max)", lines[0])
        self.assertIn("timeline fps after first present", lines[0])
        self.assertIn("render_work ms", lines[0])
        self.assertIn("dual-ISO ms", lines[0])
        windowed = lines[2]
        self.assertTrue(windowed.startswith("| cuda-windowed-s2 | cuda | windowed / windowed | Half res (0.5x)->Full res (1x) | 2560x1529 (2380x1373) | 3/3 | 12.00 (11.80-12.10) | 19.40 | 28.5 | 25.0 | PASS x3 |"), windowed)
        cpu = lines[3]
        self.assertIn("| 1/2 | 4.00 (4.00-4.00) |", cpu, "one measurement of two runs; the INVALID repeat is counted in 'ran' and shows no numbers")
        self.assertTrue(cpu.endswith("INVALID x1, PASS x1 |"), cpu)

    def test_the_scale_column_reads_as_resolution_labels_never_bare_factors(self) -> None:
        """SCALE-LABEL-CLARITY-1 r2: requested -> effective renders through the same mapping as the plan line and outcomeDetail (Get-DvScaleLabel); the
        internal receipt fields stay numeric. An unread effective scale ('UNKNOWN') says so; an off-preset factor is 'Custom res (1/N)'."""
        cells = [{"cellId": "cuda-windowed-s2", "backend": "cuda", "displayMode": "windowed", "scaleFactor": 2},
                 {"cellId": "cpu-windowed-s4", "backend": "cpu", "displayMode": "windowed", "scaleFactor": 4},
                 {"cellId": "cpu-windowed-s8", "backend": "cpu", "displayMode": "windowed", "scaleFactor": 8},
                 {"cellId": "cpu-windowed-s1", "backend": "cpu", "displayMode": "windowed", "scaleFactor": 1}]
        rows = [self.row("cuda-windowed-s2", effectiveScale=2), self.row("cpu-windowed-s4", effectiveScale=1),
                self.row("cpu-windowed-s8", effectiveScale=8),
                self.row("cpu-windowed-s1", outcome="INVALID", fps=None, presentedFps=None, effectiveScale="UNKNOWN")]
        lines = self.table(rows, cells).splitlines()
        self.assertIn("| Half res (0.5x)->Half res (0.5x) |", lines[2])
        self.assertIn("| Quarter res (0.25x)->Full res (1x) |", lines[3])
        self.assertIn("| Custom res (1/8)->Custom res (1/8) |", lines[4])
        self.assertIn("| Full res (1x)->unknown res |", lines[5])
        for line in lines[2:]:
            self.assertNotRegex(line, r"\| \d->\S+ \|", "a bare factor in the scale column is the confusing form this card replaces")

    def test_a_cell_with_no_measurement_shows_dashes_and_its_outcome(self) -> None:
        cells = [{"cellId": "cpu-windowed-s1", "backend": "cpu", "displayMode": "windowed", "scaleFactor": 1}]
        line = self.table([self.row("cpu-windowed-s1", outcome="INVALID", fps=None, presentedFps=None, timelineFpsAfterFirstPresent=None, renderWorkMs=None, dualIsoMs=None)], cells).splitlines()[2]
        self.assertIn("| 0/1 | - | - | - | - | INVALID x1 |", line)
        self.assertIn("| cpu | windowed / windowed |", line)


@requires_windows_pwsh
class DisplayMatrixRunTests(EvidenceFactory, unittest.TestCase):
    """The whole entry point, offline (stub um-run and generator): Invoke-VenueLeg -LegSet display-matrix -Repeats N runs every planned leg through the single-leg runner,
    interleaved, and writes ONE summary table from the receipts and their hashed run logs. One cell's app log says the app ran full screen where the leg asked for windowed:
    its row is INVALID with no numbers, the rest of the matrix still runs."""

    def setUp(self) -> None:
        self.make_harness()

    def cell_artifacts(self, cell: str, placement: str, fps: float) -> str:
        """Write one cell's run artifacts (its own app log: the placement the app ran in, the scale it rendered at, the four rates) and return the agent-side path."""
        _, mode, scale = cell.split("-")
        eff = int(scale[1:])   # the CUDA texture route renders at the requested 1, 2 or 4 (PLAYBACK-CUDA-HONOUR-SCALE-1)
        name = f"{cell}.artifacts"
        saved = self.artifacts
        self.artifacts = self.share / "outbox" / name
        try:
            self.write_artifacts(sheet=True, summary={"lookAssistForced": True, "lookFlavorReported": "cinematic"},
                                 line={"scale_request_last": eff, "scale_active_last": eff, "presented_fps": fps, "avg_render_work_ms": 28.5},
                                 extra_log_lines=[placement,
                                                  "playback_smoke.pace_summary session=3 first_present_ms=1000.000 paced_elapsed_ms=30000.000 timeline_fps_after_first_present=19.40 presented_fps_after_first_present=12.5 pace_fps=23.976 first_present_catchup_frames=0",
                                                  "playback_smoke.cpu_summary session=3 avg_llrawproc_total_ms=26.000 avg_llrawproc_dual_iso_ms=25.5 avg_processing_ms=5.000"])
            self.stamp_identity("ultra-magnus", self.build_sha)
        finally:
            self.artifacts = saved
        return f"X:\\stub\\agent\\outbox\\{name}"

    def run_matrix(self, repeats: int, extra: list[str], placements: dict[str, str]):
        by_leg = {}
        for cell in (c for c in MATRIX_CELLS if c.startswith("cuda-")):
            _, mode, scale = cell.split("-")
            placement = placements.get(cell, WINDOWED_PLACEMENT if mode == "windowed" else FULLSCREEN_PLACEMENT)
            by_leg[f"display-{mode}-{scale}-ultra-magnus-cuda"] = self.cell_artifacts(cell, placement, 12.0 + (0.5 if mode == "windowed" else 0.0) + 0.1 * int(scale[1:]))
        cfg = {"log": str(self.log), "genLog": str(self.gen_log), "probe": HEALTHY_PROBE, "mainMode": "capture", "healthMode": "ok", "artifactsByLeg": by_leg,
               "artifactsAgentPath": "X:\\stub\\agent\\outbox\\unused.artifacts", "genRefusal": None, "clipContentSha256": CLIP_CONTENT_SHA, "token": None, "exitCode": 0}
        self.stub_cfg.write_text(json.dumps(cfg), encoding="utf-8")
        self.log.write_text("", encoding="utf-8")
        self.gen_log.write_text("", encoding="utf-8")
        proc = run_pwsh(["-File", str(DV / "Invoke-VenueLeg.ps1"), "-Venue", "ultra-magnus", "-LegSet", "display-matrix", "-Repeats", str(repeats), "-SourceCommit", self.sha,
                         "-BuildManifestSha256", self.build_sha, "-VenueTablePath", str(self.table), "-ReceiptRoot", str(self.receipts), "-OfflineTestMode",
                         "-UmRunScript", str(self.um), "-GeneratorScript", str(self.gen), "-WorkDir", str(self.tmp / "work"), "-ConsentPath", str(self.consent),
                         "-RepoRoot", str(ROOT), "-Actor", "unit-test", *extra], env_extra={"DVE_STUB": str(self.stub_cfg)})
        return proc

    def test_a_cuda_matrix_run_interleaves_the_cells_runs_every_leg_and_writes_one_table(self) -> None:
        bad_cell = "cuda-windowed-s2"   # asked for windowed; the app's log says it ran full screen
        proc = self.run_matrix(2, ["-Backend", "cuda"], {bad_cell: FULLSCREEN_PLACEMENT})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        legs = [l for l in proc.stdout.splitlines() if l.startswith("DVE_MATRIX_LEG ")]
        self.assertEqual(len(legs), 12, proc.stdout)
        walked = [re.search(r"cell=(\S+)", l).group(1) for l in legs]
        cuda_cells = [c for c in MATRIX_CELLS if c.startswith("cuda-")]
        self.assertEqual(walked, cuda_cells + cuda_cells[::-1], "repeat 1 forward, repeat 2 back")
        # the stub saw the same order, one health probe per leg
        submitted = [l for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip() and not l.endswith("-health")]
        self.assertEqual(len(submitted), 12)
        self.assertEqual([re.search(r"m16-1243-display-(\w+)-(s\d)-ultra-magnus-cuda", j).groups() for j in submitted],
                         [(c.split("-")[1], c.split("-")[2]) for c in walked])
        # only the windowed legs asked the generator for --windowed
        gen = self.generator_calls()
        self.assertEqual(sum("DisplayMode=windowed" in g for g in gen), 6)
        # one receipt per leg, each with its own display block
        outcomes = {}
        for l in legs:
            cell = re.search(r"cell=(\S+)", l).group(1); outcome = re.search(r"outcome=(\S+)", l).group(1)
            outcomes.setdefault(cell, []).append(outcome)
        for cell in cuda_cells:
            want = "INVALID" if cell == bad_cell else "PASS"
            self.assertEqual(outcomes[cell], [want, want], cell)
        # the ONE summary table
        summary = re.search(r"DVE_MATRIX_SUMMARY=(.+)", proc.stdout).group(1).strip()
        text = Path(summary).read_text(encoding="utf-8")
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("| cuda-")]), 6)
        self.assertIn("| cuda-windowed-s1 | cuda | windowed / windowed | Full res (1x)->Full res (1x) | 2560x1529 (2380x1373) | 2/2 | 12.60 (12.60-12.60) | 19.40 | 28.5 | 25.5 | PASS x2 |", text)
        self.assertIn("| cuda-fullscreen-s4 | cuda | fullscreen / fullscreen | Quarter res (0.25x)->Quarter res (0.25x) | 2560x1600 (2560x1600) | 2/2 | 12.40 (12.40-12.40) | 19.40 | 28.5 |", text)
        bad_row = next(l for l in text.splitlines() if l.startswith(f"| {bad_cell} "))
        self.assertIn("| windowed / fullscreen |", bad_row)
        self.assertIn("| 0/2 | - | - | - | - | INVALID x2 |", bad_row)
        # the receipts the table was read from carry requested, observed and window
        receipts = [json.loads(p.read_text(encoding="utf-8")) for p in self.receipts.rglob("*.json")]
        self.assertEqual(len(receipts), 12)
        windowed_ok = [r for r in receipts if r["legId"] == "m16-1243-display-windowed-s1"]
        self.assertEqual({(r["display"]["requestedMode"], r["display"]["observedMode"], r["display"]["windowWidth"]) for r in windowed_ok}, {("windowed", "windowed", 2560)})
        self.assertEqual({r["scale"]["verdict"] for r in windowed_ok}, {"HONOURED"})
        honoured = [r for r in receipts if r["legId"] == "m16-1243-display-fullscreen-s2"]
        self.assertEqual({(r["scale"]["requestedScale"], r["scale"]["effectiveScale"], r["scale"]["verdict"]) for r in honoured}, {(2, 2, "HONOURED")})

    def test_a_table_row_is_read_only_from_a_run_log_that_still_hashes_to_its_receipt(self) -> None:
        proc = self.run_matrix(1, ["-Backend", "cuda"], {})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        receipts = sorted(self.receipts.rglob("*.json"))
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        log = Path(receipt["evidence"]["localEvidenceDir"]) / "logs" / "smoke-run.log"
        log.write_text(log.read_text(encoding="utf-8").replace("presented_fps=12", "presented_fps=99"), encoding="utf-8")   # a swapped log
        script = (f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n$r = Get-Content -LiteralPath $env:DVE_RECEIPT -Raw | ConvertFrom-Json\n"
                  "Get-DvMatrixRow -PlanEntry ([pscustomobject]@{ seq = 1; repeat = 1; cellId = 'x'; backend = 'cuda'; displayMode = 'fullscreen'; scaleFactor = 1 }) -Receipt $r | ConvertTo-Json -Compress")
        out = run_pwsh(["-Command", script], env_extra={"DVE_RECEIPT": str(receipts[0])})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        row = json.loads(out.stdout)
        self.assertIsNone(row["presentedFps"], "a log that does not hash to the receipt is no measurement")
        self.assertIn("does not hash to the receipt", row["note"])


# ---------------------------------------------------------------------------------------------------
# VENUE-QUIET-UNKNOWN-PROBE-FAILS-1: Wait-VenueQuiet.ps1 used to print `COOLDOWN_UNMET mean=UNKNOWN` and exit 0 when the venue probe was unavailable or failed, and
# its header told callers COOLDOWN_UNMET means proceed -- so a leg could run on a venue nobody measured. A final decision of UNKNOWN is now its own typed verdict
# (`VENUE_QUIET_UNKNOWN`) with its own exit code (6); QUIET and a MEASURED COOLDOWN_UNMET (state BUSY) still exit 0. The probe is a stub um-run.ps1: offline, nothing is submitted.
STUB_UM_RUN_FOR_QUIET = r"""param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec)
# test stub for tools\profiling\um-run.ps1: UMSTUB_MODE = samples (canned VENUE_QUIET line) | noline (a run with no VENUE_QUIET line) | fail (the submission throws)
if ($env:UMSTUB_MODE -eq 'fail') { throw 'stub submission failed' }
if ($env:UMSTUB_MODE -eq 'noline') { return [pscustomobject]@{ exitCode = 1; stdout = 'probe printed nothing useful' } }
$probe = [ordered]@{ schema = 'mlv-app/venue-quiet-probe/v1'; host = 'BACHELOR'; samples = @($env:UMSTUB_SAMPLES | ConvertFrom-Json); top = @() }
[pscustomobject]@{ exitCode = 0; stdout = 'VENUE_QUIET=' + ($probe | ConvertTo-Json -Compress -Depth 4) }
"""


@requires_windows_pwsh
class VenueQuietUnknownIsNotUnmetTests(unittest.TestCase):
    UNKNOWN_EXIT = 6

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-quiet-unknown-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.tree = self.tmp / "tree"
        self.dv = self.tree / "tools" / "profiling" / "dual-venue"
        shutil.copytree(DV, self.dv, ignore=shutil.ignore_patterns("__pycache__"))
        share = self.tmp / "share-bachelor"
        (share / "inbox").mkdir(parents=True)
        (share / "running").mkdir()
        table = json.loads((self.dv / "venues.json").read_text(encoding="utf-8"))
        table["venues"]["bachelor"]["agentShare"] = str(share)
        table["venues"]["bachelor"]["agentRoot"] = str(share) + "-root"
        (self.dv / "venues.json").write_text(json.dumps(table), encoding="utf-8")
        self.work = self.tmp / "work"

    def gate(self, mode: str, samples: str = "[]", stub: bool = True) -> subprocess.CompletedProcess:
        import os
        if stub:
            (self.tree / "tools" / "profiling" / "um-run.ps1").write_text(STUB_UM_RUN_FOR_QUIET, encoding="utf-8")
        # -MaxWaitSec 1 / -RecheckSec 1: one probe, no re-probe sleep, so the FINAL decision is the first one
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                               "-WorkDir", str(self.work), "-MaxWaitSec", "1", "-RecheckSec", "1"],
                              capture_output=True, text=True, timeout=180, env=dict(os.environ, UMSTUB_MODE=mode, UMSTUB_SAMPLES=samples))

    def assert_unknown_not_unmet(self, proc: subprocess.CompletedProcess, reason: str) -> None:
        text = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, self.UNKNOWN_EXIT, text)
        self.assertRegex(proc.stdout, r"(?m)^VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=")
        self.assertIn(f"reason={reason}", proc.stdout)
        self.assertNotRegex(proc.stdout, r"(?m)^(QUIET|COOLDOWN_UNMET) ", "an unmeasured venue must never print a QUIET or COOLDOWN_UNMET verdict")

    def test_a_null_sample_is_a_typed_unknown_verdict_with_its_own_exit_code(self) -> None:
        self.assert_unknown_not_unmet(self.gate("samples", "[5, null, 5]"), "invalid-samples")

    def test_no_samples_at_all_is_a_typed_unknown_verdict(self) -> None:
        self.assert_unknown_not_unmet(self.gate("samples", "[]"), "invalid-samples")

    def test_a_probe_with_no_venue_quiet_line_is_a_typed_unknown_verdict_with_the_why(self) -> None:
        proc = self.gate("noline")
        self.assert_unknown_not_unmet(proc, "no-VENUE_QUIET-line")
        self.assertIn("exit 1", proc.stdout)

    def test_a_failed_probe_submission_is_a_typed_unknown_verdict_with_the_why(self) -> None:
        proc = self.gate("fail")
        self.assert_unknown_not_unmet(proc, "probe-failed")
        self.assertIn("stub submission failed", proc.stdout)

    def test_a_missing_um_run_is_a_typed_unknown_verdict(self) -> None:
        self.assert_unknown_not_unmet(self.gate("samples", "[5, 5, 5]", stub=False), "probe-failed")

    def test_the_unknown_exit_code_is_used_by_no_other_path_and_the_header_lists_it(self) -> None:
        text = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(len(re.findall(r"\bexit 6\b", code)), 1, "exit 6 is reserved for the UNKNOWN verdict")
        header = text.split("[CmdletBinding", 1)[0]
        self.assertRegex(header, r"6 VENUE_QUIET_UNKNOWN")
        self.assertIn("A measured COOLDOWN_UNMET is recorded and the caller proceeds", header)
        self.assertIn("VENUE_QUIET_UNKNOWN means the venue was NOT MEASURED", header, "the header must not tell callers an UNKNOWN venue may proceed")

    def test_quiet_and_a_measured_cooldown_unmet_are_unchanged_and_still_exit_zero(self) -> None:
        proc = self.gate("samples", "[5, 5, 5]")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=5\.0% threshold=20(\.0)?% waitedSec=\d+$")
        self.assertNotIn("VENUE_QUIET_UNKNOWN", proc.stdout)
        proc = self.gate("samples", "[50, 50, 50]")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% threshold=20(\.0)?% waitedSec=\d+$")
        self.assertNotIn("VENUE_QUIET_UNKNOWN", proc.stdout)


# VENUE-QUIET-SETASIDE-RECORD-1: the agent sets a superseded marker tmp aside IN running\ as '<name>.stale-<ticks>' / '<name>.corrupt-<ticks>' and treats it as resolved
# (ultra-magnus-agent.ps1 Get-MarkerTempFiles). The gate counted it as a running job, so one such record held Bachelor at running=1 for every lane after its job had
# ended (2026-10-13 23:00Z). A set-aside record is not counted; a live final marker and an unresolved tmp still are.
@requires_windows_pwsh
class VenueQuietSetAsideRecordTests(unittest.TestCase):
    BUSY_EXIT = 3
    TMP = "dve-x-bachelor-cuda-20261013225031.started.json.d49877b02a3243aa941ee56642f74291.tmp"
    setUp = VenueQuietUnknownIsNotUnmetTests.setUp
    gate = VenueQuietUnknownIsNotUnmetTests.gate

    def busy_gate(self) -> subprocess.CompletedProcess:
        import os
        (self.tree / "tools" / "profiling" / "um-run.ps1").write_text(STUB_UM_RUN_FOR_QUIET, encoding="utf-8")
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                               "-WorkDir", str(self.work), "-MaxWaitSec", "1", "-RecheckSec", "1", "-MaxGateSec", "1", "-GatePollSec", "1"],
                              capture_output=True, text=True, timeout=180, env=dict(os.environ, UMSTUB_MODE="samples", UMSTUB_SAMPLES="[5, 5, 5]"))

    def running(self, name: str) -> None:
        share = Path(json.loads((self.dv / "venues.json").read_text(encoding="utf-8"))["venues"]["bachelor"]["agentShare"])
        (share / "running" / name).write_text("{}", encoding="utf-8")

    def test_a_stale_set_aside_record_is_not_a_running_job(self) -> None:
        self.running(self.TMP + ".stale-639275292076772648")
        proc = self.gate("samples", "[5, 5, 5]")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^GATE quiet-probe bachelor queued=0 running=0 ")
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=5\.0% ")

    def test_a_corrupt_set_aside_record_is_not_a_running_job(self) -> None:
        self.running(self.TMP + ".corrupt-639275292076772648")
        proc = self.gate("samples", "[5, 5, 5]")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^GATE quiet-probe bachelor queued=0 running=0 ")

    def test_a_legacy_shared_tmp_set_aside_record_is_not_a_running_job(self) -> None:
        # Get-MarkerTempFiles also lists the legacy shared '<id>.started.json.tmp' (r2c and earlier); the agent sets that one aside the same way.
        self.running("dve-x-bachelor-cuda-20261013225031.started.json.tmp.stale-639275292076772648")
        proc = self.gate("samples", "[5, 5, 5]")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^GATE quiet-probe bachelor queued=0 running=0 ")

    def test_a_live_final_marker_still_holds_the_gate(self) -> None:
        self.running("dve-x-bachelor-cuda-20261013225031.started.json")
        self.running(self.TMP + ".stale-639275292076772648")
        proc = self.busy_gate()
        self.assertEqual(proc.returncode, self.BUSY_EXIT, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^GATE quiet-probe bachelor queued=0 running=1 ")

    def test_an_unresolved_marker_tmp_still_holds_the_gate(self) -> None:
        self.running(self.TMP)
        proc = self.busy_gate()
        self.assertEqual(proc.returncode, self.BUSY_EXIT, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^GATE quiet-probe bachelor queued=0 running=1 ")


# ---------------------------------------------------------------------------------------------------
# VENUE-QUIET-ATTRIBUTION-1: the probe job reports schema v2 -- the v1 fields plus WHAT is using the venue (top 10 \Process(*) instances as percent of the whole machine,
# Process _Total / Idle / System, privileged / user / DPC / interrupt time, per-core load with P- and E-cores apart). The gate PRINTS the attribution after its verdict and
# decides nothing with it: the QUIET decision, threshold, cooldown, exit codes and the offline -SamplesJson output are byte-identical to HEAD~ (proved by a diff run, not here).
STUB_UM_RUN_FOR_QUIET_LINE = r"""param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec)
# test stub for tools\profiling\um-run.ps1: answers with UMSTUB_LINE verbatim as the probe's VENUE_QUIET= line
[pscustomobject]@{ exitCode = 0; stdout = 'VENUE_QUIET=' + $env:UMSTUB_LINE }
"""
V1_PROBE = {"schema": "mlv-app/venue-quiet-probe/v1", "host": "BACHELOR", "samples": [50.0, 50.0, 50.0],
            "top": [{"name": "pwsh", "pid": 4242, "cpuSeconds": 12.5}]}
V2_PROBE = {"schema": "mlv-app/venue-quiet-probe/v2", "host": "BACHELOR", "samples": [50.0, 50.0, 50.0],
            "top": [{"name": "pwsh", "pid": 4242, "cpuSeconds": 12.5}],
            "cpu_count": 16, "percent_basis": "per-process percents are percent of the whole machine (counter / cpu_count)",
            "processor": {"total_percent": 50.0, "privileged_percent": 30.0, "user_percent": 20.0, "dpc_percent": 0.5, "interrupt_percent": 1.0},
            "process_total_percent": 70.0, "idle_percent": 50.0, "system_percent": 2.5,
            "top_processes": [{"instance": "aws", "pid": 43644, "percent": 4.79}, {"instance": "system", "pid": 4, "percent": 2.5}, {"instance": "pwsh#3", "pid": None, "percent": 1.2}],
            "process_active_percent": 20.0, "unattributed_kernel_percent": 30.0,
            "attributed_percent": 8.49, "attribution_gap_points": 11.51,
            "core_topology": "logical-processor-information-ex",
            "core_classes": {"P": {"count": 8, "mean_percent": 60.0, "max_percent": 80.0}, "E": {"count": 16, "mean_percent": 40.0, "max_percent": 55.5}},
            "cores": [{"instance": "0,0", "core_class": "P", "efficiency_class": 1, "percent": 60.0}, {"instance": "0,8", "core_class": "E", "efficiency_class": 0, "percent": 40.0}],
            "notes": {}}


# ---------------------------------------------------------------------------------------------------
# VENUE-QUIET-PER-CORE-VERDICT-1: the machine-wide mean (-ThresholdPercent 20) can call a venue QUIET while one fast core runs flat out (one saturated core on 20 logical CPUs
# is ~5 % of the total; BACHELOR-HEAT-WHILE-QUIET-DIAG-1 r1 measured busiest cores of 66 % and 82-92 % under a < 20 % mean, at 100 C). The gate adds a busiest-FAST-core check
# beside the mean: LOGGED FIRST (one `  percore` line, always) and OFF by default (-PerCoreEnforce turns the BUSY verdict on). Off, every verdict, exit code and existing line is unchanged.
def _pc_cores(*rows) -> list:
    """rows: (instance, core_class, percent[, extra dict])"""
    out = []
    for r in rows:
        row = {"instance": r[0], "core_class": r[1], "efficiency_class": {"P": 1, "E": 0, "uniform": 0}.get(r[1]), "percent": r[2],
               "validSamples": 3, "totalSamples": 3, "incomplete": False}
        if len(r) > 3:
            row.update(r[3])
        out.append(row)
    return out


@requires_windows_pwsh
class VenueQuietAttributionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-quiet-attribution-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.tree = self.tmp / "tree"
        self.dv = self.tree / "tools" / "profiling" / "dual-venue"
        shutil.copytree(DV, self.dv, ignore=shutil.ignore_patterns("__pycache__"))
        share = self.tmp / "share-bachelor"
        (share / "inbox").mkdir(parents=True)
        (share / "running").mkdir()
        table = json.loads((self.dv / "venues.json").read_text(encoding="utf-8"))
        table["venues"]["bachelor"]["agentShare"] = str(share)
        table["venues"]["bachelor"]["agentRoot"] = str(share) + "-root"
        (self.dv / "venues.json").write_text(json.dumps(table), encoding="utf-8")
        (self.tree / "tools" / "profiling" / "um-run.ps1").write_text(STUB_UM_RUN_FOR_QUIET_LINE, encoding="utf-8")
        self.work = self.tmp / "work"

    def gate(self, probe: dict, *extra: str) -> subprocess.CompletedProcess:
        import os
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                               "-WorkDir", str(self.work), "-MaxWaitSec", "1", "-RecheckSec", "1", *extra],
                              capture_output=True, text=True, timeout=180, env=dict(os.environ, UMSTUB_LINE=json.dumps(probe)))

    def test_a_v2_line_is_attributed_after_the_verdict_and_the_verdict_is_unchanged(self) -> None:
        proc = self.gate(V2_PROBE)
        out = proc.stdout
        self.assertEqual(proc.returncode, 0, out + proc.stderr)
        self.assertRegex(out, r"(?m)^COOLDOWN_UNMET mean=50\.0% threshold=20(\.0)?% waitedSec=\d+$")
        self.assertRegex(out, r"(?m)^  top pwsh pid=4242 cpuSeconds=12\.5$", "the v1 top lines are still printed")
        self.assertRegex(out, r"(?m)^  attr cpus=16 processor total=50\.0% privileged=30\.0% user=20\.0% dpc=0\.5% interrupt=1\.0%$")
        self.assertRegex(out, r"(?m)^  attr process total=70\.0% idle=50\.0% system=2\.5% attributed=8\.5%$")
        self.assertRegex(out, r"(?m)^  attr gap processorTotal=50\.0% processActive=20\.0% attributed=8\.5% unattributedKernel=30\.0% gap=11\.5 points$")
        self.assertRegex(out, r"(?m)^  attr proc aws pid=43644 pct=4\.8%$")
        self.assertRegex(out, r"(?m)^  attr proc pwsh#3 pid=n/a pct=1\.2%$", "a process with no stable pid prints n/a, not a guess")
        self.assertRegex(out, r"(?m)^  attr cores topology=logical-processor-information-ex P n=8 mean=60\.0% max=80\.0% \| E n=16 mean=40\.0% max=55\.5%$")
        self.assertLess(out.index("COOLDOWN_UNMET"), out.index("  attr cpus="), "the attribution follows the verdict, it never precedes or replaces it")

    def test_a_quiet_v2_probe_is_still_quiet_and_is_attributed_too(self) -> None:
        proc = self.gate(dict(V2_PROBE, samples=[5.0, 5.0, 5.0]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=5\.0% threshold=20(\.0)?% waitedSec=\d+$")
        self.assertIn("  attr proc aws pid=43644", proc.stdout)

    def test_a_v1_line_still_parses_and_prints_no_attribution(self) -> None:
        proc = self.gate(V1_PROBE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% threshold=20(\.0)?% waitedSec=\d+$")
        self.assertRegex(proc.stdout, r"(?m)^  top pwsh pid=4242 cpuSeconds=12\.5$")
        self.assertNotIn("  attr ", proc.stdout, "a v1 probe carries no attribution; the gate must not invent any")

    def test_a_null_counter_with_a_reason_is_accepted_and_shown_as_unavailable(self) -> None:
        probe = json.loads(json.dumps(V2_PROBE))
        probe["processor"]["dpc_percent"] = None
        probe["idle_percent"] = None
        probe["attributed_percent"] = None
        probe["attribution_gap_points"] = None
        probe["core_topology"] = "unknown"
        probe["core_classes"] = {"unknown": {"count": 2, "mean_percent": 50.0, "max_percent": 60.0}}
        probe["notes"] = {"processor.dpc_percent": "counter group 'processor' returned no valid sample", "core_class": "core_class unknown: GetLogicalProcessorInformationEx failed"}
        proc = self.gate(probe)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% ")
        self.assertIn("dpc=n/a", proc.stdout)
        self.assertIn("idle=n/a", proc.stdout)
        self.assertIn("gap=n/a", proc.stdout)
        self.assertRegex(proc.stdout, r"(?m)^  attr note processor\.dpc_percent: counter group 'processor' returned no valid sample$")
        self.assertRegex(proc.stdout, r"(?m)^  attr cores topology=unknown unknown n=2 mean=50\.0% max=60\.0%$")

    def test_malformed_v2_fields_never_change_the_verdict_or_the_exit_code(self) -> None:
        proc = self.gate(dict(V2_PROBE, processor="not-an-object", top_processes=7, core_classes=None, notes=None))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% ")

    def test_an_unmeasured_venue_prints_no_attribution_and_still_exits_six(self) -> None:
        proc = self.gate(dict(V2_PROBE, samples=[5.0, None, 5.0]))
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=")
        self.assertNotIn("  attr ", proc.stdout)

    def test_the_header_states_the_percent_basis_and_the_v2_additions(self) -> None:
        header = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8").split("[CmdletBinding", 1)[0]
        self.assertIn("schema v2", header)
        self.assertIn("percent of the WHOLE machine: counter / logical processors", header)
        self.assertIn("core_class unknown", header)
        self.assertIn("The v1 `samples` and `top` fields are unchanged", header)

    def test_the_raw_v2_line_is_retained_verbatim_and_unrounded_beside_the_gate_log(self) -> None:
        probe = json.loads(json.dumps(V2_PROBE))
        probe["attributed_percent"] = 8.4912345
        probe["top_processes"][0]["percent"] = 4.7912345
        probe["top_processes"][0].update({"validSamples": 2, "totalSamples": 3, "incomplete": True})
        proc = self.gate(probe)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        files = sorted(self.work.glob("venue-quiet-probe-*.json"))
        self.assertEqual(len(files), 1, [f.name for f in self.work.iterdir()])
        raw = files[0].read_text(encoding="utf-8")
        self.assertEqual(raw, json.dumps(probe), "the raw line is retained verbatim, not re-serialized or rounded")
        self.assertEqual(json.loads(raw)["top_processes"][0]["percent"], 4.7912345)
        self.assertIn(f"  raw {files[0]}", proc.stdout.splitlines(), "the path of the raw file is printed")
        self.assertIn("  attr proc aws pid=43644 pct=4.8% valid=2/3 incomplete", proc.stdout)

    def test_the_raw_line_is_retained_for_an_unmeasured_venue_too(self) -> None:
        proc = self.gate(dict(V2_PROBE, samples=[5.0, None, 5.0]))
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertEqual(len(list(self.work.glob("venue-quiet-probe-*.json"))), 1)

    def test_the_gap_figures_are_printed_on_one_attr_line(self) -> None:
        probe = dict(V2_PROBE, process_active_percent=20.0, unattributed_kernel_percent=10.0, attribution_gap_points=5.0, attributed_percent=15.0)
        proc = self.gate(probe)
        self.assertRegex(proc.stdout, r"(?m)^  attr gap processorTotal=50\.0% processActive=20\.0% attributed=15\.0% unattributedKernel=10\.0% gap=5\.0 points$")

    def test_the_real_probe_job_emits_v2_with_every_field_typed_or_null_with_a_reason(self) -> None:
        text = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8")
        job = re.search(r"\$probeText = @'\r?\n(.*?)\r?\n'@", text, re.S)
        self.assertIsNotNone(job, "the probe job here-string must stay extractable")
        body = job.group(1)
        self.assertEqual(body.count("Start-Sleep -Seconds 12"), 1, "the 12 s sample cadence is the gate's contract; v2 must not change it")
        self.assertIn("$samples += ,$set.v1", body)
        job_file = self.tmp / "probe-job.ps1"
        job_file.write_text(body.replace("Start-Sleep -Seconds 12", "Start-Sleep -Seconds 0"), encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job_file)], capture_output=True, text=True, timeout=240)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("VENUE_QUIET=")]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        p = json.loads(lines[0][len("VENUE_QUIET="):])

        def number(v) -> bool:
            return isinstance(v, (int, float)) and not isinstance(v, bool)

        def number_or_noted(container: dict, key: str, note_key: str) -> None:
            v = container[key]
            if v is None:
                self.assertTrue(isinstance(p["notes"].get(note_key), str) and p["notes"][note_key], f"{note_key} is null without a reason")
            else:
                self.assertTrue(number(v) and v >= 0, f"{note_key}={v!r}")

        self.assertEqual(p["schema"], "mlv-app/venue-quiet-probe/v2")
        self.assertIsInstance(p["host"], str)
        # v1 fields, unchanged
        self.assertEqual(len(p["samples"]), 3)
        self.assertTrue(all(s is None or (number(s) and 0 <= s <= 100) for s in p["samples"]), p["samples"])
        self.assertIsInstance(p["top"], list)
        self.assertTrue(all(set(t) == {"name", "pid", "cpuSeconds"} for t in p["top"]), p["top"])
        # v2 additions
        self.assertTrue(isinstance(p["cpu_count"], int) and p["cpu_count"] > 0)
        self.assertIn("percent of the whole machine", p["percent_basis"])
        self.assertEqual(set(p["processor"]), {"total_percent", "privileged_percent", "user_percent", "dpc_percent", "interrupt_percent"})
        for k in p["processor"]:
            number_or_noted(p["processor"], k, f"processor.{k}")
        for k in ("process_total_percent", "idle_percent", "system_percent", "process_active_percent", "unattributed_kernel_percent", "attribution_gap_points"):
            number_or_noted(p, k, k)
        self.assertIsInstance(p["top_processes"], list)
        self.assertLessEqual(len(p["top_processes"]), 10)
        pcts = [t["percent"] for t in p["top_processes"]]
        self.assertEqual(pcts, sorted(pcts, reverse=True), "the ranking is descending")
        for t in p["top_processes"]:
            self.assertIsInstance(t["instance"], str)
            self.assertNotIn(t["instance"], ("_total", "idle"), "_Total and Idle are never ranked")
            self.assertTrue(t["pid"] is None or isinstance(t["pid"], int))
            self.assertTrue(number(t["percent"]) and t["percent"] >= 0)
            self.assertTrue(1 <= t["validSamples"] <= t["totalSamples"] == 3, t)
            self.assertIs(t["incomplete"], t["validSamples"] < t["totalSamples"])
        if p["top_processes"]:
            self.assertTrue(number(p["attributed_percent"]))
        self.assertTrue(p["attributed_percent"] is None or number(p["attributed_percent"]))
        self.assertTrue(p["attribution_gap_points"] is None or number(p["attribution_gap_points"]))
        self.assertIn(p["core_topology"], ("logical-processor-information-ex", "unknown"))
        self.assertIsInstance(p["core_classes"], dict)
        self.assertIsInstance(p["cores"], list)
        if not p["cores"]:
            self.assertIn("cores", p["notes"], "no per-core data and no reason")
        for c in p["cores"]:
            self.assertRegex(c["instance"], r"^\d+,\d+$")
            self.assertIn(c["core_class"], ("P", "E", "uniform", "unknown"))
            self.assertTrue(c["efficiency_class"] is None or isinstance(c["efficiency_class"], int))
            self.assertTrue(number(c["percent"]) and c["percent"] >= 0)
            if p["core_topology"] == "unknown":
                self.assertEqual(c["core_class"], "unknown", "a topology that could not be derived must say unknown, never guess a class")
        if p["core_topology"] == "unknown":
            self.assertIn("core_class", p["notes"])
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) and v for k, v in p["notes"].items()))

    QUIET_SAMPLES = [10.0, 10.0, 10.0]

    def probe(self, cores: list, samples: list | None = None) -> dict:
        return dict(json.loads(json.dumps(V2_PROBE)), samples=self.QUIET_SAMPLES if samples is None else samples, cores=cores)

    def percore_lines(self, proc: subprocess.CompletedProcess) -> list:
        return [ln for ln in proc.stdout.splitlines() if ln.startswith("  percore ")]

    HOT = ("0,2", "P", 82.0)
    COOL = ("0,0", "P", 30.0)
    EHOT = ("0,8", "E", 95.0)

    def test_off_by_default_a_quiet_mean_stays_quiet_and_the_line_records_what_the_check_would_have_said(self) -> None:
        proc = self.gate(self.probe(_pc_cores(self.COOL, self.HOT, self.EHOT)))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=10\.0% threshold=20(\.0)?% waitedSec=\d+$", "off: the verdict is the mean's, unchanged")
        self.assertEqual(self.percore_lines(proc),
                         ["  percore enabled=false threshold=60.0% busiest_fast_core=0_2 busiest_fast_pct=82.0% would_block=true"])

    def test_on_a_hot_fast_core_under_a_quiet_mean_is_busy_and_names_the_core(self) -> None:
        proc = self.gate(self.probe(_pc_cores(self.COOL, self.HOT)), "-PerCoreEnforce")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=10\.0% threshold=20(\.0)?% waitedSec=\d+$")
        self.assertNotRegex(proc.stdout, r"(?m)^QUIET ")
        self.assertRegex(proc.stdout, r"(?m)^PROBE \S+ bachelor samples=10\.0/10\.0/10\.0 mean=10\.0% state=BUSY note=percore core=0_2 ")
        lines = self.percore_lines(proc)
        self.assertEqual(lines[0], "  percore enabled=true threshold=60.0% busiest_fast_core=0_2 busiest_fast_pct=82.0% would_block=true")
        self.assertIn("  percore blocked core=0_2 percent=82.0% threshold=60.0%", lines)

    def test_on_a_cool_fast_core_leaves_the_quiet_verdict_alone(self) -> None:
        proc = self.gate(self.probe(_pc_cores(self.COOL, ("0,2", "P", 47.0), self.EHOT)), "-PerCoreEnforce")
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=10\.0% ")
        self.assertEqual(self.percore_lines(proc), ["  percore enabled=true threshold=60.0% busiest_fast_core=0_2 busiest_fast_pct=47.0% would_block=false"])

    def test_the_threshold_is_inclusive_and_a_parameter(self) -> None:
        at = self.gate(self.probe(_pc_cores(("0,2", "P", 60.0))))
        self.assertIn("would_block=true", at.stdout, "busiest >= threshold")
        raised = self.gate(self.probe(_pc_cores(self.HOT)), "-PerCoreThresholdPercent", "90", "-PerCoreEnforce")
        self.assertRegex(raised.stdout, r"(?m)^QUIET ")
        self.assertIn("threshold=90.0% busiest_fast_core=0_2 busiest_fast_pct=82.0% would_block=false", raised.stdout)

    def test_an_efficiency_core_never_counts_and_a_uniform_class_is_fast(self) -> None:
        e_only_hot = self.gate(self.probe(_pc_cores(self.COOL, self.EHOT)), "-PerCoreEnforce")
        self.assertRegex(e_only_hot.stdout, r"(?m)^QUIET ", "a busy E-core is not the fast-core check's business")
        self.assertIn("busiest_fast_core=0_0 busiest_fast_pct=30.0% would_block=false", e_only_hot.stdout)
        uniform = self.gate(self.probe(_pc_cores(("0,0", "uniform", 20.0), ("0,1", "uniform", 71.0))))
        self.assertIn("busiest_fast_core=0_1 busiest_fast_pct=71.0% would_block=true", uniform.stdout)

    def test_the_unrounded_per_core_mean_decides_not_the_two_decimal_display(self) -> None:
        # the probe rounds `percent` to 2 dp; percent_exact is the unrounded valid-sample mean. 59.9996 prints as 60.0 but is below 60.
        core = _pc_cores(("0,2", "P", 60.0, {"percent_exact": 59.9996}))
        proc = self.gate(self.probe(core), "-PerCoreEnforce")
        self.assertRegex(proc.stdout, r"(?m)^QUIET ", "three 20.04 % samples are BUSY at 20 for the mean rule; the same unrounded rule applies per core")
        self.assertIn("would_block=false", proc.stdout)
        over = self.gate(self.probe(_pc_cores(("0,2", "P", 60.0, {"percent_exact": 60.0004}))))
        self.assertIn("would_block=true", over.stdout)

    def test_an_unknown_class_core_makes_the_answer_null_with_a_reason_and_never_blocks(self) -> None:
        for flags in ((), ("-PerCoreEnforce",)):
            proc = self.gate(self.probe(_pc_cores(self.HOT, ("0,5", "unknown", 20.0))), *flags)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertRegex(proc.stdout, r"(?m)^QUIET mean=10\.0% ", f"unknown never blocks (flags={flags})")
            line = self.percore_lines(proc)[0]
            self.assertIn("would_block=null reason=core-class-unknown", line)
            self.assertNotIn("would_block=false", line, "never a silent false")

    def test_no_cores_is_null_with_a_reason(self) -> None:
        for drop_field in (False, True):
            probe = self.probe([])
            if drop_field:
                del probe["cores"]
            proc = self.gate(probe)
            self.assertRegex(proc.stdout, r"(?m)^QUIET mean=10\.0% ")
            self.assertEqual(self.percore_lines(proc), ["  percore enabled=false threshold=60.0% busiest_fast_core=n/a busiest_fast_pct=n/a would_block=null reason=no-cores"])

    def test_an_incomplete_fast_core_is_null_with_a_reason_but_an_incomplete_efficiency_core_is_not(self) -> None:
        partial = {"validSamples": 2, "incomplete": True}
        bad = self.gate(self.probe(_pc_cores(("0,2", "P", 82.0, partial), self.COOL)), "-PerCoreEnforce")
        self.assertRegex(bad.stdout, r"(?m)^QUIET ", "unknown is not a block")
        self.assertIn("would_block=null reason=incomplete-samples", bad.stdout)
        fine = self.gate(self.probe(_pc_cores(self.COOL, ("0,8", "E", 90.0, partial))))
        self.assertIn("would_block=false", fine.stdout)

    def test_a_non_numeric_core_percent_is_null_not_zero(self) -> None:
        proc = self.gate(self.probe(_pc_cores(("0,2", "P", "hot"), self.COOL)))
        self.assertIn("would_block=null reason=invalid-core-percent", proc.stdout)

    def test_a_v1_probe_prints_no_percore_line_off_and_a_null_line_on(self) -> None:
        off = self.gate(V1_PROBE)
        self.assertEqual(self.percore_lines(off), [], "a probe without v2 prints exactly what it printed before")
        on = self.gate(V1_PROBE, "-PerCoreEnforce")
        self.assertRegex(on.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% ")
        self.assertIn("would_block=null reason=no-cores", on.stdout)

    def test_the_probe_fixture_keeps_an_explicit_empty_sample_list(self) -> None:
        self.assertEqual(self.probe([], samples=[])["samples"], [], "an explicit empty list is not 'use the default'")
        self.assertEqual(self.probe([])["samples"], self.QUIET_SAMPLES)

    def test_a_core_bearing_v1_probe_logs_the_observation_with_enforcement_off(self) -> None:
        # sol r3 V-02: the observation was gated on schema v2, so a v1 probe that carries cores[] logged nothing while the header promises one line per cores[] probe
        v1 = {"schema": "mlv-app/venue-quiet-probe/v1", "host": "BACHELOR", "samples": [10, 10, 10], "cores": _pc_cores(self.HOT)}
        proc = self.gate(v1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=10\.0% ", "off: logged only, the verdict is the mean's")
        self.assertEqual(self.percore_lines(proc),
                         ["  percore enabled=false threshold=60.0% busiest_fast_core=0_2 busiest_fast_pct=82.0% would_block=true"])

    def test_an_unavailable_probe_logs_one_typed_null_observation_when_enforcing(self) -> None:
        # sol r3 V-02: a failed probe left probe = $null, so no observation at all; it must be one null line with a typed reason, and the verdict stays UNKNOWN (exit 6)
        import os
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                               "-WorkDir", str(self.work), "-MaxWaitSec", "1", "-RecheckSec", "1", "-PerCoreEnforce"],
                              capture_output=True, text=True, timeout=180, env=dict(os.environ, UMSTUB_LINE="{ not json"))
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertEqual(self.percore_lines(proc),
                         ["  percore enabled=true threshold=60.0% busiest_fast_core=n/a busiest_fast_pct=n/a would_block=null reason=no-probe"])
        self.assertIn("VENUE_QUIET_UNKNOWN", proc.stdout)
        off = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                              "-WorkDir", str(self.work), "-MaxWaitSec", "1", "-RecheckSec", "1"],
                             capture_output=True, text=True, timeout=180, env=dict(os.environ, UMSTUB_LINE="{ not json"))
        self.assertEqual(off.returncode, 6, off.stdout + off.stderr)
        self.assertEqual(self.percore_lines(off), [], "off and no cores[] to report: nothing to log")

    def test_the_mean_rule_still_wins_when_the_mean_is_busy(self) -> None:
        proc = self.gate(self.probe(_pc_cores(self.COOL), samples=[50.0, 50.0, 50.0]), "-PerCoreEnforce")
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% ")
        self.assertIn("would_block=false", proc.stdout)
        self.assertNotIn("percore blocked", proc.stdout, "blocked is only for a mean-QUIET venue the per-core check overrode")

    def test_the_offline_decision_output_is_unchanged(self) -> None:
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-SamplesJson", "[12.5, 18.0, 21.0]"],
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.stdout.strip(), "DECISION QUIET mean=17.2%", proc.stdout + proc.stderr)

    def test_the_header_documents_the_default_threshold_and_the_evidence(self) -> None:
        header = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8").split("[CmdletBinding", 1)[0]
        for token in ("VENUE-QUIET-PER-CORE-VERDICT-1", "-PerCoreEnforce", "-PerCoreThresholdPercent", "default 60", "OFF by default", "would_block"):
            self.assertIn(token, header)

    def test_the_real_probe_job_emits_the_unrounded_core_mean_beside_the_rounded_one(self) -> None:
        text = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8")
        body = re.search(r"\$probeText = @'\r?\n(.*?)\r?\n'@", text, re.S).group(1)
        self.assertIn("percent_exact", body)

    # ---- adversary pass r1 (A1 A2 A3 A4 A5 A6 A7) ---------------------------------------------------------------
    SEQ_STUB = r"""param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec)
# test stub for tools\profiling\um-run.ps1: answers probe N of the JSON array in UMSTUB_LINES (the last one repeats); UMSTUB_COUNTER holds N
$arr = @(ConvertFrom-Json -InputObject $env:UMSTUB_LINES)
$n = 0; if (Test-Path -LiteralPath $env:UMSTUB_COUNTER) { $n = [int](Get-Content -LiteralPath $env:UMSTUB_COUNTER -TotalCount 1) }
Set-Content -LiteralPath $env:UMSTUB_COUNTER -Value ($n + 1)
$obj = $arr[[Math]::Min($n, $arr.Count - 1)]
[pscustomobject]@{ exitCode = 0; stdout = 'VENUE_QUIET=' + ($obj | ConvertTo-Json -Compress -Depth 9) }
"""

    def gate_seq(self, probes: list, *extra: str) -> subprocess.CompletedProcess:
        import os
        (self.tree / "tools" / "profiling" / "um-run.ps1").write_text(self.SEQ_STUB, encoding="utf-8")
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                               "-WorkDir", str(self.work), "-MaxWaitSec", "120", "-RecheckSec", "1", "-ForeignFloorProbes", "0", *extra],
                              capture_output=True, text=True, timeout=240,
                              env=dict(os.environ, UMSTUB_LINES=json.dumps(probes), UMSTUB_COUNTER=str(self.tmp / "seq.counter")))

    def test_every_probe_logs_its_own_percore_line_not_only_the_last(self) -> None:
        busy_hot = self.probe(_pc_cores(self.HOT), samples=[50.0, 50.0, 50.0])
        quiet_cool = self.probe(_pc_cores(self.COOL))
        proc = self.gate_seq([busy_hot, quiet_cool])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(self.percore_lines(proc),
                         ["  percore enabled=false threshold=60.0% busiest_fast_core=0_2 busiest_fast_pct=82.0% would_block=true",
                          "  percore enabled=false threshold=60.0% busiest_fast_core=0_0 busiest_fast_pct=30.0% would_block=false"],
                         "the first probe's observation must survive the second probe")
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=10\.0% ")

    def test_an_unmeasured_probe_that_carries_cores_still_logs_the_observation(self) -> None:
        proc = self.gate(self.probe(_pc_cores(self.HOT), samples=[5.0, None, 5.0]), "-PerCoreEnforce")
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertEqual(self.percore_lines(proc),
                         ["  percore enabled=true threshold=60.0% busiest_fast_core=0_2 busiest_fast_pct=82.0% would_block=true"],
                         "logged, but an UNKNOWN venue is still UNKNOWN: no blocked line and exit 6")

    def test_a_core_omitted_for_having_no_valid_sample_makes_the_answer_null(self) -> None:
        probe = self.probe(_pc_cores(self.COOL, self.EHOT))
        probe["notes"] = {"cores_omitted": "1 core(s) had no valid sample and are omitted"}
        proc = self.gate(probe, "-PerCoreEnforce")
        self.assertRegex(proc.stdout, r"(?m)^QUIET ", "unknown never blocks")
        self.assertIn("busiest_fast_core=0_0 busiest_fast_pct=30.0% would_block=null reason=cores-omitted", proc.stdout)

    def test_fewer_valid_samples_than_total_is_incomplete_even_without_the_flag(self) -> None:
        proc = self.gate(self.probe(_pc_cores(("0,2", "P", 82.0, {"validSamples": 2, "totalSamples": 3, "incomplete": False}))))
        self.assertIn("would_block=null reason=incomplete-samples", proc.stdout)

    def test_a_non_numeric_exact_percent_is_invalid_and_never_falls_back_to_the_rounded_one(self) -> None:
        proc = self.gate(self.probe(_pc_cores(("0,2", "P", 60.0, {"percent_exact": "hot"}))))
        self.assertIn("would_block=null reason=invalid-core-percent", proc.stdout)
        arr = self.gate(self.probe(_pc_cores(("0,2", "P", [1]))))
        self.assertIn("would_block=null reason=invalid-core-percent", arr.stdout, "a one-element array is not a number")

    def test_the_percore_line_is_culture_invariant_and_comma_free(self) -> None:
        import os
        cmd = ("[Threading.Thread]::CurrentThread.CurrentCulture = [Globalization.CultureInfo]::GetCultureInfo('de-DE'); "
               f"& '{self.dv / 'Wait-VenueQuiet.ps1'}' -Venue bachelor -WorkDir '{self.work}' -MaxWaitSec 1 -RecheckSec 1")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=180,
                              env=dict(os.environ, UMSTUB_LINE=json.dumps(self.probe(_pc_cores(self.HOT)))))
        self.assertEqual(self.percore_lines(proc),
                         ["  percore enabled=false threshold=60.0% busiest_fast_core=0_2 busiest_fast_pct=82.0% would_block=true"], proc.stdout + proc.stderr)

    def test_a_gate_log_sink_failure_in_the_percore_writer_never_escapes(self) -> None:
        script = (DV / "Wait-VenueQuiet.ps1").as_posix()
        cmd = (f"$t=$null;$e=$null;$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$t,[ref]$e);"
               "foreach($n in 'Format-PerCorePct','ConvertTo-IdentToken','Write-PerCoreLines'){ $f=$ast.Find({param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -ceq $n},$true); . ([scriptblock]::Create($f.Extent.Text)) }; "
               "function Write-Gate([string]$Line) { Write-Output $Line; throw 'sink locked' }; "
               "$pc=[pscustomobject]@{enabled=$false;threshold=60.0;busiest_fast_core='0,2';busiest_fast_percent=82.0;would_block=$true;reason=$null}; "
               "try { Write-PerCoreLines $pc $true; 'NO_ESCAPE' } catch { 'ESCAPED ' + $_.Exception.Message }")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120)
        self.assertIn("NO_ESCAPE", proc.stdout, proc.stdout + proc.stderr)
        self.assertNotIn("ESCAPED", proc.stdout)

    def test_the_busy_probe_summary_follows_the_gate_when_per_core_enforcement_is_on(self) -> None:
        raw = self.tmp / "venue-quiet-probe-20261026T000000000Z.json"
        raw.write_text(json.dumps(self.probe(_pc_cores(self.HOT, self.COOL))), encoding="utf-8")
        summarize = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Summarize-VenueBusyProbes.ps1"), "-ProbeJson", str(raw)]
        off = subprocess.run(summarize, capture_output=True, text=True, timeout=120)
        self.assertEqual(off.returncode, 0, off.stdout + off.stderr)
        self.assertIn("Busy probes summarized: 0 (quiet skipped 1", off.stdout, "default: the mean rule, as before")
        on = subprocess.run(summarize + ["-PerCoreEnforce"], capture_output=True, text=True, timeout=120)
        self.assertEqual(on.returncode, 0, on.stdout + on.stderr)
        self.assertIn("Busy probes summarized: 1 (quiet skipped 0", on.stdout, "enforcing: the gate would have called this probe BUSY")
        cool = self.tmp / "venue-quiet-probe-20261026T000100000Z.json"
        cool.write_text(json.dumps(self.probe(_pc_cores(self.COOL))), encoding="utf-8")
        raised = subprocess.run([*summarize[:-1], str(cool), "-PerCoreEnforce"], capture_output=True, text=True, timeout=120)
        self.assertIn("Busy probes summarized: 0 (quiet skipped 1", raised.stdout)

    # ---- adversary pass r2 (A1 A2 A3): a malformed core row is UNKNOWN (null), never a definite true or false, in the gate and in the summary ----
    def test_a_malformed_core_row_is_unknown_in_the_gate_and_never_busy_in_the_summary(self) -> None:
        hot = {"instance": "0,2", "core_class": "P", "efficiency_class": 1, "percent": 82.0, "validSamples": 3, "totalSamples": 3, "incomplete": False}
        cases = {
            "null percent_exact": ([dict(hot, percent_exact=None)], "invalid-core-percent"),
            "null row beside a hot row": ([None, dict(hot)], "invalid-core-row"),
            "null row beside a cool row": ([None, dict(hot, instance="0,0", percent=30.0)], "invalid-core-row"),
            "string totalSamples": ([dict(hot, totalSamples="3")], "incomplete-samples"),
            "null validSamples": ([dict(hot, validSamples=None)], "incomplete-samples"),
        }
        for name, (cores, reason) in cases.items():
            with self.subTest(name):
                probe = self.probe(cores)
                proc = self.gate(probe, "-PerCoreEnforce")
                self.assertRegex(proc.stdout, r"(?m)^QUIET ", proc.stdout + proc.stderr)
                self.assertIn(f"would_block=null reason={reason}", proc.stdout)
                self.assertNotIn("percore blocked", proc.stdout)
                raw = self.tmp / f"venue-quiet-probe-{abs(hash(name))}.json"
                raw.write_text(json.dumps(probe), encoding="utf-8")
                summ = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Summarize-VenueBusyProbes.ps1"),
                                       "-ProbeJson", str(raw), "-PerCoreEnforce"], capture_output=True, text=True, timeout=120)
                self.assertIn("Busy probes summarized: 0 (quiet skipped 1", summ.stdout, summ.stdout + summ.stderr)

    # ---- adversary pass r4 (A1 A2 A5): row SHAPE and sample COUNTS are validated exactly, not coerced; the same helper serves the gate and the summary ----
    def test_a_row_of_the_wrong_shape_or_an_impossible_count_is_unknown_in_the_gate_and_the_summary(self) -> None:
        hot = {"instance": "0,2", "core_class": "P", "efficiency_class": 1, "percent": 82.0, "validSamples": 3, "totalSamples": 3, "incomplete": False}
        cases = {
            "a row nested in an array": ([[dict(hot, validSamples="3", totalSamples="3")]], "invalid-core-row"),
            "a nested array beside a cool row": ([[dict(hot)], dict(hot, instance="0,0", percent=30.0)], "invalid-core-row"),
            "a scalar row": ([82], "invalid-core-row"),
            "a lone null row": ([None], "invalid-core-row"),
            "an array core_class": ([dict(hot, core_class=["P"])], "core-class-unknown"),
            "counts beyond double precision": ([dict(hot, validSamples=9007199254740992, totalSamples=9007199254740993)], "incomplete-samples"),
            "zero of zero samples": ([dict(hot, validSamples=0, totalSamples=0)], "incomplete-samples"),
            "negative counts": ([dict(hot, validSamples=-1, totalSamples=-1)], "incomplete-samples"),
            "fractional counts": ([dict(hot, validSamples=3.5, totalSamples=3)], "incomplete-samples"),
            "more valid than total": ([dict(hot, validSamples=4, totalSamples=3)], "incomplete-samples"),
            # adversary pass r4 round 2 (A1 A2): the incomplete flag is a boolean or it is not evidence; notes is an object or it cannot say nothing was omitted
            "an object incomplete flag": ([dict(hot, incomplete={})], "incomplete-samples"),
            "a string incomplete flag": ([dict(hot, incomplete="false")], "incomplete-samples"),
            "a numeric incomplete flag": ([dict(hot, incomplete=0)], "incomplete-samples"),
            "a null incomplete flag": ([dict(hot, incomplete=None)], "incomplete-samples"),
            "notes as an array": ([dict(hot)], "invalid-notes", {"notes": [{"cores_omitted": "1 core had no valid sample and was omitted"}]}),
            "notes as a string": ([dict(hot)], "invalid-notes", {"notes": "cores_omitted"}),
        }
        for name, (cores, reason, *extra) in cases.items():
            with self.subTest(name):
                probe = dict(self.probe(cores), **(extra[0] if extra else {}))
                proc = self.gate(probe, "-PerCoreEnforce")
                self.assertRegex(proc.stdout, r"(?m)^QUIET ", proc.stdout + proc.stderr)
                self.assertIn(f"would_block=null reason={reason}", proc.stdout)
                self.assertNotIn("percore blocked", proc.stdout)
                raw = self.tmp / f"venue-quiet-probe-r4-{abs(hash(name))}.json"
                raw.write_text(json.dumps(probe), encoding="utf-8")
                summ = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Summarize-VenueBusyProbes.ps1"),
                                       "-ProbeJson", str(raw), "-PerCoreEnforce"], capture_output=True, text=True, timeout=120)
                self.assertIn("Busy probes summarized: 0 (quiet skipped 1", summ.stdout, summ.stdout + summ.stderr)

    # ---- r5 (sol r4 V-03 / Dot B3): a PRESENT notes property whose value is null is invalid-notes, never a definite verdict; an ABSENT property and an empty object are unchanged ----
    def _summary_for(self, probe: dict, tag: str) -> subprocess.CompletedProcess:
        raw = self.tmp / f"venue-quiet-probe-r5-{tag}.json"
        raw.write_text(json.dumps(probe), encoding="utf-8")
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Summarize-VenueBusyProbes.ps1"),
                               "-ProbeJson", str(raw), "-PerCoreEnforce"], capture_output=True, text=True, timeout=120)

    def test_an_explicit_null_notes_is_invalid_notes_in_the_gate_and_the_summary(self) -> None:
        for name, cores in {"hot 82": _pc_cores(self.HOT), "cool 30": _pc_cores(self.COOL)}.items():
            with self.subTest(name):
                probe = dict(self.probe(cores), notes=None)
                self.assertIn('"notes": null', json.dumps(probe))
                proc = self.gate(probe, "-PerCoreEnforce")
                self.assertRegex(proc.stdout, r"(?m)^QUIET ", proc.stdout + proc.stderr)
                self.assertIn("would_block=null reason=invalid-notes", proc.stdout)
                self.assertNotIn("percore blocked", proc.stdout)
                off = self.gate(probe)
                self.assertIn("would_block=null reason=invalid-notes", off.stdout, "the observation line reports the same typed unknown when enforcement is off")
                self.assertRegex(off.stdout, r"(?m)^QUIET ")
                summ = self._summary_for(probe, name.replace(" ", "-"))
                self.assertIn("Busy probes summarized: 0 (quiet skipped 1", summ.stdout, summ.stdout + summ.stderr)

    def test_an_absent_notes_property_and_an_empty_notes_object_keep_the_definite_verdict(self) -> None:
        for name, extra in {"absent": None, "empty object": {}}.items():
            with self.subTest(name):
                hot = self.probe(_pc_cores(self.HOT))
                cool = self.probe(_pc_cores(self.COOL))
                for p in (hot, cool):
                    p.pop("notes", None)
                    if extra is not None:
                        p["notes"] = extra
                self.assertEqual("notes" in hot, extra is not None)
                blocked = self.gate(hot, "-PerCoreEnforce")
                self.assertIn("would_block=true", blocked.stdout, blocked.stdout + blocked.stderr)
                self.assertNotIn("reason=invalid-notes", blocked.stdout)
                self.assertIn("percore blocked", blocked.stdout)
                self.assertIn("would_block=false", self.gate(cool, "-PerCoreEnforce").stdout)
                self.assertIn("Busy probes summarized: 1 (quiet skipped 0", self._summary_for(hot, "ok-" + name.replace(" ", "-")).stdout)

    # ---- r6 (sol r5 B4 / Dot B4): a malformed ACCEPTED sample must not stop the probe being logged first: the PROBE line, the percore lines and the typed UNKNOWN exit 6 all still happen ----
    def test_a_malformed_sample_still_logs_the_probe_and_percore_lines_and_ends_unknown(self) -> None:
        cases = {
            "string sample": ([10, "hot", 10], "10.0/bad:string/10.0"),
            "object sample": ([10, {"x": 1}, 10], "10.0/bad:object/10.0"),
            "nested array sample": ([10, [1, 2], 10], "10.0/bad:array/10.0"),
            "boolean sample": ([10, True, 10], "10.0/bad:bool/10.0"),
        }
        for name, (samples, shown) in cases.items():
            for flags in ((), ("-PerCoreEnforce",)):
                with self.subTest(name, enforce=bool(flags)):
                    proc = self.gate(self.probe(_pc_cores(self.HOT), samples=samples), *flags)
                    out = proc.stdout + proc.stderr
                    self.assertEqual(proc.returncode, 6, out)
                    self.assertRegex(proc.stdout, r"(?m)^PROBE venue-quiet-probe-\S+ bachelor samples=" + re.escape(shown) + r" mean=UNKNOWN state=UNKNOWN", out)
                    self.assertIn(f"  percore enabled={'true' if flags else 'false'} threshold=60.0% busiest_fast_core=0_2 busiest_fast_pct=82.0% would_block=true", proc.stdout, out)
                    self.assertRegex(proc.stdout, r"(?m)^VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=", out)
                    self.assertNotIn("Cannot convert", out)
                    self.assertNotIn("percore blocked", proc.stdout, "an UNKNOWN mean is never turned into a measured BUSY by the per-core check")

    # ---- r6 adversary pass (A1 A2): cores[] is an ARRAY of rows and a fast row names its core as "<group>,<core>"; anything else is typed unknown, never a definite verdict or a made-up core name ----
    def test_a_cores_value_that_is_not_an_array_or_a_fast_row_without_a_core_name_is_unknown(self) -> None:
        hot = _pc_cores(self.HOT)[0]
        nameless = {k: v for k, v in hot.items() if k != "instance"}
        cases = {
            "cores as one object": (dict(hot), "invalid-cores"),
            "cores as a string": ("0,2", "invalid-cores"),
            "cores as a number": (82, "invalid-cores"),
            "missing instance": ([nameless], "invalid-core-instance"),
            "array instance": ([dict(hot, instance=["0,0", "0,2"])], "invalid-core-instance"),
            "numeric instance": ([dict(hot, instance=2)], "invalid-core-instance"),
            "empty instance": ([dict(hot, instance="")], "invalid-core-instance"),
            "instance that is not group,core": ([dict(hot, instance="cpu2")], "invalid-core-instance"),
            "null instance": ([dict(hot, instance=None)], "invalid-core-instance"),
            # adversary pass r6 round 2 (A1): .NET `$` accepts a trailing line feed, and \d accepts non-ASCII digits; the name is exactly ASCII digits, comma, ASCII digits
            "instance with a trailing line feed": ([dict(hot, instance="0,2\n")], "invalid-core-instance"),
            "instance with Arabic-Indic digits": ([dict(hot, instance="٠,٢")], "invalid-core-instance"),
        }
        for name, (cores, reason) in cases.items():
            with self.subTest(name):
                probe = self.probe(cores)
                for flags in (("-PerCoreEnforce",), ()):
                    proc = self.gate(probe, *flags)
                    self.assertRegex(proc.stdout, r"(?m)^QUIET ", proc.stdout + proc.stderr)
                    self.assertIn(f"would_block=null reason={reason}", proc.stdout)
                    self.assertNotIn("percore blocked", proc.stdout)
                    self.assertNotIn("0_0_0_2", proc.stdout)
                self.assertIn("Busy probes summarized: 0 (quiet skipped 1", self._summary_for(probe, "r6-" + str(abs(hash(name)))).stdout)

    def test_an_efficiency_core_row_without_a_core_name_does_not_make_the_check_unknown(self) -> None:
        ecore = {k: v for k, v in _pc_cores(self.EHOT)[0].items() if k != "instance"}
        proc = self.gate(self.probe(_pc_cores(self.COOL) + [ecore]), "-PerCoreEnforce")
        self.assertIn("would_block=false", proc.stdout, proc.stdout + proc.stderr)

    # ---- adversary pass r4 (A4): a sink that fails on the observation line must not also swallow the blocked-core line ----
    def test_a_sink_failure_on_the_observation_line_still_prints_the_blocked_core_line(self) -> None:
        script = (DV / "Wait-VenueQuiet.ps1").as_posix()
        cmd = (f"$t=$null;$e=$null;$ast=[System.Management.Automation.Language.Parser]::ParseFile('{script}',[ref]$t,[ref]$e);"
               "foreach($n in 'Format-PerCorePct','ConvertTo-IdentToken','Write-PerCoreLines'){ $f=$ast.Find({param($x) $x -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $x.Name -ceq $n},$true); . ([scriptblock]::Create($f.Extent.Text)) }; "
               "function Write-Gate([string]$Line) { Write-Output $Line; if ($Line -like '  percore enabled=*') { throw 'sink locked' } }; "
               "$pc=[pscustomobject]@{enabled=$true;threshold=60.0;busiest_fast_core='0,2';busiest_fast_percent=82.0;would_block=$true;reason=$null}; "
               "try { Write-PerCoreLines $pc $true; 'NO_ESCAPE' } catch { 'ESCAPED ' + $_.Exception.Message }")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd], capture_output=True, text=True, timeout=120)
        self.assertIn("NO_ESCAPE", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("  percore blocked core=0_2 percent=82.0% threshold=60.0%", proc.stdout)


# ---------------------------------------------------------------------------------------------------
# VENUE-QUIET-ATTRIBUTION-1 r2 (hub ruling): every per-process and per-core figure is the mean over the samples whose counter status is VALID for that instance (never
# zero-filled), rows carry validSamples / totalSamples and an incomplete flag, process rows are keyed on (instance, ID Process), the gap is |processActive - attributed|
# with unattributedKernel reported beside it, and the raw v2 JSON line is retained unrounded in -WorkDir. The real probe job runs here against a canned Get-Counter
# (a function shadowing the cmdlet), so the aggregation is proved on exact numbers, not on whatever the test host is doing.
INVALID_STATUS = 3221228474  # PDH_CSTATUS_INVALID_DATA 0xC0000BBA


def _cs(obj: str, inst: str, leaf: str, value: float, status: int = 0) -> dict:
    return {"Path": f"\\\\TESTHOST\\{obj}({inst})\\{leaf}", "CookedValue": value, "Status": status}


def _proc(inst: str, pid: int, raw: float, valid: bool = True) -> list:
    return [_cs("process", inst, "% processor time", raw, 0 if valid else INVALID_STATUS), _cs("process", inst, "id process", pid)]


def _core(inst: str, value: float, valid: bool = True) -> list:
    return [_cs("processor information", inst, "% processor time", value, 0 if valid else INVALID_STATUS)]


def _flat(*parts) -> list:
    return [s for part in parts for s in part]


GET_COUNTER_PRELUDE = r"""$script:Canned = @(ConvertFrom-Json -InputObject @'
__CANNED__
'@)
$script:CallNo = 0
function Get-Counter {
    [CmdletBinding()] param($Counter)
    $set = $script:Canned[$script:CallNo]; $script:CallNo++
    [pscustomobject]@{ CounterSamples = @($set.s | ForEach-Object { [pscustomobject]@{ Path = $_.Path; CookedValue = [double]$_.CookedValue; Status = [uint32]$_.Status } }) }
}
"""

# Canned Get-Process for the top[] before/after snapshots: call 1 is the "before" list, call 2 the "after" list. Each entry is
# {id, name, cpu (seconds), start (FILETIME or null = StartTime unreadable)}.
GET_PROCESS_PRELUDE = r"""$script:Snaps = @(ConvertFrom-Json -InputObject @'
__SNAPSHOTS__
'@)
$script:SnapNo = 0
function Get-Process {
    [CmdletBinding()] param()
    $snap = $script:Snaps[$script:SnapNo]; $script:SnapNo++
    foreach ($e in @($snap)) {
        $st = $null; if ($null -ne $e.start) { $st = [datetime]::FromFileTimeUtc([int64]$e.start) }
        [pscustomobject]@{ Id = [int]$e.id; Name = [string]$e.name; TotalProcessorTime = [TimeSpan]::FromSeconds([double]$e.cpu); StartTime = $st }
    }
}
"""


@requires_windows_pwsh
class VenueQuietValidSampleTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-quiet-valid-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def probe(self, sets: list, cpus: int = 4, snapshots: list | None = None) -> dict:
        text = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8")
        body = re.search(r"\$probeText = @'\r?\n(.*?)\r?\n'@", text, re.S).group(1)
        self.assertIn("[Environment]::ProcessorCount", body)
        body = body.replace("[Environment]::ProcessorCount", str(cpus)).replace("Start-Sleep -Seconds 12", "Start-Sleep -Seconds 0")
        canned = json.dumps([{"s": s} for s in sets])
        job = self.tmp / "canned-probe-job.ps1"
        prelude = GET_COUNTER_PRELUDE.replace("__CANNED__", canned)
        if snapshots is not None:
            prelude += GET_PROCESS_PRELUDE.replace("__SNAPSHOTS__", json.dumps(snapshots))
        job.write_text(prelude + body, encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job)], capture_output=True, text=True, timeout=180)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("VENUE_QUIET=")]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        return json.loads(lines[0][len("VENUE_QUIET="):])

    @staticmethod
    def by_instance(rows: list, instance: str) -> list:
        return [r for r in rows if r["instance"] == instance]

    def test_a_process_invalid_in_the_middle_sample_is_the_mean_of_its_valid_samples_not_zero_filled(self) -> None:
        # sol's repro: 4 CPUs, worker pid 123 raw 200 in all three samples, the middle one's status invalid, Idle valid throughout -> 50.0 (not 33.33)
        sets = [_flat(_proc("worker", 123, 200), _proc("idle", 0, 100)),
                _flat(_proc("worker", 123, 200, valid=False), _proc("idle", 0, 100)),
                _flat(_proc("worker", 123, 200), _proc("idle", 0, 100))]
        p = self.probe(sets)
        rows = self.by_instance(p["top_processes"], "worker")
        self.assertEqual(len(rows), 1, p["top_processes"])
        self.assertEqual(rows[0]["percent"], 50.0, "the valid-sample mean is 50.0; 33.33 means the invalid sample was zero-filled")
        self.assertEqual((rows[0]["pid"], rows[0]["validSamples"], rows[0]["totalSamples"]), (123, 2, 3))
        self.assertIs(rows[0]["incomplete"], True)
        self.assertEqual(p["attributed_percent"], 50.0)
        self.assertTrue([k for k in p["notes"] if "incomplete" in k], p["notes"])
        self.assertRegex(" ".join(v for k, v in p["notes"].items() if "incomplete" in k), r"\b1\b")

    def test_a_complete_row_is_not_flagged_and_adds_no_incomplete_note(self) -> None:
        sets = [_flat(_proc("worker", 123, 200), _proc("idle", 0, 100)) for _ in range(3)]
        p = self.probe(sets)
        row = self.by_instance(p["top_processes"], "worker")[0]
        self.assertEqual((row["percent"], row["validSamples"], row["totalSamples"], row["incomplete"]), (50.0, 3, 3, False))
        self.assertFalse([k for k in p["notes"] if "process_incomplete" in k], p["notes"])

    def test_a_core_invalid_in_the_middle_sample_is_the_mean_of_its_valid_samples_not_zero_filled(self) -> None:
        sets = [_flat(_core("0,0", 40), _proc("idle", 0, 100)),
                _flat(_core("0,0", 40, valid=False), _proc("idle", 0, 100)),
                _flat(_core("0,0", 40), _proc("idle", 0, 100))]
        p = self.probe(sets)
        self.assertEqual(len(p["cores"]), 1, p["cores"])
        core = p["cores"][0]
        self.assertEqual(core["percent"], 40.0, "26.67 means the invalid core sample was zero-filled")
        self.assertEqual((core["validSamples"], core["totalSamples"]), (2, 3))
        self.assertIs(core["incomplete"], True)
        self.assertTrue([k for k in p["notes"] if "cores_incomplete" in k], p["notes"])
        self.assertEqual(p["core_classes"][core["core_class"]]["mean_percent"], 40.0)

    def test_a_row_with_no_valid_sample_is_omitted_from_the_sum_and_counted_in_a_note(self) -> None:
        sets = [_flat(_proc("worker", 123, 200), _proc("ghost", 9, 400, valid=False), _core("0,0", 40), _core("0,1", 99, valid=False)) for _ in range(3)]
        p = self.probe(sets)
        self.assertEqual(self.by_instance(p["top_processes"], "ghost"), [], "an all-invalid row must not be ranked")
        self.assertEqual(p["attributed_percent"], 50.0, "the all-invalid row contributes nothing to the attributed sum")
        self.assertEqual([c["instance"] for c in p["cores"]], ["0,0"], "an all-invalid core is omitted, not reported as 0")
        omitted = " ".join(v for k, v in p["notes"].items() if "omitted" in k)
        self.assertIn("process", " ".join(k for k in p["notes"] if "omitted" in k))
        self.assertIn("cores", " ".join(k for k in p["notes"] if "omitted" in k))
        self.assertGreaterEqual(len(re.findall(r"\b1\b", omitted)), 2, p["notes"])

    def test_one_instance_name_under_two_pids_is_two_rows_never_blended(self) -> None:
        # bash#21 is pid 10 (raw 40 = 10%) in samples 1-2, then #21 is reassigned to pid 20 (raw 80 = 20%) in sample 3
        sets = [_flat(_proc("bash#21", 10, 40), _proc("idle", 0, 100)),
                _flat(_proc("bash#21", 10, 40), _proc("idle", 0, 100)),
                _flat(_proc("bash#21", 20, 80), _proc("idle", 0, 100))]
        p = self.probe(sets)
        rows = {r["pid"]: r for r in self.by_instance(p["top_processes"], "bash#21")}
        self.assertEqual(set(rows), {10, 20}, p["top_processes"])
        self.assertEqual((rows[10]["percent"], rows[10]["validSamples"], rows[10]["totalSamples"]), (10.0, 2, 3))
        self.assertEqual((rows[20]["percent"], rows[20]["validSamples"], rows[20]["totalSamples"]), (20.0, 1, 3))
        self.assertTrue(rows[10]["incomplete"] and rows[20]["incomplete"])
        self.assertNotIn(13.33, [r["percent"] for r in p["top_processes"]], "13.33 is the two processes blended under one name")

    def test_the_gap_is_process_active_minus_attributed_with_unattributed_kernel_beside_it(self) -> None:
        # 4 CPUs: Processor(_Total)=30; Process(_Total) raw 160 = 40, Idle raw 80 = 20 -> processActive 20; a=10 + b=5 -> attributed 15; gap 5; unattributedKernel 30-20=10
        sets = [_flat([_cs("processor", "_total", "% processor time", 30)],_proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40), _proc("b", 2, 20)) for _ in range(3)]
        p = self.probe(sets)
        self.assertEqual(p["processor"]["total_percent"], 30.0)
        self.assertEqual(p["process_active_percent"], 20.0)
        self.assertEqual(p["attributed_percent"], 15.0)
        self.assertEqual(p["unattributed_kernel_percent"], 10.0)
        self.assertEqual(p["attribution_gap_points"], 5.0)
        self.assertEqual((p["process_total_percent"], p["idle_percent"]), (40.0, 20.0))

    def test_the_gap_is_absolute_when_attribution_overshoots_process_active(self) -> None:
        sets = [_flat([_cs("processor", "_total", "% processor time", 30)],_proc("_total", 0, 160), _proc("idle", 0, 120), _proc("a", 1, 40), _proc("b", 2, 20)) for _ in range(3)]
        p = self.probe(sets)
        self.assertEqual(p["process_active_percent"], 10.0)
        self.assertEqual(p["attributed_percent"], 15.0)
        self.assertEqual(p["attribution_gap_points"], 5.0, "the gap is |processActive - attributed|")
        self.assertEqual(p["unattributed_kernel_percent"], 20.0)

    # VENUE-QUIET-PROCESS-ACTIVE-NEGATIVE-1 (hosted CI flake, run 37927671797: process_active_percent=-12.05): Process(_Total) and Process(Idle) are read at slightly different
    # instants, so (_Total - Idle) can be negative for a sample on a near-idle host. Rule: a negative per-sample value is INVALID -- excluded from the mean (never clamped, never
    # averaged in); with no valid sample left the field is null with a notes reason, and the derived gap / unattributedKernel follow (null with their own reason).
    def test_a_negative_active_sample_is_excluded_from_the_mean_never_averaged_in_or_clamped(self) -> None:
        # 4 CPUs: Processor(_Total)=30; samples 1 and 3 have _Total raw 160 (40) and Idle raw 80 (20) -> active 20; sample 2 has Idle above _Total (raw 100 - 120 = -20 -> -5)
        good = lambda: _flat([_cs("processor", "_total", "% processor time", 30)], _proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40), _proc("b", 2, 20))
        skew = _flat([_cs("processor", "_total", "% processor time", 30)], _proc("_total", 0, 100), _proc("idle", 0, 120), _proc("a", 1, 40), _proc("b", 2, 20))
        p = self.probe([good(), skew, good()])
        self.assertEqual(p["process_active_percent"], 20.0, "-5.0 in the mean would give 11.67; clamping the sample to 0 would give 13.33")
        self.assertGreaterEqual(p["process_active_percent"], 0)
        self.assertEqual(p["attributed_percent"], 15.0)
        self.assertEqual(p["attribution_gap_points"], 5.0, "the gap follows the excluded-sample mean")
        self.assertEqual(p["unattributed_kernel_percent"], 10.0, "unattributedKernel follows the excluded-sample mean")
        self.assertRegex(p["notes"]["process_active_dropped_samples"], r"^1 of 3 sample sets")
        self.assertNotIn("process_active_percent", p["notes"], "a field that has a value carries no null reason")
        self.assertEqual((p["process_total_percent"], p["idle_percent"]), (35.0, 23.33), "the _Total / Idle means are the unfiltered counter means, unchanged")

    def test_when_every_active_sample_is_negative_the_field_is_null_with_a_reason_and_the_derived_fields_follow(self) -> None:
        skew = lambda: _flat([_cs("processor", "_total", "% processor time", 30)], _proc("_total", 0, 100), _proc("idle", 0, 120), _proc("a", 1, 40), _proc("b", 2, 20))
        p = self.probe([skew(), skew(), skew()])
        self.assertIsNone(p["process_active_percent"])
        self.assertIsNone(p["attribution_gap_points"])
        self.assertIsNone(p["unattributed_kernel_percent"])
        self.assertIn("3 of 3", p["notes"]["process_active_percent"])
        for k in ("attribution_gap_points", "unattributed_kernel_percent"):
            self.assertTrue(isinstance(p["notes"].get(k), str) and p["notes"][k], f"{k} is null without a reason")
        self.assertNotIn("process_active_dropped_samples", p["notes"])
        self.assertEqual(p["attributed_percent"], 15.0, "the attribution itself is unaffected")
        for k in ("process_active_percent", "attribution_gap_points", "unattributed_kernel_percent", "attributed_percent"):
            self.assertTrue(p[k] is None or p[k] >= 0, f"{k}={p[k]!r}")

    def test_a_zero_active_sample_is_valid_and_kept(self) -> None:
        # _Total == Idle is a legitimate fully-idle sample (0), not a skew: it stays in the mean
        flat = lambda tot, idle: _flat([_cs("processor", "_total", "% processor time", 30)], _proc("_total", 0, tot), _proc("idle", 0, idle), _proc("a", 1, 40))
        p = self.probe([flat(80, 80), flat(160, 80), flat(80, 80)])
        self.assertEqual(p["process_active_percent"], 6.67)
        self.assertNotIn("process_active_dropped_samples", p["notes"])

    # VENUE-QUIET-PROCESS-ACTIVE-NEGATIVE-1 r2 (hosted CI, PR #342 merge_group run 37931263307, shard 6/8: unattributed_kernel_percent=-0.16): unattributedKernel is
    # Processor(_Total) minus Process active, two different counter sets, so it can be negative on skew even when process_active_percent is not. Rule: computed per sample set
    # from the one Get-Counter call; a negative per-sample value is INVALID (excluded from the mean, never clamped); none valid -> null with a notes reason.
    # attribution_gap_points is |processActive - attributed| and cannot be negative; every other percent field is a direct counter mean or a sum of non-negative means.
    def test_a_negative_unattributed_kernel_sample_is_excluded_from_the_mean_never_averaged_in_or_clamped(self) -> None:
        # 4 CPUs: Process active 20 in every sample; Processor(_Total) 30, 15, 30 -> per-sample kernel 10, -5 (invalid), 10
        mk = lambda cpu: _flat([_cs("processor", "_total", "% processor time", cpu)], _proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40), _proc("b", 2, 20))
        p = self.probe([mk(30), mk(15), mk(30)])
        self.assertEqual(p["process_active_percent"], 20.0, "every active sample is valid")
        self.assertEqual(p["processor"]["total_percent"], 25.0, "the processor mean is the unfiltered counter mean")
        self.assertEqual(p["unattributed_kernel_percent"], 10.0, "the old processor.total - process_active gives 5.0; averaging -5 in gives 5.0; clamping it to 0 gives 6.67")
        self.assertGreaterEqual(p["unattributed_kernel_percent"], 0)
        self.assertRegex(p["notes"]["unattributed_kernel_dropped_samples"], r"^1 of 3 sample sets")
        self.assertNotIn("unattributed_kernel_percent", p["notes"], "a field that has a value carries no null reason")
        self.assertEqual(p["attribution_gap_points"], 5.0)

    def test_when_every_unattributed_kernel_sample_is_negative_the_field_is_null_with_a_reason(self) -> None:
        # this is the shape of the hosted flake: processor 20 against Process active 20.16 -> -0.16 in every sample (4 CPUs: raw 160 - 79.36 = 80.64 -> 20.16)
        sk = lambda: _flat([_cs("processor", "_total", "% processor time", 20)], _proc("_total", 0, 160), _proc("idle", 0, 79.36), _proc("a", 1, 40), _proc("b", 2, 20))
        p = self.probe([sk(), sk(), sk()])
        self.assertEqual(p["process_active_percent"], 20.16)
        self.assertIsNone(p["unattributed_kernel_percent"])
        self.assertIn("3 of 3", p["notes"]["unattributed_kernel_percent"])
        self.assertNotIn("unattributed_kernel_dropped_samples", p["notes"])
        self.assertEqual(p["attribution_gap_points"], 5.16, "the gap is unaffected")
        for k in ("process_active_percent", "attribution_gap_points", "unattributed_kernel_percent", "attributed_percent"):
            self.assertTrue(p[k] is None or p[k] >= 0, f"{k}={p[k]!r}")

    def test_a_zero_unattributed_kernel_sample_is_valid_and_kept(self) -> None:
        # Processor(_Total) == Process active is a legitimate 0, not a skew
        mk = lambda cpu: _flat([_cs("processor", "_total", "% processor time", cpu)], _proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40))
        p = self.probe([mk(20), mk(30), mk(20)])
        self.assertEqual(p["unattributed_kernel_percent"], 3.33)
        self.assertNotIn("unattributed_kernel_dropped_samples", p["notes"])

    # VENUE-QUIET-ACTIVE-PAIR-MISSING-NOTE-1 (fable/sol hardening on PR #347 r2): a dropped-samples note divides by the sets that HAD the valid pair (kept + dropped), and a set with a
    # missing pair member is named in its own note. No value, verdict or threshold moves.
    def test_a_set_with_total_valid_and_idle_missing_is_not_counted_as_a_pair_in_the_dropped_note(self) -> None:
        # 4 CPUs: set 1 pair valid (active 20); set 2 has Process(_Total) but NO Process(Idle); set 3 is a skewed pair (Idle above _Total -> negative)
        cpu = [_cs("processor", "_total", "% processor time", 30)]
        s1 = _flat(cpu, _proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40))
        s2 = _flat(cpu, _proc("_total", 0, 160), _proc("a", 1, 40))
        s3 = _flat(cpu, _proc("_total", 0, 100), _proc("idle", 0, 120), _proc("a", 1, 40))
        p = self.probe([s1, s2, s3])
        self.assertEqual(p["process_active_percent"], 20.0, "the value is the mean of the one valid pair")
        self.assertRegex(p["notes"]["process_active_dropped_samples"], r"^1 of 2 sample sets", "the set with no Idle never was a pair, so 1 of 3 overstates the pairs read")
        self.assertRegex(p["notes"]["process_active_dropped_samples"], r"mean of the other 1\)")
        self.assertRegex(p["notes"]["process_active_pair_missing"], r"^1 set\(s\) had no valid Process\(Idle\)/Process\(_Total\) pair")

    def test_a_set_with_idle_valid_and_total_invalid_is_named_as_a_missing_pair_and_adds_no_dropped_note(self) -> None:
        cpu = [_cs("processor", "_total", "% processor time", 30)]
        ok = lambda: _flat(cpu, _proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40))
        bad = _flat(cpu, _proc("_total", 0, 160, valid=False), _proc("idle", 0, 80), _proc("a", 1, 40))
        p = self.probe([ok(), bad, ok()])
        self.assertEqual(p["process_active_percent"], 20.0)
        self.assertNotIn("process_active_dropped_samples", p["notes"], "nothing read negative")
        self.assertRegex(p["notes"]["process_active_pair_missing"], r"^1 set\(s\)")

    def test_a_complete_set_of_pairs_adds_no_pair_missing_note(self) -> None:
        cpu = [_cs("processor", "_total", "% processor time", 30)]
        p = self.probe([_flat(cpu, _proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40)) for _ in range(3)])
        self.assertNotIn("process_active_pair_missing", p["notes"])
        self.assertNotIn("unattributed_kernel_pair_missing", p["notes"])
        self.assertNotIn("top_pid_reuse_dropped", p["notes"])

    def test_the_all_negative_reason_counts_only_the_sets_that_had_a_pair(self) -> None:
        cpu = [_cs("processor", "_total", "% processor time", 30)]
        skew = lambda: _flat(cpu, _proc("_total", 0, 100), _proc("idle", 0, 120), _proc("a", 1, 40))
        nopair = _flat(cpu, _proc("_total", 0, 100), _proc("a", 1, 40))
        p = self.probe([skew(), nopair, skew()])
        self.assertIsNone(p["process_active_percent"])
        self.assertIn("2 of 2 sample sets", p["notes"]["process_active_percent"], "2 of 3 counts the set that never had an Idle sample as a pair")
        self.assertRegex(p["notes"]["process_active_pair_missing"], r"^1 set\(s\)")

    def test_a_kernel_sample_set_with_processor_total_null_lands_in_the_pair_missing_note_not_the_denominator(self) -> None:
        # Process active 20 in every set; Processor(_Total) 30 / ABSENT / 15 -> kernel 10, no pair, -5 (invalid)
        proc = lambda: _flat(_proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40))
        cpu = lambda v: [_cs("processor", "_total", "% processor time", v)]
        p = self.probe([_flat(cpu(30), proc()), _flat(proc()), _flat(cpu(15), proc())])
        self.assertEqual(p["unattributed_kernel_percent"], 10.0, "the value is the mean of the one valid kernel sample")
        self.assertRegex(p["notes"]["unattributed_kernel_dropped_samples"], r"^1 of 2 sample sets")
        self.assertRegex(p["notes"]["unattributed_kernel_pair_missing"], r"^1 set\(s\) had no valid Processor\(_Total\)/Process active pair")
        self.assertNotIn("process_active_pair_missing", p["notes"], "the active pair itself was complete in every set")

    def test_the_all_negative_kernel_reason_divides_by_the_sets_that_had_both_members(self) -> None:
        proc = lambda: _flat(_proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40))
        cpu = lambda v: [_cs("processor", "_total", "% processor time", v)]
        p = self.probe([_flat(cpu(15), proc()), _flat(proc()), _flat(cpu(15), proc())])
        self.assertIsNone(p["unattributed_kernel_percent"])
        self.assertIn("2 of 2 sample sets", p["notes"]["unattributed_kernel_percent"], "2 of 3 counts the set with no Processor(_Total) as a pair read")
        self.assertRegex(p["notes"]["unattributed_kernel_pair_missing"], r"^1 set\(s\)")

    # VENUE-QUIET-TOP-CPUSECONDS-PID-REUSE-1: top[].cpuSeconds is after-minus-before, so it is keyed on (pid, process start time); a pid reused between the two
    # snapshots is a different process and is dropped and counted, never a negative or a foreign cpuSeconds.
    T1, T2, T3 = 133000000000000000, 133000000500000000, 133000001000000000

    def pid_probe(self, before: list, after: list) -> dict:
        cpu = [_cs("processor", "_total", "% processor time", 30)]
        sets = [_flat(cpu, _proc("_total", 0, 160), _proc("idle", 0, 80), _proc("a", 1, 40)) for _ in range(3)]
        return self.probe(sets, snapshots=[before, after])

    def test_a_pid_reused_between_snapshots_is_dropped_never_a_negative_cpuseconds(self) -> None:
        before = [{"id": 100, "name": "old", "cpu": 50.0, "start": self.T1}, {"id": 200, "name": "steady", "cpu": 10.0, "start": self.T3}]
        after = [{"id": 100, "name": "new", "cpu": 2.0, "start": self.T2}, {"id": 200, "name": "steady", "cpu": 15.0, "start": self.T3}]
        p = self.pid_probe(before, after)
        self.assertEqual([(t["name"], t["pid"], t["cpuSeconds"]) for t in p["top"]], [("steady", 200, 5.0)], "-48.0 for pid 100 is the old process's time subtracted from another's")
        self.assertTrue(all(t["cpuSeconds"] >= 0 for t in p["top"]))
        self.assertRegex(p["notes"]["top_pid_reuse_dropped"], r"^1 process")

    def test_a_pid_reused_by_a_busier_process_does_not_report_its_whole_cpu_time_as_the_old_ones(self) -> None:
        before = [{"id": 100, "name": "old", "cpu": 1.0, "start": self.T1}, {"id": 200, "name": "steady", "cpu": 10.0, "start": self.T3}]
        after = [{"id": 100, "name": "new", "cpu": 90.0, "start": self.T2}, {"id": 200, "name": "steady", "cpu": 11.0, "start": self.T3}]
        p = self.pid_probe(before, after)
        self.assertEqual([(t["pid"], t["cpuSeconds"]) for t in p["top"]], [(200, 1.0)], "89.0 would be a foreign process's lifetime CPU attributed to the window")
        self.assertRegex(p["notes"]["top_pid_reuse_dropped"], r"^1 process")

    def test_an_unreadable_start_time_cannot_be_verified_so_it_is_dropped_and_counted(self) -> None:
        before = [{"id": 100, "name": "locked", "cpu": 1.0, "start": None}, {"id": 200, "name": "steady", "cpu": 10.0, "start": self.T3}]
        after = [{"id": 100, "name": "locked", "cpu": 9.0, "start": None}, {"id": 200, "name": "steady", "cpu": 12.0, "start": self.T3}]
        p = self.pid_probe(before, after)
        self.assertEqual([(t["pid"], t["cpuSeconds"]) for t in p["top"]], [(200, 2.0)])
        self.assertRegex(p["notes"]["top_pid_reuse_dropped"], r"^1 process")

    def test_the_same_process_in_both_snapshots_keeps_its_cpuseconds_and_adds_no_note(self) -> None:
        before = [{"id": 100, "name": "busy", "cpu": 10.0, "start": self.T1}, {"id": 200, "name": "steady", "cpu": 10.0, "start": self.T3}]
        after = [{"id": 100, "name": "busy", "cpu": 17.5, "start": self.T1}, {"id": 200, "name": "steady", "cpu": 11.0, "start": self.T3}]
        p = self.pid_probe(before, after)
        self.assertEqual([(t["name"], t["pid"], t["cpuSeconds"]) for t in p["top"]], [("busy", 100, 7.5), ("steady", 200, 1.0)])
        self.assertNotIn("top_pid_reuse_dropped", p["notes"])

    def test_the_header_documents_the_pair_denominator_and_the_pid_reuse_rule(self) -> None:
        header = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8").split("[CmdletBinding", 1)[0]
        for needle in ("process_active_pair_missing", "unattributed_kernel_pair_missing", "top_pid_reuse_dropped", "process start time"):
            self.assertIn(needle, header)

    def test_the_header_documents_the_valid_sample_mean_the_pid_keying_and_the_gap(self) -> None:
        header = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8").split("[CmdletBinding", 1)[0]
        for needle in ("VALID-SAMPLE MEAN", "never zero-filled", "PID-KEYED", "(instance name, ID Process)", "processActive", "unattributedKernel", "|processActive - attributed|", "RAW RETENTION"):
            self.assertIn(needle, header)


# ---------------------------------------------------------------------------------------------------
# BACHELOR-VENUE-FOREIGN-FLOOR-FAST-FAIL-1: on Bachelor the busy floor is FOREIGN (explorer, TextInputHost, Bloody7, other projects' guardians and sessions; MLV-owned CPU was
# about 1 point in the BACHELOR-UNGATED-PRIVILEGED-LOAD-2 sample), so a leg waited the whole -MaxWaitSec (1800 s) for a floor that never dropped and then ran as COOLDOWN_UNMET anyway.
# Now N (-ForeignFloorProbes, default 3, 0 = off) consecutive MEASURED busy probes whose top owners are not MLV's, with no downward trend (newest mean not below the first of the N by
# more than -ForeignFloorTrendMarginPoints, default 2.0), print `FOREIGN_FLOOR ...` and then the SAME `COOLDOWN_UNMET mean=` line and exit 0. It is never QUIET.
# The probe is a stub um-run.ps1 that answers call k with line k (the last repeats) of UMSTUB_SEQ, so the live loop is driven by synthetic probes; nothing is submitted.
STUB_UM_RUN_SEQUENCE = r"""param([string]$ScriptPath, [string]$JobId, [string]$AgentShare, [int]$TimeoutSec, [int]$MaxQueueWaitSec, [int]$MaxClaimedWaitSec)
# test stub for tools\profiling\um-run.ps1: the n-th call (counted in UMSTUB_STATE) answers with line n of the UMSTUB_SEQ file verbatim, the last line repeating
$lines = @(Get-Content -LiteralPath $env:UMSTUB_SEQ | Where-Object { $_ })
$n = $(if (Test-Path -LiteralPath $env:UMSTUB_STATE) { [int](Get-Content -LiteralPath $env:UMSTUB_STATE) } else { 0 })
Set-Content -LiteralPath $env:UMSTUB_STATE -Value ($n + 1)
[pscustomobject]@{ exitCode = 0; stdout = 'VENUE_QUIET=' + $lines[[Math]::Min($n, $lines.Count - 1)] }
"""
FOREIGN_OWNERS = [("explorer", 17204, 3.66), ("textinputhost", 23596, 1.91), ("bloody7", 21612, 1.48), ("claude#4", 9120, 1.1), ("pwsh#3", 67876, 0.86)]


def foreign_probe(mean, owners=FOREIGN_OWNERS, **extra) -> dict:
    top = [{"instance": n, "pid": p, "percent": pc} for (n, p, pc) in owners]
    return dict(V2_PROBE, samples=[mean, mean, mean], top_processes=top, **extra)


@requires_windows_pwsh
class VenueQuietForeignFloorTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-quiet-foreign-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.tree = self.tmp / "tree"
        self.dv = self.tree / "tools" / "profiling" / "dual-venue"
        shutil.copytree(DV, self.dv, ignore=shutil.ignore_patterns("__pycache__"))
        share = self.tmp / "share-bachelor"
        (share / "inbox").mkdir(parents=True)
        (share / "running").mkdir()
        table = json.loads((self.dv / "venues.json").read_text(encoding="utf-8"))
        table["venues"]["bachelor"]["agentShare"] = str(share)
        table["venues"]["bachelor"]["agentRoot"] = str(share) + "-root"
        (self.dv / "venues.json").write_text(json.dumps(table), encoding="utf-8")
        (self.tree / "tools" / "profiling" / "um-run.ps1").write_text(STUB_UM_RUN_SEQUENCE, encoding="utf-8")
        self.work = self.tmp / "work"

    def gate(self, probes: list, max_wait: int = 8, *extra: str) -> subprocess.CompletedProcess:
        import os
        seq = self.tmp / "seq.jsonl"
        seq.write_text("\n".join(json.dumps(p) for p in probes) + "\n", encoding="utf-8")
        state = self.tmp / "state.txt"
        state.unlink(missing_ok=True)
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                               "-WorkDir", str(self.work), "-MaxWaitSec", str(max_wait), "-RecheckSec", "1", *extra],
                              capture_output=True, text=True, timeout=180, env=dict(os.environ, UMSTUB_SEQ=str(seq), UMSTUB_STATE=str(state)))

    @staticmethod
    def probe_lines(proc: subprocess.CompletedProcess) -> int:
        return len(re.findall(r"(?m)^PROBE venue-quiet-probe-", proc.stdout))

    def assert_waited_the_whole_cooldown(self, proc: subprocess.CompletedProcess, min_probes: int = 5) -> None:
        text = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, text)
        self.assertNotIn("FOREIGN_FLOOR", proc.stdout, text)
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=")
        self.assertGreaterEqual(self.probe_lines(proc), min_probes, text)

    def test_three_flat_busy_probes_with_foreign_owners_exit_early_as_foreign_floor_then_cooldown_unmet(self) -> None:
        proc = self.gate([foreign_probe(35.0)], 20)
        out = proc.stdout
        self.assertEqual(proc.returncode, 0, out + proc.stderr)
        m = re.search(r"(?m)^FOREIGN_FLOOR owners=(\S+) floorMean=(\d+\.\d)% waitedSec=(\d+) probes=(\d+)$", out)
        self.assertIsNotNone(m, out)
        self.assertEqual(m.group(1), "explorer:17204,textinputhost:23596,bloody7:21612", "the top 3 owners, name:pid, in rank order; pwsh (the probe's own shell) is never an owner")
        self.assertEqual(m.group(2), "35.0")
        self.assertEqual(m.group(4), "3")
        self.assertLessEqual(int(m.group(3)), 8, "the wait ends after probe 3, far below -MaxWaitSec 20")
        self.assertEqual(self.probe_lines(proc), 3, out)
        self.assertRegex(out, r"(?m)^COOLDOWN_UNMET mean=35\.0% threshold=20(\.0)?% waitedSec=\d+$", "the SAME verdict line callers parse today")
        self.assertLess(out.index("FOREIGN_FLOOR"), out.index("COOLDOWN_UNMET mean="), "the typed line comes first, the verdict line stays the last word")
        self.assertNotRegex(out, r"(?m)^QUIET ", "an early exit is never QUIET")
        self.assertRegex(out, r"(?m)^  attr proc explorer pid=17204 ", "the attribution of the last probe still follows the verdict")

    def test_a_falling_floor_keeps_waiting_as_today(self) -> None:
        self.assert_waited_the_whole_cooldown(self.gate([foreign_probe(m) for m in (99, 92, 85, 78, 71, 64, 57, 50, 43, 36)]))

    def test_an_mlv_owned_top_owner_keeps_waiting_as_today(self) -> None:
        owners = [("explorer", 17204, 3.66), ("mlvapp#1", 4040, 3.0), ("textinputhost", 23596, 1.91), ("bloody7", 21612, 1.48)]
        self.assert_waited_the_whole_cooldown(self.gate([foreign_probe(35.0, owners)]))

    def test_the_agent_and_um_run_count_as_mlv_owned_too(self) -> None:
        for name in ("mlv-agent", "ultra-magnus-agent", "um-run"):
            with self.subTest(name=name):
                owners = [(name, 4040, 3.0), ("explorer", 17204, 2.0), ("textinputhost", 23596, 1.9), ("bloody7", 21612, 1.4)]
                self.assert_waited_the_whole_cooldown(self.gate([foreign_probe(35.0, owners)], 6), 4)

    def test_an_mlv_row_below_the_top_three_foreign_owners_does_not_block(self) -> None:
        owners = [("explorer", 17204, 3.66), ("textinputhost", 23596, 1.91), ("bloody7", 21612, 1.48), ("mlvapp", 4040, 0.2)]
        proc = self.gate([foreign_probe(35.0, owners)], 20)
        self.assertIn("FOREIGN_FLOOR owners=explorer:17204,textinputhost:23596,bloody7:21612 ", proc.stdout)

    def test_a_quiet_probe_is_quiet_and_never_a_foreign_floor(self) -> None:
        proc = self.gate([foreign_probe(5.0)])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=5\.0% ")
        self.assertNotIn("FOREIGN_FLOOR", proc.stdout)
        self.assertNotIn("COOLDOWN_UNMET", proc.stdout)
        proc = self.gate([foreign_probe(35.0), foreign_probe(35.0), foreign_probe(5.0)])
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=5\.0% ")
        self.assertNotIn("FOREIGN_FLOOR", proc.stdout, "two busy probes are below the default three")

    def test_foreign_floor_probes_zero_is_the_old_behaviour(self) -> None:
        self.assert_waited_the_whole_cooldown(self.gate([foreign_probe(35.0)], 8, "-ForeignFloorProbes", "0"))

    def test_a_larger_probe_count_waits_for_that_many(self) -> None:
        proc = self.gate([foreign_probe(35.0)], 20, "-ForeignFloorProbes", "5")
        self.assertRegex(proc.stdout, r"(?m)^FOREIGN_FLOOR owners=\S+ floorMean=35\.0% waitedSec=\d+ probes=5$")
        self.assertEqual(self.probe_lines(proc), 5, proc.stdout)

    def test_an_unreadable_probe_is_still_exit_six_and_never_a_foreign_floor(self) -> None:
        unreadable = dict(foreign_probe(35.0), samples=[35.0, None, 35.0])
        proc = self.gate([unreadable])
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^VENUE_QUIET_UNKNOWN mean=UNKNOWN threshold=")
        self.assertNotIn("FOREIGN_FLOOR", proc.stdout)
        # busy, busy, then unreadable for the rest: the FINAL decision is UNKNOWN, so exit 6 and no early exit
        proc = self.gate([foreign_probe(35.0), foreign_probe(35.0), unreadable], 6)
        self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
        self.assertNotIn("FOREIGN_FLOOR", proc.stdout)

    def test_an_unreadable_probe_resets_the_consecutive_count(self) -> None:
        unreadable = dict(foreign_probe(35.0), samples=[35.0, None, 35.0])
        proc = self.gate([foreign_probe(35.0), foreign_probe(35.0), unreadable, foreign_probe(35.0)], 20)
        self.assertRegex(proc.stdout, r"FOREIGN_FLOOR .* probes=3")
        self.assertEqual(self.probe_lines(proc), 6, "busy x2, unknown, then busy x3 consecutive: fires on probe 6, not probe 3")

    def test_a_v1_probe_that_names_no_owner_keeps_waiting(self) -> None:
        self.assert_waited_the_whole_cooldown(self.gate([dict(V1_PROBE, samples=[35.0, 35.0, 35.0])]))
        self.assert_waited_the_whole_cooldown(self.gate([foreign_probe(35.0, [])]))
        self.assert_waited_the_whole_cooldown(self.gate([foreign_probe(35.0, [("pwsh", 1, 2.0), ("powershell#2", 2, 1.0)])]))

    def test_the_trend_margin_is_exact(self) -> None:
        # 50 -> 48.0: a drop of exactly 2.0 is NOT more than the margin, so probe 3 fires
        proc = self.gate([foreign_probe(50.0), foreign_probe(49.0), foreign_probe(48.0)], 20)
        self.assertEqual(self.probe_lines(proc), 3, proc.stdout)
        self.assertIn("FOREIGN_FLOOR ", proc.stdout)
        # 50 -> 47.9: a drop of 2.1 is a downward trend; the window slides and fires on probe 4 (48.5, 47.9, 47.9)
        proc = self.gate([foreign_probe(50.0), foreign_probe(48.5), foreign_probe(47.9)], 20)
        self.assertEqual(self.probe_lines(proc), 4, proc.stdout)
        self.assertIn("FOREIGN_FLOOR ", proc.stdout)
        # a rising floor is not falling
        proc = self.gate([foreign_probe(30.0), foreign_probe(40.0), foreign_probe(50.0)], 20)
        self.assertEqual(self.probe_lines(proc), 3, proc.stdout)

    def test_a_wider_margin_parameter_is_honoured(self) -> None:
        proc = self.gate([foreign_probe(50.0), foreign_probe(48.5), foreign_probe(47.9)], 20, "-ForeignFloorTrendMarginPoints", "5")
        self.assertEqual(self.probe_lines(proc), 3, proc.stdout)

    def test_the_header_documents_the_foreign_floor_line_the_parameters_and_the_unchanged_exit_codes(self) -> None:
        text = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8")
        header = text.split("[CmdletBinding", 1)[0]
        for needle in ("FOREIGN_FLOOR owners=<name:pid", "floorMean=", "-ForeignFloorProbes", "-ForeignFloorTrendMarginPoints", "never QUIET", "COOLDOWN_UNMET"):
            self.assertIn(needle, header)
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(len(re.findall(r"\bexit 6\b", code)), 1, "exit 6 stays reserved for the UNKNOWN verdict")
        self.assertEqual(len(re.findall(r"\bexit 0\b", code)), 2, "offline decision + the one verdict exit: the early exit adds no exit of its own")
        self.assertNotRegex(code, r"(?m)^\s*Write-Gate \"QUIET", "no QUIET line outside the verdict")


# ---------------------------------------------------------------------------------------------------
class LegSpecSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is required")
        self.jsonschema = jsonschema
        self.schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))

    def test_the_shipped_legs_validate_and_cover_speed_and_look(self) -> None:
        legs = sorted((DV / "legs").glob("*.json"))
        self.assertGreaterEqual(len(legs), 2)
        types = set()
        for path in legs:
            spec = json.loads(path.read_text(encoding="utf-8"))
            self.jsonschema.validate(spec, self.schema)
            self.assertTrue(spec["backends"], path.name)
            types.add(spec["legType"])
        self.assertEqual(types, {"speed", "look"})
        # (the speed leg and the classic look leg still run as cuda AND cpu; the scale-2 look leg is cpu-only until the CUDA route honours scale 2)
        for name in ("m16-1243-speed", "m16-1243-look"):
            self.assertEqual(sorted(json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8"))["backends"]), ["cpu", "cuda"], name)

    def test_each_pace_leg_is_its_display_matrix_cell_without_the_contact_sheet(self) -> None:
        """PLAYBACK-BACHELOR-PRESENT-JITTER-1: a pace leg runs its display-matrix cell (same display mode and scale, Look Assist forced, cinematic,
        LIGHT, same quiescence gate and criteria) as a SPEED leg, so it takes no contact sheet inside the timed Play."""
        for rel in PACE_LEGS:
            pace = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(pace, self.schema)
            cell = json.loads((DV / "legs" / f"m16-1243-display-{pace['displayMode']}-s{pace['scaleFactor']}.json").read_text(encoding="utf-8"))
            self.assertEqual(pace["legType"], "speed", rel)
            self.assertNotIn("look", pace, rel)
            self.assertEqual(pace["generatorArgs"], dict(cell["generatorArgs"], forceLookAssist=True, lookFlavor=cell["look"]["lookFlavor"]), rel)
            for key in ("clipId", "playSeconds", "backends", "scaleFactor", "displayMode", "timeouts", "criteria"):
                self.assertEqual(pace[key], cell[key], f"{rel}: {key}")
            self.assertEqual(pace.get("acceptedEffectiveScale"), cell.get("acceptedEffectiveScale"), f"{rel}: declares its cell's CUDA texture-route clamp")
        self.assertEqual(sorted(json.loads((DV / rel).read_text(encoding="utf-8"))["legId"] for rel in PACE_LEGS),
                         sorted(["m16-1243-pace-cinematic-fullscreen-s4", "m16-1243-pace-cinematic-fullscreen-s2", "m16-1243-pace-cinematic-windowed-s4",
                                 "m16-1243-pace-cinematic-fullscreen-s1", "m16-1243-pace-cinematic-windowed-s1", "m16-1243-pace-cinematic-windowed-s2"]))

    def test_each_cpu_reduced_pace_leg_is_its_cpu_reduced_cell_without_the_contact_sheet(self) -> None:
        """LOOK-LEG-PACE-TWIN-COVERAGE-1: a cpu-reduced pace twin runs its cpu-reduced look leg (same display mode, scale, playbackProcessing receipt,
        cpu only, same criteria) as a SPEED leg with Look Assist forced at the cell's flavor, so it takes no contact sheet inside the timed Play."""
        self.assertEqual(len(CPU_REDUCED_PACE_LEGS), len(CPU_REDUCED_LEGS))
        for rel in CPU_REDUCED_PACE_LEGS:
            pace = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(pace, self.schema)
            cell = json.loads((DV / "legs" / f"m16-1243-cpu-reduced-{pace['displayMode']}-s{pace['scaleFactor']}.json").read_text(encoding="utf-8"))
            self.assertEqual(pace["legType"], "speed", rel)
            self.assertNotIn("look", pace, rel)
            self.assertEqual(pace["legId"], "m16-1243-pace-cinematic-cpu-reduced-" + rel.rsplit("cpu-reduced-", 1)[1][:-len(".json")], rel)
            self.assertEqual(pace["backends"], ["cpu"], rel)
            self.assertEqual(pace["generatorArgs"]["playbackProcessing"], "receipt", rel)
            self.assertEqual(pace["generatorArgs"], dict(cell["generatorArgs"], forceLookAssist=True, lookFlavor=cell["look"]["lookFlavor"]), rel)
            for key in ("clipId", "playSeconds", "backends", "scaleFactor", "displayMode", "timeouts", "criteria"):
                self.assertEqual(pace[key], cell[key], f"{rel}: {key}")

    def test_the_cpu_receipt_heavy_leg_is_the_cpu_reduced_pace_twin_with_the_frame_log_kept(self) -> None:
        """PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1: the leg that routes a cpu leg's frames to Processed8 (playbackProcessing receipt; the default subset
        routes them to Debayered16) AND logs per-frame rows (telemetryArm HEAVY; LIGHT disables the playback_smoke.frame line). It is the cpu-reduced fullscreen
        s4 pace twin except legId, card and telemetryArm, so a later edit cannot quietly make it LIGHT, subset, or a cuda leg, or switch Look Assist off."""
        arm = json.loads((DV / CPU_RECEIPT_HEAVY_LEG).read_text(encoding="utf-8"))
        self.jsonschema.validate(arm, self.schema)
        self.assertEqual(arm["legId"], Path(CPU_RECEIPT_HEAVY_LEG).stem)
        self.assertEqual(arm["legType"], "speed")
        self.assertEqual(arm["generatorArgs"]["telemetryArm"], "HEAVY")
        self.assertEqual(arm["generatorArgs"]["playbackProcessing"], "receipt")
        self.assertEqual(arm["backends"], ["cpu"])
        self.assertEqual((arm["scaleFactor"], arm["displayMode"], arm["playSeconds"]), (4, "fullscreen", 25))
        self.assertIs(arm["generatorArgs"]["forceLookAssist"], True, "Look Assist stays ON (D4b: the owner shape)")
        self.assertEqual(arm["generatorArgs"]["lookFlavor"], "cinematic")
        self.assertIn({"metric": "lookAssistForced", "op": "eq", "value": True}, arm["criteria"]["acceptance"]["cpu"])
        self.assertIn({"metric": "lookFlavorReported", "op": "eq", "value": "cinematic"}, arm["criteria"]["acceptance"]["cpu"])
        base = json.loads((DV / "legs" / "m16-1243-pace-cinematic-cpu-reduced-fullscreen-s4.json").read_text(encoding="utf-8"))
        self.assertEqual(base["generatorArgs"]["telemetryArm"], "LIGHT")
        self.assertEqual(arm["generatorArgs"].pop("telemetryArm"), "HEAVY")
        base["generatorArgs"].pop("telemetryArm")
        self.assertEqual(dict(arm, legId=base["legId"], card=base["card"]), base)
        # the schema takes the combination as it stands: HEAVY and receipt together are valid, and each field still rejects a value outside its enum
        for key, value in (("telemetryArm", "MEDIUM"), ("playbackProcessing", "fast")):
            bad = json.loads((DV / CPU_RECEIPT_HEAVY_LEG).read_text(encoding="utf-8"))
            bad["generatorArgs"][key] = value
            with self.assertRaises(self.jsonschema.ValidationError, msg=key):
                self.jsonschema.validate(bad, self.schema)

    def test_the_lookahead_leg_is_the_owner_shape_pace_leg_at_depth_3(self) -> None:
        """PLAYBACK-BACHELOR-PRESENT-JITTER-1: the lookahead A/B's depth-3 arm differs from the owner-shape pace leg only in its legId and
        generatorArgs.playbackRenderLookaheadFrames."""
        for rel in LOOKAHEAD_LEGS:
            arm = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(arm, self.schema)
            base = json.loads((DV / "legs" / "m16-1243-pace-cinematic-fullscreen-s4.json").read_text(encoding="utf-8"))
            self.assertEqual(arm["generatorArgs"].pop("playbackRenderLookaheadFrames"), 3, rel)
            self.assertEqual(dict(arm, legId=base["legId"]), base, rel)

    def test_each_vsync1_leg_is_its_display_matrix_cell_at_swap_interval_1(self) -> None:
        """PLAYBACK-VSYNC-DEFAULT-1: a vsync1 twin differs from its display-matrix cell only in its legId, its card and
        generatorArgs.swapInterval 1; the cell itself sets no swap interval (the app's default 0)."""
        self.assertEqual(self.schema["properties"]["generatorArgs"]["properties"]["swapInterval"]["enum"], [0, 1])
        for rel, base_rel in zip(VSYNC1_LEGS, DISPLAY_MATRIX_LEGS):
            arm = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(arm, self.schema)
            base = json.loads((DV / base_rel).read_text(encoding="utf-8"))
            self.assertNotIn("swapInterval", base["generatorArgs"], base_rel)
            self.assertEqual(arm["generatorArgs"].pop("swapInterval"), 1, rel)
            self.assertEqual(arm["legId"], Path(rel).stem, rel)
            self.assertEqual(arm["card"], "PLAYBACK-VSYNC-DEFAULT-1", rel)
            self.assertEqual(dict(arm, legId=base["legId"], card=base["card"]), base, rel)
        for value in (2, -1, "1"):
            bad = json.loads((DV / VSYNC1_LEGS[0]).read_text(encoding="utf-8"))
            bad["generatorArgs"]["swapInterval"] = value
            with self.assertRaises(self.jsonschema.ValidationError, msg=repr(value)):
                self.jsonschema.validate(bad, self.schema)

    def test_the_heavy_leg_is_the_owner_shape_pace_leg_with_the_frame_log_kept(self) -> None:
        """PLAYBACK-BACHELOR-PRESENT-JITTER-1 r2: the stall-stage diagnostic differs from the owner-shape pace leg only in its legId and
        generatorArgs.telemetryArm HEAVY (the per-frame log a LIGHT leg disables)."""
        for rel in HEAVY_LEGS:
            arm = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(arm, self.schema)
            base = json.loads((DV / "legs" / "m16-1243-pace-cinematic-fullscreen-s4.json").read_text(encoding="utf-8"))
            self.assertEqual(base["generatorArgs"]["telemetryArm"], "LIGHT")
            self.assertEqual(arm["generatorArgs"].pop("telemetryArm"), "HEAVY", rel)
            base["generatorArgs"].pop("telemetryArm")
            self.assertEqual(dict(arm, legId=base["legId"]), base, rel)

    def test_the_decoder_legs_are_their_twins_at_one_raw_prefetch_decoder(self) -> None:
        """PLAYBACK-LJ92-DECODE-THROUGHPUT-1: the K=1 arm differs from the leg it pairs with only in its legId and generatorArgs.rawPrefetchDecoders 1."""
        for rel, twin_rel in DECODER_LEGS.items():
            arm = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(arm, self.schema)
            twin = json.loads((DV / twin_rel).read_text(encoding="utf-8"))
            self.assertNotIn("rawPrefetchDecoders", twin["generatorArgs"], twin_rel)
            self.assertEqual(arm["legId"], twin["legId"] + "-dec1", rel)
            self.assertEqual(arm["generatorArgs"].pop("rawPrefetchDecoders"), 1, rel)
            self.assertEqual(dict(arm, legId=twin["legId"]), twin, rel)

    def test_the_upstream_baseline_legs_are_no_look_cpu_speed_legs_at_scale_1(self) -> None:
        """UPSTREAM-CPU-BASELINE-1: the fork arm of the upstream CPU A/B. Each leg is a cpu-only SPEED leg at scale 1 in the display mode its name says,
        LIGHT, the pace legs' 95 % quiescence gate, Look Assist explicitly OFF (never forced), and informational criteria (the A/B is read lane-side)."""
        for rel in UPSTREAM_BASELINE_LEGS:
            spec = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(spec, self.schema)
            mode = re.match(r"legs/m16-1243-upstream-baseline-cpu-(fullscreen|windowed)-s1\.json$", rel).group(1)
            self.assertEqual(spec["legId"], Path(rel).stem, rel)
            self.assertEqual(spec["card"], "UPSTREAM-CPU-BASELINE-1", rel)
            self.assertEqual((spec["legType"], spec["clipId"], spec["playSeconds"], spec["backends"], spec["scaleFactor"], spec["displayMode"]),
                             ("speed", "M16-1243", 25, ["cpu"], 1, mode), rel)
            self.assertEqual(spec["generatorArgs"], {"telemetryArm": "LIGHT", "cpuQuiescenceThresholdPercent": 95, "disableLookAssist": True}, rel)
            self.assertNotIn("look", spec, rel)
            self.assertEqual(spec["criteria"], {"acceptance": {"cpu": []}, "supplementary": {"cpu": []}}, rel)
        bad = json.loads((DV / UPSTREAM_BASELINE_LEGS[0]).read_text(encoding="utf-8"))
        bad["generatorArgs"]["disableLookAssist"] = "true"
        with self.assertRaises(self.jsonschema.ValidationError):
            self.jsonschema.validate(bad, self.schema)

    def test_the_present_trace_legs_are_their_twins_with_the_trace_on(self) -> None:
        """PLAYBACK-STARTUP-CATCHUP-BURST-1 r7: a present-trace leg differs from its twin only in its legId and generatorArgs.presentTrace true."""
        self.assertEqual(self.schema["properties"]["generatorArgs"]["properties"]["presentTrace"]["type"], "boolean")
        for rel, twin_rel in PTRACE_LEGS.items():
            arm = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(arm, self.schema)
            twin = json.loads((DV / twin_rel).read_text(encoding="utf-8"))
            self.assertNotIn("presentTrace", twin["generatorArgs"], twin_rel)
            self.assertEqual(arm["legId"], twin["legId"] + "-ptrace", rel)
            self.assertIs(arm["generatorArgs"].pop("presentTrace"), True, rel)
            self.assertEqual(dict(arm, legId=twin["legId"]), twin, rel)

    def test_the_identity_legs_are_their_twins_with_the_capture_on(self) -> None:
        """PLAYBACK-STARTUP-WAIT-PREWARM-1: an identity leg differs from its twin only in its legId and generatorArgs.firstPresentCapture true."""
        self.assertEqual(self.schema["properties"]["generatorArgs"]["properties"]["firstPresentCapture"]["type"], "boolean")
        for rel, twin_rel in IDENTITY_LEGS.items():
            arm = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(arm, self.schema)
            twin = json.loads((DV / twin_rel).read_text(encoding="utf-8"))
            self.assertNotIn("firstPresentCapture", twin["generatorArgs"], twin_rel)
            self.assertEqual(arm["legId"], twin["legId"] + "-identity", rel)
            self.assertIs(arm["generatorArgs"].pop("firstPresentCapture"), True, rel)
            self.assertEqual(dict(arm, legId=twin["legId"]), twin, rel)

    def test_the_missdump_leg_is_its_twin_with_the_dump_on(self) -> None:
        """PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 F-diag: the miss-dump leg differs from its twin only in its legId and
        generatorArgs.warmReuseMissDump true."""
        self.assertEqual(self.schema["properties"]["generatorArgs"]["properties"]["warmReuseMissDump"]["type"], "boolean")
        for rel, twin_rel in MISSDUMP_LEGS.items():
            arm = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(arm, self.schema)
            twin = json.loads((DV / twin_rel).read_text(encoding="utf-8"))
            self.assertNotIn("warmReuseMissDump", twin["generatorArgs"], twin_rel)
            self.assertEqual(arm["legId"], twin["legId"] + "-missdump", rel)
            self.assertIs(arm["generatorArgs"].pop("warmReuseMissDump"), True, rel)
            self.assertEqual(dict(arm, legId=twin["legId"]), twin, rel)

    def test_the_scope_legs_are_no_look_windowed_s1_speed_legs_differing_only_in_scope(self) -> None:
        """PLAYBACK-SCOPES-OPTION-1: every scope leg is a single-backend no-look SPEED leg at windowed scale 1 (LIGHT, the 95 % quiescence gate,
        informational criteria), and each scope arm is its backend's N leg plus only the scope fields."""
        for rel, (backend, extra) in SCOPE_LEGS.items():
            spec = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(spec, self.schema)
            self.assertEqual(spec["legId"], Path(rel).stem, rel)
            self.assertEqual(spec["card"], "PLAYBACK-SCOPES-OPTION-1", rel)
            self.assertEqual((spec["legType"], spec["clipId"], spec["playSeconds"], spec["backends"], spec["scaleFactor"], spec["displayMode"]),
                             ("speed", "M16-1243", 25, [backend], 1, "windowed"), rel)
            self.assertEqual(spec["criteria"], {"acceptance": {backend: []}, "supplementary": {backend: []}}, rel)
            self.assertNotIn("look", spec, rel)
            none = json.loads((DV / f"legs/m16-1243-scopes-{backend}-windowed-s1-none.json").read_text(encoding="utf-8"))
            self.assertEqual(none["generatorArgs"], {"telemetryArm": "LIGHT", "cpuQuiescenceThresholdPercent": 95, "disableLookAssist": True})
            self.assertEqual(spec["generatorArgs"], {**none["generatorArgs"], **extra}, rel)
            self.assertEqual(dict(spec, legId=none["legId"], generatorArgs=none["generatorArgs"]), none, rel)
        for bad_value in ({"scope": "histo"}, {"scopeIntervalMs": -1}, {"scopeIntervalMs": 1001}, {"scopeIntervalMs": "0"},
                          {"scopesDuringPlayback": "off"}, {"scopesDuringPlayback": "Live"}):
            bad = json.loads((DV / "legs/m16-1243-scopes-cpu-windowed-s1-none.json").read_text(encoding="utf-8"))
            bad["generatorArgs"].update(bad_value)
            with self.assertRaises(self.jsonschema.ValidationError, msg=repr(bad_value)):
                self.jsonschema.validate(bad, self.schema)

    def test_the_scope_la_legs_are_the_no_look_twins_with_look_assist_forced_on(self) -> None:
        """PLAYBACK-SCOPES-OPTION-1 r3: each -la leg is its no-look twin with only legId, forceLookAssist (not disableLookAssist) and the
        lookAssistForced criterion changed, so the LA-on and LA-off arms measure the same thing but for Look Assist."""
        for rel in SCOPE_LA_LEGS:
            spec = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(spec, self.schema)
            self.assertEqual(spec["legId"], Path(rel).stem, rel)
            backend = spec["backends"][0]
            twin = json.loads((DV / rel.replace("-la.json", ".json")).read_text(encoding="utf-8"))
            self.assertIs(spec["generatorArgs"].pop("forceLookAssist"), True, rel)
            self.assertIs(twin["generatorArgs"].pop("disableLookAssist"), True, rel)
            self.assertEqual(spec["generatorArgs"], twin["generatorArgs"], rel)
            gate = {"metric": "lookAssistForced", "op": "eq", "value": True}
            self.assertEqual(spec["criteria"], {"acceptance": {backend: [gate]}, "supplementary": {backend: [gate]}}, rel)
            self.assertEqual(dict(spec, legId=twin["legId"], generatorArgs=twin["generatorArgs"], criteria=twin["criteria"]), twin, rel)

    def test_only_an_unpaced_swap_interval_0_leg_may_declare_its_tearing_expected(self) -> None:
        """CUDA-HEADROOM-1 r2: an unpaced leg at swap interval 0 presents with SyncInterval 0, so PresentMon reports AllowsTearing=1 (the hub's
        TEARING flag). Every headroom leg says so in presentTearingExpectedReason; no other leg carries it, and the schema refuses it on any leg
        that is not unpaced at swap interval 0, so the exemption can never reach a paced or vsync leg."""
        for rel in (*HEADROOM_LEGS, *PARTITION_LEGS, *AGXOFF_UNPACED_LEGS, *AGXOFF_REMEASURE_LEGS):
            spec = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(spec, self.schema)
            self.assertGreaterEqual(len(spec.get("presentTearingExpectedReason", "")), 12, rel)
        for path in sorted((DV / "legs").glob("*.json")):
            if "legs/" + path.name not in (*HEADROOM_LEGS, *PARTITION_LEGS, *AGXOFF_UNPACED_LEGS, *AGXOFF_REMEASURE_LEGS):
                self.assertNotIn("presentTearingExpectedReason", json.loads(path.read_text(encoding="utf-8")), path.name)
        leg = json.loads((DV / HEADROOM_LEGS[0]).read_text(encoding="utf-8"))
        paced = {k: v for k, v in leg["generatorArgs"].items() if k != "unpaced"}
        no_swap = {k: v for k, v in leg["generatorArgs"].items() if k != "swapInterval"}
        for name, bad in (("paced", dict(leg, generatorArgs=paced)), ("unpaced false", dict(leg, generatorArgs=dict(leg["generatorArgs"], unpaced=False))),
                          ("vsync", dict(leg, generatorArgs=dict(leg["generatorArgs"], swapInterval=1))), ("app-default swap", dict(leg, generatorArgs=no_swap)),
                          ("short reason", dict(leg, presentTearingExpectedReason="tearing"))):
            with self.assertRaises(self.jsonschema.ValidationError, msg=name):
                self.jsonschema.validate(bad, self.schema)

    def test_the_hand_listed_shipped_legs_are_exactly_the_legs_directory(self) -> None:
        self.assertEqual(sorted(SHIPPED_LEGS), sorted("legs/" + p.name for p in (DV / "legs").glob("*.json")), "SHIPPED_LEGS drifted from the legs/ directory")

    def test_every_non_classic_leg_gates_on_the_flavor_the_app_reports_having_applied(self) -> None:
        """LOOK-ASSIST-FLAVORS-2 landed the flavor reader, so a cinematic leg may ship (VENUE-CINEMATIC-SPEC-1). What it may not do is PASS while the app
        fell back to Classic (the app then reports `none` or `classic`): every non-classic look leg must carry `lookFlavorReported eq <its flavor>` in every
        criteria list it has, and a classic or speed leg must not mention a non-classic flavor. Every file in legs/ is checked, not one name."""
        flavored = 0
        for path in sorted((DV / "legs").glob("*.json")):
            spec = json.loads(path.read_text(encoding="utf-8"))
            flavor = (spec.get("look") or {}).get("lookFlavor", "classic")
            gen_args = spec.get("generatorArgs") or {}
            if spec["legType"] == "speed" and gen_args.get("forceLookAssist"):   # a capture-free pace leg forces its flavor through generatorArgs
                flavor = gen_args.get("lookFlavor", "classic")
            if flavor == "classic":
                self.assertNotRegex(path.read_text(encoding="utf-8").lower(), r"cinematic", f"{path.name} is a classic/speed leg and mentions the cinematic flavor")
                continue
            flavored += 1
            if spec["card"] == "DUAL-VENUE-DISPLAY-MATRIX-1":   # the display-matrix legs are cinematic look legs named by their cell (display mode and scale)
                self.assertEqual(spec["legId"], path.stem, path.name)   # (-ptrace: the cell with the present trace on, PTRACE_LEGS;
                # -identity: the cell with the first-present identity capture on, IDENTITY_LEGS; -missdump: the warm-reuse miss dump on,
                # MISSDUMP_LEGS)
                self.assertRegex(spec["legId"], r"^m16-1243-display-(fullscreen|windowed)-s[124](-ptrace|-identity|-missdump)?$", path.name)
                self.assertNotIn("prepConflatedMove", spec.get("generatorArgs") or {}, path.name)
            elif spec["card"] == "PLAYBACK-VSYNC-DEFAULT-1":   # the swap-interval-1 twins of the display-matrix legs, named by their cell
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertRegex(spec["legId"], r"^m16-1243-display-(fullscreen|windowed)-s[124]-vsync1$", path.name)
            elif spec["card"] == "VENUE-LEG-AGXOFF-UNPACED-HEADROOM-1":   # the unpaced s2 AgX-off look leg, named by flavor
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertRegex(spec["legId"], r"^m16-1243-look-scale2-(cinematic|film|vision50d)-agxoff-unpaced$", path.name)
                self.assertIs(spec["generatorArgs"]["unpaced"], True, path.name)
                self.assertEqual(spec["generatorArgs"]["swapInterval"], 0, path.name)
                self.assertEqual(spec["look"]["receipt"], "look-receipts/agx-off.marxml", path.name)
            elif spec["card"] == "VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1":   # its capture-free pace twin and the fast-path kill-switch arms
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertRegex(spec["legId"], r"^m16-1243-(look-scale2|pace)-cinematic-agxoff-unpaced(-nofast)?$", path.name)
                self.assertIs(spec["generatorArgs"]["unpaced"], True, path.name)
                self.assertEqual(spec["generatorArgs"]["swapInterval"], 0, path.name)
                self.assertEqual((spec.get("look") or {}).get("receipt", spec["generatorArgs"].get("lookReceipt")), "look-receipts/agx-off.marxml", path.name)
                self.assertEqual(spec["generatorArgs"].get("disablePrepPointwiseFast", False), spec["legId"].endswith("-nofast"), path.name)
            elif spec["card"] == "PLAYBACK-BACHELOR-PRESENT-JITTER-1":   # the capture-free pace legs, named by flavor and cell
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertRegex(spec["legId"], rf"^m16-1243-pace-{flavor}(-cpu-reduced)?-(fullscreen|windowed)-s[124](-la[0-3]|-heavy)?(-dec[0-4])?(-pcm)?$", path.name)
                # -pcm: the cell with the #397 prep-conflated-move switch on (PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1, PREP_CONFLATED_MOVE_LEGS); no other leg sets it
                self.assertEqual(spec["generatorArgs"].get("prepConflatedMove", False), spec["legId"].endswith("-pcm"), path.name)
            elif spec["card"] == "PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1":   # the cpu receipt pace leg that keeps the per-frame log, named by its cell
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertEqual(spec["legId"], "m16-1243-pace-cinematic-cpu-receipt-fullscreen-s4-heavy", path.name)
            elif spec["card"] == "CPU-DUALISO-REDUCED-ISO-NOTCH-1":   # the cpu-only legs that engage the CPU reduced recon, named by their cell
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertRegex(spec["legId"], r"^m16-1243-cpu-reduced-(fullscreen|windowed)-s[24]$", path.name)
                self.assertEqual(spec["backends"], ["cpu"], path.name)
                self.assertEqual(spec["generatorArgs"]["playbackProcessing"], "receipt", path.name)
            elif spec["card"] == "CUDA-HEADROOM-1":   # the unpaced throughput and stage legs, named by clip, kind and cell
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertRegex(spec["legId"], r"^m16-(1243|1457)-headroom-(unpaced|stage)-(fullscreen|windowed)-s[124]$", path.name)
                self.assertIs(spec["generatorArgs"]["unpaced"], True, path.name)
            elif spec["card"] == "BACHELOR-CORE-PARTITION-AB-1":   # the core-partition A/B legs (cinematic pace legs), named by arm
                self.assertEqual(spec["legId"], path.stem, path.name)
                # (VENUE-LEG-PLAYBACK-MAX-THREADS-ARG-1 added a12 and b1-12-pcore: A and B1 at a worker-thread cap of 12)
                self.assertRegex(spec["legId"], r"^m16-1243-partition-(a|b1-pcore|b2-pcore-eload|a12|b1-12-pcore)-unpaced-windowed-s1$", path.name)
                self.assertIs(spec["generatorArgs"]["unpaced"], True, path.name)
            else:   # the scale-4 flavored look leg, or its scale-2 twin (LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1), or that twin's AgX-off copy (LOOK-ASSIST-FILM-FLAVOR-2 r2)
                self.assertIn(spec["legId"], (f"m16-1243-look-{flavor}", f"m16-1243-look-scale2-{flavor}", f"m16-1243-look-scale2-{flavor}-agxoff"), path.name)
                self.assertEqual(spec["legId"], path.stem, path.name)
            for role, per_backend in spec["criteria"].items():
                self.assertTrue(per_backend, f"{path.name}: {role} has no criteria")
                for backend, criteria in per_backend.items():
                    self.assertIn({"metric": "lookFlavorReported", "op": "eq", "value": flavor}, criteria,
                                  f"{path.name} {role}/{backend}: a {flavor} leg must gate on the applied flavor, or a fallback to Classic passes it")
        self.assertGreaterEqual(flavored, 1, "the committed Cinematic look leg is missing")

    def test_the_cinematic_look_leg_is_the_classic_leg_plus_the_flavor_and_the_applied_flavor_criterion(self) -> None:
        load = lambda name: json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8"))
        classic, cinematic = load("m16-1243-look"), load("m16-1243-look-cinematic")
        self.jsonschema.validate(cinematic, self.schema)
        self.assertEqual(cinematic["legId"], "m16-1243-look-cinematic")
        self.assertEqual(cinematic["look"]["lookFlavor"], "cinematic")
        self.assertEqual(sorted(cinematic["backends"]), ["cpu", "cuda"])
        applied = {"metric": "lookFlavorReported", "op": "eq", "value": "cinematic"}
        stripped = json.loads(json.dumps(cinematic))
        for per_backend in stripped["criteria"].values():
            for backend, criteria in per_backend.items():
                self.assertEqual(criteria.count(applied), 1, backend)
                criteria.remove(applied)
        comparable = dict(stripped, legId=classic["legId"], look=dict(stripped["look"], lookFlavor="classic"))
        self.assertEqual(comparable, classic, "the Cinematic leg is the Classic leg except legId, lookFlavor and the applied-flavor criterion")
        self.assertNotIn(applied, [c for pb in classic["criteria"].values() for cl in pb.values() for c in cl], "the Classic leg must not gate on the cinematic flavor")

    def test_the_scale2_cinematic_look_leg_is_the_scale2_leg_plus_the_flavor_and_the_applied_flavor_criterion(self) -> None:
        """LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1: the Bachelor CPU look pair runs THE SAME leg twice, Classic then Cinematic. The Cinematic leg is
        m16-1243-look-scale2.json except legId, look.lookFlavor and the applied-flavor criterion appended to every criteria list -- a pinned fact."""
        load = lambda name: json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8"))
        classic, cinematic = load("m16-1243-look-scale2"), load("m16-1243-look-scale2-cinematic")
        self.jsonschema.validate(cinematic, self.schema)
        self.assertEqual(cinematic["legId"], "m16-1243-look-scale2-cinematic")
        self.assertEqual(cinematic["look"]["lookFlavor"], "cinematic")
        self.assertNotIn("displayMode", cinematic, "the pair is the legacy full-screen look leg, not a display-matrix cell")
        applied = {"metric": "lookFlavorReported", "op": "eq", "value": "cinematic"}
        stripped = json.loads(json.dumps(cinematic))
        for per_backend in stripped["criteria"].values():
            for backend, criteria in per_backend.items():
                self.assertEqual(criteria[-1], applied, f"{backend}: the applied-flavor criterion is appended")
                self.assertEqual(criteria.count(applied), 1, backend)
                criteria.remove(applied)
        comparable = dict(stripped, legId=classic["legId"], look=dict(stripped["look"], lookFlavor="classic"))
        self.assertEqual(comparable, classic, "the scale-2 Cinematic leg is the scale-2 leg except legId, lookFlavor and the applied-flavor criterion")
        self.assertEqual(list(cinematic), list(classic), "same keys in the same order: the twin is the same leg byte-for-byte in shape")

    def test_each_film_look_leg_is_its_cinematic_twin_except_legId_flavor_and_the_applied_flavor_criterion(self) -> None:
        """LOOK-ASSIST-FILM-FLAVOR-1: the Film legs are the Cinematic legs (the scale-2 CPU trio leg and the scale-4 owner-shape CUDA cost leg) except
        legId, look.lookFlavor and the applied-flavor criterion, which names `film` in the same place in every criteria list."""
        load = lambda name: json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8"))
        for cine_id, film_id in (("m16-1243-look-scale2-cinematic", "m16-1243-look-scale2-film"), ("m16-1243-look-cinematic", "m16-1243-look-film")):
            with self.subTest(leg=film_id):
                cinematic, film = load(cine_id), load(film_id)
                self.jsonschema.validate(film, self.schema)
                self.assertEqual(film["legId"], film_id)
                self.assertEqual(film["look"]["lookFlavor"], "film")
                applied_cine = {"metric": "lookFlavorReported", "op": "eq", "value": "cinematic"}
                applied_film = {"metric": "lookFlavorReported", "op": "eq", "value": "film"}
                swapped = json.loads(json.dumps(film))
                for per_backend in swapped["criteria"].values():
                    for backend, criteria in per_backend.items():
                        self.assertEqual(criteria.count(applied_film), 1, backend)
                        self.assertNotIn(applied_cine, criteria, backend)
                        criteria[criteria.index(applied_film)] = applied_cine
                comparable = dict(swapped, legId=cinematic["legId"], look=dict(swapped["look"], lookFlavor="cinematic"))
                self.assertEqual(comparable, cinematic, f"{film_id} is {cine_id} except legId, lookFlavor and the applied-flavor criterion")
                self.assertEqual(list(film), list(cinematic), "same keys in the same order")

    def test_the_schema_takes_the_film_flavor_in_both_flavor_enums_and_nothing_misspelt(self) -> None:
        look = json.loads((DV / "legs" / "m16-1243-look-scale2.json").read_text(encoding="utf-8"))
        self.jsonschema.validate(dict(look, look=dict(look["look"], lookFlavor="film")), self.schema)
        pace = json.loads((DV / "legs" / "m16-1243-pace-cinematic-fullscreen-s4.json").read_text(encoding="utf-8"))
        self.jsonschema.validate(dict(pace, generatorArgs=dict(pace["generatorArgs"], lookFlavor="film")), self.schema)
        for bad in ("filmm", "Film", "film-v1", "film-v2", "film-v3"):
            with self.assertRaises(self.jsonschema.ValidationError, msg=bad):
                self.jsonschema.validate(dict(look, look=dict(look["look"], lookFlavor=bad)), self.schema)
            with self.assertRaises(self.jsonschema.ValidationError, msg=bad):
                self.jsonschema.validate(dict(pace, generatorArgs=dict(pace["generatorArgs"], lookFlavor=bad)), self.schema)

    def test_the_applied_flavor_metric_the_cinematic_leg_gates_on_is_one_the_job_writes_and_the_app_withholds_on_a_fallback(self) -> None:
        """`lookFlavorReported` is the job summary's copy of the app's visual_state look_assist_flavor, and the app reports `none` (never the requested
        flavor) when the analysis fell back or its diagnostics are not valid; Get-DvVerbatimMetrics copies summary scalars verbatim into the receipt metrics."""
        job = (ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        self.assertIn("lookFlavorReported = $(if ($LookLeg) { $lfReported = try { [string]$resultJson.log.visualState.look_assist_flavor }", job)
        window = (ROOT / "platform" / "qt" / "MainWindow.cpp").read_text(encoding="utf-8")
        self.assertRegex(window, r"m_lastLookAssistDiagnosticsValid && !m_lastLookAssistSafetyFallback\s*&& !m_lookAssistFlavorOutcome\.appliedName\(\)\.isEmpty\(\)\s*\?\s*m_lookAssistFlavorOutcome\.appliedName\(\)\s*:\s*QStringLiteral\(\"none\"\)")

    def test_the_scale2_look_leg_differs_from_the_classic_leg_only_where_it_must(self) -> None:
        load = lambda name: json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8"))
        classic, scale2 = load("m16-1243-look"), load("m16-1243-look-scale2")
        for spec in (classic, scale2):
            self.jsonschema.validate(spec, self.schema)
        self.assertEqual(classic["look"]["lookFlavor"], "classic")
        self.assertEqual(scale2["look"]["lookFlavor"], "classic", "the scale-2 leg is a Classic leg; the Cinematic leg is m16-1243-look-cinematic")
        self.assertEqual(scale2["legId"], "m16-1243-look-scale2")
        self.assertEqual(scale2["scaleFactor"], 2)
        self.assertEqual(classic["scaleFactor"], 4)
        self.assertEqual(scale2["backends"], ["cuda", "cpu"], "the CUDA texture route honours scale 2 (PLAYBACK-CUDA-HONOUR-SCALE-1), so the scale-2 leg runs both backends")
        self.assertNotIn("acceptedEffectiveScale", scale2, "a leg that wants scale 2 declares no accepted clamp: a CUDA run at 1 must not pass it")
        self.assertNotIn("acceptedEffectiveScale", classic, "the scale-4 leg declares no clamp either: CUDA honours scale 4")
        comparable = dict(scale2, legId=classic["legId"], scaleFactor=classic["scaleFactor"])
        self.assertEqual(comparable, classic, "the scale-2 leg is the Classic leg except legId and scaleFactor")
        self.assertNotEqual(classic["legId"], scale2["legId"])

    def test_the_cuda_texture_route_clamp_is_declared_only_where_the_route_still_clamps(self) -> None:
        """TRIPWIRE: since PLAYBACK-CUDA-HONOUR-SCALE-1 the GPU texture route honours scales 2 and 4 for a play session the GPU reduced-recon plan admits
        (mainWindowGpuTextureRouteEffectivePlaybackScale) and still clamps x8 to 1. A cuda leg at 2 or 4 must therefore declare NO clamp (a fallback to 1 ends
        SCALE_NOT_HONOURED), and a cuda leg at 8 must declare acceptedEffectiveScale.cuda == 1. If the policy changes again, revisit the legs and the docs."""
        policy = (ROOT / "platform" / "qt" / "MainWindowGpuPreviewPolicy.h").read_text(encoding="utf-8")
        self.assertIn("if (requestedScale != 1 && gpuPlaybackReconTextureRouteEligibleAtScaleOne)", policy, "the clamp changed: revisit the legs' acceptedEffectiveScale")
        self.assertIn("&& (requestedScale == 2 || requestedScale == 4))", policy, "the honoured scales changed: revisit the legs' acceptedEffectiveScale")
        for path in sorted((DV / "legs").glob("*.json")):
            spec = json.loads(path.read_text(encoding="utf-8"))
            if "cuda" not in spec["backends"]:
                continue
            if spec["scaleFactor"] in (2, 4):
                self.assertNotIn("cuda", spec.get("acceptedEffectiveScale", {}), f"{path.name}: declares a CUDA clamp at an honoured scale {spec['scaleFactor']}")
            elif spec["scaleFactor"] == 8:
                self.assertEqual(spec.get("acceptedEffectiveScale", {}).get("cuda"), 1, f"{path.name}: names scale 8 on cuda without declaring the clamp")

    def test_the_docs_make_no_scale_claim_the_cuda_route_cannot_keep_and_list_every_typed_outcome(self) -> None:
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        self.assertNotIn("it is the leg that shows the owner's playback look", doc, "the scale-2 look is only reachable where the effective scale is 2 (cpu)")
        self.assertIn("`m16-1243-look-scale2` runs cuda and cpu", doc)
        self.assertIn("SCALE_NOT_HONOURED", doc)
        module = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        enum = re.search(r"\$script:OutcomeEnum = @\((.+?)\)", module).group(1)
        for token in re.findall(r"'([A-Z_]+)'", enum):
            self.assertIn(token, re.search(r"\| P4 \|.*", doc).group(0), f"P4 in the docs does not list the typed outcome {token}")

    def test_the_schema_takes_a_declared_effective_scale_per_backend_and_nothing_else(self) -> None:
        spec = json.loads((DV / "legs" / "m16-1243-look.json").read_text(encoding="utf-8"))
        self.jsonschema.validate(dict(spec, acceptedEffectiveScale={"cuda": 1, "cpu": 4}), self.schema)
        for bad in ({"gl": 1}, {"cuda": 0}, {"cuda": "1"}, {"cuda": 17}, {"cuda": 1.5}):
            with self.assertRaises(self.jsonschema.ValidationError, msg=str(bad)):
                self.jsonschema.validate(dict(spec, acceptedEffectiveScale=bad), self.schema)

    def test_no_shipped_leg_can_play_a_fixture_or_a_short_window(self) -> None:
        # ROUND 2: legs are addressed by consented clip ID, and the tracked fixtures are never a venue playback clip.
        for path in sorted((DV / "legs").glob("*.json")):
            spec = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn(spec["clipId"], FIXTURE_IDS, path.name)
            self.assertRegex(spec["clipId"], r"^[A-Za-z]\d{2}-\d{3,4}$", f"{path.name} names a consented clip id")
            self.assertGreaterEqual(spec["playSeconds"], 20, f"{path.name}: the play window is at least the owner's 20 s")
            self.assertNotRegex(path.read_text(encoding="utf-8"), r"(?i)[a-z]:[\\/]|\.mlv\b", f"{path.name} must name no path")

    def test_the_schema_rejects_a_fixture_leg_a_path_and_a_short_window(self) -> None:
        # A short window is one under the lowest floor (PLAYBACK-CLIP-LENGTH-HFR-10S-1: an HFR clip's 10 s); a leg cannot see the clip's fps.
        spec = json.loads(next(iter(sorted((DV / "legs").glob("*speed*.json")))).read_text(encoding="utf-8"))
        for override in ({"clipId": FIXTURE_IDS[0]}, {"clipId": FIXTURE_IDS[1]}, {"clipId": "C:/footage/" + OWNER_CLIP},
                         {"playSeconds": 9}, {"clipPath": "C:/x"}):
            with self.assertRaises(self.jsonschema.ValidationError, msg=str(override)):
                self.jsonschema.validate(dict(spec, **override), self.schema)

    def test_the_display_mode_is_optional_and_only_fullscreen_or_windowed(self) -> None:
        """DUAL-VENUE-DISPLAY-MATRIX-1: a spec without `displayMode` is valid (it is a full-screen leg: every existing leg is unchanged); the field takes exactly two values."""
        spec = json.loads((DV / "legs" / "m16-1243-look.json").read_text(encoding="utf-8"))
        self.assertNotIn("displayMode", spec)
        self.jsonschema.validate(spec, self.schema)
        for mode in ("fullscreen", "windowed"):
            self.jsonschema.validate(dict(spec, displayMode=mode), self.schema)
        for bad in ("maximized", "Windowed", "", None, 1):
            with self.assertRaises(self.jsonschema.ValidationError, msg=repr(bad)):
                self.jsonschema.validate(dict(spec, displayMode=bad), self.schema)

    def test_the_leg_set_file_is_a_tracked_list_of_committed_specs_and_names_no_path(self) -> None:
        doc = json.loads((DV / "legsets" / "display-matrix.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["schema"], "mlv-app/dual-venue-legset/v1")
        self.assertEqual(sorted(doc["legs"]), sorted(DISPLAY_MATRIX_LEGS))
        self.assertNotRegex((DV / "legsets" / "display-matrix.json").read_text(encoding="utf-8"), r"(?i)[a-z]:[\\/]|\.mlv\b")
        self.assertIn("tools/profiling/dual-venue/legsets/*.json text eol=lf", (ROOT / ".gitattributes").read_text(encoding="utf-8"))
        roles = json.loads((DV / "venues.json").read_text(encoding="utf-8"))["roles"]
        self.assertEqual(roles["DUAL-VENUE-DISPLAY-MATRIX-1"], {"bachelor": "acceptance", "ultra-magnus": "supplementary"}, "the roles are declared before the first byte (kernel K5)")

    def test_a_look_leg_without_a_look_block_and_an_unknown_backend_are_rejected(self) -> None:
        spec = json.loads(next(iter(sorted((DV / "legs").glob("*look*.json")))).read_text(encoding="utf-8"))
        bad = dict(spec)
        del bad["look"]
        with self.assertRaises(self.jsonschema.ValidationError):
            self.jsonschema.validate(bad, self.schema)
        bad = dict(spec, backends=["cuda", "vulkan"])
        with self.assertRaises(self.jsonschema.ValidationError):
            self.jsonschema.validate(bad, self.schema)


# LOOK-LEG-PACE-TWIN-COVERAGE-1: a look leg takes contact-sheet frame grabs DURING the timed Play, so it cannot support an absolute fps / drop / jitter / pace
# claim; the claim needs the look leg's capture-free pace twin (a speed leg that forces Look Assist at the same clip, display, scale, flavor and condition).
# A look leg with no twin must be listed here with the reason it has none; the reason is one line. The census that produced the list (KEY-PROMPT-LOOK-LEG-
# FPS-CAPTURE-FREE-ARM-1 r1, re-run on fork/master 4cf92c33) found 23 untwinned look legs; this card twinned the 9 that merged playback PRs cite for absolute
# figures (the display matrix cells fullscreen s1, windowed s1 and windowed s2 -- which also cover the s1 identity and miss-dump diagnostics -- and the four
# cpu-reduced cells) and lists the other 14.
NO_PACE_TWIN_BY_DESIGN = {
    **{f"m16-1243-display-{mode}-s{scale}-vsync1": "swapInterval 1 A/B arm; no merged PR cites an absolute figure from it, so a swapInterval 1 pace twin waits for the first PR that does"
       for mode in ("fullscreen", "windowed") for scale in (1, 2, 4)},
    "m16-1243-look": "Classic flavor-parity contact-sheet leg (also a supplementary row of the nightly baseline); pace claims use the Cinematic display-matrix twins",
    "m16-1243-look-scale2": "Classic flavor-parity contact-sheet leg at scale 2; pace claims use the Cinematic display-matrix twins",
    "m16-1243-look-film": "Film flavor-parity contact-sheet leg; no merged PR cites an absolute figure from it",
    "m16-1243-look-scale2-film": "Film flavor-parity contact-sheet leg at scale 2; no merged PR cites an absolute figure from it",
    "m16-1243-look-scale2-vision50d": "Vision50d flavor-parity contact-sheet leg at scale 2; no merged PR cites an absolute figure from it",
    **{f"m16-1243-look-scale2-{flavor}-agxoff": "carries a look receipt (agx-off.marxml) that a speed leg cannot carry, so no speed leg can be the same condition"
       for flavor in ("cinematic", "film", "vision50d")},
    "m16-1243-look-scale2-cinematic-agxoff-unpaced": "an unpaced headroom leg carrying a look receipt; its capture-free twin is m16-1243-pace-cinematic-agxoff-unpaced (an unpaced pace leg carrying the receipt in generatorArgs), which this paced-only rule does not see",
    "m16-1243-look-scale2-cinematic-agxoff-unpaced-nofast": "the unpaced AgX-off leg with the #680 fast path killed; its capture-free twin is m16-1243-pace-cinematic-agxoff-unpaced-nofast, which this paced-only rule does not see",
}


def find_capture_free_pace_twin(look_spec: dict, specs: dict) -> str | None:
    """The legId of a pace twin of `look_spec` among `specs` ({legId: spec}), else None. A twin is a SPEED leg that forces Look Assist at the look leg's
    flavor, is paced, and matches its clip, display mode (absent = fullscreen), scale and condition, over a backend set that covers the look leg's. Condition =
    generatorArgs minus the keys that do not change what is timed: the quiescence gate and telemetry arm, the speed side's forceLookAssist / lookFlavor and the
    look side's diagnostics (firstPresentCapture, presentTrace, warmReuseMissDump). A look leg that carries a look receipt has no twin: a speed leg cannot."""
    neutral = {"cpuQuiescenceThresholdPercent", "telemetryArm"}
    look_diag = {"firstPresentCapture", "presentTrace", "warmReuseMissDump"}
    speed_only = {"forceLookAssist", "lookFlavor"}
    look = look_spec.get("look", {})
    if "receipt" in look:
        return None
    want = {k: v for k, v in look_spec.get("generatorArgs", {}).items() if k not in neutral | look_diag}
    for leg_id, spec in sorted(specs.items()):
        args = spec.get("generatorArgs", {})
        if spec.get("legType") != "speed" or args.get("forceLookAssist") is not True or args.get("unpaced"):
            continue
        if args.get("lookFlavor") != look.get("lookFlavor"):
            continue
        if any(spec.get(k) != look_spec.get(k) for k in ("clipId", "scaleFactor")) or spec.get("displayMode", "fullscreen") != look_spec.get("displayMode", "fullscreen"):
            continue
        if not set(look_spec["backends"]) <= set(spec["backends"]):
            continue
        if {k: v for k, v in args.items() if k not in neutral | speed_only} == want:
            return leg_id
    return None


class LookLegPaceTwinCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.specs = {}
        for path in sorted((DV / "legs").glob("*.json")):
            spec = json.loads(path.read_text(encoding="utf-8"))
            self.specs[spec["legId"]] = spec
        self.looks = {k: v for k, v in self.specs.items() if v["legType"] == "look"}

    def test_every_look_leg_has_a_capture_free_pace_twin_or_a_by_design_entry(self) -> None:
        missing = sorted(k for k, v in self.looks.items() if find_capture_free_pace_twin(v, self.specs) is None and k not in NO_PACE_TWIN_BY_DESIGN)
        self.assertEqual(missing, [], "look legs with no capture-free pace twin and no NO_PACE_TWIN_BY_DESIGN entry: add the twin (a speed leg with generatorArgs.forceLookAssist "
                                     "true and the same clip, display, scale, flavor and condition) or list the leg with a one-line reason")

    def test_the_by_design_list_names_only_untwinned_look_legs_and_gives_a_reason(self) -> None:
        for leg_id, reason in NO_PACE_TWIN_BY_DESIGN.items():
            self.assertIn(leg_id, self.looks, f"{leg_id}: not a look leg under legs/ (stale entry)")
            self.assertIsNone(find_capture_free_pace_twin(self.looks[leg_id], self.specs), f"{leg_id}: has a pace twin now, so drop it from the by-design list")
            self.assertTrue(reason.strip() and "\n" not in reason and len(reason) >= 20, f"{leg_id}: the reason must be one real line")

    def test_the_twin_rule_accepts_a_matching_speed_leg_and_rejects_each_mismatch(self) -> None:
        look = {"legId": "x-look", "legType": "look", "clipId": "C", "scaleFactor": 2, "displayMode": "windowed", "backends": ["cuda", "cpu"],
                "generatorArgs": {"telemetryArm": "LIGHT", "cpuQuiescenceThresholdPercent": 95, "presentTrace": True}, "look": {"lookFlavor": "cinematic"}}
        twin = {"legId": "x-pace", "legType": "speed", "clipId": "C", "scaleFactor": 2, "displayMode": "windowed", "backends": ["cuda", "cpu"],
                "generatorArgs": {"telemetryArm": "HEAVY", "forceLookAssist": True, "lookFlavor": "cinematic"}}
        self.assertEqual(find_capture_free_pace_twin(look, {"x-pace": twin}), "x-pace")
        for name, change in (("another clip", {"clipId": "D"}), ("another scale", {"scaleFactor": 4}), ("another display", {"displayMode": "fullscreen"}),
                             ("a look leg, not a speed leg", {"legType": "look"}), ("backends that do not cover the look leg", {"backends": ["cpu"]}),
                             ("look assist not forced", {"generatorArgs": {"lookFlavor": "cinematic"}}),
                             ("another flavor", {"generatorArgs": {"forceLookAssist": True, "lookFlavor": "film"}}),
                             ("unpaced", {"generatorArgs": {"forceLookAssist": True, "lookFlavor": "cinematic", "unpaced": True}}),
                             ("another swap interval", {"generatorArgs": {"forceLookAssist": True, "lookFlavor": "cinematic", "swapInterval": 1}}),
                             ("another playback processing", {"generatorArgs": {"forceLookAssist": True, "lookFlavor": "cinematic", "playbackProcessing": "receipt"}})):
            self.assertIsNone(find_capture_free_pace_twin(look, {"x-pace": dict(twin, **change)}), name)
        receipt = dict(look, look={"lookFlavor": "cinematic", "receipt": "look-receipts/agx-off.marxml"})
        self.assertIsNone(find_capture_free_pace_twin(receipt, {"x-pace": twin}), "a look receipt cannot be carried by a speed leg")
        self.assertIsNone(find_capture_free_pace_twin(look, {}), "no speed legs at all")

    def test_the_display_matrix_cells_and_the_cpu_reduced_cells_are_all_twinned(self) -> None:
        for leg_id in (*(rel[len("legs/"):-len(".json")] for rel in (*DISPLAY_MATRIX_LEGS, *CPU_REDUCED_LEGS, *PTRACE_LEGS, *IDENTITY_LEGS, *MISSDUMP_LEGS)),
                       "m16-1243-look-cinematic", "m16-1243-look-scale2-cinematic"):
            self.assertIsNotNone(find_capture_free_pace_twin(self.looks[leg_id], self.specs), leg_id)


@requires_windows_pwsh
class SessionLockedHealthTests(RunnerHarness, unittest.TestCase):
    """VENUE-SESSION-LOCKED-REFUSAL-1 item 3: a venue console that is locked, or whose lock state is unknown, is VENUE_UNHEALTHY with detail
    exactly SESSION_LOCKED and the leg is never submitted; the job's SESSION_LOCKED_OWNER_ONLY result is a venue condition. Measured cause:
    Bachelor stayed locked after a Windows Update reboot on 2026-10-09 and 24 legs ended KEEPALIVE_FAILED."""

    def setUp(self) -> None:
        self.make_harness()

    def assert_locked_and_not_submitted(self, probe: dict, recorded) -> None:
        proc, receipt, submitted = self.run_leg("bachelor", self.write_spec(), probe=probe)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "VENUE_UNHEALTHY")
        self.assertEqual(receipt["outcomeDetail"], "SESSION_LOCKED")
        self.assertIn("DVE_DETAIL=SESSION_LOCKED", proc.stdout)
        self.assertEqual(receipt["health"]["outcome"], "UNHEALTHY")
        self.assertEqual(receipt["health"]["sessionLocked"], recorded)
        self.assertEqual(len(submitted), 1, f"only the health probe may be submitted, got {submitted}")
        self.assertTrue(submitted[0].endswith("-health"))

    def test_a_locked_console_is_unhealthy_session_locked_and_the_leg_is_not_submitted(self) -> None:
        self.assert_locked_and_not_submitted(dict(BACHELOR_PROBE, sessionLocked=True), True)

    def test_an_unknown_lock_state_fails_closed(self) -> None:
        self.assert_locked_and_not_submitted(dict(BACHELOR_PROBE, sessionLocked=None), None)
        absent = {k: v for k, v in BACHELOR_PROBE.items() if k != "sessionLocked"}
        self.assert_locked_and_not_submitted(absent, None)
        # a string is not a JSON false: never read as unlocked
        self.assert_locked_and_not_submitted(dict(BACHELOR_PROBE, sessionLocked="false"), "false")

    def test_an_unlocked_console_is_healthy_and_recorded(self) -> None:
        proc, receipt, submitted = self.run_leg("bachelor", self.write_spec(), probe=BACHELOR_PROBE)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["health"]["outcome"], "HEALTHY")
        self.assertIs(receipt["health"]["sessionLocked"], False)
        self.assertGreater(len(submitted), 1, "an unlocked, healthy venue gets its leg submitted")

    def test_the_verdict_names_session_locked_alongside_other_reasons(self) -> None:
        script = ("$t = [pscustomobject]@{ maxPwshColdStartMs = 3000; maxSmallHashMs = 2000; minFreeDiskGiB = 20; maxCommitUsedFraction = 0.9 }\n"
                  "$base = @{ pwshColdStartMs = 9000; smallHashMs = 40; freeDiskGiB = 600; commitUsedGiB = 40; commitLimitGiB = 128 }\n"
                  "$out = [ordered]@{}\n"
                  "foreach ($case in @('locked', 'unlocked', 'absent')) {\n"
                  "  $p = [pscustomobject]$base\n"
                  "  if ($case -eq 'locked') { $p | Add-Member sessionLocked $true } elseif ($case -eq 'unlocked') { $p | Add-Member sessionLocked $false }\n"
                  "  $out[$case] = Get-DvHealthVerdict -Probe $p -Thresholds $t\n"
                  "}\n"
                  "$out | ConvertTo-Json -Depth 5")
        proc, verdicts = run_pwsh_json(script)
        self.assertIsNotNone(verdicts, proc.stdout + proc.stderr)
        for case in ("locked", "absent"):
            self.assertFalse(verdicts[case]["healthy"])
            self.assertEqual(verdicts[case]["detail"], "SESSION_LOCKED")
            self.assertTrue(any(r.startswith("SESSION_LOCKED:") for r in verdicts[case]["reasons"]), verdicts[case])
            self.assertTrue(any("pwshColdStartMs" in r for r in verdicts[case]["reasons"]), verdicts[case])
        self.assertIsNone(verdicts["unlocked"]["detail"])
        self.assertFalse(any(r.startswith("SESSION_LOCKED") for r in verdicts["unlocked"]["reasons"]))

    def test_the_job_result_session_locked_owner_only_is_a_venue_condition(self) -> None:
        proc, out = run_pwsh_json("Resolve-DvJobOutcome -ResultToken 'SESSION_LOCKED_OWNER_ONLY' -ExitCode 30 | ConvertTo-Json")
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        self.assertEqual(out, {"outcome": "VENUE_UNHEALTHY", "detail": "SESSION_LOCKED_OWNER_ONLY"})
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8").split("## Outcome mapping", 1)[1]
        self.assertIn("`SESSION_LOCKED_OWNER_ONLY` (exit 30", doc)
        self.assertIn("exactly `SESSION_LOCKED`", doc)

    def test_the_health_probe_embeds_the_jobs_own_lock_read_verbatim(self) -> None:
        module = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
        script = (f"$p = New-DvHealthProbeJobText -AgentRoot 'C:\\mlvtmp\\mlv-agent'\n"
                  f"Import-Module '{module}' -Force\n"
                  "$fn = Get-AttrCudaEmbeddedFunctionSource -Name @('Get-AttrCudaSessionLocked')\n"
                  "$tok = $null; $err = $null; [void][System.Management.Automation.Language.Parser]::ParseInput($p, [ref]$tok, [ref]$err)\n"
                  "[ordered]@{ embedsVerbatim = $p.Contains($fn); parseErrors = @($err).Count;"
                  " assignAt = $p.IndexOf('$probe.sessionLocked = Get-AttrCudaSessionLocked'); fnAt = $p.IndexOf($fn);"
                  " printAt = $p.IndexOf('DVE_PROBE=') } | ConvertTo-Json")
        proc, out = run_pwsh_json(script)
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        self.assertTrue(out["embedsVerbatim"], "the probe must carry the job's own Get-AttrCudaSessionLocked text, not a re-implementation")
        self.assertEqual(out["parseErrors"], 0)
        self.assertLess(out["fnAt"], out["assignAt"])
        self.assertLess(out["assignAt"], out["printAt"], "sessionLocked must be set before the one DVE_PROBE line is printed")


# ---------------------------------------------------------------------------------------------------
# LOOK-ASSIST-FILM-FLAVOR-2 r2: a look leg may name a COMMITTED receipt (look.receipt + look.receiptSha256) that the GUI smoke applies before playback.
# The AgX-off receipt makes a Cinematic capture re-gradable frame-locked (gradation is followed only by AgX, the LUT and the filter). It is a measurement
# instrument, never a product setting.
def _base_commit_available() -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{LOOK_RECEIPT_BASE}^{{commit}}"], capture_output=True).returncode == 0


AGXOFF_RECEIPT = DV / "look-receipts" / "agx-off.marxml"


class LookReceiptLegSpecTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is required")
        self.jsonschema = jsonschema
        self.schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        self.receipt_sha = hashlib.sha256(AGXOFF_RECEIPT.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        self.film = json.loads((DV / "legs" / "m16-1243-look-scale2-film.json").read_text(encoding="utf-8"))

    def with_look(self, **fields) -> dict:
        return dict(self.film, look=dict(self.film["look"], **fields))

    def test_a_receipt_and_its_hash_validate_together_and_either_alone_is_refused(self) -> None:
        self.jsonschema.validate(self.with_look(receipt="look-receipts/agx-off.marxml", receiptSha256=self.receipt_sha), self.schema)
        for alone in ({"receipt": "look-receipts/agx-off.marxml"}, {"receiptSha256": self.receipt_sha}):
            with self.assertRaises(self.jsonschema.ValidationError, msg=repr(alone)):
                self.jsonschema.validate(self.with_look(**alone), self.schema)

    def test_a_receipt_outside_look_receipts_or_a_malformed_hash_is_refused(self) -> None:
        for path in ("receipts/agx-off.marxml", "look-receipts/../venues.json", "look-receipts/agx-off.xml", "C:/x/look-receipts/agx-off.marxml",
                     "look-receipts/AgX-Off.marxml", "look-receipts/sub/agx-off.marxml", "agx-off.marxml"):
            with self.assertRaises(self.jsonschema.ValidationError, msg=path):
                self.jsonschema.validate(self.with_look(receipt=path, receiptSha256=self.receipt_sha), self.schema)
        for sha in (self.receipt_sha.upper(), self.receipt_sha[:-1], "g" * 64):
            with self.assertRaises(self.jsonschema.ValidationError, msg=sha):
                self.jsonschema.validate(self.with_look(receipt="look-receipts/agx-off.marxml", receiptSha256=sha), self.schema)

    def test_every_committed_leg_spec_still_validates(self) -> None:
        for path in sorted((DV / "legs").glob("*.json")):
            self.jsonschema.validate(json.loads(path.read_text(encoding="utf-8")), self.schema)

    def test_each_agxoff_leg_is_its_source_except_legId_and_the_two_look_receipt_fields(self) -> None:
        for agx_id, source_id in AGXOFF_LEGS.items():
            with self.subTest(leg=agx_id):
                agx = json.loads((DV / "legs" / f"{agx_id}.json").read_text(encoding="utf-8"))
                source = json.loads((DV / "legs" / f"{source_id}.json").read_text(encoding="utf-8"))
                self.assertEqual(agx["legId"], agx_id)
                self.assertEqual(agx["card"], "DUAL-VENUE-EVIDENCE-1", "one card across a flavor pair's sides (New-VenueFlavorPair)")
                self.assertEqual(agx["look"].pop("receipt"), "look-receipts/agx-off.marxml")
                self.assertEqual(agx["look"].pop("receiptSha256"), self.receipt_sha, "the spec binds the committed receipt's bytes")
                self.assertEqual(dict(agx, legId=source_id), source)
                self.assertEqual(list(agx), list(source), "same keys in the same order")
                text = lambda n: (DV / "legs" / f"{n}.json").read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
                diff = [(a, b) for a, b in zip(text(agx_id).split("\n"), text(source_id).split("\n")) if a != b]
                self.assertEqual(len(diff), 2, "byte copies: only the legId line and the look line differ")

    def test_the_agxoff_unpaced_leg_is_its_source_plus_only_the_unpaced_headroom_keys(self) -> None:
        """VENUE-LEG-AGXOFF-UNPACED-HEADROOM-1: the AgX-off look leg run unpaced at swap interval 0, so its s2 rate is not pinned at the pace cap. It is its
        paced source except legId, card, playSeconds (the headroom legs' 20 s), the three unpaced-headroom generatorArgs, the tearing reason those force,
        and one contact-sheet grab (a look leg grabs inside the timed Play); the receipt and its bound hash are the source's own."""
        for rel, source_rel in AGXOFF_UNPACED_LEGS.items():
            with self.subTest(leg=rel):
                leg = json.loads((DV / rel).read_text(encoding="utf-8"))
                source = json.loads((DV / source_rel).read_text(encoding="utf-8"))
                self.jsonschema.validate(leg, self.schema)
                self.assertEqual(leg["legId"], Path(rel).stem)
                self.assertEqual(leg["card"], "VENUE-LEG-AGXOFF-UNPACED-HEADROOM-1")
                self.assertEqual(leg["playSeconds"], 20)
                for field in ("receipt", "receiptSha256"):
                    self.assertEqual(leg["look"][field], source["look"][field], field)
                self.assertEqual(leg["look"]["receiptSha256"], self.receipt_sha, "the spec binds the committed receipt's bytes")
                self.assertEqual(leg["look"]["contactSheetFrames"], 1)
                self.assertEqual(leg["generatorArgs"], dict(source["generatorArgs"], swapInterval=0, unpaced=True, presentTrace=True))
                self.assertEqual(list(leg["generatorArgs"]), [*source["generatorArgs"], "swapInterval", "unpaced", "presentTrace"])
                self.assertNotIn("presentTearingExpectedReason", source)
                self.assertGreaterEqual(len(leg["presentTearingExpectedReason"]), 12)
                self.assertEqual(leg, dict(source, legId=leg["legId"], card=leg["card"], playSeconds=20, generatorArgs=leg["generatorArgs"],
                                           presentTearingExpectedReason=leg["presentTearingExpectedReason"], look=dict(source["look"], contactSheetFrames=1)))
                self.assertEqual([k for k in leg if k != "presentTearingExpectedReason"], list(source), "the source's keys in the source's order")
                text = lambda r: (DV / r).read_bytes().replace(b"\r\n", b"\n").decode("utf-8")
                self.assertEqual(text(rel).split('"criteria"', 1)[1], text(source_rel).split('"criteria"', 1)[1], "criteria are a byte copy")

    def test_the_agxoff_unpaced_pace_leg_is_the_look_leg_without_the_contact_sheet(self) -> None:
        """VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: the capture-free twin of the unpaced AgX-off look leg is a SPEED leg (the pace-twin rule of
        test_each_cpu_reduced_pace_leg_is_its_cpu_reduced_cell_without_the_contact_sheet): the look leg minus its look block, plus forceLookAssist at the
        look's flavor and the look's own receipt and bound hash as generatorArgs.lookReceipt / lookReceiptSha256, same clip, window, backends, scale,
        display mode, timeouts and criteria. visualCapture is off (an unpaced capture tail blurs the rate) with the unpaced speed legs' reason."""
        [look_rel] = AGXOFF_UNPACED_LEGS
        pace = json.loads((DV / AGXOFF_UNPACED_PACE_LEG).read_text(encoding="utf-8"))
        look = json.loads((DV / look_rel).read_text(encoding="utf-8"))
        self.jsonschema.validate(pace, self.schema)
        self.assertEqual(pace["legId"], Path(AGXOFF_UNPACED_PACE_LEG).stem)
        self.assertEqual(pace["card"], "VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1")
        self.assertEqual(pace["legType"], "speed")
        self.assertNotIn("look", pace)
        self.assertEqual(pace["generatorArgs"], dict(look["generatorArgs"], forceLookAssist=True, lookFlavor=look["look"]["lookFlavor"],
                                                     lookReceipt=look["look"]["receipt"], lookReceiptSha256=look["look"]["receiptSha256"]))
        self.assertEqual(pace["generatorArgs"]["lookReceiptSha256"], self.receipt_sha, "the spec binds the committed receipt's bytes")
        for key in ("clipId", "playSeconds", "backends", "scaleFactor", "timeouts", "criteria", "presentTearingExpectedReason"):
            self.assertEqual(pace[key], look[key], key)
        self.assertEqual(pace.get("displayMode"), look.get("displayMode"), "displayMode")
        self.assertEqual(pace.get("acceptedEffectiveScale"), look.get("acceptedEffectiveScale"))
        headroom = json.loads((DV / "legs" / "m16-1243-headroom-unpaced-fullscreen-s2.json").read_text(encoding="utf-8"))
        self.assertIs(pace["visualCapture"], False)
        self.assertEqual(pace["visualCaptureOptOutReason"], headroom["visualCaptureOptOutReason"])
        self.assertEqual(set(pace) - set(look), {"visualCapture", "visualCaptureOptOutReason"})
        self.assertEqual(set(look) - set(pace), {"look"})

    def test_each_agxoff_unpaced_nofast_leg_differs_from_its_twin_only_in_the_kill_switch(self) -> None:
        """VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: each kill-switch arm (the #680 pointwise fast path off on the same staged build) differs from
        the leg it kills only in its legId, its card and generatorArgs.disablePrepPointwiseFast true; the twin sets no kill switch."""
        self.assertEqual(self.schema["properties"]["generatorArgs"]["properties"]["disablePrepPointwiseFast"]["type"], "boolean")
        for rel, twin_rel in AGXOFF_UNPACED_NOFAST_LEGS.items():
            with self.subTest(leg=rel):
                arm = json.loads((DV / rel).read_text(encoding="utf-8"))
                twin = json.loads((DV / twin_rel).read_text(encoding="utf-8"))
                self.jsonschema.validate(arm, self.schema)
                self.assertNotIn("disablePrepPointwiseFast", twin["generatorArgs"], twin_rel)
                self.assertEqual(arm["legId"], twin["legId"] + "-nofast")
                self.assertEqual(arm["card"], "VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1")
                self.assertIs(arm["generatorArgs"].pop("disablePrepPointwiseFast"), True)
                self.assertEqual(dict(arm, legId=twin["legId"], card=twin["card"]), twin)
                self.assertEqual(list(arm), list(twin), "the twin's keys in the twin's order")

    def test_the_schema_admits_a_pace_receipt_only_on_a_forced_look_speed_leg_and_whole(self) -> None:
        """VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: generatorArgs.lookReceipt needs lookReceiptSha256 and forceLookAssist true and a speed leg;
        the hash alone, a look leg, or a speed leg that does not force Look Assist is refused."""
        pace = json.loads((DV / AGXOFF_UNPACED_PACE_LEG).read_text(encoding="utf-8"))
        self.jsonschema.validate(pace, self.schema)
        args = pace["generatorArgs"]
        drop = lambda *keys: {k: v for k, v in args.items() if k not in keys}
        look = json.loads((DV / next(iter(AGXOFF_UNPACED_LEGS))).read_text(encoding="utf-8"))
        for name, bad in (("receipt without its hash", dict(pace, generatorArgs=drop("lookReceiptSha256"))),
                          ("hash without its receipt", dict(pace, generatorArgs=drop("lookReceipt"))),
                          ("receipt without forceLookAssist", dict(pace, generatorArgs=drop("forceLookAssist", "lookFlavor"))),
                          ("receipt with forceLookAssist false", dict(pace, generatorArgs=dict(args, forceLookAssist=False))),
                          ("receipt on a look leg", dict(look, generatorArgs=dict(look["generatorArgs"], forceLookAssist=True, lookReceipt=args["lookReceipt"],
                                                                                    lookReceiptSha256=args["lookReceiptSha256"]))),
                          ("a path outside look-receipts", dict(pace, generatorArgs=dict(args, lookReceipt="../agx-off.marxml"))),
                          ("a short hash", dict(pace, generatorArgs=dict(args, lookReceiptSha256="ab")))):
            with self.subTest(case=name):
                with self.assertRaises(self.jsonschema.ValidationError):
                    self.jsonschema.validate(bad, self.schema)

    def test_no_leg_spec_that_existed_at_the_base_changed(self) -> None:
        if not _base_commit_available():
            self.skipTest(f"base commit {LOOK_RECEIPT_BASE[:12]} is not in this clone")
        legs = "tools/profiling/dual-venue/legs/"
        changed = git("diff", "--name-only", "--diff-filter=a", LOOK_RECEIPT_BASE, "--", legs)
        self.assertEqual(changed, "", "existing leg specs are byte-identical (their legSpecSha256 values sit in receipts)")
        added = set(git("diff", "--name-only", "--diff-filter=A", LOOK_RECEIPT_BASE, "--", legs).split())
        self.assertTrue({legs + f"{leg}.json" for leg in AGXOFF_LEGS} <= added)

    def test_the_agx_off_receipt_turns_off_agx_lut_and_filter_keeps_look_assist_and_names_no_path(self) -> None:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(AGXOFF_RECEIPT.read_bytes())
        self.assertEqual((root.tag, root.get("version")), ("receipt", "4"))
        self.assertEqual([(c.tag, c.text) for c in root], [("lookAssistEnabled", "1"), ("agx", "0"), ("lutEnabled", "0"), ("filterEnabled", "0")],
                         "no gradationCurve: Film is laid only over a default curve, so the receipt must leave the curve default")
        self.assertNotRegex(AGXOFF_RECEIPT.read_text(encoding="utf-8"), r"(?i)[a-z]:[\\/]|\\\\|\.mlv\b|M16-1243")


@requires_windows_pwsh
class LookReceiptRunnerTests(RunnerHarness, unittest.TestCase):
    """(c) Invoke-VenueLeg reads the look receipt as COMMITTED (offline test mode: the RepoRoot's HEAD), never the working copy, and refuses
    LOOK_RECEIPT_UNBOUND before anything is generated or submitted unless its bytes hash to look.receiptSha256."""

    def setUp(self) -> None:
        self.make_harness()
        self.repo = self.tmp / "receipt-repo"
        target = self.repo / "tools" / "profiling" / "dual-venue" / "look-receipts" / "agx-off.marxml"
        target.parent.mkdir(parents=True)
        self.committed = AGXOFF_RECEIPT.read_bytes().replace(b"\r\n", b"\n")
        target.write_bytes(self.committed)
        for args in (("init", "-q"), ("config", "user.email", "unit@example.invalid"), ("config", "user.name", "unit"), ("config", "core.autocrlf", "false"),
                     ("add", "tools"), ("commit", "-q", "-m", "committed look receipt")):
            subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True, check=True)
        self.working = self.committed.replace(b"<agx>0</agx>", b"<agx>1</agx>")
        target.write_bytes(self.working)   # the working copy now differs from the committed blob
        param_tail = "$AutomationSettingsDir,$GpuPreLaunches)"
        self.assertIn(param_tail, STUB_GENERATOR, "STUB_GENERATOR param list changed; update this replace target")
        self.gen.write_text(STUB_GENERATOR.replace(param_tail, "$AutomationSettingsDir,$GpuPreLaunches,$LookReceiptPath)", 1), encoding="utf-8")
        self.work = self.tmp / "work"

    def spec(self, **look) -> Path:
        spec = json.loads(self.write_spec(leg_type="look", flavor="film", scale=2).read_text(encoding="utf-8"))
        spec["look"].update(look)
        path = self.tmp / f"spec-receipt-{hashlib.sha1(json.dumps(look, sort_keys=True).encode()).hexdigest()[:10]}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def run_receipt_leg(self, spec: Path, gen_refusal: str | None = None) -> tuple[subprocess.CompletedProcess, dict | None, list[str]]:
        cfg = {"log": str(self.log), "genLog": str(self.gen_log), "probe": BACHELOR_PROBE, "mainMode": "capture", "healthMode": "ok",
               "artifactsAgentPath": "X:\\stub\\agent\\outbox\\unit.artifacts", "genRefusal": gen_refusal, "clipContentSha256": CLIP_CONTENT_SHA,
               "token": None, "exitCode": 0}
        self.stub_cfg.write_text(json.dumps(cfg), encoding="utf-8")
        self.log.write_text("", encoding="utf-8")
        self.gen_log.write_text("", encoding="utf-8")
        before = set(self.receipts.rglob("*.json")) if self.receipts.exists() else set()
        proc = run_pwsh(["-File", str(DV / "Invoke-VenueLeg.ps1"), "-Venue", "bachelor", "-LegSpec", str(spec), "-SourceCommit", self.sha, "-Backend", "cpu",
                         "-BuildManifestSha256", self.build_sha, "-VenueTablePath", str(self.table), "-ReceiptRoot", str(self.receipts),
                         "-OfflineTestMode", "-UmRunScript", str(self.um), "-GeneratorScript", str(self.gen), "-WorkDir", str(self.work),
                         "-ConsentPath", str(self.consent), "-RepoRoot", str(self.repo), "-Actor", "unit-test"],
                        env_extra={"DVE_STUB": str(self.stub_cfg)})
        after = sorted((set(self.receipts.rglob("*.json")) if self.receipts.exists() else set()) - before, key=lambda f: f.stat().st_mtime_ns)
        receipt = json.loads(after[-1].read_text(encoding="utf-8")) if after else None
        return proc, receipt, [l.strip() for l in self.log.read_text(encoding="utf-8").splitlines() if l.strip()]

    def assertUnbound(self, spec: Path) -> None:
        proc, receipt, submitted = self.run_receipt_leg(spec)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["refusal"], "LOOK_RECEIPT_UNBOUND", proc.stdout)
        self.assertEqual(receipt["outcome"], "DEVICE_UNAVAILABLE")
        self.assertEqual(self.generator_calls(), [], "nothing is generated")
        self.assertEqual(submitted, [], "nothing is submitted, not even the health probe")

    def test_a_hash_only_the_working_copy_matches_is_refused_and_nothing_is_generated_or_submitted(self) -> None:
        self.assertNotEqual(self.committed, self.working)
        self.assertUnbound(self.spec(receipt="look-receipts/agx-off.marxml", receiptSha256=hashlib.sha256(self.working).hexdigest()))

    def test_a_wrong_hash_a_missing_half_an_uncommitted_file_or_a_path_outside_look_receipts_is_refused(self) -> None:
        sha = hashlib.sha256(self.committed).hexdigest()
        for look in ({"receipt": "look-receipts/agx-off.marxml", "receiptSha256": "0" * 64}, {"receipt": "look-receipts/agx-off.marxml"},
                     {"receiptSha256": sha}, {"receipt": "look-receipts/never-committed.marxml", "receiptSha256": sha},
                     {"receipt": "look-receipts/../look-receipts/agx-off.marxml", "receiptSha256": sha}):
            with self.subTest(look=look):
                self.assertUnbound(self.spec(**look))

    def test_the_committed_bytes_reach_the_generator(self) -> None:
        proc, receipt, submitted = self.run_receipt_leg(self.spec(receipt="look-receipts/agx-off.marxml", receiptSha256=hashlib.sha256(self.committed).hexdigest()),
                                                        gen_refusal="DUAL_VENUE_UNIT_STOP")
        self.assertEqual(receipt["refusal"], "GENERATOR_REFUSED_DUAL_VENUE_UNIT_STOP", proc.stdout + proc.stderr)
        [call] = self.generator_calls()
        found = re.search(r"(?:^|;)LookReceiptPath=([^;]+)", call)
        self.assertIsNotNone(found, call)
        self.assertEqual(Path(found.group(1)).read_bytes(), self.committed, "the generator is handed the COMMITTED bytes, not the working copy")
        self.assertEqual(submitted, [])

    def test_a_look_leg_without_a_receipt_passes_none(self) -> None:
        proc, receipt, _ = self.run_receipt_leg(self.spec(), gen_refusal="DUAL_VENUE_UNIT_STOP")
        self.assertEqual(receipt["refusal"], "GENERATOR_REFUSED_DUAL_VENUE_UNIT_STOP", proc.stdout + proc.stderr)
        [call] = self.generator_calls()
        self.assertNotIn("LookReceiptPath", call)

    # VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: a capture-free pace leg names the receipt in generatorArgs and is read the same way.
    def pace_spec(self, leg_type: str = "speed", **gen_args) -> Path:
        spec = json.loads(self.write_spec(leg_type=leg_type, flavor="cinematic", scale=2).read_text(encoding="utf-8"))
        spec["generatorArgs"] = dict(gen_args)
        path = self.tmp / f"spec-pace-receipt-{leg_type}-{hashlib.sha1(json.dumps(gen_args, sort_keys=True).encode()).hexdigest()[:10]}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def test_a_pace_legs_committed_receipt_bytes_reach_the_generator(self) -> None:
        sha = hashlib.sha256(self.committed).hexdigest()
        proc, receipt, submitted = self.run_receipt_leg(self.pace_spec(forceLookAssist=True, lookFlavor="cinematic", lookReceipt="look-receipts/agx-off.marxml",
                                                                       lookReceiptSha256=sha), gen_refusal="DUAL_VENUE_UNIT_STOP")
        self.assertEqual(receipt["refusal"], "GENERATOR_REFUSED_DUAL_VENUE_UNIT_STOP", proc.stdout + proc.stderr)
        [call] = self.generator_calls()
        found = re.search(r"(?:^|;)LookReceiptPath=([^;]+)", call)
        self.assertIsNotNone(found, call)
        self.assertEqual(Path(found.group(1)).read_bytes(), self.committed, "the generator is handed the COMMITTED bytes, not the working copy")
        self.assertIn("ForceLookAssist=True", call)
        self.assertNotIn("ContactSheet=", call)
        self.assertEqual(submitted, [])

    def test_a_pace_receipt_unbound_or_off_a_forced_look_speed_leg_is_refused(self) -> None:
        sha = hashlib.sha256(self.committed).hexdigest()
        force = {"forceLookAssist": True, "lookFlavor": "cinematic"}
        for name, leg_type, args in (("working-copy hash", "speed", dict(force, lookReceipt="look-receipts/agx-off.marxml", lookReceiptSha256=hashlib.sha256(self.working).hexdigest())),
                                     ("hash alone", "speed", dict(force, lookReceiptSha256=sha)),
                                     ("receipt alone", "speed", dict(force, lookReceipt="look-receipts/agx-off.marxml")),
                                     ("both null (adversary A2)", "speed", dict(force, lookReceipt=None, lookReceiptSha256=None)),
                                     ("both empty", "speed", dict(force, lookReceipt="", lookReceiptSha256="")),
                                     ("not forcing Look Assist", "speed", {"lookReceipt": "look-receipts/agx-off.marxml", "lookReceiptSha256": sha}),
                                     ("on a look leg", "look", {"lookReceipt": "look-receipts/agx-off.marxml", "lookReceiptSha256": sha})):
            with self.subTest(case=name):
                self.assertUnbound(self.pace_spec(leg_type, **args))


@requires_windows_pwsh
class LookReceiptValidatorTests(EvidenceFactory, ModuleMutationMixin, unittest.TestCase):
    """(f) Test-DvReceiptValid accepts a hashed summary.json's lookReceiptSha256 only when the committed leg spec names a look receipt with that hash;
    a summary without one is accepted only for a spec without one (both absent is every leg before this round)."""

    def setUp(self) -> None:
        self.make_harness()
        self.sha = hashlib.sha256(AGXOFF_RECEIPT.read_bytes().replace(b"\r\n", b"\n")).hexdigest()

    def receipt_spec(self) -> Path:
        spec = json.loads(self.write_spec(leg_type="look").read_text(encoding="utf-8"))
        spec["look"].update(receipt="look-receipts/agx-off.marxml", receiptSha256=self.sha)
        path = self.tmp / "spec-look-receipt.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def cases(self, module: Path | None = None) -> tuple[list, list]:
        plain_repo = self.prod_repo(leg_type="look")
        plain_spec = self.spec_path
        receipt_repo = self.prod_repo(leg_type="look", spec=self.receipt_spec())
        receipt_spec = self.spec_path
        bound = self.evidence("lr-bound", sheet=True, backend="cpu", leg_type="look", summary={"lookReceiptSha256": self.sha})
        foreign = self.evidence("lr-foreign", sheet=True, backend="cpu", leg_type="look", summary={"lookReceiptSha256": "0" * 64})
        none = self.evidence("lr-none", sheet=True, backend="cpu", leg_type="look")
        rc = self.status_batch(receipt_repo, [(self.receipt_for(receipt_repo, ev, backend="cpu", leg_type="look", spec_path=receipt_spec), None)
                                              for ev in (bound, foreign, none)], module=module)
        pl = self.status_batch(plain_repo, [(self.receipt_for(plain_repo, ev, backend="cpu", leg_type="look", spec_path=plain_spec), None)
                                            for ev in (none, bound)], module=module)
        return rc, pl

    def test_a_run_look_receipt_hash_is_accepted_only_where_the_committed_spec_names_it(self) -> None:
        rc, pl = self.cases()
        self.expect(rc[0], "ADVISORY", "the spec's receipt, bound in the hashed summary")
        self.expect(rc[1], "INVALID", "another receipt hash", "LOOK_RECEIPT_MISMATCH")
        self.expect(rc[2], "INVALID", "a receipt leg whose run recorded none", "LOOK_RECEIPT_MISMATCH")
        self.expect(pl[0], "ADVISORY", "no receipt on either side (every earlier leg)")
        self.expect(pl[1], "INVALID", "a receipt hash on a leg whose spec names none", "LOOK_RECEIPT_MISMATCH")

    def test_mutation_without_the_rule_a_foreign_receipt_hash_is_advisory(self) -> None:
        mutated = self.mutated_module([("if ([string](Get-DvProp $ev.summary 'lookReceiptSha256') -cne", "if ($false -and [string](Get-DvProp $ev.summary 'lookReceiptSha256') -cne")])
        rc, pl = self.cases(module=mutated)
        self.expect(rc[1], "ADVISORY", "with the rule removed a foreign receipt hash is believed -- so the rule is what refuses it")
        self.expect(pl[1], "ADVISORY", "with the rule removed a receipt hash on a plain leg is believed")

    # VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: the same rule for a capture-free pace leg, whose spec names its receipt in generatorArgs.
    PACE_RULE = "if ([string](Get-DvProp (Get-DvProp $spec 'generatorArgs') 'lookReceiptSha256') -cne $paceRunReceipt"

    def pace_failure_evidence(self, name: str, receipt_sha: str | None) -> Path:
        summary = dict(real_failure_summary("GPU_RECON_FRAMES_ZERO"))
        if receipt_sha is not None:
            summary["lookReceiptSha256"] = receipt_sha
        ev = self.evidence(name, source_frames=False, exact_summary=summary, token="GPU_RECON_FRAMES_ZERO", exit_code=13)
        (ev / "evidence-manifest.json").unlink()   # the job writes none on a product-failure terminal
        return ev

    def pace_cases(self, module: Path | None = None) -> tuple[list, list]:
        spec = json.loads(self.write_spec(leg_type="speed").read_text(encoding="utf-8"))
        spec["generatorArgs"] = {"forceLookAssist": True, "lookFlavor": "cinematic", "lookReceipt": "look-receipts/agx-off.marxml", "lookReceiptSha256": self.sha}
        receipt_spec = self.tmp / "spec-pace-receipt.json"
        receipt_spec.write_text(json.dumps(spec), encoding="utf-8")
        plain_repo = self.prod_repo(leg_type="speed")
        plain_spec = self.spec_path
        receipt_repo = self.prod_repo(leg_type="speed", spec=receipt_spec)
        bound = self.evidence("pr-bound", backend="cpu", summary={"lookReceiptSha256": self.sha})
        foreign = self.evidence("pr-foreign", backend="cpu", summary={"lookReceiptSha256": "0" * 64})
        none = self.evidence("pr-none", backend="cpu")
        # (a product-failure terminal: its summary states a hash only when the job wrote one; a stated hash is held to the spec's)
        ft_foreign = self.pace_failure_evidence("pr-ft-foreign", "0" * 64)
        ft_bound = self.pace_failure_evidence("pr-ft-bound", self.sha)
        ft_none = self.pace_failure_evidence("pr-ft-none", None)
        rc = self.status_batch(receipt_repo, [(self.receipt_for(receipt_repo, ev, backend="cpu", spec_path=receipt_spec), None) for ev in (bound, foreign, none)]
                               + [(self.receipt_for(receipt_repo, ev, outcome="FAIL", spec_path=receipt_spec), None) for ev in (ft_foreign, ft_bound, ft_none)], module=module)
        pl = self.status_batch(plain_repo, [(self.receipt_for(plain_repo, ev, backend="cpu", spec_path=plain_spec), None) for ev in (none, bound)], module=module)
        return rc, pl

    def test_a_pace_leg_run_receipt_hash_is_accepted_only_where_the_committed_spec_names_it(self) -> None:
        rc, pl = self.pace_cases()
        self.expect(rc[0], "ADVISORY", "the pace spec's receipt, bound in the hashed summary")
        self.expect(rc[1], "INVALID", "another receipt hash", "LOOK_RECEIPT_MISMATCH")
        self.expect(rc[2], "INVALID", "a pace receipt leg whose run recorded none", "LOOK_RECEIPT_MISMATCH")
        self.expect(rc[3], "INVALID", "a failure terminal stating another receipt hash (adversary A1)", "LOOK_RECEIPT_MISMATCH")
        self.expect(rc[4], "ADVISORY", "a failure terminal stating the spec's receipt hash")
        self.expect(rc[5], "ADVISORY", "a failure terminal stating none (the variant edit reaches the success summary only)")
        self.expect(pl[0], "ADVISORY", "no receipt on either side (every earlier speed leg)")
        self.expect(pl[1], "INVALID", "a receipt hash on a speed leg whose spec names none", "LOOK_RECEIPT_MISMATCH")

    def test_mutation_without_the_pace_rule_a_foreign_pace_receipt_hash_is_advisory(self) -> None:
        mutated = self.mutated_module([(self.PACE_RULE, self.PACE_RULE.replace("if (", "if ($false -and ", 1))])
        rc, pl = self.pace_cases(module=mutated)
        self.expect(rc[1], "ADVISORY", "with the pace rule removed a foreign receipt hash is believed -- so the rule is what refuses it")
        self.expect(rc[3], "ADVISORY", "with the pace rule removed a failure terminal's foreign hash is believed")
        self.expect(pl[1], "ADVISORY", "with the pace rule removed a receipt hash on a plain speed leg is believed")


@requires_windows_pwsh
class LookReceiptGeneratorTests(unittest.TestCase):
    """(d) The generator's -LookReceiptPath: without it the emitted job is byte-identical to the base (#328 head) generator's for the same arguments;
    with it the job embeds the receipt base64 + sha256, passes -Receipt exactly once and records lookReceiptSha256 in its summary; off a look leg it throws."""

    LOOK_ARGS = ["-Backend", "cpu", "-ScaleFactor", "2", "-ExpectedScaleRequest", "2", "-TelemetryArm", "LIGHT", "-CpuQuiescenceThresholdPercent", "95",
                 "-ContactSheet", "-ContactSheetFrames", "6", "-ForceLookAssist"]

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="dve-lookrcpt-")
        cls.tmp = Path(cls._tmp.name)
        cls.head = git("rev-parse", "HEAD")
        cls.repo = cls.tmp / "repo"
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(ROOT), str(cls.repo)], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "sparse-checkout", "set", "--cone", "tools", "tests/fixtures/clips"], check=True)
        subprocess.run(["git", "-C", str(cls.repo), "checkout", "-q", cls.head], check=True)
        for stem in FIXTURE_IDS:
            write_synthetic_mlv(cls.repo / "tests" / "fixtures" / "clips" / (stem + MLV_EXT), FRAMES_30S_AT_23976)
        cls.base_root = cls.tmp / "base"
        # KEEPALIVE-FAILURE-PUBLISH-LAST-1 r2: the generator base is the dual-venue byte-identity pin (BASELINE_COMMIT), the same generator the default-job tests compare against,
        # so the keep-alive reorder (an in-place edit of baseline lines) is pinned in ONE place; LOOK_RECEIPT_BASE still anchors the legs diff.
        cls.base_available = subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{BASELINE_COMMIT}^{{commit}}"], capture_output=True).returncode == 0
        if cls.base_available:
            tar = cls.tmp / "base.tar"
            subprocess.run(["git", "-C", str(ROOT), "archive", BASELINE_COMMIT, "--format=tar", "-o", str(tar), "tools/profiling", "tools/gates"], check=True)
            cls.base_root.mkdir()
            subprocess.run(["tar", "-xf", str(tar), "-C", str(cls.base_root)], check=True)
        cls.receipt = cls.tmp / "look-receipt.marxml"
        cls.receipt.write_bytes(AGXOFF_RECEIPT.read_bytes().replace(b"\r\n", b"\n"))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def run_generator(self, script: Path, out_name: str, extra: list[str]) -> tuple[subprocess.CompletedProcess, Path]:
        out = self.tmp / out_name
        proc = run_pwsh(["-File", str(script), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                         "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
        return proc, out

    def generate(self, script: Path, out_name: str, extra: list[str]) -> Path:
        proc, out = self.run_generator(script, out_name, extra)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out

    def test_without_a_receipt_the_job_is_byte_identical_to_the_base_generators(self) -> None:
        if not self.base_available:
            self.skipTest(f"baseline commit {BASELINE_COMMIT[:12]} is not in this clone")
        base_gen = self.base_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1"
        for name, extra in (("default", []), ("cpu-look-film", [*self.LOOK_ARGS, "-LookFlavor", "film"]),
                            ("cpu-look-cinematic", [*self.LOOK_ARGS, "-LookFlavor", "cinematic"]),
                            ("cuda-look-cinematic", ["-ContactSheet", "-ContactSheetFrames", "6", "-ForceLookAssist", "-LookFlavor", "cinematic"])):
            with self.subTest(variant=name):
                new = self.generate(GENERATOR, f"new-{name}.job.ps1", extra).read_bytes()
                old = self.generate(base_gen, f"base-{name}.job.ps1", extra).read_bytes()
                # Every card's lines sit inside its own sentinel regions (pinned by GeneratorByteIdentityAndVariantTests); outside them the job is still the
                # base generator's bytes, and the base carries the same regions as the new job (it is the pinned KEEPALIVE-FAILURE-PUBLISH-LAST-1 generator).
                kept, new_counts = GeneratorByteIdentityAndVariantTests.strip_regions(new.decode("utf-8"))
                old_kept, old_counts = GeneratorByteIdentityAndVariantTests.strip_regions(old.decode("utf-8"))
                self.assertGreater(new_counts["session-locked"], 0)
                # PLAYBACK-VSYNC-DEFAULT-1's regions: present in the new job and in the base generator (the pinned GPU-PREF-REGISTRY-STOP-AND-PRUNE-1 commit), at their pinned count.
                self.assertEqual((new_counts.pop("playback-vsync"), old_counts.pop("playback-vsync")),
                                 (GeneratorByteIdentityAndVariantTests.PLAYBACK_VSYNC_REGIONS, GeneratorByteIdentityAndVariantTests.PLAYBACK_VSYNC_REGIONS), name)
                # HFR-CONFORM-AWARE-RECEIPT-ORACLES-1's regions: present in both too, at their pinned count.
                self.assertEqual((new_counts.pop("hfr-conform-oracles"), old_counts.pop("hfr-conform-oracles")),
                                 (GeneratorByteIdentityAndVariantTests.HFR_CONFORM_REGIONS, GeneratorByteIdentityAndVariantTests.HFR_CONFORM_REGIONS), name)
                # PLAYBACK-CLIP-LENGTH-HFR-10S-1's region: in the new job only (the base predates the owner's 2026-10-15 amendment).
                self.assertEqual((new_counts.pop("hfr-10s-floor"), old_counts.pop("hfr-10s-floor")),
                                 (GeneratorByteIdentityAndVariantTests.HFR_10S_REGIONS, 0), name)
                # PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1 r2's region: in the new job only (the base predates the HEAVY frame-log opt-out).
                self.assertEqual((new_counts.pop("heavy-frame-log"), old_counts.pop("heavy-frame-log")),
                                 (GeneratorByteIdentityAndVariantTests.HEAVY_FRAME_LOG_REGIONS, 0), name)
                # PROCESSED8-MEASURED-BRANCH-HEAVY-LEG-SPEC-1 r6: the r5 cpu block is in every cpu job (its three bracketed regions) and in no cuda job; the base predates it.
                self.assertEqual((new_counts.pop("cpu-gl-env-clear"), old_counts.pop("cpu-gl-env-clear")),
                                 (GeneratorByteIdentityAndVariantTests.CPU_GL_ENV_CLEAR_REGIONS if name.startswith("cpu") else 0, 0), name)
                self.assertEqual(new_counts, old_counts, f"{name}: the job without -LookReceiptPath carries the base generator's bracketed regions")
                self.assertEqual(kept, old_kept, f"{name}: the job without -LookReceiptPath must be the base generator's bytes")
                self.assertNotIn(b"LookReceipt", new)

    def test_with_a_receipt_the_job_embeds_its_bytes_passes_minus_receipt_once_and_records_its_hash(self) -> None:
        sha = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        text = self.generate(GENERATOR, "receipt.job.ps1", [*self.LOOK_ARGS, "-LookFlavor", "film", "-LookReceiptPath", str(self.receipt)]).read_text(encoding="utf-8")
        [b64] = re.findall(r"(?m)^\$LookReceiptBase64 = '([A-Za-z0-9+/=]+)'\r?$", text)
        import base64
        self.assertEqual(base64.b64decode(b64), self.receipt.read_bytes())
        self.assertEqual(re.findall(r"(?m)^\$LookReceiptSha256 = '([0-9a-f]{64})'\r?$", text), [sha])
        self.assertEqual(text.count(" -Receipt "), 1, "the smoke runner is passed -Receipt exactly once")
        self.assertIn("-RequireLookAssist:`$true -Receipt $(ConvertTo-PsSingleQuoted $LookReceiptJobPath) -Scope none", text)
        self.assertEqual(text.count("    lookReceiptSha256 = $LookReceiptSha256\n") + text.count("    lookReceiptSha256 = $LookReceiptSha256\r\n"), 1, "the success summary records the hash")
        self.assertIn("RESULT=LOOK_RECEIPT_SHA_MISMATCH", text)
        self.assertLess(text.index("$LookReceiptJobPath = Join-Path $Work 'look-receipt.marxml'"), text.index(" -Receipt $(ConvertTo-PsSingleQuoted $LookReceiptJobPath)"),
                        "the receipt is written and re-verified before the smoke command is built")
        parsed = run_pwsh(["-Command", f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{self.tmp / 'receipt.job.ps1'}', [ref]$null, [ref]$e); $e.Count"])
        self.assertEqual(parsed.stdout.strip(), "0", parsed.stdout + parsed.stderr)

    def test_a_receipt_off_a_look_or_pace_leg_is_refused_before_anything_is_emitted(self) -> None:
        # VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: a capture-free pace leg (-ForceLookAssist -LookPaceLeg) may carry one now; a plain speed leg still may not.
        for name, extra in (("speed", []), ("unpaced speed", ["-Unpaced", "-SwapInterval", "0"])):
            with self.subTest(leg=name):
                proc, out = self.run_generator(GENERATOR, f"refused-{name.replace(' ', '-')}.job.ps1", [*extra, "-LookReceiptPath", str(self.receipt)])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("DUAL_VENUE_LOOK_RECEIPT_REQUIRES_LOOK_LEG", proc.stdout + proc.stderr)
                self.assertFalse(out.exists())

    def test_a_pace_leg_receipt_is_embedded_passed_once_and_recorded_like_a_look_legs(self) -> None:
        """VENUE-LEG-AGXOFF-UNPACED-HEADROOM-REMEASURE-1: on a capture-free pace leg (-ForceLookAssist -LookPaceLeg, unpaced, no contact sheet) the
        receipt ships the way a look leg's does: base64 + sha256, -Receipt exactly once, written and re-verified before the smoke command, its hash in
        the success summary; the summary still says lookLeg false (a pace leg)."""
        sha = hashlib.sha256(self.receipt.read_bytes()).hexdigest()
        pace = ["-Backend", "cpu", "-ScaleFactor", "2", "-ExpectedScaleRequest", "2", "-TelemetryArm", "LIGHT", "-CpuQuiescenceThresholdPercent", "95",
                "-ForceLookAssist", "-LookPaceLeg", "-LookFlavor", "cinematic", "-Unpaced", "-SwapInterval", "0"]
        plain = self.generate(GENERATOR, "pace-plain.job.ps1", pace).read_text(encoding="utf-8")
        self.assertNotIn("LookReceipt", plain)
        text = self.generate(GENERATOR, "pace-receipt.job.ps1", [*pace, "-LookReceiptPath", str(self.receipt)]).read_text(encoding="utf-8")
        [b64] = re.findall(r"(?m)^\$LookReceiptBase64 = '([A-Za-z0-9+/=]+)'\r?$", text)
        import base64
        self.assertEqual(base64.b64decode(b64), self.receipt.read_bytes())
        self.assertEqual(re.findall(r"(?m)^\$LookReceiptSha256 = '([0-9a-f]{64})'\r?$", text), [sha])
        self.assertEqual(text.count(" -Receipt "), 1, "the smoke runner is passed -Receipt exactly once")
        self.assertIn("-RequireLookAssist:`$true -Receipt $(ConvertTo-PsSingleQuoted $LookReceiptJobPath) -Scope none", text)
        self.assertEqual(text.count("    lookReceiptSha256 = $LookReceiptSha256\n") + text.count("    lookReceiptSha256 = $LookReceiptSha256\r\n"), 1, "the success summary records the hash")
        self.assertIn("    lookLeg = ($LookLeg -and -not $LookPaceLeg)", text)
        self.assertLess(text.index("$LookReceiptJobPath = Join-Path $Work 'look-receipt.marxml'"), text.index(" -Receipt $(ConvertTo-PsSingleQuoted $LookReceiptJobPath)"))
        self.assertEqual(text.count("'MLVAPP_PLAYBACK_UNPACED=1',"), 1)
        parsed = run_pwsh(["-Command", f"$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{self.tmp / 'pace-receipt.job.ps1'}', [ref]$null, [ref]$e); $e.Count"])
        self.assertEqual(parsed.stdout.strip(), "0", parsed.stdout + parsed.stderr)


# ---------------------------------------------------------------------------------------------------
# PLAYBACK-VISUAL-INSPECTION-1: every speed leg keeps window captures a reviewer opens. Owner, 2026-10-13: "you should be inspecting how each
# run looks. I thought that was being done via screnshots". The capture runs AFTER the measured window (a capture inside it roughly doubles
# render cost), and its gate catches the measured trap the cadence numbers cannot: a frozen display. All test names carry "visual" (-k visual).
VISUAL_OPEN = "PLAYBACK-VISUAL-INSPECTION-1 >>>"
VISUAL_CLOSE = "PLAYBACK-VISUAL-INSPECTION-1 <<<"
VISUAL_FUNCTIONS = ("Test-AttrCudaVisualCaptureSupport", "Add-AttrCudaVisualCaptureArgument", "Get-AttrCudaPresentMonWindowResult", "Publish-AttrCudaVisualCaptures")
ATTR_MODULE = ROOT / "tools" / "profiling" / "bachelor" / "AttrCudaArtifacts.psm1"
VIEWPORT = {"x": 10, "y": 20, "w": 300, "h": 160}

# One synthetic window per capture (never footage): grey "chrome" around a 300x160 viewport holding a gradient and a bar. -Seed moves the bar (a
# different picture), -Chrome changes only the chrome (a timecode that ticks over a frozen picture), -Bias shifts the red channel (another look),
# -Blank draws a black window.
VISUAL_PNG_PS = r"""
Add-Type -AssemblyName System.Drawing
function New-VisPng([string]$Path, [int]$Seed, [int]$Chrome, [int]$Bias, [bool]$Blank) {
    $b = [System.Drawing.Bitmap]::new(320, 200)
    $g = [System.Drawing.Graphics]::FromImage($b)
    if ($Blank) { $g.Clear([System.Drawing.Color]::Black) } else {
        $g.Clear([System.Drawing.Color]::FromArgb($Chrome, $Chrome, $Chrome))
        for ($x = 0; $x -lt 300; $x += 10) {
            $br = [System.Drawing.SolidBrush]::new([System.Drawing.Color]::FromArgb([int][math]::Min(255, 40 + $x / 2 + $Bias), 90, [int](160 - $x / 3)))
            $g.FillRectangle($br, 10 + $x, 20, 10, 160); $br.Dispose()
        }
        $g.FillRectangle([System.Drawing.Brushes]::White, 10 + ($Seed * 40) % 280, 60, 20, 80)
    }
    $g.Dispose(); $b.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png); $b.Dispose()
}
"""
# The file's requires_windows_pwsh idiom, with the reason spelled out for a test that drives Read-DvVisualCaptures over real PNGs: New-VisPng above,
# the reader's pixel measure and New-DvVisualStrip all use System.Drawing (GDI+), which .NET supports only on Windows. Off Windows the reader
# measures no frame and cannot compose the strip, so it says NOT_CAPTURED -- the venue's fail-closed answer, not the verdict under test (hosted
# Tests run 40111006101, ubuntu shard 3/4). Get-DvVisualVerdict takes counts, not pixels, so its tests stay unguarded and run on every OS.
requires_windows_imaging = unittest.skipIf(PWSH is None or sys.platform != "win32",
                                           "needs pwsh on Windows: the fixture PNGs, the visual reader's pixel measure and its strip composer use System.Drawing")


def write_visual_captures(dest: Path, mode: str = "ok") -> None:
    """The job's published visual\\ directory, as the app and the job write it: play-0..5.png, stop.png, paused.png, visual.json, and the
    job's visual-capture-request.json. mode: ok | frozen (one picture, ticking chrome, advancing app frames) | blank | diverge (the paused
    full-quality frame has another red level). PLAYBACK-FROZEN-DISPLAY-SOURCE-DIFF-1 modes: frozen (the app records source_difference "differ"),
    frozen-unknown (it records nothing), frozen-identical (it records "identical": a static clip), noviewport (a ticking-chrome frozen picture whose
    visual.json carries no viewport rectangle), frozen-partial (frozen + "differ", but play frame 5 alone carries no viewport rectangle),
    frozen-upper-differ / frozen-upper-identical (the same record in the wrong case: "DIFFER" / "IDENTICAL"), partial-ok (six distinct pictures, but play
    frame 5 alone carries no viewport rectangle: a plain OK with a not-judged note). PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1 adds two modes whose stop
    entry carries the app's binding fields: lagged (the stop capture shows an older picture than its label names: painted_serial != serial,
    label_bound false) and bound (the unforced stop-unbound.png shows the older picture; the forced, bound stop.png matches paused.png).
    PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 modes (a build that pairs each forced play capture with an unforced play-unbound-<k>.png, and binds the
    paused capture too): bound also carries the paused capture's binding (label_bound true) and every bound entry its label_bound_basis;
    bound-paused-unbound (paused label_bound false) and bound-paused-missing (paused without binding fields); paired-ok (unforced and forced play
    frames both move); masked (the unforced play frames show ONE picture over a known-differ source while the forced ones move: the frozen display
    the force-paint used to hide). r5 (Grok G1/G3 on #456): the paired modes model the r3 order -- the unforced captures are taken half a tail
    after the forced ones, so on a healthy display (paired-ok) unforced k shows a LATER frame than forced k, never the same picture; masked shows
    the last forced paint (play 5's picture) on every unforced capture; partial-freeze (over a known-differ source) shows three moving unforced
    pictures and then repeats the third, the display's own paint stopping part-way through the unforced half."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    calls = []
    frozen = mode in ("frozen", "frozen-unknown", "frozen-identical", "frozen-partial", "frozen-upper-differ", "frozen-upper-identical", "noviewport")
    paired = mode in ("paired-ok", "masked", "partial-freeze")
    bound = mode in ("bound", "bound-paused-unbound", "bound-paused-missing")
    viewport = None if mode == "noviewport" else VIEWPORT
    source_difference = {"frozen": "differ", "frozen-partial": "differ", "frozen-identical": "identical",
                         "frozen-upper-differ": "DIFFER", "frozen-upper-identical": "IDENTICAL", "masked": "differ", "partial-freeze": "differ"}.get(mode)
    for k in range(6):
        seed = 1 if frozen else k
        calls.append(f"New-VisPng '{dest / f'play-{k}.png'}' {seed} {60 + k * 10} 0 ${'true' if mode == 'blank' else 'false'}")
        if paired:
            # (seed, bias): bias 30 marks a frame of the unforced half (later than every forced capture's frame); masked repeats forced play 5's paint
            unforced_seed, unforced_bias = {"masked": (5, 0), "partial-freeze": (min(k, 2), 30)}.get(mode, (k, 30))
            calls.append(f"New-VisPng '{dest / f'play-unbound-{k}.png'}' {unforced_seed} {60 + k * 10} {unforced_bias} $false")
    if bound:
        calls.append(f"New-VisPng '{dest / 'stop-unbound.png'}' 5 120 40 $false")
    calls.append(f"New-VisPng '{dest / 'stop.png'}' 5 120 {40 if mode == 'lagged' else 0} ${'true' if mode == 'blank' else 'false'}")
    calls.append(f"New-VisPng '{dest / 'paused.png'}' 5 120 {40 if mode == 'diverge' else 0} ${'true' if mode == 'blank' else 'false'}")
    proc = run_pwsh(["-Command", VISUAL_PNG_PS + "\n".join(calls)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    frames = [{"kind": "play", "index": k, "file": f"play-{k}.png", "display_frame": 600 + 2 * k, "serial": 900 + k, "play_active": True,
               "tail_elapsed_ms": 250 + 500 * k, "capture_ms": 40.0, "viewport": (None if mode in ("frozen-partial", "partial-ok") and k == 5 else viewport), "saved": True, "reason": ""}
              for k in range(6)]
    if paired:
        # r3 order and shape: the six forced play captures across the first half of the tail, then the six unforced ones across the second, each
        # entry carrying forced_paints_before (see write_stateful_visual_captures for a surface the forced paints change)
        for k, f in enumerate(frames):
            f.update({"tail_elapsed_ms": 125 + 250 * k, "forced_paints_before": k})
        frames += [{"kind": "play_unbound", "index": k, "file": f"play-unbound-{k}.png", "display_frame": 612 + 2 * k, "serial": 906 + k, "play_active": True,
                    "tail_elapsed_ms": 1625 + 250 * k, "capture_ms": 40.0, "viewport": viewport, "saved": True, "reason": "", "forced_paints_before": 6}
                   for k in range(6)]
    if bound:
        frames.append({"kind": "stop_unbound", "index": 0, "file": "stop-unbound.png", "display_frame": 612, "serial": 912, "painted_serial": 911,
                       "painted_serial_valid": True, "label_bound": False, "label_bound_basis": "gl_viewport_paint", "play_active": False, "viewport": viewport,
                       "saved": True, "reason": ""})
    stop = {"kind": "stop", "index": 0, "file": "stop.png", "display_frame": 612, "play_active": False, "viewport": viewport, "saved": True, "reason": ""}
    if mode == "lagged" or bound:
        stop.update({"serial": 912, "painted_serial": 911 if mode == "lagged" else 912, "painted_serial_valid": True, "label_bound": bound})
    if bound:
        stop["label_bound_basis"] = "pixmap_item_by_construction"
    frames.append(stop)
    paused = {"kind": "paused", "index": 0, "file": "paused.png", "display_frame": 612, "settled": True, "settled_frame": 612, "play_active": False,
              "viewport": viewport, "saved": True, "reason": ""}
    if mode in ("bound", "bound-paused-unbound"):
        paused.update({"serial": 913, "painted_serial": 913 if mode == "bound" else 912, "painted_serial_valid": True, "label_bound": mode == "bound",
                       "label_bound_basis": "pixmap_item_by_construction"})
    frames.append(paused)
    visual = {"schema": "mlv-app/visual-capture/v1", "method": "PrintWindow(PW_RENDERFULLCONTENT)", "tail_ms": 3000,
              "tail_begin_utc": "2026-10-13T22:00:00.000Z", "tail_end_utc": "2026-10-13T22:00:03.100Z",
              "frames": frames, "frames_written": 8, "error": ""}
    if source_difference:
        visual["source_difference"] = source_difference
    (dest / "visual.json").write_text(json.dumps(visual), encoding="utf-8")
    (dest / "visual-capture-request.json").write_text(json.dumps({"schema": "mlv-app/visual-capture-request/v1", "requested": True, "probe": "supported",
                                                                  "reason": None, "forwarded": True, "presentMonWindowEndUtc": "2026-10-13T22:00:00.000Z"}), encoding="utf-8")


def stateful_visual_model(order: str, natural: str, ordinal: bool = True, record_source: bool = True,
                          observer: tuple[int, ...] = (), no_viewport: tuple[int, ...] = ()) -> tuple[list[tuple[str, int, int, int]], dict, dict]:
    """PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r3 (Dot 7dc103f1 B2): a STATEFUL model of the window surface the captures print. A forced capture paints
    the frame its label names onto the surface, then prints it, so it CHANGES what the next unforced capture sees; an unforced capture prints the
    surface as it stands. Before every capture the display's own (natural) paint lands the current frame only while it works. natural: healthy
    (always), stuck (never: only a forced capture changes the surface), partial (until the fourth unforced play capture, then never) or static
    (always, but every source frame is one picture: a static clip the app records as identical). order is the app's capture order: interleaved (the
    r2 build b35aa308: unforced k, then forced k, in each slice) or forced-first (r3: the six forced play captures across the first half of the tail,
    then the six unforced ones across the second, then stop_unbound, stop). ordinal writes each entry's forced_paints_before (an r3 build); without
    it the entries have the r2 shape. record_source=False leaves source_difference out of visual.json (nothing records whether the source frames
    differ). r5: observer lists unforced play indexes whose entry records one forced paint more than the model counted (the reader must leave them
    unjudged); no_viewport lists unforced play indexes whose entry carries no viewport rectangle.
    Pure (r5, Dot f589cdbf): no file and no pwsh. Returns (pictures, visual, unforced): one (file, seed, chrome, bias) per capture for New-VisPng,
    the visual.json body, and the unforced play pictures the model produced (index -> picture) for the trace assertions."""
    if order == "interleaved":
        plays = [op for k in range(6) for op in (("play_unbound", k, 240 + 500 * k, 600 + 2 * k), ("play", k, 250 + 500 * k, 600 + 2 * k))]
        last = 610
    else:
        plays = [("play", k, 125 + 250 * k, 600 + k) for k in range(6)] + [("play_unbound", k, 1625 + 250 * k, 606 + k) for k in range(6)]
        last = 611
    ops = plays + [("stop_unbound", 0, 3000, last + 1), ("stop", 0, 3000, last + 1), ("paused_unbound", 0, 3400, last + 1), ("paused", 0, 3400, last + 1)]
    cut = next(t for kind, k, t, _ in ops if kind == "play_unbound" and k == 3)   # partial: the natural paint stops landing here

    def picture(label: int) -> tuple[int, int]:
        return (2, 0) if natural == "static" else (label % 7, ((label - 600) // 7) * 30)

    surface = (3, 120)   # what was on screen before the tail: no frame's picture
    forced_paints = 0
    pictures, frames, unforced = [], [], {}
    for i, (kind, k, t, label) in enumerate(ops):
        if natural in ("healthy", "static") or (natural == "partial" and t < cut):
            surface = picture(label)
        forced = not kind.endswith("_unbound")
        if forced:
            surface = picture(label)
        file = f"{kind.replace('_', '-')}-{k}.png" if kind.startswith("play") else f"{kind.replace('_', '-')}.png"
        pictures.append((file, surface[0], 60 + 5 * i, surface[1]))
        if kind == "play_unbound":
            unforced[k] = surface
        entry = {"kind": kind, "index": k, "file": file, "display_frame": label, "serial": 900 + label - 600, "play_active": kind.startswith("play"),
                 "tail_elapsed_ms": t, "capture_ms": 40.0, "viewport": None if kind == "play_unbound" and k in no_viewport else VIEWPORT,
                 "saved": True, "reason": ""}
        if kind in ("stop", "paused"):
            entry.update({"painted_serial": 900 + label - 600, "painted_serial_valid": True, "label_bound": True, "label_bound_basis": "gl_window_paint"})
        if kind == "paused":
            entry.update({"settled": True, "settled_frame": label})
        if ordinal:
            entry["forced_paints_before"] = forced_paints + (1 if kind == "play_unbound" and k in observer else 0)
        frames.append(entry)
        if forced:
            forced_paints += 1
    visual = {"schema": "mlv-app/visual-capture/v1", "method": "PrintWindow(PW_RENDERFULLCONTENT)", "tail_ms": 3000,
              "tail_begin_utc": "2026-10-13T22:00:00.000Z", "tail_end_utc": "2026-10-13T22:00:03.100Z", "frames": frames, "frames_written": len(frames),
              "error": ""}
    if record_source:
        visual["source_difference"] = "identical" if natural == "static" else "differ"
    return pictures, visual, unforced


def write_stateful_visual_captures(dest: Path, order: str, natural: str, ordinal: bool = True, record_source: bool = True,
                                   observer: tuple[int, ...] = (), no_viewport: tuple[int, ...] = ()) -> dict:
    """stateful_visual_model's captures written as the job publishes them: one PNG per capture (New-VisPng, through pwsh), visual.json and the job's
    visual-capture-request.json. Returns the model's unforced play pictures (index -> picture)."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    pictures, visual, unforced = stateful_visual_model(order, natural, ordinal, record_source, observer, no_viewport)
    calls = [f"New-VisPng '{dest / file}' {seed} {chrome} {bias} $false" for file, seed, chrome, bias in pictures]
    proc = run_pwsh(["-Command", VISUAL_PNG_PS + "\n".join(calls)])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (dest / "visual.json").write_text(json.dumps(visual), encoding="utf-8")
    (dest / "visual-capture-request.json").write_text(json.dumps({"schema": "mlv-app/visual-capture-request/v1", "requested": True, "probe": "supported",
                                                                  "reason": None, "forwarded": True, "presentMonWindowEndUtc": "2026-10-13T22:00:00.000Z"}), encoding="utf-8")
    return unforced


class VisualCaptureObserverPaintTests(unittest.TestCase):
    """PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r3 (Dot 7dc103f1 B2 on b35aa308): pairing each forced capture with an unforced one in the same slice does not
    isolate the NEXT unforced capture from the forced paint. With the display's own paint stuck, unforced k+1 shows forced k's paint (trace: initial,
    F0, F1, F2, F3, F4), so six distinct unforced pictures read as a moving display and neither FROZEN_DISPLAY nor the masking note fires. Driven by
    write_stateful_visual_captures, whose surface each forced capture mutates. The tests that read real PNGs are requires_windows_imaging (r4); the
    model's own trace (pure Python since r5: stateful_visual_model writes no file) and the count-only verdict gate run on every OS."""

    def read(self, d: str, order: str, natural: str, ordinal: bool = True, record_source: bool = True,
             observer: tuple[int, ...] = (), no_viewport: tuple[int, ...] = ()) -> tuple[dict, dict]:
        art = Path(d) / "artifacts"
        unforced = write_stateful_visual_captures(art / "visual", order, natural, ordinal, record_source, observer, no_viewport)
        proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{art}' -EvidenceDir '{Path(d) / 'evidence'}' -LegId 'unit-leg' -Header 'unit' "
                                  "| ConvertTo-Json -Depth 8 -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return unforced, out

    def test_the_visual_model_reproduces_dots_trace_on_the_r2_interleaved_order(self) -> None:
        # the model itself: on the r2 order with the natural paint stuck, unforced k+1 shows forced k's picture (no file, no pwsh: r5, Dot f589cdbf)
        _, _, unforced = stateful_visual_model("interleaved", "stuck")
        self.assertEqual([unforced[k] for k in range(6)], [(3, 120)] + [((600 + 2 * k) % 7, (2 * k // 7) * 30) for k in range(5)])
        self.assertEqual(len(set(unforced.values())), 6, "six distinct unforced pictures over a frozen display")

    def test_the_visual_model_traces_the_r3_order(self) -> None:
        # r3 order: stuck shows forced play 5's paint on every unforced capture; partial moves three times, then repeats the third
        pictures = {natural: stateful_visual_model("forced-first", natural)[2] for natural in ("stuck", "partial", "healthy")}
        self.assertEqual(set(pictures["stuck"].values()), {(605 % 7, 0)})
        self.assertEqual([pictures["partial"][k] for k in range(6)], [(4, 0), (5, 30), (6, 30), (6, 30), (6, 30), (6, 30)])
        self.assertEqual(len(set(pictures["healthy"].values())), 6)
        _, visual, _ = stateful_visual_model("forced-first", "partial", observer=(5,), no_viewport=(0,))
        unforced = {f["index"]: f for f in visual["frames"] if f["kind"] == "play_unbound"}
        self.assertEqual([unforced[k]["forced_paints_before"] for k in range(6)], [6, 6, 6, 6, 6, 7])
        self.assertEqual([unforced[k]["viewport"] is None for k in range(6)], [True] + [False] * 5)

    @requires_windows_imaging
    def test_the_visual_reader_never_says_ok_over_unforced_captures_a_forced_paint_reached(self) -> None:
        # the r2 build's interleaved order, with the r2 entry shape (no ordinal) and with an ordinal: either way some unforced play captures follow a
        # forced paint, so they cannot show a frozen display; the runner judges only an interval no forced paint entered, and fails closed when that
        # leaves fewer than two -- even over a healthy display, whose pictures it cannot tell from observer paints
        for natural, ordinal in (("stuck", False), ("stuck", True), ("healthy", True), ("partial", True)):
            with self.subTest(natural=natural, ordinal=ordinal), tempfile.TemporaryDirectory(prefix="vis-r2-order-") as d:
                _, out = self.read(d, "interleaved", natural, ordinal)
                self.assertNotEqual(out["verdict"], "OK", out["reason"])
                self.assertEqual(out["verdict"], "VISUAL_ASSESSMENT_UNAVAILABLE", out["reason"])
                self.assertEqual(out["frozenJudgedOn"], "play_unbound")
                self.assertEqual((out["playFrames"], out["observerPaintedPlayFrames"]), (1 if ordinal else 0, 5 if ordinal else 6))
                self.assertIn("forced paint", out["reason"])

    @requires_windows_imaging
    def test_the_visual_reader_says_frozen_when_the_natural_paint_is_stuck_on_the_r3_order(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vis-r3-stuck-") as d:
            unforced, out = self.read(d, "forced-first", "stuck")
            self.assertEqual(len(set(unforced.values())), 1, "every unforced capture shows the last forced paint")
            self.assertEqual((out["verdict"], out["counted"]), ("FROZEN_DISPLAY", False), out["reason"])
            self.assertEqual((out["frozenJudgedOn"], out["playFrames"], out["distinctContentHashes"], out["observerPaintedPlayFrames"]), ("play_unbound", 6, 1, 0))
            self.assertIn("the force-paint would have masked this", out["reason"])
            self.assertIs(out["stopUnboundObserverClean"], True)

    @requires_windows_imaging
    def test_the_visual_reader_says_ok_over_a_healthy_display_on_the_r3_order(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vis-r3-healthy-") as d:
            _, out = self.read(d, "forced-first", "healthy")
            self.assertEqual(out["verdict"], "OK", out["reason"])
            self.assertEqual((out["playFrames"], out["distinctContentHashes"], out["observerPaintedPlayFrames"], out["repeatedPlayFrames"]), (6, 6, 0, 0))
            self.assertIs(out["stopUnboundObserverClean"], True)
            self.assertEqual([f["forcedPaintsBefore"] for f in out["frames"] if f["kind"] == "play_unbound"], [6] * 6)

    @requires_windows_imaging
    def test_the_visual_reader_reports_a_partial_freeze_on_the_r3_order(self) -> None:
        # the display's own paint stops landing at the fourth unforced capture: three unforced pictures repeat their predecessor while the app's label
        # advanced. Not the whole-tail FROZEN_DISPLAY (three pictures differ); r5 (hub ruling, ACK 6388020454): over source frames known to differ it
        # is PARTIAL_FREEZE -- never OK -- counted and surfaced
        with tempfile.TemporaryDirectory(prefix="vis-r3-partial-") as d:
            _, out = self.read(d, "forced-first", "partial")
            self.assertEqual((out["verdict"], out["counted"]), ("PARTIAL_FREEZE", True), out["reason"])
            self.assertEqual((out["playFrames"], out["distinctContentHashes"], out["repeatedPlayFrames"]), (6, 3, 3))
            self.assertIn("3 of the 6 judged play frames repeat the picture before them", out["reason"])
            self.assertIn("the display's own paint stopped landing for part of the tail", out["reason"], "the source frames are known to differ")
            self.assertEqual(out["reason"].count("repeat the picture before them"), 1, "the verdict reason is not followed by the same repeat note")
            self.assertIn("3 of the 6 unforced play captures repeat", out["repeatedPlayFramesNote"])

    @requires_windows_imaging
    def test_the_visual_reader_names_no_cause_for_a_repeat_over_unknown_source_content(self) -> None:
        # r4 (Dot a5acb3c8, hub ACK 6386874544): the same repeats, but nothing records whether the source frames differ, so a repeat may be the
        # source's own picture: the receipt says what it saw and does not name the display as the cause (r5: still a note on an OK block)
        with tempfile.TemporaryDirectory(prefix="vis-r3-partial-unknown-") as d:
            _, out = self.read(d, "forced-first", "partial", record_source=False)
            self.assertEqual(out["verdict"], "OK", out["reason"])
            self.assertEqual((out["sourceDifference"], out["distinctContentHashes"], out["repeatedPlayFrames"]), ("unknown", 3, 3))
            self.assertIn("3 of the 6 unforced play captures repeat the picture before them while the app's label advanced", out["reason"])
            self.assertIn("a possible partial freeze or repeated source pictures, not a proven cause", out["reason"])
            self.assertNotIn("the display's own paint stopped landing", out["reason"])
            self.assertEqual(out["repeatedPlayFramesNote"], out["reason"])

    @requires_windows_imaging
    def test_the_repeat_note_is_reported_whatever_reason_was_set_first(self) -> None:
        # r5 (Grok G4 6386577997, G3 6386571526): the repeat note used to be set only when no earlier note held `reason`, so an observer-painted or
        # viewport-unavailable note hid it and a half-tail freeze read as a plain OK note. repeatedPlayFramesNote carries it on its own; `reason`
        # keeps its first-wins rule. (observer / no_viewport: unforced play indexes; see stateful_visual_model)
        rows = [  # observer, no_viewport, the note that wins `reason`, judged, repeats, verdict over a known-differ source
            ((5,), (), "1 unforced play frame(s) followed a forced paint the others did not and were not judged", 5, 2, "PARTIAL_FREEZE"),
            ((), (0,), "1 saved play frame(s) have no usable viewport rectangle and were not judged", 5, 3, "PARTIAL_FREEZE"),
            ((4, 5), (), "2 unforced play frame(s) followed a forced paint the others did not and were not judged", 4, 1, "OK"),   # one repeat alone: OK
        ]
        for observer, no_viewport, first_note, judged, repeats, known_verdict in rows:
            for record_source in (False, True):
                with self.subTest(observer=observer, no_viewport=no_viewport, record_source=record_source), \
                        tempfile.TemporaryDirectory(prefix="vis-r5-repeat-note-") as d:
                    _, out = self.read(d, "forced-first", "partial", record_source=record_source, observer=observer, no_viewport=no_viewport)
                    self.assertEqual((out["playFrames"], out["repeatedPlayFrames"]), (judged, repeats))
                    note = out["repeatedPlayFramesNote"]
                    self.assertIsNotNone(note, out["reason"])
                    self.assertTrue(note.startswith(f"{repeats} of the {judged} unforced play captures repeat the picture before them"), note)
                    self.assertIn(first_note, out["reason"])
                    if record_source:
                        self.assertEqual((out["verdict"], out["counted"]), (known_verdict, True), out["reason"])
                        self.assertIn("the display's own paint stopped landing for part of the tail", note)
                    else:
                        self.assertEqual(out["verdict"], "OK", out["reason"])
                        self.assertTrue(out["reason"].startswith(first_note), "reason keeps its first-wins rule")
                        self.assertNotIn(note, out["reason"], "the earlier note still holds reason; the repeat note is in its own field")
                        self.assertIn("not a proven cause", note)

    @requires_windows_imaging
    def test_the_visual_reader_waives_a_repeated_source_on_the_r3_order(self) -> None:
        # a static clip: the display paints, but every source frame is the same picture, and the app records the source frames as identical
        with tempfile.TemporaryDirectory(prefix="vis-r3-static-") as d:
            _, out = self.read(d, "forced-first", "static")
            self.assertEqual((out["verdict"], out["counted"]), ("OK", True), out["reason"])
            self.assertEqual((out["distinctContentHashes"], out["sourceDifference"], out["repeatedPlayFrames"]), (1, "known-identical", None))
            self.assertIn("static clip", out["reason"])

    def test_the_visual_gate_fails_closed_when_observer_paints_leave_fewer_than_two_judged_frames(self) -> None:
        for judged, observer, verdict in ((1, 5, "VISUAL_ASSESSMENT_UNAVAILABLE"), (0, 6, "VISUAL_ASSESSMENT_UNAVAILABLE"), (6, 0, "OK")):
            with self.subTest(judged=judged, observer=observer):
                proc, out = run_pwsh_json(f"Get-DvVisualVerdict -Requested $true -Captured $true -PlayFrames {judged} -DistinctContentHashes {judged} -AppDistinctDisplayFrames {judged} "
                                          f"-FlatPlayFrames 0 -PlayVsPausedMeanAbsDiff 1.0 -SamePosition $true -ObserverPaintedPlayFrames {observer} | ConvertTo-Json -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual((out["verdict"], out["counted"]), (verdict, True))


class GeneratorWarmUpIsOptInPerClassTests(unittest.TestCase):
    """The generator class's concurrent warm-up (~60 jobs) belongs to the class that defines the warm list. A subclass that does not set `warm_first` in its own body
    (test_dve_leg_terminals.CpuGeneratedJobs.Boot asks for ~5 jobs) performs zero warm runs. No pwsh runs here: `warm_generated` is counted, not executed."""

    @staticmethod
    def stubs() -> tuple[list[str], type, type, type]:
        calls: list[str] = []
        key = (str(Path("job-generator.ps1")), ())

        class Defining(GeneratorByteIdentityAndVariantTests):
            warm_first = True

            @classmethod
            def setUpClass(cls) -> None:
                cls._generated = {key: (Path("cached.job.ps1"), subprocess.CompletedProcess([], 0, "", ""))}
                cls._parse_cache = {}
                cls._warmed = False

            @classmethod
            def tearDownClass(cls) -> None:
                pass

            @classmethod
            def warm_generated(cls) -> None:
                calls.append(cls.__name__)

            def runTest(self) -> None:  # pragma: no cover - never run
                pass

        class Inheriting(Defining):
            pass

        class OptedIn(Defining):
            warm_first = True

        return calls, Defining, Inheriting, OptedIn

    def request(self, cls: type) -> None:
        cls.setUpClass()
        cls("runTest").generate(Path("job-generator.ps1"), "x.job.ps1", [])

    def test_the_real_generator_class_defines_the_flag_in_its_own_body(self) -> None:
        self.assertIs(GeneratorByteIdentityAndVariantTests.__dict__.get("warm_first"), True)

    def test_the_defining_class_still_warms_once(self) -> None:
        calls, defining, _inheriting, _opted_in = self.stubs()
        self.request(defining)
        defining("runTest").generate(Path("job-generator.ps1"), "y.job.ps1", [])
        self.assertEqual(calls, ["Defining"])

    def test_a_subclass_that_does_not_opt_in_performs_zero_warm_runs(self) -> None:
        calls, _defining, inheriting, _opted_in = self.stubs()
        self.assertTrue(inheriting.warm_first, "the flag is still visible through inheritance; only the class's own body counts")
        self.request(inheriting)
        self.assertEqual(calls, [])

    def test_a_subclass_can_opt_in_in_its_own_body(self) -> None:
        calls, _defining, _inheriting, opted_in = self.stubs()
        self.request(opted_in)
        self.assertEqual(calls, ["OptedIn"])

    def test_the_visual_capture_borrower_performs_zero_warm_runs(self) -> None:
        self.assertFalse(VisualCaptureGeneratorTests.__dict__.get("warm_first", False))


@requires_windows_pwsh
class VisualCaptureGeneratorTests(unittest.TestCase):
    """-VisualCapture adds exactly three bracketed regions to the job (helpers + probe + argument; the PresentMon window cut; the publish) and nothing else."""

    setUpClass = classmethod(GeneratorByteIdentityAndVariantTests.setUpClass.__func__)
    tearDownClass = classmethod(GeneratorByteIdentityAndVariantTests.tearDownClass.__func__)
    warm_first = False   # six requests, not the generator class's ~60: nothing to warm
    generate = GeneratorByteIdentityAndVariantTests.generate
    generator_command = classmethod(GeneratorByteIdentityAndVariantTests.generator_command.__func__)
    parse_errors = GeneratorByteIdentityAndVariantTests.parse_errors

    @staticmethod
    def strip_visual(text: str) -> tuple[str, int, list[str]]:
        kept: list[str] = []
        regions: list[str] = []
        inside: list[str] | None = None
        for line in lf(text).split("\n"):
            if VISUAL_OPEN in line:
                assert inside is None, "nested visual region"
                inside = []
                continue
            if VISUAL_CLOSE in line:
                assert inside is not None, "unopened visual region"
                regions.append("\n".join(inside))
                inside = None
                continue
            if inside is None:
                kept.append(line)
            else:
                inside.append(line)
        assert inside is None, "unclosed visual region"
        return "\n".join(kept), len(regions), regions

    def test_the_visual_capture_job_is_the_default_job_plus_three_bracketed_regions(self) -> None:
        for name, extra in (("plain", []), ("windowed-sheet", ["-DisplayMode", "windowed", "-ContactSheet", "-ContactSheetFrames", "4"]), ("cpu", ["-Backend", "cpu"])):
            with self.subTest(name):
                base = self.generate(GENERATOR, f"vis-base-{name}.job.ps1", extra)
                on = self.generate(GENERATOR, f"vis-on-{name}.job.ps1", [*extra, "-VisualCapture"])
                self.assertEqual(self.parse_errors(on), 0)
                self.assertNotIn("visual-capture", base.read_text(encoding="utf-8"), "without -VisualCapture the job never names the option")
                kept, count, regions = self.strip_visual(on.read_text(encoding="utf-8"))
                self.assertEqual(count, 3)
                # region 2 REPLACES the one display-report call (the cut window is passed instead of the launcher's result); everything else is added
                report = ("$displayReport = Get-AttrCudaPresentMonDisplayReport -CsvPath $presentMonPath -ResultJson $resultJson "
                          "-EarliestCaptureStartUtc $presentMonCaptureStartUtc -LatestCaptureStartUtc $presentMonPostSpawnUtc\n")
                self.assertEqual(lf(base.read_text(encoding="utf-8")).count(report), 1)
                self.assertEqual(kept, lf(base.read_text(encoding="utf-8")).replace(report, ""),
                                 "the three regions (and the one report call region 2 replaces) are the ONLY difference from the job without -VisualCapture")
                for fn in VISUAL_FUNCTIONS:
                    self.assertIn(f"function {fn}", regions[0], "the helpers are spliced in the first region, never into the default embed list")
                # the order the job runs them in: probe before PresentMon, the window cut at the report, the publish before the contact sheet
                text = lf(on.read_text(encoding="utf-8"))
                self.assertLess(text.index("Test-AttrCudaVisualCaptureSupport -ExePath $exePath"), text.index("$PresentMonSessionName = Get-PresentMonSessionName $JobId"))
                self.assertIn("-ResultJson $presentMonWindowResult -EarliestCaptureStartUtc", regions[1])
                self.assertNotIn("Get-AttrCudaPresentMonDisplayReport -CsvPath $presentMonPath -ResultJson $resultJson ", text, "the uncut report call is replaced, not duplicated")
                self.assertLess(text.index("Publish-AttrCudaVisualCaptures -Record"), text.index("$files = Get-ChildItem -LiteralPath $Pub -Recurse -File"))

    def test_the_runner_maps_a_speed_leg_to_visual_capture_outside_the_forced_look_branch(self) -> None:
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = "if ($visualRequest.requested) { $gen['VisualCapture'] = $true }"
        self.assertIn(mapping, runner)
        self.assertGreater(runner.index(mapping), runner.index("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist']"))
        self.assertLess(runner.index(mapping), runner.index("$gen['ContactSheet'] = $true; $gen['ContactSheetFrames']"))

    def test_the_visual_capture_self_heal_pre_launches_keep_their_own_captures(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r5: the probe adds '--visual-capture-dir', <$Work\\visual> to $cmd BEFORE the self-heal pre-launches run, so each
        pre-launch (the fallback runs L1-L3) is pointed at its own pre-<n>\\visual and publishes it under self-heal\\pre-<n>; a swap that cannot be proven
        empties the command (PRE_LAUNCH_COMMAND_UNDERIVABLE). Without -VisualCapture the self-heal job never names a pre-launch capture."""
        self_heal = ["-TelemetryArm", "HEAVY", "-AutomationSettingsDir", "gpu-self-heal-settings", "-GpuPreLaunches", "702@120,702@120,none", "-GpuSafeStart", "clear"]
        off = lf(self.generate(GENERATOR, "vis-sh-off.job.ps1", self_heal).read_text(encoding="utf-8"))
        self.assertNotIn("preVisual", off)
        on_job = self.generate(GENERATOR, "vis-sh-on.job.ps1", [*self_heal, "-VisualCapture"])
        self.assertEqual(self.parse_errors(on_job), 0)
        text = lf(on_job.read_text(encoding="utf-8"))
        pre = text.index("# GPU-DEVICE-LOSS-SELF-HEAL-1 r2: the shared run-scoped settings store and the self-heal pre-launches")
        self.assertLess(text.index("$cmd = Add-AttrCudaVisualCaptureArgument -Command $cmd -VisualCaptureDir $visualCaptureDir"), pre,
                        "the measured run's capture argument is already on $cmd when the pre-launches derive theirs")
        swap = text.index("    $preVisualDir = Join-Path $preDir 'visual'\n")
        self.assertLess(text.index("    $preCmd = $cmd.Replace(", pre), swap)
        self.assertLess(swap, text.index("    if (-not $preCmd.Contains($preResult)", pre), "the swap runs before the command is proven and launched")
        publish = "    [void](Publish-AttrCudaVisualCaptures -Record $visualCaptureRecord -SourceDir $preVisualDir -PubRoot $prePub)\n"
        self.assertEqual(text.count(publish), 1)
        self.assertLess(text.index("    [void](New-AttrCudaDirectory -Path $prePub)\n"), text.index(publish))
        # the swap itself, run on the command shape the probe produced (a quote in the path included), and fail closed when the pair is absent
        lines = text[swap:text.index("    }\n", swap) + 6]
        script = ("$ErrorActionPreference = 'Stop'; Import-Module '" + str(ATTR_MODULE) + "' -Force\n"
                  "$visualCaptureRecord = @{ forwarded = $true }; $visualCaptureDir = \"C:\\w'k\\visual\"\n"
                  "foreach ($case in 'ok', 'absent') {\n"
                  "  $preDir = Join-Path $env:TEMP ('vis-sh-' + [guid]::NewGuid().ToString('N') + \"\\o'1\")\n"
                  "  $preCmd = \"& 'smoke.ps1' -Output 'r.json' -AdditionalArgs @('--windowed')\"\n"
                  "  if ($case -ceq 'ok') { $preCmd = Add-AttrCudaVisualCaptureArgument -Command $preCmd -VisualCaptureDir $visualCaptureDir }\n"
                  + lines +
                  "  Write-Output ($case + '=' + $preCmd)\n"
                  "  Write-Output ($case + '_dir=' + (Test-Path -LiteralPath $preVisualDir -PathType Container))\n"
                  "  Remove-Item -LiteralPath (Split-Path -Parent $preDir) -Recurse -Force -ErrorAction SilentlyContinue\n"
                  "}\n")
        proc = run_pwsh(["-Command", script])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)
        self.assertNotIn("C:\\w''k\\visual", out["ok"], "the measured run's capture directory is never handed to a pre-launch")
        self.assertRegex(out["ok"], r"^& 'smoke\.ps1' -Output 'r\.json' -AdditionalArgs @\('--windowed', '--visual-capture-dir', '.*\\o''1\\visual'\)$")
        self.assertEqual(out["ok_dir"], "True")
        self.assertEqual(out["absent"], "", "a command whose capture pair cannot be swapped is emptied, so the job's proof check refuses it")
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("Read-DvVisualCaptures -Requested $true -ArtifactsDir $preDir.FullName -EvidenceDir (Join-Path $preDir.FullName 'inspected')", runner)
        self.assertIn("$receipt['selfHealVisual'][$preDir.Name] = $preVisual", runner)


@requires_windows_pwsh
class VisualCaptureVenueHelperTests(unittest.TestCase):
    """The venue-side helpers the job splices in (AttrCudaArtifacts.psm1)."""

    def run_module(self, script: str) -> subprocess.CompletedProcess:
        return run_pwsh(["-Command", f"$ErrorActionPreference = 'Stop'; Import-Module '{ATTR_MODULE}' -Force\n" + script])

    def test_the_visual_capture_argument_joins_the_one_additional_args_array(self) -> None:
        proc = self.run_module(
            "Write-Output ('A=' + (Add-AttrCudaVisualCaptureArgument -Command \"& 'smoke.ps1' -Seconds 25\" -VisualCaptureDir 'C:\\w\\visual'))\n"
            "Write-Output ('B=' + (Add-AttrCudaVisualCaptureArgument -Command \"& 'smoke.ps1' -AdditionalArgs @('--contact-sheet-dir', 'C:\\w\\cs', '--windowed')\" -VisualCaptureDir \"C:\\w'x\\visual\"))\n"
            "try { [void](Add-AttrCudaVisualCaptureArgument -Command \"& 'smoke.ps1' -AdditionalArgs @('--windowed') -TracePath 't'\" -VisualCaptureDir 'v'); Write-Output 'C=no-throw' } catch { Write-Output ('C=' + $_.Exception.Message) }")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)
        self.assertEqual(out["A"], "& 'smoke.ps1' -Seconds 25 -AdditionalArgs @('--visual-capture-dir', 'C:\\w\\visual')")
        self.assertEqual(out["B"], "& 'smoke.ps1' -AdditionalArgs @('--contact-sheet-dir', 'C:\\w\\cs', '--windowed', '--visual-capture-dir', 'C:\\w''x\\visual')")
        self.assertTrue(out["C"].startswith("ATTRCUDA_VISUAL_CAPTURE_ARGS_NOT_LAST"), out["C"])

    def test_the_visual_publish_carries_the_unbound_stop_capture(self) -> None:
        # PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1: the first venue leg of a binding build wrote stop-unbound.png, and this publish filter
        # dropped it, so the runner could not compute stopUnboundVsPausedMeanAbsDiff. Every capture the app names is published; nothing else.
        with tempfile.TemporaryDirectory(prefix="vis-pub-") as d:
            src = Path(d, "src")
            src.mkdir()
            Path(d, "pub").mkdir()
            # (TIMING-2 r2: the unforced play captures, play-unbound-<k>.png, the frozen check is judged on, and paused-unbound.png are published too)
            for name in ("play-0.png", "play-unbound-0.png", "stop-unbound.png", "stop.png", "paused-unbound.png", "paused.png", "visual.json", "stray.png",
                         "stop-unbound.png.tmp", "play-unbound.png"):
                Path(src, name).write_bytes(b"x")
            proc = self.run_module(f"$pub = Publish-AttrCudaVisualCaptures -Record ([ordered]@{{ forwarded = $true }}) -SourceDir '{src}' -PubRoot '{Path(d, 'pub')}'; "
                                   "(Get-ChildItem -LiteralPath $pub -File | ForEach-Object Name | Sort-Object) -join ','")
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stdout.strip().splitlines()[-1],
                             "paused-unbound.png,paused.png,play-0.png,play-unbound-0.png,stop-unbound.png,stop.png,visual-capture-request.json,visual.json")

    def test_the_visual_tail_is_cut_off_the_presentmon_window_and_never_extends_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vis-pm-") as d:
            Path(d, "visual.json").write_text(json.dumps({"tail_begin_utc": "2026-10-13T22:00:00.000Z"}), encoding="utf-8")
            proc = self.run_module(
                f"$dir = '{d}'\n"
                "$rec = [ordered]@{ forwarded = $true; presentMonWindowEndUtc = $null }\n"
                "function Make($end) { [pscustomobject]@{ process = [pscustomobject]@{ id = 7; startedAtUtc = [datetime]::SpecifyKind([datetime]'2026-10-13T21:59:00', 'Utc'); endedAtUtc = $end }; other = 1 } }\n"
                "$utc = Make ([datetime]::SpecifyKind([datetime]'2026-10-13T22:00:10', 'Utc'))\n"
                "$r1 = Get-AttrCudaPresentMonWindowResult -ResultJson $utc -Record $rec -VisualCaptureDir $dir\n"
                "Write-Output ('UTC=' + $r1.process.endedAtUtc.ToString('o') + '|' + $r1.process.endedAtUtc.Kind + '|' + $utc.process.endedAtUtc.ToString('o') + '|' + $rec['presentMonWindowEndUtc'])\n"
                "$loc = Make ([datetime]::SpecifyKind([datetime]'2026-10-13T22:00:10', 'Utc').ToLocalTime())\n"
                "$r2 = Get-AttrCudaPresentMonWindowResult -ResultJson $loc -Record $rec -VisualCaptureDir $dir\n"
                "Write-Output ('LOCAL=' + $r2.process.endedAtUtc.ToUniversalTime().ToString('o') + '|' + $r2.process.endedAtUtc.Kind)\n"
                "$str = Make '2026-10-13T22:00:10.0000000Z'\n"
                "$r3 = Get-AttrCudaPresentMonWindowResult -ResultJson $str -Record $rec -VisualCaptureDir $dir\n"
                "Write-Output ('STRING=' + $r3.process.endedAtUtc)\n"
                "$early = Make ([datetime]::SpecifyKind([datetime]'2026-10-13T21:59:30', 'Utc'))\n"
                "Write-Output ('NEVER_LATER=' + [object]::ReferenceEquals((Get-AttrCudaPresentMonWindowResult -ResultJson $early -Record $rec -VisualCaptureDir $dir), $early))\n"
                "$off = [ordered]@{ forwarded = $false }\n"
                "Write-Output ('OFF=' + [object]::ReferenceEquals((Get-AttrCudaPresentMonWindowResult -ResultJson $utc -Record $off -VisualCaptureDir $dir), $utc))")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = dict(l.split("=", 1) for l in proc.stdout.splitlines() if "=" in l)
        self.assertEqual(out["UTC"], "2026-10-13T22:00:00.0000000Z|Utc|2026-10-13T22:00:10.0000000Z|2026-10-13T22:00:00.000Z", "a copy is cut; the launcher's own result is untouched")
        self.assertEqual(out["LOCAL"], "2026-10-13T22:00:00.0000000Z|Local", "the cut end keeps the launcher's own DateTime kind")
        self.assertEqual(out["STRING"], "2026-10-13T22:00:00.0000000Z")
        self.assertEqual(out["NEVER_LATER"], "True", "a tail that begins after the process ended never moves the window")
        self.assertEqual(out["OFF"], "True", "no forwarded capture: the launcher's result, untouched")


@requires_windows_pwsh
class VisualCaptureRunnerModuleTests(unittest.TestCase):
    """The runner's request, gate and evidence reader (DualVenueRunner.psm1)."""

    def test_visual_capture_is_on_for_every_speed_leg_unless_it_opts_out_with_a_reason(self) -> None:
        cases = {
            "absent": ("@{ legType = 'speed' }", "True|True|"),
            "true": ("@{ legType = 'speed'; visualCapture = $true }", "True|True|"),
            "optout": ("@{ legType = 'speed'; visualCapture = $false; visualCaptureOptOutReason = 'a capture-free pace A/B arm' }", "True|False|a capture-free pace A/B arm"),
            "look": ("@{ legType = 'look' }", "False|False|"),
        }
        for name, (spec, expected) in cases.items():
            with self.subTest(name):
                proc, out = run_pwsh_json(f"$r = Get-DvVisualCaptureRequest -Spec ([pscustomobject]{spec}); ConvertTo-Json @(\"$($r.applies)|$($r.requested)|$($r.optOutReason)\")")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(out[0] if isinstance(out, list) else out, expected)
        for name, spec in (("no-reason", "@{ legType = 'speed'; visualCapture = $false }"), ("reason-only", "@{ legType = 'speed'; visualCaptureOptOutReason = 'why not' }"),
                           ("not-bool", "@{ legType = 'speed'; visualCapture = 'no' }")):
            with self.subTest(name):
                proc = run_pwsh(["-Command", f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force; try {{ [void](Get-DvVisualCaptureRequest -Spec ([pscustomobject]{spec})); 'NO_THROW' }} catch {{ $_.Exception.Message }}"])
                self.assertTrue(proc.stdout.strip().startswith("DVE_LEG_SPEC_INVALID"), proc.stdout + proc.stderr)

    def test_the_visual_gate_freezes_diverges_and_blanks_only_on_its_own_evidence(self) -> None:
        rows = [  # requested, captured, play, distinct, appDistinct, flat, mad, samePosition -> verdict, counted
            ("$true", "$true", 6, 6, 6, 0, "2.0", "$true", "OK", True),
            ("$true", "$true", 6, 1, 1, 0, "2.0", "$true", "OK", True),            # the app itself presented one frame: a stall, not a frozen display
            ("$true", "$true", 6, 1, 6, 6, "$null", "$false", "CAPTURE_BLANK", True),
            ("$true", "$true", 6, 6, 6, 0, "30.0", "$true", "VISUAL_DIVERGENCE", True),
            ("$true", "$true", 6, 6, 6, 0, "30.0", "$false", "OK", True),           # not the same timeline position: the difference judges nothing
            ("$true", "$false", 0, 0, 0, 0, "$null", "$false", "NOT_CAPTURED", True),
            ("$true", "$true", 1, 1, 1, 0, "$null", "$false", "NOT_CAPTURED", True),
            ("$false", "$false", 0, 0, 0, 0, "$null", "$false", "NOT_REQUESTED", True),
        ]
        for req, cap, play, distinct, app, flat, mad, same, verdict, counted in rows:
            with self.subTest(verdict=verdict, distinct=distinct, app=app, mad=mad):
                proc, out = run_pwsh_json(f"Get-DvVisualVerdict -Requested {req} -Captured {cap} -PlayFrames {play} -DistinctContentHashes {distinct} -AppDistinctDisplayFrames {app} "
                                          f"-FlatPlayFrames {flat} -PlayVsPausedMeanAbsDiff {mad} -SamePosition {same} -Threshold 12 | ConvertTo-Json -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual((out["verdict"], out["counted"]), (verdict, counted))

    def test_the_visual_gate_judges_a_single_picture_by_whether_the_source_frames_differ(self) -> None:
        """PLAYBACK-FROZEN-DISPLAY-SOURCE-DIFF-1 (CoS #428 Improvement): one picture across the play frames is a FROZEN display only when the source
        frames are KNOWN to differ. A static clip legitimately shows one picture; an unknown source is suspected, reported and not excluded.
        r2 (CoS #474 I1): the frames that HAVE a viewport are judged first; the gate is unavailable only when fewer than two of them remain."""
        rows = [  # source difference (None = parameter omitted), judged (viewport) play frames, play frames without a viewport, distinct -> verdict, counted
            ("known-differ", 6, 0, 1, "FROZEN_DISPLAY", False),            # known-differ + one picture: unchanged, excluded
            ("known-identical", 6, 0, 1, "OK", True),                      # a static / repeated-picture clip: not frozen
            ("unknown", 6, 0, 1, "FROZEN_DISPLAY_SUSPECTED", True),        # nothing records it: suspected, NOT excluded
            (None, 6, 0, 1, "FROZEN_DISPLAY_SUSPECTED", True),             # the default is unknown, never a guess
            ("known-differ", 6, 0, 6, "OK", True),                         # pictures moved: the source difference does not matter
            ("unknown", 6, 0, 6, "OK", True),
            ("known-differ", 0, 6, 0, "VISUAL_ASSESSMENT_UNAVAILABLE", True),   # no viewport rectangle at all: the whole window is never judged instead
            (None, 1, 5, 1, "VISUAL_ASSESSMENT_UNAVAILABLE", True),             # one usable frame judges nothing
            ("known-differ", 5, 1, 1, "FROZEN_DISPLAY", False),            # CoS I1: the five frames that have a viewport show one picture over a known-differ source
            ("known-differ", 2, 4, 1, "FROZEN_DISPLAY", False),            # two usable frames are enough to judge
            ("unknown", 5, 1, 1, "FROZEN_DISPLAY_SUSPECTED", True),
            ("known-identical", 5, 1, 1, "OK", True),
            ("known-differ", 5, 1, 5, "OK", True),                         # five distinct pictures and one frame without a viewport: judged, nothing wrong
        ]
        for src, play, unavailable, distinct, verdict, counted in rows:
            with self.subTest(source=src, play=play, unavailable=unavailable, distinct=distinct):
                out = self.visual_verdict(src, play, unavailable, distinct)
                self.assertEqual((out["verdict"], out["counted"]), (verdict, counted), out["reason"])
        proc, out = run_pwsh_json("Get-DvVisualVerdict -Requested $true -Captured $true -PlayFrames 6 -DistinctContentHashes 1 -AppDistinctDisplayFrames 6 "
                                  "-FlatPlayFrames 0 -PlayVsPausedMeanAbsDiff 2.0 -SamePosition $true | ConvertTo-Json -Compress")
        self.assertEqual((proc.returncode, out["verdict"]), (0, "FROZEN_DISPLAY_SUSPECTED"), "ViewportUnavailablePlayFrames defaults to 0")

    def visual_verdict(self, src, play, unavailable, distinct, repeats=None, extra=""):
        """One Get-DvVisualVerdict call over `play` judged (viewport) play frames; src None omits -SourceDifference (the default); repeats None omits
        -RepeatedPlayFrames (the default, 0)."""
        src_arg = f"-SourceDifference {src} " if src else ""
        repeats_arg = f"-RepeatedPlayFrames {repeats} " if repeats is not None else ""
        proc, out = run_pwsh_json(f"Get-DvVisualVerdict -Requested $true -Captured $true -PlayFrames {play} -DistinctContentHashes {distinct} -AppDistinctDisplayFrames 6 "
                                  f"-FlatPlayFrames 0 -PlayVsPausedMeanAbsDiff 2.0 -SamePosition $true {src_arg}{repeats_arg}{extra}-ViewportUnavailablePlayFrames {unavailable} | ConvertTo-Json -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return out

    def test_the_visual_gate_says_why_a_known_identical_single_picture_is_ok(self) -> None:
        """CoS #474 I3: the frozen check is waived on the app's own word, so the receipt carries that word (a wrong `identical` from a future build shows)."""
        out = self.visual_verdict("known-identical", 6, 0, 1)
        self.assertEqual((out["verdict"], out["counted"]), ("OK", True))
        self.assertIn("the app records the source frames as identical", out["reason"])
        self.assertIn("static clip", out["reason"])
        self.assertIsNone(self.visual_verdict("known-identical", 6, 0, 6)["reason"], "six distinct pictures waive nothing: no reason")
        self.assertIsNone(self.visual_verdict("known-differ", 6, 0, 6)["reason"])

    # Prevention (CoS #474): when a pure verdict function gains a verdict, add rows that pair it with EVERY verdict that sets counted = false, and say which wins.
    # (source difference, judged play frames, play frames without a viewport, distinct[, repeated play frames], winner); each pair must show BOTH of its
    # verdicts winning somewhere.
    NEW_VISUAL_VERDICT_PAIRINGS = {
        ("FROZEN_DISPLAY_SUSPECTED", "FROZEN_DISPLAY"): [("unknown", 6, 0, 1, "FROZEN_DISPLAY_SUSPECTED"), ("known-differ", 6, 0, 1, "FROZEN_DISPLAY")],
        ("VISUAL_ASSESSMENT_UNAVAILABLE", "FROZEN_DISPLAY"): [("known-differ", 5, 1, 1, "FROZEN_DISPLAY"), ("known-differ", 1, 5, 1, "VISUAL_ASSESSMENT_UNAVAILABLE"),
                                                              ("known-differ", 0, 6, 0, "VISUAL_ASSESSMENT_UNAVAILABLE")],
        # r5: one picture while the label advances is the whole-tail freeze (every judged frame after the first repeats); more than one picture with two or
        # more repeats is the partial one
        ("PARTIAL_FREEZE", "FROZEN_DISPLAY"): [("known-differ", 6, 0, 1, 5, "FROZEN_DISPLAY"), ("known-differ", 6, 0, 3, 3, "PARTIAL_FREEZE"),
                                               ("known-differ", 5, 1, 2, 2, "PARTIAL_FREEZE")],
    }

    def test_every_new_visual_verdict_is_paired_with_every_excluding_verdict(self) -> None:
        text = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        body = text[text.index("function Get-DvVisualVerdict {"):text.index("function New-DvVisualStrip {")]
        excluding = set(re.findall(r"verdict = '([A-Z_]+)'; counted = \$false", body))
        self.assertTrue(excluding, "the verdict function names no counted = $false verdict (the pairing check lost its subject)")
        for new in ("FROZEN_DISPLAY_SUSPECTED", "VISUAL_ASSESSMENT_UNAVAILABLE", "PARTIAL_FREEZE"):
            for ex in sorted(excluding):
                with self.subTest(new=new, excluding=ex):
                    rows = self.NEW_VISUAL_VERDICT_PAIRINGS.get((new, ex))
                    self.assertTrue(rows, f"add rows pairing {new} with the excluding verdict {ex} to NEW_VISUAL_VERDICT_PAIRINGS")
                    self.assertEqual({r[-1] for r in rows}, {new, ex}, "both verdicts of the pair must win somewhere")
                    for *args, winner in rows:
                        out = self.visual_verdict(*args)
                        self.assertEqual(out["verdict"], winner, f"{args}: {out['reason']}")
        self.assertEqual({k[1] for k in self.NEW_VISUAL_VERDICT_PAIRINGS}, excluding, "a pairing names a verdict that no longer excludes")

    def test_the_visual_gate_says_partial_freeze_for_repeats_over_source_frames_known_to_differ(self) -> None:
        """PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r5 (hub ruling, ACK 6388020454 on #456): two or more repeated play frames while the label advanced, over
        source frames KNOWN to differ, are PARTIAL_FREEZE (counted, never OK). One repeat stays OK; over an unknown or identical source the repeats stay a
        note (the r4 wording) and the verdict does not change."""
        rows = [  # source difference, distinct, repeats -> verdict
            ("known-differ", 4, 2, "PARTIAL_FREEZE"), ("known-differ", 3, 3, "PARTIAL_FREEZE"), ("known-differ", 2, 4, "PARTIAL_FREEZE"),
            ("known-differ", 5, 1, "OK"), ("known-differ", 6, 0, "OK"), ("known-differ", 6, None, "OK"),
            ("unknown", 3, 3, "OK"), (None, 3, 3, "OK"), ("known-identical", 3, 3, "OK"),
            ("known-differ", 1, 5, "FROZEN_DISPLAY"), ("unknown", 1, 5, "FROZEN_DISPLAY_SUSPECTED"),
        ]
        for src, distinct, repeats, verdict in rows:
            with self.subTest(source=src, distinct=distinct, repeats=repeats):
                out = self.visual_verdict(src, 6, 0, distinct, repeats)
                self.assertEqual((out["verdict"], out["counted"]), (verdict, verdict != "FROZEN_DISPLAY"), out["reason"])
                if verdict == "PARTIAL_FREEZE":
                    self.assertIn(f"{repeats} of the 6 judged play frames repeat the picture before them", out["reason"])
        # a partial freeze is judged on the play frames, so it outranks the paused-capture verdicts, as the frozen ones do
        for extra in ("-LabelBound $false -PausedLabelBound $true ", "-Threshold 1 "):
            with self.subTest(extra=extra):
                self.assertEqual(self.visual_verdict("known-differ", 6, 0, 3, 3, extra)["verdict"], "PARTIAL_FREEZE")
                self.assertIn(self.visual_verdict("known-differ", 6, 0, 3, 1, extra)["verdict"], ("CAPTURE_UNBOUND", "VISUAL_DIVERGENCE"))

    def test_the_default_divergence_threshold_is_six_not_twelve(self) -> None:
        # The gate table above passes -Threshold explicitly, so it cannot see the module default. These rows pass none: a same-position
        # mean absolute difference of 8 is a divergence at the calibrated default (6.0) and would read OK if the default were 12.0.
        for mad, verdict in (("8.0", "VISUAL_DIVERGENCE"), ("6.5", "VISUAL_DIVERGENCE"), ("6.0", "OK"), ("5.9", "OK"), ("2.0", "OK")):
            with self.subTest(mad=mad):
                proc, out = run_pwsh_json("Get-DvVisualVerdict -Requested $true -Captured $true -PlayFrames 6 -DistinctContentHashes 6 -AppDistinctDisplayFrames 6 "
                                          f"-FlatPlayFrames 0 -PlayVsPausedMeanAbsDiff {mad} -SamePosition $true | ConvertTo-Json -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual((out["verdict"], out["counted"]), (verdict, True))
                if verdict == "VISUAL_DIVERGENCE":
                    self.assertIn("threshold is 6", out["reason"])

    def test_the_visual_reader_hashes_the_viewport_not_the_chrome_and_composes_one_strip(self) -> None:
        for mode, verdict in (("ok", "OK"), ("frozen", "FROZEN_DISPLAY"), ("blank", "CAPTURE_BLANK"), ("diverge", "VISUAL_DIVERGENCE")):
            with self.subTest(mode), tempfile.TemporaryDirectory(prefix=f"vis-{mode}-") as d:
                art = Path(d) / "artifacts"
                write_visual_captures(art / "visual", mode)
                ev = Path(d) / "evidence"
                proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{art}' -EvidenceDir '{ev}' -LegId 'unit-leg' -Header 'unit' | ConvertTo-Json -Depth 8 -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(out["verdict"], verdict, out["reason"])
                self.assertEqual(out["playFrames"], 6)
                self.assertEqual(out["appDistinctDisplayFrames"], 6)
                self.assertEqual(out["distinctContentHashes"], {"ok": 6, "frozen": 1, "blank": 1, "diverge": 6}[mode],
                                 "the content hash is the VIEWPORT's: a frozen picture under a ticking chrome hashes ONE value")
                self.assertTrue(out["samePosition"])
                names = sorted(f["file"] for f in out["frames"])
                self.assertEqual(names, sorted([f"unit-leg-play-{k}.png" for k in range(6)] + ["unit-leg-stop.png", "unit-leg-paused.png"]))
                for f in out["frames"]:
                    local = ev / "visual" / f["file"]
                    self.assertTrue(local.is_file(), f["file"])
                    self.assertEqual(f["sha256"], hashlib.sha256(local.read_bytes()).hexdigest())
                self.assertTrue(Path(out["stripPath"]).is_file())
                self.assertEqual(Path(out["stripPath"]).name, "unit-leg-strip.png")
                self.assertEqual(out["stripSha256"], hashlib.sha256(Path(out["stripPath"]).read_bytes()).hexdigest())
                self.assertEqual(out["presentMonWindowEndUtc"], "2026-10-13T22:00:00.0000000Z")
                self.assertEqual((out["tailBeginUtc"], out["tailEndUtc"]), ("2026-10-13T22:00:00.0000000Z", "2026-10-13T22:00:03.1000000Z"))
                mad = out["playVsPausedMeanAbsDiff"]
                if mode == "diverge":
                    self.assertGreater(mad, 12.0)
                    self.assertGreater(out["playVsPausedMeanAbsDiffPerChannel"][0], out["playVsPausedMeanAbsDiffPerChannel"][1], "the red shift shows in R")
                else:
                    self.assertLess(mad, 1.0)
                # PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1: a pre-fix visual.json (no binding fields) reads exactly as before
                self.assertIsNone(out["labelBound"])
                self.assertIsNone(out["stopUnboundVsPausedMeanAbsDiff"])
                self.assertIsNone(out["stopUnboundVsStopMeanAbsDiff"])

    def read_visual_mode(self, mode: str, d: str) -> tuple[Path, dict]:
        art = Path(d) / "artifacts"
        write_visual_captures(art / "visual", mode)
        ev = Path(d) / "evidence"
        proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{art}' -EvidenceDir '{ev}' -LegId 'unit-leg' -Header 'unit' | ConvertTo-Json -Depth 8 -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return ev, out

    def test_the_visual_reader_says_capture_unbound_when_the_stop_was_not_painted_for_its_label(self) -> None:
        # G1 (PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1): the stop capture shows an older picture than its label names, and the app says so
        # (painted_serial != serial, label_bound false). The stop-vs-paused difference then compares two moments: never a divergence verdict.
        with tempfile.TemporaryDirectory(prefix="vis-lagged-") as d:
            _, out = self.read_visual_mode("lagged", d)
        self.assertEqual((out["verdict"], out["counted"]), ("CAPTURE_UNBOUND", True), out["reason"])
        self.assertIs(out["labelBound"], False)
        self.assertGreater(out["playVsPausedMeanAbsDiff"], 12.0, "the difference is still reported")
        self.assertIn("label", out["reason"])

    def test_the_visual_reader_keeps_the_unbound_stop_beside_the_bound_one(self) -> None:
        # G2: the app keeps the unforced (master-equivalent) stop capture as stop-unbound.png, then the forced, bound stop.png.
        with tempfile.TemporaryDirectory(prefix="vis-bound-") as d:
            ev, out = self.read_visual_mode("bound", d)
            self.assertEqual(out["verdict"], "OK", out["reason"])
            self.assertIs(out["labelBound"], True)
            self.assertLess(out["playVsPausedMeanAbsDiff"], 1.0)
            self.assertGreater(out["stopUnboundVsPausedMeanAbsDiff"], 12.0)
            self.assertGreater(out["stopUnboundVsStopMeanAbsDiff"], 12.0)
            files = {f["file"]: f for f in out["frames"]}
            self.assertIn("unit-leg-stop-unbound.png", files)
            local = ev / "visual" / "unit-leg-stop-unbound.png"
            self.assertEqual(files["unit-leg-stop-unbound.png"]["sha256"], hashlib.sha256(local.read_bytes()).hexdigest())
            self.assertEqual((files["unit-leg-stop.png"]["paintedSerial"], files["unit-leg-stop.png"]["serial"]), (912, 912))
            self.assertTrue(Path(out["stripPath"]).is_file())
            self.assertIs(out["pausedLabelBound"], True)

    def test_the_visual_gate_says_capture_unbound_before_divergence(self) -> None:
        rows = [  # labelBound (stop), pausedLabelBound, mad -> verdict
            ("$false", "$true", "30.0", "CAPTURE_UNBOUND"), ("$false", "$true", "2.0", "CAPTURE_UNBOUND"),
            ("$true", "$true", "30.0", "VISUAL_DIVERGENCE"), ("$true", "$true", "2.0", "OK"),
            ("$null", "$null", "30.0", "VISUAL_DIVERGENCE"), ("$null", "$null", "2.0", "OK"),
            # r2 (bound paused capture): the divergence compares two captures, so BOTH must be bound; a binding build missing one side fails closed
            ("$true", "$false", "30.0", "CAPTURE_UNBOUND"), ("$true", "$false", "2.0", "CAPTURE_UNBOUND"),
            ("$true", "$null", "30.0", "CAPTURE_UNBOUND"), ("$true", "$null", "2.0", "CAPTURE_UNBOUND"),
            ("$null", "$true", "30.0", "CAPTURE_UNBOUND"), ("$false", "$false", "2.0", "CAPTURE_UNBOUND"),
        ]
        for bound, paused_bound, mad, verdict in rows:
            with self.subTest(bound=bound, paused=paused_bound, mad=mad):
                proc, out = run_pwsh_json("Get-DvVisualVerdict -Requested $true -Captured $true -PlayFrames 6 -DistinctContentHashes 6 -AppDistinctDisplayFrames 6 "
                                          f"-FlatPlayFrames 0 -PlayVsPausedMeanAbsDiff {mad} -SamePosition $true -LabelBound {bound} -PausedLabelBound {paused_bound} | ConvertTo-Json -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual((out["verdict"], out["counted"]), (verdict, True))
        # (PLAYBACK-FROZEN-DISPLAY-SOURCE-DIFF-1: FROZEN_DISPLAY needs source frames known to differ; over an unknown source it is SUSPECTED, which outranks too)
        for source, frozen in (("known-differ", "FROZEN_DISPLAY"), ("unknown", "FROZEN_DISPLAY_SUSPECTED")):
            with self.subTest(source=source):
                proc, out = run_pwsh_json("Get-DvVisualVerdict -Requested $true -Captured $true -PlayFrames 6 -DistinctContentHashes 1 -AppDistinctDisplayFrames 6 "
                                          f"-FlatPlayFrames 0 -PlayVsPausedMeanAbsDiff 2.0 -SamePosition $true -SourceDifference {source} -LabelBound $false | ConvertTo-Json -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(out["verdict"], frozen, "a frozen display outranks an unbound stop capture")

    def test_the_visual_reader_requires_the_paused_capture_bound_before_a_divergence_verdict(self) -> None:
        # PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 (Dot 6d0e710f improvement; DualVenueRunner.psm1 Read-DvVisualCaptures): the stop-vs-paused difference is only
        # a verdict when BOTH of its captures are bound to their labels. r1 required it of the stop capture alone.
        for mode, paused_bound in (("bound-paused-unbound", False), ("bound-paused-missing", None)):
            with self.subTest(mode), tempfile.TemporaryDirectory(prefix=f"vis-{mode}-") as d:
                _, out = self.read_visual_mode(mode, d)
                self.assertEqual((out["verdict"], out["counted"]), ("CAPTURE_UNBOUND", True), out["reason"])
                self.assertIs(out["labelBound"], True)
                self.assertIs(out["pausedLabelBound"], paused_bound)
                self.assertIn("paused re-render capture", out["reason"])
        with tempfile.TemporaryDirectory(prefix="vis-bound-both-") as d:
            _, out = self.read_visual_mode("bound", d)
            self.assertEqual(out["verdict"], "OK", out["reason"])
            self.assertEqual((out["labelBound"], out["pausedLabelBound"]), (True, True))
            # label_bound_basis (Dot improvement: the pixmap route's binding holds by construction) is carried, never dropped
            self.assertEqual((out["labelBoundBasis"], out["pausedLabelBoundBasis"]), ("pixmap_item_by_construction", "pixmap_item_by_construction"))
            files = {f["file"]: f for f in out["frames"]}
            self.assertEqual(files["unit-leg-stop-unbound.png"]["labelBoundBasis"], "gl_viewport_paint")

    def test_the_visual_reader_judges_a_frozen_display_on_the_unforced_play_captures(self) -> None:
        # PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 (Dot 6d0e710f B2, CoS 6219217683): the forced play captures paint the frame the label names first, so a display
        # whose own deferred paint never lands still showed six moving pictures. The app now pairs each with an unforced capture; the gate judges those.
        with tempfile.TemporaryDirectory(prefix="vis-masked-") as d:
            ev, out = self.read_visual_mode("masked", d)
            self.assertEqual((out["verdict"], out["counted"]), ("FROZEN_DISPLAY", False), out["reason"])
            self.assertEqual(out["frozenJudgedOn"], "play_unbound")
            self.assertEqual((out["playFrames"], out["distinctContentHashes"], out["forcedPlayDistinctContentHashes"]), (6, 1, 6))
            self.assertEqual(out["repeatedPlayFrames"], 5, "the whole-tail FROZEN_DISPLAY outranks the repeats it implies")
            self.assertIn("the force-paint would have masked this", out["reason"])
            names = sorted(f["file"] for f in out["frames"])
            self.assertEqual(names, sorted([f"unit-leg-play-{k}.png" for k in range(6)] + [f"unit-leg-play-unbound-{k}.png" for k in range(6)]
                                           + ["unit-leg-stop.png", "unit-leg-paused.png"]))
            for f in out["frames"]:
                self.assertEqual(f["sha256"], hashlib.sha256((ev / "visual" / f["file"]).read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory(prefix="vis-paired-") as d:
            # r5 (Grok G1/G3): the r3 order takes unforced k half a tail after forced k, so a healthy display shows them DIFFERENT pictures; the r2
            # index pairing (playForcedUnforcedDifferFrames) read that as six disagreements and is no longer reported
            ev, out = self.read_visual_mode("paired-ok", d)
            self.assertEqual((out["verdict"], out["reason"]), ("OK", None))
            self.assertEqual((out["frozenJudgedOn"], out["distinctContentHashes"], out["forcedPlayDistinctContentHashes"], out["repeatedPlayFrames"]),
                             ("play_unbound", 6, 6, 0))
            frames = {(f["kind"], f["index"]): f for f in out["frames"]}
            self.assertTrue(all(frames[("play", k)]["contentHash"] != frames[("play_unbound", k)]["contentHash"] for k in range(6)),
                            "the fixture models the r3 order: no unforced capture is a same-instant twin of a forced one")
            self.assertNotIn("playForcedUnforcedDifferFrames", out)
            self.assertTrue(Path(out["stripPath"]).is_file())
        with tempfile.TemporaryDirectory(prefix="vis-old-") as d:
            _, out = self.read_visual_mode("ok", d)
            self.assertEqual((out["frozenJudgedOn"], out["forcedPlayDistinctContentHashes"]), ("play", None),
                             "a build without unforced play captures is judged on its play captures, as before")
            self.assertNotIn("playForcedUnforcedDifferFrames", out)

    def test_the_visual_reader_takes_the_source_difference_from_the_apps_record_and_never_guesses(self) -> None:
        """PLAYBACK-FROZEN-DISPLAY-SOURCE-DIFF-1: visual.json's source_difference (differ | identical) is the only record of whether the source frames differ;
        absent or anything else is unknown (case counts: `DIFFER` / `IDENTICAL` are not the app's words). A missing viewport rectangle is an unavailable
        assessment, not a judgement of the whole window; the frames that DO have one are still judged (CoS #474 I1). The receipt counts the saved play
        frames (`savedPlayFrames`, `captured`) apart from the judged ones (`playFrames`) (CoS #474 N2)."""
        # mode, verdict, counted, source, judged play frames, without a viewport
        for mode, verdict, counted, source, judged, no_viewport in (("frozen", "FROZEN_DISPLAY", False, "known-differ", 6, 0),
                                                                    ("frozen-identical", "OK", True, "known-identical", 6, 0),
                                                                    ("frozen-unknown", "FROZEN_DISPLAY_SUSPECTED", True, "unknown", 6, 0),
                                                                    ("frozen-upper-differ", "FROZEN_DISPLAY_SUSPECTED", True, "unknown", 6, 0),
                                                                    ("frozen-upper-identical", "FROZEN_DISPLAY_SUSPECTED", True, "unknown", 6, 0),
                                                                    ("frozen-partial", "FROZEN_DISPLAY", False, "known-differ", 5, 1),
                                                                    ("noviewport", "VISUAL_ASSESSMENT_UNAVAILABLE", True, "unknown", 0, 6)):
            with self.subTest(mode), tempfile.TemporaryDirectory(prefix=f"vis-{mode}-") as d:
                art = Path(d) / "artifacts"
                write_visual_captures(art / "visual", mode)
                ev = Path(d) / "evidence"
                proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{art}' -EvidenceDir '{ev}' -LegId 'unit-leg' -Header 'unit' | ConvertTo-Json -Depth 8 -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual((out["verdict"], out["counted"]), (verdict, counted), out["reason"])
                self.assertEqual(out["sourceDifference"], source)
                self.assertEqual(out["distinctContentHashes"], 0 if judged == 0 else 1, "no viewport: no whole-window hash stands in for the picture" if judged == 0 else "")
                self.assertEqual(out["viewportUnavailablePlayFrames"], no_viewport)
                self.assertEqual((out["captured"], out["savedPlayFrames"], out["playFrames"]), (True, 6, judged), "6 PNGs were saved; `playFrames` counts the judged ones")
                if mode == "frozen-identical":
                    self.assertIn("the app records the source frames as identical", out["reason"])
                if mode == "frozen-partial":
                    # CoS #474 N4: the verdict reason and the reader's not-judged note are both kept, verdict reason first
                    frozen_at = out["reason"].find("show ONE picture")
                    note_at = out["reason"].find("1 saved play frame(s) have no usable viewport rectangle and were not judged")
                    self.assertGreaterEqual(frozen_at, 0, out["reason"])
                    self.assertGreater(note_at, frozen_at, out["reason"])

    def test_a_strip_that_cannot_be_composed_keeps_every_other_reason(self) -> None:
        """CoS #474 N5: the strip failure used to be overwritten by any verdict reason, and on a plain OK it replaced the not-judged note. The reasons are
        composed `<verdict reason>; <not-judged note>; <strip failure>`: none is dropped. The strip is made to fail by a directory sitting where its PNG goes."""
        strip_failure = "the strip could not be composed"
        not_judged = "1 saved play frame(s) have no usable viewport rectangle and were not judged"
        # mode, verdict, texts that must all appear, in this order
        for mode, verdict, texts in (("frozen", "FROZEN_DISPLAY", ["show ONE picture", strip_failure]),
                                     ("frozen-partial", "FROZEN_DISPLAY", ["show ONE picture", not_judged, strip_failure]),
                                     ("partial-ok", "OK", [not_judged, strip_failure]),
                                     ("ok", "OK", [strip_failure])):
            with self.subTest(mode), tempfile.TemporaryDirectory(prefix=f"vis-strip-{mode}-") as d:
                art = Path(d) / "artifacts"
                write_visual_captures(art / "visual", mode)
                ev = Path(d) / "evidence"
                (ev / "visual" / "unit-leg-strip.png").mkdir(parents=True)
                proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{art}' -EvidenceDir '{ev}' -LegId 'unit-leg' -Header 'unit' | ConvertTo-Json -Depth 8 -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(out["verdict"], verdict, out["reason"])
                self.assertIsNone(out["stripPath"])
                at = [out["reason"].find(text) for text in texts]
                self.assertTrue(all(i >= 0 for i in at), out["reason"])
                self.assertEqual(at, sorted(at), out["reason"])

    def test_a_not_captured_leg_whose_strip_also_fails_keeps_both_reasons(self) -> None:
        """PLAYBACK-VISUAL-STRIP-REASON-NOT-CAPTURED-1 (the named follow-up of #503): no play frame was saved AND the strip cannot be composed. The NOT_CAPTURED
        leg used to keep only the strip failure, dropping `only N play frame(s) were captured`. Both survive, capture-count first. The strip is made to fail by a
        directory sitting where its PNG goes; the control leg (strip fine) keeps the capture-count reason alone."""
        capture_count = "only 0 play frame(s) were captured"
        strip_failure = "the strip could not be composed"
        for mode, texts in (("strip-fails", [capture_count, strip_failure]), ("strip-composes", [capture_count])):
            with self.subTest(mode), tempfile.TemporaryDirectory(prefix=f"vis-nc-{mode}-") as d:
                art = Path(d) / "artifacts"
                write_visual_captures(art / "visual", "ok")
                visual_json = art / "visual" / "visual.json"
                visual = json.loads(visual_json.read_text(encoding="utf-8"))
                for frame in visual["frames"]:
                    if frame["kind"] == "play":
                        frame["saved"] = False
                visual_json.write_text(json.dumps(visual), encoding="utf-8")
                ev = Path(d) / "evidence"
                if mode == "strip-fails":
                    (ev / "visual" / "unit-leg-strip.png").mkdir(parents=True)
                proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{art}' -EvidenceDir '{ev}' -LegId 'unit-leg' -Header 'unit' | ConvertTo-Json -Depth 8 -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual((out["verdict"], out["counted"], out["savedPlayFrames"]), ("NOT_CAPTURED", True, 0), out["reason"])
                if mode == "strip-fails":
                    self.assertIsNone(out["stripPath"])
                    at = [out["reason"].find(text) for text in texts]
                    self.assertTrue(all(i >= 0 for i in at), out["reason"])
                    self.assertEqual(at, sorted(at), out["reason"])
                else:
                    self.assertTrue(Path(out["stripPath"]).is_file())
                    self.assertEqual(out["reason"], capture_count)

    def test_a_not_captured_leg_with_a_prior_step_reason_keeps_all_three_reasons_in_order(self) -> None:
        """DV-VISUAL-STRIP-PRIOR-REASON-FIXTURE-1 (Dot advisory 3a8b205c on #600): the NOT_CAPTURED reason is the join of THREE parts, `<verdict reason>; <the step
        reason standing when the strip failed>; <strip failure>`, and the test above only runs with an empty middle part, so dropping or reordering `$reasonBeforeStrip`
        stayed green. Here a malformed visual-capture-request.json sets the step reason BEFORE the strip fails (no play frame saved, strip PNG path blocked by a
        directory): all three survive, in that order, each exactly once."""
        capture_count = "only 0 play frame(s) were captured"
        prior_step = "the job's visual-capture request record is not valid JSON"
        strip_failure = "the strip could not be composed"
        with tempfile.TemporaryDirectory(prefix="vis-nc-prior-") as d:
            art = Path(d) / "artifacts"
            write_visual_captures(art / "visual", "ok")
            (art / "visual" / "visual-capture-request.json").write_text("{ this is not json", encoding="utf-8")
            visual_json = art / "visual" / "visual.json"
            visual = json.loads(visual_json.read_text(encoding="utf-8"))
            for frame in visual["frames"]:
                if frame["kind"] == "play":
                    frame["saved"] = False
            visual_json.write_text(json.dumps(visual), encoding="utf-8")
            ev = Path(d) / "evidence"
            (ev / "visual" / "unit-leg-strip.png").mkdir(parents=True)
            proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{art}' -EvidenceDir '{ev}' -LegId 'unit-leg' -Header 'unit' | ConvertTo-Json -Depth 8 -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((out["verdict"], out["counted"], out["savedPlayFrames"]), ("NOT_CAPTURED", True, 0), out["reason"])
        self.assertIsNone(out["stripPath"])
        self.assertEqual([out["reason"].count(text) for text in (capture_count, prior_step, strip_failure)], [1, 1, 1], out["reason"])
        at = [out["reason"].find(text) for text in (capture_count, prior_step, strip_failure)]
        self.assertEqual(at, sorted(at), out["reason"])
        self.assertEqual(out["reason"].split("; ")[1], prior_step, "the prior step reason sits between the verdict reason and the strip failure")

    def test_a_strip_that_composes_leaves_the_reason_as_it_was(self) -> None:
        """CoS #474 N5 (no change when the strip is fine): no strip text is added, and the partial-ok note stays the whole reason."""
        for mode, reason in (("ok", None), ("partial-ok", "1 saved play frame(s) have no usable viewport rectangle and were not judged; the other 5 were")):
            with self.subTest(mode), tempfile.TemporaryDirectory(prefix=f"vis-strip-ok-{mode}-") as d:
                art = Path(d) / "artifacts"
                write_visual_captures(art / "visual", mode)
                ev = Path(d) / "evidence"
                proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{art}' -EvidenceDir '{ev}' -LegId 'unit-leg' -Header 'unit' | ConvertTo-Json -Depth 8 -Compress")
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual((out["verdict"], out["reason"]), ("OK", reason))
                self.assertTrue(Path(out["stripPath"]).is_file())

    def test_a_visual_reader_without_a_published_directory_says_not_captured(self) -> None:
        with tempfile.TemporaryDirectory(prefix="vis-none-") as d:
            proc, out = run_pwsh_json(f"Read-DvVisualCaptures -Requested $true -ArtifactsDir '{d}' -EvidenceDir '{d}' -LegId 'unit-leg' | ConvertTo-Json -Depth 8 -Compress")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual((out["verdict"], out["captured"], out["counted"]), ("NOT_CAPTURED", False, True))
        self.assertIn("no visual directory", out["reason"])


@requires_windows_pwsh
class VisualCaptureRunnerTests(RunnerHarness, unittest.TestCase):
    """End to end through Invoke-VenueLeg.ps1 (offline harness): the request reaches the generator, the captures reach the receipt, the gate decides."""

    def setUp(self) -> None:
        self.make_harness()

    def speed_spec(self, **fields) -> Path:
        path = self.write_spec()
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec.update(fields)
        out = path.with_name(path.stem + "-" + hashlib.sha1(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:8] + ".json")
        out.write_text(json.dumps(spec), encoding="utf-8")
        return out

    def run_visual(self, mode: str | None, spec: Path | None = None):
        self.write_artifacts()
        if mode is None:
            shutil.rmtree(self.artifacts / "visual", ignore_errors=True)
        else:
            write_visual_captures(self.artifacts / "visual", mode)
        return self.run_leg("ultra-magnus", spec or self.write_spec(), extra=["-Backend", "cpu"])

    def test_a_speed_leg_asks_for_visual_capture_and_keeps_its_strip(self) -> None:
        proc, receipt, _ = self.run_visual("ok")
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertIn("VisualCapture=True", self.generator_calls()[-1])
        visual = receipt["visual"]
        self.assertEqual((visual["verdict"], visual["requested"], visual["captured"], visual["distinctContentHashes"]), ("OK", True, True, 6))
        self.assertEqual(Path(visual["stripPath"]).parent, Path(receipt["evidence"]["localEvidenceDir"]) / "visual")
        copy = Path(visual["stripCopyPath"])
        self.assertTrue(copy.is_file())
        self.assertEqual(copy.parent, self.tmp / "dv" / "sheets", "without -SheetCopyDir the strip lands in <receipts>\\..\\sheets")
        self.assertTrue(copy.name.startswith("strip-unit-leg-ultra-magnus-cpu-"), copy.name)
        self.assertIn(f"DVE_VISUAL=OK STRIP={copy}", proc.stdout)

    def test_a_frozen_display_leg_is_not_counted_whatever_its_cadence(self) -> None:
        _, receipt, _ = self.run_visual("frozen")
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("FROZEN_DISPLAY", receipt["outcomeDetail"])
        self.assertFalse(receipt["visual"]["counted"])

    def test_a_partial_freeze_leg_is_counted_and_surfaced_never_read_as_ok(self) -> None:
        """PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r5 (hub ruling, ACK 6388020454): the measured run's PARTIAL_FREEZE is surfaced in the outcome detail like the
        other counted non-OK verdicts, and a self-heal pre-launch with one makes the leg INVALID (that gate needs OK)."""
        _, receipt, _ = self.run_visual("partial-freeze")
        self.assertEqual((receipt["visual"]["verdict"], receipt["visual"]["counted"], receipt["visual"]["repeatedPlayFrames"]), ("PARTIAL_FREEZE", True, 3))
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertIn("PARTIAL_FREEZE (counted for speed, surfaced for review)", receipt["outcomeDetail"])
        _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "partial-freeze"})
        self.assertEqual(receipt["selfHealVisual"]["pre-1"]["verdict"], "PARTIAL_FREEZE")
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("PARTIAL_FREEZE on self-heal pre-launch pre-1", receipt["outcomeDetail"])

    def run_self_heal_visual(self, measured: str, launches: dict, declared: int | None = None, **spec_fields):
        """launches: pre-<n> -> a write_visual_captures mode, or None for a pre-launch that published its self-heal\\pre-<n> but no visual\\.
        declared: the spec's generatorArgs.gpuPreLaunches count (the pre-launches the leg runs); None leaves the spec without them."""
        self.write_artifacts()
        write_visual_captures(self.artifacts / "visual", measured)
        shutil.rmtree(self.artifacts / "self-heal", ignore_errors=True)
        for name, mode in launches.items():
            if mode is None:
                (self.artifacts / "self-heal" / name).mkdir(parents=True)
                (self.artifacts / "self-heal" / name / "pre-launch.json").write_text("{}", encoding="utf-8")
            else:
                write_visual_captures(self.artifacts / "self-heal" / name / "visual", mode)
        if declared is not None:
            spec_fields["generatorArgs"] = {"automationSettingsDir": "gpu-self-heal-settings", "gpuPreLaunches": (["702@120", "702@120", "none"] * 2)[:declared]}
        spec = self.speed_spec(**spec_fields) if spec_fields else self.write_spec()
        return self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"])

    def test_a_frozen_self_heal_pre_launch_makes_the_leg_invalid_whatever_the_measured_run_shows(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r6 (CoS #437, Dot 46f5e496): r5 recorded each pre-launch's verdict in receipt.selfHealVisual but gated only
        receipt.visual, so a frozen fallback run sat beside a PASS. Every pre-launch verdict is now folded into the outcome."""
        _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "ok", "pre-2": "frozen", "pre-3": "ok"})
        self.assertEqual(receipt["visual"]["verdict"], "OK", "the measured run's own display moved")
        self.assertEqual([receipt["selfHealVisual"][k]["verdict"] for k in ("pre-1", "pre-2", "pre-3")], ["OK", "FROZEN_DISPLAY", "OK"])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("FROZEN_DISPLAY on self-heal pre-launch pre-2", receipt["outcomeDetail"])
        self.assertNotIn("pre-1:", receipt["outcomeDetail"])

    def test_a_self_heal_leg_whose_every_pre_launch_shows_an_ok_picture_passes(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r7: the control for the pins below -- three declared pre-launches, each OK, beside an OK measured run."""
        _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "ok", "pre-2": "ok", "pre-3": "ok"}, declared=3)
        self.assertEqual([receipt["selfHealVisual"][k]["verdict"] for k in ("pre-1", "pre-2", "pre-3")], ["OK", "OK", "OK"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertNotIn("self-heal pre-launch", receipt["outcomeDetail"])

    def test_a_diverging_self_heal_pre_launch_makes_the_leg_invalid(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r7 (sol r6 blocker; the 08:05:36Z ledger scope 'fail the leg on any non-OK pre-n visual verdict'): r6 counted a
        VISUAL_DIVERGENCE pre-launch and asserted PASS here. The fallback runs are the leg's whole proof, so a picture that diverges is not counted."""
        _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "diverge", "pre-2": "ok"})
        self.assertEqual(receipt["visual"]["verdict"], "OK", "the measured run's own picture is fine")
        self.assertEqual(receipt["selfHealVisual"]["pre-1"]["verdict"], "VISUAL_DIVERGENCE")
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("VISUAL_DIVERGENCE on self-heal pre-launch pre-1", receipt["outcomeDetail"])
        self.assertNotIn("pre-2", receipt["outcomeDetail"])

    def test_a_self_heal_pre_launch_with_no_captures_makes_the_leg_invalid(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r7: pre-2 published its self-heal\\pre-2 but no visual\\ (captured=false): NOT_CAPTURED is no picture of the fallback."""
        _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "ok", "pre-2": None, "pre-3": "ok"}, declared=3)
        self.assertEqual((receipt["selfHealVisual"]["pre-2"]["verdict"], receipt["selfHealVisual"]["pre-2"]["captured"]), ("NOT_CAPTURED", False))
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("NOT_CAPTURED on self-heal pre-launch pre-2", receipt["outcomeDetail"])
        self.assertNotIn("pre-1", receipt["outcomeDetail"])

    def test_a_blank_self_heal_pre_launch_capture_makes_the_leg_invalid(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r7: six flat frames (CAPTURE_BLANK) judge nothing about the fallback run, so the leg is not counted."""
        _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "ok", "pre-2": "ok", "pre-3": "blank"}, declared=3)
        self.assertEqual(receipt["selfHealVisual"]["pre-3"]["verdict"], "CAPTURE_BLANK")
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("CAPTURE_BLANK on self-heal pre-launch pre-3", receipt["outcomeDetail"])

    def test_a_declared_self_heal_pre_launch_with_no_visual_entry_makes_the_leg_invalid(self) -> None:
        """GPU-DEVICE-LOSS-SELF-HEAL-1 r7: the spec declares three pre-launches (generatorArgs.gpuPreLaunches) but self-heal\\pre-2 never arrived, so the
        receipt holds no selfHealVisual entry for it; and a leg that opted out of visual capture holds none for any of them (NOT_REQUESTED)."""
        _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "ok", "pre-3": "ok"}, declared=3)
        self.assertNotIn("pre-2", receipt["selfHealVisual"])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("NO_VISUAL_ENTRY on self-heal pre-launch pre-2", receipt["outcomeDetail"])
        self.assertNotIn("pre-1", receipt["outcomeDetail"])
        _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "ok", "pre-2": "ok", "pre-3": "ok"}, declared=3, visualCapture=False,
                                                  visualCaptureOptOutReason="a capture-free pace A/B arm")
        self.assertEqual(receipt["visual"]["verdict"], "NOT_REQUESTED")
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        for name in ("pre-1", "pre-2", "pre-3"):
            self.assertIn(f"NO_VISUAL_ENTRY on self-heal pre-launch {name}", receipt["outcomeDetail"])

    def test_a_visual_divergence_is_counted_for_speed_and_surfaced(self) -> None:
        _, receipt, _ = self.run_visual("diverge")
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertIn("VISUAL_DIVERGENCE", receipt["outcomeDetail"])
        self.assertEqual(receipt["visual"]["verdict"], "VISUAL_DIVERGENCE")

    def test_an_unbound_visual_capture_is_counted_for_speed_and_surfaced(self) -> None:
        _, receipt, _ = self.run_visual("lagged")
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertIn("CAPTURE_UNBOUND", receipt["outcomeDetail"])
        self.assertEqual((receipt["visual"]["verdict"], receipt["visual"]["labelBound"]), ("CAPTURE_UNBOUND", False))

    def test_a_frozen_display_the_force_paint_would_have_masked_is_not_counted(self) -> None:
        # PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 (Dot 6d0e710f B2): judged on the unforced play captures, end to end through the runner
        _, receipt, _ = self.run_visual("masked")
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("FROZEN_DISPLAY", receipt["outcomeDetail"])
        self.assertIn("would have masked", receipt["outcomeDetail"])
        self.assertEqual((receipt["visual"]["frozenJudgedOn"], receipt["visual"]["counted"]), ("play_unbound", False))

    def test_a_look_assist_on_leg_that_ran_without_it_is_invalid(self) -> None:
        # PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 (D4b): a speed leg whose spec forces Look Assist on fails closed unless the app's own visual_state says
        # look_assist_enabled=1; 0 or a missing record is a typed INVALID. A leg that does not ask is unchanged.
        spec = self.speed_spec(generatorArgs={"forceLookAssist": True})
        for state, outcome in (("look_assist_enabled=1", "PASS"), ("look_assist_enabled=0", "INVALID"), ("", "INVALID"), (None, "INVALID")):
            with self.subTest(state=state):
                self.write_artifacts(look_assist_state=state)
                write_visual_captures(self.artifacts / "visual", "ok")
                _, receipt, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"])
                self.assertEqual(receipt["outcome"], outcome, receipt["outcomeDetail"])
                self.assertEqual(receipt["lookAssist"]["requested"], True)
                if outcome == "INVALID":
                    self.assertIn("LOOK_ASSIST_NOT_HONOURED", receipt["outcomeDetail"])
                else:
                    self.assertEqual((receipt["lookAssist"]["reported"], receipt["lookAssist"]["verdict"]), ("1", "HONOURED"))
        self.write_artifacts(look_assist_state="look_assist_enabled=0")
        write_visual_captures(self.artifacts / "visual", "ok")
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertNotIn("lookAssist", receipt, "a leg that does not ask for Look Assist records nothing about it")

    def test_a_single_picture_over_an_unknown_or_identical_source_is_counted_and_surfaced(self) -> None:
        """PLAYBACK-FROZEN-DISPLAY-SOURCE-DIFF-1: only a source KNOWN to differ voids the leg. Unknown is surfaced in the detail (counted); identical is OK;
        an unavailable viewport is surfaced too and never judges the whole window."""
        _, receipt, _ = self.run_visual("frozen-unknown")
        self.assertEqual((receipt["outcome"], receipt["visual"]["verdict"], receipt["visual"]["counted"]), ("PASS", "FROZEN_DISPLAY_SUSPECTED", True), receipt["outcomeDetail"])
        self.assertIn("FROZEN_DISPLAY_SUSPECTED", receipt["outcomeDetail"])
        _, receipt, _ = self.run_visual("frozen-identical")
        self.assertEqual((receipt["outcome"], receipt["visual"]["verdict"]), ("PASS", "OK"), receipt["outcomeDetail"])
        _, receipt, _ = self.run_visual("noviewport")
        self.assertEqual((receipt["outcome"], receipt["visual"]["verdict"], receipt["visual"]["counted"]), ("PASS", "VISUAL_ASSESSMENT_UNAVAILABLE", True), receipt["outcomeDetail"])
        self.assertIn("VISUAL_ASSESSMENT_UNAVAILABLE", receipt["outcomeDetail"])

    def test_a_suspected_or_unassessable_self_heal_pre_launch_is_not_ok_and_voids_the_leg(self) -> None:
        """The r7 self-heal gate is 'anything but OK is INVALID' and stays so: the two new verdicts are not OK, so a fallback run with either is not counted."""
        for mode, verdict in (("frozen-unknown", "FROZEN_DISPLAY_SUSPECTED"), ("noviewport", "VISUAL_ASSESSMENT_UNAVAILABLE")):
            with self.subTest(mode):
                _, receipt, _ = self.run_self_heal_visual("ok", {"pre-1": "ok", "pre-2": mode}, declared=2)
                self.assertEqual(receipt["selfHealVisual"]["pre-2"]["verdict"], verdict)
                self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                self.assertIn(f"{verdict} on self-heal pre-launch pre-2", receipt["outcomeDetail"])

    def test_a_blank_visual_capture_judges_nothing(self) -> None:
        _, receipt, _ = self.run_visual("blank")
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["visual"]["verdict"], "CAPTURE_BLANK")

    def test_a_speed_leg_whose_job_published_no_visual_capture_says_so(self) -> None:
        _, receipt, _ = self.run_visual(None)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["visual"]["verdict"], "NOT_CAPTURED")

    def test_an_opted_out_speed_leg_asks_for_no_visual_capture(self) -> None:
        spec = self.speed_spec(visualCapture=False, visualCaptureOptOutReason="a capture-free pace A/B arm")
        _, receipt, _ = self.run_visual("ok", spec)
        self.assertNotIn("VisualCapture", self.generator_calls()[-1])
        self.assertEqual((receipt["visual"]["verdict"], receipt["visual"]["requested"], receipt["visual"]["optOutReason"]),
                         ("NOT_REQUESTED", False, "a capture-free pace A/B arm"))

    def test_a_look_leg_never_runs_the_visual_capture(self) -> None:
        self.write_artifacts(sheet=True)
        write_visual_captures(self.artifacts / "visual", "frozen")
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(leg_type="look"), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertNotIn("VisualCapture", self.generator_calls()[-1])
        self.assertIsNone(receipt["visual"])

    def test_a_refused_speed_leg_still_says_visual_capture_was_asked_for(self) -> None:
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), gen_refusal="PLAYBACK_ATTR3_OWNER_NOT_CONSENTED stub")
        self.assertEqual(receipt["outcome"], "DEVICE_UNAVAILABLE")
        self.assertEqual((receipt["visual"]["requested"], receipt["visual"]["verdict"]), (True, "NOT_CAPTURED"))


SELF_HEAL_ROUTE_FAILURE = ("gpu_texture_route_readback_regression: 528 frame(s) did not reach gpu_texture_no_readback (cpu_frames=528) while the GPU "
                           "recon texture-present no-readback route was requested in GL-window mode; every frame must reach gpu_texture_no_readback.")


# r10 (CoS #437, Dot c503450a): L3 gpu_summary counters as a run log could carry them unreadable. On a16845f1 [long] read each of these
# (an absent key, an empty value, '0x0') as 0, so L3's zero-CUDA and zero-preview criteria passed without measuring anything.
UNREADABLE_L3_COUNTERS = {
    "counters-absent-l3": "",
    "counters-null-l3": "gpu_preview_frames= gpu_recon_readback_frames= gpu_texture_readback_frames= gpu_texture_no_readback_frames=",
    "counters-string-l3": "gpu_preview_frames=0x0 gpu_recon_readback_frames=0x0 gpu_texture_readback_frames=0x0 gpu_texture_no_readback_frames=0x0",
    "counters-word-l3": "gpu_preview_frames=n/a gpu_recon_readback_frames=n/a gpu_texture_readback_frames=n/a gpu_texture_no_readback_frames=n/a",
}


# GPU-SELFHEAL-TEST-GATE-NITS-1 (CoS #437): the L3 fault row's cuda_code as a field log could carry it unreadable. Only a JSON integer inside Int32 is a fault code;
# on 4e258a92 the Int64 2147483648 reached `[int]$code`, which THROWS, so the metrics never existed instead of naming FAULT_CODE_NOT_NUMERIC.
UNREADABLE_L3_FAULT_CODES = {
    "fault-code-string-l3": "cudaErrorLaunchTimeout",
    "fault-code-numeric-string-l3": "702",
    "fault-code-high-l3": 2147483648,
    "fault-code-low-l3": -2147483649,
    "fault-code-float-l3": 702.5,
    # ATTR-CUDA-FAULT-CODE-RANGE-FIXTURES-1 (Dot 30637950 on #659, ACK 6224003932): a present-but-empty value is not "no fault" (0), and a JSON
    # boolean is neither [int] nor [long], so neither may be cast into a fault code.
    "fault-code-empty-l3": "",
    "fault-code-whitespace-l3": " ",
    "fault-code-boolean-l3": True,
}

# The same extremes one step inside the range: a fault code, so L3's eq 0 reports the value it read, not a COULD-NOT-CHECK.
IN_RANGE_L3_FAULT_CODES = {"fault-code-int32-max-l3": 2147483647, "fault-code-int32-min-l3": -2147483648, "fault-code-negative-l3": -1}


def write_self_heal_evidence(root: Path, case: str = "good") -> None:
    """<artifacts>\\self-heal as the r5 job published it on Bachelor (d3ed879b), reduced to the lines the metrics read: L1 and L2 inject 702 (L2's
    second loss sets the flag), L3 starts on the CPU, the measured run L4 clears the flag. `case` breaks exactly one fact."""
    if root.exists():
        shutil.rmtree(root)
    launches = {1: ("702@120", 58, 528, 22.597), 2: ("702@120", 58, 528, 22.4), 3: ("none", 0, 595, 23.289)}
    for n, (inject, cuda, cpu, fps) in launches.items():
        d = root / f"pre-{n}"
        if case == "skipped-l2" and n == 2:
            continue
        d.mkdir(parents=True)
        record = {"schema": "mlv-app/gpu-self-heal-pre-launch/v1", "index": n, "faultInject": inject, "startedUtc": "2026-10-15T08:26:35Z",
                  "finishedUtc": "2026-10-15T08:27:58Z", "rc": 1, "error": None}
        failures = [SELF_HEAL_ROUTE_FAILURE]
        if case == "underivable-l1" and n == 1:
            record.update(rc=None, error="PRE_LAUNCH_COMMAND_UNDERIVABLE")
        if case == "smoke-failed-l1" and n == 1:
            failures.append("Playback did not advance past frame 0.")
        (d / "pre-launch.json").write_text(json.dumps(record), encoding="utf-8")
        (d / "result.json").write_text(json.dumps({"schema": "mlvapp-gui-smoke-result.v2", "validation": {"failures": failures}}), encoding="utf-8")
        preview = 12 if case == "gpu-preview-l3" and n == 3 else 0
        refused = 397 if case == "refused-recon-l3" and n == 3 else 0
        if case == "slow-l1" and n == 1:
            fps = 10.6
        fps_field = f"presented_fps={fps} "
        if n == 3 and case == "fps-word-l3":
            fps_field = "presented_fps=n/a "
        if n == 3 and case == "fps-absent-l3":
            fps_field = ""
        counters = (f"gpu_preview_frames={preview} gpu_recon_readback_frames=0 gpu_texture_readback_frames=0 "
                    f"gpu_texture_no_readback_frames={cuda}")
        if n == 3 and case in UNREADABLE_L3_COUNTERS:
            counters = UNREADABLE_L3_COUNTERS[case]
        if case == "counters-null-l3" and n == 3:
            refused = ""
        (d / "result.json.run.log").write_text("\n".join([
            "[2026-10-15T08:27:10.400Z] [INFO] [0xd900] playback_smoke.measured_session id=1",
            f"[2026-10-15T08:27:36.324Z] [INFO] [0xd900] playback_smoke.summary session=1 reason=visual-capture presented_frames=586 {fps_field}last_presented_frame=597",
            f"[2026-10-15T08:27:36.326Z] [INFO] [0xd900] playback_smoke.gpu_summary session=1 cpu_frames={cpu} {counters}",
            f"[2026-10-15T08:27:36.336Z] [INFO] [0xd900] playback_smoke.debayered16_recon_reuse_summary session=1 scope=presented_frames refused_acquisition_failed=0 "
            f"refused_gpu_playback_recon={refused} reduced_consumed_frames={cpu}", ""]), encoding="utf-8")
        rows = []
        if inject != "none" and not (case == "no-fault-l2" and n == 2):
            rows.append({"schema": "gpu-field-telemetry.v1", "kind": "fault", "dll": "inject", "stage": "recon:run", "rc": -1, "cuda_code": 702,
                         "cuda_name": "cudaErrorLaunchTimeout", "site": "MLVAPP_GPU_FAULT_INJECT", "fault_utc_ms": 1792052833759.0 + n, "device_loss": True, "role": "primary"})
            rows.append({"schema": "gpu-field-telemetry.v1", "kind": "recovery", "what": "device_loss", "outcome": "cuda_suppressed_session"})
        if case == "fault-code-absent-l3" and n == 3:
            rows.append({"schema": "gpu-field-telemetry.v1", "kind": "fault", "dll": "inject", "stage": "recon:run", "rc": -1,
                         "site": "MLVAPP_GPU_FAULT_INJECT", "fault_utc_ms": 1792052833762.0, "device_loss": True, "role": "primary"})
        if n == 3 and case in {**UNREADABLE_L3_FAULT_CODES, **IN_RANGE_L3_FAULT_CODES}:
            rows.append({"schema": "gpu-field-telemetry.v1", "kind": "fault", "dll": "inject", "stage": "recon:run", "rc": -1,
                         "cuda_code": {**UNREADABLE_L3_FAULT_CODES, **IN_RANGE_L3_FAULT_CODES}[case],
                         "site": "MLVAPP_GPU_FAULT_INJECT", "fault_utc_ms": 1792052833762.0, "device_loss": True, "role": "primary"})
        if n == 2:
            rows.append({"schema": "gpu-field-telemetry.v1", "kind": "recovery", "what": "safe_start_flag", "outcome": "set"})
        if n == 3:
            rows.append({"schema": "gpu-field-telemetry.v1", "kind": "recovery", "what": "safe_start", "outcome": "cpu_route"})
        (d / "gpu-field-telemetry.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        flag = n >= 2 and not (case == "no-flag-l2" and n == 2)
        (d / "MLVApp.ini").write_text("[GpuDeviceLoss]\nRecentLosses=\"1792052833768|gpu;610.74;1808x2268x14c\"\n" + ("SafeStartEnabled=true\n" if flag else ""), encoding="utf-8")
    main = root / "main"
    main.mkdir(parents=True)
    (main / "gpu-field-telemetry.jsonl").write_text(json.dumps({"schema": "gpu-field-telemetry.v1", "kind": "recovery", "what": "safe_start_flag",
                                                                "outcome": "cleared_env"}) + "\n", encoding="utf-8")
    (main / "MLVApp.ini").write_text("[GpuDeviceLoss]\nRecentLosses=\"1792052833768|gpu;610.74;1808x2268x14c\"\n"
                                     + ("SafeStartEnabled=true\n" if case == "flag-kept-l4" else ""), encoding="utf-8")
    (main / "settings-dir.json").write_text(json.dumps({"schema": "mlv-app/gpu-self-heal-settings-dir/v1", "treeRemoved": True, "filesRemoved": 1, "left": 0,
                                                        "stillExists": False}), encoding="utf-8")


@requires_windows_pwsh
class SelfHealLegGatesTheL1ToL4FactsTests(unittest.TestCase):
    """GPU-DEVICE-LOSS-SELF-HEAL-1 r6 (CoS #437): the tracked self-heal leg's criteria, evaluated by the runner's own Test-DvCriteria on the metrics the job
    writes (Get-AttrCudaSelfHealMetrics, round-tripped through summary.json and Get-DvVerbatimMetrics as a receipt reads them). r1-r5 judged only the
    measured run's counters, so a failed, skipped or underivable pre-launch, a breaker that never tripped or a slow fallback still passed."""

    CASES = {  # case -> the criteria it must fail (good: none)
        "good": [],
        "l4-counters-only": ["selfHealPreLaunches", "selfHealL1PreLaunchOk", "selfHealL1BreakerTrip", "selfHealL2SafeStartFlag", "selfHealL3SafeStartRoute",
                             "selfHealL4FlagCleared"],
        "skipped-l2": ["selfHealPreLaunches", "selfHealL2PreLaunchOk", "selfHealL2InjectedFaultCode", "selfHealL2FpsRatio"],
        "underivable-l1": ["selfHealL1PreLaunchOk"],
        "smoke-failed-l1": ["selfHealL1PreLaunchOk"],
        "no-fault-l2": ["selfHealL2InjectedFaultCode", "selfHealL2BreakerTrip"],
        "no-flag-l2": ["selfHealL2SafeStartFlag"],
        "gpu-preview-l3": ["selfHealL3GpuPreviewFrames"],
        "refused-recon-l3": ["selfHealL3RefusedGpuPlaybackRecon"],
        "slow-l1": ["selfHealL1FpsRatio"],
        "flag-kept-l4": ["selfHealL4FlagCleared"],
    }

    # r10: an unreadable L3 counter or fault code -> (the criteria it must fail, the typed reason token their verdict names). The first three PASSED
    # on a16845f1; there counters-word-l3 ([long]'n/a') and fault-code-absent-l3 (StrictMode on $j.cuda_code) threw and the metrics never existed.
    COULD_NOT_CHECK_CASES = {
        "counters-absent-l3": (["selfHealL3CudaFrames", "selfHealL3GpuPreviewFrames"], "COUNTER_ABSENT"),
        "counters-null-l3": (["selfHealL3CudaFrames", "selfHealL3GpuPreviewFrames", "selfHealL3RefusedGpuPlaybackRecon"], "COUNTER_NULL"),
        "counters-string-l3": (["selfHealL3CudaFrames", "selfHealL3GpuPreviewFrames"], "COUNTER_NOT_NUMERIC"),
        "counters-word-l3": (["selfHealL3CudaFrames", "selfHealL3GpuPreviewFrames"], "COUNTER_NOT_NUMERIC"),
        "fault-code-absent-l3": (["selfHealL3InjectedFaultCode"], "FAULT_CODE_ABSENT"),
        **{case: (["selfHealL3InjectedFaultCode"], "FAULT_CODE_NOT_NUMERIC") for case in UNREADABLE_L3_FAULT_CODES},
        # an unreadable launch presented_fps leaves the ratio null; it reuses the counter reader, so its tokens are the counter tokens
        "fps-word-l3": (["selfHealL3FpsRatio"], "COUNTER_NOT_NUMERIC"),
        "fps-absent-l3": (["selfHealL3FpsRatio"], "COUNTER_ABSENT"),
    }

    def evaluate(self, cases, measured_fps: str = "23.201") -> dict:
        with tempfile.TemporaryDirectory(prefix="sh-gate-") as d:
            measured = Path(d) / "measured.log"
            measured.write_text("playback_smoke.measured_session id=1\nplayback_smoke.summary session=1 reason=visual-capture presented_frames=599 "
                                f"presented_fps={measured_fps}\n", encoding="utf-8")
            for case in cases:
                if case != "l4-counters-only":
                    write_self_heal_evidence(Path(d) / case / "self-heal", "good" if case == "good" else case)
            script = (f"$ErrorActionPreference = 'Stop'; Import-Module '{ATTR_MODULE}' -Force; Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n"
                      f"$spec = [IO.File]::ReadAllText('{DV / SELF_HEAL_LEG}') | ConvertFrom-Json\n"
                      f"$log = [IO.File]::ReadAllText('{measured}')\n"
                      "$out = [ordered]@{}\n"
                      f"foreach ($case in @({', '.join(repr(c) for c in cases)})) {{\n"
                      "  $summary = [ordered]@{ result = 'MEASUREMENT_CAPTURED'; rows = 600; gpuFramesTotal = 599; cpuFrames = 0 }\n"
                      "  if ($case -cne 'l4-counters-only') {\n"
                      f"    $m = Get-AttrCudaSelfHealMetrics -SelfHealDir (Join-Path (Join-Path '{d}' $case) 'self-heal') -PreLaunches @('702@120', '702@120', 'none') -MeasuredLogText $log -MeasuredSessionId ''\n"
                      "    foreach ($k in $m.Keys) { $summary[$k] = $m[$k] }\n"
                      "  }\n"
                      "  $metrics = Get-DvVerbatimMetrics -Summary (($summary | ConvertTo-Json -Depth 4) | ConvertFrom-Json) -EvidenceManifest $null\n"
                      "  $v = Test-DvCriteria -Criteria $spec.criteria.acceptance.cuda -Metrics $metrics\n"
                      "  $s = Test-DvCriteria -Criteria $spec.criteria.supplementary.cuda -Metrics $metrics\n"
                      "  $out[$case] = [ordered]@{ pass = $v.pass; supplementaryPass = $s.pass; failures = @($v.failures); metrics = $metrics }\n"
                      "}\n"
                      "$out | ConvertTo-Json -Depth 6 -Compress")
            proc = run_pwsh(["-Command", script])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def test_the_self_heal_leg_passes_only_when_every_l1_to_l4_fact_holds(self) -> None:
        out = self.evaluate(self.CASES)
        good = out["good"]
        self.assertTrue(good["pass"] and good["supplementaryPass"], good["failures"])
        self.assertEqual((good["metrics"]["selfHealL1Rc"], good["metrics"]["selfHealL1PreLaunchOk"], good["metrics"]["selfHealL3CudaFrames"],
                          good["metrics"]["selfHealL1FpsRatio"]), (1, 1, 0, 0.974))
        for case, expected in self.CASES.items():
            if case == "good":
                continue
            with self.subTest(case):
                got = out[case]
                self.assertFalse(got["pass"], case)
                self.assertFalse(got["supplementaryPass"], case)
                failed = sorted({f.split(" ")[0].rstrip(":") for f in got["failures"]})
                for metric in expected:
                    self.assertIn(metric, failed, f"{case}: {got['failures']}")
                if case not in ("l4-counters-only", "skipped-l2"):
                    self.assertEqual(sorted(expected), failed, f"{case} breaks exactly its own fact: {got['failures']}")

    def test_an_unreadable_counter_is_could_not_check_never_zero(self) -> None:
        """r10 (CoS #437, Dot c503450a): an L3 gpu_summary whose CUDA and preview counters are absent, empty or not decimal counts, a reuse summary
        whose refused count is empty, and a fault row with no cuda_code each FAIL exactly their criteria with a typed COULD-NOT-CHECK reason in the
        verdict -- never a 0 that passes. A present-and-zero counter still passes (good) and a present-and-nonzero one still fails (gpu-preview-l3)."""
        out = self.evaluate(["good", "gpu-preview-l3", *self.COULD_NOT_CHECK_CASES])
        self.assertTrue(out["good"]["pass"], out["good"]["failures"])
        self.assertEqual(out["good"]["metrics"]["selfHealL3GpuPreviewFrames"], 0)
        self.assertEqual({k: v for k, v in out["good"]["metrics"].items() if k.endswith("CouldNotCheck") and v is not None}, {})
        self.assertEqual(out["gpu-preview-l3"]["failures"], ["selfHealL3GpuPreviewFrames eq 0 failed (actual 12)"])
        for case, (expected, token) in self.COULD_NOT_CHECK_CASES.items():
            with self.subTest(case):
                got = out[case]
                self.assertFalse(got["pass"], case)
                self.assertFalse(got["supplementaryPass"], case)
                self.assertEqual(sorted({f.split(" ")[0].rstrip(":") for f in got["failures"]}), sorted(expected), got["failures"])
                for failure in got["failures"]:
                    self.assertIn(f": COULD-NOT-CHECK {token} ", failure, case)
                for metric in expected:
                    self.assertIsNone(got["metrics"][metric], f"{case}: {metric} is never a default")

    def test_an_in_range_fault_code_is_read_as_the_value_never_could_not_check(self) -> None:
        """GPU-SELFHEAL-TEST-GATE-NITS-1: the range check refuses only what an Int32 cannot hold. Int32.MaxValue, Int32.MinValue and -1 are fault codes
        -- L3's eq 0 fails on the value it read ('actual N'), with no COULD-NOT-CHECK reason and a non-null metric."""
        out = self.evaluate(list(IN_RANGE_L3_FAULT_CODES))
        for case, code in IN_RANGE_L3_FAULT_CODES.items():
            with self.subTest(case):
                got = out[case]
                self.assertEqual(got["failures"], [f"selfHealL3InjectedFaultCode eq 0 failed (actual {code})"])
                self.assertEqual(got["metrics"]["selfHealL3InjectedFaultCode"], code)
                self.assertIsNone(got["metrics"]["selfHealL3InjectedFaultCodeCouldNotCheck"])

    def test_an_unusable_measured_fps_is_could_not_check_for_every_launch_ratio(self) -> None:
        """GPU-SELFHEAL-TEST-GATE-NITS-1: the measured run's presented_fps is the denominator of L1-L3's ratio. A zero or non-decimal one leaves all three
        ratios null with MEASURED_FPS_UNUSABLE (never a divide-by-zero or a default), while the launches' own PresentedFps stay readable."""
        for measured_fps, detail in (("0", "summary.presented_fps=0"), ("n/a", "COUNTER_NOT_NUMERIC summary.presented_fps='n/a'")):
            with self.subTest(measured_fps=measured_fps):
                got = self.evaluate(["good"], measured_fps=measured_fps)["good"]
                self.assertFalse(got["pass"])
                self.assertEqual(sorted({f.split(" ")[0].rstrip(":") for f in got["failures"]}), [f"selfHealL{n}FpsRatio" for n in (1, 2, 3)], got["failures"])
                for failure in got["failures"]:
                    self.assertIn(f": COULD-NOT-CHECK MEASURED_FPS_UNUSABLE {detail}", failure)
                for n in (1, 2, 3):
                    self.assertIsNone(got["metrics"][f"selfHealL{n}FpsRatio"])
                    self.assertIsNotNone(got["metrics"][f"selfHealL{n}PresentedFps"])


class VisualCaptureSpecAndAppSourceTests(unittest.TestCase):
    """The schema's opt-out shape, the proof leg, and the app side of the capture (a source census: the app is built and run on a venue, never here)."""

    def test_the_visual_capture_opt_out_needs_its_reason_and_the_reason_needs_the_opt_out(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is required")
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        leg = json.loads((DV / "legs" / "m16-1243-speed-windowed-s2.json").read_text(encoding="utf-8"))
        jsonschema.validate(leg, schema)
        self.assertNotIn("visualCapture", leg, "the proof leg runs the default: absent means on")
        self.assertEqual((leg["legType"], leg["displayMode"], leg["scaleFactor"], leg["clipId"]), ("speed", "windowed", 2, OWNER_CLIP))
        jsonschema.validate(dict(leg, visualCapture=False, visualCaptureOptOutReason="a capture-free pace A/B arm"), schema)
        jsonschema.validate(dict(leg, visualCapture=True), schema)
        for bad in (dict(leg, visualCapture=False), dict(leg, visualCaptureOptOutReason="a capture-free pace A/B arm"),
                    dict(leg, visualCapture=True, visualCaptureOptOutReason="a capture-free pace A/B arm"), dict(leg, visualCapture=False, visualCaptureOptOutReason="short")):
            with self.subTest(bad=sorted(set(bad) - set(leg))), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(bad, schema)

    def test_the_visual_speed_twins_differ_from_the_proof_leg_only_in_display_mode_and_scale(self) -> None:
        proof = json.loads((DV / "legs" / "m16-1243-speed-windowed-s2.json").read_text(encoding="utf-8"))
        self.assertEqual(len(VISUAL_TWIN_LEGS), 5)
        for rel in VISUAL_TWIN_LEGS:
            with self.subTest(rel):
                twin = json.loads((DV / rel).read_text(encoding="utf-8"))
                mode, scale = re.fullmatch(r"legs/m16-1243-speed-(fullscreen|windowed)-s(\d)\.json", rel).groups()
                self.assertEqual((twin["legId"], twin["displayMode"], twin["scaleFactor"], twin["legType"]), (Path(rel).stem, mode, int(scale), "speed"))
                self.assertNotIn("visualCapture", twin, "absent means on")
                keep = ("legId", "card", "displayMode", "scaleFactor")
                self.assertEqual({k: v for k, v in twin.items() if k not in keep}, {k: v for k, v in proof.items() if k not in keep})

    def test_the_app_visual_capture_runs_after_the_measured_session_closes_and_captures_the_window(self) -> None:
        main_cpp = (ROOT / "platform" / "qt" / "main.cpp").read_text(encoding="utf-8")
        self.assertIn('QStringLiteral("visual-capture-dir")', main_cpp)
        src = (ROOT / "platform" / "qt" / "MainWindow.cpp").read_text(encoding="utf-8")
        body = src[src.index("int MainWindow::runGuiPlaybackSmoke("):src.index("//Import a MLV, complete procedure")]
        reached = body.index("if( measuredState != playback_frame_range::PlayStopState::Reached )")
        block = body[body.index("if( !options.visualCaptureDir.isEmpty() && ui->actionPlay->isChecked() )"):body.index("// Contact-sheet files. Deliberately placed AFTER")]
        self.assertGreater(body.index("if( !options.visualCaptureDir.isEmpty()"), reached, "the capture only follows a measured Play that REACHED its stop")
        close = block.index('finishPlaybackSmokeTelemetry( "visual-capture" );')
        self.assertLess(close, block.index('grabVisual( QStringLiteral("play"), k, false );'), "the measured session closes before the first capture")
        self.assertLess(block.index('grabVisual( QStringLiteral("play"), k, false );'), block.index("ui->actionPlay->setChecked( false );"))
        self.assertLess(block.index("ui->actionPlay->setChecked( false );"), block.index('grabVisual( QStringLiteral("stop"), 0, false );'))
        self.assertLess(block.index('grabVisual( QStringLiteral("stop"), 0, false );'), block.index('seekAndSettleLoadedClip( visualStopFrame, "gui-smoke-visual-capture-paused"'))
        self.assertLess(block.index("seekAndSettleLoadedClip( visualStopFrame"), block.index('grabVisual( QStringLiteral("paused"), 0, visualSettled );'))
        self.assertLess(block.index('grabVisual( QStringLiteral("paused")'), block.index('.save( visualDir.absoluteFilePath( fileName ), "PNG" )'), "no PNG is encoded while Play runs")
        for forbidden in ("programmaticPlay(", "setChecked( true", "->trigger(", "return "):
            self.assertNotIn(forbidden, block, f"the capture block never starts a Play and never ends the smoke: {forbidden}")
        self.assertIn("PrintWindow( hwnd, memoryDc, PW_RENDERFULLCONTENT )", src)
        # PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-1 (G3): every capture binds its label to its pixels. Inside grabVisual the pending frame is painted
        # FIRST, then the label is read, then the window is printed, with no event processed in between (a paint deferred past the label let a
        # capture show frame N-1 labelled N). The helper lives outside the block, beside the PrintWindow capture.
        helper = "paintPresentedFrameForCapture("
        self.assertTrue("static bool paintPresentedFrameForCapture(" in src, "the binding helper is defined in MainWindow.cpp")
        self.assertLess(abs(src.index("static bool paintPresentedFrameForCapture(") - src.index("static QImage captureTopLevelWindowByPrintWindow(")), 6000,
                        "the binding helper sits next to the PrintWindow capture")
        lam = block[block.index("auto grabVisual = [&]"):block.index("// (1) the measured interval ends HERE")]
        self.assertLess(lam.index(helper), lam.index("m_lastPresentedRequestContext.frameNumber"), "paint first, then read the label")
        self.assertLess(lam.index("m_lastPresentedRequestContext.frameNumber"), lam.index("captureTopLevelWindowByPrintWindow("))
        self.assertNotIn("processEvents(", lam[lam.index(helper):lam.index("captureTopLevelWindowByPrintWindow(")],
                         "nothing may present a newer frame between the paint and the capture")
        # the stop grab keeps the unforced (master-equivalent) capture first, then the forced, bound one
        unbound = 'grabVisual( QStringLiteral("stop_unbound"), 0, false );'
        self.assertLess(block.index("ui->actionPlay->setChecked( false );"), block.index(unbound))
        self.assertLess(block.index(unbound), block.index('grabVisual( QStringLiteral("stop"), 0, false );'))
        self.assertNotIn("processEvents(", block[block.index(unbound):block.index('grabVisual( QStringLiteral("stop"), 0, false );')])
        for field in ('"painted_serial"', '"painted_serial_valid"', '"label_bound"', '"label_bound_basis"'):
            self.assertIn(f"QStringLiteral({field})", block)
        # TIMING-2 r2 (Dot 6d0e710f B2): only the "_unbound" kinds skip the paint -- the frozen check is judged on them, so the force-paint can no longer
        # mask a frozen display. The paused pair keeps its unforced capture just before the forced one, with no event between.
        play_unbound = 'grabVisual( QStringLiteral("play_unbound"), k, false );'
        play = 'grabVisual( QStringLiteral("play"), k, false );'
        paused_unbound = 'grabVisual( QStringLiteral("paused_unbound"), 0, visualSettled );'
        paused = 'grabVisual( QStringLiteral("paused"), 0, visualSettled );'
        self.assertLess(block.index(paused_unbound), block.index(paused))
        self.assertNotIn("processEvents(", block[block.index(paused_unbound):block.index(paused)])
        self.assertIn('!kind.endsWith( QStringLiteral("_unbound") )', lam)
        # TIMING-2 r3 (Dot 7dc103f1 B2): a forced paint changes what the NEXT unforced capture sees when the display's own paint is stuck, so r2's
        # per-slice pairing let unforced k+1 show forced k. Every forced play capture now comes first; the unforced ones follow with no forced capture
        # between any two of them or between the last of them and stop_unbound; every capture records how many forced paints preceded it.
        self.assertEqual((block.count(play), block.count(play_unbound)), (1, 1))
        self.assertLess(block.index(play), block.index(play_unbound))
        between = block[block.index(play_unbound):block.index(unbound)]
        self.assertEqual(between.count("grabVisual("), 1, "no forced capture inside the unforced observation interval")
        self.assertIn("ui->actionPlay->setChecked( false );", between)
        self.assertIn('frameJson.insert( QStringLiteral("forced_paints_before"), grab.forcedPaintsBefore );', block)
        self.assertLess(lam.index("grab.forcedPaintsBefore = visualForcedPaints;"), lam.index(helper), "the ordinal is read before this capture's own paint")
        self.assertIn("if( forcePaint ) ++visualForcedPaints;", lam)
        # where the painted serial came from is named (the pixmap route's binding holds by construction)
        for basis in ('"gl_window_paint"', '"gl_viewport_paint"', '"pixmap_item_by_construction"', '"none"'):
            self.assertIn(f"QStringLiteral({basis})", src)


# ---------------------------------------------------------------------------------------------------
# VENUE-PROBE-PROCESS-ATTRIBUTION-1: every busy quiet probe names WHO (owner, project) and WHAT (script, redacted command line, parent chain, start time, GPU) for its top CPU and top
# GPU pids, collected after the sampling window with at most 3 WQL queries, GetOwner per target and ONE GPU Engine read; the gate prints it as comma-free key=value pairs after the
# byte-identical top prefix; Summarize-VenueBusyProbes.ps1 groups busy probes by project with the gate's own lifted decision function. The real probe job runs here against canned
# Get-Counter / Get-Process / Get-CimInstance / Invoke-CimMethod shadows, so identity is proved on exact values, not on whatever the test host is running.
import base64 as _ident_base64
import datetime as _ident_dt

IDENT_SCHEMA = "mlv-app/venue-quiet-probe-identity/v1"
IDENT_T0 = _ident_dt.datetime(2026, 10, 24, 1, 2, 3, tzinfo=_ident_dt.timezone.utc)
R6_SCRIPT_CHECKS = ("ok", "notfound", "reparse", "nonfixed", "relative", "rejected", "timeout", "error")   # the closed script_check enum (r6)
_IDENT_EPOCH = _ident_dt.datetime(1601, 1, 1, tzinfo=_ident_dt.timezone.utc)

# Get-Counter shadow that also accepts -MaxSamples (the identity block's GPU read passes it); a canned set past the end of the list answers with no samples
GET_COUNTER_PRELUDE_MAXSAMPLES = GET_COUNTER_PRELUDE.replace("param($Counter)", "param($Counter, $MaxSamples)")

GET_CIM_PRELUDE = r"""$script:CimRows = @(ConvertFrom-Json -InputObject @'
__CIMROWS__
'@)
$script:CimOwners = ConvertFrom-Json -InputObject @'
__CIMOWNERS__
'@
$script:CimThrow = __CIMTHROW__
$script:CimDelayMs = __CIMDELAY__
$script:OwnerDelayMs = __OWNERDELAY__
function Get-CimInstance {
    [CmdletBinding()] param($ClassName, $Filter)
    if ($script:CimDelayMs) { [Threading.Thread]::Sleep($script:CimDelayMs) }
    if ($script:CimThrow) { throw 'canned CIM failure' }
    [IO.File]::AppendAllText('__CIMLOG__', "$ClassName|$Filter`n")
    $ids = @([regex]::Matches([string]$Filter, 'ProcessId=(\d+)') | ForEach-Object { [int64]$_.Groups[1].Value })
    foreach ($r in $script:CimRows) {
        if ($ids -contains [int64]$r.ProcessId) {
            [pscustomobject]@{ ProcessId = [uint32]$r.ProcessId; ParentProcessId = [uint32]$r.ParentProcessId; Name = [string]$r.Name
                CreationDate = [datetime]::FromFileTimeUtc([int64]$r.CreationFt).ToLocalTime(); CommandLine = $r.CommandLine; ExecutablePath = $r.ExecutablePath }
        }
    }
}
function Invoke-CimMethod {
    [CmdletBinding()] param($InputObject, $MethodName)
    if ($script:OwnerDelayMs) { [Threading.Thread]::Sleep($script:OwnerDelayMs) }
    [IO.File]::AppendAllText('__OWNERLOG__', "$($InputObject.ProcessId)`n")
    $o = $script:CimOwners.PSObject.Properties["$($InputObject.ProcessId)"]
    if ($null -eq $o) { return [pscustomobject]@{ ReturnValue = [uint32]2; Domain = $null; User = $null } }
    [pscustomobject]@{ ReturnValue = [uint32]$o.Value.rv; Domain = $o.Value.domain; User = $o.Value.user }
}
"""


# The identity workers (VENUE-PROBE-PROCESS-ATTRIBUTION-1 r2) are SEPARATE runspaces, so a test's command shadows are re-declared there through $IdentityWorkerPrelude. The GPU read is
# the only Get-Counter call a worker makes; __GPUDELAY__ is an uninterruptible sleep that stands in for a hung PDH read.
WORKER_COUNTER_PRELUDE = r"""$script:GpuSet = @(ConvertFrom-Json -InputObject @'
__GPUSET__
'@)
$script:GpuDelayMs = __GPUDELAY__
function Get-Counter {
    [CmdletBinding()] param($Counter, $MaxSamples)
    if ($script:GpuDelayMs) { [Threading.Thread]::Sleep($script:GpuDelayMs) }
    [pscustomobject]@{ CounterSamples = @($script:GpuSet | ForEach-Object { [pscustomobject]@{ Path = $_.Path; CookedValue = [double]$_.CookedValue; Status = [uint32]$_.Status } }) }
}
"""


def _ident_ft(dt: "_ident_dt.datetime") -> int:
    return int((dt - _IDENT_EPOCH).total_seconds() * 10_000_000)


def _ident_cim(pid: int, ppid: int, name: str, start, cmd, exe=None) -> dict:
    return {"ProcessId": pid, "ParentProcessId": ppid, "Name": name, "CreationFt": _ident_ft(start), "CommandLine": cmd, "ExecutablePath": exe}


def _ident_gpu_sample(pid: int, eng: int, value: float) -> dict:
    return _cs("gpu engine", f"pid_{pid}_luid_0x0_0x1_phys_0_eng_{eng}_engtype_3d", "utilization percentage", value)


def _ident_probe_body() -> str:
    text = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8")
    return re.search(r"\$probeText = @'\r?\n(.*?)\r?\n'@", text, re.S).group(1)


def _ident_run_reaped(cmd: list, timeout: float, env: dict | None = None) -> subprocess.CompletedProcess:
    """subprocess.run, except that a child still alive at the timeout (or on any error) is killed WITH its process tree (r5: an r4 probe child outlived its test).
    r6: the final drain is bounded too, so a grandchild that still holds the pipes cannot hold the test."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
    except BaseException:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=60)
        try:
            proc.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=60)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


class IdentityProbeMixin:
    """Runs the REAL probe body (12 s sleeps -> 0) against canned process, counter and CIM data."""

    def run_identity_probe(self, procs: list, cim: list, owners: dict | None = None, gpu: list | None = None, throw: bool = False, cpus: int = 4,
                           body: str | None = None, old_counter_shadow: bool = False, after_start_ft: dict | None = None,
                           delays: dict | None = None, call_ms: int | None = None, tel: dict | None = None, env: dict | None = None) -> dict:
        delays = delays or {}
        body = (_ident_probe_body() if body is None else body).replace("[Environment]::ProcessorCount", str(cpus)).replace("Start-Sleep -Seconds 12", "Start-Sleep -Seconds 0")
        if call_ms is not None:
            body = body.replace("$IdentityCallMs = 5000", f"$IdentityCallMs = {call_ms}")
        sets = []
        for _ in range(3):
            seen: dict = {}
            parts = []
            for i, (pid, name, _d, _s) in enumerate(procs):   # PDH names a second instance "name#1", so two processes of one name stay two rows
                n = seen.get(name, 0); seen[name] = n + 1
                parts.append(_proc(name if n == 0 else f"{name}#{n}", pid, 400 - 10 * i))
            sets.append({"s": _flat(*parts, _proc("idle", 0, 100))})
        if gpu is not None:
            sets.append({"s": [_ident_gpu_sample(p, e, v) for (p, e, v) in gpu]})
        before = [{"id": pid, "name": name, "cpu": 10.0, "start": _ident_ft(start)} for (pid, name, _d, start) in procs]
        after = [{"id": pid, "name": name, "cpu": 10.0 + delta, "start": (after_start_ft or {}).get(pid, _ident_ft(start))} for (pid, name, delta, start) in procs]
        self.cim_log = self.tmp / "cim.log"
        self.owner_log = self.tmp / "owner.log"
        for f in (self.cim_log, self.owner_log):
            f.write_text("", encoding="utf-8")
        counter_prelude = (GET_COUNTER_PRELUDE if old_counter_shadow else GET_COUNTER_PRELUDE_MAXSAMPLES).replace("__CANNED__", json.dumps(sets))
        if delays.get("gpu"):   # the decision reads are calls 1-3; the 4th is the GPU read
            counter_prelude = counter_prelude.replace("$set = $script:Canned[$script:CallNo]; $script:CallNo++",
                                                      "if ($script:CallNo -eq 3) { [Threading.Thread]::Sleep(%d) }; $set = $script:Canned[$script:CallNo]; $script:CallNo++" % delays["gpu"])
        cim_prelude = (GET_CIM_PRELUDE.replace("__CIMROWS__", json.dumps(cim)).replace("__CIMOWNERS__", json.dumps({str(k): {"rv": v[0], "domain": v[1], "user": v[2]} for k, v in (owners or {}).items()}))
                       .replace("__CIMTHROW__", "$true" if throw else "$false").replace("__CIMLOG__", str(self.cim_log)).replace("__OWNERLOG__", str(self.owner_log))
                       .replace("__CIMDELAY__", str(delays.get("cim", 0))).replace("__OWNERDELAY__", str(delays.get("owner", 0))))
        prelude = counter_prelude + GET_PROCESS_PRELUDE.replace("__SNAPSHOTS__", json.dumps([before, after])) + cim_prelude
        worker_counter = WORKER_COUNTER_PRELUDE.replace("__GPUSET__", json.dumps(sets[3]["s"] if gpu is not None else [])).replace("__GPUDELAY__", str(delays.get("gpu", 0)))
        if old_counter_shadow:
            worker_counter = worker_counter.replace("param($Counter, $MaxSamples)", "param($Counter)")
        worker_b64 = _ident_base64.b64encode((cim_prelude + worker_counter).encode("utf-8")).decode("ascii")
        body = body.replace("$IdentityWorkerPrelude = ''", f"$IdentityWorkerPrelude = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{worker_b64}'))")
        body = _tel_hermetic(body, self.tmp, tel)   # VENUE-PROBE-HEAT-GPU-TELEMETRY-1: the heat feed, dGPU and agent log are canned too, never the test host's
        job = self.tmp / "identity-probe-job.ps1"
        job.write_text(prelude + body, encoding="utf-8")
        proc = _ident_run_reaped([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job)], timeout=240, env=env)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("VENUE_QUIET=")]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        self.last_line = lines[0][len("VENUE_QUIET="):]   # the line exactly as the gate retains it (Wait-VenueQuiet.ps1 RAW RETENTION)
        return json.loads(self.last_line)

    def cim_queries(self) -> list:
        return [ln for ln in self.cim_log.read_text(encoding="utf-8").splitlines() if ln]

    def owner_calls(self) -> list:
        return [int(ln) for ln in self.owner_log.read_text(encoding="utf-8").splitlines() if ln]

    @staticmethod
    def row(probe: dict, pid: int) -> dict:
        rows = [r for r in probe["identity"]["processes"] if r["pid"] == pid]
        assert len(rows) == 1, (pid, probe["identity"]["processes"])
        return rows[0]


CHAIN_OWNERS = {pid: (0, "BACHELOR", "layib") for pid in (16892, 500, 400, 17000, 501, 401, 17001, 502, 402)}


@requires_windows_pwsh
class VenueProbeIdentityTests(IdentityProbeMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-probe-identity-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def chain(self, cmd: str, pcmd: str = "pwsh.exe -NoProfile", gcmd: str = "explorer.exe"):
        t = IDENT_T0
        procs = [(16892, "python", 29.1, t), (500, "pwsh", 1.0, t - _ident_dt.timedelta(seconds=60)), (400, "explorer", 0.5, t - _ident_dt.timedelta(hours=1))]
        cim = [_ident_cim(16892, 500, "python.exe", t, cmd, r"C:\Python\python.exe"),
               _ident_cim(500, 400, "pwsh.exe", t - _ident_dt.timedelta(seconds=60), pcmd, r"C:\Program Files\PowerShell\7\pwsh.exe"),
               _ident_cim(400, 0, "explorer.exe", t - _ident_dt.timedelta(hours=1), gcmd, r"C:\Windows\explorer.exe")]
        return procs, cim

    def test_the_fixture_process_is_named_by_owner_project_script_parent_chain_and_start(self) -> None:
        procs, cim = self.chain(r"C:\Python\python.exe C:\mlvtmp\lane-X-1\tools\bench.py --api_key=abc123 --token xyz")
        p = self.run_identity_probe(procs, cim, CHAIN_OWNERS, gpu=[(16892, 0, 40.0), (16892, 1, 15.0)])
        ident = p["identity"]
        self.assertEqual(ident["schema"], IDENT_SCHEMA)
        self.assertEqual(p["schema"], "mlv-app/venue-quiet-probe/v2", "the top-level schema stays v2: identity only adds a field")
        r = self.row(p, 16892)
        self.assertEqual((r["owner"], r["owner_status"]), ("BACHELOR\\layib", "ok"))
        # r5 allowlist: the script path is named only when it EXISTS on the probed host, and C:\mlvtmp\lane-X-1\tools\bench.py does not exist here; no argument text is kept
        self.assertEqual((r["project"], r["project_from"], r["script"]), ("mlv-app", "self", None))
        self.assertEqual(r["start_check"], "match")
        self.assertTrue(r["started_utc"].startswith("2026-10-24T01:02:03"), r["started_utc"])
        self.assertEqual((r["cmdline_status"], r["cmdline_redactions"], r["cmdline_truncated_chars"], r["args_redacted"], r["cmdline_args"]), ("ok", 4, 0, "all", 4))
        self.assertEqual(r["cmdline"], r"C:\Python\python.exe args=4 script=<none>")
        for secret in ("abc123", "xyz", "bench.py"):
            self.assertNotIn(secret, self.last_line)
        self.assertEqual((r["parent"]["pid"], r["parent"]["name"], r["parent"]["status"]), (500, "pwsh", "ok"))
        self.assertEqual((r["grandparent"]["pid"], r["grandparent"]["name"], r["grandparent"]["status"]), (400, "explorer", "ok"))
        self.assertEqual(r["source"], ["top", "top_processes", "gpu_top"], "this pid is in all three target sets")
        self.assertEqual((r["gpu_percent"], r["gpu_engtype"], r["gpu_status"]), (40.0, "3d", "ok"))
        for k in ("probe_user", "elevated", "project_roots", "gpu_basis", "gpu_status", "gpu_top", "cost_ms", "notes"):
            self.assertIn(k, ident)
        self.assertEqual(ident["project_roots"], "r1")
        self.assertEqual(set(ident["cost_ms"]), {"cim", "owner", "gpu", "total"})
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in ident["cost_ms"].values()), ident["cost_ms"])
        self.assertTrue(all(isinstance(k, str) and isinstance(v, str) for k, v in ident["notes"].items()))

    def test_the_cim_bound_is_at_most_three_queries_and_getowner_is_for_target_rows_only(self) -> None:
        # three tops whose parents and grandparents are NOT targets: query 1 = targets, query 2 = parents, query 3 = grandparents
        t = IDENT_T0
        procs = [(16892, "python", 29.0, t), (17000, "python", 28.0, t), (17001, "python", 27.0, t)]
        cim = [_ident_cim(16892, 500, "python.exe", t, "python a.py"), _ident_cim(17000, 501, "python.exe", t, "python b.py"), _ident_cim(17001, 502, "python.exe", t, "python c.py"),
               _ident_cim(500, 400, "pwsh.exe", t - _ident_dt.timedelta(seconds=9), "pwsh"), _ident_cim(501, 401, "pwsh.exe", t - _ident_dt.timedelta(seconds=9), "pwsh"),
               _ident_cim(502, 402, "pwsh.exe", t - _ident_dt.timedelta(seconds=9), "pwsh"),
               _ident_cim(400, 0, "explorer.exe", t - _ident_dt.timedelta(hours=1), "explorer"), _ident_cim(401, 0, "explorer.exe", t - _ident_dt.timedelta(hours=1), "explorer"),
               _ident_cim(402, 0, "explorer.exe", t - _ident_dt.timedelta(hours=1), "explorer")]
        p = self.run_identity_probe(procs, cim, CHAIN_OWNERS)
        queries = self.cim_queries()
        self.assertEqual(len(queries), 3, queries)
        self.assertTrue(all(q.startswith("Win32_Process|ProcessId=") for q in queries), "a filtered query, never a full-table enumeration")
        for pid in (16892, 17000, 17001):
            self.assertIn(f"ProcessId={pid}", queries[0])
        for pid in (500, 501, 502):
            self.assertIn(f"ProcessId={pid}", queries[1])
        for pid in (400, 401, 402):
            self.assertIn(f"ProcessId={pid}", queries[2])
        self.assertEqual(sorted(self.owner_calls()), [16892, 17000, 17001], "GetOwner is asked for the TARGET rows only, never for a parent")
        self.assertEqual({r["pid"] for r in p["identity"]["processes"]}, {16892, 17000, 17001})
        self.assertEqual(self.row(p, 17001)["grandparent"]["pid"], 402)

    def test_the_project_comes_from_self_then_parent_then_grandparent_and_otherwise_is_unknown(self) -> None:
        t = IDENT_T0
        old = _ident_dt.timedelta(seconds=9)
        procs = [(16892, "python", 29.0, t), (17000, "python", 28.0, t), (17001, "python", 27.0, t)]
        cim = [_ident_cim(16892, 500, "python.exe", t, r"C:\Python\python.exe C:\Users\x\job.py"), _ident_cim(17000, 501, "python.exe", t, r"C:\Python\python.exe C:\Users\x\job.py"),
               _ident_cim(17001, 502, "python.exe", t, r"C:\Python\python.exe C:\Users\x\job.py"),
               _ident_cim(500, 400, "pwsh.exe", t - old, r"pwsh -File C:\Users\layib\agent-bridge\run.ps1"), _ident_cim(501, 401, "pwsh.exe", t - old, "pwsh -NoProfile"),
               _ident_cim(502, 402, "pwsh.exe", t - old, "pwsh -NoProfile"),
               _ident_cim(400, 0, "explorer.exe", t - old, "explorer.exe"), _ident_cim(401, 0, "explorer.exe", t - old, "explorer.exe"),
               _ident_cim(402, 0, "airmypc.exe", t - old, r"C:\Tools\AirMyPC\airmypc.exe")]
        p = self.run_identity_probe(procs, cim, CHAIN_OWNERS)
        self.assertEqual((self.row(p, 16892)["project"], self.row(p, 16892)["project_from"]), ("agent-bridge", "parent"))
        self.assertEqual((self.row(p, 17000)["project"], self.row(p, 17000)["project_from"]), ("unknown", "none"))
        self.assertEqual((self.row(p, 17001)["project"], self.row(p, 17001)["project_from"]), ("airmypc", "grandparent"))

    def test_an_unreadable_owner_and_command_line_are_typed_never_guessed(self) -> None:
        t = IDENT_T0
        procs = [(16892, "python", 29.0, t)]
        cim = [_ident_cim(16892, 0, "python.exe", t, None, None)]
        p = self.run_identity_probe(procs, cim, {})   # no owner answer: GetOwner returns ReturnValue 2 (access denied)
        r = self.row(p, 16892)
        self.assertEqual((r["owner"], r["owner_status"]), (None, "access-denied"))
        self.assertEqual((r["cmdline"], r["cmdline_status"], r["exe"], r["exe_status"], r["script"]), (None, "unreadable", None, "unreadable", None))
        self.assertEqual((r["project"], r["project_from"]), ("unknown", "none"))
        self.assertEqual(r["parent"]["status"], "none")

    def test_other_getowner_failures_are_typed_by_return_value(self) -> None:
        t = IDENT_T0
        cim = [_ident_cim(16892, 0, "python.exe", t, "python a.py")]
        p = self.run_identity_probe([(16892, "python", 29.0, t)], cim, {16892: (8, None, None)})
        self.assertEqual((self.row(p, 16892)["owner"], self.row(p, 16892)["owner_status"]), (None, "error-8"))

    def test_an_exited_pid_a_reused_pid_and_a_reused_parent_are_typed_and_never_borrow_another_identity(self) -> None:
        t = IDENT_T0
        later = _ident_dt.timedelta(seconds=30)
        procs = [(16892, "python", 29.0, t), (17000, "python", 28.0, t), (17001, "python", 27.0, t)]
        cim = [  # 16892: no CIM row at all (exited); 17000: CIM start differs from the sampled start (pid reused); 17001: its parent started AFTER it
            _ident_cim(17000, 501, "other.exe", t + _ident_dt.timedelta(hours=1), r"C:\Users\x\other.exe --secret-sauce", r"C:\Users\x\other.exe"),
            _ident_cim(17001, 502, "python.exe", t, r"C:\Python\python.exe C:\mlvtmp\lane-X-1\run.py"),
            _ident_cim(502, 402, "pwsh.exe", t + later, r"pwsh -File C:\Users\layib\agent-bridge\run.ps1"),
            _ident_cim(402, 0, "explorer.exe", t - _ident_dt.timedelta(hours=1), "explorer.exe")]
        p = self.run_identity_probe(procs, cim, CHAIN_OWNERS)
        gone = self.row(p, 16892)
        self.assertEqual((gone["owner"], gone["owner_status"], gone["cmdline"], gone["cmdline_status"], gone["exe"], gone["exe_status"]), (None, "exited", None, "exited", None, "exited"))
        self.assertEqual(gone["parent"]["status"], "none")
        reused = self.row(p, 17000)
        self.assertEqual(set(reused), {"pid", "name", "start_check"}, "a mismatched start carries pid, name and start_check only")
        self.assertEqual((reused["name"], reused["start_check"]), ("python", "mismatch"), "the name is the SAMPLED one, never the new process's")
        self.assertNotIn(17000, self.owner_calls(), "no GetOwner for a pid that is no longer the sampled process")
        self.assertNotIn("other.exe", json.dumps(p["identity"]))
        self.assertNotIn("secret-sauce", json.dumps(p["identity"]))
        child = self.row(p, 17001)
        self.assertEqual(child["parent"]["status"], "pid-reused")
        self.assertEqual((child["parent"]["name"], child["parent"]["started_utc"]), (None, None), "a reused parent is never named")
        self.assertEqual(child["grandparent"]["status"], "none")
        self.assertEqual((child["project"], child["project_from"]), ("mlv-app", "self"))

    def test_a_reused_parent_does_not_lend_its_project(self) -> None:
        t = IDENT_T0
        cim = [_ident_cim(16892, 502, "python.exe", t, r"C:\Python\python.exe C:\Users\x\job.py"),
               _ident_cim(502, 0, "pwsh.exe", t + _ident_dt.timedelta(seconds=30), r"pwsh -File C:\Users\layib\agent-bridge\run.ps1")]
        p = self.run_identity_probe([(16892, "python", 29.0, t)], cim, CHAIN_OWNERS)
        self.assertEqual((self.row(p, 16892)["project"], self.row(p, 16892)["project_from"]), ("unknown", "none"))

    def test_redaction_vectors_and_the_200_character_cap(self) -> None:
        b64 = _ident_base64.b64encode(b"A" * 36).decode()
        self.assertEqual(len(b64), 48)
        card = "lane-CPU-17V9-FPS-GAP-AB-1-r5x-20261024T0110Z"
        self.assertEqual(len(card), 45)
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF123_-xyzABC789"
        base = "python.exe "
        cap_cmd = base + ("ab " * 200)[:500 - len(base)]
        self.assertEqual(len(cap_cmd), 500)
        vectors = [
            ("url userinfo", "curl https://user:pw123@host.example/x", ["pw123", "user:pw"], "https://<redacted>@host.example/x"),
            ("bearer", "tool --header Bearer abcDEF123456 go", ["abcDEF123456"], "Bearer <redacted>"),
            ("password=", "app password=hunter2 run", ["hunter2"], "password=<redacted>"),
            ("--secret quoted", 'app --secret "a b" go', ['"a b"'], "--secret <redacted> go"),
            ("token prefixes", "x sk-abcdefghijklmnop ghp_abcdefghijklmnop12 AKIAABCDEFGHIJKLMNOP", ["sk-abcdefghijklmnop", "ghp_abcdefghijklmnop12", "AKIAABCDEFGHIJKLMNOP"], "<redacted> <redacted> <redacted>"),
            ("jwt", f"y {jwt}", [jwt, "eyJhbGci"], "y <redacted>"),
            ("opaque run", f"z {b64}", [b64], "z <redacted>"),
        ]
        t = IDENT_T0
        procs = []; cim = []
        for i, (_label, cmd, _gone, _want) in enumerate(vectors):
            procs.append((1001 + i, "tool", 20.0 - i, t)); cim.append(_ident_cim(1001 + i, 0, "tool.exe", t, cmd))
        procs.append((1101, "tool", 9.0, t)); cim.append(_ident_cim(1101, 0, "tool.exe", t, rf"python C:\x\tool.py {card}"))
        long_exe = "C:\\" + "\\".join(["d" * 20] * 10) + "\\python.exe"   # r5: only the exe path can be long enough to reach the cap
        procs.append((1102, "tool", 8.0, t)); cim.append(_ident_cim(1102, 0, "python.exe", t, cap_cmd, long_exe))
        p = self.run_identity_probe(procs, cim, {})
        for i, (label, cmd, gone, _want) in enumerate(vectors):   # r5: no line keeps argument text, so the r1 `want` (the text around a redacted value) is retired
            r = self.row(p, 1001 + i)
            for secret in gone:
                self.assertNotIn(secret, r["cmdline"], label)
            self.assertEqual(r["args_redacted"], "all", label)
            self.assertRegex(r["cmdline"], r"^tool\.exe args=\d+ script=<none>$", label)
        no_text = self.row(p, 1101)
        self.assertEqual(no_text["cmdline"], "tool.exe args=2 script=<none>", "r5: even a clean card name is argument text and is not persisted")
        capped = self.row(p, 1102)
        full = f"{long_exe} args={len(cap_cmd[len(base):].split())} script=<none>"
        self.assertGreater(len(full), 200)
        self.assertEqual(capped["cmdline"], full[:200] + f"...[+{len(full) - 200}]")
        self.assertEqual(capped["cmdline_truncated_chars"], len(full) - 200)

    def test_gpu_is_the_per_process_max_over_engines_and_a_gpu_only_pid_becomes_a_target(self) -> None:
        t = IDENT_T0
        procs = [(16892, "python", 29.1, t)]
        cim = [_ident_cim(16892, 0, "python.exe", t, "python a.py"), _ident_cim(7777, 0, "wmiprvse.exe", t - _ident_dt.timedelta(hours=2), r"C:\Windows\system32\wbem\wmiprvse.exe")]
        p = self.run_identity_probe(procs, cim, CHAIN_OWNERS | {7777: (0, "NT AUTHORITY", "NETWORK SERVICE")},
                                    gpu=[(16892, 0, 40.0), (16892, 1, 15.0), (7777, 0, 12.0)])
        ident = p["identity"]
        self.assertEqual(ident["gpu_status"], "ok")
        self.assertEqual(ident["gpu_top"], [16892, 7777])
        r = self.row(p, 16892)
        self.assertEqual((r["gpu_percent"], r["gpu_engtype"], r["gpu_status"]), (40.0, "3d", "ok"), "the max over engines (40), not their sum (55)")
        g = self.row(p, 7777)
        self.assertEqual(g["source"], ["gpu_top"])
        self.assertEqual((g["owner"], g["gpu_percent"], g["name"]), ("NT AUTHORITY\\NETWORK SERVICE", 12.0, "wmiprvse"))

    def test_a_pid_with_no_gpu_instance_is_zero_and_an_unreadable_counter_is_typed_null(self) -> None:
        t = IDENT_T0
        cim = [_ident_cim(16892, 0, "python.exe", t, "python a.py")]
        p = self.run_identity_probe([(16892, "python", 29.1, t)], cim, CHAIN_OWNERS, gpu=[(7777, 0, 12.0)])
        r = self.row(p, 16892)
        self.assertEqual((r["gpu_percent"], r["gpu_engtype"], r["gpu_status"]), (0.0, None, "no-gpu-context"))
        p = self.run_identity_probe([(16892, "python", 29.1, t)], cim, CHAIN_OWNERS, gpu=[])
        self.assertTrue(p["identity"]["gpu_status"].startswith("counter-unavailable: "), p["identity"]["gpu_status"])
        r = self.row(p, 16892)
        self.assertEqual((r["gpu_percent"], r["gpu_engtype"]), (None, None))
        self.assertEqual(r["gpu_status"], p["identity"]["gpu_status"])
        self.assertEqual(p["identity"]["gpu_top"], [])

    def test_a_get_counter_that_rejects_the_gpu_read_is_typed_and_changes_nothing_else(self) -> None:
        t = IDENT_T0
        cim = [_ident_cim(16892, 0, "python.exe", t, "python a.py")]
        p = self.run_identity_probe([(16892, "python", 29.1, t)], cim, CHAIN_OWNERS, old_counter_shadow=True)
        self.assertTrue(p["identity"]["gpu_status"].startswith("counter-unavailable: "), p["identity"]["gpu_status"])
        self.assertEqual(self.row(p, 16892)["owner"], "BACHELOR\\layib", "the CIM half of identity still works")

    def test_the_gpu_read_can_be_switched_off_with_a_typed_reason(self) -> None:
        body = _ident_probe_body().replace("$IdentityGpu = $true", "$IdentityGpu = $false").replace("$IdentityGpuDisabledWhy = ''", "$IdentityGpuDisabledWhy = 'disabled: dev-host read 9999 ms exceeded 2500 ms'")
        t = IDENT_T0
        cim = [_ident_cim(16892, 0, "python.exe", t, "python a.py")]
        p = self.run_identity_probe([(16892, "python", 29.1, t)], cim, CHAIN_OWNERS, gpu=[(16892, 0, 40.0)], body=body)
        self.assertEqual(p["identity"]["gpu_status"], "disabled: dev-host read 9999 ms exceeded 2500 ms")
        self.assertNotIn("gpu_top", p["identity"])
        self.assertEqual((self.row(p, 16892)["gpu_percent"], self.row(p, 16892)["gpu_status"]), (None, p["identity"]["gpu_status"]))

    def test_an_identity_failure_is_typed_and_every_other_field_is_unchanged(self) -> None:
        t = IDENT_T0
        procs = [(16892, "python", 29.1, t), (500, "pwsh", 1.0, t)]
        cim = [_ident_cim(16892, 500, "python.exe", t, "python a.py"), _ident_cim(500, 0, "pwsh.exe", t, "pwsh")]
        boom = self.run_identity_probe(procs, cim, CHAIN_OWNERS, throw=True)
        self.assertEqual(set(boom["identity"]), {"schema", "error"})
        self.assertEqual(boom["identity"]["schema"], IDENT_SCHEMA)
        self.assertIn("canned CIM failure", boom["identity"]["error"])
        # the same probe with the identity code CUT OUT is the reference: samples, top and every v2 field must equal it
        body = _ident_probe_body()
        cut = re.sub(r"# IDENTITY \(VENUE-PROBE-PROCESS-ATTRIBUTION-1\).*?(?=Write-Output \('VENUE_QUIET=')", "", body, flags=re.S).replace("    identity = $identity\n", "").replace("    identity = $identity\r\n", "")
        self.assertNotIn("identity", cut)
        plain = self.run_identity_probe(procs, cim, CHAIN_OWNERS, body=cut)
        self.assertNotIn("identity", plain)
        ok = self.run_identity_probe(procs, cim, CHAIN_OWNERS)
        for k, v in plain.items():
            self.assertEqual(boom[k], v, f"{k} changed when identity failed")
            self.assertEqual(ok[k], v, f"{k} changed when identity ran")
        self.assertEqual(set(boom) - set(plain), {"identity"})

    def test_past_the_wall_clock_budget_no_further_getowner_is_made_and_the_rest_is_still_filled(self) -> None:
        body = _ident_probe_body().replace("$IdentityBudgetMs = 25000", "$IdentityBudgetMs = -1")
        self.assertIn("$IdentityBudgetMs = -1", body)
        t = IDENT_T0
        cim = [_ident_cim(16892, 0, "python.exe", t, r"C:\mlvtmp\lane-X-1\bench.py")]
        p = self.run_identity_probe([(16892, "python", 29.1, t)], cim, CHAIN_OWNERS, body=body)
        r = self.row(p, 16892)
        self.assertEqual((r["owner"], r["owner_status"]), (None, "budget-exceeded"))
        self.assertEqual(self.owner_calls(), [], "no GetOwner call once the budget is spent")
        self.assertEqual((r["project"], r["cmdline"], r["cmdline_status"]), ("mlv-app", "python.exe args=0 script=<none>", "ok"), "what needs no call is still filled")

    def test_the_budget_is_a_pinned_literal_well_inside_the_120_second_submission_timeout(self) -> None:
        m = re.search(r"\$IdentityBudgetMs = (\d+)", _ident_probe_body())
        self.assertIsNotNone(m)
        self.assertLessEqual(int(m.group(1)), 30000)

    def test_the_decision_counters_never_include_the_gpu_engine(self) -> None:
        body = _ident_probe_body()
        decision = re.search(r"\$groups = \[ordered\]@\{.*?\$allPaths = [^\n]*", body, re.S).group(0)
        self.assertNotIn("gpu engine", decision.lower())
        self.assertEqual(body.count("Get-Counter -Counter '\\GPU Engine(*)"), 1, "exactly ONE GPU Engine read in the probe")
        self.assertEqual(len(re.findall(r"\bGet-Counter -Counter\b", re.sub(r"(?m)^\s*#.*$", "", body))), 2, "the decision read plus the one GPU read, nothing else")
        self.assertEqual(body.count("Start-Sleep -Seconds 12"), 1, "no new sleep text: the 12 s cadence is the gate's contract")
        self.assertEqual(len(re.findall(r"Get-CimInstance", body)), 1, "one query helper, called at most three times")
        self.assertNotIn("Win32_Process |", body, "never a full-table enumeration")
        self.assertNotRegex(body, r"(?i)\bGet-WmiObject\b|\bGetEnvironmentVariable\b|ReadProcessMemory")

    def test_the_real_probe_job_emits_a_typed_identity_with_a_status_for_every_null(self) -> None:
        job = self.tmp / "real-probe-job.ps1"
        job.write_text(_ident_probe_body().replace("Start-Sleep -Seconds 12", "Start-Sleep -Seconds 0"), encoding="utf-8")
        proc = _ident_run_reaped([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job)], timeout=240)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("VENUE_QUIET=")]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        p = json.loads(lines[0][len("VENUE_QUIET="):])
        self.assertEqual(p["schema"], "mlv-app/venue-quiet-probe/v2")
        ident = p["identity"]
        self.assertEqual(ident["schema"], IDENT_SCHEMA)
        if "error" in ident:   # a host where identity cannot run still emits a typed error, never a malformed line
            self.assertTrue(isinstance(ident["error"], str) and ident["error"])
            return
        self.assertTrue(all(isinstance(v, int) and v >= 0 for v in ident["cost_ms"].values()), ident["cost_ms"])
        self.assertIsInstance(ident["elevated"], bool)
        self.assertTrue(ident["probe_user"])
        top_pids = {t["pid"] for t in p["top"]}
        self.assertTrue(top_pids <= {r["pid"] for r in ident["processes"]}, "every top pid has an identity row")
        for r in ident["processes"]:
            if r["start_check"] == "mismatch":
                self.assertEqual(set(r), {"pid", "name", "start_check"})
                continue
            self.assertTrue(r["name"])
            self.assertTrue(set(r["source"]) <= {"top", "top_processes", "gpu_top"} and r["source"])
            if r["owner"] is None:
                self.assertRegex(r["owner_status"], r"^(access-denied|exited|budget-exceeded|error-\w+)$")
            else:
                self.assertEqual(r["owner_status"], "ok")
            for value, status in (("cmdline", "cmdline_status"), ("exe", "exe_status")):
                self.assertTrue((r[value] is None) == (r[status] != "ok"), (value, r))
            # r6: this host's real processes go through the real worker-side script check
            self.assertIn(r["script_check"], R6_SCRIPT_CHECKS if r["cmdline_status"] == "ok" else (None,), r)
            self.assertEqual(r["script"] is not None, r["script_check"] == "ok", r)
            self.assertTrue((r["gpu_percent"] is None) == (r["gpu_status"] not in ("ok", "no-gpu-context")), r)
            self.assertIn(r["project"], ("mlv-app", "agent-bridge", "adobe-ingester", "fleet-doctrine", "adversarialllm", "virtual-ten", "airmypc", "unknown"))
            self.assertIn(r["parent"]["status"], ("ok", "exited", "pid-reused", "none"))
            self.assertIn(r["grandparent"]["status"], ("ok", "exited", "pid-reused", "none"))
            for k in ("cmdline", "exe"):
                self.assertTrue(r[k] is None or len(r[k]) <= 200 + len("...[+999999]"))


IDENT_ROW = {"pid": 16892, "name": "python", "source": ["top", "top_processes"], "started_utc": "2026-10-24T01:02:03.0000000Z", "start_check": "match",
             "owner": "BACHELOR\\layib", "owner_status": "ok", "cmdline": "python bench.py --token <redacted>", "cmdline_status": "ok", "cmdline_redactions": 1,
             "cmdline_truncated_chars": 0, "exe": "C:\\Python\\python.exe", "exe_status": "ok", "script": "bench.py", "project": "mlv-app", "project_from": "self",
             "parent": {"pid": 500, "name": "pwsh", "started_utc": "2026-10-24T01:01:03.0000000Z", "status": "ok"},
             "grandparent": {"pid": 400, "name": "explorer", "started_utc": "2026-10-23T23:02:03.0000000Z", "status": "ok"},
             "gpu_percent": 40.0, "gpu_engtype": "3d", "gpu_status": "ok"}
IDENT_GPU_ROW = dict(IDENT_ROW, pid=7777, name="wmiprvse", source=["gpu_top"], owner="NT AUTHORITY\\NETWORK SERVICE", script=None, project="unknown", project_from="none",
                     parent={"pid": None, "name": None, "started_utc": None, "status": "none"}, grandparent={"pid": None, "name": None, "started_utc": None, "status": "none"},
                     gpu_percent=12.0)
IDENT_DENIED_ROW = dict(IDENT_ROW, pid=4, name="system", owner=None, owner_status="access-denied", cmdline=None, cmdline_status="unreadable", exe=None, exe_status="unreadable",
                        script=None, project="unknown", project_from="none", gpu_percent=None, gpu_engtype=None, gpu_status="counter-unavailable: x")
IDENT_MISMATCH_ROW = {"pid": 4242, "name": "pwsh", "start_check": "mismatch"}
IDENT_BLOCK = {"schema": IDENT_SCHEMA, "probe_user": "BACHELOR\\layib", "elevated": False, "project_roots": "r1", "gpu_basis": "per-process max over GPU engines of summed ...",
               "gpu_status": "ok", "gpu_top": [16892, 7777], "processes": [IDENT_ROW, IDENT_GPU_ROW, IDENT_DENIED_ROW, IDENT_MISMATCH_ROW],
               "cost_ms": {"cim": 12, "owner": 300, "gpu": 1200, "total": 1600}, "notes": {"cim_queries": "1 WQL queries for 4 target pid(s) (bound: 3); GetOwner once per target"}}
IDENT_TOP = [{"name": "python", "pid": 16892, "cpuSeconds": 29.1}, {"name": "pwsh", "pid": 4242, "cpuSeconds": 12.5}, {"name": "system", "pid": 4, "cpuSeconds": 3.5},
             {"name": "svchost", "pid": 999, "cpuSeconds": 1.5}]


@requires_windows_pwsh
class VenueProbeIdentityGateTextTests(unittest.TestCase):
    setUp = VenueQuietAttributionTests.setUp
    gate = VenueQuietAttributionTests.gate

    def probe(self, **extra) -> dict:
        return dict(V2_PROBE, top=IDENT_TOP, identity=IDENT_BLOCK, **extra)

    def test_top_lines_keep_their_prefix_and_append_comma_free_key_value_pairs(self) -> None:
        proc = self.gate(self.probe())
        out = proc.stdout
        self.assertEqual(proc.returncode, 0, out + proc.stderr)
        self.assertRegex(out, r"(?m)^COOLDOWN_UNMET mean=50\.0% threshold=20(\.0)?% waitedSec=\d+$", "the verdict is unchanged")
        self.assertIn("  top python pid=16892 cpuSeconds=29.1 owner=BACHELOR\\layib project=mlv-app script=bench.py parent=pwsh:500|ok gparent=explorer:400|ok started=2026-10-24T01:02:03Z gpu=40.0%\n", out.replace("\r\n", "\n"))
        self.assertIn("  top pwsh pid=4242 cpuSeconds=12.5 ident=pid-reused\n", out.replace("\r\n", "\n"), "a reused pid is named as such and nothing else")
        self.assertIn("  top system pid=4 cpuSeconds=3.5 owner=unreadable(access-denied) project=unknown script=n/a parent=pwsh:500|ok gparent=explorer:400|ok started=2026-10-24T01:02:03Z gpu=n/a\n", out.replace("\r\n", "\n"))
        self.assertRegex(out, r"(?m)^  top svchost pid=999 cpuSeconds=1\.5$", "a top row with no identity row prints exactly what it printed before")
        for line in out.splitlines():
            if line.startswith("  top ") and "cpuSeconds=" in line:
                for token in line.split("cpuSeconds=", 1)[1].split()[1:]:   # everything after the cpuSeconds value
                    self.assertRegex(token, r"^[a-z]+=\S+$", line)
                    self.assertNotIn(",", token)
        self.assertNotIn("bench.py --token", out, "no command line is ever printed")

    def test_the_run_local_driver_transform_still_yields_the_process_name(self) -> None:
        out = self.gate(self.probe()).stdout.replace("\r\n", "\n")
        line = next(ln for ln in out.split("\n") if ln.startswith("  top python pid=16892 cpuSeconds=29.1 "))
        step = re.sub(r"\s+cpuSeconds=", ":", re.sub(r"^\s+top\s+", "", line))
        entries = re.split(r",\s*", step)
        self.assertEqual(len(entries), 1, "no appended value contains a comma")
        self.assertEqual(entries[0].split()[0], "python")
        self.assertTrue(entries[0].startswith("python pid=16892:29.1 "), entries[0])

    def test_whitespace_and_commas_in_an_owner_are_replaced_by_underscores(self) -> None:
        row = dict(IDENT_ROW, owner="BACH ELOR\\lay,ib", script="my script.py", name="py thon")
        proc = self.gate(dict(V2_PROBE, top=IDENT_TOP[:1], identity=dict(IDENT_BLOCK, processes=[row], gpu_top=[])))
        self.assertIn("owner=BACH_ELOR\\lay_ib project=mlv-app script=my_script.py ", proc.stdout)

    def test_attr_proc_lines_gain_owner_and_project_and_the_rest_are_unchanged(self) -> None:
        probe = self.probe(top_processes=[{"instance": "python", "pid": 16892, "percent": 6.0}, {"instance": "aws", "pid": 43644, "percent": 4.79},
                                          {"instance": "pwsh", "pid": 4242, "percent": 1.0}])
        out = self.gate(probe).stdout
        self.assertRegex(out, r"(?m)^  attr proc python pid=16892 pct=6\.0% owner=BACHELOR\\layib project=mlv-app$")
        self.assertRegex(out, r"(?m)^  attr proc aws pid=43644 pct=4\.8%$")
        self.assertRegex(out, r"(?m)^  attr proc pwsh pid=4242 pct=1\.0% ident=pid-reused$")

    def test_ident_lines_follow_the_attr_lines(self) -> None:
        out = self.gate(self.probe()).stdout.replace("\r\n", "\n")
        self.assertIn("  ident probe user=BACHELOR\\layib elevated=false cost cim=12 owner=300 gpu=1200 total=1600\n", out)
        self.assertIn("  ident gpu wmiprvse pid=7777 gpu=12.0% engtype=3d owner=NT_AUTHORITY\\NETWORK_SERVICE project=unknown\n", out)
        self.assertNotIn("  ident gpu python", out, "a GPU pid already printed in top is not printed twice")
        self.assertIn("  ident note cim_queries: 1 WQL queries for 4 target pid(s) (bound: 3); GetOwner once per target\n", out)
        self.assertLess(out.index("  attr cores"), out.index("  ident probe "))
        self.assertLess(out.index("COOLDOWN_UNMET"), out.index("  top python"))

    def test_an_identity_error_prints_one_line_and_changes_nothing_else(self) -> None:
        plain = self.gate(dict(V2_PROBE, top=IDENT_TOP)).stdout.replace("\r\n", "\n")
        bad = self.gate(dict(V2_PROBE, top=IDENT_TOP, identity={"schema": IDENT_SCHEMA, "error": "canned CIM failure"}))
        self.assertEqual(bad.returncode, 0, bad.stdout + bad.stderr)
        out = bad.stdout.replace("\r\n", "\n")
        self.assertIn("  ident error canned CIM failure\n", out)
        keep = lambda text: [ln for ln in text.split("\n") if ln.startswith(("  top ", "  attr "))]
        self.assertEqual(keep(out), keep(plain))

    def test_a_probe_without_a_usable_identity_prints_exactly_what_it_printed_before(self) -> None:
        plain = self.gate(dict(V2_PROBE, top=IDENT_TOP)).stdout.replace("\r\n", "\n")
        keep = lambda text: [ln for ln in text.split("\n") if ln.startswith(("  top ", "  attr ", "  ident "))]
        for odd in ({"schema": "mlv-app/venue-quiet-probe-identity/v9", "processes": [IDENT_ROW]}, "garbage", 7, [IDENT_ROW]):
            proc = self.gate(dict(V2_PROBE, top=IDENT_TOP, identity=odd))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(keep(proc.stdout.replace("\r\n", "\n")), keep(plain), odd)
        v1 = self.gate(dict(V1_PROBE, identity=IDENT_BLOCK)).stdout
        self.assertRegex(v1, r"(?m)^  top pwsh pid=4242 cpuSeconds=12\.5 ident=pid-reused$", "identity is read from any probe that carries it")

    def test_a_malformed_identity_never_changes_the_verdict_or_the_exit_code(self) -> None:
        for odd in (dict(IDENT_BLOCK, processes=7), dict(IDENT_BLOCK, cost_ms=None, notes=None, gpu_top=7), dict(IDENT_BLOCK, processes=[None, {"pid": None}, 5])):
            proc = self.gate(dict(V2_PROBE, top=IDENT_TOP, identity=odd))
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% ")
        proc = self.gate(dict(V2_PROBE, top=IDENT_TOP, samples=[5.0, None, 5.0], identity=IDENT_BLOCK))
        self.assertEqual(proc.returncode, 6, "an unmeasured venue stays UNKNOWN, with no identity text")
        self.assertNotIn("  ident ", proc.stdout)

    def test_a_quiet_probe_is_attributed_too(self) -> None:
        proc = self.gate(self.probe(samples=[5.0, 5.0, 5.0]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^QUIET mean=5\.0% ")
        self.assertIn("owner=BACHELOR\\layib project=mlv-app", proc.stdout)

    def test_the_header_states_the_identity_contract(self) -> None:
        header = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8").split("[CmdletBinding", 1)[0]
        for needle in ("PROCESS IDENTITY", "mlv-app/venue-quiet-probe-identity/v1", "top-level schema stays v2", "AFTER the sample window", "at most 3 WQL", "GetOwner per TARGET",
                       "separate Get-Counter call", "REDACTION", "200-character cap", "NON-ELEVATED DEGRADATION", "WALL-CLOCK BUDGET", "budget-exceeded", "working directory is NOT collected", "Summarize-VenueBusyProbes.ps1"):
            self.assertIn(needle, header)
        for kept in ("schema v2", "The v1 `samples` and `top` fields are unchanged", "core_class unknown", "6 VENUE_QUIET_UNKNOWN"):
            self.assertIn(kept, header)

    def test_the_new_scripts_are_ascii_only(self) -> None:
        for name in ("Wait-VenueQuiet.ps1", "Summarize-VenueBusyProbes.ps1"):
            data = (DV / name).read_bytes()
            self.assertFalse([b for b in data if b > 127], f"{name} must stay ASCII-only")


def _ident_summary_probe(samples, top, identity=None) -> dict:
    probe = dict(V2_PROBE, samples=samples, top=top)
    if identity is not None:
        probe["identity"] = identity
    return probe


@requires_windows_pwsh
class VenueBusyProbeSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-busy-summary-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.tree = self.tmp / "tree"
        self.dv = self.tree / "tools" / "profiling" / "dual-venue"
        shutil.copytree(DV, self.dv, ignore=shutil.ignore_patterns("__pycache__"))
        self.share = self.tmp / "share-bachelor"
        (self.share / "inbox").mkdir(parents=True)
        table = json.loads((self.dv / "venues.json").read_text(encoding="utf-8"))
        table["venues"]["bachelor"]["agentShare"] = str(self.share)
        table["venues"]["bachelor"]["agentRoot"] = str(self.share) + "-root"
        (self.dv / "venues.json").write_text(json.dumps(table), encoding="utf-8")
        self.probes = self.tmp / "probes"
        self.probes.mkdir()

    def write(self, name: str, probe) -> Path:
        path = self.probes / name
        path.write_text(probe if isinstance(probe, str) else json.dumps(probe), encoding="utf-8")
        return path

    def fixtures(self) -> None:
        busy_a = _ident_summary_probe([50.0, 50.0, 50.0], [{"name": "python", "pid": 16892, "cpuSeconds": 29.1}, {"name": "explorer", "pid": 30, "cpuSeconds": 3.0}],
                                      dict(IDENT_BLOCK, processes=[dict(IDENT_ROW, gpu_percent=40.0),
                                                                    dict(IDENT_DENIED_ROW, pid=30, name="explorer", gpu_percent=None, gpu_status="no-gpu-context")], gpu_top=[16892]))
        busy_b = _ident_summary_probe([60.0, 60.0, 60.0], [{"name": "python", "pid": 16372, "cpuSeconds": 29.0}],
                                      dict(IDENT_BLOCK, processes=[dict(IDENT_ROW, pid=16372, script="other.py", gpu_percent=55.5)], gpu_top=[16372]))
        busy_v2 = _ident_summary_probe([70.0, 70.0, 70.0], [{"name": "pwsh", "pid": 4242, "cpuSeconds": 12.5}])
        quiet = _ident_summary_probe([5.0, 5.0, 5.0], [{"name": "python", "pid": 1, "cpuSeconds": 99.0}], dict(IDENT_BLOCK, processes=[dict(IDENT_ROW, pid=1)]))
        self.write("venue-quiet-probe-20261024T010000000Z.json", busy_a)
        self.write("venue-quiet-probe-20261024T010100000Z.json", busy_b)
        self.write("venue-quiet-probe-20261024T010200000Z.json", busy_v2)
        self.write("venue-quiet-probe-20261024T010300000Z.json", quiet)

    def summarize(self, *args: str, source: str | None = None) -> subprocess.CompletedProcess:
        cmd = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Summarize-VenueBusyProbes.ps1"), *args]
        if source:
            cmd += ["-DecisionSource", source]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=180)

    def test_busy_probes_are_grouped_by_project_with_exact_text_lines(self) -> None:
        self.fixtures()
        proc = self.summarize("-ProbeDir", str(self.probes))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = proc.stdout.replace("\r\n", "\n").strip().split("\n")
        self.assertEqual(lines[0], "- Busy probes summarized: 3 (quiet skipped 1, unknown skipped 0, unreadable 0)")
        self.assertEqual(lines[1], "- Busy probes by project (probes in which the project held a top-5 CPU row; cpuSeconds summed over those rows): "
                                   "mlv-app (2 probes, 58.1 s; e.g. python script=bench.py owner=BACHELOR\\layib; python script=other.py owner=BACHELOR\\layib), "
                                   "unknown(no-identity) (1 probes, 12.5 s; e.g. pwsh script=n/a owner=n/a), "
                                   "unknown (1 probes, 3.0 s; e.g. explorer script=n/a owner=unreadable(access-denied))")
        self.assertEqual(lines[2], "- GPU-heavy across busy probes: python pid=16372 project=mlv-app max gpu=55.5% (1 probes), python pid=16892 project=mlv-app max gpu=40.0% (1 probes)")
        self.assertEqual(len(lines), 3)

    def test_json_output_is_one_parseable_object_with_the_same_aggregate(self) -> None:
        self.fixtures()
        proc = self.summarize("-ProbeDir", str(self.probes), "-Json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        j = json.loads(proc.stdout)
        self.assertEqual(j["schema"], "mlv-app/venue-busy-probe-summary/v1")
        self.assertEqual((j["busy_probes"], j["quiet_skipped"], j["unknown_skipped"], j["unreadable"]), (3, 1, 0, 0))
        self.assertEqual([p["project"] for p in j["projects"]], ["mlv-app", "unknown(no-identity)", "unknown"])
        self.assertEqual((j["projects"][0]["probes"], j["projects"][0]["cpu_seconds"]), (2, 58.1))
        self.assertEqual(j["projects"][0]["examples"][0], {"name": "python", "script": "bench.py", "owner": "BACHELOR\\layib"})
        self.assertEqual([g["pid"] for g in j["gpu_heavy"]], [16372, 16892])

    def test_json_records_the_per_core_settings_next_to_the_mean_threshold_default_off(self) -> None:
        # VENUE-BUSY-PROBE-SUMMARY-SETTINGS-1: a reader of the JSON must be able to tell which per-core decision produced busy_probes
        self.fixtures()
        proc = self.summarize("-ProbeDir", str(self.probes), "-Json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        j = json.loads(proc.stdout)
        self.assertIs(j["per_core_enforce"], False)
        self.assertEqual(j["per_core_threshold_percent"], 60.0)
        # the existing keys and their values are unchanged
        self.assertEqual(j["schema"], "mlv-app/venue-busy-probe-summary/v1")
        self.assertEqual(j["threshold_percent"], 20.0)
        self.assertEqual((j["busy_probes"], j["quiet_skipped"], j["unknown_skipped"], j["unreadable"]), (3, 1, 0, 0))
        self.assertEqual([p["project"] for p in j["projects"]], ["mlv-app", "unknown(no-identity)", "unknown"])
        self.assertEqual([g["pid"] for g in j["gpu_heavy"]], [16372, 16892])

    def test_json_records_the_per_core_settings_the_caller_passed(self) -> None:
        self.fixtures()
        proc = self.summarize("-ProbeDir", str(self.probes), "-Json", "-PerCoreEnforce", "-PerCoreThresholdPercent", "45")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        j = json.loads(proc.stdout)
        self.assertIs(j["per_core_enforce"], True)
        self.assertEqual(j["per_core_threshold_percent"], 45.0)
        self.assertEqual(j["schema"], "mlv-app/venue-busy-probe-summary/v1")
        self.assertEqual(j["threshold_percent"], 20.0)
        self.assertEqual(list(j)[:4], ["schema", "threshold_percent", "per_core_enforce", "per_core_threshold_percent"])

    def test_include_quiet_keeps_the_quiet_probes(self) -> None:
        self.fixtures()
        proc = self.summarize("-ProbeDir", str(self.probes), "-IncludeQuiet")
        self.assertEqual(proc.stdout.splitlines()[0], "- Busy probes summarized: 4 (quiet skipped 0, unknown skipped 0, unreadable 0)")

    def test_the_decision_is_the_gates_own_unrounded_rule(self) -> None:
        # three 20.04% samples print as 20.0% but are BUSY at the 20 threshold (the gate's UNROUNDED mean decides)
        self.write("venue-quiet-probe-20261024T010000000Z.json", _ident_summary_probe([20.04, 20.04, 20.04], [{"name": "pwsh", "pid": 4242, "cpuSeconds": 1.0}]))
        self.write("venue-quiet-probe-20261024T010100000Z.json", _ident_summary_probe([5.0, None, 5.0], [{"name": "pwsh", "pid": 4242, "cpuSeconds": 1.0}]))
        proc = self.summarize("-ProbeDir", str(self.probes))
        self.assertEqual(proc.stdout.splitlines()[0], "- Busy probes summarized: 1 (quiet skipped 0, unknown skipped 1, unreadable 0)")

    def test_a_decision_that_cannot_be_lifted_is_exit_three_not_a_second_copy_of_the_rule(self) -> None:
        self.fixtures()
        empty = self.tmp / "no-decision.ps1"
        empty.write_text("# no decision function here\n", encoding="utf-8")
        proc = self.summarize("-ProbeDir", str(self.probes), source=str(empty))
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("DECISION_LIFT_FAILED", proc.stderr)
        self.assertEqual(proc.stdout, "")
        text = (DV / "Summarize-VenueBusyProbes.ps1").read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("function Get-VenueQuietDecision", code, "the quiet rule is lifted by AST, never copied")
        self.assertIn("FunctionDefinitionAst", code)

    def test_a_path_inside_a_venue_share_is_refused_before_anything_is_read(self) -> None:
        inside = self.share / "venue-quiet-probe-20261024T010000000Z.json"
        inside.write_text(json.dumps(_ident_summary_probe([50.0, 50.0, 50.0], [])), encoding="utf-8")
        for args in (["-ProbeJson", str(inside)], ["-ProbeDir", str(self.share)], ["-ProbeDir", str(self.share / "inbox")]):
            proc = self.summarize(*args)
            self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
            self.assertIn("PROBE_PATH_IN_VENUE_SHARE", proc.stderr)
            self.assertEqual(proc.stdout, "")

    def test_a_corrupt_file_is_counted_unreadable_and_other_files_in_the_dir_are_ignored(self) -> None:
        self.fixtures()
        self.write("venue-quiet-probe-20261024T010400000Z.json", "{ not json")
        self.write("other-notes.json", "{ not json either")
        proc = self.summarize("-ProbeDir", str(self.probes))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.splitlines()[0], "- Busy probes summarized: 3 (quiet skipped 1, unknown skipped 0, unreadable 1)")

    def test_a_comma_joined_probe_json_list_reaches_the_script_as_separate_files(self) -> None:
        # under `pwsh -File` a comma list reaches a [string[]] parameter as ONE string (CI: test_pwsh_string_array_file_params.py); the script must split it itself
        self.fixtures()
        a = self.probes / "venue-quiet-probe-20261024T010000000Z.json"
        b = self.probes / "venue-quiet-probe-20261024T010100000Z.json"
        proc = self.summarize("-ProbeJson", f"{a},{b}", "-Json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        j = json.loads(proc.stdout)
        self.assertEqual((j["busy_probes"], j["unreadable"]), (2, 0), j)
        self.assertEqual((j["projects"][0]["project"], j["projects"][0]["probes"], j["projects"][0]["cpu_seconds"]), ("mlv-app", 2, 58.1))

    def test_a_path_that_holds_a_comma_is_one_file_not_two(self) -> None:
        self.fixtures()
        odd = self.probes / "venue-quiet-probe-20261024T010900000Z,odd.json"
        odd.write_text((self.probes / "venue-quiet-probe-20261024T010000000Z.json").read_text(encoding="utf-8"), encoding="utf-8")
        proc = self.summarize("-ProbeJson", str(odd), "-Json")
        j = json.loads(proc.stdout)
        self.assertEqual((j["busy_probes"], j["unreadable"]), (1, 0), j)

    def test_overlapping_probe_json_and_probe_dir_inputs_are_counted_once(self) -> None:
        self.fixtures()
        a = self.probes / "venue-quiet-probe-20261024T010000000Z.json"
        only_dir = json.loads(self.summarize("-ProbeDir", str(self.probes), "-Json").stdout)
        both = json.loads(self.summarize("-ProbeJson", f"{a},{str(a).upper()}", "-ProbeDir", str(self.probes), "-Json").stdout)
        self.assertEqual(both, only_dir, "the same file by two spellings and by the directory is ONE probe")
        self.assertEqual(both["busy_probes"], 3)
        self.assertEqual(both["projects"][0]["cpu_seconds"], 58.1, "seconds are not doubled")
        self.assertEqual(both["unreadable"], 0)

    def test_the_same_basename_in_two_directories_is_two_probes_and_not_one(self) -> None:
        self.fixtures()
        name = "venue-quiet-probe-20261024T010000000Z.json"
        d1 = self.tmp / "run-1"; d2 = self.tmp / "run-2"
        for d in (d1, d2):
            d.mkdir()
            shutil.copyfile(self.probes / name, d / name)
        j = json.loads(self.summarize("-ProbeJson", f"{d1 / name},{d2 / name}", "-Json").stdout)
        self.assertEqual(j["busy_probes"], 2)
        mlv = next(p for p in j["projects"] if p["project"] == "mlv-app")
        self.assertEqual((mlv["probes"], mlv["cpu_seconds"]), (2, 58.2), "two probes held the project, not one")
        gpu = next(g for g in j["gpu_heavy"] if g["pid"] == 16892)
        self.assertEqual(gpu["probes"], 2)

    def test_no_input_is_a_usage_error_and_the_script_never_references_the_submitter(self) -> None:
        proc = self.summarize()
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        code = (DV / "Summarize-VenueBusyProbes.ps1").read_text(encoding="utf-8")
        self.assertNotIn("um-run.ps1", code, "read-only: it is not a submitter and must not name the one writer to a venue share")
        self.assertNotRegex(code, r"(?i)Remove-Item|Set-Content|Add-Content|Out-File|WriteAllText|New-Item|Move-Item|Copy-Item")


# ---------------------------------------------------------------------------------------------------
# VENUE-PROBE-PROCESS-ATTRIBUTION-1 r2: Dot B1 -- the redacted command line is retained, emitted and persisted as raw JSON, so no credential may reach it. r4 retired the r2/r3
# per-shape rule tests (they pinned the text kept AROUND a redacted value, which fail-closed redaction no longer keeps); their vectors live on in the r4 table below.
SYNTH_SECRET = "demoValue42"
SYNTH_BASIC = "QWxhZGRpbjpvcGVu"   # the RFC 7617 example credential (Aladdin:open): public and synthetic


@requires_windows_pwsh
class VenueProbeIdentityRedactionR2Tests(IdentityProbeMixin, unittest.TestCase):
    setUp = VenueQuietAttributionTests.setUp
    gate = VenueQuietAttributionTests.gate
    def test_the_persisted_raw_json_holds_no_secret_for_the_json_and_the_basic_shapes(self) -> None:
        t = IDENT_T0
        cmd = ('python C:\\mlvtmp\\lane-X-1\\tools\\bench.py {"password":"demoValue42","api_key" : "demoValue42"} '
               '--hdr "Authorization: Basic QWxhZGRpbjpvcGVu" --q password%3DdemoValue42')
        cim = [_ident_cim(16892, 0, "python.exe", t, cmd, r"C:\Python\python.exe")]
        p = self.run_identity_probe([(16892, "python", 29.1, t)], cim, CHAIN_OWNERS)
        for secret in (SYNTH_SECRET, SYNTH_BASIC):
            self.assertNotIn(secret, self.last_line, "the probe line the gate retains verbatim")
        self.assertGreaterEqual(self.row(p, 16892)["cmdline_redactions"], 3)
        self.assertRegex(self.row(p, 16892)["cmdline"], r"^C:\\Python\\python\.exe args=\d+ script=<none>$")
        # and through the gate: the raw file Wait-VenueQuiet.ps1 writes next to the job file
        proc = self.gate(dict(p, host="BACHELOR"))
        raws = [f for f in self.work.glob("venue-quiet-probe-*.json")]
        self.assertEqual(len(raws), 1, raws)
        raw = raws[0].read_text(encoding="utf-8")
        for secret in (SYNTH_SECRET, SYNTH_BASIC):
            self.assertNotIn(secret, raw)
        self.assertNotIn(SYNTH_SECRET, proc.stdout)


# r2 improvement: every synchronous call in the identity block has a deadline of its OWN (a hung CIM / owner / GPU read must not hold the probe toward its 120 s submission
# timeout). The forced-slow call is an UNINTERRUPTIBLE .NET sleep in the shadow (what a hung native call looks like), not an already-expired budget.
@requires_windows_pwsh
class VenueProbeIdentityCallDeadlineTests(IdentityProbeMixin, unittest.TestCase):
    DELAY_MS = 20000
    CALL_MS = 1500

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-probe-deadline-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        t = IDENT_T0
        self.procs = [(16892, "python", 29.1, t), (500, "pwsh", 1.0, t)]
        self.cim = [_ident_cim(16892, 500, "python.exe", t, r"C:\mlvtmp\lane-X-1\bench.py"), _ident_cim(500, 0, "pwsh.exe", t, "pwsh")]

    def reference(self) -> dict:
        """The same probe with identity cut out: samples, top and every v2 field must equal it whatever happened to identity."""
        body = re.sub(r"# IDENTITY \(VENUE-PROBE-PROCESS-ATTRIBUTION-1\).*?(?=Write-Output \('VENUE_QUIET=')", "", _ident_probe_body(), flags=re.S).replace("    identity = $identity\r\n", "").replace("    identity = $identity\n", "")
        return self.run_identity_probe(self.procs, self.cim, CHAIN_OWNERS, body=body)

    def slow(self, which: str) -> tuple:
        import time
        started = time.monotonic()
        p = self.run_identity_probe(self.procs, self.cim, CHAIN_OWNERS, gpu=[(16892, 0, 40.0)], delays={which: self.DELAY_MS}, call_ms=self.CALL_MS)
        return p, time.monotonic() - started

    def assert_measurement_unchanged(self, p: dict, elapsed: float) -> None:
        ref = self.reference()
        for k, v in ref.items():
            self.assertEqual(p[k], v, f"{k} changed because an identity call was slow")
        self.assertLess(elapsed, self.DELAY_MS / 1000.0 - 2, f"the probe waited for the slow call instead of bounding it ({elapsed:.1f} s)")

    def test_a_slow_gpu_read_times_out_into_a_status_and_the_rest_of_identity_is_still_filled(self) -> None:
        p, elapsed = self.slow("gpu")
        self.assertTrue(p["identity"]["gpu_status"].startswith("timeout: "), p["identity"])
        r = self.row(p, 16892)
        self.assertEqual((r["gpu_percent"], r["gpu_status"]), (None, p["identity"]["gpu_status"]))
        self.assertEqual((r["owner"], r["project"], r["cmdline_status"]), ("BACHELOR\\layib", "mlv-app", "ok"), "the CIM half still works")
        self.assert_measurement_unchanged(p, elapsed)

    def test_a_slow_getowner_times_out_into_owner_status_timeout_and_the_row_keeps_everything_else(self) -> None:
        p, elapsed = self.slow("owner")
        r = self.row(p, 16892)
        self.assertEqual((r["owner"], r["owner_status"]), (None, "timeout"))
        self.assertEqual((r["project"], r["cmdline_status"], r["gpu_percent"]), ("mlv-app", "ok", 40.0))
        self.assert_measurement_unchanged(p, elapsed)

    def test_a_slow_win32_process_query_degrades_identity_to_a_typed_error_and_nothing_else_moves(self) -> None:
        p, elapsed = self.slow("cim")
        self.assertEqual(set(p["identity"]), {"schema", "error"}, p["identity"])
        self.assertIn("timed out", p["identity"]["error"])
        self.assert_measurement_unchanged(p, elapsed)

    def test_the_call_deadline_is_a_pinned_literal_inside_the_identity_budget_arithmetic(self) -> None:
        body = _ident_probe_body()
        m = re.search(r"\$IdentityCallMs = (\d+)", body)
        self.assertIsNotNone(m)
        # GPU + 3 WQL queries + the r6 script-slot check + budget-gated GetOwner calls (the last may start just under the budget): the identity worst case, plus the 24 s
        # sample window, stays inside 120 s
        self.assertEqual(body.count("Invoke-IdentBounded $IdentScriptCheckScript"), 1, "r6: ONE bounded script-slot call per probe, not one per row")
        worst_s = (int(m.group(1)) * 5 + int(re.search(r"\$IdentityBudgetMs = (\d+)", body).group(1)) + int(m.group(1))) / 1000.0 + 24
        self.assertLess(worst_s, 110, worst_s)


# ---------------------------------------------------------------------------------------------------
# VENUE-PROBE-PROCESS-ATTRIBUTION-1 r4: FAIL-CLOSED command-line redaction. Per-shape rules (r1-r3) kept leaking new shapes (dot 93a864e0's escaped-quote JSON value, Grok G3 a quoted
# key then whitespace, G4 compound names, G5 a quote that opens mid-value). r5 retired the r4 denylist too (no line keeps argument text at all); these vectors live on in the r5
# table below, which checks every persisted copy. Synthetic values only.
R4_MARKER = "Value42"   # every vector's secret holds it (demoValue42, or a split form such as demo"Value42 / demo&Value42 / demo Value42)
R4_VECTORS = [
    # r4: the shapes that leaked on 7bca751c
    ("dot-93a864e0 three-backslash", r'tool {\"password\":\"prefix\\\" demoValue42 tail\"}'),
    ("dot-93a864e0 one-backslash", r'tool {\"password\":\"prefix\" demoValue42 tail\"}'),
    ("G3 fully quoted argv", '"tool.exe" "--password" "demoValue42"'),
    ("G3 quoted json key then whitespace", 'tool {"password" "demoValue42"}'),
    ("G3 quoted key then whitespace", 'tool "password"  demoValue42 --verbose'),
    ("G4 --db-pass", "tool --db-pass demoValue42"),
    ("G4 --passphrase", "tool --passphrase demoValue42"),
    ("G5 quote opens mid-value", 'tool --password=demo"Value42 tail" --verbose'),
    # r1
    ("r1 key and token", r"python C:\mlvtmp\lane-X-1\tools\bench.py --api_key=demoValue42 --token demoValue42"),
    ("r1 url userinfo", "curl https://user:demoValue42@host.example/x"),
    ("r1 bearer", "tool --header Bearer demoValue42 go"),
    ("r1 password=", "app password=demoValue42 run"),
    ("r1 --secret quoted", 'app --secret "a demoValue42" go'),
    ("r1 token prefixes", "x sk-demoValue42abcdef ghp_demoValue42abcdef AKIAdemoValue42ABCDEF"),
    ("r1 jwt", "y eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJkZW1vIn0.demoValue42sig"),
    ("r1 opaque run", "z demoValue42" + "A" * 40),
    # r2 (Dot B1): JSON keys, Authorization headers, a dashed password, URL-encoded keys
    ("r2 json dq", 'python x.py {"password":"demoValue42"}'),
    ("r2 json dq space", 'python x.py {"password": "demoValue42"}'),
    ("r2 json api_key spaced", 'python x.py {"api_key" : "demoValue42"}'),
    ("r2 json sq", "python x.py {'token':'demoValue42'}"),
    ("r2 json sq spaced", "python x.py {'token' : 'demoValue42'}"),
    ("r2 json client_secret", 'python x.py {"client_secret":"demoValue42","n":1}'),
    ("r2 json escaped", r'python x.py {\"password\":\"demoValue42\"}'),
    ("r2 json unquoted value", 'python x.py {"password":demoValue42}'),
    ("r2 json value with spaces", 'python x.py {"password":"demo Value42 two"}'),
    ("r2 authorization basic dq", 'curl -H "Authorization: Basic demoValue42" https://h.example/x'),
    ("r2 authorization basic sq", "curl -H 'Authorization: Basic demoValue42' https://h.example/x"),
    ("r2 authorization basic bare", "curl -H Authorization: Basic demoValue42 https://h.example/x"),
    ("r2 authorization bearer nospace", 'curl -H "authorization:Bearer demoValue42" https://h.example/x'),
    ("r2 authorization json", 'curl -d {"Authorization":"Bearer demoValue42"} https://h.example/x'),
    ("r2 dashed password", "tool --password=demoValue42 --verbose"),
    ("r2 urlenc password", "curl https://h.example/x?user=a&password%3DdemoValue42&z=1"),
    ("r2 urlenc password only", "curl https://h.example/x?password%3DdemoValue42"),
    ("r2 urlenc api_key", "curl https://h.example/x?api_key%3ademoValue42&z=1"),
    ("r2 urlenc json", "curl https://h.example/x?%22password%22%3A%22demoValue42%22"),
    ("r2 persisted json and basic", 'python C:\\mlvtmp\\lane-X-1\\tools\\bench.py {"password":"demoValue42","api_key" : "demoValue42"} '
                                    '--hdr "Authorization: Basic QWxhZGRpbjpvcGVu" --q password%3DdemoValue42'),
    # r3 (Dot B1 quoted values, Grok G1 short keys and -p, G2 '&')
    ("r3 b1 escaped quoted value", r'tool {\"password\":\"prefix demoValue42\"}'),
    ("r3 b1 escaped two words", r'tool {\"password\":\"two demoValue42 words\",\"n\":1}'),
    ("r3 b1 embedded escaped quote", r'tool {"password":"abc\"demoValue42 tail","n":1}'),
    ("r3 b1 dq value", 'app --password "two demoValue42 words" --verbose'),
    ("r3 b1 sq doubled quote", "app --token 'it''s demoValue42 here' --verbose"),
    ("r3 b1 unterminated", 'app --password "demoValue42 unterminated'),
    ("r3 g1 --pass", "app --pass demoValue42 --verbose"),
    ("r3 g1 json pass", 'app {"pass":"demoValue42"}'),
    ("r3 g1 --pw=", "app --pw=demoValue42 --verbose"),
    ("r3 g1 --pwd", "app --pwd demoValue42 --verbose"),
    ("r3 g1 -pass=", "app -pass=demoValue42"),
    ("r3 g1 mysql -p attached", "mysql -u root -pdemoValue42"),
    ("r3 g1 mysql -p separated", "mysql -u root -p demoValue42 mydb"),
    ("r3 g1 mysql -p attached then db", "mysql -u root -pdemoValue42 mydb"),
    ("r3 g1 mysql -p quoted", 'mysql -u root -p"demo Value42" mydb'),
    ("r3 g2 ampersand in a password", "tool --password=demo&Value42 --verbose"),
    ("r3 g2 ampersand in a token", "tool --token demoValue42&tail --verbose"),
    ("r3 g2 ampersand in a secret", "tool secret=demo&Value42&more x"),
    ("r3 g2 url query", "curl https://h.example/x?user=a&password=demoValue42&z=1 --verbose"),
    ("r3 -password=", "tool -password=demoValue42 --verbose"),
]
# the rest of a secret that spans words: the marker alone would miss a partial leak (7bca751c persisted G5 as `--password=<redacted> tail"`)
R4_EXTRA_GONE = {"dot-93a864e0 three-backslash": ("prefix", "tail"), "dot-93a864e0 one-backslash": ("prefix", "tail"), "G5 quote opens mid-value": ("tail",),
                 "r3 b1 escaped quoted value": ("prefix",), "r3 b1 escaped two words": ("two", "words"), "r3 b1 embedded escaped quote": ("abc", "tail"),
                 "r3 b1 dq value": ("two", "words"), "r3 b1 sq doubled quote": ("here",), "r3 b1 unterminated": ("unterminated",), "r3 g2 ampersand in a token": ("tail",),
                 "r3 g2 ampersand in a secret": ("more",)}
R4_TOKENS = ["pass", "pwd", "pw", "secret", "token", "key", "auth", "credential", "bearer", "cookie", "session"]


def _r4_strings(v) -> list:
    if isinstance(v, dict):
        return [s for x in v.values() for s in _r4_strings(x)]
    if isinstance(v, list):
        return [s for x in v for s in _r4_strings(x)]
    return [v] if isinstance(v, str) else []


# ---------------------------------------------------------------------------------------------------
# VENUE-PROBE-PROCESS-ATTRIBUTION-1 r5: NO ARGUMENT TEXT IS PERSISTED (allowlist). The r4 token denylist still leaked: dot 9b25f619 B1 promoted `"Bearer demoValue42.py"` to the
# script slot, B2 classed `--signature demoValue42` and `?sig=demoValue42` clean and kept their text, Grok G6 `-u user:demoValue42`. cmdline is now `<exe> args=<n> script=<path|<none>>`
# on EVERY line; script comes only from the first argument and only when it is an existing, drive-rooted, allowlisted path. Every vector goes through the REAL probe body, the REAL
# gate (probe line, raw probe JSON, gate text, every file the gate wrote) and the REAL Summarize-VenueBusyProbes.ps1 (text and -Json), and no persisted byte may hold the secret.
R5_SECRET = "demoValue42"
R5_VECTORS = [
    ("dot-9b25f619 B1 bearer value promoted to the script slot", 'tool --header "Bearer demoValue42.py"'),
    ("dot-9b25f619 B2 --signature", "tool --signature demoValue42"),
    ("dot-9b25f619 B2 url ?sig=", "curl https://h.example/x?sig=demoValue42"),
    ("grok G6 curl -u", "curl -u user:demoValue42"),
    ("grok G6 --user", "curl --user user:demoValue42 https://h.example/x"),
] + R4_VECTORS   # R4_VECTORS carries every r1-r4 vector


@requires_windows_pwsh
class VenueProbeIdentityNoArgumentTextR5Tests(IdentityProbeMixin, unittest.TestCase):
    CHUNK = 5

    def setUp(self) -> None:
        VenueQuietAttributionTests.setUp(self)
        # r7: fixture paths use the temp dir's LONG spelling. A GitHub Windows runner's TEMP is C:\Users\RUNNER~1\..., and an 8.3 '~' name is now rejected as text (and
        # GetFullPath, which expands it, would never equal the slot text either), so a fixture spelled through it could never be named.
        self.tmp = self.tmp.resolve()   # Summarize reads the top-5 CPU rows, so every vector of a chunk reaches its output too

    def gate(self, probe: dict) -> subprocess.CompletedProcess:
        import os
        return _ident_run_reaped([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                                  "-WorkDir", str(self.work), "-MaxWaitSec", "1", "-RecheckSec", "1"], timeout=180, env=dict(os.environ, UMSTUB_LINE=json.dumps(probe)))

    def summarize(self, *args: str) -> str:
        proc = _ident_run_reaped([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Summarize-VenueBusyProbes.ps1"), *args], timeout=180)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def persist(self, cmds: list, exe: str | None = None, name: str = "tool.exe") -> tuple:
        """cmds -> (rows as persisted in the gate's raw probe JSON, [(where, every persisted text)]). Each gate run gets a fresh work dir."""
        t = IDENT_T0
        self.work = self.tmp / f"work-{len(list(self.tmp.glob('work-*')))}"
        procs = [(2001 + i, "tool", 30.0 - i, t) for i in range(len(cmds))]
        cim = [_ident_cim(2001 + i, 0, name, t, c, exe) for i, c in enumerate(cmds)]
        p = self.run_identity_probe(procs, cim, {})
        # the canned counters give no decision samples (UNKNOWN prints no identity text at all), so the gate is handed a MEASURED busy reading: then its top / attr / ident
        # lines and the summary really carry every row, and the raw JSON it retains is this probe's identity verbatim
        proc = self.gate(dict(p, host="BACHELOR", samples=[50.0, 50.0, 50.0]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^  top tool pid=2001 cpuSeconds=\S+ owner=", "the gate printed the identity suffix")
        raws = sorted(self.work.glob("venue-quiet-probe-*.json"))
        self.assertEqual(len(raws), 1, (raws, proc.stdout + proc.stderr))
        by_pid = {r["pid"]: r for r in json.loads(raws[0].read_text(encoding="utf-8"))["identity"]["processes"]}
        texts = [("probe line", self.last_line), ("gate text", proc.stdout + proc.stderr)]
        # every DATA file the gate wrote (the .ps1 job file is the probe's own source code, not a persisted copy of identity)
        texts += [(f"work file {f.name}", f.read_bytes().decode("utf-8", "replace")) for f in sorted(self.work.rglob("*")) if f.is_file() and f.suffix.lower() != ".ps1"]
        summary_json = self.summarize("-ProbeDir", str(self.work), "-IncludeQuiet", "-Json")
        self.assertEqual(json.loads(summary_json)["busy_probes"], 1, "the probe reached the summary, so its rows were read")
        texts += [("summary text", self.summarize("-ProbeDir", str(self.work), "-IncludeQuiet")), ("summary json", summary_json)]
        return [by_pid.get(2001 + i) for i in range(len(cmds))], texts

    def real_file(self, *parts: str) -> Path:
        path = self.tmp.joinpath(*parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# fixture\n", encoding="utf-8")
        return path

    def test_no_persisted_byte_holds_any_vectors_secret(self) -> None:
        self.assertEqual(len({c for _l, c in R5_VECTORS}), len(R5_VECTORS))
        for at in range(0, len(R5_VECTORS), self.CHUNK):
            chunk = R5_VECTORS[at:at + self.CHUNK]
            rows, texts = self.persist([c for _l, c in chunk])
            for (label, cmd), r in zip(chunk, rows):
                with self.subTest(vector=label):
                    self.assertIsNotNone(r, label)
                    leaked = [s for s in _r4_strings(r) if any(g in s for g in (R5_SECRET, R4_MARKER) + R4_EXTRA_GONE.get(label, ()))]
                    self.assertEqual(leaked, [], f"{label}: {cmd!r} persisted {leaked!r}")
                    self.assertEqual((r["cmdline"], r["script"], r["args_redacted"], r["cmdline_status"]), (f"tool.exe args={r['cmdline_args']} script=<none>", None, "all", "ok"), r)
                    self.assertIsInstance(r["cmdline_token_hits"], int)
            for where, text in texts:
                with self.subTest(chunk=at, where=where):
                    self.assertNotIn(R5_SECRET, text)
                    self.assertNotIn(R4_MARKER, text)

    def test_a_clean_line_keeps_its_script_and_count_but_none_of_its_argument_text(self) -> None:
        probe = self.real_file("x", "probe.ps1")
        self.assertRegex(str(probe), r"^[A-Za-z]:\\[^\s=@?#:]+$", "the fixture path itself must satisfy the allowlist")
        pwsh = r"C:\Program Files\PowerShell\7\pwsh.exe"
        rows, texts = self.persist([f"pwsh -File {probe} -Verbose"], exe=pwsh, name="pwsh.exe")
        r = rows[0]
        self.assertEqual((r["cmdline"], r["script"], r["cmdline_args"], r["cmdline_redactions"], r["args_redacted"]), (f"{pwsh} args=3 script={probe}", str(probe), 3, 2, "all"))
        for where, text in texts:
            with self.subTest(where=where):
                self.assertNotIn("-Verbose", text)
                self.assertNotIn("-File", text)

    def test_a_script_path_with_whitespace_is_none_even_when_the_file_exists(self) -> None:
        spaced = self.real_file("a b", "x.py")
        rows, _texts = self.persist([r'"tool.exe" "C:\a b\x.py"', f'"tool.exe" "{spaced}"'])
        for r in rows:
            self.assertEqual((r["cmdline"], r["script"]), ("tool.exe args=1 script=<none>", None), r)

    def test_only_the_first_argument_can_be_the_script_and_only_an_existing_allowlisted_path(self) -> None:
        good = self.real_file("lane", "run.py")
        cmds = [
            f"python {good} --frames 120",            # 0: named
            f"python --verbose {good}",               # 1: a later argument is never taken
            f"pwsh -NoProfile -File {good}",          # 2: only a FIRST-slot -File is skipped
            f"python {good}.missing.py",              # 3: does not exist
            f"python {self.real_file('k=v', 'a.py')}",   # 4: '=' in the path
        ]
        rows, _texts = self.persist(cmds, name="python.exe")
        self.assertEqual(rows[0]["script"], str(good))
        self.assertEqual(rows[0]["cmdline"], f"python.exe args=3 script={good}")
        for i in (1, 2, 3, 4):
            with self.subTest(cmd=cmds[i]):
                self.assertEqual(rows[i]["script"], None)
                self.assertTrue(rows[i]["cmdline"].endswith(" script=<none>"), rows[i])
        rows, _texts = self.persist([f"python {self.real_file('m@x', 'a.py')}", f"python {self.real_file('q#x', 'a.py')}", f"python {self.real_file('lane', 'notes.txt')}"], name="python.exe")
        self.assertEqual([r["script"] for r in rows], [None, None, None], "'@', '#' and a non-script extension never fill the slot")

    def test_the_token_scan_is_a_diagnostic_integer_that_gates_nothing(self) -> None:
        body = _ident_probe_body()
        fn = re.search(r"function Protect-IdentCmdLine\(.*?\r?\n\}\r?\n", body, re.S).group(0)
        self.assertNotRegex(fn, r"\bif\s*\([^)]*(Measure-IdentTokenHits|token_hits)", "the scan never decides what is written")
        self.assertNotIn(".rest", fn, "the raw argument text after argv[0] never reaches the persisted line")
        self.assertIn("Measure-IdentTokenHits", fn)
        m = re.search(r"\$identSecretTokens = @\(([^)]*)\)", body)
        self.assertEqual(re.findall(r"'([^']*)'", m.group(1)), R4_TOKENS)

    def test_every_r4_path_that_appended_argument_text_is_gone(self) -> None:
        text = (DV / "Wait-VenueQuiet.ps1").read_text(encoding="utf-8")
        for gone in ("Test-IdentTainted", "Find-IdentScriptArg", "Test-IdentPathShaped", "Get-IdentScript ", "Get-IdentScript(", "': ' + $sp.rest", "<redacted>", "fail-closed",
                     "Protect-IdentText", "$keyLikePlain", "$keyLikeUrl", "$pFlag"):
            self.assertNotIn(gone, text)
        for needle in ("REDACTION IS AN ALLOWLIST (r5)", "NO ARGUMENT TEXT IS EVER PERSISTED", "cmdline_token_hits", "FIRST argument after the executable"):
            self.assertIn(needle, text)


# r3 Dot B2: Summarize-VenueBusyProbes.ps1 judges every input as TEXT before any Test-Path, Get-Item or read; a UNC or provider-qualified path is refused with a typed exit 2.
UNC_PROBE = r"\\192.0.2.1\mlv-agent\processed\venue-quiet-probe-20261024T010000000Z.json"   # TEST-NET-1: nothing answers there, so a touch would hang or throw


@requires_windows_pwsh
class VenueBusyProbeSummaryPathRefusalR3Tests(unittest.TestCase):
    setUp = VenueBusyProbeSummaryTests.setUp
    summarize = VenueBusyProbeSummaryTests.summarize
    write = VenueBusyProbeSummaryTests.write
    fixtures = VenueBusyProbeSummaryTests.fixtures

    def assert_refused(self, *args: str, code: str = "PROBE_PATH_UNC") -> None:
        import time
        started = time.monotonic()
        proc = self.summarize(*args)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn(code, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertLess(elapsed, 20, f"the refusal took {elapsed:.1f} s: a remote path was touched before it")

    def test_b2_a_unc_probe_json_is_refused_typed_and_never_touched(self) -> None:
        for spelling in (UNC_PROBE, UNC_PROBE.replace("\\", "/"), "FileSystem::" + UNC_PROBE, "Microsoft.PowerShell.Core\\FileSystem::" + UNC_PROBE, "\\\\?\\UNC\\192.0.2.1\\mlv-agent\\x.json"):
            with self.subTest(spelling=spelling):
                self.assert_refused("-ProbeJson", spelling)

    def test_b2_a_unc_part_of_a_comma_joined_list_refuses_the_whole_call_before_the_local_part_is_read(self) -> None:
        self.fixtures()
        local = self.probes / "venue-quiet-probe-20261024T010000000Z.json"
        self.assert_refused("-ProbeJson", f"{local},{UNC_PROBE}")
        self.assert_refused("-ProbeJson", f"{UNC_PROBE},{local}")

    def test_b2_a_unc_probe_dir_is_refused_typed_and_never_listed(self) -> None:
        self.assert_refused("-ProbeDir", r"\\192.0.2.1\mlv-agent\processed")

    def test_b2_a_comma_joined_venue_share_path_is_still_refused_typed(self) -> None:
        inside = self.share / "venue-quiet-probe-20261024T010000000Z.json"
        self.assert_refused("-ProbeJson", f"{inside},{self.probes / 'x.json'}", code="PROBE_PATH_IN_VENUE_SHARE")

    def test_b2_the_script_orders_the_text_refusals_before_every_filesystem_call_on_an_input(self) -> None:
        text = (DV / "Summarize-VenueBusyProbes.ps1").read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        fs_any = re.search(r"Test-Path|Get-Item|Get-ChildItem|Resolve-Path|ReadAllText|Get-Content", code)
        fs_probe = re.search(r"Test-Path|Get-Item|Get-ChildItem|Resolve-Path|ReadAllText\(\$f\)|Get-Content", code)
        unc, share = code.index("PROBE_PATH_UNC"), code.index("PROBE_PATH_IN_VENUE_SHARE")
        self.assertLess(unc, fs_any.start(), "the UNC refusal comes before the first filesystem call of any kind")
        self.assertLess(share, fs_probe.start(), "the venue-share refusal comes before the first filesystem call on an input")
        self.assertLess(share, code.index("$ProbeJson = @($ProbeJson | ForEach-Object"), "the -ProbeJson split (which asks Test-Path about each entry) comes after both refusals")


# ---------------------------------------------------------------------------------------------------
# VENUE-PROBE-PROCESS-ATTRIBUTION-1 r6 (dot advisory 581ad0fa): r5 checked the script slot with DriveInfo.DriveType and File.Exists on the MAIN path, outside the bounded identity
# worker, so a drive-rooted path through a local link to a dead share (C: is Fixed) could hold the probe past the identity budget. Every filesystem call for the slot now runs in
# ONE bounded worker call, walks the components without following a reparse point, and each row says why in script_check.
@requires_windows_pwsh
class VenueProbeScriptCheckR6Tests(IdentityProbeMixin, unittest.TestCase):
    setUp = VenueProbeIdentityNoArgumentTextR5Tests.setUp
    gate = VenueProbeIdentityNoArgumentTextR5Tests.gate
    real_file = VenueProbeIdentityNoArgumentTextR5Tests.real_file
    SLOW_MS = 20000
    CALL_MS = 1500
    SEAM = "$IdentityScriptCheckDelayMs = 0"

    def junction(self, name: str, target: Path) -> Path:
        import os
        link = self.tmp / name
        proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.addCleanup(os.rmdir, link)   # removes the junction itself, never its target; runs before the temp dir's own cleanup
        return link

    def test_a_slow_metadata_call_is_bounded_into_script_check_timeout_and_the_verdict_and_counters_are_intact(self) -> None:
        import time
        self.assertIn(self.SEAM, _ident_probe_body(), "the slow-metadata seam is a literal of the probe body, 0 in production")
        script = self.real_file("lane", "slow.py")
        t = IDENT_T0
        procs = [(16892, "python", 29.1, t), (500, "pwsh", 1.0, t)]
        cim = [_ident_cim(16892, 500, "python.exe", t, f"python {script} --frames 9", r"C:\Python\python.exe"), _ident_cim(500, 0, "pwsh.exe", t, "pwsh")]
        # control: the same probe without the delay names the script, and its time is the baseline the bounded run is held to
        started = time.monotonic()
        fast = self.run_identity_probe(procs, cim, CHAIN_OWNERS, call_ms=self.CALL_MS)
        baseline = time.monotonic() - started
        self.assertEqual((self.row(fast, 16892)["script"], self.row(fast, 16892)["script_check"]), (str(script), "ok"))
        started = time.monotonic()
        p = self.run_identity_probe(procs, cim, CHAIN_OWNERS, body=_ident_probe_body().replace(self.SEAM, f"$IdentityScriptCheckDelayMs = {self.SLOW_MS}"), call_ms=self.CALL_MS)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, self.SLOW_MS / 1000.0 - 2, f"the probe waited for the slow metadata call ({elapsed:.1f} s)")
        self.assertLess(elapsed - baseline, self.CALL_MS / 1000.0 + 4, f"budget + margin: {elapsed:.1f} s against a {baseline:.1f} s baseline")
        r = self.row(p, 16892)
        self.assertEqual((r["script"], r["script_check"], r["cmdline"], r["cmdline_status"]), (None, "timeout", r"C:\Python\python.exe args=3 script=<none>", "ok"))
        self.assertEqual((r["owner"], r["owner_status"]), ("BACHELOR\\layib", "ok"), "a later call gets a fresh worker")
        self.assertEqual(self.row(p, 500)["script_check"], "rejected")
        self.assertIn("script_check_unresolved", p["identity"]["notes"])
        self.assertIn("calls_abandoned", p["identity"]["notes"])
        # the measured fields equal the same probe with identity cut out
        cut = re.sub(r"# IDENTITY \(VENUE-PROBE-PROCESS-ATTRIBUTION-1\).*?(?=Write-Output \('VENUE_QUIET=')", "", _ident_probe_body(), flags=re.S).replace("    identity = $identity\r\n", "").replace("    identity = $identity\n", "")
        ref = self.run_identity_probe(procs, cim, CHAIN_OWNERS, body=cut)
        for k, v in ref.items():
            self.assertEqual(p[k], v, f"{k} changed because the script check was slow")
        # and through the gate: the verdict, the top line and the retained raw JSON all carry it
        proc = self.gate(dict(p, host="BACHELOR", samples=[50.0, 50.0, 50.0]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% threshold=20(\.0)?% waitedSec=\d+$")
        self.assertRegex(proc.stdout, r"(?m)^  top python pid=16892 cpuSeconds=\S+ owner=BACHELOR\\layib project=\S+ script=n/a script_check=timeout parent=")
        self.assertRegex(proc.stdout, r"(?m)^  ident note script_check_unresolved: ")
        raw = json.loads(next(self.work.glob("venue-quiet-probe-*.json")).read_text(encoding="utf-8"))
        self.assertEqual(self.row(raw, 16892)["script_check"], "timeout")
        self.assertNotIn("--frames", proc.stdout + json.dumps(raw))

    def test_a_junction_is_reparse_never_followed_and_every_other_reason_is_typed(self) -> None:
        real = self.real_file("real", "probe.py").parent
        missing_target = self.tmp / "nowhere"
        j_missing = self.junction("j-missing", missing_target)
        j_real = self.junction("j-real", real)
        cases = [
            (3001, f"python {j_missing}\\probe.py", None, "reparse"),     # a junction to a missing target (the dead-share stand-in)
            (3002, f"python {j_real}\\probe.py", None, "reparse"),        # a junction to a real file: r5 followed it and named the script
            (3003, f"python {real}\\probe.py", str(real / "probe.py"), "ok"),
            (3004, f"python {real}\\missing.py", None, "notfound"),
            (3005, f"python {real}\\probe.py\\x.py", None, "notfound"),   # a file where a directory must be
            (3006, "python bench.py --x", None, "relative"),
            (3007, r"python \\192.0.2.1\share\a.py", None, "nonfixed"),   # UNC: text only, never touched
            (3008, "python --verbose a.py", None, "rejected"),
        ]
        t = IDENT_T0
        procs = [(pid, "python", 30.0 - i, t) for i, (pid, _c, _s, _k) in enumerate(cases)]
        cim = [_ident_cim(pid, 0, "python.exe", t, cmd, r"C:\Python\python.exe") for (pid, cmd, _s, _k) in cases]
        p = self.run_identity_probe(procs, cim, CHAIN_OWNERS)
        for pid, cmd, script, check in cases:
            with self.subTest(cmd=cmd):
                r = self.row(p, pid)
                self.assertEqual(r["script"], script, r)   # first, on its own: r5 followed j-real and named the file behind it
                self.assertEqual(r.get("script_check"), check, r)
                self.assertTrue(r["cmdline"].endswith(f" script={script or '<none>'}"), r)
                self.assertIn(r["script_check"], R6_SCRIPT_CHECKS)
        self.assertFalse(missing_target.exists(), "the walk never created or followed the junction's target")
        self.assertNotIn("script_check_unresolved", p["identity"]["notes"])

    def test_no_filesystem_call_for_the_script_slot_runs_outside_the_worker_body(self) -> None:
        body = _ident_probe_body()
        code = "\n".join(ln for ln in body.splitlines() if not ln.lstrip().startswith("#"))
        m = re.search(r"\$IdentScriptCheckScript = \{.*?\n\}\.ToString\(\)", code, re.S)
        self.assertIsNotNone(m, "the script-slot filesystem calls live in ONE worker body")
        worker = m.group(0)
        rest = code.replace(worker, "")
        fs = r"DriveInfo|File\]::Exists|Directory\]::Exists|GetAttributes|FileInfo|DirectoryInfo|GetFileAttributes|\bTest-Path\b|\bGet-Item\b|\bResolve-Path\b"
        self.assertEqual(re.findall(fs, rest), [], "nothing on the main path touches the filesystem for the script slot")
        for needle in ("DriveInfo", "GetAttributes", "ReparsePoint"):
            self.assertIn(needle, worker)
        self.assertEqual(re.findall(r"\$IdentScriptCheckScript\b", rest), ["$IdentScriptCheckScript"], "the worker body is used once")
        self.assertRegex(rest, r"Invoke-IdentBounded \$IdentScriptCheckScript @\([^)]*\) \$IdentityCallMs", "and only through the bounded call, under the call deadline")
        self.assertRegex(body, r"(?m)^\$IdentityScriptCheckDelayMs = 0$", "the seam is 0 in production")
        # VENUE-PROBE-HEAT-GPU-TELEMETRY-1: HEAVY_SLOT_FEED is the agent's own feed override, read on the ONE line that picks the heat feed path; it reaches no seam
        self.assertEqual(sorted(set(re.findall(r"\$env:(\w+)", code))), ["COMPUTERNAME", "HEAVY_SLOT_FEED"], "no other environment variable can reach the probe, so none can set the seam")
        self.assertEqual([ln.strip() for ln in code.splitlines() if "$env:HEAVY_SLOT_FEED" in ln], [TEL_FEED_LINE])

    def test_script_check_is_printed_on_the_top_attr_and_ident_lines_of_a_row_that_carries_it(self) -> None:
        block = dict(IDENT_BLOCK, processes=[dict(IDENT_ROW, script_check="ok"), dict(IDENT_GPU_ROW, script_check="rejected"), dict(IDENT_DENIED_ROW, script_check=None), IDENT_MISMATCH_ROW])
        proc = self.gate(dict(V2_PROBE, top=IDENT_TOP, identity=block, top_processes=[{"instance": "python", "pid": 16892, "percent": 6.0}]))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout.replace("\r\n", "\n")
        self.assertIn("  top python pid=16892 cpuSeconds=29.1 owner=BACHELOR\\layib project=mlv-app script=bench.py script_check=ok parent=pwsh:500|ok gparent=explorer:400|ok started=2026-10-24T01:02:03Z gpu=40.0%\n", out)
        self.assertIn("  top system pid=4 cpuSeconds=3.5 owner=unreadable(access-denied) project=unknown script=n/a script_check=n/a parent=", out)
        self.assertIn("  top pwsh pid=4242 cpuSeconds=12.5 ident=pid-reused\n", out)
        self.assertIn("  attr proc python pid=16892 pct=6.0% owner=BACHELOR\\layib project=mlv-app script_check=ok\n", out)
        self.assertIn("  ident gpu wmiprvse pid=7777 gpu=12.0% engtype=3d owner=NT_AUTHORITY\\NETWORK_SERVICE project=unknown script_check=rejected\n", out)
        for line in out.split("\n"):
            if line.startswith("  top ") and "cpuSeconds=" in line:
                for token in line.split("cpuSeconds=", 1)[1].split()[1:]:
                    self.assertRegex(token, r"^[a-z_]+=\S+$", line)
                    self.assertNotIn(",", token)


# ---------------------------------------------------------------------------------------------------
# VENUE-PROBE-PROCESS-ATTRIBUTION-1 r7 (Grok G7, comment 6388632165; dot 7d1194c7): r5/r6 admitted '.' and '..' segments in the script slot, validated GetFullPath (the NORMALIZED
# path) and persisted the ORIGINAL slot text, so C:\tok-demoValue42\..\tools\run.py validated C:\tools\run.py and wrote the ghost segment into script, cmdline, the raw JSON and the
# gate text. Ghost spellings are now rejected as text, and what is persisted is the worker's validated full path, only when it equals the slot text case-insensitively.
@requires_windows_pwsh
class VenueProbeScriptGhostSegmentR7Tests(IdentityProbeMixin, unittest.TestCase):
    setUp = VenueProbeIdentityNoArgumentTextR5Tests.setUp
    gate = VenueProbeIdentityNoArgumentTextR5Tests.gate
    summarize = VenueProbeIdentityNoArgumentTextR5Tests.summarize
    persist = VenueProbeIdentityNoArgumentTextR5Tests.persist
    real_file = VenueProbeIdentityNoArgumentTextR5Tests.real_file
    CHUNK = 5   # Summarize reads the top-5 CPU rows
    CALL_MS = 1500
    LATE_MS = 250
    SEAM = "$IdentityScriptCheckDelayMs = 0"

    def vectors(self) -> list:
        target = str(self.real_file("lane", "run.py"))                # an existing file a ghost segment walks back to
        named = str(self.real_file("lane", f"{R5_SECRET}.py"))        # an existing file whose NAME holds the secret: r6 named it through any spelling
        self.assertRegex(target, r"^[A-Za-z]:\\[^~/]+$", "the fixtures are long, backslash-only paths")
        head, leaf = named.rsplit("\\", 1)
        return [
            # the brief's five
            ("ghost .. over a real file", f"{target[:2]}\\tok-{R5_SECRET}\\..\\{target[3:]}"),
            (". segment over a real file", f"{named[:2]}\\.\\{named[3:]}"),
            ("forward slashes", f"C:/x/{R5_SECRET}.py"),
            ("doubled backslash", f"C:\\a\\\\{R5_SECRET}.py"),
            ("\\\\?\\ prefix", f"\\\\?\\C:\\{R5_SECRET}.py"),
            # the same spellings of an EXISTING file (r6 validated and persisted these), plus the rule-2-only spelling and an 8.3 name
            ("forward slashes, existing", named.replace("\\", "/")),
            ("doubled backslash, existing", f"{head}\\\\{leaf}"),
            ("\\\\?\\ prefix, existing", "\\\\?\\" + named),
            ("\\\\.\\ prefix, existing", "\\\\.\\" + named),
            ("trailing-dot segment, existing (passes every shape rule; GetFullPath drops the dot)", f"{head}.\\{leaf}"),
            ("8.3 short name", f"C:\\PROGRA~1\\{R5_SECRET}.py"),
        ]

    def test_no_ghost_spelling_persists_its_text_and_each_is_rejected(self) -> None:
        vecs = self.vectors()
        for at in range(0, len(vecs), self.CHUNK):
            chunk = vecs[at:at + self.CHUNK]
            rows, texts = self.persist([f"python {v}" for _l, v in chunk], name="python.exe")
            for (label, v), r in zip(chunk, rows):
                with self.subTest(vector=label, slot=v):
                    self.assertIsNotNone(r, label)
                    leaked = [s for s in _r4_strings(r) if R5_SECRET in s or "tok-" in s]
                    self.assertEqual(leaked, [], f"{label}: {v!r} persisted {leaked!r}")
                    self.assertEqual((r["script"], r["script_check"], r["cmdline"]), (None, "rejected", "python.exe args=1 script=<none>"), r)
            for where, text in texts:
                with self.subTest(chunk=at, where=where):
                    self.assertNotIn(R5_SECRET, text)
                    self.assertNotIn("tok-", text)

    def test_a_clean_real_path_is_still_named_ok_as_the_workers_validated_text(self) -> None:
        good = str(self.real_file("lane", "run.py"))
        lower = good[0].lower() + good[1:]
        rows, _texts = self.persist([f"python {good} --frames 9", f"python {lower}"], name="python.exe")
        self.assertEqual((rows[0]["script"], rows[0]["script_check"], rows[0]["cmdline"]), (good, "ok", f"python.exe args=3 script={good}"))
        # drive-letter case is the one difference allowed; the two spellings are one candidate, so either validated spelling may be the one persisted
        self.assertEqual(rows[1]["script_check"], "ok", rows[1])
        self.assertIn(rows[1]["script"], (good, lower), rows[1])

    def test_the_shape_rules_reject_every_ghost_spelling_as_text(self) -> None:
        body = _ident_probe_body()
        code = "\n".join([re.search(p, body, re.M).group(0) for p in (r"^\$identScriptShape = .*$", r"^\$identScriptRelShape = .*$", r"^\$identScriptGhostShape = .*$")]
                         + [re.search(r"^function Get-IdentScriptSlot\(.*?\r?\n\}", body, re.S | re.M).group(0)])
        cases = [
            (r"C:\a\.\x.py", None, "rejected"), (r"C:\a\..\x.py", None, "rejected"), (r"C:\.\x.py", None, "rejected"), (r"C:\..\x.py", None, "rejected"),
            ("C:/x/y.py", None, "rejected"), (r"C:\a/x.py", None, "rejected"), (r"C:\a\\x.py", None, "rejected"), (r"C:\\x.py", None, "rejected"),
            (r"\\?\C:\x.py", None, "rejected"), (r"\\.\C:\x.py", None, "rejected"), (r"\\.\x.py", None, "rejected"), (r"C:\x.py\\", None, "rejected"), (r"C:\x\x.py/", None, "rejected"),
            (r"C:\PROGRA~1\x.py", None, "rejected"), (r"C:\a\RUN~12.py", None, "rejected"), (r".\x.py", None, "rejected"), ("//h/s/a.py", None, "rejected"),
            # still candidates for the worker, or typed exactly as r6 typed them
            (r"C:\a\x.py", r"C:\a\x.py", None), (r"C:\a.b\x.py", r"C:\a.b\x.py", None), (r"C:\..a\x.py", r"C:\..a\x.py", None), (r"C:\a~b\x.py", r"C:\a~b\x.py", None),
            (r"\\192.0.2.1\share\a.py", None, "nonfixed"), ("bench.py", None, "relative"), (r"lane\run.py", None, "relative"),
        ]
        harness = code + "\n" + r"""$cases = ConvertFrom-Json -InputObject $env:R7_SLOTS
$out = foreach ($c in $cases) { $s = Get-IdentScriptSlot @('python.exe', [string]$c) 'python.exe'; [pscustomobject]@{ path = $s.path; check = $s.check } }
ConvertTo-Json -InputObject @($out) -Compress"""
        import os
        job = self.tmp / "slot-shapes.ps1"
        job.write_text(harness, encoding="utf-8")
        proc = _ident_run_reaped([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job)], timeout=120, env=dict(os.environ, R7_SLOTS=json.dumps([c for c, _p, _k in cases])))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(len(got), len(cases), proc.stdout)
        for (slot, path, check), g in zip(cases, got):
            with self.subTest(slot=slot):
                self.assertEqual((g["path"], g["check"]), (path, check))

    def test_no_persisted_field_is_built_from_the_pre_validation_slot_text(self) -> None:
        body = _ident_probe_body()
        fn = re.search(r"function Protect-IdentCmdLine\(.*?\r?\n\}\r?\n", body, re.S).group(0)
        code = "\n".join(ln for ln in fn.splitlines() if not ln.lstrip().startswith("#"))
        # the slot text is only ever a lookup key or the comparand of the equality check
        self.assertIn("$rc.slot.path", code, "Protect-IdentCmdLine reads the slot")
        for m in re.finditer(r"\$rc\.slot\.path\b", code):
            ctx = code[max(0, m.start() - 20):m.end() + 1]
            self.assertRegex(ctx, r"ContainsKey\(\$rc\.slot\.path\)|\$ScriptChecks\[\$rc\.slot\.path\]|-ieq \$rc\.slot\.path", ctx)
        self.assertEqual(sorted(set(re.findall(r"\$script = ([^;}\n]+)", code))), ["$null", "[string]$v.full "], "script is null or the worker's validated full path")
        self.assertRegex(code, r"text = \$lim\.text; script = \$\(if \(\$script\)", "both persisted fields come from that $script")
        self.assertNotIn(".slot.path", body.replace(fn, "").replace("(Read-IdentCmdLine $cim[$_].CommandLine $cim[$_].ExecutablePath $cim[$_].Name).slot.path", ""),
                         "outside Protect-IdentCmdLine the slot text only feeds the worker's candidate list")
        worker = re.search(r"\$IdentScriptCheckScript = \{.*?\n\}\.ToString\(\)", body, re.S).group(0)
        self.assertIn("$full = [IO.Path]::GetFullPath([string]$p)", worker)
        self.assertIn("$full -ine [string]$p", worker, "the worker rejects a path GetFullPath rewrites")
        self.assertIn("full = $(if ($r -ceq 'ok') { $full } else { $null })", worker, "and hands back the full path only for ok")

    def test_the_worker_table_is_copied_once_under_its_lock_and_never_read_again(self) -> None:
        body = _ident_probe_body()
        fn = re.search(r"function Test-IdentScriptPaths\(.*?\r?\n\}\r?\n", body, re.S).group(0)
        code = "\n".join(ln for ln in fn.splitlines() if not ln.lstrip().startswith("#"))
        after = code[code.index("$w = Invoke-IdentBounded $IdentScriptCheckScript"):]
        self.assertIn("[Threading.Monitor]::Enter($out.SyncRoot)", after)
        self.assertIn("finally { [Threading.Monitor]::Exit($out.SyncRoot) }", after)
        reads = [ln for ln in after.split("\n")[1:] if "$out" in ln]
        self.assertEqual(len(reads), 2, reads)   # Enter, then the one copy and its Exit
        self.assertEqual(sum(ln.count("$out[") for ln in reads), 1, "ONE indexed read of the worker's table, inside the locked copy")
        self.assertRegex(reads[1], r"^\s*try \{ foreach \(\$k in @\(\$out\.Keys\)\) \{ \$got\[\$k\] = \$out\[\$k\] \} \}")

    def test_an_answer_just_after_the_deadline_is_ignored_and_reads_timeout(self) -> None:
        self.assertIn(self.SEAM, _ident_probe_body())
        script = str(self.real_file("lane", "late.py"))
        t = IDENT_T0
        procs = [(16892, "python", 29.1, t), (500, "pwsh", 1.0, t)]
        cim = [_ident_cim(16892, 500, "python.exe", t, f"python {script}", r"C:\Python\python.exe"), _ident_cim(500, 0, "pwsh.exe", t, "pwsh")]
        # the injected worker answers ok for the path LATE_MS after the call deadline
        p = self.run_identity_probe(procs, cim, CHAIN_OWNERS, body=_ident_probe_body().replace(self.SEAM, f"$IdentityScriptCheckDelayMs = {self.CALL_MS + self.LATE_MS}"),
                                    call_ms=self.CALL_MS)
        r = self.row(p, 16892)
        self.assertEqual((r["script"], r["script_check"], r["cmdline"]), (None, "timeout", r"C:\Python\python.exe args=1 script=<none>"), r)
        self.assertIn("script_check_unresolved", p["identity"]["notes"])
        self.assertEqual((r["owner"], r["owner_status"]), ("BACHELOR\\layib", "ok"), "the next call still gets a fresh worker")


# ---------------------------------------------------------------------------------------------------
# VENUE-PROBE-HEAT-GPU-TELEMETRY-1: BACHELOR-HEAT-WHILE-QUIET-DIAG-1 r1 found two long quiet-lease waits that fit the heat gate while the probe saw nothing: it had no heat, power,
# throttle or GPU field and no time since the last lease. Every probe line now carries heat{} (the agent's own feed reading), gpu{} (the dGPU power state read FIRST, by a method
# that does not wake it; utilisation / temperature / power only when it is already D0) and seconds_since_last_lease (+ last_lease{}). RECORD-ONLY: the verdict never reads them.
# The real probe job runs against a canned feed file, canned Get-PnpDevice / Get-PnpDeviceProperty / nvidia-smi shadows in the telemetry worker, and a canned agent.log.
TEL_SCHEMA = "mlv-app/venue-quiet-probe-telemetry/v1"
TEL_FEED_LINE = r"$HeatFeedPath = $(if ($env:HEAVY_SLOT_FEED) { $env:HEAVY_SLOT_FEED } else { 'C:\temp\hwinfo-log-adapter.csv' })"
TEL_HEAT_FIELDS = ("sample_age_s", "hottest_core_c", "max15_c", "mean_c", "throttled", "window_rows", "package_w")
TEL_GPU_FIELDS = ("utilization_percent", "temp_c", "power_w", "pstate")
TEL_VM_ADAPTER = r"PCI\VEN_15AD&DEV_0405&SUBSYS_040515AD&REV_00\3&18D45AA6&0&78"
TEL_DGPU = r"PCI\VEN_10DE&DEV_25A2&SUBSYS_0B2A1028&REV_A1\4&1E3A2B4C&0&0008"
TEL_DGPU_B = r"PCI\VEN_10DE&DEV_2684&SUBSYS_0B2A1028&REV_A1\4&2F4B3C5D&0&0010"
TEL_WORKER_PRELUDE = r"""$script:TelFake = ConvertFrom-Json -InputObject @'
__TELFAKE__
'@
function Get-PnpDevice {
    [CmdletBinding()] param($Class, [switch]$PresentOnly)
    [IO.File]::AppendAllText('__TELLOG__', "enum|$Class|$([bool]$PresentOnly)`n")
    if ($script:TelFake.pnpDelayMs) { [Threading.Thread]::Sleep([int]$script:TelFake.pnpDelayMs) }
    if ($script:TelFake.enumThrow) { throw [string]$script:TelFake.enumThrow }
    @($script:TelFake.devices | ForEach-Object { [pscustomobject]@{ InstanceId = [string]$_; Class = 'Display' } })
}
function Get-PnpDeviceProperty {
    [CmdletBinding()] param($InstanceId, $KeyName)
    [IO.File]::AppendAllText('__TELLOG__', "prop|$InstanceId|$KeyName`n")
    if ($script:TelFake.propThrow) { throw [string]$script:TelFake.propThrow }
    $d = $script:TelFake.data
    if ($script:TelFake.dataById -and $script:TelFake.dataById.PSObject.Properties[[string]$InstanceId]) { $d = $script:TelFake.dataById.PSObject.Properties[[string]$InstanceId].Value }
    [pscustomobject]@{ KeyName = $KeyName; Type = 'Binary'; Data = [byte[]]@($d | ForEach-Object { [byte]$_ }) }
}
function nvidia-smi {
    [IO.File]::AppendAllText('__TELLOG__', 'smi|' + (($args | ForEach-Object { [string]$_ }) -join ' ') + "`n")
    if ($script:TelFake.smiDelayMs) { [Threading.Thread]::Sleep([int]$script:TelFake.smiDelayMs) }
    if ($script:TelFake.smiThrow) { throw [string]$script:TelFake.smiThrow }
    $global:LASTEXITCODE = [int]$script:TelFake.smiRc
    @($script:TelFake.smiOut)
}
"""


def _tel_power_data(dstate_code: int) -> list:
    # CM_POWER_DATA (56 bytes): PD_Size, then PD_MostRecentPowerState (DEVICE_POWER_STATE: 0 unspecified, 1 D0 ... 4 D3), then the capabilities
    return [56, 0, 0, 0, dstate_code, 0, 0, 0, 9, 0, 0, 0] + [0] * 44


def _tel_hermetic(body: str, tmp: Path, tel: dict | None) -> str:
    """Point the probe body's telemetry at canned inputs under tmp (no-op on a body without the telemetry block). Default: no feed, no NVIDIA adapter, no agent.log."""
    tel = dict(tel or {})
    fake = {"devices": [TEL_VM_ADAPTER], "data": _tel_power_data(1), "smiRc": 0, "smiOut": [], "pnpDelayMs": 0, "smiDelayMs": 0, "dataById": None,
            "enumThrow": None, "propThrow": None, "smiThrow": None}
    fake.update(tel.get("fake", {}))
    log = tmp / "tel-calls.log"
    log.write_text("", encoding="utf-8")
    if not tel.get("env_feed"):
        body = body.replace(TEL_FEED_LINE, "$HeatFeedPath = '%s'" % str(tel.get("feed", tmp / "no-such-feed.csv")).replace("'", "''"))
    body = body.replace("$LeaseLogPath = ''", "$LeaseLogPath = '%s'" % str(tel.get("log", tmp / "no-such-agent.log")).replace("'", "''"))
    if "call_ms" in tel:
        body = body.replace("$TelemetryCallMs = 4000", f"$TelemetryCallMs = {tel['call_ms']}")
    if "read_delay_ms" in tel:
        body = body.replace("$TelemetryReadDelayMs = 0", f"$TelemetryReadDelayMs = {tel['read_delay_ms']}")
    prelude = TEL_WORKER_PRELUDE.replace("__TELFAKE__", json.dumps(fake)).replace("__TELLOG__", str(log))
    b64 = _ident_base64.b64encode(prelude.encode("utf-8")).decode("ascii")
    return body.replace("$TelemetryWorkerPrelude = ''", f"$TelemetryWorkerPrelude = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))")


def _tel_feed_rows(temps: list, watts: list, throttled: list) -> list:
    # the HWiNFO adapter CSV the agent reads: a header, then rows whose column 0 starts with a digit; 3 = PackageC (the hottest core), 4 = package W, 5 / 6 = throttling
    rows = ["Date,Time,Tick,PackageC,PackageW,CoreThrottling,RingThrottling"]
    for i, (c, w, thr) in enumerate(zip(temps, watts, throttled)):
        rows.append(f"10/24/2026,08:{i // 30:02d}:{(2 * i) % 60:02d},{i},{c:.1f},{w:.1f},{'Yes' if thr else 'No'},No")
    return rows


def _tel_expected_heat(temps: list, watts: list, throttled: list) -> dict:
    # heavy-slot.py's feed_metrics() as the agent reimplements it (ConvertFrom-HeavySlotFeedRows): last 150 rows, window = last 60, max / watts over the last 15
    t, w, th = temps[-150:], watts[-150:], throttled[-150:]
    return {"window_rows": len(t[-60:]), "throttled": sum(1 for x in th[-60:] if x), "mean_c": round(sum(t[-60:]) / len(t[-60:]), 2),
            "max15_c": max(t[-15:]), "package_w": round(sum(w[-15:]) / len(w[-15:]), 1), "hottest_core_c": t[-1]}


def _tel_cut(body: str) -> str:
    """The probe body with the telemetry block and its output keys CUT OUT: the reference every other field must equal."""
    cut = re.sub(r"# HEAT / GPU / LEASE TELEMETRY \(VENUE-PROBE-HEAT-GPU-TELEMETRY-1\).*?(?=# IDENTITY \(VENUE-PROBE-PROCESS-ATTRIBUTION-1\))", "", body, flags=re.S)
    return re.sub(r"(?m)^    telemetry_schema = .*\r?\n", "", cut)


@requires_windows_pwsh
class VenueProbeHeatGpuTelemetryTests(IdentityProbeMixin, unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="venue-probe-telemetry-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        t = IDENT_T0
        self.procs = [(16892, "python", 29.1, t)]
        self.cim = [_ident_cim(16892, 0, "python.exe", t, "python a.py")]

    def probe(self, **tel) -> dict:
        env = tel.pop("env", None)
        return self.run_identity_probe(self.procs, self.cim, CHAIN_OWNERS, tel=tel, env=env)

    def feed(self, lines: list, age_s: float = 0.0) -> Path:
        import os
        import time
        f = self.tmp / "hwinfo-log-adapter.csv"
        f.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        ts = time.time() - age_s
        os.utime(f, (ts, ts))
        return f

    def agent_log(self, lines: list) -> Path:
        f = self.tmp / "agent.log"
        f.write_text("".join(ln + "\r\n" for ln in lines), encoding="ascii")
        return f

    def calls(self) -> list:
        return [ln for ln in (self.tmp / "tel-calls.log").read_text(encoding="utf-8").splitlines() if ln]

    def assert_typed(self, block: dict, fields: tuple) -> None:
        # every field is present (never omitted); a null always has a typed reason, and a reason is never left behind for a value
        for k in fields:
            self.assertIn(k, block)
            if block[k] is None:
                self.assertTrue(isinstance(block["reasons"].get(k), str) and block["reasons"][k], (k, block))
            else:
                self.assertNotIn(k, block["reasons"], (k, block))

    def test_every_probe_carries_heat_gpu_and_lease_and_every_unknown_is_a_typed_null(self) -> None:
        p = self.probe()
        self.assertEqual(p["schema"], "mlv-app/venue-quiet-probe/v2", "the top-level schema stays v2: telemetry only adds fields")
        self.assertEqual(p["telemetry_schema"], TEL_SCHEMA)
        heat = p["heat"]
        self.assertEqual(heat["source"], "unavailable")
        self.assert_typed(heat, TEL_HEAT_FIELDS)
        self.assertTrue(all(heat[k] is None for k in TEL_HEAT_FIELDS), heat)
        self.assertEqual({heat["reasons"][k] for k in TEL_HEAT_FIELDS}, {"feed-missing"})
        self.assertNotEqual(heat.get("throttled"), 0, "0 is never an unknown")
        gpu = p["gpu"]
        self.assertEqual((gpu["state"], gpu["skipped_wake_risk"], gpu["device"], gpu["method"]), ("absent", False, None, "DEVPKEY_Device_PowerData"))
        self.assert_typed(gpu, TEL_GPU_FIELDS)
        self.assertEqual({gpu["reasons"][k] for k in TEL_GPU_FIELDS}, {"no-dgpu"})
        self.assertIsNone(p["seconds_since_last_lease"])
        self.assertEqual(p["last_lease"]["reason"], "log-missing")
        self.assertTrue(p["last_lease"]["source"].endswith("no-such-agent.log"), p["last_lease"])
        self.assertEqual([c for c in self.calls() if c.startswith("smi|")], [], "no dGPU: nvidia-smi is never run")

    def test_heat_is_the_agents_own_feed_reading_over_the_agents_window(self) -> None:
        n = 80
        temps = [60.0 + (i % 7) for i in range(n - 1)] + [97.0]
        watts = [10.0 + (i % 5) for i in range(n)]
        thr = [i % 9 == 0 for i in range(n)]
        p = self.probe(feed=self.feed(_tel_feed_rows(temps, watts, thr)))
        heat = p["heat"]
        self.assertEqual(heat["source"], str(self.tmp / "hwinfo-log-adapter.csv"))
        self.assertEqual(heat["reasons"], {}, heat)
        want = _tel_expected_heat(temps, watts, thr)
        for k, v in want.items():
            self.assertAlmostEqual(heat[k], v, places=2, msg=k)
        self.assertEqual(heat["hottest_core_c"], 97.0, "the newest row's PackageC: the hottest single core")
        self.assertTrue(0 <= heat["sample_age_s"] < 60, heat)
        self.assertEqual((heat["window_rule_rows"], heat["stale_after_s"]), (60, 120))

    def test_the_heat_reading_matches_the_agent_function_on_the_same_rows(self) -> None:
        # parity pin: the agent's ConvertFrom-HeavySlotFeedRows (lifted verbatim from ultra-magnus-agent.ps1) over the same rows gives the same throttled / mean / max15 / W
        n = 170
        temps = [55.0 + (i * 37 % 41) for i in range(n)]
        watts = [8.0 + (i * 13 % 29) for i in range(n)]
        thr = [(i * 7) % 5 == 0 for i in range(n)]
        lines = _tel_feed_rows(temps, watts, thr)
        p = self.probe(feed=self.feed(lines))
        agent = (ROOT / "tools" / "profiling" / "bachelor" / "ultra-magnus-agent.ps1").read_text(encoding="utf-8")
        fn = re.search(r"function ConvertFrom-HeavySlotFeedRows \{.*?\r?\n\}\r?\n", agent, re.S).group(0)
        self.assertRegex(agent, r"\[int\]\$GateSustainedRows = 60\b")
        self.assertRegex(agent, r"\[int\]\$GateFeedStaleSec = 120\b")
        script = self.tmp / "agent-parse.ps1"
        lines_file = self.tmp / "agent-lines.txt"
        lines_file.write_text("\n".join(lines), encoding="utf-8")
        script.write_text("$GateSustainedRows = 60\n" + fn + f"\n$m = ConvertFrom-HeavySlotFeedRows -Lines @(Get-Content -LiteralPath '{lines_file}')\n$m | ConvertTo-Json -Compress\n", encoding="utf-8")
        out = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, timeout=120)
        m = json.loads(out.stdout)
        heat = p["heat"]
        self.assertEqual((heat["throttled"], heat["window_rows"]), (m["throttled"], m["window"]))
        self.assertAlmostEqual(heat["mean_c"], m["meanC"], places=2)
        self.assertAlmostEqual(heat["max15_c"], m["max15C"], places=2)
        self.assertAlmostEqual(heat["package_w"], m["watts30"], places=1)

    def test_heavy_slot_feed_overrides_the_feed_path_as_it_does_for_the_agent(self) -> None:
        import os
        f = self.feed(_tel_feed_rows([70.0] * 20, [12.0] * 20, [False] * 20))
        p = self.probe(env_feed=True, env=dict(os.environ, HEAVY_SLOT_FEED=str(f)))
        self.assertEqual(p["heat"]["source"], str(f))
        self.assertEqual(p["heat"]["mean_c"], 70.0)

    def test_a_stale_short_or_unparseable_feed_is_a_typed_null_never_a_zero(self) -> None:
        rows = _tel_feed_rows([90.0] * 30, [40.0] * 30, [True] * 30)
        p = self.probe(feed=self.feed(rows, age_s=600))
        heat = p["heat"]
        self.assertEqual(heat["source"], str(self.tmp / "hwinfo-log-adapter.csv"), "a stale feed was found: its path is the source")
        self.assertTrue(590 <= heat["sample_age_s"] <= 700, heat)
        self.assert_typed(heat, TEL_HEAT_FIELDS)
        self.assertEqual({heat["reasons"][k] for k in TEL_HEAT_FIELDS if k != "sample_age_s"}, {"feed-stale"})
        p = self.probe(feed=self.feed(_tel_feed_rows([90.0] * 10, [40.0] * 10, [False] * 10)))
        self.assertEqual({p["heat"]["reasons"][k] for k in TEL_HEAT_FIELDS if k != "sample_age_s"}, {"feed-too-few-rows"})
        self.assertIsNone(p["heat"]["throttled"], "fewer than 15 rows is unknown, never 0 throttled")
        bad = rows[:-1] + ["10/24/2026,08:59:58,99,n/a,40.0,No,No"]
        p = self.probe(feed=self.feed(bad))
        self.assertEqual({p["heat"]["reasons"][k] for k in TEL_HEAT_FIELDS if k != "sample_age_s"}, {"feed-unparseable"})

    def test_a_hung_feed_read_is_a_typed_timeout_and_the_probe_still_answers(self) -> None:
        f = self.feed(_tel_feed_rows([70.0] * 20, [12.0] * 20, [False] * 20))
        p = self.probe(feed=f, call_ms=1000, read_delay_ms=4000)
        self.assertEqual(p["heat"]["source"], "unavailable")
        self.assertEqual({p["heat"]["reasons"][k] for k in TEL_HEAT_FIELDS}, {"timeout"})
        self.assertEqual(len(p["samples"]), 3)

    def test_a_d3_dgpu_is_never_woken(self) -> None:
        p = self.probe(fake={"devices": [TEL_VM_ADAPTER, TEL_DGPU], "data": _tel_power_data(4), "smiOut": ["37, 61, 22.53, P2"]})
        gpu = p["gpu"]
        self.assertEqual((gpu["state"], gpu["skipped_wake_risk"], gpu["device"], gpu["power_state_raw"]), ("D3", True, TEL_DGPU, 4))
        self.assert_typed(gpu, TEL_GPU_FIELDS)
        self.assertEqual({gpu["reasons"][k] for k in TEL_GPU_FIELDS}, {"skipped-wake-risk"})
        calls = self.calls()
        self.assertEqual([c for c in calls if c.startswith("smi|")], [], "D3: nothing but the power state is read")
        self.assertEqual(calls, ["enum|Display|True", f"prop|{TEL_DGPU}|DEVPKEY_Device_PowerData"])

    def test_an_unknown_power_state_is_never_woken_either(self) -> None:
        for fake, why in (({"devices": [TEL_DGPU], "propThrow": "canned PnP failure"}, "pnp-read-failed"),
                          ({"devices": [TEL_DGPU], "data": _tel_power_data(0)}, "unspecified-power-state"),
                          ({"enumThrow": "canned enumeration failure"}, "pnp-read-failed")):
            p = self.probe(fake=fake)
            gpu = p["gpu"]
            self.assertEqual((gpu["state"], gpu["skipped_wake_risk"]), ("unknown", True), fake)
            self.assertEqual(gpu["state_reason"], why, gpu)
            self.assertEqual({gpu["reasons"][k] for k in TEL_GPU_FIELDS}, {"skipped-wake-risk"})
            self.assertEqual([c for c in self.calls() if c.startswith("smi|")], [], fake)

    def test_a_hung_power_state_read_is_unknown_and_never_woken(self) -> None:
        p = self.probe(fake={"devices": [TEL_DGPU], "data": _tel_power_data(1), "pnpDelayMs": 4000, "smiOut": ["37, 61, 22.53, P2"]}, call_ms=1000)
        gpu = p["gpu"]
        self.assertEqual((gpu["state"], gpu["skipped_wake_risk"], gpu["state_reason"]), ("unknown", True, "timeout"))
        self.assertEqual([c for c in self.calls() if c.startswith("smi|")], [])
        self.assertEqual(len(p["samples"]), 3)

    def test_an_awake_d0_dgpu_is_read_after_its_power_state(self) -> None:
        p = self.probe(fake={"devices": [TEL_DGPU], "data": _tel_power_data(1), "smiOut": ["37, 61, 22.53, P2"]})
        gpu = p["gpu"]
        self.assertEqual((gpu["state"], gpu["skipped_wake_risk"], gpu["device"]), ("D0", False, TEL_DGPU))
        self.assertEqual((gpu["utilization_percent"], gpu["temp_c"], gpu["power_w"], gpu["pstate"]), (37.0, 61.0, 22.53, "P2"))
        self.assertEqual(gpu["reasons"], {})
        calls = self.calls()
        self.assertEqual(len(calls), 3, calls)
        self.assertTrue(calls[0].startswith("enum|") and calls[1].startswith("prop|"), "the power state is read FIRST")
        self.assertEqual(calls[2], "smi|--query-gpu=utilization.gpu,temperature.gpu,power.draw,pstate --format=csv,noheader,nounits")

    # VENUE-PROBE-HEAT-GPU-TELEMETRY-1 r2 (CoS B1): nvidia-smi has no device selector here, so it reads EVERY NVIDIA adapter while only one adapter's power state was checked.
    # With more than one NVIDIA display adapter the reading cannot be bound to the checked one: fail toward null, and never query an adapter whose power state was not read.
    def assert_ambiguous_dgpu(self, gpu: dict, why: str) -> None:
        self.assert_typed(gpu, TEL_GPU_FIELDS)
        self.assertTrue(all(gpu[k] is None for k in TEL_GPU_FIELDS), gpu)
        self.assertEqual({gpu["reasons"][k] for k in TEL_GPU_FIELDS}, {why}, gpu)

    def test_two_nvidia_adapters_both_d0_record_no_gpu_numbers_and_never_run_nvidia_smi(self) -> None:
        # nvidia-smi lists adapter B first: r1 recorded B's numbers as the checked adapter A's
        p = self.probe(fake={"devices": [TEL_VM_ADAPTER, TEL_DGPU, TEL_DGPU_B], "data": _tel_power_data(1), "smiOut": ["99, 93, 140.0, P0", "37, 61, 22.53, P2"]})
        self.assert_ambiguous_dgpu(p["gpu"], "multi-dgpu-ambiguous")
        self.assertEqual([c for c in self.calls() if c.startswith("smi|")], [], "an ambiguous dGPU set is never queried")

    def test_two_nvidia_adapters_one_in_d3_never_query_the_unchecked_adapter(self) -> None:
        p = self.probe(fake={"devices": [TEL_DGPU, TEL_DGPU_B], "data": _tel_power_data(1), "dataById": {TEL_DGPU: _tel_power_data(1), TEL_DGPU_B: _tel_power_data(4)},
                             "smiOut": ["37, 61, 22.53, P2", "0, 40, 5.0, P8"]})
        self.assert_ambiguous_dgpu(p["gpu"], "multi-dgpu-ambiguous")
        self.assertEqual([c for c in self.calls() if c.startswith("smi|")], [], "adapter B may be asleep: nvidia-smi would wake it")

    def test_one_nvidia_adapter_but_two_nvidia_smi_rows_is_ambiguous_never_the_first_row(self) -> None:
        p = self.probe(fake={"devices": [TEL_DGPU], "data": _tel_power_data(1), "smiOut": ["99, 93, 140.0, P0", "", "37, 61, 22.53, P2"]})
        gpu = p["gpu"]
        self.assertEqual((gpu["state"], gpu["skipped_wake_risk"], gpu["device"]), ("D0", False, TEL_DGPU))
        self.assert_ambiguous_dgpu(gpu, "nvidia-smi-ambiguous")

    def test_one_nvidia_adapter_with_a_vm_display_adapter_still_records_its_one_row(self) -> None:
        p = self.probe(fake={"devices": [TEL_VM_ADAPTER, TEL_DGPU], "data": _tel_power_data(1), "smiOut": ["", "37, 61, 22.53, P2", ""]})
        gpu = p["gpu"]
        self.assertEqual((gpu["utilization_percent"], gpu["temp_c"], gpu["power_w"], gpu["pstate"]), (37.0, 61.0, 22.53, "P2"))
        self.assertEqual(gpu["reasons"], {})

    def test_a_d0_read_that_fails_is_typed(self) -> None:
        p = self.probe(fake={"devices": [TEL_DGPU], "data": _tel_power_data(1), "smiThrow": "The term 'nvidia-smi' is not recognized as a name of a cmdlet"})
        self.assertEqual({p["gpu"]["reasons"][k] for k in TEL_GPU_FIELDS}, {"nvidia-smi-missing"})
        p = self.probe(fake={"devices": [TEL_DGPU], "data": _tel_power_data(1), "smiRc": 9, "smiOut": ["NVIDIA-SMI has failed"]})
        self.assertEqual({p["gpu"]["reasons"][k] for k in TEL_GPU_FIELDS}, {"nvidia-smi-failed"})
        p = self.probe(fake={"devices": [TEL_DGPU], "data": _tel_power_data(1), "smiOut": ["[N/A], 61, [Not Supported], P8"]})
        gpu = p["gpu"]
        self.assertEqual((gpu["utilization_percent"], gpu["temp_c"], gpu["power_w"], gpu["pstate"]), (None, 61.0, None, "P8"))
        self.assertEqual((gpu["reasons"]["utilization_percent"], gpu["reasons"]["power_w"]), ("not-supported", "not-supported"))
        self.assert_typed(gpu, TEL_GPU_FIELDS)

    def test_seconds_since_last_lease_is_the_newest_lease_event_in_the_agent_log(self) -> None:
        now = _ident_dt.datetime.now(_ident_dt.timezone.utc)
        cdt = _ident_dt.timezone(_ident_dt.timedelta(hours=-5))
        ts = lambda ago: (now - _ident_dt.timedelta(seconds=ago)).astimezone(cdt).isoformat()
        log = self.agent_log([
            f"{ts(4000)} quiet-lease: active job=dve-a slot=1 held=1:4242 until=1761300000 minutes=3 waited=541s requests=1 refusals=0 owner=[ok] heat=[clear]",
            f"{ts(3000)} quiet-lease: ended job=dve-a pid=4242 active=True rc=0 releaseRc=0 killed= renews=2/2 renewFailures=0 renewSlow=0 renewMaxGapSec=61 why=[job done]",
            f"{ts(900)} quiet-lease: ended job=dve-b pid=4243 active=False rc=75 releaseRc=0 killed= renews=0/0 renewFailures=0 renewSlow=0 renewMaxGapSec=0 why=[refused]",
            f"{ts(800)} heavy-slot gate: something else entirely",
        ])
        p = self.probe(log=log)
        self.assertEqual(p["last_lease"]["event"], "ended", "a request that never held a lease (active=False) is not a lease")
        self.assertIsNone(p["last_lease"]["reason"])
        self.assertEqual(p["last_lease"]["source"], str(log))
        self.assertTrue(2990 <= p["seconds_since_last_lease"] <= 3200, p["seconds_since_last_lease"])
        log = self.agent_log([f"{ts(500)} quiet-lease: batch-parked token=abcdef012345 lease=x jobs=1 remaining=1500s idleBound=300s"])
        p = self.probe(log=log)
        self.assertEqual(p["last_lease"]["event"], "batch-parked")
        self.assertTrue(490 <= p["seconds_since_last_lease"] <= 700, p)

    def test_a_log_with_no_lease_record_is_a_typed_null(self) -> None:
        p = self.probe(log=self.agent_log(["2026-10-25T08:00:00.0000000-05:00 agent started pid=1"]))
        self.assertIsNone(p["seconds_since_last_lease"])
        self.assertEqual((p["last_lease"]["reason"], p["last_lease"]["event"]), ("no-lease-record", None))

    def test_telemetry_is_record_only_and_moves_no_other_field(self) -> None:
        hot = _tel_feed_rows([99.0] * 70, [60.0] * 70, [True] * 70)
        busy_gpu = {"devices": [TEL_DGPU], "data": _tel_power_data(1), "smiOut": ["99, 93, 140.0, P0"]}
        with_tel = self.probe(feed=self.feed(hot), fake=busy_gpu)
        self.assertEqual((with_tel["heat"]["throttled"], with_tel["gpu"]["state"]), (60, "D0"))
        plain = self.run_identity_probe(self.procs, self.cim, CHAIN_OWNERS, body=_tel_cut(_ident_probe_body()))
        for k in ("heat", "gpu", "seconds_since_last_lease", "last_lease", "telemetry_schema"):
            self.assertNotIn(k, plain)
        for k, v in plain.items():
            if k == "identity":
                continue
            self.assertEqual(with_tel[k], v, f"{k} changed when telemetry ran")
        self.assertEqual(set(with_tel) - set(plain), {"heat", "gpu", "seconds_since_last_lease", "last_lease", "telemetry_schema"})

    def test_the_telemetry_calls_are_bounded_inside_the_submission_timeout(self) -> None:
        body = _ident_probe_body()
        call = int(re.search(r"\$TelemetryCallMs = (\d+)", body).group(1))
        ident = int(re.search(r"\$IdentityCallMs = (\d+)", body).group(1))
        budget = int(re.search(r"\$IdentityBudgetMs = (\d+)", body).group(1))
        self.assertEqual(len(re.findall(r"Invoke-TelBounded \$Tel\w+Script", body)), 4, "four bounded calls: feed, agent log, power state, nvidia-smi")
        self.assertEqual(body.count("Invoke-TelBounded $TelSmiScript"), 1, "ONE nvidia-smi call site")
        worst_s = (ident * 5 + budget + ident + call * 4) / 1000.0 + 24
        self.assertLess(worst_s, 110, worst_s)
        code = re.sub(r"(?m)^\s*#.*$", "", body)
        self.assertEqual(len(re.findall(r"\bGet-Counter -Counter\b", code)), 2, "telemetry adds no counter read")
        self.assertEqual(len(re.findall(r"Get-CimInstance", body)), 1, "telemetry adds no WQL query")
        self.assertIn("if ($script:IdentAbandoned -gt 0 -or $script:TelAbandoned -gt 0)", body, "an abandoned telemetry worker ends the job like an abandoned identity worker")

    def test_the_real_probe_job_emits_typed_telemetry_on_this_host(self) -> None:
        job = self.tmp / "real-probe-job.ps1"
        job.write_text(_ident_probe_body().replace("Start-Sleep -Seconds 12", "Start-Sleep -Seconds 0").replace("$LeaseLogPath = ''", "$LeaseLogPath = '%s'" % (self.tmp / "none.log")), encoding="utf-8")
        proc = _ident_run_reaped([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(job)], timeout=240)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("VENUE_QUIET=")]
        self.assertEqual(len(lines), 1, proc.stdout + proc.stderr)
        p = json.loads(lines[0][len("VENUE_QUIET="):])
        self.assertEqual(p["telemetry_schema"], TEL_SCHEMA)
        self.assert_typed(p["heat"], TEL_HEAT_FIELDS)
        self.assert_typed(p["gpu"], TEL_GPU_FIELDS)
        self.assertIn(p["gpu"]["state"], ("D0", "D1", "D2", "D3", "unknown", "absent"))
        self.assertIs(p["gpu"]["skipped_wake_risk"], p["gpu"]["state"] not in ("D0", "absent"))
        self.assertTrue(p["seconds_since_last_lease"] is not None or p["last_lease"]["reason"])


TEL_BLOCK_HOT = {"source": "C:\\temp\\hwinfo-log-adapter.csv", "sample_age_s": 1.5, "hottest_core_c": 100.0, "max15_c": 100.0, "mean_c": 95.24, "throttled": 60,
                 "window_rows": 60, "package_w": 44.0, "window_rule_rows": 60, "stale_after_s": 120, "reasons": {}, "why": None}
TEL_GPU_D3 = {"method": "DEVPKEY_Device_PowerData", "vendor_filter": "VEN_10DE", "device": TEL_DGPU, "state": "D3", "power_state_raw": 4, "state_reason": None,
              "skipped_wake_risk": True, "utilization_percent": None, "temp_c": None, "power_w": None, "pstate": None,
              "reasons": {k: "skipped-wake-risk" for k in TEL_GPU_FIELDS}, "why": None}
TEL_LEASE = {"source": "C:\\mlv-agent\\logs\\agent.log", "event": "ended", "utc": "2026-10-24T08:10:25.0000000Z", "reason": None, "why": None}


@requires_windows_pwsh
class VenueProbeTelemetryGateTextTests(unittest.TestCase):
    setUp = VenueQuietAttributionTests.setUp
    gate = VenueQuietAttributionTests.gate

    def tel(self, probe: dict) -> dict:
        return dict(probe, telemetry_schema=TEL_SCHEMA, heat=TEL_BLOCK_HOT, gpu=TEL_GPU_D3, seconds_since_last_lease=1834.2, last_lease=TEL_LEASE)

    def test_a_hot_throttled_probe_with_a_quiet_cpu_keeps_its_verdict(self) -> None:
        quiet = dict(V2_PROBE, samples=[5.0, 5.0, 5.0])
        before = self.gate(quiet)
        after = self.gate(self.tel(quiet))
        self.assertEqual((before.returncode, after.returncode), (0, 0), after.stdout + after.stderr)
        verdict = lambda out: re.sub(r"waitedSec=\d+", "waitedSec=N", re.search(r"(?m)^(QUIET|COOLDOWN_UNMET) .*$", out).group(0))
        self.assertEqual(verdict(after.stdout), verdict(before.stdout))
        self.assertRegex(after.stdout, r"(?m)^QUIET mean=5\.0% threshold=20(\.0)?% waitedSec=\d+$", "heat is record-only: a hot venue with a quiet CPU is still QUIET")
        busy = self.gate(self.tel(V2_PROBE))
        self.assertRegex(busy.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% ")

    def test_the_telemetry_is_printed_after_the_verdict_as_comma_free_pairs(self) -> None:
        out = self.gate(self.tel(V2_PROBE)).stdout
        self.assertRegex(out, r"(?m)^  tele heat source=C:\\temp\\hwinfo-log-adapter\.csv age=1\.5s hottest=100\.0C max15=100\.0C mean=95\.2C throttled=60/60 W=44\.0$")
        self.assertRegex(out, r"(?m)^  tele gpu state=D3 skipped_wake_risk=true util=n/a temp=n/a W=n/a pstate=n/a$")
        self.assertRegex(out, r"(?m)^  tele lease since=1834s event=ended$")
        self.assertLess(out.index("COOLDOWN_UNMET"), out.index("  tele heat"))

    def test_a_probe_without_telemetry_prints_no_tele_line(self) -> None:
        self.assertNotIn("  tele ", self.gate(V2_PROBE).stdout)

    def test_malformed_telemetry_never_changes_the_verdict_or_the_exit_code(self) -> None:
        proc = self.gate(dict(V2_PROBE, telemetry_schema=TEL_SCHEMA, heat="hot", gpu=7, last_lease=None))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"(?m)^COOLDOWN_UNMET mean=50\.0% ")


@requires_windows_pwsh
class WorkerThreadsReceiptBlockIsReDerivedTests(EvidenceFactory, unittest.TestCase):
    """VENUE-LEG-PLAYBACK-MAX-THREADS-ARG-1 r2b (adversary A1): Test-DvReceiptValid re-derived the worker-thread VERDICT from the hashed run log and summary.json, but
    never compared the receipt's own `workerThreads` block (requested / observed / verdict) to it, so a receipt could state a count the evidence does not carry."""

    PROOF = {"playbackMaxThreadsRequested": 12, "playbackMaxThreadsSmoke": "12", "playbackWorkerThreadsRead": 12}
    START = "playback_smoke.start session=3 position=0 scale_request=1 quality_mode=0 worker_threads=12 start_serial=2 env_force_threads=0"
    BLOCK = {"requested": 12, "observed": 12, "verdict": "HONOURED"}

    def setUp(self) -> None:
        self.make_harness()

    def production(self, cap: int | None, with_block: dict | None):
        spec = self.write_spec()
        data = json.loads(spec.read_text(encoding="utf-8"))
        if cap is not None:
            data["generatorArgs"] = {"playbackMaxThreads": cap}
        spec.write_text(json.dumps(data), encoding="utf-8")
        repo = self.prod_repo(spec=spec)
        ev = self.evidence("wt-" + hashlib.sha1(json.dumps([cap, with_block]).encode()).hexdigest()[:8], backend="cuda",
                           extra_log_lines=[self.START] if cap is not None else [], summary=dict(self.PROOF) if cap is not None else None)
        receipt = self.receipt_for(repo, ev, backend="cuda")
        if with_block is not None:
            receipt["workerThreads"] = dict(with_block)
        return repo, receipt

    def test_a_capped_receipt_whose_block_re_derives_is_advisory(self) -> None:
        repo, receipt = self.production(12, self.BLOCK)
        self.assertAdvisory(receipt, repo)

    def test_a_receipt_that_misstates_the_worker_count_does_not_re_derive(self) -> None:
        for name, block in (("count and verdict", {"requested": 16, "observed": 16, "verdict": "HONOURED"}),
                            ("observed only", dict(self.BLOCK, observed=16)),
                            ("requested only", dict(self.BLOCK, requested=16)),
                            ("verdict only", dict(self.BLOCK, verdict="NOT_HONOURED")),
                            ("observed erased", dict(self.BLOCK, observed=None))):
            with self.subTest(case=name):
                repo, receipt = self.production(12, block)
                self.assertNotValid(receipt, repo, "WORKER_THREADS_NOT_FROM_EVIDENCE", status="INVALID")

    def test_a_capped_receipt_with_no_block_is_incomplete(self) -> None:
        repo, receipt = self.production(12, None)
        self.assertNotValid(receipt, repo, "the receipt carries no workerThreads block", status="INCOMPLETE")

    def test_an_uncapped_leg_is_unchanged_but_cannot_carry_a_block(self) -> None:
        repo, receipt = self.production(None, None)
        self.assertAdvisory(receipt, repo)
        repo, receipt = self.production(None, self.BLOCK)
        self.assertNotValid(receipt, repo, "WORKER_THREADS_NOT_FROM_EVIDENCE", status="INVALID")


class PrepConflatedMoveLegSpecTests(unittest.TestCase):
    """PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1: the ON arm of the #397 prep-conflated-move A/B is a named generatorArgs key; each ON twin differs from the cell it
    twins only in legId and that key, so no receipted cell changes a byte."""

    def setUp(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is required")
        self.jsonschema = jsonschema
        self.schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))

    def test_the_schema_names_the_key_as_the_constant_true(self) -> None:
        self.assertEqual(self.schema["properties"]["generatorArgs"]["properties"]["prepConflatedMove"]["const"], True)

    def test_each_on_twin_differs_from_its_cell_only_in_the_leg_id_and_the_key(self) -> None:
        self.assertEqual(len(PREP_CONFLATED_MOVE_LEGS), 4, "fullscreen and windowed, s2 and s4")
        for rel, cell_rel in PREP_CONFLATED_MOVE_LEGS.items():
            with self.subTest(leg=rel):
                twin = json.loads((DV / rel).read_text(encoding="utf-8"))
                cell = json.loads((DV / cell_rel).read_text(encoding="utf-8"))
                self.jsonschema.validate(twin, self.schema)
                self.assertNotIn("prepConflatedMove", cell["generatorArgs"], cell_rel)
                self.assertEqual(twin["legId"], cell["legId"] + "-pcm")
                self.assertIs(twin["generatorArgs"].pop("prepConflatedMove"), True)
                self.assertEqual(dict(twin, legId=cell["legId"]), cell)
                self.assertEqual(list(twin), list(cell), "the cell's keys in the cell's order")
                self.assertIn("cpu", twin["backends"], "the A/B runs on the CPU route")

    def test_the_key_must_be_the_constant_true_so_an_off_arm_is_the_absent_key(self) -> None:
        twin = json.loads((DV / next(iter(PREP_CONFLATED_MOVE_LEGS))).read_text(encoding="utf-8"))
        for bad in (False, 0, 1, "1", "on", None):
            with self.subTest(value=repr(bad)):
                with self.assertRaises(self.jsonschema.ValidationError):
                    self.jsonschema.validate(dict(twin, generatorArgs=dict(twin["generatorArgs"], prepConflatedMove=bad)), self.schema)

    def test_the_runner_and_the_validator_both_gate_on_the_apps_own_readback(self) -> None:
        module = (DV / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        self.assertIn("$prepConflatedMoveVerdict = Get-DvPrepConflatedMoveEvidence -Spec $spec -LogText $ev.logText", module)
        self.assertIn('if ($prepConflatedMoveVerdict.blocks) { $invalid.Add("PREP_CONFLATED_MOVE_NOT_HONOURED:', module)
        self.assertIn("Get-DvPrepConflatedMoveEvidence", module.split("Export-ModuleMember", 1)[1])
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("$prepConflatedMoveVerdict = Get-DvPrepConflatedMoveEvidence -Spec $spec -LogText $runLogText", runner)
        self.assertIn("PREP_CONFLATED_MOVE_NOT_HONOURED", runner)


@requires_windows_pwsh
class PrepConflatedMoveEvidenceTests(unittest.TestCase):
    """Get-DvPrepConflatedMoveEvidence, pure: a leg that asks for the switch counts only when the measured session's own playback_smoke.prep_route record
    reports prep_conflated_move_active=1. A switch value in the launcher's env block is a request, not engagement (ruling 2026-10-28T15:50:51Z), and the binary
    under test at the time of this card (#397 5fb4b801) emits no such record, so every ON leg on it is NOT_HONOURED rather than an unproven PASS.
    r2 (adversary A1 on #726): the record has a line of its own with no env-derived text, matched as a whole line; the start line is never read for it, because a
    CR/LF in an inherited env value used to become a separator there (tools/repo_hygiene/test_log_line_field_e2e.py runs the real app logging end to end)."""

    ON = {"generatorArgs": {"prepConflatedMove": True}}
    START = "playback_smoke.start session=3 position=0 scale_request=2 quality_mode=0 worker_threads=12 start_serial=2 env_force_threads=0"
    PREFIX = "[2026-10-31T00:00:00.000Z] [INFO] [0x1a2b] "

    def verdict(self, spec: dict, log: str) -> dict:
        proc, out = run_pwsh_json("$s = $env:DVE_SPEC | ConvertFrom-Json\n(Get-DvPrepConflatedMoveEvidence -Spec $s -LogText $env:DVE_LOG) | ConvertTo-Json -Compress",
                                  DVE_SPEC=json.dumps(spec), DVE_LOG=log)
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        return out

    @classmethod
    def log(cls, *records: str, start: str | None = None) -> str:
        return "\n".join(["playback_smoke.measured_session id=3", start or cls.START, *records, "playback_smoke.summary session=3 reason=reached presented_frames=600"]) + "\n"

    @staticmethod
    def route(value: str, session: int = 3) -> str:
        return f"playback_smoke.prep_route session={session} prep_conflated_move_active={value}"

    def test_honoured_only_when_the_measured_session_reports_the_switch_active(self) -> None:
        for name, record in (("bare", self.route("1")), ("crash-forensics prefix", self.PREFIX + self.route("1")),
                             ("prefix and Qt location", self.PREFIX + self.route("1") + " (MainWindow.cpp:25848)")):
            with self.subTest(case=name):
                v = self.verdict(self.ON, self.log(record))
                self.assertEqual((v["requested"], v["observed"], v["verdict"], v["blocks"]), (True, 1, "HONOURED", False))

    def test_the_binary_that_emits_no_field_is_not_engaged(self) -> None:
        v = self.verdict(self.ON, self.log())
        self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), v)
        self.assertIn("ROUTE_NOT_ENGAGED", v["reason"])

    def test_a_start_line_field_is_never_read_as_engagement(self) -> None:
        # r2 (adversary A1): the start line carries launcher env values, so a field there is not the app's decision.
        v = self.verdict(self.ON, self.log(start=self.START + " prep_conflated_move_active=1"))
        self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), v)
        self.assertIn("ROUTE_NOT_ENGAGED", v["reason"])

    def test_any_other_report_blocks_never_passes(self) -> None:
        for name, log in (("switch off", self.log(self.route("0"))),
                          ("not a number", self.log(self.route("yes"))),
                          ("unset text", self.log(self.route("unset"))),
                          ("value with a sign", self.log(self.route("+1"))),
                          ("value with a suffix", self.log(self.route("1x"))),
                          ("a trailing field", self.log(self.route("1") + " extra=1")),
                          ("a no-break space before the value", self.log(self.route("1").replace("active=", "active= "))),
                          ("another session's record", self.log(self.route("1", session=2))),
                          ("two records", self.log(self.route("1"), self.route("1"))),
                          ("off then on", self.log(self.route("0"), self.route("1"))),
                          ("embedded in another record after a separator an old app wrote",
                           self.log(start=self.START + " env_playback_scope_interval_ms=150 | " + self.route("1"))),
                          ("embedded after a forged prefix", self.log(start=self.START + " env_x=150 " + self.PREFIX + self.route("1"))),
                          ("no log", "")):
            with self.subTest(case=name):
                v = self.verdict(self.ON, log)
                self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), v)

    def test_a_leg_that_does_not_ask_is_unchanged_unless_the_app_reports_the_switch_on(self) -> None:
        for spec in ({}, {"generatorArgs": {"unpaced": True}}):
            for name, log in (("old binary, no record", self.log()),
                              ("record off", self.log(self.route("0"))),
                              ("a start-line field is not a record", self.log(start=self.START + " prep_conflated_move_active=1")),
                              ("no log", "")):
                with self.subTest(spec=spec, case=name):
                    v = self.verdict(spec, log)
                    self.assertEqual((v["requested"], v["verdict"], v["blocks"]), (False, "NOT_REQUESTED", False), v)

    def test_the_off_arm_that_inherited_the_switch_from_the_launcher_does_not_count_as_off(self) -> None:
        # adversary r1 A1 (6bd2a183): an absent key emits no removal of MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE, so a launcher that already carries it ran the OFF arm
        # ON; the gate returned NOT_REQUESTED/blocks=false even though the app reported active=1. A leg that does not ask counts only if the app does not say it is on.
        for spec in ({}, {"generatorArgs": {"unpaced": True}}):
            with self.subTest(spec=spec):
                v = self.verdict(spec, self.log(self.route("1")))
                self.assertEqual((v["requested"], v["verdict"], v["blocks"]), (False, "NOT_HONOURED", True), v)
                self.assertIn("PREP_CONFLATED_MOVE_INHERITED", v["reason"])

    def test_a_forged_field_in_an_env_value_never_honours_a_leg(self) -> None:
        # adversary r1 A2 (6bd2a183) and r2 A1: an env value that carries a no-break or em space (or, on a build before r2, a CR/LF) puts
        # prep_conflated_move_active=1 on the start line as a token of its own. The start line is never read for engagement, so none of these counts either way.
        for tail in ("env_force_threads=0 prep_conflated_move_active=1", "env_force_threads=0 prep_conflated_move_active=1",
                     "env_force_threads=0 prep_conflated_move_active=1 env_raw_uint16_prefetch_decoders=unset",
                     "env_playback_scope_interval_ms=150 | prep_conflated_move_active=1"):
            with self.subTest(requested=True, tail=ascii(tail)):
                v = self.verdict(self.ON, self.log(start=self.START + " " + tail))
                self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), v)
                self.assertIn("ROUTE_NOT_ENGAGED", v["reason"])
            with self.subTest(requested=False, tail=ascii(tail)):
                v = self.verdict({}, self.log(start=self.START + " " + tail))
                self.assertEqual((v["verdict"], v["blocks"]), ("NOT_REQUESTED", False), v)
        for name, record in (("duplicate key", self.route("0") + " prep_conflated_move_active=1"),
                             ("a no-break space inside the record", self.route("1").replace(" prep_", " prep_")),
                             ("value with a sign", self.route("+1")),
                             ("value with a suffix", self.route("1x"))):
            with self.subTest(case=name):
                v = self.verdict(self.ON, self.log(record))
                self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), v)

    def test_a_genuine_field_among_ordinary_fields_is_still_honoured(self) -> None:
        v = self.verdict(self.ON, self.log(self.PREFIX + "playback_smoke.display_required session=3 display_required=1", self.PREFIX + self.route("1"),
                                           start=self.START + " env_raw_uint16_prefetch_decoders=unset"))
        self.assertEqual((v["verdict"], v["observed"], v["blocks"]), ("HONOURED", 1, False), v)

    def test_an_unreadable_record_blocks_a_leg_that_does_not_ask(self) -> None:
        for name, log in (("not a number", self.log(self.route("yes"))), ("two records", self.log(self.route("0"), self.route("0"))),
                          ("embedded mention", self.log(start=self.START + " env_x=1 | " + self.route("0")))):
            with self.subTest(case=name):
                v = self.verdict({}, log)
                self.assertEqual((v["verdict"], v["blocks"]), ("NOT_HONOURED", True), v)
                self.assertIn("unreadable", v["reason"])

    def test_a_qt_location_whose_path_holds_spaces_and_parentheses_keeps_the_reported_state(self) -> None:
        # r3 (sol ADV-A1 on #726, CR 6538542792): the suffix class [^()] refused `(C:/build (x)/MainWindow.cpp:25848)`, so a clean OFF record read as
        # unreadable (NOT_HONOURED, blocks) and a clean ON record never honoured.
        # r4 (r3 adversary A1 = Grok G1 on #726): pairing the parentheses from the right refused a path with an UNBALANCED parenthesis, which Windows allows. A
        # record body never holds ` (` (keys are [A-Za-z0-9_]+ and fieldValue maps every space), so the suffix starts at the FIRST ` (` and runs to a closing
        # `:<digits>)` at the end of the line, whatever the path holds, record markers and `) (` included.
        for loc in (" (C:/build (x)/MainWindow.cpp:25848)", " (C:/b (1) (y)/sub dir/MainWindow.cpp:7)", " (MainWindow.cpp:25848)",
                    " (C:/build (x/MainWindow.cpp:25848)", " (C:/build x)/M.cpp:1)", " (C:/a) prep_conflated_move_active=1 (b/MainWindow.cpp:9)",
                    " (C:/build playback_smoke.prep_route session=3 x/MainWindow.cpp:25848)"):
            with self.subTest(loc=loc, arm="OFF"):
                v = self.verdict({}, self.log(self.PREFIX + self.route("0") + loc))
                self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (0, "NOT_REQUESTED", False), v)
            with self.subTest(loc=loc, arm="ON"):
                v = self.verdict(self.ON, self.log(self.PREFIX + self.route("1") + loc))
                self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (1, "HONOURED", False), v)
            with self.subTest(loc=loc, arm="OFF inherited ON"):
                v = self.verdict({}, self.log(self.PREFIX + self.route("1") + loc))
                self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (1, "NOT_HONOURED", True), v)
        for name, record in (("a suffix without a line number", self.route("1") + " (C:/build (x)/MainWindow.cpp)"),
                             ("a field after the suffix", self.route("1") + " (C:/x.cpp:1) extra=1"),
                             ("a suffix with no space before it", self.route("1") + "(C:/x.cpp:1)"),
                             ("an empty path", self.route("1") + " (:1)")):
            with self.subTest(case=name):
                v = self.verdict(self.ON, self.log(record))
                self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (None, "NOT_HONOURED", True), v)

    def test_record_markers_inside_a_qt_location_never_count_or_select_a_record(self) -> None:
        # r4 (r3 adversary A2 on #726): markers were matched on the RAW line, before the location suffix was stripped, so a build path holding
        # `playback_smoke.prep_route session=3` added mentions (four mentions, one record: a clean OFF leg read as unreadable and blocked), and a path holding
        # `playback_smoke.start session=3` or `playback_smoke.measured_session id=9` selected the wrong line or the wrong session. Every dual-venue reader now
        # strips the suffix first and matches markers on the record body only.
        def marked(loc: str, *records: str) -> str:
            return "\n".join(r + loc for r in records) + "\n"
        prep_loc = " (C:/build playback_smoke.prep_route session=3 x/MainWindow.cpp:25848)"
        for value, spec, expected in (("0", {}, (0, "NOT_REQUESTED", False)), ("1", self.ON, (1, "HONOURED", False))):
            with self.subTest(case="prep_route marker in every location", value=value):
                v = self.verdict(spec, marked(prep_loc, "playback_smoke.measured_session id=3", "playback_smoke.start session=3 worker_threads=8",
                                                    self.route(value), "playback_smoke.summary session=3 reason=reached presented_frames=600"))
                self.assertEqual((v["observed"], v["verdict"], v["blocks"]), expected, v)
        with self.subTest(case="measured_session marker in an earlier line's location"):
            log = (self.PREFIX + "interaction_trace event=automation.start (C:/b playback_smoke.measured_session id=9 x/MainWindow.cpp:1)\n"
                   + self.log(self.route("0")))
            v = self.verdict({}, log)
            self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (0, "NOT_REQUESTED", False), v)
        start_loc = " (C:/build playback_smoke.start session=3 worker_threads=2 x/MainWindow.cpp:25848)"
        log = "\n".join(["playback_smoke.measured_session id=3", "playback_smoke.start session=3 worker_threads=8 (C:/a/MainWindow.cpp:1)",
                         self.PREFIX + "interaction_trace event=gui_smoke.visual_state look_assist_enabled=0 (C:/a/MainWindow.cpp:2)",
                         "playback_smoke.summary session=3 reason=reached presented_frames=600 presented_fps=24" + start_loc.replace("worker_threads=2", "presented_fps=999"),
                         "playback_smoke.pace_summary session=3 timeline_fps_after_first_present=59.9" + start_loc
                         + " event=gui_smoke.visual_state look_assist_enabled=1 (y/MainWindow.cpp:3)"]) + "\n"
        proc, out = run_pwsh_json("$s = Get-DvSmokeSessionLineRead -LogText $env:DVE_LOG -Marker 'start'\n"
                                  "$l = Get-DvLookAssistEvidence -Spec (@{ generatorArgs = @{ forceLookAssist = $true } }) -LogText $env:DVE_LOG\n"
                                  "[ordered]@{ workers = $s.fields['worker_threads']; look = $l.reported; rates = (Get-DvMatrixRates -LogText $env:DVE_LOG);"
                                  " summary = (Get-DvSmokeSummaryFields -LogText $env:DVE_LOG).fields['presented_fps'] } | ConvertTo-Json -Compress -Depth 4",
                                  DVE_LOG=log)
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        # workers / look: a marker in a later line's location selects nothing; summary / presentedFps: the summary's location adds no field (r3 advisory A3)
        self.assertEqual({"workers": out["workers"], "look": out["look"], "summary": out["summary"], "presentedFps": out["rates"]["presentedFps"],
                          "timelineFpsAfterFirstPresent": out["rates"]["timelineFpsAfterFirstPresent"]},
                         {"workers": "8", "look": "0", "summary": "24", "presentedFps": 24, "timelineFpsAfterFirstPresent": 59.9})

    # r5 (sol r4 B1 on #726): the writer before r5 built the location with `.arg(file).arg(line)`, so a legal path holding `%1` took the line number and
    # the location ended `:%2)` (`C:/build %1/MainWindow.cpp`, line 25846 -> ` (C:/build 25846/MainWindow.cpp:%2)`). The reader left that suffix in the
    # record body: a clean OFF record read as unreadable, and marker text in the path selected the session, the start line, the visual state and fields.
    OLD_LOC = " (C:/build 25846/MainWindow.cpp:%2)"

    def test_an_old_writer_location_ending_in_an_unsubstituted_marker_keeps_the_route_state(self) -> None:
        for loc in (self.OLD_LOC, " (C:/b 7 (x/sub dir/MainWindow.cpp:%2)"):
            with self.subTest(loc=loc, arm="OFF"):
                v = self.verdict({}, self.log(self.PREFIX + self.route("0") + loc))
                self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (0, "NOT_REQUESTED", False), v)
            with self.subTest(loc=loc, arm="ON"):
                v = self.verdict(self.ON, self.log(self.PREFIX + self.route("1") + loc))
                self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (1, "HONOURED", False), v)
            with self.subTest(loc=loc, arm="OFF inherited ON"):
                v = self.verdict({}, self.log(self.PREFIX + self.route("1") + loc))
                self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (1, "NOT_HONOURED", True), v)

    def test_an_unreadable_location_refuses_the_record_and_never_supplies_a_marker_or_field(self) -> None:
        # r5: a tail that is neither `:<digits>)` nor `:%2)` leaves the body before the first ` (` plus ' (LOCATION_UNREADABLE)': refused, never forged.
        for loc in (" (C:/b x/MainWindow.cpp:%3)", " (C:/b x/MainWindow.cpp:%2) tail", " (C:/b x/MainWindow.cpp:)", " (:%2)",
                    " (C:/b playback_smoke.prep_route session=3 prep_conflated_move_active=1 x/MainWindow.cpp:%1)"):
            for spec, name in ((self.ON, "ON"), ({}, "OFF")):
                with self.subTest(loc=loc, arm=name):
                    v = self.verdict(spec, self.log(self.PREFIX + self.route("0" if name == "OFF" else "1") + loc))
                    self.assertEqual((v["observed"], v["verdict"], v["blocks"]), (None, "NOT_HONOURED", True), v)
        proc, out = run_pwsh_json("@(" + ", ".join(f"(Get-DvLogRecordBody -Line '{line}')" for line in (
            "a=1 (C:/b id=9 x/M.cpp:%2)", "a=1 (C:/b id=9 x/M.cpp:%3)", "a=1 (C:/b id=9 x/M.cpp:12)", "a=1", "a=1 (C:/b id=9 x/M.cpp:12) b=2")) + ") | ConvertTo-Json -Compress")
        self.assertEqual(out, ["a=1", "a=1 (LOCATION_UNREADABLE)", "a=1", "a=1", "a=1 (LOCATION_UNREADABLE)"], proc.stdout + proc.stderr)

    def test_record_markers_inside_an_old_writer_location_never_forge_a_session_or_field(self) -> None:
        # sol r4 B1 class probes: an earlier line's `:%2)` location holding `playback_smoke.measured_session id=9` selected session 9 (the OFF gate then read
        # NOT_REQUESTED despite a genuine session-3 active=1), and `:%2)` locations holding worker_threads=2, look_assist_enabled=1 and presented_fps=999
        # were read as the start line, the visual state and the summary rate.
        session_loc = " (C:/b playback_smoke.measured_session id=9 25846/MainWindow.cpp:%2)"
        for value, spec, expected in (("0", {}, (0, "NOT_REQUESTED", False)), ("1", {}, (1, "NOT_HONOURED", True)), ("1", self.ON, (1, "HONOURED", False))):
            for tail in (session_loc, session_loc + " x", session_loc.replace(":%2)", ")")):
                with self.subTest(case="measured_session marker in an earlier line's location", value=value, requested=bool(spec), tail=tail):
                    v = self.verdict(spec, self.PREFIX + "interaction_trace event=automation.start" + tail + "\n" + self.log(self.route(value)))
                    self.assertEqual((v["observed"], v["verdict"], v["blocks"]), expected, v)
        start_loc = " (C:/build playback_smoke.start session=3 worker_threads=2 25846/MainWindow.cpp:%2)"
        for name, end in (("old writer :%2)", ""), ("unreadable tail", " x")):
            with self.subTest(case=name):
                log = "\n".join(["playback_smoke.measured_session id=3", "playback_smoke.start session=3 worker_threads=8 (C:/a/MainWindow.cpp:1)",
                                 self.PREFIX + "interaction_trace event=gui_smoke.visual_state look_assist_enabled=0 (C:/a/MainWindow.cpp:2)",
                                 "playback_smoke.summary session=3 reason=reached presented_frames=600 presented_fps=24"
                                 + start_loc.replace("worker_threads=2", "presented_fps=999") + end,
                                 "playback_smoke.pace_summary session=3 timeline_fps_after_first_present=59.9" + start_loc
                                 + " event=gui_smoke.visual_state look_assist_enabled=1 (y/MainWindow.cpp:%2)" + end]) + "\n"
                proc, out = run_pwsh_json("$s = Get-DvSmokeSessionLineRead -LogText $env:DVE_LOG -Marker 'start'\n"
                                          "$l = Get-DvLookAssistEvidence -Spec (@{ generatorArgs = @{ forceLookAssist = $true } }) -LogText $env:DVE_LOG\n"
                                          "[ordered]@{ workers = $s.fields['worker_threads']; look = $l.reported; rates = (Get-DvMatrixRates -LogText $env:DVE_LOG);"
                                          " summary = (Get-DvSmokeSummaryFields -LogText $env:DVE_LOG).fields['presented_fps'] } | ConvertTo-Json -Compress -Depth 4",
                                          DVE_LOG=log)
                self.assertIsNotNone(out, proc.stdout + proc.stderr)
                self.assertEqual({"workers": out["workers"], "look": out["look"], "summary": out["summary"], "presentedFps": out["rates"]["presentedFps"],
                                  "timelineFpsAfterFirstPresent": out["rates"]["timelineFpsAfterFirstPresent"]},
                                 {"workers": "8", "look": "0", "summary": "24", "presentedFps": 24, "timelineFpsAfterFirstPresent": 59.9})

    def test_a_long_run_of_open_parentheses_is_read_in_one_pass(self) -> None:
        # r5 (sol r4 H1 on #726): the r4 suffix regex was super-linear on repeated ` (` (10000: ~0.9 s, 30000: ~6.7 s, 100000: more than 8 s). The scan is
        # linear now. No duration is asserted: the payloads below must simply come back, with these bodies, inside the test's process bound.
        script = ("$big = ' (' * 100000\n"
                  "$route = 'playback_smoke.prep_route session=3 prep_conflated_move_active=1'\n"
                  "$log = \"playback_smoke.measured_session id=3`n\" + $route + $big + \"C:/x.cpp:1)`nplayback_smoke.summary session=3 reason=reached presented_frames=600`n\"\n"
                  "[ordered]@{\n"
                  " bare = (Get-DvLogRecordBody -Line $big)\n"
                  " summary = (Get-DvLogRecordBody -Line ('playback_smoke.summary session=3 presented_fps=24' + $big))\n"
                  " located = (Get-DvLogRecordBody -Line ($route + $big + 'x:1)'))\n"
                  " old = (Get-DvLogRecordBody -Line ($route + $big + 'x:%2)'))\n"
                  # Get-DvLogRecordBodies is not exported: called in the module's scope
                  " bodies = [string[]](& (Get-Module DualVenueRunner) { param($t) Get-DvLogRecordBodies -LogText $t } (\"x\" + $big + \"`nplain line`n\"))\n"
                  " verdict = (Get-DvPrepConflatedMoveEvidence -Spec (@{ generatorArgs = @{ prepConflatedMove = $true } }) -LogText $log).verdict\n"
                  "} | ConvertTo-Json -Compress")
        try:
            proc = run_pwsh(["-Command", f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n" + script], timeout=120)
        except subprocess.TimeoutExpired:
            self.fail("Get-DvLogRecordBody did not return on a 100000-repeat ' (' payload within the test's 120 s process bound")
        out = json.loads(proc.stdout) if proc.stdout.strip() else None
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        route = "playback_smoke.prep_route session=3 prep_conflated_move_active=1"
        self.assertEqual(out, {"bare": " (LOCATION_UNREADABLE)", "summary": "playback_smoke.summary session=3 presented_fps=24 (LOCATION_UNREADABLE)",
                               "located": route, "old": route, "bodies": ["x (LOCATION_UNREADABLE)", "plain line", ""], "verdict": "HONOURED"})

    def test_the_shared_line_reader_counts_an_empty_valued_duplicate_for_every_caller(self) -> None:
        # r3 (sol ADV-A2 on #726): `active=1 active=` read as active=1 because the reader skipped an empty value; every caller of the shared reader now sees
        # the key as absent and the duplicate named, and a Qt location suffix adds no token.
        log = "\n".join(["playback_smoke.measured_session id=3", "playback_smoke.pace_unpaced session=3 active=1 drop_frame=0 active=",
                         "playback_smoke.pace_summary session=3 timeline_fps_after_first_present=59.9 TIMELINE_FPS_AFTER_FIRST_PRESENT=",
                         "playback_smoke.cpu_summary session=3 avg_llrawproc_dual_iso_ms=4.5 (C:/b x=1 (y)/MainWindow.cpp:9)",
                         "playback_smoke.summary session=3 reason=reached presented_frames=600"]) + "\n"
        proc, out = run_pwsh_json("$u = Get-DvUnpacedEvidence -Spec (@{ generatorArgs = @{ unpaced = $true } }) -LogText $env:DVE_LOG\n"
                                  "$r = Get-DvSmokeSessionLineRead -LogText $env:DVE_LOG -Marker 'pace_unpaced'\n"
                                  "$c = Get-DvSmokeSessionLineRead -LogText $env:DVE_LOG -Marker 'cpu_summary'\n"
                                  "[ordered]@{ unpaced = $u.verdict; duplicates = @($r.duplicates); rates = (Get-DvMatrixRates -LogText $env:DVE_LOG); cpuKeys = @($c.fields.Keys | Sort-Object) } | ConvertTo-Json -Compress -Depth 4",
                                  DVE_LOG=log)
        self.assertIsNotNone(out, proc.stdout + proc.stderr)
        self.assertEqual(out["unpaced"], "NOT_HONOURED")
        self.assertEqual(out["duplicates"], ["active"])
        self.assertIsNone(out["rates"]["timelineFpsAfterFirstPresent"])
        self.assertEqual(out["rates"]["dualIsoMs"], 4.5)
        self.assertEqual(out["cpuKeys"], ["avg_llrawproc_dual_iso_ms", "session"], "the location suffix's path adds no field")


if __name__ == "__main__":
    unittest.main()
