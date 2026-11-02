"""LEDGER-APPEND-ATOMIC-BUILD-LOGS-1: the two build logs must not lose lines under concurrent writers.

Survey LEDGER-APPEND-ATOMIC-SURVEY-1 measured pwsh 7 ``Add-Content`` silently losing ~14 % of appends across
6 concurrent writers (it opens FileMode.Append, which records EOF as the stream's private offset, so two
writers overwrite each other's row; an IOException retry does not protect it). Both logs below were
written that way:

  * tools/coordination/build-guard.ps1          -> the exemption log (``Invoke-BuildGuard``)
  * tools/coordination/invoke-mlv-exclusive.ps1 -> ``.claude-state/coordination/build-ownership.jsonl``

Each test starts N writer processes, releases them on a shared wall-clock instant so their appends collide,
and runs the REAL append path M times per writer: build-guard through its documented ``-Library`` mode
(``Invoke-BuildGuard -NoSlot``, memory and process table injected through the existing seams), the
exclusive runner by invoking the script itself. Every line must survive and be well formed. Every child is
bounded by a timeout and killed in ``finally``.

R18-ADVISORY-TAIL-572-573-574-576-1 (Dot R18 advisory on #574): a shared start instant encourages overlap but
cannot prove it on a loaded host (a late-starting writer can finish after an early one is done). So after its
FIRST append each writer signals and then waits, bounded, for every peer to have made its first append; only
then does it do the rest. Each writer records its append interval, and the test asserts from those records
that the intervals overlap. No sleep is involved: the wait is a spin on the signal files, under a timeout.
"""

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "tools" / "coordination" / "build-guard.ps1"
EXCLUSIVE = REPO_ROOT / "tools" / "coordination" / "invoke-mlv-exclusive.ps1"

PWSH = shutil.which("pwsh.exe") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not PWSH, reason="the build logs are written by Windows PowerShell 7 tools"
)

WRITERS = 6
APPENDS_EACH = 40
START_DELAY_SEC = 6
WRITER_TIMEOUT_SEC = 150
HANDSHAKE_TIMEOUT_SEC = 100

# Spin (no sleep) until the shared start instant so the writers' first appends overlap.
_BARRIER = "while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $StartMs) { }\n"

# Append-interval record and bounded handshake (see the module docstring). ``$spanStart`` is taken before the
# first append, ``$readyTicks`` right after it, and ``span<Id>.txt`` ("start ready end", UTC ticks) after the
# last. Signal files ``ready<Id>`` live beside the writer script (``$PSScriptRoot`` is the test's tmp_path).
_SPAN_BEGIN = "$spanStart = [DateTime]::UtcNow.Ticks\n$readyTicks = 0\n"
_SPAN_SYNC = (
    "    if ($i -eq 0) {\n"
    "        $readyTicks = [DateTime]::UtcNow.Ticks\n"
    '        [IO.File]::WriteAllText((Join-Path $PSScriptRoot "ready$Id"), [string]$readyTicks)\n'
    "        $give = [Diagnostics.Stopwatch]::StartNew()\n"
    f"        while ([IO.Directory]::GetFiles($PSScriptRoot, 'ready*').Count -lt {WRITERS}) {{\n"
    f"            if ($give.Elapsed.TotalSeconds -gt {HANDSHAKE_TIMEOUT_SEC}) {{ throw 'append-overlap handshake timed out: a peer writer never made its first append' }}\n"
    "        }\n"
    "    }\n"
)
_SPAN_END = '[IO.File]::WriteAllText((Join-Path $PSScriptRoot "span$Id.txt"), "$spanStart $readyTicks $([DateTime]::UtcNow.Ticks)")\n'

GUARD_WRITER = (
    "param([string]$Guard, [string]$LogPath, [string]$Probe, [string]$Table, [int]$Id, [int]$Count, [long]$StartMs)\n"
    "$ErrorActionPreference = 'Stop'\n"
    ". $Guard -Library\n"
    + _BARRIER
    + _SPAN_BEGIN
    + "for ($i = 0; $i -lt $Count; $i++) {\n"
    "    $opts = @{ ExemptLogPath = $LogPath; MemoryProbeFile = $Probe; ProcessTableFile = $Table }\n"
    "    Invoke-BuildGuard -Label \"w$Id\" -ExemptReason \"w$Id-i$i\" -NoSlot -Options $opts -Action { param($Jobs) } 2>$null | Out-Null\n"
    + _SPAN_SYNC
    + "}\n"
    + _SPAN_END
)

