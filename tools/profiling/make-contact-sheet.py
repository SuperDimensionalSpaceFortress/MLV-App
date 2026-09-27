#!/usr/bin/env python3
"""Compose a CUDA-playback contact sheet from a --contact-sheet-dir capture.

WHY THIS EXISTS (CUDA-PLAYBACK-CONTACT-SHEET-1)
    The GUI smoke's --contact-sheet-dir/--contact-sheet-frames option grabs N evenly
    spaced presented frames (PNG + JSON sidecar each -- see MainWindow.cpp's contact-sheet
    capture pass in runGuiPlaybackSmoke) so the owner can eyeball what Look Assist actually
    did to CUDA playback, round over round. This script turns that raw per-frame capture
    into ONE labelled grid image two runs (builds / hosts / look states) can be compared
    side by side, plus a stats sidecar so a look change can be judged by numbers too, not
    only by eye.

INPUT
    A directory containing frame-NN.png + frame-NN.json pairs, written by the app's
    contact-sheet capture pass. Each sidecar carries (at least): index, captured_utc,
    serial, display_frame, elapsed_ms, texture_source, render_path, playback_path, path,
    look_assist_enabled, look_assist_scene, look_assist_exposure/contrast/pivot/
    temperature/tint/vibrance/shadows/highlights, settled, saved. Frames with saved=false
    are skipped (a tile that failed to capture must never render as if it succeeded).
    playback_path=false marks a frame captured by the seek-based alternative mode (never
    the default) -- rendered by a different, non-playback path than the one being audited.

OUTPUT
    --sheet-out: one grid PNG (default 4 columns) with a header strip (host, GPU, build
    sha, backend, scale, Look Assist on/off + scene + ALL applied values, the majority
    render path across the sheet's tiles, clip id -- id only, never a path -- and capture
    time), a WARNING line if any tile's playback_path is false, and a per-tile label
    (frame index + elapsed_ms). Downscaled, and re-downscaled if needed, to stay under
    --max-bytes (default 4 MB).
    --stats-out: a JSON sidecar with per-tile luma p1/p50/p99, mean saturation, clipped-
    highlight % and crushed-black % -- computed on the FULL-resolution source PNG, before
    the sheet's own thumbnail downscale, so the numbers describe the captured frame, not
    a thumbnail artifact.

USAGE
    python tools/profiling/make-contact-sheet.py --frames-dir <dir> \
        --sheet-out <sheet.png> --stats-out <stats.json> \
        [--cols 4] [--clip-id ID] [--host HOST] [--gpu GPU] [--build-sha SHA] \
        [--backend BACKEND] [--scale SCALE] [--max-bytes 4000000]

    Requires Pillow + numpy (already a tools/profiling dependency; see
    frame_colour_spatial_metrics.py). Exits non-zero only on a structural failure (no
    frames dir, zero usable frames, or a write failure) -- it does not grade the look.
"""
import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

SCHEMA_STATS = "contact-sheet-stats.v1"


def _relative_or_name(path: Path, base: Path) -> str:
    """H3: never an absolute local path in a published sidecar -- relative to the
    artifacts dir (here, the stats sidecar's own directory) when possible, falling back to
    just the basename when the two are on different drives (os.path.relpath raises)."""
    try:
        return os.path.relpath(str(path), str(base)).replace(os.sep, "/")
    except ValueError:
        return path.name

HEADER_HEIGHT = 150
TILE_LABEL_HEIGHT = 22
TILE_PADDING = 6
TILE_TARGET_WIDTH = 480


