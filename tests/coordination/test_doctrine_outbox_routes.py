"""Tests for `doctrine_outbox.py validate --exercise` (card routes), fail-closed. CoS work order FWO-DEFECT-PREVENT-1
item 3 (MLV-App PR 454 comment 6227522231; hub ACK 6228165753, final scope 6263546558). Every repository is a throwaway
git repository under pytest's tmp_path; nothing touches the network, the real bus or its clone. Lives under
tests/coordination/ because CI runs that directory by name.

CARD-ROUTE-EXERCISE-2 replaces CARD-ROUTE-EXERCISE-1 (PR 479, parked). That card launched launcher routes (`python x.py`,
`pwsh -File x.ps1`) after two hand-written walks over one argv (a safety matcher and a script discovery) agreed the script
was inside the clone, and every review round found a spelling where they disagreed. This card:
  (a) checks that every route's program and every script it names EXIST, running nothing (a route that is not run included);
  (b) runs a route ONLY when its program is on the execution ALLOWLIST (`git`, by its bare name and its resolved executable
      identity); every other program is ROUTE_NOT_EXECUTED, a skip (r1 kept a denylist of launcher names, so any code-running
      program it did not list, pytest for one, was executed; r2 inverts it);
  (c) refuses every known PR 479 bypass repro before launch: one row each, the process launch mocked and asserted NOT called;
  (d) strips the interpreter-injection and git editor/pager/helper environment from a route that is run, and refuses the git
      forms that start a helper (`-c`, signing, `merge -s`, `--exec`, `--upload-pack`, `!` aliases, ...) as ROUTE_UNSAFE.

Seed RED rows come from the failures the CoS found by hand:
  #468 r3/r4  an adoption step `python factory-pins/pin-runner.py pin ...` against a bus without that file
  #469 r1     `doctrine-sync debt` named as the route, a command no bus carried

Named mutations (each turns the named rows red, then is reverted):
  M1  route_missing_reason returns None (no static check)        -> the (a) rows: test_a_*, test_seed_*
  M2  route_runs returns True (every program is launched)         -> the (b)/(c) rows: test_b_*, test_c_*
  M3  route_exercise_env returns the environment untouched        -> the (d) rows: test_d_*
"""
from __future__ import annotations

import importlib.util
import io
import os
import shutil
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
STUB = "print('stub')\n"


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


LAUNCHER_PROGRAMS = ["sh", "bash", "zsh", "dash", "cmd", "pwsh", "powershell", "python", "python3", "py", "node", "deno",
                     "bun", "awk", "tclsh", "perl", "ruby", "php", "env", "xargs", "nohup", "timeout"]
UNLISTED_PROGRAMS = ["pytest", "pyw", "ipython", "java", "go", "php-cgi", "jq"]   # names r1's launcher denylist did not know


def fake_programs(monkeypatch, tmp_path: Path, *names: str) -> None:
    """Put an empty executable named `name` on PATH for each, AFTER the real PATH (a fake `sh` ahead of it would break git's own
    clone), so the static check finds a program the host lacks and a real one still wins where the host has it."""
    folder = tmp_path / "fakebin"
    folder.mkdir(exist_ok=True)
    for name in names:
        path = folder / (f"{name}.exe" if os.name == "nt" else name)
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", os.environ["PATH"] + os.pathsep + str(folder))


@pytest.fixture
def launchers(monkeypatch, tmp_path):
    fake_programs(monkeypatch, tmp_path, *LAUNCHER_PROGRAMS, *UNLISTED_PROGRAMS)


@pytest.fixture
def launches(monkeypatch):
    """Mock the ONE place a route is launched. The list records every call; the mock reports a clean exit."""
    calls: list[dict] = []

    def fake(argv, cwd, env, timeout):
        calls.append({"argv": list(argv), "cwd": cwd, "env": dict(env), "timeout": timeout})
        return 0, "", ""

    monkeypatch.setattr(ob, "run_route_process", fake)
    return calls


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


