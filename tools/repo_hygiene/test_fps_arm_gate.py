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
# the comparators import VenueChain.psm1's strict host-load rule and its repeated-member refusal at script top (master, VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1); the probe imports the same functions
VENUE_CHAIN_MODULE = PROFILING / "dual-venue" / "VenueChain.psm1"
VENUE_CHAIN_IMPORT = (f"Import-Module '{VENUE_CHAIN_MODULE}' -Function ConvertFrom-VenueSmokeHostLoad, Get-VenueReferenceHostLoadRefusal,"
                      " ConvertFrom-VenueSmokeResultText, Find-VenueJsonDuplicateMember -DisableNameChecking\n")

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
        body = f"Import-Module '{MODULE}' -Force\n" + VENUE_CHAIN_IMPORT
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

    def test_envelope_preempt_residue_parse_level_only_keeps_repeats_and_drops_the_shape_rules(self) -> None:
        # ENVELOPE-PREEMPT-RESIDUE-1 r1 (sol r16 + key3 fable r16): the lease batch's plan read runs this BEFORE its positional BAD_ITEM_SHAPE check, so the shape rules (envelope array, root
        # kind, the depth-256 cap) must not fire here; they run after the item checks without the switch. A repeated key and unparsable text stay problems (parse-level defects). The
        # receipt readers' default call is unchanged: the same inputs without the switch are still problems (KEEP item 3).
        deep = "[" * 257 + "null" + "]" * 257
        shape_inputs = {
            "NESTED_ENVELOPE": '[[{"proof":[]}]]',
            "LEGS_NESTED_ENVELOPE": '{"legs":[[{"proof":[]}]]}',
            "TOP_ENVELOPE": '{"legs":[null],"proof":[]}',
            "HOST_SIBLING": '[null,{"host":[]}]',
            "DEEP257": deep,
            "SCALAR": '"plan"',
        }
        parse_inputs = {"DUP": '[null,{"k":1,"k":2}]', "CASEDUP": '[null,{"a":1,"A":2}]', "DUPLEGS": '{"legs":[null],"legs":[null]}', "BAD": "[oops"}
        body = ""
        for label, text in {**shape_inputs, **parse_inputs}.items():
            for mode, flag in (("PLO", " -ParseLevelOnly"), ("STRICT", " -AllowArrayRoot"), ("DEFAULT", "")):
                body += f"$p = Get-FpsJsonRepeatedKeyProblem '{text}'{flag}\nWrite-Host ('{label}_{mode}=' + $(if ($null -eq $p) {{ 'NONE' }} else {{ 'PROBLEM' }}))\n"
        body += "try { Assert-FpsJsonNoRepeatedKey '{\"legs\":[null],\"proof\":[]}' 'plan.json' -ParseLevelOnly; Write-Host 'ASSERT_PLO=NONE' } catch { Write-Host 'ASSERT_PLO=THREW' }\n"
        body += "try { Assert-FpsJsonNoRepeatedKey '{\"proof\":[]}' 'receipt.json'; Write-Host 'ASSERT_RECEIPT=NONE' } catch { Write-Host ('ASSERT_RECEIPT=' + $_.Exception.Message.Split(':')[0]) }\n"
        out = dict(line.split("=", 1) for line in self.ok(body).split())
        want = {}
        for label in shape_inputs:
            want[f"{label}_PLO"] = "NONE"
            # -AllowArrayRoot lifts only the root rule: every shape input is still a problem for the strict post-item call and for the receipt default
            want[f"{label}_STRICT"] = "PROBLEM"
            want[f"{label}_DEFAULT"] = "PROBLEM"
        for label in parse_inputs:
            for mode in ("PLO", "STRICT", "DEFAULT"):
                want[f"{label}_{mode}"] = "PROBLEM"
        want.update({"ASSERT_PLO": "NONE", "ASSERT_RECEIPT": "FPS_JSON_REPEATED_KEY"})
        self.assertEqual(out, want)

    def test_envelope_preempt_residue_a_cached_analysis_basis_is_compared_ordinally(self) -> None:
        # ENVELOPE-PREEMPT-RESIDUE-1 r1 (adversary r1 A2, the basis half): PowerShell's -cne is culture-aware and ignores a soft hyphen (U+00AD), so a cached analysis naming
        # 'candidate<U+00AD>Speed' was bound to the candidateSpeed pair. The basis must be the same string ordinally; the exact name still binds.
        body = (
            "$cmp = [pscustomobject]@{ presentedFps = [pscustomobject]@{ deltaPercent = -20 } }\n"
            "foreach ($b in @('candidateSpeed', \"candidate$([char]0xAD)Speed\", 'candidatespeed')) {\n"
            "  $a = [pscustomobject]@{ comparisonBasis = $b; evidence = [pscustomobject]@{ presentedFpsDeltaPercent = -20 } }\n"
            "  Write-Host ('BOUND=' + (Test-FpsCachedAnalysisBound -Analysis $a -Compare $cmp -ComparisonBasis 'candidateSpeed'))\n"
            "}\n"
        )
        self.assertEqual(self.ok(body).split(), ["BOUND=True", "BOUND=False", "BOUND=False"])

    def test_envelope_preempt_residue_a_receipt_reader_still_refuses_an_envelope_array(self) -> None:
        # ENVELOPE-PREEMPT-RESIDUE-1 r1 KEEP item 3: only the lease batch's plan read defers the shape rules. A receipt reader (Read-SmokeJson, Read-JsonRecords) still refuses a record whose
        # `launch` envelope is an array, exactly as at 23b2d55d; the same record with an object envelope is read.
        launch = '{"environment":' + LIGHT_ENV_JSON + ',"arguments":' + PRODUCER_ARGS_JSON + '}'
        wrapped = self.tmp / "launch-array.json"
        wrapped.write_text('{"schema":"x","launch":[' + launch + ']}', encoding="utf-8")
        plain = self.tmp / "launch-object.json"
        plain.write_text('{"schema":"x","launch":' + launch + '}', encoding="utf-8")
        body = _extract_functions(COMPARE_SMOKE_AB_SCRIPT, ["Read-SmokeJson"]) + "\n" + _extract_functions(COMPARE_MACHINE_PERF_SCRIPT, ["Read-JsonRecords"]) + "\n"
        for label, call in (("SMOKE_ENVELOPE", f"Read-SmokeJson '{wrapped}'"), ("PERF_ENVELOPE", f"Read-JsonRecords '{wrapped}'"),
                            ("SMOKE_PLAIN", f"Read-SmokeJson '{plain}'"), ("PERF_PLAIN", f"Read-JsonRecords '{plain}'")):
            body += f"try {{ $null = @({call}); Write-Host '{label}=READ' }} catch {{ Write-Host ('{label}=REFUSED:' + [string]($_.Exception.Message -match 'is an array, but')) }}\n"
        out = self.ok(body).split()
        self.assertEqual(out, ["SMOKE_ENVELOPE=REFUSED:True", "PERF_ENVELOPE=REFUSED:True", "SMOKE_PLAIN=READ", "PERF_PLAIN=READ"])

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
                # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A3): a scopes leg emits a launch outside the LIGHT shape (--scope none only, no scope-interval or scopes-mode env)
                scope_on = (("scope" in args and args["scope"] != "none") or ("scopeIntervalMs" in args and args["scopeIntervalMs"] != -1)
                            or ("scopesDuringPlayback" in args and args["scopesDuringPlayback"] not in (None, "")))
                expected[path.name] = "UNKNOWN" if (arm == "LIGHT" and (observer_on or look_light or scope_on)) else arm
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
            f"[pscustomobject]@{{ {env}; arguments = @('--drop-frame-mode', 'sometimes') }}": "UNKNOWN",
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
        call = source.index("Add-InheritedMlvAppEnvironmentRecord -Target $launchEnv -Cleared (@($clearedEnvironment) + $childReplacedEnvironment)")
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
        # R4-DEADLINE-RESIDUE-1 r4 (hosted CI on 2ee7d054): r3 moved the guard. The same no-probe list still opens the one block that holds BOTH batch.log probes (the bounded worker under -DeadlineUtc, the
        # inline one without it); the pattern follows the list to that block, never to a particular probe's first line, so a probe moved out from under the list does not match.
        skip = re.search(r"\$Reason -cnotin @\(([^)]*)\)\) \{\s*\n\s*if \(\$DeadlineUtc\) \{.*?\$probe = Invoke-ProbeWithin \$script:LogProbeBlock .*?\} else \{\s*\n\s*try \{\s*\n\s*Wait-RunDirReadSeam", refused.group(0), re.DOTALL)
        self.assertIsNotNone(skip, "the no-probe list guards the batch.log probe")
        self.assertIn("'SPEC_READ_TIMEOUT'", skip.group(1))
        self.assertIn("'EXPORT_BUDGET_PREFLIGHT_TIMEOUT'", skip.group(1))
        # the sibling (adversary r14 pass 17 A4 / pass 18 A5): the plan file's own read timeout keeps its LEG_PLAN_UNREADABLE code but skips the probe the same way a location the guard gave up on does
        self.assertIn("if ($planRead.state -ceq 'TIMEOUT') { $script:RunOutputAliased = $true; Stop-PlanRefused 'LEG_PLAN_UNREADABLE' 0 ", source)
        self.assertIn("if (-not $script:RunOutputAliased -and $logRoomSec -ge 3.0 -and $Reason -cnotin", refused.group(0))   # r4: r3 added the time-left test before the list

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
            # (merge with #903: the judged digests are keyed by Get-SpecDigestKey; this fixture's key is the spelled path, which the stubbed reads use throughout)
            "function Get-SpecDigestKey([string]$Path) { $Path }\n"
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

    def test_r3_residue_a11_a_cached_label_is_published_only_when_it_is_the_producers_own_for_the_evidence(self) -> None:
        # BENCH-LIGHT-ARM-GAIN-GATES-1-R3-MERGE-RESIDUE-1 (sol 07d50621 A11, on 2ed556bb): Test-FpsCachedAnalysisBound bound the cached analysis to the judged compare by its INPUTS only
        # (basis, fps delta, and every stage field equal or both absent). Fps +10 % with every stage field absent matched a cached queue-wait-bound, so the readers repeated advice the
        # producer would never give for that pair (it says none / validate_same_clip_on_second_machine). The label must be the one New-PlaybackAbAnalysis itself gives. Here the producer's
        # own function runs over one compare per branch of its ladder: its own output is bound, the same output with another label is not, and a label that is not a string is not.
        def metric(delta: str, candidate: str = "$null") -> str:
            return f"[pscustomobject]@{{ deltaPercent = {delta}; candidate = {candidate} }}"

        def compare(**fields: str) -> str:
            return "[pscustomobject]@{ " + "; ".join(f"{name} = {value}" for name, value in fields.items()) + " }"

        cases = {
            "NONE": compare(presentedFps=metric("10")),
            "ABS_QUEUE": compare(presentedFps=metric("12"), avgQueueWaitMs=metric("$null", "20")),
            "ABS_DRAW": compare(presentedFps=metric("12"), avgDrawTotalMs=metric("$null", "20")),
            "ABS_TEXTURE": compare(presentedFps=metric("12"), avgTextureTotalMs=metric("$null", "12")),
            "MIXED": compare(presentedFps=metric("5")),
            "PRESENT_SYNC": compare(presentedFps=metric("-2"), avgQueueWaitMs=metric("30"), avgDrawTotalMs=metric("12")),
            "QUEUE": compare(presentedFps=metric("-2"), avgQueueWaitMs=metric("30")),
            "PACING": compare(presentedFps=metric("-2"), avgPresentPacingMs=metric("30")),
            "JITTER": compare(presentedFps=metric("-2"), avgDrawTotalMs=metric("16")),
            "RECON": compare(presentedFps=metric("-2"), avgLlrawprocMs=metric("12")),
            "CADENCE": compare(presentedFps=metric("-2")),
        }
        body = ""
        for label, cmp in cases.items():
            body += (f"$cmp = {cmp}\n$a = New-PlaybackAbAnalysis -Compare $cmp -ProofFailures @() -ComparisonBasis 'candidateSpeed'\n"
                     f"Write-Host ('{label}=' + $a.dominantBottleneck + ',' + (Test-FpsCachedAnalysisBound -Analysis $a -Compare $cmp -ComparisonBasis 'candidateSpeed'))\n")
        # the attack of the finding: fps +10 % and nothing else, with queue-wait advice (the producer's own text for a queue-wait regression) cached beside it
        body += (f"$cmp = {cases['NONE']}\n$a = New-PlaybackAbAnalysis -Compare $cmp -ProofFailures @() -ComparisonBasis 'candidateSpeed'\n"
                 "$a.dominantBottleneck = 'queue-wait-bound'; $a.suggestedOptimization = 'reduce_render_queue_wait_or_present_signal_latency'\n"
                 "Write-Host ('FORGED_BOTH=' + (Test-FpsCachedAnalysisBound -Analysis $a -Compare $cmp -ComparisonBasis 'candidateSpeed'))\n"
                 "$a = New-PlaybackAbAnalysis -Compare $cmp -ProofFailures @() -ComparisonBasis 'candidateSpeed'\n"
                 "$a.suggestedOptimization = 'reduce_render_queue_wait_or_present_signal_latency'\n"
                 "Write-Host ('FORGED_SUGGESTION=' + (Test-FpsCachedAnalysisBound -Analysis $a -Compare $cmp -ComparisonBasis 'candidateSpeed'))\n"
                 "$a = New-PlaybackAbAnalysis -Compare $cmp -ProofFailures @() -ComparisonBasis 'candidateSpeed'\n"
                 "$a.dominantBottleneck = @('none')\n"
                 "Write-Host ('NOT_A_STRING=' + (Test-FpsCachedAnalysisBound -Analysis $a -Compare $cmp -ComparisonBasis 'candidateSpeed'))\n")
        out = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(out, {"NONE": "none,True", "ABS_QUEUE": "queue-wait-bound,True", "ABS_DRAW": "present-bound,True", "ABS_TEXTURE": "gpu-texture-bound,True", "MIXED": "mixed,True",
                               "PRESENT_SYNC": "present-bound,True", "QUEUE": "queue-wait-bound,True", "PACING": "frame-pacing-bound,True", "JITTER": "present-bound,True",
                               "RECON": "recon-bound,True", "CADENCE": "cadence-bound,True", "FORGED_BOTH": "False", "FORGED_SUGGESTION": "False", "NOT_A_STRING": "False"})
        # every reader that repeats a cached label goes through that one function (the three sites the finding names)
        for script, call in (("compare-machine-perf.ps1", "Test-FpsCachedAnalysisBound -Analysis $playbackAb.analysis -Compare $playbackAbCompare"),
                             ("compare-machine-perf.ps1", "Test-FpsCachedAnalysisBound -Analysis $Record.analysis -Compare $cachedCompare"),
                             ("summarize-local-cuda-proof.ps1", "Test-FpsCachedAnalysisBound -Analysis $playbackAb.analysis -Compare $playbackAbSpeedCompare")):
            self.assertIn(call, (PROFILING / script).read_text(encoding="utf-8"), script)

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
    functions = ["Convert-ToNullableDouble", "Get-PlaybackAbFpsVerdict", "Get-SmokeSummaryTelemetryArm", "Get-HostLoadProofFailures"]

    def run_block(self, baseline: str, candidate: str, fps_b: float, fps_c: float) -> list[str]:
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        block = _extract_block(source, "if ($RequireCandidateImprovesPresentedFps) {", "candidate-presented-fps-not-improved baseline=$baselineFps candidate=$candidateFps", trailing_lines=3)
        out = self.ok(
            f"$baselineSummary = [pscustomobject]@{{ telemetryArm = {baseline}; {LIGHT_EVIDENCE}presentedFps = {fps_b}; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}\n"
            f"$candidateSummary = [pscustomobject]@{{ telemetryArm = {candidate}; {LIGHT_EVIDENCE}presentedFps = {fps_c}; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}\n"
            "$candidateSpeedSummary = $null\n$SeparateCandidateSpeedRun = $false\n$RequireCandidateImprovesPresentedFps = $true\n$proofFailures = @()\n"
            "$fpsVerdict = Get-PlaybackAbFpsVerdict -BaselineSummary $baselineSummary -CandidateSummary $candidateSummary -CandidateSpeedSummary $candidateSpeedSummary\n"
            + block + "\nforeach ($f in $proofFailures) { Write-Host ('FAIL=' + $f) }\nWrite-Host ('COUNT=' + @($proofFailures).Count)\n")
        return out.splitlines()

    def run_separate(self, parity: str, speed: str, fps_speed: float) -> str:
        source = CUDA_AB_SCRIPT.read_text(encoding="utf-8")
        block = _extract_block(source, "if ($RequireCandidateImprovesPresentedFps) {", "candidate-presented-fps-not-improved baseline=$baselineFps candidate=$candidateFps", trailing_lines=3)
        return self.ok(
            f"$baselineSummary = [pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}presentedFps = 10.0; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}\n"
            f"$candidateSummary = [pscustomobject]@{{ telemetryArm = '{parity}'; {LIGHT_EVIDENCE}presentedFps = 7.0; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}\n"
            f"$candidateSpeedSummary = [pscustomobject]@{{ telemetryArm = '{speed}'; {LIGHT_EVIDENCE}presentedFps = {fps_speed}; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}\n"
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
            "    [pscustomobject]@{ telemetryArm = (Get-SmokeSummaryTelemetryArm -Json $json); launch = $json.launch; presentedFps = $Fps; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }\n"
            "}\n"
            "$baselineSummary = New-WrapperLeg $baselineEnv 10.0\n$candidateSummary = New-WrapperLeg $candidateSpeedEnv 20.0\n$candidateSpeedSummary = $null\n"
            "Write-Host ('ARM=' + $baselineSummary.telemetryArm.arm + ',' + $candidateSummary.telemetryArm.arm)\n"
            "$SeparateCandidateSpeedRun = $false\n$RequireCandidateImprovesPresentedFps = $true\n$proofFailures = @()\n"
            "$fpsVerdict = Get-PlaybackAbFpsVerdict -BaselineSummary $baselineSummary -CandidateSummary $candidateSummary -CandidateSpeedSummary $candidateSpeedSummary\n"
            "Write-Host ('ALLOWED=' + $fpsVerdict.allowed + ' REASON=' + $fpsVerdict.reason)\n"
            + block + "\nforeach ($f in $proofFailures) { Write-Host ('FAIL=' + $f) }\nWrite-Host ('COUNT=' + @($proofFailures).Count)\n")
        # r8: the baseline leg (telemetry on, no per-frame opt-out) is HEAVY; the speed leg also sets MLVAPP_GPU_PLAYBACK_RECON_VALIDATE_OUTPUT=0, a key outside the LIGHT shape, so it read UNKNOWN there.
        # R2-RESIDUE-1 r3 (sol 5add23cf A4): the HEAVY recognizer (frame telemetry on, per-frame log not opted out) runs before the LIGHT table refuses a name, so that leg is HEAVY too, as it always was.
        # The pair is refused with the typed HEAVY reason either way.
        self.assertIn("ARM=HEAVY,HEAVY", out, "the wrapper's own legs are never LIGHT (this is the open successor BENCH-WRAPPER-LIGHT-ENV-1)")
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
        def leg(arm: str | None, fps: float) -> str:
            arm_part = _light_word(arm)
            return f"[pscustomobject]@{{ {arm_part}presentedFps = {fps}; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}"
        verdict = "fpsVerdict = [pscustomobject]@{ allowed = $true; reason = '' }; " if with_verdict else ""
        return ("[pscustomobject]@{ schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; " + verdict +
                "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
                f"baseline = {leg(baseline_arm, 10.0)}; candidate = {leg(candidate_arm, 20.0)}; candidateSpeed = $null; "
                # r15 (adversary r14 pass 18 A7): the cached analysis is trusted only when bound to the judged pair (its basis and presented-fps deltaPercent), as the producer writes it
                # R3-MERGE-RESIDUE-1 (sol 07d50621 A11): and its label is the producer's own for that evidence (fps +100 %, no stage field: none / validate_same_clip_on_second_machine)
                "analysis = [pscustomobject]@{ comparisonBasis = 'candidate'; dominantBottleneck = 'none'; suggestedOptimization = 'validate_same_clip_on_second_machine'; "
                "evidence = [pscustomobject]@{ presentedFpsDeltaPercent = 100.0 } }; "
                "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 10.0; candidate = 20.0; deltaPercent = 100.0 } } }")

    def row(self, record: str) -> dict:
        out = self.ok(f"$record = {record}\n$r = New-PlaybackAbSummaryRow -Record $record -Source 'test'\n$r | ConvertTo-Json -Compress")
        return json.loads(out.strip().splitlines()[-1])

    def test_a_light_record_publishes_the_delta(self) -> None:
        r = self.row(self.record("LIGHT", "LIGHT"))
        self.assertEqual(r["playback_fps_delta_pct"], 100.0)
        self.assertEqual(r["dominant_bottleneck"], "none")
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
        leg = f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}"
        heavy = "[pscustomobject]@{ telemetryArm = 'HEAVY'; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }"
        fingerprint = "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
        compare = "[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 10.0; candidate = 20.0; deltaPercent = 100.0 } }"
        # R2-RESIDUE-1 r1 (adversary 698fffb1 A7): the compare (10 -> 20 fps) is measured on the legs it names, so each leg records the presentedFps it carried into the compare
        leg10 = leg.replace("hostLoadProvisional", "presentedFps = 10.0; hostLoadProvisional", 1)
        leg20 = leg.replace("hostLoadProvisional", "presentedFps = 20.0; hostLoadProvisional", 1)
        base = f"schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; {fingerprint}baseline = {leg10}; candidate = {leg20}; compare = {compare}; speedCompare = {compare}; "
        absent = self.row(f"[pscustomobject]@{{ {base}candidateSpeed = $null }}")
        self.assertIsNone(absent["playback_fps_delta_pct"])
        self.assertEqual(absent["playback_fps_arm_verdict"], UNKNOWN_REASON)
        heavy_speed = self.row(f"[pscustomobject]@{{ {base}candidateSpeed = {heavy} }}")
        self.assertIsNone(heavy_speed["playback_fps_delta_pct"])
        self.assertEqual(heavy_speed["playback_fps_arm_verdict"], HEAVY_REASON)
        light_speed = self.row(f"[pscustomobject]@{{ {base}candidateSpeed = {leg20} }}")
        self.assertEqual(light_speed["playback_fps_delta_pct"], 100.0)

    def test_a_saved_speed_leg_held_in_a_one_element_array_is_unknown_through_the_pair_selection(self) -> None:
        # BENCH-LIGHT-ARM-GAIN-GATES-1 r6 (adversary pass 5 A3): `$candidateLeg = if (...) { $Record.candidateSpeed }` unrolled a one-element array into the leg inside it, so a malformed
        # container the direct reader rejects was judged as that LIGHT leg and the machine row published the speed delta. Both leg choices keep the container whole.
        light = f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}"
        heavy = "[pscustomobject]@{ telemetryArm = 'HEAVY'; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }"
        fingerprint = "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
        compare = "[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 20.0; candidate = 30.0; delta = 10.0; deltaPercent = 50.0 } }"
        # R2-RESIDUE-1 r1 (adversary 698fffb1 A7): the compare (20 -> 30 fps) is measured on the legs it names
        light20 = light.replace("hostLoadProvisional", "presentedFps = 20.0; hostLoadProvisional", 1)
        light = light.replace("hostLoadProvisional", "presentedFps = 30.0; hostLoadProvisional", 1)
        base = f"schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; {fingerprint}baseline = {light20}; candidate = {heavy}; compare = {compare}; speedCompare = {compare}; "
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
                  '"baseline":{"telemetryArm":["LIGHT"],"hostLoadProvisional":false,"hostLoadState":"quiet","hostLoadCollected":true},'
                  '"candidate":{"telemetryArm":["LIGHT"],"hostLoadProvisional":false,"hostLoadState":"quiet","hostLoadCollected":true},'
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
        def bare(fps: float) -> str:
            return f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; presentedFps = {fps}; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}"
        record = self.record("LIGHT", "LIGHT").replace(f"baseline = {self.record_leg('LIGHT', 10.0)}; candidate = {self.record_leg('LIGHT', 20.0)};", f"baseline = {bare(10.0)}; candidate = {bare(20.0)};")
        self.assertIn(f"baseline = {bare(10.0)}", record, "the fixture replacement must land")
        r = self.row(record)
        self.assertIsNone(r["playback_fps_delta_pct"])
        self.assertEqual(r["playback_fps_arm_verdict"], UNKNOWN_REASON)
        self.assertEqual(r["dominant_bottleneck"], "fps-arm-refused")
        corroborated = self.row(self.record("LIGHT", "LIGHT"))
        self.assertEqual(corroborated["playback_fps_arm_verdict"], "allowed")

    @staticmethod
    def record_leg(arm: str | None, fps: float) -> str:
        return f"[pscustomobject]@{{ {_light_word(arm)}presentedFps = {fps}; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}"

    def test_a_record_that_names_no_arm_is_unknown_and_refused_even_if_it_carries_a_cached_allowed_verdict(self) -> None:
        for with_verdict in (False, True):
            with self.subTest(cached_verdict=with_verdict):
                r = self.row(self.record(None, None, with_verdict=with_verdict))
                self.assertIsNone(r["playback_fps_delta_pct"])
                self.assertEqual(r["playback_fps_arm_verdict"], UNKNOWN_REASON)

    @staticmethod
    def a7_record(analysis_basis: str, analysis_delta: str, legacy: bool = False) -> str:
        # adversary r14 pass 18 A7: quiet baseline LIGHT at 50, candidate HEAVY at 40, candidateSpeed LIGHT at 55; compare deltaPercent -20 (the HEAVY pair), speedCompare deltaPercent 10
        light = f"[pscustomobject]@{{ telemetryArm = 'LIGHT'; {LIGHT_EVIDENCE}hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}"
        heavy = "[pscustomobject]@{ telemetryArm = 'HEAVY'; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }"
        fingerprint = "machineFingerprint = [pscustomobject]@{ schema = 'machine-fingerprint.v1'; hostname = 'H'; cpu = 'x'; gpu = 'y'; os = 'z'; build_sha = 'abc1234' }; "
        # R3-MERGE-RESIDUE-1 (sol 07d50621 A11): the speed pair's OWN analysis (basis candidateSpeed, +10 %, no stage field) carries the producer's label for it (none / validate_same_clip_on_second_machine);
        # every other analysis here belongs to another pair or to none, and carries a label that is not the speed pair's ('render-work' is nobody's)
        own = analysis_basis == "candidateSpeed" and analysis_delta == "10.0"
        label = "dominantBottleneck = 'none'; suggestedOptimization = 'validate_same_clip_on_second_machine'" if own else "dominantBottleneck = 'render-work'; suggestedOptimization = 'optimize_playback_cuda_candidate'"
        analysis = (f"[pscustomobject]@{{ {label} }}" if legacy else
                    f"[pscustomobject]@{{ comparisonBasis = '{analysis_basis}'; {label}; "
                    f"evidence = [pscustomobject]@{{ presentedFpsDeltaPercent = {analysis_delta} }} }}")
        # R2-RESIDUE-1 r1 (adversary 698fffb1 A7): each leg records the presentedFps its compare was measured from (baseline 50, HEAVY candidate 40, LIGHT candidateSpeed 55)
        baseline_leg = light.replace("hostLoadProvisional", "presentedFps = 50.0; hostLoadProvisional", 1)
        speed_leg = light.replace("hostLoadProvisional", "presentedFps = 55.0; hostLoadProvisional", 1)
        heavy = heavy.replace("hostLoadProvisional", "presentedFps = 40.0; hostLoadProvisional", 1)
        return (f"[pscustomobject]@{{ schema = 'mlvapp-cuda-playback-ab.v1'; status = 'success'; {fingerprint}baseline = {baseline_leg}; candidate = {heavy}; candidateSpeed = {speed_leg}; "
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
        self.assertEqual((bound["dominant_bottleneck"], bound["suggested_optimization"]), ("none", "validate_same_clip_on_second_machine"))


@requires_pwsh
class CompareMachinePerfSuggestionFpsGateTests(_Probe):
    script = COMPARE_MACHINE_PERF_SCRIPT
    functions = ["Convert-ToNullableInt64", "Convert-ToNullableDouble", "Get-PlaybackAbLegHostLoadProvisional", "Get-PlaybackAbHostLoadRefusal", "Get-PlaybackAbFpsArmRefusal",
                 "Get-ProofSummarySuggestion"]

    def suggestion(self, arm: str | None) -> str:
        arm_part = _light_word(arm)
        # R2-RESIDUE-1 r1 (adversary 698fffb1 A7): the compare (10 -> 7 fps) is measured on the legs it names
        leg = f"[pscustomobject]@{{ {arm_part}presentedFps = {{fps}}; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}"
        return self.ok(
            "$rec = [pscustomobject]@{ proof = [pscustomobject]@{ playback = [pscustomobject]@{ totalFallbackFrames = 0; totalGpuTextureNoReadbackFrames = 5 }; "
            f"playbackAb = [pscustomobject]@{{ baseline = {leg.replace('{fps}', '10.0')}; candidate = {leg.replace('{fps}', '7.0')}; candidateSpeed = $null; "
            "compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 10.0; candidate = 7.0; delta = -3.0 } }; analysis = $null }; cdng = $null } }\n"
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
        self.assertEqual(suggestion(record("candidateSpeed", "10.0")), "S=validate_same_clip_on_second_machine", "a bound cached suggestion is still repeated")


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

    def run_summarizer_block(self, baseline_arm: str, candidate_arm: str, analysis: str = "$null", candidate_extra: str = "", bare_candidate: bool = False, stale_failures: bool = False,
                             candidate_fps: str = "49", baseline_fps: str = "50", presented_compare: str = "baseline = 50; candidate = 49; delta = -1; deltaPercent = -2",
                             leg_metrics: str | None = None,
                             compare_metrics: str = ("guiStatusFps = [pscustomobject]@{ baseline = 50; candidate = 49; delta = -1; deltaPercent = -2 }; "
                                                     "avgPresentIntervalMs = [pscustomobject]@{ baseline = 20; candidate = 21; delta = 1; deltaPercent = 5 }; "
                                                     "avgRenderTotalMs = [pscustomobject]@{ baseline = 10; candidate = 11; delta = 1; deltaPercent = 10 }")) -> list[str]:
        # R2-RESIDUE-1 r3 (sol 5add23cf A10): the compare binds EVERY metric it publishes, so each judged leg records the other figures the compare names (GUI status fps, present interval, render total)
        base_metrics = "guiStatusFps = 50; avgPresentIntervalMs = 20; avgRenderTotalMs = 10; " if leg_metrics is None else leg_metrics
        cand_metrics = "guiStatusFps = 49; avgPresentIntervalMs = 21; avgRenderTotalMs = 11; " if leg_metrics is None else leg_metrics
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
            # R2-RESIDUE-1 r1 (adversary 698fffb1 A7): each leg records the presented fps the compare below was measured from (50 and 49), so the compare is bound to the pair the gate judged
            f"$playbackAb = [pscustomobject]@{{ analysis = {analysis}; baseline = [pscustomobject]@{{ {_light_word(baseline_arm)}presentedFps = {baseline_fps}; {base_metrics}hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }}; "
            f"candidate = [pscustomobject]@{{ {candidate_word}{candidate_extra}presentedFps = {candidate_fps}; {cand_metrics}hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true }} }}\n"
            f"$playbackAbSpeedCompare = [pscustomobject]@{{ presentedFps = [pscustomobject]@{{ {presented_compare} }}; {compare_metrics} }}\n"
            "$playbackAbCandidateLeg = $playbackAb.candidate\n$playbackAbHostLoadProvisional = $false\n"
            "$playbackAbBlockers = [System.Collections.Generic.List[string]]::new()\n$diagnostics = [System.Collections.Generic.List[object]]::new()\n"
            + stale_injection
            + "if ($true) {\n" + block + "\n" + evidence +
            "\nforeach ($d in $diagnostics) { Write-Host ('DIAG=' + $d.code) }\nforeach ($e in $playbackSpeedEvidence) { Write-Host ('EV=' + $e) }\n"
            "Write-Host ('BLOCKERS=' + ($playbackAbBlockers -join ' / '))\n", encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(probe)], capture_output=True, text=True, creationflags=_NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout.splitlines()

    def test_r2_adversary_698fffb1_a7_the_speed_compare_must_be_measured_on_the_judged_pair(self) -> None:
        # adversary 698fffb1 A7: a LIGHT candidateSpeed leg at 40 fps was judged while the compare carried the ordinary HEAVY candidate's 60 fps (+20 %), so the report quoted a gain the judged pair did not show
        text = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", candidate_fps="40"))
        self.assertIn("DIAG=PLAYBACK_AB_FPS_ARM_REFUSED", text)
        self.assertIn("fps-verdict-refused:compare-not-judged-pair", text)
        self.assertNotIn("presented FPS: CPU", text)
        self.assertNotIn("PLAYBACK_PRESENTED_FPS_NOT_IMPROVED", text)
        control = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", candidate_fps="49"))
        self.assertNotIn("compare-not-judged-pair", control)
        self.assertIn("presented FPS: CPU", control, "control: the compare was measured on the judged pair")

    def test_r2_residue_r2_luna_r1b_a1_the_published_delta_must_follow_from_the_bound_endpoints(self) -> None:
        # luna r1b A1 (input verbatim): both legs presentedFps=40, compare {baseline:40, candidate:40, delta:40, deltaPercent:100}. The endpoints bind, so da6248b1 published a +100 % gain no leg measured.
        # (label, the leg's own baseline fps, its candidate fps, the compare); the second and third entries of the last group are sol c29f8dc1 A1 / A2 verbatim
        for label, leg_base, leg_cand, compare in (
                ("adversary", "40", "40", "baseline = 40; candidate = 40; delta = 40; deltaPercent = 100"),
                ("percent-only", "40", "40", "baseline = 40; candidate = 40; delta = 0; deltaPercent = 100"),
                ("delta-only", "40", "40", "baseline = 40; candidate = 40; delta = 40; deltaPercent = 0"),
                ("delta-not-finite", "40", "40", "baseline = 40; candidate = 40; delta = [double]::NaN; deltaPercent = 0"),
                ("zero-baseline", "0", "0", "baseline = 0; candidate = 0; delta = 0; deltaPercent = 0"),
                ("sol-a1-legs-at-zero-compare-at-1e-7", "0", "0", "baseline = 0.0000001; candidate = 0.0000002; delta = 0.0000001; deltaPercent = 100"),
                ("sol-a2-tie-with-a-one-micro-delta", "40", "40", "baseline = 40; candidate = 40; delta = -0.000001; deltaPercent = 0"),
                ("sol-a2-percent-off-by-the-rounding-step", "40", "39.59976", "baseline = 40; candidate = 39.59976; delta = -0.40024; deltaPercent = -1"),
                ("sol2-a1-percent-off-by-the-rounding-step", "24", "23.759879", "baseline = 24; candidate = 23.759879; delta = -0.240121; deltaPercent = -1"),
                ("sol2-a2-endpoints-near-the-legs", "50", "50", "baseline = 49.999975; candidate = 50.000025; delta = 0.00005; deltaPercent = 0")):
            text = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", candidate_fps=leg_cand, baseline_fps=leg_base, presented_compare=compare))
            self.assertIn("fps-verdict-refused:compare-not-judged-pair", text, label)
            self.assertNotIn("presented FPS: CPU", text, label)
        # control: the producer's own rounding (delta to 6 places, deltaPercent to 3) is not a refusal
        control = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", candidate_fps="47.123457", baseline_fps="40",
                                                      presented_compare="baseline = 40; candidate = 47.123457; delta = 7.123457; deltaPercent = 17.809"))
        self.assertNotIn("compare-not-judged-pair", control)
        self.assertIn("presented FPS: CPU", control)

    def test_r3_sol_5add23cf_a10_the_summarizer_publishes_no_unbound_gui_or_interval_figure(self) -> None:
        # sol 5add23cf A10 (input verbatim): both judged legs record presentedFps 40, guiStatusFps 40, avgPresentIntervalMs 25; the compare binds presentedFps (40 / 40) but publishes a +100 % GUI figure and
        # a -50 % interval figure no judged leg measured. The whole compare is refused with the typed reason; the lines for those figures are not printed.
        text = "\n".join(self.run_summarizer_block(
            "LIGHT", "LIGHT", candidate_fps="40", baseline_fps="40", presented_compare="baseline = 40; candidate = 40; delta = 0; deltaPercent = 0",
            leg_metrics="guiStatusFps = 40; avgPresentIntervalMs = 25; avgRenderTotalMs = 10; ",
            compare_metrics=("guiStatusFps = [pscustomobject]@{ baseline = 10; candidate = 20; delta = 10; deltaPercent = 100 }; "
                             "avgPresentIntervalMs = [pscustomobject]@{ baseline = 50; candidate = 25; delta = -25; deltaPercent = -50 }; "
                             "avgRenderTotalMs = [pscustomobject]@{ baseline = 10; candidate = 10; delta = 0; deltaPercent = 0 }")))
        self.assertIn("fps-verdict-refused:compare-not-judged-pair", text)
        self.assertNotIn("GUI status FPS: CPU", text)
        self.assertNotIn("avg present interval: CPU", text)
        control = "\n".join(self.run_summarizer_block(
            "LIGHT", "LIGHT", candidate_fps="40", baseline_fps="40", presented_compare="baseline = 40; candidate = 40; delta = 0; deltaPercent = 0",
            leg_metrics="guiStatusFps = 40; avgPresentIntervalMs = 25; avgRenderTotalMs = 10; ",
            compare_metrics=("guiStatusFps = [pscustomobject]@{ baseline = 40; candidate = 40; delta = 0; deltaPercent = 0 }; "
                             "avgPresentIntervalMs = [pscustomobject]@{ baseline = 25; candidate = 25; delta = 0; deltaPercent = 0 }; "
                             "avgRenderTotalMs = [pscustomobject]@{ baseline = 10; candidate = 10; delta = 0; deltaPercent = 0 }")))
        self.assertNotIn("compare-not-judged-pair", control)
        self.assertIn("GUI status FPS: CPU", control)

    def test_r5_adversary_82d6f070_a1_a_heavy_pair_beside_an_unbound_compare_publishes_none_of_its_figures(self) -> None:
        # adversary 82d6f070 A1 (input verbatim): HEAVY baseline and candidate each record presentedFps 40 and avgRenderTotalMs 10; the compare publishes presentedFps 20 -> 60 (+200 %) and avgRenderTotalMs
        # 50 -> 25 (-50 %). The binding ran only for a pair the arm gate allowed, so the arm refusal kept the stage lines and the summarizer printed the false -50 % render change.
        text = "\n".join(self.run_summarizer_block(
            "HEAVY", "HEAVY", candidate_fps="40", baseline_fps="40", presented_compare="baseline = 20; candidate = 60; delta = 40; deltaPercent = 200",
            leg_metrics="guiStatusFps = 40; avgPresentIntervalMs = 25; avgRenderTotalMs = 10; ",
            compare_metrics=("guiStatusFps = [pscustomobject]@{ baseline = 40; candidate = 40; delta = 0; deltaPercent = 0 }; "
                             "avgPresentIntervalMs = [pscustomobject]@{ baseline = 25; candidate = 25; delta = 0; deltaPercent = 0 }; "
                             "avgRenderTotalMs = [pscustomobject]@{ baseline = 50; candidate = 25; delta = -25; deltaPercent = -50 }")))
        self.assertIn(HEAVY_REASON, text, "the pair keeps its arm reason")
        self.assertIn("speed compare: withheld", text)
        self.assertIn("not measured on the judged pair either", text)
        for figure in ("render total: CPU", "CPU 50 ms", "-50%", "presented FPS: CPU", "GUI status FPS: CPU", "avg present interval: CPU"):
            self.assertNotIn(figure, text)
        # control: the same HEAVY pair beside a compare measured on its own legs keeps the arm reason and its stage lines
        control = "\n".join(self.run_summarizer_block("HEAVY", "HEAVY"))
        self.assertNotIn("compare-not-judged-pair", control)
        self.assertIn(HEAVY_REASON, control)
        self.assertIn("render total: CPU", control)

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
        # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A4): a bound analysis records every compare input its diagnosis read, so the fixture carries the present-interval pair too
        # R3-MERGE-RESIDUE-1 (sol 07d50621 A11): and the suggestion is the producer's own for that evidence (fps -2 %, present interval +5 %: a cadence regression with no stage over its threshold)
        analysis = ("[pscustomobject]@{ comparisonBasis = 'candidate'; suggestedOptimization = 'profile_playback_cadence_with_stage_timing'; evidence = [pscustomobject]@{ presentedFpsDeltaPercent = -2; "
                    "presentIntervalDeltaPercent = 5; candidatePresentIntervalMs = 21 } }")
        refused = "\n".join(self.run_summarizer_block("HEAVY", "HEAVY", analysis))
        self.assertNotIn("suggested_optimization=", refused)
        allowed = "\n".join(self.run_summarizer_block("LIGHT", "LIGHT", analysis))
        self.assertIn("suggested_optimization=profile_playback_cadence_with_stage_timing", allowed)

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
        # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A5): every row passes the compare the section judged; a measured presented-fps pair is part of the speed gate now
        measured = "([pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 50; candidate = 55; delta = 5; deltaPercent = 10 } })"
        for label, overall, section in (("REFUSED", "QUOTABLE_PASS", "REFUSED"), ("MISSING", "QUOTABLE_PASS", None), ("PASS", "QUOTABLE_PASS", "PASS"), ("NOTQ", "NOT_QUOTABLE", "PASS")):
            verdict = "$null" if section is None else f"(New-SectionVerdict -Status '{section}' -Passed $true -Evidence @() -Blockers @())"
            body += (f"$a = Get-ProofSpeedClaimAuthority -OverallStatus '{overall}' -PlaybackSpeedVerdict {verdict} -SpeedCompare {measured}\n"
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
                # ENVELOPE-PREEMPT-R1-RESIDUE-1 r1 (adversary 835a5faf A6): both read the one module predicate over the parsed report AND its raw text
                # (adversary 1ce472bc A8: the importer also binds it to a verified proof-report.json hash)
                predicate = "Test-FpsProofReportSpeedQuotable -Report $proofReport -RawText $proofReportRaw)"
                self.assertIn(("speedQuotable = ($speedHashBound -and (" if name.startswith("import") else "speedQuotable = (") + predicate, text)
                self.assertIn('Import-Module (Join-Path $PSScriptRoot "FpsArmGate.psm1") -Force', text)
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
        # R2-RESIDUE-1 r1 (adversary 698fffb1 A5, 1ce472bc A6): the isolation arms the venue leg forwards from a spec the SPEC reader reads as LIGHT (prefetch decoders, pointwise kill switch, conflated move,
        # deferred release, due wake, OMP teams, render lookahead, safe start) moved into the table, so a spec and its own launch agree; what stays here are the observers, the fault injection and the scope cadence.
        "MLVAPP_GPU_FAULT_INJECT",
        "MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK", "MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS",
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
class CudaAbReadSmokeSummaryRepeatedMemberTests(_Probe):
    """R4-DEADLINE-RESIDUE-1 r7 (the master merge, run-release-cuda-playback-ab.ps1): a repeated member in a smoke result is a PROVISIONAL, arm-UNKNOWN leg, never a throw and never the last value."""

    script = CUDA_AB_SCRIPT
    functions = ["Convert-PlaybackLogLineToObject", "Get-ScopedPlaybackLogLines", "Add-NullableDouble", "Get-AverageOrNull", "Convert-ToNullableDouble",
                 "Convert-ToNullableInt64", "Get-NestedValue", "Get-SmokeSummaryHostLoadFields", "Get-SmokeSummaryTelemetryArm", "Read-SmokeSummary"]

    QUIET = '"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}'

    def _read(self, text: str) -> str:
        smoke = self.tmp / "smoke.json"
        smoke.write_text(text, encoding="utf-8", newline="")
        return self.ok(
            f"$s = Read-SmokeSummary -Path '{smoke}' -ExitCode 0\n"
            "Write-Host \"LEG=$($s.hostLoadProvisional)|$($s.hostLoadState)|$($s.hostLoadCollected)|$($s.telemetryArm.arm)\"\n").strip()

    def test_a_repeated_member_reads_as_a_provisional_arm_unknown_leg_not_a_throw(self) -> None:
        launch = '"launch":{"environment":' + LIGHT_ENV_JSON + ',"arguments":' + PRODUCER_ARGS_JSON + '}'
        # control: the one-member form is a clean LIGHT leg, so the refusals below are the repeat's doing
        self.assertEqual(self._read('{' + self.QUIET + ',' + launch + '}'), "LEG=False|quiet|True|LIGHT")
        # a repeated member in the recorded launch environment (a HEAVY opt-out written 0 then 1: the last value reads LIGHT); master's reader scans only the root and hostLoad
        nested_env = LIGHT_ENV_JSON.replace('"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG":"1"',
                                            '"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG":"0","MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG":"1"')
        self.assertNotEqual(nested_env, LIGHT_ENV_JSON)
        nested = '{' + self.QUIET + ',"launch":{"environment":' + nested_env + ',"arguments":' + PRODUCER_ARGS_JSON + '}}'
        self.assertEqual(self._read(nested), "LEG=True|unknown|False|UNKNOWN")
        # a repeated root member (master's own refusal) reads the same way
        self.assertEqual(self._read('{' + self.QUIET + ',' + launch + ',"launch":{}}'), "LEG=True|unknown|False|UNKNOWN")


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
        # R2-RESIDUE-1 r1 (adversary 698fffb1 A5): --windowed is the app's own flag, passed by the venue job through the runner's -AdditionalArgs (job:6330, the literal sits inside a here-string with doubled quotes)
        job_literals = (PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8").replace("''", "'")
        missing = [row["name"] for row in shape["arguments"] if f'"{row["name"]}' not in runner and f"'{row['name']}'" not in job_literals]
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
            "run-release-cuda-playback-ab.ps1": ["Assert-FpsJsonNoRepeatedKey $smokeText $Path"],
            "compare-machine-perf.ps1": ["Assert-FpsJsonNoRepeatedKey $raw $resolved", 'Assert-FpsJsonNoRepeatedKey $line "${resolved}:$lineNumber"'],
            "summarize-local-cuda-proof.ps1": ["Assert-FpsJsonNoRepeatedKey $summaryText $summaryFullPath"],
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


@requires_pwsh
class EnvelopePreemptR1ResidueTests(_Probe):
    """ENVELOPE-PREEMPT-R1-RESIDUE-1 r1: the close-or-narrow round on #871 over the introduced majors the adversary passes on 8747fced and 835a5faf left open."""

    def test_p3_a2_an_arm_object_outside_its_closed_shape_is_not_light(self) -> None:
        # adversary 835a5faf A2: Get-FpsArmFromLegRecord returned an arm OBJECT unchecked, so {arm:'LIGHT', observerControls:{utilSample:true}} beside LIGHT generatorArgs read LIGHT and the pair was
        # allowed; Get-FpsArmFromSmokeRecord read the same object beside a LIGHT launch as LIGHT. An arm object is {arm, source, reason}; any other key is outside the shape.
        bad = "[pscustomobject]@{ arm = 'LIGHT'; observerControls = [pscustomobject]@{ utilSample = $true } }"
        good = "[pscustomobject]@{ arm = 'LIGHT'; source = 'environment'; reason = 'telemetryArm=LIGHT' }"
        ga = "legType = 'speed'; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }; "
        body = ""
        for label, expr in (
                ("LEG_BAD", f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = {bad}; {ga}hostLoadState = 'quiet' }})"),
                ("LEG_GOOD", f"Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = {good}; {ga}hostLoadState = 'quiet' }})"),
                ("SMOKE_BAD", f"Get-FpsArmFromSmokeRecord ([pscustomobject]@{{ telemetryArm = {bad}; {RECORDED_LIGHT_LAUNCH} }})"),
                ("SMOKE_GOOD", f"Get-FpsArmFromSmokeRecord ([pscustomobject]@{{ telemetryArm = {good}; {RECORDED_LIGHT_LAUNCH} }})")):
            body += f"Write-Host ('{label}=' + ({expr}).arm)\n"
        body += (f"$v = Get-FpsPairVerdict -Baseline (Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = {bad}; {ga}hostLoadState = 'quiet' }})) -Candidate 'LIGHT'\n"
                 "Write-Host ('PAIR_BAD=' + $v.allowed)\n")
        out = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(out, {"LEG_BAD": "UNKNOWN", "LEG_GOOD": "LIGHT", "SMOKE_BAD": "UNKNOWN", "SMOKE_GOOD": "LIGHT", "PAIR_BAD": "False"})

    def test_p3_a3_a_scopes_spec_reads_as_its_launch_does(self) -> None:
        # adversary 835a5faf A3 / 8747fced A3: the committed m16-1243-scopes-cuda-windowed-s1-histogram-i0 spec (scope histogram, interval 0, throttled) read LIGHT from the spec, the saved
        # leg's generatorArgs and spec+result, while the launch it emits reads UNKNOWN (--scope histogram and the scope env are outside the LIGHT launch shape). Every spec-side reader agrees now.
        spec_path = LEGS_DIR / "m16-1243-scopes-cuda-windowed-s1-histogram-i0.json"
        ga_bad = "telemetryArm = 'LIGHT'; scope = 'histogram'; scopeIntervalMs = 0; scopesDuringPlayback = 'throttled'"
        body = (f"$s = Get-Content -LiteralPath '{spec_path}' -Raw | ConvertFrom-Json\n"
                "Write-Host ('COMMITTED=' + (Get-FpsArmFromLegSpec $s).arm)\n"
                "Write-Host ('COMMITTED_RESULT=' + (Get-FpsArm -Spec $s -Result ([pscustomobject]@{ telemetryArm = 'LIGHT' })).arm)\n"
                f"Write-Host ('SAVED_LEG=' + (Get-FpsArmFromLegRecord ([pscustomobject]@{{ telemetryArm = 'LIGHT'; legType = 'speed'; generatorArgs = [pscustomobject]@{{ {ga_bad} }} }})).arm)\n")
        for label, ga in (("SCOPE_NONE", "scope = 'none'"), ("INTERVAL_DEFAULT", "scopeIntervalMs = -1"), ("MODE_EMPTY", "scopesDuringPlayback = ''"),
                          ("SCOPE_NULL", "scope = $null"), ("INTERVAL_ZERO", "scopeIntervalMs = 0"), ("MODE_SET", "scopesDuringPlayback = 'show'")):
            body += f"Write-Host ('{label}=' + (Get-FpsArmFromLegSpec ([pscustomobject]@{{ legType = 'speed'; generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT'; {ga} }} }})).arm)\n"
        out = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(out, {"COMMITTED": "UNKNOWN", "COMMITTED_RESULT": "UNKNOWN", "SAVED_LEG": "UNKNOWN", "SCOPE_NONE": "LIGHT", "INTERVAL_DEFAULT": "LIGHT",
                               "MODE_EMPTY": "LIGHT", "SCOPE_NULL": "UNKNOWN", "INTERVAL_ZERO": "UNKNOWN", "MODE_SET": "UNKNOWN"})

    def test_p3_a4_a_cached_analysis_is_bound_only_by_exact_equal_evidence(self) -> None:
        # adversary 835a5faf A4 / 8747fced A2: the binding compared the presented-fps delta within 1e-6 (-1.0000004 and -1.0 sit on opposite sides of the -1 % diagnosis threshold) and nothing
        # else, so cached queue-wait advice was republished beside a compare with no queue-wait regression and a 40 % llrawproc regression.
        body = ("function A($ev) { [pscustomobject]@{ comparisonBasis = 'candidateSpeed'; evidence = $ev } }\n"
                "function M($dp) { [pscustomobject]@{ deltaPercent = $dp } }\n")
        cases = (
            ("TOLERANCE", "[pscustomobject]@{ presentedFpsDeltaPercent = -1.0000004 }", "[pscustomobject]@{ presentedFps = (M (-1.0)) }"),
            ("STAGE", "[pscustomobject]@{ presentedFpsDeltaPercent = -2.0; queueWaitDeltaPercent = 30.0 }", "[pscustomobject]@{ presentedFps = (M (-2.0)); avgLlrawprocMs = (M 40.0) }"),
            ("CANDIDATE_MS", "[pscustomobject]@{ presentedFpsDeltaPercent = 12.0; candidateQueueWaitMs = 20.0 }", "[pscustomobject]@{ presentedFps = (M 12.0); avgQueueWaitMs = [pscustomobject]@{ candidate = 5.0 } }"),
            ("EXACT", "[pscustomobject]@{ presentedFpsDeltaPercent = -2.0; queueWaitDeltaPercent = 30.0; llrawprocDeltaPercent = 40.0; candidateQueueWaitMs = 20.0 }",
             "[pscustomobject]@{ presentedFps = (M (-2)); avgQueueWaitMs = [pscustomobject]@{ deltaPercent = 30; candidate = 20 }; avgLlrawprocMs = (M 40.0) }"),
        )
        for label, ev, cmp in cases:
            body += f"Write-Host ('{label}=' + (Test-FpsCachedAnalysisBound -Analysis (A ({ev})) -Compare ({cmp}) -ComparisonBasis 'candidateSpeed'))\n"
        # adversary 0e9672e5 A1 (sibling): two different overflowing JSON numbers both parse to +Infinity and compared equal
        body += ("$inf = '{\"analysis\":{\"comparisonBasis\":\"candidateSpeed\",\"evidence\":{\"presentedFpsDeltaPercent\":1e400}},\"compare\":{\"presentedFps\":{\"deltaPercent\":1e500}}}' | ConvertFrom-Json\n"
                 "Write-Host ('INFINITE=' + (Test-FpsCachedAnalysisBound -Analysis $inf.analysis -Compare $inf.compare -ComparisonBasis 'candidateSpeed'))\n")
        # adversary 1ce472bc A4: two different integers past 2^53 collapse to one double
        for label, cached, judged in (("BIGINT_DIFFERENT", "9007199254740993", "9007199254740992"), ("BIGINT_SAME", "9007199254740993", "9007199254740993"),
                                      ("BIGINT_VS_DOUBLE", "9007199254740993", "9007199254740992.0")):
            body += (f"$big = '{{\"analysis\":{{\"comparisonBasis\":\"candidateSpeed\",\"evidence\":{{\"presentedFpsDeltaPercent\":{cached}}}}},\"compare\":{{\"presentedFps\":{{\"deltaPercent\":{judged}}}}}}}' | ConvertFrom-Json\n"
                     f"Write-Host ('{label}=' + (Test-FpsCachedAnalysisBound -Analysis $big.analysis -Compare $big.compare -ComparisonBasis 'candidateSpeed'))\n")
        out = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(out, {"TOLERANCE": "False", "STAGE": "False", "CANDIDATE_MS": "False", "EXACT": "True", "INFINITE": "False",
                               "BIGINT_DIFFERENT": "False", "BIGINT_SAME": "True", "BIGINT_VS_DOUBLE": "False"})

    def test_p3_a6_speed_authority_needs_a_pass_section_one_authority_object_and_no_repeated_key(self) -> None:
        # adversary 835a5faf A6 / 8747fced A6: package and import granted speedQuotable from status + claimAuthority.speed alone: a REFUSED playbackSpeed section, singleton arrays and a
        # repeated speed key all read true. One module predicate now, over the parsed report and its raw text.
        auth = '"claimAuthority":{"speed":true}'
        section = '"playbackSpeed":{"status":"PASS"}'
        cases = {
            "GOOD": '{"status":"QUOTABLE_PASS",' + auth + ',' + section + '}',
            "REFUSED": '{"status":"QUOTABLE_PASS",' + auth + ',"playbackSpeed":{"status":"REFUSED"}}',
            "NO_SECTION": '{"status":"QUOTABLE_PASS",' + auth + '}',
            "AUTH_ARRAY": '{"status":"QUOTABLE_PASS","claimAuthority":[{"speed":true}],' + section + '}',
            "STATUS_ARRAY": '{"status":["QUOTABLE_PASS"],' + auth + ',' + section + '}',
            "REPEATED": '{"status":"QUOTABLE_PASS","claimAuthority":{"speed":false,"speed":true},' + section + '}',
            "SPEED_TEXT": '{"status":"QUOTABLE_PASS","claimAuthority":{"speed":"true"},' + section + '}',
            "NOT_QUOTABLE": '{"status":"NOT_QUOTABLE",' + auth + ',' + section + '}',
        }
        body = ""
        for label, text in cases.items():
            body += (f"$t = '{text}'\n"
                     f"try {{ $r = $t | ConvertFrom-Json; Write-Host ('{label}=' + (Test-FpsProofReportSpeedQuotable -Report $r -RawText $t)) }} catch {{ Write-Host '{label}=PARSE_THREW' }}\n")
        out = dict(line.split("=", 1) for line in self.ok(body).split())
        want = {label: "False" for label in cases}
        want["GOOD"] = "True"
        if out.get("REPEATED") == "PARSE_THREW":
            want["REPEATED"] = "PARSE_THREW"
        self.assertEqual(out, want)

    def test_p3_a5_a_pass_section_without_a_measured_presented_fps_pair_grants_no_speed(self) -> None:
        # adversary 835a5faf A5 / 8747fced A4: compare:{} (or a compare holding only stage metrics) passed the missing-compare check, the evidence printed "presented FPS: n/a", the section read
        # PASS and claimAuthority.speed was true.
        funcs = _extract_functions(SUMMARIZER_SCRIPT, ["Get-Field", "New-SectionVerdict", "Get-ProofSpeedClaimAuthority"])
        body = funcs + "\n$pass = New-SectionVerdict -Status 'PASS' -Passed $true -Evidence @() -Blockers @()\n"
        for label, cmp in (("EMPTY", "[pscustomobject]@{}"), ("NULL", "$null"),
                           ("STAGE_ONLY", "[pscustomobject]@{ avgRenderTotalMs = [pscustomobject]@{ baseline = 10; candidate = 9 } }"),
                           ("FPS_TEXT", "[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = '50'; candidate = '55' } }"),
                           ("FPS_ONE_SIDE", "[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 50; candidate = $null } }"),
                           # adversary 0e9672e5 A1: valid JSON 1e400 parses to +Infinity, which passed the numeric-type check
                           ("FPS_INFINITE", "('{\"presentedFps\":{\"baseline\":1e400,\"candidate\":55}}' | ConvertFrom-Json)"),
                           # adversary 1ce472bc A3: finite sides with an Infinity delta / deltaPercent
                           ("DELTA_INFINITE", "('{\"presentedFps\":{\"baseline\":24,\"candidate\":25,\"delta\":1e400,\"deltaPercent\":1e400}}' | ConvertFrom-Json)"),
                           ("DELTA_PERCENT_INFINITE", "('{\"presentedFps\":{\"baseline\":24,\"candidate\":25,\"delta\":1,\"deltaPercent\":-1e400}}' | ConvertFrom-Json)"),
                           # adversary 698fffb1 A4: singleton arrays unrolled to the numbers inside them
                           ("FPS_SINGLETON_ARRAY", "('{\"presentedFps\":{\"baseline\":[30],\"candidate\":[60],\"delta\":[30],\"deltaPercent\":[100]}}' | ConvertFrom-Json)"),
                           ("DELTA_SINGLETON_ARRAY", "('{\"presentedFps\":{\"baseline\":30,\"candidate\":60,\"delta\":[30]}}' | ConvertFrom-Json)"),
                           ("DELTA_TEXT","[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 24; candidate = 25; delta = 'one' } }"),
                           ("MEASURED_WITH_DELTA", "[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 24; candidate = 25; delta = 1; deltaPercent = 4.17 } }"),
                           ("MEASURED", "[pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 50; candidate = 55.5 } }")):
            body += (f"$a = Get-ProofSpeedClaimAuthority -OverallStatus 'QUOTABLE_PASS' -PlaybackSpeedVerdict $pass -SpeedCompare ({cmp})\n"
                     f"Write-Host ('{label}=' + $a.speed + '|' + $a.speedRefusal)\n")
        out = dict(line.split("=", 1) for line in self.ok(body).split())
        for label in ("EMPTY", "NULL", "STAGE_ONLY", "FPS_TEXT", "FPS_ONE_SIDE", "FPS_INFINITE", "DELTA_INFINITE", "DELTA_PERCENT_INFINITE", "FPS_SINGLETON_ARRAY", "DELTA_SINGLETON_ARRAY", "DELTA_TEXT"):
            self.assertEqual(out[label], "False|presented-fps-not-measured", label)
        self.assertEqual(out["MEASURED"], "True|")
        self.assertEqual(out["MEASURED_WITH_DELTA"], "True|")
        self.assertIn("-PlaybackSpeedVerdict $playbackAbVerdict -SpeedCompare $playbackAbSpeedCompare", SUMMARIZER_SCRIPT.read_text(encoding="utf-8"))

    # (r3: adversary 8747fced A5 "packaging never reuses a report an earlier run left" is held by the end-to-end test_r3_sol_5add23cf_a7_...: the children write into this run's empty private directory, so
    # no report an earlier run left can be taken, and a summarizer that refuses before writing is a packaging error that touches nothing. Adversary 0e9672e5 A2 (a directory as the report path) is held by
    # test_r3_sol_5add23cf_pkg_report_paths_...)

    # (r3: adversary 1ce472bc A1 / A7 and 698fffb1 A1 / A8 (the input kept, each child writing a name only this run knows, a junction in front of the report, a report name at the filesystem's limit) are
    # held end to end by test_r3_sol_5add23cf_pkg_report_paths_... and test_r3_sol_5add23cf_pkg_a_normal_run_...; the children's private names are asserted there)
    def test_r2_packager_launches_its_summarizer_from_the_running_powershell(self) -> None:
        self.assertIn("$pwshExe = Join-Path $PSHOME $pwshLeaf", (PROFILING / "package-local-cuda-proof-result.ps1").read_text(encoding="utf-8"))

    def test_r2_adversary_1ce472bc_a8_an_imported_speed_claim_needs_a_verified_proof_report_hash(self) -> None:
        # adversary 1ce472bc A8: a packet whose manifest listed no files (zero hash checks) published speedQuotable=true; the importer's own line now runs against each shape of file checks
        text = (PROFILING / "import-local-cuda-proof-result.ps1").read_text(encoding="utf-8")
        match = re.search(r"(?m)^\$speedHashBound = .*$", text)
        self.assertIsNotNone(match)
        line = match.group(0)
        # sol 5add23cf A9: the spelling of the report's name is compared the way the volume compares names (the importer opened the file by that name), so the line is run with its own comparison
        comparison = re.search(r"(?m)^\$nameComparison = .*$", text)
        self.assertIsNotNone(comparison)
        # (sol 5add23cf A8: the comparison is asked of the volume the opened report sits on, so the line runs beside a real proof-report.json in this test's own temp directory)
        probe_file = self.tmp / "proof-report.json"
        probe_file.write_text("{}", encoding="utf-8")
        line = f"$proofReportJsonPath = '{probe_file}'\n" + comparison.group(0) + "\n" + line
        case_insensitive_volume = os.name == "nt"
        # sol c29f8dc1 A3 (importer sibling): the hash the manifest check verified (actualSha256) must be the hash of the bytes that were parsed ($proofReportJudgedSha256); a report replaced between the
        # read and the hash verifies a different file than the one judged. HASH_OF_OTHER_BYTES / NOTHING_JUDGED fail on c29f8dc1, which bound the check to the path alone.
        cases = {
            "VERIFIED": ("0", "@([pscustomobject]@{ ok = $true; relativePath = 'proof-report.json'; actualSha256 = 'ab12' })", "'AB12'"),
            "NO_CHECKS": ("0", "@()", "'AB12'"),
            "OTHER_FILE": ("0", "@([pscustomobject]@{ ok = $true; relativePath = 'summary.json'; actualSha256 = 'ab12' })", "'AB12'"),
            # adversary 698fffb1 A3: PowerShell's -eq is case-insensitive, so PROOF-REPORT.JSON (another file on a case-sensitive filesystem) matched the lowercase report the importer read
            "OTHER_CASE": ("0", "@([pscustomobject]@{ ok = $true; relativePath = 'PROOF-REPORT.JSON'; actualSha256 = 'ab12' })", "'AB12'"),
            "HASH_WRONG": ("1", "@([pscustomobject]@{ ok = $false; relativePath = 'proof-report.json'; actualSha256 = 'ab12' })", "'AB12'"),
            "FAILED_IMPORT": ("1", "@([pscustomobject]@{ ok = $true; relativePath = 'proof-report.json'; actualSha256 = 'ab12' })", "'AB12'"),
            "HASH_OF_OTHER_BYTES": ("0", "@([pscustomobject]@{ ok = $true; relativePath = 'proof-report.json'; actualSha256 = 'ff00' })", "'AB12'"),
            "NOTHING_JUDGED": ("0", "@([pscustomobject]@{ ok = $true; relativePath = 'proof-report.json'; actualSha256 = 'ab12' })", "$null"),
            # sol 5add23cf A9: the genuine report of a packet built with -ReportPath ...\Proof-Report.md is listed as Proof-Report.json; a different file never gains the authority
            "GENUINE_CASE_VARIANT": ("0", "@([pscustomobject]@{ ok = $true; relativePath = 'Proof-Report.json'; actualSha256 = 'ab12' })", "'AB12'"),
            "SUFFIXED": ("0", "@([pscustomobject]@{ ok = $true; relativePath = 'proof-report.json.bak'; actualSha256 = 'ab12' })", "'AB12'"),
            "BELOW_A_FOLDER": ("0", "@([pscustomobject]@{ ok = $true; relativePath = 'old/proof-report.json'; actualSha256 = 'ab12' })", "'AB12'"),
        }
        body = ""
        for label, (failures, checks, judged) in cases.items():
            body += (f"$failures = [System.Collections.Generic.List[string]]::new(); if ({failures} -gt 0) {{ $failures.Add('x') }}\n"
                     f"$fileChecks = {checks}\n$proofReportJudgedSha256 = {judged}\n{line}\nWrite-Host ('{label}=' + $speedHashBound)\n")
        out = dict(item.split("=", 1) for item in self.ok(body).split())
        self.assertEqual(out, {"VERIFIED": "True", "NO_CHECKS": "False", "OTHER_FILE": "False", "OTHER_CASE": str(case_insensitive_volume), "HASH_WRONG": "False", "FAILED_IMPORT": "False",
                               "HASH_OF_OTHER_BYTES": "False", "NOTHING_JUDGED": "False", "GENUINE_CASE_VARIANT": str(case_insensitive_volume), "SUFFIXED": "False", "BELOW_A_FOLDER": "False"})

    def test_p2_a5_the_packager_launches_its_summarizer_by_a_path_and_a_name_that_exist_on_linux(self) -> None:
        # hosted run 45786756378 (ubuntu shard 3/4): Invoke-ProofSummarizer joined "tools\profiling\summarize-local-cuda-proof.ps1" (one file name on Linux, so "Proof summarizer not
        # found") and launched `pwsh.exe` (no such command on Linux). A static pin so the Windows shards catch the same shape: no backslash in a repo-relative path of the three CUDA-proof
        # scripts, and the child's name is chosen by the directory separator.
        scripts = [PROFILING / name for name in ("package-local-cuda-proof-result.ps1", "import-local-cuda-proof-result.ps1", "summarize-local-cuda-proof.ps1")]
        shape = re.compile(r"""Join-Path\s+\S+\s+(["'])[A-Za-z_.\-]+\\""")
        for script in scripts:
            for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1):
                self.assertIsNone(shape.search(line), f"{script.name}:{number} joins a repo-relative path with a backslash: {line.strip()}")
        packager = scripts[0].read_text(encoding="utf-8")
        self.assertIn('[IO.Path]::DirectorySeparatorChar -eq [char]92) { "pwsh.exe" } else { "pwsh" }', packager)
        self.assertEqual(packager.count("& $pwshExe -NoLogo"), 2)
        self.assertNotRegex(packager, r"(?m)^\s*&\s+pwsh\.exe")

    def test_p3_a1_a_purpose_free_plan_is_not_refused_by_a_stalled_arm_read(self) -> None:
        # adversary 835a5faf A1: a purpose-free plan whose arm read got no answer exited SPEC_READ_TIMEOUT item=1 before staging, where the base gave up on the optional ID read and returned
        # BUILD_NOT_STAGED item=1. The arm only decides anything for a plan that declares a purpose, so only such a plan refuses on its arm read's timeout.
        source = LEASE_BATCH_SCRIPT.read_text(encoding="utf-8")
        declared = "$planPurposeDeclared = ($doc -is [System.Management.Automation.PSCustomObject]) -and ($null -ne $doc.PSObject.Properties['purpose'])"
        self.assertEqual(source.count(declared), 1)
        self.assertLess(source.index(declared), source.index("for ($i = 0; $i -lt $rawItems.Count; $i++) {"))
        # adversary 1ce472bc A5: only the scalar string 'fps-ab' lets the arm decide; stage-attribution and a bad declared purpose reach their own refusals
        armDecides = ("$armDecides = $planPurposeDeclared -and ($doc.PSObject.Properties['purpose'].Value -is [string]) "
                      "-and [string]::Equals([string]$doc.PSObject.Properties['purpose'].Value, 'fps-ab', [StringComparison]::Ordinal)")
        self.assertEqual(source.count(armDecides), 1)
        self.assertLess(source.index(armDecides), source.index("for ($i = 0; $i -lt $rawItems.Count; $i++) {"))
        self.assertIn("if ($armRead.readTimedOut -and $armDecides) { Stop-PlanRefused 'SPEC_READ_TIMEOUT' $n ", source)
        self.assertNotIn("if ($armRead.readTimedOut -and $planPurposeDeclared)", source)
        self.assertNotIn("if ($armRead.readTimedOut) { Stop-PlanRefused", source)

    def test_p2_a7_the_smoke_receipt_records_no_inherited_value_the_child_never_gets(self) -> None:
        # adversary 8747fced A7: with -PreserveExperimentalEnvironment and a blank -GpuPlaybackReconBackend the child's environment block removes MLVAPP_GPU_PLAYBACK_RECON_BACKEND, but the
        # receipt recorded the parent's 'bogus' as inherited, so the readers refused an actual LIGHT pair. The runner's own lines run here against the real record function.
        script = PROFILING / "run-release-gui-smoke.ps1"
        source = script.read_text(encoding="utf-8")
        block = _extract_block(source, "$childReplacedEnvironment =", "Add-InheritedMlvAppEnvironmentRecord -Target $launchEnv -Cleared (", trailing_lines=0)
        funcs = _extract_functions(script, ["Add-InheritedMlvAppEnvironmentRecord"])
        body = funcs + "\n$script:GuiSmokePaceModeEnvironment = 'MLVAPP_PLAY_PACE_MODE'\n"
        body += ("$env:MLVAPP_GPU_PLAYBACK_RECON_BACKEND = 'bogus'; $env:MLVAPP_RUN_NONCE = 'parent'; $env:MLVAPP_PLAY_PACE_MODE = 'informational'; $env:MLVAPP_STAGE_TIMING = '1'\n"
                 "$clearedEnvironment = @()\n$launchEnv = [ordered]@{}\n" + block +
                 "Write-Host ('BLANK=' + ((@('MLVAPP_GPU_PLAYBACK_RECON_BACKEND', 'MLVAPP_RUN_NONCE', 'MLVAPP_PLAY_PACE_MODE', 'MLVAPP_STAGE_TIMING') | Where-Object { $launchEnv.Contains($_) }) -join ','))\n"
                 "$launchEnv = [ordered]@{ MLVAPP_GPU_PLAYBACK_RECON_BACKEND = 'cuda' }\n" + block +
                 "Write-Host ('SET=' + $launchEnv['MLVAPP_GPU_PLAYBACK_RECON_BACKEND'])\n")
        out = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(out["BLANK"], "MLVAPP_STAGE_TIMING", "of the four, only the variable the child really inherits is recorded")
        self.assertEqual(out["SET"], "cuda")

    # ---- BENCH-LIGHT-ARM-GAIN-GATES-1-R2-RESIDUE-1 r1 (adversary 698fffb1 A2, A5, A7; 1ce472bc A6) ----

    # one row per generatorArgs knob the venue leg forwards that the SPEC reader reads as LIGHT: (spec generatorArgs fragment, extra launch arguments, extra launch environment). The launch is
    # what the generator emits for it (playback-attr-3-cuda-job.ps1 line in the comment), so a spec and its own launch must read the same arm.
    LAUNCH_KNOBS = {
        "unpaced": ("unpaced = $true", "'--drop-frame-mode', 'off'", "MLVAPP_PLAYBACK_UNPACED = '1'"),  # job:6550 and :6531
        "rawPrefetchDecoders=2": ("rawPrefetchDecoders = 2", "", "MLVAPP_RAW_UINT16_PREFETCH_DECODERS = '2'"),  # job:6126
        "rawPrefetchDecoders=0": ("rawPrefetchDecoders = 0", "", "MLVAPP_DISABLE_RAW_UINT16_PREFETCH = '1'"),  # job:6126
        "disablePrepPointwiseFast": ("disablePrepPointwiseFast = $true", "", "MLVAPP_DISABLE_PREP_POINTWISE_FAST = '1'"),  # job:6565
        "prepConflatedMove": ("prepConflatedMove = $true", "", "MLVAPP_PLAYBACK_PREP_CONFLATED_MOVE = '1'"),  # job:6577
        "disableReconReleaseDeferred": ("disableReconReleaseDeferred = $true", "", "MLVAPP_GPU_RECON_RELEASE_DEFERRED = '0'"),  # job:6589
        "dueWakeDeliveryPost": ("dueWakeDeliveryPost = $true", "", "MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY = 'post'"),  # job:6601
        "ompTeams": ("reconOmpThreads = 2; prepOmpThreads = 3", "", "MLVAPP_PLAYBACK_RECON_OMP_THREADS = '2'; MLVAPP_PLAYBACK_PREP_OMP_THREADS = '3'"),  # job:6655
        "gpuSafeStart": ("gpuSafeStart = 'clear'", "", "MLVAPP_GPU_SAFE_START = 'clear'"),  # job:6416
        "qualityMode": ("qualityMode = 'hq'", "", "MLVAPP_PLAYBACK_QUALITY_MODE = 'hq'"),  # job:5951
        "lookahead": ("forceLookAssist = $true; lookFlavor = 'cinematic'; playbackRenderLookaheadFrames = 3", "", "MLVAPP_LOOK_ASSIST_FLAVOR = 'cinematic'; MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES = '3'"),  # job:6120
    }

    def _launch_expr(self, extra_args: str, extra_env: str) -> str:
        args = PRODUCER_ARGS_PS + (", " + extra_args if extra_args else "")
        env = LIGHT_LAUNCH_ENV_PS + ("; " + extra_env if extra_env else "")
        return f"Get-FpsArmFromLaunch ([pscustomobject]@{{ environment = [pscustomobject]@{{ {env} }}; arguments = @({args}) }})"

    def test_r2_adversary_698fffb1_a5_1ce472bc_a6_a_spec_and_its_own_launch_read_the_same_arm(self) -> None:
        # adversary 698fffb1 A5 (unpaced: -DropFrameMode off) and 1ce472bc A6 (rawPrefetchDecoders: MLVAPP_RAW_UINT16_PREFETCH_DECODERS): the spec reader said LIGHT and the launch reader said UNKNOWN for
        # the launch that spec emits. Ruling: the launch reader LEARNS the knob (each is a decode / pacing / scheduling switch, not a logger), with the exact value domain the generator writes; a value outside
        # that domain, or a knob not named here, is still UNKNOWN. Unknown never becomes LIGHT.
        body = ""
        for label, (spec_ga, extra_args, extra_env) in self.LAUNCH_KNOBS.items():
            spec = f"[pscustomobject]@{{ legType = 'speed'; generatorArgs = [pscustomobject]@{{ telemetryArm = 'LIGHT'; {spec_ga} }} }}"
            body += f"Write-Host ('{label}=SPEC:' + (Get-FpsArmFromLegSpec ({spec})).arm + ' LAUNCH:' + ({self._launch_expr(extra_args, extra_env)}).arm)\n"
        out = self.ok(body).splitlines()
        self.assertEqual(sorted(out), sorted(f"{label}=SPEC:LIGHT LAUNCH:LIGHT" for label in self.LAUNCH_KNOBS), "\n".join(out))
        # windowed: the spec key displayMode passes the app's --windowed (job:6330)
        self.assertEqual(self.ok(f"Write-Host ('ARM=' + ({self._launch_expr(chr(39) + '--windowed' + chr(39), '')}).arm)\n").strip(), "ARM=LIGHT")
        # the exact strings named by the two items
        for text in ("-DropFrameMode", "MLVAPP_RAW_UINT16_PREFETCH_DECODERS"):
            self.assertIn(text, (PROFILING / "bachelor" / "playback-attr-3-cuda-job.ps1").read_text(encoding="utf-8"))
        # a value outside the generator's domain, a repeated flag and a name no producer writes stay UNKNOWN
        refused = {
            "bad-drop-mode": self._launch_expr("'--drop-frame-mode', 'sometimes'", ""),
            "dup-drop-mode": self._launch_expr("'--drop-frame-mode', 'off', '--drop-frame-mode', 'off'", ""),
            "prefetch-5": self._launch_expr("", "MLVAPP_RAW_UINT16_PREFETCH_DECODERS = '5'"),
            "prefetch-0": self._launch_expr("", "MLVAPP_RAW_UINT16_PREFETCH_DECODERS = '0'"),
            "safe-start-other": self._launch_expr("", "MLVAPP_GPU_SAFE_START = 'always'"),
            "due-wake-send": self._launch_expr("", "MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY = 'send'"),
            "windowed-with-value": self._launch_expr("'--windowed=1'", ""),
            "unknown-name": self._launch_expr("", "MLVAPP_RAW_UINT16_PREFETCH_DECODERS_X = '1'"),
        }
        got = self.ok("".join(f"Write-Host ('{k}=' + ({expr}).arm)\n" for k, expr in refused.items())).split()
        self.assertEqual(sorted(got), sorted(f"{k}=UNKNOWN" for k in refused), got)

    def test_r2_adversary_698fffb1_a5_1ce472bc_a6_the_committed_specs_agree_with_their_launch(self) -> None:
        # the two committed specs the items name, read the way the fps-ab plan reads them
        body = ""
        for leg, extra_args, extra_env in (
                ("m16-1243-headroom-unpaced-fullscreen-s1", "'--drop-frame-mode', 'off'", "MLVAPP_PLAYBACK_UNPACED = '1'; MLVAPP_LOOK_ASSIST_FLAVOR = 'cinematic'; MLVAPP_SWAP_INTERVAL = '0'; MLVAPP_PLAYBACK_PRESENT_TRACE = '1'"),
                ("m16-1243-pace-cinematic-fullscreen-s4-dec1", "", "MLVAPP_RAW_UINT16_PREFETCH_DECODERS = '1'; MLVAPP_LOOK_ASSIST_FLAVOR = 'cinematic'")):
            path = LEGS_DIR / f"{leg}.json"
            body += (f"$spec = Get-Content -Raw -LiteralPath '{path}' | ConvertFrom-Json\n"
                     f"Write-Host ('{leg}=' + (Get-FpsArmFromLegSpec $spec).arm + '|' + ({self._launch_expr(extra_args, extra_env)}).arm)\n")
        self.assertEqual(sorted(self.ok(body).split()), sorted([
            "m16-1243-headroom-unpaced-fullscreen-s1=LIGHT|LIGHT", "m16-1243-pace-cinematic-fullscreen-s4-dec1=LIGHT|LIGHT"]))

    def test_r2_adversary_698fffb1_a7_compare_machine_perf_refuses_a_compare_not_measured_on_the_judged_pair(self) -> None:
        # adversary 698fffb1 A7, sibling reader (compare-machine-perf.ps1 Get-PlaybackAbFpsArmRefusal): it publishes the same compare, so it refuses the same way
        funcs = _extract_functions(COMPARE_MACHINE_PERF_SCRIPT, ["Get-PlaybackAbFpsArmRefusal"])
        body = funcs + (
            "$leg = { param($fps) [pscustomobject]@{ telemetryArm = 'LIGHT'; " + RECORDED_LIGHT_LAUNCH + "; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }; legType = 'speed'; presentedFps = $fps; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true } }\n"
            "$cmp = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 50; candidate = 60; delta = 10; deltaPercent = 20 } }\n"
            "foreach ($fps in 60, 40) { $r = [pscustomobject]@{ baseline = (& $leg 50); candidate = (& $leg $fps); candidateSpeed = (& $leg $fps); speedCompare = $cmp }; "
            "$v = Get-PlaybackAbFpsArmRefusal -Record $r; Write-Host ('SPEED' + $fps + '=' + $v.refused + '|' + $v.reason) }\n")
        self.assertEqual(self.ok(body).split(), ["SPEED60=False|", "SPEED40=True|fps-verdict-refused:compare-not-judged-pair"])

    def test_r2_residue_r2_luna_r1b_a1_compare_machine_perf_refuses_a_delta_the_endpoints_do_not_give(self) -> None:
        # luna r1b A1, sibling reader: both legs 40 fps, published delta 40 / deltaPercent 100 (a gain no leg measured)
        funcs = _extract_functions(COMPARE_MACHINE_PERF_SCRIPT, ["Get-PlaybackAbFpsArmRefusal"])
        body = funcs + (
            "$leg = { param($fps) [pscustomobject]@{ telemetryArm = 'LIGHT'; " + RECORDED_LIGHT_LAUNCH + "; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }; legType = 'speed'; presentedFps = $fps; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true } }\n"
            "$cases = [ordered]@{ adversary = @(40, 40, 40, 100); percent = @(40, 40, 0, 100); delta = @(40, 40, 40, 0); zero = @(0, 0, 0, 0); honest = @(40, 40, 0, 0); gain = @(40, 50, 10, 25) }\n"
            "foreach ($k in $cases.Keys) { $c = $cases[$k]; $cmp = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = $c[0]; candidate = $c[1]; delta = $c[2]; deltaPercent = $c[3] } }\n"
            "$r = [pscustomobject]@{ baseline = (& $leg $c[0]); candidate = (& $leg $c[1]); candidateSpeed = (& $leg $c[1]); speedCompare = $cmp }\n"
            "$v = Get-PlaybackAbFpsArmRefusal -Record $r; Write-Host ($k + '=' + $v.refused + '|' + $v.reason) }\n")
        refused = "fps-verdict-refused:compare-not-judged-pair"
        self.assertEqual(self.ok(body).split(), [f"adversary=True|{refused}", f"percent=True|{refused}", f"delta=True|{refused}", f"zero=True|{refused}", "honest=False|", "gain=False|"])

    def test_r2_residue_r2_luna_r1b_a1_the_shared_binding_check_follows_delta_and_percent(self) -> None:
        # the module function the two readers share, fed the adversary's input and the producer's own rounding; a value that is present and not a finite number is refused, an absent one is not this check's business
        body = (
            "function T($b, $c, $m) { $leg = { param($f) [pscustomobject]@{ presentedFps = $f } }; $p = Get-FpsCompareBindingProblem -Compare ([pscustomobject]@{ presentedFps = [pscustomobject]$m }) -Baseline (& $leg $b) -Candidate (& $leg $c); if ($null -eq $p) { 'OK' } else { 'REFUSED' } }\n"
            "Write-Host ('adversary=' + (T 40 40 @{ baseline = 40; candidate = 40; delta = 40; deltaPercent = 100 }))\n"
            "Write-Host ('rounded=' + (T 40 47.123457 @{ baseline = 40; candidate = 47.123457; delta = 7.123457; deltaPercent = 17.809 }))\n"
            "Write-Host ('exact=' + (T 50 49 @{ baseline = 50; candidate = 49; delta = -1; deltaPercent = -2 }))\n"
            "Write-Host ('percent-only=' + (T 10 20 @{ baseline = 10; candidate = 20; deltaPercent = 100 }))\n"
            "Write-Host ('wrong-percent-only=' + (T 10 20 @{ baseline = 10; candidate = 20; deltaPercent = 50 }))\n"
            "Write-Host ('wrong-delta-only=' + (T 10 20 @{ baseline = 10; candidate = 20; delta = 5 }))\n"
            "Write-Host ('inf-delta=' + (T 10 20 @{ baseline = 10; candidate = 20; delta = [double]::PositiveInfinity; deltaPercent = 100 }))\n"
            "Write-Host ('text-percent=' + (T 10 20 @{ baseline = 10; candidate = 20; delta = 10; deltaPercent = 'x' }))\n"
            "Write-Host ('zero-baseline=' + (T 0 5 @{ baseline = 0; candidate = 5; delta = 5; deltaPercent = 100 }))\n"
            "Write-Host ('negative-baseline=' + (T -5 5 @{ baseline = -5; candidate = 5; delta = 10; deltaPercent = -200 }))\n"
            # sol c29f8dc1 A1: legs at 0 fps, a compare of 1e-7 / 2e-7 (self-consistent, +100 %) must not bind to them
            "Write-Host ('sol-a1=' + (T 0 0 @{ baseline = 0.0000001; candidate = 0.0000002; delta = 0.0000001; deltaPercent = 100 }))\n"
            # sol c29f8dc1 A2: a tie with a one-micro delta, and a percent one rounding step off the producer's -1.001
            "Write-Host ('sol-a2-tie=' + (T 40 40 @{ baseline = 40; candidate = 40; delta = -0.000001; deltaPercent = 0 }))\n"
            "Write-Host ('sol-a2-percent=' + (T 40 39.59976 @{ baseline = 40; candidate = 39.59976; delta = -0.40024; deltaPercent = -1 }))\n"
            "Write-Host ('sol-a2-percent-producer-rounded=' + (T 40 39.59976 @{ baseline = 40; candidate = 39.59976; delta = -0.40024; deltaPercent = -1.001 }))\n"
            "Write-Host ('endpoint-off-by-ten-ppm=' + (T 40 40 @{ baseline = 40.0004; candidate = 40; delta = -0.0004; deltaPercent = -0.001 }))\n"
            # sol 5add23cf A1: the producer's own formula gives -1.001 for 24 -> 23.759879 (a rounding step, not a tolerance, separates it from -1); a percent beside a 1e-7 baseline, which the producer never publishes
            "Write-Host ('sol2-a1-percent=' + (T 24 23.759879 @{ baseline = 24; candidate = 23.759879; delta = -0.240121; deltaPercent = -1 }))\n"
            "Write-Host ('sol2-a1-percent-producer=' + (T 24 23.759879 @{ baseline = 24; candidate = 23.759879; delta = -0.240121; deltaPercent = -1.001 }))\n"
            "Write-Host ('sol2-a1-tiny-baseline=' + (T 0.0000001 0.0000002 @{ baseline = 0.0000001; candidate = 0.0000002; delta = 0; deltaPercent = 100 }))\n"
            # sol 5add23cf A2: two legs at 50 fps, a compare whose endpoints are 49.999975 / 50.000025 (a tie published as a +0.00005 gain)
            "Write-Host ('sol2-a2-tie=' + (T 50 50 @{ baseline = 49.999975; candidate = 50.000025; delta = 0.00005; deltaPercent = 0 }))\n"
        )
        got = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(got, {"adversary": "REFUSED", "rounded": "OK", "exact": "OK", "percent-only": "OK", "wrong-percent-only": "REFUSED", "wrong-delta-only": "REFUSED",
                               "inf-delta": "REFUSED", "text-percent": "REFUSED", "zero-baseline": "REFUSED", "negative-baseline": "REFUSED",
                               "sol-a1": "REFUSED", "sol-a2-tie": "REFUSED", "sol-a2-percent": "REFUSED", "sol-a2-percent-producer-rounded": "OK", "endpoint-off-by-ten-ppm": "REFUSED",
                               "sol2-a1-percent": "REFUSED", "sol2-a1-percent-producer": "OK", "sol2-a1-tiny-baseline": "REFUSED", "sol2-a2-tie": "REFUSED"})

    # ---- BENCH-LIGHT-ARM-GAIN-GATES-1-R2-RESIDUE-1 r3 (sol 5add23cf A3, A4, A10) ----

    def test_r3_sol_5add23cf_a3_the_venue_default_visual_capture_dir_is_light_on_both_sides(self) -> None:
        # sol 5add23cf A3: m16-1243-pace-cinematic-fullscreen-s2.json (legType speed, telemetryArm LIGHT, no visualCapture opt-out) reads LIGHT as a spec, but the venue leg launches it with
        # --visual-capture-dir <dir> (DualVenueRunner.psm1 default on, playback-attr-3-cuda-job.ps1 Add-AttrCudaVisualCaptureArgument), which the launch reader did not know: plan LIGHT, launch UNKNOWN.
        spec = LEGS_DIR / "m16-1243-pace-cinematic-fullscreen-s2.json"
        capture = "'--visual-capture-dir', 'C:/run/visual'"
        refused = {
            "no-value": self._launch_expr("'--visual-capture-dir'", ""),
            "repeated": self._launch_expr(capture + ", " + capture, ""),
            "inline-value": self._launch_expr("'--visual-capture-dir=C:/run/visual'", ""),
            "flag-as-value": self._launch_expr("'--visual-capture-dir', '--windowed'", ""),
            "near-miss-name": self._launch_expr("'--visual-capture-frames', '6'", ""),
        }
        body = (f"$spec = Get-Content -Raw -LiteralPath '{spec}' | ConvertFrom-Json\n"
                "Write-Host ('SPEC=' + (Get-FpsArmFromLegSpec $spec).arm)\n"
                f"Write-Host ('LAUNCH=' + ({self._launch_expr(capture, '')}).arm)\n"
                f"Write-Host ('LAUNCH-AND-WINDOWED=' + ({self._launch_expr(capture + ', ' + chr(39) + '--windowed' + chr(39), '')}).arm)\n"
                + "".join(f"Write-Host ('{k}=' + ({expr}).arm)\n" for k, expr in refused.items()))
        got = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(got, {"SPEC": "LIGHT", "LAUNCH": "LIGHT", "LAUNCH-AND-WINDOWED": "LIGHT", **{k: "UNKNOWN" for k in refused}})

    def test_r3_sol_5add23cf_a4_a_known_heavy_launch_is_heavy_arm_never_unknown_arm(self) -> None:
        # sol 5add23cf A4: the LIGHT allowlist refused the HEAVY producer's own telemetry names (timeline telemetry, stage timing, perf field log) before the HEAVY recognizer ran, so a known-HEAVY
        # pair was refused as unknown-arm. The recognizer (frame telemetry on, per-frame log not opted out) now runs first; a launch that opts the frame log out stays judged by the LIGHT table.
        heavy_env = ("MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'; MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '0'; MLVAPP_PLAYBACK_SMOKE_TIMELINE_TELEMETRY = '1'; "
                     "MLVAPP_PLAYBACK_DETAILED_TIMELINE_TELEMETRY = '1'; MLVAPP_STAGE_TIMING = '1'; MLVAPP_PERF_FIELD_LOG = '1'; " + PRODUCER_LOGDIR_PS)
        def launch(env: str, args: str = PRODUCER_ARGS_PS) -> str:
            return f"Get-FpsArmFromLaunch ([pscustomobject]@{{ environment = [pscustomobject]@{{ {env} }}; arguments = @({args}) }})"
        cases = {
            "heavy-producer-env": launch(heavy_env),
            "heavy-env-plus-aux-flag": launch(heavy_env, PRODUCER_ARGS_PS + ", '--stage-log=C:\\Temp\\s.csv'"),
            "heavy-env-no-opt-out-key": launch(heavy_env.replace("MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG = '0'; ", "")),
            "light-opt-out-plus-stage-timing": launch(LIGHT_LAUNCH_ENV_PS + "; MLVAPP_STAGE_TIMING = '1'"),
            "light-opt-out-plus-aux-flag": launch(LIGHT_LAUNCH_ENV_PS, PRODUCER_ARGS_PS + ", '--stage-log=C:\\Temp\\s.csv'"),
            "telemetry-value-two": launch(heavy_env.replace("MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '1'", "MLVAPP_PLAYBACK_SMOKE_TELEMETRY = '2'")),
        }
        got = dict(line.split("=", 1) for line in self.ok("".join(f"Write-Host ('{k}=' + ({expr}).arm)\n" for k, expr in cases.items())).split())
        self.assertEqual(got, {"heavy-producer-env": "HEAVY", "heavy-env-plus-aux-flag": "HEAVY", "heavy-env-no-opt-out-key": "HEAVY",
                               "light-opt-out-plus-stage-timing": "UNKNOWN", "light-opt-out-plus-aux-flag": "UNKNOWN", "telemetry-value-two": "UNKNOWN"})
        # and the pair verdict a reader publishes names the heavy arm
        verdict = self.ok(f"$h = {cases['heavy-producer-env']}\n$v = Get-FpsPairVerdict $h $h\nWrite-Host ($v.allowed.ToString() + '|' + $v.reason)\n").strip()
        self.assertEqual(verdict, "False|" + HEAVY_REASON)

    BINDING_LEG = ("[pscustomobject]@{ presentedFps = 40; guiStatusFps = 40; avgPresentIntervalMs = 25; avgRenderTotalMs = 10; fallbackFrameCount = 0 }")

    def _binding(self, compare: str, baseline: str | None = None, candidate: str | None = None) -> str:
        body = (f"$cmp = [pscustomobject]@{{ {compare} }}\n"
                f"$p = Get-FpsCompareBindingProblem -Compare $cmp -Baseline ({baseline or self.BINDING_LEG}) -Candidate ({candidate or self.BINDING_LEG})\n"
                "if ($null -eq $p) { Write-Host 'OK' } else { Write-Host ('REFUSED ' + $p) }\n")
        return self.ok(body).strip()

    def test_r3_sol_5add23cf_a10_every_metric_the_speed_compare_publishes_is_bound_to_the_judged_legs(self) -> None:
        # sol 5add23cf A10 (input verbatim): both judged legs presentedFps 40, guiStatusFps 40, avgPresentIntervalMs 25; the compare binds presentedFps (40 / 40, delta 0) but publishes guiStatusFps 10 -> 20
        # (+100 %) and avgPresentIntervalMs 50 -> 25 (-50 %), which no judged leg measured.
        honest = "presentedFps = [pscustomobject]@{ baseline = 40; candidate = 40; delta = 0; deltaPercent = 0 }"
        adversary = (honest + "; guiStatusFps = [pscustomobject]@{ baseline = 10; candidate = 20; delta = 10; deltaPercent = 100 }; "
                     "avgPresentIntervalMs = [pscustomobject]@{ baseline = 50; candidate = 25; delta = -25; deltaPercent = -50 }")
        self.assertTrue(self._binding(adversary).startswith("REFUSED"), self._binding(adversary))
        metric = lambda name, b, c, d, p: f"{name} = [pscustomobject]@{{ baseline = {b}; candidate = {c}; delta = {d}; deltaPercent = {p} }}"
        cases = {
            # bound: the legs' own numbers, the producer's own rounding
            "bound-all": (honest + "; " + metric("guiStatusFps", 40, 40, 0, 0) + "; " + metric("avgPresentIntervalMs", 25, 25, 0, 0) + "; " + metric("avgRenderTotalMs", 10, 10, 0, 0), "OK"),
            "bound-zero-count": (honest + "; " + metric("fallbackFrameCount", 0, 0, 0, "$null"), "OK"),
            # a metric whose endpoints are not the legs' numbers
            "gui-endpoints": (honest + "; " + metric("guiStatusFps", 10, 20, 10, 100), "REFUSED"),
            "interval-candidate-only": (honest + "; " + metric("avgPresentIntervalMs", 25, 20, -5, -20), "REFUSED"),
            "render-total-baseline-only": (honest + "; " + metric("avgRenderTotalMs", 12, 10, -2, -16.667), "REFUSED"),
            # endpoints bound, the published delta / percent do not follow from them
            "gui-delta": (honest + "; " + metric("guiStatusFps", 40, 40, 5, 0), "REFUSED"),
            "interval-percent": (honest + "; " + metric("avgPresentIntervalMs", 25, 25, 0, 50), "REFUSED"),
            "count-percent-at-zero-baseline": (honest + "; " + metric("fallbackFrameCount", 0, 0, 0, 100), "REFUSED"),
            "metric-not-finite": (honest + "; " + metric("guiStatusFps", 40, 40, 0, "[double]::NaN"), "REFUSED"),
            # a metric the legs do not record cannot be bound, so it is not published
            "metric-no-leg-value": (honest + "; " + metric("avgQueueWaitMs", 3, 4, 1, 33.333), "REFUSED"),
            "metric-not-an-object": (honest + "; avgQueueWaitMs = 'x'", "REFUSED"),
            # an all-null metric publishes nothing
            "metric-all-null": (honest + "; " + metric("avgQueueWaitMs", "$null", "$null", "$null", "$null"), "OK"),
        }
        got = {name: self._binding(compare).split(" ", 1)[0] for name, (compare, _) in cases.items()}
        self.assertEqual(got, {name: want for name, (_, want) in cases.items()})

    # ---- R2-RESIDUE-1 r3 (sol 5add23cf A5-A9): the packager and importer, run END TO END (the real scripts, a fake summarizer in a throwaway repo) ----

    PACKAGER = PROFILING / "package-local-cuda-proof-result.ps1"
    IMPORTER = PROFILING / "import-local-cuda-proof-result.ps1"
    # The throwaway summarizer: it reads the summary it is given, writes a valid report that names the host and fps it read (so a test can see WHICH summary a child read), and behaves as the environment
    # says (FAKE_REFUSE = exit 1 before writing; FAKE_SWAP_FIRST_CHILD / FAKE_SWAP_CONTENT = a rival replaces the ORIGINAL summary while the first child runs; FAKE_LOG = a line per call).
    FAKE_SUMMARIZER = (
        "param($RepoRoot, $SummaryPath, $Output, [switch]$Json, $SourceSummaryLabel)\n"
        "$ErrorActionPreference = 'Stop'\n"
        "if ($env:FAKE_LOG) { [IO.File]::AppendAllText($env:FAKE_LOG, ($SummaryPath + '|' + $Output + '|' + $SourceSummaryLabel + \"`n\")) }\n"
        "if ($env:FAKE_REFUSE -eq '1') { exit 1 }\n"
        "$s = [IO.File]::ReadAllText($SummaryPath) | ConvertFrom-Json\n"
        "if ($env:FAKE_SWAP_FIRST_CHILD -and -not $Json) { [IO.File]::WriteAllText($env:FAKE_SWAP_FIRST_CHILD, $env:FAKE_SWAP_CONTENT) }\n"
        "if ($Json) {\n"
        "    $report = [ordered]@{ schema = 'mlvapp-local-cuda-proof-report.v1'; status = 'QUOTABLE_PASS'; claimAuthority = [ordered]@{ speed = $true }; playbackSpeed = [ordered]@{ status = 'PASS' }; topLevelStatus = 'PASS'; host = [string]$s.host.name; fps = [string]$s.fps; who = 'own'; blockers = @(); warnings = @(); diagnostics = @() }\n"
        "    [IO.File]::WriteAllText($Output, ($report | ConvertTo-Json -Depth 8))\n"
        "} else { [IO.File]::WriteAllText($Output, ('md host=' + $s.host.name + ' fps=' + $s.fps)) }\n"
        "exit ([int]$env:FAKE_EXIT)\n")

    def _pkg_fixture(self, host: str = "Dell", fps: int = 40) -> tuple[Path, Path, bytes]:
        repo = self.tmp / "repo"
        (repo / "tools" / "profiling").mkdir(parents=True, exist_ok=True)
        (repo / "tools" / "profiling" / "summarize-local-cuda-proof.ps1").write_text(self.FAKE_SUMMARIZER, encoding="utf-8")
        run = self.tmp / "run"
        (run / "logs").mkdir(parents=True, exist_ok=True)
        original = json.dumps({"schema": "mlvapp-local-cuda-playback-dng-smoke.v1", "status": "success", "host": {"name": host}, "fps": fps}).encode("utf-8")
        (run / "summary.json").write_bytes(original)
        (run / "logs" / "a.txt").write_text("log-a", encoding="utf-8")
        return repo, run, original

    def _package(self, repo: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        full_env = dict(os.environ)
        full_env.update(env or {})
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.PACKAGER), "-RepoRoot", str(repo), *args],
                              capture_output=True, text=True, env=full_env, creationflags=_NO_WINDOW)

    def _import(self, repo: Path, packet: Path, *args: str) -> dict:
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(self.IMPORTER), "-RepoRoot", str(repo), "-PacketPath", str(packet),
                               "-DestinationRoot", str(self.tmp / "imported"), "-AllowRepoHeadMismatch", *args], capture_output=True, text=True, creationflags=_NO_WINDOW)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def _private_dirs(self) -> list[str]:
        return sorted(p.name for p in self.tmp.iterdir() if p.name.startswith(".pkg-"))

    def test_r3_sol_5add23cf_pkg_a_normal_run_packages_exactly_the_bytes_it_hashed_and_leaves_nothing_behind(self) -> None:
        # the whole packet from one structure: the archive, the manifest's hashes and the published reports all describe the same bytes, and the private directory is gone
        import hashlib, zipfile
        repo, run, original = self._pkg_fixture()
        packet = self.tmp / "out" / "packet.zip"
        log = self.tmp / "children.log"
        proc = self._package(repo, "-RunRoot", str(run), "-PacketPath", str(packet), env={"FAKE_LOG": str(log)})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        manifest = json.loads(proc.stdout)
        sha = lambda data: hashlib.sha256(data).hexdigest()
        with zipfile.ZipFile(packet) as archive:
            names = sorted(archive.namelist())
            self.assertEqual(names, ["local-cuda-proof-packet-manifest.json", "logs/a.txt", "proof-report.json", "proof-report.md", "summary.json"])
            carried = {name: archive.read(name) for name in names}
        listed = {entry["relativePath"]: entry["sha256"].lower() for entry in manifest["files"]}
        self.assertEqual(sorted(listed), ["logs/a.txt", "proof-report.json", "proof-report.md", "summary.json"])
        for name, digest in listed.items():
            self.assertEqual(sha(carried[name]), digest, name)
        self.assertEqual(carried["summary.json"], original)
        # the published files are the same bytes the packet carries, the manifest is the published one, and no private directory is left
        self.assertEqual((run / "proof-report.json").read_bytes(), carried["proof-report.json"])
        self.assertEqual((run / "proof-report.md").read_bytes(), carried["proof-report.md"])
        self.assertEqual(manifest["proofReport"]["json"]["sha256"].lower(), listed["proof-report.json"])
        self.assertEqual(manifest["proofReport"]["markdown"]["sha256"].lower(), listed["proof-report.md"])
        self.assertEqual(json.loads((run / "local-cuda-proof-packet-manifest.json").read_text(encoding="utf-8-sig"))["packet"]["sha256"].lower(), sha(packet.read_bytes()))
        self.assertEqual(self._private_dirs(), [])
        self.assertTrue(manifest["proofReport"]["speedQuotable"])
        # the children read the PRIVATE copy and wrote into the private directory; the report names the real summary
        calls = [line.split("|") for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(calls), 2)
        for summary_path, output, label in calls:
            self.assertTrue(Path(summary_path).parent.name.startswith(".pkg-"), summary_path)
            self.assertEqual(Path(summary_path).parent, Path(output).parent)
            self.assertNotEqual(os.path.realpath(summary_path), os.path.realpath(run / "summary.json"))
            self.assertEqual(os.path.realpath(label), os.path.realpath(run / "summary.json"))
        # the importer accepts it, and the genuine report keeps its speed authority
        imported = self._import(repo, packet, "-ExpectedHostName", "Dell")
        self.assertEqual((imported["status"], imported["proofReport"]["speedHashBound"], imported["proofReport"]["speedQuotable"]), ("imported", True, True))
        source = self.PACKAGER.read_text(encoding="utf-8")
        self.assertIn("$proofReportRaw = $summarizerResult.jsonText", source)
        self.assertNotRegex(source, r"\$proofReportRaw = Get-Content")
        importer = self.IMPORTER.read_text(encoding="utf-8")
        self.assertIn("$proofReportJudgedSha256 = [Convert]::ToHexString([System.Security.Cryptography.SHA256]::HashData($proofReportBytes))", importer)
        self.assertNotRegex(importer, r"\$proofReportRaw = Get-Content")

    def test_r3_sol_5add23cf_pkg_a_dry_run_publishes_the_reports_and_a_planned_manifest_but_no_packet(self) -> None:
        repo, run, original = self._pkg_fixture()
        packet = self.tmp / "out" / "packet.zip"
        proc = self._package(repo, "-RunRoot", str(run), "-PacketPath", str(packet), "-DryRun")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        manifest = json.loads(proc.stdout)
        self.assertEqual((manifest["status"], manifest["dryRun"]), ("planned", True))
        self.assertFalse(packet.exists())
        self.assertEqual(sorted(e["relativePath"] for e in manifest["files"]), ["logs/a.txt", "proof-report.json", "proof-report.md", "summary.json"])
        self.assertEqual(json.loads((run / "local-cuda-proof-packet-manifest.json").read_text(encoding="utf-8-sig"))["status"], "planned")
        self.assertEqual((run / "summary.json").read_bytes(), original)
        self.assertEqual(self._private_dirs(), [])
        # -StrictQuotable still exits 1 for a report that is not QUOTABLE_PASS (the fake's report always is, so the strict run exits 0)
        self.assertEqual(self._package(repo, "-RunRoot", str(run), "-PacketPath", str(packet), "-StrictQuotable").returncode, 0)

    def test_r3_sol_5add23cf_a8_a_summary_swapped_after_the_first_read_is_not_what_gets_packaged(self) -> None:
        # sol 5add23cf A8 (input): a valid quotable LIGHT summary for host Dell; after the first child reads it a rival atomically replaces summary.json with another valid summary (host UltraMagnus,
        # different fps) and leaves it. The packager read the summary three times, so the manifest said Dell while the JSON child, the artifact hash and the archive carried UltraMagnus.
        import hashlib, zipfile
        repo, run, original = self._pkg_fixture(host="Dell", fps=40)
        swapped = json.dumps({"schema": "mlvapp-local-cuda-playback-dng-smoke.v1", "status": "success", "host": {"name": "UltraMagnus"}, "fps": 99})
        packet = self.tmp / "out" / "packet.zip"
        proc = self._package(repo, "-RunRoot", str(run), "-PacketPath", str(packet), env={"FAKE_SWAP_FIRST_CHILD": str(run / "summary.json"), "FAKE_SWAP_CONTENT": swapped})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        manifest = json.loads(proc.stdout)
        with zipfile.ZipFile(packet) as archive:
            self.assertEqual(archive.read("summary.json"), original)
            report = json.loads(archive.read("proof-report.json"))
        self.assertEqual((report["host"], report["fps"]), ("Dell", "40"), "the JSON child read the same bytes as the first read")
        self.assertEqual(manifest["source"]["host"], "Dell")
        self.assertEqual(manifest["summary"]["artifact"]["sha256"].lower(), hashlib.sha256(original).hexdigest())
        self.assertEqual({e["relativePath"]: e["sha256"].lower() for e in manifest["files"]}["summary.json"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self._import(repo, packet, "-ExpectedHostName", "Dell")["proofReport"]["speedQuotable"], True)

    @requires_windows_pwsh
    def test_r3_sol_5add23cf_a6_a_rival_report_at_the_shared_name_never_reaches_the_archive_or_the_manifest(self) -> None:
        # sol 5add23cf A6: a second writer replaces proof-report.json at the shared name while this run is between its checks (a stand-in `git` writes it during the run, after the children). The archive
        # and the manifest took the shared name, so the run either threw or archived the rival's report; they now come from this run's private bytes.
        import hashlib, zipfile
        repo, run, original = self._pkg_fixture()
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (self.tmp / "rival.json").write_text('{"schema":"mlvapp-local-cuda-proof-report.v1","status":"RIVAL","who":"rival"}', encoding="utf-8")
        (bin_dir / "git.cmd").write_text('@echo off\r\ncopy /y "%FAKE_RIVAL_SRC%" "%FAKE_RIVAL_DST%" >nul\r\nexit /b 1\r\n', encoding="ascii")
        packet = self.tmp / "out" / "packet.zip"
        proc = self._package(repo, "-RunRoot", str(run), "-PacketPath", str(packet),
                             env={"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), "FAKE_RIVAL_SRC": str(self.tmp / "rival.json"), "FAKE_RIVAL_DST": str(run / "proof-report.json")})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        manifest = json.loads(proc.stdout)
        with zipfile.ZipFile(packet) as archive:
            carried = archive.read("proof-report.json")
        self.assertEqual(json.loads(carried)["who"], "own")
        listed = {e["relativePath"]: e["sha256"].lower() for e in manifest["files"]}
        self.assertEqual(listed["proof-report.json"], hashlib.sha256(carried).hexdigest())
        self.assertEqual(manifest["proofReport"]["json"]["sha256"].lower(), hashlib.sha256(carried).hexdigest())

    def test_r3_sol_5add23cf_a7_a_run_that_fails_removes_nothing_at_a_shared_name(self) -> None:
        # sol 5add23cf A7: the stale-report removal at start, the manifest and packet removals and the rollback deleted files at shared names this run did not create (a rival's completed reports, manifest
        # and ZIP). A run whose summarizer refuses its input now touches none of them, and leaves no private directory.
        repo, run, _ = self._pkg_fixture()
        packet = self.tmp / "out" / "packet.zip"
        packet.parent.mkdir()
        rival = {run / "proof-report.md": "rival-md", run / "proof-report.json": "rival-json", run / "local-cuda-proof-packet-manifest.json": "rival-manifest", packet: "rival-zip"}
        for path, text in rival.items():
            path.write_text(text, encoding="utf-8")
        proc = self._package(repo, "-RunRoot", str(run), "-PacketPath", str(packet), env={"FAKE_REFUSE": "1"})
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual({path: path.read_text(encoding="utf-8") for path in rival}, rival)
        self.assertEqual(self._private_dirs(), [])
        # a summarizer that exits 1 AFTER writing is still a packaged run (the report says why); the stale files are then replaced, never read
        proc = self._package(repo, "-RunRoot", str(run), "-PacketPath", str(packet), env={"FAKE_EXIT": "1"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads((run / "proof-report.json").read_text(encoding="utf-8"))["who"], "own")
        # the source holds no delete of a shared name at all: the only Remove-Item removes a file inside this run's own private directory
        source = self.PACKAGER.read_text(encoding="utf-8")
        removals = re.findall(r"Remove-Item[^\n]*", source)
        self.assertEqual(len(removals), 1, removals)
        self.assertIn("-LiteralPath $leftover.FullName", removals[0])
        self.assertNotIn("Compress-Archive", source)
        self.assertEqual(len(re.findall(r"\[IO\.File\]::Move\(", source)), 4)
        self.assertNotIn("Move-Item", source.replace("# (sol 5add23cf A4: [IO.File]::Move replaces a FILE at the destination and throws for a directory, where Move-Item puts the file inside the directory and the manifest would name the directory)", ""))
        self.assertEqual(source.count("[IO.File]::ReadAllBytes($summaryFullPath)"), 1)
        self.assertEqual(source.count("New-Item -ItemType Directory -Path $privateDir -ErrorAction Stop"), 1)

    @requires_windows_pwsh
    def test_r3_sol_5add23cf_a5_a_device_prefix_spelling_of_the_input_is_refused_before_anything_is_written(self) -> None:
        # sol 5add23cf A5 (input): -RunRoot C:\Proof\Run -SummaryPath ...\summary.json -ReportPath \\?\C:\Proof\Run\summary.md. The JSON twin keeps the device prefix and is the same file as the input; the
        # stale-report removal deleted the input before either child started. The input survives and the run is refused.
        repo, run, original = self._pkg_fixture()
        for label, report in (("device-prefix", "\\\\?\\" + str(run / "summary.md")), ("dot-prefix", "\\\\.\\" + str(run / "summary.md"))):
            proc = self._package(repo, "-RunRoot", str(run), "-SummaryPath", str(run / "summary.json"), "-ReportPath", report, "-DryRun")
            self.assertNotEqual(proc.returncode, 0, label + proc.stdout)
            self.assertEqual((run / "summary.json").read_bytes(), original, label)
            self.assertFalse((run / "summary.md").exists(), label)
        funcs = _extract_functions(self.PACKAGER, ["ConvertTo-PlainFilePath", "Get-RealFilePath"])
        plain = str(run / "summary.md")
        body = funcs + (f"Write-Host ('PLAIN=' + (Get-RealFilePath '{plain}'))\nWrite-Host ('DEVICE=' + (Get-RealFilePath ('\\\\?\\' + '{plain}')))\n"
                        # (string work only for the forms with no local file: a UNC name would make Get-Item ask the network)
                        "Write-Host ('UNC=' + (ConvertTo-PlainFilePath '\\\\?\\UNC\\srv\\share\\a\\b.md'))\nWrite-Host ('DOTUNC=' + (ConvertTo-PlainFilePath '\\\\.\\UNC\\srv\\share\\a\\b.md'))\n"
                        "Write-Host ('VOLUME=' + (ConvertTo-PlainFilePath '\\\\?\\Volume{0123}\\a.md'))\nWrite-Host ('PLAINUNC=' + (ConvertTo-PlainFilePath '\\\\srv\\share\\a\\b.md'))\n")
        out = dict(line.split("=", 1) for line in self.ok(body).splitlines())
        self.assertEqual(out["PLAIN"], out["DEVICE"])
        self.assertEqual((out["UNC"], out["DOTUNC"], out["PLAINUNC"]), ("\\\\srv\\share\\a\\b.md",) * 3)
        self.assertEqual(out["VOLUME"], "\\\\?\\Volume{0123}\\a.md")

    def test_r3_sol_5add23cf_pkg_report_paths_that_are_the_input_a_directory_or_at_the_filesystem_limit(self) -> None:
        # the report-path refusals the packager always had (adversary 1ce472bc A1, 698fffb1 A1, A8; 0e9672e5 A2), run through the real script: the json twin is the input, a junction in front of the report,
        # a directory given as the report; and a report name at the filesystem's limit still publishes.
        repo, run, original = self._pkg_fixture()
        real = self.tmp / "real"
        real.mkdir()
        (real / "summary.json").write_bytes(original)
        link = self.tmp / "link"
        item_type = "Junction" if os.name == "nt" else "SymbolicLink"
        self.ok(f"$null = New-Item -ItemType {item_type} -Path '{link}' -Target '{real}'\n")
        keep = self.tmp / "keep"
        keep.mkdir()
        for label, args in (("twin-is-input", ("-RunRoot", str(run), "-ReportPath", str(run / "summary.md"))),
                            ("junction", ("-RunRoot", str(real), "-ReportPath", str(link / "summary.md"))),
                            ("directory", ("-RunRoot", str(run), "-ReportPath", str(keep)))):
            proc = self._package(repo, *args, "-DryRun")
            self.assertNotEqual(proc.returncode, 0, label + proc.stdout)
            self.assertEqual((run / "summary.json").read_bytes(), original, label)
            self.assertEqual((real / "summary.json").read_bytes(), original, label)
        self.assertTrue(keep.is_dir())
        long_md = run / (("x" * 218) + ".md")
        proc = self._package(repo, "-RunRoot", str(run), "-ReportPath", str(long_md), "-PacketPath", str(self.tmp / "out" / "long.zip"))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(long_md.is_file() and long_md.with_suffix(".json").is_file())
        self.assertEqual(self._private_dirs(), [])

    @requires_windows_pwsh
    def test_r3_sol_5add23cf_a9_a_case_variant_of_the_genuine_report_keeps_speed_authority_on_import(self) -> None:
        # sol 5add23cf A9 (input): -ReportPath C:\Proof\Run\Proof-Report.md, then import on the same case-insensitive volume. The packager lists Proof-Report.json, the importer opened the same file as
        # proof-report.json and verified its hash, and the ordinal spelling check then granted no authority (speedHashBound false).
        repo, run, _ = self._pkg_fixture()
        packet = self.tmp / "out" / "packet.zip"
        proc = self._package(repo, "-RunRoot", str(run), "-ReportPath", str(run / "Proof-Report.md"), "-PacketPath", str(packet))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Proof-Report.json", [e["relativePath"] for e in json.loads(proc.stdout)["files"]])
        imported = self._import(repo, packet)
        self.assertEqual((imported["status"], imported["proofReport"]["speedHashBound"], imported["proofReport"]["speedQuotable"]), ("imported", True, True))

    # ---- R2-RESIDUE-1 r3, second pass (sol 07d50621 A2-A8) ----

    def test_r3_sol_07d50621_a2_a4_every_output_path_is_checked_against_the_input_and_each_other_and_a_directory(self) -> None:
        # sol 07d50621 A2 (inputs): -PacketPath = summary.json; -PacketPath = proof-report.json; -ReportPath ...\local-cuda-proof-packet-manifest.md (its JSON twin is the manifest). A4 (inputs): the packet path
        # or the manifest path is an existing directory (Move-Item put the file inside it and the manifest named the directory). Every one is refused before anything is written, the input stays.
        repo, run, original = self._pkg_fixture()
        keep_packet = self.tmp / "out" / "return.zip"
        keep_packet.mkdir(parents=True)
        manifest_dir = run / "local-cuda-proof-packet-manifest.json"
        cases = {
            "packet-is-the-input": ("-PacketPath", str(run / "summary.json")),
            "packet-is-the-json-report": ("-PacketPath", str(run / "proof-report.json")),
            "report-twin-is-the-manifest": ("-ReportPath", str(run / "local-cuda-proof-packet-manifest.md")),
            "packet-is-a-directory": ("-PacketPath", str(keep_packet)),
        }
        for label, args in cases.items():
            proc = self._package(repo, "-RunRoot", str(run), *args)
            self.assertNotEqual(proc.returncode, 0, label + proc.stdout)
            self.assertEqual((run / "summary.json").read_bytes(), original, label)
            self.assertFalse((run / "proof-report.json").exists(), label)
            self.assertEqual(list(keep_packet.iterdir()), [], label)
        manifest_dir.mkdir()
        proc = self._package(repo, "-RunRoot", str(run), "-PacketPath", str(self.tmp / "out" / "p.zip"))
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(list(manifest_dir.iterdir()), [])
        self.assertEqual(self._private_dirs(), [])

    def test_r3_sol_07d50621_a3_an_output_that_is_the_input_under_a_spelling_no_path_walk_resolves_is_refused(self) -> None:
        # sol 07d50621 A3 (a SUBST drive exposing the run root: -ReportPath S:\summary.md): the path walk cannot resolve it, but the destination EXISTS and holds the input's own bytes, so it is the input. A hard
        # link is the same shape and needs no drive letter.
        repo, run, original = self._pkg_fixture()
        link = run / "summary-link.json"
        self.ok(f"$null = New-Item -ItemType HardLink -Path '{link}' -Target '{run / 'summary.json'}'\n")
        proc = self._package(repo, "-RunRoot", str(run), "-ReportPath", str(run / "summary-link.md"), "-DryRun")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual((run / "summary.json").read_bytes(), original)
        self.assertEqual(link.read_bytes(), original)
        self.assertFalse((run / "summary-link.md").exists())

    def test_r3_sol_07d50621_a8_the_volume_is_asked_how_it_compares_names(self) -> None:
        # sol 07d50621 A8: the comparison followed the path SEPARATOR; the volume is asked instead (an existing file is looked up again with its case flipped)
        probe = self.tmp / "Mixed-Case.json"
        probe.write_text("{}", encoding="utf-8")
        got = self.ok(f"Write-Host ('NAME=' + (Get-VolumeNameComparison -ExistingFile '{probe}'))\n"
                      f"Write-Host ('MISSING=' + (Get-VolumeNameComparison -ExistingFile '{self.tmp / 'Nope.json'}'))\n"
                      f"Write-Host ('NOLETTERS=' + (Get-VolumeNameComparison -ExistingFile '{self.tmp / '0123.json'}'))\n")
        platform_default = "OrdinalIgnoreCase" if os.name == "nt" else "Ordinal"
        asked = "OrdinalIgnoreCase" if (self.tmp / "MIXED-CASE.JSON").exists() else "Ordinal"
        out = dict(line.split("=", 1) for line in got.split())
        self.assertEqual(out, {"NAME": asked, "MISSING": platform_default, "NOLETTERS": platform_default})

    def test_r3_sol_07d50621_a6_a7_a_published_figure_is_exactly_the_producers_and_finite(self) -> None:
        # sol 07d50621 A6 (inputs): legs 1 and 1e308 with a published percent of 10 (the recomputed percent overflows, the tolerance became Infinity and accepted anything); the generic branch with endpoints
        # -1e308 / 1e308 and delta 5. A7 (inputs): legs 100 and 99 with deltaPercent -1.0000000005 (the producer rounds to -1.000, and the reader chose its branch on the submitted value); avgQueueWaitMs 4 -> 5
        # with deltaPercent 24.99999999 (the producer's 25).
        body = (
            "function T($b, $c, $m, $extra) { $leg = { param($f) [pscustomobject]@{ presentedFps = $f; avgRenderTotalMs = $f; avgQueueWaitMs = $f } }; "
            "$cmp = [pscustomobject]@{ presentedFps = [pscustomobject]$m }; if ($extra) { $cmp | Add-Member -NotePropertyName $extra.name -NotePropertyValue ([pscustomobject]$extra.value) }; "
            "$p = Get-FpsCompareBindingProblem -Compare $cmp -Baseline (& $leg $b) -Candidate (& $leg $c); if ($null -eq $p) { 'OK' } else { 'REFUSED' } }\n"
            "Write-Host ('overflow-presented=' + (T 1 1e308 @{ baseline = 1; candidate = 1e308; deltaPercent = 10 } $null))\n"
            "Write-Host ('overflow-generic=' + (T 1 1e308 @{ baseline = 1; candidate = 1; delta = 0; deltaPercent = 0 } @{ name = 'avgRenderTotalMs'; value = @{ baseline = 1; candidate = 1e308; deltaPercent = 10 } }))\n"
            "Write-Host ('overflow-delta=' + (T -1e308 1e308 @{ baseline = 1; candidate = 1; delta = 0; deltaPercent = 0 } @{ name = 'avgRenderTotalMs'; value = @{ baseline = -1e308; candidate = 1e308; delta = 5 } }))\n"
            "Write-Host ('threshold-percent=' + (T 100 99 @{ baseline = 100; candidate = 99; delta = -1; deltaPercent = -1.0000000005 } $null))\n"
            "Write-Host ('threshold-percent-exact=' + (T 100 99 @{ baseline = 100; candidate = 99; delta = -1; deltaPercent = -1 } $null))\n"
            "Write-Host ('stage-percent=' + (T 4 5 @{ baseline = 4; candidate = 5; delta = 1; deltaPercent = 25 } @{ name = 'avgQueueWaitMs'; value = @{ baseline = 4; candidate = 5; delta = 1; deltaPercent = 24.99999999 } }))\n"
            "Write-Host ('stage-percent-exact=' + (T 4 5 @{ baseline = 4; candidate = 5; delta = 1; deltaPercent = 25 } @{ name = 'avgQueueWaitMs'; value = @{ baseline = 4; candidate = 5; delta = 1; deltaPercent = 25 } }))\n"
            "Write-Host ('delta-off-by-a-nano=' + (T 100 99 @{ baseline = 100; candidate = 99; delta = -1.0000000005; deltaPercent = -1 } $null))\n")
        got = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(got, {"overflow-presented": "REFUSED", "overflow-generic": "REFUSED", "overflow-delta": "REFUSED", "threshold-percent": "REFUSED", "threshold-percent-exact": "OK",
                               "stage-percent": "REFUSED", "stage-percent-exact": "OK", "delta-off-by-a-nano": "REFUSED"})

    def test_r3_sol_07d50621_a5_a_compare_not_measured_on_the_judged_pair_publishes_no_figure_of_its_own(self) -> None:
        # sol 07d50621 A5 (input): baseline and candidate both presentedFps 40 / avgRenderTotalMs 10; compare presentedFps 20 -> 60 (+200 %), avgRenderTotalMs 50 -> 25 (-50 %). The summarizer still printed the
        # render total from the rejected compare, and the machine-perf row published 20 and 60 as the legs' own fps.
        text = "\n".join(LocalProofSummarizerFpsGateTests.run_summarizer_block(
            self, "LIGHT", "LIGHT", candidate_fps="40", baseline_fps="40", presented_compare="baseline = 20; candidate = 60; delta = 40; deltaPercent = 200",
            leg_metrics="guiStatusFps = 40; avgPresentIntervalMs = 25; avgRenderTotalMs = 10; ",
            compare_metrics=("guiStatusFps = [pscustomobject]@{ baseline = 40; candidate = 40; delta = 0; deltaPercent = 0 }; "
                             "avgPresentIntervalMs = [pscustomobject]@{ baseline = 25; candidate = 25; delta = 0; deltaPercent = 0 }; "
                             "avgRenderTotalMs = [pscustomobject]@{ baseline = 50; candidate = 25; delta = -25; deltaPercent = -50 }")))
        self.assertIn("fps-verdict-refused:compare-not-judged-pair", text)
        for figure in ("render total: CPU", "CPU 50 ms", "-50%", "presented FPS: CPU", "GUI status FPS: CPU", "avg present interval: CPU"):
            self.assertNotIn(figure, text)
        funcs = _extract_functions(COMPARE_MACHINE_PERF_SCRIPT, ["Convert-ToNullableDouble", "Get-PlaybackAbFpsArmRefusal"])
        source = COMPARE_MACHINE_PERF_SCRIPT.read_text(encoding="utf-8")
        start = source.index("    $fpsArmRefusal = Get-PlaybackAbFpsArmRefusal -Record $Record")
        block = source[start:source.index("    $fpsDeltaRefused = ", start)]
        body = funcs + (
            "$leg = { [pscustomobject]@{ telemetryArm = 'LIGHT'; " + RECORDED_LIGHT_LAUNCH + "; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }; legType = 'speed'; presentedFps = 40; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true } }\n"
            "$Record = [pscustomobject]@{ baseline = (& $leg); candidate = (& $leg); candidateSpeed = $null; speedCompare = $null; compare = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 20; candidate = 60; delta = 40; deltaPercent = 200 } } }\n"
            "$baselinePresented = 20.0; $candidatePresented = 60.0\n" + block +
            "Write-Host ('PER_LEG=' + $baselinePresented + '|' + $candidatePresented + '|' + $fpsArmRefusal.refused)\n")
        self.assertEqual(self.ok(body).split(), ["PER_LEG=40|40|True"])

    def test_r3_sol_5add23cf_a10_compare_machine_perf_binds_every_metric_even_without_presented_fps(self) -> None:
        # the sibling reader (compare-machine-perf.ps1:572 reads the interval metric for its diagnosis) asked only when presentedFps was non-null, so a compare that dropped presentedFps and kept an
        # unbound avgPresentIntervalMs was never asked. A10 input: legs at presentedFps 40 / avgPresentIntervalMs 25, the compare publishes the interval 50 -> 25.
        funcs = _extract_functions(COMPARE_MACHINE_PERF_SCRIPT, ["Get-PlaybackAbFpsArmRefusal"])
        body = funcs + (
            "$leg = { [pscustomobject]@{ telemetryArm = 'LIGHT'; " + RECORDED_LIGHT_LAUNCH + "; generatorArgs = [pscustomobject]@{ telemetryArm = 'LIGHT' }; legType = 'speed'; presentedFps = 40; avgPresentIntervalMs = 25; hostLoadProvisional = $false; hostLoadState = 'quiet'; hostLoadCollected = $true } }\n"
            "$presented = [pscustomobject]@{ baseline = 40; candidate = 40; delta = 0; deltaPercent = 0 }\n"
            "$bound = [pscustomobject]@{ baseline = 25; candidate = 25; delta = 0; deltaPercent = 0 }\n"
            "$unbound = [pscustomobject]@{ baseline = 50; candidate = 25; delta = -25; deltaPercent = -50 }\n"
            "$cases = [ordered]@{ 'with-presented-unbound' = [pscustomobject]@{ presentedFps = $presented; avgPresentIntervalMs = $unbound }; 'without-presented-unbound' = [pscustomobject]@{ avgPresentIntervalMs = $unbound }; "
            "'with-presented-bound' = [pscustomobject]@{ presentedFps = $presented; avgPresentIntervalMs = $bound }; 'without-presented-bound' = [pscustomobject]@{ avgPresentIntervalMs = $bound } }\n"
            "foreach ($k in $cases.Keys) { $r = [pscustomobject]@{ baseline = (& $leg); candidate = (& $leg); candidateSpeed = (& $leg); speedCompare = $cases[$k] }; $v = Get-PlaybackAbFpsArmRefusal -Record $r; Write-Host ($k + '=' + $v.refused + '|' + $v.reason) }\n")
        refused = "fps-verdict-refused:compare-not-judged-pair"
        self.assertEqual(self.ok(body).split(), [f"with-presented-unbound=True|{refused}", f"without-presented-unbound=True|{refused}", "with-presented-bound=False|", "without-presented-bound=False|"])

    # ---- BENCH-LIGHT-ARM-GAIN-GATES-1-R4-DEADLINE-RESIDUE-1 r5 (adversary 82d6f070 A1-A3) ----

    def test_r5_adversary_82d6f070_a1_compare_machine_perf_binds_the_compare_of_a_pair_the_arm_gate_refused(self) -> None:
        # A1 sibling (compare-machine-perf.ps1:520): HEAVY baseline and candidate both presentedFps 40 / avgRenderTotalMs 10, the compare publishes 20 -> 60 and 50 -> 25. The binding sat inside `if ($verdict.allowed)`,
        # so the machine-perf row kept 20 and 60 as the legs' own fps. The typed compare refusal wins (its reason keys the per-leg re-read at New-PlaybackAbSummaryRow); the arm reason rides in the detail.
        funcs = _extract_functions(COMPARE_MACHINE_PERF_SCRIPT, ["Convert-ToNullableDouble", "Get-PlaybackAbFpsArmRefusal"])
        source = COMPARE_MACHINE_PERF_SCRIPT.read_text(encoding="utf-8")
        start = source.index("    $fpsArmRefusal = Get-PlaybackAbFpsArmRefusal -Record $Record")
        block = source[start:source.index("    $fpsDeltaRefused = ", start)]
        body = funcs + (
            "$leg = { param($arm) [pscustomobject]@{ telemetryArm = $arm; presentedFps = 40; avgRenderTotalMs = 10 } }\n"
            "$unbound = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 20; candidate = 60; delta = 40; deltaPercent = 200 }; avgRenderTotalMs = [pscustomobject]@{ baseline = 50; candidate = 25; delta = -25; deltaPercent = -50 } }\n"
            "$bound = [pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 40; candidate = 40; delta = 0; deltaPercent = 0 }; avgRenderTotalMs = [pscustomobject]@{ baseline = 10; candidate = 10; delta = 0; deltaPercent = 0 } }\n"
            "foreach ($k in 'unbound', 'bound') { $c = if ($k -eq 'unbound') { $unbound } else { $bound }; $Record = [pscustomobject]@{ baseline = (& $leg 'HEAVY'); candidate = (& $leg 'HEAVY'); candidateSpeed = $null; speedCompare = $null; compare = $c }\n"
            "    $baselinePresented = $c.presentedFps.baseline; $candidatePresented = $c.presentedFps.candidate\n" + block +
            "    Write-Host ($k + '=' + $fpsArmRefusal.refused + '|' + $fpsArmRefusal.reason + '|' + $fpsArmRefusal.compareUnbound + '|' + $baselinePresented + '|' + $candidatePresented) }\n")
        # the arm reason is kept (the row's playback_fps_arm_verdict says why the pair was refused); the unbound compare sends the per-leg fps back to the legs (40 and 40, not 20 and 60)
        self.assertEqual(self.ok(body).split(), [f"unbound=True|{HEAVY_REASON}|True|40|40", f"bound=True|{HEAVY_REASON}|False|40|40"])

    @requires_windows_pwsh
    def test_r5_adversary_82d6f070_a3_a_twin_with_other_bytes_is_a_second_file_not_the_same_name(self) -> None:
        # A3 (input): a case-sensitive directory holds summary.JSON and a DISTINCT SUMMARY.json ({}). The flipped-case lookup found the twin and answered OrdinalIgnoreCase, so the packager refused the independent
        # report destination SUMMARY.md as the input and the importer counted two report twins. A twin that exists proves nothing alone; only a twin with the same bytes can be the same file. This host's volume
        # ignores case, so the twin's bytes are made to differ by replacing the module's byte comparison for the call.
        probe = self.tmp / "summary.JSON"
        probe.write_text('{"fps":40}', encoding="utf-8")
        got = self.ok(
            f"$m = Get-Module FpsArmGate\n"
            f"Write-Host ('SAME-FILE=' + (Get-VolumeNameComparison -ExistingFile '{probe}'))\n"
            "& $m { Set-Item -Path function:script:Test-FpsFilesHoldTheSameBytes -Value { $false } }\n"
            f"Write-Host ('DISTINCT-TWIN=' + (Get-VolumeNameComparison -ExistingFile '{probe}'))\n")
        self.assertEqual(dict(line.split("=", 1) for line in got.split()), {"SAME-FILE": "OrdinalIgnoreCase", "DISTINCT-TWIN": "Ordinal"})
        # the byte comparison itself: the same file and a copy hold the same bytes; another length and another content do not; a file that cannot be read is answered "the same" (the refusing side)
        other = self.tmp / "other.json"
        other.write_text('{"fps":41}', encoding="utf-8")
        longer = self.tmp / "longer.json"
        longer.write_text('{"fps":40,"x":1}', encoding="utf-8")
        copy = self.tmp / "copy.json"
        copy.write_bytes(probe.read_bytes())
        got = self.ok("".join(f"Write-Host ('{name}=' + (Test-FpsFilesHoldTheSameBytes '{a}' '{b}'))\n" for name, a, b in (
            ("self", probe, probe), ("copy", probe, copy), ("content", probe, other), ("length", probe, longer), ("unreadable", probe, self.tmp / "nope.json"))))
        self.assertEqual(dict(line.split("=", 1) for line in got.split()), {"self": "True", "copy": "True", "content": "False", "length": "False", "unreadable": "True"})

    def test_r5_adversary_82d6f070_a2_output_paths_are_compared_by_the_volume_that_holds_them(self) -> None:
        # A2 (input): the summary sits in a case-sensitive directory (so the packager's comparison was Ordinal) and the outputs in a case-insensitive one: -ReportPath out\Proof.md (its JSON twin Proof.json) and
        # -PacketPath out\proof.json. On the output volume they are one name and the packet replaced the JSON report. Each pair is asked of the volume that holds the pair, not of the input's directory; when
        # neither name exists nothing shows the directory case sensitive, so the pair is refused as one name.
        funcs = _extract_functions(self.PACKAGER, ["ConvertTo-PlainFilePath", "Get-RealFilePath", "Get-BytesSha256", "Get-FileSha256", "Assert-OutputPathsAreSafe"])
        src = self.tmp / "in"
        src.mkdir()
        (src / "summary.json").write_text("{}", encoding="utf-8")
        out = self.tmp / "out"
        out.mkdir()
        body = funcs + (
            f"$s = '{src / 'summary.json'}'; $b = [IO.File]::ReadAllBytes($s)\n"
            "function T($o) { try { Assert-OutputPathsAreSafe -Summary $s -SummaryBytes $b -Outputs $o -Comparison ([StringComparison]::Ordinal); 'OK' } catch { 'REFUSED' } }\n"
            f"Write-Host ('case-only-twins=' + (T ([ordered]@{{ 'JSON report' = '{out / 'Proof.json'}'; 'packet' = '{out / 'proof.json'}' }})))\n"
            f"Write-Host ('distinct-names=' + (T ([ordered]@{{ 'JSON report' = '{out / 'Proof.json'}'; 'packet' = '{out / 'return.zip'}' }})))\n"
            f"Write-Host ('same-name=' + (T ([ordered]@{{ 'JSON report' = '{out / 'proof.json'}'; 'packet' = '{out / 'proof.json'}' }})))\n")
        self.assertEqual(dict(line.split("=", 1) for line in self.ok(body).split()), {"case-only-twins": "REFUSED", "distinct-names": "OK", "same-name": "REFUSED"})
        # the disk-file exclusion and the manifest / packet exclusion of the same run use the same question
        source = self.PACKAGER.read_text(encoding="utf-8")
        self.assertEqual(source.count("[string]::Equals($path, $summaryFull, $Comparison)"), 0)
        self.assertEqual(source.count("[string]::Equals($_.FullName, $manifestPath, $comparison)"), 0)
        self.assertIn("Test-FpsPathAlias $diskPath $manifestPath", source)
        self.assertIn("Test-FpsPathAlias $diskPath $packetFullPath", source)

    @requires_windows_pwsh
    def test_r5_adversary_82d6f070_a3_a_distinct_report_twin_is_not_refused_as_the_input_summary(self) -> None:
        # A3 sibling (package-local-cuda-proof-result.ps1:372/182): summary.JSON and a distinct SUMMARY.json in one case-sensitive directory, -ReportPath SUMMARY.md. The comparison the packager takes from the
        # volume was OrdinalIgnoreCase, so the independent report (its JSON twin SUMMARY.json) was refused as the input. The twin's bytes are made to differ (this host's volume ignores case) and the summary
        # bytes passed are not the file's, so only the name question is asked.
        funcs = _extract_functions(self.PACKAGER, ["ConvertTo-PlainFilePath", "Get-RealFilePath", "Get-BytesSha256", "Get-FileSha256", "Assert-OutputPathsAreSafe"])
        summary = self.tmp / "summary.JSON"
        summary.write_text('{"fps":40}', encoding="utf-8")
        body = funcs + (
            "$m = Get-Module FpsArmGate\n"
            "& $m { Set-Item -Path function:script:Test-FpsFilesHoldTheSameBytes -Value { $false } }\n"
            f"$s = '{summary}'; $b = [byte[]](1, 2, 3)\n"
            f"$cmp = Get-VolumeNameComparison -ExistingFile $s\n"
            f"try {{ Assert-OutputPathsAreSafe -Summary $s -SummaryBytes $b -Outputs ([ordered]@{{ 'Markdown report' = '{self.tmp / 'SUMMARY.md'}'; 'JSON report' = '{self.tmp / 'SUMMARY.json'}'; 'packet' = '{self.tmp / 'out' / 'return.zip'}' }}) -Comparison $cmp; Write-Host 'OUTCOME=OK' }} catch {{ Write-Host ('OUTCOME=REFUSED ' + $_.Exception.Message) }}\n")
        # r6 (adversary d68bc1e8 A1): the packager answers by FILE IDENTITY now. Where this directory ignores case, summary.JSON and SUMMARY.json ARE one file, so the report twin is refused as the
        # input summary (correct); only where the directory is case sensitive are they two files and the twin is accepted. Both outcomes are asserted, never skipped.
        probe_dir = self.tmp / "case-probe"
        probe_dir.mkdir()
        (probe_dir / "a").write_text("x", encoding="utf-8")
        case_insensitive = (probe_dir / "A").exists()
        out = self.ok(body).strip()
        if case_insensitive:
            self.assertTrue(out.startswith("OUTCOME=REFUSED"), out)
        else:
            self.assertEqual(out, "OUTCOME=OK")

    def test_r5_adversary_55683e1f_a1_the_published_figures_follow_from_the_judged_legs_not_a_near_endpoint(self) -> None:
        # adversary 55683e1f A1 (input verbatim): legs presentedFps baseline 100 and candidate 98.99949999; compare {baseline 100, candidate 98.99950004, delta -1.0005, deltaPercent -1}. The endpoint is 5e-10
        # relative from the leg (inside float noise), and the recompute started from THAT endpoint, so the percent came out -1 where the legs give the producer's -1.001. The same substitution in a generic metric.
        body = (
            "$leg = { param($f) [pscustomobject]@{ presentedFps = $f; avgRenderTotalMs = $f } }\n"
            "function T($cmp) { $p = Get-FpsCompareBindingProblem -Compare $cmp -Baseline (& $leg 100) -Candidate (& $leg 98.99949999); if ($null -eq $p) { 'OK' } else { 'REFUSED' } }\n"
            "Write-Host ('presented-near-endpoint=' + (T ([pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 100; candidate = 98.99950004; delta = -1.0005; deltaPercent = -1 } })))\n"
            "Write-Host ('presented-the-legs-own-figures=' + (T ([pscustomobject]@{ presentedFps = [pscustomobject]@{ baseline = 100; candidate = 98.99949999; delta = -1.0005; deltaPercent = -1.001 } })))\n"
            "Write-Host ('generic-near-endpoint=' + (T ([pscustomobject]@{ avgRenderTotalMs = [pscustomobject]@{ baseline = 100; candidate = 98.99950004; delta = -1.0005; deltaPercent = -1 } })))\n"
            "Write-Host ('generic-the-legs-own-figures=' + (T ([pscustomobject]@{ avgRenderTotalMs = [pscustomobject]@{ baseline = 100; candidate = 98.99949999; delta = -1.0005; deltaPercent = -1.001 } })))\n")
        self.assertEqual(dict(line.split("=", 1) for line in self.ok(body).split()), {
            "presented-near-endpoint": "REFUSED", "presented-the-legs-own-figures": "OK", "generic-near-endpoint": "REFUSED", "generic-the-legs-own-figures": "OK"})

    def test_r5_adversary_55683e1f_a2_a_held_output_name_never_hides_a_distinct_existing_file_of_another_case(self) -> None:
        # adversary 55683e1f A2 (input): C:\R is case sensitive, input\summary.json sits in a case-insensitive subdirectory (so the comparison was OrdinalIgnoreCase), C:\R\proof.json is an existing distinct
        # diagnostic and the report is Proof.md (JSON twin Proof.json). Contains('proof.json') matched the held 'Proof.json', so the diagnostic left the ZIP and the manifest. The held names are keyed
        # exactly; whether a disk file is a held output is the volume's answer (Test-FpsPathAlias).
        source = self.PACKAGER.read_text(encoding="utf-8")
        start = source.index("    $heldBytes = [System.Collections.Specialized.OrderedDictionary]::new(")
        line = source[start:source.index("\n", start)]
        body = ("$comparison = [StringComparison]::OrdinalIgnoreCase\n" + line.strip() + "\n"
                "$heldBytes['Proof.json'] = [byte[]](1)\n"
                "Write-Host ('EXACT=' + $heldBytes.Contains('Proof.json'))\nWrite-Host ('OTHER-CASE=' + $heldBytes.Contains('proof.json'))\n")
        self.assertEqual(dict(item.split("=", 1) for item in self.ok(body).split()), {"EXACT": "True", "OTHER-CASE": "False"})
        self.assertIn("(@($heldFullPaths | Where-Object { Test-FpsPathAlias $_ $diskPath }).Count -gt 0)", source)

    # ---- r6 (hub ruling 2026-11-27T13:17Z): ONE identity rule for the packager's paths (volume serial + file id), and the batch deadline read without Get-Variable ----

    # a stand-in for the module's raw identity reader, with a case-SENSITIVE volume R: (R:\r\Run and R:\r\run are two directories) and a drive S: that is a SUBST of R:\r\Run
    _MOCK_VOLUME = (
        "$m = Get-Module FpsArmGate\n"
        "& $m { Set-Item -Path function:script:Read-FpsFileIdentity -Value { param([string]$Path)\n"
        "    $p = $Path.TrimEnd([char]92)\n"
        "    if ($p -ceq 'S:') { $p = 'R:\\r\\Run' } elseif ($p.StartsWith('S:\\', [StringComparison]::Ordinal)) { $p = 'R:\\r\\Run' + $p.Substring(2) }\n"
        "    if ($p -ceq 'locked') { return [pscustomobject]@{ state = 'UNREADABLE'; id = $null; error = 5 } }\n"
        "    if (@('R:', 'R:\\r', 'R:\\r\\Run', 'R:\\r\\run', 'R:\\r\\Run\\sub') -ccontains $p) { return [pscustomobject]@{ state = 'EXISTS'; id = 'ID:' + $p; error = 0 } }\n"
        "    if ($p -like '*locked*') { return [pscustomobject]@{ state = 'UNREADABLE'; id = $null; error = 5 } }\n"
        "    [pscustomobject]@{ state = 'MISSING'; id = $null; error = 2 } } }\n")

    @requires_windows_pwsh
    def test_r6_adversary_d68bc1e8_a1_two_paths_are_one_file_when_volume_serial_and_file_id_agree(self) -> None:
        # r6: Test-FpsPathAlias compared SPELLINGS (case, volume, SUBST, root case: a new spelling every pass). Two existing paths are the same file iff they open to the same volume serial number and file
        # id; a path that does not exist yet is its existing ancestor's identity plus the names below it (compared ignoring case). Real files here: a hard link is the same file, a copy with the same
        # bytes is not, and the answer does not depend on how the path is spelled.
        a = self.tmp / "a.json"
        a.write_text("{}", encoding="utf-8")
        copy = self.tmp / "copy.json"
        copy.write_text("{}", encoding="utf-8")
        link = self.tmp / "link.json"
        (self.tmp / "sub").mkdir()
        cases = [
            ("same-path", a, a, "True"), ("hard-link", a, link, "True"), ("copy-with-the-same-bytes", a, copy, "False"),
            ("dot-dot-spelling", self.tmp / "sub" / ".." / "a.json", a, "True"),
            ("missing-case-twins", self.tmp / "New.json", self.tmp / "new.json", "True"), ("missing-distinct", self.tmp / "x.json", self.tmp / "y.json", "False"),
            ("missing-below-a-missing-directory", self.tmp / "nodir" / "q" / "p.json", self.tmp / "NODIR" / "Q" / "p.json", "True"),
            ("existing-and-missing", a, self.tmp / "other.json", "False"),
        ]
        body = f"$null = New-Item -ItemType HardLink -Path '{link}' -Target '{a}'\n"
        body += "".join(f"Write-Host ('{label}=' + (Test-FpsPathAlias '{x}' '{y}'))\n" for label, x, y, _ in cases)
        got = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertEqual(got, {label: want for label, _, _, want in cases})

    @requires_windows_pwsh
    def test_r6_adversary_d68bc1e8_a1_a_subst_drive_over_the_run_root_is_the_run_root(self) -> None:
        # adversary d68bc1e8 A1 (input): SUBST X: -> the run root; -PacketPath X:\proof-report.json beside the run root's own proof-report.json. Both are fresh names, so only the identity of the directory
        # that will hold them can show they are one file; the ZIP replaced the JSON report after it was published. The drive is made and removed by this test; without `subst` the row is skipped.
        repo, run, original = self._pkg_fixture()
        letter = next((c for c in "ZYXWVUTSRQPONM" if not Path(f"{c}:\\").exists()), None)
        if letter is None or subprocess.run(["subst", f"{letter}:", str(run)], capture_output=True, text=True, creationflags=_NO_WINDOW).returncode != 0:
            self.skipTest("SUBST_UNAVAILABLE: no free drive letter or `subst` cannot map one on this host")
        try:
            proc = self._package(repo, "-RunRoot", str(run), "-PacketPath", f"{letter}:\\proof-report.json")
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertFalse((run / "proof-report.json").exists(), "the JSON report must not be published beside a packet that is the same file")
            self.assertEqual((run / "summary.json").read_bytes(), original)
            self.assertEqual(self._private_dirs(), [])
        finally:
            subprocess.run(["subst", f"{letter}:", "/D"], capture_output=True, text=True, creationflags=_NO_WINDOW)

    @requires_windows_pwsh
    def test_r6_an_identity_that_cannot_be_read_is_a_typed_refusal_never_different(self) -> None:
        body = self._MOCK_VOLUME + (
            "function T($a, $b) { try { [string](Test-FpsPathAlias $a $b) } catch { if ($_.Exception.Message -like '*FPS_PATH_IDENTITY_UNREADABLE*') { 'TYPED-REFUSAL' } else { 'OTHER: ' + $_.Exception.Message } } }\n"
            "Write-Host ('unreadable-first=' + (T 'R:\\r\\Run\\locked.json' 'R:\\r\\Run\\b.json'))\n"
            "Write-Host ('unreadable-second=' + (T 'R:\\r\\Run\\b.json' 'R:\\r\\Run\\locked.json'))\n"
            "Write-Host ('readable=' + (T 'R:\\r\\Run\\a.json' 'R:\\r\\Run\\b.json'))\n")
        self.assertEqual(dict(line.split("=", 1) for line in self.ok(body).split()),
                         {"unreadable-first": "TYPED-REFUSAL", "unreadable-second": "TYPED-REFUSAL", "readable": "False"})

    @requires_windows_pwsh
    def test_r6_adversary_d68bc1e8_a2_held_names_are_keyed_by_the_path_below_the_identity_resolved_run_root(self) -> None:
        # adversary d68bc1e8 A2 (input): case-sensitive C:\review holds distinct Run and run; -RunRoot C:\review\Run -SummaryPath C:\review\run\summary.json. GetRelativePath collapsed the root's case, so the
        # external summary was keyed summary.json and the distinct Run\summary.json vanished from the packet. The relative name is taken under the ancestor that IS the run root (by identity); a path with no
        # such ancestor is outside the run root and holds no name in it. The stand-in volume is case sensitive and S: is a SUBST of R:\r\Run.
        body = self._MOCK_VOLUME + (
            "function T($root, $path) { $r = Get-FpsRelativeUnderRoot -Root $root -Path $path; if ($null -eq $r) { '<outside>' } else { $r } }\n"
            "Write-Host ('other-case-root=' + (T 'R:\\r\\Run' 'R:\\r\\run\\summary.json'))\n"
            "Write-Host ('own-root=' + (T 'R:\\r\\Run' 'R:\\r\\Run\\summary.json'))\n"
            "Write-Host ('below-a-subdirectory=' + (T 'R:\\r\\Run' 'R:\\r\\Run\\sub\\x.json'))\n"
            "Write-Host ('through-the-subst-drive=' + (T 'R:\\r\\Run' 'S:\\x.json'))\n"
            "Write-Host ('root-itself=' + (T 'R:\\r\\Run' 'R:\\r\\Run'))\n"
            "Write-Host ('elsewhere=' + (T 'R:\\r\\Run' 'R:\\other\\summary.json'))\n")
        self.assertEqual(dict(line.split("=", 1) for line in self.ok(body).split()), {
            "other-case-root": "<outside>", "own-root": "summary.json", "below-a-subdirectory": "sub/x.json", "through-the-subst-drive": "x.json", "root-itself": "<outside>", "elsewhere": "<outside>"})
        source = self.PACKAGER.read_text(encoding="utf-8")
        self.assertIn("Get-FpsRelativeUnderRoot -Root $runRootResolved -Path", source)
        self.assertNotIn("[System.IO.Path]::GetRelativePath($runRootResolved", source)

    @requires_pwsh
    def test_r6_adversary_d68bc1e8_a3_the_spec_read_bound_ignores_a_get_variable_default_parameter_redirect(self) -> None:
        # adversary d68bc1e8 A3 (input): a caller's $PSDefaultParameterValues['Get-Variable:Scope']='Global' sent Get-Variable's lookup of the batch deadline to the global scope, which has none, so the spec read got
        # the 120 s cap instead of the time left and SPEC_READ_TIMEOUT could land after -DeadlineUtc. The deadline is read from the script scope directly.
        funcs = _extract_functions(LEASE_BATCH_SCRIPT, ["Get-SpecReadBoundSec"])
        body = funcs + (
            "$PSDefaultParameterValues['Get-Variable:Scope'] = 'Global'\n"
            "$deadline = [datetime]::UtcNow.AddSeconds(30)\n"
            "Write-Host ('BOUND=' + [Math]::Round((Get-SpecReadBoundSec), 0))\n"
            "Remove-Variable deadline -Scope Script\n"
            "Write-Host ('NO-DEADLINE=' + (Get-SpecReadBoundSec))\n")
        got = dict(line.split("=", 1) for line in self.ok(body).split())
        self.assertIn(int(float(got["BOUND"])), (27, 28), got)
        self.assertEqual(float(got["NO-DEADLINE"]), 120.0, got)
        self.assertNotIn("Get-Variable -Name deadline", LEASE_BATCH_SCRIPT.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
