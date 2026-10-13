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
    write_map(tmp_path, {t["numbered rule one"]: {"check": "test:tests/test_x.py::TestX::test_ok"}})
    before = run(tmp_path)
    write_map(tmp_path, {
        t["numbered rule one"]: {"check": "test:tests/test_x.py::TestX::test_ok"},
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
    write_map(tmp_path, {old: {"check": "test:tests/test_x.py::TestX::test_ok"}})
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
    write_map(tmp_path, {bid: {"check": "test:tests/test_x.py::TestX::test_ok"}})
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


# ---------------------------------------------------------------------------------------------
# RULE-COVERAGE-CENSUS-HARDENING-1: five hardening items from the #412 keys. Each group below is
# RED on 997d6013 (the #412 merge) and green after the change.
#   H1  pin vs behaviour split in the printed report   RULE-COVERAGE-PIN-VS-BEHAVIOUR-SPLIT-1
#   H2  a mapping must name a test case, not a file    RULE-COVERAGE-CHECK-GRANULARITY-1
#   H3  exists-not-enforced-in-CI is its own label     RULE-COVERAGE-CI-STATUS-1
#   H4  fence delimiter type and length are tracked    RULE-COVERAGE-MARKDOWN-FENCES-1
#   H5  resolved-path containment for tests and --map  RULE-COVERAGE-PATH-CONTAINMENT-1
# ---------------------------------------------------------------------------------------------

TOKEN_CLAUDE_MD = """# Title

## Rules

- Rule one stays prose.
- Rule two has a behavioural check.
- Rule three has a behavioural check.
- Rule four stays prose.

### Pinned contract tokens

- `token-a` -- detail in somewhere.md
- `token-b` -- detail in somewhere.md
- `token-c` -- detail in somewhere.md
"""

PIN = "token-presence-pin"


def _line_with(out: str, label: str) -> str:
    hits = [ln for ln in out.splitlines() if label in ln]
    assert hits, "no report line carries %r in:\n%s" % (label, out)
    return hits[0]


def _add_file(root: Path, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


# ----- H1: pin vs behaviour ------------------------------------------------------------------

def test_report_splits_token_pins_from_behavioural_checks(tmp_path, capsys):
    make_repo(tmp_path, claude=TOKEN_CLAUDE_MD, agent="")
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {
        t["Rule two has a behavioural check."]: {"check": "test:tests/test_x.py::TestX::test_ok"},
        t["`token-a` -- detail in somewhere.md"]: {
            "check": "test:tests/test_x.py::TestX::test_ok", "strength": PIN},
        t["`token-b` -- detail in somewhere.md"]: {
            "check": "test:tests/test_x.py::TestX::test_ok", "strength": PIN},
    })
    res = run(tmp_path)
    assert res["errors"] == []
    assert res["mapped"] == 3, "mapped keeps counting every resolving check"
    assert res["behavioural_mapped"] == 1 and res["pin_mapped"] == 2
    assert res["token_inventory"] == 3 and res["rule_bullets"] == 4
    assert res["behavioural_percent"] == 25.0          # 1 of the 4 rule bullets
    assert res["coverage_percent"] == round(100 * 3 / 7, 2)  # pre-existing meaning, unchanged

    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 0
    out = capsys.readouterr().out
    head = _line_with(out, "behavioural coverage")
    assert "25.00%" in head and "1 of 4" in head
    assert "42.86%" not in head, "the headline must not be the pin-inflated number"
    assert "2" in _line_with(out, "token-presence pins")
    assert out.index("behavioural coverage") < out.index("all mapped")
    assert "42.86%" in _line_with(out, "all mapped")


def test_unknown_strength_value_is_a_mapping_error(tmp_path):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["Never launch the thing bare."]
    write_map(tmp_path, {bid: {"check": "test:tests/test_x.py::TestX::test_ok", "strength": "strong"}})
    res = run(tmp_path)
    assert len(res["errors"]) == 1 and "strength" in res["errors"][0]["why"]
    assert res["mapped"] == 0


def test_json_carries_the_split_numbers(tmp_path, capsys):
    make_repo(tmp_path, claude=TOKEN_CLAUDE_MD, agent="")
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {t["`token-a` -- detail in somewhere.md"]: {
        "check": "test:tests/test_x.py::TestX::test_ok", "strength": PIN}})
    assert rcc.main(["--repo-root", str(tmp_path), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["pin_mapped"] == 1 and data["behavioural_mapped"] == 0
    assert data["behavioural_percent"] == 0.0


# ----- H2: a mapping names a test case -------------------------------------------------------

TWO_CLASSES = ("class TestA:\n    def test_a(self):\n        pass\n\n"
               "class TestB:\n    def test_b(self):\n        pass\n\n"
               "def test_top():\n    pass\n")


def test_file_level_mapping_is_refused(tmp_path):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["Never launch the thing bare."]
    write_map(tmp_path, {bid: {"check": "test:tests/test_x.py"}})
    res = run(tmp_path)
    assert res["mapped"] == 0
    assert len(res["errors"]) == 1 and "name a test case" in res["errors"][0]["why"]


def test_a_class_name_alone_is_not_a_test_case(tmp_path):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["Never launch the thing bare."]
    write_map(tmp_path, {bid: {"check": "test:tests/test_x.py::TestX"}})
    res = run(tmp_path)
    assert res["mapped"] == 0
    assert len(res["errors"]) == 1 and "test case" in res["errors"][0]["why"]


def test_method_must_live_in_the_class_the_mapping_names(tmp_path):
    make_repo(tmp_path)
    _add_file(tmp_path, "tests/test_two.py", TWO_CLASSES)
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {
        t["Never launch the thing bare."]: {"check": "test:tests/test_two.py::TestA::test_b"},
        t["numbered rule one"]: {"check": "test:tests/test_two.py::TestB::test_b"},
        t["numbered rule two"]: {"check": "test:tests/test_two.py::test_top"},
    })
    res = run(tmp_path)
    assert [e["id"] for e in res["errors"]] == [t["Never launch the thing bare."]]
    assert "no definition named" in res["errors"][0]["why"]
    assert res["mapped"] == 2


def test_deleting_the_asserting_case_turns_the_mapping_stale(tmp_path, capsys):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["Never launch the thing bare."]
    write_map(tmp_path, {bid: {"check": "test:tests/test_x.py::TestX::test_ok"}})
    assert run(tmp_path)["mapped"] == 1
    _add_file(tmp_path, "tests/test_x.py", "class TestX:\n    def test_other(self):\n        pass\n")
    res = run(tmp_path)
    assert res["mapped"] == 0
    assert len(res["errors"]) == 1 and "stale" in res["errors"][0]["why"]
    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 1
    assert "stale" in capsys.readouterr().out


def test_a_commented_out_or_stringified_def_is_not_a_case(tmp_path):
    make_repo(tmp_path)
    _add_file(tmp_path, "tests/test_ghost.py",
              "# def test_ghost(): pass\nTEXT = '''\ndef test_ghost2(self):\n'''\n")
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {
        t["Never launch the thing bare."]: {"check": "test:tests/test_ghost.py::test_ghost"},
        t["numbered rule one"]: {"check": "test:tests/test_ghost.py::test_ghost2"},
    })
    res = run(tmp_path)
    assert res["mapped"] == 0 and len(res["errors"]) == 2


def test_powershell_cases_are_named_by_their_dash_name_argument(tmp_path):
    make_repo(tmp_path)
    _add_file(tmp_path, "tools/p.ps1",
              'Assert-Rejected -Name "case one" `\n    -Arguments @("-X")\n')
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {
        t["Never launch the thing bare."]: {"check": "test:tools/p.ps1::case one"},
        t["numbered rule one"]: {"check": "test:tools/p.ps1"},
        t["numbered rule two"]: {"check": "test:tools/p.ps1::case two"},
    })
    res = run(tmp_path)
    assert {e["id"] for e in res["errors"]} == {t["numbered rule one"], t["numbered rule two"]}
    assert res["mapped"] == 1


