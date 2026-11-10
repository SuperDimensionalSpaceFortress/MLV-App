"""Guard for MLV-LANE-CONSOLE-HIDDEN-1: no console program may be spawned without a hidden-window flag.

A console program started without CREATE_NO_WINDOW (Python) or ``-WindowStyle Hidden`` (Start-Process)
by a parent that owns no console opens a VISIBLE window on the owner's desktop. Roughly 15 such
``...pythoncore-3.14-64\\python.exe`` windows were recorded on 2026-11-09 (ledger QUEUED 2026-11-09T23:14:17Z).

Three pins:
  1. ``hidden_spawn.no_window_flags`` sets CREATE_NO_WINDOW on Windows, ORs an existing flag, drops CREATE_NEW_CONSOLE
     (Windows ignores CREATE_NO_WINDOW when that bit is also set), and is a no-op elsewhere.
  2. Every ``subprocess.run/Popen/check_output/check_call/call`` and ``asyncio.create_subprocess_exec/shell`` in a
     non-test tool module under tools/ carries a ``creationflags=`` keyword whose value is built only from accepted parts
     (see ``_provably_hidden``); the scan evaluates nothing. ``os.system``, ``os.popen``, ``os.spawn*``,
     ``subprocess.getoutput`` and ``subprocess.getstatusoutput`` cannot take a window flag, so any call is REPORTED.
     The set is derived by glob, so a NEW unflagged spawn fails here. HIGH-RISK path classes (key3) and the frozen
     in-tree agent-bridge package are listed in EXCLUDED_DIRS / EXCLUDED_FILES and are left to their own cards.
  3. The named test fixtures that the 2026-11-09 evidence points at (the spliced-body-scan runner and the
     host-load-gate fixtures) spawn hidden, and every ``Start-Process`` / ``saps`` / ``start`` statement in the gate's
     generated run.ps1 bodies carries its own ``-WindowStyle Hidden``, whatever it starts (powershell, pwsh, cmd.exe, a
     variable ``-FilePath``): the launch is judged, never its target.

What the scan follows: ``import subprocess [as x]``, ``from subprocess import run [as r]``, ``from ... import *``, plain-name
aliases (``sp = subprocess``, ``r = subprocess.run``, a default argument), ``asyncio.subprocess``, and a module obtained by
``importlib.import_module('subprocess')`` / ``__import__('subprocess')`` (also through ``from importlib import import_module as im``)
when the name is a string literal or a name bound once to one. A dynamic import whose name cannot be resolved is REPORTED, never
treated as clean. Anywhere else a spawn module or API appears (a container, call argument, return, unpacking, base class,
``__dict__`` or other dunder/private attribute, ``getattr`` of a non-constant, ``__annotations__``, ``get_type_hints``,
``globals()``) it is REPORTED, because the call cannot be tied back to it by syntax. An assignment expression (``:=``) is followed
only as a statement (``(r := subprocess.run)``); used in place or inside a larger expression it is REPORTED. An attribute stored
or deleted on a spawn module (``subprocess.CREATE_NO_WINDOW = 0``, ``os.name = ...``) is REPORTED. A submodule of asyncio is the
module it re-exports (``asyncio.base_events.subprocess`` is ``subprocess``). A name that is the string of an event-loop spawn
method (``getattr(loop, 'subprocess_exec')``, ``attrgetter``) is REPORTED. The standard ``_NO_WINDOW`` definition is trusted only
when ``os``, ``subprocess`` and ``getattr`` each come from exactly one ordinary ``import``.

Remaining limits, every one named (r5 rewrite: the scan is syntactic and static, so these are what it does NOT see):
  * Python source built or loaded at run time: ``exec``, ``eval``, ``compile``, ``runpy``, ``importlib.util`` loaders,
    ``sys.modules`` lookups, ``importlib.reload``, ``pickle`` / ``marshal``.
  * A process started without ``subprocess`` / ``asyncio`` / ``os``: ``ctypes`` / ``win32process`` CreateProcess,
    ``multiprocessing``, ``_winapi``, ``concurrent.futures`` process pools, ``pty``, ``os.startfile``.
  * A method name assembled at run time (``getattr(loop, 'subprocess_' + kind)``) or held outside a string (``match`` patterns);
    a spawn name reached by ``getattr`` on an object that is not a tracked module.
  * Hostile source that rebinds builtins or ``__import__``, or mutates a module through a path this scan does not parse. The
    card's promise is that no ACCIDENTAL unflagged spawn lands, not that deliberate evasion is impossible.
  * Any non-Python launcher: a ``.ps1`` / ``.cmd`` / ``.bat`` file that starts a console program is not read at all, and a
    PowerShell script is read only inside a Python string of the one fixture file. There only ``Start-Process`` and its aliases
    ``start`` / ``saps`` (also quoted as ``& 'Start-Process'`` or dot-sourced) are recognised as the launch; ``[Diagnostics.Process]::Start``,
    ``Invoke-Item``, ``Invoke-Expression`` over a built string, ``cmd /c start``, ``& (Get-Command ...)`` and a command name held in a
    variable are not. A Start-Process written in a string that is assembled any other way than a literal, a ``+`` chain, an f-string,
    ``%``, ``.format`` or ``.join`` (a file read at run time, ``chr()``) is not read. A built string is REPORTED only when a literal piece
    reads as ``Start-`` or as ``start`` / ``saps`` where a command word can stand.
  * Whether a child of a hidden spawn stays hidden, and ``tools/agent-bridge/**`` plus the other excluded paths (EXCLUDED_DIRS /
    EXCLUDED_FILES), which are left to their own cards.
  * A name bound to two different spawn modules or APIs in one file is REPORTED rather than followed, since the alias map is
    file-wide, not scoped. A name that two different ``import`` statements bind is never trusted as a flag or in the standard
    definition, so a call that depends on it is REPORTED.
"""
from __future__ import annotations

import ast
import asyncio
import pkgutil
import posixpath
import re
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tools.repo_hygiene import hidden_spawn

ROOT = Path(__file__).resolve().parents[2]
SPAWN_ATTRS = {"run", "Popen", "check_output", "check_call", "call"}
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_CONSOLE = 0x00000010  # with this bit set Windows ignores CREATE_NO_WINDOW

# Key3 HIGH-RISK classes (and the frozen in-tree agent-bridge) stay out of this card's diff; each is an OPEN line.
# Directories end in "/" and match by prefix; files are compared whole against the normalized path, so a path that
# merely STARTS with an excluded file name (doctrine_outbox.py.bak, brokered_closeout_helper.py) is still scanned.
EXCLUDED_DIRS = (
    "tools/agent-bridge/",
    "tools/hooks/",
    "tools/gates/",
    "tools/profiling/bachelor/",
    "tools/profiling/dual-venue/",
    "tools/gpu/backend/",
)
EXCLUDED_FILES = frozenset({
    "tools/repo_hygiene/brokered_closeout.py",
    # The bus candidate: tests/coordination/test_doctrine_outbox_hooks.py pins its LF digest in doctrine-outbox.config.json,
    # and the fleet steward lands the file on the bus. Its unflagged spawns are an OPEN line for that steward, not edited here.
    "tools/coordination/doctrine_outbox.py",
})
EXCLUDED_NAME_RE = re.compile(r"^tools/coordination/merge[^/]*$")

# Test modules whose spawns must be hidden (the evidence chain and the fixtures named by the card).
NAMED_TEST_MODULES = (
    "tools/repo_hygiene/test_attr3_spliced_body_scan.py",
    "tools/repo_hygiene/test_playback_host_load_gate.py",
)


def _normalize(rel: str) -> str:
    return posixpath.normpath(rel.replace("\\", "/"))


def _is_excluded(rel: str) -> bool:
    norm = _normalize(rel)
    return norm in EXCLUDED_FILES or norm.startswith(EXCLUDED_DIRS) or bool(EXCLUDED_NAME_RE.match(norm))


