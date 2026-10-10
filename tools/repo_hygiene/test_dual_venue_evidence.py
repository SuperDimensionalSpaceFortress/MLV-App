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
BASELINE_COMMIT = "fc12fe8c63f78e5436c11902b3aa0140e271320d"

PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")
FIXTURE_IDS = ("tiny_dual_iso", "large_dual_iso")
OWNER_CLIP = "M16-1243"   # a consented clip ID (an id is not footage); the runner never sees a path
# Every leg spec shipped under legs/ (DVE-SCALE2-LOOK-LEG-1 added the scale-2 look leg); the tracked-spec tests loop over all of them.
# DUAL-VENUE-DISPLAY-MATRIX-1 added the six display-matrix legs ({fullscreen, windowed} x {scale 1, 2, 4}); legsets/display-matrix.json names them.
DISPLAY_MATRIX_LEGS = tuple(f"legs/m16-1243-display-{mode}-s{scale}.json" for scale in (1, 2, 4) for mode in ("fullscreen", "windowed"))
# PLAYBACK-BACHELOR-PRESENT-JITTER-1 added three capture-free PACE legs: speed legs that force Look Assist (cinematic) through generatorArgs and take no
# contact sheet, so no GUI-thread framebuffer grab lands inside the timed Play.
PACE_LEGS = ("legs/m16-1243-pace-cinematic-fullscreen-s4.json", "legs/m16-1243-pace-cinematic-fullscreen-s2.json", "legs/m16-1243-pace-cinematic-windowed-s4.json")
# ... and the lookahead A/B arm: the owner-shape pace leg at MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=3 (the other arm is the pace leg itself, unset).
LOOKAHEAD_LEGS = ("legs/m16-1243-pace-cinematic-fullscreen-s4-la3.json",)
# ... and (r2) the stall-stage diagnostic: the owner-shape pace leg at telemetryArm HEAVY, which keeps the per-frame playback_smoke.frame log
# (LIGHT disables it), so the present that ends a >= 250 ms interval shows its own stage times. Diagnostic only: never a pace number.
HEAVY_LEGS = ("legs/m16-1243-pace-cinematic-fullscreen-s4-heavy.json",)
# PLAYBACK-LJ92-DECODE-THROUGHPUT-1: the K=1 arm of the raw-uint16 prefetch decoder A/B, one twin per leg it pairs with (the other arm is the leg
# itself at the app's default K=2). Each differs from its twin only in its legId and generatorArgs.rawPrefetchDecoders 1.
DECODER_LEGS = {"legs/m16-1243-pace-cinematic-fullscreen-s4-dec1.json": "legs/m16-1243-pace-cinematic-fullscreen-s4.json",
                "legs/m16-1243-pace-cinematic-fullscreen-s4-heavy-dec1.json": "legs/m16-1243-pace-cinematic-fullscreen-s4-heavy.json"}
# LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1 added the scale-2 Cinematic twin of the scale-2 look leg (the Bachelor CPU Classic | Cinematic look pair).
SHIPPED_LEGS = ("legs/m16-1243-speed.json", "legs/m16-1243-look.json", "legs/m16-1243-look-scale2.json", "legs/m16-1243-look-cinematic.json", *DISPLAY_MATRIX_LEGS, *PACE_LEGS,
                *LOOKAHEAD_LEGS, *HEAVY_LEGS, *DECODER_LEGS, "legs/m16-1243-look-scale2-cinematic.json", "legs/m16-1243-look-film.json",
                "legs/m16-1243-look-scale2-film.json", *(f"legs/m16-1243-look-scale2-{flavor}-agxoff.json" for flavor in ("cinematic", "film")))
# LOOK-ASSIST-FILM-FLAVOR-2 r2: the AgX-off twins of the scale-2 Cinematic and Film legs carry a committed look receipt (look-receipts/agx-off.marxml).
AGXOFF_LEGS = {f"m16-1243-look-scale2-{flavor}-agxoff": f"m16-1243-look-scale2-{flavor}" for flavor in ("cinematic", "film")}
LOOK_RECEIPT_BASE = "6b6f66f52d5344a97de5068b2e7cae1a61edf295"   # the #328 head whose generator, legs and runner this round extends
SHIPPED_LEGS_PS = ", ".join(f"'{rel}'" for rel in SHIPPED_LEGS)
OTHER_CLIP = "Z99-9999"
MLV_EXT = "." + "mlv"  # never spelled as one literal token (the NA-4 gate trips on fixture basenames)
LAUNCHER = ROOT / "tools" / "profiling" / "run-release-gui-smoke.ps1"