# ----- H3: exists vs enforced in CI ----------------------------------------------------------

def _write_ci_allowlist(root: Path, paths) -> None:
    body = "LOCAL_ONLY_OR_UNCOLLECTED: dict[str, str] = {\n"
    for p in paths:
        body += ('    "%s": (\n        "UNCOLLECTED at abc, follow-up X: "\n'
                 '        "tests.yml names only others"\n    ),\n' % p)
    body += "}\n"
    _add_file(root, "tools/repo_hygiene/test_ci_collects_every_test.py", body)


def test_uncollected_tests_are_labelled_exists_not_enforced(tmp_path, capsys):
    make_repo(tmp_path)
    _add_file(tmp_path, "tests/test_unc.py", "def test_u():\n    pass\n")
    _write_ci_allowlist(tmp_path, ["tests/test_unc.py"])
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {
        t["numbered rule one"]: {"check": "test:tests/test_x.py::TestX::test_ok"},
        t["numbered rule two"]: {"check": "test:tests/test_unc.py::test_u"},
        t["Never launch the thing bare."]: {"check": "guard:NA-1"},
    })
    res = run(tmp_path)
    assert res["errors"] == []
    assert res["mapped"] == 3, "existence still counts as mapped"
    assert res["exists_not_enforced"] == 1
    assert res["behavioural_ci_enforced"] == 1, "only the collected test is CI-enforced here"
    status = {b["text"]: b.get("ci_status") for b in res["bullets"]}
    assert status["numbered rule one"] == "enforced"
    assert status["numbered rule two"] == "exists-not-enforced"
    assert rcc.main(["--repo-root", str(tmp_path), "--no-list"]) == 0
    line = _line_with(capsys.readouterr().out, "exists, not enforced in CI")
    assert "1" in line


