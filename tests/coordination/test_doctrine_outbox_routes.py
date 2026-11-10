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
      forms that start a helper (`-c`, signing, `merge -s`, `--exec`, `--upload-pack`, `!` aliases, ...) as ROUTE_UNSAFE;
  (r5) runs read-only git verbs only, each with a positive option list, every value and positional inside the scratch clone,
      and global/system git config isolated (hub ruling #514 r5: the denylist leaked one option class per round).

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
    for name in ("git.exe", "git.cmd", "git.bat", "git.com", "git"):
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
    if os.name == "nt":                                                           # r5: an absolute path outside the clone is refused
        assert status == "ROUTE_UNSAFE" and launches == []
    value = "some dir\\back\\slash"                                                # the same backslashes, inside the clone
    status, _detail = ob.exercise_route("git log <where>", clone, 30, {"where": value})
    assert status == "ROUTE_OK" and launches[-1]["argv"][-1] == value and launches[-1]["argv"][-2] == "log"


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
    assert exercise(tmp_path, twin, bus) == 1                            # r5 NA-6: a branch name creates a branch, a write
    assert statuses(capsys.readouterr().out) == ["ROUTE_UNSAFE"]
    assert git(bus, "branch", "--list", "exercised-here") == ""         # nothing was made anywhere
    listing = item_file(tmp_path, adopt("git branch --list exercised-here"), name="20261017-safe-twin-2.md")
    assert exercise(tmp_path, listing, bus) == 0                         # the read-only twin runs
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"]


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
    "git tag -l v*", "git log -S needle", "git log -s", "git diff -s", "git grep needle", "git config --get alias.x",
])
def test_d_the_safe_twin_of_a_refused_git_form_is_launched(tmp_path, launches, command):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status == "ROUTE_OK" and len(launches) == 1, (command, status, detail)


@pytest.mark.parametrize("command", [                                   # r5 NA-6: these twins were run before; each one writes
    "git commit --allow-empty -m x", "git commit -s -m x", "git commit -am x", "git tag v1", "git tag -d v1",
    "git merge other", "git merge --squash other", "git merge --no-edit other", "git apply x.patch", "git apply --check x.patch",
    "git cherry-pick abc",
])
def test_d_r5_a_former_write_verb_twin_is_now_unsafe_before_launch(tmp_path, launches, command):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status == "ROUTE_UNSAFE", (command, status, detail)
    assert launches == [], f"{command} reached the process launch"


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
    assert ob.exercise_route("git commit --allow-empty", clone, 30, {})[0] == "ROUTE_UNSAFE" and launches == []   # r5 NA-6
    assert ob.exercise_route("git log", clone, 30, {})[0] == "ROUTE_OK"
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
    item = item_file(tmp_path, adopt("git branch exercised-here", "git checkout -q exercised-here", "git log --oneline"))
    head = git(bus, "rev-parse", "HEAD")
    assert exercise(tmp_path, item, bus, scratch=scratch) == 1                  # r5 NA-6: the two write routes are refused
    out = capsys.readouterr().out
    assert statuses(out) == ["ROUTE_UNSAFE", "ROUTE_UNSAFE", "ROUTE_OK"] and f"ROUTE_BUS {head[:12]}" in out
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
    # NA-6 (hub ruling at the #515 r4 line 15:11:38Z, applied in m1): `--bus` is the ruling-candidate check's option too,
    # so a bare `--bus` is accepted; only `--fill` and `--scratch-root` need `--exercise`.
    assert ob.main(["--repo", str(tmp_path), "validate", str(item), "--bus", str(tmp_path)]) == 0
    for flag in (["--fill", "a=b"], ["--scratch-root", str(tmp_path)]):
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


# ---------- r4: the r3 key findings (sol r3 blockers, opus r3 blocker and hardening) ----------
# Each refused row is asserted on the real exercise_route with the launch mocked: ROUTE_UNSAFE and no launch. The real-git rows
# run a git against a driver that WRITES A MARKER FILE and assert the marker is absent; a control run on an unhardened command
# shows the fixture is live (a driver that never ran would make "absent" prove nothing).

R4_HELPER_FORMS = {
    "grep -nOpython (sol r3 repro)": "git grep -nOpython HIT -- payload.py",
    "grep -Ofoo": "git grep -Ofoo needle",
    "grep -nO": "git grep -nO needle",
    "grep -On": "git grep -On needle",
    "grep -inOgit": "git grep -inOgit needle",
    "grep -cO": "git grep -cO needle",
    "--open-files= abbreviation": "git grep --open-files=python needle",
    "--open abbreviation, next token": "git grep --open python needle",
    "--o abbreviation": "git grep --o=python needle",
    "--out abbreviation": "git log --out=elsewhere.txt",
    "--outp abbreviation, next token": "git log --outp elsewhere.txt",
    "--ext abbreviation": "git diff --ext",
    "--ext-d abbreviation": "git diff --ext-d",
    "--exe abbreviation": "git log --exe=evil",
    "--exe abbreviation before the verb": "git --exe=evil log",
    "--upl abbreviation": "git log --upl=evil",
    "--rec abbreviation": "git log --rec evil",
    "--e abbreviation": "git log --e=evil",
    "--config-e abbreviation before the verb": "git --config-e=a=B log",
    "--exec-p abbreviation before the verb": "git --exec-p=x log",
}


@pytest.mark.parametrize("name", list(R4_HELPER_FORMS))
def test_r4_a_clustered_or_abbreviated_helper_option_is_unsafe_before_launch(tmp_path, launches, name):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(R4_HELPER_FORMS[name], clone, 30, {})
    assert status == "ROUTE_UNSAFE", (name, status, detail)
    assert launches == [], f"{name} reached the process launch"


R4_ALT_DIR_FORMS = {
    "--git-dir= inside the clone": "git --git-dir=fx show HEAD",
    "--git-dir <next token> inside the clone": "git --git-dir fx show HEAD",
    "--git abbreviation": "git --git=fx show HEAD",
    "--work-tree= inside the clone": "git --work-tree=. status",
    "--git-dir plus --work-tree": "git --git-dir=.git --work-tree=. status",
    "--namespace=": "git --namespace=x log",
    "--namespace <next token>": "git --namespace x log",
    "--super-prefix=": "git --super-prefix=x/ log",
    "--bare after -C": "git -C fx --bare log -p",
    "--ba abbreviation": "git --ba log",
    "--work abbreviation": "git --work=. status",
}


@pytest.mark.parametrize("name", list(R4_ALT_DIR_FORMS))
def test_r4_an_alternate_git_directory_is_unsafe_in_every_form(tmp_path, launches, name):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(R4_ALT_DIR_FORMS[name], clone, 30, {})
    assert status == "ROUTE_UNSAFE", (name, status, detail)
    assert launches == [], f"{name} reached the process launch"


@pytest.mark.parametrize("command", [
    "git grep -n needle", "git grep -nw needle", "git grep -c needle", "git grep -e needle", "git grep -in needle -- payload.py",
    "git log --oneline --format=%H --since=1.day --no-color", "git log --stat --name-only", "git diff --cached --name-status",
    "git log --branches --grep=x", "git log --oneline -5", "git rev-parse --git-dir", "git rev-parse --is-bare-repository",
    "git diff --word-diff",                                                      # r6 NA-6: `git log --show-signature` moved to R6_RED
])
def test_r4_the_safe_twin_of_a_refused_option_is_launched(tmp_path, launches, command):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status == "ROUTE_OK" and len(launches) == 1, (command, status, detail)


def test_r4_a_dash_c_into_a_plain_directory_is_launched_and_into_a_bare_layout_is_not(tmp_path, launches):
    clone = make_clone(tmp_path, {"sub/readme.txt": "x\n"})
    assert ob.exercise_route("git -C sub log", clone, 30, {})[0] == "ROUTE_OK" and len(launches) == 1
    for name in ("HEAD", "config"):
        (clone / "fx").mkdir(exist_ok=True)
        (clone / "fx" / name).write_text("ref: refs/heads/m\n" if name == "HEAD" else "[core]\n\tbare = true\n", encoding="utf-8")
    for sub in ("objects", "refs"):
        (clone / "fx" / sub).mkdir(exist_ok=True)
    (clone / "fx" / "deeper").mkdir(exist_ok=True)
    for target in ("fx", "fx/deeper"):
        status, detail = ob.exercise_route(f"git -C {target} log -p", clone, 30, {})
        assert status == "ROUTE_UNSAFE" and "bare" in detail and len(launches) == 1, (target, status, detail)


def planted_bare_layout(tmp_path: Path, clone: Path) -> Path:
    """A bare repository committed inside the clone as `fx/`, whose config defines a textconv driver that writes a marker file
    and whose info/attributes selects it for every path. Returns the marker path (absent until a driver runs)."""
    src = tmp_path / "bare-src"
    src.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "m", str(src)], check=True, capture_output=True, text=True)
    for text in ("one\n", "two\n"):
        (src / "payload.txt").write_text(text, encoding="utf-8")
        git(src, "add", "-A")
        git(src, "commit", "-q", "-m", f"c {text.strip()}")
    fx = clone / "fx"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(fx)], check=True, capture_output=True, text=True)
    marker = tmp_path / "driver-ran.marker"
    script = tmp_path / "driver.py"
    script.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('ran')\nprint('converted')\n", encoding="utf-8")
    command = " ".join(f'\\"{Path(p).as_posix()}\\"' for p in (sys.executable, script, marker))
    with open(fx / "config", "a", encoding="utf-8", newline="\n") as handle:
        handle.write(f'[diff "x"]\n\ttextconv = {command}\n')
    (fx / "info").mkdir(exist_ok=True)
    (fx / "info" / "attributes").write_text("* diff=x\n", encoding="utf-8")
    return marker


def real_git(argv: list[str], clone: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(clone), env=ob.route_exercise_env() if env is None else env, capture_output=True, text=True, timeout=60)


def test_r4_a_committed_bare_layout_never_runs_its_textconv_driver(tmp_path):
    """opus r3 blocker: `git -C fx log -p` with a textconv driver in fx/config and `* diff=x` in fx/info/attributes."""
    clone = make_clone(tmp_path)
    marker = planted_bare_layout(tmp_path, clone)
    git_bin = shutil.which("git")
    assert git_bin is not None
    real_git([git_bin, "-C", "fx", "log", "-p"], clone)                          # control: the plain command does run the driver
    if not marker.exists():
        pytest.skip("the control run did not start the planted driver on this git, so 'absent' would prove nothing")
    marker.unlink()
    real_git(ob.route_git_command(git_bin, ["git", "-C", "fx", "log", "-p"], clone), clone)
    assert not marker.exists(), "safe.bareRepository=explicit did not stop git opening the committed bare layout"
    for command in ("git -C fx log -p", "git --git-dir=fx show HEAD", "git -C fx show HEAD", "git --git-dir fx log -p"):
        status, detail = ob.exercise_route(command, clone, 30, {})
        assert status == "ROUTE_UNSAFE", (command, status, detail)
        assert not marker.exists(), command


def test_r4_the_hardening_pairs_name_safe_bare_repository_and_every_signing_program(tmp_path, launches):
    clone = make_clone(tmp_path)
    assert ob.exercise_route("git log", clone, 30, {})[0] == "ROUTE_OK"
    pairs = set(git_dash_c(launches[0]["argv"]))
    assert {"safe.bareRepository=explicit", "gpg.program=false", "gpg.ssh.program=false", "gpg.x509.program=false",
            "core.editor=false", "core.pager=cat", "diff.external=", "core.fsmonitor=false"} <= pairs, pairs
    assert {p.split("=", 1)[0] for p in ("safe.bareRepository=explicit", "gpg.program=false")} <= {p.split("=", 1)[0] for p in ob.ROUTE_GIT_HARDENING}


def test_r4_the_environment_path_keeps_only_absolute_entries_outside_the_clone(tmp_path):
    clone = make_clone(tmp_path)
    inside = clone / "bin"
    inside.mkdir()
    outside = tmp_path / "system"
    outside.mkdir()
    entries = [".", "", "bin", os.path.join("..", "x"), str(inside), str(clone), f'"{outside}"', str(outside)]
    env = ob.route_exercise_env({"Path": os.pathsep.join(entries), "KEEP_ME": "yes"}, clone=clone)
    assert env["Path"] == str(outside) and env["KEEP_ME"] == "yes", env["Path"]
    assert [k for k in env if k.upper() == "PATH"] == ["Path"]
    assert ob.route_exercise_env({"PATH": os.pathsep.join([".", "", str(outside)])})["PATH"] == str(outside)   # no clone: relative still goes


def test_r4_the_git_child_gets_that_path_and_a_planted_helper_is_never_started(tmp_path, launches, monkeypatch):
    """opus r3 hardening: PATH='.;<system>' and a false/gpg in the clone. The launch sees no relative and no in-clone entry."""
    clone = make_clone(tmp_path)
    inside = clone / "bin"
    inside.mkdir()
    for planted in (clone / "false.exe", clone / "false.bat", clone / "gpg.exe", inside / "false.exe"):
        planted.write_text("", encoding="utf-8")
        planted.chmod(0o755)
    monkeypatch.chdir(clone)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", str(inside), os.environ["PATH"]]))
    status, detail = ob.exercise_route("git commit --allow-empty", clone, 30, {})
    assert status == "ROUTE_UNSAFE" and launches == [], (status, detail)          # r5 NA-6: commit writes; a read route is run
    status, detail = ob.exercise_route("git log --oneline", clone, 30, {})
    assert status == "ROUTE_OK" and len(launches) == 1, (status, detail)
    entries = launches[0]["env"]["PATH"].split(os.pathsep)
    assert entries and all(os.path.isabs(e) for e in entries), entries
    assert not [e for e in entries if ob.route_inside(ob.route_real_path(e), ob.route_real_path(str(clone)))], entries


def test_r4_a_planted_gpg_and_false_in_the_clone_are_never_started_by_a_real_git(tmp_path, monkeypatch):
    """The marker row: a signed commit makes `git log --show-signature` start gpg; with PATH='.;<system>' the clone's own
    `gpg` and `false` (shebang scripts that write a marker) used to be found. They must not run."""
    repo = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "-b", "m", str(repo)], check=True, capture_output=True, text=True)
    marker = tmp_path / "helper-ran.marker"
    for name in ("gpg", "false"):
        planted = repo / name
        with open(planted, "w", encoding="utf-8", newline="\n") as handle:      # LF only: a CRLF shebang names an interpreter that does not exist
            handle.write(f"#!/bin/sh\n: > '{marker.as_posix()}'\nexit 1\n")
        planted.chmod(0o755)
    empty = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"                          # the empty tree, a fixed id in every sha1 repository
    written = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "tree", "-w", "--stdin"], input="", capture_output=True, text=True)
    assert written.returncode == 0 and written.stdout.strip() == empty, (written.returncode, written.stdout, written.stderr)
    body = (f"tree {empty}\nauthor t <t@example.invalid> 1 +0000\ncommitter t <t@example.invalid> 1 +0000\n"
            "gpgsig -----BEGIN PGP SIGNATURE-----\n \n abc\n -----END PGP SIGNATURE-----\n\nmsg\n")
    made = subprocess.run(["git", "-C", str(repo), "hash-object", "-t", "commit", "-w", "--stdin"], input=body.encode("utf-8"), capture_output=True)
    sha = made.stdout.decode("utf-8").strip()                                   # bytes in: text mode would write CRLF line ends into the object
    assert made.returncode == 0 and len(sha) >= 40, (made.returncode, made.stdout, made.stderr)
    git(repo, "update-ref", "refs/heads/m", sha)
    monkeypatch.setenv("PATH", os.pathsep.join([".", os.environ["PATH"]]))
    git_bin = shutil.which("git")
    unfiltered = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)   # keeps the `.` entry
    real_git([git_bin, "log", "--show-signature"], repo, unfiltered)             # control: the plain git starts the planted gpg
    if not marker.exists():
        pytest.skip("the control run did not start the planted gpg on this git/platform, so 'absent' would prove nothing")
    marker.unlink()
    # r6: `--show-signature` is off the option list, so the route is refused unrun; the hardened command line is still run below
    assert ob.exercise_route("git log --show-signature", repo, 30, {})[0] in ("ROUTE_OK", "ROUTE_FAILED", "ROUTE_UNSAFE")
    assert not marker.exists(), "a helper planted in the clone was started through a relative PATH entry"
    real_git(ob.route_git_command(git_bin, ["git", "log", "--show-signature"], repo), repo, ob.route_exercise_env(clone=repo))
    assert not marker.exists(), "a helper planted in the clone was started through the hardened command line"


def test_r4_program_lookup_never_consults_the_current_directory(tmp_path, monkeypatch):
    """shutil.which searches the current directory first on win32 unless NoDefaultCurrentDirectoryInExePath is set; a missing
    program must stay ROUTE_MISSING PROGRAM_NOT_FOUND however the host is set."""
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)
    clone = make_clone(tmp_path)
    for name in ("nosuch.exe", "nosuch.cmd", "nosuch.bat", "nosuch.com", "nosuch"):
        planted = clone / name
        planted.write_text("", encoding="utf-8")
        planted.chmod(0o755)
    monkeypatch.chdir(clone)
    assert ob.route_resolve_program("nosuch", clone) is None
    status, detail = ob.exercise_route("nosuch arg", clone, 30, {})
    assert status == "ROUTE_MISSING" and detail.startswith("PROGRAM_NOT_FOUND"), (status, detail)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", os.environ["PATH"]]))     # a relative PATH entry does not count either
    assert ob.route_resolve_program("nosuch", clone) is None
    status, detail = ob.exercise_route("nosuch arg", clone, 30, {})
    assert status == "ROUTE_MISSING" and detail.startswith("PROGRAM_NOT_FOUND"), (status, detail)


def test_r4_program_lookup_still_finds_an_absolute_path_entry_and_a_path_in_the_clone(tmp_path, monkeypatch):
    clone = make_clone(tmp_path, {"tools/mytool.exe": "", "tools/other": ""})
    (clone / "tools" / "other").chmod(0o755)
    folder = tmp_path / "abs"
    folder.mkdir()
    exe = folder / ("found.exe" if os.name == "nt" else "found")
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join([f'"{folder}"', str(folder)]))      # the quoted entry is dropped, the plain one walked
    assert os.path.normcase(ob.route_resolve_program("found", clone) or "") == os.path.normcase(str(exe))
    assert ob.route_resolve_program("tools/other", clone) == str(clone / "tools" / "other")
    assert ob.route_resolve_program("tools/mytool", clone) is not None             # a PATHEXT spelling of a path in the clone
    assert ob.route_resolve_program("tools/missing", clone) is None


