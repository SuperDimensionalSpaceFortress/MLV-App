"""The project-to-CoS concern channel in the doctrine outbox (card COS-CONCERN-CHANNEL-1).

A concern is an entry in the project-owned `specs/mlv-app/cos-concerns.md`. The field set is closed, so
no entry can carry an approval, a ruling or a permission; every entry expires; the drain holds an entry
until the fetched bus tip's cos-feedback README names the `CoS-CONCERN:` reader duty. Small and fast on
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
    clone = init_bus(tmp_path, "# cos-feedback\n\nThe CoS reads `CoS-CONCERN:` comments each pass.\n")
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert report["refused"] == [] and [p["target"] for p in report["published"]] == [CONCERNS]
    text = git(clone, "show", f"{report['commit']}:{CONCERNS}")
    assert text.startswith("# mlv-app concerns to the CoS (written only by mlv-app; the CoS answers in cos-feedback/mlv-app/concerns.md)\n\n### mlv-app-cc-20261016-9\n")
    assert text.count("<!-- outbox:") == 1
    changed = git(clone, "diff", "--name-only", f"{report['commit']}~1", report["commit"]).splitlines()
    assert changed == [CONCERNS], "the drain writes the project-owned file and nothing else"
    again = ob.drain(src, clone, "HEAD", tmp_path / "sent-lost.jsonl", [], push=True)
    assert again["published"] == [] and [a["via"] for a in again["already_sent"]] == ["bus_tip"]


def test_every_committed_concern_item_parses_and_passes_law4():
    loaded = ob.load_outbox_items(REPO_ROOT, "HEAD")
    concerns = [it for it in loaded if "item" in it and it["item"]["meta"]["target"] == CONCERNS]
    assert concerns, "no concern items at HEAD: the enumeration itself is broken"
    for it in concerns:
        ob.screen_law4(it["item"]["body"], [])
    assert not [it for it in loaded if "error" in it], [it["error"] for it in loaded if "error" in it]
