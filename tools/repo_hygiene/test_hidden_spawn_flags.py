"""Guard for MLV-LANE-CONSOLE-HIDDEN-1: no console program may be spawned without a hidden-window flag.

A console program started without CREATE_NO_WINDOW (Python) or ``-WindowStyle Hidden`` (Start-Process)
by a parent that owns no console opens a VISIBLE window on the owner's desktop. Roughly 15 such
``...pythoncore-3.14-64\\python.exe`` windows were recorded on 2026-11-09 (ledger QUEUED 2026-11-09T23:14:17Z).

Three pins:
  1. ``hidden_spawn.no_window_flags`` sets CREATE_NO_WINDOW on Windows, ORs an existing flag, and is a no-op elsewhere.
  2. Every ``subprocess.run/Popen/check_output/check_call/call`` in a non-test tool module under tools/ carries a
     ``creationflags=`` keyword. The set is derived by glob, so a NEW unflagged spawn fails here.
     HIGH-RISK path classes (key3) are listed in EXCLUDED_PREFIXES and are left to their own cards.
  3. The named test fixtures that the 2026-11-09 evidence points at (the spliced-body-scan runner and the
     host-load-gate fixtures) spawn hidden, and every ``Start-Process powershell.exe`` in the gate's generated
     run.ps1 bodies says ``-WindowStyle Hidden``.
"""
from __future__ import annotations

import ast
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

# A generated PowerShell fixture that starts powershell: any case, quoted or bare, with or without -FilePath, a bare name,
# a .exe name or a full path.
_START_PROCESS_POWERSHELL_RE = re.compile(
    r"Start-Process\s+(?:-FilePath\s+)?(?:'[^']*|\"[^\"]*|[^\s'\"]*)powershell(?:\.exe)?['\"]?(?=\s)", re.IGNORECASE)

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


def _label(expr: ast.AST) -> str:
    try:
        return ast.unparse(expr)[:60]
    except Exception:  # noqa: BLE001 - a display string only; never part of a verdict
        return "<expression>"


def _store_names(target: ast.AST) -> list[str]:
    return [n.id for n in ast.walk(target) if isinstance(n, ast.Name)]


