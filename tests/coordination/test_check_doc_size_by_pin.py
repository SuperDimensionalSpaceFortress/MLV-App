"""CHECK-DOC-SIZE-SINGLE-SOURCE-1: the pinned bus check-doc-size, run through the pin runner against the
committed budget config, passes a repo under budget and fails one over it.

CoS #491 I2: test_factory_pins.py validates the pins file and runs `--help`; nothing there proves the tool
GOVERNS. These cases run the real committed config (tools/docs/doc-size-budget.config.json) over scratch
repos and read the exit code and the report.

Hermetic: the pinned bytes come from the committed fixture (never the real bus clone), the blob cache is a
scratch directory, FACTORY_BUS_CLONE / FACTORY_PINS_CACHE / GIT_DIR and friends are stripped
(test_factory_pins.hermetic_env), and the consumer's bus_clone list is emptied. A case that needs a bus
builds a scratch one.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_factory_pins import REPO, consumer, hermetic_env, load_pins, pinned_fixture, scratch_bus

CONFIG = REPO / "tools" / "docs" / "doc-size-budget.config.json"
WRAPPER = REPO / "tools" / "docs" / "run_doc_size.py"


def make_repo(tmp_path: Path, files: dict[str, int]) -> Path:
    """A scratch repo (no git needed: the config scans the worktree) holding .md files of the given sizes."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, size in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"a" * size)
    return repo


def run_tool(tmp_path: Path, repo: Path, *args: str, env_extra: dict[str, str] | None = None):
    """The pinned check-doc-size, through the vendored pin runner, exactly as a hook would run it."""
    root, env = consumer(tmp_path, load_pins())
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "factory-pins/pin-runner.py", "run", "check-doc-size", "--",
         "--config", str(CONFIG), "--repo", str(repo), *args],
        cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=120)


def test_the_committed_config_is_read_by_the_pinned_tool(tmp_path):
    done = run_tool(tmp_path, make_repo(tmp_path, {"docs/a.md": 10}), "--all")
    assert done.returncode == 0, done.stderr
    assert "docs/a.md" in done.stdout and "DOC SIZE: 1 governed files" in done.stdout, done.stdout


def test_a_repo_under_budget_passes(tmp_path):
    repo = make_repo(tmp_path, {
        "CLAUDE.md": 7_999,                      # entry soft 8,000
        "AGENTS.md": 8_000,                      # at the soft cap is not over it
        "docs/a.md": 25_000,                     # working soft 25,000
        ".claude-state/ledger.md": 150_000,      # hub: soft == hard == 150,000
    })
    done = run_tool(tmp_path, repo)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "DOC SIZE: 4 governed files" in done.stdout, done.stdout
    assert "0 breach, 0 over soft cap" in done.stdout, done.stdout
    assert "BREACH" not in done.stdout


def test_a_file_over_the_hard_cap_fails(tmp_path):
    done = run_tool(tmp_path, make_repo(tmp_path, {"docs/big.md": 40_001, "docs/ok.md": 100}))
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "BREACH" in done.stdout and "docs/big.md" in done.stdout, done.stdout
    assert "1 breach" in done.stdout, done.stdout
    assert "Policy of record: docs/22-doc-fragmentation-policy.md" in done.stdout, done.stdout


def test_a_file_over_the_soft_cap_warns_without_failing(tmp_path):
    done = run_tool(tmp_path, make_repo(tmp_path, {"docs/warn.md": 25_001}))
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "WARN" in done.stdout and "docs/warn.md" in done.stdout, done.stdout
    assert "0 breach, 1 over soft cap" in done.stdout, done.stdout


