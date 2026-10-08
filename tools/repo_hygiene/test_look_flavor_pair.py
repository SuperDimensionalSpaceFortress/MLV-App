"""LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1: the Classic | Cinematic look-flavor diff (tools/profiling/look-flavor-diff.py), its pair driver
(tools/profiling/dual-venue/New-VenueFlavorPair.ps1) and the cooldown gate's pure decision (tools/profiling/dual-venue/Wait-VenueQuiet.ps1).

Synthetic only: PNGs and sidecars are generated in a temp directory; no owner footage and no venue is touched. The refusals that come before
any frame is read (out dir, sliders, flavor) run on every host; the image tests need Pillow + numpy and skip without them, as the side-by-side
sheet tests in test_dual_venue_evidence.py do.
"""
import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "profiling" / "look-flavor-diff.py"
DV = ROOT / "tools" / "profiling" / "dual-venue"
PWSH = shutil.which("pwsh")
requires_windows_pwsh = unittest.skipIf(PWSH is None or sys.platform != "win32", "needs pwsh on Windows")
HAS_IMAGING = importlib.util.find_spec("PIL") is not None and importlib.util.find_spec("numpy") is not None
requires_imaging = unittest.skipUnless(HAS_IMAGING, "Pillow + numpy are required")

W, H = 64, 40
CLASSIC = {"scene": "shade", "presetExposure": 380, "presetContrast": 15, "presetPivot": 55, "presetShadows": 12, "presetHighlights": -28,
           "presetVibrance": 5, "presetTemperatureDelta": 485, "presetTintDelta": -12, "finalTemperature": 6485, "finalTint": -12}
CINEMATIC = dict(CLASSIC, presetContrast=22, presetPivot=50, presetShadows=4, presetHighlights=-40, presetVibrance=-6)


def tile(seed, letterbox=0):
    import numpy as np
    arr = np.random.default_rng(seed).integers(20, 236, size=(H, W, 3), dtype=np.uint8)
    if letterbox:
        arr[:letterbox] = 0
        arr[-letterbox:] = 0
    return arr


def independent_luma(arr):
    import numpy as np
    f = arr.astype(np.float64)
    return 0.299 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 2]


