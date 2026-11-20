"""Behavioural tests for BENCH-LIGHT-ARM-GAIN-GATES-1.

HUB RULING 2026-11-20T05:25:54Z item (2) MEASUREMENT ARM: a presented-fps gain or cost verdict comes only from LIGHT-arm matched pairs; a HEAVY
(per-frame stage logging) leg gives stage metrics only. tools/profiling/FpsArmGate.psm1 decides which arm a leg ran and whether two legs may be compared
on fps; every gain/cost path imports it and refuses with a typed reason. These tests EXECUTE pwsh against the real module and the real functions of the real
scripts (the "spliced verbatim" precedent of test_playback_host_load_gate.py): a pass here is a statement about the code that runs, not a copy of it.
Nothing here needs a venue, a clip or an MLVApp build. Skipped cleanly when pwsh is not on PATH.

Inventory (RUN\\inventory.md of the card): the gain paths are G1 run-release-cuda-playback-ab.ps1, G2 compare-machine-perf.ps1,
G3 compare-release-gui-smoke-ab.ps1, G4 summarize-local-cuda-proof.ps1, G5 run-local-cuda-playback-dng-smoke.ps1 (pass-through) and G6 the
Invoke-VenueLeaseBatch.ps1 -LegPlan checker.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

ROOT = Path(__file__).resolve().parents[2]
PROFILING = ROOT / "tools" / "profiling"
MODULE = PROFILING / "FpsArmGate.psm1"
CUDA_AB_SCRIPT = PROFILING / "run-release-cuda-playback-ab.ps1"
COMPARE_MACHINE_PERF_SCRIPT = PROFILING / "compare-machine-perf.ps1"
COMPARE_SMOKE_AB_SCRIPT = PROFILING / "compare-release-gui-smoke-ab.ps1"
SUMMARIZER_SCRIPT = PROFILING / "summarize-local-cuda-proof.ps1"
DNG_SMOKE_SCRIPT = PROFILING / "run-local-cuda-playback-dng-smoke.ps1"
LEASE_BATCH_SCRIPT = PROFILING / "dual-venue" / "Invoke-VenueLeaseBatch.ps1"
LEGS_DIR = PROFILING / "dual-venue" / "legs"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")

HEAVY_REASON = "fps-verdict-refused:heavy-arm"
UNKNOWN_REASON = "fps-verdict-refused:unknown-arm"


def _extract_functions(script: Path, names: list[str]) -> str:
    source = script.read_text(encoding="utf-8")
    chunks = []
    for name in names:
        match = re.search(r"function " + re.escape(name) + r" \{.*?\n\}\r?\n", source, re.DOTALL)
        if not match:
            raise AssertionError(f"could not extract function {name} from {script}")
        chunks.append(match.group(0))
    return "\n".join(chunks)


def _extract_block(source: str, start_marker: str, end_marker: str, trailing_lines: int = 1) -> str:
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    for _ in range(1 + trailing_lines):
        end = source.index("\n", end) + 1
    return source[start:end]


class _Probe(unittest.TestCase):
    """Runs a pwsh snippet after importing the real module and dot-sourcing functions extracted from a real script."""

    script: Path | None = None
    functions: list[str] = []

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="fps-arm-probe-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.probe = self.tmp / "probe.ps1"
        body = f"Import-Module '{MODULE}' -Force\n"
        if self.script is not None:
            body += _extract_functions(self.script, self.functions)
        self.probe.write_text(body, encoding="utf-8")

    def run_snippet(self, body: str) -> subprocess.CompletedProcess:
        script = self.tmp / "run.ps1"
        script.write_text("$ErrorActionPreference = 'Stop'\n" f". '{self.probe}'\n" + body, encoding="utf-8")
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                              capture_output=True, text=True, creationflags=_NO_WINDOW)

    def ok(self, body: str) -> str:
        proc = self.run_snippet(body)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout


SPEC_LIGHT = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT'; presentTrace = $true } }"
SPEC_HEAVY = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'HEAVY' } }"
SPEC_NO_ARM = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ unpaced = $true } }"


@requires_pwsh
class ArmClassifierTests(_Probe):
    """(a) The pure classifier: spec -> LIGHT | HEAVY | UNKNOWN, and the recorded-result and launch-environment forms."""

    def arm(self, expr: str) -> str:
        return self.ok(f"Write-Host ('ARM=' + ({expr}).arm)").strip()

    def test_heavy_switch_spec_is_heavy(self) -> None:
        self.assertEqual(self.arm(f"Get-FpsArmFromLegSpec ({SPEC_HEAVY})"), "ARM=HEAVY")

    def test_present_trace_only_spec_is_light(self) -> None:
        self.assertEqual(self.arm(f"Get-FpsArmFromLegSpec ({SPEC_LIGHT})"), "ARM=LIGHT")

    def test_spec_missing_the_inputs_is_unknown(self) -> None:
        # the generator's own default for an absent telemetryArm is HEAVY (playback-attr-3-cuda-job.ps1 -TelemetryArm), which is not proof of LIGHT either
        for expr in (SPEC_NO_ARM, "[pscustomobject]@{ legType = 'speed' }", "[pscustomobject]@{ }", "$null",
                     "[pscustomobject]@{ legType = 'export'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' } }"):
            with self.subTest(spec=expr):
                self.assertEqual(self.arm(f"Get-FpsArmFromLegSpec ({expr})"), "ARM=UNKNOWN")

    def test_a_malformed_arm_value_is_unknown(self) -> None:
        for value in ("light", "MEDIUM", "", "LIGHT "):
            with self.subTest(value=value):
                spec = f"[pscustomobject]@{{ legType = 'speed'; generatorArgs = [pscustomobject]@{{ telemetryArm = '{value}' }} }}"
                self.assertEqual(self.arm(f"Get-FpsArmFromLegSpec ({spec})"), "ARM=UNKNOWN")

    def test_the_committed_spec_corpus_classifies_by_its_own_field(self) -> None:
        specs = sorted(LEGS_DIR.glob("*.json"))
        self.assertGreater(len(specs), 100)
        expected = {}
        for path in specs:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("legType") == "export":
                expected[path.name] = "UNKNOWN"
            else:
                expected[path.name] = doc.get("generatorArgs", {}).get("telemetryArm", "UNKNOWN")
        self.assertIn("HEAVY", expected.values())
        self.assertIn("LIGHT", expected.values())
        probe = ("foreach ($f in Get-ChildItem -LiteralPath '" + str(LEGS_DIR) + "' -Filter *.json | Sort-Object Name) {\n"
                 "  $s = Get-Content -LiteralPath $f.FullName -Raw | ConvertFrom-Json\n"
                 "  Write-Host ($f.Name + '=' + (Get-FpsArmFromLegSpec $s).arm)\n}\n")
        got = dict(line.split("=", 1) for line in self.ok(probe).splitlines() if "=" in line)
        self.assertEqual(got, expected)

    def test_a_recorded_result_that_carries_the_arm_classifies_by_it(self) -> None:
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'HEAVY' })"), "ARM=HEAVY")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT' })"), "ARM=LIGHT")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ sourceCommit = 'x' })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult $null"), "ARM=UNKNOWN")

    def test_spec_and_result_must_agree(self) -> None:
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_LIGHT}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})"), "ARM=LIGHT")
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_LIGHT}) -Result ([pscustomobject]@{{ telemetryArm = 'HEAVY' }})"), "ARM=UNKNOWN")
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_HEAVY}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArm -Spec $null -Result ([pscustomobject]@{ telemetryArm = 'LIGHT' })"), "ARM=LIGHT")
        self.assertEqual(self.arm("Get-FpsArm -Spec $null -Result $null"), "ARM=UNKNOWN")

    def test_launch_environment_follows_the_apps_frame_log_rule(self) -> None:
        cases = {
            "$null": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1')": "LIGHT",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=0')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=2')": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_STAGE_TIMING=1')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_PERF_FIELD_LOG=1')": "HEAVY",
            "@('MLVAPP_STAGE_TIMING=0')": "LIGHT",
            "[pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1' }": "HEAVY",
            "[ordered]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = 'true' }": "LIGHT",
        }
        for expr, want in cases.items():
            with self.subTest(env=expr):
                self.assertEqual(self.arm(f"Get-FpsArmFromEnvironment ({expr})"), f"ARM={want}")


@requires_pwsh
class PairVerdictTests(_Probe):
    """(c) Two legs may be compared on presented fps only when BOTH are LIGHT; stage metrics stay allowed."""

    def verdict(self, baseline: str, candidate: str) -> dict:
        out = self.ok(f"$v = Get-FpsPairVerdict -Baseline {baseline} -Candidate {candidate}\n$v | ConvertTo-Json -Compress")
        return json.loads(out.strip().splitlines()[-1])

    def test_two_light_legs_are_allowed(self) -> None:
        v = self.verdict("'LIGHT'", "'LIGHT'")
        self.assertTrue(v["allowed"])
        self.assertEqual(v["reason"], "")

    def test_two_heavy_legs_are_refused_and_keep_stage_metrics(self) -> None:
        v = self.verdict("'HEAVY'", "'HEAVY'")
        self.assertFalse(v["allowed"])
        self.assertEqual(v["reason"], HEAVY_REASON)
        self.assertTrue(v["stageMetricsAllowed"])

    def test_a_mixed_light_heavy_pair_is_refused_either_way_round(self) -> None:
        for b, c in (("'LIGHT'", "'HEAVY'"), ("'HEAVY'", "'LIGHT'")):
            with self.subTest(baseline=b, candidate=c):
                v = self.verdict(b, c)
                self.assertFalse(v["allowed"])
                self.assertEqual(v["reason"], HEAVY_REASON)
                self.assertIn("mixed", v["detail"])
                self.assertTrue(v["stageMetricsAllowed"])

    def test_an_unknown_leg_is_refused_like_heavy(self) -> None:
        for b, c in (("'LIGHT'", "$null"), ("$null", "'LIGHT'"), ("'LIGHT'", "'light'"), ("'UNKNOWN'", "'UNKNOWN'")):
            with self.subTest(baseline=b, candidate=c):
                v = self.verdict(b, c)
                self.assertFalse(v["allowed"])
                self.assertEqual(v["reason"], UNKNOWN_REASON)

    def test_heavy_outranks_unknown_in_the_typed_reason(self) -> None:
        self.assertEqual(self.verdict("'HEAVY'", "$null")["reason"], HEAVY_REASON)

    def test_arm_objects_are_accepted_as_well_as_names(self) -> None:
        v = self.verdict("(Get-FpsArmFromLegSpec (" + SPEC_LIGHT + "))", "(Get-FpsArmFromLegSpec (" + SPEC_HEAVY + "))")
        self.assertEqual(v["reason"], HEAVY_REASON)
        self.assertEqual((v["baselineArm"], v["candidateArm"]), ("LIGHT", "HEAVY"))

    def test_many_legs_follow_the_same_rule(self) -> None:
        out = self.ok("$legs = @([pscustomobject]@{ name = 'a'; arm = 'LIGHT' }, [pscustomobject]@{ name = 'b'; arm = 'HEAVY' }, [pscustomobject]@{ name = 'c'; arm = $null })\n"
                      "Get-FpsArmsVerdict -Legs $legs | ConvertTo-Json -Compress")
        v = json.loads(out.strip().splitlines()[-1])
        self.assertEqual((v["allowed"], v["reason"], v["heavy"], v["unknown"]), (False, HEAVY_REASON, ["b"], ["c"]))


@requires_pwsh
class LegPlanRuleTests(_Probe):
    """(d), module level: a plan labelled an fps A/B may not hold a HEAVY or UNKNOWN leg."""

    def refusal(self, purpose: str, arms: list[str]) -> list[str]:
        items = ", ".join(f"[pscustomobject]@{{ item = {i + 1}; arm = '{a}' }}" for i, a in enumerate(arms))
        out = self.ok(f"$r = Get-FpsLegPlanRefusal -Purpose {purpose} -Items @({items})\nWrite-Host ('R=' + ($r -join '|'))")
        return out.strip().splitlines()[-1][2:].split("|")

    def test_an_fps_ab_plan_with_a_heavy_leg_is_refused_at_that_item(self) -> None:
        r = self.refusal("'fps-ab'", ["LIGHT", "HEAVY", "LIGHT"])
        self.assertEqual(r[:2], ["FPS_AB_HEAVY_ARM", "2"])
        self.assertIn(HEAVY_REASON, r[2])

    def test_an_fps_ab_plan_with_an_unknown_leg_is_refused(self) -> None:
        self.assertEqual(self.refusal("'fps-ab'", ["LIGHT", "UNKNOWN"])[:2], ["FPS_AB_UNKNOWN_ARM", "2"])

    def test_an_all_light_fps_ab_plan_passes(self) -> None:
        self.assertEqual(self.refusal("'fps-ab'", ["LIGHT", "LIGHT"])[0], "")

    def test_a_stage_attribution_plan_may_run_heavy_legs(self) -> None:
        self.assertEqual(self.refusal("'stage-attribution'", ["HEAVY", "HEAVY"])[0], "")

    def test_an_undeclared_purpose_is_not_refused_here(self) -> None:
        self.assertEqual(self.refusal("$null", ["HEAVY", "HEAVY"])[0], "")

    def test_a_bad_purpose_is_refused(self) -> None:
        self.assertEqual(self.refusal("'fps'", ["LIGHT"])[0], "BAD_PURPOSE")


class LeaseBatchPlanCheckerTests(unittest.TestCase):
    """(d), end to end: the real Invoke-VenueLeaseBatch.ps1 -LegPlan refuses a plan that labels HEAVY legs as an fps A/B, before any claim or lease."""

    COMMIT = "1" * 40
    MANIFEST = "a" * 64

    def setUp(self) -> None:
        if PWSH is None:
            self.skipTest("pwsh is not on PATH")
        self._tmp = tempfile.TemporaryDirectory(prefix="fps-arm-plan-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        (self.tmp / "share").mkdir()

    def spec(self, name: str, ga: dict | None) -> Path:
        doc = {"schema": "mlv-app/dual-venue-leg/v1", "card": "TEST-CARD", "legId": name, "legType": "speed", "backends": ["cpu", "cuda"], "scaleFactor": 1}
        if ga is not None:
            doc["generatorArgs"] = ga
        path = self.tmp / f"{name}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return path

    def run_plan(self, specs: list[Path], purpose: object = "absent") -> subprocess.CompletedProcess:
        legs = [{"legSpec": str(p), "backend": "cpu", "sourceCommit": self.COMMIT, "buildManifestSha256": self.MANIFEST, "arm": "AB"[i % 2]} for i, p in enumerate(specs)]
        plan = {"schema": "mlv-app/venue-lease-leg-plan/v1", "legs": legs}
        if purpose != "absent":
            plan["purpose"] = purpose
        plan_path = self.tmp / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        run_dir = self.tmp / "run"
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(LEASE_BATCH_SCRIPT), "-LegPlan", str(plan_path), "-RunDir", str(run_dir),
                               "-ClaimDir", str(self.tmp / "claims"), "-AgentShare", str(self.tmp / "share"),
                               "-DeadlineUtc", "2099-01-01T00:00:00Z"],
                              capture_output=True, text=True, timeout=300, creationflags=_NO_WINDOW)

    def test_fps_ab_with_a_heavy_leg_is_refused_before_any_claim(self) -> None:
        light = self.spec("fx-light", {"telemetryArm": "LIGHT", "presentTrace": True})
        heavy = self.spec("fx-heavy", {"telemetryArm": "HEAVY"})
        r = self.run_plan([light, heavy], "fps-ab")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=FPS_AB_HEAVY_ARM item=2 ", r.stderr)
        self.assertIn(HEAVY_REASON, r.stderr)
        self.assertFalse((self.tmp / "claims").exists(), "nothing was claimed")

    def test_fps_ab_with_a_leg_that_names_no_arm_is_refused(self) -> None:
        r = self.run_plan([self.spec("fx-a", {"telemetryArm": "LIGHT"}), self.spec("fx-b", {"unpaced": True})], "fps-ab")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=FPS_AB_UNKNOWN_ARM item=2 ", r.stderr)

    def test_an_unreadable_purpose_is_refused(self) -> None:
        r = self.run_plan([self.spec("fx-a", {"telemetryArm": "LIGHT"})], "throughput")
        self.assertIn("LEG_PLAN_REFUSED reason=BAD_PURPOSE", r.stderr)

    def test_stage_attribution_and_light_fps_plans_get_past_the_arm_check(self) -> None:
        # both fall through to the build-staging check (no build is staged in this fixture), which is a different typed refusal
        heavy = [self.spec("fx-h1", {"telemetryArm": "HEAVY"}), self.spec("fx-h2", {"telemetryArm": "HEAVY"})]
        light = [self.spec("fx-l1", {"telemetryArm": "LIGHT"}), self.spec("fx-l2", {"telemetryArm": "LIGHT"})]
        for specs, purpose in ((heavy, "stage-attribution"), (light, "fps-ab"), (heavy, "absent")):
            with self.subTest(purpose=purpose, arm=specs[0].name):
                r = self.run_plan(specs, purpose)
                self.assertNotIn("FPS_AB_", r.stderr, r.stdout + r.stderr)
                self.assertIn("LEG_PLAN_REFUSED reason=", r.stderr)


@requires_pwsh
class CudaPlaybackAbFpsGateTests(_Probe):
    """G1 run-release-cuda-playback-ab.ps1: the A/B's fps comparison refuses a HEAVY / UNKNOWN pair and keeps the stage metrics."""

    script = CUDA_AB_SCRIPT
    functions = ["Get-NestedValue", "Convert-ToNullableDouble", "New-MetricDelta", "Get-CompareDeltaPercent", "Test-DeltaAtLeast", "New-PlaybackAbAnalysis",
                 "New-PlaybackCompare", "Get-SmokeSummaryTelemetryArm", "Get-PlaybackAbFpsVerdict", "Protect-PlaybackCompareFps"]

    @staticmethod
    def leg(arm: str | None, fps: float, queue_ms: float) -> str:
        arm_part = "" if arm is None else f"telemetryArm = '{arm}'; "
        return f"[pscustomobject]@{{ {arm_part}presentedFps = {fps}; avgQueueWaitMs = {queue_ms}; avgDrawTotalMs = 12.0 }}"

    def run_ab(self, baseline: str, candidate: str) -> dict:
        out = self.ok(f"$b = {baseline}\n$c = {candidate}\n"
                      "$verdict = Get-PlaybackAbFpsVerdict -BaselineSummary $b -CandidateSummary $c -CandidateSpeedSummary $null\n"
                      "$compare = New-PlaybackCompare -BaselineSummary $b -CandidateSummary $c\n"
                      "$compare = Protect-PlaybackCompareFps -Compare $compare -Verdict $verdict.candidate\n"
                      "$refusal = if ($verdict.allowed) { '' } else { $verdict.reason }\n"
                      "$analysis = New-PlaybackAbAnalysis -Compare $compare -ProofFailures @() -FpsVerdictRefusal $refusal\n"
                      "[pscustomobject]@{ verdict = $verdict; compare = $compare; analysis = $analysis } | ConvertTo-Json -Depth 8 -Compress")
        return json.loads(out.strip().splitlines()[-1])

    def test_a_light_pair_keeps_its_fps_delta(self) -> None:
        r = self.run_ab(self.leg("LIGHT", 50.0, 5.0), self.leg("LIGHT", 55.0, 5.0))
        self.assertTrue(r["verdict"]["allowed"])
        self.assertEqual(r["compare"]["presentedFps"]["deltaPercent"], 10.0)
        self.assertNotEqual(r["analysis"]["dominantBottleneck"], "fps-arm-refused")

    def test_a_heavy_pair_refuses_the_fps_delta_and_keeps_stage_deltas(self) -> None:
        r = self.run_ab(self.leg("HEAVY", 40.0, 5.0), self.leg("HEAVY", 44.0, 2.5))
        self.assertFalse(r["verdict"]["allowed"])
        self.assertEqual(r["verdict"]["reason"], HEAVY_REASON)
        fps = r["compare"]["presentedFps"]
        self.assertIsNone(fps["delta"])
        self.assertIsNone(fps["deltaPercent"])
        self.assertEqual(fps["refused"], HEAVY_REASON)
        self.assertEqual(r["compare"]["avgQueueWaitMs"]["deltaPercent"], -50.0, "stage metrics stay")
        self.assertEqual(r["analysis"]["dominantBottleneck"], "fps-arm-refused")
        self.assertEqual(r["analysis"]["evidence"]["queueWaitDeltaPercent"], -50.0)

    def test_a_mixed_pair_is_refused(self) -> None:
        r = self.run_ab(self.leg("LIGHT", 54.0, 5.0), self.leg("HEAVY", 41.0, 5.0))
        self.assertEqual(r["verdict"]["reason"], HEAVY_REASON)
        self.assertIsNone(r["compare"]["presentedFps"]["deltaPercent"])

    def test_a_leg_with_no_recorded_arm_is_unknown_and_refused(self) -> None:
        r = self.run_ab(self.leg(None, 54.0, 5.0), self.leg("LIGHT", 55.0, 5.0))
        self.assertEqual(r["verdict"]["reason"], UNKNOWN_REASON)
        self.assertIsNone(r["compare"]["presentedFps"]["deltaPercent"])

    def test_the_separate_speed_run_is_judged_on_its_own_pair(self) -> None:
        out = self.ok(f"$b = {self.leg('LIGHT', 50.0, 5.0)}\n$c = {self.leg('LIGHT', 51.0, 5.0)}\n$s = {self.leg('HEAVY', 44.0, 5.0)}\n"
                      "$v = Get-PlaybackAbFpsVerdict -BaselineSummary $b -CandidateSummary $c -CandidateSpeedSummary $s -SeparateCandidateSpeedRun\n"
                      "$v | ConvertTo-Json -Depth 6 -Compress")
        v = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(v["allowed"])
        self.assertTrue(v["candidate"]["allowed"])
        self.assertFalse(v["candidateSpeed"]["allowed"])

    def test_the_smoke_summary_arm_is_read_from_the_recorded_launch_environment(self) -> None:
        cases = {
            "launch = [pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1' } }": "HEAVY",
            ("launch = [pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; "
             "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' } }"): "LIGHT",
            "launch = [pscustomobject]@{ }": "UNKNOWN",
            "x = 1": "UNKNOWN",
        }
        for body, want in cases.items():
            with self.subTest(body=body):
                out = self.ok(f"$j = [pscustomobject]@{{ {body} }}\nWrite-Host ('ARM=' + (Get-SmokeSummaryTelemetryArm -Json $j).arm)")
                self.assertEqual(out.strip(), f"ARM={want}")


