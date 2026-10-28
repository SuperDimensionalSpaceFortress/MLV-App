"""BUILD-GUARD-1: tools/coordination/build-guard.ps1 and the pin that local launchers route through it.

On 2026-10-12 four MinGW builds overlapped on the hub VM and three died with cc1plus out-of-memory;
every serial retry at -j2/-j3 passed. The guard is exercised through its injected probes (a memory
sample file and a process-table file), so no real build, no real memory pressure and no compiler is
needed. The last class is the hygiene pin: CLAUDE.md and the routed launchers must not default to -j8.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
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

# MLV-VM-HEAVY-SLOT-ADOPT-1: no test reaches the live machine heavy slot. By default the shim is a path that does
# not exist, so the guard fails open with one WARN; the heavy-slot tests point it at the stub beside this file.
HEAVY_STUB_DIR = Path(__file__).resolve().parent / "heavy_slot_stub"
HEAVY_STUB = HEAVY_STUB_DIR / "heavy-slot.cmd"
NO_HEAVY_SLOT = HEAVY_STUB_DIR / "absent" / "heavy-slot.cmd"
PINNED_HEAVY_SLOT_SHA = "691efb4c2cc1b98cd5a7379e03a2bb5b9eebaa1174a03b11ee7a48741fc06534"

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not PWSH, reason="the build guard is a Windows PowerShell 7 tool"
)

OK_MEMORY = [{"freePhysGB": 10, "freeCommitGB": 20}]
LOW_PHYS = {"freePhysGB": 5.9, "freeCommitGB": 20}
LOW_COMMIT = {"freePhysGB": 10, "freeCommitGB": 7.9}
FAST = ["-PollSeconds", "0.2"]


def _env(exempt=None, heavy_slot: Path = NO_HEAVY_SLOT, extra: dict = None) -> dict:
    env = dict(os.environ)
    env.pop("MLV_BUILD_GUARD_EXEMPT", None)
    for name in ("HEAVY_SLOT_HELD", "HEAVY_SLOT_HELD_START", "HEAVY_SLOT_HELD_NAME", "MLV_LANE_RUN_DIR"):
        env.pop(name, None)
    env["MLV_HEAVY_SLOT_CMD"] = str(heavy_slot)
    if exempt is not None:
        env["MLV_BUILD_GUARD_EXEMPT"] = exempt
    env.update(extra or {})
    return env


def _ps(args: list, timeout: int = 90, exempt_env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_env(exempt_env),
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


def _guard(box: dict, memory, table, extra: list, mode: list, exempt_env=None) -> subprocess.CompletedProcess:
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
        ],
        exempt_env=exempt_env,
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


def test_real_probes_are_bounded_and_still_answer(box) -> None:
    # BUILD-GUARD-CIM-TIMEOUT-1: a wedged WMI provider must not hold the guard past its deadline.
    text = GUARD.read_text(encoding="ascii")
    assert text.count("Get-CimInstance") == text.count("Get-CimInstance Win32_") == 2
    assert text.count("-OperationTimeoutSec $script:BuildGuardCimTimeoutSec") == 2
    # The real probes (no injected files); exempt so the host's live memory cannot refuse the run.
    done = _ps(
        [
            "-File", str(GUARD),
            "-LockPath", str(box["lock"]),
            "-ExemptLogPath", str(box["exempt_log"]),
            "-ExemptReason", "test of the real probes",
            "-PrintJobs",
        ]
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() in ("-j2", "-j4"), done.stdout + done.stderr


@pytest.mark.parametrize("value", ["0", "false", "OFF", "no", " ", ""], ids=repr)
def test_false_y_exempt_env_does_not_skip_the_memory_floor(box, value) -> None:
    done = _guard(
        box, [LOW_PHYS], [], [*FAST, "-DeadlineMinutes", "0.03"], ["-PrintJobs"], exempt_env=value
    )
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_MEM_REFUSED" in done.stdout
    assert "BUILD_GUARD_EXEMPT" not in done.stderr
    assert not box["exempt_log"].exists()


def test_exempt_env_with_a_real_reason_skips_the_memory_floor(box) -> None:
    done = _guard(
        box, [LOW_PHYS], [], [*FAST, "-DeadlineMinutes", "0.03"], ["-PrintJobs"], exempt_env="hub ruling env"
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "reason=hub ruling env" in done.stderr


def test_exempt_build_still_takes_the_slot(box) -> None:
    fragment = f"Write-Output \"held=$(Test-Path -LiteralPath '{box['lock']}')\""
    done = _guard(
        box,
        [LOW_PHYS],
        [],
        [*FAST, "-DeadlineMinutes", "0.03", "-ExemptReason", "hub ruling slot test"],
        ["-Command", fragment],
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "held=True" in done.stdout, "an exemption skips only the memory floor, never the slot"
    assert not box["lock"].exists()


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


@pytest.mark.parametrize(
    "make_parent, expected", [(True, "jobs=4"), (False, "jobs=2")], ids=["ancestor", "unrelated"]
)
def test_a_make_that_launched_the_guard_is_not_a_peer(box, make_parent, expected) -> None:
    # The table is written from inside the guard's own process, so its real $PID is in it.
    driver = box["dir"] / "ancestor.ps1"
    driver.write_text(
        "param([string]$Guard, [string]$Lock, [string]$Mem, [string]$Table, [string]$Parent)\n"
        "$makeParent = if ($Parent -eq 'yes') { 777 } else { 4 }\n"
        "$rows = @(\n"
        "    @{ ProcessId = $PID; ParentProcessId = $makeParent; Name = 'pwsh.exe'; CreationDate = '2026-10-13T20:00:00Z' },\n"
        "    @{ ProcessId = 777; ParentProcessId = 4; Name = 'mingw32-make.exe'; CreationDate = '2026-10-13T20:00:00Z' }\n"
        ")\n"
        "ConvertTo-Json -InputObject $rows | Set-Content -LiteralPath $Table -Encoding ascii\n"
        ". $Guard -Library\n"
        "$opts = @{ LockPath = $Lock; MemoryProbeFile = $Mem; ProcessTableFile = $Table; PollSeconds = 0.2 }\n"
        "Invoke-BuildGuard -Label 'ancestor-test' -NoSlot -Options $opts -Action { param($Jobs) Write-Output \"jobs=$Jobs\" }\n",
        encoding="ascii",
    )
    done = _ps(
        [
            "-File", str(driver),
            "-Guard", str(GUARD),
            "-Lock", str(box["lock"]),
            "-Mem", _write_json(box["dir"] / "memory.json", OK_MEMORY),
            "-Table", str(box["dir"] / "table.json"),
            "-Parent", "yes" if make_parent else "no",
        ]
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert expected in done.stdout, done.stdout + done.stderr


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


@pytest.mark.parametrize(
    "fragment, reason",
    [
        ("mingw32-make-BUILD_GUARD_MISSING_TEST -j{JOBS}", "CommandNotFoundException"),
        ("Write-Error 'BUILD_FAILED'", "BUILD_FAILED"),
        ("throw 'BUILD_THROWN'", "BUILD_THROWN"),
    ],
    ids=["missing-executable", "write-error", "throw"],
)
def test_command_mode_fails_when_the_fragment_fails_without_a_native_exit_code(box, fragment, reason) -> None:
    # BG-01: none of these touch LASTEXITCODE, so the guard used to report a failed build as exit 0.
    after = f"; Set-Content -LiteralPath '{box['marker']}' -Value ran -Encoding ascii"
    probe = f"Set-Content -LiteralPath '{box['dir'] / 'held.txt'}' -Value $(Test-Path -LiteralPath '{box['lock']}'); "
    done = _guard(box, OK_MEMORY, [], [], ["-Command", probe + fragment + after])
    assert done.returncode == 1, done.stdout + done.stderr
    assert "BUILD_GUARD_COMMAND_FAILED label=build" in done.stderr
    assert reason in done.stderr
    assert (box["dir"] / "held.txt").read_text(encoding="ascii").strip() == "True"
    assert not box["marker"].exists(), "the fragment must stop at its first failure"
    assert not box["lock"].exists(), "the slot must be released after a failed fragment"


@pytest.mark.parametrize(
    "fragment, code",
    [
        ("cmd /c exit 0", 0),
        ("cmd /c exit 9", 9),
        ("cmd /c \"echo warn 1>&2 & exit 0\" 2>&1", 0),
        ("cmd /c \"echo warn 1>&2 & exit 9\" 2>&1", 9),
    ],
    ids=["native-0", "native-9", "native-0-stderr-merged", "native-9-stderr-merged"],
)
def test_command_mode_returns_native_exit_codes_unchanged(box, fragment, code) -> None:
    done = _guard(box, OK_MEMORY, [], [], ["-Command", fragment])
    assert done.returncode == code, done.stdout + done.stderr
    assert "BUILD_GUARD_COMMAND_FAILED" not in done.stderr
    assert not box["lock"].exists()


@pytest.mark.parametrize(
    "first, code",
    [
        ("cmd /c exit 9", 9),
        ("cmd /c \"echo warn 1>&2 & exit 7\" 2>&1 | ForEach-Object { $_ }", 7),
        ("cmd /c exit 0", 0),
    ],
    ids=["native-9-then-native-0", "piped-native-7-then-native-0", "native-0-then-native-0"],
)
def test_command_mode_stops_at_the_first_failing_native_command(box, first, code) -> None:
    # BG-02: a failed qmake followed by a make that exits 0 used to read as success. The second command
    # is native and writes the marker, so the exit-0 row is the control that the marker writer works.
    assert " " not in str(box["marker"])
    second = f"cmd /c \"echo ran> {box['marker']} & exit 0\""
    done = _guard(box, OK_MEMORY, [], [], ["-Command", f"{first}; {second}"])
    assert done.returncode == code, done.stdout + done.stderr
    assert "BUILD_GUARD_COMMAND_FAILED" not in done.stderr
    assert not box["lock"].exists()
    if code == 0:
        assert box["marker"].exists(), "a fragment with no failure must run to the end"
        return
    assert not box["marker"].exists(), "the command after the failing one must never run"
    assert f"first failing native command: exit={code}" in done.stderr


@pytest.mark.parametrize(
    "first",
    [
        "Write-Error 'BUILD_FAILED_CONTINUE' -ErrorAction Continue",
        "Get-Item -LiteralPath 'C:\\BUILD_FAILED_CONTINUE-missing' -ErrorAction SilentlyContinue",
    ],
    ids=["write-error-continue", "silently-continue"],
)
def test_command_mode_fails_on_an_error_record_that_did_not_stop_the_fragment(box, first) -> None:
    # BG-02: the fragment goes on (the caller asked for that), but the verdict is still a failure.
    done = _guard(box, OK_MEMORY, [], [], ["-Command", f"{first}; cmd /c exit 0; Write-Output AFTER"])
    assert done.returncode == 1, done.stdout + done.stderr
    assert "AFTER" in done.stdout
    assert "BUILD_GUARD_COMMAND_FAILED label=build lastNativeExit=0 errors=1" in done.stderr
    assert "BUILD_FAILED_CONTINUE" in done.stderr
    assert not box["lock"].exists()


@pytest.mark.parametrize(
    "fragment",
    [
        "Get-Item -LiteralPath 'C:\\BUILD_GUARD_IGNORED-missing' -ErrorAction Ignore; cmd /c exit 0; Write-Output AFTER",
        "cmd /c \"echo warn 1>&2 & exit 0\"; cmd /c exit 0; Write-Output AFTER",
    ],
    ids=["error-action-ignore", "native-stderr-unmerged"],
)
def test_command_mode_does_not_invent_a_failure(box, fragment) -> None:
    done = _guard(box, OK_MEMORY, [], [], ["-Command", fragment])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "AFTER" in done.stdout
    assert "BUILD_GUARD_COMMAND_FAILED" not in done.stderr
    assert not box["lock"].exists()


@pytest.mark.parametrize(
    "fragment, code, typed",
    [
        ("Write-Error 'BUILD_FAILED_EXIT' -ErrorAction Continue; cmd /c exit 0; exit $LASTEXITCODE", 1, True),
        ("Write-Error 'BUILD_FAILED_EXIT' -ErrorAction Continue; cmd /c exit 9", 9, True),
        ("cmd /c exit 0; exit 0", 0, False),
        ("cmd /c exit 7; exit $LASTEXITCODE", 7, False),
        ("cmd /c exit 0; exit 5", 5, False),
        ("& { Write-Error 'BUILD_FAILED_EXIT' -ErrorAction Continue; exit 0 }", 1, True),
        ("cmd /c exit 0; exit", 0, False),
        ("cmd /c exit 0; break", 1, True),
    ],
    ids=[
        "error-then-exit-lastexitcode",
        "error-then-native-9",
        "clean-explicit-exit-0",
        "native-7-then-exit-lastexitcode",
        "clean-explicit-exit-5",
        "error-then-exit-0-in-nested-block",
        "clean-bare-exit",
        "break-out-of-the-fragment",
    ],
)
def test_command_mode_adjudicates_every_way_out_of_the_fragment(box, fragment, code, typed) -> None:
    # BG-03 (sol r4): an 'exit' in the fragment used to end the guard before the collected error records
    # were judged, so a failed build read as 0. fable r4: an error record before a failing native command
    # was dropped from the verdict. Every way out now reaches one verdict: a non-zero code is kept, error
    # records add the typed line (and turn a zero code into 1), and the slot is released on every path.
    probe = f"Set-Content -LiteralPath '{box['dir'] / 'held.txt'}' -Value $(Test-Path -LiteralPath '{box['lock']}'); "
    done = _guard(box, OK_MEMORY, [], [], ["-Command", probe + fragment])
    assert done.returncode == code, done.stdout + done.stderr
    typed_lines = [line for line in done.stderr.splitlines() if line.startswith("BUILD_GUARD_COMMAND_FAILED ")]
    assert bool(typed_lines) == typed, done.stderr
    if typed:
        assert f"exit={code} " in typed_lines[0]
        if "BUILD_FAILED_EXIT" in fragment:
            assert "errors=1 " in typed_lines[0] and "BUILD_FAILED_EXIT" in typed_lines[0]
    assert (box["dir"] / "held.txt").read_text(encoding="ascii").strip() == "True"
    assert not box["lock"].exists(), "the slot must be released on every way out of the fragment"


# BG-04 (sol r5): every way a native failure or error record can be observed and then masked, and where it
# is pinned. A native failure is observed from its NativeCommandExitException record (-ErrorVariable on the
# fragment collects it from any scope, caught or not); the verdict keeps a non-zero final code and turns a
# zero final code into the FIRST failing native code (reason=native-failure-overwritten).
#
#   masking shape                                  | pinned by
#   -----------------------------------------------+---------------------------------------------------------
#   later success overwrites LASTEXITCODE          | (l) caught-native-9-then-native-0
#   try/catch around the native command            | (l), (p) caught-native-9-alone (code kept, no typed line)
#   'exit $LASTEXITCODE' after the overwrite       | (m) caught-native-9-then-native-0-then-exit-lastexitcode
#   'return' out of the fragment                   | caught-native-9-then-native-0-then-return
#   pipeline to Out-Null                           | native-9-piped-to-out-null-then-native-0 (stops at 9),
#                                                  | caught-native-9-then-native-0-piped-to-out-null
#   '2>$null' on the native command                | (n) native-9-stderr-to-null-then-native-0 (stops at 9)
#   '& { }' child scope (also '. { }', a function) | caught-native-9-in-child-scope-then-native-0
#   $ErrorActionPreference = 'SilentlyContinue'    | silently-continue-native-9-then-native-0
#   $global:LASTEXITCODE reset / $Error.Clear()    | caught-native-9-then-lastexitcode-reset,
#                                                  | caught-native-9-then-error-clear-then-native-0
#   trap { continue }                              | trap-continue-native-9-then-native-0
#   a later failure or 'exit N' with its own code  | caught-native-9-then-native-7, caught-native-9-then-exit-5
#   no failure at all (no false failure)           | (o) native-0-then-native-0
#
# Non-promises (out of scope, one line each):
#   - '$?' reset: the guard never reads $?; it judges records and codes, which $? cannot change.
#   - $PSNativeCommandUseErrorActionPreference = $false, $ErrorActionPreference = 'Ignore', -ErrorAction
#     Ignore: the fragment tells PowerShell to keep no record, so only its final code is judged (the same
#     documented exemption as -ErrorAction Ignore since r3).
#   - Start-Job / Start-Process / another runspace: a failure in another process or runspace is not the
#     fragment's own; the fragment must check it (Receive-Job, -Wait -PassThru) and fail itself.
#   - a native failure inside dynamically built code that also exits ('[scriptblock]::Create("exit 4")'):
#     fails closed via=left with exit 1 (pinned by break-out-of-the-fragment in the test above).
@pytest.mark.parametrize(
    "fragment, code, reason",
    [
        ("try { cmd /c exit 9 } catch { Write-Output CAUGHT }; cmd /c exit 0", 9, "native-failure-overwritten"),
        ("try { cmd /c exit 9 } catch { Write-Output CAUGHT }; cmd /c exit 0; exit $LASTEXITCODE", 9,
         "native-failure-overwritten"),
        ("cmd /c exit 9 2>$null; cmd /c exit 0", 9, None),
        ("cmd /c exit 0; cmd /c exit 0", 0, None),
        ("try { cmd /c exit 9 } catch { }", 9, None),
        ("try { cmd /c exit 9 } catch { }; cmd /c exit 0; return", 9, "native-failure-overwritten"),
        ("cmd /c exit 9 | Out-Null; cmd /c exit 0", 9, None),
        ("try { cmd /c exit 9 } catch { }; cmd /c exit 0 | Out-Null", 9, "native-failure-overwritten"),
        ("& { try { cmd /c exit 9 } catch { } }; cmd /c exit 0", 9, "native-failure-overwritten"),
        ("$ErrorActionPreference = 'SilentlyContinue'; cmd /c exit 9; cmd /c exit 0", 9, "native-failure-overwritten"),
        ("try { cmd /c exit 9 } catch { }; $global:LASTEXITCODE = 0", 9, "native-failure-overwritten"),
        ("try { cmd /c exit 9 } catch { }; $Error.Clear(); cmd /c exit 0", 9, "native-failure-overwritten"),
        ("trap { continue }; cmd /c exit 9; cmd /c exit 0", 9, "native-failure-overwritten"),
        ("try { cmd /c exit 9 } catch { }; cmd /c exit 7", 7, "earlier-native-failure"),
        ("try { cmd /c exit 9 } catch { }; exit 5", 5, "earlier-native-failure"),
    ],
    ids=[
        "l-caught-native-9-then-native-0",
        "m-caught-native-9-then-native-0-then-exit-lastexitcode",
        "n-native-9-stderr-to-null-then-native-0",
        "o-native-0-then-native-0",
        "p-caught-native-9-alone",
        "caught-native-9-then-native-0-then-return",
        "native-9-piped-to-out-null-then-native-0",
        "caught-native-9-then-native-0-piped-to-out-null",
        "caught-native-9-in-child-scope-then-native-0",
        "silently-continue-native-9-then-native-0",
        "caught-native-9-then-lastexitcode-reset",
        "caught-native-9-then-error-clear-then-native-0",
        "trap-continue-native-9-then-native-0",
        "caught-native-9-then-native-7",
        "caught-native-9-then-exit-5",
    ],
)
def test_command_mode_never_lets_a_native_failure_be_masked(box, fragment, code, reason) -> None:
    # BG-04 (sol r5): a caught native failure followed by a native success read as 0. The first failing
    # native code now survives every masking shape in the table above, and the slot is released on each.
    probe = f"Set-Content -LiteralPath '{box['dir'] / 'held.txt'}' -Value $(Test-Path -LiteralPath '{box['lock']}'); "
    done = _guard(box, OK_MEMORY, [], [], ["-Command", probe + fragment])
    assert done.returncode == code, done.stdout + done.stderr
    typed_lines = [line for line in done.stderr.splitlines() if line.startswith("BUILD_GUARD_COMMAND_FAILED ")]
    assert bool(typed_lines) == (reason is not None), done.stderr
    if reason is not None:
        assert f"exit={code} " in typed_lines[0] and f"reason={reason}: " in typed_lines[0], typed_lines[0]
        assert "exit code: 9" in typed_lines[0], "the typed line must name the masked native failure"
    if "CAUGHT" in fragment:
        assert "CAUGHT" in done.stdout, "the fragment's own catch still runs"
    assert (box["dir"] / "held.txt").read_text(encoding="ascii").strip() == "True"
    assert not box["lock"].exists(), "the slot must be released on every way out of the fragment"


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


def test_release_leaves_a_successors_lock_alone(box) -> None:
    # A successor reclaimed the slot while this build ran (for example after expiry): its lock must survive.
    successor = json.dumps({"ownerPid": 1, "startTicks": 1, "expiresTicks": 1, "label": "successor"})
    fragment = f"Set-Content -LiteralPath '{box['lock']}' -Value '{successor}' -Encoding ascii -NoNewline"
    done = _guard(box, OK_MEMORY, [], [], ["-Command", fragment])
    assert done.returncode == 0, done.stdout + done.stderr
    assert box["lock"].exists(), "release must never delete another owner's lock"
    assert json.loads(box["lock"].read_text(encoding="ascii"))["label"] == "successor"


def test_release_reads_and_deletes_the_lock_under_the_slot_mutex(box) -> None:
    # BUILD-GUARD-RELEASE-RACE-1: while another process holds the slot mutex, release must wait, so a
    # contender reclaiming under the mutex can never have its fresh lock deleted by our late release.
    mutex = f"Local\\MLV-BuildGuardTest-{os.getpid()}-{time.time_ns()}"
    go, ready, result = (box["dir"] / n for n in ("go.txt", "ready.txt", "result.txt"))
    holder_script = box["dir"] / "holder.ps1"
    holder_script.write_text(
        "param([string]$Name, [string]$Lock, [string]$Go, [string]$Ready, [string]$Result)\n"
        "$deadline = (Get-Date).AddSeconds(60)\n"
        "while (-not (Test-Path -LiteralPath $Go)) { if ((Get-Date) -gt $deadline) { exit 5 }; Start-Sleep -Milliseconds 50 }\n"
        "$m = [System.Threading.Mutex]::new($false, $Name)\n"
        "if (-not $m.WaitOne(30000)) { exit 6 }\n"
        "Set-Content -LiteralPath $Ready -Value ok -Encoding ascii\n"
        "Start-Sleep -Milliseconds 1500\n"
        "Set-Content -LiteralPath $Result -Value \"lockWhileHeld=$(Test-Path -LiteralPath $Lock)\" -Encoding ascii\n"
        "$m.ReleaseMutex(); $m.Dispose()\n",
        encoding="ascii",
    )
    holder = subprocess.Popen(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(holder_script),
         "-Name", mutex, "-Lock", str(box["lock"]), "-Go", str(go), "-Ready", str(ready), "-Result", str(result)]
    )
    try:
        fragment = (
            f"Set-Content -LiteralPath '{go}' -Value go -Encoding ascii; $d = (Get-Date).AddSeconds(30); "
            f"while (-not (Test-Path -LiteralPath '{ready}') -and (Get-Date) -lt $d) {{ Start-Sleep -Milliseconds 50 }}"
        )
        done = _guard(box, OK_MEMORY, [], ["-MutexName", mutex], ["-Command", fragment])
        assert holder.wait(timeout=60) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
    assert done.returncode == 0, done.stdout + done.stderr
    assert result.read_text(encoding="ascii").strip() == "lockWhileHeld=True", "release ran outside the mutex"
    assert not box["lock"].exists(), "release must still delete our own lock once the mutex is free"


def test_reclaim_waits_for_the_slot_mutex(box) -> None:
    # BUILD-GUARD-TESTS-2-MUTEX-SERIALISATION: the read-check-reclaim-create runs under the slot mutex, so
    # while another process holds it, a stale lock must stay untouched and no build may start.
    mutex = f"Local\\MLV-BuildGuardTest-{os.getpid()}-{time.time_ns()}"
    ready, release, result = (box["dir"] / n for n in ("ready.txt", "release.txt", "result.txt"))
    _write_lock(box, _dead_pid(), 1, expires_in_minutes=60)
    holder_script = box["dir"] / "holder.ps1"
    holder_script.write_text(
        "param([string]$Name, [string]$Lock, [string]$Marker, [string]$Ready, [string]$Result)\n"
        "$m = [System.Threading.Mutex]::new($false, $Name)\n"
        "if (-not $m.WaitOne(30000)) { exit 6 }\n"
        "Set-Content -LiteralPath $Ready -Value ok -Encoding ascii\n"
        "Start-Sleep -Milliseconds 2500\n"
        "$label = (Get-Content -LiteralPath $Lock -Raw | ConvertFrom-Json).label\n"
        "Set-Content -LiteralPath $Result -Value \"label=$label ran=$(Test-Path -LiteralPath $Marker)\" -Encoding ascii\n"
        "$m.ReleaseMutex(); $m.Dispose()\n",
        encoding="ascii",
    )
    holder = subprocess.Popen(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(holder_script), "-Name", mutex,
         "-Lock", str(box["lock"]), "-Marker", str(box["marker"]), "-Ready", str(ready), "-Result", str(result)]
    )
    try:
        deadline = time.monotonic() + 60
        while not ready.exists():
            assert holder.poll() is None and time.monotonic() < deadline, "holder never took the mutex"
            time.sleep(0.05)
        done = _run_with_slot(box, ["-MutexName", mutex])
        assert holder.wait(timeout=60) == 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
    assert result.read_text(encoding="ascii").strip() == "label=seeded-test-lock ran=False", "reclaim ran outside the mutex"
    assert done.returncode == 0, done.stdout + done.stderr
    assert "reclaiming stale build slot (owner-dead)" in done.stderr
    assert box["marker"].exists()
    assert not box["lock"].exists()


def test_two_contenders_for_one_stale_slot_admit_exactly_one_owner(box) -> None:
    # BUILD-GUARD-TESTS-2-MUTEX-SERIALISATION: both see the same dead-owner lock; one reclaims it and the
    # other must find the winner's fresh lock. The winner holds the slot until the loser has given up.
    _write_lock(box, _dead_pid(), 1, expires_in_minutes=60)
    release = box["dir"] / "release.txt"
    memory_file = _write_json(box["dir"] / "memory.json", OK_MEMORY)
    table_file = _write_json(box["dir"] / "table.json", [])
    mutex = f"Local\\MLV-BuildGuardTest-{os.getpid()}-{time.time_ns()}"
    contenders = []
    for n in (1, 2):
        fragment = (
            f"Set-Content -LiteralPath '{box['dir'] / f'admitted-{n}.txt'}' -Value ok -Encoding ascii; "
            f"$d = (Get-Date).AddSeconds(60); "
            f"while (-not (Test-Path -LiteralPath '{release}') -and (Get-Date) -lt $d) {{ Start-Sleep -Milliseconds 50 }}"
        )
        contenders.append(subprocess.Popen(
            [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(GUARD),
             "-MemoryProbeFile", memory_file, "-ProcessTableFile", table_file, "-LockPath", str(box["lock"]),
             "-ExemptLogPath", str(box["exempt_log"]), "-MutexName", mutex, *FAST, "-DeadlineMinutes", "0.1",
             "-Command", fragment],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=_env(),
        ))
    try:
        deadline = time.monotonic() + 60
        while all(c.poll() is None for c in contenders):
            assert time.monotonic() < deadline, "neither contender gave up"
            time.sleep(0.05)
        release.write_text("go", encoding="ascii")
        outs = [c.communicate(timeout=60) for c in contenders]
    finally:
        for c in contenders:
            if c.poll() is None:
                c.kill()
                c.wait()
    codes = sorted(c.returncode for c in contenders)
    assert codes == [0, 3], outs
    admitted = [n for n in (1, 2) if (box["dir"] / f"admitted-{n}.txt").exists()]
    assert len(admitted) == 1, outs
    assert sum("BUILD_SLOT_BUSY" in out for out, _ in outs) == 1, outs
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


# --- the machine heavy slot (MLV-VM-HEAVY-SLOT-ADOPT-1) -----------------------------------------


def _stub_sha() -> str:
    return hashlib.sha256((HEAVY_STUB_DIR / "heavy-slot.ps1").read_bytes()).hexdigest()


def _heavy(box: dict, mode: str, mode_args: list, sha: str = None, shim: Path = HEAVY_STUB, extra: dict = None,
           lock: Path = None, memory: list = None) -> subprocess.CompletedProcess:
    # Only an omitted argument takes the default: an explicit [] or '' is a test input and reaches the guard as given.
    memory_file = _write_json(box["dir"] / "memory.json", OK_MEMORY if memory is None else memory)
    table_file = _write_json(box["dir"] / "table.json", [])
    env = _env(heavy_slot=shim, extra={
        "STUB_HEAVY_SLOT_MODE": mode,
        "STUB_HEAVY_SLOT_LOG": str(box["dir"] / "stub.log"),
        "MLV_LANE_RUN_DIR": str(box["dir"] / "lane-CARD-HS-1-r2-20261027T1420Z"),
        **(extra or {}),
    })
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GUARD),
         "-MemoryProbeFile", memory_file, "-ProcessTableFile", table_file, "-LockPath", str(box["lock"] if lock is None else lock),
         "-ExemptLogPath", str(box["exempt_log"]), "-HeavySlotSha256", _stub_sha() if sha is None else sha, *mode_args],
        capture_output=True, text=True, timeout=120, env=env, check=False,
    )


def _stub_calls(box: dict) -> list:
    log = box["dir"] / "stub.log"
    return [json.loads(line) for line in log.read_text(encoding="ascii").splitlines()] if log.exists() else []


def test_heavy_slot_is_outermost_and_the_build_exit_passes_through(box) -> None:
    stamp = box["dir"] / "built-at.txt"
    fragment = (
        f"Set-Content -LiteralPath '{stamp}' -Value ([DateTime]::UtcNow.ToString('o')) -Encoding ascii; "
        f"Write-Output \"held=$(Test-Path -LiteralPath '{box['lock']}')\"; cmd /c exit 9"
    )
    done = _heavy(box, "run", ["-Command", fragment])
    assert done.returncode == 9, done.stdout + done.stderr
    assert "held=True" in done.stdout, "the MLV build slot is still taken, nested inside the heavy slot"
    assert "HEAVY-SLOT TICKET stub-" in done.stderr, "heavy-slot's stderr is relayed"
    [call] = _stub_calls(box)
    argv = call["argv"]
    sep = argv.index("--")
    assert argv[:sep] == ["run", "--class", "shared", "--label", "mlv:CARD-HS-1:build", "--eta", "20m", "--wait", "60m",
                          "--exit-file", argv[sep - 1]]
    assert argv[sep + 1].lower().endswith("pwsh.exe") and "-EncodedCommand" in argv[sep + 2:]
    assert call["expectSha"] == _stub_sha() and call["expectKeeper"] == "1"
    # Outermost: the holder (heavy-slot's child) started before the build ran and ended after it.
    assert call["childStart"] < stamp.read_text(encoding="ascii").strip() < call["childEnd"]
    assert not Path(argv[sep - 1]).exists() and not box["lock"].exists()


def test_each_call_gets_a_fresh_exit_file(box) -> None:
    for _ in range(2):
        done = _heavy(box, "run", ["-Command", "cmd /c exit 0"])
        assert done.returncode == 0, done.stdout + done.stderr
    first, second = (c["argv"][c["argv"].index("--exit-file") + 1] for c in _stub_calls(box))
    assert first != second


def test_a_marked_75_is_a_requeue_never_a_build_failure(box) -> None:
    done = _heavy(box, "marked75", ["-Command", _marker_command(box)])
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build code=75 ticket=stub-" in done.stdout
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK" not in done.stdout
    assert "BUILD_GUARD_COMMAND_FAILED" not in done.stderr
    assert not box["marker"].exists() and not box["lock"].exists(), "nothing is built or held without the heavy slot"


@pytest.mark.parametrize(
    "mode, why", [("unmarked75", "no readable exit marker"), ("forged75", "does not name this run")]
)
def test_a_75_without_a_matching_marker_is_could_not_check(box, mode, why) -> None:
    done = _heavy(box, mode, ["-Command", _marker_command(box)])
    assert done.returncode == 75, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=75 reason=" in done.stdout
    assert why in done.stdout
    assert "BUILD_HEAVY_SLOT_REQUEUE" not in done.stdout
    assert not box["marker"].exists()


def test_a_marker_for_a_non_heavy_slot_code_is_never_a_requeue(box) -> None:
    # Adversary A6: only 75-77 can be the tool's own; a matching marker that says 9 must not turn exit 9 into 3.
    done = _heavy(box, "marked9", ["-Command", _marker_command(box)])
    assert done.returncode == 9, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=9 " in done.stdout
    assert "BUILD_HEAVY_SLOT_REQUEUE" not in done.stdout


def _library_driver(box: dict, body: str, lock: str, cwd: Path = None, extra: dict = None,
                    host_tail: str = None) -> subprocess.CompletedProcess:
    driver = box["dir"] / "lib-driver.ps1"
    driver.write_text(
        "param([string]$Guard, [string]$Lock, [string]$Mem, [string]$Table, [string]$Sha)\n"
        ". $Guard -Library\n"
        "$opts = @{ LockPath = $Lock; MemoryProbeFile = $Mem; ProcessTableFile = $Table; HeavySlotSha256 = $Sha; PollSeconds = 0.2 }\n"
        + body,
        encoding="ascii",
    )
    env = _env(heavy_slot=HEAVY_STUB, extra={"STUB_HEAVY_SLOT_MODE": "run", "STUB_HEAVY_SLOT_LOG": str(box["dir"] / "stub.log"),
                                             **(extra or {})})
    driver_args = ["-Guard", str(GUARD), "-Lock", lock, "-Mem", _write_json(box["dir"] / "memory.json", OK_MEMORY),
                   "-Table", _write_json(box["dir"] / "table.json", []), "-Sha", _stub_sha()]
    if host_tail is None:
        invocation = ["-File", str(driver), *driver_args]
    else:
        # A persistent host: an 'exit' in the guard ends the driver script, and the host goes on to run host_tail.
        quoted = " ".join(a if i % 2 == 0 else "'" + a.replace("'", "''") + "'" for i, a in enumerate(driver_args))
        invocation = ["-Command", f"& '{driver}' {quoted}; Write-Output \"driver-exit=$LASTEXITCODE\"\n{host_tail}"]
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", *invocation],
        capture_output=True, text=True, timeout=120, env=env, check=False, cwd=str(cwd or box["dir"]),
    )


def test_a_relative_lock_path_still_builds_under_the_heavy_slot(box) -> None:
    # Adversary A1: Split-Path of a bare file name is empty; the guard resolves the lock path first.
    done = _library_driver(
        box,
        "$ErrorActionPreference = 'Stop'\n"
        "Invoke-BuildGuard -Label 'rel' -Options $opts -Action { param($Jobs) Write-Output \"built=$Jobs\" }\n",
        "build-slot.lock",
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "built=4" in done.stdout
    assert len(_stub_calls(box)) == 1


def test_a_throw_after_admission_still_releases_the_heavy_slot(box) -> None:
    # Adversary A2: a Library host that catches a throw raised after admission must not keep the machine slot.
    done = _library_driver(
        box,
        # DeadlineMinutes this large makes AddMinutes throw after admission and before the build slot (the adversary's input).
        "$opts.DeadlineMinutes = 10000000000\n"
        "try { Invoke-BuildGuard -Label 'thrower' -Options $opts -Action { param($Jobs) } } catch { Write-Output \"caught=$($_.Exception.GetType().Name)\" }\n"
        "$deadline = (Get-Date).AddSeconds(30)\n"
        f"while (-not (Test-Path -LiteralPath '{box['dir'] / 'stub.log'}') -and (Get-Date) -lt $deadline) {{ Start-Sleep -Milliseconds 200 }}\n"
        f"Write-Output \"released=$(Test-Path -LiteralPath '{box['dir'] / 'stub.log'}')\"\n",
        str(box["lock"]),
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "caught=" in done.stdout, done.stdout + done.stderr
    assert "released=True" in done.stdout, "the holder must end while the host that caught the throw is still alive"
    assert _stub_calls(box)[0]["childEnd"]


def test_a_missing_shim_fails_open_with_one_warn(box) -> None:
    done = _heavy(box, "run", ["-Command", _marker_command(box)], shim=NO_HEAVY_SLOT)
    assert done.returncode == 0, done.stdout + done.stderr
    warns = [line for line in done.stderr.splitlines() if "HEAVY_SLOT_WARN" in line]
    assert len(warns) == 1 and "shim=missing" in warns[0] and "fail open" in warns[0], done.stderr
    assert box["marker"].exists(), "fail open: the build still runs"
    assert _stub_calls(box) == []


def test_a_pin_mismatch_refuses_before_anything_runs(box) -> None:
    done = _heavy(box, "run", ["-Command", _marker_command(box)], sha=PINNED_HEAVY_SLOT_SHA)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REFUSED label=build reason=pin-mismatch" in done.stdout
    assert f"expected={PINNED_HEAVY_SLOT_SHA} actual={_stub_sha()}" in done.stdout
    assert _stub_calls(box) == [], "heavy-slot is never called"
    assert not box["marker"].exists() and not box["lock"].exists()


def test_print_jobs_takes_no_heavy_slot(box) -> None:
    done = _heavy(box, "run", ["-PrintJobs"])
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "-j4"
    assert _stub_calls(box) == []


def test_library_mode_builds_are_wrapped_too(box) -> None:
    driver = box["dir"] / "lib-heavy.ps1"
    driver.write_text(
        "param([string]$Guard, [string]$Lock, [string]$Mem, [string]$Table, [string]$Sha)\n"
        ". $Guard -Library\n"
        "$opts = @{ LockPath = $Lock; MemoryProbeFile = $Mem; ProcessTableFile = $Table; HeavySlotSha256 = $Sha }\n"
        "Invoke-BuildGuard -Label 'lib-heavy' -Options $opts -Action { param($Jobs) Write-Output \"jobs=$Jobs\" }\n",
        encoding="ascii",
    )
    env = _env(heavy_slot=HEAVY_STUB, extra={"STUB_HEAVY_SLOT_MODE": "run", "STUB_HEAVY_SLOT_LOG": str(box["dir"] / "stub.log")})
    done = subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(driver), "-Guard", str(GUARD),
         "-Lock", str(box["lock"]), "-Mem", _write_json(box["dir"] / "memory.json", OK_MEMORY),
         "-Table", _write_json(box["dir"] / "table.json", []), "-Sha", _stub_sha()],
        capture_output=True, text=True, timeout=120, env=env, check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "jobs=4" in done.stdout
    [call] = _stub_calls(box)
    assert "mlv:hub:build" in call["argv"], "no lane run dir: the hub's label"


def test_a_killed_guard_does_not_leave_the_heavy_slot_held(box) -> None:
    memory_file = _write_json(box["dir"] / "memory.json", OK_MEMORY)
    table_file = _write_json(box["dir"] / "table.json", [])
    env = _env(heavy_slot=HEAVY_STUB, extra={"STUB_HEAVY_SLOT_MODE": "run", "STUB_HEAVY_SLOT_LOG": str(box["dir"] / "stub.log")})
    guard = subprocess.Popen(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(GUARD), "-MemoryProbeFile", memory_file,
         "-ProcessTableFile", table_file, "-LockPath", str(box["lock"]), "-ExemptLogPath", str(box["exempt_log"]),
         "-HeavySlotSha256", _stub_sha(), "-Command", "Start-Sleep -Seconds 90"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    try:
        deadline = time.monotonic() + 60
        while not list((box["dir"] / "heavy-slot").glob("*.ready")):
            assert guard.poll() is None and time.monotonic() < deadline, "the holder never started"
            time.sleep(0.1)
    finally:
        guard.kill()
        guard.wait()
    deadline = time.monotonic() + 30
    while not _stub_calls(box):
        assert time.monotonic() < deadline, "the holder outlived its guard, so the heavy slot stayed held"
        time.sleep(0.2)
    assert _stub_calls(box)[0]["childEnd"]


# r2 (Dot advisory pr-702): a ready file admits only while its holder still runs.
# Start-Sleep is shadowed in the driver so the guard's next look is taken only after heavy-slot has ended; that makes
# "the holder wrote ready and exited before the guard read it" deterministic instead of a 250 ms race.
_SLEEP_UNTIL_HEAVY_SLOT_ENDS = (
    "function Start-Sleep { param([int]$Milliseconds, [int]$Seconds)\n"
    "    $d = (Get-Date).AddSeconds(30)\n"
    "    while (-not ($lease -and $lease.Process.HasExited) -and (Get-Date) -lt $d) { [Threading.Thread]::Sleep(50) } }\n"
)


def test_a_ready_file_from_a_holder_that_already_exited_is_a_requeue(box) -> None:
    done = _library_driver(
        box,
        _SLEEP_UNTIL_HEAVY_SLOT_ENDS
        + f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
        extra={"STUB_HEAVY_SLOT_MODE": "readyexit", "STUB_HEAVY_SLOT_DELAY_S": "1"},
    )
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build code=0 ticket=stub-" in done.stdout, done.stdout + done.stderr
    assert "reason=holder-exited-after-ready" in done.stdout
    assert "heavy-slot: admitted" not in done.stderr
    assert not box["marker"].exists() and not box["lock"].exists(), "nothing is built without a live heavy slot"
    assert list((box["dir"] / "heavy-slot").iterdir()) == []


def test_a_live_holder_admits_runs_once_and_releases_cleanly(box) -> None:
    # Positive control: the holder stays alive until the stop file.
    done = _heavy(box, "run", ["-Command", f"Add-Content -LiteralPath '{box['marker']}' -Value ran; exit 5"])
    assert done.returncode == 5, done.stdout + done.stderr
    assert "heavy-slot: admitted label=mlv:CARD-HS-1:build" in done.stderr
    assert "heavy-slot: released label=mlv:CARD-HS-1:build" in done.stderr
    assert box["marker"].read_text(encoding="ascii").splitlines() == ["ran"], "the action ran exactly once"
    assert "HEAVY_SLOT_LOST_DURING_ACTION" not in done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE" not in done.stdout
    [call] = _stub_calls(box)
    assert call["childEnd"]
    assert list((box["dir"] / "heavy-slot").iterdir()) == []


@pytest.mark.parametrize("code", [75, 76, 77])
def test_a_marked_heavy_slot_code_before_ready_stays_a_requeue(box, code) -> None:
    done = _heavy(box, "marked", ["-Command", _marker_command(box)], extra={"STUB_HEAVY_SLOT_CODE": str(code)})
    assert done.returncode == 3, done.stdout + done.stderr
    assert f"BUILD_HEAVY_SLOT_REQUEUE label=build code={code} ticket=stub-" in done.stdout
    assert "holder-exited-after-ready" not in done.stdout
    assert not box["marker"].exists()


def test_exit_1_before_ready_stays_could_not_check(box) -> None:
    done = _heavy(box, "bare", ["-Command", _marker_command(box)], extra={"STUB_HEAVY_SLOT_CODE": "1"})
    assert done.returncode == 1, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1 reason=heavy-slot ended before admitting the build" in done.stdout
    assert "BUILD_HEAVY_SLOT_REQUEUE" not in done.stdout
    assert not box["marker"].exists()


def test_losing_the_heavy_slot_during_the_build_is_classified_not_silent(box) -> None:
    # heavy-slot ends 3 s after admission while the build still runs; the build is not killed and keeps its code.
    stub_log = box["dir"] / "stub.log"
    fragment = (
        f"$d = (Get-Date).AddSeconds(30); while (-not (Test-Path -LiteralPath '{stub_log}') -and (Get-Date) -lt $d) "
        "{ Start-Sleep -Milliseconds 100 }; Start-Sleep -Seconds 2; "
        f"Set-Content -LiteralPath '{box['marker']}' -Value ran; exit 7"
    )
    done = _heavy(box, "readyexit", ["-Command", fragment], extra={"STUB_HEAVY_SLOT_HOLD_S": "3"})
    assert done.returncode == 7, done.stdout + done.stderr
    assert "heavy-slot: admitted" in done.stderr
    assert "HEAVY_SLOT_LOST_DURING_ACTION label=build code=0 utc=" in done.stdout, done.stdout + done.stderr
    assert "HEAVY_SLOT_LOST_DURING_ACTION label=build code=0 utc=" in done.stderr
    assert "heavy-slot: released" not in done.stderr
    assert box["marker"].exists(), "the build ran to its end"
    assert list((box["dir"] / "heavy-slot").iterdir()) == []


def test_a_ready_file_after_the_give_up_still_gets_a_stop_file(box) -> None:
    # r1 adversary A1: the give-up fires, then the holder writes ready before the finally; the host lives on.
    done = _library_driver(
        box,
        "function Write-Output { param([Parameter(ValueFromPipeline = $true)] $InputObject)\n"
        "    $d = (Get-Date).AddSeconds(30)\n"
        "    while ($lease -and -not (Test-Path -LiteralPath $lease.Ready) -and (Get-Date) -lt $d) { [Threading.Thread]::Sleep(50) }\n"
        "    Microsoft.PowerShell.Utility\\Write-Output $InputObject }\n"
        "$opts.HeavySlotWaitMinutes = 0; $script:HeavySlotGiveUpSlackMinutes = 0\n"
        f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
        host_tail=(
            "$d = (Get-Date).AddSeconds(20)\n"
            f"while (-not (Test-Path -LiteralPath '{box['dir'] / 'stub.log'}') -and (Get-Date) -lt $d) {{ Start-Sleep -Milliseconds 200 }}\n"
            f"Write-Output \"released=$(Test-Path -LiteralPath '{box['dir'] / 'stub.log'}')\"\n"
        ),
    )
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1 reason=no admission" in done.stdout, done.stdout + done.stderr
    assert "driver-exit=1" in done.stdout
    assert "released=True" in done.stdout, "the holder must end while the host is still alive"
    assert not box["marker"].exists()


def test_a_shim_whose_interpreter_cannot_start_fails_open(box) -> None:
    # r1 adversary A2: the shim starts, but its bare interpreter does not resolve (cmd exit 9009).
    shim_dir = box["dir"] / "shim"
    shim_dir.mkdir()
    shutil.copyfile(HEAVY_STUB_DIR / "heavy-slot.ps1", shim_dir / "heavy-slot.ps1")
    shim = shim_dir / "heavy-slot.cmd"
    shim.write_text("@echo off\r\nno-such-pwsh-for-a2 -NoProfile -File \"%~dp0heavy-slot.ps1\" %*\r\nexit /b %ERRORLEVEL%\r\n", encoding="ascii")
    done = _heavy(box, "run", ["-Command", _marker_command(box)], shim=shim)
    assert done.returncode == 0, done.stdout + done.stderr
    warns = [line for line in done.stderr.splitlines() if "HEAVY_SLOT_WARN" in line]
    assert len(warns) == 1 and "not-startable" in warns[0] and "fail open" in warns[0], done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK" not in done.stdout
    assert box["marker"].exists(), "fail open: the build still runs"


def test_a_percent_in_the_lock_dir_fails_open_instead_of_being_expanded(box) -> None:
    # r1 adversary A3: cmd expands %OS% inside quotes, so the path heavy-slot would see is not the one given.
    lock = box["dir"] / "pct-%OS%" / "build-slot.lock"
    done = _heavy(box, "run", ["-Command", _marker_command(box)], lock=lock)
    assert done.returncode == 0, done.stdout + done.stderr
    warns = [line for line in done.stderr.splitlines() if "HEAVY_SLOT_WARN" in line]
    assert len(warns) == 1 and "path-has-percent" in warns[0] and "fail open" in warns[0], done.stderr
    assert box["marker"].exists()
    assert _stub_calls(box) == []


def test_heavy_slots_own_pin_mismatch_refuses_before_the_build(box) -> None:
    # r1 adversary A4: the script heavy-slot runs may differ from the one hashed; its own PIN MISMATCH line refuses.
    done = _heavy(box, "pinwarn", ["-Command", _marker_command(box)])
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REFUSED label=build reason=pin-mismatch-at-run" in done.stdout
    assert not box["marker"].exists() and not box["lock"].exists()
    [call] = _stub_calls(box)
    assert call["childEnd"], "the slot was given back before the refusal"


def test_a_ready_file_from_an_ended_holder_is_a_requeue_while_heavy_slot_still_runs(box) -> None:
    # r2 adversary A1: the cmd wrapper outlives the release (heavy-slot's own work after its child), so liveness is the
    # holder's, named in the ready file, not the wrapper's.
    done = _heavy(box, "readyorphan", ["-Command", _marker_command(box)], extra={"STUB_HEAVY_SLOT_HOLD_S": "4"})
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build code=0 ticket=stub-" in done.stdout
    assert "reason=holder-exited-after-ready" in done.stdout
    assert not box["marker"].exists() and not box["lock"].exists()


def test_a_string_wait_is_numeric_in_the_hung_tool_bound(box) -> None:
    # r2 adversary A2: '0.01' + 15 must be 15.01 minutes, not the string '0.0115' (0.69 s).
    done = _library_driver(
        box,
        "$opts.HeavySlotWaitMinutes = '0.01'\n"
        "Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs) Write-Output \"built=$Jobs\" }\n",
        str(box["lock"]),
        extra={"STUB_HEAVY_SLOT_DELAY_S": "3"},
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "built=4" in done.stdout
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK" not in done.stdout


def test_an_unreadable_heavy_slot_log_at_admission_fails_closed(box) -> None:
    # r2 adversary A3: a log nobody can read hides a PIN MISMATCH; the slot is given back and nothing is built.
    done = _heavy(box, "locklog", ["-Command", _marker_command(box)])
    assert done.returncode == 1, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1 reason=heavy-slot log unreadable at admission" in done.stdout
    assert not box["marker"].exists() and not box["lock"].exists()
    [call] = _stub_calls(box)
    assert call["childEnd"], "the slot was given back"


def test_a_holder_lost_after_its_ready_file_vanished_is_still_classified(box) -> None:
    # r2 adversary pass 2 A2: the holder ends and its ready file is gone while the wrapper still runs; the identity read
    # at admission is kept, so the release still reports the loss.
    hs = box["dir"] / "heavy-slot"
    fragment = (
        f"$r = @(Get-ChildItem -LiteralPath '{hs}' -Filter *.ready)[0].FullName; "
        "$h = [int]([regex]::Match((Get-Content -LiteralPath $r -Raw), 'holder=(\\d+)').Groups[1].Value); "
        "$d = (Get-Date).AddSeconds(30); while ((Get-Process -Id $h -ErrorAction Ignore) -and (Get-Date) -lt $d) "
        "{ Start-Sleep -Milliseconds 100 }; Remove-Item -LiteralPath $r -Force; "
        f"Set-Content -LiteralPath '{box['marker']}' -Value ran; exit 7"
    )
    done = _heavy(box, "readyorphan", ["-Command", fragment],
                  extra={"STUB_HEAVY_SLOT_CHILD_S": "3", "STUB_HEAVY_SLOT_HOLD_S": "10"})
    assert done.returncode == 7, done.stdout + done.stderr
    assert "heavy-slot: admitted" in done.stderr
    assert "HEAVY_SLOT_LOST_DURING_ACTION label=build code=0 utc=" in done.stdout, done.stdout + done.stderr
    assert "heavy-slot: released" not in done.stderr
    assert box["marker"].exists()


def test_a_stop_file_that_cannot_be_written_still_ends_the_holder(box) -> None:
    # r2 adversary pass 2 A1: the heavy-slot dir denies file creation at release, in a host that lives on.
    hs = box["dir"] / "heavy-slot"
    pid_file = box["dir"] / "holder-pid.txt"
    deny = ("[System.Security.AccessControl.FileSystemAccessRule]::new("
            "[Security.Principal.WindowsIdentity]::GetCurrent().User, 'CreateFiles', 'Deny')")
    done = _library_driver(
        box,
        "Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs)\n"
        f"    $r = @(Get-ChildItem -LiteralPath '{hs}' -Filter *.ready)[0].FullName\n"
        f"    Set-Content -LiteralPath '{pid_file}' -Value ([regex]::Match((Get-Content -LiteralPath $r -Raw), 'holder=(\\d+)').Groups[1].Value)\n"
        f"    $acl = Get-Acl -LiteralPath '{hs}'; $acl.AddAccessRule({deny}); Set-Acl -LiteralPath '{hs}' -AclObject $acl }}\n",
        str(box["lock"]),
        host_tail=(
            f"$acl = Get-Acl -LiteralPath '{hs}'; [void]$acl.RemoveAccessRule({deny}); Set-Acl -LiteralPath '{hs}' -AclObject $acl\n"
            f"$h = [int](Get-Content -LiteralPath '{pid_file}' -Raw)\n"
            "$d = (Get-Date).AddSeconds(20)\n"
            "while ((Get-Process -Id $h -ErrorAction Ignore) -and (Get-Date) -lt $d) { Start-Sleep -Milliseconds 200 }\n"
            "Write-Output \"holder-ended=$(-not (Get-Process -Id $h -ErrorAction Ignore))\"\n"
        ),
    )
    assert "holder-ended=True" in done.stdout, done.stdout + done.stderr
    assert "stop file not written" in done.stderr, done.stderr
    assert "BUILD_HEAVY_SLOT_" not in done.stdout, "admitted and built, never refused"


def test_a_heavy_slot_lost_while_waiting_for_memory_is_a_requeue_before_the_action(box) -> None:
    # r2 adversary 090d9006-A1: the holder is alive at admission, ends during a 5 s memory poll, and the next poll passes;
    # the action must not start on a slot that is already gone.
    done = _heavy(box, "readyexit", ["-PollSeconds", "5", "-DeadlineMinutes", "1", "-Command", _marker_command(box)],
                  extra={"STUB_HEAVY_SLOT_HOLD_S": "3"}, memory=[LOW_PHYS, OK_MEMORY[0]])
    assert done.returncode == 3, done.stdout + done.stderr
    assert "heavy-slot: admitted" in done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build code=0 ticket=stub-" in done.stdout, done.stdout + done.stderr
    assert "reason=holder-exited-before-action" in done.stdout
    assert "HEAVY_SLOT_LOST_DURING_ACTION" not in done.stdout + done.stderr
    assert not box["marker"].exists() and not box["lock"].exists(), "nothing is built without a live heavy slot"
    assert list((box["dir"] / "heavy-slot").iterdir()) == []


def test_a_ready_file_whose_slot_owner_died_is_no_admission(box) -> None:
    # r2 adversary 090d9006-A2: only the heavy-slot process named on the ready file's first line (it owns the slot mutex)
    # is killed, after the ready file and before the guard's next look; the holder child and the guard live on. The host
    # outlives the guard, so the holder ending proves the stop file, not the guard's death.
    holder_file = box["dir"] / "holder-pid.txt"
    kill_owner = (
        "function Start-Sleep { param([int]$Milliseconds, [int]$Seconds)\n"
        "    if (-not $lease -or $script:ownerKilled) { return }\n"
        "    $d = (Get-Date).AddSeconds(30)\n"
        "    while (-not ((Get-Content -LiteralPath $lease.Ready -Raw -ErrorAction Ignore) -match 'holder=') -and (Get-Date) -lt $d) { [Threading.Thread]::Sleep(50) }\n"
        "    $text = Get-Content -LiteralPath $lease.Ready -Raw\n"
        "    $owner = [int]([regex]::Match($text, '\\A[^\\r\\n]*:(\\d+)').Groups[1].Value)\n"
        f"    Set-Content -LiteralPath '{holder_file}' -Value ([regex]::Match($text, 'holder=(\\d+)').Groups[1].Value)\n"
        "    Stop-Process -Id $owner -Force; Wait-Process -Id $owner -Timeout 20 -ErrorAction Ignore\n"
        "    $script:ownerKilled = $true }\n"
    )
    done = _library_driver(
        box,
        kill_owner
        + f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
        extra={"STUB_HEAVY_SLOT_DELAY_S": "1"},
        host_tail=(
            f"$h = [int](Get-Content -LiteralPath '{holder_file}' -Raw)\n"
            "$d = (Get-Date).AddSeconds(15)\n"
            "while ((Get-Process -Id $h -ErrorAction Ignore) -and (Get-Date) -lt $d) { [Threading.Thread]::Sleep(100) }\n"
            "Write-Output \"holder-ended=$(-not (Get-Process -Id $h -ErrorAction Ignore))\"\n"
        ),
    )
    assert "driver-exit=3" in done.stdout, done.stdout + done.stderr
    # Either the guard sees the owner gone first, or the holder (which watches its owner since r3b) has already ended.
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build " in done.stdout, done.stdout + done.stderr
    assert re.search(r"reason=(slot-owner|holder)-exited-after-ready", done.stdout), done.stdout
    assert "heavy-slot: admitted" not in done.stderr
    assert not box["marker"].exists() and not box["lock"].exists(), "nothing is built on a slot whose owner is gone"
    assert "holder-ended=True" in done.stdout, "the orphaned holder is stopped while the host lives on"


def test_a_heavy_slot_lost_during_the_process_census_is_a_requeue_before_the_action(box) -> None:
    # r3b adversary b16f1589-A1: the holder is alive after the build slot is taken and ends during the process census
    # (Get-BuildGuardJobs is wrapped to end it); the check before the action must come after the census.
    done = _library_driver(
        box,
        "$script:origJobs = ${function:Get-BuildGuardJobs}\n"
        "function Get-BuildGuardJobs([hashtable]$Options, [int]$SelfPid) {\n"
        "    if ($heavy -and $heavy.Holder) { Stop-Process -Id $heavy.Holder.Pid -Force; Wait-Process -Id $heavy.Holder.Pid -Timeout 20 -ErrorAction Ignore }\n"
        "    & $script:origJobs -Options $Options -SelfPid $SelfPid }\n"
        f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
    )
    assert done.returncode == 3, done.stdout + done.stderr
    assert "heavy-slot: admitted" in done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build " in done.stdout and "reason=holder-exited-before-action" in done.stdout, done.stdout + done.stderr
    assert "HEAVY_SLOT_LOST_DURING_ACTION" not in done.stdout + done.stderr
    assert not box["marker"].exists() and not box["lock"].exists(), "nothing is built without a live heavy slot"


def test_a_holder_that_outlived_its_slot_owner_still_sees_the_release(box) -> None:
    # r3b adversary b16f1589-A2: the slot owner (and so heavy-slot's wrapper) ends during the action; the holder lives on
    # in its 500 ms poll. The release must keep the stop file until that holder has seen it, while the host lives on.
    hs = box["dir"] / "heavy-slot"
    holder_file = box["dir"] / "holder-pid.txt"
    done = _library_driver(
        box,
        "Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs)\n"
        f"    $t = Get-Content -LiteralPath @(Get-ChildItem -LiteralPath '{hs}' -Filter *.ready)[0].FullName -Raw\n"
        f"    Set-Content -LiteralPath '{holder_file}' -Value ([regex]::Match($t, 'holder=(\\d+)').Groups[1].Value)\n"
        "    $o = [int]([regex]::Match($t, '\\A[^\\r\\n]*:(\\d+)').Groups[1].Value)\n"
        "    Stop-Process -Id $o -Force; Wait-Process -Id $o -Timeout 20 -ErrorAction Ignore }\n",
        str(box["lock"]),
        host_tail=(
            f"$h = [int](Get-Content -LiteralPath '{holder_file}' -Raw)\n"
            "$d = (Get-Date).AddSeconds(15)\n"
            "while ((Get-Process -Id $h -ErrorAction Ignore) -and (Get-Date) -lt $d) { [Threading.Thread]::Sleep(100) }\n"
            "Write-Output \"holder-ended=$(-not (Get-Process -Id $h -ErrorAction Ignore))\"\n"
        ),
    )
    assert "HEAVY_SLOT_LOST_DURING_ACTION label=build " in done.stdout, done.stdout + done.stderr
    assert "holder-ended=True" in done.stdout, "the holder missed the stop file and outlived the release"
    assert list(hs.iterdir()) == [], "the stop file goes once the holder has ended"


def test_the_slot_owner_identity_is_the_holders_never_a_reused_pid(box) -> None:
    # r3b adversary 78c7d246-A1: the owner pid is reused before the guard's first read. The identity the holder wrote
    # (owner=<pid>@<ticks>) is what counts, so a live process with another start time under that pid is not the owner.
    ready = box["dir"] / "x.ready"
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                "$t = (Get-Process -Id $PID).StartTime.ToUniversalTime().Ticks\n"
                f"Set-Content -LiteralPath '{ready}' -Value @(\"stub-1:$PID\", \"holder=$PID@$t\", \"owner=$PID@$($t + 1)\", 'end') -Encoding ascii\n"
                f"$lease = @{{ Ready = '{ready}'; Holder = $null; Owner = $null; Gone = ''; Process = (Get-Process -Id $PID) }}\n"
                "Write-Output \"state=$(Get-HeavySlotHolderState $lease) gone=$($lease.Gone)\"\n"
                f"Set-Content -LiteralPath '{ready}' -Value @(\"stub-1:$PID\", \"holder=$PID@$t\", \"owner=$PID@$t\", 'end') -Encoding ascii\n"
                f"$lease = @{{ Ready = '{ready}'; Holder = $null; Owner = $null; Gone = ''; Process = (Get-Process -Id $PID) }}\n"
                "Write-Output \"control=$(Get-HeavySlotHolderState $lease)\"\n"])
    assert "state=gone gone=slot-owner-exited" in done.stdout, done.stdout + done.stderr
    assert "control=alive" in done.stdout, "the owner the holder named, still running, is alive"


def test_a_holder_whose_owner_died_ends_even_when_the_stop_file_cannot_be_written(box) -> None:
    # r3b adversary 78c7d246-A2: the slot owner (and so heavy-slot's wrapper) ends during the action and the heavy-slot dir
    # denies file creation at release, in a host that lives on. The holder must still end.
    hs = box["dir"] / "heavy-slot"
    holder_file = box["dir"] / "holder-pid.txt"
    deny = ("[System.Security.AccessControl.FileSystemAccessRule]::new("
            "[Security.Principal.WindowsIdentity]::GetCurrent().User, 'CreateFiles', 'Deny')")
    done = _library_driver(
        box,
        "Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs)\n"
        f"    $t = Get-Content -LiteralPath @(Get-ChildItem -LiteralPath '{hs}' -Filter *.ready)[0].FullName -Raw\n"
        f"    Set-Content -LiteralPath '{holder_file}' -Value ([regex]::Match($t, 'holder=(\\d+)').Groups[1].Value)\n"
        f"    $acl = Get-Acl -LiteralPath '{hs}'; $acl.AddAccessRule({deny}); Set-Acl -LiteralPath '{hs}' -AclObject $acl\n"
        "    $o = [int]([regex]::Match($t, '\\A[^\\r\\n]*:(\\d+)').Groups[1].Value)\n"
        "    Stop-Process -Id $o -Force; Wait-Process -Id $o -Timeout 20 -ErrorAction Ignore }\n",
        str(box["lock"]),
        host_tail=(
            f"$acl = Get-Acl -LiteralPath '{hs}'; [void]$acl.RemoveAccessRule({deny}); Set-Acl -LiteralPath '{hs}' -AclObject $acl\n"
            f"$h = [int](Get-Content -LiteralPath '{holder_file}' -Raw)\n"
            "$d = (Get-Date).AddSeconds(15)\n"
            "while ((Get-Process -Id $h -ErrorAction Ignore) -and (Get-Date) -lt $d) { [Threading.Thread]::Sleep(100) }\n"
            "Write-Output \"holder-ended=$(-not (Get-Process -Id $h -ErrorAction Ignore))\"\n"
        ),
    )
    assert "HEAVY_SLOT_LOST_DURING_ACTION label=build " in done.stdout, done.stdout + done.stderr
    assert "holder-ended=True" in done.stdout, "a holder that outlived its slot owner kept running while the host lived"


def test_a_partly_written_ready_file_never_skips_the_owner_check(box) -> None:
    # r4 sol B1: the guard reads the ready file after its ticket and holder lines and before its owner line. That read must
    # not be kept: the complete record names a slot owner that is not running, so nothing is admitted or built.
    done = _heavy(box, "readypartial", ["-Command", _marker_command(box)],
                  extra={"STUB_HEAVY_SLOT_CHILD_S": "2", "STUB_HEAVY_SLOT_HOLD_S": "1"})
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build " in done.stdout, done.stdout + done.stderr
    assert "reason=slot-owner-exited-after-ready" in done.stdout, done.stdout
    assert "heavy-slot: admitted" not in done.stderr
    assert not box["marker"].exists() and not box["lock"].exists(), "nothing is built before the owner line is read"


def test_a_complete_ready_file_that_does_not_parse_is_could_not_check(box) -> None:
    # r4 sol B1: a ready file with its 'end' line whose holder line does not parse is a typed COULD-NOT-CHECK, never an
    # admission and never a wait that ends as a REQUEUE.
    done = _heavy(box, "readypartial", ["-Command", _marker_command(box)],
                  extra={"STUB_HEAVY_SLOT_CHILD_S": "0", "STUB_HEAVY_SLOT_FINAL": "malformed", "STUB_HEAVY_SLOT_HOLD_S": "1"})
    assert done.returncode == 1, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1 reason=ready file unreadable" in done.stdout, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE" not in done.stdout and "heavy-slot: admitted" not in done.stderr
    assert not box["marker"].exists() and not box["lock"].exists()


@pytest.mark.parametrize("final", ["none", "empty"])
def test_an_incomplete_ready_file_left_by_an_ended_heavy_slot_is_could_not_check(box, final) -> None:
    # sol r4 B1 (r5): the ready file was never completed (a failed Set-Content, then the holder's trap: 'none'), or is empty,
    # and heavy-slot has ended with 1. No complete record was ever read, so it is no identity-based REQUEUE.
    done = _heavy(box, "readypartial", ["-Command", _marker_command(box)],
                  extra={"STUB_HEAVY_SLOT_CHILD_S": "0", "STUB_HEAVY_SLOT_FINAL": final, "STUB_HEAVY_SLOT_CODE": "1"})
    assert done.returncode == 1, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1 reason=ready file incomplete (heavy-slot exit 1)" in done.stdout, \
        done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE" not in done.stdout and "heavy-slot: admitted" not in done.stderr
    assert not box["marker"].exists() and not box["lock"].exists()
    # r8 (3e68ca85-A1): the partial record's written holder line names a holder that has ended, so everything goes; an empty
    # one names nobody, so nothing proves its holder gone and the stop file stays.
    left = sorted(p.suffix for p in (box["dir"] / "heavy-slot").iterdir())
    assert left == ([] if final == "none" else [".stop"]), left


@pytest.mark.parametrize("text, verdict", [
    ("t:8420\nholder=8420@638971000020000000\n", "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1 reason=ready file incomplete (heavy-slot exit 1)"),
    ("", "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1 reason=ready file incomplete (heavy-slot exit 1)"),
    # Control: a complete record whose holder is not running is the REQUEUE r4 designed.
    ("t:8420\nholder=8420@638971000020000000\nowner=none\nend\n", "BUILD_HEAVY_SLOT_REQUEUE label=build code=1 ticket= reason=holder-exited-after-ready"),
])
def test_a_slot_gone_exit_without_a_holder_identity_never_requeues(box, text, verdict) -> None:
    # sol r4 B1 (r5), sol's literal record: every holder-exited classification (the loop's ready-file branch, its
    # wrapper-exited branch and the re-check after admission) goes through Exit-HeavySlotHolderGone. With an exited wrapper
    # and no complete ready record, it is COULD_NOT_CHECK; it never names a holder it has not read.
    ready = box["dir"] / "x.ready"
    ready.write_bytes(text.encode("ascii"))
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                "$p = [pscustomobject]@{ Id = 1; HasExited = $true; ExitCode = 1 }\n"
                "$p | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) $true }\n"
                f"$lease = @{{ Label = 'mlv:t:build'; BuildLabel = 'build'; Ready = '{ready}'; Stop = '{box['dir'] / 'x.stop'}'; "
                f"ExitFile = '{box['dir'] / 'x.exit.json'}'; Log = '{box['dir'] / 'x.log'}'; Lines = [System.Collections.Generic.List[string]]::new(); "
                "Process = $p; Ticket = ''; Holder = $null; Owner = $null; Gone = ''; Tail = ''; LogUnread = $false }\n"
                "Exit-HeavySlotHolderGone $lease\n"])
    assert verdict in done.stdout, done.stdout + done.stderr
    assert done.returncode == (3 if "REQUEUE" in verdict else 1), done.stdout + done.stderr
    if "REQUEUE" not in verdict:
        assert "BUILD_HEAVY_SLOT_REQUEUE" not in done.stdout


# r8 (adversary 3e68ca85-A1): a partial ready file (the holder was descheduled before its owner and end lines) with heavy-slot's
# cmd wrapper already ended. Cleanup used to delete the stop file at once, so the holder, resuming later, held the machine slot
# for as long as the host lived. The stop file now goes only once the holder is PROVEN gone (pid + start ticks); a holder line
# not on disk yet, or a holder still running after the 10 s wait, keeps it. The holder here sleeps through the cleanup.
_STOP_HOLDER_LEASE = (
    "$p = [pscustomobject]@{{ Id = 1; HasExited = $true; ExitCode = 1 }}\n"
    "$p | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {{ param($ms) $true }}\n"
    "$lease = @{{ Label = 'mlv:t:build'; BuildLabel = 'build'; Ready = '{ready}'; Stop = '{stop}'; "
    "ExitFile = '{d}\\x.exit.json'; Log = '{d}\\x.log'; Lines = [System.Collections.Generic.List[string]]::new(); "
    "Process = $p; Ticket = ''; Holder = $null; Owner = $null; Gone = ''; Tail = ''; LogUnread = $false; ReadySeen = $true }}\n"
)


def _sleeping_holder(stop: Path, result: Path, delay: int) -> subprocess.Popen:
    # Sleeps $delay s, then polls for the stop file for at most 20 s: 'stopped' when it saw it, else 'held'.
    script = (f"Start-Sleep -Seconds {delay}; $t = [Diagnostics.Stopwatch]::StartNew(); "
              f"while (-not (Test-Path -LiteralPath '{stop}')) {{ if ($t.Elapsed.TotalSeconds -gt 20) "
              f"{{ Set-Content -LiteralPath '{result}' -Value held; exit 2 }}; Start-Sleep -Milliseconds 200 }}; "
              f"Set-Content -LiteralPath '{result}' -Value stopped")
    return subprocess.Popen([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], env=_env())


@pytest.mark.parametrize("partial, delay, kept", [
    ("holder", 2, False),    # the holder line is on disk: the 10 s wait sees the holder take the stop and end
    ("holder", 13, True),    # still asleep after the wait: not proven gone, so the stop file stays for it
    ("ticket", 2, True),     # no holder line yet: nothing can prove it gone, so the stop file stays
])
def test_a_partial_ready_file_keeps_the_stop_file_while_its_holder_may_still_run(box, partial, delay, kept) -> None:
    d = box["dir"]
    ready, stop, result = d / "x.ready", d / "x.stop", d / "holder-result.txt"
    holder = _sleeping_holder(stop, result, delay)
    try:
        line = "\"holder={0}@{1}`n\" -f $h.Id, $h.StartTime.ToUniversalTime().Ticks" if partial == "holder" else "''"
        done = _ps(["-Command",
                    f". '{GUARD}' -Library\n"
                    f"$h = Get-Process -Id {holder.pid}\n"
                    f"[IO.File]::WriteAllText('{ready}', \"t-1:1`n\" + ({line}))\n"
                    + _STOP_HOLDER_LEASE.format(ready=ready, stop=stop, d=d) +
                    "Exit-HeavySlotHolderGone $lease\n"])
        holder.wait(timeout=60)
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait()
    out = done.stdout + done.stderr
    assert done.returncode == 1, out
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1 reason=ready file incomplete" in done.stdout, out
    assert result.read_text(encoding="ascii").strip() == "stopped", f"the holder never saw a stop file: {out}"
    assert stop.exists() == kept, out


def test_the_stop_file_is_needed_until_its_holder_is_proven_gone(box) -> None:
    # The one predicate behind every stop-file write and delete (r8, 3e68ca85-A1 and its siblings): needed while heavy-slot's
    # wrapper runs, and after it ended until the holder named by a written holder line, complete record or not, is gone.
    d = box["dir"]
    cases = {
        "running-no-ready": (None, False),
        "ended-no-ready": (None, True),
        "ended-ticket-only": ("t-1:1`n", True),
        "ended-partial-live": ("t-1:1`nholder={live}`n", True),
        "ended-partial-dead": ("t-1:1`nholder=2147483646@1`n", True),
        "ended-complete-dead": ("t-1:1`nholder=2147483646@1`nowner=none`nend`n", True),
        # A holder line with no newline yet may be cut short ('8420@1' of '8420@1234...'), so it proves nothing.
        "ended-unfinished-line": ("t-1:1`nholder=2147483646@1", True),
    }
    body = f". '{GUARD}' -Library\n$me = Get-Process -Id $PID; $live = '{{0}}@{{1}}' -f $PID, $me.StartTime.ToUniversalTime().Ticks\n"
    for name, (text, exited) in cases.items():
        ready = d / f"{name}.ready"
        if text is not None:
            body += f"[IO.File]::WriteAllText('{ready}', (\"{text}\" -replace '\\{{live\\}}', $live))\n"
        body += (f"$p = [pscustomobject]@{{ Id = 1; HasExited = ${str(exited).lower()} }}\n"
                 f"$lease = @{{ Ready = '{ready}'; Process = $p; Holder = $null; ReadySeen = ${str(text is not None).lower()} }}\n"
                 f"Write-Output ('{name}=' + (Test-HeavySlotStopNeeded $lease))\n")
    done = _ps(["-Command", body])
    out = done.stdout + done.stderr
    want = {"running-no-ready": True, "ended-no-ready": True, "ended-ticket-only": True, "ended-partial-live": True,
            "ended-partial-dead": False, "ended-complete-dead": False, "ended-unfinished-line": True}
    for name, needed in want.items():
        assert f"{name}={needed}" in done.stdout, out


def test_the_holder_binds_the_slot_owner_to_its_exported_start_time(box) -> None:
    # r4 sol B2: the holder names the owner only when the process under the HEAVY_SLOT_HELD pid started at the exported
    # HEAVY_SLOT_HELD_START. Another start time (the pid was reused) or no readable value is written as @-1, which no
    # process matches. The owner here is this pwsh; the stop file exists, so each holder writes its ready file and ends.
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                f"$stop = '{box['dir'] / 'x.stop'}'; Set-Content -LiteralPath $stop -Value stop\n"
                "$me = (Get-Process -Id $PID).StartTime.ToUniversalTime()\n"
                "$exe = (Get-Process -Id $PID).Path\n"
                "foreach ($case in @(@('match', $me.ToString('o')), @('reused', '2025-10-26T18:26:40.0000000Z'), @('missing', ''), @('garbage', 'not-a-time'))) {\n"
                f"    $ready = '{box['dir']}' + '\\' + $case[0] + '.ready'\n"
                "    $env:HEAVY_SLOT_HELD = \"t-1:$PID\"; $env:HEAVY_SLOT_HELD_START = $case[1]\n"
                "    $null = & $exe -NoProfile -NonInteractive -EncodedCommand (New-HeavySlotHolder $ready $stop)\n"
                "    $owner = [regex]::Match((Get-Content -LiteralPath $ready -Raw), '(?m)^owner=(.*)$').Groups[1].Value.Trim()\n"
                "    Write-Output (\"{0}={1}\" -f $case[0], ($owner -replace \"^$PID@\", 'me@'))\n"
                "}\n"
                "Write-Output \"ticks=$($me.Ticks)\"\n"])
    out = done.stdout
    ticks = re.search(r"ticks=(\d+)", out)
    assert ticks, out + done.stderr
    assert f"match=me@{ticks.group(1)}" in out, out + done.stderr
    for case in ("reused", "missing", "garbage"):
        assert f"{case}=me@-1" in out, out + done.stderr


def test_a_slot_owner_whose_start_time_is_not_the_exported_one_is_no_admission(box) -> None:
    # r4 sol B2 end to end: heavy-slot exports a start time its pid no longer has; the build is never admitted.
    done = _heavy(box, "run", ["-Command", _marker_command(box)],
                  extra={"STUB_HEAVY_SLOT_HELD_START": "2025-10-26T18:26:40.0000000Z"})
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build " in done.stdout, done.stdout + done.stderr
    assert re.search(r"reason=(slot-owner|holder)-exited-after-ready", done.stdout), done.stdout
    assert "heavy-slot: admitted" not in done.stderr
    assert not box["marker"].exists() and not box["lock"].exists()


def test_a_slot_lost_while_the_admitted_line_is_written_is_a_requeue_before_the_action(box) -> None:
    # r4 sol B3: the admitted line can block on an undrained stderr; the holder ends while it does (the log call is wrapped
    # to end it). The slot is checked again after the line, so the action never starts and the loss is not "during".
    done = _library_driver(
        box,
        "$script:origLog = ${function:Write-BuildGuardLog}\n"
        "function Write-BuildGuardLog([string]$Message) {\n"
        "    if ($Message -like 'admitted label=*' -and $heavy -and $heavy.Holder) {\n"
        "        Stop-Process -Id $heavy.Holder.Pid -Force; Wait-Process -Id $heavy.Holder.Pid -Timeout 20 -ErrorAction Ignore }\n"
        "    & $script:origLog $Message }\n"
        f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
    )
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REQUEUE label=build " in done.stdout and "reason=holder-exited-before-action" in done.stdout, done.stdout + done.stderr
    assert "HEAVY_SLOT_LOST_DURING_ACTION" not in done.stdout + done.stderr
    assert not box["marker"].exists() and not box["lock"].exists(), "nothing is built without a live heavy slot"


# r4 sol B4/B5 seams: Get-Process is shadowed so the pid's process can change between calls (a reused pid) or lose its start
# time after the lookup (it ended). Stop-Process is shadowed too, so nothing real is ever stopped.
_PROCESS_SEAM = (
    "$script:calls = [System.Collections.Generic.List[string]]::new()\n"
    "function New-FakeProcess([long]$Ticks, [string]$Name, [switch]$Throws) {\n"
    "    $p = [pscustomobject]@{ Id = 8420; HasExited = $false; Handle = [IntPtr]1; Name = $Name }\n"
    "    if ($Throws) { $p | Add-Member -MemberType ScriptProperty -Name StartTime -Value { throw 'the process has exited' } }\n"
    "    else { $p | Add-Member -MemberType ScriptProperty -Name StartTime -Value ([scriptblock]::Create(\"[DateTime]::new($Ticks, 'Utc')\")) }\n"
    "    $p | Add-Member -MemberType ScriptMethod -Name Kill -Value { $script:calls.Add('KILL ' + $this.Name) }\n"
    "    return $p }\n"
    "function Stop-Process { param([int]$Id, [switch]$Force, $ErrorAction) $script:calls.Add(\"STOP-PID $Id\") }\n"
    "$script:seq = @()\n"
    "function Get-Process { param([int]$Id, $ErrorAction) $next = $script:seq[0]; if ($script:seq.Count -gt 1) { $script:seq = @($script:seq | Select-Object -Skip 1) }; $next }\n"
)


def test_a_holder_stop_never_reaches_a_process_that_took_its_pid(box) -> None:
    # r4 sol B4: the stop file cannot be written and the wrapper has ended, so the holder is stopped directly. Only the
    # process whose identity was checked may be stopped (through that same handle), never a pid resolved again later.
    lease = ("$lease = @{{ Label = 'mlv:t:build'; Stop = '{stop}'; Process = [pscustomobject]@{{ Id = 1; HasExited = $true }}; "
             "Holder = @{{ Pid = 8420; Ticks = 638971000020000000 }} }}\n").format(stop=box["dir"] / "no-such-dir" / "x.stop")
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n" + _PROCESS_SEAM + lease
                + "$orig = New-FakeProcess 638971000020000000 'original'; $repl = New-FakeProcess 638971000030000000 'replacement'\n"
                # Reused after the identity check: the first lookup is the holder, every later one the replacement.
                "$script:seq = @($orig, $repl); Send-HeavySlotStop $lease\n"
                "Write-Output \"after=[$($script:calls -join ',')]\"; $script:calls.Clear()\n"
                # Reused before the check: no lookup ever returns the holder.
                "$script:seq = @($repl); Send-HeavySlotStop $lease\n"
                "Write-Output \"before=[$($script:calls -join ',')]\"\n"])
    assert "after=[KILL original]" in done.stdout, done.stdout + done.stderr
    assert "before=[]" in done.stdout, done.stdout + done.stderr
    assert "reason=identity-unverified" in done.stderr, done.stderr


def test_a_start_time_that_cannot_be_read_after_the_lookup_is_gone_never_alive(box) -> None:
    # r4 sol B5: the holder is found, then ends before its start time is read, so the read throws. That is no proof of
    # life: the identity check is false and the holder state is gone, never alive.
    ready = box["dir"] / "x.ready"
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n" + _PROCESS_SEAM
                + f"Set-Content -LiteralPath '{ready}' -Value @('stub-1:8420', 'holder=8420@638971000020000000', 'owner=none', 'end') -Encoding ascii\n"
                "$script:seq = @((New-FakeProcess 0 'ended' -Throws))\n"
                "Write-Output \"identity=$(Test-HeavySlotIdentity @{ Pid = 8420; Ticks = 638971000020000000 })\"\n"
                f"$lease = @{{ Ready = '{ready}'; Holder = $null; Owner = $null; Gone = ''; Process = [pscustomobject]@{{ HasExited = $false }} }}\n"
                "Write-Output \"state=$(Get-HeavySlotHolderState $lease) gone=$($lease.Gone)\"\n"])
    assert "identity=False" in done.stdout, done.stdout + done.stderr
    assert "state=gone gone=holder-exited" in done.stdout, done.stdout + done.stderr


def test_a_half_written_log_line_is_never_kept_in_place_of_the_whole_one(box) -> None:
    # r4 adversary A2: the log is read while heavy-slot is still writing its PIN MISMATCH warning. The half line must not be
    # kept, or the finished line (same line count) is never read and the pin refusal is missed. Once heavy-slot has ended,
    # a last line without a newline is complete and is kept.
    log = box["dir"] / "x.log"
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                f"$lease = @{{ Log = '{log}'; Lines = [System.Collections.Generic.List[string]]::new(); LogUnread = $false; Tail = ''; "
                "Process = [pscustomobject]@{ HasExited = $false } }\n"
                f"[IO.File]::WriteAllText('{log}', \"HEAVY-SLOT TICKET t1 11111111111111111111111111111111`nHEAVY-SLOT WARN: PIN MIS\")\n"
                "Send-HeavySlotLines $lease 2>$null\n"
                f"[IO.File]::AppendAllText('{log}', \"MATCH: running changed script`n\")\n"
                "Send-HeavySlotLines $lease 2>$null\n"
                "Write-Output \"pin=$(@($lease.Lines | Where-Object { $_ -match 'PIN MISMATCH' }).Count) n=$($lease.Lines.Count)\"\n"
                f"[IO.File]::AppendAllText('{log}', 'HEAVY-SLOT last words')\n"
                "Send-HeavySlotLines $lease 2>$null\n"
                "Write-Output \"running=$($lease.Lines.Count) tail=[$($lease.Tail)]\"\n"
                "$lease.Process = [pscustomobject]@{ HasExited = $true }\n"
                "Send-HeavySlotLines $lease 2>$null\n"
                "Write-Output \"ended=$($lease.Lines.Count) last=[$($lease.Lines[-1])]\"\n"])
    assert "pin=1 n=2" in done.stdout, done.stdout + done.stderr
    assert "running=2 tail=[HEAVY-SLOT last words]" in done.stdout, done.stdout + done.stderr
    assert "ended=3 last=[HEAVY-SLOT last words]" in done.stdout, done.stdout + done.stderr


def test_no_slot_is_a_typed_line_never_a_silent_bypass(box) -> None:
    # r4 adversary A1: -NoSlot takes neither slot (it is for callers that build nothing, like -PrintJobs). The bypass is
    # written as a typed line, so it can never pass for a build under the machine heavy slot.
    done = _library_driver(
        box,
        "Invoke-BuildGuard -Label 'nslot' -NoSlot -Options $opts -Action { param($Jobs) Write-Output \"ACTION_RAN jobs=$Jobs\" }\n",
        str(box["lock"]),
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ACTION_RAN jobs=4" in done.stdout
    assert "BUILD_GUARD_NO_SLOT label=nslot reason=-NoSlot" in done.stderr, done.stderr
    assert _stub_calls(box) == []
    printed = _heavy(box, "run", ["-PrintJobs"])
    assert printed.stdout.strip() == "-j4" and "BUILD_GUARD_NO_SLOT label=build" in printed.stderr


@pytest.mark.parametrize("holder, owner", [
    ("holder=2147483648@638971000020000000", "owner=none"),
    ("holder=1@9223372036854775808", "owner=none"),
    ("holder=1@638971000020000000", "owner=2147483648@1"),
    ("holder=1@638971000020000000", "owner=1@9223372036854775808"),
])
def test_an_out_of_range_identity_in_the_ready_file_is_unreadable_not_a_throw(box, holder, owner) -> None:
    # r4 adversary A3 (advisory): a pid or tick count too large for its type is a typed unreadable, never a throw that skips
    # the COULD-NOT-CHECK path.
    ready = box["dir"] / "x.ready"
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                f"Set-Content -LiteralPath '{ready}' -Value @('t:1', '{holder}', '{owner}', 'end') -Encoding ascii\n"
                f"$lease = @{{ Ready = '{ready}'; Holder = $null; Owner = $null; Gone = ''; Process = [pscustomobject]@{{ HasExited = $false }} }}\n"
                "try { Write-Output \"state=$(Get-HeavySlotHolderState $lease)\" } catch { Write-Output \"threw=$($_.Exception.GetType().Name)\" }\n"])
    assert "state=unreadable" in done.stdout, done.stdout + done.stderr


def test_fractional_minutes_reach_heavy_slot_as_plain_decimals_in_any_culture(box) -> None:
    # r4 adversary 326a0345-A1: a Library caller under de-DE passes 0.5 minutes. heavy-slot reads only ^\d+(\.\d+)?m$, so
    # '0,5m' would be a usage error (a marked 77, a REQUEUE with nothing built). Both --wait and --eta go out as '0.5m'.
    done = _library_driver(
        box,
        "[cultureinfo]::CurrentCulture = 'de-DE'\n"
        "$opts.HeavySlotWaitMinutes = 0.5; $opts.HeavySlotEtaMinutes = 0.5\n"
        "Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs) Write-Output \"built=$Jobs\" }\n",
        str(box["lock"]),
    )
    assert done.returncode == 0 and "built=4" in done.stdout, done.stdout + done.stderr
    [call] = _stub_calls(box)
    argv = call["argv"]
    assert argv[argv.index("--wait") + 1] == "0.5m" and argv[argv.index("--eta") + 1] == "0.5m", argv
    assert all(re.match(r"^\d+(\.\d+)?m$", argv[argv.index(flag) + 1]) for flag in ("--wait", "--eta")), argv


@pytest.mark.parametrize("value", ["'soon'", "-5", "'NaN'"])
def test_an_eta_heavy_slot_cannot_read_is_refused_before_heavy_slot_starts(box, value) -> None:
    # r4 adversary 326a0345-A1 sibling: --eta gets the same pre-launch check as --wait, so a value heavy-slot would reject
    # is a typed refusal with nothing launched, never a usage exit turned into a REQUEUE.
    done = _library_driver(
        box,
        f"$opts.HeavySlotEtaMinutes = {value}\n"
        f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REFUSED label=build reason=invalid-option HeavySlotWaitMinutes=60 HeavySlotEtaMinutes=" in done.stdout, done.stdout
    assert _stub_calls(box) == [] and not box["marker"].exists()


@pytest.mark.parametrize("option, value", [
    ("HeavySlotEtaMinutes", "15372286729L"), ("HeavySlotEtaMinutes", "10081"),
    ("HeavySlotWaitMinutes", "-1"), ("HeavySlotWaitMinutes", "10081"),
])
def test_a_duration_heavy_slots_parser_cannot_hold_is_refused_before_heavy_slot_starts(box, option, value) -> None:
    # r5b adversary 7cea1a1d-A2: heavy-slot reads ^\d+(\.\d+)?m$ into TimeSpan.FromMinutes, so a finite eta past
    # TimeSpan's range overflows its parser and a negative wait is a usage error, both after the launch (a 77 that reads as
    # COULD_NOT_CHECK). Wait and eta must be 0..10080 minutes; anything else is a typed refusal with nothing launched.
    done = _library_driver(
        box,
        f"$opts.{option} = {value}\n"
        f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
    )
    assert done.returncode == 2, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REFUSED label=build reason=invalid-option" in done.stdout, done.stdout + done.stderr
    assert _stub_calls(box) == [] and not box["marker"].exists()


def test_inherited_slot_selection_variables_never_reach_heavy_slot(box) -> None:
    # r5b adversary 7cea1a1d-A1: HEAVY_SLOT_NAME picks other mutexes, HEAVY_SLOT_COUNT raises the capacity, HEAVY_SLOT_DIR
    # moves the queue and HEAVY_SLOT_KEEPER_OWNER relaxes the keeper check. A guarded build always takes the canonical
    # machine slot, so none of them is passed on, whichever caller (build-release, qscreen-probe, attr-3) set it.
    done = _library_driver(
        box,
        "Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs) Write-Output \"built=$Jobs\" }\n",
        str(box["lock"]),
        extra={"HEAVY_SLOT_NAME": "Global\\HeavySlotTest-sol", "HEAVY_SLOT_COUNT": "2",
               "HEAVY_SLOT_DIR": str(box["dir"] / "other-slots"), "HEAVY_SLOT_KEEPER_OWNER": "S-1-1-0"},
    )
    assert done.returncode == 0 and "built=4" in done.stdout, done.stdout + done.stderr
    [call] = _stub_calls(box)
    assert call["slotEnv"] == {"name": None, "count": None, "dir": None, "keeperOwner": None,
                               "held": None, "heldStart": None, "heldName": None}, call["slotEnv"]


def test_an_inherited_holder_not_proven_canonical_never_reaches_heavy_slot(box) -> None:
    # r7b adversary 01aa4e7b-A2: an ancestor run with HEAVY_SLOT_COUNT=2 holds slot 1 and exports its genuine HEAVY_SLOT_HELD.
    # heavy-slot would pass it through (it checks the slot set and ancestry, not the slot), admitting this build beside the
    # canonical holder. The guard keeps an inherited holder only when its held record proves slot 0 of one; this one has none.
    held = "20261028T000000000Z-4242-abcdef:4242"
    done = _heavy(box, "run", ["-Command", "exit 0"],
                  extra={"HEAVY_SLOT_HELD": held, "HEAVY_SLOT_HELD_START": "2026-10-28T00:00:00.0000000Z",
                         "HEAVY_SLOT_HELD_NAME": "Global\\HeavySlot"})
    assert done.returncode == 0, done.stdout + done.stderr
    [call] = _stub_calls(box)
    assert call["slotEnv"]["held"] is None and call["slotEnv"]["heldStart"] is None and call["slotEnv"]["heldName"] is None, call
    assert f"[build-guard] heavy-slot: inherited HEAVY_SLOT_HELD={held} not passed through" in done.stderr, done.stderr


def test_only_a_held_record_for_slot_0_of_one_counts_as_the_canonical_slot(tmp_path) -> None:
    # r7b adversary 01aa4e7b-A2, the record check: slot 0 of the one-slot Global\HeavySlot set passes; slot 1 of two, another
    # slot set, a missing or unreadable record and a malformed value do not.
    records = {
        "canon": {"slotName": "Global\\HeavySlot", "slotCount": 1, "slots": [0]},
        "two": {"slotName": "Global\\HeavySlot", "slotCount": 2, "slots": [1]},
        "zeroOfTwo": {"slotName": "Global\\HeavySlot", "slotCount": 2, "slots": [0]},
        "other": {"slotName": "Global\\HeavySlotTest", "slotCount": 1, "slots": [0]},
        # r7b adversary df03e61e-A1: kernel object names are case-sensitive, so Global\HEAVYSLOT-0 is another mutex.
        "upper": {"slotName": "Global\\HEAVYSLOT", "slotCount": 1, "slots": [0]},
    }
    for ticket, rec in records.items():
        _write_json(tmp_path / f"{ticket}.json", rec)
    (tmp_path / "junk.json").write_text("{", encoding="ascii")
    probes = ["canon:1", "two:1", "zeroOfTwo:1", "other:1", "upper:1", "missing:1", "junk:1", "no-pid"]
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n$script:HeavySlotHeldDir = '{tmp_path}'\n"
                + "".join(f"Write-Output ('{p} -> ' + [string](Test-HeavySlotHeldCanonical '{p}'))\n" for p in probes)])
    out = done.stdout + done.stderr
    assert re.search(r"^canon:1 -> $", done.stdout, re.M), out
    for p in probes[1:]:
        assert re.search(rf"^{re.escape(p)} -> \S", done.stdout, re.M), out


def test_a_heavy_slot_wrapper_that_ends_before_admission_still_leaves_the_stop_file(box) -> None:
    # r7b adversary 01aa4e7b-A1: heavy-slot's cmd wrapper ends (exit 1, no marker, no ready file) while the heavy-slot child it
    # started may still be queued. That child could admit a holder later, which a persistent host would never stop; the stop
    # file is written on every way out before admission, so the holder ends as soon as it starts.
    done = _heavy(box, "bare", ["-Command", "exit 0"], extra={"STUB_HEAVY_SLOT_CODE": "1"})
    assert done.returncode == 1, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=1" in done.stdout, done.stdout
    stops = list((box["dir"] / "heavy-slot").glob("*.stop"))
    assert len(stops) == 1, f"no stop file was left for a surviving heavy-slot child: {list((box['dir'] / 'heavy-slot').iterdir())}"


def test_a_heavy_slot_tree_kill_at_release_keeps_the_callers_exit_code(box) -> None:
    # r5b adversary 7cea1a1d-A3: the action ends with native exit 9 and the stop file cannot be written, so the release
    # ends heavy-slot with taskkill. taskkill must not overwrite the $LASTEXITCODE a Library caller exits with.
    done = _library_driver(
        box,
        "Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs)\n"
        "    New-Item -ItemType Directory -Path $heavy.Stop | Out-Null\n"
        "    cmd /c exit 9 }\n"
        "exit $LASTEXITCODE\n",
        str(box["lock"]),
    )
    assert "stop file not written" in done.stderr, done.stdout + done.stderr
    assert done.returncode == 9, done.stdout + done.stderr


@pytest.mark.parametrize("option", ["HeavySlotWaitMinutes", "HeavySlotEtaMinutes"])
def test_negative_zero_minutes_reach_heavy_slot_as_a_readable_duration(box, option) -> None:
    # r5b adversary eef65194-A1: -0.0 is in range but formats as '-0m', which heavy-slot's parser refuses after the launch.
    # It is zero, so it goes out as '0m' and the build runs.
    done = _library_driver(
        box,
        f"$opts.{option} = -0.0\n"
        "Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs) Write-Output \"built=$Jobs\" }\n",
        str(box["lock"]),
    )
    assert done.returncode == 0 and "built=4" in done.stdout, done.stdout + done.stderr
    [call] = _stub_calls(box)
    argv = call["argv"]
    flag = "--wait" if option == "HeavySlotWaitMinutes" else "--eta"
    assert argv[argv.index(flag) + 1] == "0m", argv


@pytest.mark.parametrize("host", [
    # eef65194-A2: strict mode and no $LASTEXITCODE yet (no native command ran in this host).
    "Set-StrictMode -Version Latest\n",
    # eef65194-A3: taskkill fails (the wrapper ended after the HasExited check) under a host that makes a failing native
    # command throw.
    "$global:LASTEXITCODE = 9; $ErrorActionPreference = 'Stop'; $PSNativeCommandUseErrorActionPreference = $true\n",
])
def test_a_failed_stop_file_falls_back_to_taskkill_without_a_throw_or_a_changed_exit_code(box, host) -> None:
    # r5b adversary eef65194-A2/A3: the stop file cannot be written, so heavy-slot is ended by taskkill. Neither reading
    # the caller's $LASTEXITCODE nor taskkill's own failure may throw out of the release; the caller's code is kept (and
    # stays unset when it was unset).
    stop = box["dir"] / "x.stop"
    stop.mkdir()
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n" + host +
                "$p = [pscustomobject]@{ Id = 2147483646; HasExited = $false }\n"
                f"$lease = @{{ Label = 'mlv:t:build'; Stop = '{stop}'; Process = $p; Holder = $null }}\n"
                "$had = [bool](Get-Variable -Name LASTEXITCODE -Scope Global -ErrorAction Ignore)\n"
                "try { Send-HeavySlotStop $lease; Write-Output \"returned had=$had now=$([bool](Get-Variable -Name LASTEXITCODE -Scope Global -ErrorAction Ignore)) code=$(if ($had) { $global:LASTEXITCODE })\" }\n"
                "catch { Write-Output \"threw=$($_.Exception.GetType().Name): $($_.Exception.Message)\" }\n"])
    assert "stop file not written" in done.stderr, done.stdout + done.stderr
    assert "threw=" not in done.stdout, done.stdout + done.stderr
    expected = "returned had=True now=True code=9" if "= 9" in host else "returned had=False now=False code="
    assert expected in done.stdout, done.stdout + done.stderr


def test_explicit_empty_inputs_reach_the_guard_as_given(box) -> None:
    # sol hardening (r3b review): _heavy(memory=[]) and sha='' are test inputs, not requests for the defaults.
    done = _heavy(box, "run", ["-Command", _marker_command(box)], sha="", memory=[])
    assert json.loads((box["dir"] / "memory.json").read_text(encoding="ascii")) == []
    assert done.returncode == 3, done.stdout + done.stderr
    assert f"BUILD_HEAVY_SLOT_REFUSED label=build reason=pin-mismatch expected= actual={_stub_sha()}" in done.stdout
    assert _stub_calls(box) == [] and not box["marker"].exists()


@pytest.mark.parametrize("value", ["15372286720L", "'NaN'", "'sixty'"])
def test_a_wait_the_hung_tool_bound_cannot_hold_is_refused_before_heavy_slot_starts(box, value) -> None:
    # r2 adversary 090d9006-A3: a Library host that catches throws passes a wait that overflows TimeSpan (or is not a
    # number). It is a typed refusal with nothing launched, not a throw after heavy-slot started.
    done = _library_driver(
        box,
        f"$opts.HeavySlotWaitMinutes = {value}\n"
        "try { Invoke-BuildGuard -Label 'build' -Options $opts -Action { param($Jobs)\n"
        f"    Set-Content -LiteralPath '{box['marker']}' -Value ran }} }} catch {{ Write-Output \"caught=$($_.Exception.GetType().Name)\" }}\n",
        str(box["lock"]),
        host_tail="Start-Sleep -Seconds 2\nWrite-Output host-alive\n",
    )
    assert "BUILD_HEAVY_SLOT_REFUSED label=build reason=invalid-option HeavySlotWaitMinutes=" in done.stdout, done.stdout + done.stderr
    assert "driver-exit=2" in done.stdout and "caught=" not in done.stdout
    assert "host-alive" in done.stdout
    assert _stub_calls(box) == [], "heavy-slot is never started"
    hs = box["dir"] / "heavy-slot"
    assert not hs.exists() or list(hs.iterdir()) == []
    assert not box["marker"].exists() and not box["lock"].exists()


def test_the_hung_tool_bound_is_monotonic() -> None:
    # r2 adversary A4: a wall-clock step must not move the bound. A clock step needs admin, so this pins the source.
    text = GUARD.read_text(encoding="ascii")
    body = text[text.index("function Enter-HeavySlot"):text.index("function Exit-HeavySlot(")]
    assert "[System.Diagnostics.Stopwatch]::StartNew()" in body and "$waitClock.Elapsed -ge $giveUp" in body
    assert "[DateTime]::UtcNow.AddMinutes" not in body and "[DateTime]::UtcNow -ge" not in body


def test_the_guard_pins_the_installed_heavy_slot_sha() -> None:
    text = GUARD.read_text(encoding="ascii")
    assert text.count(f"'{PINNED_HEAVY_SLOT_SHA}'") == 2, "the param default and the library default"
    assert "'--', $exe" in text and "heavy-slot.cmd" in text, "the shim with a quoted '--', never a bare -- to '&'"


# r6 sol r5b B2: the --exit-file marker is judged by the real admission-loop tail (stub mode 'marked': a matching marker,
# then the heavy-slot code) in a Library host, strict or not. Get-Content is shadowed for the marker file only, so the guard
# reads the shape under test; every other read reaches the disk.
_MARKER_SHAPES = {
    "as-written": "$text",
    "empty-object": "'{}'",
    "array": "'[]'",
    "null": "'null'",
    "scalar": "'75'",
    "no-reason": "($text | ConvertFrom-Json -AsHashtable | ForEach-Object { $_.Remove('reason'); $_ } | ConvertTo-Json -Compress)",
    "one-element-array": "('[' + $text.Trim() + ']')",
}


def _marker_shape_seam(shape: str) -> str:
    return (
        "function Get-Content { param([string]$LiteralPath, [switch]$Raw, $ErrorAction = 'Continue')\n"
        "    $text = Microsoft.PowerShell.Management\\Get-Content -LiteralPath $LiteralPath -Raw:$Raw -ErrorAction $ErrorAction\n"
        "    if ($LiteralPath -notlike '*.exit.json') { return $text }\n"
        f"    return {_MARKER_SHAPES[shape]} }}\n"
    )


@pytest.mark.parametrize("strict", [True, False], ids=["strict", "non-strict"])
@pytest.mark.parametrize("shape, code, verdict", [
    ("empty-object", 75, "could-not-check"),
    ("array", 75, "could-not-check"),
    ("null", 75, "could-not-check"),
    ("scalar", 75, "could-not-check"),
    ("one-element-array", 75, "could-not-check"),
    ("no-reason", 75, "requeue"),
    ("as-written", 75, "requeue"),
    ("as-written", 76, "requeue"),
])
def test_a_marker_of_any_shape_is_a_typed_verdict_in_a_strict_mode_host(box, shape, code, verdict, strict) -> None:
    # {}, [], null and a scalar parse, then threw PropertyNotFoundException in a strict-mode host before the typed
    # classification; a matching marker without 'reason' threw on that optional field. Every shape is now a typed verdict,
    # and the optional reason never changes the identity verdict. Only one JSON object counts ('[{...}]' is not one).
    done = _library_driver(
        box,
        ("Set-StrictMode -Version Latest\n" if strict else "") + _marker_shape_seam(shape)
        + f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
        extra={"STUB_HEAVY_SLOT_MODE": "marked", "STUB_HEAVY_SLOT_CODE": str(code)},
    )
    out = done.stdout + done.stderr
    assert "PropertyNotFound" not in out and "cannot be found on this object" not in out, out
    if verdict == "requeue":
        assert done.returncode == 3, out
        assert f"BUILD_HEAVY_SLOT_REQUEUE label=build code={code} ticket=stub-" in done.stdout, out
        reason = "reason= utc=" if shape == "no-reason" else "reason=timed out after 3600s waiting for the slot utc="
        assert reason in done.stdout, out
    else:
        assert done.returncode == code, out
        assert f"BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code={code} reason=" in done.stdout, out
        assert "BUILD_HEAVY_SLOT_REQUEUE" not in done.stdout, out
    assert not box["marker"].exists() and not box["lock"].exists(), "nothing is built on a marker verdict"


def test_a_foreign_marker_in_a_strict_mode_host_is_refused_as_foreign(box) -> None:
    done = _library_driver(
        box,
        "Set-StrictMode -Version Latest\n"
        f"Invoke-BuildGuard -Label 'build' -Options $opts -Action {{ param($Jobs) Set-Content -LiteralPath '{box['marker']}' -Value ran }}\n",
        str(box["lock"]),
        extra={"STUB_HEAVY_SLOT_MODE": "forged75"},
    )
    assert done.returncode == 75, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_COULD_NOT_CHECK label=build code=75 reason=the exit marker does not name this run" in done.stdout
    assert not box["marker"].exists()


@pytest.mark.parametrize("text", ["{}", "[]", "5", "null", '{"ownerPid":"x"}'])
def test_the_other_json_records_read_in_a_strict_mode_host_never_throw(box, text) -> None:
    # r6 B2 class sweep: the build-slot lock record (any process can write it), the memory probe and the process table are read
    # field by field. A malformed lock is unreadable (held while fresh, reclaimed once old), a field-less memory sample reads
    # 0 (fail closed) and a field-less process row is no peer, in a strict-mode host too.
    lock = box["lock"]
    lock.write_text(text, encoding="ascii")
    mem = _write_json(box["dir"] / "memory.json", [{}])
    table = _write_json(box["dir"] / "table.json", [{"ProcessId": 5}])
    done = _ps(["-Command",
                f". '{GUARD}' -Library\nSet-StrictMode -Version Latest\n"
                f"$o = @{{ LockPath = '{lock}'; MutexName = 'Local\\MLV-BuildGuardTest-{os.getpid()}-{abs(hash(text))}'; LockExpiryMinutes = 90; "
                f"MemoryProbeFile = '{mem}'; ProcessTableFile = '{table}' }}\n"
                "try { $r = Invoke-BuildSlotAcquire -Options $o -Label t; Write-Output \"fresh acquired=$($r.Acquired) held=$($r.Held)\" }"
                " catch { Write-Output \"threw=$($_.Exception.GetType().Name)\" }\n"
                f"(Get-Item -LiteralPath '{lock}').LastWriteTimeUtc = [DateTime]::UtcNow.AddMinutes(-5)\n"
                "try { $r = Invoke-BuildSlotAcquire -Options $o -Label t; Write-Output \"old acquired=$($r.Acquired)\" }"
                " catch { Write-Output \"threw=$($_.Exception.GetType().Name)\" }\n"
                "try { $m = Get-BuildGuardMemory -Options $o; Write-Output \"mem=$($m.PhysGB)/$($m.CommitGB)\" } catch { Write-Output \"threw=$($_.Exception.GetType().Name)\" }\n"
                "try { $j = Get-BuildGuardJobs -Options $o -SelfPid $PID; Write-Output \"jobs=$($j.Jobs)\" } catch { Write-Output \"threw=$($_.Exception.GetType().Name)\" }\n"])
    out = done.stdout + done.stderr
    assert "threw=" not in done.stdout, out
    if text != "null":
        assert "fresh acquired=False held=lock-being-written" in done.stdout, out
    assert "old acquired=True" in done.stdout, out
    assert "mem=0/0" in done.stdout and "jobs=4" in done.stdout, out


# r6 sol r5b B1 (card BUILD-GUARD-UNDRAINED-PIPE-SLOT-RELEASE-1): an inherited stdout or stderr that is open, full and never
# read. The guard's diagnostics must not block admission polling, the hung-tool bound or a release, and every typed refusal
# is printed only after the machine heavy slot was given back.
def _full_pipe():
    """A pipe whose buffer is full and which nobody reads: a thread stays blocked writing 128 KiB into it (nothing reaches
    disk). _release_pipe drains it afterwards."""
    read_fd, write_fd = os.pipe()

    def fill():
        try:
            os.write(write_fd, b"#" * 131072)
        except OSError:
            pass

    filler = threading.Thread(target=fill, daemon=True)
    filler.start()
    time.sleep(0.5)
    assert filler.is_alive(), "the pipe never filled"
    return read_fd, write_fd, filler


def _release_pipe(pipe, guard) -> str:
    read_fd, write_fd, filler = pipe
    chunks = []

    def drain():
        while True:
            try:
                data = os.read(read_fd, 65536)
            except OSError:
                return
            if not data:
                return
            chunks.append(data)

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()
    try:
        guard.wait(timeout=120)
    except subprocess.TimeoutExpired:
        guard.kill()
        guard.wait()
    filler.join(timeout=30)
    os.close(write_fd)
    drainer.join(timeout=60)
    if not drainer.is_alive():
        os.close(read_fd)
    return b"".join(chunks).decode("ascii", "replace")


def _heavy_popen(box: dict, mode_args: list, memory: list = None, sha: str = None, env_extra: dict = None,
                 shim: Path = HEAVY_STUB, **streams) -> subprocess.Popen:
    memory_file = _write_json(box["dir"] / "memory.json", OK_MEMORY if memory is None else memory)
    table_file = _write_json(box["dir"] / "table.json", [])
    env = _env(heavy_slot=shim, extra={"STUB_HEAVY_SLOT_MODE": "run", "STUB_HEAVY_SLOT_LOG": str(box["dir"] / "stub.log"),
                                             **(env_extra or {})})
    return subprocess.Popen(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GUARD),
         "-MemoryProbeFile", memory_file, "-ProcessTableFile", table_file, "-LockPath", str(box["lock"]),
         "-ExemptLogPath", str(box["exempt_log"]), "-HeavySlotSha256", _stub_sha() if sha is None else sha, *mode_args],
        stdout=streams.get("stdout", subprocess.DEVNULL), stderr=streams.get("stderr", subprocess.DEVNULL), env=env,
    )


def _released_within(box: dict, seconds: float) -> bool:
    # The stub logs its call (with the holder's end time) once heavy-slot ends, so the slot is given back.
    deadline = time.monotonic() + seconds
    while True:
        try:
            calls = _stub_calls(box)
        except ValueError:
            calls = []   # read while the stub was still writing its line
        if calls and calls[0]["childEnd"]:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


_RELEASE_BOUND_S = 60


def test_an_undrained_full_stderr_never_keeps_the_heavy_slot(box) -> None:
    # sol r5b B1 repro 1: -HeavySlotWaitMinutes 0 -Command 'exit 0' with stderr open, full and never read. The waiting line
    # used to block before the hung-tool clock started, so the admitted holder was never stopped. Now the guard admits, builds,
    # releases within the bound and exits 0, all while the pipe stays full.
    pipe = _full_pipe()
    started = time.monotonic()
    guard = _heavy_popen(box, ["-HeavySlotWaitMinutes", "0", "-Command", "exit 0"], stderr=pipe[1])
    try:
        released = _released_within(box, _RELEASE_BOUND_S)
        took = time.monotonic() - started
        try:
            code = guard.wait(timeout=30)
        except subprocess.TimeoutExpired:
            code = None
    finally:
        _release_pipe(pipe, guard)
    assert released, f"the heavy slot was still held {_RELEASE_BOUND_S} s after launch behind an undrained stderr"
    assert took < _RELEASE_BOUND_S
    assert code == 0, "the guard stayed blocked on its own diagnostics after the release"
    assert not box["lock"].exists()


def test_an_undrained_full_stdout_gives_the_heavy_slot_back_before_the_refusal(box) -> None:
    # sol r5b B1 repro 2 (same class, G:791-795): the memory refusal goes to a stdout open, full and never read. The slot is
    # given back first, so it is free within the bound even while that line is still blocked; once read, the line is there.
    pipe = _full_pipe()
    guard = _heavy_popen(box, ["-DeadlineMinutes", "0", "-Command", _marker_command(box)],
                         memory=[{"freePhysGB": 0, "freeCommitGB": 0}], stdout=pipe[1])
    try:
        released = _released_within(box, _RELEASE_BOUND_S)
    finally:
        out = _release_pipe(pipe, guard)
    assert released, "the heavy slot was still held while the refusal line waited on an undrained stdout"
    assert guard.returncode == 3, out
    assert "BUILD_MEM_REFUSED label=build freePhysGB=0 freeCommitGB=0" in out, out
    assert not box["marker"].exists() and not box["lock"].exists()


# r8 (adversary 3e68ca85-A2): an ancestor that holds a heavy slot (it exported HEAVY_SLOT_HELD) keeps it until this guard
# ends, so no typed line may wait on an undrained stdout there: the pin-mismatch refusal (nothing taken) and a refusal after
# the guard's own slot was given back both end the guard within the bound. The pipe is drained afterwards (no real hang).
_ANCESTOR_REFUSALS = {
    "pin-mismatch": (["-Command", "exit 0"], None, "0" * 64, 3),
    "memory": (["-DeadlineMinutes", "0", "-Command", "exit 0"], [{"freePhysGB": 0, "freeCommitGB": 0}], None, 3),
}


@pytest.mark.parametrize("refusal", sorted(_ANCESTOR_REFUSALS))
def test_a_refusal_under_an_ancestor_slot_never_waits_on_an_undrained_stdout(box, refusal) -> None:
    args, memory, sha, want = _ANCESTOR_REFUSALS[refusal]
    pipe = _full_pipe()
    guard = _heavy_popen(box, args, memory=memory, sha=sha, env_extra={"HEAVY_SLOT_HELD": f"anc-1:{os.getpid()}"}, stdout=pipe[1])
    try:
        try:
            code = guard.wait(timeout=30)
        except subprocess.TimeoutExpired:
            code = None
    finally:
        _release_pipe(pipe, guard)
    assert code == want, f"the {refusal} refusal waited on an undrained stdout while an ancestor held the machine heavy slot"
    assert not box["lock"].exists()


def _open_exclusive(path: Path):
    """Opens path for read with no sharing (FileShare.None), so another process can neither read nor hash it."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                wintypes.DWORD, wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle and handle != ctypes.c_void_p(-1).value, f"could not lock {path}: {ctypes.get_last_error()}"
    return lambda: k32.CloseHandle(wintypes.HANDLE(handle))


