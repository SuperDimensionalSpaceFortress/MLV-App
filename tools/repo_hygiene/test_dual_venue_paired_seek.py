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
import tempfile
import unittest
from pathlib import Path

from tools.repo_hygiene.test_dual_venue_evidence import DV, STUB_GENERATOR, RunnerHarness, requires_windows_pwsh
from tools.repo_hygiene.test_playback_attr_3_cuda_behaviour import ATTRIBUTION_GENERATOR, MODULE, _run_pwsh_file, requires_pwsh

PARAMS_ANCHOR = "$AutomationSettingsDir,$GpuPreLaunches)"
PAIRED_STUB_GENERATOR = STUB_GENERATOR.replace(PARAMS_ANCHOR, "$AutomationSettingsDir,$GpuPreLaunches,[switch]$ContactSheetPairedSeek)")
SEEK_FRAMES = {"frame-00.json": b'{"capture_mode":"seek","playback_path":false}', "frame-00.png": b"\x89PNG\r\n\x1a\nseek-0",
               "frame-01.png": b"\x89PNG\r\n\x1a\nseek-1"}


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
        self.assertEqual(sorted(p.name for p in in_pass.iterdir()), ["frame-00.png"])

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


@requires_pwsh
class PairedSeekPublishTests(unittest.TestCase):
    """Adversary A1 (r1 pass on 4d446b3d): the job publishes the seek frames with -PubRoot $Pub\\paired-seek, a directory nothing had
    created, and New-AttrCudaDirectory refuses a missing parent (ATTRCUDA_DIR_PARENT_MISSING), so every paired-seek leg lost its paused
    frames. The publisher now creates its own -PubRoot (whose parent, $Pub, always exists) before contact-sheet."""

    def test_the_job_publishes_seek_frames_under_a_fresh_paired_seek_root(self) -> None:
        text = ATTRIBUTION_GENERATOR.read_text(encoding="utf-8")
        self.assertIn("-SourceDir (Join-Path $Work 'contact-sheet-seek') -PubRoot (Join-Path $Pub 'paired-seek'))", text)

    def test_publishing_into_a_paired_seek_root_that_does_not_exist_yet_succeeds(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="paired-seek-publish-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        source = root / "work" / "contact-sheet-seek"
        source.mkdir(parents=True)
        for name, data in SEEK_FRAMES.items():
            (source / name).write_bytes(data)
        pub = root / "pub"
        pub.mkdir()
        paired_root = pub / "paired-seek"
        self.assertFalse(paired_root.exists())
        script = root / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"$result = Publish-AttrCudaContactSheetRawCaptures -Enabled $true -SourceDir '{source}' -PubRoot '{paired_root}'\n"
            "Write-Output \"RESULT=$result\"\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        raw = paired_root / "contact-sheet" / "raw"
        self.assertIn(f"RESULT={raw}", proc.stdout)
        for name, data in SEEK_FRAMES.items():
            self.assertEqual((raw / name).read_bytes(), data)

    def test_a_disabled_or_empty_paired_seek_publish_creates_nothing(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="paired-seek-publish-noop-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        pub = root / "pub"
        pub.mkdir()
        script = root / "probe.ps1"
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Import-Module '{MODULE}' -Force\n"
            f"[void](Publish-AttrCudaContactSheetRawCaptures -Enabled $false -SourceDir '{root}' -PubRoot '{pub / 'paired-seek'}')\n"
            f"[void](Publish-AttrCudaContactSheetRawCaptures -Enabled $true -SourceDir '{root / 'missing'}' -PubRoot '{pub / 'paired-seek'}')\n",
            encoding="utf-8",
        )
        proc = _run_pwsh_file(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((pub / "paired-seek").exists())


if __name__ == "__main__":
    unittest.main()
