"""EXPORT-DEBAYER-QUALITY-1: the venue metric script (tools/profiling/dual-venue/debayer_quality.py) and the hub rule table
(tools/profiling/dual-venue/debayer_rule_table.py), on SYNTHETIC Bayer / RGB arrays built here. No clip, no footage, no MLVApp.

Pinned:
  * a sharp edge scores more detail (NCGE) than the same edge box-blurred, so detailRatio > 1;
  * seeded Gaussian noise raises sigma_Y monotonically;
  * an alternating chroma pattern injected on edges raises the false-colour score;
  * tile classification reads S2 only, is deterministic, and is identical for every fake debayer of one Bayer frame;
  * the EXPO / RAWI header probe reads synthetic header bytes and refuses truncated input;
  * measure() end to end over capture files (and its quality.json validates against debayer-quality.schema.json);
  * the rule table on fixture per-debayer numbers gives the expected recommended and fastestAcceptable, refuses DEBAYER_INPUT_DIVERGED and
    DEBAYER_NOT_APPLIED, emits unmeasured rows, and validates against debayer-rule-table.schema.json.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import random
import struct
import tempfile
import unittest
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DV = ROOT / "tools" / "profiling" / "dual-venue"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, DV / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dq = _load("debayer_quality")
rt = _load("debayer_rule_table")

TILE = 64
W = H = 2 * dq.BORDER + 4 * TILE   # a 4 x 4 tile grid
PER = 4
WHITE = 16383


def scene(x: int, y: int) -> tuple:
    """Left half flat mid-grey (it runs 8 px past the half so no FLAT tile's box filter reaches a stripe); then vertical stripes (period 8) well below the clip point."""
    if x < W // 2 + 8:
        return (6000, 6000, 6000)
    v = 2000 if (x // 4) % 2 == 0 else 12000
    return (v, v, v)


def rgb_from(fn) -> list:
    return [[fn(x, y) for x in range(W)] for y in range(H)]


def box_blur(img: list, rad: int) -> list:
    out = []
    for y in range(H):
        row = []
        for x in range(W):
            acc = [0, 0, 0]
            n = 0
            for yy in range(max(0, y - rad), min(H, y + rad + 1)):
                for xx in range(max(0, x - rad), min(W, x + rad + 1)):
                    p = img[yy][xx]
                    acc[0] += p[0]
                    acc[1] += p[1]
                    acc[2] += p[2]
                    n += 1
            row.append(tuple(a // n for a in acc))
        out.append(row)
    return out


def rgb_plane(img: list) -> "dq.Plane":
    a = array("H", (min(65535, max(0, c)) for row in img for p in row for c in p))
    return dq.Plane(a.tobytes(), W, H, 3, W * 6)


def bayer_plane(img: list) -> "dq.Plane":
    """RGGB mosaic of an RGB image."""
    vals = []
    for y in range(H):
        for x in range(W):
            r, g, b = img[y][x]
            vals.append(r if (x % 2 == 0 and y % 2 == 0) else b if (x % 2 == 1 and y % 2 == 1) else g)
    a = array("H", vals)
    return dq.Plane(a.tobytes(), W, H, 1, W * 2)


def add_noise(img: list, sigma: float, seed: int) -> list:
    rnd = random.Random(seed)
    return [[tuple(int(round(c + rnd.gauss(0.0, sigma))) for c in p) for p in row] for row in img]


GREENS = ((1, 0), (0, 1))
BASE = rgb_from(scene)
S2 = bayer_plane(BASE)
TILES = dq.classify_tiles(S2, TILE, PER, 0.0, float(WHITE), GREENS)


class TileClassificationTests(unittest.TestCase):
    def test_flat_and_edge_sets_are_the_expected_tiles_and_deterministic(self) -> None:
        self.assertEqual(TILES["flat"], [0, 1, 4, 5])     # zero energy everywhere on the left: ties break by index
        self.assertEqual(TILES["edge"], [3, 7, 11, 15])   # the full-stripe column outranks the column that starts 8 px flat
        self.assertEqual(dq.classify_tiles(S2, TILE, PER, 0.0, float(WHITE), GREENS), TILES)

    def test_the_tile_sets_come_from_s2_only_so_every_fake_debayer_shares_them(self) -> None:
        sharp = rgb_plane(BASE)
        blurred = rgb_plane(box_blur(BASE, 1))
        for p in (sharp, blurred):
            m = dq.frame_metrics(p, TILES, TILE)   # the metric never re-classifies: the same TILES object serves both
            self.assertEqual(set(m), set(dq.METRIC_KEYS))
        self.assertEqual(dq.classify_tiles(bayer_plane(BASE), TILE, PER, 0.0, float(WHITE), GREENS), TILES)

    def test_a_clipped_tile_is_never_edge_and_short_sets_are_refused(self) -> None:
        clipped = rgb_from(lambda x, y: (16300, 16300, 16300) if x >= W // 2 and (x // 4) % 2 else scene(x, y))
        t = dq.classify_tiles(bayer_plane(clipped), TILE, 2, 0.0, float(WHITE), GREENS)
        self.assertTrue(set(t["edge"]).isdisjoint({2, 3, 6, 7, 10, 11, 14, 15}), t)
        with self.assertRaises(dq.QualityError) as cm:
            dq.classify_tiles(S2, TILE, 12, 0.0, float(WHITE), GREENS)
        self.assertEqual(cm.exception.token, "EXPORT_QUALITY_TILES_SHORT")

    def test_green_sites_follow_the_rawi_cfa_pattern(self) -> None:
        self.assertEqual(dq.green_offsets(0x02010100), (((1, 0), (0, 1)), "rawi"))
        self.assertEqual(dq.green_offsets(0x01020001), (((0, 0), (1, 1)), "rawi"))
        self.assertEqual(dq.green_offsets(0x00000000)[1], "default-rggb")
        self.assertEqual(dq.green_offsets(None)[1], "default-rggb")


class MetricTests(unittest.TestCase):
    def test_a_sharp_edge_scores_more_detail_than_a_blurred_one(self) -> None:
        sharp = dq.frame_metrics(rgb_plane(BASE), TILES, TILE)
        blur = dq.frame_metrics(rgb_plane(box_blur(BASE, 2)), TILES, TILE)
        self.assertGreater(sharp["ncge"], blur["ncge"])
        self.assertGreater(blur["ncge"], 0.0)
        self.assertGreater(sharp["ncge"] / blur["ncge"], 1.0)

    def test_noise_raises_sigma_y_monotonically(self) -> None:
        sig = [dq.frame_metrics(rgb_plane(add_noise(BASE, s, 1234)), TILES, TILE)["sigmaY"] for s in (0.0, 15.0, 45.0, 120.0)]
        self.assertEqual(sig[0], 0.0)
        for a, b in zip(sig, sig[1:]):
            self.assertLess(a, b, sig)
        # deterministic: the same seed gives the same number
        self.assertEqual(sig[2], dq.frame_metrics(rgb_plane(add_noise(BASE, 45.0, 1234)), TILES, TILE)["sigmaY"])

    def test_an_alternating_chroma_pattern_on_edges_raises_false_colour(self) -> None:
        def zipper(k: int):
            def f(x: int, y: int) -> tuple:
                r, g, b = scene(x, y)
                if x >= W // 2 and x % 4 in (3, 0):   # the pixels beside each stripe transition
                    s = k if (x + y) % 2 == 0 else -k
                    return (r + s, g, b - s)
                return (r, g, b)
            return f
        clean = dq.frame_metrics(rgb_plane(BASE), TILES, TILE)["fc"]
        weak = dq.frame_metrics(rgb_plane(rgb_from(zipper(300))), TILES, TILE)["fc"]
        strong = dq.frame_metrics(rgb_plane(rgb_from(zipper(1500))), TILES, TILE)["fc"]
        self.assertLess(clean, weak)
        self.assertLess(weak, strong)

    def test_a_plane_whose_bytes_disagree_with_its_geometry_is_refused(self) -> None:
        with self.assertRaises(dq.QualityError) as cm:
            dq.Plane(b"\x00" * 10, 4, 4, 1, 8)
        self.assertEqual(cm.exception.token, "EXPORT_QUALITY_CAPTURE_ABSENT")


def header_bytes(iso: int = 1600, black: int = 2047, white: int = 16200, cfa: int = 0x02010100, with_expo: bool = True) -> bytes:
    """A synthetic container header: MLVI, RAWI, EXPO, one VIDF (no frame data)."""
    mlvi = b"MLVI" + struct.pack("<I", 52) + b"\x00" * 44
    rawi_body = struct.pack("<HH", W, H) + struct.pack("<IIiiiiiii", 1, 0, H, W, W * 14 // 8, 0, 14, black, white) + b"\x00" * 40 + struct.pack("<I", cfa)
    rawi_body += b"\x00" * (180 - 16 - len(rawi_body))
    rawi = b"RAWI" + struct.pack("<I", 180) + struct.pack("<Q", 0) + rawi_body
    expo = b"EXPO" + struct.pack("<I", 40) + struct.pack("<Q", 0) + struct.pack("<IIIIQ", 0, iso, iso, 0, 1000)
    vidf = b"VIDF" + struct.pack("<I", 64) + b"\x00" * 56
    return mlvi + rawi + (expo if with_expo else b"") + vidf


class HeaderProbeTests(unittest.TestCase):
    def test_the_probe_reads_the_first_expo_and_rawi_blocks(self) -> None:
        got = dq.parse_mlv_header_blocks(header_bytes(iso=3200))
        self.assertEqual(got["expo"]["isoValue"], 3200)
        self.assertEqual((got["rawi"]["blackLevel"], got["rawi"]["whiteLevel"], got["rawi"]["cfaPattern"]), (2047, 16200, 0x02010100))

    def test_the_probe_refuses_truncated_or_malformed_input(self) -> None:
        good = header_bytes()
        for name, data in (("cut-in-expo", good[: 52 + 180 + 20]), ("cut-in-rawi", good[: 52 + 60]), ("wrong-magic", b"RIFF" + good[4:]),
                           ("tiny-size", good[:4] + struct.pack("<I", 4) + good[8:]), ("no-expo", header_bytes(with_expo=False))):
            with self.subTest(case=name), self.assertRaises(dq.QualityError) as cm:
                dq.parse_mlv_header_blocks(data)
            self.assertEqual(cm.exception.token, "EXPORT_QUALITY_CLIP_PROBE_FAILED")


def write_capture(d: Path, label: str, stage: str, frame: int, p: "dq.Plane") -> None:
    (d / f"{label}_{stage}_f{frame}.bin").write_bytes(p.a.tobytes())
    side = {"stage": stage, "frame": frame, "width": p.w, "height": p.h, "bytes_per_line": p.w * p.c * 2, "channels": p.c, "bit_depth": 16,
            "debayer_mode": "rcd"}   # a WRONG label on purpose: the script must never read it
    (d / f"{label}_{stage}_f{frame}.json").write_text(json.dumps(side), encoding="utf-8")


class MeasureEndToEndTests(unittest.TestCase):
    def run_measure(self, frames=(3, 7), drop: tuple | None = None) -> dict:
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            clip = d / "synthetic-header.bin"
            clip.write_bytes(header_bytes(iso=800, black=0, white=WHITE))
            for f in frames:
                for st, p in ((dq.STAGE_S2, S2), (dq.STAGE_S3, rgb_plane(BASE)), (dq.STAGE_S4, rgb_plane(box_blur(BASE, 1)))):
                    if drop == (st, f):
                        continue
                    write_capture(d, "q", st, f, p)
            return dq.measure(str(d), "q", list(frames), list(frames), list(dq.ALLOWED_STAGES), TILE, PER, str(clip), 2)

    def test_measure_publishes_scalars_tiles_iso_and_buffer_hashes_only(self) -> None:
        doc = self.run_measure()
        self.assertEqual((doc["debayer"], doc["receiptDebayer"], doc["isoValue"], doc["levelSource"], doc["cfaSource"]), ("bilinear", 2, 800, "rawi", "rawi"))
        self.assertEqual([r["frame"] for r in doc["perFrame"]], [3, 7])
        self.assertEqual(doc["perFrame"][0]["sha256"][dq.STAGE_S2], hashlib.sha256(S2.a.tobytes()).hexdigest())
        self.assertEqual(doc["perFrame"][0]["flatTiles"], TILES["flat"])
        text = json.dumps(doc)
        self.assertNotIn("synthetic-header", text, "no path or file name is ever published")
        self.assertNotIn("debayer_mode", text)
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        jsonschema.validate(json.loads(json.dumps(doc)), json.loads((DV / "debayer-quality.schema.json").read_text(encoding="utf-8")))

    def test_a_missing_capture_for_any_requested_frame_or_stage_is_capture_absent(self) -> None:
        for drop in ((dq.STAGE_S3, 7), (dq.STAGE_S2, 3), (dq.STAGE_S4, 3)):
            with self.subTest(drop=drop), self.assertRaises(dq.QualityError) as cm:
                self.run_measure(drop=drop)
            self.assertEqual(cm.exception.token, "EXPORT_QUALITY_CAPTURE_ABSENT")


# --- the rule table --------------------------------------------------------------------------------------------------------
REF = {"ncge": 100.0, "sigmaY": 10.0, "sigmaC": 5.0, "fc": 1.0}
# debayer -> (detailRatio, noiseRatio, falseColourRatio, debayerMsMean)
FIXTURE = {"bilinear": (1.00, 1.00, 1.00, 10.0), "amaze": (1.50, 1.05, 0.72, 60.0), "rcd": (1.48, 1.04, 0.73, 30.0),
           "dcb": (1.42, 1.02, 0.75, 25.0), "lmmse": (1.20, 0.98, 0.70, 40.0), "igv": (1.55, 1.30, 1.20, 45.0)}


def fixture_leg(clip: str, name: str, ratios: tuple, s2: str = "a" * 64, s3: str | None = None, outcome: str = "PASS", s3_ratios: tuple | None = None,
                quality_over: dict | None = None, frame_over: dict | None = None, no_s3: bool = False, ref_ncge: float | None = None) -> dict:
    ms = ratios[3]
    ncge0 = REF["ncge"] if ref_ncge is None else ref_ncge

    def mset(r):
        return {"ncge": ncge0 * r[0], "sigmaY": REF["sigmaY"] * r[1], "sigmaC1": 4.0, "sigmaC2": 4.0, "sigmaC": REF["sigmaC"], "fc": REF["fc"] * r[2]}
    s3sha = s3 or hashlib.sha256(name.encode()).hexdigest()
    s4sha = hashlib.sha256(b"s4-" + name.encode()).hexdigest()
    metrics = {"S4_processing": mset(ratios), "S3_debayer": mset(s3_ratios or ratios)}
    hashes = {"S2_post_dualiso": s2, "S3_debayer": s3sha, "S4_processing": s4sha}
    stages = ["S2_post_dualiso", "S3_debayer", "S4_processing"]
    if no_s3:
        del metrics["S3_debayer"], hashes["S3_debayer"]
        stages = ["S2_post_dualiso", "S4_processing"]
    frame = {"frame": 3, "flatTiles": [0, 1], "edgeTiles": [5, 6], "sha256": hashes}
    frame.update(frame_over or {})
    quality = {"metrics": metrics, "perFrame": [frame], "stages": stages, "tileSize": 64, "tilesPerClass": 16,
               "captureOffsets": [3], "frames": [3], "border": 4}
    quality.update(quality_over or {})
    return {"receiptId": f"{clip}-{name}", "clipId": clip, "legId": f"x-{name}", "outcome": outcome, "qualityVerdict": "PASS", "evidencePass": True, "debayer": name,
            "receiptDebayer": 0, "dualIso": True, "isoValue": 1600, "sourceCommit": "c" * 40, "buildManifestSha256": "d" * 64,
            "quality": quality,
            "exportWallSeconds": 20.0 + ms, "times": {"debayerMsMean": ms, "debayerMsP10": ms - 1, "debayerMsP90": ms + 1}}


def fixture_legs(clip: str = "M16-1243", **over) -> list:
    return [fixture_leg(clip, n, FIXTURE[n], **over.get(n, {})) for n in rt.DEBAYERS]


class RuleTableTests(unittest.TestCase):
    def test_the_rule_picks_the_expected_recommended_and_fastest_acceptable(self) -> None:
        table = rt.build_table(fixture_legs())
        dual = [c for c in table["classes"] if c["classKey"] == {"dualIso": True, "isoBand": "dual"}]
        self.assertEqual(len(dual), 1)
        row = dual[0]
        self.assertEqual(row["status"], "measured")
        # igv fails the false-colour and noise gates; amaze and rcd are within 2% on detail and rcd is faster; dcb is within 5% of rcd on all three
        # ratios and faster still, so it is the fastest acceptable.
        self.assertEqual((row["recommended"], row["fastestAcceptable"]), ("rcd", "dcb"))
        self.assertTrue(row["s3AgreesWithS4"])
        self.assertAlmostEqual(row["perDebayer"]["amaze"]["detailRatio"], 1.50)
        self.assertEqual(row["perDebayer"]["bilinear"]["detailRatio"], 1.0, "the reference is the bilinear floor, never AMaZE")
        self.assertEqual(row["perDebayer"]["rcd"]["receiptIds"], ["M16-1243-rcd"])

    def test_s3_disagreeing_with_s4_is_recorded(self) -> None:
        legs = fixture_legs(igv={"s3_ratios": (2.0, 0.9, 0.5, 45.0)})
        row = [c for c in rt.build_table(legs)["classes"] if c["status"] == "measured"][0]
        self.assertEqual(row["recommended"], "rcd")
        self.assertFalse(row["s3AgreesWithS4"])

    def test_cross_leg_integrity_refusals(self) -> None:
        with self.assertRaises(rt.RuleTableError) as cm:
            rt.build_table(fixture_legs(lmmse={"s2": "b" * 64}))
        self.assertEqual(cm.exception.token, "DEBAYER_INPUT_DIVERGED")
        with self.assertRaises(rt.RuleTableError) as cm:
            rt.build_table(fixture_legs(dcb={"s3": hashlib.sha256(b"rcd").hexdigest()}))
        self.assertEqual(cm.exception.token, "DEBAYER_NOT_APPLIED")

    def test_a_negative_detail_never_crashes_the_rule(self) -> None:
        # adversary r1 A2: ncge is EDGE minus FLAT gradient energy, so it is signed. With bilinear +100 and every other debayer -100 the best detail
        # ratio was -1, the 2% tie band (>= best * 0.98) held nothing, and sorted(near)[0] raised IndexError.
        legs = [fixture_leg("M16-1243", n, (1.0 if n == "bilinear" else -1.0, 1.0, 1.0, FIXTURE[n][3])) for n in rt.DEBAYERS]
        row = [c for c in rt.build_table(legs)["classes"] if c["status"] == "measured"][0]
        self.assertEqual(row["recommended"], "dcb", "every candidate ties at -1: the fastest of them")
        self.assertAlmostEqual(row["perDebayer"]["rcd"]["detailRatio"], -1.0)
        # a non-positive REFERENCE detail has no ratio at all (its sign would invert "more detail"), so no debayer is recommended on it
        neg = [fixture_leg("M16-1243", n, FIXTURE[n], ref_ncge=-100.0) for n in rt.DEBAYERS]
        row = [c for c in rt.build_table(neg)["classes"] if c["status"] == "measured"][0]
        self.assertIsNone(row["perDebayer"]["amaze"]["detailRatio"])
        self.assertEqual((row["recommended"], row["fastestAcceptable"]), (None, None))

    def test_a_leg_whose_quality_parameters_differ_is_refused(self) -> None:
        # adversary r1 A3: IGV requested no S3, so its S3 hash was None (five distinct hashes plus None passed DEBAYER_NOT_APPLIED) and the S3 ratios raised KeyError.
        legs = fixture_legs()
        legs[5] = fixture_leg("M16-1243", "igv", FIXTURE["igv"], no_s3=True)
        with self.assertRaises(rt.RuleTableError) as cm:
            rt.build_table(legs)
        self.assertEqual(cm.exception.token, "DEBAYER_QUALITY_PARAMS_DIVERGED")
        # adversary r1 A4: bilinear scored 1 tile per class against the others' 16 -- valid passes each, incomparable samples together
        with self.assertRaises(rt.RuleTableError) as cm:
            rt.build_table(fixture_legs(bilinear={"quality_over": {"tilesPerClass": 1}}))
        self.assertEqual(cm.exception.token, "DEBAYER_QUALITY_PARAMS_DIVERGED")
        for key, value in (("tileSize", 32), ("captureOffsets", [7]), ("frames", [7]), ("border", 2), ("stages", ["S2_post_dualiso", "S4_processing", "S3_debayer"])):
            with self.subTest(key=key), self.assertRaises(rt.RuleTableError) as cm:
                rt.build_table(fixture_legs(rcd={"quality_over": {key: value}}))
            self.assertEqual(cm.exception.token, "DEBAYER_QUALITY_PARAMS_DIVERGED")
        # the same parameters but a different tile population on a frame (the tile sets are S2-derived, so they must be identical)
        with self.assertRaises(rt.RuleTableError) as cm:
            rt.build_table(fixture_legs(lmmse={"frame_over": {"edgeTiles": [5, 7]}}))
        self.assertEqual(cm.exception.token, "DEBAYER_TILES_DIVERGED")

    def test_six_legs_that_all_skip_s3_are_measured_on_s4_alone(self) -> None:
        legs = [fixture_leg("M16-1243", n, FIXTURE[n], no_s3=True) for n in rt.DEBAYERS]
        row = [c for c in rt.build_table(legs)["classes"] if c["status"] == "measured"][0]
        self.assertEqual(row["recommended"], "rcd")
        self.assertIsNone(row["s3AgreesWithS4"])
        # without S3 the debayer-applied check reads S4: two debayers with one S4 buffer are refused
        same = [fixture_leg("M16-1243", n, FIXTURE[n], no_s3=True) for n in rt.DEBAYERS]
        same[2]["quality"]["perFrame"][0]["sha256"]["S4_processing"] = same[1]["quality"]["perFrame"][0]["sha256"]["S4_processing"]
        with self.assertRaises(rt.RuleTableError) as cm:
            rt.build_table(same)
        self.assertEqual(cm.exception.token, "DEBAYER_NOT_APPLIED")

    def test_unmeasured_rows_are_emitted_and_a_class_short_of_six_passes_stays_unmeasured(self) -> None:
        table = rt.build_table(fixture_legs())
        keys = {(c["classKey"]["dualIso"], c["classKey"]["isoBand"]): c["status"] for c in table["classes"]}
        self.assertEqual(keys, {(True, "dual"): "measured", (False, "low"): "unmeasured", (False, "high"): "unmeasured"})
        short = rt.build_table(fixture_legs(amaze={"outcome": "INVALID"}))
        row = [c for c in short["classes"] if c["classKey"]["isoBand"] == "dual"][0]
        self.assertEqual(row["status"], "unmeasured")
        self.assertIsNone(row["recommended"])
        self.assertIn("amaze", row["reason"])
        empty = rt.build_table([])
        self.assertTrue(all(c["status"] == "unmeasured" for c in empty["classes"]))

    def test_the_table_validates_against_its_schema(self) -> None:
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = json.loads((DV / "debayer-rule-table.schema.json").read_text(encoding="utf-8"))
        for table in (rt.build_table(fixture_legs()), rt.build_table([])):
            jsonschema.validate(json.loads(json.dumps(table)), schema)
        bad = rt.build_table([])
        bad["classes"][0]["recommended"] = "rcd"
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(bad, schema)

    def test_iso_bands(self) -> None:
        self.assertEqual([rt.iso_band(False, v) for v in (100, 400, 800, 1600, 6400)], ["low", "low", "mid", "high", "high"])
        self.assertEqual(rt.iso_band(True, 100), "dual")

    def test_debayer_times_sum_repeated_records_and_use_nearest_rank_percentiles(self) -> None:
        log = "\n".join(f"[mlv-stage-timing] frame={i} stage=debayered_frame ms={10 + i}.000" for i in range(10))
        log += "\n[mlv-stage-timing] frame=0 stage=debayered_frame ms=5.000\n[mlv-stage-timing] frame=0 stage=processing ms=99.000\n"
        t = rt.debayer_times(log)
        self.assertAlmostEqual(t["debayerMsMean"], (sum(10 + i for i in range(10)) + 5) / 10)
        self.assertEqual((t["debayerMsP10"], t["debayerMsP90"]), (11.0, 18.0))
        with self.assertRaises(rt.RuleTableError):
            rt.debayer_times("[mlv-stage-timing] frame=0 stage=processing ms=1.0\n")

    def test_load_leg_rehashes_every_input(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            ev = Path(td) / "ev"
            (ev / "export").mkdir(parents=True)
            files = {}
            q = {"debayer": "rcd", "receiptDebayer": 7, "isoValue": 1600, "metrics": {}, "perFrame": []}
            for name, text in (("quality.json", json.dumps(q)), ("stage-timing.log", "[mlv-stage-timing] frame=0 stage=debayered_frame ms=12.5\n"),
                               ("summary.json", json.dumps({"exportWallSeconds": 33.0}))):
                (ev / "export" / name).write_text(text, encoding="utf-8", newline="\n")
                files[name] = hashlib.sha256((ev / "export" / name).read_bytes()).hexdigest()
            receipt = {"receiptId": "r1", "outcome": "PASS", "subject": {"clipId": "M16-1243", "legId": "x", "sourceCommit": "c" * 40, "buildManifestSha256": "d" * 64},
                       "evidence": {"localEvidenceDir": str(ev)}, "export": {"dualIsoExpected": True, "files": files, "quality": {"verdict": "PASS"}}}
            rp = Path(td) / "r.json"
            rp.write_text(json.dumps(receipt), encoding="utf-8")
            got = rt.load_leg(str(rp))
            self.assertEqual((got["debayer"], got["qualityVerdict"], got["exportWallSeconds"], got["times"]["debayerMsMean"]), ("rcd", "PASS", 33.0, 12.5))
            (ev / "export" / "quality.json").write_text(json.dumps(dict(q, debayer="amaze")), encoding="utf-8")
            with self.assertRaises(rt.RuleTableError) as cm:
                rt.load_leg(str(rp))
            self.assertEqual(cm.exception.token, "RECEIPT_EVIDENCE_HASH_MISMATCH")

    @staticmethod
    def write_leg(td: str, name: str, quality: dict | None, summary_quality: dict, outcome: str = "PASS", leg_invalid=None, verdict: str = "PASS") -> str:
        ev = Path(td) / f"ev-{name}"
        (ev / "export").mkdir(parents=True)
        files = {}
        texts = [("stage-timing.log", "[mlv-stage-timing] frame=0 stage=debayered_frame ms=12.5\n")]
        qtext = json.dumps(quality) if quality is not None else None
        if qtext is not None:
            texts.append(("quality.json", qtext))
            summary_quality = dict(summary_quality, qualitySha256=summary_quality.get("qualitySha256", hashlib.sha256(qtext.encode()).hexdigest()))
        texts.append(("summary.json", json.dumps({"result": "EXPORT_CAPTURED", "exportWallSeconds": 33.0, "quality": summary_quality})))
        for fname, text in texts:
            (ev / "export" / fname).write_text(text, encoding="utf-8", newline="\n")
            files[fname] = hashlib.sha256((ev / "export" / fname).read_bytes()).hexdigest()
        receipt = {"receiptId": name, "outcome": outcome, "subject": {"clipId": "M16-1243", "legId": name, "sourceCommit": "c" * 40, "buildManifestSha256": "d" * 64},
                   "evidence": {"localEvidenceDir": str(ev)},
                   "export": {"dualIsoExpected": True, "files": files, "quality": {"verdict": verdict, "legInvalid": leg_invalid, "receiptDebayer": 7}}}
        rp = Path(td) / f"{name}.json"
        rp.write_text(json.dumps(receipt), encoding="utf-8")
        return str(rp)

    def test_load_leg_rederives_the_quality_pass_from_the_hashed_evidence(self) -> None:
        # adversary r1 pass 2 A3: a receipt whose top-level outcome was edited to PASS over a pass that left its captures behind (summary.quality.captureDeleted
        # false, receipt legInvalid set) loaded as a PASS and fed a measured row. The pass is now re-derived from the hashed summary.json, never the receipt's word.
        q = {"debayer": "rcd", "receiptDebayer": 7, "isoValue": 1600, "metrics": {}, "perFrame": []}
        good = {"result": "QUALITY_CAPTURED", "captureDeleted": True}
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(rt.load_leg(self.write_leg(td, "ok", q, good))["evidencePass"])
            self.assertFalse(rt.load_leg(self.write_leg(td, "kept", q, dict(good, captureDeleted=False), leg_invalid="EXPORT_CAPTURE_NOT_DELETED"))["evidencePass"])
            self.assertFalse(rt.load_leg(self.write_leg(td, "kept2", q, dict(good, captureDeleted=False)))["evidencePass"], "the hashed record alone decides")
            self.assertFalse(rt.load_leg(self.write_leg(td, "inv", q, good, leg_invalid="EXPORT_CAPTURE_NOT_DELETED"))["evidencePass"])
            self.assertFalse(rt.load_leg(self.write_leg(td, "sha", q, dict(good, qualitySha256="0" * 64)))["evidencePass"])
            self.assertFalse(rt.load_leg(self.write_leg(td, "res", q, dict(good, result="EXPORT_QUALITY_TILES_SHORT")))["evidencePass"])
            # adversary r1 pass 2 A5: a VENUE_TOOLING leg publishes no quality.json; it loads (unmeasured), it never stops the whole table
            tooling = rt.load_leg(self.write_leg(td, "tool", None, {"result": "EXPORT_QUALITY_TOOLING_ABSENT", "captureDeleted": True}, verdict="VENUE_TOOLING"))
            self.assertFalse(tooling["evidencePass"])
            self.assertEqual(tooling["debayer"], "rcd", "named from the receipt's debayer value, so the unmeasured reason can say which one is missing")
            # adversary r1 pass 2 A2: a debayer that is not one name string is refused typed, never a TypeError later
            with self.assertRaises(rt.RuleTableError) as cm:
                rt.load_leg(self.write_leg(td, "arr", dict(q, debayer=["rcd"]), good))
            self.assertEqual(cm.exception.token, "QUALITY_DOC_INVALID")

    def test_a_class_with_one_incomplete_clip_stays_unmeasured(self) -> None:
        # adversary r1 pass 2 A1: M16-1243 complete plus M16-1456 without AMaZE (same class) was measured on M16-1243 alone and dropped the M16-1456 reason
        legs = fixture_legs("M16-1243") + [x for x in fixture_legs("M16-1456") if x["debayer"] != "amaze"]
        row = [c for c in rt.build_table(legs)["classes"] if c["classKey"]["isoBand"] == "dual"][0]
        self.assertEqual(row["status"], "unmeasured")
        self.assertIsNone(row["recommended"])
        self.assertIn("M16-1456", row["reason"])
        self.assertIn("amaze", row["reason"])
        # and a leg whose evidence does not re-derive a pass counts as missing
        legs = fixture_legs()
        legs[2]["evidencePass"] = False
        row = [c for c in rt.build_table(legs)["classes"] if c["classKey"]["isoBand"] == "dual"][0]
        self.assertEqual(row["status"], "unmeasured")
        self.assertIn("rcd", row["reason"])


if __name__ == "__main__":
    unittest.main()
