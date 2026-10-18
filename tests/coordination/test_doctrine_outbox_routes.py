"""Tests for `doctrine_outbox.py validate --exercise` (card routes) and the ruling-candidate
`touches/supersedes:` refusal. CoS work order FWO-DEFECT-PREVENT-1 items 3 and 4 (MLV-App PR 454
comment 6227522231; hub ACK 6228165753). Every repository is a throwaway git repository under
pytest's tmp_path; nothing touches the network, the real bus or its clone. Lives under
tests/coordination/ because CI runs that directory by name.

Seed RED rows come from the failures the CoS found by hand:
  #468 r3/r4  an adoption step `python factory-pins/pin-runner.py pin ...` against a bus without that file
  #469 r1     `doctrine-sync debt` named as the route, a command no bus carried, and a ruling candidate
              that said nothing about the rules it overlaps

Named mutations (each turns the named test red, then is reverted):
  M1  drop the `script not in the clone` check in route_missing_reason  -> test_seed_468_missing_runner_script_is_route_missing
  M2  run routes in the bus instead of the scratch clone                -> test_routes_never_run_in_the_bus_and_the_scratch_is_removed
  M3  let screen_ruling_candidate return early                          -> test_seed_469_ruling_candidate_without_the_list_is_refused
"""
from __future__ import annotations

import importlib.util
import io
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("doctrine_outbox_routes_under_test", REPO_ROOT / "tools" / "coordination" / "doctrine_outbox.py")
ob = importlib.util.module_from_spec(_spec)
sys.modules["doctrine_outbox_routes_under_test"] = ob
_spec.loader.exec_module(ob)

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]


@pytest.fixture(autouse=True)
def _pin_runner_identity(monkeypatch):
    monkeypatch.setattr(ob.socket, "gethostname", lambda: "outboxtestrunner")
    for name in ("COMPUTERNAME", "USERNAME", "USER"):
        monkeypatch.setenv(name, "outboxtestacct")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\outboxtestacct")
    monkeypatch.setenv("HOME", "C:\\Users\\outboxtestacct")
    monkeypatch.delenv(ob.ROUTE_BUS_ENV, raising=False)


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


STUB_OK = "import sys\nsys.exit(0)\n"
STUB_DOCTRINE_SYNC_NO_DEBT = (
    "import sys\n"
    "if sys.argv[1:2] in (['check'], ['ack']):\n"
    "    sys.exit(0)\n"
    "print('unknown command', sys.argv[1:], file=sys.stderr)\n"
    "sys.exit(2)\n"
)
STUB_DOCTRINE_SYNC_WITH_DEBT = STUB_DOCTRINE_SYNC_NO_DEBT.replace("(['check'], ['ack'])", "(['check'], ['ack'], ['debt'])")
STUB_SLOW = "import time\ntime.sleep(30)\n"
STUB_TOUCH = "import pathlib\npathlib.Path('touched.txt').write_text('x')\n"
STUB_NO_ORIGIN = (
    "import subprocess, sys\n"
    "out = subprocess.run(['git', 'remote'], capture_output=True, text=True).stdout.strip()\n"
    "sys.exit(1 if out else 0)\n"
)
STUB_MARK = "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('ran')\n"


def make_bus(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    bus = tmp_path / "bus"
    bus.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(bus)], check=True, capture_output=True, text=True)
    for rel, text in {"README.md": "bus\n", **(files or {})}.items():
        path = bus / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(bus, "add", "-A")
    git(bus, "commit", "-q", "-m", "scratch bus")
    return bus


def item_file(tmp_path: Path, body: str, name: str = "20261017-route-fixture.md", target: str = "RECEIPTS.md",
              kind: str = "receipt") -> Path:
    folder = tmp_path / "items"
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text(f"---\ntarget: {target}\nkind: {kind}\nsource_commit: PENDING\nlaw4: attested\n---\n{body}", encoding="utf-8")
    return path


def exercise(tmp_path: Path, item: Path, bus: Path | None, *extra: str, scratch: Path | None = None) -> int:
    argv = ["--repo", str(tmp_path), "validate", str(item), "--exercise", "--scratch-root", str(scratch or tmp_path / "scratch")]
    if bus is not None:
        argv += ["--bus", str(bus)]
    return ob.main([*argv, *extra])


def route_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.split(" ", 1)[0] in ob.ROUTE_STATUSES]


def statuses(out: str) -> list[str]:
    return [line.split(" ", 1)[0] for line in route_lines(out)]


def adopt(*commands: str) -> str:
    return "### mlv-app, 2026-10-17 - a route fixture\n\n- **Adopt:** do these.\n" + "".join(f"- **Step:** run `{c}`\n" for c in commands)


# ---------- extraction ----------

def test_extraction_rule():
    body = (
        "### heading\n\n"
        "Prose with `python outside.py run` in it is not a route.\n\n"
        "- **Adopt:** the steward runs `python a.py one` first.\n"
        "- **Pin:** then `git log` and the name `debt` and the file `RULINGS.md`, and `python a.py one` again.\n"
        "\n"
        "After the blank line `python after.py x` is prose again.\n\n"
        "```route\n$ python b.py two\n# a comment\n\n> python c.py three\n```\n\n"
        "```\npython fenced.py plain\n```\n\n"
        "Route: `python d.py four`\n"
        "## A heading closes the section\n"
        "`python e.py five`\n"
    )
    assert ob.extract_routes(body) == ["python a.py one", "git log", "python b.py two", "python c.py three", "python d.py four"]


def test_route_label_forms():
    for label in ("Route:", "- Route:", "**Route:**", "- **Adopt:**", "* adopt:", "**Adopt**:"):
        assert ob.extract_routes(f"### h\n\n{label} run `python x.py go`\n") == ["python x.py go"], label
    assert ob.extract_routes("### h\n\nRouted: run `python x.py go`\n") == []


# ---------- the five outcomes, RED and GREEN ----------

def test_seed_468_missing_runner_script_is_route_missing(tmp_path, capsys):
    """#468: the card's adopt step runs factory-pins/pin-runner.py; the bus keeps it at tools/factory/."""
    bus = make_bus(tmp_path, {"tools/factory/pin-runner.py": STUB_OK})
    cmd = "python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --reviewer <who> --reason <why>"
    item = item_file(tmp_path, adopt(cmd))
    assert exercise(tmp_path, item, bus, "--fill", "bus-sha=abc1234", "--fill", "who=me", "--fill", "why=because") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_MISSING"]


def test_seed_468_green_when_the_script_is_where_the_card_says(tmp_path, capsys):
    bus = make_bus(tmp_path, {"factory-pins/pin-runner.py": STUB_OK})
    cmd = "python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --reviewer <who> --reason <why>"
    item = item_file(tmp_path, adopt(cmd))
    assert exercise(tmp_path, item, bus, "--fill", "bus-sha=abc1234", "--fill", "who=me", "--fill", "why=because") == 0
    out = capsys.readouterr().out
    assert statuses(out) == ["ROUTE_OK"]
    assert "<bus-sha>" in route_lines(out)[0]   # the line shows the command as the card wrote it


def test_seed_469_unknown_program_is_route_missing(tmp_path, capsys):
    """#469: `doctrine-sync debt` is not a program on any bus."""
    bus = make_bus(tmp_path, {"tools/doctrine_sync.py": STUB_DOCTRINE_SYNC_NO_DEBT})
    item = item_file(tmp_path, adopt("doctrine-sync-not-a-program debt"))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_MISSING"]


