#!/usr/bin/env python3
"""Doctrine outbox for MLV-App: findings reach the fleet doctrine bus by FILE, not by memory.

Ported from agent-bridge's `tools/doctrine_outbox.py` (github/master bfc39bf), itself a
port of the fleet bus's reference outbox. The item format, front matter, `parse_item`,
the Law-4 screen shape, `idempotency_key` and `render_block` are byte-compatible, so a
block reads `<!-- outbox:<key> mlv-app:<sha12> -->`.

WHAT IS DIFFERENT HERE (MLV-App's checkout shape drove every change)
--------------------------------------------------------------------
1. REF, never HEAD. In MLV-App `origin` is the UPSTREAM project and the checkout sits on
   a peer branch, so every item / debt / age query takes `--ref`, default
   `refs/remotes/fork/master`, and reads committed bytes only. A ref that lacks this
   tool or the items directory is `TOOL_ABSENT_AT_REF` and debt is UNKNOWN (exit 2),
   never 0 -- the false all-clear a stale checkout would otherwise print.
2. NO RE-APPEND of hand-written bus entries. An item may carry tracked front matter
   `published_as: <bus sha>`; drain verifies that sha is an ancestor of the fetched bus
   tip AND the item's `### ` heading is present in the target at that sha, then skips
   the item. An unverifiable claim is refused, never trusted and never appended.
3. Bus-side idempotency: a key already on the fetched bus tip is never re-appended, even
   with an empty or lost sent ledger (new clone, machine move).
4. Drain guarantees a blank line before every appended block, checks the byte prefix on
   the COMMITTED blobs, retries from scratch on a non-fast-forward, and proves each push
   with `ls-remote`. Debt age is measured from the first-parent merge into `--ref`, not
   the branch commit's date.
5. `check-ledger`: the capture-check primitive. Structured findings (`Finding: KF-<nn>`
   tags, and KF ids inside `## KERNEL FINDINGS`) must each be followed by
   `Doctrine-Export: outbox <item>` or `Doctrine-Export: none <reason, >= 4 words>`.
   Keyword hits are ADVISORY output only. The watermark is a byte offset plus the sha256
   of the prefix, advanced only on disposition.
6. `check-commits`: commits touching a finding path need a `Doctrine-Export:` trailer.
7. Law 4 adds MLV deny terms: the board's private state paths and the GPU host name.
8. Dropped from the agent-bridge port: `seed` (agent-bridge's own 2026-09-25 manual drain
   rows) and `check-decision` (a decision-JSON contract no MLV lane emits).

Wiring (OS drain task, Stop hook, heartbeat debt) is card DOCTRINE-OUTBOX-ADOPT-MLV-1b and
installs against this CLI. Nothing here runs itself.

Subcommands
-----------
    validate <item.md>...                       parse + screen item files
    debt [--ref R] [--ledger P] [--bus PATH] [--json]
    drain --bus PATH [--ref R] [--push] [--ledger P] [--deny-file P]
    check-ledger [--ledger P] [--watermark P] [--advance | --baseline]
    check-commits [--range A..B]

Exit codes: 0 = ok. 1 = debt, undisposed findings, or a validation/screen refusal.
2 = UNKNOWN (usage error, git failure, ref or tool absent). Refusals print to stderr.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

PROJECT = "mlv-app"
OUTBOX_DIR = "doctrine-outbox"
TOOL_REL = "tools/coordination/doctrine_outbox.py"
DEFAULT_REF = "refs/remotes/fork/master"
TARGETS = ("RECEIPTS.md", "TRAPS.md", "RULINGS.md")
KINDS = ("receipt", "trap", "ruling")
WORD_CAP = 450
ITEM_NAME_RE = re.compile(r"^\d{8}-[a-z0-9][a-z0-9-]{2,60}\.md$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
STALE_HOURS = 24

# Fixed, non-personal identity for every bus commit this tool makes. Pinned both via
# `-c user.name=`/`-c user.email=` on the commit invocation AND via GIT_AUTHOR_*/
# GIT_COMMITTER_* env vars, so ambient repo/global git config can never win either way.
OUTBOX_IDENTITY_NAME = "mlv-app doctrine outbox"
OUTBOX_IDENTITY_EMAIL = "outbox@mlv-app.invalid"

LEDGER_REL = pathlib.Path(".claude-state") / "doctrine-outbox" / "sent.jsonl"
DENY_FILE_REL = pathlib.Path(".claude-state") / "doctrine-outbox" / "deny-names.txt"
SUBJECT_LEDGER_REL = pathlib.Path(".claude-state") / "kernel" / "subject-ledger.md"
WATERMARK_REL = pathlib.Path(".claude-state") / "doctrine-outbox" / "ledger-watermark.json"
SUBJECT_LEDGER_ENV = "MLV_OUTBOX_LEDGER"
WATERMARK_ENV = "MLV_OUTBOX_WATERMARK"

# Paths where findings have historically come from: a commit touching one must declare
# `Doctrine-Export:`. Deliberately narrow, so ordinary product commits carry nothing.
FINDING_PATH_PREFIXES = ("tools/coordination/", "agents/", ".claude/")
FINDING_PATH_FILES = ("CLAUDE.md",)

# Law 4 screen on the body. Each pattern names the class it refuses; the screen prints
# which and a short excerpt of what matched. The first nine are agent-bridge's; the rest
# are MLV's own deny terms (private board state, and the GPU host).
LAW4 = (
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("account/org uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("lane wire path", re.compile(r"coordination/(lanes|comms)/")),
    ("HUB heartbeat", re.compile(r"\bHUB\.md\b")),
    ("owner transcript store", re.compile(r"loops\.json")),
    ("bearer/API token", re.compile(r"\b(sk-ant-|pk1:|gh[pousr]_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})")),
    ("owner state dir", re.compile(r"(?<![A-Za-z0-9_./-])state/")),
    ("dead-man floor surface", re.compile(r"coordination/deadman/")),
    ("user home path", re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/|/home/|(?<![A-Za-z0-9])/[A-Za-z]/Users/)[^\s\\/]+", re.I)),
    ("board private state dir", re.compile(r"\.claude-state\b", re.I)),
    ("subject ledger", re.compile(r"subject-ledger", re.I)),
    ("fleet run receipts", re.compile(r"fleet-runs", re.I)),
    ("hub write-ahead log", re.compile(r"HUB_RUN_WAL", re.I)),
    ("dual-lane ledger", re.compile(r"dual-lane", re.I)),
    ("GPU host name", re.compile(r"ultra[\s_-]?magnus", re.I)),
)


class Refusal(Exception):
    """A schema, Law-4 or contract refusal. Maps to exit code 1."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class GitError(Exception):
    """A git command failed. Maps to exit code 2."""


