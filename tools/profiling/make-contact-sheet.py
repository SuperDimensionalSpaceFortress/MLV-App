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

    SIDE-BY-SIDE MODE (DUAL-VENUE-EVIDENCE-1, AMENDMENT 1 A2): add --pair-dir <dir> to pair
    the --frames-dir capture (the LEFT side, default label "cuda") with a second capture (the
    RIGHT side, default label "cpu") BY FRAME INDEX: one row per index, left tile | mid-grey
    gutter | right tile. An index present on only one side is rendered with an UNPAIRED
    placeholder and named in the header -- never silently dropped. The stats sidecar carries
    both sides' per-tile numbers; it does not grade the look (look metrics are a separate card).
    --left-label/--right-label name the sides; --right-host/--right-gpu/--right-build-sha
    describe the right side (the plain --host/--gpu/--build-sha describe the left).
    Each side reads ONLY the plain files of its own directory (DUAL-VENUE-EVIDENCE-3): a sidecar whose "path" is
    anything but its own <stem>.png (absolute, parent-relative, nested, another frame's name) is refused with
    PAIR_SIDECAR_PATH_OUTSIDE_STAGING, and nothing falls back to a file outside the directory. --left-listed /
    --right-listed (both or neither) name the hashed files each side may hold: an unlisted file is refused
    (PAIR_FRAME_NOT_LISTED) and every file read is verified against its sha256 AT READ TIME (PAIR_FRAME_HASH_MISMATCH).

    Requires Pillow + numpy (already a tools/profiling dependency; see
    frame_colour_spatial_metrics.py). Exits non-zero only on a structural failure (no
    frames dir, zero usable frames, or a write failure) -- it does not grade the look.
"""
import argparse
import hashlib
import io
import json
import os
import re
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


class FrameConfinementError(ValueError):
    """A pair-mode side was asked to read something that is not one of its own staged (and, when listed, hash-verified) files. The message
    starts with a typed token: PAIR_SIDECAR_PATH_OUTSIDE_STAGING, PAIR_FRAME_NOT_LISTED, PAIR_FRAME_HASH_MISMATCH or PAIR_LISTING_INVALID."""


class FrameMissing(FrameConfinementError):
    """The named plain file is simply not in the staging directory (rendered as an UNPAIRED / image-missing tile, as before)."""


_STAGED_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.(png|json)$")


def load_listing(path):
    """--left-listed / --right-listed: {"files": [{"name": ..., "sha256": ...}]} -> {name: sha256}. Names are plain *.png / *.json file names."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = doc["files"]
        listed = {}
        for entry in entries:
            name, sha = entry["name"], entry["sha256"]
            if not isinstance(name, str) or not _STAGED_NAME.match(name) or not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha) or name in listed:
                raise ValueError("bad entry")
            listed[name] = sha
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise FrameConfinementError(f"PAIR_LISTING_INVALID the frame listing {path} is not a list of plain file names with 64-hex sha256: {exc}") from exc
    return listed


class StagedFrames:
    """The files a pair-mode side may read: the plain files of ONE staging directory and nothing else (DUAL-VENUE-EVIDENCE-3, sol r2 blocker, PR #223).
    `listed` ({name: sha256}) is the caller's hashed manifest: when given, a file that is not listed is refused and every file read is verified against its
    hash AT READ TIME (not at some earlier check), so a frame edited between the check and the compose is never composed. `listed=None` still confines
    the reads to the directory but does not verify bytes."""

    def __init__(self, frames_dir, listed=None):
        self.dir = Path(frames_dir)
        self.listed = listed
        if listed is not None:
            for entry in sorted(self.dir.iterdir()):
                if entry.name not in listed:
                    raise FrameConfinementError(f"PAIR_FRAME_NOT_LISTED '{entry.name}' is in the staging directory but the hashed listing does not list it")

    def read(self, name):
        if not _STAGED_NAME.match(name):
            raise FrameConfinementError(f"PAIR_FRAME_NOT_LISTED '{name}' is not a plain staged frame name")
        path = self.dir / name
        if not os.path.lexists(path):
            raise FrameMissing(f"PAIR_FRAME_NOT_LISTED '{name}' is not in the staging directory")
        if path.is_symlink() or not path.is_file():
            raise FrameConfinementError(f"PAIR_FRAME_NOT_LISTED '{name}' is not a plain file of the staging directory")
        if self.listed is not None and name not in self.listed:
            raise FrameConfinementError(f"PAIR_FRAME_NOT_LISTED '{name}' is not in the hashed listing")
        data = path.read_bytes()
        if self.listed is not None and hashlib.sha256(data).hexdigest() != self.listed[name]:
            raise FrameConfinementError(f"PAIR_FRAME_HASH_MISMATCH '{name}' does not hash to the sha256 the listing names (edited after it was listed)")
        return data

    def image_name_for(self, sidecar):
        """The PNG a sidecar's frame is: its OWN `<stem>.png`, in this directory. The app writes `path` as exactly that name; an absolute path, a parent-relative
        one, a nested one, another frame's name or any other spelling is a reference to something that is not this staged, listed frame."""
        own = sidecar.get("_stem", "") + ".png"
        raw = sidecar.get("path", "")
        if raw not in ("", None) and raw != own:
            raise FrameConfinementError(f"PAIR_SIDECAR_PATH_OUTSIDE_STAGING {own[:-4]}.json names an image other than its own staged {own}; a pair composes only the frames its staging directory holds")
        return own


