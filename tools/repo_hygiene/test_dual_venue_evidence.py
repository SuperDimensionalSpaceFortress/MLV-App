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
BASELINE_COMMIT = "a29a1ee4325423c156c5bde13581a5de233851ae"

PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")
FIXTURE_IDS = ("tiny_dual_iso", "large_dual_iso")
OWNER_CLIP = "M16-1243"   # a consented clip ID (an id is not footage); the runner never sees a path
# Every leg spec shipped under legs/ (DVE-SCALE2-LOOK-LEG-1 added the scale-2 look leg); the tracked-spec tests loop over all of them.
SHIPPED_LEGS = ("legs/m16-1243-speed.json", "legs/m16-1243-look.json", "legs/m16-1243-look-scale2.json")
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

    @classmethod
    def strip_regions(cls, text: str) -> tuple[str, dict[str, int]]:
        """Remove every bracketed region of either sentinel family; return the kept text and the number of regions per family."""
        families = {"leg-terminals": (cls.LEG_TERMINALS_OPEN, cls.LEG_TERMINALS_CLOSE), "presentmon-evidence": (cls.PRESENTMON_EVIDENCE_OPEN, cls.PRESENTMON_EVIDENCE_CLOSE),
                    "orphan-sweep": (cls.ORPHAN_SWEEP_OPEN, cls.ORPHAN_SWEEP_CLOSE),
                    "contact-sheet-parity": (cls.CONTACT_SHEET_PARITY_OPEN, cls.CONTACT_SHEET_PARITY_CLOSE)}
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
        self.assertEqual(old_counts["orphan-sweep"], 0, "the baseline has none")
        self.assertEqual(new_counts["contact-sheet-parity"], self.CONTACT_SHEET_PARITY_REGIONS, "the default job carries exactly the pinned number of bracketed CONTACT-SHEET-PLAYBACK-PARITY-1 regions")
        self.assertEqual(old_counts["contact-sheet-parity"], 0, "the baseline has none")
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
        self.assertIn("lookFlavorHonored = $(if ($LookLeg) { 'unknown' } else { $null })", text)
        self.assertNotIn("--no-look-assist", text)

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
                             (["-ForceLookAssist"], "DUAL_VENUE_LOOK_REQUIRES_CONTACT_SHEET")):
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
param($SourceCommit,$BuildManifestSha256,$ClipId,$FixtureSha256,$OutFile,$RepoRoot,$Venue,$Backend,$ScaleFactor,$TelemetryArm,$CpuQuiescenceThresholdPercent,[switch]$ContactSheet,$ContactSheetFrames,[switch]$ForceLookAssist,$LookFlavor,$VenueTablePath,$PlaySeconds,$ExpectedScaleRequest)
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
Add-Content -LiteralPath $cfg.genLog -Value (($PSBoundParameters.Keys | Sort-Object | ForEach-Object { $_ + '=' + $PSBoundParameters[$_] }) -join ';')
if ($cfg.genRefusal) { throw $cfg.genRefusal }
Set-Content -LiteralPath $OutFile -Value "# stub job Venue=$Venue Backend=$Backend Look=$ForceLookAssist"
[pscustomobject]@{ outFile = $OutFile; recommendedJobTimeoutSec = 600; smokeRunnerClosureDirName = 'smoke-runner-stub'
                   clipContentSha256 = $cfg.clipContentSha256; playSeconds = $PlaySeconds; fixtureRehearsal = $false }
