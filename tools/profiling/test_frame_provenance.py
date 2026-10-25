"""Falsifier tests for frame_provenance.

Each test is written to FAIL if the matching rule quietly degrades to something weaker --
filename matching, or treating an orphan as a match.
"""
from __future__ import annotations

import hashlib
import json
import os

import pytest

import frame_provenance as fp
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


STAMP_SHAPES = [
    {"status": "invalid", "invalid": True, "invalid_reasons": ["OWNER_FOOTAGE_VIEW_CHANGED_DURING_CAPTURE"]},
    {"invalid": True},
    {"status": "invalid"},
]
STAMP_SHAPE_IDS = ["full-runner-stamp", "invalid-flag-only", "status-only"]


def _prefix_bom(capture_dir: str) -> None:
    """Re-save a capture's manifest with the UTF-8 BOM that PowerShell 5.1 writes, and prove the fixture kept it."""
    path = os.path.join(capture_dir, "reference-frame-candidate.json")
    with open(path, "rb") as fh:
        raw = fh.read()
    assert not raw.startswith(b"\xef\xbb\xbf"), "fixture already had a BOM"
    with open(path, "wb") as fh:
        fh.write(b"\xef\xbb\xbf" + raw)
    with open(path, "rb") as fh:
        assert fh.read(3) == b"\xef\xbb\xbf", "fixture lost its BOM"


@pytest.mark.parametrize("fields", STAMP_SHAPES, ids=STAMP_SHAPE_IDS)
def test_a_bom_prefixed_manifest_stamped_invalid_is_skipped_like_its_unprefixed_twin(tmp_path, fields):
    """REFERENCE-FRAME-STAMP-BOM-INVALID-PINS-1: the stamp tests above and the BOM test below each cover one half. A voided manifest written by
    PowerShell 5.1 carries both a BOM and a stamp, so every stamp shape must be skipped with the same typed reason and the same counts as the
    unprefixed manifest -- a reader that opened the manifest as plain utf-8 would fail to parse it, claim nothing, and index the voided pixels."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "voided", b"PIXELS-V", _manifest("aaaaaaaaaaaa"))
    _capture(str(ob), "good", b"PIXELS-G", _manifest("bbbbbbbbbbbb"))
    _stamp_invalid(d, **fields)
    _prefix_bom(d)
    assert not os.path.exists(os.path.join(d, "reference-frame-candidate.invalid"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "v.png").write_bytes(b"PIXELS-V")
    (frames / "g.png").write_bytes(b"PIXELS-G")

    skipped: list[dict] = []
    index = index_capture_records(str(ob), skipped)
    result = build(str(frames), str(ob))

    assert len(index) == 1
    assert list(index.values())[0].endswith(os.path.join("good", "reference-frame-candidate.json"))
    assert [(s["reason"], os.path.basename(s["capture"])) for s in skipped] == [("manifest-stamped-invalid", "voided")]
    assert (result["recordsIndexed"], result["matched"], result["orphaned"]) == (1, 1, 1)
    assert [(s["reason"], os.path.basename(s["capture"])) for s in result["skipped"]] == [("manifest-stamped-invalid", "voided")]
    by_name = {r["frame"]: r for r in result["frames"]}
    assert "commit" not in by_name["v.png"] and "manifest" not in by_name["v.png"]
    assert by_name["g.png"]["commit"] == "bbbbbbbbbbbb"


@pytest.mark.parametrize("fields", STAMP_SHAPES, ids=STAMP_SHAPE_IDS)
def test_a_bom_prefixed_stamped_manifest_binds_nothing_through_the_signatures_that_predate_the_skip_report(tmp_path, fields):
    """The BOM twin of the no-``skipped`` test above: the same three stamp shapes, with no skip list passed, bind nothing."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "voided", b"PIXELS-V", _manifest("aaaaaaaaaaaa"))
    _stamp_invalid(d, **fields)
    _prefix_bom(d)
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "v.png").write_bytes(b"PIXELS-V")

    assert index_capture_records(str(ob)) == {}
    result = build(str(frames), str(ob))

    assert (result["recordsIndexed"], result["matched"], result["orphaned"]) == (0, 0, 1)
    assert "commit" not in result["frames"][0] and "manifest" not in result["frames"][0]


def _corrupt_manifest_after_indexing(monkeypatch, mutate) -> None:
    """Make build()'s own index pass see a readable manifest, then run ``mutate(manifest_path)`` before the per-frame read -- the window between
    indexing and the read, in which a capture can be rewritten or removed."""
    real = fp.index_capture_records

    def index_then_mutate(root, skipped=None):
        index = real(root, skipped)
        for manifest_path in index.values():
            mutate(manifest_path)
        return index

    monkeypatch.setattr(fp, "index_capture_records", index_then_mutate)