@pytest.mark.parametrize("rel,size,code", [
    ("CLAUDE.md", 12_000, 0), ("CLAUDE.md", 12_001, 1),
    ("AGENTS.md", 12_001, 1),
    ("docs/x.md", 40_000, 0), ("docs/x.md", 40_001, 1),
    ("tools/notes.md", 40_001, 1),
    ("notes.md", 40_001, 1),                                  # fallback tier
    (".claude/ANALYSIS_LOG.md", 60_000, 0), (".claude/ANALYSIS_LOG.md", 60_001, 1),
    (".claude-state/coordination/ledger.md", 150_000, 0), (".claude-state/coordination/ledger.md", 150_001, 1),
])
def test_each_tier_caps_where_the_config_says(tmp_path, rel, size, code):
    done = run_tool(tmp_path, make_repo(tmp_path, {rel: size}))
    assert done.returncode == code, (rel, size, done.stdout, done.stderr)


@pytest.mark.parametrize("rel", [
    "README.md", "docs/archive/old.md", "docs/plan-archive-2026.md", "CLOSEOUT-CANONICAL-CONTRACT.md",
    "platform/qt/avir/readme.md", ".claude-state/profiling/kit/copy.md", ".claude-state/worktrees/x/docs/y.md",
    ".claude/worktrees/x/docs/y.md", ".codex-state/doctrine/fold.md",
])
def test_exempt_paths_are_never_governed(tmp_path, rel):
    done = run_tool(tmp_path, make_repo(tmp_path, {rel: 500_000}), "--all")
    assert done.returncode == 0, (rel, done.stdout, done.stderr)
    assert "DOC SIZE: 0 governed files" in done.stdout, (rel, done.stdout)


@pytest.mark.parametrize("rel", [
    "docs/notes-é日本語.md",                 # the bus tool's git-tracked mode loses this name
    ".claude-state/coordination/ノート-ü.md",
])
def test_a_non_ascii_file_name_is_still_checked(tmp_path, rel):
    # MLV's retired tool decoded `git ls-files` as UTF-8 with surrogateescape; the bus tool decodes it with
    # the locale codec (airmypc/factory-check-doc-size-drops-non-ascii-paths). The committed config scans the
    # worktree instead, so the name is the str os.walk yields and never passes through a git decode.
    over = 150_001 if rel.startswith(".claude-state") else 40_001
    done = run_tool(tmp_path, make_repo(tmp_path, {rel: over}))
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert rel in done.stdout, f"the non-ASCII path was reported mangled or not at all:\n{done.stdout}"
    assert "BREACH" in done.stdout


def test_an_inherited_git_dir_changes_nothing(tmp_path):
    bogus = tmp_path / "not-a-repo"
    bogus.mkdir()
    env_extra = {"GIT_DIR": str(bogus), "GIT_WORK_TREE": str(bogus)}
    over = run_tool(tmp_path, make_repo(tmp_path, {"docs/big.md": 40_001}), env_extra=env_extra)
    assert over.returncode == 1 and "docs/big.md" in over.stdout, (over.stdout, over.stderr)


def test_a_pin_with_no_cache_and_no_bus_is_unavailable_and_runs_nothing(tmp_path):
    # bus_clone portability: factory-pins.json lists ONE absolute path, which only the owner's box has. On a
    # host with neither that clone nor a seeded blob cache the runner exits 3 (loud), and never runs bytes.
    root, env = consumer(tmp_path, load_pins(), seed_cache=False)
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "factory-pins/pin-runner.py", "run", "check-doc-size", "--",
         "--config", str(CONFIG), "--repo", str(make_repo(tmp_path, {"docs/a.md": 1}))],
        cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 3, (done.returncode, done.stdout, done.stderr)
    assert "DOC SIZE" not in done.stdout, "unverified bytes ran"