@requires_pwsh
class CudaPlaybackAbRequireImprovementBlockTests(_Probe):
    """G1: -RequireCandidateImprovesPresentedFps is an fps verdict; it refuses, typed, when the pair is not LIGHT/LIGHT, and never reports a fake regression."""

    script = CUDA_AB_SCRIPT
    functions = ["Convert-ToNullableDouble", "Get-PlaybackAbFpsVerdict"]

    def run_block(self, baseline: str, candidate: str, fps_b: float, fps_c: float) -> list[str]:
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        block = _extract_block(source, "if ($RequireCandidateImprovesPresentedFps) {", "candidate-presented-fps-not-improved baseline=$baselineFps candidate=$candidateFps", trailing_lines=3)
        out = self.ok(
            f"$baselineSummary = [pscustomobject]@{{ telemetryArm = {baseline}; presentedFps = {fps_b}; hostLoadProvisional = $false }}\n"
            f"$candidateSummary = [pscustomobject]@{{ telemetryArm = {candidate}; presentedFps = {fps_c}; hostLoadProvisional = $false }}\n"
            "$candidateSpeedSummary = $null\n$SeparateCandidateSpeedRun = $false\n$RequireCandidateImprovesPresentedFps = $true\n$proofFailures = @()\n"
            "$fpsVerdict = Get-PlaybackAbFpsVerdict -BaselineSummary $baselineSummary -CandidateSummary $candidateSummary -CandidateSpeedSummary $candidateSpeedSummary\n"
            + block + "\nforeach ($f in $proofFailures) { Write-Host ('FAIL=' + $f) }\nWrite-Host ('COUNT=' + @($proofFailures).Count)\n")
        return out.splitlines()

    def test_heavy_legs_get_the_typed_refusal_not_a_regression_line(self) -> None:
        lines = self.run_block("'HEAVY'", "'HEAVY'", 40.0, 30.0)
        text = "\n".join(lines)
        self.assertIn(HEAVY_REASON, text)
        self.assertIn("candidate-presented-fps-improvement-not-evaluated", text)
        self.assertNotIn("candidate-presented-fps-not-improved", text)

    def test_light_legs_still_get_the_improvement_verdict(self) -> None:
        text = "\n".join(self.run_block("'LIGHT'", "'LIGHT'", 50.0, 49.0))
        self.assertIn("candidate-presented-fps-not-improved baseline=50 candidate=49", text)
        self.assertEqual("\n".join(self.run_block("'LIGHT'", "'LIGHT'", 50.0, 51.0)).count("FAIL="), 0)