def test_ci_status_is_unknown_not_enforced_when_the_allowlist_is_unreadable(tmp_path):
    make_repo(tmp_path)
    bid = ids_by_text(tmp_path)["numbered rule one"]
    write_map(tmp_path, {bid: {"check": "test:tests/test_x.py::TestX::test_ok"}})
    res = run(tmp_path)
    assert res["behavioural_ci_enforced"] == 0
    assert [b["ci_status"] for b in res["bullets"] if b["status"] == "mapped"] == ["unknown"]


# ----- H4: fence delimiters ------------------------------------------------------------------

def test_nested_and_tilde_fences_do_not_flip_the_in_fence_state():
    body = "\n".join([
        "````", "```", "- hidden one", "````", "- actual one",
        "~~~", "```", "- hidden two", "~~~", "- actual two",
        "```", "~~~", "- hidden three", "```", "- actual three",
    ]) + "\n"
    assert [raw for _, raw in rcc.extract_bullets(body)] == [
        "actual one", "actual two", "actual three"]


def test_a_fence_closer_needs_the_same_char_enough_length_and_no_info_string():
    body = "\n".join([
        "````", "```python", "- hidden one", "``` ", "- hidden two", "~~~~", "- hidden three",
        "````", "- actual one", "```", "- hidden four", "```x", "- hidden five", "```",
        "- actual two",
    ]) + "\n"
    assert [raw for _, raw in rcc.extract_bullets(body)] == ["actual one", "actual two"]


def test_a_longer_fence_after_continuation_lines_still_ends_the_bullet():
    body = "- first rule\n  wrapped\n````\n- hidden\n````\n"
    assert [raw for _, raw in rcc.extract_bullets(body)] == ["first rule\n  wrapped"]


# ----- H5: path containment ------------------------------------------------------------------

