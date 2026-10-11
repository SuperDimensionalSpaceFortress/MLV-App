#!/usr/bin/env python3
"""Byte-compare two MLVAPP_PLAYBACK_PRESENTED_DUMP_DIR directories (PLAYBACK-CPU-PACE-GAP-1 exactness arm).

Each directory holds <frame>.rgb8 (the CPU prep route's processed RGB8 of a presented frame) and <frame>.json
({frame, width, height, scale, lookahead_request}). Frames present in both are compared for full byte equality,
zero tolerance. Exit 0 only when at least --min-common frames are common and every one is identical; exit 1 on
any mismatch (size, dims or bytes) or too few common frames; exit 2 on a usage error.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

DEFAULT_MIN_COMMON = 8


def dump_frames(directory: Path) -> dict[int, Path]:
    """Frame number -> .rgb8 path for every <int>.rgb8 in directory."""
    frames: dict[int, Path] = {}
    for path in directory.glob("*.rgb8"):
        try:
            frames[int(path.stem)] = path
        except ValueError:
            continue
    return frames


def _meta(rgb8: Path) -> dict:
    meta_path = rgb8.with_suffix(".json")
    if not meta_path.is_file():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def compare_dirs(dir_a: Path, dir_b: Path, min_common: int = DEFAULT_MIN_COMMON) -> dict:
    frames_a = dump_frames(dir_a)
    frames_b = dump_frames(dir_b)
    common = sorted(set(frames_a) & set(frames_b))
    rows = []
    mismatches = 0
    lookahead_a = 0
    lookahead_b = 0
    for frame in common:
        meta_a = _meta(frames_a[frame])
        meta_b = _meta(frames_b[frame])
        bytes_a = frames_a[frame].read_bytes()
        bytes_b = frames_b[frame].read_bytes()
        dims_a = (meta_a.get("width"), meta_a.get("height"))
        dims_b = (meta_b.get("width"), meta_b.get("height"))
        equal = bytes_a == bytes_b and dims_a == dims_b
        if not equal:
            mismatches += 1
        lookahead_a += 1 if meta_a.get("lookahead_request") else 0
        lookahead_b += 1 if meta_b.get("lookahead_request") else 0
        rows.append(
            {
                "frame": frame,
                "equal": equal,
                "bytes_a": len(bytes_a),
                "bytes_b": len(bytes_b),
                "dims_a": list(dims_a),
                "dims_b": list(dims_b),
                "sha256_a": hashlib.sha256(bytes_a).hexdigest(),
                "sha256_b": hashlib.sha256(bytes_b).hexdigest(),
                "lookahead_a": bool(meta_a.get("lookahead_request")),
                "lookahead_b": bool(meta_b.get("lookahead_request")),
            }
        )
    too_few = len(common) < min_common
    return {
        "dir_a": str(dir_a),
        "dir_b": str(dir_b),
        "frames_a": len(frames_a),
        "frames_b": len(frames_b),
        "common": len(common),
        "min_common": min_common,
        "mismatches": mismatches,
        "lookahead_frames_a": lookahead_a,
        "lookahead_frames_b": lookahead_b,
        "verdict": "PASS" if (not too_few and mismatches == 0) else ("TOO_FEW" if too_few else "MISMATCH"),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dir_a", type=Path)
    parser.add_argument("dir_b", type=Path)
    parser.add_argument("--min-common", type=int, default=DEFAULT_MIN_COMMON)
    parser.add_argument("--json", type=Path, default=None, help="also write the full report here")
    args = parser.parse_args(argv)
    for directory in (args.dir_a, args.dir_b):
        if not directory.is_dir():
            print(f"compare_presented_dumps: not a directory: {directory}", file=sys.stderr)
            return 2
    report = compare_dirs(args.dir_a, args.dir_b, args.min_common)
    for row in report["rows"]:
        state = "equal" if row["equal"] else "MISMATCH"
        print(
            f"frame={row['frame']} {state} bytes={row['bytes_a']}/{row['bytes_b']} "
            f"dims={row['dims_a'][0]}x{row['dims_a'][1]}/{row['dims_b'][0]}x{row['dims_b'][1]} "
            f"lookahead={int(row['lookahead_a'])}/{int(row['lookahead_b'])} sha256={row['sha256_a'][:16]}"
        )
    print(
        f"compare_presented_dumps verdict={report['verdict']} common={report['common']} "
        f"min_common={report['min_common']} mismatches={report['mismatches']} "
        f"frames_a={report['frames_a']} frames_b={report['frames_b']} "
        f"lookahead_frames_a={report['lookahead_frames_a']} lookahead_frames_b={report['lookahead_frames_b']}"
    )
    if args.json is not None:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