def _write_bytes(data: bytes):
    def mutate(path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(data)
    return mutate


def _delete(path: str) -> None:
    os.remove(path)


@pytest.mark.parametrize("mutate", [
    _write_bytes(b'{"commit": "aaaaaaaaaaaa", "trunc'),
    _write_bytes(b"\xef\xbb\xbf" + b'{"commit": "aaaaaaaaaaaa", "trunc'),
    _write_bytes(b"\xff\xfe\x00\x81{}"),
    _write_bytes(b"[]"),
    _delete,
], ids=["truncated-json", "truncated-json-with-bom", "invalid-utf8", "not-a-json-object", "deleted"])
def test_a_matched_manifest_that_goes_bad_after_indexing_keeps_the_frame_row_and_the_rest_of_the_report(tmp_path, monkeypatch, mutate):
    """REFERENCE-FRAME-STAMP-BOM-INVALID-PINS-1 part 2: a matched manifest that is unreadable by the time the row is built (malformed, not an
    object, undecodable, or gone) used to raise out of the per-frame loop and abort the whole report. Now the frame keeps its row with a typed
    ``provenance`` and its pixel hash, is not counted as matched, and every other frame still renders."""
    ob = tmp_path / "outbox"
    bad = _capture(str(ob), "bad", b"PIXELS-BAD", _manifest("aaaaaaaaaaaa"))
    _capture(str(ob), "good", b"PIXELS-G", _manifest("bbbbbbbbbbbb"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "a.png").write_bytes(b"PIXELS-BAD")
    (frames / "z.png").write_bytes(b"PIXELS-G")
    bad_manifest = os.path.join(bad, "reference-frame-candidate.json")

    def mutate_bad_only(path: str) -> None:
        if path == bad_manifest:
            mutate(path)

    _corrupt_manifest_after_indexing(monkeypatch, mutate_bad_only)

    result = fp.build(str(frames), str(ob))

    assert (result["recordsIndexed"], result["matched"], result["orphaned"]) == (2, 1, 1)
    rows = {r["frame"]: r for r in result["frames"]}
    assert list(rows) == ["a.png", "z.png"], "a frame row was dropped"
    assert rows["a.png"]["sha256"] == hashlib.sha256(b"PIXELS-BAD").hexdigest()
    assert rows["a.png"]["provenance"].startswith("unreadable - ")
    assert "commit" not in rows["a.png"]
    assert rows["a.png"]["manifest"] == bad_manifest, "the operator needs the path to go and look"
    assert rows["z.png"]["commit"] == "bbbbbbbbbbbb" and "provenance" not in rows["z.png"]
    assert "skipped" not in result, "no new top-level shape: the unreadable manifest is carried on the frame row"


@pytest.mark.parametrize("content", [b'{"commit": "aaaaaaaaaaaa", "trunc', b"[]"], ids=["truncated-json", "not-a-json-object"])
def test_a_malformed_manifest_with_no_marker_gives_an_unreadable_row_not_a_crash(tmp_path, content):
    """No monkeypatch: the index deliberately keeps indexing an unparseable manifest (test above), so build() meets it for real."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "broken", b"PIXELS-B", _manifest("aaaaaaaaaaaa"))
    with open(os.path.join(d, "reference-frame-candidate.json"), "wb") as fh:
        fh.write(content)
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "f.png").write_bytes(b"PIXELS-B")

    result = build(str(frames), str(ob))

    assert (result["recordsIndexed"], result["matched"], result["orphaned"]) == (1, 0, 1)
    assert result["frames"][0]["provenance"].startswith("unreadable - ")


def test_an_unreadable_manifest_makes_main_exit_nonzero_and_still_writes_the_report(tmp_path):
    """An unbound frame is reported the way an orphan is: exit 4, with the document written."""
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "broken", b"PIXELS-B", _manifest("aaaaaaaaaaaa"))
    with open(os.path.join(d, "reference-frame-candidate.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "f.png").write_bytes(b"PIXELS-B")
    out = tmp_path / "out.json"

    assert fp.main(["--frames-dir", str(frames), "--outbox-root", str(ob), "--out", str(out)]) == 4
    assert json.loads(out.read_text(encoding="utf-8"))["frames"][0]["provenance"].startswith("unreadable - ")


def test_an_unparseable_manifest_with_a_marker_does_not_crash_the_index(tmp_path):
    ob = tmp_path / "outbox"
    d = _capture(str(ob), "broken", b"PIXELS-B", _manifest("aaaaaaaaaaaa"))
    with open(os.path.join(d, "reference-frame-candidate.json"), "w", encoding="utf-8") as fh:
        fh.write('{"status": "candidate-unreviewed", "trunc')
    with open(os.path.join(d, "reference-frame-candidate.invalid"), "w", encoding="utf-8") as fh:
        fh.write("{}")

    assert index_capture_records(str(ob)) == {}


@pytest.mark.parametrize("field", ["executable", "clip"])
@pytest.mark.parametrize("value", ["abc", 5, True, ["x"], []], ids=["string", "number", "bool", "list", "empty-list"])
def test_a_manifest_whose_nested_executable_or_clip_is_not_an_object_gives_an_unreadable_row_not_a_crash(tmp_path, field, value):
    """R18-ADVISORY-TAIL-572-573-574-576-1 (Dot R18 advisory on #576): ``_identity`` did ``(manifest.get("executable") or {}).get("sha256")``, so a truthy
    scalar such as ``"executable": "abc"`` raised AttributeError outside the unreadable-manifest guard and aborted the report for every other frame. A
    non-object nested field now leaves its sha None and the frame is recorded the way an unreadable manifest is: typed ``provenance``, pixel hash,
    manifest path, not counted as matched, no half identity."""
    ob = tmp_path / "outbox"
    manifest = _manifest("aaaaaaaaaaaa")
    manifest[field] = value
    bad = _capture(str(ob), "bad", b"PIXELS-BAD", manifest)
    _capture(str(ob), "good", b"PIXELS-G", _manifest("bbbbbbbbbbbb"))
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "a.png").write_bytes(b"PIXELS-BAD")
    (frames / "z.png").write_bytes(b"PIXELS-G")

    result = build(str(frames), str(ob))

    assert (result["recordsIndexed"], result["matched"], result["orphaned"]) == (2, 1, 1)
    rows = {r["frame"]: r for r in result["frames"]}
    assert list(rows) == ["a.png", "z.png"], "a frame row was dropped"
    assert rows["a.png"]["sha256"] == hashlib.sha256(b"PIXELS-BAD").hexdigest()
    assert rows["a.png"]["provenance"].startswith("unreadable - ")
    assert f"malformed field: {field}" in rows["a.png"]["provenance"]
    assert "commit" not in rows["a.png"], "no half identity on a row that is not counted as matched"
    assert rows["a.png"]["manifest"] == os.path.join(bad, "reference-frame-candidate.json")
    assert rows["z.png"]["commit"] == "bbbbbbbbbbbb" and "provenance" not in rows["z.png"]


def test_identity_gives_none_for_a_nested_sha_whose_parent_is_not_an_object():
    ident = fp._identity({"commit": "c", "executable": "abc", "clip": ["x"]})
    assert ident["exeSha256"] is None and ident["clipSha256"] is None and ident["commit"] == "c"
    ok = fp._identity({"executable": {"sha256": "e" * 64}, "clip": {"sha256": "c" * 64}})
    assert (ok["exeSha256"], ok["clipSha256"]) == ("e" * 64, "c" * 64)


def _capture_under_job(root: str, job: str, pixels: bytes, result_text: str | None) -> str:
    """The runner job's real layout: ``<outbox>/<jobId>.artifacts/{result.json, capture/reference-frame*}``."""
    job_dir = os.path.join(root, f"{job}.artifacts")
    capture = _capture(job_dir, "capture", pixels, _manifest("aaaaaaaaaaaa"))
    if result_text is not None:
        with open(os.path.join(job_dir, "result.json"), "w", encoding="utf-8") as fh:
            fh.write(result_text)
    return capture


def test_a_capture_whose_parent_result_says_capture_failed_is_not_indexed_even_with_no_stamp_marker_or_quarantine(tmp_path):
    """Dot R18 advisory on #573: stamp, marker and quarantine can ALL fail after a capture-failed/225, leaving unchanged candidate bytes (a plain
    ``candidate-unreviewed`` manifest, no marker). The parent ``result.json`` still says ``capture-failed``; the index must honour it."""
    ob = tmp_path / "outbox"
    capture = _capture_under_job(str(ob), "job1", b"PIXELS-A", json.dumps({"status": "capture-failed", "exitCode": 225, "candidateQuarantined": False}))
    skipped: list = []

    assert index_capture_records(str(ob), skipped) == {}
    assert skipped == [{"capture": capture, "reason": "result-capture-failed"}]
    frames = tmp_path / "frames"
    frames.mkdir()
    (frames / "a.png").write_bytes(b"PIXELS-A")
    result = build(str(frames), str(ob))
    assert (result["matched"], result["orphaned"]) == (0, 1)
    assert result["skipped"] == [{"capture": capture, "reason": "result-capture-failed"}]


@pytest.mark.parametrize("result_text", [
    json.dumps({"status": "candidate-unreviewed", "exitCode": 0}),
    "{not json",
    "[]",
    json.dumps({"exitCode": 225}),
    None,
], ids=["candidate-unreviewed", "unparseable", "not-an-object", "no-status", "absent"])
def test_a_parent_result_that_does_not_say_capture_failed_leaves_the_capture_indexable(tmp_path, result_text):
    """Only a readable ``status: capture-failed`` voids a capture; an absent, unreadable or other result keeps the prior behaviour (the residual:
    a result that cannot be read cannot be honoured, so the stamp/marker/quarantine layers remain the only guard there)."""
    ob = tmp_path / "outbox"
    capture = _capture_under_job(str(ob), "job1", b"PIXELS-A", result_text)
    skipped: list = []

    assert index_capture_records(str(ob), skipped) == {hashlib.sha256(b"PIXELS-A").hexdigest(): os.path.join(capture, "reference-frame-candidate.json")}
    assert skipped == []
