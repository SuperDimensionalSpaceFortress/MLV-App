"""LEDGER-APPEND-ATOMIC-WORKSTREAM-LOG-1: Invoke-Workstream's dispatch log must not lose lines under concurrent writers.

Invoke-Workstream.ps1 appends one row to ``workstream-dispatch-log.jsonl`` at each of its two dispatch
exits (read-only and editing). pwsh 7 ``Add-Content`` is not atomic across processes (survey
LEDGER-APPEND-ATOMIC-SURVEY-1: ~14 % of appends lost across 6 writers), so two overlapping dispatches could
drop a row.

The script is a large dispatcher with no append-only mode, so this test runs the REAL append statements of
the script: it parses Invoke-Workstream.ps1, takes every command that writes ``$LogPath`` (an
``Add-Content`` today, a call to the serializing helper after the fix) together with the helper function
when the script defines one, and executes exactly that text in N concurrent pwsh processes. The test does
not care which remedy the script uses, only that no row is lost. Every child is bounded by a timeout and
killed in ``finally``.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKSTREAM = REPO_ROOT / "tools" / "coordination" / "Invoke-Workstream.ps1"

PWSH = shutil.which("pwsh.exe") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not PWSH, reason="the dispatch log is written by a Windows PowerShell 7 tool"
)

WRITERS = 6
APPENDS_EACH = 40
START_DELAY_SEC = 6
WRITER_TIMEOUT_SEC = 150

# Extracts, from the dispatcher's own source, the helper (if any) and every command appending to $LogPath.
EXTRACT = r"""
param([string]$Source)
$tokens = $null; $errs = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$errs)
if (@($errs).Count -gt 0) { throw "parse errors in $Source" }
$helpers = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Add-DispatchLogRow' }, $true) | ForEach-Object { $_.Extent.Text })
$sites = @($ast.FindAll({ param($n)
    $n -is [System.Management.Automation.Language.CommandAst] -and
    @('Add-Content', 'Add-DispatchLogRow') -contains $n.GetCommandName() -and
    $n.Extent.Text.Contains('$LogPath')
}, $true) | ForEach-Object { $_.Extent.Text })
@{ helpers = $helpers; sites = $sites } | ConvertTo-Json -Compress -Depth 4
"""

WRITER = (
    "param([string]$HelpersFile, [string]$SitesFile, [string]$LogPath, [int]$Id, [int]$Count, [long]$StartMs)\n"
    "$ErrorActionPreference = 'Stop'\n"
    ". ([scriptblock]::Create([IO.File]::ReadAllText($HelpersFile)))\n"
    "$sites = @([IO.File]::ReadAllText($SitesFile) | ConvertFrom-Json)\n"
    "while ([DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() -lt $StartMs) { }\n"
    "for ($i = 0; $i -lt $Count; $i++) {\n"
    "    $record = [ordered]@{ schema = 'mlv-app/workstream-dispatch/v1'; cardId = \"w$Id-i$i\"; laneExitCode = 0; pid = $PID }\n"
    "    . ([scriptblock]::Create($sites[$i % $sites.Count]))\n"
    "}\n"
)


def _pwsh(args, timeout):
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True, text=True, timeout=timeout, check=False,
    )


def test_dispatch_log_keeps_every_concurrent_append(tmp_path: Path) -> None:
    extract = tmp_path / "extract.ps1"
    extract.write_text(EXTRACT, encoding="ascii")
    done = _pwsh(["-File", str(extract), str(WORKSTREAM)], timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    found = json.loads(done.stdout)
    sites = found["sites"] if isinstance(found["sites"], list) else [found["sites"]]
    helpers = found["helpers"] if isinstance(found["helpers"], list) else [found["helpers"]]
    assert len(sites) == 2, f"expected the read-only and the editing dispatch exit to append to $LogPath, found {sites}"

    writer = tmp_path / "writer.ps1"
    writer.write_text(WRITER, encoding="ascii")
    helpers_file = tmp_path / "helpers.ps1"
    helpers_file.write_text("\n".join(helpers) + "\n", encoding="utf-8")
    sites_file = tmp_path / "sites.json"
    sites_file.write_text(json.dumps(sites), encoding="utf-8")
    log = tmp_path / "workstream-dispatch-log.jsonl"
    start_ms = int((time.time() + START_DELAY_SEC) * 1000)
    procs = []
    try:
        for writer_id in range(WRITERS):
            procs.append(
                subprocess.Popen(
                    [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(writer),
                     str(helpers_file), str(sites_file), str(log), str(writer_id), str(APPENDS_EACH), str(start_ms)],
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

    raw = log.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "pwsh 7 Add-Content -Encoding UTF8 writes no BOM; keep that"
    all_lines = raw.decode("utf-8").splitlines()
    lines = [line for line in all_lines if line]
    expected = sorted(f"w{w}-i{i}" for w in range(WRITERS) for i in range(APPENDS_EACH))
    assert len(lines) == WRITERS * APPENDS_EACH, (
        f"lost {WRITERS * APPENDS_EACH - len(lines)} of {WRITERS * APPENDS_EACH} appends ({len(all_lines) - len(lines)} blank line(s))"
    )
    ids = []
    for line in all_lines:
        try:
            ids.append(json.loads(line)["cardId"])
        except (ValueError, KeyError):
            pytest.fail(f"malformed dispatch-log line: {line[:120]!r}")
    assert sorted(ids) == expected
    assert raw.endswith(b"\r\n"), "Add-Content ended each row with the platform newline; keep it"
