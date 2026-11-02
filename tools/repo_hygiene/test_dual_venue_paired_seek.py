"""CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1: a look leg's optional look.pairedSeek.

look.pairedSeek true asks the job generator for -ContactSheetPairedSeek: after the measured Play the job seeks back to each in-pass frame and
captures it paused (capture_mode=seek, playback_path=false), published under paired-seek\\contact-sheet\\raw. The runner copies those frames to
<evidence>\\paired-seek\\contact-sheet\\raw and lists them (name + sha256) in the receipt's look.pairedSeek, so a playing (reduced recon) frame
can be compared with the same frame paused (full recon). Absent or false, the generator is not asked and look.pairedSeek is null.

The runner runs for real (offline test mode) on test_dual_venue_evidence's harness; its stub generator gains the one switch, the idiom
test_core_partition_leg.py uses. Windows-only, like the harness.
"""

from __future__ import annotations

import hashlib
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import DV, STUB_GENERATOR, RunnerHarness, requires_windows_pwsh
from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import ATTRIBUTION_GENERATOR, MODULE, _run_pwsh_file, requires_pwsh

PARAMS_ANCHOR = "$AutomationSettingsDir,$GpuPreLaunches)"
PAIRED_STUB_GENERATOR = STUB_GENERATOR.replace(PARAMS_ANCHOR, "$AutomationSettingsDir,$GpuPreLaunches,[switch]$ContactSheetPairedSeek)")


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def fixture_png(tag: bytes | str, width: int = 2, height: int = 2) -> bytes:
    """r10 forced fixture (sol r9 key B1): a well-formed PNG -- signature, IHDR, IDAT, IEND -- tagged by a tEXt chunk so each still hashes apart. The
    r6-r9 fixtures were the 8-byte signature plus a tag (no IHDR), the shape r10's image check now refuses, as it refuses a zero-byte png."""
    tag = tag.encode("utf-8") if isinstance(tag, str) else tag
    rows = b"".join(b"\x00" + bytes(3 * width) for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + png_chunk(b"tEXt", b"fixture\x00" + tag)
            + png_chunk(b"IDAT", zlib.compress(rows)) + png_chunk(b"IEND", b""))


# r7: two COMPLETE paused stills (each png with its sidecar, as the app writes a seek capture). The r1-r6 fixture -- a sidecar with no display_frame
# and a png with no sidecar -- is exactly the listless shape adversary r6 A1 showed passing; it is now a typed mismatch (PairedSeekListlessProvenanceTests).
SEEK_FRAMES = {"frame-00.json": b'{"capture_mode":"seek","playback_path":false,"display_frame":50}', "frame-00.png": fixture_png("seek-0"),
               "frame-01.json": b'{"capture_mode":"seek","playback_path":false,"display_frame":150}', "frame-01.png": fixture_png("seek-1")}


def write_in_pass_partners(artifacts: Path, count: int, frames: list[int] | None = None) -> None:
    """r8 forced fixture (adversary r8 A2): the in-pass partner of each paused still, as the app writes it (contact-sheet\\raw\\frame-<ii>.png and .json,
    capture_mode in_pass, playback_path true, display_frame 50 + 100*i). With no frame list, still i must show its partner's display_frame."""
    raw = artifacts / "artifacts" / "contact-sheet" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (raw / f"frame-{i:02d}.png").write_bytes(fixture_png("raw-%d" % i))
        (raw / f"frame-{i:02d}.json").write_bytes(json.dumps({"capture_mode": "in_pass", "playback_path": True, "display_frame": frames[i] if frames else 50 + 100 * i}).encode("utf-8"))


