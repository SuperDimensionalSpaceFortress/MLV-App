#!/usr/bin/env python3
"""Compose a Classic | Cinematic look-flavor diff from two look legs' captured frames (LOOK-ASSIST-CINEMATIC-BENCH-PAIR-1).

WHY THIS EXISTS
    The Bachelor CPU look benchmark runs the SAME leg twice from one build: once with Look Assist's Classic flavor and once with Cinematic
    (legs m16-1243-look-scale2 and m16-1243-look-scale2-cinematic). This tool puts the two captures side by side, adds a |dY| heatmap per
    tile, and measures each tile pair, so the owner can SEE Cinematic and the numbers say what changed. It refuses a pair whose flavor
    switch did nothing (the app fell back to Classic, or the flavor-owned sliders are identical): such a sheet would show two Classic
    looks under a Cinematic label.

    It is normally driven by tools/profiling/dual-venue/New-VenueFlavorPair.ps1, which validates both receipts, stages the hashed frames
    and reads the sliders from the validated evidence. It can be run by hand on any two staged captures.

INPUT
    --classic-frames / --cinematic-frames   staged frame dirs (frame-NN.png + frame-NN.json, as the app's contact-sheet pass writes them)
    --classic-listed / --cinematic-listed   {"files": [{"name", "sha256"}]}: every file each dir may hold. An unlisted file is refused and
                                            every file is verified against its sha256 AT READ TIME (make-contact-sheet.py's StagedFrames).
    --classic-sliders / --cinematic-sliders JSON object with the Look Assist sliders the run applied (result JSON visualQuality.lookAssist
                                            names: presetExposure, presetContrast, presetPivot, presetShadows, presetHighlights,
                                            presetVibrance, presetTemperatureDelta, presetTintDelta, finalTemperature, finalTint, scene).
    --classic-flavor-reported / --cinematic-flavor-reported   the flavor each app run reports having applied (visual_state look_assist_flavor).
    --clip-id --venue --build-sha (12 hex) --classic-receipt-id --cinematic-receipt-id   header text only.
    --out-dir                               must contain a `.claude-state` path segment: the frames are owner footage and stay local.

REFUSALS (exit codes; the token is the first word on stderr)
    2   usage (argparse)
    10  FLAVOR_INERT                       the five flavor-owned sliders (presetContrast, presetPivot, presetShadows, presetHighlights,
                                           presetVibrance) are equal on both sides, or the Cinematic side reported anything but
                                           `cinematic`, or the Classic side anything but `classic`. Nothing is written.
    11  PAIR_TILE_COUNT_DIFFERS            the two sides hold different tile indices.
    12  PAIR_FRAME_NOT_LISTED              an unlisted, hash-mismatched or out-of-directory file (also PAIR_FRAME_HASH_MISMATCH,
                                           PAIR_SIDECAR_PATH_OUTSIDE_STAGING, PAIR_LISTING_INVALID: make-contact-sheet.py's tokens).
    13  PAIR_OWNER_SHEET_MUST_STAY_LOCAL   --out-dir has no `.claude-state` segment.
    14  PAIR_INPUT_INVALID                 a slider file is unreadable or lacks a flavor-owned field, or a side has no saved frame.
    15  PAIR_TILE_SIZE_DIFFERS             tile i is not the same pixel size on both sides (no per-pixel difference is defined).
    16  PAIR_OUTPUT_EXISTS                 --out-dir already holds the sheet, metrics.json, table.md or a row-NN / heat-NN png. Outputs are
                                           append-only: checked before anything is written, and every file is created exclusively (the
                                           attempt marker, then the sheet), so a second or concurrent composer changes nothing of the first
                                           one's evidence.
    17  PAIR_INCOMPLETE_ATTEMPT            --out-dir holds .pair-in-progress.json: an earlier attempt wrote its marker and did not finish
                                           (a crash between the sheet and the metrics, or before the driver's record). What it left is NOT
                                           recorded evidence. Nothing is changed. Compose into a new directory, or re-run with
                                           --recover-incomplete, which MOVES the marker and the unrecorded outputs into incomplete-<utc>/
                                           (nothing is deleted) and composes again -- only when no composer is still running there.

ATTEMPT MARKER
    Before the first output the composer creates .pair-in-progress.json exclusively (pid, start time, receipt ids) and removes it after the last
    one. Exactly one composer can hold it, so it doubles as the directory's reservation. --keep-marker leaves it for the caller (the pair driver
    removes it after its own record is written, so the window between the sheet and the record is marked too). A composer that loses the race
    removes nothing of the winner's and leaves no marker of its own.

OUTPUT (in --out-dir)
    sheet-classic-vs-cinematic.png   3840 px wide. A HEADER_HEIGHT header (clip, venue, build, flavors, receipt ids, claims), then per tile a
                                     ROW_LABEL_HEIGHT label band (tile index, both display_frames, delta, `NOT FRAME-MATCHED (d=k)` on EVERY
                                     row once the pair's FRAME-MATCHED is false, i.e. when any |d| > FRAME_MATCH_TOLERANCE) over [Classic | Cinematic | |dY| heatmap] at 1280 px each, aspect kept.
                                     Height = HEADER_HEIGHT + tiles * (ROW_LABEL_HEIGHT + round(1280 * h / w)).
    row-NN.png                       Classic | Cinematic at 1920 px each (3840 wide), under a ROW_LABEL_HEIGHT label band.
    heat-NN.png                      |dY| at full resolution on a FIXED 0..HEAT_SCALE_MAX code-value scale (black -> red -> yellow -> white;
                                     exactly black where the tiles are equal), with a LEGEND_HEIGHT legend bar below it.
    metrics.json                     per tile, then the means over tiles, the sliders and the claims (frameMatched, sameFrames, maxFrameDelta,
                                     flavorLive).
    table.md                         the slider table (Classic | Cinematic | delta) and the per-frame table.

METRICS (on the full-resolution tiles)
    Median luma (BT.601 0.299/0.587/0.114 on 8-bit sRGB) and mean saturation ((max-min)/max, 0 at black) are make-contact-sheet.py's
    channel_stats(), imported, so the numbers compare with every earlier sheet; R/G/B means; mean |delta| per channel; p95 |dY|; and
    d display_frame = cinematic - classic. Letterbox rows -- a row whose max luma is <= LETTERBOX_MAX_LUMA on BOTH sides -- are excluded
    from every per-tile metric; channel_stats() of the whole frame is kept beside them as `fullFrame` for continuity.

    Frames are paired by recorded tile index. The app grabs "the first frame presented at or after each target time", so on CPU the two
    runs can present different frames at a tile; the offset is reported per tile, never hidden. Exact pinning is a harness change
    (CONTACT-SHEET-PINNED-FRAMES-1), out of this tool's scope.

    Requires Pillow + numpy, loaded only after the input refusals (out dir, sliders, flavor), so those refuse on any host.
"""
import argparse
import importlib.util
import io
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

