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

import ast
import base64
import contextlib
import gc
import json
import math
import os
import re
import shutil
import signal
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


CAPTURE_IO_MAX_MS = _fifo_constant_ms("CaptureIoMaxMs")  # the largest bound the helpers accept; the bound every ACCEPT arm uses (see CAPTURE_ACCEPT_BOUND_MS)
# CI-FLAKE-HEAVY-SLOT-GATE-PLAIN-TASK-CLEANUP-5S-1: a case that asserts a SUCCESS (a finished task, a clean cleanup, the text read back) is
# measured against the helper's bound from the call's clock, and the worker process it hands the factory to must start its answer, write it
# and END inside that bound. A typed 5 s therefore made the verdict a statement about the runner (hosted `windows unittest heavy 7/14`,
# run 44725973056: "capture cleanup abandoned after 5 s: C:\x\a.txt" on a loaded runner). r1 moved the bound to the helper's maximum (60 s)
# and sol r1 (PR #876) showed that only moved the deadline: a task delayed 61000 ms is still abandoned. Every accept arm of
# BoundedCaptureIoTests therefore runs with the seam of _accept_region(): the bound ($AcceptMs), the helper's own maximum ($CaptureIoMaxMs)
# and the worker-start wait ($CaptureWorkerStartMs) are all the largest Int32 millisecond count (24.8 days), so none of the HELPER's own
# deadlines can elapse inside a unit test. The one deadline that remains is the outer one: run_bounded kills the whole pwsh tree when the
# case's whole-script budget (pwsh_script_budget_sec) runs out, and that budget charges every accept call CAPTURE_ACCEPT_CALL_CEILING_MS (never
# less than r1's 125 s). A hang is therefore still a named failure of the case, at that budget. The cases that assert a TIMEOUT keep their
# small typed bounds, because there the bound is the thing under test.
CAPTURE_ACCEPT_BOUND_MS = 2**31 - 1
CAPTURE_WORKER_START_MS = _fifo_constant_ms("CaptureWorkerStartMs")  # a worker may take this long to be ready, before the step's clock
CAPTURE_WORKER_KILL_MS = _fifo_constant_ms("CaptureWorkerKillMs")  # teardown: each of the job, group and tree kills
CAPTURE_WORKER_REAP_MS = _fifo_constant_ms("CaptureWorkerReapMs")  # teardown: the reap, and the scratch directory removal
CAPTURE_CEILING_SLACK_MS = CLEANUP_MARGIN_SEC * 1000  # scheduling slack on a loaded runner (the same named margin the cleanup ceilings use)


def capture_call_ceiling_ms(bound_ms: int) -> int:
    """The longest a whole Read-CapturedResult / Remove-CaptureFiles call may take: worker start, the operation budget, the teardown, slack."""
    return CAPTURE_WORKER_START_MS + bound_ms + 3 * CAPTURE_WORKER_KILL_MS + 2 * CAPTURE_WORKER_REAP_MS + CAPTURE_CEILING_SLACK_MS


# r3 (Grok G1): what the whole-script budget charges ONE accept call (a bound of CAPTURE_ACCEPT_BOUND_MS / `$AcceptMs`). The helper's own deadline
# is gone for an accept arm, so the outer run_bounded budget is the only deadline left, and it must not be tighter than the one r1 allowed
# (capture_call_ceiling_ms of the helper's 60 s maximum = 125 s). r2 charged a variable bound only the worker start bound (95 s per call).
CAPTURE_ACCEPT_CALL_CEILING_MS = capture_call_ceiling_ms(CAPTURE_IO_MAX_MS)


def max_wait_and_kill_sec(timeout: float) -> float:
    """The longest the wait plus the tree-kill cleanup of run_bounded(timeout=...) may take (what a virtual clock counts)."""
    return timeout + CLEANUP_BOUND_SEC


def max_run_bounded_sec(timeout: float) -> float:
    """The longest run_bounded(timeout=...) may take before it must have raised: every allowance the helper makes, added up once."""
    return max_wait_and_kill_sec(timeout) + CAPTURE_CLOSE_BOUND_SEC + MAX_READ_SEC + CAPTURE_CLEANUP_BOUND_SEC


# sol r4 B3: a case's own verdict and teardown only happen if the HOSTED JOB is still alive when run_bounded's budget runs out. The hosted ubuntu
# shard job is cancelled at its cap (tests.yml `timeout-minutes: 30`, read here from the shard runner that enforces the same number), and the
# shard runner warns at BUDGET_WARN_SHARE of it. Both are READ from tools/repo_hygiene/ci_unittest_shard.py, never typed; the cap is read, not changed.
# A generated whole-script budget, plus everything run_bounded may still do after it (max_run_bounded_sec), must fit inside that share, or the
# job ends the case before the case can name itself. _run_pwsh_script refuses a script above HOSTED_CASE_BUDGET_MAX_SEC before it launches anything.
_SHARD_SOURCE = (ROOT / "tools" / "repo_hygiene" / "ci_unittest_shard.py").read_text(encoding="utf-8")
_JOB_CAP_MATCH = re.search(r'(?m)^JOB_CAP_MINUTES = \{[^}\n]*"ubuntu": (\d+)', _SHARD_SOURCE)
_WARN_SHARE_MATCH = re.search(r"(?m)^BUDGET_WARN_SHARE = ([0-9.]+)\s*$", _SHARD_SOURCE)
if _JOB_CAP_MATCH is None or _WARN_SHARE_MATCH is None:
    raise AssertionError("ci_unittest_shard.py no longer defines JOB_CAP_MINUTES (with an ubuntu entry) and BUDGET_WARN_SHARE as plain numbers")
HOSTED_UBUNTU_JOB_CAP_SEC = int(_JOB_CAP_MATCH.group(1)) * 60  # the tightest hosted cap: the windows jobs have 50 min
SHARD_BUDGET_WARN_SHARE = float(_WARN_SHARE_MATCH.group(1))
HOSTED_CASE_BUDGET_MAX_SEC = HOSTED_UBUNTU_JOB_CAP_SEC * SHARD_BUDGET_WARN_SHARE - max_run_bounded_sec(0)


def _checked_budget_sec(budget: float, case: str) -> float:
    """`budget` if a hosted job can wait for it; a refusal (nothing launched) when it is above HOSTED_CASE_BUDGET_MAX_SEC.

    sol r4 B3: the static ceiling only. A job-clock clamp (reading how much of the job is already spent) is not part of this card; it is
    successor work (HEAVY-SLOT-POSIX-TREE-KILL-HELPER-1)."""
    if budget > HOSTED_CASE_BUDGET_MAX_SEC:
        raise AssertionError(f"{case}: the derived whole-script budget of {budget:g} s is above {HOSTED_CASE_BUDGET_MAX_SEC:g} s, the most a "
                             f"hosted job ({HOSTED_UBUNTU_JOB_CAP_SEC} s cap, warned at {SHARD_BUDGET_WARN_SHARE:g} of it) can wait for; no child was started")
    return budget


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


# sol r4 B2 (narrowed r7): POSIX has no taskkill, so a timed-out driver was killed alone. run_bounded starts its child in a session of its own on
# POSIX (start_new_session) and _kill_tree then sends ONE SIGKILL to that group. The full descendant walk (a setsid'ed capture worker is in a group
# of its own) and a real-process teardown test are NOT part of this card: they are HEAVY-SLOT-POSIX-TREE-KILL-HELPER-1.
_IS_POSIX = os.name == "posix"
_SIGKILL = getattr(signal, "SIGKILL", 9)


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


GROUP_EMPTY_SEC = 2.0  # how long a killed POSIX group may take to leave the process table before its verdict stops claiming it was killed


def _confirm_group_empty(pgid: int, problems: list[str]) -> None:
    """pass-7 A5: SIGKILL delivered and the driver reaped does not mean the GROUP is gone (a member in uninterruptible I/O survives both). Signal 0 to
    the group says whether any member is left: ProcessLookupError is empty; anything still there after GROUP_EMPTY_SEC is recorded."""
    deadline = time.monotonic() + GROUP_EMPTY_SEC
    while True:
        try:
            os.killpg(pgid, 0)  # type: ignore[attr-defined]
        except ProcessLookupError:
            return
        except Exception as exc:
            problems.append(f"process group probe failed: {_exc_type_name(exc)}")
            return
        if time.monotonic() >= deadline:
            problems.append(f"the process group still had live members {GROUP_EMPTY_SEC:g} s after the kill")
            return
        time.sleep(0.05)


def _kill_tree(proc: subprocess.Popen) -> str:
    """Kill proc's whole tree and say how it went; a failing cleanup step is reported, never allowed to replace the timeout verdict.

    Every wait below carries a timeout (subprocess.run(timeout=) does not: on expiry CPython kills the child and then waits for it
    with no timeout), so the whole cleanup stays inside CLEANUP_BOUND_SEC.

    adversary r10 (pass 2) A1: every step records ANY Exception (MemoryError, a RuntimeError from an audit hook, ...) and carries on to the
    next step, not just OSError / SubprocessError; only an interrupt (not an Exception) propagates.
    """
    problems: list[str] = []
    if _IS_POSIX:
        # sol r4 B2 (narrowed r7): there is no taskkill on POSIX; the driver leads its own session (run_bounded), so its group gets one SIGKILL.
        try:
            os.killpg(proc.pid, _SIGKILL)  # type: ignore[attr-defined]
        except ProcessLookupError:
            pass  # the group is already gone
        except Exception as exc:
            problems.append(f"process group kill failed: {_exc_type_name(exc)}")
    else:
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
    if _IS_POSIX:
        _confirm_group_empty(proc.pid, problems)
    # adversary r8 A2: on POSIX only the driver's own group was signalled (a setsid'ed capture worker is in a group of its own), so the verdict
    # claims the GROUP there and the whole TREE only where taskkill /T walked it.
    scope = "process group" if _IS_POSIX else "process tree"
    return f"its {scope} was killed" if not problems else f"its {scope} kill is NOT confirmed ({'; '.join(problems)})"


def run_bounded(argv: list[str], timeout: float, case: str, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run argv under a real wall-clock bound and return (returncode, stdout, stderr).

    `env` is the child's environment (None: inherit). On POSIX the child leads a session of its own, so a timeout can end its whole group and
    (sol r4 B2, narrowed r7: _kill_tree sends one SIGKILL to that group; the descendant walk is HEAVY-SLOT-POSIX-TREE-KILL-HELPER-1).

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
            launch: dict[str, object] = {}
            if _IS_POSIX:
                launch["start_new_session"] = True
            if env is not None:
                launch["env"] = env
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **launch)  # type: ignore[arg-type]
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
# The capture-call scanner (sol r9 B2, pass-7 A2 / A3). PowerShell command names are case-insensitive (sol r4). A call is read by a small lexer, not a
# line regex: quoted text and comments are masked (a path that CONTAINS a helper's name is no call, a brace in a string opens no block), a call's
# arguments end at the statement end (`;`, `|`, an unmatched closing bracket, a line end that is not a backtick continuation), and the arguments are
# BOUND to the helper's parameters (-BoundMs and its prefixes, positional order), so a later number on the line is never the bound.
_CAPTURE_CALL_PARAMETERS = {"read-capturedresult": ("Path", "BoundMs", "Start"), "read-capturedtext": ("Path", "BoundMs", "Start"),
                            "remove-capturefiles": ("Paths", "BoundMs", "Start")}
_CAPTURE_NAME = re.compile(r"(?i)(?<![\w$.:\-])(" + "|".join(re.escape(name) for name in _CAPTURE_CALL_PARAMETERS) + r")(?![\w\-])")
_QUOTED_INVOCATION = re.compile(r"(?i)(?<![\w$])[&.]\s*(['\"])(" + "|".join(re.escape(name) for name in _CAPTURE_CALL_PARAMETERS) + r")\1")
_LOOP_BLOCK_HEAD = re.compile(r"(?i)(?:\b(?:foreach|for|while)\s*\([^{}]*\)|\bdo|ForEach-Object)\s*$")
_OUTSIDE, _QUOTED, _COMMENT = 0, 1, 2


