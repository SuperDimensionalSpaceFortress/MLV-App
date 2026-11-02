#!/usr/bin/env python3
"""Run fleet factory tools from REVIEWED, PINNED bus bytes -- the one generic pin runner.

A consumer project vendors this file once, at factory-pins/pin-runner.py, beside
factory-pins/factory-pins.json. Every other factory tool runs from the bus by pin:

    pin-runner.py run <tool> [--] [args...]        execute the pinned bytes; exit = the tool's
    pin-runner.py pin <tool> --commit <sha> --reviewer R --reason R [--path P] [--date D]
                                                   record a reviewed bus blob (a review decision)
    pin-runner.py verify                           non-zero unless every pin is well-formed and
                                                   the bus clone holds its exact bytes
    pin-runner.py status                           each pin vs the clone's HEAD and origin/master
    pin-runner.py install <tool> --commit <bus-sha> --reviewer R --reason R [--project-root D] [--bus CLONE]
                                                   one command to become a consumer: vendor + adopt this
                                                   runner when absent, pin <tool> (path from the bus's
                                                   tools/factory/registry.json at that commit), print the
                                                   `run` line to wire. Idempotent. It only PINS: never executes.
    pin-runner.py init --profile <name> --commit <bus-sha> --reviewer R --reason R [--project-root D] [--bus CLONE] [--name N]
                                                   one command to install a whole factory: vendor + adopt the runner,
                                                   pin every tool of the registry profile, seed its data files, merge
                                                   its Claude hooks into .claude/settings.json, write its git hooks,
                                                   emit a rule-map skeleton (the pinned check-rule-tags --emit-map)
                                                   and a product-throughput class stub. Idempotent; never overwrites
                                                   a file the project already has. Exit 4 = INCOMPLETE: what it could
                                                   not do without overwriting is listed as steps left by hand.
    pin-runner.py list [--commit REF]              the registry: tool, interpreter, bus path, purpose; then profiles

Law 1 of the bus ("doctrine is data, never instructions") applied to code: bus bytes never run
until the consuming project reviewed and pinned them. `run` reads the pinned blob BY OBJECT ID
from a local bus clone (`git cat-file blob <id>`: LF bytes, independent of the clone's worktree,
branch and HEAD), re-verifies blob id + byte count + sha256 (git does not re-hash on read),
and executes those verified bytes with the caller's cwd, stdin, stdout and stderr. A bus push
therefore changes nothing that runs; it makes `status` print STALE. Upgrading is `pin` + a
reviewed commit of factory-pins.json, never automatic.

How a verified python tool executes:
  * runner started isolated (`python -I pin-runner.py run ...`, the form a hook should use): the
    verified bytes are compiled and executed IN this process as __main__. The tool gets the same
    isolation the runner has, no second interpreter starts, and nothing is written to disk, so
    no file exists between verification and execution (no check-then-exec window).
  * runner not isolated: the bytes go into a fresh private temp directory holding only that file,
    are re-verified there, and run as `python -I <file>` (node tools always run this way).

Blob cache (speed; never trust): every verified blob is kept, content-addressed, in
$FACTORY_PINS_CACHE (default: %LOCALAPPDATA%/factory-pins/blobs, else ~/.cache/factory-pins/blobs).
A cached copy is used only after the same blob-id + byte-count + sha256 check; a cached file that
fails it is ignored and the bus clone is read instead, so a tampered cache never runs. Once a pin
has been run or verified on a machine, it keeps running from the reviewed bytes even when the bus
clone is absent or moved -- the UNAVAILABLE (exit 3) case below is then confined to a pin that has
never been seen on this machine.

factory-pins.json (schema "factory-pins.v1"; "_"-prefixed keys are comments):
    bus_repository   the bus's clone URL (informational)
    bus_clone        [path, ...] candidate local clones, relative to the consumer root or absolute;
                     overridden by --bus or $FACTORY_BUS_CLONE
    runner           {commit, path, blob, bytes, sha256}: the bus copy this vendored runner equals
    tools            {name: {path, commit, blob, bytes, sha256, interpreter, reviewer, reason,
                     pinned_utc[, with]}}; interpreter is "python" (this interpreter, -I) or "node"
                     with (optional, python only; `pin --with T`): other pinned tools whose verified bytes
                     are materialised beside this one in the private directory (a pinned GROUP, for a tool
                     that loads siblings by file name). A group always runs from that directory, never
                     in-process; one level (a member with its own `with` is refused). A runner without
                     this key refuses such a pins file (exit 2) rather than run a group short of members.

Exit codes of the runner itself (a tool's own exit code passes through unchanged):
    2  REFUSED: malformed pins, unknown tool, or bytes that do not match the pin (tamper class).
       A PreToolUse hook run through the runner therefore BLOCKS on a refusal.
    3  UNAVAILABLE: the pinned blob is in neither the blob cache nor a bus clone (fetch it). Loud,
       and deliberately not 2, so a never-provisioned box does not block every tool call
       (including the `git clone` that fixes it). Nothing unverified ever runs on this path.
    4  INCOMPLETE (init only): everything init could do is done; the steps it would have had to overwrite
       a project file for are printed as HAND lines. Re-run init after doing them: it then exits 0.
Stdlib only; Windows and POSIX.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

SCHEMA = "factory-pins.v1"
PINS_NAME = "factory-pins.json"
RUNNER_BUS_PATH = "tools/factory/pin-runner.py"
HERE = Path(__file__).resolve().parent
EXIT_REFUSED, EXIT_UNAVAILABLE, EXIT_INCOMPLETE = 2, 3, 4
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TOOL_KEYS = {"path", "commit", "blob", "bytes", "sha256", "interpreter", "reviewer", "reason", "pinned_utc"}
INTERPRETERS = ("python", "node")
REGISTRY_BUS_PATH = "tools/factory/registry.json"
REGISTRY_SCHEMA = "factory-registry.v1"
REGISTRY_KEYS = {"path", "interpreter", "purpose"}
MAX_PINS_BYTES, MAX_TOOL_BYTES = 256 * 1024, 4 * 1024 * 1024
PROFILE_KEYS = {"purpose", "tools", "seeds", "claude_hooks", "git_hooks", "rule_map", "throughput_stub"}
HOOK_KEYS = {"event", "matcher", "tool", "args", "timeout", "never_block"}
CLAUDE_EVENTS = ("SessionStart", "SessionEnd", "Stop", "SubagentStop", "PreToolUse", "PostToolUse", "UserPromptSubmit",
                 "PreCompact", "Notification")
GIT_HOOK_KEYS = {"hook", "path", "markers"}
# How a runner release that supports --never-block declares it (split so this constant is not itself the marker).
NEVER_BLOCK_ARG = b'ap.add_argument("--never' + b'-block"'
SEED_DEST = re.compile(r"factory-pins/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
GIT_HOOKS = ("pre-push", "pre-commit", "commit-msg")
HOOK_ARG = re.compile(r"[A-Za-z0-9@/._=:,+-]+\Z")   # no quote, space or shell metacharacter can reach a settings command
PROJECT_ARG = "@project/"                           # a hook arg naming a file of the project: "$CLAUDE_PROJECT_DIR/..."
RULE_TAGS_SCHEMA, THROUGHPUT_SCHEMA = "fleet/rule-tags/v1", "fleet/product-throughput-classes/v1"
CONFLICT = re.compile(rb"^(?:<{7}|={7}|>{7}|\|{7})(?:[ \t]|\r?$)", re.M)


class Refused(Exception):
    pass


class Unavailable(Exception):
    pass


class Unparseable(Refused):
    """The pins bytes are not JSON at all (or carry merge-conflict markers): a broken working copy,
    not a pin decision. `run` may then judge with the committed pins (see run_pins)."""


def blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise Refused(f"pins: duplicate key {k!r}")
        out[k] = v
    return out


# Repository-locating variables git exports to hooks (GIT_DIR is set for a hook run from a linked
# worktree). Every call here names its repository with -C, so they must not redirect it: with GIT_DIR
# set, `git -C factory-pins rev-parse --show-toplevel` answers factory-pins/ itself, and a bus call
# would read the consumer's object store.
REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX")


def git(repo: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k.upper() not in REPO_ENV}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    return subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True,
                          timeout=timeout, env=env)


# --- pins file ---------------------------------------------------------------------------------
def pins_path(args: argparse.Namespace) -> Path:
    return Path(args.pins).resolve() if args.pins else HERE / PINS_NAME


def consumer_root(pins_file: Path) -> Path:
    """The consumer repository: the git toplevel above the pins file, else its parent's parent."""
    try:
        p = git(pins_file.parent, "rev-parse", "--show-toplevel", timeout=10)
        if p.returncode == 0 and p.stdout.strip():
            return Path(p.stdout.decode().strip())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return pins_file.parent.parent


