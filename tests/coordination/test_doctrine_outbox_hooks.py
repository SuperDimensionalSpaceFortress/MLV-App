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


@pytest.fixture
def repo(tmp_path):
    """A fixture project on master: the tool at its configured path, the outbox README, one root commit."""
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


def test_adoption_route_is_the_bus_pin_model():
    """The bus deleted tools/conjugal-reference/doctrine_outbox.py (665b4566) and shares tools by pin:
    "A tool here is never copied into a project: the project pins it" (bus tools/factory/README.md).
    The config, card, README and the tool's docstring must name tools/factory/ and the pin, and must
    not send a project to replace a file in the deleted reference directory or to vendor a copy."""
    assert _bus_candidate()["bus_path"] == CANDIDATE_BUS_PATH
    surfaces = {
        "config": (REPO_ROOT / "doctrine-outbox.config.json").read_text(encoding="utf-8"),
        "card": HANDOFF_CARD.read_text(encoding="utf-8"),
        "readme": OUTBOX_README.read_text(encoding="utf-8"),
        "tool docstring": ast.get_docstring(ast.parse(TOOL_SRC.read_text(encoding="utf-8"))) or "",
    }
    for name, text in surfaces.items():
        assert "factory-pins" in text and CANDIDATE_BUS_PATH in text, f"{name} does not name the bus pin route"
        assert "conjugal-reference" not in text, f"{name} still names the deleted reference directory"
        assert not re.search(r"\bvendor(s|ed|ing)?\b|re-vendor", text, re.IGNORECASE), f"{name} still tells a project to vendor a copy"
    assert "pin-runner.py pin doctrine-outbox" in surfaces["card"]


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