np = Image = ImageDraw = mcs = None

EXIT_FLAVOR_INERT = 10
EXIT_TILE_COUNT = 11
EXIT_NOT_LISTED = 12
EXIT_NOT_LOCAL = 13
EXIT_INPUT_INVALID = 14
EXIT_TILE_SIZE = 15
EXIT_OUTPUT_EXISTS = 16
EXIT_INCOMPLETE = 17

SHEET_NAME = "sheet-classic-vs-cinematic.png"
MARKER_NAME = ".pair-in-progress.json"
SCHEMA_MARKER = "mlv-app/look-flavor-diff-in-progress/v1"
_OUTPUT_NAME = re.compile(r"^(?:sheet-classic-vs-cinematic\.png|metrics\.json|table\.md|(?:row|heat)-\d{2}\.png)$")
SCHEMA_METRICS = "mlv-app/look-flavor-diff-metrics/v1"
FLAVOR_OWNED_FIELDS = ("presetContrast", "presetPivot", "presetShadows", "presetHighlights", "presetVibrance")
SLIDER_FIELDS = ("scene", "presetExposure", *FLAVOR_OWNED_FIELDS, "presetTemperatureDelta", "presetTintDelta", "finalTemperature", "finalTint")
FRAME_MATCH_TOLERANCE = 12      # display frames: 0.5 s at 23.976
LETTERBOX_MAX_LUMA = 2.0
HEAT_SCALE_MAX = 64.0
SHEET_WIDTH = 3840
SHEET_COLUMN = 1280
ROW_COLUMN = 1920
HEADER_HEIGHT = 220
ROW_LABEL_HEIGHT = 44
LEGEND_HEIGHT = 64