def test_r4_a_quoted_path_entry_is_not_walked_for_git(tmp_path, monkeypatch):
    system = shutil.which("git")
    if system is None:
        pytest.skip("no system git")
    clone = make_clone(tmp_path)
    monkeypatch.setenv("PATH", f'"{Path(system).parent}"')
    found, refused = ob.route_git_binary(clone)
    assert found is None and refused is not None and refused[0] == "ROUTE_MISSING" and "PROGRAM_NOT_FOUND" in refused[1], (found, refused)
    assert ob.route_exercise_env({"PATH": os.environ["PATH"]}, clone=clone)["PATH"] == ""


def test_r4_a_path_entry_that_is_a_junction_into_the_clone_is_excluded(tmp_path, monkeypatch):
    clone = make_clone(tmp_path)
    inside = clone / "bin"
    inside.mkdir()
    (inside / ("git.exe" if os.name == "nt" else "git")).write_text("", encoding="utf-8")
    (inside / ("git.exe" if os.name == "nt" else "git")).chmod(0o755)
    link = tmp_path / "linked-bin"
    try:
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(inside)], capture_output=True, text=True)
            if made.returncode != 0:
                raise OSError(made.stderr.strip() or made.stdout.strip())
        else:
            os.symlink(inside, link, target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot create a directory junction or symlink on this host ({e}); the realpath exclusion is read from the code")
    system = shutil.which("git")                                                   # looked up BEFORE the junction is the whole PATH
    monkeypatch.setenv("PATH", str(link))
    found, refused = ob.route_git_binary(clone)
    assert found is None and refused is not None and refused[0] == "ROUTE_UNSAFE" and "scratch clone" in refused[1], (found, refused)
    assert ob.route_exercise_env({"PATH": str(link)}, clone=clone)["PATH"] == ""
    if system is not None:                                                       # a junction entry ahead of the system git never wins
        monkeypatch.setenv("PATH", os.pathsep.join([str(link), str(Path(system).parent)]))
        found, refused = ob.route_git_binary(clone)
        assert refused is None and found is not None
        assert not ob.route_inside(ob.route_real_path(found), ob.route_real_path(str(clone)))


def test_r4_the_readme_states_the_r4_rules_and_not_the_overbroad_sentences():
    text = (REPO_ROOT / "doctrine-outbox" / "README.md").read_text(encoding="utf-8")
    for needle in ("safe.bareRepository=explicit", "gpg.program=false", "short-option cluster", "abbreviation", "--bare", "--namespace",
                   "--super-prefix", "absolute", "never the current directory"):
        assert needle in text, needle
    assert "resolve on `PATH` or inside the clone" not in text, "the existence check never consults the current directory"


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


# ---------- r5 (final round): read-only verbs, a per-verb option allowlist, path confinement, config isolation ----------
# Hub ruling #514 r5: four rounds of denylist patching each leaked one more option class (sol r4: `apply --unsafe-p`,
# `apply --build-fake-ancestor=../x`, `merge -qsoctopus`). Real card routes on the bus run no git at all, so a route keeps the
# read-only verbs only, and each verb accepts only the options on its own list: exact tokens (an abbreviation is refused), short
# clusters letter by letter (a value-taking letter takes the rest of the cluster or the next token), `--` ends the options.
# Every option value and every positional must resolve inside the scratch clone. Red rows: the real exercise_route with the
# launch mocked, ROUTE_UNSAFE and no launch.

R5_RED = {
    "sol r4: apply --unsafe-p": "git apply --unsafe-p evil.patch",
    "sol r4: apply --build-fake-ancestor= outside": "git apply --build-fake-ancestor=../outside.idx safe.patch",
    "sol r4: merge -qsoctopus": "git merge -qsoctopus abc def",
    "merge --strat=foo": "git merge --strat=foo other",
    "merge -nsfoo": "git merge -nsfoo other",
    "apply --build-f next token": "git apply --build-f ../x safe.patch",
    "diff --no-index outside": "git diff --no-index a ../outside",
    "grep --no-index outside": "git grep --no-index x ../outside",
    "log -O outside": "git log -O ../outside",
    "blame --contents outside": "git blame --contents ../outside f",
    "log --pathspec-from-file=": "git log --pathspec-from-file=../x",
    "ls-files --pathspec-from-file=": "git ls-files --pathspec-from-file=../x",
    "diff --pathspec-from-file=": "git diff --pathspec-from-file=../x",
    "grep --pathspec-from-file=": "git grep --pathspec-from-file=../x needle",
    "status --pathspec-from-file=": "git status --pathspec-from-file=../x",
    "status --pathspec-from-file next token": "git status --pathspec-from-file ../x",
    "checkout a branch": "git checkout other",
    "checkout -q": "git checkout -q other",
    "checkout -- a path": "git checkout -- f",
    "checkout -b": "git checkout -b fresh",
    "commit --allow-empty": "git commit --allow-empty -m x",
    "commit -am": "git commit -am x",
    "commit": "git commit",
    "stash": "git stash",
    "stash push -u": "git stash push -u -m x",
    "stash list": "git stash list",
    "stash pop": "git stash pop",
    "an abbreviation of an allowed option": "git log --onel",
    "an abbreviation of an allowed valued option": "git log --form=%H",
    "log --output": "git log --output=x",
    "log --ext-diff": "git log --ext-diff",
    "show --textconv": "git show --textconv HEAD",
    "a flag given a value": "git log --oneline=x",
    "a valued option in next-token form": "git log --format %H",
    "a cluster with a letter off the list": "git log -sp",
    "a cluster hiding -O after an allowed letter": "git grep -nO needle",
    "a lone dash": "git diff -",
    "a value letter with nothing after it": "git grep -e",
    "-<N> on a verb without it": "git diff -3",
    "positional outside by ..": "git log ../outside",
    "pathspec outside after --": "git log -- ../outside",
    "grep pathspec outside": "git grep needle -- ../outside",
    "ls-files outside": "git ls-files ../outside",
    "-C into a subdirectory, then .. twice": "git -C sub log -- ../../outside",
    "top magic, long form": "git log -- :(top)x",
    "top magic, short form": "git log -- :/x",
    "exclude magic": "git ls-files :!x",
    "icase magic": "git ls-files :(icase)X",
    "an option value outside by ..": "git log --grep=../../x",
    "a branch name creates a branch": "git branch fresh",
    "branch -v plus a name creates a branch": "git branch -v fresh",
    "branch -d": "git branch -d other",
    "branch -D": "git branch -D other",
    "branch -m": "git branch -m a b",
    "branch -c": "git branch -c a b",
    "branch -f": "git branch -f other HEAD",
    "branch -a": "git branch -a",
    "branch -u": "git branch -u other",
    "branch --set-upstream-to": "git branch --set-upstream-to=other",
    "a tag name creates a tag": "git tag v1",
    "tag -d": "git tag -d v1",
    "tag -a": "git tag -a v1 -m x",
    "tag -s": "git tag -s v1",
    "tag -f": "git tag -f v1",
    "tag -u": "git tag -u KEYID v1",
    "config write": "git config a.b c",
    "config read without a mode first": "git config a.b",
    "config --global read": "git config --global --get a.b",
    "config --file read": "git config --get a.b --file=../x",
    "config set subcommand": "git config set a.b c",
    "config --get after a write mode": "git config --add a.b c --get",
    "config --edit": "git config --edit",
    "-p before the verb": "git -p log",
    "--paginate before the verb": "git --paginate log",
    "--no-replace-objects before the verb": "git --no-replace-objects log",
    "--literal-pathspecs before the verb": "git --literal-pathspecs log",
    "--attr-source before the verb": "git --attr-source=HEAD log",
    "--exec-path before the verb": "git --exec-path=x log",
    "--html-path before the verb": "git --html-path",
    "no verb": "git",
    "-C and no verb": "git -C .",
}


@pytest.mark.parametrize("name", list(R5_RED))
def test_r5_a_red_row_is_unsafe_before_launch(tmp_path, launches, name):
    clone = make_clone(tmp_path, {"f": "x\n", "a": "x\n", "sub/readme.txt": "x\n"})
    status, detail = ob.exercise_route(R5_RED[name], clone, 30, {})
    assert status == "ROUTE_UNSAFE", (name, status, detail)
    assert launches == [], f"{name} reached the process launch"


R5_WRITE_VERBS = [
    "add", "commit", "checkout", "switch", "restore", "reset", "merge", "cherry-pick", "revert", "stash", "apply", "am", "mv", "rm",
    "rebase", "pull", "push", "fetch", "clone", "init", "notes", "worktree", "submodule", "gc", "prune", "repack", "update-ref",
    "update-index", "symbolic-ref", "clean", "archive", "bundle", "format-patch", "fsck", "reflog", "remote", "ls-remote",
    "maintenance", "replace", "filter-branch", "fast-import", "sparse-checkout", "bisect", "commit-tree", "hash-object", "mktag",
    "mktree", "read-tree", "write-tree", "checkout-index", "pack-refs", "credential", "daemon", "send-email", "request-pull",
    "difftool", "mergetool", "instaweb", "gui", "help", "show-branch", "diff-tree", "diff-index", "diff-files", "check-ignore",
    "var", "config-env",
]


@pytest.mark.parametrize("verb", R5_WRITE_VERBS)
def test_r5_a_write_capable_or_dropped_verb_is_unsafe_in_any_form(tmp_path, launches, verb):
    clone = make_clone(tmp_path)
    for command in (f"git {verb}", f"git {verb} HEAD", f"git -C . {verb} --help"):
        status, detail = ob.exercise_route(command, clone, 30, {})
        assert status == "ROUTE_UNSAFE", (command, status, detail)
    assert launches == [], f"git {verb} reached the process launch"


def test_r5_the_verb_list_is_read_only_and_branch_and_tag_list_only():
    assert not set(R5_WRITE_VERBS) & set(ob.ROUTE_GIT_VERBS), set(R5_WRITE_VERBS) & set(ob.ROUTE_GIT_VERBS)
    assert ob.ROUTE_GIT_VERBS["branch"].positional == "list" and ob.ROUTE_GIT_VERBS["tag"].positional == "list"
    for verb in ("branch", "tag"):
        letters = ob.ROUTE_GIT_VERBS[verb].letters | ob.ROUTE_GIT_VERBS[verb].value_letters
        assert not set("dDmMcCfFasuSe") & letters, (verb, letters)
    for verb, spec in ob.ROUTE_GIT_VERBS.items():                               # no option that names a file to read or write
        names = spec.flags | spec.valued
        assert not {"--output", "--no-index", "--pathspec-from-file", "--contents", "--file", "--textconv", "--ext-diff",
                    "--open-files-in-pager", "--exec", "--upload-pack", "--unsafe-paths", "--strategy", "--gpg-sign"} & names, verb
        assert "O" not in spec.letters | spec.value_letters, verb


R5_OK_MOCKED = [
    "git log --oneline -n 3", "git log -n3", "git log -3", "git log -s", "git log -S needle", "git log -sn3",
    "git log --format=%h %s --date=short", "git grep -eO -- docs", "git grep -ne TODO", "git grep -nie TODO -- docs",
    "git rev-parse --short HEAD", "git rev-parse -q --verify HEAD", "git status --porcelain", "git status -sb",
    "git ls-tree -r HEAD", "git ls-files -s", "git cat-file -t HEAD", "git cat-file -p HEAD:f", "git blame -L 1,2 f",
    "git describe --tags --always", "git for-each-ref --format=%(refname) --sort=-committerdate --count=5", "git show-ref --heads",
    "git merge-base --is-ancestor HEAD HEAD", "git rev-list --count HEAD", "git shortlog -sn HEAD", "git name-rev --name-only HEAD",
    "git count-objects -v", "git version", "git config --get-regexp core", "git config -l", "git config --list --show-origin",
    "git branch", "git branch --list", "git branch --list fe*", "git branch -vv", "git branch --show-current", "git tag", "git tag -l v*",
    "git tag --list", "git -C sub log -- readme.txt", "git log -- sub", "git diff HEAD -- f a",
]


@pytest.mark.parametrize("command", R5_OK_MOCKED)
def test_r5_a_listed_read_only_form_is_launched(tmp_path, launches, command):
    clone = make_clone(tmp_path, {"f": "x\n", "a": "x\n", "sub/readme.txt": "x\n", "docs/notes.md": "TODO\n"})
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status == "ROUTE_OK" and len(launches) == 1, (command, status, detail)
    assert launches[0]["argv"][-len(ob.split_route(command)) + 1:] == ob.split_route(command)[1:]   # the route's words, untouched


def r5_bus(tmp_path: Path) -> Path:
    """A bus with two commits and a docs/ file carrying TODO, so every R5_GREEN row has something to read."""
    bus = make_bus(tmp_path, {"docs/notes.md": "TODO: one\n"})
    (bus / "docs" / "notes.md").write_text("TODO: one\nTODO: two\n", encoding="utf-8")
    git(bus, "add", "-A")
    git(bus, "commit", "-q", "-m", "second")
    return bus


R5_GREEN = ["git log --oneline -n 3", "git show --stat HEAD", "git diff --name-only HEAD~1 HEAD", "git grep -n -e TODO -- docs",
            "git rev-parse --verify HEAD", "git status --short", "git ls-files"]


@pytest.mark.parametrize("command", R5_GREEN)
def test_r5_a_green_row_runs_for_real_in_the_scratch_clone(tmp_path, capsys, monkeypatch, command):
    bus = r5_bus(tmp_path)
    head = git(bus, "rev-parse", "HEAD")
    real, seen = ob.run_route_process, []
    monkeypatch.setattr(ob, "run_route_process", lambda argv, cwd, env, timeout: (seen.append((list(argv), cwd)), real(argv, cwd, env, timeout))[1])
    assert exercise(tmp_path, item_file(tmp_path, adopt(command)), bus) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"], command
    assert len(seen) == 1 and Path(seen[0][0][0]).stem.lower() == "git" and seen[0][1].name == "bus"
    assert seen[0][1].resolve() != bus.resolve()
    assert git(bus, "rev-parse", "HEAD") == head and git(bus, "status", "--porcelain") == ""


def test_r5_an_absolute_path_outside_the_clone_is_unsafe_in_every_position(tmp_path, launches):
    clone = make_clone(tmp_path, {"f": "x\n"})
    away = (tmp_path / "away").as_posix()
    for command in (f"git log -- {away}", f"git log {away}", f"git grep needle -- {away}", f"git ls-files {away}",
                    f"git log --grep={away}", f"git grep -e {away} -- f", f"git blame -L 1,2 {away}"):
        status, detail = ob.exercise_route(command, clone, 30, {})
        assert status == "ROUTE_UNSAFE" and "outside the scratch clone" in detail, (command, status, detail)
    if os.name == "nt":                                                           # case folded, and a drive-relative spelling
        status, detail = ob.exercise_route(f"git log -- {away.swapcase()}", clone, 30, {})
        assert status == "ROUTE_UNSAFE", (status, detail)
    assert launches == []


def test_r5_a_path_through_an_outward_link_in_the_clone_is_unsafe(tmp_path, launches):
    clone = make_clone(tmp_path, {"f": "x\n"})
    away = tmp_path / "away"
    away.mkdir()
    (away / "x.txt").write_text("x\n", encoding="utf-8")
    try:
        if os.name == "nt":
            made = subprocess.run(["cmd", "/c", "mklink", "/J", str(clone / "jn"), str(away)], capture_output=True, text=True)
            if made.returncode != 0:
                raise OSError(made.stderr.strip() or made.stdout.strip())
        else:
            os.symlink(away, clone / "jn", target_is_directory=True)
    except (OSError, NotImplementedError) as e:
        pytest.skip(f"cannot make a junction or symlink on this host ({e})")
    for command in ("git log -- jn/x.txt", "git ls-files jn", "git grep needle -- jn", "git -C . log jn/x.txt"):
        status, detail = ob.exercise_route(command, clone, 30, {})
        assert status == "ROUTE_UNSAFE" and "outside the scratch clone" in detail, (command, status, detail)
    assert launches == []


def test_r5_the_git_child_reads_an_empty_global_config_beside_the_clone_and_no_system_config(tmp_path, launches):
    clone = make_clone(tmp_path)
    assert ob.exercise_route("git log", clone, 30, {})[0] == "ROUTE_OK"
    env = launches[0]["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    global_config = Path(env["GIT_CONFIG_GLOBAL"])
    assert global_config.is_file() and global_config.read_bytes() == b"" and global_config.parent == clone.parent
    assert not ob.route_inside(ob.route_real_path(str(global_config)), ob.route_real_path(str(clone)))
    global_config.write_text("[alias]\n\tx = !id\n", encoding="utf-8")            # whatever was left there is emptied for the next run
    assert ob.exercise_route("git log", clone, 30, {})[0] == "ROUTE_OK"
    assert Path(launches[1]["env"]["GIT_CONFIG_GLOBAL"]).read_bytes() == b""


def r5_repo_with_a_driver(tmp_path: Path) -> tuple[Path, Path, str]:
    """A repository whose committed .gitattributes selects diff driver `x` for every path, with two commits; returns it, the
    marker path (absent until a driver runs) and a config text that defines `x` as a textconv writing the marker."""
    repo = tmp_path / "clone"
    subprocess.run(["git", "init", "-q", "-b", "m", str(repo)], check=True, capture_output=True, text=True)
    (repo / ".gitattributes").write_text("* diff=x\n", encoding="utf-8")
    for text in ("one\n", "two\n"):
        (repo / "payload.txt").write_text(text, encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", f"c {text.strip()}")
    marker = tmp_path / "global-driver-ran.marker"
    script = tmp_path / "driver.py"
    script.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('ran')\nprint('converted')\n", encoding="utf-8")
    command = " ".join(f'\\"{Path(p).as_posix()}\\"' for p in (sys.executable, script, marker))
    return repo, marker, f'[diff "x"]\n\ttextconv = {command}\n'


@pytest.mark.parametrize("where", ["GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "HOME .gitconfig", "XDG config and attributes",
                                   "GIT_CONFIG_COUNT"])
def test_r5_a_driver_in_global_or_system_config_never_runs(tmp_path, monkeypatch, where):
    """Config isolation, shown on a real git: a textconv driver defined where git looks for global or system config (and
    selected for every path by the committed .gitattributes) runs on a plain `git show HEAD`, and never on the route."""
    repo, marker, driver = r5_repo_with_a_driver(tmp_path)
    for name in ("GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "XDG_CONFIG_HOME", "GIT_CONFIG_COUNT"):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    config = tmp_path / "driver.gitconfig"
    with open(config, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(driver)
    if where in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM"):
        monkeypatch.setenv(where, str(config))
    elif where == "HOME .gitconfig":
        shutil.copy(config, home / ".gitconfig")
    elif where == "XDG config and attributes":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
        shutil.copy(config, home / ".config" / "git" / "config")
        (home / ".config" / "git" / "attributes").write_text("* diff=x\n", encoding="utf-8")
    else:
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "diff.x.textconv")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", driver.split("textconv = ", 1)[1].strip().replace('\\"', '"'))
    git_bin = shutil.which("git")
    assert git_bin is not None
    subprocess.run([git_bin, "show", "HEAD"], cwd=str(repo), env=dict(os.environ, GIT_TERMINAL_PROMPT="0"), capture_output=True, timeout=60)
    if not marker.exists():
        pytest.skip(f"the control run did not start the {where} driver on this git, so 'absent' would prove nothing")
    marker.unlink()
    status, detail = ob.exercise_route("git show HEAD", repo, 30, {})
    assert status in ("ROUTE_OK", "ROUTE_FAILED"), (status, detail)
    assert not marker.exists(), f"a driver defined in {where} ran on a route"


def test_r5_the_readme_states_the_r5_contract_and_lists_every_verb():
    text = (REPO_ROOT / "doctrine-outbox" / "README.md").read_text(encoding="utf-8")
    section = text[text.index("## Card routes are checked before they ship"):text.index("## Concerns to the CoS")]
    for needle in ("read-only verbs", "option allowlist", "exact token", "letter by letter", "`--` ends", "inside the scratch clone",
                   "GIT_CONFIG_NOSYSTEM=1", "GIT_CONFIG_GLOBAL", "pathspec magic", "list form"):
        assert needle in section, needle
    for verb in ob.ROUTE_GIT_VERBS:
        assert f"`{verb}`" in section, verb
    assert "scratch-local verb" not in section, "write verbs are no longer run"


# ---------- CARD-ROUTE-EXERCISE-3 r1: the scratch clone is made under the isolation a route gets ----------
# Successor to parked #514. Both r5 keys found one gap in PREPARATION: make_exercise_scratch cloned with the host's own git config,
# so a bus `.gitattributes` of `* filter=lfs` started git-lfs at checkout (this host's system gitconfig defines it), before any
# route was screened. Each red row builds a bus whose committed files select a filter, or a hook directory the HOST config names,
# and whose program WRITES A MARKER FILE; it then runs `validate --exercise` and asserts the marker is absent and the committed
# bytes are checked out untouched. The control rows make the SAME clone without the isolation and demand the marker, so a red row
# can fail: a program that never ran on this git would make "absent" prove nothing, and a skipped control is not accepted here.

R6_PAYLOAD = b"version https://git-lfs.github.com/spec/v1\r\noid sha256:" + b"0" * 64 + b"\r\nsize 12\r\n"   # CRLF: no eol rewrite
R6_KINDS = ["global keyprobe filter", "system keyprobe filter", "system lfs filter", "global post-checkout hook"]
R6_HOSTILE_ENV = ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_COUNT", "XDG_CONFIG_HOME")


def r6_marker_command(tmp_path: Path) -> tuple[Path, str]:
    """The marker path (absent until the program runs) and a git-config value that runs a program writing it and exiting at once
    (it never reads stdin: a long-running `process` filter would otherwise wait on git while git waits on it)."""
    marker = tmp_path / "prep-ran.marker"
    script = tmp_path / "prep_program.py"
    script.write_text("import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('ran')\n", encoding="utf-8")
    return marker, " ".join(f'\\"{Path(p).as_posix()}\\"' for p in (sys.executable, script, marker))


def r6_bus(tmp_path: Path, attributes: bytes) -> Path:
    """A bus whose committed .gitattributes selects a filter for every path and whose payload.bin is R6_PAYLOAD. Built under an
    empty config, or this host's own lfs filter would rewrite the payload while it is committed."""
    bus = tmp_path / "bus"
    bus.mkdir()
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_ATTR_NOSYSTEM="1")
    (bus / ".gitattributes").write_bytes(attributes)
    (bus / "payload.bin").write_bytes(R6_PAYLOAD)
    for args in (["init", "-q", "-b", "master"], ["add", "-A"], ["commit", "-q", "-m", "r6 bus"]):
        done = subprocess.run(["git", "-C", str(bus), *GIT_ID, *args], env=env, capture_output=True, text=True)
        assert done.returncode == 0, (args, done.stderr)
    return bus


def r6_arm(tmp_path: Path, monkeypatch, kind: str) -> tuple[Path, Path, bytes]:
    """The bus, the marker and the committed .gitattributes bytes for `kind`, with the host config the kind names put in place
    through GIT_CONFIG_GLOBAL or GIT_CONFIG_SYSTEM (a test file, never the real one)."""
    marker, command = r6_marker_command(tmp_path)
    name = "lfs" if "lfs" in kind else "keyprobe"
    attributes = f"* filter={name}\n".encode() if "filter" in kind else b"# no filter selected\n"
    bus = r6_bus(tmp_path, attributes)
    config = tmp_path / "host.gitconfig"
    if "filter" in kind:
        text = (f'[filter "{name}"]\n\tclean = {command}\n\tsmudge = {command}\n\tprocess = {command}\n'
                + ("\trequired = true\n" if name == "lfs" else ""))
    else:
        hooks = tmp_path / "host-hooks"
        hooks.mkdir()
        hook = hooks / "post-checkout"
        with open(hook, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"#!/bin/sh\n: > '{marker.as_posix()}'\nexit 0\n")
        hook.chmod(0o755)
        text = f"[core]\n\thooksPath = {hooks.as_posix()}\n"
    with open(config, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    for var in R6_HOSTILE_ENV:
        monkeypatch.delenv(var, raising=False)
    if kind.startswith("system"):
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(config))
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    else:
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
        monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return bus, marker, attributes


@pytest.mark.parametrize("kind", R6_KINDS)
def test_r6_control_the_same_clone_without_the_isolation_does_start_the_program(tmp_path, monkeypatch, kind):
    bus, marker, _attributes = r6_arm(tmp_path, monkeypatch, kind)
    subprocess.run(["git", "clone", "--local", "--no-hardlinks", "-q", str(bus), str(tmp_path / "unisolated")],
                   capture_output=True, text=True, timeout=60)
    assert marker.exists(), f"{kind}: the unisolated clone did not start the program, so the red row below could not fail"


@pytest.mark.parametrize("kind", R6_KINDS)
def test_r6_preparing_the_scratch_clone_starts_no_program_and_checks_the_bytes_out_untouched(tmp_path, monkeypatch, capsys, kind):
    bus, marker, attributes = r6_arm(tmp_path, monkeypatch, kind)
    kept: list[Path] = []
    monkeypatch.setattr(ob, "remove_exercise_scratch", lambda base, root: kept.append(base))   # keep it, to read what was checked out
    assert exercise(tmp_path, item_file(tmp_path, adopt("git ls-files")), bus) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"], kind
    assert not marker.exists(), f"{kind}: a program the host config defines was started while the scratch clone was prepared"
    clone = kept[0] / "bus"
    assert (clone / "payload.bin").read_bytes() == R6_PAYLOAD, kind
    assert (clone / ".gitattributes").read_bytes() == attributes, kind


def test_r6_every_git_call_of_preparation_gets_the_route_command_line_and_environment(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    calls: list[tuple[list[str], dict[str, str]]] = []
    real_popen = subprocess.Popen                                                    # the bounded launcher starts every git call

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), dict(kwargs["env"])))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(ob.subprocess, "Popen", spy)
    base, clone, tip = ob.make_exercise_scratch(bus, tmp_path / "scratch")
    monkeypatch.setattr(ob.subprocess, "Popen", real_popen)
    assert len(calls) == 3 and calls[0][0][-1] == str(clone)                         # clone, remote remove, rev-parse
    for argv, env in calls:
        pairs = git_dash_c(argv)
        assert set(ob.ROUTE_GIT_HARDENING) <= set(pairs), pairs
        assert {"core.attributesFile=", "core.excludesFile="} <= set(pairs), pairs
        hooks = [p.split("=", 1)[1] for p in pairs if p.startswith("core.hooksPath=")]
        assert len(hooks) == 1 and Path(hooks[0]).parent == base and list(Path(hooks[0]).iterdir()) == [], hooks
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_ATTR_NOSYSTEM"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert Path(env["GIT_CONFIG_GLOBAL"]).parent == base and Path(env["GIT_CONFIG_GLOBAL"]).read_bytes() == b""
        assert not [k for k in env if k.startswith("GIT_") and k not in
                    {"GIT_CONFIG_NOSYSTEM", "GIT_ATTR_NOSYSTEM", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL"}], sorted(env)
        assert os.path.isabs(argv[0]) and all(os.path.isabs(e) for e in env["PATH"].split(os.pathsep) if e)
    assert tip == git(bus, "rev-parse", "HEAD")
    ob.remove_exercise_scratch(base, tmp_path / "scratch")


def test_r6_preparation_refuses_a_git_that_resolves_only_inside_the_scratch(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    monkeypatch.setattr(ob, "route_git_binary", lambda cwd: (None, ("ROUTE_UNSAFE", "git resolves inside the scratch clone")))
    with pytest.raises(ob.Unknown) as raised:
        ob.make_exercise_scratch(bus, tmp_path / "scratch")
    assert raised.value.code == "EXERCISE_GIT_UNAVAILABLE"
    assert list((tmp_path / "scratch").iterdir()) == [], "the half-made scratch directory was not removed"


def test_r6_a_route_git_also_gets_attr_nosystem_and_empty_attribute_and_exclude_files(tmp_path, launches):
    clone = make_clone(tmp_path)
    assert ob.exercise_route("git log", clone, 30, {})[0] == "ROUTE_OK"
    assert launches[0]["env"]["GIT_ATTR_NOSYSTEM"] == "1" and ob.route_exercise_env()["GIT_ATTR_NOSYSTEM"] == "1"
    pairs = git_dash_c(launches[0]["argv"])
    assert "core.attributesFile=" in pairs and "core.excludesFile=" in pairs, pairs


def test_r6_the_empty_attribute_and_exclude_files_reach_a_real_git(tmp_path, capsys):
    bus = make_bus(tmp_path)
    item = item_file(tmp_path, adopt("git config --get core.attributesFile", "git config --get core.excludesFile"))
    assert exercise(tmp_path, item, bus) == 0
    assert statuses(capsys.readouterr().out) == ["ROUTE_OK"] * 2


@pytest.mark.parametrize("command", ["git branch -l", "git branch -l fe*", "git branch -lv", "git branch -rl", "git branch -vl fe*"])
def test_r6_branch_has_no_l_letter_so_list_is_spelled_out(tmp_path, launches, command):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status == "ROUTE_UNSAFE" and launches == [], (command, status, detail)
    spec = ob.ROUTE_GIT_VERBS["branch"]
    assert "l" not in spec.letters | spec.value_letters and not spec.list_letters and spec.list_flags == {"--list"}
    assert ob.exercise_route("git branch --list fe*", clone, 30, {})[0] == "ROUTE_OK" and len(launches) == 1
    assert "l" in ob.ROUTE_GIT_VERBS["tag"].letters and ob.exercise_route("git tag -l v*", clone, 30, {})[0] == "ROUTE_OK"   # tag -l is only a list


@pytest.mark.parametrize("command", ["git log --show-signature", "git show --show-signature", "git log --show-signature HEAD",
                                     "git show --show-signature --stat HEAD"])
def test_r6_show_signature_is_off_the_option_list_for_log_and_show(tmp_path, launches, command):
    clone = make_clone(tmp_path)
    status, detail = ob.exercise_route(command, clone, 30, {})
    assert status == "ROUTE_UNSAFE" and "--show-signature" in detail and launches == [], (command, status, detail)
    assert not {"--show-signature"} & (ob.ROUTE_GIT_VERBS["log"].flags | ob.ROUTE_GIT_VERBS["show"].flags)


def test_r6_the_readme_states_that_scratch_creation_runs_under_the_route_isolation():
    text = (REPO_ROOT / "doctrine-outbox" / "README.md").read_text(encoding="utf-8")
    section = text[text.index("## Card routes are checked before they ship"):text.index("## Concerns to the CoS")]
    flat = " ".join(section.split())
    for needle in ("scratch clone is made under the same isolation as the routes", "`GIT_ATTR_NOSYSTEM=1`", "-c core.attributesFile=",
                   "-c core.excludesFile=", "git-lfs filter-process", "`run_isolated_git`"):
        assert needle in flat, needle
    assert "`log --show-signature` reaches" not in flat


# ---------- ROUTE-EXERCISE-PREP-RESOURCE-ALIGN-1: preparation shares the route path's bounds; the stale-bus check is isolated ----------
# CoS Dot record c7579197 on #523. (1) The scratch clone's git calls ran under subprocess.run: output unbounded, and a timeout reached
# only the direct child. They now go through the one bounded launcher (run_bounded_process) the route path uses, and a timeout is
# the route path's typed outcome (ROUTE_TIMEOUT, here an Unknown: it is no route's result). (2) The stale-bus check ran the
# host-configured _run_git on the live bus; it now runs under run_isolated_git, so the host's GIT_* environment and config
# cannot change the verdict. Each prep row drives a stand-in for git (a python program), the same seam the launcher rows use.

PREP_FLOOD = ("import sys\nfor i in range(60000):\n    sys.stdout.write('x' * 100 + '\\n')\nprint('TAILMARK')\n"
              "sys.stderr.write('E' * 200000 + 'ERRTAIL')\n")


def prep_stand_in(monkeypatch, script: Path, *extra: str) -> None:
    """Make the next preparation git call start `script` instead of git (the command line route_git_command would build)."""
    monkeypatch.setattr(ob, "route_git_command", lambda git_bin, argv, cwd: [sys.executable, str(script), *extra])


def prep_clone_dir(tmp_path: Path) -> Path:
    clone = tmp_path / "scratch" / "bus"
    clone.parent.mkdir(parents=True, exist_ok=True)
    return clone


def test_prep_git_output_is_bounded_to_its_tail(tmp_path, monkeypatch):
    script = tmp_path / "flood.py"
    script.write_text(PREP_FLOOD, encoding="utf-8")
    prep_stand_in(monkeypatch, script)
    clone = prep_clone_dir(tmp_path)
    done = ob.run_isolated_git("git", ["status"], clone.parent, clone, timeout=60)
    assert done.returncode == 0
    assert len(done.stdout) <= ob.ROUTE_OUTPUT_CAP_BYTES and done.stdout.rstrip().endswith("TAILMARK")
    assert len(done.stderr) <= ob.ROUTE_OUTPUT_CAP_BYTES and done.stderr.endswith("ERRTAIL")


def test_prep_git_that_never_exits_is_timed_out_and_its_process_tree_killed(tmp_path, monkeypatch):
    script = tmp_path / "spawn.py"
    script.write_text(SPAWN, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    prep_stand_in(monkeypatch, script, str(pidfile))
    clone = prep_clone_dir(tmp_path)
    result: list = []

    def drive() -> None:
        try:
            result.append(ob.run_isolated_git("git", ["clone"], clone.parent, clone, timeout=2))
        except BaseException as e:                                       # the typed outcome is what is under test
            result.append(e)

    runner = threading.Thread(target=drive, daemon=True)
    started = time.monotonic()
    runner.start()
    try:
        runner.join(30)                        # the unfixed code blocks here for as long as the grandchild sleeps
        assert not runner.is_alive(), "run_isolated_git did not return: the preparation's process tree outlived the timeout"
        assert time.monotonic() - started < 15
        assert isinstance(result[0], ob.Unknown) and result[0].code == "ROUTE_TIMEOUT", result
        assert "process tree was killed" in result[0].detail
        parent, child = (int(p) for p in pidfile.read_text().split())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (pid_alive(parent) or pid_alive(child)):
            time.sleep(0.2)
        assert not pid_alive(child), "the grandchild survived the timeout"
        assert not pid_alive(parent), "the preparation's own process survived the timeout"
    finally:
        if pidfile.exists():
            for pid in pidfile.read_text().split():
                force_kill(int(pid))


def test_a_hung_scratch_preparation_is_the_route_timeout_outcome_and_leaves_no_scratch(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    script = tmp_path / "spawn.py"
    script.write_text(SPAWN, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    prep_stand_in(monkeypatch, script, str(pidfile))
    monkeypatch.setattr(ob, "ROUTE_PREP_TIMEOUT", 2)
    result: list = []

    def drive() -> None:
        try:
            result.append(ob.make_exercise_scratch(bus, tmp_path / "scratch"))
        except BaseException as e:
            result.append(e)

    runner = threading.Thread(target=drive, daemon=True)
    runner.start()
    try:
        runner.join(30)
        assert not runner.is_alive(), "make_exercise_scratch did not return: the preparation's process tree outlived the timeout"
        assert isinstance(result[0], ob.Unknown) and result[0].code == "ROUTE_TIMEOUT", result
        assert list((tmp_path / "scratch").iterdir()) == [], "the half-made scratch directory was not removed"
    finally:
        if pidfile.exists():
            for pid in pidfile.read_text().split():
                force_kill(int(pid))


# ---------- CI-FLAKE-OUTBOX-HUNG-SCRATCH-PERMISSIONERROR-1: a killed preparation may still hold the scratch directory ----------
# Hosted CI (PR #781, run 42597694336) saw the test above fail with PermissionError(13): the hung preparation's tree was killed, but
# Windows releases the scratch directory (the preparation's cwd) a moment after the kill, and the removal's single retry did not
# cover a sharing violation. The PermissionError then REPLACED the typed ROUTE_TIMEOUT. These rows inject the sharing violation on
# the removal itself (no race with a real process): a transient one is ridden out, a persistent one is a typed outcome.

class FlakyScratchRemoval:
    """shutil.rmtree stand-in: a scratch directory of this tool fails with a sharing violation `failures` times (None: always)."""

    def __init__(self, real, failures: int | None) -> None:
        self.real, self.failures, self.calls = real, failures, 0

    def __call__(self, path, *args, **kwargs):
        if Path(path).name.startswith(ob.ROUTE_SCRATCH_PREFIX):
            self.calls += 1
            if self.failures is None or self.calls <= self.failures:
                raise PermissionError(13, "The process cannot access the file because it is being used by another process", str(path))
        return self.real(path, *args, **kwargs)


def hung_prep_that_leaves_scratch_held(monkeypatch, failures: int | None) -> FlakyScratchRemoval:
    flaky = FlakyScratchRemoval(ob._remove_tree, failures)
    monkeypatch.setattr(ob, "_remove_tree", flaky)

    def hung(*_a, **_k):
        raise ob.Unknown("ROUTE_TIMEOUT", "git clone still running after 2s; its process tree was killed")

    monkeypatch.setattr(ob, "run_isolated_git", hung)
    return flaky


def test_a_transient_sharing_violation_on_scratch_removal_keeps_the_route_timeout_outcome(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    flaky = hung_prep_that_leaves_scratch_held(monkeypatch, failures=3)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    with pytest.raises(ob.Unknown) as caught:
        ob.make_exercise_scratch(bus, tmp_path / "scratch")
    assert caught.value.code == "ROUTE_TIMEOUT", caught.value
    assert flaky.calls == 4, "the removal must retry the sharing violation until it succeeds"
    assert list((tmp_path / "scratch").iterdir()) == [], "the half-made scratch directory was not removed"


def test_a_persistent_sharing_violation_on_scratch_removal_is_a_typed_outcome_naming_the_leftover(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    hung_prep_that_leaves_scratch_held(monkeypatch, failures=None)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    with pytest.raises(ob.Unknown) as caught:                            # never a bare PermissionError
        ob.make_exercise_scratch(bus, tmp_path / "scratch")
    assert caught.value.code == "ROUTE_TIMEOUT", caught.value            # the original outcome is kept ...
    leftover = list((tmp_path / "scratch").iterdir())
    assert len(leftover) == 1 and str(leftover[0]) in caught.value.detail, (leftover, caught.value.detail)   # ... and names the leftover


def test_remove_exercise_scratch_with_a_persistent_sharing_violation_raises_the_distinct_typed_code(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "held")
    base.mkdir(parents=True)
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, None))
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED" and str(base) in caught.value.detail, caught.value


# Adversary pass r1 (head 2efbdb59): A1 a child that vanished mid-removal must not read as "the scratch is gone"; A2 a tree that is
# not confirmed exited is a typed outcome, not a silent return; A3 no removal attempt starts after the deadline.

def test_a_child_that_vanishes_during_removal_is_not_the_scratch_being_gone(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "race")
    base.mkdir(parents=True)
    (base / "keep.txt").write_text("k", encoding="utf-8")
    real = ob._remove_tree
    calls: list[int] = []

    def racing(path, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:                                              # an entry the walk listed is already gone when it is removed
            ob._remove_entry(str(Path(path) / "racing.tmp"), None, False)
        return real(path, *args, **kwargs)

    monkeypatch.setattr(ob, "_remove_tree", racing)
    ob.remove_exercise_scratch(base, root)
    assert not base.exists(), "cleanup returned while the scratch directory (keep.txt) was still there"


def test_a_file_not_found_for_a_child_does_not_end_the_removal_while_the_scratch_remains(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "gone-child")
    base.mkdir(parents=True)
    (base / "keep.txt").write_text("k", encoding="utf-8")
    real = ob._remove_tree
    calls: list[int] = []

    def racing(path, *args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise FileNotFoundError(2, "The system cannot find the file specified", str(Path(path) / "racing.tmp"))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(ob, "_remove_tree", racing)
    ob.remove_exercise_scratch(base, root)
    assert not base.exists() and len(calls) == 2


def test_no_scratch_removal_attempt_starts_after_the_deadline(tmp_path, monkeypatch):
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "late")
    base.mkdir(parents=True)
    clock = [0.0]
    starts: list[float] = []

    def attempt(path, *args, **kwargs):
        starts.append(clock[0])
        clock[0] += 14.99 if len(starts) == 1 else 0.0                   # the first attempt is slow, then fails with the violation
        raise PermissionError(13, "in use", str(path))

    monkeypatch.setattr(ob, "_remove_tree", attempt)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED"
    assert starts and max(starts) <= ob.ROUTE_SCRATCH_REMOVE_DEADLINE, starts


def test_a_pause_that_overshoots_the_deadline_does_not_start_another_removal_attempt(tmp_path, monkeypatch):
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "overshoot")
    base.mkdir(parents=True)
    clock = [0.0]
    starts: list[float] = []

    def attempt(path, *args, **kwargs):
        starts.append(clock[0])
        clock[0] += 14.8 if len(starts) == 1 else 0.0                    # the first attempt fails at 14.8: the 0.1 s pause fits the budget ...
        raise PermissionError(13, "in use", str(path))

    def sleep(seconds):                                                  # ... but the scheduler wakes the sleeper at 15.1
        clock[0] += seconds + 0.2

    monkeypatch.setattr(ob, "_remove_tree", attempt)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED"
    assert starts == [0.0], starts


@pytest.mark.skipif(os.name != "nt", reason="the Job Object exit wait is Windows-only")
@pytest.mark.parametrize("remaining", [1, None])                       # still active at the deadline / the job could not be queried
def test_a_killed_tree_not_confirmed_exited_is_a_typed_outcome_after_the_direct_child_is_reaped(remaining):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])

    class Job:
        closed = False

        def kill_and_wait(self, timeout):
            return remaining

        def close(self):
            self.closed = True

    job = Job()
    try:
        with pytest.raises(ob.Unknown) as caught:
            ob._kill_route_tree(proc, job)
        assert caught.value.code == "ROUTE_TREE_NOT_EXITED", caught.value
        assert job.closed and proc.poll() is not None, "the direct child must still be killed and reaped before the outcome is raised"
    finally:
        force_kill(proc.pid)


def test_a_route_whose_killed_tree_is_not_confirmed_exited_stays_a_status_not_a_raise(tmp_path, monkeypatch):
    clone = make_clone(tmp_path)

    def stuck(*_a, **_k):
        raise ob.Unknown("ROUTE_TREE_NOT_EXITED", "the killed process tree 2 process(es) still active after 10s")

    monkeypatch.setattr(ob, "run_route_process", stuck)
    status, detail = ob.exercise_route("git log", clone, 30, {})
    assert status == "ROUTE_TIMEOUT" and "still active" in detail, (status, detail)


# Adversary pass r1 (head e5b969b6): A1 during the retry pause another process replaces the scratch directory with a directory link
# (a symlink, or a junction where symlink creation is not permitted) to an outside read-only target. The guard ran once at the top,
# so the next attempt followed the link and _writable_retry made the OUTSIDE target writable. The whole guard runs before EVERY attempt.

def make_directory_link(link: Path, target: Path) -> str:
    """Make `link` a directory link to `target`; return which kind ('symlink' or 'junction') this host allowed."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except (OSError, NotImplementedError):
        try:
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        except (ImportError, AttributeError, OSError):
            pytest.skip("this host allows neither a directory symlink nor a junction")
        return "junction"


def rename_through_os_rename(monkeypatch) -> None:
    """r7 A1: both renames of the removal go through _rename_verified, which renames BY HANDLE on Windows, so a seam row that
    hooks os.rename would never see them. This routes _rename_verified through its by-name form (the identity checked by lstat,
    then os.rename, as the seam rows were written against), keeping each seam and its assertions; the by-handle path itself is
    covered by every other removal row and by test_r7_e2_a1."""
    def by_name(src, dst, ident):
        if ident is None:
            raise ob._NotOurs(f"{src} has no pinned identity to verify")
        seen = ob._meta(os.lstat, src)
        if not ob._plain_dir(seen) or ob._ident(seen) != ident:
            raise ob._NotOurs(f"{src} is not the scratch directory pinned for it")
        ob._meta(ob.os.rename, src, dst)

    monkeypatch.setattr(ob, "_rename_verified", by_name)


def scratch_now(base: Path) -> Path:
    """Where the scratch `base` is now: its own name, or (r5: removal renames it first) the private `<name>.rm-<hex>` beside it."""
    if os.path.lexists(base):
        return base
    moved = [p for p in base.parent.iterdir() if p.name.startswith(base.name + ".rm-")]
    assert len(moved) == 1, moved
    return moved[0]


def swap_scratch_for_link_in_the_pause(monkeypatch, base: Path, target: Path, kinds: list[str]) -> None:
    """The injected retry-pause seam: the first pause replaces the scratch (a real directory, under its own or its private name)
    by a link to `target`."""
    import types
    real_time = ob.time

    def pause(seconds):
        if not kinds:
            where = scratch_now(base)
            shutil.rmtree(where)
            kinds.append(make_directory_link(where, target))

    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))


@pytest.fixture
def outside_readonly_target(tmp_path):
    import stat
    target = tmp_path / "outside"
    target.mkdir()
    keep = target / "precious.txt"
    keep.write_text("precious", encoding="utf-8")
    os.chmod(keep, stat.S_IREAD)
    os.chmod(target, stat.S_IREAD | stat.S_IEXEC)                        # r-x: the content stays readable and traversable on POSIX
    yield target, keep
    os.chmod(target, stat.S_IRWXU)                                       # the directory first: a child cannot be chmodded through 0500 teardown order
    os.chmod(keep, stat.S_IREAD | stat.S_IWRITE)


def attrs_of(path: Path) -> int:
    info = os.stat(path)
    return getattr(info, "st_file_attributes", info.st_mode)


def test_a_scratch_swapped_for_a_directory_link_during_the_retry_pause_is_never_followed_or_made_writable(tmp_path, monkeypatch, outside_readonly_target):
    target, keep = outside_readonly_target
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "swapped")
    base.mkdir(parents=True)
    (base / "child.txt").write_text("c", encoding="utf-8")
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, 1))   # attempt 1: a sharing violation, so the pause runs
    chmods: list[str] = []
    real_chmod = os.chmod
    monkeypatch.setattr(ob.os, "chmod", lambda p, *a, **k: (chmods.append(str(p)), real_chmod(p, *a, **k))[1])
    kinds: list[str] = []
    swap_scratch_for_link_in_the_pause(monkeypatch, base, target, kinds)
    before = attrs_of(target)
    with pytest.raises(ob.Unknown) as caught:                            # success with the link remaining would be a silent leak
        ob.remove_exercise_scratch(base, root)
    assert kinds, "the retry pause never ran: the test did not exercise the swap"
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED" and str(base) in caught.value.detail, (kinds, caught.value)
    link = scratch_now(base)                                             # the link stands where the scratch was swapped (r5: its private name)
    assert not any(Path(p) in (base, link) or Path(p) == target or Path(p).is_relative_to(target) for p in chmods), (kinds, chmods)
    assert keep.read_text(encoding="utf-8") == "precious" and os.path.lexists(link), kinds
    assert attrs_of(target) == before, f"the outside target was changed ({kinds})"


def test_the_rmtree_error_hook_never_makes_a_link_or_anything_under_one_writable(tmp_path, monkeypatch, outside_readonly_target):
    target, keep = outside_readonly_target
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "hook")
    base.mkdir(parents=True)
    link = base / "link"
    kind = make_directory_link(link, target)
    chmods: list[str] = []
    monkeypatch.setattr(ob.os, "chmod", lambda p, *a, **k: chmods.append(str(p)))
    retried: list[str] = []
    for path in (link, link / "precious.txt"):                           # the link itself, and an entry reached through it
        ob._writable_retry(lambda p: retried.append(p), str(path), PermissionError(13, "denied"), base)
    assert chmods == [] and retried == [], (kind, chmods, retried)


# Adversary pass r2 (head 583a913d): A1 the guard's own time may carry the next attempt past the deadline; A2 a directory between the
# hook's inspection and its chmod is swapped for a junction; A3 an unreadable scratch root must not read as "absent".

def test_a_slow_check_inside_an_attempt_that_started_in_time_ends_that_attempt_and_starts_no_other(tmp_path, monkeypatch):
    """r6 HUB RULING F2: the budget bounds ATTEMPTS (checked before each one); an attempt that started before the deadline runs to
    its end, and none starts after it. (Was: a slow guard after the pause carried the next walk past the deadline.)"""
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "slowguard")
    base.mkdir(parents=True)
    clock = [0.0]
    starts: list[float] = []
    real_in_place = ob._in_place
    checks = [0]

    def attempt(path, *args, **kwargs):
        starts.append(clock[0])
        clock[0] += 14.8 if len(starts) == 1 else 0.0                    # the first attempt fails at 14.8: the 0.1 s pause fits ...
        raise PermissionError(13, "in use", str(path))

    def slow_root_check(path):                                           # ... the pause ends at 14.9, and the second attempt's root check takes 0.2 s
        checks[0] += 1
        if os.path.normcase(str(path)) == os.path.normcase(os.path.realpath(root)) and checks[0] > 2:
            clock[0] += 0.2
        return real_in_place(path)

    monkeypatch.setattr(ob, "_remove_tree", attempt)
    monkeypatch.setattr(ob, "_in_place", slow_root_check)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED"
    assert len(starts) == 2 and starts[0] == 0.0 and starts[1] == pytest.approx(15.1), starts   # the in-time attempt ran; no third


def test_a_directory_swapped_for_a_junction_between_the_hooks_inspection_and_its_chmod_is_not_touched(tmp_path, monkeypatch, outside_readonly_target):
    target, keep = outside_readonly_target
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "toctou")
    objects = base / "objects"
    objects.mkdir(parents=True)
    victim = objects / "precious.txt"
    victim.write_text("mine", encoding="utf-8")
    import stat as stat_module
    os.chmod(victim, stat_module.S_IREAD)
    real_check = ob._inside_a_link
    swapped: list[str] = []

    def inspect_then_swap(path, b):
        verdict = real_check(path, b)                                    # the inspection finds a plain directory ...
        if not swapped:
            os.chmod(victim, stat_module.S_IWRITE)
            shutil.rmtree(objects)                                       # ... and the swap lands right after it
            swapped.append(make_directory_link(objects, target))
        return verdict

    monkeypatch.setattr(ob, "_inside_a_link", inspect_then_swap)
    removed: list[str] = []
    before = attrs_of(keep)
    ob._writable_retry(lambda p: removed.append(p), str(victim), PermissionError(13, "denied"), base)
    assert swapped, "the swap seam never ran"
    assert removed == [] and keep.exists() and attrs_of(keep) == before, (swapped, removed)


def test_an_unreadable_scratch_root_is_never_reported_as_absent(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "unreadable")
    base.mkdir(parents=True)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    monkeypatch.setattr(ob, "_remove_tree", lambda *a, **k: None)       # every entry was refused: the walk returns without removing
    real_lstat = os.lstat
    unreadable = [True]                                                  # the scratch's metadata cannot be read

    def lstat(path, *a, **k):
        if unreadable[0] and str(path) == str(base):
            raise PermissionError(13, "Access is denied", str(path))
        return real_lstat(path, *a, **k)

    def lexists(path):                                                   # os.path.lexists as documented: any OSError reads as "does not exist"
        try:
            lstat(path)
        except (OSError, ValueError):
            return False
        return True

    monkeypatch.setattr(ob.os, "lstat", lstat)
    monkeypatch.setattr(ob.os.path, "lexists", lexists)
    with pytest.raises(ob.Unknown) as caught:                            # success here would be a silent leak
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value


# Adversary pass r3 (head b5867b19): A1 unreadable from the very first look; A2 the scratch ROOT swapped for a junction during the
# pause; A3 the error hook itself starts no act after the deadline.

def test_a_scratch_unreadable_from_the_first_look_is_a_typed_outcome_naming_the_leftover(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "unreadable-first")
    base.mkdir(parents=True)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)             # an unreadable scratch is retried to the deadline: keep it short
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    real_lstat = os.lstat

    def lstat(path, *a, **k):
        if os.path.normcase(str(path)) == os.path.normcase(str(base)):
            raise PermissionError(13, "Access is denied", str(path))
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(ob.os, "lstat", lstat)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED" and str(base) in caught.value.detail, caught.value


def test_a_scratch_root_swapped_for_a_junction_during_the_retry_pause_is_never_removed_through(tmp_path, monkeypatch):
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "rootswap")
    base.mkdir(parents=True)
    outside = tmp_path / "outside"
    victim_dir = outside / base.name
    victim_dir.mkdir(parents=True)
    victim = victim_dir / "readonly.txt"
    victim.write_text("outside", encoding="utf-8")
    import stat as stat_module
    os.chmod(victim, stat_module.S_IREAD)
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, 1))
    kinds: list[str] = []
    real_time = ob.time

    def pause(seconds):
        if not kinds:
            os.rename(root, tmp_path / "scratch-saved")                  # the whole root moves away and a junction takes its name
            kinds.append(make_directory_link(root, outside))

    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))
    try:
        with pytest.raises(ob.Unknown) as caught:
            ob.remove_exercise_scratch(base, root)
        assert kinds and caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", (kinds, caught.value)
        assert victim.exists() and not os.access(victim, os.W_OK), "the outside scratch-named directory was reached"
    finally:
        os.chmod(victim, stat_module.S_IREAD | stat_module.S_IWRITE)


def test_the_error_hook_starts_neither_act_after_the_deadline_when_its_own_inspection_is_slow(tmp_path, monkeypatch):
    import types
    base = tmp_path / "scratch" / (ob.ROUTE_SCRATCH_PREFIX + "hookdeadline")
    base.mkdir(parents=True)
    victim = base / "ro.txt"
    victim.write_text("x", encoding="utf-8")
    clock = [14.9]
    real_safe = ob._safe_to_touch

    def slow_safe(path, b):                                              # each inspection takes 0.2 s
        clock[0] += 0.2
        return real_safe(path, b)

    chmods: list[str] = []
    retried: list[str] = []
    monkeypatch.setattr(ob, "_safe_to_touch", slow_safe)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: None))
    monkeypatch.setattr(ob.os, "chmod", lambda p, *a, **k: chmods.append(str(p)))
    ob._writable_retry(lambda p: retried.append(p), str(victim), PermissionError(13, "denied"), base, 15.0)
    assert chmods == [] and retried == [], (chmods, retried, clock)


@pytest.mark.skipif(os.name != "nt", reason="the Job Object exit wait is Windows-only")
def test_a_timeout_returns_only_after_the_preparations_tree_has_exited(tmp_path, monkeypatch):
    script = tmp_path / "spawn.py"
    script.write_text(SPAWN, encoding="utf-8")
    pidfile = tmp_path / "pids.txt"
    prep_stand_in(monkeypatch, script, str(pidfile))
    clone = prep_clone_dir(tmp_path)
    try:
        with pytest.raises(ob.Unknown) as caught:
            ob.run_isolated_git("git", ["clone"], clone.parent, clone, timeout=2)
        assert caught.value.code == "ROUTE_TIMEOUT"
        parent, child = (int(p) for p in pidfile.read_text().split())
        assert not pid_alive(parent) and not pid_alive(child), "the call returned while the killed tree was still running"   # no polling
    finally:
        if pidfile.exists():
            for pid in pidfile.read_text().split():
                force_kill(int(pid))


# Adversary pass r2 (head 0fc3c157): A1 a TRANSIENT metadata error on the first scratch look; A2 the scratch root swapped for a junction
# to an EMPTY directory, so "absent" is read through the replaced root; A3 a killed preparation whose tree is not confirmed exited;
# A5 the exit wait's pause must not carry it past its own deadline.

def scratch_exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    return True


def test_a_transient_permission_error_on_the_first_scratch_look_is_retried_not_read_as_done(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "transient-first")
    base.mkdir(parents=True)
    (base / "f.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    real_lstat, looks = os.lstat, [0]

    def lstat(path, *a, **k):
        if os.path.normcase(str(path)) == os.path.normcase(str(base)):
            looks[0] += 1
            if looks[0] == 1:
                raise PermissionError(13, "Access is denied", str(path))      # transient: the next look succeeds
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(ob.os, "lstat", lstat)
    ob.remove_exercise_scratch(base, root)                               # removed, or a typed Unknown; never a silent leak
    monkeypatch.undo()
    assert not scratch_exists(base), "the scratch was left behind and the call returned normally"


def test_absence_seen_through_a_scratch_root_replaced_during_the_pause_is_not_a_removal(tmp_path, monkeypatch):
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "rootswap-empty")
    base.mkdir(parents=True)
    (base / "f.txt").write_text("x", encoding="utf-8")
    outside = tmp_path / "outside-empty"
    outside.mkdir()                                                      # holds nothing named like the scratch: every look says "absent"
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, 1))
    kinds: list[str] = []
    real_time = ob.time

    def pause(seconds):
        if not kinds:
            os.rename(root, tmp_path / "scratch-saved")
            kinds.append(make_directory_link(root, outside))

    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))
    with pytest.raises(ob.Unknown) as caught:                            # success here is a scratch that remains, reported as removed
        ob.remove_exercise_scratch(base, root)
    assert kinds and caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", (kinds, caught.value)
    assert (scratch_now(tmp_path / "scratch-saved" / base.name) / "f.txt").exists()


def test_a_preparation_whose_killed_tree_is_not_confirmed_exited_is_the_route_timeout(tmp_path, monkeypatch):
    clone = prep_clone_dir(tmp_path)
    prep_stand_in(monkeypatch, tmp_path / "unused.py")

    def stuck(*_a, **_k):
        raise ob.Unknown("ROUTE_TREE_NOT_EXITED", "the killed process tree 1 process(es) still active after 10s")

    monkeypatch.setattr(ob, "run_bounded_process", stuck)
    with pytest.raises(ob.Unknown) as caught:
        ob.run_isolated_git("git", ["clone"], clone.parent, clone, timeout=2.0)
    assert caught.value.code == "ROUTE_TIMEOUT", caught.value


def test_make_exercise_scratch_with_an_unconfirmed_exit_of_the_clone_tree_is_the_route_timeout(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)

    def stuck(*_a, **_k):
        raise ob.Unknown("ROUTE_TREE_NOT_EXITED", "the killed process tree 1 process(es) still active after 10s")

    monkeypatch.setattr(ob, "run_bounded_process", stuck)
    with pytest.raises(ob.Unknown) as caught:
        ob.make_exercise_scratch(bus, tmp_path / "scratch")
    assert caught.value.code == "ROUTE_TIMEOUT", caught.value


def test_the_job_exit_wait_never_returns_later_than_its_own_deadline(monkeypatch):
    import ctypes
    import types
    clock = [0.0]
    sleeps: list[float] = []

    class Accounting(ctypes.Structure):
        _fields_ = [("ActiveProcesses", ctypes.c_uint32)]

    infos: list[Accounting] = []

    def make_info() -> Accounting:
        infos.append(Accounting())
        return infos[-1]

    class Kernel32:
        def TerminateJobObject(self, handle, code):
            return 1

        def QueryInformationJobObject(self, handle, cls, ref, size, returned):
            infos[-1].ActiveProcesses = 1                                # always one process still active
            return 1

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    job = object.__new__(ob._WinJob)                                     # no real Job Object: the accounting calls are injected
    job._k32, job._accounting, job._handle = Kernel32(), make_info, 1
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    assert job.kill_and_wait(0.05) == 1
    assert clock[0] <= 0.05 + 1e-9, (clock, sleeps)                      # the unconditional 0.02 s pause crossed the deadline


# Adversary pass r3 (head 7d239aba): A1 the FIRST removal attempt is also bound by the deadline (the guard's own time counts); A2 the
# scratch root replaced by an ORDINARY directory (or file) at the same path keeps the same real-path string, so identity, not the
# string, decides: the root and the scratch are pinned by file identity, from creation when make_exercise_scratch made them.

def test_no_attempt_starts_once_the_deadline_has_passed_and_a_slow_first_attempt_still_completes(tmp_path, monkeypatch):
    """r6 HUB RULING F2: the deadline is checked before each attempt; an attempt that started in time is never discarded, even when
    its own checks outlast the budget (it completes and the scratch is removed). A loop entered after the deadline starts none."""
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "slowfirst")
    base.mkdir(parents=True)
    clock = [0.0]
    attempts: list[str] = []
    real_in_place = ob._in_place

    def slow_first_check(path):                                          # the very first look takes 16 s, past the default 15 s deadline
        if not attempts and clock[0] == 0.0:
            clock[0] += 16.0
        return real_in_place(path)

    monkeypatch.setattr(ob, "_in_place", slow_first_check)
    monkeypatch.setattr(ob, "_remove_tree", lambda path, *a, **k: attempts.append(str(path)))
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    ob.remove_exercise_scratch(base, root)                               # started at 0.0: runs to its end
    assert len(attempts) == 1 and not base.exists(), attempts
    late = root / (ob.ROUTE_SCRATCH_PREFIX + "late")
    late.mkdir()
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", -1.0)      # the budget is spent before the first attempt could start
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(late, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert len(attempts) == 1 and late.exists(), attempts


@pytest.mark.parametrize("kind", ["populated-directory", "empty-directory", "file"])
def test_a_scratch_root_replaced_by_an_ordinary_path_during_the_pause_is_never_removed_through_or_read_as_done(tmp_path, monkeypatch, kind):
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "ordinary-swap")
    base.mkdir(parents=True)
    (base / "f.txt").write_text("original", encoding="utf-8")
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, 1))
    precious = root / base.name / "precious.txt"
    swapped: list[str] = []
    real_time = ob.time

    def pause(seconds):
        if not swapped:
            os.rename(root, tmp_path / "scratch-saved")                  # same path string, another object
            if kind == "file":
                root.write_text("not a directory", encoding="utf-8")
            else:
                root.mkdir()
                if kind == "populated-directory":
                    (root / base.name).mkdir()
                    precious.write_text("not ours", encoding="utf-8")
            swapped.append(kind)

    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert swapped and caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", (swapped, caught.value)
    assert (scratch_now(tmp_path / "scratch-saved" / base.name) / "f.txt").exists(), "the original scratch is still behind, so it was not removed"
    if kind == "populated-directory":
        assert precious.exists(), "a directory that is not ours was removed through the replaced root"


def test_a_scratch_root_replaced_while_the_scratch_was_being_prepared_is_never_removed_through(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    root = tmp_path / "scratch"
    seen: dict[str, Path] = {}

    def hung_after_a_swap(git_bin, args, cwd, clone, timeout=None):
        base = Path(cwd)
        seen["base"] = base
        os.rename(root, tmp_path / "scratch-saved")                      # the swap lands during the long preparation, before any removal
        root.mkdir()
        (root / base.name).mkdir()
        seen["precious"] = root / base.name / "precious.txt"
        seen["precious"].write_text("not ours", encoding="utf-8")
        raise ob.Unknown("ROUTE_TIMEOUT", "git clone still running after 2s; its process tree was killed")

    monkeypatch.setattr(ob, "run_isolated_git", hung_after_a_swap)
    with pytest.raises(ob.Unknown) as caught:
        ob.make_exercise_scratch(bus, root)
    assert caught.value.code == "ROUTE_TIMEOUT", caught.value            # the failure that ended the preparation stays the outcome
    assert seen["precious"].exists(), "the replacement root's scratch-named directory was removed"
    assert (tmp_path / "scratch-saved" / seen["base"].name).exists()


# Adversary pass r3 (head 26648c79): A1 the rmtree error hook acts on a directory renamed away and replaced by another ORDINARY one (no
# link, same path string) between the outer look and the hook; A2 a creation-time pin that could not be read is never completed from
# whatever is found at removal.

def test_the_error_hook_never_chmods_or_unlinks_inside_a_scratch_replaced_by_an_ordinary_directory(tmp_path, monkeypatch):
    import stat as stat_module
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "hookswap")
    base.mkdir(parents=True)
    (base / "f.txt").write_text("original", encoding="utf-8")
    acted: list[str] = []
    real_rename = os.rename

    def rename_then_lose_the_scratch_to_a_swap(src, dst, *args, **kwargs):
        real_rename(src, dst, *args, **kwargs)
        if ".rm-" in os.fspath(dst) and not acted:                       # r6: right after the private rename, before its hold opens
            acted.append(os.fspath(dst))
            real_rename(dst, tmp_path / "scratch-saved")                 # the scratch is renamed away and another ordinary directory takes its name
            Path(dst).mkdir()
            planted = Path(dst) / "precious.txt"
            planted.write_text("not ours", encoding="utf-8")
            os.chmod(planted, stat_module.S_IREAD)

    monkeypatch.setattr(ob.os, "rename", rename_then_lose_the_scratch_to_a_swap)
    rename_through_os_rename(monkeypatch)
    monkeypatch.setattr(ob, "_remove_tree", lambda path, *a: (_ for _ in ()).throw(AssertionError(f"walked {path}")))
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    monkeypatch.setattr(ob.os, "rename", real_rename)
    assert acted, "the swap seam never ran"
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    precious = scratch_now(base) / "precious.txt"                        # the replacement stays where it is (r6 A1: never moved back), untouched
    try:
        assert precious.exists() and not os.access(precious, os.W_OK), "the replacement's file was made writable or deleted"
        assert (tmp_path / "scratch-saved" / "f.txt").exists()
    finally:
        if precious.exists():
            os.chmod(precious, stat_module.S_IREAD | stat_module.S_IWRITE)


@pytest.mark.parametrize("missing", ["scratch", "root"])
def test_a_creation_pin_that_could_not_be_read_is_never_completed_from_a_replacement(tmp_path, monkeypatch, missing):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "halfpin")
    base.mkdir(parents=True)
    (base / "f.txt").write_text("original", encoding="utf-8")
    real_root_id, real_work_id = ob._file_id(root, follow=True), ob._file_id(base)
    ob._SCRATCH_PINS[ob._scratch_key(base)] = (None if missing == "root" else real_root_id, None if missing == "scratch" else real_work_id)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    precious = root / base.name / "precious.txt"
    if missing == "scratch":                                             # only the object whose identity was never read is replaced
        os.rename(base, tmp_path / "scratch-saved")
        base.mkdir()
        precious.write_text("not ours", encoding="utf-8")
    else:
        os.rename(root, tmp_path / "scratch-saved")                      # the root is replaced by an empty ordinary directory
        root.mkdir()
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    if missing == "scratch":
        assert precious.exists(), "a replacement was adopted as the scratch and removed"
        assert (tmp_path / "scratch-saved" / "f.txt").exists()
    else:
        assert (tmp_path / "scratch-saved" / base.name / "f.txt").exists(), "absence behind a replaced root was read as a removal"


@pytest.mark.parametrize("which,nth", [("scratch", 1), ("scratch", 2), ("root", 1), ("root", 2)])
def test_r7_g4_one_transient_failed_creation_read_is_read_again_under_the_hold_and_the_scratch_is_removed(tmp_path, monkeypatch, which, nth):
    """CoS G4 (6763384196), literal: one identity read at creation fails once (the scratch's first read, as in the repro, and each
    other read). It is read again AT ONCE, under the same _DirHold(base) and with no pause, so the pin is completed and the scratch
    removed, never leaked as EXERCISE_SCRATCH_NOT_REMOVED. (Replaces test_a_creation_pin_is_one_read_and_an_unreadable_one_stays_unknown,
    which pinned the leak for a single failure; a failure that repeats is the residual row below.)"""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "pinonce")
    base.mkdir(parents=True)
    real, seen, failed = ob._file_id, [], []

    def fails_once(path, follow=False):
        seen.append(follow)                                             # follow=True is the root's read, False the scratch's
        if not failed and seen.count(which == "root") == nth:
            failed.append(path)
            return None                                                 # one transient failure of exactly this read
        return real(path, follow)

    def no_pause(seconds):
        raise AssertionError(f"the pin paused {seconds}s: a pause is a swap window")

    monkeypatch.setattr(ob, "_file_id", fails_once)
    monkeypatch.setattr(ob.time, "sleep", no_pause)
    ob._pin_scratch(base, root)
    monkeypatch.setattr(ob, "_file_id", real)
    assert failed, "the injected failure never ran"
    assert ob._SCRATCH_PINS[ob._scratch_key(base)] == (real(ob._real(root), True), real(base)), ob._SCRATCH_PINS[ob._scratch_key(base)]
    ob.remove_exercise_scratch(base, root)
    assert list(root.iterdir()) == [], list(root.iterdir())


def test_r7_g9_a_creation_read_that_fails_twice_in_a_row_stays_unknown_and_the_scratch_is_the_typed_leftover(tmp_path, monkeypatch):
    """The named fail-closed residue of G4 (G9): an identity that cannot be read on its immediate re-read either is recorded as
    None, never completed later; removal leaves the scratch as the typed EXERCISE_SCRATCH_NOT_REMOVED. Still no pause."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "pintwice")
    base.mkdir(parents=True)
    real, seen = ob._file_id, []

    def failing_read(path, follow=False):
        seen.append(follow)
        return None                                                     # every read fails, so each re-read fails too

    def no_pause(seconds):
        raise AssertionError(f"the pin paused {seconds}s: a pause is a swap window")

    monkeypatch.setattr(ob, "_file_id", failing_read)
    monkeypatch.setattr(ob.time, "sleep", no_pause)
    ob._pin_scratch(base, root)
    monkeypatch.setattr(ob, "_file_id", real)
    assert ob._SCRATCH_PINS[ob._scratch_key(base)] == (None, None)
    assert len(seen) == 8, f"each of the four reads is made once and re-read once, never more: {seen}"
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert base.is_dir(), "an unpinned scratch was removed"


def test_a_replacement_made_between_the_root_read_and_the_scratch_read_is_not_pinned_as_the_creation(tmp_path, monkeypatch):
    """Adversary r4 A1: the root's identity is read, then another process renames the new scratch aside and puts an ordinary
    replacement (holding a file) under the same name, then the scratch's identity is read. A fresh scratch is EMPTY, so a
    populated one is not what this run made: its identity is never pinned, and removal refuses it (typed) instead of deleting it."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "pinbetween")
    base.mkdir(parents=True)
    real, swapped = ob._file_id, []

    def swap_after_the_root_read(path, follow=False):
        got = real(path, follow)
        if follow and not swapped:
            try:
                os.rename(base, tmp_path / "original-aside")
            except PermissionError as refused:                          # r6: the pin holds the scratch, so Windows refuses the swap
                swapped.append(refused)
                return got
            swapped.append(True)
            base.mkdir()
            (base / "precious.txt").write_text("not ours", encoding="utf-8")
        return got

    monkeypatch.setattr(ob, "_file_id", swap_after_the_root_read)
    ob._pin_scratch(base, root)
    monkeypatch.setattr(ob, "_file_id", real)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    assert swapped, "the swap seam never ran"
    if swapped[0] is not True:                                           # the swap could not happen: the pin is the scratch itself
        assert os.name == "nt", swapped
        ob.remove_exercise_scratch(base, root)
        assert not base.exists()
        return
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert (base / "precious.txt").exists(), "the replacement made between the two reads was pinned as the creation and removed"
    assert (tmp_path / "original-aside").exists()


def test_a_root_whose_identity_changes_between_the_two_creation_reads_is_not_pinned(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "pinroot")
    base.mkdir(parents=True)
    real, root_reads = ob._file_id, [0]

    def root_changes(path, follow=False):
        if follow:
            root_reads[0] += 1
            return (1, 100 + root_reads[0])                              # a different object on every read
        return real(path, follow)

    monkeypatch.setattr(ob, "_file_id", root_changes)
    ob._pin_scratch(base, root)
    root_id, work_id = ob._SCRATCH_PINS.pop(ob._scratch_key(base))
    assert root_id is None and work_id == real(base), (root_id, work_id)


def test_a_scratch_whose_first_creation_read_failed_is_not_adopted_from_a_replacement_made_before_its_re_read(tmp_path, monkeypatch):
    """CoS G5: the swap is driven at the REAL seam (the earlier version swapped in a pause the pin never takes, so it never ran).
    The new scratch's first identity read fails, and in that instant, between the failure and its one immediate re-read (r7 G4),
    another process tries to rename the scratch aside and put a populated replacement under its name. The pin never sleeps.
    Windows: the pin holds the scratch (_DirHold), so the rename is refused, the pin is the scratch itself and it is removed.
    Elsewhere (no hold): the swap happens, the replacement is not empty, so it is never pinned and its file survives a typed refusal."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "pinswap")
    base.mkdir(parents=True)
    real, swapped = ob._file_id, []

    def fail_the_first_scratch_read_and_swap(path, follow=False):
        if follow or swapped:
            return real(path, follow)
        try:
            os.rename(base, tmp_path / "saved")
        except PermissionError as refused:                              # held by the pin: the swap is refused
            swapped.append(refused)
            return None
        base.mkdir()
        (base / "precious.txt").write_text("not ours", encoding="utf-8")
        swapped.append(True)
        return None

    def no_pause(seconds):
        raise AssertionError(f"the pin paused {seconds}s: a pause is a swap window")

    monkeypatch.setattr(ob, "_file_id", fail_the_first_scratch_read_and_swap)
    monkeypatch.setattr(ob.time, "sleep", no_pause)
    ob._pin_scratch(base, root)
    monkeypatch.setattr(ob, "_file_id", real)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    assert swapped, "the swap seam never ran"
    if os.name == "nt":
        assert isinstance(swapped[0], PermissionError), swapped         # the hold refused the rename: nothing was swapped
        ob.remove_exercise_scratch(base, root)
        assert list(root.iterdir()) == [], list(root.iterdir())
        return
    assert swapped[0] is True, swapped
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert (base / "precious.txt").exists(), "a replacement made before the re-read was pinned as the creation and removed"
    assert (tmp_path / "saved").is_dir()


def stale_bus_pair(tmp_path: Path, name: str, behind: bool) -> Path:
    """A bus clone of its own upstream, `behind` commits behind it (after a fetch) or not."""
    root = tmp_path / name
    root.mkdir()
    upstream, bus = make_upstream_and_bus(root)
    if behind:
        (upstream / "tools" / "two.txt").write_text("two", encoding="utf-8")
        git(upstream, "add", "-A")
        git(upstream, "commit", "-q", "-m", "two")
        git(bus, "fetch", "-q", "origin")
    return bus


@pytest.mark.parametrize("bus_behind", [False, True])
def test_the_stale_bus_verdict_ignores_a_git_dir_in_the_host_environment(tmp_path, monkeypatch, capsys, bus_behind):
    bus = stale_bus_pair(tmp_path, "real", behind=bus_behind)
    decoy = stale_bus_pair(tmp_path, "decoy", behind=not bus_behind)       # the opposite verdict, one variable away
    item = item_file(tmp_path, adopt("git log"))
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    assert exercise(tmp_path, item, bus) == 0
    captured = capsys.readouterr()
    assert statuses(captured.out) == ["ROUTE_OK"]
    assert ("ROUTE_BUS_STALE 1" in captured.err) is bus_behind, captured.err


def test_every_stale_bus_git_call_gets_the_route_isolation(tmp_path, monkeypatch, capsys):
    bus = stale_bus_pair(tmp_path, "real", behind=True)
    hostile = tmp_path / "host.gitconfig"
    hostile.write_text("[alias]\n\trev-list = !exit 9\n[core]\n\tfsmonitor = true\n\thooksPath = /nonexistent\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    calls: list[tuple[list[str], dict[str, str]]] = []
    real_popen = subprocess.Popen

    def spy(argv, *args, **kwargs):
        calls.append((list(argv), dict(kwargs.get("env") or os.environ)))
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(ob.subprocess, "Popen", spy)
    assert exercise(tmp_path, item_file(tmp_path, adopt("git log")), bus) == 0
    monkeypatch.setattr(ob.subprocess, "Popen", real_popen)
    assert "ROUTE_BUS_STALE 1" in capsys.readouterr().err
    stale = [(argv, env) for argv, env in calls if "--verify" in argv or "rev-list" in argv]
    assert len(stale) >= 2, [argv for argv, _env in calls]
    for argv, env in stale:
        pairs = git_dash_c(argv)
        assert set(ob.ROUTE_GIT_HARDENING) <= set(pairs) and "core.fsmonitor=false" in pairs, pairs
        assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_ATTR_NOSYSTEM"] == "1"
        assert Path(env["GIT_CONFIG_GLOBAL"]) != hostile and Path(env["GIT_CONFIG_GLOBAL"]).name == ob.ROUTE_EMPTY_GLOBAL_CONFIG
        assert not [k for k in env if k.startswith("GIT_") and k not in
                    {"GIT_CONFIG_NOSYSTEM", "GIT_ATTR_NOSYSTEM", "GIT_TERMINAL_PROMPT", "GIT_CONFIG_GLOBAL"}], sorted(env)
        assert os.path.isabs(argv[0])


# ---------- r5: review keys on 2ccc728a (opus B1/B2, sol A1/A2) and CoS G2/G3 ----------
# A2: a pinned scratch whose root resolves differently between the two initial reads is the typed outcome, never a normal return.
# A1: a scratch swapped (for an ordinary replacement, or for a link) after the last look is never removed through: removal first
#     renames the scratch to a private name and removes only an object whose identity is the creation pin.
# G2: the route loop's cleanup keeps the FIRST failure. G3: the creation pin never outlives its make/remove pairing.

def test_a_pinned_scratch_whose_root_resolves_differently_between_the_two_initial_reads_is_typed(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "realpathswap")
    base.mkdir(parents=True)
    (base / "f.txt").write_text("original", encoding="utf-8")
    ob._pin_scratch(base, root)
    real, calls = os.path.realpath, [0]
    elsewhere = str(tmp_path / "outside")

    def realpath(path, *args, **kwargs):
        calls[0] += 1
        got = real(path, *args, **kwargs)
        return elsewhere if calls[0] == 2 else got                       # the root is redirected between the first and the second read

    monkeypatch.setattr(ob.os.path, "realpath", realpath)
    try:
        with pytest.raises(ob.Unknown) as caught:
            ob.remove_exercise_scratch(base, root)
    finally:
        monkeypatch.setattr(ob.os.path, "realpath", real)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert (base / "f.txt").exists(), "nothing is removed when the root cannot be pinned"
    assert ob._scratch_key(base) not in ob._SCRATCH_PINS, "the pin outlived the removal"


def test_an_unpinned_scratch_whose_root_resolves_elsewhere_is_still_left_alone(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "notours")
    base.mkdir(parents=True)
    real, calls = os.path.realpath, [0]

    def realpath(path, *args, **kwargs):
        calls[0] += 1
        return str(tmp_path / "outside") if calls[0] == 2 else real(path, *args, **kwargs)

    monkeypatch.setattr(ob.os.path, "realpath", realpath)
    try:
        ob.remove_exercise_scratch(base, root)                           # never made by this run: not ours, nothing to report
    finally:
        monkeypatch.setattr(ob.os.path, "realpath", real)
    assert base.exists()


def swap_scratch_after_its_last_look(monkeypatch, base: Path, replace) -> list[bool]:
    """The seam the review keys named: the scratch's identity is read (the last look before removal), and THEN another process
    renames it aside and `replace(base)` puts something else under the same name."""
    real, swapped = os.lstat, []

    def read_then_swap(path, *args, **kwargs):                           # r6: the removal's last look is its strict lstat of the name
        got = real(path, *args, **kwargs)
        if not swapped and Path(path).name == base.name:
            swapped.append(True)
            os.rename(base, base.parent.parent / "original-aside")
            replace(base)
        return got

    monkeypatch.setattr(ob.os, "lstat", read_then_swap)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    return swapped


def test_a_scratch_swapped_for_an_ordinary_directory_after_the_last_look_is_never_removed(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "walkswap")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("original", encoding="utf-8")

    def replacement(path: Path) -> None:
        path.mkdir()
        (path / "precious.txt").write_text("not ours", encoding="utf-8")   # writable: rmtree needs no error hook to delete it

    swapped = swap_scratch_after_its_last_look(monkeypatch, base, replacement)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert swapped, "the seam never ran"
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert (scratch_now(base) / "precious.txt").exists(), "the replacement's file was deleted"   # never moved back (r6 A1)
    assert (tmp_path / "original-aside" / "f.txt").exists()


def walk_without_a_top_level_link_check(path, *args, **kwargs):
    """shutil._rmtree_unsafe as Windows runs it once rmtree's own top-level link check has passed: every name is a path string, so
    a scratch swapped for a junction AFTER that check is walked through."""
    with os.scandir(path) as entries:
        for entry in list(entries):
            if entry.is_dir(follow_symlinks=False) and not entry.is_junction():
                walk_without_a_top_level_link_check(entry.path)
            else:
                os.unlink(entry.path)
    os.rmdir(path)


def test_a_scratch_swapped_for_a_link_after_the_last_look_is_never_walked_through(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "linkswap")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("original", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.txt"
    precious.write_text("not ours", encoding="utf-8")                   # writable: the walk deletes it unless it never walks there
    kinds: list[str] = []
    swapped = swap_scratch_after_its_last_look(monkeypatch, base, lambda path: kinds.append(make_directory_link(path, outside)))
    monkeypatch.setattr(ob, "_remove_tree", walk_without_a_top_level_link_check)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert swapped and kinds, "the seam never ran"
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert precious.exists(), f"the {kinds[0]} target's file was deleted"
    assert (tmp_path / "original-aside" / "f.txt").exists()


def test_the_route_loop_keeps_its_own_failure_when_the_scratch_removal_also_fails(tmp_path, monkeypatch, capsys):
    bus = make_bus(tmp_path)
    body = adopt("git log")
    assert ob.extract_routes(body), "the fixture names no route"

    def route_fails(*_a, **_k):
        raise ob.Unknown("ROUTE_SOMETHING_ELSE", "the route itself failed first")   # not ROUTE_TREE_NOT_EXITED: re-raised by the loop

    monkeypatch.setattr(ob, "exercise_route", route_fails)
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, None))
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    with pytest.raises(ob.Unknown) as caught:
        ob.run_exercise([("item", {"body": body})], bus, 30, {}, tmp_path / "scratch")
    assert caught.value.code == "ROUTE_SOMETHING_ELSE", caught.value     # the first failure stays the outcome ...
    assert "EXERCISE_SCRATCH_NOT_REMOVED" in caught.value.detail, caught.value.detail   # ... and the leftover is recorded on it


def test_the_route_loop_keeps_a_non_typed_failure_and_reports_the_leftover(tmp_path, monkeypatch, capsys):
    bus = make_bus(tmp_path)

    def route_interrupted(*_a, **_k):
        raise KeyboardInterrupt

    monkeypatch.setattr(ob, "exercise_route", route_interrupted)
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, None))
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    with pytest.raises(KeyboardInterrupt):
        ob.run_exercise([("item", {"body": adopt("git log")})], bus, 30, {}, tmp_path / "scratch")
    assert "EXERCISE_SCRATCH_NOT_REMOVED" in capsys.readouterr().err


def test_a_creation_pin_is_dropped_even_when_the_removal_itself_raises(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "pinleak")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    real = os.path.realpath

    def realpath_fails(*_a, **_k):
        raise OSError(5, "Access is denied")

    monkeypatch.setattr(ob.os.path, "realpath", realpath_fails)
    try:
        with pytest.raises(Exception):
            ob.remove_exercise_scratch(base, root)
    finally:
        monkeypatch.setattr(ob.os.path, "realpath", real)
    assert ob._scratch_key(base) not in ob._SCRATCH_PINS, "a pin outlived its removal and would match a later scratch of that name"


def test_a_scratch_made_but_whose_pin_could_not_be_recorded_is_never_adopted_and_is_named_as_left_behind(tmp_path, monkeypatch):
    """CoS G3 under the r6 threat model (adversary r6 A3 on 0fd4c235): mkdtemp made it, then recording its pin failed. The cleanup
    must never adopt whatever then holds its name (a replacement put there before the cleanup would be deleted), so an unpinned
    scratch is the typed EXERCISE_SCRATCH_NOT_REMOVED naming it, attached to the preparation's own failure; no pin is left behind.
    (Was: the cleanup removed it by adopting its identity at removal time, which is the adoption A3 turns into a deletion.)"""
    bus = make_bus(tmp_path)
    root = tmp_path / "scratch"

    def pin_fails(base, scratch_root):
        raise RuntimeError("the pin could not be recorded")

    monkeypatch.setattr(ob, "_pin_scratch", pin_fails)
    with pytest.raises(RuntimeError) as caught:
        ob.make_exercise_scratch(bus, root)
    leftover = list(root.iterdir())
    assert len(leftover) == 1, leftover
    assert any("EXERCISE_SCRATCH_NOT_REMOVED" in note and leftover[0].name in note for note in getattr(caught.value, "__notes__", [])), caught.value
    assert not [k for k in ob._SCRATCH_PINS if os.path.normcase(str(root)) in k], ob._SCRATCH_PINS


# Adversary pass r5 (head d9b83d99): A2 the private rename and its check take time, and the walk must not start after the deadline;
# A3 a transient denial on the private name's metadata is retried, not final; A4 a broken stderr never replaces the first failure.

def test_a_verified_rename_that_ends_past_the_deadline_still_completes_the_removal(tmp_path, monkeypatch):
    """r6 HUB RULING F2 (inverts the r5 test of d9b83d99 A2): the attempt passed its deadline check at 14.9 s, and the private
    rename plus its identity check end at 15.1 s with the identities matching. That is SUCCESS: the budget bounds attempts and
    never discards a completed, verified removal."""
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "lateaside")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    clock = [0.0]                                                        # the loop's deadline is ROUTE_SCRATCH_REMOVE_DEADLINE from here
    walks: list[str] = []
    real_rename, real_walk = os.rename, ob._remove_tree

    def slow_rename(src, dst, *args, **kwargs):
        clock[0] = 14.9                                                  # the check at the loop top passed; the rename starts ...
        real_rename(src, dst, *args, **kwargs)
        clock[0] = ob.ROUTE_SCRATCH_REMOVE_DEADLINE + 0.1                # ... and it and its identity check end past the deadline

    def walk(path, *args):
        walks.append((str(path), clock[0]))
        real_walk(path, *args)

    monkeypatch.setattr(ob.os, "rename", slow_rename)
    rename_through_os_rename(monkeypatch)
    monkeypatch.setattr(ob, "_remove_tree", walk)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s)))
    ob.remove_exercise_scratch(base, root)
    monkeypatch.setattr(ob.os, "rename", real_rename)
    assert len(walks) == 1 and ".rm-" in walks[0][0], walks              # one walk, under the private name only
    assert walks[0][1] > ob.ROUTE_SCRATCH_REMOVE_DEADLINE, walks         # the scenario: the verified walk ran past the deadline
    assert list(root.iterdir()) == [], list(root.iterdir())              # ... and its completed removal was kept