def _link_dir(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":
        import subprocess
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
    pytest.skip("cannot create a symlink or junction on this host")


def test_map_argument_outside_the_repo_root_is_rejected(tmp_path, capsys):
    repo = make_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    bid = ids_by_text(repo)["numbered rule one"]
    (outside / "map.json").write_text(json.dumps({"schema": rcc.MAP_SCHEMA, "mappings": {
        bid: {"check": "test:tests/test_x.py::TestX::test_ok"}}}), encoding="utf-8")
    assert rcc.main(["--repo-root", str(repo), "--map", str(outside / "map.json"),
                     "--no-list"]) == 2
    assert "outside the repo root" in capsys.readouterr().err


def test_relative_map_argument_that_climbs_out_is_rejected(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path / "repo")
    (tmp_path / "outside.json").write_text(
        json.dumps({"schema": rcc.MAP_SCHEMA, "mappings": {}}), encoding="utf-8")
    monkeypatch.chdir(repo)
    assert rcc.main(["--repo-root", str(repo), "--map", "../outside.json", "--no-list"]) == 2
    assert "outside the repo root" in capsys.readouterr().err


def test_map_argument_inside_the_repo_root_is_accepted(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    _add_file(repo, "alt/map.json", json.dumps({"schema": rcc.MAP_SCHEMA, "mappings": {}}))
    monkeypatch.chdir(repo)
    assert rcc.main(["--repo-root", str(repo), "--map", "alt/map.json", "--no-list"]) == 0
    assert rcc.main(["--repo-root", str(repo), "--map", str(repo / "alt" / "map.json"),
                     "--no-list"]) == 0


def test_a_link_inside_the_repo_that_points_outside_is_not_a_test_file(tmp_path):
    repo = make_repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "test_o.py").write_text("def test_o():\n    pass\n", encoding="utf-8")
    _link_dir(repo / "tests" / "linked", outside)
    t = ids_by_text(repo)
    write_map(repo, {
        t["numbered rule one"]: {"check": "test:tests/linked/test_o.py::test_o"},
        t["numbered rule two"]: {"check": "hook:tests/linked/test_o.py"},
    })
    res = run(repo)
    assert res["mapped"] == 0 and len(res["errors"]) == 2
    assert all("outside the repo root" in e["why"] for e in res["errors"])


# ---------------------------------------------------------------------------------------------
# RULE-COVERAGE-CENSUS-HARDENING-1 r2: fixes from the PR #417 r1 keys. Each group is RED on
# d74a0912 and green after the change.
#   R1  a generated case counts only while its generator is live   (sol-001 BLOCKER, NA-11)
#   R2  a PowerShell case name inside a comment or here-string is not a case   (sol hardening)
#   R3  a .py case is found at the scope the map spells, exactly   RULE-COVERAGE-CASE-SCOPE-EXACT-1
#   R4  a non-.py/.ps1 file cannot name a case   RULE-COVERAGE-CASE-MATCH-OTHER-EXTENSIONS-1
#   R5  an inventory bullet with no strength is a pin   RULE-COVERAGE-INVENTORY-DEFAULT-PIN-1
# ---------------------------------------------------------------------------------------------

GEN_SETATTR = 'setattr(HookTests, "test_" + _slug(_case["name"]), _make_test(_case))'
GEN_BODY = '''import re
import unittest

CASES = [
    {"name": "row one denies", "expect": "DENY"},
    {"name": "Row Two: allows!", "expect": "ALLOW"},
]


class HookTests(unittest.TestCase):
    def _run_case(self, case):
        pass


class OtherTests(unittest.TestCase):
    pass


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _make_test(case):
    def method(self):
        self._run_case(case)
    return method


for _case in CASES:
    %s
''' % GEN_SETATTR


def _gen_map(root: Path, spec: str) -> dict:
    t = ids_by_text(root)
    write_map(root, {t["Never launch the thing bare."]: {"check": "test:tests/test_gen.py::" + spec}})
    return run(root)


def test_a_generated_case_counts_while_its_setattr_loop_and_row_are_live(tmp_path):
    make_repo(tmp_path)
    _add_file(tmp_path, "tests/test_gen.py", GEN_BODY)
    res = _gen_map(tmp_path, "HookTests::test_row_one_denies")
    assert res["errors"] == [] and res["behavioural_mapped"] == 1
    res = _gen_map(tmp_path, "HookTests::test_row_two_allows")
    assert res["errors"] == [] and res["behavioural_mapped"] == 1


@pytest.mark.parametrize("mutation, spec", [
    ("remove the setattr loop", "HookTests::test_row_one_denies"),
    ("remove the row", "HookTests::test_row_one_denies"),
    ("another class", "OtherTests::test_row_one_denies"),
    ("bare name", "test_row_one_denies"),
    ("unknown row", "HookTests::test_row_three"),
])
def test_a_generated_case_goes_stale_with_its_generator(tmp_path, mutation, spec):
    make_repo(tmp_path)
    body = GEN_BODY
    if mutation == "remove the setattr loop":
        assert body.count(GEN_SETATTR) == 1
        body = body.replace(GEN_SETATTR, "pass")
    elif mutation == "remove the row":
        body = body.replace('    {"name": "row one denies", "expect": "DENY"},\n', "")
    _add_file(tmp_path, "tests/test_gen.py", body)
    res = _gen_map(tmp_path, spec)
    assert res["behavioural_mapped"] == 0 and res["mapped"] == 0
    assert len(res["errors"]) == 1 and "no definition named" in res["errors"][0]["why"]


@pytest.mark.parametrize("variant", [
    "# " + GEN_SETATTR,                                   # commented out
    "s = %r" % GEN_SETATTR,                               # inside a string
    "def _gen():\n    " + GEN_SETATTR,                    # in a def nothing calls
    "class Never:\n    " + GEN_SETATTR,                   # in a class body that is not a loop
])
def test_only_a_live_module_level_loop_generates_cases(tmp_path, variant):
    make_repo(tmp_path)
    body = GEN_BODY.replace("    " + GEN_SETATTR + "\n", "    pass\n") + variant + "\n"
    assert body.count("    pass\n") >= 1 and "for _case in CASES:\n    pass\n" in body
    _add_file(tmp_path, "tests/test_gen.py", body)
    res = _gen_map(tmp_path, "HookTests::test_row_one_denies")
    assert res["mapped"] == 0 and len(res["errors"]) == 1


def _tracked_repo_copy(dst: Path, mutate=None) -> Path:
    """The checked-in rule docs, map and every file the map names, copied under dst; mutate
    maps a repo-relative path to a function(text)->text applied to its copy."""
    mutate = mutate or {}
    rels = {"CLAUDE.md", "docs/never-authorized.json", "tools/docs/rule-coverage-map.json",
            rcc.CI_COLLECT_GUARD}
    rels.update("agents/" + p.name for p in (REPO_ROOT / "agents").glob("*.md"))
    mp = json.loads((REPO_ROOT / rcc.MAP_PATH).read_text(encoding="utf-8-sig"))
    for entry in mp["mappings"].values():
        check = entry["check"] if isinstance(entry, dict) else entry
        if check.startswith(("test:", "hook:")):
            rels.add(check.split(":", 1)[1].split("::")[0].strip())
    for rel in sorted(rels):
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        text = (REPO_ROOT / rel).read_text(encoding="utf-8-sig", errors="replace")
        if rel in mutate:
            new = mutate[rel](text)
            assert new != text, "mutation of %s changed nothing" % rel
            text = new
        out.write_text(text, encoding="utf-8")
    return dst


NA_TEST = "tools/repo_hygiene/test_mlv_never_authorized.py"
NA_SETATTR = "setattr(MlvNeverAuthorizedHookTests, _name, _make_test(_case))"
NA11_BULLET_START = "Hook row NA-11 denies Write/Edit/shell truncating"


def _na11(res):
    return next(b for b in res["bullets"] if b["text"].startswith(NA11_BULLET_START))


def test_removing_the_generated_na11_denial_methods_stops_it_counting(tmp_path):
    """sol-001 BLOCKER: replace the module-level setattr with `pass` in a source copy; the
    census used to keep NA-11 behavioural because the map named an inventory test."""
    real = run(REPO_ROOT)
    na11 = _na11(real)
    assert na11["status"] == "mapped" and na11["strength"] == "behavioural"

    def drop_loop(text):
        assert text.count(NA_SETATTR) == 1
        return text.replace(NA_SETATTR, "pass")

    res = run(_tracked_repo_copy(tmp_path / "copy", {NA_TEST: drop_loop}))
    assert na11["id"] in [e["id"] for e in res["errors"]], "NA-11 must be reported stale"
    assert res["behavioural_mapped"] == real["behavioural_mapped"] - 1
    assert res["mapped"] == real["mapped"] - 1


def test_the_tracked_na11_mapping_names_a_generated_denial_case():
    mp = json.loads((REPO_ROOT / rcc.MAP_PATH).read_text(encoding="utf-8-sig"))
    check = mp["mappings"][_na11(run(REPO_ROOT))["id"]]["check"]
    assert "::MlvNeverAuthorizedHookTests::test_na11_" in check, check
    assert "register_row_has_at_least_one_deny_case" not in check, "an inventory test is no hook run"


# ----- R2: PowerShell comments and here-strings ----------------------------------------------

def _ps_check(tmp_path, body, case="case one"):
    make_repo(tmp_path)
    _add_file(tmp_path, "tools/p.ps1", body)
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {t["Never launch the thing bare."]: {"check": "test:tools/p.ps1::" + case}})
    return run(tmp_path)


