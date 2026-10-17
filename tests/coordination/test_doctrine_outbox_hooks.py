"""Tests for the hook commands of tools/coordination/doctrine_outbox.py: `check`, `pre-push`,
`stop`, `session-start`, `install-hooks`, and the `check-range` alias.

The tool is ONE file for the whole fleet and takes every project value from a config. These tests
configure it with the fleet bus reference's own paths (items under coordination/doctrine-outbox/ with
a sent/ directory, the three finding-path regexes, `master` as the ref, the bare trailer grammar), the
shape a project that adopts the shared tool for the bus's hooks runs. Every repository is a throwaway
under pytest's tmp_path; nothing touches the network or the real bus.

Named mutations (each turns the named test red, then is reverted):
  H1  pre-push ignores the remote ref filter           -> test_pre_push_ignores_a_push_to_another_branch
  H2  check() counts a just-committed item as stale    -> test_check_passes_a_fresh_item_and_refuses_a_stale_one
  H3  stop() ignores stop_hook_active                  -> test_stop_blocks_once_then_yields
"""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_SRC = REPO_ROOT / "tools" / "coordination" / "doctrine_outbox.py"
TOOL_REL = "coordination/tools/doctrine_outbox.py"
OUTBOX_DIR = "coordination/doctrine-outbox"

BUS_STYLE_CONFIG = {
    "schema": "doctrine-outbox-config.v1",
    "project": "fixture",
    "tool_rel": TOOL_REL,
    "default_ref": "master",
    "default_range": "origin/master..master",
    "outbox_dir": OUTBOX_DIR,
    "sent_dir": "sent",
    "state_dir": "local-state/outbox",
    "finding_path_regexes": [r"^coordination/tools/[^/]+\.py$", r"^coordination/harvest/[^/]+\.(py|ps1)$", r"^CLAUDE\.md$"],
    "trailer_mode": "bare",
    "pre_push_refs": ["refs/heads/master"],
}

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]
ZEROS = "0" * 40


def load_tool(config_path: Path):
    """A fresh module instance configured from `config_path`, so tests never share state."""
    spec = importlib.util.spec_from_file_location(f"doctrine_outbox_hooks_{time.monotonic_ns()}", TOOL_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure(mod.load_config(config_path), config_path)
    return mod


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], capture_output=True, text=True,
                            env={**os.environ, **env} if env else None)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def dated(seconds_ago: float) -> dict[str, str]:
    stamp = f"{int(time.time() - seconds_ago)} +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def item_text(target: str = "TRAPS.md", body: str = "### A finding\nWhat happened and the fix.\n") -> str:
    return f"---\ntarget: {target}\nkind: trap\nsource_commit: PENDING\nlaw4: attested\n---\n{body}"


def commit(repo: Path, files: dict[str, str], message: str, env: dict[str, str] | None = None) -> str:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        git(repo, "add", rel)
    git(repo, "commit", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _pin_runner_identity(monkeypatch):
    """gather_deny_terms() reads this machine's host and account name; pin them to values that occur
    nowhere in the fixture bodies so no test depends on the runner it lands on."""
    monkeypatch.setenv("COMPUTERNAME", "OUTBOXTESTHOST")
    monkeypatch.setenv("USERNAME", "outboxtestacct")
    monkeypatch.setenv("USER", "outboxtestacct")
    monkeypatch.setenv("USERPROFILE", "C:\\Users\\outboxtestacct")
    monkeypatch.setenv("HOME", "C:\\Users\\outboxtestacct")
    monkeypatch.delenv("DOCTRINE_OUTBOX_CONFIG", raising=False)


@pytest.fixture
def ob(tmp_path, monkeypatch):
    cfg = tmp_path / "bus-style.config.json"
    cfg.write_text(json.dumps(BUS_STYLE_CONFIG), encoding="utf-8")
    mod = load_tool(cfg)
    monkeypatch.setattr(mod.socket, "gethostname", lambda: "outboxtestrunner")
    return mod


# COST (CI-SHARD8-GUARDRAILS-COST-1): the project below took seven git processes to build and was rebuilt for
# every test that asks for `repo`. It is now built once per pytest session by `_build_repo` (body unchanged) and
# each test gets a private copy of that directory tree. The copy holds no remote, hence no absolute path.
_REPO_TEMPLATE = {}


@pytest.fixture
def repo(tmp_path, tmp_path_factory):
    """A fixture project on master: the tool at its configured path, the outbox README, one root commit."""
    if "dir" not in _REPO_TEMPLATE:
        template = tmp_path_factory.mktemp("hooks-repo-template")
        _build_repo(template)
        _REPO_TEMPLATE["dir"] = template
    r = tmp_path / "project"
    shutil.copytree(_REPO_TEMPLATE["dir"] / "project", r)
    return r


def _build_repo(tmp_path):
    r = tmp_path / "project"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(r)], check=True, capture_output=True, text=True)
    commit(r, {TOOL_REL: TOOL_SRC.read_text(encoding="utf-8"),
               "doctrine-outbox.config.json": json.dumps(BUS_STYLE_CONFIG),
               f"{OUTBOX_DIR}/README.md": "# outbox\n"},
           "init\n\nDoctrine-Export: none\n")
    return r


@pytest.fixture
def pushed_repo(repo, tmp_path):
    """`repo` plus a bare origin that already holds master, so `origin/master..master` resolves."""
    origin = tmp_path / "stop-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")
    return repo


def push_line(local: str, remote: str = ZEROS, remote_ref: str = "refs/heads/master") -> str:
    return f"refs/heads/master {local} {remote_ref} {remote}\n"


def run_pre_push(ob, repo: Path, monkeypatch, stdin_text: str) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    return ob.main(["pre-push", str(repo), "origin", "https://example.invalid/x.git"])


# ---------- the tool carries no project value ----------

CANDIDATE_BUS_PATH = "tools/factory/doctrine_outbox.py"
HANDOFF_CARD = REPO_ROOT / "doctrine-outbox" / "20261016-doctrine-outbox-single-source-superset.md"
OUTBOX_README = REPO_ROOT / "doctrine-outbox" / "README.md"


def _bus_candidate() -> dict:
    return json.loads((REPO_ROOT / "doctrine-outbox.config.json").read_text(encoding="utf-8"))["bus_candidate"]


def test_candidate_matches_the_recorded_digest():
    """This file is the candidate for the bus's tools/factory/doctrine_outbox.py; `bus_candidate.sha256_lf`
    in the repo-root config records the digest of its LF bytes, so a local edit (or a CRLF checkout)
    cannot drift from what the steward is asked to land."""
    lf = TOOL_SRC.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(lf).hexdigest() == _bus_candidate()["sha256_lf"]


def test_the_steward_handoff_card_names_the_candidate_digest():
    """The card that asks the bus steward to adopt this file quotes its digest; it must not go stale."""
    assert _bus_candidate()["sha256_lf"] in HANDOFF_CARD.read_text(encoding="utf-8")


PIN_AWARE_FOLLOW_UP = "OUTBOX-PIN-AWARE-TOOL-AT-REF-1"


def test_surfaces_keep_the_local_copy_and_name_the_pin_aware_follow_up():
    """The installed hook runs `$REPO_ROOT/<tool_rel>` and debt/drain need `tool_rel` at the ref, so a
    project that deletes its copy after pinning bricks its own pushes (CoS repro on a4480947). Every
    surface that describes the route must say the copy stays until the pin-aware release, and name it."""
    assert _bus_candidate()["bus_path"] == CANDIDATE_BUS_PATH
    surfaces = {
        "config": (REPO_ROOT / "doctrine-outbox.config.json").read_text(encoding="utf-8"),
        "card": HANDOFF_CARD.read_text(encoding="utf-8"),
        "readme": OUTBOX_README.read_text(encoding="utf-8"),
        "tool docstring": ast.get_docstring(ast.parse(TOOL_SRC.read_text(encoding="utf-8"))) or "",
    }
    for name, text in surfaces.items():
        assert "factory-pins" in text and CANDIDATE_BUS_PATH in text, f"{name} does not name the bus pin route"
        assert PIN_AWARE_FOLLOW_UP in text, f"{name} does not name the pin-aware follow-up"
        assert "conjugal-reference" not in text, f"{name} still names the deleted reference directory"
        assert not re.search(r"\bvendor(s|ed|ing)?\b|re-vendor", text, re.IGNORECASE), f"{name} still tells a project to vendor a copy"
        # r5's pattern flagged the card's own "Only after it ships does a project delete its copy". A sentence that
        # makes the deletion conditional on the pin-aware release is the rule, not a violation of it.
        unconditional = [s for s in re.split(r"(?<=[.!?])\s+|\n", text)
                         if re.search(r"deletes? (its|their|the) (own )?(local )?copy|keeping no copy|keeps no copy", s, re.IGNORECASE)
                         and not re.search(r"\bonly after\b|\buntil\b|\bonce\b", s, re.IGNORECASE)]
        assert not unconditional, f"{name} still tells a project to delete its copy: {unconditional}"