def test_a_transient_denial_on_the_private_names_metadata_is_retried_not_final(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "denyonce")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)                                          # pinned EMPTY, as mkdtemp made it; content comes after
    (base / "f.txt").write_text("ours", encoding="utf-8")
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    real_lstat, denied = os.lstat, []

    def lstat(path, *a, **k):
        if ".rm-" in os.fspath(path) and not denied:
            denied.append(os.fspath(path))
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real_lstat(path, *a, **k)

    monkeypatch.setattr(ob.os, "lstat", lstat)
    ob.remove_exercise_scratch(base, root)                               # a single transient denial is ridden out
    monkeypatch.setattr(ob.os, "lstat", real_lstat)
    assert denied, "the seam never ran"
    assert list(root.iterdir()) == [], list(root.iterdir())


def test_a_broken_stderr_never_replaces_the_preparations_own_failure(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)

    def git_fails(*_a, **_k):
        raise ob.GitError("first failure")

    def broken_stderr(line, stream=None):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(ob, "run_isolated_git", git_fails)
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, None))
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    monkeypatch.setattr(ob, "_route_say", broken_stderr)
    with pytest.raises(ob.GitError) as caught:
        ob.make_exercise_scratch(bus, tmp_path / "scratch")
    assert "first failure" in str(caught.value)
    assert any("EXERCISE_SCRATCH_NOT_REMOVED" in note for note in getattr(caught.value, "__notes__", [])), caught.value