class PairedSeekSpecTests(unittest.TestCase):
    def test_the_schema_types_paired_seek_boolean_and_the_paired_legs_copy_their_cells(self) -> None:
        schema = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["look"]["properties"]["pairedSeek"]["type"], "boolean")
        for mode in ("fullscreen", "windowed"):
            for scale in (2, 4):
                paired = json.loads((DV / "legs" / f"m16-1243-q1-cpu-look-paired-{mode}-s{scale}.json").read_text(encoding="utf-8"))
                source = json.loads((DV / "legs" / f"m16-1243-q1-cpu-look-{mode}-s{scale}.json").read_text(encoding="utf-8"))
                self.assertNotIn("pairedSeek", source["look"], "the original cells stay unchanged")
                self.assertIs(paired["look"]["pairedSeek"], True)
                self.assertEqual(paired["legId"], f"m16-1243-q1-cpu-look-paired-{mode}-s{scale}")
                self.assertEqual(paired["card"], "CPU-DUALISO-REDUCED-RECON-PLAYBACK-ONLY-1")
                expected = dict(source, legId=paired["legId"], card=paired["card"], look=dict(source["look"], pairedSeek=True))
                self.assertEqual(paired, expected, "legId, card and look.pairedSeek are the only differences")

    def test_the_runner_maps_paired_seek_inside_the_look_block_only(self) -> None:
        runner = (DV / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        mapping = ("if ($spec.look.PSObject.Properties['pairedSeek'] -and $spec.look.pairedSeek -is [bool] -and $spec.look.pairedSeek) "
                   "{ $gen['ContactSheetPairedSeek'] = $true }")
        self.assertEqual(runner.count(mapping), 1)
        look_at = runner.index("$gen['ContactSheet'] = $true; $gen['ContactSheetFrames'] = [int]$spec.look.contactSheetFrames")
        self.assertGreater(runner.index(mapping), look_at, "the mapping sits in the look leg's block, after the contact sheet it extends")


@requires_windows_pwsh
class PairedSeekRunnerTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        self.assertEqual(STUB_GENERATOR.count(PARAMS_ANCHOR), 1, "the stub generator's param list moved")
        self.make_harness()
        self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")

    def look_spec(self, paired_seek: object | None, leg_id: str) -> Path:
        path = self.write_spec(leg_type="look", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        if paired_seek is not None:
            spec["look"]["pairedSeek"] = paired_seek
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def write_seek_frames(self) -> Path:
        seek = self.artifacts / "artifacts" / "paired-seek" / "contact-sheet" / "raw"
        seek.mkdir(parents=True, exist_ok=True)
        for name, data in SEEK_FRAMES.items():
            (seek / name).write_bytes(data)
        write_in_pass_partners(self.artifacts, 2)
        return seek

    def test_a_paired_seek_leg_asks_for_the_seek_frames_and_copies_them_hash_listed(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_frames()
        _, receipt, _ = self.run_leg("ultra-magnus", self.look_spec(True, "paired-on"), extra=["-Backend", "cpu"])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertIn("ContactSheetPairedSeek=True", self.generator_calls()[-1])
        info = receipt["look"]["pairedSeek"]
        self.assertEqual(info["count"], len(SEEK_FRAMES))
        self.assertEqual([f["name"] for f in info["files"]], sorted(SEEK_FRAMES))
        local = Path(info["path"])
        self.assertEqual(local.parts[-3:], ("paired-seek", "contact-sheet", "raw"))
        for entry in info["files"]:
            data = SEEK_FRAMES[entry["name"]]
            self.assertEqual(entry["sha256"].lower(), hashlib.sha256(data).hexdigest())
            self.assertEqual((local / entry["name"]).read_bytes(), data)
        # the in-pass frames stay where they were: the seek frames are never mixed into them
        in_pass = Path(receipt["look"]["contactSheet"]["rawFramesDir"])
        # (r8 forced fixture: the in-pass raw now holds the in-pass partner of each paused still, as the app writes it; the seek frames are still not among them)
        self.assertEqual(sorted(p.name for p in in_pass.iterdir()), ["frame-00.json", "frame-00.png", "frame-01.json", "frame-01.png"])
        self.assertFalse(any((in_pass / n).read_bytes() == d for n, d in SEEK_FRAMES.items()))

    def test_a_paired_seek_leg_whose_job_published_no_seek_frames_says_so(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        _, receipt, _ = self.run_leg("ultra-magnus", self.look_spec(True, "paired-none"), extra=["-Backend", "cpu"])
        self.assertIn("ContactSheetPairedSeek=True", self.generator_calls()[-1])
        self.assertEqual(receipt["look"]["pairedSeek"]["count"], 0)
        self.assertIsNone(receipt["look"]["pairedSeek"]["path"])

    def test_absent_or_false_asks_nothing_and_records_nothing(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_frames()
        for value, leg_id in ((None, "paired-absent"), (False, "paired-false"), ("true", "paired-string")):
            with self.subTest(value=value):
                _, receipt, _ = self.run_leg("ultra-magnus", self.look_spec(value, leg_id), extra=["-Backend", "cpu"])
                self.assertNotIn("ContactSheetPairedSeek", self.generator_calls()[-1])
                self.assertIsNone(receipt["look"]["pairedSeek"])


def hold_exclusive(test: unittest.TestCase, path: Path) -> None:
    """Open `path` with share mode 0 (FileShare.None) until the test ends: any other open of it fails with a sharing violation."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)   # GENERIC_READ, no sharing, OPEN_EXISTING
    test.assertNotEqual(handle, wintypes.HANDLE(-1).value, f"could not hold {path} (error {ctypes.get_last_error()})")
    test.addCleanup(kernel32.CloseHandle, handle)


@requires_windows_pwsh
class CollectionFailureReceiptTests(RunnerHarness, unittest.TestCase):
    """r3 (sol B1, adversary A1): a leg that ran ALWAYS ends in a typed receipt. A copy, hash or listing step of the evidence collection that
    throws (a published file held with FileShare.None, a file that vanishes or turns unreadable) ends the leg UNRESOLVED with
    `collection-failed: <step> <path> <reason>`, never a runner that exits with no receipt."""

    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")

    def paired_spec(self, leg_id: str) -> Path:
        path = self.write_spec(leg_type="look", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["look"]["pairedSeek"] = True
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def write_seek_frames(self) -> Path:
        seek = self.artifacts / "artifacts" / "paired-seek" / "contact-sheet" / "raw"
        seek.mkdir(parents=True, exist_ok=True)
        for name, data in SEEK_FRAMES.items():
            (seek / name).write_bytes(data)
        return seek

    def assert_collection_failed(self, proc, receipt, step: str, path_fragment: str) -> str:
        self.assertIsNotNone(receipt, "a collection failure must still write a receipt:\n" + proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "UNRESOLVED", receipt["outcomeDetail"])
        detail = receipt["outcomeDetail"]
        self.assertTrue(detail.startswith(f"collection-failed: {step} "), detail)
        self.assertIn(path_fragment, detail)
        self.assertIn("DVE_OUTCOME=UNRESOLVED", proc.stdout)
        return detail

    def test_a_published_seek_frame_held_with_file_share_none_ends_in_a_typed_receipt(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        seek = self.write_seek_frames()
        hold_exclusive(self, seek / "frame-00.png")
        proc, receipt, _ = self.run_leg("ultra-magnus", self.paired_spec("locked-seek"), extra=["-Backend", "cpu"])
        detail = self.assert_collection_failed(proc, receipt, "copy", "paired-seek")
        self.assertIn("frame-00.png", detail, "the reason names the file the copy could not read")

    def test_a_seek_frame_whose_hash_step_fails_ends_in_a_typed_receipt(self) -> None:
        # The copy succeeds; hashing the local copy throws (injected in a COPY of the runner's module, the runner itself unchanged).
        dv = self.mutated_runner([("DualVenueRunner.psm1", "function Get-DvSha256OfFile([string]$Path) {\n",
                                   "function Get-DvSha256OfFile([string]$Path) {\n    if ($Path -match 'paired-seek') { throw 'INJECTED_HASH_FAILURE' }\n")])
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_frames()
        proc, receipt, _ = self.run_leg("ultra-magnus", self.paired_spec("hash-seek"), extra=["-Backend", "cpu"], dv=dv)
        detail = self.assert_collection_failed(proc, receipt, "hash", "paired-seek")
        self.assertIn("INJECTED_HASH_FAILURE", detail)

    def test_every_sibling_collection_copy_ends_in_a_typed_receipt(self) -> None:
        # The class sweep: a published smoke record, the in-pass raw frames and the composed sheet are collected the same guarded way.
        # (summary.json and the manifest are rewritten by the harness just before the leg runs, so they cannot be held from here.)
        for rel, fragment in (("smoke-stderr.txt", "smoke-stderr.txt"), ("artifacts/contact-sheet/raw/frame-00.png", "raw"), ("contact-sheet/sheet.png", "sheet.png")):
            with self.subTest(file=rel):
                self.doCleanups()
                self.make_harness()
                self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")
                self.write_artifacts(sheet=True, raw_frames=True)
                target = self.artifacts / Path(rel)
                if not target.exists():
                    target.write_bytes(b"stderr of the smoke run\n")
                hold_exclusive(self, target)
                proc, receipt, _ = self.run_leg("ultra-magnus", self.paired_spec("locked-" + target.stem.replace(".", "-")), extra=["-Backend", "cpu"])
                self.assert_collection_failed(proc, receipt, "copy", fragment)


def seek_sidecar(display_frame: int) -> bytes:
    return json.dumps({"capture_mode": "seek", "playback_path": False, "display_frame": display_frame}).encode("utf-8")


@requires_windows_pwsh
class PairedSeekFrameListTests(RunnerHarness, unittest.TestCase):
    """r3 (sol B2 tooling): look.pairedSeekFrames names the display_frames every arm's paused stills must show, so an H and a C leg compare the
    SAME frames. The runner records the requested and the reported display_frame per still and FAILS CLOSED: a still that does not report its
    requested display_frame (or is missing) turns a PASS/FAIL into a typed INVALID (PAIRED_SEEK_FRAMES_NOT_HONOURED)."""

    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")

    def frame_list_spec(self, frames: object, leg_id: str, paired_seek: object = True) -> Path:
        path = self.write_spec(leg_type="look", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["look"]["pairedSeek"] = paired_seek
        spec["look"]["pairedSeekFrames"] = frames
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def write_seek_stills(self, display_frames: list[int | None]) -> None:
        seek = self.artifacts / "artifacts" / "paired-seek" / "contact-sheet" / "raw"
        seek.mkdir(parents=True, exist_ok=True)
        for i, frame in enumerate(display_frames):
            (seek / f"frame-{i:02d}.png").write_bytes(fixture_png("seek-%d" % i))
            if frame is not None:
                (seek / f"frame-{i:02d}.json").write_bytes(seek_sidecar(frame))
        # r8 fix 2 forced fixture (adversary r8 fix-1 A2): the in-pass partners of the requested frames [50, 150], which a listed-frame still must match too
        write_in_pass_partners(self.artifacts, len(display_frames))

    def test_the_schema_types_paired_seek_frames(self) -> None:
        prop = json.loads((DV / "leg-spec.schema.json").read_text(encoding="utf-8"))["properties"]["look"]["properties"]["pairedSeekFrames"]
        self.assertEqual(prop["type"], "array")
        self.assertEqual(prop["items"], {"type": "integer", "minimum": 0})
        self.assertIs(prop["uniqueItems"], True)

    def test_stills_that_report_the_requested_frames_pass_and_are_recorded(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills([50, 150])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.frame_list_spec([50, 150], "list-ok"), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        info = receipt["look"]["pairedSeek"]
        self.assertEqual(info["requestedFrames"], [50, 150])
        self.assertEqual(info["reportedFrames"], [50, 150])
        self.assertIs(info["framesHonoured"], True)

    def test_a_still_at_another_frame_fails_closed(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills([50, 131])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.frame_list_spec([50, 150], "list-off"), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("PAIRED_SEEK_FRAMES_NOT_HONOURED", receipt["outcomeDetail"])
        self.assertIn("frame-01 reported display_frame 131, requested 150", receipt["outcomeDetail"])
        self.assertEqual(receipt["look"]["pairedSeek"]["reportedFrames"], [50, 131])
        self.assertIs(receipt["look"]["pairedSeek"]["framesHonoured"], False)

    def test_missing_stills_or_sidecars_fail_closed(self) -> None:
        for stills, leg_id, needle in (([50, None], "list-nosidecar", "frame-01 reported no display_frame"),
                                       (None, "list-nostills", "frame-00 reported no display_frame")):
            with self.subTest(case=leg_id):
                self.doCleanups()
                self.make_harness()
                self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")
                self.write_artifacts(sheet=True, raw_frames=True)
                if stills is not None:
                    self.write_seek_stills(stills)
                proc, receipt, _ = self.run_leg("ultra-magnus", self.frame_list_spec([50, 150], leg_id), extra=["-Backend", "cpu"])
                self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
                self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                self.assertIn(needle, receipt["outcomeDetail"])

    def test_a_malformed_frame_list_is_refused_before_anything_runs(self) -> None:
        for frames, paired, leg_id in (([50, 150], False, "bad-notpaired"), ([50], True, "bad-count"), ([50, 50], True, "bad-dupe"),
                                       ([50, -1], True, "bad-negative"), ("50,150", True, "bad-type")):
            with self.subTest(case=leg_id):
                proc, receipt, submitted = self.run_leg("ultra-magnus", self.frame_list_spec(frames, leg_id, paired), extra=["-Backend", "cpu"])
                self.assertNotEqual(proc.returncode, 0)
                self.assertIn("DVE_LEG_SPEC_INVALID", proc.stdout + proc.stderr)
                self.assertIn("pairedSeekFrames", proc.stdout + proc.stderr)
                self.assertEqual(submitted, [], "nothing is submitted")
                self.assertEqual(self.generator_calls(), [], "nothing is generated")


    def test_a_sidecar_replaced_after_it_was_hash_listed_cannot_pass(self) -> None:
        # r5 (sol r4 B2): frame-01.json is hash-listed showing 131, then replaced on disk (between the hash step and the read) by one showing 150.
        # The runner must not report 150 under 131's hash: the bytes it interprets are the hash-listed bytes, or the still fails closed.
        replaced = ("if ($null -ne $seekLocal) { [IO.File]::WriteAllBytes((Join-Path $seekLocal 'frame-01.json'), [Text.Encoding]::UTF8.GetBytes('"
                    + seek_sidecar(150).decode("utf-8") + "')) }")
        dv = self.mutated_runner([("Invoke-VenueLeg.ps1", SEEK_INFO_ANCHOR, SEEK_INFO_ANCHOR + "        " + replaced + "\n")])
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills([50, 131])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.frame_list_spec([50, 150], "list-swap"), extra=["-Backend", "cpu"], dv=dv)
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        info = receipt["look"]["pairedSeek"]
        listed = {f["name"]: f["sha256"].lower() for f in info["files"]}
        self.assertEqual(listed["frame-01.json"], hashlib.sha256(seek_sidecar(131)).hexdigest(), "the listing hashed the sidecar showing 131")
        self.assertNotEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("PAIRED_SEEK_FRAMES_NOT_HONOURED", receipt["outcomeDetail"])
        self.assertIs(info["framesHonoured"], False)
        self.assertNotEqual(info["reportedFrames"][1], 150, "a frame read from bytes that are not the hash-listed ones is never reported")


SEEK_INFO_ANCHOR = "        $pairedSeekInfo = [ordered]@{ path = $seekLocal; count = $seekFiles.Count; files = $seekFiles }\n"
QUALITY_STUB_GENERATOR = STUB_GENERATOR.replace(PARAMS_ANCHOR, "$AutomationSettingsDir,$GpuPreLaunches,[switch]$ContactSheetPairedSeek,$QualityMode)")


@requires_windows_pwsh
class ControlContainerShapeTests(RunnerHarness, unittest.TestCase):
    """r5 (sol r4 B1): a control container the runner reads members of (look, generatorArgs, acceptedEffectiveScale, timeouts, criteria) that is
    present but not ONE JSON object is refused before anything is generated or submitted, typed (DVE_LEG_SPEC_INVALID) and naming the field.
    An array of objects read through PowerShell's member enumeration for direct members but had no PSObject property for the presence checks,
    so look=[{...pairedSeek...}] and generatorArgs=[{"qualityMode":"hq"}] silently dropped their controls."""

    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(QUALITY_STUB_GENERATOR, encoding="utf-8")

    def spec_with(self, leg_type: str, leg_id: str, **fields: object) -> Path:
        path = self.write_spec(leg_type=leg_type, leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec.update(fields)
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def assert_refused(self, spec: Path, field: str) -> None:
        proc, receipt, submitted = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"])
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn(f"DVE_LEG_SPEC_INVALID {field} must be a single JSON object", out)
        self.assertIsNone(receipt, "a refused spec writes no receipt (like every other DVE_LEG_SPEC_INVALID)")
        self.assertEqual(submitted, [], "nothing is submitted")
        self.assertEqual(self.generator_calls(), [], "nothing is generated")

    def test_sols_array_shaped_look_is_refused(self) -> None:
        look = [{"contactSheetFrames": 2, "lookFlavor": "classic", "pairedSeek": True, "pairedSeekFrames": [50, 150]}]
        self.assert_refused(self.spec_with("look", "arr-look", look=look), "look")

    def test_sols_array_shaped_generator_args_is_refused(self) -> None:
        self.assert_refused(self.spec_with("speed", "arr-gen", generatorArgs=[{"qualityMode": "hq"}]), "generatorArgs")

    def test_every_sibling_container_that_is_not_one_object_is_refused(self) -> None:
        for field, value, leg_type in (("look", None, "look"), ("look", "classic", "look"), ("generatorArgs", None, "speed"), ("generatorArgs", [], "speed"),
                                       ("acceptedEffectiveScale", [{"cuda": 1}], "speed"), ("timeouts", [{"queueWaitSec": 60, "extraClaimedWaitSec": 60}], "speed"),
                                       ("criteria", [{"acceptance": {"cpu": []}}], "speed")):
            with self.subTest(field=field, value=value):
                self.gen_log.write_text("", encoding="utf-8")
                self.assert_refused(self.spec_with(leg_type, f"shape-{field}-{type(value).__name__}", **{field: value}), field)

    def test_a_one_element_array_root_is_refused(self) -> None:
        # r6 (fable r5 key hardening, adversary r5 A4): ConvertFrom-Json enumerated a top-level array, so [{<valid spec>}] arrived as the inner object.
        path = self.write_spec(leg_type="look", leg_id="root-array")
        path.write_text(json.dumps([json.loads(path.read_text(encoding="utf-8"))]), encoding="utf-8")
        self.assert_refused(path, "the leg spec")

    def test_object_shaped_controls_still_reach_the_generator(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        look = {"contactSheetFrames": 2, "lookFlavor": "classic", "pairedSeek": True}
        proc, receipt, _ = self.run_leg("ultra-magnus", self.spec_with("look", "obj-ok", look=look, generatorArgs={"qualityMode": "hq"}), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        call = self.generator_calls()[-1]
        self.assertIn("ContactSheetPairedSeek=True", call)
        self.assertIn("QualityMode=hq", call)
        self.assertIn("ContactSheetFrames=2", call)


FINAL_COMPLETE_ANCHOR = "\nComplete-Receipt $outcome $detail\n"


@requires_windows_pwsh
class FinalValidationIoReceiptTests(RunnerHarness, unittest.TestCase):
    """r5 (sol r4 B4, Dot 4f978e0b): the receipt writer RE-READS the hashed evidence to validate a PASS/FAIL. A read that throws there (summary.json
    held with FileShare.None after collection) is an evidence-validation I/O failure: the leg ends in a typed UNRESOLVED receipt
    (`evidence-validation-io: <path> <reason>`), never exit 2 with no receipt. A receipt DESTINATION that cannot be written stays DVE_RECEIPT_WRITE_FAILED, exit 2."""

    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")

    def look_spec(self, leg_id: str) -> Path:
        return self.write_spec(leg_type="look", leg_id=leg_id)

    def test_summary_held_with_file_share_none_at_final_validation_ends_in_a_typed_receipt(self) -> None:
        hold = "\n$script:dvTestHold = [IO.File]::Open((Join-Path $evidenceDir 'summary.json'), [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)"
        dv = self.mutated_runner([("Invoke-VenueLeg.ps1", FINAL_COMPLETE_ANCHOR, hold + FINAL_COMPLETE_ANCHOR)])
        self.write_artifacts(sheet=True, raw_frames=True)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.look_spec("final-io"), extra=["-Backend", "cpu"], dv=dv)
        out = proc.stdout + proc.stderr
        self.assertNotIn("DVE_RECEIPT_WRITE_FAILED", out)
        self.assertEqual(proc.returncode, 0, out)
        self.assertIsNotNone(receipt, "an evidence read failure at final validation must still write a receipt:\n" + out)
        self.assertEqual(receipt["outcome"], "UNRESOLVED", receipt["outcomeDetail"])
        detail = receipt["outcomeDetail"]
        self.assertTrue(detail.startswith("evidence-validation-io: "), detail)
        self.assertIn("summary.json", detail)
        self.assertIn("the job result was PASS", detail)
        self.assertIn("DVE_OUTCOME=UNRESOLVED", proc.stdout)

    def test_the_unheld_control_still_passes(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.look_spec("final-ok"), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])

    def test_a_receipt_destination_that_cannot_be_written_stays_exit_2(self) -> None:
        anchor = "    $stream = [IO.File]::Open($path, [IO.FileMode]::CreateNew"
        dv = self.mutated_runner([("DualVenueRunner.psm1", anchor, "    throw 'INJECTED_DESTINATION_FAILURE'\n" + anchor)])
        self.write_artifacts(sheet=True, raw_frames=True)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.look_spec("dest-fail"), extra=["-Backend", "cpu"], dv=dv)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("DVE_RECEIPT_WRITE_FAILED INJECTED_DESTINATION_FAILURE", proc.stdout)
        self.assertIsNone(receipt)


PAIRED_PUBLISH = "-SourceDir (Join-Path $Work 'contact-sheet-seek') -PubRoot (Join-Path $Pub 'paired-seek'))"


def paired_seek_publish_region() -> str:
    """The job's CONTACT-SHEET-PLAYBACK-PARITY-1 region that publishes the paired seek frames, verbatim."""
    text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
    at = text.index(PAIRED_PUBLISH)
    start = text.rindex("# CONTACT-SHEET-PLAYBACK-PARITY-1 >>>", 0, at)
    end = text.index("# CONTACT-SHEET-PLAYBACK-PARITY-1 <<<", at)
    return text[start:end]


@requires_pwsh
class PairedSeekPublishTests(unittest.TestCase):
    """Adversary A1 (r1 pass on 4d446b3d): the job publishes the seek frames with -PubRoot $Pub\\paired-seek, a directory nothing had
    created, and New-AttrCudaDirectory refuses a missing parent (ATTRCUDA_DIR_PARENT_MISSING), so every paired-seek leg lost its paused
    frames. The job's own bracketed region now creates $Pub\\paired-seek when there are seek frames to publish. The region runs here
    verbatim, against the real module, on a fresh $Pub (the spliced module text, and so the default job's bytes, are unchanged)."""

    def run_region(self, with_frames: bool, paired_seek: bool = True) -> tuple[object, Path]:
        tmp = tempfile.TemporaryDirectory(prefix="paired-seek-publish-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        work = root / "work"
        work.mkdir()
        if with_frames:
            (work / "contact-sheet-seek").mkdir()
            for name, data in SEEK_FRAMES.items():
                (work / "contact-sheet-seek" / name).write_bytes(data)
        pub = root / "pub"
        pub.mkdir()
        script = root / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$Work = '{work}'\n$Pub = '{pub}'\n$ContactSheetEnabled = $true\n"
            f"$ContactSheetPairedSeek = ${'true' if paired_seek else 'false'}\n"
            + paired_seek_publish_region() + "\nWrite-Output 'REGION_DONE'\n",
            encoding="utf-8",
        )
        return _run_pwsh_file(script), pub

    def test_the_region_publishes_seek_frames_into_a_fresh_pub(self) -> None:
        proc, pub = self.run_region(with_frames=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("REGION_DONE", proc.stdout)
        raw = pub / "paired-seek" / "contact-sheet" / "raw"
        for name, data in SEEK_FRAMES.items():
            self.assertEqual((raw / name).read_bytes(), data)

    def test_no_seek_frames_or_paired_seek_off_creates_nothing(self) -> None:
        for with_frames, paired_seek in ((False, True), (True, False)):
            with self.subTest(with_frames=with_frames, paired_seek=paired_seek):
                proc, pub = self.run_region(with_frames=with_frames, paired_seek=paired_seek)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertFalse((pub / "paired-seek").exists())


SEEK_PNG = "(Join-Path $evidenceDir 'paired-seek\\contact-sheet\\raw\\frame-01.png')"
SEEK_JSON = "(Join-Path $evidenceDir 'paired-seek\\contact-sheet\\raw\\frame-01.json')"
PNG_MUTATIONS = (("deleted", f"Remove-Item -LiteralPath {SEEK_PNG}"),
                 ("replaced", f"[IO.File]::WriteAllBytes({SEEK_PNG}, [byte[]](1, 2, 3))"),
                 ("truncated", f"[IO.File]::WriteAllBytes({SEEK_PNG}, [byte[]]::new(0))"))


class PairedStillsHarness(RunnerHarness):
    """The paired-still fixtures the r6 tests share (a mixin: it holds no tests of its own)."""

    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")

    def paired_spec(self, leg_id: str, frames: list[int] | None = (50, 150)) -> Path:
        path = self.write_spec(leg_type="look", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["look"]["pairedSeek"] = True
        if frames is not None:
            spec["look"]["pairedSeekFrames"] = list(frames)
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def write_seek_stills(self, sidecars: list[bytes]) -> None:
        seek = self.artifacts / "artifacts" / "paired-seek" / "contact-sheet" / "raw"
        seek.mkdir(parents=True, exist_ok=True)
        for i, sidecar in enumerate(sidecars):
            (seek / f"frame-{i:02d}.png").write_bytes(fixture_png("seek-%d" % i))
            (seek / f"frame-{i:02d}.json").write_bytes(sidecar)
        write_in_pass_partners(self.artifacts, len(sidecars))

    def fresh(self) -> None:
        self.doCleanups()
        self.make_harness()
        self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")


@requires_windows_pwsh
class PairedSeekStillBytesTests(PairedStillsHarness, unittest.TestCase):
    """r6 (adversary A2, Dot 5c6bcd98, sol r5 key B1): a paused still counts only when BOTH its listed files -- the png as well as its sidecar -- are
    read back and hash to the sha256 the listing recorded. r5 took the png's presence from the cached listing, so a png deleted, replaced or truncated
    after the listing still left framesHonoured=true. And the receipt writer's final validation re-derives the paired-seek block from the files, so a
    paired file removed or replaced AFTER collection cannot leave a PASS either."""

    def run_mutated(self, leg_id: str, anchor: str, statement: str, before: bool = False, frames: list[int] | None = (50, 150)):
        new = ("\n" + statement + anchor) if before else (anchor + "        " + statement + "\n")
        dv = self.mutated_runner([("Invoke-VenueLeg.ps1", anchor, new)])
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills([seek_sidecar(50), seek_sidecar(150)])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.paired_spec(leg_id, frames), extra=["-Backend", "cpu"], dv=dv)
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        return receipt

    def test_a_png_deleted_replaced_or_truncated_after_the_listing_is_not_honoured(self) -> None:
        for case, statement in PNG_MUTATIONS:
            with self.subTest(case=case):
                self.fresh()
                receipt = self.run_mutated(f"png-{case}", SEEK_INFO_ANCHOR, statement)
                info = receipt["look"]["pairedSeek"]
                self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                self.assertIn("PAIRED_SEEK_FRAMES_NOT_HONOURED", receipt["outcomeDetail"])
                self.assertIs(info["framesHonoured"], False)
                self.assertTrue(any("frame-01.png" in m for m in info["frameMismatches"]), info["frameMismatches"])
                self.assertIn("frame-01.png", receipt["outcomeDetail"], "the mismatch names the file")

    def test_the_unchanged_control_passes(self) -> None:
        receipt = self.run_mutated("png-control", SEEK_INFO_ANCHOR, "$null = 0")
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertIs(receipt["look"]["pairedSeek"]["framesHonoured"], True)
        self.assertEqual(receipt["look"]["pairedSeek"]["frameMismatches"], [])

    def test_a_paired_file_removed_or_replaced_after_collection_is_not_a_usable_pass(self) -> None:
        # The frame verdict was taken from intact files; the mutation runs just before the receipt is written, so only final validation can see it.
        replaced_sidecar = f"[IO.File]::WriteAllBytes({SEEK_JSON}, [Text.Encoding]::UTF8.GetBytes('" + seek_sidecar(131).decode("utf-8") + "'))"
        for case, statement, frames in (("png-removed", f"Remove-Item -LiteralPath {SEEK_PNG}", (50, 150)),
                                        ("png-replaced", f"[IO.File]::WriteAllBytes({SEEK_PNG}, [byte[]](1, 2, 3))", (50, 150)),
                                        ("sidecar-replaced", replaced_sidecar, (50, 150)),
                                        ("png-removed-no-frame-list", f"Remove-Item -LiteralPath {SEEK_PNG}", None)):
            with self.subTest(case=case):
                self.fresh()
                receipt = self.run_mutated(f"final-{case}", FINAL_COMPLETE_ANCHOR, statement, before=True, frames=frames)
                self.assertNotEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
                self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                self.assertIn("the receipt writer refuses it", receipt["outcomeDetail"])
                self.assertIn("PAIRED_SEEK", receipt["outcomeDetail"])
                self.assertIn("frame-01", receipt["outcomeDetail"])

    def test_the_final_validation_control_passes(self) -> None:
        for frames in ((50, 150), None):
            with self.subTest(frames=frames):
                self.fresh()
                receipt = self.run_mutated("final-control" + ("" if frames else "-nolist"), FINAL_COMPLETE_ANCHOR, "$null = 0", before=True, frames=frames)
                self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])


def provenance_sidecar(display_frame: int, **fields: object) -> bytes:
    side = {"capture_mode": "seek", "playback_path": False, "display_frame": display_frame}
    for key, value in fields.items():
        if value is KeyError:
            side.pop(key)
        else:
            side[key] = value
    return json.dumps(side).encode("utf-8")


@requires_windows_pwsh
class PairedSeekProvenanceTests(PairedStillsHarness, unittest.TestCase):
    """r6 (adversary A3, sol r5 key B2): a paused still counts only when its sidecar says it was captured PAUSED through the full reconstruction path,
    exactly as the app writes a seek capture (capture_mode "seek", playback_path false). An in-pass (playing) frame published as a paired still, or a
    sidecar that does not say, is a typed mismatch naming the still, never a same-frame comparison."""

    def run_with_sidecars(self, leg_id: str, sidecars: list[bytes]):
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills(sidecars)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.paired_spec(leg_id), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        return receipt

    def test_a_still_not_captured_paused_through_the_full_path_is_not_honoured(self) -> None:
        for case, fields in (("in-pass", {"capture_mode": "in_pass", "playback_path": True}), ("playing-seek", {"playback_path": True}),
                             ("in-pass-mode", {"capture_mode": "in_pass"}), ("no-mode", {"capture_mode": KeyError}),
                             ("no-path", {"playback_path": KeyError}), ("string-path", {"playback_path": "false"}),
                             ("case-mode", {"capture_mode": "Seek"})):
            with self.subTest(case=case):
                self.fresh()
                receipt = self.run_with_sidecars(f"prov-{case}", [provenance_sidecar(50, **fields), provenance_sidecar(150, **fields)])
                info = receipt["look"]["pairedSeek"]
                self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                self.assertIn("PAIRED_SEEK_FRAMES_NOT_HONOURED", receipt["outcomeDetail"])
                self.assertIs(info["framesHonoured"], False)
                self.assertTrue(any(m.startswith("frame-00") and "capture_mode" in m for m in info["frameMismatches"]), info["frameMismatches"])

    def test_the_seek_paused_control_passes(self) -> None:
        receipt = self.run_with_sidecars("prov-control", [provenance_sidecar(50), provenance_sidecar(150)])
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])


SHIPPED_LISTLESS_SPEC = DV / "legs" / "m16-1243-q1-cpu-look-paired-windowed-s2.json"
# r7: the collection-time verdict replaced by one that claims every still honoured, so only the receipt writer's final validation can catch a bad still.
# r8 forced fixture: the param list gains $StillCount and $InPassFiles, which the runner now passes (a splatted key the stub does not declare throws).
LYING_VERDICT = ("        function Get-DvPairedSeekVerdict { param($SeekDir, $Files, $RequestedFrames, $StillCount, $InPassFiles) "
                 "[pscustomobject]@{ reportedFrames = @(50, 150); framesHonoured = $true; frameMismatches = @() } }\n")
LISTLESS_BAD_SIDECARS = (("a-in-pass", {"capture_mode": "in_pass"}), ("b-playing", {"playback_path": True}),
                         ("c-no-mode", {"capture_mode": KeyError}), ("d-no-path", {"playback_path": KeyError}),
                         ("e-string-path", {"playback_path": "false"}))


@requires_windows_pwsh
class PairedSeekListlessProvenanceTests(PairedStillsHarness, unittest.TestCase):
    """r7 (adversary r6 A1, Dot 6558329764, sol r6 key B1): paused provenance is validated for EVERY look.pairedSeek leg, not only one that names
    look.pairedSeekFrames. The four shipped paired legs name no frame list; r6 ran Get-DvPairedSeekVerdict only when frames were listed, both at
    collection (Invoke-VenueLeg) and at final validation (Test-DvReceiptValid), so an in-pass (playing) still, or a sidecar that does not say how it
    was captured, kept a PASS. Every collected still now needs a sidecar with capture_mode "seek" and boolean playback_path false; the frame list
    stays an additional check. The leg's look block is the shipped spec's (no pairedSeekFrames), with the harness's two frames and classic flavour."""

    def listless_spec(self, leg_id: str) -> Path:
        shipped = json.loads(SHIPPED_LISTLESS_SPEC.read_text(encoding="utf-8"))
        self.assertNotIn("pairedSeekFrames", shipped["look"], "the shipped paired leg names no frame list: the shape under test")
        path = self.write_spec(leg_type="look", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["look"] = dict(shipped["look"], contactSheetFrames=2, lookFlavor="classic")
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def run_listless(self, leg_id: str, sidecars: list[bytes] | None, lying_collection: bool = False):
        runner = {"dv": self.mutated_runner([("Invoke-VenueLeg.ps1", SEEK_INFO_ANCHOR, SEEK_INFO_ANCHOR + LYING_VERDICT)])} if lying_collection else {}
        self.write_artifacts(sheet=True, raw_frames=True)
        if sidecars is not None:
            self.write_seek_stills(sidecars)
        proc, receipt, _ = self.run_leg("ultra-magnus", self.listless_spec(leg_id), extra=["-Backend", "cpu"], **runner)
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        return receipt

    def test_a_listless_still_not_captured_paused_is_not_a_pass_at_collection(self) -> None:
        for case, fields in LISTLESS_BAD_SIDECARS:
            with self.subTest(case=case):
                self.fresh()
                receipt = self.run_listless(f"ll-{case}", [provenance_sidecar(50, **fields), provenance_sidecar(150, **fields)])
                self.assertNotIn(receipt["outcome"], ("PASS", "ADVISORY"), receipt["outcomeDetail"])
                self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                self.assertIn("PAIRED_SEEK_FRAMES_NOT_HONOURED", receipt["outcomeDetail"])
                info = receipt["look"]["pairedSeek"]
                self.assertIs(info["framesHonoured"], False)
                self.assertTrue(any("frame-00.json" in m and "capture_mode" in m for m in info["frameMismatches"]), info["frameMismatches"])

    def test_a_listless_still_not_captured_paused_is_not_a_pass_at_final_validation(self) -> None:
        # The collection-time verdict lies (honoured); the receipt writer re-derives the block from the files and refuses it.
        for case, fields in LISTLESS_BAD_SIDECARS:
            with self.subTest(case=case):
                self.fresh()
                receipt = self.run_listless(f"llf-{case}", [provenance_sidecar(50, **fields), provenance_sidecar(150, **fields)], lying_collection=True)
                self.assertNotIn(receipt["outcome"], ("PASS", "ADVISORY"), receipt["outcomeDetail"])
                self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                self.assertIn("the receipt writer refuses it", receipt["outcomeDetail"])
                self.assertIn("PAIRED_SEEK_FRAMES_NOT_HONOURED", receipt["outcomeDetail"])
                self.assertIn("frame-00.json", receipt["outcomeDetail"])

    def test_listless_malformed_or_absent_stills_are_not_a_pass(self) -> None:
        # sol r6 key: a hash-matching sidecar that is not JSON; a png whose sidecar was never published; a paired leg that collected no still at all.
        for case, sidecars, needle in (("g-malformed", [b"{", provenance_sidecar(150)], "frame-00"),
                                       ("h-no-stills", None, "no paused still")):
            for lying in (False, True):
                with self.subTest(case=case, final_validation=lying):
                    self.fresh()
                    receipt = self.run_listless(f"ll{'f' if lying else ''}-{case}", sidecars, lying_collection=lying)
                    self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
                    self.assertIn("PAIRED_SEEK_FRAMES_NOT_HONOURED", receipt["outcomeDetail"])
                    self.assertIn(needle, receipt["outcomeDetail"])
        self.fresh()
        self.write_artifacts(sheet=True, raw_frames=True)
        seek = self.artifacts / "artifacts" / "paired-seek" / "contact-sheet" / "raw"
        seek.mkdir(parents=True, exist_ok=True)
        (seek / "frame-00.png").write_bytes(fixture_png("seek-0"))
        (seek / "frame-00.json").write_bytes(provenance_sidecar(50))
        (seek / "frame-01.png").write_bytes(fixture_png("seek-1"))
        proc, receipt, _ = self.run_leg("ultra-magnus", self.listless_spec("ll-no-sidecar"), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        self.assertIn("frame-01.json", receipt["outcomeDetail"])

    def test_the_paused_listless_control_passes(self) -> None:
        for lying in (False, True):
            with self.subTest(final_validation=lying):
                self.fresh()
                receipt = self.run_listless("ll-control" + ("-f" if lying else ""), [provenance_sidecar(50), provenance_sidecar(150)], lying_collection=lying)
                self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
                info = receipt["look"]["pairedSeek"]
                self.assertIs(info["framesHonoured"], True)
                self.assertEqual(info["frameMismatches"], [])
                self.assertNotIn("requestedFrames", info, "no frame list was requested")

    def test_the_listed_frame_control_passes(self) -> None:
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills([provenance_sidecar(50), provenance_sidecar(150)])
        proc, receipt, _ = self.run_leg("ultra-magnus", self.paired_spec("ll-listed-control"), extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
        self.assertEqual(receipt["look"]["pairedSeek"]["requestedFrames"], [50, 150])


PREFLIGHT_PROBE = r"""
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $args[0] 'DualVenueRunner.psm1') -Force
$ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $args[0] 'Invoke-VenueLeaseBatch.ps1'), [ref]$null, [ref]$null)
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Get-LegSpecRefusal' }, $true)
. ([scriptblock]::Create($fn.Extent.Text))
foreach ($p in @($args | Select-Object -Skip 1)) { $r = @(Get-LegSpecRefusal $p 'cpu'); Write-Output ("ROW|" + (Split-Path -Leaf $p) + "|" + $r[0] + "|" + $r[1]) }
"""


@requires_pwsh
class BatchPreflightParityTests(unittest.TestCase):
    """r6 (adversary A1, sol r5 key hardening): the lease batch's preflight (Get-LegSpecRefusal) refuses every leg spec Invoke-VenueLeg would refuse
    before its terminal try -- a root that is not one object, a control container that is not one object, a malformed look.pairedSeekFrames -- by
    calling the SAME module predicate, so such a leg is refused before a lease opens instead of failing (LEG_FAILED, no receipt) after it."""

    def test_the_preflight_refuses_what_the_leg_refuses_and_admits_the_committed_paired_leg(self) -> None:
        base = json.loads((DV / "legs" / "m16-1243-q1-cpu-look-paired-windowed-s2.json").read_text(encoding="utf-8"))
        look = dict(base["look"], contactSheetFrames=2)
        cases = {
            "ok": (base, ""),
            "ok-frames": (dict(base, look=dict(look, pairedSeekFrames=[50, 150])), ""),
            "arr-look": (dict(base, look=[base["look"]]), "look must be a single JSON object"),
            "arr-gen": (dict(base, generatorArgs=[base["generatorArgs"]]), "generatorArgs must be a single JSON object"),
            "dupe-frames": (dict(base, look=dict(look, pairedSeekFrames=[50, 50])), "pairedSeekFrames must not repeat a frame"),
            "count-frames": (dict(base, look=dict(look, pairedSeekFrames=[50])), "pairedSeekFrames names 1 frame(s)"),
            "root-array": ([base], "is not a JSON object"),
        }
        tmp = tempfile.TemporaryDirectory(prefix="preflight-parity-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = []
        for name, (doc, _) in cases.items():
            (root / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
            paths.append(str(root / f"{name}.json"))
        script = root / "probe.ps1"
        script.write_text(PREFLIGHT_PROBE, encoding="utf-8")
        import subprocess
        from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import PWSH
        proc = subprocess.run([PWSH, "-NoProfile", "-File", str(script), str(DV)] + paths, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = {line.split("|")[1][:-5]: line.split("|", 3)[2:] for line in proc.stdout.splitlines() if line.startswith("ROW|")}
        for name, (_, needle) in cases.items():
            with self.subTest(case=name):
                code, message = rows[name]
                if needle:
                    self.assertEqual(code, "LEG_SPEC_INVALID", message)
                    self.assertIn(needle, message)
                else:
                    self.assertEqual(code, "", message)


@requires_pwsh
class MissingLookPreflightTests(unittest.TestCase):
    """r7 (adversary r6 A2): a look leg with NO look property passed the shared shape predicate, so the batch preflight admitted it, opened its lease,
    and the leg then threw "Cannot index into a null array" reading look.pairedSeekFrames before any receipt. The predicate now refuses it, typed,
    so the preflight refuses it before a lease opens and the leg refuses it before anything is generated."""

    def test_the_shipped_paired_spec_minus_its_look_is_refused_by_the_preflight(self) -> None:
        base = json.loads(SHIPPED_LISTLESS_SPEC.read_text(encoding="utf-8"))
        cases = {"no-look": ({k: v for k, v in base.items() if k != "look"}, "look"), "with-look": (base, "")}
        tmp = tempfile.TemporaryDirectory(prefix="missing-look-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        paths = []
        for name, (doc, _) in cases.items():
            (root / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
            paths.append(str(root / f"{name}.json"))
        (root / "probe.ps1").write_text(PREFLIGHT_PROBE, encoding="utf-8")
        import subprocess
        from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import PWSH
        proc = subprocess.run([PWSH, "-NoProfile", "-File", str(root / "probe.ps1"), str(DV)] + paths, capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = {line.split("|")[1][:-5]: line.split("|", 3)[2:] for line in proc.stdout.splitlines() if line.startswith("ROW|")}
        code, message = rows["no-look"]
        self.assertEqual(code, "LEG_SPEC_INVALID", message)
        self.assertIn("a look leg needs look", message)
        self.assertEqual(rows["with-look"][0], "", rows["with-look"][1])


@requires_windows_pwsh
class MissingLookLegTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")

    def test_a_look_leg_with_no_look_is_refused_typed_before_anything_runs(self) -> None:
        path = self.write_spec(leg_type="look", leg_id="no-look")
        spec = json.loads(path.read_text(encoding="utf-8"))
        del spec["look"]
        path.write_text(json.dumps(spec), encoding="utf-8")
        proc, receipt, submitted = self.run_leg("ultra-magnus", path, extra=["-Backend", "cpu"])
        out = proc.stdout + proc.stderr
        self.assertNotEqual(proc.returncode, 0, out)
        self.assertIn("DVE_LEG_SPEC_INVALID a look leg needs look", out)
        self.assertNotIn("Cannot index into a null array", out)
        self.assertIsNone(receipt, "a refused spec writes no receipt (like every other DVE_LEG_SPEC_INVALID)")
        self.assertEqual(submitted, [], "nothing is submitted")
        self.assertEqual(self.generator_calls(), [], "nothing is generated")


# r8: the collection-time block, rewritten AFTER the honest verdict: the claims a receipt could carry, so only the receipt writer's final validation can
# catch them. (The anchor is the last line of the collection block; the edit must apply exactly once, like every mutation here.)
VERDICT_DONE_ANCHOR = "        $pairedSeekInfo['frameMismatches'] = $seekVerdict.frameMismatches\n"
SIX_FRAMES = [50, 150, 250, 350, 450, 550]


def claimed_block(reported: list[int], requested: list[int] | None = None) -> str:
    frames = lambda xs: "@(" + ", ".join(str(x) for x in xs) + ")"
    text = (f"        $pairedSeekInfo['reportedFrames'] = {frames(reported)}; $pairedSeekInfo['framesHonoured'] = $true; "
            "$pairedSeekInfo['frameMismatches'] = @()\n")
    if requested is not None:
        text += f"        $pairedSeekInfo['requestedFrames'] = {frames(requested)}\n"
    return text


@requires_windows_pwsh
class PairedSeekCommittedStillSetTests(PairedStillsHarness, unittest.TestCase):
    """r8 (adversary r7 A2 / A1, Dot on #721 16:25:15Z, hub ACK): the paired stills a leg must hold are the COMMITTED spec's -- look.pairedSeekFrames when
    it lists them, otherwise look.contactSheetFrames stills frame-00 .. frame-<n-1> (the app's own numbering) -- never the stills that happened to be
    collected and never the frames a receipt claims. r7 checked only the stills it found: one valid paused still satisfied the shipped six-frame leg
    (A2), and a receipt whose requestedFrames named one frame narrowed final validation to frame-00 while five playing stills sat hash-listed beside it
    (A1). Every expected still must be present and paused, every hash-listed file must be one of them, a receipt frame list the committed spec does not
    carry is a typed mismatch, and a sidecar whose top level is not a JSON object (a one-element array) is no sidecar (adversary r7 A3)."""

    def shipped_spec(self, leg_id: str) -> Path:
        shipped = json.loads(SHIPPED_LISTLESS_SPEC.read_text(encoding="utf-8"))
        self.assertEqual((shipped["look"]["contactSheetFrames"], "pairedSeekFrames" in shipped["look"]), (6, False), "the shipped six-frame listless shape")
        path = self.write_spec(leg_type="look", leg_id=leg_id)
        spec = json.loads(path.read_text(encoding="utf-8"))
        spec["look"] = dict(shipped["look"], lookFlavor="classic")
        path.write_text(json.dumps(spec), encoding="utf-8")
        return path

    def run_case(self, spec: Path, sidecars: list[bytes], claim: str | None = None, extra_files: dict[str, bytes] | None = None,
                 partners: list[int] | None = None):
        runner ={"dv": self.mutated_runner([("Invoke-VenueLeg.ps1", VERDICT_DONE_ANCHOR, VERDICT_DONE_ANCHOR + claim)])} if claim else {}
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills(sidecars)
        if partners is not None:
            write_in_pass_partners(self.artifacts, len(partners), frames=partners)
        seek = self.artifacts / "artifacts" / "paired-seek" / "contact-sheet" / "raw"
        for name, data in (extra_files or {}).items():
            (seek / name).write_bytes(data)
        proc, receipt, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"], **runner)
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        return receipt

    def assert_not_a_pass(self, receipt, *needles: str, final: bool = False) -> None:
        self.assertNotIn(receipt["outcome"], ("PASS", "ADVISORY"), receipt["outcomeDetail"])
        self.assertEqual(receipt["outcome"], "INVALID", receipt["outcomeDetail"])
        if final:
            self.assertIn("the receipt writer refuses it", receipt["outcomeDetail"])
        for needle in needles:
            self.assertIn(needle, receipt["outcomeDetail"])

    def test_a2_one_valid_still_of_the_shipped_six_is_not_a_pass_at_collection(self) -> None:
        receipt = self.run_case(self.shipped_spec("set-one-of-six"), [provenance_sidecar(50)])
        self.assert_not_a_pass(receipt, "PAIRED_SEEK_FRAMES_NOT_HONOURED", "frame-01", "frame-05")
        info = receipt["look"]["pairedSeek"]
        self.assertIs(info["framesHonoured"], False)
        self.assertEqual(len(info["reportedFrames"]), 6, "one entry per committed still, collected or not")

    def test_a2_one_valid_still_of_the_shipped_six_is_not_a_pass_at_final_validation(self) -> None:
        # the collection block claims what r7 derived from the one still it found (honoured, [50]); the writer re-derives six expected stills
        receipt = self.run_case(self.shipped_spec("setf-one-of-six"), [provenance_sidecar(50)], claim=claimed_block([50]))
        self.assert_not_a_pass(receipt, "PAIRED_SEEK", "frame-01", final=True)

    def test_a1_a_receipt_frame_list_cannot_narrow_final_validation(self) -> None:
        # adversary r7 A1's input: frame-00 paused at 50, frames 01-05 playing (in_pass); the receipt says requestedFrames [50] and every claim agrees
        playing = [provenance_sidecar(f, capture_mode="in_pass", playback_path=True) for f in SIX_FRAMES[1:]]
        receipt = self.run_case(self.shipped_spec("setf-narrowed"), [provenance_sidecar(50)] + playing, claim=claimed_block([50], requested=[50]))
        self.assert_not_a_pass(receipt, "requestedFrames", "frame-01", final=True)
        self.assertIn("capture_mode", receipt["outcomeDetail"], "the playing stills are provenance-checked, not skipped")

    def test_a1_every_hash_listed_file_is_one_of_the_committed_stills(self) -> None:
        # an extra playing still beside a complete set: a listed-frame leg (r7 never looked past frame-01) and the shipped listless leg
        extra = {"frame-0{n}.png": fixture_png("extra"), "frame-0{n}.json": provenance_sidecar(999, capture_mode="in_pass", playback_path=True)}
        for case, spec, sidecars in (("listed", lambda: self.paired_spec("set-extra-listed"), [provenance_sidecar(50), provenance_sidecar(150)]),
                                     ("listless", lambda: self.shipped_spec("set-extra-listless"), [provenance_sidecar(f) for f in SIX_FRAMES])):
            with self.subTest(case=case):
                self.fresh()
                n = len(sidecars)
                receipt = self.run_case(spec(), sidecars, extra_files={k.format(n=n): v for k, v in extra.items()})
                self.assert_not_a_pass(receipt, "PAIRED_SEEK_FRAMES_NOT_HONOURED", f"frame-0{n}")

    def test_a3_a_sidecar_that_is_a_one_element_array_is_not_a_pass(self) -> None:
        wrapped = b"[" + provenance_sidecar(50) + b"]"
        for case, spec, sidecars in (("listed", lambda: self.paired_spec("set-array-listed"), [wrapped, provenance_sidecar(150)]),
                                     ("listless", lambda: self.shipped_spec("set-array-listless"), [wrapped] + [provenance_sidecar(f) for f in SIX_FRAMES[1:]])):
            with self.subTest(case=case):
                self.fresh()
                receipt = self.run_case(spec(), sidecars)
                self.assert_not_a_pass(receipt, "PAIRED_SEEK_FRAMES_NOT_HONOURED", "frame-00")

    def test_r8_a1_a_collected_still_the_listing_leaves_out_is_not_invisible(self) -> None:
        # adversary r8 A1: six paused stills plus a playing frame-06 on disk; the listing (and every claim) leaves frame-06 out
        drop = "        $seekFiles = @($seekFiles | Where-Object { $_.name -notlike 'frame-06*' })\n"
        extra = {"frame-06.png": fixture_png("extra"), "frame-06.json": provenance_sidecar(999, capture_mode="in_pass", playback_path=True)}
        for final in (False, True):
            with self.subTest(final_validation=final):
                self.fresh()
                mutations = [("Invoke-VenueLeg.ps1", SEEK_INFO_ANCHOR, drop + SEEK_INFO_ANCHOR)]
                if final:
                    mutations.append(("Invoke-VenueLeg.ps1", VERDICT_DONE_ANCHOR, VERDICT_DONE_ANCHOR + claimed_block(SIX_FRAMES)))
                dv = self.mutated_runner(mutations)
                self.write_artifacts(sheet=True, raw_frames=True)
                self.write_seek_stills([provenance_sidecar(f) for f in SIX_FRAMES])
                seek = self.artifacts / "artifacts" / "paired-seek" / "contact-sheet" / "raw"
                for name, data in extra.items():
                    (seek / name).write_bytes(data)
                proc, receipt, _ = self.run_leg("ultra-magnus", self.shipped_spec("set-hidden" + ("-f" if final else "")), extra=["-Backend", "cpu"], dv=dv)
                self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
                self.assertEqual([f["name"] for f in receipt["look"]["pairedSeek"]["files"] if f["name"].startswith("frame-06")], [], "the listing left it out")
                self.assert_not_a_pass(receipt, "frame-06", final=final)

    def test_r8_a2_a_listless_still_of_another_frame_than_its_in_pass_partner_is_not_a_pass(self) -> None:
        # adversary r8 A2: in-pass frames [50, 150, ...]; valid paused stills at [51, 151, ...]
        shifted = [f + 1 for f in SIX_FRAMES]
        for claim in (None, claimed_block(shifted)):
            with self.subTest(final_validation=claim is not None):
                self.fresh()
                receipt = self.run_case(self.shipped_spec("set-shifted" + ("-f" if claim else "")), [provenance_sidecar(f) for f in shifted], claim=claim)
                self.assert_not_a_pass(receipt, "PAIRED_SEEK", "frame-00", "in-pass partner", final=claim is not None)

    def test_r8_fix2_a2_a_listed_still_must_also_match_its_in_pass_partner(self) -> None:
        # adversary r8 fix-1 A2: pairedSeekFrames [50, 150] honoured by the paused stills, but the in-pass partners show 51 and 151
        for claim in (None, claimed_block([50, 150], requested=[50, 150])):
            with self.subTest(final_validation=claim is not None):
                self.fresh()
                receipt = self.run_case(self.paired_spec("set-listed-partner" + ("-f" if claim else "")), [provenance_sidecar(50), provenance_sidecar(150)], claim=claim,
                                        partners=[51, 151])
                self.assert_not_a_pass(receipt, "frame-00", "in-pass partner", final=claim is not None)

    def test_the_complete_six_still_paused_control_passes(self) -> None:
        for claim in (None, claimed_block(SIX_FRAMES)):
            with self.subTest(final_validation=claim is not None):
                self.fresh()
                receipt = self.run_case(self.shipped_spec("set-control" + ("-f" if claim else "")), [provenance_sidecar(f) for f in SIX_FRAMES], claim=claim)
                self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
                info = receipt["look"]["pairedSeek"]
                self.assertEqual(info["reportedFrames"], SIX_FRAMES)
                self.assertIs(info["framesHonoured"], True)
                self.assertNotIn("requestedFrames", info)


# r9 (adversary r8 cc8061a2 A1, Dot pr-721 21:52:55Z, hub ACK 6564543250): legType values that are not ONE string of the schema's enum. Every one of
# them, with look removed, passed the shared shape predicate on cc8061a2 (the array stringifies to 'look speed'), so the batch opened its lease and the
# leg then compared the array (truthy) and indexed the absent look before any receipt.
LEGTYPE_BAD = (("array", ["look", "speed"]), ("one-array", ["look"]), ("object", {"type": "look"}), ("number", 1), ("null", None), ("empty", ""),
               ("case", "LOOK"))


def run_preflight_probe(test: unittest.TestCase, cases: dict) -> dict:
    tmp = tempfile.TemporaryDirectory(prefix="legtype-shape-")
    test.addCleanup(tmp.cleanup)
    root = Path(tmp.name)
    paths = []
    for name, (doc, _) in cases.items():
        (root / f"{name}.json").write_text(json.dumps(doc), encoding="utf-8")
        paths.append(str(root / f"{name}.json"))
    (root / "probe.ps1").write_text(PREFLIGHT_PROBE, encoding="utf-8")
    import subprocess
    from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import PWSH
    proc = subprocess.run([PWSH, "-NoProfile", "-File", str(root / "probe.ps1"), str(DV)] + paths, capture_output=True, text=True, timeout=300)
    test.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
    return {line.split("|")[1][:-5]: line.split("|", 3)[2:] for line in proc.stdout.splitlines() if line.startswith("ROW|")}


@requires_pwsh
class LegTypeShapePreflightTests(unittest.TestCase):
    """r9 A1: legType must be ONE string of the schema's enum ("speed", "look"; case-sensitive, as the validator compares it). Anything else is a typed
    shape refusal in the shared predicate, so the lease batch's preflight refuses it before a lease opens. An ABSENT legType stays a speed leg, as the
    runner has always read it (the lease-batch fixtures rely on it); only a PRESENT value of the wrong shape is refused."""

    def test_r9_a1_a_legtype_that_is_not_one_enumerated_string_is_refused_by_the_preflight(self) -> None:
        base = json.loads(SHIPPED_LISTLESS_SPEC.read_text(encoding="utf-8"))
        no_look = {k: v for k, v in base.items() if k != "look"}
        cases = {f"bad-{name}": (dict(no_look, legType=value), "legType") for name, value in LEGTYPE_BAD}
        cases["bad-array-with-look"] = (dict(base, legType=["look", "speed"]), "legType")
        cases["ok-look"] = (base, "")
        cases["ok-absent"] = ({k: v for k, v in no_look.items() if k != "legType"}, "")
        rows = run_preflight_probe(self, cases)
        for name, (_, needle) in cases.items():
            with self.subTest(case=name):
                code, message = rows[name]
                if needle:
                    self.assertEqual(code, "LEG_SPEC_INVALID", message)
                    self.assertIn("legType must be one of", message)
                    self.assertNotIn("Cannot index", message)
                else:
                    self.assertEqual(code, "", message)


@requires_windows_pwsh
class LegTypeShapeLegTests(RunnerHarness, unittest.TestCase):
    def setUp(self) -> None:
        self.make_harness()
        self.gen.write_text(PAIRED_STUB_GENERATOR, encoding="utf-8")

    def test_r9_a1_a_legtype_array_is_refused_typed_at_leg_entry(self) -> None:
        for case, drop_look in (("array-no-look", True), ("array-with-look", False)):
            with self.subTest(case=case):
                path = self.write_spec(leg_type="look", leg_id=f"legtype-{case}")
                spec = json.loads(path.read_text(encoding="utf-8"))
                spec["legType"] = ["look", "speed"]
                if drop_look:
                    del spec["look"]
                path.write_text(json.dumps(spec), encoding="utf-8")
                self.gen_log.write_text("", encoding="utf-8")
                proc, receipt, submitted = self.run_leg("ultra-magnus", path, extra=["-Backend", "cpu"])
                out = proc.stdout + proc.stderr
                self.assertNotEqual(proc.returncode, 0, out)
                self.assertIn("DVE_LEG_SPEC_INVALID legType must be one of", out)
                self.assertNotIn("Cannot index into a null array", out)
                self.assertIsNone(receipt, "a refused spec writes no receipt (like every other DVE_LEG_SPEC_INVALID)")
                self.assertEqual(submitted, [], "nothing is submitted")
                self.assertEqual(self.generator_calls(), [], "nothing is generated")


@requires_pwsh
class ContactSheetCountShapePreflightTests(unittest.TestCase):
    """r9 fix 1 (adversary r9 a0936637 A2): a look leg's look.contactSheetFrames is a whole number from 1 to 60 (the schema's range and the generator's
    ValidateRange(1,60)); a JSON number with a fraction -- 1.4, and 6.0 too, which the paired verdict refuses as no whole number -- is refused. On a0936637
    the predicate returned null for 61 (with a 61-frame list), 0 (with []) and 1.4 (with [50]), so the batch opened a lease the generator then refused, or
    a 1.4 sheet was generated with one frame and refused only after collection."""

    def test_r9_fix1_a2_a_count_outside_1_to_60_or_not_whole_is_refused_by_the_preflight(self) -> None:
        base = json.loads(SHIPPED_LISTLESS_SPEC.read_text(encoding="utf-8"))
        look = base["look"]
        cases = {
            "c61-list": (dict(base, look=dict(look, contactSheetFrames=61, pairedSeekFrames=list(range(61)))), "contactSheetFrames"),
            "c61": (dict(base, look=dict(look, contactSheetFrames=61)), "contactSheetFrames"),
            "c0-empty": (dict(base, look=dict(look, contactSheetFrames=0, pairedSeekFrames=[])), "contactSheetFrames"),
            "c1.4": (dict(base, look=dict(look, contactSheetFrames=1.4, pairedSeekFrames=[50])), "contactSheetFrames"),
            "c6.0": (dict(base, look=dict(look, contactSheetFrames=6.0)), "contactSheetFrames"),
            "c-string": (dict(base, look=dict(look, contactSheetFrames="6")), "contactSheetFrames"),
            "c-absent": (dict(base, look={k: v for k, v in look.items() if k != "contactSheetFrames"}), "contactSheetFrames"),
            "ok-6": (base, ""),
            "ok-60": (dict(base, look=dict(look, contactSheetFrames=60)), ""),
            "ok-1-list": (dict(base, look=dict(look, contactSheetFrames=1, pairedSeekFrames=[50])), ""),
        }
        rows = run_preflight_probe(self, cases)
        for name, (_, needle) in cases.items():
            with self.subTest(case=name):
                code, message = rows[name]
                if needle:
                    self.assertEqual(code, "LEG_SPEC_INVALID", message)
                    self.assertIn("look.contactSheetFrames must be a whole number from 1 to 60", message)
                else:
                    self.assertEqual(code, "", message)


@requires_windows_pwsh
class PairedSeekRequirementParityTests(PairedStillsHarness, unittest.TestCase):
    """r9 fix 1 (adversary r9 a0936637 A1): the receipt writer requires a look.pairedSeek block exactly when the runner asks for paired stills -- a LOOK leg
    whose look.pairedSeek is Boolean true. On a0936637 it required one whenever look.pairedSeek -eq $true, which a speed leg carrying look.pairedSeek true
    and a look leg whose pairedSeek is the STRING "true" both satisfy; the runner asks neither for stills, so an otherwise good run ended INVALID
    (RECEIPT_FIELD_ABSENT). Each case must end exactly as its control (the same spec without pairedSeek)."""

    def outcome(self, spec: dict, leg_id: str):
        self.fresh()
        self.write_artifacts(sheet=True, raw_frames=True)
        path = self.tmp / f"parity-{leg_id}.json"
        path.write_text(json.dumps(dict(spec, legId=leg_id)), encoding="utf-8")
        proc, receipt, _ = self.run_leg("ultra-magnus", path, extra=["-Backend", "cpu"])
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        return receipt

    def test_r9_fix1_a1_a_spec_the_runner_asks_no_paired_stills_of_needs_no_paired_block(self) -> None:
        look_spec = json.loads(self.write_spec(leg_type="look", leg_id="parity-base").read_text(encoding="utf-8"))
        speed_spec = json.loads(self.write_spec(leg_type="speed", leg_id="parity-base-speed").read_text(encoding="utf-8"))
        speed_spec.update(visualCapture=False, visualCaptureOptOutReason="paired-block parity fixture")
        for case, spec, control in (
                ("look-string-true", dict(look_spec, look=dict(look_spec["look"], pairedSeek="true")), look_spec),
                ("speed-bool-true", dict(speed_spec, look={"contactSheetFrames": 2, "lookFlavor": "classic", "pairedSeek": True}),
                 dict(speed_spec, look={"contactSheetFrames": 2, "lookFlavor": "classic"}))):
            with self.subTest(case=case):
                expected = self.outcome(control, f"{case}-control")
                receipt = self.outcome(spec, case)
                self.assertNotIn("RECEIPT_FIELD_ABSENT", receipt["outcomeDetail"])
                self.assertEqual(receipt["outcome"], expected["outcome"], receipt["outcomeDetail"])
                self.assertIsNone(receipt["look"]["pairedSeek"] if isinstance(receipt.get("look"), dict) else None)


IN_PASS_RAW = ("artifacts", "contact-sheet", "raw")


@requires_windows_pwsh
class PairedSeekInPassPartnerTests(PairedStillsHarness, unittest.TestCase):
    """r9 (adversary r8 cc8061a2 A2 / A3, Dot pr-721 21:52:55Z, hub ACK 6564543250): a paired still is the paused rendition of a frame the measured pass
    PRESENTED, so its in-pass partner must be one: contact-sheet\\raw\\frame-<ii>.json saying capture_mode "in_pass" and playback_path Boolean true, as
    the app writes an in-pass grab (MainWindow.cpp in-pass sidecar writer), AND contact-sheet\\raw\\frame-<ii>.png hash-listed beside it. cc8061a2 read
    only the partner's display_frame: six paused stills against six paused (seek/false) partners stayed honoured (A2), and six partner sidecars with only
    frame-00.png stayed honoured (A3). Both are typed mismatches now, at collection and at the receipt writer's final validation. The complete control is
    PairedSeekCommittedStillSetTests.test_the_complete_six_still_paused_control_passes (unchanged)."""

    def run_partner_case(self, leg_id: str, partner_fields: dict | None = None, partner_of: tuple[int, ...] = tuple(range(6)),
                         drop_pngs: tuple[int, ...] = (), final: bool = False):
        runner = {"dv": self.mutated_runner([("Invoke-VenueLeg.ps1", VERDICT_DONE_ANCHOR, VERDICT_DONE_ANCHOR + claimed_block(SIX_FRAMES))])} if final else {}
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills([provenance_sidecar(f) for f in SIX_FRAMES])
        raw = self.artifacts.joinpath(*IN_PASS_RAW)
        for i in partner_of if partner_fields else ():
            side = {"capture_mode": "in_pass", "playback_path": True, "display_frame": SIX_FRAMES[i]}
            for key, value in partner_fields.items():
                if value is KeyError:
                    side.pop(key)
                else:
                    side[key] = value
            (raw / f"frame-{i:02d}.json").write_bytes(json.dumps(side).encode("utf-8"))
        for i in drop_pngs:
            (raw / f"frame-{i:02d}.png").unlink()
        spec = PairedSeekCommittedStillSetTests.shipped_spec(self, leg_id + ("-f" if final else ""))
        proc, receipt, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"], **runner)
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        return receipt

    def assert_invalid(self, receipt, *needles: str, final: bool = False) -> None:
        PairedSeekCommittedStillSetTests.assert_not_a_pass(self, receipt, "PAIRED_SEEK_FRAMES_NOT_HONOURED", *needles, final=final)
        if not final:
            self.assertIs(receipt["look"]["pairedSeek"]["framesHonoured"], False)

    def test_r9_a2_a_partner_that_was_not_an_in_pass_playing_grab_is_not_a_pass(self) -> None:
        for case, fields in (("seek-false", {"capture_mode": "seek", "playback_path": False}), ("no-mode", {"capture_mode": KeyError}),
                             ("no-path", {"playback_path": KeyError}), ("string-true", {"playback_path": "true"}), ("int-one", {"playback_path": 1}),
                             ("case-mode", {"capture_mode": "In_Pass"})):
            for final in ((False, True) if case in ("seek-false", "string-true") else (False,)):
                with self.subTest(case=case, final_validation=final):
                    self.fresh()
                    receipt = self.run_partner_case(f"partner-{case}", partner_fields=fields, final=final)
                    self.assert_invalid(receipt, "frame-00", "in-pass partner", final=final)

    def test_r9_a3_every_partner_needs_its_own_hash_listed_png(self) -> None:
        for final in (False, True):
            with self.subTest(final_validation=final):
                self.fresh()
                receipt = self.run_partner_case("partner-pngs", drop_pngs=(1, 2, 3, 4, 5), final=final)
                self.assert_invalid(receipt, "frame-01", "frame-05", "contact-sheet\\raw\\frame-01.png", final=final)

    def test_r9_fix2_a1_a_negative_display_frame_identifies_no_presented_frame(self) -> None:
        # adversary r9 7240a133 A1: frame-00's paused still AND its in-pass partner both say display_frame -1; every hash and provenance is valid
        stills = [-1] + SIX_FRAMES[1:]
        for final in (False, True):
            with self.subTest(final_validation=final):
                self.fresh()
                runner = {"dv": self.mutated_runner([("Invoke-VenueLeg.ps1", VERDICT_DONE_ANCHOR, VERDICT_DONE_ANCHOR + claimed_block(stills))])} if final else {}
                self.write_artifacts(sheet=True, raw_frames=True)
                self.write_seek_stills([provenance_sidecar(f) for f in stills])
                (self.artifacts.joinpath(*IN_PASS_RAW) / "frame-00.json").write_bytes(
                    json.dumps({"capture_mode": "in_pass", "playback_path": True, "display_frame": -1}).encode("utf-8"))
                spec = PairedSeekCommittedStillSetTests.shipped_spec(self, "partner-negative" + ("-f" if final else ""))
                proc, receipt, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"], **runner)
                self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
                self.assert_invalid(receipt, "frame-00", "non-negative", final=final)

    def test_r9_combined_a_missing_partner_png_and_a_seek_partner_in_one_receipt_is_not_a_pass(self) -> None:
        for final in (False, True):
            with self.subTest(final_validation=final):
                self.fresh()
                receipt = self.run_partner_case("partner-combined", partner_fields={"capture_mode": "seek", "playback_path": False}, partner_of=(2,),
                                                drop_pngs=(1,), final=final)
                self.assert_invalid(receipt, "frame-01", "frame-02", final=final)
                if not final:
                    blamed = sorted({m[:8] for m in receipt["look"]["pairedSeek"]["frameMismatches"]})
                    self.assertEqual(blamed, ["frame-01", "frame-02"], "each defect is named, the intact stills are not blamed")


# r10 (sol r9 key B1, r9 adversary successor ADV-A1, fable r9 hardening): files that hash-match their listing but are no PNG image. On ac26ee87 every one
# of them, as a paused still OR as its in-pass partner, was honoured (framesHonoured=true, PASS) at collection and at final validation alike.
_GOOD = fixture_png("good", width=4, height=3)
NOT_A_PNG = (("zero-byte", b""),                                                     # sha256 e3b0c442...b855, sol r9's repro
             ("four-byte-non-png", b"GIF8"),
             ("truncated-no-iend", _GOOD[:-12]),                                     # cut before IEND
             ("truncated-mid-chunk", _GOOD[:len(_GOOD) - 20]),                       # cut inside IDAT
             ("wrong-signature", b"\x89PNX" + _GOOD[4:]),
             ("zero-width-ihdr", fixture_png("w0", width=0, height=3)),
             ("zero-height-ihdr", fixture_png("h0", width=4, height=0)),
             ("renamed-jpeg", b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01" + bytes(96)),
             ("no-idat", _GOOD[:8] + _GOOD[8:33] + png_chunk(b"IEND", b"")))
PNG_PROBE = r"""
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $args[0] 'DualVenueRunner.psm1') -Force
foreach ($p in @($args | Select-Object -Skip 1)) { $r = Get-DvPngImageProblem ([IO.File]::ReadAllBytes($p)); Write-Output ("ROW|" + (Split-Path -Leaf $p) + "|" + $(if ($null -eq $r) { 'OK' } else { $r })) }
"""


@requires_pwsh
class PngImageCheckUnitTests(unittest.TestCase):
    """r10: Get-DvPngImageProblem, the one image check both members of every pair go through. Each NOT_A_PNG shape is refused with a reason; well-formed
    PNGs (the fixtures, a 1x1 grey, a 16-bit RGBA, an interlaced one) are accepted. (RED on ac26ee87: the function did not exist.)"""

    def test_r10_b1_a_file_that_is_not_a_png_is_refused_and_a_png_is_accepted(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="png-shape-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        grey = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(b"\x00\x80")) + png_chunk(b"IEND", b"")
        rgba16 = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 16, 6, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(bytes(9))) + png_chunk(b"IEND", b"")
        laced = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 1)) + png_chunk(b"IDAT", zlib.compress(bytes(4))) + png_chunk(b"IEND", b"")
        bad_depth = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 4, 2, 0, 0, 0)) + png_chunk(b"IDAT", zlib.compress(bytes(4))) + png_chunk(b"IEND", b"")
        cases = {**{f"bad-{n}.png": (b, False) for n, b in NOT_A_PNG}, "bad-depth.png": (bad_depth, False),
                 "ok-fixture.png": (_GOOD, True), "ok-grey.png": (grey, True), "ok-rgba16.png": (rgba16, True), "ok-interlaced.png": (laced, True)}
        for name, (data, _) in cases.items():
            (root / name).write_bytes(data)
        (root / "probe.ps1").write_text(PNG_PROBE, encoding="utf-8")
        import subprocess
        from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import PWSH
        proc = subprocess.run([PWSH, "-NoProfile", "-File", str(root / "probe.ps1"), str(DV)] + [str(root / n) for n in cases], capture_output=True, text=True, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = {line.split("|")[1]: line.split("|", 2)[2] for line in proc.stdout.splitlines() if line.startswith("ROW|")}
        for name, (_, ok) in cases.items():
            with self.subTest(case=name):
                self.assertIn(name, rows, proc.stdout + proc.stderr)
                if ok:
                    self.assertEqual(rows[name], "OK")
                else:
                    self.assertNotEqual(rows[name], "OK", f"{name} is no PNG image")


SEEK_RAW = ("artifacts", "paired-seek", "contact-sheet", "raw")


@requires_windows_pwsh
class PairedSeekImageContentTests(PairedStillsHarness, unittest.TestCase):
    """r10 (sol r9 key B1, r9 adversary successor ADV-A1 DualVenueRunner.psm1:2048): a paired comparison is two IMAGES of one frame. A paused still, or
    its in-pass partner, whose file hash-matches its listing but is no decodable PNG (zero bytes, four bytes, truncated, wrong signature, a zero-size IHDR,
    a JPEG renamed .png) is a typed mismatch naming the frame, framesHonoured=false and INVALID -- on a listed and a listless leg, at collection and at the
    receipt writer's final validation. The complete pair of valid images is the control."""

    def run_image_case(self, leg_id: str, member: str | None, data: bytes = b"", listed: bool = False, final: bool = False):
        frames = [50, 150] if listed else SIX_FRAMES
        if final:
            leg_id += "-f"
        runner = {"dv": self.mutated_runner([("Invoke-VenueLeg.ps1", VERDICT_DONE_ANCHOR,
                                              VERDICT_DONE_ANCHOR + claimed_block(frames, requested=frames if listed else None))])} if final else {}
        self.write_artifacts(sheet=True, raw_frames=True)
        self.write_seek_stills([provenance_sidecar(f) for f in frames])
        if member is not None:
            folder = SEEK_RAW if member == "paused" else IN_PASS_RAW
            self.artifacts.joinpath(*folder, "frame-00.png").write_bytes(data)
        spec = self.paired_spec(leg_id, frames) if listed else PairedSeekCommittedStillSetTests.shipped_spec(self, leg_id)
        proc, receipt, _ = self.run_leg("ultra-magnus", spec, extra=["-Backend", "cpu"], **runner)
        self.assertIsNotNone(receipt, proc.stdout + proc.stderr)
        return receipt

    def assert_image_mismatch(self, receipt, member: str, final: bool) -> None:
        where = "frame-00.png is not a decodable PNG" if member == "paused" else "in-pass partner image contact-sheet\\raw\\frame-00.png is not a decodable PNG"
        PairedSeekCommittedStillSetTests.assert_not_a_pass(self, receipt, "PAIRED_SEEK_FRAMES_NOT_HONOURED", where, final=final)
        if not final:
            info = receipt["look"]["pairedSeek"]
            self.assertIs(info["framesHonoured"], False)
            self.assertEqual(sorted({m[:8] for m in info["frameMismatches"]}), ["frame-00"], "only the frame with the bad image is blamed")

    def test_r10_b1_a_zero_byte_or_four_byte_image_in_either_member_is_not_a_pass(self) -> None:
        for shape, data in NOT_A_PNG[:2]:
            for member in ("paused", "partner"):
                for listed in (True, False):
                    for final in (False, True):
                        with self.subTest(shape=shape, member=member, listed=listed, final_validation=final):
                            self.fresh()
                            receipt = self.run_image_case(f"img-{shape[:4]}-{member[:4]}-{'l' if listed else 'n'}", member, data, listed=listed, final=final)
                            self.assert_image_mismatch(receipt, member, final)

    def test_r10_b1_every_other_non_png_shape_in_either_member_is_not_a_pass(self) -> None:
        for shape, data in NOT_A_PNG[2:]:
            for member in ("paused", "partner"):
                with self.subTest(shape=shape, member=member):
                    self.fresh()
                    receipt = self.run_image_case(f"img-{shape}-{member[:4]}", member, data)
                    self.assert_image_mismatch(receipt, member, final=False)

    def test_r10_the_valid_image_pair_control_passes(self) -> None:
        for listed in (True, False):
            for final in (False, True):
                with self.subTest(listed=listed, final_validation=final):
                    self.fresh()
                    receipt = self.run_image_case(f"img-control-{'l' if listed else 'n'}", None, listed=listed, final=final)
                    self.assertEqual(receipt["outcome"], "PASS", receipt["outcomeDetail"])
                    self.assertIs(receipt["look"]["pairedSeek"]["framesHonoured"], True)


if __name__ == "__main__":
    unittest.main()