def _load_font(size):
    # No bundled font ships with this repo's tooling, and a measurement host (e.g.
    # Bachelor) is not guaranteed to have one installed either -- try a couple of common
    # system locations, fall back to Pillow's built-in bitmap font rather than fail.
    candidates = [
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for candidate in candidates:
        try:
            if Path(candidate).exists():
                return ImageFont.truetype(candidate, size)
        except Exception:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        # Older Pillow: load_default() takes no size argument.
        return ImageFont.load_default()


def _resolve_frame_image_path(sidecar_path, frames_dir):
    # HARDENING (r1d, sol pre-review #2): the sidecar's own "path" field is relative to
    # frames_dir (the directory the sidecar itself sits in -- see the app's own
    # noteContactSheetPresentedFrame, which writes it relative for exactly this reason), never
    # to whatever directory happens to be the composer's own current working directory.
    # Resolving a relative path bare (against cwd) could silently pick up an unrelated
    # same-named file sitting there instead of the real capture -- resolve against frames_dir
    # first, and only try the path as-is when it is already absolute.
    raw_path = sidecar_path.get("path", "")
    if raw_path:
        candidate = Path(raw_path)
        in_frames_dir = candidate if candidate.is_absolute() else (frames_dir / candidate)
        if in_frames_dir.is_file():
            return in_frames_dir
    fallback = frames_dir / (sidecar_path.get("_stem", "") + ".png")
    if fallback.is_file():
        return fallback
    return None


def load_frames(frames_dir):
    """Return sidecar dicts (with '_stem' added) sorted by index, saved=true only."""
    frames = []
    for json_path in sorted(frames_dir.glob("frame-*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[make-contact-sheet] WARNING: failed to read {json_path}: {exc}", file=sys.stderr)
            continue
        if not data.get("saved", False):
            continue
        data["_stem"] = json_path.stem
        frames.append(data)
    frames.sort(key=lambda d: d.get("index", 0))
    return frames


def channel_stats(image):
    """Per-tile numeric stats computed on the full-resolution RGB source."""
    arr = np.asarray(image.convert("RGB")).astype(np.float64)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    luma = 0.299 * r + 0.587 * g + 0.114 * b
    channel_max = np.maximum(np.maximum(r, g), b)
    channel_min = np.minimum(np.minimum(r, g), b)
    # Saturation as in HSV: 0 where the pixel is pure black (max == 0), avoiding a
    # divide-by-zero rather than special-casing it after the fact.
    with np.errstate(divide="ignore", invalid="ignore"):
        saturation = np.where(channel_max > 0, (channel_max - channel_min) / channel_max, 0.0)
    total_pixels = luma.size
    return {
        "luma_p1": float(np.percentile(luma, 1)),
        "luma_p50": float(np.percentile(luma, 50)),
        "luma_p99": float(np.percentile(luma, 99)),
        "mean_saturation": float(saturation.mean()),
        "clipped_highlight_pct": float(100.0 * np.count_nonzero(luma >= 254.0) / total_pixels),
        "crushed_black_pct": float(100.0 * np.count_nonzero(luma <= 1.0) / total_pixels),
    }


def build_tile(image, sidecar, tile_size, font):
    thumb = image.convert("RGB").copy()
    thumb.thumbnail(tile_size, Image.LANCZOS)
    tile = Image.new("RGB", tile_size, (24, 24, 24))
    offset = ((tile_size[0] - thumb.width) // 2, (tile_size[1] - thumb.height - TILE_LABEL_HEIGHT) // 2)
    tile.paste(thumb, offset)

    draw = ImageDraw.Draw(tile)
    label = "frame {index} @ {elapsed_ms:.0f}ms (disp {display_frame})".format(
        index=sidecar.get("index", -1),
        elapsed_ms=float(sidecar.get("elapsed_ms", 0.0)),
        display_frame=sidecar.get("display_frame", -1),
    )
    label_y = tile_size[1] - TILE_LABEL_HEIGHT + 4
    draw.rectangle([0, tile_size[1] - TILE_LABEL_HEIGHT, tile_size[0], tile_size[1]], fill=(0, 0, 0))
    draw.text((4, label_y), label, fill=(255, 255, 255), font=font)
    return tile


def _majority_render_path(frames):
    """The render_path value shared by the most tiles, or "unknown" if none report one."""
    counts = Counter(f.get("render_path") for f in frames if f.get("render_path"))
    if not counts:
        return "unknown"
    return counts.most_common(1)[0][0]


def build_header_lines(args, frames):
    """Pure text form of the header strip -- kept separate from build_header (which only
    draws it) so its content is assertable without rendering an image."""
    look_assist_on = any(f.get("look_assist_enabled") for f in frames)
    scene = next((f.get("look_assist_scene") for f in frames if f.get("look_assist_scene")), "")
    capture_times = sorted(f.get("captured_utc", "") for f in frames if f.get("captured_utc"))
    capture_time = capture_times[0] if capture_times else "unknown"
    sample = frames[0] if frames else {}

    look_assist_summary = "off"
    if look_assist_on:
        look_assist_summary = (
            "on scene={scene} exposure={exposure} contrast={contrast} pivot={pivot} "
            "shadows={shadows} highlights={highlights} vibrance={vibrance} "
            "temperature={temperature} tint={tint}"
        ).format(
            scene=scene or "unknown",
            exposure=sample.get("look_assist_exposure", "?"),
            contrast=sample.get("look_assist_contrast", "?"),
            pivot=sample.get("look_assist_pivot", "?"),
            shadows=sample.get("look_assist_shadows", "?"),
            highlights=sample.get("look_assist_highlights", "?"),
            vibrance=sample.get("look_assist_vibrance", "?"),
            temperature=sample.get("look_assist_temperature", "?"),
            tint=sample.get("look_assist_tint", "?"),
        )

    non_playback_count = sum(1 for f in frames if f.get("playback_path") is False)

    lines = [
        "clip={clip_id}  host={host}  gpu={gpu}  build={build_sha}  backend={backend}  scale={scale}".format(
            clip_id=args.clip_id or "unknown",
            host=args.host or "unknown",
            gpu=args.gpu or "unknown",
            build_sha=args.build_sha or "unknown",
            backend=args.backend or "unknown",
            scale=args.scale or "unknown",
        ),
        f"look_assist: {look_assist_summary}",
        f"render_path(majority)={_majority_render_path(frames)}",
        f"captured_utc={capture_time}  frames={len(frames)}",
    ]
    if non_playback_count:
        # Finding #1 (hub, r1b): a grabbed frame that did NOT come from the playback path
        # must be flagged on the sheet, never silently blended in with genuine playback
        # captures -- it may show a different look than the one being audited.
        lines.append(
            f"WARNING: {non_playback_count}/{len(frames)} frame(s) NOT captured via the playback path"
        )
    return lines


def build_header(width, args, frames):
    header = Image.new("RGB", (width, HEADER_HEIGHT), (16, 16, 16))
    draw = ImageDraw.Draw(header)
    font = _load_font(14)
    y = 8
    for line in build_header_lines(args, frames):
        draw.text((10, y), line, fill=(255, 255, 255), font=font)
        y += 28
    return header


def compose_sheet(frames, frames_dir, args):
    if not frames:
        raise ValueError("no usable (saved=true) frames found in frames dir")

    cols = max(1, args.cols)
    rows = (len(frames) + cols - 1) // cols

    first_image_path = _resolve_frame_image_path(frames[0], frames_dir)
    if first_image_path is None:
        raise ValueError(f"could not resolve image path for frame index {frames[0].get('index')}")
    with Image.open(first_image_path) as probe:
        aspect = probe.height / probe.width if probe.width else 1.0
    tile_size = (TILE_TARGET_WIDTH, int(TILE_TARGET_WIDTH * aspect) + TILE_LABEL_HEIGHT)

    font = _load_font(13)
    tile_stats = []
    tiles = []
    for sidecar in frames:
        image_path = _resolve_frame_image_path(sidecar, frames_dir)
        if image_path is None:
            print(
                f"[make-contact-sheet] WARNING: skipping frame {sidecar.get('index')}: "
                f"image not found at {sidecar.get('path')}",
                file=sys.stderr,
            )
            continue
        with Image.open(image_path) as full_res:
            stats = channel_stats(full_res)
            stats["index"] = sidecar.get("index")
            stats["display_frame"] = sidecar.get("display_frame")
            stats["elapsed_ms"] = sidecar.get("elapsed_ms")
            tile_stats.append(stats)
            tiles.append(build_tile(full_res, sidecar, tile_size, font))

    if not tiles:
        raise ValueError("every sidecar's image failed to resolve; nothing to compose")

    grid_width = cols * (tile_size[0] + TILE_PADDING) + TILE_PADDING
    grid_height = rows * (tile_size[1] + TILE_PADDING) + TILE_PADDING
    sheet = Image.new("RGB", (grid_width, HEADER_HEIGHT + grid_height), (8, 8, 8))
    header = build_header(grid_width, args, frames)
    sheet.paste(header, (0, 0))

    for i, tile in enumerate(tiles):
        col = i % cols
        row = i // cols
        x = TILE_PADDING + col * (tile_size[0] + TILE_PADDING)
        y = HEADER_HEIGHT + TILE_PADDING + row * (tile_size[1] + TILE_PADDING)
        sheet.paste(tile, (x, y))

    return sheet, tile_stats


def save_under_budget(sheet, out_path, max_bytes):
    current = sheet
    for _ in range(6):
        current.save(out_path, "PNG", optimize=True)
        if out_path.stat().st_size <= max_bytes:
            return
        new_size = (int(current.width * 0.85), int(current.height * 0.85))
        if new_size[0] < 200 or new_size[1] < 200:
            break
        current = current.resize(new_size, Image.LANCZOS)
    # Last attempt already written above even if still over budget: a slightly
    # oversized sheet is more useful than none, and the caller can see the actual
    # byte count in the result if it matters.


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--frames-dir", required=True, type=Path)
    parser.add_argument("--sheet-out", required=True, type=Path)
    parser.add_argument("--stats-out", required=True, type=Path)
    parser.add_argument("--cols", type=int, default=4)
    parser.add_argument("--clip-id", default="")
    parser.add_argument("--host", default="")
    parser.add_argument("--gpu", default="")
    parser.add_argument("--build-sha", default="")
    parser.add_argument("--backend", default="")
    parser.add_argument("--scale", default="")
    parser.add_argument("--max-bytes", type=int, default=4_000_000)
    args = parser.parse_args()

    if not args.frames_dir.is_dir():
        print(f"[make-contact-sheet] ERROR: frames dir does not exist: {args.frames_dir}", file=sys.stderr)
        return 2

    frames = load_frames(args.frames_dir)
    try:
        sheet, tile_stats = compose_sheet(frames, args.frames_dir, args)
    except ValueError as exc:
        print(f"[make-contact-sheet] ERROR: {exc}", file=sys.stderr)
        return 3

    args.sheet_out.parent.mkdir(parents=True, exist_ok=True)
    save_under_budget(sheet, args.sheet_out, args.max_bytes)

    args.stats_out.parent.mkdir(parents=True, exist_ok=True)
    stats_doc = {
        "schema": SCHEMA_STATS,
        "sheet_path": _relative_or_name(args.sheet_out, args.stats_out.parent),
        "frames_dir": _relative_or_name(args.frames_dir, args.stats_out.parent),
        "clip_id": args.clip_id,
        "host": args.host,
        "gpu": args.gpu,
        "build_sha": args.build_sha,
        "backend": args.backend,
        "scale": args.scale,
        "tile_count": len(tile_stats),
        "sheet_bytes": args.sheet_out.stat().st_size,
        "tiles": tile_stats,
    }
    args.stats_out.write_text(json.dumps(stats_doc, indent=2), encoding="utf-8")

    print(
        f"[make-contact-sheet] OK sheet={args.sheet_out} stats={args.stats_out} "
        f"tiles={len(tile_stats)} sheet_bytes={stats_doc['sheet_bytes']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
