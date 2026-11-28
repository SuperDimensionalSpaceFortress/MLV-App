"""One shared fixture for every bounded-child site that reports `killConfirmed` (VENUE-LEASE-BATCH-LEGSPEC-COMMITTED-PREFLIGHT-1 r10, Grok G14 = adversary r9 pass 20/21 A1).

The shape a root-only confirmation cannot survive: the ROOT exits, the INTERMEDIATE parent it started has exited too, and the GRANDCHILD that intermediate started is orphaned and still alive.
`Kill($true)` of the exited root walks the chain of live parents, and that chain is gone, so a confirmation read from the root alone (`HasExited`) is true while the grandchild runs on.
Every site that decides `killConfirmed` must report this fixture as NOT confirmed (killScope 'root-only'); the module test and the batch test both build it here, so a site cannot pass with a private, easier fixture.

Both processes are started through ShellExecute (hidden), so neither inherits a pipe handle of the bounded child (a held stdout would keep the drain open and change the case under test).
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _ps_quote(path: Path) -> str:
    return str(path).replace("'", "''")


def write_orphan_fixture(directory: Path, pid_file: Path, answer: str = "") -> Path:
    """Write root.ps1 and mid.ps1 under `directory` and return root.ps1.

    root.ps1 starts mid.ps1 (hidden), waits until the grandchild has written its pid to `pid_file` and until mid.ps1 has exited, prints `answer` (a line the caller's contract wants, may be empty),
    and exits 0. mid.ps1 starts the grandchild (hidden, sleeps 600 s, writes its own pid to `pid_file`) and exits at once. root.ps1 accepts the three parameters the committed-spec preflight passes.
    """
    directory.mkdir(parents=True, exist_ok=True)
    pid_file.unlink(missing_ok=True)
    mid = directory / "mid.ps1"
    root = directory / "root.ps1"
    mid.write_text(
        "$psi = [Diagnostics.ProcessStartInfo]::new('pwsh'); $psi.UseShellExecute = $true; $psi.WindowStyle = 'Hidden'\n"
        "$psi.Arguments = '-NoProfile -Command \"Set-Content -LiteralPath ''" + _ps_quote(pid_file).replace("'", "''") + "'' -Value $PID; Start-Sleep 600\"'\n"
        "[void][Diagnostics.Process]::Start($psi)\n"
        "exit 0\n",
        encoding="utf-8",
    )
    body = (
        "param([string]$LegScript, [string]$RepoRoot, [string]$LegSpecSha256)\n"
        f"$d = '{_ps_quote(pid_file)}'\n"
        "Remove-Item -LiteralPath $d -ErrorAction SilentlyContinue\n"
        "$psi = [Diagnostics.ProcessStartInfo]::new('pwsh'); $psi.UseShellExecute = $true; $psi.WindowStyle = 'Hidden'\n"
        f"$psi.Arguments = '-NoProfile -File \"{_ps_quote(mid)}\"'\n"
        "$m = [Diagnostics.Process]::Start($psi)\n"
        "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
        "while (-not (Test-Path -LiteralPath $d) -and $sw.Elapsed.TotalSeconds -lt 30) { Start-Sleep -Milliseconds 100 }\n"
        "[void]$m.WaitForExit(30000)\n"
    )
    if answer:
        body += f"Write-Output '{answer}'\n"
    body += "exit 0\n"
    root.write_text(body, encoding="utf-8")
    return root


def grandchild_pid(pid_file: Path) -> int:
    return int(pid_file.read_text(encoding="utf-8-sig").strip())


def pid_is_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True, text=True).stdout
    return f'"{pid}"' in out


def kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