def _ps_mask(text: str) -> tuple[str, list[int]]:
    """`text` with the body of every quoted string and every comment replaced by spaces (newlines kept), and a parallel list marking each character
    _OUTSIDE, _QUOTED (a string body) or _COMMENT. A doubled quote inside a string and a backtick escape inside a double-quoted one stay in the string."""
    masked, kinds = list(text), [_OUTSIDE] * len(text)
    size = len(text)

    def blank(k: int, kind: int) -> None:
        kinds[k] = kind
        if text[k] not in "\r\n":
            masked[k] = " "

    # pass-7b A1: a double-quoted string can hold `$( ... )`, which is CODE (an executable call inside it is a call), so the scan keeps a stack:
    # a quote character for a string body, an int (the open paren depth) for a subexpression. A block comment `<# ... #>` is a comment too.
    stack: list = []
    index = 0
    while index < size:
        char, top = text[index], (stack[-1] if stack else None)
        if top in ("'", '"'):
            if top == '"' and char == "`" and index + 1 < size:
                blank(index, _QUOTED)
                blank(index + 1, _QUOTED)
                index += 2
            elif top == '"' and char == "$" and index + 1 < size and text[index + 1] == "(":
                stack.append(1)  # the subexpression is code: its characters stay _OUTSIDE
                index += 2
            elif char == top and index + 1 < size and text[index + 1] == top:
                blank(index, _QUOTED)
                blank(index + 1, _QUOTED)
                index += 2
            elif char == top:
                stack.pop()
                index += 1
            else:
                blank(index, _QUOTED)
                index += 1
            continue
        if char in "'\"":
            stack.append(char)
        elif text.startswith("<#", index):
            end = text.find("#>", index + 2)
            end = size if end < 0 else end + 2
            for k in range(index, end):
                blank(k, _COMMENT)
            index = end
            continue
        elif char == "#" and (index == 0 or text[index - 1] in " \t\r\n;({|"):
            while index < size and text[index] not in "\r\n":
                blank(index, _COMMENT)
                index += 1
            continue
        elif isinstance(top, int):
            if char == "(":
                stack[-1] = top + 1
            elif char == ")":
                if top == 1:
                    stack.pop()
                else:
                    stack[-1] = top - 1
        index += 1
    return "".join(masked), kinds


def _ps_command_arguments(text: str, masked: str, kinds: list[int], start: int) -> list[str]:
    """The argument tokens of the command whose name ends at `start`: split on top-level whitespace (never inside a string, a bracket or after a
    comma), ended by a top-level `;`, `|`, an unmatched closing bracket or a line end that is not a backtick continuation."""
    tokens: list[str] = []
    current, depth, index = "", 0, start
    while index < len(text):
        char, kind = text[index], kinds[index]
        if kind == _COMMENT:
            if current and depth == 0:  # a comment is whitespace between two arguments (`$p <# note #> 60000`)
                tokens.append(current)
                current = ""
            index += 1
            continue
        if kind == _OUTSIDE:
            if char == "`" and index + 1 < len(text) and text[index + 1] in "\r\n":
                index += 2 + (1 if text[index + 1] == "\r" and index + 2 < len(text) and text[index + 2] == "\n" else 0)
                if current:
                    tokens.append(current)
                    current = ""
                continue
            if char in "\r\n":
                break
            if masked[index] in "({[":
                depth += 1
            elif masked[index] in ")}]":
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and char in ";|":
                break
            if depth == 0 and char.isspace():
                if current and not current.endswith(","):
                    tokens.append(current)
                    current = ""
                elif current:
                    current += char
                index += 1
                continue
        current += char
        index += 1
    if current.strip():
        tokens.append(current.strip())
    return tokens


class _CaptureCall:
    """One Read-CapturedResult / Read-CapturedText / Remove-CaptureFiles call of a PowerShell text: where it starts and the bound it passes."""

    def __init__(self, name: str, position: int, arguments: list[str]) -> None:
        self.name, self.position, self.arguments = name, position, arguments
        params = _CAPTURE_CALL_PARAMETERS[name.lower()]
        named: dict[str, str] = {}
        positional: list[str] = []
        queue = list(arguments)
        while queue:
            token = queue.pop(0)
            parsed = re.fullmatch(r"-([A-Za-z]+)(:?)(.*)", token, flags=re.S)
            if parsed and parsed.group(2) == ":" and parsed.group(3):
                value: str | None = parsed.group(3)
            elif parsed and (parsed.group(2) == ":" or not parsed.group(3)):
                value = queue.pop(0) if queue else None
            else:
                positional.append(token)
                continue
            matches = [p for p in params if p.lower().startswith(parsed.group(1).lower())]  # type: ignore[union-attr]
            named[matches[0] if matches else parsed.group(1)] = value or ""  # type: ignore[union-attr]
        free = [p for p in params if p not in named]
        for parameter, token in zip(free, positional):
            named[parameter] = token
        self.bound: str | None = named.get("BoundMs")


_PS_NUMBER_TOKEN = re.compile(r"(?i)([+-]?)(0x[0-9a-f]+|0b[01]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)(kb|mb|gb|tb|pb)?[ld]?")
_PS_SIZE_SUFFIX = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4, "pb": 1024**5}


def _ps_number_value(token: str) -> float | None:
    """The value of `token` when it is one PowerShell numeric literal (decimal, fractional, exponent, hex 0x, binary 0b, kb..pb, an L / D suffix),
    quoted or not (a quoted number is converted by the binder); None otherwise."""
    text = token.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        text = text[1:-1].strip()
    parsed = _PS_NUMBER_TOKEN.fullmatch(text)
    if not parsed:
        return None
    digits = parsed.group(2).lower()
    value = float(int(digits, 16)) if digits.startswith("0x") else float(int(digits, 2)) if digits.startswith("0b") else float(digits)
    return (-1 if parsed.group(1) == "-" else 1) * value * _PS_SIZE_SUFFIX.get((parsed.group(3) or "").lower(), 1)


def ps_capture_calls(text: str) -> list[_CaptureCall]:
    """Every capture-helper call of the PowerShell `text` (a `function` definition, a quoted name and a comment are no calls)."""
    masked, kinds = _ps_mask(text)
    calls = []
    for match in _CAPTURE_NAME.finditer(masked):
        if re.search(r"(?i)\bfunction\s+$", masked[:match.start()]):
            continue
        calls.append(_CaptureCall(match.group(1), match.start(), _ps_command_arguments(text, masked, kinds, match.end())))
    # pass-7b A1: a quoted command name is still a call when it is invoked: `& 'Read-CapturedResult' $p $AcceptMs $f` (the quoted body is masked above)
    for match in _QUOTED_INVOCATION.finditer(text):
        if kinds[match.start()] == _OUTSIDE:
            calls.append(_CaptureCall(match.group(2), match.start(), _ps_command_arguments(text, masked, kinds, match.end())))
    return sorted(calls, key=lambda call: call.position)


def _call_is_inside_a_loop(body: str, position: int) -> bool:
    """True when `position` sits inside a `foreach (...) {`, `for`, `while`, `do {` or `ForEach-Object {` block of the pwsh `body`. Quoted text and
    comments are masked first (pass-7 A4): a brace in a string neither opens nor closes a block, and a comment between a loop head and its `{` hides nothing."""
    masked = _ps_mask(body)[0]
    stack: list[bool] = []
    for index in range(position):
        char = masked[index]
        if char == "{":
            stack.append(bool(_LOOP_BLOCK_HEAD.search(masked[max(0, index - 200):index])))
        elif char == "}" and stack:
            stack.pop()
    return any(stack)


_PS_PLAIN_VARIABLE = re.compile(r"\$(?:\w+:)?\w+")
_PS_TYPE_CAST = re.compile(r"(?i)\[\s*(?:u?int(?:16|32|64)?|u?long|double|single|float|decimal)\s*\]")
_PS_LITERAL_IN_EXPRESSION = re.compile(r"(?i)(?<![\w$.])(?:0x[0-9a-f]+|0b[01]+|\d+(?:\.\d+)?(?:e[+-]?\d+)?)(?:kb|mb|gb|tb|pb)?[ld]?(?![\w.])")


def _ps_bound_value(token: str) -> float | None:
    """The numeric value of a bound token: a literal (_ps_number_value), or an arithmetic expression over literals, numeric casts and quoted numbers
    (`([int]'60000')`, `(60 * 999)`), evaluated with a closed set of operators. None when it is anything else (a variable, a call, an unknown form)."""
    value = _ps_number_value(token)
    if value is not None:
        return value
    text = _PS_TYPE_CAST.sub("", token)
    text = re.sub(r"(['\"])\s*([+-]?[\w.]+)\s*\1", r"\2", text)  # a quoted number is converted by the binder
    text = _PS_LITERAL_IN_EXPRESSION.sub(lambda m: repr(_ps_number_value(m.group(0))), text)
    try:
        tree = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        return None

    def evaluate(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
            return -evaluate(node.operand) if isinstance(node.op, ast.USub) else evaluate(node.operand)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Mod)):
            left, right = evaluate(node.left), evaluate(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if not right:
                raise ValueError("division by zero")
            return left / right if isinstance(node.op, ast.Div) else left % right
        raise ValueError("not a closed numeric expression")

    try:
        result = evaluate(tree)
    except (ValueError, OverflowError, ZeroDivisionError):
        return None
    return result


def _bound_ceiling_ms(bound: str | None) -> int:
    """What the whole-script budget charges one call with this bound token: the accept ceiling for `$AcceptMs` or its value, the call ceiling of a
    numeric bound or a numeric expression, the worker start bound for a plain variable or an absent one (the helper's own default is far under it),
    and the helper's own maximum for any other expression (pass-7b A2: the worst a validated bound can be)."""
    if bound is None:
        return capture_call_ceiling_ms(CAPTURE_WORKER_START_MS)
    if re.fullmatch(r"(?i)\$AcceptMs", bound.strip()):
        return CAPTURE_ACCEPT_CALL_CEILING_MS
    value = _ps_bound_value(bound)
    if value is not None and round(value) == CAPTURE_ACCEPT_BOUND_MS:
        return CAPTURE_ACCEPT_CALL_CEILING_MS  # r3 G1: an accept call is charged the named accept ceiling, not the worker start bound
    if value is not None and round(value) > 0:
        return capture_call_ceiling_ms(round(value))
    if value is None and not _PS_PLAIN_VARIABLE.fullmatch(bound.strip()):
        return CAPTURE_ACCEPT_CALL_CEILING_MS  # an expression this scan cannot evaluate: charged the helper's maximum
    return capture_call_ceiling_ms(CAPTURE_WORKER_START_MS)


# sol r9 B1 / pass-7 A2: the source guard that keeps every accept arm off a real helper deadline. It read decimal literals only, so a hex literal
# (0x1388), a binary or suffixed one, a fractional one that PowerShell rounds up (999.6), one above the old 120000 cap, a call with NO bound at all
# (the helper then applies its own real 10 s default) and a bound passed by name (-BoundMs, -Path / -Paths without it) all passed it.
def capture_call_bound_offenders(text: str) -> list[str]:
    """Every Read-Captured* / Remove-CaptureFiles call of the PowerShell `text` that is not an accept arm's shape: no bound at all (named or positional),
    or a numeric bound of a second or more once PowerShell has rounded it (an expression is read for any literal of a second or more). `$AcceptMs`, a
    sub-second timeout bound and a variable are clean."""
    offenders: list[str] = []
    for call in ps_capture_calls(text):
        label = f"{call.name} {' '.join(call.arguments)}".strip()[:100]
        if call.bound is None:
            offenders.append(f"{label}: no bound argument (the helper applies its own real default deadline)")
            continue
        value = _ps_bound_value(call.bound)
        if value is not None:
            if round(value) >= 1000:
                offenders.append(f"{label}: {value:g}")
        elif not _PS_PLAIN_VARIABLE.fullmatch(call.bound.strip()):
            # pass-7b A2: an expression the scan cannot evaluate could be any typed bound (`([int]$x)`, a call): an accept arm names `$AcceptMs`
            offenders.append(f"{label}: the bound is an expression the guard cannot evaluate; name $AcceptMs")
    return offenders


def python_ps_fragments(function: ast.FunctionDef) -> list[str]:
    """The PowerShell text a test function builds: every string literal of its body, an f-string with each `{...}` replaced by a variable-like
    placeholder. A docstring is prose, and a string that is a direct argument of a `self.assert*` call is a source pin or a message, neither of which
    runs; both are left out. Python keeps adjacent literals in one string, and a `+` chain of strings is joined into one text (each non-literal operand
    a variable-like placeholder)."""
    fragments: list[str] = []

    def stringy(node: ast.AST) -> bool:
        return (isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))
                or (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and (stringy(node.left) or stringy(node.right))))

    def operands(node: ast.AST) -> list[ast.AST]:
        return operands(node.left) + operands(node.right) if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and stringy(node) else [node]

    def visit(node: ast.AST) -> None:
        if isinstance(node, ast.BinOp) and stringy(node):
            # pass-7b A3: a script built with `+` is ONE text (a quote opened in one literal closes in the next), each non-literal operand a variable-like
            # placeholder; reading the literals one by one made a closing quote look like an opening one and hid the call that followed it
            parts = operands(node)
            fragments.append("".join(part.value if isinstance(part, ast.Constant) else
                                     "".join(p.value if isinstance(p, ast.Constant) else "$PYTHON_EXPR" for p in part.values) if isinstance(part, ast.JoinedStr)
                                     else "$PYTHON_EXPR" for part in parts))
            for part in parts:
                if not isinstance(part, (ast.Constant, ast.JoinedStr)):
                    visit(part)
                elif isinstance(part, ast.JoinedStr):
                    for p in part.values:
                        if isinstance(p, ast.FormattedValue):
                            visit(p.value)
        elif isinstance(node, ast.JoinedStr):
            fragments.append("".join(part.value if isinstance(part, ast.Constant) else "$PYTHON_EXPR" for part in node.values))
            for part in node.values:
                if isinstance(part, ast.FormattedValue):
                    visit(part.value)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, str):
                fragments.append(node.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith("assert"):
            visit(node.func)
            for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
                if not isinstance(argument, (ast.Constant, ast.JoinedStr)):
                    visit(argument)
        else:
            for child in ast.iter_child_nodes(node):
                visit(child)

    body = list(function.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]  # the docstring
    for statement in body:
        visit(statement)
    return fragments


def pwsh_script_budget_sec(script: str, repeat: int = 1) -> float:
    """The whole-script wall-clock budget of a pwsh case, derived from the capture calls the script makes (r16, Dot C1 / adversary r15 A3).

    r13 started a worker pwsh for every Read-Captured* / Remove-CaptureFiles call, and each call may legitimately take
    capture_call_ceiling_ms(its bound) (the worker start bound, the operation, the teardown bounds, slack). A fixed 120 s therefore failed a
    valid script after four permitted slow starts (the transport case makes 18 calls). The budget is the base allowance plus, for every call
    site in the script body (the helpers' own definitions, before the END marker, are not calls), that call's ceiling; a call line whose bound
    is `$AcceptMs` (or its value) is charged CAPTURE_ACCEPT_CALL_CEILING_MS (r3 G1), any other variable bound the worker start bound. A script that wraps calls in a loop must say how often with `repeat`, so a loop can never
    hide calls from the derivation. A genuine hang still ends at the budget: run_bounded kills the whole tree and fails by case name.
    """
    marker = "# END bounded capture io"
    body = script[script.index(marker):] if marker in script else script
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    straight_ms, looped_ms = 0, 0
    for call in ps_capture_calls(body):  # pass-7 A3: one charge per real call, each for ITS OWN bound (read by parameter, not by position on a line)
        ceiling = _bound_ceiling_ms(call.bound)
        if _call_is_inside_a_loop(body, call.position):
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
    # sol r4 B3: the hosted job would be cancelled before a budget it cannot wait for ran out, so a hung capture would end with no case verdict
    # and no teardown. Nothing is launched above HOSTED_CASE_BUDGET_MAX_SEC (split the case).
    budget = _checked_budget_sec(pwsh_script_budget_sec(script, repeat), _case_name())
    with _pwsh_script_argv(script) as argv:
        code, out, err = run_bounded(argv, budget, _case_name())
    return subprocess.CompletedProcess(argv, code, out, err)


def _live_assert_less_bounds(source: str, names: set[str]) -> dict[str, list[object]]:
    """{test name: the literal bounds of its `self.assertLess(x, <literal>, ...)` statements} for the named tests, counting only a statement that
    is DIRECTLY in the test's body (an assertion inside a dead `if False:` branch is no assertion)."""
    found: dict[str, list[object]] = {}
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = [stmt.value.args[1].value for stmt in node.body  # type: ignore[attr-defined]
                                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) and isinstance(stmt.value.func, ast.Attribute)
                                and stmt.value.func.attr == "assertLess" and len(stmt.value.args) >= 2 and isinstance(stmt.value.args[1], ast.Constant)]
    return found


