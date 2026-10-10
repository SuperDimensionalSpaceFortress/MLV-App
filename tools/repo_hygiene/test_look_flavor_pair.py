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
OMIT = object()  # a display_frames value: the sidecar carries no display_frame key at all


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
            sidecar = {"index": i, "saved": True, "display_frame": (display_frames or {}).get(i, 30 + 40 * i), "elapsed_ms": 2000.0 + 4000 * i,
                       "path": f"frame-{i:02d}.png", "playback_path": True}
            if sidecar["display_frame"] is OMIT:
                del sidecar["display_frame"]
            (d / f"frame-{i:02d}.json").write_text(json.dumps(sidecar), encoding="utf-8")
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


# LOOK-ASSIST-FILM-FLAVOR-1: the three-side mode (Classic | Cinematic | Film) ------------------------------------------------------------
FILM = dict(CINEMATIC, presetGrade="film-v3")
BASE_COMMIT = "1581f29e57d5a636fc84656400f778f63ec9a406"


def independent_split_and_green(arr):
    import numpy as np
    f = arr.reshape(-1, 3).astype(np.float64)
    y = 0.299 * f[:, 0] + 0.587 * f[:, 1] + 0.114 * f[:, 2]
    p05, p30, p70, p95 = np.percentile(y, [5, 30, 70, 95])
    ba, ga = f[:, 2] - f[:, 0], f[:, 1] - (f[:, 0] + f[:, 2]) / 2.0
    s = ba[(y >= p05) & (y <= p30)].mean() - ba[(y >= p70) & (y <= p95)].mean()
    return float(s), float(ga[(y >= p30) & (y <= p70)].mean())


def independent_mad(x, y):
    import numpy as np
    d = np.abs(x.astype(np.float64) - y.astype(np.float64))
    return float((d[:, :, 0].mean() + d[:, :, 1].mean() + d[:, :, 2].mean()) / 3.0)


def graded(arr):
    """A blue-amber split laid over a tile: blue up in the dark half of the pixels, red up in the bright half."""
    import numpy as np
    f = arr.astype(np.int32)
    dark = independent_luma(arr) < 128
    f[..., 2] = np.where(dark, f[..., 2] + 9, f[..., 2] - 9)
    f[..., 0] = np.where(dark, f[..., 0] - 9, f[..., 0] + 9)
    return np.clip(f, 0, 255).astype(np.uint8)


class FlavorTrioHarness(FlavorDiffHarness):
    def run_trio(self, tiles: dict, *, film_sliders=FILM, film_reported="film", out_dir: Path | None = None, tool: Path = TOOL,
                 frames: dict | None = None):
        """tiles: {"classic": {i: arr}, "cinematic": {...}, "film": {...}}; frames: {side: {i: display_frame or OMIT}}."""
        self.runs += 1
        run = self.tmp / f"trio{self.runs}"
        stage = run / ".claude-state" / "stage"
        args = [sys.executable, str(tool)]
        for side, doc, reported in (("classic", CLASSIC, "classic"), ("cinematic", CINEMATIC, "cinematic"), ("film", film_sliders, film_reported)):
            d, listed = self.make_side(stage, side, tiles[side], (frames or {}).get(side))
            sliders = stage / f"sliders-{side}.json"
            sliders.write_text(json.dumps(doc), encoding="utf-8")
            args += [f"--{side}-frames", str(d), f"--{side}-listed", str(listed), f"--{side}-sliders", str(sliders),
                     f"--{side}-flavor-reported", reported, f"--{side}-receipt-id", f"r-{side}"]
        out = out_dir if out_dir is not None else run / ".claude-state" / "trio"
        args += ["--clip-id", "M16-1243", "--venue", "bachelor", "--build-sha", "0123456789ab", "--out-dir", str(out)]
        return subprocess.run(args, capture_output=True, text=True, timeout=300), out