# r6 HUB RULING (E): fix the CLASS, not one race shape after another. Threat model (remove_exercise_scratch's docstring): never
# delete through a link or junction, never delete a tree whose identity was not pinned, never report "removed" unless the tree is
# gone; against a concurrent swapper the guarantee is FAIL-SAFE (typed refusal, data left in place). Each row feeds an adversary
# finding's literal `input`. F1: e16da5aa A1 (the root swapped for a junction to itself before the private rename) and d9b83d99 A1
# (the pin's identity reads and its emptiness scan served by two objects). F2: e16da5aa A2/A3 (a transient denial on a single
# metadata call after the private rename is retried in the one loop). F3: a stderr whose write raises BrokenPipeError(32).

class _BrokenStderr(io.TextIOBase):
    encoding = "utf-8"

    def write(self, _text):
        raise BrokenPipeError(32, "Broken pipe")


def _swap_root_for_a_junction_to_itself(root: Path, saved: Path) -> str:
    os.rename(root, saved)
    return make_directory_link(root, saved)


@pytest.mark.parametrize("moment", ["before-the-private-rename", "before-the-root-hold"])
def test_r6_f1_a_root_swapped_for_a_junction_to_itself_is_never_removed_through(tmp_path, monkeypatch, moment):
    """e16da5aa A1, literal: a valid pinned scratch holding writable f.txt; after the final outer look and before
    os.rename(work, private), the scratch root is renamed to `saved` and a junction to `saved` takes its name. The run's own files
    may only ever be removed through the real, held root: either the swap is refused because the root is held (Windows), or the
    attempt finds the junction and refuses, typed, with f.txt untouched. Never a removal through the junction, never "removed"."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    saved = tmp_path / "saved"
    swapped: list[object] = []
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    if moment == "before-the-private-rename":
        real_rename = os.rename

        def rename(src, dst, *args, **kwargs):
            if not swapped and ".rm-" in os.fspath(dst):
                try:
                    swapped.append(_swap_root_for_a_junction_to_itself(root, saved))
                except PermissionError as refused:                      # the root is held for the whole attempt
                    swapped.append(refused)
            return real_rename(src, dst, *args, **kwargs)

        monkeypatch.setattr(ob.os, "rename", rename)
        rename_through_os_rename(monkeypatch)
    else:
        real_hold = ob._DirHold

        class SwapThenHold(real_hold):
            def __enter__(self):
                if not swapped and os.path.normcase(self.path) == os.path.normcase(os.path.realpath(root)):
                    swapped.append(_swap_root_for_a_junction_to_itself(root, saved))
                return super().__enter__()

        monkeypatch.setattr(ob, "_DirHold", SwapThenHold)
    try:
        ob.remove_exercise_scratch(base, root)
        outcome = None
    except ob.Unknown as caught:
        outcome = caught
    finally:
        monkeypatch.undo()
    assert swapped, "the swap seam never ran"
    if isinstance(swapped[0], PermissionError):                          # the hold made the swap impossible: our own scratch is removed
        assert os.name == "nt" and outcome is None and not os.path.lexists(saved), (swapped, outcome)
        assert not ob._is_link_or_junction(root) and list(root.iterdir()) == [], list(root.iterdir())
        return
    assert outcome is not None and outcome.code == "EXERCISE_SCRATCH_NOT_REMOVED", outcome
    assert (scratch_now(saved / base.name) / "f.txt").read_text(encoding="utf-8") == "ours", "removed through the junction"


@pytest.mark.skipif(os.name != "nt", reason="the hold (a share-no-delete handle) is Windows-only; the POSIX residual is DISTINGUISHed")
def test_r6_f1_a_pin_whose_reads_and_scan_are_served_by_two_objects_never_pins_the_populated_one(tmp_path, monkeypatch):
    """d9b83d99 A1, literal: _pin_scratch for empty O; after the root read O is replaced by populated P (precious.txt), both
    scratch identity reads see P, empty O is put back for the scan, and P is restored before cleanup. With the scratch HELD while
    it is pinned, Windows refuses every one of those renames, so the pin is O's and P never exists; nothing not ours is removed."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    o_aside, p_aside = tmp_path / "O-aside", tmp_path / "P-aside"
    real_id, real_scandir = ob._file_id, os.scandir
    events: list[object] = []

    def make_p_take_the_name():
        os.rename(base, o_aside)
        base.mkdir()
        (base / "precious.txt").write_text("not ours", encoding="utf-8")

    def file_id(path, follow=False):
        got = real_id(path, follow)
        if follow and not events:                                        # straight after the root read: O -> P
            try:
                make_p_take_the_name()
                events.append("P")
            except PermissionError as refused:
                events.append(refused)
        return got

    def scandir(path=".", *args, **kwargs):
        if events == ["P"] and os.path.normcase(os.fspath(path)) == os.path.normcase(str(base)):
            os.rename(base, p_aside)                                     # empty O back for the scan
            os.rename(o_aside, base)
            events.append("O")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(ob, "_file_id", file_id)
    monkeypatch.setattr(ob.os, "scandir", scandir)
    ob._pin_scratch(base, root)
    monkeypatch.setattr(ob, "_file_id", real_id)
    monkeypatch.setattr(ob.os, "scandir", real_scandir)
    if "O" in events:                                                    # P restored before cleanup
        os.rename(base, o_aside)
        os.rename(p_aside, base)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    try:
        ob.remove_exercise_scratch(base, root)
    except ob.Unknown as caught:
        assert caught.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught
    assert events and isinstance(events[0], PermissionError), f"the swap was not refused while the scratch was pinned: {events}"
    assert not (base / "precious.txt").exists() and not o_aside.exists(), "P existed, so the hold did not bind the pin"
    precious = [p for p in tmp_path.rglob("precious.txt")]
    assert precious == [] or all(p.read_text(encoding="utf-8") == "not ours" for p in precious), precious