def independent_stats(arr):
    import numpy as np
    f = arr.astype(np.float64)
    r, g, b = f[:, :, 0], f[:, :, 1], f[:, :, 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    mx, mn = np.maximum(np.maximum(r, g), b), np.minimum(np.minimum(r, g), b)
    sat = np.where(mx > 0, (mx - mn) / np.where(mx > 0, mx, 1), 0.0)
    return {"luma_p50": float(np.median(luma)), "mean_saturation": float(sat.mean()), "mean_r": float(r.mean()), "mean_g": float(g.mean()),
            "mean_b": float(b.mean())}


class FlavorDiffHarness(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="look-flavor-pair-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.runs = 0

    def make_side(self, base: Path, name: str, tiles: dict, display_frames: dict | None):
        """tiles: {index: uint8 array, or None for placeholder bytes (a refusal that never reads a frame)}."""
        d = base / name
        d.mkdir(parents=True)
        for i, arr in tiles.items():
            if arr is None:
                (d / f"frame-{i:02d}.png").write_bytes(b"not-read")
            else:
                from PIL import Image
                Image.fromarray(arr).save(d / f"frame-{i:02d}.png")
            (d / f"frame-{i:02d}.json").write_text(json.dumps({
                "index": i, "saved": True, "display_frame": (display_frames or {}).get(i, 30 + 40 * i), "elapsed_ms": 2000.0 + 4000 * i,
                "path": f"frame-{i:02d}.png", "playback_path": True}), encoding="utf-8")
        listing = base / f"{name}.listed.json"
        listing.write_text(json.dumps({"files": [{"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(d.iterdir())]}),
                           encoding="utf-8")
        return d, listing

    def run_tool(self, classic_tiles: dict, cinematic_tiles: dict, *, classic_sliders=CLASSIC, cinematic_sliders=CINEMATIC,
                 classic_reported="classic", cinematic_reported="cinematic", out_dir: Path | None = None,
                 classic_frames: dict | None = None, cinematic_frames: dict | None = None, after_staging=None):
        self.runs += 1
        run = self.tmp / f"run{self.runs}"
        stage = run / ".claude-state" / "stage"
        cla_dir, cla_list = self.make_side(stage, "classic", classic_tiles, classic_frames)
        cin_dir, cin_list = self.make_side(stage, "cinematic", cinematic_tiles, cinematic_frames)
        if after_staging:
            after_staging(cla_dir, cin_dir)
        sliders = {}
        for side, doc in (("classic", classic_sliders), ("cinematic", cinematic_sliders)):
            sliders[side] = stage / f"sliders-{side}.json"
            sliders[side].write_text(json.dumps(doc), encoding="utf-8")
        out = out_dir if out_dir is not None else run / ".claude-state" / "pair"
        proc = subprocess.run([sys.executable, str(TOOL),
                               "--classic-frames", str(cla_dir), "--classic-listed", str(cla_list), "--classic-sliders", str(sliders["classic"]),
                               "--classic-flavor-reported", classic_reported, "--classic-receipt-id", "r-classic",
                               "--cinematic-frames", str(cin_dir), "--cinematic-listed", str(cin_list), "--cinematic-sliders", str(sliders["cinematic"]),
                               "--cinematic-flavor-reported", cinematic_reported, "--cinematic-receipt-id", "r-cinematic",
                               "--clip-id", "M16-1243", "--venue", "bachelor", "--build-sha", "0123456789ab", "--out-dir", str(out)],
                              capture_output=True, text=True, timeout=300)
        return proc, out

    @staticmethod
    def pngs(out: Path):
        return sorted(out.glob("*.png")) if out.exists() else []


# 1, 2, 5 and the slider refusal: decided before any frame is read, on every host ---------------------------------------------------------
class FlavorDiffRefusalTests(FlavorDiffHarness):
    def test_identical_flavor_owned_sliders_are_refused_as_inert_and_write_no_sheet(self) -> None:
        """The five flavor-owned fields are identical; the fields the flavor does NOT own (exposure, white balance) differ -- still inert."""
        same_tone = dict(CLASSIC, presetExposure=410, finalTemperature=6250, finalTint=22)
        proc, out = self.run_tool({0: None}, {0: None}, cinematic_sliders=same_tone)
        self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
        self.assertTrue(proc.stderr.startswith("FLAVOR_INERT"), proc.stderr)
        self.assertFalse((out / "sheet-classic-vs-cinematic.png").exists())
        self.assertEqual(self.pngs(out), [])

    def test_a_side_reporting_the_wrong_flavor_is_refused_as_inert(self) -> None:
        for classic_reported, cinematic_reported in (("classic", "none"), ("classic", "classic"), ("cinematic", "cinematic")):
            with self.subTest(classic=classic_reported, cinematic=cinematic_reported):
                proc, out = self.run_tool({0: None}, {0: None}, classic_reported=classic_reported, cinematic_reported=cinematic_reported)
                self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
                self.assertIn("FLAVOR_INERT", proc.stderr)
                self.assertEqual(self.pngs(out), [])

    def test_an_out_dir_outside_claude_state_is_refused(self) -> None:
        proc, out = self.run_tool({0: None}, {0: None}, out_dir=self.tmp / "public" / "pair")
        self.assertEqual(proc.returncode, 13, proc.stdout + proc.stderr)
        self.assertTrue(proc.stderr.startswith("PAIR_OWNER_SHEET_MUST_STAY_LOCAL"), proc.stderr)
        self.assertFalse(out.exists())

    def test_a_slider_file_without_a_flavor_owned_field_is_refused(self) -> None:
        partial = {k: v for k, v in CINEMATIC.items() if k != "presetVibrance"}
        proc, _ = self.run_tool({0: None}, {0: None}, cinematic_sliders=partial)
        self.assertEqual(proc.returncode, 14, proc.stdout + proc.stderr)
        self.assertIn("PAIR_INPUT_INVALID", proc.stderr)


# 3, 4 and the frame refusals: need Pillow + numpy ------------------------------------------------------------------------------------------
@requires_imaging
class FlavorDiffComposeTests(FlavorDiffHarness):
    def test_a_live_pair_writes_the_sheet_rows_heatmaps_metrics_and_table(self) -> None:
        import numpy as np
        from PIL import Image
        a0, a1 = tile(10), tile(11)
        b0 = a0.copy()
        b0[:, W // 2:] = tile(12)[:, W // 2:]          # left half equal, right half different
        b1 = tile(13)
        proc, out = self.run_tool({0: a0, 1: a1}, {0: b0, 1: b1})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("LOOK_FLAVOR_DIFF_OK", proc.stdout)

        tile_h = round(1280 * H / W)
        with Image.open(out / "sheet-classic-vs-cinematic.png") as sheet:
            self.assertEqual(sheet.size, (3840, 220 + 2 * (44 + tile_h)))
        for i in (0, 1):
            with Image.open(out / f"row-{i:02d}.png") as row:
                self.assertEqual(row.size, (3840, 44 + round(1920 * H / W)))
            self.assertTrue((out / f"heat-{i:02d}.png").exists())

        metrics = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        self.assertIs(metrics["flavorLive"], True)
        for t, (a, b) in zip(metrics["tiles"], ((a0, b0), (a1, b1))):
            for side, arr in (("classic", a), ("cinematic", b)):
                for k, v in independent_stats(arr).items():
                    self.assertAlmostEqual(t[side][k], v, delta=1e-6, msg=f"{side} {k}")
            d = np.abs(b.astype(np.float64) - a.astype(np.float64))
            for c, ch in zip("rgb", range(3)):
                self.assertAlmostEqual(t["mean_abs_delta"][c], float(d[:, :, ch].mean()), delta=1e-6)
        # PIN-FLAVOR-DIFF-P95-1: p95 |dY| against a value computed here from the test's own luma, so a percentile replaced by 0.0 (or by the
        # mean) cannot pass. No tile in this test has letterbox rows, so every row is used.
        expected_p95 = [float(np.percentile(np.abs(independent_luma(b) - independent_luma(a)), 95)) for a, b in ((a0, b0), (a1, b1))]
        for t, want in zip(metrics["tiles"], expected_p95):
            self.assertGreater(want, 1.0, "the synthetic pair must give a non-trivial p95")
            self.assertAlmostEqual(t["p95_abs_delta_y"], want, delta=1e-6)
        self.assertAlmostEqual(metrics["means"]["p95_abs_delta_y"], sum(expected_p95) / 2, delta=1e-6)

        with Image.open(out / "heat-00.png") as heat:
            h = np.asarray(heat.convert("RGB"))
        self.assertEqual((h.shape[1], h.shape[0]), (W, H + 64), "full-resolution heatmap plus the legend bar")
        self.assertFalse(h[:H, : W // 2].any(), "the heatmap is exactly black where the two tiles are equal")
        self.assertTrue(h[:H, W // 2:].any(), "and not black where they differ")

        table = (out / "table.md").read_text(encoding="utf-8")
        self.assertIn("| presetContrast * | 15 | 22 | 7 |", table)
        self.assertIn("| presetExposure | 380 | 380 | 0 |", table)

    def test_letterbox_rows_dark_on_both_sides_are_excluded_from_the_metrics(self) -> None:
        a, b = tile(20, letterbox=5), tile(21, letterbox=5)
        proc, out = self.run_tool({0: a}, {0: b})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        t = json.loads((out / "metrics.json").read_text(encoding="utf-8"))["tiles"][0]
        self.assertEqual((t["rows_excluded_letterbox"], t["rows_used"]), (10, H - 10))
        self.assertAlmostEqual(t["classic"]["luma_p50"], independent_stats(a[5:-5])["luma_p50"], delta=1e-6)

    def test_a_display_frame_offset_beyond_12_is_labelled_not_frame_matched(self) -> None:
        proc, out = self.run_tool({0: tile(1), 1: tile(2)}, {0: tile(3), 1: tile(4)}, classic_frames={0: 30, 1: 70}, cinematic_frames={0: 30, 1: 83})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual((m["frameMatched"], m["sameFrames"], m["maxFrameDelta"]), (False, False, 13))
        self.assertEqual([t["display_frame_delta"] for t in m["tiles"]], [0, 13])
        # the aggregate FRAME-MATCHED is false, so EVERY row says so -- including tile 0, whose own offset (d=0) is within tolerance
        self.assertIn("NOT FRAME-MATCHED (d=0)", m["tiles"][0]["label"])
        self.assertIn("NOT FRAME-MATCHED (d=13)", m["tiles"][1]["label"])
        self.assertIn("NOT FRAME-MATCHED (d=13)", (out / "table.md").read_text(encoding="utf-8"))

    def test_zero_offsets_on_every_tile_are_same_frames(self) -> None:
        proc, out = self.run_tool({0: tile(1), 1: tile(2)}, {0: tile(3), 1: tile(4)}, classic_frames={0: 30, 1: 70}, cinematic_frames={0: 30, 1: 70})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual((m["frameMatched"], m["sameFrames"], m["maxFrameDelta"]), (True, True, 0))
        self.assertTrue(all("NOT FRAME-MATCHED" not in t["label"] for t in m["tiles"]), "a frame-matched pair labels no row")

    @staticmethod
    def tree_hashes(root: Path) -> dict:
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_a_second_composition_into_an_occupied_out_dir_is_refused_and_leaves_the_first_byte_identical(self) -> None:
        shared = self.tmp / "shared" / ".claude-state" / "pair"
        proc, out = self.run_tool({0: tile(1), 1: tile(2)}, {0: tile(3), 1: tile(4)}, out_dir=shared)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        before = self.tree_hashes(out)
        self.assertIn("sheet-classic-vs-cinematic.png", before)
        self.assertIn("metrics.json", before)
        proc2, _ = self.run_tool({0: tile(31), 1: tile(32)}, {0: tile(33), 1: tile(34)}, out_dir=shared)
        self.assertEqual(proc2.returncode, 16, proc2.stdout + proc2.stderr)
        self.assertTrue(proc2.stderr.startswith("PAIR_OUTPUT_EXISTS"), proc2.stderr)
        self.assertEqual(self.tree_hashes(out), before, "the first run's sheet, rows, heatmaps, metrics and table are byte-identical")

    def test_any_one_existing_artifact_refuses_before_anything_is_written(self) -> None:
        for name in ("table.md", "metrics.json", "sheet-classic-vs-cinematic.png", "row-00.png", "heat-01.png"):
            with self.subTest(existing=name):
                shared = self.tmp / name / ".claude-state" / "pair"
                shared.mkdir(parents=True)
                (shared / name).write_bytes(b"earlier evidence")
                proc, out = self.run_tool({0: tile(1), 1: tile(2)}, {0: tile(3), 1: tile(4)}, out_dir=shared)
                self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
                self.assertIn(name, proc.stderr)
                self.assertEqual(self.tree_hashes(out), {name: hashlib.sha256(b"earlier evidence").hexdigest()})

    def test_an_unlisted_file_in_a_staging_dir_is_refused(self) -> None:
        from PIL import Image
        proc, out = self.run_tool({0: tile(1)}, {0: tile(2)}, after_staging=lambda cla, _cin: Image.fromarray(tile(99)).save(cla / "frame-07.png"))
        self.assertEqual(proc.returncode, 12, proc.stdout + proc.stderr)
        self.assertIn("PAIR_FRAME_NOT_LISTED", proc.stderr)
        self.assertEqual(self.pngs(out), [])

    def test_a_frame_edited_after_it_was_listed_is_refused(self) -> None:
        from PIL import Image
        proc, _ = self.run_tool({0: tile(1)}, {0: tile(2)}, after_staging=lambda _cla, cin: Image.fromarray(tile(98)).save(cin / "frame-00.png"))
        self.assertEqual(proc.returncode, 12, proc.stdout + proc.stderr)
        self.assertIn("PAIR_FRAME_HASH_MISMATCH", proc.stderr)

    def test_unequal_tile_counts_are_refused(self) -> None:
        proc, out = self.run_tool({0: tile(1), 1: tile(2)}, {0: tile(3)})
        self.assertEqual(proc.returncode, 11, proc.stdout + proc.stderr)
        self.assertIn("PAIR_TILE_COUNT_DIFFERS", proc.stderr)
        self.assertEqual(self.pngs(out), [])


# the pair driver and the cooldown gate (pwsh) ----------------------------------------------------------------------------------------------
def synthetic_receipt(flavor, manifest="a" * 64):
    return {"receiptId": f"r-{flavor}", "card": "DUAL-VENUE-EVIDENCE-1", "legId": f"leg-{flavor}", "outcome": "PASS",
            "subject": {"buildManifestSha256": manifest, "clipId": "M16-1243", "backend": "cpu", "lookFlavor": flavor},
            "venue": {"name": "bachelor"}, "metrics": {"sourceCommit": "c" * 40}, "scale": {"requestedScale": 2, "effectiveScale": 2},
            "evidence": {}, "look": {}}


@requires_windows_pwsh
class FlavorPairDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="flavor-pair-driver-")
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def pair(self, classic, cinematic, out: Path | None = None):
        paths = []
        for name, doc in (("classic", classic), ("cinematic", cinematic)):
            p = self.tmp / f"{name}.receipt.json"
            p.write_text(json.dumps(doc), encoding="utf-8")
            paths.append(p)
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(DV / "New-VenueFlavorPair.ps1"),
                               "-ClassicReceipt", str(paths[0]), "-CinematicReceipt", str(paths[1]),
                               "-OutDir", str(out or self.tmp / ".claude-state" / "pair")], capture_output=True, text=True, timeout=300)

    def test_two_receipts_of_different_builds_are_refused(self) -> None:
        proc = self.pair(synthetic_receipt("classic"), synthetic_receipt("cinematic", manifest="b" * 64))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_SUBJECT_DIFFERS the receipts differ in subject.buildManifestSha256", proc.stdout + proc.stderr)

    def test_two_receipts_of_different_source_commits_are_refused(self) -> None:
        cinematic = synthetic_receipt("cinematic")
        cinematic["metrics"]["sourceCommit"] = "d" * 40
        proc = self.pair(synthetic_receipt("classic"), cinematic)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_SUBJECT_DIFFERS the receipts differ in metrics.sourceCommit", proc.stdout + proc.stderr)

    def test_the_flavors_in_the_wrong_order_are_refused(self) -> None:
        proc = self.pair(synthetic_receipt("cinematic"), synthetic_receipt("classic"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_FLAVORS_WRONG", proc.stdout + proc.stderr)

    def test_an_out_dir_outside_claude_state_is_refused(self) -> None:
        proc = self.pair(synthetic_receipt("classic"), synthetic_receipt("cinematic"), out=self.tmp / "public")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_OWNER_SHEET_MUST_STAY_LOCAL", proc.stdout + proc.stderr)


    @staticmethod
    def tree_hashes(root: Path) -> dict:
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}

    def test_an_out_dir_that_holds_a_pair_record_is_refused_before_any_artifact_changes(self) -> None:
        # A DIFFERENT valid-looking pair (other leg ids) aimed at the occupied directory: refused with its own exit code, nothing touched.
        out = self.tmp / ".claude-state" / "occupied"
        out.mkdir(parents=True)
        for name, data in (("flavor-pair-leg-old-vs-leg-old2-bachelor.json", b'{"record":1}\n'), ("sheet-classic-vs-cinematic.png", b"sheet"),
                           ("metrics.json", b"{}")):
            (out / name).write_bytes(data)
        before = self.tree_hashes(out)
        classic, cinematic = synthetic_receipt("classic"), synthetic_receipt("cinematic")
        classic["legId"], cinematic["legId"] = "leg-new-classic", "leg-new-cinematic"
        proc = self.pair(classic, cinematic, out=out)
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertIn("PAIR_RECORD_EXISTS", proc.stdout + proc.stderr)
        self.assertEqual(self.tree_hashes(out), before)

    def test_an_out_dir_that_holds_a_sheet_metrics_or_table_is_refused(self) -> None:
        for name in ("sheet-classic-vs-cinematic.png", "metrics.json", "table.md"):
            with self.subTest(existing=name):
                out = self.tmp / name / ".claude-state" / "pair"
                out.mkdir(parents=True)
                (out / name).write_bytes(b"earlier evidence")
                proc = self.pair(synthetic_receipt("classic"), synthetic_receipt("cinematic"), out=out)
                self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
                self.assertIn(f"PAIR_OUTPUT_EXISTS {name}", proc.stdout + proc.stderr)
                self.assertEqual(sorted(p.name for p in out.iterdir()), [name])


@requires_windows_pwsh
class VenueQuietDecisionTests(unittest.TestCase):
    def test_the_decision_is_the_mean_of_three_samples_and_a_failed_read_is_never_quiet(self) -> None:
        for samples, decision in (("[10, 20, 30]", "DECISION QUIET mean=20.0%"),
                                  ("[30.5, 18.0, 22.0]", "DECISION BUSY mean=23.5%"),
                                  ("[10, null, 5]", "DECISION UNKNOWN mean=UNKNOWN"),
                                  ("[10, 5]", "DECISION UNKNOWN mean=UNKNOWN"),
                                  ("[10, 5, 140]", "DECISION UNKNOWN mean=UNKNOWN"),
                                  # the comparison is on the UNROUNDED mean: three 20.04% samples are above a threshold of 20 and print as 20.0%
                                  ("[20.04, 20.04, 20.04]", "DECISION BUSY mean=20.0%"),
                                  ("[20, 20, 20.01]", "DECISION BUSY mean=20.0%"),
                                  ("[19.96, 19.96, 19.96]", "DECISION QUIET mean=20.0%")):
            with self.subTest(samples=samples):
                proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(DV / "Wait-VenueQuiet.ps1"),
                                       "-SamplesJson", samples], capture_output=True, text=True, timeout=120)
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertEqual(proc.stdout.strip(), decision)

    def test_an_unreachable_agent_share_is_a_typed_gate_unreadable_never_an_empty_queue(self) -> None:
        work = Path(tempfile.mkdtemp(prefix="venue-quiet-gate-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        gate_log = work / "gate.log"
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(DV / "Wait-VenueQuiet.ps1"), "-WorkDir", str(work / "w"),
                               "-AgentShare", str(work / "no-such-share"), "-GateLog", str(gate_log)], capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 4, out)
        self.assertIn("GATE_UNREADABLE quiet-probe bachelor", out)
        self.assertIn("DECISION UNKNOWN mean=UNKNOWN", out)
        self.assertNotIn("queued=0 running=0", out, "an unreadable share must never print as an empty queue")
        self.assertNotIn("PROBE ", out, "no probe job is submitted through an unreadable share")
        log = gate_log.read_text(encoding="utf-8")
        self.assertIn("GATE_UNREADABLE", log)
        self.assertNotIn("queued=0", log)

    def test_a_queue_that_never_clears_is_gate_busy_with_its_gate_lines_on_stdout(self) -> None:
        # Wait-QueueGate used to be consumed as a boolean while its GATE lines went down the same pipeline, so a busy queue read as truthy.
        work = Path(tempfile.mkdtemp(prefix="venue-quiet-busy-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        (work / "share" / "inbox").mkdir(parents=True)
        (work / "share" / "running").mkdir()
        (work / "share" / "inbox" / "earlier.job.ps1").write_text("# queued", encoding="utf-8")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(DV / "Wait-VenueQuiet.ps1"), "-WorkDir", str(work / "w"),
                               "-AgentShare", str(work / "share"), "-MaxGateSec", "0"], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("queued=1 running=0", proc.stdout)
        self.assertIn("GATE_BUSY quiet-probe", proc.stdout)


if __name__ == "__main__":
    unittest.main()