def _carried(expr: ast.AST | None, modules: set[str], funcs: dict[str, str]) -> list[tuple[str, str]]:
    """Spawn APIs ('fn', attr) and subprocess module aliases ('mod', name) that ``expr`` can evaluate to.

    Looks through containers, conditionals, ``or``/``and``, subscripts and walrus values; never into a call,
    so ``subprocess.run(...)`` (a result) is not a carrier but ``(subprocess.run, x)`` and ``a or subprocess.run`` are.
    Over-approximates on purpose: a name that MIGHT hold a spawn API is checked as one.
    """
    found: list[tuple[str, str]] = []
    stack = [expr]
    while stack:
        e = stack.pop()
        if e is None:
            continue
        if isinstance(e, ast.Attribute):
            if e.attr in SPAWN_ATTRS and any(kind == "mod" for kind, _ in _carried(e.value, modules, funcs)):
                found.append(("fn", e.attr))  # the receiver may itself be a container or conditional holding the module
        elif isinstance(e, ast.Name):
            if e.id in funcs:
                found.append(("fn", funcs[e.id]))
            if e.id in modules:
                found.append(("mod", e.id))
        elif isinstance(e, (ast.Tuple, ast.List, ast.Set)):
            stack.extend(e.elts)
        elif isinstance(e, ast.Dict):
            stack.extend(k for k in e.keys if k is not None)  # a spawn API used as a key is iterated back out by `for r in d`
            stack.extend(e.values)
        elif isinstance(e, (ast.Starred, ast.NamedExpr, ast.Subscript)):
            stack.append(e.value)
        elif isinstance(e, ast.IfExp):
            stack.extend((e.body, e.orelse))
        elif isinstance(e, ast.BoolOp):
            stack.extend(e.values)
        elif isinstance(e, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            stack.append(e.elt)
    return found


def _spawn_names(tree: ast.AST) -> tuple[set[str], dict[str, str], list[int], set[str]]:
    """Names bound to the subprocess module and to its spawn functions, the lines of bindings that cannot be tracked,
    and the names `from subprocess import` brought in.

    Resolves imports, aliases, assignments (chained, tuple/list/starred/nested unpacking), walrus, for/comprehension
    targets, ``with ... as``, match captures and default arguments. A binding whose target is not a plain name
    (``obj.run = subprocess.run``, ``d['r'] = subprocess.run``) cannot be followed to its calls, so its line is
    returned and reported: an unresolvable binding is a violation, never clean.
    """
    modules: set[str] = set()
    funcs: dict[str, str] = {}
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(a.asname or a.name for a in node.names if a.name == "subprocess")
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for a in node.names:
                if a.name == "*":  # `from subprocess import *` makes every spawn API a bare name
                    funcs.update({attr: attr for attr in SPAWN_ATTRS})
                    imported.update(SPAWN_ATTRS)
                elif a.name in SPAWN_ATTRS:
                    funcs[a.asname or a.name] = a.name
                    imported.add(a.asname or a.name)

    def pairs(node: ast.AST) -> list[tuple[ast.AST, ast.AST | None]]:
        if isinstance(node, ast.Assign):
            return [(t, node.value) for t in node.targets]
        if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            return [(node.target, node.value)]
        if isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            return [(node.target, node.iter)]
        if isinstance(node, (ast.With, ast.AsyncWith)):
            return [(i.optional_vars, i.context_expr) for i in node.items if i.optional_vars is not None]
        if isinstance(node, ast.Match):
            captures = [p.name for case in node.cases for p in ast.walk(case.pattern)
                        if isinstance(p, (ast.MatchAs, ast.MatchStar)) and p.name]
            captures += [p.rest for case in node.cases for p in ast.walk(case.pattern) if isinstance(p, ast.MatchMapping) and p.rest]
            return [(ast.Name(id=name, ctx=ast.Store()), node.subject) for name in captures]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            a = node.args
            positional = a.posonlyargs + a.args
            out = [(ast.Name(id=arg.arg, ctx=ast.Store()), d) for arg, d in zip(positional[len(positional) - len(a.defaults):], a.defaults)]
            out += [(ast.Name(id=arg.arg, ctx=ast.Store()), d) for arg, d in zip(a.kwonlyargs, a.kw_defaults) if d is not None]
            return out
        return []

    def bind(target: ast.AST, value: ast.AST | None, record: list[int] | None) -> bool:
        changed = False
        if isinstance(target, ast.Name):
            leaves, refs = [target.id], _carried(value, modules, funcs)
        elif isinstance(target, (ast.Tuple, ast.List)):
            same_shape = (
                isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts)
                and not any(isinstance(e, ast.Starred) for e in (*target.elts, *value.elts))
            )
            if same_shape:
                for t, v in zip(target.elts, value.elts):
                    changed |= bind(t, v, record)
                return changed
            leaves, refs = _store_names(target), _carried(value, modules, funcs)  # shape unknown: every name MAY hold it
        elif isinstance(target, ast.Starred):
            return bind(target.value, value, record)
        else:
            if record is not None and _carried(value, modules, funcs):
                record.append(getattr(target, "lineno", 0))
            return False
        for leaf in leaves:
            for kind, what in refs:
                if kind == "mod" and leaf not in modules:
                    modules.add(leaf)  # sp = subprocess
                    changed = True
                elif kind == "fn" and leaf not in funcs:
                    funcs[leaf] = what  # first binding wins, so `funcs` only grows and the loop below ends
                    changed = True
        return changed

    changed = True
    while changed:  # an alias of an alias (r = subprocess.run; q = r) settles in a few passes
        changed = False
        for node in ast.walk(tree):
            for target, value in pairs(node):
                changed |= bind(target, value, None)
    unresolved: list[int] = []
    for node in ast.walk(tree):
        for target, value in pairs(node):
            bind(target, value, unresolved)
    return modules, funcs, sorted(set(unresolved)), imported


