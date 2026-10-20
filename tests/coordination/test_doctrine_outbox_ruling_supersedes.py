"""A ruling candidate names what it touches or supersedes (card ROUTE-RULING-SUPERSEDES-1).

CoS work order FWO-DEFECT-PREVENT-1 item 4, seed #469 r1: a candidate that overlapped RULINGS.md:1126-1152, R15 and an
open candidate without saying so was found by the reviewer, not the producer. `validate` now refuses a ruling candidate
with no `touches/supersedes:` declaration, with an entry that is not a rule id or a bus path, or (given a bus clone)
with a target the bus does not hold at its tip. The bus here is always a scratch fixture, never the live one. Small
on purpose: the 2,900-line sibling suite takes more than ten minutes.
"""
from __future__ import annotations

import argparse
import importlib.util
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("doctrine_outbox_ruling_under_test", REPO_ROOT / "tools" / "coordination" / "doctrine_outbox.py")
ob = importlib.util.module_from_spec(_spec)
sys.modules["doctrine_outbox_ruling_under_test"] = ob
_spec.loader.exec_module(ob)

BUS_ENV = getattr(ob, "RULING_BUS_ENV", "FACTORY_BUS_CLONE")   # the baseline tool (before this card) has no such name


def call_validate(paths, bus):
    """cmd_validate with the signature of the tool that is loaded: the baseline at fork/master takes (paths, deny_terms)
    and no bus, so a red run shows that OLD validator accepting the bad items instead of dying on a TypeError."""
    if "bus" in inspect.signature(ob.cmd_validate).parameters:
        return ob.cmd_validate(paths, [], bus=bus)
    return ob.cmd_validate(paths, [])


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
    rc = call_validate([write_item(tmp_path, text)], bus)
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
        call_validate([write_item(tmp_path, candidate("touches/supersedes: R15\n"))], tmp_path / "no-such-clone")
    assert caught.value.code == "RULING_BUS_UNREADABLE"
    empty = tmp_path / "empty-bus"
    empty.mkdir()
    subprocess.run(["git", "init", "-q", str(empty)], check=True, capture_output=True, text=True)
    with pytest.raises(ob.Unknown):
        call_validate([write_item(tmp_path, candidate("touches/supersedes: R15\n"))], empty)


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
    monkeypatch.setenv(BUS_ENV, str(tmp_path / "no-such-clone"))   # an unreadable bus is never even looked at
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
    monkeypatch.delenv(BUS_ENV, raising=False)
    assert ob.main(["validate", "--bus", str(bus), str(good)]) == 0
    assert ob.main(["validate", "--bus", str(bus), str(bad)]) == 1
    capsys.readouterr()
    monkeypatch.setenv(BUS_ENV, str(bus))
    assert ob.main(["validate", str(good)]) == 0
    assert ob.main(["validate", str(bad)]) == 1
    assert "RULING_CANDIDATE_TARGET_NOT_ON_BUS" in capsys.readouterr().err
    monkeypatch.setenv(BUS_ENV, str(tmp_path / "no-such-clone"))
    assert ob.main(["validate", str(good)]) == 2
    assert "RULING_BUS_UNREADABLE" in capsys.readouterr().err


# ---------- marker variants: one normalizer for headings and body lines (sol r1 blocker, opus r1 hardening) ----------

NEUTRAL_HEADING = "### mlv-app, 2026-10-19 - a hook cannot judge a thing"
MARKER_LINES = [
    "- **Ruling candidate**",
    "> Ruling candidate:",
    "RULING CANDIDATE (r2): X",
    "RULING CANDIDATE - X",
    "1. ruling candidate",
    "*Ruling candidate*",
    "  + _Ruling-candidate_ (not law until ratified)",
    "> - **Ruling candidate (r3):** the hook is deny-only.",
]
HEADING_MARKERS = [
    "RULING CANDIDATE (r2): one standard for X",
    "RULING CANDIDATE - one standard for X",
    "*Ruling candidate*",
    "**Ruling candidate**",
    "Ruling candidate",
    "mlv-app, 2026-10-19 - RULING CANDIDATE: one standard for X",
    "mlv-app, 2026-10-19 - Ruling-candidate (r2): one standard for X",
]


def body_with(marker_line: str, declaration: str = "") -> str:
    return candidate(f"{marker_line}\n\n{declaration}", heading=NEUTRAL_HEADING)


