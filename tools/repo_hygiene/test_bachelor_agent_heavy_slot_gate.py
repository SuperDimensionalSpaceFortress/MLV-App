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

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "tools" / "profiling" / "bachelor" / "ultra-magnus-agent.ps1"
FIFO_TEST = ROOT / "tools" / "testing" / "test-ultra-magnus-agent-heavy-slot-fifo.ps1"
PWSH = shutil.which("pwsh")
# Hang guard for the -UnitOnly run: it must stay above the sum of the fifo script's own per-suite bounds (400 s + 120 s), their two
# bounded taskkills (2 x 60 s) and its ~30 s of unit cases (670 s), so a hung suite fails by its own name there first (a test pins
# the sum). Measured 349 s locally, 282 s on the hosted runner.
UNIT_RUN_TIMEOUT_SEC = 720
FIFO_UNIT_CASES_SEC = 30  # the fifo script's own unit cases, before its two bounded sub-suites


def _read_captured(path: Path, *, tolerate_oserror: bool) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        if not tolerate_oserror:
            raise
        return f"<read failed: errno {exc.errno} {exc.strerror}>"


def _kill_tree(proc: subprocess.Popen) -> str:
    """Kill proc's whole tree and say how it went; a failing cleanup step is reported, never allowed to replace the timeout verdict."""
    problems = []
    try:
        taskkill = subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if taskkill.returncode != 0:
            problems.append(f"taskkill exited {taskkill.returncode}")
    except (OSError, subprocess.SubprocessError) as exc:
        problems.append(f"taskkill failed: {exc!r}")
    try:
        proc.kill()
    except OSError as exc:
        problems.append(f"kill failed: {exc!r}")
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        problems.append("the root process was still running 30 s after the kill")
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
    tmp = tempfile.mkdtemp(prefix="heavy-slot-gate-")
    out_path, err_path = Path(tmp) / "stdout.txt", Path(tmp) / "stderr.txt"
    started = time.monotonic()
    timed_out = False
    kill_note = ""
    try:
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=err)
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_note = _kill_tree(proc)
        elapsed = time.monotonic() - started
        # After a timeout the case-named verdict below is the failure, so a read error (a Windows sharing violation on a file the
        # killed tree still holds) is recorded in the text instead of replacing it. Without a timeout the read error surfaces.
        stdout = _read_captured(out_path, tolerate_oserror=timed_out)
        stderr = _read_captured(err_path, tolerate_oserror=timed_out)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if timed_out:
        raise AssertionError(f"{case} exceeded its {timeout:g} s wall-clock bound (ran {elapsed:.0f} s) and {kill_note}.\n"
                             f"stdout tail: {stdout[-1500:]!r}\nstderr tail: {stderr[-1500:]!r}")
    return proc.returncode, stdout, stderr


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
        worst_case = FIFO_UNIT_CASES_SEC + (quiet + taskkill) + (stage + taskkill)
        self.assertGreater(UNIT_RUN_TIMEOUT_SEC, worst_case,
                           f"the outer guard ({UNIT_RUN_TIMEOUT_SEC} s) must outlast unit cases + both sub-suite bounds + both bounded kills ({worst_case} s)")

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
        self.assertIn("$json = (Get-Content -LiteralPath $outF -ErrorAction Stop) -join \"`n\"", body)
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
        self.assertLess(time.monotonic() - started, 30)
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

    def _run_failing_read(self, failing_name: str, *, timeout_expires: bool = True) -> BaseException:
        proc = self._TimedOutProc()
        if not timeout_expires:
            proc.wait = lambda timeout=None: 0  # type: ignore[method-assign]
        real_read_text = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path.name == failing_name:
                raise PermissionError(13, "sharing violation")
            return real_read_text(path, *args, **kwargs)

        with mock.patch.object(subprocess, "Popen", return_value=proc), \
                mock.patch.object(subprocess, "run"), \
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
        if isinstance(taskkill, BaseException):
            run = mock.Mock(side_effect=taskkill)
        else:
            run = mock.Mock(return_value=subprocess.CompletedProcess(["taskkill"], taskkill))
        with mock.patch.object(subprocess, "Popen", return_value=proc), mock.patch.object(subprocess, "run", run):
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


if __name__ == "__main__":
    unittest.main()