def test_factory_bus_clone_points_a_host_without_the_listed_path_at_its_own_clone(tmp_path):
    root, env = consumer(tmp_path, load_pins(), seed_cache=False)
    env["FACTORY_BUS_CLONE"] = str(scratch_bus(tmp_path))
    done = subprocess.run(
        [sys.executable, "-X", "utf8", "-I", "factory-pins/pin-runner.py", "run", "check-doc-size", "--",
         "--config", str(CONFIG), "--repo", str(make_repo(tmp_path, {"docs/a.md": 1}))],
        cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, (done.stdout, done.stderr)
    assert "DOC SIZE: 1 governed files" in done.stdout


def test_a_same_size_corrupt_cache_entry_is_never_executed(tmp_path):
    # CoS #491 I1 (doctrine-outbox note 20261018-pin-runner-cache-never-repairs-same-size-corruption): the
    # runner rejects corrupt cache bytes but cache_put skips replacement by size. Whatever the repair story,
    # the invariant MLV relies on is that corrupt bytes never run: with the bus gone it fails loudly (3).
    root, env = consumer(tmp_path, load_pins())
    record = load_pins()["tools"]["check-doc-size"]
    good = pinned_fixture("check-doc-size", record)
    (Path(env["FACTORY_PINS_CACHE"]) / record["blob"]).write_bytes(good[:-1] + bytes([good[-1] ^ 1]))
    repo = make_repo(tmp_path, {"docs/a.md": 1})
    argv = [sys.executable, "-X", "utf8", "-I", "factory-pins/pin-runner.py", "run", "check-doc-size", "--",
            "--config", str(CONFIG), "--repo", str(repo)]
    offline = subprocess.run(argv, cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert offline.returncode == 3 and "DOC SIZE" not in offline.stdout, (offline.returncode, offline.stdout)
    env["FACTORY_BUS_CLONE"] = str(scratch_bus(tmp_path))
    online = subprocess.run(argv, cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert online.returncode == 0 and "DOC SIZE: 1 governed files" in online.stdout, (online.stdout, online.stderr)


def wrapper_consumer(tmp_path: Path):
    """A scratch consumer shaped like this repo for the wrapper: tools/docs/{run_doc_size.py,config}, factory-pins/."""
    root, env = consumer(tmp_path, load_pins())
    (root / "tools" / "docs").mkdir(parents=True)
    shutil.copyfile(WRAPPER, root / "tools" / "docs" / "run_doc_size.py")
    shutil.copyfile(CONFIG, root / "tools" / "docs" / "doc-size-budget.config.json")
    return root, env


def test_the_wrapper_runs_the_pin_with_the_repo_config_and_writes_the_trace(tmp_path):
    root, env = wrapper_consumer(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "big.md").write_bytes(b"a" * 40_001)
    (root / "docs" / "small.md").write_bytes(b"a" * 10)
    argv = [sys.executable, "tools/docs/run_doc_size.py", "--trace", ".claude-state/doc-size-runs.log"]
    done = subprocess.run(argv, cwd=root, env=env, capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 1, (done.stdout, done.stderr)
    assert "docs/big.md" in done.stdout and "1 breach" in done.stdout, done.stdout
    trace = (root / ".claude-state" / "doc-size-runs.log").read_text(encoding="utf-8").splitlines()
    assert len(trace) == 1 and trace[0].endswith("governed=2 breaches=1 warnings=0"), trace
    # --warn-only keeps the hook's always-exit-0 contract, and the trace still lands.
    warn = subprocess.run(argv + ["--warn-only"], cwd=root, env=env, capture_output=True, encoding="utf-8",
                          errors="replace", timeout=120)
    assert warn.returncode == 0, (warn.stdout, warn.stderr)
    assert len((root / ".claude-state" / "doc-size-runs.log").read_text(encoding="utf-8").splitlines()) == 2


def test_the_wrapper_passes_the_runner_refusal_through_and_traces_nothing(tmp_path):
    root, env = wrapper_consumer(tmp_path)
    pins_file = root / "factory-pins" / "factory-pins.json"
    pins = json.loads(pins_file.read_text(encoding="utf-8"))
    pins["tools"]["check-doc-size"]["sha256"] = "0" * 64
    pins_file.write_text(json.dumps(pins), encoding="utf-8")
    env["FACTORY_BUS_CLONE"] = str(scratch_bus(tmp_path))
    shutil.rmtree(env["FACTORY_PINS_CACHE"])
    done = subprocess.run([sys.executable, "tools/docs/run_doc_size.py", "--trace", "run.log"], cwd=root, env=env,
                          capture_output=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 2, (done.returncode, done.stdout, done.stderr)
    assert not (root / "run.log").exists(), "a refused run wrote a trace line"