EXCLUSIVE_WRITER = (
    "param([string]$Script, [string]$Root, [string]$Exe, [int]$Id, [int]$Count, [long]$StartMs)\n"
    + _BARRIER
    + _SPAN_BEGIN
    + "for ($i = 0; $i -lt $Count; $i++) {\n"
    # A writer that loses the exclusive lock throws 'busy'; its finally block still appends its record.
    "    try { & $Script -RepoRoot $Root -Owner \"w$Id-i$i\" -FilePath $Exe -ArgumentList @('/c', 'exit 0') -TimeoutSeconds 60 *> $null } catch { }\n"
    + _SPAN_SYNC
    + "}\n"
    + _SPAN_END
)


def _read_spans(tmp_path: Path) -> list:
    """Each writer's recorded (start, ready, end) UTC ticks: before its first append, after it, after its last."""
    spans = []
    for writer_id in range(WRITERS):
        record = tmp_path / f"span{writer_id}.txt"
        assert record.is_file(), f"writer {writer_id} left no append-interval record"
        start, ready, end = (int(part) for part in record.read_text(encoding="ascii").split())
        assert 0 < start <= ready <= end, f"writer {writer_id} recorded an impossible interval {(start, ready, end)} (no first-append signal?)"
        spans.append((start, ready, end))
    return spans


def _assert_appends_overlapped(spans: list) -> None:
    """The writers' append intervals overlap, judged from what each recorded rather than from the shared start."""
    latest_start = max(start for start, _, _ in spans)
    latest_ready = max(ready for _, ready, _ in spans)
    earliest_end = min(end for _, _, end in spans)
    assert latest_start < earliest_end, (
        f"no instant at which every writer was inside its append loop (latest start {latest_start}, earliest end {earliest_end})"
    )
    assert latest_ready <= earliest_end, (
        f"a writer finished before a peer made its first append, so the loops were serialised (latest first-append {latest_ready}, earliest end {earliest_end})"
    )


def test_overlap_check_accepts_overlapping_intervals_and_rejects_serialised_ones() -> None:
    overlapped = [(100 + w, 110 + w, 200 + w) for w in range(WRITERS)]
    _assert_appends_overlapped(overlapped)
    # Writer 0 is done before writer 1 even starts: a shared start instant would not have noticed.
    serialised = [(100, 110, 150)] + [(160 + w, 170 + w, 300 + w) for w in range(WRITERS - 1)]
    with pytest.raises(AssertionError, match="no instant at which every writer"):
        _assert_appends_overlapped(serialised)
    # Everyone started together, but writer 0 had finished before the last peer made its first append.
    late_first_append = [(100, 110, 150)] + [(100, 160, 300)] + [(100, 120, 300)] * (WRITERS - 2)
    with pytest.raises(AssertionError, match="serialised"):
        _assert_appends_overlapped(late_first_append)


