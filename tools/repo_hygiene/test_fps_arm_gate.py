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


# the launch block a LIGHT smoke records (what the wrapper's leg record carries beside its arm): the members EVERY producer launch records (run-release-gui-smoke.ps1:1481-1503 arguments and the
# crash-log directory, the table rows marked required), frame telemetry on, the per-frame log opted out. It matches the producers' exact LIGHT shape (FpsArmGate.psm1 $script:FpsLightArgs /
# $script:FpsLightEnv), so it is PROOF of LIGHT (BENCH-LIGHT-ARM-GAIN-GATES-1 r8: a bare LIGHT word, an observerControls list, or an empty launch is not).
PRODUCER_ARGS_PS = ("'--gui-smoke-playback', '--input', 'C:\\clip.mlv', '--seconds', '7', '--start-frame', '0', '--settle-ms', '2500', '--settle-cpu-percent', '10', "
                    "'--settle-cpu-stable-ms', '1000', '--settle-cpu-max-ms', '45000'")
PRODUCER_LOGDIR_PS = "MLVAPP_CRASH_FORENSICS_LOG_DIR = 'C:\\logs'"
LIGHT_LAUNCH_ENV_PS = f"MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; {PRODUCER_LOGDIR_PS}"
RECORDED_LIGHT_LAUNCH = f"launch = [pscustomobject]@{{ environment = [pscustomobject]@{{ {LIGHT_LAUNCH_ENV_PS} }}; arguments = @({PRODUCER_ARGS_PS}) }}"
# the same launch as JSON text (a parsed saved record): used where a test feeds ConvertFrom-Json
PRODUCER_ARGS_JSON = ('["--gui-smoke-playback","--input","C:\\\\clip.mlv","--seconds","7","--start-frame","0","--settle-ms","2500","--settle-cpu-percent","10",'
                      '"--settle-cpu-stable-ms","1000","--settle-cpu-max-ms","45000"]')
LIGHT_ENV_JSON = '{"MLVAPP_PLAYBACK_SMOKE_TELEMETRY":"1","MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG":"1","MLVAPP_CRASH_FORENSICS_LOG_DIR":"C:\\\\logs"}'
# the evidence fragment a LIGHT leg-record literal carries (r5 carried an observerControls list here; r8 deleted that evidence: the recorded launch is the proof)
LIGHT_EVIDENCE = RECORDED_LIGHT_LAUNCH + "; "


