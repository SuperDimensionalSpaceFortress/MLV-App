"""Tests for tools/coordination/doctrine_outbox.py -- MLV-App's port of agent-bridge's doctrine
outbox. Every repository used here is a throwaway git repository built under pytest's tmp_path;
nothing touches the network or the real bus. Lives under tests/coordination/ because CI runs that
directory by name (.github/workflows/tests.yml, "Run coordination and self-healing guardrails");
a test file beside the tool would never be collected.

Named mutations (each turns the named test red, then is reverted):
  M1  drop one LAW4 pattern (e.g. "fleet run receipts")  -> test_law4_pattern_refuses[fleet run receipts]
  M2  make key_on_bus() return False                     -> test_key_on_bus_blocks_reappend_with_empty_ledger
  M3  make keyword advisories fail check-ledger          -> test_keyword_hits_are_advisory_never_failing
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("doctrine_outbox", REPO_ROOT / "tools" / "coordination" / "doctrine_outbox.py")
ob = importlib.util.module_from_spec(_spec)
sys.modules["doctrine_outbox"] = ob
_spec.loader.exec_module(ob)

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]


def git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(["git", "-C", str(repo), *GIT_ID, *args], capture_output=True, text=True,
                            env={**os.environ, **env} if env else None)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


def git_raw(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr!r}"
    return result.stdout


def dated(seconds_ago: float) -> dict[str, str]:
    stamp = f"{int(time.time() - seconds_ago)} +0000"
    return {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}


def init_bus(tmp_path: Path, crlf_target: str | None = None, name: str = "bus",
             traps: str | None = None) -> tuple[Path, Path]:
    """A bare repo as the bus 'origin', and a clone of it as the bus working repo."""
    bare = tmp_path / f"{name}-origin.git"
    clone = tmp_path / f"{name}-clone"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(bare)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True, text=True)
    # The root log files only: a project's cards file is created by its first card filing.
    seeded = [t for t in ob.TARGETS if "/" not in t]
    for target in seeded:
        content = f"# {target}\n\n"
        if target == "TRAPS.md" and traps is not None:
            content = traps
        if crlf_target == target:
            content = content.replace("\n", "\r\n")
        (clone / target).write_bytes(content.encode("utf-8"))
    git(clone, "add", *seeded)
    git(clone, "commit", "-m", "seed bus files")
    git(clone, "push", "origin", "HEAD:refs/heads/master")
    return bare, clone


def init_source(tmp_path: Path, name: str = "source", with_tool: bool = True) -> Path:
    """A small source repo on master. With the tool committed, as on a ref that has adopted it."""
    src = tmp_path / name
    src.mkdir()
    subprocess.run(["git", "init", "-b", "master", str(src)], check=True, capture_output=True, text=True)
    (src / "README.md").write_text("fixture\n", encoding="utf-8")
    git(src, "add", "README.md")
    if with_tool:
        tool = src / ob.TOOL_REL
        tool.parent.mkdir(parents=True)
        tool.write_text("# stand-in for the tool at this ref\n", encoding="utf-8")
        git(src, "add", ob.TOOL_REL)
    git(src, "commit", "-m", "init")
    return src


def item_text(target: str = "TRAPS.md", kind: str = "trap", source_commit: str = "PENDING",
              ratified_by: str | None = None, published_as: str | None = None,
              body: str = "### Example finding\nBody text describing the finding.\n") -> str:
    front = f"target: {target}\nkind: {kind}\nsource_commit: {source_commit}\nlaw4: attested\n"
    if ratified_by:
        front += f"ratified_by: {ratified_by}\n"
    if published_as:
        front += f"published_as: {published_as}\n"
    return f"---\n{front}---\n{body}"


def add_item(src: Path, name: str, message: str | None = None, env: dict[str, str] | None = None, **kwargs) -> Path:
    item_dir = src / "doctrine-outbox"
    item_dir.mkdir(exist_ok=True)
    path = item_dir / name
    path.write_text(item_text(**kwargs), encoding="utf-8")
    git(src, "add", f"doctrine-outbox/{name}")
    git(src, "commit", "-m", message or f"add {name}", env=env)
    return path


# ---------- pinned runner identity ----------
#
# gather_deny_terms() reads socket.gethostname(), COMPUTERNAME, USERNAME, USER, USERPROFILE and
# HOME straight off the runner it executes on. Any test that reaches it -- directly, or through
# the CLI commands via ob.main() -- must not depend on this machine's real hostname or account
# name. This autouse fixture pins all six to fixed values that occur nowhere in the fixture
# bodies. A test that sets its own values afterward still wins.
PINNED_HOST = "outboxtestrunner"
PINNED_COMPUTERNAME = "OUTBOXTESTHOST"
PINNED_ACCOUNT = "outboxtestacct"
PINNED_USERPROFILE = f"C:\\Users\\{PINNED_ACCOUNT}"


@pytest.fixture(autouse=True)
def _pin_runner_identity(monkeypatch):
    monkeypatch.setattr(ob.socket, "gethostname", lambda: PINNED_HOST)
    monkeypatch.setenv("COMPUTERNAME", PINNED_COMPUTERNAME)
    monkeypatch.setenv("USERNAME", PINNED_ACCOUNT)
    monkeypatch.setenv("USER", PINNED_ACCOUNT)
    monkeypatch.setenv("USERPROFILE", PINNED_USERPROFILE)
    monkeypatch.setenv("HOME", PINNED_USERPROFILE)
    monkeypatch.delenv(ob.SUBJECT_LEDGER_ENV, raising=False)
    monkeypatch.delenv(ob.WATERMARK_ENV, raising=False)
    yield


# ---------- byte-compatibility with agent-bridge ----------

def test_key_and_block_are_byte_compatible_with_agent_bridge():
    # Golden vector computed from agent-bridge's own render_block (github/master bfc39bf) with
    # project "agent-bridge"; only the project word in the marker may differ here.
    item = {"meta": {"target": "TRAPS.md"}, "body": "### Golden entry\nBody line.\n"}
    commit = "0123456789abcdef0123456789abcdef01234567"
    key, block = ob.render_block(item, commit)
    assert key == "c6ae3711beb8bd2f"
    assert block == "### Golden entry\nBody line.\n<!-- outbox:c6ae3711beb8bd2f mlv-app:0123456789ab -->\n"
    assert ob.PROJECT == "mlv-app"


def test_default_ref_is_the_fork_master_tracking_ref_everywhere():
    assert ob.DEFAULT_REF == "refs/remotes/fork/master"
    parser = ob.build_parser()
    assert parser.parse_args(["debt"]).ref == ob.DEFAULT_REF
    assert parser.parse_args(["drain", "--bus", "x"]).ref == ob.DEFAULT_REF
    assert parser.parse_args(["check-commits"]).rev_range == f"{ob.DEFAULT_REF}..HEAD"


# ---------- parse / schema refusals ----------

def test_parse_item_no_front_matter():
    with pytest.raises(ob.Refusal) as excinfo:
        ob.parse_item("not front matter at all\n")
    assert excinfo.value.code == "ITEM_NO_FRONT_MATTER"


def test_parse_item_bad_target():
    with pytest.raises(ob.Refusal) as excinfo:
        ob.parse_item(item_text(target="NOTATARGET.md"))
    assert excinfo.value.code == "ITEM_BAD_TARGET"


def test_parse_item_rulings_without_ratified_by():
    with pytest.raises(ob.Refusal) as excinfo:
        ob.parse_item(item_text(target="RULINGS.md", kind="ruling"))
    assert excinfo.value.code == "ITEM_RULING_UNRATIFIED"


def test_parse_item_body_not_an_entry():
    with pytest.raises(ob.Refusal) as excinfo:
        ob.parse_item(item_text(body="not a heading at all\n"))
    assert excinfo.value.code == "ITEM_BODY_NOT_AN_ENTRY"


def test_parse_item_rulings_with_ratified_by_ok():
    item = ob.parse_item(item_text(target="RULINGS.md", kind="ruling", ratified_by="RULINGS.md#anchor"))
    assert item["meta"]["ratified_by"] == "RULINGS.md#anchor"


def test_parse_item_accepts_published_as_and_refuses_a_malformed_one():
    assert ob.parse_item(item_text(published_as="19545ca"))["meta"]["published_as"] == "19545ca"
    with pytest.raises(ob.Refusal) as excinfo:
        ob.parse_item(item_text(published_as="not-a-sha"))
    assert excinfo.value.code == "ITEM_BAD_PUBLISHED_AS"


# ---------- Law-4 screen ----------

# One sample per LAW4 class. Each must trip ITS OWN class first, so dropping a pattern from
# LAW4 turns exactly that parametrized case red (mutation M1).
LAW4_SAMPLES = {
    "email address": "### h\ncontact someone@example.com about it\n",
    "account/org uuid": "### h\nsession 123e4567-e89b-12d3-a456-426614174000 flagged\n",
    "lane wire path": "### h\nsee coordination/lanes/x for the wire\n",
    "HUB heartbeat": "### h\nread HUB.md first\n",
    "owner transcript store": "### h\nit lives in loops.json today\n",
    "bearer/API token": "### h\nheader Bearer abcdefghijklmnopqrstuvwxyz was sent\n",
    "owner state dir": "### h\nfiles under state/ were read\n",
    "dead-man floor surface": "### h\nsee coordination/deadman/ for it\n",
    "user home path": "### h\nfound at C:\\Users\\someacct\\notes.txt during review\n",
    "board private state dir": "### h\nnotes under .claude-state/tmp were read\n",
    "subject ledger": "### h\nthe subject-ledger records it\n",
    "fleet run receipts": "### h\nreceipts sit in fleet-runs today\n",
    "hub write-ahead log": "### h\nthe HUB_RUN_WAL tail says so\n",
    "dual-lane ledger": "### h\nthe dual-lane board says so\n",
    "GPU host name": "### h\nreproduced on Ultra-Magnus overnight\n",
}


def test_every_law4_class_has_a_sample():
    assert {name for name, _rx in ob.LAW4} == set(LAW4_SAMPLES)


@pytest.mark.parametrize("name", list(LAW4_SAMPLES))
def test_law4_pattern_refuses(name):
    with pytest.raises(ob.Refusal) as excinfo:
        ob.screen_law4(LAW4_SAMPLES[name])
    assert excinfo.value.code == "LAW4_REFUSED"
    assert name in excinfo.value.detail, f"the sample tripped a different class: {excinfo.value.detail}"


def test_law4_lets_an_ordinary_entry_through():
    ob.screen_law4("### Piped make masks a missing toolchain\nThe pipeline exit status is tail's.\n")


def test_law4_over_cap():
    with pytest.raises(ob.Refusal) as excinfo:
        ob.screen_law4("### h\n" + "word " * 500)
    assert excinfo.value.code == "LAW4_OVER_CAP"


def test_screen_law4_host_and_account_name_refused_without_echo(monkeypatch):
    monkeypatch.setattr(ob.socket, "gethostname", lambda: "secrethostname")
    monkeypatch.setenv("COMPUTERNAME", "SECRETHOSTNAME")
    monkeypatch.setenv("USERNAME", "secretacct")
    monkeypatch.setenv("USER", "secretacct")
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)

    terms = ob.gather_deny_terms(None)
    lowered = {value.lower() for _cls, value in terms}
    assert "secrethostname" in lowered
    assert "secretacct" in lowered

    with pytest.raises(ob.Refusal) as host_exc:
        ob.screen_law4("### heading\nreported from secrethostname during the run\n", terms)
    assert host_exc.value.code == "LAW4_REFUSED"
    assert "secrethostname" not in str(host_exc.value).lower()

    with pytest.raises(ob.Refusal) as acct_exc:
        ob.screen_law4("### heading\nrun by secretacct this morning\n", terms)
    assert "secretacct" not in str(acct_exc.value).lower()


def test_screen_law4_deny_file_name_refused_without_echo(tmp_path):
    deny_file = tmp_path / "deny-names.txt"
    deny_file.write_text("ProjectCodename\n", encoding="utf-8")
    terms = ob.gather_deny_terms(deny_file)
    assert ("deny-list name", "ProjectCodename") in terms
    with pytest.raises(ob.Refusal) as excinfo:
        ob.screen_law4("### heading\nthe ProjectCodename effort continues\n", terms)
    assert excinfo.value.code == "LAW4_REFUSED"
    assert "projectcodename" not in str(excinfo.value).lower()


def test_gather_deny_terms_refuses_short_host_or_account(monkeypatch):
    # A name shorter than 3 characters is unscreenable: refuse fail-closed, never drop it.
    monkeypatch.setattr(ob.socket, "gethostname", lambda: "ab")
    monkeypatch.delenv("COMPUTERNAME", raising=False)
    monkeypatch.setenv("USERNAME", "ab")
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    with pytest.raises(ob.Refusal) as excinfo:
        ob.gather_deny_terms(None)
    assert excinfo.value.code == "SHORT_IDENTITY_UNSCREENABLE"
    assert "ab" not in str(excinfo.value)


def test_gather_deny_terms_refuses_short_deny_file_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(ob.socket, "gethostname", lambda: "a-perfectly-normal-hostname")
    monkeypatch.delenv("COMPUTERNAME", raising=False)
    monkeypatch.setenv("USERNAME", "normalaccountname")
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    deny_file = tmp_path / "deny.txt"
    deny_file.write_text("xy\n", encoding="utf-8")
    with pytest.raises(ob.Refusal) as excinfo:
        ob.gather_deny_terms(deny_file)
    assert excinfo.value.code == "SHORT_IDENTITY_UNSCREENABLE"
    assert "xy" not in str(excinfo.value)


def test_underscore_adjacency_is_caught():
    terms = [("account name", "secretacct")]
    with pytest.raises(ob.Refusal):
        ob.screen_law4("### heading\nfound in secretacct_dump.log during review\n", terms)
    with pytest.raises(ob.Refusal):
        ob.screen_law4("### heading\nfound in state_secretacct_dump during review\n", terms)


# ---------- drain: dry run and push ----------

def test_dry_run_drain_pushes_nothing(tmp_path):
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"

    before = git(bare, "rev-parse", "master")
    report = ob.drain(src, clone, "HEAD", ledger, [], push=False)
    after = git(bare, "rev-parse", "master")

    assert before == after
    assert report["pushed"] is False
    assert report["published"] == []
    assert len(report.get("would_push", [])) == 1
    assert not ledger.exists()


def test_push_drain_appends_exactly_one_block_and_proves_it_with_ls_remote(tmp_path):
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"
    old_bytes = git_raw(clone, "show", "origin/master:TRAPS.md")

    report = ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert report["pushed"] is True
    assert len(report["published"]) == 1
    key = report["published"][0]["key"]
    remote_sha = git(clone, "ls-remote", "origin", "refs/heads/master").split()[0]
    assert remote_sha == report["commit"] == git(bare, "rev-parse", "master")

    new_bytes = git_raw(clone, "show", f"{remote_sha}:TRAPS.md")
    assert new_bytes.startswith(old_bytes)
    assert f"<!-- outbox:{key} mlv-app:".encode("utf-8") in new_bytes
    assert new_bytes.count(f"outbox:{key}".encode("utf-8")) == 1

    rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["key"] == key and rows[0]["target"] == "TRAPS.md" and rows[0]["item"] == "20260925-example"


def test_second_drain_is_idempotent(tmp_path):
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"

    first = ob.drain(src, clone, "HEAD", ledger, [], push=True)
    master_after_first = git(bare, "rev-parse", "master")
    second = ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert first["pushed"] is True
    assert second["published"] == [] and second["pushed"] is False
    assert git(bare, "rev-parse", "master") == master_after_first
    assert len([line for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]) == 1


def test_key_on_bus_blocks_reappend_with_empty_ledger(tmp_path):
    """M2. The sent ledger is per machine and can be lost (new clone, machine move). The marker
    on the fetched bus tip is then the ONLY thing standing between a rerun and a duplicate."""
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    first = ob.drain(src, clone, "HEAD", tmp_path / "sent-a.jsonl", [], push=True)
    assert first["pushed"] is True
    master_after_first = git(bare, "rev-parse", "master")

    fresh_clone = tmp_path / "bus-fresh-clone"
    subprocess.run(["git", "clone", str(bare), str(fresh_clone)], check=True, capture_output=True, text=True)
    lost_ledger = tmp_path / "sent-lost.jsonl"
    second = ob.drain(src, fresh_clone, "HEAD", lost_ledger, [], push=True)

    assert second["published"] == [] and second["pushed"] is False
    assert [a["via"] for a in second["already_sent"]] == ["bus_tip"]
    assert git(bare, "rev-parse", "master") == master_after_first
    assert git_raw(fresh_clone, "show", "origin/master:TRAPS.md").count(b"outbox:") == 1


def test_crlf_target_stays_crlf_conformant(tmp_path):
    bare, clone = init_bus(tmp_path, crlf_target="TRAPS.md")
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    new_bytes = git_raw(clone, "show", f"{report['commit']}:TRAPS.md")

    assert b"\r\n" in new_bytes
    assert b"\n" not in new_bytes.replace(b"\r\n", b""), "a bare LF was introduced into a CRLF file"


@pytest.mark.parametrize("tail,label", [
    ("### Old entry\nold body", "no-trailing-newline"),
    ("### Old entry\nold body\n", "single-newline"),
    ("### Old entry\nold body\n\n", "already-blank-line"),
])
def test_drain_puts_a_blank_line_before_every_appended_block(tmp_path, tail, label):
    # The live bus TRAPS.md has a tip that ends without a blank line before the next heading.
    bare, clone = init_bus(tmp_path, traps="# TRAPS\n\n" + tail)
    src = init_source(tmp_path)
    add_item(src, "20260925-first.md", body="### First finding\nfirst body\n")
    add_item(src, "20260925-second.md", body="### Second finding\nsecond body\n")
    old_bytes = git_raw(clone, "show", "origin/master:TRAPS.md")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    new_text = git_raw(clone, "show", f"{report['commit']}:TRAPS.md").decode("utf-8")

    assert new_text.encode("utf-8").startswith(old_bytes), label
    assert "old body\n\n### First finding" in new_text, label
    assert new_text.count("old body\n\n\n") == 0, "a blank line was doubled"
    assert "-->\n\n### Second finding" in new_text, "consecutive blocks must be separated too"


def test_drain_blank_line_separator_in_a_crlf_target(tmp_path):
    bare, clone = init_bus(tmp_path, crlf_target="TRAPS.md", traps="# TRAPS\n\n### Old entry\nold body\n")
    src = init_source(tmp_path)
    add_item(src, "20260925-first.md", body="### First finding\nfirst body\n")
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    new_bytes = git_raw(clone, "show", f"{report['commit']}:TRAPS.md")
    assert b"old body\r\n\r\n### First finding" in new_bytes


def test_non_fast_forward_race_retries_from_scratch(tmp_path, monkeypatch):
    bare, clone = init_bus(tmp_path)
    competitor = tmp_path / "competitor-clone"
    subprocess.run(["git", "clone", str(bare), str(competitor)], check=True, capture_output=True, text=True)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")

    original_make_worktree = ob.make_temp_worktree
    calls = {"n": 0}

    def racing_make_worktree(bus_repo, tip):
        calls["n"] += 1
        if calls["n"] == 1:
            current = (competitor / "TRAPS.md").read_bytes()
            addition = b"### Competitor entry\ncompetitor body\n<!-- outbox:cccccccccccccccc other:0123456789ab -->\n"
            (competitor / "TRAPS.md").write_bytes(current + addition)
            git(competitor, "add", "TRAPS.md")
            git(competitor, "commit", "-m", "competing outbox entry")
            git(competitor, "push", "origin", "HEAD:refs/heads/master")
        return original_make_worktree(bus_repo, tip)

    monkeypatch.setattr(ob, "make_temp_worktree", racing_make_worktree)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert calls["n"] >= 2
    assert report["pushed"] is True
    final_bytes = git_raw(clone, "show", f"{report['commit']}:TRAPS.md")
    assert b"outbox:cccccccccccccccc" in final_bytes, "the competitor's entry was overwritten"
    assert final_bytes.count(b"outbox:") == 2


def test_filename_deny_term_refused_before_any_commit(tmp_path):
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-secretacct-incident.md")
    ledger = tmp_path / "sent.jsonl"
    before = git(bare, "rev-parse", "master")

    report = ob.drain(src, clone, "HEAD", ledger, [("account name", "secretacct")], push=True)

    assert git(bare, "rev-parse", "master") == before
    assert report["published"] == [] and report["pushed"] is False
    assert any(r["code"] == "LAW4_REFUSED" for r in report["refused"])
    assert not ledger.exists()


def test_commit_author_and_committer_are_pinned_not_ambient(tmp_path):
    bare, clone = init_bus(tmp_path)
    subprocess.run(["git", "-C", str(clone), "config", "user.name", "Personal Name"], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(clone), "config", "user.email", "personal@example.com"], check=True, capture_output=True, text=True)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    fmt = git(clone, "show", "-s", "--format=%an <%ae>|%cn <%ce>", report["commit"])
    who = f"{ob.OUTBOX_IDENTITY_NAME} <{ob.OUTBOX_IDENTITY_EMAIL}>"
    assert fmt == f"{who}|{who}"


def test_prefix_broken_by_clean_filter_refuses_push(tmp_path):
    """A clean filter that mangles the leading bytes of the target must be caught AFTER the
    commit, comparing committed blobs -- the in-memory concatenation cannot see this."""
    bare, clone = init_bus(tmp_path)
    (clone / ".gitattributes").write_text("TRAPS.md filter=corrupt\n", encoding="utf-8")
    git(clone, "add", ".gitattributes")
    git(clone, "commit", "-m", "seed a corrupting clean filter attribute for TRAPS.md")
    git(clone, "push", "origin", "HEAD:refs/heads/master")
    filter_script = tmp_path / "corrupt_filter.py"
    filter_script.write_text("import sys\ndata = sys.stdin.buffer.read()\nsys.stdout.buffer.write(data[1:] if data else data)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(clone), "config", "filter.corrupt.clean", f'"{sys.executable}" "{filter_script}"'],
                   check=True, capture_output=True, text=True)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"
    before = git(bare, "rev-parse", "master")

    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert excinfo.value.code == "PREFIX_BROKEN"
    assert git(bare, "rev-parse", "master") == before
    assert not ledger.exists() or ledger.read_text(encoding="utf-8").strip() == ""


# ---------- published_as: hand-written bus entries are never re-appended ----------

def _bus_with_hand_entry(tmp_path):
    """A bus whose TRAPS.md already carries an entry written by hand (no outbox marker), then a
    later commit so that entry's sha is an ancestor of the tip and not the tip itself."""
    bare, clone = init_bus(tmp_path)
    hand = "### Hand written finding\nwritten by hand, no marker\n"
    traps = (clone / "TRAPS.md").read_text(encoding="utf-8")
    (clone / "TRAPS.md").write_text(traps + hand, encoding="utf-8")
    git(clone, "add", "TRAPS.md")
    git(clone, "commit", "-m", "hand entry")
    hand_sha = git(clone, "rev-parse", "HEAD")
    (clone / "RECEIPTS.md").write_text("# RECEIPTS.md\n\nlater unrelated commit\n", encoding="utf-8")
    git(clone, "add", "RECEIPTS.md")
    git(clone, "commit", "-m", "later commit")
    git(clone, "push", "origin", "HEAD:refs/heads/master")
    return bare, hand_sha


