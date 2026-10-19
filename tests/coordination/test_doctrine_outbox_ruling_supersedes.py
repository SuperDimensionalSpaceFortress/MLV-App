"""A ruling candidate names what it touches or supersedes (card ROUTE-RULING-SUPERSEDES-1).

CoS work order FWO-DEFECT-PREVENT-1 item 4, seed #469 r1: a candidate that overlapped RULINGS.md:1126-1152, R15 and an
open candidate without saying so was found by the reviewer, not the producer. `validate` now refuses a ruling candidate
with no `touches/supersedes:` declaration, with an entry that is not a rule id or a bus path, or (given a bus clone)
with a target the bus does not hold at its tip. The bus here is always a scratch fixture, never the live one. Small
on purpose: the 2,900-line sibling suite takes more than ten minutes.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("doctrine_outbox_ruling_under_test", REPO_ROOT / "tools" / "coordination" / "doctrine_outbox.py")
ob = importlib.util.module_from_spec(_spec)
sys.modules["doctrine_outbox_ruling_under_test"] = ob
_spec.loader.exec_module(ob)

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]
FOLD_STANDARD = REPO_ROOT / "doctrine-outbox" / "20261016-fleet-fold-standard.md"
OPEN_CANDIDATE = "ruling-candidates/fold-is-a-disposition-per-entry-and-lag-is-derived-r1.md"

RULINGS = ("# RULINGS\n\n"
           "## Fleet correction, 2026-08-30 - one box-level sweep\n\ntext\n\n"
           "## OWNER RULING, 2026-10-03 - R15: a hub session that runs for days is shown new doctrine\n\n"
           "- **R15.1 - The outcome that binds.** text\n"
           "- **R15.3 - A reference mechanism.** text\n\n"
           "## OWNER RULING, 2026-10-02 - R14: the writer pays\n\n"
           "- **R14.8 - Each board implements it in its own tools.** text\n\n"
           "Prose only: see R77 and R150 mentioned in passing.\n")

BUS_FILES = {
    "RULINGS.md": RULINGS,
    OPEN_CANDIDATE: "# open candidate\n",
    "tools/fleet-sweep.mjs": "// sweep\n",
    "tools/doctrine-sync.mjs": "// sync\n",
    "specs/mlv-app/cards.md": "# cards\n",
}


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def make_bus(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """A scratch bus clone with one commit holding `files` (default BUS_FILES). Never the live bus."""
    bus = tmp_path / "bus-clone"
    bus.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(bus)], check=True, capture_output=True, text=True)
    for rel, text in (BUS_FILES if files is None else files).items():
        target = bus / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    git(bus, "add", "-A")
    git(bus, "commit", "-q", "-m", "seed bus")
    return bus


def candidate(declaration: str = "", heading: str = "### mlv-app, 2026-10-19 - RULING CANDIDATE: one standard for X") -> str:
    body = f"{heading}\n\nOwner-requested.\n\n{declaration}\nRules: S1 one thing. S2 another.\n"
    return f"---\ntarget: RECEIPTS.md\nkind: receipt\nsource_commit: PENDING\nlaw4: attested\n---\n{body}"


def write_item(tmp_path: Path, text: str, name: str = "20261019-ruling-candidate-x.md") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def validate(tmp_path: Path, text: str, bus: Path | None, capsys) -> tuple[int, str]:
    rc = ob.cmd_validate([write_item(tmp_path, text)], [], bus)
    return rc, capsys.readouterr().err


# ---------- missing, empty and malformed declarations are refused ----------

@pytest.mark.parametrize("label, declaration", [
    ("no declaration", ""),
    ("empty label", "touches/supersedes:\n"),
    ("empty touches label", "touches:\n"),
    ("a label with no entries", "- **touches/supersedes:**\n"),
    ("the label inside a fence does not count", "```\ntouches/supersedes: R15\n```\n"),
    ("the word without a colon is prose", "This touches and supersedes R15.\n"),
])
def test_a_candidate_without_a_declaration_is_refused(tmp_path, capsys, label, declaration):
    rc, err = validate(tmp_path, candidate(declaration), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (label, err)


def test_none_without_a_reason_is_refused(tmp_path, capsys):
    for declaration in ("supersedes: none\n", "touches/supersedes: none:\n", "touches/supersedes: none: nothing\n"):
        rc, err = validate(tmp_path, candidate(declaration), None, capsys)
        assert rc == 1 and "RULING_CANDIDATE_NONE_NEEDS_REASON" in err, (declaration, err)


def test_none_beside_a_rule_is_refused(tmp_path, capsys):
    rc, err = validate(tmp_path, candidate("touches/supersedes:\n- R15\n- none: it overlaps nothing at all\n"), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NONE_WITH_RULES" in err, err


def test_none_with_a_reason_is_accepted_and_needs_no_bus_lookup(tmp_path, capsys):
    rc, err = validate(tmp_path, candidate("touches/supersedes: none: it overlaps no standing rule or open candidate, checked by grep\n"), None, capsys)
    assert rc == 0 and err == "", err


@pytest.mark.parametrize("entry", ["fleet-sweep", "../RULINGS.md", "/etc/passwd", "C:\\x\\RULINGS.md", "r15.", "the whole of RULINGS", "tools\\fleet-sweep.mjs"])
def test_an_entry_that_is_not_a_rule_id_or_a_bus_path_is_refused(tmp_path, capsys, entry):
    rc, err = validate(tmp_path, candidate(f"touches/supersedes: {entry}\n"), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_TARGET_MALFORMED" in err, (entry, err)


# ---------- a named target must exist on the bus ----------

@pytest.mark.parametrize("declaration", [
    "touches/supersedes: RULINGS.md:1126-1152, R15, R14.8\n",
    "touches/supersedes:\n- RULINGS.md:1126-1152\n- `R15` (the reference mechanism)\n- tools/fleet-sweep.mjs\n- " + OPEN_CANDIDATE + "\n",
    "- **touches/supersedes:** R15.3; tools/doctrine-sync.mjs#debt; specs/mlv-app\n",
    "touches: R15\nsupersedes: tools/fleet-sweep.mjs\n",
])
def test_a_valid_list_naming_things_the_bus_holds_is_accepted(tmp_path, capsys, declaration):
    rc, err = validate(tmp_path, candidate(declaration), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", err


@pytest.mark.parametrize("entry", [
    "tools/fleet-sweep.mjs.bak",
    "ruling-candidates/fold-is-a-disposition-r1.md",
    "specs/mlv-app/nope.md",
    "R99",
    "R77",
    "R15.9",
    "R150",
])
def test_a_target_the_bus_does_not_hold_is_refused(tmp_path, capsys, entry):
    rc, err = validate(tmp_path, candidate(f"touches/supersedes:\n- R15\n- {entry}\n"), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_TARGET_NOT_ON_BUS" in err and repr(entry) in err, (entry, err)
    assert "'R15'" not in err, "only the missing entry is named"


def test_a_rule_id_the_bus_has_only_in_prose_is_not_on_the_bus(tmp_path, capsys):
    rc, err = validate(tmp_path, candidate("touches/supersedes: R77\n"), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_TARGET_NOT_ON_BUS" in err, err


def test_the_check_reads_the_bus_tip_not_its_working_tree(tmp_path, capsys):
    bus = make_bus(tmp_path)
    (bus / "tools" / "uncommitted.mjs").write_text("// not committed\n", encoding="utf-8")
    rc, err = validate(tmp_path, candidate("touches/supersedes: tools/uncommitted.mjs\n"), bus, capsys)
    assert rc == 1 and "RULING_CANDIDATE_TARGET_NOT_ON_BUS" in err, err


def test_the_bus_is_only_read(tmp_path, capsys):
    bus = make_bus(tmp_path)
    before = git(bus, "status", "--porcelain=v1", "--ignored"), git(bus, "rev-parse", "HEAD")
    validate(tmp_path, candidate("touches/supersedes: R15, tools/fleet-sweep.mjs, R99\n"), bus, capsys)
    assert (git(bus, "status", "--porcelain=v1", "--ignored"), git(bus, "rev-parse", "HEAD")) == before


# ---------- with no bus, or one that cannot be read ----------

def test_with_no_bus_the_shape_is_checked_and_the_unchecked_targets_are_named(tmp_path, capsys):
    rc, err = validate(tmp_path, candidate("touches/supersedes: R15, tools/fleet-sweep.mjs\n"), None, capsys)
    assert rc == 0 and "NOT checked against the bus" in err and "R15" in err and "tools/fleet-sweep.mjs" in err, err
    rc, err = validate(tmp_path, candidate("touches/supersedes: fleet-sweep\n"), None, capsys)
    assert rc == 1 and "RULING_CANDIDATE_TARGET_MALFORMED" in err, err


def test_a_bus_that_cannot_be_read_is_unknown_never_a_pass(tmp_path):
    with pytest.raises(ob.Unknown) as caught:
        ob.cmd_validate([write_item(tmp_path, candidate("touches/supersedes: R15\n"))], [], tmp_path / "no-such-clone")
    assert caught.value.code == "RULING_BUS_UNREADABLE"
    empty = tmp_path / "empty-bus"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True, capture_output=True, text=True)
    with pytest.raises(ob.Unknown):
        ob.cmd_validate([write_item(tmp_path, candidate("touches/supersedes: R15\n"))], [], empty)


# ---------- non-ruling items are unaffected ----------

@pytest.mark.parametrize("label, text", [
    ("a plain receipt entry",
     "---\ntarget: RECEIPTS.md\nkind: receipt\nsource_commit: PENDING\nlaw4: attested\n---\n### mlv-app, 2026-10-19 - a measured fact\n\nBody.\n"),
    ("a heading that only mentions another candidate (no colon)",
     "---\ntarget: RECEIPTS.md\nkind: receipt\nsource_commit: PENDING\nlaw4: attested\n---\n### mlv-app, 2026-10-19 - measurement for ruling candidate x\n\nBody.\n"),
    ("a trap card",
     "---\ntarget: specs/mlv-app/cards.md\nkind: trap\nsource_commit: PENDING\nlaw4: attested\n---\n## mlv-app/a-trap\nrule: r\nmechanism: m\napplies: mlv-app\ncheck: c\nevidence: measured\n"),
])
def test_a_non_ruling_item_is_unaffected(tmp_path, capsys, monkeypatch, label, text):
    monkeypatch.setenv(ob.RULING_BUS_ENV, str(tmp_path / "no-such-clone"))   # an unreadable bus is never even looked at
    rc, err = validate(tmp_path, text, tmp_path / "no-such-clone", capsys)
    assert rc == 0 and err == "", (label, err)


def test_a_body_line_opening_ruling_candidate_marks_a_candidate(tmp_path, capsys):
    text = ("---\ntarget: RECEIPTS.md\nkind: receipt\nsource_commit: PENDING\nlaw4: attested\n---\n"
            "### mlv-app, 2026-10-19 - a hook cannot judge a thing\n\n"
            "**Ruling candidate (not law until ratified):** the hook is deny-only.\n")
    rc, err = validate(tmp_path, text, make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, err


# ---------- #469 r1's case, as a row ----------

def _fold_standard_text() -> str:
    return FOLD_STANDARD.read_text(encoding="utf-8")


def test_469_r1_the_merged_fold_standard_candidate_is_refused_as_written(tmp_path, capsys):
    """The item #469 merged carries a prose 'Relationship to existing doctrine' block but no `touches/supersedes:`
    list: the validator now stops it at authoring time, before the CoS has to find the overlap."""
    rc, err = validate(tmp_path, _fold_standard_text(), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, err


def _fold_standard_with(declaration: str) -> str:
    """The declaration REPLACES the prose 'Relationship to existing doctrine' block (the item is already near the
    Law-4 word cap, so adding the list beside the prose would be refused as LAW4_OVER_CAP)."""
    text = _fold_standard_text()
    marker = "**Relationship to existing doctrine**"
    assert marker in text
    assert "**Measured at bus" in text
    start, end = text.index(marker), text.index("**Measured at bus")
    return text[:start] + declaration + "\n" + text[end:]


FOLD_DECLARATION = ("touches/supersedes:\n"
                    "- RULINGS.md:1126-1152 (the 2026-08-30 one-sweep correction)\n"
                    "- R15\n- R14.8\n"
                    f"- {OPEN_CANDIDATE}\n"
                    "- tools/fleet-sweep.mjs\n- tools/doctrine-sync.mjs\n")


def test_469_r1_the_same_item_with_the_overlaps_named_is_accepted(tmp_path, capsys):
    rc, err = validate(tmp_path, _fold_standard_with(FOLD_DECLARATION), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", err


def test_469_r1_a_misnamed_open_candidate_is_refused_with_the_name_it_got_wrong(tmp_path, capsys):
    wrong = FOLD_DECLARATION.replace("-lag-is-derived-r1", "-lag-r1")
    rc, err = validate(tmp_path, _fold_standard_with(wrong), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_TARGET_NOT_ON_BUS" in err and "lag-r1.md" in err, err


# ---------- through main(): --bus and $FACTORY_BUS_CLONE ----------

def test_the_cli_takes_the_bus_from_the_flag_or_the_environment(tmp_path, capsys, monkeypatch):
    bus = make_bus(tmp_path)
    good = write_item(tmp_path, candidate("touches/supersedes: R15\n"), "20261019-ruling-candidate-good.md")
    bad = write_item(tmp_path, candidate("touches/supersedes: R99\n"), "20261019-ruling-candidate-bad.md")
    monkeypatch.delenv(ob.RULING_BUS_ENV, raising=False)
    assert ob.main(["validate", "--bus", str(bus), str(good)]) == 0
    assert ob.main(["validate", "--bus", str(bus), str(bad)]) == 1
    capsys.readouterr()
    monkeypatch.setenv(ob.RULING_BUS_ENV, str(bus))
    assert ob.main(["validate", str(good)]) == 0
    assert ob.main(["validate", str(bad)]) == 1
    assert "RULING_CANDIDATE_TARGET_NOT_ON_BUS" in capsys.readouterr().err
    monkeypatch.setenv(ob.RULING_BUS_ENV, str(tmp_path / "no-such-clone"))
    assert ob.main(["validate", str(good)]) == 2
    assert "RULING_BUS_UNREADABLE" in capsys.readouterr().err
