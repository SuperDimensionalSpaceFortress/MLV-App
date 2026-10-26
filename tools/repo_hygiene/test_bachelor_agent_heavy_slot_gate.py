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
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT / "tools" / "profiling" / "bachelor" / "ultra-magnus-agent.ps1"
FIFO_TEST = ROOT / "tools" / "testing" / "test-ultra-magnus-agent-heavy-slot-fifo.ps1"
PWSH = shutil.which("pwsh")


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
        proc = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(FIFO_TEST), "-UnitOnly"],
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
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


if __name__ == "__main__":
    unittest.main()