_MCS_PATH = Path(__file__).resolve().parent / "make-contact-sheet.py"


def _load_imaging():
    """numpy, Pillow and make-contact-sheet.py (its StagedFrames reader and channel_stats definitions)."""
    global np, Image, ImageDraw, mcs
    import numpy as np
    from PIL import Image, ImageDraw
    spec = importlib.util.spec_from_file_location("make_contact_sheet", _MCS_PATH)
    mcs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mcs)


class Refusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def flavor_live(classic_sliders, cinematic_sliders, classic_reported, cinematic_reported):
    """(live, reasons). Live only when each side reports its own flavor AND at least one flavor-owned slider differs."""
    reasons = []
    if classic_reported != "classic":
        reasons.append(f"the Classic side reported lookFlavorReported={classic_reported!r}, not 'classic'")
    if cinematic_reported != "cinematic":
        reasons.append(f"the Cinematic side reported lookFlavorReported={cinematic_reported!r}, not 'cinematic' (the app fell back)")
    differing = [f for f in FLAVOR_OWNED_FIELDS if classic_sliders.get(f) != cinematic_sliders.get(f)]
    if not differing:
        reasons.append("the five flavor-owned sliders are identical: " + ", ".join(f"{f}={classic_sliders.get(f)}" for f in FLAVOR_OWNED_FIELDS))
    return (not reasons), reasons


def require_local(out_dir):
    parts = Path(out_dir).resolve().parts
    if ".claude-state" not in parts:
        raise Refusal(EXIT_NOT_LOCAL, "PAIR_OWNER_SHEET_MUST_STAY_LOCAL --out-dir must be under a .claude-state directory; a sheet of owner footage "
                                      "is never committed, attached or published")


def load_sliders(path, side):
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise Refusal(EXIT_INPUT_INVALID, f"PAIR_INPUT_INVALID the {side} slider file {path} is unreadable: {exc}") from exc
    if not isinstance(doc, dict):
        raise Refusal(EXIT_INPUT_INVALID, f"PAIR_INPUT_INVALID the {side} slider file is not a JSON object")
    missing = [f for f in FLAVOR_OWNED_FIELDS if doc.get(f) is None]
    if missing:
        raise Refusal(EXIT_INPUT_INVALID, f"PAIR_INPUT_INVALID the {side} slider file has no {', '.join(missing)}: the flavor switch cannot be judged")
    return doc


def load_side(frames_dir, listed_path, side):
    try:
        staged = mcs.StagedFrames(Path(frames_dir), mcs.load_listing(listed_path))
        frames = mcs.load_staged_frames(staged)
    except mcs.FrameConfinementError as exc:
        raise Refusal(EXIT_NOT_LISTED, str(exc)) from exc
    if not frames:
        raise Refusal(EXIT_INPUT_INVALID, f"PAIR_INPUT_INVALID the {side} side has no saved (saved=true) frame")
    return staged, frames


def read_rgb(staged, sidecar, side):
    try:
        image = mcs.open_staged_image(staged, sidecar)
    except mcs.FrameConfinementError as exc:
        raise Refusal(EXIT_NOT_LISTED, str(exc)) from exc
    if image is None:
        raise Refusal(EXIT_NOT_LISTED, f"PAIR_FRAME_NOT_LISTED the {side} side has no image for tile {sidecar.get('index')}")
    with image:
        return np.asarray(image.convert("RGB")).astype(np.uint8)


def luma_of(arr):
    a = arr.astype(np.float64)
    return 0.299 * a[:, :, 0] + 0.587 * a[:, :, 1] + 0.114 * a[:, :, 2]