@requires_pwsh
class CompareMachinePerfFpsGateTests(_Probe):
    """G2 compare-machine-perf.ps1: no fps delta, diagnosis or suggestion from a HEAVY / UNKNOWN playback A/B record."""

    script = COMPARE_MACHINE_PERF_SCRIPT
    functions = ["Convert-ToNullableDouble", "Get-MachineLabel", "Assert-MachineFingerprint", "Get-PlaybackAbLegHostLoadProvisional", "Get-PlaybackAbHostLoadRefusal",
                 "Get-PlaybackAbFpsArmRefusal", "Get-CompareDeltaPercent", "Test-DeltaAtLeast", "Get-PlaybackAbAnalysis", "New-PlaybackAbSummaryRow"]

    @staticmethod
    def record(baseline_arm: str | None, candidate_arm: str | None, with_verdict: bool = False) -> str:
        def leg(arm: str | None) -> str:
            arm_part = "" if arm is None else f"telemetryArm = '{arm}'; "
            return f"[pscustomobject]@{{ {arm_part}hostLoadProvisional = $false; hostLoadState = 'quiet' }}"
        verdict = "fpsVerdict = [pscustomobject]@{ allowed = $true; reason = '' }; " if with_verdict else ""
        return ("[pscustomobject]@{ schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; " + verdict +
                "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
                f"baseline = {leg(baseline_arm)}; candidate = {leg(candidate_arm)}; candidateSpeed = $null; "
                "analysis = [pscustomobject]@{ dominantBottleneck = 'recon-bound'; suggestedOptimization = 'optimize_cuda_recon_kernel_or_state_setup' }; "
                "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 10.0; candidate = 20.0; deltaPercent = 100.0 } } }")

    def row(self, record: str) -> dict:
        out = self.ok(f"$record = {record}\n$r = New-PlaybackAbSummaryRow -Record $record -Source 'test'\n$r | ConvertTo-Json -Compress")
        return json.loads(out.strip().splitlines()[-1])

    def test_a_light_record_publishes_the_delta(self) -> None:
        r = self.row(self.record("LIGHT", "LIGHT"))
        self.assertEqual(r["playback_fps_delta_pct"], 100.0)
        self.assertEqual(r["dominant_bottleneck"], "recon-bound")
        self.assertEqual(r["playback_fps_arm_verdict"], "allowed")

    def test_a_heavy_or_mixed_record_refuses_the_delta_with_the_typed_reason(self) -> None:
        for b, c in (("HEAVY", "HEAVY"), ("LIGHT", "HEAVY"), ("HEAVY", "LIGHT")):
            with self.subTest(baseline=b, candidate=c):
                r = self.row(self.record(b, c))
                self.assertIsNone(r["playback_fps_delta_pct"])
                self.assertIn(HEAVY_REASON, r["playback_fps_delta_refused_reason"])
                self.assertEqual(r["playback_fps_arm_verdict"], HEAVY_REASON)
                self.assertEqual(r["dominant_bottleneck"], "fps-arm-refused")
                self.assertEqual(r["suggested_optimization"], "rerun_playback_ab_with_light_arm")
                # the single-leg numbers stay: they are one leg's own measurement, not a comparison
                self.assertEqual((r["baseline_presented_fps"], r["presented_fps"]), (10.0, 20.0))

    def test_a_record_that_names_no_arm_is_unknown_and_refused_even_if_it_carries_a_cached_allowed_verdict(self) -> None:
        for with_verdict in (False, True):
            with self.subTest(cached_verdict=with_verdict):
                r = self.row(self.record(None, None, with_verdict=with_verdict))
                self.assertIsNone(r["playback_fps_delta_pct"])
                self.assertEqual(r["playback_fps_arm_verdict"], UNKNOWN_REASON)