# ---------- the card's route, run end to end against the bus's own pin-runner ----------

# The bus clone, when there is one, comes only from the FACTORY_BUS_CLONE environment variable, read directly below; there is no default path.
RUNNER_BUS_PATH = "tools/factory/pin-runner.py"
# r7 (CoS r6 improvement 2): hosted CI has no bus clone, so this route test used to skip there and never ran
# where it matters. The bus runner now travels with the tests as DATA (a .py.txt, outside the python census),
# pinned by bus SHA and sha256 in the sidecar. FACTORY_BUS_CLONE, when set, is checked AGAINST the fixture.
RUNNER_FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
RUNNER_FIXTURE_META = RUNNER_FIXTURE_DIR / "pin-runner.bus-49009d20.json"


def _runner_fixture() -> bytes:
    """The committed copy of the bus runner (LF bytes), proven against the sidecar's recorded digest. A missing
    or altered fixture FAILS: this test has no skip, so CI can never pass by not running it."""
    meta = json.loads(RUNNER_FIXTURE_META.read_text(encoding="utf-8"))
    assert meta["bus_path"] == RUNNER_BUS_PATH and re.fullmatch(r"[0-9a-f]{40}", meta["bus_sha"])
    data = (RUNNER_FIXTURE_DIR / f"pin-runner.bus-{meta['bus_sha'][:8]}.py.txt").read_bytes().replace(b"\r\n", b"\n")
    assert len(data) == meta["bytes"], f"fixture is {len(data)} bytes, the sidecar records {meta['bytes']}"
    assert hashlib.sha256(data).hexdigest() == meta["sha256_lf"], "the pin-runner fixture differs from its recorded sha256"
    assert hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == meta["git_blob_id"], "fixture is not the recorded git blob"
    return data


