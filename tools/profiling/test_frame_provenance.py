"""Falsifier tests for frame_provenance.

Each test is written to FAIL if the matching rule quietly degrades to something weaker --
filename matching, or treating an orphan as a match.
"""
from __future__ import annotations

import hashlib
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


def _stamp_invalid(capture_dir: str, **fields) -> None:
    """Rewrite a capture's manifest the way reference-frame-runner-job.ps1 stamps a voided one (status, invalid, invalid_reasons)."""
    path = os.path.join(capture_dir, "reference-frame-candidate.json")
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    doc.update(fields)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


@pytest.mark.parametrize("fields", [
    {"status": "invalid", "invalid": True, "invalid_reasons": ["OWNER_FOOTAGE_VIEW_CHANGED_DURING_CAPTURE"]},
    {"invalid": True},
    {"status": "invalid"},
], ids=["full-runner-stamp", "invalid-flag-only", "status-only"])
def test_a_manifest_stamped_invalid_is_not_indexed_even_with_no_marker_and_no_quarantine(tmp_path, fields):
    """REFERENCE-FRAME-CONSUMER-HONORS-INVALID-STAMP-1: when the runner job cannot publish the marker but CAN stamp the manifest, it writes no
    marker and does not quarantine (it only does either when the stamp failed too). The stamp is then the only record that the capture was
    voided, so the consumer must read it: the voided pixels bind to nothing and the skip carries a typed reason."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "voided", b"PIXELS-V", _manifest("aaaaaaaaaaaa"))
    _capture(str(ob), "good", b"PIXELS-G", _manifest("bbbbbbbbbbbb"))
    _stamp_invalid(d, **fields)
    assert not os.path.exists(os.path.join(d, "reference-frame-candidate.invalid"))
    assert os.path.isfile(os.path.join(d, "reference-frame-candidate.json"))

    skipped: list[dict] = []
    index = index_capture_records(str(ob), skipped)

    assert len(index) == 1
    assert list(index.values())[0].endswith(os.path.join("good", "reference-frame-candidate.json"))
    assert [(s["reason"], os.path.basename(s["capture"])) for s in skipped] == [("manifest-stamped-invalid", "voided")]
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "v.png").write_bytes(b"PIXELS-V")
    (frames / "g.png").write_bytes(b"PIXELS-G")
    result = build(str(frames), str(ob))
    assert (result["matched"], result["orphaned"]) == (1, 1)
    assert [(s["reason"], os.path.basename(s["capture"])) for s in result["skipped"]] == [("manifest-stamped-invalid", "voided")]


def test_a_stamped_invalid_manifest_binds_nothing_through_the_unchanged_api(tmp_path):
    """The behavioural core, through the signatures that existed before the skip report: a frame with a stamped-invalid capture's pixels is an orphan."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "voided", b"PIXELS-V", _manifest("aaaaaaaaaaaa"))
    _stamp_invalid(d, status="invalid", invalid=True, invalid_reasons=["OWNER_FOOTAGE_VIEW_CHANGED_DURING_CAPTURE"])
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "v.png").write_bytes(b"PIXELS-V")

    assert index_capture_records(str(ob)) == {}
    result = build(str(frames), str(ob))
    assert (result["matched"], result["orphaned"]) == (0, 1)
    assert "commit" not in result["frames"][0]