def make_clone(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """A stand-in for the scratch clone: a plain directory with files, for the unit rows that never clone."""
    clone = tmp_path / "clone"
    clone.mkdir(exist_ok=True)
    for rel, text in (files or {}).items():
        path = clone / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return clone


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


def check_argv(argv: list[str], clone: Path, bus: Path | None = None) -> tuple[str, str]:
    """exercise_route past the parse: the argv is given whole, so no quoting rule can differ between platforms."""
    return ob._exercise_route_argv(list(argv), clone, 30, {}, bus, [])


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


# ---------- (a) the static existence check: every route, nothing runs ----------

def test_a_a_missing_program_is_route_missing_for_a_plain_route_and_runs_nothing(tmp_path, launches):
    clone = make_clone(tmp_path)
    status, detail = check_argv(["no-such-program-xyz", "debt"], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("PROGRAM_NOT_FOUND:") and not launches


def test_a_a_missing_program_is_route_missing_for_a_launcher_route_too(tmp_path, launches, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))                  # no interpreter anywhere
    clone = make_clone(tmp_path, {"tools/x.py": STUB})
    for argv in (["python", "tools/x.py"], ["pwsh", "-File", "tools/x.ps1"], ["env", "x"], ["bash", "tools/x.sh"]):
        status, detail = check_argv(argv, clone)
        assert status == "ROUTE_MISSING" and detail.startswith("PROGRAM_NOT_FOUND:"), argv
    assert not launches


def test_a_a_launcher_naming_a_missing_script_is_route_missing_and_not_launched(tmp_path, launchers, launches):
    clone = make_clone(tmp_path)
    status, detail = check_argv(["python", "tools/gone.py", "go"], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_NOT_FOUND:") and "tools/gone.py" in detail
    assert not launches


def test_a_a_launcher_naming_an_existing_script_is_a_skip_not_a_failure(tmp_path, launchers, launches):
    clone = make_clone(tmp_path, {"tools/x.py": STUB})
    status, detail = check_argv(["python", "tools/x.py", "go"], clone)
    assert status == "ROUTE_NOT_EXECUTED" and "LAUNCHER-ALLOWLIST-1" in detail
    assert not launches
    assert status in ob.ROUTE_PASSING


@pytest.mark.parametrize("argv, script", [
    (["python", "-X", "utf8", "tools/gone.py"], "tools/gone.py"),
    (["py", "-3", "tools/gone.py"], "tools/gone.py"),
    (["python", "-W", "ignore", "-B", "tools/gone.py"], "tools/gone.py"),
    (["node", "tools/gone.js"], "tools/gone.js"),
    (["bash", "tools/gone.sh", "-c"], "tools/gone.sh"),
    (["pwsh", "-NoProfile", "-File", "tools/gone.ps1"], "tools/gone.ps1"),
    (["pwsh", "-NoProfile", "tools/gone.ps1"], "tools/gone.ps1"),
    (["perl", "tools/gone.pl"], "tools/gone.pl"),
    (["ruby", "tools/gone.rb"], "tools/gone.rb"),
    (["php", "tools/gone.php"], "tools/gone.php"),
    # sol r1 blocker 3: the paths r1 never looked at
    (["deno", "run", "tools/definitely-missing.ts"], "tools/definitely-missing.ts"),
    (["deno", "run", "--allow-read", "-c", "tools/deno.json", "tools/gone.ts"], "tools/gone.ts"),   # -c takes an operand: not the script
    (["deno", "test", "tools/gone.ts"], "tools/gone.ts"),
    (["deno", "tools/gone.ts"], "tools/gone.ts"),
    (["bun", "run", "tools/definitely-missing.js"], "tools/definitely-missing.js"),
    (["bun", "tools/gone.ts"], "tools/gone.ts"),
    (["bun", "run", "gone.mjs"], "gone.mjs"),
    (["bun", "--preload", "tools/gone-preload.ts", "run", "tools/ok.ts"], "tools/gone-preload.ts"),
    (["node", "--require", "tools/gone-preload.js", "tools/ok.js"], "tools/gone-preload.js"),
    (["node", "-r", "tools/gone-preload.js", "tools/ok.js"], "tools/gone-preload.js"),
    (["node", "--require=tools/gone-preload.js", "tools/ok.js"], "tools/gone-preload.js"),
    (["node", "--import", "tools/gone-preload.mjs", "tools/ok.js"], "tools/gone-preload.mjs"),
    (["node", "--loader=tools/gone-loader.mjs", "tools/ok.js"], "tools/gone-loader.mjs"),
    (["bash", "--rcfile", "tools/gone.rc", "tools/ok.sh"], "tools/gone.rc"),
    (["bash", "--init-file=tools/gone.rc", "tools/ok.sh"], "tools/gone.rc"),
    (["awk", "-f", "tools/gone.awk", "data.txt"], "tools/gone.awk"),
    (["gawk", "--file=tools/gone.awk", "data.txt"], "tools/gone.awk"),
    (["tclsh", "tools/gone.tcl"], "tools/gone.tcl"),
])
def test_a_every_launcher_family_has_its_named_script_checked(tmp_path, launchers, launches, argv, script):
    clone = make_clone(tmp_path, {"tools/ok.js": STUB, "tools/ok.ts": STUB, "tools/ok.sh": STUB})
    status, detail = check_argv(argv, clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_NOT_FOUND:") and script in detail, (argv, detail)
    assert not launches


@pytest.mark.parametrize("argv", [
    ["node", "-r", "{o}/outside.js", "tools/ok.js"],
    ["node", "--require={o}/outside.js", "tools/ok.js"],
    ["node", "--import", "{o}/outside.js", "tools/ok.js"],
    ["bash", "--rcfile", "{o}/outside.sh", "tools/ok.sh"],
    ["bun", "--preload", "{o}/outside.js", "run", "tools/ok.js"],
    ["deno", "run", "{o}/outside.js"],
    ["bun", "run", "{o}/outside.js"],
    ["awk", "-f", "{o}/outside.sh", "data.txt"],
])
def test_a_a_preload_or_runner_target_outside_the_clone_is_outside(tmp_path, launchers, launches, argv):
    clone, outside = bypass_layout(tmp_path)
    status, detail = check_argv([t.replace("{o}", str(outside).replace("\\", "/")) for t in argv], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:"), (argv, status, detail)
    assert not launches


@pytest.mark.parametrize("argv", [
    ["bun", "run", "build"], ["bun", "run", "test"], ["bun", "install"], ["deno", "task", "build"], ["deno", "fmt"],
    ["deno", "eval", "1"], ["bun", "-e", "1"], ["awk", "{print}", "data.txt"], ["node", "-r", "tools/ok.js", "-e", "1"],
])
def test_a_a_runner_naming_no_file_is_checked_for_its_program_only(tmp_path, launchers, launches, argv):
    clone = make_clone(tmp_path, {"tools/ok.js": STUB})
    assert check_argv(argv, clone)[0] == "ROUTE_NOT_EXECUTED", argv
    assert not launches


def test_a_deno_and_bun_with_the_script_in_place_are_skips(tmp_path, launchers, launches):
    clone = make_clone(tmp_path, {"tools/ok.ts": STUB, "tools/ok.js": STUB, "tools/pre.ts": STUB})
    for argv in (["deno", "run", "tools/ok.ts"], ["bun", "run", "tools/ok.js"], ["bun", "--preload", "tools/pre.ts", "tools/ok.ts"],
                 ["node", "-r", "tools/ok.js", "tools/ok.js"]):
        assert check_argv(argv, clone)[0] == "ROUTE_NOT_EXECUTED", argv
    assert not launches


@pytest.mark.parametrize("argv", [
    ["python", "-m", "pytest", "tests"],
    ["python", "-c", "print(1)"],
    ["bash", "-c", "echo hi"],
    ["bash", "-lc", "echo hi"],
    ["node", "-e", "1"],
    ["pwsh", "-Command", "Get-Date"],
    ["pwsh", "-c", "Get-Date"],
    ["pwsh", "-enc", "ZQBjAGgAbwA="],
    ["env", "git", "status"],
    ["python"],
])
def test_a_a_route_naming_no_file_is_checked_for_its_program_only(tmp_path, launchers, launches, argv):
    clone = make_clone(tmp_path)
    assert check_argv(argv, clone)[0] == "ROUTE_NOT_EXECUTED", argv
    assert not launches


def test_a_script_resolving_outside_the_clone_is_typed_and_not_launched(tmp_path, launchers, launches):
    clone = make_clone(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text(STUB, encoding="utf-8")
    for script in (str(outside), "../outside.py", "sub/../../outside.py"):
        status, detail = check_argv(["python", script], clone)
        assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:"), (script, detail)
    assert not launches


def test_a_a_sibling_directory_sharing_the_clone_name_prefix_is_outside(tmp_path, launchers, launches):
    clone = make_clone(tmp_path)
    sibling = tmp_path / "clone-evil"
    sibling.mkdir()
    (sibling / "x.py").write_text(STUB, encoding="utf-8")
    status, detail = check_argv(["python", "../clone-evil/x.py"], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:")
    assert not launches


def test_a_an_outward_symlink_inside_the_clone_is_outside(tmp_path, launchers, launches):
    clone = make_clone(tmp_path, {"tools/ok.py": STUB})
    outside = tmp_path / "outside.py"
    outside.write_text(STUB, encoding="utf-8")
    try:
        (clone / "tools" / "link.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege on this host")
    status, detail = check_argv(["python", "tools/link.py"], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:")
    assert not launches


@pytest.mark.skipif(os.name != "nt", reason="a directory junction is a Windows thing; the symlink row covers POSIX")
def test_a_an_outward_junction_inside_the_clone_is_outside(tmp_path, launchers, launches):
    clone = make_clone(tmp_path)
    away = tmp_path / "away"
    away.mkdir()
    (away / "x.py").write_text(STUB, encoding="utf-8")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(clone / "jn"), str(away)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip("could not make a junction")
    status, detail = check_argv(["python", "jn/x.py"], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:")
    assert not launches


def test_a_a_program_path_inside_the_clone_resolves(tmp_path):
    clone = make_clone(tmp_path, {"tools/mytool.exe": "", "tools/mytool": ""})
    assert ob.route_resolve_program("tools/mytool", clone) is not None
    assert ob.route_resolve_program("tools/no-tool", clone) is None


@pytest.mark.skipif(os.name != "nt", reason="a .cmd shim is a Windows thing")
def test_a_a_cmd_shim_standing_in_for_git_is_typed_but_any_other_shim_is_only_existence_checked(tmp_path, monkeypatch, launches):
    shims = tmp_path / "shims"
    shims.mkdir()
    for name in ("git", "fancytool", "npm"):
        (shims / f"{name}.cmd").write_text("@echo off\r\n", encoding="utf-8")
    monkeypatch.setenv("PATH", str(shims) + os.pathsep + os.environ["PATH"])
    clone = make_clone(tmp_path)
    status, detail = check_argv(["git", "log"], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("PROGRAM_IS_SHIM:") and "shim" in detail
    assert check_argv(["fancytool", "run"], clone)[0] == "ROUTE_NOT_EXECUTED"
    assert check_argv(["npm", "test"], clone)[0] == "ROUTE_NOT_EXECUTED"
    assert not launches


@pytest.mark.skipif(os.name != "nt", reason="a backslash path in route text is a Windows thing")
def test_a_a_backslash_path_in_route_text_is_kept(tmp_path, launchers, launches):
    clone = make_clone(tmp_path, {"tools/ok.py": STUB})
    assert ob.exercise_route("python tools\\ok.py go", clone, 30, {})[0] == "ROUTE_NOT_EXECUTED"
    assert ob.split_route('python "tools\\ok.py" "a b"') == ["python", "tools\\ok.py", "a b"]
    assert not launches


# ---------- the five CoS seeds, through the CLI and a real scratch clone ----------

def test_seed_468_missing_runner_script_is_route_missing(tmp_path, capsys, launchers, launches):
    """#468: the card's adopt step runs factory-pins/pin-runner.py; the bus keeps it at tools/factory/."""
    bus = make_bus(tmp_path, {"tools/factory/pin-runner.py": STUB})
    cmd = "python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --reviewer <who> --reason <why>"
    item = item_file(tmp_path, adopt(cmd))
    assert exercise(tmp_path, item, bus, "--fill", "bus-sha=abc1234", "--fill", "who=me", "--fill", "why=because") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_MISSING"] and "SCRIPT_NOT_FOUND" in captured.err
    assert not launches


def test_seed_468_a_launcher_route_with_its_script_in_place_validates_without_being_run(tmp_path, capsys, launchers, launches):
    bus = make_bus(tmp_path, {"factory-pins/pin-runner.py": STUB})
    cmd = "python factory-pins/pin-runner.py pin doctrine-outbox --commit <bus-sha> --reviewer <who> --reason <why>"
    item = item_file(tmp_path, adopt(cmd))
    assert exercise(tmp_path, item, bus, "--fill", "bus-sha=abc1234", "--fill", "who=me", "--fill", "why=because") == 0
    out = capsys.readouterr().out
    assert statuses(out) == ["ROUTE_NOT_EXECUTED"]
    assert "<bus-sha>" in route_lines(out)[0]       # the line shows the command as the card wrote it
    assert not launches


def test_seed_469_unknown_program_is_route_missing(tmp_path, capsys, launches):
    """#469: `doctrine-sync debt` is not a program on any bus."""
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("doctrine-sync-not-a-program debt"))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_MISSING"]
    assert not launches


def test_seed_469_a_file_the_bus_lacks_is_route_failed_and_green_once_it_has_it(tmp_path, capsys):
    """Run for real (git is not a launcher): `git cat-file -e HEAD:<path>` exits non-zero for a path the bus lacks."""
    route = "git cat-file -e HEAD:tools/doctrine_sync.py"
    bare = make_bus(tmp_path)
    assert exercise(tmp_path, item_file(tmp_path, adopt(route)), bare) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_FAILED"]
    (tmp_path / "bus").rename(tmp_path / "bus-old")
    stocked = make_bus(tmp_path, {"tools/doctrine_sync.py": STUB})
    assert exercise(tmp_path, item_file(tmp_path, adopt(route), name="20261017-route-fixture-2.md"), stocked) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"]


def test_output_is_one_line_per_command_with_item_name(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git log --oneline", "git rev-parse HEAD"))
    assert exercise(tmp_path, item, bus) == 0
    assert route_lines(capsys.readouterr().out) == [
        "ROUTE_OK 20261017-route-fixture.md git log --oneline",
        "ROUTE_OK 20261017-route-fixture.md git rev-parse HEAD",
    ]


def test_an_item_naming_no_route_passes_and_says_so(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, "### mlv-app, 2026-10-17 - prose only\n\nNo command here, only `RULINGS.md`.\n")
    assert exercise(tmp_path, item, bus) == 0
    assert "ROUTE_NONE 20261017-route-fixture.md" in capsys.readouterr().out


def test_a_card_target_item_is_checked_like_any_other(tmp_path, capsys):
    bus = make_bus(tmp_path)
    body = ("## mlv-app/route-fixture\nrule: r\nmechanism: m\napplies: mlv-app\ncheck: c\nevidence: measured\n"
            "- **Adopt:** run `git log --oneline` then `no-such-program-xyz go`.\n")
    item = item_file(tmp_path, body, target="specs/mlv-app/cards.md", kind="trap")
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK", "ROUTE_MISSING"]


def test_an_item_that_fails_validation_is_not_checked(tmp_path, capsys, launches):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git log --oneline"), name="not-a-dated-name.md")
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == [] and not launches


def test_a_launcher_skip_does_not_fail_the_run_but_a_missing_route_beside_it_does(tmp_path, capsys, launchers):
    bus = make_bus(tmp_path, {"tools/x.py": STUB})
    assert exercise(tmp_path, item_file(tmp_path, adopt("python tools/x.py go")), bus) == 0
    capsys.readouterr()
    item = item_file(tmp_path, adopt("python tools/x.py go", "python tools/gone.py go"), name="20261017-route-fixture-2.md")
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_NOT_EXECUTED", "ROUTE_MISSING"]


# ---------- (b) only a route whose program is on the execution allowlist (git) is run ----------

LAUNCHER_ROUTES = [
    "sh tools/x.sh", "bash tools/x.sh", "zsh tools/x.sh", "dash tools/x.sh", "cmd /c tools/x.cmd", "pwsh -NoProfile -File tools/x.ps1",
    "powershell -File tools/x.ps1", "python tools/x.py", "python3 tools/x.py", "py -3 tools/x.py", "node tools/x.js", "deno run tools/x.ts",
    "perl tools/x.pl", "ruby tools/x.rb", "php tools/x.php", "env git status", "xargs git status", "nohup git status",
    "timeout 5 git status", "python3.12 tools/x.py",
]


@pytest.mark.parametrize("command", LAUNCHER_ROUTES)
def test_b_a_launcher_route_is_never_launched(tmp_path, launchers, launches, command):
    fake_programs = ["python3.12"]                                       # a versioned python is a launcher too
    for name in fake_programs:
        path = tmp_path / "fakebin" / (f"{name}.exe" if os.name == "nt" else name)
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
    clone = make_clone(tmp_path, {f"tools/x.{ext}": STUB for ext in ("sh", "cmd", "ps1", "py", "js", "ts", "pl", "rb", "php")})
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status == "ROUTE_NOT_EXECUTED", (command, status, detail)
    assert not launches


def test_b_only_git_by_its_bare_name_is_on_the_execution_allowlist():
    assert ob.ROUTE_EXEC_ALLOW == frozenset({"git"})
    for program in ("git", "git.exe"):
        assert ob.route_runs([program, "log"]), program
    if os.name == "nt":                                                  # the filesystem folds case there, and only there
        assert ob.route_runs(["GIT.EXE", "log"]) and ob.route_runs(["Git", "log"])
    for program in ("pytest", "python", "python3.12", "pwsh", "bash", "node", "jq", "ls", "grep", "env", "git-lfs", "gitk", "git2",
                    "git.", "git ", "git.exe.", "/usr/bin/git", "./git", "tools/git", "C:\\Program Files\\Git\\cmd\\git.exe",
                    "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\POWERS~1.EXE", "C:\\Windows\\py.exe.", ""):
        assert not ob.route_runs([program, "x"]), program
    assert not ob.route_runs([])


@pytest.mark.parametrize("command", [
    "pytest <o>/test_evil.py", "pyw <o>/evil.py", "ipython <o>/x.py", "java <o>/X.java", "go run <o>/m.go", "php-cgi <o>/x.php",
    "jq -f <o>/x.jq .", "pytest -p evil_plugin tests", "pytest tests -q",
])
def test_b_a_code_running_program_off_the_allowlist_is_never_launched(tmp_path, launchers, launches, command):
    """r1 pinned `pytest` as a non-launcher and ran it, and so ran Python it named (a test file, conftest, -p plugin, env)."""
    clone, outside = bypass_layout(tmp_path)
    status, detail = ob.exercise_route(command, clone, 30, {"o": str(outside).replace("\\", "/")})
    assert status in ("ROUTE_NOT_EXECUTED", "ROUTE_MISSING"), (command, status, detail)
    assert launches == [], f"{command} reached the process launch"


def test_b_pytest_is_not_executed_but_a_missing_program_is_still_missing(tmp_path, launchers, launches):
    clone = make_clone(tmp_path)
    status, detail = check_argv(["pytest", "tests", "-q"], clone)
    assert status == "ROUTE_NOT_EXECUTED" and "allowlist" in detail and launches == []
    status, detail = check_argv(["no-such-test-runner-xyz", "tests"], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("PROGRAM_NOT_FOUND:") and launches == []


@pytest.mark.parametrize("spelling", ["8.3 short name", "trailing dot", "path to git", "program in the clone"])
def test_b_a_spelling_that_dodges_a_name_list_is_still_not_launched(tmp_path, launchers, launches, spelling):
    clone, outside = bypass_layout(tmp_path)
    folder = tmp_path / "fakebin"
    shutil.copy(folder / ("python.exe" if os.name == "nt" else "python"), folder / "POWERS~1.EXE")
    program = {
        "8.3 short name": str(folder / "POWERS~1.EXE"),
        "trailing dot": str(folder / ("python.exe" if os.name == "nt" else "python")) + ".",     # Windows folds the dot away
        "path to git": shutil.which("git"),
        "program in the clone": "tools/ok.py",
    }[spelling]
    status, detail = check_argv([program, f"{str(outside).replace(chr(92), '/')}/outside.py"], clone)
    assert status != "ROUTE_OK", (spelling, status, detail)
    assert launches == [], f"{spelling} reached the process launch"


def test_b_a_renamed_or_linked_interpreter_is_read_by_what_it_resolves_to_and_never_run(tmp_path, launchers, launches, monkeypatch):
    clone, outside = bypass_layout(tmp_path)
    folder = tmp_path / "fakebin"
    real = folder / ("python.exe" if os.name == "nt" else "python")
    (clone / "tools" / "copy.exe").write_text("", encoding="utf-8")        # a renamed COPY: no way to read it back as python
    outside_py = f"{str(outside).replace(chr(92), '/')}/outside.py"
    assert check_argv(["tools/copy.exe", outside_py], clone)[0] == "ROUTE_NOT_EXECUTED"
    with monkeypatch.context() as patched:                                   # sol's in-memory resolver repro (no link privilege needed)
        patched.setattr(ob, "route_resolve_program", lambda program, cwd: str(real))
        status, detail = check_argv(["tools/helper.exe", "../outside.py"], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:"), (status, detail)
    assert launches == []
    try:
        (clone / "tools" / "helper.exe").symlink_to(real)                  # a LINK: its real name is python, so its script is checked
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege on this host; the resolver half above ran")
    status, detail = check_argv(["tools/helper.exe", outside_py], clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:"), (status, detail)
    assert launches == []


def test_b_the_git_that_runs_is_the_system_git_never_one_in_the_clone(tmp_path, launches, monkeypatch):
    clone = make_clone(tmp_path)
    shadow = clone / "bin"
    shadow.mkdir()
    (shadow / ("git.exe" if os.name == "nt" else "git")).write_text("", encoding="utf-8")
    (shadow / "git.cmd").write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(shadow) + os.pathsep + os.environ["PATH"])
    status, detail = ob.exercise_route("git log", clone, 30, {})
    assert status == "ROUTE_OK" and len(launches) == 1, (status, detail)
    ran = ob.route_real_path(launches[0]["argv"][0])
    assert not ob.route_inside(ran, ob.route_real_path(str(clone))) and Path(ran).stem == "git"


def test_b_a_git_only_inside_the_clone_is_refused(tmp_path, launches, monkeypatch):
    clone = make_clone(tmp_path)
    shadow = clone / "bin"
    shadow.mkdir()
    (shadow / ("git.exe" if os.name == "nt" else "git")).write_text("", encoding="utf-8")
    monkeypatch.setenv("PATH", str(shadow))
    status, detail = ob.exercise_route("git log", clone, 30, {})
    assert status == "ROUTE_UNSAFE" and "scratch clone" in detail and launches == []


def test_b_a_relative_path_entry_never_finds_git(tmp_path, launches, monkeypatch):
    clone = make_clone(tmp_path)
    (clone / ("git.exe" if os.name == "nt" else "git")).write_text("", encoding="utf-8")
    monkeypatch.chdir(clone)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", os.environ["PATH"]]))
    status, detail = ob.exercise_route("git log", clone, 30, {})
    assert status == "ROUTE_OK" and len(launches) == 1, (status, detail)
    assert not ob.route_inside(ob.route_real_path(launches[0]["argv"][0]), ob.route_real_path(str(clone)))


def test_b_git_resolution_never_consults_the_current_directory(tmp_path, launches, monkeypatch):
    """shutil.which looks in the current directory before PATH on win32. Resolution must not: git.exe, git.cmd, git.bat and a bare
    `git` planted in the clone (the cwd while resolving) are never found, so the answer is the system git, or a refusal when
    the system has none."""
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)   # a host that sets it would hide the bug; resolution must not depend on it
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    system = shutil.which("git")                                          # the system git, looked up from an empty cwd
    clone = make_clone(tmp_path)
    for name in ("git.exe", "git.cmd", "git.bat", "git"):
        planted = clone / name
        planted.write_text("", encoding="utf-8")
        planted.chmod(0o755)
    monkeypatch.chdir(clone)
    found, refused = ob.route_git_binary(clone)
    if system is None:
        assert found is None and refused is not None and refused[0] in ("ROUTE_MISSING", "ROUTE_UNSAFE"), (found, refused)
    else:
        assert refused is None and found is not None, (found, refused)
        assert not ob.route_inside(ob.route_real_path(found), ob.route_real_path(str(clone)))
        assert ob.route_real_path(found) == ob.route_real_path(system)
    status, detail = ob.exercise_route("git log", clone, 30, {})
    if system is None:
        assert status in ("ROUTE_MISSING", "ROUTE_UNSAFE") and launches == [], (status, detail)
    else:
        assert status == "ROUTE_OK" and len(launches) == 1, (status, detail)
        assert not ob.route_inside(ob.route_real_path(launches[0]["argv"][0]), ob.route_real_path(str(clone)))


def test_b_a_git_that_resolves_to_another_program_is_refused(tmp_path, launches, monkeypatch):
    clone = make_clone(tmp_path)
    other = tmp_path / "elsewhere" / ("python.exe" if os.name == "nt" else "python")
    other.parent.mkdir()
    other.write_text("", encoding="utf-8")
    monkeypatch.setattr(ob, "route_find_git", lambda entries: str(other))   # a `git` on PATH that really is python
    status, detail = ob.exercise_route("git log", clone, 30, {})
    assert status == "ROUTE_UNSAFE" and "not a git binary" in detail and launches == []


def test_b_a_route_with_no_launcher_is_run_once_in_the_scratch_clone_with_the_resolved_program(tmp_path, launches):
    bus = make_bus(tmp_path)
    scratch = tmp_path / "scratch"
    item = item_file(tmp_path, adopt("git log --oneline"))
    assert exercise(tmp_path, item, bus, scratch=scratch) == 0
    assert len(launches) == 1
    call = launches[0]
    assert Path(call["argv"][0]).stem.lower() == "git" and call["argv"][-2:] == ["log", "--oneline"]
    assert call["cwd"].name == "bus" and call["cwd"].parent.parent == scratch and call["cwd"].resolve() != bus.resolve()


def test_b_a_non_launcher_route_that_exits_non_zero_is_route_failed_and_zero_is_ok(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git rev-parse --verify no-such-ref", "git rev-parse HEAD"))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_FAILED", "ROUTE_OK"]


def test_b_a_timeout_is_reported_as_route_timeout_and_the_flag_reaches_the_launch(tmp_path, capsys, monkeypatch):
    seen: list[float] = []
    monkeypatch.setattr(ob, "run_route_process", lambda argv, cwd, env, timeout: (seen.append(timeout), (None, "", ""))[1])
    bus = make_bus(tmp_path)
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log")), bus, "--timeout", "0.5") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_TIMEOUT"] and seen == [0.5]


def test_the_scratch_placeholder_names_the_scratch_clone(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git -C <scratch> log --oneline"))
    assert exercise(tmp_path, item, bus) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"]


def test_unfilled_placeholder_is_route_failed_and_not_launched(tmp_path, capsys, launches):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git log <rev> <who>"))
    assert exercise(tmp_path, item, bus, "--fill", "rev=HEAD") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_FAILED"] and "--fill who=VALUE" in captured.err
    assert not launches


def test_a_filled_windows_path_is_not_reparsed(tmp_path, launches):
    """The value is substituted after the line is split, so its backslashes survive."""
    clone = make_clone(tmp_path)
    value = "C:\\some dir\\back\\slash"
    status, _detail = ob.exercise_route("git log <where>", clone, 30, {"where": value})
    assert status == "ROUTE_OK" and launches[0]["argv"][-1] == value and launches[0]["argv"][-2] == "log"


def test_b_an_unfilled_placeholder_on_a_route_that_is_not_run_is_noted_and_not_a_failure(tmp_path, launchers, launches):
    """r1 failed `python factory-pins/pin-runner.py pin --commit <bus-sha>` with ROUTE_FAILED though it is never run."""
    clone = make_clone(tmp_path, {"tools/x.py": STUB})
    status, detail = ob.exercise_route("python tools/x.py pin --commit <bus-sha> --reviewer <who>", clone, 30, {})
    assert status == "ROUTE_NOT_EXECUTED" and "<bus-sha>" in detail and "<who>" in detail, detail
    status, detail = ob.exercise_route("python <script> go", clone, 30, {})
    assert status == "ROUTE_NOT_EXECUTED" and "<script>" in detail, detail
    status, detail = ob.exercise_route("<tool> go", clone, 30, {})
    assert status == "ROUTE_NOT_EXECUTED" and "<tool>" in detail, detail
    status, detail = ob.exercise_route("python tools/gone.py <arg>", clone, 30, {})
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_NOT_FOUND:"), detail
    status, detail = ob.exercise_route("python tools/x.py <arg>", clone, 30, {"arg": "go"})
    assert status == "ROUTE_NOT_EXECUTED" and "unfilled" not in detail, detail
    assert launches == []


def test_b_the_steward_card_of_this_pr_validates_without_fills_and_runs_nothing(tmp_path, capsys, launchers, launches):
    bus = make_bus(tmp_path, {"factory-pins/pin-runner.py": STUB})
    card = REPO_ROOT / "doctrine-outbox" / "20261017-doctrine-outbox-route-exercise-candidate.md"
    assert exercise(tmp_path, card, bus) == 0
    out = capsys.readouterr().out
    assert statuses(out) == ["ROUTE_NOT_EXECUTED"] * 2, out
    assert launches == []


# ---------- (b) unsafe routes are refused before launch (the screen reads every route and runs nothing) ----------

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
    "git log --token abc",
    "git log ANTHROPIC_API_KEY=abc",
    "git log | git log",
    "git log && git log",
    "git log > elsewhere.txt",
    "FOO=1 git status",
    "git -c core.pager=evil log",
    "git -c alias.ship=push ship",
    "git --config-env=a=B log",
    "git ship",
    "git config exercise.probe yes",
    "git config --add exercise.probe yes",
    "git config --unset exercise.probe",
    "git log --output=elsewhere.txt",
    "git grep -Ofalse needle",
    "find . -exec git push ;",
    "npx claude auth login",
    "git pull --ff-only",
    "git fetch origin",
    "git push origin master",
    "git remote update",
    "git clone https://example.invalid/x.git",
    "git clone example.com:o/r",
    "git submodule update --init",
]


@pytest.mark.parametrize("command", UNSAFE)
def test_an_unsafe_route_is_refused_before_launch_and_fails_the_item(tmp_path, capsys, launches, command):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt(command))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"]
    assert not launches


@pytest.mark.parametrize("command", ["git -C .. status", "git -C / status", "git --git-dir=.. log", "git -C sub/../.. log"])
def test_git_aimed_outside_the_scratch_clone_is_unsafe(tmp_path, command):
    (tmp_path / "sub").mkdir(exist_ok=True)
    reason = ob.route_unsafe_reason(ob.split_route(command), tmp_path)
    assert reason is not None and "outside the scratch clone" in reason, command


def test_git_aimed_at_an_absolute_outside_path_is_unsafe(tmp_path):
    reason = ob.route_unsafe_reason(["git", "-C", str(tmp_path.parent), "status"], tmp_path / "scratch")
    assert reason is not None and "outside the scratch clone" in reason


def test_scp_style_remote_is_a_network_target():
    reason = ob.route_unsafe_reason(["git", "fetch", "deploy@example.invalid:o/r.git"])
    assert reason is not None and "network target" in reason


@pytest.mark.parametrize("command", [
    "git log --oneline", "git -C . status --short", "git rev-parse HEAD", "git config --get user.name", "git config --list",
    "git -C sub log", "pytest tests -q", "jq . x.json", "doctrine-sync-not-a-program debt",
])
def test_safe_look_alikes_are_not_refused(tmp_path, command):
    (tmp_path / "sub").mkdir(exist_ok=True)
    assert ob.route_unsafe_reason(ob.split_route(command), tmp_path) is None, command


@pytest.mark.parametrize("command", [
    "cmd /c git push", "env gh pr create", "sudo git status", "xargs git push", "start notepad", "time git status",
    'bash -c "git push"', 'bash -lc "git push"', "pwsh -c 'git push'", "pwsh -Command 'git push'", "pwsh -enc ZQBjAGgAbwA=",
    'python -c "print(1)"', "python -m http.server", "python -m pip install requests", "node -e 1", "ruby -e 1",
    "npm publish --dry-run", "npm install", "pip install requests", "yarn add left-pad", "cargo install ripgrep",
])
def test_a_wrapper_inline_code_or_package_route_is_a_launcher_skip_never_launched(tmp_path, launchers, launches, command):
    fake_programs_extra = ["npm", "pip", "yarn", "cargo", "start", "sudo", "time"]
    for name in fake_programs_extra:
        path = tmp_path / "fakebin" / (f"{name}.exe" if os.name == "nt" else name)
        path.write_text("", encoding="utf-8")
        path.chmod(0o755)
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status in ("ROUTE_NOT_EXECUTED", "ROUTE_MISSING"), (command, status, detail)
    assert not launches


def test_an_unsafe_route_never_runs_and_its_safe_twin_does(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git branch exercised-here --token abc"))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"]
    twin = item_file(tmp_path, adopt("git branch exercised-here"), name="20261017-safe-twin.md")
    assert exercise(tmp_path, twin, bus) == 0
    assert git(bus, "branch", "--list", "exercised-here") == ""         # the branch was made in the scratch clone only


# ---------- (d) git forms that start a helper are refused before launch: one row each ----------
# sol r1 blocker 2 and opus r1 hardening. The allowlisted verbs still reach an editor, a pager, gpg, a merge driver, an external
# diff or an `--exec` program through an option, a `-c` override, an alias or an environment variable.

GIT_HELPER_FORMS = {
    "-c override": "git -c core.editor=python commit --allow-empty",
    "-c alias": "git -c alias.x=!id x",
    "-c pager": "git -c core.pager=evil log",
    "apply --unsafe-paths": "git apply --unsafe-paths ../x.patch",
    "commit -S": "git commit -S -m x",
    "commit -S<keyid>": "git commit -SKEYID -m x",
    "commit --gpg-sign": "git commit --gpg-sign -m x",
    "commit --gpg-sign=<keyid>": "git commit --gpg-sign=KEYID -m x",
    "tag -s": "git tag -s v1",
    "tag -u": "git tag -u KEYID v1",
    "tag --local-user": "git tag --local-user=KEYID v1",
    "merge -s": "git merge -s ours other",
    "merge -s<name>": "git merge -sours other",
    "merge --strategy=": "git merge --strategy=ours other",
    "merge --strategy <name>": "git merge --strategy ours other",
    "cherry-pick --strategy": "git cherry-pick --strategy=ours abc",
    "--exec after the verb": "git log --exec=evil",
    "--exec before the verb": "git --exec=evil log",
    "--upload-pack after the verb": "git log --upload-pack=evil",
    "--upload-pack before the verb": "git --upload-pack=evil log",
    "--receive-pack after the verb": "git log --receive-pack=evil",
    "--receive-pack before the verb": "git --receive-pack evil log",
    "! alias": "git !echo hi",
    "alias to a shell": "git config alias.x !id",
    "--ext-diff": "git diff --ext-diff",
    "grep -O": "git grep -Oevil needle",
    "--open-files-in-pager": "git grep --open-files-in-pager=evil needle",
}


@pytest.mark.parametrize("name", list(GIT_HELPER_FORMS))
def test_d_a_git_form_that_starts_a_helper_is_unsafe_before_launch(tmp_path, launches, name):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(GIT_HELPER_FORMS[name], clone, 30, {})
    assert status == "ROUTE_UNSAFE", (name, status, detail)
    assert launches == [], f"{name} reached the process launch"


@pytest.mark.parametrize("command", [
    "git commit --allow-empty -m x", "git commit -s -m x", "git commit -am x", "git tag v1", "git tag -l v*", "git tag -d v1",
    "git merge other", "git merge --squash other", "git merge --no-edit other", "git log -S needle", "git log -s", "git diff -s",
    "git apply x.patch", "git apply --check x.patch", "git cherry-pick abc", "git grep needle", "git config --get alias.x",
])
def test_d_the_safe_twin_of_a_refused_git_form_is_launched(tmp_path, launches, command):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status == "ROUTE_OK" and len(launches) == 1, (command, status, detail)


GIT_HELPER_ENV = ["GIT_EDITOR", "GIT_SEQUENCE_EDITOR", "EDITOR", "VISUAL", "GIT_PAGER", "PAGER", "GIT_ASKPASS", "SSH_ASKPASS", "GIT_SSH",
                  "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"]


@pytest.mark.parametrize("name", GIT_HELPER_ENV)
def test_d_a_git_helper_variable_is_not_in_the_environment_a_route_gets(name):
    for spelled in (name, name.lower(), name.title()):
        env = ob.route_exercise_env({spelled: "hostile", "PATH": "/bin", "KEEP_ME": "yes"})
        assert "hostile" not in env.values() and "KEEP_ME" in env, spelled
        assert spelled not in env or name == "GIT_CONFIG_GLOBAL"


def test_d_the_editor_variables_of_the_hub_do_not_reach_a_git_commit(tmp_path, launches, monkeypatch):
    """sol r1 blocker 2: EDITOR=python, VISUAL=python and EMAIL set, `git commit --allow-empty` launched with them retained."""
    for name, value in (("EDITOR", "python"), ("VISUAL", "python"), ("EMAIL", "review@example.invalid"), ("GIT_EDITOR", "python"),
                        ("PAGER", "python"), ("SSH_ASKPASS", "python")):
        monkeypatch.setenv(name, value)
    clone = make_clone(tmp_path)
    assert ob.exercise_route("git commit --allow-empty", clone, 30, {})[0] == "ROUTE_OK"
    env, argv = launches[0]["env"], launches[0]["argv"]
    assert not {"EDITOR", "VISUAL", "GIT_EDITOR", "PAGER", "SSH_ASKPASS"} & {k.upper() for k in env}
    assert "core.editor=false" in argv and "core.pager=cat" in argv


def git_dash_c(argv: list[str]) -> list[str]:
    """The `-c key=value` pairs git is started with, in order, up to the first token that is not one."""
    found, i = [], 1
    while i + 1 < len(argv) and argv[i] == "-c":
        found.append(argv[i + 1])
        i += 2
    return found


def test_d_a_run_git_route_gets_the_hardened_command_line(tmp_path, launches):
    clone = make_clone(tmp_path)
    assert ob.exercise_route("git log --oneline", clone, 30, {})[0] == "ROUTE_OK"
    argv = launches[0]["argv"]
    pairs = git_dash_c(argv)
    assert {"core.pager=cat", "core.editor=false", "diff.external="} <= set(pairs)
    hooks = [p.split("=", 1)[1] for p in pairs if p.startswith("core.hooksPath=")]
    assert len(hooks) == 1 and Path(hooks[0]).is_dir() and list(Path(hooks[0]).iterdir()) == []
    assert Path(hooks[0]).parent == clone.parent                                  # an empty directory in the scratch, not the clone
    assert argv[1 + 2 * len(pairs):] == ["log", "--oneline"]                      # the route's own words come last, untouched


def test_d_the_hardened_settings_reach_a_real_git(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git config --get core.editor", "git config --get core.pager", "git config --get core.hooksPath",
                                     "git config --get diff.external"))
    assert exercise(tmp_path, item, bus) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"] * 4


def test_d_a_hooks_dir_is_not_made_for_a_route_that_is_refused(tmp_path, launches):
    clone = make_clone(tmp_path)
    assert ob.exercise_route("git commit -S -m x", clone, 30, {})[0] == "ROUTE_UNSAFE"
    assert [p.name for p in tmp_path.iterdir()] == ["clone"] and launches == []


# ---------- (c) every known PR 479 bypass repro is refused before launch ----------
# Each row: the launch is mocked and asserted NOT called. The outside script exists, so a route that merely checked existence
# would pass it; the rows prove it is neither launched nor accepted as a script inside the clone.

def bypass_layout(tmp_path: Path) -> tuple[Path, Path]:
    clone = make_clone(tmp_path, {f"tools/ok.{ext}": STUB for ext in ("ps1", "js", "sh", "py")})
    outside = tmp_path / "outside"
    outside.mkdir()
    for ext in ("ps1", "js", "sh", "py"):
        (outside / f"outside.{ext}").write_text(STUB, encoding="utf-8")
    return clone, outside


BYPASS_REPROS = {
    "B1 python outside script by existence": lambda o: ["python", f"{o}/outside.py"],
    "pwsh -f outside": lambda o: ["pwsh", "-f", f"{o}/outside.ps1"],
    "pwsh outside.ps1 -File in-clone": lambda o: ["pwsh", f"{o}/outside.ps1", "-File", "tools/ok.ps1"],
    "pwsh -NoProfile outside.ps1 -f": lambda o: ["pwsh", "-NoProfile", f"{o}/outside.ps1", "-f"],
    "node -r in-clone outside.js": lambda o: ["node", "-r", "tools/ok.js", f"{o}/outside.js"],
    "bash --rcfile in-clone outside.sh": lambda o: ["bash", "--rcfile", "tools/ok.sh", f"{o}/outside.sh"],
    "bash -o x outside.sh": lambda o: ["bash", "-o", "x", f"{o}/outside.sh"],
    "python --check-hash-based-pycs default outside.py": lambda o: ["python", "--check-hash-based-pycs", "default", f"{o}/outside.py"],
}
EXTRA_REPROS = {
    "pwsh -fi outside": lambda o: ["pwsh", "-fi", f"{o}/outside.ps1"],
    "pwsh -File:outside": lambda o: ["pwsh", f"-File:{o}/outside.ps1"],
    "pwsh /f outside": lambda o: ["pwsh", "/f", f"{o}/outside.ps1"],
    "pwsh -ExecutionPolicy Bypass outside": lambda o: ["pwsh", "-ExecutionPolicy", "Bypass", f"{o}/outside.ps1"],
    "node --import in-clone outside.js": lambda o: ["node", "--import", "tools/ok.js", f"{o}/outside.js"],
    "bash --init-file in-clone outside.sh": lambda o: ["bash", "--init-file", "tools/ok.sh", f"{o}/outside.sh"],
    "bash -O extglob outside.sh": lambda o: ["bash", "-O", "extglob", f"{o}/outside.sh"],
    "python -X utf8 outside.py": lambda o: ["python", "-X", "utf8", f"{o}/outside.py"],
    "python -W ignore -B outside.py": lambda o: ["python", "-W", "ignore", "-B", f"{o}/outside.py"],
}


@pytest.mark.parametrize("name", list(BYPASS_REPROS))
def test_c_a_known_bypass_repro_is_refused_before_launch(tmp_path, launchers, launches, name):
    clone, outside = bypass_layout(tmp_path)
    argv = BYPASS_REPROS[name](str(outside).replace("\\", "/"))
    status, detail = check_argv(argv, clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:"), (name, status, detail)
    assert launches == [], f"{name} reached the process launch"


@pytest.mark.parametrize("name", list(EXTRA_REPROS))
def test_c_a_spelling_variant_of_a_repro_is_refused_before_launch(tmp_path, launchers, launches, name):
    clone, outside = bypass_layout(tmp_path)
    status, detail = check_argv(EXTRA_REPROS[name](str(outside).replace("\\", "/")), clone)
    assert status == "ROUTE_MISSING" and detail.startswith("SCRIPT_OUTSIDE_CLONE:"), (name, status, detail)
    assert launches == [], f"{name} reached the process launch"


@pytest.mark.parametrize("name", list(BYPASS_REPROS) + list(EXTRA_REPROS))
def test_c_a_repro_is_not_launched_even_when_the_static_check_is_fooled(tmp_path, launchers, launches, monkeypatch, name):
    """The class that parked PR 479: the check and the launch disagree about which script runs. Here the static check is
    forced to pass everything; a launcher route must STILL not reach the process launch, because launching does not
    depend on the check at all."""
    clone, outside = bypass_layout(tmp_path)
    monkeypatch.setattr(ob, "route_missing_reason", lambda *a, **k: None)
    build = {**BYPASS_REPROS, **EXTRA_REPROS}[name]
    status, _detail = check_argv(build(str(outside).replace("\\", "/")), clone)
    assert status == "ROUTE_NOT_EXECUTED"
    assert launches == [], f"{name} reached the process launch when the static check was fooled"


@pytest.mark.parametrize("name", list(BYPASS_REPROS))
def test_c_the_same_repro_through_the_cli_is_refused_before_launch(tmp_path, capsys, launchers, launches, name):
    bus = make_bus(tmp_path, {f"tools/ok.{ext}": STUB for ext in ("ps1", "js", "sh", "py")})
    outside = tmp_path / "outside"
    outside.mkdir()
    for ext in ("ps1", "js", "sh", "py"):
        (outside / f"outside.{ext}").write_text(STUB, encoding="utf-8")
    command = " ".join(BYPASS_REPROS[name]("<out>"))                     # the account-named tmp path would trip the Law-4 screen
    assert exercise(tmp_path, item_file(tmp_path, adopt(command)), bus, "--fill", f"out={str(outside).replace(chr(92), '/')}") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_MISSING"] and "SCRIPT_OUTSIDE_CLONE" in captured.err
    assert launches == []


@pytest.mark.parametrize("name", list(BYPASS_REPROS))
def test_c_the_in_clone_twin_of_a_repro_is_a_skip_and_also_not_launched(tmp_path, launchers, launches, name):
    clone, _outside = bypass_layout(tmp_path)
    twin = BYPASS_REPROS[name]("IGNORED")
    twin = [t.replace("IGNORED/outside", "tools/ok") for t in twin]
    status, _detail = check_argv(twin, clone)
    assert status == "ROUTE_NOT_EXECUTED", (name, twin)
    assert launches == []


# ---------- (d) interpreter-injection environment is stripped from a route that is run ----------

INJECTION = ["PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH", "PERL5OPT", "PERL5LIB", "RUBYOPT", "BASH_ENV", "ENV"]


@pytest.mark.parametrize("name", INJECTION)
def test_d_an_injection_variable_is_not_in_the_environment_a_route_gets(name):
    base = {name: "hostile", "PATH": "/bin", "KEEP_ME": "yes"}
    env = ob.route_exercise_env(base)
    assert name not in env and "KEEP_ME" in env and "PATH" in env


@pytest.mark.parametrize("name", INJECTION)
def test_d_the_strip_ignores_case(name):
    assert name.lower() not in ob.route_exercise_env({name.lower(): "hostile"})
    assert name.title() not in ob.route_exercise_env({name.title(): "hostile"})


def test_d_credentials_and_git_variables_are_stripped_and_git_is_made_offline():
    base = {"GH_TOKEN": "t", "ANTHROPIC_API_KEY": "k", "MY_SECRET": "s", "GIT_DIR": "elsewhere", "GIT_SSH_COMMAND": "evil", "PATH": "p"}
    env = ob.route_exercise_env(base)
    assert not {"GH_TOKEN", "ANTHROPIC_API_KEY", "MY_SECRET", "GIT_DIR", "GIT_SSH_COMMAND"} & set(env)
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == os.devnull


def test_d_the_environment_a_run_route_receives_has_none_of_them(tmp_path, launches, monkeypatch):
    for name in INJECTION:
        monkeypatch.setenv(name, "hostile")
    clone = make_clone(tmp_path)
    assert ob.exercise_route("git log", clone, 30, {})[0] == "ROUTE_OK"
    assert len(launches) == 1
    assert not set(INJECTION) & {k.upper() for k in launches[0]["env"]}
    assert "PATH" in {k.upper() for k in launches[0]["env"]}


# ---------- the bus is never run in, and the scratch is removed ----------

def test_routes_never_run_in_the_bus_and_the_scratch_is_removed(tmp_path, capsys):
    bus = make_bus(tmp_path)
    scratch = tmp_path / "scratch"
    item = item_file(tmp_path, adopt("git branch exercised-here", "git checkout -q exercised-here"))
    head = git(bus, "rev-parse", "HEAD")
    assert exercise(tmp_path, item, bus, scratch=scratch) == 0
    out = capsys.readouterr().out
    assert statuses(out) == ["ROUTE_OK", "ROUTE_OK"] and f"ROUTE_BUS {head[:12]}" in out
    assert git(bus, "branch", "--list", "exercised-here") == ""
    assert git(bus, "status", "--porcelain") == "" and git(bus, "rev-parse", "HEAD") == head
    assert list(scratch.iterdir()) == []


def test_the_scratch_clone_has_no_origin(tmp_path):
    bus = make_bus(tmp_path)
    base, clone, tip = ob.make_exercise_scratch(bus, tmp_path / "scratch")
    try:
        assert git(clone, "remote") == "" and tip == git(bus, "rev-parse", "HEAD")
    finally:
        ob.remove_exercise_scratch(base, tmp_path / "scratch")


def test_the_scratch_is_removed_when_a_route_fails(tmp_path):
    bus = make_bus(tmp_path)
    scratch = tmp_path / "scratch"
    item = item_file(tmp_path, adopt("no-such-program-xyz go", "git rev-parse --verify no-such-ref"))
    assert exercise(tmp_path, item, bus, scratch=scratch) == 1
    assert list(scratch.iterdir()) == []


def test_the_bus_is_copied_at_its_committed_tip_only(tmp_path, capsys):
    bus = make_bus(tmp_path)
    (bus / "uncommitted.txt").write_text("x", encoding="utf-8")
    item = item_file(tmp_path, adopt("git cat-file -e HEAD:uncommitted.txt"))
    assert exercise(tmp_path, item, bus) == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_FAILED"]


def test_scratch_root_inside_the_bus_is_refused_with_exit_2(tmp_path, capsys, launches):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git log"))
    inside = bus / "scratch"
    assert exercise(tmp_path, item, bus, scratch=inside) == 2
    assert "EXERCISE_SCRATCH_INSIDE_BUS" in capsys.readouterr().err
    assert not inside.exists() and not launches
    assert exercise(tmp_path, item, bus, scratch=bus) == 2


def test_a_bus_that_is_not_a_repository_is_refused_with_exit_2(tmp_path, capsys):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log")), plain) == 2
    assert "EXERCISE_BUS_NOT_A_REPO" in capsys.readouterr().err


def test_no_bus_given_and_none_in_the_environment_is_exit_2(tmp_path, capsys):
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log")), None) == 2
    assert "EXERCISE_BUS_UNSET" in capsys.readouterr().err


def test_the_bus_defaults_to_the_environment_clone(tmp_path, monkeypatch, capsys):
    bus = make_bus(tmp_path)
    monkeypatch.setenv(ob.ROUTE_BUS_ENV, str(bus))
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log")), None) == 0
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
    item = item_file(tmp_path, adopt("git log"))
    for flag in (["--bus", str(tmp_path)], ["--fill", "a=b"], ["--scratch-root", str(tmp_path)]):
        with pytest.raises(SystemExit) as excinfo:
            ob.main(["--repo", str(tmp_path), "validate", str(item), *flag])
        assert excinfo.value.code == 2
    with pytest.raises(SystemExit):
        ob.main(["--repo", str(tmp_path), "validate", str(item), "--exercise", "--fill", "no-equals-sign"])


def test_plain_validate_checks_nothing(tmp_path, capsys, launches):
    item = item_file(tmp_path, adopt("git branch plain-validate"))
    assert ob.main(["--repo", str(tmp_path), "validate", str(item)]) == 0
    assert statuses(capsys.readouterr().out) == [] and not launches


# ---------- the live bus, by identity, through every spelling ----------

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


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_an_argument_inside_the_live_bus_is_unsafe_through_any_spelling(tmp_path, capsys, launches, spelling):
    bus = make_bus(tmp_path)
    spell = bus_spellings(tmp_path, bus)
    if spelling not in spell:
        pytest.skip(f"no {spelling} spelling on this platform or the link could not be made")
    item = item_file(tmp_path, adopt("git log -- <where>"))
    assert exercise(tmp_path, item, bus, "--fill", f"where={spell[spelling]}") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"]
    assert not launches


@pytest.mark.parametrize("spelling", SPELLINGS)
def test_a_scratch_root_inside_the_live_bus_is_exit_2_through_any_spelling(tmp_path, capsys, spelling):
    bus = make_bus(tmp_path)
    spell = bus_spellings(tmp_path, bus)
    if spelling not in spell:
        pytest.skip(f"no {spelling} spelling on this platform or the link could not be made")
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log")), bus, scratch=Path(spell[spelling])) == 2
    assert "EXERCISE_SCRATCH_INSIDE_BUS" in capsys.readouterr().err
    assert git(bus, "status", "--porcelain") == ""


def test_an_option_value_inside_the_live_bus_is_unsafe(tmp_path, capsys, launches):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git log --format=<bus>/x.txt"))
    assert exercise(tmp_path, item, bus, "--fill", f"bus={bus}") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"] and not launches


def test_a_route_whose_cwd_is_the_live_bus_is_unsafe(tmp_path, launches):
    bus = make_bus(tmp_path)
    status, detail = ob.exercise_route("git log", bus, 30, {}, bus)
    assert status == "ROUTE_UNSAFE" and "live bus" in detail and not launches


@pytest.mark.parametrize("command", [
    "git -C <bus> pull --ff-only",
    "git -C <bus> log --oneline",
    "git --git-dir=<bus>/.git log",
    "git --work-tree <bus> status",
])
def test_a_git_route_aimed_at_the_live_bus_by_fill_is_unsafe(tmp_path, capsys, launches, command):
    bus = make_bus(tmp_path)
    head = git(bus, "rev-parse", "HEAD")
    assert exercise(tmp_path, item_file(tmp_path, adopt(command)), bus, "--fill", f"bus={bus}") == 1
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"] and not launches
    assert git(bus, "rev-parse", "HEAD") == head and git(bus, "status", "--porcelain") == ""


# ---------- a timeout ends the whole process tree ----------

SENTINEL = "REVIEW_SENTINEL_NOT_A_SECRET_7f3a9"
SPAWN = (
    "import os, pathlib, subprocess, sys, time\n"
    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])\n"
    "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {child.pid}')\n"
    "time.sleep(600)\n"
)


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


def test_a_timeout_kills_the_whole_process_tree(tmp_path):
    """The launch function itself, driven directly with this interpreter (a test fixture, not a route)."""
    script = tmp_path / "spawn.py"
    script.write_text(SPAWN, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    result: list = []
    runner = threading.Thread(target=lambda: result.append(ob.run_route_process([sys.executable, str(script), str(pidfile)], tmp_path,
                                                                                   dict(os.environ), 2)), daemon=True)
    started = time.monotonic()
    runner.start()
    try:
        runner.join(30)                        # the unfixed code blocks here for as long as the grandchild sleeps
        elapsed = time.monotonic() - started
        assert not runner.is_alive(), "run_route_process did not return: the route's process tree outlived the timeout"
        assert result and result[0][0] is None and elapsed < 10, (result, elapsed)
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


def test_route_output_is_bounded_to_its_tail(tmp_path):
    code = "import sys\nfor i in range(60000):\n    sys.stdout.write('x' * 100 + '\\n')\nprint('TAILMARK')\nsys.stderr.write('E' * 200000 + 'ERRTAIL')\nsys.exit(4)\n"
    (tmp_path / "noisy.py").write_text(code, encoding="utf-8")
    returncode, out, err = ob.run_route_process([sys.executable, "noisy.py"], tmp_path, dict(os.environ), 60)
    assert returncode == 4
    assert len(out) <= ob.ROUTE_OUTPUT_CAP_BYTES and out.rstrip().endswith("TAILMARK")
    assert len(err) <= ob.ROUTE_OUTPUT_CAP_BYTES and err.endswith("ERRTAIL")


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "0", "-1"])
def test_a_nonfinite_or_nonpositive_timeout_is_refused_before_any_route_runs(tmp_path, capsys, launches, value):
    bus = make_bus(tmp_path)
    with pytest.raises(SystemExit) as stop:
        exercise(tmp_path, item_file(tmp_path, adopt("git log")), bus, f"--timeout={value}")
    assert stop.value.code == 2
    assert "--timeout wants a finite number of seconds above zero" in capsys.readouterr().err
    assert not launches


def test_a_finite_positive_timeout_is_accepted(tmp_path, capsys):
    bus = make_bus(tmp_path)
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log")), bus, "--timeout", "30") == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"]


# ---------- a credential's value is never printed or written ----------

CREDENTIAL_ROUTES = [
    "GH_TOKEN=<value> gh pr create",
    "ANTHROPIC_API_KEY=<value> git log",
    "MY_SERVICE_TOKEN=<value> git log",
    "DEPLOY_KEY=<value> git log",
    "DB_PASSWORD=<value> git log",
    "git log --token=<value>",
    "git log --token <value>",
    "git log https://user:<value>@example.invalid/x",
]


def everything_written_under(root: Path) -> bytes:
    return b"".join(f.read_bytes() for f in root.rglob("*") if f.is_file())


@pytest.mark.parametrize("command", CREDENTIAL_ROUTES)
def test_a_refused_credential_never_shows_its_value(tmp_path, capsys, launches, command):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt(command))
    assert exercise(tmp_path, item, bus, "--fill", f"value={SENTINEL}") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_UNSAFE"]
    assert SENTINEL not in captured.out and SENTINEL not in captured.err
    assert SENTINEL.encode() not in everything_written_under(tmp_path)
    status, detail = ob.exercise_route(command, tmp_path, 30, {"value": SENTINEL})
    assert status == "ROUTE_UNSAFE" and SENTINEL not in detail and not launches


def test_a_route_off_the_allowlist_carrying_a_credential_is_refused_not_run_and_not_printed(tmp_path, capsys, launchers, launches):
    """r2: the safety screen reads every route (it runs nothing), so a card never tells a steward to pass a credential."""
    bus = make_bus(tmp_path, {"tools/x.py": STUB})
    item = item_file(tmp_path, adopt("python tools/x.py --token <value>", f"python tools/x.py --token={SENTINEL}"))
    assert exercise(tmp_path, item, bus, "--fill", f"value={SENTINEL}") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_UNSAFE"] * 2 and not launches
    assert SENTINEL not in captured.out and SENTINEL not in captured.err
    assert "--token=<redacted>" in captured.out


def test_a_refused_credential_keeps_its_name_and_hides_the_value(tmp_path):
    status, detail = ob.exercise_route("GH_TOKEN=<value> gh pr create", tmp_path, 30, {"value": SENTINEL})
    assert status == "ROUTE_UNSAFE" and "GH_TOKEN=<redacted>" in detail


def test_a_secret_written_in_the_card_is_redacted_in_the_route_line(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt(f"GH_TOKEN={SENTINEL} gh pr create"))
    assert exercise(tmp_path, item, bus) == 1
    captured = capsys.readouterr()
    assert route_lines(captured.out)[0].endswith("GH_TOKEN=<redacted> gh pr create")
    assert SENTINEL not in captured.out and SENTINEL not in captured.err


def test_a_fill_pair_without_equals_is_not_echoed(tmp_path, capsys):
    item = item_file(tmp_path, adopt("git log"))
    with pytest.raises(SystemExit) as excinfo:
        ob.main(["--repo", str(tmp_path), "validate", str(item), "--exercise", "--fill", SENTINEL])
    assert excinfo.value.code == 2
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out and SENTINEL not in captured.err


def test_a_credential_named_fill_is_scrubbed_from_a_failure_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(ob, "run_route_process", lambda argv, cwd, env, timeout: (3, "", f"bad ref {argv[-1]}"))
    status, detail = ob.exercise_route("git log <api_key>", tmp_path, 30, {"api_key": SENTINEL})
    assert status == "ROUTE_FAILED" and SENTINEL not in detail and "<redacted>" in detail


def test_redact_route_text_forms():
    assert ob.redact_route_text("GH_TOKEN=abc gh pr create") == "GH_TOKEN=<redacted> gh pr create"
    assert ob.redact_route_text("x --token abc y") == "x --token <redacted> y"
    assert ob.redact_route_text("x --password=abc") == "x --password=<redacted>"
    assert ob.redact_route_text("git clone https://u:p@h/x.git") == "git clone https://<redacted>@h/x.git"
    assert ob.redact_route_text("git log --oneline") == "git log --oneline"


RX05_SENTINELS = {
    "multiword": ("FIRST_SENTINEL SECOND_SENTINEL", ["FIRST_SENTINEL", "SECOND_SENTINEL"]),
    "tabs_and_newline": ("TAB_ONE_SENTINEL\tTAB_TWO_SENTINEL\nLINE_THREE_SENTINEL", ["TAB_ONE_SENTINEL", "TAB_TWO_SENTINEL", "LINE_THREE_SENTINEL"]),
    "quotes_in_the_value": ('"QUOTED_ONE_SENTINEL QUOTED_TWO_SENTINEL"', ["QUOTED_ONE_SENTINEL", "QUOTED_TWO_SENTINEL"]),
}


@pytest.mark.parametrize("case", list(RX05_SENTINELS))
def test_a_credential_fill_with_whitespace_is_redacted_whole(tmp_path, case):
    value, parts = RX05_SENTINELS[case]
    status, detail = ob.exercise_route("DB_PASSWORD=<value> git log", tmp_path, 30, {"value": value})
    assert status == "ROUTE_UNSAFE" and "DB_PASSWORD=<redacted>" in detail
    for part in parts:
        assert part not in detail, detail


@pytest.mark.parametrize("case", list(RX05_SENTINELS))
def test_nothing_a_run_prints_or_writes_carries_a_whitespace_value(tmp_path, capsys, case):
    value, parts = RX05_SENTINELS[case]
    bus = make_bus(tmp_path)
    assert exercise(tmp_path, item_file(tmp_path, adopt("DB_PASSWORD=<value> git log")), bus, "--fill", f"value={value}") == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_UNSAFE"]
    for part in parts:
        assert part not in captured.out and part not in captured.err
        assert not [p for p in tmp_path.rglob("*") if p.is_file() and part.encode() in p.read_bytes()]


def test_a_quoted_multiword_value_written_in_the_card_is_redacted_whole(tmp_path):
    for command in ('DB_PASSWORD="QLIT_ONE_SENTINEL QLIT_TWO_SENTINEL" git log',
                    "DB_PASSWORD='QLIT_ONE_SENTINEL QLIT_TWO_SENTINEL' git log"):
        status, detail = ob.exercise_route(command, tmp_path, 30, {})
        assert status == "ROUTE_UNSAFE" and "DB_PASSWORD=<redacted>" in detail
        assert "QLIT_ONE_SENTINEL" not in detail and "QLIT_TWO_SENTINEL" not in detail, detail


@pytest.mark.parametrize("tok", ["DB_PASSWORD=QTOK_ONE_SENTINEL QTOK_TWO_SENTINEL", "--password=QTOK_ONE_SENTINEL QTOK_TWO_SENTINEL"])
def test_one_token_holding_whitespace_after_posix_unquoting_is_redacted_whole(tmp_path, tok):
    reason = ob.route_unsafe_reason([tok, "git", "log"], tmp_path)
    assert reason and "QTOK_ONE_SENTINEL" not in reason and "QTOK_TWO_SENTINEL" not in reason, reason


def test_a_credential_value_that_also_appears_elsewhere_is_redacted_there_too(tmp_path, monkeypatch):
    shared = "SHARED_ONE_SENTINEL SHARED_TWO_SENTINEL"
    status, detail = ob.exercise_route("git log <label> DB_PASSWORD=<value>", tmp_path, 30, {"label": shared, "value": shared})
    assert status == "ROUTE_UNSAFE" and "SHARED_ONE_SENTINEL" not in detail and "SHARED_TWO_SENTINEL" not in detail, detail
    monkeypatch.setattr(ob, "run_route_process", lambda argv, cwd, env, timeout: (3, "", " ".join(argv[-3:])))   # the route's own words
    status, detail = ob.exercise_route("git log <label> <db_password>", tmp_path, 30, {"label": shared, "db_password": shared})
    assert status == "ROUTE_FAILED" and "<redacted>" in detail
    assert "SHARED_ONE_SENTINEL" not in detail and "SHARED_TWO_SENTINEL" not in detail, detail


def test_a_value_after_a_token_flag_is_redacted_whole(tmp_path):
    value = "FLAGGED_ONE_SENTINEL FLAGGED_TWO_SENTINEL"
    status, detail = ob.exercise_route("git log --password=<value>", tmp_path, 30, {"value": value})
    assert status == "ROUTE_UNSAFE" and "FLAGGED_ONE_SENTINEL" not in detail and "FLAGGED_TWO_SENTINEL" not in detail, detail
    assert ob.route_secret_values(["x", "--token", "<v>"], {"v": value}) == [value, "FLAGGED_ONE_SENTINEL", "FLAGGED_TWO_SENTINEL"]


def test_a_fill_that_is_not_a_credential_is_left_alone():
    assert ob.route_secret_values(["git", "log", "<branch>"], {"branch": "feature one"}) == []


RX06 = "RX06_SENTINEL"


def test_a_credential_fill_that_looks_like_a_shell_operator_is_not_printed(tmp_path):
    fills = {"label": ">" + RX06, "value": ">" + RX06}
    status, detail = ob.exercise_route("git log <label> DB_PASSWORD=<value>", tmp_path, 30, fills)
    assert status == "ROUTE_UNSAFE" and RX06 not in detail, detail


def test_a_credential_fill_that_looks_like_a_placeholder_is_not_printed(tmp_path):
    status, detail = ob.exercise_route("DB_PASSWORD=<value> git log", tmp_path, 30, {"value": f"<{RX06}>"})
    assert status.startswith("ROUTE_") and status != "ROUTE_OK" and RX06 not in detail, detail
    status, detail = ob.exercise_route("git log <api_key>", tmp_path, 30, {"api_key": f"<{RX06}>"})
    assert RX06 not in detail, detail


def test_a_credential_fill_in_the_program_position_is_not_printed_by_the_missing_reason(tmp_path):
    status, detail = ob.exercise_route("<api_key> go", tmp_path, 30, {"api_key": RX06})
    assert status == "ROUTE_MISSING" and RX06 not in detail and "<redacted>" in detail, detail


def test_a_credential_fill_in_a_launcher_script_position_is_not_printed(tmp_path, launchers):
    status, detail = ob.exercise_route("python <api_key>", tmp_path, 30, {"api_key": f"{RX06}.py"})
    assert status == "ROUTE_MISSING" and RX06 not in detail and "<redacted>" in detail, detail


@pytest.mark.parametrize("command, value", [
    ("git log <label> DB_PASSWORD=<value>", ">" + RX06),
    ("DB_PASSWORD=<value> git log", f"<{RX06}>"),
    ("<api_key> go", RX06),
])
def test_nothing_a_run_prints_or_writes_carries_a_credential_fill(tmp_path, capsys, command, value):
    bus = make_bus(tmp_path)
    fills = ["--fill", f"value={value}", "--fill", f"label={value}", "--fill", f"api_key={value}"]
    assert exercise(tmp_path, item_file(tmp_path, adopt(command)), bus, *fills) == 1
    captured = capsys.readouterr()
    assert RX06 not in captured.out and RX06 not in captured.err
    assert not [p for p in tmp_path.rglob("*") if p.is_file() and RX06.encode() in p.read_bytes()]


RX07_VALUES = [
    ">RX07\\SENT",                    # a real single backslash (repr doubles it)
    ">C:\\RX07\\SENT",                # a Windows path
    ">RX07'q\"SENT",                  # both quote kinds (repr escapes the quote)
]


def rx07_leaks(text: str, value: str) -> bool:
    return value in text or repr(value)[1:-1] in text or repr(value) in text


@pytest.mark.parametrize("value", RX07_VALUES)
def test_a_credential_fill_with_an_escaped_character_is_not_printed_by_the_shell_operator_reason(tmp_path, value):
    status, detail = ob.exercise_route("git log ><api_key>", tmp_path, 30, {"api_key": value[1:]})
    assert status == "ROUTE_UNSAFE" and not rx07_leaks(detail, value[1:]) and not rx07_leaks(detail, value), detail
    assert "<redacted>" in detail, detail


@pytest.mark.parametrize("value", RX07_VALUES)
def test_rx07_sols_r3_template_with_an_escaped_value_is_not_printed(tmp_path, value):
    """Ported from #479 (dee057b5) to a git route: the python route it used is now a skip, so the UNSAFE path needs a run route."""
    fills = {"label": value, "value": value}
    status, detail = ob.exercise_route("git log <label> DB_PASSWORD=<value>", tmp_path, 30, fills)
    assert status == "ROUTE_UNSAFE" and not rx07_leaks(detail, value), detail


def test_rx07_the_shell_operator_reason_redacts_before_it_formats(tmp_path):
    status, reason = ob.exercise_route("git log ><api_key>", tmp_path, 30, {"api_key": "RX07\\SENT"})
    assert status == "ROUTE_UNSAFE" and "RX07" not in reason and "\\\\" not in reason, reason


def test_the_choke_point_alone_redacts_the_escaped_forms_of_a_secret():
    for value in RX07_VALUES:
        text = f"a {value!r} b {repr(value)[1:-1]} c {value}"
        assert not rx07_leaks(ob.redact_secret_values(text, [value]), value), text
    assert ob.redact_secret_values("keep this", ["x\\y"]) == "keep this"


@pytest.mark.parametrize("value", RX07_VALUES)
def test_nothing_a_run_prints_or_writes_carries_the_escaped_value(tmp_path, capsys, value):
    bus = make_bus(tmp_path)
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log ><api_key>")), bus, "--fill", f"api_key={value[1:]}") == 1
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert not rx07_leaks(stream, value[1:]) and not rx07_leaks(stream, value), stream
    for p in tmp_path.rglob("*"):
        if p.is_file():
            data = p.read_bytes()
            assert value[1:].encode() not in data and repr(value[1:])[1:-1].encode() not in data, p


# ---------- a stale bus clone is flagged; output encoding ----------

def make_upstream_and_bus(tmp_path: Path) -> tuple[Path, Path]:
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(upstream)], check=True, capture_output=True, text=True)
    (upstream / "tools").mkdir()
    (upstream / "tools" / "ok.txt").write_text("ok", encoding="utf-8")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "one")
    bus = tmp_path / "bus"
    subprocess.run(["git", "clone", "-q", str(upstream), str(bus)], check=True, capture_output=True, text=True)
    return upstream, bus


