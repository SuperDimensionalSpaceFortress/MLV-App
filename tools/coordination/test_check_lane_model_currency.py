import datetime
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "check-lane-model-currency.py"

_spec = importlib.util.spec_from_file_location("check_lane_model_currency", SCRIPT)
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

NOW = datetime.datetime(2026, 10, 12, 20, 0, 0, tzinfo=datetime.timezone.utc)


def _ts(hours_ago):
    moment = NOW - datetime.timedelta(hours=hours_ago)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"


class CurrencyFixture(unittest.TestCase):
    def setUp(self):
        self.runs = Path(tempfile.mkdtemp(prefix="lane-currency-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.runs, ignore_errors=True))
        self.count = 0

    def receipt(self, lane, model, hours_ago, effort="high", state="complete"):
        self.count += 1
        run = self.runs / ("run-%03d" % self.count)
        run.mkdir()
        body = {"lane": lane, "state": state, "effort": effort, "startedUtc": _ts(hours_ago),
                "resolvedModel": model, "card": "TEST"}
        if effort is None:
            del body["effort"]
        (run / ("%s-001.receipt.json" % lane)).write_text(json.dumps(body), encoding="utf-8")

    def evaluate(self, **kwargs):
        receipts, _ = check.load_receipts(str(self.runs))
        return check.evaluate(receipts, now=NOW, **kwargs)


class ParseModelIdTests(unittest.TestCase):
    def test_claude_ids_compare_numerically_by_family(self):
        self.assertEqual(check.parse_model_id("claude-opus-5-5"), ("claude-opus", (5, 5)))
        self.assertEqual(check.parse_model_id("claude-opus-5"), ("claude-opus", (5,)))
        self.assertLess(check.parse_model_id("claude-opus-5")[1], check.parse_model_id("claude-opus-5-5")[1])
        self.assertLess(check.parse_model_id("claude-opus-5-9")[1], check.parse_model_id("claude-opus-5-10")[1])

    def test_a_trailing_date_stamp_is_not_part_of_the_version(self):
        self.assertEqual(check.parse_model_id("claude-sonnet-5-5-20261001"), ("claude-sonnet", (5, 5)))

    def test_codex_slugs_use_the_tier_as_family_and_dotted_versions(self):
        self.assertEqual(check.parse_model_id("gpt-6.1-sol"), ("sol", (6, 1)))
        self.assertLess(check.parse_model_id("gpt-6-sol")[1], check.parse_model_id("gpt-6.1-sol")[1])
        self.assertLess(check.parse_model_id("gpt-5.6-sol")[1], check.parse_model_id("gpt-6-sol")[1])

    def test_unknown_null_and_garbage_are_not_models(self):
        for value in ("unknown", None, "", 7, "opus", "gpt-sol", "claude-opus"):
            self.assertIsNone(check.parse_model_id(value), value)


class StaleModelTests(CurrencyFixture):
    def test_the_lane_s_own_latest_two_runs_decide_not_the_family_peer(self):
        self.receipt("sol", "gpt-6-sol", 30)
        self.receipt("sol", "gpt-6-sol", 20)
        self.receipt("luna", "gpt-6-luna", 5)
        self.receipt("sol", "gpt-6.1-sol", 1)
        # the sol lane's last two runs are (gpt-6-sol, gpt-6.1-sol): one is older, not twice in a row
        self.assertEqual(self.evaluate()["lanes"]["sol"]["verdict"], "CURRENT")

    def test_older_id_twice_in_a_row_while_the_family_has_a_newer_id_elsewhere(self):
        self.receipt("sol", "gpt-6-sol", 30)
        self.receipt("sol", "gpt-6-sol", 20)
        self.receipt("astra-sol-peer", "gpt-6.1-sol", 10)    # same family 'sol', different lane name
        result = self.evaluate()
        entry = result["lanes"]["sol"]
        self.assertEqual(entry["verdict"], "STALE")
        self.assertEqual(entry["reasons"][0]["kind"], "STALE-MODEL")
        self.assertEqual(entry["reasons"][0]["newest"], "gpt-6.1-sol")
        self.assertEqual(entry["reasons"][0]["ranModels"], ["gpt-6-sol", "gpt-6-sol"])
        self.assertEqual(result["verdict"], "STALE")

    def test_one_stale_run_between_current_runs_is_not_stale(self):
        self.receipt("opus", "claude-opus-5-5", 30)
        self.receipt("opus", "claude-opus-5", 20)
        self.receipt("opus", "claude-opus-5-5", 10)
        self.assertEqual(self.evaluate()["lanes"]["opus"]["verdict"], "CURRENT")

    def test_a_lane_that_recovered_after_two_stale_runs_is_current(self):
        self.receipt("sonnet", "claude-sonnet-5", 40)
        self.receipt("sonnet", "claude-sonnet-5", 30)
        self.receipt("sonnet", "claude-sonnet-5-5", 20)
        self.receipt("sonnet", "claude-sonnet-5-5", 10)
        self.assertEqual(self.evaluate()["lanes"]["sonnet"]["verdict"], "CURRENT")

    def test_stale_means_the_two_most_recent_runs_in_time_order_not_file_order(self):
        self.receipt("sonnet", "claude-sonnet-5-5", 1)      # written first, but the newest run
        self.receipt("sonnet", "claude-sonnet-5", 30)
        self.receipt("sonnet", "claude-sonnet-5", 20)
        self.assertEqual(self.evaluate()["lanes"]["sonnet"]["verdict"], "CURRENT")

    def test_families_are_compared_separately(self):
        # luna's newest is 6; sol's newest is 6.1. A luna lane on gpt-6-luna is current even though
        # a bigger sol version exists, and claude families never leak into each other.
        for hours in (30, 20):
            self.receipt("luna", "gpt-6-luna", hours)
            self.receipt("fable", "claude-fable-5-1", hours)
        self.receipt("sol", "gpt-6.1-sol", 5)
        self.receipt("opus", "claude-opus-5-5", 5)
        result = self.evaluate()
        self.assertEqual({lane: e["verdict"] for lane, e in result["lanes"].items()},
                         {"luna": "CURRENT", "fable": "CURRENT", "sol": "CURRENT", "opus": "CURRENT"})
        self.assertEqual(result["familyNewest"], {"claude-fable": "claude-fable-5-1", "claude-opus": "claude-opus-5-5",
                                                  "luna": "gpt-6-luna", "sol": "gpt-6.1-sol"})

    def test_unproven_runs_never_count_as_stale_or_as_current_evidence(self):
        self.receipt("opus", "claude-opus-5", 30)
        self.receipt("opus", "claude-opus-5", 20)
        self.receipt("opus", "unknown", 10, state="failed")
        self.receipt("opus", None, 5, state="running")
        self.receipt("opus", "claude-opus-5-5", 2)         # (another run proves the family moved on)
        # the two most recent PROVEN runs are (claude-opus-5, claude-opus-5-5): not twice in a row
        self.assertEqual(self.evaluate()["lanes"]["opus"]["verdict"], "CURRENT")

    def test_stale_when_the_only_newer_evidence_is_a_caller_supplied_newest(self):
        self.receipt("opus", "claude-opus-5-5", 30)
        self.receipt("opus", "claude-opus-5-5", 20)
        self.assertEqual(self.evaluate()["lanes"]["opus"]["verdict"], "CURRENT")
        result = self.evaluate(overrides={"claude-opus": "claude-opus-5-6"})
        self.assertEqual(result["lanes"]["opus"]["verdict"], "STALE")
        self.assertEqual(result["lanes"]["opus"]["reasons"][0]["newest"], "claude-opus-5-6")

    def test_a_single_run_can_never_be_stale(self):
        self.receipt("sol", "gpt-6.1-sol", 10)
        self.receipt("peer", "gpt-7-sol", 5)
        self.assertEqual(self.evaluate()["lanes"]["sol"]["verdict"], "CURRENT")


class EffortTests(CurrencyFixture):
    def test_any_effort_but_high_is_flagged(self):
        self.receipt("sol", "gpt-6.1-sol", 10, effort="xhigh")
        self.receipt("sol", "gpt-6.1-sol", 5)
        entry = self.evaluate()["lanes"]["sol"]
        self.assertEqual(entry["verdict"], "STALE")
        bad = [r for r in entry["reasons"] if r["kind"] == "BAD-EFFORT"][0]
        self.assertEqual(bad["efforts"], ["xhigh"])
        self.assertEqual(bad["count"], 1)

    def test_low_and_max_are_flagged_alike(self):
        self.receipt("luna", "gpt-6-luna", 10, effort="low")
        self.receipt("luna", "gpt-6-luna", 5, effort="max")
        bad = self.evaluate()["lanes"]["luna"]["reasons"][0]
        self.assertEqual((bad["kind"], bad["efforts"], bad["count"]), ("BAD-EFFORT", ["low", "max"], 2))

    def test_all_high_is_current(self):
        self.receipt("fable", "claude-fable-5-1", 10)
        self.receipt("fable", "claude-fable-5-1", 5)
        self.assertEqual(self.evaluate()["verdict"], "CURRENT")

    def test_an_unrecorded_effort_is_counted_but_not_a_violation(self):
        self.receipt("sonnet", "claude-sonnet-5-5", 10, effort=None)
        entry = self.evaluate()["lanes"]["sonnet"]
        self.assertEqual(entry["verdict"], "CURRENT")
        self.assertEqual(entry["unrecordedEffort"], 1)

    def test_the_effort_check_applies_to_runs_with_no_proven_model_too(self):
        self.receipt("sol", None, 5, effort="low", state="failed")
        entry = self.evaluate()["lanes"]["sol"]
        self.assertEqual(entry["verdict"], "STALE")
        self.assertEqual(entry["reasons"][0]["kind"], "BAD-EFFORT")


class WindowTests(CurrencyFixture):
    def test_old_receipts_outside_the_window_are_not_reported(self):
        self.receipt("astra", "gpt-6-astra", 500, effort="xhigh")
        self.receipt("sol", "gpt-6.1-sol", 2)
        result = self.evaluate(since_hours=72)
        self.assertNotIn("astra", result["lanes"])
        self.assertEqual(result["receiptsRead"], 2)
        self.assertEqual(result["receiptsInWindow"], 1)

    def test_zero_window_reads_every_receipt(self):
        self.receipt("astra", "gpt-6-astra", 500, effort="xhigh")
        result = self.evaluate(since_hours=0)
        self.assertEqual(result["lanes"]["astra"]["verdict"], "STALE")

    def test_the_family_newest_is_judged_over_all_receipts_even_outside_the_window(self):
        self.receipt("sol", "gpt-6.1-sol", 500)
        self.receipt("sol", "gpt-6-sol", 20)
        self.receipt("sol", "gpt-6-sol", 10)
        self.assertEqual(self.evaluate(since_hours=72)["lanes"]["sol"]["verdict"], "STALE")


class LoadAndCliTests(CurrencyFixture):
    def test_unreadable_and_foreign_receipts_are_counted_not_fatal(self):
        self.receipt("sol", "gpt-6.1-sol", 5)
        bad = self.runs / "run-bad"
        bad.mkdir()
        (bad / "sol-001.receipt.json").write_text("{not json", encoding="utf-8")
        nolane = self.runs / "run-nolane"
        nolane.mkdir()
        (nolane / "x-001.receipt.json").write_text(json.dumps({"startedUtc": _ts(1)}), encoding="utf-8")
        receipts, unreadable = check.load_receipts(str(self.runs))
        self.assertEqual((len(receipts), unreadable), (1, 2))

    def test_only_the_first_receipt_of_each_lane_run_is_read(self):
        self.receipt("sol", "gpt-6.1-sol", 5)
        run = next(self.runs.iterdir())
        (run / "sol-002.receipt.json").write_text(json.dumps(
            {"lane": "sol", "startedUtc": _ts(1), "effort": "low", "resolvedModel": "gpt-6-sol"}), encoding="utf-8")
        receipts, _ = check.load_receipts(str(self.runs))
        self.assertEqual(len(receipts), 1)

    def _run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--runs-dir", str(self.runs), *args],
                              capture_output=True, text=True, timeout=30)

    def test_cli_exit_zero_when_current_and_json_is_one_object(self):
        self.receipt("fable", "claude-fable-5-1", 1)
        result = self._run("--json", "--since-hours", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["verdict"], "CURRENT")
        self.assertEqual(payload["lanes"]["fable"]["latestModel"], "claude-fable-5-1")

    def test_cli_exit_one_when_stale(self):
        self.receipt("luna", "gpt-6-luna", 1, effort="low")
        result = self._run("--since-hours", "0")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("BAD-EFFORT", result.stdout)

    def test_cli_exit_two_for_a_missing_runs_dir_with_a_typed_error(self):
        result = subprocess.run([sys.executable, str(SCRIPT), "--runs-dir", str(self.runs / "nope")],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertTrue(json.loads(result.stdout)["error"].startswith("runs-dir-not-found:"))

    def test_cli_newest_override_must_name_its_own_family(self):
        self.receipt("opus", "claude-opus-5-5", 1)
        result = self._run("--newest", "claude-opus=gpt-6-sol")
        self.assertEqual(result.returncode, 2)
        self.assertTrue(json.loads(result.stdout)["error"].startswith("bad-newest:"))

    def test_cli_newest_override_marks_a_lane_stale(self):
        self.receipt("opus", "claude-opus-5-5", 3)
        self.receipt("opus", "claude-opus-5-5", 2)
        result = self._run("--newest", "claude-opus=claude-opus-5-6", "--since-hours", "0")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("STALE-MODEL", result.stdout)


if __name__ == "__main__":
    unittest.main()
