"""EXPORT-DEBAYER-QUALITY-1: debayer quality metrics, computed ON THE VENUE from the pipeline-stage captures of one untimed quality run.

Python 3 standard library ONLY (no numpy: the venue is not proven to have it). Deterministic: integer or float64 math only, every sort breaks
ties by index. Exported footage never leaves the venue, so this script reads the raw captures where they were written and prints scalars,
tile indices and the sha256 of each captured buffer. It never writes a capture, a frame or a path into its output.

Inputs (MLVAPP_PIPELINE_CAPTURE_*, src/mlv/pipeline_stage_capture.h): <dir>/<label>_<stage>_f<frame>.bin (raw, no header) plus its .json
sidecar. Only the sidecar's geometry (width, height, bytes_per_line, channels) is read. Its debayer_mode label is NEVER read: the S3 label at
src/mlv/frame_caching.c:1253-1263 uses the wrong mapping (successor DEBAYER-CAPTURE-LABEL-1); the debayer is bound from the leg's receipt value.

Metrics (docs/debayer-quality-legs.md):
  * tiles: a grid of tileSize x tileSize tiles inside a 32 px border. Tiles are classified from S2_post_dualiso ONLY (the Bayer input, the same
    for every debayer of a clip), so the FLAT and EDGE sets are identical across debayers. Per tile: G-site gradient energy (mean |dG| between
    neighbouring 2x2-quad green means, both axes), mean green level and max raw value.
      FLAT = the tilesPerClass lowest-energy tiles whose mean lies in [10%, 80%] of the black-to-white range;
      EDGE = the tilesPerClass highest-energy tiles (not FLAT) whose max is under 98% of the range.
  * luma Y = (R + 2G + B) / 4 on S3 and S4; chroma C1 = R - G, C2 = B - G.
  * DETAIL ncge = mean over EDGE pixels of (Y[x+1]-Y[x-1])^2 + (Y[y+1]-Y[y-1])^2, minus the same mean over FLAT pixels.
  * NOISE sigmaY = sqrt(mean over FLAT tiles of var(Y - box3(Y))); sigmaC1 / sigmaC2 the same on C1 / C2; sigmaC = sqrt of the mean of both.
  * FALSE COLOUR fc: per EDGE tile, over its top 10% |grad Y| pixels, mean(|C1 - box5(C1)| + |C2 - box5(C2)|) / mean(Y of the tile); fc is
    the mean over EDGE tiles.
Each is computed per captured frame and averaged over frames.

Subcommands:
  measure   the venue run: read the captures, probe the clip's first EXPO (isoValue) and RAWI (levels, CFA) blocks read-only, write quality.json.
            Exit 0 = QUALITY_RESULT=QUALITY_CAPTURED; 3 = EXPORT_QUALITY_CAPTURE_ABSENT; 4 = EXPORT_QUALITY_CLIP_PROBE_FAILED; 2 = usage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import sys
from array import array

SCHEMA = "mlv-app/debayer-quality/v1"
CARD = "EXPORT-DEBAYER-QUALITY-1"
STAGE_S2 = "S2_post_dualiso"
STAGE_S3 = "S3_debayer"
STAGE_S4 = "S4_processing"
ALLOWED_STAGES = (STAGE_S2, STAGE_S3, STAGE_S4)
METRIC_STAGES = (STAGE_S3, STAGE_S4)
BORDER = 32
FLAT_RANGE = (0.10, 0.80)
EDGE_MAX_FRACTION = 0.98
FC_TOP_FRACTION = 0.10
# platform/qt/ReceiptSettings.h:16 enum DebayerAlgorithm{ None = 0, Simple, Bilinear, LMMSE, IGV, AMaZE, AHD, RCD, DCB }
RECEIPT_DEBAYER_NAMES = {0: "none", 1: "simple", 2: "bilinear", 3: "lmmse", 4: "igv", 5: "amaze", 6: "ahd", 7: "rcd", 8: "dcb"}
METRIC_KEYS = ("ncge", "sigmaY", "sigmaC1", "sigmaC2", "sigmaC", "fc")
MAX_HEADER_BLOCKS = 4096


class QualityError(Exception):
    def __init__(self, token: str, detail: str) -> None:
        super().__init__(f"{token}: {detail}")
        self.token = token
        self.detail = detail


# --- MLV header probe (read-only) -------------------------------------------------------------------------------------------
def parse_mlv_header_blocks(data_or_file) -> dict:
    """Walk the MLV block chain (blockType[4] + uint32 blockSize, src/mlv/mlv.h) from the start and return the first EXPO
    (isoMode, isoValue, isoAnalog, digitalGain: mlv_expo_hdr_t) and the first RAWI (raw_info black_level, white_level, cfa_pattern).
    Accepts bytes or a binary file object (it seeks over VIDF payloads; it never reads a frame). Refuses truncated or malformed input."""
    if isinstance(data_or_file, (bytes, bytearray)):
        import io
        fh = io.BytesIO(bytes(data_or_file))
    else:
        fh = data_or_file
    fh.seek(0, os.SEEK_END)
    end = fh.tell()
    pos = 0
    expo = None
    rawi = None
    first = True
    for _ in range(MAX_HEADER_BLOCKS):
        if pos + 8 > end:
            break
        fh.seek(pos)
        head = fh.read(8)
        if len(head) < 8:
            raise QualityError("EXPORT_QUALITY_CLIP_PROBE_FAILED", "truncated block header")
        btype = head[:4]
        (bsize,) = struct.unpack("<I", head[4:8])
        if first and btype != b"MLVI":
            raise QualityError("EXPORT_QUALITY_CLIP_PROBE_FAILED", "the file does not start with an MLVI block")
        first = False
        if bsize < 8 or pos + bsize > end:
            raise QualityError("EXPORT_QUALITY_CLIP_PROBE_FAILED", "a block size runs past the end of the file or is under 8 bytes")
        if btype == b"EXPO" and expo is None:
            if bsize < 32:
                raise QualityError("EXPORT_QUALITY_CLIP_PROBE_FAILED", "the EXPO block is truncated")
            fh.seek(pos + 16)
            body = fh.read(16)
            if len(body) < 16:
                raise QualityError("EXPORT_QUALITY_CLIP_PROBE_FAILED", "the EXPO block is truncated")
            iso_mode, iso_value, iso_analog, digital_gain = struct.unpack("<IIII", body)
            expo = {"isoMode": iso_mode, "isoValue": iso_value, "isoAnalog": iso_analog, "digitalGain": digital_gain}
        elif btype == b"RAWI" and rawi is None:
            if bsize < 100:
                raise QualityError("EXPORT_QUALITY_CLIP_PROBE_FAILED", "the RAWI block is truncated")
            fh.seek(pos + 16)
            body = fh.read(84)
            if len(body) < 84:
                raise QualityError("EXPORT_QUALITY_CLIP_PROBE_FAILED", "the RAWI block is truncated")
            # xRes, yRes (uint16), then raw_info (src/mlv/raw.h:138): api_version, buffer, height, width, pitch, frame_size, bits_per_pixel,
            # black_level, white_level, crop[4], active_area[4], exposure_bias[2], cfa_pattern
            black, white = struct.unpack("<ii", body[32:40])
            (cfa,) = struct.unpack("<I", body[80:84])
            (bpp,) = struct.unpack("<i", body[28:32])
            rawi = {"blackLevel": black, "whiteLevel": white, "cfaPattern": cfa, "bitsPerPixel": bpp}
        if expo is not None and rawi is not None:
            break
        pos += bsize
    if expo is None:
        raise QualityError("EXPORT_QUALITY_CLIP_PROBE_FAILED", "no EXPO block in the clip header")
    return {"expo": expo, "rawi": rawi}


def green_offsets(cfa_pattern) -> tuple:
    """The two green sites of a 2x2 quad as ((dx, dy), (dx, dy)). raw_info.cfa_pattern bytes are the colours of (0,0), (1,0), (0,1),
    (1,1) with 0=R 1=G 2=B (0x02010100 = RGGB). Anything that is not two greens on one diagonal falls back to RGGB."""
    rggb = ((1, 0), (0, 1))
    if cfa_pattern is None:
        return rggb, "default-rggb"
    b = [(cfa_pattern >> (8 * i)) & 0xFF for i in range(4)]
    sites = [(0, 0), (1, 0), (0, 1), (1, 1)]
    greens = tuple(sites[i] for i in range(4) if b[i] == 1)
    if greens in (((1, 0), (0, 1)), ((0, 0), (1, 1))):
        return greens, "rawi"
    return rggb, "default-rggb"


# --- buffers ----------------------------------------------------------------------------------------------------------------
class Plane:
    """One captured buffer: uint16 samples (little-endian on disk), channels interleaved."""

    def __init__(self, data: bytes, width: int, height: int, channels: int, bytes_per_line: int) -> None:
        if width <= 0 or height <= 0 or channels not in (1, 3):
            raise QualityError("EXPORT_QUALITY_CAPTURE_ABSENT", "a capture sidecar has an impossible geometry")
        if bytes_per_line != width * channels * 2 or len(data) != bytes_per_line * height:
            raise QualityError("EXPORT_QUALITY_CAPTURE_ABSENT", "a capture's byte length does not match its sidecar geometry")
        a = array("H")
        a.frombytes(data)
        if sys.byteorder != "little":
            a.byteswap()
        self.a = a
        self.w = width
        self.h = height
        self.c = channels


def tile_grid(width: int, height: int, tile: int) -> tuple:
    nx = (width - 2 * BORDER) // tile
    ny = (height - 2 * BORDER) // tile
    if nx <= 0 or ny <= 0:
        raise QualityError("EXPORT_QUALITY_CAPTURE_ABSENT", "the frame is too small for one tile inside the border")
    return nx, ny


def classify_tiles(s2: Plane, tile: int, per_class: int, black: float, white: float, greens: tuple) -> dict:
    """FLAT / EDGE tile indices from the Bayer capture (one channel). Index = ty * nx + tx."""
    if s2.c != 1:
        raise QualityError("EXPORT_QUALITY_CAPTURE_ABSENT", "the S2 capture is not one channel")
    nx, ny = tile_grid(s2.w, s2.h, tile)
    (gx0, gy0), (gx1, gy1) = greens
    w = s2.w
    a = s2.a
    q = tile // 2
    stats = []
    span = float(white - black)
    for ty in range(ny):
        y0 = BORDER + ty * tile
        for tx in range(nx):
            x0 = BORDER + tx * tile
            gm = []   # q rows of q green sums (G1 + G2, integers)
            tmax = 0
            for qy in range(q):
                y = y0 + 2 * qy
                r0 = a[y * w + x0: y * w + x0 + tile]
                r1 = a[(y + 1) * w + x0: (y + 1) * w + x0 + tile]
                m = max(max(r0), max(r1))
                if m > tmax:
                    tmax = m
                ga = (r0 if gy0 == 0 else r1)[gx0::2]
                gb = (r0 if gy1 == 0 else r1)[gx1::2]
                gm.append([u + v for u, v in zip(ga, gb)])
            esum = 0
            n = 0
            for row in gm:
                esum += sum(abs(row[i + 1] - row[i]) for i in range(q - 1))
                n += q - 1
            for r in range(q - 1):
                ra, rb = gm[r], gm[r + 1]
                esum += sum(abs(rb[i] - ra[i]) for i in range(q))
                n += q
            level = sum(sum(row) for row in gm) / (2.0 * q * q)
            stats.append((ty * nx + tx, esum / (2.0 * n), level, tmax))
    lo = black + FLAT_RANGE[0] * span
    hi = black + FLAT_RANGE[1] * span
    flat_c = sorted((s for s in stats if lo <= s[2] <= hi), key=lambda s: (s[1], s[0]))
    flat = sorted(s[0] for s in flat_c[:per_class])
    flat_set = set(flat)
    clip = black + EDGE_MAX_FRACTION * span
    edge_c = sorted((s for s in stats if s[3] < clip and s[0] not in flat_set), key=lambda s: (-s[1], s[0]))
    edge = sorted(s[0] for s in edge_c[:per_class])
    if len(flat) < per_class or len(edge) < per_class:
        raise QualityError("EXPORT_QUALITY_TILES_SHORT", f"only {len(flat)} FLAT and {len(edge)} EDGE tiles qualify (need {per_class} each)")
    return {"nx": nx, "ny": ny, "flat": flat, "edge": edge}


def _region(p: Plane, x0: int, y0: int, size: int, m: int) -> tuple:
    """Y4 = R + 2G + B, C1 = R - G, C2 = B - G over the tile plus an m-pixel margin, as row lists (integers)."""
    w = p.w
    Y, C1, C2 = [], [], []
    for y in range(y0 - m, y0 + size + m):
        base = (y * w + (x0 - m)) * 3
        row = p.a[base: base + (size + 2 * m) * 3]
        R, G, B = row[0::3], row[1::3], row[2::3]
        Y.append([r + 2 * g + b for r, g, b in zip(R, G, B)])
        C1.append([r - g for r, g in zip(R, G)])
        C2.append([b - g for b, g in zip(B, G)])
    return Y, C1, C2


def _box(rows: list, cx: int, cy: int, rad: int) -> float:
    s = 0
    for yy in range(cy - rad, cy + rad + 1):
        s += sum(rows[yy][cx - rad: cx + rad + 1])
    return s / float((2 * rad + 1) ** 2)


def _resid_var(rows: list, m: int, size: int, rad: int) -> float:
    vals = []
    for y in range(m, m + size):
        for x in range(m, m + size):
            vals.append(rows[y][x] - _box(rows, x, y, rad))
    mean = math.fsum(vals) / len(vals)
    return math.fsum((v - mean) ** 2 for v in vals) / len(vals)


def frame_metrics(p: Plane, tiles: dict, tile: int) -> dict:
    if p.c != 3:
        raise QualityError("EXPORT_QUALITY_CAPTURE_ABSENT", "an RGB capture is not three channels")
    nx = tiles["nx"]
    m = 2
    grad_edge = []
    grad_flat = []
    var_y, var_c1, var_c2 = [], [], []
    fc_tiles = []
    top_n = max(1, int(math.ceil(FC_TOP_FRACTION * tile * tile)))
    for kind in ("flat", "edge"):
        for idx in tiles[kind]:
            ty, tx = divmod(idx, nx)
            x0 = BORDER + tx * tile
            y0 = BORDER + ty * tile
            if p.w < x0 + tile + m or p.h < y0 + tile + m:
                raise QualityError("EXPORT_QUALITY_CAPTURE_ABSENT", "an RGB capture is smaller than the S2 tile grid")
            Y, C1, C2 = _region(p, x0, y0, tile, m)
            g2 = []   # (Y4 gradient energy, pixel index) over the tile; /16 turns Y4 into Y
            for y in range(m, m + tile):
                ry, ru, rd = Y[y], Y[y - 1], Y[y + 1]
                for x in range(m, m + tile):
                    gx = ry[x + 1] - ry[x - 1]
                    gy = rd[x] - ru[x]
                    g2.append((gx * gx + gy * gy, (y - m) * tile + (x - m)))
            if kind == "flat":
                grad_flat.extend(v for v, _ in g2)
                var_y.append(_resid_var(Y, m, tile, 1) / 16.0)
                var_c1.append(_resid_var(C1, m, tile, 1))
                var_c2.append(_resid_var(C2, m, tile, 1))
            else:
                grad_edge.extend(v for v, _ in g2)
                top = sorted(g2, key=lambda t: (-t[0], t[1]))[:top_n]
                exc = []
                for _, pi in top:
                    yy, xx = divmod(pi, tile)
                    yy += m
                    xx += m
                    exc.append(abs(C1[yy][xx] - _box(C1, xx, yy, 2)) + abs(C2[yy][xx] - _box(C2, xx, yy, 2)))
                ymean = math.fsum(Y[y][x] for y in range(m, m + tile) for x in range(m, m + tile)) / (4.0 * tile * tile)
                if ymean > 0:
                    fc_tiles.append((math.fsum(exc) / len(exc)) / ymean)
    ncge = (math.fsum(grad_edge) / len(grad_edge) - math.fsum(grad_flat) / len(grad_flat)) / 16.0
    sigma_y = math.sqrt(math.fsum(var_y) / len(var_y))
    s_c1 = math.sqrt(math.fsum(var_c1) / len(var_c1))
    s_c2 = math.sqrt(math.fsum(var_c2) / len(var_c2))
    sigma_c = math.sqrt((math.fsum(var_c1) / len(var_c1) + math.fsum(var_c2) / len(var_c2)) / 2.0)
    fc = math.fsum(fc_tiles) / len(fc_tiles) if fc_tiles else 0.0
    return {"ncge": ncge, "sigmaY": sigma_y, "sigmaC1": s_c1, "sigmaC2": s_c2, "sigmaC": sigma_c, "fc": fc}


def mean_metrics(rows: list) -> dict:
    return {k: math.fsum(r[k] for r in rows) / len(rows) for k in METRIC_KEYS}


# --- the venue run --------------------------------------------------------------------------------------------------------------
def load_capture(capture_dir: str, label: str, stage: str, frame: int) -> tuple:
    stem = os.path.join(capture_dir, f"{label}_{stage}_f{frame}")
    if not (os.path.isfile(stem + ".bin") and os.path.isfile(stem + ".json")):
        raise QualityError("EXPORT_QUALITY_CAPTURE_ABSENT", f"no {stage} capture for frame {frame}")
    with open(stem + ".json", "r", encoding="utf-8") as fh:
        side = json.load(fh)
    with open(stem + ".bin", "rb") as fh:
        data = fh.read()
    sha = hashlib.sha256(data).hexdigest()
    try:
        plane = Plane(data, int(side["width"]), int(side["height"]), int(side["channels"]), int(side["bytes_per_line"]))
    except (KeyError, TypeError, ValueError):
        raise QualityError("EXPORT_QUALITY_CAPTURE_ABSENT", f"the {stage} sidecar for frame {frame} has no usable geometry")
    return plane, sha


def measure(capture_dir: str, label: str, frames: list, offsets: list, stages: list, tile: int, per_class: int, clip_path: str,
            receipt_debayer: int) -> dict:
    with open(clip_path, "rb") as fh:
        probe = parse_mlv_header_blocks(fh)
    rawi = probe["rawi"]
    greens, cfa_source = green_offsets(None if rawi is None else rawi["cfaPattern"])
    per_frame = []
    level_source = "rawi"
    black = white = None
    for fi in frames:
        planes = {}
        shas = {}
        for st in stages:
            planes[st], shas[st] = load_capture(capture_dir, label, st, fi)
        s2 = planes[STAGE_S2]
        s2max = max(s2.a)
        if rawi is not None and rawi["whiteLevel"] > rawi["blackLevel"] >= 0 and s2max <= rawi["whiteLevel"]:
            black, white = float(rawi["blackLevel"]), float(rawi["whiteLevel"])
        else:
            # the dual-ISO recon may leave the samples on another scale than the RAWI levels: the frame's own range, recorded as such
            level_source = "observed"
            black, white = float(min(s2.a)), float(s2max)
        tiles = classify_tiles(s2, tile, per_class, black, white, greens)
        row = {"frame": fi, "sha256": shas, "flatTiles": tiles["flat"], "edgeTiles": tiles["edge"], "metrics": {}}
        for st in stages:
            if st in METRIC_STAGES:
                row["metrics"][st] = frame_metrics(planes[st], tiles, tile)
        per_frame.append(row)
    metrics = {st: mean_metrics([r["metrics"][st] for r in per_frame]) for st in stages if st in METRIC_STAGES}
    return {
        "schema": SCHEMA, "card": CARD, "result": "QUALITY_CAPTURED",
        "receiptDebayer": receipt_debayer, "debayer": RECEIPT_DEBAYER_NAMES.get(receipt_debayer, "unknown"),
        "captureOffsets": offsets, "frames": frames, "stages": stages, "tileSize": tile, "tilesPerClass": per_class, "border": BORDER,
        "isoValue": probe["expo"]["isoValue"], "levelSource": level_source, "cfaSource": cfa_source,
        "perFrame": per_frame, "metrics": metrics,
    }


def _int_list(text: str) -> list:
    return [int(t) for t in text.split(",") if t.strip() != ""]


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--capture-dir", required=True)
    m.add_argument("--label", required=True)
    m.add_argument("--frames", required=True, help="absolute zero-based frame indices, comma-separated")
    m.add_argument("--offsets", required=True, help="the leg's cut-in-relative captureOffsets, comma-separated")
    m.add_argument("--stages", required=True)
    m.add_argument("--tile-size", type=int, required=True)
    m.add_argument("--tiles-per-class", type=int, required=True)
    m.add_argument("--clip", required=True)
    m.add_argument("--receipt-debayer", type=int, required=True)
    m.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    stages = [s for s in a.stages.split(",") if s]
    if STAGE_S2 not in stages or any(s not in ALLOWED_STAGES for s in stages) or a.tile_size < 8 or a.tile_size % 2 or a.tiles_per_class < 1:
        print("QUALITY_RESULT=EXPORT_QUALITY_USAGE")
        return 2
    try:
        doc = measure(a.capture_dir, a.label, _int_list(a.frames), _int_list(a.offsets), stages, a.tile_size, a.tiles_per_class, a.clip,
                      a.receipt_debayer)
    except QualityError as e:
        print(f"QUALITY_RESULT={e.token}")
        print(f"QUALITY_DETAIL={e.detail}")
        return {"EXPORT_QUALITY_CAPTURE_ABSENT": 3, "EXPORT_QUALITY_CLIP_PROBE_FAILED": 4, "EXPORT_QUALITY_TILES_SHORT": 5}.get(e.token, 6)
    except OSError as e:
        print("QUALITY_RESULT=EXPORT_QUALITY_CLIP_PROBE_FAILED")
        print(f"QUALITY_DETAIL={type(e).__name__}")
        return 4
    with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, sort_keys=True, indent=1)
        fh.write("\n")
    print("QUALITY_RESULT=QUALITY_CAPTURED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