@pytest.mark.parametrize("which", ["os.stat", "os.lstat"])
def test_r6_f2_a_single_denied_root_read_after_the_private_rename_is_retried_not_final(tmp_path, monkeypatch, which):
    """e16da5aa A2, literal: a valid pinned scratch; only the post-rename read of the root raises PermissionError(13) once; every
    later operation would succeed. One denial is unknown, not "foreign": it is retried inside the one bounded loop. (r6 holds the
    root for the whole attempt, so no post-rename root read exists any more; the row below injects one on the private name.)"""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    real_rename, real_read = os.rename, getattr(os, which.split(".")[1])
    state = {"renamed": False, "denied": 0}
    real_root = os.path.normcase(os.path.realpath(root))

    def rename(src, dst, *args, **kwargs):
        real_rename(src, dst, *args, **kwargs)
        if ".rm-" in os.fspath(dst):
            state["renamed"] = True

    def read(path, *args, **kwargs):
        if state["renamed"] and not state["denied"] and os.path.normcase(os.fspath(path)) == real_root:
            state["denied"] += 1
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real_read(path, *args, **kwargs)

    monkeypatch.setattr(ob.os, "rename", rename)
    rename_through_os_rename(monkeypatch)
    monkeypatch.setattr(ob.os, which.split(".")[1], read)
    ob.remove_exercise_scratch(base, root)                               # never the typed foreign-object outcome for one denial
    monkeypatch.undo()
    assert state["renamed"], "the private rename never ran"
    assert list(root.iterdir()) == [], list(root.iterdir())


