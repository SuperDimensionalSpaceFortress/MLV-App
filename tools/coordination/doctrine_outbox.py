#!/usr/bin/env python3
"""Doctrine outbox: findings reach the fleet doctrine bus by FILE, not by memory.

ONE implementation for the whole fleet (owner P1, 2026-10-16: "the same tool exists in several
versions"; tools/tool-census.mjs counts the drift). A project supplies only DATA, a
`doctrine-outbox.config.json` at its repo root (or `--config`), and never edits this file.
Every project-specific value (project name, default ref, items dir, finding paths, state and
ledger paths, Law-4 deny terms) comes from that config. The config schema is the `_CONFIG_KEYS` set
and `configure()` below; no bus README documents it yet. This file is the candidate for the bus's
`tools/factory/doctrine_outbox.py`. Once the bus steward lands it there, a project can run it from the
bus BY PIN (`factory-pins/pin-runner.py run`, tools/factory/README.md); the pin names a reviewed bus
SHA, so nothing runs until the project reviews and pins it. A project KEEPS its local copy at config
`tool_rel` until a pin-aware release (card OUTBOX-PIN-AWARE-TOOL-AT-REF-1: a config-driven hook
invocation plus a `require_tool_at_ref` that accepts a pinned tool), because the installed hook and the
debt and drain checks still read that path. Until then this file is the only source.

Lineage: the fleet bus's reference outbox -> agent-bridge's port -> a second project's port,
whose behaviours are the superset below. The item format, front matter, `parse_item`, the Law-4
screen shape, `idempotency_key` and `render_block` are byte-compatible across all of them, so
a block reads `<!-- outbox:<key> <project>:<sha12> -->`.

THE FAILURE THIS EXISTS FOR
---------------------------
A directive that lives in a session's head is not a mechanism: a real fix (a parity checker
minting a hard ACCOUNT_MISMATCH on a healthy host) was published only because the owner asked
"did you publish it?". The author writes the finding as a file in the SAME COMMIT as the fix;
scripts check and publish it. No model decides "is this a finding".

BEHAVIOURS
----------
1. REF, never HEAD. `--ref` (config `default_ref`) names the committed state every item / debt /
   age query reads; the worktree and a peer branch are invisible. A ref that lacks this tool or
   the items directory is `TOOL_ABSENT_AT_REF` and debt is UNKNOWN (exit 2), never 0 -- the
   false all-clear a stale checkout would otherwise print.
2. NO RE-APPEND of hand-written bus entries. An item may carry tracked front matter
   `published_as: <bus sha>`; drain verifies that sha is an ancestor of the fetched bus tip AND
   the item's `### ` heading is present in the target at that sha, then skips the item. An
   unverifiable claim is refused, never trusted and never appended.
3. Bus-side idempotency: a key already on the fetched bus tip is never re-appended, even with an
   empty or lost sent ledger (new clone, machine move).
4. Drain guarantees a blank line before every appended block, checks the byte prefix on the
   COMMITTED blobs, retries from scratch on a non-fast-forward, and proves each push with
   `ls-remote`. Debt age is measured from the first-parent merge into `--ref`, not the branch
   commit's date.
5. `check-ledger`: the capture-check primitive. Structured findings (`Finding: KF-<nn>` tags, and
   KF ids inside `## KERNEL FINDINGS`) must each be followed by `Doctrine-Export: outbox <item>`
   or `Doctrine-Export: none <reason, >= 4 words>`. Keyword hits are ADVISORY output only. The
   watermark is a byte offset plus the sha256 of the prefix, advanced only on disposition.
6. `check-commits`: commits touching a finding path (config) need a `Doctrine-Export:` trailer.
   `check-range` is its older spelling. Config `trailer_mode` picks the grammar: `reasoned`
   (default: `none <reason>` / `outbox <item>`) or `bare` (the bus reference's: `none`, or
   `outbox` when the commit itself adds an item).
7. Law 4 is screened mechanically on the body, the filename and the commit message: the universal
   classes below, plus the project's own deny terms (config `law4_extra`), plus the host, account
   and deny-file names gathered at run time.
8. R14.1 (bus RULINGS.md, "a new trap filing is a card"): `target: specs/<project>/cards.md` files
   a `kind: trap` item as a card. Once the fetched bus tip carries `tools/validate-cards.mjs`
   (R14 packet 2), a new TRAPS.md append is refused (`R14_1_TRAP_FILING_IS_A_CARD`); the switch
   is the bus ref, never a date. Before any push the would-be cards file is checked by that
   validator, run with node inside the drain's temp worktree; an absent validator or node refuses
   the card (fail closed).
9. Kernel filing (bus kernel K12 s4; adjudications/factory-kernel/README.md; PROMPT-K s4-5):
   `kernel-filing` publishes `<outbox_dir>/kernel-filing/<yyyymmdd>/` on `--ref` to bus branch
   `review/<project>-kernel-<YYYY-MM-DD>`, never master. It rewrites
   `adjudications/factory-kernel/<project>.md` wholesale and appends an optional block to
   `specs/<project>.md` (a revised block for the same date replaces the earlier one); no other
   path, and no CLI parameter can name a branch or a path. An empty filing or block is refused.
   The branch is created from bus master, or fast-forwarded when every commit on it is this
   outbox's. Every success, including a no-op and a lost-ack recovery, is proven by `ls-remote`
   equal to the certified commit (R7) and recorded in the sent ledger.
10. Hook commands, for a project that wires this file into git and agent hooks: `check` (an item
   unsent past a window is a stuck drain), `pre-push` (refuses a push whose commits fail
   check-commits), `stop` (a Stop-hook nudge over the commits not yet on the remote),
   `session-start` (installs the pre-push guard and reports debt, never blocks), `install-hooks`.
   Each takes the repo as an optional first positional, as git and agent hooks call them. The
   installed hook is made executable (git skips one that is not) and passes git's stdin through to
   whatever layer follows the guard (git lfs pre-push reads the same ref lines). A host with no python
   refuses the push (a guard that cannot run is not a pass), and an older guard block in an existing
   hook is MOVED to the splice point right after the shebang and `set` lines (`install-hooks` prints
   `repaired`), so a layer that reads git's stdin ahead of it cannot starve it. The hook and the debt checks still
   run the file at config `tool_rel`: a project keeps that local copy until a pin-aware release.

Nothing here runs itself: the OS drain task, Stop hook and heartbeat debt are the project's wiring.

Subcommands
-----------
    validate <item.md>...                       parse + screen item files
    debt [--ref R] [--ledger P] [--bus PATH] [--json]
    drain --bus PATH [--ref R] [--push] [--ledger P] [--deny-file P]
    kernel-filing --bus PATH [--ref R] [--push] [--ledger P] [--deny-file P]
    check-ledger [--ledger P] [--watermark P] [--advance | --baseline]
    check-commits [--range A..B]               (alias: check-range; legacy: check-range REPO [A..B])
    check [REPO] [MAX_AGE_MIN]                 exit 1 when an unsent item is older than the window
    pre-push [REPO] [REMOTE URL]               git pre-push hook; ref lines arrive on stdin
    stop [REPO] [--ref R]                      Stop hook; hook JSON arrives on stdin; judges R..HEAD
                                               (default: the config's default_range)
    session-start [REPO]                       install the pre-push guard, report debt; exit 0
    install-hooks [REPO]                       install the pre-push guard only
Global: [--config doctrine-outbox.config.json] [--repo PATH]

Exit codes: 0 = ok. 1 = debt, undisposed findings, or a validation/screen refusal.
2 = UNKNOWN (usage error, git failure, ref, tool or config absent). Refusals print to stderr; a
config that is present but wrong is the refusal CONFIG_INVALID (exit 1, naming the key), never CONFIG_ABSENT.
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
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

# ---------- constants that are fleet-wide, not per project ----------

GUARD_MARKER = "# doctrine-outbox-guard"
GUARD_END = "# end doctrine-outbox-guard"
CONFIG_NAME = "doctrine-outbox.config.json"
CONFIG_ENV = "DOCTRINE_OUTBOX_CONFIG"
CONFIG_SCHEMA = "doctrine-outbox-config.v1"
KINDS = ("receipt", "trap", "ruling")
CARD_VALIDATOR_REL = "tools/validate-cards.mjs"
CARD_VALIDATOR_TIMEOUT_S = 120
# The summary line the tip's validator prints on every run that reaches its main(). Only exit 0 AND this
# line reporting 0 problems is a pass: a node that exits 0 without running the validator prints nothing.
# The grammar is the validator's own summary line (tools/validate-cards.mjs, the last console.log in main():
# "[validate-cards] <files> file(s), <cards> card(s), <noCards> no-card line(s), <bad> problem(s).").
CARD_VALIDATOR_PASS_RE = re.compile(
    r"^\[validate-cards\] \d+ file\(s\), \d+ card\(s\), \d+ no-card line\(s\), 0 problem\(s\)\.$", re.MULTILINE)
# Node reads these before the validator's first line runs, so an inherited value runs foreign code.
CARD_VALIDATOR_STRIPPED_ENV = frozenset({"NODE_OPTIONS", "NODE_PATH"})
REGULAR_FILE_MODE = "100644"
# R14.1 card limits, mirrored from the bus's tools/validate-cards.mjs (MAX_CARD_LINES/MAX_CARD_BYTES);
# a test pins them to the vendored copy. The drain's authority is still that validator.
MAX_CARD_LINES = 15
MAX_CARD_BYTES = 2000
WORD_CAP = 450
ITEM_NAME_RE = re.compile(r"^\d{8}-[a-z0-9][a-z0-9-]{2,60}\.md$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
STALE_HOURS = 24
PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
TRAILER_KEY = "Doctrine-Export"
TRAILER_MODES = ("reasoned", "bare")
BARE_TRAILER_VALUES = ("none", "outbox")
HOOK_CHECK_WINDOW_MIN = 60   # the drain ticks every 15 min; an item unsent for 60 has missed several
HOOK_STOP_WINDOW_S = 24 * 3600

# Law 4 screen on the body: the classes that are banned for EVERY project. Each pattern names the
# class it refuses; the screen prints which, never the matched text. A project's own deny terms
# (private board state, a GPU host name, ...) are DATA in its config (`law4_extra`).
BASE_LAW4 = (
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("account/org uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)),
    ("lane wire path", re.compile(r"coordination/(lanes|comms)/")),
    ("HUB heartbeat", re.compile(r"\bHUB\.md\b")),
    ("owner transcript store", re.compile(r"loops\.json")),
    ("bearer/API token", re.compile(r"\b(sk-ant-|pk1:|gh[pousr]_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})")),
    ("owner state dir", re.compile(r"(?<![A-Za-z0-9_./-])state/")),
    ("dead-man floor surface", re.compile(r"coordination/deadman/")),
    ("user home path", re.compile(r"(?:[A-Za-z]:\\Users\\|/Users/|/home/|(?<![A-Za-z0-9])/[A-Za-z]/Users/)[^\s\\/]+", re.I)),
)

# ---------- per-project values: filled by configure(), never edited by hand ----------

PROJECT = ""
OUTBOX_DIR = "doctrine-outbox"
SENT_DIR = ""        # optional: a directory under OUTBOX_DIR whose items are already sent (moved there)
SENT_PREFIX = ""     # f"{OUTBOX_DIR}/{SENT_DIR}/" when SENT_DIR is set, else ""
TOOL_REL = ""
DEFAULT_REF = ""
DEFAULT_RANGE = ""
CARDS_TARGET = ""
TARGETS: tuple[str, ...] = ("RECEIPTS.md", "TRAPS.md", "RULINGS.md")
CARD_HEADING_RE = re.compile(r"^## $")
CARDS_TITLE = ""
KERNEL_DIR = ""
KERNEL_FILING_SRC = ""
KERNEL_SPEC_BLOCK_SRC = ""
KERNEL_FILING_DEST = ""
KERNEL_SPEC_DEST = ""
KERNEL_SOURCES: dict[str, str] = {}
KERNEL_BRANCH_RE = re.compile(r"$^")
OUTBOX_IDENTITY_NAME = ""
OUTBOX_IDENTITY_EMAIL = ""
LEDGER_REL = pathlib.Path()
DENY_FILE_REL = pathlib.Path()
SUBJECT_LEDGER_REL: pathlib.Path | None = None
WATERMARK_REL = pathlib.Path()
SUBJECT_LEDGER_ENV = "DOCTRINE_OUTBOX_LEDGER"
WATERMARK_ENV = "DOCTRINE_OUTBOX_WATERMARK"
FINDING_PATH_PREFIXES: tuple[str, ...] = ()
FINDING_PATH_FILES: tuple[str, ...] = ()
FINDING_PATH_REGEXES: tuple[re.Pattern[str], ...] = ()
TRAILER_MODE = "reasoned"
PRE_PUSH_REFS: tuple[str, ...] = ("refs/heads/master",)
LAW4 = BASE_LAW4
OUTBOX_REF_RE = re.compile(r"$^")
CONFIG_PATH: pathlib.Path | None = None
CONFIG_ROOT: pathlib.Path | None = None
CONFIG_ERROR: Refusal | None = None   # why the config found at import was refused; None once configure() succeeds

_CONFIG_KEYS = frozenset({
    "schema", "project", "tool_rel", "default_ref", "default_range", "outbox_dir", "sent_dir", "cards_target",
    "finding_path_prefixes", "finding_path_files", "finding_path_regexes", "trailer_mode", "pre_push_refs",
    "law4_extra", "state_dir", "ledger_rel", "deny_file_rel", "subject_ledger_rel", "watermark_rel",
    "subject_ledger_env", "watermark_env", "identity_name", "identity_email", "bus_candidate",
})


_LAW4_ROW_KEYS = frozenset({"name", "pattern", "ignorecase"})


class Refusal(Exception):
    """A schema, Law-4 or contract refusal. Maps to exit code 1."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