def side_metrics(arr, keep, sidecar):
    kept = arr[keep]
    cs = mcs.channel_stats(Image.fromarray(kept))
    f = kept.astype(np.float64)
    return {
        "display_frame": sidecar.get("display_frame"),
        "elapsed_ms": sidecar.get("elapsed_ms"),
        "playback_path": sidecar.get("playback_path"),
        "luma_p50": cs["luma_p50"],
        "mean_saturation": cs["mean_saturation"],
        "mean_r": float(f[:, :, 0].mean()),
        "mean_g": float(f[:, :, 1].mean()),
        "mean_b": float(f[:, :, 2].mean()),
        "fullFrame": mcs.channel_stats(Image.fromarray(arr)),
    }


def heat_rgb(abs_dy):
    """|dY| -> RGB on the fixed 0..HEAT_SCALE_MAX scale: black -> red -> yellow -> white. Exactly black at 0."""
    t = np.clip(abs_dy / HEAT_SCALE_MAX, 0.0, 1.0)
    r = np.clip(3.0 * t, 0.0, 1.0)
    g = np.clip(3.0 * t - 1.0, 0.0, 1.0)
    b = np.clip(3.0 * t - 2.0, 0.0, 1.0)
    return (np.stack([r, g, b], axis=-1) * 255.0 + 0.5).astype(np.uint8)


def legend_strip(width, font):
    strip = Image.new("RGB", (width, LEGEND_HEIGHT), (16, 16, 16))
    bar_w = max(16, width - 220)
    ramp = heat_rgb(np.linspace(0.0, HEAT_SCALE_MAX, bar_w)[None, :].repeat(20, axis=0))
    strip.paste(Image.fromarray(ramp), (60, 8))
    draw = ImageDraw.Draw(strip)
    draw.text((8, 8), "|dY| 0", fill=(255, 255, 255), font=font)
    draw.text((60 + bar_w + 8, 8), f"{HEAT_SCALE_MAX:.0f}+", fill=(255, 255, 255), font=font)
    draw.text((60, 34), f"fixed scale 0..{HEAT_SCALE_MAX:.0f} 8-bit code values (BT.601 luma), black = equal", fill=(200, 200, 200), font=font)
    return strip


def row_label(tile, pair_matched):
    """The row's label. When the pair as a whole is NOT frame-matched every row says so (not only the rows past the tolerance)."""
    d = tile["display_frame_delta"]
    text = (f"tile {tile['index']:02d}  classic disp {tile['classic']['display_frame']}  cinematic disp {tile['cinematic']['display_frame']}  "
            f"d={d:+d}" if isinstance(d, int) else f"tile {tile['index']:02d}  display_frame unknown")
    if not (tile["frame_matched"] and pair_matched):
        text += f"  NOT FRAME-MATCHED (d={d})"
    return text


def output_names(indices):
    return [SHEET_NAME, "metrics.json", "table.md"] + [f"{kind}-{i:02d}.png" for i in indices for kind in ("row", "heat")]


def refuse_if_occupied(out, indices):
    """Every output is append-only: a name already in --out-dir is refused BEFORE anything is written into it (exit 16)."""
    existing = [n for n in output_names(indices) if (out / n).exists()]
    if existing:
        raise Refusal(EXIT_OUTPUT_EXISTS, "PAIR_OUTPUT_EXISTS " + ", ".join(existing) + f" already in {out}: an earlier pair's evidence is never "
                                          "overwritten; compose into a new directory")


def refuse_if_incomplete(out, recover):
    """True when `out` holds an earlier attempt's marker and the caller asked to recover it; exit 17 when it did not."""
    marker = out / MARKER_NAME
    if not marker.exists():
        return False
    if recover:
        return True
    try:
        started = json.loads(marker.read_text(encoding="utf-8")).get("startedUtc", "unknown time")
    except (OSError, ValueError, AttributeError):
        started = "unknown time"
    left = sorted(p.name for p in out.iterdir() if p.is_file() and _OUTPUT_NAME.match(p.name))
    raise Refusal(EXIT_INCOMPLETE, f"PAIR_INCOMPLETE_ATTEMPT {out} holds {MARKER_NAME} (an attempt started {started} and did not finish); it left "
                                   f"{', '.join(left) if left else 'no outputs'}, none of it recorded evidence. Nothing was changed. Compose into a new directory, or "
                                   "re-run with --recover-incomplete to MOVE the marker and those files into incomplete-<utc>/ (nothing is deleted) -- only "
                                   "when no composer is still running there")