def test_r6_f2_a_denied_private_read_then_a_held_put_back_is_retried_not_final(tmp_path, monkeypatch):
    """e16da5aa A3, literal: a valid pinned scratch; the first lstat(private) raises PermissionError(13) once, then
    rename(private, work) raises a sharing violation once; later attempts would succeed within the 15 s budget. Unknown is retried
    under the private name (no put-back is needed for a transient), and the removal completes."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    real_rename, real_lstat = os.rename, os.lstat
    faults = {"lstat": 0, "rename": 0}

    def lstat(path, *args, **kwargs):
        if ".rm-" in os.fspath(path) and not faults["lstat"]:
            faults["lstat"] += 1
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    def rename(src, dst, *args, **kwargs):
        if ".rm-" in os.fspath(src) and not faults["rename"]:
            faults["rename"] += 1
            err = PermissionError(13, "The process cannot access the file because it is being used by another process", os.fspath(src))
            err.winerror = 32
            raise err
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(ob.os, "lstat", lstat)
    monkeypatch.setattr(ob.os, "rename", rename)
    rename_through_os_rename(monkeypatch)
    ob.remove_exercise_scratch(base, root)
    monkeypatch.undo()
    assert faults["lstat"] == 1, faults
    assert list(root.iterdir()) == [], list(root.iterdir())


def test_r6_f3_a_real_broken_stderr_never_replaces_the_preparations_failure(tmp_path, monkeypatch):
    """F3: sys.stderr itself is a pipe whose reader closed (every write raises BrokenPipeError(32)); the preparation fails with
    GitError and the scratch stays held, so the cleanup's own typed failure must be reported. GitError stays the outcome."""
    bus = make_bus(tmp_path)

    def git_fails(*_a, **_k):
        raise ob.GitError("first failure")

    monkeypatch.setattr(ob, "run_isolated_git", git_fails)
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, None))
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.3)
    monkeypatch.setattr(ob.sys, "stderr", _BrokenStderr())
    with pytest.raises(ob.GitError) as caught:
        ob.make_exercise_scratch(bus, tmp_path / "scratch")
    monkeypatch.undo()
    assert "first failure" in str(caught.value)
    assert any("EXERCISE_SCRATCH_NOT_REMOVED" in note for note in getattr(caught.value, "__notes__", [])), caught.value


