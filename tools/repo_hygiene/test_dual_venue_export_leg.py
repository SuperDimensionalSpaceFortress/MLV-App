"""EXPORT-VENUE-LEG-1: the dual-venue chain's `export` leg type (CPU per-stage export shares, CDNG lossless and ProRes).

What is pinned here:
  * the schema and the runner's shape refusal (one predicate for the leg and the lease batch's preflight) admit the four committed
    export legs and refuse every malformed export spec, typed;
  * Get-DvLegSetPlan gives an export leg its own cell id per format, so two export legs of one clip never collide;
  * Resolve-DvJobOutcome maps every export job token (EXPORT_CAPTURED with a non-zero exit stays INVALID in the runner);
  * Get-DvExportEvidence, the pure reader of the published profile / stage-timing log / batch log / output manifest, over hand-written
    TEXT (no footage): the metrics, the per-stage shares, frames short -> FAIL, a missing profile -> INVALID, a dual-ISO class that
    disagrees with the leg -> INVALID CLIP_CLASS_MISMATCH;
  * offline end to end: Invoke-VenueLeg.ps1 -OfflineTestMode with a stub generator and a stub um-run returning each export token; a
    timeout / no-frames / profile-absent receipt is never PASS, and the happy receipt verifies (VERIFIED_OFFLINE_TEST);
  * mutation: without the export branch of Test-DvReceiptValid the happy receipt no longer verifies;
  * the export job generator (tools/profiling/bachelor/export-stage-job.ps1): every AttrCuda / GuiSmoke command its template calls is
    embedded, and it reuses the playback generator's resolver and the length gate instead of copying them.
No test launches MLVApp and no test opens a clip (owner rule 2026-10-11).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import (
    DV,
    ROOT,
    RunnerHarness,
    requires_windows_pwsh,
    run_pwsh,
    run_pwsh_json,
)

CARD = "EXPORT-VENUE-LEG-1"
EXPORT_LEGS = ("m16-1243-export-cdng-lossless", "m16-1243-export-prores", "m16-1456-export-cdng-lossless", "m16-1456-export-prores")
RECEIPTS = {"dualiso": "look-receipts/export-owner-hq-dualiso.marxml", "nodualiso": "look-receipts/export-owner-hq-nodualiso.marxml"}
EXPORT_JOB = ROOT / "tools" / "profiling" / "bachelor" / "export-stage-job.ps1"
BUILD_ID = "e" * 40
# r11 (fable r9 B1(a)): the measured dual-ISO class of each clip an export leg ships on. M16-1243: 480 of 480 frames ran dual-ISO (receipts 638671c5, 0fc26d37, 2fcd3cf4, 64185ab8, dbd2fc28).
# M16-1456: 360 of 360 frames ran dual-ISO on the three Bachelor receipts f23fa981, 1e9153b6, 01905cfe (each INVALID CLIP_CLASS_MISMATCH against the first declaration, false).
KNOWN_CLIP_DUAL_ISO_CLASS = {"M16-1243": True, "M16-1456": True}


def leg(name: str) -> dict:
    return json.loads((DV / "legs" / f"{name}.json").read_text(encoding="utf-8"))


# --- fixture TEXT (hand-written; never real footage, no clip path) -------------------------------------------------------------
CDNG_STAGES = ("raw_read_ms", "raw_decode_ms", "raw_unpack_ms", "llrawproc_ms", "llrawproc_dark_frame_ms", "llrawproc_vertical_stripes_ms",
               "llrawproc_focus_pixels_ms", "llrawproc_bad_pixels_ms", "llrawproc_pattern_noise_ms", "llrawproc_pre_dualiso_fix_ms",
               "llrawproc_dual_iso_ms", "llrawproc_chroma_smooth_ms", "llrawproc_shared_lock_ms", "llrawproc_dualiso_refine_lock_ms",
               "llrawproc_publish_lock_ms", "llrawproc_other_ms", "dng_header_ms", "dng_pack_ms", "dng_compress_encode_ms", "payload_clone_ms",
               "disk_write_ms", "writer_queue_wait_ms", "writer_completion_lag_ms", "producer_frame_ms")


def cdng_profile(frames: int, dual_iso_ms: float = 30.0, success: bool = True, build_id: str = BUILD_ID, failed: int = 0) -> str:
    rows = []
    for i in range(frames):
        stage = {k: 1.0 for k in CDNG_STAGES}
        stage["raw_read_ms"] = 4.0
        stage["llrawproc_dual_iso_ms"] = dual_iso_ms
        stage["llrawproc_ms"] = 40.0   # r8: the llrawproc total contains its llrawproc_* children (the 30 ms dual-ISO one included)
        stage["dng_compress_encode_ms"] = 10.0
        stage["producer_frame_ms"] = 62.0 + (i % 3)   # r11: the 19 producer stages below total 60 ms, so producer_frame (which contains them all) is at least that
        row = {"clip": "view-0", "frame_index": i, "raw_input_state": "x", "raw_output_state": "y", "success": success and i >= failed}
        row.update(stage)
        rows.append(row)
    return json.dumps({"schema": "mlvapp.export_stage_profile.v1", "build_id": build_id, "frame_count": frames,
                       "dng_compress_placement": "producer", "async_writer_can_overlap_dng_compress": False, "stages": {}, "frames": rows})


def stage_log(frames: int) -> str:
    out = []
    for i in range(frames):
        for name, ms in (("raw_uint16_disk_read", 3.0), ("raw_uint16_decompress", 5.0), ("raw_uint16", 9.0), ("llrawproc", 20.0),
                         ("debayered_frame", 40.0), ("processing", 15.0), ("processed16_total", 56.0)):
            out.append(f"[mlv-stage-timing] frame={i} stage={name} ms={ms:.3f}")
    return "\n".join(out) + "\n"


def batch_log(exported: int, skipped: int = 0, elapsed: float = 20.0, verified: int | None = None) -> str:
    lines = ["[BATCH] Start", f"[BATCH] DONE view-0 exported={exported} skipped={skipped} elapsed={elapsed:.1f}"]
    if verified is not None:
        lines.insert(1, f"[BATCH] RENDER_VERIFIED view-0 decoded_frames={verified} decoded_duration=10.000 decoded_frame_bytes=1 dimensions=1x1 bytes=9 "
                        "container_probe=ffmpeg.exe probe_source=ffmpeg-i codec=prores container=mov sar=1:1 dar=16:9")
    return "\n".join(lines) + "\n"


def output_manifest(count: int, kind: str = "dng") -> str:
    return json.dumps({"schema": "export-stage-output-manifest.v1", "kind": kind, "count": count, "totalBytes": count * 1000,
                       "first": {"name": "a", "sha256": "1" * 64}, "last": {"name": "b", "sha256": "2" * 64}, "deleted": True})


EVIDENCE_CALL = ("Get-DvExportEvidence -Format $c.format -MaxFrames ([int]$c.maxFrames) -DualIsoExpected ([bool]$c.dualIsoExpected) -ExpectedBuildId $c.buildId "
                 "-ProfileText ([string]$c.profile) -StageLogText ([string]$c.stage) -BatchLogText ([string]$c.batch) -OutputManifestText ([string]$c.manifest)")


def evidence_cases(cases: list[dict]):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cases.json"
        path.write_text(json.dumps(cases), encoding="utf-8")
        return run_pwsh_json("$cases = Get-Content -Raw -LiteralPath $env:DVE_CASES -Encoding utf8 | ConvertFrom-Json\n"
                             f"@($cases | ForEach-Object {{ $c = $_; $e = {EVIDENCE_CALL}; "
                             "$o = Get-DvExportOutcome -ResultToken $c.token -ExitCode ([int]$c.exit) -Evidence $e -Criteria @($c.criteria); "
                             "[ordered]@{ metrics = $e.metrics; problems = @($e.problems); classMismatch = $e.classMismatch; stages = $e.stages; outcome = $o.outcome; detail = $o.detail } }) "
                             "| ConvertTo-Json -Compress -Depth 8 -AsArray", DVE_CASES=str(path))


def cdng_case(**over) -> dict:
    c = {"format": "cdng-lossless", "maxFrames": 30, "dualIsoExpected": True, "buildId": BUILD_ID, "profile": cdng_profile(30), "stage": None,
         "batch": batch_log(30), "manifest": output_manifest(30), "token": "EXPORT_CAPTURED", "exit": 0,
         "criteria": [{"metric": "framesExported", "op": "eq", "value": 30}, {"metric": "exportFramesFailed", "op": "eq", "value": 0},
                      {"metric": "profileFramesSucceeded", "op": "eq", "value": 30}, {"metric": "dualIsoClassAgrees", "op": "eq", "value": True}]}
    c.update(over)
    return c


def prores_case(**over) -> dict:
    c = {"format": "prores", "maxFrames": 30, "dualIsoExpected": False, "buildId": BUILD_ID, "profile": None, "stage": stage_log(30),
         "batch": batch_log(30, verified=30), "manifest": output_manifest(1, "mov"), "token": "EXPORT_CAPTURED", "exit": 0,
         "criteria": [{"metric": "framesExported", "op": "eq", "value": 30}, {"metric": "exportFramesFailed", "op": "eq", "value": 0},
                      {"metric": "stageFramesSeen", "op": "eq", "value": 30}]}
    c.update(over)
    return c


class ExportLegSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        self.jsonschema = jsonschema
        self.schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))

    def bad(self, spec: dict) -> None:
        with self.assertRaises(self.jsonschema.ValidationError, msg=json.dumps(spec)[:400]):
            self.jsonschema.validate(spec, self.schema)

    def test_the_four_committed_export_legs_validate_and_bind_their_receipt(self) -> None:
        self.assertEqual(self.schema["properties"]["legType"]["enum"], ["speed", "look", "export"])
        for name in EXPORT_LEGS:
            spec = leg(name)
            self.jsonschema.validate(spec, self.schema)
            self.assertEqual((spec["card"], spec["legType"], spec["backends"], spec["scaleFactor"]), (CARD, "export", ["cpu"], 1), name)
            self.assertEqual(spec["timeouts"], {"queueWaitSec": 1800, "extraClaimedWaitSec": 2400}, name)
            self.assertEqual(spec["generatorArgs"], {"cpuQuiescenceThresholdPercent": 20}, name)
            ex = spec["export"]
            # (the committed blob is LF; an autocrlf checkout may hold CRLF: the runner reads the committed bytes)
            self.assertEqual(hashlib.sha256((DV / ex["receipt"]).read_bytes().replace(b"\r\n", b"\n")).hexdigest(), ex["receiptSha256"], name)
            self.assertEqual(spec["criteria"]["acceptance"], spec["criteria"]["supplementary"], name)
        self.assertEqual([(leg(n)["clipId"], leg(n)["playSeconds"], leg(n)["export"]["maxFrames"], leg(n)["export"]["dualIsoExpected"], leg(n)["export"]["format"])
                          for n in EXPORT_LEGS],
                         [("M16-1243", 20, 480, True, "cdng-lossless"), ("M16-1243", 20, 480, True, "prores"),
                          ("M16-1456", 10, 360, True, "cdng-lossless"), ("M16-1456", 10, 360, True, "prores")])
        # r11 (fable r9 B1(a)): each leg's declared class is the clip's MEASURED class, never a per-leg guess: one table, every export leg against it
        for name in EXPORT_LEGS:
            spec = leg(name)
            self.assertIn(spec["clipId"], KNOWN_CLIP_DUAL_ISO_CLASS, f"{name}: no measured dual-ISO class for {spec['clipId']}; measure it before shipping a leg")
            self.assertIs(spec["export"]["dualIsoExpected"], KNOWN_CLIP_DUAL_ISO_CLASS[spec["clipId"]], f"{name}: declared class differs from the clip's measured class")
        self.assertEqual(leg(EXPORT_LEGS[0])["export"]["receipt"], RECEIPTS["dualiso"])
        self.assertEqual(leg(EXPORT_LEGS[2])["export"]["receipt"], RECEIPTS["nodualiso"])

    def test_the_schema_refuses_every_malformed_export_spec(self) -> None:
        base = leg(EXPORT_LEGS[0])
        cases = {
            "no export block": lambda s: s.pop("export"),
            "cuda backend": lambda s: s.__setitem__("backends", ["cuda"]),
            "both backends": lambda s: s.__setitem__("backends", ["cpu", "cuda"]),
            "scale 2": lambda s: s.__setitem__("scaleFactor", 2),
            "displayMode": lambda s: s.__setitem__("displayMode", "windowed"),
            "look": lambda s: s.__setitem__("look", {"contactSheetFrames": 2, "lookFlavor": "classic"}),
            "visualCapture": lambda s: s.__setitem__("visualCapture", True),
            "a playback generator arg": lambda s: s["generatorArgs"].__setitem__("unpaced", True),
            "format h264": lambda s: s["export"].__setitem__("format", "h264"),
            "receipt outside look-receipts": lambda s: s["export"].__setitem__("receipt", "receipts/FastProxy.marxml"),
            "maxFrames 23": lambda s: s["export"].__setitem__("maxFrames", 23),
            "unknown export key": lambda s: s["export"].__setitem__("resize", 2),
        }
        for name, mutate in cases.items():
            with self.subTest(case=name):
                spec = json.loads(json.dumps(base))
                mutate(spec)
                self.bad(spec)
        speed = json.loads(json.dumps(leg("m16-1243-look")))
        speed["export"] = base["export"]
        self.bad(speed)

    def test_the_description_says_play_seconds_is_the_length_floor_and_max_frames_the_window(self) -> None:
        text = self.schema["properties"]["export"]["description"]
        self.assertIn("playSeconds", text)
        self.assertIn("maxFrames", text)
        self.assertIn("length gate", text)


@requires_windows_pwsh
class ExportShapeRefusalTests(unittest.TestCase):
    def test_the_shape_refusal_admits_the_legs_and_refuses_each_bad_shape_typed(self) -> None:
        base = leg(EXPORT_LEGS[1])

        def mut(f):
            s = json.loads(json.dumps(base))
            f(s)
            return s
        speed = json.loads(json.dumps(leg("m16-1243-look")))
        speed["legType"] = "speed"
        speed.pop("look")
        speed["export"] = base["export"]
        cases = [("ok-" + n, leg(n), None) for n in EXPORT_LEGS] + [
            ("no-export", mut(lambda s: s.pop("export")), "export"),
            ("cuda", mut(lambda s: s.__setitem__("backends", ["cuda"])), "backends"),
            ("scale2", mut(lambda s: s.__setitem__("scaleFactor", 2)), "scaleFactor"),
            ("display", mut(lambda s: s.__setitem__("displayMode", "fullscreen")), "displayMode"),
            ("look", mut(lambda s: s.__setitem__("look", {"contactSheetFrames": 2, "lookFlavor": "classic"})), "look"),
            ("visual", mut(lambda s: s.__setitem__("visualCapture", False)), "visualCapture"),
            ("genarg", mut(lambda s: s["generatorArgs"].__setitem__("telemetryArm", "LIGHT")), "generatorArgs.telemetryArm"),
            ("h264", mut(lambda s: s["export"].__setitem__("format", "h264")), "export.format"),
            ("receipt", mut(lambda s: s["export"].__setitem__("receipt", "../receipts/x.marxml")), "export.receipt"),
            ("frames-float", mut(lambda s: s["export"].__setitem__("maxFrames", 480.5)), "export.maxFrames"),
            ("dualiso-string", mut(lambda s: s["export"].__setitem__("dualIsoExpected", "true")), "export.dualIsoExpected"),
            ("export-array", mut(lambda s: s.__setitem__("export", [base["export"]])), "export must be a single JSON object"),
            ("speed-with-export", speed, "export"),
        ]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            path.write_text(json.dumps([{"name": n, "spec": s} for n, s, _ in cases]), encoding="utf-8")
            proc, got = run_pwsh_json("$cases = Get-Content -Raw -LiteralPath $env:C -Encoding utf8 | ConvertFrom-Json\n"
                                      "@($cases | ForEach-Object { [ordered]@{ name = $_.name; r = (Get-DvLegSpecShapeRefusal -Spec $_.spec) } }) | ConvertTo-Json -Compress -AsArray", C=str(path))
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        self.assertEqual(len(got), len(cases))
        for (name, _spec, want), row in zip(cases, got):
            with self.subTest(case=name):
                if want is None:
                    self.assertIsNone(row["r"], row)
                else:
                    self.assertIsNotNone(row["r"], name)
                    self.assertIn(want, row["r"])


@requires_windows_pwsh
class ExportPlanAndOutcomeTests(unittest.TestCase):
    def test_two_export_formats_of_one_clip_get_distinct_cells(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "legs").mkdir()
            (root / "legsets").mkdir()
            for n in EXPORT_LEGS[:2]:
                shutil.copyfile(DV / "legs" / f"{n}.json", root / "legs" / f"{n}.json")
            (root / "legsets" / "x.json").write_text(json.dumps({"schema": "mlv-app/dual-venue-legset/v1", "legSet": "export-x", "card": CARD, "clipId": "M16-1243",
                                                                 "legs": [f"legs/{n}.json" for n in EXPORT_LEGS[:2]]}), encoding="utf-8")
            proc, got = run_pwsh_json(f"@((Get-DvLegSetPlan -LegSetPath '{root / 'legsets' / 'x.json'}' -Repeats 1).plan | ForEach-Object {{ $_.cellId }}) | ConvertTo-Json -Compress -AsArray")
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        self.assertEqual(sorted(got), ["cpu-export-cdng-lossless", "cpu-export-prores"], proc.stdout + proc.stderr)

    def test_every_export_token_maps_to_its_outcome(self) -> None:
        want = {"EXPORT_CAPTURED": "CAPTURED", "EXPORT_EXIT_NONZERO": "FAIL", "EXPORT_TIMEOUT": "FAIL", "EXPORT_NO_FRAMES": "FAIL",
                "EXPORT_FRAMES_SHORT": "FAIL", "EXPORT_PROFILE_ABSENT": "INVALID", "EXPORT_OUTPUT_MISMATCH": "INVALID",
                "EXPORT_TOOLING_ABSENT": "VENUE_TOOLING", "VENUE_NOT_QUIESCENT": "VENUE_NOT_QUIESCENT", "VENUE_HOST_MISMATCH": "VENUE_HOST_MISMATCH"}
        script = "$o = [ordered]@{}\n" + "".join(f"$o['{t}'] = (Resolve-DvJobOutcome -ResultToken '{t}' -ExitCode 7).outcome\n" for t in want) + "$o | ConvertTo-Json -Compress"
        proc, got = run_pwsh_json(script)
        self.assertEqual(got, want, proc.stdout + proc.stderr)
        proc, got = run_pwsh_json("(Resolve-DvJobOutcome -ResultToken 'EXPORT_TIMEOUT' -ExitCode 40).detail | ConvertTo-Json")
        self.assertIn("EXPORT_TIMEOUT", got or "", proc.stdout + proc.stderr)

    def test_a_length_refusal_or_unknown_token_is_invalid_for_an_export_leg(self) -> None:
        want = {"CLIP_TOO_SHORT": "INVALID", "CLIP_LENGTH_UNKNOWN": "INVALID", "OWNER_FOOTAGE_NOT_VERIFIED": "INVALID", "OWNER_WORK_NOT_CLEAN": "INVALID",
                "": "INVALID", "SOMETHING_ELSE": "INVALID", "EXPORT_TIMEOUT": "FAIL", "EXPORT_FRAMES_SHORT": "FAIL", "VENUE_NOT_QUIESCENT": "VENUE_NOT_QUIESCENT"}
        script = ("$e = [pscustomobject]@{ metrics = [pscustomobject]@{}; stages = $null; problems = @(); classMismatch = $false; classDetail = $null }\n$o = [ordered]@{}\n"
                  + "".join(f"$o['{t}'] = (Get-DvExportOutcome -ResultToken '{t}' -ExitCode 47 -Evidence $e -Criteria @()).outcome\n" for t in want)
                  + "$o | ConvertTo-Json -Compress")
        proc, got = run_pwsh_json(script)
        self.assertEqual(got, want, proc.stdout + proc.stderr)
        proc, got = run_pwsh_json("$e = [pscustomobject]@{ metrics = [pscustomobject]@{}; stages = $null; problems = @(); classMismatch = $false; classDetail = $null }\n"
                                  "(Get-DvExportOutcome -ResultToken 'CLIP_TOO_SHORT' -ExitCode 47 -Evidence $e -Criteria @()).detail | ConvertTo-Json")
        self.assertIn("length gate", got or "", proc.stdout + proc.stderr)

    def test_an_export_leg_never_runs_window_capture(self) -> None:
        proc, got = run_pwsh_json(f"Get-DvVisualCaptureRequest -Spec (Get-Content -Raw '{DV / 'legs' / (EXPORT_LEGS[0] + '.json')}' | ConvertFrom-Json) | ConvertTo-Json -Compress")
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        self.assertEqual((got["applies"], got["requested"]), (False, False), proc.stdout + proc.stderr)


MALFORMED_DECODED_FRAME_TOKENS = ("480.5", "480-1", "480/2", "480x", "480e0")


@requires_windows_pwsh
class ExportEvidenceTests(unittest.TestCase):
    def test_r15_pass14_a2_a_decoded_frame_token_that_is_not_a_whole_integer_verifies_nothing(self) -> None:
        """r13 adversary pass 14 A2: ``decoded_frames=480.5`` was read as 480 (the regex ended in a word boundary, which a '.' also satisfies), so a ProRes batch that
        verified a malformed count passed against 480 exported frames. The reader (Get-DvExportEvidence) and the emitted job (export-stage-job.ps1:345, which types the
        result from the same line) both end the token at whitespace or the end of the line, so such a line is no RENDER_VERIFIED line at all."""
        def prores_480(token) -> dict:
            return prores_case(maxFrames=480, stage=stage_log(480), batch=batch_log(480, verified=token, elapsed=60.0), criteria=[])
        proc, got = evidence_cases([prores_480(480)] + [prores_480(t) for t in MALFORMED_DECODED_FRAME_TOKENS])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual((got[0]["problems"], got[0]["outcome"]), ([], "PASS"), "the control: a whole integer token verifies")
        for token, row in zip(MALFORMED_DECODED_FRAME_TOKENS, got[1:]):
            with self.subTest(reader_token=token):
                self.assertTrue(any("RENDER_VERIFIED" in p for p in row["problems"]), row)
                self.assertNotEqual(row["outcome"], "PASS", row)
        # the emitted job's own pattern, taken from its source and run over the same lines
        job_pattern = re.search(r"'(\(\?m\)\^\\\[BATCH\\\] RENDER_VERIFIED[^']*)'", EXPORT_JOB.read_text(encoding="utf-8"))
        self.assertIsNotNone(job_pattern, "export-stage-job.ps1 no longer carries the RENDER_VERIFIED pattern this test reads")
        lines = [batch_log(480, verified=t) for t in (480,) + MALFORMED_DECODED_FRAME_TOKENS]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "lines.json"
            path.write_text(json.dumps(lines), encoding="utf-8")
            proc, counts = run_pwsh_json("$p = $env:DVE_PATTERN\n$lines = Get-Content -Raw -LiteralPath $env:DVE_LINES -Encoding utf8 | ConvertFrom-Json\n"
                                         "@($lines | ForEach-Object { @([regex]::Matches([string]$_, $p)).Count }) | ConvertTo-Json -Compress -AsArray",
                                         DVE_PATTERN=job_pattern.group(1), DVE_LINES=str(path))
        self.assertIsNotNone(counts, proc.stdout[-2000:] + proc.stderr[-2000:])
        self.assertEqual(counts, [1] + [0] * len(MALFORMED_DECODED_FRAME_TOKENS), "the job reads a malformed decoded_frames token as a verification")

    def test_the_pure_reader_over_fixture_text(self) -> None:
        cases = [
            cdng_case(),                                                              # 0 happy CDNG
            cdng_case(batch=batch_log(24), manifest=output_manifest(24), profile=cdng_profile(24)),   # 1 frames short
            cdng_case(profile=None),                                                  # 2 profile absent
            cdng_case(dualIsoExpected=False),                                         # 3 dual-ISO frames on a no-dual-ISO leg
            prores_case(),                                                            # 4 happy ProRes
            prores_case(stage=None),                                                  # 5 stage log absent
            cdng_case(token="EXPORT_CAPTURED", exit=1),                               # 6 captured, exit 1
            cdng_case(token="EXPORT_TIMEOUT", exit=40),                               # 7 timeout
            cdng_case(manifest=output_manifest(29)),                                  # 8 output count disagrees
            cdng_case(profile=cdng_profile(30, build_id="f" * 40)),                   # 9 another build's profile
            cdng_case(dualIsoExpected=True, profile=cdng_profile(30, dual_iso_ms=0.0)),   # 10 no dual-ISO frames on a dual-ISO leg
            prores_case(batch=batch_log(30, verified=29)),                            # 11 ProRes verified frames disagree
            cdng_case(manifest="null"),                                               # 12 adversary r1 A2: a JSON-null output manifest
            cdng_case(profile="null"),                                                # 13 adversary r1 A2: a JSON-null CDNG profile
            prores_case(manifest="[]"),                                               # 14 adversary r1 A2: a manifest that is no object
        ]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        happy = got[0]
        self.assertEqual(happy["outcome"], "PASS", happy)
        m = happy["metrics"]
        self.assertEqual((m["framesRequested"], m["framesExported"], m["exportFramesFailed"], m["profileFramesSucceeded"], m["dualIsoActiveFrames"],
                          m["dualIsoClassAgrees"], m["outputFileCount"], m["outputBytes"]), (30, 30, 0, 30, 30, True, 30, 30000))
        self.assertAlmostEqual(m["wallSeconds"], 20.0)
        self.assertAlmostEqual(m["exportFps"], 1.5)
        st = happy["stages"]
        self.assertEqual(st["denominator"], "producer_frame_ms")
        self.assertAlmostEqual(st["rows"]["raw_read"]["medianMs"], 4.0)
        self.assertAlmostEqual(st["rows"]["raw_read"]["share"], 4.0 / 63.0, places=4)   # (producer_frame mean: 62 + the 0 / 1 / 2 cycle)
        self.assertAlmostEqual(st["rows"]["llrawproc_dual_iso"]["meanMs"], 30.0)
        self.assertIn("disk_write", st["writerRows"])
        self.assertNotIn("disk_write", st["rows"])
        self.assertEqual(st["largestStage"], "llrawproc_dual_iso")
        self.assertEqual(st["dngCompressPlacement"], "producer")
        self.assertIs(st["asyncWriterCanOverlapDngCompress"], False)
        self.assertEqual(got[1]["outcome"], "FAIL", got[1])
        self.assertEqual(got[2]["outcome"], "INVALID", got[2])
        self.assertTrue(any("EXPORT_PROFILE_ABSENT" in p for p in got[2]["problems"]), got[2])
        self.assertEqual(got[3]["outcome"], "INVALID", got[3])
        self.assertIn("CLIP_CLASS_MISMATCH", got[3]["detail"])
        self.assertTrue(got[3]["classMismatch"])
        pr = got[4]
        self.assertEqual(pr["outcome"], "PASS", pr)
        self.assertEqual((pr["metrics"]["stageFramesSeen"], pr["metrics"]["framesExported"], pr["metrics"]["outputFileCount"]), (30, 30, 1))
        self.assertIsNone(pr["metrics"]["dualIsoActiveFrames"])
        ps = pr["stages"]
        # adversary r1 A4: the batch's DONE elapsed also times clip open, finish and verify, so it is never the share denominator; the shares are of the
        # timed per-frame work (processed16_total), and what the wall adds is reported apart, untimed, never as a stage
        self.assertEqual(ps["denominator"], "processed16_total")
        self.assertAlmostEqual(ps["denominatorMeanMs"], 56.0)
        self.assertNotIn("residual", ps["rows"])
        self.assertAlmostEqual(ps["rows"]["debayered_frame"]["share"], 40.0 / 56.0, places=4)
        self.assertAlmostEqual(ps["batchWallPerFrameMs"], 20000.0 / 30.0, places=3)
        self.assertAlmostEqual(ps["untimedPerFrameMs"], 20000.0 / 30.0 - 56.0, places=3)
        self.assertIn("EXPORT-RENDER-PIPE-TIMING-1", ps["untimed"])
        self.assertAlmostEqual(ps["disjointSumMs"], 55.0)
        self.assertTrue(ps["disjointWithin5Percent"])
        self.assertEqual(ps["largestStage"], "debayered_frame")
        self.assertEqual(pr["metrics"]["largestStage"], "debayered_frame")
        self.assertEqual(got[5]["outcome"], "INVALID", got[5])
        self.assertEqual(got[6]["outcome"], "INVALID", got[6])
        self.assertEqual(got[7]["outcome"], "FAIL", got[7])
        self.assertIn("EXPORT_TIMEOUT", got[7]["detail"])
        for i in (8, 9, 10, 11):
            self.assertEqual(got[i]["outcome"], "INVALID", got[i])
        self.assertIn("CLIP_CLASS_MISMATCH", got[10]["detail"])
        for i, token in ((12, "EXPORT_OUTPUT_MISMATCH"), (13, "EXPORT_PROFILE_ABSENT"), (14, "EXPORT_OUTPUT_MISMATCH")):
            self.assertEqual(got[i]["outcome"], "INVALID", got[i])
            self.assertTrue(any(token in p for p in got[i]["problems"]), got[i])

    def test_r6_adversary_a1_a2_disjoint_windows_and_unusable_timings_are_invalid_whatever_the_criteria(self) -> None:
        """adversary r6 A1/A2 (on f87b33f3): equal SAMPLE COUNTS hid disjoint frame windows (ProRes children on frames 30..59 against processed16_total on 0..29),
        a profile whose every frame failed, a negative or object-valued timing and a one-of-N writer population all read PASS under empty criteria. Each is INVALID."""
        def profile(mutate) -> str:
            doc = json.loads(cdng_profile(30))
            mutate(doc["frames"])
            return json.dumps(doc)

        def set_all(key, value):
            def go(rows):
                for r in rows:
                    r[key] = value
            return go

        def only_first(key):
            def go(rows):
                for r in rows[1:]:
                    r.pop(key, None)
            return go

        shifted = "".join(f"[mlv-stage-timing] frame={i if n == 'processed16_total' else i + 30} stage={n} ms={ms:.3f}\n"
                          for i in range(30) for n, ms in (("debayered_frame", 40.0), ("processing", 15.0), ("processed16_total", 56.0)))
        cases = [
            prores_case(stage=shifted, criteria=[]),                                                       # 0 disjoint child windows
            cdng_case(profile=cdng_profile(30, success=False), criteria=[]),                               # 1 every profile frame failed
            cdng_case(profile=profile(lambda rows: rows[0].update({"raw_read_ms": -1})), criteria=[]),     # 2 one negative producer timing
            cdng_case(dualIsoExpected=False, profile=profile(set_all("llrawproc_dual_iso_ms", -1)), criteria=[]),   # 3 negative dual-ISO figure on a no-dual-ISO leg
            cdng_case(profile=profile(set_all("dng_compress_encode_ms", {"ms": 10})), criteria=[]),        # 4 object-valued stage on every frame
            cdng_case(profile=profile(only_first("disk_write_ms")), criteria=[]),                          # 5 writer stage on one frame of 30
            cdng_case(criteria=[]),                                                                        # 6 control: the same case, well formed
            prores_case(criteria=[]),                                                                      # 7 control: ProRes, well formed
        ]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        # (r7: the record schema types each of these: a value or window that breaks it is EXPORT_RECORD_INVALID / EXPORT_STAGE_WINDOW_MISMATCH, a profile with no
        # successful frame stays EXPORT_PROFILE_ABSENT)
        typed = ("EXPORT_PROFILE_ABSENT", "EXPORT_RECORD_INVALID", "EXPORT_STAGE_WINDOW_MISMATCH")
        for i in range(6):
            self.assertEqual(got[i]["outcome"], "INVALID", (i, got[i]))
            self.assertTrue(any(t in p for p in got[i]["problems"] for t in typed), (i, got[i]))
        self.assertEqual((got[6]["outcome"], got[7]["outcome"]), ("PASS", "PASS"), got[6:])

    # --- r7: the validator CLASS (adversary r6 A1-A5 on a22d7786, Dot I2-EXPORT-NESTED-STAGE-WINDOW) -----------------------------------------
    @staticmethod
    def stage_lines(frames_by_stage: dict, ms: dict | None = None) -> str:
        default = {"raw_uint16_disk_read": 3.0, "raw_uint16_decompress": 5.0, "raw_uint16": 9.0, "llrawproc": 20.0, "debayered_frame": 40.0,
                   "processing": 15.0, "processed16_total": 56.0}
        default.update(ms or {})
        return "".join(f"[mlv-stage-timing] frame={i} stage={s} ms={default[s]:.3f}\n" for s, frames in frames_by_stage.items() for i in frames)

    def test_r7_a1_every_nested_rendered_stage_is_held_to_processed16_totals_frames(self) -> None:
        """adversary r6 A1 + Dot I2: only the two top-level children were held to processed16_total's frames; raw_uint16, raw_uint16_disk_read, raw_uint16_decompress and
        llrawproc published a share over any window. Every nested stage is parameterized over an unrelated singleton, an equal-sized disjoint set, a partial subset, a
        matching-frame control and a permitted absent stage, under empty AND the shipped criteria. The 480-frame shape from the finding is covered too."""
        nested = ("raw_uint16", "raw_uint16_disk_read", "raw_uint16_decompress", "llrawproc")
        everything = {s: range(30) for s in ("raw_uint16_disk_read", "raw_uint16_decompress", "raw_uint16", "llrawproc", "debayered_frame", "processing", "processed16_total")}
        scenarios = {"singleton": [100], "disjoint": range(30, 60), "partial": range(15), "control": range(30), "absent": []}
        cases, names = [], []
        for stage in nested:
            for name, frames in scenarios.items():
                for label, criteria in (("empty", []), ("shipped", None)):
                    over = dict(everything)
                    over[stage] = frames
                    case = prores_case(stage=self.stage_lines(over))
                    if criteria is not None:
                        case["criteria"] = criteria
                    cases.append(case)
                    names.append((stage, name, label))
        everything480 = {s: range(480) for s in everything}
        moved = dict(everything480)
        for s in nested:
            moved[s] = range(480, 960)
        only_first = dict(everything480)
        only_first["llrawproc"] = [0]
        for stage_map in (moved, only_first):
            cases.append(prores_case(maxFrames=480, stage=self.stage_lines(stage_map), batch=batch_log(480, verified=480), criteria=[]))
            names.append(("finding-480", "moved" if stage_map is moved else "llrawproc-one-frame", "empty"))
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for (stage, scenario, label), result in zip(names, got):
            with self.subTest(stage=stage, scenario=scenario, criteria=label):
                if scenario in ("control", "absent"):
                    self.assertEqual(result["outcome"], "PASS", result)
                else:
                    self.assertEqual(result["outcome"], "INVALID", result)
                    self.assertTrue(any("EXPORT_STAGE_WINDOW_MISMATCH" in p for p in result["problems"]), result)

    def test_r7_a2_a_null_or_negative_timing_record_is_invalid_whatever_the_criteria(self) -> None:
        """adversary r6 A2: a JSON null timing on every CDNG frame and a negative / non-finite ProRes stage-timing record read PASS (the value was dropped, never refused)."""
        def null_all(key):
            doc = json.loads(cdng_profile(30))
            for r in doc["frames"]:
                r[key] = None
            return json.dumps(doc)
        bad_lines = ("ms=-1.000", "ms=nan", "ms=inf", "ms=1e5", "ms=", "ms=1e999")
        cases = [cdng_case(profile=null_all("raw_read_ms"), criteria=[]),
                 cdng_case(profile=null_all("producer_frame_ms"), criteria=[]),
                 cdng_case(profile=null_all("llrawproc_dual_iso_ms"), criteria=[])]
        cases += [prores_case(stage=stage_log(30) + f"[mlv-stage-timing] frame=0 stage=processing {bad}\n", criteria=[]) for bad in bad_lines]
        cases += [cdng_case(criteria=[]), prores_case(criteria=[])]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(len(cases) - 2):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_RECORD_INVALID" in p for p in got[i]["problems"]), got[i])
        self.assertEqual((got[-2]["outcome"], got[-1]["outcome"]), ("PASS", "PASS"), got[-2:])

    def test_r7_a3_an_export_of_fewer_frames_than_requested_is_not_a_pass_under_empty_criteria(self) -> None:
        """adversary r6 A3: 30 exported of maxFrames=480 read PASS when both criteria arrays were empty. The receipt requires framesExported == the request unless a criterion
        of the leg names framesExported itself (then the criteria decide: the shipped legs' framesExported eq criterion still reads FAIL, see case 1 above). Decision:
        INVALID, because EXPORT_CAPTURED itself asserts the batch exported the requested window, so a short count under that token is evidence that disagrees with itself."""
        cases = [cdng_case(maxFrames=480, criteria=[]), prores_case(maxFrames=480, criteria=[]),
                 cdng_case(maxFrames=24, criteria=[]),                                       # 30 exported of a 24-frame request: more is not the request either
                 cdng_case(maxFrames=480, criteria=[{"metric": "framesExported", "op": "eq", "value": 480}]),   # the criterion decides: FAIL
                 cdng_case(criteria=[]), prores_case(criteria=[])]                           # controls: the count is the request
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in (0, 1, 2):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertIn("EXPORT_FRAMES_MISMATCH", got[i]["detail"], got[i])
        self.assertEqual(got[3]["outcome"], "FAIL", got[3])
        self.assertEqual((got[4]["outcome"], got[5]["outcome"]), ("PASS", "PASS"), got[4:])

    def test_r8_a1_naming_framesexported_in_a_criterion_does_not_switch_off_the_window_check(self) -> None:
        """adversary r7 A1: valid evidence for 24 exported frames against maxFrames=30 read PASS with criteria=[framesExported ge 0] (the r7 check was skipped for any
        criterion that merely NAMED the metric). Only `framesExported eq <the request>` hands the verdict to the criteria (it fails every other count, so the shipped legs
        still read FAIL); every other criterion on the metric leaves the count to the outcome rule, INVALID. Both formats; the full-window controls still PASS."""
        def short(maker, criteria):
            if maker is cdng_case:
                return cdng_case(maxFrames=30, profile=cdng_profile(24), batch=batch_log(24), manifest=output_manifest(24), criteria=criteria)
            return prores_case(maxFrames=30, stage=stage_log(24), batch=batch_log(24, verified=24), criteria=criteria)
        def ge0(): return [{"metric": "framesExported", "op": "ge", "value": 0}]
        invalid_criteria = (ge0(), [{"metric": "framesExported", "op": "ne", "value": 99}], [{"metric": "framesExported", "op": "eq", "value": 24}],
                            [{"metric": "framesExported", "op": "eq", "value": "30"}], [{"metric": "framesExported", "op": "le", "value": 30}], [])
        cases = [short(maker, c) for maker in (cdng_case, prores_case) for c in invalid_criteria]
        cases += [short(cdng_case, [{"metric": "framesExported", "op": "eq", "value": 30}]), short(prores_case, [{"metric": "framesExported", "op": "eq", "value": 30}]),
                  cdng_case(criteria=ge0()), prores_case(criteria=ge0())]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        n = len(invalid_criteria) * 2
        for i in range(n):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertIn("EXPORT_FRAMES_MISMATCH", got[i]["detail"], got[i])
        self.assertEqual((got[n]["outcome"], got[n + 1]["outcome"]), ("FAIL", "FAIL"), got[n:n + 2])   # the window gate itself decides
        self.assertEqual((got[n + 2]["outcome"], got[n + 3]["outcome"]), ("PASS", "PASS"), got[n + 2:])

    def test_r8_a2_repeated_records_that_sum_past_the_ceiling_are_invalid_not_dropped(self) -> None:
        """adversary r7 A2 + Dot: each of two stage-timing records for one frame conforms (ms=86400000 and ms=1) but their per-frame SUM passes the 24 h ceiling, so
        Get-DvExportStageRow dropped the frame (samples 0, share null) while the window check still counted it: PASS with no problem. Every stage the reader publishes is
        held to the ceiling on the per-frame accumulated value; a repeat that stays inside it is still a valid (summed) sample."""
        stages = ("raw_uint16", "raw_uint16_disk_read", "raw_uint16_decompress", "llrawproc", "debayered_frame", "processing", "processed16_total")
        def with_repeat(stage: str, ms: float, frames=range(30)) -> str:
            return stage_log(30) + "".join(f"[mlv-stage-timing] frame={i} stage={stage} ms={ms:.3f}\n" for i in frames)
        cases = [prores_case(stage=with_repeat(s, 86400000.0) + with_repeat(s, 1.0).replace(stage_log(30), ""), criteria=[]) for s in stages]
        cases += [prores_case(stage=with_repeat("processing", 86400000.0, [7]), criteria=[]), prores_case(stage=with_repeat("raw_uint16", 86400000.0), criteria=[{"metric": "stageFramesSeen", "op": "eq", "value": 30}])]
        cases += [prores_case(stage=with_repeat("processing", 1.0), criteria=[]), prores_case(criteria=[])]   # controls: a repeat inside the ceiling
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(len(cases) - 2):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_RECORD_INVALID" in p and "accumulated" in p for p in got[i]["problems"]), got[i])
        self.assertEqual((got[-2]["outcome"], got[-1]["outcome"]), ("PASS", "PASS"), got[-2:])
        self.assertEqual(got[-2]["stages"]["rows"]["processing"]["samples"], 30, "the summed repeat is one sample per frame")

    def test_r8_a1_a_failed_profile_frame_the_batch_did_not_skip_is_contradictory_evidence(self) -> None:
        """adversary r7 pass A1: a profile with 480 successful frames plus one success=false row, beside a batch that says exported=480 skipped=0, read PASS (only the successful
        rows were reconciled with the batch, and exportFramesFailed read the batch alone). No more failed profile rows than batch-skipped frames; a skipped frame the profile
        never logged is not a contradiction. Matching and absent-failure controls still PASS."""
        def with_failed_row(n_failed: int) -> str:
            doc = json.loads(cdng_profile(30))
            for k in range(n_failed):
                row = dict(doc["frames"][-1])
                row["frame_index"] = 30 + k
                row["success"] = False
                doc["frames"].append(row)
            doc["frame_count"] = len(doc["frames"])
            return json.dumps(doc)
        shipped = [{"metric": "framesExported", "op": "eq", "value": 30}, {"metric": "exportFramesFailed", "op": "eq", "value": 0}, {"metric": "profileFramesSucceeded", "op": "eq", "value": 30}]
        cases = [cdng_case(profile=with_failed_row(1), batch=batch_log(30, skipped=0), criteria=shipped),    # 0: the finding
                 cdng_case(profile=with_failed_row(2), batch=batch_log(30, skipped=1), criteria=[]),         # 1: two failed rows, one skipped
                 cdng_case(profile=with_failed_row(1), batch=batch_log(30, skipped=1), criteria=[]),         # 2: control: the failure is the batch's skip too
                 cdng_case(profile=with_failed_row(0), batch=batch_log(30, skipped=2), criteria=[]),         # 3: control: skipped frames with no profile row
                 cdng_case(criteria=[])]                                                                     # 4: control: no failure anywhere
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in (0, 1):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_OUTPUT_MISMATCH" in p and "failed frame" in p for p in got[i]["problems"]), got[i])
        for i in (2, 3, 4):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "PASS", got[i])

    def test_r8_a1b_a_stage_timed_longer_than_the_stage_that_contains_it_is_invalid(self) -> None:
        """adversary r8 pass 9 A1: raw_uint16 at 900 ms inside a 40 ms debayered_frame (share 16x), and a CDNG raw_read / llrawproc_other of 900 ms inside a 50 ms producer frame
        and a 5 ms llrawproc total, read PASS under empty criteria. Every child's mean is held to its parent's (5 % + 1 ms slack); an absent parent or child is skipped."""
        def stage_with(**ms) -> str:
            every = ("raw_uint16_disk_read", "raw_uint16_decompress", "raw_uint16", "llrawproc", "debayered_frame", "processing", "processed16_total")
            return self.stage_lines({s: range(30) for s in every}, ms)
        def cdng_with(**fields) -> str:
            doc = json.loads(cdng_profile(30))
            for r in doc["frames"]:
                r.update(fields)
            return json.dumps(doc)
        cases = [prores_case(stage=stage_with(raw_uint16=900.0), criteria=[]),                         # 0: the finding
                 prores_case(stage=stage_with(raw_uint16_disk_read=30.0), criteria=[]),                # 1: disk_read 30 > raw_uint16 9
                 prores_case(stage=stage_with(llrawproc=500.0), criteria=[]),                          # 2: llrawproc past debayered_frame
                 prores_case(stage=stage_with(processing=200.0), criteria=[]),                         # 3: a top-level child past processed16_total
                 cdng_case(profile=cdng_with(raw_read_ms=900.0), criteria=[]),                         # 4: a producer stage past producer_frame_ms
                 cdng_case(profile=cdng_with(llrawproc_ms=5.0, llrawproc_other_ms=20.0), criteria=[]), # 5: a llrawproc child past llrawproc_ms
                 cdng_case(profile=cdng_with(llrawproc_ms=900.0), criteria=[]),                        # 6: (pass 10 A1) the llrawproc TOTAL past producer_frame_ms
                 # (pass 10 A2) disjoint siblings that are each inside the parent but together are not:
                 prores_case(stage=stage_with(debayered_frame=10.0, processing=10.0, processed16_total=10.0, raw_uint16=2.0, raw_uint16_disk_read=1.0, raw_uint16_decompress=1.0, llrawproc=2.0), criteria=[]),  # 7
                 prores_case(stage=stage_with(raw_uint16=40.0, llrawproc=40.0, debayered_frame=40.0, raw_uint16_disk_read=3.0, raw_uint16_decompress=5.0), criteria=[]),                                    # 8
                 prores_case(stage=stage_with(raw_uint16_disk_read=9.0, raw_uint16_decompress=9.0), criteria=[])]                                                                                      # 9
        controls = [prores_case(criteria=[]), cdng_case(criteria=[])]
        proc, got = evidence_cases(cases + controls)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(len(cases)):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p for p in got[i]["problems"]), got[i])
        self.assertEqual((got[-2]["outcome"], got[-1]["outcome"]), ("PASS", "PASS"), got[-2:])

    def test_r8_a3_a_manifest_count_or_total_that_is_not_a_json_integer_is_invalid(self) -> None:
        """adversary r7 pass A3 (advisory): count [1] / "1" and totalBytes [1000] / "1000" were stringified and parsed, so a non-integer shape read PASS. Only a JSON integer is one."""
        def manifest(count, total) -> str:
            return json.dumps({"schema": "export-stage-output-manifest.v1", "kind": "mov", "count": count, "totalBytes": total, "deleted": True})
        cases = [prores_case(manifest=manifest([1], 1000), criteria=[]), prores_case(manifest=manifest("1", 1000), criteria=[]),
                 prores_case(manifest=manifest(1, [1000]), criteria=[]), prores_case(manifest=manifest(1, "1000"), criteria=[]),
                 prores_case(manifest=manifest(1, 1000.5), criteria=[]), prores_case(criteria=[])]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(5):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_OUTPUT_MISMATCH" in p and "integer" in p for p in got[i]["problems"]), got[i])
        self.assertEqual(got[5]["outcome"], "PASS", got[5])

    def test_r7_a5_a_malformed_profile_container_or_element_is_invalid_before_counting(self) -> None:
        """adversary r6 A5: a JSON null appended to profile frames (frame_count 481), or frames given as one object, read PASS because null elements were filtered and a
        lone object was wrapped. The container must be an array of objects, frame_count must be the array's length, and every element is a record."""
        def mutate(fn) -> str:
            doc = json.loads(cdng_profile(30))
            fn(doc)
            return json.dumps(doc)
        def append(value):
            def go(doc):
                doc["frames"].append(value)
                doc["frame_count"] = len(doc["frames"])
            return go
        def one_object(doc):
            doc["frames"] = doc["frames"][0]
        def wrong_count(doc):
            doc["frame_count"] = 29
        cases = [cdng_case(profile=mutate(append(None)), criteria=[]), cdng_case(profile=mutate(append(5)), criteria=[]),
                 cdng_case(profile=mutate(append("x")), criteria=[]), cdng_case(profile=mutate(append([])), criteria=[]),
                 cdng_case(profile=mutate(one_object), criteria=[]), cdng_case(profile=mutate(wrong_count), criteria=[]),
                 cdng_case(profile=mutate(append(None))),                                    # shipped-style criteria too
                 cdng_case(criteria=[])]                                                      # control
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(len(cases) - 1):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_RECORD_INVALID" in p for p in got[i]["problems"]), got[i])
        self.assertEqual(got[-1]["outcome"], "PASS", got[-1])

    def test_a_capture_with_no_stage_measurement_or_with_footage_left_behind_is_invalid(self) -> None:
        """adversary r2 A2/A3 (on c3768da1): a captured export whose profile rows carry only frame_index and one dual-ISO figure (CDNG), or whose stage log has
        processed16_total and no child stage (ProRes), measured no stage share, yet read PASS; and a manifest that does not record the exported footage as
        deleted (deleted false or absent) read PASS with the derived owner footage still on the venue. Each is INVALID with a typed problem."""
        bare_profile = json.dumps({"schema": "mlvapp.export_stage_profile.v1", "build_id": BUILD_ID, "frame_count": 30, "dng_compress_placement": "producer",
                                   "async_writer_can_overlap_dng_compress": False, "stages": {},
                                   "frames": [{"clip": "view-0", "frame_index": i, "success": True, "llrawproc_dual_iso_ms": 1.0} for i in range(30)]})
        bare_stage = "".join(f"[mlv-stage-timing] frame={i} stage=processed16_total ms=56.000\n" for i in range(30))
        one_child = "".join(f"[mlv-stage-timing] frame={i} stage={s} ms={ms}\n" for i in range(30) for s, ms in (("processed16_total", 56.0), ("processing", 15.0)))

        def manifest(count: int, kind: str, **over) -> str:
            doc = json.loads(output_manifest(count, kind))
            doc.update(over)
            if over.get("deleted", 0) is None:
                del doc["deleted"]
            return json.dumps(doc)
        cases = [cdng_case(profile=bare_profile),                                              # 0 CDNG: no stage figure at all
                 prores_case(stage=bare_stage),                                                # 1 ProRes: no child stage
                 prores_case(stage=one_child),                                                 # 2 ProRes: only one of the two disjoint children
                 cdng_case(manifest=manifest(30, "dng", deleted=False)),                       # 3 CDNG footage left behind
                 prores_case(manifest=manifest(1, "mov", deleted=False)),                      # 4 ProRes footage left behind
                 cdng_case(manifest=manifest(30, "dng", deleted=None)),                        # 5 no deleted field
                 cdng_case(manifest=manifest(30, "dng", deleted="true"))]                      # 6 a string is not true
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i, token in ((0, "EXPORT_PROFILE_ABSENT"), (1, "EXPORT_PROFILE_ABSENT"), (2, "EXPORT_PROFILE_ABSENT"), (3, "EXPORT_OUTPUT_NOT_DELETED"),
                         (4, "EXPORT_OUTPUT_NOT_DELETED"), (5, "EXPORT_OUTPUT_NOT_DELETED"), (6, "EXPORT_OUTPUT_NOT_DELETED")):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any(token in p for p in got[i]["problems"]), got[i])


    def test_a_stage_table_of_another_window_or_a_malformed_profile_is_invalid_and_never_throws(self) -> None:
        """adversary r2 pass 2 A1/A3/A4/A5: a stage timed on a few frames only (its share is of another window), a profile read with no expected build id, a
        profile with duplicate frame ids / a string success / no frames / an object-valued timing, and a negative output byte count each read INVALID with a
        typed problem and never PASS or a thrown cast."""
        def profile(rows: list[dict]) -> str:
            return json.dumps({"schema": "mlvapp.export_stage_profile.v1", "build_id": BUILD_ID, "frame_count": len(rows), "dng_compress_placement": "producer",
                               "async_writer_can_overlap_dng_compress": False, "stages": {}, "frames": rows})

        def row(i: int, **over) -> dict:
            r = {"clip": "view-0", "frame_index": i, "success": True, "llrawproc_dual_iso_ms": 30.0, "producer_frame_ms": 50.0, "raw_read_ms": 4.0}
            r.update(over)
            return r
        one_window = profile([row(i, **({"raw_read_ms": 10.0} if i == 0 else {})) for i in range(30)])
        one_window = json.loads(one_window)
        for r in one_window["frames"][1:]:
            r.pop("raw_read_ms")                       # raw_read is timed on frame 0 only
        children_late = "".join(f"[mlv-stage-timing] frame={i} stage=processed16_total ms=56.000\n" for i in range(30)) \
            + "".join(f"[mlv-stage-timing] frame={i} stage={s} ms=1000.000\n" for i, s in ((30, "debayered_frame"), (31, "processing")))
        neg_bytes = json.loads(output_manifest(30))
        neg_bytes["totalBytes"] = -1
        cases = [prores_case(stage=children_late),                                                                                    # 0 children outside processed16_total's frames
                 cdng_case(profile=json.dumps(one_window)),                                                                           # 1 raw_read timed on one frame
                 cdng_case(buildId=""),                                                                                               # 2 no expected build id
                 cdng_case(profile=profile([row(0) for _ in range(30)])),                                                             # 3 every frame_index 0
                 cdng_case(profile=profile([row(i, success="true") for i in range(30)])),                                             # 4 string success
                 cdng_case(profile=profile([]), criteria=[]),                                                                         # 5 no frames, informational criteria
                 cdng_case(profile=profile([row(i, **({"producer_frame_ms": {}} if i == 0 else {})) for i in range(30)])),            # 6 object-valued timing
                 cdng_case(manifest=json.dumps(neg_bytes)),                                                                           # 7 negative totalBytes
                 cdng_case(profile=profile([row(i, llrawproc_dual_iso_ms={}) for i in range(30)]))]                                   # 8 object-valued dual-ISO figure
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(len(got), len(cases))
        for i in range(len(cases)):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(got[i]["problems"], got[i])

    def test_r11_stage_evidence_is_held_inside_the_batch_elapsed_for_both_formats(self) -> None:
        """EXPORT-VENUE-LEG-1 r11 (sol r9 B2 (a)): a batch DONE elapsed shorter than the per-frame work the stage evidence measured is INVALID EXPORT_STAGE_CONTAINMENT_MISMATCH, for
        ProRes (processed16_total) and CDNG (producer_frame): elapsed=0.0 and 0.1 beside 480 frames used to PASS with a negative untimedPerFrameMs and an inflated exportFps. The control
        (elapsed=60.0) and the real-shaped tolerance (a few percent under, one decimal of logging) still PASS, and a slightly negative residual is reported as 0, never negative."""
        legs = {fmt: leg(name) for fmt, name in (("prores", "m16-1243-export-prores"), ("cdng-lossless", "m16-1243-export-cdng-lossless"))}
        shipped = {fmt: spec["criteria"]["acceptance"]["cpu"] for fmt, spec in legs.items()}
        frames = 480

        def case(fmt: str, elapsed: float, n: int = frames) -> dict:
            if fmt == "prores":
                return prores_case(maxFrames=n, stage=stage_log(n), batch=batch_log(n, verified=n, elapsed=elapsed), manifest=output_manifest(1, "mov"), criteria=shipped[fmt])
            return cdng_case(maxFrames=n, profile=cdng_profile(n), batch=batch_log(n, elapsed=elapsed), manifest=output_manifest(n), criteria=shipped[fmt])
        cases = [case(f, e) for f in ("prores", "cdng-lossless") for e in (0.0, 0.1, 60.0)]
        # tolerance: 30 frames of 56 ms is 1.68 s of work; an elapsed of 1.6 s is 5 % under (one decimal of logging, a clock's rounding), not a contradiction
        cases.append(prores_case(maxFrames=30, stage=stage_log(30), batch=batch_log(30, verified=30, elapsed=1.6)))
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for (fmt, elapsed), g in zip([(f, e) for f in ("prores", "cdng-lossless") for e in (0.0, 0.1, 60.0)], got):
            with self.subTest(fmt=fmt, elapsed=elapsed):
                if elapsed == 60.0:
                    self.assertEqual(g["outcome"], "PASS", g)
                    self.assertFalse(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p for p in g["problems"]), g)
                else:
                    self.assertEqual(g["outcome"], "INVALID", g)
                    self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p and "batch_wall_per_frame" in p for p in g["problems"]), g)
        near = got[-1]
        self.assertEqual(near["outcome"], "PASS", near)
        self.assertGreaterEqual(near["stages"]["untimedPerFrameMs"], 0.0, "a residual the logging precision made slightly negative is 0, never a negative stage")

    def test_r11_cdng_disjoint_producer_stages_and_llrawproc_children_are_held_together_inside_their_parents(self) -> None:
        """EXPORT-VENUE-LEG-1 r11 (fable r9 B2 / (b)): the CDNG reader held each producer stage below producer_frame but never their SUM: 40 + 30 + 30 + 40 ms of disjoint stages inside a
        41 ms producer_frame read PASS. The producer stages (the disjoint partition of producer_frame) and the llrawproc_* children (the partition of llrawproc) are each held together."""
        def profile(set_rows: dict) -> str:
            doc = json.loads(cdng_profile(30, dual_iso_ms=0.0))
            for r in doc["frames"]:
                for k in CDNG_STAGES:
                    if k not in ("writer_queue_wait_ms", "writer_completion_lag_ms", "disk_write_ms"):
                        r[k] = 0.0
                r.update(set_rows)
            return json.dumps(doc)
        producer_sum = profile({"raw_read_ms": 40.0, "raw_decode_ms": 30.0, "dng_pack_ms": 30.0, "payload_clone_ms": 40.0, "producer_frame_ms": 41.0})
        children_sum = profile({"llrawproc_dark_frame_ms": 40.0, "llrawproc_vertical_stripes_ms": 30.0, "llrawproc_focus_pixels_ms": 30.0, "llrawproc_ms": 41.0, "producer_frame_ms": 200.0})
        control = profile({"raw_read_ms": 10.0, "raw_decode_ms": 10.0, "dng_pack_ms": 10.0, "payload_clone_ms": 10.0, "llrawproc_dark_frame_ms": 5.0, "llrawproc_vertical_stripes_ms": 5.0,
                           "llrawproc_ms": 10.0, "producer_frame_ms": 60.0})
        cases = [cdng_case(dualIsoExpected=False, profile=p, criteria=[]) for p in (producer_sum, children_sum, control)]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i, inside in ((0, "producer_frame"), (1, "llrawproc")):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p and "the disjoint stages" in p and f"inside {inside} mean" in p for p in got[i]["problems"]), got[i])
        self.assertEqual(got[2]["outcome"], "PASS", got[2])

    def test_r11_adversary_a1_nested_render_stages_are_held_inside_debayered_frame_when_raw_uint16_is_absent(self) -> None:
        """adversary r11 A1: raw_uint16 is a stage the app may not publish, which skipped both child pairs and their sum group: raw_uint16_disk_read and raw_uint16_decompress at 900 ms each
        inside a 41 ms debayered_frame read PASS with 16x shares. The nested stages are held inside debayered_frame whether or not raw_uint16 is there."""
        def log(disk: float, decomp: float, n: int = 480) -> str:
            return "".join(f"[mlv-stage-timing] frame={i} stage={s} ms={ms:.3f}\n" for i in range(n)
                           for s, ms in (("processed16_total", 56.0), ("debayered_frame", 41.0), ("processing", 15.0), ("raw_uint16_disk_read", disk), ("raw_uint16_decompress", decomp)))
        criteria = leg("m16-1243-export-prores")["criteria"]["acceptance"]["cpu"]

        def case(disk: float, decomp: float) -> dict:
            return prores_case(maxFrames=480, stage=log(disk, decomp), batch=batch_log(480, verified=480, elapsed=60.0), criteria=criteria)
        proc, got = evidence_cases([case(900.0, 900.0), case(3.0, 5.0), case(30.0, 30.0)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p for p in got[0]["problems"]), got[0])
        self.assertEqual(got[1]["outcome"], "PASS", got[1])   # control: small nested stages
        self.assertEqual(got[2]["outcome"], "INVALID", got[2])   # 30 + 30 ms together do not fit a 41 ms parent beside... each fits, the SUM (60) does not
        self.assertTrue(any("the disjoint stages" in p for p in got[2]["problems"]), got[2])

    def test_r11_adversary_a2_the_llrawproc_remainder_and_its_lock_times_are_not_added_twice(self) -> None:
        """adversary r11 A2: dng.c computes llrawproc_other as the llrawproc total less its seven timed children, so it RETAINS the lock times published beside it. A consistent profile
        (producer 120, llrawproc 100 = dual_iso 30 + other 70, shared_lock 10 inside the 70) read INVALID because the groups added the lock to the remainder."""
        def profile(producer: float) -> str:
            doc = json.loads(cdng_profile(30, dual_iso_ms=0.0))
            for r in doc["frames"]:
                for k in CDNG_STAGES:
                    if k not in ("writer_queue_wait_ms", "writer_completion_lag_ms", "disk_write_ms"):
                        r[k] = 0.0
                r.update({"producer_frame_ms": producer, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 30.0, "llrawproc_other_ms": 70.0, "llrawproc_shared_lock_ms": 10.0})
            return json.dumps(doc)
        cases = [cdng_case(dualIsoExpected=True, profile=profile(p), criteria=[]) for p in (120.0, 102.0)]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in (0, 1):
            with self.subTest(case=i):
                self.assertFalse(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p for p in got[i]["problems"]), got[i])
                self.assertEqual(got[i]["outcome"], "PASS", got[i])

    def test_r13_adversary_a1_disjoint_render_work_is_held_inside_the_nearest_published_ancestor(self) -> None:
        """adversary r13 A1: with raw_uint16 unpublished, llrawproc 30 + disk_read 25 + decompress 10 (65 ms of disjoint work) inside a 40 ms debayered_frame read PASS, because the sum
        groups needed raw_uint16 beside llrawproc. The leaves are held together inside the nearest ancestor that IS published; a run that omits debayered_frame too is held inside the denominator."""
        def log(rows: tuple) -> str:
            return "".join(f"[mlv-stage-timing] frame={i} stage={s} ms={ms:.3f}\n" for i in range(480) for s, ms in rows)
        criteria = leg("m16-1243-export-prores")["criteria"]["acceptance"]["cpu"]

        def case(rows: tuple) -> dict:
            return prores_case(maxFrames=480, stage=log(rows), batch=batch_log(480, verified=480, elapsed=60.0), criteria=criteria)
        no_raw = (("processed16_total", 56.0), ("debayered_frame", 40.0), ("processing", 16.0), ("raw_uint16_disk_read", 25.0), ("raw_uint16_decompress", 10.0), ("llrawproc", 30.0))
        no_debayer = (("processed16_total", 56.0), ("processing", 16.0), ("raw_uint16", 40.0), ("llrawproc", 30.0))
        ok = (("processed16_total", 56.0), ("debayered_frame", 40.0), ("processing", 16.0), ("raw_uint16_disk_read", 5.0), ("raw_uint16_decompress", 5.0), ("llrawproc", 10.0))
        proc, got = evidence_cases([case(no_raw), case(no_debayer), case(ok)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in (0, 1):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p and "the disjoint stages" in p for p in got[i]["problems"]), got[i])
        self.assertEqual(got[2]["outcome"], "PASS", got[2])

    def test_r13_adversary_a2_cdng_disjoint_producer_work_is_held_inside_producer_frame_without_the_llrawproc_total(self) -> None:
        """adversary r13 A2: llrawproc_ms is an optional total. Without it raw_read 20 + dark_frame 30 + dual_iso 30 (80 ms of disjoint work) inside a 50 ms producer_frame read PASS,
        because the producer partition counts the total once and so had one member short of two. The partition with the total replaced by its own children is held too."""
        def profile(set_rows: dict) -> str:
            doc = json.loads(cdng_profile(30, dual_iso_ms=0.0))
            for r in doc["frames"]:
                for k in CDNG_STAGES:
                    if k not in ("writer_queue_wait_ms", "writer_completion_lag_ms", "disk_write_ms"):
                        r[k] = 0.0
                r.pop("llrawproc_ms", None)
                r.update(set_rows)
            return json.dumps(doc)
        over = profile({"producer_frame_ms": 50.0, "raw_read_ms": 20.0, "llrawproc_dark_frame_ms": 30.0, "llrawproc_dual_iso_ms": 30.0})
        control = profile({"producer_frame_ms": 50.0, "raw_read_ms": 10.0, "llrawproc_dark_frame_ms": 10.0, "llrawproc_dual_iso_ms": 10.0})
        proc, got = evidence_cases([cdng_case(dualIsoExpected=True, profile=p, criteria=[]) for p in (over, control)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p and "the disjoint stages" in p and "inside producer_frame mean" in p for p in got[0]["problems"]), got[0])
        self.assertEqual(got[1]["outcome"], "PASS", got[1])

    def test_r13_pass13_a1_dual_iso_and_the_lock_times_outside_its_window_are_held_inside_llrawproc(self) -> None:
        """adversary r13 pass 13 A1: llrawproc_other retains the lock times, so dual_iso 30 + publish_lock 90 (disjoint windows, llrawproc.c:6210-6219 vs :6292) inside a 100 ms llrawproc total
        read PASS beside a too-small llrawproc_other (70). The disjoint frontier is held inside llrawproc and, with the producer's other members, inside producer_frame. Same for the refine lock."""
        def profile(set_rows: dict, drop_total: bool = False) -> str:
            doc = json.loads(cdng_profile(30, dual_iso_ms=0.0))
            for r in doc["frames"]:
                for k in CDNG_STAGES:
                    if k not in ("writer_queue_wait_ms", "writer_completion_lag_ms", "disk_write_ms"):
                        r[k] = 0.0
                if drop_total:
                    r.pop("llrawproc_ms", None)
                r.update(set_rows)
            return json.dumps(doc)
        base = {"producer_frame_ms": 120.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 30.0, "llrawproc_other_ms": 70.0}
        publish = profile({**base, "llrawproc_publish_lock_ms": 90.0})
        refine = profile({**base, "llrawproc_dualiso_refine_lock_ms": 90.0})
        no_total = profile({"producer_frame_ms": 100.0, "llrawproc_dual_iso_ms": 30.0, "llrawproc_publish_lock_ms": 90.0}, drop_total=True)
        control = profile({**base, "llrawproc_publish_lock_ms": 10.0, "llrawproc_dualiso_refine_lock_ms": 10.0})
        proc, got = evidence_cases([cdng_case(dualIsoExpected=True, profile=p, criteria=[]) for p in (publish, refine, no_total, control)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in (0, 1, 2):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p and "the disjoint stages" in p for p in got[i]["problems"]), got[i])
        self.assertEqual(got[3]["outcome"], "PASS", got[3])

    def test_the_export_threshold_is_a_number_in_range_in_the_shape(self) -> None:
        """adversary r2 pass 2 A6: generatorArgs.cpuQuiescenceThresholdPercent is refused by the shape unless it is a number in (0, 100]; an object / bool / string
        passed the shape and threw at the generator's double cast after a lease was spent."""
        base = leg(EXPORT_LEGS[0])
        values = [{}, True, "20", [20], -1, 0, 100.5, None]
        cases = []
        for v in values:
            s = json.loads(json.dumps(base))
            s["generatorArgs"]["cpuQuiescenceThresholdPercent"] = v
            cases.append({"name": repr(v), "spec": s})
        for v in (0.05, 20, 100):
            s = json.loads(json.dumps(base))
            s["generatorArgs"]["cpuQuiescenceThresholdPercent"] = v
            cases.append({"name": "ok-" + repr(v), "spec": s})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c.json"
            path.write_text(json.dumps(cases), encoding="utf-8")
            proc, got = run_pwsh_json("$cases = Get-Content -Raw -LiteralPath $env:C -Encoding utf8 | ConvertFrom-Json\n"
                                      "@($cases | ForEach-Object { [ordered]@{ name = $_.name; r = (Get-DvLegSpecShapeRefusal -Spec $_.spec) } }) | ConvertTo-Json -Compress -AsArray", C=str(path))
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        for case, row_ in zip(cases, got):
            with self.subTest(case=case["name"]):
                if case["name"].startswith("ok-"):
                    self.assertIsNone(row_["r"], row_)
                else:
                    self.assertIsNotNone(row_["r"], row_)


