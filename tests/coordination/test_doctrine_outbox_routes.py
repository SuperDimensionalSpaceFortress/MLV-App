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
import os
import subprocess
import sys
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
    status, _detail = ob.exercise_route("python tools/mark.py <marker>", tmp_path, 30, {"marker": str(marker)}) \
        if (tmp_path / "tools" / "mark.py").exists() else ("ROUTE_MISSING", "")
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