def test_a_cli_that_cannot_be_hashed_is_a_bounded_pin_refusal_under_an_ancestor_slot(box) -> None:
    # r8 adversary f99852cb-A1: heavy-slot.ps1 beside the shim is held open with no sharing, so Get-FileHash fails. That used
    # to be a non-terminating error record, which the host wrote synchronously to a full, undrained stderr while an ancestor
    # held the machine slot. Now it is a typed pin-mismatch refusal (actual=unreadable(...)), bounded, and the guard ends.
    shim_dir = box["dir"] / "shim"
    shutil.copytree(HEAVY_STUB_DIR, shim_dir)
    unlock = _open_exclusive(shim_dir / "heavy-slot.ps1")
    pipe = _full_pipe()
    guard = None
    try:
        guard = _heavy_popen(box, ["-Command", "exit 0"], shim=shim_dir / "heavy-slot.cmd",
                             env_extra={"HEAVY_SLOT_HELD": f"anc-1:{os.getpid()}"}, stderr=pipe[1])
        try:
            code = guard.wait(timeout=30)
        except subprocess.TimeoutExpired:
            code = None
    finally:
        if guard is not None:
            _release_pipe(pipe, guard)
        unlock()
    assert code == 3, "the guard blocked on an error record while an ancestor held the machine heavy slot"


