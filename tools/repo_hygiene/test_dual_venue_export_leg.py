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
        stage["dng_compress_encode_ms"] = 10.0
        stage["producer_frame_ms"] = 50.0 + (i % 3)
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
                          ("M16-1456", 10, 360, False, "cdng-lossless"), ("M16-1456", 10, 360, False, "prores")])
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


@requires_windows_pwsh
class ExportEvidenceTests(unittest.TestCase):
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
        self.assertAlmostEqual(st["rows"]["raw_read"]["share"], 4.0 / 51.0, places=4)
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
                               exported: int | None = None, result: str = "EXPORT_CAPTURED") -> None:
        a = self.artifacts
        if a.exists():
            shutil.rmtree(a)
        a.mkdir(parents=True)
        exported = frames if exported is None else exported
        summary = {"schema": "export-stage-job.v1", "result": result, "legType": "export", "format": fmt, "maxFrames": frames,
                   "dualIsoExpected": True, "clipId": "M16-1243", "backend": "cpu", "declaredVenue": venue, "venue": venue,
                   "sourceCommit": self.sha, "buildManifestSha256": self.build_sha, "receiptSha256": leg(EXPORT_LEGS[0])["export"]["receiptSha256"],
                   "exportWallSeconds": 30.0, "appExitCode": 0, "timedOut": False, "fixtureRehearsal": False}
        (a / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (a / "batch.log").write_text(batch_log(exported), encoding="utf-8")
        if profile:
            (a / "export-profile.json").write_text(cdng_profile(frames, build_id=self.sha), encoding="utf-8")
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