def _live_bus_runner(clone: str) -> bytes | None:
    """The bytes of the clone's master pin-runner, read with `git cat-file` (never a write into the bus clone);
    None when the clone cannot supply them."""
    if not clone or not (Path(clone) / ".git").exists():
        return None
    for ref in ("origin/master", "master"):
        proc = subprocess.run(["git", "--no-optional-locks", "-C", clone, "cat-file", "blob", f"{ref}:{RUNNER_BUS_PATH}"],
                              capture_output=True, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.replace(b"\r\n", b"\n")
    return None


def _read_only_bus_runner() -> bytes:
    """The pin-runner the route test runs: always the committed fixture. With FACTORY_BUS_CLONE set, the live
    clone's blob must equal the fixture; a bus that moved on fails here, naming the refresh, rather than letting
    the fixture rot into a test of bytes the bus no longer ships."""
    fixture = _runner_fixture()
    clone = os.environ.get("FACTORY_BUS_CLONE", "")
    if clone:
        live = _live_bus_runner(clone)
        assert live is not None, f"FACTORY_BUS_CLONE={clone!r} holds no {RUNNER_BUS_PATH} on origin/master or master"
        assert live == fixture, (f"the bus clone's {RUNNER_BUS_PATH} differs from {RUNNER_FIXTURE_META.name}'s fixture; "
                                 "review the new blob, then replace the .py.txt and the sidecar values")
    return fixture


def test_runner_fixture_is_proven_and_the_route_test_has_no_skip_path(tmp_path, monkeypatch):
    """CoS r6 improvement 2: under CI (CI set, no bus clone anywhere) the route test must run on the fixture; a
    drifted clone must fail rather than skip; and the route test must not contain a skip."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.delenv("FACTORY_BUS_CLONE", raising=False)
    fixture = _read_only_bus_runner()
    assert fixture.startswith(b"#!/usr/bin/env python3") and b"def cmd_install" in fixture
    assert json.loads(RUNNER_FIXTURE_META.read_text(encoding="utf-8"))["bus_sha"][:8] in RUNNER_FIXTURE_META.name
    src = Path(__file__).read_text(encoding="utf-8")
    for fn in ("_runner_fixture", "_read_only_bus_runner", "test_the_steward_cards_route_runs_end_to_end_against_the_bus_pin_runner"):
        body = src[src.index(f"def {fn}("):]
        body = body[:body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
        assert "pytest.skip" not in body and "skipif" not in body, f"{fn} can skip"
    route_def = "def test_the_steward_cards_route_runs_end_to_end_against_the_bus_pin_runner("
    decorators = src[src.index("\n" + route_def) - 160:src.index("\n" + route_def)]   # a marker above the def would be a skip too
    assert "mark.skip" not in decorators and "mark.xfail" not in decorators
    # A clone whose blob equals the fixture passes; a clone whose blob drifted fails loudly.
    bus = tmp_path / "bus"
    (bus / "tools" / "factory").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "master", str(bus)], check=True, capture_output=True, text=True)
    (bus / RUNNER_BUS_PATH).write_bytes(fixture)
    git(bus, "add", "-A")
    git(bus, "commit", "-m", "same runner")
    monkeypatch.setenv("FACTORY_BUS_CLONE", str(bus))
    assert _read_only_bus_runner() == fixture
    (bus / RUNNER_BUS_PATH).write_bytes(fixture + b"# drift\n")
    git(bus, "commit", "-am", "bus moved on")
    with pytest.raises(AssertionError, match="differs from"):
        _read_only_bus_runner()


def _card_command(fragment: str) -> str:
    """The backticked `python factory-pins/pin-runner.py ...` command in the steward card that carries `fragment`."""
    text = HANDOFF_CARD.read_text(encoding="utf-8")
    found = re.search(r"`(python factory-pins/pin-runner\.py " + re.escape(fragment) + r"[^`]*)`", text)
    assert found, f"the steward card has no `python factory-pins/pin-runner.py {fragment} ...` command"
    return found.group(1)


def _argv(command: str, **fill: str) -> list[str]:
    argv = shlex.split(command)
    assert argv[0] == "python"
    return [sys.executable, *[fill.get(a, a) for a in argv[1:]]]


def test_the_steward_cards_route_runs_end_to_end_against_the_bus_pin_runner(tmp_path):
    """The first instance of ruling candidate steward-handoff-card-route-is-live-and-exercised-r1: a
    steward card ships only with a test that RUNS its route against the bus's current share model.

    A scratch bus holds bus master's real tools/factory/pin-runner.py and this tool at the bus path the
    card names. A scratch consumer follows the card's own commands: adopt the runner, pin the tool with
    --reviewer/--reason, run it by pin with --config (install-hooks, then session-start), and then pushes
    through the hook that run installed. The consumer KEEPS its local copy, as the card says: the hook and
    the debt checks read it. Never writes into the real bus repository."""
    runner = _read_only_bus_runner()
    pin_cmd, run_cmd = _card_command("pin doctrine-outbox"), _card_command("run doctrine-outbox")
    assert "--reviewer" in pin_cmd and "--reason" in pin_cmd, "the card's pin command is refused by the runner without these"
    assert "--config" in run_cmd, "a pinned run executes from a temp directory and cannot find the config without --config"

    bus = tmp_path / "scratch-bus"
    bus.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(bus)], check=True, capture_output=True, text=True)
    (bus / "tools" / "factory").mkdir(parents=True)
    (bus / RUNNER_BUS_PATH).write_bytes(runner)
    (bus / CANDIDATE_BUS_PATH).write_bytes(TOOL_SRC.read_bytes().replace(b"\r\n", b"\n"))
    git(bus, "add", "-A")
    git(bus, "commit", "-m", "scratch bus: the runner at bus master plus the candidate")
    bus_sha = git(bus, "rev-parse", "HEAD")

    consumer = tmp_path / "consumer"
    consumer.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(consumer)], check=True, capture_output=True, text=True)
    config = {**BUS_STYLE_CONFIG, "project": "consumer", "tool_rel": "tools/coordination/doctrine_outbox.py",
              "state_dir": ".state/outbox", "finding_path_regexes": [r"^agents/[^/]+\.md$"]}
    commit(consumer, {"doctrine-outbox.config.json": json.dumps(config),
                      "tools/coordination/doctrine_outbox.py": TOOL_SRC.read_text(encoding="utf-8"),
                      f"{OUTBOX_DIR}/README.md": "# outbox\n"},
           "init\n\nDoctrine-Export: none\n")
    (consumer / "factory-pins").mkdir()
    (consumer / "factory-pins" / "pin-runner.py").write_bytes(runner)
    commit(consumer, {"docs/readme.md": "x\n"}, "docs\n\nDoctrine-Export: none\n")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True, text=True)
    git(consumer, "remote", "add", "origin", str(origin))
    git(consumer, "push", "-q", "origin", "master")   # before the guard exists

    env = {**os.environ, "FACTORY_BUS_CLONE": str(bus), "FACTORY_PINS_CACHE": str(tmp_path / "blob-cache")}

    def sh(argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(argv, cwd=consumer, capture_output=True, text=True, env=env)

    adopt = sh([sys.executable, "factory-pins/pin-runner.py", "adopt-runner", "--commit", bus_sha])
    assert adopt.returncode == 0, adopt.stderr
    pin = sh(_argv(pin_cmd, **{"<bus-sha>": bus_sha, "<who>": "r5-e2e", "<why>": "route test"}))
    assert pin.returncode == 0, pin.stderr
    pins = json.loads((consumer / "factory-pins" / "factory-pins.json").read_text(encoding="utf-8"))
    assert pins["tools"]["doctrine-outbox"]["path"] == CANDIDATE_BUS_PATH and pins["tools"]["doctrine-outbox"]["commit"] == bus_sha

    def by_pin(*command: str) -> subprocess.CompletedProcess:
        return sh(_argv(run_cmd, **{"<command>": command[0]}) + list(command[1:]))

    # Without --config the pinned bytes cannot find the project's values: the reason the card must carry it.
    bare = sh([sys.executable, "factory-pins/pin-runner.py", "run", "doctrine-outbox", "--", "check-commits"])
    assert bare.returncode == 2 and "CONFIG_ABSENT" in bare.stderr, (bare.returncode, bare.stderr)

    hooked = by_pin("install-hooks")
    assert hooked.returncode == 0 and hooked.stdout.strip() == "installed", (hooked.stdout, hooked.stderr)
    started = by_pin("session-start")
    assert started.returncode == 0, started.stderr
    assert "TOOL_ABSENT_AT_REF" not in started.stdout and "UNKNOWN" not in started.stdout, started.stdout
    assert "pre-push guard: present" in started.stdout and "no unsent doctrine items" in started.stdout, started.stdout

    # The hook the pinned run installed guards a real push. A clean push passes (the guard ran and said yes)...
    commit(consumer, {"docs/notes.md": "prose\n"}, "docs only")
    clean = sh(["git", "push", "origin", "master"])
    assert clean.returncode == 0, clean.stderr
    # ...and the same hook refuses an undeclared finding-path commit, which proves it ran at all.
    commit(consumer, {"agents/new.md": "rule\n"}, "undeclared finding path")
    refused = sh(["git", "push", "origin", "master"])
    assert refused.returncode != 0 and "PUSH REFUSED" in refused.stderr and "agents/new.md" in refused.stderr, refused.stderr
    git(consumer, "commit", "--amend", "-m", "declared\n\nDoctrine-Export: none\n")
    allowed = sh(["git", "push", "origin", "master"])
    assert allowed.returncode == 0, allowed.stderr
    assert (consumer / "tools" / "coordination" / "doctrine_outbox.py").is_file()   # the copy the card says to keep


def test_the_tool_names_no_project_host_or_state_directory():
    text = TOOL_SRC.read_text(encoding="utf-8").lower()
    needles = ("mlv", "bachelor", ".claude-state", "ultra", "magnus", "virtual-ten", "obabalola", "layi")
    assert len(needles) == 8   # the PR body says the grep covers eight; keep the two in step
    for needle in needles:
        assert needle not in text, f"{needle!r} is a project value; it belongs in the config"


def test_config_must_name_a_state_location(tmp_path):
    bad = {k: v for k, v in BUS_STYLE_CONFIG.items() if k != "state_dir"}
    path = tmp_path / "c.json"
    path.write_text(json.dumps(bad), encoding="utf-8")
    spec =importlib.util.spec_from_file_location("doctrine_outbox_nostate", TOOL_SRC)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    with pytest.raises(fresh.Refusal) as excinfo:
        fresh.configure(fresh.load_config(path), path)
    assert excinfo.value.code == "CONFIG_INVALID" and "state_dir" in excinfo.value.detail


def test_config_refuses_an_unknown_key_and_a_bad_trailer_mode(tmp_path):
    spec = importlib.util.spec_from_file_location("doctrine_outbox_badcfg", TOOL_SRC)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    typo = tmp_path / "typo.json"
    typo.write_text(json.dumps({**BUS_STYLE_CONFIG, "law4_extras": []}), encoding="utf-8")
    with pytest.raises(fresh.Refusal) as excinfo:
        fresh.load_config(typo)
    assert "law4_extras" in excinfo.value.detail
    with pytest.raises(fresh.Refusal) as excinfo:
        fresh.configure({**BUS_STYLE_CONFIG, "trailer_mode": "lenient"})
    assert excinfo.value.code == "CONFIG_INVALID"


@pytest.mark.parametrize("override, offending", [
    ({"outbox_dir": "../outside"}, "outbox_dir"),
    ({"outbox_dir": "doctrine-outbox/../.."}, "outbox_dir"),
    ({"outbox_dir": "./doctrine-outbox"}, "outbox_dir"),
    ({"sent_dir": ".."}, "sent_dir"),
    ({"law4_extra": [{"name": "x", "pattern": "y", "ignorecas": True}]}, "ignorecas"),
    ({"law4_extra": [{"name": "x", "pattern": "y", "ignorecase": "yes"}]}, "ignorecase"),
])
def test_config_refuses_a_dot_dot_directory_and_a_stray_law4_row_key(override, offending):
    """Iverson r1 improvements 1 and 2 (r1 :302, :274; CoS 6225387980): `..` matches the directory
    character class, and a misspelt row key silently turned a case-insensitive deny term case-sensitive."""
    spec = importlib.util.spec_from_file_location("doctrine_outbox_dotdot", TOOL_SRC)
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    with pytest.raises(fresh.Refusal) as excinfo:
        fresh.configure({**BUS_STYLE_CONFIG, **override})
    assert excinfo.value.code == "CONFIG_INVALID" and offending in excinfo.value.detail
    fresh.configure({**BUS_STYLE_CONFIG, "law4_extra": [{"name": "x", "pattern": "y", "ignorecase": True, "_note": "ok"}]})


def test_no_config_is_unknown_not_a_pass(tmp_path):
    lone = tmp_path / "lone"
    lone.mkdir()
    copy = lone / "doctrine_outbox.py"
    copy.write_bytes(TOOL_SRC.read_bytes())
    result = subprocess.run([sys.executable, "-I", str(copy), "check"], capture_output=True, text=True, cwd=lone)
    assert result.returncode == 2 and "CONFIG_ABSENT" in result.stderr


@pytest.mark.parametrize("config_text, offending", [
    (json.dumps({**BUS_STYLE_CONFIG, "law4_extras": []}), "law4_extras"),
    ("{not json", "JSONDecodeError"),
])
def test_a_broken_config_is_config_invalid_not_config_absent(tmp_path, config_text, offending):
    """A config that is there but wrong must not read as a missing one: the operator would go
    looking for a file that exists. The refusal names the offending key (or the parse error)."""
    lone = tmp_path / "lone"
    lone.mkdir()
    copy = lone / "doctrine_outbox.py"
    copy.write_bytes(TOOL_SRC.read_bytes())
    (lone / "doctrine-outbox.config.json").write_text(config_text, encoding="utf-8")
    result = subprocess.run([sys.executable, "-I", str(copy), "check"], capture_output=True, text=True, cwd=lone)
    assert result.returncode == 1, result.stderr
    assert "CONFIG_INVALID" in result.stderr and offending in result.stderr
    assert "CONFIG_ABSENT" not in result.stderr


def test_check_range_is_an_alias_with_the_configured_default_range(ob):
    parser = ob.build_parser()
    assert parser.parse_args(["check-range"]).rev_range == "origin/master..master"
    assert parser.parse_args(["check-commits"]).rev_range == "origin/master..master"


# ---------- install-hooks / session-start ----------

def test_install_hooks_is_idempotent(ob, repo, capsys):
    assert ob.main(["install-hooks", str(repo)]) == 0
    assert capsys.readouterr().out.strip() == "installed"
    hook = repo / ".git" / "hooks" / "pre-push"
    first = hook.read_text(encoding="utf-8")
    assert "# doctrine-outbox-guard" in first and TOOL_REL in first and "pre-push" in first
    assert ob.main(["install-hooks", str(repo)]) == 0
    assert capsys.readouterr().out.strip() == "present"
    assert hook.read_text(encoding="utf-8") == first


def test_install_hooks_puts_the_guard_ahead_of_an_lfs_layer(ob, repo):
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nset -e\nREPO_ROOT=$(git rev-parse --show-toplevel)\ncommand -v git-lfs >/dev/null || exit 2\ngit lfs pre-push \"$@\"\n",
                    encoding="utf-8", newline="\n")
    assert ob.install_pre_push(repo) == "installed"
    text = hook.read_text(encoding="utf-8")
    assert text.index("doctrine-outbox-guard") < text.index("git lfs pre-push")


def test_install_hooks_sets_the_executable_bit(ob, repo, monkeypatch):
    """git skips a pre-push hook that is not executable, silently, on POSIX. The mode cannot be read
    back on Windows, so this pins the code path: install must chmod the hook with execute bits."""
    calls: list[tuple[Path, int]] = []
    real_chmod = os.chmod

    def recording_chmod(path, mode, *args, **kwargs):
        calls.append((Path(path), mode))
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(os, "chmod", recording_chmod)
    ob.install_pre_push(repo)
    hook = repo / ".git" / "hooks" / "pre-push"
    assert any(p == hook and mode & 0o100 for p, mode in calls), f"no chmod with the owner execute bit on {hook}: {calls}"


@pytest.mark.skipif(os.name != "posix", reason="the executable bit is a POSIX file mode")
def test_installed_hook_is_executable_when_new_existing_and_already_guarded(ob, repo):
    hook = repo / ".git" / "hooks" / "pre-push"
    assert ob.install_pre_push(repo) == "installed"
    assert os.access(hook, os.X_OK)
    hook.chmod(0o644)   # what an install made before this fix left behind
    assert ob.install_pre_push(repo) == "present"
    assert os.access(hook, os.X_OK)
    hook.unlink()
    hook.write_text("#!/bin/sh\n", encoding="utf-8", newline="\n")
    hook.chmod(0o644)   # someone else's non-executable hook that we splice into
    assert ob.install_pre_push(repo) == "installed"
    assert os.access(hook, os.X_OK)


def test_installed_guard_hands_the_downstream_layer_the_bytes_git_sent(ob, repo, tmp_path):
    """git lfs pre-push reads the ref lines from stdin. The guard sits ahead of it and its python
    reads that stdin to the end, so without a restore the downstream layer gets EOF and LFS uploads
    nothing. A stand-in reader after the guard must receive exactly what git sent, and must not run
    at all when the guard refuses."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")
    base = git(repo, "rev-parse", "HEAD")
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text('#!/bin/sh\nset -e\nREPO_ROOT=$(git rev-parse --show-toplevel)\n'
                    'cat > "$DOWNSTREAM_OUT"   # git lfs pre-push stand-in\n', encoding="utf-8", newline="\n")
    assert ob.main(["install-hooks", str(repo)]) == 0
    text = hook.read_text(encoding="utf-8")
    assert text.index("doctrine-outbox-guard") < text.index("git lfs pre-push stand-in")
    seen = tmp_path / "downstream.bin"
    env = {**os.environ, "DOWNSTREAM_OUT": str(seen)}
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "undeclared")
    refused = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True, env=env)
    assert refused.returncode != 0 and "PUSH REFUSED" in refused.stderr
    assert not seen.exists(), "the downstream layer ran although the guard refused the push"
    git(repo, "commit", "--amend", "-m", "declared\n\nDoctrine-Export: none\n")
    head = git(repo, "rev-parse", "HEAD")
    allowed = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True, env=env)
    assert allowed.returncode == 0, allowed.stderr
    assert seen.read_bytes() == f"refs/heads/master {head} refs/heads/master {base}\n".encode()


