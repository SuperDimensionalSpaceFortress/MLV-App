"""AGENT-HEAVY-SLOT-WAIT-RELEASE-1 r1c: pins on the deployed Bachelor agent's heavy-slot gate
(tools/profiling/bachelor/ultra-magnus-agent.ps1).

The design (owner-relayed from the Bachelor heat-coordination session, which owns
~/.claude/ops/heavy-slot.py on Bachelor):
  1. SUSTAINED heat: blocked only when > 6 of the last 60 feed rows are throttled or their mean
     is >= 88 C (heavy-slot.py's first-run rule). Turbo spikes with 0 throttled rows never block.
  2. FAIRNESS, no wait cap: each gated job joins heavy-slot.py's own FIFO through
     `heavy-slot.py run --exclusive --label "mlv <jobId>"` ("mlv-long <jobId>" for a job over 900 s or named
     stage / build / soak, so heavy-slot.py 2.3.1's 15 min short-exclusive self-stop cannot trip on it);
     heavy-slot.py files and reclaims the tickets and the slots. The agent never writes a slot or a ticket itself.
  3. A WAITING job holds no slot, and a heavy job starts only when no other slot is held.

The behaviour is proven end to end by tools/testing/test-ultra-magnus-agent-heavy-slot-fifo.ps1
(real agent + real heavy-slot.py); these text pins keep the shape from regressing, and the
Windows-only test below runs that script's pure-function cases (-UnitOnly, no heavy-slot.py needed).
"""

from __future__ import annotations

import base64
import contextlib
import gc
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "tools" / "profiling" / "bachelor" / "ultra-magnus-agent.ps1"
FIFO_TEST = ROOT / "tools" / "testing" / "test-ultra-magnus-agent-heavy-slot-fifo.ps1"
PWSH = shutil.which("pwsh")
# Hang guard for the -UnitOnly run: it must stay above the sum of the fifo script's own per-suite bounds (600 s + 120 s), their two
# bounded taskkills (2 x 60 s), their captured-output reads and capture cleanup (2 x (2 x 10 s + 10 s), sol r6 B1/B2) and its ~30 s of
# unit cases (930 s), so a hung suite fails by its own name there first (a test pins the sum, derived from the script's own constants).
# Measured 349 s locally, 282 s on the hosted runner. QUIET-LEASE-FILE-BOUND-HEADROOM-1: the quiet-lease bound went 400 -> 600 s
# (the guard 800 -> 1000 s, a 70 s margin over the 930 s worst case as before) after the file ran 401 s and 414 s on #781's heads.
UNIT_RUN_TIMEOUT_SEC = 1000
FIFO_UNIT_CASES_SEC = 30  # the fifo script's own unit cases, before its two bounded sub-suites
# run_bounded's cleanup after a timeout (_kill_tree). One source of truth: the helper waits with exactly these bounds and the
# regressions derive their elapsed ceiling from them, so any cleanup the helper allows passes and no other number is hand-tuned.
TASKKILL_BOUND_SEC = 60  # taskkill /F /T gets this long to exit
TASKKILL_REAP_SEC = 5  # a taskkill that outlived its bound is killed and gets this long to be reaped
ROOT_REAP_SEC = 30  # the root process gets this long to be reaped after its kill
CLEANUP_BOUND_SEC = TASKKILL_BOUND_SEC + TASKKILL_REAP_SEC + ROOT_REAP_SEC
CLEANUP_MARGIN_SEC = 10  # scheduling slack on a loaded runner, on top of the bounds
# The largest timeout run_bounded accepts (sol r5 B1). Callers pass 720 s at most; this leaves a wide margin and stays far below
# threading.TIMEOUT_MAX, above which the Windows wait raises OverflowError after the child is already launched.
MAX_RUN_BOUNDED_TIMEOUT_SEC = 3600
# Everything run_bounded does AFTER the child is launched is bounded, so the case-named verdict is never held back (sol r5-r6, adversary
# r6 A2). Waits: the run bound, then CLEANUP_BOUND_SEC for the tree kill. I/O on the capture directory: two reads, each under
# READ_BOUND_SEC, and the final removal under CAPTURE_CLEANUP_BOUND_SEC. A step that overruns is ABANDONED on a daemon thread and named in
# the verdict (never awaited). max_run_bounded_sec() is the one place that adds the allowances up, so adding one cannot leave a ceiling behind.
READ_BOUND_SEC = 10
MAX_READ_SEC = 2 * READ_BOUND_SEC
CAPTURE_CLEANUP_BOUND_SEC = 10
CAPTURE_CLOSE_BOUND_SEC = 10  # closing the two capture files (adversary r8 A1): a close that stalls or fails is named, never awaited


# sol r14 B3: a regression that measures a whole Read-CapturedResult / Remove-CaptureFiles call (a -Start seam starts a worker pwsh BEFORE the
# step's clock, and tears it down after the verdict) must allow what the implementation itself allows. The ceilings are derived from the
# script's own constants (read from its source, so a change there moves them), never typed as a number of seconds.
def _fifo_constant_ms(name: str) -> int:
    match = re.search(r"(?m)^\$" + name + r" = (\d+)\s*$", FIFO_TEST.read_text(encoding="ascii"))
    if match is None:
        raise AssertionError(f"{FIFO_TEST.name} no longer defines ${name} as a plain millisecond number")
    return int(match.group(1))


CAPTURE_WORKER_START_MS = _fifo_constant_ms("CaptureWorkerStartMs")  # a worker may take this long to be ready, before the step's clock
CAPTURE_WORKER_KILL_MS = _fifo_constant_ms("CaptureWorkerKillMs")  # teardown: each of the job, group and tree kills
CAPTURE_WORKER_REAP_MS = _fifo_constant_ms("CaptureWorkerReapMs")  # teardown: the reap, and the scratch directory removal
CAPTURE_CEILING_SLACK_MS = CLEANUP_MARGIN_SEC * 1000  # scheduling slack on a loaded runner (the same named margin the cleanup ceilings use)


def capture_call_ceiling_ms(bound_ms: int) -> int:
    """The longest a whole Read-CapturedResult / Remove-CaptureFiles call may take: worker start, the operation budget, the teardown, slack."""
    return CAPTURE_WORKER_START_MS + bound_ms + 3 * CAPTURE_WORKER_KILL_MS + 2 * CAPTURE_WORKER_REAP_MS + CAPTURE_CEILING_SLACK_MS


def max_wait_and_kill_sec(timeout: float) -> float:
    """The longest the wait plus the tree-kill cleanup of run_bounded(timeout=...) may take (what a virtual clock counts)."""
    return timeout + CLEANUP_BOUND_SEC


def max_run_bounded_sec(timeout: float) -> float:
    """The longest run_bounded(timeout=...) may take before it must have raised: every allowance the helper makes, added up once."""
    return max_wait_and_kill_sec(timeout) + CAPTURE_CLOSE_BOUND_SEC + MAX_READ_SEC + CAPTURE_CLEANUP_BOUND_SEC


# sol r14 B1 / adversary r14 A1 / adversary r15 A1, A2 / hub ruling on r15 A1 (the fifth pass on this class): no cleanup-critical path runs
# code that a caller's exception controls, and none renders an exception VALUE. repr(), str(), !r and f-strings call the exception's own
# methods and the fields they read; a validator that checks the object and then renders it still loses to a concurrent mutation or a field
# replacement between the check and the render (a validator and a snapshot were both tried and both lost). So the cleanup-critical paths
# name an exception by its TYPE only, read from the type's descriptor, plus fixed text. Nothing here validates, copies or formats a value.
_TYPE_DICT = type.__dict__  # descriptors read directly: a metaclass cannot reroute __name__ through its own code


def _exc_type_name(exc: object) -> str:
    """The exception's type name, read from the type's descriptor so that a hostile metaclass cannot run code or raise out of it.

    This is the only thing a cleanup-critical path says about an exception: never its message, arguments, errno or any other field."""
    try:
        name = _TYPE_DICT["__name__"].__get__(type(exc))
        return name if type(name) is str else "<exception>"
    except Exception:  # noqa: BLE001 - a diagnostic never replaces a verdict
        return "<exception>"


_PARKED_CAPTURES: list = []  # capture handles whose close could not be started; kept alive so no finalizer closes them on the caller's thread


def _close_captures(files: list) -> str:
    """Close the capture files under CAPTURE_CLOSE_BOUND_SEC (adversary r8 A1); return '' or a note naming what went wrong.

    A `with open(...)` closed them synchronously: an OSError from the close replaced the case-named verdict and a stalled close held it
    back with no bound. The close runs on a daemon thread that is abandoned if it overruns; a failed close is recorded, never raised.
    """
    problems: list[str] = []

    def close() -> None:
        for handle in files:
            try:
                handle.close()
            except Exception as exc:  # noqa: BLE001 - recorded in the note below
                problems.append(_exc_type_name(exc))

    try:
        closer = threading.Thread(target=close, name="close-capture", daemon=True)
        closer.start()
    except Exception as exc:  # noqa: BLE001 - a worker that cannot start must not replace the case-named verdict
        # adversary r8 pass-12 A1: dropping the handles here would let the garbage collector close them synchronously on the caller's
        # thread (traceback-frame clearing), unbounded. They are parked for the life of the process instead.
        _PARKED_CAPTURES.extend(files)
        return f"capture close not started ({_exc_type_name(exc)})"
    closer.join(CAPTURE_CLOSE_BOUND_SEC)
    if closer.is_alive():
        return f"capture close abandoned after {CAPTURE_CLOSE_BOUND_SEC:g} s"
    return f"capture close failed: {'; '.join(problems)}" if problems else ""


def _remove_capture_dir(path: str) -> str:
    """Remove the capture directory under CAPTURE_CLEANUP_BOUND_SEC (sol r6 B2); return '' or a note naming the abandoned step.

    The removal runs in a daemon thread that is abandoned if it does not finish, so a stalled temp store (a hung share, a slow
    enumeration, a held handle) cannot hold the verdict back. The note names the step and the path left behind.
    """
    left: dict[str, object] = {}

    def remove() -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
            # ignore_errors hides a held, read-only or access-denied file, so the path is probed afterwards (adversary r7 A3). os.path.exists
            # is not that probe: it answers False on a PermissionError too, so a denied delete plus a denied query read as a clean removal
            # (sol r8 B2). Only FileNotFoundError proves the path is gone; any other OSError leaves the removal unconfirmed.
            try:
                os.lstat(path)
                left["present"] = True
            except FileNotFoundError:
                left["present"] = False
            except (OSError, ValueError) as exc:
                left["unconfirmed"] = _exc_type_name(exc)
        except Exception as exc:  # noqa: BLE001 - adversary r8 A2: a worker that dies must not read as a finished removal
            left["error"] = _exc_type_name(exc)

    try:
        remover = threading.Thread(target=remove, name="rmtree-capture", daemon=True)
        remover.start()
    except Exception as exc:  # noqa: BLE001 - adversary r7 A1: a worker that cannot start must not replace the case-named verdict
        return f"capture cleanup not started ({_exc_type_name(exc)}): {path}"
    remover.join(CAPTURE_CLEANUP_BOUND_SEC)
    if remover.is_alive():
        return f"capture cleanup abandoned after {CAPTURE_CLEANUP_BOUND_SEC:g} s: {path}"
    if "error" in left:
        return f"capture cleanup failed ({left['error']}): {path}"
    if "unconfirmed" in left:
        return f"capture cleanup incomplete: removal of {path} could not be confirmed ({left['unconfirmed']})"
    if left.get("present", True):  # no recorded answer means the worker ended without one: not a success
        return f"capture cleanup incomplete: {path} was not fully removed"
    return ""


def _read_captured(path: Path, *, tolerate_oserror: bool) -> str:
    """Read a captured stream under READ_BOUND_SEC (adversary r6 A2): a stalled temp store must not hold the verdict back.

    The read runs in a daemon thread that is abandoned if it does not finish. After a failed wait the case-named verdict is the
    failure, so a read error or a stalled read is recorded in the text; otherwise a read error surfaces as itself and a stalled read
    as a TimeoutError.
    """
    box: dict[str, object] = {}

    def read() -> None:
        try:
            box["text"] = path.read_text(encoding="utf-8", errors="replace")
        except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread below
            box["error"] = exc

    try:
        reader = threading.Thread(target=read, name=f"read-{path.name}", daemon=True)
        reader.start()
    except Exception as exc:  # noqa: BLE001 - adversary r7 A1: "can't start new thread" must not replace the case-named verdict
        note = f"<read failed: could not start the reader thread ({_exc_type_name(exc)})>"
        if tolerate_oserror:
            return note
        raise
    reader.join(READ_BOUND_SEC)
    if reader.is_alive():
        note = f"<read timed out: {path.name} not readable within {READ_BOUND_SEC:g} s>"
        if tolerate_oserror:
            return note
        raise TimeoutError(note)
    error = box.get("error")
    if error is not None:
        # adversary r7 A2: after a failed wait ANY Exception from the read (MemoryError on a huge stream, ValueError, RuntimeError) is recorded
        # in the text; only a BaseException that is not an Exception (an interrupt) still propagates.
        if tolerate_oserror and issubclass(type(error), Exception):  # not isinstance(): a caller's __class__ property would run
            return f"<read failed: {_exc_type_name(error)}>"
        raise error  # type: ignore[misc]
    return str(box["text"])


def _kill_taskkill_child(taskkill: subprocess.Popen, problems: list[str]) -> None:
    """A taskkill that did not exit in time is killed and reaped under its own bound; a hung reap is reported, never waited on."""
    try:
        taskkill.kill()
    except Exception as exc:
        problems.append(f"taskkill kill failed: {_exc_type_name(exc)}")
    try:
        taskkill.wait(timeout=TASKKILL_REAP_SEC)
    except subprocess.TimeoutExpired:
        problems.append(f"taskkill was still running {TASKKILL_REAP_SEC} s after its kill")
    except Exception as exc:
        problems.append(f"waiting for taskkill failed: {_exc_type_name(exc)}")


def _kill_tree(proc: subprocess.Popen) -> str:
    """Kill proc's whole tree and say how it went; a failing cleanup step is reported, never allowed to replace the timeout verdict.

    Every wait below carries a timeout (subprocess.run(timeout=) does not: on expiry CPython kills the child and then waits for it
    with no timeout), so the whole cleanup stays inside CLEANUP_BOUND_SEC.

    adversary r10 (pass 2) A1: every step records ANY Exception (MemoryError, a RuntimeError from an audit hook, ...) and carries on to the
    next step, not just OSError / SubprocessError; only an interrupt (not an Exception) propagates.
    """
    problems: list[str] = []
    try:
        taskkill = subprocess.Popen(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        problems.append(f"taskkill failed: {_exc_type_name(exc)}")
    else:
        try:
            returncode = taskkill.wait(timeout=TASKKILL_BOUND_SEC)
            if returncode != 0:
                problems.append(f"taskkill exited {returncode}")
        except subprocess.TimeoutExpired:
            problems.append(f"taskkill failed: it did not exit within {TASKKILL_BOUND_SEC} s")
            _kill_taskkill_child(taskkill, problems)
        except Exception as exc:
            problems.append(f"taskkill failed: {_exc_type_name(exc)}")
    try:
        proc.kill()
    except Exception as exc:
        problems.append(f"kill failed: {_exc_type_name(exc)}")
    try:
        proc.wait(timeout=ROOT_REAP_SEC)
    except subprocess.TimeoutExpired:
        problems.append(f"the root process was still running {ROOT_REAP_SEC} s after the kill")
    except Exception as exc:
        problems.append(f"waiting for the root process failed: {_exc_type_name(exc)}")
    return "its process tree was killed" if not problems else f"its process tree kill is NOT confirmed ({'; '.join(problems)})"


def run_bounded(argv: list[str], timeout: float, case: str) -> tuple[int, str, str]:
    """Run argv under a real wall-clock bound and return (returncode, stdout, stderr).

    subprocess.run(timeout=) is NOT a bound on Windows when the child has children: it kills only the root process and
    then waits in communicate() for the pipes to close, and any surviving grandchild (the suites here start pwsh agents,
    fake heavy-slot.py processes and sleepers) keeps them open. Hosted run 40388902288 sat 36 min past a 600 s timeout
    that way until the job cap cancelled it. So the output goes to files (nothing waits for pipe EOF) and a timeout kills
    the whole tree, then fails naming the case and the elapsed time.
    """
    # sol r5 B1: refuse an unusable bound BEFORE any child exists. inf, nan, 1e308 or a non-positive value would otherwise be accepted,
    # the child launched, and the wait would raise OverflowError / ValueError past the TimeoutExpired handler.
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_RUN_BOUNDED_TIMEOUT_SEC):
        raise ValueError(f"{case}: run_bounded timeout must be a finite number in (0, {MAX_RUN_BOUNDED_TIMEOUT_SEC}] s, "
                         f"got {timeout!r}; no child was started")
    tmp = tempfile.mkdtemp(prefix="heavy-slot-gate-")
    out_path, err_path = Path(tmp) / "stdout.txt", Path(tmp) / "stderr.txt"
    started = time.monotonic()
    failed = False
    verdict = ""
    kill_note = ""
    cleanup_note = ""
    close_note = ""
    captures: list = []
    try:
        try:
            # adversary r8 A1: not `with open(...)`: the implicit close at the end of the block was synchronous and unbounded, and an
            # OSError from it replaced the case-named verdict. _close_captures closes them under CAPTURE_CLOSE_BOUND_SEC and names a failure.
            out = open(out_path, "wb")
            captures.append(out)
            err = open(err_path, "wb")
            captures.append(err)
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                failed = True
                verdict = f"exceeded its {timeout:g} s wall-clock bound"
                kill_note = _kill_tree(proc)
            except BaseException as exc:
                # sol r5 B1: any other failure of the wait (OverflowError, ValueError, OSError, an interrupt) happens with the child
                # already running. The tree is killed first; an interrupt is then re-raised, anything else keeps the case-named verdict.
                kill_note = _kill_tree(proc)
                if not issubclass(type(exc), Exception):  # not isinstance(): a caller's __class__ property would run
                    raise
                failed = True
                verdict = f"could not be waited on ({_exc_type_name(exc)})"
        finally:
            close_note = _close_captures(captures)
        elapsed = time.monotonic() - started
        # After a failed wait the case-named verdict below is the failure, so a read error (a Windows sharing violation on a file the
        # killed tree still holds) is recorded in the text instead of replacing it. Without a failed wait the read error surfaces.
        stdout = _read_captured(out_path, tolerate_oserror=failed)
        stderr = _read_captured(err_path, tolerate_oserror=failed)
    finally:
        cleanup_note = _remove_capture_dir(tmp)
    notes = "".join(f"\n{note}" for note in (close_note, cleanup_note) if note)
    if failed:
        raise AssertionError(f"{case} {verdict} (ran {elapsed:.0f} s) and {kill_note}.\n"
                             f"stdout tail: {stdout[-1500:]!r}\nstderr tail: {stderr[-1500:]!r}" + notes)
    if close_note:
        # No failed wait, so the captured output may be incomplete: that is the failure, named by case (it used to surface as a bare OSError).
        raise AssertionError(f"{case}: {close_note}" + (f"\n{cleanup_note}" if cleanup_note else ""))
    if cleanup_note:
        warnings.warn(f"{case}: {cleanup_note}", RuntimeWarning, stacklevel=2)
    return proc.returncode, stdout, stderr