class FlavorTrioRefusalTests(FlavorTrioHarness):
    def test_a_film_side_without_its_grade_or_its_name_is_refused_as_inert(self) -> None:
        cases = ((dict(FILM, presetGrade="none"), "film"), (dict(FILM, presetGrade="skipped_user_curve"), "film"),
                 ({k: v for k, v in FILM.items() if k != "presetGrade"}, "film"), (FILM, "cinematic"), (FILM, "none"))
        for sliders, reported in cases:
            with self.subTest(grade=sliders.get("presetGrade"), reported=reported):
                proc, out = self.run_trio({s: {0: None} for s in ("classic", "cinematic", "film")}, film_sliders=sliders, film_reported=reported)
                self.assertEqual(proc.returncode, 10, proc.stdout + proc.stderr)
                self.assertTrue(proc.stderr.startswith("FLAVOR_INERT"), proc.stderr)
                self.assertEqual(self.pngs(out), [])

    def test_a_partial_set_of_film_arguments_is_a_usage_error(self) -> None:
        proc = subprocess.run([sys.executable, str(TOOL), "--classic-frames", "a", "--classic-listed", "a", "--classic-sliders", "a",
                               "--classic-flavor-reported", "classic", "--cinematic-frames", "a", "--cinematic-listed", "a",
                               "--cinematic-sliders", "a", "--cinematic-flavor-reported", "cinematic", "--film-frames", "a",
                               "--out-dir", str(self.tmp / ".claude-state" / "x")], capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


@requires_imaging
class FlavorTrioComposeTests(FlavorTrioHarness):
    def test_a_live_trio_writes_the_sheet_rows_heatmaps_and_the_metrics_numpy_recomputes(self) -> None:
        import numpy as np
        from PIL import Image
        cla = {0: tile(40), 1: tile(41)}
        cin = {0: tile(42), 1: tile(43)}
        fil = {i: graded(a) for i, a in cin.items()}
        proc, out = self.run_trio({"classic": cla, "cinematic": cin, "film": fil})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("LOOK_FLAVOR_DIFF_OK", proc.stdout)
        tile_h = round(1280 * H / W)
        with Image.open(out / "sheet-classic-cinematic-film.png") as sheet:
            self.assertEqual(sheet.size, (3840, 220 + 2 * (44 + tile_h)))
        for i in (0, 1):
            with Image.open(out / f"row-{i:02d}.png") as row:
                self.assertEqual(row.size, (3840, 44 + tile_h))
            for kind in ("heat-film-vs-classic", "heat-film-vs-cinematic"):
                with Image.open(out / f"{kind}-{i:02d}.png") as heat:
                    self.assertEqual(heat.size, (W, H + 64))
        self.assertFalse((out / "sheet-classic-vs-cinematic.png").exists())

        m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(m["schema"], "mlv-app/look-flavor-diff-metrics/v2")
        self.assertIs(m["flavorLive"], True)
        self.assertIs(m["filmLive"], True)
        self.assertEqual(m["presetGrade"], {"classic": "none", "cinematic": "none", "film": "film-v3"})
        want = {"MAD": {"cinematic_classic": [], "film_classic": [], "film_cinematic": []}, "dS": {"cinematic": [], "film": []},
                "dGA": {"cinematic": [], "film": []}}
        for t, i in zip(m["tiles"], (0, 1)):
            s_cla, ga_cla = independent_split_and_green(cla[i])
            s_cin, ga_cin = independent_split_and_green(cin[i])
            s_fil, ga_fil = independent_split_and_green(fil[i])
            for side, s, ga in (("classic", s_cla, ga_cla), ("cinematic", s_cin, ga_cin), ("film", s_fil, ga_fil)):
                self.assertAlmostEqual(t[side]["S"], s, delta=1e-6, msg=f"S {side}")
                self.assertAlmostEqual(t[side]["GA"], ga, delta=1e-6, msg=f"GA {side}")
            expected = {"cinematic_classic": independent_mad(cin[i], cla[i]), "film_classic": independent_mad(fil[i], cla[i]),
                        "film_cinematic": independent_mad(fil[i], cin[i])}
            for k, v in expected.items():
                self.assertAlmostEqual(t["MAD"][k], v, delta=1e-6, msg=f"MAD {k}")
                want["MAD"][k].append(v)
            self.assertAlmostEqual(t["dS"]["film"], s_fil - s_cla, delta=1e-6)
            self.assertAlmostEqual(t["dS"]["cinematic"], s_cin - s_cla, delta=1e-6)
            self.assertAlmostEqual(t["dGA"]["film"], ga_fil - ga_cla, delta=1e-6)
            want["dS"]["film"].append(s_fil - s_cla); want["dS"]["cinematic"].append(s_cin - s_cla)
            want["dGA"]["film"].append(ga_fil - ga_cla); want["dGA"]["cinematic"].append(ga_cin - ga_cla)
        for group, keys in want.items():
            for k, vals in keys.items():
                self.assertAlmostEqual(m["means"][group][k], float(np.mean(vals)), delta=1e-6, msg=f"mean {group} {k}")
        self.assertAlmostEqual(m["means"]["dSGapFilmMinusCinematic"], float(np.mean(want["dS"]["film"]) - np.mean(want["dS"]["cinematic"])), delta=1e-6)
        # The synthetic grade really is a split (blue up in the shadows, red up in the highlights), so S rises.
        self.assertGreater(m["means"]["dSGapFilmMinusCinematic"], 4.0)
        self.assertIn("| presetGrade | none | none | film-v3 |", (out / "table.md").read_text(encoding="utf-8"))

    def test_a_trio_into_an_occupied_out_dir_is_refused(self) -> None:
        for name in ("sheet-classic-cinematic-film.png", "heat-film-vs-cinematic-01.png", "metrics.json"):
            with self.subTest(existing=name):
                shared = self.tmp / name / ".claude-state" / "trio"
                shared.mkdir(parents=True)
                (shared / name).write_bytes(b"earlier evidence")
                tiles = {s: {0: tile(1), 1: tile(2)} for s in ("classic", "cinematic")}
                tiles["film"] = {0: graded(tile(1)), 1: graded(tile(2))}
                proc, out = self.run_trio(tiles, out_dir=shared)
                self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
                self.assertEqual(sorted(p.name for p in out.iterdir()), [name])

    def test_a_sidecar_without_display_frame_is_unknown_frame_matching_as_on_the_pair_path(self) -> None:
        # FILM-TRIO-MISSING-FRAME-TYPED-REFUSAL-1: the pair path reports a missing display_frame as unknown frame matching
        # (maxFrameDelta null, NOT FRAME-MATCHED); the trio must do the same, not raise TypeError from abs(None).
        tiles = {s: {0: tile(60), 1: tile(61)} for s in ("classic", "cinematic")}
        tiles["film"] = {0: graded(tile(60)), 1: graded(tile(61))}
        for side in ("cinematic", "film", "classic"):
            with self.subTest(missing_on=side):
                proc, out = self.run_trio(tiles, frames={side: {1: OMIT}})
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertIn("LOOK_FLAVOR_DIFF_OK", proc.stdout)
                self.assertIn("frameMatched=false maxFrameDelta=None", proc.stdout)
                m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
                self.assertEqual((m["frameMatched"], m["maxFrameDelta"]), (False, None))
                self.assertIsNone(m["tiles"][1][side]["display_frame"])
                self.assertTrue(all("NOT FRAME-MATCHED" in t["label"] for t in m["tiles"]))
                self.assertTrue((out / "sheet-classic-cinematic-film.png").exists())
                self.assertIn("max |d frame|=None", (out / "table.md").read_text(encoding="utf-8"))
        # the pair path, for the same missing sidecar key
        proc, out = self.run_tool({0: tile(60), 1: tile(61)}, {0: tile(62), 1: tile(63)}, cinematic_frames={1: OMIT})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))
        self.assertEqual((m["frameMatched"], m["maxFrameDelta"]), (False, None))

    def test_the_two_side_outputs_are_byte_identical_to_the_base_commits_tool(self) -> None:
        """The same fixed synthetic pair through this tool and through the tool as it was at the base commit (with ITS make-contact-sheet.py)."""
        base_dir = self.tmp / "base-tool"
        base_dir.mkdir()
        for rel in ("tools/profiling/look-flavor-diff.py", "tools/profiling/make-contact-sheet.py"):
            show = subprocess.run(["git", "-C", str(ROOT), "show", f"{BASE_COMMIT}:{rel}"], capture_output=True, timeout=60)
            if show.returncode != 0:
                self.skipTest(f"base commit {BASE_COMMIT[:12]} is not in this clone (shallow checkout)")
            (base_dir / Path(rel).name).write_bytes(show.stdout)
        outputs = []
        for tool in (TOOL, base_dir / "look-flavor-diff.py"):
            self.runs += 1
            run = self.tmp / f"twoside{self.runs}"
            stage = run / ".claude-state" / "stage"
            cla_dir, cla_list = self.make_side(stage, "classic", {0: tile(50), 1: tile(51, letterbox=4)}, {0: 30, 1: 75})
            cin_dir, cin_list = self.make_side(stage, "cinematic", {0: tile(52), 1: tile(53, letterbox=4)}, {0: 31, 1: 70})
            sliders = {}
            for side, doc in (("classic", CLASSIC), ("cinematic", CINEMATIC)):
                sliders[side] = stage / f"sliders-{side}.json"
                sliders[side].write_text(json.dumps(doc), encoding="utf-8")
            out = run / ".claude-state" / "pair"
            proc = subprocess.run([sys.executable, str(tool),
                                   "--classic-frames", str(cla_dir), "--classic-listed", str(cla_list), "--classic-sliders", str(sliders["classic"]),
                                   "--classic-flavor-reported", "classic", "--classic-receipt-id", "r-classic",
                                   "--cinematic-frames", str(cin_dir), "--cinematic-listed", str(cin_list), "--cinematic-sliders", str(sliders["cinematic"]),
                                   "--cinematic-flavor-reported", "cinematic", "--cinematic-receipt-id", "r-cinematic",
                                   "--clip-id", "M16-1243", "--venue", "bachelor", "--build-sha", "0123456789ab", "--out-dir", str(out)],
                                  capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            outputs.append((proc.stdout.replace(str(out), "<OUT>"), FlavorDiffComposeTests.tree_hashes(out)))
        self.assertEqual(outputs[0][1], outputs[1][1], "every two-side output file is byte-identical to the base commit's tool")
        self.assertEqual(outputs[0][0], outputs[1][0])

    def test_the_two_side_and_trio_outputs_are_byte_identical_to_the_film_v1_merge_tool(self) -> None:
        """LOOK-ASSIST-FILM-FLAVOR-2 and -3 add the regrade mode and rename the grade id; the two-side and trio outputs are otherwise the tool's
        at the #319 merge (758e978e), byte for byte. The base tool's FILM_GRADE_ID is set to film-v3 in memory (the one intended change), so
        both accept the same Film side."""
        base_dir = self.tmp / "v1-merge-tool"
        base_dir.mkdir()
        for rel in ("tools/profiling/look-flavor-diff.py", "tools/profiling/make-contact-sheet.py"):
            show = subprocess.run(["git", "-C", str(ROOT), "show", f"{FILM_V1_MERGE}:{rel}"], capture_output=True, timeout=60)
            if show.returncode != 0:
                self.skipTest(f"base commit {FILM_V1_MERGE[:12]} is not in this clone (shallow checkout)")
            data = show.stdout
            if rel.endswith("look-flavor-diff.py"):
                self.assertEqual(data.count(b'FILM_GRADE_ID = "film-v1"'), 1)
                data = data.replace(b'FILM_GRADE_ID = "film-v1"', b'FILM_GRADE_ID = "film-v3"')
            (base_dir / Path(rel).name).write_bytes(data)
        tiles = {"classic": {0: tile(70), 1: tile(71, letterbox=4)}, "cinematic": {0: tile(72), 1: tile(73, letterbox=4)}}
        tiles["film"] = {i: graded(a) for i, a in tiles["cinematic"].items()}
        frames = {"classic": {0: 30, 1: 75}, "cinematic": {0: 31, 1: 70}, "film": {0: 33, 1: 90}}
        trio, pair = [], []
        for tool in (TOOL, base_dir / "look-flavor-diff.py"):
            proc, out = self.run_trio(tiles, tool=tool, frames=frames)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            trio.append((proc.stdout.replace(str(out), "<OUT>"), FlavorDiffComposeTests.tree_hashes(out)))
            self.runs += 1
            run = self.tmp / f"twoside-v1merge{self.runs}"
            stage = run / ".claude-state" / "stage"
            cla_dir, cla_list = self.make_side(stage, "classic", tiles["classic"], frames["classic"])
            cin_dir, cin_list = self.make_side(stage, "cinematic", tiles["cinematic"], frames["cinematic"])
            sliders = {}
            for side, doc in (("classic", CLASSIC), ("cinematic", CINEMATIC)):
                sliders[side] = stage / f"sliders-{side}.json"
                sliders[side].write_text(json.dumps(doc), encoding="utf-8")
            pout = run / ".claude-state" / "pair"
            proc = subprocess.run([sys.executable, str(tool),
                                   "--classic-frames", str(cla_dir), "--classic-listed", str(cla_list), "--classic-sliders", str(sliders["classic"]),
                                   "--classic-flavor-reported", "classic", "--classic-receipt-id", "r-classic",
                                   "--cinematic-frames", str(cin_dir), "--cinematic-listed", str(cin_list), "--cinematic-sliders", str(sliders["cinematic"]),
                                   "--cinematic-flavor-reported", "cinematic", "--cinematic-receipt-id", "r-cinematic",
                                   "--clip-id", "M16-1243", "--venue", "bachelor", "--build-sha", "0123456789ab", "--out-dir", str(pout)],
                                  capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            pair.append((proc.stdout.replace(str(pout), "<OUT>"), FlavorDiffComposeTests.tree_hashes(pout)))
        self.assertEqual(trio[0][1], trio[1][1], "every trio output file is byte-identical to the #319 merge's tool")
        self.assertEqual(trio[0][0], trio[1][0])
        self.assertEqual(pair[0][1], pair[1][1], "every two-side output file is byte-identical to the #319 merge's tool")
        self.assertEqual(pair[0][0], pair[1][0])


# LOOK-ASSIST-FILM-FLAVOR-2: the regrade mode (Cinematic | v1 re-grade | v2 re-grade, frame-locked) --------------------------------------
FILM_V1_MERGE = "758e978e8c1488c5f0e6dafc941b8de30c2ed839"
STATE_OFF = {"agx": False, "lut": False, "filter": False, "source": "synthetic"}


def table_bytes(y, r, g, b):
    """Four 65536-entry tables as the pipeline test dumps them: uint16 little-endian, Y R G B."""
    import numpy as np
    return b"".join(np.asarray(t, dtype="<u2").tobytes() for t in (y, r, g, b))


def identity_tables():
    import numpy as np
    ident = np.arange(65536)
    return table_bytes(ident, ident, ident, ident)


def known_tables(lift, split):
    """A Y lift and a split (R down / B up below mid-grey, the reverse above), different per channel, so a swap or a skipped Y is visible."""
    import numpy as np
    v = np.arange(65536, dtype=np.float64)
    sign = np.where(v < 32768, -1.0, 1.0)
    y = np.clip(lift + v * (65535.0 - lift) / 65535.0, 0, 65535).astype(np.int64)
    r = np.clip(v + sign * split, 0, 65535).astype(np.int64)
    g = np.clip(v + 0.15 * split, 0, 65535).astype(np.int64)
    b = np.clip(v - sign * split, 0, 65535).astype(np.int64)
    return table_bytes(y, r, g, b)


def independent_regrade(arr, data):
    """The re-grade, computed independently: Y then the channel table at index c * 257, back by round(v / 257)."""
    import numpy as np
    t = np.frombuffer(data, dtype="<u2").reshape(4, 65536).astype(np.int64)
    out = np.empty(arr.shape, dtype=np.uint8)
    for c in range(3):
        v = t[c + 1][t[0][arr[..., c].astype(np.int64) * 257]]
        out[..., c] = np.floor(v / 257.0 + 0.5).astype(np.uint8)
    return out


class FlavorRegradeHarness(FlavorDiffHarness):
    def run_regrade(self, tiles: dict, v1: bytes | None, v2: bytes | None, *, state=STATE_OFF, film: dict | None = None,
                    frames: dict | None = None, extra=(), out_dir: Path | None = None, v3: bytes | None = None, tool: Path = TOOL):
        self.runs += 1
        run = self.tmp / f"regrade{self.runs}"
        stage = run / ".claude-state" / "stage"
        cin_dir, cin_list = self.make_side(stage, "cinematic", tiles, (frames or {}).get("cinematic"))
        state_path = stage / "cinematic-state.json"
        state_path.write_text(json.dumps(state), encoding="utf-8")
        paths = {}
        for name, data in (("v1", v1), ("v2", v2)):
            paths[name] = stage / f"film-{name}-shade.u16"
            if data is not None:
                paths[name].write_bytes(data)
        args = [sys.executable, str(tool), "regrade", "--cinematic-frames", str(cin_dir), "--cinematic-listed", str(cin_list),
                "--cinematic-state", str(state_path), "--v1-table", str(paths["v1"]), "--v2-table", str(paths["v2"]),
                "--cinematic-receipt-id", "r-cinematic", "--clip-id", "M16-1243", "--venue", "bachelor", "--build-sha", "0123456789ab"]
        if v3 is not None:
            paths["v3"] = stage / "film-v3-shade.u16"
            paths["v3"].write_bytes(v3)
            args += ["--v3-table", str(paths["v3"])]
        if film is not None:
            fd, fl = self.make_side(stage, "film", film, (frames or {}).get("film"))
            args += ["--film-frames", str(fd), "--film-listed", str(fl), "--film-receipt-id", "r-film"]
        out = out_dir if out_dir is not None else run / ".claude-state" / "regrade"
        args += ["--out-dir", str(out), *extra]
        return subprocess.run(args, capture_output=True, text=True, timeout=300), out


class FlavorRegradeRefusalTests(FlavorRegradeHarness):
    def test_a_missing_or_short_table_is_a_typed_refusal_not_a_traceback(self) -> None:
        for v1, v2, missing in ((None, b"\0" * (4 * 65536 * 2), "v1"), (b"\0" * (4 * 65536 * 2), None, "v2"), (b"\0" * 100, b"\0" * (4 * 65536 * 2), "v1")):
            with self.subTest(missing=missing, short=v1 is not None and len(v1) == 100):
                proc, out = self.run_regrade({0: None}, v1, v2)
                self.assertEqual(proc.returncode, 19, proc.stdout + proc.stderr)
                self.assertTrue(proc.stderr.startswith("REGRADE_TABLE_INVALID"), proc.stderr)
                self.assertIn(f"the {missing} table", proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(self.pngs(out), [])

    def test_a_cinematic_render_with_agx_lut_or_filter_on_is_regrade_invalid_and_writes_nothing(self) -> None:
        for state in (dict(STATE_OFF, agx=True), dict(STATE_OFF, lut=True), dict(STATE_OFF, filter=True), {"source": "nothing said"}):
            with self.subTest(state=state):
                proc, out = self.run_regrade({0: None}, b"\0" * (4 * 65536 * 2), b"\0" * (4 * 65536 * 2), state=state)
                self.assertEqual(proc.returncode, 18, proc.stdout + proc.stderr)
                self.assertTrue(proc.stderr.startswith("REGRADE_INVALID"), proc.stderr)
                self.assertFalse(out.exists() and any(out.iterdir()))

    def test_an_out_dir_outside_claude_state_is_refused(self) -> None:
        proc, out = self.run_regrade({0: None}, None, None, out_dir=self.tmp / "public" / "regrade")
        self.assertEqual(proc.returncode, 13, proc.stdout + proc.stderr)
        self.assertFalse(out.exists())


@requires_imaging
class FlavorRegradeComposeTests(FlavorRegradeHarness):
    def test_identity_tables_reproduce_the_cinematic_capture_byte_for_byte(self) -> None:
        import numpy as np
        from PIL import Image
        cin = {0: tile(80), 1: tile(81, letterbox=4)}
        proc, out = self.run_regrade(cin, identity_tables(), identity_tables())
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("LOOK_FLAVOR_REGRADE_OK", proc.stdout)
        for i, arr in cin.items():
            for v in ("v1", "v2"):
                with Image.open(out / f"regrade-{v}-{i:02d}.png") as im:
                    self.assertTrue(np.array_equal(np.asarray(im.convert("RGB")), arr), f"{v} tile {i}")
        m = json.loads((out / "regrade-metrics.json").read_text(encoding="utf-8"))
        self.assertIs(m["valid"], True)
        for t in m["tiles"]:
            self.assertEqual((t["MAD"]["v1"], t["MAD"]["v2"]), (0.0, 0.0))
            self.assertEqual((t["dS"]["v1"], t["dS"]["v2"]), (0.0, 0.0))

    def test_known_tables_give_the_independently_computed_regrade_and_metrics(self) -> None:
        import numpy as np
        from PIL import Image
        cin = {0: tile(82), 1: tile(83, letterbox=3)}
        film = {i: graded(a) for i, a in cin.items()}
        v1 = known_tables(lift=300, split=900)
        v2 = known_tables(lift=1300, split=2300)
        proc, out = self.run_regrade(cin, v1, v2, film=film, frames={"cinematic": {0: 30, 1: 70}, "film": {0: 34, 1: 99}})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        m = json.loads((out / "regrade-metrics.json").read_text(encoding="utf-8"))
        with Image.open(out / "regrade-cinematic-v1-v2.png") as sheet:
            self.assertEqual(sheet.size, (3840, 220 + 2 * (44 + round(1280 * H / W))))
        ds = {"v1": [], "v2": []}
        for t, i in zip(m["tiles"], (0, 1)):
            keep = ~(independent_luma(cin[i]).max(axis=1) <= 2.0)
            expected = {"v1": independent_regrade(cin[i], v1), "v2": independent_regrade(cin[i], v2)}
            s_c, ga_c = independent_split_and_green(cin[i][keep])
            for v in ("v1", "v2"):
                with Image.open(out / f"regrade-{v}-{i:02d}.png") as im:
                    self.assertTrue(np.array_equal(np.asarray(im.convert("RGB")), expected[v]), f"{v} tile {i} pixels")
                s_v, ga_v = independent_split_and_green(expected[v][keep])
                self.assertAlmostEqual(t[v]["S"], s_v, delta=1e-6, msg=f"S {v}")
                self.assertAlmostEqual(t[v]["GA"], ga_v, delta=1e-6, msg=f"GA {v}")
                self.assertAlmostEqual(t["dS"][v], s_v - s_c, delta=1e-6)
                self.assertAlmostEqual(t["MAD"][v], independent_mad(expected[v][keep], cin[i][keep]), delta=1e-6)
                ds[v].append(s_v - s_c)
            self.assertAlmostEqual(t["film"]["MAD_v2_vs_film"], independent_mad(expected["v2"][keep], film[i][keep]), delta=1e-6)
        self.assertEqual([t["film"]["frame_matched"] for t in m["tiles"]], [True, False])
        self.assertAlmostEqual(m["dSRatioV2OverV1"], float(np.mean(ds["v2"]) / np.mean(ds["v1"])), delta=1e-6)
        # the known v2 table is the stronger split, as built
        self.assertGreater(m["means"]["dS"]["v2"], m["means"]["dS"]["v1"])

    def test_illustrative_composes_an_invalid_regrade_marked_invalid(self) -> None:
        cin = {0: tile(84)}
        proc, out = self.run_regrade(cin, identity_tables(), identity_tables(), state=dict(STATE_OFF, agx=True), extra=("--illustrative",))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("valid=false", proc.stdout)
        m = json.loads((out / "regrade-metrics.json").read_text(encoding="utf-8"))
        self.assertIs(m["valid"], False)
        self.assertTrue(any("agx" in r for r in m["invalidReasons"]))

    def test_a_regrade_into_an_occupied_out_dir_is_refused(self) -> None:
        shared = self.tmp / "occupied" / ".claude-state" / "regrade"
        shared.mkdir(parents=True)
        (shared / "regrade-metrics.json").write_bytes(b"earlier evidence")
        proc, out = self.run_regrade({0: tile(85)}, identity_tables(), identity_tables(), out_dir=shared)
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertEqual(sorted(p.name for p in out.iterdir()), ["regrade-metrics.json"])


# LOOK-ASSIST-WARMTH-MEASURE-1: the warm-cool (blue-amber) lean, mean((R + G) / 2 - B) on [0, 1] code values -------------------------------
def flat(rgb, rows=H):
    import numpy as np
    return np.tile(np.asarray(rgb, dtype=np.uint8), (rows, W, 1))


def constant_y_tables(dr, dg, db):
    """Y sends every code to 32768 (so a channel table applied BEFORE Y would read 0); each channel then adds a constant: every neutral input
    leaves as (32768 + dr, 32768 + dg, 32768 + db), lean ((dr + dg) / 2 - db) / 65535 exactly."""
    import numpy as np
    v = np.arange(65536)
    return table_bytes(np.full(65536, 32768), np.clip(v + dr, 0, 65535), np.clip(v + dg, 0, 65535), np.clip(v + db, 0, 65535))


def split_tables(step):
    """Identity Y, G and B; R up by `step` below mid-code and down by `step` above: the neutral lean is +step/2 in the low half, -step/2 in the
    high half, so the ramp lean is 0 and only a histogram that favours one half sees a lean."""
    import numpy as np
    v = np.arange(65536)
    return table_bytes(v, np.clip(v + np.where(v < 32768, step, -step), 0, 65535), v, v)


class WarmCoolHarness(FlavorDiffHarness):
    def run_warmcool(self, tables: dict, tiles: dict | None = None):
        self.runs += 1
        stage = self.tmp / f"warmcool{self.runs}" / ".claude-state" / "stage"
        stage.mkdir(parents=True)
        args = [sys.executable, str(TOOL), "warmcool"]
        for name, data in tables.items():
            path = stage / f"{name}.u16"
            path.write_bytes(data)
            args += ["--table", f"{name}={path}"]
        if tiles is not None:
            frames, listing = self.make_side(stage, "capture", tiles, None)
            args += ["--frames", str(frames), "--listed", str(listing)]
        return subprocess.run(args, capture_output=True, text=True, timeout=300)

    def lean_doc(self, tables: dict, tiles: dict | None = None):
        proc = self.run_warmcool(tables, tiles)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)


class WarmCoolRefusalTests(WarmCoolHarness):
    def test_a_short_table_is_a_typed_refusal_and_no_input_is_a_usage_error(self) -> None:
        proc = self.run_warmcool({"bad": b"\0" * 100})
        self.assertEqual(proc.returncode, 19, proc.stdout + proc.stderr)
        self.assertTrue(proc.stderr.startswith("REGRADE_TABLE_INVALID"), proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)
        self.assertEqual(self.run_warmcool({}).returncode, 2)


@requires_imaging
class WarmCoolMetricTests(WarmCoolHarness):
    def test_neutral_grey_is_zero_amber_is_positive_and_blue_is_negative(self) -> None:
        for rgb, expected in (((128, 128, 128), 0.0), ((150, 150, 100), 50 / 255), ((100, 100, 150), -50 / 255), ((160, 120, 100), 40 / 255)):
            with self.subTest(rgb=rgb):
                cap = self.lean_doc({}, {0: flat(rgb), 1: flat(rgb)})["capture"]
                self.assertAlmostEqual(cap["meanLean"], expected, delta=1e-12)
                self.assertEqual([t["lean"] for t in cap["tiles"]], [cap["meanLean"]] * 2)

    def test_a_table_ramp_lean_is_the_known_offset_and_y_runs_first(self) -> None:
        doc = self.lean_doc({"ident": identity_tables(), "amber": constant_y_tables(2570, 2570, -2570), "blue": constant_y_tables(-2570, -2570, 2570),
                             "redonly": constant_y_tables(1000, 0, 0)})["tables"]
        self.assertEqual(doc["ident"]["rampLean"], 0.0)
        self.assertAlmostEqual(doc["amber"]["rampLean"], 2 * 2570 / 65535, delta=1e-12)
        self.assertAlmostEqual(doc["blue"]["rampLean"], -2 * 2570 / 65535, delta=1e-12)
        self.assertAlmostEqual(doc["redonly"]["rampLean"], 500 / 65535, delta=1e-12)
        self.assertNotIn("histogramLean", doc["amber"], "no capture, no histogram")

    def test_the_histogram_lean_weights_the_neutral_curve_by_the_capture_and_dlean_is_the_regrade(self) -> None:
        import numpy as np
        tile0 = np.concatenate([flat((50, 50, 50), rows=30), flat((200, 200, 200), rows=10)])   # 3/4 of the pixels in the low half
        doc = self.lean_doc({"split": split_tables(2570)}, {0: tile0})
        t = doc["tables"]["split"]
        self.assertAlmostEqual(t["rampLean"], 0.0, delta=1e-12)
        self.assertAlmostEqual(t["histogramLean"], (0.75 - 0.25) * 1285 / 65535, delta=1e-12)
        cap = doc["capture"]
        self.assertEqual(cap["meanLean"], 0.0)
        # re-graded: 50 -> R 60, 200 -> R 190 (+-2570 / 257 = 10), so the pixel lean is +5 / 255 on 3/4 of the pixels and -5 / 255 on 1/4
        self.assertAlmostEqual(cap["meanRegradedLean"]["split"], (0.75 * 5 - 0.25 * 5) / 255, delta=1e-12)
        self.assertAlmostEqual(cap["meanDLean"]["split"], cap["meanRegradedLean"]["split"] - cap["meanLean"], delta=1e-15)

    def test_letterbox_rows_are_excluded_from_the_capture_lean(self) -> None:
        import numpy as np
        tile0 = flat((150, 150, 100))
        tile0[:4] = 0
        tile0[-4:] = 0
        cap = self.lean_doc({}, {0: tile0})["capture"]
        self.assertAlmostEqual(cap["meanLean"], 50 / 255, delta=1e-12)
        self.assertEqual(cap["tiles"][0]["rows_used"], H - 8)


# LOOK-FLAVOR-DIFF-WARMCOOL-INPUT-REFUSALS-1: a zero-sized staged tile and a malformed standalone-warmcool sidecar are typed refusals --------
# PIL can neither write nor open a zero-width or zero-height image, so the empty tile is injected the one way it can arise in the tool: read_rgb()
# returns an empty array. The wrapper below patches that one function and then runs the real main().
EMPTY_TILE_WRAPPER = """import importlib.util, sys
import numpy as np
spec = importlib.util.spec_from_file_location("lfd_empty", {tool!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
_real = module.read_rgb
def read_rgb(staged, sidecar, side):
    if sidecar.get("index") == {index}:
        return np.zeros(({h}, {w}, 3), dtype=np.uint8)
    return _real(staged, sidecar, side)
module.read_rgb = read_rgb
sys.exit(module.main(sys.argv[1:]))
"""


def list_dir(frames: Path, listing: Path) -> None:
    listing.write_text(json.dumps({"files": [{"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                                              for p in sorted(frames.iterdir())]}), encoding="utf-8")


@requires_imaging
class WarmCoolInputRefusalTests(FlavorRegradeHarness):
    EMPTY_SHAPES = ((0, H), (W, 0))     # (width, height) of the empty tile

    def empty_tile_tool(self, w, h, index=1) -> Path:
        path = self.tmp / f"empty-{w}x{h}-{self.runs}.py"
        path.write_text(EMPTY_TILE_WRAPPER.format(tool=str(TOOL), index=index, w=w, h=h), encoding="utf-8")
        return path

    def run_capture(self, tiles: dict, *, sidecars: dict | None = None, tool: Path = TOOL):
        """Standalone warmcool over a staged capture. `sidecars` {name: text} replaces a sidecar BEFORE the listing is hashed, so the
        listing still verifies and only the sidecar's own content is wrong."""
        self.runs += 1
        stage = self.tmp / f"capture{self.runs}" / ".claude-state" / "stage"
        frames, listing = self.make_side(stage, "capture", tiles, None)
        for name, text in (sidecars or {}).items():
            (frames / name).write_text(text, encoding="utf-8")
        list_dir(frames, listing)
        proc = subprocess.run([sys.executable, str(tool), "warmcool", "--frames", str(frames), "--listed", str(listing)],
                              capture_output=True, text=True, timeout=300)
        return proc, frames

    def test_warmcool_refuses_a_zero_sized_tile_with_a_named_token_and_no_measurement(self) -> None:
        for w, h in self.EMPTY_SHAPES:
            with self.subTest(size=f"{w}x{h}"):
                proc, _ = self.run_capture({0: tile(90), 1: tile(91)}, tool=self.empty_tile_tool(w, h))
                self.assertEqual(proc.returncode, 14, proc.stdout + proc.stderr)
                self.assertTrue(proc.stderr.startswith(f"WARMCOOL_TILE_EMPTY 1 {w}x{h}"), proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.stdout, "", "no measurement JSON, and so no NaN lean, may be printed")

    def test_warmcool_regrade_refuses_a_zero_sized_tile_with_a_named_token_and_writes_nothing(self) -> None:
        for w, h in self.EMPTY_SHAPES:
            for v3 in (False, True):
                with self.subTest(size=f"{w}x{h}", v3=v3):
                    proc, out = self.run_regrade({0: tile(92), 1: tile(93)}, identity_tables(), identity_tables(), tool=self.empty_tile_tool(w, h),
                                                 v3=identity_tables() if v3 else None)
                    self.assertEqual(proc.returncode, 14, proc.stdout + proc.stderr)
                    self.assertTrue(proc.stderr.startswith(f"REGRADE_TILE_EMPTY 1 {w}x{h}"), proc.stderr)
                    self.assertNotIn("Traceback", proc.stderr)
                    self.assertFalse(out.exists() and any(out.iterdir()), "a refused regrade leaves no sheet, no tile and no metrics.json")

    def test_warmcool_refuses_a_malformed_sidecar_instead_of_measuring_the_rest(self) -> None:
        for label, text in (("truncated", '{"index": 1, "saved": tr'), ("not json", "not json at all"), ("a list", "[1, 2, 3]"), ("empty", "")):
            with self.subTest(sidecar=label):
                proc, frames = self.run_capture({0: tile(94), 1: tile(95), 2: tile(96)}, sidecars={"frame-01.json": text})
                self.assertEqual(proc.returncode, 14, proc.stdout + proc.stderr)
                self.assertTrue(proc.stderr.startswith("WARMCOOL_SIDECAR_MALFORMED " + str(frames / "frame-01.json")), proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertEqual(proc.stdout, "", "a partial capture is never reported as the whole one")

    def test_warmcool_still_skips_a_sidecar_that_says_saved_false(self) -> None:
        unsaved = json.dumps({"index": 1, "saved": False, "path": "frame-01.png"})
        proc, _ = self.run_capture({0: tile(97), 1: tile(98), 2: tile(99)}, sidecars={"frame-01.json": unsaved})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual([t["index"] for t in json.loads(proc.stdout)["capture"]["tiles"]], [0, 2])

    def test_warmcool_shared_loader_keeps_warn_and_skip_for_its_other_callers(self) -> None:
        spec = importlib.util.spec_from_file_location("mcs_loader", ROOT / "tools" / "profiling" / "make-contact-sheet.py")
        mcs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mcs)
        stage = self.tmp / "loader" / ".claude-state" / "stage"
        frames, listing = self.make_side(stage, "capture", {0: tile(100), 1: tile(101)}, None)
        (frames / "frame-01.json").write_text("{broken", encoding="utf-8")
        list_dir(frames, listing)
        staged = mcs.StagedFrames(frames, mcs.load_listing(listing))
        self.assertEqual([f["index"] for f in mcs.load_staged_frames(staged)], [0])
        self.assertEqual([f["index"] for f in mcs.load_frames(frames)], [0])

    def test_warmcool_usage_says_the_lean_is_not_the_probe_scripts_r_minus_b(self) -> None:
        proc = subprocess.run([sys.executable, str(TOOL), "warmcool", "--help"], capture_output=True, text=True, timeout=60)
        text = " ".join(proc.stdout.split())
        self.assertIn("warmCool = R - B", text)
        self.assertIn("code values", text)
        self.assertIn("WARMCOOL_TILE_EMPTY", text)
        self.assertIn("WARMCOOL_SIDECAR_MALFORMED", text)


# LOOK-ASSIST-FILM-FLAVOR-3: the optional v3 column of the regrade mode, and the warm-cool guard --------------------------------------------
WARMTH_MEASURE_MERGE = "5757fc6674ac022dcc95d86b2b1a0417a72629be"   # #358: the tool as it was before this card


def independent_lean(arr):
    """mean((R + G) / 2 - B) / 255, computed independently."""
    import numpy as np
    f = arr.reshape(-1, 3).astype(np.float64) / 255.0
    return float(((f[:, 0] + f[:, 1]) / 2.0 - f[:, 2]).mean())


def merged_tool(test, tmp: Path, name: str) -> Path:
    """The look-flavor-diff.py (and its make-contact-sheet.py) as #358 merged them, written under `tmp`; skips on a shallow clone."""
    base_dir = tmp / name
    base_dir.mkdir()
    for rel in ("tools/profiling/look-flavor-diff.py", "tools/profiling/make-contact-sheet.py"):
        show = subprocess.run(["git", "-C", str(ROOT), "show", f"{WARMTH_MEASURE_MERGE}:{rel}"], capture_output=True, timeout=60)
        if show.returncode != 0:
            test.skipTest(f"base commit {WARMTH_MEASURE_MERGE[:12]} is not in this clone (shallow checkout)")
        (base_dir / Path(rel).name).write_bytes(show.stdout)
    return base_dir / "look-flavor-diff.py"


@requires_imaging
class RegradeV3Tests(FlavorRegradeHarness):
    def test_the_v3_column_gives_the_independently_computed_regrade_metrics_ratio_and_dlean(self) -> None:
        import numpy as np
        from PIL import Image
        cin = {0: tile(86), 1: tile(87, letterbox=3)}
        film = {i: graded(a) for i, a in cin.items()}
        tables = {"v1": known_tables(lift=300, split=900), "v2": known_tables(lift=1300, split=2300), "v3": known_tables(lift=1300, split=1600)}
        proc, out = self.run_regrade(cin, tables["v1"], tables["v2"], v3=tables["v3"], film=film,
                                     frames={"cinematic": {0: 30, 1: 70}, "film": {0: 30, 1: 99}})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("dSv3=", proc.stdout)
        self.assertFalse((out / "regrade-cinematic-v1-v2.png").exists())
        with Image.open(out / "regrade-cinematic-v1-v2-v3.png") as sheet:
            self.assertEqual(sheet.size, (3840, 220 + 2 * (44 + round(960 * H / W))))
        m = json.loads((out / "regrade-metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(m["sheet"], "regrade-cinematic-v1-v2-v3.png")
        self.assertEqual(sorted(m["tables"]), ["v1", "v2", "v3"])
        ds, dlean = {v: [] for v in tables}, {v: [] for v in tables}
        for t, i in zip(m["tiles"], (0, 1)):
            keep = ~(independent_luma(cin[i]).max(axis=1) <= 2.0)
            s_c, _ = independent_split_and_green(cin[i][keep])
            for v, data in tables.items():
                expected = independent_regrade(cin[i], data)
                with Image.open(out / f"regrade-{v}-{i:02d}.png") as im:
                    self.assertTrue(np.array_equal(np.asarray(im.convert("RGB")), expected), f"{v} tile {i} pixels")
                s_v, ga_v = independent_split_and_green(expected[keep])
                self.assertAlmostEqual(t[v]["S"], s_v, delta=1e-6, msg=f"S {v}")
                self.assertAlmostEqual(t[v]["GA"], ga_v, delta=1e-6, msg=f"GA {v}")
                self.assertAlmostEqual(t["dS"][v], s_v - s_c, delta=1e-6)
                self.assertAlmostEqual(t["MAD"][v], independent_mad(expected[keep], cin[i][keep]), delta=1e-6)
                d = independent_lean(expected[keep]) - independent_lean(cin[i][keep])
                self.assertAlmostEqual(t["dLean"][v], d, delta=1e-9, msg=f"dLean {v}")
                ds[v].append(s_v - s_c)
                dlean[v].append(d)
            self.assertAlmostEqual(t["film"]["MAD_v3_vs_film"], independent_mad(independent_regrade(cin[i], tables["v3"])[keep], film[i][keep]),
                                   delta=1e-6)
        for v in tables:
            self.assertAlmostEqual(m["means"]["dLean"][v], float(np.mean(dlean[v])), delta=1e-9)
            self.assertAlmostEqual(m["means"]["dS"][v], float(np.mean(ds[v])), delta=1e-6)
        self.assertAlmostEqual(m["dSRatioV3OverV2"], float(np.mean(ds["v3"]) / np.mean(ds["v2"])), delta=1e-6)
        self.assertAlmostEqual(m["dSRatioV2OverV1"], float(np.mean(ds["v2"]) / np.mean(ds["v1"])), delta=1e-6)

    def test_without_v3_every_output_is_byte_identical_to_the_merged_tool(self) -> None:
        base = merged_tool(self, self.tmp, "warmth-merge-tool")
        cin = {0: tile(88), 1: tile(89, letterbox=4)}
        film = {i: graded(a) for i, a in cin.items()}
        v1, v2 = known_tables(lift=300, split=900), known_tables(lift=1300, split=2300)
        runs = []
        for tool in (TOOL, base):
            proc, out = self.run_regrade(cin, v1, v2, film=film, frames={"cinematic": {0: 30, 1: 70}, "film": {0: 34, 1: 99}}, tool=tool)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            metrics = json.loads((out / "regrade-metrics.json").read_text(encoding="utf-8"))
            # the table paths name each run's own stage directory; everything else must match byte for byte
            for doc in metrics["tables"].values():
                doc["path"] = Path(doc["path"]).name
            hashes = {k: v for k, v in FlavorDiffComposeTests.tree_hashes(out).items() if k != "regrade-metrics.json"}
            runs.append((proc.stdout.replace(str(out), "<OUT>"), hashes, metrics))
        self.assertEqual(runs[0][1], runs[1][1], "every PNG is byte-identical to the #358 merge's tool")
        self.assertEqual(runs[0][2], runs[1][2], "the metrics are the #358 merge's tool's")
        # L-04 adds one field to the OK line, the sha256 of every table graded; the rest of the line is the #358 merge's tool's
        added = f" tableSha256=v1:{hashlib.sha256(v1).hexdigest()},v2:{hashlib.sha256(v2).hexdigest()}"
        self.assertIn(added, runs[0][0])
        self.assertEqual(runs[0][0].replace(added, ""), runs[1][0])
        self.assertNotIn("dLean", json.dumps(runs[0][2]))


# LOOK-ASSIST-FILM-FLAVOR-3 r2 (catalogue L-04): a stale candidate table was graded and labelled as the shipped grade ---------------------------
class RegradeTableBindingRefusalTests(FlavorRegradeHarness):
    TABLE = 4 * 65536 * 2

    def test_a_table_other_than_the_expected_sha256_is_table_unbound_before_any_frame_is_read(self) -> None:
        v1, v2, v3 = b"\0" * self.TABLE, b"\2" * self.TABLE, b"\3" * self.TABLE
        sha = {name: hashlib.sha256(data).hexdigest() for name, data in (("v1", v1), ("v2", v2), ("v3", v3))}
        # with --v3-table the option binds v3; without it, v2 (the newest table graded). Another table's sha does not bind it.
        for bound, v3_data, expected in (("v3", v3, sha["v2"]), ("v3", v3, "0" * 64), ("v2", None, sha["v1"])):
            with self.subTest(bound=bound, expected=expected[:8]):
                proc, out = self.run_regrade({0: None}, v1, v2, v3=v3_data, extra=("--expect-table-sha256", expected))
                self.assertEqual(proc.returncode, 21, proc.stdout + proc.stderr)
                self.assertTrue(proc.stderr.startswith("TABLE_UNBOUND"), proc.stderr)
                self.assertIn(f"the {bound} table", proc.stderr)
                self.assertIn(sha[bound], proc.stderr)
                self.assertIn(expected, proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                self.assertFalse(out.exists() and any(out.iterdir()), "nothing is written")

    def test_an_expected_sha256_that_is_not_64_hex_digits_is_a_usage_error(self) -> None:
        for bad in ("913cbec9", "g" * 64, "0" * 65, ""):
            with self.subTest(bad=bad):
                proc, out = self.run_regrade({0: None}, b"\0" * self.TABLE, b"\0" * self.TABLE, extra=("--expect-table-sha256", bad))
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
                self.assertFalse(out.exists())


@requires_imaging
class RegradeTableBindingComposeTests(FlavorRegradeHarness):
    def test_the_expected_sha256_grades_and_every_output_names_the_bound_table(self) -> None:
        cin = {0: tile(90)}
        tables = {"v1": known_tables(lift=300, split=900), "v2": known_tables(lift=1300, split=2300), "v3": known_tables(lift=1300, split=1600)}
        sha = {v: hashlib.sha256(d).hexdigest() for v, d in tables.items()}
        proc, out = self.run_regrade(cin, tables["v1"], tables["v2"], v3=tables["v3"], extra=("--expect-table-sha256", sha["v3"].upper()))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f" tableSha256=v1:{sha['v1']},v2:{sha['v2']},v3:{sha['v3']}", proc.stdout)
        m = json.loads((out / "regrade-metrics.json").read_text(encoding="utf-8"))
        self.assertEqual(m["tableBinding"], {"table": "v3", "expectedSha256": sha["v3"], "bound": True})
        self.assertEqual({v: d["sha256"] for v, d in m["tables"].items()}, sha)
        self.assertTrue((out / "regrade-cinematic-v1-v2-v3.png").exists())

    def test_without_the_option_the_ok_line_still_names_every_table_and_no_binding_is_claimed(self) -> None:
        v1, v2 = identity_tables(), known_tables(lift=1300, split=2300)
        proc, out = self.run_regrade({0: tile(91)}, v1, v2)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(proc.stdout.rstrip().endswith(f" tableSha256=v1:{hashlib.sha256(v1).hexdigest()},v2:{hashlib.sha256(v2).hexdigest()}"),
                        proc.stdout)
        self.assertNotIn("tableBinding", json.loads((out / "regrade-metrics.json").read_text(encoding="utf-8")))


class WarmCoolBoundTests(WarmCoolHarness):
    BOUND = "0.0058823529411764705"   # WC_NEUTRAL_BOUND = 1.5 / 255

    def run_bound(self, tables: dict, tiles: dict | None = None, bound: str | None = BOUND, tool: Path = TOOL):
        self.runs += 1
        stage = self.tmp / f"warmbound{self.runs}" / ".claude-state" / "stage"
        stage.mkdir(parents=True)
        args = [sys.executable, str(tool), "warmcool"]
        for name, data in tables.items():
            path = stage / f"{name}.u16"
            path.write_bytes(data)
            args += ["--table", f"{name}={path}"]
        if tiles is not None:
            frames, listing = self.make_side(stage, "capture", tiles, None)
            args += ["--frames", str(frames), "--listed", str(listing)]
        if bound is not None:
            args += ["--max-abs-lean", bound]
        proc = subprocess.run(args, capture_output=True, text=True, timeout=300)
        return proc, stage

    def breaches(self, proc):
        return [line.split()[1:3] for line in proc.stderr.splitlines() if line.startswith("WARMCOOL_BOUND_EXCEEDED")]

    @requires_imaging
    def test_the_module_bound_is_the_apps_neutrality_bar(self) -> None:
        spec = importlib.util.spec_from_file_location("lfd_bound", TOOL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertEqual(module.WC_NEUTRAL_BOUND, 1.5 / 255)
        self.assertEqual(module.EXIT_WARMCOOL_BOUND, 20)
        self.assertEqual(repr(module.WC_NEUTRAL_BOUND), self.BOUND)

    @requires_imaging
    def test_a_ramp_lean_beyond_the_bound_either_way_is_refused_and_inside_it_passes(self) -> None:
        # constant_y_tables: lean ((dr + dg) / 2 - db) / 65535; 514 / 65535 = 2 / 255, 257 / 65535 = 1 / 255
        for name, data, code in (("amber2", constant_y_tables(257, 257, -257), 20), ("blue2", constant_y_tables(-257, -257, 257), 20),
                                 ("amber1", constant_y_tables(0, 0, -257), 0), ("blue1", constant_y_tables(0, 0, 257), 0)):
            with self.subTest(table=name):
                proc, _ = self.run_bound({name: data})
                self.assertEqual(proc.returncode, code, proc.stdout + proc.stderr)
                self.assertNotIn("Traceback", proc.stderr)
                doc = json.loads(proc.stdout)   # the measurement is printed either way
                self.assertIn("rampLean", doc["tables"][name])
                self.assertEqual(self.breaches(proc), [[name, "rampLean"]] if code else [])
                if code:
                    self.assertTrue(proc.stderr.startswith(f"WARMCOOL_BOUND_EXCEEDED {name} rampLean "), proc.stderr)

    @requires_imaging
    def test_a_histogram_breach_and_a_regrade_breach_are_each_refused_on_their_own(self) -> None:
        import numpy as np
        # A flat (200, 200, 50) capture: BT.601 luma 182.9 -> level 183; the channels sit at 200 and 50.
        v = np.arange(65536)
        band = lambda c: np.abs(v - c * 257) <= 128
        # R up by 2570 on the level-183 band only: the neutral curve there leans +5 / 255, the pixels (R = 200) never touch it.
        hist_only = table_bytes(v, np.clip(v + np.where(band(183), 2570, 0), 0, 65535), v, v)
        # B down by 2570 on the code-50 band only: the luma histogram never reaches it, the pixels' B does (lean +10 / 255).
        regrade_only = table_bytes(v, v, v, np.clip(v - np.where(band(50), 2570, 0), 0, 65535))
        cap = {0: flat((200, 200, 50)), 1: flat((200, 200, 50))}
        for name, data, which in (("histonly", hist_only, "histogramLean"), ("regradeonly", regrade_only, "meanDLean")):
            with self.subTest(table=name):
                proc, _ = self.run_bound({name: data}, cap)
                self.assertEqual(proc.returncode, 20, proc.stdout + proc.stderr)
                self.assertEqual(self.breaches(proc), [[name, which]])
                doc = json.loads(proc.stdout)
                self.assertLessEqual(abs(doc["tables"][name]["rampLean"]), 1.5 / 255)

    @requires_imaging
    def test_without_the_bound_the_output_and_exit_are_the_merged_tools(self) -> None:
        base = merged_tool(self, self.tmp, "warmth-merge-tool-wc")
        cap = {0: flat((200, 200, 50)), 1: flat((120, 110, 90))}
        tables = {"amber2": constant_y_tables(257, 257, -257), "split": split_tables(2570)}
        outs = []
        for tool in (TOOL, base):
            proc, stage = self.run_bound(tables, cap, bound=None, tool=tool)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertEqual(proc.stderr, "")
            outs.append(proc.stdout.replace(json.dumps(str(stage))[1:-1], "<STAGE>"))   # the path as JSON escapes it
        self.assertEqual(outs[0], outs[1])

    def test_a_negative_bound_is_a_usage_error(self) -> None:
        proc, _ = self.run_bound({"ident": b"\0" * (4 * 65536 * 2)}, bound="-1")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)


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

    # LOOK-ASSIST-FILM-FLAVOR-1: -FilmReceipt makes it a trio; the same equality checks hold across all three.
    def trio(self, classic, cinematic, film, out: Path | None = None):
        paths = []
        for name, doc in (("classic", classic), ("cinematic", cinematic), ("film", film)):
            p = self.tmp / f"{name}.receipt.json"
            p.write_text(json.dumps(doc), encoding="utf-8")
            paths.append(p)
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(DV / "New-VenueFlavorPair.ps1"),
                               "-ClassicReceipt", str(paths[0]), "-CinematicReceipt", str(paths[1]), "-FilmReceipt", str(paths[2]),
                               "-OutDir", str(out or self.tmp / ".claude-state" / "trio")], capture_output=True, text=True, timeout=300)

    def test_a_third_receipt_that_is_not_film_is_refused(self) -> None:
        proc = self.trio(synthetic_receipt("classic"), synthetic_receipt("cinematic"), synthetic_receipt("cinematic"))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_FLAVORS_WRONG the third receipt must be the film flavor", proc.stdout + proc.stderr)

    def test_a_film_receipt_of_another_build_is_refused(self) -> None:
        proc = self.trio(synthetic_receipt("classic"), synthetic_receipt("cinematic"), synthetic_receipt("film", manifest="b" * 64))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("PAIR_SUBJECT_DIFFERS the film receipt differs in subject.buildManifestSha256", proc.stdout + proc.stderr)

    def test_an_out_dir_that_holds_a_trio_record_is_refused(self) -> None:
        out = self.tmp / ".claude-state" / "occupied-trio"
        out.mkdir(parents=True)
        (out / "flavor-trio-a-b-c-bachelor.json").write_bytes(b'{"record":1}\n')
        proc = self.trio(synthetic_receipt("classic"), synthetic_receipt("cinematic"), synthetic_receipt("film"), out=out)
        self.assertEqual(proc.returncode, 16, proc.stdout + proc.stderr)
        self.assertIn("PAIR_RECORD_EXISTS", proc.stdout + proc.stderr)


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
        gate_log = work / "w" / "gate.log"   # (-GateLog is bound to the run's -WorkDir or a .claude-state directory)
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(DV / "Wait-VenueQuiet.ps1"), "-WorkDir", str(work / "w"),
                               "-AgentShare", str(work / "no-such-share"), "-AllowShareOverride", "-ReadBackoffSec", "0", "-GateLog", str(gate_log)],
                              capture_output=True, text=True, timeout=120)
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
                               "-AgentShare", str(work / "share"), "-AllowShareOverride", "-MaxGateSec", "0"], capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("queued=1 running=0", proc.stdout)
        self.assertIn("GATE_BUSY quiet-probe", proc.stdout)


if __name__ == "__main__":
    unittest.main()
