"""FACTORY-PINS-INSTALL-1: guard the installed factory-pins/ so a bus tool can be run BY PIN.

Sits beside the pin-runner route test in test_doctrine_outbox_hooks.py. Fails when
factory-pins/factory-pins.json is missing or malformed, when the vendored runner is not the bytes it
records, or when a pinned tool's bytes do not hash to the recorded blob id, byte count and sha256.

Hosted CI has no bus clone, so the pinned tool bytes travel as DATA (a .py.txt under
fixtures/factory-pins-bytes/, outside the python census), named by the blob id they must equal.
There is no skip path: a missing fixture FAILS. Refresh after a re-pin: write the new bus blob to
<tool>.<blob[:12]>.py.txt and delete the old one.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PINS_DIR = REPO / "factory-pins"
PINS_FILE = PINS_DIR / "factory-pins.json"
RUNNER = PINS_DIR / "pin-runner.py"
BYTES_DIR = Path(__file__).resolve().parent / "fixtures" / "factory-pins-bytes"

SCHEMA = "factory-pins.v1"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
TOOL_KEYS = {"path", "commit", "blob", "bytes", "sha256", "interpreter", "reviewer", "reason", "pinned_utc"}
RUNNER_KEYS = {"commit", "path", "blob", "bytes", "sha256"}
REQUIRED_TOOLS = ("check-doc-size",)


def load_pins(path: Path = PINS_FILE) -> dict:
    assert path.is_file(), f"{path.relative_to(REPO) if path.is_relative_to(REPO) else path} is missing: factory-pins/ is not installed"
    try:
        pins = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        pytest.fail(f"factory-pins.json is not valid JSON: {exc}")
    return pins


def malformed(pins: dict) -> list[str]:
    """Every way the pins file is not a well-formed factory-pins.v1 record (empty list = well formed)."""
    bad: list[str] = []
    if not isinstance(pins, dict) or pins.get("schema") != SCHEMA:
        return [f"schema is not {SCHEMA!r}"]
    runner, tools = pins.get("runner"), pins.get("tools")
    if not isinstance(runner, dict) or set(runner) != RUNNER_KEYS:
        bad.append(f"runner must carry exactly {sorted(RUNNER_KEYS)}")
    elif not (HEX40.match(str(runner["commit"])) and HEX40.match(str(runner["blob"]))
              and HEX64.match(str(runner["sha256"])) and type(runner["bytes"]) is int):
        bad.append("runner commit/blob/sha256/bytes are not 40-hex/40-hex/64-hex/int")
    if not isinstance(tools, dict) or not tools:
        return bad + ["tools must be a non-empty object"]
    for name in REQUIRED_TOOLS:
        if name not in tools:
            bad.append(f"tool {name!r} is not pinned")
    for name, t in tools.items():
        if not isinstance(t, dict) or not TOOL_KEYS <= set(t):
            bad.append(f"{name}: missing {sorted(TOOL_KEYS - set(t if isinstance(t, dict) else {}))}")
        elif not (HEX40.match(str(t["commit"])) and HEX40.match(str(t["blob"])) and HEX64.match(str(t["sha256"]))
                  and type(t["bytes"]) is int and t["interpreter"] in ("python", "node")):
            bad.append(f"{name}: commit/blob/sha256/bytes/interpreter are malformed")
    return bad


def hash_mismatch(label: str, record: dict, data: bytes) -> str | None:
    """None when `data` is exactly what `record` pins (git blob id, byte count and sha256), else why not."""
    if len(data) != record["bytes"]:
        return f"{label}: {len(data)} bytes, the pin records {record['bytes']}"
    if hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() != record["blob"]:
        return f"{label}: does not hash to the pinned git blob {record['blob'][:12]}"
    if hashlib.sha256(data).hexdigest() != record["sha256"]:
        return f"{label}: does not hash to the pinned sha256 {record['sha256'][:12]}"
    return None


def pinned_fixture(name: str, record: dict) -> bytes:
    fixture = BYTES_DIR / f"{name}.{record['blob'][:12]}.py.txt"
    assert fixture.is_file(), (f"no committed bytes for pinned tool {name!r}: expected "
                               f"{fixture.relative_to(REPO)} (the blob the pin names)")
    return fixture.read_bytes().replace(b"\r\n", b"\n")


def test_pins_file_is_present_and_well_formed():
    assert malformed(load_pins()) == []


def test_vendored_runner_is_the_recorded_bus_blob():
    pins = load_pins()
    assert RUNNER.is_file(), "factory-pins/pin-runner.py is missing"
    # Raw bytes, no CRLF normalisation: a CRLF checkout breaks the runner's own identity check.
    assert hash_mismatch("factory-pins/pin-runner.py", pins["runner"], RUNNER.read_bytes()) is None


def test_every_pinned_tool_hashes_to_its_recorded_value():
    pins = load_pins()
    assert pins["tools"], "no tool is pinned"
    for name, record in pins["tools"].items():
        assert hash_mismatch(name, record, pinned_fixture(name, record)) is None


def test_a_one_byte_change_to_a_pinned_tool_is_caught():
    record = load_pins()["tools"]["check-doc-size"]
    data = pinned_fixture("check-doc-size", record)
    assert hash_mismatch("same size", record, data[:-1] + bytes([data[-1] ^ 1])) is not None
    assert hash_mismatch("shorter", record, data[:-1]) is not None


def test_a_malformed_pins_file_is_reported():
    pins = load_pins()
    for mutate in (
        lambda p: p.update(schema="factory-pins.v0"),
        lambda p: p["tools"]["check-doc-size"].update(blob="deadbeef"),
        lambda p: p["tools"]["check-doc-size"].pop("sha256"),
        lambda p: p["tools"].pop("check-doc-size"),
        lambda p: p.pop("runner"),
    ):
        broken = json.loads(json.dumps(pins))
        mutate(broken)
        assert malformed(broken), "a broken pins file was accepted"


def consumer(tmp_path: Path, pins: dict) -> tuple[Path, dict[str, str]]:
    """A scratch consumer holding the vendored runner, `pins`, and a blob cache seeded from the fixtures."""
    root = tmp_path / "consumer"
    (root / "factory-pins").mkdir(parents=True)
    shutil.copyfile(RUNNER, root / "factory-pins" / "pin-runner.py")
    (root / "factory-pins" / "factory-pins.json").write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")
    cache = tmp_path / "cache"
    cache.mkdir()
    for name, record in load_pins()["tools"].items():
        (cache / record["blob"]).write_bytes(pinned_fixture(name, record))
    env = {k: v for k, v in os.environ.items() if k != "FACTORY_BUS_CLONE"}
    env.update(FACTORY_PINS_CACHE=str(cache), PYTHONUTF8="1")
    return root, env


def run_pinned(root: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", "factory-pins/pin-runner.py", "run", "check-doc-size", "--", *args],
                          cwd=root, env=env, capture_output=True, text=True, timeout=120)


def test_the_runner_executes_the_pinned_bytes_without_a_bus_clone(tmp_path):
    root, env = consumer(tmp_path, load_pins())
    done = run_pinned(root, env, "--help")
    assert done.returncode == 0, done.stderr
    assert "--config" in done.stdout, "the pinned check-doc-size did not run"


def test_the_runner_refuses_a_pin_whose_recorded_hash_is_tampered(tmp_path):
    tampered = load_pins()
    tampered["tools"]["check-doc-size"]["sha256"] = "0" * 64
    root, env = consumer(tmp_path, tampered)
    done = run_pinned(root, env, "--help")
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