def test_a_bus_behind_its_origin_warns_and_the_exit_is_unchanged(tmp_path, capsys):
    upstream, bus = make_upstream_and_bus(tmp_path)
    (upstream / "tools" / "ok.txt").unlink()
    git(upstream, "add", "-A")
    git(upstream, "commit", "-q", "-m", "two removes the file")
    git(bus, "fetch", "-q", "origin")
    item = item_file(tmp_path, adopt("git cat-file -e HEAD:tools/ok.txt"))
    assert exercise(tmp_path, item, bus) == 0                     # the stale bus still has the file
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_OK"] and "ROUTE_BUS_STALE 1" in captured.err
    git(bus, "merge", "-q", "--ff-only", "origin/master")          # caught up: the file is gone, and no warning
    assert exercise(tmp_path, item, bus) == 1
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_FAILED"] and "ROUTE_BUS_STALE" not in captured.err


def test_a_bus_without_an_origin_does_not_warn(tmp_path, capsys):
    bus = make_bus(tmp_path)
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log")), bus) == 0
    assert "ROUTE_BUS_STALE" not in capsys.readouterr().err


def test_a_character_the_stream_cannot_encode_does_not_raise():
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
    ob._route_say("ROUTE_OK item git log caf\u00e9 \u2192 \u65e5\u672c", stream)
    stream.flush()
    assert stream.buffer.getvalue().startswith(b"ROUTE_OK item")


