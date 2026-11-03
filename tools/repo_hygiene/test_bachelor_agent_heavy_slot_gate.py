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
# Hang guard for the -UnitOnly run: it must stay above the sum of the fifo script's own per-suite bounds (400 s + 120 s), their two
# bounded taskkills (2 x 60 s), their captured-output reads and capture cleanup (2 x (2 x 10 s + 10 s), sol r6 B1/B2) and its ~30 s of
# unit cases (730 s), so a hung suite fails by its own name there first (a test pins the sum, derived from the script's own constants).
# Measured 349 s locally, 282 s on the hosted runner.
UNIT_RUN_TIMEOUT_SEC = 800
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


def max_wait_and_kill_sec(timeout: float) -> float:
    """The longest the wait plus the tree-kill cleanup of run_bounded(timeout=...) may take (what a virtual clock counts)."""
    return timeout + CLEANUP_BOUND_SEC


def max_run_bounded_sec(timeout: float) -> float:
    """The longest run_bounded(timeout=...) may take before it must have raised: every allowance the helper makes, added up once."""
    return max_wait_and_kill_sec(timeout) + CAPTURE_CLOSE_BOUND_SEC + MAX_READ_SEC + CAPTURE_CLEANUP_BOUND_SEC


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
                problems.append(f"{type(exc).__name__}: {exc}")

    try:
        closer = threading.Thread(target=close, name="close-capture", daemon=True)
        closer.start()
    except Exception as exc:  # noqa: BLE001 - a worker that cannot start must not replace the case-named verdict
        # adversary r8 pass-12 A1: dropping the handles here would let the garbage collector close them synchronously on the caller's
        # thread (traceback-frame clearing), unbounded. They are parked for the life of the process instead.
        _PARKED_CAPTURES.extend(files)
        return f"capture close not started ({type(exc).__name__}: {exc})"
    closer.join(CAPTURE_CLOSE_BOUND_SEC)
    if closer.is_alive():
        return f"capture close abandoned after {CAPTURE_CLOSE_BOUND_SEC:g} s"
    return f"capture close failed: {'; '.join(problems)}" if problems else ""


def _remove_capture_dir(path: str) -> str:
    """Remove the capture directory under CAPTURE_CLEANUP_BOUND_SEC (sol r6 B2); return '' or a note naming the abandoned step.

    The removal runs in a daemon thread that is abandoned if it does not finish, so a stalled temp store (a hung share, a slow
    enumeration, a held handle) cannot hold the verdict back. The note names the step and the path left behind.
    """
    left: dict[str, bool] = {}

    def remove() -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
            left["present"] = os.path.exists(path)  # ignore_errors hides a held or read-only file; the caller is told it stayed (adversary r7 A3)
        except Exception as exc:  # noqa: BLE001 - adversary r8 A2: a worker that dies must not read as a finished removal
            left["error"] = f"{type(exc).__name__}: {exc}"

    try:
        remover = threading.Thread(target=remove, name="rmtree-capture", daemon=True)
        remover.start()
    except Exception as exc:  # noqa: BLE001 - adversary r7 A1: a worker that cannot start must not replace the case-named verdict
        return f"capture cleanup not started ({type(exc).__name__}: {exc}): {path}"
    remover.join(CAPTURE_CLEANUP_BOUND_SEC)
    if remover.is_alive():
        return f"capture cleanup abandoned after {CAPTURE_CLEANUP_BOUND_SEC:g} s: {path}"
    if "error" in left:
        return f"capture cleanup failed ({left['error']}): {path}"
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
        note = f"<read failed: could not start the reader thread ({type(exc).__name__}: {exc})>"
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
        if tolerate_oserror and isinstance(error, OSError):
            return f"<read failed: errno {error.errno} {error.strerror}>"
        if tolerate_oserror and isinstance(error, Exception):
            return f"<read failed: {type(error).__name__}: {error}>"
        raise error  # type: ignore[misc]
    return str(box["text"])


def _kill_taskkill_child(taskkill: subprocess.Popen, problems: list[str]) -> None:
    """A taskkill that did not exit in time is killed and reaped under its own bound; a hung reap is reported, never waited on."""
    try:
        taskkill.kill()
    except OSError as exc:
        problems.append(f"taskkill kill failed: {exc!r}")
    try:
        taskkill.wait(timeout=TASKKILL_REAP_SEC)
    except subprocess.TimeoutExpired:
        problems.append(f"taskkill was still running {TASKKILL_REAP_SEC} s after its kill")
    except OSError as exc:
        problems.append(f"waiting for taskkill failed: {exc!r}")


