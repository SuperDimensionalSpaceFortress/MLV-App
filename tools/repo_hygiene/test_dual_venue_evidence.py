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
    """A FAKE owner-typed consent record (tests only): the owner's exact line ("CLIP <venue>: <clip id>", no path) and its sha256."""
    line = f"CLIP {venue}: {clip}"
    return {"venue": venue, "clipId": clip, "ownerLine": line, "ownerLineSha256": hashlib.sha256(line.encode()).hexdigest(),
            "recordedUtc": "2026-10-01T22:10:00Z", "recordedBy": "owner"}


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
                        nonce: str | None = NONCE, sheet: bool = False, line: dict | None = None, observed_nonce: str | None | bool = True,
                        manifest_nonce: str | None | bool = True, log: bool = True, result: bool = True, isolated: str | None = "run_scoped",
                        declared_log_sha: str | None = None, compose_marker: str | None = None, raw_frames: bool = False) -> None:
        """Write the job's artifacts the way master's generator does, INCLUDING the run's own records the receipt is re-derived
        from: the launcher's result.json (evidence.runNonce = the nonce it generated, the sha256 of the log snapshot) and
        logs/smoke-run.log (the app's playback_smoke.summary line carrying source_advanced / required_source_frames / native_fps /
        pace_fps / fps_override / wrapped / wrap_count and the run_nonce it echoed). `line` overrides summary-line fields;
        `nonce` is the launcher's (expected) nonce, `observed_nonce` what the app echoed (True = the same one, None = none)."""
        self.artifacts.mkdir(parents=True, exist_ok=True)
        for stale in ("logs/smoke-run.log", "result.json", "contact-sheet/sheet.png", "contact-sheet/compose-status.txt"):
            (self.artifacts / stale).unlink(missing_ok=True)   # a case that omits a record must not inherit the previous case's
        fields = {"source_advanced": 960, "required_source_frames": 600, "native_fps": "23.976", "pace_fps": "23.976", "fps_override": 0,
                  "wrapped": 0, "wrap_count": 0}
        fields.update(line or {})
        observed = nonce if observed_nonce is True else observed_nonce
        summary_line = ("playback_smoke.summary session=3 reason=play-stop elapsed_ms=30000.000 presented_frames=900 "
                        + " ".join(f"{k}={v}" for k, v in fields.items() if v is not None)
                        + (f" run_nonce={observed}" if observed is not None else ""))
        log_lines = []
        if isolated is not None:
            log_lines.append(f"interaction_trace event=automation.pacing_isolated site=gui-smoke-entry persisted_fps_override=0 persisted_frame_rate=24.000 drop_frame=1 settings_store={isolated}")
        # a DECOY earlier session with another nonce: the measured session (the marker) is the one that counts
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

    def run_leg(self, venue: str, spec: Path, probe: dict | None = None, main_mode: str = "capture", health_mode: str = "ok",
                extra: list[str] | None = None, gen_refusal: str | None = None, dv: Path = DV, consent: Path | None = None,
                token: tuple[str, int] | None = None) -> tuple[subprocess.CompletedProcess, dict | None, list[str]]:
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
            self.assertEqual(record["ownerLine"], f"CLIP {record['venue']}: {record['clipId']}")
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
                f"  $r['outcome'] = $o; $r['subject']['clipId'] = '{OWNER_CLIP}'; $r['subject']['clipContentSha256'] = '{CLIP_CONTENT_SHA}'; $r['evidence']['umRunOutcome'] = 'RECEIPT'\n"
                "  $r['admission'] = [ordered]@{ mode = 'production'; consentBlobSha = ('c' * 40); venueTableBlobSha = ('d' * 40); ownerLineSha256 = ('e' * 64) }; $r }\n"
                "function Try-Write($r) { try { Write-DvReceipt -Receipt $r -ReceiptRoot '" + str(direct) + "' | Out-Null; 'WRITTEN' } catch { 'REFUSED:' + $_.Exception.Message.Split(' ')[0] } }\n")
        script = base + (
            "foreach ($o in 'PASS','FAIL') { $r = Make $o; Write-Output ($o + '-no-playback=' + (Try-Write $r)) }\n"
            "$r = Make 'PASS'\n"
            f"$r['playback'] = {PS_GOOD_BLOCK}\n"
            "Write-Output ('PASS-with-playback=' + (Try-Write $r))\n"
            "$r = Make 'INVALID'; Write-Output ('INVALID-no-playback=' + (Try-Write $r))\n"
            "$r = Make 'PASS'\n"
            f"$r['playback'] = {PS_GOOD_BLOCK.replace('sourceAdvanced = 960', 'sourceAdvanced = 10')}\n"
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
        # ... and removing only the RUNNER's downgrade, the writer refuses (exit 2, no receipt file for the leg).
        mutated = self.mutated_runner([("Invoke-VenueLeg.ps1", "if ($outcome -in @('PASS', 'FAIL') -and $proofProblems.Count -gt 0) {", "if ($false) {")])
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


def ps_receipt_valid(receipt: dict, allow_offline: bool = False, module: Path | None = None) -> tuple[bool, list[str]]:
    flag = " -AllowOfflineTestMode" if allow_offline else ""
    proc = _ps_json(f"$r = $env:DVE_RECEIPT | ConvertFrom-Json\n$v = Test-DvReceiptValid -Receipt $r{flag}\nWrite-Output ('VALID=' + $v.valid)\n$v.reasons | ForEach-Object {{ Write-Output ('REASON=' + $_) }}\n",
                    module or DV / "DualVenueRunner.psm1", {"DVE_RECEIPT": json.dumps(receipt)})
    if proc.returncode != 0:
        raise AssertionError(proc.stdout + proc.stderr)
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    return lines[0] == "VALID=True", [l[len("REASON="):] for l in lines[1:]]


def production_receipt(**over) -> dict:
    """A PASS receipt that carries every proof a reader requires (production admission, the oracle's re-derivable block)."""
    receipt = {"outcome": "PASS", "subject": {"clipId": OWNER_CLIP, "clipContentSha256": CLIP_CONTENT_SHA},
               "evidence": {"umRunOutcome": "RECEIPT"},
               "admission": {"mode": "production", "consentBlobSha": "c" * 40, "venueTableBlobSha": "d" * 40, "ownerLineSha256": "e" * 64},
               "playback": good_block()}
    receipt.update(over)
    return receipt


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

    def test_sols_exact_repro_a_20_frame_play_at_24_fps_is_not_valid(self) -> None:
        receipt = production_receipt(playback=good_block(sourceAdvanced=20, requiredSourceFrames=20, nativeFps=24.0, paceFps=24.0,
                                                          jobSourceAdvanced=20, jobRequiredSourceFrames=20))
        valid, reasons = ps_receipt_valid(receipt)
        self.assertFalse(valid)
        self.assertTrue(any("ceil(20 s x native_fps=24)" in r for r in reasons), reasons)

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

    def test_a_receipt_that_says_valid_true_is_not_believed(self) -> None:
        receipt = production_receipt(playback=dict(good_block(sourceAdvanced=20, requiredSourceFrames=20, nativeFps=24.0, paceFps=24.0), valid=True, invalidReasons=[]))
        self.assertFalse(ps_receipt_valid(receipt)[0])

    def test_mutation_without_the_floor_the_repro_receipt_is_valid(self) -> None:
        anchor = "elseif ($null -ne $required -and $required -lt [int64][Math]::Ceiling($script:MinPlaySeconds * $native - 0.02)) {"
        mutated = self.mutated_module([(anchor, "elseif ($false) {")])
        receipt = production_receipt(playback=good_block(sourceAdvanced=20, requiredSourceFrames=20, nativeFps=24.0, paceFps=24.0, jobSourceAdvanced=20, jobRequiredSourceFrames=20))
        valid, reasons = ps_receipt_valid(receipt, module=mutated)
        self.assertEqual([r for r in reasons if "ceil(20 s" in r], [])
        self.assertTrue(valid, "with the fps-aware floor removed the 20-frame floor alone lets a sub-20 s play through -- so the floor test guards it")

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

    def test_a_pass_without_an_admission_block_is_not_valid(self) -> None:
        for admission in (None, {"mode": "production"}, {"mode": "production", "consentBlobSha": "c" * 40, "venueTableBlobSha": "d" * 40},
                          {"mode": "production", "consentBlobSha": "xyz", "venueTableBlobSha": "d" * 40, "ownerLineSha256": "e" * 64},
                          {"mode": "anything-else"}):
            valid, reasons = ps_receipt_valid(production_receipt(admission=admission))
            self.assertFalse(valid, admission)
            self.assertTrue(any("ADMISSION_UNPROVEN" in r for r in reasons), reasons)
        self.assertTrue(ps_receipt_valid(production_receipt())[0])

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
        self.git("init", "-q")
        self.git("config", "user.email", "unit@example.invalid")
        self.git("config", "user.name", "unit")
        self.git("config", "core.autocrlf", "false")
        self.git("add", "tools")
        self.git("commit", "-q", "-m", "committed dual-venue tree")

    def git(self, *args: str) -> str:
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True).stdout.strip()

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
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($line -cne ('CLIP ' + [string]$r.venue + ': ' + [string]$r.clipId)) {", "if ($false) {"),
                                       ("DualVenueRunner.psm1", "if ((Get-DvSha256OfText $line) -cne [string]$r.ownerLineSha256) {", "if ($false) {"),
                                       ("DualVenueRunner.psm1", "if ([string]$r.recordedBy -cne 'owner') {", "if ($false) {")])
        self.write_artifacts()
        _, receipt, submitted = self.run_leg("ultra-magnus", self.write_spec(), dv=mutated)
        self.assertNotEqual(submitted, [], "with the three owner-line checks removed the hollow record admits the leg -- each is guarded")

    def test_each_owner_line_check_is_needed_on_its_own(self) -> None:
        base = consent_record("ultra-magnus", OWNER_CLIP)
        cases = [
            ("the line", dict(base, ownerLine="CLIP bachelor: " + OWNER_CLIP, ownerLineSha256=hashlib.sha256(("CLIP bachelor: " + OWNER_CLIP).encode()).hexdigest()), "if ($line -cne ('CLIP ' + [string]$r.venue + ': ' + [string]$r.clipId)) {"),
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
        # exactly what the job writes on these terminals: a summary.json WITHOUT a sourceFrames block (and no evidence manifest)
        for token, code in (("GPU_RECON_FRAMES_ZERO", 13), ("CPU_FALLBACK_DETECTED", 14), ("CPU_BACKEND_PATH_MISMATCH", 28)):
            self.write_artifacts(source_frames=False, summary={"result": token}, manifest=None)
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
        self.assertTrue((local / "sheet-ultra-magnus-cpu-classic.png").exists())

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
def _valid_look_receipt(backend: str, frames_dir: Path, clip: str = OWNER_CLIP, with_playback: bool = True) -> dict:
    receipt = {
        "receiptId": f"r-{backend}", "card": "DUAL-VENUE-EVIDENCE-1", "legId": "m16-1243-look", "outcome": "PASS",
        "subject": {"backend": backend, "buildManifestSha256": "ab" * 32, "legSpecSha256": "cd" * 32, "clipId": clip,
                    "clipContentSha256": CLIP_CONTENT_SHA, "lookFlavor": "classic"},
        "venue": {"name": "ultra-magnus", "hostName": "ULTRA-MAGNUS", "gpuNames": ["RTX 4090"]},
        "evidence": {"umRunOutcome": "RECEIPT"},
        "admission": {"mode": "production", "consentBlobSha": "c" * 40, "venueTableBlobSha": "d" * 40, "ownerLineSha256": "e" * 64},
        "look": {"contactSheet": {"rawFramesDir": str(frames_dir)}},
        "playback": None,
    }
    if with_playback:
        receipt["playback"] = good_block(clipId=clip)
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