@pytest.mark.parametrize("raised, code", [("Unknown", 2), ("Refusal", 1)])
def test_r6_f3_a_broken_stderr_never_replaces_mains_typed_exit_code(monkeypatch, raised, code):
    """F3 siblings (main's exception paths): reporting a typed failure on a broken stderr still returns its typed exit code."""
    def failing(_argv):
        raise ob.Unknown("EXERCISE_SCRATCH_NOT_REMOVED", "left behind") if raised == "Unknown" else ob.Refusal("refused")

    monkeypatch.setattr(ob, "_apply_cli_config", failing)
    monkeypatch.setattr(ob.sys, "stderr", _BrokenStderr())
    assert ob.main(["validate"]) == code


def test_r6_f1_a_scratch_moved_off_its_private_name_by_another_process_is_never_reported_removed(tmp_path, monkeypatch):
    """Threat model: "removed" only when the tree is gone. After the private rename (and before its hold opens) another process
    moves the scratch elsewhere: its private name is then empty, but this removal deleted nothing. That is typed, never a return."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    moved = tmp_path / "moved-away"
    real_rename = os.rename

    def rename(src, dst, *args, **kwargs):
        real_rename(src, dst, *args, **kwargs)
        if ".rm-" in os.fspath(dst) and not moved.exists():
            real_rename(dst, moved)

    monkeypatch.setattr(ob.os, "rename", rename)
    rename_through_os_rename(monkeypatch)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    monkeypatch.undo()
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert (moved / "f.txt").read_text(encoding="utf-8") == "ours", "the moved scratch was touched"

# Adversary pass r6 (head 0fd4c235): A1 a pinned scratch moved off its own name before the first lookup is not "removed";
# A2 Windows realpath keeps the \\?\ prefix when its verification lookup fails, which must not read as "foreign";
# A3 a scratch whose pin record was never written is never adopted; A4 a child's identity comes from the listing itself.
# A6: the post-rename denial row must really inject its denial.

def test_r6_a6_a_single_denied_metadata_read_right_after_the_private_rename_is_injected_and_retried(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    real_rename, real_lstat = os.rename, os.lstat
    state = {"renamed": False, "denied": []}
    names = {os.path.normcase(os.path.realpath(root))}

    def rename(src, dst, *args, **kwargs):
        real_rename(src, dst, *args, **kwargs)
        if ".rm-" in os.fspath(dst):
            state["renamed"] = True
            names.add(os.path.normcase(os.fspath(dst)))

    def lstat(path, *args, **kwargs):
        if state["renamed"] and not state["denied"] and os.path.normcase(os.fspath(path)) in names:
            state["denied"].append(os.fspath(path))
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(ob.os, "rename", rename)
    rename_through_os_rename(monkeypatch)
    monkeypatch.setattr(ob.os, "lstat", lstat)
    ob.remove_exercise_scratch(base, root)
    monkeypatch.undo()
    assert len(state["denied"]) == 1, state                              # the denial was really injected, once
    assert list(root.iterdir()) == [], list(root.iterdir())


def test_r6_a1_a_pinned_scratch_moved_off_its_name_before_the_first_lookup_is_never_reported_removed(tmp_path, monkeypatch):
    """0fd4c235 A1, literal: pin while empty, add f.txt, rename base to root/saved before the first scratch lookup, then remove."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    os.rename(base, root / "saved")
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert (root / "saved" / "f.txt").read_text(encoding="utf-8") == "ours"


@pytest.mark.skipif(os.name != "nt", reason="the extended-length prefix is a Windows realpath behavior")
@pytest.mark.parametrize("fault", ["prefix-kept", "denied-once"])
def test_r6_a2_a_transient_failure_inside_realpath_is_never_foreign(tmp_path, monkeypatch, fault):
    """0fd4c235 A2, literal: during _in_place(root) the native lookup succeeds and only realpath's verification of the unprefixed
    name fails once, so realpath returns the \\\\?\\-prefixed name; or the strict lookup itself raises a sharing violation once."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    real_realpath, hits = os.path.realpath, []
    real_root = os.path.normcase(real_realpath(root))

    def realpath(path, *args, strict=False, **kwargs):
        got = real_realpath(path, *args, strict=strict, **kwargs)
        if strict and not hits and os.path.normcase(got) == real_root:
            hits.append(fault)
            if fault == "denied-once":
                raise PermissionError(13, "The process cannot access the file because it is being used by another process", os.fspath(path))
            return "\\\\?\\" + got
        return got

    monkeypatch.setattr(ob.os.path, "realpath", realpath)
    ob.remove_exercise_scratch(base, root)
    monkeypatch.undo()
    assert hits == [fault], hits
    assert list(root.iterdir()) == [], list(root.iterdir())


def test_r6_a3_a_scratch_whose_pin_record_was_never_written_is_never_adopted_from_a_replacement(tmp_path, monkeypatch):
    """0fd4c235 A3, literal: make_exercise_scratch(valid bus, root); an exception before _pin_scratch records anything, and before
    the cleanup the original is renamed aside and an ordinary replacement holding precious.txt takes its name."""
    bus = make_bus(tmp_path)
    root = tmp_path / "scratch"
    seen: dict[str, Path] = {}

    def pin_fails_after_a_swap(base, scratch_root):
        seen["base"] = base
        os.rename(base, tmp_path / "original-aside")
        base.mkdir()
        (base / "precious.txt").write_text("not ours", encoding="utf-8")
        raise RuntimeError("before the pin record was written")

    monkeypatch.setattr(ob, "_pin_scratch", pin_fails_after_a_swap)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    with pytest.raises(RuntimeError):
        ob.make_exercise_scratch(bus, root)
    assert (seen["base"] / "precious.txt").read_text(encoding="utf-8") == "not ours", "the replacement was adopted and deleted"
    assert (tmp_path / "original-aside").is_dir()


def test_r6_a4_a_child_swapped_after_the_listing_is_never_walked_or_deleted(tmp_path, monkeypatch):
    """0fd4c235 A4, literal: inside the privately renamed scratch, `objects` is listed; after the listing and before the child is
    checked, `objects` is renamed aside and an ordinary directory holding precious.txt is moved into its name."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    (base / "objects").mkdir(parents=True)                              # not pinned: identities adopted at the first look
    (base / "objects" / "ours.txt").write_text("ours", encoding="utf-8")
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "precious.txt").write_text("not ours", encoding="utf-8")
    real_list, swapped = ob._list_dir, []

    def list_then_swap(path, record=None):
        got = real_list(path, record)
        for child, _identity, is_dir in got:
            if is_dir and os.path.basename(child) == "objects" and not swapped:
                os.rename(child, tmp_path / "objects-aside")
                os.rename(planted, child)
                swapped.append(child)
        return got

    monkeypatch.setattr(ob, "_list_dir", list_then_swap)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert swapped, "the swap seam never ran"
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    survivors = list(tmp_path.rglob("precious.txt"))
    assert len(survivors) == 1 and survivors[0].read_text(encoding="utf-8") == "not ours", survivors
    assert (tmp_path / "objects-aside" / "ours.txt").exists()

# Adversary pass r6 (head bc0fecd8): A1 a put-back only ever moves the pinned scratch under the pinned root; A2 a retry never
# re-pins a name with a new identity; A3 a scratch root named through a junction; A4 a lone UTF-16 surrogate in a listed name;
# A5 a tree nested deeper than the walk can hold is a typed refusal, never a RecursionError.

