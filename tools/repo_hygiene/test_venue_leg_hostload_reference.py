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
# r3 (sol r2 B2): an export receipt as the shipped writer writes one (Invoke-VenueLeg.ps1 `if ($isExport)`: scale/display nulled, the export block binds its spec).
REAL_EXPORT_RECEIPT = {"schema": "mlv-app/dual-venue-receipt/v1", "scale": None, "display": None, "evidence": {},
                       "export": {"format": "prores", "maxFrames": 480, "dualIsoExpected": True, "receiptSha256": "2f57f11f95fa9a0eac428f256ad596b1706fe9c9a2f9dfef8411815f970bab6a",
                                  "files": None, "stages": None, "problems": [], "containmentAdvisory": [], "verdict": None}}


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

    def test_adversary2_a3_an_uncollected_interior_sample_is_not_collected(self) -> None:
        # adversary-b0c05520.json A3, its input: a during[] sample collected=false under a quiet/false block read clean.
        s = self.standing({"state": "quiet", "provisional": False, "before": {"collected": True},
                           "during": [{"collected": False, "error": "collection failed: CimException"}], "after": {"collected": True}})
        self.assertEqual((s["state"], s["provisional"], s["collected"]), ("unknown", True, False), s)
        for during in ([{"collected": [True]}], [{"collected": True}, None], "x", [{"error": "no collected field"}]):
            with self.subTest(during=during):
                s = self.standing({**QUIET, "during": during})
                self.assertTrue(s["provisional"], s)
        for during in ([], [{"collected": True}, {"collected": True}]):
            with self.subTest(during=during):
                s = self.standing({**QUIET, "during": during})
                self.assertFalse(s["provisional"], s)

    def test_adversary3_a1_a_during_member_that_is_null_is_not_collected(self) -> None:
        # adversary-90151822.json A1, its input: during=$null beside clean before/after read collected/quiet/false and the arm COUNTED. The smoke always
        # writes during as a list (run-release-gui-smoke.ps1 `during = @($hostLoadDuringSamples)`), so a present null is malformed, never "no samples".
        s = self.standing({**QUIET, "during": None})
        self.assertEqual((s["state"], s["provisional"], s["collected"]), ("unknown", True, False), s)
        p = run_ps("$v = Get-VenueReferenceArmVerdict -Legs @(@{legId='q';hostLoad=@{state='quiet';provisional=$false;before=@{collected=$true};"
                   "during=$null;after=@{collected=$true}}}); Write-Output ('V=' + $v.verdict + ' R=' + ($v.refusals -join '|'))")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("V=INCONCLUSIVE R=REFERENCE_PROVISIONAL leg=q reason=hostload-unknown", p.stdout)
        s = self.standing({k: v for k, v in QUIET.items()})   # an absent during member is unchanged (older smoke results carry none)
        self.assertEqual((s["state"], s["provisional"]), ("quiet", False), s)

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
                folder = ev / rid / ("export" if kind == "export" else "")
                folder.mkdir(parents=True, exist_ok=True)
                (folder / "summary.json").write_text(json.dumps({"cpuQuiescence": {"timeMeanPercent": 5}}), encoding="utf-8")
                rbody: dict = json.loads(json.dumps(REAL_EXPORT_RECEIPT)) if kind == "export" else {"evidence": {}}
                if kind == "play" and block != "absent":
                    body = {"schema": "mlvapp-gui-smoke-result.v2"}
                    if block is not None:
                        body["hostLoad"] = block
                    raw = json.dumps(body).encode("utf-8")
                    (ev / rid / "result.json").write_bytes(raw)
                    import hashlib
                    rbody["evidence"]["resultJsonSha256"] = hashlib.sha256(raw).hexdigest()   # the receipt binds its smoke result (adversary-b0c05520.json A1)
                receipt.write_text(json.dumps(rbody), encoding="utf-8")
            # r6 (sol r5 B2): the export exemption needs the PLANNED leg type; each case passes the type its leg was planned as
            body = ("$r = [ordered]@{}; "
                    + "".join(f"$r['{rid}'] = Get-VenueLegHostLoad -ReceiptPath '{root / 'CARD-1' / 'leg-x' / 'bachelor' / (rid + '.json')}'"
                              f" -PlannedLegType '{'export' if kind == 'export' else 'playback'}'; " for rid, (kind, _b) in cases.items())
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
class LegHostLoadBindingTests(unittest.TestCase):
    """adversary-b0c05520.json A1 (the standing comes from the receipt's own smoke result; only a receipt that is an export skips it) and A2
    (duplicate members never pick a clean value)."""

    def read(self, cases: dict, planned: dict | None = None) -> dict:
        import hashlib
        planned = planned or {}
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "receipts"
            ev = Path(d) / "evidence"
            for rid, (receipt_body, layout, result_text) in cases.items():
                receipt = root / "CARD-1" / "leg-x" / "bachelor" / f"{rid}.json"
                receipt.parent.mkdir(parents=True, exist_ok=True)
                folder = ev / rid
                (folder / "export").mkdir(parents=True, exist_ok=True)
                summary = folder / ("export" if layout == "export" else "") / "summary.json"
                summary.write_text(json.dumps({"cpuQuiescence": {"timeMeanPercent": 5}}), encoding="utf-8")
                sha = None
                if result_text is not None:
                    (folder / "result.json").write_bytes(result_text.encode("utf-8"))
                    sha = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
                body = receipt_body(sha) if callable(receipt_body) else receipt_body
                receipt.write_text(body if isinstance(body, str) else json.dumps(body), encoding="utf-8")   # a str is the receipt's raw text
            # r6 (sol r5 B2): a case laid out as an export leg was PLANNED as one; every other case was planned as playback (planned= overrides)
            p = run_ps("$r = [ordered]@{}; "
                       + "".join(f"$r['{rid}'] = Get-VenueLegHostLoad -ReceiptPath '{root / 'CARD-1' / 'leg-x' / 'bachelor' / (rid + '.json')}'"
                                 f" -PlannedLegType '{planned.get(rid, 'export' if c[1] == 'export' else 'playback')}'; " for rid, c in cases.items())
                       + "$r | ConvertTo-Json -Compress -Depth 4")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return json.loads(p.stdout)

    def test_a1_a_result_json_that_no_longer_matches_the_receipt_reads_unknown(self) -> None:
        cim = json.dumps({"hostLoad": CIM_FAILED})
        quiet = json.dumps({"hostLoad": QUIET})
        import hashlib
        cim_sha = hashlib.sha256(cim.encode("utf-8")).hexdigest()
        got = self.read({
            "bound-1": (lambda sha: {"evidence": {"resultJsonSha256": sha}}, "play", quiet),
            "swapped-1": ({"evidence": {"resultJsonSha256": cim_sha}}, "play", quiet),   # the receipt bound the CIM result; the bytes now say quiet
            "unbound-1": ({"evidence": {}}, "play", quiet),
            "spoofexp-1": (lambda sha: {"evidence": {"resultJsonSha256": sha}}, "export", cim),   # root summary moved to export\: still a playback receipt
            "realexp-1": (REAL_EXPORT_RECEIPT, "export", None),
        })
        self.assertEqual((got["bound-1"]["state"], got["bound-1"]["provisional"]), ("quiet", False), got["bound-1"])
        for rid in ("swapped-1", "unbound-1", "spoofexp-1"):
            self.assertEqual((got[rid]["state"], got[rid]["provisional"], got[rid]["applies"]), ("unknown", True, True), (rid, got[rid]))
        self.assertEqual((got["realexp-1"]["state"], got["realexp-1"]["applies"]), ("not-applicable", False), got["realexp-1"])

    def test_a2_duplicate_members_read_unknown(self) -> None:
        base = '"before":{"collected":true},"after":{"collected":true}'
        texts = {
            "dupprov-1": '{"hostLoad":{"state":"quiet","provisional":true,"provisional":false,' + base + '}}',
            "duphl-1": '{"hostLoad":{"state":"unknown","provisional":true},"hostLoad":{"state":"quiet","provisional":false,' + base + '}}',
            "dupcoll-1": '{"hostLoad":{"state":"quiet","provisional":false,"before":{"collected":false,"collected":true},"after":{"collected":true}}}',
        }
        got = self.read({rid: (lambda sha: {"evidence": {"resultJsonSha256": sha}}, "play", t) for rid, t in texts.items()})
        for rid in texts:
            self.assertEqual((got[rid]["state"], got[rid]["provisional"]), ("unknown", True), (rid, got[rid]))

    def test_adversary3_a2_a_receipt_with_a_duplicated_member_reads_unknown(self) -> None:
        # adversary-90151822.json A2, its input: receipt {"export":null,"export":{},"evidence":{}} over a playback summary and a CIM-failed result.json
        # read not-applicable (the last export wins), so the leg was counted. The receipt is checked for duplicates like the result (A2 of pass 2).
        import hashlib
        cim = json.dumps({"hostLoad": CIM_FAILED})
        quiet = json.dumps({"hostLoad": QUIET})
        cim_sha = hashlib.sha256(cim.encode("utf-8")).hexdigest()
        got = self.read({
            "dupexp-1": ('{"export":null,"export":{},"evidence":{}}', "play", cim),
            "dupsha-1": (lambda sha: '{"evidence":{"resultJsonSha256":"' + cim_sha + '","resultJsonSha256":"' + sha + '"}}', "play", quiet),
            "bound-1": (lambda sha: {"evidence": {"resultJsonSha256": sha}}, "play", quiet),
        })
        for rid in ("dupexp-1", "dupsha-1"):
            self.assertEqual((got[rid]["state"], got[rid]["provisional"], got[rid]["applies"]), ("unknown", True, True), (rid, got[rid]))
            self.assertIn("repeats the member", got[rid]["reason"], (rid, got[rid]))
        self.assertEqual((got["bound-1"]["state"], got["bound-1"]["provisional"]), ("quiet", False), got["bound-1"])


    def test_r3_b2_an_export_shaped_object_never_exempts_a_playback_receipt(self) -> None:
        # sol r2 B2, its input: a playback receipt with export:{} (or the committed ProRes leg SPEC passed as the receipt) and a valid hash of a CIM-failed smoke
        # result read not-applicable / provisional false / applies false, so both drivers classed it QUIET_PRELOAD_OK and counted it. Only a receipt shaped as
        # the shipped writer writes an export leg (receipt schema, scale/display nulled, an export block naming its format with its spec binding or verdict) skips.
        cim = json.dumps({"hostLoad": CIM_FAILED})
        spec = json.loads((ROOT / "tools" / "profiling" / "dual-venue" / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8"))
        real = REAL_EXPORT_RECEIPT
        def bound(extra: dict):
            return lambda sha: {**extra, "evidence": {"resultJsonSha256": sha}}
        got = self.read({
            "emptyexp-1": (bound({"export": {}}), "play", cim),
            "specasrc-1": (lambda sha: {**spec, "evidence": {"resultJsonSha256": sha}}, "play", cim),
            "noschema-1": (bound({k: v for k, v in real.items() if k not in ("schema", "evidence")}), "play", cim),
            "withscale-1": (bound({**{k: v for k, v in real.items() if k != "evidence"}, "scale": {"requestedScale": 1}}), "play", cim),
            "withdisplay-1": (bound({**{k: v for k, v in real.items() if k != "evidence"}, "display": {"requestedMode": "fullscreen"}}), "play", cim),
            "noformat-1": (bound({**{k: v for k, v in real.items() if k != "evidence"}, "export": {**real["export"], "format": ""}}), "play", cim),
            "noevidence-1": (bound({**{k: v for k, v in real.items() if k != "evidence"}, "export": {**real["export"], "receiptSha256": "x", "verdict": None}}), "play", cim),
            "arrexp-1": (bound({**{k: v for k, v in real.items() if k != "evidence"}, "export": [real["export"]]}), "play", cim),
            # adversary-01a12498.json A1, its two inputs: scale/display [] with export {format prores, verdict x} over a bound playback result; and scale/display
            # omitted with format fake and 64 zeroes. Siblings: a real export block on a receipt that still binds its smoke result; an unbound verdict-only block.
            "a1arr-1": (bound({"schema": real["schema"], "scale": [], "display": [], "export": {"format": "prores", "verdict": "x"}}), "play", cim),
            "a1fake-1": (bound({"schema": real["schema"], "export": {"format": "fake", "receiptSha256": "0" * 64, "verdict": None}}), "play", cim),
            "a1bound-1": (bound({k: v for k, v in real.items() if k != "evidence"}), "play", cim),
            "a1verdictonly-1": ({**real, "export": {**real["export"], "receiptSha256": None, "verdict": "PASS"}}, "export", None),
            "realexp-1": (real, "export", None),
            "verdictexp-1": ({**real, "export": {**real["export"], "verdict": "PASS"}}, "export", None),
        })
        for rid in ("emptyexp-1", "specasrc-1", "noschema-1", "withscale-1", "withdisplay-1", "noformat-1", "noevidence-1", "arrexp-1",
                    "a1arr-1", "a1fake-1", "a1bound-1", "a1verdictonly-1"):
            with self.subTest(rid=rid):
                self.assertEqual((got[rid]["state"], got[rid]["provisional"], got[rid]["applies"]), ("unknown", True, True), (rid, got[rid]))
        for rid in ("realexp-1", "verdictexp-1"):
            with self.subTest(rid=rid):
                self.assertEqual((got[rid]["state"], got[rid]["provisional"], got[rid]["applies"]), ("not-applicable", False, False), (rid, got[rid]))

    def test_r3_a2_a_receipt_replaced_between_the_figure_read_and_the_host_load_read_reads_unknown(self) -> None:
        # adversary-01a12498.json A2, its input: receipt A (60 fps, CIM-failed) is read for its figures; then A's receipt and result.json bytes are replaced by
        # receipt B's (quiet). The host-load read, bound to A's evidence.resultJsonSha256 (-ExpectResultJsonSha256), must not return B's quiet standing.
        import hashlib
        cim = json.dumps({"hostLoad": CIM_FAILED}); quiet = json.dumps({"hostLoad": QUIET})
        a_sha = hashlib.sha256(cim.encode("utf-8")).hexdigest()
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "receipts"; ev = Path(d) / "evidence"
            rid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            receipt = root / "CARD-1" / "leg-x" / "bachelor" / f"{rid}.json"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            (ev / rid).mkdir(parents=True, exist_ok=True)
            (ev / rid / "result.json").write_text(quiet, encoding="utf-8")   # B's bytes now sit at A's path
            b_sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            cases = {"swapped": ({"receiptId": rid, "evidence": {"resultJsonSha256": b_sha}}, a_sha),
                     "same": ({"receiptId": rid, "evidence": {"resultJsonSha256": b_sha}}, b_sha),
                     "wrongid": ({"receiptId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "evidence": {"resultJsonSha256": b_sha}}, None),
                     "expectnone": ({"receiptId": rid, "evidence": {"resultJsonSha256": b_sha}}, "")}
            got = {}
            for name, (body, expect) in cases.items():
                receipt.write_text(json.dumps(body), encoding="utf-8")
                arg = "" if expect is None else f" -ExpectResultJsonSha256 '{expect}'"
                p = run_ps(f"Get-VenueLegHostLoad -ReceiptPath '{receipt}'{arg} | ConvertTo-Json -Compress")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                got[name] = json.loads(p.stdout)
        for name in ("swapped", "wrongid", "expectnone"):
            with self.subTest(case=name):
                self.assertEqual((got[name]["state"], got[name]["provisional"]), ("unknown", True), (name, got[name]))
        self.assertEqual((got["same"]["state"], got["same"]["provisional"]), ("quiet", False), "the unchanged receipt still reads its own standing")
        for script in ("Invoke-VenueLeg.ps1", "Invoke-VenueBaseline.ps1"):
            text = (ROOT / "tools" / "profiling" / "dual-venue" / script).read_text(encoding="utf-8")
            self.assertIn("Get-VenueLegHostLoad -ReceiptPath", text)
            self.assertIn("-ExpectResultJsonSha256", text, f"{script} binds the host-load read to the receipt its figures came from")


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
            cases = {"rootarr-1": [{"hostLoad": clean}], "hlarr-1": {"hostLoad": [clean]}, "provarr-1": {"hostLoad": {**clean, "provisional": [False]}},
                     "plain-1": {"hostLoad": clean}}   # the control: the same bound fixture, unwrapped, reads quiet (the read reached the parse)
            import hashlib
            for rid, doc in cases.items():
                receipt = root / "CARD-1" / "leg-x" / "bachelor" / f"{rid}.json"
                receipt.parent.mkdir(parents=True, exist_ok=True)
                (ev / rid).mkdir(parents=True, exist_ok=True)
                (ev / rid / "summary.json").write_text(json.dumps({"cpuQuiescence": {"timeMeanPercent": 5}}), encoding="utf-8")
                raw = json.dumps(doc).encode("utf-8")
                (ev / rid / "result.json").write_bytes(raw)
                # r3 (sol r2 / key3 H-ARRAY): the receipt binds these bytes, so the read reaches the -NoEnumerate parse (with receipt {} it stopped at the missing hash
                # and removing -NoEnumerate stayed green). Mutation-checked: without -NoEnumerate rootarr-1 reads quiet and this test fails.
                receipt.write_text(json.dumps({"evidence": {"resultJsonSha256": hashlib.sha256(raw).hexdigest()}}), encoding="utf-8")
            body = ("$r = [ordered]@{}; "
                    + "".join(f"$r['{rid}'] = Get-VenueLegHostLoad -ReceiptPath '{root / 'CARD-1' / 'leg-x' / 'bachelor' / (rid + '.json')}'; " for rid in cases)
                    + "$r | ConvertTo-Json -Compress -Depth 4")
            p = run_ps(body)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            got = json.loads(p.stdout)
        for rid in ("rootarr-1", "hlarr-1"):
            self.assertEqual((got[rid]["state"], got[rid]["provisional"]), ("unknown", True), (rid, got[rid]))
            self.assertNotIn("binds no evidence", got[rid]["reason"], "the fixture must reach the smoke parse, not stop at a missing hash")
        self.assertTrue(got["provarr-1"]["provisional"], got["provarr-1"])
        self.assertEqual((got["plain-1"]["state"], got["plain-1"]["provisional"]), ("quiet", False), got["plain-1"])

    def test_adversary2_a8_a_hashtable_leg_and_block_are_read(self) -> None:
        # adversary-b0c05520.json A8, its input: @{legId='q'; hostLoad=$quiet} read a blank id; an -AsHashtable block read unknown.
        q = json.dumps(QUIET).replace("'", "''")
        p = run_ps(f"$quiet = ConvertFrom-Json -InputObject '{q}'; $v = Get-VenueReferenceArmVerdict -Legs @(@{{ legId = 'q'; hostLoad = $quiet }}); "
                   f"$h = ConvertFrom-VenueSmokeHostLoad -HostLoad (ConvertFrom-Json -InputObject '{q}' -AsHashtable); "
                   "Write-Output ('V=' + $v.verdict + ' ID=' + $v.legs[0].legId + ' H=' + $h.state + '/' + $h.provisional)")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("V=COUNTED ID=q H=quiet/False", p.stdout)

    def test_no_control_leg_at_all_is_inconclusive(self) -> None:
        v, _ = self.verdict([])
        self.assertEqual(v["verdict"], "INCONCLUSIVE", v)


DV_RUNNER = ROOT / "tools" / "profiling" / "dual-venue" / "DualVenueRunner.psm1"
DV_DIR = ROOT / "tools" / "profiling" / "dual-venue"
# sol r5 B2, its input: a playback leg's receipt slot holding the production schema shaped as an export receipt (scale/display null, format prores,
# receiptSha256 64 zeroes, verdict null) with no receipt identity and no playback hashes.
EXPORT_SHAPED = {"schema": "mlv-app/dual-venue-receipt/v1", "scale": None, "display": None, "evidence": {},
                 "export": {"format": "prores", "receiptSha256": "0" * 64, "verdict": None}}


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class PlannedLegTypeExemptionTests(unittest.TestCase):
    """VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (sol r5 B2): the export exemption is granted only from the PLANNED leg type, never from the receipt's shape."""

    def write_leg(self, d: Path, rid: str, receipt: dict, result: dict | None) -> Path:
        root = d / "receipts"; ev = d / "evidence"
        path = root / "CARD-1" / "leg-x" / "bachelor" / f"{rid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        (ev / rid).mkdir(parents=True, exist_ok=True)
        (ev / rid / "summary.json").write_text(json.dumps({"cpuQuiescence": {"timeMeanPercent": 5}}), encoding="utf-8")   # pre-load 5
        if result is not None:
            (ev / rid / "result.json").write_text(json.dumps(result), encoding="utf-8")
        path.write_text(json.dumps(receipt), encoding="utf-8")
        return path

    def test_r6_b2_an_export_shaped_receipt_in_a_playback_leg_is_provisional_and_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            receipt = self.write_leg(Path(d), "shaped-1", EXPORT_SHAPED, {"hostLoad": CIM_FAILED})
            real = self.write_leg(Path(d), "realexp-1", REAL_EXPORT_RECEIPT, None)
            out = {}
            for name, path, planned in (("playback", receipt, "playback"), ("unreadable", receipt, "unknown"), ("blank", receipt, ""),
                                        ("export", real, "export"), ("realexp-as-playback", real, "playback")):
                p = run_ps(f"$s = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType '{planned}'; "
                           f"$l = Get-VenueReferenceHostLoadRefusal -LegId 'L' -Standing $s; "
                           "Write-Output ('J=' + ($s | ConvertTo-Json -Compress)); Write-Output ('LINE=' + $l)")
                self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                j = json.loads([l for l in p.stdout.splitlines() if l.startswith("J=")][-1][2:])
                line = [l for l in p.stdout.splitlines() if l.startswith("LINE=")][-1][5:]
                out[name] = (j, line)
        for name in ("playback", "unreadable", "blank", "realexp-as-playback"):
            with self.subTest(case=name):
                j, line = out[name]
                self.assertEqual((j["state"], j["provisional"], j["applies"]), ("unknown", True, True), (name, j))
                self.assertEqual(line, "REFERENCE_PROVISIONAL leg=L reason=hostload-unknown", (name, line))
        j, line = out["export"]
        self.assertEqual((j["state"], j["provisional"], j["applies"]), ("not-applicable", False, False), "a leg PLANNED as export keeps its exemption")
        self.assertEqual(line, "")

    def test_r6_b2_the_planned_type_comes_from_the_spec_and_only_an_exact_export_exempts(self) -> None:
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            specs = {"exp": '{"legType":"export","export":{"format":"prores"}}', "play": '{"legType":"playback"}', "none": '{"clipId":"M16-1243"}',
                     "case": '{"legType":"Export"}', "dup": '{"legType":"playback","legType":"export"}', "arr": '[{"legType":"export"}]',
                     "bad": '{"legType":', "arrval": '{"legType":["export"]}'}
            paths = {}
            for k, t in specs.items():
                paths[k] = Path(d) / f"{k}.json"; paths[k].write_text(t, encoding="utf-8")
            exp_sha = hashlib.sha256(specs["exp"].encode("utf-8")).hexdigest()
            calls = [(k, f"Get-VenuePlannedLegType -SpecPath '{p}'") for k, p in paths.items()]
            calls += [("missing", f"Get-VenuePlannedLegType -SpecPath '{Path(d) / 'absent.json'}'"), ("empty", "Get-VenuePlannedLegType -SpecPath ''"),
                      ("exp-bound", f"Get-VenuePlannedLegType -SpecPath '{paths['exp']}' -ExpectSpecSha256 '{exp_sha}'"),
                      ("exp-changed", f"Get-VenuePlannedLegType -SpecPath '{paths['exp']}' -ExpectSpecSha256 '{'0' * 64}'")]
            p = run_ps("".join(f"Write-Output ('{k}=' + ({c})); " for k, c in calls))
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got, {"exp": "export", "play": "playback", "none": "playback", "case": "playback", "dup": "unknown", "arr": "unknown",
                               "bad": "unknown", "arrval": "playback", "missing": "unknown", "empty": "unknown", "exp-bound": "export", "exp-changed": "unknown"})

    def test_r6_a1_the_smoke_result_text_reader_never_unrolls_or_takes_a_repeated_member(self) -> None:
        # adversary-de8e3799.json A1, its inputs: a singleton root array, a singleton hostLoad array, and state exceeded then quiet.
        clean = {"state": "quiet", "provisional": False, "before": {"collected": True}, "after": {"collected": True}}
        texts = {"rootarr": json.dumps([{"hostLoad": clean}]), "hlarr": json.dumps({"hostLoad": [clean]}),
                 "dupstate": '{"hostLoad":{"state":"exceeded","state":"quiet","provisional":false,"before":{"collected":true},"after":{"collected":true}}}',
                 "bom": "﻿" + json.dumps({"hostLoad": clean}), "plain": json.dumps({"hostLoad": clean})}
        body = "".join(f"$s = ConvertFrom-VenueSmokeResultText -Text '{t.replace(chr(39), chr(39) * 2)}'; Write-Output ('{k}=' + $s.state + '/' + $s.provisional); "
                       for k, t in texts.items())
        p = run_ps(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got, {"rootarr": "unknown/True", "hlarr": "unknown/True", "dupstate": "unknown/True", "bom": "quiet/False", "plain": "quiet/False"})

    def test_r6_a4_the_host_load_read_is_bound_to_the_receipt_bytes_the_figures_came_from(self) -> None:
        # adversary-de8e3799.json A4: figures read from receipt bytes X; the receipt is replaced (same result digest) before the host-load read.
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            first = '{"receiptId":"' + rid + '","outcome":"INVALID","outcome":"PASS","evidence":{"resultJsonSha256":"' + sha + '"}}'
            first_sha = hashlib.sha256(first.encode("utf-8")).hexdigest()
            now_sha = hashlib.sha256(path.read_bytes()).hexdigest()
            p = run_ps(f"$a = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ExpectReceiptSha256 '{first_sha}'; "
                       f"$b = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ExpectReceiptSha256 '{now_sha}'; "
                       "Write-Output ('A=' + $a.state + '/' + $a.provisional); Write-Output ('B=' + $b.state + '/' + $b.provisional)")
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("A=unknown/True", p.stdout, "a receipt that is not the bytes the figures came from is unknown")
        self.assertIn("B=quiet/False", p.stdout, "the same bytes still read their own standing")

    def test_r6_b2_every_driver_passes_the_planned_type_to_the_reader(self) -> None:
        # The class guard: every production call of Get-VenueLegHostLoad passes -PlannedLegType from the planned spec (Get-VenuePlannedLegType).
        # adversary-de8e3799.json A3: chain, baseline and matrix read the planned type AT PLANNING (before any leg runs); the batch binds its read to the
        # preflight-judged bytes. A4: matrix and baseline bind the host-load read to the receipt bytes the figures came from.
        # adversary-fa329743.json A3/A4: the chain's scalar branch and the baseline judge the text they read for the plan; the batch records the type
        # judged from the preflight's bytes and never reopens the spec after the leg.
        drivers = {"Invoke-VenueChain.ps1": "$plannedType = Get-VenuePlannedLegType -SpecText $specText",
                   "Invoke-VenueLeaseBatch.ps1": "$script:SpecPlannedLegType[$Path] = Get-VenuePlannedLegType -SpecText",
                   "Invoke-VenueBaseline.ps1": "plannedLegType = (Get-VenuePlannedLegType -SpecText $specText)",
                   "Invoke-VenueLeg.ps1": "$plannedTypes[[string]$pe.seq] = Get-VenuePlannedLegType -SpecPath ([string]$pe.specPath)"}
        chain = (DV_DIR / "Invoke-VenueChain.ps1").read_text(encoding="utf-8")
        self.assertIn("-PlannedLegType ([string]$leg.plannedLegType)", chain)
        self.assertNotIn("-PlannedLegType (Get-VenuePlannedLegType", chain, "never re-read at classification")
        batch = (DV_DIR / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Get-VenuePlannedLegType -SpecPath", batch, "the batch never reopens the spec (adversary-fa329743.json A3)")
        self.assertIn("-PlannedLegType $plannedType", batch)
        p = run_ps("Write-Output ('E=' + (Get-VenuePlannedLegType -SpecText '{\"legType\":\"export\"}')); Write-Output ('Z=' + (Get-VenuePlannedLegType -SpecText ''));"
                   " Write-Output ('P=' + (Get-VenuePlannedLegType -SpecText '{\"legType\":\"playback\"}'))")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("E=export", p.stdout); self.assertIn("Z=unknown", p.stdout); self.assertIn("P=playback", p.stdout)
        for script in ("Invoke-VenueLeg.ps1", "Invoke-VenueBaseline.ps1"):
            self.assertIn("-ExpectReceiptSha256", (DV_DIR / script).read_text(encoding="utf-8"), script)
        for script, plan_call in drivers.items():
            with self.subTest(script=script):
                text = (DV_DIR / script).read_text(encoding="utf-8")
                self.assertIn(plan_call, text)
                calls = [l for l in text.splitlines() if "Get-VenueLegHostLoad -ReceiptPath" in l and not l.lstrip().startswith("#")]
                self.assertTrue(calls, script)
                for line in calls:   # every call on the line carries its own -PlannedLegType
                    self.assertEqual(line.count("Get-VenueLegHostLoad -ReceiptPath"), line.count("-PlannedLegType"), (script, line))


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class MatrixRowStrictPredicateTests(unittest.TestCase):
    """VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1 r6 (sol r5 B3, opus r5 A4): a matrix row is measured only when hostLoadState is exactly 'quiet' AND hostLoadProvisional
    is a real $false; the count, the medians and the baseline count all use that one predicate."""

    def run_dv(self, body: str) -> str:
        script = f"$ErrorActionPreference = 'Stop'; Import-Module '{DV_RUNNER}' -Force -DisableNameChecking; " + body
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=180)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout

    def test_r6_b3_an_unknown_missing_or_exceeded_row_is_never_counted(self) -> None:
        rows = {"unknown": "hostLoadState = 'unknown'; hostLoadProvisional = $false", "missing": "hostLoadProvisional = $false",
                "exceeded": "hostLoadState = 'exceeded'; hostLoadProvisional = $false", "upper": "hostLoadState = 'QUIET'; hostLoadProvisional = $false",
                "na": "hostLoadState = 'not-applicable'; hostLoadProvisional = $false", "zero": "hostLoadState = 'quiet'; hostLoadProvisional = 0",
                "arr": "hostLoadState = 'quiet'; hostLoadProvisional = @($false)", "str": "hostLoadState = 'quiet'; hostLoadProvisional = 'false'",
                # residue r1 (sol r6 B3): collected is part of the one rule; only the collected quiet row is clean
                "uncollected": "hostLoadState = 'quiet'; hostLoadProvisional = $false; hostLoadCollected = $false",
                "nocollected": "hostLoadState = 'quiet'; hostLoadProvisional = $false",
                "collected1": "hostLoadState = 'quiet'; hostLoadProvisional = $false; hostLoadCollected = 1",
                "collectedarr": "hostLoadState = 'quiet'; hostLoadProvisional = $false; hostLoadCollected = @($true)",
                "quiet": "hostLoadState = 'quiet'; hostLoadProvisional = $false; hostLoadCollected = $true"}
        body = "".join(f"Write-Output ('{k}=' + (Get-DvMatrixMeasuredCount -Rows @([pscustomobject]@{{ outcome = 'PASS'; presentedFps = 59.56; {v} }})) + ',' + "
                       f"(Test-DvHostLoadRowClean -Row ([ordered]@{{ status = 'RAN'; fps = 42; {v} }}))); " for k, v in rows.items())
        got = dict(l.split("=", 1) for l in self.run_dv(body).splitlines() if "=" in l)
        for k in rows:
            with self.subTest(row=k):
                self.assertEqual(got[k], "1,True" if k == "quiet" else "0,False", (k, got[k]))

    def test_r6_a5_a_soft_hyphenated_quiet_row_is_not_clean(self) -> None:
        # adversary-de8e3799.json A5, its input: hostLoadState 'qui<U+00AD>et' (culture-aware -ceq says equal), provisional false -> counted 1.
        out = self.run_dv("Write-Output ('N=' + (Get-DvMatrixMeasuredCount -Rows @([pscustomobject]@{ outcome = 'PASS'; presentedFps = 59.56; "
                          "hostLoadState = ('qui' + [char]0x00AD + 'et'); hostLoadProvisional = $false })))")
        self.assertIn("N=0", out)

    def test_r6_b3_the_row_builder_and_the_medians_use_the_strict_rule(self) -> None:
        out = self.run_dv(
            "$e = [pscustomobject]@{ seq = 1; repeat = 1; cellId = 'c1'; backend = 'cuda'; displayMode = 'fullscreen'; scaleFactor = 1; legId = 'l' }; "
            "$r = Get-DvMatrixRow -PlanEntry $e -Receipt $null -HostLoad ([pscustomobject]@{ state = 'unknown'; provisional = $false }); "
            "Write-Output ('ROWPROV=' + $r.hostLoadProvisional); "
            "$full = [ordered]@{}; foreach ($p in $r.PSObject.Properties) { $full[$p.Name] = $p.Value }; "
            "$full.outcome = 'PASS'; $full.presentedFps = 59.56; $full.timelineFpsAfterFirstPresent = 59.0; $full.hostLoadState = 'unknown'; $full.hostLoadProvisional = $false; "
            "$rows = @([pscustomobject]$full); "
            "$t = ConvertTo-DvMatrixTable -Rows $rows -Cells @([pscustomobject]@{ cellId = 'c1'; backend = 'cuda'; displayMode = 'fullscreen'; scaleFactor = 1 }); "
            "Write-Output ('TABLE=' + (@(@($t) -join \"`n\" -split \"`r?`n\") | Where-Object { $_ -like '| c1 |*' }))")
        self.assertIn("ROWPROV=True", out, "a standing that says unknown beside provisional=false is a provisional row")
        table = [l for l in out.splitlines() if l.startswith("TABLE=")][-1]
        self.assertIn("| 0/1 |", table, table)
        self.assertNotIn("59.56", table, "an unknown row never reaches the median")
        baseline = (DV_DIR / "Invoke-VenueBaseline.ps1").read_text(encoding="utf-8")
        self.assertIn("$_.status -ceq 'RAN' -and $null -ne $_.fps -and (Test-DvHostLoadRowClean -Row $_)", baseline)

    def test_residue_b3_the_row_builder_keeps_collected_false_and_the_row_is_never_measured(self) -> None:
        # VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-RESIDUE-1 r1 (sol r6 B3, its catalogue addition): -HostLoad @{state='quiet'; provisional=$false; collected=$false;
        # applies=$true} gave a row with provisional false and a measured count of 1 (the builder dropped collected); Get-VenueReferenceHostLoadRefusal
        # refused the identical standing. The row now keeps hostLoadCollected=false, reads provisional, and is never counted or in a median.
        for name, standing in (("pscustomobject", "[pscustomobject]@{ state = 'quiet'; provisional = $false; collected = $false; applies = $true }"),
                               ("hashtable", "@{ state = 'quiet'; provisional = $false; collected = $false; applies = $true }"),
                               ("collected-array", "[pscustomobject]@{ state = 'quiet'; provisional = $false; collected = @($true); applies = $true }")):
            with self.subTest(standing=name):
                out = self.run_dv(
                    "$e = [pscustomobject]@{ seq = 1; repeat = 1; cellId = 'c1'; backend = 'cuda'; displayMode = 'fullscreen'; scaleFactor = 1; legId = 'l' }; "
                    f"$r = Get-DvMatrixRow -PlanEntry $e -Receipt $null -HostLoad ({standing}); "
                    "Write-Output ('ROWPROV=' + $r.hostLoadProvisional + ' ROWCOLL=' + $r.hostLoadCollected); "
                    "$full = [ordered]@{}; foreach ($p in $r.PSObject.Properties) { $full[$p.Name] = $p.Value }; "
                    "$full.outcome = 'PASS'; $full.presentedFps = 59.56; $full.timelineFpsAfterFirstPresent = 59.0; $full.hostLoadProvisional = $false; "
                    "$rows = @([pscustomobject]$full); "
                    "Write-Output ('N=' + (Get-DvMatrixMeasuredCount -Rows $rows)); "
                    "$t = ConvertTo-DvMatrixTable -Rows $rows -Cells @([pscustomobject]@{ cellId = 'c1'; backend = 'cuda'; displayMode = 'fullscreen'; scaleFactor = 1 }); "
                    "Write-Output ('TABLE=' + (@(@($t) -join \"`n\" -split \"`r?`n\") | Where-Object { $_ -like '| c1 |*' }))")
                self.assertIn("ROWPROV=True ROWCOLL=False", out, out)
                self.assertIn("N=0", out, "a row whose standing said collected=false is never measured, even when its provisional is later read false")
                table = [l for l in out.splitlines() if l.startswith("TABLE=")][-1]
                self.assertIn("| 0/1 |", table, table)
                self.assertNotIn("59.56", table, "an uncollected row never reaches the median")
        # the collected quiet standing is still a clean row (the regression guard)
        out = self.run_dv("$e = [pscustomobject]@{ seq = 1; repeat = 1; cellId = 'c1'; backend = 'cuda'; displayMode = 'fullscreen'; scaleFactor = 1; legId = 'l' }; "
                          "$r = Get-DvMatrixRow -PlanEntry $e -Receipt $null -HostLoad ([pscustomobject]@{ state = 'quiet'; provisional = $false; collected = $true; applies = $true }); "
                          "Write-Output ('ROWPROV=' + $r.hostLoadProvisional + ' ROWCOLL=' + $r.hostLoadCollected)")
        self.assertIn("ROWPROV=False ROWCOLL=True", out)
        baseline = (DV_DIR / "Invoke-VenueBaseline.ps1").read_text(encoding="utf-8")
        self.assertIn("hostLoadCollected = ($hl.collected -is [bool] -and $hl.collected)", baseline, "the baseline row keeps the standing's collected too")


if __name__ == "__main__":
    unittest.main()