def _light_word(arm: str | None) -> str:
    """The `telemetryArm` (and recorded launch for LIGHT) fragment of a pwsh leg-record literal; empty for no arm."""
    if arm is None:
        return ""
    return f"telemetryArm = '{arm}'; " + (LIGHT_EVIDENCE if arm == "LIGHT" else "")


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

    def test_r15_a1_a_singleton_array_root_or_envelope_is_refused_by_the_raw_check(self) -> None:
        # r15 (adversary r14 pass 18 A1): ConvertFrom-Json unwraps a one-element array root, and member enumeration projects `.proof.playbackAb` / `.launch` through an array, so a wrapped
        # valid LIGHT record reached the gate as the record inside it. The raw check every judged reader calls first refuses a non-object root and an array under an object envelope;
        # ordinary lists (arguments, legs, failures) stay allowed.
        light = '{"smokePresentedFps":55,"launch":{"environment":' + LIGHT_ENV_JSON + ',"arguments":' + PRODUCER_ARGS_JSON + '}}'
        cases = {
            "ROOT": "[" + light + "]",
            "PROOF": '{"proof":[{"playbackAb":{"compare":{"presentedFps":{"delta":5}}}}]}',
            "PLAYBACKAB": '{"proof":{"playbackAb":[{"compare":{"presentedFps":{"delta":5}}}]}}',
            "LAUNCH": '{"launch":[{"environment":' + LIGHT_ENV_JSON + ',"arguments":' + PRODUCER_ARGS_JSON + '}]}',
            "COMPARE": '{"speedCompare":[{"presentedFps":{"delta":5}}]}',
            "CASE": '{"PlaybackAB":[{}]}',
            "SCALARROOT": '"x"',
            "OK": light,
            "OKLIST": '{"legs":[{"spec":"a"}],"failures":[],"proof":{"playbackAb":{"proofFailures":["x"]}}}',
        }
        body = "".join(f"$p = Get-FpsJsonRepeatedKeyProblem '{text}'\nWrite-Host ('{label}=' + $(if ($null -eq $p) {{ 'NONE' }} else {{ 'PROBLEM' }}))\n" for label, text in cases.items())
        body += f"try {{ Assert-FpsJsonNoRepeatedKey '[{light}]' 'x.json'; Write-Host 'ASSERT=NONE' }} catch {{ Write-Host ('ASSERT=' + $_.Exception.Message.Split(':')[0]) }}\n"
        out = self.ok(body).split()
        expected = [f"{label}={'NONE' if label.startswith('OK') else 'PROBLEM'}" for label in cases] + ["ASSERT=FPS_JSON_REPEATED_KEY"]
        self.assertEqual(out, expected)

    def test_a1_item_shape_residue_allow_array_root_admits_a_list_root_and_nothing_else(self) -> None:
        # A1-ITEM-SHAPE-RESIDUE-1 r1: the venue lease batch's leg plan is legitimately a bare array whose entries that reader shape-checks itself (BAD_ITEM_SHAPE item=<n>), so its raw check passes
        # -AllowArrayRoot. The switch lifts ONLY the root-must-be-an-object rule: a repeated key, an envelope array, a scalar root and unparsable text are still problems, and the default stays strict.
        cases = {
            "DEFAULT_LIST": ("[{\"a\":1},null]", ""),
            "LIST_NULL": ("[{\"a\":1},null]", " -AllowArrayRoot"),
            "LIST_ONLY_NULL": ("[null]", " -AllowArrayRoot"),
            "LIST_DUP": ("[{\"k\":1,\"k\":2}]", " -AllowArrayRoot"),
            "LIST_ENVELOPE": ("[{\"proof\":[{}]}]", " -AllowArrayRoot"),
            "SCALAR": ("\"x\"", " -AllowArrayRoot"),
            "BAD": ("[oops", " -AllowArrayRoot"),
            "OBJECT": ("{\"legs\":[null]}", " -AllowArrayRoot"),
        }
        body = "".join(f"$p = Get-FpsJsonRepeatedKeyProblem '{text}'{flag}\nWrite-Host ('{label}=' + $(if ($null -eq $p) {{ 'NONE' }} else {{ 'PROBLEM' }}))\n" for label, (text, flag) in cases.items())
        body += "try { Assert-FpsJsonNoRepeatedKey '[null]' 'plan.json' -AllowArrayRoot; Write-Host 'ASSERT_OK=NONE' } catch { Write-Host 'ASSERT_OK=THREW' }\n"
        body += "try { Assert-FpsJsonNoRepeatedKey '[null]' 'plan.json'; Write-Host 'ASSERT_STRICT=NONE' } catch { Write-Host 'ASSERT_STRICT=THREW' }\n"
        out = self.ok(body).split()
        self.assertEqual(out, ["DEFAULT_LIST=PROBLEM", "LIST_NULL=NONE", "LIST_ONLY_NULL=NONE", "LIST_DUP=PROBLEM", "LIST_ENVELOPE=PROBLEM", "SCALAR=PROBLEM", "BAD=PROBLEM", "OBJECT=NONE",
                               "ASSERT_OK=NONE", "ASSERT_STRICT=THREW"])

    def test_r15_a1_the_readers_refuse_a_wrapped_record_before_the_parser_unwraps_it(self) -> None:
        # r15 (adversary r14 pass 18 A1 and siblings): compare-release-gui-smoke-ab.ps1 Read-SmokeJson and compare-machine-perf.ps1 Read-JsonRecords (whole document and JSONL line) refuse
        # a singleton-array root; the same record unwrapped is read.
        light = '{"schema":"x","launch":{"environment":' + LIGHT_ENV_JSON + ',"arguments":' + PRODUCER_ARGS_JSON + '}}'
        wrapped = self.tmp / "wrapped.json"
        wrapped.write_text("[" + light + "]", encoding="utf-8")
        jsonl = self.tmp / "wrapped.jsonl"
        jsonl.write_text(light + "\n[" + light + "]\n", encoding="utf-8")
        plain = self.tmp / "plain.json"
        plain.write_text(light, encoding="utf-8")
        funcs = _extract_functions(COMPARE_SMOKE_AB_SCRIPT, ["Read-SmokeJson"]) + "\n" + _extract_functions(COMPARE_MACHINE_PERF_SCRIPT, ["Read-JsonRecords"]) + "\n"
        body = funcs
        for label, call in (("SMOKE_WRAPPED", f"Read-SmokeJson '{wrapped}'"), ("PERF_WRAPPED", f"Read-JsonRecords '{wrapped}'"), ("PERF_JSONL", f"Read-JsonRecords '{jsonl}'"),
                            ("SMOKE_PLAIN", f"Read-SmokeJson '{plain}'"), ("PERF_PLAIN", f"Read-JsonRecords '{plain}'")):
            body += f"try {{ $null = @({call}); Write-Host '{label}=READ' }} catch {{ Write-Host '{label}=REFUSED' }}\n"
        out = self.ok(body).split()
        self.assertEqual(out, ["SMOKE_WRAPPED=REFUSED", "PERF_WRAPPED=REFUSED", "PERF_JSONL=REFUSED", "SMOKE_PLAIN=READ", "PERF_PLAIN=READ"])

    def test_a_launch_whose_arguments_are_not_a_list_is_unknown(self) -> None:
        # BENCH-LIGHT-ARM-GAIN-GATES-1 r6 (adversary pass 5 A2): `@($launchArgs)` turned the JSON string "--playback-smoke" (or " ") into a one-element list, so a launch with a scalar argument
        # value was LIGHT. The recorded argument list must BE a list (an empty one is a launch that passed nothing); anything else is UNKNOWN, and a list holding --stage-log is still HEAVY.
        out = self.ok(
            "$envJson = '\"environment\":" + LIGHT_ENV_JSON + "'\n"
            "foreach ($argText in @('[]', '\"--playback-smoke\"', '\" \"', '\"\"', 'null', '7', '[\"--stage-log=x.csv\"]', '[\"--playback-smoke\"]', '" + PRODUCER_ARGS_JSON + "')) {\n"
            "    $launch = ('{' + $envJson + ',\"arguments\":' + $argText + '}') | ConvertFrom-Json\n"
            "    $label = if ($argText.Length -gt 30) { $argText.Substring(0, 17) } else { $argText }\n"
            "    Write-Host ('ARGS=' + $label + ' ARM=' + (Get-FpsArmFromLaunch $launch).arm)\n"
            "}\n")
        got = dict(line.split(" ARM=") for line in out.strip().splitlines())
        # r8: a list holding an argument outside the producers' LIGHT shape (a stage log, an unknown flag) is UNKNOWN, not HEAVY and not LIGHT; an EMPTY list is not the producers' shape either
        # (A8: every producer launch records --gui-smoke-playback and the rest of the runner's unconditional arguments); the full producer list is LIGHT
        self.assertEqual(got, {"ARGS=[]": "UNKNOWN", 'ARGS="--playback-smoke"': "UNKNOWN", 'ARGS=" "': "UNKNOWN", 'ARGS=""': "UNKNOWN", "ARGS=null": "UNKNOWN", "ARGS=7": "UNKNOWN",
                               'ARGS=["--stage-log=x.csv"]': "UNKNOWN", 'ARGS=["--playback-smoke"]': "UNKNOWN", 'ARGS=["--gui-smoke-pla': "LIGHT"})

    def test_an_embedded_generator_args_arm_must_be_the_scalar_light(self) -> None:
        # BENCH-LIGHT-ARM-GAIN-GATES-1 r6 (adversary pass 5 A4): generatorArgs.telemetryArm embedded in a record was read through the arm-object-tolerant ConvertTo-FpsArmName, so
        # {"arm":"LIGHT"} there passed both record readers while the spec reader refuses it. The embedded value is a spec word: exactly the string LIGHT, nothing else.
        out = self.ok(
            "foreach ($embedded in @('\"LIGHT\"', '{\"arm\":\"LIGHT\"}', '[\"LIGHT\"]', '\"light\"')) {\n"
            "    $rec = ('{\"telemetryArm\":\"LIGHT\",\"legType\":\"speed\",\"generatorArgs\":{\"telemetryArm\":' + $embedded + '}}') | ConvertFrom-Json\n"
            "    Write-Host ('E=' + $embedded + ' LEG=' + (Get-FpsArmFromLegRecord $rec).arm + ' RESULT=' + (Get-FpsArmFromJobResult $rec).arm)\n"
            "}\n")
        got = {line.split(" LEG=")[0]: line.split(" LEG=")[1] for line in out.strip().splitlines()}
        self.assertEqual(got, {'E="LIGHT"': "LIGHT RESULT=LIGHT", 'E={"arm":"LIGHT"}': "UNKNOWN RESULT=UNKNOWN", 'E=["LIGHT"]': "UNKNOWN RESULT=UNKNOWN", 'E="light"': "UNKNOWN RESULT=UNKNOWN"})

    def test_a_result_only_light_word_is_never_light_through_the_observers_from_spec_override(self) -> None:
        # BENCH-LIGHT-ARM-GAIN-GATES-1 r6 (adversary A13): -ObserversFromSpec was a bare caller-supplied switch, so a result that records no observer list was admitted as LIGHT with no spec at all.
        # The override now carries the spec the caller judged the observers from: a bare switch, a null, a scalar or a boolean (no spec) is refused (a binding error or UNKNOWN), never LIGHT;
        # a real spec object still admits a result that records no observer list (Get-FpsArm passes it).
        out = self.ok(
            "$r = [pscustomobject]@{ telemetryArm = 'LIGHT' }\n"
            "$spec = [pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' } }\n"
            "foreach ($case in @('BARE', 'NULL', 'TRUE', 'TEXT', 'EMPTYLIST', 'EMPTYOBJ', 'HEAVYSPEC', 'SPEC')) {\n"
            "    try {\n"
            "        $a = switch ($case) {\n"
            "            'EMPTYLIST' { Get-FpsArmFromJobResult $r -ObserversFromSpec @() }\n"
            "            'EMPTYOBJ' { Get-FpsArmFromJobResult $r -ObserversFromSpec @{} }\n"
            "            'HEAVYSPEC' { Get-FpsArmFromJobResult $r -ObserversFromSpec ([pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'HEAVY' } }) }\n"
            "            'BARE' { Get-FpsArmFromJobResult $r -ObserversFromSpec }\n"
            "            'NULL' { Get-FpsArmFromJobResult $r -ObserversFromSpec $null }\n"
            "            'TRUE' { Get-FpsArmFromJobResult $r -ObserversFromSpec $true }\n"
            "            'TEXT' { Get-FpsArmFromJobResult $r -ObserversFromSpec 'spec' }\n"
            "            'SPEC' { Get-FpsArmFromJobResult $r -ObserversFromSpec $spec }\n"
            "        }\n"
            "        Write-Host ($case + '=' + $a.arm)\n"
            "    } catch { Write-Host ($case + '=REFUSED') }\n"
            "}\n"
            "Write-Host ('BOTH=' + (Get-FpsArm $spec $r).arm)\n")
        self.assertEqual(out.split(), ["BARE=REFUSED", "NULL=UNKNOWN", "TRUE=UNKNOWN", "TEXT=UNKNOWN", "EMPTYLIST=UNKNOWN", "EMPTYOBJ=UNKNOWN", "HEAVYSPEC=UNKNOWN", "SPEC=LIGHT", "BOTH=LIGHT"])

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
                # r2 (adversary A2): a spec that also enables an observer control is not proof of LIGHT (the independent oracle: the keys named in OBSERVER_SPEC_KEYS)
                observer_on = any(
                    (k in args) and ((isinstance(args[k], (int, float)) and not isinstance(args[k], bool) and args[k] >= 0) if k == "cudaLaunchTimingMinMs"
                                     else (args[k] is not False and args[k] is not None))
                    for k in OBSERVER_SPEC_KEYS)
                # r8 (sibling sweep of r6 A1): a LIGHT word counts only on a 'speed' leg; the 52 committed 'look' legs capture a contact sheet inside the timed Play, so they are UNKNOWN
                look_light = arm == "LIGHT" and doc.get("legType") != "speed"
                expected[path.name] = "UNKNOWN" if (arm == "LIGHT" and (observer_on or look_light)) else arm
        self.assertIn("HEAVY", expected.values())
        self.assertIn("LIGHT", expected.values())
        self.assertGreater(sum(1 for p in specs if json.loads(p.read_text(encoding="utf-8")).get("legType") == "look"), 40, "the corpus holds the look legs this rule narrows")
        probe = ("foreach ($f in Get-ChildItem -LiteralPath '" + str(LEGS_DIR) + "' -Filter *.json | Sort-Object Name) {\n"
                 "  $s = Get-Content -LiteralPath $f.FullName -Raw | ConvertFrom-Json\n"
                 "  Write-Host ($f.Name + '=' + (Get-FpsArmFromLegSpec $s).arm)\n}\n")
        got = dict(line.split("=", 1) for line in self.ok(probe).splitlines() if "=" in line)
        self.assertEqual(got, expected)

    def test_a_recorded_result_that_carries_the_arm_classifies_by_it(self) -> None:
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'HEAVY' })"), "ARM=HEAVY")
        # r2 pass 5 A2: a result that records no observer controls cannot prove there were none (provenance.json carries telemetryArm only), so alone it is UNKNOWN
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT' })"), "ARM=UNKNOWN")
        # r8: an observerControls list is no evidence at all (it is neither read nor trusted); the proof is a launch matching the producers' LIGHT shape, LIGHT generatorArgs, or a LIGHT spec
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ utilSample = $false } })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm(f"Get-FpsArmFromJobResult ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {RECORDED_LIGHT_LAUNCH} }})"), "ARM=LIGHT")
        # r10 G10: LIGHT generatorArgs alone prove LIGHT only on a speed leg (re-baselined: the same record without a legType is UNKNOWN)
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' } })"), "ARM=LIGHT")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' } })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult ([pscustomobject]@{ sourceCommit = 'x' })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArmFromJobResult $null"), "ARM=UNKNOWN")

    def test_spec_and_result_must_agree(self) -> None:
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_LIGHT}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})"), "ARM=LIGHT")
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_LIGHT}) -Result ([pscustomobject]@{{ telemetryArm = 'HEAVY' }})"), "ARM=UNKNOWN")
        self.assertEqual(self.arm(f"Get-FpsArm -Spec ({SPEC_HEAVY}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArm -Spec $null -Result ([pscustomobject]@{ telemetryArm = 'LIGHT' })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm("Get-FpsArm -Spec $null -Result ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ cudaLaunchTimingMinMs = -1 } })"), "ARM=UNKNOWN")
        self.assertEqual(self.arm(f"Get-FpsArm -Spec $null -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {RECORDED_LIGHT_LAUNCH} }})"), "ARM=LIGHT")
        self.assertEqual(self.arm("Get-FpsArm -Spec $null -Result $null"), "ARM=UNKNOWN")

    def test_launch_environment_follows_the_apps_frame_log_rule(self) -> None:
        # r8 (the narrow promise): the environment is judged against the producers' exact LIGHT shape (a closed MLVAPP_* table with exact values); the app's frame-log rule then decides
        # LIGHT (telemetry off, or on with the per-frame log opted out) from HEAVY (telemetry on, not opted out). Anything outside the table, whatever its value, is UNKNOWN.
        both = "'MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1'"
        cases = {
            "$null": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1')": "HEAVY",
            f"@({both})": "LIGHT",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=0')": "HEAVY",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=2')": "UNKNOWN",
            "@()": "LIGHT",                                     # a recorded environment that holds no MLVAPP_* control: frame telemetry is off (the real runner always records its log directory)
            "[pscustomobject]@{ }": "LIGHT",
            "@('QT_OPENGL=desktop')": "LIGHT",                  # only the MLVAPP_* namespace is the app's control surface
            "[pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1' }": "HEAVY",
            "[ordered]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' }": "LIGHT",
            # the opt-out is the exact text 1 (the table's value); every other spelling is outside the shape
            "[ordered]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = 'true' }": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=TRUE')": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1 ')": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1', \"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=$([char]0xA0)1$([char]0xA0)\")": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=false')": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=0')": "UNKNOWN",
            # Windows environment names compare ignoring case
            "@('mlvapp_playback_smoke_telemetry=1','mlvapp_playback_smoke_telemetry_disable_frame_log=1')": "LIGHT",
            # a key NOT in the table is UNKNOWN whatever it is and whatever its value: every observer, probe and trace the old denylist named, and the ones nobody has named
            f"@({both},'MLVAPP_STAGE_TIMING=1')": "UNKNOWN",
            f"@({both},'MLVAPP_STAGE_TIMING=0')": "UNKNOWN",
            f"@({both},'MLVAPP_STAGE_TIMING=false')": "UNKNOWN",
            f"@({both},'mlvapp_stage_timing=1')": "UNKNOWN",
            f"@({both},'MLVAPP_PERF_FIELD_LOG=1')": "UNKNOWN",
            f"@({both},'MLVAPP_PERF_FIELD_LOG=')": "UNKNOWN",
            f"@({both},'MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY=false')": "UNKNOWN",
            f"@({both},'MLVAPP_GPU_TEX_NR_OVERLAP_TRACE=1')": "UNKNOWN",
            f"@({both},'MLVAPP_INTERACTIVE_TRACE=1')": "UNKNOWN",
            f"@({both},'MLVAPP_PHASE3_TEL_PATH=C:\\Temp\\stages.csv')": "UNKNOWN",
            f"@({both},'MLVAPP_VIEWPORT_PRESENT_DIAG=1')": "UNKNOWN",
            f"@({both},'MLVAPP_VIEWPORT_PRESENT_DIAG=0')": "UNKNOWN",
            f"@({both},'MLVAPP_A_NEW_DIAGNOSTIC_NOBODY_LISTED=1')": "UNKNOWN",
            f"@({both},'MLVAPP_A_NEW_DIAGNOSTIC_NOBODY_LISTED=0')": "UNKNOWN",
            "@('MLVAPP_A_NEW_DIAGNOSTIC_NOBODY_LISTED=1')": "UNKNOWN",
            # a table key with a value the table does not name is UNKNOWN too
            f"@({both},'MLVAPP_PLAYBACK_QUALITY_MODE=other')": "UNKNOWN",
            f"@({both},'MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=0')": "UNKNOWN",
            f"@({both},'MLVAPP_PLAYBACK_MAX_THREADS=4x')": "UNKNOWN",
            # the table keys with their exact values, and a non-MLVAPP name, stay LIGHT
            f"@({both},'MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1','MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq','MLVAPP_PLAYBACK_MAX_THREADS=4','OTHER_VAR=1')": "LIGHT",
            f"@({both},'MLVAPP_CRASH_FORENSICS_LOG_DIR=C:\\Temp\\forensics')": "LIGHT",
            # a repeated key is malformed, whatever its values (r8 MOOT A4: the duplicate that a JSON parser collapses is a key the table does not list; a KEY=VALUE list repeats are refused here)
            f"@({both},'MLVAPP_STAGE_TIMING=1','mlvapp_stage_timing=0')": "UNKNOWN",
            "@('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','mlvapp_playback_smoke_telemetry=1')": "UNKNOWN",
            # a value that is not text is malformed, never its coerced text
            "[pscustomobject]@{ MLVAPP_STAGE_TIMING = @('0') }": "UNKNOWN",
            "[pscustomobject]@{ MLVAPP_STAGE_TIMING = [pscustomobject]@{ a = 1 } }": "UNKNOWN",
            "@(1, 2)": "UNKNOWN",
        }
        got = self.arms([f"Get-FpsArmFromEnvironment ({expr}) @()" for expr in cases])
        self.assertEqual(dict(zip(cases, got)), {expr: f"ARM={want}" for expr, want in cases.items()})

    def test_the_launch_block_is_judged_on_its_arguments_too(self) -> None:
        # r8 (the narrow promise; A1 of r6): the recorded arguments must match the producers' exact LIGHT shape (a closed flag table, FpsArmGate.psm1 $script:FpsLightArgs). --stage-log and the
        # contact-sheet options turn on work inside the timed window; neither is in the table, so a launch that passed one is UNKNOWN, like any flag nobody listed.
        env = f"environment = [pscustomobject]@{{ {LIGHT_LAUNCH_ENV_PS} }}"
        producer_args = ("'--gui-smoke-playback', '--input', 'C:\\clips\\a.mlv', '--seconds', '7.5', '--start-frame', '0', '--presented-frames', '120', '--drop-frame-mode', 'persisted', "
                         "'--settle-ms', '2500', '--settle-cpu-percent', '10', '--settle-cpu-stable-ms', '1000', '--settle-cpu-max-ms', '45000', '--receipt', 'C:\\r\\receipt.json', "
                         "'--scope', 'none', '--playback-debayer=amaze', '--gpu-amaze-texture-present', '--no-look-assist', '--display-prefer=\\\\.\\DISPLAY1'")
        cases = {
            f"[pscustomobject]@{{ {env}; arguments = @({PRODUCER_ARGS_PS}) }}": "LIGHT",
            # A8: an empty or partial list matches the tables vacuously but is not the shape every producer launch records (--gui-smoke-playback, --input, --seconds, --start-frame, the settle options)
            f"[pscustomobject]@{{ {env}; arguments = @() }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--input', 'x') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @({PRODUCER_ARGS_PS.replace(chr(39) + '--start-frame' + chr(39) + ', ' + chr(39) + '0' + chr(39) + ', ', '')}) }}": "UNKNOWN",
            f"[pscustomobject]@{{ environment = [pscustomobject]@{{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' }}; arguments = @({PRODUCER_ARGS_PS}) }}": "UNKNOWN",   # no crash-log directory
            "[pscustomobject]@{ environment = [pscustomobject]@{ }; arguments = @() }": "UNKNOWN",                                                                                                    # the adversary's empty launch
            f"[pscustomobject]@{{ {env}; arguments = @({producer_args}) }}": "LIGHT",                       # the whole shape the runner emits
            f"[pscustomobject]@{{ {env}; arguments = @('--stage-log=C:\\Temp\\stage.csv') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--stage-log', 'C:\\Temp\\stage.csv') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('-STAGE-LOG=x') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('/stage-log:x') }}": "UNKNOWN",
            # A1 (the adversary's input): contact-sheet capture reads the framebuffer in the timed pass
            f"[pscustomobject]@{{ {env}; arguments = @('--gui-smoke', '--contact-sheet-dir', 'C:\\Temp\\sheet', '--contact-sheet-frames', '6') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--gui-smoke-playback', '--contact-sheet-dir', 'C:\\Temp\\sheet') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--gui-smoke-playback', '--contact-sheet-seek-mode') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--gui-smoke-playback', '--screenshot-output', 'C:\\Temp\\s.png') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--gui-smoke-playback', '--exercise-clip-lifecycle-stress') }}": "UNKNOWN",
            # a known flag in a shape the table does not name: repeated, valueless, valued when bare, a value that is a flag, an equals form of a next-value flag
            f"[pscustomobject]@{{ {env}; arguments = @('--gui-smoke-playback', '--gui-smoke-playback') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--seconds') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--seconds', '--start-frame') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--seconds', '7x') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--seconds=7') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--gui-smoke-playback=1') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--playback-debayer=') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--scope', 'all') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--drop-frame-mode', 'off') }}": "UNKNOWN",
            f"[pscustomobject]@{{ {env}; arguments = @('--input', \"a$([char]10)b\") }}": "UNKNOWN",
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


@requires_pwsh
class GuiSmokeInheritedEnvironmentRecordTests(_Probe):
    """BENCH-LIGHT-ARM-GAIN-GATES-1 r6 (adversary pass 5 A1): the smoke runner's recorded launch environment must show every MLVAPP_* variable the child inherits."""

    script = PROFILING / "run-release-gui-smoke.ps1"
    functions = ["Add-InheritedMlvAppEnvironmentRecord"]

    def test_an_inherited_stage_logger_is_recorded_so_a_light_override_does_not_hide_it(self) -> None:
        # the runner records only the overrides it set; a parent MLVAPP_STAGE_TIMING=1 reached the child unrecorded and the classifier allowed the pair. The inherited value is now recorded
        # (same value, so what runs is unchanged), the classifier reads it, and a variable the launch clears is not recorded.
        out = self.ok(
            "$env:MLVAPP_STAGE_TIMING = '1'\n$env:MLVAPP_SOME_STRANGER = 'x'\n$env:MLVAPP_PLAYBACK_QUALITY_MODE = 'q'\n$env:NOT_MLVAPP_THING = 'y'\n"
            "$launchEnv = [ordered]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' }\n"
            "Write-Host ('BEFORE=' + (Get-FpsArmFromEnvironment $launchEnv @()).arm)\n"
            "Add-InheritedMlvAppEnvironmentRecord -Target $launchEnv -Cleared @('MLVAPP_PLAYBACK_QUALITY_MODE')\n"
            "Write-Host ('AFTER=' + (Get-FpsArmFromEnvironment $launchEnv @()).arm)\n"
            "Write-Host ('RECORDED=' + (($launchEnv.Keys | Sort-Object) -join ','))\n"
            "$o = [ordered]@{ MLVAPP_STAGE_TIMING = '0' }\nAdd-InheritedMlvAppEnvironmentRecord -Target $o -Cleared @()\nWrite-Host ('OVERRIDE_KEPT=' + $o['MLVAPP_STAGE_TIMING'])\n")
        lines = dict(line.split("=", 1) for line in out.strip().splitlines())
        self.assertEqual(lines["BEFORE"], "LIGHT")
        self.assertEqual(lines["AFTER"], "UNKNOWN", "r8: the recorded inherited variables are outside the LIGHT shape (a stage logger, and a stranger nobody listed)")
        self.assertIn("MLVAPP_STAGE_TIMING", lines["RECORDED"])
        self.assertIn("MLVAPP_SOME_STRANGER", lines["RECORDED"])
        self.assertNotIn("MLVAPP_PLAYBACK_QUALITY_MODE", lines["RECORDED"], "a cleared variable never reaches the child")
        self.assertNotIn("NOT_MLVAPP_THING", lines["RECORDED"])
        self.assertEqual(lines["OVERRIDE_KEPT"], "0", "an explicit override is never replaced by the inherited value")

    def test_an_inherited_dualiso_debug_dump_is_recorded_and_refuses_light(self) -> None:
        # r14 (sol r13 B6): DUALISO_DEBUG_SOURCE_SMOOTH_DIR is an app control (src/mlv/llrawproc/dualiso.c dumps two image planes inside the measured Play); the runner recorded only MLVAPP_*
        # names and the classifier judged only MLVAPP_* names, so an inherited dump read LIGHT. It is now recorded and outside the LIGHT shape.
        out = self.ok(
            "$env:DUALISO_DEBUG_SOURCE_SMOOTH_DIR = 'C:\\dump'\n"
            f"$launchEnv = [ordered]@{{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; {PRODUCER_LOGDIR_PS} }}\n"
            f"Write-Host ('BEFORE=' + (Get-FpsArmFromLaunch ([pscustomobject]@{{ environment = $launchEnv; arguments = @({PRODUCER_ARGS_PS}) }})).arm)\n"
            "Add-InheritedMlvAppEnvironmentRecord -Target $launchEnv -Cleared @()\n"
            f"Write-Host ('AFTER=' + (Get-FpsArmFromLaunch ([pscustomobject]@{{ environment = $launchEnv; arguments = @({PRODUCER_ARGS_PS}) }})).arm)\n"
            "Write-Host ('RECORDED=' + [bool]$launchEnv.Contains('DUALISO_DEBUG_SOURCE_SMOOTH_DIR'))\n")
        lines = dict(line.split("=", 1) for line in out.strip().splitlines())
        self.assertEqual((lines["BEFORE"], lines["AFTER"], lines["RECORDED"]), ("LIGHT", "UNKNOWN", "True"))

    def test_the_runner_records_the_inherited_variables_before_it_reports_or_dry_runs_its_launch(self) -> None:
        source = (PROFILING / "run-release-gui-smoke.ps1").read_text(encoding="utf-8")
        call = source.index("Add-InheritedMlvAppEnvironmentRecord -Target $launchEnv -Cleared $clearedEnvironment")
        self.assertLess(source.index("$clearedEnvironment = @($clearedEnvironment | Select-Object -Unique)"), call)
        self.assertLess(call, source.index("if ($DryRun) {"))
        self.assertLess(call, source.index("environment = $launchEnv\n        validationPolicy"))


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
    def test_sol_r13_b4_a_plan_that_repeats_its_purpose_is_unreadable_not_the_last_value(self) -> None:
        # r14 (sol r13 B4): ConvertFrom-Json kept the LAST `purpose` ('stage-attribution'), so an fps-ab plan with a HEAVY item skipped the HEAVY refusal. A plan that repeats a key is refused.
        spec = self.spec("fx-dup", {"telemetryArm": "HEAVY"})
        leg = json.dumps({"legSpec": str(spec), "backend": "cpu", "sourceCommit": self.COMMIT, "buildManifestSha256": self.MANIFEST, "arm": "A"})
        plan_path = self.tmp / "plan.json"
        plan_path.write_text('{"schema":"mlv-app/venue-lease-leg-plan/v1","purpose":"fps-ab","purpose":"stage-attribution","legs":[' + leg + "]}", encoding="utf-8")
        r = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(LEASE_BATCH_SCRIPT), "-LegPlan", str(plan_path), "-RunDir", str(self.tmp / "run"),
                            "-ClaimDir", str(self.tmp / "claims"), "-AgentShare", str(self.tmp / "share"), "-DeadlineUtc", "2099-01-01T00:00:00Z"],
                           capture_output=True, text=True, timeout=300, creationflags=_NO_WINDOW)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("LEG_PLAN_REFUSED reason=LEG_PLAN_UNREADABLE ", r.stderr)
        self.assertIn("FPS_JSON_REPEATED_KEY", r.stderr)
        self.assertFalse((self.tmp / "claims").exists() and any((self.tmp / "claims").iterdir()), "no claim was written")

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

    def test_an_arm_read_that_gets_no_answer_in_its_bound_is_a_typed_timeout_not_a_quiet_unknown(self) -> None:
        # BENCH-LIGHT-ARM-GAIN-GATES-1 r6 (adversary pass 5 A5): Get-LegSpecTelemetryArm dropped the bounded read's TIMEOUT state, so an arm read that used up the room before -DeadlineUtc was just an
        # UNKNOWN arm and the plan went on (more reads, staging) toward a refusal after the deadline. The arm now says it timed out, and the plan loop stops at once with the same typed
        # SPEC_READ_TIMEOUT its sibling reads use.
        source = LEASE_BATCH_SCRIPT.read_text(encoding="utf-8")
        m = re.search(r"function Get-LegSpecTelemetryArm\(.*?\n\}\r?\n", source, re.DOTALL)
        self.assertIsNotNone(m)
        script = self.tmp / "armtimeout.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n" f"Import-Module '{MODULE}' -Force\n" + m.group(0) +
            "function Get-SpecReadBoundSec { 5.0 }\n"
            "function Read-SpecBytesWithin($Path, $Bound) { [pscustomobject]@{ state = $script:State; bytes = $null; error = '' } }\n"
            "$script:SpecBudgetSha256 = @{}\n"
            "foreach ($s in @('TIMEOUT', 'FAILED')) { $script:State = $s; $a = Get-LegSpecTelemetryArm 's.json'; Write-Host ($s + '=' + $a.arm + ',timedOut=' + [bool]$a.readTimedOut) }\n",
            encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)], capture_output=True, text=True, creationflags=_NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout.split(), ["TIMEOUT=UNKNOWN,timedOut=True", "FAILED=UNKNOWN,timedOut=False"])
        call = source.index("$armRead = Get-LegSpecTelemetryArm $path")
        stop = source.index("Stop-PlanRefused 'SPEC_READ_TIMEOUT' $n", call)
        self.assertLess(stop, source.index("$items.Add(", call), "the typed timeout stops the plan before the item is kept")
        # r14 (sol r13 B5): the refusal of a read that timed out must not probe batch.log first (the stalled share may hold -RunDir too, and the synchronous probe would hold the typed
        # refusal): the timeout codes are in Stop-PlanRefused's no-probe list, beside the codes that already skip it.
        refused = re.search(r"function Stop-PlanRefused\(.*?\n\}\r?\n", source, re.DOTALL)
        self.assertIsNotNone(refused)
        skip = re.search(r"\$Reason -cnotin @\(([^)]*)\)\) \{\s*\n\s*try \{\s*\n\s*Wait-RunDirReadSeam", refused.group(0))
        self.assertIsNotNone(skip, "the no-probe list guards the batch.log probe")
        self.assertIn("'SPEC_READ_TIMEOUT'", skip.group(1))
        self.assertIn("'EXPORT_BUDGET_PREFLIGHT_TIMEOUT'", skip.group(1))
        # the sibling (adversary r14 pass 17 A4 / pass 18 A5): the plan file's own read timeout keeps its LEG_PLAN_UNREADABLE code but skips the probe the same way a location the guard gave up on does
        self.assertIn("if ($planRead.state -ceq 'TIMEOUT') { $script:RunOutputAliased = $true; Stop-PlanRefused 'LEG_PLAN_UNREADABLE' 0 ", source)
        self.assertIn("if (-not $script:RunOutputAliased -and $Reason -cnotin", refused.group(0))

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
        return f"[pscustomobject]@{{ {arm_part}presentedFps = {fps}; avgPresentIntervalMs = {round(1000.0 / fps, 3)}; avgQueueWaitMs = {queue_ms}; avgDrawTotalMs = 12.0 }}"

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
        self.assertIsNotNone(r["compare"]["avgPresentIntervalMs"]["deltaPercent"], "an allowed pair keeps the interval delta")
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
        # r5 (adversary A9): the gap between presents is the fps in another unit, so its cross-leg delta is withheld with the fps ones (and the analysis does not carry it)
        interval = r["compare"]["avgPresentIntervalMs"]
        self.assertIsNone(interval["delta"])
        self.assertIsNone(interval["deltaPercent"])
        self.assertEqual(interval["refused"], HEAVY_REASON)
        self.assertIsNone(r["analysis"]["evidence"]["presentIntervalDeltaPercent"])
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
            f"launch = [pscustomobject]@{{ arguments = @({PRODUCER_ARGS_PS}); environment = [pscustomobject]@{{ {LIGHT_LAUNCH_ENV_PS} }} }}": "LIGHT",
            ("launch = [pscustomobject]@{ arguments = @(); environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; "
             "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' } }"): "UNKNOWN",   # A8: the empty argument list and the missing crash-log directory are not the producers' shape
            ("launch = [pscustomobject]@{ arguments = @('--stage-log=C:\\Temp\\s.csv'); environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; "
             "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' } }"): "UNKNOWN",   # r8: outside the producers' LIGHT argument shape
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
    functions = ["Convert-ToNullableDouble", "Get-PlaybackAbFpsVerdict", "Get-SmokeSummaryTelemetryArm"]

    def run_block(self, baseline: str, candidate: str, fps_b: float, fps_c: float) -> list[str]:
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        block = _extract_block(source, "if ($RequireCandidateImprovesPresentedFps) {", "candidate-presented-fps-not-improved baseline=$baselineFps candidate=$candidateFps", trailing_lines=3)
        out = self.ok(
            f"$baselineSummary = [pscustomobject]@{{ telemetryArm = {baseline}; {LIGHT_EVIDENCE}presentedFps = {fps_b}; hostLoadProvisional = $false }}\n"
            f"$candidateSummary = [pscustomobject]@{{ telemetryArm = {candidate}; {LIGHT_EVIDENCE}presentedFps = {fps_c}; hostLoadProvisional = $false }}\n"
            "$candidateSpeedSummary = $null\n$SeparateCandidateSpeedRun = $false\n$RequireCandidateImprovesPresentedFps = $true\n$proofFailures = @()\n"
            "$fpsVerdict = Get-PlaybackAbFpsVerdict -BaselineSummary $baselineSummary -CandidateSummary $candidateSummary -CandidateSpeedSummary $candidateSpeedSummary\n"
            + block + "\nforeach ($f in $proofFailures) { Write-Host ('FAIL=' + $f) }\nWrite-Host ('COUNT=' + @($proofFailures).Count)\n")
        return out.splitlines()

    def run_separate(self, parity: str, speed: str, fps_speed: float) -> str:
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        block = _extract_block(source, "if ($RequireCandidateImprovesPresentedFps) {", "candidate-presented-fps-not-improved baseline=$baselineFps candidate=$candidateFps", trailing_lines=3)
        return self.ok(
            f"$baselineSummary = [pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}presentedFps = 10.0; hostLoadProvisional = $false }}\n"
            f"$candidateSummary = [pscustomobject]@{{ telemetryArm = '{parity}'; {LIGHT_EVIDENCE}presentedFps = 7.0; hostLoadProvisional = $false }}\n"
            f"$candidateSpeedSummary = [pscustomobject]@{{ telemetryArm = '{speed}'; {LIGHT_EVIDENCE}presentedFps = {fps_speed}; hostLoadProvisional = $false }}\n"
            "$SeparateCandidateSpeedRun = $true\n$RequireCandidateImprovesPresentedFps = $true\n$proofFailures = @()\n"
            "$fpsVerdict = Get-PlaybackAbFpsVerdict -BaselineSummary $baselineSummary -CandidateSummary $candidateSummary -CandidateSpeedSummary $candidateSpeedSummary -SeparateCandidateSpeedRun\n"
            + block + "\nforeach ($f in $proofFailures) { Write-Host ('FAIL=' + $f) }\nWrite-Host ('COUNT=' + @($proofFailures).Count)\n")

    def test_adversary_r14_a1_a_smoke_result_with_record_level_observer_evidence_is_not_light(self) -> None:
        # r14 (adversary r14 A1, the sol r13 B2 class at the CUDA A/B reader): Get-SmokeSummaryTelemetryArm classified the smoke result from its launch block alone, and Read-SmokeSummary then
        # dropped the record-level observerControls, so the saved pair was allowed. The whole result is judged now; a plain LIGHT result still reads LIGHT.
        launch = f"launch = [pscustomobject]@{{ environment = [pscustomobject]@{{ {LIGHT_LAUNCH_ENV_PS} }}; arguments = @({PRODUCER_ARGS_PS}) }}"
        out = self.ok(
            f"Write-Host ('PLAIN=' + (Get-SmokeSummaryTelemetryArm ([pscustomobject]@{{ {launch} }})).arm)\n"
            f"Write-Host ('OC=' + (Get-SmokeSummaryTelemetryArm ([pscustomobject]@{{ observerControls = [pscustomobject]@{{ utilSample = $true }}; {launch} }})).arm)\n"
            f"Write-Host ('SWITCH=' + (Get-SmokeSummaryTelemetryArm ([pscustomobject]@{{ firstPresentCapture = $true; {launch} }})).arm)\n")
        self.assertEqual(out.split(), ["PLAIN=LIGHT", "OC=UNKNOWN", "SWITCH=UNKNOWN"])
        # adversary r14 pass 18 A2: a smoke result that names a telemetryArm other than LIGHT beside a LIGHT launch is contradictory evidence (the runner's own result names no arm)
        out = self.ok(
            f"Write-Host ('HEAVYWORD=' + (Get-SmokeSummaryTelemetryArm ([pscustomobject]@{{ telemetryArm = 'HEAVY'; {launch} }})).arm)\n"
            f"Write-Host ('ARMOBJ=' + (Get-SmokeSummaryTelemetryArm ([pscustomobject]@{{ telemetryArm = [pscustomobject]@{{ arm = 'HEAVY' }}; {launch} }})).arm)\n"
            f"Write-Host ('NULLWORD=' + (Get-SmokeSummaryTelemetryArm ([pscustomobject]@{{ telemetryArm = $null; {launch} }})).arm)\n"
            f"Write-Host ('LIGHTWORD=' + (Get-SmokeSummaryTelemetryArm ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {launch} }})).arm)\n")
        self.assertEqual(out.split(), ["HEAVYWORD=UNKNOWN", "ARMOBJ=UNKNOWN", "NULLWORD=UNKNOWN", "LIGHTWORD=LIGHT"])

    def test_a_separate_speed_run_is_judged_on_the_speed_pair_not_the_aggregate(self) -> None:
        # r5 (adversary A5): the parity candidate is HEAVY (the validation observer), the speed run is LIGHT; the requirement compares baseline with the speed run, so the parity pair's refusal
        # (reported by its own compare) must not refuse it
        ok = self.run_separate("HEAVY", "LIGHT", 20.0)
        self.assertNotIn("not-evaluated", ok)
        self.assertIn("COUNT=0", ok)
        regress = self.run_separate("HEAVY", "LIGHT", 9.0)
        self.assertIn("candidate-presented-fps-not-improved baseline=10 candidate=9", regress)
        # and a HEAVY speed run is still refused, typed
        self.assertIn(HEAVY_REASON, self.run_separate("LIGHT", "HEAVY", 20.0))

    def test_a_leg_the_wrapper_itself_launches_is_heavy_and_refused_with_the_typed_reason(self) -> None:
        # BENCH-LIGHT-ARM-GAIN-GATES-1 r6 (adversary A4 on 245e26de; HUB RULING: the wrapper's recorded leg is not changed in this card, a venue run owns that): the wrapper's baseline and
        # speed environment lists carry no MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG, and every smoke it generates sets FrameTelemetry = $true, so its own legs record the HEAVY launch.
        # This feeds the wrapper's REAL env lists (spliced from its source) through the REAL recorder (Get-SmokeSummaryTelemetryArm on the launch block the smoke would record) and the
        # REAL fps verdict and require-improvement block: the pair is refused with the typed reason and "not evaluated", never a fake regression ("not-improved") and never a gain.
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("FrameTelemetry = `$true", source, "the wrapper's generated smoke turns the per-frame telemetry on")
        baseline_env = _extract_block(source, "$baselineEnv = @(", ")", trailing_lines=0)
        speed_env = _extract_block(source, "$candidateSpeedEnv = @(", ")", trailing_lines=0)
        block = _extract_block(source, "if ($RequireCandidateImprovesPresentedFps) {", "candidate-presented-fps-not-improved baseline=$baselineFps candidate=$candidateFps", trailing_lines=3)
        out = self.ok(
            baseline_env + speed_env +
            "function New-WrapperLeg([string[]]$EnvList, [double]$Fps) {\n"
            "    $environment = [ordered]@{}\n"
            "    foreach ($kv in $EnvList) { $k, $v = $kv -split '=', 2; $environment[$k] = $v }\n"
            "    $environment['MLVAPP_PLAYBACK_SMOKE_TELEMETRY'] = '1'   # FrameTelemetry = $true in the generated smoke (run-release-gui-smoke.ps1: launchEnv)\n"
            "    $json = [pscustomobject]@{ launch = [pscustomobject]@{ environment = [pscustomobject]$environment; arguments = @() } }\n"
            "    [pscustomobject]@{ telemetryArm = (Get-SmokeSummaryTelemetryArm -Json $json); launch = $json.launch; presentedFps = $Fps; hostLoadProvisional = $false }\n"
            "}\n"
            "$baselineSummary = New-WrapperLeg $baselineEnv 10.0\n$candidateSummary = New-WrapperLeg $candidateSpeedEnv 20.0\n$candidateSpeedSummary = $null\n"
            "Write-Host ('ARM=' + $baselineSummary.telemetryArm.arm + ',' + $candidateSummary.telemetryArm.arm)\n"
            "$SeparateCandidateSpeedRun = $false\n$RequireCandidateImprovesPresentedFps = $true\n$proofFailures = @()\n"
            "$fpsVerdict = Get-PlaybackAbFpsVerdict -BaselineSummary $baselineSummary -CandidateSummary $candidateSummary -CandidateSpeedSummary $candidateSpeedSummary\n"
            "Write-Host ('ALLOWED=' + $fpsVerdict.allowed + ' REASON=' + $fpsVerdict.reason)\n"
            + block + "\nforeach ($f in $proofFailures) { Write-Host ('FAIL=' + $f) }\nWrite-Host ('COUNT=' + @($proofFailures).Count)\n")
        # r8: the baseline leg (telemetry on, no per-frame opt-out) is HEAVY; the speed leg also sets MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=0, a key outside the LIGHT shape, so it is UNKNOWN
        # (it was HEAVY by the denylist's frame-log rule). The pair is still refused with the typed HEAVY reason (heavy outranks unknown).
        self.assertIn("ARM=HEAVY,UNKNOWN", out, "the wrapper's own legs are never LIGHT (this is the open successor BENCH-WRAPPER-LIGHT-ENV-1)")
        self.assertIn(f"ALLOWED=False REASON={HEAVY_REASON}", out)
        self.assertIn("candidate-presented-fps-improvement-not-evaluated", out)
        self.assertNotIn("candidate-presented-fps-not-improved", out, "a HEAVY pair is never reported as a regression either")
        self.assertIn("COUNT=1", out, "the requirement fails closed: one proof failure, no verdict from the numbers")

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
                # r15 (adversary r14 pass 18 A7): the cached analysis is trusted only when bound to the judged pair (its basis and presented-fps deltaPercent), as the producer writes it
                "analysis = [pscustomobject]@{ comparisonBasis = 'candidate'; dominantBottleneck = 'recon-bound'; suggestedOptimization = 'optimize_cuda_recon_kernel_or_state_setup'; "
                "evidence = [pscustomobject]@{ presentedFpsDeltaPercent = 100.0 } }; "
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
        leg = f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}hostLoadProvisional = $false; hostLoadState = 'quiet' }}"
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

    def test_a_saved_speed_leg_held_in_a_one_element_array_is_unknown_through_the_pair_selection(self) -> None:
        # BENCH-LIGHT-ARM-GAIN-GATES-1 r6 (adversary pass 5 A3): `$candidateLeg = if (...) { $Record.candidateSpeed }` unrolled a one-element array into the leg inside it, so a malformed
        # container the direct reader rejects was judged as that LIGHT leg and the machine row published the speed delta. Both leg choices keep the container whole.
        light = f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}hostLoadProvisional = $false; hostLoadState = 'quiet' }}"
        heavy = "[pscustomobject]@{ telemetryArm = 'HEAVY'; hostLoadProvisional = $false; hostLoadState = 'quiet' }"
        fingerprint = "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
        compare = "[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 20.0; candidate = 30.0; delta = 10.0; deltaPercent = 50.0 } }"
        base = f"schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; {fingerprint}baseline = {light}; candidate = {heavy}; compare = {compare}; speedCompare = {compare}; "
        wrapped = self.row(f"[pscustomobject]@{{ {base}candidateSpeed = @({light}) }}")
        self.assertIsNone(wrapped["playback_fps_delta_pct"])
        self.assertEqual(wrapped["playback_fps_arm_verdict"], UNKNOWN_REASON)
        plain = self.row(f"[pscustomobject]@{{ {base}candidateSpeed = {light} }}")
        self.assertEqual(plain["playback_fps_delta_pct"], 50.0, "control: the same leg, not wrapped")
        out = self.ok(f"$r = [pscustomobject]@{{ {base}candidateSpeed = @({light}) }}\nWrite-Host ('PROV=' + (Get-PlaybackAbHostLoadRefusal -Record $r).candidateProvisional)\n")
        self.assertIn("PROV=True", out, "the host-load reader judges the same leg: a wrapped container is provisional, never the leg inside it")

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

    @staticmethod
    def a7_record(analysis_basis: str, analysis_delta: str, legacy: bool = False) -> str:
        # adversary r14 pass 18 A7: quiet baseline LIGHT at 50, candidate HEAVY at 40, candidateSpeed LIGHT at 55; compare deltaPercent -20 (the HEAVY pair), speedCompare deltaPercent 10
        light = f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}hostLoadProvisional = $false; hostLoadState = 'quiet' }}"
        heavy = "[pscustomobject]@{ telemetryArm = 'HEAVY'; hostLoadProvisional = $false; hostLoadState = 'quiet' }"
        fingerprint = "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
        analysis = ("[pscustomobject]@{ dominantBottleneck = 'render-work'; suggestedOptimization = 'optimize_playback_cuda_candidate' }" if legacy else
                    f"[pscustomobject]@{{ comparisonBasis = '{analysis_basis}'; dominantBottleneck = 'render-work'; suggestedOptimization = 'optimize_playback_cuda_candidate'; "
                    f"evidence = [pscustomobject]@{{ presentedFpsDeltaPercent = {analysis_delta} }} }}")
        return (f"[pscustomobject]@{{ schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; {fingerprint}baseline = {light}; candidate = {heavy}; candidateSpeed = {light}; "
                "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 50.0; candidate = 40.0; delta = -10.0; deltaPercent = -20.0 } }; "
                "speedCompare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 50.0; candidate = 55.0; delta = 5.0; deltaPercent = 10.0 } }; "
                f"analysis = {analysis} }}")

    def test_r15_a7_a_cached_diagnosis_of_the_other_pair_is_not_published(self) -> None:
        # r15 (adversary r14 pass 18 A7, sol r13 B3): the reader gated the LIGHT speed pair, then published the cached diagnosis of the HEAVY candidate pair. It is re-derived from the gated
        # pair now; a legacy analysis with no basis / evidence, or one whose delta differs, is not bound either. A bound analysis (the speed pair's own) is still used.
        for label, record in (("other-basis", self.a7_record("candidate", "-20.0")), ("legacy", self.a7_record("", "", legacy=True)),
                              ("delta-mismatch", self.a7_record("candidateSpeed", "-20.0"))):
            with self.subTest(label):
                r = self.row(record)
                self.assertEqual(r["playback_fps_arm_verdict"], "allowed")
                self.assertNotEqual(r["dominant_bottleneck"], "render-work")
                self.assertNotEqual(r["suggested_optimization"], "optimize_playback_cuda_candidate")
        bound = self.row(self.a7_record("candidateSpeed", "10.0"))
        self.assertEqual((bound["dominant_bottleneck"], bound["suggested_optimization"]), ("render-work", "optimize_playback_cuda_candidate"))


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

    def test_r15_a7_the_cached_suggestion_of_the_other_pair_is_not_repeated(self) -> None:
        # r15 (adversary r14 pass 18 A7 sibling compare-machine-perf.ps1:382): the speed pair (LIGHT/LIGHT, +10%) is gated, the cached suggestion belongs to the HEAVY candidate pair (-20%)
        record = CompareMachinePerfFpsGateTests.a7_record
        def suggestion(playback_ab: str) -> str:
            return self.ok("$rec = [pscustomobject]@{ proof = [pscustomobject]@{ playback = [pscustomobject]@{ totalFallbackFrames = 0; totalGpuTextureNoReadbackFrames = 5 }; "
                           f"playbackAb = {playback_ab}; cdng = $null }} }}\nWrite-Host ('S=' + (Get-ProofSummarySuggestion -Record $rec))").strip()
        for label, playback_ab in (("other-basis", record("candidate", "-20.0")), ("legacy", record("", "", legacy=True))):
            with self.subTest(label):
                self.assertEqual(suggestion(playback_ab), "S=run_dng_hash_export_proof", "the gated pair's own delta (+5 fps) suggests nothing; the next proof step follows")
        self.assertEqual(suggestion(record("candidateSpeed", "10.0")), "S=optimize_playback_cuda_candidate", "a bound cached suggestion is still repeated")