def _kill_tree(proc: subprocess.Popen) -> str:
    """Kill proc's whole tree and say how it went; a failing cleanup step is reported, never allowed to replace the timeout verdict.

    Every wait below carries a timeout (subprocess.run(timeout=) does not: on expiry CPython kills the child and then waits for it
    with no timeout), so the whole cleanup stays inside CLEANUP_BOUND_SEC.
    """
    problems: list[str] = []
    try:
        taskkill = subprocess.Popen(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(f"taskkill failed: {exc!r}")
    else:
        try:
            returncode = taskkill.wait(timeout=TASKKILL_BOUND_SEC)
            if returncode != 0:
                problems.append(f"taskkill exited {returncode}")
        except subprocess.TimeoutExpired:
            problems.append(f"taskkill failed: it did not exit within {TASKKILL_BOUND_SEC} s")
            _kill_taskkill_child(taskkill, problems)
        except OSError as exc:
            problems.append(f"taskkill failed: {exc!r}")
    try:
        proc.kill()
    except OSError as exc:
        problems.append(f"kill failed: {exc!r}")
    try:
        proc.wait(timeout=ROOT_REAP_SEC)
    except subprocess.TimeoutExpired:
        problems.append(f"the root process was still running {ROOT_REAP_SEC} s after the kill")
    except OSError as exc:
        problems.append(f"waiting for the root process failed: {exc!r}")
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
                if not isinstance(exc, Exception):
                    raise
                failed = True
                verdict = f"could not be waited on ({exc!r})"
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


def _fifo_capture_io_region(fifo: str) -> str:
    """The fifo script's bounded capture io helpers, between their BEGIN / END markers, ready to dot-source in a bare pwsh."""
    begin = fifo.index("# BEGIN bounded capture io")
    return fifo[begin:fifo.index("# END bounded capture io")]


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
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
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
                  "foreach ($t in " + ",".join(str(b) for b in bounds) + ",1) { try { Invoke-BoundedSuite 'case-x' 'C:\\s.ps1' $t } "
                  "catch { Write-Output ('RESULT|' + $t + '|' + $_.Exception.Message + '|' + $script:launched) } }\n")
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
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
                      "Write-Output ('HUNG|' + (Read-CapturedText 'C:\\x\\stdout.txt' 300 $hang) + '|' + ($sw.ElapsedMilliseconds -lt 5000))\n"
                      "Write-Output ('GOOD|' + (Read-CapturedText '" + good.replace("'", "''") + "'))\n"
                      "Write-Output ('MISSING|' + (Read-CapturedText 'C:\\no-such-dir-r6\\x.txt'))\n")
            proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)
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
        self.assertIn("<read failed: errno 13", str(caught))

    def test_a_timeout_keeps_its_case_named_verdict_when_the_stderr_read_fails(self) -> None:
        caught = self._run_failing_read("stderr.txt")
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("stub-hang-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("<read failed: errno 13", str(caught))

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
                self.assertIn(repr(exc), str(caught.exception), label)
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
        self.assertIn("<read failed: could not start the reader thread (RuntimeError: can't start new thread)>", str(caught))
        caught = self._run_with_failing_thread_start("rmtree-capture")
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("thread-start-case exceeded its 3 s wall-clock bound", str(caught))
        self.assertIn("capture cleanup not started (RuntimeError: can't start new thread): ", str(caught))

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
        self.assertIn("capture close failed: OSError: [Errno 5] capture close failed", str(caught))

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
        self.assertIn("capture close not started (RuntimeError: can't start new thread)", str(caught))

    def test_a_capture_close_failure_without_a_timeout_is_a_named_failure_not_a_pass(self) -> None:
        def failing_close() -> None:
            raise OSError(5, "capture close failed")

        caught = self._run_with_capture_close(failing_close, timeout_expires=False)
        self.assertIsInstance(caught, AssertionError, repr(caught))
        self.assertIn("close-case", str(caught))
        self.assertIn("capture close failed: OSError: [Errno 5] capture close failed", str(caught))

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
            self.assertIn("capture close not started (RuntimeError: can't start new thread)", note)
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
            self.assertIn("capture cleanup failed (PermissionError: [Errno 13] audit policy denies shutil.rmtree)", note)
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
    def _pwsh(script: str) -> subprocess.CompletedProcess:
        return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120)

    @unittest.skipUnless(os.name == "nt" and PWSH is not None, "named pipes and pwsh are Windows-only here")
    def test_a_stalled_capture_open_is_abandoned_inside_its_bound_and_named(self) -> None:
        # sol r6 B1: the default reader used to open the file synchronously before its timed wait. Now open + read both run on a pool
        # thread under one bound. A listening pipe nobody writes to blocks the very first read.
        script = (_fifo_capture_io_region(FIFO_TEST.read_text(encoding="ascii")) + "\n"
                  "$srv = [IO.Pipes.NamedPipeServerStream]::new('mlv-r7-stall-" + str(os.getpid()) + "', [IO.Pipes.PipeDirection]::Out, 1)\n"
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  "$r = Read-CapturedText '\\\\.\\pipe\\mlv-r7-stall-" + str(os.getpid()) + "' 400\n"
                  "Write-Output ('STALLED|' + $r + '|' + ($sw.ElapsedMilliseconds -lt 5000))\n"
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
        proc = self._pwsh(script)
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
                  "$sw = [Diagnostics.Stopwatch]::StartNew()\n"
                  "Write-Output ('READ-TWO|' + (Read-CapturedText 'C:\\x\\a.txt' 300 $two))\n"
                  "Write-Output ('READ-NONE|' + (Read-CapturedText 'C:\\x\\a.txt' 300 $none))\n"
                  "Write-Output ('DEL-TWO|' + (Remove-CaptureFiles @('C:\\x\\a.txt') 300 $two))\n"
                  "Write-Output ('DEL-NONE|' + (Remove-CaptureFiles @('C:\\x\\a.txt') 300 $none))\n"
                  "Write-Output ('FAST|' + ($sw.ElapsedMilliseconds -lt 5000))\n")
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
                  "Write-Output ('STALLED|' + (Remove-CaptureFiles @('C:\\x\\a.txt', 'C:\\x\\b.txt') 300 $hang) + '|' + ($sw.ElapsedMilliseconds -lt 5000))\n"
                  "$real = [IO.Path]::GetTempFileName()\n"
                  "Write-Output ('CLEAN|[' + (Remove-CaptureFiles @($real)) + ']|' + (Test-Path -LiteralPath $real))\n")
        proc = self._pwsh(script)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("STALLED|capture cleanup abandoned after 0.3 s: C:\\x\\a.txt, C:\\x\\b.txt|True", proc.stdout)
        self.assertIn("CLEAN|[]|False", proc.stdout)

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

    @unittest.skipUnless(PWSH is not None, "pwsh is not on PATH")
    def test_the_suite_json_is_read_once_under_the_bound_and_every_stalled_step_fails_the_suite_by_name(self) -> None:
        # sol r6 B1/B2 through Invoke-BoundedSuite itself: Start-Process is a stub (no child), the bounded readers and the bounded removal are
        # stubs that report what a stalled step returns. The suite must (a) read each stream once, (b) fail by name when the JSON read
        # stalled, (c) name an abandoned cleanup in a timeout verdict, (d) name the timeout even when both reads stalled.
        fifo = FIFO_TEST.read_text(encoding="ascii")
        start = fifo.index("function ConvertTo-CommandLineArg")
        body = fifo[start:fifo.index("\n}\n", fifo.index("function Invoke-BoundedSuite")) + 3]
        script = ("$RepoRoot = 'C:\\repo'; $agentScript = 'C:\\repo\\agent.ps1'\n"
                  "function Assert-True([bool]$Condition, [string]$Message) { if (-not $Condition) { throw \"FAIL: $Message\" } }\n" + body + "\n"
                  "function Start-Process { param([string]$FilePath, $ArgumentList, [switch]$NoNewWindow, [switch]$PassThru, $RedirectStandardOutput, $RedirectStandardError)\n"
                  "  $fin = if ($FilePath -eq 'taskkill.exe') { $true } else { $script:finish }\n"
                  "  $o = [pscustomobject]@{ Id = 4242; Handle = 1; ExitCode = 0 }\n"
                  "  $o | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value ([scriptblock]::Create('param($ms) $' + $fin.ToString().ToLower()))\n"
                  "  $o | Add-Member -MemberType ScriptMethod -Name Kill -Value { }\n"
                  "  $o }\n"
                  "function Read-CapturedResult([string]$Path) { $script:reads++\n"
                  "  if ($script:stallReads) { [pscustomobject]@{ Ok = $false; Text = '<read timed out: x not readable within 10000 ms>' } } else { [pscustomobject]@{ Ok = $true; Text = '{\"a\":1}' } } }\n"
                  "function Remove-CaptureFiles([string[]]$Paths) { $script:removed += $Paths.Count; foreach ($f in $Paths) { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue }; $script:cleanupNote }\n"
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