@contextlib.contextmanager
def _pwsh_script_argv(script: str):
    """A pwsh argv that runs `script` from a temporary .ps1, dot-sourced by -Command (so the -Command exit-code rules still apply).

    r13: the bounded capture region (with the worker's job object and wire transport) made an inline `-Command <script>` longer than the
    32767-character Windows command line (WinError 206)."""
    fd, path = tempfile.mkstemp(prefix="heavy-slot-gate-", suffix=".ps1")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(script)
        yield [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", ". '" + path.replace("'", "''") + "'"]
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


PWSH_SCRIPT_BASE_SEC = 120  # a bare pwsh start, the compile of the capture type and the script's own end: what a script with NO worker call needs
_CAPTURE_CALL = re.compile(r"\b(Read-CapturedResult|Read-CapturedText|Remove-CaptureFiles)\b([^\n]*)")
_LOOP_BLOCK_HEAD = re.compile(r"(?i)(?:\b(?:foreach|for|while)\s*\([^{}]*\)|\bdo|ForEach-Object)\s*$")


def _call_is_inside_a_loop(body: str, position: int) -> bool:
    """True when `position` sits inside a `foreach (...) {`, `for`, `while`, `do {` or `ForEach-Object {` block of the pwsh `body`."""
    stack: list[bool] = []
    for index in range(position):
        char = body[index]
        if char == "{":
            stack.append(bool(_LOOP_BLOCK_HEAD.search(body[max(0, index - 200):index])))
        elif char == "}" and stack:
            stack.pop()
    return any(stack)
MAX_DECLARED_BOUND_MS = 120000  # an integer on a call line above this is a pid or a count, not an operation bound


def pwsh_script_budget_sec(script: str, repeat: int = 1) -> float:
    """The whole-script wall-clock budget of a pwsh case, derived from the capture calls the script makes (r16, Dot C1 / adversary r15 A3).

    r13 started a worker pwsh for every Read-Captured* / Remove-CaptureFiles call, and each call may legitimately take
    capture_call_ceiling_ms(its bound) (the worker start bound, the operation, the teardown bounds, slack). A fixed 120 s therefore failed a
    valid script after four permitted slow starts (the transport case makes 18 calls). The budget is the base allowance plus, for every call
    site in the script body (the helpers' own definitions, before the END marker, are not calls), that call's ceiling; a call line whose bound
    is a variable is charged the worker start bound. A script that wraps calls in a loop must say how often with `repeat`, so a loop can never
    hide calls from the derivation. A genuine hang still ends at the budget: run_bounded kills the whole tree and fails by case name.
    """
    marker = "# END bounded capture io"
    body = script[script.index(marker):] if marker in script else script
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    straight_ms, looped_ms = 0, 0
    for match in _CAPTURE_CALL.finditer(body):
        if re.search(r"(?i)\bfunction\s+$", body[:match.start()]):
            continue
        bounds = [int(n) for n in re.findall(r"(?<![\w$.])\d+(?![\w.])", match.group(2)) if 0 < int(n) <= MAX_DECLARED_BOUND_MS]
        ceiling = capture_call_ceiling_ms(max(bounds) if bounds else CAPTURE_WORKER_START_MS)
        if _call_is_inside_a_loop(body, match.start()):
            looped_ms += ceiling
        else:
            straight_ms += ceiling
    if looped_ms and repeat < 2:
        raise AssertionError("this pwsh script makes capture calls inside a loop: pass repeat=<iterations> so the budget counts them")
    return PWSH_SCRIPT_BASE_SEC + (straight_ms + repeat * looped_ms) / 1000


def _case_name() -> str:
    frame = sys._getframe(1)
    while frame is not None and not frame.f_code.co_name.startswith("test_"):
        frame = frame.f_back  # type: ignore[assignment]
    return frame.f_code.co_name if frame is not None else "pwsh script"


def _run_pwsh_script(script: str, repeat: int = 1) -> subprocess.CompletedProcess:
    """Run `script` in a bare pwsh under its derived budget; a hang kills the tree and fails by case name (run_bounded)."""
    budget = pwsh_script_budget_sec(script, repeat)
    with _pwsh_script_argv(script) as argv:
        code, out, err = run_bounded(argv, budget, _case_name())
    return subprocess.CompletedProcess(argv, code, out, err)


def _fifo_capture_io_region(fifo: str) -> str:
    """The fifo script's bounded capture io helpers, between their BEGIN / END markers, ready to dot-source in a bare pwsh."""
    begin = fifo.index("# BEGIN bounded capture io")
    return fifo[begin:fifo.index("# END bounded capture io")]


# sol r12 B1: what a forced end of a pwsh process looks like in code. Neither the bounded capture helpers nor the proof of their exit may use it.
_FORCED_EXIT = re.compile(r"(?im)Environment\]?::Exit|Environment\.Exit|Environment\.FailFast|SetShouldExit|(?:^|[;{}])\s*exit\b")


def _code_lines(text: str) -> list[str]:
    """Agent lines with full-line comments dropped (pins are about code, not prose)."""
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


class BachelorAgentHeavySlotGatePins(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.raw = AGENT.read_bytes()
        cls.text = cls.raw.decode("ascii")
        cls.code = "\n".join(_code_lines(cls.text))

    def test_ascii_only(self) -> None:
        self.assertTrue(all(b < 128 for b in self.raw), "the agent must stay ASCII-only")

    def test_waiting_job_never_writes_a_slot_or_a_ticket(self) -> None:
        # r1j-r1k reserved a slot while waiting (Enter-HeavySlot / CreateDirectoryW / owner.json) and then
        # counted that reservation as busy. The agent now holds a slot ONLY through heavy-slot.py.
        for gone in ("Enter-HeavySlot", "Write-HeavySlotOwner", "CreateDirectoryW", "Invoke-SlotReleaseAttempt",
                     "PendingSlotReleases", "$script:HeavySlotHeld"):
            self.assertNotIn(gone, self.code, gone)
        self.assertIsNone(re.search(r"owner\.json", self.code), "the agent must not read or write owner.json")
        # Tickets are heavy-slot.py's: the agent never builds a queue path (it only reads `status` queue lines).
        self.assertIsNone(re.search(r"Join-Path\s+\$HeavySlotDir\s+[\"']queue", self.code))
        self.assertIsNone(re.search(r"queue\\", self.code))

    def test_heavy_job_is_held_through_heavy_slot_py_run_exclusive(self) -> None:
        # AGENT-HEAVY-SLOT-2.3-ADAPT-1: the label is "mlv <jobId>" or "mlv-long <jobId>" (Get-HeavySlotLabel).
        # AGENT-HEAVY-SLOT-LABEL-SINGLE-READ-1: the wait resolves it once and hands it to the holder start.
        self.assertIn("$label = Get-HeavySlotLabel -JobId $JobId -TimeoutSec ([int]$Metadata.timeoutSec)", self.code)
        self.assertIn("$h = Start-HeavySlotHolder -JobId $JobId -Label $label", self.code)
        self.assertRegex(self.code, r'\$HeavySlotScript,\s*"run",\s*"--exclusive",\s*"--label",\s*\$Label,\s*"--slots"')
        # A holder must take its own slot, never pass through as nested under a stale HEAVY_SLOT_HELD.
        self.assertIn("HEAVY_SLOT_HELD = $null", self.code)
        # The job runs inside the holder's slot and inherits it (heavy-slot.py NESTING contract).
        self.assertIn("$env:HEAVY_SLOT_HELD = [string]$script:Admission.heavySlotHeld", self.code)
        self.assertIn("Env:\\HEAVY_SLOT_HELD", self.code)

    def test_no_wait_cap_and_no_admitted_after_cap(self) -> None:
        self.assertNotIn("GateHotWaitCapSec", self.text)
        # loaded-comparable survives ONLY as the downgrade of a run whose agent restarted mid-job.
        self.assertEqual(self.code.count('"loaded-comparable"'), 1)
        self.assertIn('if ($Admission.label -eq "clear") { $o.label = "loaded-comparable" }', self.code)

    def test_sustained_heat_rule_is_heavy_slot_py_first_run_rule(self) -> None:
        self.assertRegex(self.code, r"\[int\]\$GateSustainedRows = 60\b")
        self.assertRegex(self.code, r"\[int\]\$GateMaxThrottledRows = 6\b")
        self.assertRegex(self.code, r"\[double\]\$GateMeanMaxC = 88\b")
        self.assertIn("$Metrics.throttled -gt $GateMaxThrottledRows -or $Metrics.meanC -ge $GateMeanMaxC", self.code)
        for gone in ("GateMaxTempC", "GateJoinWatts", "GateJoinClearSec"):
            self.assertNotIn(gone, self.code, gone)

    def test_admission_requires_busy_zero_with_own_slot_excluded(self) -> None:
        self.assertIn("-OwnSlot $hs.slot", self.code)
        self.assertIn('elseif ($occ.busy -gt 0) { $v.state = "busy"', self.code)
        self.assertIn('if ($v.state -eq "clear") {', self.code)
        # Anything else with the slot in hand releases it before re-queueing.
        self.assertRegex(self.code, r'\$why = "acquired slot .*released, re-joining the FIFO"\s*\n\s*Stop-HeavySlotHolder')

    def test_own_slot_is_excluded_only_for_the_verified_live_holder(self) -> None:
        # r2 (sol r1 BLOCKER): r1c excluded the slot NUMBER without checking its owner, so a holder that exited
        # during the status probe let another suite's slot be excluded and a heavy job was admitted beside it.
        self.assertIn("-OwnSlot $hs.slot -OwnPid $hs.ownerPid -Label $label", self.code)
        # AGENT-LEASE-PENDING-HEAT-TRACE-1 (adversary r3 A2): the holder text is split by ConvertFrom-HeavySlotHolderText (read from the
        # right, so a label containing "(pid N)" still matches itself); the own slot is still excluded only when BOTH its label and its
        # owner pid are this holder's, and only when the holder's label and pid are known.
        self.assertIn("$oh = if ($Label -and $OwnPid -gt 0) { ConvertFrom-HeavySlotHolderText -Text $who } else { $null }", self.code)
        self.assertIn("if ($null -ne $oh -and $oh.label -eq $Label -and [int]$oh.ownerPid -eq $OwnPid) { $ownSeen = $true; continue }", self.code)
        self.assertIn("$o.ownLost = $true", self.code)
        self.assertIn("if ($Text -notmatch '(?s)^(.*) \\(pid (\\d+)((?:, [^,)]*)*)\\)(?: \\[[^\\]]*\\])?\\s*$') { return $null }", self.code)
        self.assertIn('elseif ($occ.ownLost) { $v.state = "holder-lost"', self.code)
        # The holder must still hold the slot AFTER the probe: checked after Invoke-HeavySlotStatus, before any admit.
        acquired = self.code[self.code.index("$status = Invoke-HeavySlotStatus -HeartbeatActivity \"gate-wait job=$JobId state=acquired"):]
        live = acquired.index("$live = Test-HeavySlotHolderHeld -Holder $holder -Acquired $hs")
        self.assertLess(live, acquired.index('if ($v.state -eq "clear") {'))
        self.assertIn('if (-not $live.ok) { $v.state = "holder-lost"; $v.why = $live.why }', acquired)

    def test_pre_launch_revalidation_precedes_the_child_launch(self) -> None:
        body = self.code[self.code.index("function Invoke-QueuedJob"):]
        body = body[:body.index("\n}\n")]
        confirm = body.index("$pre = Confirm-HeavySlotHeldForLaunch -JobId $jobId")
        launch = body.index("$launch = Start-WindowlessRedirectedProcess -FilePath $psExe")
        self.assertLess(body.index("$marker = New-LaunchingMarker"), confirm)
        self.assertLess(confirm, launch)
        refusal = body[confirm:launch]
        self.assertIn("Exit-HeavySlot", refusal)
        self.assertIn('-Phase "pre-launch"', refusal)
        self.assertRegex(refusal, r"\breturn\b")
        self.assertIn("Add-HeavySlotHolderLossToAdmission -JobId $jobId -Admission $script:Admission", body)

    def test_the_fifo_suite_bound_kills_its_child_through_a_bounded_taskkill(self) -> None:
        # sol adversary A2: `& taskkill.exe` after a WaitForExit timeout had no bound of its own, so a hung taskkill held back the
        # suite-named failure. The kill is now a Start-Process with its own WaitForExit, and the message does not claim a kill that failed.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        self.assertNotIn("& taskkill.exe", fifo)
        self.assertIn("Start-Process -FilePath 'taskkill.exe'", fifo)
        self.assertIn("$k.WaitForExit(60000)", fifo)
        self.assertIn("process tree kill is NOT confirmed", fifo)
        self.assertIn("and $killNote; output so far", fifo)

    def test_the_outer_guard_outlasts_every_inner_bound_so_a_hung_suite_fails_by_its_own_name(self) -> None:
        # adversary r4 A2: the -UnitOnly run is quiet-lease (bound + bounded taskkill) then stage-exempt (bound + bounded taskkill) after
        # about 30 s of unit cases. If the outer guard is shorter than that worst case, the outer kill reports only the parent test.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        quiet = int(re.search(r"\[int\]\$QuietLeaseBoundSec = (\d+)", fifo).group(1))
        stage = int(re.search(r"\[int\]\$StageExemptBoundSec = (\d+)", fifo).group(1))
        taskkill = int(re.search(r"\$k\.WaitForExit\((\d+)000\)", fifo).group(1))
        # sol r6 B1/B2: after the wait and the kill each suite also spends two bounded captured-output reads and one bounded cleanup.
        read_sec = int(re.search(r"^\$ReadBoundMs = (\d+)000$", fifo, re.M).group(1))
        cleanup_sec = int(re.search(r"^\$CaptureCleanupBoundMs = (\d+)000$", fifo, re.M).group(1))
        per_suite_io = 2 * read_sec + cleanup_sec
        worst_case = FIFO_UNIT_CASES_SEC + (quiet + taskkill + per_suite_io) + (stage + taskkill + per_suite_io)
        self.assertGreater(UNIT_RUN_TIMEOUT_SEC, worst_case,
                           f"the outer guard ({UNIT_RUN_TIMEOUT_SEC} s) must outlast unit cases + both sub-suite bounds + both bounded kills "
                           f"+ both suites' bounded reads and cleanup ({worst_case} s)")

    def test_the_suite_bounds_are_range_checked_before_any_child_is_launched(self) -> None:
        # adversary r4 pass 3 A1: -QuietLeaseBoundSec -1 or 2147484 bound as a legal [int], then WaitForExit threw after the child
        # was launched, skipping the kill block. The range is refused at parameter binding, before any child exists.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        for name in ("QuietLeaseBoundSec", "StageExemptBoundSec"):
            self.assertRegex(fifo, r"\[ValidateRange\(1, 3600\)\]\s*\[int\]\$" + name + r" = \d+", name)

    def test_a_completed_suite_must_hand_back_its_json_and_its_elapsed_time_is_monotonic(self) -> None:
        # adversary r4 pass 3 A2 / A3: a captured-output read error was swallowed (SilentlyContinue), so a passing suite could aggregate
        # as quietLease:null; and the timeout diagnostic subtracted local wall-clock times, which go negative across a DST fall-back.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        body = fifo[fifo.index("function Invoke-BoundedSuite"):]
        body = body[:body.index("\n}\n")]
        # sol r6 B1: the JSON is no longer re-read (Get-Content had no deadline); the one bounded read feeds the diagnostic and the result,
        # and a read that did not succeed fails the suite by name instead of aggregating as an empty result.
        self.assertNotIn("Get-Content", body)
        self.assertEqual(body.count("Read-CapturedResult $outF"), 1, "stdout is opened and read exactly once")
        self.assertIn("elseif (-not $outR.Ok) { $fail =", body)
        self.assertIn("[Diagnostics.Stopwatch]::StartNew()", body)
        self.assertNotIn("Get-Date", body)
        for name in ("lease", "stage"):
            self.assertIn(f"Assert-True ($null -ne ${name}.Json -and ${name}.Json.Trim().Length -gt 0)", fifo, name)

    def test_light_probes_stay_exempt_and_deploy_gate_string_matches(self) -> None:
        self.assertIn("[string]$GateExemptJobPattern = '^(venue-quiet-probe|agent-swap)-|^dve-[A-Za-z0-9._-]+-health$'", self.text)
        m = re.search(r'^\$script:GateVersion = "([^"]+)"', self.text, re.M)
        self.assertIsNotNone(m)
        # Deploy-AgentSwap.ps1's default -ExpectGate.
        self.assertIn("heavy-slot-gate-1", m.group(1))
        self.assertIn("wait-release r1c", m.group(1))
        self.assertIn("maint-dispatch r1", m.group(1))

    def test_staging_jobs_skip_the_lease_but_never_run_during_a_wait(self) -> None:
        # STAGE-JOB-LEASE-EXEMPT-1: a build-artifact staging step (copy + hash into cache\, measures nothing) waited 6889 s
        # for a quiet lease on 2026-10-24. Its exact generator id grammars are exempt; anything else stays gated.
        self.assertIn("[string]$GateStageJobPattern = '^(playback-attr-3-cuda-stage-[0-9a-f]{12}(-[A-Za-z0-9-]{1,32})?"
                      "|attr3-stage-smoke-runner-[0-9a-f]{12}-[0-9a-f]{12}|attr3-stage-fixture-(tiny|large)_dual_iso-[0-9a-f]{12})\\z'",
                      self.text)
        classify = self.code[self.code.index("function Get-GateExemptClass"):]
        classify = classify[:classify.index("\n}\n")]
        self.assertIn("$JobId -match $GateExemptJobPattern", classify)
        self.assertIn("$JobId -cmatch $GateStageJobPattern", classify)
        queued = self.code[self.code.index("function Invoke-QueuedJob"):]
        queued = queued[:queued.index("\n}\n")]
        self.assertIn("$exemptClass = Get-GateExemptClass -JobId $jobId", queued)
        # Serial with the agent's own measured leg: the during-wait dispatcher runs light jobs only, never a staging job.
        helper = self.code[self.code.index("function Invoke-ExemptJobsDuringWait"):]
        helper = helper[:helper.index("\n}\n")]
        self.assertNotIn("GateStageJobPattern", helper)
        self.assertNotIn("Get-GateExemptClass", helper)
        self.assertEqual(len(re.findall(r"-c?match \$GateStageJobPattern", self.code)), 1,
                         "the stage pattern is matched in one place (Get-GateExemptClass), case-sensitively")

    def test_stage_exempt_hardening_bounds_override_and_run_time(self) -> None:
        # STAGE-JOB-LEASE-EXEMPT-HARDENING-1 r1 (Dot 04eedafd on #649): the override is stated positively (staging stems only) and a
        # stage-class job has its own short ceiling. Behaviour is driven by tools/testing/test-ultra-magnus-agent-stage-exempt.ps1.
        self.assertIn("stage-exempt r2 (2026-10-25)", self.text)
        self.assertIn("[ValidateRange(1, 900)]\n    [int]$StageExemptMaxSec = 300,".replace("\n", "\r\n" if "\r\n" in self.text else "\n"), self.text)
        # The validator's copy of the grammar is the parameter default (the .ps1 test pins the same).
        default = re.search(r"\[string\]\$GateStageJobPattern = '([^']+)'", self.text).group(1)
        self.assertIn("$stageGrammar = '" + default + "'", self.text)
        for stem in ("playback-attr-3-cuda-stage-", "attr3-stage-smoke-runner-", "attr3-stage-fixture-"):
            self.assertIn("'" + stem + "'", self.text)
        # Only the stage branch clamps; the kill path types the reason; light and gated paths never read the ceiling.
        queued = self.code[self.code.index("function Invoke-QueuedJob"):]
        queued = queued[:queued.index("\n}\n")]
        stage_branch = queued[queued.index('elseif ($exemptClass -eq "stage")'):]
        stage_branch = stage_branch[:stage_branch.index("    else {")]
        self.assertIn("$effectiveTimeoutSec = $StageExemptMaxSec", stage_branch)
        self.assertEqual(queued.count("$effectiveTimeoutSec = $StageExemptMaxSec"), 1)
        self.assertIn("STAGE_EXEMPT_CEILING_APPLIED", stage_branch)
        self.assertIn("STAGE_EXEMPT_CEILING_EXCEEDED: ", queued)
        # The parameter declaration, the stage branch and (r3) the adopt-path clamp of a pre-ceiling marker are the only places the ceiling appears.
        adopt_fn = self.code[self.code.index("function Invoke-AdoptStartedJob"):]
        adopt_fn = adopt_fn[:adopt_fn.index("\n}\n")]
        self.assertEqual(self.code.count("$StageExemptMaxSec"), 1 + stage_branch.count("$StageExemptMaxSec") + adopt_fn.count("$StageExemptMaxSec"),
                         "the ceiling is read only by the stage branch and the adopt clamp")
        self.assertEqual(adopt_fn.count("$StageExemptMaxSec"), 2)
        # r2: classify = the immutable default grammar AND the effective pattern (an override only narrows), and an adopted stage child
        # killed at the ceiling is typed too. Behaviour is driven by the .ps1 test (classify-and-override, stage-ceiling-kill, stage-ceiling-adopt).
        self.assertIn("stage-exempt r3 (2026-10-25)", self.text)
        self.assertIn("$script:StageGrammarDefault = '" + default + "'", self.text)
        classify = self.code[self.code.index("function Get-GateExemptClass"):]
        classify = classify[:classify.index("\n}\n")]
        self.assertIn("($JobId -cmatch $script:StageGrammarDefault) -and ($JobId -cmatch $GateStageJobPattern)", classify)
        adopt = self.code[self.code.index("function Invoke-AdoptStartedJob"):]
        adopt = adopt[:adopt.index("\n}\n")]
        self.assertIn("STAGE_EXEMPT_CEILING_EXCEEDED: adopted staging step", adopt)
        # r3: the classifier fails closed on an empty default grammar, and an un-ceilinged stage marker is clamped at the adopt read.
        # Behaviour is driven by the .ps1 test (classify-and-override, stage-ceiling-adopt legacy-over-ceiling).
        self.assertIn("if ([string]::IsNullOrEmpty($script:StageGrammarDefault)) { return $null }", classify)
        self.assertIn('[string]$Marker.admission.exemptClass -ceq "stage" -and $adoptedTimeoutSec -gt $StageExemptMaxSec', adopt)

    def test_exempt_jobs_run_during_a_gate_wait(self) -> None:
        # GATE-MAINT-DISPATCH-DURING-WAIT-1: a job waiting on the gate has no cap, and the inbox loop is blocked
        # inside it, so the light jobs behind it (probes, dve health, agent-swap) are run from inside the wait.
        wait = self.code[self.code.index("function Wait-HeavySlotAdmission"):]
        wait = wait[:wait.index("\n}\n")]
        self.assertIn("$null = Invoke-ExemptJobsDuringWait -WaitingJobId $JobId", wait)
        helper = self.code[self.code.index("function Invoke-ExemptJobsDuringWait"):]
        helper = helper[:helper.index("\n}\n")]
        self.assertIn("$eid -eq $WaitingJobId", helper)
        self.assertIn("-notmatch $GateExemptJobPattern", helper)
        self.assertIn("$null = Invoke-QueuedJob -Job $ej", helper)
        # The waiting job's holder is the wait's local: the helper must never release a slot.
        self.assertNotIn("Exit-HeavySlot", helper)
        # The inbox loop's listing is stale after a wait: a job already run inline must not run again.
        loop = self.code[self.code.index("Invoke-QueuedJob -Job $job") - 1500:self.code.index("Invoke-QueuedJob -Job $job")]
        self.assertIn("if (-not (Test-Path -LiteralPath $job.FullName -PathType Leaf)) { continue }", loop)

    def test_heartbeat_keeps_the_um_run_gate_wait_shape(self) -> None:
        # tools/profiling/um-run.ps1 parses "gate-wait job=<id> state=<\S*> waited=<n>s" (unanchored at the end), so the
        # busy-slot holder note is a trailing field: the prefix stays byte-identical.
        self.assertIn('"gate-wait job=$JobId state=$state waited=$([int]((Get-Date) - $t0).TotalSeconds)s$holderNote"', self.code)
        # The two status probes heartbeat the same shape (they heartbeat while the status call runs).
        self.assertIn('-HeartbeatActivity "gate-wait job=$JobId state=acquired waited=$([int]$waited)s$holderNote"', self.code)
        self.assertIn('-HeartbeatActivity "gate-wait job=$JobId state=$state waited=$([int]$waited)s$holderNote"', self.code)

    def test_label_is_decided_in_one_place_and_long_jobs_are_not_short_exclusive(self) -> None:
        # AGENT-HEAVY-SLOT-2.3-ADAPT-1: heavy-slot.py 2.3.1 treats an exclusive holder labelled with the literal prefix
        # "mlv " (trailing space) as short-exclusive and writes a stop file when one runs past 15 min. A gated job over
        # 900 s, or a stage / build / soak job, is "mlv-long <id>", which that prefix does not match.
        self.assertEqual(self.code.count('"mlv $JobId"'), 1, "the short label is spelled once, inside Get-HeavySlotLabel")
        # AGENT-HEAVY-SLOT-LABEL-SINGLE-READ-1: only the wait resolves it; the holder start takes it as -Label (pinned below).
        for fn in ("function Wait-HeavySlotAdmission",):
            body = self.code[self.code.index(fn):]
            body = body[:body.index("\n}\n")]
            self.assertIn("Get-HeavySlotLabel", body, fn)
        label = self.code[self.code.index("function Get-HeavySlotLabel"):]
        label = label[:label.index("\n}\n")]
        self.assertIn('"mlv-long $JobId"', label)
        self.assertIn('"mlv $JobId"', label)
        self.assertRegex(self.text, r"\[int\]\$GateLongJobSec = 900\b")
        self.assertRegex(self.text, r"\[string\]\$GateLongJobPattern = '[^']*stage\|build\|soak[^']*'")
        # The label's timeout is the metadata the agent applies (default 1800 s), never a restated number; it is read
        # once per job, in the wait, and the holder start takes the resolved label instead of reading metadata again.
        wait = self.code[self.code.index("function Wait-HeavySlotAdmission"):]
        wait = wait[:wait.index("\n}\n")]
        self.assertEqual(wait.count("Get-HeavySlotLabel"), 1, "the wait resolves the label once")
        self.assertIn("[int]$Metadata.timeoutSec", wait)
        start = self.code[self.code.index("function Start-HeavySlotHolder"):]
        start = start[:start.index("\n}\n")]
        self.assertNotIn("Get-JobMetadata", start, "the holder start must not read the metadata a second time")
        self.assertNotIn("Get-HeavySlotLabel", start, "the holder start must not resolve a label of its own")
        self.assertRegex(start, r"\[Parameter\(Mandatory = \$true\)\]\[string\]\$Label")
        self.assertEqual(self.code.count("Start-HeavySlotHolder -JobId"), 1, "one holder-start call site, and it passes the label")
        self.assertEqual(self.code.count("Get-HeavySlotLabel -JobId"), 1, "one label resolution per job admission path")


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
@unittest.skipUnless(os.name == "nt", "runs the Windows agent's PowerShell functions")
class BachelorAgentHeavySlotGateUnit(unittest.TestCase):
    def test_pure_function_cases_pass(self) -> None:
        # A hang guard, not a speed bound. -UnitOnly also runs the quiet-lease and stage-exempt suites in their own processes;
        # AGENT-LEASE-PENDING-HEAT-TRACE-1 r3 runs real (fake) status probes there, and the whole run took 312 s on a loaded host.
        # CI-FLAKE-HEAVY-SLOT-GATE-UNIT-HOSTED-HANG-1: run_bounded, because subprocess.run(timeout=) hangs past its timeout here.
        rc, stdout, stderr = run_bounded([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(FIFO_TEST), "-UnitOnly"],
                                         UNIT_RUN_TIMEOUT_SEC, "BachelorAgentHeavySlotGateUnit.test_pure_function_cases_pass")
        self.assertEqual(rc, 0, stdout + stderr)
        out = json.loads(stdout)
        self.assertEqual(out["status"], "PASS")
        self.assertEqual(out["results"]["unit"]["thresholds"]["t7"], "hot")
        self.assertEqual(out["results"]["unit"]["verdicts"]["ownSlotExcluded"], "clear")
        # r2: sol's r1 repro, owner mismatch and the pre-launch revalidation.
        unit = out["results"]["unit"]
        self.assertEqual(unit["ownerCheck"]["foreignOwner"], "holder-lost")
        self.assertEqual(unit["holderLost"]["solRepro"], "holder-lost")
        self.assertEqual(unit["holderLost"]["label"], "queue-expired")
        self.assertEqual(unit["holderLost"]["ownerMismatch"], "holder-lost")
        self.assertEqual(unit["holderLost"]["preLaunch"]["exitDuringProbe"], "holder-lost")
        self.assertEqual(unit["holderLost"]["preLaunch"]["ownerMismatch"], "holder-lost")
        # GATE-MAINT-DISPATCH-DURING-WAIT-1: light jobs run from inside a gate-wait, and the wait still returns one record.
        self.assertEqual(unit["maintDispatch"]["dispatched"], ["agent-swap-U1", "venue-quiet-probe-U2"])
        self.assertTrue(unit["maintDispatch"]["singleObject"])
        self.assertEqual(unit["maintDispatch"]["label"], "queue-expired")
        # STAGE-JOB-LEASE-EXEMPT-1: staging jobs reach the launch with zero heavy-slot.py calls; measured ids still lease.
        stage = out["results"]["stageExempt"]
        self.assertEqual(stage["status"], "PASS")
        self.assertEqual(stage["results"]["stageUngated"]["attrStage"]["calls"], 0)
        self.assertGreaterEqual(stage["results"]["stageGated"]["stageElsewhere"]["requests"], 1)
        self.assertEqual(stage["results"]["stageNotDuringWait"]["ranDuringWait"], ["agent-swap-20261024T0519Z"])
        # AGENT-HEAVY-SLOT-2.3-ADAPT-1: long jobs are "mlv-long <id>"; the busy heartbeat names the holder.
        cases = unit["longLabel"]["cases"]
        self.assertEqual(cases["at900"], "mlv jobA")
        self.assertEqual(cases["at901"], "mlv-long jobA")
        self.assertEqual(cases["stage"], "mlv-long playback-attr-3-cuda-stage-1a2b3c")
        self.assertEqual(cases["notAToken"], "mlv upstage-1")
        self.assertEqual(unit["longLabel"]["metadata"]["noMeta"], "1800 -> mlv-long mNone")
        self.assertEqual(unit["longLabel"]["metadata"]["explicit900"], "900 -> mlv m900")
        self.assertEqual(unit["longLabel"]["waitAdmitsOnLongLabel"], "clear")
        self.assertEqual(unit["longLabel"]["waitRejectsShortLabel"], "holder-lost")
        # AGENT-HEAVY-SLOT-LABEL-SINGLE-READ-1: meta.json rewritten after the wait read it (600 -> 1200 s and 1200 -> 600 s
        # with GateLongJobSec 900) does not change the label the ticket is filed under.
        self.assertEqual(unit["singleRead"]["shortThenLong"], "mlv jslr")
        self.assertEqual(unit["singleRead"]["longThenShort"], "mlv-long jslr")
        self.assertEqual(unit["heartbeat"]["busy"], "gate-wait job=j1 state=queued waited=Ns holder=suite X held=62m")
        self.assertEqual(unit["heartbeat"]["free"], "gate-wait job=j1 state=queued waited=Ns")
        self.assertEqual(unit["heartbeat"]["umRunState"], "queued")
        self.assertLessEqual(unit["heartbeat"]["capped"], 64)


@unittest.skipUnless(os.name == "nt", "the bound is a Windows process-tree kill")
class BoundedRunTests(unittest.TestCase):
    """run_bounded fails a hung process tree by case name inside its bound (the old subprocess.run(timeout=) did not)."""

    # The root sleeps and so does a grandchild that inherits its stdout/stderr, the shape that kept the pipes open.
    HUNG_TREE = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); time.sleep(120)"

    def test_a_hung_tree_fails_naming_the_case_and_returns_inside_the_bound(self) -> None:
        started = time.monotonic()
        with self.assertRaises(AssertionError) as caught:
            run_bounded([sys.executable, "-c", self.HUNG_TREE], 3, "stub-hang-case")
        # sol r4b B2: the ceiling is the helper's own bounds (run bound + whole cleanup bound) plus a stated margin, so a slow but
        # successful taskkill (27 s, 31 s, up to its 60 s bound) is not a false CI failure.
        self.assertLess(time.monotonic() - started, max_run_bounded_sec(3) + CLEANUP_MARGIN_SEC)
        self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught.exception))

    @unittest.skipIf(PWSH is None, "pwsh is not on PATH")
    def test_an_out_of_range_suite_bound_is_refused_at_binding_without_launching_a_child(self) -> None:
        for flag, value in (("-QuietLeaseBoundSec", "-1"), ("-QuietLeaseBoundSec", "2147484"),
                            ("-StageExemptBoundSec", "-1"), ("-StageExemptBoundSec", "2147484")):
            rc, stdout, stderr = run_bounded([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(FIFO_TEST), "-UnitOnly", flag, value],
                                             90, f"fifo binding {flag} {value}")
            self.assertNotEqual(rc, 0, f"{flag} {value}: {stdout}")
            self.assertIn(flag.lstrip("-"), stdout + stderr, f"{flag} {value}")
            self.assertNotIn('"status"', stdout, f"{flag} {value}: the suite must not have run")

    @unittest.skipIf(PWSH is None, "pwsh is not on PATH")
    def test_the_fifo_suite_hands_a_trailing_backslash_root_to_its_children_intact(self) -> None:
        # adversary r4 A1: Invoke-BoundedSuite hand-quotes the child command line, and a RepoRoot that ends in a backslash escaped
        # its closing quote, so -AgentScript and its value merged into -RepoRoot. The ps1's own quoting function is extracted and its
        # output is parsed back with the Windows parser (CommandLineToArgvW) the child process uses.
        import ctypes
        from ctypes import wintypes
        fifo = FIFO_TEST.read_text(encoding="ascii")
        start = fifo.index("function ConvertTo-CommandLineArg") if "function ConvertTo-CommandLineArg" in fifo else -1
        self.assertGreaterEqual(start, 0, "the fifo suite must quote each child argument through ConvertTo-CommandLineArg")
        body = fifo[start:]
        body = body[:body.index("\n}") + 2]
        values = ["C:\\repo root\\", "C:\\repo root", "\\\\server\\share\\dir\\", "C:\\a\\\\", "C:\\plain\\agent.ps1", "a \"quoted\" word\\"]
        script = body + "\n$v = @(" + ",".join("'" + v.replace("'", "''") + "'" for v in values) + ")\n" \
                 "($v | ForEach-Object { ConvertTo-CommandLineArg $_ }) -join ' '"
        proc = _run_pwsh_script(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        command_line = proc.stdout.strip()
        argv_w = ctypes.windll.shell32.CommandLineToArgvW
        argv_w.restype = ctypes.POINTER(wintypes.LPWSTR)
        argv_w.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
        count = ctypes.c_int(0)
        argv = argv_w("prog.exe " + command_line, ctypes.byref(count))
        try:
            parsed = [argv[i] for i in range(1, count.value)]
        finally:
            ctypes.windll.kernel32.LocalFree(argv)
        self.assertEqual(parsed, values, command_line)

    @unittest.skipIf(PWSH is None, "pwsh is not on PATH")
    def test_the_fifo_suite_helper_refuses_an_out_of_range_bound_before_launching_a_child(self) -> None:
        # sol r5 B1 (PowerShell half): the public -QuietLeaseBoundSec / -StageExemptBoundSec are ValidateRange'd, but a direct call to the
        # internal Invoke-BoundedSuite with 2147484 launched the suite and then failed converting to milliseconds. Start-Process is shadowed
        # by a counting stub, so the assertion is that the launcher was never reached for the bad bounds and was reached for a good one.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        start = fifo.index("function ConvertTo-CommandLineArg")
        end_marker = fifo.index("function Invoke-BoundedSuite")
        body = fifo[start:fifo.index("\n}\n", end_marker) + 3]
        self.assertIn("$SuiteBoundMaxSec = 3600", body)
        bounds = (2147484, 3601, 0, -1)
        script = ("$RepoRoot = 'C:\\repo'; $agentScript = 'C:\\repo\\agent.ps1'; $script:launched = 0\n"
                  "function Start-Process { $script:launched++; throw 'LAUNCH-ATTEMPTED' }\n" + body + "\n"
                  "function Initialize-BoundedCaptureIo { }\n"
                  "foreach ($t in " + ",".join(str(b) for b in bounds) + ",1) { try { Invoke-BoundedSuite 'case-x' 'C:\\s.ps1' $t } "
                  "catch { Write-Output ('RESULT|' + $t + '|' + $_.Exception.Message + '|' + $script:launched) } }\n")
        proc = _run_pwsh_script(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = [line.split("|", 3) for line in proc.stdout.splitlines() if line.startswith("RESULT|")]
        self.assertEqual([r[1] for r in rows], [str(b) for b in bounds] + ["1"], proc.stdout)
        for _, bound, message, launched in rows[:-1]:
            self.assertIn("case-x", message, bound)
            self.assertIn(f"bound {bound} s is outside 1..3600 s; no child was started", message, bound)
            self.assertEqual(launched, "0", f"bound {bound}: a child was launched")
        _, bound, message, launched = rows[-1]
        self.assertEqual((message, launched), ("LAUNCH-ATTEMPTED", "1"), "a valid bound must reach the launch")

    @unittest.skipIf(PWSH is None, "pwsh is not on PATH")
    def test_the_fifo_suite_reads_its_captured_output_under_a_bound(self) -> None:
        # adversary r6 A2 (PowerShell half): the suite-named failure reads the captured output through Read-CapturedText, which has its
        # own bound, so a stalled temp store cannot hold the verdict back. The seam hands in a task that never completes.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        body = _fifo_capture_io_region(fifo)
        self.assertIn("$ReadBoundMs = 10000", fifo)
        suite = fifo[fifo.index("function Invoke-BoundedSuite"):]
        suite = suite[:suite.index("\n}\n")]
        self.assertIn("Read-CapturedResult $outF", suite)
        self.assertIn("Read-CapturedResult $errF", suite)
        self.assertIn("Remove-CaptureFiles @($outF, $errF)", suite)
        self.assertNotIn("Remove-Item", suite, "the capture removal is bounded (Remove-CaptureFiles), never a synchronous Remove-Item")
        self.assertNotIn("SilentlyContinue", suite)
        fd, good = tempfile.mkstemp(prefix="read-captured-")
        os.close(fd)
        try:
            Path(good).write_text("hello", encoding="ascii")
            script = ("$ReadBoundMs = 10000\n" + body + "\n"
                      "$hang = { param($p) [Threading.Tasks.TaskCompletionSource[string]]::new().Task }\n"
                      "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                      f"Write-Output ('HUNG|' + (Read-CapturedText (Join-Path ([IO.Path]::GetTempPath()) 'stdout.txt') 300 $hang) + '|' + ($sw.ElapsedMilliseconds -lt {capture_call_ceiling_ms(300)}))\n"
                      "Write-Output ('GOOD|' + (Read-CapturedText '" + good.replace("'", "''") + "'))\n"
                      "Write-Output ('MISSING|' + (Read-CapturedText (Join-Path (Join-Path ([IO.Path]::GetTempPath()) 'no-such-dir-r6') 'x.txt')))\n")
            proc = _run_pwsh_script(script)
        finally:
            os.unlink(good)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = {line.split("|", 1)[0]: line.split("|", 1)[1] for line in proc.stdout.splitlines() if "|" in line}
        self.assertEqual(lines["HUNG"], "<read timed out: stdout.txt not readable within 300 ms>|True")
        self.assertEqual(lines["GOOD"], "hello")
        self.assertTrue(lines["MISSING"].startswith("<read failed: "), lines["MISSING"])

    def test_a_finished_run_returns_its_code_and_both_streams(self) -> None:
        rc, out, err = run_bounded([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"], 60, "finished-case")
        self.assertEqual((rc, out.strip(), err), (3, "out", "err"))


class BoundedRunOutputReadFailureTests(unittest.TestCase):
    """sol r2 BLOCKER: a captured-output read error after a timeout must not replace the case-named timeout verdict.

    Injected in memory (no real process, no real sharing violation): wait() raises TimeoutExpired, the tree and root cleanup
    succeed, and one of the two output reads raises PermissionError(13, 'sharing violation').
    """

    class _TimedOutProc:
        pid = 4242

        def __init__(self) -> None:
            self.waits = 0

        def wait(self, timeout: float | None = None) -> int:
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(["child"], timeout)
            return 1

        def kill(self) -> None:
            pass

    class _FakeTaskkill:
        """The taskkill child: exits with returncode, or never exits (every bounded wait expires; a wait with no bound is a test failure)."""

        def __init__(self, returncode: int | None, *, never_exits: bool = False) -> None:
            self.returncode = returncode
            self.never_exits = never_exits
            self.wait_timeouts: list[float | None] = []
            self.killed = False

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            if timeout is None:
                raise AssertionError("an unbounded wait on taskkill")
            if self.never_exits:
                raise subprocess.TimeoutExpired(["taskkill"], timeout)
            return self.returncode  # type: ignore[return-value]

        def kill(self) -> None:
            self.killed = True

    @staticmethod
    def _popen_for(root: object, taskkill: object, *, taskkill_launch_error: BaseException | None = None):
        """A subprocess.Popen stand-in: the taskkill launch gets the taskkill fake (or raises), anything else is the root process."""
        def popen(argv: list[str], *args: object, **kwargs: object) -> object:
            if argv and argv[0] == "taskkill":
                if taskkill_launch_error is not None:
                    raise taskkill_launch_error
                return taskkill
            return root
        return popen

    def _run_failing_read(self, failing_name: str, *, timeout_expires: bool = True, error: Exception | None = None) -> BaseException:
        proc = self._TimedOutProc()
        if not timeout_expires:
            proc.wait = lambda timeout=None: 0  # type: ignore[method-assign]
        real_read_text = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path.name == failing_name:
                raise error if error is not None else PermissionError(13, "sharing violation")
            return real_read_text(path, *args, **kwargs)

        with mock.patch.object(subprocess, "Popen", side_effect=self._popen_for(proc, self._FakeTaskkill(0))), \
                mock.patch.object(Path, "read_text", read_text):
            try:
                run_bounded(["child"], 3, "stub-hang-case")
            except BaseException as exc:  # noqa: BLE001 - the verdict type is the assertion
                return exc
        self.fail("run_bounded returned instead of failing")

    def test_a_timeout_keeps_its_case_named_verdict_when_the_stdout_read_fails(self) -> None:
        caught = self._run_failing_read("stdout.txt")
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("<read failed: PermissionError>", str(caught))

    def test_a_timeout_keeps_its_case_named_verdict_when_the_stderr_read_fails(self) -> None:
        caught = self._run_failing_read("stderr.txt")
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("<read failed: PermissionError>", str(caught))

    def test_a_timeout_keeps_its_case_named_verdict_whatever_exception_the_read_raises(self) -> None:
        # adversary r7 A2: only OSError was tolerated after a failed wait, so a MemoryError (a huge captured stream under a memory ceiling),
        # a ValueError or a RuntimeError from the read replaced the case-named verdict. Any Exception is recorded in the text instead.
        for name in ("stdout.txt", "stderr.txt"):
            for exc in (MemoryError(), ValueError("bad stream"), RuntimeError("boom"), OSError(5, "I/O error")):
                with self.subTest(stream=name, exc=repr(exc)):
                    caught = self._run_failing_read(name, error=exc)
                    self.assertIsInstance(caught, AssertionError, repr(caught))
                    self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught))
                    self.assertIn("<read failed: ", str(caught))
                    self.assertIn("and its process tree was killed.", str(caught))

    def test_a_read_exception_without_a_timeout_still_surfaces_as_itself(self) -> None:
        for exc in (MemoryError(), ValueError("bad stream")):
            with self.subTest(exc=repr(exc)):
                caught = self._run_failing_read("stdout.txt", timeout_expires=False, error=exc)
                self.assertIs(caught, exc)

    def _run_stalled_read(self, *, timeout_expires: bool) -> tuple[BaseException, float]:
        # adversary r6 A2: after a successful cleanup the captured-output read stalls (a hung share). It must not hold the verdict back.
        release = threading.Event()
        proc = self._TimedOutProc()
        if not timeout_expires:
            proc.wait = lambda timeout=None: 0  # type: ignore[method-assign]
        real_read_text = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path.name == "stdout.txt":
                release.wait(60)
            return real_read_text(path, *args, **kwargs)

        started = time.monotonic()
        try:
            with mock.patch.object(subprocess, "Popen", side_effect=self._popen_for(proc, self._FakeTaskkill(0))), \
                    mock.patch.object(Path, "read_text", read_text), \
                    mock.patch.object(sys.modules[__name__], "READ_BOUND_SEC", 0.3):
                try:
                    run_bounded(["child"], 3, "stalled-read-case")
                except BaseException as exc:  # noqa: BLE001 - the verdict type is the assertion
                    return exc, time.monotonic() - started
        finally:
            release.set()
        self.fail("run_bounded returned instead of failing")

    def test_a_stalled_output_read_after_a_timeout_keeps_the_case_named_verdict_inside_the_read_bound(self) -> None:
        caught, elapsed = self._run_stalled_read(timeout_expires=True)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("stalled-read-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("<read timed out: stdout.txt not readable within 0.3 s>", str(caught))
        self.assertLess(elapsed, 10, "the stalled read held the verdict back")

    def test_a_stalled_output_read_without_a_timeout_surfaces_as_a_timeout_error_not_a_hang(self) -> None:
        caught, elapsed = self._run_stalled_read(timeout_expires=False)
        self.assertIsInstance(caught, TimeoutError, repr(caught))
        self.assertIn("stdout.txt not readable within 0.3 s", str(caught))
        self.assertLess(elapsed, 10)

    def test_the_read_bounds_are_part_of_the_stated_ceiling_arithmetic(self) -> None:
        self.assertEqual(MAX_READ_SEC, 2 * READ_BOUND_SEC)
        self.assertGreater(MAX_READ_SEC, 0)

    def _run_failing_cleanup(self, *, taskkill: object, second_wait_expires: bool = False,
                             second_wait_raises: BaseException | None = None) -> BaseException:
        # sol adversary A1: the cleanup steps after a timeout must not replace the case-named verdict either.
        proc = self._TimedOutProc()
        if second_wait_expires:
            def wait(timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired(["child"], timeout)
            proc.wait = wait  # type: ignore[method-assign]
        elif second_wait_raises is not None:
            # sol r3 B1: the first wait times out (the hang), the final cleanup wait fails with a Windows handle error.
            def wait(timeout: float | None = None) -> int:
                proc.waits += 1
                if proc.waits == 1:
                    raise subprocess.TimeoutExpired(["child"], timeout)
                raise second_wait_raises
            proc.wait = wait  # type: ignore[method-assign]
        if isinstance(taskkill, subprocess.TimeoutExpired):
            popen = self._popen_for(proc, self._FakeTaskkill(None, never_exits=True))
        elif isinstance(taskkill, BaseException):
            popen = self._popen_for(proc, None, taskkill_launch_error=taskkill)
        else:
            popen = self._popen_for(proc, self._FakeTaskkill(taskkill))
        with mock.patch.object(subprocess, "Popen", side_effect=popen):
            try:
                run_bounded(["child"], 3, "stub-hang-case")
            except BaseException as exc:  # noqa: BLE001 - the verdict type is the assertion
                return exc
        self.fail("run_bounded returned instead of failing")

    def test_a_timeout_keeps_its_case_named_verdict_when_taskkill_hangs_or_fails(self) -> None:
        for label, kwargs, expect in (
                ("taskkill timeout", {"taskkill": subprocess.TimeoutExpired(["taskkill"], 60)}, "taskkill failed"),
                ("taskkill missing", {"taskkill": FileNotFoundError(2, "no taskkill")}, "taskkill failed"),
                ("taskkill rc 1", {"taskkill": 1}, "taskkill exited 1"),
                ("root wait timeout", {"taskkill": 0, "second_wait_expires": True}, "still running 30 s after the kill")):
            caught = self._run_failing_cleanup(**kwargs)
            self.assertIsInstance(caught, AssertionError, f"{label}: {caught!r}")
            self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught), label)
            self.assertIn("process tree kill is NOT confirmed", str(caught), label)
            self.assertIn(expect, str(caught), label)
            self.assertNotIn("process tree was killed", str(caught), label)

    def test_a_timeout_keeps_its_case_named_verdict_when_the_final_cleanup_wait_raises_an_oserror(self) -> None:
        # sol r3 B1: _kill_tree caught OSError on taskkill and on kill() but not on the final root wait, so an
        # OSError(6, "invalid handle") there escaped and replaced the promised case-named AssertionError.
        for label, exc in (("invalid handle", OSError(6, "invalid handle")),
                           ("access denied", PermissionError(5, "denied"))):
            caught = self._run_failing_cleanup(taskkill=0, second_wait_raises=exc)
            self.assertIsInstance(caught, AssertionError, f"{label}: {caught!r}")
            self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught), label)
            self.assertIn("process tree kill is NOT confirmed", str(caught), label)
            self.assertIn("waiting for the root process failed", str(caught), label)
            self.assertNotIn("process tree was killed", str(caught), label)

    def test_a_timeout_keeps_its_case_named_verdict_whatever_exception_a_cleanup_step_raises(self) -> None:
        # adversary r10 (pass 2) A1: the cleanup steps caught only OSError / SubprocessError, so a MemoryError or a RuntimeError (an audit hook
        # that denies the taskkill launch) from ANY step escaped, skipped the remaining steps (the root kill) and replaced the case-named
        # AssertionError. Every step records any Exception and carries on; only an interrupt (not an Exception) still propagates.
        def boom(exc: BaseException):
            def raiser(*args: object, **kwargs: object) -> None:
                raise exc
            return raiser

        def run(proc: object, taskkill: object, **popen_kwargs: object) -> BaseException:
            with mock.patch.object(subprocess, "Popen", side_effect=self._popen_for(proc, taskkill, **popen_kwargs)):
                try:
                    run_bounded(["child"], 3, "stub-hang-case")
                except BaseException as caught:  # noqa: BLE001 - the verdict type is the assertion
                    return caught
            self.fail("run_bounded returned instead of failing")

        for exc in (MemoryError(), RuntimeError("audit denies taskkill")):
            tk_wait = self._FakeTaskkill(0)
            tk_wait.wait = boom(exc)  # type: ignore[method-assign]
            tk_kill = self._FakeTaskkill(None, never_exits=True)
            tk_kill.kill = boom(exc)  # type: ignore[method-assign]
            tk_reap = self._FakeTaskkill(None, never_exits=True)
            reaps: list[int] = []

            def reap_wait(timeout: float | None = None, _tk: object = tk_reap, _reaps: list[int] = reaps, _exc: BaseException = exc) -> int:
                _reaps.append(1)
                if len(_reaps) == 1:
                    raise subprocess.TimeoutExpired(["taskkill"], timeout)
                raise _exc
            tk_reap.wait = reap_wait  # type: ignore[method-assign]
            proc_kill = self._TimedOutProc()
            proc_kill.kill = boom(exc)  # type: ignore[method-assign]
            proc_wait = self._TimedOutProc()

            def root_wait(timeout: float | None = None, _p: object = proc_wait, _exc: BaseException = exc) -> int:
                _p.waits += 1  # type: ignore[attr-defined]
                if _p.waits == 1:  # type: ignore[attr-defined]
                    raise subprocess.TimeoutExpired(["child"], timeout)
                raise _exc
            proc_wait.wait = root_wait  # type: ignore[method-assign]
            cases = (
                ("taskkill launch", run(self._TimedOutProc(), None, taskkill_launch_error=exc), "taskkill failed"),
                ("taskkill wait", run(self._TimedOutProc(), tk_wait), "taskkill failed"),
                ("taskkill kill", run(self._TimedOutProc(), tk_kill), "taskkill kill failed"),
                ("taskkill reap", run(self._TimedOutProc(), tk_reap), "waiting for taskkill failed"),
                ("root kill", run(proc_kill, self._FakeTaskkill(0)), "kill failed"),
                ("root wait", run(proc_wait, self._FakeTaskkill(0)), "waiting for the root process failed"),
            )
            for label, caught, expect in cases:
                with self.subTest(exc=repr(exc), step=label):
                    self.assertIsInstance(caught, AssertionError, repr(caught))
                    self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught))
                    self.assertIn("process tree kill is NOT confirmed", str(caught))
                    self.assertIn(expect, str(caught))

    def test_a_confirmed_kill_still_says_the_tree_was_killed(self) -> None:
        caught = self._run_failing_cleanup(taskkill=0)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("and its process tree was killed.", str(caught))

    def test_a_read_failure_without_a_timeout_still_surfaces_as_the_oserror(self) -> None:
        # The non-timeout path is unchanged: with no verdict to protect, the read error is the failure.
        for name in ("stdout.txt", "stderr.txt"):
            caught = self._run_failing_read(name, timeout_expires=False)
            self.assertIsInstance(caught, PermissionError, f"{name}: {caught!r}")