def run_pwsh(args: list[str], env_extra: dict | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


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

    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="dve-gen-")
        cls.tmp = Path(cls._tmp.name)
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

    def generate(self, script: Path, out_name: str, extra: list[str]) -> Path:
        out = self.tmp / out_name
        proc = run_pwsh(["-File", str(script), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32,
                         "-ClipId", FIXTURE_IDS[0], "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo),
                         "-OutFile", str(out), *extra])
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

    @classmethod
    def strip_regions(cls, text: str) -> tuple[str, dict[str, int]]:
        """Remove every bracketed region of either sentinel family; return the kept text and the number of regions per family."""
        families = {"leg-terminals": (cls.LEG_TERMINALS_OPEN, cls.LEG_TERMINALS_CLOSE), "presentmon-evidence": (cls.PRESENTMON_EVIDENCE_OPEN, cls.PRESENTMON_EVIDENCE_CLOSE),
                    "orphan-sweep": (cls.ORPHAN_SWEEP_OPEN, cls.ORPHAN_SWEEP_CLOSE),
                    "contact-sheet-parity": (cls.CONTACT_SHEET_PARITY_OPEN, cls.CONTACT_SHEET_PARITY_CLOSE),
                    "keepalive-hung-probe": (cls.KEEPALIVE_HUNG_PROBE_OPEN, cls.KEEPALIVE_HUNG_PROBE_CLOSE),
                    "session-locked": (cls.SESSION_LOCKED_OPEN, cls.SESSION_LOCKED_CLOSE)}
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

    def test_the_runner_passes_a_speed_legs_forced_look_to_the_generator(self) -> None:
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("if (-not $isLook -and $spec.generatorArgs.PSObject.Properties['forceLookAssist'] -and [bool]$spec.generatorArgs.forceLookAssist) {", runner)
        self.assertIn("$gen['ForceLookAssist'] = $true; $gen['LookPaceLeg'] = $true", runner)
        self.assertIn("if ($spec.generatorArgs.PSObject.Properties['lookFlavor']) { $gen['LookFlavor'] = [string]$spec.generatorArgs.lookFlavor }", runner)
        self.assertIn("if ($spec.generatorArgs.PSObject.Properties['playbackRenderLookaheadFrames']) { $gen['PlaybackRenderLookaheadFrames'] = [int]$spec.generatorArgs.playbackRenderLookaheadFrames }", runner)

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
        for extra, token in ((["-Backend", "cpu", "-DisablePaintPerSubmit"], "DUAL_VENUE_CPU_BACKEND_CONFLICT"),
                             (["-ForceLookAssist"], "DUAL_VENUE_LOOK_REQUIRES_CONTACT_SHEET"),
                             (["-LookPaceLeg"], "DUAL_VENUE_LOOK_PACE_LEG_SHAPE"),
                             (["-ForceLookAssist", "-ContactSheet", "-LookPaceLeg"], "DUAL_VENUE_LOOK_PACE_LEG_SHAPE"),
                             (["-PlaybackRenderLookaheadFrames", "3"], "DUAL_VENUE_LOOKAHEAD_NEEDS_LOOK_PACE_LEG")):
            out = self.tmp / "refused.job.ps1"
            proc = run_pwsh(["-File", str(GENERATOR), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32,
                             "-ClipId", FIXTURE_IDS[0], "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), *extra])
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
        # ... and a play window under 20 s never binds.
        proc = run_pwsh(["-File", str(GENERATOR), "-SourceCommit", self.head, "-BuildManifestSha256", "ab" * 32, "-ClipId", FIXTURE_IDS[0],
                         "-FixtureSha256", "cd" * 32, "-RepoRoot", str(self.repo), "-OutFile", str(out), "-PlaySeconds", "19"])
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
param($SourceCommit,$BuildManifestSha256,$ClipId,$FixtureSha256,$OutFile,$RepoRoot,$Venue,$Backend,$ScaleFactor,$TelemetryArm,$CpuQuiescenceThresholdPercent,[switch]$ContactSheet,$ContactSheetFrames,[switch]$ForceLookAssist,$LookFlavor,$VenueTablePath,$PlaySeconds,$ExpectedScaleRequest,$DisplayMode)
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


def consent_file(*records: dict) -> dict:
    return {"schema": "mlv-app/dual-venue-clip-consent/v1", "records": list(records)}


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

    def write_consent(self, *records: dict) -> None:
        self.consent.write_text(json.dumps(consent_file(*records)), encoding="utf-8")

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
                        exact_summary: dict | None = None, extra_log_lines: list[str] | None = None) -> None:
        """Write the job's artifacts the way master's generator does, INCLUDING the run's own records the receipt is re-derived
        from: the launcher's result.json (evidence.runNonce = the nonce it generated, the sha256 of the log snapshot) and
        logs/smoke-run.log (the app's playback_smoke.summary line carrying source_advanced / required_source_frames / native_fps /
        pace_fps / fps_override / wrapped / wrap_count and the run_nonce it echoed). `line` overrides summary-line fields;
        `nonce` is the launcher's (expected) nonce, `observed_nonce` what the app echoed (True = the same one, None = none)."""
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

    def test_a_window_under_20_seconds_is_refused_before_submission(self) -> None:
        for window in (1, 19):
            _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(play_seconds=window))
            self.assertEqual(submitted, [])
            self.assertEqual(self.generator_calls(), [])
            self.assertEqual(receipt["refusal"], "PLAY_WINDOW_TOO_SHORT")

    def test_mutation_without_the_window_floor_a_one_second_play_reaches_the_venue(self) -> None:
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($playSeconds -lt 20) {", "if ($false) {")])
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
        for native, floor in ((24.0, 480), (23.976, 480), (25.0, 500), (30.0, 600), (60.0, 1200)):
            ok = ps_problems(good_block(nativeFps=native, paceFps=native, requiredSourceFrames=floor, sourceAdvanced=floor + 10, jobRequiredSourceFrames=floor, jobSourceAdvanced=floor + 10))
            self.assertEqual(ok, [], f"native {native}: {floor} frames is exactly the floor")
            short = ps_problems(good_block(nativeFps=native, paceFps=native, requiredSourceFrames=floor - 1, sourceAdvanced=floor + 10, jobRequiredSourceFrames=floor - 1, jobSourceAdvanced=floor + 10))
            self.assertTrue(any("ceil(20 s x native_fps" in p for p in short), f"native {native}: {floor - 1} frames is under 20 s: {short}")

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
        anchor = "elseif ($null -ne $native -and $native -gt 0 -and [Math]::Abs($pace - $native) -gt (0.005 * $native)) {"
        mutated = self.mutated_module([(anchor, "elseif ($false) {")])
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

    def __init__(self, test: unittest.TestCase, records: list[dict], cleanup_gone: bool = False, mutations: list[tuple[str, str, str]] | None = None) -> None:
        self.test = test
        tmp = tempfile.TemporaryDirectory(prefix="dve-prod-")
        test.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "repo"
        self.dv = self.root / "tools" / "profiling" / "dual-venue"
        shutil.copytree(DV, self.dv)
        for name, old, new in mutations or []:
            path = self.dv / name
            text = path.read_text(encoding="utf-8")
            test.assertEqual(text.count(old), 1, f"mutation anchor must occur exactly once in {name}: {old!r}")
            path.write_text(text.replace(old, new), encoding="utf-8")
        table = json.loads((self.dv / "venues.json").read_text(encoding="utf-8"))
        table["ownerFootage"]["cleanupClassGone"] = cleanup_gone
        (self.dv / "venues.json").write_text(json.dumps(table, indent=2) + "\n", encoding="utf-8", newline="\n")
        self.write_consent(*records)
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

    def write_consent(self, *records: dict) -> None:
        (self.dv / "venue-clip-consent.json").write_text(json.dumps(consent_file(*records), indent=2) + "\n", encoding="utf-8", newline="\n")

    def blob(self, name: str) -> str:
        return self.git("rev-parse", f"HEAD:tools/profiling/dual-venue/{name}")

    def receipts(self) -> list[dict]:
        root = self.root / ".claude-state" / "dual-venue" / "receipts"
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(root.rglob("*.json"), key=lambda f: f.stat().st_mtime_ns)] if root.exists() else []

    def run(self, spec: Path, sha: str, build_sha: str, venue: str = "ultra-magnus", extra: list[str] | None = None) -> subprocess.CompletedProcess:
        return run_pwsh(["-File", str(self.dv / "Invoke-VenueLeg.ps1"), "-Venue", venue, "-LegSpec", str(spec), "-SourceCommit", sha,
                         "-BuildManifestSha256", build_sha, *(extra or [])])


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
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if (-not [string]::IsNullOrWhiteSpace($SheetCopyDir) -and -not (Test-DvUnderClaudeState -Path $SheetCopyDir)) {", "if ($false) {")])
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
                  spec: Path | None = None, mutations: list[tuple[str, str, str]] | None = None) -> ProductionRepo:
        recs = [consent_record(venue, OWNER_CLIP)] if records is None else records
        repo = ProductionRepo(self, records=recs, cleanup_gone=cleanup_gone, mutations=mutations)
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
        repo = self.prod_repo()
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
                                   "tools/profiling/dual-venue/New-VenueFlavorPair.ps1"})
        self.assertEqual(self.readers_without_repo_root({n: texts[n] for n in callers}), [])
        # nothing else in the tree reads a receipt file: a new reader must call the validator (and be added to this list in review)
        readers = {n for n, t in texts.items() if ("dual-venue-receipt/v1" in t or "dual-venue\\receipts" in t or "dual-venue/receipts" in t)
                   and n not in ("tools/repo_hygiene/test_dual_venue_evidence.py", "tools/repo_hygiene/test_mlv_never_authorized.py")}
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
        for rel in SHIPPED_LEGS:
            tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "--error-unmatch", "tools/profiling/dual-venue/" + rel], capture_output=True, text=True)
            self.assertEqual(tracked.returncode, 0, rel)
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
        self.assertTrue(windowed.startswith("| cuda-windowed-s2 | cuda | windowed / windowed | 2->1 | 2560x1529 (2380x1373) | 3/3 | 12.00 (11.80-12.10) | 19.40 | 28.5 | 25.0 | PASS x3 |"), windowed)
        cpu = lines[3]
        self.assertIn("| 1/2 | 4.00 (4.00-4.00) |", cpu, "one measurement of two runs; the INVALID repeat is counted in 'ran' and shows no numbers")
        self.assertTrue(cpu.endswith("INVALID x1, PASS x1 |"), cpu)

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
        self.assertIn("| cuda-windowed-s1 | cuda | windowed / windowed | 1->1 | 2560x1529 (2380x1373) | 2/2 | 12.60 (12.60-12.60) | 19.40 | 28.5 | 25.5 | PASS x2 |", text)
        self.assertIn("| cuda-fullscreen-s4 | cuda | fullscreen / fullscreen | 4->4 | 2560x1600 (2560x1600) | 2/2 | 12.40 (12.40-12.40) | 19.40 | 28.5 |", text)
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

    def gate(self, probe: dict) -> subprocess.CompletedProcess:
        import os
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(self.dv / "Wait-VenueQuiet.ps1"), "-Venue", "bachelor",
                               "-WorkDir", str(self.work), "-MaxWaitSec", "1", "-RecheckSec", "1"],
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
                         sorted(["m16-1243-pace-cinematic-fullscreen-s4", "m16-1243-pace-cinematic-fullscreen-s2", "m16-1243-pace-cinematic-windowed-s4"]))

    def test_the_lookahead_leg_is_the_owner_shape_pace_leg_at_depth_3(self) -> None:
        """PLAYBACK-BACHELOR-PRESENT-JITTER-1: the lookahead A/B's depth-3 arm differs from the owner-shape pace leg only in its legId and
        generatorArgs.playbackRenderLookaheadFrames."""
        for rel in LOOKAHEAD_LEGS:
            arm = json.loads((DV / rel).read_text(encoding="utf-8"))
            self.jsonschema.validate(arm, self.schema)
            base = json.loads((DV / "legs" / "m16-1243-pace-cinematic-fullscreen-s4.json").read_text(encoding="utf-8"))
            self.assertEqual(arm["generatorArgs"].pop("playbackRenderLookaheadFrames"), 3, rel)
            self.assertEqual(dict(arm, legId=base["legId"]), base, rel)

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
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertRegex(spec["legId"], r"^m16-1243-display-(fullscreen|windowed)-s[124]$", path.name)
            elif spec["card"] == "PLAYBACK-BACHELOR-PRESENT-JITTER-1":   # the capture-free pace legs, named by flavor and cell
                self.assertEqual(spec["legId"], path.stem, path.name)
                self.assertRegex(spec["legId"], rf"^m16-1243-pace-{flavor}-(fullscreen|windowed)-s[124](-la[0-3]|-heavy)?(-dec[0-4])?$", path.name)
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
        spec = json.loads(next(iter(sorted((DV / "legs").glob("*speed*.json")))).read_text(encoding="utf-8"))
        for override in ({"clipId": FIXTURE_IDS[0]}, {"clipId": FIXTURE_IDS[1]}, {"clipId": "C:/footage/" + OWNER_CLIP},
                         {"playSeconds": 19}, {"clipPath": "C:/x"}):
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
        self.gen.write_text(STUB_GENERATOR.replace("$DisplayMode)", "$DisplayMode,$LookReceiptPath)", 1), encoding="utf-8")
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

    def test_a_receipt_off_a_look_leg_is_refused_before_anything_is_emitted(self) -> None:
        for name, extra in (("speed", []), ("pace", ["-ForceLookAssist", "-LookPaceLeg"])):
            with self.subTest(leg=name):
                proc, out = self.run_generator(GENERATOR, f"refused-{name}.job.ps1", [*extra, "-LookReceiptPath", str(self.receipt)])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("DUAL_VENUE_LOOK_RECEIPT_REQUIRES_LOOK_LEG", proc.stdout + proc.stderr)
                self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
