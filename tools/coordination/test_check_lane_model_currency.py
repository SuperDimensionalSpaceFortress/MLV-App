import datetime
import importlib.util
import json
import os
import shutil
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

    def receipt(self, lane, model, hours_ago, effort="high", state="complete", timeout=None):
        self.count += 1
        run = self.runs / ("run-%03d" % self.count)
        run.mkdir()
        body = {"lane": lane, "state": state, "effort": effort, "startedUtc": _ts(hours_ago),
                "resolvedModel": model, "card": "TEST"}
        if effort is None:
            del body["effort"]
        if timeout is not None:
            body["timeoutSec"] = timeout
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

    def test_an_unrecorded_effort_is_counted_and_is_not_proof_of_high(self):
        self.receipt("sonnet", "claude-sonnet-5-5", 10, effort=None)
        entry = self.evaluate()["lanes"]["sonnet"]
        self.assertEqual(entry["verdict"], "INCOMPLETE")
        self.assertEqual(entry["reasons"], [])         # not a violation, just unproven
        self.assertEqual(entry["unrecordedEffort"], 1)

    def test_the_effort_check_applies_to_runs_with_no_proven_model_too(self):
        self.receipt("sol", None, 5, effort="low", state="failed")
        entry = self.evaluate()["lanes"]["sol"]
        self.assertEqual(entry["verdict"], "STALE")
        self.assertEqual(entry["reasons"][0]["kind"], "BAD-EFFORT")


