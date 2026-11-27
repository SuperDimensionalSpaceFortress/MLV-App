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
# residue r2 (adversary-07b6a932.json A4): the writer always stamps a typed outcome (Write-DvReceipt validates it against the enum) and sets export.verdict before it
# completes a PASS/FAIL, so the completed shipped receipt carries outcome PASS beside verdict PASS (a null verdict is the pre-export receipt's, never a PASS's).
REAL_EXPORT_RECEIPT = {"schema": "mlv-app/dual-venue-receipt/v1", "scale": None, "display": None, "evidence": {}, "outcome": "PASS",
                       "export": {"format": "prores", "maxFrames": 480, "dualIsoExpected": True, "receiptSha256": "2f57f11f95fa9a0eac428f256ad596b1706fe9c9a2f9dfef8411815f970bab6a",
                                  "files": None, "stages": None, "problems": [], "containmentAdvisory": [], "verdict": "PASS"}}


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

    def test_r2n1_a6_the_duplicate_scan_reads_every_depth_the_json_readers_accept(self) -> None:
        # adversary-7c5bfe86.json A6 (R2-NARROW-1 r2), its input: an otherwise passing smoke result with a root member `extra` holding 64 nested arrays (depth 65). RED on 7c5bfe86: JsonDocument.Parse
        # was called with its default depth limit of 64, so the scan threw and a valid, duplicate-free quiet result read unknown. ConvertFrom-Json -Depth 100 (the readers' own parser) accepts it, so the
        # scan must too; a real duplicate under that depth is still found. (The nesting is built in the pwsh body, not here.)
        deep = "$deep = ('[' * 64) + '0' + (']' * 64); "
        quiet = json.dumps({"hostLoad": QUIET}).replace("'", "''")
        p = run_ps(
            deep + f"$t = '{quiet[:-1]}' + ',\"extra\":' + $deep + '}}'; "
            "$s = ConvertFrom-VenueSmokeResultText -Text $t; Write-Output ('S=' + $s.state + ',' + $s.provisional); "
            "$d = Find-VenueJsonDuplicateMember -Text $t -All; Write-Output ('D=[' + $d + ']'); "
            "$t2 = $t.Substring(0, $t.Length - 1) + ',\"EXTRA\":1}'; $d2 = Find-VenueJsonDuplicateMember -Text $t2; Write-Output ('D2=' + $d2)")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["S"], "quiet,False", p.stdout)
        self.assertEqual(got["D"], "[]", p.stdout)
        self.assertEqual(got["D2"], "$.EXTRA", p.stdout)

    def test_r2n1_pass1_a4_the_duplicate_scan_reads_every_depth_the_default_readers_accept(self) -> None:
        # adversary-baf2e69a.json A4, its input: Q plus an extra root member of 128 nested arrays (depth 129, no duplicate). RED on baf2e69a: the scan capped total depth at 128 while the readers'
        # default ConvertFrom-Json accepts 1023 arrays, so the bound leg reader read unknown for a valid document. The scan's cap is now the default reader's own (1024); a document deeper than
        # either parser accepts is refused by the parsers. (The nesting is built in the pwsh body.)
        quiet = json.dumps({"hostLoad": QUIET}).replace("'", "''")
        p = run_ps(
            f"$head = '{quiet[:-1]}'; "
            "foreach ($n in 128, 1000) { $t = $head + ',\"extra\":' + (('[' * $n) + '0' + (']' * $n)) + '}'; $s = ConvertFrom-VenueSmokeResultText -Text $t; Write-Output ('N' + $n + '=' + $s.state + ',' + $s.provisional) }")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual((got["N128"], got["N1000"]), ("quiet,False", "quiet,False"), p.stdout)

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
            # residue r1 (adversary-ac6205bd.json A6): a non-null verdict is the receipt's own outcome, as Invoke-VenueLeg.ps1 writes both
            "verdictexp-1": ({**real, "outcome": "PASS", "export": {**real["export"], "verdict": "PASS"}}, "export", None),
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

    def test_adversary_a6_a_planned_export_with_playback_evidence_or_an_invented_verdict_is_not_exempt(self) -> None:
        # adversary-ac6205bd.json A6, its input: a leg planned as export whose receipt carries outcome PASS, playback {presentedFps 59}, a contact-frames
        # binding and export.verdict 'NONEXISTENT' (digest zeros) was exempt (not-applicable, applies false). Each of those marks a non-export receipt now.
        zeros, ones = "0" * 64, "1" * 64
        base = {**json.loads(json.dumps(REAL_EXPORT_RECEIPT)), "outcome": "PASS"}
        base["export"]["receiptSha256"] = zeros
        cases = {
            "adversary": {**base, "playback": {"presentedFps": 59}, "evidence": {"contactFramesJsonSha256": ones}, "export": {**base["export"], "verdict": "NONEXISTENT"}},
            "playback-block": {**base, "playback": {"presentedFps": 59}},
            "contact-frames": {**base, "evidence": {"contactFramesJsonSha256": ones}},
            "invented-verdict": {**base, "export": {**base["export"], "verdict": "NONEXISTENT"}},
        }
        accepted = {"verdict-is-outcome": {**base, "export": {**base["export"], "verdict": "PASS"}},
                    "downgraded": {**base, "outcome": "INVALID", "export": {**base["export"], "verdict": "PASS"}},
                    "real": REAL_EXPORT_RECEIPT}
        with tempfile.TemporaryDirectory() as d:
            for name, rc in {**cases, **accepted}.items():
                with self.subTest(receipt=name):
                    path = self.write_leg(Path(d), f"{name}-1", rc, None)
                    p = run_ps(f"$s = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'export'; Write-Output ('J=' + ($s | ConvertTo-Json -Compress))")
                    self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
                    j = json.loads([l for l in p.stdout.splitlines() if l.startswith("J=")][-1][2:])
                    if name in cases:
                        self.assertEqual((j["state"], j["applies"]), ("unknown", True), (name, j))
                    else:
                        self.assertEqual((j["state"], j["applies"]), ("not-applicable", False), (name, j))

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

    def test_r2_a4_a_host_load_read_that_does_not_answer_is_given_up_on_at_its_bound(self) -> None:
        # adversary-7efea617.json A4, its input: a final leg whose receipt/result.json read returns its bytes only after a long stall. RED on 7c79d20b: the readers were
        # synchronous ([IO.File]::ReadAllBytes, no bound and no -ReadTimeoutSec at all), so a deadline stop waited out the stall. The two reads now run on
        # Invoke-VenueBoundedRead's worker: a read not answered inside -ReadTimeoutSec is 'unknown' (uncounted) with that reason, one given up on is counted for
        # Complete-VenueProcess, a bound under 1 s starts no read, and an unstalled read still reads its own standing.
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            p = run_ps(
                "& (Get-Module VenueChain) { $script:VenueTestFileReadDelayMs = 6000 }; "
                f"$t = [Diagnostics.Stopwatch]::StartNew(); $a = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 1; $ms = $t.ElapsedMilliseconds; "
                "Write-Output ('A=' + $a.state + '/' + $a.provisional + '/' + $a.collected); Write-Output ('AMS=' + $ms); Write-Output ('AWHY=' + $a.reason); "
                "Write-Output ('ABANDONED=' + (& (Get-Module VenueChain) { $script:VenueAbandonedReads })); "
                f"$t = [Diagnostics.Stopwatch]::StartNew(); $b = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 0.25; $bms = $t.ElapsedMilliseconds; "
                "Write-Output ('B=' + $b.state + '/' + $b.provisional); Write-Output ('BMS=' + $bms); Write-Output ('BWHY=' + $b.reason); "
                "& (Get-Module VenueChain) { $script:VenueTestFileReadDelayMs = 0 }; "
                f"$c = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20; Write-Output ('C=' + $c.state + '/' + $c.provisional); "
                # adversary-b3d61554.json A1: ONE budget across the two reads. Each read alone answers in 0.6 s (inside a 1 s bound), together they do not: the second is cut to what is
                # left, so the standing is unknown; the same two reads under a 20 s budget are quiet. RED on b3d61554: each read got a fresh bound and the pair read quiet.
                "& (Get-Module VenueChain) { $script:VenueTestFileReadDelayMs = 600 }; "
                f"$d = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 1.0; Write-Output ('D=' + $d.state + '/' + $d.provisional); "
                f"$e = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20; Write-Output ('E=' + $e.state + '/' + $e.provisional); "
                # A11: a receipt that is not there names its leaf and the cause, never a null read
                "& (Get-Module VenueChain) { $script:VenueTestFileReadDelayMs = 0 }; "
                f"$f = Get-VenueLegHostLoad -ReceiptPath '{Path(d) / 'receipts' / 'CARD-1' / 'leg-x' / 'bachelor' / 'missing.json'}' -PlannedLegType 'playback' -ReadTimeoutSec 5; Write-Output ('FWHY=' + $f.reason); "
                "Complete-VenueProcess -ExitCode 0; Write-Output 'NOT-REACHED-WHEN-A-READ-WAS-ABANDONED'")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "unknown/True/False", p.stdout)
        self.assertIn("did not finish", got["AWHY"], p.stdout)
        self.assertLess(int(got["AMS"]), 5500, "the read was waited out past its 1 s bound (the stall is 6 s): " + p.stdout)
        self.assertGreaterEqual(int(got["ABANDONED"]), 1, "a read given up on is counted so Complete-VenueProcess ends the process: " + p.stdout)
        self.assertEqual(got["B"], "unknown/True", p.stdout)
        self.assertIn("no time was left", got["BWHY"], p.stdout)
        self.assertLess(int(got["BMS"]), 1500, "a bound under 1 s must not start a read: " + p.stdout)
        self.assertEqual(got["C"], "quiet/False", "an unstalled read still reads its own standing: " + p.stdout)
        self.assertEqual(got["D"], "unknown/True", "two reads that each fit the bound but not together must not both be granted a fresh one: " + p.stdout)
        self.assertEqual(got["E"], "quiet/False", p.stdout)
        self.assertIn("missing.json", got["FWHY"], p.stdout)
        self.assertNotIn("HashData", got["FWHY"], "a failed receipt read names its leaf, not a null-argument error: " + p.stdout)
        self.assertNotIn("NOT-REACHED", p.stdout, "Complete-VenueProcess must end the process at once when a read was abandoned")

    def test_r2_a4_every_caller_bounds_the_read_and_the_batch_by_its_deadline(self) -> None:
        # The class guard for A4: the batch hands over what is left before -DeadlineUtc (capped at 20 s); the reader's two file reads are bounded reads, never a raw
        # ReadAllBytes; the matrix runner (which calls the reader) ends through Complete-VenueProcess so an abandoned read cannot hold its exit.
        dv = ROOT / "tools" / "profiling" / "dual-venue"
        batch = (dv / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        # R4-NARROW-1 r2 (adversary-6596e055.json A4): the remainder is no longer sampled as a number; the reader is handed the absolute UTC end (-CallDeadlineUtc) and judges it itself
        self.assertIn("-ExpectReceiptSha256 $receiptSha -ReadTimeoutSec 20.0 -CallDeadlineUtc $deadline", batch)
        module = MODULE.read_text(encoding="utf-8")
        reader = module[module.index("function Get-VenueLegHostLoad {"):module.index("function Get-VenueReferenceHostLoadRefusal {")]
        self.assertNotIn("ReadAllBytes", reader)
        self.assertNotIn("Test-Path", reader.split("$root = Split-Path")[1].split("$bytes = ")[0], "the result.json existence probe runs on the bounded worker too")
        self.assertEqual(reader.count("= Read-VenueFileBytesBounded -Path "), 2)
        leg = (dv / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("Get-VenuePlannedLegType, Complete-VenueProcess -DisableNameChecking -Force", leg)
        self.assertIn("Complete-VenueProcess -ExitCode 0   # a host-load read given up on", leg)

    def test_mr1_a3_a_read_whose_budget_is_already_spent_starts_no_worker(self) -> None:
        # adversary-55dcf49e.json A3, its input: the caller is suspended after its first read and resumes past the shared budget's end before the second. RED on 55dcf49e: the worker was
        # launched BEFORE the clock was looked at, so the file probe and ReadAllBytes still ran after the end (the late-answer check only discarded the answer). Here the seam
        # VenueTestPreWaitDelayMs makes the old order observable: the worker (writing a marker file) had 1.5 s to run before the caller looked at the clock. Now nothing starts.
        with tempfile.TemporaryDirectory() as d:
            gone, live, blob = Path(d) / "marker-expired.txt", Path(d) / "marker-live.txt", Path(d) / "f.bin"
            blob.write_bytes(b"abc")
            body = (
                "$vc = Get-Module VenueChain; $script = 'param($p) [IO.File]::WriteAllText($p, ''x'')'; "
                "& $vc { $script:VenueTestPreWaitDelayMs = 1500 }; "
                "$sw = [Diagnostics.Stopwatch]::StartNew(); Start-Sleep -Milliseconds 700; "
                f"$a = & $vc {{ param($sw, $m, $s) Invoke-VenueBoundedRead -ScriptText $s -Argument $m -TimeoutSec 5 -Clock $sw -EndSec 0.3 }} $sw '{gone}' $script; "
                "Write-Output ('A=' + $a.answered); Write-Output ('AERR=' + $a.error); "
                f"Write-Output ('M1=' + (Test-Path -LiteralPath '{gone}')); "
                "$c = try { & $vc { param($sw, $f) $r = Read-VenueFileBytesBounded -Path $f -TimeoutSec 5 -Clock $sw -EndSec 0.3; 'READ' } $sw "
                f"'{blob}' }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('C=' + $c); "
                "& $vc { $script:VenueTestPreWaitDelayMs = 0 }; "
                "$sw2 = [Diagnostics.Stopwatch]::StartNew(); "
                f"$b = & $vc {{ param($sw, $m, $s) Invoke-VenueBoundedRead -ScriptText $s -Argument $m -TimeoutSec 5 -Clock $sw -EndSec 30 }} $sw2 '{live}' $script; "
                "Write-Output ('B=' + $b.answered); "
                f"Write-Output ('M2=' + (Test-Path -LiteralPath '{live}'))")
            p = run_ps(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "False", p.stdout)
        self.assertIn("no time is left", got["AERR"], p.stdout)
        self.assertEqual(got["M1"], "False", "the worker was started after the caller's end: " + p.stdout)
        self.assertTrue(got["C"].startswith("THROWN") and "did not finish" in got["C"], p.stdout)
        self.assertEqual((got["B"], got["M2"]), ("True", "True"), "a read inside its budget still runs: " + p.stdout)

    def test_r4n2_a3_a_budget_spent_during_worker_setup_starts_no_read(self) -> None:
        # adversary-6596e055.json A3, its input: the caller is descheduled after the budget check and the worker is created / given its script and argument, and resumes past the shared
        # end before BeginInvoke. RED on 077be843: the budget was checked once, BEFORE Create/AddScript/AddArgument, so the worker still started and ran its I/O (marker written).
        # The seam VenueTestPreBeginDelayMs is that pause (after AddArgument, before BeginInvoke). Now the budget is looked at again right before BeginInvoke: nothing starts.
        with tempfile.TemporaryDirectory() as d:
            gone, live = Path(d) / "marker-expired.txt", Path(d) / "marker-live.txt"
            body = (
                "$vc = Get-Module VenueChain; $script = 'param($p) [IO.File]::WriteAllText($p, ''x'')'; "
                "& $vc { $script:VenueTestPreBeginDelayMs = 700 }; "
                "$sw = [Diagnostics.Stopwatch]::StartNew(); "
                f"$a = & $vc {{ param($sw, $m, $s) Invoke-VenueBoundedRead -ScriptText $s -Argument $m -TimeoutSec 5 -Clock $sw -EndSec 0.3 }} $sw '{gone}' $script; "
                "Start-Sleep -Milliseconds 1500; "
                "Write-Output ('A=' + $a.answered); Write-Output ('AERR=' + $a.error); "
                f"Write-Output ('M1=' + (Test-Path -LiteralPath '{gone}')); "
                "& $vc { $script:VenueTestPreBeginDelayMs = 0 }; "
                "$sw2 = [Diagnostics.Stopwatch]::StartNew(); "
                f"$b = & $vc {{ param($sw, $m, $s) Invoke-VenueBoundedRead -ScriptText $s -Argument $m -TimeoutSec 5 -Clock $sw -EndSec 30 }} $sw2 '{live}' $script; "
                "Write-Output ('B=' + $b.answered); "
                f"Write-Output ('M2=' + (Test-Path -LiteralPath '{live}'))")
            p = run_ps(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "False", p.stdout)
        self.assertIn("no time is left", got["AERR"], p.stdout)
        self.assertEqual(got["M1"], "False", "the worker was started after the caller's end: " + p.stdout)
        self.assertEqual((got["B"], got["M2"]), ("True", "True"), "a read inside its budget still runs: " + p.stdout)

    def test_r4n2_a3_the_file_read_worker_is_handed_the_call_end_and_reads_nothing_past_it(self) -> None:
        # adversary-6596e055.json A3 (the worker half): the worker (Test-Path then ReadAllBytes) received neither a clock nor an end, so a read that started in time still ran its I/O past
        # the end. Now it is handed the absolute UTC end and checks it before the existence probe and again before ReadAllBytes. The worker text is run here as the bounded read runs it.
        with tempfile.TemporaryDirectory() as d:
            blob = Path(d) / "f.bin"
            blob.write_bytes(b"abc")
            body = (
                "$vc = Get-Module VenueChain; $w = & $vc { $script:VenueFileReadWorkerText }; $sb = [scriptblock]::Create([string]$w); "
                "$past = [DateTime]::UtcNow.AddSeconds(-1).Ticks; $future = [DateTime]::UtcNow.AddSeconds(60).Ticks; "
                f"$r1 = try {{ $x = & $sb @{{ path = '{blob}'; missingOk = $false; delayMs = 0; endUtcTicks = $past }}; 'READ' + @($x).Count }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('PAST=' + $r1); "
                f"$r2 = try {{ $x = & $sb @{{ path = '{blob}'; missingOk = $true; delayMs = 0; endUtcTicks = $past }}; 'READ' + @($x).Count }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('PASTMISSINGOK=' + $r2); "
                f"$r3 = try {{ $x = & $sb @{{ path = '{blob}'; missingOk = $false; delayMs = 700; endUtcTicks = [DateTime]::UtcNow.AddMilliseconds(300).Ticks }}; 'READ' + @($x).Count }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('EXPIRESDURINGDELAY=' + $r3); "
                f"$r4 = try {{ $x = & $sb @{{ path = '{blob}'; missingOk = $false; delayMs = 0; endUtcTicks = $future }}; 'READ' + @($x).Count }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('FUTURE=' + $r4)")
            p = run_ps(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        for key in ("PAST", "PASTMISSINGOK", "EXPIRESDURINGDELAY"):
            self.assertTrue(got[key].startswith("THROWN") and "call end" in got[key], (key, p.stdout))
        self.assertEqual(got["FUTURE"], "READ3", "a read inside its end still reads its three bytes: " + p.stdout)
        module = MODULE.read_text(encoding="utf-8")
        self.assertEqual(module.count("endUtcTicks = "), 1, "the one bounded file read hands its worker the end")

    def test_r2n1_a4_the_file_read_worker_also_ends_by_the_callers_monotonic_clock_when_the_wall_clock_was_stepped_back(self) -> None:
        # adversary-7c5bfe86.json A4 (R2-NARROW-1 r2), its input: the worker was handed only a projected UTC tick end. A wall clock stepped back after the caller computed it leaves that end far in the
        # future, so both worker checks saw time left and the file was read after the shared monotonic end. RED on 7c5bfe86: the worker read its three bytes (READ3) with the stopwatch past its end.
        # Now the worker is also handed the caller's stopwatch and its end (clock, clockEndSec) and refuses to touch the file when EITHER end has passed.
        with tempfile.TemporaryDirectory() as d:
            blob = Path(d) / "f.bin"
            blob.write_bytes(b"abc")
            body = (
                "$vc = Get-Module VenueChain; $w = & $vc { $script:VenueFileReadWorkerText }; $sb = [scriptblock]::Create([string]$w); "
                "$future = [DateTime]::UtcNow.AddSeconds(600).Ticks; $sw = [Diagnostics.Stopwatch]::StartNew(); [Threading.Thread]::Sleep(400); "
                f"$r1 = try {{ $x = & $sb @{{ path = '{blob}'; missingOk = $false; delayMs = 0; endUtcTicks = $future; clock = $sw; clockEndSec = 0.1 }}; 'READ' + @($x).Count }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('STEPPED=' + $r1); "
                f"$r2 = try {{ $x = & $sb @{{ path = '{blob}'; missingOk = $true; delayMs = 0; endUtcTicks = $future; clock = $sw; clockEndSec = 0.1 }}; 'READ' + @($x).Count }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('STEPPEDMISSINGOK=' + $r2); "
                f"$r3 = try {{ $x = & $sb @{{ path = '{blob}'; missingOk = $false; delayMs = 0; endUtcTicks = $future; clock = $sw; clockEndSec = 60 }}; 'READ' + @($x).Count }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('INSIDE=' + $r3)")
            p = run_ps(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        for key in ("STEPPED", "STEPPEDMISSINGOK"):
            self.assertTrue(got[key].startswith("THROWN") and "call end" in got[key], (key, p.stdout))
        self.assertEqual(got["INSIDE"], "READ3", "a read inside both ends still reads its three bytes: " + p.stdout)
        module = MODULE.read_text(encoding="utf-8")
        self.assertIn("clock = $workClock; clockEndSec = $workEndSec", module, "the one bounded file read hands its worker the caller's stopwatch and its end")

    def test_r2n1_pass1_a1_the_workers_monotonic_end_comes_from_one_clock_sample_not_a_stale_remainder_plus_a_fresh_read(self) -> None:
        # adversary-baf2e69a.json A1, its input: the caller samples "0.95 s left" of its shared stopwatch, is descheduled 0.75 s before the worker's end is computed, and the end was then taken as the FRESH clock
        # read plus the STALE remainder (~1.75 s instead of 1.0 s), so a worker started in the gap still read after the real end. RED on baf2e69a: clockEndSec read ~1.8 here. The seam
        # VenueTestReadSampleDelayMs stalls the caller right after its one clock sample (800 ms); the shared end (-EndSec 1.0) must be what the worker is handed, whatever the stall.
        with tempfile.TemporaryDirectory() as d:
            blob = Path(d) / "f.bin"
            blob.write_bytes(b"abc")
            body = (
                "$vc = Get-Module VenueChain; & $vc { $script:VenueTestCaptureWorkerArg = $true; $script:VenueTestReadSampleDelayMs = 800 }; "
                "$sw = [Diagnostics.Stopwatch]::StartNew(); "
                f"$r = try {{ & $vc {{ param($p, $c) $null = Read-VenueFileBytesBounded -Path $p -TimeoutSec 5 -Clock $c -EndSec 1.0; 'OK' }} '{blob}' $sw }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('R=' + $r); "
                "$a = & $vc { $script:VenueTestLastFileWorkerArg }; Write-Output ('END=' + [Math]::Round([double]$a.clockEndSec, 2)); "
                "& $vc { $script:VenueTestCaptureWorkerArg = $false; $script:VenueTestReadSampleDelayMs = 0 }")
            p = run_ps(body)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertLessEqual(float(got["END"]), 1.0, "the worker's monotonic end is the caller's shared end (1.0 s), not a stale remainder added to a later read: " + p.stdout)
        self.assertGreater(float(got["END"]), 0.0, p.stdout)

    def test_r4n2_a4_the_reader_ends_by_the_absolute_utc_deadline_the_caller_hands_it(self) -> None:
        # adversary-6596e055.json A4, its input: the caller sampled "remaining = 1.25 s" as a number, was paused past the UTC deadline, and the reader started its own stopwatch then, so both reads
        # ran after the deadline. RED on 077be843: there was no way to hand the reader the end (no -CallDeadlineUtc), only a relative number. The deadline passed here is already behind us while
        # -ReadTimeoutSec still says 20: the reader judges the ABSOLUTE end at its own start, so no read starts and the standing is unknown (uncounted). A deadline far off reads quiet; one
        # under 1 s away is not started either; the reader's -ReadTimeoutSec still caps a far deadline.
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            base = f"Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20"
            p = run_ps(
                "& (Get-Module VenueChain) { $script:VenueTestFileReadDelayMs = 0 }; "
                f"$a = {base} -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(-1)); Write-Output ('A=' + $a.state + '/' + $a.provisional); Write-Output ('AWHY=' + $a.reason); "
                f"$b = {base} -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(0.5)); Write-Output ('B=' + $b.state + '/' + $b.provisional); "
                f"$c = {base} -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(60)); Write-Output ('C=' + $c.state + '/' + $c.provisional); "
                # the absolute end also cuts a SECOND read that no longer fits: each read alone answers in 0.9 s, the deadline is 2.0 s out and the budget under it is shared
                "& (Get-Module VenueChain) { $script:VenueTestFileReadDelayMs = 900 }; "
                f"$d = {base} -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(1.5)); Write-Output ('D=' + $d.state + '/' + $d.provisional); "
                "& (Get-Module VenueChain) { $script:VenueTestFileReadDelayMs = 0 }; "
                "Complete-VenueProcess -ExitCode 0; Write-Output 'NOT-REACHED-WHEN-A-READ-WAS-ABANDONED'")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "unknown/True", "a deadline already behind us starts no read: " + p.stdout)
        self.assertIn("no time was left", got["AWHY"], p.stdout)
        self.assertEqual(got["B"], "unknown/True", "under 1 s to the deadline starts no read: " + p.stdout)
        self.assertEqual(got["C"], "quiet/False", "a far deadline still reads its own standing: " + p.stdout)
        self.assertEqual(got["D"], "unknown/True", "two reads that do not both fit before the deadline: the second is cut: " + p.stdout)
        chain = (ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueChain.ps1").read_text(encoding="utf-8")
        batch = (ROOT / "tools" / "profiling" / "dual-venue" / "Invoke-VenueLeaseBatch.ps1").read_text(encoding="utf-8")
        for label, text in (("chain", chain), ("batch", batch)):
            self.assertIn("-ExpectReceiptSha256 $receiptSha -ReadTimeoutSec 20.0 -CallDeadlineUtc $deadline", text, label)
            self.assertNotIn("[Math]::Min(20.0, ($deadline - [DateTime]::UtcNow).TotalSeconds)", text, f"{label}: the remainder is no longer sampled as a number")

    def test_r4n2_p2_a4_the_absolute_deadline_reaches_the_file_read_even_when_the_wall_clock_is_stepped_after_the_sample(self) -> None:
        # adversary-ac722a96.json A4, its input: Get-VenueLegHostLoad -CallDeadlineUtc <now+2 s> with a 20 s -ReadTimeoutSec; the wall clock steps forward (a VM resume, an NTP step) after
        # the reader sampled its 2 s budget. RED on ac722a96: the read got only the relative budget (the worker's end was recomputed from the stepped clock), so the read ran after the deadline.
        # Now the read is handed the absolute end itself: with the wall clock stepped 10 s (the test seam VenueTestUtcStepSec) no read starts and the standing is unknown. Read-VenueFileBytesBounded
        # -EndUtc is the same rule on its own: an end already passed, or passed on the stepped clock, throws before a worker is made; an end far off still reads.
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "abababab-abab-4bab-8bab-abababababab"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            blob = Path(d) / "f.bin"
            blob.write_bytes(b"abc")
            p = run_ps(
                "$vc = Get-Module VenueChain; "
                f"$a = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(5)); Write-Output ('A=' + $a.state + '/' + $a.provisional); "
                "& $vc { $script:VenueTestUtcStepSec = 10 }; "
                f"$b = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(5)); Write-Output ('B=' + $b.state + '/' + $b.provisional); "
                f"$c = try {{ & $vc {{ param($f) $sw = [Diagnostics.Stopwatch]::StartNew(); Read-VenueFileBytesBounded -Path $f -TimeoutSec 5 -Clock $sw -EndSec 5 -EndUtc ([DateTime]::UtcNow.AddSeconds(5)) }} '{blob}'; 'READ' }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('C=' + $c); "
                "& $vc { $script:VenueTestUtcStepSec = 0 }; "
                f"$e = try {{ & $vc {{ param($f) $sw = [Diagnostics.Stopwatch]::StartNew(); Read-VenueFileBytesBounded -Path $f -TimeoutSec 5 -Clock $sw -EndSec 5 -EndUtc ([DateTime]::UtcNow.AddSeconds(-1)) }} '{blob}'; 'READ' }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('E=' + $e); "
                f"$f2 = try {{ & $vc {{ param($f) $sw = [Diagnostics.Stopwatch]::StartNew(); Read-VenueFileBytesBounded -Path $f -TimeoutSec 5 -Clock $sw -EndSec 5 -EndUtc ([DateTime]::UtcNow.AddSeconds(60)) }} '{blob}'; 'READ' }} catch {{ 'THROWN ' + $_.Exception.Message }}; Write-Output ('F=' + $f2)")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "quiet/False", "no step: the read inside the deadline reads: " + p.stdout)
        self.assertEqual(got["B"], "unknown/True", "the clock stepped past the absolute deadline: nothing is read: " + p.stdout)
        self.assertTrue(got["C"].startswith("THROWN") and "absolute end" in got["C"], p.stdout)
        self.assertTrue(got["E"].startswith("THROWN") and "absolute end" in got["E"], p.stdout)
        self.assertTrue(got["F"].endswith("READ"), p.stdout)

    def test_r4n2_p3_a2_an_answer_seen_after_the_absolute_deadline_is_never_acted_on(self) -> None:
        # adversary-d10f5241.json A2, its input: Get-VenueLegHostLoad -CallDeadlineUtc <now+2 s>; the receipt read finishes; the caller is suspended (or the wall clock steps ten seconds) INSIDE the
        # final result.json read, which then returns its bytes while the stopwatch still shows under a second. RED on d10f5241: the late-answer check read the stopwatch alone, the bytes were
        # accepted and the leg read quiet, collected, non-provisional after its UTC deadline. The seam VenueTestPostReadDelayMs is that suspension (after the read answered, before the answer is
        # judged): the standing is now unknown. Without the suspension the same call still reads quiet.
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            p = run_ps(
                "$vc = Get-Module VenueChain; "
                f"$a = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(2.5)); Write-Output ('A=' + $a.state + '/' + $a.provisional); "
                "& $vc { $script:VenueTestPostReadDelayMs = 3000 }; "
                f"$b = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(2.5)); Write-Output ('B=' + $b.state + '/' + $b.provisional); Write-Output ('BWHY=' + $b.reason); "
                "& $vc { $script:VenueTestPostReadDelayMs = 0 }")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "quiet/False", p.stdout)
        self.assertEqual(got["B"], "unknown/True", "an answer seen after the absolute deadline is not acted on: " + p.stdout)
        self.assertIn("absolute end", got["BWHY"], p.stdout)

    def test_r2res_a2_bytes_that_reach_classification_after_the_absolute_end_are_never_quiet_or_exempt(self) -> None:
        # adversary-3be19c39.json A2, its input: a valid PASS receipt and matching quiet result.json, -CallDeadlineUtc 2.5 s away, and the caller suspended 3 s AFTER the read's own end check and
        # BEFORE the bytes are classified (the seams VenueTestPostBytesDelayMs / VenueTestPostReceiptBytesDelayMs). RED on 3be19c39: Get-VenueLegHostLoad returned quiet / not-applicable after the end,
        # so the caller could count the leg. The absolute end is checked again before a quiet or export-exempt answer is returned: unknown and provisional, never counted.
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "efefefef-efef-4fef-8fef-efefefefefef"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            exp = self.write_leg(Path(d), "realexp-2", REAL_EXPORT_RECEIPT, None)
            p = run_ps(
                "$vc = Get-Module VenueChain; "
                f"$a = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(2.5)); Write-Output ('A=' + $a.state + '/' + $a.provisional); "
                f"$x = Get-VenueLegHostLoad -ReceiptPath '{exp}' -PlannedLegType 'export' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(2.5)); Write-Output ('X=' + $x.state + '/' + $x.applies); "
                "& $vc { $script:VenueTestPostBytesDelayMs = 3000 }; "
                f"$b = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(2.5)); Write-Output ('B=' + $b.state + '/' + $b.provisional); Write-Output ('BWHY=' + $b.reason); "
                "& $vc { $script:VenueTestPostBytesDelayMs = 0; $script:VenueTestPostReceiptBytesDelayMs = 3000 }; "
                f"$y = Get-VenueLegHostLoad -ReceiptPath '{exp}' -PlannedLegType 'export' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(2.5)); Write-Output ('Y=' + $y.state + '/' + $y.applies); Write-Output ('YWHY=' + $y.reason); "
                "& $vc { $script:VenueTestPostReceiptBytesDelayMs = 0 }")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "quiet/False", "no stall: the read inside the deadline reads quiet: " + p.stdout)
        self.assertEqual(got["X"], "not-applicable/False", "no stall: a planned export is exempt: " + p.stdout)
        self.assertEqual(got["B"], "unknown/True", "bytes classified after the absolute end are never quiet: " + p.stdout)
        self.assertIn("absolute end", got["BWHY"], p.stdout)
        self.assertEqual(got["Y"], "unknown/True", "a receipt answer seen after the absolute end grants no export exemption: " + p.stdout)
        self.assertIn("absolute end", got["YWHY"], p.stdout)

    def test_r2res2_a2_a_call_with_no_deadline_never_returns_quiet_or_exempt_past_its_own_read_budget(self) -> None:
        # adversary-f1221390.json A2, its input: Get-VenueLegHostLoad -ReadTimeoutSec 1.5 and NO -CallDeadlineUtc over a valid receipt and result.json; the caller is suspended 2 s after the reads (the seams
        # VenueTestPostBytesDelayMs / VenueTestPostReceiptBytesDelayMs). RED on f1221390: the closure that guards the quiet and export-exempt returns judged only a supplied UTC deadline, so a call with none
        # (the baseline's, the leg's own) returned quiet / not-applicable past the shared read budget. The budget on the shared stopwatch is the end then. The chain and the batch also judge their own
        # -DeadlineUtc where they consume the standing (pinned below).
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "f1f1f1f1-f1f1-4f1f-8f1f-f1f1f1f1f1f1"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            exp = self.write_leg(Path(d), "realexp-3", REAL_EXPORT_RECEIPT, None)
            p = run_ps(
                "$vc = Get-Module VenueChain; "
                f"$a = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 1.5; Write-Output ('A=' + $a.state + '/' + $a.provisional); "
                f"$x = Get-VenueLegHostLoad -ReceiptPath '{exp}' -PlannedLegType 'export' -ReadTimeoutSec 1.5; Write-Output ('X=' + $x.state + '/' + $x.applies); "
                "& $vc { $script:VenueTestPostBytesDelayMs = 2000 }; "
                f"$b = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 1.5; Write-Output ('B=' + $b.state + '/' + $b.provisional); Write-Output ('BWHY=' + $b.reason); "
                "& $vc { $script:VenueTestPostBytesDelayMs = 0; $script:VenueTestPostReceiptBytesDelayMs = 2000 }; "
                f"$y = Get-VenueLegHostLoad -ReceiptPath '{exp}' -PlannedLegType 'export' -ReadTimeoutSec 1.5; Write-Output ('Y=' + $y.state + '/' + $y.applies); Write-Output ('YWHY=' + $y.reason); "
                "& $vc { $script:VenueTestPostReceiptBytesDelayMs = 0 }")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "quiet/False", "no stall: reads quiet: " + p.stdout)
        self.assertEqual(got["X"], "not-applicable/False", "no stall: a planned export is exempt: " + p.stdout)
        self.assertEqual(got["B"], "unknown/True", "bytes classified after the read budget are never quiet: " + p.stdout)
        self.assertIn("read budget", got["BWHY"], p.stdout)
        self.assertEqual(got["Y"], "unknown/True", "an export answer seen after the read budget is not exempt: " + p.stdout)
        for label, script in (("chain", "Invoke-VenueChain.ps1"), ("batch", "Invoke-VenueLeaseBatch.ps1")):
            text = (DV_DIR / script).read_text(encoding="utf-8")
            # R2-NARROW-1 r7 (sol r7 A3): the inline guard became the one shared Limit-VenueHostLoadToDeadline (its body is pinned by the r7 tests); the same position and the same -DeadlineUtc
            self.assertRegex(text, r"-CallDeadlineUtc \$deadline\r?\n(?:\s+#[^\n]*\r?\n)+\s+\$hl = Limit-VenueHostLoadToDeadline -Standing \$hl -DeadlineUtc \$deadline", f"{label}: the standing is judged against -DeadlineUtc where it is consumed")

    def test_r2_pass2_a5_a7_a13_the_matrix_refuses_a_replaced_receipt_the_planner_refuses_a_duplicated_legtype_the_verdict_keeps_a_list(self) -> None:
        # adversary-b3d61554.json A5: the matrix drops the receipt whose bytes are not the ones the child published (as the baseline refuses them), so a replacement's outcome and
        # fps are never attributed to the planned leg. A7: a spec that declares legType twice is planned 'unknown', never 'export'. A13: the GUI verdict helper never unrolls a
        # one-element evidence list into its clean member.
        dv = ROOT / "tools" / "profiling" / "dual-venue"
        leg = (dv / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8")
        self.assertIn("{ $legReceiptSha = ''; $legReceipt = $null }", leg)
        runner = (dv / "DualVenueRunner.psm1").read_text(encoding="utf-8")
        # MERGE-RESIDUE-1 r1 (adversary-55dcf49e.json A1): the planner takes the strict reader's answer; it no longer counts the literal text "legType":
        self.assertIn("$specPlannedLegType = Get-VenuePlannedLegType -SpecText $specText", runner)
        self.assertNotIn("$specLegTypeDeclarations", runner)
        gui = (ROOT / "tools" / "profiling" / "compare-release-gui-smoke-ab.ps1").read_text(encoding="utf-8")
        self.assertNotIn("$legEvidence = if (", gui)
        self.assertNotIn("$standing = if (", gui)
        self.assertIn("$legEvidence -isnot [System.Collections.IList]", gui)

    def test_r2_a2_a_legacy_chain_row_without_a_published_sha_binds_no_host_load(self) -> None:
        # adversary-7efea617.json A2 (the legacy-row tail), its input: a chain row with no receiptSha256 member over a receipt and result.json replaced together. RED on
        # 7c79d20b: the baseline fell back to the sha256 of its OWN read of the receipt as the expectation, so the replacement judged itself and read quiet. An empty
        # expectation never matches: the standing is unknown and provisional.
        import hashlib
        dv = ROOT / "tools" / "profiling" / "dual-venue"
        baseline = (dv / "Invoke-VenueBaseline.ps1").read_text(encoding="utf-8")
        self.assertIn("$hlExpectSha = $(if ($null -ne $publishedSha) { [string]$publishedSha } else { '' })", baseline)
        for line in baseline.splitlines():
            if "Get-VenueLegHostLoad -ReceiptPath" in line and not line.lstrip().startswith("#"):
                self.assertNotIn("$fig.receiptSha256", line, "the baseline's own read hash must never be the expectation of that read: " + line)
                self.assertEqual(line.count("-ExpectReceiptSha256"), line.count("Get-VenueLegHostLoad -ReceiptPath"), line)
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            p = run_ps(f"$a = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ExpectReceiptSha256 ''; Write-Output ('A=' + $a.state + '/' + $a.provisional)")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("A=unknown/True", p.stdout)

    def test_r6_b2_every_driver_passes_the_planned_type_to_the_reader(self) -> None:
        # The class guard: every production call of Get-VenueLegHostLoad passes -PlannedLegType from the planned spec (Get-VenuePlannedLegType).
        # adversary-de8e3799.json A3: chain, baseline and matrix read the planned type AT PLANNING (before any leg runs); the batch binds its read to the
        # preflight-judged bytes. A4: matrix and baseline bind the host-load read to the receipt bytes the figures came from.
        # adversary-fa329743.json A3/A4: the chain's scalar branch and the baseline judge the text they read for the plan; the batch records the type
        # judged from the preflight's bytes and never reopens the spec after the leg.
        drivers = {"Invoke-VenueChain.ps1": "$plannedType = Get-VenuePlannedLegType -SpecText $specText",
                   "Invoke-VenueLeaseBatch.ps1": "$script:SpecPlannedLegType[$digestKey] = Get-VenuePlannedLegType -SpecText",   # (the key is master's ordinal digest key since the #903 merge: one file under two spellings is one key)
                   "Invoke-VenueBaseline.ps1": "plannedLegType = (Get-VenuePlannedLegType -SpecText $specText)",
                   # residue r2 (adversary-07b6a932.json A3): the matrix parent takes the planner's type (judged from the bytes the child is pinned to), never a re-read
                   "Invoke-VenueLeg.ps1": "$pePlanned = $(if ($null -ne $pe.PSObject.Properties['plannedLegType'])"}
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
        # residue r2 (adversary-07b6a932.json A5): the chain and the batch bind their read to the receipt bytes the child wrote (its DVE_RECEIPT_SHA256 line)
        for script in ("Invoke-VenueChain.ps1", "Invoke-VenueLeaseBatch.ps1"):
            text = (DV_DIR / script).read_text(encoding="utf-8")
            self.assertIn("-ExpectReceiptSha256 $receiptSha", text, script)
            self.assertIn("'^DVE_RECEIPT_SHA256=[0-9A-Fa-f]{64}$'", text, script)
        self.assertIn('Write-Output "DVE_RECEIPT_SHA256=$(Get-DvLastWrittenReceiptSha256)"', (DV_DIR / "Invoke-VenueLeg.ps1").read_text(encoding="utf-8"))
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

    def test_adversary_a8_a_soft_hyphenated_leg_type_is_refused_by_the_spec_validator(self) -> None:
        # adversary-ac6205bd.json A8, its input: the shipped ProRes export spec with legType "ex<U+00AD>port". -cin (culture-aware) accepted it and every later
        # -ceq 'export' dispatched it as an export while the ordinal planned-type reader read playback. The validator's membership is ordinal now.
        spec = json.loads((DV_DIR / "legs" / "m16-1243-export-prores.json").read_text(encoding="utf-8-sig"))
        out = []
        for name, lt in (("soft-hyphen", "ex­port"), ("real", "export")):
            spec["legType"] = lt
            text = json.dumps(spec).replace("'", "''")
            out.append((name, self.run_dv(f"$s = ConvertFrom-Json -InputObject '{text}'; $r = Get-DvLegSpecShapeRefusal -Spec $s; "
                                          "Write-Output ('REFUSAL=' + $(if ($r) { [string]$r } else { '<none>' }))")))
        self.assertIn("REFUSAL=legType must be one of", out[0][1], out[0][1])
        self.assertNotIn("legType must be one of", out[1][1], "the real export legType is still accepted")

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

    def test_r3_a5_the_baselines_per_row_host_load_read_ends_at_the_runs_one_absolute_deadline(self) -> None:
        # R2-NARROW-1 r3 (adversary-baf2e69a.json A5, Invoke-VenueBaseline.ps1:492), its input: MaxTotalSec 900, the CUDA chain returns clean at 885 s, and the per-row host-load read of its
        # result.json stalls for 16 s. RED on 42b0c117: the read was a fresh default 20 s with no absolute end, so it ran to 902 s, past the run's cap, and the CPU chain was refused.
        # The row read is the one thing the acceptance needs (the baseline counts a measured row only when its standing is quiet: a provisional or unknown row is never counted), so it stays;
        # it now ends at the run's ONE deadline ($deadline, set once from MaxTotalSec): Get-VenueLegHostLoad -CallDeadlineUtc starts no read with under 1 s to that end (unknown,
        # provisional, uncounted: the safe side), cuts the shared read budget to what is left of it and never acts on an answer seen after it (pinned in the tests above).
        baseline = (DV_DIR / "Invoke-VenueBaseline.ps1").read_text(encoding="utf-8")
        reads = [l for l in baseline.splitlines() if "Get-VenueLegHostLoad -ReceiptPath $hlReceipt" in l]
        self.assertEqual(len(reads), 2, "both reading branches of the row's host-load standing: " + repr(reads))
        for line in reads:
            self.assertIn("-CallDeadlineUtc $deadline", line, "the row's host-load read is bounded by the run's absolute deadline: " + line)
        self.assertEqual(baseline.count("$deadline = $startedUtc.AddSeconds($MaxTotalSec)"), 1, "the deadline is read once and never restarted")


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class VenueChainImportResolvesTests(unittest.TestCase):
    """adversary-07b6a932.json A1: every `Import-Module ...VenueChain.psm1 -Function <names>` in tools/profiling imports names
    the module actually exports. A name the module does not export imports nothing and fails only at call time (CommandNotFound),
    which a caller's catch then mistakes for a parse failure."""

    def test_adversary_r1b_a1_every_profiling_import_of_venuechain_resolves_each_named_function(self) -> None:
        import re
        pattern = re.compile(r"Import-Module \(Join-Path \$PSScriptRoot 'dual-venue\\VenueChain\.psm1'\) -Function (.+?) -DisableNameChecking")
        sites = []
        for script in sorted((ROOT / "tools" / "profiling").glob("*.ps1")):
            for m in pattern.finditer(script.read_text(encoding="utf-8")):
                sites.append((script.name, [n.strip() for n in m.group(1).split(",")]))
        self.assertGreaterEqual(len(sites), 5, sites)
        names = sorted({n for _, ns in sites for n in ns})
        body = ("Remove-Module VenueChain -ErrorAction SilentlyContinue; Import-Module '" + str(MODULE) + "' -Function " + ", ".join(names)
                + " -DisableNameChecking -Force; foreach ($n in @(" + ", ".join("'" + n + "'" for n in names) + ")) {"
                " Write-Output ($n + '=' + [bool](Get-Command $n -ErrorAction SilentlyContinue)) }")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", body],
                           capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for script, ns in sites:
            for n in ns:
                with self.subTest(script=script, name=n):
                    self.assertIn(n + "=True", p.stdout, f"{script} imports {n} from VenueChain.psm1, which does not export it")


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class R7ReceiptDigestGetterAndBaselineObservationGuardTests(unittest.TestCase):
    """VENUE-LEG-HOSTLOAD-CIM-UNKNOWN-1-R2-NARROW-1 r7 (sol r7 B3 and A3, key3 fable r7 B3; adversary-aab90f89.json A1)."""

    write_leg = PlannedLegTypeExemptionTests.write_leg   # the one fixture writer (a receipt, its summary and result.json under a receipts/evidence pair)

    def test_r7_b3_the_receipt_digest_getter_answers_empty_on_a_fresh_module_before_any_write(self) -> None:
        # sol r7 B3, its input: import the REAL DualVenueRunner.psm1 and call the exported Get-DvLastWrittenReceiptSha256 before any Write-DvReceipt. RED on 8bdb42a5: the module runs
        # Set-StrictMode -Version Latest and the variable was assigned only after a successful write, so the getter threw VariableIsUndefined instead of the '' its comment promises.
        module = DV_DIR / "DualVenueRunner.psm1"
        body = ("$ErrorActionPreference = 'Stop'; Import-Module '" + str(module) + "' -Force -DisableNameChecking; "
                "$r = Get-DvLastWrittenReceiptSha256; Write-Output ('TYPE=' + $r.GetType().FullName); Write-Output ('VALUE=[' + $r + ']')")
        p = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", body],
                           capture_output=True, text=True, creationflags=_NO_WINDOW, timeout=120)
        self.assertEqual(p.returncode, 0, "the getter must not throw on a fresh module: " + p.stdout + p.stderr)
        self.assertIn("TYPE=System.String", p.stdout)
        self.assertIn("VALUE=[]", p.stdout)
        text = module.read_text(encoding="utf-8")
        init = text.index("$script:DvLastWrittenReceiptSha256 = ''")
        self.assertLess(init, text.index("function Get-DvLastWrittenReceiptSha256"), "the variable is initialised at module scope, before the getter that reads it")

    def test_r7_adv_a1_a_caller_descheduled_before_the_deadline_sample_keeps_its_remaining_budget(self) -> None:
        # adversary-aab90f89.json A1 (introduced), its input: -ReadTimeoutSec 20, -CallDeadlineUtc 12 s away, the caller descheduled 7 s right after the read stopwatch started (the seam
        # VenueTestPreDeadlineSampleDelayMs), a valid receipt and a quiet result.json both immediately readable. RED on aab90f89: the seconds LEFT (5) were taken as the endpoint on a stopwatch
        # already at 7, so the first read was refused with 5 s still before the deadline (unknown, provisional). The endpoint is the stopwatch reading at the sample plus the seconds left (12): quiet.
        # The control: a deadline already inside the descheduling (6 s away, 7 s asleep) still reads unknown, never quiet.
        import hashlib
        with tempfile.TemporaryDirectory() as d:
            quiet = json.dumps({"hostLoad": QUIET})
            sha = hashlib.sha256(quiet.encode("utf-8")).hexdigest()
            rid = "a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1"
            path = self.write_leg(Path(d), rid, {"receiptId": rid, "outcome": "PASS", "evidence": {"resultJsonSha256": sha}}, {"hostLoad": QUIET})
            p = run_ps(
                "$vc = Get-Module VenueChain; & $vc { $script:VenueTestPreDeadlineSampleDelayMs = 7000 }; "
                f"$a = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(12)); Write-Output ('A=' + $a.state + '/' + $a.provisional); Write-Output ('AWHY=' + $a.reason); "
                f"$b = Get-VenueLegHostLoad -ReceiptPath '{path}' -PlannedLegType 'playback' -ReadTimeoutSec 20 -CallDeadlineUtc ([DateTime]::UtcNow.AddSeconds(7.5)); Write-Output ('B=' + $b.state + '/' + $b.provisional); "
                "& $vc { $script:VenueTestPreDeadlineSampleDelayMs = 0 }")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["A"], "quiet/False", "5 s are left before the deadline and the quiet standing is readable: " + p.stdout)
        self.assertEqual(got["B"], "unknown/True", "a deadline that fell inside the descheduling is never a quiet read: " + p.stdout)

    def test_r7_a3_one_shared_guard_judges_a_standing_against_the_callers_absolute_end(self) -> None:
        # sol r7 A3, its input: the final row's reader passes quiet/nonprovisional/collected at elapsed 899.9 s and the baseline resumes at 901 s with MaxTotalSec=900. The standing
        # is judged where it is consumed: past the end it reads unknown, provisional and uncollected (never counted); before the end it is returned as it came.
        p = run_ps(
            "$q = [pscustomobject]@{ state = 'quiet'; provisional = $false; collected = $true; reason = 'peak 18% <= 75%'; applies = $true }; "
            "$late = Limit-VenueHostLoadToDeadline -Standing $q -DeadlineUtc ([DateTime]::UtcNow.AddSeconds(-1.1)); "
            "Write-Output ('LATE=' + $late.state + '/' + $late.provisional + '/' + $late.collected + '/' + $late.applies); Write-Output ('LATEWHY=' + $late.reason); "
            "$early = Limit-VenueHostLoadToDeadline -Standing $q -DeadlineUtc ([DateTime]::UtcNow.AddSeconds(60)); "
            "Write-Output ('EARLY=' + $early.state + '/' + $early.provisional + '/' + $early.collected + '/' + [object]::ReferenceEquals($early, $q)); "
            "$row = [ordered]@{ status = 'RAN'; fps = 59.9; hostLoadState = [string]$late.state; hostLoadProvisional = [bool]$late.provisional; hostLoadCollected = ($late.collected -is [bool] -and $late.collected) }; "
            "Import-Module '" + str(DV_DIR / "DualVenueRunner.psm1") + "' -Force -DisableNameChecking; "
            "Write-Output ('COUNTED=' + [bool](Test-DvHostLoadRowClean -Row $row))")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        got = dict(l.split("=", 1) for l in p.stdout.splitlines() if "=" in l)
        self.assertEqual(got["LATE"], "unknown/True/False/True", p.stdout)
        self.assertIn("absolute end", got["LATEWHY"], p.stdout)
        self.assertEqual(got["EARLY"], "quiet/False/True/True", p.stdout)
        self.assertEqual(got["COUNTED"], "False", "a row built from a late standing is never a measured row: " + p.stdout)

    def test_r7_a3_chain_batch_and_baseline_all_consume_the_standing_through_that_one_guard(self) -> None:
        import re
        for script in ("Invoke-VenueChain.ps1", "Invoke-VenueLeaseBatch.ps1", "Invoke-VenueBaseline.ps1"):
            text = (DV_DIR / script).read_text(encoding="utf-8")
            self.assertEqual(text.count("$hl = Limit-VenueHostLoadToDeadline -Standing $hl -DeadlineUtc $deadline"), 1, f"{script}: the standing is judged against its run's end where it is consumed, through the shared guard")
            self.assertNotIn("the standing was seen after the absolute end", text, f"{script}: no private copy of the guard's body")
        baseline = (DV_DIR / "Invoke-VenueBaseline.ps1").read_text(encoding="utf-8")
        self.assertRegex(baseline, r"-ExpectReceiptSha256 '' -CallDeadlineUtc \$deadline \}\)\r?\n(?:[ \t]*#[^\n]*\r?\n)*[ \t]*\$hl = Limit-VenueHostLoadToDeadline -Standing \$hl -DeadlineUtc \$deadline\r?\n(?:[ \t]*#[^\n]*\r?\n)*[ \t]*\$hlLine = ",
                         "the baseline judges the standing after the reader returns and before the refusal line and the row take it")
        chain_text = MODULE.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"^function Limit-VenueHostLoadToDeadline ", chain_text, re.M)), 1)
        exports = re.search(r"Export-ModuleMember -Function ([^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*)", chain_text).group(1)
        self.assertIn("Limit-VenueHostLoadToDeadline", exports)


if __name__ == "__main__":
    unittest.main()
