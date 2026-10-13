import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "Select-ReviewKeyB.ps1"
PWSH = shutil.which("pwsh") or "pwsh"


class SelectReviewKeyBTests(unittest.TestCase):
    def setUp(self):
        self.runs = Path(tempfile.mkdtemp(prefix="review-key-b-"))
        self.addCleanup(lambda: shutil.rmtree(self.runs, ignore_errors=True))
        self.n = 0

    def receipt(self, card, lane, started, allow_edits=True, run_name=None, **extra):
        self.n += 1
        run = self.runs / (run_name or ("lane-%s-r1-%02d" % (card, self.n)))
        run.mkdir(parents=True, exist_ok=True)
        body = {"lane": lane, "card": card, "allowEdits": allow_edits, "startedUtc": started, "state": "complete"}
        body.update(extra)
        (run / ("%s-001.receipt.json" % lane)).write_text(json.dumps(body), encoding="utf-8")
        return run / ("%s-001.receipt.json" % lane)

    def select(self, card="CARD-1", *args, expect=0):
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-Card", card,
                                 "-RunsDir", str(self.runs), "-Json", *args],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, expect, (result.stdout, result.stderr))
        return json.loads(result.stdout) if expect == 0 else result

    def test_a_sonnet_implemented_card_gets_opus_as_key_b(self):
        self.receipt("CARD-1", "sonnet", "2026-10-12T10:00:00.0000000Z")
        s = self.select()
        self.assertEqual((s["keyA"], s["keyB"], s["producerLane"], s["producerKnown"]), ("sol", "opus", "sonnet", True))
        self.assertEqual(s["warnings"], [])

    def test_an_opus_implemented_card_falls_back_to_fable_no_self_review(self):
        self.receipt("CARD-1", "opus", "2026-10-12T10:00:00.0000000Z")
        s = self.select()
        self.assertEqual((s["keyB"], s["producerLane"]), ("fable", "opus"))
        self.assertIn("no self-review", s["reason"])

    def test_the_newest_producer_receipt_decides_by_start_time_not_by_directory_order(self):
        self.receipt("CARD-1", "sonnet", "2026-10-12T12:00:00.0000000Z", run_name="lane-CARD-1-r1-a")
        self.receipt("CARD-1", "opus", "2026-10-12T09:00:00.0000000Z", run_name="lane-CARD-1-r2-z")
        self.assertEqual(self.select()["keyB"], "opus")     # sonnet is newer, so opus is a fine key B
        self.receipt("CARD-1", "opus", "2026-10-12T15:00:00.0000000Z", run_name="lane-CARD-1-r3-b")
        self.assertEqual(self.select()["keyB"], "fable")    # opus is now the newest producer

    def test_review_key_receipts_never_name_the_producer(self):
        self.receipt("CARD-1", "sonnet", "2026-10-12T10:00:00.0000000Z")
        # a later fable and sol review round: read-only (allowEdits false), in review-pr run dirs
        self.receipt("CARD-1", "fable", "2026-10-12T11:00:00.0000000Z", allow_edits=False, run_name="review-pr9-fable-r1-CARD-1")
        self.receipt("CARD-1", "sol", "2026-10-12T11:05:00.0000000Z", allow_edits=False, run_name="review-pr9-sol-r1-CARD-1")
        s = self.select()
        self.assertEqual((s["keyB"], s["producerLane"]), ("opus", "sonnet"))

    def test_another_card_with_a_longer_name_is_not_this_card(self):
        self.receipt("CARD-1", "sonnet", "2026-10-12T10:00:00.0000000Z")
        self.receipt("CARD-1-EXTRA", "opus", "2026-10-12T14:00:00.0000000Z")
        s = self.select()
        self.assertEqual((s["keyB"], s["producerLane"]), ("opus", "sonnet"))

    def test_a_card_with_no_producer_receipt_fails_toward_fable_and_says_it_was_not_evidence_backed(self):
        # HUB RULING HUB-TICK 20261013T0321Z: an unknown producer may have been opus, so opus is not a
        # safe default; key B is independent of the producer only if it is fable.
        self.receipt("OTHER-CARD", "opus", "2026-10-12T10:00:00.0000000Z")
        s = self.select()
        self.assertEqual((s["keyB"], s["producerLane"], s["producerKnown"], s["reason"]), ("fable", None, False, "no-producer-receipt"))
        self.assertEqual(len(s["warnings"]), 1)
        self.assertIn("no producer receipt", s["warnings"][0])
        self.assertIn("fable", s["warnings"][0])

    def test_a_sol_producer_warns_that_key_a_would_review_itself(self):
        self.receipt("CARD-1", "sol", "2026-10-12T10:00:00.0000000Z")
        s = self.select()
        self.assertEqual((s["keyA"], s["keyB"]), ("sol", "opus"))
        self.assertTrue(any("key A would review its own work" in w for w in s["warnings"]), s["warnings"])

    def test_unreadable_and_lane_less_receipts_are_skipped(self):
        self.receipt("CARD-1", "sonnet", "2026-10-12T10:00:00.0000000Z")
        bad = self.runs / "lane-CARD-1-r9-bad"
        bad.mkdir()
        (bad / "opus-001.receipt.json").write_text("{not json", encoding="utf-8")
        (bad / "x-001.receipt.json").write_text(json.dumps({"card": "CARD-1", "allowEdits": True, "startedUtc": "2026-10-12T20:00:00Z"}), encoding="utf-8")
        self.assertEqual(self.select()["producerLane"], "sonnet")

    def test_the_real_receipt_timestamp_shape_with_seven_fraction_digits_parses(self):
        self.receipt("CARD-1", "opus", "2026-10-12T19:16:41.6987971Z")
        self.assertEqual(self.select()["keyB"], "fable")

    def test_an_explicit_producer_lane_skips_the_lookup(self):
        s = self.select("NO-SUCH-CARD", "-ProducerLane", "opus")
        self.assertEqual((s["keyB"], s["reason"].split(";")[0]), ("fable", "producer-given"))
        self.assertEqual(self.select("NO-SUCH-CARD", "-ProducerLane", "sonnet")["keyB"], "opus")

    def test_an_unknown_producer_lane_or_runs_dir_is_a_usage_error(self):
        result = self.select("CARD-1", "-ProducerLane", "gpt", expect=2)
        self.assertIn("unknown -ProducerLane", result.stderr)
        missing = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-Card", "CARD-1",
                                  "-RunsDir", str(self.runs / "nope")], capture_output=True, text=True, timeout=120)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("RunsDir not found", missing.stderr)

    def test_plain_output_is_just_the_lane_name(self):
        self.receipt("CARD-1", "sonnet", "2026-10-12T10:00:00.0000000Z")
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), "-Card", "CARD-1",
                                 "-RunsDir", str(self.runs)], capture_output=True, text=True, timeout=120)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "opus"))

    def test_the_hub_call_shape_from_inside_a_script_that_owns_a_card_variable(self):
        # new-review-keys.ps1 has its own $Card; the selector must be called as a child process so
        # it can never overwrite that variable (a dot-sourced param block would).
        self.receipt("CARD-1", "opus", "2026-10-12T10:00:00.0000000Z")
        code = ("$Card = 'CARD-1'; $keyB = (& '%s' -Card $Card -RunsDir '%s' -Json | ConvertFrom-Json).keyB; "
                "\"$Card/$keyB\"") % (SCRIPT, self.runs)
        result = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-Command", code], capture_output=True, text=True, timeout=120)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "CARD-1/fable"), result.stderr)

    def test_the_script_is_ascii_only(self):
        SCRIPT.read_bytes().decode("ascii")


if __name__ == "__main__":
    unittest.main()