@pytest.mark.parametrize("fields", [
    {"status": "invalid", "invalid": True, "invalid_reasons": ["OWNER_FOOTAGE_VIEW_CHANGED_DURING_CAPTURE"]},
    {"invalid": True},
    {"status": "invalid"},
], ids=["full-runner-stamp", "invalid-flag-only", "status-only"])
def test_every_stamp_shape_binds_nothing_through_the_signatures_that_predate_the_skip_report(tmp_path, fields):
    """REFERENCE-FRAME-STAMP-SHAPES-BEHAVIOURAL-RED-1: the parametrized test above calls ``index_capture_records(root, skipped)``, so on a reader
    that predates #564 all three of its cases die with a TypeError on the new parameter and never reach the behaviour. This one passes no
    ``skipped`` at all, so against that reader it fails on the assertion: a stamped manifest is still indexed (1 entry, not 0)."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "voided", b"PIXELS-V", _manifest("aaaaaaaaaaaa"))
    _stamp_invalid(d, **fields)
    assert not os.path.exists(os.path.join(d, "reference-frame-candidate.invalid"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "v.png").write_bytes(b"PIXELS-V")

    index = index_capture_records(str(ob))
    result = build(str(frames), str(ob))

    assert len(index) == 0, "a manifest stamped invalid was indexed as a candidate"
    assert (result["recordsIndexed"], result["matched"], result["orphaned"]) == (0, 0, 1)
    assert "commit" not in result["frames"][0] and "manifest" not in result["frames"][0]


def test_a_marker_skip_carries_its_own_typed_reason(tmp_path):
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "voided", b"PIXELS-V", _manifest("aaaaaaaaaaaa"))
    with open(os.path.join(d, "reference-frame-candidate.invalid"), "w", encoding="utf-8") as fh:
        fh.write("{}")
    skipped: list[dict] = []

    assert index_capture_records(str(ob), skipped) == {}
    assert [s["reason"] for s in skipped] == ["invalid-marker"]


def test_a_valid_capture_output_is_unchanged_and_names_no_skips(tmp_path):
    """Valid-capture behaviour is byte-identical: no 'skipped' key appears, and a manifest whose invalid/status fields are present but benign
    (invalid false, status candidate-unreviewed) is still a candidate."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "run1", b"PIXELS-A", _manifest("aaaaaaaaaaaa"))
    _stamp_invalid(d, status="candidate-unreviewed", invalid=False)
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "f.png").write_bytes(b"PIXELS-A")

    result = build(str(frames), str(ob))

    assert (result["matched"], result["orphaned"]) == (1, 0)
    assert "skipped" not in result
    # The whole document, key order included, so a future key (or a reordering) in the valid case cannot slip past a counts-only check.
    manifest = _manifest("aaaaaaaaaaaa")
    expected = {
        "schema": SCHEMA,
        "framesDir": str(frames),
        "outboxRoot": str(ob),
        "recordsIndexed": 1,
        "matched": 1,
        "orphaned": 0,
        "frames": [{
            "frame": "f.png",
            "sha256": hashlib.sha256(b"PIXELS-A").hexdigest(),
            "commit": "aaaaaaaaaaaa",
            "host": "BENCH",
            "capturedUtc": "2026-09-03T12:00:00Z",
            "exeSha256": manifest["executable"]["sha256"],
            "clipSha256": manifest["clip"]["sha256"],
            "settings": manifest["settings"],
            "appSettings": manifest["appSettings"],
            "manifest": os.path.join(str(d), "reference-frame-candidate.json"),
        }],
    }
    assert json.dumps(result) == json.dumps(expected)


def test_a_manifest_with_a_utf8_bom_binds_like_any_other(tmp_path):
    """The reader opens a manifest as utf-8-sig (PowerShell 5.1 writes a BOM); build() must too, or a BOM-prefixed manifest that the index
    accepted raises JSONDecodeError when the matched row's identity is read."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "bom", b"PIXELS-A", _manifest("aaaaaaaaaaaa"))
    with open(os.path.join(d, "reference-frame-candidate.json"), "w", encoding="utf-8-sig") as fh:
        json.dump(_manifest("aaaaaaaaaaaa"), fh)
    with open(os.path.join(d, "reference-frame-candidate.json"), "rb") as fh:
        assert fh.read(3) == b"\xef\xbb\xbf", "fixture lost its BOM"
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "f.png").write_bytes(b"PIXELS-A")

    result = build(str(frames), str(ob))

    assert (result["matched"], result["orphaned"]) == (1, 0)
    assert result["frames"][0]["commit"] == "aaaaaaaaaaaa"


def test_an_unparseable_manifest_without_a_marker_keeps_its_existing_behaviour(tmp_path):
    """Not widened: a manifest that does not parse and has no marker is indexed exactly as before (the stamp cannot be read, so nothing is claimed)."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "broken", b"PIXELS-B", _manifest("aaaaaaaaaaaa"))
    with open(os.path.join(d, "reference-frame-candidate.json"), "w", encoding="utf-8") as fh:
        fh.write('{"status": "candidate-unreviewed", "trunc')
    skipped: list[dict] = []

    assert len(index_capture_records(str(ob), skipped)) == 1
    assert skipped == []


def test_an_unparseable_manifest_with_a_marker_does_not_crash_the_index(tmp_path):
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "broken", b"PIXELS-B", _manifest("aaaaaaaaaaaa"))
    with open(os.path.join(d, "reference-frame-candidate.json"), "w", encoding="utf-8") as fh:
        fh.write('{"status": "candidate-unreviewed", "trunc')
    with open(os.path.join(d, "reference-frame-candidate.invalid"), "w", encoding="utf-8") as fh:
        fh.write("{}")

    assert index_capture_records(str(ob)) == {}