class GitError(Exception):
    """A git command failed. Maps to exit code 2."""


class Unknown(Exception):
    """The answer cannot be derived (ref/tool/ledger/config absent). Maps to exit code 2, never 0."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail


# ---------- configuration ----------

def _str_list(cfg: dict, key: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = cfg.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise Refusal("CONFIG_INVALID", f"{key} must be a list of non-empty strings")
    return tuple(value)


def _rel_value(value: object, key: str) -> pathlib.Path:
    if not isinstance(value, str) or not value:
        raise Refusal("CONFIG_INVALID", f"{key} must be a non-empty string")
    parts = pathlib.PurePosixPath(value.replace("\\", "/")).parts
    if pathlib.PurePosixPath(value.replace("\\", "/")).is_absolute() or ".." in parts or not parts:
        raise Refusal("CONFIG_INVALID", f"{key} must be a relative path inside the repo: {value}")
    return pathlib.Path(*parts)


def _state_rel(cfg: dict, key: str, leaf: str) -> pathlib.Path:
    """A state file's repo-relative path: its own key if given, else `state_dir`/`leaf`. Neither is
    a refusal -- the tool names no default location, so no project's layout is baked into it."""
    if key in cfg:
        return _rel_value(cfg[key], key)
    if "state_dir" in cfg:
        return _rel_value(cfg["state_dir"], "state_dir") / leaf
    raise Refusal("CONFIG_INVALID", f"{key} or state_dir is required")


def load_config(path: pathlib.Path) -> dict:
    """The parsed config, schema-checked. An unknown key is refused (a typo must not silently drop a
    deny term); keys starting with `_` are comments."""
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise Refusal("CONFIG_INVALID", f"{path}: {type(e).__name__}: {e}") from None
    if not isinstance(cfg, dict):
        raise Refusal("CONFIG_INVALID", f"{path}: top level must be an object")
    if cfg.get("schema") != CONFIG_SCHEMA:
        raise Refusal("CONFIG_INVALID", f"{path}: schema must be {CONFIG_SCHEMA!r}")
    unknown = sorted(k for k in cfg if k not in _CONFIG_KEYS and not k.startswith("_"))
    if unknown:
        raise Refusal("CONFIG_INVALID", f"{path}: unknown key(s) {', '.join(unknown)}")
    return cfg


def configure(cfg: dict, config_path: pathlib.Path | None = None) -> None:
    """Install one project's values into the module. Atomic: every value is validated and built
    first, and the module is rebound only when all of them are good."""
    for key in ("project", "tool_rel", "default_ref"):
        if not isinstance(cfg.get(key), str) or not cfg[key]:
            raise Refusal("CONFIG_INVALID", f"{key} is required")
    project = cfg["project"]
    if not PROJECT_RE.match(project):
        raise Refusal("CONFIG_INVALID", f"project must match {PROJECT_RE.pattern}")
    dir_re = r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*"
    def plain_dir(value: object) -> bool:
        # `.` and `..` match the character class, so the regex alone lets `../x` climb out of the repo.
        return isinstance(value, str) and bool(re.fullmatch(dir_re, value)) and not {".", ".."} & set(value.split("/"))

    outbox_dir = cfg.get("outbox_dir", "doctrine-outbox")
    if not plain_dir(outbox_dir):
        raise Refusal("CONFIG_INVALID", "outbox_dir must be a relative posix path with no . or .. segment")
    sent_dir = cfg.get("sent_dir", "")
    if not isinstance(sent_dir, str) or (sent_dir and not plain_dir(sent_dir)):
        raise Refusal("CONFIG_INVALID", "sent_dir must be a relative posix path under outbox_dir with no . or .. segment")
    cards_target = cfg.get("cards_target", f"specs/{project}/cards.md")
    if not isinstance(cards_target, str) or not cards_target.startswith("specs/") or ".." in cards_target:
        raise Refusal("CONFIG_INVALID", "cards_target must live under specs/")
    trailer_mode = cfg.get("trailer_mode", "reasoned")
    if trailer_mode not in TRAILER_MODES:
        raise Refusal("CONFIG_INVALID", f"trailer_mode must be one of {TRAILER_MODES}")
    default_range = cfg.get("default_range", f"{cfg['default_ref']}..HEAD")
    if not isinstance(default_range, str) or ".." not in default_range:
        raise Refusal("CONFIG_INVALID", "default_range must be a git range A..B")
    law4 = list(BASE_LAW4)
    extra = cfg.get("law4_extra", [])
    if not isinstance(extra, list):
        raise Refusal("CONFIG_INVALID", "law4_extra must be a list of {name, pattern, ignorecase?}")
    for row in extra:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not isinstance(row.get("pattern"), str):
            raise Refusal("CONFIG_INVALID", "each law4_extra row needs a string name and pattern")
        stray = sorted(k for k in row if k not in _LAW4_ROW_KEYS and not k.startswith("_"))
        if stray or not isinstance(row.get("ignorecase", False), bool):
            # A typo such as `ignorecas` would silently turn a case-insensitive deny term case-sensitive.
            raise Refusal("CONFIG_INVALID", f"law4_extra {row['name']!r}: "
                          + (f"unknown key(s) {', '.join(stray)}" if stray else "ignorecase must be true or false"))
        try:
            law4.append((row["name"], re.compile(row["pattern"], re.I if row.get("ignorecase") else 0)))
        except re.error as e:
            raise Refusal("CONFIG_INVALID", f"law4_extra {row['name']!r}: {e}") from None
    finding_regexes = []
    for pattern in _str_list(cfg, "finding_path_regexes"):
        try:
            finding_regexes.append(re.compile(pattern))
        except re.error as e:
            raise Refusal("CONFIG_INVALID", f"finding_path_regexes {pattern!r}: {e}") from None
    identity_name = cfg.get("identity_name", f"{project} doctrine outbox")
    identity_email = cfg.get("identity_email", f"outbox@{project}.invalid")
    env_stem = project.upper().replace("-", "_")
    values = {
        "PROJECT": project,
        "OUTBOX_DIR": outbox_dir,
        "SENT_DIR": sent_dir,
        "SENT_PREFIX": f"{outbox_dir}/{sent_dir}/" if sent_dir else "",
        "TOOL_REL": cfg["tool_rel"],
        "DEFAULT_REF": cfg["default_ref"],
        "DEFAULT_RANGE": default_range,
        "CARDS_TARGET": cards_target,
        "TARGETS": ("RECEIPTS.md", "TRAPS.md", "RULINGS.md", cards_target),
        "CARD_HEADING_RE": re.compile(r"^## " + re.escape(project) + r"/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
        "CARDS_TITLE": f"# {project} cards (R14.1; written only by {project})\n\n",
        "KERNEL_DIR": f"{outbox_dir}/kernel-filing",
        "KERNEL_FILING_SRC": f"adjudications-factory-kernel-{project}.md",
        "KERNEL_SPEC_BLOCK_SRC": f"specs-{project}-block.md",
        "KERNEL_FILING_DEST": f"adjudications/factory-kernel/{project}.md",
        "KERNEL_SPEC_DEST": f"specs/{project}.md",
        "KERNEL_BRANCH_RE": re.compile(r"review/" + re.escape(project) + r"-kernel-[0-9]{4}-[0-9]{2}-[0-9]{2}"),  # fullmatch only
        "OUTBOX_IDENTITY_NAME": identity_name,
        "OUTBOX_IDENTITY_EMAIL": identity_email,
        "LEDGER_REL": _state_rel(cfg, "ledger_rel", "sent.jsonl"),
        "DENY_FILE_REL": _state_rel(cfg, "deny_file_rel", "deny-names.txt"),
        "SUBJECT_LEDGER_REL": _rel_value(cfg["subject_ledger_rel"], "subject_ledger_rel") if "subject_ledger_rel" in cfg else None,
        "WATERMARK_REL": _state_rel(cfg, "watermark_rel", "ledger-watermark.json"),
        "SUBJECT_LEDGER_ENV": cfg.get("subject_ledger_env", f"{env_stem}_OUTBOX_LEDGER"),
        "WATERMARK_ENV": cfg.get("watermark_env", f"{env_stem}_OUTBOX_WATERMARK"),
        "FINDING_PATH_PREFIXES": _str_list(cfg, "finding_path_prefixes"),
        "FINDING_PATH_FILES": _str_list(cfg, "finding_path_files"),
        "FINDING_PATH_REGEXES": tuple(finding_regexes),
        "TRAILER_MODE": trailer_mode,
        "PRE_PUSH_REFS": _str_list(cfg, "pre_push_refs", ("refs/heads/master",)),
        "LAW4": tuple(law4),
        "OUTBOX_REF_RE": re.compile(r"^(?:" + re.escape(outbox_dir) + r"/)?\d{8}-[a-z0-9][a-z0-9-]{2,60}\.md(?:@\S+)?$"),
        "CONFIG_PATH": config_path.resolve() if config_path is not None else None,
        "CONFIG_ROOT": config_path.resolve().parent if config_path is not None else None,
        "CONFIG_ERROR": None,
    }
    values["KERNEL_SOURCES"] = {values["KERNEL_FILING_SRC"]: values["KERNEL_FILING_DEST"],
                                values["KERNEL_SPEC_BLOCK_SRC"]: values["KERNEL_SPEC_DEST"]}
    for name in (values["SUBJECT_LEDGER_ENV"], values["WATERMARK_ENV"]):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise Refusal("CONFIG_INVALID", f"environment variable name {name!r} is not UPPER_SNAKE")
    globals().update(values)


def find_config(start: pathlib.Path) -> pathlib.Path | None:
    """The config for a tool file at `start`: $DOCTRINE_OUTBOX_CONFIG, else the nearest
    doctrine-outbox.config.json walking up from `start`, stopping at the first directory that is a
    git root (so a tool never adopts a config from outside its own repository)."""
    env = os.environ.get(CONFIG_ENV)
    if env:
        return pathlib.Path(env)
    for directory in (start, *list(start.parents)[:5]):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
        if (directory / ".git").exists():
            return None
    return None


def _autoconfigure() -> None:
    """Import-time: the tool finds its own project's config. A broken config is not fatal at
    import (the module stays unconfigured and main() reports it); nothing is guessed. The refusal is
    kept in CONFIG_ERROR so require_configured() reports a config that is present but wrong as
    CONFIG_INVALID, never as the CONFIG_ABSENT of a missing one."""
    global CONFIG_ERROR
    try:
        found = find_config(pathlib.Path(__file__).resolve().parent)
        if found is not None:
            configure(load_config(found), found)
    except Refusal as e:
        CONFIG_ERROR = e


def require_configured() -> None:
    if not PROJECT:
        if CONFIG_ERROR is not None:
            raise Refusal(CONFIG_ERROR.code, CONFIG_ERROR.detail)
        raise Unknown("CONFIG_ABSENT", f"no {CONFIG_NAME} found (pass --config, set ${CONFIG_ENV}, or keep it at the repo root)")


_autoconfigure()


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


def git_paths(repo: pathlib.Path, *args: str) -> list[str]:
    """The paths a git path-list command prints, NUL-separated (`-z`). Without it git quotes a
    non-ASCII path (`"agents/caf\\303\\251.md"`, core.quotepath), which matches no prefix or set
    entry, so a path test on the plain output silently misses it (bus de09cae)."""
    proc = _run_git(repo, [args[0], "-z", *args[1:]])  # before any `--`, or git reads -z as a pathspec
    if proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} (in {repo}): {proc.stderr.strip()}")
    return [p for p in proc.stdout.split("\0") if p]