def _guard_block(*, spool: bool, python_missing_passes: bool) -> str:
    """The guard snippet as an older release wrote it (no end marker): r1/r2 had no stdin spool and let a
    push through when python was missing; r3/r4 spooled stdin but still let that push through."""
    run = ('  "$OUTBOX_PY" "$REPO_ROOT/' + TOOL_REL + '" pre-push "$REPO_ROOT" "$@"'
           + (' < "$OUTBOX_STDIN" || { OUTBOX_RC=$?; rm -f "$OUTBOX_STDIN"; exit $OUTBOX_RC; }\n' if spool else ' || exit $?\n'))
    assert python_missing_passes
    return ("# doctrine-outbox-guard: findings reach the bus by file; a finding-path commit must declare.\n"
            'REPO_ROOT=${REPO_ROOT:-$(git rev-parse --show-toplevel)}\nOUTBOX_PY=\nfor cand in python3 python; do\n'
            '  if command -v "$cand" >/dev/null 2>&1 && "$cand" --version >/dev/null 2>&1; then OUTBOX_PY=$cand; break; fi\ndone\n'
            + ('OUTBOX_STDIN=$(mktemp 2>/dev/null) || OUTBOX_STDIN="${TMPDIR:-/tmp}/outbox-pre-push.$$"\ncat > "$OUTBOX_STDIN"\n' if spool else "")
            + 'if [ -n "$OUTBOX_PY" ]; then\n' + run + 'else\n'
            '  echo "doctrine-outbox: no python on PATH; the push guard did not run" >&2\nfi\n'
            + ('exec < "$OUTBOX_STDIN"\nrm -f "$OUTBOX_STDIN" 2>/dev/null || true\n' if spool else ""))


@pytest.mark.parametrize("spool", [False, True], ids=["r1-r2-guard", "r3-r4-guard"])
def test_install_hooks_replaces_an_older_guard_in_place_and_repairs_the_stdin_hand_off(ob, repo, tmp_path, spool):
    """r3 improvement 1 (CoS 6224591286, repeated 6225387980): a guard written before r3 swallows git's ref
    lines, and once install-hooks set its executable bit it started running. install-hooks must replace the
    old block (and only it) so the layer after the guard reads exactly what git sent."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")
    base = git(repo, "rev-parse", "HEAD")
    hook = repo / ".git" / "hooks" / "pre-push"
    before = "#!/bin/sh\nset -e\nREPO_ROOT=$(git rev-parse --show-toplevel)\n"
    after = 'cat > "$DOWNSTREAM_OUT"   # git lfs pre-push stand-in\n'
    hook.write_text(before + "\n" + _guard_block(spool=spool, python_missing_passes=True) + after, encoding="utf-8", newline="\n")
    assert ob.main(["install-hooks", str(repo)]) == 0
    text = hook.read_text(encoding="utf-8")
    assert text.count("# doctrine-outbox-guard:") == 1 and text.count("# end doctrine-outbox-guard") == 1
    # r7: the guard moves to the splice point (right after the shebang and `set -e`), so the REPO_ROOT line that
    # used to precede it now follows it. Every line around the guard is still there, in the same order.
    assert text.startswith("#!/bin/sh\nset -e\n") and text.endswith(after), "the lines around the guard were disturbed"
    assert text.index("# end doctrine-outbox-guard") < text.index("REPO_ROOT=$(git rev-parse --show-toplevel)\n\n" + after[:10])
    assert "the push guard did not run" not in text and "exec < \"$OUTBOX_STDIN\"" in text
    assert ob.install_pre_push(repo) == "present"
    seen = tmp_path / "downstream.bin"
    head = commit(repo, {"docs/notes.md": "prose\n"}, "docs only")
    pushed = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True,
                            env={**os.environ, "DOWNSTREAM_OUT": str(seen)})
    assert pushed.returncode == 0, pushed.stderr
    assert seen.read_bytes() == f"refs/heads/master {head} refs/heads/master {base}\n".encode()


def test_install_hooks_reports_repaired_and_refuses_a_block_it_cannot_place(ob, repo, capsys):
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n\n" + _guard_block(spool=True, python_missing_passes=True), encoding="utf-8", newline="\n")
    assert ob.main(["install-hooks", str(repo)]) == 0
    assert capsys.readouterr().out.strip() == "repaired"
    odd = "#!/bin/sh\n# doctrine-outbox-guard: hand edited\nexit 0\n"
    hook.write_text(odd, encoding="utf-8", newline="\n")
    assert ob.main(["install-hooks", str(repo)]) == 1
    assert "HOOK_UNRECOGNISED" in capsys.readouterr().err
    assert hook.read_text(encoding="utf-8") == odd, "a block that could not be placed must not be rewritten"
    assert ob.main(["session-start", str(repo)]) == 0   # informational: never blocks a session start
    assert "pre-push guard: UNKNOWN" in capsys.readouterr().out


def test_installed_hook_refuses_the_push_when_no_python_is_on_path(ob, repo, tmp_path):
    """Iverson r1 improvement 3 (r1 :1799, CoS 6225387980): a host with no usable python used to print a
    warning and let the push through, so the guard failed open. A stub python that fails `--version`, first
    on PATH, stands in for a box without one."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")
    assert ob.main(["install-hooks", str(repo)]) == 0
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    for name in ("python3", "python"):
        (stubs / name).write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
        (stubs / name).chmod(0o755)
    commit(repo, {"docs/notes.md": "prose\n"}, "docs only")
    env = {**os.environ, "PATH": str(stubs) + os.pathsep + os.environ["PATH"]}
    refused = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True, env=env)
    assert refused.returncode != 0 and "no python on PATH" in refused.stderr and "push refused" in refused.stderr, refused.stderr
    assert git(repo, "rev-parse", "origin/master") != git(repo, "rev-parse", "HEAD"), "the push went through without the guard"
    allowed = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True)
    assert allowed.returncode == 0, allowed.stderr   # the same hook with a working python passes the clean push