def test_a_cli_that_cannot_be_hashed_is_a_typed_pin_refusal(box) -> None:
    # Control with drained streams: the refusal names why, and no error record reaches stderr.
    shim_dir = box["dir"] / "shim"
    shutil.copytree(HEAVY_STUB_DIR, shim_dir)
    unlock = _open_exclusive(shim_dir / "heavy-slot.ps1")
    try:
        done = _heavy(box, "run", ["-Command", _marker_command(box)], shim=shim_dir / "heavy-slot.cmd")
    finally:
        unlock()
    assert done.returncode == 3, done.stdout + done.stderr
    assert "BUILD_HEAVY_SLOT_REFUSED label=build reason=pin-mismatch expected=" in done.stdout and "actual=unreadable(" in done.stdout, done.stdout
    assert "Get-FileHash" not in done.stderr, done.stderr
    assert not box["marker"].exists()


def test_a_holder_that_cannot_be_inspected_keeps_its_stop_file(box) -> None:
    # r8 adversary f99852cb-A2: the wrapper has ended and the verified holder still runs, but its handle cannot be opened
    # (access denied). That is not proof it is gone, so the stop file is still needed and cleanup keeps it.
    stop = box["dir"] / "x.stop"
    stop.write_text("stop", encoding="ascii")
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                "function Get-Process { param([int]$Id, $ErrorAction)\n"
                "    $p = [pscustomobject]@{ Id = $Id }\n"
                "    $p | Add-Member -MemberType ScriptProperty -Name StartTime -Value { [DateTime]::new(638971000020000000, 'Utc') }\n"
                "    foreach ($n in 'Handle', 'HasExited') { $p | Add-Member -MemberType ScriptProperty -Name $n -Value { throw [ComponentModel.Win32Exception]::new(5) } }\n"
                "    $p }\n"
                "$w = [pscustomobject]@{ Id = 1; HasExited = $true }\n"
                f"$lease = @{{ Ready = '{box['dir'] / 'none.ready'}'; Stop = '{stop}'; ExitFile = '{box['dir'] / 'x.exit.json'}'; "
                f"Log = '{box['dir'] / 'x.log'}'; Process = $w; Holder = @{{ Pid = 8420; Ticks = 638971000020000000 }} }}\n"
                "Write-Output ('needed=' + (Test-HeavySlotStopNeeded $lease))\n"
                "Remove-HeavySlotLeaseFiles $lease\n"
                f"Write-Output ('kept=' + (Test-Path -LiteralPath '{stop}'))\n"])
    out = done.stdout + done.stderr
    assert "needed=True" in done.stdout, out
    assert "kept=True" in done.stdout, out


