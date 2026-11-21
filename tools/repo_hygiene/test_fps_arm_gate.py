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
import sys
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
# Invoke-VenueLeaseBatch.ps1 is a Windows-only runner: on Linux pwsh its input guard cannot resolve the plan's physical path and refuses it as LEG_PLAN_IS_RUN_OUTPUT before the
# purpose / arm check (hosted Tests run 44737591712, ubuntu shard 3/4). test_venue_lease_batch.py skips its runner tests the same way (requires_windows_pwsh); the Windows shards run these.
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows (the lease-batch runner is Windows-only)")

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


# BENCH-LIGHT-ARM-GAIN-GATES-1 r5 (Dot on f51e0821): a saved leg record's LIGHT arm WORD is proof only beside a recorded observer list, the rule a recorded result already had. A LIGHT leg
# fixture therefore carries the list a real leg records, every observer off (a bare 'LIGHT' record is pinned UNKNOWN in ObserverControlTests).
OBSERVERS_RECORDED_OFF = "observerControls = [pscustomobject]@{ utilSample = $false; corePartitionTelemetry = $false }; "


def _light_word(arm: str | None) -> str:
    """The `telemetryArm` (and recorded observer list for LIGHT) fragment of a pwsh leg-record literal; empty for no arm."""
    if arm is None:
        return ""
    return f"telemetryArm = '{arm}'; " + (OBSERVERS_RECORDED_OFF if arm == "LIGHT" else "")


SPEC_LIGHT = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT'; presentTrace = $true } }"
SPEC_HEAVY = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'HEAVY' } }"
SPEC_NO_ARM = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ unpaced = $true } }"


@requires_pwsh
class ArmClassifierTests(_Probe):
    """(a) The pure classifier: spec -> LIGHT | HEAVY | UNKNOWN, and the recorded-result and launch-environment forms."""

    def arm(self, expr: str) -> str:
        return self.ok(f"Write-Host ('ARM=' + ({expr}).arm)").strip()

    def arms(self, exprs: list[str]) -> list[str]:
        # one pwsh launch for many cases (each launch costs a second or more on a shared host and the shard budget counts it)
        body = "".join(f"Write-Host ('ARM=' + ({e}).arm)\n" for e in exprs)
        return self.ok(body).split()

    def test_heavy_switch_spec_is_heavy(self) -> None:
        self.assertEqual(self.arm(f"Get-FpsArmFromLegSpec ({SPEC_HEAVY})"), "ARM=HEAVY")

    def test_present_trace_only_spec_is_light(self) -> None:
        self.assertEqual(self.arm(f"Get-FpsArmFromLegSpec ({SPEC_LIGHT})"), "ARM=LIGHT")

    def test_spec_missing_the_inputs_is_unknown(self) -> None:
        # the generator's own default for an absent telemetryArm is HEAVY (playback-attr-3-cuda-job.ps1 -TelemetryArm), which is not proof of LIGHT either
        specs = (SPEC_NO_ARM, "[pscustomobject]@{ legType = 'speed' }", "[pscustomobject]@{ }", "$null",
                 "[pscustomobject]@{ legType = 'export'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' } }")
        self.assertEqual(self.arms([f"Get-FpsArmFromLegSpec ({s})" for s in specs]), ["ARM=UNKNOWN"] * len(specs))

    def test_a_malformed_arm_value_is_unknown(self) -> None:
        specs = [f"[pscustomobject]@{{ legType = 'speed'; generatorArgs = [pscustomobject]@{{ telemetryArm = '{v}' }} }}" for v in ("light", "MEDIUM", "", "LIGHT ")]
        self.assertEqual(self.arms([f"Get-FpsArmFromLegSpec ({s})" for s in specs]), ["ARM=UNKNOWN"] * len(specs))

    def test_the_committed_spec_corpus_classifies_by_its_own_field(self) -> None:
        specs = sorted(LEGS_DIR.glob("*.json"))
        self.assertGreater(len(specs), 100)
        expected = {}
        for path in specs:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("legType") == "export":
                expected[path.name] = "UNKNOWN"
            else:
                args = doc.get("generatorArgs", {})
                arm = args.get("telemetryArm", "UNKNOWN")
                # r2 (adversary A2): a spec that also enables an observer control is not proof of LIGHT (the independent oracle: the five keys named in OBSERVER_SPEC_KEYS)
                observer_on = any(
                    (k in args) and ((isinstance(args[k], (int, float)) and not isinstance(args[k], bool) and args[k] >= 0) if k == "cudaLaunchTimingMinMs"
                                     else (args[k] is not False and args[k] is not None))
                    for k in OBSERVER_SPEC_KEYS)
                expected[path.name] = "UNKNOWN" if (arm == "LIGHT" and observer_on) else arm
        self.assertIn("HEAVY", expected.values())
        self.assertIn("LIGHT", expected.values())
        probe = ("foreach ($f in Get-ChildItem -LiteralPath '" + str(LEGS_DIR) + "' -Filter *.json | Sort-Object Name) {\n"
                 "  $s = Get-Content -LiteralPath $f.FullName -Raw | ConvertFrom-Json\n"
                 "  Write-Host ($f.Name + '=' + (Get-FpsArmFromLegSpec $s).arm)\n}\n")
        got = dict(line.split("=", 1) for line in self.ok(probe).splitlines() if "=" in line)
        self.assertEqual(got, expected)

    def test_a_recorded_result_that_carries_the_arm_classifies_by_it(self) -> None:
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'HEAVY' })"), "ARM=HEAVY")
        # r2 pass 5 A2: a result that records no observer controls cannot prove there were none (provenance.json carries telemetryArm only), so alone it is UNKNOWN
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT' })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ utilSample = $false } })"), "ARM=LIGHT")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ sourceCommit = 'x' })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult $null"), "ARM=UNKNOWN")

    def test_spec_and_result_must_agree(self) -> None:
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_LIGHT}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})"), "ARM=LIGHT")
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_LIGHT}) -Result ([pscustomobject]@{{ telemetryArm = 'HEAVY' }})"), "ARM=UNKNOWN")
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_HEAVY}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArm -Spec $null -Result ([pscustomobject]@{ telemetryArm = 'LIGHT' })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArm -Spec $null -Result ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ cudaLaunchTimingMinMs = -1 } })"), "ARM=LIGHT")
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
            # adversary A1: the native readers disagree on every value but "0", so "false" / "off" / "" are read toward HEAVY
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=false')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_STAGE_TIMING=false')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY=false')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_PERF_FIELD_LOG=')": "HEAVY",
            # adversary A2: Windows environment names compare ignoring case
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','mlvapp_stage_timing=1')": "HEAVY",
            "@('mlvapp_playback_smoke_telemetry=1','mlvapp_playback_smoke_telemetry_disable_frame_log=1')": "LIGHT",
            "@('MLVAPP_STAGE_TIMING=1','mlvapp_stage_timing=0')": "UNKNOWN",
            "@('MLVAPP_STAGE_TIMING=0','mlvapp_stage_timing=0')": "LIGHT",
            # adversary pass 2 A1: LIGHT is proven only against a closed list of MLVAPP_* variables; other per-frame diagnostics (named or not) are never LIGHT
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_GPU_TEX_NR_OVERLAP_TRACE=1')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_INTERACTIVE_TRACE=1')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_PHASE3_TEL_PATH=C:\\Temp\\stages.csv')": "HEAVY",
            # r4 (luna r3 B2, supersedes r2 pass 5 A2): MLVAPP_VIEWPORT_PRESENT_DIAG logs from the per-present paths (GpuDisplayViewport.cpp:1226-1231, :2014-2019), so it is an observer; the venue
            # job no longer puts it in a LIGHT leg's environment (playback-attr-3-cuda-job.ps1), so a LIGHT launch does not carry it
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_VIEWPORT_PRESENT_DIAG=1')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_VIEWPORT_PRESENT_DIAG=0')": "LIGHT",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_A_NEW_DIAGNOSTIC_NOBODY_LISTED=1')": "UNKNOWN",
            # r2 pass 5 A1: "0" is not proof of OFF for a variable nobody listed (the native mode parsers read 0 as a mode), so any presence is UNKNOWN
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_A_NEW_DIAGNOSTIC_NOBODY_LISTED=0')": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1','MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq','OTHER_VAR=1')": "LIGHT",
            "@('MLVAPP_A_NEW_DIAGNOSTIC_NOBODY_LISTED=1')": "UNKNOWN",
            # adversary pass 2 A2: no Unicode trimming of the opt-out (the native readers trim ASCII only); any whitespace or non-ASCII text is UNKNOWN
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1', \"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=$([char]0xA0)1$([char]0xA0)\")": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1 ')": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=TRUE')": "LIGHT",
            # adversary A1: a value that is not text is malformed, never its coerced text
            "[pscustomobject]@{ MLVAPP_STAGE_TIMING = @('0') }": "UNKNOWN",
            "[pscustomobject]@{ MLVAPP_STAGE_TIMING = [pscustomobject]@{ a = 1 } }": "UNKNOWN",
            "@(1, 2)": "UNKNOWN",
        }
        got = self.arms([f"Get-FpsArmFromEnvironment ({expr})" for expr in cases])
        self.assertEqual(dict(zip(cases, got)), {expr: f"ARM={want}" for expr, want in cases.items()})

    def test_the_launch_block_is_judged_on_its_arguments_too(self) -> None:
        # adversary A3: --stage-log=<file> turns the app's stage logger on whatever the environment says
        env = "environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' }"
        cases = {
            f"[pscustomobject]@{{ {env}; arguments = @() }}": "LIGHT",
            f"[pscustomobject]@{{ {env}; arguments = @('--input', 'x') }}": "LIGHT",
            f"[pscustomobject]@{{ {env}; arguments = @('--stage-log=C:\\Temp\\stage.csv') }}": "HEAVY",
            f"[pscustomobject]@{{ {env}; arguments = @('--stage-log', 'C:\\Temp\\stage.csv') }}": "HEAVY",
            f"[pscustomobject]@{{ {env}; arguments = @('-STAGE-LOG=x') }}": "HEAVY",
            f"[pscustomobject]@{{ {env}; arguments = @('/stage-log:x') }}": "HEAVY",
            f"[pscustomobject]@{{ {env}; arguments = @(5) }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env} }}": "UNKNOWN",
            "[pscustomobject]@{ arguments = @() }": "UNKNOWN",
            "$null": "UNKNOWN",
        }
        got = self.arms([f"Get-FpsArmFromLaunch ({expr})" for expr in cases])
        self.assertEqual(dict(zip(cases, got)), {expr: f"ARM={want}" for expr, want in cases.items()})

    def test_a_malformed_container_is_unknown_never_the_scalar_inside_it(self) -> None:
        # adversary A5: a one-element JSON array must not unroll to the string it holds
        exprs = ["Get-FpsArmFromJobResult (ConvertFrom-Json '{\"telemetryArm\":[\"LIGHT\"]}')",
                 "Get-FpsArmFromJobResult (ConvertFrom-Json '{\"telemetryArm\":{\"arm\":\"LIGHT\"}}')",
                 "Get-FpsArmFromLegSpec (ConvertFrom-Json '{\"legType\":\"speed\",\"generatorArgs\":{\"telemetryArm\":[\"LIGHT\"]}}')",
                 "[pscustomobject]@{ arm = (ConvertTo-FpsArmName (ConvertFrom-Json '{\"arm\":[\"LIGHT\"]}')) }"]
        self.assertEqual(self.arms(exprs), ["ARM=UNKNOWN"] * len(exprs))

    def test_a_given_spec_with_no_usable_arm_is_not_set_aside_for_a_light_result(self) -> None:
        # adversary A8
        spec = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 1 } }"
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({spec}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})"), "ARM=UNKNOWN")
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_NO_ARM}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})"), "ARM=UNKNOWN")