def test_published_as_item_is_skipped_on_a_fresh_clone_with_an_empty_ledger(tmp_path):
    bare, hand_sha = _bus_with_hand_entry(tmp_path)
    fresh_clone = tmp_path / "bus-fresh-clone"
    subprocess.run(["git", "clone", str(bare), str(fresh_clone)], check=True, capture_output=True, text=True)
    src = init_source(tmp_path)
    add_item(src, "20260928-hand-written.md", published_as=hand_sha[:12],
             body="### Hand written finding\nwritten by hand, no marker\n")
    before = git(bare, "rev-parse", "master")
    ledger = tmp_path / "never-existed.jsonl"

    report = ob.drain(src, fresh_clone, "HEAD", ledger, [], push=True)

    assert git(bare, "rev-parse", "master") == before, "a hand-published entry was appended a second time"
    assert report["published"] == [] and report["pushed"] is False and report["refused"] == []
    assert [a["via"] for a in report["already_sent"]] == ["published_as"]
    assert not ledger.exists()


@pytest.mark.parametrize("claim,heading,why", [
    ("deadbeefdead", "### Hand written finding", "not an ancestor"),
    (None, "### A different heading entirely", "heading"),
])
def test_unverifiable_published_as_claim_is_refused_and_never_appended(tmp_path, claim, heading, why):
    bare, hand_sha = _bus_with_hand_entry(tmp_path)
    clone = tmp_path / "bus-drain-clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True, text=True)
    src = init_source(tmp_path)
    add_item(src, "20260928-hand-written.md", published_as=claim or hand_sha[:12], body=f"{heading}\nbody\n")
    before = git(bare, "rev-parse", "master")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert git(bare, "rev-parse", "master") == before
    assert report["published"] == []
    assert [r["code"] for r in report["refused"]] == ["PUBLISHED_AS_UNVERIFIED"]
    assert why in report["refused"][0]["detail"]


# ---------- ref, not HEAD: a checkout on a peer branch ----------

def _fork_checkout(tmp_path):
    """MLV-App's shape: remote `fork` holds master; the work clone sits on a peer branch that
    carries an extra item master does not have."""
    fork_bare = tmp_path / "fork.git"
    subprocess.run(["git", "init", "--bare", "-b", "master", str(fork_bare)], check=True, capture_output=True, text=True)
    wc = init_source(tmp_path, "wc")
    add_item(wc, "20260925-on-master.md", body="### On master\nbody\n")
    git(wc, "remote", "add", "fork", str(fork_bare))
    git(wc, "push", "fork", "master")
    git(wc, "checkout", "-b", "peer/other-lane")
    add_item(wc, "20260926-peer-only.md", body="### Peer only\nbody\n")
    return wc