def _is_no_window_definition(value: ast.AST | None, modules: set[str]) -> bool:
    """Exactly ``getattr(subprocess, "CREATE_NO_WINDOW", 0)`` or that call under ``if os.name == "nt" else 0``."""
    if isinstance(value, ast.IfExp):
        test = value.test
        is_nt = (
            isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)
            and isinstance(test.left, ast.Attribute) and test.left.attr == "name"
            and isinstance(test.left.value, ast.Name) and test.left.value.id == "os"
            and len(test.comparators) == 1 and isinstance(test.comparators[0], ast.Constant) and test.comparators[0].value == "nt"
        )
        zero = isinstance(value.orelse, ast.Constant) and type(value.orelse.value) is int and value.orelse.value == 0
        if not (is_nt and zero):
            return False
        value = value.body
    return (
        isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "getattr"
        and len(value.args) == 3 and not value.keywords
        and isinstance(value.args[0], ast.Name) and value.args[0].id in modules
        and isinstance(value.args[1], ast.Constant) and value.args[1].value == "CREATE_NO_WINDOW"
        and isinstance(value.args[2], ast.Constant) and type(value.args[2].value) is int and value.args[2].value == 0
    )


def _provably_hidden(expr: ast.AST, names: set[str]) -> bool:
    """True only for a form that carries CREATE_NO_WINDOW by syntax alone. No expression is ever evaluated.

    Accepted: an int literal (bool is an int, so False and True carry no bit) with the 0x08000000 bit; a name or
    attribute spelled CREATE_NO_WINDOW / _NO_WINDOW that nothing in the source rebinds; a call to ``no_window_flags(...)``
    that nothing in the source rebinds; a ``|`` any operand of which is one of those; a name every binding of which is
    one of those. Any other arithmetic, variable or call is not provable. A ``|`` that also carries CREATE_NEW_CONSOLE
    (0x10, a literal or a name) is never hidden: Windows ignores CREATE_NO_WINDOW when that bit is set.
    """
    hidden = poisoned = False
    stack = [expr]
    while stack:
        e = stack.pop()
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.BitOr):
            stack.extend((e.left, e.right))
        elif isinstance(e, ast.Constant):
            if isinstance(e.value, int):
                hidden |= bool(e.value & _CREATE_NO_WINDOW)
                poisoned |= bool(e.value & _CREATE_NEW_CONSOLE)
        elif isinstance(e, ast.Name):
            hidden |= e.id in names
            poisoned |= e.id == "CREATE_NEW_CONSOLE"
        elif isinstance(e, ast.Attribute):
            hidden |= e.attr in _HIDDEN_NAMES and e.attr in names
            poisoned |= e.attr == "CREATE_NEW_CONSOLE"
        elif isinstance(e, ast.Call):
            f = e.func
            if (isinstance(f, ast.Name) and f.id == _HELPER_NAME) or (isinstance(f, ast.Attribute) and f.attr == _HELPER_NAME):
                hidden |= _HELPER_NAME in names  # the shared helper strips CREATE_NEW_CONSOLE from its argument itself
    return hidden and not poisoned


def _hidden_names(tree: ast.AST, modules: set[str]) -> set[str]:
    """Names whose EVERY binding in the source is provably hidden (least fixpoint). Any other kind of binding disqualifies.

    The trusted spellings (CREATE_NO_WINDOW, _NO_WINDOW, no_window_flags) are in the result only when nothing in the
    source rebinds them (other than a plain import of the shared name) or their one definition is the standard one-liner.
    """
    binders: dict[str, list[ast.AST | None]] = {}
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
            for a in node.names:
                name = (a.asname or a.name).split(".")[0]
                shared = name in _TRUSTED_NAMES and (a.asname is None or a.asname == a.name)
                if not shared:  # a plain import of the shared name is how a module gets it; a renamed import is another object
                    binders.setdefault(name, []).append(None)
    names = {n for n in _TRUSTED_NAMES if n not in binders}
    changed = True
    while changed:
        changed = False
        for name, values in binders.items():
            if name in names or any(v is None for v in values):
                continue
            if all((name in _HIDDEN_NAMES and _is_no_window_definition(v, modules)) or _provably_hidden(v, names) for v in values):
                names.add(name)
                changed = True
    return names