def test_a_pin_refusal_without_an_ancestor_slot_still_reaches_a_late_stdout_reader(box) -> None:
    # Control: no slot is held anywhere, so the typed line waits for its reader and is there once the pipe is drained.
    pipe = _full_pipe()
    guard = _heavy_popen(box, ["-Command", "exit 0"], sha="0" * 64, stdout=pipe[1])
    out = _release_pipe(pipe, guard)
    assert guard.returncode == 3, out
    assert "BUILD_HEAVY_SLOT_REFUSED label=build reason=pin-mismatch expected=" + "0" * 64 in out, out


@pytest.mark.parametrize("stream", ["stderr", "stdout"])
def test_a_closed_output_reader_still_gives_the_typed_verdict(box, stream) -> None:
    # Control: a reader that has already closed its end is a different failure (the write fails at once). The guard still
    # releases the slot and exits with its typed code.
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    if stream == "stderr":
        args, memory, code = ["-Command", "exit 0"], None, 0
    else:
        args, memory, code = ["-DeadlineMinutes", "0", "-Command", _marker_command(box)], [{"freePhysGB": 0, "freeCommitGB": 0}], 3
    try:
        guard = _heavy_popen(box, args, memory=memory, **{stream: write_fd})
    finally:
        os.close(write_fd)
    assert guard.wait(timeout=90) == code
    assert _released_within(box, 5)
    assert not box["marker"].exists() and not box["lock"].exists()


