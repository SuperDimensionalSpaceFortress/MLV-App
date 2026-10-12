"""Tests for tools/docs/rule_coverage_census.py -- the bullet-to-check census over CLAUDE.md and
agents/*.md. Every repository used here is a throwaway tree under pytest's tmp_path, except
test_tracked_map_resolves_at_head, which runs the tool over the real checkout.
Lives under tests/docs/ and is listed by directory in .github/workflows/tests.yml, because a
test file beside the tool would never be collected.

Named mutations (each turns the named test red, then is reverted):
  M1  count a prose-only label as mapped                  -> test_prose_only_relabel_leaves_coverage_unchanged
  M2  skip the guard-exists check (accept any guard id)   -> test_mapping_naming_a_missing_guard_fails
  M3  stop reporting an id that matches no bullet         -> test_reworded_bullet_makes_its_old_id_stale
  M4  stop collapsing whitespace in normalize()           -> test_normalization_is_stable_across_whitespace_only_edits
  M5  count every bullet as mapped when no map exists     -> test_unmapped_bullet_is_counted_unmapped
  M6  accept a guard whose enforced_today is prose-only   -> test_guard_that_is_itself_prose_only_checks_nothing
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "rule_coverage_census", REPO_ROOT / "tools" / "docs" / "rule_coverage_census.py")
rcc = importlib.util.module_from_spec(_spec)
sys.modules["rule_coverage_census"] = rcc
_spec.loader.exec_module(rcc)

CLAUDE_MD = """# Title

- Never launch the thing bare.
- **Always** run   the check
  before you push.
  - nested rule about `__init__` and snake_case
1. numbered rule one
2) numbered rule two

```
- not a rule, inside a fence
```

Plain paragraph, not a bullet.
"""

AGENT_MD = """## Rules