@pytest.mark.parametrize("marker", MARKER_LINES)
def test_every_body_line_marker_variant_makes_a_candidate_that_is_refused_bare(tmp_path, capsys, marker):
    rc, err = validate(tmp_path, body_with(marker), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (marker, err)


@pytest.mark.parametrize("marker", MARKER_LINES)
def test_every_body_line_marker_variant_is_accepted_with_a_declaration(tmp_path, capsys, marker):
    rc, err = validate(tmp_path, body_with(marker, "touches/supersedes: R15\n"), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", (marker, err)


@pytest.mark.parametrize("heading", HEADING_MARKERS)
def test_every_heading_marker_variant_makes_a_candidate_that_is_refused_bare(tmp_path, capsys, heading):
    rc, err = validate(tmp_path, candidate(heading=f"### {heading}"), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (heading, err)


@pytest.mark.parametrize("sub_heading", ["## RULING CANDIDATE: X", "# Ruling candidate (r2) - X", "#### **Ruling candidate**", "### mlv-app - RULING CANDIDATE: X"])
def test_a_later_heading_line_marker_makes_a_candidate_that_is_refused_bare(tmp_path, capsys, sub_heading):
    rc, err = validate(tmp_path, body_with(sub_heading), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (sub_heading, err)


@pytest.mark.parametrize("heading", HEADING_MARKERS)
def test_every_heading_marker_variant_is_accepted_with_a_declaration(tmp_path, capsys, heading):
    rc, err = validate(tmp_path, candidate("touches/supersedes: R15\n", heading=f"### {heading}"), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", (heading, err)


@pytest.mark.parametrize("line", [
    "Nothing here is a ruling candidate; it only cites one.",
    "This is not a **ruling candidate** at all.",
    "> quoted: see the ruling candidate on the bus",
    "- measured against the ruling candidate named above",
    "A ruling-candidate check is what this entry measures.",
])
def test_a_line_that_only_mentions_the_phrase_mid_sentence_is_not_a_marker(tmp_path, capsys, line):
    rc, err = validate(tmp_path, body_with(line), tmp_path / "no-such-clone", capsys)
    assert rc == 0 and err == "", (line, err)


def test_a_marker_line_inside_a_fence_still_counts_because_the_check_fails_closed(tmp_path, capsys):
    rc, err = validate(tmp_path, body_with("```\nRuling candidate: X\n```"), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, err


# ---------- the bus template's own field is an alias of touches/supersedes ----------

@pytest.mark.parametrize("declaration", [
    "Relationship to existing doctrine: R15, tools/fleet-sweep.mjs\n",
    "relationship to existing doctrine: RULINGS.md:1126-1152; " + OPEN_CANDIDATE + "\n",
    "- **Relationship to existing doctrine:** R15.3\n",
    "**Relationship to existing doctrine**: R14.8\n",
    "Relationship to existing doctrine:\n- R15\n- tools/doctrine-sync.mjs\n",
    "Relationship to existing doctrine: none: it overlaps no standing rule or open candidate, checked by grep\n",
    "touches: R15\nRelationship to existing doctrine: tools/fleet-sweep.mjs\n",
])
def test_the_relationship_to_existing_doctrine_alias_is_accepted(tmp_path, capsys, declaration):
    rc, err = validate(tmp_path, candidate(declaration), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", (declaration, err)


@pytest.mark.parametrize("declaration", [
    "Relationship to existing doctrine:\n",
    "- **Relationship to existing doctrine:**\n",
    "Relationship to existing doctrine: none\n",
    "Relationship to existing doctrine (see the bus template) without a colon list\n",
    "**Relationship to existing doctrine** (`ruling-candidates-name-what-they-supersede-r1`): prose, no list\n",
])
def test_the_alias_without_a_list_is_refused(tmp_path, capsys, declaration):
    rc, err = validate(tmp_path, candidate(declaration), make_bus(tmp_path), capsys)
    assert rc == 1 and ("RULING_CANDIDATE_NO_SUPERSEDES" in err or "RULING_CANDIDATE_NONE_NEEDS_REASON" in err), (declaration, err)


def test_the_alias_keeps_the_target_rules(tmp_path, capsys):
    bus = make_bus(tmp_path)
    rc, err = validate(tmp_path, candidate("Relationship to existing doctrine: R15, R99\n"), bus, capsys)
    assert rc == 1 and "RULING_CANDIDATE_TARGET_NOT_ON_BUS" in err and "'R99'" in err, err
    rc, err = validate(tmp_path, candidate("Relationship to existing doctrine: fleet-sweep\n"), bus, capsys)
    assert rc == 1 and "RULING_CANDIDATE_TARGET_MALFORMED" in err, err


# ---------- one --bus on validate (shared with the route exercise since CARD-ROUTE-EXERCISE-3 m1) ----------

def _subcommand_parsers():
    """name -> subparser, for every subcommand build_parser() defines."""
    parser = ob.build_parser()
    subparsers = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    assert len(subparsers) == 1
    return subparsers[0].choices


def test_build_parser_succeeds_and_defines_bus_exactly_once_on_validate():
    choices = _subcommand_parsers()
    options = [opt for action in choices["validate"]._actions for opt in action.option_strings]
    assert options.count("--bus") == 1, options
    parsed = ob.build_parser().parse_args(["validate", "--bus", "some-clone", "item.md"])
    assert parsed.bus == Path("some-clone") and parsed.items == [Path("item.md")]


def test_no_subcommand_defines_any_option_string_twice():
    for name, sub in _subcommand_parsers().items():
        options = [opt for action in sub._actions for opt in action.option_strings]
        assert len(options) == len(set(options)), (name, options)


# ---------- r3: marker detection is Unicode-normalised and reads only where a block opens ----------
# Sol r2 (10 of 22 marker probes bypassed, executed) and opus r2 (plain bus prose falsely refused; more bypasses).

# every probe below was ACCEPTED bare by the 096d7493 tool and must now be refused; each goes where its shape belongs
# (a body line after a blank line, or the item's first heading)
SOL_R2_BODY_BYPASSES = [
    "Ruling–candidate: X",                       # en dash between the words
    "Ruling—candidate: X",                       # em dash
    "Rul​ing candidate: X",                      # zero-width space inside a word
    '"Ruling candidate (r2): X"',                     # straight quotes
    "> ### proj - RULING CANDIDATE (r2): X",          # blockquoted heading
    "- ### proj - RULING CANDIDATE (r2): X",          # bulleted heading
    "### proj：RULING CANDIDATE (r2): X",         # full-width colon as the separator
    "proj - RULING CANDIDATE (r2): X\n---",           # setext heading with a prefix
]
OPUS_R2_BODY_BYPASSES = [
    '### "RULING CANDIDATE": X',                      # straight-quoted phrase
    "### “RULING CANDIDATE”: X",            # curly-quoted phrase
    "### ​RULING CANDIDATE (r2): X",             # zero-width space before the phrase
    " Ruling candidate: x",                      # no-break space before the phrase
    "> ### proj - RULING CANDIDATE: X",
    "proj, 2026-10-19 - RULING CANDIDATE: X\n===",    # setext (===) with a prefix, later in the body
    "**Proposed ruling candidate:** x",               # qualified
    "[RULING CANDIDATE] x",                           # bracketed
]
OTHER_BODY_BYPASSES = [
    "Rul­ing candidate: X",                      # soft hyphen (Cf) inside a word
    "Ruling−candidate: X",                       # minus sign
    "RULING‑CANDIDATE: x",                       # non-breaking hyphen
    "Ruling candidate (r2): X",                  # no-break space between the words
    "﻿Ruling candidate: x",                      # byte-order mark
    "　Ruling candidate: x",                      # ideographic space
    "＞ Ruling candidate: x",                     # full-width >
    "Ｒuling candidate: x",                       # full-width R
    "> > Ruling candidate: x",                        # nested blockquote
    "Draft ruling candidate: x",
    "New ruling candidate - x",
    "2) ruling candidate",
    "“Ruling candidate”",
    "'Ruling candidate' x",
    "> - **Proposed Ruling-candidate (r3):** x",
    "Ruling candidate (r2) - X\n---",
]
BODY_BYPASSES = SOL_R2_BODY_BYPASSES + OPUS_R2_BODY_BYPASSES + OTHER_BODY_BYPASSES
HEADING_BYPASSES = [
    '"RULING CANDIDATE": X',
    "“RULING CANDIDATE”: X",
    "​RULING CANDIDATE (r2): X",
    " RULING CANDIDATE: X",
    "proj：RULING CANDIDATE: X",
    "proj—RULING CANDIDATE: X",
    "mlv-app, 2026-10-19 − RULING CANDIDATE (r2): X",
    "[RULING CANDIDATE] X",
    "Proposed ruling candidate: X",
]


@pytest.mark.parametrize("marker", BODY_BYPASSES)
def test_r3_every_normalised_body_marker_is_refused_bare(tmp_path, capsys, marker):
    rc, err = validate(tmp_path, body_with(marker), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (marker, err)


@pytest.mark.parametrize("marker", BODY_BYPASSES)
def test_r3_every_normalised_body_marker_is_accepted_with_a_declaration(tmp_path, capsys, marker):
    rc, err = validate(tmp_path, body_with(marker, "touches/supersedes: R15\n"), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", (marker, err)


@pytest.mark.parametrize("heading", HEADING_BYPASSES)
def test_r3_every_normalised_heading_marker_is_refused_bare(tmp_path, capsys, heading):
    rc, err = validate(tmp_path, candidate(heading=f"### {heading}"), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (heading, err)


@pytest.mark.parametrize("heading", HEADING_BYPASSES)
def test_r3_every_normalised_heading_marker_is_accepted_with_a_declaration(tmp_path, capsys, heading):
    rc, err = validate(tmp_path, candidate("touches/supersedes: R15\n", heading=f"### {heading}"), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", (heading, err)


def _receipt(body: str) -> str:
    return f"---\ntarget: RECEIPTS.md\nkind: receipt\nsource_commit: PENDING\nlaw4: attested\n---\n{body}"


# Lines copied VERBATIM from the doctrine bus (read only): plain prose that names ruling-candidates/ or the phrase, wrapped
# by an author who hard-wraps. The 096d7493 tool refused every one of these with RULING_CANDIDATE_NO_SUPERSEDES.
BUS_RECEIPTS_3876_3879 = (
    "With the filing now `HARVESTED / open=0`, the steward's `open>0` trigger can never re-fire, so 20 dispositioned\n"
    "findings count zero toward §5 permanently. Only `kernel-e2e.py` sees it (`filed_but_unrowed`, exit 1);\n"
    "`harvest-status.py` and `arbitration-queue.py` both read clean. Predicate fix proposed in\n"
    "`ruling-candidates/steward-filing-has-no-legal-row-writer-r1.md`.\n")
BUS_RECEIPTS_3375_3378 = (
    "3. **Our flagship negative is internally consistent but not third-party re-measurable.** Both arms ran\n"
    "   against a base engine that lives outside the repository and is unpinned in the ledger. We filed a\n"
    "   ruling candidate titled *filing-evidence-must-be-re-measurable* in the same push. Pin the engine\n"
    "   digest in the binding, or the finding is ours alone — exactly the defect we asked the steward to fix.\n")
BUS_TRAPS_9722_9725 = (
    "**Corroboration that the qualified form is the durable one:** the evidence in this board's own\n"
    "ruling-candidate deliberately avoided PR numbers and used `git ls-remote` instead - eight kernel review\n"
    "refs, none belonging to AdversarialLLM. That claim survived the correction unchanged, while the\n"
    "PR-numbered one did not.\n")
BUS_RECEIPTS_1293_1295 = (
    "- This receipt grants no fleet adoption, review credit, landing, or ruling\n"
    "  authority. It records what was measured on this box. Companion candidate:\n"
    "  `ruling-candidates/orchestrator-seat-fit-r1.md`.\n")
BUS_HEADING_12698 = ("### mlv-app, 2026-10-06 — an MLV measurement for ruling candidate mechanical-outbox-every-member-r1 (bus 2877eff): "
                     "merged outbox items waited 23.7 h and 49.5 h because only a session ran the drain; the hourly unattended drain is now adopted")
BUS_HEADING_12951 = ("### airmypc, 2026-10-08 — ADOPT of ruling candidate mechanical-outbox-every-member-r1 (all three parts), and the heartbeat "
                     "record has no slot for the debt that part 3 and fold-lag F4 ask it to carry")
BUS_PRIOR_ART_12953 = ("**Prior art:** ruling-candidates/mechanical-outbox-every-member-r1.md listed airmypc as exporting by direct, hand-written "
                       "commits \"only when a session remembers\".")

FALSIFIER_BODIES = [
    ("a wrapped line that opens with a ruling-candidates/ path (bus RECEIPTS 3876-3879)",
     "### proj, 2026-10-19 - a measured fact\n\n" + BUS_RECEIPTS_3876_3879),
    ("a wrapped numbered item whose line opens 'ruling candidate titled' (bus RECEIPTS 3375-3378)",
     "### proj, 2026-10-19 - a measured fact\n\n" + BUS_RECEIPTS_3375_3378),
    ("a wrapped paragraph whose line opens 'ruling-candidate deliberately avoided' (bus TRAPS 9722-9725)",
     "### proj, 2026-10-19 - a measured fact\n\n" + BUS_TRAPS_9722_9725),
    ("a bullet that wraps onto a ruling-candidates/ path (bus RECEIPTS 1293-1295)",
     "### proj, 2026-10-19 - a measured fact\n\n" + BUS_RECEIPTS_1293_1295),
    ("a bullet that is only a ruling-candidates/ path", "### proj, 2026-10-19 - a measured fact\n\n- `ruling-candidates/x.md`\n"),
    ("a bullet that is a longer ruling-candidates/ path",
     "### proj, 2026-10-19 - a measured fact\n\n- `ruling-candidates/steward-filing-has-no-legal-row-writer-r1.md`\n"),
    ("a paragraph that opens with the plural", "### proj, 2026-10-19 - a measured fact\n\nruling candidates are filed under ruling-candidates/ by the steward.\n"),
    ("a paragraph that opens with a slug", "### proj, 2026-10-19 - a measured fact\n\nRuling-candidate-check ran green on this box.\n"),
    ("a paragraph that names the directory first", "### proj, 2026-10-19 - a measured fact\n\nruling-candidates/ holds the open proposals.\n"),
    ("a heading that measures someone else's candidate, em dash separator (bus RECEIPTS 12698)", BUS_HEADING_12698 + "\n\nBody.\n"),
    ("an ADOPT heading and its Prior art line (bus RECEIPTS 12951, 12953)", BUS_HEADING_12951 + "\n\n" + BUS_PRIOR_ART_12953 + "\n"),
    ("a quoted slug inside a heading", "### proj, 2026-10-19 - the `outbox-ruling-candidate-check` trap\n\nBody.\n"),
]


@pytest.mark.parametrize("label, body", FALSIFIER_BODIES, ids=[label for label, _ in FALSIFIER_BODIES])
def test_r3_plain_bus_prose_that_names_the_phrase_is_not_a_candidate(tmp_path, capsys, label, body):
    rc, err = validate(tmp_path, _receipt(body), tmp_path / "no-such-clone", capsys)
    assert rc == 0 and err == "", (label, err)


def test_r3_a_trap_card_headed_by_a_ruling_candidate_slug_is_not_a_candidate(tmp_path, capsys):
    text = ("---\ntarget: specs/mlv-app/cards.md\nkind: trap\nsource_commit: PENDING\nlaw4: attested\n---\n"
            "## mlv-app/outbox-ruling-candidate-check\nrule: r\nmechanism: m\napplies: mlv-app\ncheck: c\nevidence: measured\n")
    rc, err = validate(tmp_path, text, tmp_path / "no-such-clone", capsys)
    assert rc == 0 and err == "", err


def test_r3_a_marker_opens_a_block_so_the_same_words_after_a_blank_line_are_refused(tmp_path, capsys):
    """The wrapped line of a paragraph is not a marker; the same words starting a paragraph are."""
    wrapped = "### proj, 2026-10-19 - a measured fact\n\nWe filed a\nruling candidate titled x in the same push.\n"
    opening = "### proj, 2026-10-19 - a measured fact\n\nWe filed a thing.\n\nruling candidate titled x in the same push.\n"
    rc, err = validate(tmp_path, _receipt(wrapped), tmp_path / "no-such-clone", capsys)
    assert rc == 0 and err == "", err
    rc, err = validate(tmp_path, _receipt(opening), tmp_path / "no-such-clone", capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, err


def test_r3_a_quote_or_list_line_opens_a_block_even_without_a_blank_line_before_it(tmp_path, capsys):
    for line in ("> Ruling candidate: x", "- ruling candidate: x", "1. Ruling candidate: x"):
        text = _receipt("### proj, 2026-10-19 - a measured fact\n\nSome text that runs\n" + line + "\n")
        rc, err = validate(tmp_path, text, tmp_path / "no-such-clone", capsys)
        assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (line, err)


# ---------- the alias takes a verb per entry (the bus candidate template's rule 1) ----------

@pytest.mark.parametrize("declaration", [
    "Relationship to existing doctrine: extends R15; supersedes RULINGS.md:1126-1152\n",
    "Relationship to existing doctrine: distinguishes R14.8, narrows tools/fleet-sweep.mjs\n",
    "Relationship to existing doctrine: Adopts R15.3; Touches specs/mlv-app/cards.md\n",
    "Relationship to existing doctrine:\n- extends R15\n- supersedes " + OPEN_CANDIDATE + "\n",
    "touches/supersedes: extends R15 (the reference mechanism); R14.8\n",
    "Relationship to existing doctrine: R15 (extends); supersedes R14.8\n",
])
def test_r3_a_leading_verb_per_entry_is_accepted_and_the_target_still_checked(tmp_path, capsys, declaration):
    rc, err = validate(tmp_path, candidate(declaration), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", (declaration, err)


@pytest.mark.parametrize("declaration, code, named", [
    ("Relationship to existing doctrine: extends R99\n", "RULING_CANDIDATE_TARGET_NOT_ON_BUS", "'extends R99'"),
    ("Relationship to existing doctrine: extends R15; supersedes specs/mlv-app/nope.md\n", "RULING_CANDIDATE_TARGET_NOT_ON_BUS", "nope.md"),
    ("Relationship to existing doctrine: extends fleet-sweep\n", "RULING_CANDIDATE_TARGET_MALFORMED", "fleet-sweep"),
    ("Relationship to existing doctrine: extends\n", "RULING_CANDIDATE_TARGET_MALFORMED", "'extends'"),
    ("Relationship to existing doctrine: replaces R15\n", "RULING_CANDIDATE_TARGET_MALFORMED", "replaces R15"),
])
def test_r3_a_verb_does_not_excuse_a_bad_target(tmp_path, capsys, declaration, code, named):
    rc, err = validate(tmp_path, candidate(declaration), make_bus(tmp_path), capsys)
    assert rc == 1 and code in err and named in err, (declaration, err)


# ---------- every subcommand still parses (a second --bus raises for all of them, the push hook included) ----------

SUBCOMMAND_ARGV = {
    "validate": ["validate", "item.md"],
    "debt": ["debt"],
    "drain": ["drain", "--bus", "bus-clone"],
    "kernel-filing": ["kernel-filing", "--bus", "bus-clone"],
    "check-ledger": ["check-ledger"],
    "check-commits": ["check-commits"],
    "check-range": ["check-range"],
    "check": ["check"],
    "pre-push": ["pre-push"],
    "stop": ["stop"],
    "session-start": ["session-start"],
    "install-hooks": ["install-hooks"],
}


def test_r3_the_argv_table_covers_every_subcommand_the_parser_defines():
    assert set(SUBCOMMAND_ARGV) == set(_subcommand_parsers()), sorted(set(SUBCOMMAND_ARGV) ^ set(_subcommand_parsers()))


@pytest.mark.parametrize("name", sorted(SUBCOMMAND_ARGV))
def test_r3_parse_args_succeeds_for_every_subcommand(name):
    parsed = ob.build_parser().parse_args(SUBCOMMAND_ARGV[name])
    assert parsed.cmd == name


def test_r3_the_validate_bus_help_names_no_flag_this_tool_does_not_have():
    # NA-6 (hub ruling at the #515 r4 line 15:11:38Z, applied in CARD-ROUTE-EXERCISE-3 m1): the merged tool has
    # `--exercise`, so the help may name it; what it may not do is name a flag `validate` lacks.
    validate_actions = _subcommand_parsers()["validate"]._actions
    bus_help = [a.help for a in validate_actions if "--bus" in a.option_strings][0]
    options = {opt for a in validate_actions for opt in a.option_strings}
    assert "second add_argument" not in bus_help, bus_help
    assert [a for a in validate_actions if "--exercise" in a.option_strings]
    assert {flag for flag in re.findall(r"--[a-z][a-z-]*", bus_help)} <= options, bus_help


# ---------- r4: block-opening is also a property of the PREVIOUS line; invisible characters; render before the test ----------
# Opus r3 (a marker directly under a heading, setext underline, thematic break or closing fence) and sol r3 (a zero-width
# space BETWEEN the words; table cell, &nbsp;, &#160; and <strong> forms). Exotic forms move to ROUTE-RULING-MARKUP-FORMS-1.

R4_MARKER = "**Ruling candidate (not law until ratified):** the hook is deny-only."
R4_PREDECESSORS = [
    ("an ATX heading", NEUTRAL_HEADING + "\n"),
    ("a closed ATX heading", "### A title ###\n"),
    ("a setext heading over ===", "A title\n===\n"),
    ("a setext heading over ---", "A title\n---\n"),
    ("a thematic break of dashes", "Some text.\n\n---\n"),
    ("a thematic break of stars", "Some text.\n\n***\n"),
    ("a thematic break of underscores", "Some text.\n\n___\n"),
    ("a closing backtick fence", "```\ncode\n```\n"),
    ("a closing tilde fence", "~~~\ncode\n~~~\n"),
    ("a closing backtick fence with an info string above it", "```python\ncode\n```\n"),
    ("a closing long fence", "````\ncode\n````\n"),
    ("a closing tilde fence that holds a backtick fence line", "~~~\n```\n~~~\n"),
]


def after_predecessor(predecessor: str, marker: str, declaration: str = "") -> str:
    """The marker sits on the VERY NEXT line after `predecessor`, no blank line between; a heading comes first so the
    item's own first line is neutral."""
    head = "" if predecessor.startswith(("#", NEUTRAL_HEADING)) else NEUTRAL_HEADING + "\n\n"
    return _receipt(f"{head}{predecessor}{marker}\n\n{declaration}Rules: S1 one thing.\n")


@pytest.mark.parametrize("label, predecessor", R4_PREDECESSORS, ids=[label for label, _ in R4_PREDECESSORS])
def test_r4_a_marker_on_the_line_right_after_a_block_ending_line_is_refused_bare(tmp_path, capsys, label, predecessor):
    rc, err = validate(tmp_path, after_predecessor(predecessor, R4_MARKER), make_bus(tmp_path), capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (label, err)


@pytest.mark.parametrize("label, predecessor", R4_PREDECESSORS, ids=[label for label, _ in R4_PREDECESSORS])
def test_r4_a_marker_on_the_line_right_after_a_block_ending_line_is_accepted_with_a_declaration(tmp_path, capsys, label, predecessor):
    rc, err = validate(tmp_path, after_predecessor(predecessor, R4_MARKER, "touches/supersedes: R15\n"), make_bus(tmp_path), capsys)
    assert rc == 0 and err == "", (label, err)


def test_r4_a_wrapped_line_after_a_properly_closed_mixed_fence_is_still_not_a_marker(tmp_path, capsys):
    """A backtick line inside a tilde fence does not close it (character and length must match), so the fence ends at the
    second tilde line and the wrapped line that follows its paragraph start is prose, not a marker."""
    body = NEUTRAL_HEADING + "\n\n~~~\n```\n~~~\nWe filed a\nruling candidate titled x in the same push.\n"
    rc, err = validate(tmp_path, _receipt(body), tmp_path / "no-such-clone", capsys)
    assert rc == 0 and err == "", err


def test_r4_prose_directly_under_a_heading_that_does_not_open_with_the_phrase_is_not_a_candidate(tmp_path, capsys):
    for text in ("### proj, 2026-10-19 - a measured fact\nWe filed a\nruling candidate titled x in the same push.\n",
                 "### proj, 2026-10-19 - a measured fact\nSee ruling-candidates/x.md for the proposal.\n",
                 "### proj, 2026-10-19 - a measured fact\n---\nSee ruling-candidates/x.md for the proposal.\n"):
        rc, err = validate(tmp_path, _receipt(text), tmp_path / "no-such-clone", capsys)
        assert rc == 0 and err == "", (text, err)


# invisible characters between the words must not join them (sol r3 blocker 1: U+200B between the words)
R4_INVISIBLE_BODY = [
    "Ruling​candidate: X",           # zero-width space
    "Ruling­candidate: X",           # soft hyphen
    "Ruling⁠candidate: X",           # word joiner
    "Ruling﻿candidate: X",           # byte-order mark
    "Ruling​​candidate: X",     # two of them
    "RULING​CANDIDATE (r2): X",
    "**Ruling​candidate:** X",
    "Ruling ​candidate: X",          # beside a plain space
    "Ruling​-candidate: X",          # beside a hyphen
]
R4_INVISIBLE_HEADING = ["Ruling​candidate (r2): X", "proj - Ruling­candidate: X", "proj, 2026-10-19 - RULING​CANDIDATE: X"]


@pytest.mark.parametrize("marker", R4_INVISIBLE_BODY)
def test_r4_an_invisible_character_between_the_words_does_not_hide_a_body_marker(tmp_path, capsys, marker):
    bus = make_bus(tmp_path)
    rc, err = validate(tmp_path, body_with(marker), bus, capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (marker, err)
    rc, err = validate(tmp_path, body_with(marker, "touches/supersedes: R15\n"), bus, capsys)
    assert rc == 0 and err == "", (marker, err)


@pytest.mark.parametrize("heading", R4_INVISIBLE_HEADING)
def test_r4_an_invisible_character_between_the_words_does_not_hide_a_heading_marker(tmp_path, capsys, heading):
    bus = make_bus(tmp_path)
    rc, err = validate(tmp_path, candidate(heading=f"### {heading}"), bus, capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (heading, err)
    rc, err = validate(tmp_path, candidate("touches/supersedes: R15\n", heading=f"### {heading}"), bus, capsys)
    assert rc == 0 and err == "", (heading, err)


@pytest.mark.parametrize("line", [
    "rulingcandidate/x.md holds the open proposals.",
    "rulingcandidates are filed by the steward.",
    "rulingcandidate-check ran green on this box.",
    "Ruling​candidates are filed under ruling-candidates/ by the steward.",
    "Ruling​candidate-check ran green on this box.",
    "Ruling​candidate/ holds the open proposals.",
    "Ruling​-candidates/x.md holds the open proposals.",
])
def test_r4_a_single_word_slug_path_or_plural_is_still_not_a_marker(tmp_path, capsys, line):
    rc, err = validate(tmp_path, _receipt(f"{NEUTRAL_HEADING}\n\n{line}\n"), tmp_path / "no-such-clone", capsys)
    assert rc == 0 and err == "", (line, err)


# rendered forms (sol r3 blocker 2): table cell, entities, tags, and combining marks are rendered before the marker test
R4_RENDERED_BODY = [
    "| Ruling candidate: X |\n| --- |\n| detail |",       # sol r3
    "Ruling&nbsp;candidate: X",                            # sol r3
    "Ruling&#160;candidate: X",                            # sol r3
    "<strong>Ruling candidate:</strong> X",                # sol r3
    "Ruling&#x00A0;candidate: X",
    "Ruling&ensp;candidate: X",
    "&#82;uling candidate: X",                             # a numeric entity for the R
    "<b>Ruling</b> candidate: X",
    "<b>Ruling</b><i> candidate</i>: X",
    '<span class="status">Ruling candidate</span>: X',
    "<p>Ruling candidate: X</p>",
    "<strong>Ruling&nbsp;candidate</strong>: X",
    "| Status | Ruling candidate: X |\n| --- | --- |",    # a later cell opens a block
    "| Item | Note |\n| --- | --- |\n| 1 | Ruling candidate: X |",
    "| 1 | Ruling candidate: X |",
    "a | Ruling candidate: X",                             # no leading pipe
    "| **Ruling candidate:** X |",
    "Rúling candidate: X",                           # combining acute on the u
    "Rulïng candidate: X",                            # precomposed i-diaeresis
    "Ruling candidáte: X",
    "Ru&#x301;ling candidate: X",                          # an entity for the combining mark
]
R4_RENDERED_HEADING = ["<strong>Ruling candidate</strong>: X", "proj - Ruling&nbsp;candidate: X", "Ruling&#160;candidate (r2): X",
                       "| Ruling candidate: X |", "proj, 2026-10-19 - <b>RULING CANDIDATE</b>: X"]


@pytest.mark.parametrize("marker", R4_RENDERED_BODY)
def test_r4_a_rendered_body_marker_is_refused_bare_and_accepted_with_a_declaration(tmp_path, capsys, marker):
    bus = make_bus(tmp_path)
    rc, err = validate(tmp_path, body_with(marker), bus, capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (marker, err)
    rc, err = validate(tmp_path, body_with(marker, "touches/supersedes: R15\n"), bus, capsys)
    assert rc == 0 and err == "", (marker, err)


@pytest.mark.parametrize("heading", R4_RENDERED_HEADING)
def test_r4_a_rendered_heading_marker_is_refused_bare_and_accepted_with_a_declaration(tmp_path, capsys, heading):
    bus = make_bus(tmp_path)
    rc, err = validate(tmp_path, candidate(heading=f"### {heading}"), bus, capsys)
    assert rc == 1 and "RULING_CANDIDATE_NO_SUPERSEDES" in err, (heading, err)
    rc, err = validate(tmp_path, candidate("touches/supersedes: R15\n", heading=f"### {heading}"), bus, capsys)
    assert rc == 0 and err == "", (heading, err)


@pytest.mark.parametrize("label, body", [
    ("a wrapped line whose phrase is joined by an entity", f"{NEUTRAL_HEADING}\n\nWe filed a\nruling&nbsp;candidate titled x in the same push.\n"),
    ("a wrapped line that opens a tag around the phrase", f"{NEUTRAL_HEADING}\n\nWe filed a\n<b>ruling candidate</b> titled x in the same push.\n"),
    ("a mention mid-sentence in a table cell", f"{NEUTRAL_HEADING}\n\n| Item | Note |\n| --- | --- |\n| 1 | the ruling candidate titled x was filed |\n"),
    ("a comparison that is not a tag", f"{NEUTRAL_HEADING}\n\nIf a < b and c > d then the ruling candidate titled x stands.\n"),
    ("a pipe in prose", f"{NEUTRAL_HEADING}\n\nUse the a|b form; see ruling-candidates/x.md.\n"),
    ("a path with a combining accent", f"{NEUTRAL_HEADING}\n\nSee ruling-candidates/x.md and étude notes.\n"),
])
def test_r4_rendering_does_not_make_plain_prose_a_candidate(tmp_path, capsys, label, body):
    rc, err = validate(tmp_path, _receipt(body), tmp_path / "no-such-clone", capsys)
    assert rc == 0 and err == "", (label, err)