def load_staged_frames(staged):
    """load_frames() for a pair-mode side: sidecars are read through the staged (hash-verified) reader, and each saved sidecar's image reference is confined."""
    frames = []
    for name in sorted(p.name for p in staged.dir.glob("frame-*.json")):
        try:
            data = json.loads(staged.read(name).decode("utf-8"))
        except FrameConfinementError:
            raise
        except (ValueError, OSError) as exc:
            print(f"[make-contact-sheet] WARNING: failed to read {name}: {exc}", file=sys.stderr)
            continue
        if not isinstance(data, dict) or not data.get("saved", False):
            continue
        data["_stem"] = name[:-len(".json")]
        staged.image_name_for(data)
        frames.append(data)
    frames.sort(key=lambda d: d.get("index", 0))
    return frames


def open_staged_image(staged, sidecar):
    """The frame's image from the staged reader (verified bytes), or None when the staging directory simply does not hold it."""
    try:
        return Image.open(io.BytesIO(staged.read(staged.image_name_for(sidecar))))
    except FrameMissing:
        return None


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


def _playback_path_token(value):
    """'true' / 'false' for a sidecar's playback_path, 'unknown' when a sidecar predates the field."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def tile_label(sidecar):
    """CONTACT-SHEET-PLAYBACK-PARITY-1: every tile says whether it is a frame the playback path
    presented (playback_path=true, in-pass) or a seek render (false), so a seek tile -- which takes a
    different render path and can look different -- is never read as what played."""
    return "frame {index} @ {elapsed_ms:.0f}ms (disp {display_frame}) playback_path={pp}".format(
        index=sidecar.get("index", -1),
        elapsed_ms=float(sidecar.get("elapsed_ms", 0.0)),
        display_frame=sidecar.get("display_frame", -1),
        pp=_playback_path_token(sidecar.get("playback_path")),
    )


def sheet_playback_path(frames):
    """The whole sheet's playback_path: true/false when every tile agrees, else mixed (or unknown)."""
    tokens = {_playback_path_token(f.get("playback_path")) for f in frames}
    if len(tokens) == 1:
        return tokens.pop()
    return "mixed" if tokens else "unknown"