def cat_file_blob(repo: pathlib.Path, ref: str, path: str) -> bytes:
    """The raw bytes of `path` at `ref`, or b"" if it does not exist there (a target
    missing at the tip counts as empty). A failed read is NOT absence: `git cat-file` exits 128 both
    for a missing path and for a bad ref, a non-blob or an unreadable object, so when it fails the
    path is looked up with `git ls-tree`, which exits 0 with no output only when `ref` is readable
    and carries no such path. Anything else raises Refusal TIP_BLOB_READ_FAILED, so a read failure
    is never mistaken for an empty file."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{ref}:{path}"],
        capture_output=True, timeout=120,
    )
    if proc.returncode == 0:
        return proc.stdout
    listing = _run_git(repo, ["ls-tree", "-z", "--name-only", ref, "--", path])
    if listing.returncode == 0 and not listing.stdout:
        return b""
    why = proc.stderr.decode("utf-8", errors="replace").strip() or listing.stderr.strip() or "the path is listed but did not read as a blob"
    raise Refusal("TIP_BLOB_READ_FAILED", f"{ref[:12]}:{path}: {why[:300]}")


# ---------- items: parse, screen, key, render ----------

def parse_item(text: str) -> dict:
    """Front matter + body. Refuses on any schema hole; never guesses a default."""
    require_configured()
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
        raise Refusal("ITEM_BAD_TARGET", f"{meta['target']} not in {TARGETS}; other specs are steward-owned")
    if meta["kind"] not in KINDS:
        raise Refusal("ITEM_BAD_KIND", meta["kind"])
    if meta["target"] == CARDS_TARGET and meta["kind"] != "trap":
        raise Refusal("ITEM_CARD_NOT_A_TRAP", f"{CARDS_TARGET} holds trap cards only (R14.1); a {meta['kind']} keeps its own target")
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
    if meta["target"] == CARDS_TARGET:
        if not CARD_HEADING_RE.match(body.lstrip().splitlines()[0].rstrip()):
            raise Refusal("ITEM_BODY_NOT_A_CARD", f"body must start with a '## {PROJECT}/<id>' card heading "
                                                  f"(R14.1; format in the bus's {CARD_VALIDATOR_REL})")
    elif not body.lstrip().startswith("### "):
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


def screen_law4(body: str, deny_terms: list[tuple[str, str]] = (), word_cap: int | None = WORD_CAP) -> None:
    words = len(body.split())
    if word_cap is not None and words > word_cap:
        raise Refusal("LAW4_OVER_CAP", f"{words} words > {word_cap}")
    for name, rx in LAW4:
        if rx.search(body):
            # Name the CLASS only: the matched text is exactly what must not leave the machine.
            raise Refusal("LAW4_REFUSED", name)
    for cls, value in deny_terms:
        if _word_boundary_pattern(value).search(body):
            # Never echo the matched value -- the refusal message itself must not leak it.
            raise Refusal("LAW4_REFUSED", cls)


def idempotency_key(source_commit: str, target: str, body: str) -> str:
    return hashlib.sha256(f"{source_commit}\n{target}\n{body}".encode("utf-8")).hexdigest()[:16]


def render_block(item: dict, source_commit: str, project: str | None = None) -> tuple[str, str]:
    project = project or PROJECT
    key = idempotency_key(source_commit, item["meta"]["target"], item["body"])
    body = item["body"].rstrip("\n")
    if item["meta"]["target"] == CARDS_TARGET:
        # Every line of a card is a field, a continuation or a bold-bullet note; the validator
        # refuses an HTML comment there, so the marker is a note line. It counts toward the
        # card's 15-line and 2,000-byte limits, which the validator checks before any push.
        return key, f"{body}\n- **Outbox:** outbox:{key} {project}:{source_commit[:12]}\n"
    block = f"{body}\n<!-- outbox:{key} {project}:{source_commit[:12]} -->\n"
    return key, block


def item_heading(item: dict) -> str:
    """The `### ` line an item opens with -- how a published_as claim is looked up."""
    return item["body"].lstrip().splitlines()[0].rstrip()


def screen_rendered_card(item: dict) -> None:
    """Refuse a card whose RENDERED block (what the drain appends: body plus the Outbox note) is over
    the validator's limits, so it fails at authoring time and not after it merges. The drain renders
    with the resolved 40-hex source commit, so PENDING is judged at that full width."""
    if item["meta"]["target"] != CARDS_TARGET:
        return
    src = item["meta"]["source_commit"]
    _key, block = render_block(item, "0" * 40 if src == "PENDING" else src)
    rows = block.replace("\r\n", "\n").strip("\n").split("\n")
    size = len("\n".join(rows).encode("utf-8"))
    if len(rows) > MAX_CARD_LINES:
        raise Refusal("CARD_INVALID", f"rendered card is {len(rows)} lines; the limit is {MAX_CARD_LINES}")
    if size > MAX_CARD_BYTES:
        raise Refusal("CARD_INVALID", f"rendered card is {size} bytes; the limit is {MAX_CARD_BYTES}")


# ---------- repository queries (committed bytes only, never the worktree) ----------

def require_tool_at_ref(repo: pathlib.Path, ref: str) -> None:
    """Raise Unknown unless `ref` resolves AND carries this tool and the items directory.
    Called before any debt/drain answer, so a checkout that predates the tool can never
    read as 'no debt'."""
    require_configured()
    rc, _out, _err = git_try(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if rc != 0:
        raise Unknown("REF_UNRESOLVED", ref)
    rc, _out, _err = git_try(repo, "cat-file", "-e", f"{ref}:{TOOL_REL}")
    if rc != 0:
        raise Unknown("TOOL_ABSENT_AT_REF", f"{TOOL_REL} is not at {ref}")
    rc, out, _err = git_try(repo, "ls-tree", "--name-only", "-z", ref, "--", f"{OUTBOX_DIR}/")
    if rc != 0 or not out:
        raise Unknown("TOOL_ABSENT_AT_REF", f"{OUTBOX_DIR}/ is not at {ref}")


def added_in(repo: pathlib.Path, ref: str, path: str) -> str | None:
    """The commit that ADDED this path on `ref`, or None if it cannot be found."""
    out = git(repo, "log", "--diff-filter=A", "--format=%H", "-n", "1", ref, "--", path)
    return out or None


def load_outbox_items(repo: pathlib.Path, ref: str) -> list[dict]:
    """Items committed on `ref` under OUTBOX_DIR. Worktree-only files are invisible on
    purpose: drain and debt both publish/report committed bytes. Items already moved under the
    project's `sent_dir` (when it sets one) are sent by construction and are not loaded."""
    items: list[dict] = []
    for p in git_paths(repo, "ls-tree", "-r", "--name-only", ref, "--", OUTBOX_DIR):
        if not p or "/" not in p or not p.startswith(OUTBOX_DIR + "/") or p.startswith(KERNEL_DIR + "/"):
            continue
        if SENT_PREFIX and p.startswith(SENT_PREFIX):
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


def is_ancestor(repo: pathlib.Path, sha: str, tip: str) -> bool:
    rc, _out, _err = git_try(repo, "merge-base", "--is-ancestor", sha, tip)
    return rc == 0


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


# ---------- R14.1: cards ----------

def _node_exclusion_roots(*extra: pathlib.Path | None) -> list[str]:
    """The directories no validator interpreter may live in or under: this process's cwd (a bare
    `node` lookup checks it first on win32) and every bus checkout handed in. Fetched bus content
    is data, never a source of the interpreter that judges it."""
    return [os.getcwd(), *(str(r) for r in extra if r is not None)]


def _node_candidates(entry: str) -> list[str]:
    if sys.platform != "win32":
        return [os.path.join(entry, "node")]
    exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    return [os.path.join(entry, "node" + e) for e in exts]