@requires_pwsh
class CompareMachinePerfSuggestionFpsGateTests(_Probe):
    script = COMPARE_MACHINE_PERF_SCRIPT
    functions = ["Convert-ToNullableInt64", "Convert-ToNullableDouble", "Get-PlaybackAbLegHostLoadProvisional", "Get-PlaybackAbHostLoadRefusal", "Get-PlaybackAbFpsArmRefusal",
                 "Get-ProofSummarySuggestion"]

    def suggestion(self, arm: str | None) -> str:
        arm_part = "" if arm is None else f"telemetryArm = '{arm}'; "
        leg = f"[pscustomobject]@{{ {arm_part}hostLoadProvisional = $false; hostLoadState = 'quiet' }}"
        return self.ok(
            "$rec = [pscustomobject]@{ proof = [pscustomobject]@{ playback = [pscustomobject]@{ totalFallbackFrames = 0; totalGpuTextureNoReadbackFrames = 5 }; "
            f"playbackAb = [pscustomobject]@{{ baseline = {leg}; candidate = {leg}; candidateSpeed = $null; "
            "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ delta = -3.0 } }; analysis = $null }; cdng = $null } }\n"
            "Write-Host ('S=' + (Get-ProofSummarySuggestion -Record $rec))").strip()

    def test_heavy_and_unknown_records_never_suggest_from_an_fps_delta(self) -> None:
        self.assertEqual(self.suggestion("HEAVY"), "S=rerun_playback_ab_with_light_arm")
        self.assertEqual(self.suggestion(None), "S=rerun_playback_ab_with_light_arm")

    def test_a_light_record_keeps_the_fps_delta_suggestion(self) -> None:
        self.assertEqual(self.suggestion("LIGHT"), "S=optimize_playback_cuda_candidate")