"""

HEALTHY_PROBE = {"pwshColdStartMs": 500, "smallHashMs": 40, "freeDiskGiB": 600.0, "commitUsedGiB": 40.0, "commitLimitGiB": 128.0,
                 "hostName": "ULTRA-MAGNUS", "gpuNames": ["NVIDIA GeForce RTX 4090"], "driverVersion": "32.0.1", "displayDevice": "\\\\.\\DISPLAY2",
                 "presentmonSha256": "9b" * 32}
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
                   flavor: str = "classic", scale: int = 4, accepted: dict | None = None, leg_id: str = "unit-leg") -> Path:
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
        suffix = (("" if flavor == "classic" else f"-{flavor}") + ("" if scale == 4 else f"-s{scale}") + ("" if leg_id == "unit-leg" else f"-{leg_id}")
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
        The app reports no flavor today (no reader of MLVAPP_LOOK_ASSIST_FLAVOR, no flavor field in look_assist.apply.result), so the
        receipt of a leg asking for cinematic (a synthetic spec here; none is shipped until LOOK-ASSIST-FLAVORS-1) must stay 'unknown'
        -- never True, never a string that echoes the spec."""
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
                    self.assertRegex(line, r"'unknown'|\$null", f"{name}: lookFlavorHonored must be 'unknown' (or null off a look leg), never derived from the spec: {line.strip()}")

    def test_tripwire_the_app_has_no_reader_of_the_flavor_env_var_so_honoured_stays_unknown(self) -> None:
        """If this fails, LOOK-ASSIST-FLAVORS-1 (or equivalent) landed an app-side reader: make the job record the flavor the app REPORTS
        (a field in look_assist.apply.result / the visual-state telemetry) and set lookFlavorHonored from it, then update this test."""
        needle = "MLVAPP_LOOK_ASSIST_FLAVOR"
        hits = []
        for sub in ("platform", "src"):
            for path in (ROOT / sub).rglob("*"):
                if path.suffix.lower() in (".cpp", ".h", ".hpp", ".cu", ".c", ".mm") and needle in path.read_text(encoding="utf-8", errors="replace"):
                    hits.append(str(path.relative_to(ROOT)))
        self.assertEqual(hits, [], "the app now reads the flavor env var; wire lookFlavorHonored to what it reports")


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
        path = Path(tmp.name) / "DualVenueRunner.psm1"
        path.write_text(text, encoding="utf-8")
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
        self.assertEqual(callers, {"tools/profiling/dual-venue/DualVenueRunner.psm1", "tools/profiling/dual-venue/New-VenueSheetPair.ps1"})
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

    def test_the_hand_listed_shipped_legs_are_exactly_the_legs_directory(self) -> None:
        self.assertEqual(sorted(SHIPPED_LEGS), sorted("legs/" + p.name for p in (DV / "legs").glob("*.json")), "SHIPPED_LEGS drifted from the legs/ directory")

    def test_no_shipped_leg_asks_for_a_non_classic_flavor_whatever_its_file_name(self) -> None:
        """The app has no reader of the flavor yet (LOOK-ASSIST-FLAVORS-1), so a spec labelled cinematic would render Classic under a cinematic label
        (fable H1, sol hardening: the old check named one file). Every file in legs/ is checked, not one name."""
        for path in sorted((DV / "legs").glob("*.json")):
            spec = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual((spec.get("look") or {}).get("lookFlavor", "classic"), "classic", f"{path.name} asks for a non-classic flavor the app cannot apply")
            self.assertNotRegex(path.read_text(encoding="utf-8").lower(), r"cinematic", f"{path.name} mentions the cinematic flavor")

    def test_the_scale2_look_leg_differs_from_the_classic_leg_only_where_it_must(self) -> None:
        load = lambda name: json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8"))
        classic, scale2 = load("m16-1243-look"), load("m16-1243-look-scale2")
        for spec in (classic, scale2):
            self.jsonschema.validate(spec, self.schema)
        self.assertEqual(classic["look"]["lookFlavor"], "classic")
        self.assertEqual(scale2["look"]["lookFlavor"], "classic", "no cinematic leg ships until the app can apply a flavor (LOOK-ASSIST-FLAVORS-1)")
        self.assertFalse((DV / "legs" / "m16-1243-look-cinematic.json").exists(), "a cinematic spec would be labelled cinematic and render Classic")
        self.assertEqual(scale2["legId"], "m16-1243-look-scale2")
        self.assertEqual(scale2["scaleFactor"], 2)
        self.assertEqual(classic["scaleFactor"], 4)
        self.assertEqual(scale2["backends"], ["cpu"], "the CUDA texture route clamps scale 2 to 1, so the scale-2 leg is cpu-only until it honours scale 2")
        self.assertNotIn("acceptedEffectiveScale", scale2, "a leg that wants scale 2 declares no accepted clamp: a CUDA run at 1 must not pass it")
        self.assertEqual(sorted(scale2["criteria"]["acceptance"]), ["cpu"], "a cpu-only leg carries no criteria for a backend it cannot run")
        classic_cpu_only = json.loads(json.dumps(classic))
        del classic_cpu_only["acceptedEffectiveScale"]
        for role in classic_cpu_only["criteria"].values():
            role.pop("cuda")
        comparable = dict(scale2, legId=classic["legId"], scaleFactor=classic["scaleFactor"], backends=classic["backends"])
        self.assertEqual(comparable["criteria"], classic_cpu_only["criteria"])
        comparable["criteria"] = classic["criteria"]
        comparable["acceptedEffectiveScale"] = classic["acceptedEffectiveScale"]
        self.assertEqual(comparable, classic, "the scale-2 leg is the Classic leg except legId, scaleFactor, backends (and the CUDA criteria and clamp declaration that go with a CUDA backend)")
        self.assertNotEqual(classic["legId"], scale2["legId"])

    def test_the_cuda_texture_route_clamp_is_declared_by_every_leg_that_runs_cuda_at_a_scale_other_than_1(self) -> None:
        """TRIPWIRE: MainWindowGpuPreviewPolicy.h clamps every requested scale != 1 to 1 on the GPU texture route. While that is true, a leg that names scale S != 1
        and lists cuda must DECLARE the effective scale it renders at (acceptedEffectiveScale.cuda == 1): the receipt then says requested S / rendered 1 instead of a
        silent mismatch. If this fails because the clamp was removed, drop the declarations and let the CUDA backend of the scale-2 leg back in (and update the docs)."""
        policy = (ROOT / "platform" / "qt" / "MainWindowGpuPreviewPolicy.h").read_text(encoding="utf-8")
        self.assertIn("if (requestedScale != 1 && gpuPlaybackReconTextureRouteEligibleAtScaleOne)", policy, "the clamp changed: revisit the legs' acceptedEffectiveScale and the scale-2 leg's cpu-only backends")
        for path in sorted((DV / "legs").glob("*.json")):
            spec = json.loads(path.read_text(encoding="utf-8"))
            if "cuda" in spec["backends"] and spec["scaleFactor"] != 1:
                self.assertEqual(spec.get("acceptedEffectiveScale", {}).get("cuda"), 1, f"{path.name}: names scale {spec['scaleFactor']} on cuda without declaring the clamp")

    def test_the_docs_make_no_scale_claim_the_cuda_route_cannot_keep_and_list_every_typed_outcome(self) -> None:
        doc = (ROOT / "docs" / "dual-venue-evidence.md").read_text(encoding="utf-8")
        self.assertNotIn("it is the leg that shows the owner's playback look", doc, "the scale-2 look is only reachable where the effective scale is 2 (cpu)")
        self.assertIn("`m16-1243-look-scale2` is cpu only", doc)
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

    def test_a_look_leg_without_a_look_block_and_an_unknown_backend_are_rejected(self) -> None:
        spec = json.loads(next(iter(sorted((DV / "legs").glob("*look*.json")))).read_text(encoding="utf-8"))
        bad = dict(spec)
        del bad["look"]
        with self.assertRaises(self.jsonschema.ValidationError):
            self.jsonschema.validate(bad, self.schema)
        bad = dict(spec, backends=["cuda", "vulkan"])
        with self.assertRaises(self.jsonschema.ValidationError):
            self.jsonschema.validate(bad, self.schema)


if __name__ == "__main__":
    unittest.main()