def test_seed_469_subcommand_the_bus_lacks_is_route_failed(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/doctrine_sync.py": STUB_DOCTRINE_SYNC_NO_DEBT})
    item = item_file(tmp_path, adopt("python tools/doctrine_sync.py check", "python tools/doctrine_sync.py debt"))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK", "ROUTE_FAILED"]


def test_seed_469_green_once_the_bus_has_debt(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/doctrine_sync.py": STUB_DOCTRINE_SYNC_WITH_DEBT})
    item = item_file(tmp_path, adopt("python tools/doctrine_sync.py check", "python tools/doctrine_sync.py debt"))
    assert exercise(tmp_path, item, bus) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK", "ROUTE_OK"]


def test_route_timeout_kills_and_fails(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/slow.py": STUB_SLOW})
    item = item_file(tmp_path, adopt("python tools/slow.py go"))
    assert exercise(tmp_path, item, bus, "--timeout", "0.5") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_TIMEOUT"]


def test_unfilled_placeholder_is_route_failed_and_not_run(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    marker = tmp_path / "marker.txt"
    item = item_file(tmp_path, adopt("python tools/mark.py <marker> <who>"))
    assert exercise(tmp_path, item, bus, "--fill", f"marker={marker}") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_FAILED"] and "--fill who=VALUE" in captured.err
    assert not marker.exists()


def test_output_is_one_line_per_command_with_item_name(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/ok.py": STUB_OK})
    item = item_file(tmp_path, adopt("python tools/ok.py a", "python tools/ok.py b"))
    assert exercise(tmp_path, item, bus) == 0
    assert route_lines(capsys.readouterr().out) == [
        "ROUTE_OK 20261017-route-fixture.md python tools/ok.py a",
        "ROUTE_OK 20261017-route-fixture.md python tools/ok.py b",
    ]


def test_an_item_naming_no_route_passes_and_says_so(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, "### mlv-app, 2026-10-17 - prose only\n\nNo command here, only `RULINGS.md`.\n")
    assert exercise(tmp_path, item, bus) == 0
    assert "ROUTE_NONE 20261017-route-fixture.md" in capsys.readouterr().out


def test_a_card_target_item_is_exercised_like_any_other(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/ok.py": STUB_OK})
    body = ("## mlv-app/route-fixture\nrule: r\nmechanism: m\napplies: mlv-app\ncheck: c\nevidence: measured\n"
            "- **Adopt:** run `python tools/ok.py go` then `python tools/gone.py go`.\n")
    item = item_file(tmp_path, body, target="specs/mlv-app/cards.md", kind="trap")
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK", "ROUTE_MISSING"]


def test_an_item_that_fails_validation_is_not_exercised(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    marker = tmp_path / "marker.txt"
    item = item_file(tmp_path, adopt("python tools/mark.py <marker>"), name="not-a-dated-name.md")
    assert exercise(tmp_path, item, bus, "--fill", f"marker={marker}") == 1
    assert statuses(capsys.readouterr().out) == [] and not marker.exists()


# ---------- unsafe routes are refused, not run ----------

UNSAFE = [
    "git push origin master",
    "git -C . push --force origin HEAD:master",
    "gh pr create --draft",
    "gh api repos/o/r/issues -f title=x",
    "claude auth login",
    "codex login --device",
    "curl -s example.invalid",
    "ssh host true",
    "git ls-remote https://example.invalid/x.git",
    "git fetch ssh://example.invalid/o/r.git",
    "npm publish --dry-run",
    "python tools/mark.py out.txt --token abc",
    "python tools/mark.py out.txt ANTHROPIC_API_KEY=abc",
    "python tools/mark.py out.txt | python tools/mark.py out2.txt",
    "python tools/mark.py out.txt && python tools/mark.py out2.txt",
    "python tools/mark.py out.txt > elsewhere.txt",
    'python -c "print(1)"',
    "node -e 1",
]


@pytest.mark.parametrize("command", UNSAFE)
def test_unsafe_route_is_refused_and_fails_the_item(tmp_path, capsys, command):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    item = item_file(tmp_path, adopt(command))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"]


def test_an_unsafe_route_never_runs(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    marker = tmp_path / "marker.txt"
    fill = ("--fill", f"marker={marker}")
    item = item_file(tmp_path, adopt("python tools/mark.py <marker> --token abc"))
    assert exercise(tmp_path, item, bus, *fill) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"] and not marker.exists()
    # the same script without the credential flag runs and writes the marker
    item = item_file(tmp_path, adopt("python tools/mark.py <marker>"), name="20261017-safe-twin.md")
    assert exercise(tmp_path, item, bus, *fill) == 0 and marker.exists()


def test_scp_style_remote_is_a_network_target():
    reason = ob.route_unsafe_reason(["git", "fetch", "deploy@example.invalid:o/r.git"])
    assert reason is not None and "network target" in reason


def test_a_filled_windows_path_is_not_reparsed(tmp_path):
    """The value is substituted after the line is split, so its backslashes survive."""
    marker = tmp_path / "back\\slash-marker.txt" if os.name != "nt" else tmp_path / "marker.txt"
    status, _detail = ob.exercise_route("python tools/mark.py <marker>", tmp_path, 30, {"marker": str(marker)})
    assert status == "ROUTE_MISSING"          # no script in a bare tmp_path: the run never reached the marker
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "mark.py").write_text(STUB_MARK, encoding="utf-8")
    assert ob.exercise_route("python tools/mark.py <marker>", tmp_path, 30, {"marker": str(marker)}) == ("ROUTE_OK", "")
    assert marker.read_text(encoding="utf-8") == "ran"


@pytest.mark.parametrize("command", ["git log --oneline", "git -C . status --short", "python tools/mark.py out.txt"])
def test_safe_look_alikes_are_not_refused(command):
    assert ob.route_unsafe_reason(__import__("shlex").split(command)) is None


# ---------- the bus is never run in, and the scratch is removed ----------

def test_routes_never_run_in_the_bus_and_the_scratch_is_removed(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/touch.py": STUB_TOUCH, "tools/no_origin.py": STUB_NO_ORIGIN})
    scratch = tmp_path / "scratch"
    item = item_file(tmp_path, adopt("python tools/touch.py go", "python tools/no_origin.py go"))
    head = git(bus, "rev-parse", "HEAD")
    assert exercise(tmp_path, item, bus, scratch=scratch) == 0
    out = capsys.readouterr().out
    assert statuses(out) == ["ROUTE_OK", "ROUTE_OK"] and f"ROUTE_BUS {head[:12]}" in out
    assert not (bus / "touched.txt").exists()
    assert git(bus, "status", "--porcelain") == "" and git(bus, "rev-parse", "HEAD") == head
    assert list(scratch.iterdir()) == []


def test_the_scratch_is_removed_when_a_route_fails(tmp_path):
    bus = make_bus(tmp_path)
    scratch = tmp_path / "scratch"
    item = item_file(tmp_path, adopt("python tools/gone.py go"))
    assert exercise(tmp_path, item, bus, scratch=scratch) == 1
    assert list(scratch.iterdir()) == []


def test_the_bus_is_copied_at_its_committed_tip_only(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/ok.py": STUB_OK})
    (bus / "tools" / "uncommitted.py").write_text(STUB_OK, encoding="utf-8")
    item = item_file(tmp_path, adopt("python tools/uncommitted.py go"))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_MISSING"]


def test_scratch_root_inside_the_bus_is_refused_with_exit_2(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/touch.py": STUB_TOUCH})
    item = item_file(tmp_path, adopt("python tools/touch.py go"))
    inside = bus / "scratch"
    assert exercise(tmp_path, item, bus, scratch=inside) == 2
    assert "EXERCISE_SCRATCH_INSIDE_BUS" in capsys.readouterr().err
    assert not inside.exists() and not (bus / "touched.txt").exists()
    assert exercise(tmp_path, item, bus, scratch=bus) == 2


def test_a_bus_that_is_not_a_repository_is_refused_with_exit_2(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    item = item_file(tmp_path, adopt("python tools/ok.py go"))
    assert exercise(tmp_path, item, plain) == 2
    assert "EXERCISE_BUS_NOT_A_REPO" in capsys.readouterr().err


def test_no_bus_given_and_none_in_the_environment_is_exit_2(tmp_path, capsys):
    item = item_file(tmp_path, adopt("python tools/ok.py go"))
    assert exercise(tmp_path, item, None) == 2
    assert "EXERCISE_BUS_UNSET" in capsys.readouterr().err


def test_the_bus_defaults_to_the_environment_clone(tmp_path, monkeypatch, capsys):
    bus = make_bus(tmp_path, {"tools/ok.py": STUB_OK})
    monkeypatch.setenv(ob.ROUTE_BUS_ENV, str(bus))
    item = item_file(tmp_path, adopt("python tools/ok.py go"))
    assert exercise(tmp_path, item, None) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"]


def test_remove_exercise_scratch_only_removes_its_own_directory(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    stranger = root / "keep-me"
    stranger.mkdir()
    (stranger / "f.txt").write_text("x", encoding="utf-8")
    ob.remove_exercise_scratch(stranger, root)                       # wrong prefix
    elsewhere = tmp_path / f"{ob.ROUTE_SCRATCH_PREFIX}other"
    elsewhere.mkdir()
    ob.remove_exercise_scratch(elsewhere, root)                      # right prefix, not under the root
    assert stranger.exists() and elsewhere.exists()
    mine = root / f"{ob.ROUTE_SCRATCH_PREFIX}mine"
    mine.mkdir()
    (mine / "f.txt").write_text("x", encoding="utf-8")
    ob.remove_exercise_scratch(mine, root)
    assert not mine.exists() and stranger.exists()


def test_exercise_flags_need_exercise(tmp_path):
    item = item_file(tmp_path, adopt("python tools/ok.py go"))
    for flag in (["--bus", str(tmp_path)], ["--fill", "a=b"], ["--scratch-root", str(tmp_path)]):
        with pytest.raises(SystemExit) as excinfo:
            ob.main(["--repo", str(tmp_path), "validate", str(item), *flag])
        assert excinfo.value.code == 2
    with pytest.raises(SystemExit):
        ob.main(["--repo", str(tmp_path), "validate", str(item), "--exercise", "--fill", "no-equals-sign"])


def test_plain_validate_still_runs_nothing(tmp_path, capsys):
    item = item_file(tmp_path, adopt("python tools/mark.py marker.txt"))
    assert ob.main(["--repo", str(tmp_path), "validate", str(item)]) == 0
    assert statuses(capsys.readouterr().out) == [] and not list(tmp_path.rglob("marker.txt"))


# ---------- ruling candidates: touches/supersedes ----------

CANDIDATE_HEADING = "### mlv-app, 2026-10-16 - RULING CANDIDATE: one fleet standard for doctrine fold-in\n\n"
CANDIDATE_RULES = "**Rules, each with its check**\n- **S1 One debt definition.** debt = bus commits after the cursor.\n"


def validate(tmp_path: Path, body: str) -> int:
    return ob.main(["--repo", str(tmp_path), "validate", str(item_file(tmp_path, body))])


def refusal_code(body: str) -> str | None:
    try:
        ob.screen_ruling_candidate({"meta": {}, "body": body})
    except ob.Refusal as e:
        return e.code
    return None


def test_seed_469_ruling_candidate_without_the_list_is_refused(tmp_path, capsys):
    """#469 r1 shape: a ruling candidate with rules and checks and no word on the rules it overlaps."""
    body = CANDIDATE_HEADING + "Owner-requested.\n\n" + CANDIDATE_RULES
    assert validate(tmp_path, body) == 1
    assert "RULING_CANDIDATE_NO_SUPERSEDES" in capsys.readouterr().err
    assert refusal_code(body) == "RULING_CANDIDATE_NO_SUPERSEDES"


def test_seed_469_green_with_the_list(tmp_path, capsys):
    body = CANDIDATE_HEADING + "touches/supersedes: R14.8, R15.5; FLEET-FOLD-LOOP-SINGLE-SOURCE-1\n\n" + CANDIDATE_RULES
    assert validate(tmp_path, body) == 0
    assert ob.touches_supersedes_entries(body) == ["R14.8", "R15.5", "FLEET-FOLD-LOOP-SINGLE-SOURCE-1"]


@pytest.mark.parametrize("label", ["touches/supersedes:", "- touches/supersedes:", "**touches/supersedes:**", "- **Touches/Supersedes:**"])
def test_the_list_may_be_bullets_under_the_label(label):
    indent = "  " if label.startswith("-") else ""
    body = CANDIDATE_HEADING + f"{label}\n{indent}- R14.8 (extends)\n{indent}- `R15.5`\n\n" + CANDIDATE_RULES
    assert ob.touches_supersedes_entries(body) == ["R14.8 (extends)", "R15.5"]
    assert refusal_code(body) is None


def test_none_with_a_reason_is_accepted(tmp_path):
    body = CANDIDATE_HEADING + "touches/supersedes: none: no standing rule covers fold-in debt, checked RULINGS.md and R14\n\n" + CANDIDATE_RULES
    assert validate(tmp_path, body) == 0
    bullet = CANDIDATE_HEADING + "- touches/supersedes:\n  - none - nothing in RULINGS.md names fold debt\n\n" + CANDIDATE_RULES
    assert refusal_code(bullet) is None


@pytest.mark.parametrize("value, code", [
    ("none", "RULING_CANDIDATE_NONE_NEEDS_REASON"),
    ("none: nothing", "RULING_CANDIDATE_NONE_NEEDS_REASON"),
    ("none: too few words", "RULING_CANDIDATE_NONE_NEEDS_REASON"),
    ("", "RULING_CANDIDATE_NO_SUPERSEDES"),
])
def test_a_thin_or_empty_list_is_refused(value, code):
    assert refusal_code(CANDIDATE_HEADING + f"touches/supersedes: {value}\n\n" + CANDIDATE_RULES) == code


def test_none_beside_rule_ids_is_refused():
    body = CANDIDATE_HEADING + "touches/supersedes:\n- R14.8\n- none: and nothing else at all\n\n" + CANDIDATE_RULES
    assert refusal_code(body) == "RULING_CANDIDATE_NONE_WITH_RULES"


def test_a_label_inside_a_code_fence_does_not_count():
    body = CANDIDATE_HEADING + "```\ntouches/supersedes: R14.8\n```\n\n" + CANDIDATE_RULES
    assert refusal_code(body) == "RULING_CANDIDATE_NO_SUPERSEDES"


def test_a_bullet_list_stops_at_the_blank_line():
    body = CANDIDATE_HEADING + "touches/supersedes:\n- R14.8\n\n- S1 One debt definition.\n"
    assert ob.touches_supersedes_entries(body) == ["R14.8"]


@pytest.mark.parametrize("body", [
    "### mlv-app, 2026-10-06 - an MLV measurement for ruling candidate mechanical-outbox-every-member-r1 (bus 2877eff)\n\nBody.\n",
    "### mlv-app - a plain trap\n\nA sentence that mentions a ruling candidate in passing.\n",
])
def test_an_item_that_does_not_propose_a_candidate_is_untouched(body):
    assert refusal_code(body) is None


@pytest.mark.parametrize("body", [
    "### mlv-app - a hook is a tripwire\n\n**Ruling candidate (not law until ratified):** the hook is deny-only.\n",
    "### mlv-app - a hook is a tripwire\n\nRuling-candidate: the hook is deny-only.\n",
    "### mlv-app - ruling-candidate: a hook is a tripwire\n\nBody.\n",
])
def test_the_ways_an_item_proposes_a_candidate(body):
    assert refusal_code(body) == "RULING_CANDIDATE_NO_SUPERSEDES"


def test_the_check_runs_with_and_without_exercise(tmp_path, capsys):
    bus = make_bus(tmp_path)
    body = CANDIDATE_HEADING + CANDIDATE_RULES
    item = item_file(tmp_path, body)
    assert ob.main(["--repo", str(tmp_path), "validate", str(item)]) == 1
    assert exercise(tmp_path, item, bus) == 1
    assert capsys.readouterr().err.count("RULING_CANDIDATE_NO_SUPERSEDES") == 2


def test_the_steward_handoff_card_validates_and_its_route_is_extracted():
    """The card that offers this tool to the bus passes `validate` (card size limits included) and its Adopt
    section yields the pin and run commands, so a producer can exercise it with --fill."""
    card = REPO_ROOT / "doctrine-outbox" / "20261017-doctrine-outbox-route-exercise-candidate.md"
    assert ob.main(["--repo", str(REPO_ROOT), "validate", str(card)]) == 0
    routes = ob.extract_routes(ob.parse_item(card.read_text(encoding="utf-8"))["body"])
    assert [r.split()[:4] for r in routes] == [["python", "factory-pins/pin-runner.py", "pin", "doctrine-outbox"],
                                              ["python", "factory-pins/pin-runner.py", "run", "doctrine-outbox"]]


def test_the_shipped_candidates_that_predate_the_rule_still_parse():
    """parse_item is unchanged: only validate refuses, so already-merged items keep validating as items."""
    for name in ("20261016-fleet-fold-standard.md", "20261005-an-edit-time-text-hook-cannot-judge-test-still-executes.md"):
        text = (REPO_ROOT / "doctrine-outbox" / name).read_text(encoding="utf-8")
        assert ob.parse_item(text)["meta"]["law4"] == "attested"


# ====================================================================================================
# r2 fix round (sol r2 B1-B4, opus H1-H7). Named mutations, each turns its rows red then is reverted:
#   M4  route_live_bus_reason returns None                          -> the RX-03 identity rows
#   M5  _kill_route_tree kills only the direct child                  -> the RX-01 process-tree row
#   M6  redact_route_text returns its input                           -> the B4 redaction rows
#   M7  ROUTE_WRAPPERS emptied / _route_git_unsafe returns None       -> the RX-02 rows
#   M8  bus_commits_behind_origin returns 0                           -> the RX-04 row
#   M9  RULING_CANDIDATE_LEAD_RE without the bullet/quote prefix      -> the H6 rows
# r4 (sol r3 RX-06): a credential fill with shell-operator / placeholder syntax, or in the program or script position,
# is printed raw -> M10 exercise_route returns its detail without redact_secret_values -> the RX-06 rows
# r4b (opus r4 RX-07): a credential value that repr() escapes (backslash, both quote kinds) is printed by the shell-operator
# reason -> M11 that reason formats the raw token with !r (the choke point alone still holds, rows stay green);
# M12 M11 plus redact_secret_values replaces the literal secret only -> the RX-07 rows
# ====================================================================================================

SENTINEL = "REVIEW_SENTINEL_NOT_A_SECRET_7f3a9"
STUB_SPAWN = (
    "import os, pathlib, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')\n"
    "time.sleep(600)\n"
)
STUB_ECHO_ERR = "import sys\nprint(sys.argv[1], file=sys.stderr)\nsys.exit(3)\n"


def pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except ImportError:
        pass
    if os.name == "nt":
        listing = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in listing.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        return "State:\tZ" not in Path(f"/proc/{pid}/status").read_text()
    except OSError:
        return True


def force_kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def bus_spellings(tmp_path: Path, bus: Path) -> dict[str, str]:
    """Every spelling of 'a file inside the live bus' that the identity rule has to see through."""
    spell = {"canonical": str(bus / "x.txt"), "trailing-slash": str(bus) + os.sep, "dotdot": str(bus / "sub" / ".." / "x.txt")}
    if os.name == "nt":
        spell["case-varied"] = str(bus / "x.txt").swapcase()
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(bus), buf, 1024) and buf.value.lower() != str(bus).lower():
            spell["short-name"] = buf.value + "\\x.txt"
    link = tmp_path / "link-to-bus"
    try:
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(bus)], capture_output=True, text=True).returncode == 0
        else:
            os.symlink(bus, link)
            made = True
    except OSError:
        made = False
    if made:
        spell["junction"] = str(link / "x.txt")
    return spell


SPELLINGS = ["canonical", "trailing-slash", "dotdot", "case-varied", "short-name", "junction"]


# ---------- B1 / H3 / RX-03: the live bus, by identity, through every spelling ----------

@pytest.mark.parametrize("spelling", SPELLINGS)
def test_rx03_an_argument_inside_the_live_bus_is_unsafe_through_any_spelling(tmp_path, capsys, spelling):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    spell = bus_spellings(tmp_path, bus)
    if spelling not in spell:
        pytest.skip(f"no {spelling} spelling on this platform or the link could not be made")
    item = item_file(tmp_path, adopt("python tools/mark.py <where>"))
    assert exercise(tmp_path, item, bus, "--fill", f"where={spell[spelling]}") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"]
    assert not (bus / "x.txt").exists()


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_rx03_a_scratch_root_inside_the_live_bus_is_exit_2_through_any_spelling(tmp_path, capsys, spelling):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    spell = bus_spellings(tmp_path, bus)
    if spelling not in spell:
        pytest.skip(f"no {spelling} spelling on this platform or the link could not be made")
    item = item_file(tmp_path, adopt("python tools/mark.py out.txt"))
    assert exercise(tmp_path, item, bus, scratch=Path(spell[spelling])) == 2
    assert "EXERCISE_SCRATCH_INSIDE_BUS" in capsys.readouterr().err
    assert not (bus / "x.txt").exists() and git(bus, "status", "--porcelain") == ""


def test_rx03_an_option_value_inside_the_live_bus_is_unsafe(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    item = item_file(tmp_path, adopt("python tools/mark.py --out=<bus>/x.txt"))
    assert exercise(tmp_path, item, bus, "--fill", f"bus={bus}") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"] and not (bus / "x.txt").exists()


def test_rx03_a_route_whose_cwd_is_the_live_bus_is_unsafe(tmp_path):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    status, detail = ob.exercise_route("python tools/mark.py m.txt", bus, 30, {}, bus)
    assert status == "ROUTE_UNSAFE" and "live bus" in detail
    assert not (bus / "m.txt").exists()


@pytest.mark.parametrize("command", [
    "git -C <bus> pull --ff-only",
    "git -C <bus> log --oneline",
    "git --git-dir=<bus>/.git log",
    "git --work-tree <bus> status",
])
def test_rx03_a_git_route_aimed_at_the_live_bus_by_fill_is_unsafe(tmp_path, capsys, command):
    bus = make_bus(tmp_path, {"README.md": "bus\n"})
    head = git(bus, "rev-parse", "HEAD")
    item = item_file(tmp_path, adopt(command))
    assert exercise(tmp_path, item, bus, "--fill", f"bus={bus}") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"]
    assert git(bus, "rev-parse", "HEAD") == head and git(bus, "status", "--porcelain") == ""


def test_the_scratch_placeholder_names_the_scratch_clone(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    item = item_file(tmp_path, adopt("git -C <scratch> log --oneline", "python tools/mark.py <scratch>/marker.txt"))
    assert exercise(tmp_path, item, bus) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK", "ROUTE_OK"]
    assert not (bus / "marker.txt").exists()


# ---------- B2 / RX-02: wrappers, indirections, online and install verbs ----------

BYPASSES = [
    "cmd /c git push",
    "cmd /c python tools/mark.py out.txt",
    "env gh pr create",
    "env python tools/mark.py out.txt",
    "sudo git status",
    "xargs git push",
    "start notepad",
    "time git status",
    "FOO=1 python tools/mark.py out.txt",
    'bash -c "git push"',
    'bash -lc "git push"',
    'sh -ec "git push"',
    "bash -o pipefail -c 'git push'",
    "pwsh -c 'git push'",
    "pwsh -co 'git push'",
    "pwsh -com 'git push'",
    "pwsh -Command 'git push'",
    "pwsh -e ZQBjAGgAbwA=",
    "pwsh -ec ZQBjAGgAbwA=",
    "pwsh -enc ZQBjAGgAbwA=",
    "powershell -EncodedCommand ZQBjAGgAbwA=",
    "pwsh git push",
    "pwsh -c 'git push' -File tools/x.ps1",
    "pwsh -co 'git push' -File tools/x.ps1",
    "pwsh -NoProfile -enc ZQBjAGgAbwA= -File tools/x.ps1",
    "git -c core.pager=evil log",
    "git -c core.fsmonitor=evil status",
    'python -c "print(1)"',
    'python -Ic "print(1)"',
    "python -m twine upload dist/x",
    "python -m pip install requests",
    "python -m http.server",
    "py -3 -c 1",
    "node -pe 1",
    "node --eval 1",
    "ruby -e 1",
    "npx claude auth login",
    "npx some-publisher",
    "claude auth login",
    "git -c alias.ship=push ship",
    "git -c core.sshCommand=x log",
    "git --config-env=a=B log",
    "git ship",
    "git config exercise.probe yes",
    "git config --add exercise.probe yes",
    "git config --unset exercise.probe",
    "git log --output=elsewhere.txt",
    "git grep -Ofalse needle",
    "find . -exec git push ;",
]
ONLINE_AND_INSTALL = [
    "git pull --ff-only",
    "git fetch origin",
    "git fetch --all",
    "git push origin master",
    "git ls-remote origin",
    "git remote update",
    "git clone https://example.invalid/x.git",
    "git clone example.com:o/r",
    "git submodule update --init",
    "pip install requests",
    "pip3 download requests",
    "npm install",
    "npm i left-pad",
    "yarn install",
    "yarn add left-pad",
    "pnpm add left-pad",
    "cargo install ripgrep",
]


@pytest.mark.parametrize("command", BYPASSES)
def test_rx02_a_wrapper_or_indirection_is_unsafe(tmp_path, command):
    assert ob.route_unsafe_reason(ob.split_route(command), tmp_path) is not None, command


@pytest.mark.parametrize("command", ONLINE_AND_INSTALL)
def test_online_git_verbs_and_package_installs_are_unsafe(tmp_path, command):
    assert ob.route_unsafe_reason(ob.split_route(command), tmp_path) is not None, command


@pytest.mark.parametrize("command", ["git -C .. status", "git -C / status", "git --git-dir=.. log", "git -C sub/../.. log"])
def test_rx02_git_aimed_outside_the_scratch_clone_is_unsafe(tmp_path, command):
    (tmp_path / "sub").mkdir(exist_ok=True)
    reason = ob.route_unsafe_reason(ob.split_route(command), tmp_path)
    assert reason is not None and "outside the scratch clone" in reason, command


def test_rx02_git_aimed_at_an_absolute_outside_path_is_unsafe(tmp_path):
    outside = tmp_path.parent
    reason = ob.route_unsafe_reason(["git", "-C", str(outside), "status"], tmp_path / "scratch")
    assert reason is not None and "outside the scratch clone" in reason


@pytest.mark.parametrize("command", [
    "git log --oneline", "git -C . status --short", "git rev-parse HEAD", "git config --get user.name", "git config --list",
    "git -C sub log", "python tools/mark.py out.txt", "python -u tools/mark.py out.txt", "python -W ignore tools/mark.py x",
    "python -X utf8 tools/mark.py x", "python -m pytest tests -q", "python tools/mark.py -c", "py -3 tools/mark.py x",
    "pwsh -NoProfile -File tools/x.ps1 -Command foo", "bash tools/x.sh -c", "node tools/x.js --eval", "node --max-old-space-size=100 tools/x.js",
    "doctrine-sync-not-a-program debt", "pytest tests -q",
])
def test_rx02_safe_look_alikes_are_still_not_refused(tmp_path, command):
    (tmp_path / "sub").mkdir(exist_ok=True)
    assert ob.route_unsafe_reason(ob.split_route(command), tmp_path) is None, command


def test_rx02_a_bypass_is_refused_before_it_is_launched(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    marker = tmp_path / "marker.txt"
    commands = ["cmd /c python tools/mark.py <marker>", "env python tools/mark.py <marker>",
                'bash -lc "python tools/mark.py <marker>"', "pwsh -co 'python tools/mark.py marker'",
                "git -c alias.m=status m", "python -Ic 1"]
    item = item_file(tmp_path, adopt(*commands))
    assert exercise(tmp_path, item, bus, "--fill", f"marker={marker}") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"] * len(commands)
    assert not marker.exists()


# ---------- B3 / RX-01: a timeout ends the whole process tree ----------

def test_rx01_a_timeout_kills_the_whole_process_tree(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "spawn.py").write_text(STUB_SPAWN, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    result: list = []
    runner = threading.Thread(target=lambda: result.append(ob.exercise_route("python tools/spawn.py <pidfile>", tmp_path, 2, {"pidfile": str(pidfile)})),
                              daemon=True)
    started = time.monotonic()
    runner.start()
    try:
        runner.join(30)                        # the unfixed code blocks here for as long as the grandchild sleeps
        elapsed = time.monotonic() - started
        assert not runner.is_alive(), "exercise_route did not return: the route's process tree outlived the timeout"
        assert result and result[0][0] == "ROUTE_TIMEOUT" and elapsed < 10, (result, elapsed)
        assert pidfile.exists(), "the route never started"
        parent, child = (int(p) for p in pidfile.read_text().split())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (pid_alive(parent) or pid_alive(child)):
            time.sleep(0.2)
        assert not pid_alive(child), "the grandchild survived the timeout"
        assert not pid_alive(parent), "the route's own process survived the timeout"
    finally:
        if pidfile.exists():
            for pid in pidfile.read_text().split():
                force_kill(int(pid))


def test_rx01_the_timeout_reaches_a_route_run_through_the_cli(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/spawn.py": STUB_SPAWN})
    pidfile = tmp_path / "pids.txt"
    item = item_file(tmp_path, adopt("python tools/spawn.py <pidfile>"))
    try:
        assert exercise(tmp_path, item, bus, "--timeout", "2", "--fill", f"pidfile={pidfile}") == 1
        assert statuses(capsys.readouterr().out) == ["ROUTE_TIMEOUT"]
        assert not any(pid_alive(int(p)) for p in pidfile.read_text().split())
    finally:
        if pidfile.exists():
            for pid in pidfile.read_text().split():
                force_kill(int(pid))


def test_route_output_is_bounded_to_its_tail(tmp_path):
    code = "import sys\nfor i in range(60000):\n    sys.stdout.write('x' * 100 + '\\n')\nprint('TAILMARK')\nsys.stderr.write('E' * 200000 + 'ERRTAIL')\nsys.exit(4)\n"
    (tmp_path / "noisy.py").write_text(code, encoding="utf-8")
    returncode, out, err = ob.run_route_process([sys.executable, "noisy.py"], tmp_path, dict(os.environ), 60)
    assert returncode == 4
    assert len(out) <= ob.ROUTE_OUTPUT_CAP_BYTES and out.rstrip().endswith("TAILMARK")
    assert len(err) <= ob.ROUTE_OUTPUT_CAP_BYTES and err.endswith("ERRTAIL")


# ---------- B4: a credential's value is never printed or written ----------

CREDENTIAL_ROUTES = [
    "GH_TOKEN=<value> gh pr create",
    "ANTHROPIC_API_KEY=<value> python tools/mark.py out.txt",
    "MY_SERVICE_TOKEN=<value> python tools/mark.py out.txt",
    "DEPLOY_KEY=<value> python tools/mark.py out.txt",
    "DB_PASSWORD=<value> python tools/mark.py out.txt",
    "python tools/mark.py out.txt --token=<value>",
    "python tools/mark.py out.txt --token <value>",
    "python tools/mark.py https://user:<value>@example.invalid/x",
]


def everything_written_under(root: Path) -> bytes:
    return b"".join(f.read_bytes() for f in root.rglob("*") if f.is_file())


@pytest.mark.parametrize("command", CREDENTIAL_ROUTES)
def test_b4_a_refused_credential_never_shows_its_value(tmp_path, capsys, command):
    bus = make_bus(tmp_path, {"tools/mark.py": STUB_MARK})
    item = item_file(tmp_path, adopt(command))
    assert exercise(tmp_path, item, bus, "--fill", f"value={SENTINEL}") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_UNSAFE"]
    assert SENTINEL not in captured.out and SENTINEL not in captured.err
    assert SENTINEL.encode() not in everything_written_under(tmp_path)
    status, detail = ob.exercise_route(command, tmp_path, 30, {"value": SENTINEL})
    assert status == "ROUTE_UNSAFE" and SENTINEL not in detail


def test_b4_a_refused_credential_keeps_its_name_and_hides_the_value(tmp_path):
    status, detail = ob.exercise_route("GH_TOKEN=<value> gh pr create", tmp_path, 30, {"value": SENTINEL})
    assert status == "ROUTE_UNSAFE" and "GH_TOKEN=<redacted>" in detail


def test_b4_a_secret_written_in_the_card_is_redacted_in_the_route_line(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt(f"GH_TOKEN={SENTINEL} gh pr create"))
    assert exercise(tmp_path, item, bus) == 1
    captured = capsys.readouterr()
    assert route_lines(captured.out)[0].endswith("GH_TOKEN=<redacted> gh pr create")
    assert SENTINEL not in captured.out and SENTINEL not in captured.err


def test_b4_a_fill_pair_without_equals_is_not_echoed(tmp_path, capsys):
    item = item_file(tmp_path, adopt("python tools/ok.py go"))
    with pytest.raises(SystemExit) as excinfo:
        ob.main(["--repo", str(tmp_path), "validate", str(item), "--exercise", "--fill", SENTINEL])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out and SENTINEL not in captured.err


def test_b4_a_credential_named_fill_is_scrubbed_from_a_failure_tail(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "echo_err.py").write_text(STUB_ECHO_ERR, encoding="utf-8")
    status, detail = ob.exercise_route("python tools/echo_err.py <api_key>", tmp_path, 30, {"api_key": SENTINEL})
    assert status == "ROUTE_FAILED" and SENTINEL not in detail and "<redacted>" in detail


def test_redact_route_text_forms():
    assert ob.redact_route_text("GH_TOKEN=abc gh pr create") == "GH_TOKEN=<redacted> gh pr create"
    assert ob.redact_route_text("x --token abc y") == "x --token <redacted> y"
    assert ob.redact_route_text("x --password=abc") == "x --password=<redacted>"
    assert ob.redact_route_text("git clone https://u:p@h/x.git") == "git clone https://<redacted>@h/x.git"
    assert ob.redact_route_text("python tools/ok.py go") == "python tools/ok.py go"


# ---------- r3 B4 / RX-05: a refused credential assignment redacts its ENTIRE value ----------
# r2 redacted the expanded token with a `\S*` pattern, so a --fill value holding whitespace lost only its first word
# (`DB_PASSWORD=<redacted> SECOND_SENTINEL`). The value is now decided on the TEMPLATE token and replaced whole.
#   M8  route_secret_values returns []  and the whole-token branch of the credential diagnostic removed -> the RX-05 rows

RX05_SENTINELS = {
    "multiword": ("FIRST_SENTINEL SECOND_SENTINEL", ["FIRST_SENTINEL", "SECOND_SENTINEL"]),
    "tabs_and_newline": ("TAB_ONE_SENTINEL\tTAB_TWO_SENTINEL\nLINE_THREE_SENTINEL", ["TAB_ONE_SENTINEL", "TAB_TWO_SENTINEL", "LINE_THREE_SENTINEL"]),
    "quotes_in_the_value": ('"QUOTED_ONE_SENTINEL QUOTED_TWO_SENTINEL"', ["QUOTED_ONE_SENTINEL", "QUOTED_TWO_SENTINEL"]),
}


@pytest.mark.parametrize("case", list(RX05_SENTINELS))
def test_rx05_a_credential_fill_with_whitespace_is_redacted_whole(tmp_path, case):
    value, parts = RX05_SENTINELS[case]
    status, detail = ob.exercise_route("DB_PASSWORD=<value> python tools/x.py", tmp_path, 30, {"value": value})
    assert status == "ROUTE_UNSAFE" and "DB_PASSWORD=<redacted>" in detail
    for part in parts:
        assert part not in detail, detail


@pytest.mark.parametrize("case", list(RX05_SENTINELS))
def test_rx05_nothing_a_run_prints_or_writes_carries_the_value(tmp_path, capsys, case):
    value, parts = RX05_SENTINELS[case]
    bus = make_bus(tmp_path, {"tools/x.py": STUB_OK})
    item = item_file(tmp_path, adopt("DB_PASSWORD=<value> python tools/x.py"))
    assert exercise(tmp_path, item, bus, "--fill", f"value={value}") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_UNSAFE"]
    for part in parts:
        assert part not in captured.out and part not in captured.err
        assert not [p for p in tmp_path.rglob("*") if p.is_file() and part.encode() in p.read_bytes()]


def test_rx05_a_quoted_multiword_value_written_in_the_card_is_redacted_whole(tmp_path):
    for command in ('DB_PASSWORD="QLIT_ONE_SENTINEL QLIT_TWO_SENTINEL" python tools/x.py',
                    "DB_PASSWORD='QLIT_ONE_SENTINEL QLIT_TWO_SENTINEL' python tools/x.py"):
        status, detail = ob.exercise_route(command, tmp_path, 30, {})
        assert status == "ROUTE_UNSAFE" and "DB_PASSWORD=<redacted>" in detail
        assert "QLIT_ONE_SENTINEL" not in detail and "QLIT_TWO_SENTINEL" not in detail, detail


@pytest.mark.parametrize("tok", ["DB_PASSWORD=QTOK_ONE_SENTINEL QTOK_TWO_SENTINEL", "--password=QTOK_ONE_SENTINEL QTOK_TWO_SENTINEL"])
def test_rx05_one_token_holding_whitespace_after_posix_unquoting_is_redacted_whole(tmp_path, tok):
    # POSIX shlex turns DB_PASSWORD="a b" into ONE token `DB_PASSWORD=a b`; Windows' non-POSIX split does not, so the
    # token list is given directly to keep this row the same on both.
    reason = ob.route_unsafe_reason([tok, "python", "tools/x.py"], tmp_path)
    assert reason and "QTOK_ONE_SENTINEL" not in reason and "QTOK_TWO_SENTINEL" not in reason, reason


def test_rx05_a_credential_value_that_also_appears_elsewhere_is_redacted_there_too(tmp_path):
    shared = "SHARED_ONE_SENTINEL SHARED_TWO_SENTINEL"
    fills = {"label": shared, "value": shared}
    status, detail = ob.exercise_route("python tools/x.py <label> DB_PASSWORD=<value>", tmp_path, 30, fills)
    assert status == "ROUTE_UNSAFE" and "SHARED_ONE_SENTINEL" not in detail and "SHARED_TWO_SENTINEL" not in detail, detail
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "echo_err.py").write_text(STUB_ECHO_ERR, encoding="utf-8")
    status, detail = ob.exercise_route("python tools/echo_err.py <label> <db_password>", tmp_path, 30, {"label": shared, "db_password": shared})
    assert status == "ROUTE_FAILED" and "<redacted>" in detail
    assert "SHARED_ONE_SENTINEL" not in detail and "SHARED_TWO_SENTINEL" not in detail, detail


def test_rx05_a_value_after_a_token_flag_is_redacted_whole(tmp_path):
    value = "FLAGGED_ONE_SENTINEL FLAGGED_TWO_SENTINEL"
    status, detail = ob.exercise_route("python tools/x.py --password=<value>", tmp_path, 30, {"value": value})
    assert status == "ROUTE_UNSAFE" and "FLAGGED_ONE_SENTINEL" not in detail and "FLAGGED_TWO_SENTINEL" not in detail, detail
    assert ob.route_secret_values(["x", "--token", "<v>"], {"v": value}) == [value, "FLAGGED_ONE_SENTINEL", "FLAGGED_TWO_SENTINEL"]


def test_rx05_a_fill_that_is_not_a_credential_is_left_alone(tmp_path):
    assert ob.route_secret_values(["python", "tools/x.py", "<branch>"], {"branch": "feature one"}) == []


# ---------- r4 / RX-06: EVERY diagnostic of a route is redacted, not only the credential branch ----------
# r3 redacted a credential fill in the whole-token branch and the failure tail only; the shell-operator reason, the
# unfilled-placeholder reason and route_missing_reason printed the expanded token raw. exercise_route now redacts the
# detail it returns once, and judges "unfilled" on the template. Mutation: drop that one call (or judge unfilled after the
# fill) -> the RX-06 rows go red.

RX06 = "RX06_SENTINEL"


def test_rx06_a_credential_fill_that_looks_like_a_shell_operator_is_not_printed(tmp_path):
    fills = {"label": ">" + RX06, "value": ">" + RX06}
    status, detail = ob.exercise_route("python tools/x.py <label> DB_PASSWORD=<value>", tmp_path, 30, fills)
    assert status == "ROUTE_UNSAFE" and RX06 not in detail, detail


def test_rx06_a_credential_fill_that_looks_like_a_placeholder_is_not_printed(tmp_path):
    status, detail = ob.exercise_route("DB_PASSWORD=<value> python tools/x.py", tmp_path, 30, {"value": f"<{RX06}>"})
    assert status.startswith("ROUTE_") and status != "ROUTE_OK" and RX06 not in detail, detail
    status, detail = ob.exercise_route("python tools/x.py <api_key>", tmp_path, 30, {"api_key": f"<{RX06}>"})
    assert RX06 not in detail, detail


def test_rx06_a_credential_fill_in_the_program_or_script_position_is_not_printed_by_route_missing(tmp_path):
    status, detail = ob.exercise_route("python <api_key>", tmp_path, 30, {"api_key": f"{RX06}.py"})
    assert status == "ROUTE_MISSING" and RX06 not in detail and "<redacted>" in detail, detail
    status, detail = ob.exercise_route("<api_key> go", tmp_path, 30, {"api_key": RX06})
    assert status == "ROUTE_MISSING" and RX06 not in detail and "<redacted>" in detail, detail


@pytest.mark.parametrize("command, value", [
    ("python tools/x.py <label> DB_PASSWORD=<value>", ">" + RX06),
    ("DB_PASSWORD=<value> python tools/x.py", f"<{RX06}>"),
    ("python <api_key>", f"{RX06}.py"),
])
def test_rx06_nothing_a_run_prints_or_writes_carries_the_value(tmp_path, capsys, command, value):
    bus = make_bus(tmp_path, {"tools/x.py": STUB_OK})
    item = item_file(tmp_path, adopt(command))
    fills = ["--fill", f"value={value}", "--fill", f"label={value}", "--fill", f"api_key={value}"]
    assert exercise(tmp_path, item, bus, *fills) == 1
    captured = capsys.readouterr()
    assert RX06 not in captured.out and RX06 not in captured.err
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and RX06.encode() in p.read_bytes()]


# ---------- r4b / RX-07: a credential value that repr() escapes is not printed either ----------
# The shell-operator reason formatted the raw token with !r BEFORE the redact_secret_values choke point, so a value with a
# backslash (doubled by repr) or both quote kinds (the quote escaped) no longer matched the literal secret and was printed.
# Mutations: M11 revert the site (route_unsafe_reason formats the raw token with !r) -> still green, the choke point holds;
# M12 revert the choke point's repr forms too -> the RX-07 rows go red.

RX07_VALUES = [
    ">RX07\\SENT",                    # a real single backslash (repr doubles it)
    ">C:\\RX07\\SENT",                # a Windows path
    ">RX07'q\"SENT",                  # both quote kinds (repr escapes the quote)
]


def rx07_leaks(text: str, value: str) -> bool:
    return value in text or repr(value)[1:-1] in text or repr(value) in text


@pytest.mark.parametrize("value", RX07_VALUES)
def test_rx07_a_credential_fill_with_an_escaped_character_is_not_printed_by_the_shell_operator_reason(tmp_path, value):
    status, detail = ob.exercise_route("python tools/x.py ><api_key>", tmp_path, 30, {"api_key": value[1:]})
    assert status == "ROUTE_UNSAFE" and not rx07_leaks(detail, value[1:]) and not rx07_leaks(detail, value), detail
    assert "<redacted>" in detail, detail


@pytest.mark.parametrize("value", RX07_VALUES)
def test_rx07_sols_r3_template_with_an_escaped_value_is_not_printed(tmp_path, value):
    fills = {"label": value, "value": value}
    status, detail = ob.exercise_route("python tools/x.py <label> DB_PASSWORD=<value>", tmp_path, 30, fills)
    assert status == "ROUTE_UNSAFE" and not rx07_leaks(detail, value), detail


def test_rx07_the_choke_point_alone_redacts_the_escaped_forms_of_a_secret():
    for value in RX07_VALUES:
        text = f"a {value!r} b {repr(value)[1:-1]} c {value}"
        assert not rx07_leaks(ob.redact_secret_values(text, [value]), value), text
    assert ob.redact_secret_values("keep this", ["x\\y"]) == "keep this"


def test_rx07_the_shell_operator_reason_redacts_before_it_formats(tmp_path):
    status, reason = ob.exercise_route("python tools/x.py ><api_key>", tmp_path, 30, {"api_key": "RX07\\SENT"})
    assert status == "ROUTE_UNSAFE" and "RX07" not in reason and "\\\\" not in reason, reason


@pytest.mark.parametrize("value", RX07_VALUES)
def test_rx07_nothing_a_run_prints_or_writes_carries_the_escaped_value(tmp_path, capsys, value):
    bus = make_bus(tmp_path, {"tools/x.py": STUB_OK})
    item = item_file(tmp_path, adopt("python tools/x.py ><api_key>"))
    assert exercise(tmp_path, item, bus, "--fill", f"api_key={value[1:]}") == 1
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert not rx07_leaks(stream, value[1:]) and not rx07_leaks(stream, value), stream
    for p in tmp_path.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            assert value[1:].encode() not in data and repr(value[1:])[1:-1].encode() not in data, p


# ---------- r3 hardening: a python option that takes an operand is not the script ----------

@pytest.mark.parametrize("command", [
    "python -B -X utf8 tools/ok.py go",
    "python -W ignore tools/ok.py go",
    "python -Xutf8 -W error tools/ok.py go",
    "python --check-hash-based-pycs default tools/ok.py go",
])
def test_a_python_option_operand_is_not_taken_for_the_script(tmp_path, command):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "ok.py").write_text(STUB_OK, encoding="utf-8")
    assert ob.exercise_route(command, tmp_path, 30, {}) == ("ROUTE_OK", "")


def test_a_python_launcher_naming_a_missing_script_past_its_options_is_still_route_missing(tmp_path):
    status, detail = ob.exercise_route("python -X utf8 tools/gone.py go", tmp_path, 30, {})
    assert status == "ROUTE_MISSING" and "tools/gone.py" in detail and "utf8" not in detail


# ---------- H5 / RX-04: a stale bus clone is flagged ----------

def make_upstream_and_bus(tmp_path: Path) -> tuple[Path, Path]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(upstream)], check=True, capture_output=True, text=True)
    (upstream / "tools").mkdir()
    (upstream / "tools" / "ok.py").write_text(STUB_OK, encoding="utf-8")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "one")
    bus = tmp_path / "bus"
    subprocess.run(["git", "clone", "-q", str(upstream), str(bus)], check=True, capture_output=True, text=True)
    return upstream, bus


def test_rx04_a_bus_behind_its_origin_warns_and_the_exit_is_unchanged(tmp_path, capsys):
    upstream, bus = make_upstream_and_bus(tmp_path)
    (upstream / "tools" / "ok.py").unlink()
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "two removes the script")
    git(bus, "fetch", "-q", "origin")
    item = item_file(tmp_path, adopt("python tools/ok.py go"))
    assert exercise(tmp_path, item, bus) == 0                     # the stale bus still has the script
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_OK"] and "ROUTE_BUS_STALE 1" in captured.err
    git(bus, "merge", "-q", "--ff-only", "origin/master")          # caught up: the script is gone, and no warning
    assert exercise(tmp_path, item, bus) == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_MISSING"] and "ROUTE_BUS_STALE" not in captured.err


def test_a_bus_without_an_origin_does_not_warn(tmp_path, capsys):
    bus = make_bus(tmp_path, {"tools/ok.py": STUB_OK})
    item = item_file(tmp_path, adopt("python tools/ok.py go"))
    assert exercise(tmp_path, item, bus) == 0
    assert "ROUTE_BUS_STALE" not in capsys.readouterr().err


# ---------- H6: a bulleted or quoted ruling-candidate lead ----------

@pytest.mark.parametrize("body", [
    "### mlv-app - x\n\n- **Ruling candidate:** the hook is deny-only.\n",
    "### mlv-app - x\n\n> Ruling candidate: the hook is deny-only.\n",
    "### mlv-app - x\n\n* Ruling-candidate (not law): the hook is deny-only.\n",
    "### mlv-app - x\n\n> - **Ruling candidate** the hook is deny-only.\n",
])
def test_h6_a_bulleted_or_quoted_lead_still_needs_the_list(body):
    assert refusal_code(body) == "RULING_CANDIDATE_NO_SUPERSEDES"


def test_h6_a_bulleted_lead_with_the_list_passes():
    body = "### mlv-app - x\n\n- **Ruling candidate:** the hook is deny-only.\n\ntouches/supersedes: R14.8\n"
    assert refusal_code(body) is None


# ---------- H7: Windows fidelity ----------

@pytest.mark.skipif(os.name != "nt", reason="a .cmd shim and a backslash path are Windows things")
def test_h7_a_cmd_shim_is_route_missing_with_its_reason(tmp_path, monkeypatch):
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "fancytool.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(shims) + os.pathsep + os.environ["PATH"])
    status, detail = ob.exercise_route("fancytool run", tmp_path, 30, {})
    assert status == "ROUTE_MISSING" and "shim" in detail


@pytest.mark.skipif(os.name != "nt", reason="a backslash path in route text is a Windows thing")
def test_h7_a_backslash_path_in_route_text_is_kept(tmp_path):
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "ok.py").write_text(STUB_OK, encoding="utf-8")
    assert ob.exercise_route("python tools\\ok.py go", tmp_path, 30, {}) == ("ROUTE_OK", "")
    assert ob.split_route('python "tools\\ok.py" "a b"') == ["python", "tools\\ok.py", "a b"]


def test_h7_a_character_the_stream_cannot_encode_does_not_raise():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    ob._route_say("ROUTE_OK item python tools/ok.py caf\u00e9 \u2192 \u65e5\u672c", stream)
    stream.flush()
    assert stream.buffer.getvalue().startswith(b"ROUTE_OK item")