def _run_writers(tmp_path: Path, script_text: str, args_for, start_delay_sec: int = START_DELAY_SEC,
                 timeout_sec: int = WRITER_TIMEOUT_SEC) -> None:
    """Start WRITERS pwsh children on one start instant; wait for all; kill every one in finally.

    Child output goes to files, not pipes: build-guard echoes each exemption line to stderr, and a writer
    blocked on a full pipe while the parent drains an earlier writer would serialise the writers and hide
    the very collisions these tests exist to cause.
    """
    script = tmp_path / "writer.ps1"
    script.write_text(script_text, encoding="ascii")
    start_ms = int((time.time() + start_delay_sec) * 1000)
    procs = []
    handles = []
    try:
        for writer_id in range(WRITERS):
            out_fh = open(tmp_path / f"writer{writer_id}.out", "wb")
            err_fh = open(tmp_path / f"writer{writer_id}.err", "wb")
            handles += [out_fh, err_fh]
            procs.append(
                subprocess.Popen(
                    [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", str(script), *args_for(writer_id, start_ms)],
                    stdin=subprocess.DEVNULL, stdout=out_fh, stderr=err_fh,
                )
            )
        deadline = time.monotonic() + timeout_sec
        for writer_id, proc in enumerate(procs):
            proc.wait(timeout=max(1.0, deadline - time.monotonic()))
            err = (tmp_path / f"writer{writer_id}.err").read_text(encoding="utf-8", errors="replace")
            assert proc.returncode == 0, f"writer {writer_id} failed rc={proc.returncode}\n{err[-2000:]}"
        _assert_appends_overlapped(_read_spans(tmp_path))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        for fh in handles:
            fh.close()


def test_build_guard_exempt_log_keeps_every_concurrent_append(tmp_path: Path) -> None:
    probe = tmp_path / "mem.json"
    probe.write_text(json.dumps([{"freePhysGB": 10, "freeCommitGB": 20}]), encoding="ascii")
    table = tmp_path / "procs.json"
    table.write_text("[]", encoding="ascii")
    log = tmp_path / "exempt.log"

    _run_writers(
        tmp_path,
        GUARD_WRITER,
        lambda i, start_ms: [str(GUARD), str(log), str(probe), str(table), str(i), str(APPENDS_EACH), str(start_ms)],
    )

    raw = log.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "the exemption log is ASCII; no BOM"
    assert raw.isascii()
    all_lines = raw.decode("ascii").splitlines()
    lines = [line for line in all_lines if line]
    shape = re.compile(r"^BUILD_GUARD_EXEMPT label=w(\d+) reason=w\1-i(\d+) utc=\S+ pid=\d+$")
    expected = sorted((str(w), str(i)) for w in range(WRITERS) for i in range(APPENDS_EACH))
    assert len(lines) == WRITERS * APPENDS_EACH, (
        f"lost {WRITERS * APPENDS_EACH - len(lines)} of {WRITERS * APPENDS_EACH} appends ({len(all_lines) - len(lines)} blank line(s))"
    )
    bad = [line for line in all_lines if not shape.match(line)]
    assert not bad, f"{len(bad)} malformed line(s), first: {bad[0]!r}"
    assert sorted(shape.match(line).group(1, 2) for line in lines) == expected
    assert raw.endswith(b"\r\n"), "Add-Content ended each line with the platform newline; keep it"


def test_exclusive_runner_ownership_log_keeps_every_concurrent_append(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    cmd = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"

    _run_writers(
        tmp_path,
        EXCLUSIVE_WRITER,
        lambda i, start_ms: [str(EXCLUSIVE), str(root), cmd, str(i), str(APPENDS_EACH), str(start_ms)],
    )

    log = root / ".claude-state" / "coordination" / "build-ownership.jsonl"
    raw = log.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "pwsh 7 Add-Content -Encoding UTF8 writes no BOM; keep that"
    all_lines = raw.decode("utf-8").splitlines()
    lines = [line for line in all_lines if line]
    expected = sorted(f"w{w}-i{i}" for w in range(WRITERS) for i in range(APPENDS_EACH))
    assert len(lines) == WRITERS * APPENDS_EACH, (
        f"lost {WRITERS * APPENDS_EACH - len(lines)} of {WRITERS * APPENDS_EACH} appends ({len(all_lines) - len(lines)} blank line(s))"
    )
    owners = []
    for line in all_lines:
        try:
            record = json.loads(line)
        except ValueError:
            pytest.fail(f"malformed ownership line: {line[:120]!r}")
        assert {"owner", "startTimeUtc", "outcome", "endTimeUtc"} <= set(record), record
        owners.append(record["owner"])
    assert sorted(owners) == expected
    assert raw.endswith(b"\r\n"), "Add-Content ended each line with the platform newline; keep it"


# --- LEDGER-APPEND-ATOMIC-LARGE-RECORD-PINS-1 ------------------------------------------------------------
# The tests above only write short ASCII records. The claim #569 made is wider: one FILE_APPEND_DATA write
# lands at the current end of file as a unit, whatever its size and bytes. These tests pin that for records
# straddling and well above one 4096-byte buffer (the FileStream buffer size both scripts pass) and for
# non-ASCII values, with every record checked byte for byte against what its writer was asked to write.
#
# Each writer's records are distinguishable from every other writer's at every offset (the filler walks a
# per-writer rotation), so a record that was split into two writes, or interleaved with another writer's
# bytes, cannot read back as a valid record of its own writer.

LARGE_RECORDS_EACH = 18
LARGE_START_DELAY_SEC = 10
LARGE_WRITER_TIMEOUT_SEC = 240

# (non-ASCII?, filler length in characters). Lengths under, at and far over one 4096-byte write: the guard line
# adds about 80 bytes of framing and the ownership record about 230, so 3800..4030 puts records on both sides.
_LARGE_KINDS = [
    (False, 6000), (True, 6000), (False, 9000), (True, 3800), (False, 3950), (True, 9000), (False, 7500), (True, 4030),
]
# BMP 2-byte (e-acute, A-macron), BMP 3-byte (euro, CJK), a supplementary-plane character (a UTF-16 surrogate
# pair in .NET, 4 bytes in UTF-8) and one ASCII letter.
_NON_ASCII_CYCLE = "a\u00e9\u20ac\u6f22\U0001F600\u0100"


def _large_reasons(writer_id: int, unicode_scale: float) -> list:
    """The values writer ``writer_id`` is asked to append, in order. ``unicode_scale`` shrinks the non-ASCII
    runs where a character costs several output bytes, so the record sizes land in the same band."""
    reasons = []
    for i in range(LARGE_RECORDS_EACH):
        non_ascii, length = _LARGE_KINDS[(writer_id + i) % len(_LARGE_KINDS)]
        head = f"w{writer_id}-i{i}-"
        if non_ascii:
            n = int(length * unicode_scale)
            body = "".join(_NON_ASCII_CYCLE[(writer_id + k) % len(_NON_ASCII_CYCLE)] for k in range(n))
        else:
            body = "".join(chr(ord("a") + (writer_id * 7 + k) % 26) for k in range(length))
        reasons.append(head + body)
    return reasons


def _write_reasons(tmp_path: Path, unicode_scale: float) -> dict:
    """Write one ASCII-only JSON file of reasons per writer (``\\u`` escapes keep the file encoding-proof)."""
    expected = {}
    for writer_id in range(WRITERS):
        reasons = _large_reasons(writer_id, unicode_scale)
        (tmp_path / f"reasons{writer_id}.json").write_text(json.dumps(reasons), encoding="ascii")
        for i, reason in enumerate(reasons):
            expected[(writer_id, i)] = reason
    return expected


def _dotnet_ascii(text: str) -> bytes:
    """What ``[Text.Encoding]::ASCII.GetBytes`` writes: ASCII as is, one '?' per UTF-16 code unit otherwise."""
    return "".join(c if c.isascii() else "?" * (len(c.encode("utf-16-le")) // 2) for c in text).encode("ascii")


def _split_records(raw: bytes) -> list:
    """Split on the exact CRLF terminator (``str.splitlines`` would also split on U+0085, U+2028 and friends)."""
    assert raw.endswith(b"\r\n"), "every record ends with the platform newline, including the last"
    parts = raw.split(b"\r\n")
    assert parts.pop() == b""
    return parts


def _report_sizes(label: str, lines: list, elapsed: float) -> None:
    sizes = [len(line) + 2 for line in lines]
    print(f"[large-record:{label}] records={len(lines)} bytes min={min(sizes)} max={max(sizes)} "
          f"over4096={sum(s > 4096 for s in sizes)} elapsed={elapsed:.1f}s")


GUARD_LARGE_WRITER = (
    "param([string]$Guard, [string]$LogPath, [string]$Probe, [string]$Table, [string]$ReasonsFile, [int]$Id, [long]$StartMs)\n"
    "$ErrorActionPreference = 'Stop'\n"
    ". $Guard -Library\n"
    "$reasons = @([IO.File]::ReadAllText($ReasonsFile) | ConvertFrom-Json)\n"
    + _BARRIER
    + _SPAN_BEGIN
    + "for ($i = 0; $i -lt $reasons.Count; $i++) {\n"
    "    $opts = @{ ExemptLogPath = $LogPath; MemoryProbeFile = $Probe; ProcessTableFile = $Table }\n"
    "    Invoke-BuildGuard -Label \"w$Id\" -ExemptReason $reasons[$i] -NoSlot -Options $opts -Action { param($Jobs) } 2>$null | Out-Null\n"
    + _SPAN_SYNC
    + "}\n"
    + _SPAN_END
)

EXCLUSIVE_LARGE_WRITER = (
    "param([string]$Script, [string]$Root, [string]$Exe, [string]$ReasonsFile, [int]$Id, [long]$StartMs)\n"
    "$reasons = @([IO.File]::ReadAllText($ReasonsFile) | ConvertFrom-Json)\n"
    + _BARRIER
    + _SPAN_BEGIN
    + "for ($i = 0; $i -lt $reasons.Count; $i++) {\n"
    # A writer that loses the exclusive lock throws 'busy'; its finally block still appends its record.
    "    try { & $Script -RepoRoot $Root -Owner $reasons[$i] -FilePath $Exe -ArgumentList @('/c', 'exit 0') -TimeoutSeconds 60 *> $null } catch { }\n"
    + _SPAN_SYNC
    + "}\n"
    + _SPAN_END
)


def test_build_guard_exempt_log_large_and_non_ascii_records_are_whole(tmp_path: Path) -> None:
    probe = tmp_path / "mem.json"
    probe.write_text(json.dumps([{"freePhysGB": 10, "freeCommitGB": 20}]), encoding="ascii")
    table = tmp_path / "procs.json"
    table.write_text("[]", encoding="ascii")
    log = tmp_path / "exempt.log"
    # build-guard writes the log through [Text.Encoding]::ASCII, one output byte per character.
    expected = _write_reasons(tmp_path, unicode_scale=1.0)

    started = time.monotonic()
    _run_writers(
        tmp_path,
        GUARD_LARGE_WRITER,
        lambda i, start_ms: [str(GUARD), str(log), str(probe), str(table), str(tmp_path / f"reasons{i}.json"),
                             str(i), str(start_ms)],
        start_delay_sec=LARGE_START_DELAY_SEC,
        timeout_sec=LARGE_WRITER_TIMEOUT_SEC,
    )
    elapsed = time.monotonic() - started

    raw = log.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "the exemption log is ASCII; no BOM"
    assert raw.isascii(), "non-ASCII input must be transcoded, never written as raw multi-byte sequences"
    lines = _split_records(raw)
    _report_sizes("build-guard", lines, elapsed)
    assert len(lines) == WRITERS * LARGE_RECORDS_EACH, (
        f"expected {WRITERS * LARGE_RECORDS_EACH} records, found {len(lines)}"
    )
    shape = re.compile(rb"BUILD_GUARD_EXEMPT label=w(\d+) reason=(w(\d+)-i(\d+)-\S*) utc=\S+ pid=\d+")
    seen = set()
    for line in lines:
        m = shape.fullmatch(line)
        assert m, f"record split, interleaved or malformed ({len(line)} bytes): {line[:100]!r} ... {line[-60:]!r}"
        key = (int(m.group(3)), int(m.group(4)))
        assert int(m.group(1)) == key[0], f"label/reason writer mismatch, a record was spliced: {line[:100]!r}"
        assert key not in seen, f"record {key} appears twice"
        seen.add(key)
        # What the script does with non-ASCII: every UTF-16 code unit becomes one '?' (observed: .NET's ASCII
        # replacement fallback turns a surrogate pair into '??'); the record stays one whole line.
        assert m.group(2) == _dotnet_ascii(expected[key]), (
            f"record {key} is not byte-identical to what its writer appended"
        )
    assert seen == set(expected)
    sizes = [len(line) + 2 for line in lines]
    assert max(sizes) > 8192 and sum(s > 4096 for s in sizes) >= len(sizes) // 2, "the test must really write large records"


def test_exclusive_runner_ownership_log_large_and_non_ascii_records_are_whole(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    cmd = os.environ.get("ComSpec") or r"C:\Windows\System32\cmd.exe"
    # The ownership log is UTF-8: a non-ASCII character costs 2-4 bytes, so shrink those runs to keep sizes in band.
    expected = _write_reasons(tmp_path, unicode_scale=0.4)

    started = time.monotonic()
    _run_writers(
        tmp_path,
        EXCLUSIVE_LARGE_WRITER,
        lambda i, start_ms: [str(EXCLUSIVE), str(root), cmd, str(tmp_path / f"reasons{i}.json"), str(i), str(start_ms)],
        start_delay_sec=LARGE_START_DELAY_SEC,
        timeout_sec=LARGE_WRITER_TIMEOUT_SEC,
    )
    elapsed = time.monotonic() - started

    log = root / ".claude-state" / "coordination" / "build-ownership.jsonl"
    raw = log.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 without a BOM"
    lines = _split_records(raw)
    _report_sizes("invoke-mlv-exclusive", lines, elapsed)
    assert len(lines) == WRITERS * LARGE_RECORDS_EACH, (
        f"expected {WRITERS * LARGE_RECORDS_EACH} records, found {len(lines)}"
    )
    seen = set()
    for line in lines:
        try:
            record = json.loads(line.decode("utf-8"))  # strict: a split inside a multi-byte character fails here
        except ValueError:
            pytest.fail(f"record split, interleaved or not valid UTF-8 JSON ({len(line)} bytes): {line[:100]!r} ... {line[-60:]!r}")
        assert {"owner", "startTimeUtc", "outcome", "endTimeUtc"} <= set(record), record
        owner = record["owner"]
        m = re.match(r"w(\d+)-i(\d+)-", owner)
        assert m, f"owner lost its head: {owner[:60]!r}"
        key = (int(m.group(1)), int(m.group(2)))
        assert key not in seen, f"record {key} appears twice"
        seen.add(key)
        assert owner == expected[key], f"record {key} did not round-trip byte-identical"
    assert seen == set(expected)
    sizes = [len(line) + 2 for line in lines]
    assert max(sizes) > 8192 and sum(s > 4096 for s in sizes) >= len(sizes) // 2, "the test must really write large records"