def quarantine_incomplete(out):
    """Move the dead attempt's marker and unrecorded outputs into incomplete-<utc>/ beside them. Moves only; a pair record is never touched."""
    dest = out / ("incomplete-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ"))
    dest.mkdir()
    for path in sorted(out.iterdir()):
        if path.is_file() and (path.name == MARKER_NAME or _OUTPUT_NAME.match(path.name)):
            path.rename(dest / path.name)


def write_new(path, data):
    """Create `path` exclusively. A composer that lost a race for the directory fails here, on the first (sheet) write, with nothing of its own written."""
    try:
        with open(path, "xb") as handle:
            handle.write(data)
    except FileExistsError as exc:
        raise Refusal(EXIT_OUTPUT_EXISTS, f"PAIR_OUTPUT_EXISTS {Path(path).name} already in {Path(path).parent}: an earlier pair's evidence is never "
                                          "overwritten") from exc


def png_bytes(image):
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def fit(arr, width):
    image = Image.fromarray(arr)
    height = max(1, round(width * image.height / image.width))
    return image.resize((width, height), Image.LANCZOS)


def compose(args):
    require_local(args.out_dir)
    classic_sliders = load_sliders(args.classic_sliders, "Classic")
    cinematic_sliders = load_sliders(args.cinematic_sliders, "Cinematic")
    live, reasons = flavor_live(classic_sliders, cinematic_sliders, args.classic_flavor_reported, args.cinematic_flavor_reported)
    if not live:
        raise Refusal(EXIT_FLAVOR_INERT, "FLAVOR_INERT " + "; ".join(reasons))

    _load_imaging()
    classic_staged, classic_frames = load_side(args.classic_frames, args.classic_listed, "Classic")
    cinematic_staged, cinematic_frames = load_side(args.cinematic_frames, args.cinematic_listed, "Cinematic")
    classic_by = {f.get("index"): f for f in classic_frames}
    cinematic_by = {f.get("index"): f for f in cinematic_frames}
    if sorted(classic_by) != sorted(cinematic_by):
        raise Refusal(EXIT_TILE_COUNT, f"PAIR_TILE_COUNT_DIFFERS Classic holds tiles {sorted(classic_by)}, Cinematic {sorted(cinematic_by)}")

    out = Path(args.out_dir)
    recovering = refuse_if_incomplete(out, args.recover_incomplete)
    if not recovering:   # (a recovered directory's files are about to be moved aside, so they are not an obstacle)
        refuse_if_occupied(out, sorted(classic_by))
    font =mcs._load_font(22)
    small = mcs._load_font(16)
    tiles, panels, row_images, heat_images = [], [], {}, {}
    for index in sorted(classic_by):
        a = read_rgb(classic_staged, classic_by[index], "Classic")
        b = read_rgb(cinematic_staged, cinematic_by[index], "Cinematic")
        if a.shape != b.shape:
            raise Refusal(EXIT_TILE_SIZE, f"PAIR_TILE_SIZE_DIFFERS tile {index}: Classic {a.shape[1]}x{a.shape[0]}, Cinematic {b.shape[1]}x{b.shape[0]}")
        ya, yb = luma_of(a), luma_of(b)
        keep = ~((ya.max(axis=1) <= LETTERBOX_MAX_LUMA) & (yb.max(axis=1) <= LETTERBOX_MAX_LUMA))
        if not keep.any():
            keep = np.ones_like(keep)
        abs_d = np.abs(b.astype(np.float64) - a.astype(np.float64))[keep]
        abs_dy = np.abs(yb - ya)
        cla, cin = side_metrics(a, keep, classic_by[index]), side_metrics(b, keep, cinematic_by[index])
        da, db = cla["display_frame"], cin["display_frame"]
        delta = (db - da) if isinstance(da, int) and isinstance(db, int) else None
        tile = {
            "index": index,
            "classic": cla,
            "cinematic": cin,
            "delta": {k: cin[k] - cla[k] for k in ("luma_p50", "mean_saturation", "mean_r", "mean_g", "mean_b")},
            "mean_abs_delta": {"r": float(abs_d[:, :, 0].mean()), "g": float(abs_d[:, :, 1].mean()), "b": float(abs_d[:, :, 2].mean())},
            "p95_abs_delta_y": float(np.percentile(abs_dy[keep], 95)),
            "display_frame_delta": delta,
            "frame_matched": delta is not None and abs(delta) <= FRAME_MATCH_TOLERANCE,
            "rows_used": int(keep.sum()),
            "rows_excluded_letterbox": int((~keep).sum()),
            "size": [int(a.shape[1]), int(a.shape[0])],
        }
        tiles.append(tile)

        heat = heat_rgb(abs_dy)
        heat_image = Image.new("RGB", (heat.shape[1], heat.shape[0] + LEGEND_HEIGHT), (16, 16, 16))
        heat_image.paste(Image.fromarray(heat), (0, 0))
        heat_image.paste(legend_strip(heat.shape[1], small), (0, heat.shape[0]))
        heat_images[index] = heat_image

        ra, rb = fit(a, ROW_COLUMN), fit(b, ROW_COLUMN)
        row = Image.new("RGB", (SHEET_WIDTH, ROW_LABEL_HEIGHT + ra.height), (8, 8, 8))
        row.paste(ra, (0, ROW_LABEL_HEIGHT))
        row.paste(rb, (ROW_COLUMN, ROW_LABEL_HEIGHT))
        row_images[index] = row
        panels.append((tile, fit(a, SHEET_COLUMN), fit(b, SHEET_COLUMN), fit(heat, SHEET_COLUMN)))

    matched = all(t["frame_matched"] for t in tiles)
    for t in tiles:
        t["label"] = row_label(t, matched)
    same_frames = all(t["display_frame_delta"] == 0 for t in tiles)
    deltas = [abs(t["display_frame_delta"]) for t in tiles if t["display_frame_delta"] is not None]
    max_delta = max(deltas) if len(deltas) == len(tiles) else None

    tile_h = panels[0][1].height
    sheet = Image.new("RGB", (SHEET_WIDTH, HEADER_HEIGHT + len(panels) * (ROW_LABEL_HEIGHT + tile_h)), (8, 8, 8))
    draw = ImageDraw.Draw(sheet)
    header = [
        f"clip={args.clip_id}  venue={args.venue}  build={args.build_sha}  LEFT=Classic  MIDDLE=Cinematic  RIGHT=|dY| heatmap 0..{HEAT_SCALE_MAX:.0f}",
        f"flavors: classic reported={args.classic_flavor_reported}  cinematic reported={args.cinematic_flavor_reported}",
        f"receipts: classic={args.classic_receipt_id}  cinematic={args.cinematic_receipt_id}",
        "sliders classic:   " + "  ".join(f"{k}={classic_sliders.get(k)}" for k in SLIDER_FIELDS),
        "sliders cinematic: " + "  ".join(f"{k}={cinematic_sliders.get(k)}" for k in SLIDER_FIELDS),
        f"paired by tile index  FRAME-MATCHED(|d|<={FRAME_MATCH_TOLERANCE})={str(matched).lower()}  SAME-FRAMES={str(same_frames).lower()}  max|d|={max_delta}",
    ]
    for i, text in enumerate(header):
        draw.text((10, 8 + i * 34), text, fill=(255, 255, 255), font=font)
    for n, (tile, pa, pb, ph) in enumerate(panels):
        y = HEADER_HEIGHT + n * (ROW_LABEL_HEIGHT + tile_h)
        colour = (255, 255, 255) if matched else (255, 120, 120)
        draw.text((10, y + 10), tile["label"], fill=colour, font=font)
        for col, panel in enumerate((pa, pb, ph)):
            sheet.paste(panel.crop((0, 0, SHEET_COLUMN, tile_h)), (col * SHEET_COLUMN, y + ROW_LABEL_HEIGHT))
    sheet_path = out / SHEET_NAME
    # Nothing has been written yet. The attempt marker is created first and exclusively: it is this directory's reservation (see write_new), and it
    # stays until the last output (or, with --keep-marker, until the caller's own record) so an attempt that dies in between is recognisable.
    out.mkdir(parents=True, exist_ok=True)
    if recovering:
        quarantine_incomplete(out)
    marker_path = out / MARKER_NAME
    write_new(marker_path, json.dumps({"schema": SCHEMA_MARKER, "pid": os.getpid(), "startedUtc": datetime.now(timezone.utc).isoformat(),
                                       "classicReceiptId": args.classic_receipt_id, "cinematicReceiptId": args.cinematic_receipt_id,
                                       "keptForCaller": bool(args.keep_marker)}, indent=2).encode("utf-8"))
    try:
        write_new(sheet_path, png_bytes(sheet))
    except Refusal:
        marker_path.unlink(missing_ok=True)   # this composer lost the race for the sheet: nothing of ITS attempt may remain
        raise

    numeric = ("luma_p50", "mean_saturation", "mean_r", "mean_g", "mean_b")
    means = {
        "classic": {k: float(np.mean([t["classic"][k] for t in tiles])) for k in numeric},
        "cinematic": {k: float(np.mean([t["cinematic"][k] for t in tiles])) for k in numeric},
        "delta": {k: float(np.mean([t["delta"][k] for t in tiles])) for k in numeric},
        "mean_abs_delta": {c: float(np.mean([t["mean_abs_delta"][c] for t in tiles])) for c in "rgb"},
        "p95_abs_delta_y": float(np.mean([t["p95_abs_delta_y"] for t in tiles])),
    }
    doc = {
        "schema": SCHEMA_METRICS,
        "clipId": args.clip_id, "venue": args.venue, "buildSha12": args.build_sha,
        "classicReceiptId": args.classic_receipt_id, "cinematicReceiptId": args.cinematic_receipt_id,
        "lookFlavorReported": {"classic": args.classic_flavor_reported, "cinematic": args.cinematic_flavor_reported},
        "sliders": {"classic": {k: classic_sliders.get(k) for k in SLIDER_FIELDS}, "cinematic": {k: cinematic_sliders.get(k) for k in SLIDER_FIELDS}},
        "flavorOwnedFields": list(FLAVOR_OWNED_FIELDS),
        "flavorLive": live,
        "frameMatchTolerance": FRAME_MATCH_TOLERANCE,
        "frameMatched": matched, "sameFrames": same_frames, "maxFrameDelta": max_delta,
        "letterboxMaxLuma": LETTERBOX_MAX_LUMA, "heatScaleMax": HEAT_SCALE_MAX,
        "tiles": tiles, "means": means,
        "sheet": sheet_path.name,
    }
    write_new(out / "metrics.json", json.dumps(doc, indent=2).encode("utf-8"))
    write_new(out / "table.md", render_table(doc).encode("utf-8"))
    for index in sorted(row_images):
        label = next(t["label"] for t in tiles if t["index"] == index)
        ImageDraw.Draw(row_images[index]).text((10, 10), f"CLASSIC | CINEMATIC   {label}", fill=(255, 255, 255), font=font)
        write_new(out / f"row-{index:02d}.png", png_bytes(row_images[index]))
        write_new(out / f"heat-{index:02d}.png", png_bytes(heat_images[index]))
    if not args.keep_marker:
        marker_path.unlink()
    print(f"LOOK_FLAVOR_DIFF_OK sheet={sheet_path} tiles={len(tiles)} frameMatched={str(matched).lower()} "
          f"sameFrames={str(same_frames).lower()} maxFrameDelta={max_delta}")
    return 0