@requires_pwsh
class CompareGuiSmokeAbFpsGateTests(_Probe):
    """G3 compare-release-gui-smoke-ab.ps1: the before/after fps deltas are withheld, typed, unless both smokes ran LIGHT; the screenshot verdict is unaffected."""

    script = COMPARE_SMOKE_AB_SCRIPT
    functions = ["Get-NestedValue", "Convert-ToNullableDouble", "New-DeltaObject", "Get-FpsArmComparisonEvidence", "Hide-FpsComparisonDeltas"]

    @staticmethod
    def smoke(env: str | None) -> str:
        if env is None:
            return "[pscustomobject]@{ launch = [pscustomobject]@{ } }"
        return f"[pscustomobject]@{{ launch = [pscustomobject]@{{ environment = [pscustomobject]@{{ {env} }} }} }}"

    HEAVY_ENV = "MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'"
    LIGHT_ENV = "MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'"

    def run_compare(self, before: str | None, after: str | None) -> dict:
        out = self.ok(
            f"$before = {self.smoke(before)}\n$after = {self.smoke(after)}\n"
            "$e = Get-FpsArmComparisonEvidence -BeforeSmoke $before -AfterSmoke $after\n"
            "$result = [pscustomobject]@{ fps = [pscustomobject]@{ smokePresentedFps = (New-DeltaObject -BeforeValue 50 -AfterValue 55); "
            "visibleBottomLeftGuiFps = (New-DeltaObject -BeforeValue 50 -AfterValue 55); smokeTimelineFps = (New-DeltaObject -BeforeValue 50 -AfterValue 55) }; "
            "autoDecision = [pscustomobject]@{ averageFpsEquivalent = (New-DeltaObject -BeforeValue 50 -AfterValue 55); averageMs = (New-DeltaObject -BeforeValue 20 -AfterValue 18) } }\n"
            "Hide-FpsComparisonDeltas -Result $result -Evidence $e\n"
            "[pscustomobject]@{ evidence = $e; result = $result } | ConvertTo-Json -Depth 8 -Compress")
        return json.loads(out.strip().splitlines()[-1])

    def test_two_light_smokes_keep_their_fps_deltas(self) -> None:
        r = self.run_compare(self.LIGHT_ENV, self.LIGHT_ENV)
        self.assertFalse(r["evidence"]["refused"])
        self.assertEqual(r["result"]["fps"]["smokePresentedFps"]["deltaPercent"], 10.0)

    def test_heavy_smokes_have_every_fps_delta_withheld_and_stage_figures_kept(self) -> None:
        r = self.run_compare(self.HEAVY_ENV, self.HEAVY_ENV)
        self.assertTrue(r["evidence"]["refused"])
        self.assertEqual(r["evidence"]["reason"], HEAVY_REASON)
        for name in ("smokePresentedFps", "visibleBottomLeftGuiFps", "smokeTimelineFps"):
            self.assertIsNone(r["result"]["fps"][name]["delta"], name)
            self.assertIsNone(r["result"]["fps"][name]["deltaPercent"], name)
            self.assertEqual(r["result"]["fps"][name]["after"], 55.0, "each leg's own raw value stays")
        self.assertIsNone(r["result"]["autoDecision"]["averageFpsEquivalent"]["deltaPercent"])
        self.assertEqual(r["result"]["autoDecision"]["averageMs"]["deltaPercent"], -10.0, "a ms stage figure stays")

    def test_a_mixed_pair_and_a_smoke_with_no_recorded_environment_are_refused(self) -> None:
        self.assertEqual(self.run_compare(self.LIGHT_ENV, self.HEAVY_ENV)["evidence"]["reason"], HEAVY_REASON)
        self.assertEqual(self.run_compare(self.LIGHT_ENV, None)["evidence"]["reason"], UNKNOWN_REASON)