def test_a_drained_stderr_keeps_every_guard_line_in_order(box) -> None:
    # Control: with a reader, each guard line is written before the guard goes on, so the order is the order of events.
    done = _heavy(box, "run", ["-Command", "exit 0"])
    assert done.returncode == 0, done.stdout + done.stderr
    marks = ["heavy-slot: waiting label=", "HEAVY-SLOT TICKET stub-", "heavy-slot: admitted label=", "[build-guard] admitted label=build",
             "heavy-slot: released label="]
    at = [done.stderr.index(m) for m in marks]
    assert at == sorted(at), done.stderr


# r6b adversary A1/A2: the bounded stderr writer, driven in memory. A stream whose writes never complete, and an open that
# completes after a delay, stand in for an inherited stderr nobody drains (no pipe, nothing on disk).
_STDERR_SEAMS = (
    "Add-Type -TypeDefinition @'\n"
    "using System; using System.IO; using System.Threading; using System.Threading.Tasks;\n"
    "public class BuildGuardStuckStream : Stream {\n"
    "    public int Writes;\n"
    "    public override bool CanRead { get { return false; } } public override bool CanSeek { get { return false; } }\n"
    "    public override bool CanWrite { get { return true; } } public override long Length { get { return 0; } }\n"
    "    public override long Position { get { return 0; } set { } } public override void Flush() { }\n"
    "    public override int Read(byte[] b, int o, int c) { throw new NotSupportedException(); }\n"
    "    public override long Seek(long o, SeekOrigin s) { throw new NotSupportedException(); }\n"
    "    public override void SetLength(long v) { } public override void Write(byte[] b, int o, int c) { Thread.Sleep(-1); }\n"
    "    public override Task WriteAsync(byte[] b, int o, int c, CancellationToken t) {\n"
    "        Interlocked.Increment(ref Writes); return new TaskCompletionSource<object>().Task; }\n"
    "}\n"
    "public static class BuildGuardSlowOpen {\n"
    "    public static Task<Stream> After(int ms, Stream s) { return Task.Delay(ms).ContinueWith(t => s); }\n"
    "}\n"
    "'@\n"
)


