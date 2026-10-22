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

# Spin (no sleep) until the shared start instant so the writers' first appends overlap.
_BARRIER = "while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $StartMs) { }\n"

GUARD_WRITER = (
    "param([string]$Guard, [string]$LogPath, [string]$Probe, [string]$Table, [int]$Id, [int]$Count, [long]$StartMs)\n"
    "$ErrorActionPreference = 'Stop'\n"
    ". $Guard -Library\n"
    + _BARRIER
    + "for ($i = 0; $i -lt $Count; $i++) {\n"
    "    $opts = @{ ExemptLogPath = $LogPath; MemoryProbeFile = $Probe; ProcessTableFile = $Table }\n"
    "    Invoke-BuildGuard -Label \"w$Id\" -ExemptReason \"w$Id-i$i\" -NoSlot -Options $opts -Action { param($Jobs) } 2>$null | Out-Null\n"
    "}\n"
)

EXCLUSIVE_WRITER = (
    "param([string]$Script, [string]$Root, [string]$Exe, [int]$Id, [int]$Count, [long]$StartMs)\n"
    + _BARRIER
    + "for ($i = 0; $i -lt $Count; $i++) {\n"
    # A writer that loses the exclusive lock throws 'busy'; its finally block still appends its record.
    "    try { & $Script -RepoRoot $Root -Owner \"w$Id-i$i\" -FilePath $Exe -ArgumentList @('/c', 'exit 0') -TimeoutSeconds 60 *> $null } catch { }\n"
    "}\n"
)


def _run_writers(tmp_path: Path, script_text: str, args_for) -> None:
    """Start WRITERS pwsh children on one start instant; wait for all; kill every one in finally."""
    script = tmp_path / "writer.ps1"
    script.write_text(script_text, encoding="ascii")
    start_ms = int((time.time() + START_DELAY_SEC) * 1000)
    procs = []
    try:
        for writer_id in range(WRITERS):
            procs.append(
                subprocess.Popen(
                    [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                     "-File", str(script), *args_for(writer_id, start_ms)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
            )
        deadline = time.monotonic() + WRITER_TIMEOUT_SEC
        for proc in procs:
            out, err = proc.communicate(timeout=max(1.0, deadline - time.monotonic()))
            assert proc.returncode == 0, f"writer failed rc={proc.returncode}\n{out}\n{err}"
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()


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
