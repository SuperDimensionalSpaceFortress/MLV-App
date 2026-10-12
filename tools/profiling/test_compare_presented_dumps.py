"""Falsifier tests for compare_presented_dumps (PLAYBACK-CPU-PACE-GAP-1 exactness arm).

Each test FAILS if the comparison quietly weakens: a one-byte difference passing, too few common frames passing,
or frames present in only one directory being counted as common.
"""
from __future__ import annotations

import json

from compare_presented_dumps import compare_dirs, main


def _write(directory, frame, payload, width=4, height=2, lookahead=False):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{frame}.rgb8").write_bytes(payload)
    (directory / f"{frame}.json").write_text(
        json.dumps({"frame": frame, "width": width, "height": height, "scale": 2, "lookahead_request": lookahead}),
        encoding="utf-8",
    )


def _payload(frame):
    return bytes((frame * 7 + i) % 256 for i in range(4 * 2 * 3))


def test_identical_dumps_over_eight_frames_pass(tmp_path):
    for frame in range(10, 18):
        _write(tmp_path / "a", frame, _payload(frame), lookahead=frame % 2 == 0)
        _write(tmp_path / "b", frame, _payload(frame))
    report = compare_dirs(tmp_path / "a", tmp_path / "b")
    assert report["verdict"] == "PASS"
    assert report["common"] == 8
    assert report["lookahead_frames_a"] == 4
    assert main([str(tmp_path / "a"), str(tmp_path / "b")]) == 0


def test_a_single_differing_byte_fails(tmp_path):
    for frame in range(8):
        _write(tmp_path / "a", frame, _payload(frame))
        payload = bytearray(_payload(frame))
        if frame == 5:
            payload[-1] ^= 1
        _write(tmp_path / "b", frame, bytes(payload))
    report = compare_dirs(tmp_path / "a", tmp_path / "b")
    assert report["verdict"] == "MISMATCH"
    assert report["mismatches"] == 1
    assert main([str(tmp_path / "a"), str(tmp_path / "b")]) == 1


def test_differing_dims_with_equal_bytes_fail(tmp_path):
    for frame in range(8):
        _write(tmp_path / "a", frame, _payload(frame), width=4, height=2)
        _write(tmp_path / "b", frame, _payload(frame), width=2, height=4 if frame == 0 else 2)
    assert compare_dirs(tmp_path / "a", tmp_path / "b")["mismatches"] == 8


def test_fewer_than_eight_common_frames_fail_even_when_equal(tmp_path):
    for frame in range(10):
        _write(tmp_path / "a", frame, _payload(frame))
    for frame in range(3, 10, 1):
        if frame != 4:
            _write(tmp_path / "b", frame, _payload(frame))
    report = compare_dirs(tmp_path / "a", tmp_path / "b")
    assert report["common"] == 6
    assert report["verdict"] == "TOO_FEW"
    assert main([str(tmp_path / "a"), str(tmp_path / "b")]) == 1


def test_missing_directory_is_a_usage_error(tmp_path):
    (tmp_path / "a").mkdir()
    assert main([str(tmp_path / "a"), str(tmp_path / "missing")]) == 2