def test_install_hooks_works_from_a_linked_worktree(ob, repo, tmp_path):
    """In a linked worktree `.git` is a file and hooks live in the common dir."""
    linked = tmp_path / "linked"
    git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    assert ob.main(["install-hooks", str(linked)]) == 0
    assert (repo / ".git" / "hooks" / "pre-push").is_file()


def test_session_start_installs_the_guard_reports_debt_and_exits_zero(ob, repo, capsys):
    commit(repo, {f"{OUTBOX_DIR}/20261016-open-finding.md": item_text()}, "file a finding\n\nDoctrine-Export: outbox\n")
    assert ob.main(["session-start", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "pre-push guard: installed" in out
    assert "1 unsent doctrine item(s)" in out and "20261016-open-finding.md" in out


def test_session_start_never_blocks_when_the_ref_lacks_the_tool(ob, tmp_path, capsys):
    bare_repo = tmp_path / "no-tool"
    bare_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(bare_repo)], check=True, capture_output=True, text=True)
    commit(bare_repo, {"README.md": "x\n"}, "init")
    assert ob.main(["session-start", str(bare_repo)]) == 0
    out = capsys.readouterr().out
    assert "UNKNOWN" in out and "TOOL_ABSENT_AT_REF" in out


# ---------- check ----------

def test_check_passes_a_fresh_item_and_refuses_a_stale_one(ob, repo, capsys):
    commit(repo, {f"{OUTBOX_DIR}/20261016-fresh-finding.md": item_text()}, "fresh\n\nDoctrine-Export: outbox\n", env=dated(60))
    assert ob.main(["check", str(repo), "60"]) == 0
    capsys.readouterr()
    commit(repo, {f"{OUTBOX_DIR}/20261015-old-finding.md": item_text(body="### An older finding\nStill unsent.\n")},
           "old\n\nDoctrine-Export: outbox\n", env=dated(3 * 3600))
    assert ob.main(["check", str(repo), "60"]) == 1
    out = capsys.readouterr().out
    assert "REFUSED" in out and "20261015-old-finding.md" in out


def test_check_ignores_items_already_moved_to_the_sent_directory(ob, repo, capsys):
    commit(repo, {f"{OUTBOX_DIR}/sent/20261001-done-finding.md": item_text()}, "moved to sent\n\nDoctrine-Export: none\n", env=dated(5 * 3600))
    assert ob.main(["check", str(repo), "60"]) == 0
    assert "no unsent doctrine items" in capsys.readouterr().out


def test_check_is_unknown_when_the_ref_lacks_the_outbox(ob, tmp_path, capsys):
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(other)], check=True, capture_output=True, text=True)
    commit(other, {"README.md": "x\n"}, "init")
    assert ob.main(["check", str(other)]) == 2
    assert "UNKNOWN" in capsys.readouterr().out


# ---------- pre-push ----------

def test_pre_push_refuses_a_finding_path_commit_without_a_trailer(ob, repo, monkeypatch, capsys):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "touch a tool, say nothing")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, base)) == 1
    err = capsys.readouterr().err
    assert "PUSH REFUSED" in err and "coordination/tools/other.py" in err


def test_pre_push_allows_an_explicit_none(ob, repo, monkeypatch):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "touch a tool\n\nDoctrine-Export: none\n")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, base)) == 0


def test_pre_push_ignores_a_commit_outside_the_finding_paths(ob, repo, monkeypatch):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"docs/notes.md": "prose\n"}, "docs only")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, base)) == 0


def test_pre_push_ignores_a_push_to_another_branch(ob, repo, monkeypatch):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "untrailered, but not headed for master")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, base, remote_ref="refs/heads/feature")) == 0


def test_pre_push_ignores_a_branch_deletion(ob, repo, monkeypatch):
    assert run_pre_push(ob, repo, monkeypatch, push_line(ZEROS, git(repo, "rev-parse", "HEAD"))) == 0


def test_pre_push_judges_the_whole_history_of_a_new_remote_branch(ob, repo, monkeypatch, capsys):
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "history without a trailer")
    head = commit(repo, {"docs/notes.md": "prose\n"}, "later and clean")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head)) == 1
    assert "PUSH REFUSED" in capsys.readouterr().err


def test_pre_push_refuses_a_range_it_cannot_resolve(ob, repo, monkeypatch, capsys):
    head = git(repo, "rev-parse", "HEAD")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, "1" * 40)) == 1
    assert "cannot check" in capsys.readouterr().err


def test_bare_mode_refuses_a_trailer_that_is_not_exactly_none_or_outbox(ob, repo, monkeypatch, capsys):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "touch\n\nDoctrine-Export: none because nothing to say\n")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, base)) == 1
    assert "is not one of" in capsys.readouterr().err


def test_bare_outbox_trailer_needs_an_item_in_the_same_commit(ob, repo, monkeypatch, capsys):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "declares but files nothing\n\nDoctrine-Export: outbox\n")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, base)) == 1
    assert "declared-but-not-filed" in capsys.readouterr().err


def test_bare_outbox_trailer_with_a_valid_item_passes(ob, repo, monkeypatch):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {"coordination/tools/other.py": "print(1)\n", f"{OUTBOX_DIR}/20261016-the-fix.md": item_text()},
                  "fix and file it\n\nDoctrine-Export: outbox\n")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, base)) == 0


def test_bare_outbox_item_that_breaks_law4_is_refused_by_name(ob, repo, monkeypatch, capsys):
    base = git(repo, "rev-parse", "HEAD")
    head = commit(repo, {f"{OUTBOX_DIR}/20261016-leaky-item.md": item_text(body="### Leak\nsee coordination/lanes/x for the wire\n")},
                  "file it\n\nDoctrine-Export: outbox\n")
    assert run_pre_push(ob, repo, monkeypatch, push_line(head, base)) == 1
    err = capsys.readouterr().err
    assert "lane wire path" in err and "coordination/lanes/x" not in err


def test_the_stewards_move_into_sent_is_not_validated_as_an_item(ob, repo, monkeypatch):
    """The steward's landing commit deletes the root item and adds it under sent/. Validating the
    deleted root path (or the sent copy) refused that commit on the reference's own first live drain."""
    commit(repo, {f"{OUTBOX_DIR}/20261016-the-fix.md": item_text()}, "file\n\nDoctrine-Export: outbox\n")
    base = git(repo, "rev-parse", "HEAD")
    (repo / OUTBOX_DIR / "sent").mkdir()
    git(repo, "mv", f"{OUTBOX_DIR}/20261016-the-fix.md", f"{OUTBOX_DIR}/sent/20261016-the-fix.md")
    git(repo, "commit", "-m", "landed on the bus\n\nDoctrine-Export: none\n")
    assert run_pre_push(ob, repo, monkeypatch, push_line(git(repo, "rev-parse", "HEAD"), base)) == 0


# ---------- check-range (legacy form) ----------

def test_check_range_legacy_form_takes_repo_then_range(ob, repo, capsys):
    base = git(repo, "rev-parse", "HEAD")
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "no trailer")
    assert ob.main(["check-range", str(repo), f"{base}..HEAD"]) == 1
    assert "carries no" in capsys.readouterr().err
    assert ob.main(["check-range", str(repo), f"{base}..{base}"]) == 0


