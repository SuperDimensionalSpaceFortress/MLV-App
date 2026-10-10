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

RE-GRADE MODE (LOOK-ASSIST-FILM-FLAVOR-2, -3)
    `look-flavor-diff.py regrade ...` lays two engine-built Film tables (film-v1, film-v2), and optionally a third (film-v3), over ONE
    Cinematic capture: a frame-locked Cinematic | v1 | v2 (| v3) strip. See `look-flavor-diff.py regrade --help`.

WARM-COOL MODE (LOOK-ASSIST-WARMTH-MEASURE-1)
    `look-flavor-diff.py warmcool ...` measures the blue-amber lean, mean((R + G) / 2 - B) on [0, 1] code values, of engine grade tables
    (over a neutral ramp, and weighted by a capture's luma histogram) and of a capture before and after each table. Numbers only, on stdout.
    See `look-flavor-diff.py warmcool --help`.
"""
import argparse
import hashlib
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


def load_side(frames_dir, listed_path, side, strict_sidecars=False):
    try:
        staged = mcs.StagedFrames(Path(frames_dir), mcs.load_listing(listed_path))
        frames = mcs.load_staged_frames(staged, strict=strict_sidecars)
    except mcs.FrameConfinementError as exc:
        raise Refusal(EXIT_NOT_LISTED, str(exc)) from exc
    except mcs.SidecarMalformed as exc:
        raise Refusal(EXIT_INPUT_INVALID, f"WARMCOOL_SIDECAR_MALFORMED {staged.dir / exc.name} ({exc}): a capture with a sidecar that cannot "
                                          "be read is not measured as if the remaining tiles were all of it. Nothing is printed.") from exc
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


def refuse_empty_tile(arr, index, token):
    """A zero-width or zero-height tile has no pixel to measure: luma_of(arr).max(axis=1) raises on width 0, and height 0 leaves a NaN lean."""
    if arr.shape[0] == 0 or arr.shape[1] == 0:
        raise Refusal(EXIT_INPUT_INVALID, f"{token} {index} {arr.shape[1]}x{arr.shape[0]}: a tile with no pixels has no lean to measure. Nothing is written.")


def finite_json(doc, token):
    """json.dumps that never writes NaN or Infinity as a number: a non-finite measurement is a refusal, not a figure."""
    try:
        return json.dumps(doc, indent=2, allow_nan=False)
    except ValueError as exc:
        raise Refusal(EXIT_INPUT_INVALID, f"{token} a measurement is not a finite number ({exc}). Nothing is written.") from exc


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
        # FLAVOR-DIFF-RECOVER-RECORD-GUARD-1 / FLAVOR-TRIO-RECOVERY-PROTECT-1: a pair OR trio record names its outputs (the pair's sheet by hash), so a directory
        # that holds either is never recovered (nothing is moved).
        records = sorted(p.name for pattern in ("flavor-pair-*.json", "flavor-trio-*.json") for p in out.glob(pattern) if p.is_file())
        if records:
            raise Refusal(EXIT_OUTPUT_EXISTS, f"PAIR_RECORD_EXISTS {records[0]} is already in {out}: its outputs are named by that record, so --recover-incomplete "
                                              "will not move anything here; compose into a new directory")
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
    """Move the dead attempt's marker and unrecorded outputs into incomplete-<utc>/ beside them. Moves only; a pair or trio record is never touched."""
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


# ---- Three-side mode (LOOK-ASSIST-FILM-FLAVOR-1): Classic | Cinematic | Film ----
# Reached only when the --film-* arguments are given; without them the tool above is byte-for-byte the two-side tool.

TRIO_SHEET_NAME = "sheet-classic-cinematic-film.png"
SCHEMA_METRICS_TRIO = "mlv-app/look-flavor-diff-metrics/v2"
FILM_GRADE_ID = "film-v3"
TRIO_SIDES = ("classic", "cinematic", "film")


def film_live(film_sliders, film_reported):
    """(live, reasons). The Film side must report `film` AND carry presetGrade FILM_GRADE_ID (its colour grade was laid)."""
    reasons = []
    if film_reported != "film":
        reasons.append(f"the Film side reported lookFlavorReported={film_reported!r}, not 'film' (the app fell back)")
    grade = film_sliders.get("presetGrade") or "none"
    if grade != FILM_GRADE_ID:
        reasons.append(f"the Film side's presetGrade={grade!r}, not {FILM_GRADE_ID!r} (no colour grade was laid)")
    return (not reasons), reasons


def trio_output_names(indices):
    return [TRIO_SHEET_NAME, "metrics.json", "table.md"] + [
        f"{kind}-{i:02d}.png" for i in indices for kind in ("row", "heat-film-vs-classic", "heat-film-vs-cinematic")]


def mad(x, y):
    """MAD(X,Y): mean over channels of mean |X - Y| (rows already letterbox-filtered)."""
    d = np.abs(x.astype(np.float64) - y.astype(np.float64))
    return float(np.mean([d[..., c].mean() for c in range(3)]))


def split_and_green(kept):
    """S = BA(shadows) - BA(highlights), BA = mean(B - R); shadows = luma in [p05, p30], highlights = [p70, p95];
    GA = mean(G - (R+B)/2) over the mid band [p30, p70]. Luma is BT.601 on the 8-bit pixels, ranked per image."""
    f = kept.reshape(-1, 3).astype(np.float64)
    y = 0.299 * f[:, 0] + 0.587 * f[:, 1] + 0.114 * f[:, 2]
    p05, p30, p70, p95 = np.percentile(y, [5, 30, 70, 95])
    ba = f[:, 2] - f[:, 0]
    ga = f[:, 1] - (f[:, 0] + f[:, 2]) / 2.0
    sh = (y >= p05) & (y <= p30)
    hi = (y >= p70) & (y <= p95)
    mid = (y >= p30) & (y <= p70)
    return {"BA_shadows": float(ba[sh].mean()), "BA_highlights": float(ba[hi].mean()),
            "S": float(ba[sh].mean() - ba[hi].mean()), "GA": float(ga[mid].mean()),
            "luma_p05": float(p05), "luma_p30": float(p30), "luma_p70": float(p70), "luma_p95": float(p95)}


def heat_image_with_legend(abs_dy, small):
    heat = heat_rgb(abs_dy)
    image = Image.new("RGB", (heat.shape[1], heat.shape[0] + LEGEND_HEIGHT), (16, 16, 16))
    image.paste(Image.fromarray(heat), (0, 0))
    image.paste(legend_strip(heat.shape[1], small), (0, heat.shape[0]))
    return image


def compose_trio(args):
    require_local(args.out_dir)
    sliders = {s: load_sliders(getattr(args, f"{s}_sliders"), s.capitalize()) for s in TRIO_SIDES}
    reported = {s: getattr(args, f"{s}_flavor_reported") for s in TRIO_SIDES}
    live, reasons = flavor_live(sliders["classic"], sliders["cinematic"], reported["classic"], reported["cinematic"])
    flive, freasons = film_live(sliders["film"], reported["film"])
    if not (live and flive):
        raise Refusal(EXIT_FLAVOR_INERT, "FLAVOR_INERT " + "; ".join(reasons + freasons))

    _load_imaging()
    staged, by = {}, {}
    for s in TRIO_SIDES:
        staged[s], frames = load_side(getattr(args, f"{s}_frames"), getattr(args, f"{s}_listed"), s.capitalize())
        by[s] = {f.get("index"): f for f in frames}
    if not (sorted(by["classic"]) == sorted(by["cinematic"]) == sorted(by["film"])):
        raise Refusal(EXIT_TILE_COUNT, "PAIR_TILE_COUNT_DIFFERS Classic holds tiles {}, Cinematic {}, Film {}".format(
            sorted(by["classic"]), sorted(by["cinematic"]), sorted(by["film"])))
    indices = sorted(by["classic"])

    out = Path(args.out_dir)
    refuse_if_incomplete(out, False)   # a dead attempt's marker is the typed 17; the trio is never recovered in place
    existing = [n for n in trio_output_names(indices) if (out / n).exists()]
    if existing:
        raise Refusal(EXIT_OUTPUT_EXISTS, "PAIR_OUTPUT_EXISTS " + ", ".join(existing) + f" already in {out}: an earlier pair's evidence is never "
                                          "overwritten; compose into a new directory")
    font = mcs._load_font(22)
    small = mcs._load_font(16)
    tiles, panels, row_images, heat_images = [], [], {}, {}
    for index in indices:
        arr = {s: read_rgb(staged[s], by[s][index], s.capitalize()) for s in TRIO_SIDES}
        if not (arr["classic"].shape == arr["cinematic"].shape == arr["film"].shape):
            raise Refusal(EXIT_TILE_SIZE, f"PAIR_TILE_SIZE_DIFFERS tile {index}: " + ", ".join(
                f"{s} {arr[s].shape[1]}x{arr[s].shape[0]}" for s in TRIO_SIDES))
        lum = {s: luma_of(arr[s]) for s in TRIO_SIDES}
        keep = ~np.logical_and.reduce([lum[s].max(axis=1) <= LETTERBOX_MAX_LUMA for s in TRIO_SIDES])
        if not keep.any():
            keep = np.ones_like(keep)
        kept = {s: arr[s][keep] for s in TRIO_SIDES}
        side = {s: side_metrics(arr[s], keep, by[s][index]) for s in TRIO_SIDES}
        for s in TRIO_SIDES:
            side[s].update(split_and_green(kept[s]))
        frame_delta = {}
        for s in ("cinematic", "film"):
            da, db = side["classic"]["display_frame"], side[s]["display_frame"]
            frame_delta[s] = (db - da) if isinstance(da, int) and isinstance(db, int) else None
        tile = {
            "index": index,
            **{s: side[s] for s in TRIO_SIDES},
            "MAD": {"cinematic_classic": mad(kept["cinematic"], kept["classic"]),
                    "film_classic": mad(kept["film"], kept["classic"]),
                    "film_cinematic": mad(kept["film"], kept["cinematic"])},
            "dS": {s: side[s]["S"] - side["classic"]["S"] for s in ("cinematic", "film")},
            "dGA": {s: side[s]["GA"] - side["classic"]["GA"] for s in ("cinematic", "film")},
            "display_frame_delta": frame_delta,
            "frame_matched": {s: frame_delta[s] is not None and abs(frame_delta[s]) <= FRAME_MATCH_TOLERANCE for s in ("cinematic", "film")},
            "rows_used": int(keep.sum()),
            "rows_excluded_letterbox": int((~keep).sum()),
            "size": [int(arr["classic"].shape[1]), int(arr["classic"].shape[0])],
        }
        tiles.append(tile)
        heat_images[index] = {
            "classic": heat_image_with_legend(np.abs(lum["film"] - lum["classic"]), small),
            "cinematic": heat_image_with_legend(np.abs(lum["film"] - lum["cinematic"]), small),
        }
        cols = [fit(arr[s], SHEET_COLUMN) for s in TRIO_SIDES]
        row = Image.new("RGB", (SHEET_WIDTH, ROW_LABEL_HEIGHT + cols[0].height), (8, 8, 8))
        for c, panel in enumerate(cols):
            row.paste(panel, (c * SHEET_COLUMN, ROW_LABEL_HEIGHT))
        row_images[index] = row
        panels.append((tile, cols))

    matched = all(all(t["frame_matched"].values()) for t in tiles)
    for t in tiles:
        d = t["display_frame_delta"]
        t["label"] = (f"tile {t['index']:02d}  classic disp {t['classic']['display_frame']}  cinematic disp {t['cinematic']['display_frame']} "
                      f"(d={d['cinematic']})  film disp {t['film']['display_frame']} (d={d['film']})")
        if not matched:
            worst = max((abs(v) for v in d.values() if v is not None), default=None)
            t["label"] += f"  NOT FRAME-MATCHED (d={worst})"
    # The None guard comes before abs(), as on the pair path: a sidecar without display_frame is unknown frame matching, not a TypeError.
    all_deltas = [t["display_frame_delta"][s] for t in tiles for s in ("cinematic", "film")]
    max_delta = max(abs(v) for v in all_deltas) if all(v is not None for v in all_deltas) else None

    tile_h = panels[0][1][0].height
    sheet = Image.new("RGB", (SHEET_WIDTH, HEADER_HEIGHT + len(panels) * (ROW_LABEL_HEIGHT + tile_h)), (8, 8, 8))
    draw = ImageDraw.Draw(sheet)
    header = [
        f"clip={args.clip_id}  venue={args.venue}  build={args.build_sha}  LEFT=Classic  MIDDLE=Cinematic  RIGHT=Film grade",
        f"flavors reported: classic={reported['classic']}  cinematic={reported['cinematic']}  film={reported['film']}  "
        f"presetGrade: classic={sliders['classic'].get('presetGrade') or 'none'}  cinematic={sliders['cinematic'].get('presetGrade') or 'none'}  "
        f"film={sliders['film'].get('presetGrade') or 'none'}",
        f"receipts: classic={args.classic_receipt_id}  cinematic={args.cinematic_receipt_id}  film={args.film_receipt_id}",
    ] + [f"sliders {s}: " + "  ".join(f"{k}={sliders[s].get(k)}" for k in SLIDER_FIELDS) for s in TRIO_SIDES]
    for i, text in enumerate(header):
        draw.text((10, 8 + i * 34), text, fill=(255, 255, 255), font=font)
    for n, (tile, cols) in enumerate(panels):
        y = HEADER_HEIGHT + n * (ROW_LABEL_HEIGHT + tile_h)
        draw.text((10, y + 10), tile["label"], fill=(255, 255, 255) if matched else (255, 120, 120), font=font)
        for c, panel in enumerate(cols):
            sheet.paste(panel.crop((0, 0, SHEET_COLUMN, tile_h)), (c * SHEET_COLUMN, y + ROW_LABEL_HEIGHT))
    sheet_path = out / TRIO_SHEET_NAME
    # FILM-TRIO-INCOMPLETE-ATTEMPT-MARKER-1: the pair's attempt marker, created first and exclusively, kept until the last output (or, with
    # --keep-marker, until the driver's trio record), so a trio that dies in between is recognisable (17) instead of a silent PAIR_OUTPUT_EXISTS.
    out.mkdir(parents=True, exist_ok=True)
    marker_path = out / MARKER_NAME
    write_new(marker_path, json.dumps({"schema": SCHEMA_MARKER, "pid": os.getpid(), "startedUtc": datetime.now(timezone.utc).isoformat(),
                                       **{f"{s}ReceiptId": getattr(args, f"{s}_receipt_id") for s in TRIO_SIDES},
                                       "keptForCaller": bool(args.keep_marker)}, indent=2).encode("utf-8"))
    try:
        write_new(sheet_path, png_bytes(sheet))
    except Refusal:
        marker_path.unlink(missing_ok=True)   # this composer lost the race for the sheet: nothing of ITS attempt may remain
        raise

    def mean_of(get):
        return float(np.mean([get(t) for t in tiles]))
    means = {
        "MAD": {k: mean_of(lambda t, k=k: t["MAD"][k]) for k in ("cinematic_classic", "film_classic", "film_cinematic")},
        "S": {s: mean_of(lambda t, s=s: t[s]["S"]) for s in TRIO_SIDES},
        "GA": {s: mean_of(lambda t, s=s: t[s]["GA"]) for s in TRIO_SIDES},
        "dS": {s: mean_of(lambda t, s=s: t["dS"][s]) for s in ("cinematic", "film")},
        "dGA": {s: mean_of(lambda t, s=s: t["dGA"][s]) for s in ("cinematic", "film")},
        "luma_p50": {s: mean_of(lambda t, s=s: t[s]["luma_p50"]) for s in TRIO_SIDES},
        "mean_saturation": {s: mean_of(lambda t, s=s: t[s]["mean_saturation"]) for s in TRIO_SIDES},
    }
    means["dSGapFilmMinusCinematic"] = means["dS"]["film"] - means["dS"]["cinematic"]
    doc = {
        "schema": SCHEMA_METRICS_TRIO,
        "clipId": args.clip_id, "venue": args.venue, "buildSha12": args.build_sha,
        "receiptIds": {s: getattr(args, f"{s}_receipt_id") for s in TRIO_SIDES},
        "lookFlavorReported": reported,
        "presetGrade": {s: sliders[s].get("presetGrade") or "none" for s in TRIO_SIDES},
        "sliders": {s: {k: sliders[s].get(k) for k in SLIDER_FIELDS} for s in TRIO_SIDES},
        "flavorOwnedFields": list(FLAVOR_OWNED_FIELDS),
        "flavorLive": live, "filmLive": flive,
        "frameMatchTolerance": FRAME_MATCH_TOLERANCE,
        "frameMatched": matched, "maxFrameDelta": max_delta,
        "letterboxMaxLuma": LETTERBOX_MAX_LUMA, "heatScaleMax": HEAT_SCALE_MAX,
        "metricDefinitions": {
            "MAD": "mean over channels of mean |X - Y|, letterbox rows excluded",
            "S": "BA(luma in [p05,p30]) - BA(luma in [p70,p95]), BA = mean(B - R), BT.601 luma ranked per image",
            "GA": "mean(G - (R+B)/2) over luma in [p30,p70]",
            "dS": "S(F) - S(classic)", "dGA": "GA(F) - GA(classic)",
        },
        "tiles": tiles, "means": means,
        "sheet": sheet_path.name,
    }
    write_new(out / "metrics.json", json.dumps(doc, indent=2).encode("utf-8"))
    write_new(out / "table.md", render_trio_table(doc).encode("utf-8"))
    for index in indices:
        label = next(t["label"] for t in tiles if t["index"] == index)
        ImageDraw.Draw(row_images[index]).text((10, 10), f"CLASSIC | CINEMATIC | FILM GRADE   {label}", fill=(255, 255, 255), font=font)
        write_new(out / f"row-{index:02d}.png", png_bytes(row_images[index]))
        write_new(out / f"heat-film-vs-classic-{index:02d}.png", png_bytes(heat_images[index]["classic"]))
        write_new(out / f"heat-film-vs-cinematic-{index:02d}.png", png_bytes(heat_images[index]["cinematic"]))
    if not args.keep_marker:
        marker_path.unlink()
    print(f"LOOK_FLAVOR_DIFF_OK sheet={sheet_path} tiles={len(tiles)} frameMatched={str(matched).lower()} maxFrameDelta={max_delta} "
          f"dSGap={means['dSGapFilmMinusCinematic']:.3f} MADfilmCinematic={means['MAD']['film_cinematic']:.3f}")
    return 0


def render_trio_table(doc):
    lines = ["### Look Assist sliders (flavor-owned fields marked *)", "", "| field | Classic | Cinematic | Film |", "|---|---|---|---|"]
    for k in SLIDER_FIELDS:
        vals = [doc["sliders"][s].get(k) for s in TRIO_SIDES]
        lines.append(f"| {k}{' *' if k in FLAVOR_OWNED_FIELDS else ''} | " + " | ".join(_num(v) for v in vals) + " |")
    lines.append("| presetGrade | " + " | ".join(doc["presetGrade"][s] for s in TRIO_SIDES) + " |")
    lines += ["", "lookFlavorReported: " + ", ".join(f"{s}={doc['lookFlavorReported'][s]}" for s in TRIO_SIDES)
              + f"; flavorLive={str(doc['flavorLive']).lower()} filmLive={str(doc['filmLive']).lower()}", "",
              f"### Per tile (letterbox rows excluded; FRAME-MATCHED = |d frame| <= {doc['frameMatchTolerance']} against Classic)", "",
              "| tile | disp cla / cin / film | d cin / film | MAD cin-cla | MAD film-cla | MAD film-cin | S cla / cin / film | dS cin / film | GA cla / cin / film | dGA cin / film |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for t in doc["tiles"]:
        d = t["display_frame_delta"]
        lines.append(f"| {t['index']:02d} | {t['classic']['display_frame']} / {t['cinematic']['display_frame']} / {t['film']['display_frame']} | "
                     f"{_num(d['cinematic'])} / {_num(d['film'])} | {t['MAD']['cinematic_classic']:.2f} | {t['MAD']['film_classic']:.2f} | "
                     f"{t['MAD']['film_cinematic']:.2f} | {t['classic']['S']:.2f} / {t['cinematic']['S']:.2f} / {t['film']['S']:.2f} | "
                     f"{t['dS']['cinematic']:.2f} / {t['dS']['film']:.2f} | {t['classic']['GA']:.2f} / {t['cinematic']['GA']:.2f} / {t['film']['GA']:.2f} | "
                     f"{t['dGA']['cinematic']:.2f} / {t['dGA']['film']:.2f} |")
    m = doc["means"]
    lines.append(f"| mean | | | {m['MAD']['cinematic_classic']:.2f} | {m['MAD']['film_classic']:.2f} | {m['MAD']['film_cinematic']:.2f} | "
                 f"{m['S']['classic']:.2f} / {m['S']['cinematic']:.2f} / {m['S']['film']:.2f} | {m['dS']['cinematic']:.2f} / {m['dS']['film']:.2f} | "
                 f"{m['GA']['classic']:.2f} / {m['GA']['cinematic']:.2f} / {m['GA']['film']:.2f} | {m['dGA']['cinematic']:.2f} / {m['dGA']['film']:.2f} |")
    lines += ["", f"dS(film) - dS(cinematic) = {m['dSGapFilmMinusCinematic']:.3f}  FRAME-MATCHED={str(doc['frameMatched']).lower()}  "
                  f"max |d frame|={doc['maxFrameDelta']}", ""]
    return "\n".join(lines)


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


# ---- Re-grade mode (LOOK-ASSIST-FILM-FLAVOR-2): Cinematic | v1 re-grade | v2 re-grade, frame-locked ----
# Reached only through `look-flavor-diff.py regrade ...`; the two-side and three-side tools above are untouched by it.
REGRADE_USAGE = """look-flavor-diff.py regrade: re-grade ONE Cinematic capture through two engine-built Film tables (film-v1, film-v2).

WHY
    Film's tone is Cinematic's; its grade is the gradation stage, the last engine stage before AgX, LUT and filter
    (src/processing/raw_processing.c, the gradation loop then the AgX inverse, apply_lut and applyFilterObject). Laying the Film tables
    over the Cinematic capture therefore gives the Film picture of the SAME frames: a v1 / v2 comparison without the CPU venue's
    frame-to-frame offsets. Valid only when the Cinematic receipt rendered with AgX, LUT and filter all off.

INPUT
    --cinematic-frames / --cinematic-listed   the staged Cinematic capture (hash-verified, as above)
    --cinematic-state     JSON object {"agx": bool, "lut": bool, "filter": bool, "source": "<where these were read>"}
    --v1-table / --v2-table   4 x 65536 uint16 little-endian, Y R G B (pipeline test LookAssistFilmGrade.DumpTablesWhenAsked)
    --v3-table            optional (LOOK-ASSIST-FILM-FLAVOR-3): a third table, film-v3; adds a v3 column and the v3 metrics (below).
                          Without it every output is byte-identical to the two-table mode.
    --film-frames / --film-listed   optional: the real Film capture, for MAD(v2(C), Film) on tiles frame-matched with Cinematic
    --clip-id --venue --build-sha --cinematic-receipt-id --film-receipt-id   header text only
    --out-dir             must contain a `.claude-state` path segment (owner footage)
    --illustrative        with an invalid state, compose anyway: valid=false in the metrics and a banner on the strip (never evidence)
    --expect-table-sha256 HEX   optional (L-04): the sha256 the NEWEST table graded must have (v3 with --v3-table, else v2), e.g. the
                          subject's FilmCurveIsPinned pin. Any other table is refused (21) before a frame is read; with a match the
                          metrics carry tableBinding and the strip names the bound table. Without it the outputs are unchanged.

RE-GRADE (per 8-bit pixel value c, per channel)
    v = T_ch[ T_Y[ round(c * 257) ] ];  out = round(v / 257)   -- Y first, then the channel's own table, as every kernel applies them.

REFUSALS: 12, 13, 14, 16 as above, and
    18  REGRADE_INVALID         the state says AgX, LUT or filter was on, or does not say all three: the re-grade is not the engine's picture.
    19  REGRADE_TABLE_INVALID   a table file is missing, unreadable or not 4 x 65536 uint16.
    21  TABLE_UNBOUND           with --expect-table-sha256: the newest table's sha256 is not the expected one (a stale candidate table
                                must never be graded under the shipped grade's name). Nothing is written.

OUTPUT (in --out-dir; created exclusively, never overwritten)
    stdout: LOOK_FLAVOR_REGRADE_OK ... ending in tableSha256=v1:<hex>,v2:<hex>[,v3:<hex>], the sha256 of every table graded.
    regrade-cinematic-v1-v2.png   3840 px wide: a header, then per tile a label band over [Cinematic | v1 re-grade | v2 re-grade] at 1280 px;
                                  every re-graded column is labelled "re-graded from the Cinematic capture".
    regrade-v1-NN.png / regrade-v2-NN.png   the full-resolution re-graded tiles.
    regrade-metrics.json          per tile S, GA (as the trio defines them) for C, v1(C), v2(C); MAD(v(C), C); dS_v = S(v(C)) - S(C); means;
                                  dSRatioV2OverV1; with --film-*: MAD(v2(C), Film) and frame matching; valid and the state.
    With --v3-table: regrade-cinematic-v1-v2-v3.png instead (four 960 px columns), regrade-v3-NN.png, the v3 entries of every per-tile
    and mean S / GA / dS / dGA / MAD, dSRatioV3OverV2, per tile and as a mean dLean = lean(v(C)) - lean(C) for v1, v2 and v3 (the warmcool
    metric), and with --film-*: MAD(v3(C), Film) too.
"""
EXIT_REGRADE_INVALID = 18
EXIT_REGRADE_TABLE = 19
EXIT_TABLE_UNBOUND = 21
REGRADE_SHEET_NAME = "regrade-cinematic-v1-v2.png"
REGRADE_V3_SHEET_NAME = "regrade-cinematic-v1-v2-v3.png"
REGRADE_V3_COLUMN = 960
REGRADE_METRICS_NAME = "regrade-metrics.json"
SCHEMA_METRICS_REGRADE = "mlv-app/look-flavor-regrade-metrics/v1"
REGRADE_TABLE_BYTES = 4 * 65536 * 2
REGRADE_SIDES = ("cinematic", "v1", "v2")


def read_regrade_table(path, name):
    """The table file's bytes, checked before any imaging import (so it refuses on every host); a typed refusal otherwise."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise Refusal(EXIT_REGRADE_TABLE, f"REGRADE_TABLE_INVALID the {name} table {path} is unreadable: {exc}") from exc
    if len(data) != REGRADE_TABLE_BYTES:
        raise Refusal(EXIT_REGRADE_TABLE, f"REGRADE_TABLE_INVALID the {name} table {path} holds {len(data)} bytes, not 4 x 65536 uint16 ({REGRADE_TABLE_BYTES})")
    return data


def regrade_tables(data):
    """The four engine tables (Y, R, G, B) as a (4, 65536) int64 array."""
    return np.frombuffer(data, dtype="<u2").reshape(4, 65536).astype(np.int64)


def regrade_state(path):
    """(valid, state, reasons) from the Cinematic render state: valid only when AgX, LUT and filter are all stated false."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise Refusal(EXIT_INPUT_INVALID, f"PAIR_INPUT_INVALID the Cinematic state file {path} is unreadable: {exc}") from exc
    if not isinstance(state, dict):
        raise Refusal(EXIT_INPUT_INVALID, "PAIR_INPUT_INVALID the Cinematic state file is not a JSON object")
    reasons = []
    for stage in ("agx", "lut", "filter"):
        if state.get(stage) is not False:
            reasons.append(f"{stage}={state.get(stage)!r} (must be false: it runs after the gradation stage)")
    return (not reasons), state, reasons


def regrade(arr, tables):
    """Y first, then each channel's own table, at index round(c * 257); back to 8 bits by round(v / 257)."""
    idx = np.rint(arr.astype(np.float64) * 257.0).astype(np.int64)
    out = np.empty(arr.shape, dtype=np.float64)
    for c in range(3):
        out[..., c] = tables[c + 1][tables[0][idx[..., c]]]
    return np.clip(np.rint(out / 257.0), 0, 255).astype(np.uint8)


def compose_regrade(args):
    require_local(args.out_dir)
    valid, state, reasons = regrade_state(args.cinematic_state)
    if not valid and not args.illustrative:
        raise Refusal(EXIT_REGRADE_INVALID, "REGRADE_INVALID " + "; ".join(reasons) + ". Nothing is written; FILM-STRONGER falls back to the trio means "
                                            "(or re-run with --illustrative for an eyeball-only strip, valid=false)")
    with_v3 = getattr(args, "v3_table", None) is not None
    versions = ("v1", "v2", "v3") if with_v3 else ("v1", "v2")
    sides = ("cinematic",) + versions
    sheet_name = REGRADE_V3_SHEET_NAME if with_v3 else REGRADE_SHEET_NAME
    column = REGRADE_V3_COLUMN if with_v3 else SHEET_COLUMN
    table_bytes = {v: read_regrade_table(getattr(args, f"{v}_table"), v) for v in versions}
    table_sha = {v: hashlib.sha256(table_bytes[v]).hexdigest() for v in versions}
    expected = getattr(args, "expect_table_sha256", None)
    if expected is not None and table_sha[versions[-1]] != expected:
        raise Refusal(EXIT_TABLE_UNBOUND, f"TABLE_UNBOUND the {versions[-1]} table {getattr(args, f'{versions[-1]}_table')} has sha256 "
                                          f"{table_sha[versions[-1]]}, not the expected {expected}: a table other than the bound one is never "
                                          "graded under its name. Nothing is written.")
    _load_imaging()
    tables = {v: regrade_tables(table_bytes[v]) for v in versions}
    staged, frames = load_side(args.cinematic_frames, args.cinematic_listed, "Cinematic")
    by = {f.get("index"): f for f in frames}
    film_staged = film_by = None
    if args.film_frames is not None:
        film_staged, film_frames = load_side(args.film_frames, args.film_listed, "Film")
        film_by = {f.get("index"): f for f in film_frames}
        if sorted(film_by) != sorted(by):
            raise Refusal(EXIT_TILE_COUNT, f"PAIR_TILE_COUNT_DIFFERS Cinematic holds tiles {sorted(by)}, Film {sorted(film_by)}")
    indices = sorted(by)
    out = Path(args.out_dir)
    names = [sheet_name, REGRADE_METRICS_NAME] + [f"regrade-{v}-{i:02d}.png" for i in indices for v in versions]
    existing = [n for n in names if (out / n).exists()]
    if existing:
        raise Refusal(EXIT_OUTPUT_EXISTS, "PAIR_OUTPUT_EXISTS " + ", ".join(existing) + f" already in {out}: earlier evidence is never overwritten; "
                                          "compose into a new directory")
    font = mcs._load_font(22)
    tiles, panels, full = [], [], {}
    for index in indices:
        c = read_rgb(staged, by[index], "Cinematic")
        refuse_empty_tile(c, index, "REGRADE_TILE_EMPTY")
        arr = {"cinematic": c, **{v: regrade(c, tables[v]) for v in versions}}
        keep = ~(luma_of(c).max(axis=1) <= LETTERBOX_MAX_LUMA)
        if not keep.any():
            keep = np.ones_like(keep)
        kept = {s: arr[s][keep] for s in sides}
        sg = {s: split_and_green(kept[s]) for s in sides}
        tile = {
            "index": index,
            "display_frame": by[index].get("display_frame"),
            **{s: sg[s] for s in sides},
            "MAD": {v: mad(kept[v], kept["cinematic"]) for v in versions},
            "dS": {v: sg[v]["S"] - sg["cinematic"]["S"] for v in versions},
            "dGA": {v: sg[v]["GA"] - sg["cinematic"]["GA"] for v in versions},
            "rows_used": int(keep.sum()),
            "size": [int(c.shape[1]), int(c.shape[0])],
        }
        if with_v3:
            base_lean = warmcool_lean(kept["cinematic"], 255)
            tile["dLean"] = {v: warmcool_lean(kept[v], 255) - base_lean for v in versions}
        if film_by is not None:
            f = read_rgb(film_staged, film_by[index], "Film")
            if f.shape != c.shape:
                raise Refusal(EXIT_TILE_SIZE, f"PAIR_TILE_SIZE_DIFFERS tile {index}: Cinematic {c.shape[1]}x{c.shape[0]}, Film {f.shape[1]}x{f.shape[0]}")
            df, dc = film_by[index].get("display_frame"), tile["display_frame"]
            delta = (df - dc) if isinstance(df, int) and isinstance(dc, int) else None
            tile["film"] = {"display_frame": df, "display_frame_delta": delta,
                            "frame_matched": delta is not None and abs(delta) <= FRAME_MATCH_TOLERANCE,
                            "MAD_v2_vs_film": mad(kept["v2"], f[keep])}
            if with_v3:
                tile["film"]["MAD_v3_vs_film"] = mad(kept["v3"], f[keep])
        tiles.append(tile)
        full[index] = arr
        panels.append((tile, [fit(arr[s], column) for s in sides]))

    banner = "" if valid else "  REGRADE-INVALID (" + "; ".join(reasons) + "): ILLUSTRATIVE ONLY, NOT EVIDENCE"
    column_labels = ("CINEMATIC (the Cinematic capture)", "FILM-V1 re-graded from the Cinematic capture", "FILM-V2 re-graded from the Cinematic capture")
    if with_v3:
        column_labels = ("CINEMATIC (capture)", "FILM-V1 re-graded", "FILM-V2 re-graded", "FILM-V3 re-graded")
    layout = ("LEFT=Cinematic  then v1, v2, v3 re-grades" if with_v3 else "LEFT=Cinematic  MIDDLE=v1 re-grade  RIGHT=v2 re-grade")
    tile_h = panels[0][1][0].height
    sheet = Image.new("RGB", (SHEET_WIDTH, HEADER_HEIGHT + len(panels) * (ROW_LABEL_HEIGHT + tile_h)), (8, 8, 8))
    draw = ImageDraw.Draw(sheet)
    header = [
        f"clip={args.clip_id}  venue={args.venue}  build={args.build_sha}  {layout}{banner}",
        f"receipts: cinematic={args.cinematic_receipt_id}  film={args.film_receipt_id}  state: agx={state.get('agx')} lut={state.get('lut')} "
        f"filter={state.get('filter')} ({state.get('source', '')})",
        "frame-locked: every column is the SAME captured frame; the v1 / v2 columns are re-graded from the Cinematic capture "
        "(Y then R/G/B tables, round(c*257), /257, round)" if not with_v3 else
        "frame-locked: every column is the SAME captured frame; the v1 / v2 / v3 columns are re-graded from the Cinematic capture "
        "(Y then R/G/B tables, round(c*257), /257, round)",
        "  |  ".join(column_labels),
    ]
    if expected is not None:
        header.append(f"table bound: {versions[-1]} sha256={expected} (--expect-table-sha256)")
    for i, text in enumerate(header):
        draw.text((10, 8 + i * 34), text, fill=(255, 255, 255) if valid else (255, 120, 120), font=font)
    for n, (tile, cols) in enumerate(panels):
        y = HEADER_HEIGHT + n * (ROW_LABEL_HEIGHT + tile_h)
        for col, panel in enumerate(cols):
            sheet.paste(panel.crop((0, 0, column, tile_h)), (col * column, y + ROW_LABEL_HEIGHT))
            draw.text((col * column + 10, y + 10), f"tile {tile['index']:02d} disp {tile['display_frame']}  {column_labels[col]}",
                      fill=(255, 255, 255), font=font)

    def mean_of(get):
        return float(np.mean([get(t) for t in tiles]))
    means = {
        "S": {s: mean_of(lambda t, s=s: t[s]["S"]) for s in sides},
        "GA": {s: mean_of(lambda t, s=s: t[s]["GA"]) for s in sides},
        "dS": {v: mean_of(lambda t, v=v: t["dS"][v]) for v in versions},
        "dGA": {v: mean_of(lambda t, v=v: t["dGA"][v]) for v in versions},
        "MAD": {v: mean_of(lambda t, v=v: t["MAD"][v]) for v in versions},
    }
    if with_v3:
        means["dLean"] = {v: mean_of(lambda t, v=v: t["dLean"][v]) for v in versions}
    ratio = means["dS"]["v2"] / means["dS"]["v1"] if means["dS"]["v1"] != 0 else None
    doc = {
        "schema": SCHEMA_METRICS_REGRADE,
        "clipId": args.clip_id, "venue": args.venue, "buildSha12": args.build_sha,
        "receiptIds": {"cinematic": args.cinematic_receipt_id, "film": args.film_receipt_id},
        "valid": valid, "invalidReasons": reasons, "state": state,
        "tables": {v: {"path": str(getattr(args, f"{v}_table")), "sha256": table_sha[v]} for v in versions},
        "letterboxMaxLuma": LETTERBOX_MAX_LUMA, "frameMatchTolerance": FRAME_MATCH_TOLERANCE,
        "metricDefinitions": {
            "S": "BA(luma in [p05,p30]) - BA(luma in [p70,p95]), BA = mean(B - R), BT.601 luma ranked per image",
            "GA": "mean(G - (R+B)/2) over luma in [p30,p70]",
            "dS": "S(v(C)) - S(C) = dS(v(C)) - dS(C) for any reference", "dGA": "GA(v(C)) - GA(C)",
            "MAD": "mean over channels of mean |v(C) - C|", "MAD_v2_vs_film": "mean over channels of mean |v2(C) - Film|",
        },
        "tiles": tiles, "means": means, "dSRatioV2OverV1": ratio,
        "sheet": sheet_name,
    }
    ratio3 = None
    if with_v3:
        ratio3 = means["dS"]["v3"] / means["dS"]["v2"] if means["dS"]["v2"] != 0 else None
        doc["dSRatioV3OverV2"] = ratio3
        doc["metricDefinitions"]["MAD_v3_vs_film"] = "mean over channels of mean |v3(C) - Film|"
        doc["metricDefinitions"]["dLean"] = "lean(v(C)) - lean(C), lean = " + WARMCOOL_DEFINITION + " (the warmcool metric)"
    if expected is not None:
        doc["tableBinding"] = {"table": versions[-1], "expectedSha256": expected, "bound": True}
    metrics_bytes = finite_json(doc, "REGRADE_NOT_FINITE").encode("utf-8")
    out.mkdir(parents=True, exist_ok=True)
    write_new(out / sheet_name, png_bytes(sheet))
    for index in indices:
        for v in versions:
            write_new(out / f"regrade-{v}-{index:02d}.png", png_bytes(Image.fromarray(full[index][v])))
    write_new(out / REGRADE_METRICS_NAME, metrics_bytes)
    v3_tail = (f" dSv3={means['dS']['v3']:.3f} ratioV3OverV2={'None' if ratio3 is None else f'{ratio3:.3f}'}" if with_v3 else "")
    print(f"LOOK_FLAVOR_REGRADE_OK sheet={out / sheet_name} tiles={len(tiles)} valid={str(valid).lower()} "
          f"dSv1={means['dS']['v1']:.3f} dSv2={means['dS']['v2']:.3f} ratio={'None' if ratio is None else f'{ratio:.3f}'}{v3_tail}"
          f" tableSha256={','.join(f'{v}:{table_sha[v]}' for v in versions)}")
    return 0


def regrade_main(argv):
    p = argparse.ArgumentParser(prog="look-flavor-diff.py regrade", description=REGRADE_USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cinematic-frames", required=True, type=Path)
    p.add_argument("--cinematic-listed", required=True, type=Path)
    p.add_argument("--cinematic-state", required=True, type=Path)
    p.add_argument("--v1-table", required=True, type=Path)
    p.add_argument("--v2-table", required=True, type=Path)
    p.add_argument("--v3-table", type=Path, default=None)
    p.add_argument("--film-frames", type=Path, default=None)
    p.add_argument("--film-listed", type=Path, default=None)
    p.add_argument("--cinematic-receipt-id", default="")
    p.add_argument("--film-receipt-id", default="")
    p.add_argument("--clip-id", default="")
    p.add_argument("--venue", default="")
    p.add_argument("--build-sha", default="")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--illustrative", action="store_true")
    p.add_argument("--expect-table-sha256", default=None, metavar="HEX")
    args = p.parse_args(argv)
    if (args.film_frames is None) != (args.film_listed is None):
        p.error("the Film side needs both --film-frames and --film-listed, or neither")
    if args.expect_table_sha256 is not None:
        if not re.fullmatch(r"[0-9a-fA-F]{64}", args.expect_table_sha256):
            p.error("--expect-table-sha256 wants a sha256: 64 hex digits")
        args.expect_table_sha256 = args.expect_table_sha256.lower()
    try:
        return compose_regrade(args)
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


# ---- Warm-cool mode (LOOK-ASSIST-WARMTH-MEASURE-1): the blue-amber lean of grade tables and captures ----
# Reached only through `look-flavor-diff.py warmcool ...`; it writes nothing, so every other mode's outputs are untouched by it.
WARMCOOL_USAGE = """look-flavor-diff.py warmcool: the blue-amber (warm-cool) lean of engine grade tables and, optionally, of one staged capture.

WHY
    S and GA (the trio and regrade metrics) are a per-band split and a green axis; neither says whether a look is warm or cool overall.
    This is that number, defined once and used the same way for a table, a capture and a re-grade.

METRIC
    lean = mean over pixels of ((R + G) / 2 - B), code values normalized to [0, 1] (8-bit / 255, table / 65535).
    Positive = amber (warm), negative = blue (cool), exactly 0 on any neutral grey. A grade's lean is lean(graded) - lean(ungraded).
    This is NOT the probe scripts' warmCool = R - B in code values: the lean averages R and G against B and divides by the full scale, so a
    pure R - B offset of 2 codes is a lean of 1.5 / 255, not 2. Compare the two only after converting.

INPUT
    --table NAME=PATH     repeatable: 4 x 65536 uint16 little-endian, Y R G B (the regrade mode's tables)
    --frames / --listed   optional: one staged capture (hash-verified, as above)

OUTPUT (stdout, JSON; nothing is written)
    per table:    rampLean = the lean of the neutral ramp R = G = B = i / 65535 over every 16-bit code, through Y then the channel table.
    with a capture, per table: histogramLean = that neutral lean weighted by the capture's BT.601 luma histogram (8-bit level c read at
                  index c * 257, as the re-grade reads it);
    and per tile and as a mean over tiles: the capture's own lean, the lean of the capture re-graded through each table (as the regrade
                  mode re-grades), and dLean = the re-graded lean minus the capture's. Letterbox rows are excluded, as everywhere here.

REFUSALS: 12, 14 as above, and 19 REGRADE_TABLE_INVALID (a table is missing, unreadable or not 4 x 65536 uint16). Exit 14 also names two
    capture refusals, each with nothing printed: `WARMCOOL_TILE_EMPTY <index> <w>x<h>` (a staged tile with no pixels) and
    `WARMCOOL_SIDECAR_MALFORMED <path>` (a sidecar that is unreadable, not JSON or not a JSON object; the contact-sheet tool warns and skips
    such a sidecar, but a measurement over the rest of a capture would pass for the whole one). A sidecar that says saved=false is still skipped.

GUARD (LOOK-ASSIST-FILM-FLAVOR-3)
    --max-abs-lean X      after printing the JSON, exit 20 WARMCOOL_BOUND_EXCEEDED (one stderr line per breach:
                          `WARMCOOL_BOUND_EXCEEDED <name> <rampLean|histogramLean|meanDLean> <value>`) when |rampLean|, |histogramLean| or
                          |meanDLean| of any table exceeds X, either way (amber or blue). The neutral bound film-v3 was held to is
                          WC_NEUTRAL_BOUND = 1.5 / 255: the app's white-balance search calls a surface neutral at |B - R| < 2 codes, and a pure
                          R - B offset of 2 is a lean of 1.5 codes. Without --max-abs-lean the output and exit code are unchanged.
"""
SCHEMA_WARMCOOL = "mlv-app/look-flavor-warmcool/v1"
WARMCOOL_DEFINITION = "mean((R + G) / 2 - B) on code values normalized to [0, 1]; positive = amber, negative = blue, 0 on neutral grey"
EXIT_WARMCOOL_BOUND = 20
WC_NEUTRAL_BOUND = 1.5 / 255


def warmcool_lean(pixels, full_scale):
    """The blue-amber lean of an (..., 3) array of code values on a 0..full_scale scale."""
    f = np.asarray(pixels, dtype=np.float64).reshape(-1, 3) / float(full_scale)
    return float(((f[:, 0] + f[:, 1]) / 2.0 - f[:, 2]).mean())


def warmcool_neutral_curve(tables):
    """Per 16-bit code i, the lean of the neutral pixel R = G = B = i after the grade (Y first, then each channel's table)."""
    y = tables[0]
    return ((tables[1][y] + tables[2][y]) / 2.0 - tables[3][y]) / 65535.0


def compose_warmcool(named, frames_dir, listed):
    table_bytes = {name: read_regrade_table(path, name) for name, path in named}
    _load_imaging()
    tables = {name: regrade_tables(data) for name, data in table_bytes.items()}
    curves = {name: warmcool_neutral_curve(t) for name, t in tables.items()}
    doc = {
        "schema": SCHEMA_WARMCOOL, "definition": WARMCOOL_DEFINITION, "letterboxMaxLuma": LETTERBOX_MAX_LUMA,
        "tables": {name: {"path": str(path), "sha256": hashlib.sha256(table_bytes[name]).hexdigest(), "rampLean": float(curves[name].mean())}
                   for name, path in named},
    }
    if frames_dir is None:
        return doc
    staged, frames = load_side(frames_dir, listed, "capture", strict_sidecars=True)
    hist = np.zeros(256, dtype=np.float64)
    tiles = []
    for sidecar in sorted(frames, key=lambda f: f.get("index")):
        arr = read_rgb(staged, sidecar, "capture")
        refuse_empty_tile(arr, sidecar.get("index"), "WARMCOOL_TILE_EMPTY")
        keep = ~(luma_of(arr).max(axis=1) <= LETTERBOX_MAX_LUMA)
        if not keep.any():
            keep = np.ones_like(keep)
        kept = arr[keep]
        hist += np.bincount(np.clip(np.rint(luma_of(kept)), 0, 255).astype(np.int64).ravel(), minlength=256)
        lean = warmcool_lean(kept, 255)
        graded = {name: warmcool_lean(regrade(kept, t), 255) for name, t in tables.items()}
        tiles.append({"index": sidecar.get("index"), "display_frame": sidecar.get("display_frame"), "rows_used": int(keep.sum()),
                      "lean": lean, "regradedLean": graded, "dLean": {name: v - lean for name, v in graded.items()}})
    level = np.arange(256) * 257
    for name in tables:
        doc["tables"][name]["histogramLean"] = float((hist / hist.sum() * curves[name][level]).sum())
    doc["capture"] = {
        "frames": str(frames_dir), "tiles": tiles,
        "meanLean": float(np.mean([t["lean"] for t in tiles])),
        "meanRegradedLean": {name: float(np.mean([t["regradedLean"][name] for t in tiles])) for name in tables},
        "meanDLean": {name: float(np.mean([t["dLean"][name] for t in tiles])) for name in tables},
    }
    return doc


def warmcool_breaches(doc, bound):
    """(name, which, value) for every table lean whose magnitude exceeds `bound`, in table order: rampLean, histogramLean, meanDLean."""
    breaches = []
    dlean = doc.get("capture", {}).get("meanDLean", {})
    for name, t in doc["tables"].items():
        for which, value in (("rampLean", t.get("rampLean")), ("histogramLean", t.get("histogramLean")), ("meanDLean", dlean.get(name))):
            if value is not None and abs(value) > bound:
                breaches.append((name, which, value))
    return breaches


def warmcool_main(argv):
    p = argparse.ArgumentParser(prog="look-flavor-diff.py warmcool", description=WARMCOOL_USAGE, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--table", action="append", default=[], metavar="NAME=PATH")
    p.add_argument("--frames", type=Path, default=None)
    p.add_argument("--listed", type=Path, default=None)
    p.add_argument("--max-abs-lean", type=float, default=None, metavar="X")
    args = p.parse_args(argv)
    if (args.frames is None) != (args.listed is None):
        p.error("a capture needs both --frames and --listed, or neither")
    if args.max_abs_lean is not None and not args.max_abs_lean >= 0:
        p.error("--max-abs-lean wants a non-negative number")
    named = []
    for spec in args.table:
        name, sep, path = spec.partition("=")
        if not sep or not name or not path:
            p.error(f"--table wants NAME=PATH, got {spec!r}")
        named.append((name, Path(path)))
    if not named and args.frames is None:
        p.error("give at least one --table, or a capture")
    if len({name for name, _ in named}) != len(named):
        p.error("--table names must be unique")
    try:
        doc = compose_warmcool(named, args.frames, args.listed)
        print(finite_json(doc, "WARMCOOL_NOT_FINITE"))
        if args.max_abs_lean is None:
            return 0
        breaches = warmcool_breaches(doc, args.max_abs_lean)
        for name, which, value in breaches:
            print(f"WARMCOOL_BOUND_EXCEEDED {name} {which} {value:+.6f} (|{which}| > {args.max_abs_lean:.6f})", file=sys.stderr)
        return EXIT_WARMCOOL_BOUND if breaches else 0
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "regrade":
        return regrade_main(argv[1:])
    if argv and argv[0] == "warmcool":
        return warmcool_main(argv[1:])
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for side in ("classic", "cinematic"):
        p.add_argument(f"--{side}-frames", required=True, type=Path)
        p.add_argument(f"--{side}-listed", required=True, type=Path)
        p.add_argument(f"--{side}-sliders", required=True, type=Path)
        p.add_argument(f"--{side}-flavor-reported", required=True)
        p.add_argument(f"--{side}-receipt-id", default="")
    # Three-side mode (LOOK-ASSIST-FILM-FLAVOR-1): all four --film-* inputs, or none.
    p.add_argument("--film-frames", type=Path, default=None)
    p.add_argument("--film-listed", type=Path, default=None)
    p.add_argument("--film-sliders", type=Path, default=None)
    p.add_argument("--film-flavor-reported", default=None)
    p.add_argument("--film-receipt-id", default="")
    p.add_argument("--clip-id", default="")
    p.add_argument("--venue", default="")
    p.add_argument("--build-sha", default="")
    p.add_argument("--out-dir", required=True, type=Path)
    p.add_argument("--keep-marker", action="store_true", help="leave .pair-in-progress.json for the caller to remove after its own record")
    p.add_argument("--recover-incomplete", action="store_true",
                   help="move an earlier unfinished attempt's marker and unrecorded outputs into incomplete-<utc>/ and compose again")
    args = p.parse_args(argv)
    film_inputs = [args.film_frames, args.film_listed, args.film_sliders, args.film_flavor_reported]
    if any(v is not None for v in film_inputs) and not all(v is not None for v in film_inputs):
        p.error("three-side mode needs all of --film-frames, --film-listed, --film-sliders and --film-flavor-reported")
    try:
        if all(v is not None for v in film_inputs):
            return compose_trio(args)
        return compose(args)
    except Refusal as exc:
        print(str(exc), file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
