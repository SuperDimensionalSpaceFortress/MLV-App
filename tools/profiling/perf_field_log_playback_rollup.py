"""Validate the playback record of the mlvapp.perf-field-log.v1 NDJSON sink.

C2-TELEM-2: the sink already carried ``gpu_playback_recon_async_h2d_last_presented``
(the last frame's async-H2D fields only) with no session-wide rollup, no clock-identity
fields, and no byte-hash of the running executable (only ``machineFingerprint.build_sha``,
the git commit the build claims -- which says nothing about the bytes actually on disk).
``finishPlaybackSmokeTelemetry`` in platform/qt/MainWindow.cpp now also emits
``gpu_playback_recon_async_h2d_session_rollup`` (mean/max/sample-count per key, aggregated
across every presented frame, not just the last one), the ``playback_prep_region_clock_*``
fields, and ``machineFingerprint.executable_sha256``. This module is the oracle for that
contract; it has no prior version to extend because none of this was checked before.
"""
from __future__ import annotations

import json
from typing import Any

SCHEMA = "mlvapp.perf-field-log.v1"
KIND = "playback"

# Mirrors gpuPlaybackReconAsyncH2dTelemetryKeys() in platform/qt/MainWindow.cpp.
# Kept as an explicit list (not "whatever keys happen to show up") so a key added on
# one side and not the other is a loud failure instead of a silent pass.
ASYNC_H2D_KEYS = [
    "gpu_playback_recon_async_h2d_env_enabled",
    "gpu_playback_recon_async_h2d_available",
    "gpu_playback_recon_async_h2d_accepted",
    "gpu_playback_recon_async_h2d_used",
    "gpu_playback_recon_async_h2d_exact_match",
    "gpu_playback_recon_async_h2d_submitted_while_prior_run_active",
    "gpu_playback_recon_async_h2d_ready_before_run",
    "gpu_playback_recon_async_h2d_frame_id_mismatch",
    "gpu_playback_recon_async_h2d_host_staging_ms",
    "gpu_playback_recon_async_h2d_upload_ms",
    "gpu_playback_recon_async_h2d_upload_wait_ms",
]

ROLLUP_KEY = "gpu_playback_recon_async_h2d_session_rollup"
CLOCK_RESOLUTION_KEY = "playback_prep_region_clock_resolution_ns"
CLOCK_MONOTONIC_KEY = "playback_prep_region_clock_monotonic"


def parse_ndjson(text: str) -> list[dict[str, Any]]:
    """Parse a perf-field-log file's contents. Malformed lines are skipped, not fatal --
    the sink appends one JSON object per line and a torn last line (killed mid-write) is
    expected, not corruption."""
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def playback_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter to playback records; the sink also carries other kinds (e.g. export)."""
    return [r for r in records if r.get("schema") == SCHEMA and r.get("kind") == KIND]


def validate_playback_record(record: dict[str, Any]) -> list[str]:
    """Return a list of contract violations against one playback record; [] means clean.

    The rollup check is exact-membership, not "at least these keys present": a stray
    extra key means the C++ emitter and this oracle have drifted apart on what the
    schema is, and that must fail loudly rather than pass because every expected key
    also happened to be there.
    """
    errors: list[str] = []

    rollup = record.get(ROLLUP_KEY)
    if not isinstance(rollup, dict):
        errors.append(f"missing or non-object '{ROLLUP_KEY}'")
    else:
        unknown = sorted(set(rollup.keys()) - set(ASYNC_H2D_KEYS))
        if unknown:
            errors.append(f"unknown key(s) in '{ROLLUP_KEY}': {unknown}")

    resolution = record.get(CLOCK_RESOLUTION_KEY)
    if not isinstance(resolution, (int, float)) or isinstance(resolution, bool):
        errors.append(f"missing or non-numeric '{CLOCK_RESOLUTION_KEY}'")

    if not isinstance(record.get(CLOCK_MONOTONIC_KEY), bool):
        errors.append(f"missing or non-boolean '{CLOCK_MONOTONIC_KEY}'")

    fingerprint = record.get("machineFingerprint")
    exe_sha = fingerprint.get("executable_sha256") if isinstance(fingerprint, dict) else None
    if not isinstance(exe_sha, str) or len(exe_sha) != 64:
        errors.append("missing or malformed 'machineFingerprint.executable_sha256'")

    return errors