def _inside_any(path: str, roots: list[str]) -> bool:
    """True if `path` is a root or lies under one, judged by file identity (samefile on every
    ancestor of the real path), never by comparing spellings. A root that cannot be stat'ed raises."""
    real = pathlib.Path(os.path.realpath(path))
    return any(os.path.samefile(a, r) for a in (real, *real.parents) for r in roots)


def find_node(*bus_roots: pathlib.Path | None) -> str | None:
    """The `node` the card validator runs under, as an ABSOLUTE path (resolved against this process's cwd
    at lookup time, and identity-checked in that same form), found by walking PATH entries only: never the cwd
    (shutil.which looks there first on win32, so a planted node.exe would win) and never anything
    inside the cwd or a bus checkout, compared by file identity. None means no node on PATH at all.
    Raises Refusal CARD_VALIDATOR_NODE_ABSENT when node exists only at excluded places, and
    CARD_VALIDATOR_NODE_COULD_NOT_CHECK when the exclusions cannot be computed (fail closed)."""
    try:
        roots = _node_exclusion_roots(*bus_roots)
        excluded = None
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            entry = entry.strip().strip('"')
            if not entry or not os.path.isdir(entry):
                continue
            entry = os.path.abspath(entry)  # absolute now: the child runs under another cwd, so a relative PATH entry would name a different file there
            for cand in _node_candidates(entry):
                if not (os.path.isfile(cand) and os.access(cand, os.X_OK)):
                    continue
                if _inside_any(cand, roots):
                    excluded = excluded or cand
                    continue
                return cand
    except Refusal:
        raise
    except Exception as e:  # noqa: BLE001 -- any failure here is "could not check", never a pass
        raise Refusal("CARD_VALIDATOR_NODE_COULD_NOT_CHECK",
                      f"{type(e).__name__} while checking which node may run {CARD_VALIDATOR_REL}; a card is never published unvalidated") from e
    if excluded is not None:
        raise Refusal("CARD_VALIDATOR_NODE_ABSENT",
                      f"the only node on PATH is inside the cwd or a bus checkout ({excluded}), which is never run as the "
                      f"validator interpreter; a card is never published unvalidated")
    return None


def card_validator_on_bus(bus_repo: pathlib.Path, tip: str) -> bool:
    """R14.1 took effect at the bus commit that landed the card validator (packet 2), so its
    presence AT THE FETCHED TIP is the switch -- never a calendar date (R14.6)."""
    rc, _out, _err = git_try(bus_repo, "cat-file", "-e", f"{tip}:{CARD_VALIDATOR_REL}")
    return rc == 0


def r14_gate(bus_repo: pathlib.Path, tip: str, candidates: list[dict], report: dict) -> list[dict]:
    """The candidates that may be appended at `tip`; the rest go to report["refused"]. A key
    already on the tip is kept, so the drain reports it as sent rather than refusing history."""
    validator = card_validator_on_bus(bus_repo, tip)
    has_cards = any(c["target"] == CARDS_TARGET for c in candidates)
    node, node_refusal = None, None
    if has_cards:
        try:
            node = find_node(bus_repo)
        except Refusal as e:
            node_refusal = (e.code, e.detail)
    entry_problem = cards_tip_entry_problem(bus_repo, tip) if has_cards and validator else None
    kept = []
    for c in candidates:
        refusal = None
        if key_on_bus(bus_repo, tip, c["target"], c["key"]):
            pass
        elif c["target"] == "TRAPS.md" and validator:
            refusal = ("R14_1_TRAP_FILING_IS_A_CARD",
                       f"R14.1: the bus at {tip[:12]} carries {CARD_VALIDATOR_REL}, so a new trap filing is a card "
                       f"in {CARDS_TARGET}, not a TRAPS.md entry; retarget the item (target: {CARDS_TARGET}, "
                       f"body opening '## {PROJECT}/<id>')")
        elif c["target"] == CARDS_TARGET and not validator:
            refusal = ("CARD_VALIDATOR_ABSENT",
                       f"{CARD_VALIDATOR_REL} is not on the bus at {tip[:12]}; a card is never published unvalidated")
        elif c["target"] == CARDS_TARGET and node_refusal:
            refusal = node_refusal
        elif c["target"] == CARDS_TARGET and node is None:
            refusal = ("CARD_VALIDATOR_NODE_ABSENT",
                       f"node is not on PATH, so {CARD_VALIDATOR_REL} cannot run; a card is never published unvalidated")
        elif c["target"] == CARDS_TARGET and entry_problem:
            refusal = ("CARDS_PATH_NOT_REGULAR_FILE", f"{entry_problem}; a card is never written through a link or a non-regular entry")
        if refusal is None:
            kept.append(c)
        else:
            report["refused"].append({"path": c["path"], "code": refusal[0], "detail": refusal[1]})
    return kept


