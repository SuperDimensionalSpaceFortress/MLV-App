"""Behavioural tests for DUAL-VENUE-RECONCILE-1: tools/profiling/dual-venue/Get-VenueEvidence.ps1 (C3).

WHY. The Dual-Venue Evidence framework lets Ultra-Magnus (UM) and the laptop (bachelor) run one leg each,
at their own pace, and file their own receipt (schema mlv-app/dual-venue-receipt/v1). The reconciler is the
ONLY reader acceptance may use, so every refusal it makes is pinned here:
  P2 no merged verdict: a pair is COMPLETE only when every venue in the card's roles has a PASS/FAIL receipt
     at the SAME subject digest; the cross-venue delta is DIAGNOSTIC and exists only for COMPLETE pairs.
  P3 role is data: -AcceptanceFor <card> returns only receipts whose venue.role is `acceptance` for that card,
     and a receipt whose recorded role disagrees with venues.json is refused (ROLE_MISMATCH).
  P4 only PASS/FAIL carries signal; a malformed receipt is reported and never counted.
  P6 a PASS/FAIL receipt must have been run on the venue it claims (declared == detected == venue.name).
  Acceptance never rests on a self-declared role: -AcceptanceFor needs a PRESENT, valid venue table that lists
  the card (absent -> NO_ACCEPTANCE_EVIDENCE VENUE_TABLE_ABSENT, exit 2; unlisted card -> CARD_NOT_IN_VENUE_TABLE).
  Exit codes: 0 acceptance evidence, all PASS; 3 acceptance evidence that includes a FAIL; 2 none; 1 tool error.
  Owner rule 2026-09-30: a PASS/FAIL receipt is evidence only with metrics.clipSeconds >= 20 and metrics.wrapped == 0.

The tests EXECUTE the real script in a child pwsh against SYNTHETIC receipts in a temp dir (pure file logic: no
venue, no job, no network). They never touch the real .claude-state receipts root.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DV_DIR = ROOT / "tools" / "profiling" / "dual-venue"
SCRIPT = DV_DIR / "Get-VenueEvidence.ps1"
MODULE = DV_DIR / "VenueEvidence.psm1"

PWSH = shutil.which("pwsh")
requires_pwsh = unittest.skipIf(PWSH is None, "pwsh is not on PATH")

CARD = "CARD-A"
LEG = "leg-play-1"
BACH = "bachelor"
UM = "ultra-magnus"
DIGEST_1 = hashlib.sha256(b"subject-1").hexdigest()
DIGEST_2 = hashlib.sha256(b"subject-2").hexdigest()

VENUE_TABLE = {
    "venues": {
        BACH: {"agentShare": "x", "agentRoot": "x", "scratchRoot": "x", "expectedHost": "x"},
        UM: {"agentShare": "x", "agentRoot": "x", "scratchRoot": "x", "expectedHost": "x"},
    },
    "roles": {
        CARD: {BACH: "acceptance", UM: "supplementary"},
        "CARD-B": {BACH: "supplementary", UM: "acceptance"},
    },
    "defaultRole": "supplementary",
}


def make_receipt(
    *,
    card: str = CARD,
    leg: str = LEG,
    venue: str = BACH,
    role: str | None = None,
    outcome: str = "PASS",
    digest: str = DIGEST_1,
    finished: str = "2026-09-30T10:00:00Z",
    started: str | None = None,
    metrics: dict | None = None,
    backend: str | None = None,
    receipt_id: str | None = None,
    declared: str | None = None,
    detected: str | None = None,
    build: str = "b" * 64,
    leg_spec: str = "c" * 64,
    clip_id: str = "fixture-1",
    look_flavor: str | None = None,
    clip_seconds: float | None = 25.0,
    wrapped: object | None = 0,
    clip_keys: tuple[str, str] = ("clipSeconds", "wrapped"),
) -> dict:
    if role is None:
        role = VENUE_TABLE["roles"].get(card, {}).get(venue, VENUE_TABLE["defaultRole"])
    subject = {
        "digest": digest,
        "buildManifestSha256": build,
        "legSpecSha256": leg_spec,
        "clipId": clip_id,
        "clipContentSha256": "d" * 64,
    }
    if backend:
        subject["backend"] = backend
    if look_flavor is not None:
        subject["lookFlavor"] = look_flavor
    metrics = dict(metrics) if metrics is not None else {"framesPresented": 100, "framesExpected": 100, "stalls": 0}
    # Owner rule 2026-09-30: playback metrics carry the clip length and whether the timeline wrapped.
    if clip_seconds is not None:
        metrics[clip_keys[0]] = clip_seconds
    if wrapped is not None:
        metrics[clip_keys[1]] = wrapped
    return {
        "schema": "mlv-app/dual-venue-receipt/v1",
        "receiptId": receipt_id or str(uuid.uuid4()),
        "card": card,
        "legId": leg,
        "subject": subject,
        "venue": {
            "name": venue,
            "role": role,
            "declared": declared or venue,
            "detected": detected or venue,
            "hostName": "HOST",
            "gpuNames": ["GPU"],
            "driverVersion": "1",
            "displayDevice": "d",
            "instrumentDigests": {"presentmon": "p", "scorer": "s"},
        },
        "actor": "lane-test",
        "method": {"script": "x.ps1", "blobId": "abc"},
        "startedUtc": started or "2026-09-30T00:00:00Z",
        "finishedUtc": finished,
        "health": {"outcome": "OK", "pwshColdStartMs": 100, "smallHashMs": 5, "freeDiskGiB": 50, "commitUsedGiB": 1},
        "outcome": outcome,
        "outcomeDetail": "",
        "evidence": {
            "summaryJsonSha256": "e" * 64,
            "evidenceManifestSha256": "f" * 64,
            "artifactIndexPath": "x",
            "umRunOutcome": "RECEIPT",
        },
        "metrics": metrics,
    }


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dv-reconcile-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "receipts"
        self.root.mkdir()
        self.table = self.tmp / "venues.json"
        self.table.write_text(json.dumps(VENUE_TABLE), encoding="utf-8")

    def put(self, receipt: dict, *, rel: Path | None = None) -> Path:
        if rel is None:
            rel = Path(receipt["card"]) / receipt["legId"] / receipt["venue"]["name"] / f"{receipt['receiptId']}.json"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        return path

    def run_tool(self, *extra: str, table: bool = True) -> subprocess.CompletedProcess:
        cmd = [
            PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), "-ReceiptsRoot", str(self.root),
        ]
        if table:
            cmd += ["-VenueTable", str(self.table)]
        else:
            cmd += ["-VenueTable", str(self.tmp / "no-such-venues.json")]
        cmd += list(extra)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    def report(self, *extra: str, table: bool = True) -> dict:
        proc = self.run_tool("-Json", *extra, table=table)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return json.loads(proc.stdout)

    def group(self, rep: dict, digest: str = DIGEST_1, leg: str = LEG, card: str = CARD) -> dict:
        found = [g for g in rep["groups"] if g["subjectDigest"] == digest and g["legId"] == leg and g["card"] == card]
        self.assertEqual(len(found), 1, json.dumps(rep, indent=2))
        return found[0]


@requires_pwsh
class ScaffoldTests(_Base):
    def test_files_exist_and_are_ascii(self) -> None:
        for path in (SCRIPT, MODULE):
            self.assertTrue(path.is_file(), f"missing {path}")
            path.read_bytes().decode("ascii")  # ASCII-only scripts (cp1252-safe)

    def test_json_carries_the_report_schema_id(self) -> None:
        rep = self.report()
        self.assertEqual(rep["schema"], "mlv-app/dual-venue-evidence-report/v1")
        self.assertEqual(rep["groups"], [])

    def test_missing_receipts_root_is_zero_receipts_not_a_crash(self) -> None:
        shutil.rmtree(self.root)
        rep = self.report()
        self.assertEqual(rep["groups"], [])
        self.assertFalse(rep["receiptsRootExists"])

    def test_default_human_table_prints(self) -> None:
        self.put(make_receipt(venue=BACH))
        proc = self.run_tool()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(CARD, proc.stdout)
        self.assertIn("INCOMPLETE", proc.stdout)

    def test_reconciler_never_modifies_receipts(self) -> None:
        path = self.put(make_receipt(venue=BACH))
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        self.run_tool()
        self.run_tool("-Json")
        self.run_tool("-AcceptanceFor", CARD)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


@requires_pwsh
class PairingTests(_Base):
    def test_um_pass_plus_bachelor_unresolved_is_incomplete_not_green(self) -> None:
        self.put(make_receipt(venue=UM, outcome="PASS"))
        self.put(make_receipt(venue=BACH, outcome="UNRESOLVED"))
        g = self.group(self.report())
        self.assertEqual(g["pair"]["state"], "INCOMPLETE")
        self.assertIn({"venue": BACH, "reason": "UNRESOLVED"}, g["pair"]["reasons"])
        self.assertNotIn("delta", g)
        self.assertEqual(g["venues"][UM]["latest"]["outcome"], "PASS")
        self.assertEqual(g["venues"][BACH]["latest"]["outcome"], "UNRESOLVED")

    def test_incomplete_pair_human_output_prints_no_delta(self) -> None:
        self.put(make_receipt(venue=UM, outcome="PASS"))
        self.put(make_receipt(venue=BACH, outcome="UNRESOLVED"))
        out = self.run_tool().stdout
        self.assertIn("INCOMPLETE", out)
        self.assertNotIn("DIAGNOSTIC", out)
        self.assertNotIn("COMPLETE\n", out.replace("INCOMPLETE", ""))

    def test_missing_venue_is_incomplete_with_reason_missing(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        g = self.group(self.report())
        self.assertEqual(g["pair"]["state"], "INCOMPLETE")
        self.assertIn({"venue": UM, "reason": "missing"}, g["pair"]["reasons"])

    def test_every_non_signal_outcome_names_itself_as_the_reason(self) -> None:
        for outcome in ("RETRACTED", "VENUE_UNHEALTHY", "VENUE_NOT_QUIESCENT", "VENUE_HOST_MISMATCH",
                        "DEVICE_UNAVAILABLE", "UNRESOLVED"):
            with self.subTest(outcome=outcome):
                shutil.rmtree(self.root)
                self.root.mkdir()
                self.put(make_receipt(venue=BACH, outcome="PASS"))
                self.put(make_receipt(venue=UM, outcome=outcome))
                g = self.group(self.report())
                self.assertEqual(g["pair"]["state"], "INCOMPLETE")
                self.assertIn({"venue": UM, "reason": outcome}, g["pair"]["reasons"])

    def test_complete_pair_has_a_delta_labelled_diagnostic_and_no_merged_verdict(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", metrics={"framesPresented": 100, "stalls": 0, "note": "a"}))
        self.put(make_receipt(venue=UM, outcome="FAIL", metrics={"framesPresented": 90, "stalls": 0, "note": "b"}))
        rep = self.report()
        g = self.group(rep)
        self.assertEqual(g["pair"]["state"], "COMPLETE")
        self.assertEqual(g["pair"]["reasons"], [])
        delta = g["delta"]
        self.assertEqual(delta["label"], "DIAGNOSTIC")
        fp = delta["metrics"]["framesPresented"]
        self.assertEqual(fp["values"], {BACH: 100, UM: 90})
        self.assertTrue(fp["differs"])
        self.assertEqual(fp["diff"], -10)  # ultra-magnus minus bachelor
        self.assertFalse(delta["metrics"]["stalls"]["differs"])
        self.assertTrue(delta["metrics"]["note"]["differs"])
        # P2: per-venue outcomes are reported side by side; there is no overall / merged verdict anywhere.
        self.assertEqual(g["venues"][BACH]["latest"]["outcome"], "PASS")
        self.assertEqual(g["venues"][UM]["latest"]["outcome"], "FAIL")
        blob = json.dumps(rep).lower()
        for banned in ('"verdict"', '"overall"', '"merged"', '"combined"'):
            self.assertNotIn(banned, blob)

    def test_complete_pair_human_output_labels_the_delta_diagnostic(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        self.put(make_receipt(venue=UM, outcome="PASS", metrics={"framesPresented": 95}))
        out = self.run_tool().stdout
        self.assertIn("DIAGNOSTIC", out)
        self.assertIn("framesPresented", out)

    def test_digests_differ_are_never_paired_and_the_tool_says_so(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", digest=DIGEST_1))
        self.put(make_receipt(venue=UM, outcome="PASS", digest=DIGEST_2))
        rep = self.report()
        self.assertEqual(len(rep["groups"]), 2)
        for digest, present, absent in ((DIGEST_1, BACH, UM), (DIGEST_2, UM, BACH)):
            g = self.group(rep, digest)
            self.assertEqual(g["pair"]["state"], "INCOMPLETE")
            self.assertIn({"venue": absent, "reason": "missing"}, g["pair"]["reasons"])
            self.assertNotIn("delta", g)
            self.assertIsNotNone(g["venues"][present]["latest"])
            self.assertIsNone(g["venues"][absent]["latest"])
        self.assertTrue(any("never compared" in n for n in rep["digestNotes"]), rep["digestNotes"])
        self.assertIn("never compared", self.run_tool().stdout)

    def test_backend_keeps_cuda_and_cpu_receipts_apart(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", backend="cuda"))
        self.put(make_receipt(venue=UM, outcome="PASS", backend="cpu"))
        rep = self.report()
        self.assertEqual(len(rep["groups"]), 2)
        self.assertTrue(all(g["pair"]["state"] == "INCOMPLETE" for g in rep["groups"]))

    def test_latest_receipt_wins_per_venue_and_history_is_counted(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="FAIL", finished="2026-09-30T08:00:00Z"))
        newest = make_receipt(venue=BACH, outcome="PASS", finished="2026-09-30T11:00:00Z")
        self.put(newest)
        self.put(make_receipt(venue=BACH, outcome="UNRESOLVED", finished="2026-09-30T09:00:00Z"))
        self.put(make_receipt(venue=UM, outcome="PASS"))
        g = self.group(self.report())
        b = g["venues"][BACH]
        self.assertEqual(b["latest"]["receiptId"], newest["receiptId"])
        self.assertEqual(b["history"]["total"], 3)
        self.assertEqual(b["history"]["byOutcome"], {"FAIL": 1, "PASS": 1, "UNRESOLVED": 1})
        self.assertEqual(g["pair"]["state"], "COMPLETE")

    def test_latest_is_by_instant_not_by_string_so_offsets_are_honoured(self) -> None:
        older = make_receipt(venue=BACH, outcome="FAIL", finished="2026-09-30T10:30:00+02:00")  # 08:30Z
        newer = make_receipt(venue=BACH, outcome="PASS", finished="2026-09-30T09:00:00Z")
        self.put(older)
        self.put(newer)
        g = self.group(self.report())
        self.assertEqual(g["venues"][BACH]["latest"]["receiptId"], newer["receiptId"])

    def test_tie_on_finished_between_equal_outcomes_breaks_by_receipt_id(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", receipt_id="aaaa-1"))
        self.put(make_receipt(venue=BACH, outcome="PASS", receipt_id="zzzz-1"))
        g = self.group(self.report())
        self.assertEqual(g["venues"][BACH]["latest"]["receiptId"], "zzzz-1")

    def test_tie_on_finished_prefers_the_non_signal_receipt_whatever_the_ids(self) -> None:
        # DVE-TIE-BREAK-FAIL-CLOSED-1: a random uuid must not decide between a PASS and a retraction.
        self.put(make_receipt(venue=BACH, outcome="RETRACTED", receipt_id="aaaa-1"))
        self.put(make_receipt(venue=BACH, outcome="PASS", receipt_id="zzzz-1"))
        self.put(make_receipt(venue=UM, outcome="PASS"))
        g = self.group(self.report())
        self.assertEqual(g["venues"][BACH]["latest"]["outcome"], "RETRACTED")
        self.assertEqual(g["pair"]["state"], "INCOMPLETE")
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 2, proc.stdout)

    def test_tie_on_finished_prefers_fail_over_pass(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="FAIL", receipt_id="aaaa-1"))
        self.put(make_receipt(venue=BACH, outcome="PASS", receipt_id="zzzz-1"))
        g = self.group(self.report())
        self.assertEqual(g["venues"][BACH]["latest"]["outcome"], "FAIL")

    def test_enum_values_are_case_sensitive_so_a_lowercase_outcome_is_malformed(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="pass"))
        rep = self.report()
        self.assertEqual(rep["counts"]["valid"], 0)
        self.assertEqual(rep["counts"]["malformed"], 1)
        r = make_receipt(venue=BACH, outcome="PASS")
        r["venue"]["role"] = "Acceptance"
        self.put(r)
        self.assertEqual(self.report()["counts"]["malformed"], 2)

    def test_a_newer_retraction_supersedes_an_older_pass(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", finished="2026-09-30T08:00:00Z"))
        self.put(make_receipt(venue=BACH, outcome="RETRACTED", finished="2026-09-30T09:00:00Z"))
        self.put(make_receipt(venue=UM, outcome="PASS"))
        g = self.group(self.report())
        self.assertEqual(g["pair"]["state"], "INCOMPLETE")
        self.assertIn({"venue": BACH, "reason": "RETRACTED"}, g["pair"]["reasons"])

    def test_expected_venues_come_from_the_card_roles(self) -> None:
        table = json.loads(self.table.read_text(encoding="utf-8"))
        table["roles"][CARD] = {BACH: "acceptance"}
        self.table.write_text(json.dumps(table), encoding="utf-8")
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        g = self.group(self.report())
        self.assertEqual(g["expectedVenues"], [BACH])
        self.assertEqual(g["pair"]["state"], "COMPLETE")

    def test_card_filter_limits_the_report(self) -> None:
        self.put(make_receipt(card=CARD, venue=BACH))
        self.put(make_receipt(card="CARD-B", venue=BACH))
        rep = self.report("-Card", "CARD-B")
        self.assertEqual({g["card"] for g in rep["groups"]}, {"CARD-B"})


@requires_pwsh
class MalformedTests(_Base):
    def _counts_and_reasons(self) -> dict:
        return self.report()

    def test_malformed_receipts_are_reported_and_never_counted(self) -> None:
        good = make_receipt(venue=BACH, outcome="PASS")
        self.put(good)
        # 1 invalid JSON
        bad_json = self.root / CARD / LEG / UM / "not-json.json"
        bad_json.parent.mkdir(parents=True, exist_ok=True)
        bad_json.write_text("{ this is not json", encoding="utf-8")
        # 2 missing required field
        r = make_receipt(venue=UM, outcome="PASS")
        del r["subject"]["clipContentSha256"]
        self.put(r)
        # 3 unknown outcome
        self.put(make_receipt(venue=UM, outcome="MOSTLY_PASS"))
        # 4 wrong schema id
        r = make_receipt(venue=UM, outcome="PASS")
        r["schema"] = "mlv-app/dual-venue-receipt/v2"
        self.put(r)
        # 5 digest not sha256 hex
        self.put(make_receipt(venue=UM, outcome="PASS", digest="nothex"))
        # 6 finished before started
        self.put(make_receipt(venue=UM, outcome="PASS", started="2026-09-30T12:00:00Z",
                              finished="2026-09-30T10:00:00Z"))
        # 7 timestamp without a zone
        self.put(make_receipt(venue=UM, outcome="PASS", finished="2026-09-30T10:00:00"))
        # 8 venue name not in the enum
        r = make_receipt(venue=UM, outcome="PASS")
        r["venue"]["name"] = "laptop-2"
        self.put(r, rel=Path(CARD) / LEG / UM / "laptop.json")
        # 9 top-level JSON is not an object
        arr = self.root / CARD / LEG / UM / "array.json"
        arr.write_text("[1,2,3]", encoding="utf-8")
        # 10 metrics is not an object
        r = make_receipt(venue=UM, outcome="PASS")
        r["metrics"] = 5
        self.put(r)

        rep = self.report()
        self.assertEqual(rep["counts"]["valid"], 1)
        self.assertEqual(rep["counts"]["malformed"], 10)
        self.assertEqual(len(rep["malformed"]), 10)
        self.assertTrue(all(m["reasons"] for m in rep["malformed"]))
        g = self.group(rep)
        self.assertEqual(g["venues"][BACH]["history"]["total"], 1)
        self.assertIsNone(g["venues"][UM]["latest"])
        self.assertIn({"venue": UM, "reason": "missing"}, g["pair"]["reasons"])
        self.assertIn("MALFORMED", self.run_tool().stdout)

    def test_a_malformed_newer_receipt_never_displaces_a_valid_one(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", finished="2026-09-30T08:00:00Z"))
        self.put(make_receipt(venue=BACH, outcome="FAIL", finished="2026-09-30T12:00:00Z", digest="bad"))
        g = self.group(self.report())
        self.assertEqual(g["venues"][BACH]["latest"]["outcome"], "PASS")

    def test_receipt_filed_under_the_wrong_venue_folder_is_malformed(self) -> None:
        r = make_receipt(venue=BACH, outcome="PASS")
        self.put(r, rel=Path(CARD) / LEG / UM / f"{r['receiptId']}.json")
        rep = self.report()
        self.assertEqual(rep["counts"]["malformed"], 1)
        self.assertEqual(rep["groups"], [])

    def test_duplicate_receipt_ids_are_both_refused(self) -> None:
        dup = "dup-1"
        self.put(make_receipt(venue=BACH, outcome="PASS", receipt_id=dup))
        other = make_receipt(venue=BACH, leg="leg-other", outcome="PASS", receipt_id=dup)
        self.put(other)
        rep = self.report()
        self.assertEqual(rep["counts"]["valid"], 0)
        self.assertEqual(rep["counts"]["refused"], 2)
        self.assertTrue(all(x["reason"] == "DUPLICATE_RECEIPT_ID" for x in rep["refused"]))

    def test_oversized_receipt_is_malformed_not_read(self) -> None:
        big = self.root / CARD / LEG / BACH / "big.json"
        big.parent.mkdir(parents=True, exist_ok=True)
        big.write_text(" " * (2 * 1024 * 1024), encoding="utf-8")
        rep = self.report()
        self.assertEqual(rep["counts"]["malformed"], 1)

    def test_invalid_venue_table_is_a_tool_error_not_a_silent_pass(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        self.table.write_text("{ not json", encoding="utf-8")
        proc = self.run_tool("-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertNotIn("NO_ACCEPTANCE_EVIDENCE", proc.stdout)
        table = json.loads(json.dumps(VENUE_TABLE))
        table["roles"][CARD][BACH] = "sovereign"
        self.table.write_text(json.dumps(table), encoding="utf-8")
        self.assertEqual(self.run_tool("-AcceptanceFor", CARD).returncode, 1)


@requires_pwsh
class AcceptanceTests(_Base):
    def test_um_pass_only_yields_no_acceptance_evidence(self) -> None:
        um = make_receipt(venue=UM, outcome="PASS")  # supplementary for CARD
        self.put(um)
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["acceptance"]["status"], "NO_ACCEPTANCE_EVIDENCE")
        self.assertEqual(rep["acceptance"]["receipts"], [])
        self.assertNotIn(um["receiptId"], proc.stdout)  # a supplementary PASS never appears, anywhere
        human = self.run_tool("-AcceptanceFor", CARD)
        self.assertIn("NO_ACCEPTANCE_EVIDENCE", human.stdout)
        self.assertNotIn(um["receiptId"], human.stdout)

    def test_no_receipts_at_all_is_no_acceptance_evidence(self) -> None:
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["acceptance"]["status"], "NO_ACCEPTANCE_EVIDENCE")

    def test_only_the_acceptance_venue_receipt_is_returned(self) -> None:
        b = make_receipt(venue=BACH, outcome="PASS")
        u = make_receipt(venue=UM, outcome="PASS")
        self.put(b)
        self.put(u)
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["acceptance"]["status"], "ACCEPTANCE_EVIDENCE")
        ids = [r["receiptId"] for r in rep["acceptance"]["receipts"]]
        self.assertEqual(ids, [b["receiptId"]])
        self.assertEqual(rep["acceptance"]["receipts"][0]["role"], "acceptance")
        self.assertTrue(rep["acceptance"]["receipts"][0]["roleVerified"])
        self.assertNotIn(u["receiptId"], proc.stdout)
        self.assertNotIn("groups", rep)  # acceptance mode carries no per-venue supplementary rows

    def test_acceptance_is_per_card(self) -> None:
        a_b = make_receipt(card="CARD-B", venue=BACH, outcome="PASS")  # supplementary for CARD-B
        a_u = make_receipt(card="CARD-B", venue=UM, outcome="PASS")  # acceptance for CARD-B
        self.put(a_b)
        self.put(a_u)
        rep = json.loads(self.run_tool("-Json", "-AcceptanceFor", "CARD-B").stdout)
        self.assertEqual([r["receiptId"] for r in rep["acceptance"]["receipts"]], [a_u["receiptId"]])

    def test_an_acceptance_fail_is_evidence_but_exits_3_never_0(self) -> None:
        # DVE-ACCEPTANCE-EXIT-FAIL-1: a caller gating on "exit 0" must never read a FAIL as green.
        self.put(make_receipt(venue=BACH, outcome="FAIL"))
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["acceptance"]["status"], "ACCEPTANCE_EVIDENCE")
        self.assertEqual(rep["acceptance"]["receipts"][0]["outcome"], "FAIL")
        self.assertEqual(self.run_tool("-AcceptanceFor", CARD).returncode, 3)

    def test_one_fail_among_passes_still_exits_3(self) -> None:
        self.put(make_receipt(venue=BACH, leg="leg-a", outcome="PASS"))
        self.put(make_receipt(venue=BACH, leg="leg-b", outcome="FAIL"))
        self.assertEqual(self.run_tool("-Json", "-AcceptanceFor", CARD).returncode, 3)

    def test_exit_0_only_when_acceptance_evidence_is_present_and_all_pass(self) -> None:
        self.put(make_receipt(venue=BACH, leg="leg-a", outcome="PASS"))
        self.put(make_receipt(venue=BACH, leg="leg-b", outcome="PASS"))
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["acceptance"]["reason"], None)

    def test_non_signal_acceptance_receipt_is_withheld_not_evidence(self) -> None:
        for outcome in ("UNRESOLVED", "RETRACTED", "VENUE_UNHEALTHY"):
            with self.subTest(outcome=outcome):
                shutil.rmtree(self.root)
                self.root.mkdir()
                self.put(make_receipt(venue=BACH, outcome=outcome))
                proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
                self.assertEqual(proc.returncode, 2)
                rep = json.loads(proc.stdout)
                self.assertEqual(rep["acceptance"]["status"], "NO_ACCEPTANCE_EVIDENCE")
                self.assertEqual(rep["acceptance"]["receipts"], [])
                self.assertEqual(rep["acceptance"]["withheld"][0]["reason"], outcome)

    def test_a_newer_unresolved_withholds_an_older_acceptance_pass_at_the_same_digest(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", finished="2026-09-30T08:00:00Z"))
        self.put(make_receipt(venue=BACH, outcome="RETRACTED", finished="2026-09-30T09:00:00Z"))
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["acceptance"]["status"], "NO_ACCEPTANCE_EVIDENCE")

    def test_without_a_filter_acceptance_reports_only_the_newest_digest_per_leg_and_says_so(self) -> None:
        r1 = make_receipt(venue=BACH, outcome="PASS", digest=DIGEST_1)
        r2 = make_receipt(venue=BACH, outcome="FAIL", digest=DIGEST_2, finished="2026-09-30T11:00:00Z")
        self.put(r1)
        self.put(r2)
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        rep = json.loads(proc.stdout)
        got = {r["subjectDigest"]: r["outcome"] for r in rep["acceptance"]["receipts"]}
        self.assertEqual(got, {DIGEST_2: "FAIL"})
        self.assertEqual(rep["acceptance"]["digestSelection"], "NEWEST_PER_LEG")
        self.assertEqual([o["subjectDigest"] for o in rep["acceptance"]["olderDigests"]], [DIGEST_1])
        self.assertNotIn(r1["receiptId"], json.dumps(rep["acceptance"]["receipts"]))
        self.assertIn("NEWEST", self.run_tool("-AcceptanceFor", CARD).stdout)

    def test_an_old_digest_pass_is_not_acceptance_evidence_when_the_newest_digest_is_unresolved(self) -> None:
        # DVE-ACCEPTANCE-SUBJECT-FILTER-1 repro.
        self.put(make_receipt(venue=BACH, outcome="PASS", digest=DIGEST_1, finished="2026-09-30T08:00:00Z"))
        self.put(make_receipt(venue=BACH, outcome="UNRESOLVED", digest=DIGEST_2, finished="2026-09-30T11:00:00Z"))
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["acceptance"]["receipts"], [])
        self.assertEqual(rep["acceptance"]["withheld"][0]["subjectDigest"], DIGEST_2)
        # ... but a reader who pins the old digest explicitly still gets it, as a filtered answer.
        pinned = self.run_tool("-Json", "-AcceptanceFor", CARD, "-SubjectDigest", DIGEST_1)
        self.assertEqual(pinned.returncode, 0, pinned.stdout)
        prep = json.loads(pinned.stdout)
        self.assertEqual(prep["acceptance"]["digestSelection"], "FILTERED")
        self.assertEqual([r["subjectDigest"] for r in prep["acceptance"]["receipts"]], [DIGEST_1])

    def test_digest_selection_is_per_leg_and_per_backend(self) -> None:
        self.put(make_receipt(venue=BACH, leg="leg-a", outcome="PASS", digest=DIGEST_1, finished="2026-09-30T08:00:00Z"))
        self.put(make_receipt(venue=BACH, leg="leg-b", outcome="PASS", digest=DIGEST_2, finished="2026-09-30T11:00:00Z"))
        self.put(make_receipt(venue=BACH, leg="leg-a", outcome="PASS", digest=DIGEST_2, finished="2026-09-30T09:00:00Z",
                              backend="cpu"))
        rep = json.loads(self.run_tool("-Json", "-AcceptanceFor", CARD).stdout)
        got = sorted(((r["legId"], r["backend"] or "", r["subjectDigest"]) for r in rep["acceptance"]["receipts"]))
        self.assertEqual(got, [("leg-a", "", DIGEST_1), ("leg-a", "cpu", DIGEST_2), ("leg-b", "", DIGEST_2)])

    def test_subject_digest_filter_selects_that_digest_only(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", digest=DIGEST_1, finished="2026-09-30T08:00:00Z"))
        self.put(make_receipt(venue=BACH, outcome="FAIL", digest=DIGEST_2, finished="2026-09-30T11:00:00Z"))
        upper = DIGEST_1.upper()
        rep = json.loads(self.run_tool("-Json", "-AcceptanceFor", CARD, "-SubjectDigest", upper).stdout)
        self.assertEqual([(r["subjectDigest"], r["outcome"]) for r in rep["acceptance"]["receipts"]], [(DIGEST_1, "PASS")])

    def test_build_manifest_filter_binds_evidence_to_the_candidate_build(self) -> None:
        build_old, build_new = "1" * 64, "2" * 64
        self.put(make_receipt(venue=BACH, outcome="PASS", digest=DIGEST_1, build=build_old, finished="2026-09-30T08:00:00Z"))
        self.put(make_receipt(venue=BACH, outcome="PASS", digest=DIGEST_2, build=build_new, finished="2026-09-30T09:00:00Z"))
        rep = json.loads(self.run_tool("-Json", "-AcceptanceFor", CARD, "-BuildManifestSha256", build_old).stdout)
        self.assertEqual([r["buildManifestSha256"] for r in rep["acceptance"]["receipts"]], [build_old])
        none = self.run_tool("-Json", "-AcceptanceFor", CARD, "-BuildManifestSha256", "9" * 64)
        self.assertEqual(none.returncode, 2, none.stdout)

    def test_both_filters_must_match(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", digest=DIGEST_1, build="1" * 64))
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD, "-SubjectDigest", DIGEST_1, "-BuildManifestSha256", "2" * 64)
        self.assertEqual(proc.returncode, 2, proc.stdout)

    def test_a_malformed_filter_is_a_tool_error_not_an_empty_answer(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        self.assertEqual(self.run_tool("-Json", "-AcceptanceFor", CARD, "-SubjectDigest", "nothex").returncode, 1)
        self.assertEqual(self.run_tool("-Json", "-AcceptanceFor", CARD, "-BuildManifestSha256", "abc").returncode, 1)

    def test_acceptance_receipt_view_binds_to_the_build(self) -> None:
        r = make_receipt(venue=BACH, outcome="PASS", backend="cuda", look_flavor="cinematic", build="a1" * 32,
                         leg_spec="b2" * 32, clip_id="owner-clip-9")
        self.put(make_receipt(venue=BACH, outcome="FAIL", backend="cuda", look_flavor="cinematic", build="a1" * 32,
                              leg_spec="b2" * 32, clip_id="owner-clip-9", finished="2026-09-30T08:00:00Z",
                              digest=r["subject"]["digest"]))
        self.put(r)
        rep = json.loads(self.run_tool("-Json", "-AcceptanceFor", CARD).stdout)
        view = rep["acceptance"]["receipts"][0]
        self.assertEqual(view["buildManifestSha256"], "a1" * 32)
        self.assertEqual(view["legSpecSha256"], "b2" * 32)
        self.assertEqual(view["clipId"], "owner-clip-9")
        self.assertEqual(view["backend"], "cuda")
        self.assertEqual(view["lookFlavor"], "cinematic")
        # DVE-ACCEPTANCE-HISTORY-1: a PASS that followed a FAIL at the same digest says so.
        self.assertEqual(view["history"], {"total": 2, "byOutcome": {"FAIL": 1, "PASS": 1}})
        human = self.run_tool("-AcceptanceFor", CARD).stdout
        for needle in (("a1" * 32)[:12], "owner-clip-9", "cuda", "cinematic"):
            self.assertIn(needle, human)

    def test_supplementary_receipt_relabelled_acceptance_is_refused(self) -> None:
        forged = make_receipt(venue=UM, outcome="PASS", role="acceptance")  # table says supplementary
        self.put(forged)
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["acceptance"]["status"], "NO_ACCEPTANCE_EVIDENCE")
        self.assertEqual(rep["acceptance"]["receipts"], [])
        self.assertEqual([x["reason"] for x in rep["refused"]], ["ROLE_MISMATCH"])
        self.assertEqual(rep["counts"]["refused"], 1)

    def test_acceptance_receipt_relabelled_supplementary_is_refused_too(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", role="supplementary"))  # table says acceptance
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 2)
        rep = json.loads(proc.stdout)
        self.assertEqual([x["reason"] for x in rep["refused"]], ["ROLE_MISMATCH"])

    def test_role_mismatch_is_refused_in_the_normal_report_and_never_paired(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        self.put(make_receipt(venue=UM, outcome="PASS", role="acceptance"))
        rep = self.report()
        self.assertEqual([x["reason"] for x in rep["refused"]], ["ROLE_MISMATCH"])
        g = self.group(rep)
        self.assertEqual(g["pair"]["state"], "INCOMPLETE")
        self.assertIn({"venue": UM, "reason": "missing"}, g["pair"]["reasons"])

    def test_card_absent_from_the_table_takes_the_default_role_so_an_acceptance_claim_is_refused(self) -> None:
        self.put(make_receipt(card="CARD-NEW", venue=BACH, outcome="PASS", role="acceptance"))
        proc = self.run_tool("-Json", "-AcceptanceFor", "CARD-NEW")
        self.assertEqual(proc.returncode, 2)
        rep = json.loads(proc.stdout)
        self.assertEqual([x["reason"] for x in rep["refused"]], ["ROLE_MISMATCH"])
        self.assertEqual(rep["acceptance"]["reason"], "CARD_NOT_IN_VENUE_TABLE")

    def test_card_absent_from_the_table_yields_no_acceptance_evidence_whatever_the_receipt_says(self) -> None:
        # The table lists CARD-A and CARD-B only. Even a receipt whose own role is "acceptance" for an unlisted card
        # is a label, not data: there is no acceptance venue to read.
        forged = make_receipt(card="CARD-NEW", venue=UM, outcome="PASS", role="acceptance")
        self.put(forged)
        for extra in ((), ("-Json",)):
            proc = self.run_tool(*extra, "-AcceptanceFor", "CARD-NEW")
            self.assertEqual(proc.returncode, 2, proc.stdout)
            self.assertIn("NO_ACCEPTANCE_EVIDENCE", proc.stdout)
            self.assertIn("CARD_NOT_IN_VENUE_TABLE", proc.stdout)
        rep = json.loads(self.run_tool("-Json", "-AcceptanceFor", "CARD-NEW").stdout)
        self.assertEqual(rep["acceptance"]["receipts"], [])
        self.assertEqual(rep["acceptance"]["status"], "NO_ACCEPTANCE_EVIDENCE")

    def test_absent_venue_table_fails_closed_for_a_relabelled_supplementary_receipt(self) -> None:
        # Both review keys (sol + fable, r1) found this FAILING OPEN: exit 0 ACCEPTANCE_EVIDENCE on a forged role.
        forged = make_receipt(venue=UM, outcome="PASS", role="acceptance")  # the table would say supplementary
        self.put(forged)
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD, table=False)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        rep = json.loads(proc.stdout)
        self.assertFalse(rep["venueTable"]["present"])
        self.assertEqual(rep["acceptance"]["status"], "NO_ACCEPTANCE_EVIDENCE")
        self.assertEqual(rep["acceptance"]["reason"], "VENUE_TABLE_ABSENT")
        self.assertEqual(rep["acceptance"]["receipts"], [])
        human = self.run_tool("-AcceptanceFor", CARD, table=False)
        self.assertEqual(human.returncode, 2, human.stdout)
        self.assertIn("NO_ACCEPTANCE_EVIDENCE", human.stdout)
        self.assertIn("VENUE_TABLE_ABSENT", human.stdout)
        self.assertNotIn(forged["receiptId"], human.stdout)

    def test_absent_venue_table_refuses_even_a_genuine_acceptance_venue_receipt(self) -> None:
        # No table means no authority for ANY role; the bachelor PASS is also not accepted on its own say-so.
        b = make_receipt(venue=BACH, outcome="PASS", role="acceptance")
        self.put(b)
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD, table=False)
        self.assertEqual(proc.returncode, 2, proc.stdout)
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["acceptance"]["receipts"], [])
        self.assertNotIn(b["receiptId"], proc.stdout)

    def test_a_table_path_that_is_a_directory_is_absent_not_trusted(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        folder = self.tmp / "a-directory"
        folder.mkdir()
        cmd = [PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
               "-ReceiptsRoot", str(self.root), "-VenueTable", str(folder), "-AcceptanceFor", CARD, "-Json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["acceptance"]["reason"], "VENUE_TABLE_ABSENT")

    def test_report_mode_without_a_table_still_shows_recorded_roles_marked_unverified(self) -> None:
        b = make_receipt(venue=BACH, outcome="PASS", role="acceptance")
        u = make_receipt(venue=UM, outcome="PASS", role="supplementary")
        self.put(b)
        self.put(u)
        rep = self.report(table=False)
        g = self.group(rep)
        self.assertFalse(rep["venueTable"]["present"])
        self.assertEqual(g["venues"][BACH]["latest"]["role"], "acceptance")
        self.assertFalse(g["venues"][BACH]["latest"]["roleVerified"])
        human = self.run_tool(table=False)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("UNVERIFIED", human.stdout)

    def test_report_mode_with_a_table_marks_roles_verified(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        g = self.group(self.report())
        self.assertTrue(g["venues"][BACH]["latest"]["roleVerified"])
        self.assertNotIn("UNVERIFIED", self.run_tool().stdout)

    def test_acceptance_receipts_are_always_role_verified(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        rep = json.loads(self.run_tool("-Json", "-AcceptanceFor", CARD).stdout)
        self.assertTrue(all(r["roleVerified"] for r in rep["acceptance"]["receipts"]))

    def test_table_present_but_invalid_is_exit_1_for_acceptance(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS"))
        self.table.write_text("[]", encoding="utf-8")
        self.assertEqual(self.run_tool("-AcceptanceFor", CARD).returncode, 1)

    def test_pass_on_a_venue_that_was_not_the_detected_host_is_refused(self) -> None:
        # P6: declared bachelor, but the run-time detection said ultra-magnus.
        self.put(make_receipt(venue=BACH, outcome="PASS", detected=UM))
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD)
        self.assertEqual(proc.returncode, 2)
        rep = json.loads(proc.stdout)
        self.assertEqual([x["reason"] for x in rep["refused"]], ["VENUE_DETECTION_MISMATCH"])

    def test_host_mismatch_terminal_may_disagree_because_it_carries_no_signal(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="VENUE_HOST_MISMATCH", detected=UM))
        rep = self.report()
        self.assertEqual(rep["counts"]["refused"], 0)
        g = self.group(rep)
        self.assertEqual(g["venues"][BACH]["latest"]["outcome"], "VENUE_HOST_MISMATCH")


@requires_pwsh
class ClipLengthTests(_Base):
    """Owner rule 2026-09-30: playback evidence needs a clip >= 20 s played without a loop wrap."""

    def _acceptance(self, *extra: str) -> tuple[subprocess.CompletedProcess, dict]:
        proc = self.run_tool("-Json", "-AcceptanceFor", CARD, *extra)
        return proc, json.loads(proc.stdout)

    def test_a_wrapped_playback_receipt_is_invalid_and_never_counted(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", wrapped=1))
        proc, rep = self._acceptance()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(rep["acceptance"]["receipts"], [])
        self.assertEqual([x["reason"] for x in rep["refused"]], ["INVALID_LOOPED"])
        self.assertIn("INVALID_LOOPED", self.run_tool("-AcceptanceFor", CARD).stdout)

    def test_a_boolean_wrapped_true_is_invalid_too(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="FAIL", wrapped=True))
        proc, rep = self._acceptance()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual([x["reason"] for x in rep["refused"]], ["INVALID_LOOPED"])

    def test_a_clip_under_twenty_seconds_is_invalid_and_never_counted(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", clip_seconds=16 / 24))
        proc, rep = self._acceptance()
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertEqual(rep["acceptance"]["receipts"], [])
        self.assertEqual([x["reason"] for x in rep["refused"]], ["INVALID_CLIP_TOO_SHORT"])

    def test_the_floor_is_twenty_seconds_inclusive(self) -> None:
        self.put(make_receipt(venue=BACH, leg="leg-a", outcome="PASS", clip_seconds=20))
        self.put(make_receipt(venue=BACH, leg="leg-b", outcome="PASS", clip_seconds=19.999))
        proc, rep = self._acceptance()
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual([r["legId"] for r in rep["acceptance"]["receipts"]], ["leg-a"])
        self.assertEqual([x["legId"] for x in rep["refused"]], ["leg-b"])

    def test_missing_clip_fields_are_unverified_and_excluded_from_acceptance(self) -> None:
        for kwargs in ({"clip_seconds": None}, {"wrapped": None}, {"clip_seconds": None, "wrapped": None},
                       {"clip_seconds": "25"}, {"wrapped": "0"}, {"wrapped": 2}):
            with self.subTest(**kwargs):
                shutil.rmtree(self.root)
                self.root.mkdir()
                self.put(make_receipt(venue=BACH, outcome="PASS", **kwargs))
                proc, rep = self._acceptance()
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertEqual(rep["acceptance"]["receipts"], [])
                self.assertEqual(rep["acceptance"]["withheld"][0]["reason"], "UNVERIFIED_CLIP_LENGTH")
                self.assertIn("UNVERIFIED_CLIP_LENGTH", self.run_tool("-AcceptanceFor", CARD).stdout)

    def test_snake_case_app_summary_names_are_read_too(self) -> None:
        self.put(make_receipt(venue=BACH, leg="leg-ok", outcome="PASS", clip_keys=("clip_seconds", "wrapped")))
        self.put(make_receipt(venue=BACH, leg="leg-bad", outcome="PASS", clip_seconds=3, clip_keys=("clip_seconds", "wrapped")))
        proc, rep = self._acceptance()
        self.assertEqual([r["legId"] for r in rep["acceptance"]["receipts"]], ["leg-ok"])
        self.assertEqual([x["reason"] for x in rep["refused"]], ["INVALID_CLIP_TOO_SHORT"])

    def test_the_receipt_view_shows_the_clip_length_it_was_admitted_on(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", clip_seconds=31.5))
        _, rep = self._acceptance()
        view = rep["acceptance"]["receipts"][0]
        self.assertEqual(view["clipSeconds"], 31.5)
        self.assertEqual(view["wrapped"], 0)
        self.assertEqual(view["clipLength"], "OK")

    def test_an_invalid_receipt_does_not_count_in_the_pair_report_either(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", wrapped=1))
        self.put(make_receipt(venue=UM, outcome="PASS"))
        rep = self.report()
        g = self.group(rep)
        self.assertEqual(g["pair"]["state"], "INCOMPLETE")
        self.assertIn({"venue": BACH, "reason": "missing"}, g["pair"]["reasons"])
        self.assertEqual([x["reason"] for x in rep["refused"]], ["INVALID_LOOPED"])

    def test_an_unverified_clip_length_is_flagged_in_the_pair_report(self) -> None:
        self.put(make_receipt(venue=BACH, outcome="PASS", clip_seconds=None))
        rep = self.report()
        self.assertEqual(self.group(rep)["venues"][BACH]["latest"]["clipLength"], "UNVERIFIED_CLIP_LENGTH")
        self.assertIn("UNVERIFIED_CLIP_LENGTH", self.run_tool().stdout)

    def test_a_non_signal_receipt_needs_no_clip_fields(self) -> None:
        # UNRESOLVED / VENUE_UNHEALTHY never ran a playback window, so they are not refused for lacking one.
        self.put(make_receipt(venue=BACH, outcome="UNRESOLVED", clip_seconds=None, wrapped=None))
        rep = self.report()
        self.assertEqual(rep["counts"]["refused"], 0)
        self.assertEqual(rep["counts"]["valid"], 1)

    def test_a_wrapped_run_in_the_acceptance_venue_does_not_hide_behind_an_older_pass(self) -> None:
        # The invalid receipt is refused (never counted), so the older genuine PASS is what remains -- and the
        # refusal is listed so the reader sees a looped run happened.
        self.put(make_receipt(venue=BACH, outcome="PASS", finished="2026-09-30T08:00:00Z"))
        self.put(make_receipt(venue=BACH, outcome="FAIL", finished="2026-09-30T09:00:00Z", wrapped=1))
        proc, rep = self._acceptance()
        self.assertEqual([r["outcome"] for r in rep["acceptance"]["receipts"]], ["PASS"])
        self.assertEqual([x["reason"] for x in rep["refused"]], ["INVALID_LOOPED"])


if __name__ == "__main__":
    unittest.main()