class Unknown(Exception):
    """The answer cannot be derived (ref/tool/ledger absent). Maps to exit code 2, never 0."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


# ---------- git plumbing ----------

def _run_git(repo: pathlib.Path, args: list[str], input_text: str | None = None,
             extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    if extra_env:
        env.update(extra_env)
    # This tool decides line-ending fidelity for the bytes it appends, byte-prefix-verified
    # in Python before any git call; the host's global core.autocrlf must not silently
    # renormalize a committed CRLF target back to LF underneath that check.
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        input=input_text, timeout=120, env=env,
    )


def git(repo: pathlib.Path, *args: str, input_text: str | None = None,
        extra_env: dict[str, str] | None = None) -> str:
    proc = _run_git(repo, list(args), input_text, extra_env)
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {repo}): {proc.stderr.strip()}")
    return proc.stdout.strip()


def git_try(repo: pathlib.Path, *args: str, extra_env: dict[str, str] | None = None) -> tuple[int, str, str]:
    proc = _run_git(repo, list(args), extra_env=extra_env)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def cat_file_blob(repo: pathlib.Path, ref: str, path: str) -> bytes:
    """The raw bytes of `path` at `ref`, or b"" if it does not exist there (a target
    missing at the tip counts as empty)."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{ref}:{path}"],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0:
        return b""
    return proc.stdout


# ---------- items: parse, screen, key, render ----------

def parse_item(text: str) -> dict:
    """Front matter + body. Refuses on any schema hole; never guesses a default."""
    if not text.startswith("---\n"):
        raise Refusal("ITEM_NO_FRONT_MATTER")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise Refusal("ITEM_UNTERMINATED_FRONT_MATTER")
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise Refusal("ITEM_BAD_FRONT_MATTER_LINE", line)
        k, v = line.split(":", 1)
        meta[k.strip()] = v.strip()
    body = text[end + 5:]
    for k in ("target", "kind", "source_commit", "law4"):
        if k not in meta:
            raise Refusal("ITEM_MISSING_FIELD", k)
    if meta["target"] not in TARGETS:
        raise Refusal("ITEM_BAD_TARGET", f"{meta['target']} not in {TARGETS}; specs are steward-owned")
    if meta["kind"] not in KINDS:
        raise Refusal("ITEM_BAD_KIND", meta["kind"])
    if meta["source_commit"] != "PENDING" and not SHA_RE.match(meta["source_commit"]):
        raise Refusal("ITEM_BAD_SOURCE_COMMIT", meta["source_commit"])
    if "published_as" in meta and not SHA_RE.match(meta["published_as"]):
        raise Refusal("ITEM_BAD_PUBLISHED_AS", meta["published_as"])
    if meta["law4"] != "attested":
        raise Refusal("ITEM_LAW4_NOT_ATTESTED", "law4: attested is required (and is then SCREENED, not trusted)")
    if meta["target"] == "RULINGS.md" and not meta.get("ratified_by"):
        raise Refusal("ITEM_RULING_UNRATIFIED", "a RULINGS.md item needs ratified_by: <RULINGS anchor>; sessions do not mint law")
    if not body.strip():
        raise Refusal("ITEM_EMPTY_BODY")
    if not body.lstrip().startswith("### "):
        raise Refusal("ITEM_BODY_NOT_AN_ENTRY", "body must start with a '### ' heading (bus entry grammar)")
    return {"meta": meta, "body": body}


def _word_boundary_pattern(value: str) -> re.Pattern[str]:
    # `_` is a separator for a *name*, not a word character -- `\b`/the old
    # `[A-Za-z0-9_]` class would let `name_suffix` or `prefix_name` slip past undetected.
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", re.IGNORECASE)


def gather_deny_terms(deny_file: pathlib.Path | None) -> list[tuple[str, str]]:
    """Host name, account name and (optional) deny-file names for the Law-4 screen.
    Computed fresh on every call so tests can monkeypatch env/hostname beforehand."""
    terms: list[tuple[str, str]] = []
    try:
        host = socket.gethostname()
    except OSError:
        host = ""
    if host:
        terms.append(("host name", host))
    computername = os.environ.get("COMPUTERNAME")
    if computername:
        terms.append(("host name", computername))
    for env_name in ("USERNAME", "USER"):
        value = os.environ.get(env_name)
        if value:
            terms.append(("account name", value))
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if home:
        name = pathlib.Path(home).name
        if name:
            terms.append(("account name", name))
    if deny_file is not None and deny_file.is_file():
        for line in deny_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(("deny-list name", line))
    seen: set[tuple[str, str]] = set()
    result: list[tuple[str, str]] = []
    for cls, value in terms:
        if len(value) < 3:
            # A name too short to screen safely (its deny-term regex would match almost
            # anything, or nothing at all) is a fail-closed refusal, never a silent skip
            # -- and the refusal names the CLASS only, never the value.
            raise Refusal("SHORT_IDENTITY_UNSCREENABLE", cls)
        key = (cls, value.lower())
        if key in seen:
            continue
        seen.add(key)
        result.append((cls, value))
    return result


