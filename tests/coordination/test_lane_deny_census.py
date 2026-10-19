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


def test_the_successor_arrow_is_never_redirect_evidence():
    # r3 sol B2: the bare '>' in the redirect pattern read '-> successor r2' as a redirect.
    lines = ["PARTIAL hook deny (NA-2) -> successor r2", "NA-2: deleting RUN/logs/err.txt is denied"]
    assert census_tool.classify(lines) == "NA-2 delete"
    assert census_tool.classify(["PARTIAL hook deny NA-2 -> successor r2"]) == "other-deny"
    assert census_tool.classify(["PARTIAL hook deny", "NA-2: `type x > RUN/summary.md` is denied"]) == "NA-2 redirect"


def test_a_terminal_reason_that_is_not_a_deny_beats_an_earlier_incidental_deny_in_the_body():
    # r3 sol B3: line 1 names an unrelated terminal reason; the body mentions a deny that was recovered from.
    lines = ["PARTIAL work pushed; CI pending at poll limit",
             "Earlier in the round NA-2 denied `echo x > RUN/logs/a.txt` (redirect); I re-wrote it with the Write tool."]
    assert census_tool.classify(lines) == "no-deny"
    assert census_tool.classify(["PARTIAL work pushed; CI pending at poll limit -> successor r2"]) == "no-deny"


def test_only_the_successor_arrow_ends_the_line_one_reason():
    # An arrow inside the reason ("RED -> GREEN") must not hide a deny named after it; a deny named only in the
    # successor card's name ("-> successor LANE-HOOK-DENY-...") must not make a non-deny reason a deny.
    red_green = "PARTIAL fix proven (RED 20/20 -> GREEN 0/20) but NOT deployed: the Edit hook (NA-2) denied a harness edit"
    assert census_tool.classify([red_green + " -> successor r2"]) == "other-deny"
    assert census_tool.classify([red_green, "NA-2 shrinking overwrite denied"]) == "NA-2 shrink"
    assert census_tool.classify(["PARTIAL CI pending at the poll limit -> successor LANE-HOOK-DENY-RETRY-1"]) == "no-deny"


def test_a_line_one_that_names_an_NA_rule_is_refined_by_the_body_lines_naming_that_rule():
    # PLAYBACK-SCOPES-OPTION-1 r2: the stopping deny was NA-2 (shrinking overwrite); an earlier, recovered
    # python-stdin deny is also in the body and comes first in class order, but it is not the stop.
    lines = ["PARTIAL stopped on a hook deny (NA-2, report-script edit) at 22:25Z -> successor r3",
             "- mlv-never-authorized.py NA-2 \"shrinking overwrite\" denied one Edit of the report script.",
             "- Disclosure: earlier a PreToolUse hook blocked a `python - <<EOF` edit; I used the Edit tool instead."]
    assert census_tool.classify(lines) == "NA-2 shrink"
    # SAFETY-LINT-MASTER-PARITY-DIFF-1 r1: line 1 names NA-10 (no sub-class); body prose that mentions NA-7 and a
    # temp path on the same line is not the stop.
    na10 = ["PARTIAL hook NA-10 denied a read-only diagnostic Bash command (redirect read as a write)",
            "Hook reply: \"NA-10: a shell write edits this lane's OWN gate\"",
            "The master's hook is loaded from a temp path and derives a root from its own location (NA-10 / NA-7)."]
    assert census_tool.classify(na10) == "other-deny"
    assert census_tool.classify(["PARTIAL PreToolUse hook NA-11 denies any Edit of tools/agent-bridge/** -> successor r2",
                                 "all three Edit calls were denied by hook NA-11; RED->GREEN (forced) and a delete of x"]) == "other-deny"
    # No NA token on line 1: every body evidence line is still in play, class order decides.
    assert census_tool.classify(["PARTIAL hook deny"] + lines[1:]) == "STDIN-REPL"


def test_body_evidence_still_decides_an_unattributed_or_deny_naming_line_one():
    body = ["NA-6: the assert count fell, hook denied it"]
    assert census_tool.classify(["PARTIAL hook deny"] + body) == "NA-6"
    assert census_tool.classify(["PARTIAL"] + body) == "NA-6"
    assert census_tool.classify(["PARTIAL -> successor r2"] + body) == "NA-6"
    assert census_tool.classify(["PARTIAL hook denied a tool call"] + body) == "NA-6"


def test_the_two_lane_scratch_tests_agree_that_a_bare_TEMP_is_scratch():
    # r3 H1: \bTEMP\b was in the NA-7 lane-scratch rule but not in the second-pass _SCRATCH test.
    assert census_tool.classify(["PARTIAL hook deny NA-7 -> successor r2", "NA-7: it was TEMP"]) == "NA-7 lane-scratch"
    assert census_tool.classify(["PARTIAL hook deny", "Hook: NA-7: write outside both the worktree and the board root (TEMP)"]) == "NA-7 lane-scratch"
    assert census_tool.classify(["PARTIAL hook deny NA-7 on a temp-file write"]) == "NA-7 lane-scratch"
    assert census_tool.classify(["PARTIAL hook deny NA-7 (write to C:\\mlvtmp\\red-tree)"]) == "NA-7 outside-root"


def test_unstamped_run_dirs_and_unreadable_summaries_are_counted_not_dropped(tmp_path, monkeypatch, capsys):
    def summary(name, text="PARTIAL hook deny NA-6 assert count fell\n"):
        d = tmp_path / name
        d.mkdir()
        (d / "summary.md").write_text(text, encoding="utf-8")

    summary("lane-A-r1-20261013T0100Z")
    summary("lane-X-r1-20261013T0100Z-retry")        # no trailing stamp
    summary("lane-Y-r1")                              # no stamp at all
    summary("lane-LOCKED-r1-20261013T0200Z")
    real_open = open

    def flaky_open(path, *a, **k):
        if "LOCKED" in str(path):
            raise PermissionError(13, "locked", str(path))
        return real_open(path, *a, **k)

    monkeypatch.setattr(census_tool, "open", flaky_open, raising=False)
    stats = {}
    rows, table = census_tool.census(str(tmp_path), "2026-10-12", stats)
    assert rows == [("2026-10-13", "lane-A-r1-20261013T0100Z", "NA-6")]
    assert sorted(stats["unstamped"]) == ["lane-X-r1-20261013T0100Z-retry", "lane-Y-r1"]
    assert stats["unreadable"] == ["lane-LOCKED-r1-20261013T0200Z"]
    assert census_tool.main(["--root", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "unstamped run dirs skipped: 2" in out and "unreadable summary.md: 1" in out


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
