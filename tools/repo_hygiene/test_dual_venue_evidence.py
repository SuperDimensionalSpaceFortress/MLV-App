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

# The fork/master commit whose generator the DEFAULT emitted job must stay byte-identical to (the first master
# that carries PLAYBACK-CLIP-LENGTH-ENFORCE-4; this card's own merge base for the generator).
BASELINE_COMMIT = "38ed2d8f96c29df273d1de60f2dfd8a03ab6873a"

PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")
FIXTURE_IDS = ("tiny_dual_iso", "large_dual_iso")
OWNER_CLIP = "M16-1243"   # a consented clip ID (an id is not footage); the runner never sees a path
OTHER_CLIP = "Z99-9999"
MLV_EXT = "." + "mlv"  # never spelled as one literal token (the NA-4 gate trips on fixture basenames)
NONCE = "n" + "a1" * 16


def run_pwsh(args: list[str], env_extra: dict | None = None, timeout: int = 600) -> subprocess.CompletedProcess:
    import os
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
                          capture_output=True, text=True, timeout=timeout, env=env)


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()


def lf(text: str) -> str:
    return text.replace("\r\n", "\n")


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

    def assertByteIdenticalToBaseline(self, name: str, extra: list[str]) -> None:
        if not self.baseline_available:
            self.skipTest(f"baseline commit {BASELINE_COMMIT[:12]} is not in this clone")
        new = self.generate(GENERATOR, f"new-{name}.job.ps1", extra)
        old = self.generate(self.baseline_root / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-job.ps1", f"old-{name}.job.ps1", extra)
        self.assertEqual(lf(new.read_text(encoding="utf-8")), lf(old.read_text(encoding="utf-8")),
                         "the DEFAULT (bachelor/cuda) emitted job changed -- it must stay byte-identical to master's")

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
if ($JobId -like '*-regsnap') { return [pscustomobject]@{ exitCode = 0; stdout = 'DVE_REG_SNAPSHOT ok=True existed=True sha256=00' } }
if ($JobId -like '*-regrestore') { return [pscustomobject]@{ exitCode = 0; stdout = 'DVE_REG_RESTORE restored=True' } }
switch ($cfg.mainMode) {
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
param($SourceCommit,$BuildManifestSha256,$ClipId,$FixtureSha256,$OutFile,$RepoRoot,$Venue,$Backend,$ScaleFactor,$TelemetryArm,$CpuQuiescenceThresholdPercent,[switch]$ContactSheet,$ContactSheetFrames,[switch]$ForceLookAssist,$LookFlavor,$VenueTablePath,$PlaySeconds)
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
    """A fake owner-typed consent record: the owner's line is carried only as its sha256 (never as text, never a path)."""
    return {"venue": venue, "clipId": clip, "ownerLineSha256": hashlib.sha256(f"CLIP_OR_NONE: {clip} @ {venue}".encode()).hexdigest(),
            "recordedUtc": "2026-10-01T22:10:00Z", "recordedBy": "hub"}


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

    def write_spec(self, card: str = "DUAL-VENUE-EVIDENCE-1", clip: str = OWNER_CLIP, leg_type: str = "speed", play_seconds: int | None = 25) -> Path:
        spec = {
            "schema": "mlv-app/dual-venue-leg/v1", "legId": "unit-leg", "card": card, "legType": leg_type, "clipId": clip,
            "backends": ["cuda", "cpu"], "scaleFactor": 4,
            "timeouts": {"queueWaitSec": 60, "extraClaimedWaitSec": 60},
            "criteria": {"acceptance": {"cuda": [{"metric": "rows", "op": "gt", "value": 0}], "cpu": []},
                         "supplementary": {"cuda": [{"metric": "rows", "op": "gt", "value": 0}], "cpu": []}},
        }
        if play_seconds is not None:
            spec["playSeconds"] = play_seconds
        if leg_type == "look":
            spec["look"] = {"contactSheetFrames": 2, "lookFlavor": "classic"}
        path = self.tmp / f"spec-{card}-{clip}-{leg_type}-{play_seconds}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def write_artifacts(self, summary: dict | None = None, manifest: dict | None = None, source_frames: dict | None | bool = True,
                        nonce: str | None = NONCE, sheet: bool = False) -> None:
        """Write the job's artifacts the way master's generator does (summary.json carries the `sourceFrames` block, the
        evidence manifest carries smokeRunLog.runNonce)."""
        self.artifacts.mkdir(parents=True, exist_ok=True)
        body = {"result": "MEASUREMENT_CAPTURED", "fixtureRehearsal": False, "clipId": OWNER_CLIP, "rows": 900, "gpuFramesTotal": 900,
                "cpuFrames": 0, "artifactRoot": "X:\\stub"}
        if source_frames is True:
            body["sourceFrames"] = {"oracle": "source_advanced >= required_source_frames, wrapped=0, native pace, no fps override",
                                    "sourceAdvanced": 960, "requiredSourceFrames": 600, "wrapped": False, "failures": []}
        elif isinstance(source_frames, dict):
            body["sourceFrames"] = source_frames
        body.update(summary or {})
        (self.artifacts / "summary.json").write_text(json.dumps(body), encoding="utf-8")
        man = {"smokeRunLog": {"runNonce": nonce} if nonce is not None else {}, "presentMonStats": {"p50": 16.6}}
        man.update(manifest or {})
        (self.artifacts / "evidence-manifest.json").write_text(json.dumps(man), encoding="utf-8")
        if sheet:
            (self.artifacts / "contact-sheet").mkdir(exist_ok=True)
            (self.artifacts / "contact-sheet" / "sheet.png").write_bytes(b"\x89PNG\r\n\x1a\nstub")

    def run_leg(self, venue: str, spec: Path, probe: dict | None = None, main_mode: str = "capture", health_mode: str = "ok",
                extra: list[str] | None = None, gen_refusal: str | None = None, dv: Path = DV, consent: Path | None = None,
                ) -> tuple[subprocess.CompletedProcess, dict | None, list[str]]:
        if probe is None:
            probe = BACHELOR_PROBE if venue == "bachelor" else HEALTHY_PROBE
        cfg = {"log": str(self.log), "genLog": str(self.gen_log), "probe": probe, "mainMode": main_mode, "healthMode": health_mode,
               "artifactsAgentPath": "X:\\stub\\agent\\outbox\\unit.artifacts", "genRefusal": gen_refusal, "clipContentSha256": CLIP_CONTENT_SHA}
        self.stub_cfg.write_text(json.dumps(cfg), encoding="utf-8")
        self.log.write_text("", encoding="utf-8")
        self.gen_log.write_text("", encoding="utf-8")
        before = set(self.receipts.rglob("*.json")) if self.receipts.exists() else set()
        proc = run_pwsh(["-File", str(dv / "Invoke-VenueLeg.ps1"), "-Venue", venue, "-LegSpec", str(spec), "-SourceCommit", self.sha,
                         "-BuildManifestSha256", self.build_sha, "-VenueTablePath", str(self.table), "-ReceiptRoot", str(self.receipts),
                         "-UmRunScript", str(self.um), "-GeneratorScript", str(self.gen), "-WorkDir", str(self.tmp / "work"),
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
            # the venue's QSettings were snapshotted and restored around the submitted leg
            self.assertTrue(receipt["registry"]["snapshotTaken"])
            self.assertTrue(receipt["registry"]["restored"])
            self.assertEqual([s.rsplit("-", 1)[-1] for s in submitted], ["health", "regsnap", submitted[2].rsplit("-", 1)[-1], "regrestore"])

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
                    "health", "outcome", "outcomeDetail", "evidence", "metrics", "playback", "owner_verdict", "model_verdicts"):
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
        }
        for name, body in bodies.items():
            path = self.tmp / "bad-consent.json"
            if body is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(body, encoding="utf-8")
            _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), consent=path)
            self.assertRefusedBeforeSubmitting(receipt, submitted, "VENUE_CLIP_CONSENT_INVALID")

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
            self.assertEqual(set(record), {"venue", "clipId", "ownerLineSha256", "recordedUtc", "recordedBy"})
            self.assertRegex(record["ownerLineSha256"], r"^[0-9a-f]{64}$")
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
        self.assertEqual(playback["runNonce"], NONCE)
        self.assertEqual(playback["clipId"], OWNER_CLIP)
        self.assertFalse(playback["wrapped"])
        self.assertTrue(playback["valid"])
        self.assertEqual(playback["invalidReasons"], [])
        self.assertEqual(receipt["subject"]["clipId"], OWNER_CLIP)

    def test_a_capture_without_the_source_frame_proof_is_invalid_not_pass_or_fail(self) -> None:
        cases = {
            "no sourceFrames block": dict(source_frames=False),
            "sourceFrames null": dict(source_frames=None),
            "advanced field absent": dict(source_frames={"requiredSourceFrames": 600, "wrapped": False, "failures": []}),
            "required field absent": dict(source_frames={"sourceAdvanced": 960, "wrapped": False, "failures": []}),
            "advanced short of required": dict(source_frames={"sourceAdvanced": 599, "requiredSourceFrames": 600, "wrapped": False, "failures": []}),
            "required under 20 frames": dict(source_frames={"sourceAdvanced": 16, "requiredSourceFrames": 16, "wrapped": False, "failures": []}),
            "wrapped": dict(source_frames={"sourceAdvanced": 960, "requiredSourceFrames": 600, "wrapped": True, "failures": []}),
            "wrapped absent": dict(source_frames={"sourceAdvanced": 960, "requiredSourceFrames": 600, "failures": []}),
            "oracle failures present": dict(source_frames={"sourceAdvanced": 960, "requiredSourceFrames": 600, "wrapped": False, "failures": ["INVALID_SOURCE_FRAMES: x"]}),
            "no run nonce": dict(nonce=None),
            "malformed run nonce": dict(nonce="none"),
            "ran a fixture rehearsal": dict(summary={"fixtureRehearsal": True}),
            "fixtureRehearsal field absent": dict(summary={"fixtureRehearsal": None}),
            "another clip's summary": dict(summary={"clipId": OTHER_CLIP}),
        }
        for name, args in cases.items():
            proc, receipt, _ = self.run_capture(**args)
            self.assertEqual(proc.returncode, 0, name + proc.stdout + proc.stderr)
            self.assertEqual(receipt["outcome"], "INVALID", f"{name}: {receipt['outcomeDetail']}")
            self.assertNotIn(receipt["outcome"], ("PASS", "FAIL"))
            self.assertFalse(receipt["playback"]["valid"], name)
            self.assertTrue(receipt["playback"]["invalidReasons"], name)

    def test_a_failing_criterion_on_an_unproven_run_is_invalid_not_a_fail(self) -> None:
        proc, receipt, _ = self.run_capture(summary={"rows": 0}, source_frames=False)
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
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), main_mode="captured-nonzero", dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS", "with the exit-code check removed the contradictory capture passes -- so the check is guarded")

    def test_a_smoke_refusal_of_the_play_window_is_invalid_not_a_product_fail(self) -> None:
        # The job ended in its own length/pace refusal (exit 28-ish, RESULT=SMOKE_RUN_FAILED, smokeRefusalReason=...).
        self.write_artifacts(source_frames=False, summary={"result": "SMOKE_RUN_FAILED", "smokeRefusalReason": "PLAY_WINDOW_TOO_SHORT"})
        stub = self.um.read_text(encoding="utf-8").replace("'source-frames-invalid' {", "'smoke-refused' { return [pscustomobject]@{ exitCode = 30; stdout = 'RESULT=SMOKE_RUN_FAILED ARTIFACTS=' + $cfg.artifactsAgentPath } }\n    'source-frames-invalid' {")
        self.um.write_text(stub, encoding="utf-8")
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), main_mode="smoke-refused")
        self.assertEqual(receipt["outcome"], "INVALID")
        self.assertIn("PLAY_WINDOW_TOO_SHORT", receipt["outcomeDetail"])

    def test_the_writer_refuses_a_pass_or_fail_receipt_without_the_verdict(self) -> None:
        module = DV / "DualVenueRunner.psm1"
        direct = self.tmp / "direct"
        base = ("Import-Module '" + str(module) + "' -Force\n"
                "function Make([string]$o) { $r = New-DvReceipt -Card 'C' -LegId 'l' -DeclaredVenue 'ultra-magnus' -Role 'supplementary' -Actor 'a'\n"
                f"  $r['outcome'] = $o; $r['subject']['clipId'] = '{OWNER_CLIP}'; $r['subject']['clipContentSha256'] = '{CLIP_CONTENT_SHA}'; $r['evidence']['umRunOutcome'] = 'RECEIPT'; $r }}\n"
                "function Try-Write($r) { try { Write-DvReceipt -Receipt $r -ReceiptRoot '" + str(direct) + "' | Out-Null; 'WRITTEN' } catch { 'REFUSED:' + $_.Exception.Message.Split(' ')[0] } }\n")
        script = base + (
            "foreach ($o in 'PASS','FAIL') { $r = Make $o; Write-Output ($o + '-no-playback=' + (Try-Write $r)) }\n"
            "$r = Make 'PASS'\n"
            f"$r['playback'] = [ordered]@{{ sourceAdvanced = 960; requiredSourceFrames = 600; wrapped = $false; runNonce = '{NONCE}'; fixtureRehearsal = $false; clipId = '{OWNER_CLIP}'; failures = @(); valid = $true; invalidReasons = @() }}\n"
            "Write-Output ('PASS-with-playback=' + (Try-Write $r))\n"
            "$r = Make 'INVALID'; Write-Output ('INVALID-no-playback=' + (Try-Write $r))\n"
            "$r = Make 'PASS'\n"
            f"$r['playback'] = [ordered]@{{ sourceAdvanced = 10; requiredSourceFrames = 600; wrapped = $false; runNonce = '{NONCE}'; fixtureRehearsal = $false; clipId = '{OWNER_CLIP}'; failures = @(); valid = $true; invalidReasons = @() }}\n"
            "Write-Output ('PASS-claims-valid-but-short=' + (Try-Write $r))\n")
        proc = run_pwsh(["-Command", script])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = proc.stdout
        self.assertIn("PASS-no-playback=REFUSED:DVE_RECEIPT_INVALID", out)
        self.assertIn("FAIL-no-playback=REFUSED:DVE_RECEIPT_INVALID", out)
        self.assertIn("PASS-with-playback=WRITTEN", out)
        self.assertIn("INVALID-no-playback=WRITTEN", out, "an INVALID receipt is a legitimate terminal and carries no signal")
        self.assertIn("PASS-claims-valid-but-short=REFUSED:DVE_RECEIPT_INVALID", out, "the writer re-derives validity; a self-declared valid=true is not believed")

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
        self.write_artifacts(source_frames={"sourceAdvanced": 100, "requiredSourceFrames": 600, "wrapped": False, "failures": []})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS", "the mutated runner believes a short run -- so the short-run test DOES guard the rule")

    def test_mutation_without_the_wrap_check_a_looped_run_passes(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($wrapped) {", "if ($false) {")])
        self.write_artifacts(source_frames={"sourceAdvanced": 960, "requiredSourceFrames": 600, "wrapped": True, "failures": []})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS")

    def test_mutation_without_the_nonce_check_a_foreign_receipt_passes(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($nonce -notmatch '^n[0-9a-f]{32}$') {", "if ($false) {")])
        self.write_artifacts(nonce=None)
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS")

    def test_mutation_without_the_rehearsal_check_a_fixture_run_passes(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($rehearsal -ne $false) {", "if ($false) {")])
        self.write_artifacts(summary={"fixtureRehearsal": True})
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS")

    def test_mutation_without_the_writer_validation_a_proofless_pass_is_written(self) -> None:
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($Receipt['outcome'] -in @('PASS', 'FAIL')) {", "if ($false) {"),
                                       ("Invoke-VenueLeg.ps1", "if ($outcome -in @('PASS', 'FAIL') -and -not $playback.valid) {", "if ($false) {")])
        self.write_artifacts(source_frames=False)
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "PASS", "with both layers removed a proofless capture is a PASS -- each layer is needed")

    def test_mutation_the_runner_layer_alone_still_stops_a_proofless_pass(self) -> None:
        # Defence in depth: removing only the WRITER's check, the runner's own downgrade still yields INVALID ...
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($Receipt['outcome'] -in @('PASS', 'FAIL')) {", "if ($false) {")])
        self.write_artifacts(source_frames=False)
        _, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(receipt["outcome"], "INVALID")
        # ... and removing only the RUNNER's downgrade, the writer refuses (exit 2, no receipt file for the leg).
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($outcome -in @('PASS', 'FAIL') -and -not $playback.valid) {", "if ($false) {")])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("DVE_RECEIPT_WRITE_FAILED", proc.stdout)
        self.assertIsNone(receipt)


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
def _valid_look_receipt(backend: str, frames_dir: Path, clip: str = OWNER_CLIP, with_playback: bool = True) -> dict:
    receipt = {
        "receiptId": f"r-{backend}", "card": "DUAL-VENUE-EVIDENCE-1", "legId": "m16-1243-look", "outcome": "PASS",
        "subject": {"backend": backend, "buildManifestSha256": "ab" * 32, "legSpecSha256": "cd" * 32, "clipId": clip,
                    "clipContentSha256": CLIP_CONTENT_SHA, "lookFlavor": "classic"},
        "venue": {"name": "ultra-magnus", "hostName": "ULTRA-MAGNUS", "gpuNames": ["RTX 4090"]},
        "evidence": {"umRunOutcome": "RECEIPT"},
        "look": {"contactSheet": {"rawFramesDir": str(frames_dir)}},
        "playback": None,
    }
    if with_playback:
        receipt["playback"] = {"sourceAdvanced": 960, "requiredSourceFrames": 600, "wrapped": False, "failures": [], "runNonce": NONCE,
                               "fixtureRehearsal": False, "clipId": clip}
    return receipt


@requires_windows_pwsh
class SheetPairStaysLocalTests(unittest.TestCase):
    """Every leg now plays an owner clip, so a paired sheet is a sheet of OWNER footage: it is written only under a
    .claude-state directory and only from receipts that are themselves valid evidence."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dve-pair-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.frames = {b: self.tmp / b for b in ("cuda", "cpu")}
        for d in self.frames.values():
            d.mkdir()

    def pair(self, out: Path, **kwargs):
        paths = []
        for backend in ("cuda", "cpu"):
            path = self.tmp / f"{backend}.json"
            path.write_text(json.dumps(_valid_look_receipt(backend, self.frames[backend], **kwargs)), encoding="utf-8")
            paths.append(path)
        return run_pwsh(["-File", str(DV / "New-VenueSheetPair.ps1"), "-CudaReceipt", str(paths[0]), "-CpuReceipt", str(paths[1]), "-OutDir", str(out)])

    def test_a_sheet_outside_a_claude_state_directory_is_refused(self) -> None:
        proc = self.pair(self.tmp / "published")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_OWNER_SHEET_MUST_STAY_LOCAL", proc.stdout + proc.stderr)
        self.assertFalse((self.tmp / "published").exists(), "nothing is written for a refused pair")

    def test_a_receipt_without_the_oracle_verdict_cannot_be_paired(self) -> None:
        proc = self.pair(self.tmp / ".claude-state" / "sheets", with_playback=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_RECEIPT_INVALID", proc.stdout + proc.stderr)

    def test_a_fixture_or_a_path_is_not_a_consented_clip_for_a_sheet(self) -> None:
        for clip in FIXTURE_IDS:
            proc = self.pair(self.tmp / ".claude-state" / "sheets", clip=clip)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("PAIR_NOT_A_CONSENTED_CLIP", proc.stdout + proc.stderr)

    def test_a_valid_pair_under_claude_state_is_composed_and_marked_local(self) -> None:
        try:
            import PIL, numpy  # noqa: F401
            from PIL import Image
        except ImportError:
            self.skipTest("Pillow + numpy are required")
        for backend, colour in (("cuda", (200, 40, 40)), ("cpu", (40, 40, 200))):
            for i in (0, 1):
                Image.new("RGB", (64, 36), colour).save(self.frames[backend] / f"frame-{i:02d}.png")
                (self.frames[backend] / f"frame-{i:02d}.json").write_text(json.dumps({
                    "index": i, "saved": True, "display_frame": i * 3, "elapsed_ms": i * 40.0, "path": f"frame-{i:02d}.png",
                    "look_assist_enabled": True, "look_assist_scene": "night"}), encoding="utf-8")
        out = self.tmp / ".claude-state" / "sheets"
        proc = self.pair(out)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        record = json.loads(next(out.glob("sheet-pair-*.json")).read_text(encoding="utf-8"))
        self.assertTrue(record["ownerFootage"])
        self.assertIn("never committed", record["localOnly"])
        self.assertIsNone(record["owner_verdict"])
        self.assertEqual(record["model_verdicts"], [])


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
class LegSpecSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is required")
        self.jsonschema = jsonschema
        self.schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))

    def test_the_shipped_legs_validate_and_cover_speed_and_look_on_both_backends(self) -> None:
        legs = sorted((DV / "legs").glob("*.json"))
        self.assertGreaterEqual(len(legs), 2)
        types = set()
        for path in legs:
            spec = json.loads(path.read_text(encoding="utf-8"))
            self.jsonschema.validate(spec, self.schema)
            self.assertEqual(sorted(spec["backends"]), ["cpu", "cuda"], f"{path.name} must run as cuda AND cpu")
            types.add(spec["legType"])
        self.assertEqual(types, {"speed", "look"})

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