def ensure_cards_file(path: pathlib.Path) -> None:
    """R14.7: the project creates its own cards file on its first card."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(CARDS_TITLE.encode("utf-8"))


def validate_cards_file(wt: pathlib.Path, cards_file: pathlib.Path | None = None,
                        bus_repo: pathlib.Path | None = None) -> None:
    """Run the tip's own validator over the would-be cards file in the drain's temp worktree.
    It only reads. A pass is exit 0 AND the validator's summary line reporting 0 problems; exit 1
    is the one per-card verdict (CARD_INVALID); every other outcome, a silent exit 0 included,
    refuses the publish as CARD_VALIDATOR_FAILED (fail closed). The child runs in a fresh empty
    directory of its own: not the cwd, not a bus checkout."""
    node = find_node(wt, bus_repo)
    if node is None:
        raise Refusal("CARD_VALIDATOR_NODE_ABSENT", f"node is not on PATH, so {CARD_VALIDATOR_REL} cannot run")
    if not (wt / CARD_VALIDATOR_REL).is_file():
        raise Refusal("CARD_VALIDATOR_ABSENT", f"{CARD_VALIDATOR_REL} is not in the bus worktree")
    env = {k: v for k, v in os.environ.items() if k.upper() not in CARD_VALIDATOR_STRIPPED_ENV}
    try:
        with tempfile.TemporaryDirectory(prefix="doctrine-outbox-node-cwd-") as child_cwd:
            proc = subprocess.run(
                [node, str(wt / CARD_VALIDATOR_REL), "--bus", str(wt), "--file", str(cards_file or wt / CARDS_TARGET), "--project", PROJECT],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=CARD_VALIDATOR_TIMEOUT_S, env=env,
                cwd=child_cwd,
            )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Refusal("CARD_VALIDATOR_FAILED", type(e).__name__) from e
    out = (proc.stdout + proc.stderr).strip()
    if proc.returncode == 1:
        raise Refusal("CARD_INVALID", out[:1000])
    if proc.returncode != 0:
        raise Refusal("CARD_VALIDATOR_FAILED", f"exit {proc.returncode}: {out[:400]}")
    if not CARD_VALIDATOR_PASS_RE.search(proc.stdout):
        raise Refusal("CARD_VALIDATOR_FAILED", f"exit 0 without a '0 problem(s)' summary line: {out[:400] or 'no output'}")


def cards_tip_entry_problem(bus_repo: pathlib.Path, tip: str) -> str | None:
    """Why the tip's cards-path entry may not be written through, or None. Only a missing entry
    (the project's first card) or a regular 100644 blob qualifies: a 120000 link, a 160000 gitlink
    or a 100755 file is refused, since the drain writes through the checkout of that path."""
    proc = _run_git(bus_repo, ["ls-tree", "-z", tip, "--", CARDS_TARGET])
    if proc.returncode != 0:
        raise Refusal("TIP_BLOB_READ_FAILED", f"{tip[:12]}:{CARDS_TARGET}: ls-tree: {proc.stderr.strip()[:300]}")
    for entry in proc.stdout.split("\0"):
        meta, _tab, path = entry.partition("\t")
        if not entry or path != CARDS_TARGET:
            continue
        fields = meta.split()
        if fields[:2] != [REGULAR_FILE_MODE, "blob"]:
            return f"{CARDS_TARGET} at {tip[:12]} is mode {fields[0] if fields else '?'}, not a regular {REGULAR_FILE_MODE} blob"
    return None


def assert_no_link_in_path(root: pathlib.Path, rel: str) -> None:
    """Refuse when `rel` under `root`, or any directory on the way to it, is a symlink or a Windows
    junction (a reparse point): a write there lands wherever the link points. Never follows one."""
    cur = root
    for part in pathlib.PurePosixPath(rel).parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            raise Refusal("CARDS_PATH_IS_LINK", f"{cur.relative_to(root).as_posix()} is a link or junction in the bus worktree")


def invalid_cards(wt: pathlib.Path, tip: str, cards: list[dict],
                  bus_repo: pathlib.Path | None = None) -> list[tuple[dict, str, str]]:
    """The cards to refuse, each as (card, code, validator message). Judged by the same tip validator
    on a file outside the repo that starts as the cards file AT THE BUS TIP -- exactly the base the
    batch validation used -- and takes the cards in drain order, keeping each one that passes: a
    card is judged as (tip + the cards kept before it + that card). A card that clashes with the tip
    or with an earlier pending card (duplicate id, any cross-card rule) is therefore named and
    refused CARD_INVALID alone; the later-sorted of two clashing pending cards is the one refused.
    A tip file the validator already rejects names no card: every card gets CARD_INVALID_UNATTRIBUTED
    and nothing is published into it. A verdict other than valid/invalid (node gone, timeout, crash)
    is not a verdict on any card and propagates."""
    tip_bytes = cat_file_blob(wt, tip, CARDS_TARGET)
    with tempfile.TemporaryDirectory(prefix="doctrine-outbox-card-") as tmp:
        trial = pathlib.Path(tmp) / "cards.md"
        if tip_bytes:
            trial.write_bytes(tip_bytes)
            try:
                validate_cards_file(wt, trial, bus_repo)
            except Refusal as e:
                if e.code != "CARD_INVALID":
                    raise
                return [(c, "CARD_INVALID_UNATTRIBUTED", e.detail) for c in cards]
        else:
            ensure_cards_file(trial)
        kept = trial.read_bytes()
        bad = []
        for c in cards:
            trial.write_bytes(kept)
            append_block_to_file(trial, c["block"])
            try:
                validate_cards_file(wt, trial, bus_repo)
            except Refusal as e:
                if e.code != "CARD_INVALID":
                    raise
                bad.append((c, "CARD_INVALID", e.detail))
                continue
            kept = trial.read_bytes()
    return bad


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

    attempted: dict[str, str] = {}  # key -> the bus commit WE built for it, across attempts
    attempt = 0
    card_pass_len = None  # candidates before the last per-card refusal; the next pass must hold fewer
    while True:
        attempt += 1
        # Re-gated on every attempt: a refetched tip may be the one that lands the validator.
        candidates = r14_gate(bus_repo, tip, candidates, report)
        if card_pass_len is not None:
            if len(candidates) >= card_pass_len:
                raise Refusal("CARD_PASS_NOT_SHRINKING", f"{len(candidates)} candidates after a per-card refusal pass over {card_pass_len}")
            card_pass_len = None
        pending = []
        tip_sent_rows = []
        landed_rows = []
        for c in candidates:
            if key_on_bus(bus_repo, tip, c["target"], c["key"]):
                ours = attempted.get(c["key"])
                if ours is not None and is_ancestor(bus_repo, ours, tip):
                    # An earlier push of ours returned a failure, yet its commit is on the
                    # refetched tip: the bus accepted it and the acknowledgement was lost.
                    # That is a landed publication of OURS, not another publisher's.
                    report["published"].append({"path": c["path"], "key": c["key"], "target": c["target"],
                                                 "bus_commit": ours})
                    landed_rows.append(_ledger_row(c, ours))
                else:
                    report["already_sent"].append({"path": c["path"], "key": c["key"], "via": "bus_tip"})
                    if push and c["key"] not in ledger_keys:
                        tip_sent_rows.append(_ledger_row(c, tip))
            else:
                pending.append(c)

        if landed_rows:
            report["pushed"] = True
            append_ledger_rows(ledger, landed_rows)

        if not pending:
            if push and tip_sent_rows:
                append_ledger_rows(ledger, tip_sent_rows)
            report["commit"] = tip
            return report

        wt = make_temp_worktree(bus_repo, tip)
        try:
            commits: dict[str, str] = {}
            for c in pending:
                if c["target"] == CARDS_TARGET:
                    assert_no_link_in_path(wt, CARDS_TARGET)
                    ensure_cards_file(wt / c["target"])
                append_block_to_file(wt / c["target"], c["block"])
                commits[c["key"]] = git_commit(wt, c["message"])
                attempted[c["key"]] = commits[c["key"]]
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
            if any(c["target"] == CARDS_TARGET for c in pending):
                try:
                    validate_cards_file(wt, bus_repo=bus_repo)
                except Refusal as e:
                    if e.code != "CARD_INVALID":
                        raise
                    cards = [c for c in pending if c["target"] == CARDS_TARGET]
                    bad = invalid_cards(wt, tip, cards, bus_repo)
                    if not bad:
                        # Invalid, yet no card can be named: that card set is not published (fail closed).
                        bad = [(c, "CARD_INVALID_UNATTRIBUTED", e.detail) for c in cards]
                    for c, code, detail in bad:
                        report["refused"].append({"path": c["path"], "code": code, "detail": detail})
                    dropped = {c["key"] for c, _code, _detail in bad}
                    card_pass_len = len(candidates)
                    candidates = [c for c in candidates if c["key"] not in dropped]
                    attempt -= 1  # a per-card refusal is not a push attempt
                    continue  # rebuild the commits without the refused cards and validate again
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
                # Equality would call a benign race a failure: another publisher may have
                # advanced the bus between our push and this ls-remote. Landed means our
                # commit is an ANCESTOR of the remote tip, checked after fetching that tip.
                if remote_sha is None or not is_ancestor(bus_repo, head, bus_fetch(bus_repo)):
                    raise Refusal("PUSH_VERIFY_FAILED", f"ls-remote={remote_sha!r} head={head!r}")
            rows = [_ledger_row(c, commits[c["key"]]) for c in pending] + tip_sent_rows
            append_ledger_rows(ledger, rows)
            report["published"] += [{"path": c["path"], "key": c["key"], "target": c["target"],
                                      "bus_commit": commits[c["key"]]} for c in pending]
            report["pushed"] = True
            report["commit"] = head
            return report

        if attempt >= max_attempts:
            raise Refusal("PUSH_REJECTED", err[:200])
        tip = bus_fetch(bus_repo)
        # loop again: re-derive `pending` against the fresh tip, so a competitor that already
        # landed one of our exact keys is skipped rather than double-appended.


# ---------- kernel filing: review/<project>-kernel-<date>, never master ----------

def assert_kernel_branch(branch: str) -> str:
    """The only bus branch shape this route may push. Checked when the branch is derived and
    again on the push refspec, so master/main or another project's branch is unreachable."""
    if not KERNEL_BRANCH_RE.fullmatch(branch):
        raise Refusal("KERNEL_BRANCH_REFUSED", branch)
    return branch


def kernel_branch(date: str) -> str:
    """`<yyyymmdd>` -> `review/<project>-kernel-<YYYY-MM-DD>`, the bus README's branch grammar."""
    try:
        if not re.fullmatch(r"[0-9]{8}", date):
            raise ValueError(date)
        day = datetime.strptime(date, "%Y%m%d")
    except ValueError:
        raise Refusal("KERNEL_FILING_BAD_DATE", date) from None
    return assert_kernel_branch(f"review/{PROJECT}-kernel-{day:%Y-%m-%d}")


def load_kernel_filings(repo: pathlib.Path, ref: str) -> list[dict]:
    """One entry per `KERNEL_DIR/<yyyymmdd>/` committed on `ref`. A date dir holding anything but
    the two fixed source names, or lacking the filing, is refused whole."""
    by_date: dict[str, list[str]] = {}
    for p in git_paths(repo, "ls-tree", "-r", "--name-only", ref, "--", KERNEL_DIR):
        if p.startswith(KERNEL_DIR + "/"):
            date, _sep, rest = p[len(KERNEL_DIR) + 1:].partition("/")
            by_date.setdefault(date, []).append(rest)
    filings: list[dict] = []
    for date, names in sorted(by_date.items()):
        path = f"{KERNEL_DIR}/{date}"
        try:
            branch = kernel_branch(date)
            extra = sorted(n for n in names if n not in KERNEL_SOURCES)
            if extra:
                raise Refusal("KERNEL_FILING_EXTRA_PATH", ", ".join(extra))
            if KERNEL_FILING_SRC not in names:
                raise Refusal("KERNEL_FILING_MISSING", KERNEL_FILING_SRC)
            blobs = {n: cat_file_blob(repo, ref, f"{path}/{n}") for n in names}
            try:
                texts = {n: b.decode("utf-8") for n, b in blobs.items()}
            except UnicodeDecodeError:
                raise Refusal("KERNEL_FILING_NOT_UTF8", path) from None
            # The filing is a wholesale rewrite: an empty one would erase the bus adjudication.
            empty = sorted(n for n, t in texts.items() if not t.strip())
            if empty:
                raise Refusal("KERNEL_FILING_EMPTY", ", ".join(empty))
        except Refusal as e:
            filings.append({"path": path, "error": e})
            continue
        src = git(repo, "log", "-n", "1", "--format=%H", ref, "--", path)
        raw_block = texts.get(KERNEL_SPEC_BLOCK_SRC)
        block = block_key = None
        if raw_block is not None:
            raw_block = raw_block.strip("\n") + "\n"
            block_key = idempotency_key(date, KERNEL_SPEC_DEST, raw_block)
            block = raw_block + f"<!-- outbox:{block_key} {PROJECT}:{src[:12]} -->\n"
        filings.append({
            "path": path, "date": date, "branch": branch, "src": src,
            "filing": blobs[KERNEL_FILING_SRC], "filing_text": texts[KERNEL_FILING_SRC],
            "block": block, "block_key": block_key,
            # Content-keyed, not commit-keyed: a squash merge re-lands the same bytes under a new sha,
            # so the key covers the block before its sha-bearing marker.
            "key": idempotency_key(date, branch, texts[KERNEL_FILING_SRC] + "\0" + (raw_block or "")),
            "message": f"kernel({PROJECT}): factory-kernel filing {date} -> {branch}\n\nDoctrine-Export: outbox\n",
        })
    return filings


def remote_branch_sha(bus_repo: pathlib.Path, branch: str) -> str | None:
    rc, out, err = git_try(bus_repo, "ls-remote", "origin", f"refs/heads/{branch}")
    if rc != 0:
        raise GitError(f"git ls-remote origin refs/heads/{branch}: {err}")
    for line in out.splitlines():
        sha, _tab, name = line.partition("\t")
        if name == f"refs/heads/{branch}":
            return sha
    return None


def kernel_bus_fetch(bus_repo: pathlib.Path, branch: str) -> tuple[str, str | None]:
    """(bus master tip, review branch tip or None when the branch does not exist yet)."""
    master = bus_fetch(bus_repo)
    if remote_branch_sha(bus_repo, branch) is None:
        return master, None
    git(bus_repo, "fetch", "--quiet", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    return master, git(bus_repo, "rev-parse", f"refs/remotes/origin/{branch}")


def kernel_branch_foreign(bus_repo: pathlib.Path, master: str, tip: str) -> str | None:
    """None when every commit on the branch past bus master is this outbox's; else which is not."""
    for line in git(bus_repo, "log", "--format=%H %ae %ce", f"{master}..{tip}").splitlines():
        sha, author, committer = line.split(" ", 2)
        if author != OUTBOX_IDENTITY_EMAIL or committer != OUTBOX_IDENTITY_EMAIL:
            return f"{sha[:12]} on the branch is not this outbox's commit"
    return None


def write_kernel_files(wt: pathlib.Path, filing: dict, spec_base: bytes) -> bool:
    """Rewrite the filing wholesale; the spec becomes `spec_base` (the spec where the branch left
    bus master) plus this filing's block, so a revised same-date block replaces the one before it.
    True if anything changed."""
    if not filing["filing"].strip():
        raise Refusal("KERNEL_FILING_EMPTY", KERNEL_FILING_SRC)
    changed = False
    dest = wt / KERNEL_FILING_DEST
    if not dest.is_file() or dest.read_bytes() != filing["filing"]:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(filing["filing"])
        changed = True
    if filing["block"] is not None:
        spec = wt / KERNEL_SPEC_DEST
        old = spec.read_bytes().decode("utf-8", errors="replace") if spec.is_file() else ""
        if f"outbox:{filing['block_key']}" not in old:
            spec.parent.mkdir(parents=True, exist_ok=True)
            spec.write_bytes(spec_base)
            append_block_to_file(spec, filing["block"])
            changed = True
    return changed


def kernel_verify_tip(bus_repo: pathlib.Path, branch: str, sha: str | None) -> str | None:
    """R7: every success path ends here. The branch has a single writer, so the proof is
    ls-remote equal to the commit being certified, not ancestry."""
    remote = remote_branch_sha(bus_repo, branch)
    if remote != sha:
        raise Refusal("PUSH_VERIFY_FAILED", f"ls-remote={remote!r} certified={sha!r}")
    return remote


def _kernel_row(filing: dict, bus_commit: str, remote: str) -> dict:
    return {"key": filing["key"], "route": "kernel-filing", "target": filing["branch"],
            "item": f"kernel-filing/{filing['date']}", "source_commit": filing["src"],
            "bus_commit": bus_commit, "ls_remote": remote, "ts": _now_iso()}


def publish_kernel_filing(bus_repo: pathlib.Path, filing: dict, ledger: pathlib.Path, push: bool,
                          report: dict, max_attempts: int = 3) -> None:
    branch = assert_kernel_branch(filing["branch"])
    entry = {"path": filing["path"], "branch": branch, "key": filing["key"]}
    allowed = {KERNEL_FILING_DEST} | ({KERNEL_SPEC_DEST} if filing["block"] is not None else set())
    ours: set[str] = set()  # commits we pushed; one on the refetched tip is a landed push whose ack was lost
    attempt = 0
    while True:
        attempt += 1
        master, tip = kernel_bus_fetch(bus_repo, branch)
        if tip is not None:
            why = kernel_branch_foreign(bus_repo, master, tip)
            if why is not None:
                report["refused"].append({"path": filing["path"], "code": "KERNEL_BRANCH_NOT_OURS", "detail": why})
                return
        base = tip or master
        # Where the branch left bus master: every commit past it is ours, so the spec there is the
        # prefix every revision of this date's block must keep.
        fork = git(bus_repo, "merge-base", master, tip) if tip is not None else master
        wt = make_temp_worktree(bus_repo, base)
        try:
            spec_base = cat_file_blob(wt, fork, KERNEL_SPEC_DEST)
            if not write_kernel_files(wt, filing, spec_base):
                # No change: the bytes are already on the branch, from a push whose ack was lost
                # (`ours`) or from an earlier run whose ledger is gone. Certified only by ls-remote.
                remote = kernel_verify_tip(bus_repo, branch, tip)
                if tip in ours:
                    report["published"].append({**entry, "bus_commit": tip})
                    report["pushed"] = True
                else:
                    report["already_sent"].append({**entry, "via": "bus_branch"})
                if push:
                    append_ledger_rows(ledger, [_kernel_row(filing, tip, remote)])
                return
            head = git_commit(wt, filing["message"])
            touched = set(git_paths(wt, "diff", "--name-only", base, head))
            if not touched <= allowed:
                raise Refusal("KERNEL_FILING_PATH_REFUSED", ", ".join(sorted(touched - allowed)))
            if cat_file_blob(wt, "HEAD", KERNEL_FILING_DEST) != filing["filing"]:
                raise Refusal("KERNEL_FILING_BYTES_CHANGED", KERNEL_FILING_DEST)
            if not cat_file_blob(wt, "HEAD", KERNEL_SPEC_DEST).startswith(spec_base):
                raise Refusal("PREFIX_BROKEN", KERNEL_SPEC_DEST)
            if not push:
                report["would_push"].append({**entry, "bus_commit": head, "base": base})
                return
            rc, _out, err = git_try(wt, "push", "origin", f"HEAD:refs/heads/{assert_kernel_branch(branch)}")
            ours.add(head)
        finally:
            remove_temp_worktree(bus_repo, wt)

        if rc == 0:
            remote = kernel_verify_tip(bus_repo, branch, head)
            append_ledger_rows(ledger, [_kernel_row(filing, head, remote)])
            report["published"].append({**entry, "bus_commit": head})
            report["pushed"] = True
            return
        if attempt >= max_attempts:
            raise Refusal("PUSH_REJECTED", err[:200])


def drain_kernel_filing(repo: pathlib.Path, bus_repo: pathlib.Path, ref: str, ledger: pathlib.Path,
                        deny_terms: list[tuple[str, str]], push: bool, max_attempts: int = 3) -> dict:
    require_tool_at_ref(repo, ref)
    report: dict = {"push": push, "published": [], "already_sent": [], "refused": [], "would_push": [], "pushed": False}
    ledger_keys = read_ledger_keys(ledger)
    for filing in load_kernel_filings(repo, ref):
        if "error" in filing:
            e = filing["error"]
            report["refused"].append({"path": filing["path"], "code": e.code, "detail": e.detail})
            continue
        try:
            # A kernel filing is wholesale by kernel s4, so the 450-word item cap does not apply;
            # every Law-4 class and deny term still does.
            for text in (filing["filing_text"], filing["block"] or "", filing["message"]):
                screen_law4(text, deny_terms, word_cap=None)
        except Refusal as e:
            report["refused"].append({"path": filing["path"], "code": e.code, "detail": e.detail})
            continue
        if filing["key"] in ledger_keys:
            report["already_sent"].append({"path": filing["path"], "branch": filing["branch"],
                                           "key": filing["key"], "via": "ledger"})
            continue
        publish_kernel_filing(bus_repo, filing, ledger, push, report, max_attempts)
    return report


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


def _debt_text(result: dict, ref: str) -> str:
    if not result["count"]:
        return f"[doctrine-outbox] no unsent doctrine items on {ref}"
    stale = " STALE (> 24 h)" if result["stale_over_24h"] else ""
    lines = [f"[doctrine-outbox] {result['count']} unsent doctrine item(s) on {ref}; "
             f"oldest {result['oldest_age_hours']} h since merge{stale}:"]
    for u in result["items"]:
        age = "age unknown" if u["age_hours"] is None else f"{u['age_hours']} h"
        lines.append(f"  {u['path']}  ({u['detail']}; {age})")
    return "\n".join(lines)


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
    print(_debt_text(result, ref))
    return 1 if result["count"] else 0


def cmd_check(repo: pathlib.Path, ref: str, ledger: pathlib.Path, bus_repo: pathlib.Path | None = None,
              max_age_min: int = HOOK_CHECK_WINDOW_MIN) -> int:
    """Unsent items older than the window. The drain ticks every 15 min, so an item unsent for 60
    has missed several: that names a stuck seam, not a queue. `debt` stays "any unsent = 1"; a gate
    needs this age form so a just-committed item is not a red bar."""
    try:
        result = compute_debt(repo, ref, ledger, bus_repo)
    except Unknown as e:
        print(f"[doctrine-outbox] check: UNKNOWN ({e})")
        return 2
    print(_debt_text(result, ref))
    stale = [u for u in result["items"] if u["age_hours"] is not None and u["age_hours"] * 60 > max_age_min]
    if stale:
        print(f"[doctrine-outbox] REFUSED: {len(stale)} item(s) unsent for > {max_age_min} min -- "
              f"the drain is not running or is refusing; read its receipts")
        return 1
    return 0


# ---------- check-ledger: the capture check ----------

FINDING_TAG_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?Finding:\s*(?:\*\*)?\s*(KF-\d+)\b")
LEDGER_SECTION_RE = re.compile(r"^##\s+KERNEL FINDINGS\b")
LEDGER_HEADING_RE = re.compile(r"^#{1,2}\s")
SECTION_ITEM_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?(KF-\d+)\b")
DISPOSITION_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?Doctrine-Export:\s*(?:\*\*)?\s*(outbox|none)\b\s*(.*?)\s*$")
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
    if SUBJECT_LEDGER_REL is None:
        raise Unknown("SUBJECT_LEDGER_UNCONFIGURED",
                      f"pass --ledger, set ${SUBJECT_LEDGER_ENV}, or set subject_ledger_rel in {CONFIG_NAME}")
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
    require_configured()
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
    return (path in FINDING_PATH_FILES or path.startswith(FINDING_PATH_PREFIXES)
            or any(rx.match(path) for rx in FINDING_PATH_REGEXES))


def range_added_items(repo: pathlib.Path, rev_range: str) -> set[str]:
    return set(git_paths(repo, "log", "--no-merges", "--diff-filter=A", "--name-only", "--format=", rev_range, "--", OUTBOX_DIR))


def check_commit_bare(repo: pathlib.Path, sha: str, deny_terms: list[tuple[str, str]]) -> list[str]:
    """`trailer_mode: bare` -- the fleet bus reference's grammar. The trailer is exactly `none` or
    `outbox`; `outbox` means THIS commit adds an item under the outbox directory (not the README,
    not the sent directory, not a kernel filing). The item is parsed and screened from the commit's
    own bytes. Items are judged from ADDED/MODIFIED paths only: a steward's move into the sent
    directory DELETES the root item, and validating a deleted path would refuse its landing commit."""
    paths = git_paths(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
    present = set(git_paths(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", "--diff-filter=AM", sha))
    added = [p for p in paths if p in present and p.startswith(OUTBOX_DIR + "/")
             and not (SENT_PREFIX and p.startswith(SENT_PREFIX)) and not p.startswith(KERNEL_DIR + "/")
             and p != OUTBOX_DIR + "/README.md"]
    hits = [p for p in paths if is_finding_path(p)]
    lines = git(repo, "show", "-s", f"--format=%(trailers:key={TRAILER_KEY},valueonly)", sha).splitlines()
    trailer = lines[0].strip().lower() if lines and lines[0].strip() else None
    problems: list[str] = []
    if hits and trailer is None:
        problems.append(f"{sha[:10]} touches {hits[0]} and carries no '{TRAILER_KEY}:' trailer "
                        f"(write '{TRAILER_KEY}: none' or '{TRAILER_KEY}: outbox' + an item)")
    if trailer is not None and trailer not in BARE_TRAILER_VALUES:
        problems.append(f"{sha[:10]} '{TRAILER_KEY}: {trailer}' is not one of {BARE_TRAILER_VALUES}")
    if trailer == "outbox" and not added:
        problems.append(f"{sha[:10]} declares '{TRAILER_KEY}: outbox' but adds no {OUTBOX_DIR}/ item (declared-but-not-filed)")
    for p in added:
        name = p.rsplit("/", 1)[-1]
        if not ITEM_NAME_RE.match(name):
            problems.append(f"{sha[:10]} {p}: name must be <yyyymmdd>-<slug>.md")
            continue
        try:
            text = git(repo, "show", f"{sha}:{p}")
            item = parse_item(text + "\n")
            screen_law4(item["body"], deny_terms)
            screen_rendered_card(item)
        except Refusal as e:
            problems.append(f"{sha[:10]} {p}: {e}")
    return problems


def check_commit(repo: pathlib.Path, sha: str, added_items: set[str],
                 deny_terms: list[tuple[str, str]]) -> list[str]:
    """Problems with one commit's `Doctrine-Export:` trailers; empty means it passes."""
    if TRAILER_MODE == "bare":
        return check_commit_bare(repo, sha, deny_terms)
    files = git_paths(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha)
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
            screen_rendered_card(parsed)
        except Refusal as e:
            problems.append(f"DOCTRINE_EXPORT_OUTBOX_ITEM_INVALID: {norm}: {e}")
    return problems


def check_commits(repo: pathlib.Path, rev_range: str, deny_terms: list[tuple[str, str]]) -> dict[str, list[str]]:
    """{sha: problems} for every non-merge commit in `rev_range` that fails. Raises Unknown
    when the range cannot be resolved -- an unresolvable range is not a passing one."""
    require_configured()
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


# ---------- hook commands: pre-push, stop, session-start, install-hooks ----------

def install_pre_push(repo: pathlib.Path) -> str:
    """Idempotently splice the outbox guard into the repo's pre-push hook. Machine-local, so a
    SessionStart hook can do this on every machine and every rotation without being asked."""
    # Resolve through git: in a linked worktree `.git` is a FILE and hooks live in the common dir.
    hook = pathlib.Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push"))
    hook.parent.mkdir(parents=True, exist_ok=True)
    # git hands a pre-push hook its ref lines on stdin, once. The guard's python reads that stream to
    # the end, so the snippet spools it to a file, feeds the guard from the file, and then rebinds the
    # hook's own stdin to the file: every layer after the guard (git lfs pre-push, a project's own
    # checks) reads the same bytes git sent. The file is unlinked while the hook holds it open.
    # A machine with no python refuses the push: a guard that cannot run must not read as a pass.
    snippet = (f"{GUARD_MARKER}: findings reach the bus by file; a finding-path commit must declare.\n"
               f"REPO_ROOT=${{REPO_ROOT:-$(git rev-parse --show-toplevel)}}\n"
               f"OUTBOX_PY=\n"
               f"for cand in python3 python; do\n"
               f"  if command -v \"$cand\" >/dev/null 2>&1 && \"$cand\" --version >/dev/null 2>&1; then OUTBOX_PY=$cand; break; fi\n"
               f"done\n"
               f"OUTBOX_STDIN=$(mktemp 2>/dev/null) || OUTBOX_STDIN=\"${{TMPDIR:-/tmp}}/outbox-pre-push.$$\"\n"
               f"cat > \"$OUTBOX_STDIN\"\n"
               f"if [ -n \"$OUTBOX_PY\" ]; then\n"
               f"  \"$OUTBOX_PY\" \"$REPO_ROOT/{TOOL_REL}\" pre-push \"$REPO_ROOT\" \"$@\" < \"$OUTBOX_STDIN\" || {{ OUTBOX_RC=$?; rm -f \"$OUTBOX_STDIN\"; exit $OUTBOX_RC; }}\n"
               f"else\n"
               f"  echo \"doctrine-outbox: no python on PATH; push refused because the guard cannot run\" >&2\n"
               f"  rm -f \"$OUTBOX_STDIN\"; exit 1\n"
               f"fi\n"
               f"exec < \"$OUTBOX_STDIN\"\n"
               f"rm -f \"$OUTBOX_STDIN\" 2>/dev/null || true\n"
               f"{GUARD_END}\n")
    current = hook.read_text(encoding="utf-8") if hook.exists() else "#!/bin/sh\nset -e\nREPO_ROOT=$(git rev-parse --show-toplevel)\n"
    lines = current.splitlines(True)
    start = next((i for i, line in enumerate(lines) if line.startswith(GUARD_MARKER)), None)
    if start is not None:
        # A guard sitting after a top-level `exit`/`exec` is dead code: git runs the hook, the hook ends
        # first, and the push goes through unjudged. Reporting "present" or "repaired" for it would be a lie.
        _refuse_if_terminated_before(lines, start)
        end = _guard_block_end(lines, start)
        # OUTBOX-REPAIR-MOVES-GUARD-1: a guard is only live when nothing but the shebang, `set` lines and
        # comments stand ahead of it. Behind a layer that reads git's ref lines from stdin it gets an empty
        # stream and passes every push, so "present" or an in-place "repaired" there would be a dead guard.
        at_splice = _only_preamble_before(lines, start)
        if at_splice and "".join(lines[start:end + 1]) == snippet:
            _make_executable(hook)   # an install made before the bit was set is repaired, not skipped
            return "present"
        # An older guard (r1-r4: no end marker, no stdin spool, or a push that passes without python)
        # is replaced. Leaving it would be worse than before the executable bit: git used to
        # skip the hook, and now runs one that swallows the ref lines a following layer needs.
        if at_splice:
            lines[start:end + 1] = [snippet]
        else:
            # Not at the splice point: take the block out and put the current one where a fresh install
            # puts it, then prove nothing ends the hook ahead of the new position.
            del lines[start:end + 1]
            idx = _preamble_end(lines)
            _refuse_if_terminated_before(lines, idx)
            if idx and not lines[idx - 1].endswith("\n"):
                lines[idx - 1] += "\n"
            lines.insert(idx, "\n" + snippet)
        hook.write_text("".join(lines), encoding="utf-8", newline="\n")
        _make_executable(hook)
        return "repaired"
    # OUTBOX-HOOK-SPLICE-AFTER-EXIT-1: right after the shebang and any `set -e` preamble, ahead of every
    # layer the hook runs. Appended at the end, a hook that ends in `exit 0` or `exec <next hook>` never
    # reached the guard. The snippet restores stdin for the layers after it (see above), and it sits ahead
    # of an LFS layer that may exit 2 for an unrelated reason. (r1-r5 spliced before the LFS line, else at the end.)
    idx = _preamble_end(lines)
    _refuse_if_terminated_before(lines, idx)
    if idx and not lines[idx - 1].endswith("\n"):
        lines[idx - 1] += "\n"
    lines.insert(idx, "\n" + snippet)
    hook.write_text("".join(lines), encoding="utf-8", newline="\n")
    _make_executable(hook)
    return "installed"


SET_PREAMBLE_RE = re.compile(r"^set(\s+[-+]\w+)+\s*(#.*)?$")
# A top-level line that ends the hook: `exit`, or `exec <command>`. `exec < file` / `exec 3>&1` only
# redirect, and an indented `exit` sits inside an `if`, so neither ends the script.
TERMINATOR_RE = re.compile(r"^(?:exit\b|exec\s+(?![0-9]*[<>&]))")


def _preamble_end(lines: list[str]) -> int:
    """Index just past the shebang and any leading `set -e`-style lines (blank and comment lines between
    them are skipped); 0 when the hook has neither."""
    first = 1 if lines and lines[0].startswith("#!") else 0
    end = first
    for i in range(first, len(lines)):
        text = lines[i].strip()
        if SET_PREAMBLE_RE.match(text):
            end = i + 1
        elif text and not text.startswith("#"):
            break
    return end


def _only_preamble_before(lines: list[str], idx: int) -> bool:
    """True when every line ahead of `idx` is the shebang, a `set` line, a comment or blank: the guard at
    `idx` then runs before any layer of the hook, with git's stdin untouched."""
    for i, line in enumerate(lines[:idx]):
        text = line.strip()
        if not text or text.startswith("#") or SET_PREAMBLE_RE.match(text):
            continue
        return False
    return True


def _refuse_if_terminated_before(lines: list[str], idx: int) -> None:
    """Refuse when a top-level `exit`/`exec` stands ahead of line `idx`: a guard placed there would never run."""
    for i, line in enumerate(lines[:idx]):
        if TERMINATOR_RE.match(line):
            raise Refusal("HOOK_GUARD_UNREACHABLE",
                          f"line {i + 1} of the pre-push hook ({line.strip()!r}) ends the hook before the guard point, "
                          "so a guard there would never run; remove the guard block by hand and run install-hooks again")


def _guard_block_end(lines: list[str], start: int) -> int:
    """Index of the last line of the guard block that begins at `lines[start]`. A current block ends at
    GUARD_END. A block written before the end marker existed ends at the `fi` that closes its missing-python
    branch, plus the stdin rebind and unlink lines when it has them. A block this cannot place is refused:
    splicing blind into a hook someone edited would break their layers."""
    for i in range(start + 1, len(lines)):
        if lines[i].rstrip("\r\n") == GUARD_END:
            return i
    said = next((i for i in range(start + 1, len(lines)) if "the push guard did not run" in lines[i]), None)
    if said is None or said + 1 >= len(lines) or lines[said + 1].rstrip("\r\n") != "fi":
        raise Refusal("HOOK_UNRECOGNISED", f"the {GUARD_MARKER} block in the pre-push hook has no end marker and is not a "
                                           "known older shape; remove it by hand and run install-hooks again")
    end = said + 1
    for tail in ('exec < "$OUTBOX_STDIN"', 'rm -f "$OUTBOX_STDIN"'):
        if end + 1 < len(lines) and lines[end + 1].startswith(tail):
            end += 1
    return end


def _make_executable(hook: pathlib.Path) -> None:
    """git silently skips a pre-push hook that is not executable on POSIX. Grant execute to each
    class that can already read the file, so a restrictive umask is not widened. On Windows the
    call is a harmless no-op."""
    mode = stat.S_IMODE(os.stat(hook).st_mode)
    os.chmod(hook, mode | ((mode & (stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)) >> 2))


def _remote_default_base(repo: pathlib.Path, remote: str, local_sha: str) -> str | None:
    """The merge-base of `local_sha` with the remote's default branch, or None when the remote's default
    branch cannot be found locally or shares no history with the push. OUTBOX-PREPUSH-NEW-BRANCH-COST-1:
    a push that creates a branch has no remote tip to start from, and walking the whole history on every
    new branch judges every old commit again. The default branch's commits were judged when they landed,
    so the walk stops at the point the new branch left it. None means "walk everything": a base this
    cannot prove is never guessed. r8: "default branch" means a branch in `pre_push_refs` (its commits were
    judged when they landed); the remote's HEAD, master and main count only when that config guards them."""
    if not remote:
        return None
    # r8 (Iverson advisory on b5310592, ACK 6227530991): a base is taken only from a branch the hook guards
    # (`pre_push_refs`), by its WHOLE name after refs/heads/. r1-r7 also tried the remote's HEAD and
    # `{remote}/master` / `{remote}/main` whatever the config guarded, and read the name after the last
    # slash (refs/heads/release/1.0 became `1.0`), so the walk could start from a ref the hook never judged:
    #     candidates.extend(f"{remote}/{ref.rsplit('/', 1)[-1]}" for ref in PRE_PUSH_REFS)
    #     candidates.extend((f"{remote}/master", f"{remote}/main"))
    guarded = [ref[len("refs/heads/"):] for ref in PRE_PUSH_REFS if ref.startswith("refs/heads/")]
    candidates: list[str] = []
    rc, out, _err = git_try(repo, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    if rc == 0 and out.startswith(f"{remote}/") and out[len(remote) + 1:] in guarded:
        candidates.append(out)
    candidates.extend(f"{remote}/{name}" for name in guarded)
    for cand in candidates:
        rc, _out, _err = git_try(repo, "rev-parse", "--verify", "--quiet", f"refs/remotes/{cand}^{{commit}}")
        if rc != 0:
            continue
        rc, base, _err = git_try(repo, "merge-base", f"refs/remotes/{cand}", local_sha)
        if rc == 0 and base:
            return base
    return None


def cmd_pre_push(repo: pathlib.Path, deny_terms: list[tuple[str, str]], remote: str = "") -> int:
    """git passes <remote> <url> as argv and ref lines on stdin: <local-ref> <local-sha> <remote-ref> <remote-sha>.
    Only a push to one of the config's `pre_push_refs` is judged; an unresolvable range refuses.
    A push that creates the branch judges only the commits not on the remote's default branch
    (`_remote_default_base`); with no such base it judges the whole history.
    This reads stdin to the end; the shell snippet install_pre_push writes spools it first and
    rebinds the hook's stdin afterwards, so the layers after the guard see the same bytes."""
    problems: list[str] = []
    for line in sys.stdin.read().splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        _local_ref, local_sha, remote_ref, remote_sha = parts
        if remote_ref not in PRE_PUSH_REFS or set(local_sha) == {"0"}:
            continue
        if set(remote_sha) != {"0"}:
            rng = f"{remote_sha}..{local_sha}"
        else:  # a new branch: bound the walk to what is not already on the remote's default branch
            base = _remote_default_base(repo, remote, local_sha)
            rng = f"{base}..{local_sha}" if base else local_sha
        try:
            failures = check_commits(repo, rng, deny_terms)
        except Unknown as e:
            problems.append(f"cannot check {rng}: {e}")
            continue
        for sha, found in failures.items():
            problems.extend(f"{sha[:10]} {p}" for p in found)
    if problems:
        print("[doctrine-outbox] PUSH REFUSED -- doctrine export is declared per commit, not remembered:", file=sys.stderr)
        for p in problems:
            print("  " + p, file=sys.stderr)
        print(f"  see {OUTBOX_DIR}/README.md", file=sys.stderr)
        return 1
    return 0


def cmd_stop(repo: pathlib.Path, rev_range: str, deny_terms: list[tuple[str, str]]) -> int:
    """Stop hook: block ONCE (stop_hook_active ends the loop) when a commit from the last 24 h in
    `rev_range` would be refused at push. A nudge at the seam the session is standing on; the
    refusing gate is pre-push.

    The range is the commits NOT YET on the remote: the config's `default_range` (for a project whose
    default_ref is a remote-tracking ref, `<default_ref>..HEAD`), or `--ref R` meaning `R..HEAD`. A
    commit already on the remote cannot be repaired by the follow-up commit the nudge asks for, so it
    is never judged. CONSTRAINT: a project must therefore set `default_range` (or pass `--ref`) to a
    base that exists locally; an unresolvable range is no nudge, because a Stop hook never wedges a
    session."""
    try:
        data = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 -- no or malformed hook JSON is "not active"
        data = {}
    if data.get("stop_hook_active"):
        return 0
    since = int(time.time()) - HOOK_STOP_WINDOW_S
    rc, out, _err = git_try(repo, "rev-list", "--no-merges", f"--since={since}", rev_range)
    if rc != 0:
        return 0
    added = set(git_paths(repo, "log", "--no-merges", "--diff-filter=A", "--name-only", "--format=",
                          f"--since={since}", rev_range, "--", OUTBOX_DIR))
    problems: list[str] = []
    for sha in out.split():
        problems.extend(check_commit(repo, sha, added, deny_terms))
    if not problems:
        return 0
    print(json.dumps({"decision": "block",
                      "reason": ("A commit from the last 24 h will be refused at push by the doctrine-outbox guard. "
                                 "Fix it now (a follow-up commit is fine; never rewrite history): "
                                 + " | ".join(problems[:4]) + f" -- see {OUTBOX_DIR}/README.md")}))
    return 0


def cmd_session_start(repo: pathlib.Path, ref: str, ledger: pathlib.Path) -> int:
    """Install the pre-push guard, then report debt. Informational: it never blocks a session
    start, so every failure here is printed and the exit status stays 0."""
    try:
        print(f"[doctrine-outbox] pre-push guard: {install_pre_push(repo)}")
    except (GitError, OSError, Refusal) as e:
        print(f"[doctrine-outbox] pre-push guard: UNKNOWN ({e})")
    try:
        print(_debt_text(compute_debt(repo, ref, ledger), ref))
    except (Unknown, GitError) as e:
        print(f"[doctrine-outbox] debt: UNKNOWN ({e})")
    return 0


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
            screen_rendered_card(item)
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

HOOK_COMMANDS = ("check", "pre-push", "stop", "session-start", "install-hooks")


def _default_repo() -> pathlib.Path:
    """The repo the config belongs to (the config sits at its root); the cwd when there is none."""
    return CONFIG_ROOT if CONFIG_ROOT is not None else pathlib.Path.cwd()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="doctrine_outbox", description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=pathlib.Path, default=None,
                        help=f"the project's {CONFIG_NAME} (default: ${CONFIG_ENV}, else found beside the tool)")
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

    kernel_p = sub.add_parser("kernel-filing", help=f"publish {KERNEL_DIR or '<outbox_dir>/kernel-filing'}/<yyyymmdd>/ to "
                                                     f"review/{PROJECT or '<project>'}-kernel-<date> "
                                                     "(dry run unless --push); no branch or path parameter exists")
    kernel_p.add_argument("--bus", required=True, type=pathlib.Path)
    kernel_p.add_argument("--ref", default=DEFAULT_REF)
    kernel_p.add_argument("--push", action="store_true")
    kernel_p.add_argument("--ledger", type=pathlib.Path, default=None, help="the SENT ledger (sent.jsonl)")
    kernel_p.add_argument("--deny-file", type=pathlib.Path, default=None)

    ledger_p = sub.add_parser("check-ledger", help="every structured finding needs a Doctrine-Export disposition")
    ledger_p.add_argument("--ledger", type=pathlib.Path, default=None,
                          help=f"the SUBJECT ledger to scan (env {SUBJECT_LEDGER_ENV})")
    ledger_p.add_argument("--watermark", type=pathlib.Path, default=None, help=f"(env {WATERMARK_ENV})")
    mode = ledger_p.add_mutually_exclusive_group()
    mode.add_argument("--advance", action="store_true", help="advance the watermark past disposed findings")
    mode.add_argument("--baseline", action="store_true", help="explicitly waive all history up to now")

    for name in ("check-commits", "check-range"):
        commits_p = sub.add_parser(name, help="commits touching a finding path need a Doctrine-Export trailer "
                                              "(legacy form: [REPO [A..B]])")
        commits_p.add_argument("legacy", nargs="*", help="optional REPO then A..B, as the older check-range was called")
        commits_p.add_argument("--range", dest="rev_range", default=DEFAULT_RANGE)
        commits_p.add_argument("--deny-file", type=pathlib.Path, default=None)

    check_p = sub.add_parser("check", help="exit 1 when an unsent item is older than MAX_AGE_MIN (default "
                                           f"{HOOK_CHECK_WINDOW_MIN}); args: [REPO [MAX_AGE_MIN]]")
    check_p.add_argument("legacy", nargs="*")
    check_p.add_argument("--ref", default=DEFAULT_REF)
    check_p.add_argument("--ledger", type=pathlib.Path, default=None)
    check_p.add_argument("--bus", type=pathlib.Path, default=None)

    pre_p = sub.add_parser("pre-push", help="git pre-push hook: refuse a push of commits that fail check-commits; "
                                            "args: [REPO [REMOTE URL]], ref lines on stdin")
    pre_p.add_argument("legacy", nargs="*")
    pre_p.add_argument("--deny-file", type=pathlib.Path, default=None)

    stop_p = sub.add_parser("stop", help="Stop hook: block once when a commit from the last 24 h that is not yet on "
                                         f"the remote would be refused at push (range: {DEFAULT_RANGE or 'the config default_range'})")
    stop_p.add_argument("legacy", nargs="*")
    stop_p.add_argument("--ref", default=None, help="judge the commits in R..HEAD instead of the config's default_range")
    stop_p.add_argument("--deny-file", type=pathlib.Path, default=None)

    start_p = sub.add_parser("session-start", help="install the pre-push guard and report debt; always exit 0")
    start_p.add_argument("legacy", nargs="*")
    start_p.add_argument("--ref", default=DEFAULT_REF)
    start_p.add_argument("--ledger", type=pathlib.Path, default=None)

    install_p = sub.add_parser("install-hooks", help="install the pre-push guard only")
    install_p.add_argument("legacy", nargs="*")

    return parser


def _apply_cli_config(argv: list[str]) -> None:
    """Honour `--config` before the parser is built: its defaults (--ref, --range, --repo) come from it."""
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=pathlib.Path, default=None)
    known, _rest = pre.parse_known_args(argv)
    if known.config is not None:
        configure(load_config(known.config), known.config)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        _apply_cli_config(argv)
        if "-h" not in argv and "--help" not in argv:
            require_configured()
    except Refusal as e:
        print(f"[doctrine-outbox] {e}", file=sys.stderr)
        return 1
    except Unknown as e:
        print(f"[doctrine-outbox] UNKNOWN {e}", file=sys.stderr)
        return 2
    parser = build_parser()
    args = parser.parse_args(argv)
    repo = args.repo.resolve()
    legacy = list(getattr(args, "legacy", []) or [])
    if legacy:  # hooks call `<cmd> REPO ...`; the first positional is always the repo
        repo = pathlib.Path(legacy[0]).resolve()
    try:
        if args.cmd == "validate":
            deny_terms = gather_deny_terms(resolve_deny_file(repo, args.deny_file))
            return cmd_validate(args.items, deny_terms)
        if args.cmd == "debt":
            ledger = resolve_ledger(repo, args.ledger)
            bus = args.bus.resolve() if args.bus is not None else None
            return cmd_debt(repo, args.ref, ledger, bus, args.json)
        if args.cmd in ("drain", "kernel-filing"):
            ledger = resolve_ledger(repo, args.ledger)
            deny_terms = gather_deny_terms(resolve_deny_file(repo, args.deny_file))
            route = drain if args.cmd == "drain" else drain_kernel_filing
            report = route(repo, args.bus.resolve(), args.ref, ledger, deny_terms, args.push)
            print(json.dumps(report, indent=2, sort_keys=True))
            for r in report["refused"]:
                print(f"[doctrine-outbox] REFUSED {r['path']}: {r['code']}: {r['detail']}", file=sys.stderr)
            return 1 if report["refused"] else 0
        if args.cmd == "check-ledger":
            code, out = check_ledger(resolve_subject_ledger(repo, args.ledger), resolve_watermark(repo, args.watermark),
                                     advance=args.advance, baseline=args.baseline)
            print("\n".join(out))
            return code
        if args.cmd in ("check-commits", "check-range"):
            deny_terms = gather_deny_terms(resolve_deny_file(repo, args.deny_file))
            rev_range = legacy[1] if len(legacy) > 1 else args.rev_range
            return cmd_check_commits(repo, rev_range, deny_terms)
        if args.cmd == "check":
            # was: max_age = int(legacy[1]) if len(legacy) > 1 else HOOK_CHECK_WINDOW_MIN  (a bad value was a traceback)
            max_age = HOOK_CHECK_WINDOW_MIN
            if len(legacy) > 1:
                try:
                    max_age = int(legacy[1])
                except ValueError:
                    print(f"[doctrine-outbox] usage: check [REPO [MAX_AGE_MIN]]; MAX_AGE_MIN must be a whole number of "
                          f"minutes, got {legacy[1]!r}", file=sys.stderr)
                    return 2
            bus = args.bus.resolve() if args.bus is not None else None
            return cmd_check(repo, args.ref, resolve_ledger(repo, args.ledger), bus, max_age)
        if args.cmd == "pre-push":
            remote = legacy[1] if len(legacy) > 1 else ""   # git's argv: <remote> <url>, after the repo the hook adds
            return cmd_pre_push(repo, gather_deny_terms(resolve_deny_file(repo, args.deny_file)), remote)
        if args.cmd == "stop":
            rev_range = f"{args.ref}..HEAD" if args.ref else DEFAULT_RANGE
            return cmd_stop(repo, rev_range, gather_deny_terms(resolve_deny_file(repo, args.deny_file)))
        if args.cmd == "session-start":
            return cmd_session_start(repo, args.ref, resolve_ledger(repo, args.ledger))
        if args.cmd == "install-hooks":
            print(install_pre_push(repo))
            return 0
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