class BoundedRunInputRangeTests(unittest.TestCase):
    """sol r5 B1 (INV-9): an accepted timeout must never bypass the cleanup after the child is launched.

    The unusable bounds (1e308, inf, nan) made the Windows wait raise OverflowError / ValueError after the launch, past the
    TimeoutExpired handler, so the child survived and no case-named verdict was produced. Everything is in memory: Popen is a mock.
    """

    class _Proc:
        pid = 4242
        returncode = 0

        def __init__(self, wait_error: BaseException | None = None) -> None:
            self.wait_error = wait_error
            self.wait_timeouts: list[float | None] = []
            self.kills = 0

        def wait(self, timeout: float | None = None) -> int:
            self.wait_timeouts.append(timeout)
            if self.wait_error is not None and len(self.wait_timeouts) == 1:
                raise self.wait_error
            return 0

        def kill(self) -> None:
            self.kills += 1

    UNUSABLE = (1e308, float("inf"), float("-inf"), float("nan"), 0, 0.0, -1, -0.5, MAX_RUN_BOUNDED_TIMEOUT_SEC + 1, True, None, "5")

    def test_an_unusable_timeout_is_refused_before_any_child_is_launched(self) -> None:
        for timeout in self.UNUSABLE:
            with self.subTest(timeout=timeout), mock.patch.object(subprocess, "Popen") as popen, \
                    mock.patch.object(tempfile, "mkdtemp") as mkdtemp:
                with self.assertRaises(ValueError) as caught:
                    run_bounded(["child"], timeout, "range-case")  # type: ignore[arg-type]
                popen.assert_not_called()
                mkdtemp.assert_not_called()
                self.assertIn("range-case", str(caught.exception))
                self.assertIn(repr(timeout), str(caught.exception))
                self.assertIn("no child was started", str(caught.exception))

    def test_the_largest_accepted_timeout_is_far_below_the_wait_overflow_limit(self) -> None:
        import threading
        self.assertLess(MAX_RUN_BOUNDED_TIMEOUT_SEC, threading.TIMEOUT_MAX)
        self.assertGreaterEqual(MAX_RUN_BOUNDED_TIMEOUT_SEC, UNIT_RUN_TIMEOUT_SEC)

    def test_a_usable_timeout_at_either_edge_runs_the_child(self) -> None:
        for timeout in (0.001, 1, UNIT_RUN_TIMEOUT_SEC, MAX_RUN_BOUNDED_TIMEOUT_SEC):
            proc = self._Proc()
            with self.subTest(timeout=timeout), mock.patch.object(subprocess, "Popen", return_value=proc):
                self.assertEqual(run_bounded(["child"], timeout, "edge-case"), (0, "", ""))
                self.assertEqual(proc.wait_timeouts, [timeout])

    def test_a_wait_that_raises_after_the_launch_still_kills_the_tree_and_names_the_case(self) -> None:
        for label, exc in (("overflow", OverflowError("timeout is too large")), ("value", ValueError("Invalid value NaN (not a number)")),
                           ("oserror", OSError(6, "invalid handle"))):
            proc = self._Proc(wait_error=exc)
            taskkill = BoundedRunOutputReadFailureTests._FakeTaskkill(0)
            popen = BoundedRunOutputReadFailureTests._popen_for(proc, taskkill)
            with self.subTest(label=label), mock.patch.object(subprocess, "Popen", side_effect=popen):
                with self.assertRaises(AssertionError) as caught:
                    run_bounded(["child"], 3, "wait-fail-case")
                self.assertEqual(taskkill.wait_timeouts, [TASKKILL_BOUND_SEC], label)  # the bounded taskkill ran
                self.assertGreaterEqual(proc.kills, 1, label)  # and so did the root kill
                self.assertIn("wait-fail-case could not be waited on", str(caught.exception), label)
                self.assertIn(f"({type(exc).__name__})", str(caught.exception), label)
                self.assertNotIn(str(exc), str(caught.exception), label)  # r16: the type only, never the message
                self.assertIn("and its process tree was killed.", str(caught.exception), label)

    def test_an_interrupt_during_the_wait_kills_the_tree_and_is_not_swallowed(self) -> None:
        proc = self._Proc(wait_error=KeyboardInterrupt())
        taskkill = BoundedRunOutputReadFailureTests._FakeTaskkill(0)
        popen = BoundedRunOutputReadFailureTests._popen_for(proc, taskkill)
        with mock.patch.object(subprocess, "Popen", side_effect=popen):
            with self.assertRaises(KeyboardInterrupt):
                run_bounded(["child"], 3, "interrupt-case")
        self.assertEqual(taskkill.wait_timeouts, [TASKKILL_BOUND_SEC])
        self.assertGreaterEqual(proc.kills, 1)