@requires_pwsh
class LocalProofSummarizerFpsGateTests(unittest.TestCase):
    """G4 summarize-local-cuda-proof.ps1 and G5 run-local-cuda-playback-dng-smoke.ps1: the 'presented FPS not improved' diagnostic and the delta evidence lines are not
    produced from a HEAVY / UNKNOWN A/B; the dng-smoke packager passes the verdict through."""

    def test_the_summarizer_imports_the_gate_and_guards_both_fps_sites(self) -> None:
        source = SUMMARIZER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("FpsArmGate.psm1", source)
        guard = _extract_block(source, "$playbackAbFpsArmVerdict =", "PLAYBACK_AB_FPS_ARM_REFUSED", trailing_lines=8)
        self.assertIn("Get-FpsPairVerdict", guard)
        # the improvement diagnostic is behind the arm refusal
        self.assertRegex(source, r"-not \$playbackAbHostLoadProvisional -and\s+-not \$playbackAbFpsArmRefusal\.refused -and")

    def run_summarizer_block(self, baseline_arm: str, candidate_arm: str) -> list[str]:
        tmp = Path(tempfile.mkdtemp(prefix="fps-arm-summ-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        source = SUMMARIZER_SCRIPT.read_text(encoding="utf-8")
        funcs = _extract_functions(SUMMARIZER_SCRIPT, ["Get-Field", "Convert-ToNullableDouble", "New-Diagnostic", "Add-Diagnostic", "Format-Nullable", "Format-Percent", "Format-MetricDeltaLine"])
        start = "$playbackAbFpsArmVerdict ="
        end = "$playbackSpeedEvidence = @()"
        block = source[source.index(start):source.index(end)]
        evidence_end = source.index("$playbackAbVerdict = New-SectionVerdict")
        evidence = source[source.index(end):evidence_end]
        probe = tmp / "run.ps1"
        probe.write_text(
            "$ErrorActionPreference = 'Stop'\n" f"Import-Module '{MODULE}' -Force\n" + funcs +
            "\n$playbackAbComparisonBasis = 'candidate'\n"
            f"$playbackAb = [pscustomobject]@{{ analysis = $null; baseline = [pscustomobject]@{{ telemetryArm = '{baseline_arm}'; hostLoadProvisional = $false; hostLoadState = 'quiet' }}; "
            f"candidate = [pscustomobject]@{{ telemetryArm = '{candidate_arm}'; hostLoadProvisional = $false; hostLoadState = 'quiet' }} }}\n"
            "$playbackAbSpeedCompare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 50; candidate = 49; delta = -1; deltaPercent = -2 }; "
            "guiStatusFps = [pscustomobject]@{ baseline = 50; candidate = 49; delta = -1; deltaPercent = -2 }; "
            "avgPresentIntervalMs = [pscustomobject]@{ baseline = 20; candidate = 21; delta = 1; deltaPercent = 5 }; "
            "avgRenderTotalMs = [pscustomobject]@{ baseline = 10; candidate = 11; delta = 1; deltaPercent = 10 } }\n"
            "$playbackAbCandidateLeg = $playbackAb.candidate\n$playbackAbHostLoadProvisional = $false\n"
            "$playbackAbBlockers = [System.Collections.Generic.List[string]]::new()\n$diagnostics = [System.Collections.Generic.List[object]]::new()\n"
            + "if ($true) {\n" + block + "\n" + evidence +
            "\nforeach ($d in $diagnostics) { Write-Host ('DIAG=' + $d.code) }\nforeach ($e in $playbackSpeedEvidence) { Write-Host ('EV=' + $e) }\n"
            "Write-Host ('BLOCKERS=' + ($playbackAbBlockers -join ' / '))\n", encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe)], capture_output=True, text=True, creationflags=_NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.splitlines()

    def test_heavy_ab_gets_a_refusal_diagnostic_and_no_fps_delta_lines(self) -> None:
        text = "\n".join(self.run_summarizer_block("HEAVY", "HEAVY"))
        self.assertIn("DIAG=PLAYBACK_AB_FPS_ARM_REFUSED", text)
        self.assertNotIn("PLAYBACK_PRESENTED_FPS_NOT_IMPROVED", text)
        self.assertIn(HEAVY_REASON, text)
        self.assertNotIn("presented FPS: CPU", text)
        self.assertIn("avg present interval", text, "stage lines stay")

    def test_light_ab_keeps_the_verdict_and_delta_lines(self) -> None:
        text = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT"))
        self.assertIn("DIAG=PLAYBACK_PRESENTED_FPS_NOT_IMPROVED", text)
        self.assertNotIn("PLAYBACK_AB_FPS_ARM_REFUSED", text)
        self.assertIn("presented FPS: CPU", text)

    def test_the_dng_smoke_packager_passes_the_fps_verdict_through(self) -> None:
        source = DNG_SMOKE_SCRIPT.read_text(encoding="utf-8")
        body = _extract_functions(DNG_SMOKE_SCRIPT, ["Get-PlaybackAbProofSummary"])
        self.assertIn("fpsVerdict = $Summary.fpsVerdict", body)
        self.assertIn("Get-PlaybackAbProofSummary", source)


if __name__ == "__main__":
    unittest.main()
