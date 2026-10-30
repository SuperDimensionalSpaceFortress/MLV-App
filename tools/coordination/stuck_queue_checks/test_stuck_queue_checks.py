"""RED/GREEN proof for the two stuck-work doctrine-card checks in this folder."""

import subprocess
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


# An arming command that works and a beat writer that fails before it saves anything. The beat
# runs in a worker thread, so its exception never reaches main(); it must still be a harness error.
_WRITER_RAISES = '''
import json
from pathlib import Path


def arm(path, entry_id, after_write=None):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    rows.append({"id": entry_id, "status": "armed"})
    Path(path).write_text(json.dumps(rows), encoding="utf-8")
    return 0


def beat_save(path, snapshot, changed):
    raise RuntimeError("beat failed before saving")
'''


def test_lost_update_beat_writer_that_raises_is_a_harness_error_not_green(tmp_path, capsys):
    module = tmp_path / "writers_beat_raises.py"
    module.write_text(_WRITER_RAISES, encoding="utf-8")
    assert lost_update.main(["--impl-module", str(module), "--trials", "2"]) == 2
    out = capsys.readouterr().out
    assert "GREEN" not in out
    assert "beat_save raised" in out


# A beat writer that never returns. The check gives a writer 30 s, raises HarnessError and promises
# exit 2; a non-daemon worker thread kept the interpreter alive at shutdown, so the CLI hung instead.
_WRITER_NEVER_RETURNS = '''
import json
import threading
from pathlib import Path


def arm(path, entry_id, after_write=None):
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    rows.append({"id": entry_id, "status": "armed"})
    Path(path).write_text(json.dumps(rows), encoding="utf-8")
    return 0


def beat_save(path, snapshot, changed):
    threading.Event().wait()
'''


def test_lost_update_writer_that_never_returns_exits_2_instead_of_hanging(tmp_path):
    module = tmp_path / "writers_beat_hangs.py"
    module.write_text(_WRITER_NEVER_RETURNS, encoding="utf-8")
    cmd = [sys.executable, str(HERE / "action_list_lost_update_check.py"), "--impl-module", str(module), "--trials", "1"]
    # The writer budget is 30 s; 60 s leaves room for start-up and still fails fast on a hang.
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    assert done.returncode == 2
    assert "a writer did not finish within 30 s" in done.stdout
    assert "GREEN" not in done.stdout


def test_lost_update_arming_command_that_raises_is_a_harness_error_not_green(tmp_path, capsys):
    module = tmp_path / "writers_arm_raises.py"
    module.write_text(
        _WRITER_RAISES.replace("raise RuntimeError(\"beat failed before saving\")", "return None")
        .replace("    rows = json.loads", "    raise RuntimeError(\"arm failed\")\n    rows = json.loads"),
        encoding="utf-8",
    )
    assert lost_update.main(["--impl-module", str(module), "--trials", "2"]) == 2
    assert "GREEN" not in capsys.readouterr().out