def screen_law4(body: str, deny_terms: list[tuple[str, str]] = ()) -> None:
    words = len(body.split())
    if words > WORD_CAP:
        raise Refusal("LAW4_OVER_CAP", f"{words} words > {WORD_CAP}")
    for name, rx in LAW4:
        m = rx.search(body)
        if m:
            raise Refusal("LAW4_REFUSED", f"{name}: {m.group(0)[:24]!r}")
    for cls, value in deny_terms:
        if _word_boundary_pattern(value).search(body):
            # Never echo the matched value -- the refusal message itself must not leak it.
            raise Refusal("LAW4_REFUSED", cls)


def idempotency_key(source_commit: str, target: str, body: str) -> str:
    return hashlib.sha256(f"{source_commit}\n{target}\n{body}".encode("utf-8")).hexdigest()[:16]


def render_block(item: dict, source_commit: str, project: str = PROJECT) -> tuple[str, str]:
    key = idempotency_key(source_commit, item["meta"]["target"], item["body"])
    body = item["body"].rstrip("\n")
    block = f"{body}\n<!-- outbox:{key} {project}:{source_commit[:12]} -->\n"
    return key, block


def item_heading(item: dict) -> str:
    """The `### ` line an item opens with -- how a published_as claim is looked up."""
    return item["body"].lstrip().splitlines()[0].rstrip()


# ---------- repository queries (committed bytes only, never the worktree) ----------

def require_tool_at_ref(repo: pathlib.Path, ref: str) -> None:
    """Raise Unknown unless `ref` resolves AND carries this tool and the items directory.
    Called before any debt/drain answer, so a checkout that predates the tool can never
    read as 'no debt'."""
    rc, _out, _err = git_try(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if rc != 0:
        raise Unknown("REF_UNRESOLVED", ref)
    rc, _out, _err = git_try(repo, "cat-file", "-e", f"{ref}:{TOOL_REL}")
    if rc != 0:
        raise Unknown("TOOL_ABSENT_AT_REF", f"{TOOL_REL} is not at {ref}")
    rc, out, _err = git_try(repo, "ls-tree", "--name-only", ref, "--", f"{OUTBOX_DIR}/")
    if rc != 0 or not out:
        raise Unknown("TOOL_ABSENT_AT_REF", f"{OUTBOX_DIR}/ is not at {ref}")


def added_in(repo: pathlib.Path, ref: str, path: str) -> str | None:
    """The commit that ADDED this path on `ref`, or None if it cannot be found."""
    out = git(repo, "log", "--diff-filter=A", "--format=%H", "-n", "1", ref, "--", path)
    return out or None


def load_outbox_items(repo: pathlib.Path, ref: str) -> list[dict]:
    """Items committed on `ref` under OUTBOX_DIR. Worktree-only files are invisible on
    purpose: drain and debt both publish/report committed bytes."""
    out = git(repo, "ls-tree", "-r", "--name-only", ref, "--", OUTBOX_DIR)
    items: list[dict] = []
    for p in out.splitlines():
        p = p.strip()
        if not p or "/" not in p or not p.startswith(OUTBOX_DIR + "/"):
            continue
        name = p.rsplit("/", 1)[-1]
        if name == "README.md" and p == f"{OUTBOX_DIR}/README.md":
            continue
        if not ITEM_NAME_RE.match(name):
            items.append({"path": p, "name": name, "error": f"ITEM_BAD_FILENAME: {name}"})
            continue
        text = git(repo, "show", f"{ref}:{p}")
        if not text.endswith("\n"):
            text += "\n"
        try:
            item = parse_item(text)
        except Refusal as e:
            items.append({"path": p, "name": name, "error": str(e)})
            continue
        src = item["meta"]["source_commit"]
        if src == "PENDING":
            src = added_in(repo, ref, p) or "PENDING"
        items.append({"path": p, "name": name, "item": item, "source_commit": src})
    return items


def _commit_time(repo: pathlib.Path, sha: str) -> int:
    return int(git(repo, "show", "-s", "--format=%ct", sha))


def merged_into_ref_time(repo: pathlib.Path, ref: str, added_commit: str, first_parent: set[str]) -> int:
    """When `added_commit` LANDED on `ref`: its own time if it sits on the first-parent line
    (a squash or direct commit), else the first-parent merge that brought it in. The branch
    commit's date is when the item was written, which can precede the merge by days."""
    if added_commit in first_parent:
        return _commit_time(repo, added_commit)
    out = git(repo, "rev-list", "--first-parent", "--ancestry-path", "--reverse", f"{added_commit}..{ref}")
    first = out.splitlines()[0] if out else None
    return _commit_time(repo, first or added_commit)


# ---------- sent ledger ----------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def resolve_ledger(repo: pathlib.Path, ledger: pathlib.Path | None) -> pathlib.Path:
    return ledger.resolve() if ledger is not None else (repo / LEDGER_REL)


def resolve_deny_file(repo: pathlib.Path, deny_file: pathlib.Path | None) -> pathlib.Path:
    return deny_file.resolve() if deny_file is not None else (repo / DENY_FILE_REL)


def read_ledger_keys(ledger: pathlib.Path) -> set[str]:
    if not ledger.is_file():
        return set()
    keys: set[str] = set()
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("key"), str):
            keys.add(row["key"])
    return keys


