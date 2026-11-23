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

import functools
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
        stage["llrawproc_other_ms"] = 4.0   # EXPORT-CDNG-TOTAL-CONTAINMENT-1: dng.c:579-604 sets other = total - the seven timed children (6 x 1 + 30 = 36), so 36 + 4 = the 40 ms total
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


# EXPORT-RENDER-PIPE-TIMING-1: the stages a rendered export emits from that card on (src/debug/StageTiming.h MLV_STAGE_LLRAWPROC_DUAL_ISO, MLV_STAGE_EXPORT_*):
# llrawproc_dual_iso (inside llrawproc) and export_pipe_write (plus export_scale when the frame plan is scaled) per frame, export_pipe_drain and export_verify once per
# clip on its last frame. llrawproc is 31 ms here so a 30 ms dual-ISO run fits inside it (raw_uint16 9 + llrawproc 31 = 40 still fits debayered_frame).
PIPE_CRITERIA = ({"metric": "dualIsoClassAgrees", "op": "eq", "value": True}, {"metric": "renderPipeTimed", "op": "eq", "value": True})


def piped_stage_log(frames: int, dual_iso_ms: float = 30.0, llrawproc_ms: float = 31.0, pipe_ms: float = 10.0, drain_ms: float = 100.0, verify_ms: float = 200.0,
                    drain_count: int = 1, verify_count: int = 1, pipe_frames=None, scale_ms: float | None = None, dual_iso: bool = True) -> str:
    out = []
    pipe_frames = range(frames) if pipe_frames is None else pipe_frames
    for i in range(frames):
        for name, ms in (("raw_uint16_disk_read", 3.0), ("raw_uint16_decompress", 5.0), ("raw_uint16", 9.0), ("llrawproc", llrawproc_ms),
                         ("debayered_frame", 40.0), ("processing", 15.0), ("processed16_total", 56.0)):
            out.append(f"[mlv-stage-timing] frame={i} stage={name} ms={ms:.3f}")
            if name == "llrawproc" and dual_iso:
                out.append(f"[mlv-stage-timing] frame={i} stage=llrawproc_dual_iso ms={dual_iso_ms:.3f}")
        if scale_ms is not None:
            out.append(f"[mlv-stage-timing] frame={i} stage=export_scale ms={scale_ms:.3f}")
        if i in pipe_frames:
            out.append(f"[mlv-stage-timing] frame={i} stage=export_pipe_write ms={pipe_ms:.3f}")
    out += [f"[mlv-stage-timing] frame={frames - 1} stage=export_pipe_drain ms={drain_ms:.3f}"] * drain_count
    out += [f"[mlv-stage-timing] frame={frames - 1} stage=export_verify ms={verify_ms:.3f}"] * verify_count
    return "\n".join(out) + "\n"


def with_pipe_stages(log: str, frames: int, dual_iso_ms: float = 5.0) -> str:
    """EXPORT-RENDER-PIPE-TIMING-1 forced companion: a test that judges a hand-written log under the SHIPPED ProRes criteria (which now gate dualIsoClassAgrees and
    renderPipeTimed) appends what a build of that card emits: a small dual-ISO figure on every frame, a 1 ms pipe write per frame, one drain and one verify.
    (r2, adversary A4 on a6f48506: on a frame that carries llrawproc the dual-ISO record is written right after it, as video_mlv.c writes it; a frame without llrawproc gets it appended.)"""
    # (r2 fix 1, pass 4 A2: and the pipe write closes each frame, after its last render record, as the frame loop writes it)
    # (r2 fix 2, adversary A1 on c2da7e24: and processed16_total is the render's LAST note, video_mlv.c:7656-7661, so it is moved to the end of its frame's block)
    out, paired, written = [], set(), set()
    blocks: list = []
    for l in log.splitlines():
        m = re.match(r"\[mlv-stage-timing\] frame=(\d+) ", l)
        f = int(m.group(1)) if m else None
        if blocks and f is not None and blocks[-1][0] == f:
            blocks[-1][1].append(l)
        else:
            blocks.append((f, [l]))
    for f, ls in blocks:
        if f is None or f >= frames or f in written:
            out += ls
            continue
        for line in ls:
            if " stage=processed16_total " in line:
                continue
            out.append(line)
            if " stage=llrawproc " in line:
                out.append(f"[mlv-stage-timing] frame={f} stage=llrawproc_dual_iso ms={dual_iso_ms:.3f}")
                paired.add(f)
        if f not in paired:
            out.append(f"[mlv-stage-timing] frame={f} stage=llrawproc_dual_iso ms={dual_iso_ms:.3f}")
            paired.add(f)
        out += [l for l in ls if " stage=processed16_total " in l]
        out.append(f"[mlv-stage-timing] frame={f} stage=export_pipe_write ms=1.000")
        written.add(f)
    per_frame = "".join(f"[mlv-stage-timing] frame={i} stage=llrawproc_dual_iso ms={dual_iso_ms:.3f}\n" for i in range(frames) if i not in paired)
    per_frame += "".join(f"[mlv-stage-timing] frame={i} stage=export_pipe_write ms=1.000\n" for i in range(frames) if i not in written)
    return "\n".join(out) + "\n" + per_frame + f"[mlv-stage-timing] frame={frames - 1} stage=export_pipe_drain ms=1.000\n[mlv-stage-timing] frame={frames - 1} stage=export_verify ms=1.000\n"


def batch_log(exported: int, skipped: int = 0, elapsed: float = 20.0, verified: int | None = None, scaled: bool | None = False) -> str:
    lines = ["[BATCH] Start"]
    if verified is not None:
        # (EXPORT-RENDER-PIPE-TIMING-1 r1 fix 2: the rendered runner's own RENDER_START line, BatchRunner.cpp, which declares whether the frame plan is scaled; scaled=None omits it)
        # (r2, adversary A5 on a6f48506: in the order BatchRunner.cpp writes them: RENDER_START before the frames, RENDER_VERIFIED after the verify, DONE last)
        if scaled is not None:
            lines.append(f"[BATCH] RENDER_START view-0 codec=prores container=mov source=1x1 output=1x1 scaled={'true' if scaled else 'false'} fps=24 frames={exported} "
                         f"cutIn=1 cutOut={exported} out=view-0.mov")
        lines.append(f"[BATCH] RENDER_VERIFIED view-0 decoded_frames={verified} decoded_duration=10.000 decoded_frame_bytes=1 dimensions=1x1 bytes=9 "
                     "container_probe=ffmpeg.exe probe_source=ffmpeg-i codec=prores container=mov sar=1:1 dar=16:9")
    lines.append(f"[BATCH] DONE view-0 exported={exported} skipped={skipped} elapsed={elapsed:.1f}")
    return "\n".join(lines) + "\n"


def output_manifest(count: int, kind: str = "dng") -> str:
    # r19 (sol r18 B3): the endpoint records carry their byte size, as export-stage-job.ps1 writes them, and agree with totalBytes (one file: first and last are that file)
    first = {"name": "a", "bytes": 1000, "sha256": "1" * 64} if count > 0 else None
    last = ({"name": "a", "bytes": 1000, "sha256": "1" * 64} if count == 1 else {"name": "b", "bytes": 1000, "sha256": "2" * 64}) if count > 0 else None
    return json.dumps({"schema": "export-stage-output-manifest.v1", "kind": kind, "count": count, "totalBytes": count * 1000,
                       "first": first, "last": last, "deleted": True})


EVIDENCE_CALL = ("Get-DvExportEvidence -Format $c.format -MaxFrames ([int]$c.maxFrames) -DualIsoExpected ([bool]$c.dualIsoExpected) -ExpectedBuildId $c.buildId "
                 "-ProfileText ([string]$c.profile) -StageLogText ([string]$c.stage) -BatchLogText ([string]$c.batch) -OutputManifestText ([string]$c.manifest)")


def evidence_cases(cases: list[dict]):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "cases.json"
        path.write_text(json.dumps(cases), encoding="utf-8")
        return run_pwsh_json("$cases = Get-Content -Raw -LiteralPath $env:DVE_CASES -Encoding utf8 | ConvertFrom-Json\n"
                             f"@($cases | ForEach-Object {{ $c = $_; $e = {EVIDENCE_CALL}; "
                             "$o = Get-DvExportOutcome -ResultToken $c.token -ExitCode ([int]$c.exit) -Evidence $e -Criteria @($c.criteria); "
                             "[ordered]@{ metrics = $e.metrics; problems = @($e.problems); classMismatch = $e.classMismatch; stages = $e.stages; outcome = $o.outcome; detail = $o.detail; "
                             "containmentAdvisory = @($e.containmentAdvisory) } }) "
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
        # EXPORT-RENDER-PIPE-TIMING-1: a log with no pipe stage is a build before that card, and the text says what such a build leaves untimed
        self.assertIn("a build before EXPORT-RENDER-PIPE-TIMING-1", ps["untimed"])
        self.assertIn("pipe write", ps["untimed"])
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

    def test_r19_b3_a_manifest_total_that_contradicts_its_endpoint_records_is_invalid(self) -> None:
        """EXPORT-VENUE-LEG-1 r19 (sol r18 B3 = pass20-A3): totalBytes was checked for type and sign only, never against the first / last records the job writes beside it, so a MOV
        manifest count=1 totalBytes=1 naming one 1000-byte file, and a DNG manifest count=30 totalBytes=1 with two distinct 1000-byte endpoints, read PASS with outputBytes=1. One file
        is the whole total (first and last are that file); more files are two distinct endpoints inside the total. Anything else is INVALID EXPORT_OUTPUT_MISMATCH."""
        def manifest(count: int, kind: str, total: int, first, last) -> str:
            return json.dumps({"schema": "export-stage-output-manifest.v1", "kind": kind, "count": count, "totalBytes": total, "first": first, "last": last, "deleted": True})
        one = {"name": "view-0.mov", "bytes": 1000, "sha256": "1" * 64}
        a = {"name": "000000.dng", "bytes": 1000, "sha256": "1" * 64}
        b = {"name": "000029.dng", "bytes": 1000, "sha256": "2" * 64}
        cases = [prores_case(manifest=manifest(1, "mov", 1, one, one)),                                             # 0 sol repro: MOV total 1, a 1000-byte file
                 cdng_case(manifest=manifest(30, "dng", 1, a, b)),                                                  # 1 sol repro: DNG total 1, two 1000-byte endpoints
                 prores_case(manifest=manifest(1, "mov", 1000, one, {**one, "name": "other.mov"})),                 # 2 one file, two names
                 cdng_case(manifest=manifest(30, "dng", 30000, a, a)),                                              # 3 thirty files, one endpoint twice
                 cdng_case(manifest=manifest(30, "dng", 30000, a, None)),                                           # 4 no last record
                 cdng_case(manifest=manifest(30, "dng", 30000, a, {**b, "bytes": "1000"})),                         # 5 a byte size that is no integer
                 prores_case(manifest=manifest(1, "mov", 1000, {**one, "sha256": "a" * 64}, {**one, "sha256": "b" * 64})),  # 6 adversary pass 22 A2: one file, two hashes
                 cdng_case(manifest=manifest(30, "dng", 30000, a, {**b, "sha256": "not a hash"})),                  # 7 an endpoint with no sha256
                 cdng_case(manifest=manifest(30, "dng", 2000, a, b)),                                               # 8 adversary pass 23 A1: endpoints use the whole total
                 cdng_case(manifest=manifest(30, "dng", 30000, {**a, "bytes": 0}, {**b, "bytes": 0})),              # 9 pass 23 A1: empty endpoint files
                 prores_case(manifest=manifest(1, "mov", 1000, one, one)),                                          # 10 control: one consistent file
                 cdng_case(manifest=manifest(30, "dng", 30000, a, b)),                                              # 11 control: sol's consistent DNG
                 cdng_case(manifest=manifest(30, "dng", 2028, a, b))]                                               # 12 control: the tightest consistent total (1 byte each between)
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(10):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "INVALID", got[i])
                self.assertTrue(any(p.startswith("EXPORT_OUTPUT_MISMATCH") for p in got[i]["problems"]), got[i])
        for i in (10, 11, 12):
            with self.subTest(case=i):
                self.assertEqual(got[i]["outcome"], "PASS", got[i])
                self.assertEqual(got[i]["problems"], [], got[i])

    def test_r20_a1_endpoints_near_int64_max_cannot_use_up_the_total_through_a_double_rounding(self) -> None:
        """EXPORT-VENUE-LEG-1 r20 (adversary pass 25 A1 on d65eec9b): first.bytes 9223372036854775806 + last.bytes 1 + one byte for each of the 28 files between overflowed Int64
        into a double, whose rounding read the sum as within totalBytes 9223372036854775807, so a manifest whose endpoints use the whole total read PASS. The sums are exact now."""
        top = 9223372036854775807
        doc = {"schema": "export-stage-output-manifest.v1", "kind": "dng", "count": 30, "totalBytes": top, "deleted": True,
               "first": {"name": "000000.dng", "bytes": top - 1, "sha256": "1" * 64}, "last": {"name": "000029.dng", "bytes": 1, "sha256": "2" * 64}}
        proc, got = evidence_cases([cdng_case(manifest=json.dumps(doc))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any(p.startswith("EXPORT_OUTPUT_MISMATCH") for p in got[0]["problems"]), got[0])

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
                return prores_case(maxFrames=n, dualIsoExpected=True, stage=with_pipe_stages(stage_log(n), n), batch=batch_log(n, verified=n, elapsed=elapsed),
                                   manifest=output_manifest(1, "mov"), criteria=shipped[fmt])
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
        # (EXPORT-CDNG-STAGE-CONTAINMENT-1: gating again, as r11 shipped it; r18 had made it advisory)
        for i, inside in ((0, "producer_frame"), (1, "llrawproc")):
            with self.subTest(case=i):
                assert_containment_gates(self, got[i], "the disjoint stages", f"inside {inside} mean")
        self.assertEqual(got[2]["outcome"], "PASS", got[2])
        self.assertEqual(got[2]["containmentAdvisory"], [], got[2])

    def test_r11_adversary_a1_nested_render_stages_are_held_inside_debayered_frame_when_raw_uint16_is_absent(self) -> None:
        """adversary r11 A1: raw_uint16 is a stage the app may not publish, which skipped both child pairs and their sum group: raw_uint16_disk_read and raw_uint16_decompress at 900 ms each
        inside a 41 ms debayered_frame read PASS with 16x shares. The nested stages are held inside debayered_frame whether or not raw_uint16 is there."""
        def log(disk: float, decomp: float, n: int = 480) -> str:
            return "".join(f"[mlv-stage-timing] frame={i} stage={s} ms={ms:.3f}\n" for i in range(n)
                           for s, ms in (("processed16_total", 56.0), ("debayered_frame", 41.0), ("processing", 15.0), ("raw_uint16_disk_read", disk), ("raw_uint16_decompress", decomp)))
        criteria = leg("m16-1243-export-prores")["criteria"]["acceptance"]["cpu"]

        def case(disk: float, decomp: float) -> dict:
            return prores_case(maxFrames=480, dualIsoExpected=True, stage=with_pipe_stages(log(disk, decomp), 480), batch=batch_log(480, verified=480, elapsed=60.0), criteria=criteria)
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
            return prores_case(maxFrames=480, dualIsoExpected=True, stage=with_pipe_stages(log(rows), 480), batch=batch_log(480, verified=480, elapsed=60.0), criteria=criteria)
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
        assert_containment_gates(self, got[0], "the disjoint stages", "inside producer_frame mean")   # (EXPORT-CDNG-STAGE-CONTAINMENT-1: gating again)
        self.assertEqual(got[1]["outcome"], "PASS", got[1])
        self.assertEqual(got[1]["containmentAdvisory"], [], got[1])

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
        # (r17, adversary pass17 A1: shared_lock accumulates the refine and publish times, llrawproc.c:6131 / :6293, so a valid control publishes it at least their sum)
        control = profile({**base, "llrawproc_publish_lock_ms": 10.0, "llrawproc_dualiso_refine_lock_ms": 10.0, "llrawproc_shared_lock_ms": 20.0})
        proc, got = evidence_cases([cdng_case(dualIsoExpected=True, profile=p, criteria=[]) for p in (publish, refine, no_total, control)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in (0, 1, 2):
            with self.subTest(case=i):
                assert_containment_gates(self, got[i], "the disjoint stages")   # (EXPORT-CDNG-STAGE-CONTAINMENT-1: gating again)
        self.assertEqual(got[3]["outcome"], "PASS", got[3])
        self.assertEqual(got[3]["containmentAdvisory"], [], got[3])

    def test_r16_pass15_a1_every_disjoint_llrawproc_child_and_the_outside_lock_times_are_held_together(self) -> None:
        """adversary r15 A1 (= pass14 A1): the held frontier was only dual_iso + the two locks, so a timed child beside them was unjudged: dark_frame 80 + dual_iso 1 + publish_lock 80 (161 ms of
        disjoint work: llrawproc.c times the seven children in disjoint windows and takes the refine lock (:6063-6130) and the publish lock (:6288-6292) outside every one of them) inside a 100 ms
        llrawproc read PASS. The seven timed children plus the two locks are held together inside llrawproc and, with the producer's other members, inside producer_frame; llrawproc_ms may be absent."""
        children = ("llrawproc_dark_frame_ms", "llrawproc_vertical_stripes_ms", "llrawproc_focus_pixels_ms", "llrawproc_bad_pixels_ms", "llrawproc_pattern_noise_ms", "llrawproc_chroma_smooth_ms")

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
        profiles = []
        for child in children:
            profiles.append(profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, child: 80.0, "llrawproc_dual_iso_ms": 1.0, "llrawproc_other_ms": 19.0, "llrawproc_publish_lock_ms": 80.0}))
            profiles.append(profile({"producer_frame_ms": 100.0, child: 80.0, "llrawproc_dual_iso_ms": 1.0, "llrawproc_other_ms": 19.0, "llrawproc_publish_lock_ms": 80.0}, drop_total=True))
        refine = profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_dark_frame_ms": 80.0, "llrawproc_dual_iso_ms": 1.0, "llrawproc_other_ms": 19.0, "llrawproc_dualiso_refine_lock_ms": 80.0})
        control = profile({"producer_frame_ms": 120.0, "llrawproc_ms": 100.0, "llrawproc_dark_frame_ms": 20.0, "llrawproc_dual_iso_ms": 1.0, "llrawproc_other_ms": 79.0,
                           "llrawproc_publish_lock_ms": 10.0, "llrawproc_dualiso_refine_lock_ms": 10.0, "llrawproc_shared_lock_ms": 20.0})   # (r17: pass17 A1, see the r13 control; EXPORT-CDNG-TOTAL-CONTAINMENT-1: other = total 100 - the 21 ms of timed children, as dng.c:579-604 writes it)
        proc, got = evidence_cases([cdng_case(dualIsoExpected=True, profile=p, criteria=[]) for p in profiles + [refine, control]])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(len(profiles) + 1):
            with self.subTest(case=i):
                assert_containment_gates(self, got[i], "the disjoint stages")   # (EXPORT-CDNG-STAGE-CONTAINMENT-1: gating again)
        self.assertEqual(got[-1]["outcome"], "PASS", got[-1])
        self.assertEqual(got[-1]["containmentAdvisory"], [], got[-1])

    def test_r17_pass16_a1_disjoint_work_with_the_shared_lock_is_held_inside_its_parent(self) -> None:
        """adversary pass16 A1 (EXPORT-VENUE-LEG-1-OPEN-16-1): shared_lock (llrawproc.c :5142-5288, :5302-5376, :6731-6815 and the refine / publish adds) never meets dual_iso, chroma_smooth,
        vertical_stripes or pattern_noise, yet no group held them together: dual_iso 80 + shared_lock 80 + other 20 inside a 100 ms llrawproc read PASS (also with llrawproc_ms absent),
        raw_read 80 + dual_iso 1 + shared_lock 80 inside a 100 ms producer_frame read PASS, chroma_smooth 60 + shared_lock 60 inside a 100 ms llrawproc read PASS."""
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
        bad = [
            profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 80.0, "llrawproc_shared_lock_ms": 80.0, "llrawproc_other_ms": 20.0}),
            profile({"producer_frame_ms": 100.0, "llrawproc_dual_iso_ms": 80.0, "llrawproc_shared_lock_ms": 80.0, "llrawproc_other_ms": 20.0}, drop_total=True),
            profile({"producer_frame_ms": 100.0, "raw_read_ms": 80.0, "llrawproc_dual_iso_ms": 1.0, "llrawproc_shared_lock_ms": 80.0}, drop_total=True),
            profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_chroma_smooth_ms": 60.0, "llrawproc_shared_lock_ms": 60.0, "llrawproc_other_ms": 40.0}),
            # (the same class through the two other members the table derives: vertical_stripes and pattern_noise never meet shared_lock either)
            profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_vertical_stripes_ms": 30.0, "llrawproc_pattern_noise_ms": 30.0, "llrawproc_dual_iso_ms": 10.0,
                     "llrawproc_shared_lock_ms": 50.0, "llrawproc_other_ms": 30.0}),
        ]
        controls = [
            # a real shape: shared_lock overlaps dark_frame / focus_pixels / bad_pixels (the first lock window holds them), and retains the refine and publish times
            profile({"producer_frame_ms": 110.0, "llrawproc_ms": 100.0, "llrawproc_dark_frame_ms": 40.0, "llrawproc_bad_pixels_ms": 20.0, "llrawproc_dual_iso_ms": 30.0,
                     "llrawproc_shared_lock_ms": 65.0, "llrawproc_dualiso_refine_lock_ms": 3.0, "llrawproc_publish_lock_ms": 2.0, "llrawproc_other_ms": 10.0}),
            profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 50.0, "llrawproc_shared_lock_ms": 50.0, "llrawproc_other_ms": 50.0}),
            profile({"producer_frame_ms": 100.0, "raw_read_ms": 40.0, "llrawproc_dual_iso_ms": 30.0, "llrawproc_shared_lock_ms": 30.0}, drop_total=True),
        ]
        proc, got = evidence_cases([cdng_case(dualIsoExpected=True, profile=p, criteria=[]) for p in bad + controls])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(len(bad)):
            with self.subTest(case=i):
                assert_containment_gates(self, got[i], "the disjoint stages")   # (EXPORT-CDNG-STAGE-CONTAINMENT-1: gating again)
        for i in range(len(bad), len(bad) + len(controls)):
            with self.subTest(control=i):
                self.assertEqual(got[i]["outcome"], "PASS", got[i])
                self.assertEqual(got[i]["containmentAdvisory"], [], got[i])

    def test_r17_pass17_a1_the_lock_times_shared_lock_accumulates_are_inside_it(self) -> None:
        """adversary pass17 A1: llrawproc.c adds the refine and publish lock times to shared_lock (:6131, :6293, :7058), so each, and their disjoint sum, is inside it. refine 10 + publish 10
        beside a 0 ms shared_lock (the old r13 control) read PASS. The table's contains column derives the pairs and the group."""
        def profile(set_rows: dict) -> str:
            doc = json.loads(cdng_profile(30, dual_iso_ms=0.0))
            for r in doc["frames"]:
                for k in CDNG_STAGES:
                    if k not in ("writer_queue_wait_ms", "writer_completion_lag_ms", "disk_write_ms"):
                        r[k] = 0.0
                r.update(set_rows)
            return json.dumps(doc)
        base = {"producer_frame_ms": 120.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 30.0, "llrawproc_other_ms": 70.0}
        bad = [profile({**base, "llrawproc_dualiso_refine_lock_ms": 10.0, "llrawproc_publish_lock_ms": 10.0, "llrawproc_shared_lock_ms": 0.0}),   # the adversary's input
               profile({**base, "llrawproc_dualiso_refine_lock_ms": 30.0, "llrawproc_shared_lock_ms": 10.0}),                                     # one member above it
               profile({**base, "llrawproc_dualiso_refine_lock_ms": 10.0, "llrawproc_publish_lock_ms": 10.0, "llrawproc_shared_lock_ms": 15.0})]  # each below, the sum above
        control = profile({**base, "llrawproc_dualiso_refine_lock_ms": 10.0, "llrawproc_publish_lock_ms": 10.0, "llrawproc_shared_lock_ms": 25.0})
        # (adversary pass18 A1: llrawproc_other = total - the seven timed children, and the two lock times lie outside every child window, so they are inside it too)
        other_base = {"producer_frame_ms": 120.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 80.0, "llrawproc_shared_lock_ms": 20.0,
                      "llrawproc_dualiso_refine_lock_ms": 10.0, "llrawproc_publish_lock_ms": 10.0}
        bad += [profile({**other_base, "llrawproc_other_ms": 0.0}), profile({**other_base, "llrawproc_other_ms": 10.0})]   # the pass-18 input, both remainder values
        proc, got = evidence_cases([cdng_case(dualIsoExpected=True, profile=p, criteria=[]) for p in bad + [control]])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for i in range(len(bad)):
            with self.subTest(case=i):
                container = "llrawproc_shared_lock" if i < 3 else "llrawproc_other"
                # (EXPORT-CDNG-STAGE-CONTAINMENT-1: gating again; a group line and a contained-pair line both name "inside <container>")
                assert_containment_gates(self, got[i], f"inside {container}")
        self.assertEqual(got[-1]["outcome"], "PASS", got[-1])
        self.assertEqual(got[-1]["containmentAdvisory"], [], got[-1])

    def test_r17_every_disjoint_group_is_derived_from_the_stage_window_table(self) -> None:
        """CDNG-STAGE-WINDOW-TABLE-1: the groups the reader holds are every maximal pairwise-disjoint set of each parent's descendants in ExportCdngStageWindowTable, recomputed here from the
        table alone (no shared code), so a sixth member cannot be hand-missed; and the table names every CDNG producer stage plus the llrawproc total, so a new timer cannot be left out of it."""
        proc, got = run_pwsh_json(f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n"
                                  "& (Get-Module DualVenueRunner) { [ordered]@{ table = @($script:ExportCdngStageWindowTable); stages = @($script:ExportCdngProducerStages); "
                                  "groups = @(foreach ($g in $script:ExportCdngSumGroups) { [ordered]@{ members = @($g[0]); parent = [string]$g[1] } }); "
                                  "pairs = @(foreach ($p in $script:ExportCdngContainedPairs) { , @($p) }) } } | ConvertTo-Json -Compress -Depth 6")
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        rows = {r["name"]: r for r in got["table"]}
        self.assertEqual(set(rows), set(got["stages"]) | {"llrawproc"})
        rng = re.compile(r"^([^:#]+):(\d+)-(\d+)$")

        def meet(a: list, b: list) -> bool:
            for x in a:
                for y in b:
                    if x == y:
                        return True
                    mx, my = rng.match(x), rng.match(y)
                    if mx and my and mx.group(1) == my.group(1) and int(mx.group(2)) <= int(my.group(3)) and int(my.group(2)) <= int(mx.group(3)):
                        return True
            return False

        def lines(w: list) -> set:
            out = set()
            for x in w:
                m = rng.match(x)
                out |= {(m.group(1), n) for n in range(int(m.group(2)), int(m.group(3)) + 1)} if m else {x}
            return out

        def sib_overlap(x: str, y: str) -> bool:
            for rem, sib in ((rows[x], rows[y]), (rows[y], rows[x])):
                if rem.get("remainderOf"):
                    if sib["name"] in rem["remainderOf"]:
                        return False
                    covered = set().union(*(lines(rows[s]["windows"] or []) for s in rem["remainderOf"]))
                    return bool(lines(sib["windows"] or []) - covered)
            return meet(rows[x]["windows"] or [], rows[y]["windows"] or [])

        def chain(n: str) -> list:
            out = [n]
            while out[-1] in rows:
                out.append(rows[out[-1]]["parent"])
            return out

        @functools.lru_cache(maxsize=None)
        def overlap(x: str, y: str) -> bool:
            cx, cy = chain(x), chain(y)
            if y in cx or x in cy:
                return True
            for u in cx:
                for v in cy:
                    if u in rows and v in rows and rows[u]["parent"] == rows[v]["parent"]:
                        return sib_overlap(u, v)
            return True

        expected = set()
        containers = {n: list(r["contains"]) for n, r in rows.items() if r.get("contains")}   # (pass17 A1: a row that accumulates others contains them)
        for n, r in rows.items():   # (pass18 A1: a remainder contains every sibling that accrues on no line of the rows it is the remainder of)
            if r.get("remainderOf"):
                covered = [w for s in r["remainderOf"] for w in (rows[s]["windows"] or [])]
                inside = [m for m, s in rows.items() if s["parent"] == r["parent"] and m != n and m not in r["remainderOf"] and s["windows"] and not meet(s["windows"], covered)]
                containers[n] = containers.get(n, []) + [m for m in inside if m not in containers.get(n, [])]
        for parent in {r["parent"] for r in rows.values()} | set(containers):
            desc = list(containers[parent]) if parent in containers else [n for n in rows if parent in chain(n)[1:]]
            nbr = {v: {w for w in desc if w != v and not overlap(v, w)} for v in desc}
            best: list[set] = []

            def bk(r: set, p: set, x: set) -> None:   # Bron-Kerbosch with a pivot over the disjointness graph
                if not p and not x:
                    best.append(r)
                    return
                pivot = max(p | x, key=lambda u: len(p & nbr[u]))
                for v in list(p - nbr[pivot]):
                    bk(r | {v}, p & nbr[v], x & nbr[v])
                    p = p - {v}
                    x = x | {v}
            bk(set(), set(desc), set())
            for c in best:
                self.assertFalse(any(v not in c and c <= nbr[v] for v in desc), (parent, sorted(c)))   # maximal
                if len(c) >= 2:
                    expected.add((frozenset(c), parent))
        derived = {(frozenset(g["members"]), g["parent"]) for g in got["groups"]}
        self.assertEqual(derived, expected)
        self.assertEqual({tuple(p) for p in got["pairs"]}, {(m, n) for n, ms in containers.items() for m in ms})
        self.assertIn((frozenset({"llrawproc_dualiso_refine_lock", "llrawproc_publish_lock"}), "llrawproc_shared_lock"), derived)
        self.assertIn((frozenset({"llrawproc_dualiso_refine_lock", "llrawproc_publish_lock"}), "llrawproc_other"), derived)
        # (the pass16 A1 member is in a derived group, and so is every earlier round's)
        # (EXPORT-CDNG-STAGE-CONTAINMENT-1: pre_dualiso_fix has no export window, so it meets no row and joins every maximal group; r17 key3 B1's pair is held)
        self.assertIn((frozenset({"llrawproc_vertical_stripes", "llrawproc_pattern_noise", "llrawproc_dual_iso", "llrawproc_chroma_smooth", "llrawproc_shared_lock",
                                  "llrawproc_pre_dualiso_fix"}), "llrawproc"), derived)
        self.assertTrue(all("llrawproc_pre_dualiso_fix" in g["members"] for g in got["groups"] if g["parent"] in ("llrawproc", "producer_frame") and "llrawproc" not in g["members"]),
                        got["groups"])   # (a group holding the llrawproc TOTAL holds none of its children)

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


