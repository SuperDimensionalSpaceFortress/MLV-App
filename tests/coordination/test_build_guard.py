"""BUILD-GUARD-1: tools/coordination/build-guard.ps1 and the pin that local launchers route through it.

On 2026-10-12 four MinGW builds overlapped on the hub VM and three died with cc1plus out-of-memory;
every serial retry at -j2/-j3 passed. The guard is exercised through its injected probes (a memory
sample file and a process-table file), so no real build, no real memory pressure and no compiler is
needed. The last class is the hygiene pin: CLAUDE.md and the routed launchers must not default to -j8.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD = REPO_ROOT / "tools" / "coordination" / "build-guard.ps1"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
BUILD_RELEASE = REPO_ROOT / "tools" / "build-release.ps1"
STAGE_PROBE = REPO_ROOT / "tools" / "profiling" / "qscreen-probe" / "stage-probe-and-job.ps1"
ATTR3_ASSEMBLE = REPO_ROOT / "tools" / "profiling" / "bachelor" / "playback-attr-3-cuda-assemble.ps1"
ROUTED_LAUNCHERS = (BUILD_RELEASE, STAGE_PROBE, ATTR3_ASSEMBLE)

PWSH = shutil.which("pwsh.exe") or shutil.which("pwsh")

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not PWSH, reason="the build guard is a Windows PowerShell 7 tool"
)

OK_MEMORY = [{"freePhysGB": 10, "freeCommitGB": 20}]
LOW_PHYS = {"freePhysGB": 5.9, "freeCommitGB": 20}
LOW_COMMIT = {"freePhysGB": 10, "freeCommitGB": 7.9}
FAST = ["-PollSeconds", "0.2"]


def _env() -> dict:
    env = dict(os.environ)
    env.pop("MLV_BUILD_GUARD_EXEMPT", None)
    return env


def _ps(args: list, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(),
        check=False,
    )


def _write_json(path: Path, value) -> str:
    path.write_text(json.dumps(value), encoding="ascii")
    return str(path)


@pytest.fixture()
def box(tmp_path: Path) -> dict:
    return {
        "dir": tmp_path,
        "lock": tmp_path / "build-slot.lock",
        "exempt_log": tmp_path / "exempt.log",
        "marker": tmp_path / "ran.txt",
        "argv_echo": _write_argv_echo(tmp_path),
    }


def _write_argv_echo(tmp_path: Path) -> Path:
    script = tmp_path / "argv_echo.py"
    script.write_text("import sys\nprint('ARGV=' + repr(sys.argv[1:]))\n", encoding="ascii")
    return script


def _guard(box: dict, memory, table, extra: list, mode: list) -> subprocess.CompletedProcess:
    memory_file = _write_json(box["dir"] / "memory.json", memory)
    table_file = _write_json(box["dir"] / "table.json", table)
    return _ps(
        [
            "-File", str(GUARD),
            "-MemoryProbeFile", memory_file,
            "-ProcessTableFile", table_file,
            "-LockPath", str(box["lock"]),
            "-ExemptLogPath", str(box["exempt_log"]),
            *extra,
            *mode,
        ]
    )


def _print_jobs(box: dict, memory, table, extra: list = ()) -> subprocess.CompletedProcess:
    return _guard(box, memory, table, list(extra), ["-PrintJobs"])


def _marker_command(box: dict) -> str:
    return f"Set-Content -LiteralPath '{box['marker']}' -Value ran -Encoding ascii; cmd /c exit 0"


# --- memory pre-flight -------------------------------------------------------------------------


def test_memory_pass_prints_the_default_job_count(box) -> None:
    done = _print_jobs(box, OK_MEMORY, [])
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "-j4"


def test_memory_at_exactly_the_floor_passes(box) -> None:
    done = _print_jobs(box, [{"freePhysGB": 6, "freeCommitGB": 8}], [])
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "-j4"


def test_memory_wait_then_pass(box) -> None:
    samples = [LOW_PHYS, LOW_COMMIT, OK_MEMORY[0]]
    started = time.monotonic()
    done = _print_jobs(box, samples, [], [*FAST, "-DeadlineMinutes", "1"])
    elapsed = time.monotonic() - started
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "-j4"
    assert elapsed >= 0.35, "two low samples must each cost one poll before the pass"


@pytest.mark.parametrize("low", [LOW_PHYS, LOW_COMMIT], ids=["physical", "commit"])
def test_memory_timeout_exits_3_with_the_typed_line_and_runs_nothing(box, low) -> None:
    done = _guard(
        box, [low], [], [*FAST, "-DeadlineMinutes", "0.03"], ["-Command", _marker_command(box)]
    )
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_MEM_REFUSED" in done.stdout
    assert "need=6/8" in done.stdout
    assert not box["marker"].exists(), "a refused build must not start"
    assert not box["lock"].exists(), "a memory refusal must not leave a slot behind"


def test_exempt_reason_skips_the_memory_floor_and_is_logged(box) -> None:
    done = _guard(
        box,
        [LOW_PHYS],
        [],
        [*FAST, "-DeadlineMinutes", "0.03", "-ExemptReason", "hub ruling 2026-10-13 test"],
        ["-PrintJobs"],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "-j4"
    assert "BUILD_GUARD_EXEMPT" in done.stderr
    assert "hub ruling 2026-10-13 test" in box["exempt_log"].read_text(encoding="ascii")


# --- job count ---------------------------------------------------------------------------------


def _row(pid: int, name: str, created: str = "2026-10-13T20:00:00Z") -> dict:
    return {"ProcessId": pid, "ParentProcessId": 4, "Name": name, "CreationDate": created}


def test_job_count_is_four_when_no_other_build_runs(box) -> None:
    table = [_row(10, "explorer.exe"), _row(11, "cc1.exe"), _row(12, "notcc1plus.exe"), _row(13, "gcc.exe")]
    done = _print_jobs(box, OK_MEMORY, table)
    assert done.stdout.strip() == "-j4", done.stderr


@pytest.mark.parametrize("name", ["cc1plus.exe", "mingw32-make.exe", "cc1plus", "MinGW32-Make.EXE"])
def test_job_count_drops_to_two_beside_another_build(box, name) -> None:
    table = [_row(10, "explorer.exe"), _row(4242, name, "2026-10-13T20:05:06Z")]
    done = _print_jobs(box, OK_MEMORY, table)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "-j2"
    assert "4242@2026-10-13T20:05:06" in done.stderr, "a peer is identified by pid and start time"


def test_requested_jobs_can_lower_but_never_raise_the_choice(box) -> None:
    assert _print_jobs(box, OK_MEMORY, [], ["-RequestedJobs", "1"]).stdout.strip() == "-j1"
    assert _print_jobs(box, OK_MEMORY, [], ["-RequestedJobs", "8"]).stdout.strip() == "-j4"
    peer = [_row(4242, "cc1plus.exe")]
    assert _print_jobs(box, OK_MEMORY, peer, ["-RequestedJobs", "8"]).stdout.strip() == "-j2"


# --- quoting -----------------------------------------------------------------------------------


def test_command_mode_delivers_the_job_count_as_one_quoted_token(box) -> None:
    fragment = f"& '{sys.executable}' '{box['argv_echo']}' -j{{JOBS}}"
    done = _guard(box, OK_MEMORY, [], [], ["-Command", fragment])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ARGV=['-j4']" in done.stdout


def test_command_mode_returns_the_childs_exit_code_and_releases_the_slot(box) -> None:
    done = _guard(box, OK_MEMORY, [], [], ["-Command", "cmd /c exit 9"])
    assert done.returncode == 9, done.stdout + done.stderr
    assert not box["lock"].exists()


def test_command_mode_without_a_command_is_a_usage_error(box) -> None:
    done = _guard(box, OK_MEMORY, [], [], [])
    assert done.returncode == 2
    assert not box["lock"].exists()


def test_library_mode_keeps_the_quoted_token_and_script_scope_results(box) -> None:
    driver = box["dir"] / "driver.ps1"
    driver.write_text(
        "param([string]$Guard, [string]$Echo, [string]$Py, [string]$Lock, [string]$Mem, [string]$Table)\n"
        ". $Guard -Library\n"
        "$opts = @{ LockPath = $Lock; MemoryProbeFile = $Mem; ProcessTableFile = $Table; PollSeconds = 0.2 }\n"
        "Invoke-BuildGuard -Label 'lib-test' -Options $opts -Action {\n"
        "    param($Jobs)\n"
        "    $script:seen = $Jobs\n"
        "    $script:out = @(& $Py $Echo \"-j$Jobs\" 2>&1 | ForEach-Object { [string]$_ })\n"
        "    $script:rc = $LASTEXITCODE\n"
        "    $script:lockWhileBuilding = Test-Path -LiteralPath $Lock\n"
        "}\n"
        "Write-Output \"seen=$seen rc=$rc held=$lockWhileBuilding out=$($out -join '|')\"\n"
        "Write-Output \"lockAfter=$(Test-Path -LiteralPath $Lock)\"\n",
        encoding="ascii",
    )
    done = _ps(
        [
            "-File", str(driver),
            "-Guard", str(GUARD),
            "-Echo", str(box["argv_echo"]),
            "-Py", sys.executable,
            "-Lock", str(box["lock"]),
            "-Mem", _write_json(box["dir"] / "memory.json", OK_MEMORY),
            "-Table", _write_json(box["dir"] / "table.json", []),
        ]
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "seen=4 rc=0 held=True out=ARGV=['-j4']" in done.stdout
    assert "lockAfter=False" in done.stdout


# --- the host-global slot ----------------------------------------------------------------------


def _write_lock(box: dict, owner_pid: int, start_ticks: int, expires_in_minutes: float) -> None:
    # .NET ticks of 'now' without a clock dependency on the guard: 0001-01-01 to 1970-01-01 plus epoch.
    now_ticks = 621355968000000000 + int(time.time() * 10_000_000)
    record = {
        "ownerPid": owner_pid,
        "startTicks": start_ticks,
        "startUtc": "test",
        "expiresTicks": now_ticks + int(expires_in_minutes * 60 * 10_000_000),
        "label": "seeded-test-lock",
        "host": "test",
    }
    box["lock"].write_text(json.dumps(record), encoding="ascii")


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def _run_with_slot(box: dict, extra: list = ()) -> subprocess.CompletedProcess:
    return _guard(box, OK_MEMORY, [], list(extra), ["-Command", _marker_command(box)])


def test_slot_is_held_while_building_and_gone_afterwards(box) -> None:
    fragment = f"Write-Output \"held=$(Test-Path -LiteralPath '{box['lock']}')\""
    done = _guard(box, OK_MEMORY, [], [], ["-Command", fragment])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "held=True" in done.stdout
    assert not box["lock"].exists()


def test_stale_lock_with_a_dead_owner_is_reclaimed(box) -> None:
    _write_lock(box, _dead_pid(), 1, expires_in_minutes=60)
    done = _run_with_slot(box)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "reclaiming stale build slot (owner-dead)" in done.stderr
    assert box["marker"].exists()
    assert not box["lock"].exists()


def test_expired_lock_is_reclaimed_even_when_its_owner_pid_is_alive(box) -> None:
    _write_lock(box, os.getpid(), 1, expires_in_minutes=-1)
    done = _run_with_slot(box)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "reclaiming stale build slot (expired)" in done.stderr
    assert box["marker"].exists()


def test_reused_pid_with_a_different_start_time_does_not_hold_the_slot(box) -> None:
    # os.getpid() is alive, but the recorded start time is from another process that once had this pid.
    _write_lock(box, os.getpid(), 1, expires_in_minutes=60)
    done = _run_with_slot(box)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "reclaiming stale build slot (owner-dead)" in done.stderr


def test_unreadable_old_lock_is_reclaimed_but_a_fresh_one_is_not(box) -> None:
    box["lock"].write_text("", encoding="ascii")
    fresh = _run_with_slot(box, [*FAST, "-DeadlineMinutes", "0.03"])
    assert fresh.returncode == 3, fresh.stdout + fresh.stderr
    assert "BUILD_SLOT_BUSY" in fresh.stdout
    old = time.time() - 600
    os.utime(box["lock"], (old, old))
    stale = _run_with_slot(box)
    assert stale.returncode == 0, stale.stdout + stale.stderr
    assert "reclaiming stale build slot (unreadable)" in stale.stderr


def test_live_unexpired_lock_blocks_the_build_with_slot_busy(box) -> None:
    holder = subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-Command", "Start-Sleep -Seconds 120"])
    try:
        probe = _ps(
            ["-Command", f"(Get-Process -Id {holder.pid}).StartTime.ToUniversalTime().Ticks"]
        )
        assert probe.returncode == 0, probe.stderr
        _write_lock(box, holder.pid, int(probe.stdout.strip()), expires_in_minutes=60)
        done = _run_with_slot(box, [*FAST, "-DeadlineMinutes", "0.03"])
        assert done.returncode == 3, done.stdout + done.stderr
        assert "BUILD_SLOT_BUSY" in done.stdout
        assert f"pid={holder.pid}" in done.stdout
        assert not box["marker"].exists(), "a build must not start while another holds the slot"
        assert box["lock"].exists(), "a live owner's lock must survive a refused contender"
    finally:
        holder.kill()
        holder.wait()


# --- hygiene pin: no local launcher defaults to -j8 --------------------------------------------


class TestLocalLaunchersRouteThroughTheGuard:
    def test_the_guard_is_tracked_ascii_and_names_its_typed_lines(self) -> None:
        text = GUARD.read_bytes()
        text.decode("ascii")
        for token in (b"BUILD_MEM_REFUSED", b"BUILD_SLOT_BUSY", b"C:\\mlvtmp\\locks\\build-slot.lock"):
            assert token in text

    def test_claude_md_no_longer_instructs_a_literal_j8(self) -> None:
        text = CLAUDE_MD.read_text(encoding="utf-8")
        assert "-j8" not in text
        assert "tools/coordination/build-guard.ps1" in text
        assert "{JOBS}" in text

    @pytest.mark.parametrize("path", ROUTED_LAUNCHERS, ids=lambda p: p.name)
    def test_launcher_has_no_literal_j8_and_calls_the_guard(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        assert "-j8" not in text
        assert "build-guard.ps1" in text
        assert "Invoke-BuildGuard" in text

    def test_attr3_assembler_defaults_to_the_guards_choice_and_keeps_the_quoting_fix(self) -> None:
        text = ATTR3_ASSEMBLE.read_text(encoding="utf-8")
        assert "[int]$MakeJobs = 0," in text
        assert "[int]$MakeJobs = 4" not in text
        assert '"-j$MakeJobs"' in text

    def test_build_release_passes_the_guards_job_count_not_a_constant(self) -> None:
        text = BUILD_RELEASE.read_text(encoding="utf-8")
        assert "\"-j$Jobs\"" in text

    @pytest.mark.parametrize("path", [GUARD, *ROUTED_LAUNCHERS], ids=lambda p: p.name)
    def test_edited_script_still_parses(self, path: Path) -> None:
        literal = str(path).replace("'", "''")
        done = _ps(
            [
                "-Command",
                "$e = $null; $t = $null; "
                f"[void][System.Management.Automation.Language.Parser]::ParseFile('{literal}', [ref]$t, [ref]$e); "
                "if ($e.Count -gt 0) { $e | ForEach-Object { $_.Message }; exit 1 }",
            ]
        )
        assert done.returncode == 0, done.stdout + done.stderr
