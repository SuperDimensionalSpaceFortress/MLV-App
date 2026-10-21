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
    pin-runner.py list [--commit REF]              the registry: tool, interpreter, bus path, purpose

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
Stdlib only; Windows and POSIX.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import re
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
EXIT_REFUSED, EXIT_UNAVAILABLE = 2, 3
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
TOOL_KEYS = {"path", "commit", "blob", "bytes", "sha256", "interpreter", "reviewer", "reason", "pinned_utc"}
INTERPRETERS = ("python", "node")
REGISTRY_BUS_PATH = "tools/factory/registry.json"
REGISTRY_SCHEMA = "factory-registry.v1"
REGISTRY_KEYS = {"path", "interpreter", "purpose"}
MAX_PINS_BYTES, MAX_TOOL_BYTES = 256 * 1024, 4 * 1024 * 1024


class Refused(Exception):
    pass


class Unavailable(Exception):
    pass


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
    try:
        pins = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_no_dupes,
                          parse_constant=lambda c: (_ for _ in ()).throw(Refused("pins: non-finite number")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise Refused(f"pins: invalid JSON ({exc.__class__.__name__})") from exc
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
    pins = load_pins(pins_path(args), read_pins_ref(args))
    t = tool_pin(pins, name)
    data = tool_bytes(args, pins, name, t)
    # A group (`with`) loads its members by file name beside itself, so it never runs in-process: there its
    # __file__ is the bus path, and a sibling load would read the CALLER's tree. Every member is verified first.
    members = [(m, pins["tools"][m], tool_bytes(args, pins, m, pins["tools"][m])) for m in t.get("with", [])]
    if t["interpreter"] == "python" and sys.flags.isolated and not members:
        return run_in_process(name, t, data, tool_args)
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
            return subprocess.run([*argv, *tool_args], env=child_env()).returncode
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
    """tools/factory/registry.json (DATA read from a reviewed commit, never executed):
    {schema, tools: {name: {path, interpreter, purpose}}}."""
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
    return tools


def registry_at(bus: Path, ref: str) -> dict[str, dict[str, Any]]:
    try:
        _want, data = read_blob(bus, ref, REGISTRY_BUS_PATH)
    except Refused as exc:
        if " has no " in str(exc):
            raise Refused(f"{ref[:12]} has no {REGISTRY_BUS_PATH}; pin by hand with `pin <tool> --path ...`") from None
        raise
    return load_registry(data, f"{ref[:12]}:{REGISTRY_BUS_PATH}")


def runner_record(commit: str, data: bytes) -> dict[str, Any]:
    return {"commit": commit, "path": RUNNER_BUS_PATH, "blob": blob_id(data), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


def short(v: Any) -> str:
    return v[:12] if isinstance(v, str) and HEX40.match(v) else str(v)


def cmd_install(args: argparse.Namespace, _rest: list[str]) -> int:
    """Make a project a consumer of <tool>: vendor + adopt the runner if absent, pin the tool. Writes data
    and one vendored copy of the REVIEWED runner blob; executes nothing (`run` is the only executor)."""
    if args.tool == "pin-runner":
        raise Refused("the runner is vendored by install itself, not pinned as a tool")
    root = Path(args.project_root).resolve() if args.project_root else Path.cwd().resolve()
    pfile = Path(args.pins).resolve() if args.pins else root / "factory-pins" / PINS_NAME
    if args.bus:
        args.bus = str(Path(args.bus).resolve())  # a command-line clone is cwd-relative, not consumer-relative
    pins = load_pins(pfile) if pfile.exists() else {"schema": SCHEMA, "tools": {}}
    before = json.dumps(pins, sort_keys=True)
    bus = find_bus(args, pins, pfile)
    commit = resolve_commit(bus, args.commit)
    # Law 1 on the installer itself: before anything is read from the registry or written, the bytes RUNNING
    # this install and the bytes it will vendor must both be the reviewed blob at --commit (git's own object id).
    want, runner_bytes = read_blob(bus, commit, RUNNER_BUS_PATH)
    if blob_id(own_bytes()) != want:
        raise Refused(f"this running pin-runner.py is not byte-identical to {commit[:12]}:{RUNNER_BUS_PATH}; install only runs "
                      f"from the reviewed blob: git -C <bus> show {commit[:12]}:{RUNNER_BUS_PATH} > factory-pins/pin-runner.py, "
                      f"then python factory-pins/pin-runner.py install ...")
    reg = registry_at(bus, commit)
    if args.tool not in reg:
        raise Refused(f"unknown tool {args.tool!r}; the registry at {commit[:12]} lists: {', '.join(sorted(reg)) or '(none)'}")
    r = reg[args.tool]
    vendored = pfile.parent / "pin-runner.py"
    have = vendored.read_bytes().replace(b"\r\n", b"\n") if vendored.is_file() else None
    rec = pins.get("runner")
    vendor = adopt = False
    if have is None:
        vendor = adopt = True
    elif isinstance(rec, dict) and rec.get("blob") == blob_id(have) and rec.get("sha256") == hashlib.sha256(have).hexdigest():
        pass  # already adopted, maybe an older release than <commit>: `status` reports STALE; install never overwrites it
    elif have == runner_bytes:
        adopt = True
    else:
        raise Refused(f"{vendored} is neither adopted nor byte-identical to the bus runner at {commit[:12]}; delete it to "
                      f"let install vendor a reviewed copy, or adopt-runner it at the commit it equals")
    old = pins["tools"].get(args.tool, {})
    entry = pin_entry(bus, commit, r["path"], old, r["interpreter"], args.reviewer, args.reason, args.date)
    if r.get("with"):
        missing = [m for m in r["with"] if m not in pins["tools"]]
        if missing:
            raise Refused(f"{args.tool} runs with {', '.join(missing)}: install {' and '.join(missing)} first")
        entry["with"] = list(r["with"])
    same = all(old.get(k) == entry.get(k) for k in ("path", "commit", "blob", "interpreter", "with"))
    if same:
        entry = old
    validate_tool(args.tool, entry)
    validate_groups({**pins["tools"], args.tool: entry})
    pfile.parent.mkdir(parents=True, exist_ok=True)
    if vendor:
        vendored.write_bytes(runner_bytes)
        wrote = vendored.read_bytes().replace(bytes([13, 10]), bytes([10]))
        if blob_id(wrote) != want or hashlib.sha256(wrote).hexdigest() != hashlib.sha256(runner_bytes).hexdigest():
            vendored.unlink(missing_ok=True)
            raise Refused(f"{vendored} does not hold the reviewed runner after writing (object {want[:12]}); removed")
    if adopt:
        pins["runner"] = runner_record(commit, runner_bytes)
    if args.bus:
        try:
            rel = Path(os.path.relpath(bus, consumer_root(pfile))).as_posix()
        except ValueError:  # another drive: only an absolute path can name it
            rel = bus.as_posix()
        listed = pins.get("bus_clone", [])
        listed = [listed] if isinstance(listed, str) else list(listed)
        if rel not in listed:
            pins["bus_clone"] = [rel, *listed]
    pins["tools"][args.tool] = entry
    changed = vendor or json.dumps(pins, sort_keys=True) != before
    if changed:
        pfile.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8", newline="\n")
    runner_rel = (vendored.relative_to(root) if vendored.is_relative_to(root) else vendored).as_posix()
    if not changed:
        print(f"NO-OP: {args.tool} already pinned at {commit[:12]} (blob {entry['blob'][:12]}) in {pfile}")
    else:
        print(f"{'RE-PINNED' if old and not same else 'INSTALLED'} {args.tool}: {r['path']} blob {entry['blob'][:12]} "
              f"at bus {commit[:12]} -> {pfile}")
        if vendor or adopt:
            print(f"  {'vendored' if vendor else 'adopted'} runner: {vendored} (bus blob {pins['runner']['blob'][:12]})")
        if old and not same:
            for k in ("path", "commit", "blob", "bytes", "sha256", "interpreter", "reviewer", "reason", "pinned_utc"):
                if old.get(k) != entry[k]:
                    print(f"  ~ {k}: {short(old.get(k))} -> {short(entry[k])}")
    print(f"WIRE: python {runner_rel} run {args.tool} -- <tool args>   (a hook should use `python -I`)")
    print(f"CHECK: python {runner_rel} verify")
    return 0


def cmd_list(args: argparse.Namespace, _rest: list[str]) -> int:
    pfile = pins_path(args)
    pins = load_pins(pfile) if pfile.exists() else {"schema": SCHEMA, "tools": {}}
    try:
        bus = find_bus(args, pins, pfile)
        reg, src = registry_at(bus, resolve_commit(bus, args.commit)), f"{bus} @ {args.commit}"
    except Unavailable:
        local = HERE / "registry.json"  # run from inside a bus checkout: the data file beside the runner
        if not local.is_file():
            raise
        reg, src = load_registry(local.read_bytes(), str(local)), str(local)
    print(f"# {len(reg)} tool(s) in the registry ({src})")
    for name in sorted(reg):
        t = reg[name]
        print(f"{name}\t{t['interpreter']}\t{t['path']}\t{t['purpose']}")
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
    li = sub.add_parser("list", allow_abbrev=False)
    li.add_argument("--commit", default="HEAD")
    li.add_argument("--bus", default=argparse.SUPPRESS)
    argv = sys.argv[1:] if argv is None else argv
    cmds_names = ("run", "pin", "adopt-runner", "verify", "status", "install", "list")
    # Everything after `run` belongs to the tool, verbatim (argparse must not eat the tool's flags).
    first = next((i for i, a in enumerate(argv) if a in cmds_names), None)
    if first is not None and argv[first] == "run":
        i = first
        args, rest = ap.parse_args(argv[:i + 1]), argv[i + 1:]
    else:
        args, rest = ap.parse_args(argv), []
    cmds = {"run": cmd_run, "pin": cmd_pin, "adopt-runner": cmd_adopt_runner, "verify": cmd_verify, "status": cmd_status,
            "install": cmd_install, "list": cmd_list}
    try:
        return cmds[args.cmd](args, rest)
    except Refused as exc:
        print(f"[factory-pins] *** REFUSED *** {exc}; nothing executed", file=sys.stderr)
        return EXIT_REFUSED
    except Unavailable as exc:
        print(f"[factory-pins] *** UNAVAILABLE *** {exc}; nothing executed", file=sys.stderr)
        return EXIT_UNAVAILABLE


if __name__ == "__main__":
    sys.exit(main())
