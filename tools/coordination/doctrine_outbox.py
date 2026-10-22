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
SHA, so nothing runs until the project reviews and pins it. This release is pin-aware (card
OUTBOX-PIN-AWARE-TOOL-AT-REF-1): a config that names `tool_pin` has the installed hook run the pin through
`factory-pins/pin-runner.py`, and `require_tool_at_ref` accept the pin runner plus a pins file naming it in
place of the file at `tool_rel`. A project without `tool_pin` KEEPS its local copy at `tool_rel`, because
the hook and the debt and drain checks then read that path. Until the bus holds the file this one is the only source.

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
   `repaired`), so a layer that reads git's stdin ahead of it cannot starve it. The hook and the debt checks run the
   file at config `tool_rel`, or, when the config sets `tool_pin`, `python -I factory-pins/pin-runner.py run
   <tool_pin> --` with the config path in $DOCTRINE_OUTBOX_CONFIG; `tool_rel` is then optional, and a pin that is
   not provisioned on the machine refuses the push (the runner's exit passes through: a guard that cannot run is not a pass).
11. Ruling candidates name what they touch or supersede (CoS work order FWO-DEFECT-PREVENT-1 item 4; seed #469 r1).
   `validate` refuses an item that proposes a ruling candidate (a line that opens `Ruling candidate` where a block
   opens, once the body is rendered (HTML entities and tags, table cells) and normalised (Unicode, invisible characters)
   and its bullet, quote, bold and heading marks are off; or a heading with `- RULING CANDIDATE` / `RULING CANDIDATE:` in
   it; a wrapped continuation line, a mid-sentence mention and a citation (the phrase, a `ruling-candidates/` path and
   a status word such as `remains`, with no colon after it and not the item's own path) are not markers) unless it
   carries a `touches/supersedes:` declaration (`touches:`, `supersedes:` and the bus template's `Relationship to
   existing doctrine:` count too): rule ids (`R15`) or bus-relative paths (`RULINGS.md:1126-1152`) on that line, comma or semicolon
   separated, or as bullets under it, or the single entry `none: <reason of >= 4 words>`. Typed refusals:
   RULING_CANDIDATE_NO_SUPERSEDES, _NONE_NEEDS_REASON, _NONE_WITH_RULES, _TARGET_MALFORMED, and, when the bus clone
   (`--bus`, default $FACTORY_BUS_CLONE) lacks a named target at the commit its HEAD resolves to (resolved once per
   item: every target of one item is read at one commit, but two items in one `validate` call can be judged at
   different tips), _TARGET_NOT_ON_BUS. The bus is only read; one that cannot be read, or whose object cannot be read
   once the tip is known, including a path whose tree lists it but whose blob or subtree is missing
   (RULING_BUS_UNREADABLE, RULING_BUS_OBJECT_UNREADABLE), is UNKNOWN (exit 2), never a missing target. With no bus the
   shape is still checked and the unchecked targets are printed as a NOTE. The drain does not apply it, and sent items
   are not re-judged. Not rendered, each pinned by a test (none is used on the bus): HTML comments, footnotes,
   definition lists, look-alike letters, a bullet glyph that is not markdown, decoration between a heading separator and
   the phrase, a U+2236 used as a colon, a declaration label in a form the tool does not read, an indented fence line,
   prose after a pipe, and a table delimiter row (it makes the cell before it a setext heading).
12. Card routes are checked before they ship (CoS work order FWO-DEFECT-PREVENT-1 item 3; fail-closed, CARD-ROUTE-EXERCISE-2).
   `validate --exercise` takes every command an item names and, in a scratch `git clone --local --no-hardlinks` of the
   bus (`--bus`, default $FACTORY_BUS_CLONE) with `origin` removed, never in the bus itself, (a) checks that its program
   and every script it names exist, running nothing, and (b) runs it only when its program is on the execution ALLOWLIST
   (`git`, by its bare name and its resolved executable). Every other program (a shell, an interpreter, pytest, ...) is
   existence-checked and then skipped as `ROUTE_NOT_EXECUTED`, never launched. It prints one
   `ROUTE_OK|ROUTE_NOT_EXECUTED|ROUTE_MISSING|ROUTE_FAILED|ROUTE_TIMEOUT|ROUTE_UNSAFE <item> <command>` line per
   command; anything but the first two exits 1. The extraction rule and the outcome meanings are the block comment above
   `extract_routes`. `--bus` is one option shared with behaviour 11: it is accepted without `--exercise`.

Nothing here runs itself: the OS drain task, Stop hook and heartbeat debt are the project's wiring.

Subcommands
-----------
    validate <item.md>... [--bus PATH] [--exercise [--timeout S] [--fill NAME=VALUE]... [--scratch-root PATH]]
                                                parse + screen item files; a ruling candidate needs `touches/supersedes:`
                                                naming rules or bus paths that exist at the bus tip (behaviour 11);
                                                --exercise also checks every route command an item names (behaviour 12)
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
import html
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

# ---------- constants that are fleet-wide, not per project ----------

GUARD_MARKER = "# doctrine-outbox-guard"
GUARD_END = "# end doctrine-outbox-guard"
CONFIG_NAME = "doctrine-outbox.config.json"
CONFIG_ENV = "DOCTRINE_OUTBOX_CONFIG"
CONFIG_SCHEMA = "doctrine-outbox-config.v1"
KINDS = ("receipt", "trap", "ruling", "concern")
# The project-to-CoS concern channel (card COS-CONCERN-CHANNEL-1): a project-owned file, one writer (bus Law 2).
# The CoS answers each id once in its own cos-feedback/<project>/concerns.md; this tool never writes that file.
CONCERNS_READER_FILE = "cos-feedback/README.md"
CONCERNS_READER_TOKEN = "CoS-CONCERN:"
# The ratification is ONE line of that README, not the token anywhere in it: the CoS's reader duty names both routes
# (R1 PR comments and R2 every specs/*/cos-concerns.md). The gate opens only on a line EQUAL to the example below
# (an optional leading `-` or `*` bullet followed by 1 to 4 spaces or a tab, indented 0 to 3 spaces, and trailing
# whitespace); a line that adds, forbids or negates anything is not the duty, a line at 4 or more spaces or a tab (or
# with 5 or more spaces after the bullet) is an indented code block, and a line inside a ``` fence, a ~~~ fence or an
# HTML comment never counts. Lines end only at \n, \r\n or \r, as in Markdown.
CONCERNS_DUTY_LINE_EXAMPLE = "- The CoS reads `CoS-CONCERN:` PR comments and every `specs/*/cos-concerns.md` each pass."
CONCERNS_DUTY_LINE_RE = re.compile(r"^ {0,3}(?:[-*](?: {1,4}|\t))?" + re.escape(CONCERNS_DUTY_LINE_EXAMPLE[2:]) + r"\s*$")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CONCERN_FIELDS = ("id", "to", "opened_utc", "expires_utc", "kind", "evidence", "ask")
CONCERN_KINDS = ("output-format", "process", "shared-tool-defect", "fleet-pattern")
CONCERN_DEFAULT_DAYS = 14
CONCERN_UTC_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
CONCERN_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
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
# A pin runner tool name (factory-pins/pin-runner.py's NAME), and where a consumer keeps the runner and its pins.
PIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PIN_RUNNER_REL = "factory-pins/pin-runner.py"
PINS_REL = "factory-pins/factory-pins.json"
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
TOOL_PIN = ""        # optional: the tool's name in factory-pins/factory-pins.json; when set the hook runs it by pin
DEFAULT_REF = ""
DEFAULT_RANGE = ""
CARDS_TARGET = ""
TARGETS: tuple[str, ...] = ("RECEIPTS.md", "TRAPS.md", "RULINGS.md")
CARD_HEADING_RE = re.compile(r"^## $")
CARDS_TITLE = ""
CONCERNS_TARGET = ""
CONCERNS_TITLE = ""
CONCERN_ID_RE = re.compile(r"$^")
CONCERN_HEADING_RE = re.compile(r"$^")
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
    "schema", "project", "tool_rel", "tool_pin", "default_ref", "default_range", "outbox_dir", "sent_dir", "cards_target",
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
    tool_pin = cfg.get("tool_pin", "")
    if not isinstance(tool_pin, str) or (tool_pin and not PIN_NAME_RE.match(tool_pin)):
        raise Refusal("CONFIG_INVALID", f"tool_pin must match {PIN_NAME_RE.pattern}")
    # A project that runs the tool by pin may keep no local copy, so `tool_rel` is required only without a pin.
    for key in ("project", "tool_rel", "default_ref"):
        if key == "tool_rel" and tool_pin and "tool_rel" not in cfg:
            continue
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
        "TOOL_REL": cfg.get("tool_rel", ""),
        "TOOL_PIN": tool_pin,
        "DEFAULT_REF": cfg["default_ref"],
        "DEFAULT_RANGE": default_range,
        "CARDS_TARGET": cards_target,
        "CONCERNS_TARGET": f"specs/{project}/cos-concerns.md",
        "TARGETS": ("RECEIPTS.md", "TRAPS.md", "RULINGS.md", cards_target, f"specs/{project}/cos-concerns.md"),
        "CARD_HEADING_RE": re.compile(r"^## " + re.escape(project) + r"/[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"),
        "CARDS_TITLE": f"# {project} cards (R14.1; written only by {project})\n\n",
        "CONCERNS_TITLE": (f"# {project} concerns to the CoS (written only by {project}; the CoS answers in "
                           f"cos-feedback/{project}/concerns.md)\n\n"),
        "CONCERN_ID_RE": re.compile(r"^" + re.escape(project) + r"-cc-[0-9]{8}-[0-9]{1,3}$"),
        "CONCERN_HEADING_RE": re.compile(r"^### (" + re.escape(project) + r"-cc-[0-9]{8}-[0-9]{1,3})[ \t]*$", re.MULTILINE),
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

# The bus clone is only read here, never written: a partial (promisor) clone must answer from what it holds and not fetch
# a missing object from its remote mid-lookup. git reads this from 2.44; an older git ignores it.
NO_LAZY_FETCH_ENV = {"GIT_NO_LAZY_FETCH": "1"}

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


def cat_file_blob(repo: pathlib.Path, ref: str, path: str, extra_env: dict[str, str] | None = None) -> bytes:
    """The raw bytes of `path` at `ref`, or b"" if it does not exist there (a target
    missing at the tip counts as empty). A failed read is NOT absence: `git cat-file` exits 128 both
    for a missing path and for a bad ref, a non-blob or an unreadable object, so when it fails the
    path is looked up with `git ls-tree`, which exits 0 with no output only when `ref` is readable
    and carries no such path. Anything else raises Refusal TIP_BLOB_READ_FAILED, so a read failure
    is never mistaken for an empty file. `extra_env` (opt-in, NO_LAZY_FETCH_ENV for a read-only bus) reaches both git calls;
    None leaves the environment untouched."""
    proc = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "blob", f"{ref}:{path}"],
        capture_output=True, timeout=120, **({"env": {**os.environ, **extra_env}} if extra_env else {}),
    )
    if proc.returncode == 0:
        return proc.stdout
    listing = _run_git(repo, ["ls-tree", "-z", "--name-only", ref, "--", path], extra_env=extra_env)
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
    if (meta["target"] == CONCERNS_TARGET) != (meta["kind"] == "concern"):
        raise Refusal("ITEM_CONCERN_TARGET_MISMATCH", f"kind: concern and target: {CONCERNS_TARGET} go together, and only together")
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
    item = {"meta": meta, "body": body}
    if meta["target"] == CONCERNS_TARGET:
        item["concern"] = screen_concern(body)
    return item


def screen_concern(body: str) -> dict[str, str]:
    """A concern entry is a heading plus exactly the seven `- field: value` lines of CONCERN_FIELDS, each on one
    line. The field set is closed, so no field is an approval, a ruling or a permission; free text is data: the
    channel is a message to the CoS, never a grant (conjugal-fleet-advisory-channel security musts). Every entry
    expires within CONCERN_DEFAULT_DAYS of opening, and its id is the heading, so the CoS's one-line answer names
    it exactly. Returns the validated fields. Whether the id is unique and whether the entry has already expired are
    questions about OTHER items and about the clock, so the drain answers them (r14_gate), not this parse."""
    rows = [ln.rstrip() for ln in body.strip().splitlines() if ln.strip()]
    heading_id = rows[0][4:].strip()
    if not CONCERN_ID_RE.match(heading_id):
        raise Refusal("CONCERN_BAD_ID", f"the heading must be '### {PROJECT}-cc-<yyyymmdd>-<n>'")
    fields: dict[str, str] = {}
    for ln in rows[1:]:
        m = re.match(r"^- ([a-z_]+): (\S.*)$", ln)
        if not m:
            raise Refusal("CONCERN_BAD_LINE", "every line after the heading is '- <field>: <value>' on one line")
        if m.group(1) not in CONCERN_FIELDS:
            raise Refusal("CONCERN_UNKNOWN_FIELD", f"{m.group(1)}; the closed set is {', '.join(CONCERN_FIELDS)}")
        if m.group(1) in fields:
            raise Refusal("CONCERN_DUPLICATE_FIELD", m.group(1))
        fields[m.group(1)] = m.group(2).strip()
    missing = [f for f in CONCERN_FIELDS if f not in fields]
    if missing:
        raise Refusal("CONCERN_MISSING_FIELD", ", ".join(missing))
    if fields["id"] != heading_id:
        raise Refusal("CONCERN_ID_MISMATCH", "the id field must equal the heading")
    if fields["to"] != "CoS":
        raise Refusal("CONCERN_BAD_ADDRESSEE", "to: CoS is the only addressee")
    if fields["kind"] not in CONCERN_KINDS:
        raise Refusal("CONCERN_BAD_KIND", f"{fields['kind']} not in {CONCERN_KINDS}")
    for f in ("opened_utc", "expires_utc"):
        if not CONCERN_UTC_RE.match(fields[f]):
            raise Refusal("CONCERN_BAD_TIME", f"{f} must be YYYY-MM-DDTHH:MM:SSZ")
        try:
            datetime.strptime(fields[f], CONCERN_UTC_FORMAT)
        except ValueError:
            raise Refusal("CONCERN_BAD_TIME", f"{f} is not a real calendar time") from None
    opened = datetime.strptime(fields["opened_utc"], CONCERN_UTC_FORMAT)
    expires = datetime.strptime(fields["expires_utc"], CONCERN_UTC_FORMAT)
    if expires <= opened:
        raise Refusal("CONCERN_NOT_EXPIRING", "expires_utc must be after opened_utc (every message expires)")
    if expires - opened > timedelta(days=CONCERN_DEFAULT_DAYS):
        raise Refusal("CONCERN_WINDOW_TOO_LONG", f"expires_utc is more than {CONCERN_DEFAULT_DAYS} days after opened_utc "
                                                 f"(an open ask is a standing claim on the CoS; renew with a new id)")
    if not heading_id.split("-cc-")[1].startswith(fields["opened_utc"][:10].replace("-", "")):
        raise Refusal("CONCERN_ID_DATE", "the id date must be the opened_utc date")
    return fields


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

def _require_pin_at_ref(repo: pathlib.Path, ref: str) -> None:
    """Raise Unknown unless `ref` carries the pin runner AND a pins file that names TOOL_PIN. The pinned
    bytes live on the bus, so a project that runs the tool by pin keeps no copy at `tool_rel`; a ref that
    predates the pin still reads as TOOL_ABSENT_AT_REF, never as 'no debt'."""
    rc, _out, _err = git_try(repo, "cat-file", "-e", f"{ref}:{PIN_RUNNER_REL}")
    if rc != 0:
        raise Unknown("TOOL_ABSENT_AT_REF", f"{PIN_RUNNER_REL} is not at {ref}")
    rc, text, _err = git_try(repo, "show", f"{ref}:{PINS_REL}")
    if rc != 0:
        raise Unknown("TOOL_ABSENT_AT_REF", f"{PINS_REL} is not at {ref}")
    try:
        pinned = json.loads(text.lstrip("﻿")).get("tools", {})
    except (ValueError, AttributeError):
        raise Unknown("TOOL_ABSENT_AT_REF", f"{PINS_REL} at {ref} is not a pins file") from None
    if not isinstance(pinned, dict) or TOOL_PIN not in pinned:
        raise Unknown("TOOL_ABSENT_AT_REF", f"{PINS_REL} at {ref} has no pin named {TOOL_PIN}")


def require_tool_at_ref(repo: pathlib.Path, ref: str) -> None:
    """Raise Unknown unless `ref` resolves AND carries this tool and the items directory. The tool is the
    file at `tool_rel`, or, when the config names a `tool_pin`, the pin runner plus a pins file naming it.
    Called before any debt/drain answer, so a checkout that predates the tool can never
    read as 'no debt'."""
    require_configured()
    rc, _out, _err = git_try(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if rc != 0:
        raise Unknown("REF_UNRESOLVED", ref)
    if TOOL_PIN:
        _require_pin_at_ref(repo, ref)
    else:
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


def _utc_now_z() -> str:
    """The clock the drain judges a concern's expiry by, in the entry's own YYYY-MM-DDTHH:MM:SSZ form."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def r14_gate(bus_repo: pathlib.Path, tip: str, candidates: list[dict], report: dict,
             concern_dup_ids: frozenset[str] | set[str] = frozenset()) -> list[dict]:
    """The candidates that may be appended at `tip`; the rest go to report["refused"]. A key
    already on the tip is kept, so the drain reports it as sent rather than refusing history.
    `concern_dup_ids` are the concern ids that head more than one committed outbox item."""
    validator = card_validator_on_bus(bus_repo, tip)
    has_cards = any(c["target"] == CARDS_TARGET for c in candidates)
    node, node_refusal = None, None
    if has_cards:
        try:
            node = find_node(bus_repo)
        except Refusal as e:
            node_refusal = (e.code, e.detail)
    cards_link_problem = tip_parent_link_problem(bus_repo, tip, CARDS_TARGET) if has_cards and validator else None
    entry_problem = cards_tip_entry_problem(bus_repo, tip) if has_cards and validator else None
    has_concerns = any(c["target"] == CONCERNS_TARGET for c in candidates)
    concerns_link_problem = tip_parent_link_problem(bus_repo, tip, CONCERNS_TARGET) if has_concerns else None
    concerns_entry_problem = cards_tip_entry_problem(bus_repo, tip, CONCERNS_TARGET) if has_concerns else None
    concerns_on_tip = (concern_entries_on_bus(bus_repo, tip)
                       if has_concerns and not (concerns_entry_problem or concerns_link_problem) else {})
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
        elif c["target"] == CARDS_TARGET and cards_link_problem:
            refusal = ("CARDS_PATH_IS_LINK", f"{cards_link_problem}; a card is never written through a link on its path")
        elif c["target"] == CARDS_TARGET and entry_problem:
            refusal = ("CARDS_PATH_NOT_REGULAR_FILE", f"{entry_problem}; a card is never written through a link or a non-regular entry")
        elif c["target"] == CONCERNS_TARGET and not concern_reader_duty_on_bus(bus_repo, tip):
            refusal = ("CONCERN_CHANNEL_NOT_RATIFIED",
                       f"{CONCERNS_READER_FILE} at {tip[:12]} has no reader-duty line of the form '{CONCERNS_DUTY_LINE_EXAMPLE}'; "
                       f"a concern is held until the CoS has taken the reader duty, so it is never written to a file nobody reads")
        elif c["target"] == CONCERNS_TARGET and concerns_link_problem:
            refusal = ("CONCERNS_PATH_IS_LINK",
                       f"{concerns_link_problem}; a concern is never written through a link on its path")
        elif c["target"] == CONCERNS_TARGET and concerns_entry_problem:
            refusal = ("CONCERNS_PATH_NOT_REGULAR_FILE",
                       f"{concerns_entry_problem}; a concern is never written through a link or a non-regular entry")
        elif c["target"] == CONCERNS_TARGET and c["concern"]["expires_utc"] <= _utc_now_z():
            refusal = ("CONCERN_EXPIRED",
                       f"{c['concern']['id']} expired at {c['concern']['expires_utc']}; an expired ask is not published "
                       f"(open a new id if the concern still stands)")
        elif c["target"] == CONCERNS_TARGET and c["concern"]["id"] in concern_dup_ids:
            refusal = ("CONCERN_DUPLICATE_ID",
                       f"{c['concern']['id']} heads more than one committed outbox item; the CoS answers an id once, "
                       f"so an id names exactly one concern (give each its own -<n>)")
        elif c["target"] == CONCERNS_TARGET and c["concern"]["id"] in concerns_on_tip:
            refusal = ("CONCERN_DUPLICATE_ID",
                       concern_tip_duplicate_detail(c["concern"], concerns_on_tip[c["concern"]["id"]], tip))
        if refusal is None:
            kept.append(c)
        else:
            report["refused"].append({"path": c["path"], "code": refusal[0], "detail": refusal[1]})
    return kept


def concern_reader_duty_on_bus(bus_repo: pathlib.Path, tip: str) -> bool:
    """The channel is live only once the fetched tip's cos-feedback README carries the CoS-CONCERN reader-duty
    LINE, the ratification this tool's R2 route waits for. The tip decides, never a date."""
    text = bus_file_at(bus_repo, tip, CONCERNS_READER_FILE)
    return text is not None and has_concerns_duty_line(text)


def has_concerns_duty_line(text: str) -> bool:
    """True when a line EQUAL to CONCERNS_DUTY_LINE_EXAMPLE (an optional `-` or `*` bullet followed by 1 to 4 spaces or
    a tab, indented 0 to 3 spaces, and trailing whitespace allowed, nothing else) stands outside every ``` fence, ~~~
    fence and <!-- --> comment. A line that merely starts like the duty, adds a clause, forbids it or negates it is not
    the duty, and neither is one at 4 or more spaces or a tab (an indented code block), or one with 5 or more spaces
    after the bullet (the same, inside the list item). Lines end only at \\n, \\r\\n or \\r, as in Markdown: a \\v, \\f,
    \\x1c-\\x1e, \\x85, U+2028 or U+2029 does not start a new line. A fence marker inside a comment, and a comment marker
    inside a fence, are plain text (CommonMark: a fence closes on a marker of the same character at least as long as
    the opener). The scan does not parse inline code: a `<!--` inside a code span before the line opens a comment
    that never closes, so the gate stays shut. That fails closed, which is the safe side."""
    fence = ""        # the opening marker while inside a fence
    in_comment = False
    for line in re.split(r"\r\n|\r|\n", text):
        if fence:
            close = re.match(r"^ {0,3}(" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",})\s*$", line)
            if close:
                fence = ""
            continue
        if not in_comment:
            opener = _FENCE_OPEN_RE.match(line)
            if opener:
                fence = opener.group(1)
                continue
            if CONCERNS_DUTY_LINE_RE.match(line):
                return True
        pos = 0
        while True:
            marker = "-->" if in_comment else "<!--"
            hit = line.find(marker, pos)
            if hit < 0:
                break
            in_comment = not in_comment
            pos = hit + len(marker)
    return False


def concern_entries_on_bus(bus_repo: pathlib.Path, tip: str) -> dict[str, dict[str, str]]:
    """Every concern already headed in the tip's project-owned concerns file, as id -> its `- field: value` lines.
    An entry runs from its heading to the next heading or outbox marker."""
    text = bus_file_at(bus_repo, tip, CONCERNS_TARGET) or ""
    entries: dict[str, dict[str, str]] = {}
    current: dict[str, str] | None = None
    for line in text.splitlines():
        heading = CONCERN_HEADING_RE.match(line)
        if heading:
            current = entries.setdefault(heading.group(1), {})
        elif line.startswith(("### ", "<!-- outbox:")):
            current = None
        elif current is not None:
            m = re.match(r"^- ([a-z_]+): (\S.*)$", line.rstrip())
            if m:
                current.setdefault(m.group(1), m.group(2).strip())
    return entries


def concern_tip_duplicate_detail(concern: dict[str, str], published: dict[str, str], tip: str) -> str:
    """Why an id already at the tip refuses a new item: the fields that differ from the published entry, named, or
    the plain fact that none does (the same entry filed again under another outbox key)."""
    differs = [f for f in CONCERN_FIELDS if concern.get(f) != published.get(f)]
    what = (f"field(s) {', '.join(differs)} differ from that entry" if differs
            else "no field differs from that entry (the same concern filed again under another outbox key)")
    return (f"{concern['id']} is already in {CONCERNS_TARGET} at {tip[:12]}; {what}. "
            f"An id names exactly one concern (give this one its own -<n>)")


def concern_duplicate_ids(items: list[dict]) -> set[str]:
    """Ids that head more than one committed concern item. The drain cannot tell which of two is the
    real concern, so it holds all of them."""
    seen: dict[str, int] = {}
    for it in items:
        if "item" in it and "concern" in it["item"]:
            cid = it["item"]["concern"]["id"]
            seen[cid] = seen.get(cid, 0) + 1
    return {cid for cid, n in seen.items() if n > 1}


def ensure_concerns_file(path: pathlib.Path) -> None:
    """The project creates its own concerns file on its first concern (one writer, bus Law 2)."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(CONCERNS_TITLE.encode("utf-8"))


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


def cards_tip_entry_problem(bus_repo: pathlib.Path, tip: str, target: str | None = None) -> str | None:
    """Why the tip's entry for a project-owned file (the cards file, or the concerns file when `target` names it)
    may not be written through, or None. Only a missing entry (the project's first card or concern) or a regular
    100644 blob qualifies: a 120000 link, a 160000 gitlink or a 100755 file is refused, since the drain writes
    through the checkout of that path."""
    target = target or CARDS_TARGET
    proc = _run_git(bus_repo, ["ls-tree", "-z", tip, "--", target])
    if proc.returncode != 0:
        raise Refusal("TIP_BLOB_READ_FAILED", f"{tip[:12]}:{target}: ls-tree: {proc.stderr.strip()[:300]}")
    for entry in proc.stdout.split("\0"):
        meta, _tab, path = entry.partition("\t")
        if not entry or path != target:
            continue
        fields = meta.split()
        if fields[:2] != [REGULAR_FILE_MODE, "blob"]:
            return f"{target} at {tip[:12]} is mode {fields[0] if fields else '?'}, not a regular {REGULAR_FILE_MODE} blob"
    return None


def tip_parent_link_problem(bus_repo: pathlib.Path, tip: str, target: str) -> str | None:
    """Why a directory on the way to `target` at the tip is not a plain directory, or None. Each parent component is
    looked up on its own (`specs`, then `specs/<project>`): a leaf lookup through a link returns nothing, so
    cards_tip_entry_problem alone cannot see a link above the file. Only a tree (040000) or a missing component
    qualifies; a 120000 link or a 160000 gitlink is named as a mode that is not a directory, and any other entry (a
    regular file, say) is named "not a directory: mode <mode>". The walk stops at the first missing component."""
    parts = pathlib.PurePosixPath(target).parts
    for depth in range(1, len(parts)):
        prefix = "/".join(parts[:depth])
        proc = _run_git(bus_repo, ["ls-tree", "-z", tip, "--", prefix])
        if proc.returncode != 0:
            raise Refusal("TIP_BLOB_READ_FAILED", f"{tip[:12]}:{prefix}: ls-tree: {proc.stderr.strip()[:300]}")
        found = False
        for entry in proc.stdout.split("\0"):
            meta, _tab, path = entry.partition("\t")
            if not entry or path != prefix:
                continue
            found = True
            fields = meta.split()
            if fields[:2] != ["040000", "tree"]:
                mode = fields[0] if fields else "?"
                if mode in ("120000", "160000"):
                    return f"{prefix} at {tip[:12]} is mode {mode}, not a directory"
                return f"{prefix} at {tip[:12]} is not a directory: mode {mode}"
        if not found:
            return None
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
    concern_dup_ids = concern_duplicate_ids(items)
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
        candidate = {"path": it["path"], "name": it["name"], "src": src, "target": target,
                     "key": key, "block": block, "message": message}
        if "concern" in item:
            candidate["concern"] = item["concern"]
        candidates.append(candidate)

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
        candidates = r14_gate(bus_repo, tip, candidates, report, concern_dup_ids)
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
                elif c["target"] == CONCERNS_TARGET:
                    assert_no_link_in_path(wt, CONCERNS_TARGET)
                    ensure_concerns_file(wt / c["target"])
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

def _hook_tool_command(repo: pathlib.Path) -> str:
    """The shell words the guard runs ahead of `pre-push`: the file at `tool_rel`, or, when the config names a
    `tool_pin`, the project's pin runner running that pin. A pinned run executes the bytes from a temp
    directory (or in-process), where the tool cannot find the project's config beside itself, so the hook
    hands it the config path in $DOCTRINE_OUTBOX_CONFIG. The pin runner is started isolated (`-I`), the form
    its own docs give a hook."""
    if not TOOL_PIN:
        return f"\"$OUTBOX_PY\" \"$REPO_ROOT/{TOOL_REL}\""
    try:
        config = f"\"$REPO_ROOT/{CONFIG_PATH.relative_to(repo.resolve()).as_posix()}\""
    except (ValueError, AttributeError):
        config = shlex.quote(CONFIG_PATH.as_posix()) if CONFIG_PATH is not None else f"\"$REPO_ROOT/{CONFIG_NAME}\""
    return f"{CONFIG_ENV}={config} \"$OUTBOX_PY\" -I \"$REPO_ROOT/{PIN_RUNNER_REL}\" run {TOOL_PIN} --"


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
               f"  {_hook_tool_command(repo)} pre-push \"$REPO_ROOT\" \"$@\" < \"$OUTBOX_STDIN\" || {{ OUTBOX_RC=$?; rm -f \"$OUTBOX_STDIN\"; exit $OUTBOX_RC; }}\n"
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
        # Invariant: a guard is live only when no top-level `exit`/`exec` stands ahead of it. A guard behind
        # one is dead code: git runs the hook, the hook ends first, and the push goes through unjudged, so
        # "present" or "repaired" may be reported only for a block that ends up reachable. The refusal is
        # checked only at the new index, after the move below, never at the old position: a recognised block
        # is taken out and re-inserted at the splice point, so where it used to sit does not matter. A hook
        # that ends in `exit 0` and was given its guard by appending at the end holds exactly such a dead
        # older block, and that is the population this repair exists for.
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
            # puts it. The check below is a defensive invariant: `_preamble_end` stops at the first line that
            # is not the shebang, a `set` line, a comment or blank, and none of those is a terminator, so it
            # cannot fire today; it keeps "the new position is reachable" true if `_preamble_end` changes.
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
    # The refusal below is the same defensive invariant as in the repair path: it cannot fire while
    # `_preamble_end` only skips the shebang, `set` lines, comments and blanks.
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


# ---------- card routes: check every command a card names (validate --exercise) ----------
#
# CoS work order FWO-DEFECT-PREVENT-1 item 3 (ruling candidate `steward-handoff-card-route-is-live-and-exercised-r1`):
# a card that names a command nobody ran ships a route that does not exist (#468 adoption steps; #469 `doctrine-sync debt`).
# CARD-ROUTE-EXERCISE-2 is the fail-closed successor of CARD-ROUTE-EXERCISE-1 (PR 479, parked): that card decided which script
# a launcher would run with two hand-written walks over one argv (a safety matcher and a script discovery), and every review
# round found a spelling where they disagreed. r1 of this card then kept a DENYLIST of launcher names, so any code-running
# program it did not list (pytest) was run. Fail-closed is the opposite: only an explicitly ALLOWLISTED program is ever run
# (ROUTE_EXEC_ALLOW, decided by its bare name and its resolved executable); everything else is existence-checked and skipped.
#
# WHAT COUNTS AS A ROUTE COMMAND (the extraction rule, applied to the item body):
#   1. Every non-blank line of a fenced block whose info string starts with `route` (```route or ~~~route), except a
#      line that starts with `#`; a leading `$ ` or `> ` is dropped.
#   2. Every backticked span with two or more words inside a ROUTE SECTION. A section opens at a line whose label is
#      `Route:` or `Adopt:` (bullet and bold allowed, any case) and runs to the next blank line, `#` heading or fence.
#      Adjacent bullets belong to the section, so a `Pin:` bullet right under `Adopt:` is checked too.
#   A one-word span (`debt`, `RULINGS.md`) is a name, not a command. Spans outside a section are prose and are not run.
# Commands are split without a shell (POSIX shlex; on Windows non-POSIX shlex so a backslash path stays whole), one per
# line, and checked in a scratch clone of the bus. `<name>` placeholders are filled from --fill name=VALUE (`<scratch>` is
# the scratch clone itself); one left unfilled is ROUTE_FAILED on a route that is run and is only NOTED on one that is not.
#
# THREE STEPS PER ROUTE, in this order:
#   1. SAFETY SCREEN, for EVERY route (route_unsafe_reason). It runs nothing: a shell operator, a URL, a credential, a
#      network client, `gh`, git off its read-only option lists (below) or anything inside the live bus is ROUTE_UNSAFE.
#   2. STATIC EXISTENCE CHECK, for EVERY route. It runs nothing. The route's program must resolve (PATH, or a path inside the
#      scratch clone), and every script it names (python/py, node and its -r/--import preloads, a shell and its --rcfile, deno,
#      bun, pwsh/powershell, awk -f, perl/ruby/php/lua/Rscript/osascript/tclsh) must be a file that resolves, symlinks and
#      junctions followed, inside the scratch clone. The program is read by what it RESOLVES to as well as by its name, so a
#      link named `helper.exe` that is really python has its script checked. A miss is ROUTE_MISSING with a typed reason first
#      in its detail: PROGRAM_NOT_FOUND, PROGRAM_IS_SHIM (a `.cmd`/`.bat` shim cannot start without `cmd /c`), SCRIPT_NOT_FOUND
#      or SCRIPT_OUTSIDE_CLONE. WHICH token is "the script" is a best-effort reading of the program's options, and nothing is
#      launched on the strength of it.
#   3. EXECUTION, only when the program is on the EXECUTION ALLOWLIST (ROUTE_EXEC_ALLOW: `git`). The decision is the program as
#      the route spells it, a bare name (`git`, `git.exe`; never a path, so `tools/git` and `/usr/bin/git` are not on it), AND
#      the executable it resolves to: the first `git` on a PATH stripped of every relative entry and every entry inside the
#      scratch clone, whose real path is not inside the clone and is itself called git (route_git_binary). Anything else (a
#      shell, an interpreter, pytest, a renamed copy, a link, an unknown name) that passes steps 1 and 2 is `ROUTE_NOT_EXECUTED`:
#      a skip, NOT a failure, so a card's `py -3 tools/x.py` still validates. It is NEVER launched, whatever its arguments, so
#      there is no second grammar to disagree with the script reading above (`pwsh <outside.ps1> -File <in-clone>`, `node -r
#      <in-clone> <outside.js>`, `bash --rcfile <in-clone> <outside.sh>`, `python --check-hash-based-pycs default <outside.py>`
#      and the other PR 479 repros are all routes off the allowlist). Widening the allowlist is the separate later card
#      CARD-ROUTE-EXERCISE-LAUNCHER-ALLOWLIST-1: exact shapes, a pinned executable, and secondary execution inputs (preload
#      flags, environment) have to be closed first.
#
# OUTCOMES, one line each: `ROUTE_OK|ROUTE_NOT_EXECUTED|ROUTE_MISSING|ROUTE_FAILED|ROUTE_TIMEOUT|ROUTE_UNSAFE <item> <command>`.
#   OK        it ran and exited 0.
#   NOT_EXECUTED  its program is not on the execution allowlist and it passed steps 1 and 2; not run, not a failure. An unfilled
#             `<placeholder>` is noted in its detail, and the script check skips an operand that holds one.
#   MISSING   the static check failed (the typed reasons above).
#   FAILED    it ran and exited non-zero, or a placeholder was left unfilled on a route that is run, or the line does not parse.
#   TIMEOUT   it ran past --timeout seconds; its whole process tree was killed (a Job Object on Windows, a process
#             group on POSIX), so nothing outlives the call. Only the last 64 KiB of each output stream is kept.
#   UNSAFE    it was NOT run: a route must be offline, credential-free and confined to the scratch clone.
#             Refused: a shell operator (pipe, `&&`, `;`, redirect); an environment-assignment prefix; a URL or `user@host:`
#             remote in any argument; a credential assignment (`NAME=value` for ANTHROPIC_*, GH_*, *_TOKEN, *_KEY, *PASSWORD*,
#             ...) or `--token`/`--password`; any `gh`; claude/codex `auth|login|logout`; a network client (curl, ssh, npx, ...);
#             `find -exec`/`-delete`; a `git` that resolves only inside the clone or to a program that is not git; and every git
#             route that is not READ-ONLY GIT ON A POSITIVE LIST (r5 hub ruling; real card routes run no git, so nothing real is
#             lost): before the verb only `-C`, inside the scratch clone and never into (or under) a bare repository layout (`-c`,
#             `--config-env`, an alternate git directory in any form or abbreviation, ROUTE_GIT_ALT_DIR_OPTIONS, and every other
#             global option are refused); the verb one of ROUTE_GIT_VERBS (no verb that can write: no commit, checkout, merge,
#             apply, stash, ...; `branch` and `tag` in list form only; `config` in a read mode only); each option on that verb's
#             own list (_route_git_options_reason): an EXACT token, so an abbreviation is refused; a long value only as
#             `--name=value`; a short cluster letter by letter, a value letter taking the rest of the cluster or the next token;
#             `--` ends the options. Every option value and every positional must resolve inside the scratch clone (`..`,
#             symlinks, junctions, case), and a positional starting with `:` (pathspec magic, an index path) is refused.
#   LIVE BUS HUB RULING (r2, binding): "refuse if --bus resolves to the live clone" means NO ROUTE runs with its working
#             directory, a `-C`/`--git-dir`/`--work-tree` target, or any argument or --fill value (also the value of
#             `--opt=VALUE`) that resolves INSIDE the live bus. The default $FACTORY_BUS_CLONE is still cloned, read-only.
#             Identity is decided on `os.path.realpath` + `normcase` (junctions, symlinks and 8.3 names followed, `..`
#             collapsed, case folded, trailing separators dropped) and compared by containment: a route that does is
#             ROUTE_UNSAFE, and a --scratch-root inside the bus is exit 2.
# A route that IS run gets the interpreter-injection environment stripped (PYTHONPATH, PYTHONSTARTUP, PYTHONHOME, NODE_OPTIONS,
# NODE_PATH, PERL5OPT, PERL5LIB, RUBYOPT, BASH_ENV, ENV), every credential-shaped variable, every GIT_* variable (GIT_EDITOR,
# GIT_SEQUENCE_EDITOR, GIT_PAGER, GIT_ASKPASS, GIT_SSH, GIT_SSH_COMMAND, GIT_EXTERNAL_DIFF, GIT_CONFIG_PARAMETERS, ...) and the
# generic helpers EDITOR, VISUAL, PAGER and SSH_ASKPASS (route_exercise_env), with GIT_CONFIG_NOSYSTEM=1, GIT_ATTR_NOSYSTEM=1 and
# GIT_CONFIG_GLOBAL an empty file beside the clone, so no driver or helper defined in the user, XDG or system config is reachable.
# Git itself is started as `git -c core.pager=cat -c core.editor=false -c diff.external= -c core.attributesFile= -c core.excludesFile=
# -c core.hooksPath=<an empty directory in the scratch> ...` (ROUTE_GIT_HARDENING, route_git_command), so no pager, editor, external
# diff, hook or signing program is reachable even through a verb that wants one.
# The SCRATCH CLONE is made under the same isolation (run_isolated_git: the same git, environment and `-c` pairs a route gets), so a
# filter, hook or helper the host's config defines (git-lfs in the system config) never starts while the bus is being checked out.
# Preparation also shares the route path's BOUNDS (run_bounded_process): output kept to its tail, the whole process tree killed at
# ROUTE_PREP_TIMEOUT, and a timeout is the route outcome ROUTE_TIMEOUT (an Unknown, exit 2: it is no route's result). The stale-bus
# check (bus_commits_behind_origin) reads the live bus through the same isolated git, so a host GIT_DIR or config cannot change it.
# A credential's value is never printed: a diagnostic, a ROUTE line or an error shows `NAME=<redacted>`.
# Any outcome but OK and NOT_EXECUTED fails the run (exit 1). The commands run in `git clone --local --no-hardlinks`
# of --bus (default $FACTORY_BUS_CLONE) with `origin` removed, under a scratch directory this tool creates and removes by name.
# --bus is only READ, by that clone; no route runs in it. The scratch root may not be inside it (exit 2).
# A bus clone behind its own origin/master prints a `ROUTE_BUS_STALE <behind>` warning on stderr; the exit is unchanged.

ROUTE_LABEL_RE = re.compile(r"^[ \t]*(?:[-*][ \t]+)?(?:\*\*)?(?:route|adopt)(?:\*\*)?[ \t]*:(?:\*\*)?", re.I)
ROUTE_FENCE_INFO_RE = re.compile(r"^route\b", re.I)
ROUTE_SPAN_RE = re.compile(r"`([^`\n]+)`")
ROUTE_HEADING_RE = re.compile(r"^ {0,3}#{1,6}\s")
ROUTE_PLACEHOLDER_RE = re.compile(r"<([A-Za-z][\w.-]*)>")
ROUTE_PROMPT_RE = re.compile(r"^[$>]\s+")
ROUTE_SHELL_OPERATOR_RE = re.compile(r"^(?:&&|\|\||\||;|&|[0-9]?[<>]+.*)$")
ROUTE_URL_RE = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:)", re.I)
ROUTE_CREDENTIAL_RE = re.compile(r"(?:ANTHROPIC_|CLAUDE_CODE_OAUTH|GITHUB_TOKEN|GH_TOKEN|API_KEY|AUTH_TOKEN|--token|--password)", re.I)
_ROUTE_CRED_NAME = (r"(?:[^=\s'\"]*(?:TOKEN|PASSWORD|PASSWD|SECRET|CREDENTIAL|ANTHROPIC_|CLAUDE_CODE_OAUTH)[^=\s'\"]*"
                    r"|[^=\s'\"]*[_-]KEY|(?:GH|GITHUB)_[^=\s'\"]*)")
ROUTE_CRED_ASSIGN_RE = re.compile(rf"^{_ROUTE_CRED_NAME}=", re.I)
ROUTE_CRED_ASSIGN_ANY_RE = re.compile(rf"(?P<name>{_ROUTE_CRED_NAME})=(?:\"[^\"]*\"|'[^']*'|\S*)", re.I)
ROUTE_CRED_FLAG_RE = re.compile(r"(?P<flag>--(?:token|password)[\w-]*)(?P<sep>\s+)(?!<redacted>)(?:\"[^\"]*\"|'[^']*'|\S+)", re.I)
ROUTE_URL_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s@]+@", re.I)
ROUTE_CRED_NAME_RE = re.compile(r"token|password|passwd|secret|credential|api[_-]?key|[_-]key$", re.I)
ROUTE_ENV_PREFIX_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
ROUTE_NETWORK_CLIENTS = frozenset({"curl", "wget", "ssh", "scp", "sftp", "ftp", "rsync", "nc", "ncat", "telnet",
                                   "invoke-webrequest", "iwr", "invoke-restmethod", "irm", "npx", "pnpx", "bunx", "uvx",
                                   "apt", "apt-get", "brew", "choco", "winget", "scoop", "bitsadmin", "certutil"})
ROUTE_FIND_RUNS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls"})
ROUTE_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh"})
ROUTE_PYTHONS = frozenset({"python", "pythonw", "pypy", "py"})
ROUTE_POWERSHELLS = frozenset({"pwsh", "powershell", "pwsh-preview"})
ROUTE_INLINE_INTERPRETERS = {"ruby": {"-e"}, "perl": {"-e", "-E"}, "php": {"-r"}, "lua": {"-e"}, "rscript": {"-e"},
                             "osascript": {"-e"}, "deno": {"eval"}, "bun": {"-e", "--eval", "-p", "--print"}}
ROUTE_AWKS = frozenset({"awk", "gawk", "mawk", "nawk"})
ROUTE_SCRIPT_EXT_RE = re.compile(r"\.[cm]?[jt]sx?$", re.I)
ROUTE_DENO_SCRIPT_VERBS = frozenset({"run", "test", "bench", "check", "compile", "serve"})
ROUTE_DENO_OTHER_VERBS = frozenset({"task", "fmt", "lint", "doc", "info", "install", "uninstall", "upgrade", "repl", "init", "add",
                                    "remove", "outdated", "publish", "jsr", "bundle", "cache", "clean", "completions", "coverage",
                                    "types", "vendor", "eval", "help"})
ROUTE_DENO_OPERAND_FLAGS = frozenset({"-c", "--config", "--import-map", "--cert", "--location", "--seed"})
ROUTE_BUN_OTHER_VERBS = frozenset({"install", "i", "add", "a", "remove", "rm", "update", "upgrade", "link", "unlink", "pm", "x",
                                   "create", "init", "build", "test", "exec", "repl", "audit", "outdated", "publish", "patch",
                                   "info", "why", "completions", "help"})
ROUTE_BUN_OPERAND_FLAGS = frozenset({"--cwd", "-c", "--config", "--env-file", "--tsconfig-override", "--port", "--define", "-d",
                                     "--loader", "-l", "-r", "--preload", "--require", "--import"})
ROUTE_PRELOAD_FLAGS = {"node": frozenset({"-r", "--require", "--import", "--loader", "--experimental-loader"}),
                       "bun": frozenset({"-r", "--preload", "--require", "--import"}),
                       "shell": frozenset({"--rcfile", "--init-file"})}
ROUTE_EXEC_ALLOW = frozenset({"git"})                   # the ONLY programs a route is ever run with (route_runs)


class RouteGitVerb(NamedTuple):
    """The options one read-only git verb accepts in a route; anything else is ROUTE_UNSAFE (_route_git_options_reason).
    flags: exact long or short tokens that take no value. valued: long names accepted only as `--name=value`. letters / value_letters:
    the short letters of a cluster; a value letter takes the rest of the cluster, or the next token when it is last. digits: `-<N>`
    is accepted. positional: "any", "none", or "list" (only after a list flag or letter, so `branch fresh` never creates a branch).
    first: when set, the first token after the verb must be one of these (config: a read mode before anything else)."""
    flags: frozenset[str] = frozenset()
    valued: frozenset[str] = frozenset()
    letters: frozenset[str] = frozenset()
    value_letters: frozenset[str] = frozenset()
    digits: bool = False
    positional: str = "any"
    list_flags: frozenset[str] = frozenset()
    list_letters: frozenset[str] = frozenset()
    first: frozenset[str] = frozenset()


def _verb(flags: str = "", valued: str = "", letters: str = "", value_letters: str = "", **rest) -> RouteGitVerb:
    return RouteGitVerb(frozenset(flags.split()), frozenset(valued.split()), frozenset(letters), frozenset(value_letters), **rest)


_ROUTE_GIT_DIFF_FLAGS = "--stat --shortstat --numstat --name-only --name-status --no-color --no-textconv --no-ext-diff --no-patch"
ROUTE_GIT_VERBS: dict[str, RouteGitVerb] = {          # read-only verbs only; the per-verb lists are the whole option grammar
    "log": _verb(f"{_ROUTE_GIT_DIFF_FLAGS} --oneline --all --branches --tags --reverse --first-parent --no-merges --merges "
                 "--abbrev-commit --decorate --no-decorate",
                 "--format --pretty --since --until --author --grep --max-count --date", "s", "nS", digits=True),
    "show": _verb(f"{_ROUTE_GIT_DIFF_FLAGS} --oneline --abbrev-commit", "--format --pretty", "s"),
    "diff": _verb(f"{_ROUTE_GIT_DIFF_FLAGS} --cached --staged --word-diff --quiet --exit-code", "", "s"),
    "grep": _verb("", "", "nilcwFE", "e"),
    "rev-parse": _verb("--verify --short --abbrev-ref --symbolic-full-name --show-toplevel --git-dir --is-inside-work-tree "
                       "--is-bare-repository --quiet", "", "q"),
    "rev-list": _verb("--count --all --reverse --first-parent --no-merges --merges", "--max-count --since --until", "", "n",
                      digits=True),
    "status": _verb("--short --porcelain --branch", "", "sb"),
    "ls-files": _verb("--cached --deleted --modified --others --stage --exclude-standard --full-name --error-unmatch", "",
                      "cdmostz"),
    "ls-tree": _verb("--name-only --name-status --full-name --full-tree", "", "rtdlz"),
    "cat-file": _verb("", "", "etsp"),
    "describe": _verb("--tags --always --long --all"),
    "blame": _verb("--porcelain --line-porcelain --root --no-textconv", "", "slewf", "L"),
    "shortlog": _verb("--summary --numbered --email", "", "sne"),
    "merge-base": _verb("--is-ancestor --all"),
    "for-each-ref": _verb("", "--format --sort --count"),
    "show-ref": _verb("--heads --tags --verify --hash --quiet", "", "q"),
    "name-rev": _verb("--name-only --tags"),
    "count-objects": _verb("--verbose --human-readable", "", "vH", positional="none"),
    "version": _verb("--build-options", positional="none"),
    "branch": _verb("--list --remotes --verbose --show-current --no-color", "", "rv", positional="list",
                    list_flags=frozenset({"--list"})),                  # no `-l`: an older git reads it as --create-reflog
    "tag": _verb("--list", "", "l", positional="list", list_flags=frozenset({"--list"}), list_letters=frozenset("l")),
    "config": _verb("--get --get-all --get-regexp --list --show-origin", "", "l",
                    first=frozenset({"--get", "--get-all", "--get-regexp", "--list", "-l"})),
}
ROUTE_GIT_ALT_DIR_OPTIONS = frozenset({"--git-dir", "--work-tree", "--namespace", "--super-prefix", "--bare"})   # another repository or tree
ROUTE_GIT_HARDENING = ("core.pager=cat", "core.editor=false", "diff.external=", "core.fsmonitor=false", "commit.gpgsign=false",
                       "tag.gpgsign=false", "safe.bareRepository=explicit", "gpg.program=false", "gpg.ssh.program=false",
                       "gpg.x509.program=false", "core.attributesFile=", "core.excludesFile=")  # `-c` pairs before every run git
ROUTE_NO_HOOKS_DIR = "outbox-route-no-hooks"                                                   # empty, beside the clone in the scratch
ROUTE_EMPTY_GLOBAL_CONFIG = "outbox-route-empty-gitconfig"                                      # empty file, beside the clone: GIT_CONFIG_GLOBAL
ROUTE_HELPER_ENV = frozenset({"EDITOR", "VISUAL", "PAGER", "SSH_ASKPASS"})                     # helpers git reads besides GIT_*
ROUTE_STRIPPED_ENV_RE = re.compile(r"(?:TOKEN|SECRET|PASSWORD|API_?KEY|CREDENTIAL)|^(?:ANTHROPIC_|CLAUDE_CODE_|GH_|GIT_|GITHUB_)", re.I)
ROUTE_INJECTION_ENV = frozenset({"PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH", "PERL5OPT", "PERL5LIB",
                                 "RUBYOPT", "BASH_ENV", "ENV"})
ROUTE_SCRATCH_PREFIX = "outbox-exercise-"
ROUTE_DEFAULT_TIMEOUT_S = 60.0
ROUTE_PREP_TIMEOUT = 120.0                                                                      # seconds per git call that prepares the scratch
ROUTE_OUTPUT_CAP_BYTES = 64 * 1024
ROUTE_BUS_ENV = "FACTORY_BUS_CLONE"
ROUTE_PASSING = ("ROUTE_OK", "ROUTE_NOT_EXECUTED")
ROUTE_STATUSES = (*ROUTE_PASSING, "ROUTE_MISSING", "ROUTE_FAILED", "ROUTE_TIMEOUT", "ROUTE_UNSAFE")


def extract_routes(body: str) -> list[str]:
    """The route commands an item body names, in order and without repeats; the rule is the block comment above."""
    routes: list[str] = []
    fence: tuple[str, int, bool] | None = None
    in_section = False

    def add(command: str) -> None:
        if command and command not in routes:
            routes.append(command)

    for raw in body.splitlines():
        line = raw.rstrip("\r")
        opened = _FENCE_OPEN_RE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= fence[1] and not line.strip(" `~"):
                fence = None
            elif fence[2]:
                command = ROUTE_PROMPT_RE.sub("", line.strip())
                if command and not command.startswith("#"):
                    add(command)
            continue
        if opened:
            info = line.strip()[len(opened.group(1)):].strip()
            fence = (opened.group(1)[0], len(opened.group(1)), bool(ROUTE_FENCE_INFO_RE.match(info)))
            in_section = False
            continue
        if not line.strip() or ROUTE_HEADING_RE.match(line):
            in_section = False
            continue
        if ROUTE_LABEL_RE.match(line):
            in_section = True
        if in_section:
            for span in ROUTE_SPAN_RE.findall(line):
                command = ROUTE_PROMPT_RE.sub("", span.strip())
                if len(command.split()) >= 2:
                    add(command)
    return routes


def route_program_name(program: str) -> str:
    """The lower-case program name of an argv[0]: directory and `.exe/.cmd/.bat/.com/.ps1` dropped, a trailing version
    dropped (`python3.12` and `python3` read as `python`, `perl5.36` as `perl`)."""
    name = re.sub(r"\.(?:exe|cmd|bat|com|ps1)$", "", re.split(r"[\\/]", program)[-1].lower())
    return re.sub(r"[\d.\-]+$", "", name) or name


def route_runs(argv: list[str]) -> bool:
    """True when the route's program is on the execution ALLOWLIST (ROUTE_EXEC_ALLOW): its bare name, `git` or `git.exe`, never
    a path (`tools/git`, `/usr/bin/git`). Nothing else is ever launched; a route this refuses is only existence-checked."""
    if not argv:
        return False
    name = os.path.normcase(argv[0])
    return name in ROUTE_EXEC_ALLOW or (name.endswith(".exe") and name[:-4] in ROUTE_EXEC_ALLOW)


def route_pathext() -> list[str]:
    """The extensions a Windows lookup appends, in PATHEXT order; empty off Windows."""
    if os.name != "nt":
        return []
    return [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]


def route_program_candidates(entry: str, program: str) -> list[str]:
    """The files a lookup of `program` tries in one directory: the name itself, or on Windows the name plus each PATHEXT
    extension in order (the bare name first only when it already ends in one of them)."""
    exts = route_pathext()
    if not exts:
        return [os.path.join(entry, program)]
    spelled = [os.path.join(entry, program)] if program.lower().endswith(tuple(e.lower() for e in exts)) else []
    return [*spelled, *(os.path.join(entry, program + e) for e in exts)]


def route_git_candidates(entry: str) -> list[str]:
    """The files a `git` lookup tries in one directory: `git` itself, or on Windows `git` plus each PATHEXT extension in order."""
    return route_program_candidates(entry, "git")


def route_absolute_entries(path_value: str | None) -> list[str]:
    """The ABSOLUTE directories of a PATH value, in order. A relative entry (`.`, empty, `bin`, `..\\x`), a quoted entry
    (`"C:\\x"`, whose quotes make it relative to the current directory) and one with a NUL are dropped: none is a place a lookup
    may search, because each one means a directory under the current one."""
    return [e for e in (path_value or "").split(os.pathsep) if e and "\0" not in e and os.path.isabs(e)]


def route_find_program(entries: Iterable[str], program: str) -> str | None:
    """The first existing, executable regular file named `program` (PATHEXT spellings on Windows) in the ABSOLUTE directories
    `entries`, in order; None when there is none. Walked by hand because shutil.which puts the current directory in front of
    any `path=` on Windows (unless NoDefaultCurrentDirectoryInExePath is set), so it would find a program planted in the clone it
    runs from."""
    for entry in entries:
        for candidate in route_program_candidates(entry, program):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def route_find_git(entries: Iterable[str]) -> str | None:
    """route_find_program for `git`: the first git in the ABSOLUTE directories `entries`, else None."""
    return route_find_program(entries, "git")


def route_git_binary(cwd: str | os.PathLike) -> tuple[str | None, tuple[str, str] | None]:
    """The git executable a route may run, as (path, None), else (None, (ROUTE_* status, detail)). The first `git` on a PATH
    stripped of every relative entry (`.`, empty) and every entry inside the scratch clone `cwd`, so a git planted in the clone
    (a copy, a hardlink, a link, a renamed program) is never found. Resolution never consults the current directory (the
    implicit search shutil.which makes on Windows): only the absolute PATH entries are walked (route_find_git). The REAL path
    of what is found must not lie inside the clone and must itself be called git, and it must not be a `.cmd`/`.bat` shim."""
    scratch = route_real_path(str(cwd))
    absolute, kept = [], []
    for entry in route_absolute_entries(os.environ.get("PATH", "")):
        try:
            absolute.append(entry)
            if not route_inside(route_real_path(entry), scratch):
                kept.append(entry)
        except (OSError, ValueError):
            continue
    found = route_find_git(kept)
    if found is None:
        if route_find_git(absolute) is not None:
            return None, ("ROUTE_UNSAFE", "git resolves only inside the scratch clone; a git the bus carries is never run")
        return None, ("ROUTE_MISSING", "PROGRAM_NOT_FOUND: git: no such program on PATH")
    real = route_real_path(found)
    if route_inside(real, scratch):
        return None, ("ROUTE_UNSAFE", f"git resolves to {found!r}, inside the scratch clone; a git the bus carries is never run")
    if pathlib.PurePath(real).stem.lower() != "git":
        return None, ("ROUTE_UNSAFE", f"git resolves to {pathlib.PurePath(real).name!r}, which is not a git binary")
    if os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        return None, ("ROUTE_MISSING", f"PROGRAM_IS_SHIM: git: resolves to a {pathlib.PurePath(found).suffix.lower()} shim ({found}); "
                                       "a shim is not exercisable without cmd /c, which a route may not use")
    return found, None


def route_git_command(git_bin: str, argv: list[str], cwd: str | os.PathLike) -> list[str]:
    """The command line git is started with: `-c` overrides that switch off every pager, editor, external diff, hook, fsmonitor
    and signing helper (ROUTE_GIT_HARDENING and `core.hooksPath` = an empty directory beside the clone), then the route's own
    words untouched. A route may not pass `-c` or any other global option but `-C` (_route_git_unsafe), so nothing it says can
    undo these."""
    hooks = pathlib.Path(cwd).parent / ROUTE_NO_HOOKS_DIR
    hooks.mkdir(exist_ok=True)
    pairs = [*ROUTE_GIT_HARDENING, f"core.hooksPath={hooks}"]
    return [git_bin, *(part for pair in pairs for part in ("-c", pair)), *argv[1:]]


def route_secret_values(template: list[str], fills: dict[str, str]) -> list[str]:
    """The --fill values bound to a credential, found on the TEMPLATE argv (before any value is substituted), so a value
    with whitespace in it is one secret and never a run of tokens: every placeholder in a token that names a credential
    (`NAME=<v>`, `--password=<v>`) or follows `--token`/`--password`, plus every fill whose own name reads as one."""
    names = {n for n, v in fills.items() if v and ROUTE_CRED_NAME_RE.search(n)}
    for i, tok in enumerate(template):
        after_flag = i > 0 and re.fullmatch(r"--(?:token|password)[\w-]*", template[i - 1], re.I) is not None
        if after_flag or ROUTE_CREDENTIAL_RE.search(tok) or ROUTE_CRED_ASSIGN_RE.match(tok):
            names.update(m.group(1) for m in ROUTE_PLACEHOLDER_RE.finditer(tok))
    values = {fills[n] for n in names if fills.get(n)}
    for value in list(values):                   # a route may echo one line of a multi-line value, or one word of a multiword one
        values.update(part for part in re.split(r"\s+", value) if len(part) >= 3)
    return sorted(values, key=lambda v: (-len(v), v))


def redact_secret_values(text: str, secrets: Iterable[str] = ()) -> str:
    """`text` with every secret (route_secret_values) replaced by `<redacted>`, longest first, wherever it appears: as it
    is, and as repr() writes it (a backslash doubled, a quote escaped), so a `!r` site cannot print what a plain one hides."""
    forms = {form for x in secrets if x for form in (x, repr(x), repr(x)[1:-1]) if form}
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "<redacted>")
    return text


def redact_route_text(text: str, secrets: Iterable[str] = ()) -> str:
    """Route text as shown to a person: a credential assignment keeps its name and loses its value
    (`GH_TOKEN=abc` -> `GH_TOKEN=<redacted>`), as do the value after `--token`/`--password` and the user-info of a URL.
    `secrets` (route_secret_values) are replaced first, longest first, wherever they appear and whatever they contain."""
    text = redact_secret_values(text, secrets)
    text = ROUTE_CRED_ASSIGN_ANY_RE.sub(lambda m: f"{m.group('name')}=<redacted>", text)
    text = ROUTE_CRED_FLAG_RE.sub(lambda m: f"{m.group('flag')}{m.group('sep')}<redacted>", text)
    return ROUTE_URL_USERINFO_RE.sub(r"\1<redacted>@", text)


def route_real_path(path: str, base: str | None = None) -> str:
    """A path in the form two spellings of one place share: `~` and `..` collapsed, junctions/symlinks followed
    (8.3 short names expand on Windows), case folded where the filesystem folds it, no trailing separator."""
    expanded = os.path.expanduser(path)
    if base is not None and not os.path.isabs(expanded):
        expanded = os.path.join(base, expanded)
    return os.path.normcase(os.path.realpath(expanded))


def route_inside(path: str, root: str) -> bool:
    """True when the already-normalized `path` is `root` or lies under it."""
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def _route_path_candidates(tok: str) -> list[str]:
    """What a token may name as a path: the token, and the part after `=` of `--opt=VALUE`."""
    found = [tok]
    if "=" in tok:
        found.append(tok.split("=", 1)[1])
    return [c for c in found if c and "\0" not in c]


def route_live_bus_reason(argv: list[str], cwd: str, bus: str, secrets: Iterable[str] = ()) -> str | None:
    """Why a route touches the live bus: its cwd, or any argument (or the value of `--opt=VALUE`) resolves inside it.
    Decided on identity (real path, case-folded, `..` collapsed), so every spelling of the bus path is caught."""
    live = route_real_path(bus)
    if route_inside(route_real_path(cwd), live):
        return "the route's working directory is inside the live bus"
    for tok in argv:
        for candidate in _route_path_candidates(tok):
            try:
                inside = route_inside(route_real_path(candidate, cwd), live)
            except (OSError, ValueError):                       # not a path (`HEAD:master`): nothing to compare
                continue
            if inside:
                return f"argument {redact_route_text(tok, secrets)!r} resolves inside the live bus; a route works in the scratch clone only"
    return None


def _route_git_long_refused(tok: str, refused: Iterable[str]) -> str | None:
    """The refused long option `tok` names, else None: `tok` is `--name` or `--name=value`, and git reads any unambiguous prefix of
    a long option as that option (`--open-files` is `--open-files-in-pager`, `--out` is `--output`), so every `--name` that is a
    PREFIX of a refused option counts, fail-closed (an ambiguous prefix git would reject is refused too)."""
    if not tok.startswith("--") or len(tok) < 3:
        return None
    name = tok[2:].split("=", 1)[0]
    if not name:
        return None
    return next((full for full in sorted(r.lstrip("-") for r in refused) if full.startswith(name)), None)


def _route_git_bare_layout(path: str, scratch: str) -> str | None:
    """The directory between `path` and the scratch clone root `scratch` (inclusive of `path`, exclusive of the root) that is a
    bare repository layout (`HEAD` file plus `objects/` and `refs/`), else None. `git -C fx` would open it as the repository
    and read its config (a textconv or filter driver) unless safe.bareRepository says otherwise; the route screen refuses it."""
    current = route_real_path(path)
    while route_inside(current, scratch) and current != scratch:
        if (os.path.isfile(os.path.join(current, "HEAD")) and os.path.isdir(os.path.join(current, "objects"))
                and os.path.isdir(os.path.join(current, "refs"))):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def _route_git_unsafe(argv: list[str], cwd: str, secrets: Iterable[str] = ()) -> str | None:
    """git: before the verb only `-C` into a directory inside the scratch clone (`cwd`) that is not itself, nor sits under, a
    bare repository layout (`-c`, an alternate git directory and every other global option are refused); then one READ-ONLY verb
    of ROUTE_GIT_VERBS with only the options on its own list, every value and positional inside the clone
    (_route_git_options_reason)."""
    scratch = route_real_path(cwd)
    here = cwd
    i, sub = 1, None
    while i < len(argv):
        tok = argv[i]
        if tok == "-c":
            return "git -c: a config override can smuggle in an alias, a helper or a pager"
        alternate = _route_git_long_refused(tok, ROUTE_GIT_ALT_DIR_OPTIONS)
        if alternate:
            tok_name = tok.split("=", 1)[0]
            if alternate in ("git-dir", "work-tree") and "=" not in tok and i + 1 < len(argv):
                value = argv[i + 1]                                     # next-token form: its value is the target
            else:
                value = tok.split("=", 1)[1] if "=" in tok else None
            if alternate in ("git-dir", "work-tree") and value:
                target = os.path.join(here, os.path.expanduser(value))
                try:
                    outside = not route_inside(route_real_path(target), scratch)
                except (OSError, ValueError):
                    outside = True
                if outside:
                    return f"git {tok_name} targets {redact_route_text(target, secrets)!r}, outside the scratch clone"
            return f"git {tok_name} selects another git directory or work tree; a route runs in the scratch clone's own repository"
        if tok == "-C" and i + 1 < len(argv):
            here = os.path.join(here, os.path.expanduser(argv[i + 1]))
            try:
                outside = not route_inside(route_real_path(here), scratch)
            except (OSError, ValueError):
                outside = True
            if outside:
                return f"git -C targets {redact_route_text(here, secrets)!r}, outside the scratch clone"
            bare = _route_git_bare_layout(here, scratch)
            if bare is not None:
                return (f"git -C targets {redact_route_text(here, secrets)!r}, which is or sits under a bare repository layout "
                        f"({pathlib.PurePath(bare).name}); git would open it as the repository and read its config")
            i += 1
        elif tok.startswith("-"):
            return (f"git {redact_route_text(tok.split('=', 1)[0], secrets)} is not an allowed global option (before the verb a "
                    "route may pass only -C <a directory inside the scratch clone>)")
        else:
            sub = tok
            break
        i += 1
    if sub is None:
        return "git with no verb: a route names one read-only verb"
    spec = ROUTE_GIT_VERBS.get(sub)
    if spec is None:
        return (f"git {redact_route_text(sub, secrets)} is not an allowlisted read-only verb (a verb that can write, an online "
                "verb, an alias and an unlisted verb are never run)")
    return _route_git_options_reason(sub, spec, argv[i + 1:], here, scratch, secrets)


def _route_git_options_reason(sub: str, spec: RouteGitVerb, after: list[str], here: str, scratch: str,
                              secrets: Iterable[str] = ()) -> str | None:
    """Why the words after an allowlisted verb are refused, else None. Each option must be on the verb's own list, matched as an
    EXACT token (git reads any unambiguous prefix of a long option; the screen reads none, so `--onel` is refused); a long option
    with a value only as `--name=value`; a short cluster letter by letter, where a value letter takes the rest of the cluster or
    the next token; `--` ends the options. Then every option value and every positional (revision, path or pathspec) must
    resolve, `..`, symlinks and junctions followed and case folded, inside the scratch clone, and no positional may start with
    `:` (pathspec magic such as `:(top)` or `:/`, or an index path)."""
    def shown(tok: str) -> str:
        return redact_route_text(tok, secrets)

    if spec.first and (not after or after[0] not in spec.first):
        return f"git {sub} must start with one of {', '.join(sorted(spec.first))}: only its read modes are run"
    values: list[str] = []
    positionals: list[str] = []
    listing, ended, i = False, False, 0
    while i < len(after):
        tok = after[i]
        i += 1
        if ended or not tok.startswith("-"):
            positionals.append(tok)
        elif tok == "--":
            ended = True
        elif tok.startswith("--"):
            name, eq, value = tok.partition("=")
            if eq and name in spec.valued:
                values.append(value)
            elif not eq and tok in spec.flags:
                listing = listing or tok in spec.list_flags
            else:
                return (f"git {sub} {shown(name)}{'=...' if eq else ''} is not on the option list for git {sub} (exact tokens only; "
                        "an abbreviation or a form the list does not name is refused)")
        elif spec.digits and re.fullmatch(r"-\d+", tok):
            continue
        elif tok == "-":
            return f"git {sub} -: reading standard input is not on the option list"
        else:
            cluster = tok[1:]
            for pos, ch in enumerate(cluster):
                if ch in spec.value_letters:
                    if pos + 1 < len(cluster):
                        values.append(cluster[pos + 1:])
                    elif i < len(after):
                        values.append(after[i])
                        i += 1
                    else:
                        return f"git {sub} -{ch} needs a value"
                    break
                if ch not in spec.letters:
                    return (f"git {sub} {shown(tok)}: -{ch} is not on the option list for git {sub} (a short cluster is read "
                            "letter by letter)")
                listing = listing or ch in spec.list_letters
    if positionals and spec.positional == "none":
        return f"git {sub} takes no argument in a route"
    if positionals and spec.positional == "list" and not listing:
        return f"git {sub} with a name creates, renames or deletes; a route runs only its list form (--list/-l)"
    for tok in positionals:
        if tok.startswith(":"):
            return f"git {sub} {shown(tok)!r}: pathspec magic and index paths are refused"
    for tok in (*values, *positionals):
        if _route_git_operand_outside(tok, here, scratch):
            return f"git {sub} argument {shown(tok)!r} resolves outside the scratch clone"
    return None


def _route_git_operand_outside(tok: str, here: str, scratch: str) -> bool:
    """True when `tok`, read as a path relative to the directory git runs in (`here`), does not resolve inside the scratch clone
    (`scratch`, already real): `..`, symlinks, junctions and 8.3 names followed, case folded where the filesystem folds it. A
    token that cannot be read as a path at all counts as outside, fail-closed."""
    if "\0" in tok:
        return True
    try:
        return not route_inside(route_real_path(tok, here), scratch)
    except (OSError, ValueError):
        return True


def route_unsafe_reason(argv: list[str], cwd: str | os.PathLike | None = None, bus: str | os.PathLike | None = None,
                        secrets: Iterable[str] = ()) -> str | None:
    """Why a parsed route is refused, or None. Asked of EVERY route (it only reads the argv, so a route that is never run is
    screened too); the list is in the block comment above. A diagnostic never carries a credential's
    value (redact_route_text; `secrets` are the filled values bound to one, route_secret_values). `cwd` is the scratch clone
    (default: the current directory) and `bus` the live bus, when known."""
    if not argv:
        return "empty command"
    here = str(cwd) if cwd is not None else os.getcwd()
    for tok in argv:
        if ROUTE_SHELL_OPERATOR_RE.match(tok):
            return f"shell operator {redact_route_text(tok, secrets)!r}: a route is one command, no shell"
        if ROUTE_URL_RE.match(tok) or ROUTE_URL_RE.match(tok.split("=", 1)[-1]):
            return f"network target {redact_route_text(tok, secrets)!r}: a route must run offline"
        if ROUTE_CREDENTIAL_RE.search(tok) or ROUTE_CRED_ASSIGN_RE.match(tok):
            shown = f"{tok.split('=', 1)[0]}=<redacted>" if ROUTE_CRED_ASSIGN_RE.match(tok) else redact_route_text(tok, secrets)
            return f"credential {shown!r}: a route touches no credential"
    if ROUTE_ENV_PREFIX_RE.match(argv[0]):
        return "an environment assignment prefix is shell syntax; a route is one command, no shell"
    name = route_program_name(argv[0])
    rest = [t.lower() for t in argv[1:]]
    if name in ROUTE_NETWORK_CLIENTS:
        return f"{name} is a network client"
    if name == "gh":
        return "gh talks to GitHub"
    if name in ("claude", "codex") and any(t in ("auth", "login", "logout") for t in rest):
        return f"{name} auth is never run by a route"
    if name == "find" and any(t in ROUTE_FIND_RUNS for t in rest):
        return "find -exec/-delete runs or removes something else"
    if name == "git":
        reason = _route_git_unsafe(argv, here, secrets)
        if reason:
            return reason
    if bus is not None:
        return route_live_bus_reason(argv, here, str(bus), secrets)
    return None


def _route_flag_cluster(tok: str) -> str | None:
    """The letters of a single-dash option cluster (`-Ic` -> `Ic`), or None for a long option or a non-option."""
    return tok[1:] if len(tok) > 1 and tok[0] == "-" and tok[1] != "-" else None


def _route_python_script(rest: list[str]) -> str | None:
    """The script a python/py launcher names, past the options that take an operand (`-X utf8`, `-W ignore`,
    `--check-hash-based-pycs default`); None when it runs a module (`-m`) or inline code (`-c`) or names no script."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        cluster = _route_flag_cluster(tok)
        if cluster is None:
            if tok == "-":
                return None
            if tok.startswith("--"):
                i += 2 if tok == "--check-hash-based-pycs" else 1
                continue
            return tok
        for pos, ch in enumerate(cluster):
            if ch in "mc":
                return None
            if ch in "WX":                                        # takes the rest of the cluster, or the next token
                if pos == len(cluster) - 1:
                    i += 1
                break
        i += 1
    return None


def _route_shell_script(rest: list[str]) -> str | None:
    """bash/sh/zsh/...: the first operand past the options that take one (`-o pipefail`, `--rcfile f`); None for `-c` code."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        cluster = _route_flag_cluster(tok)
        if cluster is None:
            if tok == "-":
                return None
            if tok.startswith("--"):
                if tok.split("=", 1)[0] == "--command":
                    return None
                i += 1
                continue
            return tok
        if "c" in cluster:
            return None
        i += 1
    return None


def _route_node_script(rest: list[str]) -> str | None:
    """node: the first operand past the options that take one (`-r preload.js`, `--import x`); None for `-e`/`-p` code."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("-r", "--require", "--import", "--loader", "--experimental-loader", "--input-type", "--conditions", "-C"):
            i += 2
            continue
        cluster = _route_flag_cluster(tok)
        if cluster is None:
            if tok == "-":
                return None
            if tok.startswith("--"):
                if tok.split("=", 1)[0] in ("--eval", "--print"):
                    return None
                i += 1
                continue
            return tok
        if set(cluster) & {"e", "p"}:
            return None
        i += 1
    return None


def _route_generic_script(rest: list[str], inline: set[str]) -> str | None:
    """perl/ruby/php/lua/Rscript/osascript: the first operand that is not an option; None for inline code."""
    for tok in rest:
        if tok in inline:
            return None
        if tok.startswith("-") and tok != "-":
            continue
        return None if tok == "-" else tok
    return None


def _powershell_flag(tok: str) -> str:
    """The lowercase name of a PowerShell switch token (`-NoProfile`, `/f`, `--file`, `-f:x`), else ''. A `/`-led token
    with another separator after it (`/tmp/x.ps1`) is a path, not a switch."""
    if tok[:1] not in ("-", "/") or len(tok) < 2:
        return ""
    if tok[0] == "/" and re.search(r"[\\/]", tok[1:]):
        return ""
    return tok[1:].split(":", 1)[0].lower().lstrip("-")


def _powershell_is_file_flag(tok: str) -> bool:
    """True for every spelling PowerShell takes as -File: any prefix of it, either case, `-`/`/`/`--`."""
    flag = _powershell_flag(tok)
    return bool(flag) and "file".startswith(flag)


ROUTE_PWSH_OPERAND_FLAGS = frozenset({"executionpolicy", "ep", "workingdirectory", "wd", "inputformat", "in", "if", "outputformat", "o",
                                      "of", "windowstyle", "w", "configurationname", "config", "cfg", "settingsfile", "version", "v",
                                      "custompipename"})


def _route_powershell_script(rest: list[str]) -> str | None:
    """pwsh/powershell: the FIRST of a bare operand (pwsh 7 reads the first bare argument as the script, an implicit -File) and
    a `-File <script>` flag in any spelling; None for `-Command`/`-EncodedCommand` (inline code) and for no script at all."""
    i = 0
    while i < len(rest):
        tok = rest[i]
        flag = _powershell_flag(tok)
        if not flag:
            return tok
        if _powershell_is_file_flag(tok):
            if ":" in tok:
                return tok.split(":", 1)[1] or None
            return rest[i + 1] if i + 1 < len(rest) else None
        if "command".startswith(flag) or "encodedcommand".startswith(flag) or "commandwithargs".startswith(flag) or flag == "ec":
            return None
        i += 2 if flag in ROUTE_PWSH_OPERAND_FLAGS else 1
    return None


def _route_file_like(tok: str) -> bool:
    """True when a `bun run` operand names a file (a path, or a .js/.ts/.mjs/... name), not a package.json script (`build`)."""
    return bool(re.search(r"[\\/]", tok) or ROUTE_SCRIPT_EXT_RE.search(tok))


def _route_deno_script(rest: list[str]) -> str | None:
    """deno: the operand of `run|test|bench|check|compile|serve`, or the bare script (`deno x.ts`), past the options that take an
    operand (`-c deno.json`); None for `task`, `eval`, `fmt` and the other verbs that name no script."""
    i, verb = 0, None
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("-") and tok != "-":
            i += 2 if tok in ROUTE_DENO_OPERAND_FLAGS else 1
            continue
        if verb is None and tok in ROUTE_DENO_SCRIPT_VERBS:
            verb = tok
        elif verb is None and tok in ROUTE_DENO_OTHER_VERBS:
            return None
        else:
            return tok
        i += 1
    return None


def _route_bun_script(rest: list[str]) -> str | None:
    """bun: the file operand of `bun run <file>` or `bun <file>`, past the options that take an operand; None for inline code
    (-e/-p), a package.json script (`bun run build`) and the verbs that name no file (`install`, `test`, ...)."""
    i, verb = 0, None
    while i < len(rest):
        tok = rest[i]
        if tok in ROUTE_INLINE_INTERPRETERS["bun"]:
            return None
        if tok.startswith("-") and tok != "-":
            i += 2 if tok in ROUTE_BUN_OPERAND_FLAGS else 1
            continue
        if verb is None and tok == "run":
            verb = tok
        elif verb is None and tok in ROUTE_BUN_OTHER_VERBS:
            return None
        else:
            return tok if _route_file_like(tok) else None
        i += 1
    return None


def _route_flag_operands(rest: list[str], flags: frozenset[str], main: str | None) -> list[str]:
    """The operand of every option in `flags` (`-r x`, `--require=x`) placed before the `main` script: one after it belongs to the
    script, not to the program. A preload or rc file is code the program runs, so it is a named script too."""
    found, i = [], 0
    while i < len(rest):
        tok = rest[i]
        key = tok.split("=", 1)[0]
        if key in flags:
            if "=" in tok:
                found.append(tok.split("=", 1)[1])
                i += 1
            elif i + 1 < len(rest):
                found.append(rest[i + 1])
                i += 2
            else:
                i += 1
            continue
        if main is not None and tok == main:
            break
        i += 1
    return [f for f in found if f]


def _route_awk_files(rest: list[str]) -> list[str]:
    """awk/gawk/mawk/nawk: every program file named with `-f FILE`, `-fFILE` or `--file=FILE`; the program text itself is no file."""
    found, i = [], 0
    while i < len(rest):
        tok = rest[i]
        if tok in ("-f", "--file") and i + 1 < len(rest):
            found.append(rest[i + 1])
            i += 2
            continue
        if tok.startswith("--file="):
            found.append(tok.split("=", 1)[1])
        elif tok.startswith("-f") and len(tok) > 2 and not tok.startswith("--"):
            found.append(tok[2:])
        i += 1
    return [f for f in found if f]


def route_script_operands(argv: list[str], name: str | None = None) -> list[str]:
    """Every file a route names as code its program runs, in order, for the STATIC existence check only (nothing is launched on
    its strength): the script, plus node's -r/--import preloads, a shell's --rcfile, bun's --preload and awk's -f files. `name`
    is the program family to read the options as (default: the program's own name). Empty when the route names none (a module,
    inline code, a wrapper, a program that is no interpreter)."""
    name = name or route_program_name(argv[0])
    rest = argv[1:]
    if name in ROUTE_PYTHONS:
        found = [_route_python_script(rest)]
    elif name in ROUTE_SHELLS:
        main = _route_shell_script(rest)
        found = [*_route_flag_operands(rest, ROUTE_PRELOAD_FLAGS["shell"], main), main]
    elif name in ("node", "nodejs"):
        main = _route_node_script(rest)
        found = [*_route_flag_operands(rest, ROUTE_PRELOAD_FLAGS["node"], main), main]
    elif name == "deno":
        found = [_route_deno_script(rest)]
    elif name == "bun":
        main = _route_bun_script(rest)
        found = [*_route_flag_operands(rest, ROUTE_PRELOAD_FLAGS["bun"], main), main]
    elif name in ROUTE_POWERSHELLS:
        found = [_route_powershell_script(rest)]
    elif name in ROUTE_AWKS:
        found = _route_awk_files(rest)
    elif name == "tclsh":
        found = [_route_generic_script(rest, set())]
    elif name in ROUTE_INLINE_INTERPRETERS:
        found = [_route_generic_script(rest, ROUTE_INLINE_INTERPRETERS[name])]
    else:
        found = []
    return [f for f in found if f]


def route_resolve_program(program: str, cwd: pathlib.Path) -> str | None:
    """The file a route's program names: a path (relative to the scratch clone) when it holds a separator, else the first
    match in the ABSOLUTE entries of PATH (route_find_program). The current directory is never searched, and neither is a
    relative or quoted PATH entry: shutil.which would, on Windows, so a program planted in the clone would resolve. None when
    there is none."""
    if "/" in program or "\\" in program:
        local = pathlib.Path(program)
        if not local.is_absolute():
            local = cwd / local
        if local.is_file():
            return str(local)
        return next((str(spelled) for spelled in (pathlib.Path(str(local) + e) for e in route_pathext()) if spelled.is_file()), None)
    return route_find_program(route_absolute_entries(os.environ.get("PATH")), program)


def _route_script_problem(script: str, cwd: pathlib.Path) -> tuple[str, str] | None:
    """(typed reason, text) when `script` is not a file inside the scratch clone `cwd`: missing, or resolving outside it (an
    absolute path, `..` or an outward symlink or junction). None when it is."""
    try:
        real = (cwd / script).resolve(strict=False)                     # follows a symlink; an absolute `script` replaces cwd
        inside = real.is_relative_to(cwd.resolve())                     # containment by path, never a string prefix
        is_file = real.is_file()
    except (OSError, ValueError):
        return "SCRIPT_NOT_FOUND", f"{script}: no such script in the scratch clone"
    if not inside:
        return "SCRIPT_OUTSIDE_CLONE", f"{script}: resolves outside the scratch clone"
    if not is_file:
        return "SCRIPT_NOT_FOUND", f"{script}: no such script in the scratch clone"
    return None


def route_missing_reason(argv: list[str], cwd: pathlib.Path, will_run: bool = False,
                         pending: frozenset[str] = frozenset()) -> tuple[str, str] | None:
    """The STATIC existence check; it runs nothing. (typed reason, text) when the route's program does not resolve, resolves
    to a `.cmd`/`.bat` shim (only a route that will be RUN needs to start), or any script it names (route_script_operands) is
    not a file inside the scratch clone. The program is read under its own name AND under the name of the file it resolves to,
    so a link or alias of an interpreter has its script checked too. `pending` holds the tokens that still carry an unfilled
    `<placeholder>`: the program or script operand among them cannot be looked up and is skipped. None when all exist."""
    program = argv[0]
    if program in pending:
        return None
    found = route_resolve_program(program, cwd)
    if found is None:
        return "PROGRAM_NOT_FOUND", f"{program}: no such program on PATH or in the scratch clone"
    if will_run and os.name == "nt" and found.lower().endswith((".cmd", ".bat")):
        return "PROGRAM_IS_SHIM", (f"{program}: resolves to a {pathlib.PurePath(found).suffix.lower()} shim ({found}); "
                                   "a shim is not exercisable without cmd /c, which a route may not use")
    names = [route_program_name(program), route_program_name(os.path.realpath(found))]
    checked: set[str] = set()
    for name in dict.fromkeys(names):
        for script in route_script_operands(argv, name):
            if script in pending or script in checked:
                continue
            checked.add(script)
            problem = _route_script_problem(script, cwd)
            if problem:
                return problem
    return None


def split_route(command: str) -> list[str]:
    """A route line as argv, without a shell. POSIX quoting; on Windows non-POSIX shlex, so a backslash path
    (`tools\\x.py`) stays whole, with the quotes then removed from each token."""
    if os.name != "nt":
        return shlex.split(command)
    return [re.sub(r'"([^"]*)"|\'([^\']*)\'', lambda m: m.group(1) if m.group(1) is not None else m.group(2), tok)
            for tok in shlex.split(command, posix=False)]


def route_exercise_env(base: dict[str, str] | None = None, clone: str | os.PathLike | None = None) -> dict[str, str]:
    """The environment a route that IS run gets: `base` (default: this process's) without the interpreter-injection variables
    (ROUTE_INJECTION_ENV, any case: a startup hook or preload a launched interpreter would run), the generic helper variables
    (ROUTE_HELPER_ENV: EDITOR, VISUAL, PAGER, SSH_ASKPASS), any credential-shaped variable and every GIT_* variable (GIT_EDITOR,
    GIT_SEQUENCE_EDITOR, GIT_PAGER, GIT_ASKPASS, GIT_SSH[_COMMAND], GIT_EXTERNAL_DIFF, GIT_CONFIG_PARAMETERS, ...), then git
    told to prompt for nothing and read no system config or system attributes (GIT_CONFIG_NOSYSTEM=1, GIT_ATTR_NOSYSTEM=1) and, as
    its global config, only an EMPTY file beside the clone (route_git_global_config; os.devnull when no clone is given), so no
    driver, filter, alias or helper defined in ~/.gitconfig, the XDG config or the system config is reachable. PATH (any case of the key) keeps only its ABSOLUTE entries
    (route_absolute_entries) and, when `clone` is given, only those whose real path is outside the clone: a `false.exe` or `gpg`
    planted in the clone is never found through `.`, an empty entry, a relative one, a quoted one or a junction into it."""
    source = os.environ if base is None else base
    env = {k: v for k, v in source.items()
           if k.upper() not in ROUTE_INJECTION_ENV and k.upper() not in ROUTE_HELPER_ENV and not ROUTE_STRIPPED_ENV_RE.search(k)}
    scratch = route_real_path(str(clone)) if clone is not None else None
    for key in [k for k in env if k.upper() == "PATH"]:
        kept = []
        for entry in route_absolute_entries(env[key]):
            try:
                if scratch is None or not route_inside(route_real_path(entry), scratch):
                    kept.append(entry)
            except (OSError, ValueError):
                continue
        env[key] = os.pathsep.join(kept)
    global_config = route_git_global_config(clone) if clone is not None else os.devnull
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_ATTR_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": global_config})
    return env


def run_isolated_git(git_bin: str, args: list[str], cwd: str | os.PathLike, clone: str | os.PathLike,
                     timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Run `git <args>` in `cwd` exactly as a route's git is run: the command line of route_git_command, the environment of
    route_exercise_env (both keyed to the scratch `clone`, whose parent directory holds the empty hooks directory and the empty
    global config) and the launcher of a route (run_bounded_process: output kept to its tail, the whole process tree killed on
    timeout). The scratch clone is made and the bus read through this one function, so preparation and routes cannot drift.
    A git still running after `timeout` (default ROUTE_PREP_TIMEOUT) is the route path's outcome, Unknown ROUTE_TIMEOUT."""
    limit = ROUTE_PREP_TIMEOUT if timeout is None else timeout
    returncode, out, err = run_bounded_process(route_git_command(git_bin, ["git", *args], clone), pathlib.Path(cwd),
                                               route_exercise_env(clone=clone), limit)
    if returncode is None:
        verb = next((a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] not in ("-c", "-C"))), "")
        raise Unknown("ROUTE_TIMEOUT", f"git {verb} still running after {limit:g}s; its process tree was killed")
    return subprocess.CompletedProcess(["git", *args], returncode, out, err)


def route_git_global_config(clone: str | os.PathLike) -> str:
    """The file a run git reads as its global config: ROUTE_EMPTY_GLOBAL_CONFIG beside the clone, in the scratch directory this
    tool made, emptied on every call so nothing a previous route left there is read."""
    path = pathlib.Path(clone).parent / ROUTE_EMPTY_GLOBAL_CONFIG
    path.write_bytes(b"")
    return str(path)


class _WinJob:
    """A Windows Job Object that kills every process in it when closed (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE), so a
    timeout reaches the route's grandchildren, not just its direct child."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

        class IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("Read", "Write", "Other", "ReadBytes", "WriteBytes", "OtherBytes")]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._k32 = k32
        self._handle = k32.CreateJobObjectW(None, None)
        if not self._handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = Extended()
        info.Basic.LimitFlags = 0x2000                                  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        process = None
        try:
            if not k32.SetInformationJobObject(self._handle, 9, ctypes.byref(info), ctypes.sizeof(info)):  # JobObjectExtendedLimitInformation
                raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            process = k32.OpenProcess(0x0100 | 0x0001, False, pid)      # PROCESS_SET_QUOTA | PROCESS_TERMINATE
            if not process or not k32.AssignProcessToJobObject(self._handle, process):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        except OSError:
            self.close()
            raise
        finally:
            if process:
                k32.CloseHandle(process)

    def close(self) -> None:
        """Close the job; Windows kills whatever is still in it."""
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


def _kill_route_tree(proc: subprocess.Popen, job: "_WinJob | None") -> None:
    """End the route's process and every descendant it started, then reap the direct child."""
    if os.name == "nt":
        if job is not None:
            job.close()
        else:
            try:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
            except (OSError, subprocess.SubprocessError):
                pass
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _drain_tail(stream, cap: int, sink: list) -> None:
    """Read `stream` to EOF keeping only its last `cap` bytes, so a noisy route cannot grow memory without bound."""
    tail = bytearray()
    try:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            tail += chunk
            if len(tail) > cap:
                del tail[:len(tail) - cap]
    except (OSError, ValueError):
        pass
    sink.append(bytes(tail))


def run_route_process(argv: list[str], cwd: pathlib.Path, env: dict[str, str], timeout: float) -> tuple[int | None, str, str]:
    """Run one route to completion or `timeout`: (returncode, stdout tail, stderr tail); returncode None on timeout.
    The ONLY place a route is launched (run_bounded_process, which preparation shares)."""
    return run_bounded_process(argv, cwd, env, timeout)


def run_bounded_process(argv: list[str], cwd: pathlib.Path, env: dict[str, str], timeout: float) -> tuple[int | None, str, str]:
    """Run `argv` to completion or `timeout`: (returncode, stdout tail, stderr tail); returncode None on timeout. The one bounded
    launcher: a route and the scratch preparation (run_isolated_git) both run through it. On timeout the whole process tree is
    killed (a Job Object on Windows, a process group on POSIX), so no descendant outlives the call or keeps a pipe open. Only the
    last ROUTE_OUTPUT_CAP_BYTES of each stream are kept."""
    extra = {"start_new_session": True} if os.name != "nt" else {}
    proc = subprocess.Popen(argv, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=env, bufsize=0, **extra)
    job = None
    if os.name == "nt":
        try:
            job = _WinJob(proc.pid)
        except OSError:
            job = None                                                  # falls back to taskkill /T on timeout
    sinks: tuple[list, list] = ([], [])
    readers = [threading.Thread(target=_drain_tail, args=(stream, ROUTE_OUTPUT_CAP_BYTES, sink), daemon=True)
               for stream, sink in ((proc.stdout, sinks[0]), (proc.stderr, sinks[1]))]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    timed_out = False
    try:
        proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        for reader in readers:                                          # a descendant holding a pipe open counts against the same deadline
            reader.join(max(0.0, deadline - time.monotonic()))
        timed_out = any(reader.is_alive() for reader in readers)
    except subprocess.TimeoutExpired:
        timed_out = True
    finally:
        if timed_out:
            _kill_route_tree(proc, job)
        elif job is not None:
            job.close()                                                 # a clean run leaves nothing of its own running
    for reader in readers:
        reader.join(5)
    out, err = ([s[0].decode("utf-8", "replace") if s else "" for s in sinks])
    return (None if timed_out else proc.returncode), out, err


def exercise_route(command: str, cwd: pathlib.Path, timeout: float, fills: dict[str, str],
                   bus: pathlib.Path | None = None) -> tuple[str, str]:
    """Check one route command against `cwd` (the scratch clone); return (ROUTE_* status, detail). Never raises for a route's
    own failure. `bus` is the live bus: a route that would be run with its cwd or any argument inside it is ROUTE_UNSAFE.
    No detail carries a credential's value."""
    try:
        argv = split_route(command)
    except ValueError as e:
        return "ROUTE_FAILED", f"does not parse as a command line: {e}"
    secrets = route_secret_values(argv, fills)                          # decided on the template, before any value is substituted
    status, detail = _exercise_route_argv(argv, cwd, timeout, fills, bus, secrets)
    return status, redact_secret_values(detail, secrets)                # the one choke point: no branch below can print a credential


def _exercise_route_argv(argv: list[str], cwd: pathlib.Path, timeout: float, fills: dict[str, str],
                         bus: pathlib.Path | None, secrets: list[str]) -> tuple[str, str]:
    """exercise_route past the parse; its detail is redacted once by the caller (redact_secret_values)."""
    # Judged on the template: a filled value is never a placeholder, so a value shaped like `<x>` is not read as one.
    unfilled = list(dict.fromkeys(m.group(1) for tok in argv for m in ROUTE_PLACEHOLDER_RE.finditer(tok) if m.group(1) not in fills))
    # Fill after splitting, so a filled value (a Windows path with backslashes) is never re-parsed as shell text.
    filled = [ROUTE_PLACEHOLDER_RE.sub(lambda m: fills.get(m.group(1), m.group(0)), tok) for tok in argv]
    pending = frozenset(new for old, new in zip(argv, filled)
                        if any(m.group(1) not in fills for m in ROUTE_PLACEHOLDER_RE.finditer(old)))
    # The screen reads an unfilled `<name>` as a plain word (`<bus-sha>` is no redirect), so it still judges the rest of the token.
    screened = [ROUTE_PLACEHOLDER_RE.sub(lambda m: fills.get(m.group(1), "UNFILLED"), tok) for tok in argv]
    argv = filled
    if not argv:
        return "ROUTE_FAILED", "empty command"
    runs = route_runs(argv)                                             # the ONE decision whether anything is launched
    if unfilled and runs:                                               # a route that is not run needs no value for its placeholder
        return "ROUTE_FAILED", f"unfilled placeholder <{unfilled[0]}>; pass --fill {unfilled[0]}=VALUE"
    unsafe = route_unsafe_reason(screened, cwd, bus, secrets)           # the safety screen reads every route and runs nothing
    if unsafe:
        return "ROUTE_UNSAFE", unsafe
    if not runs:
        missing = route_missing_reason(argv, cwd, False, pending)       # the static check: nothing runs
        if missing:
            return "ROUTE_MISSING", f"{missing[0]}: {missing[1]}"
        noted = f"; unfilled {', '.join(f'<{n}>' for n in unfilled)} (not run, so none is needed)" if unfilled else ""
        return "ROUTE_NOT_EXECUTED", (f"{route_program_name(argv[0])} is not on the execution allowlist ({', '.join(sorted(ROUTE_EXEC_ALLOW))}): "
                                      "its program and scripts exist and it is never run (widening it is "
                                      f"CARD-ROUTE-EXERCISE-LAUNCHER-ALLOWLIST-1){noted}")
    git_bin, refused = route_git_binary(cwd)                            # the executable decides too, never the name alone
    if refused:
        return refused
    try:
        returncode, out, err = run_route_process(route_git_command(str(git_bin), argv, cwd), cwd, route_exercise_env(clone=cwd), timeout)
    except FileNotFoundError as e:
        return "ROUTE_MISSING", f"PROGRAM_NOT_FOUND: {redact_route_text(str(e), secrets)}"
    except OSError as e:
        return "ROUTE_FAILED", f"could not start: {redact_route_text(str(e), secrets)}"
    if returncode is None:
        return "ROUTE_TIMEOUT", f"still running after {timeout:g}s; its process tree was killed"
    if returncode != 0:
        text = redact_route_text(err.strip() or out.strip(), secrets)      # whole output first: a value may span lines
        shown = (text.splitlines()[-1:] or [""])[0]
        return "ROUTE_FAILED", f"exit {returncode}: {shown[:200]}"
    return "ROUTE_OK", ""


def _writable_retry(func, path, _exc) -> None:
    """rmtree error hook: git object files are read-only on Windows; make the entry writable and retry once."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_exercise_scratch(base: pathlib.Path, scratch_root: pathlib.Path) -> None:
    """Remove the scratch directory THIS run made, by name: it must sit directly under `scratch_root`, carry the
    run's prefix and be a real directory (not a link). Anything else is left alone."""
    if (base.parent.resolve() != scratch_root.resolve() or not base.name.startswith(ROUTE_SCRATCH_PREFIX)
            or base.is_symlink() or not base.is_dir()):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(base, onexc=_writable_retry)
    else:
        shutil.rmtree(base, onerror=lambda f, p, e: _writable_retry(f, p, e))


def make_exercise_scratch(bus: pathlib.Path, scratch_root: pathlib.Path) -> tuple[pathlib.Path, pathlib.Path, str]:
    """A scratch clone of the bus's committed tip: (scratch dir, clone, tip sha). The bus is read, never written,
    and the scratch root may not be inside it. `origin` is removed so nothing in the clone can push back. Every git call here runs
    under the isolation a route gets (run_isolated_git), so no filter, hook or helper the host's config defines starts while the
    bus is cloned and checked out, before any route is screened."""
    if not (bus / ".git").exists() and not (bus / "HEAD").is_file():
        raise Unknown("EXERCISE_BUS_NOT_A_REPO", f"{bus} is not a git repository")
    if route_inside(route_real_path(str(scratch_root)), route_real_path(str(bus))):
        raise Unknown("EXERCISE_SCRATCH_INSIDE_BUS", f"scratch root {scratch_root} is inside the bus {bus}; routes never run in the live bus")
    scratch_root.mkdir(parents=True, exist_ok=True)
    base = pathlib.Path(tempfile.mkdtemp(prefix=ROUTE_SCRATCH_PREFIX, dir=str(scratch_root)))
    try:
        clone = base / "bus"
        git_bin, refused = route_git_binary(clone)
        if refused:
            raise Unknown("EXERCISE_GIT_UNAVAILABLE", refused[1])

        def isolated(cwd: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
            return run_isolated_git(git_bin, ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false", *args], cwd, clone)

        proc = isolated(base, "clone", "--local", "--no-hardlinks", "-q", str(bus), str(clone))
        if proc.returncode != 0:
            raise Unknown("EXERCISE_CLONE_FAILED", proc.stderr.strip()[:300])
        for args in (("remote", "remove", "origin"), ("rev-parse", "HEAD")):
            proc = isolated(clone, *args)
            if proc.returncode != 0:
                raise GitError(f"git {' '.join(args)} (in {clone}): {proc.stderr.strip()}")
        return base, clone, proc.stdout.strip()
    except BaseException:
        remove_exercise_scratch(base, scratch_root)
        raise


def run_exercise(entries: list[tuple[str, dict]], bus: pathlib.Path | None, timeout: float,
                 fills: dict[str, str], scratch_root: pathlib.Path | None) -> int:
    """Check the routes of already-validated items (name, item); 0 when every route is ROUTE_OK or ROUTE_NOT_EXECUTED."""
    plan = [(name, command) for name, item in entries for command in extract_routes(item["body"])]
    for name, item in entries:
        if not extract_routes(item["body"]):
            _route_say(f"ROUTE_NONE {name} (names no route command)")
    if not plan:
        return 0
    if bus is None:
        env_bus = os.environ.get(ROUTE_BUS_ENV)
        if not env_bus:
            raise Unknown("EXERCISE_BUS_UNSET", f"pass --bus <bus clone> or set ${ROUTE_BUS_ENV}")
        bus = pathlib.Path(env_bus)
    root = scratch_root if scratch_root is not None else pathlib.Path(tempfile.gettempdir())
    bus = bus.resolve()
    base, clone, tip = make_exercise_scratch(bus, root)
    ok = True
    try:
        _route_say(f"ROUTE_BUS {tip[:12]} (scratch clone of {bus.name}; commands run there, never in the bus)")
        git_bin, refused = route_git_binary(clone)
        behind = 0 if refused else bus_commits_behind_origin(bus, str(git_bin), clone)
        if behind:
            _route_say(f"ROUTE_BUS_STALE {behind} (the bus clone is {behind} commit(s) behind its origin as of its last fetch; "
                       f"git pull --ff-only there, or a script removed upstream still reads ROUTE_OK)", sys.stderr)
        fills = {"scratch": str(clone), **fills}                       # `<scratch>` names the scratch clone
        for name, command in plan:
            status, detail = exercise_route(command, clone, timeout, fills, bus)
            _route_say(f"{status} {name} {redact_route_text(command)}")
            if status != "ROUTE_OK":
                _route_say(f"  {detail}", sys.stderr)
            if status not in ROUTE_PASSING:
                ok = False
    finally:
        remove_exercise_scratch(base, root)
    return 0 if ok else 1


def _route_say(line: str, stream=None) -> None:
    """Print one line that cannot raise on a stream whose encoding lacks a character of it (a cp1252 pipe)."""
    stream = stream or sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    print(line.encode(encoding, errors="replace").decode(encoding), file=stream)


def bus_commits_behind_origin(bus: pathlib.Path, git_bin: str, clone: str | os.PathLike) -> int:
    """How many commits the bus clone's HEAD is behind its own origin/HEAD, origin/master or origin/main (the newest
    of what its last fetch saw); 0 when none of those refs exists or the count cannot be read. Read through run_isolated_git
    (`git_bin` and the scratch `clone` of the run), the isolation every other git call of the exercise gets: no host GIT_*
    variable, config, hook or fsmonitor can change the verdict, and `--no-optional-locks` keeps the live bus untouched."""
    for ref in ("refs/remotes/origin/HEAD", "refs/remotes/origin/master", "refs/remotes/origin/main"):
        try:
            where = ["--no-optional-locks", "-C", str(bus)]
            if run_isolated_git(git_bin, [*where, "rev-parse", "--verify", "--quiet", ref], pathlib.Path(clone).parent, clone).returncode != 0:
                continue
            counted = run_isolated_git(git_bin, [*where, "rev-list", "--count", f"HEAD..{ref}"], pathlib.Path(clone).parent, clone)
        except (OSError, subprocess.SubprocessError, Unknown):
            return 0
        if counted.returncode == 0 and counted.stdout.strip().isdigit():
            return int(counted.stdout.strip())
    return 0


# ---------- ruling candidates must name what they touch or supersede ----------
#
# CoS work order FWO-DEFECT-PREVENT-1 item 4 (seed: #469 r1, where the candidate quietly contradicted RULINGS.md:1126-1152,
# R15 and an open candidate and the reviewer, not the producer, found it). `validate` refuses a ruling candidate that
# carries no `touches/supersedes:` declaration, names a target that is not shaped like one, or names one the bus clone
# it reads does not hold. The bus is only READ, at its committed tip (the commit HEAD resolves to, once per item); a bus the validator
# cannot read is UNKNOWN, and so is one whose object cannot be read after that (a read failure is not a missing target).

# Whole words: the right boundary leaves out `ruling-candidates/`, `ruling-candidate-<slug>` and the plural. Zero spaces or
# dashes may sit between the words, because deleting an invisible character (U+200B, a soft hyphen) joins them.
RULING_CANDIDATE_PHRASE = r"ruling[ \t-]*candidate(?![-\w/])"
RULING_QUALIFIER = r"(?:(?:proposed|draft|new)[ \t]+)?"
RULING_CANDIDATE_LEAD_RE = re.compile(RULING_QUALIFIER + RULING_CANDIDATE_PHRASE, re.I)
RULING_CANDIDATE_AFTER_SEP_RE = re.compile(r"[-:][ \t]*" + RULING_QUALIFIER + RULING_CANDIDATE_PHRASE, re.I)
RULING_CANDIDATE_COLON_RE = re.compile(r"(?<![\w/-])ruling[ \t-]*candidate[ \t]*:", re.I)
# `Ruling candidate `ruling-candidates/x.md` remains PROPOSED`: the phrase, a space, a candidate's path (no colon), a space
# and a STATUS word (remains, stays, is, was, ...). That is a statement about a candidate, not a marker (bus
# cos-feedback/_bus/pr-69.md:17); a colon, a dash, a parenthesis, nothing, or any other word after the phrase or the path
# keeps it a marker, and so does a colon after the status word (`is: every lane must X` states a rule). A verb of its own
# (`... proposes that X`) is refused by the verb. An item's own path (its outbox file's slug, `own_candidate_slug`) is no
# citation: an item that cites itself proposes itself.
RULING_CANDIDATE_CITES_RE = re.compile(r"[ \t]+ruling-candidates/(?P<path>[^\s:]*)[ \t]+(?i:remains?|stays?|stayed|is|are|was|were|has|have|had|becomes?|became)\b(?![ \t]*:)")
RULING_CANDIDATE_REVISION_RE = re.compile(r"-r\d+$")


def own_candidate_slug(item_name: str) -> str | None:
    """The slug an outbox file `YYYYMMDD-<slug>.md` files its ruling candidate under (`ruling-candidates/<slug>[-rN].md`),
    or None when the name is not an item name."""
    return item_name[9:-3].lower() if ITEM_NAME_RE.match(item_name) else None


def cites_another_candidate(text: str, pos: int, own: str | None = None) -> bool:
    """True when `text` at `pos` is a citation: a `ruling-candidates/` path (no colon), a status word, and no colon after
    the status word (`is: every lane must X` states a rule). The path must not be the item's own (`own`, from
    `own_candidate_slug`): an item that cites itself proposes itself."""
    found = RULING_CANDIDATE_CITES_RE.match(text, pos)
    if not found:
        return False
    if own:
        path = found.group("path").lower()
        stem = path.split(".md", 1)[0] if ".md" in path else path.rstrip("`\"')]>*_,.;")
        if stem == own or RULING_CANDIDATE_REVISION_RE.sub("", stem) == own:
            return False
    return True
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^<>]*>")
# Markup that may sit in front of a marker, in any order: spaces, `>`, `#`s, a list bullet, emphasis, quotes, brackets.
MARKER_FRONT_RE = re.compile(r"[ \t]+|(?P<quote>>)|(?P<atx>#{1,6})(?=[ \t]|$)|(?P<bullet>(?:[-+*]|\d+[.)])(?=[ \t]|$))"
                             "|[*_`\"'“”‘’«»‹›\\[(]")
MARKER_UNDERLINE_RE = re.compile(r"=+|-+")   # a setext underline, once the line is stripped
RULING_TARGET_VERB_RE = re.compile(r"(?:extends|supersedes|distinguishes|touches|narrows|adopts)[ \t]+(?=\S)", re.I)
TOUCHES_LABEL_RE = re.compile(r"^([ \t]*)([-*][ \t]+)?(?:\*\*)?(?:touches/supersedes|touches|supersedes|relationship[ \t]+to[ \t]+existing[ \t]+doctrine)(?:\*\*)?:(?:\*\*)?[ \t]*(.*?)[ \t]*$", re.I)
TOUCHES_BULLET_RE = re.compile(r"^([ \t]*)[-*][ \t]+(\S.*?)[ \t]*$")
TOUCHES_NONE_RE = re.compile(r"^none\b[ \t]*[:\-–—]?[ \t]*(.*)$", re.I)
RULING_RULE_ID_RE = re.compile(r"^R\d+(?:\.\d+)*$")
RULING_PATH_RE = re.compile(r"^([A-Za-z0-9_][\w.@+-]*(?:/[\w.@+-]+)*)/?(?::\d+(?:-\d+)?|#[\w.-]+)?$")
RULING_TARGET_NOTE_RE = re.compile(r"[ \t]+\(.*\)$")
RULING_BUS_ENV = ROUTE_BUS_ENV   # one bus clone for the ruling-candidate check and the route exercise
RULINGS_FILE = "RULINGS.md"


def normalise_marker_line(line: str) -> str:
    """One line as the marker test reads it: Unicode NFKD with the combining marks (category Mn) dropped, then NFKC
    (full-width forms and no-break spaces become their ASCII twins), every format character (zero-width space or
    joiner, BOM, soft hyphen) deleted, every space separator a plain space, every dash (en, em, minus, non-breaking
    hyphen) a `-`."""
    out = []
    decomposed = "".join(ch for ch in unicodedata.normalize("NFKD", line.rstrip("\r\n")) if unicodedata.category(ch) != "Mn")
    for ch in unicodedata.normalize("NFKC", decomposed):
        category = unicodedata.category(ch)
        if category == "Cf":
            continue
        out.append(" " if category == "Zs" else "-" if category == "Pd" or ch == "−" else ch)
    return "".join(out)


def marker_line(line: str) -> tuple[str, int, bool, bool]:
    """(text, quote depth, was a list item, was an ATX heading) for one line: its markup (`>`, `#`s, a bullet,
    emphasis, quotes, brackets) comes off the front in any order, and `*`, `_` and backticks come out of the rest,
    so the words a marker opens with are the first thing left."""
    text, depth, bullet, heading = normalise_marker_line(line), 0, False, False
    while True:
        found = MARKER_FRONT_RE.match(text)
        if not found:
            return re.sub(r"[*_`]", "", text).strip(), depth, bullet, heading
        depth += bool(found.group("quote"))
        bullet = bullet or bool(found.group("bullet"))
        heading = heading or bool(found.group("atx"))
        text = text[found.end():]


def render_marker_lines(body: str) -> list[tuple[str, bool]]:
    """The body as the marker test reads it, as (line, opens a block) pairs: HTML entities decoded (`&nbsp;`, `&#160;`),
    HTML tags dropped with their text kept (`<strong>Ruling candidate:</strong>`), and a line with a `|` split into its
    cells, each its own block-opening line (a leading pipe or a table delimiter row leaves no cell of its own)."""
    rendered: list[tuple[str, bool]] = []
    for raw in body.splitlines():
        line = HTML_TAG_RE.sub("", html.unescape(raw.rstrip("\r")))
        if "|" in line:
            rendered += [(cell, True) for cell in [c for c in line.split("|") if c.strip()] or [""]]
        else:
            rendered.append((line, False))
    return rendered


def is_ruling_candidate(item: dict, own: str | None = None) -> bool:
    """A ruling candidate is an item with a marker line. A line can be one only where a block OPENS: the item's first
    line, a line after a blank one, a line after a heading (ATX or setext underline), a thematic break or a fence
    line (opening or closing), a heading, a list item, a table cell, a line that starts a deeper blockquote, a setext
    heading (a line over `===` or `---`), or any line in or at a fence (when in doubt it is a candidate; a fence
    closes on the same character, at least as long). A continuation line of a wrapped paragraph never is, so prose
    that wraps onto `ruling-candidates/x.md` or `ruling candidate titled ...` is left alone. The body is rendered
    first (`render_marker_lines`). On such a line, once normalised (`normalise_marker_line`) and stripped of markup
    (`marker_line`), the marker is `Ruling candidate` as whole words, any case, a space or dash between them, with
    an optional `Proposed`, `Draft` or `New` before it, whatever follows (`:`, ` (r2):`, ` - `, the end of the line),
    except a citation: the phrase, a `ruling-candidates/` path with no colon, and a status word (remains, stays, is,
    was, ...: `Ruling candidate `ruling-candidates/x.md` remains PROPOSED`; any other word, `... proposes that X`, a colon after the status word, or the item's own path (`own`), is a marker). A heading-class line (ATX, setext, the item's first line) also counts when the phrase follows a `-` or `:`
    (`### proj, 2026-10-16 - RULING CANDIDATE (r2): X`) or is followed by a colon. A line that only mentions the
    phrase, or a heading that mentions someone else's candidate (`... for ruling candidate <id>`, no colon), is
    not a marker. A `kind: ruling` item is a ratified ruling (`ratified_by` is required for it), not a candidate."""
    lines = render_marker_lines(item["body"])
    fence: tuple[str, int] | None = None
    seen_text, prev_blank, prev_depth, prev_closes = False, True, 0, False
    for i, (line, forced) in enumerate(lines):
        text, depth, bullet, heading = marker_line(line)
        unquoted = line.lstrip(" \t>")
        fence_line = _FENCE_OPEN_RE.match(unquoted)
        at_fence = False
        if fence is not None:
            if fence_line and fence_line.group(1)[0] == fence[0] and len(fence_line.group(1)) >= fence[1] and not unquoted.strip(" `~"):
                fence, at_fence = None, True
        elif fence_line:
            fence, at_fence = (fence_line.group(1)[0], len(fence_line.group(1))), True
        setext = (bool(text) and not MARKER_UNDERLINE_RE.fullmatch(text) and i + 1 < len(lines)
                  and bool(MARKER_UNDERLINE_RE.fullmatch(marker_line(lines[i + 1][0])[0])))
        first = bool(text) and not seen_text
        seen_text = seen_text or bool(text)
        opens = (prev_blank or prev_closes or forced or bullet or heading or setext or fence is not None or at_fence
                 or depth > prev_depth)
        if text and opens:
            lead = RULING_CANDIDATE_LEAD_RE.match(text)
            if lead and not cites_another_candidate(text, lead.end(), own):
                return True
            if (heading or setext or first) and (RULING_CANDIDATE_AFTER_SEP_RE.search(text) or RULING_CANDIDATE_COLON_RE.search(text)):
                return True
        # the next line opens a block when this one ended one: an ATX heading, a setext underline or thematic break, a fence line
        prev_blank, prev_depth = not text, depth
        prev_closes = heading or at_fence or bool(MARKER_UNDERLINE_RE.fullmatch(text))
    return False


def touches_supersedes_entries(body: str) -> list[str] | None:
    """The entries of a candidate's `touches/supersedes:` declaration, or None when no label is present. The label
    is `touches/supersedes:`, `touches:`, `supersedes:` or `Relationship to existing doctrine:` (the bus candidate
    template's own field), may be a bullet and bold, and may repeat; entries add up.
    Entries are the comma- or semicolon-separated names after a label on the same line, plus the bullet lines
    indented under it (or, when the label is not a bullet, the bullets right below it) up to the first blank line or
    non-bullet. A value that opens with `none` is one entry, whole, so its reason keeps its commas. A label inside
    a fenced block does not count."""
    lines = body.splitlines()
    fence: tuple[str, int] | None = None
    entries: list[str] | None = None
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r")
        opened = _FENCE_OPEN_RE.match(line)
        if fence is not None:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= fence[1] and not line.strip(" `~"):
                fence = None
            continue
        if opened:
            fence = (opened.group(1)[0], len(opened.group(1)))
            continue
        found = TOUCHES_LABEL_RE.match(line)
        if not found:
            continue
        indent, bullet, inline = len(found.group(1).expandtabs(4)), found.group(2), found.group(3).replace("**", "").strip()
        entries = [] if entries is None else entries
        if inline:
            entries += [inline] if TOUCHES_NONE_RE.match(inline) else [e.strip(" `") for e in re.split(r"[;,]", inline) if e.strip(" `")]
        for follow in lines[i + 1:]:
            sub = TOUCHES_BULLET_RE.match(follow.rstrip("\r"))
            if not sub:
                break
            depth = len(sub.group(1).expandtabs(4))
            if bullet is not None and depth <= indent:
                break
            entries.append(sub.group(2).replace("**", "").strip(" `"))
    return entries


def _ruling_target(entry: str) -> tuple[str, str] | None:
    """(kind, name) for one entry, or None when it is not shaped like a target. `rule` is an id like `R15` or `R14.8`;
    `path` is a bus-relative file or directory, with an optional `:<line>[-<line>]` or `#<anchor>` that is not
    checked. A leading verb (`extends`, `supersedes`, `distinguishes`, `touches`, `narrows`, `adopts`) and a
    trailing parenthetical note are dropped. A bare word, an absolute or `..` path and a backslash path are not
    targets."""
    target = RULING_TARGET_NOTE_RE.sub("", RULING_TARGET_VERB_RE.sub("", entry, count=1)).strip(" `")
    if RULING_RULE_ID_RE.match(target):
        return "rule", target
    found = RULING_PATH_RE.match(target)
    if not found or re.match(r"^r\d+(?:\.\d+)*\.?$", target, re.I):   # `r15.` is a mistyped rule id, not a file
        return None
    path = found.group(1)
    if ("/" not in path and "." not in path) or any(part in (".", "..") for part in path.split("/")):
        return None
    return "path", path


def _bus_tip(bus: pathlib.Path) -> str:
    """The bus clone's committed tip, resolved ONCE per item (`screen_ruling_candidate` calls it once): every target of that
    item is then read at this commit, so a writer that advances the clone mid-check cannot mix two snapshots into one
    verdict. Two items in one `validate` call each resolve their own."""
    rc, out, _err = git_try(bus, "rev-parse", "--verify", "-q", "HEAD^{commit}", extra_env=NO_LAZY_FETCH_ENV)
    if rc != 0 or not out:
        raise Unknown("RULING_BUS_UNREADABLE", f"{bus} is not a git clone with a committed tip; the touches/supersedes targets cannot be checked")
    return out


def _bus_ruling_ids(bus: pathlib.Path, tip: str) -> str:
    """RULINGS.md at `tip` (empty when the tip has none: no rule id is then on the bus). A read failure is UNKNOWN."""
    try:
        return cat_file_blob(bus, tip, RULINGS_FILE, extra_env=NO_LAZY_FETCH_ENV).decode("utf-8", errors="replace")
    except Refusal as e:
        if e.code != "TIP_BLOB_READ_FAILED":
            raise
        raise Unknown("RULING_BUS_OBJECT_UNREADABLE", e.detail) from e


def _bus_has_path(bus: pathlib.Path, tip: str, path: str) -> bool:
    """Whether `path` (a file or a directory) is in the tree of `tip`. `git ls-tree` exits 0 with the path listed or with
    no output when the tip is readable and lacks it; any other exit is a read failure of the bus, which is UNKNOWN and
    never a missing target (`git cat-file -e` exits 128 for both, so it cannot tell them apart). A listing is only the
    parent tree's entry, so the blob or subtree it names is read as well (`cat-file -e`, with no lazy fetch: the bus is
    never written); an entry that lists but does not read is UNKNOWN too. A gitlink names a commit of another
    repository, which this object store need not hold, so it is not read."""
    proc = _run_git(bus, ["ls-tree", "-z", tip, "--", path], extra_env=NO_LAZY_FETCH_ENV)
    if proc.returncode != 0:
        raise Unknown("RULING_BUS_OBJECT_UNREADABLE", f"{tip[:12]}:{path}: {proc.stderr.strip()[:300]}")
    for entry in proc.stdout.split("\0"):
        meta, _tab, name = entry.partition("\t")
        if name != path:
            continue
        fields = meta.split()
        if len(fields) != 3:
            raise Unknown("RULING_BUS_OBJECT_UNREADABLE", f"{tip[:12]}:{path}: unexpected ls-tree entry {entry[:120]!r}")
        _mode, kind, oid = fields
        if kind != "commit":
            read = _run_git(bus, ["cat-file", "-e", oid], extra_env=NO_LAZY_FETCH_ENV)
            if read.returncode != 0:
                raise Unknown("RULING_BUS_OBJECT_UNREADABLE",
                              f"{tip[:12]}:{path}: listed as {kind} {oid[:12]}, but the object does not read: {read.stderr.strip()[:300] or 'missing'}")
        return True
    return False


def _bus_has_rule(rulings: str, rule_id: str) -> bool:
    """A rule id is on the bus when a heading or a bold bullet of RULINGS.md opens with or names it (`## ... R15: ...`,
    `- **R15.3 - ...`); an id mentioned only in prose does not count, and `R15` is not satisfied by `R150` or `R15.3`."""
    token = re.compile(r"(?<![\w.])" + re.escape(rule_id) + r"(?![\w]|\.\d)")
    for line in rulings.splitlines():
        if (line.startswith("#") or re.match(r"^\s*[-*]?\s*\*\*", line)) and token.search(line[:240]):
            return True
    return False


def screen_ruling_candidate(item: dict, bus: pathlib.Path | None = None, own: str | None = None) -> list[str]:
    """Refuse a ruling candidate whose `touches/supersedes:` declaration is missing or empty, mixes `none` with rule
    ids, gives `none` without a reason of NONE_REASON_MIN_WORDS words (the floor a `Doctrine-Export: none` carries),
    names an entry that is not a rule id or a bus-relative path, or, when `bus` is given, names one the bus clone
    does not hold at its tip. Returns the targets it could not check because no bus was given (empty otherwise).
    A bus that cannot be read is UNKNOWN, never a pass. A non-candidate is not looked at."""
    if not is_ruling_candidate(item, own):
        return []
    entries = touches_supersedes_entries(item["body"])
    if not entries:
        raise Refusal("RULING_CANDIDATE_NO_SUPERSEDES",
                      "a ruling candidate needs a non-empty `touches/supersedes:` list (or `Relationship to existing "
                      "doctrine:`): the rules or specs it overlaps, as rule ids or bus paths, or `none: <reason>`")
    nones = [e for e in entries if TOUCHES_NONE_RE.match(e)]
    if nones:
        if len(entries) > 1:
            raise Refusal("RULING_CANDIDATE_NONE_WITH_RULES", "`none` cannot sit beside other entries in `touches/supersedes:`")
        reason = TOUCHES_NONE_RE.match(nones[0]).group(1)
        if len(reason.split()) < NONE_REASON_MIN_WORDS:
            raise Refusal("RULING_CANDIDATE_NONE_NEEDS_REASON",
                          f"`none` in `touches/supersedes:` needs a reason of at least {NONE_REASON_MIN_WORDS} words")
        return []
    targets = [(entry, _ruling_target(entry)) for entry in entries]
    bad = [entry for entry, target in targets if target is None]
    if bad:
        raise Refusal("RULING_CANDIDATE_TARGET_MALFORMED",
                      "each `touches/supersedes:` entry is a rule id (`R15`, `R14.8`) or a bus-relative path "
                      "(`RULINGS.md:1126-1152`, `tools/fleet-sweep.mjs`); not one: " + "; ".join(repr(b) for b in bad[:4]))
    if bus is None:
        return [entry for entry, _target in targets]
    tip = _bus_tip(bus)
    rulings: str | None = None
    missing: list[str] = []
    for entry, target in targets:
        kind, name = target
        if kind == "rule":
            rulings = _bus_ruling_ids(bus, tip) if rulings is None else rulings
            present = _bus_has_rule(rulings, name)
        else:
            present = _bus_has_path(bus, tip, name)
        if not present:
            missing.append(entry)
    if missing:
        raise Refusal("RULING_CANDIDATE_TARGET_NOT_ON_BUS",
                      f"not on the bus clone at its tip {tip[:12]} (a path is bus-relative, a rule id is a heading or bold bullet of "
                      f"{RULINGS_FILE}): " + "; ".join(repr(m) for m in missing[:6]))
    return []


# ---------- CLI commands ----------

def cmd_validate(paths: list[pathlib.Path], deny_terms: list[tuple[str, str]],
                 bus: pathlib.Path | None = None, exercise: dict | None = None) -> int:
    """Validate item files; `bus` is the bus clone a ruling candidate's targets are read from. With `exercise`
    (bus, timeout, fills, scratch_root) also check each valid item's routes."""
    ok = True
    valid: list[tuple[str, dict]] = []
    concern_ids: dict[str, pathlib.Path] = {}
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
            unchecked = screen_ruling_candidate(item, bus, own_candidate_slug(p.name))
            if unchecked:
                print(f"[doctrine-outbox] NOTE {p}: touches/supersedes targets NOT checked against the bus (pass --bus <bus clone> "
                      f"or set ${RULING_BUS_ENV}): {'; '.join(unchecked[:6])}", file=sys.stderr)
            if "concern" in item:
                cid = item["concern"]["id"]
                if cid in concern_ids:
                    raise Refusal("CONCERN_DUPLICATE_ID", f"{cid} also heads {concern_ids[cid].name}; the drain holds both")
                concern_ids[cid] = p
        except Refusal as e:
            print(f"[doctrine-outbox] REFUSED {p}: {e}", file=sys.stderr)
            ok = False
            continue
        print(f"[doctrine-outbox] OK {p}: target={item['meta']['target']} kind={item['meta']['kind']}")
        valid.append((p.name, item))
    if exercise is not None:
        if run_exercise(valid, **exercise) != 0:
            ok = False
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
    validate_p.add_argument("--exercise", action="store_true",
                            help="also check every command each item names (see 'card routes') in a scratch clone of the bus: "
                                 "existence for every route, execution only for a program on the allowlist (git)")
    validate_p.add_argument("--bus", type=pathlib.Path, default=None,
                            help=f"the bus clone (default ${RULING_BUS_ENV}): a ruling candidate's touches/supersedes targets "
                                 "must exist at its tip, and --exercise copies it; read, never written or run in")
    validate_p.add_argument("--timeout", type=float, default=ROUTE_DEFAULT_TIMEOUT_S,
                            help="with --exercise: seconds before a route is ROUTE_TIMEOUT (default %(default)s)")
    validate_p.add_argument("--fill", action="append", default=[], metavar="NAME=VALUE",
                            help="with --exercise: the value for a <NAME> placeholder in a route (repeatable)")
    validate_p.add_argument("--scratch-root", type=pathlib.Path, default=None,
                            help="with --exercise: where the scratch clone is made and removed (default: the temp dir)")

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
            env_bus = os.environ.get(RULING_BUS_ENV)
            bus = args.bus.resolve() if args.bus is not None else (pathlib.Path(env_bus).resolve() if env_bus else None)
            exercise = None
            if args.exercise:
                if not math.isfinite(args.timeout) or args.timeout <= 0:
                    parser.error(f"--timeout wants a finite number of seconds above zero; got {args.timeout:g}")
                fills: dict[str, str] = {}
                for pair in args.fill:
                    name, sep, value = pair.partition("=")
                    if not sep or not name:
                        parser.error("--fill wants NAME=VALUE; an argument without `=` was given (its text is not echoed: it may be a secret)")
                    fills[name] = value
                exercise = {"bus": bus, "timeout": args.timeout, "fills": fills, "scratch_root": args.scratch_root}
            elif args.fill or args.scratch_root is not None:
                parser.error("--fill and --scratch-root need --exercise")
            return cmd_validate(args.items, deny_terms, bus=bus, exercise=exercise)
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