@pytest.mark.parametrize("body, case", [
    ('# Assert-Rejected -Name "case one"\n', "case one"),
    ('  #Assert-Rejected -Name "case one"\n', "case one"),
    ('Write-Host 1 # Assert-Rejected -Name "case one"\n', "case one"),
    ('<#\nAssert-Rejected -Name "case one"\n#>\n', "case one"),
    ('$x = @\'\nAssert-Rejected -Name "case one"\n\'@\n', "case one"),
    ('$x = @"\nAssert-Rejected -Name "case one"\n"@\n', "case one"),
    ('# function case-one\n', "case-one"),
])
def test_a_powershell_case_name_only_in_a_comment_or_here_string_is_stale(tmp_path, body, case):
    res = _ps_check(tmp_path, body, case)
    assert res["mapped"] == 0 and len(res["errors"]) == 1
    assert "stale" in res["errors"][0]["why"]


@pytest.mark.parametrize("body, case", [
    ('Assert-Rejected -Name "case one" `\n   -Arguments @("-X")\n', "case one"),
    ('# note\nAssert-Rejected -Name "case one"  # trailing\n', "case one"),
    ('Assert-Rejected -Name "has # inside"\n', "has # inside"),
    ("$x = @'\nignored\n'@\nAssert-Rejected -Name \"case one\"\n", "case one"),
    ('<# header #>\nAssert-Rejected -Name "case one"\n', "case one"),
    ('function case-one {\n}\n', "case-one"),
])
def test_a_powershell_case_that_runs_is_still_a_case(tmp_path, body, case):
    res = _ps_check(tmp_path, body, case)
    assert res["errors"] == [] and res["mapped"] == 1