- Agent rule alpha.
- Agent rule beta.
"""

GUARDS = {
    "items": [
        {"id": "NA-1", "enforced_today": "project hook tools/hooks/h.py, DENY cases per row"},
        {"id": "NA-2", "enforced_today": "prose-only"},
    ]
}


def make_repo(root: Path, claude: str = CLAUDE_MD, agent: str = AGENT_MD) -> Path:
    (root / "agents").mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "tests").mkdir(exist_ok=True)
    (root / "tools" / "hooks").mkdir(parents=True, exist_ok=True)
    (root / "tools" / "docs").mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text(claude, encoding="utf-8")
    (root / "agents" / "a.md").write_text(agent, encoding="utf-8")
    (root / "docs" / "never-authorized.json").write_text(json.dumps(GUARDS), encoding="utf-8")
    (root / "tests" / "test_x.py").write_text(
        "class TestX:\n    def test_ok(self):\n        pass\n", encoding="utf-8")
    (root / "tools" / "hooks" / "h.py").write_text("# hook\n", encoding="utf-8")
    return root


def write_map(root: Path, mappings: dict) -> None:
    doc = {"schema": rcc.MAP_SCHEMA, "mappings": mappings}
    (root / "tools" / "docs" / "rule-coverage-map.json").write_text(
        json.dumps(doc), encoding="utf-8")


def ids_by_text(root: Path) -> dict:
    return {b["text"]: b["id"] for b in rcc.collect_bullets(str(root))}


def run(root: Path):
    res, err = rcc.census(str(root))
    assert err is None, err
    return res


def test_extraction_handles_nesting_continuations_numbering_and_fences(tmp_path):
    make_repo(tmp_path)
    texts = [b["text"] for b in rcc.collect_bullets(str(tmp_path)) if b["file"] == "CLAUDE.md"]
    assert texts == [
        "Never launch the thing bare.",
        "Always run the check before you push.",
        "nested rule about `__init__` and snake_case",
        "numbered rule one",
        "numbered rule two",
    ]
    assert not any("inside a fence" in t for t in texts)
    lines = {b["text"]: b["line"] for b in rcc.collect_bullets(str(tmp_path))}
    assert lines["Never launch the thing bare."] == 3


def test_unmapped_bullet_is_counted_unmapped(tmp_path):
    make_repo(tmp_path)
    res = run(tmp_path)
    assert res["total"] == 7
    assert res["mapped"] == 0 and res["prose_only"] == 0
    assert res["unmapped"] == 7
    assert res["coverage_percent"] == 0.0
    assert res["errors"] == []
    assert {(b["file"], b["line"]) for b in res["bullets"] if b["status"] == "unmapped"} >= {
        ("CLAUDE.md", 3), ("agents/a.md", 3)}
    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 0


def test_mapping_naming_a_missing_guard_fails(tmp_path, capsys):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["Never launch the thing bare."]
    write_map(tmp_path, {bid: {"check": "guard:NA-99"}})
    res = run(tmp_path)
    assert [e["id"] for e in res["errors"]] == [bid]
    assert "NA-99" in res["errors"][0]["why"]
    assert res["mapped"] == 0
    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 1
    assert "NA-99" in capsys.readouterr().out


@pytest.mark.parametrize("check, needle", [
    ("test:tests/test_gone.py", "does not exist"),
    ("test:tests/test_x.py::test_missing", "no definition named"),
    ("hook:tools/hooks/gone.py", "does not exist"),
    ("test:../outside.py", "does not exist"),
    ("prose-only", "needs a reason"),
    ("prose-only:   ", "needs a reason"),
    ("vibes", "unrecognized check"),
])
def test_dangling_or_malformed_checks_fail(tmp_path, check, needle):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["Never launch the thing bare."]
    write_map(tmp_path, {bid: {"check": check}})
    res = run(tmp_path)
    assert len(res["errors"]) == 1 and needle in res["errors"][0]["why"]
    assert res["mapped"] == 0
    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 1


def test_real_checks_resolve_and_raise_coverage(tmp_path):
    make_repo(tmp_path)
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {
        t["Never launch the thing bare."]: {"check": "guard:NA-1"},
        t["numbered rule one"]: {"check": "test:tests/test_x.py::TestX::test_ok"},
        t["numbered rule two"]: "hook:tools/hooks/h.py",
    })
    res = run(tmp_path)
    assert res["errors"] == []
    assert res["mapped"] == 3 and res["mapped_by_kind"] == {"guard": 1, "test": 1, "hook": 1}
    assert res["coverage_percent"] == round(100 * 3 / 7, 2)
    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 0


def test_guard_that_is_itself_prose_only_checks_nothing(tmp_path):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["Never launch the thing bare."]
    write_map(tmp_path, {bid: {"check": "guard:NA-2"}})
    res = run(tmp_path)
    assert res["mapped"] == 0
    assert len(res["errors"]) == 1 and "prose-only" in res["errors"][0]["why"]


def test_prose_only_relabel_leaves_coverage_unchanged(tmp_path):
    make_repo(tmp_path)
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {t["numbered rule one"]: {"check": "test:tests/test_x.py::test_ok"}})
    before = run(tmp_path)
    write_map(tmp_path, {
        t["numbered rule one"]: {"check": "test:tests/test_x.py::test_ok"},
        t["Never launch the thing bare."]: {"check": "prose-only: nothing can observe it"},
        t["numbered rule two"]: {"check": "prose-only: judgment call"},
    })
    after = run(tmp_path)
    assert after["errors"] == []
    assert after["mapped"] == before["mapped"] == 1
    assert after["coverage_percent"] == before["coverage_percent"]
    assert after["prose_only"] == 2
    assert after["unmapped"] == before["unmapped"] - 2
    # Relabelling a bullet that WAS mapped can only lower coverage.
    write_map(tmp_path, {t["numbered rule one"]: {"check": "prose-only: gave up"}})
    lowered = run(tmp_path)
    assert lowered["mapped"] == 0 and lowered["prose_only"] == 1


def test_reworded_bullet_makes_its_old_id_stale(tmp_path, capsys):
    make_repo(tmp_path)
    old = ids_by_text(tmp_path)["Agent rule alpha."]
    write_map(tmp_path, {old: {"check": "test:tests/test_x.py::test_ok"}})
    assert run(tmp_path)["mapped"] == 1

    (tmp_path / "agents" / "a.md").write_text(
        AGENT_MD.replace("Agent rule alpha.", "Agent rule alpha, reworded."), encoding="utf-8")
    res = run(tmp_path)
    assert [e["id"] for e in res["errors"]] == [old]
    assert "stale" in res["errors"][0]["why"]
    assert res["mapped"] == 0, "the reworded bullet must not inherit the old mapping"
    assert rcc.bullet_id("Agent rule alpha, reworded.") != old
    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 1
    assert "stale" in capsys.readouterr().out


def test_normalization_is_stable_across_whitespace_only_edits():
    base = "Always run the check before you push, see `a  b`."
    variants = [
        "Always run the check before you push, see `a  b`.",
        "  Always   run the\tcheck\nbefore you push,\n  see `a  b`.  ",
        "**Always** run the check before you push, see `a  b`.",
        "Always run _the_ check before you *push*, see `a  b`.",
        "__Always__ run the check before you push, see `a  b`.",
    ]
    want = rcc.bullet_id(base)
    assert [rcc.bullet_id(v) for v in variants] == [want] * len(variants)
    # content changes DO change the id, including inside a code span and in case
    assert rcc.bullet_id(base.replace("push", "pull")) != want
    assert rcc.bullet_id(base.replace("Always", "ALWAYS")) != want
    assert rcc.bullet_id(base.replace("`a  b`", "`a  c`")) != want
    # emphasis stripping must not eat identifiers or lone glob stars
    assert rcc.normalize("use `__init__` and snake_case_name and a*b") == (
        "use `__init__` and snake_case_name and a*b")
    assert len(want) == 64


def test_whitespace_edit_in_a_file_keeps_the_mapping_valid(tmp_path):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["Always run the check before you push."]
    write_map(tmp_path, {bid: {"check": "test:tests/test_x.py::test_ok"}})
    rewrapped = CLAUDE_MD.replace("- **Always** run   the check\n  before you push.",
                                  "- Always run the check before\n      you   push.")
    assert rewrapped != CLAUDE_MD
    (tmp_path / "CLAUDE.md").write_text(rewrapped, encoding="utf-8")
    res = run(tmp_path)
    assert res["errors"] == [] and res["mapped"] == 1


def test_json_output_is_machine_readable(tmp_path, capsys):
    make_repo(tmp_path)
    assert rcc.main(["--repo-root", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == rcc.SCHEMA
    assert data["total"] == 7 and data["unmapped"] == 7 and data["coverage_percent"] == 0.0
    assert all(len(b["id"]) == 64 and b["status"] == "unmapped" for b in data["bullets"])


def test_unreadable_mapping_file_is_exit_two(tmp_path, capsys):
    make_repo(tmp_path)
    (tmp_path / "tools" / "docs" / "rule-coverage-map.json").write_text("{not json", encoding="utf-8")
    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 2
    assert "cannot read mapping file" in capsys.readouterr().err


def test_tracked_map_resolves_at_head():
    """The checked-in map must be valid against the checked-in rule docs. If this fails because
    someone reworded a mapped bullet, re-point or drop that entry in tools/docs/rule-coverage-map.json
    (the failure names the id; `py -3 tools/docs/rule_coverage_census.py --json` lists every id)."""
    res = run(REPO_ROOT)
    assert res["errors"] == [], res["errors"]
    assert res["total"] > 0
    assert res["mapped"] > 0
    assert res["mapped"] + res["prose_only"] + res["unmapped"] == res["total"]
