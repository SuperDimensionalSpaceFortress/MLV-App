"""BUILD-GUARD-1: tools/coordination/build-guard.ps1 and the pin that local launchers route through it.

On 2026-10-12 four MinGW builds overlapped on the hub VM and three died with cc1plus out-of-memory;
every serial retry at -j2/-j3 passed. The guard is exercised through its injected probes (a memory
sample file and a process-table file), so no real build, no real memory pressure and no compiler is
needed. The last class is the hygiene pin: CLAUDE.md and the routed launchers must not default to -j8.
"""

import hashlib
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
           lock: Path = None) -> subprocess.CompletedProcess:
    memory_file = _write_json(box["dir"] / "memory.json", OK_MEMORY)
    table_file = _write_json(box["dir"] / "table.json", [])
    env = _env(heavy_slot=shim, extra={
        "STUB_HEAVY_SLOT_MODE": mode,
        "STUB_HEAVY_SLOT_LOG": str(box["dir"] / "stub.log"),
        "MLV_LANE_RUN_DIR": str(box["dir"] / "lane-CARD-HS-1-r2-20261027T1420Z"),
        **(extra or {}),
    })
    return subprocess.run(
        [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(GUARD),
         "-MemoryProbeFile", memory_file, "-ProcessTableFile", table_file, "-LockPath", str(lock or box["lock"]),
         "-ExemptLogPath", str(box["exempt_log"]), "-HeavySlotSha256", sha or _stub_sha(), *mode_args],
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
        "$opts.HeavySlotWaitMinutes = -15\n"
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


def test_the_guard_pins_the_installed_heavy_slot_sha() -> None:
    text = GUARD.read_text(encoding="ascii")
    assert text.count(f"'{PINNED_HEAVY_SLOT_SHA}'") == 2, "the param default and the library default"
    assert "'--', $exe" in text and "heavy-slot.cmd" in text, "the shim with a quoted '--', never a bare -- to '&'"


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