def test_a_stderr_that_recovers_after_a_full_queue_gets_every_line() -> None:
    # r6b adversary A1: 256 lines queued behind a pending open, then the open completes with a writable stream. The next line
    # (here the missing-shim fail-open warning) flushes the queue and is written itself; nothing is dropped.
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                "$tcs = [System.Threading.Tasks.TaskCompletionSource[System.IO.Stream]]::new()\n"
                "$script:BuildGuardStderr.Open = $tcs.Task\n"
                "for ($i = 0; $i -lt 256; $i++) { Write-BuildGuardStderr ('queued-{0}' -f $i) }\n"
                "$ms = [System.IO.MemoryStream]::new()\n"
                "[void]$tcs.TrySetResult($ms)\n"
                "$lease = Enter-HeavySlot -Options @{ HeavySlotCmd = 'C:\\no-such-heavy-slot-for-review\\heavy-slot.cmd' } -Label t\n"
                "Write-Output (\"lease-null={0} dropped={1}\" -f ($null -eq $lease), $script:BuildGuardStderr.Dropped)\n"
                "Write-Output ([Text.Encoding]::ASCII.GetString($ms.ToArray()))\n"])
    out = done.stdout + done.stderr
    assert "lease-null=True dropped=0" in done.stdout, out
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith(("queued-", "[build-guard]"))]
    assert lines[:256] == [f"queued-{i}" for i in range(256)], out
    assert lines[256].startswith("[build-guard] HEAVY_SLOT_WARN label=t shim=missing"), out