def _hostile_exception(kind: str, calls: list[str]) -> BaseException:
    """An exception whose rendering is hostile (sol r14 B1 / adversary r14 A1). kind: repr_throws, str_throws, repr_blocks, str_blocks.

    Every call of a hostile method is recorded in `calls` and then raises (a real blocking call cannot be bounded, so the contract is that
    a cleanup-critical path never calls it at all: the tests assert `calls` stays empty)."""
    release = threading.Event()

    def hostile(label: str, blocks: bool):
        def method(self: object) -> str:
            calls.append(label)
            if blocks:
                release.wait(0.2)  # a short stand-in for a call that would never return
            raise ValueError("exception " + label + " failed")
        return method

    attrs = {"__repr__": hostile("repr", kind == "repr_blocks")} if kind.startswith("repr") else {"__str__": hostile("str", kind == "str_blocks")}
    return type("Hostile" + kind.title().replace("_", ""), (RuntimeError,), attrs)("hostile detail")


HOSTILE_KINDS = ("repr_throws", "str_throws", "repr_blocks", "str_blocks")


class HostileExceptionRenderingTests(unittest.TestCase):
    """sol r14 B1 / adversary r14 A1: no cleanup-critical path of run_bounded renders an arbitrary exception with repr() / str() / !r.

    Sol reproduced it with a real subprocess.Popen audit event: an exception with a throwing __repr__ escaped the taskkill launch handler
    and the root process was neither killed nor reaped. The class is every place the helper formats an exception it did not create.
    """

    class _Root:
        """The root process: records its kill and its reaping (a wait after the kill)."""
        pid = 4242

        def __init__(self, *, first_wait: BaseException | None = None, kill_error: BaseException | None = None,
                     reap_error: BaseException | None = None) -> None:
            self.first_wait, self.kill_error, self.reap_error = first_wait, kill_error, reap_error
            self.events: list[str] = []

        def wait(self, timeout: float | None = None) -> int:
            first = "wait" not in self.events
            self.events.append("wait")
            if first:
                raise self.first_wait if self.first_wait is not None else subprocess.TimeoutExpired(["child"], timeout)
            if self.reap_error is not None:
                raise self.reap_error
            return 1

        def kill(self) -> None:
            self.events.append("kill")
            if self.kill_error is not None:
                raise self.kill_error

        @property
        def killed_and_reaped(self) -> bool:
            return "kill" in self.events and "wait" in self.events[self.events.index("kill"):]

    def _run(self, root: object, taskkill: object, **popen_kwargs: object) -> BaseException:
        popen = BoundedRunOutputReadFailureTests._popen_for(root, taskkill, **popen_kwargs)
        with mock.patch.object(subprocess, "Popen", side_effect=popen):
            try:
                run_bounded(["child"], 3, "hostile-case")
            except BaseException as caught:  # noqa: BLE001 - the verdict type is the assertion
                return caught
        self.fail("run_bounded returned instead of failing")

    def test_a_hostile_exception_in_any_cleanup_phase_keeps_the_case_named_verdict_and_the_root_kill_and_reap(self) -> None:
        fake = BoundedRunOutputReadFailureTests._FakeTaskkill
        for kind in HOSTILE_KINDS:
            calls: list[str] = []
            exc = _hostile_exception(kind, calls)

            def boom(*args: object, **kwargs: object) -> None:
                raise exc

            tk_wait = fake(0)
            tk_wait.wait = boom  # type: ignore[method-assign]
            tk_kill = fake(None, never_exits=True)
            tk_kill.kill = boom  # type: ignore[method-assign]
            tk_reap = fake(None, never_exits=True)
            reaps: list[int] = []

            def reap_wait(timeout: float | None = None, _reaps: list[int] = reaps) -> int:
                _reaps.append(1)
                if len(_reaps) == 1:
                    raise subprocess.TimeoutExpired(["taskkill"], timeout)
                raise exc
            tk_reap.wait = reap_wait  # type: ignore[method-assign]
            phases = (
                ("taskkill launch", self._Root(), None, {"taskkill_launch_error": exc}, "taskkill failed", "exceeded its 3 s wall-clock bound"),
                ("taskkill wait", self._Root(), tk_wait, {}, "taskkill failed", "exceeded its 3 s wall-clock bound"),
                ("taskkill kill", self._Root(), tk_kill, {}, "taskkill kill failed", "exceeded its 3 s wall-clock bound"),
                ("taskkill reap", self._Root(), tk_reap, {}, "waiting for taskkill failed", "exceeded its 3 s wall-clock bound"),
                ("root kill", self._Root(kill_error=exc), fake(0), {}, "kill failed", "exceeded its 3 s wall-clock bound"),
                ("root reap", self._Root(reap_error=exc), fake(0), {}, "waiting for the root process failed", "exceeded its 3 s wall-clock bound"),
                ("main wait", self._Root(first_wait=exc), fake(0), {}, "its process tree was killed", "could not be waited on"),
            )
            for label, root, taskkill, popen_kwargs, expect, verdict in phases:
                with self.subTest(kind=kind, phase=label):
                    caught = self._run(root, taskkill, **popen_kwargs)
                    self.assertIsInstance(caught, AssertionError, repr(caught))
                    self.assertIn("hostile-case " + verdict, str(caught))
                    self.assertIn(expect, str(caught))
                    self.assertTrue(root.killed_and_reaped, f"the root was not killed and reaped: {root.events}")
                    self.assertEqual(calls, [], f"{kind}: the hostile {calls} was called by a cleanup path")
                    calls.clear()

    def test_a_hostile_exception_in_a_capture_io_helper_is_named_by_its_type_and_never_rendered(self) -> None:
        for kind in HOSTILE_KINDS:
            calls: list[str] = []
            exc = _hostile_exception(kind, calls)
            name = type(exc).__name__

            class Handle:
                def __init__(self, error: BaseException | None) -> None:
                    self.error, self.closed = error, False

                def close(self) -> None:
                    self.closed = True
                    if self.error is not None:
                        raise self.error

            with self.subTest(kind=kind, helper="close: a failing close does not leave the next handle open"):
                first, second = Handle(exc), Handle(None)
                note = _close_captures([first, second])
                self.assertTrue(second.closed, "the close worker died on the hostile exception before closing the next handle")
                self.assertIn("capture close failed", note)
                self.assertIn(name, note)
            with self.subTest(kind=kind, helper="close: thread start"), mock.patch.object(threading.Thread, "start", side_effect=exc):
                note = _close_captures([Handle(None)])
                self.assertIn("capture close not started", note)
                self.assertIn(name, note)
            tmp = tempfile.mkdtemp(prefix="hostile-rm-")
            try:
                with self.subTest(kind=kind, helper="remove: rmtree"), mock.patch.object(shutil, "rmtree", side_effect=exc):
                    note = _remove_capture_dir(tmp)
                    self.assertIn("capture cleanup failed", note)
                    self.assertIn(name, note)
                with self.subTest(kind=kind, helper="remove: probe"), mock.patch.object(os, "lstat", side_effect=exc):
                    note = _remove_capture_dir(tmp)
                    self.assertIn("capture cleanup", note)
                    self.assertNotIn("was not fully removed", note, "the probe's failure was lost as a worker death")
                    self.assertIn(name, note)
                with self.subTest(kind=kind, helper="remove: thread start"), mock.patch.object(threading.Thread, "start", side_effect=exc):
                    note = _remove_capture_dir(tmp)
                    self.assertIn("capture cleanup not started", note)
                    self.assertIn(name, note)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            target = Path(tempfile.gettempdir()) / "hostile-read.txt"
            with self.subTest(kind=kind, helper="read: read_text"), mock.patch.object(Path, "read_text", side_effect=exc):
                text = _read_captured(target, tolerate_oserror=True)
                self.assertIn("<read failed: ", text)
                self.assertIn(name, text)
            with self.subTest(kind=kind, helper="read: thread start"), mock.patch.object(threading.Thread, "start", side_effect=exc):
                text = _read_captured(target, tolerate_oserror=True)
                self.assertIn("<read failed: could not start the reader thread", text)
                self.assertIn(name, text)
            self.assertEqual(calls, [], f"{kind}: a capture io helper rendered the hostile exception")

    def test_the_diagnostic_names_the_type_and_nothing_else(self) -> None:
        # r16 (replaces test_the_rendering_helpers_keep_the_builtin_detail_and_drop_everything_else, which pinned repr / str detail): an
        # ordinary builtin exception is named by its type too, because no cleanup-critical path renders an exception value any more.
        self.assertEqual(_exc_type_name(OSError(6, "invalid handle")), "OSError")
        self.assertEqual(_exc_type_name(PermissionError(13, "denied")), "PermissionError")
        self.assertEqual(_exc_type_name(subprocess.TimeoutExpired(["child"], 3)), "TimeoutExpired")
        calls: list[str] = []

        class Quiet(RuntimeError):
            def __str__(self) -> str:
                calls.append("str")
                return "never"

        class Arg:
            def __repr__(self) -> str:
                calls.append("arg-repr")
                return "never"
            __str__ = __repr__

        for exc in (Quiet("x"), RuntimeError(Arg()), OSError(2, "no", Arg()), _hostile_exception("repr_throws", calls)):
            self.assertEqual(_exc_type_name(exc), type(exc).__name__)
        self.assertEqual(calls, [])

        class Meta(type):
            @property
            def __name__(cls) -> str:  # type: ignore[override]
                raise ValueError("hostile metaclass name")

        hostile_class = Meta("Whatever", (RuntimeError,), {})
        self.assertEqual(_exc_type_name(hostile_class("x")), "Whatever", "the name is read past a metaclass that overrides __name__; a subclass is never rendered")

    def test_adversary_r15_a1_an_exact_builtin_exception_with_a_reassigned_field_is_not_rendered(self) -> None:
        # adversary r15 A1: the trust check looked at args and filename only, so an exact OSError whose strerror was reassigned to an object
        # with a hostile __str__ was "trusted" and str() ran that object (it raised and replaced the case-named verdict; it could block).
        calls: list[str] = []

        class Payload:
            def __str__(self) -> str:
                calls.append("payload-str")
                raise RuntimeError("REPLACED-CASE-VERDICT")
            __repr__ = __str__

        fields = OSError(5, "original")
        fields.strerror = Payload()  # type: ignore[assignment]
        expired = subprocess.TimeoutExpired(["child"], 3)
        expired.cmd = Payload()
        called = subprocess.CalledProcessError(1, ["child"])
        called.output = Payload()
        for exc in (fields, expired, called):
            self.assertEqual(_exc_type_name(exc), type(exc).__name__)
        target = Path(tempfile.gettempdir()) / "hostile-field-read.txt"
        with mock.patch.object(Path, "read_text", side_effect=fields):
            self.assertEqual(_read_captured(target, tolerate_oserror=True), "<read failed: OSError>")
        self.assertEqual(calls, [])

    def test_adversary_r15_a2_a_metaclass_or_class_property_of_the_exception_never_runs(self) -> None:
        # adversary r15 A2: _exc_trusted did `klass not in frozenset` (the metaclass __hash__ / __eq__ ran, and a blocking one held the
        # cleanup before the root kill), and the read path used isinstance(error, OSError) (a __class__ property ran). Identity checks only.
        calls: list[str] = []

        class Meta(type):
            def __hash__(cls) -> int:
                calls.append("meta-hash")
                return type.__hash__(cls)

            def __eq__(cls, other: object) -> bool:
                calls.append("meta-eq")
                return type.__eq__(cls, other)

        class AuditError(RuntimeError, metaclass=Meta):
            def __repr__(self) -> str:
                calls.append("repr")
                raise ValueError("exception repr failed")

            @property  # type: ignore[misc]
            def __class__(self):  # noqa: ANN204
                calls.append("class-property")
                return type(self)

        exc = AuditError("denied")
        calls.clear()
        self.assertEqual(_exc_type_name(exc), "AuditError")
        target = Path(tempfile.gettempdir()) / "hostile-meta-read.txt"
        with mock.patch.object(Path, "read_text", side_effect=exc):
            self.assertEqual(_read_captured(target, tolerate_oserror=True), "<read failed: AuditError>")
        root = self._Root()
        caught = self._run(root, None, taskkill_launch_error=exc)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("hostile-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertTrue(root.killed_and_reaped, f"the root was not killed and reaped: {root.events}")
        self.assertEqual(calls, [], "a cleanup path ran the exception class's metaclass or __class__ property")

    def test_r16_a1_a_mutation_between_steps_reaches_no_formatting_and_the_named_failure_is_kept(self) -> None:
        # hub ruling r16 on adversary r15 A1 (the fifth pass on this class): a validator or a snapshot still lost to a mutation between the
        # check and the render. The mutation is injected at the last moment before any rendering used to happen (the return of the old
        # validator; the call of the type-name read now), by a profile hook, so the interleaving is deterministic. A list the exception
        # holds gets a payload appended, or its args are replaced by one; the payload's repr / str record themselves and raise an interrupt.
        for mode in ("list-append", "args-replace", "strerror-replace"):
            calls: list[str] = []

            class Payload:
                def __repr__(self) -> str:
                    calls.append("payload-render")
                    raise KeyboardInterrupt("REPLACED-CASE-VERDICT")
                __str__ = __repr__

            values: list[object] = []
            exc = OSError(5, "original") if mode == "strerror-replace" else RuntimeError(values)

            def mutate(_mode: str = mode, _values: list[object] = values, _exc: BaseException = exc) -> None:
                if _mode == "list-append":
                    _values.append(Payload())
                elif _mode == "args-replace":
                    _exc.args = (Payload(),)
                else:
                    _exc.strerror = Payload()  # type: ignore[attr-defined]
                    _exc.args = (5, Payload())

            def profiler(frame: object, event: str, arg: object) -> None:
                name = frame.f_code.co_name  # type: ignore[attr-defined]
                if (event == "call" and name == "_exc_type_name") or (event == "return" and name == "_exc_trusted"):
                    mutate()

            fake = BoundedRunOutputReadFailureTests._FakeTaskkill
            phases = (
                ("taskkill launch", self._Root(), None, {"taskkill_launch_error": exc}, f"taskkill failed: {type(exc).__name__}", "exceeded its 3 s wall-clock bound"),
                ("root kill", self._Root(kill_error=exc), fake(0), {}, f"kill failed: {type(exc).__name__}", "exceeded its 3 s wall-clock bound"),
                ("root reap", self._Root(reap_error=exc), fake(0), {}, f"waiting for the root process failed: {type(exc).__name__}", "exceeded its 3 s wall-clock bound"),
                ("main wait", self._Root(first_wait=exc), fake(0), {}, "its process tree was killed", "could not be waited on"),
            )
            for label, root, taskkill, popen_kwargs, expect, verdict in phases:
                with self.subTest(mode=mode, phase=label):
                    sys.setprofile(profiler)
                    try:
                        caught = self._run(root, taskkill, **popen_kwargs)
                    finally:
                        sys.setprofile(None)
                    self.assertIsInstance(caught, AssertionError, repr(caught))
                    self.assertIn("hostile-case " + verdict, str(caught))
                    self.assertIn(expect, str(caught))
                    self.assertNotIn("REPLACED-CASE-VERDICT", str(caught))
                    self.assertTrue(root.killed_and_reaped, f"the root was not killed and reaped: {root.events}")
                    self.assertEqual(calls, [], f"{mode}: a mutated exception field reached formatting in the {label} step")
                    calls.clear()
            target = Path(tempfile.gettempdir()) / "mutated-read.txt"
            sys.setprofile(profiler)
            try:
                with mock.patch.object(Path, "read_text", side_effect=exc):
                    read = _read_captured(target, tolerate_oserror=True)
            finally:
                sys.setprofile(None)
            self.assertEqual(read, f"<read failed: {type(exc).__name__}>")
            self.assertEqual(calls, [], f"{mode}: a mutated exception field reached formatting in the read step")

    def test_r16_a1_the_cleanup_source_renders_no_exception_value(self) -> None:
        # r16 source pin: an exception in the cleanup-critical helpers is named through _exc_type_name; none is formatted, repr()'d or str()'d.
        source = Path(__file__).read_text(encoding="utf-8")
        region = source[source.index("def _exc_type_name"):source.index("def run_bounded")]
        for forbidden in ("_safe_repr", "_safe_text", "_exc_trusted", "_PLAIN_DATA", "_TRUSTED_OS", "!r}", "repr(exc", "str(exc", "OSError.errno", "OSError.strerror"):
            self.assertNotIn(forbidden, "\n".join(_code_lines(region)), f"the cleanup helpers still contain {forbidden!r}")
        for name in re.findall(r"\{(\w+)\}", "\n".join(_code_lines(region))):
            self.assertNotIn(name, ("exc", "error", "caught", "e"), f"an exception object is interpolated into text as {{{name}}}")


class BoundedCaptureIoTests(unittest.TestCase):
    """sol r6 B1 / B2: every step after the launch that touches the capture files (open, read, JSON re-read, removal) is bounded,
    and a step that overruns is abandoned and NAMED in the verdict rather than awaited.

    The Python half injects a stalled rmtree in memory; the PowerShell half drives the script's own helpers in a bare pwsh, with a real
    named pipe as the stalled file (a client that connects to a pipe nobody writes to blocks in its first read).
    """

    def _run_with_stalled_rmtree(self, *, timeout_expires: bool) -> tuple[BaseException | None, float, list[warnings.WarningMessage]]:
        release = threading.Event()
        real_rmtree = shutil.rmtree
        proc = BoundedRunOutputReadFailureTests._TimedOutProc()
        if not timeout_expires:
            proc.wait = lambda timeout=None: 0  # type: ignore[method-assign]
        seen: list[str] = []

        def stalled_rmtree(path: str, *args: object, **kwargs: object) -> None:
            seen.append(path)
            release.wait(60)
            real_rmtree(path, *args, **kwargs)

        popen = BoundedRunOutputReadFailureTests._popen_for(proc, BoundedRunOutputReadFailureTests._FakeTaskkill(0))
        started = time.monotonic()
        caught: BaseException | None = None
        try:
            with warnings.catch_warnings(record=True) as recorded, mock.patch.object(subprocess, "Popen", side_effect=popen), \
                    mock.patch.object(shutil, "rmtree", stalled_rmtree), \
                    mock.patch.object(sys.modules[__name__], "CAPTURE_CLEANUP_BOUND_SEC", 0.3):
                warnings.simplefilter("always")
                try:
                    run_bounded(["child"], 3, "stalled-cleanup-case")
                except BaseException as exc:  # noqa: BLE001 - the verdict type is the assertion
                    caught = exc
        finally:
            release.set()
        elapsed = time.monotonic() - started
        self.assertEqual(len(seen), 1)
        self._leftover = seen[0]
        deadline = time.monotonic() + 10
        while os.path.exists(seen[0]) and time.monotonic() < deadline:  # the abandoned thread finishes once released
            time.sleep(0.05)
        return caught, elapsed, recorded

    def test_a_stalled_capture_cleanup_after_a_timeout_returns_the_named_verdict_and_names_the_abandoned_step(self) -> None:
        caught, elapsed, _ = self._run_with_stalled_rmtree(timeout_expires=True)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("stalled-cleanup-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn(f"capture cleanup abandoned after 0.3 s: {self._leftover}", str(caught))
        self.assertLess(elapsed, 10, "the stalled removal held the verdict back")

    def test_a_stalled_capture_cleanup_after_a_clean_run_returns_the_result_and_warns(self) -> None:
        # The run itself succeeded, so its result is returned; the abandoned removal is reported as a warning, not raised.
        release = threading.Event()
        real_rmtree = shutil.rmtree

        def stalled_rmtree(path: str, *args: object, **kwargs: object) -> None:
            release.wait(60)
            real_rmtree(path, *args, **kwargs)

        started = time.monotonic()
        try:
            with warnings.catch_warnings(record=True) as recorded, mock.patch.object(shutil, "rmtree", stalled_rmtree), \
                    mock.patch.object(sys.modules[__name__], "CAPTURE_CLEANUP_BOUND_SEC", 0.3):
                warnings.simplefilter("always")
                result = run_bounded([sys.executable, "-c", "print('ok')"], 60, "clean-run-case")
        finally:
            release.set()
        self.assertEqual((result[0], result[1].strip()), (0, "ok"))
        self.assertLess(time.monotonic() - started, 30)
        self.assertTrue(any("capture cleanup abandoned after 0.3 s" in str(w.message) and "clean-run-case" in str(w.message)
                            for w in recorded), [str(w.message) for w in recorded])

    def _run_with_failing_thread_start(self, thread_name_prefix: str, *, timeout_expires: bool = True) -> BaseException:
        proc = BoundedRunOutputReadFailureTests._TimedOutProc()
        if not timeout_expires:
            proc.wait = lambda timeout=None: 0  # type: ignore[method-assign]
        real_start = threading.Thread.start

        def start(thread: threading.Thread) -> None:
            if thread.name.startswith(thread_name_prefix):
                raise RuntimeError("can't start new thread")
            real_start(thread)

        popen = BoundedRunOutputReadFailureTests._popen_for(proc, BoundedRunOutputReadFailureTests._FakeTaskkill(0))
        with mock.patch.object(subprocess, "Popen", side_effect=popen), mock.patch.object(threading.Thread, "start", start):
            try:
                run_bounded(["child"], 3, "thread-start-case")
            except BaseException as exc:  # noqa: BLE001 - the verdict type is the assertion
                return exc
        self.fail("run_bounded returned instead of failing")

    def test_a_worker_thread_that_cannot_start_keeps_the_case_named_verdict(self) -> None:
        # adversary r7 A1: Thread.start() raising RuntimeError("can't start new thread") (a thread or memory limit) escaped from the reader
        # and from the cleanup and replaced the case-named verdict.
        caught = self._run_with_failing_thread_start("read-")
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("thread-start-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("<read failed: could not start the reader thread (RuntimeError)>", str(caught))
        caught = self._run_with_failing_thread_start("rmtree-capture")
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("thread-start-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("capture cleanup not started (RuntimeError): ", str(caught))

    def test_a_reader_thread_that_cannot_start_without_a_timeout_surfaces_as_itself(self) -> None:
        caught = self._run_with_failing_thread_start("read-", timeout_expires=False)
        self.assertIsInstance(caught, RuntimeError, repr(caught))

    def test_a_cleanup_that_leaves_the_capture_behind_is_reported_not_silent(self) -> None:
        # adversary r7 A3: shutil.rmtree(ignore_errors=True) hides a held or read-only file, so the removal read as a success.
        real_rmtree = shutil.rmtree
        seen: list[str] = []

        def leave_behind(path: str, *args: object, **kwargs: object) -> None:
            seen.append(path)

        proc = BoundedRunOutputReadFailureTests._TimedOutProc()
        popen = BoundedRunOutputReadFailureTests._popen_for(proc, BoundedRunOutputReadFailureTests._FakeTaskkill(0))
        try:
            with mock.patch.object(subprocess, "Popen", side_effect=popen), mock.patch.object(shutil, "rmtree", leave_behind):
                with self.assertRaises(AssertionError) as caught:
                    run_bounded(["child"], 3, "left-behind-case")
        finally:
            for path in seen:
                real_rmtree(path, ignore_errors=True)
        self.assertEqual(len(seen), 1)
        self.assertIn(f"capture cleanup incomplete: {seen[0]} was not fully removed", str(caught.exception))

    @staticmethod
    @contextlib.contextmanager
    def _existence_probe_denied(denied: str):
        """Every stat of `denied` raises PermissionError, and os.path.exists answers the way the CPython docs say it does on such an error
        (False). Patching exists as well keeps the case independent of the platform's native exists (nt._path_exists on Windows)."""
        real_stat, real_lstat = os.stat, os.lstat

        def deny(real):
            def probe(path, *args, **kwargs):
                if os.fspath(path) == denied:
                    raise PermissionError(13, "Access is denied")
                return real(path, *args, **kwargs)
            return probe

        denying_stat, denying_lstat = deny(real_stat), deny(real_lstat)

        def exists(path) -> bool:
            try:
                denying_stat(path)
            except (OSError, ValueError):
                return False
            return True

        with mock.patch.object(os, "stat", denying_stat), mock.patch.object(os, "lstat", denying_lstat), \
                mock.patch.object(os.path, "exists", exists):
            yield

    def test_a_cleanup_whose_delete_and_existence_probe_are_both_denied_is_unconfirmed_not_a_removal(self) -> None:
        # sol r8 B2: rmtree(ignore_errors=True) hid the denied delete and os.path.exists answers False on a PermissionError, so the retained
        # capture directory read as a clean removal (note ''). Only FileNotFoundError proves the path is gone.
        path = tempfile.mkdtemp(prefix="heavy-slot-gate-denied-")
        try:
            with mock.patch.object(shutil, "rmtree", lambda *args, **kwargs: None), self._existence_probe_denied(path):
                note = _remove_capture_dir(path)
            self.assertTrue(os.path.isdir(path), "the fixture directory must have been retained")
            self.assertIn("capture cleanup incomplete", note)
            self.assertIn("could not be confirmed", note)
            self.assertIn("PermissionError", note)
            self.assertIn(path, note)
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def test_a_cleanup_whose_path_is_really_gone_is_still_a_clean_removal(self) -> None:
        # The other side of sol r8 B2: only a FileNotFoundError from the probe counts as gone, and it must still read as success.
        path = tempfile.mkdtemp(prefix="heavy-slot-gate-gone-")
        try:
            self.assertEqual(_remove_capture_dir(path), "")
            self.assertFalse(os.path.exists(path))
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def _run_with_capture_close(self, on_close, *, timeout_expires: bool = True, close_bound: float | None = None) -> BaseException:
        """run_bounded with in-memory Popen fakes where closing a capture file calls on_close() after really closing it."""
        proc = BoundedRunOutputReadFailureTests._TimedOutProc()
        if not timeout_expires:
            proc.wait = lambda timeout=None: 0  # type: ignore[method-assign]
        real_open = open

        class CaptureFile:
            def __init__(self, handle: object) -> None:
                self.handle = handle

            def fileno(self) -> int:
                return self.handle.fileno()  # type: ignore[attr-defined]

            def close(self) -> None:
                self.handle.close()  # type: ignore[attr-defined]
                on_close()

            def __enter__(self) -> "CaptureFile":
                return self

            def __exit__(self, *exc_info: object) -> None:
                self.close()

        def fake_open(path: object, mode: str = "r", *args: object, **kwargs: object) -> object:
            return CaptureFile(real_open(path, mode, *args, **kwargs))

        popen = BoundedRunOutputReadFailureTests._popen_for(proc, BoundedRunOutputReadFailureTests._FakeTaskkill(0))
        patches = [mock.patch.object(subprocess, "Popen", side_effect=popen),
                   mock.patch.object(sys.modules[__name__], "open", fake_open, create=True)]
        if close_bound is not None:
            patches.append(mock.patch.object(sys.modules[__name__], "CAPTURE_CLOSE_BOUND_SEC", close_bound))
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            try:
                run_bounded(["child"], 3, "close-case")
            except BaseException as exc:  # noqa: BLE001 - the verdict type is the assertion
                return exc
        self.fail("run_bounded returned instead of failing")

    def test_a_capture_file_whose_close_fails_keeps_the_case_named_verdict(self) -> None:
        # adversary r8 A1: `with open(...)` closed the captures synchronously, so an OSError from the close replaced the case-named
        # AssertionError with a bare OSError.
        def failing_close() -> None:
            raise OSError(5, "capture close failed")

        caught = self._run_with_capture_close(failing_close)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("close-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("capture close failed: OSError", str(caught))  # r16: the type only, never the message
        self.assertNotIn("[Errno 5]", str(caught))

    def test_a_capture_close_that_stalls_is_abandoned_inside_its_bound_and_named(self) -> None:
        # adversary r8 A1 (slow variant): two synchronous closes of 300 s each held the verdict 603 s past a 128 s ceiling.
        release = threading.Event()
        started = time.monotonic()
        try:
            caught = self._run_with_capture_close(lambda: release.wait(60), close_bound=0.3)
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 30)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("close-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("capture close abandoned after 0.3 s", str(caught))

    def test_a_capture_close_that_cannot_start_keeps_the_case_named_verdict(self) -> None:
        real_start = threading.Thread.start

        def start(thread: threading.Thread) -> None:
            if thread.name == "close-capture":
                raise RuntimeError("can't start new thread")
            real_start(thread)

        parked_before = len(_PARKED_CAPTURES)
        try:
            with mock.patch.object(threading.Thread, "start", start):
                caught = self._run_with_capture_close(lambda: None)
        finally:
            for handle in _PARKED_CAPTURES[parked_before:]:  # the helper parks unclosable handles for the process; this test releases its own
                handle.close()
            del _PARKED_CAPTURES[parked_before:]
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("close-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("capture close not started (RuntimeError)", str(caught))

    def test_a_capture_close_failure_without_a_timeout_is_a_named_failure_not_a_pass(self) -> None:
        def failing_close() -> None:
            raise OSError(5, "capture close failed")

        caught = self._run_with_capture_close(failing_close, timeout_expires=False)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("close-case", str(caught))
        self.assertIn("capture close failed: OSError", str(caught))  # r16: the type only, never the message
        self.assertNotIn("[Errno 5]", str(caught))

    def test_captures_whose_close_could_not_start_are_never_closed_by_the_garbage_collector(self) -> None:
        # adversary r8 pass-12 A1: with the close thread unstartable the handles were dropped, and clearing the traceback frames finalized
        # them synchronously on the caller's thread (two 300 s closes took the helper 603 s against a 138 s ceiling). They are parked.
        finalized: list[int] = []
        parked_before = len(_PARKED_CAPTURES)

        class Handle:
            def close(self) -> None:
                pass

            def __del__(self) -> None:
                finalized.append(1)

        handles = [Handle(), Handle()]
        real_start = threading.Thread.start

        def start(thread: threading.Thread) -> None:
            if thread.name == "close-capture":
                raise RuntimeError("can't start new thread")
            real_start(thread)

        try:
            with mock.patch.object(threading.Thread, "start", start):
                note = _close_captures(handles)
            del handles
            gc.collect()
            self.assertIn("capture close not started (RuntimeError)", note)
            self.assertEqual(finalized, [], "a parked handle must not be finalized (closed) by the collector")
            self.assertEqual(len(_PARKED_CAPTURES), parked_before + 2)
        finally:
            del _PARKED_CAPTURES[parked_before:]

    def test_a_cleanup_worker_that_dies_is_reported_not_read_as_a_removal(self) -> None:
        # adversary r8 pass-12 A2: an exception out of shutil.rmtree (an audit hook, a patched seam) killed the worker before it recorded
        # an answer, and the caller returned the empty success note.
        path = tempfile.mkdtemp(prefix="heavy-slot-gate-dies-")
        try:
            with mock.patch.object(shutil, "rmtree", side_effect=PermissionError(13, "audit policy denies shutil.rmtree")):
                note = _remove_capture_dir(path)
            self.assertIn("capture cleanup failed (PermissionError)", note)  # r16: the type only, never the message
            self.assertNotIn("audit policy denies", note)
            self.assertIn(path, note)
        finally:
            shutil.rmtree(path, ignore_errors=True)

    def test_a_cleanup_worker_that_ends_without_an_answer_is_not_a_success(self) -> None:
        path = tempfile.mkdtemp(prefix="heavy-slot-gate-silent-")
        try:
            with mock.patch.object(shutil, "rmtree", side_effect=SystemExit(1)), mock.patch.object(threading, "excepthook", lambda args: None):
                note = _remove_capture_dir(path)
            self.assertIn("capture cleanup incomplete", note)
        finally:
            shutil.rmtree(path, ignore_errors=True)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_directory_path_handed_to_the_capture_cleanup_is_reported_as_still_present(self) -> None:
        # adversary r8 pass-12 A4: File.Exists(directory) is false, so a path that is a directory (nothing deleted) read as a clean removal.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$dir = Join-Path ([IO.Path]::GetTempPath()) ('mlv-r8-dir-' + [guid]::NewGuid().ToString('N'))\n"
                  "$null = New-Item -ItemType Directory -Path $dir\n"
                  "Write-Output ('DIR|' + (Remove-CaptureFiles @($dir)))\n"
                  "Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DIR|capture cleanup incomplete: still present: ", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_capture_whose_delete_and_existence_probe_both_fail_is_unconfirmed_not_a_removal(self) -> None:
        # sol r8 B2 (PowerShell half): File.Delete threw (swallowed) and File.Exists / Directory.Exists return False on ANY error, so the
        # helper reported an empty success note. A NUL in the path makes all three behave that way on every platform, without an ACL change.
        # Only "file not found" / "directory not found" from the probe means gone; any other error leaves the removal unconfirmed.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$bad = 'C:\\x\\a' + [char]0 + 'b.tmp'\n"
                  "$real = [IO.Path]::GetTempFileName()\n"
                  "Write-Output ('BAD|' + (Remove-CaptureFiles @($bad)).Replace([string][char]0, '<NUL>'))\n"
                  "Write-Output ('MIXED|' + (Remove-CaptureFiles @($real, $bad)).Replace([string][char]0, '<NUL>') + '|' + (Test-Path -LiteralPath $real))\n"
                  "$gone = Join-Path ([IO.Path]::GetTempPath()) ('mlv-r9-gone-' + [guid]::NewGuid().ToString('N') + '.tmp')\n"
                  "Write-Output ('GONE|[' + (Remove-CaptureFiles @($gone)) + ']')\n"
                  "$noParent = Join-Path ([IO.Path]::GetTempPath()) ('mlv-r9-noparent-' + [guid]::NewGuid().ToString('N') + '\\a.tmp')\n"
                  "Write-Output ('NOPARENT|[' + (Remove-CaptureFiles @($noParent)) + ']')\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"BAD\|capture cleanup incomplete: removal not confirmed: C:\\x\\a<NUL>b\.tmp \(ArgumentException", proc.stdout)
        self.assertRegex(proc.stdout, r"MIXED\|capture cleanup incomplete: removal not confirmed: C:\\x\\a<NUL>b\.tmp \(ArgumentException.*\|False")
        self.assertIn("GONE|[]", proc.stdout, "a file that is really gone is still a clean removal")
        self.assertIn("NOPARENT|[]", proc.stdout, "a path whose parent directory is gone is still a clean removal")

    def test_a_thread_constructor_that_fails_keeps_the_case_named_verdict(self) -> None:
        # adversary r8 A2: threading.Thread(...) itself (MemoryError) ran before the protected start() call, so it escaped.
        real_init = threading.Thread.__init__

        def init(thread: threading.Thread, *args: object, **kwargs: object) -> None:
            if str(kwargs.get("name", "")).startswith(("read-", "rmtree-capture", "close-capture")):
                raise MemoryError("thread constructor allocation failed")
            real_init(thread, *args, **kwargs)

        with mock.patch.object(threading.Thread, "__init__", init):
            caught = self._run_with_capture_close(lambda: None)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("close-case exceeded its 3 s wall-clock bound", str(caught))

    def test_a_finished_cleanup_adds_no_note_to_the_verdict(self) -> None:
        caught = BoundedRunOutputReadFailureTests()._run_failing_cleanup(taskkill=0)
        self.assertNotIn("capture cleanup", str(caught))

    @staticmethod
    def _pwsh(script: str, repeat: int = 1) -> subprocess.CompletedProcess:
        return _run_pwsh_script(script, repeat)

    @unittest.skipUnless(os.name == "nt" and PWSH is not None, "named pipes and pwsh are Windows-only here")
    def test_a_stalled_capture_open_is_abandoned_inside_its_bound_and_named(self) -> None:
        # sol r6 B1: the default reader used to open the file synchronously before its timed wait. Now open + read both run on a pool
        # thread under one bound. A listening pipe nobody writes to blocks the very first read.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$srv = [IO.Pipes.NamedPipeServerStream]::new('mlv-r7-stall-" + str(os.getpid()) + "', [IO.Pipes.PipeDirection]::Out, 1)\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  "$r = Read-CapturedText '\\\\.\\pipe\\mlv-r7-stall-" + str(os.getpid()) + "' 400\n"
                  f"Write-Output ('STALLED|' + $r + '|' + ($sw.ElapsedMilliseconds -lt {capture_call_ceiling_ms(400)}))\n"
                  "$srv.Dispose()\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"STALLED|<read timed out: mlv-r7-stall-{os.getpid()} not readable within 400 ms>|True", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_non_positive_or_oversized_capture_bound_is_refused_before_anything_is_opened(self) -> None:
        # sol r6 B1: BoundMs = -1 is an infinite Task.Wait. Every bound outside 1..CaptureIoMaxMs is refused before any open or delete.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$script:started = 0\n"
                  "$seam = { param($p) $script:started++; [Threading.Tasks.Task]::FromResult('x') }\n"
                  "foreach ($b in -1, 0, -2147483648, 60001) {\n"
                  "  try { $null = Read-CapturedText 'C:\\x\\a.txt' $b $seam; Write-Output \"READ|$b|NOT-REFUSED\" } catch { Write-Output \"READ|$b|$($_.Exception.Message)\" }\n"
                  "  try { $null = Remove-CaptureFiles @('C:\\x\\a.txt') $b $seam; Write-Output \"DEL|$b|NOT-REFUSED\" } catch { Write-Output \"DEL|$b|$($_.Exception.Message)\" }\n"
                  "}\n"
                  "Write-Output \"STARTED|$($script:started)\"\n")
        proc = self._pwsh(script, repeat=4)  # the loop runs the four refused bounds
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = [line.split("|", 2) for line in proc.stdout.splitlines() if line.startswith(("READ|", "DEL|"))]
        self.assertEqual(len(rows), 8, proc.stdout)
        for kind, bound, message in rows:
            self.assertIn(f"bound {bound} ms is outside 1..60000 ms; nothing was opened", message, f"{kind} {bound}")
        self.assertIn("STARTED|0", proc.stdout, "a refused bound must never reach the open or the delete")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_start_seam_that_hands_back_anything_but_one_task_is_a_named_failure_not_a_false_success(self) -> None:
        # adversary r7 A4: `$task = & $Start` kept an array when the seam emitted two tasks, and `$task.Wait($left)` on that array is a
        # truthy array, so two pending tasks read as finished (the cleanup reported success; the reader blocked on .Result).
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$two = { param($p) [Threading.Tasks.TaskCompletionSource[string]]::new().Task; [Threading.Tasks.TaskCompletionSource[string]]::new().Task }\n"
                  "$none = { param($p) }\n"
                  # r15 (sol r14 B3): each seam call starts its own worker process BEFORE its clock, and the factory is dispatched AFTER it, so a
                  # 300 ms operation budget let a correct scheduling delay turn the shape error into a timeout. The budget is the worker's own
                  # start bound (long enough that dispatch cannot outlast it), the error still returns at once, and the ceiling per call is the
                  # derived whole-call ceiling (worker start, the budget, the teardown, slack).
                  f"$budget = $CaptureWorkerStartMs; $limit = {capture_call_ceiling_ms(CAPTURE_WORKER_START_MS)}\n"
                  "$fast = $true; $sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  "Write-Output ('READ-TWO|' + (Read-CapturedText 'C:\\x\\a.txt' $budget $two)); $fast = $fast -and $sw.ElapsedMilliseconds -lt $limit; $sw.Restart()\n"
                  "Write-Output ('READ-NONE|' + (Read-CapturedText 'C:\\x\\a.txt' $budget $none)); $fast = $fast -and $sw.ElapsedMilliseconds -lt $limit; $sw.Restart()\n"
                  "Write-Output ('DEL-TWO|' + (Remove-CaptureFiles @('C:\\x\\a.txt') $budget $two)); $fast = $fast -and $sw.ElapsedMilliseconds -lt $limit; $sw.Restart()\n"
                  "Write-Output ('DEL-NONE|' + (Remove-CaptureFiles @('C:\\x\\a.txt') $budget $none)); $fast = $fast -and $sw.ElapsedMilliseconds -lt $limit\n"
                  "Write-Output ('FAST|' + $fast)\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for label in ("READ-TWO", "READ-NONE"):
            self.assertRegex(proc.stdout, label + r"\|<read failed: .*exactly one Task", label)
        for label in ("DEL-TWO", "DEL-NONE"):
            self.assertRegex(proc.stdout, label + r"\|capture cleanup failed: .*exactly one Task", label)
        self.assertIn("FAST|True", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_stalled_capture_cleanup_is_abandoned_and_named(self) -> None:
        # sol r6 B2 (PowerShell half): a delete that never completes is abandoned after the bound and the note names the files.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$hang = { param($p) [Threading.Tasks.TaskCompletionSource[bool]]::new().Task }\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  f"Write-Output ('STALLED|' + (Remove-CaptureFiles @('C:\\x\\a.txt', 'C:\\x\\b.txt') 300 $hang) + '|' + ($sw.ElapsedMilliseconds -lt {capture_call_ceiling_ms(300)}))\n"
                  "$real = [IO.Path]::GetTempFileName()\n"
                  "Write-Output ('CLEAN|[' + (Remove-CaptureFiles @($real)) + ']|' + (Test-Path -LiteralPath $real))\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STALLED|capture cleanup abandoned after 0.3 s: C:\\x\\a.txt, C:\\x\\b.txt|True", proc.stdout)
        self.assertIn("CLEAN|[]|False", proc.stdout)

    def test_r15_b3_every_ceiling_that_spans_a_worker_start_is_derived_from_the_implementations_own_bounds(self) -> None:
        # sol r14 B3: Start-CaptureWorker permits a CaptureWorkerStartMs start before the step's clock, yet several regressions required the whole
        # call inside a typed 5 s / 10 s / 15 s / 30 s. Source pin (every OS): the derivation reads the script's constants, the start bound is the
        # one Start-CaptureWorker enforces, and no test keeps a typed elapsed ceiling around a Read-Captured* / Remove-CaptureFiles call.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        begin = fifo.index("function Start-CaptureWorker")
        self.assertIn("-ge $CaptureWorkerStartMs", fifo[begin:fifo.index("function Get-CaptureTaskResult", begin)])
        self.assertEqual(capture_call_ceiling_ms(300), CAPTURE_WORKER_START_MS + 300 + 3 * CAPTURE_WORKER_KILL_MS + 2 * CAPTURE_WORKER_REAP_MS + CLEANUP_MARGIN_SEC * 1000)
        self.assertGreater(capture_call_ceiling_ms(1), CAPTURE_WORKER_START_MS, "the ceiling must allow a worker start that takes its full bound")
        self.assertGreater(BoundedCaptureIoTests._NEVER_ENDING_PROCESS_CEILING_SEC * 1000, CAPTURE_WORKER_START_MS)
        self.assertGreater(BoundedCaptureIoTests._SLOW_OPERATION_MS, BoundedCaptureIoTests._SLOW_CEILING_MS,
                           "the slow operation must outlast the ceiling, or a call that waited for it would still pass")
        source = Path(__file__).read_text(encoding="utf-8")
        offenders = []
        for chunk in re.split(r"\n    (?:@unittest[^\n]*\n    )?def ", source):
            name = chunk.split("(", 1)[0]
            if name.startswith("test_r15_b3_every_ceiling"):
                continue
            for line in chunk.splitlines():
                # the sleeper-death polls and the readiness poll run after (or apart from) a worker start; they are not call ceilings
                if re.search(r"ElapsedMilliseconds -lt \d+", line) and "Get-Process" not in line and "Exists(" not in line:
                    offenders.append(f"{name}: {line.strip()[:100]}")
        self.assertEqual(offenders, [], "a typed elapsed ceiling spans a worker start; derive it with capture_call_ceiling_ms")

    # r16 (Dot C1, adversary r15 A3): the WHOLE-script budget. _run_pwsh_script capped every script at a typed 120 s, but the transport case
    # makes 18 worker calls and each may spend CaptureWorkerStartMs (30 s) before its step's clock, so permitted starts alone exceed it.
    def test_r16_c1_the_whole_script_budget_is_derived_from_the_calls_the_script_makes(self) -> None:
        region = "function Read-CapturedResult { }\n# END bounded capture io\n"
        self.assertEqual(pwsh_script_budget_sec("Write-Output 'no worker call'"), PWSH_SCRIPT_BASE_SEC)
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult $p 300 $f\n"),
                         PWSH_SCRIPT_BASE_SEC + capture_call_ceiling_ms(300) / 1000, "the definition is not a call; the call is charged its own ceiling")
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedText $p $b $f\n"),
                         PWSH_SCRIPT_BASE_SEC + capture_call_ceiling_ms(CAPTURE_WORKER_START_MS) / 1000, "a variable bound is charged the worker start bound")
        looped = region + "foreach ($t in $texts) {\n  $r = Read-CapturedResult $p 10000 $ok\n  $n = Remove-CaptureFiles @($p) 10000 $ok\n}\n$z = Read-CapturedResult $p 50 $f\n"
        with self.assertRaises(AssertionError, msg="calls inside a loop must declare how often the loop runs"):
            pwsh_script_budget_sec(looped)
        self.assertEqual(pwsh_script_budget_sec(looped, repeat=6),
                         PWSH_SCRIPT_BASE_SEC + (6 * 2 * capture_call_ceiling_ms(10000) + capture_call_ceiling_ms(50)) / 1000)
        outside = region + "$a = Read-CapturedResult $p 50 $f\nforeach ($f in $files) { Stop-Process -Id $f }\n"
        self.assertEqual(pwsh_script_budget_sec(outside), PWSH_SCRIPT_BASE_SEC + capture_call_ceiling_ms(50) / 1000, "a loop with no call in it needs no repeat")
        # The failing shape: 18 calls that each take a PERMITTED worker start. The old typed 120 s could not hold even four of them.
        permitted_starts_sec = 18 * CAPTURE_WORKER_START_MS / 1000
        self.assertGreater(permitted_starts_sec, 120, "the old fixed cap could not hold the permitted starts of the transport case")
        self.assertGreater(pwsh_script_budget_sec(looped, repeat=6), PWSH_SCRIPT_BASE_SEC + 6 * 2 * CAPTURE_WORKER_START_MS / 1000)
        # No call site may bring its own typed timeout back.
        source = Path(__file__).read_text(encoding="utf-8")
        runner = source[source.index("def _run_pwsh_script("):source.index("def _fifo_capture_io_region(")]
        self.assertNotIn("timeout", runner.split('"""')[0], "the runner takes no typed whole-script timeout")
        self.assertIn("run_bounded(argv, budget, _case_name())", runner, "a whole script runs under run_bounded: a hang kills the tree and fails by case")
        self.assertNotIn("subprocess.run(", runner)
        self.assertEqual(re.findall(r"_run_pwsh_script\([^)\n]*timeout", source), [], "a case passes a typed whole-script timeout")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_r16_c1_a_script_whose_permitted_slow_worker_starts_outlast_the_old_fixed_cap_still_passes(self) -> None:
        # Real pwsh, scaled: the typed base is patched down to 4 s (standing in for the old fixed cap), and every worker start is delayed
        # 2 s (a PERMITTED delay: far under CaptureWorkerStartMs). Three calls then run longer than that base, so a fixed cap of that size
        # fails this script; the derived budget adds the three calls' own ceilings and passes it.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "# END bounded capture io\n"
                  "$real = (Get-Command Start-CaptureWorker -CommandType Function).ScriptBlock\n"
                  "function Start-CaptureWorker { Start-Sleep -Milliseconds 2000; & $real }\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$f = { param($p) [Threading.Tasks.Task]::FromResult('ok') }\n"
                  "$r = Read-CapturedResult $p 5000 $f; Write-Output ('ONE|' + $r.Ok + '|' + $r.Text)\n"
                  "$r = Read-CapturedResult $p 5000 $f; Write-Output ('TWO|' + $r.Ok + '|' + $r.Text)\n"
                  "$r = Read-CapturedResult $p 5000 $f; Write-Output ('THREE|' + $r.Ok + '|' + $r.Text)\n")
        base = 4
        started = time.monotonic()
        with mock.patch(f"{__name__}.PWSH_SCRIPT_BASE_SEC", base):
            proc = _run_pwsh_script(script)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for label in ("ONE", "TWO", "THREE"):
            self.assertIn(f"{label}|True|ok", proc.stdout, proc.stdout + proc.stderr)
        self.assertGreater(elapsed, base, "the script must outlast the typed base, or this case no longer shows what a fixed cap does")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_r16_c1_a_genuine_hang_still_fails_by_case_name_at_the_budget(self) -> None:
        # The verdict is case-named either way. The kill note depends on the host: taskkill exists only on Windows, so on Ubuntu the
        # note is "kill is NOT confirmed (taskkill failed: FileNotFoundError)" (hosted run 42004196485). The second pass makes the
        # Windows host take that same path, so both notes are exercised everywhere.
        real_popen = subprocess.Popen

        def without_taskkill(argv: object, *args: object, **kwargs: object) -> object:
            if isinstance(argv, (list, tuple)) and argv and str(argv[0]).lower() == "taskkill":
                raise FileNotFoundError(2, "taskkill is not installed")
            return real_popen(argv, *args, **kwargs)

        for label, patch in (("host taskkill", mock.patch.object(subprocess, "Popen", real_popen)),
                             ("taskkill missing", mock.patch.object(subprocess, "Popen", without_taskkill))):
            with self.subTest(label), patch:
                with mock.patch(f"{__name__}.PWSH_SCRIPT_BASE_SEC", 3):
                    with self.assertRaises(AssertionError) as caught:
                        _run_pwsh_script("Start-Sleep -Seconds 600\n")
                message = str(caught.exception)
                self.assertIn("test_r16_c1_a_genuine_hang_still_fails_by_case_name_at_the_budget exceeded its 3 s wall-clock bound", message)
                self.assertTrue("its process tree was killed" in message
                                or "its process tree kill is NOT confirmed (taskkill failed: FileNotFoundError)" in message, message)
                if label == "taskkill missing":
                    self.assertIn("kill is NOT confirmed (taskkill failed: FileNotFoundError)", message)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_factory_that_consumes_the_bound_before_returning_a_completed_task_is_a_timeout_not_a_late_success(self) -> None:
        # sol r9 B1 (catalogue B-01): an expired budget used to become Wait(0), and an already-completed task then returned success even
        # though its factory had spent the whole bound. The deadline is measured from the call's start: elapsed >= bound is a timeout.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"   # native path: a drive-letter literal is not split on Linux (r11)
                  "$slowText = { param($p) Start-Sleep -Milliseconds 300; [Threading.Tasks.Task]::FromResult('payload') }\n"
                  "$slowDel = { param($p) Start-Sleep -Milliseconds 300; [Threading.Tasks.Task]::FromResult('') }\n"
                  "$fastText = { param($p) [Threading.Tasks.Task]::FromResult('payload') }\n"
                  "$fastDel = { param($p) [Threading.Tasks.Task]::FromResult('') }\n"
                  "$r = Read-CapturedResult $p 50 $slowText\n"
                  "Write-Output ('LATE-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('LATE-DEL|' + (Remove-CaptureFiles @('C:\\x\\a.txt') 50 $slowDel))\n"
                  "$r = Read-CapturedResult $p 5000 $fastText\n"
                  "Write-Output ('FAST-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('FAST-DEL|[' + (Remove-CaptureFiles @('C:\\x\\a.txt') 5000 $fastDel) + ']')\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("LATE-READ|False|<read timed out: a.txt not readable within 50 ms>", proc.stdout)
        self.assertIn("LATE-DEL|capture cleanup abandoned after 0.05 s: C:\\x\\a.txt", proc.stdout)
        self.assertIn("FAST-READ|True|payload", proc.stdout, "a task that finishes inside its bound still succeeds")
        self.assertIn("FAST-DEL|[]", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_the_deadline_is_checked_after_the_result_is_converted_not_before(self) -> None:
        # adversary r10 A1 (major): the elapsed check ran BEFORE the completed task's result was turned into text, so a result whose
        # conversion spent the bound (a 200000-element array took 286 ms under a 50 ms bound) still returned Ok / an "incomplete" note.
        # The slow ToString makes the conversion cost deterministic. The check is the LAST step before a success is returned.
        # r13 (sol r12 B1): the factory and the conversion run in a worker PROCESS, so the slow type is defined inside the factory itself
        # (a PowerShell class: no compile, and a type of the calling process is not visible in the worker).
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"   # native path (r11): see the leaf-name guard test
                  "$slow = { param($p) class SlowText { [string] ToString() { [Threading.Thread]::Sleep(300); return 'payload' } }; "
                  "[Threading.Tasks.Task]::FromResult([object][SlowText]::new()) }\n"
                  "$r = Read-CapturedResult $p 100 $slow\n"
                  "Write-Output ('SLOW-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('SLOW-DEL|' + (Remove-CaptureFiles @('C:\\x\\a.txt') 100 $slow))\n"
                  "$r = Read-CapturedResult $p 5000 $slow\n"
                  "Write-Output ('INSIDE-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('INSIDE-DEL|' + (Remove-CaptureFiles @('C:\\x\\a.txt') 5000 $slow))\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("SLOW-READ|False|<read timed out: a.txt not readable within 100 ms>", proc.stdout)
        self.assertIn("SLOW-DEL|capture cleanup abandoned after 0.1 s: C:\\x\\a.txt", proc.stdout)
        self.assertIn("INSIDE-READ|True|payload", proc.stdout, "a conversion that fits inside the bound still succeeds")
        self.assertIn("INSIDE-DEL|capture cleanup incomplete: payload", proc.stdout)

    def test_a_test_that_asserts_a_leaf_name_never_feeds_the_reader_a_windows_literal_path(self) -> None:
        # CI-FLAKE-HEAVY-SLOT-GATE-UNIT-HOSTED-HANG-1 r11: [IO.Path]::GetFileName('C:\x\a.txt') is 'a.txt' on Windows but the WHOLE string on
        # Linux, where a backslash is not a separator, so hosted ubuntu shard 3/4 went red on two r10 tests that asserted the leaf. The reader
        # is right for native paths; a fixture whose assertion names the leaf must build its path natively (Join-Path) on every OS.
        source = Path(__file__).read_text(encoding="utf-8")
        offenders = []
        for chunk in re.split(r"\n    (?:@unittest[^\n]*\n    )?def ", source):
            name = chunk.split("(", 1)[0]
            if name.startswith("test_a_test_that_asserts_a_leaf_name"):
                continue
            if "<read timed out: " in chunk and re.search(r"Read-Captured(?:Result|Text) '[A-Za-z]:\\\\", chunk):
                offenders.append(name)
        self.assertEqual(offenders, [], "a leaf-asserting test feeds Read-Captured* a drive-letter literal; use a Join-Path temp path")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_an_accepted_plain_completed_task_has_no_result_so_no_false_cleanup_problem_and_no_fabricated_text(self) -> None:
        # sol r9 B2: a plain [Task]::CompletedTask exposes the runtime type System.Threading.Tasks.VoidTaskResult as its Result. A non-generic
        # Task has no result: cleanup reports no problem and the reader returns empty text, never the type name.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$plain = { param($p) [Threading.Tasks.Task]::CompletedTask }\n"
                  "$ran = { param($p) [Threading.Tasks.Task]::Delay(1) }\n"
                  "Write-Output ('PLAIN-DEL|[' + (Remove-CaptureFiles @('C:\\x\\a.txt') 5000 $plain) + ']')\n"
                  "Write-Output ('RUN-DEL|[' + (Remove-CaptureFiles @('C:\\x\\a.txt') 5000 $ran) + ']')\n"
                  "$r = Read-CapturedResult 'C:\\x\\a.txt' 5000 $plain\n"
                  "Write-Output ('PLAIN-READ|' + $r.Ok + '|[' + $r.Text + ']')\n"
                  "$r = Read-CapturedResult 'C:\\x\\a.txt' 5000 $ran\n"
                  "Write-Output ('RUN-READ|' + $r.Ok + '|[' + $r.Text + ']')\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("VoidTaskResult", proc.stdout)
        self.assertIn("PLAIN-DEL|[]", proc.stdout)
        self.assertIn("RUN-DEL|[]", proc.stdout)
        self.assertIn("PLAIN-READ|True|[]", proc.stdout)
        self.assertIn("RUN-READ|True|[]", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_task_that_derives_from_task_of_string_keeps_its_result_in_both_callers(self) -> None:
        # sol r11 B1: Get-CaptureTaskResult asked the RUNTIME type whether it is generic. A class derived from Task<string> is not
        # (IsGenericType is False) yet carries a real result, so the reader returned Ok with empty text and the cleanup reported success
        # over 'still present: a.txt'. The generic base is now found by walking the BaseType chain. The ordinary Task<string> is the control.
        # r13 (sol r12 B1): the derived type is defined inside each factory (the factory runs in a worker process of its own).
        derived_type = "class DerivedTextTask : System.Threading.Tasks.Task[string] { DerivedTextTask([Func[string]]$f) : base($f) {} }; "
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$derived = { param($p) " + derived_type + "$t = [DerivedTextTask]::new([Func[string]]{ 'payload' }); $t.RunSynchronously(); $t }\n"
                  "$derivedDel = { param($p) " + derived_type + "$t = [DerivedTextTask]::new([Func[string]]{ 'still present: a.txt' }); $t.RunSynchronously(); $t }\n"
                  "$plain = { param($p) [Threading.Tasks.Task]::FromResult('payload') }\n"
                  "$plainDel = { param($p) [Threading.Tasks.Task]::FromResult('still present: a.txt') }\n"
                  "Write-Output ('DERIVED-TYPE|' + (& $derived $p).GetType().IsGenericType)\n"
                  "$r = Read-CapturedResult $p 5000 $derived\n"
                  "Write-Output ('DERIVED-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('DERIVED-DEL|' + (Remove-CaptureFiles @($p) 5000 $derivedDel))\n"
                  "$r = Read-CapturedResult $p 5000 $plain\n"
                  "Write-Output ('PLAIN-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('PLAIN-DEL|' + (Remove-CaptureFiles @($p) 5000 $plainDel))\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DERIVED-TYPE|False", proc.stdout, "the repro must really be a non-generic runtime type")
        self.assertIn("DERIVED-READ|True|payload", proc.stdout)
        self.assertIn("DERIVED-DEL|capture cleanup incomplete: still present: a.txt", proc.stdout)
        self.assertIn("PLAIN-READ|True|payload", proc.stdout, "control: the ordinary Task<string>")
        self.assertIn("PLAIN-DEL|capture cleanup incomplete: still present: a.txt", proc.stdout, "control: the ordinary Task<string>")

    # sol r11 B2: a synchronous factory, and the synchronous conversion of the result, used to run on the caller's thread, so the elapsed
    # check classified an expiry only AFTER they returned (a 1 ms bound returned after 386 ms; Thread.Sleep(Infinite) never returned).
    # r13 (sol r12 B1): each call now also starts a worker process before its clock, and tears it down after the verdict.
    # r15 (sol r14 B3): the ceiling is the DERIVED whole-call ceiling (worker start bound + the 50 ms budget + teardown bounds + slack), not
    # a fixed 10 s that the implementation's own 30 s start bound contradicts; the operation outlasts that ceiling by a full margin, so a
    # call that waited for the factory or the conversion instead of its bound still fails. The slow type is a PowerShell class defined
    # inside the factory, because the factory runs in the worker process.
    _SLOW_BOUND_MS = 50
    _SLOW_CEILING_MS = capture_call_ceiling_ms(_SLOW_BOUND_MS)
    _SLOW_OPERATION_MS = _SLOW_CEILING_MS + CAPTURE_CEILING_SLACK_MS
    _SLOW_TEXT_TYPE = ("class SlowText { [string] ToString() { [Threading.Thread]::Sleep(" + str(_SLOW_OPERATION_MS) + "); return 'payload' } }; ")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_synchronous_factory_or_result_conversion_that_outlasts_the_bound_returns_the_named_timeout_inside_a_generous_ceiling(self) -> None:
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  f"$slowFactory = {{ param($p) [Threading.Thread]::Sleep({self._SLOW_OPERATION_MS}); [Threading.Tasks.Task]::FromResult('payload') }}\n"
                  f"$slowFactoryDel = {{ param($p) [Threading.Thread]::Sleep({self._SLOW_OPERATION_MS}); [Threading.Tasks.Task]::FromResult('') }}\n"
                  "$slowConv = { param($p) " + self._SLOW_TEXT_TYPE + "[Threading.Tasks.Task]::FromResult([object][SlowText]::new()) }\n"
                  f"$ceiling = {self._SLOW_CEILING_MS}\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  "$r = Read-CapturedResult $p 50 $slowFactory\n"
                  "Write-Output ('FACTORY-READ|' + $r.Ok + '|' + $r.Text + '|' + ($sw.ElapsedMilliseconds -lt $ceiling))\n"
                  "$sw.Restart(); $n = Remove-CaptureFiles @($p) 50 $slowFactoryDel\n"
                  "Write-Output ('FACTORY-DEL|' + $n + '|' + ($sw.ElapsedMilliseconds -lt $ceiling))\n"
                  "$sw.Restart(); $r = Read-CapturedResult $p 50 $slowConv\n"
                  "Write-Output ('CONV-READ|' + $r.Ok + '|' + $r.Text + '|' + ($sw.ElapsedMilliseconds -lt $ceiling))\n"
                  "$sw.Restart(); $n = Remove-CaptureFiles @($p) 50 $slowConv\n"
                  "Write-Output ('CONV-DEL|' + $n + '|' + ($sw.ElapsedMilliseconds -lt $ceiling))\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("FACTORY-READ|False|<read timed out: a.txt not readable within 50 ms>|True", proc.stdout)
        self.assertIn("CONV-READ|False|<read timed out: a.txt not readable within 50 ms>|True", proc.stdout)
        self.assertRegex(proc.stdout, r"FACTORY-DEL\|capture cleanup abandoned after 0\.05 s: .*a\.txt\|True")
        self.assertRegex(proc.stdout, r"CONV-DEL\|capture cleanup abandoned after 0\.05 s: .*a\.txt\|True")

    # sol r12 B1: an accepted factory, or a result conversion, that NEVER returns. r12 ran it in an in-process runspace: the helper returned
    # its named timeout, but the abandoned runspace stayed open inside ps.Invoke and ordinary pwsh shutdown (which disposes every open
    # runspace and stops its pipeline) blocked forever; the r12 test hid that with [Environment]::Exit(0). Each case is now a child pwsh that
    # runs ONE caller and then simply reaches the end of its script. It must print the named timeout AND end on its own inside the ceiling;
    # run_bounded kills it and fails the case by name if it does not. No forced exit of any kind is in the script.
    # r15 (sol r14 B3): the whole child pwsh (its own start, the compile of the capture type, ONE call, its end) is bounded by the derived
    # whole-call ceiling for the 200 ms budget plus the named cleanup margin, not by a fixed 30 s that the 30 s worker start bound contradicts.
    _NEVER_ENDING_BOUND_MS = 200
    _NEVER_ENDING_CALL_CEILING_MS = capture_call_ceiling_ms(_NEVER_ENDING_BOUND_MS)
    _NEVER_ENDING_PROCESS_CEILING_SEC = math.ceil(_NEVER_ENDING_CALL_CEILING_MS / 1000) + CLEANUP_MARGIN_SEC
    _FOREVER_FACTORY = "{ param($p) [Threading.Thread]::Sleep([Threading.Timeout]::Infinite); [Threading.Tasks.Task]::FromResult('x') }"
    _FOREVER_CONVERSION = ("{ param($p) class ForeverText { [string] ToString() { [Threading.Thread]::Sleep([Threading.Timeout]::Infinite); return 'x' } }; "
                           "[Threading.Tasks.Task]::FromResult([object][ForeverText]::new()) }")

    def _never_ending_case(self, case: str, call: str, factory: str) -> str:
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  f"$forever = {factory}\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  f"{call}\n"
                  f"Write-Output ('ELAPSED-OK|' + ($sw.ElapsedMilliseconds -lt {self._NEVER_ENDING_CALL_CEILING_MS}))\n")
        code_lines = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith(("#", "//")))
        self.assertNotRegex(code_lines, _FORCED_EXIT, "the proof must end its process the ordinary way")
        with _pwsh_script_argv(script) as argv:
            code, out, err = run_bounded(argv, self._NEVER_ENDING_PROCESS_CEILING_SEC, case)
        self.assertEqual(code, 0, out + err)
        self.assertIn("ELAPSED-OK|True", out, out + err)
        return out

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_factory_that_never_returns_still_gets_the_named_timeout_and_the_process_still_exits(self) -> None:
        out = self._never_ending_case("forever-factory-reader", "$r = Read-CapturedResult $p 200 $forever; Write-Output ('FOREVER-READ|' + $r.Ok + '|' + $r.Text)",
                                      self._FOREVER_FACTORY)
        self.assertIn("FOREVER-READ|False|<read timed out: a.txt not readable within 200 ms>", out)
        out = self._never_ending_case("forever-factory-cleanup", "Write-Output ('FOREVER-DEL|' + (Remove-CaptureFiles @($p) 200 $forever))",
                                      self._FOREVER_FACTORY)
        self.assertRegex(out, r"FOREVER-DEL\|capture cleanup abandoned after 0\.2 s: .*a\.txt")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_result_conversion_that_never_returns_still_gets_the_named_timeout_and_the_process_still_exits(self) -> None:
        out = self._never_ending_case("forever-conversion-reader", "$r = Read-CapturedResult $p 200 $forever; Write-Output ('FOREVER-READ|' + $r.Ok + '|' + $r.Text)",
                                      self._FOREVER_CONVERSION)
        self.assertIn("FOREVER-READ|False|<read timed out: a.txt not readable within 200 ms>", out)
        out = self._never_ending_case("forever-conversion-cleanup", "Write-Output ('FOREVER-DEL|' + (Remove-CaptureFiles @($p) 200 $forever))",
                                      self._FOREVER_CONVERSION)
        self.assertRegex(out, r"FOREVER-DEL\|capture cleanup abandoned after 0\.2 s: .*a\.txt")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_the_worker_hands_back_every_text_and_every_error_message_exactly(self) -> None:
        # adversary r13 A1: the r13 worker's JSON transport turned an ISO date string into a DateTime, so '2026-11-04T12:00:00Z' came back as
        # '11/04/2026 12:00:00' with Ok=True. Each text is rebuilt in the factory from its code units (the factory is self-contained), and
        # must come back code unit for code unit through the reader's text, the reader's failure and the cleanup's note.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$texts = @('2026-11-04T12:00:00Z', '{\"a\":1}', ' padded ', 'true', ('nul' + [char]0 + 'x'), ([string][char]0x00e9 + [char]0xD800 + 'z'))\n"
                  "$i = 0\n"
                  "foreach ($t in $texts) {\n"
                  "  $units = ($t.ToCharArray() | ForEach-Object { [int]$_ }) -join ','\n"
                  "  $make = '[string]::new([char[]]@(' + $units + '))'\n"
                  "  $ok = [scriptblock]::Create('param($p) [Threading.Tasks.Task]::FromResult(' + $make + ')')\n"
                  "  $bad = [scriptblock]::Create('param($p) throw ' + $make)\n"
                  "  $r = Read-CapturedResult $p 10000 $ok\n"
                  "  Write-Output (\"TEXT|$i|\" + ($r.Ok -and $r.Text -ceq $t))\n"
                  "  $r = Read-CapturedResult $p 10000 $bad\n"
                  "  Write-Output (\"ERROR|$i|\" + ((-not $r.Ok) -and $r.Text -ceq \"<read failed: $t>\"))\n"
                  "  Write-Output (\"NOTE|$i|\" + ((Remove-CaptureFiles @($p) 10000 $ok) -ceq \"capture cleanup incomplete: $t\"))\n"
                  "  $i++\n"
                  "}\n")
        proc = self._pwsh(script, repeat=6)  # the loop runs the six texts: 18 worker calls in all
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for kind in ("TEXT", "ERROR", "NOTE"):
            for i in range(6):
                self.assertIn(f"{kind}|{i}|True", proc.stdout, f"{kind} {i}\n{proc.stdout}{proc.stderr}")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_the_worker_hands_the_factory_its_argument_in_the_shape_it_was_given(self) -> None:
        # adversary r13 A2: a one-element string[] reached the cleanup factory as a String (so $paths[0] was 'C'), an empty one as $null.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$shape = { param($x) [Threading.Tasks.Task]::FromResult('type=' + $x.GetType().FullName + ' n=' + @($x).Count + ' items=' + ($x -join '|')) }\n"
                  "$none = { param($x) [Threading.Tasks.Task]::FromResult('null=' + ($null -eq $x) + ' type=' + $(if ($null -ne $x) { $x.GetType().FullName })) }\n"
                  "Write-Output ('ONE|' + (Remove-CaptureFiles @('C:\\x\\a.txt') 5000 $shape))\n"
                  "Write-Output ('TWO|' + (Remove-CaptureFiles @('C:\\x\\a.txt', 'C:\\x\\b.txt') 5000 $shape))\n"
                  "Write-Output ('EMPTY|' + (Remove-CaptureFiles @() 5000 $none))\n"
                  "Write-Output ('SCALAR|' + (Read-CapturedResult $p 5000 $shape).Text)\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ONE|capture cleanup incomplete: type=System.String[] n=1 items=C:\\x\\a.txt", proc.stdout)
        self.assertIn("TWO|capture cleanup incomplete: type=System.String[] n=2 items=C:\\x\\a.txt|C:\\x\\b.txt", proc.stdout)
        self.assertIn("EMPTY|capture cleanup incomplete: null=False type=System.String[]", proc.stdout)
        self.assertRegex(proc.stdout, r"SCALAR\|type=System\.String n=1 items=.*a\.txt")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_null_element_of_the_argument_reaches_the_factory_as_null(self) -> None:
        # adversary r13 pass-2 A1: the wire kept the null ('-'), but the worker's indexed assignment into its string[] turned it into ''.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$probe = { param($x) [Threading.Tasks.Task]::FromResult('null0=' + ($null -eq $x[0]) + ' empty1=' + ($x[1] -ceq '') + ' n=' + $x.Count) }\n"
                  "$paths = [string[]]::new(2); $paths[1] = ''\n"
                  "Write-Output ('NULLS|' + (Remove-CaptureFiles $paths 5000 $probe))\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("NULLS|capture cleanup incomplete: null0=True empty1=True n=2", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_process_orphaned_by_an_ended_intermediate_still_ends_with_the_worker(self) -> None:
        # adversary r13 pass-2 A2: the factory runs an intermediate pwsh that starts a 600 s sleeper and ends, then hands back a finished
        # task. The sleeper's parent is gone, so Kill(true) (which walks current parent links) could not reach it. Windows: the worker's job
        # object holds it; POSIX: the worker's process group does.
        pid_dir = tempfile.mkdtemp(prefix="heavy-slot-gate-a2b-")
        try:
            pid_file = os.path.join(pid_dir, "orphan.pid")
            inner = ("$c = Start-Process -FilePath (Get-Process -Id $PID).Path -NoNewWindow -PassThru "
                     "-ArgumentList @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 600'); "
                     "[IO.File]::WriteAllText('" + pid_file.replace("'", "''") + "', [string]$c.Id)")
            encoded = base64.b64encode(inner.encode("utf-16-le")).decode("ascii")
            script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                      "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                      "$orphaning = { param($p) $m = Start-Process -FilePath (Get-Process -Id $PID).Path -NoNewWindow -PassThru "
                      "-ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', '" + encoded + "'); "
                      "$null = $m.WaitForExit(30000); [Threading.Tasks.Task]::FromResult('orphaned') }\n"
                      "$r = Read-CapturedResult $p 40000 $orphaning\n"
                      "Write-Output ('ORPHAN-READ|' + $r.Ok + '|' + $r.Text)\n"
                      "$id = [int][IO.File]::ReadAllText('" + pid_file.replace("'", "''") + "'); $sw = [Diagnostics.Stopwatch]::StartNew()\n"
                      "while ($null -ne (Get-Process -Id $id -ErrorAction SilentlyContinue) -and $sw.ElapsedMilliseconds -lt 5000) { Start-Sleep -Milliseconds 100 }\n"
                      "$alive = $null -ne (Get-Process -Id $id -ErrorAction SilentlyContinue)\n"
                      "Write-Output (\"ALIVE|$alive\")\n"
                      "if ($alive) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }\n")
            proc = self._pwsh(script)
        finally:
            shutil.rmtree(pid_dir, ignore_errors=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ORPHAN-READ|True|orphaned", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("ALIVE|False", proc.stdout, proc.stdout + proc.stderr)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_process_the_factory_started_ends_with_the_worker_even_when_the_worker_ended_first(self) -> None:
        # adversary r13 A3: teardown skipped the tree kill once the worker root had ended, so a process the factory launched before throwing (or
        # before handing back a finished task) kept running. Each factory starts a 600 s sleeper and records its pid; after the helper returns,
        # the sleeper must be gone (Windows: Kill(true) through the held handle; POSIX: the worker's own process group).
        pid_dir = tempfile.mkdtemp(prefix="heavy-slot-gate-a3-")
        try:
            pid_files = [os.path.join(pid_dir, name) for name in ("throw.pid", "finish.pid")]

            def spawner(pid_file: str, tail: str) -> str:
                quoted = pid_file.replace("'", "''")
                return ("{ param($p) $c = Start-Process -FilePath (Get-Process -Id $PID).Path -NoNewWindow -PassThru "
                        "-ArgumentList @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 600'); "
                        f"[IO.File]::WriteAllText('{quoted}', [string]$c.Id); {tail} }}")

            script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                      "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                      "$throwing = " + spawner(pid_files[0], "throw 'factory exploded'") + "\n"
                      "$finishing = " + spawner(pid_files[1], "[Threading.Tasks.Task]::FromResult('')") + "\n"
                      "$r = Read-CapturedResult $p 20000 $throwing\n"
                      "Write-Output ('THROW-READ|' + $r.Ok + '|' + $r.Text)\n"
                      "Write-Output ('FINISH-DEL|[' + (Remove-CaptureFiles @($p) 20000 $finishing) + ']')\n"
                      "foreach ($f in @(" + ", ".join("'" + f.replace("'", "''") + "'" for f in pid_files) + ")) {\n"
                      "  $id = [int][IO.File]::ReadAllText($f); $sw = [Diagnostics.Stopwatch]::StartNew()\n"
                      "  while ($null -ne (Get-Process -Id $id -ErrorAction SilentlyContinue) -and $sw.ElapsedMilliseconds -lt 5000) { Start-Sleep -Milliseconds 100 }\n"
                      "  $alive = $null -ne (Get-Process -Id $id -ErrorAction SilentlyContinue)\n"
                      "  Write-Output (\"ALIVE|$([IO.Path]::GetFileName($f))|$alive\")\n"
                      "  if ($alive) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }\n"
                      "}\n")
            proc = self._pwsh(script)
        finally:
            shutil.rmtree(pid_dir, ignore_errors=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("THROW-READ|False|<read failed: factory exploded>", proc.stdout)
        self.assertIn("FINISH-DEL|[]", proc.stdout)
        self.assertIn("ALIVE|throw.pid|False", proc.stdout, proc.stdout + proc.stderr)
        self.assertIn("ALIVE|finish.pid|False", proc.stdout, proc.stdout + proc.stderr)

    def test_the_bounded_capture_region_never_forces_its_process_to_exit(self) -> None:
        # sol r12 B1 acceptance: the fix must not be a forced exit in the helpers either.
        region = _fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii"))
        code_lines = "\n".join(line for line in region.splitlines() if not line.lstrip().startswith(("#", "//")))
        self.assertNotRegex(code_lines, _FORCED_EXIT)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_factory_the_worker_cannot_run_is_a_named_failure_not_a_hang_or_a_success(self) -> None:
        # r12/r13: the factory runs in its own worker process, so it must be self-contained. A variable of the calling scope, a missing type and a
        # throw are all named failures (the same <read failed: ...> / cleanup failed note), never a hang or a success.
        script = ("$callerOnly = 'x'\n" + _fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$callerClass = { param($p) if ($null -eq $callerOnly) { throw 'the caller scope is not visible in the worker' }; [Threading.Tasks.Task]::FromResult('x') }\n"
                  "$noType = { param($p) [NoSuchTypeAnywhere12]::new(); [Threading.Tasks.Task]::FromResult('x') }\n"
                  "$boom = { param($p) throw 'factory exploded' }\n"
                  "$r = Read-CapturedResult $p 5000 $callerClass\n"
                  "Write-Output ('CLASS-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('CLASS-DEL|' + (Remove-CaptureFiles @($p) 5000 $callerClass))\n"
                  "$r = Read-CapturedResult $p 5000 $noType\n"
                  "Write-Output ('NOTYPE-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('NOTYPE-DEL|' + (Remove-CaptureFiles @($p) 5000 $noType))\n"
                  "$r = Read-CapturedResult $p 5000 $boom\n"
                  "Write-Output ('BOOM-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('BOOM-DEL|' + (Remove-CaptureFiles @($p) 5000 $boom))\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("CLASS-READ|False|<read failed: the caller scope is not visible in the worker>", proc.stdout)
        self.assertIn("CLASS-DEL|capture cleanup failed: the caller scope is not visible in the worker: ", proc.stdout)
        self.assertIn("NOTYPE-READ|False|<read failed: ", proc.stdout)
        self.assertIn("NOTYPE-DEL|capture cleanup failed: ", proc.stdout)
        self.assertIn("BOOM-READ|False|<read failed: factory exploded>", proc.stdout)
        self.assertIn("BOOM-DEL|capture cleanup failed: factory exploded: ", proc.stdout)

    @unittest.skipUnless(os.name == "nt" and PWSH is not None,
                         "a read-only attribute blocks File.Delete only on Windows; on POSIX the delete succeeds and nothing is left behind")
    def test_a_capture_file_that_cannot_be_deleted_is_reported_as_still_present(self) -> None:
        # adversary r7 A3: a delete that fails (a read-only or held file) is reported, not read as a success. Split out of the STALLED/CLEAN test
        # (hosted ubuntu CI, run 41485699571): the ReadOnly attribute does not stop a POSIX unlink, so the case printed `STUCK||False` there
        # and the teardown threw on the already-removed file. The teardown now also tolerates a file that is gone.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$stuck = [IO.Path]::GetTempFileName(); [IO.File]::SetAttributes($stuck, [IO.FileAttributes]::ReadOnly)\n"
                  "Write-Output ('STUCK|' + (Remove-CaptureFiles @($stuck)) + '|' + (Test-Path -LiteralPath $stuck))\n"
                  "if (Test-Path -LiteralPath $stuck) { [IO.File]::SetAttributes($stuck, [IO.FileAttributes]::Normal); Remove-Item -LiteralPath $stuck -Force }\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"STUCK\|capture cleanup incomplete: still present: .+\.tmp\|True")

    @staticmethod
    def _suite_stub_script() -> str:
        """Invoke-BoundedSuite lifted out of the fifo script, with Start-Process, the bounded readers and the bounded removal stubbed (no
        child, no file). $script:finish decides whether the suite exits; $script:stallReads stalls both captures, $script:stallErr only
        the SECOND read (stderr); $script:cleanupNote is what the removal reports."""
        fifo = FIFO_TEST.read_text(encoding="ascii")
        start = fifo.index("function ConvertTo-CommandLineArg")
        body = fifo[start:fifo.index("\n}\n", fifo.index("function Invoke-BoundedSuite")) + 3]
        return ("$RepoRoot = 'C:\\repo'; $agentScript = 'C:\\repo\\agent.ps1'\n"
                "function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw \"FAIL: $Message\" } }\n" + body + "\n"
                "function Initialize-BoundedCaptureIo { }\n"
                "function Start-Process { param([string]$FilePath, $ArgumentList, [switch]$NoNewWindow, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)\n"
                "  $fin = if ($FilePath -eq 'taskkill.exe') { $true } else { $script:finish }\n"
                "  $o = [pscustomobject]@{ Id = 4242; Handle = 1; ExitCode = 0 }\n"
                "  $o | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value ([scriptblock]::Create('param($ms) $' + $fin.ToString().ToLower()))\n"
                "  $o | Add-Member -MemberType ScriptMethod -Name Kill -Value { }\n"
                "  $o }\n"
                "function Read-CapturedResult([string]$Path) { $script:reads++\n"
                "  if ($script:stallReads -or ($script:stallErr -and $script:reads -eq 2)) { [pscustomobject]@{ Ok = $false; Text = '<read timed out: x not readable within 10000 ms>' } } else { [pscustomobject]@{ Ok = $true; Text = '{\"a\":1}' } } }\n"
                "function Remove-CaptureFiles([string[]]$Paths) { $script:removed += $Paths.Count; foreach ($f in $Paths) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }; $script:cleanupNote }\n")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_finished_suite_whose_stderr_capture_could_not_be_read_fails_by_name_instead_of_keeping_its_pass(self) -> None:
        # sol r8 B1: only the stdout read was checked. A suite that exited 0 with readable JSON kept PASS when its stderr read timed out (or
        # failed); the abandoned step survived only in unused diagnostic text. Stdout-only and stderr-only failures must both throw by name.
        script = (self._suite_stub_script() +
                  "function Case($label, $stallOut, $stallErr) {\n"
                  "  $script:finish = $true; $script:stallReads = $stallOut; $script:stallErr = $stallErr; $script:cleanupNote = ''; $script:reads = 0; $script:removed = 0\n"
                  "  try { $r = Invoke-BoundedSuite 'case-x' 'C:\\s.ps1' 5 3>$null; Write-Output \"$label|RESULT|$($r.ExitCode)|$($r.Json)|removed=$($script:removed)\" }\n"
                  "  catch { Write-Output \"$label|THROW|$($_.Exception.Message)|removed=$($script:removed)\" } }\n"
                  "Case 'OK' $false $false\n"
                  "Case 'ERRONLY' $false $true\n"
                  "Case 'OUTONLY' $true $false\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = {line.split("|", 1)[0]: line.split("|", 1)[1] for line in proc.stdout.splitlines() if line.split("|", 1)[0] in ("OK", "ERRONLY", "OUTONLY")}
        self.assertEqual(rows["OK"], 'RESULT|0|{"a":1}|removed=2', proc.stdout)
        self.assertIn("THROW|FAIL: case-x (C:\\s.ps1) finished but its stderr capture could not be read: <read timed out", rows["ERRONLY"])
        self.assertTrue(rows["ERRONLY"].endswith("removed=2"), "the captures are still removed before the named failure is raised")
        self.assertIn("THROW|FAIL: case-x (C:\\s.ps1) finished but its JSON result could not be read: <read timed out", rows["OUTONLY"])

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_the_suite_json_is_read_once_under_the_bound_and_every_stalled_step_fails_the_suite_by_name(self) -> None:
        # sol r6 B1/B2 through Invoke-BoundedSuite itself: Start-Process is a stub (no child), the bounded readers and the bounded removal are
        # stubs that report what a stalled step returns. The suite must (a) read each stream once, (b) fail by name when the JSON read
        # stalled, (c) name an abandoned cleanup in a timeout verdict, (d) name the timeout even when both reads stalled.
        script = (self._suite_stub_script() +
                  "function Case($label, $finish, $stall, $note) {\n"
                  "  $script:finish = $finish; $script:stallReads = $stall; $script:cleanupNote = $note; $script:reads = 0; $script:removed = 0\n"
                  "  try { $r = Invoke-BoundedSuite 'case-x' 'C:\\s.ps1' 5 3>$null; Write-Output \"$label|RESULT|$($r.ExitCode)|$($r.Json)|reads=$($script:reads)|removed=$($script:removed)\" }\n"
                  "  catch { Write-Output \"$label|THROW|$($_.Exception.Message)|reads=$($script:reads)|removed=$($script:removed)\" } }\n"
                  "Case 'A' $true $false ''\n"
                  "Case 'B' $true $true ''\n"
                  "Case 'C' $false $false 'capture cleanup abandoned after 10 s: out.tmp'\n"
                  "Case 'D' $false $true 'capture cleanup abandoned after 10 s: out.tmp'\n"
                  # adversary r7 A1: a finished suite with an abandoned cleanup must emit ONLY its result object on every PowerShell stream
                  # (the -UnitOnly stdout is one JSON object); the abandonment note goes to the process's stderr, not Write-Warning.
                  "$script:finish = $true; $script:stallReads = $false; $script:cleanupNote = 'capture cleanup abandoned after 10 s: out.tmp'\n"
                  "$all = @(Invoke-BoundedSuite 'case-x' 'C:\\s.ps1' 5 *>&1)\n"
                  "Write-Output \"E|$($all.Count)|$($all[0].GetType().Name)\"\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("E|1|PSCustomObject", proc.stdout)
        self.assertIn("capture cleanup abandoned after 10 s: out.tmp", proc.stderr, "the abandonment of a clean run's cleanup is still reported")
        rows = {line.split("|", 1)[0]: line.split("|", 1)[1] for line in proc.stdout.splitlines() if line[:2] in ("A|", "B|", "C|", "D|")}
        self.assertEqual(rows["A"], 'RESULT|0|{"a":1}|reads=2|removed=2', proc.stdout)
        self.assertIn("THROW|FAIL: case-x (C:\\s.ps1) finished but its JSON result could not be read: <read timed out", rows["B"])
        self.assertTrue(rows["B"].endswith("reads=2|removed=2"), rows["B"])
        self.assertIn("exceeded its 5 s bound", rows["C"])
        self.assertIn("; capture cleanup abandoned after 10 s: out.tmp", rows["C"])
        self.assertTrue(rows["C"].endswith("reads=2|removed=2"), rows["C"])
        self.assertIn("output so far: <read timed out: x not readable within 10000 ms>", rows["D"])
        self.assertIn("exceeded its 5 s bound", rows["D"])

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_huge_captured_output_cannot_stretch_the_verdict_past_its_post_wait_allowance(self) -> None:
        # adversary r11 A1 (major): after the wait and kill the diagnostic text was built by splitting, filtering and joining the WHOLE capture
        # (8,000,000 lines took 31.8 s), and the finished-suite JSON was normalised the same way, so the named verdict and cleanup ran far
        # past the advertised post-wait allowance. The diagnostic keeps only a bounded tail; a JSON result over the cap is a named failure.
        script = (self._suite_stub_script() +
                  "$big = 'x' + [char]10; $big = $big * 2000000\n"
                  "function Read-CapturedResult([string]$Path) { [pscustomobject]@{ Ok = $true; Text = $big } }\n"
                  "function Case($label, $finish) {\n"
                  "  $script:finish = $finish; $script:cleanupNote = ''; $script:removed = 0\n"
                  "  $sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  "  try { $r = Invoke-BoundedSuite 'case-x' 'C:\\s.ps1' 5 3>$null; $m = 'RESULT|' + $r.Text.Length }\n"
                  "  catch { $m = 'THROW|' + $_.Exception.Message.Length + '|' + $_.Exception.Message.Substring(0, [Math]::Min(120, $_.Exception.Message.Length)) }\n"
                  "  Write-Output \"$label|$m|fast=$($sw.Elapsed.TotalSeconds -lt 3)|secs=$([int]$sw.Elapsed.TotalSeconds)\" }\n"
                  "Case 'HUNG' $false\n"
                  "Case 'DONE' $true\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rows = {line.split("|", 1)[0]: line.split("|", 1)[1] for line in proc.stdout.splitlines() if line[:5] in ("HUNG|", "DONE|")}
        for label in ("HUNG", "DONE"):
            self.assertIn("fast=True", rows[label], f"{label}: the verdict must not scale with the captured size: {rows[label]}")
        self.assertRegex(rows["HUNG"], r"^THROW\|\d{1,5}\|FAIL: case-x \(C:\\s\.ps1\) exceeded its 5 s bound", rows["HUNG"])
        self.assertRegex(rows["DONE"], r"^THROW\|\d{1,5}\|FAIL: case-x \(C:\\s\.ps1\) finished but its JSON result is larger than 262144 characters", rows["DONE"])

    # ---- Dot B1 (PR #717 comments 6620616030, 6620858041): capture initialization must never replace the suite-named verdict -------------
    @staticmethod
    def _init_injection_script(fail_from_suite_too: bool) -> str:
        """The real Read-CapturedResult / Remove-CaptureFiles / Invoke-BoundedSuite with ONLY Start-Process stubbed (a suite that never
        exits, and a counter of launches) and Initialize-BoundedCaptureIo replaced by a stub that throws 'compile denied'.

        fail_from_suite_too=False lets the call made by Invoke-BoundedSuite itself through (its own early setup succeeded), so every later
        call from a helper throws: the verdict must stay the suite-named timeout with the failures as secondary diagnostics.
        fail_from_suite_too=True makes that early setup fail as well: the run must fail by its own setup error and never start the suite."""
        fifo = FIFO_TEST.read_text(encoding="ascii")
        suite_start = fifo.index("function Invoke-BoundedSuite")
        suite = fifo[suite_start:fifo.index("\n}\n", suite_start) + 3]
        tail = fifo[fifo.index("function Get-DiagnosticTail"):]
        quote = tail[:tail.index("\n}\n") + 3]   # Get-DiagnosticTail lives outside the region; ConvertTo-CommandLineArg is inside it (r14 A1)
        return ("$RepoRoot = 'C:\\repo'; $agentScript = 'C:\\repo\\agent.ps1'; $script:launched = 0; $script:initCalls = 0\n"
                "$SuiteBoundMaxSec = 3600; $DiagnosticTailChars = 4000; $JsonMaxChars = 262144\n"
                "function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw \"FAIL: $Message\" } }\n"
                "$work = Join-Path ([IO.Path]::GetTempPath()) ('b1-' + [guid]::NewGuid().ToString('N')); $null = New-Item -ItemType Directory -Path $work\n"
                "$env:TMP = $work; $env:TEMP = $work; $env:TMPDIR = $work\n"
                + _fifo_capture_io_region(fifo) + "\n" + quote + suite + "\n"
                "$realInit = ${function:Initialize-BoundedCaptureIo}\n"
                "function Initialize-BoundedCaptureIo {\n"
                "  $script:initCalls++\n"
                f"  $fromSuite = (Get-PSCallStack)[1].FunctionName -eq 'Invoke-BoundedSuite' -and -not ${str(fail_from_suite_too).lower()}\n"
                "  if ($fromSuite) { & $realInit; return }\n"
                "  throw 'compile denied'\n"
                "}\n"
                "function Start-Process { param([string]$FilePath, $ArgumentList, [switch]$NoNewWindow, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)\n"
                "  $script:launched++\n"
                "  $fin = $FilePath -eq 'taskkill.exe'\n"
                "  $o = [pscustomobject]@{ Id = 4242; Handle = 1; ExitCode = 0 }\n"
                "  $o | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value ([scriptblock]::Create('param($ms) $' + $fin.ToString().ToLower()))\n"
                "  $o | Add-Member -MemberType ScriptMethod -Name Kill -Value { }\n"
                "  $o }\n"
                "try { $r = Invoke-BoundedSuite 'case-x' 'C:\\s.ps1' 1 3>$null; Write-Output ('RESULT|' + $r.ExitCode) }\n"
                "catch { Write-Output ('THROW|' + $_.Exception.Message) }\n"
                "Write-Output ('LAUNCHED|' + $script:launched)\n"
                "Write-Output ('LEFTOVER|' + @(Get-ChildItem -LiteralPath $work -Force).Count)\n"
                "Get-ChildItem -LiteralPath $work -Force | Remove-Item -Force -ErrorAction SilentlyContinue\n"
                "Remove-Item -LiteralPath $work -Force -ErrorAction SilentlyContinue\n")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_b1_a_capture_setup_that_fails_after_a_timeout_stays_a_secondary_diagnostic_of_the_suite_named_verdict(self) -> None:
        # Dot B1 (b)+(c): Initialize-BoundedCaptureIo ran before the `try` of Read-CapturedResult and Remove-CaptureFiles, so a setup throw
        # ('compile denied') at the stdout read escaped before the timeout verdict was built, and the finally's removal repeated it.
        proc = self._pwsh(self._init_injection_script(fail_from_suite_too=False))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        throw = next((line for line in proc.stdout.splitlines() if line.startswith("THROW|")), "")
        self.assertRegex(throw, r"^THROW\|FAIL: case-x \(C:\\s\.ps1\) exceeded its 1 s bound", proc.stdout + proc.stderr)
        self.assertIn("<read failed: compile denied>", throw, "the read's setup failure is kept as the output diagnostic")
        self.assertIn("capture cleanup failed: compile denied", throw, "the cleanup's setup failure is kept as a cleanup diagnostic")
        self.assertIn("LAUNCHED|2", proc.stdout, "the suite and its taskkill")
        self.assertIn("LEFTOVER|2", proc.stdout, "the two captures the failed cleanup could not remove are named, and this test removes them")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_b1_a_capture_setup_that_fails_before_the_suite_starts_is_a_named_setup_failure_and_no_suite_runs(self) -> None:
        # Dot B1 (a)+(c): the capture setup is established BEFORE the suite starts; if it fails, the run fails by its own named setup error.
        proc = self._pwsh(self._init_injection_script(fail_from_suite_too=True))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        throw = next((line for line in proc.stdout.splitlines() if line.startswith("THROW|")), "")
        self.assertRegex(throw, r"^THROW\|FAIL: case-x \(C:\\s\.ps1\) capture setup failed: compile denied; no child was started", proc.stdout + proc.stderr)
        self.assertIn("LAUNCHED|0", proc.stdout, "the suite must never start when the capture setup failed")
        self.assertIn("LEFTOVER|0", proc.stdout, "no capture file is created before the setup is established")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_b1_the_helpers_return_a_failure_note_when_their_own_setup_throws(self) -> None:
        # Dot B1 (b): called directly, with the setup failing, neither helper throws; each reports the failure in its own return value.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        script = (_fifo_capture_io_region(fifo) + "\n"
                  "function Initialize-BoundedCaptureIo { throw 'compile denied' }\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "try { $r = Read-CapturedResult $p; Write-Output ('READ|' + $r.Ok + '|' + $r.Text) } catch { Write-Output ('READ-THREW|' + $_.Exception.Message) }\n"
                  "try { Write-Output ('DEL|' + (Remove-CaptureFiles @($p))) } catch { Write-Output ('DEL-THREW|' + $_.Exception.Message) }\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("READ|False|<read failed: compile denied>", proc.stdout, proc.stdout + proc.stderr)
        self.assertRegex(proc.stdout, r"DEL\|capture cleanup failed: compile denied: .*a\.txt", proc.stdout + proc.stderr)

    # ---- Dot B2 (PR #717 comments 6633365895, 6633763672): the setsid branch of Start-CaptureWorker with a pwsh whose real path has a space ----
    def test_b2_the_setsid_operand_is_quoted_before_start_process_joins_the_arguments(self) -> None:
        # Source pin (runs on every OS, including this Windows hub): Start-Process joins -ArgumentList with spaces and quotes nothing, so
        # the pwsh path handed to setsid as an operand must go through ConvertTo-CommandLineArg. The executed proof is the two tests below.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        begin = fifo.index("function Start-CaptureWorker")
        worker = fifo[begin:fifo.index("function Get-CaptureTaskResult", begin)]
        setsid_lines = [line.strip() for line in worker.splitlines() if "$exe = $setsid" in line]
        self.assertEqual(len(setsid_lines), 1, setsid_lines)
        self.assertRegex(setsid_lines[0], r"\$argv = @\(ConvertTo-CommandLineArg \$exe\) \+ \$argv", setsid_lines[0])

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_b2_adversary_a1_the_bounded_capture_region_calls_nothing_the_script_defines_outside_it(self) -> None:
        # adversary r14 A1 (major, introduced): the B2 fix called ConvertTo-CommandLineArg from Start-CaptureWorker, but that function was
        # defined OUTSIDE the BEGIN/END region every region-only driver (and every Ubuntu test above) dot-sources, so the setsid branch raised
        # command-not-found. The class: no command the region calls may be a function the script defines outside the region.
        script = ("$text = [IO.File]::ReadAllText('" + str(FIFO_TEST).replace("'", "''") + "')\n"
                  "$i = $text.IndexOf('# BEGIN bounded capture io'); $j = $text.IndexOf('# END bounded capture io')\n"
                  "$region = $text.Substring($i, $j - $i)\n"
                  "$t = $null; $e = $null\n"
                  "$file = [Management.Automation.Language.Parser]::ParseInput($text, [ref]$t, [ref]$e)\n"
                  "$reg = [Management.Automation.Language.Parser]::ParseInput($region, [ref]$t, [ref]$e)\n"
                  "$fileDefined = @($file.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object Name)\n"
                  "$regDefined = @($reg.FindAll({ param($a) $a -is [Management.Automation.Language.FunctionDefinitionAst] }, $true) | ForEach-Object Name)\n"
                  "$regCalled = @($reg.FindAll({ param($a) $a -is [Management.Automation.Language.CommandAst] }, $true) | ForEach-Object { $_.GetCommandName() } | Where-Object { $_ } | Sort-Object -Unique)\n"
                  "$missing = @($regCalled | Where-Object { $fileDefined -contains $_ -and $regDefined -notcontains $_ })\n"
                  "Write-Output ('MISSING|' + ($missing -join ','))\n"
                  "Write-Output ('QUOTE-IN-REGION|' + ($regDefined -contains 'ConvertTo-CommandLineArg'))\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("MISSING|\n", proc.stdout.replace("\r", "") + "\n", proc.stdout + proc.stderr)
        self.assertIn("QUOTE-IN-REGION|True", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_b2_adversary_a2_a_posix_host_without_setsid_is_a_named_failure_not_an_untracked_worker(self) -> None:
        # adversary r14 A2 (major, introduced): without /usr/bin/setsid or /bin/setsid the worker started with neither a job nor a process
        # group, so a process the factory orphaned survived teardown while the reader returned Ok=True. Windows already makes "no job" a named
        # failure; POSIX now does the same for "no setsid". The resolver takes the platform and the candidates, so this runs on any host.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$here = [IO.Path]::GetTempFileName()\n"
                  "Write-Output ('WINDOWS|[' + (Resolve-CaptureWorkerSetsid -Windows $true -Candidates @('/no/such/setsid')) + ']')\n"
                  "Write-Output ('FOUND|' + ((Resolve-CaptureWorkerSetsid -Windows $false -Candidates @('/no/such/setsid', $here)) -ceq $here))\n"
                  "try { $null = Resolve-CaptureWorkerSetsid -Windows $false -Candidates @('/no/such/setsid', '/no/such/either'); Write-Output 'NONE|returned' }\n"
                  "catch { Write-Output ('NONE|' + $_.Exception.Message) }\n"
                  "Remove-Item -LiteralPath $here -Force\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("WINDOWS|[]", proc.stdout)
        self.assertIn("FOUND|True", proc.stdout)
        self.assertRegex(proc.stdout, r"NONE\|the capture worker cannot be tracked: no setsid at /no/such/setsid, /no/such/either", proc.stdout + proc.stderr)
        fifo = FIFO_TEST.read_text(encoding="ascii")
        begin = fifo.index("function Start-CaptureWorker")
        worker = fifo[begin:fifo.index("function Get-CaptureTaskResult", begin)]
        self.assertIn("$setsid = Resolve-CaptureWorkerSetsid", worker)
        self.assertRegex(worker, r"if \(\[int\]\$fields\[2\] -eq \$worker\.Process\.Id\) \{ \$worker\.Group = \$worker\.Process\.Id(?:; \$worker\.GroupStart = \[string\]\$fields\[19\])? \}\s*\n\s*else \{ throw", "an unconfirmed group is a named failure too")

    @staticmethod
    def _link_or_copy(src: str, dst: str) -> str:
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        return dst

    def _run_worker_driver(self, pwsh_exe: str, work: str) -> subprocess.CompletedProcess:
        """Runs the real Start-CaptureWorker (through Read-CapturedResult and Remove-CaptureFiles) under `pwsh_exe`, with a private TMPDIR.

        The factory starts a 600 s sleeper under the worker; after the helpers return the sleeper must be gone (the worker's group is killed)
        and no capture-worker scratch directory may be left in TMPDIR."""
        tmp = os.path.join(work, "tmp")
        os.makedirs(tmp, exist_ok=True)
        pid_file = os.path.join(work, "sleeper.pid")
        sleeper = ("{ param($p) $c = Start-Process -FilePath (Get-Process -Id $PID).Path -NoNewWindow -PassThru "
                   "-ArgumentList @('-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Seconds 600'); "
                   "[IO.File]::WriteAllText('" + pid_file.replace("'", "''") + "', [string]$c.Id); [Threading.Tasks.Task]::FromResult('hello from the factory') }")
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  f"$factory = {sleeper}\n"
                  "Write-Output ('EXE|' + (Get-Process -Id $PID).Path)\n"
                  "$r = Read-CapturedResult $p 60000 $factory\n"
                  "Write-Output ('READ|' + $r.Ok + '|' + $r.Text)\n"
                  "$id = [int][IO.File]::ReadAllText('" + pid_file.replace("'", "''") + "'); $sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  "while ($null -ne (Get-Process -Id $id -ErrorAction SilentlyContinue) -and $sw.ElapsedMilliseconds -lt 5000) { Start-Sleep -Milliseconds 100 }\n"
                  "$alive = $null -ne (Get-Process -Id $id -ErrorAction SilentlyContinue)\n"
                  "Write-Output ('ALIVE|' + $alive)\n"
                  "if ($alive) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }\n"
                  "Write-Output ('SCRATCH|' + @(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) -Directory -Filter 'mlv-capture-worker-*' -Force).Count)\n")
        script_path = os.path.join(work, "driver.ps1")
        Path(script_path).write_text(script, encoding="utf-8")
        env = dict(os.environ, TMPDIR=tmp)
        return subprocess.run([pwsh_exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", script_path], capture_output=True, text=True, timeout=240, env=env)

    def _skip_unless_setsid_branch(self) -> None:
        if os.name != "posix":
            self.skipTest("SKIPPED: Start-CaptureWorker's setsid branch exists only on POSIX (os.name=" + os.name + "); hosted Ubuntu CI executes it")
        if not any(os.path.exists(p) for p in ("/usr/bin/setsid", "/bin/setsid")):
            self.skipTest("SKIPPED: no setsid at /usr/bin/setsid or /bin/setsid, so the setsid branch of Start-CaptureWorker is not taken")
        if PWSH is None:
            self.skipTest("SKIPPED: pwsh is not on PATH")

    def _assert_worker_driver_ok(self, proc: subprocess.CompletedProcess) -> None:
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("READ|True|hello from the factory", proc.stdout, "the capture worker was not ready / returned no result\n" + out)
        self.assertIn("ALIVE|False", proc.stdout, "the worker's process group was not ended\n" + out)
        self.assertIn("SCRATCH|0", proc.stdout, "the worker's scratch directory was not removed\n" + out)

    def test_b2_the_capture_worker_starts_runs_and_tears_down_under_an_ordinary_pwsh_path(self) -> None:
        # The control: the same driver under the ordinary pwsh path.
        self._skip_unless_setsid_branch()
        work = tempfile.mkdtemp(prefix="heavy-slot-gate-b2-control-")
        try:
            self._assert_worker_driver_ok(self._run_worker_driver(PWSH, work))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_b2_the_capture_worker_starts_runs_and_tears_down_under_a_pwsh_whose_real_path_contains_a_space(self) -> None:
        # Dot B2: `$argv = @($exe) + $argv` handed setsid the pwsh path unquoted, Start-Process split '/.../Power Shell/pwsh' into two operands,
        # setsid could not exec the first, the worker ended before it was ready and the -Start seam failed instead of running its factory.
        # (Get-Process).Path resolves symlinks on Linux, so the spaced pwsh is a hard-linked (or copied) tree of $PSHOME, not a symlink.
        self._skip_unless_setsid_branch()
        home = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-Command", "[Console]::Out.Write($PSHOME)"], capture_output=True, text=True, timeout=60).stdout.strip()
        work = tempfile.mkdtemp(prefix="heavy-slot-gate-b2-spaced-")
        try:
            spaced = os.path.join(work, "Power Shell")
            try:
                shutil.copytree(home, spaced, symlinks=True, copy_function=self._link_or_copy)
            except (OSError, shutil.Error) as exc:
                self.skipTest(f"SKIPPED: could not build a pwsh install under a path with a space from {home} ({type(exc).__name__}: {exc})")
            spaced_pwsh = os.path.join(spaced, "pwsh")
            probe = subprocess.run([spaced_pwsh, "-NoLogo", "-NoProfile", "-Command", "(Get-Process -Id $PID).Path"], capture_output=True, text=True, timeout=120)
            if probe.returncode != 0 or " " not in probe.stdout.strip():
                self.skipTest(f"SKIPPED: the copied pwsh does not run, or its real path has no space (exit {probe.returncode}, path {probe.stdout.strip()!r}, {probe.stderr.strip()[:200]!r})")
            proc = self._run_worker_driver(spaced_pwsh, work)
            self.assertRegex(proc.stdout, r"EXE\|.*Power Shell.*pwsh", proc.stdout + proc.stderr)
            self._assert_worker_driver_ok(proc)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---- r15 B2 (sol r14 blocker 2, adversary r14 A2; hub ruling: NARROW THE CONTRACT) ----
    def test_r15_b2_the_teardown_contract_is_stated_as_the_group_and_the_visible_descendants_only(self) -> None:
        # Source pin (runs on every OS): the promise that a factory "leaves nothing behind" was false for a factory child that starts its own
        # session (setsid): the recorded-group kill misses it and the ended worker's tree no longer lists it. The wording must keep saying so.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        begin = fifo.index("# sol r11 B2: ONE deadline")
        contract = fifo[begin:fifo.index("$CaptureWorkerStartMs = ", begin)]
        flat = re.sub(r"\s*\n#\s*", " ", contract)
        for needle in ("recorded process group", "still visible in the worker's tree", "starts its own session or group (setsid)", "OUTSIDE this contract",
                       "may survive", "the reader does not detect that"):
            self.assertIn(needle, flat, f"the narrowed teardown contract no longer says: {needle}")
        self.assertRegex(flat, r"(?i)POSIX.{0,200}recorded process group")
        self.assertNotRegex(fifo, r"(?i)nothing of the step is left|leaves? nothing behind|nothing is left behind|left in this process or after it|ends a capture worker and every process it started",
                            "a comment or message repeats the dropped 'leaves nothing behind' promise")
        for function in ("function Stop-CaptureWorker", "public static Task<string> KillTree"):
            at = fifo.index(function)
            self.assertIn("visible", fifo[max(0, at - 600):at + 1500], f"{function} is documented without the visible-descendants limit")

    def test_r16_a5_pid_ownership_is_a_stated_limitation_and_an_unreadable_identity_refuses_the_kill(self) -> None:
        # Source pin (every OS), r16 re-pass A2 / Dot's hardening note: the contract no longer implies the identity check and the group signal
        # are one step, no time window is claimed that was never measured, and "File.Exists = false" is not read as "gone".
        fifo = FIFO_TEST.read_text(encoding="ascii")
        begin = fifo.index("# PID OWNERSHIP IS A STATED LIMITATION")
        statement = re.sub(r"\s*\n#\s*", " ", fifo[begin:fifo.index("$CaptureWorkerStartMs = ", begin)])
        for needle in ("on POSIX the group is signalled by number", "separate identity check", "The identity check and the group signal are two steps",
                       "can still be signalled", "has not been measured", "no pidfd, no group handle"):
            self.assertIn(needle, statement, f"the pid-ownership limitation no longer says: {needle}")
        self.assertNotRegex(fifo, r"(?i)microsecond|no other leader has taken the number|cannot be reused|atomic(?:ally)? (?:check|kill)",
                            "a comment claims a pid-reuse window that was never measured, or an atomicity the code does not have")
        problem = fifo[fifo.index("function Get-CaptureGroupIdentityProblem"):fifo.index("function Stop-CaptureWorker")]
        code = "\n".join(_code_lines(problem))
        self.assertNotIn("[IO.File]::Exists(", code, "File.Exists answers false for a denied or failed lookup as well as for a gone file")
        self.assertIn("catch [IO.FileNotFoundException] { return '' }", code)
        self.assertIn("catch [IO.DirectoryNotFoundException] { return '' }", code)
        self.assertRegex(code, r"catch \{ return \"the identity of capture worker group", "any other failure refuses the kill")
        self.assertIn("SEPARATE steps", problem, "the check's own comment states that the signal is a separate step")
        stop = fifo[fifo.index("function Stop-CaptureWorker"):fifo.index("function Close-CaptureWorker")]
        self.assertIn("separate identity check", stop, "Stop-CaptureWorker repeats the limitation")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_adversary_r15_a3_a_group_number_reused_by_another_leader_is_not_killed(self) -> None:
        # adversary r15 A3: Stop-CaptureWorker sent SIGKILL to -<recorded group> even after the worker had been reaped, so a number reused by an
        # unrelated group leader was killed and reported as success. The worker's start time is recorded with the group and checked against the
        # process stat file first. The check takes the stat root, so it runs here against a fake one on any OS.
        root = tempfile.mkdtemp(prefix="heavy-slot-gate-a3-proc-")
        try:
            def stat(pid: int, start: str) -> str:
                return f"{pid} (pwsh) S 1 {pid} {pid} 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 {start} 1000 2000"

            for name, text in (("same", stat(4242, "777")), ("reused", stat(4242, "999")), ("comm", "4242 (a) b) c) S 1 4242 4242 0 -1 4194560 1 0 0 0 0 0 0 0 20 0 1 0 777 1 2")):
                os.makedirs(os.path.join(root, name, "4242"))
                Path(root, name, "4242", "stat").write_text(text, encoding="ascii")
            os.makedirs(os.path.join(root, "gone"))
            os.makedirs(os.path.join(root, "unreadable", "4242"))
            Path(root, "unreadable", "4242", "stat").write_text("", encoding="ascii")  # nothing to parse: the identity cannot be confirmed
            os.makedirs(os.path.join(root, "denied", "4242", "stat"))  # r16: a directory where the file belongs: the read is denied, which is NOT "gone"
            script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                      "$w = [pscustomobject]@{ Group = 4242; GroupStart = '777' }\n"
                      "foreach ($n in 'same', 'reused', 'comm', 'gone', 'unreadable', 'denied') {\n"
                      "  Write-Output ($n + '|[' + (Get-CaptureGroupIdentityProblem $w (Join-Path '" + root.replace("'", "''") + "' $n)) + ']')\n"
                      "}\n")
            proc = self._pwsh(script)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for name in ("same", "comm", "gone"):
            self.assertIn(f"{name}|[]", proc.stdout, f"{name}: the group is the worker's (or only its orphans remain) and may be killed")
        self.assertRegex(proc.stdout, r"reused\|\[capture worker group 4242 is no longer the worker's .*the group was not killed\]")
        self.assertRegex(proc.stdout, r"unreadable\|\[the identity of capture worker group 4242 could not be confirmed .*the group was not killed\]")
        self.assertRegex(proc.stdout, r"denied\|\[the identity of capture worker group 4242 could not be confirmed .*the group was not killed\]",
                         "a lookup that is denied (not NotFound) must refuse the kill, not read as a gone worker")
        fifo = FIFO_TEST.read_text(encoding="ascii")
        stop = fifo[fifo.index("function Stop-CaptureWorker"):fifo.index("function Close-CaptureWorker")]
        self.assertLess(stop.index("Get-CaptureGroupIdentityProblem"), stop.index("KillGroup"), "the identity is checked before any group kill")
        start = fifo[fifo.index("function Start-CaptureWorker"):fifo.index("function Get-CaptureTaskResult", fifo.index("function Start-CaptureWorker"))]
        self.assertIn("$worker.GroupStart = [string]$fields[19]", start, "the start time (stat field 22) is recorded with the group")

    # The fixture's own sleeper, exactly as the escaper script execs it. r16 (Dot's hardening note, re-pass A2): the cleanup kills an exact pid
    # only while /proc/<pid>/cmdline IS this full command line, not merely contains "sleep" (any unrelated process whose argv mentions sleep).
    _ESCAPER_CMDLINE = b"/bin/sleep\x00600\x00"

    def test_r15_b2_a_session_escaping_factory_child_is_outside_the_teardown_contract_and_is_killed_by_its_pid(self) -> None:
        # The stated limit, executed: a factory launches a readiness-confirmed child in its OWN session (setsid). The read succeeds and the child
        # survives teardown, which the narrowed contract names as out of scope. This test never relies on teardown for it: it ends the child itself
        # by exact pid in a finally (never a group or a name kill). If teardown ever reaches it this assertion fails: update the contract wording.
        self._run_session_escape_case(spaced=False)

    def test_r16_a4_a_session_escaping_factory_child_under_a_tmpdir_with_a_space(self) -> None:
        # r16 (re-pass A4): the fixture builds its script and pid-file paths under TMPDIR, and Start-Process joins its ArgumentList with spaces
        # and quotes nothing, so a space in either path split the argument. The driver now quotes the final argv with the fifo script's own
        # ConvertTo-CommandLineArg. Same assertions, a TMPDIR and fixture paths that contain a space.
        self._run_session_escape_case(spaced=True)

    def _run_session_escape_case(self, *, spaced: bool) -> None:
        self._skip_unless_setsid_branch()
        if not os.path.isdir("/proc"):
            self.skipTest("SKIPPED: no /proc, so the escaped sleeper's identity cannot be confirmed before it is killed by pid")
        setsid = next(p for p in ("/usr/bin/setsid", "/bin/setsid") if os.path.exists(p))
        work = tempfile.mkdtemp(prefix="heavy-slot-gate-r15-b2-")
        base = os.path.join(work, "tmp dir with space") if spaced else work
        pid_file = os.path.join(base, "escaper.pid")
        escaped_pid: int | None = None
        try:
            tmp = os.path.join(base, "tmp")
            os.makedirs(tmp)
            if spaced:
                self.assertIn(" ", tmp)
            script_file = os.path.join(base, "escaper.sh")
            Path(script_file).write_text('#!/bin/sh\necho $$ > "$1.tmp" && mv "$1.tmp" "$1"\nexec /bin/sleep 600\n', encoding="ascii")
            quoted_pid_file = pid_file.replace("'", "''")
            # The factory text runs in the worker process, where the driver's functions do not exist, so the driver quotes the argv (with the
            # real ConvertTo-CommandLineArg) and splices the quoted literals into the factory text before the factory is handed over.
            template = ("param($p) $null = Start-Process -FilePath '" + setsid + "' -NoNewWindow -PassThru -ArgumentList @ARGV@; "
                        "$sw = [Diagnostics.Stopwatch]::StartNew(); "
                        "while (-not [IO.File]::Exists('" + quoted_pid_file + "') -and $sw.ElapsedMilliseconds -lt 20000) { [Threading.Thread]::Sleep(20) }; "
                        "if (-not [IO.File]::Exists('" + quoted_pid_file + "')) { throw 'the session-escaping sleeper never became ready' }; "
                        "[Threading.Tasks.Task]::FromResult('escaped sleeper ready')")
            self.assertNotIn("\n'@", template)
            script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                      "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                      "$argv = @((ConvertTo-CommandLineArg '/bin/sh'), (ConvertTo-CommandLineArg '" + script_file.replace("'", "''") + "'), "
                      "(ConvertTo-CommandLineArg '" + quoted_pid_file + "'))\n"
                      "$argvLiteral = '(' + (($argv | ForEach-Object { \"'\" + $_.Replace(\"'\", \"''\") + \"'\" }) -join ', ') + ')'\n"
                      "$template = @'\n" + template + "\n'@\n"
                      "$factory = [scriptblock]::Create($template.Replace('@ARGV@', $argvLiteral))\n"
                      "$r = Read-CapturedResult $p 60000 $factory\n"
                      "Write-Output ('READ|' + $r.Ok + '|' + $r.Text)\n")
            script_path = os.path.join(base, "driver.ps1")
            Path(script_path).write_text(script, encoding="utf-8")
            proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", script_path], capture_output=True, text=True,
                                  timeout=240, env=dict(os.environ, TMPDIR=tmp))
            if os.path.exists(pid_file):
                escaped_pid = int(Path(pid_file).read_text(encoding="ascii").strip())
            out = proc.stdout + proc.stderr
            self.assertEqual(proc.returncode, 0, out)
            self.assertIn("READ|True|escaped sleeper ready", proc.stdout, out)
            self.assertIsNotNone(escaped_pid, "the session-escaping sleeper never recorded its pid\n" + out)
            try:
                os.kill(escaped_pid, 0)  # type: ignore[arg-type]
            except ProcessLookupError:
                self.fail("the session-escaping sleeper did not survive teardown: teardown now reaches it, so the narrowed contract wording is out of date")
        finally:
            if escaped_pid is None and os.path.exists(pid_file):
                with contextlib.suppress(OSError, ValueError):
                    escaped_pid = int(Path(pid_file).read_text(encoding="ascii").strip())
            if escaped_pid is not None:
                with contextlib.suppress(OSError):
                    # the exact pid, and only while it is still the fixture's own sleeper: its FULL command line, not a substring
                    if Path(f"/proc/{escaped_pid}/cmdline").read_bytes() == self._ESCAPER_CMDLINE:
                        os.kill(escaped_pid, 9)
            shutil.rmtree(work, ignore_errors=True)

    def test_r16_a4_the_escaper_cleanup_matches_the_fixtures_full_command_line_not_a_substring(self) -> None:
        # Source pin (every OS): the cleanup compares /proc/<pid>/cmdline for equality with the fixture's own command line.
        source = Path(__file__).read_text(encoding="utf-8")
        case = source[source.index("    def _run_session_escape_case("):source.index("    def test_r16_a4_the_escaper_cleanup_matches")]
        self.assertIn("read_bytes() == self._ESCAPER_CMDLINE", case)
        self.assertNotIn('b"sleep" in', case)
        self.assertIn("exec /bin/sleep 600", case)
        self.assertEqual(self._ESCAPER_CMDLINE, b"/bin/sleep\x00600\x00", "the pinned command line is the one the escaper script execs")
        self.assertIn("ConvertTo-CommandLineArg", case, "the fixture's argv is quoted with the fifo script's own function")
        self.assertNotRegex(case, r"-ArgumentList @\('/bin/sh'", "the argv must not be handed to Start-Process unquoted")


class BoundedCleanupTests(unittest.TestCase):
    """sol r4b B1 / B2: the cleanup after a timeout is bounded end to end, and the regression's ceiling is derived from that bound.

    Everything runs on a virtual clock with in-memory process fakes: a fake wait advances the clock by what it would have waited
    and expires like the real one, and a wait with no timeout is a test failure (the CPython subprocess.run() recovery path did one).
    """

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

    class _VirtualProc:
        """A process on the virtual clock. It exits `exit_after` seconds after its wait begins; None means it never exits."""

        def __init__(self, clock: "BoundedCleanupTests._Clock", exit_after: float | None, *, first_wait_expires: bool = False,
                     returncode: int = 0) -> None:
            self.clock = clock
            self.exit_after = exit_after
            self.first_wait_expires = first_wait_expires
            self.returncode = returncode
            self.pid = 4242
            self.waits: list[float | None] = []

        def wait(self, timeout: float | None = None) -> int:
            self.waits.append(timeout)
            if timeout is None:
                raise AssertionError("an unbounded wait")
            if self.first_wait_expires and len(self.waits) == 1:
                self.clock.now += timeout
                raise subprocess.TimeoutExpired(["child"], timeout)
            if self.exit_after is None or self.exit_after > timeout:
                self.clock.now += timeout
                raise subprocess.TimeoutExpired(["child"], timeout)
            self.clock.now += self.exit_after
            return self.returncode

        def kill(self) -> None:
            pass

    def _run(self, taskkill_exit_after: float | None, *, root_exit_after: float | None = 0) -> tuple[AssertionError, float]:
        """Run run_bounded(timeout=3) against a hung root; return (its verdict, virtual seconds it took)."""
        clock = self._Clock()
        root = self._VirtualProc(clock, root_exit_after, first_wait_expires=True)
        taskkill = self._VirtualProc(clock, taskkill_exit_after)

        def popen(argv: list[str], *args: object, **kwargs: object) -> object:
            return taskkill if argv and argv[0] == "taskkill" else root

        with mock.patch.object(subprocess, "Popen", side_effect=popen), mock.patch.object(time, "monotonic", lambda: clock.now):
            try:
                run_bounded(["child"], 3, "stub-hang-case")
            except AssertionError as exc:
                self.assertTrue(all(w is not None for w in root.waits + taskkill.waits), (root.waits, taskkill.waits))
                return exc, clock.now
        self.fail("run_bounded returned instead of failing")

    def test_a_taskkill_that_never_exits_and_a_wait_that_never_returns_still_return_inside_the_bound(self) -> None:
        # sol r4b B1: taskkill never exits, the kill of it is never reaped and the root never exits either. The helper must still
        # raise the case-named verdict, having waited exactly the sum of its documented bounds and nothing unbounded.
        caught, elapsed = self._run(None, root_exit_after=None)
        self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("process tree kill is NOT confirmed", str(caught))
        self.assertIn(f"taskkill failed: it did not exit within {TASKKILL_BOUND_SEC} s", str(caught))
        self.assertIn(f"taskkill was still running {TASKKILL_REAP_SEC} s after its kill", str(caught))
        self.assertIn(f"the root process was still running {ROOT_REAP_SEC} s after the kill", str(caught))
        self.assertEqual(elapsed, max_wait_and_kill_sec(3))  # the virtual clock counts the waits; the I/O allowances are real-time

    def test_the_cleanup_bound_is_the_sum_of_the_waits_the_helper_makes(self) -> None:
        self.assertEqual(CLEANUP_BOUND_SEC, TASKKILL_BOUND_SEC + TASKKILL_REAP_SEC + ROOT_REAP_SEC)
        self.assertEqual(max_wait_and_kill_sec(3), 3 + CLEANUP_BOUND_SEC)
        # sol r6 B3: the ceiling is every allowance, added in one place. Adding an allowance without adding it here is a red test.
        # adversary r8 A1: the capture close is one more bounded allowance.
        self.assertEqual(max_run_bounded_sec(3),
                         3 + CLEANUP_BOUND_SEC + CAPTURE_CLOSE_BOUND_SEC + 2 * READ_BOUND_SEC + CAPTURE_CLEANUP_BOUND_SEC)

    def test_a_run_that_uses_every_allowance_stays_inside_the_ceiling(self) -> None:
        # sol r6 B3: wait 3 + taskkill wait 60 + taskkill reap 5 + root reap 30 + stdout read 10 + stderr read 10 = 118 s is permitted by
        # the helper, and the old ceiling (max_run_bounded_sec(3) + CLEANUP_MARGIN_SEC = 108 s) rejected it. Both reads stall for their
        # full bound on the virtual clock here; the capture removal is an extra allowance on top.
        clock = self._Clock()
        root = self._VirtualProc(clock, None, first_wait_expires=True)
        taskkill = self._VirtualProc(clock, None)

        def popen(argv: list[str], *args: object, **kwargs: object) -> object:
            return taskkill if argv and argv[0] == "taskkill" else root

        def stalled_read(path: Path, *, tolerate_oserror: bool) -> str:
            clock.now += READ_BOUND_SEC
            return f"<read timed out: {path.name} not readable within {READ_BOUND_SEC:g} s>"

        with mock.patch.object(subprocess, "Popen", side_effect=popen), mock.patch.object(time, "monotonic", lambda: clock.now), \
                mock.patch.object(sys.modules[__name__], "_read_captured", stalled_read):
            with self.assertRaises(AssertionError) as caught:
                run_bounded(["child"], 3, "all-allowances-case")
        self.assertEqual(clock.now, 3 + CLEANUP_BOUND_SEC + MAX_READ_SEC)
        self.assertEqual(clock.now, 118)
        self.assertLessEqual(clock.now, max_run_bounded_sec(3))
        self.assertLess(clock.now, max_run_bounded_sec(3) + CLEANUP_MARGIN_SEC)
        self.assertGreater(clock.now, 3 + CLEANUP_BOUND_SEC + CLEANUP_MARGIN_SEC, "the permitted 118 s run must not be the old 108 s ceiling")
        self.assertIn("all-allowances-case exceeded its 3 s wall-clock bound", str(caught.exception))

    def test_a_slow_but_successful_taskkill_stays_inside_the_regression_ceiling(self) -> None:
        # sol r4b B2: taskkill 1 / 26 / 27 / 31 s took the hung-tree regression from 4 / 29 / 30 / 34 s total; the old fixed 30 s
        # ceiling failed the last two although taskkill succeeded inside its bound. Up to the full bound is a clean, confirmed kill.
        ceiling = max_run_bounded_sec(3) + CLEANUP_MARGIN_SEC
        for taskkill_sec in (1, 26, 27, 31, TASKKILL_BOUND_SEC):
            with self.subTest(taskkill_sec=taskkill_sec):
                caught, elapsed = self._run(taskkill_sec)
                self.assertEqual(elapsed, 3 + taskkill_sec)
                self.assertLess(elapsed, ceiling)
                self.assertIn("and its process tree was killed.", str(caught))

    def test_a_taskkill_past_its_bound_is_reported_not_waited_for(self) -> None:
        ceiling = max_run_bounded_sec(3) + CLEANUP_MARGIN_SEC
        for taskkill_sec in (TASKKILL_BOUND_SEC + 1, 600):
            with self.subTest(taskkill_sec=taskkill_sec):
                caught, elapsed = self._run(taskkill_sec)
                self.assertLess(elapsed, ceiling)
                self.assertIn("process tree kill is NOT confirmed", str(caught))
                self.assertIn(f"taskkill failed: it did not exit within {TASKKILL_BOUND_SEC} s", str(caught))


if __name__ == "__main__":
    unittest.main()