def _spawn_escapes(tree: ast.AST, modules: set[str], funcs: dict[str, str], imported: set[str]) -> list[int]:
    """Lines where a spawn API is handed to code whose calls the scan cannot follow.

    Direct references only: ``helper(subprocess.run)``, ``partial(subprocess.run, ...)``, ``runner=subprocess.Popen``,
    ``return subprocess.call``, a lambda body, and the same for a name imported with ``from subprocess import``.
    ``isinstance``/``issubclass`` arguments, annotations and base classes are type uses, not escapes. A spawn API that
    reaches a callee through a parameter or alias name is the scan's stated limit (lexical, one module at a time).
    """
    parent: dict[int, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[id(child)] = node
    lines = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            hit = node.attr in SPAWN_ATTRS and any(kind == "mod" for kind, _ in _carried(node.value, modules, funcs))
        elif isinstance(node, ast.Name):
            hit = node.id in imported and isinstance(node.ctx, ast.Load)
        else:
            continue
        if not hit:
            continue
        up = parent.get(id(node))
        if isinstance(up, ast.keyword):
            up = parent.get(id(up))
            escapes = isinstance(up, ast.Call)
        elif isinstance(up, ast.Call):
            escapes = node is not up.func and not (isinstance(up.func, ast.Name) and up.func.id in ("isinstance", "issubclass"))
        else:
            escapes = isinstance(up, (ast.Return, ast.Yield, ast.YieldFrom, ast.Lambda, ast.Await))
        if escapes:
            lines.append(node.lineno)
    return sorted(set(lines))


def _unflagged_spawns_in_source(source: str, label: str) -> list[str]:
    tree = ast.parse(source, filename=label)
    modules, funcs, unresolved, imported = _spawn_names(tree)
    hidden = _hidden_names(tree, modules)
    bad = [f"{label}:{line} binds a subprocess spawn API where its calls cannot be followed" for line in unresolved]
    bad += [f"{label}:{line} hands a subprocess spawn API to code whose calls cannot be followed"
            for line in _spawn_escapes(tree, modules, funcs, imported)]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attrs = [what for kind, what in _carried(node.func, modules, funcs) if kind == "fn"]
        if not attrs:
            continue
        shown = f"subprocess.{attrs[0]}" if isinstance(node.func, ast.Attribute) and node.func.attr == attrs[0] \
            else f"{_label(node.func)} (= subprocess.{attrs[0]})"
        flags = [kw for kw in node.keywords if kw.arg == "creationflags"]
        if flags and all(_provably_hidden(kw.value, hidden) for kw in flags):
            continue
        why = "creationflags= is not provably CREATE_NO_WINDOW" if flags else "has no creationflags="
        bad.append(f"{label}:{node.lineno} {shown}(...) {why}")
    return bad


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
        starts = [m.start() for m in _START_PROCESS_POWERSHELL_RE.finditer(src)]
        self.assertGreaterEqual(len(starts), 3, "expected the three run.ps1 Start-Process fixtures")
        bad = []
        for pos in starts:
            end = src.find("-PassThru", pos)
            end = end if end != -1 else pos + 400  # a fixture without -PassThru is still checked, over a bounded window
            if "-WindowStyle Hidden" not in src[pos:end]:
                bad.append(f"test_playback_host_load_gate.py:{src.count(chr(10), 0, pos) + 1}")
        self.assertEqual(bad, [], "Start-Process powershell.exe without -WindowStyle Hidden: " + ", ".join(bad))


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
        self.assertEqual(out, [[]])

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
            "_NO_WINDOW | kwargs.pop('creationflags', 0)", "(0x20 | 0x4000) | no_window_flags()",
        )
        for api in sorted(SPAWN_ATTRS):
            for value in clean:
                src = f"import subprocess\nsubprocess.{api}(['x'], creationflags={value})\n"
                self.assertEqual(self._bad(src), [], (api, value))

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
        for label in ("tuple", "list", "chained", "walrus", "for loop", "or", "subscript", "star import"):
            hidden = unhidden[label].replace("(['x'])", "(['x'], creationflags=_NO_WINDOW)")
            self.assertEqual(self._bad(hidden), [], label)

    def test_a_tuple_binding_does_not_taint_its_other_elements(self):
        self.assertEqual(self._bad("import subprocess\na, b = subprocess.run, len\nb(['x'])\n"), [])
        self.assertEqual(self._bad("import subprocess\nx = subprocess.CompletedProcess(['x'], 0)\ny = (x, 1)\n"), [])

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

        with mock.patch.object(rfs, "_NO_WINDOW", 0x08000000), mock.patch.object(rfs.subprocess, "Popen", fake_popen):
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
            self.assertEqual(len(self._bad(src)), 1, rebind)

    # A5: a module alias held in a container or conditional is still the module.
    def test_a5_a_module_reached_through_a_container_is_resolved(self):
        for recv in ("mods[0]", "[subprocess][0]", "(subprocess or x)", "(subprocess if a else x)", "{'m': subprocess}['m']"):
            src = f"import subprocess\nmods = [subprocess]\n{recv}.run(['x'])\n"
            self.assertEqual(len(self._bad(src)), 1, recv)
            ok = f"import subprocess\nmods = [subprocess]\n{recv}.run(['x'], creationflags=_NO_WINDOW)\n"
            self.assertEqual(self._bad(ok), [], recv)

    # A6: spawn APIs that travel by dict key, context manager, match capture, call argument or return are reported.
    def test_a6_a_spawn_api_that_escapes_is_resolved_or_reported(self):
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
        }
        for label, src in escapes.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        fine = {
            "isinstance": "import subprocess\nisinstance(p, subprocess.Popen)\n",
            "issubclass": "import subprocess\nissubclass(t, (subprocess.Popen, int))\n",
            "annotation": "import subprocess\ndef f(p: subprocess.Popen) -> subprocess.Popen:\n    q: subprocess.Popen = p\n    return q\n",
            "module argument": "import subprocess\nhelper(subprocess)\nx = subprocess.CompletedProcess(['x'], 0)\n",
            "non-spawn attribute": "import subprocess\nhelper(subprocess.PIPE, subprocess.TimeoutExpired)\n",
        }
        for label, src in fine.items():
            self.assertEqual(self._bad(src), [], label)

    # A1 (scanner side): no accepted expression may carry CREATE_NEW_CONSOLE.
    def test_a1_scanner_rejects_create_new_console_in_any_accepted_form(self):
        for value in ("0x08000010", "0x10 | 0x08000000", "subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NO_WINDOW",
                      "CREATE_NEW_CONSOLE | _NO_WINDOW", "_NO_WINDOW | 0x10", "0x08004010", "_NO_WINDOW | CREATE_NEW_CONSOLE | 0x20"):
            self.assertEqual(len(self._run(value)), 1, value)
        for value in ("no_window_flags(0x10)", "hidden_spawn.no_window_flags(subprocess.CREATE_NEW_CONSOLE)", "_NO_WINDOW | 0x4000"):
            self.assertEqual(self._run(value), [], value)

    # A7: the fixture guard finds a Start-Process powershell fixture in any quoting/case/arg-order spelling.
    def test_a7_the_start_process_fixture_pattern_covers_other_spellings(self):
        for text in ("Start-Process -FilePath 'powershell.exe' -PassThru", "Start-Process -FilePath PowerShell.exe -PassThru",
                     'Start-Process -FilePath "powershell" -PassThru', "start-process powershell.exe -PassThru",
                     "Start-Process -FilePath 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe' -PassThru"):
            self.assertIsNotNone(_START_PROCESS_POWERSHELL_RE.search(text), text)
        for text in ("Start-Process -FilePath 'notepad.exe'", "# Start-Process lives in this module", "Stop-Process -Name powershell"):
            self.assertIsNone(_START_PROCESS_POWERSHELL_RE.search(text), text)


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

        with mock.patch.object(rfs, "_NO_WINDOW", 0x08000000), mock.patch.object(rfs.subprocess, "Popen", fake_popen):
            rfs._spawn(["x"], creationflags=0x00004000)
            self.assertEqual(seen["creationflags"], 0x08004000)
            rfs._spawn(["x"])
            self.assertEqual(seen["creationflags"], 0x08000000)


if __name__ == "__main__":
    unittest.main()