def test_debt_reads_fork_master_not_the_checked_out_peer_branch(tmp_path, capsys):
    wc = _fork_checkout(tmp_path)
    assert ob.main(["--repo", str(wc), "debt", "--ledger", str(tmp_path / "s.jsonl"), "--json"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ref"] == "refs/remotes/fork/master"
    assert [i["path"] for i in out["items"]] == ["doctrine-outbox/20260925-on-master.md"]


def test_debt_json_reports_ok_when_the_ledger_covers_the_ref(tmp_path, capsys):
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"
    key = ob.idempotency_key(git(src, "rev-parse", "HEAD"), "TRAPS.md", "### Example finding\nBody text describing the finding.\n")
    ob.append_ledger_rows(ledger, [{"key": key}])
    assert ob.main(["--repo", str(src), "debt", "--ref", "master", "--ledger", str(ledger), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "OK"


def test_debt_is_unknown_never_zero_when_the_ref_lacks_the_tool(tmp_path, capsys):
    src = init_source(tmp_path, with_tool=False)
    add_item(src, "20260925-example.md")
    code = ob.main(["--repo", str(src), "debt", "--ref", "master", "--ledger", str(tmp_path / "s.jsonl")])
    out = capsys.readouterr().out
    assert code == 2
    assert "UNKNOWN" in out and "TOOL_ABSENT_AT_REF" in out
    assert "no unsent" not in out


def test_debt_is_unknown_when_the_ref_lacks_the_items_directory(tmp_path, capsys):
    src = init_source(tmp_path)
    code = ob.main(["--repo", str(src), "debt", "--ref", "master", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["status"] == "UNKNOWN" and out["code"] == "TOOL_ABSENT_AT_REF"
    assert "doctrine-outbox/" in out["detail"]


def test_debt_is_unknown_when_the_default_ref_does_not_resolve(tmp_path, capsys):
    src = init_source(tmp_path)  # no `fork` remote: refs/remotes/fork/master does not exist
    add_item(src, "20260925-example.md")
    code = ob.main(["--repo", str(src), "debt", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 2 and out["code"] == "REF_UNRESOLVED"


def test_drain_refuses_a_ref_without_the_tool_and_pushes_nothing(tmp_path, capsys):
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path, with_tool=False)
    add_item(src, "20260925-example.md")
    before = git(bare, "rev-parse", "master")
    code = ob.main(["--repo", str(src), "drain", "--bus", str(clone), "--ref", "master", "--push",
                    "--ledger", str(tmp_path / "s.jsonl")])
    assert code == 2
    assert "TOOL_ABSENT_AT_REF" in capsys.readouterr().err
    assert git(bare, "rev-parse", "master") == before


def test_debt_with_a_bus_sees_a_marker_the_ledger_never_recorded(tmp_path, capsys):
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ob.drain(src, clone, "HEAD", tmp_path / "a.jsonl", [], push=True)
    args = ["--repo", str(src), "debt", "--ref", "master", "--ledger", str(tmp_path / "lost.jsonl")]
    assert ob.main(args) == 1  # without the bus, the lost ledger reads as debt
    capsys.readouterr()
    assert ob.main([*args, "--bus", str(clone)]) == 0


# ---------- committed-bytes-only visibility ----------

def test_working_tree_only_item_is_invisible(tmp_path):
    src = init_source(tmp_path)
    item_dir = src / "doctrine-outbox"
    item_dir.mkdir()
    (item_dir / "README.md").write_text("# doctrine-outbox\n", encoding="utf-8")
    git(src, "add", "doctrine-outbox/README.md")
    git(src, "commit", "-m", "readme")
    (item_dir / "20260925-uncommitted.md").write_text(item_text(), encoding="utf-8")
    assert ob.load_outbox_items(src, "HEAD") == []
    assert ob.main(["--repo", str(src), "debt", "--ref", "HEAD", "--ledger", str(tmp_path / "s.jsonl")]) == 0


def test_readme_in_outbox_dir_is_not_an_item(tmp_path):
    src = init_source(tmp_path)
    (src / "doctrine-outbox").mkdir()
    (src / "doctrine-outbox" / "README.md").write_text("# doctrine-outbox\n", encoding="utf-8")
    git(src, "add", "doctrine-outbox/README.md")
    git(src, "commit", "-m", "add readme")
    assert ob.load_outbox_items(src, "HEAD") == []


def test_pending_source_commit_resolves_to_adding_commit(tmp_path):
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    adding_commit = git(src, "rev-parse", "HEAD")
    items = ob.load_outbox_items(src, "HEAD")
    assert len(items) == 1 and items[0]["source_commit"] == adding_commit


def test_debt_command_respects_ledger(tmp_path):
    src = init_source(tmp_path)
    item = add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"
    argv = ["--repo", str(src), "debt", "--ref", "HEAD", "--ledger", str(ledger)]
    assert ob.main(argv) == 1
    parsed = ob.parse_item(item.read_text(encoding="utf-8"))
    key = ob.idempotency_key(git(src, "rev-parse", "HEAD"), parsed["meta"]["target"], parsed["body"])
    ob.append_ledger_rows(ledger, [{"key": key, "target": "TRAPS.md", "item": "20260925-example"}])
    assert ob.main(argv) == 0


# ---------- debt age: from the merge into the ref, not the branch commit ----------

def _merged_item_repo(tmp_path, branch_age_s: float, merge_age_s: float):
    src = init_source(tmp_path)
    git(src, "checkout", "-b", "feature")
    add_item(src, "20260925-example.md", env=dated(branch_age_s))
    git(src, "checkout", "master")
    git(src, "merge", "--no-ff", "feature", "-m", "Merge pull request #1 from feature", env=dated(merge_age_s))
    return src


def test_debt_age_counts_from_the_merge_not_the_branch_commit(tmp_path):
    day = 86400
    src = _merged_item_repo(tmp_path, branch_age_s=5 * day, merge_age_s=2 * 3600)
    result = ob.compute_debt(src, "master", tmp_path / "s.jsonl")
    assert result["count"] == 1
    assert 1.5 < result["oldest_age_hours"] < 3, "age must be ~2h since the merge, not ~120h since the branch commit"
    assert result["stale_over_24h"] is False


def test_debt_older_than_24h_since_the_merge_is_stale(tmp_path):
    day = 86400
    src = _merged_item_repo(tmp_path, branch_age_s=6 * day, merge_age_s=3 * day)
    result = ob.compute_debt(src, "master", tmp_path / "s.jsonl")
    assert 70 < result["oldest_age_hours"] < 74
    assert result["stale_over_24h"] is True


def test_debt_age_of_a_direct_first_parent_commit_is_its_own_time(tmp_path):
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md", env=dated(30 * 3600))
    result = ob.compute_debt(src, "master", tmp_path / "s.jsonl")
    assert 29 < result["oldest_age_hours"] < 31 and result["stale_over_24h"] is True


# ---------- check-ledger: structured findings need a disposition ----------

LEDGER_TEXT = """# Subject ledger

## SUBJECT 1
A hub error, disclosed; the root cause was a TRAP in the launcher. See KF-9 above.

## KERNEL FINDINGS from this attempt (file to the bus at the next seam)

- **KF-1 -- first finding.** It happened.
  Doctrine-Export: outbox 20260929-first-finding.md
- **KF-2 -- second finding.** It also happened.

## SUBJECT 2
Finding: KF-3 tagged outside any section
Doctrine-Export: none the fix is product internal only
"""


def _ledger(tmp_path, text: str = LEDGER_TEXT) -> Path:
    path = tmp_path / "subject-ledger.md"
    path.write_bytes(text.encode("utf-8"))
    return path


def test_check_ledger_flags_only_the_undisposed_finding(tmp_path):
    code, lines = ob.check_ledger(_ledger(tmp_path), tmp_path / "wm.json")
    text = "\n".join(lines)
    assert code == 1
    assert "UNDISPOSED KF-2" in text
    assert "UNDISPOSED KF-1" not in text and "UNDISPOSED KF-3" not in text
    assert "3 finding(s)" in text and "1 undisposed" in text


def test_check_ledger_ignores_a_kf_mentioned_mid_line(tmp_path):
    code, lines = ob.check_ledger(_ledger(tmp_path), tmp_path / "wm.json")
    assert "KF-9" not in "\n".join(lines)


def test_keyword_hits_are_advisory_never_failing(tmp_path):
    """M3. 'hub error', 'root cause' and 'TRAP' say "look here"; only a structured finding gate fails."""
    ledger = _ledger(tmp_path, "# L\n\n## S\nHub error, disclosed. Root cause found. A TRAP.\n")
    code, lines = ob.check_ledger(ledger, tmp_path / "wm.json")
    text = "\n".join(lines)
    assert code == 0, text
    assert text.count("ADVISORY") == 1
    disposed = _ledger(tmp_path, LEDGER_TEXT.replace("It also happened.\n", "It also happened.\n  Doctrine-Export: none nothing fleet relevant here\n"))
    code, lines = ob.check_ledger(disposed, tmp_path / "wm.json")
    assert code == 0, "\n".join(lines)
    assert "ADVISORY" in "\n".join(lines)


@pytest.mark.parametrize("line,why", [
    ("Doctrine-Export: none n/a", "DISPOSITION_NONE_NEEDS_REASON_OF_4_WORDS"),
    ("Doctrine-Export: none", "DISPOSITION_NONE_NEEDS_REASON_OF_4_WORDS"),
    ("Doctrine-Export: outbox", "DISPOSITION_OUTBOX_NEEDS_ITEM_FILE"),
    ("Doctrine-Export: outbox somefile", "DISPOSITION_OUTBOX_NEEDS_ITEM_FILE"),
])
def test_a_weak_or_malformed_disposition_does_not_count(tmp_path, line, why):
    ledger = _ledger(tmp_path, f"## KERNEL FINDINGS\n\n- **KF-1 -- f.** x\n  {line}\n")
    code, lines = ob.check_ledger(ledger, tmp_path / "wm.json")
    assert code == 1 and why in "\n".join(lines)


def test_a_disposition_before_the_finding_or_after_the_next_one_does_not_count(tmp_path):
    ledger = _ledger(tmp_path, "## KERNEL FINDINGS\n\nDoctrine-Export: none stray line before any finding\n"
                               "- **KF-1 -- a.** x\n- **KF-2 -- b.** y\n  Doctrine-Export: none belongs to the second one only\n")
    code, lines = ob.check_ledger(ledger, tmp_path / "wm.json")
    text = "\n".join(lines)
    assert code == 1 and "UNDISPOSED KF-1" in text and "UNDISPOSED KF-2" not in text


def test_a_heading_ends_a_findings_span(tmp_path):
    ledger = _ledger(tmp_path, "## KERNEL FINDINGS\n- **KF-1 -- a.** x\n## NEXT\nDoctrine-Export: none but this is under another heading\n")
    code, _lines = ob.check_ledger(ledger, tmp_path / "wm.json")
    assert code == 1


def test_check_ledger_is_unknown_when_the_ledger_is_missing(tmp_path):
    code, lines = ob.check_ledger(tmp_path / "nope.md", tmp_path / "wm.json")
    assert code == 2 and "UNKNOWN" in lines[0]


def test_watermark_advances_only_on_disposition_and_detects_a_rewrite(tmp_path):
    ledger, wm = _ledger(tmp_path), tmp_path / "wm.json"
    code, lines = ob.check_ledger(ledger, wm, advance=True)
    assert code == 1
    stop = json.loads(wm.read_text(encoding="utf-8"))["offset"]
    data = ledger.read_bytes()
    assert data[stop:].startswith(b"- **KF-2"), "watermark must stop AT the first undisposed finding"

    # Dispose KF-2 (appended after its line): the next run advances to the end.
    ledger.write_bytes(data.replace(b"It also happened.\n", b"It also happened.\n  Doctrine-Export: none nothing fleet relevant here\n"))
    code, lines = ob.check_ledger(ledger, wm, advance=True)
    assert code == 0
    assert json.loads(wm.read_text(encoding="utf-8"))["offset"] == len(ledger.read_bytes())

    # A new undisposed finding after the watermark is the only thing reported.
    ledger.write_bytes(ledger.read_bytes() + b"\nFinding: KF-4 a brand new one\n")
    code, lines = ob.check_ledger(ledger, wm)
    text = "\n".join(lines)
    assert code == 1 and "UNDISPOSED KF-4" in text and "1 finding(s)" in text

    # Rewriting history under the watermark is detected and the scan restarts from 0.
    ledger.write_bytes(ledger.read_bytes().replace(b"# Subject ledger", b"# Subject LEDGER"))
    code, lines = ob.check_ledger(ledger, wm)
    assert "WATERMARK_PREFIX_MISMATCH" in "\n".join(lines)
    assert "4 finding(s)" in "\n".join(lines)


def test_check_ledger_without_advance_never_writes_the_watermark(tmp_path):
    ob.check_ledger(_ledger(tmp_path), tmp_path / "wm.json")
    assert not (tmp_path / "wm.json").exists()


def test_baseline_explicitly_waives_history_and_says_how_much(tmp_path):
    ledger, wm = _ledger(tmp_path), tmp_path / "wm.json"
    code, lines = ob.check_ledger(ledger, wm, baseline=True)
    assert code == 0 and "1 undisposed historical finding(s) explicitly waived" in lines[0]
    code, lines = ob.check_ledger(ledger, wm)
    assert code == 0 and "0 finding(s)" in "\n".join(lines)


def test_advance_stops_at_the_last_complete_line(tmp_path):
    ledger, wm = _ledger(tmp_path, "## S\nplain text\nhalf a li"), tmp_path / "wm.json"
    ob.check_ledger(ledger, wm, advance=True)
    assert json.loads(wm.read_text(encoding="utf-8"))["offset"] == len(b"## S\nplain text\n")


def test_check_ledger_cli_path_precedence_flag_over_env_over_default(tmp_path, monkeypatch, capsys):
    env_ledger = tmp_path / "env.md"
    env_ledger.write_text("## KERNEL FINDINGS\n- **KF-7 -- env.** x\n", encoding="utf-8")
    flag_ledger = tmp_path / "flag.md"
    flag_ledger.write_text("# nothing\n", encoding="utf-8")
    wm = tmp_path / "wm.json"
    monkeypatch.setenv(ob.SUBJECT_LEDGER_ENV, str(env_ledger))
    assert ob.main(["--repo", str(tmp_path), "check-ledger", "--watermark", str(wm)]) == 1
    assert "KF-7" in capsys.readouterr().out
    assert ob.main(["--repo", str(tmp_path), "check-ledger", "--ledger", str(flag_ledger), "--watermark", str(wm)]) == 0
    monkeypatch.delenv(ob.SUBJECT_LEDGER_ENV)
    assert ob.main(["--repo", str(tmp_path), "check-ledger", "--watermark", str(wm)]) == 2  # default path absent


def test_check_ledger_watermark_env_override(tmp_path, monkeypatch):
    ledger, wm = _ledger(tmp_path), tmp_path / "from-env.json"
    monkeypatch.setenv(ob.WATERMARK_ENV, str(wm))
    ob.main(["--repo", str(tmp_path), "check-ledger", "--ledger", str(ledger), "--baseline"])
    assert wm.is_file()


# ---------- check-commits: the trailer check ----------

def _commit(src: Path, files: dict[str, str], message: str) -> str:
    for rel, content in files.items():
        path = src / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(src, "add", rel)
    git(src, "commit", "-m", message)
    return git(src, "rev-parse", "HEAD")


def _range_repo(tmp_path):
    src = init_source(tmp_path)
    return src, git(src, "rev-parse", "HEAD")


@pytest.mark.parametrize("path", ["tools/coordination/x.ps1", "agents/x.md", ".claude/x.json", "CLAUDE.md"])
def test_a_finding_path_commit_without_a_trailer_fails(tmp_path, path):
    src, base = _range_repo(tmp_path)
    _commit(src, {path: "x\n"}, "touch a finding path")
    failures = ob.check_commits(src, f"{base}..HEAD", [])
    assert [p for ps in failures.values() for p in ps][0].startswith("MISSING_DOCTRINE_EXPORT")


def _quotepath_repo(tmp_path):
    """A scratch repo with git's DEFAULT path quoting pinned on, whatever the host's global config says."""
    src, base = _range_repo(tmp_path)
    git(src, "config", "core.quotepath", "true")
    return src, base


@pytest.mark.parametrize("path", ["agents/café.md", "tools/coordination/日本.ps1", ".claude/naïve.json"])
def test_a_non_ascii_finding_path_commit_without_a_trailer_fails(tmp_path, path):
    """git prints `agents/caf\\303\\251.md` quoted by default, which matches no prefix: bus de09cae."""
    src, base = _quotepath_repo(tmp_path)
    _commit(src, {path: "x\n"}, "touch a non-ASCII finding path")
    failures = ob.check_commits(src, f"{base}..HEAD", [])
    assert [p for ps in failures.values() for p in ps][0].startswith("MISSING_DOCTRINE_EXPORT")


def test_the_ascii_control_for_the_non_ascii_finding_path_fails_too(tmp_path):
    src, base = _quotepath_repo(tmp_path)
    _commit(src, {"agents/cafe.md": "x\n"}, "touch an ASCII finding path")
    failures = ob.check_commits(src, f"{base}..HEAD", [])
    assert [p for ps in failures.values() for p in ps][0].startswith("MISSING_DOCTRINE_EXPORT: touches agents/cafe.md")


def test_the_missing_trailer_message_names_the_real_non_ascii_path(tmp_path):
    src, base = _quotepath_repo(tmp_path)
    _commit(src, {"agents/café.md": "x\n"}, "touch a non-ASCII finding path")
    failures = ob.check_commits(src, f"{base}..HEAD", [])
    assert [p for ps in failures.values() for p in ps][0] == "MISSING_DOCTRINE_EXPORT: touches agents/café.md"


def test_a_non_ascii_outbox_item_name_is_refused_not_silently_skipped(tmp_path):
    """The quoted form began with a `"`, so the drain's prefix test dropped the item without a word."""
    src = init_source(tmp_path)
    git(src, "config", "core.quotepath", "true")
    _commit(src, {"doctrine-outbox/20260929-café-item.md": item_text()}, "add a non-ASCII item name")
    items = ob.load_outbox_items(src, "HEAD")
    assert [i["name"] for i in items] == ["20260929-café-item.md"]
    assert items[0]["error"].startswith("ITEM_BAD_FILENAME")


def test_a_non_ascii_outbox_item_added_in_range_is_seen_by_the_range_scan(tmp_path):
    src, base = _quotepath_repo(tmp_path)
    _commit(src, {"doctrine-outbox/20260929-café-item.md": item_text()}, "add a non-ASCII item name")
    assert ob.range_added_items(src, f"{base}..HEAD") == {"doctrine-outbox/20260929-café-item.md"}


def test_a_non_finding_path_commit_needs_no_trailer(tmp_path):
    src, base = _range_repo(tmp_path)
    _commit(src, {"src/foo.cpp": "x\n", "docs/y.md": "y\n"}, "ordinary product commit")
    assert ob.check_commits(src, f"{base}..HEAD", []) == {}


def test_none_with_a_real_reason_passes_and_a_short_one_fails(tmp_path):
    src, base = _range_repo(tmp_path)
    _commit(src, {"agents/a.md": "a\n"}, "ok\n\nDoctrine-Export: none tooling change with no fleet lesson")
    assert ob.check_commits(src, f"{base}..HEAD", []) == {}
    _commit(src, {"agents/b.md": "b\n"}, "bad\n\nDoctrine-Export: none n/a")
    failures = ob.check_commits(src, f"{base}..HEAD", [])
    assert len(failures) == 1
    assert "NEEDS_REASON_OF_4_WORDS" in next(iter(failures.values()))[0]


def test_outbox_trailer_needs_an_item_added_in_the_range_that_passes_validation(tmp_path):
    src, base = _range_repo(tmp_path)
    _commit(src, {"agents/a.md": "a\n", "doctrine-outbox/20260929-real-item.md": item_text()},
            "good\n\nDoctrine-Export: outbox 20260929-real-item.md")
    assert ob.check_commits(src, f"{base}..HEAD", []) == {}

    _commit(src, {"agents/b.md": "b\n"}, "cites nothing\n\nDoctrine-Export: outbox 20260929-not-there.md")
    _commit(src, {"agents/c.md": "c\n", "doctrine-outbox/20260929-leaky-item.md": item_text(body="### h\nmail x@example.com\n")},
            "leaky\n\nDoctrine-Export: outbox doctrine-outbox/20260929-leaky-item.md")
    _commit(src, {"agents/d.md": "d\n"}, "no item at all\n\nDoctrine-Export: outbox")
    failures = ob.check_commits(src, f"{base}..HEAD", [])
    reasons = sorted(p.split(":")[0] for ps in failures.values() for p in ps)
    assert reasons == ["DOCTRINE_EXPORT_OUTBOX_ITEM_INVALID", "DOCTRINE_EXPORT_OUTBOX_ITEM_NOT_ADDED_IN_RANGE",
                       "DOCTRINE_EXPORT_OUTBOX_NEEDS_ITEM_FILE"]


def test_a_garbled_trailer_fails_even_on_a_non_finding_path(tmp_path):
    src, base = _range_repo(tmp_path)
    _commit(src, {"src/a.cpp": "a\n"}, "garbled\n\nDoctrine-Export: maybe later")
    failures = ob.check_commits(src, f"{base}..HEAD", [])
    assert next(iter(failures.values()))[0].startswith("BAD_DOCTRINE_EXPORT")


def test_merge_commits_are_not_checked_but_their_branch_commits_are(tmp_path):
    src, base = _range_repo(tmp_path)
    git(src, "checkout", "-b", "feature")
    _commit(src, {"agents/a.md": "a\n"}, "branch commit\n\nDoctrine-Export: none tooling change with no fleet lesson")
    git(src, "checkout", "master")
    _commit(src, {"src/m.cpp": "m\n"}, "master moves on")
    git(src, "merge", "--no-ff", "feature", "-m", "Merge pull request #2 from feature")
    assert ob.check_commits(src, f"{base}..HEAD", []) == {}


def test_an_unresolvable_range_is_unknown_not_a_pass(tmp_path, capsys):
    src = init_source(tmp_path)
    assert ob.main(["--repo", str(src), "check-commits"]) == 2  # default refs/remotes/fork/master absent
    assert "RANGE_UNRESOLVED" in capsys.readouterr().err


def test_check_commits_cli_reports_failures_on_stderr(tmp_path, capsys):
    src, base = _range_repo(tmp_path)
    _commit(src, {"CLAUDE.md": "x\n"}, "touch")
    assert ob.main(["--repo", str(src), "check-commits", "--range", f"{base}..HEAD"]) == 1
    assert "MISSING_DOCTRINE_EXPORT" in capsys.readouterr().err


@pytest.mark.skipif(os.environ.get("GITHUB_EVENT_NAME") != "pull_request",
                    reason="runs only on a hosted pull_request event, where the PR's commit range is known")
def test_pull_request_commits_declare_doctrine_export():
    """The CI seam for the trailer check: every non-merge commit of THIS pull request that touches a
    finding path (tools/coordination/**, agents/**, .claude/**, CLAUDE.md) carries a valid
    `Doctrine-Export:` trailer. An unresolvable range raises, which fails the test."""
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
    base, head = event["pull_request"]["base"]["sha"], event["pull_request"]["head"]["sha"]
    failures = ob.check_commits(REPO_ROOT, f"{base}..{head}", [])
    assert failures == {}, "\n".join(f"{sha[:12]}: {'; '.join(p)}" for sha, p in failures.items())


# ---------- validate CLI ----------

def test_validate_cli_ok_and_refused(tmp_path, capsys):
    good = tmp_path / "20260929-good-item.md"
    good.write_text(item_text(body="### heading\nbody\n"), encoding="utf-8")
    bad = tmp_path / "20260929-bad-item.md"
    bad.write_text("not front matter\n", encoding="utf-8")
    assert ob.main(["--repo", str(tmp_path), "validate", str(good)]) == 0
    assert ob.main(["--repo", str(tmp_path), "validate", str(bad)]) == 1
    assert "ITEM_NO_FRONT_MATTER" in capsys.readouterr().err


def test_validate_refuses_a_filename_the_drain_would_refuse_forever(tmp_path, capsys):
    """A slug over the limit passed validate but was refused by every drain as ITEM_BAD_FILENAME;
    found by the shipped-items test against the staged backfill (five of them were over)."""
    too_long = tmp_path / ("20260929-" + "a" * 62 + ".md")
    too_long.write_text(item_text(body="### heading\nbody\n"), encoding="utf-8")
    assert ob.main(["--repo", str(tmp_path), "validate", str(too_long)]) == 1
    assert "ITEM_BAD_FILENAME" in capsys.readouterr().err
    src = init_source(tmp_path, "namesrc")
    item_dir = src / "doctrine-outbox"
    item_dir.mkdir()
    (item_dir / too_long.name).write_text(item_text(body="### heading\nbody\n"), encoding="utf-8")
    git(src, "add", "doctrine-outbox")
    git(src, "commit", "-m", "long name")
    assert [i["error"].split(":")[0] for i in ob.load_outbox_items(src, "HEAD")] == ["ITEM_BAD_FILENAME"]


def test_validate_refuses_short_identity_via_cli(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ob.socket, "gethostname", lambda: "ab")
    monkeypatch.delenv("COMPUTERNAME", raising=False)
    monkeypatch.setenv("USERNAME", "ab")
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    good = tmp_path / "20260929-good-item.md"
    good.write_text(item_text(body="### heading\nbody\n"), encoding="utf-8")
    assert ob.main(["--repo", str(tmp_path), "validate", str(good)]) == 1
    err = capsys.readouterr().err
    assert "SHORT_IDENTITY_UNSCREENABLE" in err and "ab" not in err


# ---------- the sent ledger and watermark never travel ----------

def test_gitignore_excludes_the_outbox_state_files_portably(tmp_path):
    """Present in the TRACKED .gitignore, not only a local exclude: proved in a brand new repo
    holding only that file."""
    clean = tmp_path / "gitignore-check-repo"
    clean.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(clean)], check=True, capture_output=True, text=True)
    (clean / ".gitignore").write_bytes((REPO_ROOT / ".gitignore").read_bytes())
    for rel in (ob.LEDGER_REL, ob.WATERMARK_REL, ob.DENY_FILE_REL):
        result = subprocess.run(["git", "-C", str(clean), "check-ignore", "-q", str(rel).replace("\\", "/")],
                                capture_output=True, text=True)
        assert result.returncode == 0, f"tracked .gitignore does not exclude {rel}"


def test_shipped_backfill_items_all_validate_and_screen_clean():
    """Every item committed under doctrine-outbox/ in this checkout parses and passes the Law-4
    screen, so the first drain cannot refuse one."""
    items = sorted(p for p in (REPO_ROOT / "doctrine-outbox").glob("*.md") if p.name != "README.md")
    for path in items:
        assert ob.ITEM_NAME_RE.match(path.name), path.name
        parsed = ob.parse_item(path.read_text(encoding="utf-8"))
        ob.screen_law4(parsed["body"], [])
        ob.screen_law4(path.name, [])
        assert path.read_text(encoding="utf-8").startswith("---\n") and "published_as" not in parsed["meta"]


# ---------- drain: acknowledgement and verification races (sol pre-review B1, B2) ----------
#
# Named mutations for this block (each turns the named test red, then is reverted):
#   M4  ignore that our own commit is on the refetched tip  -> test_lost_push_ack_that_landed_is_reported_published
#   M5  restore ls-remote equality (`remote_sha != head`)   -> test_ls_remote_descendant_of_the_pushed_commit_is_a_success

def _competitor_push(bare: Path, tmp_path: Path, tag: str) -> str:
    """Land one unrelated commit on the bus origin from a second clone; return its sha."""
    clone = tmp_path / f"competitor-{tag}"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True, text=True)
    current = (clone / "TRAPS.md").read_bytes()
    addition = f"### Competitor {tag}\nbody\n<!-- outbox:{tag * 16} other:0123456789ab -->\n"
    (clone / "TRAPS.md").write_bytes(current + addition.encode("utf-8"))
    git(clone, "add", "TRAPS.md")
    git(clone, "commit", "-m", f"competing entry {tag}")
    git(clone, "push", "origin", "HEAD:refs/heads/master")
    return git(clone, "rev-parse", "HEAD")


def _ledger_rows(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_lost_push_ack_that_landed_is_reported_published(tmp_path, monkeypatch):
    """B1. The bus accepted the push but the acknowledgement came back as a failure. The retry
    refetches, finds the key on the tip, and our own commit is an ancestor of that tip: that is
    a landed publication, not `pushed:false, published:[]`."""
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"
    real_git_try, calls = ob.git_try, {"pushes": 0}

    def lossy_git_try(repo, *args, **kw):
        if args and args[0] == "push":
            calls["pushes"] += 1
            rc, out, err = real_git_try(repo, *args, **kw)
            assert rc == 0, err
            if calls["pushes"] == 1:
                return 1, "", "simulated lost acknowledgement"
            return rc, out, err
        return real_git_try(repo, *args, **kw)

    monkeypatch.setattr(ob, "git_try", lossy_git_try)
    report = ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert calls["pushes"] == 1, "the retry must find the landed key, not push again"
    assert report["pushed"] is True
    assert len(report["published"]) == 1
    assert report["already_sent"] == []
    rows = _ledger_rows(ledger)
    assert [r["key"] for r in rows] == [report["published"][0]["key"]]
    assert rows[0]["bus_commit"] == report["published"][0]["bus_commit"]
    assert git_raw(clone, "show", "origin/master:TRAPS.md").count(b"outbox:") == 1


def test_a_key_landed_by_another_publisher_is_already_sent_not_published(tmp_path, monkeypatch):
    """The other half of B1: the push really was rejected and a different publisher landed the
    exact same block. Our commit is NOT on the tip, so it is already_sent, never `published`."""
    bare, clone = init_bus(tmp_path)
    rival_clone = tmp_path / "rival-bus-clone"
    subprocess.run(["git", "clone", str(bare), str(rival_clone)], check=True, capture_output=True, text=True)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    real_git_try, state = ob.git_try, {"armed": True}

    def rejecting_git_try(repo, *args, **kw):
        if args and args[0] == "push" and state["armed"]:
            state["armed"] = False
            # The rival builds the same block on the same tip with the same pinned identity, so
            # within one clock second its commit would be byte-identical to ours -- and an
            # identical commit IS ours. Date it differently so it is a different publisher's.
            with monkeypatch.context() as m:
                m.setenv("GIT_AUTHOR_DATE", "2001-01-01T00:00:00Z")
                m.setenv("GIT_COMMITTER_DATE", "2001-01-01T00:00:00Z")
                rival = ob.drain(src, rival_clone, "HEAD", tmp_path / "rival-sent.jsonl", [], push=True)
            assert rival["pushed"] is True
            return 1, "", "rejected: non-fast-forward"
        return real_git_try(repo, *args, **kw)

    monkeypatch.setattr(ob, "git_try", rejecting_git_try)
    ledger = tmp_path / "sent.jsonl"
    report = ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert report["published"] == [] and report["pushed"] is False
    assert [a["via"] for a in report["already_sent"]] == ["bus_tip"]
    assert git_raw(clone, "show", "origin/master:TRAPS.md").count(b"outbox:") == 1


def test_ls_remote_descendant_of_the_pushed_commit_is_a_success(tmp_path, monkeypatch):
    """B2. Another publisher advances the bus between our push and our ls-remote. Our commit is
    an ancestor of the remote tip, so the publication landed; equality would call it a failure."""
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"
    real_git = ob.git

    def racing_git(repo, *args, **kw):
        if args and args[0] == "ls-remote":
            _competitor_push(bare, tmp_path, "b")
        return real_git(repo, *args, **kw)

    monkeypatch.setattr(ob, "git", racing_git)
    report = ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert report["pushed"] is True and len(report["published"]) == 1
    assert git(bare, "rev-parse", "master") != report["commit"], "the competitor must have moved the tip"
    assert len(_ledger_rows(ledger)) == 1
    assert git_raw(bare, "show", "master:TRAPS.md").count(b"outbox:") == 2


def test_ls_remote_tip_that_lacks_the_pushed_commit_still_fails_verification(tmp_path, monkeypatch):
    """B2's guard: verification fails only when the pushed commit is genuinely not on the remote."""
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    ledger = tmp_path / "sent.jsonl"
    real_git_try = ob.git_try

    def phantom_git_try(repo, *args, **kw):
        if args and args[0] == "push":
            return 0, "", ""  # claims success, pushes nothing
        return real_git_try(repo, *args, **kw)

    monkeypatch.setattr(ob, "git_try", phantom_git_try)
    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert excinfo.value.code == "PUSH_VERIFY_FAILED"
    assert not ledger.exists()


# ---------- hardening: refusal details name the class, never the matched value ----------

@pytest.mark.parametrize("name", list(LAW4_SAMPLES))
def test_law4_refusal_detail_is_the_class_alone(name):
    with pytest.raises(ob.Refusal) as excinfo:
        ob.screen_law4(LAW4_SAMPLES[name])
    assert excinfo.value.detail == name


def test_law4_refusal_does_not_echo_a_home_path_or_a_token():
    cases = (("### h\nfound at /Users/OtherPerson/notes\n", "OtherPerson"),
             ("### h\ntoken ghp_a1B2c3D4e5F6g7H8i9J0k1L2m3 was pasted\n", "ghp_a1B2"))
    for body, leaked in cases:
        with pytest.raises(ob.Refusal) as excinfo:
            ob.screen_law4(body)
        assert leaked not in str(excinfo.value)


# ---------- R14.1: a new trap filing is a card (bus RULINGS.md R14, packet 2 at 0c78890) ----------
#
# Named mutations for this block (each turns the named test red, then is reverted):
#   M6  make card_validator_on_bus() always return False  -> test_r14_trap_to_traps_md_is_refused_once_the_bus_carries_the_validator
#   M7  skip validate_cards_file() in drain                -> test_r14_card_the_validator_rejects_is_never_pushed
#   M8  re-raise the CARD_INVALID instead of refusing per card -> test_r14_card_the_validator_rejects_is_never_pushed, test_r14_one_invalid_card_is_refused_alone_and_the_rest_of_the_drain_publishes (the one whose docstring is M8) and test_r14_dry_run_also_reports_the_invalid_card_and_would_push_the_rest; the M9, M10, M11 and M12 tests below go red under it too
#   M9  judge each card alone (title + card) instead of tip + kept cards + card -> the two duplicate-id tests (M9 pending/pending, M10 pending/tip)
#   M11 the tip's own cards file is invalid / the per-card pass names nobody -> CARD_INVALID_UNATTRIBUTED for the set, never a raise
#   M12 delete the `candidates = [...]` filter after a per-card refusal -> CARD_PASS_NOT_SHRINKING (a Refusal, never a hang)

CARDS = "specs/mlv-app/cards.md"
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "bus-validate-cards"
CARD_BODY = ("## mlv-app/example-card\n"
             "rule: the rule\n"
             "mechanism: the mechanism\n"
             "check: py -3 -m pytest tests/coordination -q\n"
             "supersedes: none\n"
             "evidence: measured\n")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")


def add_validator(clone: Path) -> str:
    """Land R14 packet 2 on the fixture bus: tools/validate-cards.mjs (a byte copy of the bus's own)
    and a specs/mlv-app.md, so mlv-app is a fleet member on the validator's roster."""
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "--detach", "origin/master")
    (clone / "tools").mkdir(exist_ok=True)
    for name in ("validate-cards.mjs", "fleet-membership.mjs"):
        shutil.copyfile(FIXTURE_DIR / name, clone / "tools" / name)
    (clone / "specs").mkdir(exist_ok=True)
    (clone / "specs" / "mlv-app.md").write_text("# mlv-app\n", encoding="utf-8")
    git(clone, "add", "tools", "specs")
    git(clone, "commit", "-m", "R14 packet 2: card validator")
    git(clone, "push", "origin", "HEAD:refs/heads/master")
    return git(clone, "rev-parse", "HEAD")


def run_validator(checkout: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["node", str(checkout / "tools" / "validate-cards.mjs"), "--bus", str(checkout),
                           "--file", str(checkout / CARDS), "--project", "mlv-app"],
                          capture_output=True, text=True, timeout=60)


def test_vendored_validator_is_byte_identical_to_the_bus_blobs():
    assert git(REPO_ROOT, "hash-object", str(FIXTURE_DIR / "validate-cards.mjs")) == "14c50e976cab9eb8faa9ca7e65a9f9d7bbee0fd3"
    assert git(REPO_ROOT, "hash-object", str(FIXTURE_DIR / "fleet-membership.mjs")) == "e325e616d2b0ddcb16da42a7fe2d1d1c6a9b8cf0"


def test_r14_cards_target_is_the_projects_own_cards_file_and_takes_only_a_card():
    assert ob.CARDS_TARGET == CARDS and CARDS in ob.TARGETS
    item = ob.parse_item(item_text(target=CARDS, body=CARD_BODY))
    assert ob.item_heading(item) == "## mlv-app/example-card"
    with pytest.raises(ob.Refusal) as excinfo:
        ob.parse_item(item_text(target=CARDS, body="### A TRAPS.md-style entry\nbody\n"))
    assert excinfo.value.code == "ITEM_BODY_NOT_A_CARD"
    with pytest.raises(ob.Refusal) as excinfo:
        ob.parse_item(item_text(target=CARDS, body=CARD_BODY.replace("## mlv-app/", "## agent-bridge/")))
    assert excinfo.value.code == "ITEM_BODY_NOT_A_CARD"


def test_r14_trap_to_traps_md_is_refused_once_the_bus_carries_the_validator(tmp_path, capsys):
    """M6. R14.1 took effect at the bus commit that landed tools/validate-cards.mjs: a new trap
    filing to TRAPS.md is refused, by name, and nothing reaches the bus."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-example.md")
    ledger = tmp_path / "sent.jsonl"
    before = git(bare, "rev-parse", "master")

    report = ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [
        ("doctrine-outbox/20261007-example.md", "R14_1_TRAP_FILING_IS_A_CARD")]
    assert "R14.1" in report["refused"][0]["detail"] and CARDS in report["refused"][0]["detail"]
    assert report["published"] == [] and report["pushed"] is False
    assert git(bare, "rev-parse", "master") == before
    assert not ledger.exists()

    rc = ob.main(["--repo", str(src), "drain", "--bus", str(clone), "--ref", "HEAD", "--ledger", str(ledger), "--push"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "R14_1_TRAP_FILING_IS_A_CARD" in err and "R14.1" in err
    assert git(bare, "rev-parse", "master") == before


def test_r14_guard_is_keyed_on_the_bus_ref_not_the_clock(tmp_path):
    """The same kind of item, at the same wall-clock time: published to TRAPS.md while the bus
    ref lacks the validator (today's behaviour), refused once the bus ref carries it. An entry
    already on the bus stays already-sent, never refused, even with a lost ledger."""
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20261007-before.md", body="### Before the validator\nbody one\n")

    first = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert first["refused"] == []
    assert [p["target"] for p in first["published"]] == ["TRAPS.md"]

    add_validator(clone)
    add_item(src, "20261007-after.md", body="### After the validator\nbody two\n")
    second = ob.drain(src, clone, "HEAD", tmp_path / "sent-lost.jsonl", [], push=True)

    assert [(a["path"], a["via"]) for a in second["already_sent"]] == [("doctrine-outbox/20261007-before.md", "bus_tip")]
    assert [(r["path"], r["code"]) for r in second["refused"]] == [
        ("doctrine-outbox/20261007-after.md", "R14_1_TRAP_FILING_IS_A_CARD")]
    assert b"After the validator" not in git_raw(clone, "show", "origin/master:TRAPS.md")


@needs_node
def test_r14_trap_card_is_rendered_in_the_validator_format_and_published(tmp_path):
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-card-one.md", target=CARDS, body=CARD_BODY)
    add_item(src, "20261007-card-two.md", target=CARDS,
             body=CARD_BODY.replace("example-card", "second-card").replace("rule: the rule", "- **Rule:** a bold-bullet rule"))
    traps_before = git_raw(clone, "show", "origin/master:TRAPS.md")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert report["refused"] == []
    assert [p["target"] for p in report["published"]] == [CARDS, CARDS]
    text = git_raw(clone, "show", f"{report['commit']}:{CARDS}").decode("utf-8")
    assert text.startswith("# mlv-app cards (R14.1; written only by mlv-app)\n\n## mlv-app/example-card\n")
    for p in report["published"]:
        assert text.count(f"- **Outbox:** outbox:{p['key']} mlv-app:") == 1
    assert "-->" not in text, "an HTML marker line is not a card field and the validator refuses it"
    assert git_raw(clone, "show", f"{report['commit']}:TRAPS.md") == traps_before
    assert not (clone / CARDS).exists(), "the bus clone's own working tree is never written"

    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", str(bare), str(check)], check=True, capture_output=True, text=True)
    result = run_validator(check)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 card(s), 0 no-card line(s), 0 problem(s)" in result.stdout

    again = ob.drain(src, clone, "HEAD", tmp_path / "sent-lost.jsonl", [], push=True)
    assert again["published"] == [] and [a["via"] for a in again["already_sent"]] == ["bus_tip", "bus_tip"]


@needs_node
def test_r14_card_the_validator_rejects_is_never_pushed(tmp_path):
    """M7. The would-be cards file is validated by the bus tip's validate-cards.mjs in the drain's
    temp worktree before any push; a rejection refuses the publish."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-bad-card.md", target=CARDS, body=CARD_BODY.replace("evidence: measured\n", ""))
    before = git(bare, "rev-parse", "master")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [r["code"] for r in report["refused"]] == ["CARD_INVALID"]
    assert "missing field 'evidence'" in report["refused"][0]["detail"]
    assert report["published"] == [] and report["pushed"] is False
    assert git(bare, "rev-parse", "master") == before


OVERSIZED_CARD_BODY = CARD_BODY.replace("example-card", "oversized-card").replace("rule: the rule", "rule: " + "x" * 2100)


@needs_node
def test_r14_one_invalid_card_is_refused_alone_and_the_rest_of_the_drain_publishes(tmp_path, capsys):
    """M8. The cards file is validated for the whole batch; a rejection used to raise out of drain()
    and hold every other pending item. Now each card is judged on its own: the bad one is refused
    (CARD_INVALID, with the validator's message) and the valid card and the receipt still land."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-oversized.md", target=CARDS, body=OVERSIZED_CARD_BODY)
    add_item(src, "20261007-b-good-card.md", target=CARDS, body=CARD_BODY)
    add_item(src, "20261007-c-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")
    ledger = tmp_path / "sent.jsonl"

    report = ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [("doctrine-outbox/20261007-a-oversized.md", "CARD_INVALID")]
    assert "oversized-card" in report["refused"][0]["detail"] and "bytes" in report["refused"][0]["detail"]
    assert sorted((p["path"], p["target"]) for p in report["published"]) == [
        ("doctrine-outbox/20261007-b-good-card.md", CARDS), ("doctrine-outbox/20261007-c-receipt.md", "RECEIPTS.md")]
    assert report["pushed"] is True
    cards = git_raw(clone, "show", f"{report['commit']}:{CARDS}").decode("utf-8")
    assert "## mlv-app/example-card" in cards and "oversized-card" not in cards
    assert b"A receipt" in git_raw(clone, "show", f"{report['commit']}:RECEIPTS.md")
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", str(bare), str(check)], check=True, capture_output=True, text=True)
    result = run_validator(check)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 card(s), 0 no-card line(s), 0 problem(s)" in result.stdout
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2, "only what landed is ledgered"

    # The refused card is never retried by this ledger, and the CLI exit stays non-zero while it is pending.
    rc = ob.main(["--repo", str(src), "drain", "--bus", str(clone), "--ref", "HEAD", "--ledger", str(ledger), "--push"])
    captured = capsys.readouterr()
    assert rc == 1 and "REFUSED doctrine-outbox/20261007-a-oversized.md: CARD_INVALID" in captured.err
    assert json.loads(captured.out)["published"] == []


@needs_node
def test_r14_dry_run_also_reports_the_invalid_card_and_would_push_the_rest(tmp_path):
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-oversized.md", target=CARDS, body=OVERSIZED_CARD_BODY)
    add_item(src, "20261007-b-good-card.md", target=CARDS, body=CARD_BODY)
    before = git(bare, "rev-parse", "master")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)

    assert [r["code"] for r in report["refused"]] == ["CARD_INVALID"]
    assert [w["path"] for w in report["would_push"]] == ["doctrine-outbox/20261007-b-good-card.md"]
    assert report["pushed"] is False and git(bare, "rev-parse", "master") == before


def add_tip_cards(clone: Path, text: str) -> None:
    """Put a cards file on the fixture bus's tip (the cards that are already published)."""
    git(clone, "fetch", "-q", "origin")
    git(clone, "checkout", "-q", "--detach", "origin/master")
    (clone / "specs" / "mlv-app").mkdir(parents=True, exist_ok=True)
    (clone / CARDS).write_text(ob.CARDS_TITLE + text, encoding="utf-8", newline="\n")
    git(clone, "add", CARDS)
    git(clone, "commit", "-m", "tip cards")
    git(clone, "push", "origin", "HEAD:refs/heads/master")


SECOND_CARD_BODY = CARD_BODY.replace("example-card", "second-card")


@needs_node
def test_r14_a_pending_card_that_duplicates_a_pending_card_refuses_the_later_one_only(tmp_path):
    """M9. Two pending cards that pass alone but clash (duplicate id): the later-sorted is the one
    judged against (tip + the earlier pending card) and refused CARD_INVALID; the first, a third
    card and the receipt still publish. Nothing raises and nothing waits."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-card.md", target=CARDS, body=CARD_BODY)
    add_item(src, "20261007-b-card.md", target=CARDS, body=CARD_BODY.replace("the mechanism", "another mechanism"))
    add_item(src, "20261007-c-card.md", target=CARDS, body=SECOND_CARD_BODY)
    add_item(src, "20261007-d-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [("doctrine-outbox/20261007-b-card.md", "CARD_INVALID")]
    assert "duplicate card id" in report["refused"][0]["detail"]
    assert sorted(p["path"] for p in report["published"]) == [
        "doctrine-outbox/20261007-a-card.md", "doctrine-outbox/20261007-c-card.md", "doctrine-outbox/20261007-d-receipt.md"]
    cards = git_raw(clone, "show", f"{report['commit']}:{CARDS}").decode("utf-8")
    assert "the mechanism" in cards and "another mechanism" not in cards


@needs_node
def test_r14_a_pending_card_that_duplicates_a_card_already_on_the_tip_is_refused_alone(tmp_path, capsys):
    """M10. Sol's row: card X is already in the bus cards file; a pending card with the same id (a
    different body) passes a title-plus-card check but not tip-plus-card. It is refused per card,
    CARD_INVALID 'duplicate card id'; the valid card Y and the receipt publish; the exit stays 1."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    add_tip_cards(clone, CARD_BODY)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-dup-of-tip.md", target=CARDS, body=CARD_BODY.replace("the mechanism", "another mechanism"))
    add_item(src, "20261007-b-good-card.md", target=CARDS, body=SECOND_CARD_BODY)
    add_item(src, "20261007-c-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")
    ledger = tmp_path / "sent.jsonl"

    report = ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [("doctrine-outbox/20261007-a-dup-of-tip.md", "CARD_INVALID")]
    assert "duplicate card id" in report["refused"][0]["detail"]
    assert sorted((p["path"], p["target"]) for p in report["published"]) == [
        ("doctrine-outbox/20261007-b-good-card.md", CARDS), ("doctrine-outbox/20261007-c-receipt.md", "RECEIPTS.md")]
    assert report["pushed"] is True
    cards = git_raw(clone, "show", f"{report['commit']}:{CARDS}").decode("utf-8")
    assert cards.count("## mlv-app/example-card") == 1 and "another mechanism" not in cards and "## mlv-app/second-card" in cards
    assert b"A receipt" in git_raw(clone, "show", f"{report['commit']}:RECEIPTS.md")
    assert len(ledger.read_text(encoding="utf-8").splitlines()) == 2, "only what landed is ledgered"
    check = tmp_path / "check"
    subprocess.run(["git", "clone", "-q", str(bare), str(check)], check=True, capture_output=True, text=True)
    result = run_validator(check)
    assert result.returncode == 0, result.stdout + result.stderr

    rc = ob.main(["--repo", str(src), "drain", "--bus", str(clone), "--ref", "HEAD", "--ledger", str(ledger), "--push"])
    assert rc == 1 and "REFUSED doctrine-outbox/20261007-a-dup-of-tip.md: CARD_INVALID" in capsys.readouterr().err


@needs_node
def test_r14_a_cards_file_the_tip_already_fails_refuses_every_card_unattributed_and_still_ships_the_rest(tmp_path, capsys):
    """M11 (ruling B). The bus's own cards file is invalid, so no pending card can be blamed: every
    pending card is refused CARD_INVALID_UNATTRIBUTED (nothing is published into a file the
    validator rejects), the receipt still publishes, and the exit is non-zero."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    add_tip_cards(clone, CARD_BODY.replace("evidence: measured\n", ""))
    src = init_source(tmp_path)
    add_item(src, "20261007-a-good-card.md", target=CARDS, body=SECOND_CARD_BODY)
    add_item(src, "20261007-b-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")
    cards_before = git_raw(clone, "show", "origin/master:" + CARDS)

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [("doctrine-outbox/20261007-a-good-card.md", "CARD_INVALID_UNATTRIBUTED")]
    assert "missing field 'evidence'" in report["refused"][0]["detail"]
    assert [p["path"] for p in report["published"]] == ["doctrine-outbox/20261007-b-receipt.md"]
    git(clone, "fetch", "-q", "origin")
    assert git_raw(clone, "show", "origin/master:" + CARDS) == cards_before
    rc = ob.main(["--repo", str(src), "drain", "--bus", str(clone), "--ref", "HEAD", "--ledger", str(tmp_path / "sent2.jsonl"), "--push"])
    assert rc == 1 and "CARD_INVALID_UNATTRIBUTED" in capsys.readouterr().err


@needs_node
def test_r14_an_invalid_set_no_single_card_explains_refuses_all_of_it_unattributed(tmp_path, monkeypatch):
    """M11 (ruling B). The batch fails and the per-card pass can name nobody (forced here): every card
    of the set is refused CARD_INVALID_UNATTRIBUTED instead of raising, and the receipt still ships."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-card.md", target=CARDS, body=CARD_BODY)
    add_item(src, "20261007-b-card.md", target=CARDS, body=CARD_BODY.replace("the mechanism", "another mechanism"))
    add_item(src, "20261007-c-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")
    monkeypatch.setattr(ob, "invalid_cards", lambda wt, tip, cards: [])

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [
        ("doctrine-outbox/20261007-a-card.md", "CARD_INVALID_UNATTRIBUTED"),
        ("doctrine-outbox/20261007-b-card.md", "CARD_INVALID_UNATTRIBUTED")]
    assert "duplicate card id" in report["refused"][0]["detail"]
    assert [p["path"] for p in report["published"]] == ["doctrine-outbox/20261007-c-receipt.md"]
    assert CARDS not in git(clone, "ls-tree", "-r", "--name-only", report["commit"])


@needs_node
def test_r14_a_per_card_pass_that_removes_nothing_raises_instead_of_looping(tmp_path, monkeypatch):
    """M12. Each re-validation pass must remove at least one candidate. Here the per-card pass names a
    card that is not a candidate, so nothing shrinks: a typed Refusal, not a loop (the call counter
    bounds the test itself)."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-oversized.md", target=CARDS, body=OVERSIZED_CARD_BODY)
    before = git(bare, "rev-parse", "master")
    calls = []

    def stuck(wt, tip, cards):
        calls.append(1)
        assert len(calls) < 5, "the drain looped"
        return [(dict(cards[0], key="not-a-candidate"), "CARD_INVALID", "x")]
    monkeypatch.setattr(ob, "invalid_cards", stuck)

    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert excinfo.value.code == "CARD_PASS_NOT_SHRINKING" and len(calls) == 1
    assert git(bare, "rev-parse", "master") == before


@needs_node
def test_r14_a_validator_failure_is_not_a_card_verdict_and_refuses_the_whole_set(tmp_path, monkeypatch):
    """Only exit 1 (INVALID) is a per-card verdict. Any other validator outcome publishes nothing."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-good-card.md", target=CARDS, body=CARD_BODY)
    add_item(src, "20261007-b-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")
    before = git(bare, "rev-parse", "master")
    real_run, node = subprocess.run, ob.find_node()

    def run(cmd, *a, **kw):
        if cmd[0] == node:
            return subprocess.CompletedProcess(cmd, 2, "", "boom")
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(ob.subprocess, "run", run)

    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert excinfo.value.code == "CARD_VALIDATOR_FAILED"
    assert git(bare, "rev-parse", "master") == before


