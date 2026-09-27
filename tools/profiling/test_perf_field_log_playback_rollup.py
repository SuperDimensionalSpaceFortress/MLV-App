"""Falsifier tests for perf_field_log_playback_rollup.

Each test is written to FAIL if the matching check quietly degrades to something
weaker -- accepting a rollup with extra keys, treating a missing clock field as
optional, or accepting a truncated/absent executable hash.
"""
from __future__ import annotations

import copy

from perf_field_log_playback_rollup import (
    ASYNC_H2D_KEYS,
    KIND,
    ROLLUP_KEY,
    SCHEMA,
    parse_ndjson,
    playback_records,
    validate_playback_record,
)


def _clean_rollup() -> dict:
    # Mirrors buildAsyncH2dRollup()'s two field shapes: boolean fields carry
    # samples/true_frames/false_frames, timing fields carry samples/mean_ms/max_ms.
    boolean_keys = ASYNC_H2D_KEYS[:8]
    timing_keys = ASYNC_H2D_KEYS[8:]
    rollup = {}
    for key in boolean_keys:
        rollup[key] = {"samples": 12, "true_frames": 9, "false_frames": 3}
    for key in timing_keys:
        rollup[key] = {"samples": 12, "mean_ms": 1.5, "max_ms": 4.0}
    return rollup


def _clean_record() -> dict:
    return {
        "schema": SCHEMA,
        "kind": KIND,
        ROLLUP_KEY: _clean_rollup(),
        "playback_prep_region_clock_resolution_ns": 100,
        "playback_prep_region_clock_monotonic": True,
        "machineFingerprint": {
            "build_sha": "a" * 40,
            "executable_sha256": "e" * 64,
        },
    }


def test_accepts_a_clean_record():
    assert validate_playback_record(_clean_record()) == []


def test_accepts_a_one_frame_mismatch_rollup():
    # ASYNC-H2D-FRAME-BINDING-1 sol-flagged blocker repro: MainWindow's
    # compare-and-reject emits gpu_playback_recon_async_h2d_frame_id_mismatch into
    # the session rollup; a one-frame session whose rollup carries only that key
    # (as buildAsyncH2dRollup() would produce when every other key's inputs were
    # absent that frame) must not be rejected as "unknown key(s)".
    record = _clean_record()
    record[ROLLUP_KEY] = {
        "gpu_playback_recon_async_h2d_frame_id_mismatch": {
            "samples": 1, "true_frames": 1, "false_frames": 0,
        },
    }
    assert validate_playback_record(record) == []


def test_rejects_bogus_eleventh_key_in_rollup():
    record = _clean_record()
    record[ROLLUP_KEY]["gpu_playback_recon_async_h2d_bogus_field"] = {
        "samples": 1, "mean_ms": 0.1, "max_ms": 0.1,
    }
    errors = validate_playback_record(record)
    assert any("bogus_field" in e for e in errors), errors


def test_rejects_missing_rollup():
    record = _clean_record()
    del record[ROLLUP_KEY]
    errors = validate_playback_record(record)
    assert any(ROLLUP_KEY in e for e in errors), errors


def test_rejects_rollup_with_wrong_type():
    record = _clean_record()
    record[ROLLUP_KEY] = "not-an-object"
    errors = validate_playback_record(record)
    assert any(ROLLUP_KEY in e for e in errors), errors


def test_rejects_missing_clock_resolution_field():
    record = _clean_record()
    del record["playback_prep_region_clock_resolution_ns"]
    errors = validate_playback_record(record)
    assert any("clock_resolution_ns" in e for e in errors), errors


def test_rejects_non_boolean_clock_monotonic_field():
    record = _clean_record()
    record["playback_prep_region_clock_monotonic"] = 1
    errors = validate_playback_record(record)
    assert any("clock_monotonic" in e for e in errors), errors


def test_rejects_missing_executable_sha256():
    record = _clean_record()
    del record["machineFingerprint"]["executable_sha256"]
    errors = validate_playback_record(record)
    assert any("executable_sha256" in e for e in errors), errors


def test_rejects_truncated_executable_sha256():
    record = _clean_record()
    record["machineFingerprint"]["executable_sha256"] = "e" * 10
    errors = validate_playback_record(record)
    assert any("executable_sha256" in e for e in errors), errors


def test_rejects_build_sha_masquerading_as_executable_sha256():
    # build_sha is a ~7-40 char git commit hash, not a 64-char content hash -- a
    # regression that wired build_sha into the wrong field must still fail.
    record = _clean_record()
    record["machineFingerprint"]["executable_sha256"] = record["machineFingerprint"]["build_sha"]
    errors = validate_playback_record(record)
    assert any("executable_sha256" in e for e in errors), errors


def test_playback_records_filters_out_other_kinds_and_schemas():
    records = [
        _clean_record(),
        {**copy.deepcopy(_clean_record()), "kind": "export"},
        {**copy.deepcopy(_clean_record()), "schema": "mlvapp.other-schema.v1"},
    ]
    assert len(playback_records(records)) == 1


def test_parse_ndjson_skips_a_torn_trailing_line():
    text = '{"a": 1}\n{"a": 2}\n{"a": 3, "trunc'
    assert parse_ndjson(text) == [{"a": 1}, {"a": 2}]


def test_end_to_end_ndjson_line_round_trip():
    import json

    line = json.dumps(_clean_record())
    records = playback_records(parse_ndjson(line))
    assert len(records) == 1
    assert validate_playback_record(records[0]) == []