# ---------- stop ----------

def test_stop_blocks_once_then_yields(ob, pushed_repo, monkeypatch, capsys):
    repo = pushed_repo
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "a recent commit with no trailer")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert ob.main(["stop", str(repo)]) == 0
    verdict = json.loads(capsys.readouterr().out)
    assert verdict["decision"] == "block" and "coordination/tools/other.py" in verdict["reason"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"stop_hook_active": True})))
    assert ob.main(["stop", str(repo)]) == 0
    assert capsys.readouterr().out == ""


def test_stop_is_silent_when_every_recent_commit_is_clean(ob, pushed_repo, monkeypatch, capsys):
    commit(pushed_repo, {"coordination/tools/other.py": "print(1)\n"}, "declared\n\nDoctrine-Export: none\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert ob.main(["stop", str(pushed_repo)]) == 0
    assert capsys.readouterr().out == ""


def test_stop_looks_back_24_hours_only(ob, pushed_repo, monkeypatch, capsys):
    commit(pushed_repo, {"coordination/tools/other.py": "print(1)\n"}, "an old commit with no trailer", env=dated(3 * 24 * 3600))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert ob.main(["stop", str(pushed_repo)]) == 0
    assert capsys.readouterr().out == ""


def test_stop_survives_an_unreadable_range(ob, pushed_repo, monkeypatch, capsys):
    git(pushed_repo, "branch", "-m", "trunk")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert ob.main(["stop", str(pushed_repo)]) == 0
    assert capsys.readouterr().out == ""


def test_stop_judges_only_commits_not_yet_on_the_remote(ob, pushed_repo, monkeypatch, capsys):
    """A commit already on the remote default ref cannot be fixed by the follow-up commit the nudge
    asks for, so it must not be judged; the commit still waiting to be pushed must be."""
    repo = pushed_repo
    commit(repo, {"coordination/tools/old.py": "print(1)\n"}, "pushed long ago, no trailer")
    git(repo, "push", "-q", "origin", "master")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert ob.main(["stop", str(repo)]) == 0
    assert capsys.readouterr().out == "", "an already-pushed commit was judged"
    commit(repo, {"coordination/tools/new.py": "print(2)\n"}, "not pushed yet, no trailer")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert ob.main(["stop", str(repo)]) == 0
    reason = json.loads(capsys.readouterr().out)["reason"]
    assert "coordination/tools/new.py" in reason and "coordination/tools/old.py" not in reason


def test_stop_ref_option_names_the_base_of_the_unpushed_range(ob, pushed_repo, monkeypatch, capsys):
    repo = pushed_repo
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "no trailer")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert ob.main(["stop", str(repo), "--ref", "HEAD"]) == 0     # HEAD..HEAD is empty
    assert capsys.readouterr().out == ""
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert ob.main(["stop", str(repo), "--ref", "origin/master"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


# ---------- end to end: the installed git hook, the real file, a real push ----------

def test_installed_hook_blocks_and_allows_a_real_push(ob, repo, tmp_path):
    """The shell snippet install-hooks writes runs the vendored file in the repo, which finds its own
    config at the repo root: a push of an undeclared finding-path commit is refused by git itself."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")   # before the guard exists
    assert ob.main(["install-hooks", str(repo)]) == 0
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "undeclared")
    refused = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True)
    assert refused.returncode != 0 and "PUSH REFUSED" in refused.stderr
    git(repo, "commit", "--amend", "-m", "declared\n\nDoctrine-Export: none\n")
    allowed = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True)
    assert allowed.returncode == 0, allowed.stderr


# ---------- r6: where the guard is spliced, the check argument, the cost of a new branch ----------

def _origin_with_master(repo: Path, tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True, text=True)
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "master")


def test_install_splices_the_guard_ahead_of_a_hook_that_ends_in_exit_zero(ob, repo, tmp_path):
    """Dot / opus OUTBOX-HOOK-SPLICE-AFTER-EXIT-1: r1-r5 appended the guard at the end, so a hook that
    finishes with `exit 0` ended before it and every push went through unjudged."""
    _origin_with_master(repo, tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nset -e\necho the-hooks-own-layer >&2\nexit 0\n", encoding="utf-8", newline="\n")
    assert ob.install_pre_push(repo) == "installed"
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\nset -e\n")
    assert text.index("doctrine-outbox-guard") < text.index("the-hooks-own-layer") < text.index("exit 0")
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "undeclared")
    refused = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True)
    assert refused.returncode != 0 and "PUSH REFUSED" in refused.stderr, refused.stderr
    assert "the-hooks-own-layer" not in refused.stderr, "the hook's own layer ran although the guard refused"
    git(repo, "commit", "--amend", "-m", "declared\n\nDoctrine-Export: none\n")
    allowed = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True)
    assert allowed.returncode == 0 and "the-hooks-own-layer" in allowed.stderr, allowed.stderr


def test_install_splices_the_guard_ahead_of_a_hook_that_execs_another_command(ob, repo, tmp_path):
    _origin_with_master(repo, tmp_path)
    base = git(repo, "rev-parse", "HEAD")
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text('#!/bin/sh\nexec cat > "$DOWNSTREAM_OUT"   # the next hook replaces this shell\n', encoding="utf-8", newline="\n")
    assert ob.install_pre_push(repo) == "installed"
    assert hook.read_text(encoding="utf-8").index("doctrine-outbox-guard") < hook.read_text(encoding="utf-8").index("exec cat")
    seen = tmp_path / "downstream.bin"
    env = {**os.environ, "DOWNSTREAM_OUT": str(seen)}
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "undeclared")
    refused = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True, env=env)
    assert refused.returncode != 0 and "PUSH REFUSED" in refused.stderr and not seen.exists()
    git(repo, "commit", "--amend", "-m", "declared\n\nDoctrine-Export: none\n")
    head = git(repo, "rev-parse", "HEAD")
    allowed = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True, env=env)
    assert allowed.returncode == 0, allowed.stderr
    assert seen.read_bytes() == f"refs/heads/master {head} refs/heads/master {base}\n".encode()


def test_install_refuses_a_guard_that_sits_after_a_terminating_exit_or_exec(ob, repo, capsys):
    """An older or current guard behind a top-level `exit`/`exec` is repaired: install-hooks moves it ahead of the terminator, prints `repaired`, leaves exactly one guard, and a second install prints `present`.
    A guard behind a terminator never runs, so install-hooks must not report `present` for it before the move.
    (The name predates the repair behaviour and is kept; a rename would read as a removed test.)"""
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    assert ob.install_pre_push(repo) == "installed"
    current = hook.read_text(encoding="utf-8")
    current_guard = current[current.index("# doctrine-outbox-guard"):]
    shapes = {
        "older guard after exit": "#!/bin/sh\nexit 0\n\n" + _guard_block(spool=True, python_missing_passes=True),
        "current guard after exit": "#!/bin/sh\nexit 0\n\n" + current_guard,
        "current guard after exec": "#!/bin/sh\nexec true\n\n" + current_guard,
    }
    for name, text in shapes.items():
        hook.write_text(text, encoding="utf-8", newline="\n")
        # r9 (CoS r7 improvement 1): a recognised guard behind an `exit`/`exec` is MOVED to the splice point, not
        # refused. r8 and earlier asserted exit code 1, HOOK_GUARD_UNREACHABLE on stderr and an untouched hook here.
        assert ob.main(["install-hooks", str(repo)]) == 0, name
        captured = capsys.readouterr()
        assert captured.out.strip() == "repaired" and "HOOK_GUARD_UNREACHABLE" not in captured.err, (name, captured)
        moved = hook.read_text(encoding="utf-8")
        assert moved != text and moved.count("# doctrine-outbox-guard:") == 1, f"{name}: the guard was not moved"
        assert moved.index("# doctrine-outbox-guard:") < re.search(r"^(exit|exec)\b", moved, re.MULTILINE).start(), name
        assert ob.install_pre_push(repo) == "present", name
    # `exec < file` only redirects and an indented `exit` is conditional: neither ends the hook.
    hook.write_text("#!/bin/sh\nif [ -n \"$SKIP_LAYER\" ]; then\n  exit 0\nfi\nexec 3>&1\n" + current_guard, encoding="utf-8", newline="\n")
    # r7: a guard behind real layers is no longer "present": it moves to the splice point ("repaired"), after
    # which a second install finds it where it belongs.
    assert ob.install_pre_push(repo) == "repaired"
    assert ob.install_pre_push(repo) == "present"


def test_check_with_a_non_integer_age_is_a_usage_error_not_a_traceback(ob, repo, capsys):
    """OUTBOX-CHECK-ARG-PARSE-1: `check REPO soon` died in int() with a traceback."""
    assert ob.main(["check", str(repo), "soon"]) == 2
    captured = capsys.readouterr()
    assert "usage: check" in captured.err and "'soon'" in captured.err and "Traceback" not in captured.err
    assert captured.out == "", "a usage error must not print a debt verdict"
    run = subprocess.run([sys.executable, str(repo / TOOL_REL), "--config", str(repo / "doctrine-outbox.config.json"),
                          "check", str(repo), "1.5"], capture_output=True, text=True)
    assert run.returncode == 2 and "Traceback" not in run.stderr and "usage: check" in run.stderr, run.stderr


@pytest.mark.parametrize("set_head", [True, False], ids=["origin-HEAD-set", "origin-HEAD-absent"])
def test_pre_push_of_a_new_branch_judges_only_what_is_not_on_the_remote_default(tmp_path, pushed_repo, monkeypatch, capsys, set_head):
    """OUTBOX-PREPUSH-NEW-BRANCH-COST-1: a push that creates a branch used to re-judge the whole history.
    The old undeclared commit is already on origin/master, so it is not this push's business; a bad
    commit made on the new branch is."""
    cfg = tmp_path / "new-branch.config.json"
    cfg.write_text(json.dumps({**BUS_STYLE_CONFIG, "pre_push_refs": ["refs/heads/master", "refs/heads/feature"]}), encoding="utf-8")
    mod = load_tool(cfg)
    repo = pushed_repo
    commit(repo, {"coordination/tools/old.py": "print(1)\n"}, "on the default branch long ago, no trailer")
    git(repo, "push", "-q", "origin", "master")
    if set_head:
        git(repo, "remote", "set-head", "origin", "master")
    git(repo, "checkout", "-q", "-b", "feature")
    clean = commit(repo, {"docs/notes.md": "prose\n"}, "clean on the branch")
    new_branch = lambda sha: f"refs/heads/feature {sha} refs/heads/feature {ZEROS}\n"   # noqa: E731
    assert run_pre_push(mod, repo, monkeypatch, new_branch(clean)) == 0, capsys.readouterr().err
    bad = commit(repo, {"coordination/tools/new.py": "print(2)\n"}, "on the branch, no trailer")
    assert run_pre_push(mod, repo, monkeypatch, new_branch(bad)) == 1
    err = capsys.readouterr().err
    assert "coordination/tools/new.py" in err and "coordination/tools/old.py" not in err
    # No remote default branch to measure from (the push names a remote this clone has never fetched):
    # fall back to the whole history rather than guess a base.
    monkeypatch.setattr(sys, "stdin", io.StringIO(new_branch(clean)))
    assert mod.main(["pre-push", str(repo), "elsewhere", "https://example.invalid/x.git"]) == 1
    assert "coordination/tools/old.py" in capsys.readouterr().err


# ---------- r7: a repaired guard moves to the splice point; outcome x hostile shape ----------

STDIN_READER_LAYER = "while read lref lsha rref rsha; do :; done\necho the-hooks-own-layer >&2\n"


def _push_undeclared(repo: Path) -> subprocess.CompletedProcess:
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "undeclared")
    return subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True)


def test_repair_moves_an_older_guard_ahead_of_a_layer_that_reads_the_ref_lines(ob, repo, tmp_path):
    """CoS r6 improvement 1 (comment 6226980669): repair replaced an r4 guard where it stood, at the end, so a
    project layer that reads git's ref lines from stdin ahead of it left the guard an empty stream and every
    push went through. A recognised older block moves to the splice point a fresh install uses."""
    _origin_with_master(repo, tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text("#!/bin/sh\nset -e\n" + STDIN_READER_LAYER + "\n" + _guard_block(spool=True, python_missing_passes=True),
                    encoding="utf-8", newline="\n")
    assert ob.install_pre_push(repo) == "repaired"
    text = hook.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\nset -e\n")
    assert text.count("# doctrine-outbox-guard:") == 1 and "the push guard did not run" not in text
    refused = _push_undeclared(repo)   # behaviour first: on b5310592 this push is ALLOWED (returncode 0)
    assert refused.returncode != 0 and "PUSH REFUSED" in refused.stderr, f"undeclared push was allowed: {refused.returncode} {refused.stderr}"
    assert "the-hooks-own-layer" not in refused.stderr, "the hook's own layer ran although the guard refused"
    assert text.index("doctrine-outbox-guard") < text.index("while read lref"), "the repaired guard still sits behind the stdin reader"
    assert ob.install_pre_push(repo) == "present"


def test_repair_moves_an_older_guard_that_sits_after_a_top_level_exit(ob, repo, tmp_path):
    """CoS r7 improvement 1 (comment 6227688765): r1-r4 appended the guard at the end, so every project hook that
    ends in `exit 0` holds a dead older guard. Repair used to refuse that one population (the pre-move check on
    the OLD position); it now moves the block ahead of the exit, and the check on the NEW index stays."""
    _origin_with_master(repo, tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    # The CoS repro: hook body `echo L; exit 0`, then an r4 install (guard appended at the end).
    hook.write_text("#!/bin/sh\necho L\nexit 0\n\n" + _guard_block(spool=True, python_missing_passes=True),
                    encoding="utf-8", newline="\n")
    assert ob.install_pre_push(repo) == "repaired"   # on 87569086 this raised HOOK_GUARD_UNREACHABLE
    text = hook.read_text(encoding="utf-8")
    assert text.count("# doctrine-outbox-guard:") == 1 and "the push guard did not run" not in text
    assert text.index("doctrine-outbox-guard") < text.index("echo L") < text.index("exit 0"), "the guard is still behind the exit"
    refused = _push_undeclared(repo)
    assert refused.returncode != 0 and "PUSH REFUSED" in refused.stderr, f"undeclared push was allowed: {refused.returncode} {refused.stderr}"
    assert ob.install_pre_push(repo) == "present"


_HOSTILE_SHAPES = {
    "ends-in-exit-0": "echo the-hooks-own-layer >&2\nexit 0\n",
    "execs-another-hook": 'exec cat > "$DOWNSTREAM_OUT"\n',
    "stdin-reading-layer": STDIN_READER_LAYER,
}


def _outcome_hook(kind: str, shape_body: str, ob, repo: Path) -> tuple[str, str]:
    """(hook text, expected install outcome when the install is not refused) for an install outcome built
    over a hostile hook shape."""
    head = "#!/bin/sh\nset -e\n"
    if kind == "installed":
        return head + shape_body, "installed"
    if kind == "repaired":
        return head + shape_body + "\n" + _guard_block(spool=True, python_missing_passes=True), "repaired"
    if kind == "present":
        hook = repo / ".git" / "hooks" / "pre-push"
        hook.write_text(head + shape_body, encoding="utf-8", newline="\n")
        assert ob.install_pre_push(repo) == "installed"
        return hook.read_text(encoding="utf-8"), "present"
    if kind == "edited-block":
        # r9: a block someone edited by hand: the marker is there, but there is no end marker and no known older
        # shape, so its extent cannot be told. The one kind that stays refused (see _CELL_REFUSAL_REASON).
        return (head + shape_body + "\n# doctrine-outbox-guard: findings reach the bus by file; a finding-path commit must declare.\n"
                "echo edited-by-the-owner >&2\n"), "refused"
    # A CURRENT guard appended behind the shape: it is no more reachable than an old one.
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(head, encoding="utf-8", newline="\n")
    assert ob.install_pre_push(repo) == "installed"
    current = hook.read_text(encoding="utf-8")
    return head + shape_body + "\n" + current[current.index("# doctrine-outbox-guard"):], "repaired"


_MATRIX_KINDS = ["installed", "repaired", "present", "current-guard-appended", "edited-block"]
# r9 (CoS r7 improvement 2, ACK 6227942977): ONE pinned outcome per cell. r7 and r8 accepted any refusal in any
# cell, so a safe repair that regressed into a refusal still passed (repaired x ends-in-exit-0 did exactly that
# until r9). A pin is "installed" / "repaired" / "present", or ("refused", CODE).
_CELL_PINS = {
    **{("installed", s): "installed" for s in _HOSTILE_SHAPES},              # fresh install: the guard goes ahead of every layer
    **{("repaired", s): "repaired" for s in _HOSTILE_SHAPES},                # an r4 block is moved to the splice point
    **{("present", s): "present" for s in _HOSTILE_SHAPES},                  # the install above is already at the splice point
    **{("current-guard-appended", s): "repaired" for s in _HOSTILE_SHAPES},  # a current block behind the layers is moved too
    **{("edited-block", s): ("refused", "HOOK_UNRECOGNISED") for s in _HOSTILE_SHAPES},
}
# Every refused cell carries the written reason a move is not safe there (the CoS prevention check).
_CELL_REFUSAL_REASON = {
    "edited-block": "the block has a marker but no end marker and no known older shape, so where it ends cannot be proved; "
                    "moving it would cut into the owner's own lines of the hook, and leaving it would report a dead guard as live",
}


def _check_matrix_cell(ob, repo: Path, tmp_path: Path, capsys, kind: str, shape: str, pin) -> None:
    """Run install-hooks over one (kind, shape) cell and assert the cell's outcome equals `pin` exactly."""
    _origin_with_master(repo, tmp_path)
    hook = repo / ".git" / "hooks" / "pre-push"
    text, _unused = _outcome_hook(kind, _HOSTILE_SHAPES[shape], ob, repo)
    hook.write_text(text, encoding="utf-8", newline="\n")
    capsys.readouterr()
    rc = ob.main(["install-hooks", str(repo)])
    out = capsys.readouterr()
    if isinstance(pin, tuple):
        code = pin[1]
        assert rc != 0 and code in out.err, f"{kind}/{shape}: pinned as refused with {code}, got rc={rc} out={out.out!r} err={out.err!r}"
        assert hook.read_text(encoding="utf-8") == text, "a refused install must not rewrite the hook"
        return
    assert rc == 0 and out.out.strip() == pin, f"{kind}/{shape}: pinned as {pin!r}, got rc={rc} out={out.out!r} err={out.err!r}"
    assert hook.read_text(encoding="utf-8").count("# doctrine-outbox-guard:") == 1
    seen = tmp_path / "downstream.bin"
    commit(repo, {"coordination/tools/other.py": "print(1)\n"}, "undeclared")
    refused = subprocess.run(["git", "-C", str(repo), "push", "origin", "master"], capture_output=True, text=True,
                             env={**os.environ, "DOWNSTREAM_OUT": str(seen)})
    assert refused.returncode != 0 and "PUSH REFUSED" in refused.stderr, f"{kind}/{shape}: the guard is dead: {refused.stderr}"
    assert not seen.exists() and "the-hooks-own-layer" not in refused.stderr


@pytest.mark.parametrize("kind", _MATRIX_KINDS)
@pytest.mark.parametrize("shape", sorted(_HOSTILE_SHAPES))
def test_no_install_outcome_leaves_a_dead_guard_behind_a_hostile_hook_shape(ob, repo, tmp_path, capsys, kind, shape):
    """CoS r6 prevention check, r9 form: every install outcome over every hostile hook shape matches ONE pinned
    outcome. "installed", "repaired" or "present" must leave a guard that refuses an undeclared push; a pinned
    refusal must name its code and leave the hook untouched. A cell that drifts from its pin fails."""
    _check_matrix_cell(ob, repo, tmp_path, capsys, kind, shape, _CELL_PINS[(kind, shape)])


def test_every_matrix_cell_has_exactly_one_pin_and_every_refused_cell_has_a_written_reason():
    cells = {(k, s) for k in _MATRIX_KINDS for s in _HOSTILE_SHAPES}
    assert set(_CELL_PINS) == cells, "a cell has no pin, or a pin names no cell"
    for (kind, shape), pin in _CELL_PINS.items():
        if isinstance(pin, tuple):
            assert pin[0] == "refused" and re.fullmatch(r"HOOK_[A-Z_]+", pin[1]), (kind, shape, pin)
            assert len(_CELL_REFUSAL_REASON.get(kind, "")) > 40, f"refused cell {kind}/{shape} carries no written reason a move is unsafe"
        else:
            assert pin in ("installed", "repaired", "present"), (kind, shape, pin)


def test_flipping_a_cells_pin_turns_the_matrix_red(ob, repo, tmp_path, capsys):
    """r9 improvement 2: the repaired x ends-in-exit-0 cell is `repaired`. Pinned as a refusal (what r7 and r8 let
    pass) or as `installed`, the same cell must FAIL; the correct pin must pass."""
    scratch = tmp_path / "flip"
    scratch.mkdir()
    for wrong in (("refused", "HOOK_GUARD_UNREACHABLE"), "installed", "present"):
        with pytest.raises(AssertionError):
            _check_matrix_cell(ob, repo, scratch, capsys, "repaired", "ends-in-exit-0", wrong)
        git(repo, "remote", "remove", "origin")   # each call adds the scratch origin again
    _check_matrix_cell(ob, repo, scratch, capsys, "repaired", "ends-in-exit-0", "repaired")


# ---------- r8: the new-branch base comes only from a ref the hook guards ----------

def _diverged_remote(repo: Path, guarded_branch: str, publish_guarded: bool) -> tuple[str, str]:
    """`pushed_repo` plus: the guarded branch (one commit past the root), an unguarded origin/master that has
    moved on, and an unguarded origin/topic. Returns (guarded tip, local sha of a new branch cut from that tip
    with one more commit). The two merge-bases differ, so a wrong base shows as a different sha."""
    root = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "-b", guarded_branch, root)
    tip = commit(repo, {"docs/guarded.md": "prose\n"}, "on the guarded branch")
    if publish_guarded:
        git(repo, "push", "-q", "origin", f"{guarded_branch}:refs/heads/{guarded_branch}")
    git(repo, "push", "-q", "origin", f"{root}:refs/heads/topic")
    git(repo, "checkout", "-q", "master")
    commit(repo, {"coordination/tools/old.py": "print(1)\n"}, "on master long ago, no trailer")
    git(repo, "push", "-q", "origin", "master")
    git(repo, "checkout", "-q", "-b", "feature", tip)
    return tip, commit(repo, {"docs/notes.md": "more prose\n"}, "on the new branch")


@pytest.mark.parametrize("guarded_branch,publish_guarded,set_head,expect_guarded_tip", [
    ("release", False, False, False),       # guarded branch absent on the remote; unguarded origin/master must not stand in
    ("release/1.0", True, False, True),     # a branch name with a slash is the whole name after refs/heads/
    ("release", False, True, False),        # origin/HEAD -> origin/master is a ref the hook never judged
], ids=["unguarded-master-never-picked", "slash-branch-keeps-its-whole-name", "unguarded-origin-HEAD-never-picked"])
def test_new_branch_base_comes_only_from_a_guarded_ref(tmp_path, pushed_repo, guarded_branch, publish_guarded, set_head, expect_guarded_tip):
    """Iverson advisory on b5310592 (comment 6227229808, ACK 6227530991): _remote_default_base fell back to
    origin/master and origin/main by name and read the name after the LAST slash, so the base could be a ref
    the hook never judged. A base it cannot prove is None (walk everything)."""
    cfg = tmp_path / "base-ref.config.json"
    cfg.write_text(json.dumps({**BUS_STYLE_CONFIG, "pre_push_refs": [f"refs/heads/{guarded_branch}"]}), encoding="utf-8")
    mod = load_tool(cfg)
    repo = pushed_repo
    tip, local = _diverged_remote(repo, guarded_branch, publish_guarded)
    if set_head:
        git(repo, "remote", "set-head", "origin", "master")
    base = mod._remote_default_base(repo, "origin", local)
    assert base == (tip if expect_guarded_tip else None), (base, tip)
    assert base != git(repo, "merge-base", "refs/remotes/origin/master", local)


def test_new_branch_push_with_no_guarded_base_judges_the_whole_history(tmp_path, pushed_repo, monkeypatch, capsys):
    """The behaviour behind the unit rows: with origin/master unguarded and the guarded branch not yet on the
    remote, the old undeclared commit on origin/master is judged, not skipped."""
    cfg = tmp_path / "base-ref-push.config.json"
    cfg.write_text(json.dumps({**BUS_STYLE_CONFIG, "pre_push_refs": ["refs/heads/release"]}), encoding="utf-8")
    mod = load_tool(cfg)
    repo = pushed_repo
    _diverged_remote(repo, "release", publish_guarded=False)
    git(repo, "checkout", "-q", "-B", "release", "master")   # cut from the unguarded master, old.py in its history
    local = commit(repo, {"docs/notes.md": "more prose\n"}, "on the new guarded branch")
    line = f"refs/heads/release {local} refs/heads/release {ZEROS}\n"
    assert run_pre_push(mod, repo, monkeypatch, line) == 1
    assert "coordination/tools/old.py" in capsys.readouterr().err
