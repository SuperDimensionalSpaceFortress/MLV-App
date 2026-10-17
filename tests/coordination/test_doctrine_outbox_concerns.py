"""The project-to-CoS concern channel in the doctrine outbox (card COS-CONCERN-CHANNEL-1).

A concern is an entry in the project-owned `specs/mlv-app/cos-concerns.md`. The field set is closed, so
no field is an approval, a ruling or a permission; every entry expires within 14 days; the drain holds an
entry that is expired, repeats an id, or has no exact `CoS-CONCERN:` reader-duty line at the fetched bus tip. Small on
purpose: the 2,900-line sibling suite takes more than ten minutes.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("doctrine_outbox_concerns_under_test", REPO_ROOT / "tools" / "coordination" / "doctrine_outbox.py")
ob = importlib.util.module_from_spec(_spec)
sys.modules["doctrine_outbox_concerns_under_test"] = ob
_spec.loader.exec_module(ob)

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]
CONCERNS = "specs/mlv-app/cos-concerns.md"

ENTRY = ("### mlv-app-cc-20261016-9\n"
         "- id: mlv-app-cc-20261016-9\n"
         "- to: CoS\n"
         "- opened_utc: 2026-10-16T21:50:00Z\n"
         "- expires_utc: 2026-10-30T21:50:00Z\n"
         "- kind: process\n"
         "- evidence: bus commit abc1234, PR comment 6224672124\n"
         "- ask: Re-read the thread before each write.\n")


DUTY_LINE = "- The CoS reads `CoS-CONCERN:` PR comments and every `specs/*/cos-concerns.md` each pass.\n"
RATIFIED_README = "# cos-feedback\n\n## Writer duties\n\n" + DUTY_LINE
NOW = "2026-10-17T00:00:00Z"


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    """The drain holds an expired concern, so the clock is pinned: ENTRY must not rot on 2026-10-30."""
    monkeypatch.setattr(ob, "_utc_now_z", lambda: NOW)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def item_text(body: str = ENTRY, target: str = CONCERNS, kind: str = "concern") -> str:
    return f"---\ntarget: {target}\nkind: {kind}\nsource_commit: PENDING\nlaw4: attested\n---\n{body}"


def init_bus(tmp_path: Path, readme: str) -> Path:
    bare, clone = tmp_path / "bus-origin.git", tmp_path / "bus-clone"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(bare)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True, text=True)
    (clone / "cos-feedback").mkdir()
    (clone / "cos-feedback" / "README.md").write_text(readme, encoding="utf-8")
    git(clone, "add", "cos-feedback")
    git(clone, "commit", "-m", "seed bus")
    git(clone, "push", "origin", "HEAD:refs/heads/master")
    return clone


def init_source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    (src / "doctrine-outbox").mkdir(parents=True)
    (src / ob.TOOL_REL).parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "master", str(src)], check=True, capture_output=True, text=True)
    (src / ob.TOOL_REL).write_text("# stand-in for the tool at this ref\n", encoding="utf-8")
    (src / "doctrine-outbox" / "20261016-example-concern.md").write_text(item_text(), encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-m", "init")
    return src


def test_a_well_formed_concern_parses():
    item = ob.parse_item(item_text())
    assert item["meta"]["kind"] == "concern" and item["meta"]["target"] == CONCERNS
    assert ob.CONCERNS_TARGET == CONCERNS and CONCERNS in ob.TARGETS


@pytest.mark.parametrize("label,body,code", [
    ("an approval field", ENTRY + "- approved_by: nobody\n", "CONCERN_UNKNOWN_FIELD"),
    ("a permission field", ENTRY + "- permission: merge\n", "CONCERN_UNKNOWN_FIELD"),
    ("a ruling field", ENTRY + "- ruling: adopted\n", "CONCERN_UNKNOWN_FIELD"),
    ("a missing ask", ENTRY.replace("- ask: Re-read the thread before each write.\n", ""), "CONCERN_MISSING_FIELD"),
    ("a duplicate field", ENTRY + "- ask: a second ask\n", "CONCERN_DUPLICATE_FIELD"),
    ("a continuation line", ENTRY + "  more text on a second line\n", "CONCERN_BAD_LINE"),
    ("a bad heading id", ENTRY.replace("### mlv-app-cc-20261016-9", "### a concern"), "CONCERN_BAD_ID"),
    ("an id field that differs from the heading", ENTRY.replace("- id: mlv-app-cc-20261016-9", "- id: mlv-app-cc-20261016-8"), "CONCERN_ID_MISMATCH"),
    ("another project's id", ENTRY.replace("mlv-app-cc", "other-cc"), "CONCERN_BAD_ID"),
    ("an addressee other than the CoS", ENTRY.replace("- to: CoS", "- to: steward"), "CONCERN_BAD_ADDRESSEE"),
    ("an unknown kind", ENTRY.replace("- kind: process", "- kind: approval"), "CONCERN_BAD_KIND"),
    ("a malformed time", ENTRY.replace("2026-10-30T21:50:00Z", "next week"), "CONCERN_BAD_TIME"),
    ("an entry that never expires", ENTRY.replace("2026-10-30T21:50:00Z", "2026-10-16T21:50:00Z"), "CONCERN_NOT_EXPIRING"),
    ("a window of 14 days and one second", ENTRY.replace("2026-10-30T21:50:00Z", "2026-10-30T21:50:01Z"), "CONCERN_WINDOW_TOO_LONG"),
    ("an expiry in the year 2999", ENTRY.replace("2026-10-30T21:50:00Z", "2999-01-01T00:00:00Z"), "CONCERN_WINDOW_TOO_LONG"),
    ("a calendar-impossible time", ENTRY.replace("2026-10-30T21:50:00Z", "2026-13-40T21:50:00Z"), "CONCERN_BAD_TIME"),
    ("an id dated on another day", ENTRY.replace("2026-10-16T21:50:00Z", "2026-10-17T21:50:00Z").replace("2026-10-30T21:50:00Z", "2026-10-31T21:50:00Z"), "CONCERN_ID_DATE"),
])
def test_a_concern_with_a_hole_is_refused(label, body, code):
    with pytest.raises(ob.Refusal) as excinfo:
        ob.parse_item(item_text(body))
    assert excinfo.value.code == code, label


def test_a_concern_kind_and_its_target_go_together():
    for text in (item_text(kind="trap"), item_text(target="TRAPS.md")):
        with pytest.raises(ob.Refusal) as excinfo:
            ob.parse_item(text)
        assert excinfo.value.code == "ITEM_CONCERN_TARGET_MISMATCH"


def test_the_drain_holds_a_concern_until_the_bus_names_the_reader_duty(tmp_path):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, "# cos-feedback\n\nOnly the CoS writes here.\n")
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert [(r["path"], r["code"]) for r in report["refused"]] == [
        ("doctrine-outbox/20261016-example-concern.md", "CONCERN_CHANNEL_NOT_RATIFIED")]
    assert report["published"] == [] and "would_push" not in report


def test_the_drain_appends_a_ratified_concern_under_a_title_and_never_touches_cos_feedback(tmp_path):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, RATIFIED_README)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert report["refused"] == [] and [p["target"] for p in report["published"]] == [CONCERNS]
    text = git(clone, "show", f"{report['commit']}:{CONCERNS}")
    assert text.startswith("# mlv-app concerns to the CoS (written only by mlv-app; the CoS answers in cos-feedback/mlv-app/concerns.md)\n\n### mlv-app-cc-20261016-9\n")
    assert text.count("<!-- outbox:") == 1
    changed = git(clone, "diff", "--name-only", f"{report['commit']}~1", report["commit"]).splitlines()
    assert changed == [CONCERNS], "the drain writes the project-owned file and nothing else"
    again = ob.drain(src, clone, "HEAD", tmp_path / "sent-lost.jsonl", [], push=True)
    assert again["published"] == [] and [a["via"] for a in again["already_sent"]] == ["bus_tip"]


@pytest.mark.parametrize("label,readme", [
    ("a sentence that forbids the token", "# cos-feedback\n\nProjects must not use CoS-CONCERN: comments.\n"),
    ("the token with no duty line", "# cos-feedback\n\nThe CoS reads `CoS-CONCERN:` comments each pass.\n"),
    ("a duty line that names only R1", "# cos-feedback\n\n- The CoS reads `CoS-CONCERN:` PR comments each pass.\n"),
    ("the path with no duty line", "# cos-feedback\n\nDo not read `specs/*/cos-concerns.md`; `CoS-CONCERN:` is retired.\n"),
    ("the exact line inside a code fence", "# cos-feedback\n\n```\n" + DUTY_LINE + "```\n"),
])
def test_only_the_exact_duty_line_opens_the_gate(tmp_path, label, readme):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, readme)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert [r["code"] for r in report["refused"]] == ["CONCERN_CHANNEL_NOT_RATIFIED"], label
    assert report["published"] == []


DUTY_PREFIX = "The CoS reads `CoS-CONCERN:` "


@pytest.mark.parametrize("label,readme", [
    ("the prefix then a sentence that forbids R2",
     "# cos-feedback\n\n- " + DUTY_PREFIX + "PR comments only; reading `specs/*/cos-concerns.md` is forbidden.\n"),
    ("the prefix then a negated form",
     "# cos-feedback\n\n- " + DUTY_PREFIX + "PR comments and every `specs/*/cos-concerns.md` each pass is NOT the rule.\n"),
    ("the exact line with a trailing clause",
     "# cos-feedback\n\n" + DUTY_LINE.rstrip("\n") + " Except never.\n"),
    ("the exact line inside a tilde fence", "# cos-feedback\n\n~~~\n" + DUTY_LINE + "~~~\n"),
    ("the exact line inside a longer tilde fence", "# cos-feedback\n\n~~~~\n~~~\n" + DUTY_LINE + "~~~~\n"),
    ("the exact line inside a single-line HTML comment", "# cos-feedback\n\n<!-- " + DUTY_LINE.rstrip("\n") + " -->\n"),
    ("the exact line inside a multi-line HTML comment", "# cos-feedback\n\n<!--\n" + DUTY_LINE + "-->\n"),
    ("the exact line in a comment that opens after text", "# cos-feedback\n\nnote <!-- retired:\n" + DUTY_LINE + "still retired -->\n"),
    ("the exact line after an inline-code `<!--` that never closes (fails closed, CoS r3 N3)",
     "# cos-feedback\n\nUse `<!--` carefully.\n\n" + DUTY_LINE),
])
def test_a_line_that_starts_with_the_duty_prefix_but_is_not_the_duty_does_not_open_the_gate(tmp_path, label, readme):
    """CoS r2 review, I5: the r2 gate anchored only the prefix and then took `.*`, so each of these opened it."""
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, readme)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert [r["code"] for r in report["refused"]] == ["CONCERN_CHANNEL_NOT_RATIFIED"], label
    assert report["published"] == []


@pytest.mark.parametrize("label,readme", [
    ("the line after a closed HTML comment", "# cos-feedback\n\n<!-- a note -->\n" + DUTY_LINE),
    ("the line after a closed multi-line comment", "# cos-feedback\n\n<!--\nnote\n-->\n" + DUTY_LINE),
    ("the line after a closed backtick fence", "# cos-feedback\n\n```\ncode\n```\n" + DUTY_LINE),
    ("the line after a closed tilde fence", "# cos-feedback\n\n~~~\ncode\n~~~\n" + DUTY_LINE),
    ("a fence marker inside a comment is not a fence", "# cos-feedback\n\n<!--\n~~~\n-->\n" + DUTY_LINE),
    ("a comment marker inside a fence is not a comment", "# cos-feedback\n\n```\n<!--\n```\n" + DUTY_LINE),
    ("the line with surrounding whitespace", "# cos-feedback\n\n  " + DUTY_LINE.rstrip("\n") + "   \n"),
    ("the line with CRLF endings", "# cos-feedback\r\n\r\n" + DUTY_LINE.rstrip("\n") + "\r\n"),
])
def test_the_duty_line_still_counts_after_a_closed_fence_or_comment(tmp_path, label, readme):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, readme)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == [] and [w["target"] for w in report["would_push"]] == [CONCERNS], label


@pytest.mark.parametrize("label,readme", [
    ("the exact line at 4 spaces after a blank line", "# cos-feedback\n\npara\n\n    " + DUTY_LINE),
    ("the exact line at a tab after a blank line", "# cos-feedback\n\npara\n\n\t" + DUTY_LINE),
    ("the exact line at 8 spaces after a blank line", "# cos-feedback\n\npara\n\n        " + DUTY_LINE),
])
def test_the_duty_line_in_an_indented_code_block_does_not_open_the_gate(tmp_path, label, readme):
    """CoS r3 review, I6: the r3 anchor `^\\s*` took the line at 4 spaces or a tab, which CommonMark renders as an
    indented code block; the CoS writes the duty as a top-level bullet, so only 0 to 3 spaces count."""
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, readme)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert [r["code"] for r in report["refused"]] == ["CONCERN_CHANNEL_NOT_RATIFIED"], label
    assert report["published"] == []


@pytest.mark.parametrize("indent", ["", " ", "   "])
def test_the_duty_line_indented_up_to_three_spaces_still_opens_the_gate(tmp_path, indent):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, "# cos-feedback\n\npara\n\n" + indent + DUTY_LINE)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == [] and [w["target"] for w in report["would_push"]] == [CONCERNS]


@pytest.mark.parametrize("gap", [" ", "  ", "   ", "    ", "\t"])
def test_a_bullet_followed_by_one_to_four_spaces_or_a_tab_opens_the_gate(tmp_path, gap):
    """CoS r4 review, N5 (controls): CommonMark takes 1 to 4 spaces, or a tab, after a list marker as the marker gap."""
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, "# cos-feedback\n\n" + DUTY_LINE.replace("- ", "-" + gap, 1))
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == [] and [w["target"] for w in report["would_push"]] == [CONCERNS]


@pytest.mark.parametrize("marker", ["-", "*"])
def test_a_bullet_followed_by_five_spaces_is_an_indented_code_block_and_does_not_open_the_gate(tmp_path, marker):
    """CoS r4 review, N5: with 5 or more spaces after the marker the item content is an indented code block."""
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, "# cos-feedback\n\n" + DUTY_LINE.replace("- ", marker + "     ", 1))
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert [r["code"] for r in report["refused"]] == ["CONCERN_CHANNEL_NOT_RATIFIED"]
    assert report["published"] == []


@pytest.mark.parametrize("label,readme", [
    ("an indented code line ended by a vertical tab", "# cos-feedback\n\np\n\n    code\x0b" + DUTY_LINE),
    ("a form feed before the line", "# cos-feedback\n\np\n\n    code\x0c" + DUTY_LINE),
    ("a unicode line separator inside one paragraph line", "# cos-feedback\n\nx " + DUTY_LINE),
    ("a unicode paragraph separator inside one paragraph line", "# cos-feedback\n\nx " + DUTY_LINE),
    ("a next-line character inside one paragraph line", "# cos-feedback\n\nx\x85" + DUTY_LINE),
])
def test_only_newline_carriage_return_and_crlf_end_a_line_for_the_duty_scan(tmp_path, label, readme):
    """CoS r4 review, N6: str.splitlines() also breaks on \\v, \\f, \\x1c-\\x1e, \\x85, U+2028 and U+2029, which Markdown
    does not; the duty text there is part of a longer line, so it must not open the gate."""
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, readme)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert [r["code"] for r in report["refused"]] == ["CONCERN_CHANNEL_NOT_RATIFIED"], label
    assert report["published"] == []


@pytest.mark.parametrize("newline", ["\r\n", "\r"])
def test_the_duty_line_still_counts_when_lines_end_in_crlf_or_cr(tmp_path, newline):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, ("# cos-feedback\n\n" + DUTY_LINE).replace("\n", newline))
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == [] and [w["target"] for w in report["would_push"]] == [CONCERNS]


@pytest.mark.parametrize("readme", [DUTY_LINE, "# cos-feedback\n\n" + DUTY_LINE.lstrip("- "),"# cos-feedback\n\n" + DUTY_LINE.replace("- ", "* ", 1)])
def test_the_exact_duty_line_opens_the_gate_with_or_without_a_bullet(tmp_path, readme):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, readme)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == [] and [w["target"] for w in report["would_push"]] == [CONCERNS]


def add_item(src: Path, name: str, body: str) -> None:
    (src / "doctrine-outbox").mkdir(exist_ok=True)  # git rm of the last item removes the directory
    (src / "doctrine-outbox" / name).write_text(item_text(body), encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-m", f"add {name}")


def test_two_committed_items_with_one_concern_id_are_both_held(tmp_path):
    """CoS repro on 892922b7: two items headed mlv-app-cc-20261016-9 with different asks both published."""
    src = init_source(tmp_path)
    add_item(src, "20261016-second-concern.md", ENTRY.replace("Re-read the thread before each write.", "Answer each ask on its own thread."))
    clone = init_bus(tmp_path, RATIFIED_README)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert sorted((r["path"], r["code"]) for r in report["refused"]) == [
        ("doctrine-outbox/20261016-example-concern.md", "CONCERN_DUPLICATE_ID"),
        ("doctrine-outbox/20261016-second-concern.md", "CONCERN_DUPLICATE_ID")]
    assert report["published"] == [] and not report["pushed"]


def test_a_new_item_reusing_an_id_already_in_the_tip_concerns_file_is_refused(tmp_path):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, RATIFIED_README)
    first = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert first["refused"] == [] and len(first["published"]) == 1
    # Same id, different ask: a second file in the outbox. The first stays history; only the new one is refused.
    add_item(src, "20261016-reused-id.md", ENTRY.replace("Re-read the thread before each write.", "A different ask under the same id."))
    again = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert [(r["path"], r["code"]) for r in again["refused"]] == [("doctrine-outbox/20261016-reused-id.md", "CONCERN_DUPLICATE_ID")]
    assert again["published"] == [] and [a["via"] for a in again["already_sent"]] == ["ledger"]
    text = git(clone, "show", f"{first['commit']}:{CONCERNS}")
    assert text.count("### mlv-app-cc-20261016-9") == 1


def retire_first_item(src: Path) -> None:
    """Drop the already-published item from the outbox, so the new item meets the TIP's duplicate check and not
    the one for two committed items sharing an id."""
    git(src, "rm", "-q", "doctrine-outbox/20261016-example-concern.md")
    git(src, "commit", "-m", "retire the first concern item")


def test_the_tip_duplicate_refusal_names_the_fields_that_differ(tmp_path):
    """CoS r2 review, N2: the message said 'under different text' for any differing key; it now names them."""
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, RATIFIED_README)
    first = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert first["refused"] == []
    changed = (ENTRY.replace("Re-read the thread before each write.", "A different ask under the same id.")
               .replace("bus commit abc1234, PR comment 6224672124", "bus commit def5678"))
    retire_first_item(src)
    add_item(src, "20261016-reused-id.md", changed)
    again = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    [refusal] = again["refused"]
    assert refusal["code"] == "CONCERN_DUPLICATE_ID"
    assert "field(s) evidence, ask differ from that entry" in refusal["detail"], refusal["detail"]
    assert "different text" not in refusal["detail"]


def test_the_tip_duplicate_refusal_says_so_when_no_field_differs(tmp_path):
    """The same entry re-filed under another outbox key: no field differs, and the message must not claim one does."""
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, RATIFIED_README)
    first = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert first["refused"] == []
    # A later commit changes the item's outbox key (it hashes the source commit) but not one concern field.
    retire_first_item(src)
    add_item(src, "20261016-refiled.md", ENTRY)
    again = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    [refusal] = again["refused"]
    assert refusal["code"] == "CONCERN_DUPLICATE_ID"
    assert "no field differs" in refusal["detail"] and "differ from that entry" not in refusal["detail"], refusal["detail"]


def _tip_entry(clone: Path, mode: str, content: str, path: str = CONCERNS) -> None:
    """Land `path` on the bus tip as an entry of the given git mode (a 120000 entry is a symlink)."""
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "--detach", "origin/master")
    blob = subprocess.run(["git", "-C", str(clone), "hash-object", "-w", "--stdin"], input=content, check=True,
                          capture_output=True, text=True).stdout.strip()
    git(clone, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{path}")
    git(clone, "commit", "-m", f"tip concerns entry {mode}")
    git(clone, "push", "origin", "HEAD:refs/heads/master")


@pytest.mark.parametrize("mode", ["120000", "100755"])
def test_a_concerns_file_that_is_not_a_regular_blob_at_the_tip_is_a_per_item_refusal(tmp_path, mode):
    """Iverson advisory on 35dca646: a symlinked concerns file had no per-item refusal, so the drain aborted."""
    src = init_source(tmp_path)
    (src / "doctrine-outbox" / "20261016-b-receipt.md").write_text(
        item_text("### A receipt\nbody\n", target="RECEIPTS.md", kind="receipt"), encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-m", "add a receipt")
    clone = init_bus(tmp_path, RATIFIED_README)
    _tip_entry(clone, mode, "../../elsewhere.md" if mode == "120000" else ob.CONCERNS_TITLE)
    bare = tmp_path / "bus-origin.git"
    before_blob = git(bare, "rev-parse", f"master:{CONCERNS}")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [
        ("doctrine-outbox/20261016-example-concern.md", "CONCERNS_PATH_NOT_REGULAR_FILE")]
    assert mode in report["refused"][0]["detail"] and CONCERNS in report["refused"][0]["detail"]
    assert [p["target"] for p in report["published"]] == ["RECEIPTS.md"], "the receipt is not held by the refused concern"
    assert git(bare, "ls-tree", "master", "--", CONCERNS).startswith(f"{mode} "), "the concerns entry itself is untouched"
    assert git(bare, "rev-parse", f"master:{CONCERNS}") == before_blob, "nothing was written through the link"
    assert git(bare, "ls-tree", "-r", "master", "--name-only").splitlines().count("elsewhere.md") == 0


@pytest.mark.parametrize("link_path", ["specs", "specs/mlv-app"])
def test_a_link_on_a_parent_of_the_concerns_path_at_the_tip_is_a_per_item_refusal(tmp_path, link_path):
    """CoS r3 review, I7: with `specs` or `specs/mlv-app` a link at the tip, ls-tree on the leaf path returns nothing,
    so the leaf check passed and the write-time guard aborted the whole drain. Now only the concern is refused."""
    src = init_source(tmp_path)
    (src / "doctrine-outbox" / "20261016-b-receipt.md").write_text(
        item_text("### A receipt\nbody\n", target="RECEIPTS.md", kind="receipt"), encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-m", "add a receipt")
    clone = init_bus(tmp_path, RATIFIED_README)
    _tip_entry(clone, "120000", "../../elsewhere", path=link_path)
    bare = tmp_path / "bus-origin.git"

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [
        ("doctrine-outbox/20261016-example-concern.md", "CONCERNS_PATH_IS_LINK")]
    assert link_path in report["refused"][0]["detail"] and "120000" in report["refused"][0]["detail"]
    assert [p["target"] for p in report["published"]] == ["RECEIPTS.md"], "the receipt is not held by the refused concern"
    assert git(bare, "ls-tree", "master", "--", link_path).startswith("120000 "), "the link itself is untouched"


@pytest.mark.parametrize("plain_path", ["specs", "specs/mlv-app"])
def test_a_regular_file_on_a_parent_of_the_concerns_path_is_refused_as_not_a_directory(tmp_path, plain_path):
    """CoS r4 review, N7: a regular file where `specs` or `specs/mlv-app` should be is still a per-item refusal, but
    its detail says "not a directory: mode 100644" rather than describing a link."""
    src = init_source(tmp_path)
    (src / "doctrine-outbox" / "20261016-b-receipt.md").write_text(
        item_text("### A receipt\nbody\n", target="RECEIPTS.md", kind="receipt"), encoding="utf-8")
    git(src, "add", "-A")
    git(src, "commit", "-m", "add a receipt")
    clone = init_bus(tmp_path, RATIFIED_README)
    _tip_entry(clone, "100644", "just a file\n", path=plain_path)
    bare = tmp_path / "bus-origin.git"

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [
        ("doctrine-outbox/20261016-example-concern.md", "CONCERNS_PATH_IS_LINK")]
    assert f"{plain_path} at " in report["refused"][0]["detail"]
    assert "not a directory: mode 100644" in report["refused"][0]["detail"]
    assert [p["target"] for p in report["published"]] == ["RECEIPTS.md"], "the receipt is not held by the refused concern"
    assert git(bare, "ls-tree", "master", "--", plain_path).startswith("100644 "), "the file itself is untouched"


def test_a_directory_on_the_concerns_path_is_not_a_link(tmp_path):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, RATIFIED_README)
    _tip_entry(clone, "100644", ob.CONCERNS_TITLE)  # specs and specs/mlv-app are ordinary directories here
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == [] and [w["target"] for w in report["would_push"]] == [CONCERNS]


def test_a_regular_concerns_file_at_the_tip_still_takes_the_next_concern(tmp_path):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, RATIFIED_README)
    _tip_entry(clone, "100644", ob.CONCERNS_TITLE)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == [] and [w["target"] for w in report["would_push"]] == [CONCERNS]


def test_a_distinct_id_is_not_a_duplicate(tmp_path):
    src = init_source(tmp_path)
    add_item(src, "20261016-other-concern.md", ENTRY.replace("-9", "-10"))
    clone = init_bus(tmp_path, RATIFIED_README)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert report["refused"] == [] and len(report["published"]) == 2


def test_the_drain_holds_a_concern_whose_expiry_has_passed(tmp_path, monkeypatch):
    src = init_source(tmp_path)
    clone = init_bus(tmp_path, RATIFIED_README)
    monkeypatch.setattr(ob, "_utc_now_z", lambda: "2026-10-30T21:50:00Z")  # exactly at expires_utc: expired
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert [(r["path"], r["code"]) for r in report["refused"]] == [("doctrine-outbox/20261016-example-concern.md", "CONCERN_EXPIRED")]
    assert report["published"] == []
    monkeypatch.setattr(ob, "_utc_now_z", lambda: "2026-10-30T21:49:59Z")  # one second earlier: still open
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == [] and len(report["would_push"]) == 1


def test_validate_flags_a_repeated_concern_id_across_the_files_it_is_given(tmp_path):
    a, b = tmp_path / "20261016-one-concern.md", tmp_path / "20261016-two-concern.md"
    a.write_text(item_text(), encoding="utf-8")
    b.write_text(item_text(ENTRY.replace("Re-read the thread before each write.", "Another ask.")), encoding="utf-8")
    assert ob.cmd_validate([a], []) == 0
    assert ob.cmd_validate([a, b], []) == 1


def test_every_committed_concern_item_parses_and_passes_law4():
    loaded = ob.load_outbox_items(REPO_ROOT, "HEAD")
    concerns = [it for it in loaded if "item" in it and it["item"]["meta"]["target"] == CONCERNS]
    assert concerns, "no concern items at HEAD: the enumeration itself is broken"
    for it in concerns:
        ob.screen_law4(it["item"]["body"], [])
    assert not [it for it in loaded if "error" in it], [it["error"] for it in loaded if "error" in it]