def load_pins(path: Path, raw: bytes | None = None) -> dict[str, Any]:
    if raw is None:
        try:
            raw = path.read_bytes()
        except OSError:
            raise Refused(f"pins unreadable at {path}") from None
    if len(raw) > MAX_PINS_BYTES:
        raise Refused("pins: over size cap")
    if CONFLICT.search(raw):
        raise Unparseable("pins: merge-conflict markers")
    try:
        pins = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_dupes,
                          parse_constant=lambda c: (_ for _ in ()).throw(Refused("pins: non-finite number")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Unparseable(f"pins: invalid JSON ({exc.__class__.__name__})") from exc
    if not isinstance(pins, dict) or pins.get("schema") != SCHEMA:
        raise Refused(f"pins: schema must be {SCHEMA!r}")
    tools = pins.get("tools")
    if not isinstance(tools, dict):
        raise Refused("pins: 'tools' must be an object")
    for name, t in tools.items():
        if name.startswith("_"):
            continue
        validate_tool(name, t)
    validate_groups(tools)
    return pins


def bad_path(p: Any) -> bool:
    return (not isinstance(p, str) or not p or p.startswith(("/", "\\")) or ":" in p or "\\" in p
            or any(seg in ("", ".", "..") for seg in p.split("/")))


def validate_tool(name: str, t: Any) -> None:
    if not NAME.match(name):
        raise Refused(f"pins: bad tool name {name!r}")
    if not isinstance(t, dict) or {k for k in t if not k.startswith("_")} - {"with"} != TOOL_KEYS:
        raise Refused(f"pins: tool {name!r} key set must be exactly {sorted(TOOL_KEYS)} (plus an optional 'with')")
    if bad_path(t["path"]):
        raise Refused(f"pins: tool {name!r} path must be a plain bus-relative POSIX path")
    if not (HEX40.match(str(t["commit"])) and HEX40.match(str(t["blob"])) and HEX64.match(str(t["sha256"]))
            and type(t["bytes"]) is int and 0 < t["bytes"] <= MAX_TOOL_BYTES):
        raise Refused(f"pins: tool {name!r} has a malformed commit/blob/bytes/sha256")
    if t["interpreter"] not in INTERPRETERS:
        raise Refused(f"pins: tool {name!r} interpreter must be one of {INTERPRETERS}")
    for k in ("reviewer", "reason", "pinned_utc"):
        if not isinstance(t[k], str) or not t[k].strip():
            raise Refused(f"pins: tool {name!r} needs a non-empty {k} (a pin is a review decision)")
    if "with" in t:
        w = t["with"]
        if (not isinstance(w, list) or not w or len(set(map(str, w))) != len(w)
                or not all(isinstance(m, str) and NAME.match(m) and m != name for m in w)):
            raise Refused(f"pins: tool {name!r} 'with' must be a non-empty list of distinct other tool names")
        if t["interpreter"] != "python":
            raise Refused(f"pins: tool {name!r} 'with' is for python tools only")


def validate_groups(tools: dict[str, Any]) -> None:
    """A pinned GROUP (bus K154): a tool whose `with` names other pinned tools runs with every member's verified
    bytes beside it, so it can load them by file name. One level: a member's own `with` is not followed, so a
    member that declares one is refused rather than silently run without its siblings."""
    for name, t in tools.items():
        if name.startswith("_") or "with" not in t:
            continue
        names = {Path(t["path"]).name}
        for m in t["with"]:
            mt = tools.get(m)
            if mt is None or m.startswith("_"):
                raise Refused(f"pins: tool {name!r} runs with {m!r}, which is not pinned")
            if "with" in mt:
                raise Refused(f"pins: tool {name!r} runs with {m!r}, which has its own 'with' (groups are one level)")
            if mt["interpreter"] != "python":
                raise Refused(f"pins: tool {name!r} runs with {m!r}, which is not a python tool")
            base = Path(mt["path"]).name
            if base in names:
                raise Refused(f"pins: tool {name!r} group has two files named {base!r}")
            names.add(base)


def tool_pin(pins: dict[str, Any], name: str) -> dict[str, Any]:
    t = pins["tools"].get(name)
    if t is None or name.startswith("_"):
        known = ", ".join(sorted(k for k in pins["tools"] if not k.startswith("_"))) or "(none)"
        raise Refused(f"no pin for tool {name!r}; pinned: {known}")
    return t


def check_bytes(name: str, t: dict[str, Any], data: bytes) -> None:
    if len(data) != t["bytes"] or hashlib.sha256(data).hexdigest() != t["sha256"] or blob_id(data) != t["blob"]:
        raise Refused(f"{name}: bytes do not match the pin (blob {t['blob'][:12]}, {t['bytes']} bytes)")


# --- bus clone ---------------------------------------------------------------------------------
def find_bus(args: argparse.Namespace, pins: dict[str, Any], pins_file: Path) -> Path:
    # An explicit choice is authoritative: --bus, else $FACTORY_BUS_CLONE, else the pins' candidates.
    # The consumer root (a git call) is computed only when a relative candidate needs it.
    tried = []
    root: Path | None = None
    listed = pins.get("bus_clone", [])
    cands = ([args.bus] if args.bus else [os.environ["FACTORY_BUS_CLONE"]] if os.environ.get("FACTORY_BUS_CLONE")
             else listed if isinstance(listed, list) else [listed])
    for c in cands:
        if not isinstance(c, str) or not c:
            continue
        p = Path(os.path.expanduser(c))
        if not p.is_absolute():
            root = root or consumer_root(pins_file)
            p = root / p
        tried.append(str(p))
        if (p / ".git").exists():
            return p.resolve()
    raise Unavailable("no bus clone found (tried: " + ("; ".join(tried) or "nothing") +
                      "). Pass --bus, set FACTORY_BUS_CLONE, or list one in factory-pins.json bus_clone.")


def pinned_bytes(bus: Path, name: str, t: dict[str, Any]) -> bytes:
    try:
        p = git(bus, "cat-file", "blob", t["blob"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Unavailable(f"git cannot read the pinned blob ({exc.__class__.__name__})") from None
    if p.returncode != 0:
        raise Unavailable(f"{name}: pinned blob {t['blob'][:12]} is not in the bus clone {bus} (git -C <clone> fetch origin)")
    check_bytes(name, t, p.stdout)
    cache_put(t["blob"], p.stdout)
    return p.stdout


# --- blob cache: content-addressed, re-verified on every read, never trusted -------------------
def cache_dir() -> Path:
    if os.environ.get("FACTORY_PINS_CACHE"):
        return Path(os.environ["FACTORY_PINS_CACHE"])
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return Path(base) / "factory-pins" / "blobs"


def cache_get(name: str, t: dict[str, Any]) -> bytes | None:
    try:
        data = (cache_dir() / t["blob"]).read_bytes()
        check_bytes(name, t, data)
        return data
    except (OSError, Refused):
        return None  # absent, unreadable, or not the pinned bytes: never used; the bus clone decides


def cache_put(blob: str, data: bytes) -> None:
    """Best effort: write to a private temp name, then rename into place (readers never see a torn file)."""
    try:
        d = cache_dir()
        d.mkdir(parents=True, exist_ok=True)
        if (d / blob).is_file() and (d / blob).stat().st_size == len(data):
            return
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, d / blob)
    except OSError:
        pass


def tool_bytes(args: argparse.Namespace, pins: dict[str, Any], name: str, t: dict[str, Any]) -> bytes:
    """The pinned bytes, verified: from the cache when it holds them exactly, else from a bus clone."""
    data = cache_get(name, t)
    if data is not None:
        return data
    return pinned_bytes(find_bus(args, pins, pins_path(args)), name, t)


def blob_at(bus: Path, ref: str, path: str) -> str:
    try:
        p = git(bus, "rev-parse", "--verify", "--quiet", f"{ref}:{path}", timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return "(unreadable)"
    return p.stdout.decode().strip() if p.returncode == 0 else "(absent)"


# --- commands ----------------------------------------------------------------------------------
def child_env() -> dict[str, str]:
    # NODE_OPTIONS / NODE_PATH and PYTHON* would load code the pin never saw (python also gets -I).
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("NODE_", "PYTHON"))}
    env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


def run_in_process(name: str, t: dict[str, Any], data: bytes, tool_args: list[str]) -> int:
    """Execute verified python bytes as __main__ of a fresh module in this (isolated) interpreter.
    The bytes executed are the very object that was hashed: there is no file to swap afterwards."""
    import types
    code = compile(data, f"<factory-pin {name} {t['blob'][:12]}>", "exec", dont_inherit=True)
    mod = types.ModuleType("__main__")
    mod.__dict__.update(__file__=t["path"], __builtins__=__builtins__)
    saved_main, saved_argv = sys.modules.get("__main__"), sys.argv
    sys.modules["__main__"], sys.argv = mod, [t["path"], *tool_args]
    try:
        exec(code, mod.__dict__)
        return 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        sys.argv = saved_argv
        if saved_main is not None:
            sys.modules["__main__"] = saved_main


def cmd_run(args: argparse.Namespace, rest: list[str]) -> int:
    if not rest:
        raise Refused("run needs a tool name")
    name, tool_args = rest[0], rest[1:]
    if tool_args[:1] == ["--"]:
        tool_args = tool_args[1:]
    pins = run_pins(args)
    t = tool_pin(pins, name)
    data = tool_bytes(args, pins, name, t)
    # A group (`with`) loads its members by file name beside itself, so it never runs in-process: there its
    # __file__ is the bus path, and a sibling load would read the CALLER's tree. Every member is verified first.
    members = [(m, pins["tools"][m], tool_bytes(args, pins, m, pins["tools"][m])) for m in t.get("with", [])]
    if t["interpreter"] == "python" and sys.flags.isolated and not members:
        return run_in_process(name, t, data, tool_args)
    return run_materialized(t, data, members, tool_args)


def run_materialized(t: dict[str, Any], data: bytes, members: list, tool_args: list[str],
                     capture: bool = False, cwd: Path | None = None) -> Any:
    """Write verified bytes (and a group's members) into a fresh private directory, prove the directory holds exactly
    those blobs, and run the tool there (python -I / node). Returns the exit code, or the CompletedProcess if capture."""
    work = Path(tempfile.mkdtemp(prefix="factory-pin-"))
    try:
        script = work / Path(t["path"]).name
        script.write_bytes(data)
        for _m, mt, mdata in members:
            (work / Path(mt["path"]).name).write_bytes(mdata)
        want = {script.name: t["blob"], **{Path(mt["path"]).name: mt["blob"] for _m, mt, _d in members}}
        if ({x.name for x in work.iterdir()} != set(want)
                or any(blob_id((work / f).read_bytes()) != b for f, b in want.items())):
            raise Refused("materialized directory differs from the pin")
        argv = [sys.executable, "-I", str(script)] if t["interpreter"] == "python" else ["node", str(script)]
        try:
            if capture:
                return subprocess.run([*argv, *tool_args], env=child_env(), capture_output=True, text=True, cwd=cwd,
                                      encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=300)
            return subprocess.run([*argv, *tool_args], env=child_env()).returncode
        except subprocess.TimeoutExpired:
            raise Refused(f"{script.name} did not finish in 300 s") from None
        except OSError as exc:
            raise Unavailable(f"cannot start {argv[0]} ({exc})") from None
    finally:
        shutil.rmtree(work, ignore_errors=True)


def read_pins_ref(args: argparse.Namespace) -> bytes | None:
    """--pins-ref REF: read factory-pins/factory-pins.json from a git ref of the consumer (a pre-push
    layer judges with the pins on origin/master, so a pushed branch cannot re-pin its own judge)."""
    if not getattr(args, "pins_ref", None):
        return None
    pins_file = pins_path(args)
    root = consumer_root(pins_file)
    rel = pins_file.relative_to(root).as_posix() if pins_file.is_relative_to(root) else f"factory-pins/{PINS_NAME}"
    p = git(root, "show", f"{args.pins_ref}:{rel}")
    if p.returncode != 0:
        raise Refused(f"cannot read {args.pins_ref}:{rel} (git fetch origin)")
    return p.stdout


def run_pins(args: argparse.Namespace) -> dict[str, Any]:
    """The pins `run` judges with. A working copy that is not JSON at all -- conflict markers mid-merge, a
    torn write -- would otherwise refuse every hook-guarded command, including the ones that resolve it.
    Then, and only then, the pins COMMITTED at HEAD are used (already-reviewed bytes, so Law 1 is not
    weakened), with a notice on stderr. Never "no guard": an unusable HEAD copy keeps the refusal.
    The index is not consulted: mid-merge the conflicted path has no stage 0 and stage 2 is HEAD's blob,
    and an uncommitted stage-0 copy is no more reviewed than the working copy that just failed.
    --pins-ref (pre-push) never falls back: it names its ref explicitly."""
    ref = read_pins_ref(args)
    pfile = pins_path(args)
    try:
        return load_pins(pfile, ref)
    except Unparseable as exc:
        if ref is not None:
            raise
        why = str(exc)
    try:
        p = git(pfile.parent, "show", f"HEAD:./{pfile.name}", timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refused(f"{why}; HEAD's committed pins unreadable ({exc.__class__.__name__})") from None
    if p.returncode != 0:
        raise Refused(f"{why}; no committed pins at HEAD to fall back to")
    try:
        pins = load_pins(pfile, p.stdout)
    except Refused as exc:
        raise Refused(f"{why}; HEAD's committed pins are unusable too ({exc})") from None
    print(f"[factory-pins] FALLBACK: {pfile} is unusable ({why}); judging with the pins committed at HEAD. "
          f"Resolve the working copy.", file=sys.stderr)
    return pins


def resolve_commit(bus: Path, ref: str) -> str:
    c = git(bus, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if c.returncode != 0:
        raise Refused(f"not a commit in {bus}: {ref}")
    return c.stdout.decode().strip()


def read_blob(bus: Path, commit: str, path: str) -> tuple[str, bytes]:
    """The bytes of <commit>:<path>, proven in-process against git's own tree entry: the id from
    `rev-parse <commit>:<path>` is the expectation, sha1("blob N\\0" + bytes) of what `cat-file` returned must
    equal it. Git blobs hash raw (no LF normalisation; that is only for a vendored file read from disk).
    A response that does not hash to the tree entry is never recorded, vendored or run."""
    w = git(bus, "rev-parse", "--verify", f"{commit}:{path}")
    want = w.stdout.decode().strip()
    if w.returncode != 0 or not HEX40.match(want):
        raise Refused(f"{commit[:12]} has no {path}")
    b = git(bus, "cat-file", "blob", f"{commit}:{path}")
    if b.returncode != 0 or blob_id(b.stdout) != want:
        raise Refused(f"the bus returned bytes for {commit[:12]}:{path} that are not object {want[:12]}; nothing recorded or vendored")
    return want, b.stdout


def pin_entry(bus: Path, commit: str, path: str, old: dict[str, Any], interpreter: str | None,
              reviewer: str, reason: str, date: str | None) -> dict[str, Any]:
    _want, data = read_blob(bus, commit, path)
    entry = {k: v for k, v in old.items() if k.startswith("_")}
    entry.update({"path": path, "commit": commit, "blob": blob_id(data), "bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "interpreter": interpreter or old.get("interpreter") or ("node" if path.endswith((".mjs", ".js")) else "python"),
                  "reviewer": reviewer, "reason": reason,
                  "pinned_utc": date or _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")})
    return entry


def cmd_pin(args: argparse.Namespace, _rest: list[str]) -> int:
    if args.tool == "pin-runner":
        raise Refused("the runner itself is vendored, not run by pin; use `pin-runner.py adopt-runner`")
    pfile = pins_path(args)
    pins = load_pins(pfile) if pfile.exists() else {"schema": SCHEMA, "tools": {}}
    bus = find_bus(args, pins, pfile)
    old = pins["tools"].get(args.tool, {})
    path = args.path or old.get("path")
    if not path:
        raise Refused(f"new tool {args.tool!r}: pass --path <bus path>")
    commit = resolve_commit(bus, args.commit)
    entry = pin_entry(bus, commit, path, old, args.interpreter, args.reviewer, args.reason, args.date)
    group = args.with_ or old.get("with")
    if group:
        entry["with"] = list(group)
    validate_tool(args.tool, entry)
    validate_groups({**pins["tools"], args.tool: entry})
    pins["tools"][args.tool] = entry
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"PINNED {args.tool}: {path} blob {entry['blob'][:12]} at bus {commit[:12]} -> {pfile}")
    return 0


def own_bytes() -> bytes:
    # Hash as LF: a CRLF checkout of the vendored copy is the same reviewed bytes.
    return Path(__file__).read_bytes().replace(b"\r\n", b"\n")


def cmd_adopt_runner(args: argparse.Namespace, _rest: list[str]) -> int:
    """Record which bus commit this vendored runner equals (after copying it from that commit)."""
    pfile = pins_path(args)
    pins = load_pins(pfile) if pfile.exists() else {"schema": SCHEMA, "tools": {}}
    bus = find_bus(args, pins, pfile)
    commit = resolve_commit(bus, args.commit)
    _want, data = read_blob(bus, commit, RUNNER_BUS_PATH)
    if data != own_bytes():
        raise Refused(f"this runner is not byte-identical to {commit[:12]}:{RUNNER_BUS_PATH}; copy that blob first")
    pins["runner"] = runner_record(commit, data)
    pfile.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"RUNNER {RUNNER_BUS_PATH} blob {pins['runner']['blob'][:12]} at bus {commit[:12]} -> {pfile}")
    return 0


# --- registry + install: one command to become a consumer --------------------------------------
def load_registry(raw: bytes, where: str) -> dict[str, dict[str, Any]]:
    return parse_registry(raw, where)[0]


def parse_registry(raw: bytes, where: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """tools/factory/registry.json (DATA read from a reviewed commit, never executed):
    {schema, tools: {name: {path, interpreter, purpose[, with]}}[, profiles: {name: PROFILE}]}. Returns (tools, profiles)."""
    try:
        reg = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_dupes)
    except (UnicodeError, json.JSONDecodeError, Refused) as exc:
        raise Refused(f"registry at {where}: invalid JSON ({exc.__class__.__name__})") from exc
    if not isinstance(reg, dict) or reg.get("schema") != REGISTRY_SCHEMA or not isinstance(reg.get("tools"), dict):
        raise Refused(f"registry at {where}: schema must be {REGISTRY_SCHEMA!r} with a 'tools' object")
    tools = {k: v for k, v in reg["tools"].items() if not k.startswith("_")}
    for name, t in tools.items():
        if (not NAME.match(name) or not isinstance(t, dict)
                or {k for k in t if not k.startswith("_")} - {"with"} != REGISTRY_KEYS
                or bad_path(t["path"]) or t["interpreter"] not in INTERPRETERS
                or not isinstance(t["purpose"], str) or not t["purpose"].strip()):
            raise Refused(f"registry at {where}: malformed entry {name!r} (need exactly {sorted(REGISTRY_KEYS)}, "
                          "plus an optional 'with')")
        # Bus K154: a registry group names registered python members (installed first, then the group).
        w = t.get("with")
        if "with" in t and (t["interpreter"] != "python" or not isinstance(w, list) or not w
                            or len(set(map(str, w))) != len(w)
                            or not all(isinstance(m, str) and m != name and m in tools
                                       and tools[m].get("interpreter") == "python" and "with" not in tools[m]
                                       for m in w)):
            raise Refused(f"registry at {where}: entry {name!r} 'with' must list distinct other registered python "
                          "tools that have no 'with' of their own")
    profiles = reg.get("profiles", {})
    if not isinstance(profiles, dict):
        raise Refused(f"registry at {where}: 'profiles' must be an object")
    profiles = {k: v for k, v in profiles.items() if not k.startswith("_")}
    for name, p in profiles.items():
        validate_profile(name, p, tools, where)
    return tools, profiles


def validate_profile(name: str, p: Any, tools: dict[str, dict[str, Any]], where: str) -> None:
    """A PROFILE (read by `init`): {purpose, tools: [registered names], seeds?: {"factory-pins/<file>": <bus path>},
    claude_hooks?: [{event, tool, matcher?, args?, timeout?}], git_hooks?: [{hook, path, marker}], rule_map?: bool,
    throughput_stub?: bool}. Every write it implies stays inside factory-pins/, .claude/settings.json or the hooks dir."""
    def bad(why: str) -> Refused:
        return Refused(f"registry at {where}: profile {name!r} {why}")
    if not NAME.match(name) or not isinstance(p, dict):
        raise bad("is malformed")
    keys = {k for k in p if not k.startswith("_")}
    if keys - PROFILE_KEYS or not {"purpose", "tools"} <= keys:
        raise bad(f"needs purpose and tools, and only keys from {sorted(PROFILE_KEYS)}")
    if not isinstance(p["purpose"], str) or not p["purpose"].strip():
        raise bad("needs a purpose")
    ts = p["tools"]
    if not isinstance(ts, list) or not ts or len(set(map(str, ts))) != len(ts) or not all(isinstance(t, str) and t in tools for t in ts):
        raise bad("'tools' must be a non-empty list of distinct registered tools")
    for t in ts:
        missing = [m for m in tools[t].get("with", []) if m not in ts]
        if missing:
            raise bad(f"installs group {t!r} without its member(s) {missing}")
    seeds = p.get("seeds", {})
    reserved = {f"factory-pins/{PINS_NAME}".lower(), "factory-pins/pin-runner.py"}   # compared case-folded (Windows)
    if not isinstance(seeds, dict) or not all(
            isinstance(d, str) and SEED_DEST.match(d) and d.lower() not in reserved
            and isinstance(s, str) and not bad_path(s) for d, s in seeds.items()):
        raise bad("'seeds' must map factory-pins/<plain file name> (not the pins or the runner, in any case) to bus paths")
    hooks = p.get("claude_hooks", [])
    if not isinstance(hooks, list):
        raise bad("'claude_hooks' must be a list")
    for h in hooks:
        if (not isinstance(h, dict) or {k for k in h if not k.startswith("_")} - HOOK_KEYS or not {"event", "tool"} <= set(h)
                or h["event"] not in CLAUDE_EVENTS or h["tool"] not in ts
                or ("matcher" in h and not (isinstance(h["matcher"], str) and h["matcher"].strip()))
                or not (isinstance(h.get("args", []), list) and all(isinstance(a, str) and HOOK_ARG.match(a) for a in h.get("args", [])))
                or not (type(h.get("timeout", 60)) is int and 1 <= h.get("timeout", 60) <= 600)
                or type(h.get("never_block", False)) is not bool):
            raise bad(f"has a malformed claude hook {h!r} (event from {CLAUDE_EVENTS}, a tool of the profile, args "
                      f"matching {HOOK_ARG.pattern}, timeout 1-600)")
    gh = p.get("git_hooks", [])
    if not isinstance(gh, list) or len({h.get("hook") for h in gh if isinstance(h, dict)}) != len(gh) or not all(
            isinstance(h, dict) and set(h) == GIT_HOOK_KEYS and h["hook"] in GIT_HOOKS and not bad_path(h["path"])
            and isinstance(h["markers"], list) and h["markers"] and len(set(map(str, h["markers"]))) == len(h["markers"])
            and all(isinstance(m, str) and m.strip() and "\n" not in m for m in h["markers"]) for h in gh):
        raise bad(f"'git_hooks' entries need exactly {sorted(GIT_HOOK_KEYS)} (markers: the distinct one-line layer "
                  f"markers the body carries), a hook from {GIT_HOOKS}, one each")
    for k in ("rule_map", "throughput_stub"):
        if type(p.get(k, False)) is not bool:
            raise bad(f"'{k}' must be true or false")
    if p.get("rule_map") and "check-rule-tags" not in ts:
        raise bad("'rule_map' needs check-rule-tags among its tools")


def registry_doc_at(bus: Path, ref: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    try:
        _want, data = read_blob(bus, ref, REGISTRY_BUS_PATH)
    except Refused as exc:
        if " has no " in str(exc):
            raise Refused(f"{ref[:12]} has no {REGISTRY_BUS_PATH}; pin by hand with `pin <tool> --path ...`") from None
        raise
    return parse_registry(data, f"{ref[:12]}:{REGISTRY_BUS_PATH}")


def registry_at(bus: Path, ref: str) -> dict[str, dict[str, Any]]:
    return registry_doc_at(bus, ref)[0]


def runner_record(commit: str, data: bytes) -> dict[str, Any]:
    return {"commit": commit, "path": RUNNER_BUS_PATH, "blob": blob_id(data), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def short(v: Any) -> str:
    return v[:12] if isinstance(v, str) and HEX40.match(v) else str(v)


class Consumer:
    """What install and init share: the consumer's pins, the bus at the reviewed commit, and the runner decision.
    Law 1 on the installer itself: before anything is read from the registry or written, the bytes RUNNING this
    command and the bytes it will vendor must both be the reviewed blob at --commit (git's own object id)."""

    def __init__(self, args: argparse.Namespace, verb: str):
        self.root = Path(args.project_root).resolve() if args.project_root else Path.cwd().resolve()
        self.pfile = Path(args.pins).resolve() if args.pins else self.root / "factory-pins" / PINS_NAME
        if args.bus:
            args.bus = str(Path(args.bus).resolve())  # a command-line clone is cwd-relative, not consumer-relative
        self.args = args
        self.pins = load_pins(self.pfile) if self.pfile.exists() else {"schema": SCHEMA, "tools": {}}
        self.before = json.dumps(self.pins, sort_keys=True)
        self.bus = find_bus(args, self.pins, self.pfile)
        self.commit = resolve_commit(self.bus, args.commit)
        self.want, self.runner_bytes = read_blob(self.bus, self.commit, RUNNER_BUS_PATH)
        if blob_id(own_bytes()) != self.want:
            raise Refused(f"this running pin-runner.py is not byte-identical to {self.commit[:12]}:{RUNNER_BUS_PATH}; {verb} "
                          f"only runs from the reviewed blob: git -C <bus> show {self.commit[:12]}:{RUNNER_BUS_PATH} > "
                          f"factory-pins/pin-runner.py, then python factory-pins/pin-runner.py {verb} ...")
        self.vendored = self.pfile.parent / "pin-runner.py"

    def plan_runner(self) -> tuple[bool, bool]:
        """(vendor, adopt). install/init never overwrite a vendored runner: an adopted older release stays (status
        reports STALE), an unadopted byte-identical one is adopted, anything else refuses. An adopted record is DATA the
        consumer wrote, so it is believed only when the bus agrees: the recorded commit must carry exactly that blob at
        the runner's bus path (a forged runner with a matching hand-written record is refused)."""
        have = self.vendored.read_bytes().replace(b"\r\n", b"\n") if self.vendored.is_file() else None
        rec = self.pins.get("runner")
        if have is None:
            return True, True
        if isinstance(rec, dict) and rec.get("blob") == blob_id(have) and rec.get("sha256") == hashlib.sha256(have).hexdigest():
            if not HEX40.match(str(rec.get("commit"))):
                raise Refused(f"{self.pfile}: the runner record names no commit; adopt-runner it at the commit it equals")
            try:
                want, _data = read_blob(self.bus, rec["commit"], RUNNER_BUS_PATH)
            except Refused:
                raise Refused(f"the adopted runner's commit {rec['commit'][:12]} has no {RUNNER_BUS_PATH} in {self.bus} "
                              "(fetch the bus, or the record is not a bus release)") from None
            if want != rec["blob"]:
                raise Refused(f"{self.vendored} is recorded as bus {rec['commit'][:12]}:{RUNNER_BUS_PATH}, but that commit "
                              f"carries blob {want[:12]}, not {rec['blob'][:12]}: not a bus release; nothing written")
            return False, False
        if have == self.runner_bytes:
            return False, True
        raise Refused(f"{self.vendored} is neither adopted nor byte-identical to the bus runner at {self.commit[:12]}; delete "
                      f"it to let install vendor a reviewed copy, or adopt-runner it at the commit it equals")

    def plan_pin(self, name: str, r: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], bool, bytes]:
        """(entry, old, same, verified bytes) for pinning registry entry <name> at the commit; nothing written."""
        old = self.pins["tools"].get(name, {})
        a = self.args
        entry = pin_entry(self.bus, self.commit, r["path"], old, r["interpreter"], a.reviewer, a.reason, a.date)
        if r.get("with"):
            missing = [m for m in r["with"] if m not in self.pins["tools"]]
            if missing:
                raise Refused(f"{name} runs with {', '.join(missing)}: install {' and '.join(missing)} first")
            entry["with"] = list(r["with"])
        same = all(old.get(k) == entry.get(k) for k in ("path", "commit", "blob", "interpreter", "with"))
        if same:
            entry = old
        validate_tool(name, entry)
        validate_groups({**self.pins["tools"], name: entry})
        return entry, old, same, read_blob(self.bus, self.commit, r["path"])[1]

    def write_runner(self) -> None:
        self.vendored.write_bytes(self.runner_bytes)
        wrote = self.vendored.read_bytes().replace(bytes([13, 10]), bytes([10]))
        if blob_id(wrote) != self.want or hashlib.sha256(wrote).hexdigest() != hashlib.sha256(self.runner_bytes).hexdigest():
            self.vendored.unlink(missing_ok=True)
            raise Refused(f"{self.vendored} does not hold the reviewed runner after writing (object {self.want[:12]}); removed")

    def record_clone(self) -> None:
        if not self.args.bus:
            return
        try:
            rel = Path(os.path.relpath(self.bus, consumer_root(self.pfile))).as_posix()
        except ValueError:  # another drive: only an absolute path can name it
            rel = self.bus.as_posix()
        listed = self.pins.get("bus_clone", [])
        listed = [listed] if isinstance(listed, str) else list(listed)
        if rel not in listed:
            self.pins["bus_clone"] = [rel, *listed]

    def runner_rel(self) -> str:
        return (self.vendored.relative_to(self.root) if self.vendored.is_relative_to(self.root) else self.vendored).as_posix()


def repin_lines(old: dict[str, Any], entry: dict[str, Any]) -> list[str]:
    return [f"  ~ {k}: {short(old.get(k))} -> {short(entry[k])}"
            for k in ("path", "commit", "blob", "bytes", "sha256", "interpreter", "reviewer", "reason", "pinned_utc")
            if old.get(k) != entry[k]]


def cmd_install(args: argparse.Namespace, _rest: list[str]) -> int:
    """Make a project a consumer of <tool>: vendor + adopt the runner if absent, pin the tool. Writes data
    and one vendored copy of the REVIEWED runner blob; executes nothing (`run` is the only executor)."""
    if args.tool == "pin-runner":
        raise Refused("the runner is vendored by install itself, not pinned as a tool")
    c = Consumer(args, "install")
    reg = registry_at(c.bus, c.commit)
    if args.tool not in reg:
        raise Refused(f"unknown tool {args.tool!r}; the registry at {c.commit[:12]} lists: {', '.join(sorted(reg)) or '(none)'}")
    r = reg[args.tool]
    vendor, adopt = c.plan_runner()
    entry, old, same, _data = c.plan_pin(args.tool, r)
    c.pfile.parent.mkdir(parents=True, exist_ok=True)
    if vendor:
        c.write_runner()
    if adopt:
        c.pins["runner"] = runner_record(c.commit, c.runner_bytes)
    c.record_clone()
    c.pins["tools"][args.tool] = entry
    changed = vendor or json.dumps(c.pins, sort_keys=True) != c.before
    if changed:
        c.pfile.write_text(json.dumps(c.pins, indent=2) + "\n", encoding="utf-8", newline="\n")
    if not changed:
        print(f"NO-OP: {args.tool} already pinned at {c.commit[:12]} (blob {entry['blob'][:12]}) in {c.pfile}")
    else:
        print(f"{'RE-PINNED' if old and not same else 'INSTALLED'} {args.tool}: {r['path']} blob {entry['blob'][:12]} "
              f"at bus {c.commit[:12]} -> {c.pfile}")
        if vendor or adopt:
            print(f"  {'vendored' if vendor else 'adopted'} runner: {c.vendored} (bus blob {c.pins['runner']['blob'][:12]})")
        if old and not same:
            for line in repin_lines(old, entry):
                print(line)
    print(f"WIRE: python {c.runner_rel()} run {args.tool} -- <tool args>   (a hook should use `python -I`)")
    print(f"CHECK: python {c.runner_rel()} verify")
    return 0


# --- init: a whole factory in one command --------------------------------------------------------
def hook_command(h: dict[str, Any]) -> str:
    nb = " --never-block" if h.get("never_block") else ""
    cmd = f'python -I "$CLAUDE_PROJECT_DIR/factory-pins/pin-runner.py"{nb} run {h["tool"]}'
    if h.get("args"):
        cmd += " --" + "".join(" " + (f'"$CLAUDE_PROJECT_DIR/{a[len(PROJECT_ARG):]}"' if a.startswith(PROJECT_ARG) else a)
                               for a in h["args"])
    return cmd


def registrations(settings: Any, event: str) -> list[tuple[str | None, str]]:
    """[(matcher or None, command)] for every command hook registered on <event>."""
    out: list[tuple[str | None, str]] = []
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    groups = hooks.get(event, []) if isinstance(hooks, dict) else []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        for h in g.get("hooks") if isinstance(g.get("hooks"), list) else []:
            if isinstance(h, dict) and isinstance(h.get("command"), str):
                out.append((g.get("matcher") or None, h["command"]))
    return out


SHELL_SEPARATORS = {"&&", "||", ";", "|", "&"}
INTERPRETER_WORDS = {"python", "python3", "py", "pythonw", "node", "pwsh", "powershell", "sh", "bash"}
ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


RUNNER_NAMES = ("pin-runner.py", "pin_runner.py")
RUNNER_VALUE_OPTS = ("--pins", "--pins-ref", "--bus")
# The project's own vendored runner, as a hook names it (init writes the first form).
PROJECT_RUNNER = re.compile(r"(?:\$CLAUDE_PROJECT_DIR/|\$\{CLAUDE_PROJECT_DIR\}/|\./)?factory-pins/pin-runner\.py\Z")


def command_scripts(command: str) -> list[list[str]]:
    """For each simple command in a hook command line: [the program it runs, *its arguments]. The program is the
    first word, or the first non-option word after an interpreter (python -I x.py ...). Words are split like a POSIX
    shell with its punctuation (`a;b`, `a&&b`, `a|b`, `(a)`, redirections) as separate tokens: quotes removed,
    backslashes read as path separators, env assignments skipped."""
    try:
        lex = shlex.shlex(command.replace("\\", "/"), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        lex.commenters = ""                             # a '#' inside a path (C:/dev/C#/...) is part of the word
        words = list(lex)
    except ValueError:
        words = command.split()
    simple: list[list[str]] = [[]]
    for w in words:
        if w and all(ch in "();<>|&" for ch in w):
            simple.append([])
        else:
            simple[-1].append(w)
    out = []
    for c in simple:
        i = 0
        while i < len(c) and ENV_ASSIGN.match(c[i]):
            i += 1
        if i < len(c):
            prog = c[i].rsplit("/", 1)[-1].lower()
            if prog.removesuffix(".exe") in INTERPRETER_WORDS:
                i += 1
                while i < len(c) and c[i].startswith("-"):
                    i += 2 if c[i] in ("-X", "-W", "-m") else 1
        if i < len(c):
            out.append(c[i:])
    return out


def runner_tool(c: list[str]) -> tuple[str | None, list[str]]:
    """When the simple command c runs a pin runner's `run` subcommand: (the tool operand, the runner's own options).
    Only the subcommand position counts -- `run a -- ... run b` runs a, never b."""
    if c[0].rsplit("/", 1)[-1].lower() not in RUNNER_NAMES:
        return None, []
    i = 1
    while i < len(c) and c[i].startswith("--"):
        i += 2 if c[i] in RUNNER_VALUE_OPTS else 1
    if i + 1 < len(c) and c[i] == "run":
        return c[i + 1], c[1:i]
    return None, []


def wires_tool(command: str, tool: str, path: str) -> bool:
    """Does a hook command already run this tool: a copy by file name, or any pin runner running it?"""
    names = {Path(path).name.lower(), Path(path).name.replace("-", "_").lower()}
    return any(c[0].rsplit("/", 1)[-1].lower() in names or runner_tool(c)[0] == tool for c in command_scripts(command))


def load_settings(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        s = json.loads(path.read_bytes().decode("utf-8-sig"), object_pairs_hook=_no_dupes)
    except Refused as exc:
        raise Refused(f"{path} has a {str(exc).removeprefix('pins: ')}; fix it, then re-run init") from None
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise Refused(f"{path} is not readable JSON ({exc.__class__.__name__}); fix it, then re-run init") from None
    if not isinstance(s, dict) or not isinstance(s.get("hooks", {}), dict) or not all(
            isinstance(v, list) and all(isinstance(g, dict) and isinstance(g.get("hooks", []), list) for g in v)
            for v in s.get("hooks", {}).values()):
        raise Refused(f"{path}: 'hooks' is not {{event: [{{matcher?, hooks: [...]}}]}}; fix it, then re-run init")
    return s


def by_pin(command: str, tool: str, never_block: bool = False) -> bool:
    """Is this command THIS project's vendored runner running <tool> (any tool args; no runner option but
    --never-block, so it judges with the pins init writes), with --never-block when the profile requires it (a
    Stop/SessionStart hook that could exit 2 would block)?"""
    for c in command_scripts(command):
        t, opts = runner_tool(c)
        if (t == tool and PROJECT_RUNNER.match(c[0]) and set(opts) <= {"--never-block"}
                and (not never_block or "--never-block" in opts)):
            return True
    return False


def plan_settings(path: Path, hooks: list[dict[str, Any]], reg: dict[str, dict[str, Any]], out: list[str],
                  hand: list[str], never_block_ok: bool = True) -> bytes | None:
    """The merged .claude/settings.json bytes, or None when nothing is added. Existing entries are never edited or
    removed. Every scope Claude Code merges is read: this file, .claude/settings.local.json, the user's
    ~/.claude/settings.json. For each profile hook, the registrations of its tool on its event across those scopes:
      none                      -> WIRED (added here)
      exactly one, and it is this file running the tool BY PIN under the profile's matcher (any flags or tool args:
      `-- --observe-only` is the project's choice)   -> kept
      exactly one, in another scope (settings.local.json or the user's settings; a copy or a pin)
                                -> kept: it already runs once, and adding ours would run it twice
      exactly one here that is a copy (not by pin) or under another matcher, or two or more anywhere
                                -> HAND (replace the copy by the pin / keep exactly one)
    Every HAND step can be cleared by the person, and the same init then exits 0."""
    settings = load_settings(path)
    scopes: list[tuple[Path, dict[str, Any]]] = [(path, settings)]
    for p in (path.parent / "settings.local.json", Path.home() / ".claude" / "settings.json"):
        if p.exists() and p.resolve() != path.resolve():
            try:
                scopes.append((p, load_settings(p)))
            except Refused as exc:
                hand.append(f"{exc}; init could not check it for hooks that would run twice")
    added = False
    for h in hooks:
        cmd, ev, tool, m = hook_command(h), h["event"], h["tool"], h.get("matcher") or None
        label = f"{ev}{'/' + m if m else ''}"
        if h.get("never_block") and not never_block_ok:
            hand.append(f"the vendored runner predates --never-block, so a {label} hook through it would exit 2 on a "
                        f"refusal (on Stop: block every turn end). Delete factory-pins/pin-runner.py and re-run init: it then "
                        f"vendors the reviewed runner of this commit, and wires {tool}")
            continue
        regs = [(p, mm, c) for p, s in scopes for mm, c in registrations(s, ev) if c == cmd or wires_tool(c, tool, reg[tool]["path"])]
        if not regs:
            groups = settings.setdefault("hooks", {}).setdefault(ev, [])
            g = next((g for g in groups if (g.get("matcher") or None) == m), None)
            if g is None:
                g = ({"matcher": m} if m else {})
                g["hooks"] = []
                groups.append(g)
            g.setdefault("hooks", []).append({"type": "command", "command": cmd, "timeout": h.get("timeout", 60)})
            out.append(f"  WIRED   hook {label}: {cmd}")
            added = True
            continue
        where = "; ".join(f"{p} (matcher {mm or 'none'}): {c}" for p, mm, c in regs)
        if len(regs) > 1:
            hand.append(f"{tool} runs {len(regs)} times on {ev}: {where}  -- keep exactly one (the pinned form is: {cmd}"
                        + (f", matcher {m}" if m else "") + f", in {path})")
            continue
        p, mm, c = regs[0]
        if p != path and h.get("never_block") and any(
                runner_tool(sc)[0] == tool and "--never-block" not in runner_tool(sc)[1] for sc in command_scripts(c)):
            hand.append(f"{tool} runs on {ev} from {p} by pin without --never-block: a runner refusal there exits 2 "
                        f"(on Stop: blocks every turn end). Add --never-block before `run` in that entry: {c}")
        elif p != path:
            out.append(f"  kept    hook {label}: {tool} already runs once from {p}" + ("" if by_pin(c, tool) else " (not by pin)"))
        elif by_pin(c, tool, h.get("never_block", False)) and mm == m:
            out.append(f"  kept    hook {label}: {tool}" + ("" if c == cmd else f" (the project's own form: {c})"))
        else:
            hand.append(f"{tool} already runs on {ev} as {where}  -- replace it with: {cmd}"
                        + (f" with matcher {m}" if m else "") + f" in {path}")
    return (json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8") if added else None


def rule_tags_config(name: str, root: Path) -> dict[str, Any]:
    files = [{"path": f, "bullet": "^- "} for f in ("CLAUDE.md", "AGENTS.md") if (root / f).is_file()]
    return {"schema": RULE_TAGS_SCHEMA,
            "_comment": ("Seeded by pin-runner.py init. Add each file whose bullets are rules (optionally a 'section' "
                         "heading), and the hooks/CI files that run your checks as invocation_surfaces; then tag rules in "
                         "the map (kinds enforced|judgement|record|prose). Grammar: the bus tools/check-rule-tags.py "
                         "docstring. Run: python factory-pins/pin-runner.py run check-rule-tags -- --config "
                         f"factory-pins/rule-tags.config.json --project {name} --report"),
            "projects": [{"name": name, "root_candidates": ["."], "files": files, "rule_map": "factory-pins/rule-map.json"}]}


def throughput_stub(name: str) -> dict[str, Any]:
    return {"schema": THROUGHPUT_SCHEMA,
            "note": ("Seeded by pin-runner.py init: a stub. List this repository's repo-relative path prefixes per class "
                     "(pattern rules: the note in the bus tools/product-throughput-classes.json); unmatched paths are "
                     "reported as 'unclassified', never dropped. DIAGNOSTIC ONLY, never a gate. Measure: pwsh "
                     f"<bus>/tools/Get-ProductThroughput.ps1 -Repo . -Project {name} -ClassConfig "
                     "factory-pins/product-throughput-classes.json -Lines"),
            "projects": [{"name": name, "root": ".", "ref": "HEAD",
                          "classes": {"product": [], "tests": [],
                                      "factory-tooling": ["factory-pins/", ".claude/", ".githooks/", ".github/"],
                                      "coordination-paperwork": [], "vendored-copies": []}}]}


def emit_rule_map(entry: dict[str, Any], data: bytes, cfg_bytes: bytes, name: str, root: Path) -> bytes:
    """Run the pinned check-rule-tags (verified bytes, private directory, python -I) with --emit-map against the
    project; return the map's bytes. The config goes in through a private file and the root is explicit, so nothing
    is written into the project by the tool itself."""
    tmp = Path(tempfile.mkdtemp(prefix="factory-init-"))
    try:
        (tmp / "rule-tags.config.json").write_bytes(cfg_bytes)
        p = run_materialized(entry, data, [], ["--config", str(tmp / "rule-tags.config.json"), "--project", name,
                                               "--root", str(root), "--emit-map", str(tmp / "rule-map.json")], capture=True, cwd=tmp)
        if p.returncode != 0 or not (tmp / "rule-map.json").is_file():
            raise Refused(f"check-rule-tags --emit-map exited {p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}")
        return (tmp / "rule-map.json").read_bytes()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def within(p: Path, base: Path) -> bool:
    a, b = os.path.normcase(os.path.abspath(p)), os.path.normcase(os.path.abspath(base))
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:  # another drive
        return False


def refuse_links(p: Path, base: Path) -> None:
    """Refuse a destination that is not a plain path inside base: no symlink or junction from base down to it, and the
    resolved path still inside the resolved base."""
    cur, stop = Path(os.path.abspath(p)), os.path.normcase(os.path.abspath(base))
    while os.path.normcase(str(cur)) != stop and cur != cur.parent:
        if cur.is_symlink() or (hasattr(os.path, "isjunction") and os.path.isjunction(cur)):
            raise Refused(f"{cur} is a link; init writes only plain files inside {base}; nothing written")
        cur = cur.parent
    if not (within(p, base) and within(Path(p).resolve(), Path(base).resolve())):
        raise Refused(f"{p} resolves outside {base}; nothing written")


def remove_dirs(made: list[Path]) -> None:
    for d in reversed(made):                            # deepest first; a directory something else filled stays
        try:
            d.rmdir()
        except OSError:
            pass


def commit_writes(staged: list[tuple[Path, bytes, int]], verify: dict[Path, str] | None = None) -> None:
    """Stage every file under a temporary name beside its destination; only when all are staged, rename each into
    place, moving any original aside first. A failure at either step undoes everything done so far (originals restored,
    new files and temporaries removed) and refuses: no destination keeps a change."""
    temps: list[tuple[Path, Path, int]] = []
    made: list[Path] = []                               # directories this call created (removed again on refusal)
    try:
        for final, body, mode in staged:
            missing = [d for d in (final.parent, *final.parent.parents) if not d.exists()]
            final.parent.mkdir(parents=True, exist_ok=True)
            made += reversed(missing)
            fd, tmp = tempfile.mkstemp(dir=final.parent, prefix=f".{final.name}.init-")
            temps.append((Path(tmp), final, mode))
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
            os.chmod(tmp, mode)
    except OSError as exc:
        for t, _f, _m in temps:
            t.unlink(missing_ok=True)
        remove_dirs(made)
        raise Refused(f"cannot write {final} ({exc.__class__.__name__}: {exc}); nothing written") from None
    for t, final, _m in temps:                          # a staged file that must be a known blob is read back first
        want = (verify or {}).get(final)
        if want is not None and blob_id(t.read_bytes().replace(bytes([13, 10]), bytes([10]))) != want:
            for t2, _f, _m2 in temps:
                t2.unlink(missing_ok=True)
            remove_dirs(made)
            raise Refused(f"{final} would not hold object {want[:12]} (it read back differently once staged); nothing written")
    done: list[tuple[Path, Path | None]] = []          # (destination, where its original was moved, or None)
    try:
        for t, final, _m in temps:
            aside = None
            if final.exists():
                aside = final.with_name(f".{final.name}.init-orig-{os.getpid()}")
                os.replace(final, aside)
            done.append((final, aside))
            os.replace(t, final)
    except OSError as exc:
        for final, aside in reversed(done):
            try:
                if aside is not None:
                    os.replace(aside, final)
                else:
                    final.unlink(missing_ok=True)
            except OSError:
                pass
        for t, _f, _m in temps:
            t.unlink(missing_ok=True)
        remove_dirs(made)
        raise Refused(f"cannot replace {final} ({exc.__class__.__name__}: {exc}); every change undone, nothing written") from None
    for _final, aside in done:
        if aside is not None:
            aside.unlink(missing_ok=True)


def project_name(root: Path, given: str | None) -> str:
    n = given or (re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-") or "project")
    if not NAME.match(n):
        raise Refused(f"--name {n!r} must match {NAME.pattern}")
    return n


def cmd_init(args: argparse.Namespace, _rest: list[str]) -> int:
    """Install a registry PROFILE into a repository in one command. Every bus byte it records, writes or runs is first
    proven to be git's object at --commit (read_blob), exactly as install does; the only tool it executes is the
    profile's pinned check-rule-tags (--emit-map), from those verified bytes. Everything is planned before the first
    write; a file the project already has is never overwritten (a conflict is a HAND step, exit 4). Never commits."""
    if args.pins:
        root = Path(args.project_root) if args.project_root else Path.cwd()
        if os.path.normcase(os.path.abspath(args.pins)) != os.path.normcase(os.path.abspath(root / "factory-pins" / PINS_NAME)):
            raise Refused(f"init writes the pins at factory-pins/{PINS_NAME} only (its hooks and configs name that place); "
                          "drop --pins")
    c = Consumer(args, "init")
    top = git(c.root, "rev-parse", "--show-toplevel", timeout=10)
    if top.returncode != 0 or Path(top.stdout.decode().strip()).resolve() != c.root:
        raise Refused(f"init installs at the top level of a git repository; {c.root} is not one")
    reg, profiles = registry_doc_at(c.bus, c.commit)
    if args.profile not in profiles:
        raise Refused(f"unknown profile {args.profile!r}; the registry at {c.commit[:12]} lists: "
                      f"{', '.join(sorted(profiles)) or '(none)'}")
    prof = profiles[args.profile]
    out: list[str] = []
    hand: list[str] = []
    writes: list[tuple[Path, bytes]] = []
    fp = c.pfile.parent

    vendor, adopt = c.plan_runner()
    order = sorted(prof["tools"], key=lambda t: "with" in reg[t])          # group members before their group
    verified: dict[str, tuple[dict[str, Any], bytes]] = {}
    for t in order:
        entry, old, same, data = c.plan_pin(t, reg[t])
        c.pins["tools"][t] = entry
        verified[t] = (entry, data)
        out.append(f"  {'kept   ' if same else 'RE-PINNED' if old else 'PINNED '} {t}: {reg[t]['path']} blob {entry['blob'][:12]}")
        if old and not same:
            out += repin_lines(old, entry)
    name = project_name(c.root, args.name)

    for dest, src in sorted(prof.get("seeds", {}).items()):
        if (fp / Path(dest).name).exists():                     # beside the pins, wherever --pins puts them
            out.append(f"  kept    {dest} (the project's own)")
        else:
            writes.append((fp / Path(dest).name, read_blob(c.bus, c.commit, src)[1]))
            out.append(f"  SEEDED  {dest} from {src}")
    if prof.get("throughput_stub"):
        dest = fp / "product-throughput-classes.json"
        if dest.exists():
            out.append("  kept    factory-pins/product-throughput-classes.json")
        else:
            writes.append((dest, (json.dumps(throughput_stub(name), indent=2) + "\n").encode()))
            out.append("  STUB    factory-pins/product-throughput-classes.json (fill the classes)")
    if prof.get("rule_map"):
        cfg_path, map_path = fp / "rule-tags.config.json", fp / "rule-map.json"
        if cfg_path.exists():
            cfg_bytes = cfg_path.read_bytes()
            try:
                projects = json.loads(cfg_bytes.decode("utf-8-sig")).get("projects") or []
                name_in = projects[0]["name"] if len(projects) == 1 else None
            except (ValueError, AttributeError, KeyError, TypeError, IndexError):
                name_in = None
            out.append("  kept    factory-pins/rule-tags.config.json")
        else:
            cfg_bytes = (json.dumps(rule_tags_config(name, c.root), indent=2) + "\n").encode()
            name_in = name
            writes.append((cfg_path, cfg_bytes))
            out.append("  SEEDED  factory-pins/rule-tags.config.json (rule files: "
                       + (", ".join(f["path"] for f in rule_tags_config(name, c.root)["projects"][0]["files"]) or "none found") + ")")
        if map_path.exists():
            out.append("  kept    factory-pins/rule-map.json")
        elif name_in is None:
            hand.append(f"{cfg_path} does not name exactly one project; emit the map with: python factory-pins/pin-runner.py "
                        "run check-rule-tags -- --config factory-pins/rule-tags.config.json --project <name> --emit-map "
                        "factory-pins/rule-map.json")
        else:
            entry, data = verified["check-rule-tags"]
            body = emit_rule_map(entry, data, cfg_bytes, name_in, c.root)
            writes.append((map_path, body))
            out.append(f"  EMITTED factory-pins/rule-map.json ({len(json.loads(body)['rules'])} rule(s), every kind prose)")

    settings_path = c.root / ".claude" / "settings.json"
    running = c.runner_bytes if vendor else c.vendored.read_bytes()
    sbytes = plan_settings(settings_path, prof.get("claude_hooks", []), reg, out, hand,
                           never_block_ok=NEVER_BLOCK_ARG in running)
    if sbytes is not None:
        writes.append((settings_path, sbytes))

    hp = git(c.root, "rev-parse", "--path-format=absolute", "--git-path", "hooks", timeout=10)
    if hp.returncode != 0:
        raise Refused(f"cannot locate the git hooks directory of {c.root}")
    hooks_dir = Path(hp.stdout.decode().strip())
    common = git(c.root, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=10)
    git_dir = Path(common.stdout.decode().strip()) if common.returncode == 0 else c.root / ".git"
    hooks_ours = any(within(hooks_dir, d) and within(hooks_dir.resolve(), d.resolve()) for d in (c.root, git_dir))
    if hooks_ours and not within(hooks_dir, c.root):
        refuse_links(hooks_dir, git_dir)                # <common-dir>/hooks must not be a link to a shared place
    git_modes: list[Path] = []
    for gh in prof.get("git_hooks", []):
        target = hooks_dir / gh["hook"]
        body = read_blob(c.bus, c.commit, gh["path"])[1]
        lines = body.decode("utf-8", "replace").splitlines()
        if not all(m in lines for m in gh["markers"]):
            raise Refused(f"profile {args.profile!r}: {gh['path']} at {c.commit[:12]} lacks a declared marker line")
        if hooks_ours and not target.exists():
            writes.append((target, body))
            git_modes.append(target)
            out.append(f"  WROTE   git {gh['hook']}: {target} (bus {gh['path']})")
            continue
        # An existing hook (ours, or in a shared core.hooksPath that init never writes) is inspected, read-only: it
        # counts only when it carries every layer marker AND git will run it (executable on POSIX).
        have = target.read_bytes().decode("utf-8", "replace").splitlines() if target.is_file() else []
        missing = [m for m in gh["markers"] if m not in have]
        runnable = target.is_file() and (os.name == "nt" or os.access(target, os.X_OK))
        where = "" if hooks_ours else " (a shared core.hooksPath outside this repository: init writes nothing there)"
        if not missing and runnable:
            out.append(f"  kept    git {gh['hook']}: {target} carries every layer ({', '.join(gh['markers'])}){where}")
        elif not missing:
            hand.append(f"{target} carries every layer but git will not run it: chmod +x {shlex.quote(str(target))}")
        else:
            hand.append(f"{target}{where} {'exists without' if target.is_file() else 'is absent; it needs'} the layer(s) "
                        f"{', '.join(missing)}: chain those layers of the bus {gh['path']} at {c.commit[:12]} into it by hand "
                        "(ahead of any layer that consumes stdin, executable); init re-checks each marker line")

    # Every destination is a plain file inside the place it is promised to be: no link or junction anywhere on its way
    # (an existing .claude/settings.json symlink must not carry a write elsewhere), and no two writes -- or a write and
    # the runner or the pins -- name the same file, compared case-folded (Windows).
    planned = [(p, c.root if within(p, c.root) else hooks_dir) for p, _b in writes]
    planned += [(c.vendored, c.root), (c.pfile, c.root)]
    seen: set[str] = set()
    for p, base in planned:
        refuse_links(p, base)
        key = os.path.normcase(os.path.abspath(p))
        if key in seen:
            raise Refused(f"two planned writes name the same file {p}; nothing written")
        seen.add(key)

    if adopt:
        c.pins["runner"] = runner_record(c.commit, c.runner_bytes)
    c.record_clone()
    pins_changed = json.dumps(c.pins, sort_keys=True) != c.before
    # --- every decision above is made. Write: every file is first staged under a temporary name beside its
    # destination, and only when ALL are staged are they renamed into place (an OSError while staging -- '.claude' is a
    # file, a read-only hooks dir -- removes the staged files and writes nothing). The pins, the decision record, last.
    staged: list[tuple[Path, bytes, int]] = [(c.vendored, c.runner_bytes, 0o644)] if vendor else []
    staged += [(p, b, 0o755 if p in git_modes else 0o644) for p, b in writes]
    if pins_changed or vendor:
        staged.append((c.pfile, (json.dumps(c.pins, indent=2) + "\n").encode("utf-8"), 0o644))
    commit_writes(staged, verify={c.vendored: c.want} if vendor else None)

    print(f"{'NO-OP' if not staged else 'INIT'} profile {args.profile} ({prof['purpose']}) at bus {c.commit[:12]} "
          f"into {c.root} as project {name!r}")
    if vendor or adopt:
        print(f"  {'vendored' if vendor else 'adopted'} runner: {c.vendored} (bus blob {c.pins['runner']['blob'][:12]})")
    print("\n".join(out))
    # The COMMIT line names every file init manages that HEAD does not hold as it is now -- not only this run's writes:
    # a runner adopted after being placed by hand, or a previous run's files never committed, belong in the commit too
    # (without them the pre-push layers cannot read the runner from the pushed commit).
    managed = {c.vendored, c.pfile, *(p for p, _b in writes), fp / "product-throughput-classes.json",
               fp / "rule-tags.config.json", fp / "rule-map.json", c.root / ".claude" / "settings.json",
               *(fp / Path(d).name for d in prof.get("seeds", {})), *(hooks_dir / gh["hook"] for gh in prof.get("git_hooks", []))}
    uncommitted = []
    for p in sorted(managed):
        if not (p.is_file() and within(p, c.root)) or ".git" in Path(os.path.relpath(p, c.root)).parts:
            continue
        rel = Path(os.path.relpath(p, c.root)).as_posix()
        if (git(c.root, "cat-file", "-e", f"HEAD:{rel}", timeout=10).returncode != 0
                or git(c.root, "diff", "--quiet", "HEAD", "--", rel, timeout=10).returncode != 0):
            uncommitted.append(rel)
    if uncommitted:
        print(f"COMMIT (a review decision; init never commits): git add -- {' '.join(map(shlex.quote, uncommitted))}")
    print(f"CHECK: python {c.runner_rel()} verify")
    for h in hand:
        print(f"HAND: {h}")
    if hand:
        print(f"INCOMPLETE: {len(hand)} step(s) left by hand; re-run the same init afterwards (it then exits 0)")
        return EXIT_INCOMPLETE
    return 0


def cmd_list(args: argparse.Namespace, _rest: list[str]) -> int:
    pfile = pins_path(args)
    pins = load_pins(pfile) if pfile.exists() else {"schema": SCHEMA, "tools": {}}
    try:
        bus = find_bus(args, pins, pfile)
        (reg, profiles), src = registry_doc_at(bus, resolve_commit(bus, args.commit)), f"{bus} @ {args.commit}"
    except Unavailable:
        local = HERE / "registry.json"  # run from inside a bus checkout: the data file beside the runner
        if not local.is_file():
            raise
        (reg, profiles), src = parse_registry(local.read_bytes(), str(local)), str(local)
    print(f"# {len(reg)} tool(s) in the registry ({src})")
    for name in sorted(reg):
        t = reg[name]
        print(f"{name}\t{t['interpreter']}\t{t['path']}\t{t['purpose']}")
    print(f"# {len(profiles)} profile(s): pin-runner.py init --profile <name>")
    for name in sorted(profiles):
        print(f"profile {name}\t{','.join(profiles[name]['tools'])}\t{profiles[name]['purpose']}")
    return 0


def runner_line(pins: dict[str, Any], bus: Path | None) -> tuple[bool, str]:
    r = pins.get("runner")
    data = own_bytes()
    if not isinstance(r, dict):
        return False, "runner: no 'runner' record in the pins (run adopt-runner)"
    if blob_id(data) != r.get("blob") or hashlib.sha256(data).hexdigest() != r.get("sha256"):
        return False, f"runner: this vendored pin-runner.py differs from the recorded bus blob {str(r.get('blob'))[:12]}"
    if bus is None:
        return True, f"runner: vendored copy = bus blob {r['blob'][:12]}"
    if not HEX40.match(str(r.get("commit"))) or blob_at(bus, r["commit"], RUNNER_BUS_PATH) != r["blob"]:
        return False, (f"runner: the record names bus {str(r.get('commit'))[:12]}:{RUNNER_BUS_PATH}, which is not blob "
                       f"{str(r.get('blob'))[:12]} in {bus}: not a bus release (or fetch the bus)")
    moved = [f"{ref} {got[:8]}" for ref in ("HEAD", "origin/master")
             if (got := blob_at(bus, ref, RUNNER_BUS_PATH)) != r["blob"]]
    return True, (f"runner: vendored copy = bus blob {r['blob'][:12]}"
                  + (f"  *** STALE *** bus moved: {', '.join(moved)}" if moved else "  (current)"))


def cmd_verify(args: argparse.Namespace, _rest: list[str]) -> int:
    pins = load_pins(pins_path(args))
    bus = find_bus(args, pins, pins_path(args))
    bad = 0
    ok, line = runner_line(pins, bus)
    bad += not ok
    print(("VERIFIED " if ok else "DRIFT ") + line)
    for name, t in sorted(pins["tools"].items()):
        if name.startswith("_"):
            continue
        try:
            pinned_bytes(bus, name, t)
            if blob_at(bus, t["commit"], t["path"]) != t["blob"]:
                raise Refused(f"{name}: commit {t['commit'][:12]} does not carry blob {t['blob'][:12]} at {t['path']}")
            print(f"VERIFIED {name}: {t['path']} blob {t['blob'][:12]} exact in {bus}")
        except (Refused, Unavailable) as exc:
            bad += 1
            print(f"DRIFT {exc}")
    return EXIT_REFUSED if bad else 0


def cmd_status(args: argparse.Namespace, _rest: list[str]) -> int:
    pins = load_pins(pins_path(args))
    try:
        bus: Path | None = find_bus(args, pins, pins_path(args))
    except Unavailable as exc:
        bus = None
        print(f"[factory-pins] bus clone UNAVAILABLE: {exc}")
    print(f"[factory-pins] {runner_line(pins, bus)[1]}")
    stale = 0
    for name, t in sorted(pins["tools"].items()):
        if name.startswith("_"):
            continue
        if bus is None:
            print(f"[factory-pins] {name}: pinned {t['blob'][:12]} (staleness UNKNOWN)")
            continue
        moved = [f"{ref} {got[:8]}" for ref in ("HEAD", "origin/master")
                 if (got := blob_at(bus, ref, t["path"])) != t["blob"]]
        stale += bool(moved)
        print(f"[factory-pins] {name}: pinned {t['blob'][:12]} @ bus {t['commit'][:8]} "
              + (f"*** STALE *** {t['path']} moved: {', '.join(moved)}. The reviewed copy still runs; review "
                 f"`git -C <bus> diff {t['commit'][:12]}..origin/master -- {t['path']}` then re-pin." if moved else "(current)"))
    if stale:
        print(f"[factory-pins] {stale} stale pin(s). Never auto-upgraded: re-pin is a reviewed commit.")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    ap.add_argument("--pins", help=f"the pins file (default: {PINS_NAME} beside this runner)")
    ap.add_argument("--pins-ref", help="read the pins file from this git ref of the consumer instead of the worktree")
    ap.add_argument("--bus", help="the local bus clone (data source for pinned blobs)")
    ap.add_argument("--never-block", action="store_true",
                    help="for hooks that must never block (Stop, SessionStart): a REFUSED or UNAVAILABLE runner exits 1 "
                         "(a loud, non-blocking hook error) instead of 2/3; the tool's own exit still passes through")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run <tool> [--] [args...]", add_help=False)
    p = sub.add_parser("pin", allow_abbrev=False)
    p.add_argument("tool")
    p.add_argument("--commit", required=True)
    p.add_argument("--reviewer", required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--path")
    p.add_argument("--date")
    p.add_argument("--interpreter", choices=INTERPRETERS)
    p.add_argument("--with", dest="with_", action="append", metavar="TOOL",
                   help="a pinned tool whose verified bytes run beside this one (repeat; a pinned group)")
    a = sub.add_parser("adopt-runner", allow_abbrev=False)
    a.add_argument("--commit", required=True)
    sub.add_parser("verify")
    sub.add_parser("status")
    i = sub.add_parser("install", allow_abbrev=False)
    i.add_argument("tool")
    i.add_argument("--commit", required=True)
    i.add_argument("--reviewer", required=True)
    i.add_argument("--reason", required=True)
    i.add_argument("--project-root")
    i.add_argument("--date")
    i.add_argument("--bus", default=argparse.SUPPRESS)  # also accepted after the subcommand
    n = sub.add_parser("init", allow_abbrev=False)
    n.add_argument("--profile", required=True)
    n.add_argument("--commit", required=True)
    n.add_argument("--reviewer", required=True)
    n.add_argument("--reason", required=True)
    n.add_argument("--project-root")
    n.add_argument("--name", help="the project's name in the seeded configs (default: the folder name, slugged)")
    n.add_argument("--date")
    n.add_argument("--bus", default=argparse.SUPPRESS)
    n.add_argument("--pins", default=argparse.SUPPRESS)  # accepted only as the canonical place (cmd_init)
    li = sub.add_parser("list", allow_abbrev=False)
    li.add_argument("--commit", default="HEAD")
    li.add_argument("--bus", default=argparse.SUPPRESS)
    argv = sys.argv[1:] if argv is None else argv
    cmds_names = ("run", "pin", "adopt-runner", "verify", "status", "install", "init", "list")
    # Everything after `run` belongs to the tool, verbatim (argparse must not eat the tool's flags).
    first = next((i for i, a in enumerate(argv) if a in cmds_names), None)
    if first is not None and argv[first] == "run":
        i = first
        args, rest = ap.parse_args(argv[:i + 1]), argv[i + 1:]
    else:
        args, rest = ap.parse_args(argv), []
    cmds = {"run": cmd_run, "pin": cmd_pin, "adopt-runner": cmd_adopt_runner, "verify": cmd_verify, "status": cmd_status,
            "install": cmd_install, "init": cmd_init, "list": cmd_list}
    try:
        return cmds[args.cmd](args, rest)
    except Refused as exc:
        print(f"[factory-pins] *** REFUSED *** {exc}; nothing executed", file=sys.stderr)
        return 1 if args.never_block else EXIT_REFUSED
    except Unavailable as exc:
        print(f"[factory-pins] *** UNAVAILABLE *** {exc}; nothing executed", file=sys.stderr)
        return 1 if args.never_block else EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main())