@requires_pwsh
class PairVerdictTests(_Probe):
    """(c) Two legs may be compared on presented fps only when BOTH are LIGHT; stage metrics stay allowed."""

    def verdict(self, baseline: str, candidate: str) -> dict:
        out = self.ok(f"$v = Get-FpsPairVerdict -Baseline {baseline} -Candidate {candidate}\n$v | ConvertTo-Json -Compress")
        return json.loads(out.strip().splitlines()[-1])

    def verdicts(self, pairs: list[tuple[str, str]]) -> list[dict]:
        body = "".join(f"(Get-FpsPairVerdict -Baseline {b} -Candidate {c}) | ConvertTo-Json -Compress\n" for b, c in pairs)
        return [json.loads(line) for line in self.ok(body).splitlines() if line.startswith("{")]

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
        for v in self.verdicts([("'LIGHT'", "'HEAVY'"), ("'HEAVY'", "'LIGHT'")]):
            self.assertFalse(v["allowed"])
            self.assertEqual(v["reason"], HEAVY_REASON)
            self.assertIn("mixed", v["detail"])
            self.assertTrue(v["stageMetricsAllowed"])

    def test_an_unknown_leg_is_refused_like_heavy(self) -> None:
        for v in self.verdicts([("'LIGHT'", "$null"), ("$null", "'LIGHT'"), ("'LIGHT'", "'light'"), ("'UNKNOWN'", "'UNKNOWN'")]):
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

    def test_a_null_item_in_an_fps_ab_plan_is_an_unknown_leg_not_a_dropped_one(self) -> None:
        # adversary A7
        out = self.ok("$r = Get-FpsLegPlanRefusal -Purpose 'fps-ab' -Items @([pscustomobject]@{ item = 1; arm = 'LIGHT' }, $null)\nWrite-Host ('R=' + ($r -join '|'))\n"
                      "$v = Get-FpsArmsVerdict -Legs @([pscustomobject]@{ name = 'a'; arm = 'LIGHT' }, $null)\nWrite-Host ('V=' + $v.allowed + '|' + $v.reason + '|' + ($v.unknown -join ','))")
        r_line = next(line for line in out.splitlines() if line.startswith("R="))
        v_line = next(line for line in out.splitlines() if line.startswith("V="))
        self.assertTrue(r_line.startswith("R=FPS_AB_UNKNOWN_ARM|2|"), r_line)
        self.assertEqual(v_line, f"V=False|{UNKNOWN_REASON}|leg-2")

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

    @requires_windows_pwsh
    def test_fps_ab_with_a_heavy_leg_is_refused_before_any_claim(self) -> None:
        light = self.spec("fx-light", {"telemetryArm": "LIGHT", "presentTrace": True})
        heavy = self.spec("fx-heavy", {"telemetryArm": "HEAVY"})
        r = self.run_plan([light, heavy], "fps-ab")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=FPS_AB_HEAVY_ARM item=2 ", r.stderr)
        self.assertIn(HEAVY_REASON, r.stderr)
        self.assertFalse((self.tmp / "claims").exists(), "nothing was claimed")

    @requires_windows_pwsh
    def test_fps_ab_with_a_leg_that_names_no_arm_is_refused(self) -> None:
        r = self.run_plan([self.spec("fx-a", {"telemetryArm": "LIGHT"}), self.spec("fx-b", {"unpaced": True})], "fps-ab")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=FPS_AB_UNKNOWN_ARM item=2 ", r.stderr)

    def test_the_arm_is_judged_from_the_bytes_the_leg_is_bound_to(self) -> None:
        # adversary A4: Get-LegSpecTelemetryArm must hash its read against the digest the preflight judged (the one -ExpectSpecSha256 binds the leg to). A spec shown as LIGHT to this
        # read but judged (and later run) as HEAVY is UNKNOWN. Real function text, stubbed reads.
        source = LEASE_BATCH_SCRIPT.read_text(encoding="utf-8")

        def fn(name: str) -> str:
            m = re.search(r"function " + re.escape(name) + r"\(.*?\n\}\r?\n", source, re.DOTALL)
            self.assertIsNotNone(m, name)
            return m.group(0)

        light = json.dumps({"legType": "speed", "generatorArgs": {"telemetryArm": "LIGHT"}})
        heavy = json.dumps({"legType": "speed", "generatorArgs": {"telemetryArm": "HEAVY"}})
        script = self.tmp / "toctou.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n" f"Import-Module '{MODULE}' -Force\n" + fn("Get-BytesSha256") + fn("Get-LegSpecTelemetryArm") +
            "function Get-SpecReadBoundSec { 5.0 }\n"
            f"function Read-SpecBytesWithin($Path, $Bound) {{ [pscustomobject]@{{ state = 'OK'; bytes = [Text.Encoding]::UTF8.GetBytes('{light}'); error = '' }} }}\n"
            f"$heavyBytes = [Text.Encoding]::UTF8.GetBytes('{heavy}')\n$lightBytes = [Text.Encoding]::UTF8.GetBytes('{light}')\n"
            "$script:SpecBudgetSha256 = @{}\n"
            "$script:SpecBudgetSha256['s.json'] = Get-BytesSha256 $heavyBytes\n"
            "Write-Host ('JUDGED_HEAVY_READ_LIGHT=' + (Get-LegSpecTelemetryArm 's.json').arm)\n"
            "$script:SpecBudgetSha256['s.json'] = Get-BytesSha256 $lightBytes\n"
            "Write-Host ('SAME_BYTES=' + (Get-LegSpecTelemetryArm 's.json').arm)\n"
            "$script:SpecBudgetSha256 = @{}\n"
            "Write-Host ('NO_DIGEST=' + (Get-LegSpecTelemetryArm 's.json').arm)\n", encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, creationflags=_NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.split(), ["JUDGED_HEAVY_READ_LIGHT=UNKNOWN", "SAME_BYTES=LIGHT", "NO_DIGEST=UNKNOWN"])

    @requires_windows_pwsh
    def test_the_spec_only_light_specs_luna_named_pass_the_arm_check_because_the_job_no_longer_injects_the_diag(self) -> None:
        # r4 B2 spec-only route, decision (a): the job injected MLVAPP_VIEWPORT_PRESENT_DIAG=1 into every leg, so these three specs ran a per-present logger under a LIGHT word. The job now leaves
        # it out of a LIGHT leg (test_the_venue_job_leaves_the_present_diag_out_of_a_light_leg_and_keeps_it_elsewhere in ObserverControlTests), so the spec word is true again and the gate
        # admits them; a LIGHT spec that switches an observer on is still refused (UNKNOWN arm), as is a HEAVY one.
        admitted = [{"telemetryArm": "LIGHT"}, {"telemetryArm": "LIGHT", "utilSample": False}, {"telemetryArm": "LIGHT", "corePartitionTelemetry": False}]
        for n, ga in enumerate(admitted):
            with self.subTest(generatorArgs=ga):
                r = self.run_plan([self.spec(f"fx-adm{n}a", ga), self.spec(f"fx-adm{n}b", ga)], "fps-ab")
                self.assertNotIn("FPS_AB_", r.stderr, r.stdout + r.stderr)
        for n, ga in enumerate([{"telemetryArm": "LIGHT", "utilSample": True}, {"telemetryArm": "LIGHT", "corePartitionTelemetry": True}, {"telemetryArm": "LIGHT", "firstPresentCapture": True}]):
            with self.subTest(refused=ga):
                r = self.run_plan([self.spec(f"fx-ref{n}a", {"telemetryArm": "LIGHT"}), self.spec(f"fx-ref{n}b", ga)], "fps-ab")
                self.assertIn("LEG_PLAN_REFUSED reason=FPS_AB_UNKNOWN_ARM item=2 ", r.stderr, r.stdout + r.stderr)

    @requires_windows_pwsh
    def test_an_unreadable_purpose_is_refused(self) -> None:
        # adversary pass 2 A3: the purpose is judged as parsed. A one-element array, a number and an explicit null are not the scalar string 'fps-ab' / 'stage-attribution'.
        for purpose in ("throughput", ["fps-ab"], ["stage-attribution"], 5, None):
            with self.subTest(purpose=purpose):
                r = self.run_plan([self.spec("fx-a", {"telemetryArm": "LIGHT"})], purpose)
                self.assertIn("LEG_PLAN_REFUSED reason=BAD_PURPOSE", r.stderr, r.stdout + r.stderr)

    @requires_windows_pwsh
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
        arm_part = _light_word(arm)
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

    def test_a_leg_that_carries_only_the_light_word_is_unknown_and_refused(self) -> None:
        # r5 (Dot on f51e0821): the arm word alone is not proof (the rule a recorded result already had); a leg that also records its observer list is
        bare = "[pscustomobject]@{ telemetryArm = 'LIGHT'; presentedFps = 55.0; avgQueueWaitMs = 5.0; avgDrawTotalMs = 12.0 }"
        r = self.run_ab(self.leg("LIGHT", 50.0, 5.0), bare)
        self.assertFalse(r["verdict"]["allowed"])
        self.assertEqual(r["verdict"]["reason"], UNKNOWN_REASON)
        self.assertIsNone(r["compare"]["presentedFps"]["deltaPercent"])
        r = self.run_ab(bare, self.leg("LIGHT", 55.0, 5.0))
        self.assertEqual(r["verdict"]["reason"], UNKNOWN_REASON)

    def test_a_leg_with_no_recorded_arm_is_unknown_and_refused(self) -> None:
        r = self.run_ab(self.leg(None, 54.0, 5.0), self.leg("LIGHT", 55.0, 5.0))
        self.assertEqual(r["verdict"]["reason"], UNKNOWN_REASON)
        self.assertIsNone(r["compare"]["presentedFps"]["deltaPercent"])

    def test_a_light_leg_that_records_an_enabled_observer_is_refused(self) -> None:
        # r2 pass 4 A3: the reader projected only telemetryArm, so a LIGHT leg whose record also carries utilSample (or any observer key) still compared on fps
        with_observer = "[pscustomobject]@{ telemetryArm = 'LIGHT'; utilSample = $true; presentedFps = 55.0; avgQueueWaitMs = 5.0; avgDrawTotalMs = 12.0 }"
        r = self.run_ab(self.leg("LIGHT", 50.0, 5.0), with_observer)
        self.assertFalse(r["verdict"]["allowed"])
        self.assertEqual(r["verdict"]["reason"], UNKNOWN_REASON)
        self.assertIsNone(r["compare"]["presentedFps"]["deltaPercent"])
        out = self.ok(f"$b = {self.leg('LIGHT', 50.0, 5.0)}\n$c = {self.leg('LIGHT', 51.0, 5.0)}\n$s = {with_observer}\n"
                      "(Get-PlaybackAbFpsVerdict -BaselineSummary $b -CandidateSummary $c -CandidateSpeedSummary $s -SeparateCandidateSpeedRun) | ConvertTo-Json -Depth 6 -Compress")
        v = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(v["candidate"]["allowed"])
        self.assertFalse(v["candidateSpeed"]["allowed"], "the separate speed run is judged on its whole record too")

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
            "launch = [pscustomobject]@{ arguments = @(); environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1' } }": "HEAVY",
            ("launch = [pscustomobject]@{ arguments = @(); environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; "
             "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' } }"): "LIGHT",
            ("launch = [pscustomobject]@{ arguments = @('--stage-log=C:\\Temp\\s.csv'); environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; "
             "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' } }"): "HEAVY",
            "launch = [pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1' } }": "UNKNOWN",
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
            f"$baselineSummary = [pscustomobject]@{{ telemetryArm = {baseline}; {OBSERVERS_RECORDED_OFF}presentedFps = {fps_b}; hostLoadProvisional = $false }}\n"
            f"$candidateSummary = [pscustomobject]@{{ telemetryArm = {candidate}; {OBSERVERS_RECORDED_OFF}presentedFps = {fps_c}; hostLoadProvisional = $false }}\n"
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
            arm_part = _light_word(arm)
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

    def test_a_record_with_a_speed_compare_is_judged_on_its_speed_leg(self) -> None:
        # adversary A6: the row publishes speedCompare, so the leg judged is candidateSpeed; an absent one is UNKNOWN, never the ordinary candidate
        leg = f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; {OBSERVERS_RECORDED_OFF}hostLoadProvisional = $false; hostLoadState = 'quiet' }}"
        heavy = "[pscustomobject]@{ telemetryArm = 'HEAVY'; hostLoadProvisional = $false; hostLoadState = 'quiet' }"
        fingerprint = "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
        compare = "[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 10.0; candidate = 20.0; deltaPercent = 100.0 } }"
        base = f"schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; {fingerprint}baseline = {leg}; candidate = {leg}; compare = {compare}; speedCompare = {compare}; "
        absent = self.row(f"[pscustomobject]@{{ {base}candidateSpeed = $null }}")
        self.assertIsNone(absent["playback_fps_delta_pct"])
        self.assertEqual(absent["playback_fps_arm_verdict"], UNKNOWN_REASON)
        heavy_speed = self.row(f"[pscustomobject]@{{ {base}candidateSpeed = {heavy} }}")
        self.assertIsNone(heavy_speed["playback_fps_delta_pct"])
        self.assertEqual(heavy_speed["playback_fps_arm_verdict"], HEAVY_REASON)
        light_speed = self.row(f"[pscustomobject]@{{ {base}candidateSpeed = {leg} }}")
        self.assertEqual(light_speed["playback_fps_delta_pct"], 100.0)

    def test_a_json_record_whose_arm_is_a_one_element_array_is_refused(self) -> None:
        # adversary A5: parsed JSON, the way compare-machine-perf reads a record
        record = ('{"schema":"mlvapp-cuda-playback-ab.v1","status":"success","machineFingerprint":{"schema":"machine-fingerprint.v1","hostname":"H","cpu":"x","gpu":"y","os":"z","build_sha":"abc1234"},'
                  '"baseline":{"telemetryArm":["LIGHT"],"hostLoadProvisional":false,"hostLoadState":"quiet"},'
                  '"candidate":{"telemetryArm":["LIGHT"],"hostLoadProvisional":false,"hostLoadState":"quiet"},'
                  '"compare":{"presentedFps":{"baseline":10.0,"candidate":20.0,"deltaPercent":100.0}}}')
        r = self.row(f"(ConvertFrom-Json '{record}')")
        self.assertIsNone(r["playback_fps_delta_pct"])
        self.assertEqual(r["playback_fps_arm_verdict"], UNKNOWN_REASON)

    def test_a_light_record_that_carries_an_enabled_observer_is_refused(self) -> None:
        # r2 pass 4 A3: the adversary's record (candidate carries utilSample = true) published deltaPercent 100 and the cached optimization
        def with_extra(extra: str) -> str:
            return self.record("LIGHT", "LIGHT").replace("candidate = [pscustomobject]@{ telemetryArm = 'LIGHT'; ", f"candidate = [pscustomobject]@{{ telemetryArm = 'LIGHT'; {extra}")
        for extra in ("utilSample = $true; ", "cudaLaunchTimingMinMs = 0; ", "corePartitionTelemetry = $true; ", "generatorArgs = [pscustomobject]@{ firstPresentCapture = $true }; "):
            with self.subTest(extra=extra):
                r = self.row(with_extra(extra))
                self.assertIsNone(r["playback_fps_delta_pct"])
                self.assertEqual(r["playback_fps_arm_verdict"], UNKNOWN_REASON)
                self.assertEqual(r["dominant_bottleneck"], "fps-arm-refused")
        ok = self.row(with_extra("utilSample = $false; "))
        self.assertEqual(ok["playback_fps_delta_pct"], 100.0)

    def test_a_record_whose_legs_carry_only_the_light_word_is_unknown_and_refused(self) -> None:
        # r5 (Dot on f51e0821): the saved A/B record's legs name LIGHT and record no observer controls, so the delta and the cached optimization are withheld through THIS verdict reader too
        def bare(extra: str = "") -> str:
            return f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; {extra}hostLoadProvisional = $false; hostLoadState = 'quiet' }}"
        record = self.record("LIGHT", "LIGHT").replace(f"baseline = {self.record_leg('LIGHT')}; candidate = {self.record_leg('LIGHT')};", f"baseline = {bare()}; candidate = {bare()};")
        self.assertIn(f"baseline = {bare()}", record, "the fixture replacement must land")
        r = self.row(record)
        self.assertIsNone(r["playback_fps_delta_pct"])
        self.assertEqual(r["playback_fps_arm_verdict"], UNKNOWN_REASON)
        self.assertEqual(r["dominant_bottleneck"], "fps-arm-refused")
        corroborated = self.row(self.record("LIGHT", "LIGHT"))
        self.assertEqual(corroborated["playback_fps_arm_verdict"], "allowed")

    @staticmethod
    def record_leg(arm: str | None) -> str:
        return f"[pscustomobject]@{{ {_light_word(arm)}hostLoadProvisional = $false; hostLoadState = 'quiet' }}"

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
        arm_part = _light_word(arm)
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
        return f"[pscustomobject]@{{ launch = [pscustomobject]@{{ arguments = @(); environment = [pscustomobject]@{{ {env} }} }} }}"

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

    def test_a_stage_log_argument_makes_a_smoke_heavy(self) -> None:
        # adversary A3, sibling site: the arm gate reads launch.arguments as well as launch.environment
        staged = ("[pscustomobject]@{ launch = [pscustomobject]@{ arguments = @('--stage-log=C:\\Temp\\s.csv'); "
                  f"environment = [pscustomobject]@{{ {self.LIGHT_ENV} }} }} }}")
        out = self.ok(f"$e = Get-FpsArmComparisonEvidence -BeforeSmoke ({self.smoke(self.LIGHT_ENV)}) -AfterSmoke ({staged})\n$e | ConvertTo-Json -Compress")
        e = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(e["refused"])
        self.assertEqual((e["reason"], e["after"]["arm"]), (HEAVY_REASON, "HEAVY"))

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

    def run_summarizer_block(self, baseline_arm: str, candidate_arm: str, analysis: str = "$null", candidate_extra: str = "", bare_candidate: bool = False) -> list[str]:
        candidate_word = "telemetryArm = '" + candidate_arm + "'; " if bare_candidate else _light_word(candidate_arm)
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
            f"$playbackAb = [pscustomobject]@{{ analysis = {analysis}; baseline = [pscustomobject]@{{ {_light_word(baseline_arm)}hostLoadProvisional = $false; hostLoadState = 'quiet' }}; "
            f"candidate = [pscustomobject]@{{ {candidate_word}{candidate_extra}hostLoadProvisional = $false; hostLoadState = 'quiet' }} }}\n"
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

    def test_a_refused_pair_repeats_no_cached_fps_derived_suggestion(self) -> None:
        # adversary A9 (pre-existing append, same class): the packet's analysis.suggestedOptimization is derived from the fps delta
        analysis = "[pscustomobject]@{ suggestedOptimization = 'optimize_playback_cuda_candidate' }"
        refused = "\n".join(self.run_summarizer_block("HEAVY", "HEAVY", analysis))
        self.assertNotIn("suggested_optimization=", refused)
        allowed = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", analysis))
        self.assertIn("suggested_optimization=optimize_playback_cuda_candidate", allowed)

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

    def test_a_light_leg_that_records_an_enabled_observer_is_refused_here_too(self) -> None:
        # r2 pass 4 A3: the summarizer projected only the arm word, so a candidate record with utilSample set passed as LIGHT
        text = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", candidate_extra="utilSample = $true; "))
        self.assertIn("DIAG=PLAYBACK_AB_FPS_ARM_REFUSED", text)
        self.assertIn(UNKNOWN_REASON, text)
        self.assertNotIn("PLAYBACK_PRESENTED_FPS_NOT_IMPROVED", text)
        self.assertNotIn("presented FPS: CPU", text)
        off = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", candidate_extra="utilSample = $false; "))
        self.assertNotIn("PLAYBACK_AB_FPS_ARM_REFUSED", off)

    def test_a_candidate_that_carries_only_the_light_word_is_refused_here_too(self) -> None:
        # r5 (Dot on f51e0821): the third verdict reader (summarize-local-cuda-proof.ps1:700-701) reads the same rule
        text = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", bare_candidate=True))
        self.assertIn("DIAG=PLAYBACK_AB_FPS_ARM_REFUSED", text)
        self.assertIn(UNKNOWN_REASON, text)
        self.assertNotIn("PLAYBACK_PRESENTED_FPS_NOT_IMPROVED", text)
        self.assertNotIn("presented FPS: CPU", text)

    def test_a_refused_pair_labels_the_section_refused_not_pass(self) -> None:
        # r2 advisory A4 (summarize-local-cuda-proof.ps1:761): the section verdict read PASS although its fps comparison was refused
        source = SUMMARIZER_SCRIPT.read_text(encoding="utf-8")
        verdict = _extract_block(source, "$playbackAbVerdict = New-SectionVerdict", "-Blockers @($playbackAbBlockers)", trailing_lines=0)
        self.assertIn("REFUSED", verdict)
        self.assertIn("playbackAbFpsArmRefusal", verdict)
        # a refusal never turns a blocked section into REFUSED: BLOCKED outranks it, and an allowed pair is still PASS
        self.assertRegex(verdict, r"playbackAbBlockers\.Count -ne 0\) \{ ?\"BLOCKED\"")
        self.assertIn('"PASS"', verdict)


