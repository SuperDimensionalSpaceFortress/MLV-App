"""RED/GREEN proof for the two stuck-work doctrine-card checks in this folder."""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import action_list_lost_update_check as lost_update  # noqa: E402
import queue_starvation_check as starvation  # noqa: E402


def test_starvation_naive_drain_is_red():
    assert starvation.main(["--impl", "naive"]) == 1


def test_starvation_aged_drain_is_green():
    assert starvation.main(["--impl", "aged"]) == 0


def test_starvation_drain_cmd_is_the_same_contract():
    cmd = f'"{sys.executable}" -c "import json,sys; print(json.dumps({{\'drained\':[],\'starved\':[],\'bumped\':{{}}}}))"'
    assert starvation.main(["--drain-cmd", cmd]) == 1


def test_starvation_broken_drain_cmd_is_a_harness_error_not_green():
    assert starvation.main(["--drain-cmd", f'"{sys.executable}" -c "raise SystemExit(3)"']) == 2


def test_lost_update_naive_writers_are_red():
    assert lost_update.main(["--impl", "naive", "--trials", "10"]) == 1


def test_lost_update_locked_writers_are_green():
    assert lost_update.main(["--impl", "locked", "--trials", "10"]) == 0