# ----- R3: exact scope -----------------------------------------------------------------------

NESTED = ("class TestA:\n    def test_a(self):\n        pass\n\n"
          "    class Inner:\n        def test_in(self):\n            pass\n\n"
          "def test_top():\n    def helper():\n        pass\n")


@pytest.mark.parametrize("spec, ok", [
    ("test_a", False),                       # a method is not a module-level function
    ("TestA::test_a", True),
    ("Inner::test_in", False),               # only the suffix of TestA::Inner::test_in
    ("TestA::Inner::test_in", True),
    ("test_top", True),
    ("test_top::helper", True),
    ("helper", False),
])
def test_a_python_case_resolves_only_at_the_scope_the_map_spells(tmp_path, spec, ok):
    make_repo(tmp_path)
    _add_file(tmp_path, "tests/test_n.py", NESTED)
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {t["Never launch the thing bare."]: {"check": "test:tests/test_n.py::" + spec}})
    res = run(tmp_path)
    assert (res["mapped"] == 1) is ok, res["errors"]


# ----- R4: other file types ------------------------------------------------------------------

@pytest.mark.parametrize("rel", ["tests/notes.md", "tests/run.sh", "tests/t.js"])
def test_a_file_that_is_neither_python_nor_powershell_cannot_name_a_case(tmp_path, rel):
    make_repo(tmp_path)
    _add_file(tmp_path, rel, "it('does the thing')\ntest_x\n")
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {t["Never launch the thing bare."]: {"check": "test:%s::test_x" % rel}})
    res = run(tmp_path)
    assert res["mapped"] == 0 and len(res["errors"]) == 1
    assert "not a .py or .ps1 file" in res["errors"][0]["why"]


# ----- R5: an inventory bullet defaults to a pin ---------------------------------------------

def test_an_inventory_bullet_with_no_strength_is_counted_as_a_pin(tmp_path):
    make_repo(tmp_path, claude=TOKEN_CLAUDE_MD, agent="")
    t = ids_by_text(tmp_path)
    write_map(tmp_path, {
        t["`token-a` -- detail in somewhere.md"]: {"check": "test:tests/test_x.py::TestX::test_ok"},
        t["`token-b` -- detail in somewhere.md"]: "test:tests/test_x.py::TestX::test_ok",
        t["`token-c` -- detail in somewhere.md"]: {
            "check": "test:tests/test_x.py::TestX::test_ok", "strength": "behavioural"},
        t["Rule two has a behavioural check."]: {"check": "test:tests/test_x.py::TestX::test_ok"},
    })
    res = run(tmp_path)
    assert res["errors"] == []
    assert res["pin_mapped"] == 2, "a and b carry no strength, so they are pins"
    assert res["behavioural_mapped"] == 2, "an explicit behavioural, and a rule bullet"
    assert res["token_inventory"] == 3 and res["rule_bullets"] == 4 + 1