@requires_pwsh
class CompareGuiSmokeAbFpsGateTests(_Probe):
    """G3 compare-release-gui-smoke-ab.ps1: the before/after fps deltas are withheld, typed, unless both smokes ran LIGHT; the screenshot verdict is unaffected."""

    script = COMPARE_SMOKE_AB_SCRIPT
    functions = ["Get-NestedValue", "Convert-ToNullableDouble", "New-DeltaObject", "Get-FpsArmComparisonEvidence", "Hide-FpsComparisonDeltas"]

    @staticmethod
    def smoke(env: str | None) -> str:
        if env is None:
            return "[pscustomobject]@{ launch = [pscustomobject]@{ } }"
        return f"[pscustomobject]@{{ launch = [pscustomobject]@{{ arguments = @({PRODUCER_ARGS_PS}); environment = [pscustomobject]@{{ {env} }} }} }}"

    HEAVY_ENV = "MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'"
    LIGHT_ENV = LIGHT_LAUNCH_ENV_PS

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

    def test_an_argument_outside_the_light_shape_refuses_the_smoke(self) -> None:
        # adversary A3, sibling site: the arm gate reads launch.arguments as well as launch.environment. r8: a --stage-log (and the contact-sheet options of r6 A1) is not in the producers'
        # LIGHT argument table, so the smoke is UNKNOWN (refused with the unknown-arm reason), no longer HEAVY.
        for argument in ("'--stage-log=C:\\Temp\\s.csv'", "'--contact-sheet-dir', 'C:\\Temp\\sheet', '--contact-sheet-frames', '6'"):
            with self.subTest(argument=argument):
                staged = (f"[pscustomobject]@{{ launch = [pscustomobject]@{{ arguments = @({argument}); "
                          f"environment = [pscustomobject]@{{ {self.LIGHT_ENV} }} }} }}")
                out = self.ok(f"$e = Get-FpsArmComparisonEvidence -BeforeSmoke ({self.smoke(self.LIGHT_ENV)}) -AfterSmoke ({staged})\n$e | ConvertTo-Json -Compress")
                e = json.loads(out.strip().splitlines()[-1])
                self.assertTrue(e["refused"])
                self.assertEqual((e["reason"], e["after"]["arm"]), (UNKNOWN_REASON, "UNKNOWN"))

    def test_a_mixed_pair_and_a_smoke_with_no_recorded_environment_are_refused(self) -> None:
        self.assertEqual(self.run_compare(self.LIGHT_ENV, self.HEAVY_ENV)["evidence"]["reason"], HEAVY_REASON)
        self.assertEqual(self.run_compare(self.LIGHT_ENV, None)["evidence"]["reason"], UNKNOWN_REASON)

    def test_sol_r13_b2_a_light_launch_beside_record_level_observer_evidence_is_refused(self) -> None:
        # r14 (sol r13 B2, launch-projection half): the comparator read only the launch block, so a valid LIGHT launch beside a record-level observerControls entry (which the saved-leg reader
        # refuses) read LIGHT and the pair kept its fps deltas. The whole smoke record is judged now; a plain LIGHT smoke still reads LIGHT.
        light = self.smoke(self.LIGHT_ENV)
        extras = ("observerControls = [pscustomobject]@{ utilSample = $true }", "utilSample = $true", "look = [pscustomobject]@{ contactSheetFrames = 6 }", "legType = 'look'")
        for extra in extras:
            with self.subTest(extra=extra):
                staged = (f"[pscustomobject]@{{ {extra}; launch = [pscustomobject]@{{ arguments = @({PRODUCER_ARGS_PS}); "
                          f"environment = [pscustomobject]@{{ {self.LIGHT_ENV} }} }} }}")
                out = self.ok(f"$e = Get-FpsArmComparisonEvidence -BeforeSmoke ({light}) -AfterSmoke ({staged})\n$e | ConvertTo-Json -Compress")
                e = json.loads(out.strip().splitlines()[-1])
                self.assertTrue(e["refused"])
                self.assertEqual((e["reason"], e["before"]["arm"], e["after"]["arm"]), (UNKNOWN_REASON, "LIGHT", "UNKNOWN"))


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

    def run_summarizer_block(self, baseline_arm: str, candidate_arm: str, analysis: str = "$null", candidate_extra: str = "", bare_candidate: bool = False, stale_failures: bool = False) -> list[str]:
        # stale_failures: what the proof-failure loop (before this block) copied from a packet written before the gate: the wrapper's own fps-regression string, and one unrelated failure
        stale_injection = ""
        if stale_failures:
            stale_injection = ("foreach ($f in @('candidate-presented-fps-not-improved baseline=50 candidate=40', 'candidate-no-readback-frames=0')) { [void]$playbackAbBlockers.Add($f); "
                               "Add-Diagnostic -Diagnostics $diagnostics -Area 'playback-speed' -Code 'PLAYBACK_AB_PROOF_FAILURE' -Message $f -NextAction 'x' }\n")
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
            + stale_injection
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
        self.assertIn("render total", text, "stage lines stay")
        # r5 (adversary A9): the gap between presents is the fps in another unit, so no delta line is printed for it on a refused pair
        self.assertIn("avg present interval: withheld", text)
        self.assertNotIn("avg present interval: CPU", text)

    def test_a_refused_pair_repeats_no_cached_fps_derived_suggestion(self) -> None:
        # adversary A9 (pre-existing append, same class): the packet's analysis.suggestedOptimization is derived from the fps delta
        # r15 (adversary r14 pass 18 A7): the fixture's analysis is bound to the judged pair (basis 'candidate', deltaPercent -2), as the producer writes it
        analysis = "[pscustomobject]@{ comparisonBasis = 'candidate'; suggestedOptimization = 'optimize_playback_cuda_candidate'; evidence = [pscustomobject]@{ presentedFpsDeltaPercent = -2 } }"
        refused = "\n".join(self.run_summarizer_block("HEAVY", "HEAVY", analysis))
        self.assertNotIn("suggested_optimization=", refused)
        allowed = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", analysis))
        self.assertIn("suggested_optimization=optimize_playback_cuda_candidate", allowed)

    def test_r15_a7_an_allowed_pair_repeats_no_cached_suggestion_of_another_pair(self) -> None:
        # r15 (adversary r14 pass 18 A7 sibling summarize-local-cuda-proof.ps1:764): an allowed pair repeated whatever suggestion the packet cached, whichever pair it came from
        for label, analysis in (
                ("other-basis", "[pscustomobject]@{ comparisonBasis = 'candidateSpeed'; suggestedOptimization = 'optimize_playback_cuda_candidate'; evidence = [pscustomobject]@{ presentedFpsDeltaPercent = -2 } }"),
                ("delta-mismatch", "[pscustomobject]@{ comparisonBasis = 'candidate'; suggestedOptimization = 'optimize_playback_cuda_candidate'; evidence = [pscustomobject]@{ presentedFpsDeltaPercent = -20 } }"),
                ("legacy", "[pscustomobject]@{ suggestedOptimization = 'optimize_playback_cuda_candidate' }"),
                ("string-delta", "[pscustomobject]@{ comparisonBasis = 'candidate'; suggestedOptimization = 'optimize_playback_cuda_candidate'; evidence = [pscustomobject]@{ presentedFpsDeltaPercent = '-2' } }")):
            with self.subTest(label):
                text = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", analysis))
                self.assertIn("presented FPS: CPU", text, "control: the pair is allowed")
                self.assertNotIn("suggested_optimization=", text)

    def test_light_ab_keeps_the_verdict_and_delta_lines(self) -> None:
        text = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT"))
        self.assertIn("DIAG=PLAYBACK_PRESENTED_FPS_NOT_IMPROVED", text)
        self.assertNotIn("PLAYBACK_AB_FPS_ARM_REFUSED", text)
        self.assertIn("presented FPS: CPU", text)

    def test_r15_a6_a_refused_playback_speed_section_grants_no_speed_authority(self) -> None:
        # r15 (adversary r14 pass 18 A6): an otherwise passing packet whose presented-fps pair the arm gate refused (playbackSpeed.status=REFUSED, `passed` true) read QUOTABLE_PASS and its action
        # plan granted "support/speed" quoting (QUOTE_WITH_BOUNDARIES). Speed authority is a separate gate now: claimAuthority.speed is true only for QUOTABLE_PASS with a PASS playbackSpeed.
        tmp = Path(tempfile.mkdtemp(prefix="fps-arm-auth-"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        funcs = _extract_functions(SUMMARIZER_SCRIPT, ["Get-Field", "New-SectionVerdict", "Test-DiagnosticCode", "New-ActionStep", "Add-ActionStep", "Get-ProofSpeedClaimAuthority", "New-ActionPlan"])
        probe = tmp / "run.ps1"
        body = "$ErrorActionPreference = 'Stop'\n" + funcs + "\n$summary = [pscustomobject]@{ inputs = [pscustomobject]@{ clipPath = 'C:\\clip.mlv' } }\n"
        for label, overall, section in (("REFUSED", "QUOTABLE_PASS", "REFUSED"), ("MISSING", "QUOTABLE_PASS", None), ("PASS", "QUOTABLE_PASS", "PASS"), ("NOTQ", "NOT_QUOTABLE", "PASS")):
            verdict = "$null" if section is None else f"(New-SectionVerdict -Status '{section}' -Passed $true -Evidence @() -Blockers @())"
            body += (f"$a = Get-ProofSpeedClaimAuthority -OverallStatus '{overall}' -PlaybackSpeedVerdict {verdict}\n"
                     f"$plan = New-ActionPlan -Status '{overall}' -Diagnostics @() -DiagnosticSummary $null -Summary $summary -ClaimAuthority $a\n"
                     f"Write-Host ('{label}=' + $a.speed + '|' + (@($plan.steps | ForEach-Object {{ $_.id }}) -join ','))\n")
        probe.write_text(body, encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe)], capture_output=True, text=True, creationflags=_NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = dict(line.split("=", 1) for line in proc.stdout.split() if "=" in line)
        self.assertEqual(rows["REFUSED"].split("|")[0], "False")
        self.assertNotIn("QUOTE_WITH_BOUNDARIES", rows["REFUSED"])
        self.assertIn("QUOTE_SUPPORT_ONLY_SPEED_REFUSED", rows["REFUSED"])
        self.assertEqual(rows["MISSING"].split("|")[0], "False")
        self.assertNotIn("QUOTE_WITH_BOUNDARIES", rows["MISSING"])
        self.assertEqual(rows["PASS"].split("|")[0], "True")
        self.assertIn("QUOTE_WITH_BOUNDARIES", rows["PASS"])
        self.assertEqual(rows["NOTQ"].split("|")[0], "False")

    def test_r15_a6_the_report_package_and_import_carry_the_speed_gate(self) -> None:
        # r15 (adversary r14 pass 18 A6 siblings): the report publishes claimAuthority, and package / import surface speedQuotable from it (never from the support status alone)
        source = SUMMARIZER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("claimAuthority = $claimAuthority", source)
        self.assertIn("-ClaimAuthority $claimAuthority", source)
        self.assertNotIn("Only QUOTABLE_PASS from the same host unlocks support or speed claims.", source)
        for name in ("package-local-cuda-proof-result.ps1", "import-local-cuda-proof-result.ps1"):
            text = (PROFILING / name).read_text(encoding="utf-8")
            with self.subTest(name):
                self.assertIn('speedQuotable = (($proofReport.status -ceq "QUOTABLE_PASS") -and ($null -ne $proofReport.claimAuthority) -and ($proofReport.claimAuthority.speed -is [bool]) -and $proofReport.claimAuthority.speed)', text)
                self.assertNotIn("support or speed only from proofReport.status=QUOTABLE_PASS", text)
                self.assertNotIn("support or speed remains quotable only when proofReport.status is QUOTABLE_PASS", text)

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

    def test_a_stale_fps_regression_failure_is_not_republished_for_a_refused_pair(self) -> None:
        # r5 (adversary A8): a packet written before this gate carried "candidate-presented-fps-not-improved baseline=50 candidate=40" (an fps pair the gate refuses now); the loop above the arm
        # judgement copied it into the blockers and a diagnostic. It is taken out for a refused pair; the unrelated failure stays, and an allowed (LIGHT) pair keeps both.
        refused = self.run_summarizer_block("HEAVY", "HEAVY", stale_failures=True)
        blockers = next(line for line in refused if line.startswith("BLOCKERS="))
        self.assertNotIn("candidate-presented-fps-not-improved", blockers)
        self.assertIn("candidate-no-readback-frames=0", blockers)
        self.assertEqual([line for line in refused if line == "DIAG=PLAYBACK_AB_PROOF_FAILURE"], ["DIAG=PLAYBACK_AB_PROOF_FAILURE"])
        allowed = next(line for line in self.run_summarizer_block("LIGHT", "LIGHT", stale_failures=True) if line.startswith("BLOCKERS="))
        self.assertIn("candidate-presented-fps-not-improved", allowed)

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


# The six generatorArgs keys of a leg spec that turn an observer, or a presentation-moving hook (r13: midPlayWindowToggles, adversary pass 15 A1), on inside the timed window (the independent oracle
# of the corpus test; the module holds the same list as data).
OBSERVER_SPEC_KEYS = ("cudaLaunchTimingMinMs", "corePartitionTelemetry", "utilSample", "firstPresentCapture", "warmReuseMissDump", "midPlayWindowToggles", "gpuFaultInject")
LIGHT_ENV = "'MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1'"

# r10 G11: the raw environment reader judges an environment list without the launch members every producer records; only the module itself and this test file may name it
ENVIRONMENT_READER = "Get-FpsArmFromEnvironment"
ENVIRONMENT_READER_ALLOWED_PATHS = frozenset({"tools/profiling/FpsArmGate.psm1", "tools/repo_hygiene/test_fps_arm_gate.py"})


def _callers_of_the_environment_reader(files: list[tuple[str, str]]) -> list[str]:
    """The repo-relative paths, among the (path, text) pairs given, that name the raw environment reader and are not the module or this test."""
    # case-insensitive: PowerShell resolves a command name without regard to case (adversary r10 pass A6)
    return sorted(path for path, text in files if ENVIRONMENT_READER.lower() in text.lower() and path.replace("\\", "/") not in ENVIRONMENT_READER_ALLOWED_PATHS)


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

    # --- the environment table: a closed list with exact values (r8 replaces the per-consumer denylist of r2-r6) ---------------------------------------------
    def test_a_path_valued_variable_outside_the_table_is_unknown_whatever_its_value(self) -> None:
        # RenderFrameThread.cpp:60-62 opens a stage CSV from ANY non-empty MLVAPP_PHASE3_TEL_PATH; StageTiming.h:87-90 opens MLVAPP_STAGE_TIMING_FILE the same way; CrashForensics.cpp:687 reads
        # MLVAPP_PERF_FIELD_LOG_PATH; GpuDisplayWindow.cpp:146 dumps into a non-empty MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR. The table names none of them, so each is UNKNOWN whatever its value
        # (empty included: an empty value is still a set variable, qEnvironmentVariableIsSet); the table's own path keys (a log directory, a DLL) stay LIGHT.
        def env(*pairs: str) -> str:
            return "Get-FpsArmFromEnvironment @(" + LIGHT_ENV + "," + ",".join(f"'{p}'" for p in pairs) + ") @()"
        self.check({
            env("MLVAPP_PHASE3_TEL_PATH=0"): "UNKNOWN",
            env("MLVAPP_PHASE3_TEL_PATH=false"): "UNKNOWN",
            env("MLVAPP_PHASE3_TEL_PATH=C:\\Temp\\stages.csv"): "UNKNOWN",
            env("MLVAPP_PHASE3_TEL_PATH="): "UNKNOWN",
            env("mlvapp_phase3_tel_path=0"): "UNKNOWN",
            env("MLVAPP_STAGE_TIMING_FILE=0"): "UNKNOWN",
            env("MLVAPP_PERF_FIELD_LOG_PATH=0"): "UNKNOWN",
            env("MLVAPP_PERF_FIELD_LOG_DIR=0"): "UNKNOWN",
            env("MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR=0"): "UNKNOWN",
            env("MLVAPP_A_NEW_DIAGNOSTIC_DUMP_DIR=0"): "UNKNOWN",
            env("MLVAPP_A_NEW_DIAGNOSTIC_FILE=0"): "UNKNOWN",
            env("MLVAPP_A_NEW_DIAGNOSTIC_DUMP_DIR="): "UNKNOWN",
            env("MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS=0"): "UNKNOWN",
            env("MLVAPP_CRASH_FORENSICS_LOG_DIR=C:\\Temp\\forensics"): "LIGHT",
            env("MLVAPP_GPU_PLAYBACK_RECON_DLL=C:\\pkg\\recon.dll"): "LIGHT",
            env("MLVAPP_PLAYBACK_PRESENT_TRACE=1"): "LIGHT",                # the in-memory trace is a table row, matching the spec classifier (presentTrace)
            env("MLVAPP_PLAYBACK_PRESENT_TRACE=0"): "UNKNOWN",              # the table names the value the job emits, 1
        })

    def test_the_adversary_input_for_a1_is_not_light_through_the_launch_block(self) -> None:
        launch = ("[pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; "
                  "MLVAPP_PHASE3_TEL_PATH = '0' }; arguments = @() }")
        out = self.ok(f"$a = Get-FpsArmFromLaunch ({launch})\nWrite-Host ('ARM=' + $a.arm)\n"
                      f"$v = Get-FpsPairVerdict -Baseline $a -Candidate $a\nWrite-Host ('ALLOWED=' + $v.allowed)")
        self.assertIn("ARM=UNKNOWN", out)
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

    def test_the_recon_output_validation_flag_is_outside_the_light_shape(self) -> None:
        # r2 pass 4 A2: MainWindow.cpp:404-409 / RenderFrameThread.cpp:696-702 (set and not "0") run a GL readback, SHA256 and a CPU recon replay per presented frame. r8: the table does not name it
        # (nor its sample-every), so it is UNKNOWN at any value, the wrapper speed leg's explicit "0" included; the routing keys the job sets stay LIGHT.
        def env(*pairs: str) -> str:
            return "Get-FpsArmFromEnvironment @(" + LIGHT_ENV + "," + ",".join(f"'{p}'" for p in pairs) + ") @()"
        adversary = ("[pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; MLVAPP_GPU_PLAYBACK_RECON = '1'; "
                     "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT = '1'; MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT = '1'; "
                     "MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT = '1'; MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT_SAMPLE_EVERY = '1' }")
        self.check({
            f"Get-FpsArmFromEnvironment ({adversary}) @()": "UNKNOWN",
            env("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=1"): "UNKNOWN",
            env("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=true"): "UNKNOWN",
            env("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=0"): "UNKNOWN",                # the speed leg of run-release-cuda-playback-ab.ps1:1378 (a key outside the shape)
            env("MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=0", "MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT_SAMPLE_EVERY=4"): "UNKNOWN",
            env("MLVAPP_GPU_PLAYBACK_RECON=1", "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT=1", "MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT=1"): "LIGHT",   # a route, not an observer
            env("MLVAPP_GPU_PLAYBACK_RECON_ELIGIBILITY_DIAG=1"): "LIGHT",                # deduped to one line per session (playback-attr-3-cuda-job.ps1:268-277)
        })

    def test_the_record_helper_judges_the_whole_leg_not_its_arm_word(self) -> None:
        self.check({
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT' })": "UNKNOWN",   # r5: the bare arm word is not proof (the rule a recorded result already had)
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}}})": "LIGHT",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT'; utilSample = $true })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT'; generatorArgs = [pscustomobject]@{ cudaLaunchTimingMinMs = 0 } })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'HEAVY'; utilSample = $true })": "HEAVY",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x') })": "UNKNOWN",   # r5 pass 2 A3: an arm label, whatever its source, is not proof
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x'); {RECORDED_LIGHT_LAUNCH} }})": "LIGHT",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x'); firstPresentCapture = $true })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = (New-FpsArm 'HEAVY' 'environment' 'x') })": "HEAVY",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = @('LIGHT') })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ sourceCommit = 'x' })": "UNKNOWN",
            "Get-FpsArmFromLegRecord $null": "UNKNOWN",
        })

    def test_an_unlisted_variable_is_unknown_whatever_its_value(self) -> None:
        # r2 pass 5 A1: MLVAPP_PROCESSING_CORE_COLOR_MAIN_PRELUDE_WB_PROBE=0 is a MODE (raw_processing.c:346 processing_parse_probe_mode: mode 0 enables the probe timing); the dual-iso
        # probes read their value the same way (dualiso.c:304, :396). "0" is no proof of off for a name outside the table (r8: the table is the only thing a LIGHT leg may carry).
        def env(*pairs: str) -> str:
            return "Get-FpsArmFromEnvironment @(" + LIGHT_ENV + "," + ",".join(f"'{p}'" for p in pairs) + ") @()"
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

    # The MLVAPP_* names the job generator writes that the LIGHT table does NOT hold, each for one stated reason. A launch that records one is UNKNOWN: refused, the safe side.
    #   observers a LIGHT leg must not carry: the HEAVY arm's loggers (playback-attr-3-cuda-job.ps1:3041-3045), the viewport present diag a LIGHT leg drops (:3078), the per-launch CUDA timer
    #   (spec key cudaLaunchTimingMinMs), the first-present capture and the warm-reuse miss dump (spec keys firstPresentCapture / warmReuseMissDump);
    #   spec-driven knobs and test hooks (thread partitions, scope cadence, kill switches, fault injection) that a venue leg sets from its spec: the spec reader (c) judges those legs, and a recorded
    #   launch that carries one is not the producers' standing LIGHT shape, so it over-refuses rather than guess a value domain for each.
    JOB_NAMES_A_LIGHT_LEG_DOES_NOT_CARRY = {
        "MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY", "MLVAPP_PLAYBACK_DETAILED_TIMELINE_TELEMETRY", "MLVAPP_STAGE_TIMING", "MLVAPP_PERF_FIELD_LOG", "MLVAPP_VIEWPORT_PRESENT_DIAG",
        "MLVAPP_CUDA_LAUNCH_TIMING", "MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS", "MLVAPP_SMOKE_FIRST_PRESENT_CAPTURE", "MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR",
        "MLVAPP_DISABLE_PREP_POINTWISE_FAST", "MLVAPP_DISABLE_RAW_UINT16_PREFETCH", "MLVAPP_RAW_UINT16_PREFETCH_DECODERS", "MLVAPP_GPU_FAULT_INJECT", "MLVAPP_GPU_RECON_RELEASE_DEFERRED",
        "MLVAPP_GPU_SAFE_START", "MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY", "MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE", "MLVAPP_PLAYBACK_PREP_OMP_THREADS", "MLVAPP_PLAYBACK_RECON_OMP_THREADS",
        "MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES", "MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK", "MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS",
        # r13 (master 1a65d592, #851): the mid-play window-toggle hook (fullscreen / windowed / restore / maximize while the measured Play runs, playback-attr-3-cuda-job.ps1 under -MidPlayWindowToggles)
        # moves the presentation surface, so a launch that records it is not the standing LIGHT shape and reads UNKNOWN (a spec-driven test hook, like the fault injection above).
        "MLVAPP_GUI_SMOKE_MID_PLAY_WINDOW_TOGGLES",
    }

    def test_every_variable_the_job_generator_emits_is_in_the_light_table_or_named_as_not_light(self) -> None:
        # r2 pass 5 A2 (class guard, r8 form): each MLVAPP_* name the generator itself writes is either a row of the LIGHT environment table (the closed list a LIGHT leg may carry, each row citing its
        # producer) or named in JOB_NAMES_A_LIGHT_LEG_DOES_NOT_CARRY, so a NEW name the generator starts to emit cannot pass the gate unclassified: it fails here until someone decides which it is.
        job = (PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        names = sorted(set(re.findall(r"MLVAPP_[A-Z0-9_]+(?==)", job)))
        self.assertGreater(len(names), 25)
        table = set(self.ok("(Get-FpsLightShape).environment | ForEach-Object { $_.name }").split())
        unclassified = [n for n in names if n not in table and n not in self.JOB_NAMES_A_LIGHT_LEG_DOES_NOT_CARRY]
        self.assertEqual(unclassified, [], "classify each name the generator emits: add a row to $script:FpsLightEnv (a LIGHT leg carries it) or name it in JOB_NAMES_A_LIGHT_LEG_DOES_NOT_CARRY")
        # the exemption list holds only names the job really writes and the table really excludes (no stale entry hides a decision)
        self.assertEqual(sorted(n for n in self.JOB_NAMES_A_LIGHT_LEG_DOES_NOT_CARRY if n not in names or n in table), [])
        # and each one a LIGHT leg must not carry reads UNKNOWN or HEAVY through the gate, never LIGHT
        body = "".join(f"Write-Host ('ARM=' + (Get-FpsArmFromEnvironment @({LIGHT_ENV}, '{n}=1') @()).arm)\n" for n in sorted(self.JOB_NAMES_A_LIGHT_LEG_DOES_NOT_CARRY))
        self.assertEqual(set(self.ok(body).split()), {"ARM=UNKNOWN"})

    def test_g12_a_fixture_launch_reads_unknown_and_the_job_comment_names_no_variable_assignment(self) -> None:
        # r10 G12 (Grok ask 7060550362): the LIGHT-job comment said the hub-host fixture flag is exempt and implied such a leg is still an admitted LIGHT leg. The code keeps the flag for the smoke
        # runner, no row of the LIGHT environment table names it, so a fixture launch reads UNKNOWN: fixture legs are refused for fps on purpose. This UNKNOWN pin passes on 5aa4a230 too (it pins existing behaviour).
        out = self.ok(f"Write-Host ('PLAIN=' + (Get-FpsArmFromEnvironment @({LIGHT_ENV}) @()).arm)\n"
                      f"Write-Host ('FIXTURE=' + (Get-FpsArmFromEnvironment @({LIGHT_ENV}, 'MLVAPP_HUB_PLAYBACK_CI_FIXTURE=1') @()).arm)\n")
        self.assertEqual(out.split(), ["PLAIN=LIGHT", "FIXTURE=UNKNOWN"])
        job_lines = (PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8").splitlines()
        start = next(i for i, line in enumerate(job_lines) if "BENCH-LIGHT-ARM-GAIN-GATES-1 r8 (adversary r6 A2)" in line)
        end = start
        while job_lines[end].lstrip().startswith("#"):
            end += 1
        block = " ".join(line.lstrip(" #") for line in job_lines[start:end])
        self.assertGreater(end - start, 4, "the comment block under test was not found")
        self.assertNotRegex(block, r"MLVAPP_[A-Z0-9_]+=", "a comment token the generator scan would read as an emitted variable")
        self.assertIn("fixture legs are refused for fps on purpose", block)
        self.assertNotIn("is exempt", block)

    # --- r10 G11: nobody outside the module may call the raw environment reader --------------------------------------------------------------------------
    def test_g11_the_guard_reports_a_caller_outside_the_module_in_a_synthetic_tree(self) -> None:
        # RED proof of the guard: a reader script that calls the function is reported; the module and this test file are not; a file that only lacks the name is not.
        reported = _callers_of_the_environment_reader([
            ("tools/profiling/some-reader.ps1", "$arm = Get-FpsArmFromEnvironment $env @()"),
            ("tools/profiling/FpsArmGate.psm1", "function Get-FpsArmFromEnvironment { }"),
            ("tools/repo_hygiene/test_fps_arm_gate.py", "Get-FpsArmFromEnvironment"),
            ("tools/profiling/other.ps1", "Get-FpsArmFromLaunch $launch"),
            # PowerShell resolves command names without regard to case, so a lower-case call is a call (adversary r10 pass A6)
            ("tools/profiling/lower-case-reader.ps1", "& 'get-fpsarmfromenvironment' @{} @()"),
        ])
        self.assertEqual(reported, ["tools/profiling/lower-case-reader.ps1", "tools/profiling/some-reader.ps1"])

    @unittest.skipIf(shutil.which("git") is None, "git is not on PATH")
    def test_g11_no_tracked_file_outside_the_module_calls_the_raw_environment_reader(self) -> None:
        # The raw environment reader judges an environment list WITHOUT the launch members every producer records, so a caller that holds a recorded launch must go through
        # Get-FpsArmFromLaunch. It stays exported for the test probes; this scan keeps any other file from calling it. Tracked files only (git grep), no filesystem walk.
        proc = subprocess.run(["git", "grep", "-l", "-i", "-F", "Get-FpsArmFromEnvironment"], cwd=ROOT, capture_output=True, text=True, creationflags=_NO_WINDOW)
        self.assertIn(proc.returncode, (0, 1), proc.stderr)
        paths = [p for p in proc.stdout.splitlines() if p.strip()]
        self.assertIn("tools/profiling/FpsArmGate.psm1", paths, "the scan found nothing it should always find")
        pairs = [(p, (ROOT / p).read_text(encoding="utf-8", errors="replace")) for p in paths]
        self.assertEqual(_callers_of_the_environment_reader(pairs), [], "call Get-FpsArmFromLaunch (the launch block) instead of Get-FpsArmFromEnvironment")

    def test_a_result_that_records_observer_controls_is_judged_on_them(self) -> None:
        self.check({
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ utilSample = $true } })": "UNKNOWN",
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ cudaLaunchTimingMinMs = 0 } })": "UNKNOWN",
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ cudaLaunchTimingMinMs = -1; utilSample = $false } })": "UNKNOWN",   # r8: no evidence
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
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT'; corePartitionTelemetry = $false })": "UNKNOWN",   # r8: an off switch is no evidence of LIGHT by itself
            f"Get-FpsArmFromJobResult ([pscustomobject]@{{ telemetryArm = 'LIGHT'; corePartitionTelemetry = $false; {RECORDED_LIGHT_LAUNCH} }})": "LIGHT",
            f"Get-FpsArmFromJobResult ([pscustomobject]@{{ telemetryArm = 'LIGHT'; utilSample = $true; {RECORDED_LIGHT_LAUNCH} }})": "UNKNOWN",
            "Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'HEAVY'; utilSample = $true })": "HEAVY",
        })

    # --- the shared list --------------------------------------------------------------------------------------------------------------------------------
    def test_one_list_drives_the_spec_and_the_result_classifiers(self) -> None:
        # r8: the list is the generator's forwarded switches only (OBSERVER_SPEC_KEYS) (the environment is judged against the LIGHT table instead). Every control must refuse LIGHT in the spec and the result
        # classifier (a result judged beside a LIGHT spec), and its OFF value must not.
        out = self.ok(
            "foreach ($c in Get-FpsObserverControls) {\n"
            "  $row = @{ id = $c.id; specKey = $c.specKey }\n"
            "  $on = switch ($c.specKind) { 'threshold' { 0 } default { $true } }\n"
            "  $off = switch ($c.specKind) { 'threshold' { -1 } default { $false } }\n"
            "  $mk = { param($v) $ga = [pscustomobject]@{ telemetryArm = 'LIGHT' }; $ga | Add-Member -NotePropertyName $c.specKey -NotePropertyValue $v; [pscustomobject]@{ legType = 'speed'; generatorArgs = $ga } }\n"
            "  $row.specOn = (Get-FpsArmFromLegSpec (& $mk $on)).arm; $row.specOff = (Get-FpsArmFromLegSpec (& $mk $off)).arm\n"
            "  $rk = { param($v) $r = [pscustomobject]@{ telemetryArm = 'LIGHT' }; $r | Add-Member -NotePropertyName $c.specKey -NotePropertyValue $v; $r }\n"
            "  $row.resultOn = (Get-FpsArmFromJobResult (& $rk $on) -ObserversFromSpec (& $mk $off)).arm; $row.resultOff = (Get-FpsArmFromJobResult (& $rk $off) -ObserversFromSpec (& $mk $off)).arm\n"
            "  $row | ConvertTo-Json -Compress\n}\n")
        rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
        spec_keys = {r["specKey"] for r in rows if r.get("specKey")}
        self.assertEqual(spec_keys, set(OBSERVER_SPEC_KEYS))
        for r in rows:
            self.assertEqual((r["specOn"], r["resultOn"]), ("UNKNOWN", "UNKNOWN"), r)
            self.assertEqual((r["specOff"], r["resultOff"]), ("LIGHT", "LIGHT"), r)

    def test_a_mid_play_window_toggle_spec_is_not_proof_of_light(self) -> None:
        # r13, adversary pass 15 A1 (introduced): the committed fstoggle legs (master #851) say telemetryArm=LIGHT on a speed leg but drive fs_on / fs_off / restore / maximize inside the measured Play
        # (playback-attr-3-cuda-job.ps1 under -MidPlayWindowToggles), so their spec read LIGHT while the same setting recorded as a launch environment read UNKNOWN. Every reader must agree: UNKNOWN.
        legs = PROFILING / "dual-venue" / "legs"
        specs = sorted(legs.glob("*fstoggle*.json"))
        self.assertGreaterEqual(len(specs), 2)
        for spec in specs:
            doc = json.loads(spec.read_text(encoding="utf-8"))
            self.assertIs(doc["generatorArgs"].get("midPlayWindowToggles"), True, spec.name)
            self.assertEqual(doc["generatorArgs"].get("telemetryArm"), "LIGHT", spec.name)
        path = str(specs[0]).replace("\\", "/")
        spec_expr = f"(Get-Content -Raw -LiteralPath '{path}' | ConvertFrom-Json)"
        out = self.ok(
            f"$s = {spec_expr}\n"
            "Write-Host ('SPEC=' + (Get-FpsArmFromLegSpec $s).arm)\n"
            "Write-Host ('RECORD=' + (Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT'; legType = 'speed'; generatorArgs = $s.generatorArgs })).arm)\n"
            "Write-Host ('RESULT=' + (Get-FpsArmFromJobResult ([pscustomobject]@{ telemetryArm = 'LIGHT' }) -ObserversFromSpec $s).arm)\n"
            "Write-Host ('PLAN=' + (Get-FpsLegPlanRefusal 'fps-ab' @([pscustomobject]@{ item = 1; arm = (Get-FpsArmFromLegSpec $s) }) -Declared)[0])\n")
        self.assertEqual(out.split()[:4], ["SPEC=UNKNOWN", "RECORD=UNKNOWN", "RESULT=UNKNOWN", "PLAN=FPS_AB_UNKNOWN_ARM"])

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

    def test_an_observer_controls_entry_of_any_kind_is_no_evidence_for_a_leg_record(self) -> None:
        # r8 MOOT A3 (r6 A3 lived in the observerControls reading, Read-FpsRecordedObserverControls / Test-FpsRecordHasObserverEvidence, both deleted): the adversary's record was two quiet saved legs
        # with telemetryArm LIGHT, observerControls {"irrelevant":false} and no launch, which the old reader called LIGHT because one unrelated false value made the entry count positive. The
        # record's `observerControls` is neither read nor trusted now: LIGHT needs a launch matching the producers' shape or LIGHT generatorArgs, so every one of these is UNKNOWN, all-off included.
        def rec(controls: str) -> str:
            return f"Get-FpsArmFromLegRecord {self._json('{' + chr(34) + 'telemetryArm' + chr(34) + ':' + chr(34) + 'LIGHT' + chr(34) + ',' + chr(34) + 'observerControls' + chr(34) + ':' + controls + '}')}"
        controls = ['{"irrelevant":false}', '{"someObserverNobodyListed":true}', '{"someObserverNobodyListed":0}', '{"someObserverNobodyListed":false}', '{"cudaLaunchTimingMinMs":0}',
                    '{"utilSample":false,"corePartitionTelemetry":false,"firstPresentCapture":false,"warmReuseMissDump":false,"cudaLaunchTimingMinMs":-1}',
                    '{"utilSample":false,"corePartitionTelemetry":true}', '{}', '"utilSample"', '5', 'true', '["utilSample"]', '[]', 'null', '{"someObserverNobodyListed":null}']
        self.check({rec(c): "UNKNOWN" for c in controls})
        # the same record through the verdict reader every caller uses: the pair is refused as unknown-arm, never allowed
        irrelevant = self._json('{"telemetryArm":"LIGHT","observerControls":{"irrelevant":false}}')
        out = self.ok(f"$a = Get-FpsArmFromLegRecord {irrelevant}\n$v = Get-FpsPairVerdict -Baseline $a -Candidate $a\nWrite-Host ('PAIR=' + $v.allowed + '|' + $v.reason)\n")
        self.assertEqual(out.split(), [f"PAIR=False|{UNKNOWN_REASON}"])

    def test_a_bare_light_leg_record_is_unknown_and_the_pair_is_refused(self) -> None:
        # r5 (Dot on f51e0821, bus 4cd3358 cos-feedback/mlv-app/pr-871.md; Grok, PR comment 7039843864): a saved leg whose only evidence is the arm WORD read LIGHT here while a recorded
        # result with the same word was refused. r8: one evidence rule for both: the word is proof only beside a launch that matches the producers' LIGHT shape, or LIGHT generatorArgs.
        bare = self._json('{"telemetryArm":"LIGHT"}')
        empty = self._json('{"telemetryArm":"LIGHT","observerControls":{}}')
        recorded_off = self._json('{"telemetryArm":"LIGHT","observerControls":{"utilSample":false}}')
        launch = '{"environment":' + LIGHT_ENV_JSON + ',"arguments":' + PRODUCER_ARGS_JSON + '}'
        with_launch = self._json('{"telemetryArm":"LIGHT","launch":' + launch + '}')
        # r10 (G10): generatorArgs proves LIGHT only on a speed leg, so a record with no legType reads UNKNOWN (re-baselined from LIGHT) and the legType 'speed' variant stays LIGHT
        with_ga = self._json('{"telemetryArm":"LIGHT","generatorArgs":{"telemetryArm":"LIGHT","presentTrace":true}}')
        with_ga_speed = self._json('{"telemetryArm":"LIGHT","legType":"speed","generatorArgs":{"telemetryArm":"LIGHT","presentTrace":true}}')
        heavy = self._json('{"telemetryArm":"HEAVY","observerControls":{"utilSample":true}}')
        self.check({
            f"Get-FpsArmFromLegRecord {bare}": "UNKNOWN",
            f"Get-FpsArmFromLegRecord {empty}": "UNKNOWN",
            f"Get-FpsArmFromLegRecord {recorded_off}": "UNKNOWN",
            f"Get-FpsArmFromLegRecord {with_launch}": "LIGHT",
            f"Get-FpsArmFromLegRecord {with_ga}": "UNKNOWN",
            f"Get-FpsArmFromLegRecord {with_ga_speed}": "LIGHT",
            f"Get-FpsArmFromLegRecord {heavy}": "HEAVY",
            # the boundary the reader shares with a recorded result: the same record reads the same arm through both
            f"Get-FpsArmFromJobResult {bare}": "UNKNOWN",
            f"Get-FpsArmFromJobResult {empty}": "UNKNOWN",
            f"Get-FpsArmFromJobResult {recorded_off}": "UNKNOWN",
            f"Get-FpsArmFromJobResult {with_launch}": "LIGHT",
            f"Get-FpsArmFromJobResult {with_ga}": "UNKNOWN",
            f"Get-FpsArmFromJobResult {with_ga_speed}": "LIGHT",
        })
        # through the verdict reader every caller uses (Get-FpsPairVerdict): an uncorroborated bare LIGHT/LIGHT pair is refused as UNKNOWN, a launch-proven pair is allowed
        out = self.ok(
            f"$b = Get-FpsArmFromLegRecord {bare}\n$v = Get-FpsPairVerdict -Baseline $b -Candidate $b\nWrite-Host ('BARE=' + $v.allowed + '|' + $v.reason)\n"
            f"$o = Get-FpsArmFromLegRecord {with_launch}\n$v = Get-FpsPairVerdict -Baseline $o -Candidate $o\nWrite-Host ('LAUNCH=' + $v.allowed + '|' + $v.reason)\n")
        self.assertEqual(out.split(), [f"BARE=False|{UNKNOWN_REASON}", "LAUNCH=True|"])

    def test_a_job_result_ignores_an_observer_controls_entry_too(self) -> None:
        # r8: the result reader shares the leg reader's evidence rule, so an observerControls entry of any kind (all-off included) leaves a result's LIGHT word unproven
        def res(controls: str) -> str:
            return "Get-FpsArmFromJobResult " + self._json('{"telemetryArm":"LIGHT","observerControls":' + controls + '}')
        self.check({res(c): "UNKNOWN" for c in ('{"someObserverNobodyListed":true}', '"utilSample"', '["utilSample"]', '{"someObserverNobodyListed":null}', '{"utilSample":false}', '{"someObserverNobodyListed":0}')})

    def test_viewport_present_diag_is_outside_the_light_shape_in_every_spelling(self) -> None:
        # B2: GpuDisplayViewport.cpp:62-71 envFlagEnabled reads the value trimmed and lowercased: 1 / true / yes / on are ON, everything else (0, false, off, no, empty) is OFF; the app logs
        # from the per-present paths when it is on (:1226-1231 setPresentedImage, :2014-2019 updateTextureIfNeeded). r8: the LIGHT table does not hold the variable (the job drops it from a LIGHT
        # leg), so any recorded value of it, an off spelling included, is UNKNOWN; absent is LIGHT.
        def launch(value: str | None) -> str:
            extra = "" if value is None else f"; MLVAPP_VIEWPORT_PRESENT_DIAG = '{value}'"
            return ("Get-FpsArmFromLaunch ([pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; "
                    f"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; {PRODUCER_LOGDIR_PS}{extra} }}; arguments = @({PRODUCER_ARGS_PS}) }})")
        cases = {launch(v): "UNKNOWN" for v in ("1", "true", "on", "yes", "TRUE", "On", "YES", " 1 ", "1\t", "true\r\n", "0", "false", "off", "no", "", "FALSE", "Off")}
        cases[launch(None)] = "LIGHT"
        self.check(cases)
        # the key compares ignoring case, and the list form of the launch environment reads the same
        self.check({
            "Get-FpsArmFromEnvironment @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','mlvapp_viewport_present_diag=on') @()": "UNKNOWN",
            "Get-FpsArmFromEnvironment @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1','MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1','MLVAPP_VIEWPORT_PRESENT_DIAG=0') @()": "UNKNOWN",
        })
        out = self.ok("$a = " + launch("1") + "\n$v = Get-FpsPairVerdict -Baseline $a -Candidate $a\nWrite-Host ('ALLOWED=' + $v.allowed + '|' + $v.reason)")
        self.assertIn(f"ALLOWED=False|{UNKNOWN_REASON}", out)

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

    def test_a_light_venue_leg_clears_every_inherited_mlvapp_variable_the_job_does_not_set(self) -> None:
        # r5 (adversary A3), r8 (r6 A2, FIXED): the filter above drops only the override the job emits; the smoke child inherits the job's process environment, so a worker carrying an app control
        # the job does not set ran it inside an admitted LIGHT leg while the plan and sidecars said LIGHT (r6's input: MLVAPP_GPU_TEX_NR_READY_BACKLOG_TELEMETRY=1, a variable no list named). A
        # hard-coded list cannot be complete, so a LIGHT leg removes EVERY inherited MLVAPP_* variable of its own process that the job does not itself set for the leg (the names in $envs), and
        # nothing else: another arm keeps them, a name the job sets keeps its inherited value (the explicit override replaces it), the hub fixture flag (read by the runner, not the app) and a
        # non-MLVAPP name are untouched.
        # The clear is a generator-level edit emitted ONLY into a LIGHT job (the publish-write scan's R4 allowlist reads the default text and does not admit the environment members it needs; the
        # emission itself is pinned in test_dual_venue_evidence.GeneratorByteIdentityAndVariantTests). Here its code, extracted between its own markers, runs against a polluted environment.
        job = (PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8")
        region = job.split("# LIGHT-LEG-INHERITED-MLVAPP-CLEAR-1 >>>", 1)[1].split("# LIGHT-LEG-INHERITED-MLVAPP-CLEAR-1 <<<", 1)[0]
        for name in ("MLVAPP_STAGE_TIMING", "MLVAPP_PERF_FIELD_LOG_DIR", "MLVAPP_SMOKE_FIRST_PRESENT_CAPTURE", "MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR"):
            self.assertNotIn(name, region, f"the region keeps no hard-coded observer name list ({name})")
        body = ("$envs = @('MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq', 'QT_OPENGL=desktop'); $env:MLVAPP_GPU_TEX_NR_READY_BACKLOG_TELEMETRY = '1'; $env:MLVAPP_STAGE_TIMING = '1'; "
                "$env:mlvapp_viewport_present_diag = 'on'; $env:MLVAPP_PLAYBACK_QUALITY_MODE = 'inherited'; $env:MLVAPP_HUB_PLAYBACK_CI_FIXTURE = 'CI_FIXTURE_ONLY'; $env:NOT_THE_APP = 'x'\n"
                f"{region}\n"
                "Write-Host ('R=' + [bool]$env:MLVAPP_GPU_TEX_NR_READY_BACKLOG_TELEMETRY + '|' + [bool]$env:MLVAPP_STAGE_TIMING + '|' + [bool]$env:MLVAPP_VIEWPORT_PRESENT_DIAG + '|' + "
                "[string]$env:MLVAPP_PLAYBACK_QUALITY_MODE + '|' + [bool]$env:MLVAPP_HUB_PLAYBACK_CI_FIXTURE + '|' + [bool]$env:NOT_THE_APP)\n")
        rows = [line for line in self.ok(body).splitlines() if line.startswith("R=")]
        self.assertEqual(rows, ["R=False|False|False|inherited|True|True"])
        # r14 (key3 r13 B1 sibling, dictionary-member-shadow; sol r13 B6): a worker variable named Keys (any case) made `.Keys` return its value, so the clear saw one name and cleared nothing;
        # and the app reads DUALISO_DEBUG_SOURCE_SMOOTH_DIR (dualiso.c), a control the MLVAPP_ prefix missed.
        for shadow in ("Keys", "keys", "Values"):
            with self.subTest(shadow=shadow):
                body = (f"$envs = @('MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq'); [Environment]::SetEnvironmentVariable('{shadow}', 'x'); $env:MLVAPP_STAGE_TIMING = '1'; "
                        "$env:DUALISO_DEBUG_SOURCE_SMOOTH_DIR = 'C:\\dump'; $env:NOT_THE_APP = 'x'\n"
                        f"{region}\n"
                        "Write-Host ('R=' + [bool]$env:MLVAPP_STAGE_TIMING + '|' + [bool]$env:DUALISO_DEBUG_SOURCE_SMOOTH_DIR + '|' + [bool]$env:NOT_THE_APP)\n")
                rows = [line for line in self.ok(body).splitlines() if line.startswith("R=")]
                self.assertEqual(rows, ["R=False|False|True"])

    def test_a_flag_or_arm_word_carrying_a_zero_width_character_is_not_the_plain_one(self) -> None:
        # r5 (adversary A1): PowerShell's -ceq / -cne / -cin ignore U+00AD and the zero-width characters, so 'LIGHT'+U+00AD read as LIGHT and ('0'+U+00AD) as the zero the native reader
        # compares byte for byte. Recorded text is compared ordinally.
        shy = "[char]173"
        # r8: the table's exact values are compared ordinally, so a value carrying such a character is not the table's value (it was a HEAVY read of an unlisted "0" before)
        cases = {
            f"Get-FpsArmFromEnvironment @({LIGHT_ENV},('MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq' + {shy})) @()": "UNKNOWN",
            f"Get-FpsArmFromEnvironment @({LIGHT_ENV},'MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq') @()": "LIGHT",
            f"Get-FpsArmFromEnvironment @({LIGHT_ENV},('MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq' + [char]8203)) @()": "UNKNOWN",
            f"Get-FpsArmFromEnvironment @({LIGHT_ENV},('MLVAPP_PLAYBACK_QUALITY_MODE=phase3_hq' + [char]65279)) @()": "UNKNOWN",
            f"Get-FpsArmFromEnvironment @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1',('MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1' + {shy})) @()": "UNKNOWN",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = ('LIGHT' + {shy}); {LIGHT_EVIDENCE}}})": "UNKNOWN",
            f"Get-FpsArmFromJobResult ([pscustomobject]@{{ telemetryArm = ('LIGHT' + {shy}); {LIGHT_EVIDENCE}}})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ([pscustomobject]@{{ legType = 'speed'; generatorArgs = [pscustomobject]@{{ telemetryArm = ('LIGHT' + {shy}) }} }})": "UNKNOWN",
        }
        self.check(cases)
        # a key repeated with a value that differs only by such a character is a malformed environment, not one value
        out = self.ok(f"$e = ConvertTo-FpsEnvMap @('MLVAPP_STAGE_TIMING=0', ('mlvapp_stage_timing=0' + {shy}))\nWrite-Host ('STATE=' + $e.state)")
        self.assertEqual(out.strip(), "STATE=malformed")
        # r8: a repeated key is malformed whatever its values (the old rule refused only repeats whose values differed)
        out = self.ok("$e = ConvertTo-FpsEnvMap @('MLVAPP_STAGE_TIMING=0', 'mlvapp_stage_timing=0')\nWrite-Host ('STATE=' + $e.state)")
        self.assertEqual(out.strip(), "STATE=malformed")

    def test_a_record_whose_generator_args_is_an_array_fails_closed_in_both_readers(self) -> None:
        # r5 (adversary A14): the array holder's properties were invisible to the helpers, and the outer all-off list corroborated the record
        doc = self._json('{"telemetryArm":"LIGHT","observerControls":{"utilSample":false},"generatorArgs":[{"utilSample":true}]}')
        self.check({f"Get-FpsArmFromLegRecord {doc}": "UNKNOWN", f"Get-FpsArmFromJobResult {doc}": "UNKNOWN"})

    def test_a_threshold_below_the_binders_floor_is_not_quiet(self) -> None:
        # r5 (adversary A11): the generator binder is [ValidateRange(-1, 10000)] (playback-attr-3-cuda-job.ps1:442): -1 is the default (off) and a number in [-1, 0) is still off to the job's -ge 0;
        # a number below -1 is a leg that cannot run, so it is never an admitted LIGHT leg
        self.check({
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='-2')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='-1.5')})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='-1')})": "LIGHT",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='-0.5')})": "LIGHT",
            f"Get-FpsArmFromLegSpec ({_spec(cudaLaunchTimingMinMs='0')})": "UNKNOWN",
        })

    def test_a_malformed_launch_block_cannot_read_light_through_the_comparators(self) -> None:
        # r5 (adversary A6): Get-NestedValue unrolled a one-element "launch" array into the object inside, so the comparators classified a malformed record LIGHT. They read the block with Get-FaProp now.
        # r14 (sol r13 B2): the GUI comparator reads the whole smoke record through the module's Get-FpsArmFromSmokeRecord, which reads the launch block with Get-FaProp itself
        module = MODULE.read_text(encoding="utf-8")
        smoke_reader = module[module.index("function Get-FpsArmFromSmokeRecord"):module.index("function Get-FpsPairVerdict")]
        self.assertIn("Get-FpsArmFromLaunch (Get-FaProp $Record 'launch')", smoke_reader)
        compare_source = COMPARE_SMOKE_AB_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('Get-FpsArmFromLaunch (Get-NestedValue', compare_source)
        self.assertIn("$beforeArm = Get-FpsArmFromSmokeRecord $BeforeSmoke", compare_source)
        self.assertIn("$afterArm = Get-FpsArmFromSmokeRecord $AfterSmoke", compare_source)
        # r14 (adversary r14 A1): the CUDA A/B smoke reader judges the whole result through the same module reader
        cuda_source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('Get-FpsArmFromLaunch (Get-NestedValue', cuda_source)
        self.assertIn("Get-FpsArmFromSmokeRecord $Json", cuda_source)
        out = self.ok("$j = ConvertFrom-Json '{\"launch\":[{\"environment\":{\"MLVAPP_PLAYBACK_SMOKE_TELEMETRY\":\"1\",\"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG\":\"1\"},\"arguments\":[]}]}'\n"
                      "Write-Host ('ARM=' + (Get-FpsArmFromLaunch (Get-FaProp $j 'launch')).arm)")
        self.assertEqual(out.strip(), "ARM=UNKNOWN")

    def test_a_light_arm_object_that_contradicts_its_recorded_launch_is_not_light(self) -> None:
        # r5 (adversary A2): an arm object returned as it was; a leg that also records its launch must agree with it, and an object that does not name the launch environment as its source is a label
        launch_heavy = "launch = [pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; MLVAPP_VIEWPORT_PRESENT_DIAG = 'yes' }; arguments = @() }"
        launch_light = RECORDED_LIGHT_LAUNCH
        self.check({
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x'); {launch_heavy} }})": "UNKNOWN",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x'); {launch_light} }})": "LIGHT",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = [pscustomobject]@{ arm = 'LIGHT' } })": "UNKNOWN",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = [pscustomobject]@{{ arm = 'LIGHT' }}; {LIGHT_EVIDENCE}}})": "LIGHT",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x') })": "UNKNOWN",
            # r5 pass 2 A3: the `source` label does not exempt an arm object from evidence, and a null launch is no launch
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = [pscustomobject]@{ arm = 'LIGHT'; source = 'environment' } })": "UNKNOWN",
            "Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = [pscustomobject]@{ arm = 'LIGHT'; source = 'environment' }; launch = $null })": "UNKNOWN",
        })

    def test_a_light_label_that_contradicts_the_rest_of_its_record_is_not_light(self) -> None:
        # r5 pass 2 A2: a STRING arm skipped launch corroboration and an embedded generatorArgs.telemetryArm was never compared
        stage = "launch = [pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1'; MLVAPP_STAGE_TIMING = '1' }; arguments = @() }"
        self.check({
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {stage} }})": "UNKNOWN",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}generatorArgs = [pscustomobject]@{{ telemetryArm = 'HEAVY' }} }})": "UNKNOWN",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = (New-FpsArm 'LIGHT' 'environment' 'x'); {RECORDED_LIGHT_LAUNCH}; generatorArgs = [pscustomobject]@{{ telemetryArm = 'HEAVY' }} }})": "UNKNOWN",
            f"Get-FpsArmFromJobResult ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}generatorArgs = [pscustomobject]@{{ telemetryArm = 'HEAVY' }} }})": "UNKNOWN",
            # the agreeing controls: a recorded LIGHT launch alone is evidence, and an embedded LIGHT is no contradiction
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {RECORDED_LIGHT_LAUNCH} }})": "LIGHT",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT' }} }})": "LIGHT",
        })

    def test_a_scalar_generator_args_fails_closed_in_both_readers(self) -> None:
        # r5 pass 2 A4: an array was refused but a scalar generatorArgs was read as absent, and the outer all-off list corroborated the record
        cases = {}
        for literal in ('"broken"', '""', '" "', "0", "1", "true", "false", "null"):
            doc = self._json('{"telemetryArm":"LIGHT","observerControls":{"utilSample":false},"generatorArgs":' + literal + '}')
            cases[f"Get-FpsArmFromLegRecord {doc}"] = "UNKNOWN"
            cases[f"Get-FpsArmFromJobResult {doc}"] = "UNKNOWN"
        self.check(cases)

    def test_a_control_character_in_a_recorded_value_or_key_is_not_the_plain_one(self) -> None:
        # r5 pass 2 A1: `-in` and `-ieq` are culture compares too: "1" + U+0001 read as the frame-log opt-out "1", and an observerControls key with U+00AD matched the listed threshold's name
        # and so skipped the nonzero-unlisted-entry refusal
        def launch(value: str, suffix: str) -> str:
            return ("Get-FpsArmFromLaunch ([pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; " + PRODUCER_LOGDIR_PS + "; "
                    "MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = ('" + value + "' + " + suffix + ") }; arguments = @(" + PRODUCER_ARGS_PS + ") })")
        self.check({
            launch("1", "[char]1"): "UNKNOWN",
            launch("on", "[char]173"): "UNKNOWN",
            launch("0", "[char]173"): "UNKNOWN",
            launch("1", "''"): "LIGHT",
            self._shy_key_record(): "UNKNOWN",
        })

    def _shy_key_record(self) -> str:
        return ("Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ ('cudaLaunchTimingMinMs' + [char]173) = -1 } })")

    def test_every_generator_arg_the_venue_leg_forwards_is_classified(self) -> None:
        # The class guard: a NEW generatorArgs key must be named observer (module list) or not-an-observer (below), so none reaches the job unclassified.
        source = (PROFILING / "dual-venue" / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        keys = set(re.findall(r"\$spec\.generatorArgs\.PSObject\.Properties\['(\w+)'\]", source))
        self.assertGreater(len(keys), 30)
        not_observers = {
            "telemetryArm", "cpuQuiescenceThresholdPercent", "playbackProcessing", "qualityMode", "swapInterval", "playbackConform", "rawPrefetchDecoders", "benchAffinityMask",
            "partitionLoadMask", "partitionLoadThreads", "corePartitionHelperMask", "playbackMaxThreads", "gpuSafeStart", "automationSettingsDir", "gpuPreLaunches",
            "disableLookAssist", "presentTrace", "unpaced", "disablePrepPointwiseFast", "prepConflatedMove", "disableReconReleaseDeferred", "dueWakeDeliveryPost", "reconOmpThreads",
            "prepOmpThreads", "scope", "scopeIntervalMs", "scopesDuringPlayback", "forceLookAssist", "lookFlavor", "playbackRenderLookaheadFrames", "lookReceipt", "lookReceiptSha256",
        }
        out = self.ok("(Get-FpsObserverControls | Where-Object { $_.specKey } | ForEach-Object { $_.specKey }) -join ','")
        observers = set(out.strip().split(","))
        self.assertEqual(sorted(keys - observers - not_observers), [], "classify each new generatorArgs key: add it to Get-FpsObserverControls or to not_observers here")


@requires_pwsh
class LightShapeAllowlistTests(_Probe):
    """BENCH-LIGHT-ARM-GAIN-GATES-1 r8, THE NARROW PROMISE: LIGHT only on the producers' exact closed shape (two data tables in FpsArmGate.psm1), UNKNOWN on anything else."""

    def arms(self, exprs: list[str]) -> list[str]:
        return self.ok("".join(f"Write-Host ('ARM=' + ({e}).arm)\n" for e in exprs)).split()

    def check(self, cases: dict[str, str]) -> None:
        self.assertEqual(dict(zip(cases, self.arms(list(cases)))), {expr: f"ARM={want}" for expr, want in cases.items()})

    def test_every_row_of_the_light_table_is_emitted_by_a_producer(self) -> None:
        # the table is derived from the producers, never invented: each argument flag is a literal of the smoke runner, each environment name a literal of the runner, the venue job or the length gate
        runner = (PROFILING / "run-release-gui-smoke.ps1").read_text(encoding="utf-8")
        sources = runner + (PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8") + (PROFILING / "gui-smoke-length-gate.ps1").read_text(encoding="utf-8")
        shape = json.loads(self.ok("Get-FpsLightShape | ConvertTo-Json -Depth 5 -Compress").strip().splitlines()[-1])
        self.assertGreater(len(shape["arguments"]), 15)
        self.assertGreater(len(shape["environment"]), 25)
        missing = [row["name"] for row in shape["arguments"] if f'"{row["name"]}' not in runner]
        missing += [row["name"] for row in shape["environment"] if row["name"] not in sources]
        self.assertEqual(missing, [], "a table row no producer emits")
        for row in shape["arguments"] + shape["environment"]:
            self.assertRegex(row["rule"], r"^(|int|num|word|text|exact:.+|oneof:.+)$", row)

    def test_the_a1_contact_sheet_input_is_unknown_for_a_saved_record_a_launch_and_the_pair(self) -> None:
        # A1 (r6, FIXED): the adversary's launch (contact-sheet options, telemetry on with the frame-log opt-out) read LIGHT through the saved-leg reader and the launch reader and the pair was allowed
        out = self.ok(
            f"$launch = [pscustomobject]@{{ environment = [pscustomobject]@{{ {LIGHT_LAUNCH_ENV_PS} }}; "
            f"arguments = @({PRODUCER_ARGS_PS}, '--contact-sheet-dir', 'C:\\Temp\\sheet', '--contact-sheet-frames', '6') }}\n"
            "$rec = [pscustomobject]@{ telemetryArm = 'LIGHT'; launch = $launch }\n"
            f"$good = [pscustomobject]@{{ telemetryArm = 'LIGHT'; launch = [pscustomobject]@{{ environment = $launch.environment; arguments = @({PRODUCER_ARGS_PS}) }} }}\n"
            "$a = Get-FpsArmFromLegRecord $rec; $g = Get-FpsArmFromLegRecord $good\n"
            "Write-Host ('LEG=' + $a.arm + ' LAUNCH=' + (Get-FpsArmFromLaunch $launch).arm + ' GOOD=' + $g.arm)\n"
            "$v = Get-FpsPairVerdict -Baseline $g -Candidate $a\nWrite-Host ('PAIR=' + $v.allowed + '|' + $v.reason)\n")
        self.assertEqual(out.split(), ["LEG=UNKNOWN", "LAUNCH=UNKNOWN", "GOOD=LIGHT", f"PAIR=False|{UNKNOWN_REASON}"])

    def test_moot_a4_a_repeated_environment_key_is_unknown(self) -> None:
        # r6 A4 lived in the duplicate-value check of the environment reader; the allowlist makes the adversary's input (MLVAPP_STAGE_TIMING twice, "1" then "0") UNKNOWN twice over: the key is
        # not in the table, and a repeated key is malformed whatever its values. A parser that collapses a duplicate JSON key to its last value still leaves the key outside the table.
        out = self.ok(
            "$json = '{\"environment\":{\"MLVAPP_STAGE_TIMING\":\"1\",\"MLVAPP_STAGE_TIMING\":\"0\",\"MLVAPP_PLAYBACK_SMOKE_TELEMETRY\":\"1\",\"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG\":\"1\"},\"arguments\":[\"--gui-smoke\"]}'\n"
            "try { $l = $json | ConvertFrom-Json; Write-Host ('JSON=' + (Get-FpsArmFromLaunch $l).arm) } catch { Write-Host 'JSON=PARSE_REFUSED' }\n"
            "Write-Host ('LIST=' + (Get-FpsArmFromEnvironment @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1', 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG=1', 'MLVAPP_STAGE_TIMING=1', 'MLVAPP_STAGE_TIMING=0') @()).arm)\n"
            "Write-Host ('LISTSAME=' + (Get-FpsArmFromEnvironment @('MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1', 'MLVAPP_PLAYBACK_SMOKE_TELEMETRY=1') @()).arm)\n")
        lines = dict(line.split("=", 1) for line in out.split())
        self.assertIn(lines["JSON"], ("UNKNOWN", "PARSE_REFUSED"))
        self.assertEqual((lines["LIST"], lines["LISTSAME"]), ("UNKNOWN", "UNKNOWN"))

    def test_sol_r13_b4_a_repeated_json_key_is_refused_before_the_parser_keeps_one_value(self) -> None:
        # r14 (sol r13 B4, adversary r14 A2): ConvertFrom-Json keeps the LAST value of a repeated key, so a launch whose opt-out was written "0" then "1" read LIGHT. The raw text is checked first.
        dup = ('{"launch":{"environment":{"MLVAPP_PLAYBACK_SMOKE_TELEMETRY":"1","MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG":"0",'
               '"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG":"1","MLVAPP_CRASH_FORENSICS_LOG_DIR":"C:\\\\logs"},"arguments":' + PRODUCER_ARGS_JSON + '}}')
        clean = dup.replace('"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG":"0",', "")
        case_dup = clean.replace('"MLVAPP_CRASH_FORENSICS_LOG_DIR"', '"mlvapp_playback_smoke_telemetry":"1","MLVAPP_CRASH_FORENSICS_LOG_DIR"')
        nested = '[{"a":1},{"b":{"c":[{"k":1,"k":2}]}}]'
        body = ""
        for name, text in (("DUP", dup), ("CLEAN", clean), ("CASE", case_dup), ("NESTED", nested), ("BAD", "{oops")):
            body += f"$t = @'\n{text}\n'@\nWrite-Host ('{name}=' + $(if ($null -eq (Get-FpsJsonRepeatedKeyProblem $t)) {{ 'none' }} else {{ 'repeat' }}))\n"
        body += (f"$t = @'\n{clean}\n'@\nWrite-Host ('CLEANARM=' + (Get-FpsArmFromSmokeRecord ($t | ConvertFrom-Json)).arm)\n"
                 f"$t = @'\n{dup}\n'@\ntry {{ Assert-FpsJsonNoRepeatedKey $t 'probe'; Write-Host 'ASSERT=passed' }} catch {{ Write-Host ('ASSERT=' + $_.Exception.Message.Split(':')[0]) }}\n")
        lines = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(lines, {"DUP": "repeat", "CLEAN": "none", "CASE": "repeat", "NESTED": "repeat", "BAD": "repeat", "CLEANARM": "LIGHT", "ASSERT": "FPS_JSON_REPEATED_KEY"})

    def test_every_reader_of_a_judged_record_checks_for_a_repeated_key_before_it_parses(self) -> None:
        # the class guard for sol r13 B4 / adversary r14 A2: each parse site of a record, smoke result, summary or plan the arm gate judges calls Assert-FpsJsonNoRepeatedKey on the raw text
        # before ConvertFrom-Json, so a new reader of one of those files that parses directly is a decision someone makes here.
        sites = {
            "compare-release-gui-smoke-ab.ps1": ["Assert-FpsJsonNoRepeatedKey $raw $resolved"],
            "run-release-cuda-playback-ab.ps1": ["Assert-FpsJsonNoRepeatedKey $raw $Path"],
            "compare-machine-perf.ps1": ["Assert-FpsJsonNoRepeatedKey $raw $resolved", 'Assert-FpsJsonNoRepeatedKey $line "${resolved}:$lineNumber"'],
            "summarize-local-cuda-proof.ps1": ["Assert-FpsJsonNoRepeatedKey $summaryRaw $summaryFullPath"],
            "run-local-cuda-playback-dng-smoke.ps1": ["Assert-FpsJsonNoRepeatedKey $playbackAbRaw $playbackAbSummaryPath"],
            "dual-venue/Invoke-VenueLeaseBatch.ps1": ["Assert-FpsJsonNoRepeatedKey $specText $Path", "Assert-FpsJsonNoRepeatedKey $planText $planPath"],
        }
        # (the other ConvertFrom-Json reads in these scripts are not judged by the arm gate: a screenshot manifest, a logged-metadata line, a clip descriptor, the single-leg playback smoke
        # summary and the CDNG matrix summary)
        for rel, calls in sites.items():
            source = (PROFILING / rel).read_text(encoding="utf-8")
            for call in calls:
                self.assertIn(call, source, rel)

    def test_the_whole_table_is_light_and_nothing_added_to_it_is(self) -> None:
        # the full producer shape (every table environment row at the value the producers emit) is LIGHT, and each single outside addition (a name, a value, a flag) turns it UNKNOWN
        body = ("$shape = Get-FpsLightShape\n$map = [ordered]@{}\n"
                "foreach ($row in $shape.environment) {\n"
                "  $v = switch -Regex ($row.rule) { '^exact:(.*)$' { $Matches[1] } '^oneof:([^|]*)' { $Matches[1] } '^int$' { '4' } '^num$' { '7.5' } '^word$' { 'amaze' } default { 'C:\\x\\y' } }\n"
                "  $map[$row.name] = $v\n}\n"
                "Write-Host ('FULL=' + (Get-FpsArmFromEnvironment $map @()).arm)\n"
                "$extra = [ordered]@{}; foreach ($k in $map.Keys) { $extra[$k] = $map[$k] }; $extra['MLVAPP_ANYTHING_ELSE'] = '0'\n"
                "Write-Host ('EXTRA=' + (Get-FpsArmFromEnvironment $extra @()).arm)\n"
                "$bad = [ordered]@{}; foreach ($k in $map.Keys) { $bad[$k] = $map[$k] }; $bad['MLVAPP_GPU_PLAYBACK_RECON'] = '2'\n"
                "Write-Host ('VALUE=' + (Get-FpsArmFromEnvironment $bad @()).arm)\n")
        lines = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(lines, {"FULL": "LIGHT", "EXTRA": "UNKNOWN", "VALUE": "UNKNOWN"})

    def test_dictionary_member_shadow_a_recorded_key_named_keys_hides_nothing(self) -> None:
        # r14 (key3 r13 B1, catalogue row dictionary-member-shadow): the environment reader enumerated `$Map.Keys`, and on an IDictionary PowerShell resolves `.Keys` to an ENTRY named Keys (any
        # case) before the property, so the allowlist loop saw one non-MLVAPP name and judged nothing: the key3 repro (a LIGHT launch plus MLVAPP_STAGE_TIMING=1 and Keys='x') read LIGHT. The
        # dictionary-shaped environment (ConvertTo-FpsEnvMap) had the same defect one step earlier: a Keys entry naming one variable hid a fully HEAVY environment.
        def launch(extra: str, env_kind: str = "[pscustomobject]") -> str:
            return (f"Get-FpsArmFromLaunch ([pscustomobject]@{{ environment = {env_kind}@{{ {LIGHT_LAUNCH_ENV_PS}; {extra} }}; arguments = @({PRODUCER_ARGS_PS}) }})")
        cases = {}
        for shadow in ("Keys", "keys", "KEYS", "Values", "Count", "GetEnumerator"):
            cases[launch(f"MLVAPP_STAGE_TIMING = '1'; {shadow} = 'x'")] = "UNKNOWN"
            cases[launch(f"MLVAPP_STAGE_TIMING = '1'; {shadow} = 'x'", "[ordered]")] = "UNKNOWN"
            cases[launch(f"{shadow} = 'x'")] = "LIGHT"   # the extra non-app name itself changes nothing
        cases[launch("MLVAPP_GPU_PLAYBACK_RECON_ASYNC_H2D = '1'; Keys = 'x'")] = "UNKNOWN"   # the value check is not skipped either
        heavy = ("Get-FpsArmFromLaunch ([pscustomobject]@{ environment = [ordered]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; " + PRODUCER_LOGDIR_PS +
                 "; Keys = 'MLVAPP_CRASH_FORENSICS_LOG_DIR' }; arguments = @(" + PRODUCER_ARGS_PS + ") })")
        cases[heavy] = "HEAVY"
        cases[launch("DUALISO_DEBUG_SOURCE_SMOOTH_DIR = 'C:\\dump'")] = "UNKNOWN"   # sol r13 B6: a DUALISO_ app control is outside the shape too
        self.check(cases)
        # the verdict's exact repro, through the saved-leg reader and the pair verdict
        out = self.ok(
            "$l = [pscustomobject]@{ environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY='1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG='1'; "
            "MLVAPP_CRASH_FORENSICS_LOG_DIR='C:\\logs'; MLVAPP_STAGE_TIMING='1'; Keys='x' }; arguments=@(" + PRODUCER_ARGS_PS + ") }\n"
            "$a = Get-FpsArmFromLegRecord ([pscustomobject]@{ telemetryArm = 'LIGHT'; launch = $l })\n"
            f"$b = Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {RECORDED_LIGHT_LAUNCH} }})\n"
            "$v = Get-FpsPairVerdict -Baseline $b -Candidate $a\nWrite-Host ('REPRO=' + (Get-FpsArmFromLaunch $l).arm + '|' + $a.arm + '|' + $v.allowed)\n")
        self.assertEqual(out.strip(), "REPRO=UNKNOWN|UNKNOWN|False")

    def test_sol_r13_a_spec_with_a_fault_injection_or_an_embedded_observer_controls_entry_is_not_light(self) -> None:
        # r14 (sol r13 B1, B2): the spec reader admitted LIGHT for generatorArgs carrying gpuFaultInject (a timed device loss inside the measured Play; the launch reader already refuses
        # MLVAPP_GPU_FAULT_INJECT) or an observerControls entry (the record reader already refuses it). Spec, result-with-spec and the plan rule now agree on UNKNOWN.
        fault = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT'; gpuFaultInject = '702@120' } }"
        oc = "[pscustomobject]@{ legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{ utilSample = $true } } }"
        self.check({
            f"Get-FpsArmFromLegSpec {fault}": "UNKNOWN",
            f"Get-FpsArmFromLegSpec {oc}": "UNKNOWN",
            f"Get-FpsArm -Spec {fault} -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})": "UNKNOWN",
            f"Get-FpsArm -Spec {oc} -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})": "UNKNOWN",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; legType = 'speed'; generatorArgs = ({fault}).generatorArgs }})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ([pscustomobject]@{{ legType = 'speed'; generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT'; gpuFaultInject = $null }} }})": "LIGHT",
        })
        out = self.ok(f"$p = Get-FpsLegPlanRefusal -Purpose 'fps-ab' -Items @([pscustomobject]@{{ item = 1; arm = (Get-FpsArmFromLegSpec {fault}) }}, "
                      f"[pscustomobject]@{{ item = 2; arm = (Get-FpsArmFromLegSpec {oc}) }})\nWrite-Host ('PLAN=' + ($p -join '|').Substring(0, 21))")
        self.assertEqual(out.strip(), "PLAN=FPS_AB_UNKNOWN_ARM|1|")

    def test_a_look_leg_spec_is_not_a_light_fps_leg_whatever_its_arm_word_says(self) -> None:
        # the spec-side sibling of A1: a look leg grabs its contact sheet in the timed Play (Invoke-VenueLeg.ps1:586-589), so a LIGHT word on it is not proof; only legType 'speed' is
        ga = "generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }"
        self.check({
            f"Get-FpsArmFromLegSpec ([pscustomobject]@{{ legType = 'speed'; {ga} }})": "LIGHT",
            f"Get-FpsArmFromLegSpec ([pscustomobject]@{{ legType = 'look'; {ga}; look = [pscustomobject]@{{ contactSheetFrames = 6 }} }})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ([pscustomobject]@{{ {ga} }})": "UNKNOWN",
            f"Get-FpsArmFromLegSpec ([pscustomobject]@{{ legType = 'Speed'; {ga} }})": "UNKNOWN",
            f"Get-FpsArm -Spec ([pscustomobject]@{{ legType = 'look'; {ga} }}) -Result ([pscustomobject]@{{ telemetryArm = 'LIGHT' }})": "UNKNOWN",
        })
        out = self.ok("$p = Get-FpsLegPlanRefusal -Purpose 'fps-ab' -Items @([pscustomobject]@{ item = 1; arm = (Get-FpsArmFromLegSpec ([pscustomobject]@{ legType = 'look'; "
                      "generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' } })) })\nWrite-Host ('PLAN=' + ($p -join '|').Substring(0, 21))")
        self.assertEqual(out.strip(), "PLAN=FPS_AB_UNKNOWN_ARM|1|")

    def test_a3_a_look_leg_carried_by_a_saved_record_or_result_is_not_light(self) -> None:
        # r8 adversary A3: the spec reader refused a look spec, but a saved record or a result that embeds the same fields (record-level legType 'look', a `look` block, generatorArgs LIGHT)
        # read LIGHT through the record readers, and the pair was allowed. A record that carries a look block or any legType but 'speed' is outside the LIGHT shape.
        ga = "generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }"
        cases = {}
        for reader in ("Get-FpsArmFromLegRecord", "Get-FpsArmFromJobResult"):
            cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; legType = 'look'; {ga}; look = [pscustomobject]@{{ contactSheetFrames = 6 }} }})"] = "UNKNOWN"
            cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {ga}; look = [pscustomobject]@{{ contactSheetFrames = 6 }} }})"] = "UNKNOWN"
            cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; legType = 'look'; {ga} }})"] = "UNKNOWN"
            cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; legType = 'speed'; {ga} }})"] = "LIGHT"
            cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; legType = 'look'; {RECORDED_LIGHT_LAUNCH} }})"] = "UNKNOWN"
        self.check(cases)
        out = self.ok(f"$a = Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; legType = 'look'; {ga}; look = [pscustomobject]@{{ contactSheetFrames = 6 }} }})\n"
                      f"$b = Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {RECORDED_LIGHT_LAUNCH} }})\n"
                      "$v = Get-FpsPairVerdict -Baseline $b -Candidate $a\nWrite-Host ('PAIR=' + $v.allowed + '|' + $v.reason)")
        self.assertEqual(out.strip(), f"PAIR=False|{UNKNOWN_REASON}")

    def test_a4_an_observer_controls_entry_beside_corroborating_evidence_is_not_light(self) -> None:
        # r8 adversary A4: a record with LIGHT generatorArgs (or a launch) AND an observerControls entry that says an observer is enabled read LIGHT, because the entry is no longer read and the
        # other field corroborated. No producer records an observerControls entry at all, so its mere presence is outside the LIGHT shape (a presence test, not a reading: the entry's value is never
        # interpreted, so none of the deleted denylist logic returns). The reverse stays: a record with no such key and a proof is LIGHT.
        cases = {}
        for reader in ("Get-FpsArmFromLegRecord", "Get-FpsArmFromJobResult"):
            for controls in ("[pscustomobject]@{ utilSample = $true }", "[pscustomobject]@{ corePartitionTelemetry = $true }", "[pscustomobject]@{ firstPresentCapture = $true }",
                             "[pscustomobject]@{ utilSample = $false }", "[pscustomobject]@{ }", "$null"):
                cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT' }}; observerControls = {controls} }})"] = "UNKNOWN"
                cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {RECORDED_LIGHT_LAUNCH}; observerControls = {controls} }})"] = "UNKNOWN"
            cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT'; observerControls = [pscustomobject]@{{ utilSample = $true }} }} }})"] = "UNKNOWN"
            # r10 (G10): LIGHT generatorArgs prove LIGHT only on a speed leg; with no legType the record is UNKNOWN (re-baselined from LIGHT), and the legType 'speed' variant stays LIGHT
            cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT' }} }})"] = "UNKNOWN"
            cases[f"{reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; legType = 'speed'; generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT' }} }})"] = "LIGHT"
        self.check(cases)

    def test_g10_a_record_with_no_speed_legtype_and_light_generator_args_is_unknown_through_the_pair_verdict(self) -> None:
        # r10 G10 (Grok ask 7060550362): the record path refused only a PRESENT non-speed legType and the generatorArgs path never looked at legType, while the spec reader needs exactly
        # 'speed'. A record with telemetryArm LIGHT, LIGHT generatorArgs and NO legType read LIGHT and the pair was allowed. RED on 5aa4a230: allowed = True for the legType-absent pair.
        ga = "generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }"
        out = self.ok(
            "".join(
                f"$a = {reader} ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {extra}{ga} }})\n"
                f"$v = Get-FpsPairVerdict -Baseline $a -Candidate $a\nWrite-Host ('{label}=' + $v.allowed + '|' + $v.reason)\n"
                for reader in ("Get-FpsArmFromLegRecord", "Get-FpsArmFromJobResult")
                for label, extra in ((f"{reader}-ABSENT", ""), (f"{reader}-SPEED", "legType = 'speed'; "))))
        self.assertEqual(out.split(), [
            f"Get-FpsArmFromLegRecord-ABSENT=False|{UNKNOWN_REASON}", "Get-FpsArmFromLegRecord-SPEED=True|",
            f"Get-FpsArmFromJobResult-ABSENT=False|{UNKNOWN_REASON}", "Get-FpsArmFromJobResult-SPEED=True|"])

    def test_g10_one_speed_leg_predicate_gives_a_record_and_a_spec_the_same_answer_for_every_legtype(self) -> None:
        # r10 G10 parity: one table of legType values fed to a record and to a spec that are otherwise LIGHT; each row reads LIGHT on both or on neither (one private predicate, Test-FpsSpeedLeg)
        zws = "[string]::new(@([char]0x200B))"
        rows = {
            "absent": None, "null": "$null", "empty": "''", "speed": "'speed'", "SPEED": "'SPEED'", "space-speed": "' speed'", "speed-zwsp": f"('speed' + {zws})",
            "look": "'look'", "export": "'export'", "int7": "7",
        }
        ga = "generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }"
        body = ""
        for name, value in rows.items():
            lt = "" if value is None else f"legType = {value}; "
            body += (f"Write-Host ('{name}|' + (Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; {lt}{ga} }})).arm"
                     f" + '|' + (Get-FpsArmFromLegSpec ([pscustomobject]@{{ {lt}{ga} }})).arm)\n")
        got = {}
        for line in self.ok(body).split():
            name, record, spec = line.split("|")
            got[name] = (record, spec)
        self.assertEqual(set(got), set(rows))
        for name, (record, spec) in got.items():
            self.assertEqual(record == "LIGHT", spec == "LIGHT", f"{name}: record={record} spec={spec}")
        self.assertEqual({n for n, (r, s) in got.items() if r == "LIGHT"}, {"speed"})

    def test_a8_an_empty_or_partial_recorded_launch_is_not_light_through_every_reader(self) -> None:
        # r8 adversary A8: {"arguments":[],"environment":{}} matched both tables vacuously and read LIGHT through the launch reader and the saved-leg reader. LIGHT needs the members every producer
        # launch records (the rows marked required: --gui-smoke-playback, --input, --seconds, --start-frame, the settle options, the crash-log directory).
        empty = "[pscustomobject]@{ arguments = @(); environment = [pscustomobject]@{ } }"
        no_dir = f"[pscustomobject]@{{ arguments = @({PRODUCER_ARGS_PS}); environment = [pscustomobject]@{{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '1' }} }}"
        full = f"[pscustomobject]@{{ arguments = @({PRODUCER_ARGS_PS}); environment = [pscustomobject]@{{ {LIGHT_LAUNCH_ENV_PS} }} }}"
        self.check({
            f"Get-FpsArmFromLaunch ({empty})": "UNKNOWN",
            f"Get-FpsArmFromLaunch ({no_dir})": "UNKNOWN",
            f"Get-FpsArmFromLaunch ({full})": "LIGHT",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; launch = {empty} }})": "UNKNOWN",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; launch = {full} }})": "LIGHT",
            f"Get-FpsArmFromJobResult ([pscustomobject]@{{ telemetryArm = 'LIGHT'; launch = {empty} }})": "UNKNOWN",
            # the HEAVY reading is a positive fact about a table-clean launch and does not need the mandatory members
            "Get-FpsArmFromLaunch ([pscustomobject]@{ arguments = @(); environment = [pscustomobject]@{ MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1' } })": "HEAVY",
        })
        shape = json.loads(self.ok("Get-FpsLightShape | ConvertTo-Json -Depth 5 -Compress").strip().splitlines()[-1])
        required = sorted(r["name"] for r in shape["arguments"] + shape["environment"] if r["required"])
        self.assertEqual(required, sorted(["--gui-smoke-playback", "--input", "--seconds", "--start-frame", "--settle-ms", "--settle-cpu-percent", "--settle-cpu-stable-ms", "--settle-cpu-max-ms",
                                           "MLVAPP_CRASH_FORENSICS_LOG_DIR"]))

    def test_a_launch_outside_the_shape_is_never_light_through_any_reader(self) -> None:
        # one record, every reader: a launch with a flag outside the table is UNKNOWN through the launch reader, the saved-leg reader and the result reader alike
        launch = (f"[pscustomobject]@{{ environment = [pscustomobject]@{{ {LIGHT_LAUNCH_ENV_PS} }}; "
                  f"arguments = @({PRODUCER_ARGS_PS}, '--launch-only') }}")
        self.check({
            f"Get-FpsArmFromLaunch ({launch})": "UNKNOWN",
            f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; launch = {launch} }})": "UNKNOWN",
            f"Get-FpsArmFromJobResult ([pscustomobject]@{{ telemetryArm = 'LIGHT'; launch = {launch} }})": "UNKNOWN",
            f"Get-FpsArmFromJobResult ([pscustomobject]@{{ telemetryArm = 'LIGHT'; launch = {launch} }}) -ObserversFromSpec ({SPEC_LIGHT})": "UNKNOWN",
        })


if __name__ == "__main__":
    unittest.main()