@requires_windows_pwsh
class ExportRenderPipeTimingTests(unittest.TestCase):
    """EXPORT-RENDER-PIPE-TIMING-1: the rendered (ProRes) export emits llrawproc_dual_iso and export_pipe_write (export_scale when scaled) per frame and export_pipe_drain and
    export_verify once per clip, under MLVAPP_STAGE_TIMING only. The judge reads them: a ProRes leg gains dualIsoClassAgrees (mirroring the CDNG profile's
    llrawproc_dual_iso_ms rule) and renderPipeTimed, and untimedPerFrameMs no longer counts the pipe, drain and verify. A log from an older build keeps its old reading."""

    SHIPPED = [{"metric": "framesExported", "op": "eq", "value": 30}, {"metric": "exportFramesFailed", "op": "eq", "value": 0},
               {"metric": "stageFramesSeen", "op": "eq", "value": 30}] + [dict(c) for c in PIPE_CRITERIA]

    def case(self, stage: str, **over) -> dict:
        c = prores_case(dualIsoExpected=True, stage=stage, criteria=self.SHIPPED)
        c.update(over)
        return c

    def test_r1_a_log_without_the_new_stages_is_never_pass_under_the_new_criteria(self) -> None:
        proc, got = evidence_cases([self.case(stage_log(30)), self.case(stage_log(30), dualIsoExpected=False)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got:
            self.assertNotEqual(g["outcome"], "PASS", g)
            m = g["metrics"]
            self.assertIsNone(m["dualIsoClassAgrees"], g)
            self.assertIsNone(m["renderPipeTimed"], g)
            self.assertIn("a build before EXPORT-RENDER-PIPE-TIMING-1", m["dualIsoClassAgreesCouldNotCheck"])
            self.assertIn("a build before EXPORT-RENDER-PIPE-TIMING-1", m["renderPipeTimedCouldNotCheck"])
            self.assertEqual(g["problems"], [], "an older build's log raises no new problem: the criteria, not the reader, refuse it")

    def test_r2_a_log_with_the_new_stages_passes_and_untimed_is_the_batch_wall_less_the_timed_work(self) -> None:
        proc, got = evidence_cases([self.case(piped_stage_log(30))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        g = got[0]
        self.assertEqual(g["outcome"], "PASS", g)
        m, st = g["metrics"], g["stages"]
        self.assertIs(m["dualIsoClassAgrees"], True)
        self.assertEqual(m["dualIsoActiveFrames"], 30)
        self.assertIs(m["renderPipeTimed"], True)
        self.assertNotIn("dualIsoClassAgreesCouldNotCheck", m)
        self.assertNotIn("renderPipeTimedCouldNotCheck", m)
        # batch wall 20 s / 30 frames, less processed16_total 56 + export_pipe_write 10 + (drain 100 + verify 200) / 30 frames
        self.assertAlmostEqual(st["untimedPerFrameMs"], 20000.0 / 30.0 - (56.0 + 10.0 + 300.0 / 30.0), places=3)
        self.assertAlmostEqual(st["untimedPerFrameMs"], 590.666667, places=3)
        # (the card's list said "probe"; the decode / ffprobe / ffmpeg-dump probes run inside the timed export_verify block, BatchRunner.cpp verify section, so what stays
        # untimed is the work before the first frame and the publish)
        for name in ("clip open", "receipt apply", "ffmpeg start", "publish"):
            self.assertIn(name, st["untimed"])
        self.assertIn("export_verify", st["untimed"])
        self.assertAlmostEqual(st["rows"]["llrawproc_dual_iso"]["meanMs"], 30.0)
        self.assertAlmostEqual(st["rows"]["export_pipe_write"]["meanMs"], 10.0)
        self.assertAlmostEqual(st["clipRows"]["export_pipe_drain"]["meanMs"], 100.0)
        self.assertAlmostEqual(st["clipRows"]["export_verify"]["meanMs"], 200.0)
        self.assertEqual(st["largestStage"], "debayered_frame", "largestStage stays a share of processed16_total's top-level children")
        self.assertIsNone(st["rows"]["export_pipe_write"]["share"], "the pipe write is not inside processed16_total, so it is no share of it")
        # scaled: export_scale per frame is counted too
        proc, got = evidence_cases([self.case(piped_stage_log(30, scale_ms=6.0), batch=batch_log(30, verified=30, scaled=True))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "PASS", got[0])
        self.assertAlmostEqual(got[0]["stages"]["untimedPerFrameMs"], 20000.0 / 30.0 - (56.0 + 6.0 + 10.0 + 10.0), places=3)

    def test_r3_no_dual_iso_on_a_dual_iso_leg_is_a_clip_class_mismatch(self) -> None:
        proc, got = evidence_cases([self.case(piped_stage_log(30, dual_iso_ms=0.0)), self.case(piped_stage_log(30), dualIsoExpected=False),
                                    self.case(piped_stage_log(30, dual_iso_ms=0.0), dualIsoExpected=False)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:2]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertIn("CLIP_CLASS_MISMATCH", g["detail"])
            self.assertTrue(g["classMismatch"])
            self.assertIs(g["metrics"]["dualIsoClassAgrees"], False)
        self.assertEqual(got[0]["metrics"]["dualIsoActiveFrames"], 0)
        self.assertIs(got[2]["metrics"]["dualIsoClassAgrees"], True, got[2])   # control: no dual-ISO on a no-dual-ISO leg
        self.assertEqual(got[2]["outcome"], "PASS", got[2])

    def test_r4_a_dual_iso_figure_longer_than_its_llrawproc_is_a_containment_problem(self) -> None:
        proc, got = evidence_cases([self.case(piped_stage_log(30, dual_iso_ms=35.0, llrawproc_ms=20.0)), self.case(piped_stage_log(30, dual_iso_ms=20.0, llrawproc_ms=20.0))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p and "llrawproc_dual_iso" in p for p in got[0]["problems"]), got[0])
        self.assertEqual(got[1]["outcome"], "PASS", got[1])

    def test_r5_a_clip_stage_recorded_twice_or_on_another_frame_is_typed(self) -> None:
        moved = piped_stage_log(30).replace("frame=29 stage=export_verify", "frame=3 stage=export_verify")
        proc, got = evidence_cases([self.case(piped_stage_log(30, drain_count=2)), self.case(piped_stage_log(30, verify_count=3)), self.case(moved, criteria=[])])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g, stage in ((got[0], "export_pipe_drain"), (got[1], "export_verify")):
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID") and stage in p and "exactly once" in p for p in g["problems"]), g)
        self.assertEqual(got[2]["outcome"], "INVALID", got[2])
        self.assertTrue(any(p.startswith("EXPORT_STAGE_WINDOW_MISMATCH") and "export_verify" in p for p in got[2]["problems"]), got[2])

    def test_r6_a_per_frame_pipe_stage_on_a_subset_of_frames_is_a_window_mismatch(self) -> None:
        cases = [self.case(piped_stage_log(30, pipe_frames=range(15))), self.case(piped_stage_log(30, pipe_frames=range(15)), criteria=[]),
                 self.case(piped_stage_log(30, scale_ms=4.0).replace("frame=7 stage=export_scale ms=4.000\n", ""), batch=batch_log(30, verified=30, scaled=True), criteria=[])]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_STAGE_WINDOW_MISMATCH") for p in g["problems"]), g)
        self.assertIs(got[1]["metrics"]["renderPipeTimed"], False)

    def test_r6b_a_pipe_timed_on_every_frame_without_its_drain_or_verify_is_not_render_pipe_timed(self) -> None:
        proc, got = evidence_cases([self.case(piped_stage_log(30, drain_count=0)), self.case(piped_stage_log(30, verify_count=0))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got:
            self.assertIs(g["metrics"]["renderPipeTimed"], False, g)
            self.assertNotEqual(g["outcome"], "PASS", g)

    def test_r6c_the_pipe_and_clip_stages_are_held_inside_the_batch_wall(self) -> None:
        # 30 frames, a 2.0 s wall = 66.7 ms per frame; processed16_total 56 fits alone, 56 + a 40 ms pipe write does not
        proc, got = evidence_cases([self.case(piped_stage_log(30, pipe_ms=40.0, drain_ms=1.0, verify_ms=1.0), batch=batch_log(30, verified=30, elapsed=2.0), criteria=[]),
                                    self.case(piped_stage_log(30, pipe_ms=1.0, drain_ms=1.0, verify_ms=900.0), batch=batch_log(30, verified=30, elapsed=2.0), criteria=[]),
                                    self.case(piped_stage_log(30, pipe_ms=1.0, drain_ms=1.0, verify_ms=1.0), batch=batch_log(30, verified=30, elapsed=2.0), criteria=[])])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:2]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p and "batch_wall_per_frame" in p for p in g["problems"]), g)
        self.assertEqual(got[2]["outcome"], "PASS", got[2])

    def test_r7_back_compat_todays_log_and_criteria_read_exactly_as_before(self) -> None:
        proc, got = evidence_cases([prores_case(), prores_case(dualIsoExpected=True), prores_case(criteria=[])])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got:
            self.assertEqual(g["outcome"], "PASS", g)
            self.assertEqual(g["problems"], [], g)
            self.assertFalse(g["classMismatch"], g)
            st = g["stages"]
            self.assertAlmostEqual(st["untimedPerFrameMs"], 20000.0 / 30.0 - 56.0, places=3)
            self.assertEqual(st["largestStage"], "debayered_frame")
            self.assertIsNone(g["metrics"]["dualIsoActiveFrames"])

    def test_adversary_r1_a1_the_dual_iso_leaf_stands_in_for_an_unpublished_llrawproc_in_every_sum_group(self) -> None:
        """adversary r1 A1 (00a7cf09): with llrawproc unpublished, llrawproc_dual_iso joined no disjoint group, so raw_uint16 30 + dual-ISO 30 (60 ms of disjoint work) inside a
        40 ms debayered_frame read PASS. The dual-ISO leaf takes llrawproc's place in every group that holds llrawproc (inside debayered_frame and, without it, the denominator)."""
        def log(rows: tuple) -> str:
            per = "".join(f"[mlv-stage-timing] frame={i} stage={s} ms={ms:.3f}\n" for i in range(480) for s, ms in rows)
            return per + "[mlv-stage-timing] frame=479 stage=export_pipe_drain ms=100.000\n[mlv-stage-timing] frame=479 stage=export_verify ms=200.000\n"
        finding = (("raw_uint16", 30.0), ("llrawproc_dual_iso", 30.0), ("debayered_frame", 40.0), ("processing", 15.0), ("processed16_total", 56.0), ("export_pipe_write", 10.0))
        no_debayer = (("raw_uint16", 30.0), ("llrawproc_dual_iso", 30.0), ("processing", 15.0), ("processed16_total", 56.0), ("export_pipe_write", 10.0))
        control = (("raw_uint16", 9.0), ("llrawproc_dual_iso", 30.0), ("debayered_frame", 40.0), ("processing", 15.0), ("processed16_total", 56.0), ("export_pipe_write", 10.0))
        shipped = leg("m16-1243-export-prores")["criteria"]["acceptance"]["cpu"]
        cases = [prores_case(maxFrames=480, dualIsoExpected=True, stage=log(rows), batch=batch_log(480, verified=480, elapsed=60.0), criteria=shipped) for rows in (finding, no_debayer, control)]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:2]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p and "the disjoint stages" in p and "llrawproc_dual_iso" in p for p in g["problems"]), g)
        self.assertEqual(got[2]["outcome"], "PASS", got[2])

    @staticmethod
    def rows_log(rows: tuple, frames: int = 480) -> str:
        per = "".join(f"[mlv-stage-timing] frame={i} stage={s} ms={ms:.3f}\n" for i in range(frames) for s, ms in rows)
        return per + f"[mlv-stage-timing] frame={frames - 1} stage=export_pipe_drain ms=100.000\n[mlv-stage-timing] frame={frames - 1} stage=export_verify ms=200.000\n"

    def big(self, stage: str, frames: int = 480, **over) -> dict:
        c = prores_case(maxFrames=frames, dualIsoExpected=True, stage=stage, batch=batch_log(frames, verified=frames, elapsed=60.0),
                        criteria=leg("m16-1243-export-prores")["criteria"]["acceptance"]["cpu"])
        c.update(over)
        return c

    def test_adversary_r1f2_a1_the_dual_iso_twin_group_is_not_judged_when_llrawproc_is_published(self) -> None:
        """adversary A1 on 33f7a05a: the derived twin (raw_uint16 + llrawproc_dual_iso inside debayered_frame) fired beside a published llrawproc, where the dual-ISO figure is
        INSIDE llrawproc and the real group already holds it: raw 23 + llrawproc 20 + dual-ISO 20.001 inside a 40 ms debayered_frame read INVALID. The twin stands in only for an
        llrawproc the run did not publish."""
        rows = (("raw_uint16", 23.0), ("llrawproc", 20.0), ("llrawproc_dual_iso", 20.001), ("debayered_frame", 40.0), ("processing", 15.0), ("processed16_total", 56.0),
                ("export_pipe_write", 10.0))
        proc, got = evidence_cases([self.big(self.rows_log(rows))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "PASS", got[0])
        self.assertEqual(got[0]["problems"], [], got[0])

    def test_adversary_r1f2_a3_the_render_start_scaled_flag_decides_whether_export_scale_is_owed(self) -> None:
        """adversary A3 on 33f7a05a: the reader never read RENDER_START, so a scaled plan with no export_scale (its residual 6 ms high) and an unscaled plan carrying export_scale
        (6 ms subtracted for work it declared none of) both read PASS. A build of this card declares scaled= and times export_scale on every frame exactly when it is true."""
        def batch(scaled: bool, src: str, out: str) -> str:
            return batch_log(480, verified=480, elapsed=60.0, scaled=None).replace(
                "[BATCH] RENDER_VERIFIED", f"[BATCH] RENDER_START view-0 codec=prores container=mov source={src} output={out} scaled={'true' if scaled else 'false'} fps=24 "
                                           "frames=480 cutIn=1 cutOut=480 out=view-0.mov\n[BATCH] RENDER_VERIFIED", 1)
        scaled_log = piped_stage_log(480, scale_ms=6.0)
        no_scale = "".join(l + "\n" for l in scaled_log.splitlines() if "stage=export_scale " not in l)
        cases = [self.big(no_scale, batch=batch(True, "1920x1080", "1920x1440")), self.big(scaled_log, batch=batch(False, "1920x1080", "1920x1080")),
                 self.big(scaled_log, batch=batch(True, "1920x1080", "1920x1440")), self.big(no_scale, batch=batch(False, "1920x1080", "1920x1080")),
                 self.big(scaled_log, batch=batch_log(480, verified=480, elapsed=60.0, scaled=None))]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:2]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any("export_scale" in p and "scaled=" in p for p in g["problems"]), g)
        for g in got[2:4]:
            self.assertEqual(g["outcome"], "PASS", g)
        self.assertAlmostEqual(got[2]["stages"]["untimedPerFrameMs"], 125.0 - (56.0 + 6.0 + 10.0 + 300.0 / 480.0), places=3)
        self.assertEqual(got[4]["outcome"], "INVALID", got[4])
        self.assertTrue(any("RENDER_START" in p for p in got[4]["problems"]), got[4])

    def test_adversary_r1f2_a4_a_per_frame_stage_recorded_twice_on_one_frame_is_invalid(self) -> None:
        """adversary A4 on 33f7a05a: per-frame records were SUMMED, so every export_pipe_write (or export_scale) repeated once doubled the subtracted work and still read PASS, and a
        dual-ISO 0.000 row beside a 30.000 row on one frame read as an active frame. The producer notes each once per frame (llrawproc_dual_iso once per llrawproc record)."""
        base = piped_stage_log(480)
        dup_pipe = "".join(l + "\n" + (l + "\n" if "stage=export_pipe_write " in l else "") for l in base.splitlines())
        scaled = piped_stage_log(480, scale_ms=6.0)
        dup_scale = "".join(l + "\n" + (l + "\n" if "stage=export_scale " in l else "") for l in scaled.splitlines())
        zero = piped_stage_log(480, dual_iso_ms=0.0)
        dup_dual = "".join(l + "\n" + (l.replace("ms=0.000", "ms=30.000") + "\n" if "stage=llrawproc_dual_iso " in l else "") for l in zero.splitlines())
        sb = batch_log(480, verified=480, elapsed=60.0, scaled=True)
        proc, got = evidence_cases([self.big(dup_pipe), self.big(dup_scale, batch=sb), self.big(dup_dual), self.big(base)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g, stage in zip(got[:3], ("export_pipe_write", "export_scale", "llrawproc_dual_iso")):
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID") and stage in p and "per frame" in p for p in g["problems"]), (stage, g))
        self.assertEqual(got[3]["outcome"], "PASS", got[3])

    def test_adversary_r1f2_a5_a_log_with_some_of_this_cards_stages_is_an_incomplete_capture_not_an_older_build_or_a_fail(self) -> None:
        """adversary A5 on 33f7a05a: a log that carries this card's pipe stages but no dual-ISO row kept the older-build reason, and one missing its drain (or verify) read a
        product FAIL with no problem. Any of this card's stages is proof the build emits them all, so a missing one is an incomplete capture: INVALID, naming it."""
        proc, got = evidence_cases([self.big(piped_stage_log(480, dual_iso=False)), self.big(piped_stage_log(480, drain_count=0)), self.big(piped_stage_log(480, verify_count=0)),
                                    self.big(piped_stage_log(480).replace("stage=export_pipe_write", "stage=other_stage")),
                                    self.big(stage_log(480) + "".join(f"[mlv-stage-timing] frame={i} stage=llrawproc_dual_iso ms=5.000\n" for i in range(480)))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g, stage in zip(got, ("llrawproc_dual_iso", "export_pipe_drain", "export_verify", "export_pipe_write", "export_pipe_write")):
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_PROFILE_ABSENT") and stage in p and "incomplete" in p for p in g["problems"]), (stage, g))
            self.assertNotIn("a build before EXPORT-RENDER-PIPE-TIMING-1", json.dumps(g["metrics"]), g)
        self.assertIs(got[1]["metrics"]["renderPipeTimed"], False)

    def test_adversary_r1f2_a7_distinguish_a_frame_whose_raw_read_failed_carries_no_llrawproc_either_and_is_invalid(self) -> None:
        """adversary A7 on 33f7a05a (DISTINGUISH): a raw-read failure returns before llrawproc runs, so that frame carries neither llrawproc nor llrawproc_dual_iso while
        processed16_total is still timed. The base judge already holds llrawproc to processed16_total's frames, so the run is EXPORT_STAGE_WINDOW_MISMATCH: INVALID, never a
        PASS with a dual-ISO class read from 479 of 480 frames."""
        log = "".join(l + "\n" for l in piped_stage_log(480).splitlines() if not (l.startswith("[mlv-stage-timing] frame=17 stage=llrawproc")))
        proc, got = evidence_cases([self.big(log)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any(p.startswith("EXPORT_STAGE_WINDOW_MISMATCH") and "llrawproc" in p for p in got[0]["problems"]), got[0])
        self.assertIsNone(got[0]["metrics"]["dualIsoClassAgrees"], got[0])

    # --- r2: adversary pass 3 on a6f48506 (A1-A6, advisory A8). The control is the adversary's own: 480 frames, cutIn=1 cutOut=480, the shipped acceptance criteria.
    CTL_START = ("[BATCH] RENDER_START view-0 codec=prores container=mov source=1920x1080 output=1920x1080 scaled=false fps=24 frames=480 cutIn=1 cutOut=480 "
                 "out=view-0.mov")

    @classmethod
    def ctl_batch(cls, start: str | None = None, verified_clip: str = "view-0", order: tuple = ("start", "verified", "done")) -> str:
        lines = {"start": cls.CTL_START if start is None else start, "verified": f"[BATCH] RENDER_VERIFIED {verified_clip} decoded_frames=480",
                 "done": "[BATCH] DONE view-0 exported=480 skipped=0 elapsed=60.0"}
        return "\n".join(lines[k] for k in order) + "\n"

    def ctl(self, stage: str | None = None, **over) -> dict:
        c = self.big(piped_stage_log(480) if stage is None else stage, batch=self.ctl_batch())
        c.update(over)
        return c

    @staticmethod
    def head_sha() -> str:
        import subprocess
        return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    def test_adversary_r2_a1_the_legacy_shape_reads_zero_or_one_new_stage_under_strict_mode_and_stop(self) -> None:
        """adversary A1 on a6f48506 (blocker): -LegacyRenderShape read .Count on a bare filter result, which under the module's Set-StrictMode -Version Latest throws
        PropertyNotFoundStrict for zero or one matching stage. Called directly with ErrorActionPreference Stop, both must return: zero new stages -> the base shape (no
        renderPipeTimed), one new stage -> this card's shape (and the incomplete capture typed)."""
        one = stage_log(30) + "".join(f"[mlv-stage-timing] frame={i} stage=export_pipe_write ms=1.000\n" for i in range(30))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cases.json"
            path.write_text(json.dumps([prores_case(dualIsoExpected=True, stage=stage_log(30)), prores_case(dualIsoExpected=True, stage=one)]), encoding="utf-8")
            proc, got = run_pwsh_json("$ErrorActionPreference = 'Stop'\n$cases = Get-Content -Raw -LiteralPath $env:DVE_CASES -Encoding utf8 | ConvertFrom-Json\n"
                                      f"@($cases | ForEach-Object {{ $c = $_; try {{ $e = {EVIDENCE_CALL} -LegacyRenderShape; "
                                      "[ordered]@{ threw = $null; metrics = $e.metrics; problems = @($e.problems) } } catch { [ordered]@{ threw = [string]$_.FullyQualifiedErrorId } } }) "
                                      "| ConvertTo-Json -Compress -Depth 8 -AsArray", DVE_CASES=str(path))
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got:
            self.assertIsNone(g["threw"], g)
        self.assertNotIn("renderPipeTimed", got[0]["metrics"], "zero new stages: the base reader's shape")
        self.assertEqual(got[0]["problems"], [], got[0])
        self.assertIn("renderPipeTimed", got[1]["metrics"], "one new stage: this card's shape, never the base one")
        self.assertTrue(any(p.startswith("EXPORT_PROFILE_ABSENT") and "incomplete" in p for p in got[1]["problems"]), got[1])

    def test_adversary_r2_a2_render_start_is_read_whole_for_dones_clip(self) -> None:
        """adversary A2 on a6f48506: the first scaled= token of any RENDER_START line was the declaration, so a conflicting second scaled=, a line cut short after scaled= and
        another clip's line all read PASS. The line is read whole, in the producer's field order, for the clip DONE names."""
        conflict = self.CTL_START.replace("output=1920x1080 scaled=false", "output=1920x1440 scaled=false scaled=true")
        foreign = self.CTL_START.replace("RENDER_START view-0", "RENDER_START view-1")
        cut = self.CTL_START.split(" fps=")[0]
        doubled = self.CTL_START.replace(" out=view-0.mov", " cutIn=1 out=view-0.mov")
        cases = [self.ctl(batch=self.ctl_batch(s)) for s in (conflict, foreign, cut, doubled)] + [self.ctl()]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:4]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any("RENDER_START" in p for p in g["problems"]), g)
        self.assertTrue(any("view-1" in p and "DONE" in p for p in got[1]["problems"]), got[1])
        self.assertEqual(got[4]["outcome"], "PASS", got[4])
        self.assertEqual(got[4]["problems"], [], got[4])

    @staticmethod
    def shift(log: str, by: int) -> str:
        return re.sub(r"frame=(\d+)", lambda m: f"frame={int(m.group(1)) + by}", log)

    def test_adversary_r2_a3_the_frame_window_is_the_declared_cut_not_the_log(self) -> None:
        """adversary A3 on a6f48506: the window came from the stage log itself, so frames 1..480 under cutIn=1 cutOut=480 read PASS (the producer's loop, BatchRunner.cpp:541,
        times frames cutIn-1 .. cutOut-1 only), and a one-frame cut timed on frame 999999999999 read no problem. The control: a cut that starts at 5 is timed on 4..483."""
        rows = (("raw_uint16", 9.0), ("llrawproc", 31.0), ("llrawproc_dual_iso", 30.0), ("debayered_frame", 40.0), ("processing", 15.0), ("processed16_total", 56.0), ("export_pipe_write", 10.0))
        one = "".join(f"[mlv-stage-timing] frame=999999999999 stage={s} ms={ms:.3f}\n" for s, ms in rows)
        one += "[mlv-stage-timing] frame=999999999999 stage=export_pipe_drain ms=1.000\n[mlv-stage-timing] frame=999999999999 stage=export_verify ms=1.000\n"
        one_batch = ("[BATCH] RENDER_START view-0 codec=prores container=mov source=1x1 output=1x1 scaled=false fps=24 frames=1 cutIn=1 cutOut=1 out=view-0.mov\n"
                     "[BATCH] RENDER_VERIFIED view-0 decoded_frames=1\n[BATCH] DONE view-0 exported=1 skipped=0 elapsed=1.0\n")
        at5 = self.CTL_START.replace("cutIn=1 cutOut=480", "cutIn=5 cutOut=484")
        cases = [self.ctl(self.shift(piped_stage_log(480), 1)), prores_case(maxFrames=1, dualIsoExpected=True, stage=one, batch=one_batch, criteria=[]),
                 self.ctl(self.shift(piped_stage_log(480), 4), batch=self.ctl_batch(at5)), self.ctl(piped_stage_log(480), batch=self.ctl_batch(at5))]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in (got[0], got[1], got[3]):
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_STAGE_WINDOW_MISMATCH") and "cutIn" in p for p in g["problems"]), g)
        self.assertEqual(got[2]["outcome"], "PASS", got[2])
        self.assertEqual(got[2]["problems"], [], got[2])

    def test_adversary_r2_a4_each_dual_iso_record_pairs_with_the_llrawproc_run_it_follows(self) -> None:
        """adversary A4 on a6f48506: per-frame sums lost the pairing, so llrawproc 0 / dual-ISO 30 then llrawproc 31 / dual-ISO 0 on every frame summed to 31 / 30 and read all
        480 frames active. Each dual-ISO record is the one right after an llrawproc record of its frame, inside that run. Controls: the producer's order, and a figure inside the slack."""
        base = piped_stage_log(480)
        def swap(line: str) -> str:
            if " stage=llrawproc ms=" in line:
                f = line.split("frame=")[1].split(" ")[0]
                return (f"[mlv-stage-timing] frame={f} stage=llrawproc ms=0.000\n[mlv-stage-timing] frame={f} stage=llrawproc_dual_iso ms=30.000\n"
                        f"[mlv-stage-timing] frame={f} stage=llrawproc ms=31.000\n[mlv-stage-timing] frame={f} stage=llrawproc_dual_iso ms=0.000\n")
            return "" if " stage=llrawproc_dual_iso " in line else line + "\n"
        finding = "".join(swap(l) for l in base.splitlines())
        lines = base.splitlines()
        moved = [l for l in lines if " stage=llrawproc_dual_iso " not in l]
        moved = "\n".join(moved[:-2] + [l for l in lines if " stage=llrawproc_dual_iso " in l] + moved[-2:]) + "\n"
        proc, got = evidence_cases([self.ctl(finding), self.ctl(moved), self.ctl(), self.ctl(piped_stage_log(480, dual_iso_ms=31.0, llrawproc_ms=30.0))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:2]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID") and "llrawproc_dual_iso" in p and "follow" in p for p in g["problems"]), g)
        for g in got[2:]:
            self.assertEqual(g["outcome"], "PASS", g)

    def test_adversary_r2_a5_clip_records_and_batch_lines_keep_the_producers_order(self) -> None:
        """adversary A5 on a6f48506: export_verify moved to the first line, and RENDER_START moved below DONE, both read PASS; the fixture batch_log() itself wrote
        RENDER_VERIFIED before RENDER_START. The drain and verify follow every per-frame record (verify after the drain), and RENDER_START, RENDER_VERIFIED and DONE keep their order."""
        lines = piped_stage_log(480).splitlines()
        verify_first = "\n".join([lines[-1]] + lines[:-1]) + "\n"
        verify_before_drain = "\n".join(lines[:-2] + [lines[-1], lines[-2]]) + "\n"
        drain_mid = "\n".join(lines[:100] + [lines[-2]] + lines[100:-2] + [lines[-1]]) + "\n"
        cases = [self.ctl(verify_first), self.ctl(verify_before_drain), self.ctl(drain_mid), self.ctl(batch=self.ctl_batch(order=("verified", "done", "start"))),
                 self.ctl(batch=self.ctl_batch(order=("verified", "start", "done")))]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:3]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID") and ("export_verify" in p or "export_pipe_drain" in p) for p in g["problems"]), g)
        for g in got[3:]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any("RENDER_START" in p and ("DONE" in p or "order" in p) for p in g["problems"]), g)
        text = batch_log(30, verified=30)
        self.assertLess(text.index("RENDER_START"), text.index("RENDER_VERIFIED"))
        self.assertLess(text.index("RENDER_VERIFIED"), text.index("[BATCH] DONE"))

    def test_adversary_r2_a6_a_log_without_the_new_stages_from_a_build_that_emits_them_is_an_incomplete_capture(self) -> None:
        """adversary A6 on a6f48506: with ExpectedBuildId this head and none of the new stages, the reader presumed an older build and the shipped criteria read FAIL with no
        problem. A build whose own src/debug/StageTiming.h defines this card's stages times them all, so that log is an incomplete capture: INVALID, typed, with no older-build
        reason. Removing only export_verify stays INVALID too."""
        head = self.head_sha()
        old = stage_log(480)
        only_verify_gone = "".join(l + "\n" for l in piped_stage_log(480).splitlines() if " stage=export_verify " not in l)
        proc, got = evidence_cases([self.ctl(old, buildId=head), self.ctl(only_verify_gone, buildId=head), self.ctl(buildId=head)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:2]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_PROFILE_ABSENT") and "incomplete" in p for p in g["problems"]), g)
            self.assertNotIn("a build before EXPORT-RENDER-PIPE-TIMING-1", json.dumps(g["metrics"]), g)
        self.assertTrue(any(head in p for p in got[0]["problems"]), got[0])
        self.assertEqual(got[2]["outcome"], "PASS", got[2])

    def test_adversary_r2_a6_distinguish_a_build_this_repo_cannot_name_keeps_the_r1_reading_and_an_older_build_cannot_carry_the_stages(self) -> None:
        """A6, the distinguished half: a build id that is no commit of this repo (the fixtures' 'e' * 40, or a venue build from another tree) cannot be classed, so a log without
        the new stages keeps the r1 reading (null metrics, refused by the criteria, never PASS). And the base commit 2b796198, which defines none of this card's stages, beside a
        log carrying them is EXPORT_PROFILE_FOREIGN."""
        proc, got = evidence_cases([self.ctl(stage_log(480), buildId=BUILD_ID)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertNotEqual(got[0]["outcome"], "PASS", got[0])
        self.assertIsNone(got[0]["metrics"]["renderPipeTimed"], got[0])
        import subprocess
        base = "2b79619832444c0a63a8573d04f6cd02a4a113dc"
        if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-t", base], capture_output=True, text=True).stdout.strip() != "commit":
            self.skipTest("the base commit is not in this clone (a shallow checkout)")
        proc, got = evidence_cases([self.ctl(buildId=base), self.ctl(stage_log(480), buildId=base)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any(p.startswith("EXPORT_PROFILE_FOREIGN") and base in p for p in got[0]["problems"]), got[0])
        self.assertNotEqual(got[1]["outcome"], "PASS", got[1])
        self.assertEqual(got[1]["problems"], [], "an older build's log raises no new problem")

    def test_adversary_r2_a8_render_verified_is_of_the_clip_done_names(self) -> None:
        """advisory A8 on a6f48506 (base defect, fixed in scope): RENDER_VERIFIED view-1 beside DONE view-0 read PASS."""
        proc, got = evidence_cases([self.ctl(batch=self.ctl_batch(verified_clip="view-1"))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any(p.startswith("EXPORT_OUTPUT_MISMATCH") and "view-1" in p for p in got[0]["problems"]), got[0])

    # --- r2 fix 1: adversary pass 4 on 2bd239b8 (A1-A6, advisory A9)
    def test_adversary_r2f1_a1_an_unknown_stage_record_after_the_verify_is_out_of_order_too(self) -> None:
        """pass 4 A1: a record of a stage the reader does not table (raw_uint16_copy) after export_verify did not move the last per-frame line, so the order check passed."""
        proc, got = evidence_cases([self.ctl(piped_stage_log(480) + "[mlv-stage-timing] frame=479 stage=raw_uint16_copy ms=0.001\n")])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID") and "before the last per-frame record" in p for p in got[0]["problems"]), got[0])

    def test_adversary_r2f1_a2_each_frame_renders_then_scales_then_writes_in_frame_order(self) -> None:
        """pass 4 A2: frame 0's pipe write before its processed16_total, all frame blocks reversed, and export_scale after export_pipe_write all read PASS. The producer
        completes processed16_total, then the optional scale, then the pipe write, frame by frame in rising order (BatchRunner.cpp:541-567)."""
        base = piped_stage_log(480)
        early = base.replace("[mlv-stage-timing] frame=0 stage=export_pipe_write ms=10.000\n", "").replace(
            "[mlv-stage-timing] frame=0 stage=processed16_total ms=56.000\n", "[mlv-stage-timing] frame=0 stage=export_pipe_write ms=10.000\n[mlv-stage-timing] frame=0 stage=processed16_total ms=56.000\n")
        lines = base.splitlines()
        per, clip = lines[:-2], lines[-2:]
        blocks = [per[i:i + 9] for i in range(0, len(per), 9)]
        reversed_log = "\n".join([l for b in reversed(blocks) for l in b] + clip) + "\n"
        scaled = piped_stage_log(480, scale_ms=6.0)
        swapped = re.sub(r"(\[mlv-stage-timing\] frame=(\d+) stage=export_scale ms=6\.000)\n(\[mlv-stage-timing\] frame=\2 stage=export_pipe_write ms=10\.000)\n", r"\3\n\1\n", scaled)
        sb = self.ctl_batch(self.CTL_START.replace("output=1920x1080 scaled=false", "output=1920x1440 scaled=true"))
        proc, got = evidence_cases([self.ctl(early), self.ctl(reversed_log), self.ctl(swapped, batch=sb), self.ctl(scaled, batch=sb)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(len(blocks), 480)
        for g in got[:3]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID") and "export_pipe_write" in p and "order" in p for p in g["problems"]), g)
        self.assertEqual(got[3]["outcome"], "PASS", got[3])

    def test_adversary_r2f1_a3_render_start_dimensions_and_cut_fit_the_producer(self) -> None:
        """pass 4 A3: output 1920x1440 beside source 1920x1080 with scaled=false read PASS with no scale owed (an unscaled plan's output IS its source), and a cut past the
        producer's uint32 range (cutIn=4294967296) read PASS. A scaled plan may keep its size (a resize to the source dimensions), so that direction stays open."""
        grown = self.CTL_START.replace("output=1920x1080", "output=1920x1440")
        huge = self.CTL_START.replace("cutIn=1 cutOut=480", "cutIn=4294967296 cutOut=4294967775")
        same_scaled = self.CTL_START.replace("scaled=false", "scaled=true")
        cases = [self.ctl(batch=self.ctl_batch(grown)), self.ctl(self.shift(piped_stage_log(480), 4294967295), batch=self.ctl_batch(huge)),
                 self.ctl(piped_stage_log(480, scale_ms=6.0), batch=self.ctl_batch(same_scaled))]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got[:2]:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any("RENDER_START" in p for p in g["problems"]), g)
        self.assertEqual(got[2]["outcome"], "PASS", got[2])

    def test_adversary_r2f1_a4_a9_a_build_id_in_capitals_is_the_same_build_and_derive_in_any_case_derives(self) -> None:
        """pass 4 A4 (and advisory A9): the uppercase spelling of the base commit read 'unknown' (PASS) where the lowercase read EXPORT_PROFILE_FOREIGN (INVALID); and
        -RenderPipeBuildClass DERIVE skipped the lookup. One build is one class, however its id is spelled."""
        import subprocess
        base = "2b79619832444c0a63a8573d04f6cd02a4a113dc"
        if subprocess.run(["git", "-C", str(ROOT), "cat-file", "-t", base], capture_output=True, text=True).stdout.strip() != "commit":
            self.skipTest("the base commit is not in this clone (a shallow checkout)")
        proc, got = evidence_cases([self.ctl(buildId=base.upper()), self.ctl(buildId=self.head_sha().upper(), stage=stage_log(480))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertTrue(any(p.startswith("EXPORT_PROFILE_FOREIGN") for p in got[0]["problems"]), got[0])
        self.assertTrue(any(p.startswith("EXPORT_PROFILE_ABSENT") and "incomplete" in p for p in got[1]["problems"]), got[1])
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "cases.json"
            path.write_text(json.dumps([self.ctl(buildId=base)]), encoding="utf-8")
            proc, got = run_pwsh_json("$cases = Get-Content -Raw -LiteralPath $env:DVE_CASES -Encoding utf8 | ConvertFrom-Json\n"
                                      f"@($cases | ForEach-Object {{ $c = $_; $e = {EVIDENCE_CALL} -RenderPipeBuildClass DERIVE; [ordered]@{{ problems = @($e.problems) }} }}) "
                                      "| ConvertTo-Json -Compress -Depth 8 -AsArray", DVE_CASES=str(path))
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertTrue(any(p.startswith("EXPORT_PROFILE_FOREIGN") for p in got[0]["problems"]), got[0])

    def test_adversary_r2f1_a5_a_lookup_that_fails_is_never_cached_and_never_reads_older(self) -> None:
        """pass 4 A5: a failed lookup was cached (unknown, or older when the header read failed), so one session kept that answer after the object became readable. Only a
        header that was read decides a class; a seam that fails the header read once reads unknown, and the next call (seam restored) reads the real class."""
        head = self.head_sha()
        script = ("$m = Get-Module DualVenueRunner\n"
                  # (r2 fix 2: the seam is git not answering -- a header path the tree lacks now reads 'older', which is what that tree says)
                  "$first = & $m { param($id) $saved = $script:DvGitExecutable; $script:DvGitExecutable = 'C:\\no-such-dir\\git-missing.exe'; try { Get-DvRenderPipeBuildClass -BuildId $id } finally { $script:DvGitExecutable = $saved } } $env:DVE_HEAD\n"
                  "$second = & $m { param($id) Get-DvRenderPipeBuildClass -BuildId $id } $env:DVE_HEAD\n"
                  "$unknown1 = & $m { Get-DvRenderPipeBuildClass -BuildId ('0' * 40) }\n"
                  "[ordered]@{ first = $first; second = $second; unknown = $unknown1 } | ConvertTo-Json -Compress")
        proc, got = run_pwsh_json(script, DVE_HEAD=head)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got["first"], "unknown", "an unreadable header is no evidence of an older build")
        self.assertEqual(got["second"], "emits", "and the failed read was not cached")
        self.assertEqual(got["unknown"], "unknown")

    def test_adversary_r2f1_a6_a_clip_name_that_only_looks_like_dones_is_another_clip(self) -> None:
        """pass 4 A6: view-0 followed by U+200B in RENDER_START or RENDER_VERIFIED compared equal to DONE's view-0 under the culture-aware -cne. Names compare ordinally."""
        zw = "view-0​"
        cases = [self.ctl(batch=self.ctl_batch(self.CTL_START.replace("RENDER_START view-0", f"RENDER_START {zw}"))), self.ctl(batch=self.ctl_batch(verified_clip=zw))]
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_OUTPUT_MISMATCH") and "DONE" in p for p in g["problems"]), g)

    # --- r2 fix 2: adversary pass 5 on c2da7e24 (A1, A4, A5, A6; A2 and A3 stay open for r3)
    def test_adversary_r2f2_a1_each_frame_is_one_block_rendered_then_written(self) -> None:
        """pass 5 A1: processing moved between frame 0's total and its write, frame 1's disk read before frame 0's write, an untabled record of frame 0 after its write, and an
        untabled record of frame 9999 after the first record all read PASS. Each frame is one block: its render (processed16_total last), its scale, its write."""
        lines = piped_stage_log(480).splitlines()
        def idx(s: str) -> int:
            return lines.index(s)
        p0, w0 = idx("[mlv-stage-timing] frame=0 stage=processed16_total ms=56.000"), idx("[mlv-stage-timing] frame=0 stage=export_pipe_write ms=10.000")
        proc0 = "[mlv-stage-timing] frame=0 stage=processing ms=15.000"
        v1 = [l for l in lines if l != proc0]
        v1.insert(v1.index("[mlv-stage-timing] frame=0 stage=export_pipe_write ms=10.000"), proc0)
        d1 = "[mlv-stage-timing] frame=1 stage=raw_uint16_disk_read ms=3.000"
        v2 = [l for l in lines if l != d1]
        v2.insert(v2.index("[mlv-stage-timing] frame=0 stage=export_pipe_write ms=10.000"), d1)
        v3 = lines[:w0 + 1] + ["[mlv-stage-timing] frame=0 stage=raw_uint16_copy ms=0.001"] + lines[w0 + 1:]
        v4 = lines[:1] + ["[mlv-stage-timing] frame=9999 stage=raw_uint16_copy ms=0.001"] + lines[1:]
        self.assertLess(p0, w0)
        proc, got = evidence_cases([self.ctl("\n".join(v) + "\n") for v in (v1, v2, v3, v4)])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID") and "frame loop's order" in p for p in g["problems"]), g)

    def test_adversary_r2f2_a4_a_build_id_with_a_newline_or_a_tree_without_the_header_is_classed(self) -> None:
        """pass 5 A4: the base id with a trailing LF matched the '$' anchor and failed the lookup (unknown, PASS); and a commit whose tree has no src/debug/StageTiming.h read
        unknown (PASS) beside a log carrying this card's stages. The first is a malformed id (EXPORT_PROFILE_FOREIGN), the second a build before stage timing (older, FOREIGN)."""
        import subprocess
        added = subprocess.run(["git", "-C", str(ROOT), "log", "--diff-filter=A", "--format=%H", "--", "src/debug/StageTiming.h"], capture_output=True, text=True).stdout.split()
        cases = [self.ctl(buildId="2b79619832444c0a63a8573d04f6cd02a4a113dc\n")]
        before = None
        if added:
            r = subprocess.run(["git", "-C", str(ROOT), "rev-parse", f"{added[-1]}^"], capture_output=True, text=True)
            before = r.stdout.strip() if r.returncode == 0 else None
        if before:
            cases.append(self.ctl(buildId=before))
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for g in got:
            self.assertEqual(g["outcome"], "INVALID", g)
            self.assertTrue(any(p.startswith("EXPORT_PROFILE_FOREIGN") for p in g["problems"]), g)
        if not before:
            self.skipTest("the commit before src/debug/StageTiming.h existed is not in this clone")

    def test_adversary_r2f2_a5_the_ceiling_and_the_clip_sum_are_held_on_the_exact_record_figures(self) -> None:
        """pass 5 A5: verify=86400000.000000001 rounded onto the ceiling as a double (no problem), and a verify figure 9.6e-6 ms past the wall limit was admitted because
        the clip sum went through a double. Both are read exactly from the record text. Control: the same wall with verify a full increment under the limit."""
        def log(drain: str, verify: str) -> str:
            rows = (("raw_uint16", 9.0), ("llrawproc", 31.0), ("llrawproc_dual_iso", 30.0), ("debayered_frame", 40.0), ("processing", 15.0), ("processed16_total", 56.0), ("export_pipe_write", 10.0))
            per = "".join(f"[mlv-stage-timing] frame={i} stage={s} ms={ms:.3f}\n" for i in range(24) for s, ms in rows)
            return per + f"[mlv-stage-timing] frame=23 stage=export_pipe_drain ms={drain}\n[mlv-stage-timing] frame=23 stage=export_verify ms={verify}\n"
        def batch(elapsed: str) -> str:
            return ("[BATCH] RENDER_START view-0 codec=prores container=mov source=1x1 output=1x1 scaled=false fps=24 frames=24 cutIn=1 cutOut=24 out=view-0.mov\n"
                    f"[BATCH] RENDER_VERIFIED view-0 decoded_frames=24\n[BATCH] DONE view-0 exported=24 skipped=0 elapsed={elapsed}\n")
        mk = lambda d, v, e: prores_case(maxFrames=24, dualIsoExpected=True, stage=log(d, v), batch=batch(e), criteria=[])
        proc, got = evidence_cases([mk("0.000", "86400000.000000001", "86400.0"), mk("778440.000", "3000000.000009601", "3600.0"), mk("778440.000", "2999999.999", "3600.0")])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(got[0]["outcome"], "INVALID", got[0])
        self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID") and "ceiling" in p for p in got[0]["problems"]), got[0])
        self.assertEqual(got[1]["outcome"], "INVALID", got[1])
        self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p for p in got[1]["problems"]), got[1])
        self.assertEqual(got[2]["outcome"], "PASS", got[2])

    def test_adversary_r2f2_a6_the_reader_writes_no_git_trace_file(self) -> None:
        """pass 5 A6: the reader now runs git, and Invoke-DvGit passed GIT_TRACE through, so an inherited GIT_TRACE=<path> made a read-only reader append to that file."""
        with tempfile.TemporaryDirectory() as d:
            trace = Path(d) / "trace.txt"
            path = Path(d) / "cases.json"
            path.write_text(json.dumps([self.ctl(buildId=self.head_sha())]), encoding="utf-8")
            proc, got = run_pwsh_json("$cases = Get-Content -Raw -LiteralPath $env:DVE_CASES -Encoding utf8 | ConvertFrom-Json\n"
                                      f"@($cases | ForEach-Object {{ $c = $_; $e = {EVIDENCE_CALL}; [ordered]@{{ problems = @($e.problems) }} }}) "
                                      "| ConvertTo-Json -Compress -Depth 8 -AsArray", DVE_CASES=str(path), GIT_TRACE=str(trace), GIT_TRACE2=str(trace))
            self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
            self.assertEqual(got[0]["problems"], [], got[0])
            self.assertFalse(trace.exists(), "the reader wrote a git trace file")

    def test_r8_both_prores_leg_specs_gate_both_new_criteria(self) -> None:
        for name in ("m16-1243-export-prores", "m16-1456-export-prores"):
            spec = leg(name)
            for role in ("acceptance", "supplementary"):
                with self.subTest(leg=name, role=role):
                    crit = spec["criteria"][role]["cpu"]
                    for want in PIPE_CRITERIA:
                        self.assertIn(want, crit)
            self.assertIs(spec["export"]["dualIsoExpected"], True, name)
            self.assertNotIn("forceLookAssist", json.dumps(spec), name)


EXPORT_STUB_GENERATOR = r"""
param($SourceCommit,$BuildManifestSha256,$ClipId,$OutFile,$RepoRoot,$Venue,$Backend,$CpuQuiescenceThresholdPercent,$VenueTablePath,$PlaySeconds,$Format,$MaxFrames,$ReceiptPath,$DualIsoExpected,$ExpectReceiptSha256)
$cfg = Get-Content -LiteralPath $env:DVE_STUB -Raw | ConvertFrom-Json
Add-Content -LiteralPath $cfg.genLog -Value (($PSBoundParameters.Keys | Sort-Object | ForEach-Object { $_ + '=' + $PSBoundParameters[$_] }) -join ';')
Set-Content -LiteralPath $OutFile -Value "# stub export job"
[pscustomobject]@{ outFile = $OutFile; recommendedJobTimeoutSec = 900; clipContentSha256 = $cfg.clipContentSha256; playSeconds = $PlaySeconds; fixtureRehearsal = $false; legType = 'export' }
"""


# --- EXPORT-CDNG-STAGE-CONTAINMENT-1: the CDNG disjoint-stage containment check gates again, on a window table that holds only what CDNG export runs -------------
SRC = ROOT / "src"
LLRAWPROC_C = SRC / "mlv" / "llrawproc" / "llrawproc.c"
DNG_C = SRC / "dng" / "dng.c"
# the llrawproc timers dng.c publishes (dng.c:571-601) -> the table row each one is
LLRP_TIMERS = ("dark_frame", "vertical_stripes", "focus_pixels", "bad_pixels", "pattern_noise", "pre_dualiso_fix", "dual_iso", "chroma_smooth", "shared_lock",
               "dualiso_refine_lock", "publish_lock")
LLRP_LOCAL_ACCUMULATORS = ("dark_frame", "vertical_stripes", "focus_pixels", "bad_pixels", "pattern_noise", "dual_iso", "chroma_smooth")   # (copied to the globals at the worker's end)


def cdng_zero_profile(set_rows: dict, drop_total: bool = False) -> str:
    """A CDNG profile with every producer and llrawproc stage 0 except set_rows (the writer stages stay at 1 ms)."""
    doc = json.loads(cdng_profile(30, dual_iso_ms=0.0))
    for r in doc["frames"]:
        for k in CDNG_STAGES:
            if k not in ("writer_queue_wait_ms", "writer_completion_lag_ms", "disk_write_ms"):
                r[k] = 0.0
        if drop_total:
            r.pop("llrawproc_ms", None)
        r.update(set_rows)
    return json.dumps(doc)


def assert_containment_gates(tc: unittest.TestCase, row: dict, *needles: str) -> None:
    """EXPORT-CDNG-STAGE-CONTAINMENT-1: a CDNG disjoint-stage containment finding GATES: it is an EXPORT_STAGE_CONTAINMENT_MISMATCH problem and the outcome is INVALID."""
    tc.assertTrue(any(p.startswith("EXPORT_STAGE_CONTAINMENT_MISMATCH: ") and all(n in p for n in needles) for p in row["problems"]), row)
    tc.assertEqual(row["outcome"], "INVALID", row)


def _c_strip(text: str) -> str:
    """C text with comments, string and char literals blanked (newlines kept, so offsets and line numbers are unchanged)."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(c + re.sub(r"[^\n]", " ", text[i + 1:j]) + c)
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _c_splice(text: str) -> str:
    """C text with every backslash-newline spliced (translation phase 2), the line count kept: a spliced logical line is followed by as many empty lines as it absorbed."""
    # (adversary pass 2 A2: the ??/ trigraph is a backslash, and a backslash followed only by blanks before the newline splices too, as GCC reads it)
    lines, out, i = text.replace("??/", "\\").split("\n"), [], 0
    while i < len(lines):
        cur, extra = lines[i], 0
        while cur.rstrip(" \t\r").endswith("\\") and i + 1 < len(lines):
            i += 1
            cur, extra = cur.rstrip(" \t\r")[:-1] + lines[i], extra + 1
        out.append(cur)
        out.extend([""] * extra)
        i += 1
    return "\n".join(out)


def _match_close(s: str, i: int, open_c: str, close_c: str) -> int:
    """The index just past the bracket that closes s[i] (s[i] == open_c)."""
    depth = 0
    while i < len(s):
        depth += (s[i] == open_c) - (s[i] == close_c)
        i += 1
        if depth == 0:
            return i
    raise AssertionError(f"unbalanced {open_c}{close_c}")


def _c_functions(s: str) -> dict:
    """name -> [(params text, body start offset, body end offset)] for every function DEFINITION (a column-0 declarator whose parameter list is followed by '{')."""
    funcs: dict = {}
    for m in re.finditer(r"^[A-Za-z_][\w \t*]*?\b([A-Za-z_]\w*)[ \t]*\(", s, re.M):
        if m.group(1) in ("if", "while", "for", "switch", "return", "sizeof"):
            continue
        close = _match_close(s, m.end() - 1, "(", ")")
        j = close
        while j < len(s) and s[j] in " \t\r\n":
            j += 1
        if j < len(s) and s[j] == "{":
            funcs.setdefault(m.group(1), []).append((s[m.end():close - 1], j, _match_close(s, j, "{", "}")))
    return funcs


def derive_export_timer_windows(dng_text: str, llr_text: str) -> dict:
    """Re-derive, from the C sources alone, which llrawproc timer accrual sites CDNG export can reach.

    The root is dng.c's one llrawproc entry call (apply_llrawproc_locked, dng.c:103). Reachability is the call graph over llrawproc.c's function definitions from that
    entry; integer literals the entry passes bind the callee's parameters, and an `if (<param>)` block whose parameter is bound to 0 is dead (dng.c -> applyLLRawProcObject ->
    applyLLRawProcObjectWorker(..., NULL, 0), llrawproc.c:6411-6413, so the stop_before_dual_iso block at :5461-5472 never runs on export). An accrual site is a write of a timer
    global (or of the worker's local accumulator) from `(<now or end> - <start>)`; its window is <start's last assignment line>..<the accrual line>. A write of one timer from
    another (`g_shared += g_refine`) is a CONTAINS relation. Returns the reachable live function line spans, the windows per row, the contains relations, the call lines
    per function, and any accrual write the derivation could not read (each one is a failure: a site the guard cannot see is not a site it vouches for)."""
    dng = _c_strip(_c_splice(dng_text))
    llr = _c_strip(_c_splice(llr_text))
    uw = llr   # (adversary r1 A2: `(g_x) = v` is `g_x = v`; a parenthesized identifier that is no argument list is unwrapped IN PLACE, so offsets and lines are unchanged)
    while True:
        nxt = re.sub(r"(?<![\w)\]])\(\s*[A-Za-z_]\w*\s*\)", lambda m: " " + m.group(0)[1:-1] + " ", uw)
        if nxt == uw:
            break
        uw = nxt
    line_of = lambda s, off: s.count("\n", 0, off) + 1   # noqa: E731
    entries = sorted(set(re.findall(r"\b(applyLLRawProcObject\w*)\s*\(", dng)))
    funcs = _c_functions(llr)
    reach: dict = {}   # name -> bound params
    todo = [(e, {}) for e in entries]
    dead_spans: list = []
    live_spans: dict = {}
    calls: dict = {}   # callee -> [call line]
    while todo:
        name, bound = todo.pop()
        if name in reach or name not in funcs:
            continue
        reach[name] = bound
        for params, b0, b1 in funcs[name]:
            body = llr[b0:b1]
            # (adversary pass 2 A5: a literal binds a parameter only while the body never writes it or takes its address)
            eff = {p: v for p, v in bound.items()
                   if not re.search(rf"(?<![\w.>])(?:&\s*{p}(?!\w)|{p}\s*(?:[-+*/%&|^]?=(?!=)|<<=|>>=|\+\+|--)|(?:\+\+|--)\s*{p}(?!\w))", uw[b0:b1])}
            dead = []
            for m in re.finditer(r"\bif\s*\(\s*(!?)\s*([A-Za-z_]\w*)\s*\)\s*\{", body):
                if m.group(2) in eff and (eff[m.group(2)] == 0) != (m.group(1) == "!"):
                    k = b0 + m.end() - 1
                    dead.append((k, _match_close(llr, k, "{", "}")))
            dead_spans += [(line_of(llr, a), line_of(llr, z - 1)) for a, z in dead]
            live_spans.setdefault(name, []).append((line_of(llr, b0), line_of(llr, b1 - 1)))
            for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", body):
                callee, off = m.group(1), b0 + m.start()
                if callee not in funcs or any(a <= off < z for a, z in dead):
                    continue
                calls.setdefault(callee, []).append(line_of(llr, off))
                cb = {}
                args_end = _match_close(llr, b0 + m.end() - 1, "(", ")")
                args = [a.strip() for a in llr[b0 + m.end():args_end - 1].split(",")]
                pnames = [re.findall(r"([A-Za-z_]\w*)\s*$", p.strip())[0] if re.findall(r"([A-Za-z_]\w*)\s*$", p.strip()) else "" for p in funcs[callee][0][0].split(",")]
                for pn, a in zip(pnames, args):
                    if re.fullmatch(r"\d+", a):
                        cb[pn] = int(a)
                todo.append((callee, cb))
    def live(ln: int) -> bool:
        return any(a <= ln <= z for spans in live_spans.values() for a, z in spans) and not any(a <= ln <= z for a, z in dead_spans)

    windows: dict = {}
    contains: dict = {}
    unread: list = []
    targets = {f"g_llrawproc_last_{t}_ms": f"llrawproc_{t}" for t in LLRP_TIMERS}
    targets.update({f"{t}_ms": f"llrawproc_{t}" for t in LLRP_LOCAL_ACCUMULATORS})
    alt = "|".join(sorted(map(re.escape, targets), key=len, reverse=True))
    uw_lines = uw.split("\n")
    # adversary r1 A1/A2 (pass 2): FAIL CLOSED over whole statements. Every occurrence of a timer name in live code (or on any #define line) must be accounted for: the lvalue of
    # a `<timer> = ...;` / `<timer> += ...;` statement (which may span lines), or the whole right-hand side of one (a copy, or one timer accumulating another). Any other
    # occurrence -- &timer, a macro or call argument, ++ / -- / -= / *=, a subscript, a pointer write -- is a write this derivation cannot read, and is reported.
    write_re = re.compile(r"(?<![\w.>])(" + alt + r")\s*(\+=|=)(?!=)([^;{}]*);")
    handled: set = set()
    for m in write_re.finditer(uw):
        ln, end_ln = line_of(uw, m.start(1)), line_of(uw, m.end() - 1)
        if not (live(ln) and live(end_ln)):
            continue
        handled.add(m.start(1))
        row, rhs = targets[m.group(1)], " ".join(m.group(3).split())
        if re.fullmatch(r"0(\.0*)?", rhs):
            continue   # a reset
        if rhs in targets:
            handled.add(m.start(3) + m.group(3).index(rhs))
            if targets[rhs] != row:
                contains.setdefault(row, set()).add(targets[rhs])   # (one timer accumulating another); else the copy of the worker's local accumulator into its global
            continue
        d = re.fullmatch(r"\(\s*(?:mlv_stage_timing_now\s*\(\s*\)|[A-Za-z_]\w*)\s*-\s*([A-Za-z_]\w*)\s*\)\s*\*\s*1000\.0", rhs)
        if not d:
            unread.append(f"llrawproc.c:{ln}: {' '.join(m.group(0).split())}")
            continue
        starts = [s for s in re.finditer(rf"(?<![\w.>]){re.escape(d.group(1))}\s*=(?!=)", uw[:m.start(1)]) if live(line_of(uw, s.start()))]
        if not starts:
            unread.append(f"llrawproc.c:{ln}: no assignment of {d.group(1)} before the accrual")
            continue
        windows.setdefault(row, set()).add((line_of(uw, starts[-1].start()), end_ln))
    is_directive = lambda ln: uw_lines[ln - 1].lstrip().startswith("#")   # noqa: E731
    for m in re.finditer(r"(?<![\w.>])(" + alt + r")(?!\w)", uw):
        ln = line_of(uw, m.start())
        if m.start() not in handled and live(ln) and not is_directive(ln):
            unread.append(f"llrawproc.c:{ln}: {m.group(1)} is used in a way this derivation cannot read: {uw_lines[ln - 1].strip()}")
    # (a macro whose body names a timer -- the LLRP_LAST_RUN_TIMING X-macro, :7181 -- or names such a macro, writes the timers wherever it is EXPANDED: an expansion in
    # export-reachable code is a write this derivation cannot read)
    defs = [(m.group(1), m.group(2)) for m in re.finditer(r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)(.*)$", uw, re.M)]
    tainted = {n for n, body in defs if re.search(r"(?<![\w.>])(" + alt + r")(?!\w)", body)}
    grew = True
    while grew:
        grew = False
        for n, body in defs:
            if n not in tainted and any(re.search(rf"(?<!\w){re.escape(t)}(?!\w)", body) for t in tainted):
                tainted.add(n)
                grew = True
    for n in sorted(tainted):
        for m in re.finditer(rf"(?<!\w){re.escape(n)}(?!\w)", uw):
            ln = line_of(uw, m.start())
            if live(ln) and not is_directive(ln):
                unread.append(f"llrawproc.c:{ln}: the macro {n}, which names a timer, is expanded in export-reachable code")
    return {"entries": entries, "reachable": sorted(reach), "live": live, "windows": windows, "contains": contains, "calls": calls, "unread": unread,
            "funcLines": {n: [(line_of(llr, b0), line_of(llr, b1 - 1)) for _p, b0, b1 in v] for n, v in funcs.items()}}


def table_vs_export_problems(table: list, derived: dict) -> list:
    """Every way the module's ExportCdngStageWindowTable differs from the export-reachable derivation (an empty list when it equals it)."""
    rng = re.compile(r"^llrawproc\.c:(\d+)-(\d+)$")
    problems = []
    rows = {r["name"]: r for r in table}
    for name, r in rows.items():
        wins = [w for w in (r.get("windows") or []) if w.startswith("llrawproc.c")]
        spans = []
        for w in wins:
            m = rng.match(w)
            if not m:
                problems.append(f"{name}: unreadable window {w}")
                continue
            a, z = int(m.group(1)), int(m.group(2))
            spans.append((a, z))
            if not all(derived["live"](ln) for ln in (a, z)):
                problems.append(f"{name}: window {w} is not in code CDNG export reaches")
        own = set(derived["windows"].get(name, ()))
        for inner in derived["contains"].get(name, ()):   # (a timer that accumulates another one accrues in that one's windows too: shared_lock += refine / publish)
            own |= set(derived["windows"].get(inner, ()))
        for w, (a, z) in zip(wins, spans):
            held = [(sa, sz) for sa, sz in own if sa <= z and a <= sz]
            if held:
                # (adversary r1 A1, pass 2 A3: the window's lines are EXACTLY the union of the accrual windows it holds -- no wider, and no untimed gap between two of them,
                # so dual_iso widened to the whole worker, or to :5560-6219 across the refine lock, is refused)
                covered = set().union(*(range(sa, sz + 1) for sa, sz in held))
                if covered != set(range(a, z + 1)):
                    problems.append(f"{name}: window {w} is not the union of the export-reachable accrual windows it holds {sorted(held)}")
                continue
            # (a callee's lines count when every one of its live call sites is inside the row's other windows: the table's header rule)
            owners = [f for f in derived["reachable"] if f != "applyLLRawProcObjectWorker" and any(fa <= a and z <= fz for fa, fz in derived["funcLines"].get(f, []))]
            others = [s for s in spans if s != (a, z)]
            if owners and all(derived["calls"].get(f) and all(any(oa <= c <= oz for oa, oz in others) for c in derived["calls"][f]) for f in owners):
                continue
            problems.append(f"{name}: window {w} holds no accrual site CDNG export reaches")
        for sa, sz in sorted(derived["windows"].get(name, ())):
            if not any(a <= sa and sz <= z for a, z in spans):
                problems.append(f"{name}: the export-reachable accrual window llrawproc.c:{sa}-{sz} is not in the row")
        missing = derived["contains"].get(name, set()) - set(r.get("contains") or [])
        if missing:
            problems.append(f"{name}: accumulates {sorted(missing)} but its contains column does not name them")
        extra = set(r.get("contains") or []) - derived["contains"].get(name, set())
        if extra:
            problems.append(f"{name}: its contains column names {sorted(extra)}, which it does not accumulate")
    for name in derived["windows"]:
        if name not in rows:
            problems.append(f"{name}: an export-reachable timer with no table row")
    # (adversary pass 2 A4: the rest of the table's structure is compared too: one row per name; every published llrawproc timer (dng.c:571-601) is a row whose parent is
    # llrawproc and whose windows are llrawproc.c line ranges only; llrawproc_other is the remainder of exactly the seven timed children (dng.c:579-604) and has no window)
    names = [r["name"] for r in table]
    problems += [f"{n}: duplicated row" for n in sorted({n for n in names if names.count(n) > 1})]
    expected = {f"llrawproc_{t}" for t in LLRP_TIMERS} | {"llrawproc_other"}
    problems += [f"{n}: a published llrawproc timer with no table row" for n in sorted(expected - set(rows))]
    for name, r in rows.items():
        if not name.startswith("llrawproc_"):
            continue
        if name not in expected:
            problems.append(f"{name}: not a timer dng.c publishes")
        if r.get("parent") != "llrawproc":
            problems.append(f"{name}: parent {r.get('parent')!r}, not llrawproc")
        problems += [f"{name}: window {w} is not an llrawproc.c line range" for w in (r.get("windows") or []) if not rng.match(w)]
        if name == "llrawproc_other" and (set(r.get("remainderOf") or []) != {f"llrawproc_{t}" for t in LLRP_LOCAL_ACCUMULATORS} or r.get("windows")):
            problems.append(f"{name}: not the windowless remainder of the seven timed children")
        elif name != "llrawproc_other" and r.get("remainderOf"):
            problems.append(f"{name}: carries a remainderOf column")
    return problems


def cdng_total_profile(build_id: str = BUILD_ID, zero: bool = False, set_rows: dict | None = None, per_frame: dict | None = None, drop: tuple = (), drop_llrawproc: bool = False) -> str:
    """EXPORT-CDNG-TOTAL-CONTAINMENT-1: a 30-frame CDNG profile. zero: every producer and llrawproc stage 0 first (the boundary cases). set_rows: fields set on every frame.
    per_frame: frame position -> fields set on that frame only. drop: fields removed from every frame. drop_llrawproc: no llrawproc stage at all (the required dual-ISO field stays, at 0)."""
    doc = json.loads(cdng_profile(30, dual_iso_ms=0.0 if zero else 30.0, build_id=build_id))
    for r in doc["frames"]:
        if zero:
            for k in CDNG_STAGES:
                if k not in ("writer_queue_wait_ms", "writer_completion_lag_ms", "disk_write_ms"):
                    r[k] = 0.0
        if drop_llrawproc:
            for k in [k for k in r if k.startswith("llrawproc") and k != "llrawproc_dual_iso_ms"]:
                del r[k]
            r["llrawproc_dual_iso_ms"] = 0.0
        r.update(set_rows or {})
        for k in drop:
            r.pop(k, None)
    for i, fields in (per_frame or {}).items():
        doc["frames"][i].update(fields)
    return json.dumps(doc)


CONTAINMENT = "EXPORT_STAGE_CONTAINMENT_MISMATCH"
SUM_MISMATCH = "EXPORT_STAGE_SUM_MISMATCH"
RECORD_INVALID = "EXPORT_RECORD_INVALID"
# (name, cdng_total_profile arguments, the typed reason, text the problem must name). Dot I5 (#838 ask 6967301971): ONLY llrawproc_ms changes (40 -> 900) beside producer frames of 62 / 63 / 64 ms.
TOTAL_CONTAINMENT_BAD = (
    ("dot_i5_only_the_total_changes_40_to_900", dict(set_rows={"llrawproc_ms": 900.0}), CONTAINMENT, "llrawproc"),
    # a single frame's total past its producer frame (130 ms in 63 ms) that the 30-frame mean (43 ms) hides; its other_ms is its remainder, so only the aggregate-parent pair can see it
    ("one_frame_total_past_its_producer_frame_the_mean_hides", dict(per_frame={7: {"llrawproc_ms": 130.0, "llrawproc_other_ms": 94.0}}), CONTAINMENT, "frame_index 7"),
    # a total inside producer_frame (60 of 63 ms) that the llrawproc children (7 timed 36 + other 4 = 40 ms) do not hold
    ("total_inside_producer_frame_but_past_its_children", dict(set_rows={"llrawproc_ms": 60.0}), SUM_MISMATCH, "llrawproc_ms"),
    # the children's own sum past the bracket that contains them on ONE frame (110 ms of children inside a 40 ms total): nesting is violated on that frame, whatever the mean says
    ("one_frame_children_past_the_total_the_mean_hides", dict(per_frame={7: {"llrawproc_dual_iso_ms": 100.0}}), SUM_MISMATCH, "frame_index 7"),
    # tolerance is 5 % + 1 ms: against a 100 ms parent (or 100 ms of children) 106.0 is the last honest figure
    ("pair_just_above_tolerance", dict(zero=True, set_rows={"producer_frame_ms": 100.0, "llrawproc_ms": 106.5, "llrawproc_dual_iso_ms": 100.0, "llrawproc_other_ms": 6.5}), CONTAINMENT, "llrawproc_ms"),
    ("sum_just_above_tolerance", dict(zero=True, set_rows={"producer_frame_ms": 120.0, "llrawproc_ms": 106.5, "llrawproc_dual_iso_ms": 100.0}), SUM_MISMATCH, "llrawproc_ms"),
    # r2 (opus H3 / fable H4): the tenth-rule boundary (4 of 30 refused, 3 of 30 a control) and a whole frame's worker total BELOW its children (the second disjunct of the identity)
    ("bracket_past_its_children_on_four_of_thirty_frames", dict(per_frame={i: {"llrawproc_ms": 60.0} for i in range(4)}), SUM_MISMATCH, "llrawproc_ms"),
    ("worker_total_below_its_children_on_one_frame", dict(per_frame={7: {"llrawproc_total_ms": 10.0}}), SUM_MISMATCH, "llrawproc_total_ms"),
    # r2 fix 1 A4 (adversary pass on 62dff0fa): the worker is the call the llrawproc_ms bracket wraps (dng.c:2619), so a worker total past the bracket (and past producer_frame_ms when the bracket is
    # absent) is outside the work that contains it, whether the remainder is omitted (36 ms of present children cannot refute 900), or all eight figures are present and agree with the worker (41 / 44)
    ("a4_worker_total_past_the_bracket_other_omitted", dict(drop=("llrawproc_other_ms",), per_frame={7: {"llrawproc_total_ms": 900.0}}), CONTAINMENT, "frame_index 7"),
    ("a4_worker_total_past_the_bracket_agreeing_with_its_children", dict(per_frame={7: {"llrawproc_other_ms": 5.0, "llrawproc_total_ms": 44.0}}), CONTAINMENT, "frame_index 7"),
    ("a4_worker_total_past_producer_frame_bracket_absent", dict(drop=("llrawproc_other_ms", "llrawproc_ms"), per_frame={7: {"llrawproc_total_ms": 900.0}}), CONTAINMENT, "frame_index 7"),
    # a systematic gap, not a hiccup: the bracket 20 ms past its children on 5 of 30 frames (more than a tenth of them)
    ("bracket_past_its_children_on_a_sixth_of_the_frames", dict(per_frame={i: {"llrawproc_ms": 60.0} for i in range(5)}), SUM_MISMATCH, "llrawproc_ms"),
    # adversary A4 (pre-existing, in scope): the worker's own total (llrawproc_total_ms, dng.c:588) is the exact identity other = max(0, total - timed children), so a total of 900 ms
    # beside 40 ms of children is a forged field whatever llrawproc_ms says
    ("worker_total_past_its_children_on_one_frame", dict(per_frame={7: {"llrawproc_total_ms": 900.0}}), SUM_MISMATCH, "llrawproc_total_ms"),
    # EXPORT-CDNG-TOTAL-CONTAINMENT-1 r2 B1 (sol r1 INV-4): a PRESENT worker total that is not a conforming timing is a typed record problem, never "absent" (each read PASS on 29b664c2)
    ("worker_total_is_a_string", dict(per_frame={7: {"llrawproc_total_ms": "900"}}), RECORD_INVALID, "llrawproc_total_ms"),
    ("worker_total_is_null", dict(per_frame={7: {"llrawproc_total_ms": None}}), RECORD_INVALID, "llrawproc_total_ms"),
    ("worker_total_is_an_array", dict(per_frame={7: {"llrawproc_total_ms": [900]}}), RECORD_INVALID, "llrawproc_total_ms"),
    ("worker_total_is_past_the_ceiling", dict(per_frame={7: {"llrawproc_total_ms": 86400001}}), RECORD_INVALID, "llrawproc_total_ms"),
    ("worker_total_is_negative", dict(per_frame={7: {"llrawproc_total_ms": -1.0}}), RECORD_INVALID, "llrawproc_total_ms"),
    ("worker_total_is_a_boolean", dict(per_frame={7: {"llrawproc_total_ms": True}}), RECORD_INVALID, "llrawproc_total_ms"),
    # B2 (sol r1 INV-2): llrawproc_other_ms omitted throughout, so the sum is not derivable, but the children that ARE present (36 ms + a 100 ms dual-ISO on frame 7 = 106 ms) are a lower bound
    # that a 40 ms bracket (and a 40 ms worker total) cannot hold
    ("other_omitted_one_frame_children_lower_bound_past_the_total", dict(drop=("llrawproc_other_ms",), per_frame={7: {"llrawproc_dual_iso_ms": 100.0}}), SUM_MISMATCH, "frame_index 7"),
    ("other_omitted_children_lower_bound_past_the_worker_total", dict(drop=("llrawproc_other_ms", "llrawproc_ms"), per_frame={7: {"llrawproc_dual_iso_ms": 100.0, "llrawproc_total_ms": 40.0}}),
     SUM_MISMATCH, "llrawproc_total_ms"),
    ("other_omitted_worker_total_below_the_present_children", dict(drop=("llrawproc_other_ms",), per_frame={7: {"llrawproc_total_ms": 10.0}}), SUM_MISMATCH, "llrawproc_total_ms"),
    # a second child omitted too: the lower bound is still of what is present
    ("two_children_omitted_the_rest_past_the_total", dict(drop=("llrawproc_other_ms", "llrawproc_dark_frame_ms"), per_frame={7: {"llrawproc_dual_iso_ms": 100.0}}), SUM_MISMATCH, "frame_index 7"),
)
TOTAL_CONTAINMENT_CONTROLS = (
    ("normal", dict(), {}),
    ("total_absent_children_present", dict(drop=("llrawproc_ms",)), {}),
    ("other_absent_the_sum_cannot_be_derived", dict(drop=("llrawproc_other_ms",)), {}),
    ("one_frame_total_inside_both_parents", dict(per_frame={7: {"llrawproc_ms": 60.0, "llrawproc_other_ms": 24.0}}), {}),
    # a bracket longer than its children on ONE frame (60 ms beside 40 ms of children) is bracket entry/exit time the worker's clock does not see: a systematic-gap rule (more than a tenth
    # of the frames), which one frame does not meet. r2 (opus H3 / fable H4 boundary): 3 of 30 frames is exactly a tenth and is accepted; 4 of 30 is refused (bad case below)
    ("one_frame_bracket_longer_than_its_children_is_under_a_tenth_of_the_frames", dict(per_frame={7: {"llrawproc_ms": 60.0}}), {}),
    ("three_of_thirty_frames_bracket_longer_than_its_children_is_exactly_a_tenth", dict(per_frame={i: {"llrawproc_ms": 60.0} for i in range(3)}), {}),
    # adversary A1: ONE scheduler pause outside the worker clock (100 ms in the bracket and the producer, none in the children) must not void correct evidence: the mean moves by 3.3 ms
    ("one_scheduler_pause_outside_the_worker_clock", dict(per_frame={7: {"llrawproc_ms": 140.0, "producer_frame_ms": 163.0}}), {}),
    # adversary A2: equality at 5 % + 1 ms is accepted when the figures are decimals the binary product lands a hair under (40.00046 * 1.05 + 1 = 43.000482999999996 < 43.000483)
    ("pair_at_tolerance_with_decimal_figures", dict(zero=True, set_rows={"producer_frame_ms": 40.00046, "llrawproc_ms": 43.000483, "llrawproc_dual_iso_ms": 43.000483}), {}),
    ("sum_at_tolerance_with_decimal_figures", dict(zero=True, set_rows={"producer_frame_ms": 120.0, "llrawproc_ms": 32.50021, "llrawproc_dual_iso_ms": 30.0002}), {}),
    ("worker_total_agrees_with_its_children", dict(set_rows={"llrawproc_total_ms": 40.0}), {}),
    # r2 fix 1 A4 control: the worker total at the bracket's own tolerance (43 ms inside a 40 ms bracket, children 36 + other 7 = 43) is inside it
    ("worker_total_at_the_bracket_tolerance", dict(per_frame={7: {"llrawproc_other_ms": 7.0, "llrawproc_total_ms": 43.0}}), {}),
    # r2 fix 1 A2 (adversary pass on 62dff0fa): a figure exactly at the decimal limit is accepted at HIGH MAGNITUDE too, where a binary double product is several ulps (3.7e-9 ms at 2e7) past the
    # decimal: 20000000.00038 * 1.05 + 1 = 21000001.000399 on every frame, and the mean of 30 such figures. The batch elapsed is long enough that the wall is not what is held.
    ("every_frame_at_the_decimal_limit_at_high_magnitude",
     dict(zero=True, set_rows={"producer_frame_ms": 20000000.00038, "llrawproc_ms": 21000001.000399, "llrawproc_dual_iso_ms": 21000001.000399}),
     dict(batch=batch_log(30, elapsed=1000000.0))),
    # r2 fix 1 A3: the wall gate has the same published-precision slack as every pair: 29 frames at the wall's limit (3.3333 ms/frame * 1.05 + 1 = 4.5) and one 9 nanoseconds over it is a mean 0.3 ns past the limit
    # (below the 6-decimal precision the figures are written at), which the means rounded to that precision accepted and an unrounded comparison refused
    ("wall_gate_mean_a_sub_precision_hair_past_the_limit",
     dict(zero=True, set_rows={"producer_frame_ms": 4.5, "llrawproc_ms": 1.0, "llrawproc_dual_iso_ms": 1.0}, per_frame={0: {"producer_frame_ms": 4.500009}}),
     dict(batch=batch_log(30, elapsed=0.1))),
    # r2 B2 controls: the lower bound only judges what is PRESENT: children inside the bracket and the worker total with the remainder omitted stay valid
    ("other_omitted_worker_total_agrees_with_the_present_children", dict(drop=("llrawproc_other_ms",), set_rows={"llrawproc_total_ms": 40.0}), {}),
    ("other_omitted_present_children_just_inside_the_tolerance", dict(zero=True, drop=("llrawproc_other_ms",), set_rows={"producer_frame_ms": 120.0, "llrawproc_ms": 106.0, "llrawproc_dual_iso_ms": 100.0}), {}),
    # r2 B3 (sol r1 INV-5): 29 frames at the documented 5 % + 1 ms tolerance (producer 40, bracket 43, children 43) and one frame a hair inside it: the means (40.0000093, 43.0000098) sit AT the
    # tolerance; rounded to 6 places before the gate (40.000009 -> 43.00000945 < 43.00001) they read INVALID on 29b664c2
    ("every_frame_at_the_tolerance_the_rounded_means_must_not_reject",
     dict(zero=True, set_rows={"producer_frame_ms": 40.0, "llrawproc_ms": 43.0, "llrawproc_dual_iso_ms": 43.0},
          per_frame={0: {"producer_frame_ms": 40.00028, "llrawproc_ms": 43.000294, "llrawproc_dual_iso_ms": 43.000294}}), {}),
    ("pair_at_tolerance", dict(zero=True, set_rows={"producer_frame_ms": 100.0, "llrawproc_ms": 106.0, "llrawproc_dual_iso_ms": 100.0, "llrawproc_other_ms": 6.0}), {}),
    ("sum_at_tolerance", dict(zero=True, set_rows={"producer_frame_ms": 120.0, "llrawproc_ms": 106.0, "llrawproc_dual_iso_ms": 100.0}), {}),
    # a clip with no llrawproc stage at all (permitted absence: the app ran none of it) stays valid
    ("no_llrawproc_stage_at_all", dict(drop_llrawproc=True), dict(dualIsoExpected=False, criteria=[{"metric": "framesExported", "op": "eq", "value": 30},
                                                                                                    {"metric": "exportFramesFailed", "op": "eq", "value": 0},
                                                                                                    {"metric": "profileFramesSucceeded", "op": "eq", "value": 30}])),
)


@requires_windows_pwsh
class CdngTotalContainmentTests(unittest.TestCase):
    """EXPORT-CDNG-TOTAL-CONTAINMENT-1 (Dot I5-EXPORT-CDNG-TOTAL-CONTAINMENT on #838, ask 6967301971): the llrawproc TOTAL is held inside producer_frame on every frame and
    agrees with the llrawproc children it is the sum of, under empty and shipped criteria. The writer and the receipt re-derivation read the same reader (ExportRunnerTests)."""

    def test_the_total_is_held_inside_producer_frame_and_agrees_with_its_children_under_empty_and_shipped_criteria(self) -> None:
        cases = []
        for _name, kw, _code, _needle in TOTAL_CONTAINMENT_BAD:
            cases.append(cdng_case(profile=cdng_total_profile(**kw), criteria=[]))
            cases.append(cdng_case(profile=cdng_total_profile(**kw)))
        for _name, kw, over in TOTAL_CONTAINMENT_CONTROLS:
            cases.append(cdng_case(profile=cdng_total_profile(**kw), **over))
        proc, got = evidence_cases(cases)
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        for n, (name, _kw, code, needle) in enumerate(TOTAL_CONTAINMENT_BAD):
            for which, row in (("empty criteria", got[2 * n]), ("shipped criteria", got[2 * n + 1])):
                with self.subTest(case=name, criteria=which):
                    self.assertEqual(row["outcome"], "INVALID", row)
                    self.assertTrue(any(p.startswith(code + ": ") and needle in p for p in row["problems"]), row)
        for n, (name, _kw, _over) in enumerate(TOTAL_CONTAINMENT_CONTROLS):
            row = got[2 * len(TOTAL_CONTAINMENT_BAD) + n]
            with self.subTest(control=name):
                self.assertEqual((row["problems"], row["outcome"]), ([], "PASS"), row)

    def test_a_timing_that_is_a_json_object_with_a_getType_key_is_a_typed_record_problem_not_a_throw(self) -> None:
        """r2 fix 1 A1 (adversary pass on 62dff0fa): a JSON OBJECT {"GetType": null} where a timing belongs made the schema's own diagnostic (`$Value.GetType().Name`) throw, so the writer recorded
        RUNNER_ERROR and a direct receipt re-derivation threw. A present value of any shape, on the worker total or on any other timing field, is typed EXPORT_RECORD_INVALID. Kept apart from the
        table test: a throw loses every case of its run."""
        hostile = {"GetType": None}
        fields = ("llrawproc_total_ms", "raw_read_ms", "producer_frame_ms", "llrawproc_ms")
        proc, got = evidence_cases([cdng_case(profile=cdng_total_profile(per_frame={7: {f: hostile}}), criteria=[]) for f in fields]
                                   + [cdng_case(profile=cdng_total_profile(per_frame={7: {"llrawproc_total_ms": {"GetType": "x"}}}), criteria=[])])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        # (a case that throws inside the ForEach-Object yields no row, so every case must have one: a short list is the throw)
        self.assertEqual(len(got), len(fields) + 1, (len(got), proc.stderr[-3000:]))
        # (under the harness's default preference the failed method call is a non-terminating error and the reason prints "is a , not a JSON number"; under Stop, as the writer runs, it throws)
        self.assertNotIn("Method invocation failed", proc.stderr + proc.stdout)
        for f, row in zip(fields + ("llrawproc_total_ms",), got):
            with self.subTest(field=f):
                self.assertFalse(any("is a , not" in p for p in row["problems"]), row)
                self.assertEqual(row["outcome"], "INVALID", row)
                self.assertTrue(any(p.startswith("EXPORT_RECORD_INVALID: ") and f in p for p in row["problems"]), row)

    def test_the_rendered_path_gates_read_unrounded_means_so_a_log_at_the_tolerance_is_not_rejected(self) -> None:
        """r2 B3 (sol r1 INV-5), the rendered sibling of the CDNG case: 29 frames of processed16_total 40 / debayered_frame 43 (processing 0) and one frame a hair inside the 5 % + 1 ms
        tolerance. The means (40.0000093, 43.0000098) are AT the tolerance; rounded to 6 places before the gate they read EXPORT_STAGE_CONTAINMENT_MISMATCH. One frame genuinely past it is
        still refused."""
        def log(first_p16: float, first_deb: float, rest_deb: float = 43.0) -> str:
            out = []
            for i in range(30):
                p16, deb = (first_p16, first_deb) if i == 0 else (40.0, rest_deb)
                for name, ms in (("raw_uint16_disk_read", 3.0), ("raw_uint16_decompress", 5.0), ("raw_uint16", 9.0), ("llrawproc", 20.0), ("debayered_frame", deb), ("processing", 0.0), ("processed16_total", p16)):
                    out.append(f"[mlv-stage-timing] frame={i} stage={name} ms={ms:.6f}")
            return "\n".join(out) + "\n"
        proc, got = evidence_cases([prores_case(stage=log(40.00028, 43.000294)), prores_case(stage=log(40.0, 43.5, rest_deb=43.5))])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual((got[0]["problems"], got[0]["outcome"]), ([], "PASS"), got[0])
        # (control: every frame past the tolerance, a mean of 43.5 beside 40, is still refused)
        self.assertEqual(got[1]["outcome"], "INVALID", got[1])
        self.assertTrue(any("EXPORT_STAGE_CONTAINMENT_MISMATCH" in p for p in got[1]["problems"]), got[1])


    def test_a_mean_a_full_emitted_increment_over_the_exact_limit_is_refused_and_one_at_the_limit_is_not(self) -> None:
        """r3 B1 (sol r2 INV-4, literal inputs): the comparator rounded BOTH sides to the writers' 6 decimals before comparing, so a mean over the limit by one full emitted increment
        (1e-6 ms; 128 frames of elapsed=12.3 -> limit 101.8984375, producer timings split 101.898438 / 101.898439 -> mean 101.8984385, both round to 101.898438) read PASS at every gate that uses
        it. The same literal input at each: the CDNG and ProRes wall gates, the CDNG mean-pair gate, the disjoint-mean gate and the per-frame sum. Each has a control exactly AT its limit."""
        def wall(lo: float, hi: float) -> dict:
            doc = json.loads(cdng_profile(128))
            for i, r in enumerate(doc["frames"]):
                r["producer_frame_ms"] = lo if i < 64 else hi
            return cdng_case(maxFrames=128, profile=json.dumps(doc), batch=batch_log(128, elapsed=12.3), manifest=output_manifest(128), criteria=[])

        def prores_wall(lo: float, hi: float) -> dict:
            out = []
            for i in range(128):
                p16 = lo if i < 64 else hi
                for name, ms in (("raw_uint16_disk_read", 0.0), ("raw_uint16_decompress", 0.0), ("raw_uint16", 0.0), ("llrawproc", 0.0), ("debayered_frame", p16), ("processing", 0.0), ("processed16_total", p16)):
                    out.append(f"[mlv-stage-timing] frame={i} stage={name} ms={ms:.6f}")
            return prores_case(maxFrames=128, stage="\n".join(out) + "\n", batch=batch_log(128, verified=128, elapsed=12.3), criteria=[])

        def pair(lo: float, hi: float) -> dict:   # producer 40.000030, raw_read split lo / hi (limit 43.0000315)
            return cdng_case(dualIsoExpected=False, criteria=[], profile=cdng_total_profile(zero=True, set_rows={"producer_frame_ms": 40.00003}, per_frame={i: {"raw_read_ms": lo if i % 2 == 0 else hi} for i in range(30)}))

        def disjoint(lo: float, hi: float) -> dict:   # producer 40.000030, raw_read 20, compress split lo / hi (sum limit 43.0000315)
            return cdng_case(dualIsoExpected=False, criteria=[], profile=cdng_total_profile(zero=True, set_rows={"producer_frame_ms": 40.00003, "raw_read_ms": 20.0},
                                                                                           per_frame={i: {"dng_compress_encode_ms": lo if i % 2 == 0 else hi} for i in range(30)}))

        def frame_sum(bracket: float, children: float) -> dict:   # bracket 60.00001 beside children 64.000011: over the exact limit 64.0000105 by 5e-7
            return cdng_case(criteria=[], profile=cdng_total_profile(zero=True, set_rows={"producer_frame_ms": 100.0, "llrawproc_ms": bracket, "llrawproc_dual_iso_ms": children}))

        bad = (("cdng_wall_gate", wall(101.898438, 101.898439), CONTAINMENT, "producer_frame"),
               ("prores_wall_gate", prores_wall(101.898438, 101.898439), CONTAINMENT, "processed16_total"),
               ("cdng_mean_pair_gate", pair(43.000032, 43.000033), CONTAINMENT, "raw_read"),
               ("cdng_disjoint_mean_gate", disjoint(23.000032, 23.000033), CONTAINMENT, "disjoint stages"),
               ("per_frame_sum_over_by_half_an_increment", frame_sum(60.00001, 64.000011), SUM_MISMATCH, "llrawproc_ms"))
        controls = (("cdng_wall_gate_at_the_limit", wall(101.898437, 101.898438)),
                    ("prores_wall_gate_at_the_limit", prores_wall(101.898437, 101.898438)),
                    ("cdng_mean_pair_gate_at_the_limit", pair(43.000031, 43.000032)),
                    ("cdng_disjoint_mean_gate_at_the_limit", disjoint(23.000031, 23.000032)),
                    ("per_frame_sum_at_the_limit", frame_sum(60.00002, 64.000021)))
        proc, got = evidence_cases([c for _n, c, _code, _needle in bad] + [c for _n, c in controls])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(len(got), len(bad) + len(controls), (len(got), proc.stderr[-3000:]))
        for (name, _c, code, needle), row in zip(bad, got):
            with self.subTest(case=name):
                self.assertEqual(row["outcome"], "INVALID", row)
                self.assertTrue(any(p.startswith(code + ": ") and needle in p for p in row["problems"]), row)
        for (name, _c), row in zip(controls, got[len(bad):]):
            with self.subTest(control=name):
                self.assertEqual((row["problems"], row["outcome"]), ([], "PASS"), row)

    def test_the_wall_gate_limit_is_built_from_the_exact_done_elapsed_never_the_rounded_published_figure(self) -> None:
        """r4 B1 (opus r3, DEFECT_CLASS rounded-gate-input, literal inputs): the DONE regex accepts up to 9 decimals of elapsed, but the wall limit was built from wallSeconds, which is
        rounded to 6 decimals for publication. elapsed=12.2999996 rounds to 12.3, so the limit moved from the exact 101.89843421875 (+ the 4e-7 epsilon) to 101.8984375 and a mean of 101.8984375
        read PASS at both wall gates (CDNG producer_frame, ProRes processed16_total). The same input at each, a one-frame run (elapsed=0.0999996, mean 105.9999), and controls at the exact limit."""
        def exact(text: str) -> str:
            return batch_log(128, elapsed=12.3).replace("elapsed=12.3", f"elapsed={text}")

        def cdng_wall(lo: float, hi: float, elapsed: str = "12.2999996") -> dict:
            doc = json.loads(cdng_profile(128))
            for i, r in enumerate(doc["frames"]):
                r["producer_frame_ms"] = lo if i < 64 else hi
            return cdng_case(maxFrames=128, profile=json.dumps(doc), batch=exact(elapsed), manifest=output_manifest(128), criteria=[])

        def prores_wall(lo: float, hi: float, elapsed: str = "12.2999996") -> dict:
            out = []
            for i in range(128):
                p16 = lo if i < 64 else hi
                for name, ms in (("raw_uint16_disk_read", 0.0), ("raw_uint16_decompress", 0.0), ("raw_uint16", 0.0), ("llrawproc", 0.0), ("debayered_frame", p16), ("processing", 0.0), ("processed16_total", p16)):
                    out.append(f"[mlv-stage-timing] frame={i} stage={name} ms={ms:.6f}")
            return prores_case(maxFrames=128, stage="\n".join(out) + "\n", batch=batch_log(128, verified=128, elapsed=12.3).replace("elapsed=12.3", f"elapsed={elapsed}"), criteria=[])

        def one_frame() -> dict:   # exact limit 99.9996 * 1.05 + 1 = 105.99958 (+ 4e-7); the rounded elapsed 0.1 gives 106.0
            doc = json.loads(cdng_profile(1))
            doc["frames"][0]["producer_frame_ms"] = 105.9999
            return cdng_case(maxFrames=1, profile=json.dumps(doc), batch=batch_log(1, elapsed=0.1).replace("elapsed=0.1", "elapsed=0.0999996"), manifest=output_manifest(1), criteria=[])

        bad = (("cdng_wall_gate_3_increments_over", cdng_wall(101.898437, 101.898438), "producer_frame"),
               ("prores_wall_gate_3_increments_over", prores_wall(101.898437, 101.898438), "processed16_total"),
               ("cdng_one_frame_elapsed_0_0999996", one_frame(), "producer_frame"))
        controls = (("cdng_wall_gate_at_the_exact_limit", cdng_wall(101.898434, 101.898435)),
                    ("prores_wall_gate_at_the_exact_limit", prores_wall(101.898434, 101.898435)),
                    ("cdng_wall_gate_nine_decimals_that_round_to_itself", cdng_wall(101.898437, 101.898438, elapsed="12.300000000")))
        proc, got = evidence_cases([c for _n, c, _needle in bad] + [c for _n, c in controls])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(len(got), len(bad) + len(controls), (len(got), proc.stderr[-3000:]))
        for (name, _c, needle), row in zip(bad, got):
            with self.subTest(case=name):
                self.assertEqual(row["outcome"], "INVALID", row)
                self.assertTrue(any(p.startswith(CONTAINMENT + ": ") and needle in p for p in row["problems"]), row)
        for (name, _c), row in zip(controls, got[len(bad):]):
            with self.subTest(control=name):
                self.assertEqual((row["problems"], row["outcome"]), ([], "PASS"), row)


@requires_windows_pwsh
class CdngStageContainmentGateTests(unittest.TestCase):
    """EXPORT-CDNG-STAGE-CONTAINMENT-1 (split from EXPORT-VENUE-LEG-1 / #838 by the hub STOP RULE of 2026-11-18T17:26:25Z): the six members #838 found one round at a time, each
    built from its round's repro, now GATE (INVALID EXPORT_STAGE_CONTAINMENT_MISMATCH) on CDNG-profile legs, and the window table is the code path CDNG export runs."""

    def run_cases(self, profiles: list, dual_iso: bool = True) -> list:
        proc, got = evidence_cases([cdng_case(dualIsoExpected=dual_iso, profile=p, criteria=[]) for p in profiles])
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        return got

    def assert_controls_pass(self, rows: list) -> None:
        for i, row in enumerate(rows):
            with self.subTest(control=i):
                self.assertFalse(any("EXPORT_STAGE_CONTAINMENT" in p for p in row["problems"]), row)
                self.assertEqual(row["outcome"], "PASS", row)

    def test_member_r11_disjoint_producer_stages_and_llrawproc_children_gate_inside_their_parents(self) -> None:
        """r11 (fable r9 B2): 40 + 30 + 30 + 40 ms of disjoint producer stages inside a 41 ms producer_frame, and dark 40 + stripes 30 + focus 30 inside a 41 ms llrawproc."""
        got = self.run_cases([cdng_zero_profile({"raw_read_ms": 40.0, "raw_decode_ms": 30.0, "dng_pack_ms": 30.0, "payload_clone_ms": 40.0, "producer_frame_ms": 41.0}),
                              cdng_zero_profile({"llrawproc_dark_frame_ms": 40.0, "llrawproc_vertical_stripes_ms": 30.0, "llrawproc_focus_pixels_ms": 30.0, "llrawproc_ms": 41.0,
                                                 "producer_frame_ms": 200.0}),
                              cdng_zero_profile({"raw_read_ms": 10.0, "raw_decode_ms": 10.0, "dng_pack_ms": 10.0, "payload_clone_ms": 10.0, "llrawproc_dark_frame_ms": 5.0,
                                                 "llrawproc_vertical_stripes_ms": 5.0, "llrawproc_ms": 10.0, "producer_frame_ms": 60.0})], dual_iso=False)
        assert_containment_gates(self, got[0], "the disjoint stages", "inside producer_frame mean")
        assert_containment_gates(self, got[1], "the disjoint stages", "inside llrawproc mean")
        self.assert_controls_pass(got[2:])

    def test_member_r13_disjoint_producer_work_gates_inside_producer_frame_without_the_llrawproc_total(self) -> None:
        """r13 (adversary r13 A2): llrawproc_ms absent; raw_read 20 + dark_frame 30 + dual_iso 30 inside a 50 ms producer_frame."""
        got = self.run_cases([cdng_zero_profile({"producer_frame_ms": 50.0, "raw_read_ms": 20.0, "llrawproc_dark_frame_ms": 30.0, "llrawproc_dual_iso_ms": 30.0}, drop_total=True),
                              cdng_zero_profile({"producer_frame_ms": 50.0, "raw_read_ms": 10.0, "llrawproc_dark_frame_ms": 10.0, "llrawproc_dual_iso_ms": 10.0}, drop_total=True)])
        assert_containment_gates(self, got[0], "the disjoint stages", "inside producer_frame mean")
        self.assert_controls_pass(got[1:])

    def test_member_r13_2_dual_iso_and_the_lock_times_gate_inside_llrawproc(self) -> None:
        """r13-2 (adversary r13 pass 13 A1): dual_iso 30 + publish_lock 90 (or refine_lock 90) inside a 100 ms llrawproc beside a 70 ms llrawproc_other; and with llrawproc_ms absent."""
        base = {"producer_frame_ms": 120.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 30.0, "llrawproc_other_ms": 70.0}
        got = self.run_cases([cdng_zero_profile({**base, "llrawproc_publish_lock_ms": 90.0, "llrawproc_shared_lock_ms": 90.0}),
                              cdng_zero_profile({**base, "llrawproc_dualiso_refine_lock_ms": 90.0, "llrawproc_shared_lock_ms": 90.0}),
                              cdng_zero_profile({"producer_frame_ms": 100.0, "llrawproc_dual_iso_ms": 30.0, "llrawproc_publish_lock_ms": 90.0, "llrawproc_shared_lock_ms": 90.0}, drop_total=True),
                              cdng_zero_profile({**base, "llrawproc_publish_lock_ms": 10.0, "llrawproc_dualiso_refine_lock_ms": 10.0, "llrawproc_shared_lock_ms": 20.0})])
        for i in range(3):
            with self.subTest(case=i):
                assert_containment_gates(self, got[i], "the disjoint stages")
        self.assert_controls_pass(got[3:])

    def test_member_r15_every_timed_child_and_the_outside_lock_times_gate_together(self) -> None:
        """r15 (adversary pass14/15 A1): dark_frame 80 + dual_iso 1 + publish_lock 80 inside a 100 ms llrawproc (each timed child in turn), also with llrawproc_ms absent."""
        children = ("llrawproc_dark_frame_ms", "llrawproc_vertical_stripes_ms", "llrawproc_focus_pixels_ms", "llrawproc_bad_pixels_ms", "llrawproc_pattern_noise_ms", "llrawproc_chroma_smooth_ms")
        bad = []
        for child in children:
            row = {"producer_frame_ms": 100.0, child: 80.0, "llrawproc_dual_iso_ms": 1.0, "llrawproc_other_ms": 19.0, "llrawproc_publish_lock_ms": 80.0, "llrawproc_shared_lock_ms": 80.0}
            bad += [cdng_zero_profile({**row, "llrawproc_ms": 100.0}), cdng_zero_profile(row, drop_total=True)]
        got = self.run_cases(bad + [cdng_zero_profile({"producer_frame_ms": 120.0, "llrawproc_ms": 100.0, "llrawproc_dark_frame_ms": 20.0, "llrawproc_dual_iso_ms": 1.0,
                                                       "llrawproc_other_ms": 79.0, "llrawproc_publish_lock_ms": 10.0, "llrawproc_dualiso_refine_lock_ms": 10.0,
                                                       "llrawproc_shared_lock_ms": 20.0})])
        for i in range(len(bad)):
            with self.subTest(case=i):
                assert_containment_gates(self, got[i], "the disjoint stages")
        self.assert_controls_pass(got[len(bad):])

    def test_member_r16_disjoint_work_with_the_shared_lock_gates_inside_its_parent(self) -> None:
        """r16 (adversary pass16 A1): dual_iso 80 + shared_lock 80 + other 20 inside a 100 ms llrawproc (also with llrawproc_ms absent); raw_read 80 + dual_iso 1 + shared_lock 80
        inside a 100 ms producer_frame; chroma_smooth 60 + shared_lock 60 inside a 100 ms llrawproc."""
        got = self.run_cases([
            cdng_zero_profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 80.0, "llrawproc_shared_lock_ms": 80.0, "llrawproc_other_ms": 20.0}),
            cdng_zero_profile({"producer_frame_ms": 100.0, "llrawproc_dual_iso_ms": 80.0, "llrawproc_shared_lock_ms": 80.0, "llrawproc_other_ms": 20.0}, drop_total=True),
            cdng_zero_profile({"producer_frame_ms": 100.0, "raw_read_ms": 80.0, "llrawproc_dual_iso_ms": 1.0, "llrawproc_shared_lock_ms": 80.0}, drop_total=True),
            cdng_zero_profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_chroma_smooth_ms": 60.0, "llrawproc_shared_lock_ms": 60.0, "llrawproc_other_ms": 40.0}),
            # controls: shared_lock overlaps dark_frame / bad_pixels (its first lock window holds them) and retains the refine and publish times
            cdng_zero_profile({"producer_frame_ms": 110.0, "llrawproc_ms": 100.0, "llrawproc_dark_frame_ms": 40.0, "llrawproc_bad_pixels_ms": 20.0, "llrawproc_dual_iso_ms": 30.0,
                               "llrawproc_shared_lock_ms": 65.0, "llrawproc_dualiso_refine_lock_ms": 3.0, "llrawproc_publish_lock_ms": 2.0, "llrawproc_other_ms": 10.0}),
            cdng_zero_profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 50.0, "llrawproc_shared_lock_ms": 50.0, "llrawproc_other_ms": 50.0})])
        for i in range(4):
            with self.subTest(case=i):
                assert_containment_gates(self, got[i], "the disjoint stages")
        self.assert_controls_pass(got[4:])

    def test_member_r17_pre_dualiso_fix_beside_dual_iso_gates_inside_llrawproc(self) -> None:
        """r17 (fable/opus key3 B1 on b35c279b): the open repro. CDNG profile, 30 frames, producer_frame 100, llrawproc 100, pre_dualiso_fix 80, dual_iso 80, other 20 read PASS with
        problems=[]: the table's pre_dualiso_fix row carried the scaled path's whole call (llrawproc.c:6606-7076), which export never runs, so it met dual_iso's scaled windows. On the
        export path (applyLLRawProcObjectWorker(..., NULL, 0)) pre_dualiso_fix accrues nowhere (its one main-path write, :5463, is the stop_before_dual_iso block), so its 80 ms is
        work outside every window, and 80 + 80 do not fit inside a 100 ms llrawproc."""
        got = self.run_cases([cdng_zero_profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_pre_dualiso_fix_ms": 80.0, "llrawproc_dual_iso_ms": 80.0,
                                                 "llrawproc_other_ms": 20.0}),
                              cdng_zero_profile({"producer_frame_ms": 100.0, "llrawproc_pre_dualiso_fix_ms": 80.0, "llrawproc_dual_iso_ms": 80.0, "llrawproc_other_ms": 20.0},
                                                drop_total=True),
                              cdng_zero_profile({"producer_frame_ms": 100.0, "llrawproc_ms": 100.0, "llrawproc_dual_iso_ms": 80.0, "llrawproc_other_ms": 20.0})])
        for i in (0, 1):   # (the first group over its parent is named: with the producer's other members inside producer_frame, or inside llrawproc)
            with self.subTest(case=i):
                assert_containment_gates(self, got[i], "the disjoint stages", "llrawproc_pre_dualiso_fix", "llrawproc_dual_iso")
        self.assert_controls_pass(got[2:])

    def test_guard_the_window_table_equals_what_cdng_export_reaches_in_the_c_sources(self) -> None:
        """The Guard: re-derive, from dng.c and llrawproc.c alone (no module code), the llrawproc timer accrual sites CDNG export reaches by call-site reachability from dng.c's
        applyLLRawProcObject call, and assert the module's table equals it. It fails if a row carries a window export cannot reach (the r17 class), if a reachable accrual site
        is outside its row, or if a row's window holds no reachable site; and it is proven to bite on the base's pre_dualiso_fix row."""
        derived = derive_export_timer_windows(DNG_C.read_text(encoding="utf-8", errors="replace"), LLRAWPROC_C.read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(derived["entries"], ["applyLLRawProcObject"], "dng.c must reach llrawproc only through applyLLRawProcObject")
        self.assertIn("applyLLRawProcObjectWorker", derived["reachable"])
        self.assertNotIn("llrawproc_apply_with_dims_internal", derived["reachable"], "the scaled path is playback-only")
        self.assertEqual(derived["unread"], [], "an accrual write the guard cannot read")
        self.assertNotIn("llrawproc_pre_dualiso_fix", derived["windows"], "pre_dualiso_fix accrues only in the stop_before_dual_iso block export never runs")
        proc, got = run_pwsh_json(f"Import-Module '{DV / 'DualVenueRunner.psm1'}' -Force\n"
                                  "& (Get-Module DualVenueRunner) { @($script:ExportCdngStageWindowTable) } | ConvertTo-Json -Compress -Depth 6 -AsArray")
        self.assertIsNotNone(got, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(table_vs_export_problems(got, derived), [])
        # (negative control: the base's pre_dualiso_fix row, and a scaled-path window on any row, are refused; adversary r1 A1: so is a row widened to the whole worker)
        for name, extra, replace in (("llrawproc_pre_dualiso_fix", ["llrawproc.c:5014-5463", "llrawproc.c:6606-7076"], False),
                                     ("llrawproc_dual_iso", ["llrawproc.c:6890-6954"], False), ("llrawproc_dual_iso", ["llrawproc.c:5014-6329"], True)):
            with self.subTest(mutant=name, extra=extra):
                mutant = json.loads(json.dumps(got))
                for r in mutant:
                    if r["name"] == name:
                        r["windows"] = extra if replace else list(dict.fromkeys(list(r.get("windows") or []) + extra))
                self.assertTrue(any(p.startswith(f"{name}: window") for p in table_vs_export_problems(mutant, derived)), name)
        # (adversary r1 A2: a timer write the derivation cannot read is reported, whatever its C spelling: a parenthesized lvalue, a backslash-newline inside the name, another operator)
        llr = LLRAWPROC_C.read_text(encoding="utf-8", errors="replace")
        anchor = "applyLLRawProcObjectWorker(video, raw_image_buff, raw_image_size, NULL, 0);"
        self.assertEqual(llr.count(anchor), 1)
        dng = DNG_C.read_text(encoding="utf-8", errors="replace")
        # (pass 2 A1/A2: a statement split across lines, a pointer or subscript write, a macro argument, a blank after the splice backslash, the ??/ trigraph splice)
        for spelling in ("(g_llrawproc_last_pre_dualiso_fix_ms) = 80.0;", "g_llrawproc_last_pre_dual\\\niso_fix_ms = 80.0;", "g_llrawproc_last_pre_dualiso_fix_ms -= 1.0;",
                         "++g_llrawproc_last_dual_iso_ms;", "g_llrawproc_last_pre_dualiso_fix_ms\n = 80.0;", "double *p = &g_llrawproc_last_pre_dualiso_fix_ms; *p = 80.0;",
                         "(&g_llrawproc_last_pre_dualiso_fix_ms)[0] = 80.0;", "SET_TIMER(g_llrawproc_last_pre_dualiso_fix_ms, 80.0);",
                         "g_llrawproc_last_pre_dual\\ \niso_fix_ms = 80.0;", "g_llrawproc_last_pre_dual??/\niso_fix_ms = 80.0;"):
            with self.subTest(spelling=spelling):
                mutated = derive_export_timer_windows(dng, llr.replace(anchor, anchor + " " + spelling))
                self.assertTrue(mutated["unread"] or "llrawproc_pre_dualiso_fix" in mutated["windows"], spelling)
        # (pass 2 A2: the worker's local accumulators too, by pointer and by a macro that names one; and an X-macro naming the timers expanded on the export path)
        local_anchor = "double chroma_smooth_ms = 0.0;"   # (its FIRST occurrence is the worker's declaration, llrawproc.c:5063; the second is the scaled path's)
        self.assertLess(llr.index(local_anchor), llr.index("void applyLLRawProcObject(mlvObject_t"))
        for spelling in ("double *q = &dark_frame_ms; *q = 80.0;", "SET_TIMER(dual_iso_ms, 80.0);"):
            with self.subTest(local=spelling):
                self.assertTrue(derive_export_timer_windows(dng, llr.replace(local_anchor, local_anchor + " " + spelling, 1))["unread"], spelling)
        with self.subTest(xmacro="LLRP_LAST_RUN_TIMING"):
            self.assertIn("LLRP_LAST_RUN_TIMING", llr)
            self.assertTrue(derive_export_timer_windows(dng, llr.replace(anchor, anchor + " LLRP_LAST_RUN_TIMING(SOMETHING)"))["unread"])
        # (pass 2 A5: a literal binds stop_before_dual_iso only while the body never writes it: reassigned before its if, the block is live and pre_dualiso_fix accrues)
        gate = "if (stop_before_dual_iso)"
        self.assertEqual(llr.count(gate), 1)
        mutated = derive_export_timer_windows(dng, llr.replace(gate, "stop_before_dual_iso = 1; " + gate))
        self.assertTrue(mutated["unread"] or "llrawproc_pre_dualiso_fix" in mutated["windows"])
        # (pass 2 A3/A4: a window across an untimed gap, a wrong parent, an extra contains entry, a token window, a removed or duplicated row are refused)
        def table_mutant(fn) -> list:
            t = json.loads(json.dumps(got))
            fn(t)
            return table_vs_export_problems(t, derived)

        def row(t: list, name: str) -> dict:
            return next(r for r in t if r["name"] == name)
        mutants = {
            "dual_iso across the refine lock": lambda t: row(t, "llrawproc_dual_iso").__setitem__("windows", ["llrawproc.c:5560-6219"]),
            "pre_dualiso_fix parent": lambda t: row(t, "llrawproc_pre_dualiso_fix").__setitem__("parent", "unmeasured"),
            "extra contains": lambda t: row(t, "llrawproc_dual_iso").__setitem__("contains", ["llrawproc_publish_lock"]),
            "token window": lambda t: row(t, "llrawproc_dual_iso").__setitem__("windows", row(t, "llrawproc_dual_iso")["windows"] + ["dng.c#raw_read"]),
            "removed pre row": lambda t: t.remove(row(t, "llrawproc_pre_dualiso_fix")),
            "duplicated row": lambda t: t.append(json.loads(json.dumps(row(t, "llrawproc_dual_iso")))),
            "other not the remainder": lambda t: row(t, "llrawproc_other").__setitem__("remainderOf", ["llrawproc_dual_iso"]),
        }
        for label, fn in mutants.items():
            with self.subTest(table=label):
                self.assertTrue(table_mutant(fn), label)


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
                               source_commit: str | None = None, manifest: str | None = None, profile_text: str | None = None) -> None:
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
            # (EXPORT-RENDER-PIPE-TIMING-1: the committed ProRes legs gate dualIsoClassAgrees and renderPipeTimed, so the default log is a build of that card's)
            (a / "stage-timing.log").write_text(piped_stage_log(frames) if stage is None else stage, encoding="utf-8")
            (a / "output-manifest.json").write_text(output_manifest(1, "mov"), encoding="utf-8")
            return
        if profile:
            (a / "export-profile.json").write_text(cdng_profile(frames, build_id=source_commit or self.sha) if profile_text is None else profile_text, encoding="utf-8")
        (a / "output-manifest.json").write_text(output_manifest(exported) if manifest is None else manifest, encoding="utf-8")

    def validate(self, receipt: dict, spec: Path, dv: Path = DV, evidence_dir: str = "", metrics_as_hashtable: bool = False) -> dict:
        rp = self.tmp / "receipt-under-test.json"
        rp.write_text(json.dumps(receipt), encoding="utf-8")
        ev = f" -EvidenceDir '{evidence_dir}'" if evidence_dir else ""
        # (EXPORT-RENDER-PIPE-TIMING-1 r1 fix 2, adversary A2: the same metrics parsed -AsHashtable, as a caller that reads its receipt that way hands them over)
        ht = "$r.metrics = ($r.metrics | ConvertTo-Json -Depth 20 | ConvertFrom-Json -AsHashtable)\n" if metrics_as_hashtable else ""
        proc, got = run_pwsh_json(f"Import-Module '{dv / 'DualVenueRunner.psm1'}' -Force\n"
                                  f"$r = Get-Content -Raw '{rp}' | ConvertFrom-Json\n{ht}"
                                  f"$v = Test-DvReceiptValid -Receipt $r -AllowOfflineTestMode -OfflineLegSpecBytes ([IO.File]::ReadAllBytes('{spec}')){ev}\n"
                                  "[ordered]@{ valid = $v.valid; status = $v.status; reasons = @($v.reasons) } | ConvertTo-Json -Compress")
        self.assertIsNotNone(got, proc.stdout + proc.stderr)
        return got

    def test_r20_a2_the_leg_hands_the_generator_the_specs_committed_receipt_sha256(self) -> None:
        """r20 (adversary pass 24 A2 on 9b8c4753): the generator is told the receipt hash the spec commits, so the bytes it reads and embeds are checked against it."""
        spec = self.export_spec(maxframes=30)
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        want = leg(EXPORT_LEGS[0])["export"]["receiptSha256"]
        self.assertIn(f"ExpectReceiptSha256={want}", self.gen_log.read_text(encoding="utf-8"))

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

    def test_r19_b3_a_contradictory_manifest_total_is_invalid_at_write_and_in_the_receipt_rederivation(self) -> None:
        """EXPORT-VENUE-LEG-1 r19 (sol r18 B3): the writer (Invoke-VenueLeg.ps1) and the receipt re-derivation (Test-DvReceiptValid) share the reader, so both refuse a manifest
        whose totalBytes contradicts its endpoint records. Writer: count=30 totalBytes=1 beside two 1000-byte endpoints is INVALID, not PASS. Re-derivation: a PASS receipt whose
        hashed manifest is swapped for one with the SAME count and totalBytes (so the metrics still match) but endpoints that cannot fit inside that total does not verify."""
        spec = self.export_spec(maxframes=30)
        bad = json.dumps({"schema": "export-stage-output-manifest.v1", "kind": "dng", "count": 30, "totalBytes": 1,
                          "first": {"name": "000000.dng", "bytes": 1000, "sha256": "1" * 64}, "last": {"name": "000029.dng", "bytes": 1000, "sha256": "2" * 64}, "deleted": True})
        self.write_export_artifacts(30, manifest=bad)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("EXPORT_OUTPUT_MISMATCH", receipt["outcomeDetail"])
        # the re-derivation: a happy receipt, its manifest swapped (hash claim updated) for one whose endpoints (2 x 20000 bytes) cannot fit in the same 30000-byte total
        self.write_export_artifacts(30)
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(self.validate(receipt, spec)["status"], "VERIFIED_OFFLINE_TEST")
        evdir = Path(receipt["evidence"]["localEvidenceDir"])
        work = self.tmp / "ev-r19-b3"
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(evdir, work)
        doc = json.loads((work / "export" / "output-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual((doc["count"], doc["totalBytes"]), (30, 30000), doc)
        doc["first"]["bytes"] = 20000
        doc["last"]["bytes"] = 20000
        forged = json.dumps(doc).encode("utf-8")
        (work / "export" / "output-manifest.json").write_bytes(forged)
        r = json.loads(json.dumps(receipt))
        r["export"]["files"]["output-manifest.json"] = hashlib.sha256(forged).hexdigest()
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

    def test_adversary_r1_a2_a_receipt_the_base_reader_wrote_still_rederives(self) -> None:
        """adversary r1 A2 (00a7cf09): a ProRes receipt written before EXPORT-RENDER-PIPE-TIMING-1 (old-format stage log, the base leg criteria) stores the BASE reader's metrics and
        stage table; the new reader added two metric fields, changed two reasons and the stage rows, so the unchanged evidence re-derived METRICS_NOT_FROM_EVIDENCE: INVALID. The
        oracle is the base reader's own output (2b796198, tools/repo_hygiene/fixtures/export_prores_base_2b796198_evidence.json) for the exact fixture published here. A receipt of
        this card's shape over the same evidence still verifies, and the base shape is never applied to a log that carries a new stage."""
        oracle = json.loads((ROOT / "tools" / "repo_hygiene" / "fixtures" / "export_prores_base_2b796198_evidence.json").read_text(encoding="utf-8"))
        spec_doc = leg(EXPORT_LEGS[1])
        spec_doc["export"]["maxFrames"] = 30
        for role in ("acceptance", "supplementary"):
            spec_doc["criteria"][role]["cpu"] = [dict(c, value=30) if c["metric"] in ("framesExported", "stageFramesSeen") else c
                                                 for c in spec_doc["criteria"][role]["cpu"] if c not in PIPE_CRITERIA]
        spec = self.tmp / "prores-base-criteria.json"
        spec.write_text(json.dumps(spec_doc), encoding="utf-8")
        self.write_export_artifacts(30, fmt="prores", stage=stage_log(30))
        proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(self.validate(receipt, spec)["status"], "VERIFIED_OFFLINE_TEST", "this card's own receipt over old-format evidence")
        base = json.loads(json.dumps(receipt))
        base["metrics"] = oracle["metrics"]
        base["export"]["stages"] = oracle["stages"]
        got = self.validate(base, spec)
        self.assertEqual(got["status"], "VERIFIED_OFFLINE_TEST", got)
        # r1 fix 2 (adversary A2 on 33f7a05a): the same base-shaped metrics parsed -AsHashtable are the same receipt (the shape test read PSCustomObject only)
        got = self.validate(base, spec, metrics_as_hashtable=True)
        self.assertEqual(got["status"], "VERIFIED_OFFLINE_TEST", got)
        # the base shape is for old-format evidence only: the same base-shaped receipt over a log WITH the new stages is no re-derivation of it
        evdir = Path(receipt["evidence"]["localEvidenceDir"])
        work = self.tmp / "ev-a2-new-stages"
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(evdir, work)
        raw = piped_stage_log(30).encode("utf-8")
        (work / "export" / "stage-timing.log").write_bytes(raw)
        forged = json.loads(json.dumps(base))
        forged["export"]["files"]["stage-timing.log"] = hashlib.sha256(raw).hexdigest()
        got = self.validate(forged, spec, evidence_dir=str(work))
        self.assertEqual(got["status"], "INVALID", got)
        self.assertTrue(any("METRICS_NOT_FROM_EVIDENCE" in r for r in got["reasons"]), got)

    def test_i5_the_llrawproc_total_is_held_inside_producer_frame_and_its_children_at_write_and_in_the_receipt_rederivation(self) -> None:
        """Dot I5-EXPORT-CDNG-TOTAL-CONTAINMENT (#838 ask 6967301971) through the WRITER (Invoke-VenueLeg) and the receipt VALIDATOR (Test-DvReceiptValid), each mutating ONLY the
        llrawproc total (or one frame's): a typed INVALID at write time (never PASS), and a good receipt whose hashed export-profile.json is rewritten to that profile (every sha256
        claim updated to match) does not verify. A matching profile and a permitted absent total verify, written by the writer and re-derived by the validator."""
        spec = self.export_spec(maxframes=30)
        bad = [b for b in TOTAL_CONTAINMENT_BAD if b[0] in ("dot_i5_only_the_total_changes_40_to_900", "one_frame_total_past_its_producer_frame_the_mean_hides",
                                                             "total_inside_producer_frame_but_past_its_children", "one_frame_children_past_the_total_the_mean_hides",
                                                             "worker_total_is_a_string", "other_omitted_one_frame_children_lower_bound_past_the_total")]   # (r2 B1, B2: the same reader at write and re-derivation)
        self.assertEqual(len(bad), 6)
        controls = {"normal": cdng_total_profile(self.sha), "total_absent": cdng_total_profile(self.sha, drop=("llrawproc_ms",))}
        for name, text in controls.items():
            with self.subTest(control=name, via="writer"):
                self.write_export_artifacts(30, profile_text=text)
                proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
                self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
                self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
                self.assertEqual(self.validate(receipt, spec)["status"], "VERIFIED_OFFLINE_TEST")
        for name, kw, code, needle in bad:
            with self.subTest(case=name, via="writer"):
                self.write_export_artifacts(30, profile_text=cdng_total_profile(self.sha, **kw))
                proc, receipt, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
                self.assertIsNotNone(receipt, proc.stdout[-3000:] + proc.stderr[-3000:])
                self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                self.assertIn(code, receipt["outcomeDetail"])
        # the validator: a good receipt, its hashed profile rewritten, the claim updated to match
        self.write_export_artifacts(30)
        proc, good, _ = self.run_leg("bachelor", spec, token=("EXPORT_CAPTURED", 0), extra=["-Backend", "cpu"])
        self.assertIsNotNone(good, proc.stdout[-3000:] + proc.stderr[-3000:])
        self.assertEqual(good["outcome"], "PASS", good["outcomeDetail"])
        evdir = Path(good["evidence"]["localEvidenceDir"])
        for name, kw, code, needle in bad:
            with self.subTest(case=name, via="validator"):
                work = self.tmp / f"ev-i5-{name}"
                if work.exists():
                    shutil.rmtree(work)
                shutil.copytree(evdir, work)
                raw = cdng_total_profile(self.sha, **kw).encode("utf-8")
                (work / "export" / "export-profile.json").write_bytes(raw)
                r = json.loads(json.dumps(good))
                r["export"]["files"]["export-profile.json"] = hashlib.sha256(raw).hexdigest()
                got = self.validate(r, spec, evidence_dir=str(work))
                self.assertEqual(got["status"], "INVALID", got)
                self.assertTrue(any(code in x for x in got["reasons"]), got)

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

    def test_r20_a2_a_receipt_file_that_is_not_the_committed_bytes_is_refused_before_any_job_is_written(self) -> None:
        """r20 (adversary pass 24 A2 on 9b8c4753): a receipt file replaced after the leg wrote it (here: other bytes than the spec's receiptSha256) is refused typed,
        DUAL_VENUE_EXPORT_RECEIPT_CHANGED, before git or the job text (and after the preflight's return, r8 A3); the generator refuses to run without the expected hash.
        The check is the generator's own top-level block, lifted from its AST and run on the replaced file (the owner clip it resolves first is not on a CI host)."""
        committed = leg(EXPORT_LEGS[0])["export"]["receiptSha256"]
        text = EXPORT_JOB.read_text(encoding="utf-8")
        self.assertLess(text.index("if ($PreflightBudgetOnly) {\n    if ($MaxFrames"), text.index("DUAL_VENUE_EXPORT_RECEIPT_CHANGED the batch receipt"))
        self.assertLess(text.index("DUAL_VENUE_EXPORT_RECEIPT_CHANGED the batch receipt"), text.index("& git -C $RepoRoot cat-file"))
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "j.job.ps1"
            receipt = Path(d) / "replaced.marxml"
            receipt.write_bytes(b"<receipt>replacement bytes</receipt>")
            actual = hashlib.sha256(receipt.read_bytes()).hexdigest()
            self.assertNotEqual(actual, committed)
            lift = ("$ast = [System.Management.Automation.Language.Parser]::ParseFile('" + str(EXPORT_JOB) + "', [ref]$null, [ref]$null)\n"
                    "$blocks = @($ast.EndBlock.Statements | Where-Object { $_ -is [System.Management.Automation.Language.IfStatementAst] -and $_.Extent.Text.Contains('DUAL_VENUE_EXPORT_RECEIPT_CHANGED') })\n"
                    "if ($blocks.Count -ne 1) { 'BLOCKS ' + $blocks.Count; exit 0 }\n"
                    "$PreflightBudgetOnly = $false; $ReceiptPath = '" + str(receipt) + "'; $ExpectReceiptSha256 = $args[0]\n"
                    "try { . ([scriptblock]::Create($blocks[0].Extent.Text)); 'ACCEPTED ' + $receiptSha256 } catch { 'REFUSED ' + $_.Exception.Message }\n")
            lifter = Path(d) / "lift.ps1"
            lifter.write_text(lift, encoding="utf-8")
            for expect, want in ((committed, "REFUSED DUAL_VENUE_EXPORT_RECEIPT_CHANGED"), (actual, "ACCEPTED " + actual)):
                with self.subTest(expect=expect):
                    proc = run_pwsh(["-File", str(lifter), expect])
                    self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                    self.assertTrue(proc.stdout.strip().startswith(want), proc.stdout + proc.stderr)
            proc = run_pwsh(["-File", str(EXPORT_JOB), "-SourceCommit", "e" * 40, "-BuildManifestSha256", "ab" * 32, "-ClipId", "M16-1243", "-Venue", "bachelor",
                             "-Backend", "cpu", "-Format", "prores", "-MaxFrames", "480", "-ReceiptPath", str(receipt), "-PlaySeconds", "20", "-OutFile", str(out)])
            self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("DUAL_VENUE_EXPORT_ARG_MISSING -ExpectReceiptSha256", proc.stdout + proc.stderr)
            self.assertFalse(out.exists())


if __name__ == "__main__":
    unittest.main()
