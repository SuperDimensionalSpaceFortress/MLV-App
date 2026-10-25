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