# ---------- the README catalogue names every repro and the launcher rule ----------

def test_the_readme_catalogues_every_repro_and_states_the_launcher_rule():
    text = (REPO_ROOT / "doctrine-outbox" / "README.md").read_text(encoding="utf-8")
    for needle in ("ROUTE_NOT_EXECUTED", "CARD-ROUTE-EXERCISE-LAUNCHER-ALLOWLIST-1", "existence-checked but not executed",
                   "PYTHONPATH", "NODE_OPTIONS", "BASH_ENV", "PERL5OPT", "RUBYOPT",
                   "pwsh -f", "pwsh <outside.ps1> -File", "pwsh -NoProfile <outside.ps1> -f", "node -r", "bash --rcfile",
                   "bash -o x", "--check-hash-based-pycs", "B1"):
        assert needle in text, needle
    assert "ROUTE_NOT_EXECUTED_LAUNCHER" not in text, "the r1 status name is gone"
    for needle in ("execution allowlist", "resolved executable", "pytest", "core.hooksPath", "core.editor=false", "--unsafe-paths",
                   "GIT_EDITOR", "EDITOR", "SSH_ASKPASS", "GIT_EXTERNAL_DIFF", "--gpg-sign", "merge -s", "--upload-pack", "`!` alias"):
        assert needle in text, needle


def test_the_readme_claims_nothing_the_code_does_not_enforce():
    """Every program the README says is run is on the allowlist, and every refused git form it lists is refused."""
    text = (REPO_ROOT / "doctrine-outbox" / "README.md").read_text(encoding="utf-8")
    section = text[text.index("## Card routes are checked before they ship"):]
    assert "`pytest` is never run" in section
    for program in ob.ROUTE_EXEC_ALLOW:
        assert f"`{program}`" in section, program
    for form in ("git apply --unsafe-paths ../x.patch", "git commit -S -m x", "git tag -s v1", "git merge -s ours other",
                 "git log --exec=evil", "git !echo hi"):
        status, _detail = ob.exercise_route(form, REPO_ROOT, 30, {})
        assert status == "ROUTE_UNSAFE", form
