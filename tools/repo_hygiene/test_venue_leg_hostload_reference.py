"""Tests for VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1: a venue leg whose host load is provisional or unknown is never COUNTED.

The defect: on 2026-11-21 the reference leg a134532e (the m16-1457-native-fullscreen-s1 CUDA CONTROL, 59.56 fps) ran with
hostLoad.before/after collected=false, error "collection failed: CimException" in its smoke result.json, so the smoke's own verdict was
state "unknown", provisional true ("never usable as a regression or acceptance signal"). The lease batch classed it QUIET_PRELOAD_OK and
counted it (it reads only the pre-load mean), and nothing showed the hostLoad state, so the 59.56 -> 38.78 fps comparison rested on it.

WHAT IS PINNED (tools/profiling/dual-venue/VenueChain.psm1):
  * ConvertFrom-VenueSmokeHostLoad: the canonical predicate (clean iff state == 'quiet' AND provisional == false), plus: a before/after
    snapshot not collected reads 'unknown' (never 'quiet', never blank); a missing block reads 'unknown', provisional true.
  * Get-VenueLegHostLoad: reads the leg's own <evidence>\\<receiptId>\\result.json (the smoke result) for a playback leg; an export leg
    (export\\summary.json only) is 'not-applicable' and unchanged.
  * Get-VenueReferenceArmVerdict: a comparison whose control/reference leg is provisional prints
    `REFERENCE_PROVISIONAL leg=<id> reason=hostload-<state>` and is INCONCLUSIVE, never a pass or a regression.
Rows (card step 4): (a) CimException reads unknown + provisional; (b) a provisional control refuses, verdict INCONCLUSIVE;
(c) a quiet, non-provisional control is still counted; (d) a missing hostLoad block reads unknown, not quiet.
The batch/chain wiring rows live in test_venue_lease_batch.py and test_venue_chain_runner.py (FAKE_LEG_HOSTLOADS).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "tools" / "profiling" / "dual-venue" / "VenueChain.psm1"
PWSH = shutil.which("pwsh")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

# The real a134532e smoke result's hostLoad block, reduced to the fields the reader looks at (result.json, 2026-11-21).
CIM_FAILED = {
    "schema": "mlvapp-gui-smoke-host-load.v1",
    "before": {"collected": False, "error": "collection failed: CimException"},
    "during": [{"collected": True}],
    "after": {"collected": False, "error": "collection failed: CimException"},
    "state": "unknown",
    "provisional": True,
    "reason": "Host load telemetry could not be collected for 2 of 17 snapshot(s) (before.collected=False, during=15 sample(s), after.collected=False)",
}
QUIET = {"before": {"collected": True}, "after": {"collected": True}, "state": "quiet", "provisional": False, "reason": "peak 18% <= 75%"}


def run_ps(body: str) -> subprocess.CompletedProcess:
    script = f"$ErrorActionPreference = 'Stop'; Import-Module '{MODULE}' -Force -DisableNameChecking; " + body
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=120)


def ps_json(value: object) -> str:
    """A PowerShell expression that yields the JSON value as objects (ConvertFrom-Json), or $null."""
    if value is None:
        return "$null"
    return "(ConvertFrom-Json -NoEnumerate -InputObject '" + json.dumps(value).replace("'", "''") + "')"


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class SmokeHostLoadStandingTests(unittest.TestCase):
    def standing(self, block: object) -> dict:
        p = run_ps(f"ConvertFrom-VenueSmokeHostLoad -HostLoad {ps_json(block)} | ConvertTo-Json -Compress")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return json.loads(p.stdout)

    def test_a_cim_exception_block_reads_unknown_and_provisional(self) -> None:
        s = self.standing(CIM_FAILED)
        self.assertEqual((s["state"], s["provisional"], s["collected"]), ("unknown", True, False), s)
        self.assertIn("CimException", s["reason"], "the collection error is carried, so the report says why")

    def test_a_not_collected_snapshot_with_a_quiet_state_still_reads_unknown(self) -> None:
        # A block whose state says quiet while a before/after snapshot was never collected is not a measurement of a quiet host.
        s = self.standing({**QUIET, "after": {"collected": False, "error": "collection failed: CimException"}})
        self.assertEqual((s["state"], s["provisional"], s["collected"]), ("unknown", True, False), s)

    def test_c_a_quiet_collected_block_stays_quiet_and_not_provisional(self) -> None:
        s = self.standing(QUIET)
        self.assertEqual((s["state"], s["provisional"], s["collected"]), ("quiet", False, True), s)

    def test_d_a_missing_block_reads_unknown_not_quiet(self) -> None:
        s = self.standing(None)
        self.assertEqual((s["state"], s["provisional"], s["collected"]), ("unknown", True, False), s)

    def test_adversary_a1_array_wrapped_values_never_unroll_to_clean(self) -> None:
        # adversary-8c2884e9.json A1, its input: provisional [false] and before.collected [true] read quiet / provisional false / collected true.
        clean = {"state": "quiet", "provisional": False, "before": {"collected": True}, "after": {"collected": True}}
        for block in ({**clean, "provisional": [False], "before": {"collected": [True]}}, {**clean, "provisional": [False]},
                      {**clean, "before": {"collected": [True]}}, {**clean, "state": ["quiet"]}, {**clean, "before": [{"collected": True}]}, [clean]):
            with self.subTest(block=block):
                s = self.standing(block)
                self.assertTrue(s["provisional"], "never clean: provisional stays true")
                if block == {**clean, "provisional": [False]}:
                    self.assertEqual((s["state"], s["collected"]), ("quiet", True), s)   # the state itself was quiet; the leg is provisional
                else:
                    self.assertEqual(s["state"], "unknown", s)

    def test_adversary_a2_only_the_exact_literal_quiet_is_clean(self) -> None:
        # adversary-8c2884e9.json A2, its input: "quiet " (and qui\\u0000et, qui\\u00adet, QUIET) read clean.
        for state in ("quiet ", " quiet", "qui\u0000et", "qui­et", "QUIET", "Quiet", "quiet\n"):
            with self.subTest(state=state):
                s = self.standing({"state": state, "provisional": False, "before": {"collected": True}, "after": {"collected": True}})
                self.assertTrue(s["provisional"], s)
                self.assertNotEqual(s["state"], "quiet", s)

    def test_degenerate_blocks_never_read_clean(self) -> None:
        for block in ({"state": "quiet"}, {"provisional": False}, {"state": "exceeded", "provisional": False}, {"state": "", "provisional": False,
                      "before": {"collected": True}, "after": {"collected": True}}, {"state": "quiet", "provisional": "false", "before": {"collected": True},
                      "after": {"collected": True}}):
            with self.subTest(block=block):
                s = self.standing(block)
                self.assertTrue(s["provisional"], s)
                self.assertNotEqual(s["state"], "", s)


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class LegHostLoadReaderTests(unittest.TestCase):
    def test_the_reader_finds_the_smoke_result_beside_the_playback_summary(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "receipts"
            ev = Path(d) / "evidence"
            cases = {"cim-1": ("play", CIM_FAILED), "quiet-1": ("play", QUIET), "nohl-1": ("play", None), "noresult-1": ("play", "absent"),
                     "exp-1": ("export", None)}
            for rid, (kind, block) in cases.items():
                receipt = root / "CARD-1" / "leg-x" / "bachelor" / f"{rid}.json"
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text("{}", encoding="utf-8")
                folder = ev / rid / ("export" if kind == "export" else "")
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "summary.json").write_text(json.dumps({"cpuQuiescence": {"timeMeanPercent": 5}}), encoding="utf-8")
                if kind == "play" and block != "absent":
                    body = {"schema": "mlvapp-gui-smoke-result.v2"}
                    if block is not None:
                        body["hostLoad"] = block
                    (ev / rid / "result.json").write_text(json.dumps(body), encoding="utf-8")
            body = ("$r = [ordered]@{}; "
                    + "".join(f"$r['{rid}'] = Get-VenueLegHostLoad -ReceiptPath '{root / 'CARD-1' / 'leg-x' / 'bachelor' / (rid + '.json')}'; " for rid in cases)
                    + "$r | ConvertTo-Json -Compress -Depth 4")
            p = run_ps(body)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            got = json.loads(p.stdout)
        self.assertEqual((got["cim-1"]["state"], got["cim-1"]["provisional"]), ("unknown", True), got["cim-1"])
        self.assertEqual((got["quiet-1"]["state"], got["quiet-1"]["provisional"]), ("quiet", False), got["quiet-1"])
        self.assertEqual((got["nohl-1"]["state"], got["nohl-1"]["provisional"]), ("unknown", True), "a smoke result with no hostLoad block is unknown")
        self.assertEqual((got["noresult-1"]["state"], got["noresult-1"]["provisional"]), ("unknown", True), "a playback leg with no result.json is unknown")
        self.assertEqual((got["exp-1"]["state"], got["exp-1"]["provisional"], got["exp-1"]["applies"]), ("not-applicable", False, False),
                         "an export leg runs no GUI smoke: its counting is unchanged")
        self.assertTrue(all(got[k]["applies"] for k in ("cim-1", "quiet-1", "nohl-1", "noresult-1")), got)


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class ReferenceArmVerdictTests(unittest.TestCase):
    def verdict(self, legs: list[tuple[str, object]]) -> tuple[dict, str]:
        arr = ", ".join(f"[pscustomobject]@{{ legId = '{lid}'; hostLoad = {ps_json(block)} }}" for lid, block in legs)
        p = run_ps(f"$v = Get-VenueReferenceArmVerdict -Legs @({arr}); $v.refusals | ForEach-Object {{ Write-Output $_ }}; "
                   "Write-Output ('VERDICT_JSON=' + ($v | ConvertTo-Json -Compress -Depth 5))")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        line = [l for l in p.stdout.splitlines() if l.startswith("VERDICT_JSON=")][-1]
        return json.loads(line[len("VERDICT_JSON="):]), p.stdout

    def test_b_a_provisional_control_leg_refuses_with_the_typed_line_and_is_inconclusive(self) -> None:
        v, out = self.verdict([("a134532e-81fe-496b-889b-c2a0b87d0336", CIM_FAILED), ("q-2", QUIET)])
        self.assertEqual(v["verdict"], "INCONCLUSIVE", v)
        self.assertNotIn(v["verdict"], ("PASS", "REGRESSION", "FAIL"))
        self.assertIn("REFERENCE_PROVISIONAL leg=a134532e-81fe-496b-889b-c2a0b87d0336 reason=hostload-unknown", out)
        self.assertEqual(len(v["refusals"]), 1, v)
        self.assertEqual([l["legId"] for l in v["counted"]], ["q-2"], "the provisional leg is recorded, never counted")
        self.assertEqual([l["legId"] for l in v["legs"]], ["a134532e-81fe-496b-889b-c2a0b87d0336", "q-2"], "every leg stays recorded")

    def test_b_an_exceeded_control_leg_names_its_state(self) -> None:
        _, out = self.verdict([("x-1", {**QUIET, "state": "exceeded", "provisional": True})])
        self.assertIn("REFERENCE_PROVISIONAL leg=x-1 reason=hostload-exceeded", out)

    def test_c_quiet_control_legs_are_counted(self) -> None:
        v, out = self.verdict([("q-1", QUIET), ("q-2", QUIET)])
        self.assertEqual(v["verdict"], "COUNTED", v)
        self.assertEqual(v["refusals"], [], v)
        self.assertNotIn("REFERENCE_PROVISIONAL", out)
        self.assertEqual([l["legId"] for l in v["counted"]], ["q-1", "q-2"])

    def test_d_a_control_leg_with_no_hostload_block_refuses_as_unknown(self) -> None:
        v, out = self.verdict([("old-1", None)])
        self.assertEqual(v["verdict"], "INCONCLUSIVE", v)
        self.assertIn("REFERENCE_PROVISIONAL leg=old-1 reason=hostload-unknown", out)

    def test_adversary_a3_a_supplied_standing_is_never_trusted(self) -> None:
        # adversary-8c2884e9.json A3, its input: a hostLoad carrying applies/collected took the trusted-standing branch and counted an unknown leg.
        for hl in ({"state": "unknown", "provisional": True, "collected": False, "applies": False},
                   {"state": "unknown", "provisional": False, "collected": False, "applies": True},
                   {"state": "quiet", "provisional": False, "collected": False, "applies": True},
                   {"state": "unknown", "provisional": True, "collected": False, "applies": 0},
                   {"state": "unknown", "provisional": True, "collected": False, "applies": None},
                   {"state": "unknown", "provisional": True, "collected": False, "applies": []}):
            with self.subTest(hl=hl):
                v, out = self.verdict([("x", hl)])
                self.assertEqual(v["verdict"], "INCONCLUSIVE", v)
                self.assertIn("REFERENCE_PROVISIONAL leg=x reason=hostload-", out)

    def test_adversary_a4_a_null_leg_is_recorded_and_refused(self) -> None:
        # adversary-8c2884e9.json A4, its input: @($null, <quiet leg>) read COUNTED with the null leg dropped.
        p = run_ps("$q = ('" + json.dumps({"legId": "q", "hostLoad": QUIET}) + "' | ConvertFrom-Json); "
                   "$v = Get-VenueReferenceArmVerdict -Legs @($null, $q); $v.refusals | ForEach-Object { Write-Output $_ }; "
                   "Write-Output ('VERDICT_JSON=' + ($v | ConvertTo-Json -Compress -Depth 5))")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        v = json.loads([l for l in p.stdout.splitlines() if l.startswith("VERDICT_JSON=")][-1][len("VERDICT_JSON="):])
        self.assertEqual(v["verdict"], "INCONCLUSIVE", v)
        self.assertEqual(len(v["legs"]), 2, v)
        self.assertIn("REFERENCE_PROVISIONAL leg=UNKNOWN reason=hostload-unknown", p.stdout)

    def test_adversary_a1_the_reader_never_unrolls_an_array_wrapped_document_or_block(self) -> None:
        # adversary-8c2884e9.json A1 (root-document and hostLoad singleton-array unwrapping): [ {hostLoad: clean} ] and {hostLoad: [clean]} are not clean.
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "receipts"
            ev = Path(d) / "evidence"
            clean = {"state": "quiet", "provisional": False, "before": {"collected": True}, "after": {"collected": True}}
            cases = {"rootarr-1": [{"hostLoad": clean}], "hlarr-1": {"hostLoad": [clean]}, "provarr-1": {"hostLoad": {**clean, "provisional": [False]}}}
            for rid, doc in cases.items():
                receipt = root / "CARD-1" / "leg-x" / "bachelor" / f"{rid}.json"
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text("{}", encoding="utf-8")
                (ev / rid).mkdir(parents=True, exist_ok=True)
                (ev / rid / "summary.json").write_text(json.dumps({"cpuQuiescence": {"timeMeanPercent": 5}}), encoding="utf-8")
                (ev / rid / "result.json").write_text(json.dumps(doc), encoding="utf-8")
            body = ("$r = [ordered]@{}; "
                    + "".join(f"$r['{rid}'] = Get-VenueLegHostLoad -ReceiptPath '{root / 'CARD-1' / 'leg-x' / 'bachelor' / (rid + '.json')}'; " for rid in cases)
                    + "$r | ConvertTo-Json -Compress -Depth 4")
            p = run_ps(body)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            got = json.loads(p.stdout)
        for rid in ("rootarr-1", "hlarr-1"):
            self.assertEqual((got[rid]["state"], got[rid]["provisional"]), ("unknown", True), (rid, got[rid]))
        self.assertTrue(got["provarr-1"]["provisional"], got["provarr-1"])

    def test_no_control_leg_at_all_is_inconclusive(self) -> None:
        v, _ = self.verdict([])
        self.assertEqual(v["verdict"], "INCONCLUSIVE", v)


if __name__ == "__main__":
    unittest.main()
