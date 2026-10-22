"""Falsifier tests for frame_provenance.

Each test is written to FAIL if the matching rule quietly degrades to something weaker --
filename matching, or treating an orphan as a match.
"""
from __future__ import annotations

import json
import os

import pytest

from frame_provenance import SCHEMA, build, index_capture_records


def _capture(root: str, name: str, pixels: bytes, manifest: dict) -> str:
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "reference-frame.png"), "wb") as fh:
        fh.write(pixels)
    with open(os.path.join(d, "reference-frame-candidate.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh)
    return d


def _manifest(commit: str) -> dict:
    return {
        "commit": commit,
        "host": "BENCH",
        "capturedUtc": "2026-09-03T12:00:00Z",
        "executable": {"sha256": "e" * 64},
        "clip": {"sha256": "c" * 64},
        "settings": {"seconds": 45},
        "appSettings": {"playbackProcessingSubset": "true"},
    }


def test_binds_frame_to_its_capture_record(tmp_path):
    ob = tmp_path / "outbox"
    _capture(str(ob), "run1", b"PIXELS-A", _manifest("aaaaaaaaaaaa"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "whatever.png").write_bytes(b"PIXELS-A")

    r = build(str(frames), str(ob))
    assert r["schema"] == SCHEMA
    assert r["matched"] == 1 and r["orphaned"] == 0
    assert r["frames"][0]["commit"] == "aaaaaaaaaaaa"
    assert r["frames"][0]["appSettings"]["playbackProcessingSubset"] == "true"


def test_a_MISLEADING_FILENAME_does_not_inherit_provenance(tmp_path):
    """The whole point of hashing bytes: a frame named after build B but containing build
    A's pixels must resolve to A. Filename matching would return B and be silently wrong."""
    ob = tmp_path / "outbox"
    _capture(str(ob), "buildA", b"PIXELS-A", _manifest("aaaaaaaaaaaa"))
    _capture(str(ob), "buildB", b"PIXELS-B", _manifest("bbbbbbbbbbbb"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "buildB-frame.png").write_bytes(b"PIXELS-A")

    r = build(str(frames), str(ob))
    assert r["frames"][0]["commit"] == "aaaaaaaaaaaa", "resolved by name, not by bytes"


def test_orphan_is_reported_not_silently_matched(tmp_path):
    ob = tmp_path / "outbox"
    _capture(str(ob), "run1", b"PIXELS-A", _manifest("aaaaaaaaaaaa"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "stray.png").write_bytes(b"PIXELS-UNSEEN")

    r = build(str(frames), str(ob))
    assert r["matched"] == 0 and r["orphaned"] == 1
    row = r["frames"][0]
    assert "commit" not in row
    assert "orphaned" in row["provenance"]
    assert row["sha256"], "an orphan must still carry its pixel hash so it stays identifiable"


def test_manifest_without_its_frame_is_not_indexed(tmp_path):
    """A capture record whose image is gone cannot be the source of a frame we hold."""
    ob = tmp_path / "outbox"
    d = ob / "run1"
    d.mkdir(parents=True)
    (d / "reference-frame-candidate.json").write_text(json.dumps(_manifest("aaaaaaaaaaaa")), encoding="utf-8")

    assert index_capture_records(str(ob)) == {}


def test_identical_pixels_from_two_runs_do_not_multiply_records(tmp_path):
    """Deterministic captures produce byte-identical frames. The index must stay keyed by
    pixels, and a frame must bind to exactly one record rather than duplicating."""
    ob = tmp_path / "outbox"
    _capture(str(ob), "run1", b"SAME", _manifest("aaaaaaaaaaaa"))
    _capture(str(ob), "run2", b"SAME", _manifest("aaaaaaaaaaaa"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "f.png").write_bytes(b"SAME")

    r = build(str(frames), str(ob))
    assert r["recordsIndexed"] == 1
    assert len(r["frames"]) == 1 and r["matched"] == 1


def test_non_png_files_are_ignored(tmp_path):
    ob = tmp_path / "outbox"
    _capture(str(ob), "run1", b"PIXELS-A", _manifest("aaaaaaaaaaaa"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "notes.txt").write_text("not a frame", encoding="utf-8")
    (frames / "f.png").write_bytes(b"PIXELS-A")

    r = build(str(frames), str(ob))
    assert [f["frame"] for f in r["frames"]] == ["f.png"]


def test_a_capture_with_an_invalid_marker_is_not_indexed_whatever_its_manifest_says(tmp_path):
    """VENUE-REFERENCE-FRAME-CANDIDATE-INVALID-MARKER-1: the runner job stamps a voided manifest invalid in place on a best-effort basis; when
    the stamp cannot be written it leaves reference-frame-candidate.invalid beside it. Marker present = not a candidate, so the pixels of that
    capture bind to nothing -- even when the manifest still parses and still says candidate-unreviewed."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "voided", b"PIXELS-V", _manifest("aaaaaaaaaaaa"))
    _capture(str(ob), "good", b"PIXELS-G", _manifest("bbbbbbbbbbbb"))
    with open(os.path.join(d, "reference-frame-candidate.invalid"), "w", encoding="utf-8") as fh:
        json.dump({"invalid": True, "invalid_reasons": ["OWNER_FOOTAGE_VIEW_CHANGED_DURING_CAPTURE"]}, fh)

    index = index_capture_records(str(ob))

    assert len(index) == 1
    assert list(index.values())[0].endswith(os.path.join("good", "reference-frame-candidate.json"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "v.png").write_bytes(b"PIXELS-V")
    result = build(str(frames), str(ob))
    assert (result["matched"], result["orphaned"]) == (0, 1)


def test_an_unparseable_manifest_with_a_marker_does_not_crash_the_index(tmp_path):
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "broken", b"PIXELS-B", _manifest("aaaaaaaaaaaa"))
    with open(os.path.join(d, "reference-frame-candidate.json"), "w", encoding="utf-8") as fh:
        fh.write('{"status": "candidate-unreviewed", "trunc')
    with open(os.path.join(d, "reference-frame-candidate.invalid"), "w", encoding="utf-8") as fh:
        fh.write("{}")

    assert index_capture_records(str(ob)) == {}