def _fifo_capture_io_region(fifo: str) -> str:
    """The fifo script's bounded capture io helpers, between their BEGIN / END markers, ready to dot-source in a bare pwsh."""
    begin = fifo.index("# BEGIN bounded capture io")
    return fifo[begin:fifo.index("# END bounded capture io")]


def _run_argv_bounded(argv: list[str], timeout: float, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """A child that may start capture workers, under run_bounded (sol r4 B2): a timeout fails by case name and ends the whole tree, the worker
    included. The stdlib's own run-with-timeout kills the root only, and on POSIX there is no taskkill to cover the rest."""
    case = _case_name()
    code, out, err = run_bounded(argv, _checked_budget_sec(timeout, case), case, env)
    return subprocess.CompletedProcess(argv, code, out, err)


# adversary r5 pass 3 A5: the budget derivation scans only what follows this marker (the helpers' own definitions, comments and dormant internal calls
# are not calls the script makes). A generated region carries it, so a region with no call of its own costs the base allowance and nothing more.
_END_OF_CAPTURE_REGION = "# END bounded capture io\n"


def _raw_region_with_maximum(helper_max_ms: int, worker_start_ms: int = CAPTURE_ACCEPT_BOUND_MS) -> str:
    """The capture region with NO accept seam on the OPERATION, its helper maximum ($CaptureIoMaxMs) patched down to `helper_max_ms`: the scaled
    stand-in for a seam that only moved the deadline (r1: the bound was the helper's own maximum).

    sol r4 B1: the worker-start wait ($CaptureWorkerStartMs) is extended to `worker_start_ms` (default: the accept bound, as _accept_region does),
    because it is no part of the subject here: a slow worker start must never fail the case before its operation is exercised. A caller passes a
    small value only to show that the seam is what keeps a start error out."""
    return (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n" + _END_OF_CAPTURE_REGION
            + f"$CaptureIoMaxMs = {helper_max_ms}\n$CaptureWorkerStartMs = {worker_start_ms}\n")


def _accept_region(helper_max_ms: int | None = None) -> str:
    """The capture region plus the accept-arm seam (sol r2 B1): after the region has defined its bounds, `$AcceptMs`, `$CaptureIoMaxMs` and
    `$CaptureWorkerStartMs` become CAPTURE_ACCEPT_BOUND_MS, so no deadline of the HELPER can end a success asserted by an accept arm (the outer
    run_bounded whole-script budget remains). `helper_max_ms` first patches the helper maximum down, as _raw_region_with_maximum does, to
    prove the seam overrides it (r3 G2)."""
    region = _fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n" + _END_OF_CAPTURE_REGION
    if helper_max_ms is not None:
        region += f"$CaptureIoMaxMs = {helper_max_ms}\n"
    return region + f"$CaptureIoMaxMs = {CAPTURE_ACCEPT_BOUND_MS}; $CaptureWorkerStartMs = {CAPTURE_ACCEPT_BOUND_MS}; $AcceptMs = {CAPTURE_ACCEPT_BOUND_MS}\n"


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


def _windows_kill_path() -> "mock._patch":
    """adversary r8 A1: a fixture that fakes `taskkill` models the Windows kill path, so it forces that branch of _kill_tree on every host."""
    return mock.patch.object(sys.modules[__name__], "_IS_POSIX", False)


_TASKKILL_MISSING_NOTE = "its process tree kill is NOT confirmed (taskkill failed: FileNotFoundError)"


def _hang_verdict_wordings(label: str, host_is_posix: bool) -> tuple[str, ...]:
    """The kill wordings a genuine-hang verdict may carry on one pass of the hang regression (sol r9 B3: one wording per branch, never the union).

    "host taskkill" runs the host's own kill branch: a POSIX host kills the process GROUP; a Windows host kills the TREE with its own taskkill (the
    original assertion also allowed the missing-taskkill note there, and nothing else). "taskkill missing" forces the Windows branch with taskkill
    absent on every host, so it is the missing-taskkill note only."""
    if label == "taskkill missing":
        return (_TASKKILL_MISSING_NOTE,)
    if host_is_posix:
        return ("its process group was killed",)
    return ("its process tree was killed", _TASKKILL_MISSING_NOTE)


class _WindowsKillPath:
    """Mixin for a fixture class whose every test fakes `taskkill`: the Windows kill branch is forced for each test (adversary r8 A1)."""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        patcher = _windows_kill_path()
        patcher.start()
        self.addCleanup(patcher.stop)  # type: ignore[attr-defined]


class PosixGroupKillTests(unittest.TestCase):
    """sol r4 B2, narrowed r7: on POSIX run_bounded starts its child in a session of its own and a timeout sends ONE SIGKILL to that group.

    MOCKED, on every OS: nothing here starts or signals a real process tree. A test that proves a real session-escaping descendant dies is not part of
    this card; it belongs to HEAVY-SLOT-POSIX-TREE-KILL-HELPER-1."""

    def _timed_out_run(self, killpg: object) -> tuple[list[dict], object, str]:
        module = sys.modules[__name__]
        launched: list[dict] = []

        class FakeRoot:
            pid = 424242
            returncode = 1
            killed = False
            waits = 0

            def wait(self, timeout: object = None) -> int:
                FakeRoot.waits += 1
                if FakeRoot.waits == 1:
                    raise subprocess.TimeoutExpired("fake", timeout)  # the run bound
                return 1  # the reap after the kill

            def kill(self) -> None:
                FakeRoot.killed = True

        def popen(argv: object, *args: object, **kwargs: object) -> object:
            launched.append(dict(kwargs))
            return FakeRoot()

        with mock.patch.object(module, "_IS_POSIX", True), mock.patch.object(os, "killpg", killpg, create=True), \
                mock.patch.object(subprocess, "Popen", side_effect=popen):
            with self.assertRaises(AssertionError) as caught:
                run_bounded(["x"], 1, "posix-group-case")
        return launched, FakeRoot, str(caught.exception)

    def test_r7_b2_a_posix_timeout_starts_the_child_in_its_own_session_and_sigkills_its_group_once(self) -> None:
        killpg = mock.Mock(side_effect=[None, ProcessLookupError()])  # the SIGKILL lands; the probe then finds the group empty (pass-7 A5)
        launched, root, message = self._timed_out_run(killpg)
        self.assertEqual(len(launched), 1, "on POSIX no taskkill is started; the only child is the driver")
        self.assertTrue(launched[0].get("start_new_session"), "the driver must lead its own session so its group is its own")
        self.assertEqual(killpg.call_args_list, [mock.call(424242, sys.modules[__name__]._SIGKILL), mock.call(424242, 0)],
                         "one SIGKILL to the group, then one signal-0 probe that the group is empty")
        self.assertTrue(root.killed, "the driver itself is still killed and reaped after the group")
        self.assertIn("posix-group-case exceeded its 1 s wall-clock bound", message)
        # adversary r8 A2: only the driver's group was signalled, so the verdict says GROUP, never "tree" (a setsid'ed worker is in a group of its own)
        self.assertIn("its process group was killed", message)
        self.assertNotIn("process tree was killed", message)

    def test_r7_b2_a_group_that_is_already_gone_is_no_problem_and_a_failed_kill_is_named_not_confirmed(self) -> None:
        _, _, gone = self._timed_out_run(mock.Mock(side_effect=ProcessLookupError(3, "no such process")))
        self.assertIn("its process group was killed", gone)
        _, _, denied = self._timed_out_run(mock.Mock(side_effect=PermissionError(1, "not permitted")))
        self.assertIn("process group kill failed: PermissionError", denied)
        self.assertIn("its process group kill is NOT confirmed", denied)
        self.assertIn("posix-group-case exceeded its 1 s wall-clock bound", denied)

    def test_r10_a5_a_group_that_still_has_a_live_member_after_the_kill_is_not_called_killed(self) -> None:
        # pass-7 A5: SIGKILL delivered and the driver reaped left a same-group child in uninterruptible I/O, and the verdict still said the group
        # was killed. The probe now sees the member; the verdict says the kill is NOT confirmed and names why.
        module = sys.modules[__name__]
        killpg = mock.Mock(return_value=None)  # the SIGKILL and every probe succeed: a member is alive
        with mock.patch.object(module, "GROUP_EMPTY_SEC", 0.2):
            _, _, message = self._timed_out_run(killpg)
        first = message.split("\n", 1)[0]
        self.assertIn("its process group kill is NOT confirmed (the process group still had live members 0.2 s after the kill)", first)
        self.assertNotIn("its process group was killed", first)
        self.assertGreater(killpg.call_count, 2, "the probe polls until the group empties or the bound ends")
        self.assertLessEqual(GROUP_EMPTY_SEC, TASKKILL_BOUND_SEC + TASKKILL_REAP_SEC,
                             "the probe fits in the slack the POSIX path leaves under CLEANUP_BOUND_SEC (it starts no taskkill)")
        probe_error = mock.Mock(side_effect=[None, PermissionError()])
        _, _, message = self._timed_out_run(probe_error)
        self.assertIn("process group probe failed: PermissionError", message.split("\n", 1)[0])


class WindowsPathFixturesOnAPosixHostTests(unittest.TestCase):
    """adversary r8 A1: the fixtures that fake `taskkill` model the WINDOWS kill path. On a POSIX host (hosted ubuntu) `_kill_tree` took the group-kill
    branch for them instead: the fake taskkill was never started, the expected "NOT confirmed" verdicts never appeared, and a fixture's fake pid
    (4242) was signalled as a real process group. Each fixture class forces the Windows branch in its own setUp."""

    NAMES = (
        "BoundedRunOutputReadFailureTests.test_a_timeout_keeps_its_case_named_verdict_when_taskkill_hangs_or_fails",
        "BoundedRunInputRangeTests.test_a_wait_that_raises_after_the_launch_still_kills_the_tree_and_names_the_case",
        "HostileExceptionRenderingTests.test_a_hostile_exception_in_any_cleanup_phase_keeps_the_case_named_verdict_and_the_root_kill_and_reap",
        "BoundedCaptureIoTests.test_a_finished_cleanup_adds_no_note_to_the_verdict",
        "BoundedCleanupTests.test_a_slow_but_successful_taskkill_stays_inside_the_regression_ceiling",
    )

    def test_r8_a1_the_taskkill_fixtures_pass_on_a_posix_host_and_never_signal_a_process_group(self) -> None:
        module = sys.modules[__name__]
        killpg = mock.Mock()
        suite = unittest.defaultTestLoader.loadTestsFromNames(self.NAMES, module)
        result = unittest.TestResult()
        with mock.patch.object(module, "_IS_POSIX", True), mock.patch.object(os, "killpg", killpg, create=True):
            suite.run(result)
        self.assertEqual(killpg.call_count, 0, f"a taskkill fixture signalled a process group: {killpg.call_args_list}")
        self.assertEqual([(str(test), trace) for test, trace in result.failures + result.errors], [])
        self.assertEqual((result.testsRun, len(result.skipped)), (len(self.NAMES), 0))


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

    # ---- CI-FLAKE-HEAVY-SLOT-GATE-PLAIN-TASK-CLEANUP-5S-1 r5 (sol r4 B2, B3, B4) ----
    def test_r5_b3_a_generated_whole_script_budget_that_the_hosted_job_could_not_wait_for_is_refused_before_any_child(self) -> None:
        # sol r4 B3: the 18-call transport script's budget was 2465 s on a hosted Ubuntu job that is cancelled at 1800 s, so a hung capture
        # was ended by the job, with no case-level verdict and no teardown. A generated budget must leave the case's own verdict room inside
        # the job: _run_pwsh_script refuses (before it launches anything) a budget above HOSTED_CASE_BUDGET_MAX_SEC.
        module = sys.modules[__name__]
        region = "function Read-CapturedResult { }\n# END bounded capture io\n"
        looped = (region + "foreach ($t in $texts) {\n  $r = Read-CapturedResult $p $AcceptMs $ok\n  $r = Read-CapturedResult $p $AcceptMs $bad\n"
                  "  $n = Remove-CaptureFiles @($p) $AcceptMs $ok\n}\n")
        with mock.patch.object(module, "run_bounded", return_value=(0, "", "")) as bounded:
            with self.assertRaises(AssertionError) as refused:
                _run_pwsh_script(looped, repeat=6)  # the old transport shape: 18 accept calls
            bounded.assert_not_called()
        self.assertIn("hosted", str(refused.exception))
        one = region + "$r = Read-CapturedResult $p $AcceptMs $ok\n"
        with mock.patch.object(module, "run_bounded", return_value=(0, "", "")) as bounded:
            _run_pwsh_script(one)
        self.assertEqual(bounded.call_args.args[1], PWSH_SCRIPT_BASE_SEC + CAPTURE_ACCEPT_CALL_CEILING_MS / 1000)
        # the limit is derived from the workflow's job cap and the shard budget share, never typed
        self.assertEqual(HOSTED_UBUNTU_JOB_CAP_SEC, 30 * 60)
        self.assertLessEqual(max_run_bounded_sec(HOSTED_CASE_BUDGET_MAX_SEC), HOSTED_UBUNTU_JOB_CAP_SEC * SHARD_BUDGET_WARN_SHARE)
        self.assertLess(pwsh_script_budget_sec(looped, repeat=2), HOSTED_CASE_BUDGET_MAX_SEC, "two texts per script would still fit; six did not")

    def test_r5_b3_the_capture_call_scan_is_case_insensitive_like_powershell(self) -> None:
        # sol r4 hardening: PowerShell command names are case-insensitive, so a lowercase call must be charged like the recognized spelling.
        region = "function Read-CapturedResult { }\n# END bounded capture io\n"
        self.assertEqual(pwsh_script_budget_sec(region + "$r = read-capturedresult $p $acceptms $f\n"), PWSH_SCRIPT_BASE_SEC + CAPTURE_ACCEPT_CALL_CEILING_MS / 1000)

    def test_r5_b4_the_three_existing_timing_assertions_keep_their_original_bounds(self) -> None:
        # sol r4 B4: the three assertions below were widened from 10 s / 30 s / 30 s to 148 s / 205 s / 148 s, which let a 60 s abandonment pass.
        # They stay at their original bounds (coverage preservation); a larger ceiling belongs in a NEW test with its own reason.
        expected = {"test_a_stalled_capture_cleanup_after_a_timeout_returns_the_named_verdict_and_names_the_abandoned_step": 10,
                    "test_a_stalled_capture_cleanup_after_a_clean_run_returns_the_result_and_warns": 30,
                    "test_a_capture_close_that_stalls_is_abandoned_inside_its_bound_and_named": 30}
        self.assertEqual(_live_assert_less_bounds(Path(__file__).read_text(encoding="utf-8"), set(expected)), {name: [bound] for name, bound in expected.items()})
        # adversary r5 A11: a literal in a dead branch is no assertion; only a direct statement of the test body counts
        weakened = ("class T:\n    def test_a(self):\n        if False:\n            self.assertLess(elapsed, 10)\n        self.assertLess(elapsed, limit)\n")
        self.assertEqual(_live_assert_less_bounds(weakened, {"test_a"}), {"test_a": []})

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
            # sol r9 B1 (sibling): GOOD is an accept arm, so the accept seam and never the helper's real 10 s default; MISSING names it too.
            # pass-7 A1: HUNG is the timeout arm and keeps the raw region (its elapsed ceiling is derived from the raw 30 s start bound), so it
            # runs as its own script.
            hung = ("$ReadBoundMs = 10000\n" + _fifo_capture_io_region(fifo) + "\n"
                    "$hang = { param($p) [Threading.Tasks.TaskCompletionSource[string]]::new().Task }\n"
                    "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                    f"Write-Output ('HUNG|' + (Read-CapturedText (Join-Path ([IO.Path]::GetTempPath()) 'stdout.txt') 300 $hang) + '|' + ($sw.ElapsedMilliseconds -lt {capture_call_ceiling_ms(300)}))\n")
            accepted = ("$ReadBoundMs = 10000\n" + _accept_region() + "\n"
                        "$good = '" + good.replace("'", "''") + "'\n"
                        "Write-Output ('GOOD|' + (Read-CapturedText $good $AcceptMs))\n"
                        "Write-Output ('MISSING|' + (Read-CapturedText (Join-Path (Join-Path ([IO.Path]::GetTempPath()) 'no-such-dir-r6') 'x.txt') $AcceptMs))\n")
            hung_proc = _run_pwsh_script(hung)
            proc = _run_pwsh_script(accepted)
        finally:
            os.unlink(good)
        self.assertEqual(hung_proc.returncode, 0, hung_proc.stdout + hung_proc.stderr)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        lines = {line.split("|", 1)[0]: line.split("|", 1)[1] for line in (hung_proc.stdout + proc.stdout).splitlines() if "|" in line}
        self.assertEqual(lines["HUNG"], "<read timed out: stdout.txt not readable within 300 ms>|True")
        self.assertEqual(lines["GOOD"], "hello")
        self.assertTrue(lines["MISSING"].startswith("<read failed: "), lines["MISSING"])

    def test_a_finished_run_returns_its_code_and_both_streams(self) -> None:
        rc, out, err = run_bounded([sys.executable, "-c", "import sys; print('out'); sys.stderr.write('err'); sys.exit(3)"], 60, "finished-case")
        self.assertEqual((rc, out.strip(), err), (3, "out", "err"))


class BoundedRunOutputReadFailureTests(_WindowsKillPath, unittest.TestCase):
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


class BoundedRunInputRangeTests(_WindowsKillPath, unittest.TestCase):
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


class HostileExceptionRenderingTests(_WindowsKillPath, unittest.TestCase):
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
                    _windows_kill_path(), mock.patch.object(shutil, "rmtree", stalled_rmtree), \
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
        with mock.patch.object(subprocess, "Popen", side_effect=popen), _windows_kill_path(), mock.patch.object(threading.Thread, "start", start):
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
            with mock.patch.object(subprocess, "Popen", side_effect=popen), _windows_kill_path(), mock.patch.object(shutil, "rmtree", leave_behind):
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
        patches = [mock.patch.object(subprocess, "Popen", side_effect=popen), _windows_kill_path(),
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
        # sol r9 B1: an accept arm (a success verdict is asserted), so it runs at the accept seam and names it, never the helper's real 10 s default.
        script = (_accept_region() + "\n"
                  "$dir = Join-Path ([IO.Path]::GetTempPath()) ('mlv-r8-dir-' + [guid]::NewGuid().ToString('N'))\n"
                  "$null = New-Item -ItemType Directory -Path $dir\n"
                  "Write-Output ('DIR|' + (Remove-CaptureFiles @($dir) $AcceptMs))\n"
                  "Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("DIR|capture cleanup incomplete: still present: ", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_capture_whose_delete_and_existence_probe_both_fail_is_unconfirmed_not_a_removal(self) -> None:
        # sol r8 B2 (PowerShell half): File.Delete threw (swallowed) and File.Exists / Directory.Exists return False on ANY error, so the
        # helper reported an empty success note. A NUL in the path makes all three behave that way on every platform, without an ACL change.
        # Only "file not found" / "directory not found" from the probe means gone; any other error leaves the removal unconfirmed.
        script = (_accept_region() + "\n"  # sol r9 B1: accept arms (success rows GONE / NOPARENT), so the accept seam and no real default deadline
                  "$bad = 'C:\\x\\a' + [char]0 + 'b.tmp'\n"
                  "$real = [IO.Path]::GetTempFileName()\n"
                  "Write-Output ('BAD|' + (Remove-CaptureFiles @($bad) $AcceptMs).Replace([string][char]0, '<NUL>'))\n"
                  "Write-Output ('MIXED|' + (Remove-CaptureFiles @($real, $bad) $AcceptMs).Replace([string][char]0, '<NUL>') + '|' + (Test-Path -LiteralPath $real))\n"
                  "$gone = Join-Path ([IO.Path]::GetTempPath()) ('mlv-r9-gone-' + [guid]::NewGuid().ToString('N') + '.tmp')\n"
                  "Write-Output ('GONE|[' + (Remove-CaptureFiles @($gone) $AcceptMs) + ']')\n"
                  "$noParent = Join-Path ([IO.Path]::GetTempPath()) ('mlv-r9-noparent-' + [guid]::NewGuid().ToString('N') + '\\a.tmp')\n"
                  "Write-Output ('NOPARENT|[' + (Remove-CaptureFiles @($noParent) $AcceptMs) + ']')\n")
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
        with _windows_kill_path():  # a bare instance of the fixture class has had no setUp
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
        # sol r9 B1: STALLED is the timeout arm (its 300 ms bound is the subject); CLEAN is an accept arm and names the accept seam, because the
        # helper's real 10 s default would turn a slow delete on a loaded runner into "capture cleanup abandoned after 10 s".
        # sol r9 pass-7 A1: two scripts. The accept seam raises the worker-start wait, and the STALLED row's elapsed ceiling is derived from the raw
        # 30 s start bound, so the timeout arm keeps the raw region and only the accept arm runs under the seam.
        stalled = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                   "$hang = { param($p) [Threading.Tasks.TaskCompletionSource[bool]]::new().Task }\n"
                   "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                   f"Write-Output ('STALLED|' + (Remove-CaptureFiles @('C:\\x\\a.txt', 'C:\\x\\b.txt') 300 $hang) + '|' + ($sw.ElapsedMilliseconds -lt {capture_call_ceiling_ms(300)}))\n")
        clean = (_accept_region() + "\n"
                 "$real = [IO.Path]::GetTempFileName()\n"
                 "Write-Output ('CLEAN|[' + (Remove-CaptureFiles @($real) $AcceptMs) + ']|' + (Test-Path -LiteralPath $real))\n")
        proc = self._pwsh(stalled)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STALLED|capture cleanup abandoned after 0.3 s: C:\\x\\a.txt, C:\\x\\b.txt|True", proc.stdout)
        proc = self._pwsh(clean)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
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
        script = (_accept_region() + "\n"
                  "# END bounded capture io\n"
                  "$real = (Get-Command Start-CaptureWorker -CommandType Function).ScriptBlock\n"
                  "function Start-CaptureWorker { Start-Sleep -Milliseconds 2000; & $real }\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$f = { param($p) [Threading.Tasks.Task]::FromResult('ok') }\n"
                  "$r = Read-CapturedResult $p $AcceptMs $f; Write-Output ('ONE|' + $r.Ok + '|' + $r.Text)\n"
                  "$r = Read-CapturedResult $p $AcceptMs $f; Write-Output ('TWO|' + $r.Ok + '|' + $r.Text)\n"
                  "$r = Read-CapturedResult $p $AcceptMs $f; Write-Output ('THREE|' + $r.Ok + '|' + $r.Text)\n")
        base = 4
        started = time.monotonic()
        with mock.patch(f"{__name__}.PWSH_SCRIPT_BASE_SEC", base):
            proc = _run_pwsh_script(script)
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for label in ("ONE", "TWO", "THREE"):
            self.assertIn(f"{label}|True|ok", proc.stdout, proc.stdout + proc.stderr)
        self.assertGreater(elapsed, base, "the script must outlast the typed base, or this case no longer shows what a fixed cap does")

    def test_r10_the_hang_regression_accepts_one_kill_wording_per_pass_and_branch(self) -> None:
        # sol r9 B3: the union (tree OR group OR not-confirmed) let the Windows host pass accept a group-only verdict, which the original
        # assertion rejected. Every cell of (pass, host) names the wordings it accepts; the others are refused.
        group, tree, missing = "its process group was killed", "its process tree was killed", _TASKKILL_MISSING_NOTE
        self.assertEqual(_hang_verdict_wordings("host taskkill", host_is_posix=True), (group,))
        self.assertEqual(_hang_verdict_wordings("host taskkill", host_is_posix=False), (tree, missing))
        self.assertEqual(_hang_verdict_wordings("taskkill missing", host_is_posix=True), (missing,))
        self.assertEqual(_hang_verdict_wordings("taskkill missing", host_is_posix=False), (missing,))
        self.assertNotIn(group, _hang_verdict_wordings("host taskkill", host_is_posix=False), "a Windows host never kills by process group")
        self.assertNotIn(tree, _hang_verdict_wordings("host taskkill", host_is_posix=True))
        for posix in (True, False):
            self.assertNotIn(group, _hang_verdict_wordings("taskkill missing", host_is_posix=posix))
            self.assertNotIn(tree, _hang_verdict_wordings("taskkill missing", host_is_posix=posix))

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

        # adversary r8 A1/A2: on a POSIX host the first pass takes the group-kill branch (verdict: "process group"); the second pass is the Windows
        # branch with taskkill missing, so it forces that branch (a POSIX driver would otherwise be killed by group and never ask for taskkill).
        for label, patch, windows_branch in (("host taskkill", mock.patch.object(subprocess, "Popen", real_popen), contextlib.nullcontext()),
                                             ("taskkill missing", mock.patch.object(subprocess, "Popen", without_taskkill), _windows_kill_path())):
            with self.subTest(label), patch, windows_branch:
                with mock.patch(f"{__name__}.PWSH_SCRIPT_BASE_SEC", 3):
                    with self.assertRaises(AssertionError) as caught:
                        _run_pwsh_script("Start-Sleep -Seconds 600\n")
                message = str(caught.exception)
                self.assertIn("test_r16_c1_a_genuine_hang_still_fails_by_case_name_at_the_budget exceeded its 3 s wall-clock bound", message)
                # pass-7 A6: the kill wording is read from the verdict line only; the stdout / stderr tails that follow it are the child's text
                verdict_line = message.split("\n", 1)[0]
                self.assertTrue(any(wording in verdict_line for wording in _hang_verdict_wordings(label, host_is_posix=_IS_POSIX)), message)
                if label == "taskkill missing":
                    self.assertIn("kill is NOT confirmed (taskkill failed: FileNotFoundError)", verdict_line)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_factory_that_consumes_the_bound_before_returning_a_completed_task_is_a_timeout_not_a_late_success(self) -> None:
        # sol r9 B1 (catalogue B-01): an expired budget used to become Wait(0), and an already-completed task then returned success even
        # though its factory had spent the whole bound. The deadline is measured from the call's start: elapsed >= bound is a timeout.
        script = (_accept_region() + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"   # native path: a drive-letter literal is not split on Linux (r11)
                  "$slowText = { param($p) Start-Sleep -Milliseconds 300; [Threading.Tasks.Task]::FromResult('payload') }\n"
                  "$slowDel = { param($p) Start-Sleep -Milliseconds 300; [Threading.Tasks.Task]::FromResult('') }\n"
                  "$fastText = { param($p) [Threading.Tasks.Task]::FromResult('payload') }\n"
                  "$fastDel = { param($p) [Threading.Tasks.Task]::FromResult('') }\n"
                  "$r = Read-CapturedResult $p 50 $slowText\n"
                  "Write-Output ('LATE-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('LATE-DEL|' + (Remove-CaptureFiles @('C:\\x\\a.txt') 50 $slowDel))\n"
                  "$r = Read-CapturedResult $p $AcceptMs $fastText\n"
                  "Write-Output ('FAST-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('FAST-DEL|[' + (Remove-CaptureFiles @('C:\\x\\a.txt') $AcceptMs $fastDel) + ']')\n")
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
        script = (_accept_region() + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"   # native path (r11): see the leaf-name guard test
                  "$slow = { param($p) class SlowText { [string] ToString() { [Threading.Thread]::Sleep(300); return 'payload' } }; "
                  "[Threading.Tasks.Task]::FromResult([object][SlowText]::new()) }\n"
                  "$r = Read-CapturedResult $p 100 $slow\n"
                  "Write-Output ('SLOW-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('SLOW-DEL|' + (Remove-CaptureFiles @('C:\\x\\a.txt') 100 $slow))\n"
                  "$r = Read-CapturedResult $p $AcceptMs $slow\n"
                  "Write-Output ('INSIDE-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('INSIDE-DEL|' + (Remove-CaptureFiles @('C:\\x\\a.txt') $AcceptMs $slow))\n")
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
            # adversary r1 A1: _plain_task_script builds the reads whose timeout text the forced-slow case asserts, so it is leaf-asserting too.
            if ("<read timed out: " in chunk or name == "_plain_task_script") and re.search(r"Read-Captured(?:Result|Text) '[A-Za-z]:\\\\", chunk):
                offenders.append(name)
        self.assertEqual(offenders, [], "a leaf-asserting test feeds Read-Captured* a drive-letter literal; use a Join-Path temp path")

    @staticmethod
    def _plain_task_script(bound_ms: int | str, factory_delay_ms: int = 0, region: str | None = None) -> str:
        """The plain-completed-task arms at `bound_ms` (a number, or the name of a pwsh variable such as `$AcceptMs`). `factory_delay_ms` is the
        forced-slow seam: every factory spends that long before it hands back its task, the way a loaded runner spends it starting the worker
        and finishing the call. `region` replaces the default accept region (r3 G2: the raw region with a patched-down maximum)."""
        slow = f"Start-Sleep -Milliseconds {factory_delay_ms}; " if factory_delay_ms else ""
        # The reads take a NATIVE path (Join-Path): the reader names the leaf with GetFileName, which is 'a.txt' for 'C:\x\a.txt' on Windows but
        # the whole literal on Linux (the r11 leaf-name rule), and the forced-slow case asserts that leaf in its timeout text.
        return ((region if region is not None else _accept_region()) + "\n"
                "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                f"$plain = {{ param($p) {slow}[Threading.Tasks.Task]::CompletedTask }}\n"
                f"$ran = {{ param($p) {slow}[Threading.Tasks.Task]::Delay(1) }}\n"
                f"Write-Output ('PLAIN-DEL|[' + (Remove-CaptureFiles @('C:\\x\\a.txt') {bound_ms} $plain) + ']')\n"
                f"Write-Output ('RUN-DEL|[' + (Remove-CaptureFiles @('C:\\x\\a.txt') {bound_ms} $ran) + ']')\n"
                f"$r = Read-CapturedResult $p {bound_ms} $plain\n"
                "Write-Output ('PLAIN-READ|' + $r.Ok + '|[' + $r.Text + ']')\n"
                f"$r = Read-CapturedResult $p {bound_ms} $ran\n"
                "Write-Output ('RUN-READ|' + $r.Ok + '|[' + $r.Text + ']')\n")

    def test_r7_g4_the_generated_plain_task_assignment_is_spaced_like_every_other_assignment(self) -> None:
        # Grok G4: the generator line emitted `$plain ={ ...` (no space after `=`) while every sibling line emits `$name = {`.
        script = self._plain_task_script(CAPTURE_ACCEPT_BOUND_MS)
        self.assertIn("$plain = { param($p) [Threading.Tasks.Task]::CompletedTask }\n", script)
        self.assertNotIn("$plain ={", script)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_an_accepted_plain_completed_task_has_no_result_so_no_false_cleanup_problem_and_no_fabricated_text(self) -> None:
        # sol r9 B2: a plain [Task]::CompletedTask exposes the runtime type System.Threading.Tasks.VoidTaskResult as its Result. A non-generic
        # Task has no result: cleanup reports no problem and the reader returns empty text, never the type name.
        # CI-FLAKE-HEAVY-SLOT-GATE-PLAIN-TASK-CLEANUP-5S-1: an ACCEPT arm, so it runs at CAPTURE_ACCEPT_BOUND_MS (see its comment).
        proc = self._pwsh(self._plain_task_script(CAPTURE_ACCEPT_BOUND_MS))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("VoidTaskResult", proc.stdout)
        self.assertIn("PLAIN-DEL|[]", proc.stdout)
        self.assertIn("RUN-DEL|[]", proc.stdout)
        self.assertIn("PLAIN-READ|True|[]", proc.stdout)
        self.assertIn("RUN-READ|True|[]", proc.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_the_plain_task_arms_pass_under_a_forced_slow_seam_only_because_their_bound_cannot_elapse(self) -> None:
        # CI-FLAKE-HEAVY-SLOT-GATE-PLAIN-TASK-CLEANUP-5S-1 RED/GREEN, without a real wait of a second: every factory is forced to spend 400 ms
        # (a loaded runner's start/finish latency, scaled down). Under a bound that latency can exceed (300 ms: the old typed 5 s and equally the
        # r1 bound of 60 s, the helper's own maximum, which a 61 s latency crosses; both scaled to a bound under the seam's 400 ms) the arms are
        # ABANDONED, so the success the accept test asserts was a statement about the runner; under the accept bound (CAPTURE_ACCEPT_BOUND_MS,
        # no helper deadline) the same slow seam still yields every clean verdict. sol r2 B1 made the second half true for 61 s, not only 5 s.
        slow_ms = 400
        self.assertLess(slow_ms, 1000, "a case in this class never performs a real wait of a second")
        red = self._pwsh(self._plain_task_script(300, factory_delay_ms=slow_ms))
        self.assertEqual(red.returncode, 0, red.stdout + red.stderr)
        self.assertIn("PLAIN-DEL|[capture cleanup abandoned after 0.3 s: C:\\x\\a.txt]", red.stdout, "the slow seam must really be able to trip a small bound")
        self.assertIn("PLAIN-READ|False|[<read timed out: a.txt not readable within 300 ms>]", red.stdout)
        green = self._pwsh(self._plain_task_script(CAPTURE_ACCEPT_BOUND_MS, factory_delay_ms=slow_ms))
        self.assertEqual(green.returncode, 0, green.stdout + green.stderr)
        for row in ("PLAIN-DEL|[]", "RUN-DEL|[]", "PLAIN-READ|True|[]", "RUN-READ|True|[]"):
            self.assertIn(row, green.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_r3_g2_a_delay_past_the_helper_maximum_abandons_a_raw_read_and_not_an_accept_arm(self) -> None:
        # r3 (Grok G2), executable and scaled: the helper maximum is patched down to 300 ms, and the factory delay is taken from the bound it
        # must defeat (maximum + margin), never a fixed constant. In the RAW region (r1's seam: the bound IS the helper maximum) a read that
        # delayed past it is ABANDONED, which is the sol r1 escape (61000 ms past 60000 ms), without a real wait of a second. The same delay
        # under _accept_region() (with the same patched maximum, which the seam must override) yields every clean row. A seam that only moved
        # the deadline (the accept bound = the helper maximum) fails the second half.
        maximum_ms, margin_ms = 300, 100
        slow_ms = maximum_ms + margin_ms
        self.assertLess(slow_ms, 1000, "a case in this class never performs a real wait of a second")
        raw = self._pwsh(self._plain_task_script("$CaptureIoMaxMs", factory_delay_ms=slow_ms, region=_raw_region_with_maximum(maximum_ms)))
        self.assertEqual(raw.returncode, 0, raw.stdout + raw.stderr)
        self.assertIn(f"PLAIN-DEL|[capture cleanup abandoned after {maximum_ms / 1000:g} s: C:\\x\\a.txt]", raw.stdout, "the delay must cross the patched maximum")
        self.assertIn(f"PLAIN-READ|False|[<read timed out: a.txt not readable within {maximum_ms} ms>]", raw.stdout)
        accept = self._pwsh(self._plain_task_script("$AcceptMs", factory_delay_ms=slow_ms, region=_accept_region(helper_max_ms=maximum_ms)))
        self.assertEqual(accept.returncode, 0, accept.stdout + accept.stderr)
        for row in ("PLAIN-DEL|[]", "RUN-DEL|[]", "PLAIN-READ|True|[]", "RUN-READ|True|[]"):
            self.assertIn(row, accept.stdout)

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_r5_b1_the_raw_g2_arm_carries_the_same_extended_worker_start_seam_as_every_accept_arm(self) -> None:
        # sol r4 B1: the G2 control's RAW arm kept the 30 s worker-start wait ($CaptureWorkerStartMs), so a runner that started the worker in 31 s
        # failed G2 with "the capture worker was not ready within 30000 ms" before the forced-slow operation ran: the runner-speed dependency
        # G2 exists to remove. The raw arm now extends ONLY the start wait (its helper maximum stays the patched-down one: that is the
        # subject), exactly as _accept_region does. Source half: the LAST assignment in each region is the accept bound.
        for label, region in (("raw", _raw_region_with_maximum(300)), ("accept", _accept_region(helper_max_ms=300))):
            self.assertEqual(re.findall(r"\$CaptureWorkerStartMs = (\d+)", region)[-1], str(CAPTURE_ACCEPT_BOUND_MS), label)
        # Executed half, DETERMINISTIC (adversary r5 A7: a 1 ms start wait is no control, because Start-CaptureWorker tests the ready FILE before
        # the clock, so a worker published before the parent is scheduled again skips the deadline): a real pwsh reports the values the helpers
        # will actually read once the region is dot-sourced, the raw arm's maximum staying the patched-down one and only its start wait moving.
        probe = "\nWrite-Output ('SEAM|' + $CaptureWorkerStartMs + '|' + $CaptureIoMaxMs)\n"
        for label, region, expected in (("raw", _raw_region_with_maximum(300), f"SEAM|{CAPTURE_ACCEPT_BOUND_MS}|300"),
                                        ("raw with a start wait", _raw_region_with_maximum(300, worker_start_ms=1234), "SEAM|1234|300"),
                                        ("accept", _accept_region(helper_max_ms=300), f"SEAM|{CAPTURE_ACCEPT_BOUND_MS}|{CAPTURE_ACCEPT_BOUND_MS}")):
            proc = self._pwsh(region + probe)
            self.assertEqual(proc.returncode, 0, label + proc.stdout + proc.stderr)
            self.assertIn(expected, proc.stdout, label)
        # and the worker-start check the seam feeds is the one Start-CaptureWorker enforces (the r15 B3 source pin, repeated where G2 lives)
        fifo = FIFO_TEST.read_text(encoding="ascii")
        begin = fifo.index("function Start-CaptureWorker")
        self.assertIn("-ge $CaptureWorkerStartMs", fifo[begin:fifo.index("function Get-CaptureTaskResult", begin)])

    def test_r3_g1_one_accept_call_is_charged_no_less_than_the_r1_ceiling_by_the_whole_script_budget(self) -> None:
        # r3 (Grok G1): the whole-script budget is the one deadline an accept arm still has (run_bounded kills the tree there). r2 charged a call
        # whose bound is a variable the worker start bound only (95 s), below r1's 125 s. A script with exactly one `$AcceptMs` call is pinned to
        # the named accept ceiling, so the charge cannot shrink silently again; so is a generated script that embeds the accept bound's value.
        self.assertGreaterEqual(CAPTURE_ACCEPT_CALL_CEILING_MS, capture_call_ceiling_ms(CAPTURE_IO_MAX_MS))
        self.assertGreaterEqual(CAPTURE_ACCEPT_CALL_CEILING_MS, 125_000, "r1's per-call charge for the helper's 60 s maximum")
        self.assertGreater(CAPTURE_ACCEPT_CALL_CEILING_MS, capture_call_ceiling_ms(CAPTURE_WORKER_START_MS), "r2's per-call charge (95 s), which was too small")
        region = "function Read-CapturedResult { }\n# END bounded capture io\n"
        one = region + "$r = Read-CapturedResult $p $AcceptMs $f\n"
        self.assertEqual(pwsh_script_budget_sec(one), PWSH_SCRIPT_BASE_SEC + CAPTURE_ACCEPT_CALL_CEILING_MS / 1000)
        self.assertEqual(pwsh_script_budget_sec(region + "$n = Remove-CaptureFiles @($p) $AcceptMs $f\n"), PWSH_SCRIPT_BASE_SEC + CAPTURE_ACCEPT_CALL_CEILING_MS / 1000)
        generated = region + f"$r = Read-CapturedResult $p {CAPTURE_ACCEPT_BOUND_MS} $f\n"
        self.assertEqual(pwsh_script_budget_sec(generated), PWSH_SCRIPT_BASE_SEC + CAPTURE_ACCEPT_CALL_CEILING_MS / 1000)
        two = one + "$r = Read-CapturedResult $p $AcceptMs $f\n"
        self.assertEqual(pwsh_script_budget_sec(two), PWSH_SCRIPT_BASE_SEC + 2 * CAPTURE_ACCEPT_CALL_CEILING_MS / 1000)

    def test_r10_two_capture_calls_on_one_line_are_two_call_sites_each_charged_its_own_ceiling(self) -> None:
        # sol r9 B2: the call scan took the REST of the line as one call's arguments, so a line that makes two accept calls was charged one ceiling
        # (245 s instead of 370 s): two calls that each completed within their promised allowance were cut off by the outer run_bounded budget.
        region = "function Read-CapturedResult { }\n# END bounded capture io\n"
        accept = CAPTURE_ACCEPT_CALL_CEILING_MS
        same_line = region + "$r = Read-CapturedResult $p $AcceptMs $f; Remove-CaptureFiles @($p) $AcceptMs $f\n"
        self.assertEqual(pwsh_script_budget_sec(same_line), PWSH_SCRIPT_BASE_SEC + 2 * accept / 1000)
        self.assertEqual(PWSH_SCRIPT_BASE_SEC + 2 * accept / 1000, 120 + 2 * 125, "the report's figure: 120 s base + 2 x 125 s")
        self.assertEqual(pwsh_script_budget_sec(region + "Write-Output ((Read-CapturedResult $p $AcceptMs $f).Ok + (Remove-CaptureFiles @($p) $AcceptMs $f))\n"),
                         PWSH_SCRIPT_BASE_SEC + 2 * accept / 1000, "a call nested in a statement is a call site as well")
        mixed = region + "$r = Read-CapturedResult $p 300 $f; $n = Remove-CaptureFiles @($p) $AcceptMs $f\n"
        self.assertEqual(pwsh_script_budget_sec(mixed), PWSH_SCRIPT_BASE_SEC + (capture_call_ceiling_ms(300) + accept) / 1000,
                         "each call of the line is read for its OWN bound, not the line's largest")
        self.assertEqual(pwsh_script_budget_sec(region + "$a = Read-CapturedResult $p 300 $f; $b = Read-CapturedResult $p 300 $f\n"),
                         PWSH_SCRIPT_BASE_SEC + 2 * capture_call_ceiling_ms(300) / 1000)
        # pass-7 A3: the scan reads a command, not a line. A helper's name inside a quoted path is no call; a backtick continuation keeps its bound; a
        # number after the statement end is no bound; a named bound is the bound.
        one_call = PWSH_SCRIPT_BASE_SEC + accept / 1000
        quoted = region + "$r = Read-CapturedResult '" + "-".join(["Read-CapturedText"] * 11) + "' $AcceptMs $f\n"
        self.assertEqual(pwsh_script_budget_sec(quoted), one_call, "eleven names inside one quoted path are one call, not twelve")
        self.assertEqual(pwsh_script_budget_sec(region + "$t = Read-CapturedText $good `\n  $AcceptMs\n"), one_call, "the bound on a continuation line is read")
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult $p 300 $f; $later = 60000\n"),
                         PWSH_SCRIPT_BASE_SEC + capture_call_ceiling_ms(300) / 1000, "a number after the `;` is not the call's bound")
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult -Path $p -BoundMs $AcceptMs -Start $f\n"), one_call)
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult $p -B 300\n"), PWSH_SCRIPT_BASE_SEC + capture_call_ceiling_ms(300) / 1000)
        self.assertEqual(pwsh_script_budget_sec(region + f"$r = Read-CapturedResult $p {CAPTURE_ACCEPT_BOUND_MS} $f\n"), one_call, "the accept value is the accept bound")
        # pass-7b A1: code inside a double-quoted string is a call; a quoted invocation is a call; a block comment between arguments is whitespace
        self.assertEqual(pwsh_script_budget_sec(region + 'Write-Output "R|$(Read-CapturedResult $p 60000 $f)"\n'), one_call, "an interpolated call is charged")
        self.assertEqual(pwsh_script_budget_sec(region + "& 'Read-CapturedResult' $p $AcceptMs $f\n"), one_call, "a quoted invocation is charged")
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult $p <# note #> 60000 $f\n"), one_call, "the comment is no argument")
        # pass-7b A2: a numeric bound written as an expression is charged for its value; an expression the scan cannot evaluate, the helper's maximum
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult $p ([int]'60000') $f\n"), one_call)
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult $p (60 * 999) $f\n"), PWSH_SCRIPT_BASE_SEC + capture_call_ceiling_ms(59940) / 1000)
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult $p ([int]$x) $f\n"), one_call, "an unknown expression is charged the maximum")
        self.assertEqual(pwsh_script_budget_sec(region + "$r = Read-CapturedResult $p $b $f\n"), PWSH_SCRIPT_BASE_SEC + capture_call_ceiling_ms(CAPTURE_WORKER_START_MS) / 1000,
                         "a plain variable is still the worker start bound")
        # pass-7 A4: a comment between a loop head and its brace hides nothing, and a brace in a string closes no block
        commented = region + "foreach ($i in 1..9) # read\n{ $r = Read-CapturedResult $p $AcceptMs $f }\n"
        with self.assertRaises(AssertionError, msg="a call in a commented loop head still needs its repeat"):
            pwsh_script_budget_sec(commented)
        braced = region + "foreach ($i in 1..9) {\n  Write-Output '}'\n  $r = Read-CapturedResult $p $AcceptMs $f\n}\n"
        with self.assertRaises(AssertionError, msg="a quoted brace is no end of the loop"):
            pwsh_script_budget_sec(braced)

    def test_the_accept_bound_is_no_deadline_a_runner_delay_can_cross(self) -> None:
        # CI-FLAKE-HEAVY-SLOT-GATE-PLAIN-TASK-CLEANUP-5S-1 r2 (sol r1 blocker): 5 s -> 60 s only moved the real deadline. A success verdict that
        # is measured against ANY real deadline of the helper is still a statement about the runner (a task delayed 61000 ms under a 60000 ms
        # bound is abandoned). The accept arms therefore run with the seam of _accept_region: the bound and the worker-start wait are both raised
        # past anything a case can wait out, so a slow success is never turned red by the helper. The one deadline left is the outer run_bounded
        # budget (pwsh_script_budget_sec): a hang is still a named failure of the case there.
        self.assertGreater(CAPTURE_ACCEPT_BOUND_MS, CAPTURE_IO_MAX_MS, "the accept bound is the helper's maximum, which is itself a real deadline")
        self.assertLessEqual(CAPTURE_ACCEPT_BOUND_MS, 2**31 - 1, "an [int] bound: Task.Wait / WaitForExit take an Int32")
        seam = _accept_region()
        self.assertIn(f"$CaptureIoMaxMs = {CAPTURE_ACCEPT_BOUND_MS}", seam)
        self.assertIn(f"$CaptureWorkerStartMs = {CAPTURE_ACCEPT_BOUND_MS}", seam)
        self.assertIn(f"$AcceptMs = {CAPTURE_ACCEPT_BOUND_MS}", seam)
        self.assertGreater(seam.rindex("$CaptureIoMaxMs = "), seam.index("$CaptureIoMaxMs = " + str(CAPTURE_IO_MAX_MS)), "the override must come AFTER the region defines the variable")

    def test_no_accept_arm_of_the_bounded_capture_io_cases_carries_a_wall_clock_dependent_bound(self) -> None:
        # CI-FLAKE-HEAVY-SLOT-GATE-PLAIN-TASK-CLEANUP-5S-1 guard, source pin (every OS). In this class a bound of a second or more on a
        # Read-Captured* / Remove-CaptureFiles call is an ACCEPT arm (a success is asserted against it) and must be CAPTURE_ACCEPT_BOUND_MS: a
        # typed 5 s / 10 s / 20 s lets a loaded runner turn the success into an abandonment. The timeout cases use bounds under a second.
        # And no case of the class performs a real wait of a second or more: a Start-Sleep / Thread.Sleep / Task.Delay / time.sleep literal of
        # 1000 ms or more is allowed only where it is never waited out (a factory killed at its small bound, a hang killed at its budget, a
        # deliberately slow worker start) and is named in _ALLOWED_LONG_SLEEPS with the reason.
        source = Path(__file__).read_text(encoding="utf-8")
        start = source.index("class BoundedCaptureIoTests(")
        body = source[start:source.index("\nclass BoundedCleanupTests(")]
        bounds = []
        # pass-7 A2: the PowerShell a case builds is read from the parsed source (string literals, not source lines), so prose, docstrings and
        # source pins are no calls, and the calls are read by the lexer of ps_capture_calls.
        tree = ast.parse(source)
        in_class = [node for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name == "BoundedCaptureIoTests"
                    for node in cls.body if isinstance(node, ast.FunctionDef)]
        # sol r9 B1 sweep: one case outside the class makes real accept calls too (its other lines are source pins), so it is read as well
        outside = [node for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name != "BoundedCaptureIoTests"
                   for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "test_the_fifo_suite_reads_its_captured_output_under_a_bound"]
        self.assertGreater(len(in_class), 20, "the guarded class must be found")
        self.assertEqual(len(outside), 1, "the guarded case outside the class must exist exactly once")
        for function in in_class + outside:
            name = function.name
            if name.startswith("test_no_accept_arm_of_the_bounded_capture_io_cases") or name in self._LONG_BOUND_EXEMPT:
                continue
            # sol r2 B1: NO typed number of a second or more is an accept arm's bound, the helper's own maximum included (60000 was accepted here
            # and is still a real deadline). An accept arm passes `$AcceptMs` (or CAPTURE_ACCEPT_BOUND_MS from Python). sol r9 B1: a call with NO
            # bound and a hex or suffixed literal are the same defect (capture_call_bound_offenders reads every PowerShell spelling).
            for fragment in python_ps_fragments(function):
                bounds.extend(f"{name}: {offender}" for offender in capture_call_bound_offenders(fragment))
            # a generator call (_plain_task_script) carries its bound as a Python argument
            for node in ast.walk(function):
                if isinstance(node, ast.Call) and (getattr(node.func, "attr", None) or getattr(node.func, "id", None)) == "_plain_task_script":
                    argument = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "bound_ms"), None)
                    if isinstance(argument, ast.Constant):
                        number = argument.value if isinstance(argument.value, (int, float)) and not isinstance(argument.value, bool) else _ps_number_value(str(argument.value))
                        if number is not None and round(number) >= 1000:
                            bounds.append(f"{name}: _plain_task_script({argument.value!r})")
        self.assertEqual(bounds, [], "an accept arm passes a typed bound; use $AcceptMs (the seam of _accept_region), which no helper deadline can end")
        waits = []
        for chunk in re.split(r"\n    (?:@[^\n]*\n    )*def ", body):
            name = chunk.split("(", 1)[0]
            if name.startswith("test_no_accept_arm_of_the_bounded_capture_io_cases") or name in self._ALLOWED_LONG_SLEEPS:
                continue
            for line in chunk.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                for match in re.finditer(r"(?i)Start-Sleep\s+-Milliseconds\s+(\d+)|Start-Sleep\s+-Seconds\s+(\d+)|Sleep\s*\(\s*(\d+)\s*\)|Task\]::Delay\s*\(\s*(\d+)\s*\)|time\.sleep\s*\(\s*([\d.]+)\s*\)"
                                         r"|factory_delay_ms=(\d+)", line):
                    ms, sec, thread_ms, delay_ms, py_sec, seam_ms = match.groups()
                    millis = (int(ms) if ms else int(sec) * 1000 if sec else int(thread_ms) if thread_ms else int(delay_ms) if delay_ms
                              else int(seam_ms) if seam_ms else float(py_sec) * 1000)
                    if millis >= 1000 and "$sw.ElapsedMilliseconds -lt" not in line:
                        waits.append(f"{name}: {line.strip()[:90]}")
        self.assertEqual(waits, [], "a BoundedCaptureIoTests case waits a second or more for real; name it in _ALLOWED_LONG_SLEEPS with why it is never waited out")

    def test_no_accept_arm_of_the_bounded_capture_io_cases_guard_flags_bare_hex_and_suffixed_bounds(self) -> None:
        # sol r9 B1: the guard's own catalogue. A bare call (real 10 s default), a hex literal and a suffixed literal were all passed by the
        # decimal-only guard; the accept shapes and the sub-second timeout shapes must stay clean.
        # pass-7 A2 adds the named forms (the omitted bound binds the real 10 s default), a bare path, a binary literal, 999.6 (PowerShell rounds it to
        # 1000), a bound above the old 120000 cap, a quoted number and an expression.
        flagged = ("Remove-CaptureFiles @($real)", "Read-CapturedResult $p", "Read-CapturedText 'C:\\x\\a.txt'",
                   "Remove-CaptureFiles @($real) 0x1388 $f", "Read-CapturedResult $p 0xEA60 $f", "Read-CapturedResult $p 5000 $f",
                   "Remove-CaptureFiles @($real) 10000L $f", "Read-CapturedResult $p 5kb $f", "Read-CapturedResult $p 5e3 $f",
                   "Write-Output ('X|' + (Remove-CaptureFiles @($real)) + '|' + (Test-Path -LiteralPath $real))",
                   "Read-CapturedText -Path $good", "Remove-CaptureFiles -Paths @($real)", "Read-CapturedResult a.txt", "Read-CapturedResult -Path:a.txt",
                   "Read-CapturedResult $p 0b1001110001000 $f", "Read-CapturedResult $p 999.6 $f", "Read-CapturedResult $p 200000 $f",
                   "Read-CapturedResult $p '5000' $f", "Read-CapturedResult $p (5 * 1000) $f", "Read-CapturedResult -Path $p -BoundMs 5000",
                   "Remove-CaptureFiles @($real) -B 12000", "Read-CapturedResult $p `\n  5000 $f",
                   # pass-7b A1 / A2: executable code inside a string, a quoted invocation, a block comment between arguments, and bound expressions
                   'Write-Output "R|$(Read-CapturedResult $p 60000 $f)"', "& 'Read-CapturedResult' $p 5000 $f", "Read-CapturedResult $p <# note #> 60000 $f",
                   "Read-CapturedResult $p ([int]'60000') $f", "Read-CapturedResult $p (60 * 999) $f", "Read-CapturedResult $p ([int]$x) $f",
                   "Read-CapturedResult $p ($x + 1) $f", "Write-Output \"a $(Remove-CaptureFiles @($real))\"")
        for text in flagged:
            with self.subTest(text):
                self.assertNotEqual(capture_call_bound_offenders(text), [], text)
        clean = ("Remove-CaptureFiles @($real) $AcceptMs $f", "Read-CapturedResult $p $AcceptMs $f", "Remove-CaptureFiles @('a', 'b') 300 $hang",
                 "Read-CapturedText 'C:\\x\\a.txt' $b $seam; Write-Output 'x'", "Write-Output ('Y|' + (Remove-CaptureFiles @($real) $AcceptMs $f) + ']')",
                 "# Remove-CaptureFiles @($real) is only prose", "function Read-CapturedResult([string]$Path) { }",
                 "Read-CapturedResult -Path $p -BoundMs $AcceptMs", "Remove-CaptureFiles -Paths @($real) -BoundMs $AcceptMs",
                 "Read-CapturedResult $p $AcceptMs $f; $later = 60000", "Read-CapturedResult $p 999.4 $f",
                 "Read-CapturedResult '" + "-".join(["Read-CapturedText"] * 3) + "' $AcceptMs $f", "Read-CapturedText $good `\n  $AcceptMs",
                 "Write-Output 'Read-CapturedResult a.txt' # Remove-CaptureFiles @($x)",
                 'Write-Output "R|$(Read-CapturedResult $p $AcceptMs $f)"', "& 'Read-CapturedResult' $p $AcceptMs $f",
                 "Read-CapturedResult $p <# 5000 #> $AcceptMs $f", "Read-CapturedResult $p (30 * 10) $f", "Read-CapturedResult $p ([int]'300') $f",
                 'Write-Output "Read-CapturedResult a.txt $x"', "<# Read-CapturedResult a.txt #> Write-Output 1")
        for text in clean:
            with self.subTest(text):
                self.assertEqual(capture_call_bound_offenders(text), [], text)
        self.assertEqual(len(capture_call_bound_offenders("Read-CapturedResult $p; Remove-CaptureFiles @($p)")), 2, "both calls of one line are read")
        # the Python side: prose, a docstring and a source pin are no PowerShell; a literal and an f-string are
        source = ("class C:\n    def case(self):\n        'Read-CapturedResult and Remove-CaptureFiles is prose'\n"
                  "        self.assertIn('Read-CapturedResult $outF', suite)\n"
                  "        run = ('Remove-CaptureFiles @($real) $AcceptMs\\n' f'Read-CapturedText {x}\\n')\n")
        fragments = python_ps_fragments(ast.parse(source).body[0].body[0])  # type: ignore[attr-defined]
        self.assertEqual(len(fragments), 1, fragments)
        self.assertEqual([call.name for call in ps_capture_calls(fragments[0])], ["Remove-CaptureFiles", "Read-CapturedText"])
        self.assertEqual(len(capture_call_bound_offenders(fragments[0])), 1, "the f-string call has a path placeholder and no bound")
        # pass-7b A3: a script built with `+` is one text, so a quote opened in one literal and closed in the next hides nothing
        concatenated = ("class C:\n    def case(self):\n        s = '$good = \\'' + good + '\\'\\nRead-CapturedText $good 5000\\n'\n")
        fragments = python_ps_fragments(ast.parse(concatenated).body[0].body[0])  # type: ignore[attr-defined]
        self.assertEqual(fragments, ["$good = '$PYTHON_EXPR'\nRead-CapturedText $good 5000\n"])
        self.assertEqual(len(capture_call_bound_offenders(fragments[0])), 1, "the restored 5 s bound after a concatenated path is seen")

    # Cases that legitimately carry a sleep literal of a second or more, because it is never waited out: the worker is killed at the case's small
    # bound, or the hang is killed at its budget (the reason is the case's own comment). A case NOT listed here may not add one.
    _ALLOWED_LONG_SLEEPS = frozenset({
        "test_r16_c1_a_script_whose_permitted_slow_worker_starts_outlast_the_old_fixed_cap_still_passes",  # the scaled slow-start case: its 2 s start delay IS the subject
        "test_r16_c1_a_genuine_hang_still_fails_by_case_name_at_the_budget",  # a 600 s sleep killed at a 3 s budget
        "test_a_synchronous_factory_or_result_conversion_that_outlasts_the_bound_returns_the_named_timeout_inside_a_generous_ceiling",  # Sleep(ceiling+slack) killed at a 50 ms bound
        "test_a_factory_that_never_returns_still_gets_the_named_timeout_and_the_process_still_exits",  # Sleep(Infinite) killed at a 200 ms bound
        "test_a_result_conversion_that_never_returns_still_gets_the_named_timeout_and_the_process_still_exits",
        # a 600 s sleeper the factory starts so the teardown has a descendant to reap; the case polls until it is gone and never waits it out
        "test_a_process_orphaned_by_an_ended_intermediate_still_ends_with_the_worker",
        "test_a_process_the_factory_started_ends_with_the_worker_even_when_the_worker_ended_first",
        "_run_worker_driver",
    })
    # Cases whose long literal is not an accept arm's bound: the budget arithmetic feeds the text of a call to a PURE function (nothing runs).
    # (sol r2 B1: the orphan case was exempt for its typed 40 s bound; it is an accept arm like the rest and now passes $AcceptMs.)
    _LONG_BOUND_EXEMPT = frozenset({
        "test_r16_c1_the_whole_script_budget_is_derived_from_the_calls_the_script_makes",
        "test_r3_g1_one_accept_call_is_charged_no_less_than_the_r1_ceiling_by_the_whole_script_budget",  # pins the budget arithmetic; `/ 1000` is ms -> s, not a bound
        "test_r10_two_capture_calls_on_one_line_are_two_call_sites_each_charged_its_own_ceiling",  # feeds call text to the PURE budget function; nothing runs
    })

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_task_that_derives_from_task_of_string_keeps_its_result_in_both_callers(self) -> None:
        # sol r11 B1: Get-CaptureTaskResult asked the RUNTIME type whether it is generic. A class derived from Task<string> is not
        # (IsGenericType is False) yet carries a real result, so the reader returned Ok with empty text and the cleanup reported success
        # over 'still present: a.txt'. The generic base is now found by walking the BaseType chain. The ordinary Task<string> is the control.
        # r13 (sol r12 B1): the derived type is defined inside each factory (the factory runs in a worker process of its own).
        derived_type = "class DerivedTextTask : System.Threading.Tasks.Task[string] { DerivedTextTask([Func[string]]$f) : base($f) {} }; "
        script = (_accept_region() + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$derived = { param($p) " + derived_type + "$t = [DerivedTextTask]::new([Func[string]]{ 'payload' }); $t.RunSynchronously(); $t }\n"
                  "$derivedDel = { param($p) " + derived_type + "$t = [DerivedTextTask]::new([Func[string]]{ 'still present: a.txt' }); $t.RunSynchronously(); $t }\n"
                  "$plain = { param($p) [Threading.Tasks.Task]::FromResult('payload') }\n"
                  "$plainDel = { param($p) [Threading.Tasks.Task]::FromResult('still present: a.txt') }\n"
                  "Write-Output ('DERIVED-TYPE|' + (& $derived $p).GetType().IsGenericType)\n"
                  "$r = Read-CapturedResult $p $AcceptMs $derived\n"
                  "Write-Output ('DERIVED-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('DERIVED-DEL|' + (Remove-CaptureFiles @($p) $AcceptMs $derivedDel))\n"
                  "$r = Read-CapturedResult $p $AcceptMs $plain\n"
                  "Write-Output ('PLAIN-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('PLAIN-DEL|' + (Remove-CaptureFiles @($p) $AcceptMs $plainDel))\n")
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
        # sol r4 B3: one script per text (three worker calls each), not one 18-call loop: that script's derived budget was above what a hosted
        # ubuntu job can wait for, so a hung capture ended with the job's cancellation and no case verdict. _run_pwsh_script now refuses it.
        for i in range(6):
            script = (_accept_region() + "\n"
                      "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                      "$texts = @('2026-11-04T12:00:00Z', '{\"a\":1}', ' padded ', 'true', ('nul' + [char]0 + 'x'), ([string][char]0x00e9 + [char]0xD800 + 'z'))\n"
                      f"$i = {i}\n"
                      "$t = $texts[$i]\n"
                      "$units = ($t.ToCharArray() | ForEach-Object { [int]$_ }) -join ','\n"
                      "$make = '[string]::new([char[]]@(' + $units + '))'\n"
                      "$ok = [scriptblock]::Create('param($p) [Threading.Tasks.Task]::FromResult(' + $make + ')')\n"
                      "$bad = [scriptblock]::Create('param($p) throw ' + $make)\n"
                      "$r = Read-CapturedResult $p $AcceptMs $ok\n"
                      "Write-Output (\"TEXT|$i|\" + ($r.Ok -and $r.Text -ceq $t))\n"
                      "$r = Read-CapturedResult $p $AcceptMs $bad\n"
                      "Write-Output (\"ERROR|$i|\" + ((-not $r.Ok) -and $r.Text -ceq \"<read failed: $t>\"))\n"
                      "Write-Output (\"NOTE|$i|\" + ((Remove-CaptureFiles @($p) $AcceptMs $ok) -ceq \"capture cleanup incomplete: $t\"))\n")
            with self.subTest(text=i):
                proc = self._pwsh(script)  # three worker calls: a budget far inside the hosted job
                self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
                for kind in ("TEXT", "ERROR", "NOTE"):
                    self.assertIn(f"{kind}|{i}|True", proc.stdout, f"{kind} {i}\n{proc.stdout}{proc.stderr}")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_the_worker_hands_the_factory_its_argument_in_the_shape_it_was_given(self) -> None:
        # adversary r13 A2: a one-element string[] reached the cleanup factory as a String (so $paths[0] was 'C'), an empty one as $null.
        script = (_accept_region() + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$shape = { param($x) [Threading.Tasks.Task]::FromResult('type=' + $x.GetType().FullName + ' n=' + @($x).Count + ' items=' + ($x -join '|')) }\n"
                  "$none = { param($x) [Threading.Tasks.Task]::FromResult('null=' + ($null -eq $x) + ' type=' + $(if ($null -ne $x) { $x.GetType().FullName })) }\n"
                  "Write-Output ('ONE|' + (Remove-CaptureFiles @('C:\\x\\a.txt') $AcceptMs $shape))\n"
                  "Write-Output ('TWO|' + (Remove-CaptureFiles @('C:\\x\\a.txt', 'C:\\x\\b.txt') $AcceptMs $shape))\n"
                  "Write-Output ('EMPTY|' + (Remove-CaptureFiles @() $AcceptMs $none))\n"
                  "Write-Output ('SCALAR|' + (Read-CapturedResult $p $AcceptMs $shape).Text)\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("ONE|capture cleanup incomplete: type=System.String[] n=1 items=C:\\x\\a.txt", proc.stdout)
        self.assertIn("TWO|capture cleanup incomplete: type=System.String[] n=2 items=C:\\x\\a.txt|C:\\x\\b.txt", proc.stdout)
        self.assertIn("EMPTY|capture cleanup incomplete: null=False type=System.String[]", proc.stdout)
        self.assertRegex(proc.stdout, r"SCALAR\|type=System\.String n=1 items=.*a\.txt")

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_a_null_element_of_the_argument_reaches_the_factory_as_null(self) -> None:
        # adversary r13 pass-2 A1: the wire kept the null ('-'), but the worker's indexed assignment into its string[] turned it into ''.
        script = (_accept_region() + "\n"
                  "$probe = { param($x) [Threading.Tasks.Task]::FromResult('null0=' + ($null -eq $x[0]) + ' empty1=' + ($x[1] -ceq '') + ' n=' + $x.Count) }\n"
                  "$paths = [string[]]::new(2); $paths[1] = ''\n"
                  "Write-Output ('NULLS|' + (Remove-CaptureFiles $paths $AcceptMs $probe))\n")
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
            script = (_accept_region() + "\n"
                      "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                      "$orphaning ={ param($p) $m = Start-Process -FilePath (Get-Process -Id $PID).Path -NoNewWindow -PassThru "
                      "-ArgumentList @('-NoProfile', '-NonInteractive', '-EncodedCommand', '" + encoded + "'); "
                      "$null = $m.WaitForExit(30000); [Threading.Tasks.Task]::FromResult('orphaned') }\n"
                      "$r = Read-CapturedResult $p $AcceptMs $orphaning\n"
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

            script = (_accept_region() + "\n"
                      "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                      "$throwing = " + spawner(pid_files[0], "throw 'factory exploded'") + "\n"
                      "$finishing = " + spawner(pid_files[1], "[Threading.Tasks.Task]::FromResult('')") + "\n"
                      "$r = Read-CapturedResult $p $AcceptMs $throwing\n"
                      "Write-Output ('THROW-READ|' + $r.Ok + '|' + $r.Text)\n"
                      "Write-Output ('FINISH-DEL|[' + (Remove-CaptureFiles @($p) $AcceptMs $finishing) + ']')\n"
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
        script = ("$callerOnly = 'x'\n" + _accept_region() + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "$callerClass = { param($p) if ($null -eq $callerOnly) { throw 'the caller scope is not visible in the worker' }; [Threading.Tasks.Task]::FromResult('x') }\n"
                  "$noType = { param($p) [NoSuchTypeAnywhere12]::new(); [Threading.Tasks.Task]::FromResult('x') }\n"
                  "$boom = { param($p) throw 'factory exploded' }\n"
                  "$r = Read-CapturedResult $p $AcceptMs $callerClass\n"
                  "Write-Output ('CLASS-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('CLASS-DEL|' + (Remove-CaptureFiles @($p) $AcceptMs $callerClass))\n"
                  "$r = Read-CapturedResult $p $AcceptMs $noType\n"
                  "Write-Output ('NOTYPE-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('NOTYPE-DEL|' + (Remove-CaptureFiles @($p) $AcceptMs $noType))\n"
                  "$r = Read-CapturedResult $p $AcceptMs $boom\n"
                  "Write-Output ('BOOM-READ|' + $r.Ok + '|' + $r.Text)\n"
                  "Write-Output ('BOOM-DEL|' + (Remove-CaptureFiles @($p) $AcceptMs $boom))\n")
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
        script = (_accept_region() + "\n"  # sol r9 B1: the accept seam, never the helper's real 10 s default
                  "$stuck = [IO.Path]::GetTempFileName(); [IO.File]::SetAttributes($stuck, [IO.FileAttributes]::ReadOnly)\n"
                  "Write-Output ('STUCK|' + (Remove-CaptureFiles @($stuck) $AcceptMs) + '|' + (Test-Path -LiteralPath $stuck))\n"
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
        script = (_accept_region() + "\n"  # sol r9 B1: no call of the class names no bound
                  "function Initialize-BoundedCaptureIo { throw 'compile denied' }\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  "try { $r = Read-CapturedResult $p $AcceptMs; Write-Output ('READ|' + $r.Ok + '|' + $r.Text) } catch { Write-Output ('READ-THREW|' + $_.Exception.Message) }\n"
                  "try { Write-Output ('DEL|' + (Remove-CaptureFiles @($p) $AcceptMs)) } catch { Write-Output ('DEL-THREW|' + $_.Exception.Message) }\n")
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
        script = (_accept_region() + "\n"
                  "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                  f"$factory = {sleeper}\n"
                  "Write-Output ('EXE|' + (Get-Process -Id $PID).Path)\n"
                  "$r = Read-CapturedResult $p $AcceptMs $factory\n"
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
        # adversary r5 A5: the budget is DERIVED from the capture calls the driver makes (here one accept call), as _run_pwsh_script does; the typed
        # 240 s it replaces was below what one permitted slow worker start (CAPTURE_ACCEPT_CALL_CEILING_MS) plus the script base allows.
        return _run_argv_bounded([pwsh_exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", script_path], pwsh_script_budget_sec(script), env)

    def _skip_unless_setsid_branch(self) -> None:
        if os.name != "posix":
            self.skipTest("SKIPPED: Start-CaptureWorker's setsid branch exists only on POSIX (os.name=" + os.name + "); hosted Ubuntu CI executes it")
        if not any(os.path.exists(p) for p in ("/usr/bin/setsid", "/bin/setsid")):
            self.skipTest("SKIPPED: no setsid at /usr/bin/setsid or /bin/setsid, so the setsid branch of Start-CaptureWorker is not taken")
        if PWSH is None:
            self.skipTest("SKIPPED: pwsh is not on PATH")

    def test_r5_b2_the_real_worker_drivers_run_under_a_budget_derived_from_their_calls_not_a_typed_240_s(self) -> None:
        # sol r4 B2 (the callers routed through the bounded runner): both real-driver entry points called the raw runner with a typed 240 s, which a valid schedule (one worker start
        # of 241 s, the script ending at 244 s) exceeds although the derived budget for one accept call is longer, and which skipped the hosted preflight.
        module = sys.modules[__name__]
        with tempfile.TemporaryDirectory(prefix="heavy-slot-gate-r5-a5-") as work, \
                mock.patch.object(module, "_run_argv_bounded", return_value=subprocess.CompletedProcess([], 0, "", "")) as bounded:
            self._run_worker_driver("pwsh", work)
            script = Path(work, "driver.ps1").read_text(encoding="utf-8")
        self.assertEqual(bounded.call_args.args[1], pwsh_script_budget_sec(script))
        self.assertGreaterEqual(bounded.call_args.args[1], PWSH_SCRIPT_BASE_SEC + CAPTURE_ACCEPT_CALL_CEILING_MS / 1000)
        escape = Path(__file__).read_text(encoding="utf-8")
        escape = escape[escape.index("\n    def _run_session_escape_case") + 1:]
        escape = escape[:escape.index("\n    def ", 10)] if "\n    def " in escape[10:] else escape
        self.assertIn("pwsh_script_budget_sec(script)", escape, "the session-escape driver passes a typed budget")

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
        home = _run_argv_bounded([PWSH, "-NoLogo", "-NoProfile", "-Command", "[Console]::Out.Write($PSHOME)"], 60).stdout.strip()
        work = tempfile.mkdtemp(prefix="heavy-slot-gate-b2-spaced-")
        try:
            spaced = os.path.join(work, "Power Shell")
            try:
                shutil.copytree(home, spaced, symlinks=True, copy_function=self._link_or_copy)
            except (OSError, shutil.Error) as exc:
                self.skipTest(f"SKIPPED: could not build a pwsh install under a path with a space from {home} ({type(exc).__name__}: {exc})")
            spaced_pwsh = os.path.join(spaced, "pwsh")
            probe = _run_argv_bounded([spaced_pwsh, "-NoLogo", "-NoProfile", "-Command", "(Get-Process -Id $PID).Path"], 120)
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
            script = (_accept_region() + "\n"
                      "$p = Join-Path ([IO.Path]::GetTempPath()) 'a.txt'\n"
                      "$argv = @((ConvertTo-CommandLineArg '/bin/sh'), (ConvertTo-CommandLineArg '" + script_file.replace("'", "''") + "'), "
                      "(ConvertTo-CommandLineArg '" + quoted_pid_file + "'))\n"
                      "$argvLiteral = '(' + (($argv | ForEach-Object { \"'\" + $_.Replace(\"'\", \"''\") + \"'\" }) -join ', ') + ')'\n"
                      "$template = @'\n" + template + "\n'@\n"
                      "$factory = [scriptblock]::Create($template.Replace('@ARGV@', $argvLiteral))\n"
                      "$r = Read-CapturedResult $p $AcceptMs $factory\n"
                      "Write-Output ('READ|' + $r.Ok + '|' + $r.Text)\n")
            script_path = os.path.join(base, "driver.ps1")
            Path(script_path).write_text(script, encoding="utf-8")
            proc = _run_argv_bounded([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", script_path], pwsh_script_budget_sec(script),  # derived (adversary r5 A5)
                                     dict(os.environ, TMPDIR=tmp))
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


class BoundedCleanupTests(_WindowsKillPath, unittest.TestCase):
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