def test_one_stderr_line_waits_at_most_the_bound_for_open_and_write_together() -> None:
    # r6b adversary A2: the first line, the open completes after 1500 ms and the write never does. The line waits at most
    # BuildGuardLogWaitMs (2000 ms) in total, not 1500 + 2000; the next line, behind the stuck write, does not wait at all.
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n" + _STDERR_SEAMS +
                "$stuck = [BuildGuardStuckStream]::new()\n"
                "function New-BuildGuardStderrOpen { return [BuildGuardSlowOpen]::After(1500, $stuck) }\n"
                "$sw = [System.Diagnostics.Stopwatch]::StartNew()\n"
                "Write-BuildGuardLog 'first'\n"
                "$first = $sw.ElapsedMilliseconds\n"
                "$sw.Restart(); Write-BuildGuardLog 'second'; $second = $sw.ElapsedMilliseconds\n"
                "Write-Output (\"first={0} second={1} writes={2}\" -f $first, $second, $stuck.Writes)\n"])
    out = done.stdout + done.stderr
    m = re.search(r"first=(\d+) second=(\d+) writes=(\d+)", done.stdout)
    assert m, out
    first, second, writes = (int(g) for g in m.groups())
    assert writes >= 1, f"the slow open was never used: {out}"
    assert 1400 <= first <= 2600, f"one line waited {first} ms for the open and the write together (bound 2000 ms): {out}"
    assert second < 500, out