def _num(v, nd=2):
    return "-" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def render_table(doc):
    lines = ["### Look Assist sliders (flavor-owned fields marked *)", "", "| field | Classic | Cinematic | delta |", "|---|---|---|---|"]
    cla, cin = doc["sliders"]["classic"], doc["sliders"]["cinematic"]
    for k in SLIDER_FIELDS:
        a, b = cla.get(k), cin.get(k)
        d = (b - a) if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) else ("same" if a == b else "differs")
        lines.append(f"| {k}{' *' if k in FLAVOR_OWNED_FIELDS else ''} | {_num(a)} | {_num(b)} | {_num(d)} |")
    lines += ["", f"lookFlavorReported: classic={doc['lookFlavorReported']['classic']}, cinematic={doc['lookFlavorReported']['cinematic']}; "
                  f"flavorLive={str(doc['flavorLive']).lower()}", "",
              f"### Per tile (Classic -> Cinematic; letterbox rows excluded; FRAME-MATCHED = |d frame| <= {doc['frameMatchTolerance']})", "",
              "| tile | disp cla | disp cin | d frame | match | luma p50 cla / cin | sat cla / cin | R cla / cin | G cla / cin | B cla / cin | mean abs d R / G / B | p95 abs dY |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for t in doc["tiles"]:
        a, b, m = t["classic"], t["cinematic"], t["mean_abs_delta"]
        match = "yes" if (t["frame_matched"] and doc["frameMatched"]) else f"NOT FRAME-MATCHED (d={t['display_frame_delta']})"
        lines.append(f"| {t['index']:02d} | {a['display_frame']} | {b['display_frame']} | {_num(t['display_frame_delta'])} | {match} | "
                     f"{a['luma_p50']:.1f} / {b['luma_p50']:.1f} | {a['mean_saturation']:.4f} / {b['mean_saturation']:.4f} | "
                     f"{a['mean_r']:.2f} / {b['mean_r']:.2f} | {a['mean_g']:.2f} / {b['mean_g']:.2f} | {a['mean_b']:.2f} / {b['mean_b']:.2f} | "
                     f"{m['r']:.2f} / {m['g']:.2f} / {m['b']:.2f} | {t['p95_abs_delta_y']:.2f} |")
    mn = doc["means"]
    lines.append(f"| mean | | | | | {mn['classic']['luma_p50']:.1f} / {mn['cinematic']['luma_p50']:.1f} | "
                 f"{mn['classic']['mean_saturation']:.4f} / {mn['cinematic']['mean_saturation']:.4f} | "
                 f"{mn['classic']['mean_r']:.2f} / {mn['cinematic']['mean_r']:.2f} | {mn['classic']['mean_g']:.2f} / {mn['cinematic']['mean_g']:.2f} | "
                 f"{mn['classic']['mean_b']:.2f} / {mn['cinematic']['mean_b']:.2f} | "
                 f"{mn['mean_abs_delta']['r']:.2f} / {mn['mean_abs_delta']['g']:.2f} / {mn['mean_abs_delta']['b']:.2f} | {mn['p95_abs_delta_y']:.2f} |")
    lines += ["", f"FRAME-MATCHED={str(doc['frameMatched']).lower()}  SAME-FRAMES={str(doc['sameFrames']).lower()}  max |d frame|={doc['maxFrameDelta']}", ""]
    return "\n".join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for side in ("classic", "cinematic"):
        p.add_argument(f"--{side}-frames", required=True, type=Path)
        p.add_argument(f"--{side}-listed", required=True, type=Path)
        p.add_argument(f"--{side}-sliders", required=True, type=Path)
        p.add_argument(f"--{side}-flavor-reported", required=True)
        p.add_argument(f"--{side}-receipt-id", default="")
    p.add_argument("--clip-id", default="")
    p.add_argument("--venue", default="")
    p.add_argument("--build-sha", default="")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--keep-marker", action="store_true", help="leave .pair-in-progress.json for the caller to remove after its own record")
    p.add_argument("--recover-incomplete", action="store_true",
                   help="move an earlier unfinished attempt's marker and unrecorded outputs into incomplete-<utc>/ and compose again")
    args = p.parse_args(argv)
    try:
        return compose(args)
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