def fail_cards_blob_reads(monkeypatch, fails):
    """Make `git cat-file blob <ref>:<cards file>` exit 128 (what git prints for a transient read
    failure, and also for a missing path) whenever fails(n, ref) holds, n counting these reads from 1.
    Every other git call, including the `ls-tree` that shows the file IS there, runs for real."""
    real_run, seen = subprocess.run, []

    def run(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd[3:5] == ["cat-file", "blob"] and cmd[5].endswith(":" + CARDS):
            seen.append(cmd[5])
            if fails(len(seen), cmd[5].rsplit(":", 1)[0]):
                return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: unable to read blob object (simulated)")
        return real_run(cmd, *a, **kw)
    monkeypatch.setattr(ob.subprocess, "run", run)
    return seen


@needs_node
@pytest.mark.parametrize("fails", [
    pytest.param(lambda n, ref: True, id="every-read"),
    pytest.param(lambda n, ref: ref == "HEAD", id="the-committed-copy-only"),
    pytest.param(lambda n, ref: n >= 3, id="the-per-card-trial-only"),
])
def test_r14_a_cards_blob_that_is_present_at_the_tip_but_unreadable_is_a_typed_refusal_not_unattributed(tmp_path, monkeypatch, fails):
    """The tip carries example-card and a pending card duplicates it, so the batch is invalid and the
    per-card trial must start from the tip's cards file. When that read fails (transiently, here
    simulated at the git call) it used to come back as b"": the trial started from a bare title and
    every card was refused CARD_INVALID_UNATTRIBUTED, blaming the cards for a read error. Now the drain
    stops on TIP_BLOB_READ_FAILED naming the ref and path, whichever of the three reads failed, and
    publishes nothing."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    add_tip_cards(clone, CARD_BODY)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-dup-of-tip.md", target=CARDS, body=CARD_BODY.replace("the mechanism", "another mechanism"))
    add_item(src, "20261007-b-second-card.md", target=CARDS, body=SECOND_CARD_BODY)
    add_item(src, "20261007-c-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")
    before = git(bare, "rev-parse", "master")
    ledger = tmp_path / "sent.jsonl"
    seen = fail_cards_blob_reads(monkeypatch, fails)

    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain(src, clone, "HEAD", ledger, [], push=True)

    assert excinfo.value.code == "TIP_BLOB_READ_FAILED" and excinfo.value.code != "CARD_INVALID_UNATTRIBUTED"
    assert CARDS in excinfo.value.detail and "unable to read blob object" in excinfo.value.detail
    assert seen, "the cards file was never read through git cat-file"
    assert git(bare, "rev-parse", "master") == before
    assert not ledger.exists()


@needs_node
def test_r14_the_cli_exits_1_naming_the_tip_blob_read_failure(tmp_path, monkeypatch, capsys):
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    add_tip_cards(clone, CARD_BODY)
    src = init_source(tmp_path)
    add_item(src, "20261007-a-dup-of-tip.md", target=CARDS, body=CARD_BODY.replace("the mechanism", "another mechanism"))
    fail_cards_blob_reads(monkeypatch, lambda n, ref: True)

    rc = ob.main(["--repo", str(src), "drain", "--bus", str(clone), "--ref", "HEAD", "--ledger", str(tmp_path / "sent.jsonl"), "--push"])

    err = capsys.readouterr().err
    assert rc == 1 and "TIP_BLOB_READ_FAILED" in err and "CARD_INVALID_UNATTRIBUTED" not in err


def test_cat_file_blob_tells_an_absent_path_from_a_failed_read(tmp_path):
    """Absent at a readable ref is b"" (a target missing at the tip counts as empty); everything else
    git cannot read as a blob raises TIP_BLOB_READ_FAILED instead of passing for an empty file."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)  # gives the tip a `tools` directory: a tree is present but is not a blob
    tip = git(clone, "rev-parse", "origin/master")
    assert ob.cat_file_blob(clone, tip, "TRAPS.md") == git_raw(clone, "show", f"{tip}:TRAPS.md") != b""
    assert ob.cat_file_blob(clone, tip, "specs/mlv-app/cards.md") == b""
    assert ob.cat_file_blob(clone, tip, "TRAPS.md/under-a-file") == b""
    for ref, path in ((tip, "tools"), ("0" * 40, "TRAPS.md"), ("no-such-ref", "TRAPS.md")):
        with pytest.raises(ob.Refusal) as excinfo:
            ob.cat_file_blob(clone, ref, path)
        assert excinfo.value.code == "TIP_BLOB_READ_FAILED" and path in excinfo.value.detail


@needs_node
def test_r14_a_cards_file_that_is_genuinely_absent_at_the_tip_still_starts_the_trial_from_a_bare_title(tmp_path, monkeypatch):
    """The other half of the split: no cards file at the tip is still b"" and the per-card trial still
    runs from the title, so two clashing first cards are judged CARD_INVALID per card exactly as before
    (never TIP_BLOB_READ_FAILED) and the receipt still ships."""
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    assert ob.cat_file_blob(clone, git(clone, "rev-parse", "origin/master"), CARDS) == b""
    src = init_source(tmp_path)
    add_item(src, "20261007-a-card.md", target=CARDS, body=CARD_BODY)
    add_item(src, "20261007-b-card.md", target=CARDS, body=CARD_BODY.replace("the mechanism", "another mechanism"))
    add_item(src, "20261007-c-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [(r["path"], r["code"]) for r in report["refused"]] == [("doctrine-outbox/20261007-b-card.md", "CARD_INVALID")]
    assert "duplicate card id" in report["refused"][0]["detail"]
    assert sorted(p["path"] for p in report["published"]) == ["doctrine-outbox/20261007-a-card.md", "doctrine-outbox/20261007-c-receipt.md"]
    assert report["pushed"] is True


def test_r14_card_without_a_validator_on_the_bus_is_refused_fail_closed(tmp_path):
    bare, clone = init_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20261007-card.md", target=CARDS, body=CARD_BODY)
    before = git(bare, "rev-parse", "master")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [r["code"] for r in report["refused"]] == ["CARD_VALIDATOR_ABSENT"]
    assert "tools/validate-cards.mjs" in report["refused"][0]["detail"]
    assert report["published"] == [] and git(bare, "rev-parse", "master") == before


def test_r14_card_without_node_is_refused_fail_closed(tmp_path, monkeypatch):
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-card.md", target=CARDS, body=CARD_BODY)
    before = git(bare, "rev-parse", "master")
    monkeypatch.setattr(ob, "find_node", lambda: None)

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [r["code"] for r in report["refused"]] == ["CARD_VALIDATOR_NODE_ABSENT"]
    assert report["published"] == [] and git(bare, "rev-parse", "master") == before


def test_r14_receipts_and_rulings_keep_their_targets(tmp_path):
    bare, clone = init_bus(tmp_path)
    add_validator(clone)
    src = init_source(tmp_path)
    add_item(src, "20261007-receipt.md", target="RECEIPTS.md", kind="receipt", body="### A receipt\nbody\n")
    add_item(src, "20261007-ruling.md", target="RULINGS.md", kind="ruling", ratified_by="RULINGS.md#r14",
             body="### A ruling\nbody\n")

    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert report["refused"] == []
    assert sorted(p["target"] for p in report["published"]) == ["RECEIPTS.md", "RULINGS.md"]
    for kind in ("receipt", "ruling"):
        with pytest.raises(ob.Refusal) as excinfo:
            ob.parse_item(item_text(target=CARDS, kind=kind, ratified_by="RULINGS.md#r14", body=CARD_BODY))
        assert excinfo.value.code == "ITEM_CARD_NOT_A_TRAP"


# ---------- kernel filing: a wholesale filing on review/mlv-app-kernel-<date>, never master ----------
#
# Bus adjudications/factory-kernel/README.md and PROMPT-K s4-5: a kernel filing is committed on
# review/<project>-kernel-<YYYY-MM-DD> and pushed at once; ls-remote of that branch must equal the
# local tip; never master. The outbox route writes exactly two bus paths.

KF_DATE = "20261007"
KF_BRANCH = "review/mlv-app-kernel-2026-10-07"
KF_FILING = "project: mlv-app\nkernel: fleet-factory-kernel r5\n\nK1 | FIT | \"quote\" | evidence\n"
KF_BLOCK = "## 2026-10-07 KERNEL REFILE\n\nA block for the project's own spec.\n"
KF_SPEC_SEED = "# mlv-app\n\nold spec text\n"
OUTBOX_ENV = {"GIT_AUTHOR_NAME": ob.OUTBOX_IDENTITY_NAME, "GIT_AUTHOR_EMAIL": ob.OUTBOX_IDENTITY_EMAIL,
              "GIT_COMMITTER_NAME": ob.OUTBOX_IDENTITY_NAME, "GIT_COMMITTER_EMAIL": ob.OUTBOX_IDENTITY_EMAIL}


def init_kernel_bus(tmp_path: Path) -> tuple[Path, Path]:
    bare, clone = init_bus(tmp_path)
    (clone / "specs").mkdir()
    (clone / "specs" / "mlv-app.md").write_bytes(KF_SPEC_SEED.encode("utf-8"))
    git(clone, "add", "specs/mlv-app.md")
    git(clone, "commit", "-m", "seed project spec")
    git(clone, "push", "origin", "HEAD:refs/heads/master")
    return bare, clone


def add_kernel_filing(src: Path, date: str = KF_DATE, filing: str | None = KF_FILING, block: str | None = KF_BLOCK,
                      extra: dict[str, str] | None = None, commit: bool = True) -> Path:
    d = src / "doctrine-outbox" / "kernel-filing" / date
    d.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    if filing is not None:
        files["adjudications-factory-kernel-mlv-app.md"] = filing
    if block is not None:
        files["specs-mlv-app-block.md"] = block
    files.update(extra or {})
    for name, text in files.items():
        p = d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    if commit:
        git(src, "add", "--", f"doctrine-outbox/kernel-filing/{date}")
        git(src, "commit", "-m", f"kernel filing {date}")
    return d


def remote_heads(bare: Path) -> dict[str, str]:
    out = git(bare, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
    return dict(line.split() for line in out.splitlines())


def test_kernel_filing_writes_only_the_two_paths_and_proves_the_push(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    ledger = tmp_path / "sent.jsonl"
    master_before = git(bare, "rev-parse", "master")

    report = ob.drain_kernel_filing(src, clone, "HEAD", ledger, [], push=True)

    assert report["refused"] == [] and report["pushed"] is True
    assert [p["branch"] for p in report["published"]] == [KF_BRANCH]
    head = report["published"][0]["bus_commit"]
    assert git(bare, "rev-parse", "master") == master_before, "master must never move"
    assert git(clone, "ls-remote", "origin", f"refs/heads/{KF_BRANCH}").split()[0] == head
    assert set(remote_heads(bare)) == {"refs/heads/master", f"refs/heads/{KF_BRANCH}"}
    changed = git(bare, "diff", "--name-only", master_before, head).splitlines()
    assert sorted(changed) == ["adjudications/factory-kernel/mlv-app.md", "specs/mlv-app.md"]
    assert git_raw(bare, "show", f"{head}:adjudications/factory-kernel/mlv-app.md") == KF_FILING.encode("utf-8")
    spec = git_raw(bare, "show", f"{head}:specs/mlv-app.md").decode("utf-8")
    assert spec.startswith(KF_SPEC_SEED + "\n" + KF_BLOCK)
    assert spec.count("<!-- outbox:") == 1
    ident = f"{ob.OUTBOX_IDENTITY_NAME} <{ob.OUTBOX_IDENTITY_EMAIL}>"
    assert git(bare, "log", "-1", "--format=%an <%ae>|%cn <%ce>", head) == f"{ident}|{ident}"
    rows = _ledger_rows(ledger)
    assert len(rows) == 1
    assert rows[0]["route"] == "kernel-filing" and rows[0]["target"] == KF_BRANCH
    assert rows[0]["bus_commit"] == rows[0]["ls_remote"] == head


def test_kernel_filing_dry_run_is_the_default_and_pushes_nothing(tmp_path, capsys):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    ledger = tmp_path / "sent.jsonl"
    before = remote_heads(bare)

    rc = ob.main(["--repo", str(src), "kernel-filing", "--bus", str(clone), "--ref", "HEAD", "--ledger", str(ledger)])

    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report["pushed"] is False and report["published"] == []
    assert [w["branch"] for w in report["would_push"]] == [KF_BRANCH]
    assert remote_heads(bare) == before
    assert not ledger.exists()


def test_kernel_filing_without_a_spec_block_writes_the_filing_alone(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src, block=None)
    master = git(bare, "rev-parse", "master")
    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    head = report["published"][0]["bus_commit"]
    assert git(bare, "diff", "--name-only", master, head).splitlines() == ["adjudications/factory-kernel/mlv-app.md"]


@pytest.mark.parametrize("extra", [
    {"notes.md": "a third file\n"},
    {"sub/adjudications-factory-kernel-mlv-app.md": "nested\n"},
])
def test_kernel_filing_refuses_an_extra_source_path(tmp_path, extra):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src, extra=extra)
    before = remote_heads(bare)

    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [r["code"] for r in report["refused"]] == ["KERNEL_FILING_EXTRA_PATH"]
    assert report["published"] == [] and remote_heads(bare) == before


def test_kernel_filing_refuses_a_date_without_the_filing(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src, filing=None)
    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert [r["code"] for r in report["refused"]] == ["KERNEL_FILING_MISSING"]


def test_kernel_filing_refuses_a_write_outside_the_two_paths(tmp_path, monkeypatch):
    """Belt and braces: if anything but the two destinations changes in the bus commit, the
    publish is refused before any push."""
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    before = remote_heads(bare)
    real_write = ob.write_kernel_files

    def sneaky_write(wt, *args):
        (wt / "RULINGS.md").write_text("# rewritten\n", encoding="utf-8")
        return real_write(wt, *args)

    monkeypatch.setattr(ob, "write_kernel_files", sneaky_write)
    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert excinfo.value.code == "KERNEL_FILING_PATH_REFUSED"
    assert remote_heads(bare) == before


@pytest.mark.parametrize("branch", [
    "master", "main", "refs/heads/master", "review/mlv-app-kernel-20261007", "review/conjugal-kernel-2026-10-07",
    "review/mlv-app-kernel-2026-10-07-2", "review/mlv-app-kernel-2026-10-07/../master",
])
def test_kernel_filing_refuses_master_or_any_foreign_branch(branch):
    with pytest.raises(ob.Refusal) as excinfo:
        ob.assert_kernel_branch(branch)
    assert excinfo.value.code == "KERNEL_BRANCH_REFUSED"


def test_kernel_filing_cli_cannot_name_a_branch_or_a_path(tmp_path):
    ob.build_parser().parse_args(["kernel-filing", "--bus", str(tmp_path)])
    for flag in ("--branch", "--target", "--path"):
        with pytest.raises(SystemExit):
            ob.build_parser().parse_args(["kernel-filing", "--bus", str(tmp_path), flag, "master"])


@pytest.mark.parametrize("date,branch", [
    ("20261007", "review/mlv-app-kernel-2026-10-07"),
    ("20260914", "review/mlv-app-kernel-2026-09-14"),
    ("2026107", None), ("20261340", None), ("2026-10-07", None), ("..", None),
])
def test_kernel_branch_name_pattern(date, branch):
    if branch is None:
        with pytest.raises(ob.Refusal) as excinfo:
            ob.kernel_branch(date)
        assert excinfo.value.code == "KERNEL_FILING_BAD_DATE"
    else:
        assert ob.kernel_branch(date) == branch
        ob.assert_kernel_branch(branch)


def test_kernel_filing_bad_date_dir_is_refused_and_pushes_nothing(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src, date="20261399")
    before = remote_heads(bare)
    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert [r["code"] for r in report["refused"]] == ["KERNEL_FILING_BAD_DATE"]
    assert remote_heads(bare) == before


def test_kernel_filing_rerun_is_a_noop_by_ledger_and_by_bus(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    ledger = tmp_path / "sent.jsonl"
    first = ob.drain_kernel_filing(src, clone, "HEAD", ledger, [], push=True)
    heads = remote_heads(bare)

    second = ob.drain_kernel_filing(src, clone, "HEAD", ledger, [], push=True)
    assert second["published"] == [] and second["pushed"] is False
    assert [a["via"] for a in second["already_sent"]] == ["ledger"]

    fresh_clone = tmp_path / "bus-fresh-clone"
    subprocess.run(["git", "clone", str(bare), str(fresh_clone)], check=True, capture_output=True, text=True)
    lost = tmp_path / "sent-lost.jsonl"
    third = ob.drain_kernel_filing(src, fresh_clone, "HEAD", lost, [], push=True)
    assert third["published"] == [] and third["pushed"] is False
    assert [a["via"] for a in third["already_sent"]] == ["bus_branch"]
    assert remote_heads(bare) == heads
    assert [r["bus_commit"] for r in _ledger_rows(lost)] == [first["published"][0]["bus_commit"]]


def test_kernel_filing_revision_fast_forwards_our_branch_without_duplicating_the_block(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    first = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    old_tip = first["published"][0]["bus_commit"]
    add_kernel_filing(src, filing=KF_FILING + "K2 | FIT | \"q\" | revised\n")

    second = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    new_tip = second["published"][0]["bus_commit"]
    assert git(bare, "rev-parse", f"{new_tip}^") == old_tip, "a revision is a fast-forward of our branch"
    assert git(bare, "diff", "--name-only", old_tip, new_tip).splitlines() == ["adjudications/factory-kernel/mlv-app.md"]
    assert git_raw(bare, "show", f"{new_tip}:specs/mlv-app.md").count(b"<!-- outbox:") == 1


def test_kernel_filing_refuses_a_branch_that_is_not_ours(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    other = tmp_path / "steward-clone"
    subprocess.run(["git", "clone", str(bare), str(other)], check=True, capture_output=True, text=True)
    (other / "note.md").write_text("steward note\n", encoding="utf-8")
    git(other, "add", "note.md")
    git(other, "commit", "-m", "steward commit")
    git(other, "push", "origin", f"HEAD:refs/heads/{KF_BRANCH}")
    src = init_source(tmp_path)
    add_kernel_filing(src)
    before = remote_heads(bare)

    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [r["code"] for r in report["refused"]] == ["KERNEL_BRANCH_NOT_OURS"]
    assert remote_heads(bare) == before


def test_kernel_filing_reads_committed_bytes_only(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    d = add_kernel_filing(src)
    add_kernel_filing(src, date="20261008", commit=False)  # never committed: invisible
    (d / "adjudications-factory-kernel-mlv-app.md").write_text("UNCOMMITTED EDIT\n", encoding="utf-8")

    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    assert [p["branch"] for p in report["published"]] == [KF_BRANCH]
    head = report["published"][0]["bus_commit"]
    assert git_raw(bare, "show", f"{head}:adjudications/factory-kernel/mlv-app.md") == KF_FILING.encode("utf-8")


def test_kernel_filing_ls_remote_mismatch_fails_loud(tmp_path, monkeypatch):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    ledger = tmp_path / "sent.jsonl"
    real_git_try = ob.git_try

    def phantom_git_try(repo, *args, **kw):
        if args and args[0] == "push":
            return 0, "", ""  # claims success, pushes nothing
        return real_git_try(repo, *args, **kw)

    monkeypatch.setattr(ob, "git_try", phantom_git_try)
    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain_kernel_filing(src, clone, "HEAD", ledger, [], push=True)
    assert excinfo.value.code == "PUSH_VERIFY_FAILED"
    assert not ledger.exists()


def test_kernel_filing_non_fast_forward_retries_from_the_new_tip(tmp_path, monkeypatch):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    first = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent-a.jsonl", [], push=True)
    add_kernel_filing(src, filing=KF_FILING + "revised\n")
    rival = tmp_path / "rival-clone"
    # Pinned in the clone's own config: the host's global autocrlf would leave a CRLF checkout that
    # the helper's `-c core.autocrlf=false` then reads as local changes.
    subprocess.run(["git", "clone", "-c", "core.autocrlf=false", str(bare), str(rival)],
                   check=True, capture_output=True, text=True)
    real_make, calls = ob.make_temp_worktree, {"n": 0}

    def racing_make(bus_repo, tip):
        calls["n"] += 1
        if calls["n"] == 1:  # another run of this outbox lands on our branch first
            git(rival, "checkout", "-q", "-b", "k", f"origin/{KF_BRANCH}")
            (rival / "adjudications" / "factory-kernel" / "mlv-app.md").write_text("rival revision\n", encoding="utf-8")
            git(rival, "add", "adjudications/factory-kernel/mlv-app.md")
            git(rival, "commit", "-m", "rival", env=OUTBOX_ENV)
            git(rival, "push", "origin", f"HEAD:refs/heads/{KF_BRANCH}")
        return real_make(bus_repo, tip)

    monkeypatch.setattr(ob, "make_temp_worktree", racing_make)
    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent-b.jsonl", [], push=True)

    assert calls["n"] == 2 and report["pushed"] is True
    tip = report["published"][0]["bus_commit"]
    assert git(bare, "rev-parse", f"refs/heads/{KF_BRANCH}") == tip
    git(bare, "merge-base", "--is-ancestor", first["published"][0]["bus_commit"], tip)
    assert git_raw(bare, "show", f"{tip}:adjudications/factory-kernel/mlv-app.md") == (KF_FILING + "revised\n").encode("utf-8")


def test_kernel_filing_law4_screen_refuses_without_a_word_cap(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src, filing=KF_FILING + ("word " * 2000) + "\nsee .claude-state/RESUME.md\n")
    before = remote_heads(bare)
    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert report["refused"] == [{"path": f"doctrine-outbox/kernel-filing/{KF_DATE}", "code": "LAW4_REFUSED",
                                  "detail": "board private state dir"}]
    assert remote_heads(bare) == before
    long_clean = add_kernel_filing(src, filing=KF_FILING + ("word " * 2000) + "\n")
    assert long_clean.is_dir()
    report = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    assert report["refused"] == [] and report["pushed"] is True, "a kernel filing has no 450-word cap"


def test_kernel_filing_subtree_is_not_an_outbox_item(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_item(src, "20260925-example.md")
    add_kernel_filing(src)
    report = ob.drain(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=False)
    assert report["refused"] == []
    assert [w["path"] for w in report["would_push"]] == ["doctrine-outbox/20260925-example.md"]


def assert_shipped_kernel_filings(root: Path) -> None:
    dates = sorted(root.iterdir()) if root.is_dir() else []
    assert dates, f"no kernel filing shipped under {root}"
    for d in dates:
        ob.kernel_branch(d.name)
        names = sorted(p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file())
        assert "adjudications-factory-kernel-mlv-app.md" in names, names
        assert set(names) <= {"adjudications-factory-kernel-mlv-app.md", "specs-mlv-app-block.md"}, names
        for name in names:
            text = (d / name).read_text(encoding="utf-8")
            assert text.strip(), f"{d.name}/{name} is empty"
            ob.screen_law4(text, [], word_cap=None)


def test_shipped_kernel_filings_are_well_formed_and_screen_clean():
    """Every kernel filing committed in this checkout has the fixed shape and passes the Law-4
    screen, so the first kernel-filing drain cannot refuse it."""
    assert_shipped_kernel_filings(REPO_ROOT / "doctrine-outbox" / "kernel-filing")


@pytest.mark.parametrize("layout", ["absent", "empty"])
def test_shipped_kernel_filing_guard_fails_when_the_payload_is_missing(tmp_path, layout):
    root = tmp_path / "kernel-filing"
    if layout == "empty":
        root.mkdir()
    with pytest.raises(AssertionError):
        assert_shipped_kernel_filings(root)


@pytest.mark.parametrize("filing,block,empty", [
    ("", KF_BLOCK, "adjudications-factory-kernel-mlv-app.md"),
    (" \n\t\n", KF_BLOCK, "adjudications-factory-kernel-mlv-app.md"),
    (KF_FILING, "\n\n", "specs-mlv-app-block.md"),
])
def test_kernel_filing_refuses_an_empty_filing_or_block_before_any_write(tmp_path, monkeypatch, filing, block, empty):
    """An empty committed filing would rewrite the bus adjudication wholesale to zero bytes."""
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src, filing=filing, block=block)
    ledger = tmp_path / "sent.jsonl"
    before = remote_heads(bare)
    monkeypatch.setattr(ob, "make_temp_worktree", lambda *a: pytest.fail("no worktree for an empty filing"))

    report = ob.drain_kernel_filing(src, clone, "HEAD", ledger, [], push=True)

    assert report["refused"] == [{"path": f"doctrine-outbox/kernel-filing/{KF_DATE}",
                                  "code": "KERNEL_FILING_EMPTY", "detail": empty}]
    assert report["published"] == [] and report["pushed"] is False
    assert remote_heads(bare) == before and not ledger.exists()


def test_write_kernel_files_refuses_an_empty_filing_at_the_sink(tmp_path):
    """The one function that writes the bus adjudication refuses blank bytes on its own."""
    dest = tmp_path / ob.KERNEL_FILING_DEST
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"existing adjudication\n")
    with pytest.raises(ob.Refusal) as excinfo:
        ob.write_kernel_files(tmp_path, {"filing": b" \r\n", "block": None, "block_key": None}, b"")
    assert excinfo.value.code == "KERNEL_FILING_EMPTY"
    assert dest.read_bytes() == b"existing adjudication\n"


def _rival_push(bare: Path, tmp_path: Path, text: str) -> str:
    """Another run of this outbox lands commit C on the review branch."""
    rival = tmp_path / f"rival-{len(list(tmp_path.glob('rival-*')))}"
    subprocess.run(["git", "clone", "-c", "core.autocrlf=false", str(bare), str(rival)],
                   check=True, capture_output=True, text=True)
    git(rival, "checkout", "-q", "-b", "k", f"origin/{KF_BRANCH}")
    (rival / "adjudications" / "factory-kernel" / "mlv-app.md").write_text(text, encoding="utf-8")
    git(rival, "add", "adjudications/factory-kernel/mlv-app.md")
    git(rival, "commit", "-m", "rival", env=OUTBOX_ENV)
    git(rival, "push", "origin", f"HEAD:refs/heads/{KF_BRANCH}")
    return git(rival, "rev-parse", "HEAD")


def _lossy_push(monkeypatch):
    """Every push lands but reports failure: the acknowledgement is lost."""
    real_git_try = ob.git_try

    def lossy(repo, *args, **kw):
        rc, out, err = real_git_try(repo, *args, **kw)
        if args and args[0] == "push" and rc == 0:
            return 1, out, "fatal: the remote end hung up unexpectedly"
        return rc, out, err
    monkeypatch.setattr(ob, "git_try", lossy)


def test_kernel_filing_lost_ack_that_landed_is_published_after_ls_remote(tmp_path, monkeypatch):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    ledger = tmp_path / "sent.jsonl"
    _lossy_push(monkeypatch)

    report = ob.drain_kernel_filing(src, clone, "HEAD", ledger, [], push=True)

    tip = git(bare, "rev-parse", f"refs/heads/{KF_BRANCH}")
    assert report["pushed"] is True and [p["bus_commit"] for p in report["published"]] == [tip]
    assert [(r["bus_commit"], r["ls_remote"]) for r in _ledger_rows(ledger)] == [(tip, tip)]


def test_kernel_filing_lost_ack_then_a_rival_tip_is_refused_with_no_sent_row(tmp_path, monkeypatch):
    """B lands but its ack is lost; the retry fetches B; C lands before the no-change branch
    certifies B. Publication of B must not be claimed: ls-remote is C."""
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    ledger = tmp_path / "sent.jsonl"
    _lossy_push(monkeypatch)
    real_write, calls, rival = ob.write_kernel_files, {"n": 0}, {}

    def write_then_race(*args):
        calls["n"] += 1
        changed = real_write(*args)
        if calls["n"] == 2:  # the retry, on fetched B
            assert changed is False
            rival["c"] = _rival_push(bare, tmp_path, "rival revision C\n")
        return changed

    monkeypatch.setattr(ob, "write_kernel_files", write_then_race)
    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain_kernel_filing(src, clone, "HEAD", ledger, [], push=True)

    assert excinfo.value.code == "PUSH_VERIFY_FAILED"
    assert rival["c"] in excinfo.value.detail
    assert git(bare, "rev-parse", f"refs/heads/{KF_BRANCH}") == rival["c"]
    assert not ledger.exists()


def test_kernel_filing_bus_branch_noop_is_refused_when_the_tip_moves(tmp_path, monkeypatch):
    """The no-change branch on a fresh clone (lost ledger) also ends with ls-remote equality."""
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent-a.jsonl", [], push=True)
    real_write = ob.write_kernel_files

    def write_then_race(*args):
        changed = real_write(*args)
        _rival_push(bare, tmp_path, "rival revision C\n")
        return changed

    monkeypatch.setattr(ob, "write_kernel_files", write_then_race)
    lost = tmp_path / "sent-lost.jsonl"
    with pytest.raises(ob.Refusal) as excinfo:
        ob.drain_kernel_filing(src, clone, "HEAD", lost, [], push=True)
    assert excinfo.value.code == "PUSH_VERIFY_FAILED"
    assert not lost.exists()


def test_kernel_filing_key_is_content_only_not_source_commit(tmp_path):
    """Identical filing and block bytes under different source commits are the same filing."""
    a = init_source(tmp_path, name="source-a")
    add_kernel_filing(a)
    b = init_source(tmp_path, name="source-b")
    git(b, "commit", "--allow-empty", "-m", "an unrelated commit first")
    add_kernel_filing(b)
    fa, fb = ob.load_kernel_filings(a, "HEAD")[0], ob.load_kernel_filings(b, "HEAD")[0]
    assert fa["src"] != fb["src"]
    assert fa["key"] == fb["key"] and fa["block_key"] == fb["block_key"]


def test_kernel_filing_same_date_block_revision_replaces_the_block(tmp_path):
    bare, clone = init_kernel_bus(tmp_path)
    src = init_source(tmp_path)
    add_kernel_filing(src)
    first = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)
    old_tip = first["published"][0]["bus_commit"]
    revised = KF_BLOCK.replace("A block", "A revised block")
    add_kernel_filing(src, block=revised)

    second = ob.drain_kernel_filing(src, clone, "HEAD", tmp_path / "sent.jsonl", [], push=True)

    new_tip = second["published"][0]["bus_commit"]
    assert git(bare, "rev-parse", f"{new_tip}^") == old_tip
    assert git(bare, "diff", "--name-only", old_tip, new_tip).splitlines() == ["specs/mlv-app.md"]
    spec = git_raw(bare, "show", f"{new_tip}:specs/mlv-app.md").decode("utf-8")
    assert spec.startswith(KF_SPEC_SEED + "\n" + revised)
    assert "A block for" not in spec and spec.count("<!-- outbox:") == 1


# ---------- validate / check-commits judge the card the drain will actually append ----------
#
# Named mutations for this block (each turns the named tests red, then is reverted):
#   M13 drop the rendered-card check from cmd_validate        -> the over-limit validate tests
#   M14 drop the rendered-card check from check_commit        -> test_check_commits_lists_an_item_whose_rendered_card_is_over_the_limit
#   M15 count the bare body instead of the rendered block     -> test_validate_accepts_a_card_exactly_at_the_rendered_limits (the +1 case passes)

def _vendored_card_limits() -> tuple[int, int]:
    text = (FIXTURE_DIR / "validate-cards.mjs").read_text(encoding="utf-8")
    lines = int(re.search(r"export const MAX_CARD_LINES = (\d+);", text).group(1))
    size = int(re.search(r"export const MAX_CARD_BYTES = (\d+);", text).group(1))
    return lines, size


def _rendered_card_size(body: str) -> tuple[int, int]:
    """(lines, bytes) of the block the drain appends, counted the way the bus validator counts a card."""
    _key, block = ob.render_block({"meta": {"target": CARDS}, "body": body}, "0" * 40)
    rows = block.strip("\n").split("\n")
    return len(rows), len("\n".join(rows).encode("utf-8"))


def _card_with_rendered_bytes(target_bytes: int) -> str:
    """A card whose RENDERED size is exactly target_bytes (one padded note line)."""
    base = CARD_BODY + "- **Note:** "
    pad = target_bytes - _rendered_card_size(base)[1]
    assert pad >= 0
    return base + "x" * pad + "\n"


def _card_with_rendered_lines(n_lines: int) -> str:
    body = CARD_BODY
    while _rendered_card_size(body)[0] < n_lines:
        body += f"- **Note:** line {_rendered_card_size(body)[0]}\n"
    assert _rendered_card_size(body)[0] == n_lines
    return body


def _write_card_item(directory: Path, body: str, name: str = "20261009-a-card.md") -> Path:
    path = directory / name
    path.write_text(item_text(target=CARDS, body=body), encoding="utf-8")
    return path


def test_card_limits_are_the_vendored_bus_validators():
    assert (ob.MAX_CARD_LINES, ob.MAX_CARD_BYTES) == _vendored_card_limits()


def test_validate_refuses_a_card_in_limits_bare_but_over_in_bytes_once_rendered(tmp_path, capsys):
    _max_lines, max_bytes = _vendored_card_limits()
    body = _card_with_rendered_bytes(max_bytes + 1)
    assert len(body.rstrip("\n").encode("utf-8")) <= max_bytes < _rendered_card_size(body)[1]
    item = _write_card_item(tmp_path, body)
    assert ob.main(["--repo", str(tmp_path), "validate", str(item)]) == 1
    err = capsys.readouterr().err
    assert "CARD_INVALID" in err and f"{max_bytes + 1} bytes" in err and f"limit is {max_bytes}" in err


def test_validate_refuses_a_card_in_limits_bare_but_over_in_lines_once_rendered(tmp_path, capsys):
    max_lines, _max_bytes = _vendored_card_limits()
    body = _card_with_rendered_lines(max_lines + 1)
    assert len(body.rstrip("\n").split("\n")) == max_lines
    item = _write_card_item(tmp_path, body)
    assert ob.main(["--repo", str(tmp_path), "validate", str(item)]) == 1
    err = capsys.readouterr().err
    assert "CARD_INVALID" in err and f"{max_lines + 1} lines" in err and f"limit is {max_lines}" in err


def test_validate_accepts_a_card_exactly_at_the_rendered_limits(tmp_path, capsys):
    """No false refusal: the boundary is the rendered size, computed from render_block's real overhead."""
    max_lines, max_bytes = _vendored_card_limits()
    at_bytes = _write_card_item(tmp_path, _card_with_rendered_bytes(max_bytes), "20261009-at-bytes.md")
    at_lines = _write_card_item(tmp_path, _card_with_rendered_lines(max_lines), "20261009-at-lines.md")
    assert ob.main(["--repo", str(tmp_path), "validate", str(at_bytes), str(at_lines)]) == 0
    over = _write_card_item(tmp_path, _card_with_rendered_bytes(max_bytes + 1), "20261009-over-by-one.md")
    assert ob.main(["--repo", str(tmp_path), "validate", str(over)]) == 1
    assert capsys.readouterr().err.count("REFUSED") == 1


def test_validate_does_not_apply_the_card_limit_to_other_targets(tmp_path):
    long_trap = tmp_path / "20261009-long-trap.md"
    long_trap.write_text(item_text(body="### heading\n" + "x" * 5000 + "\n"), encoding="utf-8")
    assert ob.main(["--repo", str(tmp_path), "validate", str(long_trap)]) == 0


def test_validate_judges_a_pending_card_at_the_full_width_source_commit(tmp_path):
    """PENDING resolves to a full commit at drain time; validate renders a 40-hex placeholder, so the
    marker width is the drain's worst case and a card at the limit under it is accepted."""
    _max_lines, max_bytes = _vendored_card_limits()
    item = tmp_path / "20261009-pending-card.md"
    item.write_text(item_text(target=CARDS, body=_card_with_rendered_bytes(max_bytes), source_commit="PENDING"),
                    encoding="utf-8")
    assert ob.main(["--repo", str(tmp_path), "validate", str(item)]) == 0


def test_check_commits_lists_an_item_whose_rendered_card_is_over_the_limit(tmp_path):
    _max_lines, max_bytes = _vendored_card_limits()
    src, base = _range_repo(tmp_path)
    over_name, ok_name = "20261009-over-card.md", "20261009-ok-card.md"
    _commit(src, {f"doctrine-outbox/{over_name}": item_text(target=CARDS, body=_card_with_rendered_bytes(max_bytes + 1))},
            f"add an over-limit card\n\nDoctrine-Export: outbox {over_name}")
    _commit(src, {f"doctrine-outbox/{ok_name}": item_text(target=CARDS, body=_card_with_rendered_bytes(max_bytes))},
            f"add an in-limit card\n\nDoctrine-Export: outbox {ok_name}")
    failures = ob.check_commits(src, f"{base}..HEAD", [])
    assert len(failures) == 1
    (problems,) = failures.values()
    assert len(problems) == 1
    assert problems[0].startswith(f"DOCTRINE_EXPORT_OUTBOX_ITEM_INVALID: doctrine-outbox/{over_name}")
    assert "CARD_INVALID" in problems[0] and f"{max_bytes + 1} bytes" in problems[0]


# ---------- every committed outbox item must survive the drain's card validation ----------
#
# The drain refuses a card the bus validator rejects (size over 2,000 bytes, an `applies` value that
# is not a fleet member, ...) only AFTER the item has merged, so the lesson silently never ships.
# This renders every tracked doctrine-outbox item at HEAD exactly as drain() does (load_outbox_items
# + render_block) and runs each card through the vendored validator, so CI refuses it before merge.
#
# Named mutation for this block (turns the named test red, then is reverted):
#   M16 skip the validator run in _validator_refusals()   -> test_a_card_the_validator_rejects_is_named_with_its_reason

# The roster `applies` is judged against: the validator's own fleet-membership.mjs run over the bus's
# top-level specs/*.md. A snapshot (CI has no bus clone); a card legitimately naming a newer member
# fails here until this list is refreshed from the bus.
FLEET_MEMBERS_SNAPSHOT = (
    "account-rotation-and-project-continuity", "account-rotation-automation", "adobe-ingester",
    "adversarial-swarms-and-doctrine-publishing-standard", "adversarialllm", "agent-bridge", "airmypc",
    "autonomous-decision-making-with-adversarial-swarms", "autonomous-swarm-adjudication",
    "cli-credential-rotation-automation", "cli-credential-rotation-coexistence",
    "cli-credential-synchronization", "cli-orchestration-standard", "cloudvore", "conjugal",
    "context-ultra-salesforce", "design-loop-protocol", "dispatch-trigger-standard", "dng-auto-processor",
    "doctrine-guard-conflict-prevention", "machine-inventory-schema", "mlv-app",
    "multi-provider-failover-pattern", "parallel-consensus-swarm", "phased-concurrent-review-pattern",
    "posture-templates-conjugal-standard", "pre-rotation-proof-and-resume-dispatcher", "salesforce-tools",
    "spec-adoption-pipeline", "spec-continuous-sync-for-floors",
)


def _validator_refusals(loaded: list[dict], tmp_path: Path) -> list[str]:
    """One line per loaded outbox item the drain would refuse: a parse/name error, or a card the
    vendored validator rejects (judged as title + that card, as the drain's per-card pass does)."""
    bus = tmp_path / "bus"
    (bus / "specs").mkdir(parents=True)
    (bus / "RULINGS.md").write_text("# RULINGS\n", encoding="utf-8")
    for member in FLEET_MEMBERS_SNAPSHOT:
        (bus / "specs" / f"{member}.md").write_text(f"# {member}\n", encoding="utf-8")
    refusals = []
    for it in loaded:
        if "error" in it:
            refusals.append(f"{it['path']}: OUTBOX_ITEM_INVALID: {it['error']}")
            continue
        item = it["item"]
        if item["meta"]["target"] != CARDS:
            continue
        src = it["source_commit"]
        _key, block = ob.render_block(item, "0" * 40 if src == "PENDING" else src)
        cards = tmp_path / f"cards-{it['name']}"
        cards.write_bytes(ob.CARDS_TITLE.encode("utf-8") + block.encode("utf-8"))
        proc = subprocess.run(["node", str(FIXTURE_DIR / "validate-cards.mjs"), "--bus", str(bus),
                               "--file", str(cards), "--project", "mlv-app"],
                              capture_output=True, text=True, encoding="utf-8", timeout=60)
        if proc.returncode != 0:
            reasons = "; ".join(line.split("] ", 1)[-1] for line in proc.stdout.splitlines() if line.startswith("INVALID"))
            refusals.append(f"{it['path']}: validator exit {proc.returncode}: {reasons or (proc.stdout + proc.stderr).strip()}")
    return refusals


@needs_node
def test_every_committed_outbox_item_passes_the_card_validator(tmp_path):
    loaded = ob.load_outbox_items(REPO_ROOT, "HEAD")
    assert loaded, "no doctrine-outbox items at HEAD: the enumeration itself is broken"
    refusals = _validator_refusals(loaded, tmp_path)
    assert not refusals, "the drain would refuse these committed outbox items:\n" + "\n".join(refusals)


@needs_node
def test_a_card_the_validator_rejects_is_named_with_its_reason(tmp_path):
    """The guard test above cannot pass vacuously: an over-size card whose `applies` is not a member
    comes back named, with both validator reasons; an in-limit member card does not."""
    _max_lines, max_bytes = _vendored_card_limits()
    bad_body = _card_with_rendered_bytes(max_bytes + 1).replace(
        "check:", "applies: every board that retires worktrees\ncheck:", 1)
    sha = "a" * 40
    good = {"path": "doctrine-outbox/20261009-good.md", "name": "20261009-good.md", "source_commit": sha,
            "item": ob.parse_item(item_text(target=CARDS, source_commit=sha, body=CARD_BODY))}
    bad = {"path": "doctrine-outbox/20261009-bad.md", "name": "20261009-bad.md", "source_commit": sha,
           "item": ob.parse_item(item_text(target=CARDS, source_commit=sha, body=bad_body))}
    broken = {"path": "doctrine-outbox/Bad Name.md", "name": "Bad Name.md", "error": "ITEM_BAD_FILENAME: Bad Name.md"}
    refusals = _validator_refusals([good, bad, broken], tmp_path)
    assert len(refusals) == 2
    assert refusals[0].startswith("doctrine-outbox/20261009-bad.md: validator exit 1:")
    assert re.search(rf"card is \d+ bytes; the limit is {max_bytes}", refusals[0])
    assert "applies names 'every board that retires worktrees', which is not a fleet member" in refusals[0]
    assert refusals[1] == "doctrine-outbox/Bad Name.md: OUTBOX_ITEM_INVALID: ITEM_BAD_FILENAME: Bad Name.md"