def append_ledger_rows(ledger: pathlib.Path, rows: list[dict]) -> None:
    if not rows:
        return
    ledger.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    with open(ledger, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ---------- bus interaction ----------

def bus_fetch(bus_repo: pathlib.Path) -> str:
    git(bus_repo, "fetch", "--quiet", "origin", "+refs/heads/master:refs/remotes/origin/master")
    return git(bus_repo, "rev-parse", "refs/remotes/origin/master")


def bus_file_at(bus_repo: pathlib.Path, ref: str, target: str) -> str | None:
    rc, out, _err = git_try(bus_repo, "show", f"{ref}:{target}")
    return out if rc == 0 else None


def key_on_bus(bus_repo: pathlib.Path, ref: str, target: str, key: str) -> bool:
    content = bus_file_at(bus_repo, ref, target)
    return content is not None and f"outbox:{key}" in content


def verify_published_as(bus_repo: pathlib.Path, tip: str, item: dict) -> str | None:
    """None if the item's `published_as` claim holds on the bus; else why not. The sha must
    be an ancestor of the fetched tip and the item's heading must be a whole line of the
    target file AT that sha -- a claim is evidence only once the bus confirms it."""
    sha = item["meta"]["published_as"]
    rc, _out, _err = git_try(bus_repo, "merge-base", "--is-ancestor", sha, tip)
    if rc != 0:
        return f"{sha} is not an ancestor of the bus tip"
    content = bus_file_at(bus_repo, sha, item["meta"]["target"])
    if content is None:
        return f"{item['meta']['target']} does not exist at {sha}"
    heading = item_heading(item)
    if heading not in {line.rstrip() for line in content.splitlines()}:
        return f"heading {heading[:60]!r} is not in {item['meta']['target']} at {sha}"
    return None


def make_temp_worktree(bus_repo: pathlib.Path, tip: str) -> pathlib.Path:
    base = pathlib.Path(tempfile.mkdtemp(prefix="doctrine-outbox-wt-"))
    wt = base / "wt"
    git(bus_repo, "worktree", "add", "--detach", str(wt), tip)
    return wt


def remove_temp_worktree(bus_repo: pathlib.Path, wt: pathlib.Path) -> None:
    try:
        git_try(bus_repo, "worktree", "remove", "--force", str(wt))
    finally:
        git_try(bus_repo, "worktree", "prune")
        shutil.rmtree(wt.parent, ignore_errors=True)


def append_block_to_file(path: pathlib.Path, block: str) -> None:
    """Append `block` after a guaranteed blank line. The bus tip may end with no blank line
    (or with none at all), and a block glued to the previous entry breaks the entry grammar.
    Existing bytes are never rewritten; the file's own line ending (LF or CRLF) is kept."""
    old = path.read_bytes() if path.exists() else b""
    crlf = b"\r\n" in old
    eol = b"\r\n" if crlf else b"\n"
    text = block.replace("\n", "\r\n") if crlf else block
    data = text.encode("utf-8")
    if not old:
        sep = b""
    elif old.endswith(eol + eol):
        sep = b""
    elif old.endswith(eol):
        sep = eol
    else:
        sep = eol + eol
    new = old + sep + data
    if not new.startswith(old):
        raise Refusal("APPEND_NOT_PREFIX", str(path))
    path.write_bytes(new)


def git_commit(worktree: pathlib.Path, message: str) -> str:
    # Pin the bus commit's author/committer to a fixed, non-personal identity -- both via
    # `-c user.*` on the commit invocation AND via GIT_AUTHOR_*/GIT_COMMITTER_* env vars,
    # so neither the worktree's repo config nor any global/system ambient git config can win.
    identity_env = {
        "GIT_AUTHOR_NAME": OUTBOX_IDENTITY_NAME, "GIT_AUTHOR_EMAIL": OUTBOX_IDENTITY_EMAIL,
        "GIT_COMMITTER_NAME": OUTBOX_IDENTITY_NAME, "GIT_COMMITTER_EMAIL": OUTBOX_IDENTITY_EMAIL,
    }
    git(worktree, "add", "-A", extra_env=identity_env)
    git(
        worktree,
        "-c", f"user.name={OUTBOX_IDENTITY_NAME}",
        "-c", f"user.email={OUTBOX_IDENTITY_EMAIL}",
        "-c", "commit.gpgsign=false",
        "commit", "-m", message,
        extra_env=identity_env,
    )
    return git(worktree, "rev-parse", "HEAD")


def _ledger_row(candidate: dict, bus_commit: str) -> dict:
    name = candidate["name"]
    item_name = name[:-3] if name.endswith(".md") else name
    return {
        "key": candidate["key"], "target": candidate["target"], "item": item_name,
        "source_commit": candidate["src"], "bus_commit": bus_commit, "ts": _now_iso(),
    }


# ---------- drain ----------

def drain(repo: pathlib.Path, bus_repo: pathlib.Path, ref: str, ledger: pathlib.Path,
          deny_terms: list[tuple[str, str]], push: bool, max_attempts: int = 3) -> dict:
    require_tool_at_ref(repo, ref)
    items = load_outbox_items(repo, ref)
    ledger_keys = read_ledger_keys(ledger)
    report: dict = {"push": push, "published": [], "already_sent": [], "refused": [], "commit": None, "pushed": False}

    candidates = []
    claimed = []  # items whose front matter says they are already on the bus by hand
    for it in items:
        if "error" in it:
            report["refused"].append({"path": it["path"], "code": "OUTBOX_ITEM_INVALID", "detail": it["error"]})
            continue
        item, src = it["item"], it["source_commit"]
        if src == "PENDING":
            report["refused"].append({"path": it["path"], "code": "OUTBOX_NOT_COMMITTED", "detail": ""})
            continue
        meta = item["meta"]
        if meta["kind"] == "ruling" and not meta.get("ratified_by"):
            report["refused"].append({"path": it["path"], "code": "RULING_UNRATIFIED", "detail": ""})
            continue
        target = meta["target"]
        name_slug = it["name"][:-3] if it["name"].endswith(".md") else it["name"]
        # The filename and the whole commit message are published bytes too -- screen them
        # with the same screen_law4 + deny terms as the body, and refuse BEFORE any commit
        # is built (this candidate is simply dropped, never staged).
        message = f"outbox({PROJECT}): {name_slug} -> {target}\n\nDoctrine-Export: outbox\n"
        try:
            screen_law4(item["body"], deny_terms)
            screen_law4(it["name"], deny_terms)
            screen_law4(message, deny_terms)
        except Refusal as e:
            report["refused"].append({"path": it["path"], "code": e.code, "detail": e.detail})
            continue
        if "published_as" in meta:
            claimed.append({"path": it["path"], "item": item})
            continue
        key, block = render_block(item, src, PROJECT)
        if key in ledger_keys:
            report["already_sent"].append({"path": it["path"], "key": key, "via": "ledger"})
            continue
        candidates.append({"path": it["path"], "name": it["name"], "src": src, "target": target,
                            "key": key, "block": block, "message": message})

    if not candidates and not claimed:
        return report

    tip = bus_fetch(bus_repo)
    for c in claimed:
        why = verify_published_as(bus_repo, tip, c["item"])
        if why is None:
            report["already_sent"].append({"path": c["path"], "via": "published_as",
                                            "published_as": c["item"]["meta"]["published_as"]})
        else:
            report["refused"].append({"path": c["path"], "code": "PUBLISHED_AS_UNVERIFIED", "detail": why})

    if not candidates:
        report["commit"] = tip
        return report

    attempt = 0
    while True:
        attempt += 1
        pending = []
        tip_sent_rows = []
        for c in candidates:
            if key_on_bus(bus_repo, tip, c["target"], c["key"]):
                report["already_sent"].append({"path": c["path"], "key": c["key"], "via": "bus_tip"})
                if push and c["key"] not in ledger_keys:
                    tip_sent_rows.append(_ledger_row(c, tip))
            else:
                pending.append(c)

        if not pending:
            if push and tip_sent_rows:
                append_ledger_rows(ledger, tip_sent_rows)
            report["commit"] = tip
            return report

        wt = make_temp_worktree(bus_repo, tip)
        try:
            commits: dict[str, str] = {}
            for c in pending:
                append_block_to_file(wt / c["target"], c["block"])
                commits[c["key"]] = git_commit(wt, c["message"])
            head = git(wt, "rev-parse", "HEAD")
            # The in-memory prefix check inside append_block_to_file proves nothing about what
            # actually landed in the commit -- a clean filter, attribute, or hook can still
            # rewrite bytes between `write_bytes` and `git add`/`commit`. Read the COMMITTED
            # blobs back, as bytes, and compare those. Runs after the commit and before any
            # push, for every distinct target touched this attempt.
            for target in {c["target"] for c in pending}:
                tip_bytes = cat_file_blob(wt, tip, target)
                head_bytes = cat_file_blob(wt, "HEAD", target)
                if not head_bytes.startswith(tip_bytes):
                    raise Refusal("PREFIX_BROKEN", target)
            if not push:
                report["commit"] = head
                report["would_push"] = [{"path": c["path"], "key": c["key"], "target": c["target"]} for c in pending]
                return report
            rc, _out, err = git_try(wt, "push", "origin", "HEAD:refs/heads/master")
        finally:
            remove_temp_worktree(bus_repo, wt)

        if rc == 0:
            remote = git(bus_repo, "ls-remote", "origin", "refs/heads/master")
            remote_sha = remote.split()[0] if remote else None
            if remote_sha != head:
                raise Refusal("PUSH_VERIFY_FAILED", f"ls-remote={remote_sha!r} head={head!r}")
            rows = [_ledger_row(c, commits[c["key"]]) for c in pending] + tip_sent_rows
            append_ledger_rows(ledger, rows)
            report["published"] = [{"path": c["path"], "key": c["key"], "target": c["target"],
                                     "bus_commit": commits[c["key"]]} for c in pending]
            report["pushed"] = True
            report["commit"] = head
            return report

        if attempt >= max_attempts:
            raise Refusal("PUSH_REJECTED", err[:200])
        tip = bus_fetch(bus_repo)
        # loop again: re-derive `pending` against the fresh tip, so a competitor that already
        # landed one of our exact keys is skipped rather than double-appended.


# ---------- debt ----------

def compute_debt(repo: pathlib.Path, ref: str, ledger: pathlib.Path, bus_repo: pathlib.Path | None = None,
                 now: float | None = None) -> dict:
    """Unsent items on `ref`, with the age of each measured from its merge into `ref`.
    Raises Unknown when `ref` lacks the tool or the items directory."""
    require_tool_at_ref(repo, ref)
    now = time.time() if now is None else now
    items = load_outbox_items(repo, ref)
    ledger_keys = read_ledger_keys(ledger)
    tip = bus_fetch(bus_repo) if bus_repo is not None else None
    first_parent: set[str] | None = None
    unsent: list[dict] = []
    for it in items:
        if "error" in it:
            unsent.append({"path": it["path"], "detail": it["error"], "age_hours": None})
            continue
        item, src = it["item"], it["source_commit"]
        if src == "PENDING":
            unsent.append({"path": it["path"], "detail": "OUTBOX_NOT_COMMITTED (uncommitted item)", "age_hours": None})
            continue
        meta = item["meta"]
        if "published_as" in meta:
            # Debt cannot see the bus unless it was given one. With --bus the claim is verified
            # and a failed claim is debt; without it the tracked claim stands and drain checks it.
            if tip is not None and verify_published_as(bus_repo, tip, item) is not None:
                detail = "published_as claim not confirmed by the bus"
            else:
                continue
        else:
            key = idempotency_key(src, meta["target"], item["body"])
            if key in ledger_keys:
                continue
            if tip is not None and key_on_bus(bus_repo, tip, meta["target"], key):
                continue
            detail = f"unsent target={meta['target']}"
        if first_parent is None:
            first_parent = set(git(repo, "rev-list", "--first-parent", ref).split())
        added = added_in(repo, ref, it["path"]) or src
        age = (now - merged_into_ref_time(repo, ref, added, first_parent)) / 3600.0
        unsent.append({"path": it["path"], "detail": detail, "age_hours": round(age, 2)})
    ages = [u["age_hours"] for u in unsent if u["age_hours"] is not None]
    oldest = max(ages) if ages else None
    return {
        "status": "DEBT" if unsent else "OK", "ref": ref, "count": len(unsent),
        "oldest_age_hours": oldest,
        "stale_over_24h": bool(oldest is not None and oldest > STALE_HOURS),
        "items": unsent,
    }


def cmd_debt(repo: pathlib.Path, ref: str, ledger: pathlib.Path, bus_repo: pathlib.Path | None = None,
             as_json: bool = False, now: float | None = None) -> int:
    try:
        result = compute_debt(repo, ref, ledger, bus_repo, now)
    except Unknown as e:
        # UNKNOWN is a first-class answer: it is printed as such and exits 2, so a heartbeat
        # can never render "could not look" as "nothing owed".
        if as_json:
            print(json.dumps({"status": "UNKNOWN", "code": e.code, "detail": e.detail, "ref": ref}, sort_keys=True))
        else:
            print(f"[doctrine-outbox] debt: UNKNOWN ({e})")
        return 2
    if as_json:
        print(json.dumps(result, sort_keys=True))
        return 1 if result["count"] else 0
    if not result["count"]:
        print(f"[doctrine-outbox] no unsent doctrine items on {ref}")
        return 0
    stale = " STALE (> 24 h)" if result["stale_over_24h"] else ""
    print(f"[doctrine-outbox] {result['count']} unsent doctrine item(s) on {ref}; "
          f"oldest {result['oldest_age_hours']} h since merge{stale}:")
    for u in result["items"]:
        age = "age unknown" if u["age_hours"] is None else f"{u['age_hours']} h"
        print(f"  {u['path']}  ({u['detail']}; {age})")
    return 1


# ---------- check-ledger: the capture check ----------

FINDING_TAG_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?Finding:\s*(?:\*\*)?\s*(KF-\d+)\b")
LEDGER_SECTION_RE = re.compile(r"^##\s+KERNEL FINDINGS\b")
LEDGER_HEADING_RE = re.compile(r"^#{1,2}\s")
SECTION_ITEM_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?(KF-\d+)\b")
DISPOSITION_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?Doctrine-Export:\s*(?:\*\*)?\s*(outbox|none)\b\s*(.*?)\s*$")
OUTBOX_REF_RE = re.compile(r"^(?:" + re.escape(OUTBOX_DIR) + r"/)?\d{8}-[a-z0-9][a-z0-9-]{2,60}\.md(?:@\S+)?$")
NONE_REASON_MIN_WORDS = 4
# Advisory only: these say "look here", never "fail". The structured tag is the gate.
ADVISORY_RES = (
    ("hub error", re.compile(r"hub error", re.I)),
    ("root cause", re.compile(r"root cause", re.I)),
    ("TRAP", re.compile(r"\bTRAP\b")),
)


def judge_disposition(kind: str, rest: str) -> str | None:
    """None when the disposition is well formed; else the reason it is not."""
    if kind == "outbox":
        first = rest.split()[0] if rest.split() else ""
        if not OUTBOX_REF_RE.match(first):
            return "DISPOSITION_OUTBOX_NEEDS_ITEM_FILE"
        return None
    if len(rest.split()) < NONE_REASON_MIN_WORDS:
        return f"DISPOSITION_NONE_NEEDS_REASON_OF_{NONE_REASON_MIN_WORDS}_WORDS"
    return None


def scan_ledger(data: bytes) -> tuple[list[dict], list[dict]]:
    """Every structured finding in the ledger bytes with its disposition state, plus
    advisory keyword hits. A finding's disposition is a `Doctrine-Export:` line AFTER it and
    BEFORE the next finding or the next level-1/2 heading."""
    findings: list[dict] = []
    advisories: list[dict] = []
    current: dict | None = None
    in_section = False
    offset = 0
    for lineno, raw in enumerate(data.splitlines(keepends=True), start=1):
        text = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if LEDGER_HEADING_RE.match(text):
            in_section = bool(LEDGER_SECTION_RE.match(text))
            current = None
        m = FINDING_TAG_RE.match(text) or (SECTION_ITEM_RE.match(text) if in_section else None)
        if m:
            current = {"id": m.group(1), "line": lineno, "offset": offset, "disposed": False, "why": "no Doctrine-Export line after it"}
            findings.append(current)
        else:
            d = DISPOSITION_RE.match(text)
            if d and current is not None and not current["disposed"]:
                why = judge_disposition(d.group(1), d.group(2))
                if why is None:
                    current["disposed"] = True
                    current["why"] = ""
                else:
                    current["why"] = why
        for label, rx in ADVISORY_RES:
            if rx.search(text):
                advisories.append({"line": lineno, "offset": offset, "keyword": label})
                break
        offset += len(raw)
    return findings, advisories


def _safe_boundary(data: bytes) -> int:
    """The end of the last complete line: a half-written final line is never inside a mark."""
    return data.rfind(b"\n") + 1


def resolve_subject_ledger(repo: pathlib.Path, ledger: pathlib.Path | None) -> pathlib.Path:
    if ledger is not None:
        return ledger.resolve()
    env = os.environ.get(SUBJECT_LEDGER_ENV)
    if env:
        return pathlib.Path(env).resolve()
    return repo / SUBJECT_LEDGER_REL


def resolve_watermark(repo: pathlib.Path, watermark: pathlib.Path | None) -> pathlib.Path:
    if watermark is not None:
        return watermark.resolve()
    env = os.environ.get(WATERMARK_ENV)
    if env:
        return pathlib.Path(env).resolve()
    return repo / WATERMARK_REL


def _read_watermark(path: pathlib.Path, data: bytes) -> tuple[int, str | None]:
    """(offset, warning). A watermark whose prefix hash no longer matches means the ledger
    was rewritten under it; the scan restarts from 0 rather than trusting a stale offset."""
    if not path.is_file():
        return 0, None
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        offset, digest = int(row["offset"]), str(row["prefix_sha256"])
    except (OSError, ValueError, KeyError, TypeError):
        return 0, "WATERMARK_UNREADABLE: rescanning from 0"
    if offset < 0 or offset > len(data) or hashlib.sha256(data[:offset]).hexdigest() != digest:
        return 0, "WATERMARK_PREFIX_MISMATCH: ledger was rewritten; rescanning from 0"
    return offset, None


def _write_watermark(path: pathlib.Path, data: bytes, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"offset": offset, "prefix_sha256": hashlib.sha256(data[:offset]).hexdigest(), "ts": _now_iso()}
    path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")


def check_ledger(ledger: pathlib.Path, watermark: pathlib.Path, advance: bool = False,
                 baseline: bool = False) -> tuple[int, list[str]]:
    """(exit code, output lines). 0 = every finding past the watermark is disposed, 1 = at
    least one is not, 2 = the ledger cannot be read (UNKNOWN). Advisory hits never affect
    the code. `advance` moves the watermark to the first undisposed finding (or the end of
    the last complete line if none); `baseline` is the one explicit waiver of history."""
    lines: list[str] = []
    try:
        data = ledger.read_bytes()
    except OSError as e:
        return 2, [f"[doctrine-outbox] check-ledger: UNKNOWN LEDGER_UNREADABLE ({ledger}): {e}"]
    findings, advisories = scan_ledger(data)
    if baseline:
        stop = _safe_boundary(data)
        waived = [f for f in findings if f["offset"] < stop and not f["disposed"]]
        _write_watermark(watermark, data, stop)
        return 0, [f"[doctrine-outbox] check-ledger: BASELINE watermark set at byte {stop}; "
                   f"{len(waived)} undisposed historical finding(s) explicitly waived"]
    start, warning = _read_watermark(watermark, data)
    if warning:
        lines.append(f"[doctrine-outbox] check-ledger: {warning}")
    scanned = [f for f in findings if f["offset"] >= start]
    undisposed = [f for f in scanned if not f["disposed"]]
    lines.append(f"[doctrine-outbox] check-ledger: {len(scanned)} finding(s) past byte {start}, "
                 f"{len(undisposed)} undisposed")
    for f in undisposed:
        lines.append(f"UNDISPOSED {f['id']} at line {f['line']}: {f['why']}")
    for a in advisories:
        if a["offset"] >= start:
            lines.append(f"ADVISORY line {a['line']}: keyword {a['keyword']!r} (advisory only, never failing)")
    if advance:
        new_offset = min((f["offset"] for f in undisposed), default=_safe_boundary(data))
        if new_offset > start:
            _write_watermark(watermark, data, new_offset)
            lines.append(f"[doctrine-outbox] check-ledger: watermark advanced to byte {new_offset}")
    return (1 if undisposed else 0), lines


# ---------- check-commits: the PR trailer check ----------

TRAILER_RE = re.compile(r"^Doctrine-Export:[ \t]*(outbox|none)\b[ \t]*(.*?)[ \t]*$", re.M)
ANY_TRAILER_RE = re.compile(r"^Doctrine-Export:", re.M)


def is_finding_path(path: str) -> bool:
    return path in FINDING_PATH_FILES or path.startswith(FINDING_PATH_PREFIXES)


def range_added_items(repo: pathlib.Path, rev_range: str) -> set[str]:
    out = git(repo, "log", "--no-merges", "--diff-filter=A", "--name-only", "--format=", rev_range, "--", OUTBOX_DIR)
    return {p.strip() for p in out.splitlines() if p.strip()}


def check_commit(repo: pathlib.Path, sha: str, added_items: set[str],
                 deny_terms: list[tuple[str, str]]) -> list[str]:
    """Problems with one commit's `Doctrine-Export:` trailers; empty means it passes."""
    files = git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha).splitlines()
    touches = [f for f in files if is_finding_path(f)]
    message = git(repo, "show", "-s", "--format=%B", sha)
    trailers = TRAILER_RE.findall(message)
    problems: list[str] = []
    if len(ANY_TRAILER_RE.findall(message)) != len(trailers):
        problems.append("BAD_DOCTRINE_EXPORT: a Doctrine-Export line is neither 'outbox <item>' nor 'none <reason>'")
    if touches and not trailers and not problems:
        problems.append(f"MISSING_DOCTRINE_EXPORT: touches {touches[0]}"
                        f"{' (+%d more)' % (len(touches) - 1) if len(touches) > 1 else ''}")
    for kind, rest in trailers:
        if kind == "none":
            if len(rest.split()) < NONE_REASON_MIN_WORDS:
                problems.append(f"DOCTRINE_EXPORT_NONE_NEEDS_REASON_OF_{NONE_REASON_MIN_WORDS}_WORDS")
            continue
        first = rest.split()[0] if rest.split() else ""
        norm = first.replace("\\", "/")
        if not norm.startswith(OUTBOX_DIR + "/"):
            norm = f"{OUTBOX_DIR}/{norm}"
        if not OUTBOX_REF_RE.match(norm) or "@" in norm:
            problems.append(f"DOCTRINE_EXPORT_OUTBOX_NEEDS_ITEM_FILE: {first!r}")
            continue
        if norm not in added_items:
            problems.append(f"DOCTRINE_EXPORT_OUTBOX_ITEM_NOT_ADDED_IN_RANGE: {norm}")
            continue
        rc, text, _err = git_try(repo, "show", f"{sha}:{norm}")
        if rc != 0:
            problems.append(f"DOCTRINE_EXPORT_OUTBOX_ITEM_MISSING_AT_COMMIT: {norm}")
            continue
        try:
            parsed = parse_item(text + "\n")
            screen_law4(parsed["body"], deny_terms)
        except Refusal as e:
            problems.append(f"DOCTRINE_EXPORT_OUTBOX_ITEM_INVALID: {norm}: {e}")
    return problems