# The five generatorArgs keys of a leg spec that turn an observer on inside the timed window (the independent oracle of the corpus test; the module holds the same list as data).
OBSERVER_SPEC_KEYS = ("cudaLaunchTimingMinMs", "corePartitionTelemetry", "utilSample", "firstPresentCapture", "warmReuseMissDump")
LIGHT_ENV = "'MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1'"


def _spec(**generator_args: str) -> str:
    """A pwsh expression for a LIGHT speed-leg spec whose generatorArgs also carry the given raw pwsh values."""
    extra = "".join(f"; {k} = {v}" for k, v in generator_args.items())
    return f"[pscustomobject]@{{ legType = 'speed'; generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT'{extra} }} }}"


@requires_pwsh
class ObserverControlTests(_Probe):
    """BENCH-LIGHT-ARM-GAIN-GATES-1 r2 (adversary pass 3 A1, A2): ONE list of observer controls, read by the env, spec and result classifiers. A LIGHT proof
    excludes every control on it. A variable whose value is DATA (a path, a file name, a threshold) is on whenever it is non-empty -- "0" is a file name and a
    threshold of zero ms, not off. A spec / result that enables a per-launch logger or a sampler is not proof of LIGHT whatever its telemetryArm says."""

    def arms(self, exprs: list[str]) -> list[str]:
        return self.ok("".join(f"Write-Host ('ARM=' + ({e}).arm)\n" for e in exprs)).split()

    def check(self, cases: dict[str, str]) -> None:
        got = self.arms(list(cases))
        self.assertEqual(dict(zip(cases, got)), {expr: f"ARM={want}" for expr, want in cases.items()})

    # --- A1: env path, the consumer decides what "on" means (each rule read from its source) ---------------------------------------------------------------
    def test_a_path_valued_variable_is_on_whenever_it_is_non_empty(self) -> None:
        # RenderFrameThread.cpp:60-62 opens a stage CSV from ANY non-empty MLVAPP_PHASE3_TEL_PATH; StageTiming.h:87-90 opens MLVAPP_STAGE_TIMING_FILE the same way;
        # CrashForensics.cpp:687 reads MLVAPP_PERF_FIELD_LOG_PATH trimmed-non-empty; GpuDisplayWindow.cpp:146 dumps into a non-empty MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR.
        def env(*pairs: str) -> str:
            return "Get-FpsArmFromEnvironment @(" + LIGHT_ENV + "," + ",".join(f"'{p}'" for p in pairs) + ")"
        self.check({
            env("MLVAPP_PHASE3_TEL_PATH=0"): "HEAVY",                       # the adversary's input
            env("MLVAPP_PHASE3_TEL_PATH=false"): "HEAVY",
            env("MLVAPP_PHASE3_TEL_PATH=C:\\Temp\\stages.csv"): "HEAVY",
            env("MLVAPP_PHASE3_TEL_PATH="): "LIGHT",                        # empty is the one value the consumer reads as off
            env("mlvapp_phase3_tel_path=0"): "HEAVY",
            env("MLVAPP_STAGE_TIMING_FILE=0"): "UNKNOWN",
            env("MLVAPP_PERF_FIELD_LOG_PATH=0"): "UNKNOWN",
            env("MLVAPP_PERF_FIELD_LOG_DIR=0"): "UNKNOWN",
            env("MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR=0"): "UNKNOWN",
            env("MLVAPP_A_NEW_DIAGNOSTIC_DUMP_DIR=0"): "UNKNOWN",           # any unlisted *_PATH / *_FILE / *_DIR is a path
            env("MLVAPP_A_NEW_DIAGNOSTIC_FILE=0"): "UNKNOWN",
            env("MLVAPP_A_NEW_DIAGNOSTIC_DUMP_DIR="): "UNKNOWN",             # an empty value is still a set variable (qEnvironmentVariableIsSet); only a LISTED consumer is known to read it as off
            env("MLVAPP_CRASH_FORENSICS_LOG_DIR=C:\\Temp\\forensics"): "LIGHT",  # configuration, on the closed benign list
            env("MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS=0"): "UNKNOWN",          # a threshold of zero ms is a value, not an off switch
            env("MLVAPP_PLAYBACK_PRESENT_TRACE=1"): "LIGHT",                # the in-memory trace is on the benign list, matching the spec classifier (presentTrace)
        })

    def test_the_adversary_input_for_a1_is_not_light_through_the_launch_block(self) -> None:
        launch = ("[pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; "
                  "MLVAPP_PHASE3_TEL_PATH = '0' }; arguments = @() }")
        out = self.ok(f"$a = Get-FpsArmFromLaunch ({launch})\nWrite-Host ('ARM=' + $a.arm)\n"
                      f"$v = Get-FpsPairVerdict -Baseline $a -Candidate $a\nWrite-Host ('ALLOWED=' + $v.allowed)")
        self.assertIn("ARM=HEAVY", out)
        self.assertIn("ALLOWED=False", out)

    # --- A2: spec / result path, the same list ----------------------------------------------------------------------------------------------------------
    def test_a_light_spec_that_enables_an_observer_is_not_light(self) -> None:
        # Invoke-VenueLeg.ps1:538 forwards cudaLaunchTimingMinMs; playback-attr-3-cuda-job.ps1 -ge 0 turns the per-launch logger on, so 0 is ON
        self.check({
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='0')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='0.5')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='2.5')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='-1')})": "LIGHT",       # -1 is the generator default: nothing set
            f"Get-FpsArmFromLegSpec ({_spec(corePartitionTelemetry='$true')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(corePartitionTelemetry='$false')})": "LIGHT",
            f"Get-FpsArmFromLegSpec ({_spec(utilSample='$true')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(firstPresentCapture='$true')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(warmReuseMissDump='$true')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(presentTrace='$true')})": "LIGHT",             # in-memory trace, written after the Play (PlaybackPresentTrace.h)
            f"Get-FpsArmFromLegSpec ({_spec(corePartitionTelemetry=chr(39) + 'false' + chr(39))})": "UNKNOWN",   # the forwarder's [bool]'false' is $true
        })

    def test_a_present_but_null_threshold_is_on_because_the_forwarder_casts_null_to_zero(self) -> None:
        # r2 pass 4 A1: Invoke-VenueLeg.ps1:538 forwards by PRESENCE and casts [double]$null to 0, and the job's `-ge 0` then starts the per-launch logger
        adversary = ('{"schema":"mlv-app/dual-venue-leg/v1","card":"TEST","legId":"fx","legType":"speed","clipId":"C1","backends":["cpu","cuda"],"scaleFactor":1,'
                     '"generatorArgs":{"telemetryArm":"LIGHT","cudaLaunchTimingMinMs":null}}')
        self.check({
            f"Get-FpsArmFromLegSpec (ConvertFrom-Json '{adversary}')": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='$null')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='$false')})": "UNKNOWN",   # [double]$false is 0
            f"Get-FpsArmFromLegSpec ({_spec(corePartitionTelemetry='$null')})": "LIGHT",      # a bool key's [bool]$null is false: absent and null agree
            f"Get-FpsArmFromLegSpec ({_spec(utilSample='$null')})": "LIGHT",
            "Get-FpsArmFromJobResult (ConvertFrom-Json '{\"telemetryArm\":\"LIGHT\",\"cudaLaunchTimingMinMs\":null}')": "UNKNOWN",
        })

    def test_the_recon_output_validation_flag_is_a_per_frame_observer(self) -> None:
        # r2 pass 4 A2: MainWindow.cpp:404-409 / RenderFrameThread.cpp:696-702 (set and not "0") run a GL readback, SHA256 and a CPU recon replay per presented frame
        def env(*pairs: str) -> str:
            return "Get-FpsArmFromEnvironment @(" + LIGHT_ENV + "," + ",".join(f"'{p}'" for p in pairs) + ")"
        adversary = ("[pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; MLVAPP_GPU_PLAYBACK_RECON = '1'; "
                     "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = '1'; MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT = '1'; "
                     "MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT = '1'; MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT_SAMPLE_EVERY = '1' }")
        self.check({
            f"Get-FpsArmFromEnvironment ({adversary})": "HEAVY",
            env("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=1"): "HEAVY",
            env("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=true"): "HEAVY",
            env("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=0"): "LIGHT",                 # the speed leg of run-release-cuda-playback-ab.ps1:1378
            env("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=0", "MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT_SAMPLE_EVERY=4"): "LIGHT",   # inert without the flag
            env("MLVAPP_GPU_PLAYBACK_RECON=1", "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT=1", "MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT=1"): "LIGHT",   # a route, not an observer
            env("MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1"): "LIGHT",                # deduped to one line per session (playback-attr-3-cuda-job.ps1:268-277)
        })

    def test_the_record_helper_judges_the_whole_leg_not_its_arm_word(self) -> None:
        self.check({
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT' })": "UNKNOWN",   # r5: the bare arm word is not proof (the rule a recorded result already had)
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {OBSERVERS_RECORDED_OFF}}})": "LIGHT",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT'; utilSample = $true })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT'; generatorArgs = [pscustomobject]@{ cudaLaunchTimingMinMs = 0 } })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'HEAVY'; utilSample = $true })": "HEAVY",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x') })": "LIGHT",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x'); firstPresentCapture = $true })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = (New-FpsArm 'HEAVY' 'environment' 'x') })": "HEAVY",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = @('LIGHT') })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ sourceCommit = 'x' })": "UNKNOWN",
            "Get-FpsArmFromLegRecord $null": "UNKNOWN",
        })

    def test_an_unlisted_variable_is_unknown_whatever_its_value(self) -> None:
        # r2 pass 5 A1: MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_WB_PROBE=0 is a MODE (raw_processing.c:346 processing_parse_probe_mode: mode 0 enables the probe timing); the dual-iso
        # probes read their value the same way (dualiso.c:304, :396). "0" is only proof of off for a consumer this module lists.
        def env(*pairs: str) -> str:
            return "Get-FpsArmFromEnvironment @(" + LIGHT_ENV + "," + ",".join(f"'{p}'" for p in pairs) + ")"
        self.check({
            env("MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_WB_PROBE=0"): "UNKNOWN",   # the adversary's input
            env("MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_WB_PROBE=1"): "UNKNOWN",
            env("MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_WB_PROBE="): "UNKNOWN",
            env("MLVAPP_SHADOWS_HIGHLIGHTS_PROBE=0"): "UNKNOWN",
            env("MLVAPP_DUALISO_MIX_CHROMA_PROBE=0"): "UNKNOWN",
            env("MLVAPP_DUALISO_FULL20_FINAL_BLEND_PROBE=0"): "UNKNOWN",
            env("mlvapp_dualiso_mix_halfres_probe=0"): "UNKNOWN",
            env("NOT_AN_MLVAPP_VARIABLE=0", "QT_OPENGL=desktop"): "LIGHT",             # only the MLVAPP_ namespace is the app's own control surface
        })

    def test_every_variable_the_job_generator_emits_is_classified(self) -> None:
        # r2 pass 5 A2: the spec classifier said LIGHT for a leg whose job adds MLVAPP_VIEWPORT_PRESENT_DIAG=1, which the env classifier called HEAVY. The class guard: each MLVAPP_* name
        # the generator itself writes is either on the observer list (not LIGHT when set) or a standing configuration the closed benign list holds (LIGHT), so the classifiers cannot disagree
        # about the generator's own environment. The two arm switches are the arm itself.
        job = (PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        names = sorted(set(re.findall(r"MLVAPP_[A-Z0-9_]+(?==)", job)) - {"MLVAPP_PLAYBACK_SMOKE_TELEMETRY", "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG"})
        self.assertGreater(len(names), 30)
        body = ("$observerEnv = @(Get-FpsObserverControls | Where-Object { $_.envVar } | ForEach-Object { $_.envVar })\n"
                f"foreach ($n in @({','.join(repr(n) for n in names)})) {{\n"
                f"  $arm = (Get-FpsArmFromEnvironment @({LIGHT_ENV}, ($n + '=1'))).arm\n"
                "  Write-Host ('ROW=' + $n + '|' + $arm + '|' + ($observerEnv -contains $n))\n}\n")
        wrong = []
        for line in self.ok(body).splitlines():
            if line.startswith("ROW="):
                name, arm, is_observer = line[4:].split("|")
                if (is_observer == "True") == (arm == "LIGHT"):
                    wrong.append(f"{name}: arm={arm} observer={is_observer}")
        self.assertEqual(wrong, [], "classify each name the generator emits: add it to the observer list or to FpsArmKnownBenignEnv")

    def test_a_result_that_records_observer_controls_is_judged_on_them(self) -> None:
        self.check({
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ utilSample = $true } })": "UNKNOWN",
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ cudaLaunchTimingMinMs = 0 } })": "UNKNOWN",
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ cudaLaunchTimingMinMs = -1; utilSample = $false } })": "LIGHT",
            # the adversary's result-only input: provenance of a valid -TelemetryArm LIGHT -UtilSample job carries the arm word and no observer record
            "Get-FpsArmFromJobResult (ConvertFrom-Json '{\"rangeHeadSha\":\"x\",\"telemetryArm\":\"LIGHT\"}')": "UNKNOWN",
            # a spec supplies the observer record, so a bare LIGHT result under a clean LIGHT spec still agrees
            f"Get-FpsArm -Spec ({_spec()}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})": "LIGHT",
            f"Get-FpsArm -Spec ({_spec(utilSample='$true')}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})": "UNKNOWN",
        })

    def test_no_reader_projects_the_arm_word_out_of_a_leg_record(self) -> None:
        # The class guard for A3: a gain/cost reader hands Get-FpsPairVerdict the arm of the WHOLE leg record (Get-FpsArmFromLegRecord), never `Get-FaProp <leg> "telemetryArm"`,
        # which drops the observer fields the record carries.
        offenders = []
        for script in sorted(PROFILING.glob("*.ps1")):
            for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), start=1):
                if re.search(r"Get-FaProp\s+[^\r\n]*[\"']telemetryArm[\"']", line) and not line.lstrip().startswith("#"):
                    offenders.append(f"{script.name}:{number}")
        self.assertEqual(offenders, [], "use Get-FpsArmFromLegRecord <leg> so the leg's observer fields are judged with its arm")

    def test_the_adversary_input_for_a2_gets_no_fps_ab_approval_with_or_without_a_result(self) -> None:
        spec = _spec(cudaLaunchTimingMinMs="0")
        out = self.ok(f"$s = {spec}\n$r = [pscustomobject]@{{ telemetryArm = 'LIGHT' }}\n"
                      "$a = Get-FpsArm -Spec $s -Result $null\nWrite-Host ('SPEC=' + $a.arm)\n"
                      "$b = Get-FpsArm -Spec $s -Result $r\nWrite-Host ('BOTH=' + $b.arm)\n"
                      "$v = Get-FpsPairVerdict -Baseline $b -Candidate $b\nWrite-Host ('ALLOWED=' + $v.allowed)\n"
                      "$p = Get-FpsLegPlanRefusal -Purpose 'fps-ab' -Items @([pscustomobject]@{ item = 1; arm = $a })\nWrite-Host ('PLAN=' + ($p -join '|'))")
        self.assertIn("SPEC=UNKNOWN", out)
        self.assertIn("BOTH=UNKNOWN", out)
        self.assertIn("ALLOWED=False", out)
        self.assertIn("PLAN=FPS_AB_UNKNOWN_ARM|1|", out)

    def test_a_recorded_result_that_carries_an_enabled_observer_is_not_light(self) -> None:
        self.check({
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; cudaLaunchTimingMinMs = 0 })": "UNKNOWN",
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; generatorArgs = [pscustomobject]@{ utilSample = $true } })": "UNKNOWN",
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; corePartitionTelemetry = $false })": "LIGHT",
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'HEAVY'; utilSample = $true })": "HEAVY",
        })

    # --- the shared list --------------------------------------------------------------------------------------------------------------------------------
    def test_one_list_drives_the_three_classifiers(self) -> None:
        # Every control the module lists must refuse LIGHT in the classifier of each surface it has (spec key -> spec and result, env name -> env), and its OFF value must not.
        out = self.ok(
            "foreach ($c in Get-FpsObserverControls) {\n"
            "  $row = @{ id = $c.id; specKey = $c.specKey; envVar = $c.envVar; kind = $c.kind; severity = $c.severity }\n"
            "  if ($c.specKey) {\n"
            "    $on = switch ($c.specKind) { 'threshold' { 0 } default { $true } }\n"
            "    $off = switch ($c.specKind) { 'threshold' { -1 } default { $false } }\n"
            "    $mk = { param($v) $ga = [pscustomobject]@{ telemetryArm = 'LIGHT' }; $ga | Add-Member -NotePropertyName $c.specKey -NotePropertyValue $v; [pscustomobject]@{ legType = 'speed'; generatorArgs = $ga } }\n"
            "    $row.specOn = (Get-FpsArmFromLegSpec (& $mk $on)).arm; $row.specOff = (Get-FpsArmFromLegSpec (& $mk $off)).arm\n"
            "    $rk = { param($v) $r = [pscustomobject]@{ telemetryArm = 'LIGHT' }; $r | Add-Member -NotePropertyName $c.specKey -NotePropertyValue $v; $r }\n"
            "    $row.resultOn = (Get-FpsArmFromJobResult (& $rk $on)).arm; $row.resultOff = (Get-FpsArmFromJobResult (& $rk $off)).arm\n"
            "  }\n"
            "  if ($c.envVar) {\n"
            "    $env = @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1', 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1', ($c.envVar + '=0'))\n"
            "    $row.envZero = (Get-FpsArmFromEnvironment $env).arm\n"
            "    $env2 = @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1', 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1', ($c.envVar + '='))\n"
            "    $row.envEmpty = (Get-FpsArmFromEnvironment $env2).arm\n"
            "  }\n"
            "  $row | ConvertTo-Json -Compress\n}\n")
        rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        self.assertGreaterEqual(len(rows), 12, "the list must hold the nine per-frame diagnostics plus the generator observers")
        spec_keys = {r["specKey"] for r in rows if r.get("specKey")}
        self.assertEqual(spec_keys, set(OBSERVER_SPEC_KEYS))
        for r in rows:
            if r.get("specKey"):
                self.assertEqual((r["specOn"], r["resultOn"]), ("UNKNOWN", "UNKNOWN"), r)
                self.assertEqual((r["specOff"], r["resultOff"]), ("LIGHT", "LIGHT"), r)
            if r.get("envVar"):
                # kind 'flag': "0" is off and "" reads toward on; kind 'value': "0" is a value (on) and "" is the only off
                if r["kind"] == "value":
                    self.assertNotEqual(r["envZero"], "LIGHT", r)
                    self.assertEqual(r["envEmpty"], "LIGHT", r)
                elif r["kind"] == "truthy":
                    # the app reads only 1 / true / yes / on (trimmed, any case) as on: "0" and "" are off (GpuDisplayViewport.cpp:51-60)
                    self.assertEqual((r["envZero"], r["envEmpty"]), ("LIGHT", "LIGHT"), r)
                else:
                    self.assertEqual(r["envZero"], "LIGHT", r)
                    self.assertNotEqual(r["envEmpty"], "LIGHT", r)

    # --- r4 (luna r3 blockers B1 = A1, B2 = A2) -------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _json(doc: str) -> str:
        return f"(ConvertFrom-Json '{doc}')"

    def test_a_leg_record_with_an_enabled_observer_controls_entry_is_not_light(self) -> None:
        # B1: Get-FpsArmFromLegRecord read `telemetryArm` and ignored `observerControls`, so each of luna's three literal records was LIGHT and a LIGHT/LIGHT pair was allowed.
        # Callers that must refuse them through this reader: compare-machine-perf.ps1:498-499, run-release-cuda-playback-ab.ps1:351-355, summarize-local-cuda-proof.ps1:700-701.
        literal = [
            '{"telemetryArm":"LIGHT","observerControls":{"utilSample":true}}',
            '{"telemetryArm":"LIGHT","observerControls":{"corePartitionTelemetry":true}}',
            '{"telemetryArm":"LIGHT","observerControls":{"firstPresentCapture":true}}',
        ]
        self.check({f"Get-FpsArmFromLegRecord {self._json(doc)}": "UNKNOWN" for doc in literal})
        out = self.ok("".join(
            f"$a = Get-FpsArmFromLegRecord {self._json(doc)}\n$v = Get-FpsPairVerdict -Baseline $a -Candidate $a\nWrite-Host ('PAIR=' + $v.allowed + '|' + $v.reason)\n" for doc in literal))
        self.assertEqual(out.split(), [f"PAIR=False|{UNKNOWN_REASON}"] * len(literal))

    def test_a_leg_record_reads_every_kind_of_enabled_observer_control(self) -> None:
        def rec(controls: str) -> str:
            return f"Get-FpsArmFromLegRecord {self._json('{' + chr(34) + 'telemetryArm' + chr(34) + ':' + chr(34) + 'LIGHT' + chr(34) + ',' + chr(34) + 'observerControls' + chr(34) + ':' + controls + '}')}"
        self.check({
            rec('{"cudaLaunchTimingMinMs":0}'): "UNKNOWN",             # the threshold's own rule: 0 is ON (the job's -ge 0)
            rec('{"warmReuseMissDump":true}'): "UNKNOWN",
            rec('{"someObserverNobodyListed":true}'): "UNKNOWN",       # an unlisted entry: true
            rec('{"someObserverNobodyListed":1}'): "UNKNOWN",          # a non-zero number
            rec('{"someObserverNobodyListed":2.5}'): "UNKNOWN",
            rec('{"someObserverNobodyListed":"true"}'): "UNKNOWN",     # a non-empty truthy string
            rec('{"someObserverNobodyListed":"yes"}'): "UNKNOWN",
            rec('{"utilSample":false,"corePartitionTelemetry":true}'): "UNKNOWN",   # one enabled entry among disabled ones
            # fail closed: present but not an object, or an entry that cannot be read as a boolean
            rec('"utilSample"'): "UNKNOWN",
            rec('5'): "UNKNOWN",
            rec('true'): "UNKNOWN",
            rec('["utilSample"]'): "UNKNOWN",
            rec('[]'): "UNKNOWN",
            rec('null'): "UNKNOWN",
            rec('{"someObserverNobodyListed":null}'): "UNKNOWN",
            rec('{"someObserverNobodyListed":[true]}'): "UNKNOWN",
            rec('{"someObserverNobodyListed":{"on":true}}'): "UNKNOWN",
            rec('{"someObserverNobodyListed":""}'): "UNKNOWN",
            # admission controls: all-false (and the threshold's -1) stays LIGHT; an EMPTY list records nothing, so it is the bare word again (UNKNOWN, r5)
            rec('{}'): "UNKNOWN",
            rec('{"utilSample":false,"corePartitionTelemetry":false,"firstPresentCapture":false,"warmReuseMissDump":false,"cudaLaunchTimingMinMs":-1}'): "LIGHT",
            rec('{"someObserverNobodyListed":false}'): "LIGHT",
            rec('{"someObserverNobodyListed":0}'): "LIGHT",
        })

    def test_a_bare_light_leg_record_is_unknown_and_the_pair_is_refused(self) -> None:
        # r5 (Dot on f51e0821, bus 4cd3358 cos-feedback/mlv-app/pr-871.md; Grok, PR comment 7039843864): a saved leg whose only evidence is the arm WORD read LIGHT here while a recorded
        # result with the same word was refused (Get-FpsArmFromJobResult, no observer record). One evidence rule now: the word is proof only beside a recorded observer list.
        bare = self._json('{"telemetryArm":"LIGHT"}')
        empty = self._json('{"telemetryArm":"LIGHT","observerControls":{}}')
        recorded_off = self._json('{"telemetryArm":"LIGHT","observerControls":{"utilSample":false}}')
        heavy = self._json('{"telemetryArm":"HEAVY","observerControls":{"utilSample":true}}')
        self.check({
            f"Get-FpsArmFromLegRecord {bare}": "UNKNOWN",
            f"Get-FpsArmFromLegRecord {empty}": "UNKNOWN",
            f"Get-FpsArmFromLegRecord {recorded_off}": "LIGHT",
            f"Get-FpsArmFromLegRecord {heavy}": "HEAVY",
            # the boundary the reader shares with a recorded result: the same record reads the same arm through both
            f"Get-FpsArmFromJobResult {bare}": "UNKNOWN",
            f"Get-FpsArmFromJobResult {empty}": "UNKNOWN",
            f"Get-FpsArmFromJobResult {recorded_off}": "LIGHT",
        })
        # through the verdict reader every caller uses (Get-FpsPairVerdict): an uncorroborated bare LIGHT/LIGHT pair is refused as UNKNOWN, a corroborated pair is allowed
        out = self.ok(
            f"$b = Get-FpsArmFromLegRecord {bare}\n$v = Get-FpsPairVerdict -Baseline $b -Candidate $b\nWrite-Host ('BARE=' + $v.allowed + '|' + $v.reason)\n"
            f"$o = Get-FpsArmFromLegRecord {recorded_off}\n$v = Get-FpsPairVerdict -Baseline $o -Candidate $o\nWrite-Host ('OFF=' + $v.allowed + '|' + $v.reason)\n")
        self.assertEqual(out.split(), [f"BARE=False|{UNKNOWN_REASON}", "OFF=True|"])

    def test_a_job_result_reads_observer_controls_the_same_way(self) -> None:
        # sibling sweep: Get-FpsArmFromJobResult already read observerControls, but skipped a malformed one; the leg-record and result readers now share one rule
        def res(controls: str) -> str:
            return "Get-FpsArmFromJobResult " + self._json('{"telemetryArm":"LIGHT","observerControls":' + controls + '}')
        self.check({
            res('{"someObserverNobodyListed":true}'): "UNKNOWN",
            res('"utilSample"'): "UNKNOWN",
            res('["utilSample"]'): "UNKNOWN",
            res('{"someObserverNobodyListed":null}'): "UNKNOWN",
            res('{"utilSample":false}'): "LIGHT",
            res('{"someObserverNobodyListed":0}'): "LIGHT",
        })

    def test_viewport_present_diag_is_an_active_observer_in_every_spelling_the_app_accepts(self) -> None:
        # B2: GpuDisplayViewport.cpp:62-71 envFlagEnabled reads the value trimmed and lowercased: 1 / true / yes / on are ON, everything else (0, false, off, no, empty) is OFF; the app logs
        # from the per-present paths when it is on (:1226-1231 setPresentedImage, :2014-2019 updateTextureIfNeeded). It was on the benign list, so a LIGHT-looking launch passed.
        def launch(value: str | None) -> str:
            extra = "" if value is None else f"; MLVAPP_VIEWPORT_PRESENT_DIAG = '{value}'"
            return ("Get-FpsArmFromLaunch ([pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; "
                    f"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'{extra} }}; arguments = @() }})")
        cases = {launch(v): "HEAVY" for v in ("1", "true", "on", "yes", "TRUE", "On", "YES", " 1 ", "1\t", "true\r\n")}
        cases.update({launch(v): "LIGHT" for v in (None, "0", "false", "off", "no", "", "FALSE", "Off")})
        self.check(cases)
        # the key compares ignoring case, and the list form of the launch environment reads the same
        self.check({
            "Get-FpsArmFromEnvironment @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','mlvapp_viewport_present_diag=on')": "HEAVY",
            "Get-FpsArmFromEnvironment @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_VIEWPORT_PRESENT_DIAG=0')": "LIGHT",
        })
        out = self.ok("$a = " + launch("1") + "\n$v = Get-FpsPairVerdict -Baseline $a -Candidate $a\nWrite-Host ('ALLOWED=' + $v.allowed + '|' + $v.reason)")
        self.assertIn(f"ALLOWED=False|{HEAVY_REASON}", out)

    def test_the_venue_job_leaves_the_present_diag_out_of_a_light_leg_and_keeps_it_elsewhere(self) -> None:
        # B2, spec-only route: the job puts MLVAPP_VIEWPORT_PRESENT_DIAG=1 in $envs unconditionally (playback-attr-3-cuda-job.ps1 :3061), so a spec that says only telemetryArm LIGHT
        # (Invoke-VenueLeaseBatch.ps1 Get-LegSpecTelemetryArm) ran with a per-present logger. The generated LIGHT leg must not carry it; every other arm keeps the line.
        job = (PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        block = _extract_block(job, "$telemetryArmEnvs = if ($TelemetryArm -eq 'LIGHT') {", "$envList = ", trailing_lines=-1)
        body = "".join(
            f"$TelemetryArm = '{arm}'; $reconDll = 'x'; $pkgDir = 'pkg'; $DisablePaintPerSubmit = {flag}\n{block}\nWrite-Host ('ENVS={arm}|{flag}|' + ($envs -join ','))\n"
            for arm, flag in (("LIGHT", "$false"), ("LIGHT", "$true"), ("HEAVY", "$false"), ("HEAVY", "$true"), ("", "$false")))
        rows = {}
        for line in self.ok(body).splitlines():
            if line.startswith("ENVS="):
                key, value = line[5:].rsplit("|", 1)[0], line.split("|", 2)[2]
                rows[key] = value.split(",")
        diag = "MLVAPP_VIEWPORT_PRESENT_DIAG=1"
        self.assertEqual(len(rows), 5, rows)
        for key, envs in rows.items():
            if key.startswith("LIGHT"):
                self.assertNotIn(diag, envs, key)
                self.assertIn("MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1", envs)
                self.assertIn("MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT=1", envs)   # the rest of the standing environment is untouched
            else:
                self.assertIn(diag, envs, key)                                   # every non-LIGHT leg keeps it (the generator default, an absent arm, is HEAVY)
        self.assertEqual(rows["LIGHT|$true"][-1], "MLVAPP_GPU_WINDOW_PAINT_PER_SUBMIT=0")
        self.assertIn("MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY=1", rows["HEAVY|$false"])
        # a LIGHT leg differs from a HEAVY one by the arm switches and the diag line only: dropping the diag from the HEAVY list leaves the LIGHT list's common part in the same order
        common = [e for e in rows["LIGHT|$false"] if e in rows["HEAVY|$false"]]
        self.assertEqual(common, [e for e in rows["HEAVY|$false"] if e in rows["LIGHT|$false"]])
        self.assertEqual(sorted(set(rows["HEAVY|$false"]) - set(rows["LIGHT|$false"]) - {diag}),
                         sorted(["MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=0", "MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY=1", "MLVAPP_PLAYBACK_DETAILED_TIMELINE_TELEMETRY=1",
                                "MLVAPP_STAGE_TIMING=1", "MLVAPP_PERF_FIELD_LOG=1"]))

    def test_every_generator_arg_the_venue_leg_forwards_is_classified(self) -> None:
        # The class guard: a NEW generatorArgs key must be named observer (module list) or not-an-observer (below), so none reaches the job unclassified.
        source = (PROFILING / "dual-venue" / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        keys = set(re.findall(r"\$spec\.generatorArgs\.PSObject\.Properties\['(\w+)'\]", source))
        self.assertGreater(len(keys), 30)
        not_observers = {
            "telemetryArm", "cpuQuiescenceThresholdPercent", "playbackProcessing", "qualityMode", "swapInterval", "playbackConform", "rawPrefetchDecoders", "benchAffinityMask",
            "partitionLoadMask", "partitionLoadThreads", "corePartitionHelperMask", "playbackMaxThreads", "gpuFaultInject", "gpuSafeStart", "automationSettingsDir", "gpuPreLaunches",
            "disableLookAssist", "presentTrace", "unpaced", "disablePrepPointwiseFast", "prepConflatedMove", "disableReconReleaseDeferred", "dueWakeDeliveryPost", "reconOmpThreads",
            "prepOmpThreads", "scope", "scopeIntervalMs", "scopesDuringPlayback", "forceLookAssist", "lookFlavor", "playbackRenderLookaheadFrames", "lookReceipt", "lookReceiptSha256",
        }
        out = self.ok("(Get-FpsObserverControls | Where-Object { $_.specKey } | ForEach-Object { $_.specKey }) -join ','")
        observers = set(out.strip().split(","))
        self.assertEqual(sorted(keys - observers - not_observers), [], "classify each new generatorArgs key: add it to Get-FpsObserverControls or to not_observers here")


if __name__ == "__main__":
    unittest.main()