EXPORT_STUB_GENERATOR = r"""
param($SourceCommit,$BuildManifestSha256,$ClipId,$OutFile,$RepoRoot,$Venue,$Backend,$CpuQuiescenceThresholdPercent,$VenueTablePath,$PlaySeconds,$Format,$MaxFrames,$ReceiptPath,$DualIsoExpected)
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
Add-Content -LiteralPath $cfg.genLog -Value (($PSBoundParameters.Keys | Sort-Object | ForEach-Object { $_ + '=' + $PSBoundParameters[$_] }) -join ';')
Set-Content -LiteralPath $OutFile -Value "# stub export job"
[pscustomobject]@{ outFile = $OutFile; recommendedJobTimeoutSec = 900; clipContentSha256 = $cfg.clipContentSha256; playSeconds = $PlaySeconds; fixtureRehearsal = $false; legType = 'export' }
"""


@requires_windows_pwsh
class ExportRunnerTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(EXPORT_STUB_GENERATOR, encoding="utf-8")

    def export_spec(self, name: str = EXPORT_LEGS[0], maxframes: int | None = None) -> Path:
        spec = leg(name)
        if maxframes is not None:
            spec["export"]["maxFrames"] = maxframes
            for role in ("acceptance", "supplementary"):
                for c in spec["criteria"][role]["cpu"]:
                    if c["metric"] in ("framesExported", "profileFramesSucceeded", "stageFramesSeen"):
                        c["value"] = maxframes
        path = self.tmp / f"{name}-{maxframes}.json"
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def write_export_artifacts(self, frames: int, profile: bool = True, venue: str = "bachelor", fmt: str = "cdng-lossless",
                               exported: int | None = None, result: str = "EXPORT_CAPTURED", observed_venue: str | None = None, stage: str | None = None,
                               source_commit: str | None = None) -> None:
        a = self.artifacts
        if a.exists():
            shutil.rmtree(a)
        a.mkdir(parents=True)
        exported = frames if exported is None else exported
        summary = {"schema": "export-stage-job.v1", "result": result, "legType": "export", "format": fmt, "maxFrames": frames,
                   "dualIsoExpected": True, "clipId": "M16-1243", "backend": "cpu", "declaredVenue": venue, "venue": observed_venue or venue,
                   "sourceCommit": source_commit or self.sha, "buildManifestSha256": self.build_sha, "receiptSha256": leg(EXPORT_LEGS[0])["export"]["receiptSha256"],
                   "exportWallSeconds": 30.0, "appExitCode": 0, "timedOut": False, "fixtureRehearsal": False}
        (a / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (a / "batch.log").write_text(batch_log(exported), encoding="utf-8")
        if fmt == "prores":
            (a / "batch.log").write_text(batch_log(exported, verified=exported), encoding="utf-8")
            (a / "stage-timing.log").write_text(stage_log(frames) if stage is None else stage, encoding="utf-8")
            (a / "output-manifest.json").write_text(output_manifest(1, "mov"), encoding="utf-8")
            return
        if profile:
            (a / "export-profile.json").write_text(cdng_profile(frames, build_id=source_commit or self.sha), encoding="utf-8")
        (a / "output-manifest.json").write_text(output_manifest(exported), encoding="utf-8")

    def validate(self, receipt: dict, spec: Path, dv: Path = DV, evidence_dir: str = "") -> dict:
        rp = self.tmp / "receipt-under-test.json"
        rp.write_text(json.dumps(receipt), encoding="utf-8")
        ev = f" -EvidenceDir '{evidence_dir}'" if evidence_dir else ""
        proc, got = run_pwsh_json(f"Import-Module '{dv / 'DualVenueRunner.psm1'}' -Force\n"
                                  f"$r = Get-Content -Raw '{rp}' | ConvertFrom-Json\n"
                                  f"$v = Test-DvReceiptValid -Receipt $r -AllowOfflineTestMode -OfflineLegSpecBytes ([IO.File]::ReadAllBytes('{spec}')){ev}\n"
                                  "[ordered]@{ valid = $v.valid; status = $v.status; reasons = @($v.reasons) } | ConvertTo-Json -Compress")
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        return got

    def test_a_json_null_summary_or_um_run_with_its_hash_updated_does_not_verify(self) -> None:
        """adversary r1 A2: a published document that parses to JSON null (its sha256 claim updated to match) is no evidence, never a PASS."""
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(self.validate(receipt, spec)["status"], "VERIFIED_OFFLINE_TEST")
        evdir = Path(receipt["evidence"]["localEvidenceDir"])
        null_sha = hashlib.sha256(b"null").hexdigest()
        for target in ("summary", "um-run"):
            with self.subTest(target=target):
                r = json.loads(json.dumps(receipt))
                work = self.tmp / f"ev-{target}"
                if work.exists():
                    shutil.rmtree(work)
                shutil.copytree(evdir, work)
                if target == "summary":
                    (work / "export" / "summary.json").write_bytes(b"null")
                    r["export"]["files"]["summary.json"] = null_sha
                    if r["evidence"].get("summaryJsonSha256"):
                        r["evidence"]["summaryJsonSha256"] = null_sha
                else:
                    (work / "um-run.json").write_bytes(b"null")
                    r["evidence"]["umRunJsonSha256"] = null_sha
                got = self.validate(r, spec, evidence_dir=str(work))
                self.assertFalse(got["valid"], got)
                self.assertIn(got["status"], ("INVALID", "INCOMPLETE"), got)

    def test_r8_a3b_an_export_built_from_another_commit_than_the_requested_one_is_invalid_at_write(self) -> None:
        """adversary r8 pass 9 A3: the leg took ExpectedBuildId from the job's OWN summary.sourceCommit and never compared that to the requested SourceCommit, so a summary and
        profile that both named another commit passed. The writer binds the requested commit: the reader's expected build is the request, and the identity loop compares it."""
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30, source_commit="f" * 40)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertTrue("sourceCommit" in receipt["outcomeDetail"] or "EXPORT_PROFILE_FOREIGN" in receipt["outcomeDetail"], receipt["outcomeDetail"])
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])

    def test_r8_a4_an_out_of_range_or_non_integer_um_run_exit_code_is_a_typed_invalid_not_a_throw(self) -> None:
        """adversary r7 pass A4: um-run.json exitCode 2147483648 (valid Int64) passed the null-only guard and the [int] narrowing threw before Test-DvReceiptValid returned.
        Out-of-range, string and array exit codes (hash claim updated to match) are typed INVALID, and a real exit code still verifies."""
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(self.validate(receipt, spec)["status"], "VERIFIED_OFFLINE_TEST")
        evdir = Path(receipt["evidence"]["localEvidenceDir"])
        for label, code in (("int32-max-plus-1", "2147483648"), ("int32-min-minus-1", "-2147483649"), ("int64-max", "9223372036854775807"), ("string", '"0"'), ("array", "[0]")):
            with self.subTest(exitCode=label):
                work = self.tmp / f"ev-exit-{label}"
                if work.exists():
                    shutil.rmtree(work)
                shutil.copytree(evdir, work)
                raw = ('{"schema":"mlv-app/dual-venue-um-run/v1","exitCode":' + code + ',"resultToken":"EXPORT_CAPTURED"}\n').encode("utf-8")
                (work / "um-run.json").write_bytes(raw)
                r = json.loads(json.dumps(receipt))
                r["evidence"]["umRunJsonSha256"] = hashlib.sha256(raw).hexdigest()
                got = self.validate(r, spec, evidence_dir=str(work))
                self.assertFalse(got["valid"], got)
                self.assertEqual(got["status"], "INVALID", got)
                self.assertTrue(any("EVIDENCE_UNPARSABLE: um-run.json" in x and "exitCode" in x for x in got["reasons"]), got)

    def test_a_summary_observed_on_another_venue_is_invalid_at_write_and_at_verify(self) -> None:
        """adversary r2 A4 (on c3768da1): summary.venue is the host the job MEASURED on (Get-AttrCudaMeasurementVenue); only summary.declaredVenue was compared, so
        a Bachelor receipt whose job ran on Ultra-Magnus wrote PASS and re-derived VERIFIED. Both identity loops now compare it."""
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30, observed_venue="ultra-magnus")
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("EXPORT_SUMMARY_MISMATCH", receipt["outcomeDetail"])
        # the validator: a good receipt, then its hashed summary rewritten to name another observed venue with every sha256 claim updated to match
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        evdir = Path(receipt["evidence"]["localEvidenceDir"])
        work = self.tmp / "ev-venue-A4"
        shutil.copytree(evdir, work)
        summary = json.loads((work / "export" / "summary.json").read_text(encoding="utf-8"))
        summary["venue"] = "ultra-magnus"
        raw = json.dumps(summary).encode("utf-8")
        (work / "export" / "summary.json").write_bytes(raw)
        new_sha = hashlib.sha256(raw).hexdigest()
        r = json.loads(json.dumps(receipt))
        r["export"]["files"]["summary.json"] = new_sha
        if r["evidence"].get("summaryJsonSha256"):
            r["evidence"]["summaryJsonSha256"] = new_sha
        got = self.validate(r, spec, evidence_dir=str(work))
        self.assertEqual(got["status"], "INVALID", got)
        self.assertTrue(any("EXPORT_SUMMARY_MISMATCH" in x for x in got["reasons"]), got)

    def test_a_missing_evidence_dir_is_incomplete_not_an_exception(self) -> None:
        """adversary r1 A3: an evidence directory that is not there is a typed INCOMPLETE, never a StrictMode throw."""
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        got = self.validate(receipt, spec, evidence_dir=str(self.tmp / "missing-export-evidence-A3"))
        self.assertEqual(got["status"], "INCOMPLETE", got)
        self.assertTrue(any("EVIDENCE_ABSENT" in r for r in got["reasons"]), got)

    def test_the_runner_hands_the_export_generator_the_export_arguments(self) -> None:
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30)
        proc, receipt, submitted = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        calls = self.generator_calls()
        self.assertTrue(calls, proc.stdout[-3000:] + proc.stderr[-3000:])
        fields = dict(kv.split("=", 1) for kv in calls[-1].split(";") if "=" in kv)
        self.assertEqual((fields.get("Format"), fields.get("MaxFrames"), fields.get("DualIsoExpected"), fields.get("Backend"), fields.get("CpuQuiescenceThresholdPercent")),
                         ("cdng-lossless", "30", "True", "cpu", "20"), calls[-1])
        self.assertTrue(fields.get("ReceiptPath", "").endswith(".marxml"), calls[-1])
        self.assertNotIn("ScaleFactor", fields)

    def test_a_happy_export_receipt_verifies_and_carries_the_metrics(self) -> None:
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["verification"]["status"], "VERIFIED_OFFLINE_TEST")
        self.assertEqual(receipt["export"]["format"], "cdng-lossless")
        self.assertEqual(receipt["metrics"]["framesExported"], 30)
        self.assertEqual(self.validate(receipt, spec)["status"], "VERIFIED_OFFLINE_TEST")
        # a tampered published file no longer verifies
        ev = Path(receipt["evidence"]["localEvidenceDir"]) / "export" / "batch.log"
        ev.write_text(batch_log(29), encoding="utf-8")
        self.assertEqual(self.validate(receipt, spec)["status"], "INVALID")

    def test_no_failure_token_ends_pass(self) -> None:
        spec = self.export_spec(maxframes=30)
        for token, exit_code, want in (("EXPORT_TIMEOUT", 40, "FAIL"), ("EXPORT_NO_FRAMES", 41, "FAIL"), ("EXPORT_PROFILE_ABSENT", 43, "INVALID"),
                                       ("EXPORT_TOOLING_ABSENT", 45, "VENUE_TOOLING"), ("EXPORT_CAPTURED", 1, "INVALID")):
            with self.subTest(token=token):
                self.write_export_artifacts(30, profile=(token != "EXPORT_PROFILE_ABSENT"), exported=(0 if token == "EXPORT_NO_FRAMES" else None), result=token)
                proc, receipt, _ = self.run_leg("bachelor", spec, token=(token, exit_code), extra=["-Backend", "cpu"])
                self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
                self.assertNotEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
                self.assertEqual(receipt["outcome"], want, receipt["outcomeDetail"])
                if want == "FAIL":
                    self.assertEqual(receipt["verification"]["status"], "VERIFIED_OFFLINE_TEST", receipt["outcomeDetail"])

    def test_r7_i2_the_receipt_validator_holds_every_nested_stage_to_processed16_totals_frames(self) -> None:
        """Dot I2-EXPORT-NESTED-STAGE-WINDOW through the WRITER (Invoke-VenueLeg) and the receipt VALIDATOR (Test-DvReceiptValid): a nested rendered stage on an unrelated
        window is an INVALID receipt at write time (never PASS), and a good receipt whose hashed stage log is rewritten to that window (every sha256 claim updated to
        match) does not verify. A matching-frame control verifies, and a permitted absent stage verifies."""
        spec = self.export_spec(EXPORT_LEGS[1], maxframes=30)
        everything = {s: range(30) for s in ("raw_uint16_disk_read", "raw_uint16_decompress", "raw_uint16", "llrawproc", "debayered_frame", "processing", "processed16_total")}

        def log(stage: str, frames) -> str:
            over = dict(everything)
            over[stage] = frames
            return ExportEvidenceTests.stage_lines(over)
        # a good receipt first
        self.write_export_artifacts(30, fmt="prores")
        proc, good, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(good, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(good["outcome"], "PASS", good["outcomeDetail"])
        self.assertEqual(self.validate(good, spec)["status"], "VERIFIED_OFFLINE_TEST")
        for stage in ("raw_uint16", "raw_uint16_disk_read", "raw_uint16_decompress", "llrawproc"):
            for name, frames in (("singleton", [100]), ("disjoint", range(30, 60)), ("partial", range(15))):
                with self.subTest(stage=stage, scenario=name, via="writer"):
                    self.write_export_artifacts(30, fmt="prores", stage=log(stage, frames))
                    proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
                    self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
                    self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                    self.assertIn("EXPORT_STAGE_WINDOW_MISMATCH", receipt["outcomeDetail"])
        # the validator: the good receipt's hashed stage log rewritten, every claim updated to match
        evdir = Path(good["evidence"]["localEvidenceDir"])
        for stage, name, frames, want in (("llrawproc", "partial", range(15), "INVALID"), ("raw_uint16", "disjoint", range(30, 60), "INVALID"),
                                          ("llrawproc", "absent", [], "VERIFIED_OFFLINE_TEST"), ("llrawproc", "control", range(30), "VERIFIED_OFFLINE_TEST")):
            with self.subTest(stage=stage, scenario=name, via="validator"):
                work = self.tmp / f"ev-i2-{stage}-{name}"
                if work.exists():
                    shutil.rmtree(work)
                shutil.copytree(evdir, work)
                raw = log(stage, frames).encode("utf-8")
                (work / "export" / "stage-timing.log").write_bytes(raw)
                r = json.loads(json.dumps(good))
                r["export"]["files"]["stage-timing.log"] = hashlib.sha256(raw).hexdigest()
                got = self.validate(r, spec, evidence_dir=str(work))
                if want == "INVALID":
                    self.assertEqual(got["status"], "INVALID", got)
                else:
                    # an absent or matching nested stage changes the stage table, so the receipt's own table no longer equals the re-derived one: still never a PASS
                    # that verifies as the ORIGINAL receipt; the point here is that the reader itself raises no window problem
                    self.assertFalse(any("EXPORT_STAGE_WINDOW_MISMATCH" in x for x in got["reasons"]), got)

    def test_a_profile_missing_on_a_captured_run_is_invalid(self) -> None:
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30, profile=False)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("EXPORT_PROFILE_ABSENT", receipt["outcomeDetail"])

    def test_mutation_without_the_export_branch_the_happy_receipt_does_not_verify(self) -> None:
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        mutated = self.mutated_runner([("DualVenueRunner.psm1", "if ($null -ne $exportCheck) { return $exportCheck }", "if ($false) { return $exportCheck }")])
        got = self.validate(receipt, spec, dv=mutated)
        self.assertFalse(got["valid"], got)

    def test_mutation_without_the_export_hash_recheck_an_edited_export_file_is_not_caught_by_the_hash(self) -> None:
        """r6 (hosted CI): the sha256 recheck line exists twice in DualVenueRunner.psm1, so the anchor test in test_dual_venue_evidence.py now names the capture-evidence
        copy and this one names the export-evidence copy ($Invalid, then "export/$name"). The unmutated validator names the hash mismatch of a tampered export/batch.log;
        with only the export copy disabled it no longer does, so the export loop's recheck is what produces that reason."""
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        (Path(receipt["evidence"]["localEvidenceDir"]) / "export" / "batch.log").write_text(batch_log(29), encoding="utf-8")
        plain = self.validate(receipt, spec)
        self.assertEqual(plain["status"], "INVALID", plain)
        self.assertTrue(any("EVIDENCE_HASH_MISMATCH: export/batch.log" in x for x in plain["reasons"]), plain)
        anchor = 'if ((Get-DvSha256OfBytes $b) -cne $claim) { $Invalid.Add("EVIDENCE_HASH_MISMATCH: export/$name'
        mutated = self.mutated_runner([("DualVenueRunner.psm1", anchor, anchor.replace("if ((Get-DvSha256OfBytes $b) -cne $claim) {", "if ($false) {"))])
        got = self.validate(receipt, spec, dv=mutated)
        self.assertFalse(any("EVIDENCE_HASH_MISMATCH: export/batch.log" in x for x in got["reasons"]), got)