def _tracked_py_files(root: Path = ROOT) -> list[str]:
    # -z: with core.quotepath on, a non-ASCII path comes back quoted and escaped, and open() then names nothing on disk.
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--", "tools/*.py"],
        capture_output=True, check=True, timeout=60,
        creationflags=hidden_spawn.no_window_flags(),
    )
    return sorted(part.decode("utf-8", "surrogateescape") for part in proc.stdout.split(b"\0") if part)


def _tool_modules() -> list[str]:
    files = []
    for rel in _tracked_py_files():
        name = rel.rsplit("/", 1)[-1]
        if name.startswith("test_") or name == "conftest.py":
            continue
        if _is_excluded(rel):
            continue
        files.append(rel)
    return files


_HIDDEN_NAMES = frozenset({"CREATE_NO_WINDOW", "_NO_WINDOW", "_WINDOWS_NO_WINDOW"})
_HELPER_NAME = "no_window_flags"
_TRUSTED_NAMES = _HIDDEN_NAMES | {_HELPER_NAME}
_MODULE_ALIASES = {"nt": "os", "posix": "os"}  # the C modules behind os: nt.system IS os.system
_OWN_ROOTS = ("tools", "hidden_spawn", "core")  # an import rooted here is this repo's own package, not a stranger's module

# Modules whose spawn APIs the scan follows. SPAWN: needs an accepted creationflags= operand (asyncio passes **kwargs on to Popen).
# FORBIDDEN: cannot take a window flag at all, so any call in a scanned tool module is reported.
_SPAWN_APIS = {
    "subprocess": frozenset(SPAWN_ATTRS),
    "asyncio": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
    "asyncio.subprocess": frozenset({"create_subprocess_exec", "create_subprocess_shell"}),
}
_FORBIDDEN_APIS = {
    "subprocess": frozenset({"getoutput", "getstatusoutput"}),
    "os": frozenset({"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                     "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe"}),
}
_LOOP_SPAWN_METHODS = frozenset({"subprocess_exec", "subprocess_shell"})  # asyncio event-loop methods; they forward **kwargs to Popen
_TRACKED_MODULES = frozenset({"subprocess", "os", "asyncio", "asyncio.subprocess", "asyncio.*"})
# subprocess and asyncio.subprocess are used for little besides spawning, so only constants and exception/data types may be read
# off them; os and asyncio are everywhere, so any non-dunder attribute that is not a spawn API may be read off those.
_STRICT_MODULES = frozenset({"subprocess", "asyncio.subprocess"})
_BENIGN_CONSTANT_RE = re.compile(r"[A-Z][A-Z0-9_]*")
_BENIGN_NAMES = frozenset({"CompletedProcess", "CalledProcessError", "TimeoutExpired", "SubprocessError", "STARTUPINFO",
                           "list2cmdline", "Process"})
_DUNDER_OK = frozenset({"__name__", "__file__", "__doc__"})
_IMPORT_FUNCS = frozenset({"import_module", "__import__"})
# Ways to reach a module's contents or a function's annotations without a followable reference.
_REFLECTION = frozenset({"__annotations__", "__builtins__", "get_type_hints", "get_annotations", "globals", "locals"})
_CLASS_SCOPE_IMPORT_ROOTS = frozenset({"subprocess", "os", "asyncio", "nt", "posix", "importlib", "builtins"})
_MUTATORS = frozenset({"setattr", "delattr"})
_MUTATING_ATTRS = frozenset({"__dict__", "__setattr__", "__delattr__"})
# Submodules of asyncio (read off the installed package, plus the ones that re-export subprocess/os): ``asyncio.base_events.subprocess``
# IS the subprocess module, so a submodule reference stays the tracked module "asyncio" and ``.subprocess`` / ``.os`` off it is followed.
_ASYNCIO_SUBMODULES = frozenset({m.name for m in pkgutil.iter_modules(asyncio.__path__)} | {
    "base_events", "base_subprocess", "unix_events", "windows_events", "proactor_events", "selector_events", "events"}) - {"subprocess"}
# "asyncio.*" is any asyncio submodule object. `.subprocess` / `.os` off a submodule (asyncio.base_events.subprocess) are the REAL modules,
# but `.subprocess` off the asyncio package is asyncio's own wrapper module.
_SUBMODULE_REEXPORTS = {"subprocess": "subprocess", "os": "os", "nt": "os", "posix": "os"}
_PACKAGE_REEXPORTS = {"subprocess": "asyncio.subprocess", "os": "os", "nt": "os", "posix": "os"}
_LOOP_METHOD_NAME_RE = re.compile(r"(?:\w+\.)*subprocess_(?:exec|shell)")


def _label(expr: ast.AST) -> str:
    try:
        return ast.unparse(expr)[:60]
    except Exception:  # noqa: BLE001 - a display string only; never part of a verdict
        return "<expression>"


def _tracked(module: str | None) -> frozenset:
    if module is None:
        return frozenset()
    return _SPAWN_APIS.get(module, frozenset()) | _FORBIDDEN_APIS.get(module, frozenset())


def _attr_ok(module: str, attr: str) -> bool:
    """May ``attr`` be read off the tracked module without being followed? Spawn APIs are judged at their own node."""
    if attr in _tracked(module):
        return True
    if module in _STRICT_MODULES:
        return bool(_BENIGN_CONSTANT_RE.fullmatch(attr)) or attr in _BENIGN_NAMES
    return not (attr.startswith("__") and attr.endswith("__")) or attr in _DUNDER_OK


def _collect_binders(tree: ast.AST) -> tuple[dict[str, list], set[str], set[str], dict[str, set[str]]]:
    """(name -> every value bound to it, None for a binding with no plain value; names bound by imports; own-package import names;
    name -> every import source it is bound from, ``import x`` and ``from m import x`` alike)."""
    binders: dict[str, list[ast.AST | None]] = {}
    imports: set[str] = set()
    own: set[str] = set()
    import_src: dict[str, set[str]] = {}
    plain: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    plain.add(id(t))
                    binders.setdefault(t.id, []).append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            plain.add(id(node.target))
            binders.setdefault(node.target.id, []).append(node.value)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)) and id(node) not in plain:
            binders.setdefault(node.id, []).append(None)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and node.attr in _TRUSTED_NAMES:
            binders.setdefault(node.attr, []).append(None)  # hidden_spawn._NO_WINDOW = 0
        elif isinstance(node, ast.arg):
            binders.setdefault(node.arg, []).append(None)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            binders.setdefault(node.name, []).append(None)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            binders.setdefault(node.name, []).append(None)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            binders.setdefault(node.name, []).append(None)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            binders.setdefault(node.rest, []).append(None)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            own_package = isinstance(node, ast.ImportFrom) and (
                node.level > 0 or (node.module or "").split(".")[0] in _OWN_ROOTS)
            for a in node.names:
                name = (a.asname or a.name).split(".")[0]
                if isinstance(node, ast.Import):
                    import_src.setdefault(name, set()).add(a.name if a.asname else name)
                    if a.name.split(".")[0] in _OWN_ROOTS:
                        own.add(name)
                else:  # `from sys import implementation as os` puts a second object under the name `os`
                    # keyed on the module's last component: `from .hidden_spawn import x` / `from hidden_spawn import x` (a try/except
                    # ImportError pair) are one source, `from sys import implementation as os` is not `import os`
                    import_src.setdefault(name, set()).add(f"from {(node.module or '').rsplit('.', 1)[-1]} import {a.name}")
                    if own_package:
                        own.add(name)
                if name in _TRUSTED_NAMES and not (own_package and a.asname in (None, a.name)):
                    binders.setdefault(name, []).append(None)  # another module's object, or a renamed import
                else:
                    imports.add(name)
    for name, sources in import_src.items():
        if len(sources) > 1:  # `import os` then `import posixpath as os`: two objects under one name
            binders.setdefault(name, []).append(None)
    return binders, imports, own, import_src


def _string_consts(binders: dict[str, list]) -> dict[str, str]:
    """Names whose ONLY binding is one plain string literal: ``TEST_MODULE = "pkg.mod"``."""
    return {name: values[0].value for name, values in binders.items()
            if len(values) == 1 and isinstance(values[0], ast.Constant) and isinstance(values[0].value, str)}


class _Facts:
    """Names bound to the tracked modules and to their spawn APIs (direct bindings only; everything else is a reference violation).

    A name bound to two DIFFERENT tracked modules or APIs (``import os as m`` in one function, ``import subprocess as m`` in
    another) is ``ambiguous`` and every use of it is reported: the map is file-wide, not scoped. Resolution is a worklist over
    the plain-name bindings, so a long chain of aliases costs time linear in the source.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.binders, self.imports, self.own, self.import_src = _collect_binders(tree)
        self.consts = _string_consts(self.binders)
        self.modules: dict[str, str] = {}
        self.funcs: dict[str, tuple[str, str]] = {}
        self.ambiguous: set[str] = set()
        self.importers: dict[str, str] = {"__import__": "__import__"}  # names that import a module by string -> which kind
        self.helper_modules: dict[str, str] = {}  # names bound to importlib / builtins
        for node in ast.walk(tree):
            self._seed(node)
        pairs = [(name, value) for node in ast.walk(tree) for name, value in self._direct(node)]
        by_source: dict[str, list[tuple[str, ast.AST]]] = {}
        for pair in pairs:
            for sub in ast.walk(pair[1]):
                if isinstance(sub, ast.Name):
                    by_source.setdefault(sub.id, []).append(pair)
        queue = list(pairs)
        while queue:
            name, value = queue.pop()
            for learned in self._learn(name, value):  # each name is learned a bounded number of times, so this ends
                queue.extend(by_source.get(learned, ()))

    def _bind_module(self, name: str, module: str) -> bool:
        if name in self.funcs:
            self.ambiguous.add(name)
        if name in self.modules:
            if self.modules[name] != module:
                self.ambiguous.add(name)
            return False
        self.modules[name] = module
        return True

    def _bind_func(self, name: str, target: tuple[str, str]) -> bool:
        if name in self.modules:
            self.ambiguous.add(name)
        if name in self.funcs:
            if self.funcs[name] != target:
                self.ambiguous.add(name)
            return False
        self.funcs[name] = target
        return True

    def _seed(self, node: ast.AST) -> None:
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if a.name in _MODULE_ALIASES:
                    self._bind_module(a.asname or a.name, _MODULE_ALIASES[a.name])
                if a.name in ("importlib", "builtins") or (a.asname is None and root in ("importlib", "builtins")):
                    self.helper_modules.setdefault(a.asname or root, root)
                if a.asname and a.name in _TRACKED_MODULES:
                    self._bind_module(a.asname, a.name)
                elif a.asname and root == "asyncio" and a.name.split(".")[1] in _ASYNCIO_SUBMODULES:  # import asyncio.base_events as be
                    self._bind_module(a.asname, "asyncio.*")
                elif not a.asname and root in ("subprocess", "os", "asyncio"):
                    self._bind_module(root, root)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            origin = _MODULE_ALIASES.get(node.module, node.module)
            if node.module in ("importlib", "builtins"):
                for a in node.names:
                    if a.name in _IMPORT_FUNCS:
                        self.importers.setdefault(a.asname or a.name, a.name)
                    elif a.name == "*":
                        self.importers.setdefault("import_module", "import_module")
            if origin in _TRACKED_MODULES:
                for a in node.names:
                    if a.name == "*":
                        for attr in _tracked(origin):
                            self._bind_func(attr, (origin, attr))
                    elif a.name in _tracked(origin):
                        self._bind_func(a.asname or a.name, (origin, a.name))
                    elif origin == "asyncio" and a.name == "subprocess":
                        self._bind_module(a.asname or a.name, "asyncio.subprocess")
                    elif origin == "asyncio" and a.name in _ASYNCIO_SUBMODULES:  # `from asyncio import base_events as be`
                        self._bind_module(a.asname or a.name, "asyncio.*")
                    elif a.name in ("os", "subprocess", "asyncio"):  # `from subprocess import os`: the module itself
                        self._bind_module(a.asname or a.name, a.name)
            parts = node.module.split(".")
            if parts[0] == "asyncio" and len(parts) > 1 and parts[1] in _ASYNCIO_SUBMODULES:  # `from asyncio.base_events import subprocess`
                for a in node.names:
                    for reexported in (_SUBMODULE_REEXPORTS if a.name == "*" else [a.name] if a.name in _SUBMODULE_REEXPORTS else []):
                        self._bind_module(reexported if a.name == "*" else a.asname or a.name, _SUBMODULE_REEXPORTS[reexported])

    def _learn(self, name: str, value: ast.AST) -> list[str]:
        learned: list[str] = []
        module = self.module_of(value)
        if module is not None:
            if self._bind_module(name, module):
                learned.append(name)
        elif isinstance(value, ast.Name):
            if value.id in self.funcs and self._bind_func(name, self.funcs[value.id]):
                learned.append(name)
            if value.id in self.importers and name not in self.importers:
                self.importers[name] = self.importers[value.id]
                learned.append(name)
            if value.id in self.helper_modules and name not in self.helper_modules:
                self.helper_modules[name] = self.helper_modules[value.id]
                learned.append(name)
        elif isinstance(value, ast.Attribute):
            base = self.module_of(value.value)
            if base is not None and value.attr in _tracked(base):
                if self._bind_func(name, (base, value.attr)):
                    learned.append(name)
            elif (isinstance(value.value, ast.Name) and value.value.id in self.helper_modules
                  and value.attr in _IMPORT_FUNCS and name not in self.importers):
                self.importers[name] = value.attr
                learned.append(name)
        return learned

    @staticmethod
    def _direct(node: ast.AST) -> list[tuple[str, ast.AST]]:
        if isinstance(node, ast.Assign):
            return [(t.id, node.value) for t in node.targets if isinstance(t, ast.Name)]
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            return [(node.target.id, node.value)]
        if isinstance(node, ast.NamedExpr):
            return [(node.target.id, node.value)]
        if isinstance(node, ast.arguments):
            positional = node.posonlyargs + node.args
            out = [(arg.arg, d) for arg, d in zip(positional[len(positional) - len(node.defaults):], node.defaults)]
            return out + [(arg.arg, d) for arg, d in zip(node.kwonlyargs, node.kw_defaults) if d is not None]
        return []

    def import_kind(self, call: ast.Call) -> str | None:
        f = call.func
        if isinstance(f, ast.Name) and f.id in self.importers:
            return self.importers[f.id]
        if isinstance(f, ast.Attribute) and f.attr in _IMPORT_FUNCS and isinstance(f.value, ast.Name) and f.value.id in self.helper_modules:
            return f.attr
        return None

    def literal(self, arg: ast.AST | None) -> str | None:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value
        if isinstance(arg, ast.Name) and isinstance(arg.ctx, ast.Load):
            return self.consts.get(arg.id)
        return None

    def import_target(self, node: ast.AST) -> tuple[bool, str | None, bool]:
        """(is this a dynamic import, the tracked module it returns or None, whether its module name could be resolved)."""
        if not isinstance(node, ast.Call):
            return False, None, True
        kind = self.import_kind(node)
        if kind is None:
            return False, None, True
        arg = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "name"), None)
        name = self.literal(arg)
        if name is None:
            return True, None, False
        if kind == "__import__":  # without a fromlist __import__('os.path') returns os; the root is the safe superset
            root = name.split(".")[0]
            root = _MODULE_ALIASES.get(root, root)
            return True, (root if root in _TRACKED_MODULES else None), True
        if name.startswith("."):  # import_module('.sub', package='pkg') is pkg.sub
            package = self.literal(node.args[1] if len(node.args) > 1 else next((kw.value for kw in node.keywords if kw.arg == "package"), None))
            dots = len(name) - len(name.lstrip("."))
            parts = package.split(".") if package else []
            if not package or dots - 1 >= len(parts):
                return True, None, False
            name = ".".join(parts[:len(parts) - (dots - 1)] + [name.lstrip(".")])
        name = _MODULE_ALIASES.get(name, name)
        parts = name.split(".")
        if parts[0] == "asyncio" and len(parts) > 1 and parts[1] in _ASYNCIO_SUBMODULES:
            return True, "asyncio.*", True  # import_module('asyncio.base_events'): a submodule that re-exports subprocess and os
        return True, (name if name in _TRACKED_MODULES else None), True

    def module_of(self, node: ast.AST) -> str | None:
        """The tracked module this expression IS (a bound name, ``asyncio.subprocess``, a submodule chain, or a literal dynamic import)."""
        if isinstance(node, ast.Name):
            return self.modules.get(node.id) if isinstance(node.ctx, ast.Load) else None
        if isinstance(node, ast.Attribute):
            base = self.module_of(node.value)
            if base not in ("asyncio", "asyncio.*"):
                return None
            # asyncio.subprocess is asyncio's wrapper; asyncio.base_events.subprocess / .os ARE the real modules; a submodule stays "asyncio.*"
            reexports = _PACKAGE_REEXPORTS if base == "asyncio" else _SUBMODULE_REEXPORTS
            return reexports.get(node.attr) or ("asyncio.*" if node.attr in _ASYNCIO_SUBMODULES else None)
        if isinstance(node, ast.Call):
            return self.import_target(node)[1]
        return None


def _in_class_scope(node: ast.AST, parent: dict[int, ast.AST]) -> bool:
    """Is this statement in a class body (directly, or under if/for/try/with there), where a name is a class attribute?"""
    cur = parent.get(id(node))
    while cur is not None:
        if isinstance(cur, ast.ClassDef):
            return True
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        cur = parent.get(id(cur))
    return False


def _in_annotation(node: ast.AST, parent: dict[int, ast.AST]) -> bool:
    """Is this expression, or a subscript/tuple/union holding it, the annotation of a parameter, variable or return?"""
    cur = node
    while True:
        up = parent.get(id(cur))
        if isinstance(up, ast.arg):
            return cur is up.annotation
        if isinstance(up, ast.AnnAssign):
            return cur is up.annotation
        if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur is up.returns
        if isinstance(up, (ast.Subscript, ast.Tuple, ast.List)) or (isinstance(up, ast.BinOp) and isinstance(up.op, ast.BitOr)):
            cur = up
            continue
        return False


def _position_ok(node: ast.AST, up: ast.AST | None, module: str | None, parent: dict[int, ast.AST]) -> bool:
    """May a spawn API (or, if ``module`` is set, that tracked module) appear at this place without being followed?"""
    if isinstance(up, ast.Call):
        if node is up.func:
            return True  # the call itself: judged for creationflags below
        if module is not None:  # getattr(subprocess, "CREATE_NO_WINDOW", 0): reading a NON-spawn constant by name is not an escape
            return (isinstance(up.func, ast.Name) and up.func.id in ("getattr", "hasattr") and up.args and up.args[0] is node
                    and len(up.args) >= 2 and isinstance(up.args[1], ast.Constant) and isinstance(up.args[1].value, str)
                    and up.args[1].value not in _tracked(module) and _attr_ok(module, up.args[1].value))
        return isinstance(up.func, ast.Name) and up.func.id in ("isinstance", "issubclass")
    if isinstance(up, ast.Attribute):
        return module is not None and node is up.value and _attr_ok(module, up.attr)  # subprocess.PIPE, sp.run, ...
    if isinstance(up, ast.Assign):  # in a class body the name is a class attribute, which no call is tied back to
        return node is up.value and all(isinstance(t, ast.Name) for t in up.targets) and not _in_class_scope(up, parent)
    if isinstance(up, ast.AnnAssign):
        return node is up.annotation or (node is up.value and isinstance(up.target, ast.Name) and not _in_class_scope(up, parent))
    if isinstance(up, ast.NamedExpr):  # `(r := subprocess.run)` as a statement is an alias; inside any larger expression it is not followed
        return node is up.value and isinstance(parent.get(id(up)), ast.Expr) and not _in_class_scope(up, parent)
    if isinstance(up, ast.arguments):
        return True  # a default argument: bound to its parameter name by _Facts
    if isinstance(up, ast.arg):
        return node is up.annotation
    if isinstance(up, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return node is up.returns
    if isinstance(up, ast.Subscript):  # subprocess.Popen[bytes] in an annotation; as a value it is a callable alias
        return module is None and node is up.value and _in_annotation(up, parent)
    if isinstance(up, ast.Tuple) and module is None:
        gp = parent.get(id(up))
        return isinstance(gp, ast.Call) and isinstance(gp.func, ast.Name) and gp.func.id in ("isinstance", "issubclass")
    return False


def _named_helper_access(node: ast.AST, up: ast.AST | None, facts: _Facts) -> bool:
    """``setattr(builtins, NAME, v)`` / ``getattr(importlib, 'x')``: the import machinery is named, and the name is not an import function."""
    if not (isinstance(up, ast.Call) and isinstance(up.func, ast.Name) and up.func.id in ("setattr", "delattr", "getattr", "hasattr")):
        return False
    if len(up.args) < 2 or up.args[0] is not node:
        return False
    name = facts.literal(up.args[1])
    return name is not None and name not in _IMPORT_FUNCS and not (name.startswith("__") and name.endswith("__"))


def _reference_violations(tree: ast.AST, facts: _Facts) -> list[tuple[int, str]]:
    """(line, reason) where a tracked module or spawn API appears somewhere the scan cannot follow it.

    Default-deny: a reference is fine only as the callee of a call, the receiver of a harmless attribute, the value of a
    plain-name assignment or default argument (outside a class body), an annotation, or an isinstance() type. In a container,
    comprehension, unpacking, call argument, return, yield, lambda body, starred target, ``with``/``match``/``for`` source, base
    class, subscript value or attribute target it is REPORTED, because the call that eventually spawns cannot be tied back to it
    by syntax. The same holds for the functions that import a module by name. A dynamic import is followed when its module
    name is a literal (or a name bound once to a literal) and REPORTED when it is not.
    """
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    out: list[tuple[int, str]] = []
    binds_spawn = bool(facts.modules or facts.funcs)
    for node in ast.walk(tree):
        module: str | None = None
        is_ref = False
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]] if node.level == 0 else []
            else:
                roots = [a.name.split(".")[0] for a in node.names]
            if any(r in _CLASS_SCOPE_IMPORT_ROOTS for r in roots) and _in_class_scope(node, parent):
                out.append((node.lineno, "imports a spawn or import module in a class body, where the name is a class attribute"))
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                origin = _MODULE_ALIASES.get(node.module or "", node.module or "")
                if origin in _STRICT_MODULES:
                    for a in node.names:
                        if a.name != "*" and not _attr_ok(origin, a.name):
                            out.append((node.lineno, f"imports {a.name} from {origin}, which is not a constant or a data type"))
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _LOOP_METHOD_NAME_RE.fullmatch(node.value):
            out.append((node.lineno, f"names {node.value!r} as a string, which reaches an event-loop spawn method without calling it in place"))
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and facts.module_of(node.value) is not None:
            out.append((node.lineno, f"assigns or deletes the attribute {node.attr} on a spawn module"))  # CREATE_NO_WINDOW, os.name, run ...
        if isinstance(node, ast.Attribute) and node.attr in _LOOP_SPAWN_METHODS:
            up = parent.get(id(node))
            if not (isinstance(up, ast.Call) and up.func is node):  # an alias of loop.subprocess_exec is not followed
                out.append((node.lineno, f"uses {node.attr} other than by calling it in place"))
        if isinstance(node, ast.Call):
            is_import, module, resolved = facts.import_target(node)
            if is_import and not resolved:
                out.append((node.lineno, "makes a dynamic import whose module name is not a string literal"))
                continue
            is_ref = module is not None
            f = node.func
            if (isinstance(f, ast.Name) and f.id in ("getattr", "hasattr") and node.args and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in facts.helper_modules
                    and not (len(node.args) > 1 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                             and node.args[1].value not in _IMPORT_FUNCS)):
                out.append((node.lineno, "reads an import function off importlib/builtins by getattr"))
        elif isinstance(node, ast.Attribute):
            module = facts.module_of(node)
            if module is not None:
                is_ref = True
            else:
                base = facts.module_of(node.value)
                is_ref = (base is not None and node.attr in _tracked(base)) or (
                    isinstance(node.value, ast.Name) and node.value.id in facts.helper_modules and node.attr in _IMPORT_FUNCS)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in facts.ambiguous:
                out.append((node.lineno, "uses a name bound to more than one spawn module or API"))
            module = facts.modules.get(node.id)
            is_ref = module is not None or node.id in facts.funcs or node.id in facts.importers or node.id in facts.helper_modules
        up = parent.get(id(node))
        # importlib / builtins: `importlib.import_module` is judged at its own node, but the module itself in a container or call
        # argument hides the import function from the scan (`loaders = [importlib]; loaders[0].import_module('subprocess')`).
        helper_receiver = (isinstance(node, ast.Name) and module is None and node.id in facts.helper_modules
                           and ((isinstance(up, ast.Attribute) and node is up.value) or _named_helper_access(node, up, facts)))
        if is_ref and not helper_receiver and not _position_ok(node, up, module, parent):
            out.append((node.lineno, "uses a spawn module or API where its calls cannot be followed"))
        if binds_spawn:
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            if name in _REFLECTION and (not isinstance(node, ast.Name) or isinstance(node.ctx, ast.Load)):
                out.append((node.lineno, f"uses {name}, which can reach a spawn API without a followable reference"))
    return sorted(set(out))


def _mutates_modules(tree: ast.AST, receivers: set[str]) -> bool:
    """True when something could change what an own-package module attribute holds (setattr, __dict__, vars(), globals())."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in _MUTATORS | {"vars", "globals", "locals"}:
                first = node.args[0] if node.args else None
                if f.id in ("globals", "locals") or not isinstance(first, ast.Name) or first.id in receivers:
                    return True
        elif isinstance(node, ast.Attribute) and node.attr in _MUTATING_ATTRS:
            if not isinstance(node.value, ast.Name) or node.value.id in receivers:
                return True
    return False


def _provably_hidden(expr: ast.AST, names: set[str], module_like: set[str]) -> bool:
    """True only for a flag built ENTIRELY from accepted parts, at least one of which carries CREATE_NO_WINDOW. Nothing is evaluated.

    Parts joined by ``|``: an int literal without the 0x10 bit (bool is an int, so False/True carry no hidden bit); a name or
    attribute spelled CREATE_NO_WINDOW / _NO_WINDOW that nothing rebinds (an attribute must hang off an imported module);
    a ``no_window_flags(...)`` call that nothing rebinds (the helper strips 0x10 itself); and a name every binding of which is
    such a flag. Any other part (a variable, a call, ``x & ~0x10``, other arithmetic) makes the whole flag not provable: Windows
    ignores CREATE_NO_WINDOW when CREATE_NEW_CONSOLE (0x10) is also set, and an unknown part, or an operator an unknown object
    overloads, could carry it.
    """
    hidden = False
    stack = [expr]
    while stack:
        e = stack.pop()
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.BitOr):
            stack.extend((e.left, e.right))
        elif isinstance(e, ast.Constant):
            if not isinstance(e.value, int) or e.value & _CREATE_NEW_CONSOLE:
                return False
            hidden |= bool(e.value & _CREATE_NO_WINDOW)
        elif isinstance(e, ast.Name):
            if e.id not in names:
                return False
            hidden = True
        elif isinstance(e, ast.Attribute):
            if not (e.attr in _HIDDEN_NAMES and e.attr in names and isinstance(e.value, ast.Name) and e.value.id in module_like):
                return False
            hidden = True
        elif isinstance(e, ast.Call):
            f = e.func
            helper = (isinstance(f, ast.Name) and f.id == _HELPER_NAME) or (
                isinstance(f, ast.Attribute) and f.attr == _HELPER_NAME and isinstance(f.value, ast.Name) and f.value.id in module_like)
            if not (helper and _HELPER_NAME in names):
                return False
            hidden = True
        else:
            return False
    return hidden


def _is_no_window_definition(value: ast.AST | None, facts: _Facts) -> bool:
    """Exactly ``getattr(subprocess, "CREATE_NO_WINDOW", 0)`` or that call under ``if os.name == "nt" else 0``.

    The definition is trusted only when ``getattr``, ``os`` and the subprocess module are the real ones: nothing in the source
    rebinds them or imports a second object under their name.
    """
    if isinstance(value, ast.IfExp):
        test = value.test
        is_nt = (
            isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.left, ast.Attribute) and test.left.attr == "name"
            and isinstance(test.left.value, ast.Name) and test.left.value.id == "os"
            and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value == "nt"
        )
        zero = isinstance(value.orelse, ast.Constant) and type(value.orelse.value) is int and value.orelse.value == 0
        if not (is_nt and zero) or facts.import_src.get("os") != {"os"} or "os" in facts.binders:
            return False
        value = value.body
    return (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "getattr"
        and "getattr" not in facts.binders and "getattr" not in facts.imports
        and len(value.args) == 3 and not value.keywords
        and isinstance(value.args[0], ast.Name) and facts.modules.get(value.args[0].id) == "subprocess"
        and facts.import_src.get(value.args[0].id) == {"subprocess"}  # one source: `import subprocess [as x]`, never a from-import
        and value.args[0].id not in facts.binders
        and isinstance(value.args[1], ast.Constant) and value.args[1].value == "CREATE_NO_WINDOW"
        and isinstance(value.args[2], ast.Constant) and type(value.args[2].value) is int and value.args[2].value == 0
    )


def _hidden_names(tree: ast.AST, facts: _Facts) -> tuple[set[str], set[str]]:
    """(names whose EVERY binding is provably hidden, the receivers an attribute form may hang off).

    The trusted spellings (CREATE_NO_WINDOW, _NO_WINDOW, no_window_flags) are in the first set only when nothing in the
    source rebinds them, by any statement or by an attribute store, other than a plain import of the shared name from this
    package, or when their one definition is the standard one-liner. A receiver (``hidden_spawn.no_window_flags``,
    ``subprocess.CREATE_NO_WINDOW``) must be this repo's own package or the real subprocess module, imported and never rebound,
    and nothing in the source may setattr/delattr/vars/``__dict__`` it. A worklist re-judges a name only when a name its
    values mention became hidden, so the cost is linear in the source.
    """
    binders = facts.binders
    subprocess_imports = {name for name, module in facts.modules.items() if module == "subprocess"}
    receivers = (facts.own | subprocess_imports) - set(binders)
    if _mutates_modules(tree, receivers | set(binders)):
        receivers = set()
    names = {n for n in _TRUSTED_NAMES if n not in binders}
    eligible = {name for name, values in binders.items() if name not in facts.imports and not any(v is None for v in values)}
    dependants: dict[str, list[tuple[str, int]]] = {}
    queue: list[tuple[str, int]] = []
    for name in eligible:
        for index, value in enumerate(binders[name]):
            queue.append((name, index))
            for sub in ast.walk(value):
                if isinstance(sub, ast.Name):
                    dependants.setdefault(sub.id, []).append((name, index))
    proven: dict[str, set[int]] = {}
    while queue:  # a binding is re-judged only when a name it mentions became hidden: linear in the source
        name, index = queue.pop()
        if name in names or index in proven.get(name, ()):
            continue
        value = binders[name][index]
        if (name in _HIDDEN_NAMES and _is_no_window_definition(value, facts)) or _provably_hidden(value, names, receivers):
            proven.setdefault(name, set()).add(index)
            if len(proven[name]) == len(binders[name]):
                names.add(name)
                queue.extend(dependants.get(name, ()))
    return names, receivers


def _unflagged_spawns_in_source(source: str, label: str) -> list[str]:
    tree = ast.parse(source, filename=label)
    facts = _Facts(tree)
    hidden, module_like = _hidden_names(tree, facts)
    bad = [f"{label}:{line} {why}" for line, why in _reference_violations(tree, facts)]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        target = None
        if isinstance(fn, ast.Attribute):
            base = facts.module_of(fn.value)
            if base is not None and fn.attr in _tracked(base):
                target = (base, fn.attr)
        elif isinstance(fn, ast.Name) and fn.id in facts.funcs:
            target = facts.funcs[fn.id]
        if target is None and isinstance(fn, ast.Attribute) and fn.attr in _LOOP_SPAWN_METHODS:
            target = ("asyncio", fn.attr)  # loop.subprocess_exec(...): whatever the receiver is, it is a Popen underneath
        if target is None:
            continue
        module, attr = target
        shown = f"{module}.{attr}" if isinstance(fn, ast.Attribute) else f"{fn.id} (= {module}.{attr})"
        if attr in _FORBIDDEN_APIS.get(module, ()):
            bad.append(f"{label}:{node.lineno} {shown}(...) cannot take CREATE_NO_WINDOW and is forbidden in a tool module")
            continue
        flags = [kw for kw in node.keywords if kw.arg == "creationflags"]
        if flags and all(_provably_hidden(kw.value, hidden, module_like) for kw in flags):
            continue
        why = "creationflags= is not provably CREATE_NO_WINDOW" if flags else "has no creationflags="
        bad.append(f"{label}:{node.lineno} {shown}(...) {why}")
    return bad


# --- PowerShell fixtures: Start-Process powershell must be hidden, judged per statement ---------------------------------

_WINDOW_STYLE_HIDDEN_RE = re.compile(r"(?<!\S)-WindowStyle[\s:]+(?:'Hidden'|\"Hidden\"|Hidden(?![\w-]))", re.IGNORECASE)
_START_PROCESS_RE = re.compile(r"(?<![\w$.-])(Start-Process|saps|start)(?![\w-])", re.IGNORECASE)  # the cmdlet and its aliases


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every PowerShell quoted string, both quotes included. A doubled quote escapes itself."""
    spans, i, n = [], 0, len(text)
    while i < n:
        quote = text[i]
        if text.startswith("<#", i):  # a comment may hold an apostrophe; it must not open a string
            end = text.find("#>", i + 2)
            i = n if end == -1 else end + 2
            continue
        if quote == "#" and (i == 0 or text[i - 1].isspace() or text[i - 1] in ";|{}("):
            while i < n and text[i] != "\n":
                i += 1
            continue
        if quote not in "'\"":
            i += 1
            continue
        j = i + 1
        while j < n:
            if text[j] == quote:
                if j + 1 < n and text[j + 1] == quote:
                    j += 2
                    continue
                break
            j += 2 if (quote == '"' and text[j] == "`") else 1
        spans.append((i, min(j, n - 1)))
        i = j + 1
    return spans


def _ps_statements(script: str) -> list[str]:
    """Split a PowerShell script into statements: on ; | { } and newlines outside quotes, comments dropped, backtick-newline joined."""
    script = re.sub(r"`\r?\n", " ", script)
    spans = dict(_quoted_spans(script))
    out, cur, i, n = [], [], 0, len(script)
    while i < n:
        if i in spans:
            cur.append(script[i:spans[i] + 1])
            i = spans[i] + 1
            continue
        ch = script[i]
        if script.startswith("<#", i):
            end = script.find("#>", i + 2)
            i = n if end == -1 else end + 2
            continue
        if ch == "#" and (i == 0 or script[i - 1].isspace() or script[i - 1] in ";|{}("):
            while i < n and script[i] != "\n":
                i += 1
            continue
        if ch in ";|&{}\r\n":  # & also backgrounds the command before it
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    out.append("".join(cur))
    return [s for s in out if s.strip()]


def _at_command_position(before: str) -> bool:
    """May a command word stand right after ``before`` (the statement text up to it)? A call operator ``&`` or a dot-source ``.`` may sit between."""
    b = before.rstrip()
    while b and b[-1] in "&." and (b[-1] == "&" or len(b) == 1 or b[-2].isspace() or b[-2] in "=({"):
        b = b[:-1].rstrip()
    return b == "" or b[-1] in "=("


def _judge_script(script: str) -> tuple[int, int]:
    """(Start-Process / saps / start statements, those lacking their own outer -WindowStyle Hidden). Quoted text is a nested script.

    The parameter must be a token of the Start-Process command itself: outside quotes, at the same parenthesis depth, and
    starting a word (``foo-WindowStyle:Hidden`` and ``(Get-ChildArgs -WindowStyle Hidden)`` do not count).
    """
    found = bad = 0
    for statement in _ps_statements(script):
        spans = _quoted_spans(statement)
        quoted = [False] * len(statement)
        for s, e in spans:
            for k in range(s, e + 1):
                quoted[k] = True
        depth, depths = 0, [0] * len(statement)
        for k, ch in enumerate(statement):
            if not quoted[k]:
                depth = depth + 1 if ch == "(" else max(0, depth - 1) if ch == ")" else depth
            depths[k] = depth
        hole = "\x01" in statement  # a run-time value of unknown text sits in this statement: it could hold a comment or a quote
        command_names = set()  # quoted strings that ARE the command name (`& 'Start-Process' ...`): not a nested script
        for m in _START_PROCESS_RE.finditer(statement):
            word = m.group(1).lower()
            before = statement[:m.start()].rstrip()
            span = next(((s, e) for s, e in spans if s <= m.start() <= e), None)
            if span is not None:  # a quoted command name is a command only as `& 'Start-Process' ...`, and it must be the whole string
                before = statement[:span[0]].rstrip()
                if statement[span[0] + 1:span[1]].strip().lower() != word or not _at_command_position(before):
                    continue
                command_names.add(span)
            elif word != "start-process" and not _at_command_position(before):
                continue  # `start` / `saps` count only where a command word can stand
            # Every launch is judged, whatever it starts (powershell, pwsh, cmd.exe, `-FilePath $exe`): the target decides nothing.
            found += 1
            if hole or not any(not quoted[h.start()] and depths[h.start()] == depths[m.start()]
                               for h in _WINDOW_STYLE_HIDDEN_RE.finditer(statement, m.start())):
                bad += 1
        for s, e in spans:
            inner = statement[s + 1:e]
            if (s, e) not in command_names and _START_PROCESS_RE.search(inner):
                inner_found, inner_bad = _judge_script(inner)
                found += inner_found
                bad += inner_bad
    return found, bad


_STRING_BUILDERS = frozenset({"format", "format_map", "join", "replace", "__mod__"})
# A literal piece of a run-time built string that reads as a launch: a ``Start-`` prefix, or ``start`` / ``saps`` where a command word can
# stand (the piece's start, or after ; & | ( { = or a dot-source), so 'the start of {}' is prose and 'start {}' is the alias.
_BUILT_LAUNCH_RE = re.compile(r"start-|(?:^|[;&|({=]|\s\.)\s*(?:start|saps)(?![\w-])", re.IGNORECASE | re.MULTILINE)


def _string_values(tree: ast.AST) -> list[tuple[int, str, bool]]:
    """(line, text, built at run time) for every maximal string expression.

    A literal, an implicit concatenation and a ``+`` chain of literals have a known text. Anything else that builds a string
    (an f-string, ``%``, ``.format``, ``.join``) cannot be read statically; if a literal piece of it says ``Start-`` it is
    reported as run-time built (fail closed) instead of being guessed at.
    """
    out: list[tuple[int, str, bool]] = []

    def literal_text(node: ast.AST) -> str | None:
        operands = []
        while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):  # iterative: a long chain must not exhaust the stack
            operands.append(node.right)
            node = node.left
        operands.append(node)
        texts = [o.value if isinstance(o, ast.Constant) and isinstance(o.value, str) else None for o in reversed(operands)]
        return None if any(t is None for t in texts) else "".join(texts)

    stack = [tree]
    while stack:
        node = stack.pop()
        text = literal_text(node)
        if text is not None:
            out.append((node.lineno, text, False))
            continue
        if isinstance(node, ast.JoinedStr):  # an f-string whose holes hold no string literal reads as its text with a mark per hole
            holes = [v.value for v in node.values if isinstance(v, ast.FormattedValue)]
            if not any(isinstance(c, ast.Constant) and isinstance(c.value, str) for h in holes for c in ast.walk(h)):
                out.append((node.lineno, "".join(v.value if isinstance(v, ast.Constant) else "\x01" for v in node.values), False))
                continue
        builds = isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _STRING_BUILDERS)
        if builds:
            pieces = [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            if any(_BUILT_LAUNCH_RE.search(p) for p in pieces):
                out.append((node.lineno, "".join(pieces), True))
                continue
        stack.extend(ast.iter_child_nodes(node))
    return out


def _start_process_violations(py_source: str) -> tuple[int, list[int]]:
    """(number of PowerShell ``Start-Process ... powershell`` statements, python lines of those lacking an outer -WindowStyle Hidden).

    Reads the module's string expressions (so a comment never counts; implicit concatenation and ``+`` of literals are joined),
    splits each into PowerShell statements, and judges every statement holding ``Start-Process`` and ``powershell`` on its own
    text. Quoted text is judged as a nested script, so ``-WindowStyle Hidden`` inside an -ArgumentList value does not satisfy
    the outer statement; a quoted value (``-WindowStyle 'Hidden'``) does. A string built at run time out of a ``Start-`` piece
    (f-string, ``%``, ``.format``, ``.join``) is REPORTED: its text is not known, so it cannot be proven hidden.
    """
    found, bad = 0, []
    for lineno, text, built in _string_values(ast.parse(py_source)):
        if built:
            found += 1
            bad.append(lineno)
            continue
        if not _START_PROCESS_RE.search(text) and not re.search(r"'\s*(start-process|saps|start)\s*'|\"\s*(start-process|saps|start)\s*\"", text, re.I):
            continue
        text_found, text_bad = _judge_script(text)
        found += text_found
        bad.extend([lineno] * text_bad)
    return found, bad


def _unflagged_spawns(rel: str) -> list[str]:
    return _unflagged_spawns_in_source((ROOT / rel).read_text(encoding="utf-8"), rel)


class NoWindowFlagsTests(unittest.TestCase):
    def test_sets_create_no_window_on_windows(self):
        with mock.patch.object(hidden_spawn.os, "name", "nt"):
            flags = hidden_spawn.no_window_flags()
        self.assertEqual(flags & 0x08000000, 0x08000000)

    def test_ors_an_existing_flag_instead_of_replacing_it(self):
        below_normal = 0x00004000
        with mock.patch.object(hidden_spawn.os, "name", "nt"):
            flags = hidden_spawn.no_window_flags(below_normal)
        self.assertEqual(flags, below_normal | 0x08000000)

    def test_is_a_no_op_off_windows(self):
        with mock.patch.object(hidden_spawn.os, "name", "posix"):
            self.assertEqual(hidden_spawn.no_window_flags(), 0)
            self.assertEqual(hidden_spawn.no_window_flags(0x20), 0x20)


class ToolSpawnSitesTests(unittest.TestCase):
    def test_scan_set_is_not_empty_and_skips_excluded_classes(self):
        files = _tool_modules()
        self.assertGreater(len(files), 50, "glob matched suspiciously few tool modules")
        self.assertIn("tools/repo_hygiene/core.py", files)
        self.assertNotIn("tools/gates/output_budget.py", files)

    def test_every_tool_spawn_carries_creationflags(self):
        bad = []
        for rel in _tool_modules():
            bad.extend(_unflagged_spawns(rel))
        self.assertEqual(bad, [], "console spawn without CREATE_NO_WINDOW:\n" + "\n".join(bad))


class NamedTestFixtureTests(unittest.TestCase):
    def test_named_test_modules_spawn_hidden(self):
        bad = []
        for rel in NAMED_TEST_MODULES:
            bad.extend(_unflagged_spawns(rel))
        self.assertEqual(bad, [], "test fixture spawn without CREATE_NO_WINDOW:\n" + "\n".join(bad))

    def test_host_load_gate_start_process_fixtures_are_hidden(self):
        src = (ROOT / "tools/repo_hygiene/test_playback_host_load_gate.py").read_text(encoding="utf-8")
        found, bad = _start_process_violations(src)
        self.assertGreaterEqual(found, 3, "expected the three run.ps1 Start-Process fixtures")
        self.assertEqual(bad, [], "Start-Process powershell.exe without -WindowStyle Hidden at python line(s): " + ", ".join(map(str, bad)))


class ScannerHardeningTests(unittest.TestCase):
    """MLV-LANE-CONSOLE-HIDDEN-1 r2 rows A2 and A3: the scanner itself must not be foolable."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    def test_literal_zero_creationflags_is_flagged(self):
        self.assertEqual(len(self._bad("import subprocess\nsubprocess.run(['x'], creationflags=0)\n")), 1)

    def test_a_real_flag_value_passes(self):
        self.assertEqual(self._bad("import subprocess\nsubprocess.run(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("import subprocess\nsubprocess.run(['x'], creationflags=0x08000000)\n"), [])

    def test_import_subprocess_as_alias_is_resolved(self):
        self.assertEqual(len(self._bad("import subprocess as sp\nsp.run(['x'])\n")), 1)
        self.assertEqual(self._bad("import subprocess as sp\nsp.run(['x'], creationflags=_NO_WINDOW)\n"), [])

    def test_from_import_spawn_names_are_resolved(self):
        self.assertEqual(len(self._bad("from subprocess import run\nrun(['x'])\n")), 1)
        self.assertEqual(len(self._bad("from subprocess import Popen as P\nP(['x'])\n")), 1)
        self.assertEqual(self._bad("from subprocess import run\nrun(['x'], creationflags=_NO_WINDOW)\n"), [])

    def test_runner_default_argument_alias_is_resolved(self):
        src = "import subprocess\ndef f(*, runner=subprocess.run):\n    return runner(['x'], capture_output=True)\n"
        self.assertEqual(len(self._bad(src)), 1)
        ok = "import subprocess\ndef f(*, runner=subprocess.run):\n    return runner(['x'], creationflags=_NO_WINDOW)\n"
        self.assertEqual(self._bad(ok), [])

    def test_assignment_alias_is_resolved(self):
        self.assertEqual(len(self._bad("import subprocess\nr = subprocess.check_output\nr(['x'])\n")), 1)

    def test_scan_terminates_on_a_conflicting_rebinding(self):
        import threading
        src = "import subprocess\nr = subprocess.run\nr = subprocess.Popen\nr(['x'], creationflags=0x08000000)\n"
        out: list = []
        worker = threading.Thread(target=lambda: out.append(self._bad(src)), daemon=True)
        worker.start()
        worker.join(10)
        self.assertFalse(worker.is_alive(), "alias resolution did not terminate")
        self.assertEqual(len(out), 1)
        self.assertGreaterEqual(len(out[0]), 1)  # r = run then r = Popen: a name bound to two APIs is reported, not guessed

    def test_a_constant_flag_without_the_hidden_bit_is_flagged(self):
        for value in ("0x20", "0 | 0", "-0", "0x4000"):
            src = f"import subprocess\nsubprocess.run(['x'], creationflags={value})\n"
            self.assertEqual(len(self._bad(src)), 1, value)
        self.assertEqual(self._bad("import subprocess\nsubprocess.run(['x'], creationflags=0x08000000 | 0x20)\n"), [])

    def test_module_assignment_alias_is_resolved(self):
        self.assertEqual(len(self._bad("import subprocess\nsp = subprocess\nsp.run(['x'])\n")), 1)
        self.assertEqual(self._bad("import subprocess\nsp = subprocess\nsp.run(['x'], creationflags=_NO_WINDOW)\n"), [])

    def test_unrelated_names_are_not_flagged(self):
        self.assertEqual(self._bad("def run(a):\n    return a\nrun(1)\nfoo.run(2)\n"), [])

    def test_exclusion_compares_the_whole_normalized_path(self):
        self.assertTrue(_is_excluded("tools/coordination/doctrine_outbox.py"))
        self.assertTrue(_is_excluded("tools\\coordination\\doctrine_outbox.py"))
        self.assertTrue(_is_excluded("./tools/repo_hygiene/brokered_closeout.py"))
        self.assertTrue(_is_excluded("tools/gates/output_budget.py"))
        self.assertFalse(_is_excluded("tools/coordination/doctrine_outbox.py.bak"))
        self.assertFalse(_is_excluded("tools/coordination/doctrine_outbox_extra.py"))
        self.assertFalse(_is_excluded("tools/repo_hygiene/brokered_closeout_helper.py"))
        self.assertFalse(_is_excluded("tools/gates_extra/x.py"))

    def test_ls_files_survives_a_non_ascii_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            flags = hidden_spawn.no_window_flags()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, timeout=60, creationflags=flags)
            (repo / "tools").mkdir()
            names = ["caf\u00e9.py", "plain.py"]
            for name in names:
                (repo / "tools" / name).write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, timeout=60, creationflags=flags)
            self.assertEqual(_tracked_py_files(repo), sorted("tools/" + n for n in names))


class ScannerFalseCleanTests(unittest.TestCase):
    """MLV-LANE-CONSOLE-HIDDEN-1 r3 rows B2 and B3: the scanner must never give a false-clean verdict and never evaluate source."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    def test_false_and_true_flags_lack_the_hidden_bit_for_every_spawn_api(self):
        for api in sorted(SPAWN_ATTRS):
            for value in ("False", "True", "None", "'x'", "1.5", "-1", "0x08000000 & 0", "0x08000000 ^ 0x08000000", "x", "f()"):
                src = f"import subprocess\nsubprocess.{api}(['x'], creationflags={value})\n"
                self.assertEqual(len(self._bad(src)), 1, (api, value))

    def test_only_the_accepted_hidden_forms_are_clean(self):
        clean = (
            "0x08000000", "0x08004000", "134217728", "subprocess.CREATE_NO_WINDOW", "_NO_WINDOW", "core._NO_WINDOW",
            "no_window_flags()", "hidden_spawn.no_window_flags(0x20)", "0x20 | subprocess.CREATE_NO_WINDOW",
            "(0x20 | 0x4000) | no_window_flags()",
        )
        head = "import subprocess\nfrom tools.repo_hygiene import core, hidden_spawn\n"
        for api in sorted(SPAWN_ATTRS):
            for value in clean:
                src = f"{head}subprocess.{api}(['x'], creationflags={value})\n"
                self.assertEqual(self._bad(src), [], (api, value))
        for value in ("_NO_WINDOW | kwargs.pop('creationflags', 0)", "_NO_WINDOW | extra", "_NO_WINDOW | f()", "_NO_WINDOW & 0x4000",
                      "_NO_WINDOW + 0", "_NO_WINDOW | (x & ~0x20)", "_NO_WINDOW | (x & 0x10)",
                      "_NO_WINDOW | (kwargs.pop('creationflags', 0) & ~0x10)"):
            self.assertEqual(len(self._bad(f"{head}subprocess.run(['x'], creationflags={value})\n")), 1, value)

    def test_a_flag_name_is_hidden_only_when_every_binding_of_it_is_hidden(self):
        ok = "import subprocess\nflags = no_window_flags()\nsubprocess.run(['x'], creationflags=flags)\n"
        self.assertEqual(self._bad(ok), [])
        for later in ("flags = 0", "flags |= 1", "def f(flags): pass", "for flags in (1,): pass", "flags: int = 0"):
            src = f"import subprocess\nflags = no_window_flags()\n{later}\nsubprocess.run(['x'], creationflags=flags)\n"
            self.assertEqual(len(self._bad(src)), 1, later)
        self.assertEqual(len(self._bad("import subprocess\nsubprocess.run(['x'], creationflags=flags)\n")), 1)

    def test_a_redefined_no_window_constant_is_not_trusted(self):
        std = 'import subprocess, os\n_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0\n'
        self.assertEqual(self._bad(std + "subprocess.run(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(len(self._bad("import subprocess\n_NO_WINDOW = 0\nsubprocess.run(['x'], creationflags=_NO_WINDOW)\n")), 1)

    def test_every_creationflags_keyword_must_be_hidden(self):
        src = "import subprocess\nsubprocess.run(['x'], creationflags=_NO_WINDOW, **opts)\n"
        self.assertEqual(self._bad(src), [])
        self.assertEqual(len(self._bad("import subprocess\nsubprocess.run(['x'], **opts)\n")), 1)

    def test_bindings_that_carry_a_spawn_api_are_resolved_or_reported(self):
        unhidden = {
            "tuple": "import subprocess\nr,=(subprocess.run,)\nr(['x'])\n",
            "paren tuple": "import subprocess\n(r,)=(subprocess.run,)\nr(['x'])\n",
            "list": "import subprocess\n[r]=[subprocess.Popen]\nr(['x'])\n",
            "chained": "import subprocess\na=b=subprocess.call\nb(['x'])\n",
            "walrus": "import subprocess\n(r:=subprocess.run)\nr(['x'])\n",
            "walrus in expr": "import subprocess\nx = (r:=subprocess.check_output)\nr(['x'])\n",
            "starred": "import subprocess\nfirst, *rest = (subprocess.run, subprocess.Popen)\nfirst(['x'])\n",
            "nested": "import subprocess\n(a, (b, c)) = (1, (subprocess.check_call, 2))\nb(['x'])\n",
            "for loop": "import subprocess\nfor r in (subprocess.run,):\n    r(['x'])\n",
            "comprehension": "import subprocess\nrs = [r(['x']) for r in (subprocess.run,)]\n",
            "or": "import subprocess\nr = None or subprocess.run\nr(['x'])\n",
            "conditional": "import subprocess\nr = subprocess.run if a else subprocess.Popen\nr(['x'])\n",
            "subscript": "import subprocess\nt = (subprocess.run, 1)\nt[0](['x'])\n",
            "module in tuple": "import subprocess\nsp, = (subprocess,)\nsp.run(['x'])\n",
            "star import": "from subprocess import *\nrun(['x'])\n",
            "attribute target": "import subprocess\nclass C: pass\nC.r = subprocess.run\n",
            "subscript target": "import subprocess\nd = {}\nd['r'] = subprocess.run\n",
            "dict value": "import subprocess\nd = {'r': subprocess.run}\nd['r'](['x'])\n",
        }
        for label, src in unhidden.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # Only DIRECT bindings are followed, so only those can be made clean by a hidden flag on the call.
        for label in ("chained", "walrus", "star import"):
            hidden = unhidden[label].replace("(['x'])", "(['x'], creationflags=_NO_WINDOW)")
            self.assertEqual(self._bad(hidden), [], label)
        # Anything that wraps the spawn API is reported even when the eventual call is hidden: the call is not tied back by syntax.
        for label in ("tuple", "list", "for loop", "or", "subscript", "starred", "nested", "dict value", "module in tuple"):
            wrapped = unhidden[label].replace("(['x'])", "(['x'], creationflags=_NO_WINDOW)")
            self.assertGreaterEqual(len(self._bad(wrapped)), 1, label)

    def test_a_non_spawn_use_of_the_module_is_not_an_escape(self):
        fine = (
            "import subprocess\nx = subprocess.CompletedProcess(['x'], 0)\ny = (x, 1)\n",
            "import subprocess\nhelper(subprocess.PIPE, subprocess.TimeoutExpired)\n",
            "import subprocess, os\n_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0\n",
            "import subprocess\ntry:\n    pass\nexcept subprocess.TimeoutExpired:\n    pass\n",
        )
        for src in fine:
            self.assertEqual(self._bad(src), [], src)
        for src in ("import subprocess\ngetattr(subprocess, 'run')(['x'])\n", "import subprocess\ngetattr(subprocess, name)(['x'])\n",
                    "import subprocess\nhelper(subprocess)\n", "import subprocess\nmods = [subprocess]\n"):
            self.assertGreaterEqual(len(self._bad(src)), 1, src)

    def test_no_expression_is_ever_evaluated_and_hostile_arithmetic_is_reported_fast(self):
        import builtins
        import time
        hostile = (
            "2 ** 1000000000000", "1 / 0", "1 << 100000000000", "9 ** 9 ** 9", "10 ** 10 ** 10 | 0x08000000 & 0",
            "0x08000000 + 0", "0x10000000 // 2", "~0", "-(-0x08000000)",
        )
        for value in hostile:
            for src in (
                f"import subprocess\nsubprocess.run(['x'], creationflags={value})\n",
                f"import subprocess\ndef deferred():\n    return subprocess.run(['x'], creationflags={value})\n",
            ):
                t0 = time.perf_counter()
                with mock.patch.object(builtins, "eval", side_effect=AssertionError("scanner evaluated source")), \
                        mock.patch.object(builtins, "exec", side_effect=AssertionError("scanner executed source")):
                    bad = self._bad(src)
                self.assertLess(time.perf_counter() - t0, 1.0, value)
                self.assertEqual(len(bad), 1, value)

    def test_a_deeply_nested_flag_expression_does_not_crash_or_pass(self):
        deep = "|".join(["0x20"] * 5000)
        try:
            bad = self._bad(f"import subprocess\nsubprocess.run(['x'], creationflags={deep})\n")
        except (RecursionError, MemoryError, SyntaxError):
            return  # the parser refused the source; nothing was judged clean
        self.assertEqual(len(bad), 1)


class AdversaryR3Tests(unittest.TestCase):
    """MLV-LANE-CONSOLE-HIDDEN-1 r3 adversary pass (run review-adversary-...-r3-7954a7b2), findings A1-A7."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    def _run(self, value: str) -> list[str]:
        return self._bad(f"import subprocess\nsubprocess.run(['x'], creationflags={value})\n")

    # A1: CREATE_NEW_CONSOLE (0x10) makes Windows ignore CREATE_NO_WINDOW, so no hidden form may carry it.
    def test_a1_the_helper_drops_create_new_console_from_a_caller_flag(self):
        with mock.patch.object(hidden_spawn.os, "name", "nt"):
            flags = hidden_spawn.no_window_flags(0x10)
            both = hidden_spawn.no_window_flags(0x10 | 0x4000)
        self.assertEqual(flags & 0x10, 0)
        self.assertEqual(flags & 0x08000000, 0x08000000)
        self.assertEqual(both, 0x08004000)

    def test_a1_runtime_floor_spawn_drops_create_new_console(self):
        from tools.repo_hygiene import runtime_floor_suites as rfs
        seen = {}

        def fake_popen(argv, **kw):
            seen.update(kw)
            return object()

        with mock.patch.object(hidden_spawn.os, "name", "nt"), mock.patch.object(rfs.subprocess, "Popen", fake_popen):
            rfs._spawn(["x"], creationflags=0x10)
            self.assertEqual(seen["creationflags"], 0x08000000)
            rfs._spawn(["x"], creationflags=0x10 | 0x4000)
            self.assertEqual(seen["creationflags"], 0x08004000)

    # A2: a redefined constant is trusted only in the exact standard one-liner.
    def test_a2_only_the_standard_constant_definition_is_trusted(self):
        head = "import subprocess, os\n"
        use = "subprocess.run(['x'], creationflags=_NO_WINDOW)\n"
        for definition in (
            '_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name != "nt" else 0\n',
            '_NO_WINDOW = getattr(object(), "CREATE_NO_WINDOW", 0)\n',
            '_NO_WINDOW = getattr(os, "CREATE_NO_WINDOW", 0)\n',
            '_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 1\n',
            '_NO_WINDOW = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)\n',
        ):
            self.assertEqual(len(self._bad(head + definition + use)), 1, definition)
        for definition in (
            '_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0\n',
            '_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)\n',
        ):
            self.assertEqual(self._bad(head + definition + use), [], definition)

    # A3: helper names, attribute names and import aliases are trusted only when nothing in the source rebinds them.
    def test_a3_a_locally_rebound_trusted_name_is_not_trusted(self):
        bad = {
            "local def": "import subprocess\ndef no_window_flags(): return 0\nsubprocess.run(['x'], creationflags=no_window_flags())\n",
            "local lambda": "import subprocess\nno_window_flags = lambda: 0\nsubprocess.run(['x'], creationflags=no_window_flags())\n",
            "local def, attribute call": "import subprocess\ndef no_window_flags(): return 0\nsubprocess.run(['x'], creationflags=hs.no_window_flags())\n",
            "class attribute": "import subprocess\nclass Flags:\n    _NO_WINDOW = 0\nsubprocess.run(['x'], creationflags=Flags._NO_WINDOW)\n",
            "renamed import": "import subprocess\nfrom os import F_OK as _NO_WINDOW\nsubprocess.run(['x'], creationflags=_NO_WINDOW)\n",
            "renamed helper import": "import subprocess\nfrom os import getpid as no_window_flags\nsubprocess.run(['x'], creationflags=no_window_flags())\n",
        }
        for label, src in bad.items():
            self.assertEqual(len(self._bad(src)), 1, label)
        good = {
            "constant import": "import subprocess\nfrom tools.repo_hygiene.core import _NO_WINDOW\nsubprocess.run(['x'], creationflags=_NO_WINDOW)\n",
            "helper import": "import subprocess\nfrom tools.repo_hygiene.hidden_spawn import no_window_flags\nsubprocess.run(['x'], creationflags=no_window_flags())\n",
            "module helper": "import subprocess\nfrom tools.repo_hygiene import hidden_spawn\nsubprocess.run(['x'], creationflags=hidden_spawn.no_window_flags())\n",
        }
        for label, src in good.items():
            self.assertEqual(self._bad(src), [], label)

    # A4: every Python binding form disqualifies a trusted flag name.
    def test_a4_match_and_except_bindings_disqualify_a_flag_name(self):
        for rebind in ("match 0:\n case flags: pass", "match 0:\n case [*flags]: pass", "match {}:\n case {**flags}: pass",
                       "try:\n pass\nexcept Exception as flags:\n pass", "import os as flags", "with open('x') as flags:\n pass"):
            src = f"import subprocess\nflags = 0x08000000\n{rebind}\nsubprocess.run(['x'], creationflags=flags)\n"
            self.assertGreaterEqual(len(self._bad(src)), 1, rebind)

    # A5: a module alias held in a container, conditional or call argument is reported (default-deny), never followed.
    def test_a5_a_module_reached_through_a_container_is_reported(self):
        for recv in ("mods[0]", "[subprocess][0]", "(subprocess or x)", "(subprocess if a else x)", "{'m': subprocess}['m']"):
            for call in ("['x']", "['x'], creationflags=_NO_WINDOW"):
                src = f"import subprocess\nmods = [subprocess]\n{recv}.run({call})\n"
                self.assertGreaterEqual(len(self._bad(src)), 1, (recv, call))

    # A6: spawn APIs that travel by dict key, context manager, match capture, call argument or return are reported.
    def test_a6_a_spawn_api_that_escapes_is_reported(self):
        escapes = {
            "dict key": "import subprocess\nd = {subprocess.run: 1}\nfor r in d:\n    r(['x'])\n",
            "with item": "import subprocess, contextlib\nwith contextlib.nullcontext(subprocess.run) as r:\n    r(['x'])\n",
            "with as": "import subprocess\nwith subprocess.run as r:\n    r(['x'])\n",
            "match capture": "import subprocess\nmatch subprocess.run:\n    case r:\n        r(['x'])\n",
            "positional argument": "import subprocess\nhelper(subprocess.run)\n",
            "keyword argument": "import subprocess\nhelper(runner=subprocess.Popen)\n",
            "partial": "import subprocess, functools\nr = functools.partial(subprocess.run, creationflags=0)\nr(['x'])\n",
            "return": "import subprocess\ndef pick():\n    return subprocess.check_output\n",
            "yield": "import subprocess\ndef pick():\n    yield subprocess.call\n",
            "imported name argument": "from subprocess import Popen\nhelper(Popen)\n",
            "alias passed on": "import subprocess\nr = subprocess.run\nhelper(r)\n",
            "dict comprehension": "import subprocess\nrunners = {k: subprocess.run for k in ('run',)}\nrunners['run'](['x'])\n",
            "list argument": "import subprocess\ndef launch(runners):\n    runners[0](['x'])\nlaunch([subprocess.run])\n",
            "returned dict": "import subprocess\ndef pick():\n    return {'run': subprocess.run}\npick()['run'](['x'])\n",
            "starred attribute target": "import subprocess\nbox.r, *rest = [subprocess.run]\nbox.r(['x'])\n",
            "class base": "import subprocess\nclass P(subprocess.Popen):\n    pass\nP(['x'])\n",
            "lambda body": "import subprocess\nf = lambda: subprocess.run\nf()(['x'])\n",
        }
        for label, src in escapes.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        fine = {
            "isinstance": "import subprocess\nisinstance(p, subprocess.Popen)\n",
            "issubclass": "import subprocess\nissubclass(t, (subprocess.Popen, int))\n",
            "annotation": "import subprocess\ndef f(p: subprocess.Popen) -> subprocess.Popen:\n    q: subprocess.Popen = p\n    return q\n",
            "non-spawn attribute": "import subprocess\nhelper(subprocess.PIPE, subprocess.TimeoutExpired)\n",
            "default runner": "import subprocess\ndef f(runner=subprocess.run):\n    return runner(['x'], creationflags=_NO_WINDOW)\n",
        }
        for label, src in fine.items():
            self.assertEqual(self._bad(src), [], label)

    # A1 (scanner side): no accepted expression may carry CREATE_NEW_CONSOLE, and an UNKNOWN part is not provably free of it.
    def test_a1_scanner_rejects_create_new_console_in_any_accepted_form(self):
        for value in ("0x08000010", "0x10 | 0x08000000", "subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW",
                      "CREATE_NEW_CONSOLE | _NO_WINDOW", "_NO_WINDOW | 0x10", "0x08004010", "_NO_WINDOW | CREATE_NEW_CONSOLE | 0x20",
                      "subprocess.CREATE_NO_WINDOW | extra", "subprocess.CREATE_NO_WINDOW | opts.pop('creationflags', 0)",
                      "(8 + 8) | _NO_WINDOW", "_NO_WINDOW | (x & 0x10)", "_NO_WINDOW | (x & ~0x10)"):
            self.assertEqual(len(self._run(value)), 1, value)
        head = "import subprocess\nfrom tools.repo_hygiene import hidden_spawn\n"
        for value in ("no_window_flags(0x10)", "hidden_spawn.no_window_flags(subprocess.CREATE_NEW_CONSOLE)", "_NO_WINDOW | 0x4000"):
            self.assertEqual(_unflagged_spawns_in_source(f"{head}subprocess.run(['x'], creationflags={value})\n", "s.py"), [], value)

    # Pass-5 reproductions (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r3-e8e7da15), verbatim.
    def test_pass5_reproductions_are_reported(self):
        cases = {
            "A1 extra variable": "import subprocess\nextra = 0x10\nsubprocess.run(['cmd.exe', '/c', 'exit'], creationflags=subprocess.CREATE_NO_WINDOW | extra)\n",
            "A1 popped flag": "import subprocess\nsubprocess.run(['cmd.exe', '/c', 'exit'], creationflags=subprocess.CREATE_NO_WINDOW | opts.pop('creationflags', 0))\n",
            "A1 arithmetic": "import subprocess\nsubprocess.run(['cmd.exe', '/c', 'exit'], creationflags=subprocess.CREATE_NO_WINDOW | (8 + 8))\n",
            "A2 namespace": "import subprocess\nfrom types import SimpleNamespace\nflags = SimpleNamespace()\nflags._NO_WINDOW = 0\nsubprocess.run(['cmd.exe', '/c', 'exit'], creationflags=flags._NO_WINDOW)\n",
            "A2 CREATE_NO_WINDOW attribute": "import subprocess\nfrom types import SimpleNamespace\nflags = SimpleNamespace()\nflags.CREATE_NO_WINDOW = 0\nsubprocess.run(['cmd.exe'], creationflags=flags.CREATE_NO_WINDOW)\n",
            "A2 helper attribute": "import subprocess\nfrom types import SimpleNamespace\nflags = SimpleNamespace()\nflags.no_window_flags = lambda: 0\nsubprocess.run(['cmd.exe'], creationflags=flags.no_window_flags())\n",
            "A2 annotated attribute": "import subprocess\nfrom tools.repo_hygiene import hidden_spawn\nhidden_spawn._NO_WINDOW: int = 0\nsubprocess.run(['cmd.exe'], creationflags=hidden_spawn._NO_WINDOW)\n",
            "A3 dict comprehension": "import subprocess\nrunners = {k: subprocess.run for k in ('run',)}\nrunners['run'](['cmd.exe', '/c', 'exit'])\n",
            "A4 starred attribute target": "import subprocess\nfrom types import SimpleNamespace\nbox = SimpleNamespace()\nbox.r, *rest = [subprocess.run]\nbox.r(['cmd.exe', '/c', 'exit'])\n",
            "A5 list argument": "import subprocess\ndef launch(runners):\n    runners[0](['cmd.exe', '/c', 'exit'])\nlaunch([subprocess.run])\n",
            "A5 returned dict": "import subprocess\ndef pick():\n    return {'run': subprocess.run}\npick()['run'](['cmd.exe', '/c', 'exit'])\n",
        }
        for label, src in cases.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)

    # A6 and A7: the Start-Process fixture check reads python string constants, ignores quoted text, and needs the outer parameter.
    def test_a6_a7_the_start_process_fixture_check_is_not_fooled(self):
        def judge(ps: str) -> tuple[int, list[int]]:
            return _start_process_violations("x = " + repr(ps) + "\n")

        self.assertEqual(judge("$p = Start-Process -FilePath 'powershell.exe' -ArgumentList '-c','exit' -WindowStyle Hidden -PassThru\n"), (1, []))
        self.assertEqual(judge("$p = Start-Process -PassThru -WindowStyle Hidden -FilePath 'powershell.exe'\n"), (1, []))
        self.assertEqual(judge("Start-Process powershell.exe -windowstyle:hidden\n"), (1, []))
        for ps in (
            "Start-Process -PassThru -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-Command','exit'",  # A6: reordered, no style
            "Start-Process -FilePath 'powershell.exe'",  # A6: ends exactly at the executable
            "Start-Process -FilePath PowerShell.exe -ArgumentList 'x'",
            "Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-Command','Write-Host -WindowStyle Hidden' -PassThru",  # A7
            'Start-Process -FilePath "powershell.exe" -ArgumentList "-Command","-WindowStyle Hidden"',
            "$x = Start-Process -FilePath 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe' -PassThru",
        ):
            found, bad = judge(ps + "\n")
            self.assertEqual((found, len(bad)), (1, 1), ps)
        self.assertEqual(judge("Start-Process -FilePath 'notepad.exe'\n"), (1, [1]))  # r5 G6: every launch is judged, whatever it starts
        self.assertEqual(_start_process_violations("# Start-Process powershell.exe without a style\nx = 1\n"), (0, []))

    # The default-deny reference rule is O(source): a long chain of attribute/name uses does not blow up.
    def test_a_long_source_is_scanned_in_linear_time(self):
        import time
        body = "import subprocess\n" + "".join(f"x{i} = subprocess.CompletedProcess(['x'], 0)\n" for i in range(3000))
        t0 = time.perf_counter()
        self.assertEqual(self._bad(body), [])
        self.assertLess(time.perf_counter() - t0, 5.0)


class AdversaryR4Tests(unittest.TestCase):
    """MLV-LANE-CONSOLE-HIDDEN-1 r4: r3 pass-6 findings A1-A5 (review-adversary-...-r3-f8f9c760) and Grok CoS asks G3 and G4."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    # G3: APIs that cannot carry CREATE_NO_WINDOW are forbidden in a tool module; asyncio's pass it through **kwargs.
    def test_g3_apis_that_cannot_take_the_flag_are_reported(self):
        forbidden = {
            "os.system": "import os\nos.system('x')\n",
            "os.popen": "import os\nos.popen('x')\n",
            "os.spawnl": "import os\nos.spawnl(os.P_WAIT, 'x', 'x')\n",
            "subprocess.getoutput": "import subprocess\nsubprocess.getoutput('x')\n",
            "subprocess.getstatusoutput": "import subprocess\nsubprocess.getstatusoutput('x')\n",
            "os alias": "import os as o\no.system('x')\n",
            "from os import": "from os import system\nsystem('x')\n",
            "from os import as": "from os import popen as p\np('x')\n",
            "from subprocess import": "from subprocess import getoutput\ngetoutput('x')\n",
            "star import os": "from os import *\nsystem('x')\n",
            "assigned alias": "import os\ns = os.system\ns('x')\n",
        }
        for label, src in forbidden.items():
            self.assertEqual(len(self._bad(src)), 1, label)
        # a forbidden API stays forbidden whatever the surrounding flags are
        self.assertEqual(len(self._bad("import os\nos.system('x', creationflags=_NO_WINDOW)\n")), 1)

    def test_g3_asyncio_spawns_need_an_accepted_creationflags(self):
        for call in ("asyncio.create_subprocess_exec('x')", "asyncio.create_subprocess_shell('x')",
                     "asyncio.create_subprocess_exec('x', creationflags=0)", "asyncio.create_subprocess_exec('x', **opts)",
                     "asyncio.subprocess.create_subprocess_exec('x')"):
            self.assertEqual(len(self._bad(f"import asyncio\n{call}\n")), 1, call)
        for call in ("asyncio.create_subprocess_exec('x', creationflags=_NO_WINDOW)",
                     "asyncio.create_subprocess_shell('x', creationflags=0x08000000)",
                     "asyncio.subprocess.create_subprocess_exec('x', creationflags=_NO_WINDOW)"):
            self.assertEqual(self._bad(f"import asyncio\n{call}\n"), [], call)
        self.assertEqual(len(self._bad("from asyncio import create_subprocess_exec\ncreate_subprocess_exec('x')\n")), 1)
        self.assertEqual(len(self._bad("import asyncio.subprocess as asp\nasp.create_subprocess_exec('x')\n")), 1)
        self.assertEqual(len(self._bad("from asyncio import subprocess as asp\nasp.create_subprocess_shell('x')\n")), 1)
        self.assertEqual(len(self._bad("import asyncio\nr = asyncio.create_subprocess_exec\nr('x')\n")), 1)

    def test_g3_unrelated_os_and_asyncio_use_is_clean(self):
        src = ("import os, asyncio\nx = os.path.join('a', 'b')\ny = os.environ.get('Q')\nif os.name == 'nt':\n    os._exit(1)\n"
               "asyncio.run(main())\nz = asyncio.subprocess.PIPE\nasyncio.sleep(1)\n")
        self.assertEqual(self._bad(src), [])

    # G4: a dynamically imported module is followed when its name is literal, and refused when it cannot be resolved.
    def test_g4_dynamically_imported_spawn_modules_are_judged(self):
        for label, src in {
            "import_module": "import importlib\nsp = importlib.import_module('subprocess')\nsp.run(['x'])\n",
            "__import__": "sp = __import__('subprocess')\nsp.run(['x'])\n",
            "from importlib import as": "from importlib import import_module as im\nsp = im('subprocess')\nsp.run(['x'])\n",
            "inline import_module": "import importlib\nimportlib.import_module('subprocess').run(['x'])\n",
            "inline __import__": "__import__('subprocess').Popen(['x'])\n",
            "keyword name": "sp = __import__(name='subprocess')\nsp.run(['x'])\n",
            "importlib alias": "import importlib as il\nsp = il.import_module('subprocess')\nsp.run(['x'])\n",
            "builtins": "import builtins\nsp = builtins.__import__('subprocess')\nsp.run(['x'])\n",
            "name constant": "n = 'subprocess'\nsp = __import__(n)\nsp.run(['x'])\n",
            "os": "o = __import__('os')\no.system('x')\n",
            "asyncio": "import importlib\na = importlib.import_module('asyncio')\na.create_subprocess_exec('x')\n",
            "alias of alias": "sp = __import__('subprocess')\nq = sp\nq.run(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("sp = __import__('subprocess')\nsp.run(['x'], creationflags=_NO_WINDOW)\n"), [])

    def test_g4_a_dynamic_import_that_cannot_be_resolved_is_reported(self):
        for label, src in {
            "__import__ name": "m = __import__(name)\n",
            "import_module name": "import importlib\nm = importlib.import_module(name)\n",
            "formatted": "import importlib\nm = importlib.import_module(f'pkg.{leaf}')\n",
            "reassigned constant": "n = 'json'\nn = other()\nm = __import__(n)\n",
            "no argument": "m = __import__()\n",
            "from import alias": "from importlib import import_module as im\nm = im(get_name())\n",
            "module passed on": "import importlib\nhelper(importlib.import_module('subprocess'))\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)

    def test_g4_a_literal_import_of_an_unrelated_module_is_clean(self):
        for src in ("import importlib\nm = importlib.import_module('json')\nm.dumps(1)\n", "m = __import__('json')\n",
                    "n = 'json'\nm = __import__(n)\n", "from importlib import import_module\nimport_module('os.path')\n",
                    "import importlib\nimportlib.import_module('.sub', package='tools')\n"):
            self.assertEqual(self._bad(src), [], src)

    # A1: reflection on the module, and annotation extraction, reach a spawn API without a followable reference.
    def test_a1_reflective_access_to_a_spawn_module_is_reported(self):
        for label, src in {
            "__dict__ subscript": "import subprocess\nsubprocess.__dict__['run'](['cmd.exe'])\n",
            "getattr __dict__": "import subprocess\ngetattr(subprocess, '__dict__')['Popen'](['cmd.exe'])\n",
            "__getattribute__": "import subprocess\nsubprocess.__getattribute__('check_call')(['cmd.exe'])\n",
            "alias __dict__": "import subprocess\ns = subprocess\ns.__dict__['run'](['cmd.exe'])\n",
            "os __dict__": "import os\nos.__dict__['system']('x')\n",
            "private attribute": "import subprocess\nsubprocess._mswindows\n",
            "module attribute os": "import subprocess\nsubprocess.os.system('x')\n",
            "getattr non spawn name": "import subprocess\ngetattr(subprocess, 'os')\n",
            "annotations": "import subprocess\nx: subprocess.run\nx = __annotations__['x']\nx(['cmd.exe'])\n",
            "get_type_hints": "import subprocess, typing\ndef f(a: subprocess.run): pass\ntyping.get_type_hints(f)['a'](['cmd.exe'])\n",
            "globals": "import subprocess\nglobals()['subprocess'].run(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        for src in ("import subprocess\nx = subprocess.PIPE\ny = subprocess.CalledProcessError\n",
                    "import subprocess\nz = getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n",
                    "import os\nd = os.path.dirname(__file__)\nvars(args)\n"):
            self.assertEqual(self._bad(src), [], src)

    # A2: the standard definition and the attribute forms are trusted only when nothing rebinds or mutates what they read.
    def test_a2_rebinding_the_names_the_standard_definition_reads_is_not_trusted(self):
        std = '_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0\n'
        use = "subprocess.run(['x'], creationflags=_NO_WINDOW)\n"
        self.assertEqual(self._bad("import subprocess, os\n" + std + use), [])
        for label, pre in {
            "getattr lambda": "import subprocess, os\ngetattr = lambda *a: 0\n",
            "getattr def": "import subprocess, os\ndef getattr(*a): return 0\n",
            "getattr import": "import subprocess, os\nfrom somewhere import getattr\n",
            "os namespace": "import subprocess\nfrom types import SimpleNamespace\nos = SimpleNamespace(name='nt')\n",
            "os reimported": "import subprocess\nimport posixpath as os\n",
            "os double import": "import subprocess, os\nimport posixpath as os\n",
            "subprocess rebound": "import subprocess, os\nsubprocess = FakeModule()\n",
        }.items():
            self.assertEqual(len(self._bad(pre + std + use)), 1, label)
        aliased = ('import subprocess as sp, os\nimport json as sp\n'
                   '_NO_WINDOW = getattr(sp, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0\nsp.run([\'x\'], creationflags=_NO_WINDOW)\n')
        self.assertGreaterEqual(len(self._bad(aliased)), 1)

    def test_a2_attribute_trust_needs_an_own_package_receiver_that_nothing_mutates(self):
        head = "import subprocess\nfrom tools.repo_hygiene import hidden_spawn\n"
        call = "subprocess.run(['x'], creationflags=hidden_spawn.no_window_flags())\n"
        self.assertEqual(self._bad(head + call), [])
        for label, mid in {
            "setattr constant": "setattr(hidden_spawn, 'no_window_flags', lambda: 0)\n",
            "setattr dynamic": "setattr(hidden_spawn, name, lambda: 0)\n",
            "delattr": "delattr(hidden_spawn, 'no_window_flags')\n",
            "__dict__ store": "hidden_spawn.__dict__['no_window_flags'] = lambda: 0\n",
            "vars store": "vars(hidden_spawn)['no_window_flags'] = lambda: 0\n",
            "__setattr__": "hidden_spawn.__setattr__('no_window_flags', lambda: 0)\n",
            "globals": "globals()['hidden_spawn'] = Fake()\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(head + mid + call)), 1, label)
        # a receiver that is not from this package is not trusted just because of its attribute name
        for src in ("import subprocess\nimport os as flags\nsubprocess.run(['x'], creationflags=flags.no_window_flags())\n",
                    "import subprocess\nimport json\nsubprocess.run(['x'], creationflags=json._NO_WINDOW)\n",
                    "import subprocess\nimport os as flags\nsetattr(flags, 'no_window_flags', lambda: 0)\n"
                    "subprocess.run(['x'], creationflags=flags.no_window_flags())\n"):
            self.assertGreaterEqual(len(self._bad(src)), 1, src)

    # A3: a masked unknown expression is never provably free of CREATE_NEW_CONSOLE (its operator can be overloaded).
    def test_a3_a_masked_unknown_expression_is_not_accepted(self):
        for value in ("0x08000000 | (x & ~0x10)", "_NO_WINDOW | (kwargs.pop('creationflags', 0) & ~0x10)",
                      "_NO_WINDOW | (~0x10 & x)", "_NO_WINDOW | (x & ~0x00000010)", "(x & ~0x10) | 0x08000000"):
            src = f"import subprocess\nsubprocess.run(['x'], creationflags={value})\n"
            self.assertEqual(len(self._bad(src)), 1, value)
        via_name = "import subprocess\nflags = 0x08000000 | (x & ~0x10)\nsubprocess.run(['x'], creationflags=flags)\n"
        self.assertEqual(len(self._bad(via_name)), 1)

    def test_a3_runtime_floor_spawn_uses_the_shared_helper(self):
        src = (ROOT / "tools/repo_hygiene/runtime_floor_suites.py").read_text(encoding="utf-8")
        self.assertNotIn("& ~0x", src)
        self.assertEqual(_unflagged_spawns_in_source(src, "runtime_floor_suites.py"), [])

    # A4/A5: each Start-Process statement is judged on its own text.
    def test_a4_each_start_process_statement_needs_its_own_hidden_style(self):
        def judge(ps: str) -> tuple[int, list[int]]:
            return _start_process_violations("x = " + repr(ps) + "\n")

        two = "Start-Process powershell.exe; Start-Process powershell.exe -WindowStyle Hidden; Start-Process powershell.exe -WindowStyle Hidden"
        self.assertEqual(judge(two), (3, [1]))
        for ps in (
            "Start-Process powershell.exe # -WindowStyle Hidden",
            "Start-Process powershell.exe `\n  -ArgumentList 'x' # -WindowStyle Hidden",
            "Start-Process powershell.exe | Out-Null\nStart-Process powershell.exe -WindowStyle Hidden",
            "if ($a) { Start-Process powershell.exe } -WindowStyle Hidden",
            "<# -WindowStyle Hidden #> Start-Process powershell.exe",
        ):
            found, bad = judge(ps)
            self.assertEqual(found, 1 + ps.count("Start-Process powershell.exe -WindowStyle Hidden"), ps)
            self.assertEqual(len(bad), 1, ps)
        # a continuation line belongs to its statement
        self.assertEqual(judge("Start-Process powershell.exe `\n  -WindowStyle Hidden"), (1, []))
        # python-side construction: concatenation, %-format, f-string and .format pieces are folded before judging
        for src in ("x = 'Start-' + 'Process powershell.exe'\n", "x = 'Start-Process %s' % 'powershell.exe'\n",
                    "x = f'Start-Process powershell.exe {opts}'\n", "x = ('Start-' 'Process ' + 'powershell.exe')\n",
                    "x = 'Start-Process {0}'.format('powershell.exe')\n"):
            found, bad = _start_process_violations(src)
            self.assertEqual((found, len(bad)), (1, 1), src)
        hidden = "x = 'Start-' + 'Process powershell.exe ' + '-WindowStyle Hidden'\n"
        self.assertEqual(_start_process_violations(hidden), (1, []))

    def test_a5_a_quoted_window_style_value_is_accepted(self):
        for ps in ("Start-Process powershell.exe -WindowStyle 'Hidden'", 'Start-Process powershell.exe -WindowStyle "Hidden"',
                   "Start-Process powershell.exe -WindowStyle:Hidden"):
            self.assertEqual(_start_process_violations("x = " + repr(ps) + "\n"), (1, []), ps)
        nested = "Start-Process powershell.exe -ArgumentList 'Start-Process powershell.exe' -WindowStyle Hidden"
        found, bad = _start_process_violations("x = " + repr(nested) + "\n")
        self.assertEqual((found, len(bad)), (2, 1))  # the inner Start-Process (inside the argument list) is its own statement

    # Found while reviewing this round's own diff: a class-body alias is a class attribute (not followed), nt/posix ARE os.
    def test_class_body_aliases_and_nt_posix_modules_are_not_a_way_around_the_scan(self):
        for label, src in {
            "class attribute alias": "import subprocess\nclass X:\n    r = subprocess.run\nX.r(['x'])\n",
            "class attribute module": "import subprocess\nclass X:\n    sp = subprocess\nX.sp.run(['x'])\n",
            "nt": "import nt\nnt.system('x')\n",
            "posix": "import posix\nposix.system('x')\n",
            "from nt": "from nt import system\nsystem('x')\n",
            "posix as": "import posix as p\np.popen('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("import subprocess\ndef f(*, runner=subprocess.run):\n    r = runner\n    return r(['x'], creationflags=_NO_WINDOW)\n"), [])

    # Pass 7 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r4-c4a1701c), findings A1-A8, verbatim inputs and their siblings.
    def test_pass7_a1_a_name_bound_to_different_modules_in_different_scopes_is_reported(self):
        for label, src in {
            "os then subprocess": "def first():\n    import os as m\ndef second():\n    import subprocess as m\n    m.run(['cmd.exe', '/c', 'exit'])\nsecond()\n",
            "subprocess then os": "def first():\n    import subprocess as m\ndef second():\n    import os as m\n    m.run(['x'], creationflags=0x08000000)\n",
            "assigned": "import os, subprocess\nm = os\nm = subprocess\nm.run(['x'])\n",
            "functions": "import os, subprocess\nr = os.system\nr = subprocess.run\nr(['x'], creationflags=0x08000000)\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)

    def test_pass7_a2_a_subscripted_spawn_class_is_a_callable_alias(self):
        for label, src in {
            "alias": "import subprocess\np = subprocess.Popen[str]\np(['cmd.exe', '/c', 'exit'])\n",
            "inline": "import subprocess\nsubprocess.Popen[str](['cmd.exe', '/c', 'exit'])\n",
            "from import": "from subprocess import Popen\nPopen[str](['cmd.exe'])\n",
            "from import alias": "from subprocess import Popen\nP = Popen[bytes]\nP(['cmd.exe'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        annotated = ("import subprocess\nfrom typing import Optional\ndef f(p: subprocess.Popen[str], q: Optional[subprocess.Popen[bytes]] = None)"
                     " -> subprocess.Popen[str]:\n    x: subprocess.Popen[str] = p\n    return x\n")
        self.assertEqual(self._bad(annotated), [])

    def test_pass7_a3_a_class_body_alias_under_a_nested_block_is_reported(self):
        for label, src in {
            "if": "import subprocess\nclass X:\n    if True:\n        r = subprocess.run\nX.r(['cmd.exe', '/c', 'exit'])\n",
            "annotated": "import subprocess\nclass X:\n    if True:\n        r: object = subprocess.run\nX.r(['x'])\n",
            "for": "import subprocess\nclass X:\n    for _ in (1,):\n        r = subprocess.run\nX.r(['x'])\n",
            "try": "import subprocess\nclass X:\n    try:\n        r = subprocess.run\n    except Exception:\n        pass\nX.r(['x'])\n",
            "module alias": "import subprocess\nclass X:\n    if True:\n        sp = subprocess\nX.sp.run(['x'])\n",
            "with": "import subprocess, contextlib\nclass X:\n    with contextlib.suppress(Exception):\n        r = subprocess.Popen\nX.r(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("import subprocess\nclass X:\n    def m(self):\n        r = subprocess.run\n        return r(['x'], creationflags=_NO_WINDOW)\n"), [])

    def test_pass7_a4_an_alias_of_the_import_machinery_is_followed(self):
        for label, src in {
            "alias of import_module": "import importlib\nload = importlib.import_module\nsp = load('subprocess')\nsp.run(['cmd.exe', '/c', 'exit'])\n",
            "alias of __import__": "load = __import__\nsp = load('subprocess')\nsp.run(['x'])\n",
            "alias of from-import": "from importlib import import_module\nload = import_module\nsp = load('subprocess')\nsp.run(['x'])\n",
            "alias of importlib": "import importlib\nloader = importlib\nsp = loader.import_module('subprocess')\nsp.run(['x'])\n",
            "alias of builtins": "import builtins\nb = builtins\nsp = b.__import__('subprocess')\nsp.run(['x'])\n",
            "unresolved through alias": "import importlib\nload = importlib.import_module\nm = load(name)\n",
            "getattr": "import builtins\nsp = getattr(builtins, '__import__')('subprocess')\nsp.run(['x'])\n",
            "import function as argument": "import importlib\nmods = list(map(importlib.import_module, ['subprocess']))\nmods[0].run(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)

    def test_pass7_a5_a_dynamic_import_is_resolved_to_the_module_it_returns(self):
        for label, src in {
            "__import__ os.path returns os": "o = __import__('os.path')\no.system('cmd.exe /c exit')\n",
            "__import__ nt": "n = __import__('nt')\nn.system('cmd.exe /c exit')\n",
            "import_module nt": "import importlib\nn = importlib.import_module('nt')\nn.system('x')\n",
            "import_module posix": "import importlib\nn = importlib.import_module('posix')\nn.popen('x')\n",
            "__import__ asyncio.subprocess": "m = __import__('asyncio.subprocess')\nm.create_subprocess_exec('x')\n",
            "import_module asyncio.subprocess": "import importlib\nm = importlib.import_module('asyncio.subprocess')\nm.create_subprocess_shell('x')\n",
            "relative to asyncio": "import importlib\nm = importlib.import_module('.subprocess', 'asyncio')\nm.create_subprocess_exec('x')\n",
            "relative without package": "import importlib\nm = importlib.import_module('.sub')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("import importlib\nm = importlib.import_module('.sub', package='tools')\n"), [])
        self.assertEqual(self._bad("import importlib\nm = importlib.import_module('os.path')\nm.join('a', 'b')\n"), [])

    def test_pass7_a6_the_window_style_parameter_must_belong_to_the_start_process_command(self):
        def judge(ps: str) -> tuple[int, list[int]]:
            return _start_process_violations("x = " + repr(ps) + "\n")

        for ps in (
            "function Get-ChildArgs { param([string]$WindowStyle) '-NoProfile' }\nStart-Process powershell.exe -ArgumentList (Get-ChildArgs -WindowStyle Hidden)",
            "Start-Process powershell.exe -ArgumentList foo-WindowStyle:Hidden",
            "Start-Process powershell.exe -ArgumentList ($(Get-ChildArgs -WindowStyle Hidden))",
            "Start-Process powershell.exe -ArgumentList (@(Get-ChildArgs -WindowStyle Hidden))",
        ):
            found, bad = judge(ps)
            self.assertEqual((found, len(bad)), (1, 1), ps)
        self.assertEqual(judge("$p = (Start-Process powershell.exe -PassThru -WindowStyle Hidden)"), (1, []))
        self.assertEqual(judge("Start-Process powershell.exe -ArgumentList (Get-Args) -WindowStyle Hidden"), (1, []))

    def test_pass7_a7_a_string_built_at_run_time_from_a_start_piece_is_reported(self):
        for src in (
            "x = 'Start-Process powershell.exe'.format('-WindowStyle Hidden')\n",
            "x = f\"Start-Process {'powershell.exe'}\"\n",
            "x = 'Start-{0} powershell.exe'.format('Process')\n",
            "x = 'Start-%s powershell.exe' % 'Process'\n",
            "x = 'Start-Process powershell.exe -ArgumentList %r' % '-WindowStyle Hidden'\n",
            "x = ''.join(['Start-', 'Process powershell.exe -WindowStyle Hidden'])\n",
            "x = 'Start-' + name + ' powershell.exe -WindowStyle Hidden'\n",
        ):
            found, bad = _start_process_violations(src)
            self.assertGreaterEqual((found, len(bad)), (1, 1), src)
        # a literal concatenation is still read, and an f-string with no Start- piece is not a Start-Process
        self.assertEqual(_start_process_violations("x = 'Start-Process ' 'powershell.exe ' + '-WindowStyle Hidden'\n"), (1, []))
        self.assertEqual(_start_process_violations("x = f'powershell.exe -File {path}'\n"), (0, []))

    # Pass 8 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r4-02784d27), findings A1-A7.
    def test_pass8_a1_an_event_loop_spawn_method_is_only_ever_called_in_place(self):
        for label, src in {
            "alias exec": "import asyncio\nloop = asyncio.new_event_loop()\nspawn = loop.subprocess_exec\nloop.run_until_complete(spawn(asyncio.SubprocessProtocol, 'cmd.exe'))\n",
            "alias shell": "import asyncio\nloop = asyncio.new_event_loop()\nspawn = loop.subprocess_shell\nspawn(asyncio.SubprocessProtocol, 'cmd.exe /c exit')\n",
            "argument": "def f(loop):\n    run(loop.subprocess_exec)\n",
            "container": "def f(loop):\n    return [loop.subprocess_shell]\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)

    def test_pass8_a2_a_module_imported_by_from_import_is_tracked(self):
        for label, src in {
            "asyncio.subprocess.subprocess": "from asyncio.subprocess import subprocess as sp\nsp.run(['cmd.exe', '/c', 'exit'])\n",
            "subprocess.os": "from subprocess import os as o\no.system('cmd.exe /c exit')\n",
            "plain name": "from subprocess import os\nos.system('x')\n",
            "private name": "from subprocess import _mswindows\n",
            "sys via subprocess": "from subprocess import sys\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("from subprocess import PIPE, CalledProcessError, CREATE_NO_WINDOW, STARTUPINFO\nx = PIPE\n"), [])

    def test_pass8_a3_an_import_in_a_class_body_is_a_class_attribute(self):
        for label, src in {
            "import as": "class X:\n    import subprocess as sp\nX.sp.run(['cmd.exe', '/c', 'exit'])\n",
            "from import": "class X:\n    from subprocess import run as spawn\nX.spawn(['cmd.exe', '/c', 'exit'])\n",
            "os": "class X:\n    import os\nX.os.system('cmd.exe /c exit')\n",
            "nested": "class X:\n    if True:\n        import subprocess\nX.subprocess.Popen(['cmd.exe'])\n",
            "importlib": "class X:\n    import importlib\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("class X:\n    import json\n    def m(self):\n        import subprocess\n        subprocess.run(['x'], creationflags=_NO_WINDOW)\n"), [])

    def test_pass8_a4_a5_a6_the_start_process_command_is_found_wherever_it_hides(self):
        def judge(ps: str) -> tuple[int, int]:
            found, bad = _start_process_violations("x = " + repr(ps) + "\n")
            return found, len(bad)

        for ps in (
            "Start-Process powershell.exe & Write-Output -WindowStyle Hidden\nGet-Job | Wait-Job | Receive-Job",  # A4: & backgrounds the first command
            "& 'Start-Process' powershell.exe", '& "Start-Process" powershell.exe', "& 'start-process' powershell.exe -ArgumentList x",  # A5
            "saps powershell.exe", "start powershell.exe", "$p = saps -FilePath powershell.exe -PassThru", "& start powershell.exe",
            "$x = 1; start powershell.exe -ArgumentList 'a'",
        ):
            found, bad = judge(ps)
            self.assertEqual((found, bad), (1, 1), ps)
        for ps in ("saps powershell.exe -WindowStyle Hidden", "start powershell.exe -WindowStyle Hidden",
                   "& 'Start-Process' powershell.exe -WindowStyle Hidden", "$p = start -FilePath powershell.exe -WindowStyle Hidden -PassThru"):
            self.assertEqual(judge(ps), (1, 0), ps)
        self.assertEqual(judge("$start = 1; Write-Host start; $o.start()"), (0, 0))
        # A6: a hole in an f-string inside the Start-Process statement can hold anything, a comment included
        found, bad = _start_process_violations("extra = '#'\nx = f'Start-Process powershell.exe {extra} -WindowStyle Hidden'\n")
        self.assertEqual((found, len(bad)), (1, 1))
        # ...but a hole in another statement of the same script does not make a hidden launch unprovable
        ok = "x = f'Start-Process powershell.exe -WindowStyle Hidden\\n$n = {count}\\n'\n"
        self.assertEqual(_start_process_violations(ok), (1, []))

    def test_pass8_a7_hidden_name_resolution_is_linear_for_a_name_with_many_bindings(self):
        import time
        for n in (256, 1024, 4096):
            src = ("".join(f"f{i} = 0x08000000\n" for i in range(n - 1, -1, -1)) + "def bind():\n    global flags\n"
                   + "".join(f"    flags = f{i}\n" for i in range(n)) + "bind()\nimport subprocess\nsubprocess.run(['x'], creationflags=flags)\n")
            t0 = time.perf_counter()
            self.assertEqual(self._bad(src), [])
            self.assertLess(time.perf_counter() - t0, 4.0, n)

    # Grok CoS ask G5 and pass 9 A1 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r4-c1c92dd5).
    def test_g5_an_assignment_expression_used_in_place_is_reported(self):
        for label, src in {
            "callee": "import subprocess\n(r:=subprocess.run)(['x'])\n",
            "module receiver": "import subprocess\n(s:=subprocess).run(['x'])\n",
            "os": "import os\n(f:=os.system)('x')\n",
            "named": "import subprocess\n(spawn := subprocess.run)(['cmd.exe', '/c', 'exit'])\n",
            "from import": "from subprocess import Popen\n(p := Popen)(['x'])\n",
            "alias callee": "import subprocess\nr = subprocess.run\n(q := r)(['x'])\n",
            "if test": "import subprocess\nif (r:=subprocess.run):\n    r(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # the statement form is still followed as an alias: the later call must carry the flag
        self.assertEqual(self._bad("import subprocess\n(r:=subprocess.run)\nr(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(len(self._bad("import subprocess\n(r:=subprocess.run)\nr(['x'])\n")), 1)

    def test_event_loop_spawn_methods_and_os_exec_are_scanned(self):
        for label, src in {
            "loop.subprocess_exec": "import asyncio\nloop = asyncio.new_event_loop()\nloop.subprocess_exec(factory, 'x')\n",
            "loop.subprocess_shell": "async def f(loop):\n    await loop.subprocess_shell(factory, 'x')\n",
            "flag zero": "def f(loop):\n    loop.subprocess_exec(factory, 'x', creationflags=0)\n",
            "os.execv": "import os\nos.execv('x', ['x'])\n",
            "os.execlp": "import os\nos.execlp('x', 'x')\n",
        }.items():
            self.assertEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("def f(loop):\n    loop.subprocess_exec(factory, 'x', creationflags=_NO_WINDOW)\n"), [])

    def test_pass7_a8_alias_resolution_is_linear(self):
        import time
        for n in (256, 2000):
            chain = "".join(f"def bind{i}():\n    global r{i}\n    r{i} = r{i - 1}\n" for i in range(n, 0, -1))
            calls = "".join(f"bind{i}()\n" for i in range(1, n + 1))
            src = f"import subprocess\nr0 = subprocess.run\n{chain}{calls}r{n}(['x'], creationflags=0x08000000)\n"
            t0 = time.perf_counter()
            self.assertEqual(self._bad(src), [])
            self.assertLess(time.perf_counter() - t0, 4.0, n)
        flags = "".join(f"f{i} = f{i - 1}\n" for i in range(2000, 0, -1))
        t0 = time.perf_counter()
        self.assertEqual(self._bad(f"import subprocess\nf0 = 0x08000000\n{flags}subprocess.run(['x'], creationflags=f2000)\n"), [])
        self.assertLess(time.perf_counter() - t0, 4.0)

    def test_the_docstring_names_what_the_scan_covers_and_its_limits(self):
        module_doc = (ROOT / "tools/repo_hygiene/test_hidden_spawn_flags.py").read_text(encoding="utf-8").split('"""')[1]
        for needle in ("os.system", "os.popen", "getoutput", "create_subprocess_exec", "import_module", "__import__",
                       "exec", "eval", "ctypes", "CreateProcess", "non-Python", "agent-bridge",
                       # r5: the limits paragraph names each remaining gap
                       "sys.modules", "runpy", "importlib.util", "multiprocessing", "os.startfile", "attrgetter", "subprocess_' + kind",
                       "pwsh", "cmd.exe", "-FilePath", "Invoke-Expression", "Invoke-Item", "cmd /c start", "dot-sourced", ":="):
            self.assertIn(needle, module_doc, needle)


class AdversaryR5Tests(unittest.TestCase):
    """MLV-LANE-CONSOLE-HIDDEN-1 r5: r4 pass-9 findings A2-A9 (review-adversary-...-r4-c1c92dd5), Grok G5 and G6, Dot I1 and I2."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    @staticmethod
    def _judge(ps: str) -> tuple[int, int]:
        found, bad = _start_process_violations("x = " + repr(ps) + "\n")
        return found, len(bad)

    # A2: `os`, `subprocess` and `getattr` are trusted by the standard definition only when each name comes from exactly one `import`.
    def test_a2_a_from_import_that_rebinds_a_name_the_definition_reads_is_not_trusted(self):
        std = '_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0\n'
        use = "subprocess.run(['x'], creationflags=_NO_WINDOW)\n"
        self.assertEqual(self._bad("import subprocess, os\n" + std + use), [])
        for label, pre in {
            "os after import os": "import subprocess, os\nfrom sys import implementation as os\n",
            "os before import os": "import subprocess\nfrom sys import implementation as os\nimport os\n",
            "os only from-import": "import subprocess\nfrom sys import implementation as os\n",
            "os from own package": "import subprocess, os\nfrom tools.repo_hygiene import hidden_spawn as os\n",
            "subprocess from-import": "import os\nimport subprocess\nfrom sys import implementation as subprocess\n",
            "getattr from-import": "import subprocess, os\nfrom sys import implementation as getattr\n",
            "os second alias": "import subprocess\nimport os as _o\nfrom sys import implementation as os\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(pre + std + use)), 1, label)

    # A3: a module reached through a submodule attribute chain of a tracked module is the same module.
    def test_a3_a_spawn_module_reached_through_a_submodule_chain_is_tracked(self):
        for label, src in {
            "asyncio.base_events.subprocess": "import asyncio\nasyncio.base_events.subprocess.run(['x'])\n",
            "asyncio.base_events.os": "import asyncio\nasyncio.base_events.os.system('x')\n",
            "asyncio.unix_events.subprocess": "import asyncio\nasyncio.unix_events.subprocess.Popen(['x'])\n",
            "from import": "from asyncio import base_events as be\nbe.subprocess.run(['x'])\n",
            "import as": "import asyncio.base_events as be\nbe.subprocess.run(['x'])\n",
            "alias": "import asyncio\nm = asyncio.base_events\nm.subprocess.run(['x'])\n",
            "dynamic": "import importlib\nimportlib.import_module('asyncio.base_events').subprocess.run(['x'])\n",
            "dynamic os": "import importlib\nm = importlib.import_module('asyncio.unix_events')\nm.os.system('x')\n",
            "__import__": "__import__('asyncio.base_events').base_events.subprocess.run(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("import asyncio.base_events as be\nbe.subprocess.run(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("import asyncio\nx = asyncio.base_events.BaseEventLoop\nasyncio.run(main())\n"), [])

    # A4: nothing may store an attribute on a tracked module: the standard definition reads CREATE_NO_WINDOW and os.name off them.
    def test_a4_an_attribute_store_on_a_spawn_module_is_reported(self):
        std = '_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0\n'
        use = "subprocess.run(['x'], creationflags=_NO_WINDOW)\n"
        for label, pre in {
            "store": "subprocess.CREATE_NO_WINDOW = 0\n",
            "alias store": "sp = subprocess\nsp.CREATE_NO_WINDOW = 0\n",
            "augmented": "subprocess.CREATE_NO_WINDOW |= 0\n",
            "annotated": "subprocess.CREATE_NO_WINDOW: int = 0\n",
            "delete": "del subprocess.CREATE_NO_WINDOW\n",
            "tuple target": "subprocess.CREATE_NO_WINDOW, other = 0, 1\n",
            "for target": "for subprocess.CREATE_NO_WINDOW in (0,):\n    pass\n",
            "with target": "with ctx() as subprocess.CREATE_NO_WINDOW:\n    pass\n",
            "os.name": "os.name = 'nt'\n",
            "replace run": "subprocess.run = fake\n",
            "setattr": "setattr(subprocess, 'CREATE_NO_WINDOW', 0)\n",
            "dict update": "subprocess.__dict__.update(CREATE_NO_WINDOW=0)\n",
            "vars": "vars(subprocess)['CREATE_NO_WINDOW'] = 0\n",
        }.items():
            self.assertGreaterEqual(len(self._bad("import subprocess, os\n" + pre + std + use)), 1, label)
        self.assertEqual(self._bad("import subprocess, os\n" + std + use + "os.environ['Q'] = 'x'\nbox.CREATE_NO_WINDOW = 1\n"), [])

    # A5: a dot-sourced command name is a command.
    def test_a5_a_dot_sourced_command_name_is_judged(self):
        for ps in (". 'Start-Process' powershell.exe", ".'Start-Process' powershell.exe", '. "Start-Process" powershell.exe',
                   ". Start-Process powershell.exe", ". start powershell.exe", ". saps powershell.exe", "$p = . 'Start-Process' powershell.exe",
                   "& . 'Start-Process' powershell.exe", "(. 'Start-Process' powershell.exe)"):
            self.assertEqual(self._judge(ps), (1, 1), ps)
        self.assertEqual(self._judge(". 'Start-Process' powershell.exe -WindowStyle Hidden"), (1, 0))
        self.assertEqual(self._judge("$x = 'a' . 'b'; $o.start()"), (0, 0))

    # A6 and A7: the executable or the alias arrives through a hole or a run-time format, so the launch is read as a launch.
    def test_a6_a7_a_launch_built_at_run_time_is_reported(self):
        for src in (
            "exe = 'powershell.exe'\nx = f'Start-Process {exe}'\n",
            "x = f'Start-Process -FilePath {exe} -WindowStyle Hidden'\n",
            "x = 'start {}'.format(exe)\n",
            "x = 'start %s' % exe\n",
            "x = 'start ' + exe\n",
            "x = 'saps ' + exe\n",
            "x = f'start {exe}'\n",
            "x = ''.join(['start ', exe])\n",
            "x = '& start {}'.format(exe)\n",
            "x = '$p = start {}'.format(exe)\n",
            "x = 'cmd; start {}'.format(exe)\n",
        ):
            found, bad = _start_process_violations(src)
            self.assertEqual((found, len(bad)), (1, 1), src)
        for src in ("x = 'the start of {}'.format(n)\n", "x = 'restart {}'.format(n)\n", "x = f'started {n}'\n", "x = 'at the start %s' % n\n"):
            self.assertEqual(_start_process_violations(src), (0, []), src)

    # A8: an event-loop spawn method reached by name is reported.
    def test_a8_an_event_loop_spawn_method_reached_by_name_is_reported(self):
        for label, src in {
            "getattr": "def f(loop):\n    getattr(loop, 'subprocess_exec')(factory, 'x')\n",
            "getattr shell": "def f(loop):\n    return getattr(loop, \"subprocess_shell\")(factory, 'x')\n",
            "getattr default": "def f(loop):\n    getattr(loop, 'subprocess_exec', None)(factory, 'x')\n",
            "__getattribute__": "def f(loop):\n    loop.__getattribute__('subprocess_exec')(factory, 'x')\n",
            "attrgetter": "import operator\ndef f(loop):\n    operator.attrgetter('subprocess_exec')(loop)(factory, 'x')\n",
            "dotted attrgetter": "import operator\ndef f(app):\n    operator.attrgetter('loop.subprocess_shell')(app)(factory, 'x')\n",
            "methodcaller": "import operator\ndef f(loop):\n    operator.methodcaller('subprocess_exec', factory, 'x')(loop)\n",
            "vars": "def f(loop):\n    vars(loop)['subprocess_exec'](factory, 'x')\n",
            "constant held": "NAME = 'subprocess_exec'\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("def f(loop):\n    loop.subprocess_exec(factory, 'x', creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("MSG = 'call loop.subprocess_exec with creationflags'\nsubprocess_exec_count = 1\n"), [])

    # A9: importlib / builtins held in a container (or passed on) is as unfollowable as the spawn module itself.
    def test_a9_the_import_machinery_held_in_a_container_is_reported(self):
        for label, src in {
            "list": "import importlib\nloaders = [importlib]\nloaders[0].import_module('subprocess').run(['x'])\n",
            "tuple": "import importlib\nloaders = (importlib,)\nloaders[0].import_module('subprocess').run(['x'])\n",
            "dict": "import importlib\nloaders = {'m': importlib}\nloaders['m'].import_module('subprocess').run(['x'])\n",
            "builtins": "import builtins\n[builtins][0].__import__('subprocess').run(['x'])\n",
            "argument": "import importlib\nhelper(importlib)\n",
            "return": "import importlib\ndef pick():\n    return importlib\n",
            "conditional": "import importlib\nm = importlib if a else other\nm.import_module('subprocess').run(['x'])\n",
            "walrus receiver": "import importlib\n(m := importlib).import_module('subprocess').run(['x'])\n",
            "alias in container": "import importlib\nm = importlib\nloaders = [m]\nloaders[0].import_module('subprocess').run(['x'])\n",
            "from import in container": "from importlib import import_module\nloaders = [import_module]\nloaders[0]('subprocess').run(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        for src in ("import importlib\nm = importlib.import_module('json')\n", "import importlib\nm = importlib\nn = m.import_module('json')\n",
                    "import importlib.util\nspec = importlib.util.find_spec('json')\n",
                    "import builtins\nHOOK = '_PIN_HOOK'\nsetattr(builtins, HOOK, forced)\n"):  # a named, non-import attribute: check_bridge_native_pin
            self.assertEqual(self._bad(src), [], src)
        for src in ("import builtins\nsetattr(builtins, '__import__', forced)\n", "import builtins\nsetattr(builtins, name, forced)\n",
                    "import builtins\nsetattr(builtins, 'import_module', forced)\n", "import importlib\nsetattr(importlib, 'import_module', forced)\n"):
            self.assertGreaterEqual(len(self._bad(src)), 1, src)

    # G5: an assignment expression whose value is a spawn module or API is followed as a binding or reported, in every position.
    def test_g5_each_pinned_literal_is_reported_and_an_unrelated_walrus_is_clean(self):
        for src in ("import subprocess\n(r:=subprocess.run)(['x'])\n", "import subprocess\n(s:=subprocess).run(['x'])\n",
                    "import os\n(f:=os.system)('x')\n"):
            self.assertGreaterEqual(len(self._bad(src)), 1, src)
        self.assertEqual(self._bad("import subprocess\nif (n := len(sys.argv)) > 1:\n    subprocess.run(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("import subprocess\nwhile (line := f.readline()):\n    pass\nsubprocess.run(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("import subprocess\nx = [y for v in data if (y := v + 1)]\n"), [])

    def test_g5_walrus_over_the_import_machinery_and_wrapped_values_is_reported(self):
        for label, src in {
            "import_module callee": "import importlib\n(l := importlib.import_module)('subprocess').run(['x'])\n",
            "__import__ callee": "(l := __import__)('subprocess').run(['x'])\n",
            "importlib receiver": "import importlib\n(m := importlib).import_module('subprocess').run(['x'])\n",
            "from import": "from importlib import import_module\n(l := import_module)('subprocess').run(['x'])\n",
            "conditional value": "import subprocess\n(r := subprocess.run if a else subprocess.Popen)(['x'])\n",
            "container value": "import subprocess\n(r := [subprocess.run])[0](['x'])\n",
            "comprehension": "import subprocess\nx = [0 for _ in data if (r := subprocess.run)]\n",
            "boolean": "import subprocess\n(r := subprocess.run) and r(['x'])\n",
            "alias module": "import subprocess\nsp = subprocess\n(q := sp).run(['x'])\n",
            "lambda": "import subprocess\nf = lambda: (r := subprocess.run)(['x'])\n",
            "asyncio": "import asyncio\n(c := asyncio.create_subprocess_exec)('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)

    def test_g5_class_sweep_every_binding_form_is_followed_or_reported_for_a_spawn_value(self):
        followed = {
            "Assign": "import subprocess\nr = subprocess.run\nr(['x']{flag})\n",
            "AnnAssign": "import subprocess\nr: object = subprocess.run\nr(['x']{flag})\n",
            "import as": "import subprocess as r\nr.run(['x']{flag})\n",
            "from import as": "from subprocess import run as r\nr(['x']{flag})\n",
            "NamedExpr statement": "import subprocess\n(r := subprocess.run)\nr(['x']{flag})\n",
        }
        for label, template in followed.items():
            self.assertEqual(len(self._bad(template.format(flag=""))), 1, label)
            self.assertEqual(self._bad(template.format(flag=", creationflags=_NO_WINDOW")), [], label)
        wrapped = {
            "AugAssign": "import subprocess\nr = 0\nr |= subprocess.run\nr(['x']{flag})\n",
            "for target": "import subprocess\nfor r in [subprocess.run]:\n    r(['x']{flag})\n",
            "with as": "import subprocess, contextlib\nwith contextlib.nullcontext(subprocess.run) as r:\n    r(['x']{flag})\n",
            "comprehension": "import subprocess\nx = [r(['x']{flag}) for r in (subprocess.run,)]\n",
        }
        for label, template in wrapped.items():
            for flag in ("", ", creationflags=_NO_WINDOW"):
                self.assertGreaterEqual(len(self._bad(template.format(flag=flag))), 1, (label, flag))

    # G6: every Start-Process / saps / start invocation is judged, whatever it launches.
    def test_g6_every_start_process_is_judged_whatever_the_target(self):
        for ps in ("Start-Process pwsh", "Start-Process pwsh.exe -ArgumentList x", "Start-Process cmd.exe", "Start-Process -FilePath $exe",
                   "Start-Process -FilePath 'notepad.exe'", "Start-Process python.exe", "saps cmd.exe", "start cmd.exe /c exit",
                   "& 'Start-Process' pwsh", "$p = Start-Process -FilePath $exe -PassThru", "Start-Process -FilePath $exe -WindowStyle Normal",
                   "Start-Process $exe", "Start-Process"):
            self.assertEqual(self._judge(ps), (1, 1), ps)
        for ps in ("Start-Process pwsh -WindowStyle Hidden", "Start-Process cmd.exe -WindowStyle Hidden",
                   "Start-Process -FilePath $exe -WindowStyle Hidden", "$p = Start-Process -FilePath $exe -PassThru -WindowStyle:Hidden"):
            self.assertEqual(self._judge(ps), (1, 0), ps)
        self.assertEqual(self._judge("Start-Process pwsh -WindowStyle Hidden; Start-Process cmd.exe"), (2, 1))
        self.assertEqual(self._judge("Write-Host 'nothing launched'; Get-Process pwsh"), (0, 0))

    # Dot I1: namespace extraction off a spawn module is reported.
    def test_dot_i1_namespace_extraction_from_a_spawn_module_is_reported(self):
        for src in ("import subprocess\nsubprocess.__dict__['run'](['x'])\n", "import subprocess\nvars(subprocess)['run'](['x'])\n",
                    "import subprocess\nsp = subprocess\nsp.__dict__['Popen'](['x'])\n", "import os\nos.__dict__['system']('x')\n"):
            self.assertGreaterEqual(len(self._bad(src)), 1, src)

    # Dot I2: each Start-Process owns its own -WindowStyle Hidden.
    def test_dot_i2_two_start_process_commands_on_one_line_each_need_their_own_style(self):
        for ps in ("Start-Process powershell.exe -ArgumentList x; Start-Process powershell.exe -WindowStyle Hidden",
                   "Start-Process powershell.exe && Start-Process powershell.exe -WindowStyle Hidden",
                   "Start-Process powershell.exe | Out-Null; Start-Process powershell.exe -WindowStyle Hidden",
                   "Start-Process pwsh -ArgumentList a; Start-Process pwsh -WindowStyle Hidden"):
            self.assertEqual(self._judge(ps), (2, 1), ps)
        self.assertEqual(self._judge("Start-Process powershell.exe -WindowStyle Hidden; Start-Process powershell.exe -WindowStyle Hidden"), (2, 0))
        found, bad = _start_process_violations("x = 'Start-Process powershell.exe; ' 'Start-Process powershell.exe -WindowStyle Hidden'\n")
        self.assertEqual((found, len(bad)), (2, 1))


class SpawnSiteBehaviourTests(unittest.TestCase):
    """Rows A1 and A4: the two behaviours behind the scanner findings."""

    def test_worktree_live_refusal_passes_the_hidden_flag_to_its_runner(self):
        from tools.repo_hygiene import core
        calls = []

        def runner(args, **kw):
            calls.append(kw)
            return subprocess.CompletedProcess(args, 0, stdout="FREE\n", stderr="")

        with mock.patch.object(core.shutil, "which", return_value="pwsh"):
            self.assertIsNone(core.worktree_live_refusal(Path("C:/r"), Path("C:/mlvtmp/lane-X-1"), runner=runner))
        flags = calls[0].get("creationflags")  # never assert on the whole kwargs dict: it carries the process env
        self.assertIsNotNone(flags, "worktree_live_refusal spawned its runner without creationflags=")
        self.assertEqual(flags & core._NO_WINDOW, core._NO_WINDOW)

    def test_runtime_floor_spawn_ors_the_hidden_bit_into_a_caller_flag(self):
        from tools.repo_hygiene import runtime_floor_suites as rfs
        seen = {}

        def fake_popen(argv, **kw):
            seen.update(kw)
            return object()

        with mock.patch.object(hidden_spawn.os, "name", "nt"), mock.patch.object(rfs.subprocess, "Popen", fake_popen):
            rfs._spawn(["x"], creationflags=0x00004000)
            self.assertEqual(seen["creationflags"], 0x08004000)
            rfs._spawn(["x"])
            self.assertEqual(seen["creationflags"], 0x08000000)


if __name__ == "__main__":
    unittest.main()