@pytest.mark.parametrize("ending", ["return", "exit 7"])
def test_a_line_queued_behind_a_late_open_is_written_when_the_guard_ends(box, ending) -> None:
    # r7b (adversary 8d0c73bd-A1): the shim is missing, so the fail-open HEAVY_SLOT_WARN is the first line; the stderr open
    # completes only during the action, after the admitted line, and the action logs nothing. Every queued line still reaches
    # stderr when the guard ends, by return or by exit. The stream is an unbuffered file, so a line written before an exit
    # is on disk afterwards.
    out_file = box["dir"] / "stderr.bin"
    memory = _write_json(box["dir"] / "memory.json", OK_MEMORY)
    table = _write_json(box["dir"] / "table.json", [])
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                "$tcs = [System.Threading.Tasks.TaskCompletionSource[System.IO.Stream]]::new()\n"
                "function New-BuildGuardStderrOpen { return $tcs.Task }\n"
                "$script:BuildGuardLogWaitMs = 300\n"
                f"$fs = [System.IO.FileStream]::new('{out_file}', 'Create', 'Write', 'Read', 1)\n"
                f"$o = @{{ HeavySlotCmd = '{NO_HEAVY_SLOT}'; MemoryProbeFile = '{memory}'; ProcessTableFile = '{table}'; "
                f"LockPath = '{box['lock']}'; MutexName = 'Local\\MLV-R7B-A1-{os.getpid()}'; ExemptLogPath = '{box['exempt_log']}' }}\n"
                "Invoke-BuildGuard -Label t -Options $o -Action { param($Jobs) [void]$tcs.TrySetResult($fs); "
                + ("" if ending == "return" else ending) + " }\n"
                "Write-Output (\"queued={0} dropped={1}\" -f $script:BuildGuardStderr.Bytes.Count, $script:BuildGuardStderr.Dropped)\n"])
    out = done.stdout + done.stderr
    written = out_file.read_bytes().decode("ascii", "replace") if out_file.exists() else ""
    if ending == "return":
        assert done.returncode == 0, out
        assert "queued=0 dropped=0" in done.stdout, out
    else:
        assert done.returncode == 7, out
    warn = written.find("[build-guard] HEAVY_SLOT_WARN label=t shim=missing")
    admitted = written.find("[build-guard] admitted label=t ")
    assert warn >= 0 and admitted > warn, f"queued guard lines were never written: stderr={written!r} {out}"


def test_a_stderr_that_cannot_be_opened_or_written_counts_every_line_it_drops() -> None:
    # r7b (fable r6b note on :189-195): an open that faults takes no line, and a write that fails loses its line. Each one
    # is counted in the stderr channel's Dropped, never left queued until the cap and lost silently.
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n"
                "$tcs = [System.Threading.Tasks.TaskCompletionSource[System.IO.Stream]]::new()\n"
                "[void]$tcs.TrySetException([System.IO.IOException]::new('no stderr'))\n"
                "function New-BuildGuardStderrOpen { return $tcs.Task }\n"
                "Write-BuildGuardLog 'one'; Write-BuildGuardLog 'two'; Write-BuildGuardLog 'three'\n"
                "Complete-BuildGuardStderr\n"
                "Write-Output (\"faulted-open queued={0} dropped={1}\" -f $script:BuildGuardStderr.Bytes.Count, $script:BuildGuardStderr.Dropped)\n"
                "$ms = [System.IO.MemoryStream]::new(); $ms.Dispose()\n"
                "$script:BuildGuardStderr.Open = [System.Threading.Tasks.Task]::FromResult([System.IO.Stream]$ms)\n"
                "$script:BuildGuardStderr.Dropped = 0\n"
                "Write-BuildGuardLog 'four'; Write-BuildGuardLog 'five'\n"
                "Complete-BuildGuardStderr\n"
                "Write-Output (\"failed-write pending={0} dropped={1}\" -f $script:BuildGuardStderr.Pending.Count, $script:BuildGuardStderr.Dropped)\n"])
    out = done.stdout + done.stderr
    assert "faulted-open queued=0 dropped=3" in done.stdout, out
    assert "failed-write pending=0 dropped=2" in done.stdout, out


def test_a_build_slot_release_never_throws_past_the_heavy_slot_stop(tmp_path) -> None:
    # r6b adversary A1 (pass 2): the build-slot mutex name is taken by another kernel object, so opening it throws. The release
    # stays inside its catch (a Library host with ErrorActionPreference=Stop sees no throw), and the before-action loss path
    # sends the heavy-slot stop in a finally, whatever the release does.
    name = f"Local\\MLV-R6B-A1-{os.getpid()}"
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n$ErrorActionPreference = 'Stop'\n"
                f"$ev = [System.Threading.EventWaitHandle]::new($false, 'AutoReset', '{name}')\n"
                f"try {{ Remove-BuildSlot @{{ MutexName = '{name}'; Path = '{tmp_path / 'lock.json'}'; OwnerPid = $PID; StartTicks = 1 }}; "
                "Write-Output 'release=returned' } catch { Write-Output (\"release=threw {0}\" -f $_.Exception.GetType().Name) }\n"])
    assert "release=returned" in done.stdout, done.stdout + done.stderr
    text = GUARD.read_text(encoding="ascii")
    assert "try { Remove-BuildSlot $ticket } finally { $ticket = $null; Exit-HeavySlotHolderGone $lost 'before-action' }" in text


def test_a_closed_stdout_never_turns_a_lost_slot_release_into_a_throw(tmp_path) -> None:
    # r6b adversary A2 (pass 2): the slot ended during the action and Console.Out is a writer the host has closed. The
    # HEAVY_SLOT_LOST_DURING_ACTION line is warning-only: Exit-HeavySlot returns instead of throwing.
    stop = tmp_path / "stop"
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n$ErrorActionPreference = 'Stop'\n"
                "$p = [System.Diagnostics.Process]::Start('cmd.exe', '/c exit 0'); $p.WaitForExit()\n"
                f"$lease = @{{ Label = 't'; BuildLabel = 't'; Process = $p; Holder = $null; Ready = '{tmp_path / 'ready'}'; "
                f"Stop = '{stop}'; ExitFile = '{tmp_path / 'exit.json'}'; Log = '{tmp_path / 'log.txt'}'; "
                "Lines = [System.Collections.Generic.List[string]]::new() }\n"
                "$keep = [Console]::Out\n"
                "$w = [System.IO.StreamWriter]::new([System.IO.MemoryStream]::new()); $w.Dispose(); [Console]::SetOut($w)\n"
                "try { Exit-HeavySlot $lease; $r = 'release=returned' } catch { $r = \"release=threw $($_.Exception.Message)\" }\n"
                "[Console]::SetOut($keep)\nWrite-Output $r\n"])
    assert "release=returned" in done.stdout, done.stdout + done.stderr
    assert "HEAVY_SLOT_LOST_DURING_ACTION label=t" in done.stderr, done.stderr
    assert "Cannot write to a closed TextWriter" not in done.stdout, done.stdout


def test_the_hung_tool_clock_starts_before_any_diagnostic_line() -> None:
    # r6 B1: the clock starts before the launch, and no guard stderr line bypasses the bounded writer while a slot can be held.
    text = GUARD.read_text(encoding="ascii")
    body = text[text.index("function Enter-HeavySlot"):text.index("function Exit-HeavySlot(")]
    clock = body.index("$waitClock = [System.Diagnostics.Stopwatch]::StartNew()")
    assert clock < body.index("[System.Diagnostics.Process]::Start($psi)") < body.index("heavy-slot: waiting label=")
    # r8 (3e68ca85-A2): the two usage errors are bounded too, and no typed line is written to stdout unbounded.
    assert text.count("[Console]::Error.WriteLine(") == 0, "a guard stderr line must go through Write-BuildGuardStderr"
    assert text.count("[Console]::Out.WriteLine(") == 0, "a typed line must go through Write-BuildGuardTyped"
    assert len(re.findall(r"Write-Output \('BUILD_|Write-Output \(\"BUILD_", text)) == 0, "a typed line must go through Write-BuildGuardTyped"


# r7 (a34e2098-A1, sol + fable r6b; folds BUILD-GUARD-NONTERMINATING-ERROR-STDERR-1): a non-terminating error the guard's own
# cmdlets write while a slot is held is rendered by the host, synchronously, to stderr, past the bounded writer. Command mode
# runs Invoke-BuildGuard under the default Continue, so a full undrained stderr blocked the release before the heavy-slot stop.
def _hold_lock_fragment(box: dict) -> str:
    # The action leaves build-slot.lock open with FileShare.Read (no Delete share), so the release cannot delete it.
    return f"$global:BuildGuardR7Hold = [IO.File]::Open('{box['lock']}', 'Open', 'Read', 'Read'); exit 0"


def test_a_build_slot_that_cannot_be_deleted_never_keeps_the_heavy_slot_behind_an_undrained_stderr(box) -> None:
    pipe = _full_pipe()
    guard = _heavy_popen(box, ["-Command", _hold_lock_fragment(box)], stderr=pipe[1])
    try:
        released = _released_within(box, _RELEASE_BOUND_S)
        try:
            code = guard.wait(timeout=30)
        except subprocess.TimeoutExpired:
            code = None
    finally:
        _release_pipe(pipe, guard)
    assert released, f"the heavy slot was still held {_RELEASE_BOUND_S} s after the action: the release blocked on stderr"
    assert code == 0, "the guard stayed blocked on a rendered error after the release"


def test_a_build_slot_that_cannot_be_deleted_is_reported_through_the_guard_log(box) -> None:
    # Control with a drained stderr: the failed delete is one [build-guard] line, never a host-rendered error record.
    done = _heavy(box, "run", ["-Command", _hold_lock_fragment(box)])
    assert done.returncode == 0, done.stdout + done.stderr
    assert "[build-guard] build slot not released" in done.stderr, done.stderr
    assert "Remove-Item" not in done.stderr, done.stderr
    assert "heavy-slot: released label=" in done.stderr, done.stderr


def test_the_slot_functions_write_no_error_record_under_continue(box) -> None:
    # The class (sol r6b sites :258 :277 :338 :350 :387 :392): every cmdlet the guard runs while a slot can be held either
    # stops and is caught, or is silenced with its outcome checked and logged. Under the default Continue, none of them
    # writes an error record to the PowerShell error stream (which the host renders to stderr, unbounded).
    lock = box["lock"]
    lock.write_text('{"ownerPid":1,"startTicks":1,"expiresTicks":1,"label":"stale"}', encoding="ascii")
    missing = box["dir"] / "no-such-probe.json"
    empty = box["dir"] / "empty.json"
    empty.write_bytes(b"")
    name =f"Local\\MLV-R7-{os.getpid()}"
    taken = f"Local\\MLV-R7-taken-{os.getpid()}"
    done = _ps(["-Command",
                f". '{GUARD}' -Library\n$ErrorActionPreference = 'Continue'\n"
                f"$o = @{{ LockPath = '{lock}'; MutexName = '{name}'; LockExpiryMinutes = 90; "
                f"MemoryProbeFile = '{missing}'; ProcessTableFile = '{missing}' }}\n"
                f"$h = [IO.File]::Open('{lock}', 'Open', 'Read', 'Read')\n"
                "function Show($what, $out) {\n"
                "    $errs = @($out | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })\n"
                "    $val = @($out | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })\n"
                "    Write-Output (\"{0} errors={1} value={2}\" -f $what, $errs.Count, (($val | ForEach-Object {\n"
                "        if ($_ -is [hashtable]) { ($_.Keys | Sort-Object | ForEach-Object { \"$_=$($val[0][$_])\" }) -join ';' } else { [string]$_ } }) -join '|'))\n"
                "}\n"
                "Show 'stale-held' (Invoke-BuildSlotAcquire -Options $o -Label t 2>&1)\n"
                "Show 'release-held' (Remove-BuildSlot @{ MutexName = '" + name + "'; Path = '" + str(lock) + "'; OwnerPid = 1; StartTicks = 1 } 2>&1)\n"
                "$h.Dispose()\n"
                "Show 'memory' (Get-BuildGuardMemory -Options $o 2>&1)\n"
                "Show 'jobs' (Get-BuildGuardJobs -Options $o -SelfPid $PID 2>&1)\n"
                f"$o.MemoryProbeFile = '{empty}'; $o.ProcessTableFile = '{empty}'\n"
                "Show 'memory-empty' (Get-BuildGuardMemory -Options $o 2>&1)\n"
                "Show 'jobs-empty' (Get-BuildGuardJobs -Options $o -SelfPid $PID 2>&1)\n"
                f"$ev =[System.Threading.EventWaitHandle]::new($false, 'AutoReset', '{taken}')\n"
                f"$o.MutexName = '{taken}'\n"
                "try { Show 'mutex-taken' (Invoke-BuildSlotAcquire -Options $o -Label t 2>&1) } catch { Write-Output \"mutex-taken threw\" }\n"])
    out = done.stdout + done.stderr
    for what in ("stale-held", "release-held", "memory", "jobs", "memory-empty", "jobs-empty"):
        assert re.search(rf"^{what} errors=0 ", done.stdout, re.M), out
    assert re.search(r"^memory-empty errors=0 value=CommitGB=0;PhysGB=0", done.stdout, re.M), out
    assert re.search(r"^jobs-empty errors=0 value=.*Jobs=4", done.stdout, re.M), out
    assert re.search(r"^stale-held errors=0 value=Acquired=False;Held=stale-lock-not-removed", done.stdout, re.M), out
    assert re.search(r"^memory errors=0 value=CommitGB=0;PhysGB=0", done.stdout, re.M), out
    assert re.search(r"^jobs errors=0 value=.*Jobs=4", done.stdout, re.M), out
    # A mutex name taken by another kernel object refuses admission: it throws past the acquire (the caller's finally
    # releases first), never an error record rendered while the slot is held.
    assert "mutex-taken threw" in done.stdout, out
    assert lock.exists(), "a lock that could not be deleted is left for reclaim"
    for what in ("stale build slot not removed", "build slot not released", "memory probe unreadable", "process table unreadable"):
        assert f"[build-guard] {what}" in done.stderr, out


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