def check_commits(repo: pathlib.Path, rev_range: str, deny_terms: list[tuple[str, str]]) -> dict[str, list[str]]:
    """{sha: problems} for every non-merge commit in `rev_range` that fails. Raises Unknown
    when the range cannot be resolved -- an unresolvable range is not a passing one."""
    for end in [e for e in re.split(r"\.{2,3}", rev_range) if e]:
        rc, _out, _err = git_try(repo, "rev-parse", "--verify", "--quiet", f"{end}^{{commit}}")
        if rc != 0:
            raise Unknown("RANGE_UNRESOLVED", rev_range)
    shas = git(repo, "rev-list", "--no-merges", "--reverse", rev_range).split()
    added = range_added_items(repo, rev_range)
    failures: dict[str, list[str]] = {}
    for sha in shas:
        problems = check_commit(repo, sha, added, deny_terms)
        if problems:
            failures[sha] = problems
    return failures


# ---------- CLI commands ----------

def cmd_validate(paths: list[pathlib.Path], deny_terms: list[tuple[str, str]]) -> int:
    ok = True
    for p in paths:
        try:
            # drain refuses a badly named item forever (ITEM_BAD_FILENAME), so validate must too:
            # a file that validates here has to be one the drain will accept.
            if not ITEM_NAME_RE.match(p.name):
                raise Refusal("ITEM_BAD_FILENAME", p.name)
            text = p.read_text(encoding="utf-8")
            if not text.endswith("\n"):
                text += "\n"
            item = parse_item(text)
            screen_law4(item["body"], deny_terms)
        except Refusal as e:
            print(f"[doctrine-outbox] REFUSED {p}: {e}", file=sys.stderr)
            ok = False
            continue
        print(f"[doctrine-outbox] OK {p}: target={item['meta']['target']} kind={item['meta']['kind']}")
    return 0 if ok else 1


