"""LANE-HOOK-DENY-RECURRENCE-1: tools/coordination/lane_deny_census.py puts each PARTIAL lane round in one deny class."""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("lane_deny_census", ROOT / "tools" / "coordination" / "lane_deny_census.py")
census_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(census_tool)

ONE_LINE_PER_CLASS = [
    ("NA-2 redirect", "PARTIAL hook deny NA-2 on a shell redirect into RUN -> successor r2"),
    ("NA-2 shrink", "PARTIAL hook deny NA-2 shrinking overwrite of summary.md -> successor r2"),
    ("NA-2 delete", "PARTIAL hook deny NA-2 on Remove-Item of RUN scratch -> successor r2"),
    ("NA-7 outside-root", "PARTIAL hook deny NA-7 (write to C:\\mlvtmp\\red-tree) -> successor r2"),
    ("NA-7 lane-scratch", "PARTIAL hook deny NA-7 on a scratchpad write -> successor r2"),
    ("STDIN-REPL", "PARTIAL hook denied python -c on stdin -> successor r2"),
    ("NA-6", "PARTIAL hook deny NA-6 assert count fell -> successor r2"),
    ("other-deny", "PARTIAL hook deny NA-4 on gh pr ready -> successor r2"),
    ("no-deny", "PARTIAL venue unreachable -> successor r2"),
    ("placeholder", "PARTIAL in-progress (lane started; this line is replaced at the end)"),
]


def test_every_class_has_a_case():
    assert sorted(c for c, _ in ONE_LINE_PER_CLASS) == sorted(census_tool.CLASSES)


@pytest.mark.parametrize("want,line", ONE_LINE_PER_CLASS, ids=[c for c, _ in ONE_LINE_PER_CLASS])
def test_classify_one_line_per_class(want, line):
    assert census_tool.classify([line]) == want


def test_a_bare_hook_deny_line_one_is_decided_by_the_body_and_scratch_wins_over_outside_root():
    hook_line = "Hook: NA-7: write outside both the worktree and the board root (c:/mlvtmp/x)"
    assert census_tool.classify(["PARTIAL hook deny", "unrelated prose", hook_line]) == "NA-7 outside-root"
    assert census_tool.classify(["PARTIAL hook deny", hook_line, "NA-7 again: it was the harness scratchpad"]) == "NA-7 lane-scratch"


def test_census_reads_only_partial_lane_rounds_on_or_after_since(tmp_path):
    def summary(name, text):
        d = tmp_path / name
        d.mkdir()
        (d / "summary.md").write_text(text, encoding="utf-8")

    summary("lane-A-r1-20261012T0100Z", "PARTIAL hook deny NA-6 assert count fell\n")
    summary("lane-B-r1-20261013T0100Z", "PARTIAL venue unreachable\n")
    summary("lane-C-r1-20261013T0200Z", "DONE abc PR 1\n")
    summary("lane-D-r1-20261011T0100Z", "PARTIAL hook deny NA-6 assert count fell\n")   # before --since
    summary("review-E-r1-20261013T0100Z", "PARTIAL hook deny NA-6 assert count fell\n")  # not a lane dir
    rows, table = census_tool.census(str(tmp_path), "2026-10-12")
    assert rows == [("2026-10-12", "lane-A-r1-20261012T0100Z", "NA-6"), ("2026-10-13", "lane-B-r1-20261013T0100Z", "no-deny")]
    assert table["2026-10-12"]["NA-6"] == 1 and table["2026-10-13"]["no-deny"] == 1
    assert census_tool.render(table).splitlines()[-1].endswith("| 2 |")
    assert census_tool.main(["--root", str(tmp_path / "missing")]) == 2
