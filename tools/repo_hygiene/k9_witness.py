#!/usr/bin/env python3
"""K9 pushed-tree resumability witness (kernel clause K9, "resume from durable artifacts alone").

It judges the TREE BEING LANDED, never the working tree: every byte it inspects comes
from `git ls-tree` and `git cat-file` on an object id. A tree it cannot read is UNKNOWN
(exit 2), never RED and never PASS.

Modes
    tree <treeish> [--repo PATH] [--json]
        C1  spine present (CLAUDE.md, agents/factory-kernel-instance.md, docs/ROTATION.md)
            as regular-file blobs with exact case; no case-variant twin of a spine path
        C2  CLAUDE.md links the instance map; the instance map has exactly one `| K9 ` row
            with exactly 4 non-empty cells
        C3  every backticked path in the K9 row resolves in the tree (board-local paths are
            exempt and listed); at least one tracked path resolves
        C4  no derived value (full sha, short sha, session guid, pid) in the K9 row or in
            docs/ROTATION.md
    pre-push <remote-name> <remote-url> [--repo PATH]
        git's pre-push argv; the refs arrive ONLY on stdin. A ref is judged (as `tree`
        judges its tip) when the pushed range touches a spine path, tools/session-checkpoint.py
        or this file; otherwise it passes untouched. Deletions are skipped.
    install --repo PATH [--ref REF] [--dry-run] / uninstall --repo PATH
        manage an opt-in pre-push layer. The block is loaded from a pinned ref, not from the
        pushing checkout, and hands stdin on to later hook layers.

Exit codes: 0 PASS, 1 RED (a check failed), 2 UNKNOWN (usage, git failure, unreadable object).
The last stdout line in every mode is
    k9-witness.v1 verdict=<PASS|RED|UNKNOWN> tree=<40hex|none> checks=<n> failed=<ids>
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

VERDICT_TAG = "k9-witness.v1"
GIT_TIMEOUT_SECONDS = 60
HEX40 = re.compile(r"^[0-9a-f]{40}$")
ZERO_SHA = "0" * 40

SPINE = ("CLAUDE.md", "agents/factory-kernel-instance.md", "docs/ROTATION.md")
INSTANCE_MAP_LINK = re.compile(r"\]\(\s*agents/factory-kernel-instance\.md(#[^)\s]*)?\s*\)")
TOUCH_PATHS = SPINE + ("tools/session-checkpoint.py", "tools/repo_hygiene/k9_witness.py")
FILE_MODES = ("100644", "100755")

# Ported verbatim from the board checker; the board checker lives under .claude-state, which
# is gitignored and absent in CI, so it is not imported.
DERIVED_PATTERNS = (
    ("FULL-SHA", re.compile(r"\b[0-9a-f]{40}\b")),
    ("SHORT-SHA", re.compile(r"\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*[0-9])[0-9a-f]{7,39}\b")),
    ("SESSION-GUID", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("PID", re.compile(r"(?i)\bpid[= ]\d{3,}\b")),
)

DEFAULT_REF = "refs/remotes/fork/master"
REF_SAFE = re.compile(r"^[A-Za-z0-9._/+-]+$")
BLOCK_BEGIN = "# >>> mlv-k9-witness (managed) >>>"
BLOCK_BEGIN_PREFIX = "# >>> mlv-k9-witness"
BLOCK_END = "# <<< mlv-k9-witness <<<"
SHELL_SHEBANG = re.compile(r"^#!\s*(\S*/)?(env\s+)?(ba|da|z)?sh(\s|$)")
_KW = r"(^|[;&|(]|\b(while|until|do|then|else|if)\b)\s*"
STDIN_READERS = (
    re.compile(r"\$\(\s*cat\b"),
    re.compile(r"`\s*cat\b"),
    re.compile(_KW + r"cat(\s|$)"),
    re.compile(_KW + r"read(\s|$)"),
    re.compile(r"\bgit[ -]lfs\s+pre-push\b"),
    re.compile(r"/dev/stdin|<&0"),
)


class WitnessError(Exception):
    """The witness cannot judge (usage, git failure, unreadable object). Maps to exit 2."""


# ---------------------------------------------------------------------------------------
# git access. Every call goes through _run; git() raises on any failure.
# ---------------------------------------------------------------------------------------

def _run(repo: str, args: list[str], input_bytes: bytes | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            input=input_bytes, capture_output=True, timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WitnessError(f"git {args[0]} could not run: {exc}") from exc


def git(repo: str, *args: str, input_bytes: bytes | None = None) -> bytes:
    """Run git and return stdout. A non-zero exit raises; an error is never an empty result."""
    p = _run(repo, list(args), input_bytes)
    if p.returncode != 0:
        detail = p.stderr.decode("utf-8", "replace").strip()[:200]
        raise WitnessError(f"git {' '.join(args[:2])} exited {p.returncode}: {detail}")
    return p.stdout


def object_exists(repo: str, sha: str) -> bool:
    """True/False for a full object id; any other git outcome raises."""
    p = _run(repo, ["cat-file", "-e", sha])
    if p.returncode == 0:
        return True
    if p.returncode == 1:
        return False
    raise WitnessError(f"git cat-file -e exited {p.returncode}: {p.stderr.decode('utf-8', 'replace').strip()[:200]}")


def resolve_tree(repo: str, treeish: str) -> str:
    if not treeish or treeish.startswith("-"):
        raise WitnessError(f"refusing treeish {treeish!r}")
    out = git(repo, "rev-parse", "--verify", "--quiet", treeish + "^{tree}").decode("ascii", "replace").strip()
    if not HEX40.match(out):
        raise WitnessError(f"unparseable tree id for {treeish!r}: {out!r}")
    return out


def list_tree(repo: str, tree: str) -> dict[str, tuple[str, str, str]]:
    """path -> (mode, type, sha), read from the tree object alone."""
    raw = git(repo, "ls-tree", "-r", "-z", "--full-tree", tree)
    entries: dict[str, tuple[str, str, str]] = {}
    for rec in raw.split(b"\0"):
        if not rec:
            continue
        meta, sep, path = rec.partition(b"\t")
        parts = meta.decode("ascii", "replace").split(" ")
        if not sep or len(parts) != 3 or not HEX40.match(parts[2]):
            raise WitnessError(f"unparseable ls-tree record: {rec[:80]!r}")
        entries[path.decode("utf-8", "surrogateescape")] = (parts[0], parts[1], parts[2])
    return entries


def read_blob_text(repo: str, entry: tuple[str, str, str]) -> str:
    text = git(repo, "cat-file", "blob", entry[2]).decode("utf-8", "replace")
    return text[1:] if text.startswith("﻿") else text


# ---------------------------------------------------------------------------------------
# tree checks
# ---------------------------------------------------------------------------------------

def _is_file(entry: tuple[str, str, str] | None) -> bool:
    return entry is not None and entry[1] == "blob" and entry[0] in FILE_MODES


POINTER_SUFFIX = re.compile(r"(#\S*|:\d+(-\d+)?)$")
POINTER_EXTENSION = re.compile(r"\.[A-Za-z0-9_-]+$")


def _pointer_token(
    span: str, entries: dict[str, tuple[str, str, str]]
) -> tuple[str | None, tuple[str, str, str] | None]:
    """Judge one backticked span whole (whitespace inside it is part of the path).

    Returns (path, entry). entry is the tree entry when the path is a regular-file blob, else
    None. path is None when the span is not path-shaped (it needs a `/` and a file extension).
    A tracked file named exactly like the span wins; otherwise surrounding whitespace and a
    trailing `#anchor` or `:line[-line]` suffix are dropped before the lookup.
    """
    stripped = span.strip()
    candidates = [span, stripped, POINTER_SUFFIX.sub("", stripped)]
    for cand in candidates:
        if _is_file(entries.get(cand)):
            return cand, entries[cand]
    name = candidates[-1]
    if "/" not in name or not POINTER_EXTENSION.search(name):
        return None, None
    return name, None


def _lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _check(check_id: str, ok: bool, notes: list[str]) -> dict:
    return {"id": check_id, "ok": ok, "notes": notes}


def judge_tree(repo: str, tree: str) -> dict:
    entries = list_tree(repo, tree)
    texts: dict[str, list[str]] = {}
    for path in SPINE:
        if _is_file(entries.get(path)):
            texts[path] = _lines(read_blob_text(repo, entries[path]))

    # C1 spine present, exact case, no case-variant twin
    notes: list[str] = []
    by_fold: dict[str, list[str]] = {}
    for path in entries:
        by_fold.setdefault(path.casefold(), []).append(path)
    for path in SPINE:
        if not _is_file(entries.get(path)):
            notes.append(f"spine path missing or not a regular-file blob: {path}")
        for other in by_fold.get(path.casefold(), []):
            if other != path:
                notes.append(f"case-variant of spine path {path}: {other}")
    c1 = _check("C1", not notes, notes or ["spine present"])

    # C2 entry chain and K9 row shape
    notes = []
    claude = texts.get("CLAUDE.md")
    if claude is None:
        notes.append("CLAUDE.md unreadable")
    elif not INSTANCE_MAP_LINK.search("\n".join(claude)):
        notes.append("CLAUDE.md does not link agents/factory-kernel-instance.md")
    imap = texts.get("agents/factory-kernel-instance.md")
    k9_rows: list[tuple[int, str]] = []
    if imap is None:
        notes.append("instance map unreadable")
    else:
        k9_rows = [(i + 1, ln) for i, ln in enumerate(imap) if ln.startswith("| K9 ")]
        if len(k9_rows) != 1:
            notes.append(f"instance map has {len(k9_rows)} rows starting '| K9 ' (need exactly 1)")
        else:
            stripped = k9_rows[0][1].strip()
            cells = stripped.split("|")[1:-1] if stripped.endswith("|") else []
            if len(cells) != 4:
                notes.append(f"K9 row has {len(cells)} cells (need exactly 4)")
            elif any(not c.strip() for c in cells):
                notes.append("K9 row has an empty cell")
    c2 = _check("C2", not notes, notes or ["entry chain and K9 row shape ok"])

    # C3 pointers in the K9 row
    notes = []
    board_local: list[str] = []
    tracked_ok = 0
    if len(k9_rows) != 1:
        notes.append("no single K9 row to read")
    else:
        verified: set[str] = set()
        for span in re.findall(r"`([^`]+)`", k9_rows[0][1]):
            tok, hit = _pointer_token(span, entries)
            if tok is None:
                continue
            if tok.startswith((".claude-state/", "~/")) or re.match(r"^[A-Za-z]:[\\/]", tok):
                board_local.append(tok)
                continue
            if hit is not None:
                # Present in the listing is not readable: read the blob through git and let any
                # failure raise (exit 2) rather than count the pointer as resolved.
                if hit[2] not in verified:
                    git(repo, "cat-file", "blob", hit[2])
                    verified.add(hit[2])
                tracked_ok += 1
                continue
            twins = [p for p in by_fold.get(tok.casefold(), []) if p != tok]
            notes.append(f"pointer does not resolve: {tok}" + (f" (tree has {twins[0]})" if twins else ""))
        if tracked_ok == 0 and not notes:
            notes.append("no tracked pointer in the K9 row resolves")
    c3 = _check("C3", not notes, notes or [f"{tracked_ok} tracked pointer(s) resolve"])

    # C4 derived values in the K9 row and docs/ROTATION.md
    notes = []
    scoped: list[tuple[str, int, str]] = [("K9 row", n, ln) for n, ln in k9_rows]
    scoped += [("docs/ROTATION.md", i + 1, ln) for i, ln in enumerate(texts.get("docs/ROTATION.md", []))]
    for source, number, line in scoped:
        for name, pattern in DERIVED_PATTERNS:
            if pattern.search(line):
                notes.append(f"{name} in {source} line {number}")
    c4 = _check("C4", not notes, notes or ["no derived values"])

    checks = [c1, c2, c3, c4]
    return {
        "tree": tree,
        "verdict": "PASS" if all(c["ok"] for c in checks) else "RED",
        "checks": checks,
        "board_local": board_local,
    }


# ---------------------------------------------------------------------------------------
# output
# ---------------------------------------------------------------------------------------

def verdict_line(verdict: str, tree: str, checks: int, failed: list[str]) -> str:
    return f"{VERDICT_TAG} verdict={verdict} tree={tree} checks={checks} failed={','.join(failed) or 'none'}"


def _failed_ids(results: list[dict]) -> list[str]:
    return sorted({c["id"] for r in results for c in r["checks"] if not c["ok"]})


def print_tree_result(result: dict, label: str = "") -> None:
    prefix = f"{label}: " if label else ""
    for c in result["checks"]:
        print(f"{prefix}{c['id']} {'PASS' if c['ok'] else 'FAIL'}: " + "; ".join(c["notes"]))
    for tok in result["board_local"]:
        print(f"{prefix}BOARD-LOCAL (exempt, not judged): {tok}")


# ---------------------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------------------

def cmd_tree(args: argparse.Namespace) -> int:
    tree = resolve_tree(args.repo, args.treeish)
    result = judge_tree(args.repo, tree)
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print_tree_result(result)
    print(verdict_line(result["verdict"], tree, len(result["checks"]), _failed_ids([result])))
    return 0 if result["verdict"] == "PASS" else 1


def parse_ref_lines(text: str) -> list[tuple[str, str, str, str]]:
    lines = []
    for raw in re.split(r"\r?\n", text):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(" ")
        if len(parts) != 4 or not HEX40.match(parts[1]) or not HEX40.match(parts[3]):
            raise WitnessError(f"malformed pre-push line: {line[:120]!r}")
        lines.append((parts[0], parts[1], parts[2], parts[3]))
    return lines


def range_touches(repo: str, local_sha: str, remote_sha: str) -> tuple[bool, str]:
    if remote_sha == ZERO_SHA:
        rev_args = [local_sha, "--not", "--remotes"]
    elif not object_exists(repo, remote_sha):
        return True, "remote sha is not a local object (fail closed)"
    else:
        rev_args = [f"{remote_sha}..{local_sha}"]
    raw = git(
        repo, "-c", "log.showSignature=false", "log", "--format=", "--name-only", "-z", "-m",
        "--no-renames", *rev_args, "--",
    )
    watched = {p.casefold() for p in TOUCH_PATHS}
    for rec in raw.split(b"\0"):
        path = rec.decode("utf-8", "surrogateescape").strip("\n")
        if path and path.casefold() in watched:
            return True, f"range changes {path}"
    return False, "range touches no watched path"


def cmd_pre_push(args: argparse.Namespace) -> int:
    refs = parse_ref_lines(sys.stdin.buffer.read().decode("utf-8", "strict"))
    severity = 0
    judged: list[dict] = []
    for ref, local_sha, _remote_ref, remote_sha in refs:
        if local_sha == ZERO_SHA:
            print(f"{ref}: deletion, skipped")
            continue
        try:
            touching, why = range_touches(args.repo, local_sha, remote_sha)
            if not touching:
                print(f"{ref}: untouched ({why})")
                continue
            print(f"{ref}: judging tip {local_sha} ({why})")
            result = judge_tree(args.repo, resolve_tree(args.repo, local_sha))
        except WitnessError as exc:
            print(f"{ref}: UNKNOWN: {exc}")
            severity = 2
            continue
        judged.append(result)
        print_tree_result(result, label=ref)
        if result["verdict"] == "RED":
            severity = max(severity, 1)
    verdict = ("PASS", "RED", "UNKNOWN")[severity]
    first_bad = next((r for r in judged if r["verdict"] != "PASS"), None)
    shown = first_bad or (judged[-1] if judged else None)
    print(verdict_line(
        verdict, shown["tree"] if shown else "none", sum(len(r["checks"]) for r in judged), _failed_ids(judged),
    ))
    return severity


# ---------------------------------------------------------------------------------------
# installer
# ---------------------------------------------------------------------------------------

def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


def render_block(ref: str, interpreter: str) -> list[str]:
    py = _sh_quote(interpreter)
    return [
        BLOCK_BEGIN,
        f"K9_REF=${{MLV_K9_WITNESS_REF:-{ref}}}",
        "K9_REFS=$(cat)",
        "K9_TOOL=$(mktemp) || exit 2",
        'git show "$K9_REF:tools/repo_hygiene/k9_witness.py" > "$K9_TOOL" && [ -s "$K9_TOOL" ] || '
        '{ echo "k9: cannot read witness at $K9_REF; fetch fork" >&2; rm -f "$K9_TOOL"; exit 2; }',
        f'[ -x {py} ] || {{ echo "k9: interpreter missing" >&2; rm -f "$K9_TOOL"; exit 2; }}',
        f"printf '%s\\n' \"$K9_REFS\" | {py} \"$K9_TOOL\" pre-push \"$1\" \"$2\"; K9_RC=$?",
        'rm -f "$K9_TOOL"; [ "$K9_RC" -eq 0 ] || exit "$K9_RC"',
        "exec 0<<K9EOF",
        "$K9_REFS",
        "K9EOF",
        BLOCK_END,
    ]


def _find_reader(lines: list[str], start: int, stop: int) -> tuple[int, str] | None:
    for i in range(start, stop):
        code = lines[i].strip()
        if not code or code.startswith("#"):
            continue
        if any(p.search(code) for p in STDIN_READERS):
            return i + 1, code
    return None


def _block_span(lines: list[str]) -> tuple[int, int] | None:
    begins = [i for i, ln in enumerate(lines) if ln.startswith(BLOCK_BEGIN_PREFIX)]
    ends = [i for i, ln in enumerate(lines) if ln == BLOCK_END]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0] < begins[0]:
        raise WitnessError("refusing: malformed managed block (need exactly one begin and one end marker)")
    return begins[0], ends[0]


def _load_hook_lines(data: bytes) -> list[str]:
    if b"\r" in data:
        raise WitnessError("refusing: hook has CR bytes; convert it to LF first, then install")
    try:
        lines = data.decode("utf-8").split("\n")
    except UnicodeDecodeError as exc:
        raise WitnessError(f"refusing: hook is not UTF-8: {exc}") from exc
    if not SHELL_SHEBANG.match(lines[0]):
        raise WitnessError("refusing: hook has no POSIX-shell shebang on line 1")
    return lines


def plan_install(existing: bytes | None, block: list[str]) -> tuple[str, list[str] | None]:
    """Return (action, new_lines). new_lines is None when nothing changes."""
    if existing is None:
        return "created", ["#!/bin/sh", *block, ""]
    lines = _load_hook_lines(existing)
    span = _block_span(lines)
    if span is not None:
        begin, end = span
        if begin != 1:
            above = _find_reader(lines, 1, begin)
            extra = f"; stdin reader above it at line {above[0]}: {above[1]}" if above else ""
            raise WitnessError(f"refusing: our block is not directly under the shebang (starts at line {begin + 1}){extra}")
        if lines[1:end + 1] == block:
            return "present", None
        return "migrated", lines[:1] + block + lines[end + 1:]
    reader = _find_reader(lines, 1, len(lines))
    if reader:
        raise WitnessError(
            f"refusing: existing stdin reader at line {reader[0]} ({reader[1]}); not guessing a placement"
        )
    return "inserted", lines[:1] + block + lines[1:]


def hook_path(repo: str) -> Path:
    out = git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push")
    text = out.decode("utf-8", "surrogateescape").strip()
    if not text:
        raise WitnessError("git returned no hook path")
    return Path(text)


def _write_hook(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes("\n".join(lines).encode("utf-8"))
    os.chmod(path, path.stat().st_mode | 0o111)


def cmd_install(args: argparse.Namespace) -> int:
    if not REF_SAFE.match(args.ref):
        raise WitnessError(f"refusing ref with unsafe characters: {args.ref!r}")
    interpreter = os.path.abspath(sys.executable).replace("\\", "/")
    path = hook_path(args.repo)
    existing = path.read_bytes() if path.exists() else None
    action, new_lines = plan_install(existing, render_block(args.ref, interpreter))
    if args.dry_run:
        print(f"install: dry-run, would report '{action}' for {path}")
    else:
        if new_lines is not None:
            _write_hook(path, new_lines)
        print(f"install: {action} {path}")
    print(verdict_line("PASS", "none", 0, []))
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    path = hook_path(args.repo)
    if not path.exists():
        print(f"uninstall: no hook at {path}")
    else:
        lines = _load_hook_lines(path.read_bytes())
        span = _block_span(lines)
        if span is None:
            print(f"uninstall: no managed block in {path}")
        else:
            rest = lines[:span[0]] + lines[span[1] + 1:]
            if all(not ln.strip() for ln in rest[1:]):
                path.unlink()
                print(f"uninstall: removed {path} (it held only our block)")
            else:
                _write_hook(path, rest)
                print(f"uninstall: removed our block from {path}")
    print(verdict_line("PASS", "none", 0, []))
    return 0


# ---------------------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------------------

class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # usage errors are UNKNOWN (exit 2), with the verdict line last
        raise WitnessError(f"usage: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="k9_witness", description="K9 pushed-tree resumability witness")
    sub = parser.add_subparsers(dest="mode", required=True, parser_class=_Parser)

    p = sub.add_parser("tree", help="judge a tree-ish")
    p.add_argument("treeish")
    p.add_argument("--repo", default=".")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("pre-push", help="git pre-push entry; refs on stdin")
    p.add_argument("remote_name")
    p.add_argument("remote_url")
    p.add_argument("--repo", default=".")
    p.set_defaults(func=cmd_pre_push)

    p = sub.add_parser("install", help="install the opt-in pre-push layer")
    p.add_argument("--repo", required=True)
    p.add_argument("--ref", default=DEFAULT_REF)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("uninstall", help="remove the managed block")
    p.add_argument("--repo", required=True)
    p.set_defaults(func=cmd_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    try:
        args = build_parser().parse_args(argv)
        return args.func(args)
    except (WitnessError, UnicodeDecodeError, OSError) as exc:  # OSError: an unwritable/unreadable hook
        print(f"UNKNOWN: {exc}")
        print(f"k9 witness could not judge: {exc}", file=sys.stderr)
        print(verdict_line("UNKNOWN", "none", 0, []))
        return 2


if __name__ == "__main__":
    sys.exit(main())