def cmd_check_commits(repo: pathlib.Path, rev_range: str, deny_terms: list[tuple[str, str]]) -> int:
    failures = check_commits(repo, rev_range, deny_terms)
    if not failures:
        print(f"[doctrine-outbox] check-commits OK over {rev_range}")
        return 0
    for sha, problems in failures.items():
        for p in problems:
            print(f"[doctrine-outbox] {sha[:12]} {p}", file=sys.stderr)
    return 1


# ---------- argument parsing / entry point ----------

def _default_repo() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="doctrine_outbox", description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=pathlib.Path, default=_default_repo())
    sub = parser.add_subparsers(dest="cmd", required=True)

    validate_p = sub.add_parser("validate", help="parse + screen one or more item files")
    validate_p.add_argument("items", nargs="+", type=pathlib.Path)
    validate_p.add_argument("--deny-file", type=pathlib.Path, default=None)

    debt_p = sub.add_parser("debt", help="list unsent items; exit 1 if any, 2 if UNKNOWN")
    debt_p.add_argument("--ref", default=DEFAULT_REF)
    debt_p.add_argument("--ledger", type=pathlib.Path, default=None, help="the SENT ledger (sent.jsonl)")
    debt_p.add_argument("--bus", type=pathlib.Path, default=None, help="a bus clone: also check markers and published_as")
    debt_p.add_argument("--json", action="store_true")

    drain_p = sub.add_parser("drain", help="publish unsent items to the bus (dry run unless --push)")
    drain_p.add_argument("--bus", required=True, type=pathlib.Path)
    drain_p.add_argument("--ref", default=DEFAULT_REF)
    drain_p.add_argument("--push", action="store_true")
    drain_p.add_argument("--ledger", type=pathlib.Path, default=None, help="the SENT ledger (sent.jsonl)")
    drain_p.add_argument("--deny-file", type=pathlib.Path, default=None)

    ledger_p = sub.add_parser("check-ledger", help="every structured finding needs a Doctrine-Export disposition")
    ledger_p.add_argument("--ledger", type=pathlib.Path, default=None,
                          help=f"the SUBJECT ledger to scan (env {SUBJECT_LEDGER_ENV})")
    ledger_p.add_argument("--watermark", type=pathlib.Path, default=None, help=f"(env {WATERMARK_ENV})")
    mode = ledger_p.add_mutually_exclusive_group()
    mode.add_argument("--advance", action="store_true", help="advance the watermark past disposed findings")
    mode.add_argument("--baseline", action="store_true", help="explicitly waive all history up to now")

    commits_p = sub.add_parser("check-commits", help="commits touching a finding path need a Doctrine-Export trailer")
    commits_p.add_argument("--range", dest="rev_range", default=f"{DEFAULT_REF}..HEAD")
    commits_p.add_argument("--deny-file", type=pathlib.Path, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    try:
        if args.cmd == "validate":
            deny_terms = gather_deny_terms(resolve_deny_file(repo, args.deny_file))
            return cmd_validate(args.items, deny_terms)
        if args.cmd == "debt":
            ledger = resolve_ledger(repo, args.ledger)
            bus = args.bus.resolve() if args.bus is not None else None
            return cmd_debt(repo, args.ref, ledger, bus, args.json)
        if args.cmd == "drain":
            ledger = resolve_ledger(repo, args.ledger)
            deny_terms = gather_deny_terms(resolve_deny_file(repo, args.deny_file))
            report = drain(repo, args.bus.resolve(), args.ref, ledger, deny_terms, args.push)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 1 if report["refused"] else 0
        if args.cmd == "check-ledger":
            code, out = check_ledger(resolve_subject_ledger(repo, args.ledger), resolve_watermark(repo, args.watermark),
                                     advance=args.advance, baseline=args.baseline)
            print("\n".join(out))
            return code
        if args.cmd == "check-commits":
            deny_terms = gather_deny_terms(resolve_deny_file(repo, args.deny_file))
            return cmd_check_commits(repo, args.rev_range, deny_terms)
    except Refusal as e:
        print(f"[doctrine-outbox] {e}", file=sys.stderr)
        return 1
    except Unknown as e:
        print(f"[doctrine-outbox] UNKNOWN {e}", file=sys.stderr)
        return 2
    except GitError as e:
        print(f"[doctrine-outbox] {e}", file=sys.stderr)
        return 2
    print(f"[doctrine-outbox] unknown command {args.cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