def test_r6_bc_a1_a_foreign_root_never_gets_an_unrelated_private_name_directory_moved_back(tmp_path, monkeypatch):
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "f.txt").write_text("ours", encoding="utf-8")
    monkeypatch.setattr(ob, "_remove_tree", FlakyScratchRemoval(ob._remove_tree, 1))   # attempt 1: a sharing violation
    seen: list[Path] = []
    real_time = ob.time

    def pause(_seconds):
        if not seen:
            private = scratch_now(base)
            seen.append(private)
            os.rename(root, tmp_path / "saved")
            root.mkdir()
            (root / private.name).mkdir()
            (root / private.name / "precious.txt").write_text("not ours", encoding="utf-8")

    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert seen and caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert (root / seen[0].name / "precious.txt").exists() and not base.exists(), list(root.iterdir())
    assert (tmp_path / "saved" / seen[0].name / "f.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="the held-handle open of a listed child is the Windows path")
def test_r6_bc_a2_a_retry_never_accepts_a_new_identity_under_a_name_already_listed(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    (base / "objects").mkdir(parents=True)
    (base / "objects" / "ours.txt").write_text("ours", encoding="utf-8")
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "precious.txt").write_text("not ours", encoding="utf-8")
    real_hold, swapped = ob._DirHold, []

    class SwapThenFail(real_hold):
        def __enter__(self):
            if not swapped and os.path.basename(self.path) == "objects":
                os.rename(self.path, tmp_path / "objects-aside")        # listed with one identity, then moved out ...
                os.rename(planted, self.path)                            # ... and another takes the name
                swapped.append(self.path)
                raise PermissionError(13, "The process cannot access the file because it is being used by another process", self.path)
            return super().__enter__()

    monkeypatch.setattr(ob, "_DirHold", SwapThenFail)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert swapped and caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    survivors = list(tmp_path.rglob("precious.txt"))
    assert len(survivors) == 1 and survivors[0].read_text(encoding="utf-8") == "not ours", survivors


def test_r6_bc_a3_a_scratch_root_named_through_a_junction_is_made_and_removed_under_its_real_path(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    real_root = tmp_path / "scratch-real"
    real_root.mkdir()
    alias = tmp_path / "scratch-link"
    make_directory_link(alias, real_root)

    def hung(*_a, **_k):                                                 # the preparation fails, so its own cleanup removes the scratch
        raise ob.Unknown("ROUTE_TIMEOUT", "git clone still running after 2s; its process tree was killed")

    monkeypatch.setattr(ob, "run_isolated_git", hung)
    with pytest.raises(ob.Unknown) as caught:
        ob.make_exercise_scratch(bus, alias)
    assert caught.value.code == "ROUTE_TIMEOUT" and "EXERCISE_SCRATCH_NOT_REMOVED" not in caught.value.detail, caught.value
    assert list(real_root.iterdir()) == [], list(real_root.iterdir())


@pytest.mark.skipif(os.name != "nt", reason="NTFS names may hold a lone UTF-16 surrogate")
def test_r6_bc_a4_a_listed_name_with_a_lone_surrogate_is_removed(tmp_path):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    with open(os.path.join(str(base), "\ud800"), "w", encoding="utf-8") as odd:
        odd.write("ours")
    ob.remove_exercise_scratch(base, root)
    assert list(root.iterdir()) == [], list(root.iterdir())


def test_r6_bc_a5_a_tree_nested_deeper_than_the_walk_can_hold_is_a_typed_refusal(tmp_path, monkeypatch):
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    deep = base
    for _ in range(40):
        deep = deep / "a"
    deep.mkdir(parents=True)
    (deep / "f.txt").write_text("ours", encoding="utf-8")
    depth = len(__import__("inspect").stack())
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(depth + 25)                                  # the walk cannot hold 40 levels on top of this stack
    try:
        with pytest.raises(ob.Unknown) as caught:
            ob.remove_exercise_scratch(base, root)
    finally:
        sys.setrecursionlimit(limit)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert (scratch_now(base).joinpath(*["a"] * 40) / "f.txt").exists()

# r7: Dot 786-D824-B1. The walk recorded a sibling's identity only when its own iteration began, so a transient failure on the
# first sibling A, then the not-yet-processed sibling B replaced by an ordinary B' during the retry pause, let the next listing
# adopt B' and delete its data. Every entry of a directory's FIRST listing is now recorded before any fallible per-entry step, and
# a relisting may only drop entries: a new name, or a listed name with another identity, is a typed refusal with the data left.
@pytest.mark.parametrize("change", ["replaced", "added"])
def test_r7_b1_a_sibling_changed_during_a_retry_pause_is_refused_and_never_adopted(tmp_path, monkeypatch, change):
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)                                          # pinned EMPTY, as mkdtemp made it; content comes after
    for name in ("A", "B"):
        (base / name).mkdir()
        (base / name / "ours.txt").write_text("ours", encoding="utf-8")
    real_hold, failed, changed = ob._DirHold, [], []

    class FailTheFirstSiblingOnce(real_hold):
        def __enter__(self):
            if not failed and os.path.basename(self.path) in ("A", "B"):
                failed.append(os.path.basename(self.path))               # whichever sibling the walk enters first fails, once
                raise PermissionError(13, "The process cannot access the file because it is being used by another process", self.path)
            return super().__enter__()

    real_time = ob.time

    def pause(seconds):                                                  # the retry pause: the sibling not yet processed changes
        if failed and not changed:
            where = scratch_now(base)
            other = "B" if failed[0] == "A" else "A"
            if change == "replaced":
                os.rename(where / other, tmp_path / "other-aside")
                (where / other).mkdir()
                (where / other / "precious.txt").write_text("not ours", encoding="utf-8")
            else:
                (where / "C").mkdir()
                (where / "C" / "precious.txt").write_text("not ours", encoding="utf-8")
            changed.append(other)

    monkeypatch.setattr(ob, "_DirHold", FailTheFirstSiblingOnce)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert failed and changed, (failed, changed)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    survivors = list(root.rglob("precious.txt"))
    assert len(survivors) == 1 and survivors[0].read_text(encoding="utf-8") == "not ours", survivors
    if change == "replaced":
        assert (tmp_path / "other-aside" / "ours.txt").exists()


def test_r7_b1_a_relisting_that_only_drops_entries_still_completes(tmp_path, monkeypatch):
    """The other side of B1: entries removed before a transient failure are simply absent from the relisting, which is allowed,
    so a retried walk still finishes the removal."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    for name in ("A", "B", "C"):
        (base / name).mkdir()
        (base / name / "ours.txt").write_text("ours", encoding="utf-8")
    real_hold, entered = ob._DirHold, []

    class FailTheSecondSiblingOnce(real_hold):
        def __enter__(self):
            if os.path.basename(self.path) in ("A", "B", "C"):
                entered.append(os.path.basename(self.path))
                if len(entered) == 2:                                    # one sibling is already removed when the next one fails
                    raise PermissionError(13, "The process cannot access the file because it is being used by another process", self.path)
            return super().__enter__()

    monkeypatch.setattr(ob, "_DirHold", FailTheSecondSiblingOnce)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 2)
    ob.remove_exercise_scratch(base, root)
    assert len(entered) >= 4, entered
    assert list(root.iterdir()) == [], list(root.iterdir())

# Adversary pass r7 (head e2d403d9): A1 a put-back renames only the very object it verified (by handle); A2 identities read from
# a listing that failed part-way are kept, so a retry never adopts a replacement of an entry it already saw; A3 the scratch root
# recorded at creation is used as it is, so a transient failure of a later non-strict resolution is never a refusal.

@pytest.mark.skipif(os.name != "nt", reason="the put-back renames by handle on Windows; elsewhere it is the documented POSIX limit")
def test_r7_e2_a1_a_put_back_never_moves_a_directory_swapped_in_after_its_verification(tmp_path, monkeypatch):
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    for name in ("A", "B"):
        (base / name).mkdir()
        (base / name / "ours.txt").write_text("ours", encoding="utf-8")
    real_hold, failed, swapped = ob._DirHold, [], []

    class FailTheFirstSiblingOnce(real_hold):
        def __enter__(self):
            if not failed and os.path.basename(self.path) in ("A", "B"):
                failed.append(self.path)
                raise PermissionError(13, "The process cannot access the file because it is being used by another process", self.path)
            return super().__enter__()

    real_time = ob.time

    def private_now() -> Path:
        moved = [p for p in root.iterdir() if p.name.startswith(base.name + ".rm-")]
        assert len(moved) == 1, moved
        return moved[0]

    def pause(seconds):                                                  # a new entry appears: the relisting refuses (B1)
        if failed and not (private_now() / "C").exists():
            (private_now() / "C").mkdir()

    real_lexists = os.path.lexists

    def lexists_then_swap(path):                                         # the put-back's last look: then the private name is swapped
        got = real_lexists(path)
        if (os.path.normcase(str(path)) == os.path.normcase(str(base)) and not swapped and failed
                and (private_now() / "C").exists()):
            private = private_now()
            os.rename(private, tmp_path / "scratch-aside")
            private.mkdir()
            (private / "precious.txt").write_text("not ours", encoding="utf-8")
            swapped.append(private)
        return got

    monkeypatch.setattr(ob, "_DirHold", FailTheFirstSiblingOnce)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))
    monkeypatch.setattr(ob.os.path, "lexists", lexists_then_swap)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    try:
        with pytest.raises(ob.Unknown) as caught:
            ob.remove_exercise_scratch(base, root)
    finally:
        monkeypatch.setattr(ob.os.path, "lexists", real_lexists)
    assert swapped, "the swap seam never ran"
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    assert not base.exists(), "the directory swapped in after the put-back's verification was moved under the scratch's name"
    assert (swapped[0] / "precious.txt").exists() and "put back" not in caught.value.detail, caught.value.detail
    assert (tmp_path / "scratch-aside" / "A").exists() or (tmp_path / "scratch-aside" / "B").exists()


@pytest.mark.skipif(os.name != "nt", reason="the multi-response FileIdBothDirectoryInfo enumeration is the Windows listing")
def test_r7_e2_a2_an_entry_seen_by_a_listing_that_failed_part_way_is_never_adopted_from_a_replacement(tmp_path, monkeypatch):
    import ctypes
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    for name in ("A", "B"):
        (base / name).mkdir()
        (base / name / "ours.txt").write_text("ours", encoding="utf-8")
    real_k32, failed, changed = ob._kernel32(), [], []

    class FailTheSecondResponseOnce:
        def __getattr__(self, name):
            return getattr(real_k32, name)

        def GetFileInformationByHandleEx(self, handle, info_class, buffer, size):
            if info_class == 10 and not failed:                         # the first response (A and B) came back; the next one fails
                failed.append(handle)
                ctypes.set_last_error(32)                               # ERROR_SHARING_VIOLATION: transient
                return 0
            return real_k32.GetFileInformationByHandleEx(handle, info_class, buffer, size)

    proxy = FailTheSecondResponseOnce()
    real_time = ob.time

    def pause(seconds):                                                  # B, seen by the failed listing, is replaced by B'
        if failed and not changed:
            where = scratch_now(base)
            os.rename(where / "B", tmp_path / "B-aside")
            (where / "B").mkdir()
            (where / "B" / "precious.txt").write_text("not ours", encoding="utf-8")
            changed.append(where)

    monkeypatch.setattr(ob, "_kernel32", lambda: proxy)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert failed and changed, (failed, changed)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    survivors = list(root.rglob("precious.txt"))
    assert len(survivors) == 1 and survivors[0].read_text(encoding="utf-8") == "not ours", survivors
    assert (tmp_path / "B-aside" / "ours.txt").exists()


def test_r7_e2_a3_a_transient_failure_resolving_an_aliased_scratch_root_at_removal_is_not_a_refusal(tmp_path, monkeypatch):
    bus = make_bus(tmp_path)
    real_root = tmp_path / "scratch-real"
    real_root.mkdir()
    alias = tmp_path / "scratch-link"
    make_directory_link(alias, real_root)

    def git_ok(git_bin, args, cwd, clone):
        return subprocess.CompletedProcess(args, 0, "0123456789abcdef0123456789abcdef01234567\n", "")

    monkeypatch.setattr(ob, "run_isolated_git", git_ok)
    base, _clone, _tip = ob.make_exercise_scratch(bus, alias)
    real_real, unresolved = ob._real, []

    def realpath_denied_once(path, strict=False):                        # a sharing violation during the non-strict lookup:
        if not strict and not unresolved and os.path.normcase(os.fspath(path)) == os.path.normcase(str(alias)):
            unresolved.append(path)
            return os.path.abspath(path)                                 # realpath gives the alias back, unresolved
        return real_real(path, strict)

    monkeypatch.setattr(ob, "_real", realpath_denied_once)
    ob.remove_exercise_scratch(base, alias)
    assert list(real_root.iterdir()) == [], list(real_root.iterdir())

# Adversary pass r7 (head 4ebdeffd): A1 a listing that read any entry before it failed counts as the directory's first listing,
# so a name added before the retry is refused (fail-closed: a name the failed listing never reached is refused too); A2 the
# walk's keys are the exact names, never case-folded, so two names differing only in case on a case-sensitive directory stay two.

@pytest.mark.skipif(os.name != "nt", reason="the multi-response FileIdBothDirectoryInfo enumeration is the Windows listing")
def test_r7_4e_a1_a_name_added_after_a_listing_that_failed_part_way_is_refused(tmp_path, monkeypatch):
    import ctypes
    import types
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "A").mkdir()
    (base / "A" / "ours.txt").write_text("ours", encoding="utf-8")
    real_k32, failed, changed = ob._kernel32(), [], []

    class FailTheSecondResponseOnce:
        def __getattr__(self, name):
            return getattr(real_k32, name)

        def GetFileInformationByHandleEx(self, handle, info_class, buffer, size):
            if info_class == 10 and not failed:                         # the first response (A) came back; the next one fails
                failed.append(handle)
                ctypes.set_last_error(32)
                return 0
            return real_k32.GetFileInformationByHandleEx(handle, info_class, buffer, size)

    proxy = FailTheSecondResponseOnce()
    real_time = ob.time

    def pause(seconds):                                                  # B (holding precious.txt) is moved in before the retry
        if failed and not changed:
            where = scratch_now(base)
            (where / "B").mkdir()
            (where / "B" / "precious.txt").write_text("not ours", encoding="utf-8")
            changed.append(where)

    monkeypatch.setattr(ob, "_kernel32", lambda: proxy)
    monkeypatch.setattr(ob, "time", types.SimpleNamespace(monotonic=real_time.monotonic, sleep=pause))
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_PAUSE", 0)
    monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 0.5)
    with pytest.raises(ob.Unknown) as caught:
        ob.remove_exercise_scratch(base, root)
    assert failed and changed, (failed, changed)
    assert caught.value.code == "EXERCISE_SCRATCH_NOT_REMOVED", caught.value
    survivors = list(root.rglob("precious.txt"))
    assert len(survivors) == 1 and survivors[0].read_text(encoding="utf-8") == "not ours", survivors


def test_r7_4e_a2_two_listed_names_that_differ_only_in_case_are_two_entries(tmp_path, monkeypatch):
    """A case-sensitive directory (NTFS per-directory case sensitivity, or any POSIX volume) lists `A` and `a` as two objects
    with two identities in ONE listing. The listing is fed that literal input (an `a` beside the real `A`)."""
    root = tmp_path / "scratch"
    base = root / (ob.ROUTE_SCRATCH_PREFIX + "test")
    base.mkdir(parents=True)
    ob._pin_scratch(base, root)
    (base / "A").write_text("ours", encoding="utf-8")
    real_list, fed = ob._list_dir, []

    def with_a_case_twin(path, record=None):
        got = real_list(path, record)
        if any(os.path.basename(child) == "A" for child, _i, _d in got) and not fed:
            twin = os.path.join(path, "a")
            if record is not None:
                record(twin, 987654321)                                  # another object: another identity
            got.append((twin, 987654321, False))
            fed.append(twin)
        return got

    monkeypatch.setattr(ob, "_list_dir", with_a_case_twin)
    if os.name == "nt":                                                  # the twin is gone by the time it is deleted: already removed
        monkeypatch.setattr(ob, "ROUTE_SCRATCH_REMOVE_DEADLINE", 2)
    ob.remove_exercise_scratch(base, root)
    assert fed, "the case twin was never listed"
    assert list(root.iterdir()) == [], list(root.iterdir())


# Adversary pass r8 (head a18ce4a8): A1 (blocker) a delete or a rename that opens an entry by NAME follows a directory junction put
# on an INTERMEDIATE component after the walk verified it, so the handle's identity can match the listed one while the object is
# OUTSIDE the scratch (a hardlink to a file elsewhere); the handle's own final path must be the path asked for. A2 (major) a
# POSIX retry after a permission denial is bound to the listed entry's identity, not only to "still a plain entry".

def _outside_file_and_junction(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    precious = outside / "precious.txt"
    precious.write_text("not ours", encoding="utf-8")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(scratch / "D"), str(outside)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    return precious, scratch / "D"


@pytest.mark.skipif(os.name != "nt", reason="a directory junction on an intermediate component is a Windows thing")
def test_r8_a1_delete_by_name_never_deletes_through_a_junction_on_an_intermediate_component(tmp_path):
    precious, junction = _outside_file_and_junction(tmp_path)
    listed_id = os.lstat(precious).st_ino                                # the identity the walk listed under D\precious.txt
    with pytest.raises(ob._NotOurs):
        ob._delete_entry(str(junction / "precious.txt"), listed_id, False)
    assert precious.read_text(encoding="utf-8") == "not ours", "a file outside the scratch was deleted through a junction"


@pytest.mark.skipif(os.name != "nt", reason="a directory junction on an intermediate component is a Windows thing")
def test_r8_a1_sibling_rename_by_handle_never_moves_a_directory_reached_through_a_junction(tmp_path):
    root = tmp_path / "root"
    work = root / "work"
    work.mkdir(parents=True)
    link = tmp_path / "rootlink"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(root)], capture_output=True, text=True)
    assert made.returncode == 0, made.stdout + made.stderr
    ident = ob._file_id(work)
    with pytest.raises(ob._NotOurs):
        ob._rename_verified(link / "work", link / "work2", ident)
    assert work.exists() and not (root / "work2").exists(), "a directory was renamed through a junction on its parent"


@pytest.mark.parametrize("is_dir", [False, True])
def test_r8_a2_a_posix_retry_after_a_denial_never_acts_on_a_replacement_of_the_listed_entry(tmp_path, monkeypatch, is_dir):
    """The first unlink/rmdir is denied; before the retry the listed entry is moved aside and a stable ordinary replacement
    is put under the same name. The retry must not delete the replacement."""
    real_os = os

    class PosixOs:
        name = "posix"
        denied: list = []

        def __getattr__(self, item):
            return getattr(real_os, item)

        def _first_call_is_denied(self, real, path):
            if not self.denied:
                self.denied.append(path)
                real_os.rename(path, path + ".aside")                    # the listed entry moves away ...
                if is_dir:                                               # ... and a replacement takes the name
                    real_os.mkdir(path)
                else:
                    Path(path).write_text("not ours", encoding="utf-8")
                raise PermissionError(13, "denied", path)
            return real(path)

        def unlink(self, path):
            return self._first_call_is_denied(real_os.unlink, path)

        def rmdir(self, path):
            return self._first_call_is_denied(real_os.rmdir, path)

    entry = tmp_path / "entry"
    if is_dir:
        entry.mkdir()
    else:
        entry.write_text("ours", encoding="utf-8")
    listed = os.lstat(entry)
    monkeypatch.setattr(ob, "os", PosixOs())
    with pytest.raises((PermissionError, ob._NotOurs)):
        ob._delete_entry(str(entry), listed.st_ino, is_dir)
    assert entry.exists(), "the retry removed a replacement of the listed entry"
    assert PosixOs.denied, "the denial seam never ran"
    assert (tmp_path / "entry.aside").exists(), "the listed entry was not the one moved aside"


# Adversary pass r8 on 2592c3e4: A1 (blocker) `_real` stripped the extended-length prefix from `\\?\C:\bus.`, and Windows reads
# the bare `C:\bus.` as `C:\bus` (trailing dots and spaces are trimmed): the live bus. The prefix is kept whenever the bare name
# would mean another place. A2 (major) the POSIX put-aside rename replaces an existing destination silently; it now refuses one.

@pytest.mark.skipif(os.name != "nt", reason="trailing-dot trimming of a bare name is a Windows path rule")
@pytest.mark.parametrize("tail", [".", " "])
def test_r8b_a1_real_keeps_the_extended_prefix_when_the_bare_name_would_name_another_place(tmp_path, tail):
    dotted = "\\\\?\\" + str(tmp_path / ("bus" + tail))
    os.mkdir(dotted)
    try:
        real = ob._real(dotted)
        assert real.startswith("\\\\?\\"), f"the prefix was stripped: {real!r} now names {os.path.abspath(real)!r}"
    finally:
        os.rmdir(dotted)


@pytest.mark.skipif(os.name != "nt", reason="trailing-dot trimming of a bare name is a Windows path rule")
def test_r8b_a1_a_scratch_root_with_a_trailing_dot_never_makes_the_scratch_inside_the_live_bus(tmp_path, monkeypatch):
    bus = tmp_path / "bus"
    (bus / ".git").mkdir(parents=True)
    dotted_root = "\\\\?\\" + str(tmp_path / "bus.")
    made_in = []
    real_mkdtemp = ob.tempfile.mkdtemp

    def recording(*args, **kwargs):
        made_in.append(kwargs.get("dir"))
        return real_mkdtemp(*args, **kwargs)

    monkeypatch.setattr(ob.tempfile, "mkdtemp", recording)
    try:
        with pytest.raises(ob.Unknown) as caught:                        # refused before anything is created
            ob.make_exercise_scratch(bus, Path(dotted_root))
    finally:
        shutil.rmtree(dotted_root, ignore_errors=True)                   # the literal `bus.` directory, had one been made
    assert caught.value.code == "EXERCISE_SCRATCH_ROOT_AMBIGUOUS", caught.value
    assert not made_in, f"a scratch was made in {made_in!r}"
    assert os.listdir(bus) == [".git"], os.listdir(bus)


def test_r8b_a2_a_posix_put_aside_never_replaces_an_existing_destination(tmp_path, monkeypatch):
    real_os = os

    class PosixOs:
        name = "posix"

        def __getattr__(self, item):
            return getattr(real_os, item)

        def rename(self, src, dst):                                      # POSIX rename(2): an existing empty directory is replaced
            if real_os.path.isdir(dst):
                real_os.rmdir(dst)
            return real_os.rename(src, dst)

    work, other = tmp_path / "work", tmp_path / "work.rm-0000000000000000"
    work.mkdir()
    other.mkdir()
    keep = os.lstat(other)
    monkeypatch.setattr(ob, "os", PosixOs())
    with pytest.raises(OSError):
        ob._rename_verified(work, other, ob._file_id(work))
    assert work.exists() and os.lstat(other).st_ino == keep.st_ino, "the unrelated destination was replaced"