def build_tile(image, sidecar, tile_size, font):
    thumb = image.convert("RGB").copy()
    thumb.thumbnail(tile_size, Image.LANCZOS)
    tile = Image.new("RGB", tile_size, (24, 24, 24))
    offset = ((tile_size[0] - thumb.width) // 2, (tile_size[1] - thumb.height - TILE_LABEL_HEIGHT) // 2)
    tile.paste(thumb, offset)

    draw = ImageDraw.Draw(tile)
    label = tile_label(sidecar)
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
        f"render_path(majority)={_majority_render_path(frames)}  playback_path={sheet_playback_path(frames)}",
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


PAIR_GUTTER_WIDTH = 24
PAIR_GUTTER_COLOUR = (128, 128, 128)
SCHEMA_STATS_PAIR = "contact-sheet-stats-pair.v1"


def _frames_by_index(frames):
    by_index = {}
    for sidecar in frames:
        by_index.setdefault(sidecar.get("index", -1), sidecar)
    return by_index


def _unpaired_tile(tile_size, label, font):
    tile = Image.new("RGB", tile_size, (48, 16, 16))
    draw = ImageDraw.Draw(tile)
    draw.text((8, tile_size[1] // 2 - 8), f"UNPAIRED: {label} has no frame at this index", fill=(255, 200, 200), font=font)
    return tile


def compose_pair_sheet(left_frames, left_dir, right_frames, right_dir, args, left_staged=None, right_staged=None):
    """Side-by-side sheet: rows are frame indices, columns are (left | gutter | right). Each side reads ONLY the plain files of its own directory
    (StagedFrames; hash-verified at read time when a listing was given): a sidecar `path` that is absolute, parent-relative or not its own `<stem>.png` is
    refused, and nothing falls back to a file outside the directory."""
    if not left_frames or not right_frames:
        raise ValueError("side-by-side needs usable (saved=true) frames on BOTH sides")
    left_staged = left_staged or StagedFrames(left_dir)
    right_staged = right_staged or StagedFrames(right_dir)
    left_by = _frames_by_index(left_frames)
    right_by = _frames_by_index(right_frames)
    indices = sorted(set(left_by) | set(right_by))

    probe = open_staged_image(left_staged, left_frames[0])
    if probe is None:
        raise ValueError("could not resolve the left side's first image")
    with probe:
        aspect = probe.height / probe.width if probe.width else 1.0
    tile_size = (TILE_TARGET_WIDTH, int(TILE_TARGET_WIDTH * aspect) + TILE_LABEL_HEIGHT)
    font = _load_font(13)

    def side_tile(by_index, staged, index, side_label, stats_out):
        sidecar = by_index.get(index)
        if sidecar is None:
            return _unpaired_tile(tile_size, side_label, font)
        image = open_staged_image(staged, sidecar)
        if image is None:
            return _unpaired_tile(tile_size, side_label + " (image missing)", font)
        with image as full_res:
            stats = channel_stats(full_res)
            stats["index"] = index
            stats["display_frame"] = sidecar.get("display_frame")
            stats["elapsed_ms"] = sidecar.get("elapsed_ms")
            stats_out.append(stats)
            tile = build_tile(full_res, sidecar, tile_size, font)
        ImageDraw.Draw(tile).text((tile_size[0] - 70, 4), side_label, fill=(255, 255, 0), font=font)
        return tile

    left_stats, right_stats = [], []
    rows = []
    for index in indices:
        rows.append((
            index,
            side_tile(left_by, left_staged, index, args.left_label, left_stats),
            side_tile(right_by, right_staged, index, args.right_label, right_stats),
        ))

    row_width = tile_size[0] * 2 + PAIR_GUTTER_WIDTH
    grid_width = row_width + 2 * TILE_PADDING
    grid_height = len(rows) * (tile_size[1] + TILE_PADDING) + TILE_PADDING
    sheet = Image.new("RGB", (grid_width, HEADER_HEIGHT + grid_height), (8, 8, 8))

    header = Image.new("RGB", (grid_width, HEADER_HEIGHT), (16, 16, 16))
    draw = ImageDraw.Draw(header)
    header_font = _load_font(14)
    left_args = argparse.Namespace(**{**vars(args), "backend": args.left_label})
    right_args = argparse.Namespace(**{
        **vars(args), "backend": args.right_label,
        "host": args.right_host or args.host, "gpu": args.right_gpu or args.gpu,
        "build_sha": args.right_build_sha or args.build_sha,
    })
    left_lines = build_header_lines(left_args, left_frames)
    right_lines = build_header_lines(right_args, right_frames)
    unpaired = [i for i in indices if i not in left_by or i not in right_by]
    y = 6
    for text in (
        f"LEFT  | {left_lines[0]}",
        f"RIGHT | {right_lines[0]}",
        f"LEFT  {left_lines[1]}",
        f"RIGHT {right_lines[1]}",
        f"paired_by=frame_index  rows={len(rows)}  unpaired={unpaired or 'none'}",
    ):
        draw.text((10, y), text, fill=(255, 255, 255), font=header_font)
        y += 22
    sheet.paste(header, (0, 0))

    for row_number, (_index, left_tile, right_tile) in enumerate(rows):
        y = HEADER_HEIGHT + TILE_PADDING + row_number * (tile_size[1] + TILE_PADDING)
        x = TILE_PADDING
        sheet.paste(left_tile, (x, y))
        sheet.paste(Image.new("RGB", (PAIR_GUTTER_WIDTH, tile_size[1]), PAIR_GUTTER_COLOUR), (x + tile_size[0], y))
        sheet.paste(right_tile, (x + tile_size[0] + PAIR_GUTTER_WIDTH, y))
    return sheet, left_stats, right_stats, unpaired


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


def main_pair(args):
    if not args.pair_dir.is_dir():
        print(f"[make-contact-sheet] ERROR: pair dir does not exist: {args.pair_dir}", file=sys.stderr)
        return 2
    try:
        left_staged = StagedFrames(args.frames_dir, load_listing(args.left_listed) if args.left_listed else None)
        right_staged = StagedFrames(args.pair_dir, load_listing(args.right_listed) if args.right_listed else None)
        left_frames = load_staged_frames(left_staged)
        right_frames = load_staged_frames(right_staged)
        sheet, left_stats, right_stats, unpaired = compose_pair_sheet(
            left_frames, args.frames_dir, right_frames, args.pair_dir, args, left_staged, right_staged)
    except ValueError as exc:
        print(f"[make-contact-sheet] ERROR: {exc}", file=sys.stderr)
        return 3
    args.sheet_out.parent.mkdir(parents=True, exist_ok=True)
    save_under_budget(sheet, args.sheet_out, args.max_bytes)
    args.stats_out.parent.mkdir(parents=True, exist_ok=True)
    stats_doc = {
        "schema": SCHEMA_STATS_PAIR,
        "sheet_path": _relative_or_name(args.sheet_out, args.stats_out.parent),
        "paired_by": "frame_index",
        "clip_id": args.clip_id,
        "left": {"label": args.left_label, "host": args.host, "gpu": args.gpu,
                 "build_sha": args.build_sha, "tile_count": len(left_stats), "tiles": left_stats},
        "right": {"label": args.right_label, "host": args.right_host or args.host,
                  "gpu": args.right_gpu or args.gpu, "build_sha": args.right_build_sha or args.build_sha,
                  "tile_count": len(right_stats), "tiles": right_stats},
        "unpaired_indices": unpaired,
        "sheet_bytes": args.sheet_out.stat().st_size,
    }
    args.stats_out.write_text(json.dumps(stats_doc, indent=2), encoding="utf-8")
    print(
        f"[make-contact-sheet] OK pair sheet={args.sheet_out} stats={args.stats_out} "
        f"left={len(left_stats)} right={len(right_stats)} unpaired={len(unpaired)} sheet_bytes={stats_doc['sheet_bytes']}"
    )
    return 0


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
    parser.add_argument("--pair-dir", type=Path, default=None,
                        help="Side-by-side mode: the RIGHT side's capture dir, paired with --frames-dir by frame index.")
    parser.add_argument("--left-listed", type=Path, default=None,
                        help="Side-by-side mode: JSON {\"files\": [{\"name\", \"sha256\"}]} listing the files the left (--frames-dir) staging directory may hold; each is "
                             "hash-verified when read, and an unlisted file is refused. Both sides' listings or neither.")
    parser.add_argument("--right-listed", type=Path, default=None, help="Same, for the right (--pair-dir) side.")
    parser.add_argument("--left-label", default="cuda")
    parser.add_argument("--right-label", default="cpu")
    parser.add_argument("--right-host", default="")
    parser.add_argument("--right-gpu", default="")
    parser.add_argument("--right-build-sha", default="")
    args = parser.parse_args()
    if (args.left_listed is None) != (args.right_listed is None) or (args.left_listed is not None and args.pair_dir is None):
        parser.error("--left-listed and --right-listed go together, and only with --pair-dir")

    if not args.frames_dir.is_dir():
        print(f"[make-contact-sheet] ERROR: frames dir does not exist: {args.frames_dir}", file=sys.stderr)
        return 2

    if args.pair_dir is not None:
        return main_pair(args)

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