class MissingEvidenceTests(CurrencyFixture):
    """Missing evidence is never CURRENT (R3 blocker B1: three sol repros on ad3f1b72)."""

    def test_a_current_model_with_no_recorded_effort_is_incomplete_not_current(self):
        self.receipt("sol", "gpt-6.1-sol", 5, effort=None)
        result = self.evaluate()
        self.assertEqual(result["lanes"]["sol"]["verdict"], "INCOMPLETE")
        self.assertEqual(result["verdict"], "INCOMPLETE")

    def test_a_proven_current_run_followed_by_an_unresolved_newest_run_is_no_data(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.receipt("opus", None, 2, state="failed")
        result = self.evaluate()
        self.assertEqual(result["lanes"]["opus"]["verdict"], "NO-DATA")
        self.assertEqual(result["verdict"], "INCOMPLETE")

    def test_a_lone_unresolved_receipt_cannot_leave_the_overall_verdict_current(self):
        self.receipt("fable", None, 3, state="failed")
        result = self.evaluate()
        self.assertEqual(result["lanes"]["fable"]["verdict"], "NO-DATA")
        self.assertNotEqual(result["verdict"], "CURRENT")

    def test_one_unproven_lane_taints_the_overall_verdict_beside_a_current_lane(self):
        self.receipt("sol", "gpt-6.1-sol", 5)
        self.receipt("luna", "gpt-6-luna", 5, effort=None)
        result = self.evaluate()
        self.assertEqual(result["lanes"]["sol"]["verdict"], "CURRENT")
        self.assertEqual(result["verdict"], "INCOMPLETE")

    def test_stale_still_outranks_incomplete(self):
        self.receipt("sol", "gpt-6.1-sol", 5, effort="low")
        self.receipt("luna", "gpt-6-luna", 5, effort=None)
        self.assertEqual(self.evaluate()["verdict"], "STALE")

    def test_an_empty_window_is_no_data_not_current(self):
        self.receipt("sol", "gpt-6.1-sol", 500)
        self.assertEqual(self.evaluate(since_hours=72)["verdict"], "NO-DATA")

    def test_the_table_shows_unrecorded_effort_so_no_lane_prints_a_bare_current(self):
        self.receipt("sonnet", "claude-sonnet-5-5", 5, effort=None)
        self.receipt("fable", "claude-fable-5-1", 5)
        lines = check.render(self.evaluate()).splitlines()
        sonnet = next(line for line in lines if line.strip().startswith("sonnet"))
        fable = next(line for line in lines if line.strip().startswith("fable"))
        self.assertIn("INCOMPLETE", sonnet)
        self.assertIn("unrecordedEffort=1", sonnet)
        self.assertNotIn("unrecordedEffort", fable)
        self.assertIn("INCOMPLETE", lines[0])


class InFlightTests(CurrencyFixture):
    """CURRENCY-CHECK-WIRED-INTO-HUB-TICK-1: a lane whose newest receipt is a LIVE running run (no
    resolvedModel and no effort yet, by design) is IN-FLIGHT, judged on its newest finished run. A dead
    running receipt (past startedUtc + timeoutSec + grace) is not in flight and stays unproven."""

    def running(self, lane, hours_ago, timeout=7200):
        self.receipt(lane, None, hours_ago, effort=None, state="running", timeout=timeout)

    def test_running_newest_over_a_current_finished_run_is_in_flight_and_names_the_run(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.running("opus", 1)
        result = self.evaluate()
        entry = result["lanes"]["opus"]
        self.assertEqual(entry["verdict"], "IN-FLIGHT")
        self.assertEqual(entry["inFlightRuns"], 1)
        self.assertEqual(entry["judgedOn"]["model"], "claude-opus-5-5")
        self.assertEqual(entry["unrecordedEffort"], 0)       # the running receipt is not evidence either way
        self.assertEqual(result["verdict"], "CURRENT")        # IN-FLIGHT alone never taints the overall verdict
        self.assertEqual(result["inFlightLanes"], ["opus"])

    def test_running_newest_over_a_stale_pair_keeps_stale(self):
        self.receipt("sol", "gpt-6-sol", 30)
        self.receipt("sol", "gpt-6-sol", 20)
        self.receipt("peer", "gpt-6.1-sol", 10)
        self.running("sol", 1)
        result = self.evaluate()
        self.assertEqual(result["lanes"]["sol"]["verdict"], "STALE")
        self.assertEqual(result["verdict"], "STALE")

    def test_running_newest_over_an_incomplete_finished_run_keeps_incomplete(self):
        self.receipt("sonnet", "claude-sonnet-5-5", 10, effort=None)
        self.running("sonnet", 1)
        self.assertEqual(self.evaluate()["lanes"]["sonnet"]["verdict"], "INCOMPLETE")

    def test_running_with_no_finished_run_stays_no_data(self):
        self.running("opus", 1)
        result = self.evaluate()
        self.assertEqual(result["lanes"]["opus"]["verdict"], "NO-DATA")
        self.assertEqual(result["verdict"], "INCOMPLETE")
        self.assertIsNone(result["lanes"]["opus"]["judgedOn"])

    def test_a_running_receipt_past_its_timeout_is_dead_not_in_flight(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.running("opus", 5, timeout=3600)                 # bound = 1 h + grace; 5 h old
        result = self.evaluate()
        entry = result["lanes"]["opus"]
        self.assertEqual(entry["verdict"], "NO-DATA")
        self.assertEqual(entry["inFlightRuns"], 0)
        self.assertEqual(entry["deadRunning"], 1)
        self.assertEqual(result["verdict"], "INCOMPLETE")

    # --- r2 (PR 753 fix round): the grace and the cap are pinned at their edges ---------------------------
    def _running_at(self, started, timeout, **extra):
        """A running receipt written as a real file with an exact startedUtc (a datetime)."""
        self.count += 1
        run = self.runs / ("raw-run-%03d" % self.count)
        run.mkdir()
        body = {"lane": "opus", "state": "running", "card": "TEST", "timeoutSec": timeout,
                "startedUtc": started.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"}
        body.update(extra)
        (run / "opus-001.receipt.json").write_text(json.dumps(body), encoding="utf-8")

    def _opus_verdict_with_running_started(self, started, timeout, since_hours=72):
        self.receipt("opus", "claude-opus-5-5", 40)           # a CURRENT finished run well before any case
        self._running_at(started, timeout)
        return self.evaluate(since_hours=since_hours)["lanes"]["opus"]

    def test_the_liveness_bound_is_timeout_plus_grace_before_at_and_after(self):
        grace = datetime.timedelta(seconds=300)               # literal on purpose: not read back from the script
        timeout = 3600
        edge = NOW - datetime.timedelta(seconds=timeout) - grace    # the instant the bound is exactly reached
        for label, started, expected in (
                ("at the timeout, no grace used", NOW - datetime.timedelta(seconds=timeout), "IN-FLIGHT"),
                ("1 s inside the grace", edge + datetime.timedelta(seconds=1), "IN-FLIGHT"),
                ("exactly at timeout + grace", edge, "IN-FLIGHT"),
                ("1 s past timeout + grace", edge - datetime.timedelta(seconds=1), "NO-DATA"),
                ("1 microsecond past timeout + grace", edge - datetime.timedelta(microseconds=1), "NO-DATA")):
            with self.subTest(label):
                self.setUp()
                entry = self._opus_verdict_with_running_started(started, timeout)
                self.assertEqual(entry["verdict"], expected)
                self.assertEqual(entry["inFlightRuns"], 1 if expected == "IN-FLIGHT" else 0)
                self.assertEqual(entry["deadRunning"], 0 if expected == "IN-FLIGHT" else 1)

    def test_the_grace_is_three_hundred_seconds_and_removing_it_is_caught(self):
        self.assertEqual(check.LIVE_GRACE_SEC, 300.0)
        # between the timeout and timeout + grace the receipt is live only because of the grace
        started = NOW - datetime.timedelta(seconds=3600 + 100)
        self.assertEqual(self._opus_verdict_with_running_started(started, 3600)["verdict"], "IN-FLIGHT")

    def test_the_timeout_cap_is_pinned_at_its_edge_independent_of_the_age_bound(self):
        started = NOW - datetime.timedelta(hours=1)           # young: only the cap can make it dead
        for label, timeout, expected in (
                ("cap itself", check.MAX_LIVE_TIMEOUT_SEC, "IN-FLIGHT"),
                ("cap - 1", check.MAX_LIVE_TIMEOUT_SEC - 1, "IN-FLIGHT"),
                ("cap + 1", check.MAX_LIVE_TIMEOUT_SEC + 1, "NO-DATA"),
                ("zero", 0, "NO-DATA"),
                ("one second", 1, "NO-DATA")):                # 1 s timeout, 1 h old: past timeout + grace
            with self.subTest(label):
                self.setUp()
                self.assertEqual(self._opus_verdict_with_running_started(started, timeout)["verdict"], expected)

    # --- r2 B1: a receipt dated in the future is not live ---------------------------------------------
    def test_b1_the_hub_counterexample_through_real_receipt_files(self):
        # hub repro-753-b1.py: completed sol run 19:00Z on gpt-6.1-sol/high, then a running receipt that
        # starts 31 days later with timeout 3600. now = 2026-10-12T20:00Z.
        for since in (72, 0):
            with self.subTest(since_hours=since):
                self.setUp()
                self.receipt("sol", "gpt-6.1-sol", 1)
                self.count += 1
                run = self.runs / "future"
                run.mkdir()
                started = NOW + datetime.timedelta(days=31)
                (run / "sol-001.receipt.json").write_text(json.dumps({
                    "lane": "sol", "state": "running", "card": "TEST", "timeoutSec": 3600,
                    "startedUtc": started.strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"}), encoding="utf-8")
                receipts, _ = check.load_receipts(str(self.runs))
                future = next(r for r in receipts if r["state"] == "running")
                self.assertFalse(check.is_live_running(future, NOW))
                result = check.evaluate(receipts, since_hours=since, now=NOW)
                entry = result["lanes"]["sol"]
                if since == 72:       # the window reaches back from now, so the future receipt is in it
                    self.assertEqual(entry["runsInWindow"], 2)
                self.assertEqual(entry["verdict"], "NO-DATA")      # unproven, as before this PR
                self.assertEqual(entry["inFlightRuns"], 0)
                self.assertEqual(entry["deadRunning"], 1)
                self.assertEqual(result["inFlightLanes"], [])
                self.assertEqual(result["verdict"], "INCOMPLETE")

    def test_b1_the_future_start_skew_bound_is_pinned_inside_at_and_past(self):
        self.assertEqual(check.MAX_FUTURE_START_SKEW_SEC, 300)
        skew = datetime.timedelta(seconds=check.MAX_FUTURE_START_SKEW_SEC)
        for since in (72, 0):
            for label, started, expected in (
                    ("1 s inside the bound", NOW + skew - datetime.timedelta(seconds=1), "IN-FLIGHT"),
                    ("exactly at the bound", NOW + skew, "IN-FLIGHT"),
                    ("1 microsecond past the bound", NOW + skew + datetime.timedelta(microseconds=1), "NO-DATA"),
                    ("1 s past the bound", NOW + skew + datetime.timedelta(seconds=1), "NO-DATA"),
                    ("an hour ahead", NOW + datetime.timedelta(hours=1), "NO-DATA")):
                with self.subTest(label=label, since_hours=since):
                    self.setUp()
                    entry = self._opus_verdict_with_running_started(started, 3600, since_hours=since)
                    self.assertEqual(entry["verdict"], expected)
                    self.assertEqual(entry["inFlightRuns"], 1 if expected == "IN-FLIGHT" else 0)

    def test_b1_a_future_dated_receipt_is_not_live_through_the_cli_with_the_real_clock(self):
        real_now = datetime.datetime.now(datetime.timezone.utc)
        self._write_raw("fin", lane="sol", state="complete", resolvedModel="gpt-6.1-sol", effort="high",
                        startedUtc=(real_now - datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z")
        self._write_raw("fut", lane="sol", state="running", timeoutSec=3600,
                        startedUtc=(real_now + datetime.timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z")
        for flags in ((), ("--since-hours", "0")):
            with self.subTest(flags=flags):
                result = self._cli("--json", *flags)
                self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["lanes"]["sol"]["verdict"], "NO-DATA")
                self.assertEqual(payload["inFlightLanes"], [])

    def test_a_running_receipt_with_no_timeout_cannot_be_proven_live(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.receipt("opus", None, 1, effort=None, state="running")
        self.assertEqual(self.evaluate()["lanes"]["opus"]["verdict"], "NO-DATA")

    def test_a_running_receipt_that_already_has_an_end_time_is_not_live(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.running("opus", 1)
        path = next(p for p in self.runs.glob("*/opus-001.receipt.json") if "running" in p.read_text())
        body = json.loads(path.read_text())
        body["endedUtc"] = _ts(0.5)
        path.write_text(json.dumps(body), encoding="utf-8")
        self.assertEqual(self.evaluate()["lanes"]["opus"]["verdict"], "NO-DATA")

    def test_a_failed_newest_run_still_reads_no_data(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.receipt("opus", None, 2, state="failed")
        self.assertEqual(self.evaluate()["lanes"]["opus"]["verdict"], "NO-DATA")

    def test_a_failed_run_before_a_live_running_one_is_not_papered_over(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.receipt("opus", None, 3, state="failed")
        self.running("opus", 1)
        self.assertEqual(self.evaluate()["lanes"]["opus"]["verdict"], "NO-DATA")

    def test_a_live_running_run_older_than_a_finished_one_is_ignored_as_evidence(self):
        self.running("opus", 3, timeout=14400)                # still live (3 h of 4 h), but not the newest receipt
        self.receipt("opus", "claude-opus-5-5", 1)
        entry = self.evaluate()["lanes"]["opus"]
        self.assertEqual(entry["verdict"], "CURRENT")
        self.assertEqual(entry["unrecordedEffort"], 0)

    def test_in_flight_beside_a_no_data_lane_leaves_the_overall_incomplete(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.running("opus", 1)
        self.receipt("fable", None, 2, state="failed")
        self.assertEqual(self.evaluate()["verdict"], "INCOMPLETE")

    def test_the_table_shows_in_flight_and_the_run_it_was_judged_on(self):
        self.receipt("opus", "claude-opus-5-5", 10)
        self.running("opus", 1)
        line = next(l for l in check.render(self.evaluate()).splitlines() if l.strip().startswith("opus"))
        self.assertIn("IN-FLIGHT", line)
        self.assertIn("judged-on=claude-opus-5-5", line)

    # --- adversary pass 1 (sol, head 70403d1c): A1-A4 -------------------------------------------------
    def _write_raw(self, name, **fields):
        run = self.runs / name
        run.mkdir()
        (run / ("%s-001.receipt.json" % fields["lane"])).write_text(json.dumps(fields), encoding="utf-8")

    def _cli(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), "--runs-dir", str(self.runs), *args],
                              capture_output=True, text=True, timeout=30)

    def test_a1_an_absurd_timeout_cannot_crash_the_check_it_is_simply_not_provably_live(self):
        for index, bad in enumerate((1e20, float("inf"), float("nan"), 2147483647, -5, True)):
            self._write_raw("bad-%d" % index, lane="opus", state="running",
                            startedUtc="2026-11-04T11:00:00Z", timeoutSec=bad)
        result = self._cli("--json", "--since-hours", "0")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["lanes"]["opus"]["verdict"], "NO-DATA")
        self.assertEqual(payload["lanes"]["opus"]["inFlightRuns"], 0)

    def test_a1_the_liveness_bound_is_capped_so_a_huge_but_finite_timeout_is_dead(self):
        self.receipt("opus", "claude-opus-5-5", 100)
        self.running("opus", 90, timeout=check.MAX_LIVE_TIMEOUT_SEC + 1)
        self.assertEqual(self.evaluate(since_hours=0)["lanes"]["opus"]["verdict"], "NO-DATA")

    def test_a2_a_live_running_receipt_does_not_raise_the_family_newest_either(self):
        self.receipt("sol", "gpt-6-sol", 30)
        self.receipt("sol", "gpt-6-sol", 20)
        # a running receipt that already carries a newer id: not evidence, so it cannot make the two
        # finished runs STALE
        self.receipt("sol", "gpt-6.1-sol", 1, effort="high", state="running", timeout=7200)
        result = self.evaluate()
        self.assertEqual(result["lanes"]["sol"]["verdict"], "IN-FLIGHT")
        self.assertEqual(result["familyNewest"]["sol"], "gpt-6-sol")
        self.assertEqual(result["verdict"], "CURRENT")

    def test_a2_the_adversary_input_with_an_eligible_timeout_exits_zero_in_flight_through_the_cli(self):
        # The adversary's shape (two finished gpt-6-sol runs, then a running receipt that already names the
        # newer gpt-6.1-sol), with a timeout the liveness rule accepts, stamped against the real clock the
        # CLI uses. The over-cap timeout 2147483647 is the separate case in test_a1_an_absurd_timeout....
        real_now = datetime.datetime.now(datetime.timezone.utc)

        def stamp(hours_ago):
            return (real_now - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"
        self._write_raw("a", lane="sol", state="complete", startedUtc=stamp(30), resolvedModel="gpt-6-sol", effort="high")
        self._write_raw("b", lane="sol", state="complete", startedUtc=stamp(20), resolvedModel="gpt-6-sol", effort="high")
        self._write_raw("c", lane="sol", state="running", startedUtc=stamp(1), timeoutSec=7200,
                        resolvedModel="gpt-6.1-sol", effort="high")
        result = self._cli("--json", "--since-hours", "0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["lanes"]["sol"]["verdict"], "IN-FLIGHT")
        self.assertEqual(payload["familyNewest"]["sol"], "gpt-6-sol")     # the running receipt raised nothing
        self.assertEqual(payload["verdict"], "CURRENT")
        self.assertEqual(payload["inFlightLanes"], ["sol"])

    def test_n1_an_exact_tie_in_started_reads_the_running_receipt_as_the_newer_in_both_creation_orders(self):
        stamp = "2026-10-12T19:00:00.1234567Z"      # the same seven digits on both receipts: a true tie
        specs = {"fin": dict(state="complete", resolvedModel="claude-opus-5-5", effort="high"),
                 "run": dict(state="running", timeoutSec=7200)}
        for order in (("fin", "run"), ("run", "fin")):
            with self.subTest(order=order):
                self.setUp()
                for name in order:
                    self._write_raw(name, lane="opus", startedUtc=stamp, **specs[name])
                receipts, _ = check.load_receipts(str(self.runs))
                self.assertEqual(receipts[0]["order"], receipts[1]["order"])     # truly equal sort keys
                self.assertEqual(check.evaluate(receipts, now=NOW)["lanes"]["opus"]["verdict"], "IN-FLIGHT")

    def test_a3_equal_started_timestamps_after_microsecond_truncation_still_read_in_flight(self):
        for name, state, extra in (("fin", "complete", {"resolvedModel": "claude-opus-5-5", "effort": "high"}),
                                   ("run", "running", {"timeoutSec": 7200})):
            self._write_raw(name, lane="opus", state=state,
                            startedUtc=("2026-10-12T19:00:00.1234561Z" if name == "fin" else "2026-10-12T19:00:00.1234569Z"),
                            **extra)
        entry = self.evaluate()["lanes"]["opus"]
        self.assertEqual(entry["verdict"], "IN-FLIGHT")

    def test_a3_the_seventh_fractional_digit_orders_receipts_so_an_older_live_run_is_not_in_flight(self):
        # adversary pass 2 A1: the finished run is the NEWER one by 800 ns; the running one must not win the tie
        self._write_raw("fin", lane="opus", state="complete", startedUtc="2026-10-12T19:00:00.1234569Z",
                        resolvedModel="claude-opus-5-5", effort="high")
        self._write_raw("run", lane="opus", state="running", startedUtc="2026-10-12T19:00:00.1234561Z",
                        timeoutSec=7200)
        self.assertEqual(self.evaluate()["lanes"]["opus"]["verdict"], "CURRENT")

    def test_a4_a_huge_or_infinite_window_means_every_receipt_not_a_crash(self):
        self.receipt("fable", "claude-fable-5-1", 5)

        def strict(token):
            raise ValueError("non-JSON constant %s" % token)
        for value in ("1e20", "inf", "nan", "-inf"):
            result = self._cli("--json", "--since-hours=" + value)
            self.assertNotIn("Traceback", result.stderr, value)
            payload = json.loads(result.stdout, parse_constant=strict)     # strict: no Infinity / NaN
            self.assertTrue(payload["ok"], value)
            self.assertEqual(payload["receiptsInWindow"], 1, value)

    def test_cli_exit_zero_when_the_only_non_current_lane_is_in_flight(self):
        # The CLI judges against the real clock, so these receipts are stamped relative to it.
        real_now = datetime.datetime.now(datetime.timezone.utc)

        def write(name, lane, hours_ago, **fields):
            run = self.runs / name
            run.mkdir()
            started = (real_now - datetime.timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%S.%f") + "0Z"
            body = {"lane": lane, "startedUtc": started, "card": "TEST"}
            body.update(fields)
            (run / ("%s-001.receipt.json" % lane)).write_text(json.dumps(body), encoding="utf-8")

        write("a", "fable", 2, state="complete", effort="high", resolvedModel="claude-fable-5-1")
        write("b", "opus", 2, state="complete", effort="high", resolvedModel="claude-opus-5-5")
        write("c", "opus", 0.5, state="running", timeoutSec=7200)
        result = subprocess.run([sys.executable, str(SCRIPT), "--runs-dir", str(self.runs), "--since-hours", "0",
                                 "--json"], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["lanes"]["opus"]["verdict"], "IN-FLIGHT")
        self.assertEqual(payload["verdict"], "CURRENT")


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

    def test_cli_exit_three_when_the_evidence_is_incomplete(self):
        self.receipt("sol", "gpt-6.1-sol", 1, effort=None)
        result = self._run("--json", "--since-hours", "0")
        self.assertEqual(result.returncode, 3, result.stdout)
        self.assertEqual(json.loads(result.stdout)["verdict"], "INCOMPLETE")

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


REPO_ROOT = ROOT.parent.parent

# The pre-MODEL-POSTURE-R19-ADOPT-1 shapes (fork/master 13d65263): Astra named as THE model of the
# design-pass / arbiter row, and as the tie-breaker in the lane table comment.
OLD_TIERING_ROW = (
    "| judgement, cross-family | Codex Astra (`astra` tier row, `gpt-*-astra`) | high | one pre-implementation "
    "design pass per substantive packet; one arbiter seat when consequential verdicts conflict | anything routine |"
)
NEW_TIERING_ROW = (
    "| judgement, cross-family | Codex Sol tier (`sol` row). Astra (`astra` row) is an OPTIONAL diversity seat only "
    "(bus R19.1: never a default, never a gate, never the only Codex seat a procedure waits for; a round that uses "
    "it records `astra: diversity`) | high | one design pass; one arbiter seat | anything routine |"
)
OLD_LANES_COMMENT = (
    "#   luna   -- breadth recon.\n"
    "#   astra  -- tie-breaker / design arbiter on contested designs; never xhigh.\n"
    "$LANES = @{\n"
)
NEW_LANES_COMMENT = (
    "#   luna   -- breadth recon.\n"
    "#   astra  -- optional diversity seat (bus R19.1): never a default, never a\n"
    "#             gate, never the only Codex seat a procedure waits for; a round using it records\n"
    "#             `astra: diversity`. Design-pass and arbiter duties go to the sol tier. Never xhigh.\n"
    "$LANES = @{\n"
)


class AstraPostureTests(unittest.TestCase):
    """MODEL-POSTURE-R19-ADOPT-1 (bus R19.1 clause 3): Astra is an occasional diversity seat, never a
    default or a gate; a procedure that reserved a role for it re-points that role to the Sol tier."""

    def test_the_old_astra_as_the_design_arbiter_row_is_refused(self):
        problems = check.posture_violations(OLD_TIERING_ROW, NEW_LANES_COMMENT)
        self.assertTrue(any("orchestration-tiering" in p for p in problems), problems)

    def test_the_new_diversity_row_and_comment_pass(self):
        self.assertEqual(check.posture_violations(NEW_TIERING_ROW, NEW_LANES_COMMENT), [])

    def test_the_old_tie_breaker_comment_is_refused(self):
        problems = check.posture_violations(NEW_TIERING_ROW, OLD_LANES_COMMENT)
        self.assertTrue(any("Invoke-Lane" in p for p in problems), problems)

    def test_a_row_that_makes_astra_a_gate_is_refused_even_with_a_diversity_word(self):
        gate = NEW_TIERING_ROW.replace("never a gate", "a gate")
        self.assertTrue(check.posture_violations(gate, NEW_LANES_COMMENT))

    def test_a_row_that_names_astra_before_sol_is_refused(self):
        swapped = NEW_TIERING_ROW.replace("Codex Sol tier (`sol` row). Astra (`astra` row)",
                                          "Codex Astra (`astra` row). Sol tier (`sol` row)")
        self.assertTrue(check.posture_violations(swapped, NEW_LANES_COMMENT))

    def test_a_missing_astra_comment_block_is_refused_not_passed(self):
        problems = check.posture_violations(NEW_TIERING_ROW, "$LANES = @{\n}\n")
        self.assertTrue(any("Invoke-Lane" in p for p in problems), problems)

    def test_prose_mentions_of_astra_outside_table_rows_are_not_judged(self):
        prose = "astra joined Luna and Sol at high.\n" + NEW_TIERING_ROW
        self.assertEqual(check.posture_violations(prose, NEW_LANES_COMMENT), [])

    # KNOWN LIMITS (dot 718b5ce8 I1 on PR 675): the guard is a bounded text lint over marker substrings,
    # their order and one comment block. These two tests pin what it does NOT catch; if a later change
    # makes it catch them, flip the assertion rather than delete the test.
    def test_known_limit_a_row_keeping_every_marker_but_adding_a_required_duty_still_passes(self):
        contradictory = NEW_TIERING_ROW.replace(
            "| anything routine |",
            "| anything routine; Astra is required for every design pass and must sign off before merge |")
        self.assertIn("required", contradictory)
        self.assertIn("never a gate", contradictory)
        self.assertEqual(check.posture_violations(contradictory, NEW_LANES_COMMENT), [])

    def test_known_limit_a_duty_outside_the_checked_comment_block_still_passes(self):
        elsewhere = NEW_LANES_COMMENT + (
            "# Every packet must wait for the astra seat; astra is the required design arbiter.\n"
            "#   astra  -- required design arbiter (a second, unchecked comment block).\n")
        self.assertEqual(check.posture_violations(NEW_TIERING_ROW, elsewhere), [])

    def test_known_limit_a_contradictory_duty_in_a_row_that_does_not_name_astra_is_not_seen(self):
        other_row = "| design arbiter | the second Codex seat is required on every packet | high | x | y |"
        self.assertEqual(check.posture_violations(NEW_TIERING_ROW + "\n" + other_row, NEW_LANES_COMMENT), [])

    def test_the_lint_describes_itself_as_bounded_not_proof(self):
        self.assertIn("BOUNDED TEXT LINT", check.__doc__)
        self.assertIn("KNOWN LIMIT", check.__doc__)
        self.assertIn("not proof", check.posture_violations.__doc__)
        done = subprocess.run([sys.executable, str(SCRIPT), "--posture", "--repo-root", str(REPO_ROOT)],
                              capture_output=True, text=True, timeout=30)
        self.assertIn("bounded text lint", json.loads(done.stdout)["scope"])

    def test_the_astra_role_label_is_marked_legacy_where_it_is_defined_and_emitted(self):
        text = (REPO_ROOT / check.INVOKE_LANE_REL).read_text(encoding="utf-8-sig")
        self.assertIn("role = 'tie-breaker-design-arbiter'", text)  # label unchanged; guardrail test pins it
        self.assertEqual(text.count("LEGACY"), 2, "one note at the $LANES row, one at the receipt emit site")
        self.assertEqual(text.count("NOT current routing authority"), 1)
        self.assertEqual(text.count("must not take it as"), 1)

    def test_the_committed_files_satisfy_the_posture(self):
        problems, error = check.check_posture(str(REPO_ROOT))
        self.assertIsNone(error)
        self.assertEqual(problems, [])

    def test_cli_posture_exit_zero_on_the_committed_files_and_one_on_a_violation(self):
        done = subprocess.run([sys.executable, str(SCRIPT), "--posture", "--repo-root", str(REPO_ROOT)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stdout)
        root = Path(tempfile.mkdtemp(prefix="posture-root-"))
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "agents").mkdir()
        (root / "tools" / "coordination").mkdir(parents=True)
        (root / "agents" / "orchestration-tiering.md").write_text(OLD_TIERING_ROW + "\n", encoding="utf-8")
        (root / "tools" / "coordination" / "Invoke-Lane.ps1").write_text(OLD_LANES_COMMENT, encoding="utf-8")
        done = subprocess.run([sys.executable, str(SCRIPT), "--posture", "--repo-root", str(root)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 1, done.stdout)
        self.assertFalse(json.loads(done.stdout)["ok"])

    def test_cli_posture_exit_two_when_a_file_is_missing(self):
        root = Path(tempfile.mkdtemp(prefix="posture-empty-"))
        self.addCleanup(shutil.rmtree, str(root), True)
        done = subprocess.run([sys.executable, str(SCRIPT), "--posture", "--repo-root", str(root)],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 2, done.stdout)
        self.assertTrue(json.loads(done.stdout)["error"].startswith("posture-file-unreadable:"))


class DefaultRunsDirTests(unittest.TestCase):
    """default_runs_dir() must not depend on the caller's cwd: git prints a RELATIVE common dir
    from the main checkout, and abspath() of that resolved against the caller, not the script."""

    def setUp(self):
        self.addCleanup(os.chdir, os.getcwd())
        self.addCleanup(os.environ.pop, "MLV_FLEET_RUNS_DIR", None)
        os.environ.pop("MLV_FLEET_RUNS_DIR", None)
        self.foreign = os.path.realpath(tempfile.mkdtemp(prefix="lane-currency-cwd-"))
        self.addCleanup(shutil.rmtree, self.foreign, True)

    @staticmethod
    def _same(left, right):
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))

    def test_resolves_to_the_checkout_that_owns_the_script_from_a_foreign_cwd(self):
        common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                capture_output=True, text=True, timeout=15, cwd=str(ROOT)).stdout.strip()
        self.assertTrue(common, "git could not resolve the common dir from %s" % ROOT)
        expected = os.path.join(os.path.dirname(common), ".claude-state", "fleet-runs")
        os.chdir(self.foreign)
        self.assertTrue(self._same(check.default_runs_dir(), expected),
                        "%s != %s" % (check.default_runs_dir(), expected))

    def test_main_checkout_layout_where_git_prints_a_relative_common_dir(self):
        repo = os.path.join(self.foreign, "main-checkout")
        script_dir = os.path.join(repo, "tools", "coordination")
        os.makedirs(script_dir)
        subprocess.run(["git", "init", "-q", repo], check=True, capture_output=True, timeout=30)
        copy = os.path.join(script_dir, "check-lane-model-currency.py")
        shutil.copy(str(SCRIPT), copy)
        spec = importlib.util.spec_from_file_location("check_lane_model_currency_main_layout", copy)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        relative = subprocess.run(["git", "rev-parse", "--git-common-dir"], capture_output=True, text=True,
                                  timeout=15, cwd=script_dir).stdout.strip()
        self.assertFalse(os.path.isabs(relative), "layout does not reproduce a relative common dir: %r" % relative)
        elsewhere = os.path.join(self.foreign, "elsewhere")
        os.mkdir(elsewhere)
        os.chdir(elsewhere)
        self.assertTrue(self._same(module.default_runs_dir(), os.path.join(repo, ".claude-state", "fleet-runs")),
                        module.default_runs_dir())

    def test_env_override_still_wins(self):
        os.environ["MLV_FLEET_RUNS_DIR"] = self.foreign
        self.assertEqual(check.default_runs_dir(), self.foreign)


if __name__ == "__main__":
    unittest.main()