@requires_windows_pwsh
class ExportJobGeneratorTests(unittest.TestCase):
    def test_every_command_the_template_calls_is_embedded_and_nothing_is_copied(self) -> None:
        proc = run_pwsh(["-Command", f"$d = & '{EXPORT_JOB}' -DescribeTemplate; $d | ConvertTo-Json -Depth 4 -Compress"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        d = json.loads(proc.stdout)
        template = d["template"]
        called = set(re.findall(r"\b((?:[A-Z][a-z]+)+-(?:AttrCuda|GuiSmoke)[A-Za-z0-9]*)\b", template))
        embedded = set(d["embedded"])
        self.assertTrue(called, "the template calls the shared helpers")
        self.assertEqual(called - embedded - {"Test-GuiSmokeClipLength"}, set(), "a helper the job calls is not embedded")
        self.assertTrue(d["lengthGateEmbedded"], "the length gate file is embedded whole, as the smoke runner dot-sources it")
        src = EXPORT_JOB.read_text(encoding="utf-8")
        self.assertNotIn("function Resolve-AttrCudaOwnerClipParts", src, "the resolver is imported from the playback generator, never copied")
        self.assertIn("Resolve-AttrCudaOwnerClipParts", src)
        for token in ("EXPORT_CAPTURED", "EXPORT_EXIT_NONZERO", "EXPORT_TIMEOUT", "EXPORT_NO_FRAMES", "EXPORT_FRAMES_SHORT", "EXPORT_PROFILE_ABSENT",
                      "EXPORT_OUTPUT_MISMATCH", "EXPORT_TOOLING_ABSENT", "VENUE_NOT_QUIESCENT"):
            self.assertIn(token, template, token)
        for needle in ("MLVAPP_EXPORT_STAGE_PROFILER", "MLVAPP_EXPORT_STAGE_PROFILE_FILE", "MLVAPP_EXPORT_STAGE_PROFILE_BUILD_ID", "MLVAPP_STAGE_TIMING_FILE",
                       "--cdng-codec", "--rendered-codec", "--rendered-ffmpeg", "--max-frames", "QT_QPA_PLATFORM"):
            self.assertIn(needle, template, needle)
        self.assertNotIn("--rendered-resize", template)
        # the published set is the five records only: the job never copies an exported frame or .mov to the share
        self.assertIn("Remove-ExportOutput", template)
        self.assertNotRegex(template, r"Publish-AttrCudaFileCopy -Source \$outputs", "an exported output is never published")

    def test_the_embedding_closure_follows_a_two_word_verb_and_is_complete(self) -> None:
        """r2 (venue evidence, 12 of 12 export legs INVALID): the closure scan matched only one-word verbs, so ConvertTo-AttrCudaFileIdObject (called by the embedded
        Get-AttrCudaFileId) was never embedded and the venue job died at its first file-id read with 'ConvertTo-AttrCudaFileIdObject is not recognized' (agent exit 1,
        no summary.json, so no pre-load mean). Every AttrCuda function an embedded function calls, whatever its verb, is embedded or listed unresolved."""
        script = (f"$d = & '{EXPORT_JOB}' -DescribeTemplate\n"
                  f"Import-Module '{EXPORT_JOB.parent / 'AttrCudaArtifacts.psm1'}' -Force\n"
                  "$have = @($d.embedded) + @($d.unresolved)\n"
                  "$missing = [System.Collections.Generic.List[string]]::new()\n"
                  "foreach ($n in @($d.embedded)) {\n"
                  f"  $t = $null; foreach ($m in '{EXPORT_JOB.parent / 'AttrCudaArtifacts.psm1'}', '{EXPORT_JOB.parent / 'AttrCudaOwnerFootage.psm1'}') {{ try {{ $t = Get-AttrCudaEmbeddedFunctionSource -ModulePath $m -Name @($n); break }} catch {{ $t = $null }} }}\n"
                  "  if ($null -eq $t) { continue }\n"
                  "  foreach ($c in [regex]::Matches($t, '\\b((?:[A-Z][a-z]+)+-AttrCuda[A-Za-z0-9]*)\\b')) { if ($have -cnotcontains $c.Groups[1].Value) { $missing.Add(\"$n -> $($c.Groups[1].Value)\") } }\n"
                  "}\n"
                  "[ordered]@{ missing = @($missing); embedded = @($d.embedded) } | ConvertTo-Json -Compress")
        proc = run_pwsh(["-Command", script])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(got["missing"], [], got["missing"])
        self.assertIn("ConvertTo-AttrCudaFileIdObject", got["embedded"], "Get-AttrCudaFileId's callee must ride with it")

    def test_the_app_never_inherits_the_venue_agents_mlvapp_environment(self) -> None:
        """adversary r2 A1 (on c3768da1): the export child inherited the agent's environment, so MLVAPP_GPU_EXPORT=1 on a CUDA venue made the 'cpu' leg a GPU
        export under a cpu label. Every inherited MLVAPP_* variable is removed from the child's environment BEFORE the job sets its own (the profiler pair, the
        stage-timing pair), and the names removed are published in the summary."""
        proc = run_pwsh(["-Command", f"$d = & '{EXPORT_JOB}' -DescribeTemplate; $d.template"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        template = proc.stdout
        scrub = template.find("$psi.Environment.Keys")
        first_set = template.find("$psi.Environment['MLVAPP_")
        self.assertGreater(scrub, 0, "no scrub of the inherited environment")
        self.assertGreater(first_set, scrub, "the scrub must precede the job's own MLVAPP_* settings")
        self.assertIn("-like 'MLVAPP_*'", template[scrub - 200:scrub + 300])
        self.assertIn("Environment.Remove", template[scrub:first_set])
        self.assertIn("mlvappEnvScrubbed", template)

    def test_the_quiescence_record_carries_the_names_the_leg_batch_reads(self) -> None:
        """r2: the leg batch reads cpuQuiescence.timeMeanPercent from summary.json (VenueChain.psm1 Get-VenueLegPreLoadMean, the playback job's name); an export
        summary that spells it cpuTimeMean reads UNKNOWN and every leg is QUIET_PRELOAD_UNKNOWN, never counted."""
        proc = run_pwsh(["-Command", f"$d = & '{EXPORT_JOB}' -DescribeTemplate; $d.template"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        template = proc.stdout
        for field in ("utilitySamples", "utilityMeanPercent", "timeSamples", "timeMeanPercent", "timeUnknown", "thresholdPercent", "pass"):
            self.assertTrue(re.search(rf"\b{field}\s*=", template), field)
        for old in ("cpuTimeMean = $avgTime", "cpuUtilityMean = $avgUtility", "cpuTimeSamples = $cpuTimeSamples", "cpuTimeUnknown = $cpuTimeUnknown",
                    "cpuThresholdPercent = $cpuThresholdPercent"):
            self.assertFalse(old in template, f"the export quiescence record must use the playback summary's field names, not {old}")

    def test_the_leg_batch_finds_the_export_summary_where_the_export_job_publishes_it(self) -> None:
        """r3 (venue proof 2026-11-17, receipt 638671c5): the export ran (480 frames) and its summary carried cpuQuiescence.timeMeanPercent=8.5, yet the leg read
        preLoadMean=UNKNOWN / QUIET_PRELOAD_UNKNOWN. A playback leg's summary.json is <evidence>\\<receiptId>\\summary.json; the export evidence is published under
        <evidence>\\<receiptId>\\export\\summary.json, and Get-VenueLegPreLoadMean looked only at the first. Both readers (the module's and the chain driver's
        own copy, which now delegates) read either layout; a malformed or field-less summary is still $null, never 0."""
        mod = DV / "VenueChain.psm1"
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "receipts"
            ev = Path(d) / "evidence"
            cases = {"play-1": ("", 7.25), "exp-1": ("export", 8.5), "bad-1": ("export", None), "none-1": (None, None)}
            for rid, (sub, mean) in cases.items():
                receipt = root / CARD / "leg-x" / "bachelor" / f"{rid}.json"
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text("{}", encoding="utf-8")
                if sub is None:
                    continue
                folder = ev / rid / sub if sub else ev / rid
                folder.mkdir(parents=True, exist_ok=True)
                body = {"cpuQuiescence": {"timeMeanPercent": mean}} if mean is not None else {"cpuQuiescence": {"cpuTimeMean": 3.0}}
                (folder / "summary.json").write_text(json.dumps(body), encoding="utf-8")
            script = (f"Import-Module '{mod}' -Force -DisableNameChecking; $r = [ordered]@{{}}; "
                      + "".join(f"$r['{rid}'] = Get-VenueLegPreLoadMean -ReceiptPath '{root / CARD / 'leg-x' / 'bachelor' / (rid + '.json')}'; " for rid in cases)
                      + "$r | ConvertTo-Json -Compress")
            proc = run_pwsh(["-Command", script])
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            got = json.loads(proc.stdout)
        self.assertEqual(got["play-1"], 7.25, "the playback layout must keep working")
        self.assertEqual(got["exp-1"], 8.5, "the export layout (<evidence>\\<id>\\export\\summary.json) must be read")
        self.assertIsNone(got["bad-1"], "a summary without timeMeanPercent is UNKNOWN, never 0")
        self.assertIsNone(got["none-1"])

    def test_the_chain_driver_does_not_keep_its_own_copy_of_the_preload_reader(self) -> None:
        """r3: Invoke-VenueChain.ps1 carried a second copy of the same reader, with the same wrong path. It delegates to the module's one function."""
        text = (DV / "Invoke-VenueChain.ps1").read_text(encoding="utf-8")
        start = text.index("function Get-LegPreLoadMean")
        body = text[start:text.index("function Add-Result", start)]
        self.assertIn("Get-VenueLegPreLoadMean", body)
        self.assertNotIn("summary.json", body, "a second copy of the summary path is how the two drifted")

    def test_the_time_budget_fits_the_shipped_legs_and_long_windows(self) -> None:
        """adversary r1 A1: the export's budget is identity read + the app's re-read + the export wait; the shipped M16-1243 legs (two parts, 3.4 GB,
        480 frames) and a 1000-frame window generate, and the job timeout holds every allowance."""
        m1243 = 2221399552 + 1174057472
        for frames in (480, 360, 1000, 4000):
            with self.subTest(frames=frames):
                proc = run_pwsh(["-Command", f"& '{EXPORT_JOB}' -DescribeTimeBudgetBytes {m1243} -MaxFrames {frames} | ConvertTo-Json -Compress"])
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                b = json.loads(proc.stdout)
                self.assertGreaterEqual(b["exportTimeoutSec"], max(900, 4 * frames))
                self.assertGreaterEqual(b["jobTimeoutSec"], b["identityReadSec"] + b["exportTimeoutSec"])

    def test_r7_a4_a_window_whose_budget_passes_the_agent_cap_is_refused_typed_before_the_runner_sees_it(self) -> None:
        """adversary r6 A4 (and r5 A3): maxFrames=20000 on the 3.4 GB clip gave jobTimeoutSec 87144; the runner adds 120 and submits 87264, UmRunDrop refuses past 86400
        (UMRUN_JOB_TIMEOUT_INVALID) and the leg ended UNRESOLVED after a lease was spent. The generator now refuses a budget whose submitted timeout passes the cap, with
        a typed reason, and never lets an int cast of an out-of-range figure throw."""
        m1243 = 2221399552 + 1174057472
        for frames, bytes_, ok in ((4000, m1243, True), (19000, m1243, True), (20000, m1243, False), (100000, m1243, False), (480, 1099511627776, False)):
            with self.subTest(frames=frames, clipBytes=bytes_):
                proc = run_pwsh(["-Command", f"try {{ & '{EXPORT_JOB}' -DescribeTimeBudgetBytes {bytes_} -MaxFrames {frames} -ErrorAction Stop | ConvertTo-Json -Compress }} catch {{ 'REFUSED ' + $_.Exception.Message }}"])
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                if ok:
                    b = json.loads(proc.stdout)
                    self.assertLessEqual(b["jobTimeoutSec"] + 120, 86400, b)
                else:
                    self.assertTrue(proc.stdout.startswith("REFUSED DUAL_VENUE_EXPORT_TIME_BUDGET_EXCEEDED"), proc.stdout + proc.stderr)

    def test_r8_a3_the_preflight_budget_mode_needs_no_receipt_build_or_output_and_stops_before_them(self) -> None:
        """adversary r7 A3: the lease batch asks the generator for the job budget BEFORE any lease, so -PreflightBudgetOnly takes only the clip, format and window. It must not
        ask for the receipt / build / out file (those are the leg's, written after the lease), and its return must come before anything reads them (git, the receipt)."""
        text = EXPORT_JOB.read_text(encoding="utf-8")
        self.assertIn("[switch]$PreflightBudgetOnly", text)
        gate = text.index("if ($PreflightBudgetOnly) {\n    if ($MaxFrames")
        self.assertLess(gate, text.index("& git -C $RepoRoot cat-file"), "the preflight returns before git is asked")
        self.assertLess(gate, text.index("$receiptBytes = [IO.File]::ReadAllBytes((Resolve-Path -LiteralPath $ReceiptPath"), "and before the receipt is read")
        self.assertLess(gate, text.index("$timeBudget = Get-ExportJobTimeBudget -ClipBytes $clipBytes"), "the generation below makes the SAME budget call")
        self.assertIn("Get-ExportJobTimeBudget -ClipBytes $preflightBytes -Frames $MaxFrames", text, "and the preflight asks that SAME function")
        proc = run_pwsh(["-File", str(EXPORT_JOB), "-PreflightBudgetOnly", "-ClipId", "M16-1243", "-Format", "prores", "-MaxFrames", "20000"])
        out = proc.stdout + proc.stderr
        for absent in ("DUAL_VENUE_EXPORT_ARG_MISSING", "DUAL_VENUE_EXPORT_RECEIPT_ABSENT"):
            self.assertNotIn(absent, out, "the preflight mode never asks for the leg's own arguments")

    def test_r8_a2_a_timeout_whose_kill_is_not_confirmed_is_its_own_typed_invalid_token(self) -> None:
        """adversary r7 pass A2: Kill($true) and Kill() errors were swallowed and the 30 s wait's result discarded, so an unkillable export tree published an ordinary
        EXPORT_TIMEOUT (a FAIL claim about the product) after reading and deleting output a live process still held. The job now types it EXPORT_KILL_UNCONFIRMED (exit 48),
        which the runner reads as INVALID, never PASS or FAIL."""
        text = EXPORT_JOB.read_text(encoding="utf-8")
        self.assertIn("$killConfirmed = $true", text)
        wait = text.index("if (-not $app.WaitForExit(30000)) { $killConfirmed = $false }")
        self.assertIn("catch { $killConfirmed = $false", text[text.index("try { $app.Kill($true) }"):wait])
        self.assertIn("if ($timedOut -and -not $killConfirmed) { 'EXPORT_KILL_UNCONFIRMED' }", text)
        self.assertIn("EXPORT_KILL_UNCONFIRMED = 48", text)
        self.assertIn("killConfirmed = $killConfirmed", text, "the summary records whether the tree was confirmed dead")
        # (pass 10 A3) a log or stage record a survivor holds is read and copied best effort, so no file access can cost the typed terminal
        self.assertIn("$logText = try { if (Test-Path -LiteralPath $batchLog -PathType Leaf) { [IO.File]::ReadAllText($batchLog) } else { '' } } catch {", text)
        self.assertIn("try { if (Test-Path -LiteralPath $batchLog -PathType Leaf) { [void](Publish-AttrCudaFileCopy -Source $batchLog", text)
        self.assertIn("try { if (Test-Path -LiteralPath $stageEvidence -PathType Leaf) { [void](Publish-AttrCudaFileCopy -Source $stageEvidence", text)
        self.assertLess(text.index("$killConfirmed = $true"), text.index("$manifest.deleted = $(try { Remove-ExportOutput }"), "the kill is settled before any output is read or deleted")
        proc, got = run_pwsh_json("$e = [pscustomobject]@{ metrics = [pscustomobject]@{}; stages = $null; problems = @(); classMismatch = $false; classDetail = $null }\n"
                                  "[ordered]@{ unconfirmed = (Get-DvExportOutcome -ResultToken 'EXPORT_KILL_UNCONFIRMED' -ExitCode 48 -Evidence $e -Criteria @()).outcome; "
                                  "timeout = (Get-DvExportOutcome -ResultToken 'EXPORT_TIMEOUT' -ExitCode 40 -Evidence $e -Criteria @()).outcome } | ConvertTo-Json -Compress")
        self.assertEqual(got, {"unconfirmed": "INVALID", "timeout": "FAIL"}, proc.stdout + proc.stderr)

    def test_r11_adversary_a3_the_stage_record_size_read_cannot_cost_the_typed_kill_terminal(self) -> None:
        """adversary r11 A3: after an unconfirmed kill the stage record can be removed or locked between Test-Path and Get-Item; that unguarded read threw into the finally and bypassed
        EXPORT_KILL_UNCONFIRMED, the summary and exit 48. The read is best effort like every other file access on that path."""
        text = EXPORT_JOB.read_text(encoding="utf-8")
        self.assertNotIn("$stagePresent = (Test-Path -LiteralPath $stageEvidence -PathType Leaf) -and ((Get-Item -LiteralPath $stageEvidence).Length -gt 0)", text)
        self.assertRegex(text, r"\$stagePresent = try \{ \(Test-Path -LiteralPath \$stageEvidence -PathType Leaf\) -and \(\(Get-Item -LiteralPath \$stageEvidence -ErrorAction Stop\)\.Length -gt 0\) \} catch \{")
        self.assertLess(text.index("$stagePresent = try {"), text.index("$result = $(if ($timedOut -and -not $killConfirmed)"), "the read is settled before the result is typed")

    def test_r13_adversary_a3_the_evidence_writes_cannot_cost_the_typed_kill_terminal(self) -> None:
        """adversary r13 A3: after an unconfirmed kill a survivor may hold output-manifest.json or summary.json without sharing; the unguarded Save-Json / Write-ExportSummary threw through
        the finally before RESULT=EXPORT_KILL_UNCONFIRMED and exit 48. On that terminal both writes are best effort; on every other terminal they stay fatal."""
        text = EXPORT_JOB.read_text(encoding="utf-8")
        self.assertIn("$evidenceBestEffort = ($timedOut -and -not $killConfirmed)", text)
        self.assertRegex(text, r"try \{ Save-Json \$manifest \(Join-Path \$Pub 'output-manifest\.json'\) \} catch \{ if \(-not \$evidenceBestEffort\) \{ throw \}")
        self.assertRegex(text, r"\} catch \{ if \(-not \$evidenceBestEffort\) \{ throw \}; Write-JobTrace \"step export summary write error=")
        tail = text[text.index("$evidenceBestEffort = "):]
        self.assertLess(tail.index("Write-ExportSummary $result"), tail.index('Write-Output "RESULT=$result'), "the summary write is settled before the RESULT line")
        self.assertNotRegex(text, r"(?m)^    Save-Json \$manifest \(Join-Path \$Pub 'output-manifest\.json'\)\s*$")

    def test_every_schema_valid_quiescence_threshold_binds(self) -> None:
        """adversary r1 A5: the schema admits any threshold in (0, 100]; the generator binds each one (0.05 was refused by ValidateRange(0.1, 100))."""
        for value, ok in (("0.05", True), ("100", True), ("0", False), ("100.5", False)):
            with self.subTest(value=value):
                proc = run_pwsh(["-Command", f"try {{ & '{EXPORT_JOB}' -DescribeTemplate -CpuQuiescenceThresholdPercent {value} -ErrorAction Stop | Out-Null; 'BOUND' }} catch {{ 'REFUSED' }}"])
                self.assertEqual("BOUND" in proc.stdout, ok, proc.stdout + proc.stderr)
                self.assertEqual("REFUSED" in proc.stdout, not ok, proc.stdout + proc.stderr)

    def test_the_emitted_job_parses_with_every_token_substituted(self) -> None:
        proc = run_pwsh(["-Command", f"$d = & '{EXPORT_JOB}' -DescribeTemplate\n"
                         "$t = [regex]::Replace($d.template, '__(?:MAX_FRAMES|PLAY_SECONDS|EXPORT_TIMEOUT_SEC|CPU_QUIESCENCE_THRESHOLD_PERCENT)__', '20')\n"
                         "$t = $t.Replace('__DUAL_ISO_EXPECTED__', '$true').Replace('__EMBEDDED_FUNCTIONS__', '').Replace('__SHARED_FUNCTIONS__', '')\n"
                         "$t = [regex]::Replace($t, '__[A-Z0-9_]+__', 'x')\n"
                         "$errs = $null; [void][System.Management.Automation.Language.Parser]::ParseInput($t, [ref]$null, [ref]$errs)\n"
                         "[ordered]@{ errors = @($errs | ForEach-Object { $_.Message }); unresolved = @($d.unresolved); shared = @($d.shared) } | ConvertTo-Json -Compress"])
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        got = json.loads(proc.stdout)
        self.assertEqual(got["errors"], [], got)
        self.assertEqual(sorted(got["shared"]), sorted(["Assert-UnderMlvTmp", "Save-Json", "Write-JobTrace", "Get-Sha", "Get-Mean"]))

    def test_the_generator_refuses_a_non_cpu_backend_and_a_bad_format(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "j.job.ps1"
            for flag, value in (("-Backend", "cuda"), ("-Format", "h264")):
                args = ["-File", str(EXPORT_JOB), "-SourceCommit", "e" * 40, "-BuildManifestSha256", "ab" * 32, "-ClipId", "M16-1243", "-Venue", "bachelor",
                        "-Backend", "cpu", "-Format", "prores", "-MaxFrames", "480", "-ReceiptPath", str(out), "-PlaySeconds", "20", "-OutFile", str(out)]
                args[args.index(flag) + 1] = value
                proc = run_pwsh(args)
                self.assertNotEqual(proc.returncode, 0, flag)
                self.assertIn(flag.lstrip("-"), proc.stdout + proc.stderr)
                self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
