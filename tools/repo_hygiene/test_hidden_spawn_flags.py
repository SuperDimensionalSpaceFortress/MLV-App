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
module it re-exports (``asyncio.base_events.subprocess`` is ``subprocess``). A dotted ``__import__`` whose fromlist is known only at
run time (a variable, a call, a ``*`` or ``**`` in its arguments, a list of only starred elements) may return the root or the submodule, so
it is REPORTED. A name that is the string of an event-loop spawn method (``getattr(loop, 'subprocess_exec')``, ``attrgetter``) is REPORTED,
also when it is built from literals (a ``+`` chain, ``%``, ``.format``, ``.join``, ``.replace`` or an f-string whose holes are literals).
A module bound by ``import x [as y]`` (and a plain alias or literal dynamic import of one) is followed as a value: in a container,
conditional, subscript, return, lambda, class attribute or call argument it is REPORTED, because any module may re-export ``os``; for the
same reason a dunder attribute read off it (``shutil.__dict__``) and every reflection helper (``globals()``, ``signature``, ``__annotations__``)
is REPORTED in a file that imports any module. A built string is folded with ``replace`` (and its count), ``strip``/``lower``/``upper``, a
slice, ``chr(n)``, ``bytes.decode`` / ``str.encode`` (also ``str(b, enc, err)``, ``bytes(s, enc, err)`` and ``codecs.decode`` / ``encode``) and a ``format`` / ``%`` spec
(``{:.4s}``), applied the way Python applies them where the fold is exact: a standard-library codec (utf-8, ascii, latin-1, utf-16/32, cp1252, the escape
codecs) with a standard error mode (``ignore``, ``replace``, ``backslashreplace``, ``xmlcharrefreplace``, ``namereplace``, ``surrogateescape``) is run by Python
itself on the literal (R9-F1). Any other codec or error mode, ``*args``, a call that raises, or a text with an unknown part reads as UNKNOWN (every non-ASCII
character is any text and the text is padded with a wildcard), never as a literal. A NUL character in a folded text is the wildcard, so the bytes a utf-16 or
utf-32 encode writes read as unknown pieces (an over-report, never a miss). A literal read through a codec Python knows but the fold does not run
(``rot_13``, ``base64``, ``hex``, ``zlib``) or through a codec named by a run-time value reads as every spawn-method name and as a launch word, so it is REPORTED
(pass-25 A2); a case mapper or the default strip does not map a character from U+0080 to U+00FF, which may be a byte of a bytes literal (A1). What stays unread is
a codec reached through a function the fold does not follow (``functools.partial(codecs.decode, ...)``, a wrapper of your own): the text such a function returns
over its arguments is hedged only for the names the arguments hold.
A method called unbound on the type (``str.lower(s)``, ``builtins.str``, ``type('')``, ``''.__class__``) is the bound call, and ``operator.add`` /
``concat`` / ``mod`` / ``mul``, ``s.__add__(t)`` and ``''.join(reversed(s))`` are folded like the operators they stand for. A call the fold has no
arm for hedges its receiver AND every positional argument and keyword value (a call that only looks an attribute up or tests, ``getattr`` and
``len`` among them, does not return its argument, so it hedges none).
Any piece the fold cannot read EXACTLY (a number printed through a spec such as ``{115:c}`` or ``%c``, a ``*`` / ``**`` expansion, a mapping
the scan cannot read key by key, a set whose iteration order is unknown, a spec wider than the fold applies, a run-time name) is a WILDCARD
that stands for any text, and the built text is matched as a pattern against the spawn-method names and the launch keywords: a fold mistake
can only over-report, never hide a spawn (r7). A pattern of wildcards with fewer than three literal characters around them says nothing and
is not reported. The standard ``_NO_WINDOW`` definition is trusted only
when ``os``, ``subprocess`` and ``getattr`` each come from exactly one ordinary ``import``.

Remaining limits, every one named (r5 rewrite: the scan is syntactic and static, so these are what it does NOT see):
  * Python source built or loaded at run time: ``exec``, ``eval``, ``compile``, ``runpy``, ``importlib.util`` loaders,
    ``sys.modules`` lookups, ``importlib.reload``, ``pickle`` / ``marshal``; a spawn API read back out of a frame
    (``sys._getframe().f_locals``, ``inspect.currentframe``) or out of an object by an attribute the scan does not name.
  * A process started without ``subprocess`` / ``asyncio`` / ``os``: ``ctypes`` / ``win32process`` CreateProcess,
    ``multiprocessing``, ``_winapi``, ``concurrent.futures`` process pools, ``pty``, ``os.startfile``.
  * A method name assembled at run time from a value the file does not state (``getattr(loop, 'subprocess_' + kind)``: the wildcard
    pattern ``subprocess_*`` IS reported, and so is a ``.replace`` / slice / ``strip`` with run-time arguments over a text that holds a
    spawn-method name; but a name whose literal part is under three characters is not reported; a ``.join`` over anything but a literal list,
    tuple or set (a slice of one, a generator, ``map(chr, ...)``) is an unreadable text) or held outside a string (``match`` patterns);
    a spawn name reached by ``getattr`` on an object that is not a tracked module.
  * A module that is not bound by ``import x`` (a from-import such as ``from concurrent import futures``, or ``os.path`` taken as a value)
    flowing into a container or return: a from-import may bind a class or function, and an attribute may be either, so it is not followed.
  * Hostile source that rebinds builtins or ``__import__``, or mutates a module through a path this scan does not parse. The
    card's promise is that no ACCIDENTAL unflagged spawn lands, not that deliberate evasion is impossible.
  * Any non-Python launcher: a ``.ps1`` / ``.cmd`` / ``.bat`` file that starts a console program is not read at all, and a
    PowerShell script is read only inside a Python string of the one fixture file. There only ``Start-Process`` and its aliases
    ``start`` / ``saps`` (also quoted as ``& 'Start-Process'`` or dot-sourced, after ``return`` / ``throw`` / ``exit`` / ``!`` / ``-not``, and with
    typographic quotes, dashes and a no-break space read as the ASCII ones) are recognised as the launch; ``[Diagnostics.Process]::Start``,
    ``Invoke-Item``, ``Invoke-Expression`` over a built string, ``cmd /c start``, ``& (Get-Command ...)`` and a command name held in a
    variable are not. A launch is read through the fold set of ``_flatten``, the one the Python side reads a spawn-method name through (the literal,
    ``+`` / ``%`` / f-string / ``.format`` / ``.join`` / ``.replace`` / slice / ``strip`` / case-mapper / ``chr`` / ``decode`` folds, their unbound
    ``str.m(s, ...)`` and ``operator.add`` / ``concat`` / ``mod`` / ``mul`` spellings, ``''.join(reversed(s))``, and a string method with no arm by way of
    ``_hedge``): a launch in a string built any of those ways is read (G13, G15, G16). A string that is not built in the file (read at run time) is
    not read, and neither is the text an UNRECOGNISED function returns over its arguments (``run('zz' + 'Start-Process x')``): the Python side
    hedges every argument of such a call, the PowerShell side does not, because it would report a hidden launch passed to any call as a literal.
    A built string is REPORTED only when a literal piece reads as ``Start-`` or as ``start`` / ``saps`` where a command word can stand.
  * Whether a child of a hidden spawn stays hidden, and ``tools/agent-bridge/**`` plus the other excluded paths (EXCLUDED_DIRS /
    EXCLUDED_FILES), which are left to their own cards.
  * A name bound to two different spawn modules or APIs in one file is REPORTED rather than followed, since the alias map is
    file-wide, not scoped. A name that two different ``import`` statements bind is never trusted as a flag or in the standard
    definition, so a call that depends on it is REPORTED.
"""
from __future__ import annotations

import ast
import asyncio
import codecs
import functools
import pkgutil
import posixpath
import re
import subprocess
import unittest
import warnings
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
        ["git", "-C", str(root), "ls-files", "-z", "--", "tools/*.py", "tools/*.pyw"],  # a .pyw is run by pythonw.exe and starts a console child the same way (pass-32 A2)
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
    # asyncio submodules (asyncio.windows_utils.Popen subclasses subprocess.Popen; the transports forward **kwargs to it)
    "asyncio.*": frozenset({"Popen", "BaseSubprocessTransport", "_WindowsSubprocessTransport", "_UnixSubprocessTransport"}),
}
_FORBIDDEN_APIS = {
    "subprocess": frozenset({"getoutput", "getstatusoutput"}),
    "os": frozenset({"system", "popen", "spawnl", "spawnle", "spawnlp", "spawnlpe", "spawnv", "spawnve", "spawnvp", "spawnvpe",
                     "execl", "execle", "execlp", "execlpe", "execv", "execve", "execvp", "execvpe"}),
}
_LOOP_SPAWN_METHODS = frozenset({"subprocess_exec", "subprocess_shell", "_make_subprocess_transport"})  # event-loop methods; **kwargs reach Popen
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
_REFLECTION_BARE = frozenset({"__annotations__", "__builtins__", "get_type_hints", "get_annotations", "globals", "locals"})
_REFLECTION = _REFLECTION_BARE | frozenset({
    # an annotation or a default can hold a spawn API, and inspection reads it back: signature(...).parameters[..].annotation,
    # getfullargspec, f.__defaults__ / __kwdefaults__ / __signature__ / __globals__ / __closure__, a frame's f_globals, the gc.
    # (sys._getframe / f_locals stay allowed: they are limits, named in the module docstring, and a tool legitimately uses them.)
    "signature", "getfullargspec", "annotation", "return_annotation", "__defaults__", "__kwdefaults__", "__signature__",
    "__globals__", "__closure__", "cell_contents", "f_globals", "get_referrers", "get_objects",
})
_REFLECTION_STRINGS = frozenset(n for n in _REFLECTION if n.startswith("__"))  # getattr(f, '__defaults__') reaches what f.__defaults__ does
_CLASS_SCOPE_IMPORT_ROOTS = frozenset({"subprocess", "os", "asyncio", "nt", "posix", "importlib", "builtins"})
_MUTATORS = frozenset({"setattr", "delattr"})
_MUTATING_ATTRS = frozenset({"__dict__", "__setattr__", "__delattr__"})
# Submodules of asyncio (read off the installed package, plus the ones that re-export subprocess/os): ``asyncio.base_events.subprocess``
# IS the subprocess module, so a submodule reference stays the tracked module "asyncio" and ``.subprocess`` / ``.os`` off it is followed.
_ASYNCIO_SUBMODULES = frozenset({m.name for m in pkgutil.iter_modules(asyncio.__path__)} | {
    "base_events", "base_subprocess", "unix_events", "windows_events", "proactor_events", "selector_events", "events"}) - {"subprocess"}
# "asyncio.*" is any asyncio submodule object. `.subprocess` / `.os` off a submodule (asyncio.base_events.subprocess) are the REAL modules,
# but `.subprocess` off the asyncio package is asyncio's own wrapper module.
_SUBMODULE_REEXPORTS = {"subprocess": "subprocess", "_subprocess": "subprocess", "os": "os", "_os": "os", "nt": "os", "posix": "os"}
_PACKAGE_REEXPORTS = {"subprocess": "asyncio.subprocess", "os": "os", "nt": "os", "posix": "os"}
_LOOP_METHOD_NAME_RE = re.compile(r"(?:[^.]*\.)*(?:" + "|".join(sorted(map(re.escape, _LOOP_SPAWN_METHODS))) + ")")


def _label(expr: ast.AST) -> str:
    try:
        return ast.unparse(expr)[:60]
    except Exception:  # noqa: BLE001 - a display string only; never part of a verdict
        return "<expression>"


_FORMAT_BODY_RE = re.compile(r"([^!:]*)(?:!([rsa]))?(?::(.*))?", re.DOTALL)
_PERCENT_SPEC_RE = re.compile(r"([#0\- +]*)([0-9]+|\*)?(?:\.([0-9]*|\*))?[hlL]?([sraidiouxXeEfFgGcb%])")  # ASCII digits only: int() meets nothing else; `%.s` is precision 0 (pass-22 A1)
_PERCENT_TAIL_RE = re.compile(r"(?:[^)]*\))?[#0\- +0-9.*hlL]*[A-Za-z%]?")  # what a field opened before a wildcard may still take: key, flags, width, conversion
_PERCENT_UNPARSED_RE = re.compile(r"[#0\- +0-9.*hlL]*")  # the flags, width and precision of a `%` field the fold could not parse; the letter after them stays literal, so no name is cut
_SPEC_NUMBER_LIMIT = 256  # a width or precision above this is never handed to Python's formatter (no huge allocation)
_STRING_MAPPERS = frozenset({"lower", "upper", "casefold", "strip", "lstrip", "rstrip", "title", "capitalize", "swapcase"})
_CASE_MAPPERS = frozenset({"lower", "upper", "casefold", "swapcase"})  # act character by character, so a wildcard in the text survives them
_FLATTEN_DEPTH = 48
# A piece the fold cannot read (a name bound to a run-time value, a number, a spec it does not apply, a set it cannot order) is this
# character, not nothing: it stands for ANY text, so a fold mistake can only make the scan report more, never less.
_WILD = "\x00"


def _spec_too_big(numbers) -> bool:
    """A width / precision that is longer than four digits or above the limit; the length test comes first so ``int()`` never meets a 5000-digit string."""
    return any(len(number) > 4 or int(number) > _SPEC_NUMBER_LIMIT for number in numbers if number)


# Where a PowerShell launch keyword can begin inside a text some operation the fold cannot apply may cut a substring from: the substring may start
# at ANY character, so no left boundary is asked of the keyword (``'xsaps cmd.exe'.partition('x')[2]`` is ``saps cmd.exe``).
_LOOSE_LAUNCH_RE = re.compile(r"start-|(?:start|saps)(?![\w-])", re.IGNORECASE)
# What an operation the fold cannot apply may leave of a text is read two ways, and one text cannot say both: the Python side asks which spawn-method
# name it may leave, the PowerShell side which launch keyword. ``_flatten_launch`` reads in the second way.
_HEDGE_MODE = ["name"]


_PATTERN_CAP_REASON = "builds a text past the pattern cap, which the scan does not search, so it is REPORTED"
# Reviewed sites, by file: only THIS report is dropped for them, never any other finding. A text past the cap (a template with holes):
_PATTERN_CAP_REVIEWED = {
    "tools/repo_hygiene/candidate_acceptance.py": "a 38k-character PowerShell script literal passed to the bounded runner; it holds no spawn-method name",
    "tools/repo_hygiene/closeout_dashboard.py": "the 27k-character HTML dashboard template (an f-string with holes); it holds no spawn-method name",
}
_GETATTR_REASON = "reads an attribute named by a run-time text, which can reach an event-loop spawn method"
# A getattr over a run-time name on an os-like object:
_GETATTR_REVIEWED = {
    "tools/release/release_evidence.py": "getattr over a fixed tuple of st_* field names of os.stat results (identity_fields); no spawn method is reachable from a stat result",
}
_PATTERN_ITEM_CAP = 20_000  # a pattern of more literal characters and wildcard runs than this is not searched: the scan REPORTS it (a `_FoldTooBig`), never reads it as clean


def _pattern_items(text: str) -> list[str | None]:
    """``text`` as literal characters and ``None`` for a run of wildcards; refused past ``_PATTERN_ITEM_CAP`` before it is searched (every item list is built here)."""
    if len(text) > _PATTERN_ITEM_CAP:
        raise _FoldTooBig(len(text))
    items: list[str | None] = []
    for ch in text:
        if ch != _WILD:
            items.append(ch)
        elif not items or items[-1] is not None:
            items.append(None)
    return items


_REPORT_NAME = _WILD + "subprocess_exec" + _WILD  # what a text the fold could not prove clean reads as: it can read as a spawn-method name, so the scan reports it
_REPORT_LAUNCH = _WILD + "sa" + _WILD + "s"  # the same for a PowerShell launch: a keyword lies across a literal and a wildcard
_NAME_GRAMS = frozenset(name[i:i + 3] for name in _LOOP_SPAWN_METHODS for i in range(len(name) - 2))  # every run of three characters a spawn-method name holds


def _unproven(text: str) -> str:
    """r15 (HUB RULING, the class rule). What an operation the fold cannot apply leaves of ``text`` is not proven clean: it is REPORTED when ``text`` holds a literal run
    of three characters (two for a launch keyword) that a spawn-method name (a keyword) holds, and says nothing otherwise. No per-operation matcher rules it out."""
    launch = _HEDGE_MODE[0] == "launch"
    size, grams = (2, _KEYWORD_GRAMS) if launch else (3, _NAME_GRAMS)
    for run in re.findall(r"[^\x00]+", text.lower()):
        if any(run[i:i + size] in grams for i in range(len(run) - size + 1)):
            return _REPORT_LAUNCH if launch else _REPORT_NAME
    return _WILD


def _hedge(text: str, cut: bool = False) -> str:
    """What an operation the fold cannot apply may leave of ``text``. An EXACT text can only be cut: anything around the spawn-method name it holds (the launch reading:
    the text from its first launch keyword on). A PARTIAL text a string operation CUTS (``cut``) is ``_unproven``; one a plain call merely returns stays as it is with a
    wildcard around it, and the caller's name / launch test reads it."""
    if _HEDGE_MODE[0] == "launch":
        keyword = _LOOSE_LAUNCH_RE.search(text)
        if keyword:
            return _WILD + text[keyword.start():]
        return (_unproven(text) if cut else _WILD + text + _WILD) if _WILD in text else _WILD
    if _WILD in text:
        return _unproven(text) if cut else _WILD + text + _WILD
    held = [name for name in sorted(_LOOP_SPAWN_METHODS) if name in text.lower()]  # an unknown function may lower-case what it was given
    if not held:
        return _WILD
    common = held[0]
    for other in held[1:]:
        common = max((common[i:j] for i in range(len(common)) for j in range(i + 1, len(common) + 1) if common[i:j] in other), key=len, default="")
    return _WILD + common + _WILD if common else _WILD


def _flatten_launch(node: ast.AST, consts: dict[str, str]) -> tuple[str, bool]:
    """``_flatten`` read for a PowerShell launch: an operation it cannot apply leaves the text from the launch keyword on."""
    _HEDGE_MODE[0] = "launch"
    try:
        return _flatten(node, consts)
    finally:
        _HEDGE_MODE[0] = "name"


def _receiver_ok(left: list) -> bool:
    """Can these items (a literal character, or None for any text) read as a dotted receiver, a run of ``segment.`` segments?"""
    if not left:
        return True
    # An empty segment (a leading dot, ``..``) is a segment: ``operator.attrgetter`` splits on dots and looks each piece up as written, and
    # ``getattr(holder, '')`` can hold an object (R8-F1). Any other character (a hyphen, a space) is part of an attribute name too.
    return left[-1] is None or left[-1] == "."  # the receiver ends in a dot, or in text that can


@functools.lru_cache(maxsize=4096)
def _could_name_pattern(built: str, names: frozenset) -> bool:
    items = _pattern_items(built)  # a literal character, or None for a run of wildcards; refused past the cap (r15 A6)
    for n in names:
        # The name is the tail of the text; everything before it is a dotted receiver. Try every place it can begin, including inside a wildcard,
        # and ask of the tail that three of its word characters are literal text: a receiver is not the name, so 'loop.value_*' (a name that must
        # come wholly from the wildcard) and 'allowed_roots.*' say nothing, while 'su*pr*ce*s_*x*c' and 'root.component_*.subprocess_exec' do.
        # Exact: no bound on the depth of the receiver or on the number of segments a wildcard completes.
        splits = []
        literals = 0
        for b in range(len(items), -1, -1):
            if b < len(items) and items[b] is not None:
                literals += 1
                if literals > len(n):
                    break
            splits.append((items[:b], items[b:]))
            if b > 0 and items[b - 1] is None:
                splits.append((items[:b], [None] + items[b:]))  # the wildcard supplies the end of the receiver and the start of the name
        for left, right in splits:
            if sum(1 for it in right if it is not None and (it.isalnum() or it == "_")) < 3 or not _receiver_ok(left):
                continue
            if re.fullmatch("".join(".*" if it is None else re.escape(it) for it in right), n, re.DOTALL):
                return True
    return False


def _could_name(built: str, known: bool, names) -> bool:
    """Can this folded text (``_WILD`` = any text) read as one of ``names``, possibly behind a dotted receiver (``loop.subprocess_exec``)?"""
    if _WILD not in built:  # every uncertainty is a wildcard, so a text without one is exact whatever `known` says
        return any(built == n or re.fullmatch(r"(?:[^.]*\.)+" + re.escape(n), built) for n in names)
    return _could_name_pattern(built, frozenset(names))


def _fold_args(args: list[ast.AST], keywords: list[ast.keyword], consts: dict[str, str], depth: int):
    """(positional folds, keyword folds, positional unknown past the folds, keywords unknown) for a ``.format`` call."""
    positional, any_pos = [], False
    for a in args:
        if isinstance(a, ast.Starred):
            any_pos = True  # `*rest` may be any number of arguments: every position from here on is unknown
            break
        positional.append(_formatted(a, consts, depth))
    named, any_named = {}, False
    for kw in keywords:
        if kw.arg is None:
            any_named = True  # `**rest` may rebind any name
        else:
            named[kw.arg] = _formatted(kw.value, consts, depth)
    return positional, named, any_pos, any_named


_CONTAINER_BUILDERS = frozenset({"list", "tuple", "set", "frozenset", "dict", "sorted", "reversed", "enumerate", "zip", "map", "filter", "iter"})


def _padded(text: str) -> str:
    """The text an unknown text may print as through a conversion (``!r``, ``%a``) or a spec: its literal characters with anything around them."""
    core = text.strip(_WILD)
    return _WILD + core + _WILD if core.replace(_WILD, "") else _WILD


def _padded_by(text: str, spec: str | None) -> str:
    """``_padded`` for a value printed through a format spec: a PRECISION (``.0``, ``.3``, ``%.s``) keeps only a prefix of the value, so the text is not there to
    be relied on; a spec with a wildcard or a nested field (``{:{}}``) may hold one. Either reads as any text (pass-33 A2)."""
    if spec is not None and ("." in spec or "{" in spec or _WILD in spec):
        return _WILD
    return _padded(text)


def _formatted(node: ast.AST, consts: dict[str, str], depth: int) -> tuple[str, bool]:
    """``_flatten`` of a value that is about to be PRINTED (an f-string field, a ``.format`` / ``%`` argument). Printing a bytes value shows its repr
    (``b'abc'``), not its characters, and the fold does not track which values are bytes: an expression that holds a bytes literal (and does not
    decode it back to a text) reads as its text with anything around it (pass-26 A1)."""
    text, known = _flatten(node, consts, depth + 1)
    shown = _bound_form(node, consts)
    if (isinstance(shown, (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
            or (isinstance(shown, ast.Call) and isinstance(shown.func, ast.Name) and shown.func.id in _CONTAINER_BUILDERS)
            or (isinstance(shown, ast.Subscript) and isinstance(shown.slice, ast.Slice) and isinstance(_bound_form(shown.value, consts), (ast.List, ast.Tuple)))):
        # pass-28 A5: printing a container shows its repr (``['abc']``): the brackets, the quotes and the commas are text the fold does not carry
        return (text if text.startswith(_WILD) else _WILD + text) + ("" if text.endswith(_WILD) else _WILD), False
    if (known and _WILD not in text and isinstance(shown, ast.Call) and isinstance(shown.func, ast.Attribute) and shown.func.attr == "encode"
            and all(ord(c) < 256 for c in text)):
        _check_size(_repr_bound(text, True))  # `\xNN` for each byte: four characters for one (R11-L1)
        return repr(text.encode("latin-1")), True  # pass-29 A3: the bytes the fold holds one character per byte print as Python prints them, exactly
    if not any((isinstance(n, ast.Constant) and isinstance(n.value, bytes))
               or (isinstance(n, ast.Call) and ((isinstance(n.func, ast.Attribute) and n.func.attr == "encode")  # pass-29 A3: ``'..'.encode()`` is a bytes value too
                                                or (isinstance(n.func, ast.Name) and (n.func.id in ("bytes", "bytearray") or n.func.id in _BYTES_ALIASES))))
               for n in ast.walk(node)):
        return text, known
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "decode":
        return text, known  # a str: printed as it is
    return (text if text.startswith(_WILD) else _WILD + text) + ("" if text.endswith(_WILD) else _WILD), False


def _fold_mapping(node: ast.AST | None, consts: dict[str, str], depth: int) -> tuple[dict, bool]:
    """(folds by key, whether any key can be rebound) for a mapping argument; anything but a plain ``{'k': v}`` literal is unknown."""
    if not isinstance(node, ast.Dict):
        return {}, True
    if any(k is None or not (isinstance(k, ast.Constant) and isinstance(k.value, str)) for k in node.keys):
        return {}, True  # `**other`, a computed key: the dict may rebind any name
    return {k.value: _formatted(v, consts, depth) for k, v in zip(node.keys, node.values)}, False


# Calls whose result is not a piece of their argument text (they look an attribute up, test, measure or print): hedging their arguments would report the
# CONSUMER of a name (``getattr(loop, 'subprocess_exec_x')``) as if it were the name, so only the call that RETURNS the text counts (G15 narrowing).
_NOT_TEXT_OF_ARGS = frozenset({"getattr", "hasattr", "setattr", "delattr", "attrgetter", "methodcaller", "itemgetter", "isinstance", "issubclass", "len",
                               "print", "callable", "id", "hash", "bool", "int", "float", "type", "vars", "dir"})
_OPERATOR_BINOPS = {"add": ast.Add, "concat": ast.Add, "mod": ast.Mod, "mul": ast.Mult}
_DUNDER_BINOPS = {"__add__": (ast.Add, False), "__radd__": (ast.Add, True), "__mod__": (ast.Mod, False), "__rmod__": (ast.Mod, True),
                  "__mul__": (ast.Mult, False), "__rmul__": (ast.Mult, True)}

_STR_METHODS = frozenset(name for name in dir(str) if not name.startswith("_")) | {"decode"}  # every public method of str, for a call spelled through the type


_AMBIGUOUS_ACTIVE: set[int] = set()
_STR_ALIASES: set[str] = set()  # names the file being scanned binds to the str type (`S = str`, `from builtins import str as S`); set per scan by _note_aliases
_BYTES_ALIASES: set[str] = set()  # the subset of _STR_ALIASES bound to the bytes type (`from builtins import bytes as B`): B(s, enc) encodes, str(b, enc) decodes
_STR_KIND_ALIASES: set[str] = set()  # the subset of _STR_ALIASES bound to a type that is provably str (`S = str`, `T = type('')`); an alias in neither kind set is of unproven type
_BUILTINS_NAMES: set[str] = set()  # names the file binds to the builtins module (`import builtins`, `import builtins as b`) and nothing else; `b.str` is the builtin str only through one
_REBOUND_TYPES: set[str] = set()  # `str` / `bytes` the file binds to something other than the builtin (`str = bytes`, `def str`, `class bytes`, an argument): the bare name proves nothing
_CODEC_ALIASES: set[str] = set()  # names the file being scanned binds to the codecs module (`import codecs as c`); set per scan by _note_aliases
_CODEC_FUNCS: dict[str, str] = {}  # names the file binds to codecs.decode / codecs.encode (`from codecs import decode as d`), to the method they stand for


_TEXT_BASE_MEMO: dict[tuple[int, int], tuple[ast.AST, dict[str, str], bool]] = {}  # per scan: (id(node), id(consts)) -> (node, consts, answer); cleared by _note_aliases


def _type_kind(node: ast.AST) -> str | None:
    """r15 (HUB RULING, A3): which type a callee provably is, and None for everything else. ``"bytes"`` / ``"str"`` for the builtin NAME (unless the file rebinds it), a name
    bound to one (an alias chain that ends at the builtin), ``builtins.bytes`` through the builtins module, ``type(<literal>)`` and ``<literal>.__class__``. An attribute
    (``T.str``, ``self.bytes``), a class attribute, ``type(x)`` and ``x.__class__`` of a name are UNKNOWN: the type is never inferred from how an attribute is spelled."""
    if isinstance(node, ast.Name):
        if node.id in _REBOUND_TYPES:
            return None
        is_bytes, is_str = node.id == "bytes" or node.id in _BYTES_ALIASES, node.id == "str" or node.id in _STR_KIND_ALIASES
        return "bytes" if is_bytes and not is_str else "str" if is_str and not is_bytes else None  # a name bound to both is of unproven type
    if isinstance(node, ast.Attribute):
        if node.attr in ("str", "bytes"):
            return node.attr if isinstance(node.value, ast.Name) and node.value.id in _BUILTINS_NAMES else None
        operand = node.value if node.attr == "__class__" else None
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "type" and len(node.args) == 1 and not node.keywords:
        operand = node.args[0]
    else:
        return None
    if isinstance(operand, ast.Constant):
        return "bytes" if isinstance(operand.value, bytes) else "str" if isinstance(operand.value, str) else None
    return "str" if isinstance(operand, ast.JoinedStr) else None


def _note_aliases(tree: ast.AST) -> None:
    _TEXT_BASE_MEMO.clear()
    _STR_ALIASES.clear()
    _BYTES_ALIASES.clear()
    _STR_KIND_ALIASES.clear()
    _BUILTINS_NAMES.clear()
    _REBOUND_TYPES.clear()
    _CODEC_ALIASES.clear()
    _CODEC_FUNCS.clear()
    for n in ast.walk(tree):  # the names an import gives the types come first: an alias of one (`C = B`) is read against them
        if isinstance(n, ast.ImportFrom) and n.module == "builtins":
            _STR_ALIASES.update(a.asname or a.name for a in n.names if a.name in ("str", "bytes"))
            _BYTES_ALIASES.update(a.asname or a.name for a in n.names if a.name == "bytes")
            _STR_KIND_ALIASES.update(a.asname or a.name for a in n.names if a.name == "str")
        elif isinstance(n, ast.Import):
            _CODEC_ALIASES.update(a.asname or a.name for a in n.names if a.name == "codecs")
            _BUILTINS_NAMES.update(a.asname or a.name for a in n.names if a.name == "builtins")
        if isinstance(n, ast.ImportFrom) and n.module == "codecs":
            _CODEC_FUNCS.update({a.asname or a.name: a.name for a in n.names if a.name in ("decode", "encode")})  # from codecs import decode as d
    for n in ast.walk(tree):
        bound = ([t.id for t in n.targets if isinstance(t, ast.Name)] if isinstance(n, ast.Assign) else [n.target.id] if isinstance(n, (ast.AnnAssign, ast.AugAssign)) and isinstance(n.target, ast.Name)
                 else [n.name] if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else [n.arg] if isinstance(n, ast.arg)
                 else [a.asname or a.name.split(".")[0] for a in n.names if not (isinstance(n, ast.ImportFrom) and n.module == "builtins" and a.name in ("str", "bytes") and not a.asname)]
                 if isinstance(n, (ast.Import, ast.ImportFrom)) else [])
        _REBOUND_TYPES.update(name for name in bound if name in ("str", "bytes"))
        if isinstance(n, ast.Import):
            continue
        _BUILTINS_NAMES.difference_update(name for name in bound if name == "builtins")  # `builtins = Fake` is not the module
    binds = [(n.targets[0].id, n.value) for n in ast.walk(tree) if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name)]
    binds += [(n.target.id, n.value) for n in ast.walk(tree) if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None]  # `B: type = bytes` (pass-35 A3)
    for _ in range(len(binds) + 1):  # an alias of an alias (`C = B`) is read once `B` is known; repeated until nothing new is learnt
        known = (len(_STR_ALIASES), len(_BYTES_ALIASES), len(_STR_KIND_ALIASES))
        for name, v in binds:
            kind = _type_kind(v)  # r15 (A3): a name is an alias of a type only when the value PROVES the type; `C = T.str` / `C = type(x)` bind an unknown
            if kind == "bytes":
                _STR_ALIASES.add(name)
                _BYTES_ALIASES.add(name)  # `B = bytes`, `B = type(b'')`, `B = b''.__class__`, `C = B`
            elif kind == "str":
                _STR_ALIASES.add(name)
                _STR_KIND_ALIASES.add(name)
        if known == (len(_STR_ALIASES), len(_BYTES_ALIASES), len(_STR_KIND_ALIASES)):
            break


def _codecs_like(recv: ast.AST) -> bool:
    """The ``codecs`` module under any name the file gives it (``codecs``, ``import codecs as c``, ``x.codecs``): ``codecs.decode(b, enc, err)`` is ``b.decode(enc, err)``."""
    return (isinstance(recv, ast.Name) and (recv.id == "codecs" or recv.id in _CODEC_ALIASES)) or (isinstance(recv, ast.Attribute) and recv.attr == "codecs")


def _type_like(recv: ast.AST) -> bool:
    """``str`` / ``bytes`` under any name that PROVES the type (``_type_kind``): the receiver of an UNBOUND string method. ``type(x)`` and an attribute are not."""
    return _type_kind(recv) is not None


def _bound_form(node: ast.AST, consts: dict[str, str]) -> ast.AST:
    """The bound form of a call the fold reads under another spelling (G15): ``str.m(s, ...)`` is ``s.m(...)``, ``operator.concat(a, b)`` and
    ``a.__add__(b)`` are ``a + b`` (``add`` / ``mod`` / ``mul`` and their reflected forms too), and ``reversed([a, b])`` / ``list([a, b])`` are the
    list they stand for. Anything else is returned as it came."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("reversed", "list", "tuple") and len(node.args) == 1 and not node.keywords:
        arg = node.args[0]
        if isinstance(arg, (ast.List, ast.Tuple)) and not any(isinstance(e, ast.Starred) for e in arg.elts):
            return ast.List(elts=list(reversed(arg.elts)) if node.func.id == "reversed" else list(arg.elts), ctx=ast.Load())
        return node
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _OPERATOR_BINOPS and len(node.args) == 2 and not node.keywords
            and not any(isinstance(a, ast.Starred) for a in node.args)):  # ``from operator import concat``: concat(a, b)
        return ast.BinOp(left=node.args[0], op=_OPERATOR_BINOPS[node.func.id](), right=node.args[1])
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _CODEC_FUNCS and node.args and not isinstance(node.args[0], ast.Starred):
        return ast.Call(func=ast.Attribute(value=node.args[0], attr=_CODEC_FUNCS[node.func.id], ctx=ast.Load()), args=node.args[1:], keywords=node.keywords)
    if (isinstance(node, ast.Call) and _type_like(node.func) and node.args and not isinstance(node.args[0], ast.Starred)
            and (len(node.args) > 1 or any(k.arg in ("encoding", "errors") for k in node.keywords))):
        # R9-F1: ``str(b, 'ascii', 'ignore')`` is ``b.decode('ascii', 'ignore')`` and ``bytes(s, 'ascii', 'ignore')`` is ``s.encode(...)``; pass-28 A1: the type under any
        # name that proves it (``builtins.str``, ``S = str``, ``type('')``, ``''.__class__``) is the same call. r15 (HUB RULING, A3): a callee that is not PROVEN to be
        # the builtin is no constructor the fold models (it is no longer told by its first argument): the generic fallback hedges it and its result is unknown.
        kind = _type_kind(node.func)
        first = node.args[0]
        return ast.Call(func=ast.Attribute(value=first, attr="encode" if kind == "bytes" else "decode", ctx=ast.Load()),
                        args=node.args[1:], keywords=node.keywords)
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return node
    attr, recv = node.func.attr, node.func.value
    plain = not any(isinstance(a, ast.Starred) for a in node.args)
    if plain and not _text_base(recv, consts):
        # The receiver is a module or a type under ANY name (``op = operator``, ``import builtins as b``, ``S = str``, ``type('')``, ``''.__class__``): a call of an
        # operator function, a dunder or a string method through it is read by its method name, never by the spelling of the receiver (pass-22 A3).
        if attr in _OPERATOR_BINOPS and len(node.args) == 2 and not node.keywords:  # (mod's second operand may be a tuple or a mapping)
            return ast.BinOp(left=node.args[0], op=_OPERATOR_BINOPS[attr](), right=node.args[1])
        if attr in _DUNDER_BINOPS and len(node.args) == 2 and not node.keywords:  # str.__add__(a, b)
            op, reflected = _DUNDER_BINOPS[attr]
            return ast.BinOp(left=node.args[1] if reflected else node.args[0], op=op(), right=node.args[0] if reflected else node.args[1])
        # A string method called on the TYPE: `str`, `builtins.str`, `type(x)`, `x.__class__`. Any other receiver may be an instance (`source.index('needle')`), whose
        # argument is not the text the method works on; those calls stay a call the fallback hedges (their arguments are hedged, never read as a receiver).
        if node.args and _text_base(node.args[0], consts) and (_type_like(recv) or (attr in ("decode", "encode") and _codecs_like(recv))):  # str.lower(s, ...) is s.lower(...); codecs.decode(b, ...) too
            node = ast.Call(func=ast.Attribute(value=node.args[0], attr=attr, ctx=ast.Load()), args=node.args[1:], keywords=node.keywords)
            recv = node.func.value
    if attr in _DUNDER_BINOPS and len(node.args) == 1 and not node.keywords and plain:
        op, reflected = _DUNDER_BINOPS[attr]
        left, right = (node.args[0], recv) if reflected else (recv, node.args[0])
        return ast.BinOp(left=left, op=op(), right=right)
    return node


# R9-F1. A ``decode`` / ``encode`` is run by Python itself where its codec and error mode are plain text names of the standard library (the key is the name
# with every non-alphanumeric dropped: ``utf_16-LE`` -> ``utf16le``), so the result is exactly Python's. Anything else reads as unknown.
_CODEC_KEYS = frozenset({"utf8", "u8", "utf", "ascii", "usascii", "646", "latin1", "latin", "l1", "iso88591", "iso8859", "8859", "cp819", "utf16", "utf16le", "utf16be",
                         "utf32", "utf32le", "utf32be", "cp1252", "windows1252", "utf8sig", "unicodeescape", "rawunicodeescape"})
_CODEC_ERRORS = frozenset({"strict", "ignore", "replace", "backslashreplace", "xmlcharrefreplace", "namereplace", "surrogateescape", "surrogatepass"})
_ESCAPE_SEQUENCE_RE = re.compile(r"\\(?:x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8}|N\{[^}]*\}|[0-7]{1,3}|.)", re.DOTALL)


def _fold_codec(node: ast.Call, text: str, known: bool, consts: dict[str, str], depth: int) -> tuple[str, bool]:
    """``text.decode(...)`` / ``text.encode(...)`` where bytes are read one character per byte (latin-1), so a slice of them cuts where Python cuts.

    A plain codec and error mode over a text with no unknown part are applied by Python itself (``bytes.decode`` / ``str.encode`` on the literal), so
    ``ignore`` drops the byte, ``replace`` leaves U+FFFD or ``?``, and ``backslashreplace`` / ``xmlcharrefreplace`` / ``namereplace`` /
    ``surrogateescape`` write what Python writes (R9-F1). Whatever the fold cannot reproduce exactly (an unknown or unlisted codec or error mode,
    ``*args`` / ``**kw``, a text with an unknown part, a call that raises) reads as UNKNOWN: every non-ASCII character is any text (it may be dropped,
    replaced, escaped or joined with its neighbours), a codec outside the list also rewrites escape sequences, and the text gets a wildcard on both
    sides when it has none, so `known` False always comes with the wildcard and the consumers never read a guess as a literal."""
    slots: dict[str, ast.AST] = dict(zip(("encoding", "errors"), node.args))
    shape_ok = len(node.args) <= 2 and not any(isinstance(a, ast.Starred) for a in node.args) and all(k.arg in ("encoding", "errors") and k.arg not in slots for k in node.keywords)
    plan = None
    codec_key = None  # the listed codec the call names, or None
    not_a_codec = False  # the call names a text that is no codec at all (`replace`): it raises, so it reads nothing
    if shape_ok:
        slots.update({k.arg: k.value for k in node.keywords})
        encoding = _flatten(slots["encoding"], consts, depth + 1) if "encoding" in slots else ("utf-8", True)
        errors = _flatten(slots["errors"], consts, depth + 1) if "errors" in slots else ("strict", True)
        if encoding[1] and re.sub(r"[^a-z0-9]", "", encoding[0].lower()) in _CODEC_KEYS:
            codec_key = re.sub(r"[^a-z0-9]", "", encoding[0].lower())
            if errors[1] and errors[0] in _CODEC_ERRORS:
                plan = (encoding[0], errors[0])
        else:
            not_a_codec = encoding[1] and not _is_codec(encoding[0])
    if codec_key is None and not not_a_codec and text.replace(_WILD, ""):
        # A codec the fold does not read (`rot_13`, `base64`, `hex`, one named by a run-time value, `*args`) may rewrite every letter of a LITERAL, so the result
        # may be any text, a method name or a launch word among them: it reads as every one of them (pass-25 A2). A run-time receiver says nothing.
        return _unread_codec_text(), False
    if plan is not None and _WILD not in text:
        try:
            if node.func.attr == "decode":
                if plan[1] == "backslashreplace":  # a byte the codec rejects is written `\xNN`: four characters for one, so the worst case is checked first (R11-L1)
                    _check_size(4 * len(text))
                return text.encode("latin-1").decode(*plan), known
            _check_size(_encoded_bound(text, encoding[0], plan[1]))  # a ValueError (a codec or mode with no proven row) is read as unknown below, before `encode` runs (R13)
            return text.encode(*plan).decode("latin-1"), known
        except _FoldTooBig:
            raise  # a RecursionError, which the clause below would read as "the call raises at run time"
        except Exception:  # the call raises at run time, or a text the other way round: not a result to read
            pass
    unknown = re.sub(r"[^\x00-\x7f]", _WILD, text)
    if codec_key in (None, "unicodeescape", "rawunicodeescape"):  # a codec that rewrites characters (`unicode_escape` turns `\x5f` into `_`): every escape sequence is any text
        unknown = _ESCAPE_SEQUENCE_RE.sub(_WILD, unknown)
    return (unknown if _WILD in unknown else _WILD + unknown + _WILD), False


@functools.lru_cache(maxsize=256)
def _is_codec(name: str) -> bool:
    """Does Python know a codec by this name? (Looked up by the standard library; a name that is not one makes the call raise, so it reads nothing.)"""
    if len(name) > 64 or not name.isascii() or "\x00" in name:
        return True  # not a name to look up: read it as the worst case
    try:
        codecs.lookup(name)
    except LookupError:
        return False
    except Exception:
        return True
    return True


def _bytes_or_text(text: str, known: bool) -> tuple[str, bool]:
    """A character from U+0080 to U+00FF is a byte of a bytes literal (read as latin-1) or a character of a str, and a case mapper or the default strip treats
    the two differently (``b'\\xdf'.upper()`` is ``b'\\xdf'``, ``'\\xdf'.upper()`` is ``'SS'``; ``b'\\xa0'.strip()`` keeps its byte, ``'\\xa0'.strip()`` does not).
    The fold does not track which it is reading, so such a character reads as any text (pass-25 A1)."""
    if re.search(r"[\x80-\xff]", text):
        return re.sub(r"[\x80-\xff]", _WILD, text), False
    return text, known


def _unread_codec_text() -> str:
    """What a literal read through a codec the fold does not know may be: a text that reads as EVERY spawn-method name (Python side) or as a launch word
    (PowerShell side), so a consumer reports it. A pattern that is only wildcards says nothing, and this one is not."""
    if _HEDGE_MODE[0] == "launch":
        return _WILD + "Start-Process" + _WILD
    return _WILD + _WILD.join(sorted(_LOOP_SPAWN_METHODS)) + _WILD


_FLATTEN_MAX_CHARS = 200_000


class _FoldTooBig(RecursionError):
    """A fold whose text outgrew ``_FLATTEN_MAX_CHARS`` (``'x'`` followed by 48 ``.replace('x', 'xx')`` is 2**48 characters). A RecursionError, so the scan's
    existing too-deep guard REPORTS the source instead of the scan allocating it or dying of MemoryError (pass-29 A5)."""


def _flatten(node: ast.AST, consts: dict[str, str], depth: int = 0) -> tuple[str, bool]:
    result = _flatten_node(node, consts, depth)
    _check_size(len(result[0]))
    return result


# R11-L1. The check above runs AFTER a fold built its text, which is too late for a fold that can make a text far longer than its inputs in one step
# (a ``replace`` of one character by 199,999, a ``join`` of many pieces, a template that uses one field many times, an escaping codec, a ``!r``). Every such
# fold works out the length its result WOULD have and refuses with ``_check_size`` before it builds anything. A fold whose output is never longer than
# its input (a slice, ``reversed``, ``strip``, ``chr``, a hedge that keeps a wildcard) needs none: its inputs were each checked by ``_flatten``.
_ASCII_RE = re.compile(r"[\x00-\x7f]")
_PRINTABLE_ASCII_RE = re.compile(r"[ -~]")


def _check_size(length: int) -> None:
    """Raise ``_FoldTooBig`` for a text of this length that has NOT been built (or has just been, by a fold that cannot grow past a small multiple)."""
    if length > _FLATTEN_MAX_CHARS:
        raise _FoldTooBig(length)


def _joined(parts: list[str], sep: str = "") -> str:
    """``sep.join(parts)`` refused before it builds anything: the pieces and the separators between them are added up first."""
    _check_size(sum(map(len, parts)) + len(sep) * max(len(parts) - 1, 0))
    return sep.join(parts)


def _repr_bound(text: str, as_bytes: bool = False) -> int:
    """An UPPER bound on ``len(repr(text))`` / ``len(ascii(text))`` (or of ``repr(text.encode('latin-1'))`` with ``as_bytes``), worked out from the characters
    and never by building the result: a character outside printable ASCII is written as at most ten characters (``\\U0010ffff``; four for a byte, ``\\xNN``), and
    a quote or a backslash gains an escape. A chunk-by-chunk ``repr`` would pick its quote per chunk and miss the escapes of the whole (pass-30 A3)."""
    per, size = (4, len(text)) if as_bytes else (10, len(text))
    if per * size + 3 <= _FLATTEN_MAX_CHARS:
        return per * size + 3  # too short to reach the cap whatever it holds
    odd = len(_PRINTABLE_ASCII_RE.sub("", text))  # what is left once printable ASCII is taken out: the characters outside it
    return size + (per - 1) * odd + text.count("'") + text.count("\\") + 3


# R13 (sol r12 blocker). The rows `_encoded_bound` is PROVEN for, keyed by the codec's canonical name (``codecs.lookup(name).name``, so ``utf16``, ``UTF_16``, ``u16``
# land on ``utf-16``): the width in bytes of one code unit the codec writes. The text an error mode writes in place of a character it cannot encode (``\ud800``,
# ``&#55296;``) is encoded BY THE CODEC like any other text, so it takes this many bytes for each of its characters: two under UTF-16, four under UTF-32.
_CODEC_UNIT_BYTES = {"ascii": 1, "iso8859-1": 1, "cp1252": 1, "utf-8": 1, "utf-8-sig": 1, "utf-16": 2, "utf-16-le": 2, "utf-16-be": 2,
                     "utf-32": 4, "utf-32-le": 4, "utf-32-be": 4}
_ESCAPE_CODEC_NAMES = frozenset({"unicode-escape", "raw-unicode-escape"})  # never consult an error mode: a character is `\xNN`, `\uNNNN` or `\UNNNNNNNN`
# The most characters an error mode writes for ONE code point: `\U0010ffff` and `&#1114111;` are ten, a `\N{...}` name up to a hundred (a name is at most ~90); the other
# modes write one (`replace` -> `?`), drop it (`ignore`), or write one byte (`surrogateescape`, `surrogatepass` at most four, covered by the floor below).
_HANDLER_REPLACEMENT_CHARS = {"strict": 1, "ignore": 1, "replace": 1, "surrogateescape": 1, "surrogatepass": 1, "backslashreplace": 10, "xmlcharrefreplace": 10, "namereplace": 100}


def _encoded_bound(text: str, encoding: str, errors: str) -> int:
    """An UPPER bound on ``len(text.encode(encoding, errors).decode('latin-1'))``, worked out from the characters and never by building the result. Raises
    ``ValueError`` for a codec or an error mode it has no proven row for, so the caller reads the call as unknown BEFORE ``encode`` runs.

    Per character: an ASCII one takes one code unit (``\\xNN``, four bytes, for a control character under the escape codecs); any other takes at most four bytes
    (UTF-8, a UTF-16 pair, UTF-32) or, when the codec cannot encode it (a lone surrogate under UTF-8 / UTF-16 / UTF-32, anything outside the table under ASCII /
    latin-1 / cp1252), the longest text the error mode writes for one code point TIMES the codec's code unit (``\\U0010ffff`` is ten characters, so 20 bytes under UTF-16
    and 40 under UTF-32). The escape codecs write at most ten bytes for one. A byte-order mark is up to four more. Every character is costed as the worst of those,
    so the sum cannot fall below what Python builds."""
    if not isinstance(encoding, str) or len(encoding) > 64 or not encoding.isascii() or "\x00" in encoding:
        raise ValueError(f"no bound row for the codec {encoding!r}")
    try:
        name = codecs.lookup(encoding).name
    except LookupError as exc:
        raise ValueError(f"no bound row for the codec {encoding!r}") from exc
    replacement_chars = _HANDLER_REPLACEMENT_CHARS.get(errors)
    if replacement_chars is None:
        raise ValueError(f"no bound row for the error mode {errors!r}")
    non_ascii = len(_ASCII_RE.sub("", text))
    ascii_count = len(text) - non_ascii
    if name in _ESCAPE_CODEC_NAMES:
        return 4 * ascii_count + 10 * non_ascii + 4
    unit = _CODEC_UNIT_BYTES.get(name)
    if unit is None:
        raise ValueError(f"no bound row for the codec {encoding!r} ({name})")
    return unit * ascii_count + max(4, replacement_chars * unit) * non_ascii + 4


def _replaced(text: str, old: str, new: str, count: int) -> str:
    """``text.replace(old, new, count)`` refused before ``str.replace`` runs: the result is ``len(text) + n * (len(new) - len(old))`` characters, where ``n``
    is how many places it replaces (``text.count(old)``, or ``len(text) + 1`` insertion points for an empty ``old``, and no more than ``count`` when it is >= 0)."""
    places = len(text) + 1 if not old else text.count(old)
    if count >= 0:
        places = min(places, count)
    _check_size(len(text) + places * (len(new) - len(old)))
    return text.replace(old, new, count)


def _mapped(method: str, text: str) -> str:
    """``text.<method>()`` for a case mapper, ``title`` or ``capitalize``: an ASCII character maps to one, any other to at most three (the ligature U+FB03
    upper-cases to ``FFI``), so the bound is worked out from the characters before the text is mapped (pass-30 A3). ``strip`` and its kin never lengthen."""
    if method not in ("strip", "lstrip", "rstrip") and 3 * len(text) > _FLATTEN_MAX_CHARS:
        _check_size(len(text) + 2 * len(_ASCII_RE.sub("", text)))
    return getattr(text, method)()


class _SizedOut(list):
    """The pieces of a template being filled, with a running length: a piece that takes the total past the cap raises when it is added, before the pieces are
    joined (and before a padded copy of one field is added a hundred times over)."""

    def __init__(self) -> None:
        super().__init__()
        self.size = 0

    def append(self, piece: str) -> None:
        self.size += len(piece)
        _check_size(self.size)
        super().append(piece)


def _flatten_node(node: ast.AST, consts: dict[str, str], depth: int = 0) -> tuple[str, bool]:
    """(text, known): what a string expression reads as when every piece is a string literal or a name bound once to one.

    Folds adjacent literals, ``+``, ``%`` and ``.format`` / ``.format_map`` templates, ``.join`` and ``.replace`` over literals, and an
    f-string whose holes fold. A piece that does not fold (a name bound to a run-time value, a number, a spec the fold does not apply, a
    set whose order is unknown) is the wildcard ``_WILD``: ``known`` is then False and the text reads as a pattern in which the wildcard
    stands for any text, so ``'Sta' + name + 'rt-Process'`` reads as ``Sta<any>rt-Process`` and a fold mistake can only over-report. The scan fills the template
    itself; it never calls ``eval``/``exec`` and never hands scanned text to ``str.format`` (a ``{:>999999999}`` spec must not allocate).
    """
    if depth > _FLATTEN_DEPTH:
        # r14 (pass-35 A2): an expression nested deeper than the fold reads is not read as NOTHING (a `.lower()` chain 49 deep would turn a spawn name into a clean source): it reads as
        # the literal text it holds, in source order, with a wildcard on each side, so it is reported exactly when those literals can name a spawn method or a launch.
        pieces = sorted((c for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, (str, bytes))), key=lambda c: (c.lineno, c.col_offset))
        return _WILD + _joined([c.value if isinstance(c.value, str) else c.value.decode("latin-1") for c in pieces]) + _WILD, False
    node = _bound_form(node, consts)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bytes):
            return node.value.decode("latin-1"), True  # b'subprocess_' + b'exec' read as the text it spells, one character per byte (pass-24 A2)
        return (node.value, True) if isinstance(node.value, str) else (_WILD, False)  # a number can print as any character (`{115:c}`)
    if isinstance(node, ast.Name):
        value = consts.get(node.id) if isinstance(node.ctx, ast.Load) else None
        return (value, True) if value is not None else (_WILD, False)
    if isinstance(node, ast.JoinedStr):
        parts = []
        for v in node.values:
            if isinstance(v, ast.Constant):
                parts.append((v.value, True))
            elif isinstance(v, ast.FormattedValue):
                text, known = _formatted(v.value, consts, depth)
                if v.conversion in (-1, 115) and v.format_spec is None:
                    parts.append((text, known))  # `{x}` / `{x!s}` of a text is that text; a wildcard inside it stays
                    continue
                spec, spec_known = _flatten(v.format_spec, consts, depth + 1) if v.format_spec is not None else (None, True)
                shown = _apply_spec(text, {115: "s", 114: "r", 97: "a"}.get(v.conversion), spec) if known and spec_known else None
                if shown is None:  # `!r`, `!a` or a spec of a text with an unknown part: that text, with anything around it (pass-26 A1)
                    parts.append((_padded_by(text, spec), False))
                else:
                    parts.append((shown, True))
            else:
                parts.append((_WILD, False))
        return _joined([t for t, _ in parts]), all(k for _, k in parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        leaves, todo = [], [node]  # iterative: a long chain must not exhaust the stack
        while todo:
            cur = todo.pop()
            if isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Add):
                todo.append(cur.right)
                todo.append(cur.left)
            else:
                leaves.append(cur)
        parts = [_flatten(leaf, consts, depth + 1) for leaf in leaves]
        return _joined([t for t, _ in parts]), all(k for _, k in parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        template, known = _flatten(node.left, consts, depth + 1)
        right = node.right
        if isinstance(right, ast.Dict):
            named, any_named = _fold_mapping(right, consts, depth)
            return _fill_percent(template, [], named, known, any_pos=False, any_named=any_named)
        if isinstance(right, ast.Tuple):
            positional, _, any_pos, _ = _fold_args(right.elts, [], consts, depth)
            return _fill_percent(template, positional, {}, known, any_pos=any_pos, any_named=False)
        if isinstance(right, (ast.Constant, ast.JoinedStr, ast.BinOp)):
            return _fill_percent(template, [_formatted(right, consts, depth)], {}, known, any_pos=False, any_named=False)
        # a name, call or subscript on the right may be a tuple or a mapping at run time: every field is unknown
        return _fill_percent(template, [], {}, known, any_pos=True, any_named=True)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):  # string repetition: `('subprocess_' + kind) * 1` is the text itself, `* n` repeats it
        texts = [t for t, _ in (_flatten(node.left, consts, depth + 1), _flatten(node.right, consts, depth + 1)) if t.replace(_WILD, "")]
        return (_WILD + texts[0] + _WILD, False) if len(texts) == 1 else (_WILD, False)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method, recv = node.func.attr, node.func.value
        if method == "join" and len(node.args) == 1 and not node.keywords:
            node = ast.Call(func=node.func, args=[_bound_form(node.args[0], consts)], keywords=[])  # ''.join(reversed(['a', 'b'])) joins the list reversed
        if (method in _STR_METHODS and node.args and not isinstance(node.args[0], ast.Starred) and not _text_base(recv, consts) and _text_base(node.args[0], consts)
                and id(node.func) not in _AMBIGUOUS_ACTIVE):  # keyed by the callee: a rebuilt call node shares it
            # `S.format('{}{}', 'a', 'b')`: S may be the str type under a name this scan cannot resolve (the first argument is then the text) or an instance of
            # something (the first argument is then an argument). Both readings are kept, side by side (pass-23 A1).
            _AMBIGUOUS_ACTIVE.add(id(node.func))
            try:
                own = _flatten(node, consts, depth + 1)[0]
            finally:
                _AMBIGUOUS_ACTIVE.discard(id(node.func))
            alt = _flatten(ast.Call(func=ast.Attribute(value=node.args[0], attr=method, ctx=ast.Load()), args=node.args[1:], keywords=node.keywords), consts, depth + 1)[0]
            return (own if own == alt else _joined([own, _WILD, alt])), False
        if method in ("format", "format_map"):
            template, known = _flatten(recv, consts, depth + 1)
            if method == "format_map":
                named, any_named = _fold_mapping(node.args[0] if node.args else None, consts, depth)
                return _fill_format(template, [], named, known, any_pos=False, any_named=any_named)
            positional, named, any_pos, any_named = _fold_args(node.args, node.keywords, consts, depth)
            return _fill_format(template, positional, named, known, any_pos=any_pos, any_named=any_named)
        if method == "join" and len(node.args) == 1 and not node.keywords and not isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set, ast.Starred)):
            sep, sep_known = _flatten(recv, consts, depth + 1)
            if sep == "" and sep_known:
                return _flatten(node.args[0], consts, depth + 1)  # ''.join(reversed(s)) / ''.join(s): the characters of s put back together
        if method == "join" and len(node.args) == 1 and not node.keywords and isinstance(node.args[0], (ast.List, ast.Tuple, ast.Set)):
            sep, sep_known = _flatten(recv, consts, depth + 1)
            elts = node.args[0].elts
            if any(isinstance(e, ast.Starred) for e in elts):
                return _WILD, False  # `[*rest]` may hold any number of pieces
            parts = [_flatten(e, consts, depth + 1) for e in elts]
            if isinstance(node.args[0], ast.Set):
                # a set holds each distinct element once, in an order that depends on the hash seed: one distinct known text is exact,
                # anything else could read in any order (or be a different set altogether)
                distinct = {t for t, k in parts if k}
                if len(distinct) == 1 and all(k for _, k in parts):
                    return distinct.pop(), True  # one element: the separator is never written
                return _WILD, False
            if len(parts) <= 1:  # nothing to put a separator between: it contributes no characters, so its uncertainty must not leak
                return (parts[0][0], parts[0][1]) if parts else ("", True)
            return _joined([t for t, _ in parts], sep), sep_known and all(k for _, k in parts)
        if method == "replace" and 2 <= len(node.args) + len(node.keywords) <= 3 and len(node.args) >= 2 and all(kw.arg == "count" for kw in node.keywords):
            text, known = _flatten(recv, consts, depth + 1)
            old, old_known = _flatten(node.args[0], consts, depth + 1)
            new, new_known = _flatten(node.args[1], consts, depth + 1)
            count = -1
            if len(node.args) + len(node.keywords) == 3:  # 'ssubprocess_exec'.replace('s', '', 1) / count=1: only the first one goes
                c = node.args[2] if len(node.args) == 3 else node.keywords[0].value
                count = _int_literal(c)
                count = (-1 if count < 0 or count > len(text) + 1 else count) if isinstance(count, int) else None  # str.replace takes a C ssize_t: more than the text holds is all
            if count is None or not (known and old_known and new_known):
                kept = _joined([text, new])  # what the result is made of: the text it keeps and the text it puts in
                if kept.replace(_WILD, ""):  # r15 (class rule): a replace the fold cannot apply exactly leaves a text that is reported when it holds literal text, never read as clean
                    return _unproven(kept), False
                return _hedge_texts([text, old, new]), False  # an unknown receiver (`S.replace(s, a, b)` through an alias of str) leaves its arguments as the text
            return _replaced(text, old, new, count), True  # refused BEFORE str.replace builds anything (R11-L1)
        if method in _STRING_MAPPERS and not node.keywords and len(node.args) <= 1:
            text, known = _flatten(recv, consts, depth + 1)
            if method in _CASE_MAPPERS and not node.args:
                text, known = _bytes_or_text(text, known)
                return _mapped(method, text), known  # per character: a wildcard stays one
            chars, chars_known = _flatten(node.args[0], consts, depth + 1) if node.args else (None, True)
            if not (known and chars_known):
                return _hedge_texts([text, chars or ""], True), False  # `S.lower('SUBPROCESS_EXEC')` with S an alias of str: the receiver reads as nothing, the argument is the text
            if chars is not None:
                return (getattr(text, method)(chars), True) if method in ("strip", "lstrip", "rstrip") else (_hedge_texts([text, chars], True), False)
            text, known = _bytes_or_text(text, True)  # the default strip takes the whitespace of a str: more than the whitespace of bytes
            return _mapped(method, text), known
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)) and not any(isinstance(e, ast.Starred) for e in node.elts):
        return _joined([_flatten(e, consts, depth + 1)[0] for e in node.elts]), False  # a list of texts read as the texts side by side (read only as an argument of a call the fold has no arm for)
    if isinstance(node, ast.Dict) and all(k is not None for k in node.keys):
        return _joined([_flatten(e, consts, depth + 1)[0] for pair in zip(node.keys, node.values) for e in pair]), False  # a mapping: its keys and values side by side
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("decode", "encode"):
        text, known = _flatten(node.func.value, consts, depth + 1)  # bytes are read one character per byte (latin-1), so a slice of them cuts where Python cuts
        return _fold_codec(node, text, known, consts, depth)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "reversed" and len(node.args) == 1 and not node.keywords:
        text, known = _flatten(node.args[0], consts, depth + 1)  # the characters of a text, backwards (only ever read by a ``''.join``)
        return text[::-1], known
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "chr" and len(node.args) == 1 and not node.keywords:
        code = _int_literal(node.args[0])  # chr(115) + 'ubprocess_exec'
        return (chr(code), True) if isinstance(code, int) and 0 <= code < 0x110000 else (_WILD, False)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "decode" and not node.args and not node.keywords
            and isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, bytes)):
        return node.func.value.value.decode("utf-8", "replace"), True  # b'subprocess_exec'.decode()
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):  # 'xsubprocess_exec'[1:], 'cexe_ssecorpbus'[::-1]
        text, known = _flatten(node.value, consts, depth + 1)
        bounds = [_int_literal(p) for p in (node.slice.lower, node.slice.upper, node.slice.step)]
        if known and "?" not in bounds and bounds[2] != 0:
            return text[slice(*bounds)], True
        return _hedge(text, True), False  # a bound the fold cannot read: some substring of the text, which may be a spawn-method name
    # A call the fold has no arm for (a string method such as `removeprefix`, `partition`, `split`; an unrecognised function) or an index into one: the
    # result is some piece of the receiver OR of an argument (``str.removeprefix(s, 'zz')``, ``ident('zz' + s)``), so the text of the receiver and of every
    # positional argument and keyword value is hedged, never dropped (G14, G15). A piece that reads as nothing (a run-time value) says nothing.
    if isinstance(node, ast.Call):
        callee = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
        parts = ([node.func.value] if isinstance(node.func, ast.Attribute) else []) + (
            [] if callee in _NOT_TEXT_OF_ARGS else [a for a in node.args if not isinstance(a, ast.Starred)] + [k.value for k in node.keywords])
    else:
        parts = [node.value] if isinstance(node, ast.Subscript) else []
    recv = node.func.value if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) else None
    cuts = recv is not None and _text_base(recv, consts) and not (isinstance(recv, ast.BinOp) and not isinstance(recv.op, (ast.Add, ast.Mod, ast.Mult)))  # a string method; `path / 'x'` is no text
    return _hedge_texts([_flatten(part, consts, depth + 1)[0] for part in parts], cuts), False


def _hedge_texts(texts: list[str], cut: bool = False) -> str:
    """What a call the fold cannot apply may return of these texts (its receiver and arguments; a text that reads as nothing says nothing)."""
    texts = [t for t in texts if t.replace(_WILD, "")]
    if not texts:
        return _WILD
    # What the call returns may be any one of its texts, OR the texts put together in either order or read backwards (an aliased ``op.concat(a, b)``,
    # ``fmt(a, b)``, ``rv(s)``): each piece alone is hedged, and so is the whole joined both ways and reversed (pass-22 A3).
    joined = _joined(texts)  # refused past the cap before it is built (R11-L1); the reversed copies are the same length
    variants = [*texts, joined, "".join(reversed(texts)), joined[::-1]]
    if len(texts) >= 2:  # a function that fills a template (``fmt('%sexec', 'subprocess_')``): the first or the last text filled with the others
        for at in (0, len(texts) - 1):
            rest = [(t, True) for i, t in enumerate(texts) if i != at]
            variants.append(_fill_percent(texts[at], rest, {}, True)[0])
            variants.append(_fill_format(texts[at], rest, {}, True)[0])
    return _joined(list(dict.fromkeys(_hedge(v, cut) for v in variants)))  # the launch reading keeps the tail of each variant: up to one text per variant


def _int_literal(node: ast.AST | None) -> int | None | str:
    """The int a slice bound spells (``1``, ``-1``), None for an absent bound, ``"?"`` for anything else."""
    if node is None:
        return None
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        inner = _int_literal(node.operand)
        return -inner if isinstance(inner, int) and isinstance(node.op, ast.USub) else inner if isinstance(inner, int) else "?"
    return node.value if isinstance(node, ast.Constant) and type(node.value) is int else "?"


def _apply_spec(text: str, conv: str | None, spec: str | None) -> str | None:
    """``format(text, spec)`` after a ``!r`` / ``!a`` conversion, or None when the spec cannot be applied safely or is not valid for a string."""
    if conv in ("r", "a"):  # a character may be written as `\U0010ffff` (ten characters): the bound is worked out from the characters before the result is built (R11-L1)
        _check_size(_repr_bound(text))
        text = repr(text) if conv == "r" else ascii(text)
    if not spec:
        return text
    if _spec_too_big(re.findall(r"\d+", spec)):  # Python reads any Unicode decimal digit as a width
        return None
    try:
        return format(text, spec)
    except (ValueError, TypeError):
        return None


def _percent_field(template: str, at: int):
    """(key, flags, width, precision, conversion, end) for the ``%`` field at ``at``, or None. A ``%(key)`` key runs to its BALANCED close paren, as
    Python reads it (``%(a(b))s`` has the key ``a(b)``)."""
    i, key = at + 1, None
    if template.startswith("(", i):
        depth = 0
        for j in range(i, len(template)):
            depth += (template[j] == "(") - (template[j] == ")")
            if depth == 0:
                key, i = template[i + 1:j], j + 1
                break
        else:
            return None
    spec = _PERCENT_SPEC_RE.match(template, i)
    return None if spec is None else (key, *spec.groups(), spec.end())


def _fill_unknown_template(template: str, percent: bool) -> tuple[str, bool]:
    """A template that itself holds a wildcard has an unknown structure: the wildcard may open, close or lengthen a field. So every field in
    a literal segment is unknown, and the start of each segment after a wildcard may be the tail of a field the wildcard opened (up to its
    closing brace, or up to its key, flags, width and conversion character): that much of the segment is not trusted, the rest is kept."""
    out = []
    for i, seg in enumerate(re.split(re.escape(_WILD) + "+", template)):
        # The head of a segment after a wildcard may be the tail of a field the wildcard opened, or may not: BOTH readings are kept (side by side, a wildcard
        # between them), because dropping the head hides the literal text it holds (``(prefix + 'subprocess_exec{tail}').format(tail='')``, pass-23 A4).
        lead = ""
        fill = _fill_percent if percent else _fill_format
        if i:
            # The head may be the tail of a field the wildcard opened (a key up to its paren, flags, width, precision and ONE conversion character; or up to a
            # closing brace) or plain text. BOTH readings are kept: the head as plain text (a literal name in it is a name, ``'subprocess_exec)s'``), then the
            # rest after the field tail the wildcard may have opened, with a wildcard between (pass-23 A4, pass-27 A1).
            trimmed = None
            if percent and not seg.startswith("%"):  # a segment that opens with its own field has no tail of an earlier one to shed
                trimmed = seg[re.match(r"(?:[^()]*\))?[#0\- +0-9.*hlL]*[A-Za-z%]?", seg).end():]
            elif not percent:
                close = seg.find("}")
                if close >= 0 and "{" not in seg[:close]:  # a field the wildcard opened closes at this brace; a brace after a '{' closes the segment's OWN field
                    trimmed = seg[close + 1:]
            if trimmed is not None:
                lead = _joined([_WILD, fill(seg, [], {}, False, any_pos=True, any_named=True)[0], _WILD])
                seg = trimmed
        out.append(_joined([lead, fill(seg, [], {}, False, any_pos=True, any_named=True)[0]]))
    return _joined(out, _WILD), False


def _fill_format(template: str, positional: list[tuple[str, bool]], named: dict[str, tuple[str, bool]], known: bool,
                 any_pos: bool = False, any_named: bool = False) -> tuple[str, bool]:
    """Fill a ``str.format`` template field by field. A field the fold cannot read (a missing or unknown argument, a nested field, an
    accessor it does not apply) is the wildcard, never a wrong known text; with ``**`` in play no keyword is trusted."""
    if _WILD in template:
        return _fill_unknown_template(template, False)
    out = _SizedOut()  # a running length: a piece that takes it past the cap raises before the pieces are joined (R11-L1)
    ok = known
    auto, i, n = 0, 0, len(template)
    while i < n:
        ch = template[i]
        if ch == "{" and template.startswith("{{", i):
            out.append("{")
            i += 2
        elif ch == "}" and template.startswith("}}", i):
            out.append("}")
            i += 2
        elif ch == "}":
            out.append(_WILD)  # a lone `}` is a ValueError at run time
            ok = False
            i += 1
        elif ch == "{":
            depth, j = 1, i + 1
            while j < n and depth:
                depth += (template[j] == "{") - (template[j] == "}")
                j += 1
            if depth:  # never closed
                out.append(_WILD)
                ok = False
                break
            body, i = template[i + 1:j - 1], j
            match = None if "{" in body else _FORMAT_BODY_RE.fullmatch(body)  # a nested field in the spec is not applied
            if match is None:
                out.append(_WILD)
                ok = False
                continue
            field_name, conv, spec = match.group(1), match.group(2), match.group(3)
            name = re.match(r"\w*", field_name).group(0)
            accessors = field_name[len(name):]
            if name == "":
                index, auto = auto, auto + 1
                value = positional[index] if index < len(positional) else None
            elif re.fullmatch(r"[0-9]{1,4}", name):  # an ASCII index of sane length; anything else (`²`, 5000 digits) is unknown, never int()'d
                value = positional[int(name)] if int(name) < len(positional) else None
            else:
                value = None if any_named else named.get(name)
            if value is not None and accessors:
                index = re.fullmatch(r"\[([0-9]{1,4})\]", accessors)  # str.format reads `[-1]` as the string key '-1', which a str rejects
                try:
                    value = (value[0][int(index.group(1))], True) if index and value[1] else None
                except IndexError:
                    value = None
            if value is None:
                out.append(_WILD)
                ok = False
            elif value[1]:
                shown = _apply_spec(value[0], conv, spec)
                out.append(shown if shown is not None else _WILD)
                ok = ok and shown is not None
            elif conv is None and not spec:
                out.append(value[0])  # an unknown text printed as it is: its wildcard stays in place
                ok = False
            else:
                out.append(_padded_by(value[0], spec))  # printed through a conversion or a spec: its text, with anything around it (pass-26 A1), or any text under a precision (pass-33 A2)
                ok = False
        else:
            out.append(ch)
            i += 1
    return "".join(out), ok


def _fill_percent(template: str, positional: list[tuple[str, bool]], named: dict[str, tuple[str, bool]], known: bool,
                  any_pos: bool = False, any_named: bool = False) -> tuple[str, bool]:
    """Fill a ``%`` template. A field the fold cannot read (a number, a ``*`` width, a missing or unknown argument, a conversion other than
    ``%s`` of a known text) is the wildcard, never a wrong known text; with ``**`` or a run-time right operand no argument is trusted."""
    if _WILD in template:
        return _fill_unknown_template(template, True)
    out = _SizedOut()  # (R11-L1)
    ok = known
    nxt, lost, pos = 0, False, 0
    while True:
        at = template.find("%", pos)
        if at < 0:
            out.append(template[pos:])
            break
        out.append(template[pos:at])
        parsed = _percent_field(template, at)
        if parsed is None:  # a `%` the fold cannot parse: the field is unknown, but the literal text AFTER it is still the template's (pass-22 A1)
            out.append(_WILD)
            ok = False
            lost = True  # the arguments after an unknown field are not tracked
            pos = at + 1
            if template.startswith("(", pos):  # a key runs to its balanced close paren; with none it runs to the end
                depth = 0
                for j in range(pos, len(template)):
                    depth += (template[j] == "(") - (template[j] == ")")
                    if depth == 0:
                        pos = j + 1
                        break
                else:
                    pos = len(template)
            pos = _PERCENT_UNPARSED_RE.match(template, pos).end()  # what a field may still take: flags, width, precision, length, one conversion character
            continue
        key, flags, width, precision, conv, pos = parsed
        if conv == "%":
            out.append("%")
            continue
        if conv == "b":  # a bytes template's `%b` writes the bytes of its argument, as `%s` of the text the fold reads them as (pass-33 A1)
            conv = "s"
        if "*" in (width, precision):  # a width taken from the arguments: the positions after it are not tracked
            lost = True
            out.append(_WILD)
            ok = False
            continue
        if key is not None:
            value = None if any_named else named.get(key)
        else:
            index, nxt = nxt, nxt + 1
            value = None if lost or (any_pos and index >= len(positional)) else (positional[index] if index < len(positional) else None)
        if value is None:
            out.append(_WILD)
            ok = False
        elif conv not in "sra":
            out.append(_WILD)  # `%c` of a number is a character, `%d` a number: neither is a text the fold knows
            ok = False
        elif value[1] and not _spec_too_big((width, precision)):
            if conv in "ra":  # `%r` / `%a` write up to ten characters for one: the bound is worked out first (pass-30 A2)
                _check_size(_repr_bound(value[0]) + _SPEC_NUMBER_LIMIT)
            try:
                out.append(("%" + flags + (width or "") + ("." + precision if precision is not None else "") + conv) % (value[0],))
            except (ValueError, TypeError):
                out.append(_WILD)
                ok = False
        elif conv == "s" and not flags and not width and precision is None:  # `%.s` has an EMPTY precision, which is zero: it prints nothing (pass-33 A2)
            out.append(value[0])  # an unknown text printed as it is: its wildcard stays in place
            ok = False
        else:
            # `%r` of an unknown text: that text, with anything around it (pass-26 A1); under a precision (`%.0r`) only a prefix of it is written: any text (pass-33 A2)
            out.append(_padded(value[0]) if value is not None and not value[1] and precision is None else _WILD)
            ok = False
    return "".join(out), ok


_LAUNCH_KEYWORDS = ("start-", "start", "saps")
_KEYWORD_GRAMS = frozenset(word[i:i + 2] for word in _LAUNCH_KEYWORDS for i in range(len(word) - 1))  # every run of two characters a launch keyword holds (read by `_unproven`)
_LEFT_OF_LAUNCH_RE = re.compile(r"(?:^|[;&|({=!]|\s\.|(?<![\w$.-])(?:return|throw|exit|-not)\s)\s*\Z", re.IGNORECASE | re.MULTILINE)  # where a command word can stand


def _could_launch(text: str) -> bool:
    """Can this folded text (``_WILD`` = any text) read as a PowerShell launch (``Start-`` / ``start`` / ``saps`` where a command word can stand)
    once its wildcards are filled with something, using characters of BOTH the source and a wildcard? A keyword wholly from the source is the
    plain text's business, and one wholly from a wildcard says nothing about the text around it. Exact: it tries every way the keyword can
    lie across the literal characters and the wildcards, so no bound on the number of wildcards or the length of the gaps between them."""
    if _WILD not in text:
        return False
    items = _pattern_items(text)  # a literal character, or None for a run of wildcards; refused past the cap (r15 A6)
    n = len(items)

    def left_ok(start: int) -> bool:
        prefix = "".join(_WILD if it is None else it for it in items[:start])
        return prefix.rstrip(" \t").endswith(_WILD) or _LEFT_OF_LAUNCH_RE.search(prefix) is not None

    def right_ok(kw: str, i: int) -> bool:
        # r15 (A5): the boundary after the keyword may lie inside the wildcard that ended it (`sa*JUNK` with `ps cmd.exe ` for the wildcard), not only in the next literal
        return kw == "start-" or i >= n or items[i] is None or items[i - 1] is None or not re.match(r"[\w-]", items[i])

    for kw in _LAUNCH_KEYWORDS:
        size = len(kw)

        def go(start: int, i: int, j: int, wild: bool, lit: bool) -> bool:
            if j == size:
                return wild and lit and right_ok(kw, i) and left_ok(start)
            if i >= n:
                return False
            item = items[i]
            if item is None:  # the wildcard supplies 0..(rest of the keyword) characters
                return any(go(start, i + 1, j + take, wild or take > 0, lit) for take in range(size - j + 1))
            return item.lower() == kw[j] and go(start, i + 1, j + 1, wild, True)

        if any(go(a, a, 0, False, False) for a in range(n)):
            return True
    return False


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


def _string_consts(binders: dict[str, list], imported: dict[str, set[str]] | None = None) -> dict[str, str]:
    """Names whose ONLY binding is one plain string literal: ``TEST_MODULE = "pkg.mod"``. An import that binds the name too
    (``from os import __name__ as name``) is a second binding, so the literal is not its value."""
    imported = imported or {}
    return {name: values[0].value for name, values in binders.items()
            if len(values) == 1 and isinstance(values[0], ast.Constant) and isinstance(values[0].value, str) and name not in imported}


class _Facts:
    """Names bound to the tracked modules and to their spawn APIs (direct bindings only; everything else is a reference violation).

    A name bound to two DIFFERENT tracked modules or APIs (``import os as m`` in one function, ``import subprocess as m`` in
    another) is ``ambiguous`` and every use of it is reported: the map is file-wide, not scoped. Resolution is a worklist over
    the plain-name bindings, so a long chain of aliases costs time linear in the source.
    """

    def __init__(self, tree: ast.AST) -> None:
        self.binders, self.imports, self.own, self.import_src = _collect_binders(tree)
        self.consts = _string_consts(self.binders, self.import_src)
        self.modules: dict[str, str] = {}
        self.funcs: dict[str, tuple[str, str]] = {}
        self.ambiguous: set[str] = set()
        self.importers: dict[str, str] = {"__import__": "__import__"}  # names that import a module by string -> which kind
        self.helper_modules: dict[str, str] = {}  # names bound to importlib / builtins
        self.imported: set[str] = set(self.imports)  # names that are an imported module, directly or through a plain alias of one
        # names bound by `import x [as y]` only (never a from-import, which may bind a class or a function), and plain aliases of them:
        # the names whose value IS a module, so a flow into a binding the scan cannot follow is a lost module identity
        self.module_names: set[str] = {n for n, srcs in self.import_src.items() if not any(s.startswith("from ") for s in srcs)}
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

    def _bind_importer(self, name: str, kind: str) -> bool:
        """``load`` bound to ``import_module`` and to ``__import__`` returns different modules for the same call: the name is ambiguous, not whichever came first."""
        if name in self.importers:
            if self.importers[name] != kind:
                self.ambiguous.add(name)
            return False
        self.importers[name] = kind
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
        elif isinstance(node, ast.ImportFrom) and (node.level > 0 or not node.module):  # `from .helpers import *`, `from . import os`
            for a in node.names:
                if a.name == "*":
                    self._bind_module("os", "os")
                    self._bind_module("subprocess", "subprocess")
                elif a.name in _SUBMODULE_REEXPORTS:
                    self._bind_module(a.asname or a.name, _SUBMODULE_REEXPORTS[a.name])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            origin = _MODULE_ALIASES.get(node.module, node.module)
            if node.module in ("importlib", "builtins"):
                for a in node.names:
                    if a.name in _IMPORT_FUNCS:
                        self._bind_importer(a.asname or a.name, a.name)
                    elif a.name == "*":
                        self._bind_importer("import_module", "import_module")
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
                    for api in (_SPAWN_APIS["asyncio.*"] if a.name == "*" else [a.name] if a.name in _SPAWN_APIS["asyncio.*"] else []):
                        self._bind_func(api if a.name == "*" else a.asname or a.name, ("asyncio.*", api))  # asyncio.windows_utils.Popen
            elif origin not in _TRACKED_MODULES:  # `from shutil import os as o`: any module's re-export of os / subprocess is the real one
                for a in node.names:
                    if a.name in _SUBMODULE_REEXPORTS:
                        self._bind_module(a.asname or a.name, _SUBMODULE_REEXPORTS[a.name])
                    elif a.name == "*":  # `from platform import *` brings the module's public `os` and `subprocess` along
                        self._bind_module("os", "os")
                        self._bind_module("subprocess", "subprocess")

    def _learn(self, name: str, value: ast.AST) -> list[str]:
        learned: list[str] = []
        module = self.module_of(value)
        if module is not None:
            if self._bind_module(name, module):
                learned.append(name)
        elif isinstance(value, ast.Name):
            if value.id in self.funcs and self._bind_func(name, self.funcs[value.id]):
                learned.append(name)
            if value.id in self.importers and self._bind_importer(name, self.importers[value.id]):
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
                  and value.attr in _IMPORT_FUNCS and self._bind_importer(name, value.attr)):
                learned.append(name)
        if name not in self.imported and self._imported_root(value):  # `s = shutil`, `p = os.path`: still an imported module under another name
            self.imported.add(name)
            learned.append(name)
        if name not in self.module_names and self._is_module_value(value):  # `s = shutil`, `m = __import__('shutil')`
            self.module_names.add(name)
            learned.append(name)
        return learned

    def _is_module_value(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return isinstance(node.ctx, ast.Load) and node.id in self.module_names
        return isinstance(node, ast.Call) and self.import_target(node)[0]

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

    @staticmethod
    def _fromlist_state(call: ast.Call) -> bool | None:
        """``__import__(name, ..., fromlist)``: True when the fromlist is a non-empty literal (the NAMED submodule is returned), False when
        it is absent or an empty literal (the root is returned), None when it is a run-time value (either can be returned: both the root and
        the submodule identity are kept by the caller, which reports the import). A ``*`` in the arguments, a ``**`` in the keywords, or a
        container made only of starred elements (``[*names]`` expands to anything, empty included) is a run-time value too."""
        if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
            return None  # `__import__(*args)`, `__import__('a.b', **kw)`: the fromlist may arrive through the expansion
        arg = call.args[3] if len(call.args) > 3 else next((kw.value for kw in call.keywords if kw.arg == "fromlist"), None)
        if arg is None or (isinstance(arg, ast.Constant) and not arg.value):
            return False
        if isinstance(arg, (ast.List, ast.Tuple)):
            if any(not isinstance(e, ast.Starred) for e in arg.elts):
                return True  # one plain element makes it nonempty whatever a star adds
            return None if arg.elts else False
        return True if isinstance(arg, ast.Constant) else None

    @staticmethod
    def _absolute_level(call: ast.Call) -> bool:
        """``__import__(name, globals, locals, fromlist, level)``: True only when the level is provably 0 (absent, or the literal 0). A level above
        0 names the module relative to ``globals['__package__']``; a run-time level, or a ``*`` / ``**`` that may carry one, is not provable."""
        if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
            return False
        level = call.args[4] if len(call.args) > 4 else next((kw.value for kw in call.keywords if kw.arg == "level"), None)
        return level is None or (isinstance(level, ast.Constant) and type(level.value) is int and level.value == 0)

    def unresolved_reason(self, call: ast.Call) -> str:
        """Why ``import_target`` could not resolve this dynamic import (its ``resolved`` is False): the module name, the fromlist, or the package."""
        kind = self.import_kind(call)
        arg = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg == "name"), None)
        name = self.literal(arg)
        if name is None:
            return "makes a dynamic import whose module name is not a string literal"
        if kind == "__import__" and "." in name and self._fromlist_state(call) is None:
            return "makes a dynamic import whose fromlist is not statically known, so the returned module cannot be followed"
        if kind == "__import__" and not self._absolute_level(call):
            return "makes a dynamic import with a relative level that is not provably 0, so the module it names cannot be resolved"
        return "makes a dynamic import of a relative module name whose package is not a string literal"

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
        if kind == "__import__" and not self._absolute_level(node):
            return True, None, False  # `__import__('windows_utils', {'__package__': 'asyncio'}, level=1)` is asyncio.windows_utils, not a top-level module
        fromlist = self._fromlist_state(node) if kind == "__import__" else False
        if fromlist is None and "." in name:  # os.path with a fromlist known only at run time is os or os.path: not resolvable, so reported
            return True, None, False
        if kind == "__import__" and not fromlist:  # without a fromlist __import__('os.path') returns os; the root is the safe superset
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

    def _imported_root(self, node: ast.AST) -> bool:
        """Does this attribute chain start at an imported module: a name an ``import`` bound (or a plain alias of one), a tracked module,
        or a literal dynamic import (``importlib.import_module('shutil').os``)?"""
        while isinstance(node, ast.Attribute):
            node = node.value
        if isinstance(node, ast.Call):
            return self.import_target(node)[0]
        return isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and (node.id in self.imported or node.id in self.modules)

    def module_of(self, node: ast.AST) -> str | None:
        """The tracked module this expression IS (a bound name, ``asyncio.subprocess``, a submodule chain, or a literal dynamic import)."""
        if isinstance(node, ast.Name):
            return self.modules.get(node.id) if isinstance(node.ctx, ast.Load) else None
        if isinstance(node, ast.Attribute):
            base = self.module_of(node.value)
            if base in ("asyncio", "asyncio.*"):
                # asyncio.subprocess is asyncio's wrapper; asyncio.base_events.subprocess / .os ARE the real modules; a submodule stays "asyncio.*"
                reexports = _PACKAGE_REEXPORTS if base == "asyncio" else _SUBMODULE_REEXPORTS
                return reexports.get(node.attr) or ("asyncio.*" if node.attr in _ASYNCIO_SUBMODULES else None)
            # `os.path.os`, `shutil.os`, `platform.subprocess`: a module an imported module re-exports under that name is the real one
            if node.attr in _SUBMODULE_REEXPORTS and (base is not None or self._imported_root(node.value)):
                return _SUBMODULE_REEXPORTS[node.attr]
            return None
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


def _imported_module_position_ok(node: ast.AST, up: ast.AST | None, parent: dict[int, ast.AST], facts: _Facts) -> bool:
    """May an imported module (not a tracked one) appear here without being followed? Stricter than a read: the value must not flow into a
    container, conditional, subscript, return, lambda, class attribute or any other binding the scan cannot tie a later call back to."""
    if isinstance(up, ast.Attribute):
        return node is up.value  # shutil.which, shutil.os: the attribute is judged at its own node
    if isinstance(up, (ast.Compare, ast.Expr, ast.UnaryOp, ast.FormattedValue)):
        return True
    if isinstance(up, ast.Assert):
        return node is up.test  # `assert shutil` is read and dropped; `assert x, shutil` carries the module away in AssertionError.args
    if isinstance(up, (ast.If, ast.While, ast.IfExp)) and node is up.test:
        return True
    if isinstance(up, ast.BoolOp) and isinstance(up.op, ast.And) and node is not up.values[-1]:
        return True  # `yaml and yaml.load(x)`: a module is truthy, so the first operand is never the value
    if (isinstance(up, ast.Call) and node is not up.func and isinstance(up.func, ast.Name) and up.func.id in ("getattr", "hasattr", "setattr", "delattr")
            and up.args and up.args[0] is node):
        name = facts.literal(up.args[1]) if len(up.args) > 1 else None  # getattr(shutil, 'which'): the name is fixed and is not a re-export
        return name is not None and name not in _IMPORT_FUNCS and name not in _SUBMODULE_REEXPORTS and not (name.startswith("__") and name.endswith("__"))
    return _position_ok(node, up, None, parent)


def _named_helper_access(node: ast.AST, up: ast.AST | None, facts: _Facts) -> bool:
    """``setattr(builtins, NAME, v)`` / ``getattr(importlib, 'x')``: the import machinery is named, and the name is not an import function."""
    if not (isinstance(up, ast.Call) and isinstance(up.func, ast.Name) and up.func.id in ("setattr", "delattr", "getattr", "hasattr")):
        return False
    if len(up.args) < 2 or up.args[0] is not node:
        return False
    name = facts.literal(up.args[1])
    return name is not None and name not in _IMPORT_FUNCS and not (name.startswith("__") and name.endswith("__"))


def _spawn_like(recv: ast.AST, facts: _Facts, bound: dict[str, list[ast.AST]]) -> bool:
    """r15: is this ``getattr`` receiver a subprocess / asyncio / os-like object? It is when it is made of a name the file binds to a spawn module (``loop = asyncio.get_running_loop()``)."""
    modnames = set(facts.modules)
    todo, seen = [recv], set()
    while todo:
        for n in ast.walk(todo.pop()):
            if isinstance(n, ast.Name) and n.id not in seen:
                seen.add(n.id)
                if n.id in modnames:
                    return True
                todo.extend(bound.get(n.id, ()))
    return False


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
    reflection_aliases = {a.asname: a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names if a.asname and a.name in _REFLECTION}
    binds_spawn = bool(facts.modules or facts.funcs or facts.module_names)  # any imported module may re-export os / subprocess
    bound: dict[str, list[ast.AST]] = {}  # the values the file binds to each name: what a receiver such as `loop = asyncio.get_running_loop()` is made of
    for n in ast.walk(tree):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name):
                    bound.setdefault(t.id, []).append(n.value)
        elif isinstance(n, (ast.AnnAssign, ast.NamedExpr)) and isinstance(n.target, ast.Name) and n.value is not None:
            bound.setdefault(n.target.id, []).append(n.value)
        elif isinstance(n, ast.withitem) and isinstance(n.optional_vars, ast.Name):
            bound.setdefault(n.optional_vars.id, []).append(n.context_expr)
        elif isinstance(n, (ast.For, ast.AsyncFor)) and isinstance(n.target, ast.Name):
            bound.setdefault(n.target.id, []).append(n.iter)
    for node in ast.walk(tree):
        module: str | None = None
        is_ref = False
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]] if node.level == 0 else []
            else:
                roots = [a.name.split(".")[0] for a in node.names]
            # Any import in a class body: the name is a class attribute (`X.shutil.os`), which no later call is tied back to, and any
            # module may re-export os / subprocess. (`roots` still names the spawn/import modules for the message.)
            if _in_class_scope(node, parent):
                what = "a spawn or import module" if any(r in _CLASS_SCOPE_IMPORT_ROOTS for r in roots) else "a module"
                out.append((node.lineno, f"imports {what} in a class body, where the name is a class attribute"))
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                origin = _MODULE_ALIASES.get(node.module or "", node.module or "")
                if origin in _STRICT_MODULES:
                    for a in node.names:
                        if a.name != "*" and not _attr_ok(origin, a.name):
                            out.append((node.lineno, f"imports {a.name} from {origin}, which is not a constant or a data type"))
            continue
        if (isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))
                and _LOOP_METHOD_NAME_RE.fullmatch(node.value if isinstance(node.value, str) else node.value.decode("latin-1"))):  # b'subprocess_exec' too
            out.append((node.lineno, f"names {node.value!r} as a string, which reaches an event-loop spawn method without calling it in place"))
        if isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call, ast.Subscript)) and not (isinstance(node, ast.BinOp) and isinstance(parent.get(id(node)), ast.BinOp) and isinstance(parent[id(node)].op, (ast.Add, ast.Mod, ast.Mult))):  # only a parent that folds its operands covers it
            built, known = _flatten(node, facts.consts)  # 'subprocess_' + 'exec', '%sexec' % 'subprocess_', f'subprocess_{"exec"}': the folded name
            if binds_spawn and _WILD not in built and built in _REFLECTION_STRINGS:
                out.append((node.lineno, f"builds {built!r} as a string, which can reach a spawn API without a followable reference"))
            could = False
            try:
                could = _could_name(built, known, _LOOP_SPAWN_METHODS)  # a wildcard piece can read as the name, so a fold mistake only over-reports
            except _FoldTooBig:  # r15 (A6): a pattern past the cap is not searched; THIS node is reported (a reviewed template is allowlisted in `_PATTERN_CAP_REVIEWED`), the rest of the file is still read
                out.append((node.lineno, _PATTERN_CAP_REASON))
            if could:
                out.append((node.lineno, f"names {built.replace(_WILD, '*')!r} as a built string, which can reach an event-loop spawn method without calling it in place"))
        if binds_spawn and isinstance(node, ast.Call):  # r15 (HUB RULING): a name that is not a constant, in a getattr-name position, is reported; the scan does not try to prove it cannot reach a spawn method
            fn = node.func.id if isinstance(node.func, ast.Name) else node.func.attr if isinstance(node.func, ast.Attribute) else ""
            named = node.args[1:2] if fn in ("getattr", "hasattr") and node.args and _spawn_like(node.args[0], facts, bound) else node.args if fn in ("attrgetter", "methodcaller") else []
            for arg in named:
                if not isinstance(arg, ast.Starred) and _WILD in _flatten(arg, facts.consts)[0]:
                    out.append((node.lineno, _GETATTR_REASON))
                    break
        if binds_spawn and isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in _REFLECTION_STRINGS:
            out.append((node.lineno, f"names {node.value!r} as a string, which can reach a spawn API without a followable reference"))
        if (isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__") and node.attr not in _DUNDER_OK
                and ((isinstance(node.value, ast.Name) and node.value.id in facts.module_names)
                     or (isinstance(node.value, ast.Call) and facts.import_target(node.value)[0]))):
            out.append((node.lineno, f"reads {node.attr} off an imported module, which reaches its contents without a followable reference"))
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)) and facts.module_of(node.value) is not None:
            out.append((node.lineno, f"assigns or deletes the attribute {node.attr} on a spawn module"))  # CREATE_NO_WINDOW, os.name, run ...
        if isinstance(node, ast.Attribute) and node.attr in _LOOP_SPAWN_METHODS:
            up = parent.get(id(node))
            if not (isinstance(up, ast.Call) and up.func is node):  # an alias of loop.subprocess_exec is not followed
                out.append((node.lineno, f"uses {node.attr} other than by calling it in place"))
        if isinstance(node, ast.Call):
            is_import, module, resolved = facts.import_target(node)
            if is_import and not resolved:
                out.append((node.lineno, facts.unresolved_reason(node)))
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
        if not is_ref and (
                (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id in facts.module_names and node.id not in facts.modules)
                or (isinstance(node, ast.Call) and facts.import_target(node)[0])):
            if not _imported_module_position_ok(node, up, parent, facts):  # `s, = (shutil,)`, `s = [shutil][0]`, `class X: m = shutil`
                out.append((node.lineno, "uses an imported module where its calls cannot be followed"))
        # importlib / builtins: `importlib.import_module` is judged at its own node, but the module itself in a container or call
        # argument hides the import function from the scan (`loaders = [importlib]; loaders[0].import_module('subprocess')`).
        helper_receiver = (isinstance(node, ast.Name) and module is None and node.id in facts.helper_modules
                           and ((isinstance(up, ast.Attribute) and node is up.value) or _named_helper_access(node, up, facts)))
        if is_ref and not helper_receiver and not _position_ok(node, up, module, parent):
            out.append((node.lineno, "uses a spawn module or API where its calls cannot be followed"))
        if binds_spawn:
            name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None
            # A bare name is reported only for the names that are never an ordinary variable (or when it was imported: `from inspect
            # import signature`); an attribute (`inspect.signature`, `f.__defaults__`, `p.annotation`) is reported whatever it hangs off.
            local = name
            if isinstance(node, ast.Name):
                name = reflection_aliases.get(name, name)  # `from typing import get_type_hints as hints`: hints is get_type_hints
            named = isinstance(node, ast.Attribute) or name in _REFLECTION_BARE or local in facts.imports
            if name in _REFLECTION and named and (not isinstance(node, ast.Name) or isinstance(node.ctx, ast.Load)):
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


def _reported_when_too_deep(on_deep):
    """A scan that runs out of stack on an expression nested deeper than the fold reads (``'x'`` followed by a thousand ``.lower()``) REPORTS the source
    (``on_deep``) instead of crashing: a source the scan cannot read is never a clean one (r9 pass-24 A4, r10 pass-27 A2)."""
    def wrap(scan):
        @functools.wraps(scan)
        def guarded(*args, **kwargs):
            try:
                return scan(*args, **kwargs)
            except RecursionError:
                return on_deep(*args, **kwargs)
        return guarded
    return wrap


@_reported_when_too_deep(lambda source, label: [f"{label}:1 holds an expression nested too deep for the scan to read, so it is REPORTED"])
def _unflagged_spawns_in_source(source: str, label: str) -> list[str]:
    tree = ast.parse(source, filename=label)
    _note_aliases(tree)
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


# --- PowerShell fixtures: any Start-Process / saps / start launch, whatever its target, must be hidden; judged per statement ---

_WINDOW_STYLE_HIDDEN_RE = re.compile(r"(?<!\S)-WindowStyle[\s:]+(?:'Hidden'|\"Hidden\"|Hidden)(?=[\s;)|}&]|\Z)", re.IGNORECASE)  # the value ends at Hidden: `Hidden,Minimized` is another value (a line continuation after it is a space, see _ps_statements)
_REDIRECT_TARGET_BEFORE_RE = re.compile(r">[ \t]*\Z")  # matched against the statement up to a word: the previous thing is a redirection operator (`>`, `>>`, `2>`, `*>`), so the word is its target
_START_PROCESS_RE = re.compile(r"(?<![\w$.-])(Start-Process|saps|start)(?![\w-])", re.IGNORECASE)  # the cmdlet and its aliases
_PS_COMMAND_PREFIX_RE = re.compile(r"(?<![\w$.-])(?:return|throw|exit|-not)$", re.IGNORECASE)  # a word that ends right before a command name
# PowerShell reads these typographic characters as the ASCII ones: single and double quotes, the three dashes a parameter may start with, no-break space.
_PS_CHAR_MAP = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"',
                              "–": "-", "—": "-", "―": "-", " ": " ", "\u0085": " "})


def _ps_text(text: str) -> str:
    """The text as PowerShell's tokenizer reads its characters: typographic quotes and dashes are ASCII, and a line ends at CR, LF or CRLF alike (pass-28 A2:
    a comment ends at a bare CR, which the readers below, that look for LF, did not know). Every reader of a script starts from this."""
    return re.sub(r"\r\n?", "\n", text.translate(_PS_CHAR_MAP))


def _ps_comment_at(text: str, i: int) -> bool:
    """Does a PowerShell ``#`` comment start at ``text[i]``? It does wherever a new token can begin: after white space, a quote that closes a string, a
    bracket, a separator or an operator (``'/c'# -WindowStyle Hidden``, ``(1)# note``), and not inside a bare word (``a#b``). The one predicate the three
    readers share (pass-25 A3): a comment kept as text could supply a ``-WindowStyle Hidden`` the launch does not carry."""
    return text[i] == "#" and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] in "_-.:/\\$\x02<"))  # \x02: a glued or escaped character, part of the same word; `<`: a `<#` that opened no block comment is a bare word (r15 A4)


_PS_BLOCK_COMMENT_AFTER = frozenset(";|&{}(),'\"")  # besides white space, what always ends a token before a `<#` that opens a block comment (measured against Parser.ParseInput, r15 A4)
_COMMENT_LOOSE = [False]  # set while a script is read the second way: every `<#` is a comment (`_judge_both`)


def _ps_block_comment_at(text: str, i: int) -> bool:
    """Does a PowerShell ``<#`` block comment start at ``text[i]``? Where a token can begin (the start of the text, after white space, a separator, a bracket, a comma or a quote
    that closes a string): ``a<#`` is a bare word and the launch after it is code (r15 A4), but ``$a[0]<#`` is a comment, so a character the parser reads either way is judged both."""
    return text.startswith("<#", i) and (_COMMENT_LOOSE[0] or i == 0 or text[i - 1].isspace() or text[i - 1] in _PS_BLOCK_COMMENT_AFTER)


def _judge_both(script: str) -> tuple[int, int]:
    """``_judge_script`` read with a ``<#`` that follows a non-separator as a bare word, and again as a comment: the worse reading counts (r15 A2; no per-character table of the parser's modes)."""
    strict = _judge_script(script)
    _COMMENT_LOOSE[0] = True
    try:
        loose = _judge_script(script)
    finally:
        _COMMENT_LOOSE[0] = False
    return max(strict[0], loose[0]), max(strict[1], loose[1])


_HERE_OPEN_RE = re.compile(r"@(['\"])[ \t]*\n")  # the opening of a here-string: `@"` or `@'` and the end of the line


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    """(start, end) of every PowerShell quoted string, both quotes included. A doubled quote escapes itself."""
    spans, i, n = [], 0, len(text)
    while i < n:
        quote = text[i]
        here = _HERE_OPEN_RE.match(text, i) if quote == "@" else None
        if here:  # r15 (A1): a here-string holds quotes and is closed only by a quote and `@` at the start of a line
            close = text.find("\n" + here.group(1) + "@", here.end() - 1)
            end = n - 1 if close == -1 else close + 2
            spans.append((i, end))
            i = end + 1
            continue
        if _ps_block_comment_at(text, i):  # a comment may hold an apostrophe; it must not open a string
            end = text.find("#>", i + 2)
            i = n if end == -1 else end + 2
            continue
        if _ps_comment_at(text, i):
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


def _comment_continuations_dropped(script: str) -> str:
    """A backtick at the end of a ``#`` comment line is comment text, not a line continuation: the next line is a new statement (pass-24 A3)."""
    spans = dict(_quoted_spans(script))
    out, i, n = [], 0, len(script)
    while i < n:
        if i in spans:
            out.append(script[i:spans[i] + 1])
            i = spans[i] + 1
        elif _ps_block_comment_at(script, i):
            end = script.find("#>", i + 2)
            end = n if end == -1 else end + 2
            out.append(script[i:end])
            i = end
        elif _ps_comment_at(script, i):
            end = script.find("\n", i)
            end = n if end == -1 else end
            line = script[i:end]
            body = line.rstrip("\r")
            out.append(body[:-1] + line[len(body):] if body.endswith("`") else line)  # the trailing backtick goes; the newline stays a statement break
            i = end
        else:
            out.append(script[i])
            i += 1
    return "".join(out)


def _ps_statements(script: str) -> list[str]:
    """Split a PowerShell script into statements: on ; | { } and newlines outside quotes, comments dropped, backtick-newline joined."""
    script = _comment_continuations_dropped(script)
    # mid-word: `foo`<LF>-WindowStyle:Hidden` is ONE argument, not a parameter. A continuation whose next line starts with white space is a separator
    # (`-WindowStyle Hidden`<LF> -Wait` ends the value at Hidden, G18), so only a continuation that glues two non-space characters is marked.
    script = re.sub(r"(?<=\S)`\r?\n(?=\S)", "\x02", script)
    script = re.sub(r"`\r?\n", " ", script)  # after whitespace: a line continuation
    spans = dict(_quoted_spans(script))
    out, cur, i, n = [], [], 0, len(script)
    while i < n:
        if i in spans:
            cur.append(script[i:spans[i] + 1])
            i = spans[i] + 1
            continue
        ch = script[i]
        if _ps_block_comment_at(script, i):
            end = script.find("#>", i + 2)
            i = n if end == -1 else end + 2
            continue
        if _ps_comment_at(script, i):
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


_PS_ESCAPE_LETTERS = frozenset("0abfnrtuv")  # in double quotes `0 `a `b `f `n `r `t `u `v are real escapes; elsewhere the backtick is just dropped
_PS_DQ_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", "0": "\0", "a": "\a", "b": "\b", "f": "\f", "v": "\v"}


def _unescape_ps(script: str) -> str:
    """Drop the backtick PowerShell's parser drops, so ``Start-Proce`ss`` reads as ``Start-Process``.

    A backtick before an ordinary character is removed outside quotes and inside double quotes; single-quoted text keeps it
    (it is a plain character there); a double-quote escape letter (`n, `t ...) stays so a string boundary survives; a
    backtick before a quote character or a newline (a continuation) is left for the statement splitter.
    """
    if "`" not in script:
        return script
    quote = [""] * len(script)
    for s, e in _quoted_spans(script):
        for k in range(s, e + 1):
            quote[k] = script[s + 1] if script[s] == "@" else script[s]  # a here-string takes its escapes from the quote after the `@`
    out, i, n = [], 0, len(script)
    while i < n:
        ch = script[i]
        if ch == "`" and i + 1 < n and quote[i] != "'" and script[i + 1] not in "\r\n\"'":
            nxt = script[i + 1]
            if nxt == "-" and (i == 0 or script[i - 1].isspace() or script[i - 1] in "(;|{"):
                out.append(ch)  # `-WindowStyle at the start of a word is argument TEXT, not a parameter: keep the backtick that says so
                out.append(nxt)
                i += 2
                continue
            if quote[i] == "" and nxt in " \t":
                out.append("\x02")  # outside quotes an escaped space is part of the SAME word (`foo` -WindowStyle` Hidden` is one argument): never a boundary
                i += 2
                continue
            if quote[i] == "" and nxt in "(){}":
                out.append("\x02")  # an escaped bracket is a plain character of its word: unescaped it would open or close a group and move a -WindowStyle to another command (pass-29 A4)
                i += 2
                continue
            if quote[i] == "" and (nxt == "#" or (nxt == "<" and script.startswith("#", i + 2))):
                out.append("\x02")  # an escaped `#` (or the `<` of an escaped `<#`) is a plain character of its word: unescaped it would open a comment that eats the rest (pass-26 A2)
                i += 2
                continue
            if nxt == "`":
                out.append("\x02")  # an escaped backtick is a plain character of its word: left as one backtick it would join with a newline after it into a line continuation (pass-28 A3)
                i += 2
                continue
            if quote[i] == '"' and nxt in _PS_ESCAPE_LETTERS:
                out.append(ch)
            out.append(nxt)
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _dq_value(text: str) -> str:
    """The run-time value of the inside of a double-quoted string, as far as backtick escapes go: the text a nested script would see."""
    out, i, n = [], 0, len(text)
    while i < n:
        if text[i] == "`" and i + 1 < n:
            out.append(_PS_DQ_ESCAPES.get(text[i + 1], text[i + 1]))
            i += 2
            continue
        out.append(text[i])
        i += 1
    return "".join(out)


def _at_command_position(before: str) -> bool:
    """May a command word stand right after ``before`` (the statement text up to it)? A call operator ``&`` or a dot-source ``.`` may sit between."""
    b = before.rstrip()
    while b and b[-1] in "&." and (b[-1] == "&" or len(b) == 1 or b[-2].isspace() or b[-2] in "=({"):
        b = b[:-1].rstrip()
    # `return saps x`, `throw start x`, `exit saps`, `-not saps x`, `! saps x`: the keyword or operator leaves the next word in command position
    return b == "" or b[-1] in "=(!" or bool(_PS_COMMAND_PREFIX_RE.search(b))


_SPLICED_WORD_RE = re.compile(r"(?<![\w$.'\"-])[\w'\"-]*['\"][\w'\"-]*(?![\w$.'\"-])")


def _unsplice(script: str) -> str:
    """``Start-"Process"`` and ``Sta'rt'-Process`` are the command ``Start-Process`` to PowerShell: take the quotes out of a word that, bare, IS a launch
    word. A word that is one quoted string (``'Start-Process'``) is text, not a splice, and stays."""
    def bare(m: re.Match) -> str:
        word = m.group(0)
        plain = re.sub(r"['\"]", "", word)
        whole = len(word) >= 2 and word[0] in "'\"" and word[-1] == word[0] and word.count(word[0]) == 2
        balanced = word.count("'") % 2 == 0 and word.count('"') % 2 == 0  # `'Start-Process` opens a quote that closes in the next word: not a splice
        return plain if balanced and not whole and plain.lower() in ("start-process", "saps", "start") else word
    return _SPLICED_WORD_RE.sub(bare, script)


_QUOTED_LAUNCH_RE = re.compile(r"'\s*(start-process|saps|start)\s*'|\"\s*(start-process|saps|start)\s*\"", re.IGNORECASE)


_NESTED_SCRIPT_PARAM_RE = re.compile(r"(?<!\S)-(?:c|a)\w*(?=[\s:]|$)", re.IGNORECASE)  # -Command / -c / -ArgumentList / -args: a value that PowerShell parses as a script


def _might_launch(text: str, nested_value: bool = True) -> bool:
    """The cheap filter in front of ``_judge_script``, read the way ``_judge_script`` reads its input: typographic characters mapped, backticks dropped
    (and the double-quote reading of the text), quote splices taken out, THEN the launch words looked for. A quoted value that is NOT handed to a
    command parameter that parses it (``Write-Host 'Start-Proce`ss'``) is text, so its backtick stays a plain character there (``nested_value`` False)."""
    if not nested_value:
        return bool(_START_PROCESS_RE.search(text))
    text = _ps_text(text)
    readings = (text, _unescape_ps(text), _dq_value(text)) if "`" in text else (text,)
    return any(_START_PROCESS_RE.search(r) or _QUOTED_LAUNCH_RE.search(r) for r in (*readings, *(_unsplice(r) for r in readings)))


def _judge_script(script: str) -> tuple[int, int]:
    """(Start-Process / saps / start statements, those lacking their own outer -WindowStyle Hidden). Quoted text is a nested script.

    The parameter must be a token of the Start-Process command itself: outside quotes, at the same parenthesis depth, and
    starting a word (``foo-WindowStyle:Hidden`` and ``(Get-ChildArgs -WindowStyle Hidden)`` do not count).
    """
    found = bad = 0
    for statement in _ps_statements(_unsplice(_unescape_ps(_ps_text(script)))):
        spans = _quoted_spans(statement)
        quoted = [False] * len(statement)
        for s, e in spans:
            for k in range(s, e + 1):
                quoted[k] = True
        # The id of the innermost parenthesized group each character sits in. A depth number would let `(Start-Process a), (Start-Process b
        # -WindowStyle Hidden)` give the first launch the second one's parameter: two sibling groups share a depth but not a group.
        open_groups, next_group, depths = [], 0, [0] * len(statement)
        for k, ch in enumerate(statement):
            if not quoted[k]:
                if ch == "(":
                    next_group += 1
                    open_groups.append(next_group)
                elif ch == ")" and open_groups:
                    open_groups.pop()
            depths[k] = open_groups[-1] if open_groups else 0
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
            # `> -WindowStyle Hidden` writes the output to a file NAMED `-WindowStyle`: a word right after a redirection operator is its target, not a parameter (pass-32 A1)
            if hole or not any(not quoted[h.start()] and depths[h.start()] == depths[m.start()] and not _REDIRECT_TARGET_BEFORE_RE.search(statement, 0, h.start())
                               for h in _WINDOW_STYLE_HIDDEN_RE.finditer(statement, m.start())):
                bad += 1
        for s, e in spans:
            inner = statement[s + 1:e]
            if statement[s] == '"':
                inner = _dq_value(inner)  # what a nested script made of this string would read
            # A nested script is judged by _judge_script, which unescapes and unsplices it; a filter on the RAW inner would miss Sta"rt"-Proce`ss (pass-22 A2)
            if (s, e) not in command_names and _might_launch(inner, bool(_NESTED_SCRIPT_PARAM_RE.search(statement[:s]))):
                inner_found, inner_bad = _judge_script(inner)
                found += inner_found
                bad += inner_bad
    return found, bad


# Methods that build a string out of their ARGUMENTS as well as their receiver: they count whatever the receiver is.
_ARGUMENT_FOLDS = frozenset({"format", "format_map", "join", "replace", "__mod__"})


def _text_base(node: ast.AST, consts: dict[str, str]) -> bool:
    """True when the chain of method calls and subscripts under ``node`` ends in a text ``_flatten`` reads (a string literal, a built string,
    a name bound once to a string).

    Memoised per scan (pass-28 A4): ``_bound_form`` asks ``_text_base`` of the receiver and this loop then walks the same receiver again, so an
    unmemoised chain of n calls cost 2**n. The entry keeps the node alive, so its ``id`` cannot be reused by a node ``_bound_form`` builds later."""
    key = (id(node), id(consts))
    hit = _TEXT_BASE_MEMO.get(key)
    if hit is not None:
        return hit[2]
    start = node
    while True:
        node = _bound_form(node, consts)
        if isinstance(node, ast.Subscript):
            node = node.value
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            node = node.func.value
        else:
            break
    result = (isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))) or isinstance(node, (ast.JoinedStr, ast.BinOp)) or (
        isinstance(node, ast.Name) and node.id in consts)
    _TEXT_BASE_MEMO[key] = (start, consts, result)
    return result


def _folds(node: ast.AST, consts: dict[str, str]) -> bool:
    """True when ``_flatten`` reads this node's text through a fold (the PowerShell side asks exactly this of every node, so it judges everything the
    Python side reads: a slice, a case or strip mapper, ``chr``, ``decode``, and a string method the fold has no arm for, by way of ``_hedge``)."""
    node = _bound_form(node, consts)  # ``str.m(s, ...)`` / ``operator.concat(a, b)`` are read as ``s.m(...)`` / ``a + b``
    if isinstance(node, (ast.JoinedStr, ast.BinOp)):
        return True
    if isinstance(node, ast.Subscript):
        return _text_base(node.value, consts)
    if isinstance(node, ast.Call):
        if any(isinstance(a, ast.Constant) and isinstance(a.value, bytes) for a in node.args):
            return True  # r15 (A3): a callee that is not PROVEN to be str / bytes still gets a bytes literal read as the text it spells (`T.str(b'Start-Process cmd.exe', 'ascii')`)
        if isinstance(node.func, ast.Attribute):
            return node.func.attr in _ARGUMENT_FOLDS or _text_base(node.func.value, consts)
        return isinstance(node.func, ast.Name) and node.func.id == "chr"
    return False
# A literal piece of a run-time built string that reads as a launch: a ``Start-`` prefix, or ``start`` / ``saps`` where a command word can
# stand (the piece's start, or after ; & | ( { = or a dot-source), so 'the start of {}' is prose and 'start {}' is the alias.
_BUILT_LAUNCH_RE = re.compile(r"start-|(?:^|[;&|({=!]|\s\.)\s*(?:start|saps)(?![\w-])|(?<![\w$.-])(?:return|throw|exit|-not)\s+(?:start|saps)(?![\w-])",
                              re.IGNORECASE | re.MULTILINE)


def _string_values(tree: ast.AST) -> list[tuple[int, str, bool]]:
    """(line, text, built at run time) for every maximal string expression.

    A literal, an implicit concatenation and a ``+`` chain of literals have a known text. Anything else that builds a string
    (an f-string, ``%``, ``.format``, ``.join``) cannot be read statically; if a literal piece of it says ``Start-`` it is
    reported as run-time built (fail closed) instead of being guessed at.
    """
    out: list[tuple[int, str, bool]] = []
    collected = _collect_binders(tree)
    named_strings = _string_consts(collected[0], collected[3])

    def literal_text(node: ast.AST) -> str | None:
        """The text of a ``+`` tree whose every leaf is a string literal, in either nesting (``'a' + ('b' + 'c')`` too); else None."""
        texts: list[str] = []
        todo = [node]  # iterative: a long chain must not exhaust the stack
        while todo:
            cur = todo.pop()
            if isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Add):
                todo.append(cur.right)
                todo.append(cur.left)  # popped first, so leaves come out left to right
            elif isinstance(cur, ast.Constant) and isinstance(cur.value, str):
                texts.append(cur.value)
            else:
                return None
        return _joined(texts)  # a chain of literals is as long as the source it was read from, but the cap holds for it too (pass-30 A2)

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
                marked = "".join(v.value if isinstance(v, ast.Constant) else "\x01" for v in node.values)
                flat, _ = _flatten_launch(node, named_strings)  # the literal pieces with a wildcard per hole: f'Sta{name}rt-Process cmd.exe'
                plain = flat.replace(_WILD, "")  # the holes dropped: f'Sta{name}rt-Process cmd.exe'
                built = (len(_START_PROCESS_RE.findall(plain)) > len(_START_PROCESS_RE.findall(marked)) and _BUILT_LAUNCH_RE.search(plain)) or _could_launch(flat)
                out.append((node.lineno, plain, True) if built else (node.lineno, marked, False))  # ...or filled with pieces of a launch (`f'{115:c}aps cmd.exe'`)
                continue
                out.append((node.lineno, marked, False))
                continue
        if _folds(node, named_strings):
            consts = sorted((c for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)),
                            key=lambda c: (c.lineno, c.col_offset))
            pieces = [c.value for c in consts]
            # a launch split across the literal pieces ('Sta' + name + 'rt-Process') reads as one only when they are joined in source order
            flat, flat_known = _flatten_launch(node, named_strings)  # '{}rt-Process'.format('Sta'), '%srt-Process' % 'Sta': the template filled in
            if flat_known and not isinstance(node, (ast.JoinedStr, ast.BinOp)) and not (
                    isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _ARGUMENT_FOLDS):
                out.append((node.lineno, flat, False))  # a slice / strip / case mapper / chr / decode that folds EXACTLY: its text is judged as written
                continue
            pieces_joined = _joined(pieces)
            if any(_BUILT_LAUNCH_RE.search(p) for p in pieces + [pieces_joined, flat.replace(_WILD, "")]) or _could_launch(flat):
                out.append((node.lineno, pieces_joined, True))
                continue
        stack.extend(ast.iter_child_nodes(node))
    return out


@_reported_when_too_deep(lambda py_source: (1, [1]))
def _start_process_violations(py_source: str) -> tuple[int, list[int]]:
    """(number of PowerShell ``Start-Process`` / ``saps`` / ``start`` launch statements, whatever each one starts, python lines of those lacking an outer -WindowStyle Hidden).

    Reads the module's string expressions (so a comment never counts; implicit concatenation and ``+`` of literals are joined),
    splits each into PowerShell statements, and judges every statement holding a ``Start-Process`` / ``saps`` / ``start`` launch, whatever
    its target (powershell, pwsh, cmd.exe, a variable ``-FilePath``), on its own text. Quoted text is judged as a nested script, so ``-WindowStyle Hidden`` inside an -ArgumentList value does not satisfy
    the outer statement; a quoted value (``-WindowStyle 'Hidden'``) does. A string built at run time out of a ``Start-`` piece
    (f-string, ``%``, ``.format``, ``.join``) is REPORTED: its text is not known, so it cannot be proven hidden.
    """
    found, bad = 0, []
    tree = ast.parse(py_source)
    _note_aliases(tree)
    for lineno, text, built in _string_values(tree):
        if built:
            found += 1
            bad.append(lineno)
            continue
        text = _ps_text(text)  # typographic quotes and dashes are the ASCII ones to PowerShell
        # a backtick-escaped name (bareword) or a `n boundary (double quotes) must not hide the launch from the filter; every reading is unspliced AFTER it is
        # unescaped (the order _judge_script uses), because a backtick is not in a spliced word's class: St`art-Pro'cess' is Start-Process (G17)
        if not _might_launch(text):
            continue
        text = _unsplice(text)  # `Start-"Process"` is Start-Process
        text_found, text_bad = _judge_both(text)
        found += text_found
        bad.extend([lineno] * text_bad)
    return found, bad


def _unflagged_spawns(rel: str) -> list[str]:
    bad = _unflagged_spawns_in_source((ROOT / rel).read_text(encoding="utf-8"), rel)
    return [b for b in bad if not ((rel in _PATTERN_CAP_REVIEWED and b.endswith(_PATTERN_CAP_REASON)) or (rel in _GETATTR_REVIEWED and b.endswith(_GETATTR_REASON)))]  # reviewed sites; every other finding stays


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
        self.assertEqual(bad, [], "Start-Process / saps / start launch (whatever its target) without -WindowStyle Hidden at python line(s): " + ", ".join(map(str, bad)))


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
        self.assertEqual(self._bad("class X:\n    def m(self):\n        import subprocess\n        subprocess.run(['x'], creationflags=_NO_WINDOW)\n"), [])

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
                       "pwsh", "cmd.exe", "-FilePath", "Invoke-Expression", "Invoke-Item", "cmd /c start", "dot-sourced", ":=",
                       # r6: fromlist known only at run time, folded names, module flows
                       "fromlist", "list of only starred", ".format", "class attribute", "from concurrent import futures"):
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


class AdversaryR5Pass10Tests(unittest.TestCase):
    """r5 adversary pass 10 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r5-2bda2c09), findings A1-A4 and their siblings, as classes."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    @staticmethod
    def _judge(ps: str) -> tuple[int, int]:
        found, bad = _start_process_violations("x = " + repr(ps) + "\n")
        return found, len(bad)

    # A1: `os.path.os`, `shutil.os`, `platform.subprocess` ... are the real modules; a re-exported spawn module is followed off any import.
    def test_a1_a_spawn_module_re_exported_by_another_module_is_followed(self):
        for label, src in {
            "os.path.os": "import os\nos.path.os.system('cmd.exe /c exit')\n",
            "os.path.os alias": "import os\nm = os.path.os\nm.system('x')\n",
            "shutil.os": "import shutil\nshutil.os.system('x')\n",
            "pathlib.os": "import pathlib\npathlib.os.popen('x')\n",
            "tempfile._os": "import tempfile\ntempfile._os.system('x')\n",
            "platform.subprocess": "import platform\nplatform.subprocess.run(['cmd.exe'])\n",
            "from import": "from shutil import os as o\no.system('x')\n",
            "os.path.subprocess": "import os\nos.path.subprocess.Popen(['x'])\n",
            "nested package": "import concurrent.futures\nconcurrent.futures.os.system('x')\n",
            "posixpath.os": "import posixpath\nposixpath.os.system('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("import platform\nplatform.subprocess.run(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("import os, shutil\nx = os.path.join('a', 'b')\ny = shutil.which('z')\nself.os = 1\nargs.subprocess = 2\n"), [])

    # A2: a spawn API held only in an annotation or a default can be read back by inspection.
    def test_a2_signature_and_default_extraction_is_reported(self):
        for label, src in {
            "signature annotation": "import inspect, subprocess\ndef launch(arg: subprocess.run): pass\n"
                                    "r = inspect.signature(launch).parameters['arg'].annotation\nr(['cmd.exe'])\n",
            "return annotation": "import inspect, subprocess\ndef launch() -> subprocess.Popen: pass\n"
                                 "r = inspect.signature(launch).return_annotation\nr(['cmd.exe'])\n",
            "from import signature": "from inspect import signature\nimport subprocess\ndef launch(a: subprocess.run): pass\nsignature(launch)\n",
            "getfullargspec": "import inspect, subprocess\ndef launch(a: subprocess.run): pass\ninspect.getfullargspec(launch).annotations['a']\n",
            "__defaults__": "import subprocess\ndef launch(a=subprocess.run): pass\nlaunch.__defaults__[0](['x'])\n",
            "__kwdefaults__": "import subprocess\ndef launch(*, a=subprocess.run): pass\nlaunch.__kwdefaults__['a'](['x'])\n",
            "__globals__": "import subprocess\ndef launch(): pass\nlaunch.__globals__['subprocess'].run(['x'])\n",
            "__signature__": "import subprocess\ndef launch(a: subprocess.run): pass\nlaunch.__signature__\n",
            "frame globals": "import subprocess, sys\nsys._getframe().f_globals['subprocess'].run(['x'])\n",
            "closure cell": "import subprocess\ndef outer():\n    def inner():\n        return 1\n    return inner\nouter().__closure__\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # r6 (pass 13 A2): any imported module may re-export os, so reflection is reported in a file that imports one; a file that imports nothing has nothing to reach
        self.assertGreaterEqual(len(self._bad("import inspect\ndef launch(a: int): pass\nr = inspect.signature(launch)\n")), 1)
        self.assertEqual(self._bad("def launch(a: int): pass\nr = launch.__annotations__\nsignature = 1\n"), [])

    # A3: spawn classes re-exported by asyncio submodules (asyncio.windows_utils.Popen forwards to subprocess.Popen) need creationflags.
    def test_a3_asyncio_submodule_spawn_classes_need_an_accepted_flag(self):
        for label, src in {
            "from import": "from asyncio.windows_utils import Popen\nPopen(['cmd.exe', '/c', 'exit'])\n",
            "from import as": "from asyncio.windows_utils import Popen as P\nP(['x'])\n",
            "attribute": "import asyncio\nasyncio.windows_utils.Popen(['x'])\n",
            "import as": "import asyncio.windows_utils as wu\nwu.Popen(['x'])\n",
            "from asyncio import": "from asyncio import windows_utils\nwindows_utils.Popen(['x'])\n",
            "dynamic": "import importlib\nimportlib.import_module('asyncio.windows_utils').Popen(['x'])\n",
            "star": "from asyncio.windows_utils import *\nPopen(['x'])\n",
            "alias": "import asyncio\nP = asyncio.windows_utils.Popen\nP(['x'])\n",
            "transport": "from asyncio.base_subprocess import BaseSubprocessTransport\nBaseSubprocessTransport(loop, proto, ['x'], False, None, None, None, 0)\n",
            "loop private": "def f(loop):\n    loop._make_subprocess_transport(proto, ['x'], False, None, None, None, 0)\n",
        }.items():
            self.assertEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("from asyncio.windows_utils import Popen\nPopen(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("from asyncio.windows_utils import PipeHandle\nPipeHandle(1)\n"), [])

    # A4: a PowerShell backtick before an ordinary character is dropped by the parser, so it cannot hide the command name.
    def test_a4_backtick_escaped_command_names_are_judged(self):
        for ps in ("Start-Proce`ss cmd.exe", "sa`ps cmd.exe", "`Start-Process cmd.exe", "Sta`rt-Pro`cess cmd.exe", "$p = Start-Proce`ss cmd.exe",
                   "& \"Start-Proce`ss\" cmd.exe", "& 'Start-Process' cmd.exe", "star`t cmd.exe", "Start-Process cmd.exe -Window`Style Normal"):
            self.assertEqual(self._judge(ps), (1, 1), ps)
        for ps in ("Start-Proce`ss cmd.exe -WindowStyle Hidden", "Start-Process cmd.exe -Window`Style Hi`dden", "sa`ps cmd.exe -WindowStyle Hidden"):
            self.assertEqual(self._judge(ps), (1, 0), ps)
        # inside double quotes `n and `t are real escapes, so they keep a boundary; in single quotes a backtick is a plain character
        self.assertEqual(self._judge("Write-Host \"a`nStart-Process cmd.exe\""), (1, 1))
        self.assertEqual(self._judge("Write-Host 'Start-Proce`ss'"), (0, 0))
        self.assertEqual(self._judge("Write-Host 'a`b'; Get-Date"), (0, 0))
        self.assertEqual(_start_process_violations("x = 'Start-Proce`' + 'ss cmd.exe'\n"), (1, [1]))

    # Pass 11 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r5-ce82dba3), findings A1-A4.
    def test_pass11_a1_dunder_import_with_a_fromlist_returns_the_submodule(self):
        for label, src in {
            "fromlist keyword": "wu = __import__('asyncio.windows_utils', fromlist=['Popen'])\nwu.Popen(['cmd.exe', '/c', 'exit'])\n",
            "fromlist positional": "wu = __import__('asyncio.windows_utils', globals(), locals(), ['Popen'])\nwu.Popen(['x'])\n",
            "fromlist star": "import builtins\nwu = builtins.__import__('asyncio.base_events', fromlist=['*'])\nwu.subprocess.run(['x'])\n",
            "fromlist unknown": "wu = __import__('asyncio.windows_utils', fromlist=names)\nwu.Popen(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("wu = __import__('asyncio.windows_utils', fromlist=['Popen'])\nwu.Popen(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertGreaterEqual(len(self._bad("m = __import__('asyncio.windows_utils')\nm.windows_utils.Popen(['x'])\n")), 1)  # no fromlist: the root

    def test_pass11_a2_a_private_loop_spawn_method_reached_by_name_is_reported(self):
        for label, src in {
            "getattr": "import asyncio\nasync def main():\n    loop = asyncio.get_running_loop()\n"
                       "    await getattr(loop, '_make_subprocess_transport')(asyncio.SubprocessProtocol(), ['cmd.exe'], False, None, None, None, 0)\n",
            "attrgetter": "import operator\ndef f(loop):\n    operator.attrgetter('_make_subprocess_transport')(loop)\n",
            "dotted": "import operator\ndef f(a):\n    operator.attrgetter('loop._make_subprocess_transport')(a)\n",
            "alias": "def f(loop):\n    t = loop._make_subprocess_transport\n    t(proto, ['x'], False, None, None, None, 0)\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        for name in sorted(_LOOP_SPAWN_METHODS):  # every method the direct-call rule tracks is also reported when named as a string
            self.assertGreaterEqual(len(self._bad(f"def f(loop):\n    getattr(loop, {name!r})\n")), 1, name)

    def test_pass11_a3_a_launch_split_over_a_nested_concatenation_is_read(self):
        for src in ("x = 'Sta' + ('rt-Pro' + 'cess cmd.exe')\n", "x = ('Sta' + 'rt-') + ('Pro' + 'cess cmd.exe')\n",
                    "x = 'a; ' + ('Start-' + ('Process ' + 'cmd.exe'))\n", "x = 'Sta' + name + 'rt-Process cmd.exe'\n",
                    "x = 'Sta' + (name + 'rt-Process cmd.exe')\n", "x = 'sta' + name + 'rt cmd.exe'\n"):
            found, bad = _start_process_violations(src)
            self.assertEqual((found, len(bad)), (1, 1), src)
        self.assertEqual(_start_process_violations("x = 'Sta' + ('rt-Pro' + 'cess cmd.exe -WindowStyle Hidden')\n"), (1, []))
        self.assertEqual(_start_process_violations("x = 'the ' + (name + ' list')\n"), (0, []))

    def test_pass11_a4_an_alias_or_dynamic_import_of_an_imported_module_keeps_its_identity(self):
        for label, src in {
            "alias": "import shutil\ns = shutil\ns.os.system('cmd.exe /c exit')\n",
            "alias of alias": "import shutil\ns = shutil\nt = s\nt.os.system('x')\n",
            "annotated alias": "import shutil\ns: object = shutil\ns.os.system('x')\n",
            "walrus statement": "import shutil\n(s := shutil)\ns.os.system('x')\n",
            "attribute alias": "import os\np = os.path\np.os.system('x')\n",
            "dynamic": "import importlib\nimportlib.import_module('shutil').os.system('x')\n",
            "dynamic __import__": "__import__('platform').subprocess.run(['x'])\n",
            "dynamic alias": "import importlib\ns = importlib.import_module('tempfile')\ns._os.system('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("import shutil\ns = shutil\ns.which('z')\nself.os = 1\n"), [])

    # Pass 12 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r5-8f9b7763), findings A1-A4.
    def test_pass12_a1_a_dotted_dunder_import_with_an_unresolvable_fromlist_is_reported(self):
        for label, src in {
            "name": "names = []\nm = __import__('os.path', fromlist=names)\nm.system('cmd.exe /c exit')\n",
            "call": "m = __import__('asyncio.windows_utils', fromlist=pick())\nm.Popen(['x'])\n",
            "positional": "m = __import__('os.path', globals(), locals(), names)\nm.system('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("m = __import__('json', fromlist=names)\nm.dumps(1)\n"), [])  # no dot: the root is the module either way

    def test_pass12_a2_an_import_in_a_class_body_is_a_class_attribute(self):
        for label, src in {
            "shutil": "class X:\n    import shutil\nX.shutil.os.system('cmd.exe /c exit')\n",
            "from import": "class X:\n    from platform import os\nX.os.system('x')\n",
            "tempfile": "class X:\n    import tempfile\nX.tempfile._os.system('x')\n",
            "nested": "class X:\n    if True:\n        import platform\nX.platform.subprocess.run(['x'])\n",
            "json": "class X:\n    import json\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("class X:\n    def m(self):\n        import json\n        return json.dumps(1)\n"), [])

    def test_pass12_a3_a_star_import_from_any_module_may_bring_os_and_subprocess(self):
        for label, src in {
            "platform": "from platform import *\nos.system('cmd.exe /c exit')\n",
            "subprocess": "from platform import *\nsubprocess.run(['x'])\n",
            "logging.config": "from logging.config import *\nos.system('x')\n",
            "alias": "from platform import *\ns = os\ns.system('x')\n",
            "own package": "from .helpers import *\nos.popen('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("from platform import *\nprint(machine())\n"), [])

    def test_pass12_a4_an_escaped_leading_dash_is_argument_text_not_a_parameter(self):
        found, bad = _start_process_violations("x = 'Start-Process cmd.exe -ArgumentList `-WindowStyle:Hidden'\n")
        self.assertEqual((found, len(bad)), (1, 1))
        found, bad = _start_process_violations("x = 'Start-Process cmd.exe -ArgumentList `-WindowStyle Hidden'\n")
        self.assertEqual((found, len(bad)), (1, 1))
        self.assertEqual(_start_process_violations("x = 'Start`-Process cmd.exe -WindowStyle Hidden'\n"), (1, []))  # mid-token: still the command
        self.assertEqual(_start_process_violations("x = 'Start-Process cmd.exe -Window`Style Hidden'\n"), (1, []))


class AdversaryR6Tests(unittest.TestCase):
    """r6: Dot R5-F1 (+ starred residual), Grok G8-G11. Each row is RED on a6f58aef and GREEN on the fix head."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    # --- Dot R5-F1: a fromlist known only at run time keeps BOTH identities (root and submodule) => the import is reported ---
    def test_r5_f1_an_unresolved_fromlist_reports_the_root_and_the_submodule_spawn(self):
        for label, src in {
            "dot's literal (empty at run time)": "names = []\nm = __import__('asyncio.base_events', fromlist=names)\n"
                                                 "async def launch():\n    await m.create_subprocess_exec('cmd.exe')\n",
            "nonempty variable, submodule spawn": "names = ['subprocess']\nm = __import__('asyncio.base_events', fromlist=names)\nm.subprocess.run(['x'])\n",
            "positional, root spawn": "names = []\nm = __import__('asyncio.base_events', None, None, names)\nm.create_subprocess_shell('x')\n",
            "keyword, submodule spawn": "m = __import__('asyncio.windows_utils', fromlist=pick())\nm.Popen(['x'])\n",
            "builtins form": "import builtins\nnames = []\nm = builtins.__import__('asyncio.base_events', fromlist=names)\nm.create_subprocess_exec('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # controls: a fromlist that IS resolved keeps one identity, and a flagged call through it is clean
        self.assertEqual(self._bad("m = __import__('asyncio.windows_utils', fromlist=['Popen'])\nm.Popen(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("m = __import__('asyncio.base_events', fromlist=[])\nm.create_subprocess_exec('x', creationflags=_NO_WINDOW)\n"), [])
        self.assertGreaterEqual(len(self._bad("m = __import__('asyncio.base_events', fromlist=[])\nm.create_subprocess_exec('x')\n")), 1)
        self.assertGreaterEqual(len(self._bad("m = __import__('asyncio.windows_utils', fromlist=['Popen'])\nm.Popen(['x'])\n")), 1)

    def test_r5_f1_residual_a_starred_fromlist_has_an_unknown_cardinality(self):
        for label, src in {
            "list star": "names = []\nm = __import__('asyncio.base_events', fromlist=[*names])\nm.create_subprocess_exec('cmd.exe')\n",
            "tuple star": "names = []\nm = __import__('asyncio.base_events', fromlist=(*names,))\nm.create_subprocess_exec('cmd.exe')\n",
            "positional list star": "names = []\nm = __import__('asyncio.base_events', None, None, [*names])\nm.create_subprocess_exec('x')\n",
            "two stars": "a = []\nb = []\nm = __import__('asyncio.base_events', fromlist=[*a, *b])\nm.create_subprocess_exec('x')\n",
            "starred call args": "args = ('asyncio.base_events', None, None, [])\nm = __import__(*args)\nm.create_subprocess_exec('x')\n",
            "double-star keywords": "kw = {'fromlist': []}\nm = __import__('asyncio.base_events', **kw)\nm.create_subprocess_exec('x')\n",
            "trailing positional star": "rest = ([],)\nm = __import__('asyncio.base_events', None, None, *rest)\nm.create_subprocess_exec('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # controls: one plain element makes it nonempty whatever the star holds; an all-literal empty tuple is the root
        self.assertEqual(self._bad("m = __import__('asyncio.windows_utils', fromlist=['Popen', *names])\nm.Popen(['x'], creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("m = __import__('asyncio.base_events', fromlist=())\nm.create_subprocess_exec('x', creationflags=_NO_WINDOW)\n"), [])
        self.assertEqual(self._bad("m = __import__('json', fromlist=[*names])\nm.dumps(1)\n"), [])  # no dot: one identity either way

    # --- Grok G8: a launch split across the holes of an f-string, a .format() or a % template ---
    def test_g8_a_start_process_split_by_a_hole_is_reported_in_every_spelling(self):
        for label, src in {
            "f-string hole": "x = f'Sta{name}rt-Process cmd.exe'\n",
            "format, literal arg": "x = '{}rt-Process cmd.exe'.format('Sta')\n",
            "percent, literal arg": "x = '%srt-Process cmd.exe' % 'Sta'\n",
            "format, variable arg": "x = 'Sta{}rt-Process cmd.exe'.format(name)\n",
            "percent, variable arg": "x = 'Sta%srt-Process cmd.exe' % name\n",
            "format, numbered": "x = '{1}{0}-Process cmd.exe'.format('rt', 'Sta')\n",
            "format, keyword": "x = '{a}rt-Process cmd.exe'.format(a='Sta')\n",
            "percent, tuple": "x = '%s%s-Process cmd.exe' % ('Sta', 'rt')\n",
            "percent, mapping": "x = '%(a)srt-Process cmd.exe' % {'a': 'Sta'}\n",
            "f-string, alias": "x = f'sa{name}ps cmd.exe'\n",
            "f-string, two holes": "x = f'S{a}ta{b}rt-Process cmd.exe'\n",
        }.items():
            found, bad = _start_process_violations(src)
            self.assertEqual((found, len(bad)), (1, 1), label)
        # controls: no launch anywhere in the assembled text
        # (r7: `f'Sta{name}rtup'` is no longer a control: the hole may hold `rt-`, so `Start-` can be built; the exact fold keeps the known ones clean)
        for src in ("x = '{}rtup cmd.exe'.format('Sta')\n", "x = '%sged of the line' % 'Sta'\n", "x = f'the {kind} of it'\n"):
            self.assertEqual(_start_process_violations(src), (0, []), src)
        # the unsplit forms keep their old verdicts
        self.assertEqual(_start_process_violations("x = 'Start-Process cmd.exe -WindowStyle Hidden'\n"), (1, []))

    # --- Grok G9: the event-loop spawn method named by a folded string ---
    def test_g9_a_loop_method_name_assembled_from_literals_is_reported(self):
        for method in sorted(_LOOP_SPAWN_METHODS):
            head, tail = method[: len(method) // 2], method[len(method) // 2:]
            for label, expr in {
                "plus": repr(head) + " + " + repr(tail),
                "plus, three parts": repr(head[:2]) + " + " + repr(head[2:]) + " + " + repr(tail),
                "percent": repr(head + "%s") + " % " + repr(tail),
                "format": repr(head + "{}") + ".format(" + repr(tail) + ")",
                "join": "''.join((" + repr(head) + ", " + repr(tail) + "))",
                "f-string literal hole": "f" + repr(head + "{'" + tail + "'}"),
            }.items():
                with self.subTest(method=method, form=label):
                    self.assertGreaterEqual(len(self._bad("getattr(loop, " + expr + ")\n")), 1)
            with self.subTest(method=method, form="constant name"):
                self.assertGreaterEqual(len(self._bad("H = " + repr(head) + "\ngetattr(loop, H + " + repr(tail) + ")\n")), 1)
        # controls: a name that is not an event-loop spawn method stays clean
        self.assertEqual(self._bad("getattr(loop, 'call_' + 'soon')\n"), [])
        self.assertEqual(self._bad("x = 'subprocess_' + 'helper'\n"), [])

    # --- Grok G10: an imported module flowing into a binding the scan cannot follow ---
    def test_g10_an_imported_module_in_an_unfollowable_binding_is_reported(self):
        for label, src in {
            "tuple unpack": "import shutil\ns, = (shutil,)\ns.os.system('x')\n",
            "conditional": "import shutil\ns = shutil if True else None\ns.os.system('x')\n",
            "list subscript": "import shutil\ns = [shutil][0]\ns.os.system('x')\n",
            "dict value": "import shutil\ns = {'m': shutil}['m']\ns.os.system('x')\n",
            "or-chain": "import shutil\ns = None or shutil\ns.os.system('x')\n",
            "star unpack": "import shutil\ns, = [*(shutil,)]\ns.os.system('x')\n",
            "lambda return": "import shutil\nf = lambda: shutil\nf().os.system('x')\n",
            "function return": "import shutil\ndef get():\n    return shutil\nget().os.system('x')\n",
            "dynamic import in a container": "s = [__import__('shutil')][0]\ns.os.system('x')\n",
            "alias of an alias": "import shutil\nt = shutil\ns, = (t,)\ns.os.system('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # controls: a plain alias, an attribute receiver, a flagged call and an unrelated container stay clean
        self.assertEqual(self._bad("import shutil\ns = shutil\ns.which('z')\n"), [])
        self.assertEqual(self._bad("import shutil\nx = shutil.which('z')\n"), [])
        self.assertEqual(self._bad("import os\nvalues = [os.getcwd(), 1]\nprint(values[0])\n"), [])
        self.assertEqual(self._bad("import json\nx = [json.dumps(1)]\n"), [])

    # --- Grok G11: a class-body binding of an imported module ---
    def test_g11_a_class_body_binding_of_an_imported_module_is_reported(self):
        for label, src in {
            "__import__": "class X:\n    m = __import__('shutil')\nX.m.os.system('x')\n",
            "import_module": "import importlib\nclass X:\n    m = importlib.import_module('shutil')\nX.m.os.system('x')\n",
            "plain alias": "import shutil\nclass X:\n    m = shutil\nX.m.os.system('x')\n",
            "annotated": "import shutil\nclass X:\n    m: object = shutil\nX.m.os.system('x')\n",
            "nested block": "import shutil\nclass X:\n    if True:\n        m = shutil\nX.m.os.system('x')\n",
            "alias of alias": "import shutil\nt = shutil\nclass X:\n    m = t\nX.m.os.system('x')\n",
            "from importlib import": "from importlib import import_module as im\nclass X:\n    m = im('shutil')\nX.m.os.system('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # controls: a non-module class attribute, and the same bindings outside a class, stay clean
        self.assertEqual(self._bad("import shutil\nclass X:\n    n = 1\n    w = shutil.which\n"), [])
        self.assertEqual(self._bad("import shutil\nm = shutil\nm.which('z')\n"), [])
        self.assertEqual(self._bad("class X:\n    def f(self):\n        import shutil\n        m = shutil\n        return m.which('z')\n"), [])


class AdversaryR6Pass13Tests(unittest.TestCase):
    """r6 adversary pass 13 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r6-f58ea05c), findings A1-A4 and their siblings, as classes."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    @staticmethod
    def _judge(ps: str) -> tuple[int, int]:
        found, bad = _start_process_violations("x = " + repr(ps) + "\n")
        return found, len(bad)

    # A1: the fold must do what Python does: a replace count, a format precision/width/conversion, a % precision
    def test_a1_the_fold_applies_replace_counts_and_format_specs_like_python(self):
        for src in ("'ssubprocess_exec'.replace('s', '', 1)", "'ssubprocess_exec'.replace('s', '')", "'{:.4s} cmd'.format('saps_suffix')",
                    "'{:>6.4s}'.format('saps_x')", "'{!r}'.format('ab')", "'{0:.5s}{1}'.format('start-x', 'y')", "'%.4s cmd' % 'saps_suffix'",
                    "'%-6.4s|' % 'saps_x'", "'%r' % 'ab'", "'{{}}{}'.format('a')", "'%%%s' % 'a'"):
            want = eval(src)  # the test's own literal, never scanned text
            self.assertEqual(_flatten(ast.parse(src, mode="eval").body, {}), (want, True), src)
        # a spec the fold cannot apply makes the text unknown rather than wrong; an enormous width is never allocated
        self.assertEqual(_flatten(ast.parse("'{:>999999999}'.format('x')", mode="eval").body, {})[1], False)
        self.assertEqual(_flatten(ast.parse("'{:{}}'.format('x', 3)", mode="eval").body, {})[1], False)

    def test_a1_a_replace_count_or_format_precision_cannot_hide_a_loop_method_or_a_launch(self):
        for label, src in {
            "replace count": "import asyncio\nloop = asyncio.get_running_loop()\ngetattr(loop, 'ssubprocess_exec'.replace('s', '', 1))\n",
            "precision on exec": "getattr(loop, '{:.15s}'.format('subprocess_exec_extra'))\n",
            "precision on shell": "getattr(loop, '%.16s' % 'subprocess_shell_x')\n",
            "precision on transport": "getattr(loop, '{:.26s}'.format('_make_subprocess_transport_x'))\n",
            "replace on shell": "getattr(loop, 'subprocess_shellX'.replace('X', ''))\n",
            "replace count keyword": "getattr(loop, 'ssubprocess_exec'.replace('s', '', count=1))\n",
            "upper/lower": "getattr(loop, 'SUBPROCESS_EXEC'.lower())\n",
            "strip": "getattr(loop, '  subprocess_exec '.strip())\n",
            "slice": "getattr(loop, 'xsubprocess_exec'[1:])\n",
            "reversed slice": "getattr(loop, 'cexe_ssecorpbus'[::-1])\n",
            "chr": "getattr(loop, chr(115) + 'ubprocess_exec')\n",
            "bytes decode": "getattr(loop, b'subprocess_exec'.decode())\n",
            "format conversion": "getattr(loop, '{!s}'.format('subprocess_exec'))\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        for label, ps in {
            "format precision": "x = '{:.4s} cmd.exe'.format('saps_suffix')\n",
            "percent precision": "x = '%.5s cmd.exe' % 'start_suffix'\n",
            "replace count": "x = 'ssaps cmd.exe'.replace('s', '', 1)\n",
        }.items():
            found, bad = _start_process_violations(ps)
            self.assertEqual((found, len(bad)), (1, 1), label)

    # A2: reflection reaches any imported module (any module may re-export os), not only a bound spawn module
    def test_a2_reflection_over_a_file_that_only_imports_a_module_is_reported(self):
        for label, src in {
            "globals": "import shutil\nglobals()['shutil'].os.system('cmd.exe')\n",
            "locals": "import shutil\nlocals()['shutil'].os.system('cmd.exe')\n",
            "vars of the module": "import shutil\nvars(shutil)['os'].system('cmd.exe')\n",
            "module __dict__": "import shutil\nshutil.__dict__['os'].system('cmd.exe')\n",
            "__getattribute__": "import shutil\nshutil.__getattribute__('os').system('cmd.exe')\n",
            "dunder on a dynamic import": "__import__('shutil').__dict__['os'].system('x')\n",
            "annotation": "import shutil\ndef f(a: shutil.os): pass\nf.__annotations__['a'].system('x')\n",
            "signature": "import shutil, inspect\ndef f(a: shutil.os): pass\ninspect.signature(f).parameters['a'].annotation.system('x')\n",
            "get_type_hints": "import shutil, typing\ndef f(a: shutil.os): pass\ntyping.get_type_hints(f)['a'].system('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # controls: a file that reflects over nothing it imported, and a module read by a fixed attribute, stay clean
        self.assertEqual(self._bad("x = {}\ny = vars()\nz = 1\n"), [])
        self.assertEqual(self._bad("import shutil\nx = shutil.which('z')\nprint(shutil.__name__)\n"), [])
        self.assertEqual(self._bad("import json\nprint(json.__doc__)\n"), [])

    # A3: PowerShell keywords that put a command name next to the keyword
    def test_a3_a_launch_after_return_throw_or_exit_is_judged(self):
        for kw in ("return", "throw", "exit", "RETURN", "Throw"):
            for alias in ("saps", "start", "Start-Process"):
                self.assertEqual(self._judge(f"{kw} {alias} cmd.exe"), (1, 1), f"{kw} {alias}")
                self.assertEqual(self._judge(f"{kw} {alias} cmd.exe -WindowStyle Hidden"), (1, 0), f"{kw} {alias} hidden")
        self.assertEqual(self._judge("return (saps cmd.exe)"), (1, 1))
        self.assertEqual(self._judge("if ($a) { return saps cmd.exe }"), (1, 1))
        self.assertEqual(self._judge("! saps cmd.exe"), (1, 1))
        self.assertEqual(self._judge("-not saps cmd.exe"), (1, 1))
        for src in ("x = 'ret' + 'urn saps cmd.exe'\n", "x = 'throw {} cmd.exe'.format('sa' + 'ps')\n",
                    "x = 'exit s{}s cmd.exe'.format('ap')\n", "x = 'return %s cmd.exe' % 'start'\n"):
            found, bad = _start_process_violations(src)
            self.assertEqual((found, len(bad)), (1, 1), src)
        # controls: prose that merely contains the words
        self.assertEqual(self._judge("Write-Output 'return the start of it'"), (0, 0))
        self.assertEqual(self._judge("return $start"), (0, 0))

    # A4: PowerShell reads typographic quotes, dashes and a no-break space as the ASCII ones
    def test_a4_typographic_quotes_dashes_and_spaces_are_read_like_ascii(self):
        for label, ps in {
            "smart double alias": "& “saps” cmd.exe",
            "smart single alias": "& ‘saps’ cmd.exe",
            "smart double start": "& “Start-Process” cmd.exe",
            "low double": "& „saps” cmd.exe",
            "single low-9": "& ‚saps’ cmd.exe",
            "reversed single": "& ‛saps’ cmd.exe",
            "nested script in smart quotes": "powershell -c “Start-Process cmd.exe”",
            "no-break space": "Start-Process cmd.exe",
            "en dash style": "Start-Process cmd.exe –WindowStyle Normal",
        }.items():
            self.assertEqual(self._judge(ps), (1, 1), label)
        for label, ps in {
            "en dash hidden": "Start-Process cmd.exe –WindowStyle Hidden",
            "em dash hidden": "Start-Process cmd.exe —WindowStyle Hidden",
            "smart quoted hidden": "Start-Process cmd.exe -WindowStyle ‘Hidden’",
            "smart double quoted hidden": "& “saps” cmd.exe -WindowStyle “Hidden”",
        }.items():
            self.assertEqual(self._judge(ps), (1, 0), label)
        self.assertEqual(self._judge("Write-Output “the start of it”"), (0, 0))


class AdversaryR6Pass14Tests(unittest.TestCase):
    """r6 adversary pass 14 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r6-231e0d2d), findings A1-A4 and their siblings, as classes."""

    def _bad(self, source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    # A1 / A2: the fold keeps an f-string hole's spec and conversion, and a format field's index
    def test_a1_a2_the_fold_keeps_fstring_specs_and_format_field_indexes(self):
        for src in ("f\"{'subprocess_exec_extra':.15s}\"", "f\"{'subprocess_exec':>20}\"", "f\"{'ab'!r}\"", "f\"{'abc':{'.2'}s}\"",
                    "'{0[0]}ubprocess_exec'.format('sx')", "'{[0]}ubprocess_exec'.format('sx')", "'{a[1]}'.format(a='xs')",
                    "'{0[0]}{0[1]}'.format('su')", "'{x[0]}ubprocess_exec'.format_map({'x': 'sx'})"):
            want = eval(src)  # the test's own literal, never scanned text
            got = _flatten(ast.parse(src, mode="eval").body, {})
            self.assertEqual(got, (want, True), src)
        # an accessor the fold cannot apply is unknown, never a wrong known text
        for src in ("'{0.real}'.format('x')", "'{0[a]}'.format('x')", "'{0[9]}'.format('x')", "'{0[-1]}'.format('x')", "f\"{'x':{width}}\""):
            self.assertFalse(_flatten(ast.parse(src, mode="eval").body, {})[1], src)
        for label, src in {
            "fstring precision": "getattr(loop, f\"{'subprocess_exec_extra':.15s}\")\n",
            "format index": "getattr(loop, '{0[0]}ubprocess_exec'.format('sx'))\n",
            "format keyword index": "getattr(loop, '{a[0]}ubprocess_exec'.format(a='sx'))\n",
            "format_map index": "getattr(loop, '{a[0]}ubprocess_exec'.format_map({'a': 'sx'}))\n",
            "fstring conversion": "getattr(loop, f\"{'subprocess_exec'!s}\")\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        found, bad = _start_process_violations("x = f\"{'saps_extra':.4s} cmd.exe\"\n")
        self.assertEqual((found, len(bad)), (1, 1))

    # A3: a parameter belongs to the parenthesized group its command sits in, not to any group at the same depth
    def test_a3_a_hidden_parameter_in_a_sibling_group_does_not_count(self):
        judge = lambda ps: (lambda r: (r[0], len(r[1])))(_start_process_violations("x = " + repr(ps) + "\n"))  # noqa: E731
        for ps in ("$p = (Start-Process cmd.exe -PassThru), (Start-Process cmd.exe -WindowStyle Hidden -PassThru)",
                   "@((saps a), (saps b -WindowStyle Hidden))",
                   "Write-Output (start a) (Get-X -WindowStyle Hidden)",
                   "(Start-Process cmd.exe) -WindowStyle Hidden",
                   "$(Start-Process a) + $(Get-Foo -WindowStyle Hidden)"):
            found, bad = judge(ps)
            self.assertGreaterEqual(bad, 1, ps)
        self.assertEqual(judge("$p = (Start-Process cmd.exe -WindowStyle Hidden -PassThru), (Start-Process cmd.exe -WindowStyle Hidden)"), (2, 0))
        self.assertEqual(judge("$p = (Start-Process a -ArgumentList (Get-Args) -WindowStyle Hidden)"), (1, 0))
        self.assertEqual(judge("$x = @((saps a -WindowStyle Hidden), (saps b -WindowStyle Hidden))"), (2, 0))

    # A4: the message of an assert is a value the AssertionError carries away
    def test_a4_an_imported_module_as_an_assert_message_is_reported(self):
        for label, src in {
            "assert message": "import shutil\ntry:\n    assert False, shutil\nexcept AssertionError as e:\n    e.args[0].os.system('cmd.exe')\n",
            "raise argument": "import shutil\ntry:\n    raise ValueError(shutil)\nexcept ValueError as e:\n    e.args[0].os.system('cmd.exe')\n",
            "raise the module": "import shutil\ntry:\n    raise shutil\nexcept TypeError:\n    pass\n",
            "match subject": "import shutil\nmatch shutil:\n    case m:\n        m.os.system('x')\n",
            "with item": "import shutil\nwith shutil as s:\n    s.os.system('x')\n",
            "await": "import shutil\nasync def f():\n    s = await shutil\n    s.os.system('x')\n",
            "dynamic import message": "assert False, __import__('shutil')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("import shutil\nassert shutil\nassert shutil.which('x'), 'missing'\n"), [])  # the test of an assert is never carried away


class AdversaryR7Tests(unittest.TestCase):
    """r7: adversary pass 15 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r6-95a5f55e) findings A1-A4 and their siblings, as a class, plus Grok G12.

    The string fold no longer re-implements Python's formatting to the last corner: a piece it cannot read exactly (a number, a ``*``/``**``
    expansion, a set whose order is unknown, a run-time name, a spec it does not apply) is a wildcard, and a built name is matched as a
    pattern against the spawn-method names and the PowerShell launch keywords. A fold mistake can therefore only over-report.
    """

    @staticmethod
    def _bad(source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    @staticmethod
    def _launch(expr: str) -> str:
        return ("import asyncio\nasync def launch():\n    loop = asyncio.get_running_loop()\n"
                f"    await getattr(loop, {expr})(asyncio.SubprocessProtocol, 'cmd.exe', '/c', 'exit')\n")

    @staticmethod
    def _named(expr: str) -> str:
        """r15: the same text in a position that is NOT a getattr name (an assignment), so a control keeps testing the name decision the text goes through; in a getattr
        name position (``_launch``) every text that is not a constant is reported (the HUB RULING), so a clean control cannot live there."""
        return ("import asyncio\nasync def launch():\n    loop = asyncio.get_running_loop()\n"
                f"    name = {expr}\n    await loop.run_in_executor(None, print, name)\n")

    @staticmethod
    def _ps(expr: str) -> tuple[int, int]:
        found, bad = _start_process_violations(f"x = {expr}\n")
        return found, len(bad)

    # every spelling of the name `subprocess_exec` / the launch `saps cmd.exe` that hides one character behind something the fold cannot read as a text
    HIDDEN_NAME = (
        # A1: a set holds each element once, in no fixed order
        "'subprocess_' + ''.join({'exec', 'exec'})", "'subprocess_' + ''.join({'exec', 'x'})", "'subprocess_' + ''.join({*parts})",
        "'subprocess_' + ''.join([*parts])", "'subprocess_e' + ''.join({'x', 'x'}) + 'ec'",
        # A2: a number prints as whatever its spec says
        "f'{115:c}ubprocess_exec'", "f'{0x73:c}ubprocess_exec'", "f'{115:{\"c\"}}ubprocess_exec'", "'{:c}ubprocess_exec'.format(115)",
        "'{0:c}ubprocess_exec'.format(115)", "'{x:c}ubprocess_exec'.format(x=115)", "'{x:c}ubprocess_exec'.format_map({'x': 115})",
        "'%cubprocess_exec' % 115", "'%cubprocess_exec' % (115,)", "'%(x)cubprocess_exec' % {'x': 115}", "'{:c}ubprocess_exec'.format(115.0)",
        # A3: an expansion rebinds what the literal part of the mapping says
        "'{x}ubprocess_exec'.format_map({'x': 'z', **{'x': 's'}})", "'%(x)subprocess_exec' % {'x': 'z', **{'x': 's'}}",
        "'{x}ubprocess_exec'.format(**{'x': 's'})", "'{x}ubprocess_exec'.format(x='z', **kw)", "'{}ubprocess_exec'.format(*['s'])",
        "'{}{}ubprocess_exec'.format('', *['s'])", "'%subprocess_exec' % (*['s'],)", "'%s%subprocess_exec' % ('', *['s'])",
        "'{x}ubprocess_exec'.format_map(mapping)", "'%(x)subprocess_exec' % mapping", "'%subprocess_exec' % args",
        # an operation with run-time arguments over a text that holds the name may leave it whole
        "'subprocess_exec'.replace(a, b)", "'xsubprocess_exec'[n:]", "'subprocess_exec_x'[:n]", "'subprocess_exec'.strip(chars)", "'subprocess_exec'.replace('x', b, 1)",
        # pass 16 A1: the template itself holds a wildcard, so the wildcard may open, close or lengthen a field
        "('%' + kind + 'ubprocess_exec') % 115", "('{' + field + 'ubprocess_exec').format(115)", "(prefix + '%subprocess_exec') % ('x',)",
        "(prefix + '{}ubprocess_exec').format('s')", "('%(' + key + ')subprocess_exec') % mapping", "('{' + field + '}ubprocess_exec').format_map(m)",
        "('%' + kind + 'subprocess_exec') % 115",
        # self-probe: a key with nested parentheses reads to its balanced close; a bytes literal of the name
        "'%(a(b))ssubprocess_exec' % mapping", "str(b'subprocess_exec', 'ascii')", "b'subprocess_exec'.decode()",
        # pass 16 A2: an operand that contributes no characters (a separator with fewer than two pieces) must not make an exact name unknown
        "sep.join([]) + 'sub' + 'process_exec'", "sep.join(()) + 'subprocess_exec'", "sep.join(['sub']) + 'process_exec'",
        "sep.join(('sub',)) + 'process_exec'", "sep.join({'sub'}) + 'process_exec'", "f'{sep.join([])}subprocess_exec'",
        "'{}subprocess_exec'.format(sep.join([]))", "'%ssubprocess_exec' % sep.join(())", "'{}'.format(sep.join(['subprocess_exec']))",
        # unknown positions behind a `*` width, a missing argument, a lone brace
        "'%*dsubprocess_exec' % (1, 2)", "'{}{}ubprocess_exec'.format('s')",
    )
    HIDDEN_LAUNCH = (
        "'sa' + ''.join({'p', 'p'}) + 's cmd.exe'", "'%caps cmd.exe' % 115", "f'{115:c}aps cmd.exe'", "'{:c}aps cmd.exe'.format(115)",
        "'{x}aps cmd.exe'.format_map({'x': 'z', **{'x': 's'}})", "'%(x)saps cmd.exe' % {'x': 'z', **{'x': 's'}}",
        "'{x}aps cmd.exe'.format(**kw)", "'%saps cmd.exe' % (*parts,)", "'{}aps cmd.exe'.format(*parts)",
        "'%c%caps cmd.exe' % (1, 2)", "'sa' + '%c' % 112 + 's cmd.exe'", "f'S{97:c}rt-Process cmd.exe'", "f'{115:c}{116:c}art-Process cmd.exe'",
        # pass 16 A1 (a wildcard in the template) and A3 (holes far apart: no bound on how many or how long the gaps between them are)
        "('%' + kind + 'aps cmd.exe') % 115", "('{' + field + 'aps cmd.exe').format(115)", "f'St{a}-Pro{b}ss cmd.exe'", "f'S{a}t{b}a{c}r{d}t-Process cmd.exe'",
        "f'St{a}rt-Pro{b}ces{c}s cmd.exe'", "f'{a}-Process-and-more-text-between {b}tart-Process cmd.exe'", "f'sa{a}ps cmd.exe'",
        "f'S{a}t{b}a{c}r{d}t-Pro{e}ce{f}ss cmd.exe'", "'S%st%sa%sr%st-Process cmd.exe' % (a, b, c, d)", "'St{}-Pro{}ss cmd.exe'.format(a, b)",
    )

    def test_a1_a2_a3_a_name_hidden_behind_an_unreadable_piece_is_reported(self):
        for expr in self.HIDDEN_NAME:
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)

    def test_a1_a2_a3_a_powershell_launch_hidden_behind_an_unreadable_piece_is_reported(self):
        for expr in self.HIDDEN_LAUNCH:
            self.assertEqual(self._ps(expr), (1, 1), expr)

    def test_the_fold_gives_a_known_text_only_where_it_equals_pythons(self):
        for src in ("''.join({'exec', 'exec'})", "''.join({'e'})", "''.join(('a', 'b'))", "'-'.join(['a', 'b'])", "'{}x'.format('s')", "'%sx' % 's'",
                    "f'{\"a\"!r}'", "'{x}'.format_map({'x': 'a', 'x': 'b'})"):
            self.assertEqual(_flatten(ast.parse(src, mode="eval").body, {}), (eval(src), True), src)  # the test's own literal, never scanned text
        # a text the fold cannot guarantee is marked unknown and carries the wildcard
        for src in ("''.join({'a', 'b'})", "''.join({*x})", "f'{115:c}'", "'{:c}'.format(115)", "'%c' % 115", "'{x}'.format(**kw)", "'{}'.format(*a)",
                    "'%(x)s' % {**m}", "'{x}'.format_map({**m})", "'%s%s' % (*a,)", "'{:{}}'.format('x', 3)", "'%*d' % (3, 4)", "'a' + n"):
            text, known = _flatten(ast.parse(src, mode="eval").body, {})
            self.assertFalse(known, src)
            self.assertIn(_WILD, text, src)

    def test_a4_a_huge_spec_digit_string_is_never_converted_or_applied(self):
        digits = "9" * 5000  # int() of this raises ValueError (4300-digit limit) on Python 3.11+
        for src in (f"f\"{{'x':{digits}}}\"", "'{:" + digits + "}'.format('x')", "'{x:" + digits + "}'.format_map({'x': 'y'})", "'%" + digits + "s' % 'x'",
                    "'%." + digits + "s' % 'x'", "f\"{'x':.{'" + digits + "'}}\""):
            text, known = _flatten(ast.parse(src, mode="eval").body, {})
            self.assertFalse(known, src[:40])
        self.assertGreaterEqual(len(self._bad(self._launch("'{:" + digits + "}ubprocess_exec'.format('s')"))), 1)  # reported, not crashed on
        self.assertEqual(self._ps("'%" + digits + "saps cmd.exe' % 'x'")[0], 1)  # read as a launch, not crashed on

    def test_every_fold_that_is_not_exact_carries_a_wildcard(self):
        """The invariant the consumers rely on: a text WITHOUT the wildcard is exact, so `known` False always comes with `_WILD` in the text."""
        for expr in self.HIDDEN_NAME + self.HIDDEN_LAUNCH + ("'a' + n", "f'{n}'", "'%s' % n", "'{}'.format(n)", "sep.join([a, b])", "x[n:]", "x.replace(a, b)"):
            text, known = _flatten(ast.parse(expr, mode="eval").body, {})
            self.assertTrue(known or _WILD in text, (expr, text))
        # a join with fewer than two pieces writes no separator, so an unknown one does not matter
        for src, want in (("sep.join([])", ""), ("sep.join(['ab'])", "ab"), ("sep.join(('ab',))", "ab"), ("sep.join({'ab'})", "ab"), ("sep.join({'ab', 'ab'})", "ab")):
            self.assertEqual(_flatten(ast.parse(src, mode="eval").body, {}), (want, True), src)

    def test_a3_a_launch_hidden_across_any_number_of_holes_and_any_gap_is_reported(self):
        # exact: the matcher lays the keyword across the literal characters and the wildcards, so nothing is bounded
        self.assertTrue(_could_launch("St\x00-Pro\x00ss cmd.exe"))
        self.assertTrue(_could_launch("S\x00t\x00a\x00r\x00t-Process"))
        self.assertTrue(_could_launch("\x00aps cmd.exe") and _could_launch("sa\x00s cmd.exe") and _could_launch("; sa\x00s"))
        for text in ("\x00", "\x00 \x00", "the \x00 of it", "last\x00", "just\x00 now", "x\x00 y\x00 z", "wait for \x00-art", "\x00start", "restart\x00"):
            self.assertFalse(_could_launch(text), text)  # no keyword with characters of both the source and a wildcard where a command word stands

    def test_a4_a_dunder_import_with_a_level_is_resolved_relative_to_its_package_or_reported(self):
        for label, src in {
            "keyword level and globals": "wu = __import__('windows_utils', globals={'__package__': 'asyncio'}, fromlist=['Popen'], level=1)\nwu.Popen(['cmd.exe', '/c', 'exit'])\n",
            "positional level": "wu = __import__('windows_utils', {'__package__': 'asyncio'}, None, ['Popen'], 1)\nwu.Popen(['x'])\n",
            "run-time level": "wu = __import__('windows_utils', fromlist=['Popen'], level=n)\nwu.Popen(['x'])\n",
            "level by star": "wu = __import__('windows_utils', *args)\nwu.Popen(['x'])\n",
            "level by double star": "wu = __import__('windows_utils', **kw)\nwu.Popen(['x'])\n",
            "builtins form": "import builtins\nwu = builtins.__import__('base_events', globals(), None, ['x'], 1)\nwu.subprocess.run(['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        # the literal level 0, or none, is the ordinary absolute import
        self.assertEqual(self._bad("wu = __import__('json', fromlist=['dumps'], level=0)\nwu.dumps(1)\n"), [])
        self.assertEqual(self._bad("wu = __import__('json', None, None, ['dumps'], 0)\nwu.dumps(1)\n"), [])
        self.assertEqual(self._bad("wu = __import__('asyncio.windows_utils', fromlist=['Popen'])\nwu.Popen(['x'], creationflags=_NO_WINDOW)\n"), [])
        got = self._bad("wu = __import__('windows_utils', fromlist=['Popen'], level=1)\n")
        self.assertTrue(any("relative level" in g for g in got), got)

    def test_a5_a_field_index_that_int_would_reject_or_choke_on_is_unknown_not_a_crash(self):
        digits = "9" * 5000
        for expr in ("'{²}ubprocess_exec'.format(**{'²': 's'})", "'{²}ubprocess_exec'.format_map({'²': 's'})", "'{٣}ubprocess_exec'.format('s')",
                     "'{" + digits + "}ubprocess_exec'.format('s')", "'{0[" + digits + "]}ubprocess_exec'.format('s')", "'{0[²]}ubprocess_exec'.format('s')",
                     ):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr[:40])  # reported (the field is unknown), never ValueError
            _flatten(ast.parse(expr, mode="eval").body, {})
        for expr in ("'%²subprocess_exec' % 's'", "'%." + "٣" + "subprocess_exec' % 's'", "'%" + digits + "subprocess_exec' % 's'"):
            self._bad(self._launch(expr))  # a `%` field Python itself rejects never yields a string: only that the scan does not crash
            _flatten(ast.parse(expr, mode="eval").body, {})

    # --- pass 17 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r7-43fd2bec) A1-A4, as classes ---
    def test_pass17_a1_a_name_that_an_import_also_binds_is_not_the_literal_it_was_assigned(self):
        for label, src in {
            "dynamic import by name": "name = 'math'\nfrom os import __name__ as name\nm = __import__(name)\nm.system('cmd.exe /c exit')\n",
            "folded spawn name": "piece = 'wrong'\nfrom os import __name__ as piece\ngetattr(loop, '{}ubprocess_exec'.format(piece[1:]))(factory, 'cmd.exe')\n",
            "plain import as": "name = 'math'\nimport os as name\nm = __import__(name)\nm.system('x')\n",
            "import after": "from os import __name__ as name\nname = 'math'\nm = __import__(name)\nm.system('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._ps("'sap{} cmd.exe'.format(piece[1:])\nfrom os import __name__ as piece\npiece = 'wrong'"), (1, 1))
        self.assertEqual(self._bad("name = 'json'\nm = __import__(name)\nm.dumps(1)\n"), [])  # the ordinary once-bound literal is still followed

    def test_pass17_a2_a_wildcard_name_behind_any_dotted_receiver_is_reported(self):
        for expr in ("'loop.subprocess_' + kind", "'self.loop.subprocess_' + kind", "'{}.subprocess_exec'.format(x)", "f'{a}.{b}.subprocess_exec'",
                     "'loop.subprocess_%s' % kind", "'loop.' + 'subprocess_' + kind", "'a.b.c.subprocess_' + kind", "'%s.subprocess_' % kind + k2"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        self.assertGreaterEqual(len(self._bad("import asyncio, operator\nasync def f(kind, holder):\n    operator.attrgetter('loop.subprocess_' + kind)(holder)(1)\n")), 1)
        self.assertEqual(self._bad(self._named("'loop.value_' + kind")), [])

    def test_pass17_a3_an_escaped_space_outside_quotes_does_not_split_a_word(self):
        for ps in ("Start-Process cmd.exe -ArgumentList /c` exit` -WindowStyle` Hidden", "saps cmd.exe -ArgumentList foo` -WindowStyle` Hidden",
                   "start cmd.exe -ArgumentList foo` -WindowStyle` Hidden", "Start-Process cmd.exe -ArgumentList foo` -WindowStyle:Hidden",
                   "Start-Process cmd.exe foo`\t-WindowStyle Hidden"):
            self.assertEqual(self._ps(repr(ps)), (1, 1), ps)
        for ps in ("Start-Process cmd.exe -ArgumentList /c -WindowStyle Hidden", "Start-Process cmd.exe -ArgumentList a`  -WindowStyle Hidden",
                   "Start-Pro`cess cmd.exe -WindowStyle Hidden", "saps cmd.exe -WindowStyle` Hidden".replace("` ", " ")):
            self.assertEqual(self._ps(repr(ps)), (1, 0), ps)  # a real parameter still counts

    def test_pass17_a4_a_name_bound_to_two_import_functions_is_reported_not_resolved_by_the_first(self):
        for label, src in {
            "alias then rebind": "from importlib import import_module as load\nload = __import__\nm = load('asyncio.base_events', fromlist=[])\nasync def f():\n    await m.create_subprocess_exec('cmd.exe')\n",
            "two from-imports": "from importlib import import_module as load\nfrom builtins import __import__ as load\nm = load('asyncio.base_events', fromlist=[])\nm.create_subprocess_exec('cmd.exe')\n",
            "reverse direction": "from builtins import __import__ as load\nfrom importlib import import_module as load\nm = load('asyncio.windows_utils')\nm.Popen(['cmd.exe'])\n",
            "attribute then name": "import importlib\nload = importlib.import_module\nload = __import__\nm = load('shutil')\nm.os.system('x')\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("from importlib import import_module as load\nfrom importlib import import_module as load\nm = load('json')\nm.dumps(1)\n"), [])
        self.assertEqual(self._bad("import importlib\nload = importlib.import_module\nm = load('json')\nm.dumps(1)\n"), [])

    def test_over_reporting_stays_inside_the_names_and_keywords(self):
        for expr in ("'{}ubprocess_exec'.format('z')", "'subprocess_exec_' + 'x'", "f'{name}_value'", "'%s: %d' % (a, b)", "'{}'.format(x)", "''.join({'x'})",
                     "'{:c}'.format(115)", "name + 'value'", "'-'.join(parts)", "f'{n:>8}'", "'hello'[n:]"):  # r15: `'hello world'.replace(a, b)` flipped (it holds `hel`, a run of `subprocess_shell`)
            self.assertEqual(self._bad(self._named(expr)), [], expr)
        for expr in ("'{}aps cmd.exe'.format('z')", "f'the {kind} of it'", "'%s: %d' % (a, b)", "'{:c}'.format(115)", "f'{n:c} and {m:c}'",
                     "'x' + ''.join({'y', 'z'}) + 'x'", "f'{a}; {b}'"):
            self.assertEqual(self._ps(expr), (0, 0), expr)

    # --- Grok G12: an unresolved fromlist is reported with its own reason ---
    def test_g12_an_unresolved_fromlist_is_reported_as_a_fromlist_not_a_module_name(self):
        wanted = "fromlist is not statically known"
        for label, src in {
            "name": "names = []\nm = __import__('asyncio.base_events', fromlist=names)\n",
            "starred list": "names = []\nm = __import__('asyncio.base_events', fromlist=[*names])\n",
            "starred tuple": "names = []\nm = __import__('asyncio.base_events', fromlist=(*names,))\n",
            "star args": "a = ()\nm = __import__('asyncio.base_events', *a)\n",
            "double star": "kw = {}\nm = __import__('asyncio.base_events', **kw)\n",
            "builtins form": "import builtins\nn = []\nm = builtins.__import__('os.path', fromlist=n)\n",
        }.items():
            got = self._bad(src)  # (the builtins form also draws the unrelated "reads __import__ off an imported module" line)
            self.assertEqual(len([g for g in got if wanted in g]), 1, (label, got))
            self.assertFalse(any("module name is not a string literal" in g for g in got), (label, got))
        # the module-name reason keeps its text, and so does the relative-name reason
        got = self._bad("import importlib\nname = pick()\nm = importlib.import_module(name)\n")
        self.assertEqual(len(got), 1, got)
        self.assertIn("module name is not a string literal", got[0])
        got = self._bad("import importlib\nm = importlib.import_module('.sub', package=pick())\n")
        self.assertEqual(len(got), 1, got)
        self.assertIn("relative module name", got[0])
        # a resolved fromlist is not reported at all
        self.assertEqual(self._bad("m = __import__('asyncio.windows_utils', fromlist=['Popen'])\n"), [])


class AdversaryR8Tests(unittest.TestCase):
    """r8: Grok G13 / G14 and Dot 789-R7-F1 / 789-R7-F2 (head 43fd2bec / d79a3f80).

    G13: the PowerShell side judges the ``_flatten`` text of every node ``_flatten`` can fold (``_folds``), so the two sides cannot drift.
    G14: a string method the fold does not know hedges its receiver (``_hedge``) instead of dropping it.
    F1: the minimum-literal policy of ``_could_name`` counts the whole fold, not one piece.
    F2: a dotted receiver is matched by structure (``loop.*.subprocess_exec``), not by four sampled prefixes.
    """

    SP = "Start-Process"

    @staticmethod
    def _bad(source: str) -> list[str]:
        return _unflagged_spawns_in_source(source, "synthetic.py")

    @staticmethod
    def _launch(expr: str) -> str:
        return ("import asyncio\nasync def launch():\n    loop = asyncio.get_running_loop()\n"
                f"    await getattr(loop, {expr})(asyncio.SubprocessProtocol, 'cmd.exe', '/c', 'exit')\n")

    @staticmethod
    def _named(expr: str) -> str:
        """r15: the same text in a position that is NOT a getattr name (an assignment), so a control keeps testing the name decision the text goes through; in a getattr
        name position (``_launch``) every text that is not a constant is reported (the HUB RULING), so a clean control cannot live there."""
        return ("import asyncio\nasync def launch():\n    loop = asyncio.get_running_loop()\n"
                f"    name = {expr}\n    await loop.run_in_executor(None, print, name)\n")

    @staticmethod
    def _ps(expr: str) -> tuple[int, int]:
        found, bad = _start_process_violations(f"script = {expr}\n")
        return found, len(bad)

    # ---- G13: one row per spelling Grok named, each an unhidden launch ----
    def g13_rows(self) -> list[str]:
        sp = self.SP
        return [f"'zz{sp} cmd.exe'[2:]", "'zzsaps cmd.exe'[2:]", f"('zz' + '{sp} cmd.exe')[2:]", f"'zz{sp} cmd.exe'.lstrip('z')",
                f"'x{sp} cmd.exe'.partition('x')[2]"]

    def test_g13_a_launch_behind_a_fold_the_python_side_reads_is_reported_by_the_powershell_side(self):
        for expr in self.g13_rows():
            self.assertEqual(self._ps(expr), (1, 1), expr)
        self.assertEqual(self._ps(f"'{self.SP} cmd.exe'.upper()"), (1, 1))  # the control: reported before and after
        # a fold that reads EXACTLY is judged as the text it spells, so a hidden launch behind one is still clean
        hidden = f"{self.SP} cmd.exe -WindowStyle Hidden"
        for expr in (f"'zz{hidden}'[2:]", f"'zz{hidden}'.lstrip('z')", f"'{hidden}'.strip()", f"('zz' + '{hidden}')[2:]"):
            self.assertEqual(self._ps(expr), (1, 0), expr)

    def fold_kinds(self, target: str) -> dict[str, str]:
        """One expression per fold kind ``_flatten`` supports, each spelling ``target``; the keys name the kind."""
        pad = "zz" + target
        half = max(1, len(target) // 2)
        a, b = target[:half], target[half:]
        return {
            "literal": repr(target), "concat": f"{a!r} + {b!r}", "implicit concat": f"{a!r} {b!r}",
            "fstring const": f"f'{{{a!r}}}{b}'", "fstring plain": f"f'{target}'",
            "percent tuple": f"'%s{b}' % ({a!r},)", "percent dict": f"'%(a)s{b}' % {{'a': {a!r}}}",
            "percent scalar": f"'%s{b}' % {a!r}", "format": f"'{{}}{b}'.format({a!r})",
            "format named": f"'{{a}}{b}'.format(a={a!r})", "format_map": f"'{{a}}{b}'.format_map({{'a': {a!r}}})",
            "join list": f"''.join([{a!r}, {b!r}])", "join tuple": f"''.join(({a!r}, {b!r}))",
            "join set": f"''.join({{{target!r}}})", "replace": f"{pad.replace('zz', 'zq')!r}.replace('zq', '')", "replace count": f"{('z' + target)!r}.replace('z', '', 1)",
            "slice": f"{pad!r}[2:]", "slice step": f"{target[::-1]!r}[::-1]", "slice of concat": f"({'zz'!r} + {target!r})[2:]",
            "lstrip": f"{pad!r}.lstrip('z')", "rstrip": f"{(target + 'zz')!r}.rstrip('z')", "strip": f"{('zz' + target + 'zz')!r}.strip('z')",
            "lower": f"{target.upper()!r}.lower()", "upper": f"{target.lower()!r}.upper()", "casefold": f"{target.upper()!r}.casefold()",
            "swapcase": f"{target.swapcase()!r}.swapcase()", "chr": f"chr({ord(target[0])}) + {target[1:]!r}", "bytes decode": f"{target.encode()!r}.decode()",
            # G14: methods the fold has no arm for
            "removeprefix": f"{pad!r}.removeprefix('zz')", "removesuffix": f"{(target + 'zz')!r}.removesuffix('zz')",
            "partition": f"{('x' + target)!r}.partition('x')[2]", "rpartition": f"{(target + 'x')!r}.rpartition('x')[0]",
            "split": f"{('x_' + target)!r}.split('_', 1)[1]", "rsplit": f"{(target + '_x')!r}.rsplit('_', 1)[0]",
            "splitlines": f"{(target + chr(10) + 'x')!r}.splitlines()[0]", "unknown method": f"{target!r}.zzz_unknown_method()",
            "unknown method chain": f"{('x' + target)!r}.zzz_one().zzz_two('q')[1]",
        }

    def test_g13_every_fold_kind_is_judged_the_same_by_both_sides(self):
        """Walks every kind of fold ``_flatten`` supports (and the unknown-method fallback): the Python side reads the spawn-method name through it AND the
        PowerShell side reports the launch through it. A kind one side lost shows up here by name."""
        for kind, expr in self.fold_kinds("subprocess_exec").items():
            if kind != "upper":  # upper-casing the name makes it a different name on the Python side; the PowerShell side is case-insensitive
                self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, ("python side", kind, expr))
        for kind, expr in self.fold_kinds(f"{self.SP} cmd.exe").items():
            self.assertEqual(self._ps(expr), (1, 1), ("powershell side", kind, expr))
        for kind, expr in self.fold_kinds("saps cmd.exe").items():
            self.assertEqual(self._ps(expr), (1, 1), ("powershell side, alias", kind, expr))

    def test_g13_the_exact_kinds_fold_to_the_target_text(self):
        """The table exercises real fold arms: every kind but the unknown-method ones folds to exactly the target text."""
        unknown = {"removeprefix", "removesuffix", "partition", "rpartition", "split", "rsplit", "splitlines", "unknown method", "unknown method chain", "join set"}
        for kind, expr in self.fold_kinds("subprocess_exec").items():
            text, known = _flatten(ast.parse(expr, mode="eval").body, {})
            if kind not in unknown:
                self.assertEqual((text.lower(), known), ("subprocess_exec", True), (kind, expr))

    # ---- G14: a method the fold does not know hedges its receiver ----
    def test_g14_an_unknown_string_method_keeps_its_receiver_text(self):
        for expr in ("'zzsubprocess_exec'.removeprefix('zz')", "'subprocess_execzz'.removesuffix('zz')", "'xsubprocess_exec'.partition('x')[2]",
                     "'subprocess_exec_x'.rpartition('_')[0]", "'x_subprocess_exec'.split('_', 1)[1]", "'subprocess_exec_x'.rsplit('_', 1)[0]",
                     "'subprocess_exec\\nx'.splitlines()[0]", "'subprocess_exec'.zzz_unknown()", "'zsubprocess_exec'.zzz_a().zzz_b()[1:]"):
            text, known = _flatten(ast.parse(expr, mode="eval").body, {})
            self.assertFalse(known, expr)
            self.assertIn("subprocess_exec", text, expr)  # the receiver's text survives, followed by the wildcard
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)

    def test_g14_a_hedged_receiver_adds_no_false_positive_to_a_harmless_text(self):
        for expr in ("'hello'.removeprefix('h')", "'value'.partition('x')[2]", "'a,b'.split(',')[0]", "'path/to/file'.rsplit('/', 1)[1]", "'a\\nb'.splitlines()[0]",
                     "name.removeprefix('x')", "'abc'.zzz()", "'loop.value'.partition('.')[2]"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)
        for expr in ("'hello world'.partition(' ')[2]", "'a b'.split(' ')[0]", "'the end'.removesuffix('d')", "name.partition('x')[2]", "'plain'.zzz()"):
            self.assertEqual(self._ps(expr), (0, 0), expr)

    # ---- R7-F1: the minimum-literal policy counts the whole fold ----
    def test_f1_a_name_spelled_in_fragments_each_too_short_to_count_is_reported(self):
        for expr in ("f'su{98:c}pr{111:c}ce{115:c}s_{101:c}x{101:c}c'", "'su{}pr{}ce{}s_{}x{}c'.format(98, 111, 115, 101, 101)",
                     "'su%cpr%cce%cs_%cx%cc' % (98, 111, 115, 101, 101)", "'su{:c}pr{:c}ce{:c}s_{:c}x{:c}c'.format(98, 111, 115, 101, 101)",
                     "f'su{a}pr{b}ce{c}s_{d}x{e}c'", "f'su{a}pr{b}ce{c}s_{d}x{e}c'.lower()", "'s' + a + 'u' + b + 'b' + c"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        # the end-to-end launch of the finding
        src = ("import asyncio\nasync def go():\n    loop = asyncio.get_running_loop()\n"
               "    await getattr(loop, f'su{98:c}pr{111:c}ce{115:c}s_{101:c}x{101:c}c')(asyncio.SubprocessProtocol, 'cmd.exe')\n")
        self.assertGreaterEqual(len(self._bad(src)), 1)

    def test_f1_the_minimum_literal_policy_keeps_its_controls_clean(self):
        for expr in ("f'{a}{b}'", "'%s%s' % (a, b)", "'{}-{}'.format(a, b)", "f'x{a}y{b}'", "a + b + c", "f'{a}.{b}'", "'ab' + a", "f'{a}_{b}_{c}'", "f'p{a}q'",
                     "'allowed_roots.' + k"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)

    # ---- R7-F2: a dotted receiver is matched by structure ----
    def test_f2_a_literal_dot_after_a_wildcard_reads_as_a_dotted_receiver(self):
        src = ("import operator\nasync def launch(holder, part, factory):\n"
               "    return await operator.attrgetter('loop.' + part + '.subprocess_exec')(holder)(factory, 'cmd.exe')\n")
        self.assertGreaterEqual(len(self._bad(src)), 1)
        for expr in ("'loop.' + p + '.subprocess_exec'", "'a.' + p + '.b.' + q + '.subprocess_exec'", "p + '.loop.' + q + '.subprocess_shell'",
                     "'loop.%s.subprocess_exec' % p", "'{}.{}.subprocess_exec'.format(p, q)", "f'loop.{p}.subprocess_exec'", "f'self.{p}.loop.{q}.subprocess_exec'",
                     "p + '.' + q + '.subprocess_exec'", "'loop.' + p + '.' + q + '.subprocess_exec'"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)

    # ---- pass 18 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r8-6e1a9266) A1-A4, as classes ----
    def test_pass18_a1_a_receiver_segment_a_wildcard_completes_or_a_deep_receiver_is_reported(self):
        for expr in ("'root.component_' + part + '.subprocess_exec'", "'root.one.two.three.four.' + part + '.subprocess_exec'",
                     "'a.b.c.d.e.f.g.' + p + '.subprocess_shell'", "'root.comp_%s.subprocess_exec' % part", "f'root.component_{part}.subprocess_exec'",
                     "'one.two.' + p + 'three.' + q + '.subprocess_exec'", "p + 'x.' + q + '.subprocess_exec'", "'root.' + p + '_c.' + q + '.subprocess_exec'"):
            src = ("import operator\nasync def launch(holder, p, q, part, factory):\n"
                   f"    return await operator.attrgetter({expr})(holder)(factory, 'cmd.exe')\n")
            self.assertGreaterEqual(len(self._bad(src)), 1, expr)
        # a receiver, or a segment that is the receiver's, says nothing about the name
        for expr in ("'root.component_' + part", "'root.component_' + part + '.run'", "'root.one.two.three.four.' + part", "'root.value_' + part + '.x'",
                     "'root.' + p + '.' + q + '.value'"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)

    def test_pass18_a2_an_operation_with_a_run_time_result_over_a_named_text_does_not_hide_it(self):
        for expr in ("('subprocess_' + kind) * 1", "1 * ('subprocess_' + kind)", "('subprocess_' + kind) * n", "('subprocess_exec') * n", "('subprocess_shell' * n)",
                     "('_make_subprocess_' + kind) * 1", "('subprocess_' + kind) * 1 + ''", "(('subprocess_' + kind) * 1)[:]"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        for expr in ("('saps cmd.exe' + kind) * 1", "('saps cmd.exe') * n", f"('{self.SP} cmd.exe' + kind) * 1", "2 * ('saps cmd.exe' + kind)"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("('value_' + kind) * 1", "n * m", "('abc' + kind) * 2"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)

    def test_pass18_a4_a_hedged_text_holding_two_names_keeps_each_of_them_possible(self):
        for expr in ("'subprocess_exec;saps'.split(';')[0]", "'subprocess_exec;subprocess_shell'.split(';')[1]", "'subprocess_exec;subprocess_shell'.split(';')[0]",
                     "'subprocess_exec;_make_subprocess_transport'.split(';')[1]", "'_make_subprocess_transport;subprocess_exec'.split(';')[0]",
                     "'subprocess_exec;saps'.rstrip(';saps')", "'subprocess_exec;saps'[:n]", "'subprocess_exec;saps'.replace(a, b)",
                     "'subprocess_exec;saps'.removesuffix(';saps')", "'saps;subprocess_exec'.partition(';')[2]"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        # and the PowerShell side keeps the launch behind the same operations, even when the text also holds a spawn-method name
        for expr in ("'saps cmd.exe;subprocess_exec'.split(';')[0]", "'subprocess_exec;saps cmd.exe'.split(';')[1]", "'subprocess_exec;saps cmd.exe'[n:]"):
            self.assertEqual(self._ps(expr), (1, 1), expr)

    def test_pass18_a3_a_unicode_digit_width_is_as_big_as_python_reads_it(self):
        for spec in ("２５７", "٢٥٧", "１０２４"):  # 257 and 1024 spelled in fullwidth / Arabic-Indic digits
            text, known = _flatten(ast.parse("'{:" + spec + "}'.format('x')", mode="eval").body, {})
            self.assertFalse(known, spec)
            text, known = _flatten(ast.parse("f'{\"x\":" + spec + "}'", mode="eval").body, {})
            self.assertFalse(known, spec)

    # ---- pass 19 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r8-fb098bff) A1-A4, as classes ----
    def test_pass19_a1_a_string_that_spells_a_reflection_attribute_is_reported(self):
        for label, src in {
            "getattr defaults": "import subprocess\ndef launch(runner=subprocess.run): pass\ngetattr(launch, '__defaults__')[0](['cmd.exe', '/c', 'exit'])\n",
            "getattr kwdefaults": "import subprocess\ndef launch(*, runner=subprocess.run): pass\ngetattr(launch, '__kwdefaults__')['runner'](['x'])\n",
            "getattr globals": "import subprocess\ndef launch(): pass\ngetattr(launch, '__globals__')['subprocess'].run(['x'])\n",
            "built name": "import subprocess\ndef launch(runner=subprocess.run): pass\ngetattr(launch, '__def' + 'aults__')[0](['x'])\n",
            "format name": "import subprocess\ndef launch(runner=subprocess.run): pass\ngetattr(launch, '__{}__'.format('defaults'))[0](['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("import subprocess\nname = 'defaults'\nx = getattr(obj, name)\nsubprocess.run(['x'], creationflags=0x08000000)\n"), [])

    def test_pass19_a2_an_aliased_reflection_import_is_reported_by_what_it_imports(self):
        for label, src in {
            "typing": "from typing import get_type_hints as hints\nimport subprocess\ndef launch(runner: subprocess.run): pass\nhints(launch)['runner'](['x'])\n",
            "inspect signature": "from inspect import signature as sig\nimport subprocess\ndef launch(r=subprocess.run): pass\nsig(launch).parameters['r'].default(['x'])\n",
            "inspect getfullargspec": "from inspect import getfullargspec as spec\nimport subprocess\ndef launch(r=subprocess.run): pass\nspec(launch).defaults[0](['x'])\n",
            "inspect get_annotations": "from inspect import get_annotations as ann\nimport subprocess\ndef launch(r: subprocess.run): pass\nann(launch)['r'](['x'])\n",
        }.items():
            self.assertGreaterEqual(len(self._bad(src)), 1, label)
        self.assertEqual(self._bad("from os.path import join as hints\nimport subprocess\nhints('a', 'b')\nsubprocess.run(['x'], creationflags=0x08000000)\n"), [])

    def test_pass19_a3_a_receiver_segment_is_any_text_without_a_dot(self):
        for expr in ("'loop-instance.subprocess_exec'", "'loop instance.subprocess_exec'", "'loop-' + p + '.subprocess_exec'", "'my loop.' + p + '.subprocess_shell'",
                     "'a-b.' + p + '-c.' + q + '.subprocess_exec'", "'loop:inst.' + p + '.subprocess_exec'", "f'loop-{p}.subprocess_exec'"):
            src = ("import operator\nasync def launch(holder, p, q, factory):\n"
                   f"    return await operator.attrgetter({expr})(holder)(factory, 'cmd.exe')\n")
            self.assertGreaterEqual(len(self._bad(src)), 1, expr)
        for expr in ("'loop-instance.' + part", "'loop-' + part + '.run'", "'a b.' + p + '.value'", "'loop-instance.' + part + ' failed'"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)

    def test_pass19_a4_a_backtick_newline_inside_a_word_is_part_of_the_word(self):
        for ps in ("Start-Process cmd.exe -ArgumentList foo`\n-WindowStyle:Hidden", "Start-Process cmd.exe -ArgumentList foo`\r\n-WindowStyle Hidden",
                   "saps cmd.exe -ArgumentList a`\n-WindowStyle:Hidden", "start cmd.exe -ArgumentList /c`\n-WindowStyle Hidden"):
            self.assertEqual(self._ps(repr(ps)), (1, 1), ps)
        # a continuation after whitespace, or a word that ends before the next line, still lets the parameter count
        for ps in ("Start-Process cmd.exe -ArgumentList foo `\n-WindowStyle:Hidden", "Start-Process cmd.exe `\n  -WindowStyle Hidden", "Start-Process cmd.exe -ArgumentList foo`\n -WindowStyle Hidden"):
            self.assertEqual(self._ps(repr(ps)), (1, 0), ps)

    # ---- pass 20 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r8-15d5ded6) A1-A3, as classes ----
    def test_pass20_a1_quotes_spliced_into_a_launch_word_are_the_launch(self):
        for ps in ('Start-"Process" cmd.exe', "Start-'Process' cmd.exe", 'Sta"rt"-Process cmd.exe', "S'tart-Proc'ess cmd.exe", 's"aps" cmd.exe', "sa'ps' cmd.exe",
                   '"st"art cmd.exe', 'Start-"Process" cmd.exe -WindowStyle Visible'):
            self.assertEqual(_start_process_violations("script = " + repr(ps) + "\n"), (1, [1]), ps)
        self.assertEqual(_start_process_violations("script = " + repr('Start-"Process" cmd.exe -WindowStyle Hidden') + "\n"), (1, []))
        for ps in ("Get-Item 'a'b", "Write-Host 'hello' world", 'Get-Item "st"art.txt', "Get-Item 'sa'p.txt"):  # a quoted splice that is no launch word
            self.assertEqual(_start_process_violations("script = " + repr(ps) + "\n"), (0, []), ps)

    def test_pass20_a2_the_hidden_value_must_end_at_hidden(self):
        for ps in ("Start-Process cmd.exe -WindowStyle Hidden`,Minimized", "Start-Process cmd.exe -WindowStyle Hidden,Minimized", "Start-Process cmd.exe -WindowStyle 'Hidden'x",
                   "Start-Process cmd.exe -WindowStyle Hidden$x", "Start-Process cmd.exe -WindowStyle Hidden'x'", "Start-Process cmd.exe -WindowStyle:Hidden.x"):
            self.assertEqual(_start_process_violations("script = " + repr(ps) + "\n"), (1, [1]), ps)
        for ps in ("Start-Process cmd.exe -WindowStyle Hidden", "Start-Process cmd.exe -WindowStyle 'Hidden' -Wait", "(Start-Process cmd.exe -WindowStyle:Hidden)",
                   "Start-Process cmd.exe -WindowStyle Hidden; exit", "Start-Process cmd.exe -WindowStyle \"Hidden\"|Out-Null"):
            self.assertEqual(_start_process_violations("script = " + repr(ps) + "\n"), (1, []), ps)

    def test_pass20_a3_a_replace_count_beyond_a_c_ssize_t_does_not_crash_the_scan(self):
        for count in ("9223372036854775808", "99999999999999999999999", "-99999999999999999999999"):
            for src in (f"def unused():\n    return 'x'.replace('x', 'y', {count})\n", f"def unused():\n    return 'x'.replace('x', 'y', count={count})\n"):
                self._bad(src)
                _start_process_violations(src)
        self.assertEqual(_flatten(ast.parse("'xx'.replace('x', 'y', 99999999999999999999)", mode="eval").body, {}), ("yy", True))

    def test_f2_a_receiver_only_text_stays_clean(self):
        for expr in ("'allowed_roots.' + k", "'loop.' + part", "'loop.' + part + '.run'", "part + '.' + other", "'self.loop.' + part", "f'{a}.{b}'",
                     "'loop.' + part + '.value'", "p + '.zz.' + q", "'a.' + p + '.b.' + q + '.c'"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)


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


class _Unbind(ast.NodeTransformer):
    """``X.m(args)`` becomes ``str.m(X, args)``: every method call spelled the unbound way (a test helper; it only rewrites the test's own expressions)."""

    def visit_Call(self, node: ast.Call) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.func, ast.Attribute) and not (isinstance(node.func.value, ast.Name) and node.func.value.id in ("str", "operator", "asyncio", "loop")):
            return ast.Call(func=ast.Attribute(value=ast.Name(id="str", ctx=ast.Load()), attr=node.func.attr, ctx=ast.Load()),
                            args=[node.func.value, *node.args], keywords=node.keywords)
        return node


class AdversaryR9Tests(unittest.TestCase):
    """r9: Grok G15 / G16 / G17 / G18 (head 15d5ded6 / bdf30a13) and Dot 789-R8-F1.

    G15: an unbound ``str.m(s, ...)`` is ``s.m(...)``, ``operator.concat`` / ``__add__`` are ``+``, ``''.join(reversed(s))`` reads, and an unrecognised
    call hedges the text of every argument (not only a receiver). G16: the module docstring names the fold set. G17: the PowerShell prefilter unsplices
    after it unescapes. G18: a line continuation right after ``Hidden`` ends the value. R8-F1: an empty attribute segment is a segment.
    """

    SP = "Start-Process"

    _bad = staticmethod(AdversaryR8Tests._bad)
    _launch = staticmethod(AdversaryR8Tests._launch)
    _named = staticmethod(AdversaryR8Tests._named)
    _ps = staticmethod(AdversaryR8Tests._ps)

    @staticmethod
    def _unbound(expr: str) -> str:
        return ast.unparse(_Unbind().visit(ast.parse(expr, mode="eval").body))

    # ---- G15: Python side, the seven spellings Grok named ----
    def g15_python_rows(self) -> list[str]:
        return ["str.lower('SUBPROCESS_EXEC')", "str.removeprefix('zzsubprocess_exec', 'zz')", "str.__add__('subprocess_', 'exec')",
                "str.join('', ['subprocess_', 'exec'])", "str.format('{}exec', 'subprocess_')", "operator.concat('subprocess_', 'exec')",
                "''.join(reversed('cexe_ssecorpbus'))"]

    def test_g15_an_unbound_or_operator_spelling_of_a_spawn_method_name_is_reported(self):
        for expr in self.g15_python_rows():
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        # more spellings of the same class
        for expr in ("str.__radd__('exec', 'subprocess_')", "str.__mod__('%sexec', 'subprocess_')", "operator.mod('%sexec', 'subprocess_')", "operator.add('subprocess_', 'exec')",
                     "builtins.str.upper('subprocess_exec').lower()", "type('').lower('SUBPROCESS_EXEC')", "''.__class__.strip('zsubprocess_execz', 'z')",
                     "''.join(reversed(['exec', 'subprocess_']))", "''.join(reversed(('exec', 'subprocess_')))", "''.join(reversed('cexe_ssecorpbus'))[0:]",
                     "str.join('', reversed(['exec', 'subprocess_']))", "str.replace('zsubprocess_exec', 'z', '')", "str.partition('xsubprocess_exec', 'x')[2]"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)

    def test_g15_the_unbound_form_reads_exactly_as_the_bound_form(self):
        for bound, want in (("'SUBPROCESS_EXEC'.lower()", "subprocess_exec"), ("''.join(['subprocess_', 'exec'])", "subprocess_exec"),
                            ("'{}exec'.format('subprocess_')", "subprocess_exec"), ("'zzsubprocess_exec'.strip('z')", "subprocess_exec")):
            for expr in (bound, self._unbound(bound)):
                self.assertEqual(_flatten(ast.parse(expr, mode="eval").body, {}), (want, True), expr)
        for expr in ("str.__add__('subprocess_', 'exec')", "operator.concat('subprocess_', 'exec')", "''.join(reversed('cexe_ssecorpbus'))"):
            self.assertEqual(_flatten(ast.parse(expr, mode="eval").body, {}), ("subprocess_exec", True), expr)

    # ---- G15: an unrecognised call hedges its arguments ----
    def test_g15_an_unrecognised_call_keeps_the_text_of_its_arguments(self):
        for expr in ("ident('zzsubprocess_exec')", "ident('zz' + 'subprocess_exec', 1)", "ident(x='zz' + 'subprocess_exec')", "copy.copy('subprocess_' + 'exec')",
                     "str('subprocess_exec')", "ident(1, 'subprocess_shell')", "ident(*rest, last='xsubprocess_exec')", "ident(a, b, 'subprocess_exec', c)",
                     "''.join(list(reversed('cexe_ssecorpbus')))", "ident(ident2('zzsubprocess_exec'))"):
            text, known = _flatten(ast.parse(expr, mode="eval").body, {})
            self.assertFalse(known, expr)
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)

    def test_g15_hedging_every_argument_adds_no_false_positive_to_a_harmless_text(self):
        for expr in ("ident('value')", "ident('hello', 'world')", "ident(name)", "str.lower(name)", "str.join(', ', names)", "str.format('{}-{}', a, b)", "operator.concat(a, b)",
                     "str.removeprefix(name, 'x')", "''.join(reversed(name))", "''.join(reversed('abc'))", "str.strip('abc')", "ident('subprocess')", "ident('exec')",
                     "str.__add__('sub', 'process')", "operator.concat('exec', 'subprocess_')", "''.join(reversed('subprocess_'))", "str.upper('value')"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)
        # the consumer of a name is not the name: getattr / attrgetter / hasattr read their argument, they do not return it
        for expr in ("'subprocess_exec_' + 'x'", "'subprocess_exec_x'"):
            self.assertEqual(self._bad(self._launch(expr)), [], expr)
        self.assertGreaterEqual(len(self._bad(self._launch("'value_' + kind"))), 1)  # r15 (conservative flip): a name that is not a constant, in a getattr name position, is reported
        self.assertEqual(self._bad("import operator\nasync def f(o):\n    return hasattr(o, 'subprocess_exec_x') and operator.attrgetter('subprocess_exec_y')(o)\n"), [])
        for expr in ("str.lower('hello')", "str.join(' ', parts)", "ident('hello world')", "str.removeprefix('the end', 'the ')", "''.join(reversed('dlrow'))",
                     "operator.concat('a', 'b')", "str.__add__('foo', 'bar')"):
            self.assertEqual(self._ps(expr), (0, 0), expr)

    # ---- G15: PowerShell side ----
    def g15_ps_rows(self) -> list[str]:
        sp = self.SP
        return [f"str.removeprefix('zz{sp} cmd.exe', 'zz')", f"str.__add__('Start-', 'Process cmd.exe')", f"''.join(reversed({(sp + ' cmd.exe')[::-1]!r}))"]

    def test_g15_a_launch_behind_an_unbound_or_operator_fold_is_reported_by_the_powershell_side(self):
        for expr in self.g15_ps_rows():
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("operator.concat('Start-', 'Process cmd.exe')", "operator.add('sap', 's cmd.exe')", "str.lower('START-PROCESS cmd.exe')", "str.join('', ['Start-', 'Process cmd.exe'])",
                     "str.format('{}Process cmd.exe', 'Start-')", "''.join(reversed(['cmd.exe', ' Process-', 'Start']))[0:]", "''.__class__.strip('zStart-Process cmd.exez', 'z')"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        hidden = f"{self.SP} cmd.exe -WindowStyle Hidden"  # a fold that reads exactly is judged as written: a hidden launch stays clean
        for expr in (f"str.lstrip('zz{hidden}', 'z')", f"str.strip('zz{hidden}zz', 'z')", f"operator.concat('Start-', 'Process cmd.exe -WindowStyle Hidden')",
                     f"str.__add__('Start-', 'Process cmd.exe -WindowStyle Hidden')"):
            self.assertEqual(self._ps(expr), (1, 0), expr)

    def test_g15_every_string_mapper_and_builder_reads_unbound_the_way_it_reads_bound(self):
        """Walks every ``_STRING_MAPPERS`` method and every builder kind ``fold_kinds`` has, spelled bound and unbound, on both sides."""
        mappers = {}
        for m in sorted(_STRING_MAPPERS):
            for target in ("subprocess_exec", f"{self.SP} cmd.exe"):
                arg = "'z'" if m in ("strip", "lstrip", "rstrip") else ""
                text = ("zz" + target + "zz") if arg else target
                mappers[(m, target)] = f"{text!r}.{m}({arg})"
        kinds = {("kind", k, t): e for t in ("subprocess_exec", f"{self.SP} cmd.exe") for k, e in AdversaryR8Tests.fold_kinds(None, t).items()}
        changed = 0
        for key, bound in {**mappers, **kinds}.items():
            unbound = self._unbound(bound)
            changed += unbound != bound  # a kind with no method call in it (a literal, a ``+`` chain, ``%``) spells the same either way
            if unbound == bound:
                continue
            self.assertEqual(_flatten(ast.parse(unbound, mode="eval").body, {}), _flatten(ast.parse(bound, mode="eval").body, {}), (key, unbound))
            if key[-1] == "subprocess_exec":
                self.assertEqual(len(self._bad(self._launch(unbound))), len(self._bad(self._launch(bound))), (key, unbound))
                if key[0] in ("lower", "casefold", "strip") or (key[0] == "kind" and key[1] != "upper"):  # the ones whose bound form spells the name back exactly
                    self.assertGreaterEqual(len(self._bad(self._launch(unbound))), 1, (key, unbound))
            else:
                self.assertEqual(self._ps(unbound), self._ps(bound), (key, unbound))
                if key[0] not in ("lstrip", "rstrip"):  # those two strip only one side of the padding, so neither spelling reads as a launch
                    self.assertEqual(self._ps(unbound), (1, 1), (key, unbound))
        self.assertGreater(changed, 40)  # the walk really rewrote the methods

    # ---- G17: a backtick plus a quote splice ----
    def test_g17_a_backtick_and_a_quote_splice_in_a_launch_word_is_reported(self):
        for script in ("St`art-Pro'cess' cmd.exe", "Sta`rt-\"Process\" cmd.exe", "s`a'ps' cmd.exe", "`s'tart' cmd.exe", "Start-`P\"roc\"ess cmd.exe"):
            self.assertEqual(self._ps(repr(script)), (1, 1), script)
            self.assertEqual(_judge_script(script), (1, 1), script)  # the judge alone always read it; the prefilter in front of it did not
        self.assertEqual(self._ps(repr("St`art-Pro'cess' cmd.exe -WindowStyle Hidden")), (1, 0))  # a hidden one is still clean

    def test_g17_every_backtick_position_crossed_with_every_quote_splice_position(self):
        for word in (self.SP, "saps", "start"):
            seen = 0
            for j in range(0, len(word)):
                for k in range(j + 1, len(word) + 1):
                    for quote in ("'", '"'):
                        for i in range(1, len(word)):
                            if i in (j, k) or (j < i < k and quote == "'"):
                                continue  # a backtick next to a quote escapes the quote; in single quotes it is a plain character
                            if j < i < k and word[i] in "0abfnrtuv":
                                continue  # inside double quotes these are real escapes (`n is a newline)
                            spelled = word[:j] + quote + word[j:k] + quote + word[k:]
                            at = i + (1 if i > j else 0) + (1 if i > k else 0)
                            script = spelled[:at] + "`" + spelled[at:] + " cmd.exe"
                            self.assertEqual(self._ps(repr(script)), self._ps(repr(script.replace("`", ""))), script)  # as read without the backtick
                            self.assertEqual(self._ps(repr(script)), (1, 1), script)
                            seen += 1
            self.assertGreater(seen, 20, word)

    # ---- G18: a line continuation right after Hidden ----
    def test_g18_a_hidden_launch_that_continues_the_line_after_hidden_is_clean(self):
        for script in ("Start-Process cmd.exe -WindowStyle Hidden`\n -Wait", "Start-Process cmd.exe -WindowStyle Hidden`\r\n    -Wait", "saps cmd.exe -WindowStyle Hidden`\n -Wait",
                       "Start-Process cmd.exe -WindowStyle `\n Hidden -Wait", "Start-Process cmd.exe `\n -WindowStyle Hidden`\n -Wait", "start cmd.exe -WindowStyle:Hidden`\n -Wait",
                       "Start-Process cmd.exe -WindowStyle 'Hidden'`\n -Wait"):
            self.assertEqual(_judge_script(script), (1, 0), script)
            self.assertEqual(self._ps(repr(script)), (1, 0), script)

    def test_g18_the_value_still_has_to_end_at_hidden(self):
        for script in ("Start-Process cmd.exe -WindowStyle Hidden`\n-Wait", "Start-Process cmd.exe -WindowStyle Hidden#x", "Start-Process cmd.exe -WindowStyle Hidden]",
                       "Start-Process cmd.exe -WindowStyle Hid\"den\"", "Start-Process cmd.exe -WindowStyle \"Hidden\",x", "Start-Process cmd.exe -WindowStyle Hidden` -Wait",
                       "Start-Process cmd.exe -WindowStyle Hidden,Minimized`\n -Wait", "Start-Process cmd.exe -WindowStyle Hidden`x"):
            self.assertEqual(_judge_script(script), (1, 1), script)

    # ---- pass 22 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r9-d1f2aea1) A1-A3, as classes ----
    def test_pass22_a1_a_percent_field_the_fold_cannot_parse_never_cuts_the_literal_text_after_it(self):
        for expr in ("'%.ssubprocess_exec' % 'unused'", "'%.0ssubprocess_exec' % 'u'", "'%-.ssubprocess_exec' % 'u'",
                     "'%(a).ssubprocess_exec' % {'a': 'u'}", "'%.*ssubprocess_exec' % (0, 'u')", "'%.s%.ssubprocess_exec' % ('u', 'v')",
                     "'%.s' % 'x' + 'subprocess_exec'",
                     "('%.s' + 'subprocess_') % 'x' + 'exec'", "'%.ssubprocess_%.sexec' % ('a', 'b')", "'%.ssubprocess_%sexec' % ('a', '')"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        self.assertEqual(_flatten(ast.parse("'%.ssubprocess_exec' % 'unused'", mode="eval").body, {}), ("subprocess_exec", True))  # `%.s` is precision 0: nothing is printed
        for expr in ("'%.ssaps cmd.exe' % 'unused'", f"'%.s{self.SP} cmd.exe' % 'unused'", f"'%5.s{self.SP} cmd.exe' % 'u'", f"'%(a).s{self.SP} cmd.exe' % {{'a': 'u'}}"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("'%.s' % 'unused'", "'%.sx' % a", "'%s: %.3s' % (a, b)", "'%.s%.s' % (a, b)", "'%.s' % kind + 'value'", "'%zvalue' % 'u'"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)

    def test_pass22_a2_a_nested_quoted_script_is_judged_after_unescaping_and_unsplicing(self):
        outer = "Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-Command','{}'"
        for inner in ("Sta\"rt\"-Proce`ss cmd.exe", "s\"a\"`ps cmd.exe", "St`art-Pro\"cess\" cmd.exe", "Start-Process cmd.exe", "s`aps cmd.exe"):
            script = outer.format(inner)
            self.assertEqual(self._ps(repr(script)), (2, 1), inner)  # the hidden outer launch is fine; the inner one it hands to PowerShell is not
            self.assertEqual(_judge_script(script), (2, 1), inner)
            self.assertEqual(self._ps(repr(outer.format(inner + " -WindowStyle Hidden"))), (2, 0), inner)
        dq = 'Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-Command","Sta\'rt\'-Pr`ocess cmd.exe"'
        self.assertEqual(self._ps(repr(dq)), (2, 1))
        for inner in ("Write-Host hi", "Get-Date", "st'ar'tup"):  # a nested script that launches nothing adds nothing
            self.assertEqual(self._ps(repr(outer.format(inner))), (1, 0), inner)

    def test_pass22_a3_the_fold_does_not_depend_on_the_name_a_module_or_type_was_imported_under(self):
        aliases = {
            "operator as op": ("import operator as op\n", ["op.concat('subprocess_', 'exec')", "op.add('subprocess_', 'exec')", "op.mod('%sexec', 'subprocess_')",
                                                           "op.mul('subprocess_exec', 1)", "''.join(reversed(op.concat('cexe_', 'ssecorpbus')))"]),
            "from operator import concat": ("from operator import concat, add, mod\n", ["concat('subprocess_', 'exec')", "add('subprocess_', 'exec')", "mod('%sexec', 'subprocess_')"]),
            "builtins as b": ("import builtins as b\n", ["b.str.lower('SUBPROCESS_EXEC')", "b.str.removeprefix('zzsubprocess_exec', 'zz')", "b.str.format('{}exec', 'subprocess_')"]),
            "str alias": ("S = str\nT = type('')\n", ["S.lower('SUBPROCESS_EXEC')", "S.join('', ['subprocess_', 'exec'])", "T.lower('SUBPROCESS_EXEC')", "S.__add__('subprocess_', 'exec')"]),
            "method alias": ("low = str.lower\nrm = str.removeprefix\n", ["low('SUBPROCESS_EXEC')", "rm('zzsubprocess_exec', 'zz')"]),
            "reversed alias": ("rv = reversed\nfrom builtins import reversed as rev\n", ["''.join(rv('cexe_ssecorpbus'))", "''.join(rev('cexe_ssecorpbus'))"]),
            "functools": ("import functools\n", ["functools.reduce(op_add, ['subprocess_', 'exec'])", "ident('subprocess_', 'exec')", "ident('exec', 'subprocess_')"]),
        }
        for label, (prefix, exprs) in aliases.items():
            for expr in exprs:
                self.assertGreaterEqual(len(self._bad(prefix + self._launch(expr))), 1, (label, expr))
        for expr in ("op.concat('Start-', 'Process cmd.exe')", "type('').join('', ['sap', 's cmd.exe'])", "S.__add__('Start-', 'Process cmd.exe')", "add('sap', 's cmd.exe')", "mod('%saps cmd.exe', 's')"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("b.str.lower('START-PROCESS cmd.exe')", "b.str.removeprefix('zzStart-Process cmd.exe', 'zz')"):  # r15 (A3): `b` is the builtins module only where the file imports it
            self.assertEqual(_start_process_violations("import builtins as b\nx = " + expr + "\n")[0], 1, expr)
        # an INSTANCE method with a launch word as its argument is a call on something else: the fixture line `source.index("...Process]::Start(...)")`
        for expr in ("source.index('$p = [System.Diagnostics.Process]::Start($i)')", "text.find('start')", "text.count('Start-Process')", "log.format('saps')"):
            self.assertEqual(self._ps(expr)[1] <= self._ps(repr(expr.split("'")[1]))[1], True, expr)  # never worse than the argument text alone
        for prefix, expr in (("import operator as op\n", "op.concat(a, b)"), ("import operator as op\n", "op.concat('sub', 'process')"), ("S = str\n", "S.lower(name)"),
                             ("import builtins as b\n", "b.str.lower('hello')"), ("rv = reversed\n", "''.join(rv(name))"), ("", "ident('subprocess', 'exec')"),
                             ("import os\n", "os.path.join('subprocess_', 'x')"), ("", "ident('value_', 'x')"), ("", "ident(a, b)"),
                             ("tpl = 'abc{}'\n", "tpl.format('subprocess_')"), ("tpl = 'abc{}'\n", "tpl.replace('{}', 'subprocess_')"), ("", "'abc{}'.format('subprocess_')")):
            self.assertEqual(self._bad(prefix + self._named(expr)), [], (prefix, expr))
        # a name bound to a text keeps its bound meaning: `tpl.format(...)` fills tpl, it does not make the argument the template
        self.assertEqual(_flatten(ast.parse("tpl.format('subprocess_exec')", mode="eval").body, {"tpl": "abc{}"}), ("abcsubprocess_exec", True))
        self.assertEqual(self._ps("S.lower('hello world')"), (0, 0))
        self.assertEqual(self._ps("source.index('$p = [System.Diagnostics.Process]::Start($i)')"), (0, 0))  # the real fixture line (test_playback_host_load_gate.py)

    # ---- pass 23 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r9-7b06e18a) A1-A4, as classes ----
    def test_pass23_a1_a_string_method_through_an_alias_of_the_type_reads_its_arguments(self):
        for prefix, expr in (("S = str\n", "S.format('{}{}', 'subprocess_', 'exec')"), ("S = str\n", "S.format_map('{a}{b}', {'a': 'subprocess_', 'b': 'exec'})"),
                             ("from builtins import str as S\n", "S.format('{}{}', 'subprocess_', 'exec')"), ("T = type('')\n", "T.join('', ['subprocess_', 'exec'])"),
                             ("", "make().format('{}{}', 'subprocess_', 'exec')"), ("", "S.format('{}{}', 'subprocess_', 'exec')"),
                             ("S = str\n", "S.format('{}exec', 'subprocess_')"), ("S = str\n", "S.replace('zzsubprocess_exec', 'zz', '')")):
            for method in ("subprocess_exec", ):
                self.assertGreaterEqual(len(self._bad(prefix + self._launch(expr))), 1, (prefix, expr))
        for prefix, expr in (("S = str\n", "S.format('{}{}', 'sa', 'ps cmd.exe')"), ("S = str\n", "S.join('', ['sa', 'ps cmd.exe'])"), ("", "S.format('{}{}', 'sa', 'ps cmd.exe')")):
            found, bad = _start_process_violations(prefix + f"script = {expr}\n")
            self.assertEqual((found, len(bad)), (1, 1), (prefix, expr))
        for prefix, expr in (("S = str\n", "S.format('{}', a)"), ("S = str\n", "S.lower(name)"), ("", "log.format('{}-{}', 'a', 'b')")):
            self.assertEqual(self._bad(prefix + self._named(expr)), [], (prefix, expr))

    def test_pass23_a2_an_operator_function_with_a_mapping_or_tuple_operand_is_read(self):
        for prefix, expr in (("import operator as op\n", "op.mod('%(a)s%(b)s', {'a': 'subprocess_', 'b': 'exec'})"), ("import operator as op\n", "op.mod('%s%s', ('subprocess_', 'exec'))"),
                             ("", "ident.mod('%(a)s%(b)s', {'a': 'subprocess_', 'b': 'exec'})"), ("", "mod('%(a)s%(b)s', {'a': 'subprocess_', 'b': 'exec'})"),
                             ("S = str\n", "S.format_map('{a}{b}', {'a': 'subprocess_', 'b': 'exec'})"), ("import operator as op\n", "op.add(prefix, 'subprocess_exec')"),
                             ("import operator as op\n", "op.concat('subprocess_', suffix)")):
            self.assertGreaterEqual(len(self._bad(prefix + self._launch(expr))), 1, (prefix, expr))
        for expr in ("op.mod('%(a)s%(b)s', {'a': 'sa', 'b': 'ps cmd.exe'})", "op.mod('%s%s', ('sa', 'ps cmd.exe'))", "mod('%(a)s%(b)s', {'a': 'Start-', 'b': 'Process cmd.exe'})"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for prefix, expr in (("import operator as op\n", "op.mod('%(a)s%(b)s', {'a': 'sub', 'b': 'process'})"), ("import operator as op\n", "op.add(a, b)")):
            self.assertEqual(self._bad(prefix + self._named(expr)), [], (prefix, expr))

    def test_pass23_a3_bytes_fragments_and_an_explicit_codec_read_as_the_text_they_spell(self):
        for expr in ("(b'subprocess_' + b'exec').decode()", "b'subprocess_'.decode('ascii') + b'exec'.decode('ascii')", "(b'subprocess_' + b'exec').decode('utf-8')",
                     "'subprocess_exec'.encode().decode()", "(b'subprocess_' b'exec').decode()", "bytes.decode(b'subprocess_' + b'exec')", "(b'sub' + b'process_' + b'exec').decode('latin-1')"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        for expr in ("(b'Start-' + b'Process cmd.exe').decode()", "b'saps cmd.exe'.decode('ascii')", "(b'sa' + b'ps cmd.exe').decode('utf-8')"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("(b'value_' + b'x').decode()", "b'abc'.decode('ascii')", "(b'hello' + b' world').decode()"):
            self.assertEqual(self._bad(self._launch(expr)), [], expr)
            self.assertEqual(self._ps(expr), (0, 0), expr)

    def test_pass23_a4_a_template_with_an_unknown_part_keeps_the_literal_text_before_its_fields(self):
        for prefix, expr in (("import os\nprefix = os.environ.get('MLV_PREFIX', '')\n", "(prefix + 'subprocess_exec{tail}').format(tail='')"),
                             ("", "(prefix + 'subprocess_exec%(tail)s') % {'tail': ''}"), ("", "(prefix + 'subprocess_exec{}').format('')"), ("", "(prefix + 'subprocess_exec%s') % ''"),
                             ("", "(prefix + 'x{a}subprocess_exec{b}').format(a='', b='')"), ("", "(prefix + '%ssubprocess_exec%s') % ('', '')"),
                             ("", "(prefix + 'subprocess_exec{tail}').format_map({'tail': ''})"), ("", "(prefix + '{}subprocess_exec').format('')"),
                             ("", "(prefix + '%(k)ssubprocess_exec') % {'k': ''}")):
            self.assertGreaterEqual(len(self._bad(prefix + self._launch(expr))), 1, (prefix, expr))
        for expr in ("(prefix + 'saps cmd.exe{tail}').format(tail='')", "(prefix + 'saps cmd.exe%(tail)s') % {'tail': ''}", "(prefix + '{}saps cmd.exe').format('')"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("(prefix + 'value{tail}').format(tail='')", "(prefix + '{}x').format('')", "(prefix + '%s: %d') % (a, b)", "(prefix + '{} {}').format(a, b)"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)

    # ---- pass 24 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r9-d5971734) A1-A3, as classes ----
    def test_pass24_a1_a_codec_that_rewrites_characters_does_not_hide_a_name(self):
        for expr in ("br'subprocess\\x5fexec'.decode('unicode_escape')", "b'subprocess\\\\x5fexec'.decode('unicode_escape')", "br'subprocess\\u005fexec'.decode(encoding='unicode_escape')",
                     "br'subprocess\\137exec'.decode('unicode_escape')", "br'sub\\u0070rocess_exec'.decode('raw_unicode_escape')", "b'subprocess_exec'.decode('utf-16')",
                     "'subprocess\\\\x5fexec'.encode().decode('unicode_escape')"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        for expr in ("br'Sta\\x72t-Process cmd.exe'.decode('unicode_escape')", "br'saps cmd.exe'.decode('unicode_escape')", "br'sa\\u0070s cmd.exe'.decode('raw_unicode_escape')"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("b'value\\x5fx'.decode('unicode_escape')", "b'abc'.decode('ascii')"):
            self.assertEqual(self._bad(self._launch(expr)), [], expr)

    def test_pass24_a2_bytes_are_cut_where_python_cuts_them(self):
        for expr in ("b'\\xc3\\xa9subprocess_exec'[2:].decode()", "b'\\xc3\\xa9subprocess_exec'[2:].decode('utf-8')", "b'\\xffsubprocess_exec'[1:].decode()", "b'\\xff\\xfesubprocess_exec'[2:].decode('latin-1')",
                     "(b'\\xc3\\xa9subprocess_exec%s' % b'')[2:].decode()", "(b'\\xc3\\xa9{}subprocess_exec').replace(b'{}', b'')[2:].decode()"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        self.assertEqual(_flatten(ast.parse("b'\\xc3\\xa9subprocess_exec'[2:].decode('latin-1')", mode="eval").body, {}), ("subprocess_exec", True))
        self.assertEqual(self._ps("b'\\xc3\\xa9Start-Process cmd.exe'[2:].decode()"), (1, 1))
        for expr in ("b'\\xc3\\xa9value'[2:].decode()", "b'abc'[1:].decode()"):
            self.assertEqual(self._bad(self._launch(expr)), [], expr)

    def test_pass24_a3_a_backtick_at_the_end_of_a_comment_is_not_a_continuation(self):
        for script in ("# comment `\nStart-Process cmd.exe", "# c `\r\nStart-Process cmd.exe", "<# block #>\n# note `\nsaps cmd.exe", "$x = 1 # trailing `\nStart-Process cmd.exe",
                       "# a `\n# b `\nStart-Process cmd.exe"):
            self.assertEqual(_judge_script(script), (1, 1), script)
            self.assertEqual(self._ps(repr(script)), (1, 1), script)
        for script in ("# comment `\nStart-Process cmd.exe -WindowStyle Hidden", "Write-Host 'a #b `'\nStart-Process cmd.exe -WindowStyle Hidden"):
            self.assertEqual(_judge_script(script), (1, 0), script)
        # a real continuation still joins: the comment character is inside the next line, which is a continuation of this one
        self.assertEqual(_judge_script("Start-Process cmd.exe `\n -WindowStyle Hidden"), (1, 0))
        self.assertEqual(_judge_script("Start-Process cmd.exe -WindowStyle Hidden `\n -Wait # done"), (1, 0))

    # ---- G16: the docstring names the fold set, not an example of an unread form ----
    def test_g16_the_powershell_limit_points_at_the_fold_set_and_does_not_call_chr_unread(self):
        doc = " ".join((__doc__ or "").split())
        self.assertIn("A launch is read through the fold set of ``_flatten``", doc)
        self.assertNotIn("(a file read at run time, ``chr()``) is not read", doc)
        self.assertNotIn("``.join`` (a file read at run time, ``chr()``)", doc)
        self.assertIn("an UNRECOGNISED function returns over its arguments", doc)  # the part of the G15 class that stays unread on the PowerShell side
        # and the claim holds: chr() is folded on the PowerShell side
        self.assertEqual(self._ps("chr(83) + 'tart-Process cmd.exe'"), (1, 1))

    # ---- R8-F1: an empty attribute segment ----
    def test_r8_f1_a_leading_or_internal_empty_segment_does_not_hide_a_spawn_method_name(self):
        for expr in ("'.subprocess_exec'", "'.subprocess_' + kind", "'.' + 'subprocess_' + kind", "'..subprocess_exec'", "'loop..subprocess_exec'", "'a..b.subprocess_shell'",
                     "'.' + p + '..subprocess_exec'", "f'.{p}.subprocess_exec'", "'.subprocess_%s' % kind", "'.{}'.format('subprocess_' + kind)", "'.' + p + '.subprocess_exec'",
                     "'x.' + p + '..subprocess_shell'", "'..' + p + '.' + q + '.subprocess_exec'", "'.sub' + 'process_' + 'exec'", "p + '..subprocess_' + q"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        witness = ("import operator\nasync def launch(holder, factory, kind):\n"
                   "    return await operator.attrgetter('.subprocess_' + kind)(holder)(factory, 'cmd.exe')\n")
        self.assertGreaterEqual(len(self._bad(witness)), 1)
        exact = "import operator\nasync def launch(holder, factory):\n    return await operator.attrgetter('.subprocess_exec')(holder)(factory, 'cmd.exe')\n"
        self.assertGreaterEqual(len(self._bad(exact)), 1)

    def test_r8_f1_a_receiver_only_text_with_empty_segments_stays_clean(self):
        for expr in ("'.value_' + kind", "'..'", "'.' + x", "'.subprocess'", "'.run'", "'a..b'", "'.allowed_roots.' + k", "'. ' + x", "'..' + x + '.value'", "'.' + p + '..' + q",
                     "'.loop.' + p", "f'.{p}.{q}'", "'.exec' + kind", "'..exec_' + kind"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)


    # ---- Dot R9-F1: a codec / error-handling call reads as Python reads it, or as unknown ----
    # (decode errors mode, how the spelling restores the name afterwards) for every transforming error mode a decode or an encode can take
    _R9_DECODE_MODES = (("ignore", ""), ("replace", ".replace('\\ufffd', '')"), ("backslashreplace", ".replace('\\\\xff', '')"), ("surrogateescape", ".replace('\\udcff', '')"))
    _R9_ENCODE_MODES = (("ignore", ""), ("replace", ".replace(b'?', b'')"), ("backslashreplace", ".replace(b'\\\\xe9', b'')"),
                        ("xmlcharrefreplace", ".replace(b'&#233;', b'')"), ("namereplace", ".replace(b'\\\\N{LATIN SMALL LETTER E WITH ACUTE}', b'')"))

    @classmethod
    def _r9_rows(cls, target: str) -> list[str]:
        """Every error mode crossed with a non-ASCII byte (or character) at every position of ``target``; each reads as ``target`` once Python has applied it."""
        rows = []
        for at in range(len(target) + 1):
            data = target[:at].encode() + b"\xff" + target[at:].encode()
            text = target[:at] + "é" + target[at:]
            for mode, post in cls._R9_DECODE_MODES:
                enc = "ascii" if mode != "surrogateescape" else "utf-8"
                rows.append(f"{data!r}.decode({enc!r}, {mode!r}){post}")
                rows.append(f"{data!r}.decode({enc!r}, errors={mode!r}){post}")
            rows.append(f"{data!r}.decode(encoding='ascii', errors='ignore')")
            for mode, post in cls._R9_ENCODE_MODES:
                rows.append(f"{text!r}.encode('ascii', {mode!r}){post}.decode()")
                rows.append(f"{text!r}.encode(encoding='ascii', errors={mode!r}){post}.decode('ascii')")
        return rows

    def test_r9_f1_the_dot_witness_is_reported(self):
        witness = ("import asyncio\nasync def launch(factory):\n    loop = asyncio.get_running_loop()\n"
                   "    return await getattr(loop, b'subprocess_\\xffexec'.decode('ascii','ignore'))(factory, 'cmd.exe')\n")
        self.assertGreaterEqual(len(self._bad(witness)), 1)
        self.assertEqual(self._bad(witness.replace("'ignore'", "'replace'")), [])  # the replacement character stays: that text is not a method name

    def test_r9_f1_every_error_mode_at_every_position_of_a_method_name_is_reported(self):
        for target in ("subprocess_exec", "subprocess_shell", "_make_subprocess_transport"):
            for expr in self._r9_rows(target):
                self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
                text, known = _flatten(ast.parse(expr, mode="eval").body, {})
                self.assertEqual(text, target, expr)  # an exact codec result is exact, not a wildcard
                self.assertTrue(known, expr)

    def test_r9_f1_the_same_rows_on_a_create_subprocess_name_fold_to_that_name(self):
        for expr in self._r9_rows("create_subprocess_exec"):
            text, known = _flatten(ast.parse(expr, mode="eval").body, {})
            self.assertEqual((text, known), ("create_subprocess_exec", True), expr)
            source = f"import asyncio\nasync def launch():\n    await getattr(asyncio, {expr})('cmd.exe')\n"
            self.assertGreaterEqual(len(self._bad(source)), 1, expr)

    def test_r9_f1_a_non_ascii_byte_inside_each_launch_word_is_read_as_python_reads_it(self):
        for launch in ("Start-Process cmd.exe", "saps cmd.exe"):
            for at in range(len(launch) + 1):
                data = launch[:at].encode() + b"\xff" + launch[at:].encode()
                text = launch[:at] + "é" + launch[at:]
                for expr in (f"{data!r}.decode('ascii', 'ignore')", f"{data!r}.decode('ascii', errors='ignore')",
                             f"{data!r}.decode('ascii', 'replace').replace('\\ufffd', '')", f"{data!r}.decode('utf-8', 'surrogateescape').replace('\\udcff', '')",
                             f"{text!r}.encode('ascii', 'ignore').decode()",
                             f"{text!r}.encode('ascii', 'xmlcharrefreplace').replace(b'&#233;', b'').decode()"):
                    self.assertEqual(self._ps(expr), (1, 1), expr)

    def test_r9_f1_a_codec_that_maps_to_a_method_name_is_read(self):
        for expr in ("'subprocess_exec'.encode('utf-16').decode('utf-16')", "'subprocess_exec'.encode('utf-16-le').decode('utf-16-le')", "'subprocess_exec'.encode('utf-32').decode('utf-32')",
                     "'subprocess_exec'.encode('utf-16-be').decode('utf-16-be', 'strict')", "'subprocess_exec'.encode('latin-1').decode('latin-1', 'ignore')",
                     "'subprocess_\\u00ffexec'.encode('latin-1').decode('ascii', 'ignore')", "'subprocess_\\u0100exec'.encode('latin-1', 'ignore').decode('latin-1')",
                     "b'subprocess_\\xffexec'.decode('utf-8', 'ignore')",
                     "b'\\xff\\xfes\\x00u\\x00b\\x00p\\x00r\\x00o\\x00c\\x00e\\x00s\\x00s\\x00_\\x00e\\x00x\\x00e\\x00c\\x00'.decode('utf-16')",
                     "'subprocess_exec'.encode('cp1252').decode('cp1252')", "'subprocess_\\u00e9exec'.encode('utf-8').decode('ascii', 'ignore')",
                     "str(b'subprocess_\\xffexec', 'ascii', 'ignore')", "str(b'subprocess_\\xffexec', encoding='ascii', errors='ignore')",
                     "bytes('subprocess_\\u00e9exec', 'ascii', 'ignore').decode()",
                     "bytes.decode(b'subprocess_\\xffexec', 'ascii', 'ignore')", "codecs.decode(b'subprocess_\\xffexec', 'ascii', 'ignore')",
                     "codecs.encode('subprocess_\\u00e9exec', 'ascii', 'ignore').decode()", "(b'subprocess_' + b'\\xffexec').decode('ascii', 'ignore')",
                     "(b'sub' + b'\\xff' + b'process_exec').decode('ascii', errors='ignore')", "b'subprocess_\\xffexec'.decode('ascii', ('ig' + 'nore'))"):
            self.assertGreaterEqual(len(self._bad("import codecs\n" + self._launch(expr))), 1, expr)
        for expr in ("b'Sta\\xffrt-Process cmd.exe'.decode('ascii', 'ignore')", "str(b'sa\\xffps cmd.exe', 'ascii', 'ignore')", "'saps cmd.exe'.encode('utf-16').decode('utf-16')",
                     "codecs.decode(b'Start-\\xffProcess cmd.exe', 'ascii', 'ignore')", "bytes('sa\\u00e9ps cmd.exe', 'ascii', 'ignore').decode()"):
            self.assertEqual(self._ps(expr), (1, 1), expr)

    def test_r9_f1_a_codec_or_error_mode_the_fold_does_not_model_is_unknown_never_a_literal(self):
        for expr in ("b'subprocess_\\xffexec'.decode('ascii', handler)", "b'subprocess_\\xffexec'.decode('ascii', 'custom_handler')", "b'subprocess_\\xffexec'.decode(enc, 'ignore')",
                     "b'subprocess_\\xffexec'.decode('rot13_text', 'ignore')", "b'subprocess_\\xffexec'.decode(*args)", "b'subprocess_\\xffexec'.decode('ascii', **kw)",
                     "'subprocess_exec'.encode('idna')", "'subprocess_exec'.encode('no-such-codec').decode('no-such-codec')", "'subprocess_exec'.encode(enc).decode(enc)",
                     "b'subprocess_exec'.decode('utf-16')", "b'subprocess_exec\\xff'.decode('ascii')", "b'\\xffsubprocess_exec'.decode()"):
            text, known = _flatten(ast.parse(expr, mode="eval").body, {})
            self.assertTrue(known or _WILD in text, (expr, text))  # the invariant: a text without the wildcard is exact
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)

    def test_r9_f1_the_fold_equals_python_where_it_claims_to_know(self):
        for data in (b"subprocess_exec", b"sub\xffprocess_exec", b"\xc3\xa9x", b"ab\xe9\xff", b"\xfe\xffa\x00b\x00", b"a\x80b"):
            for enc in ("ascii", "utf-8", "latin-1", "utf-16", "utf-16-le", "utf-32", "cp1252", "unicode_escape", "raw_unicode_escape", "utf-8-sig"):
                for err in (None, "strict", "ignore", "replace", "backslashreplace", "surrogateescape"):
                    call = f"{data!r}.decode({enc!r}" + (f", {err!r})" if err else ")")
                    try:
                        real = eval(call)
                    except (UnicodeError, LookupError):
                        real = None
                    text, known = _flatten(ast.parse(call, mode="eval").body, {})
                    if known and _WILD not in text:
                        self.assertEqual(text, real, call)
                    elif not known:
                        self.assertIn(_WILD, text, call)
        for text_in in ("subprocess_exec", "subéprocess_exec", "xĀy", "a\U0001f600b"):
            for enc in ("ascii", "utf-8", "latin-1", "utf-16", "utf-32", "cp1252"):
                for err in (None, "strict", "ignore", "replace", "backslashreplace", "xmlcharrefreplace", "namereplace"):
                    call = f"{text_in!r}.encode({enc!r}" + (f", {err!r})" if err else ")")
                    try:
                        real = eval(call).decode("latin-1")
                    except (UnicodeError, LookupError):
                        real = None
                    text, known = _flatten(ast.parse(call, mode="eval").body, {})
                    if known and _WILD not in text:
                        self.assertEqual(text, real, call)
                    elif not known:
                        self.assertIn(_WILD, text, call)

    def test_r9_f1_the_docstring_names_the_codec_fold_and_what_it_leaves_unread(self):
        doc = " ".join((__doc__ or "").split())
        self.assertIn("is run by Python itself on the literal (R9-F1)", doc)
        self.assertIn("reads as UNKNOWN (every non-ASCII character is any text and the text is padded with a wildcard), never as a literal", doc)
        self.assertIn("A NUL character in a folded text is the wildcard", doc)
        self.assertIn("reads as every spawn-method name and as a launch word, so it is REPORTED (pass-25 A2)", doc)
        self.assertIn("What stays unread is a codec reached through a function the fold does not follow", doc)

    def test_r9_f1_clean_controls_stay_clean(self):
        for expr in ("b'value_\\xffx'.decode('ascii', 'ignore')", "b'subprocess_\\xffvalue'.decode('ascii', 'ignore')", "b'subprocess_\\xffexec'.decode('ascii', 'replace')",
                     "b'subprocess_\\xffexec'.decode('ascii', 'backslashreplace')", "'subprocess_\\u00e9exec'.encode('ascii', 'replace').decode()",
                     "'subprocess_exe'.encode('latin-1').decode('latin-1')", "b'abc'.decode('ascii', 'replace')", "(b'hello' + b' world').decode('ascii', 'strict')",
                     "'valu\\u00e9'.encode('ascii', 'ignore').decode()", "str(b'value_\\xffx', 'ascii', 'ignore')", "codecs.decode(b'value_x', 'ascii', 'ignore')",
                     "'.' + b'value_\\xffx'.decode('ascii', 'ignore')", "b'abc'.decode('utf-16', 'ignore')", "'loop.' + 'abc'.encode('latin-1').decode('latin-1')",
                     "br'sub\\x70rocess_exec'.decode('raw_unicode_escape')"):  # the last: raw_unicode_escape does not read \x70, so Python keeps it as text and it names no method
            self.assertEqual(self._bad("import codecs\n" + self._launch(expr)), [], expr)
            self.assertEqual(self._ps(expr), (0, 0), expr)


    # ---- pass 25 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r10-0f30bb2d) A1-A3, as classes ----
    def test_pass25_a1_a_bytes_method_is_not_read_as_the_str_method_on_a_latin1_character(self):
        # bytes.upper / swapcase / capitalize / strip leave a byte above 0x7f alone; str maps or strips the same character (sharp s -> SS, no-break space)
        for expr in ("b'\\xdfsubprocess_exec'.upper()[1:].decode('ascii').lower()", "b'\\xdfSUBPROCESS_EXEC'.swapcase()[1:].decode('ascii')",
                     "b'\\xdfsubprocess_exec'.lower()[1:].decode('ascii')", "b'\\xdfsubprocess_exec'.capitalize()[1:].decode('ascii')",
                     "b'\\xa0subprocess_exec'.strip()[1:].decode('ascii')", "b'\\xa0subprocess_exec'.lstrip()[1:].decode('ascii')",
                     "b'subprocess_exec\\xa0'.rstrip()[:-1].decode('ascii')", "b'\\x85subprocess_shell'.strip()[1:].decode('ascii')",
                     "b'\\xdf_make_subprocess_transport'.upper()[1:].decode('ascii').lower()", "b'\\xdfsubprocess_shell'.casefold()[1:].decode('ascii')"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        for expr in ("b'\\xdfsaps cmd.exe'.upper()[1:].decode('ascii').lower()", "b'\\xa0saps cmd.exe'.strip()[1:].decode('ascii')",
                     "b'\\xdfSAPS CMD.EXE'.swapcase()[1:].decode('ascii')", "b'\\xa0Start-Process cmd.exe'.lstrip()[1:].decode('ascii')"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("b'\\xdfvalue'.upper()[1:].decode('ascii').lower()", "'\\u00dfvalue'.upper()", "b'\\xa0value_x'.strip()[1:].decode('ascii')", "'\\u00dfsubprocess_exe'.upper().lower()"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)
            self.assertEqual(self._ps(expr), (0, 0), expr)

    def test_pass25_a2_a_codec_the_fold_does_not_read_may_rewrite_every_letter_of_a_literal(self):
        for prefix, expr in (("import codecs\n", "codecs.decode('fhocebprff_rkrp', 'rot_13')"), ("import codecs\n", "codecs.decode('fhocebprff_rkrp', 'rot13')"),
                             ("import codecs\n", "codecs.decode('fhocebprff_funyy', encoding='rot_13')"), ("import codecs\n", "codecs.decode('_znxr_fhocebprff_genafcbeg', 'rot-13')"),
                             ("import codecs\n", "codecs.decode(b'c3VicHJvY2Vzc19leGVj', 'base64').decode('ascii')"), ("import codecs\n", "codecs.decode(b'7375627072', 'hex').decode()"),
                             ("import codecs as c\n", "c.decode('fhocebprff_rkrp', 'rot_13')"), ("from codecs import decode as d\n", "d('fhocebprff_rkrp', 'rot_13')"),
                             ("from codecs import decode\n", "decode(b'c3VicHJvY2Vzc19leGVj', 'base64').decode()"), ("import codecs\n", "codecs.decode('fhocebprff_rkrp', *args)"),
                             ("", "b'c3VicHJvY2Vzc19leGVj'.decode(enc)"), ("", "'fhocebprff_rkrp'.encode(codec_name)"), ("", "b'x'.decode('zlib_codec')"),
                             ("import codecs\n", "codecs.encode('subprocess_exec', 'rot_13').lower()")):
            self.assertGreaterEqual(len(self._bad(prefix + self._launch(expr))), 1, (prefix, expr))
            text, known = _flatten(ast.parse(expr, mode="eval").body, {})
            self.assertTrue(known or _WILD in text, expr)
        for expr in ("codecs.decode('Fgneg-Cebprff pzq.rkr', 'rot_13')", "codecs.decode(b'U3RhcnQtUHJvY2VzcyBjbWQuZXhl', 'base64').decode()", "codecs.decode('fnCF pzq.rkr', 'rot13')"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for prefix, expr in (("import codecs\n", "codecs.decode(data, 'rot_13')"), ("", "payload.decode(enc)"), ("import codecs\n", "codecs.decode(b'abc', 'ascii')"),
                             ("import codecs\n", "codecs.decode(read_bytes(), 'utf-8', 'replace')"), ("", "b''.decode(enc)"), ("", "'\\x00'.encode(enc)")):
            self.assertEqual(self._bad(prefix + self._named(expr)), [], (prefix, expr))
            self.assertEqual(self._ps(expr), (0, 0), expr)

    def test_pass25_a3_a_powershell_comment_is_a_comment_wherever_a_token_can_start(self):
        for script in ("Start-Process cmd.exe -ArgumentList '/c'# -WindowStyle Hidden", "Start-Process cmd.exe -ArgumentList ('/c')# -WindowStyle Hidden",
                       "Start-Process cmd.exe -ArgumentList @('/c')# -WindowStyle Hidden", "Start-Process cmd.exe -ArgumentList \"/c\"# -WindowStyle Hidden",
                       "Start-Process cmd.exe -ArgumentList $a[0]# -WindowStyle Hidden", "Start-Process cmd.exe -ArgumentList '/c'# it's -WindowStyle Hidden",
                       "saps cmd.exe -ArgumentList '/c'#-WindowStyle Hidden", "Start-Process cmd.exe -ArgumentList '/c'# -WindowStyle Hidden `\n -Wait",
                       "Start-Process cmd.exe -ArgumentList (1)# `\nsaps cmd.exe -WindowStyle Hidden"):
            self.assertEqual(_judge_script(script)[0], 1 + script.count("\nsaps"), script)
            self.assertGreaterEqual(_judge_script(script)[1], 1, script)
            self.assertGreaterEqual(self._ps(repr(script))[1], 1, script)
        for script in ("Start-Process cmd.exe -ArgumentList '/c' -WindowStyle Hidden # note", "Start-Process cmd.exe -ArgumentList a#b -WindowStyle Hidden",
                       "Start-Process cmd.exe -ArgumentList '/c' -WindowStyle Hidden;# done", "Start-Process cmd.exe -WindowStyle Hidden -ArgumentList '/c'# ok"):
            self.assertEqual(_judge_script(script), (1, 0), script)

    # ---- pass 26 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r10-7eda314f) A1-A2, as classes ----
    def test_pass26_a1_printing_a_bytes_value_shows_its_repr_not_its_characters(self):
        for expr in ("f\"{b'xxsubprocess_exec'}\"[4:-1]", "f\"{b'xxsubprocess_exec'!s}\"[4:-1]", "'{}'.format(b'xxsubprocess_exec')[4:-1]", "'{x}'.format(x=b'xxsubprocess_exec')[4:-1]",
                     "'{x}'.format_map({'x': b'xxsubprocess_exec'})[4:-1]", "('%s' % b'xxsubprocess_exec')[4:-1]", "('%s' % (b'xxsubprocess_exec',))[4:-1]",
                     "('%(x)s' % {'x': b'xxsubprocess_exec'})[4:-1]", "('%s' % (b'xx' + b'subprocess_exec'))[4:-1]", "f\"{b'xxsubprocess_shell'}\"[4:-1]",
                     "f\"{b'xx_make_subprocess_transport'}\"[4:-1]", "'{!r}'.format(b'xxsubprocess_exec')[4:-1]", "'{!s}'.format(b'xxsubprocess_exec')[4:-1]",
                     "('%r' % b'xxsubprocess_exec')[4:-1]", "('%a' % b'xxsubprocess_exec')[4:-1]", "f\"{b'xxsubprocess_exec'!r}\"[4:-1]", "f\"{b'xxsubprocess_exec'!a}\"[4:-1]",
                     "f\"{b'xx'}{b'subprocess_exec'}\"[7:-1]"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
            text, known = _flatten(ast.parse(expr, mode="eval").body, {})
            self.assertTrue(known or _WILD in text, expr)
        for expr in ("f\"{b'xxsaps cmd.exe'}\"[4:-1]", "('%s' % b'xxStart-Process cmd.exe')[4:-1]", "'{}'.format(b'xxsaps cmd.exe')[4:-1]"):
            self.assertEqual(self._ps(expr), (1, 1), expr)
        for expr in ("f\"{b'abc'}\"[4:-1]", "'{}'.format(b'value')", "'%s' % b'value_x'", "f\"{b'subprocess_exe'.decode()}\"", "'{}'.format(b'subprocess_exe'.decode())",
                     "f\"{b'xxvalue'}\"[4:-1]"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)
            self.assertEqual(self._ps(expr), (0, 0), expr)
        self.assertEqual(_flatten(ast.parse("f\"{b'subprocess_exec'.decode()}\"", mode="eval").body, {}), ("subprocess_exec", True))  # a decoded text prints as it is

    def test_pass26_a2_an_escaped_hash_is_a_character_not_a_comment(self):
        for script in ("Write-Host `#; Start-Process cmd.exe", "Write-Host `#; saps cmd.exe", "Write-Host `<#; Start-Process cmd.exe", "Write-Host a `#b; Start-Process cmd.exe",
                       "Write-Host `#\nStart-Process cmd.exe", "Write-Host `<#\nsaps cmd.exe", "Write-Host `#`#; Start-Process cmd.exe", "Write-Host `#' it'; Start-Process cmd.exe"):
            self.assertEqual(_judge_script(script), (1, 1), script)
            self.assertEqual(self._ps(repr(script)), (1, 1), script)
        for script in ("Write-Host `#; Start-Process cmd.exe -WindowStyle Hidden", "Write-Host a#b; Start-Process cmd.exe -WindowStyle Hidden",
                       "Write-Host `<#; saps cmd.exe -WindowStyle Hidden"):
            self.assertEqual(_judge_script(script), (1, 0), script)
        # a real comment still ends the line, and an escaped hash inside it changes nothing
        for script in ("Write-Host x # `#; Start-Process cmd.exe", "Write-Host x <# `#; Start-Process cmd.exe #>", "Write-Host x"):
            self.assertEqual(_judge_script(script), (0, 0), script)

    # ---- pass 27 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r10-f770c8e2) A1, as a class ----
    def test_pass27_a1_the_head_after_a_wildcard_is_also_plain_text_when_it_looks_like_a_field_tail(self):
        prefix = "import os\nprefix = os.environ.get('MLV_PREFIX', '')\n"
        rows = ["((prefix + 'subprocess_exec)s') % ())[:-2]", "((prefix + 'subprocess_exec}}s').format())[:-2]", "((prefix + 'subprocess_exec}}s').format_map({}))[:-2]",
                "((prefix + 'subprocess_shell)s') % {})[:-2]", "((prefix + '_make_subprocess_transport}}s').format())[:-2]", "((prefix + 'subprocess_exec)ds') % ())[:-3]",
                "((prefix + 'subprocess_exec}x}}').format_map({}))[:-4]", "((prefix + '%(k)ssubprocess_exec)s') % {'k': ''})[:-2]"]
        for expr in rows:
            self.assertGreaterEqual(len(self._bad(prefix + self._launch(expr))), 1, expr)
        for expr in ("(prefix + 'value)s') % ()", "(prefix + 'value}}s').format()", "(prefix + 'subprocess_exe)s') % ()", "(prefix + 'allowed_roots}}s').format_map({})"):
            self.assertEqual(self._bad(prefix + self._named(expr)), [], expr)
        for script in ("(prefix + 'saps cmd.exe)s') % ()", "(prefix + 'Start-Process cmd.exe}}s').format()"):
            self.assertEqual(self._ps(script), (1, 1), script)

    # ---- r9 pass-24 A4 / r10 pass-27 A2: an expression nested deeper than the fold reads is reported, never a crash ----
    def test_r10_a_deeply_nested_expression_is_reported_not_a_crash(self):
        for body in ("'SUBPROCESS_EXEC'", "'x'", "'subprocess_exec'", "'xsubprocess_exec'"):
            for depth, suffix in ((500, ".lower()"), (1000, ".strip()"), (3000, ".lower()")):
                expr = body + suffix * depth
                try:
                    reported = self._bad(self._launch(expr))
                except RecursionError:
                    self.fail(f"RecursionError for {body} + {suffix!r} * {depth}")
                self.assertGreaterEqual(len(reported), 1, (body, suffix, depth))
        found, bad = _start_process_violations("value = 'x'" + ".strip()" * 1000 + "\n")
        self.assertEqual((found, len(bad)), (1, 1))
        # a source of ordinary depth is read as before
        self.assertEqual(self._bad(self._launch("'SUBPROCESS_EXEC'.lower().strip().lower()")) != [], True)
        self.assertEqual(self._bad(self._launch("'value'.lower().strip()")), [])
        self.assertEqual(_start_process_violations("value = 'x'.strip()\n"), (0, []))

    # ---- pass 28 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r11-bddda880) ----
    def test_pass28_a1_a_qualified_or_aliased_str_bytes_constructor_with_a_codec_reads_as_the_decode(self):
        pre = "import builtins\nfrom builtins import str as T\nS = str\n"
        rows = ["builtins.str(b'subprocess_ex', 'ascii') + 'ec'", "T(b'subprocess_ex', 'ascii') + 'ec'", "S(b'subprocess_ex', 'ascii') + 'ec'",
                "type('')(b'subprocess_ex', 'ascii') + 'ec'", "''.__class__(b'subprocess_ex', 'ascii') + 'ec'", "builtins.str(b'subprocess_ex', encoding='ascii') + 'ec'",
                "builtins.bytes('subprocess_ex', 'ascii').decode() + 'ec'", "builtins.str(b'subprocess_exec', 'ascii')"]
        for expr in rows:
            self.assertGreaterEqual(len(self._bad(pre + self._launch(expr))), 1, expr)
        for script in ("builtins.str(b'Start-Process cmd.exe', 'ascii')", "type('')(b'saps cmd.exe', 'ascii')", "''.__class__(b'Start-Process cmd.exe', encoding='ascii')"):
            self.assertEqual(self._ps(script)[0], 1, script)
            self.assertEqual(self._ps(script)[1], 1, script)
        self.assertEqual(self._bad(pre + self._launch("builtins.str(b'value', 'ascii') + 'ec'")), [])

    def test_pass28_a2_a_bare_cr_or_unicode_newline_ends_a_powershell_comment(self):
        for newline in ("\r",):  # checked against Parser.ParseInput: U+0085 / U+2028 / U+2029 do NOT end a comment, only CR, LF and CRLF do
            for launch in ("Start-Process cmd.exe", "saps cmd.exe", "start cmd.exe"):
                script = f"Write-Host x # comment{newline}{launch}"
                self.assertEqual(_judge_script(script), (1, 1), (newline, launch))
                self.assertEqual(self._ps(repr(script)), (1, 1), (newline, launch))
            script = f"Write-Host x # comment{newline}Start-Process cmd.exe -WindowStyle Hidden"
            self.assertEqual(_judge_script(script), (1, 0), newline)
        for script in ("Write-Host x # comment\nStart-Process cmd.exe", "Write-Host x # comment\r\nStart-Process cmd.exe"):
            self.assertEqual(_judge_script(script), (1, 1), script)
        self.assertEqual(_judge_script("Write-Host x # Start-Process cmd.exe"), (0, 0))

    def test_pass28_a3_an_escaped_backtick_before_a_newline_does_not_join_two_commands(self):
        for ticks in ("``", "````", "``````"):
            for newline in ("\n", "\r\n", "\r"):
                for launch in ("Start-Process", "saps", "start"):
                    script = f"{launch} cmd.exe {ticks}{newline}Write-Host x -WindowStyle Hidden"
                    self.assertEqual(_judge_script(script), (1, 1), (ticks, newline, launch))
                    self.assertEqual(self._ps(repr(script)), (1, 1), (ticks, newline, launch))
        # an odd number of backticks IS a continuation: one command, hidden
        self.assertEqual(_judge_script("Start-Process cmd.exe `\nWrite-Host x -WindowStyle Hidden"), (1, 0))
        self.assertEqual(_judge_script("Start-Process cmd.exe ```\nWrite-Host x -WindowStyle Hidden"), (1, 0))
        self.assertEqual(_judge_script("Start-Process cmd.exe -WindowStyle Hidden ``\nWrite-Host x"), (1, 0))

    def test_pass28_a4_a_method_chain_is_read_in_time_linear_in_its_length(self):
        import time
        for body in ("'value'", "'SUBPROCESS_EXEC'"):
            for depth in (20, 48, 200):
                source = f"x = {body}" + ".lower()" * depth + "\n"
                started = time.monotonic()
                try:
                    self._bad(source)
                    _start_process_violations(source)
                except RecursionError:
                    self.fail(f"RecursionError for {body} * {depth}")
                self.assertLess(time.monotonic() - started, 5.0, (body, depth))
        reported = self._bad(self._launch("'SUBPROCESS_EXEC'" + ".lower()" * 48))
        self.assertGreaterEqual(len(reported), 1)

    def test_pass28_a5_a_container_printed_inside_a_text_shows_its_repr_not_its_texts(self):
        rows = ["(f\"{['subprocess_ex']}\")[2:-2] + 'ec'", "(f\"{('subprocess_ex',)}\")[2:-3] + 'ec'", "('{}'.format(['subprocess_ex']))[2:-2] + 'ec'",
                "('{}'.format({'subprocess_ex': 1}))[2:-5] + 'ec'", "(f\"{{'subprocess_ex'}}\")[2:-2] + 'ec'",
                "('%s' % (['subprocess_ex'],))[2:-2] + 'ec'", "(f\"{list(['subprocess_ex'])}\")[2:-2] + 'ec'", "(f\"{reversed(['subprocess_ex'])}\")[2:-2] + 'ec'"]
        for expr in rows:
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        for expr in ("(f\"{['value']}\")[2:-2] + 'ec'", "(f\"{['allowed_roots']}\")[2:-2]"):
            self.assertEqual(self._bad(self._named(expr)), [], expr)
        for script in ("(f\"{['Start-Process cmd.exe']}\")[2:-2]", "('{}'.format(['saps cmd.exe']))[2:-2]"):
            self.assertEqual(self._ps(script), (1, 1), script)

    # ---- pass 29 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r11-9b2d430a); A1 (an unknown slice drops a partial name) stays OPEN, see summary.md ----
    def test_pass29_a2_an_alias_of_bytes_encodes_and_does_not_decode(self):
        pre = "from builtins import bytes as B\nT = bytes\n"
        for ctor in ("B", "T"):
            expr = f"{ctor}('Ã©subprocess_ex', 'utf8')[4:].decode('utf8') + 'ec'"
            self.assertGreaterEqual(len(self._bad(pre + self._launch(expr))), 1, expr)
        self.assertEqual(self._ps("bytes('Ã©sa', 'utf8')[4:].decode('utf8') + 'ps cmd.exe'"), (1, 1))

    def test_pass29_a3_a_bytes_made_by_encode_prints_as_its_repr(self):
        for expr in ("'{}'.format('xxsubprocess_ex'.encode())[4:-1] + 'ec'", "f\"{'xxsubprocess_ex'.encode()}\"[4:-1] + 'ec'",
                     "'%s' % ('xxsubprocess_ex'.encode(),)")[:2]:
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        self.assertEqual(self._ps("'{}'.format('xxsaps cmd.exe'.encode())[4:-1]"), (1, 1))

    def test_pass29_a4_an_escaped_paren_or_brace_is_a_plain_character_of_its_word(self):
        for launch in ("Start-Process", "saps", "start"):
            for escaped in ("`(", "`)", "`{", "`}"):
                script = f"function Emit {{ param($Value,$WindowStyle) }}; Emit ({launch} cmd.exe -ArgumentList {escaped} ) -WindowStyle Hidden"
                self.assertEqual(_judge_script(script), (1, 1), (launch, escaped))
        self.assertEqual(_judge_script("Start-Process cmd.exe -ArgumentList `( -WindowStyle Hidden"), (1, 0))

    def test_pass29_a5_a_chain_that_grows_its_text_is_reported_not_materialised(self):
        for chain in (".replace('x', 'xx')", ".join(['x', 'x', 'x'])"):
            source = "def unused():\n    return 'x'" + chain * 48 + "\n"
            self.assertGreaterEqual(len(self._bad(source)), 1, chain)
            self.assertEqual(_start_process_violations(source)[0], 1, chain)

    # ---- r12: Dot R11-L1 -- a fold that can grow its text in ONE step is bounded BEFORE it allocates (the cap was only checked after the fact) ----
    # No test here builds a large result: the cap is patched down to a few dozen characters, or (at the real cap) only the PROJECTION is computed and refused.
    class _SpyStr(str):
        """A str whose ``replace`` / ``join`` record that they ran: bound to a name in ``consts`` it is the receiver the fold works on, with no seam in the fold."""
        def __new__(cls, value, calls):
            obj = super().__new__(cls, value)
            obj.calls = calls
            return obj

        def replace(self, *args):
            self.calls.append("replace")
            return super().replace(*args)

        def join(self, parts):
            self.calls.append("join")
            return super().join(parts)

        def encode(self, *args, **kwargs):
            self.calls.append("encode")
            return super().encode(*args, **kwargs)

    def _spied(self, expr: str, cap: int | None = None, **names: str):
        """(outcome, calls): the fold of ``expr`` with each name bound to a spying str, under a patched cap. outcome is the (text, known) or the _FoldTooBig."""
        calls: list[str] = []
        consts = {name: self._SpyStr(value, calls) for name, value in names.items()}
        with mock.patch.dict(globals(), {"_FLATTEN_MAX_CHARS": cap} if cap is not None else {}):
            try:
                outcome = _flatten(ast.parse(expr, mode="eval").body, consts)
            except _FoldTooBig as exc:
                outcome = exc
        return outcome, calls

    def test_r11_l1_replace_refuses_a_one_step_expansion_before_str_replace_runs(self):
        for expr in ("R.replace('a', 'bbb')",          # 30 + 30*2 = 90
                     "R.replace('', 'xx')",            # an empty old matches at len+1 points: 30 + 31*2 = 92
                     "R.replace('a', 'bbb', 40)",      # a count past the text is all of them
                     "R.replace('a', 'bbb', count=-1)",
                     "R.replace('a', 'bbbb', 25)"):    # 30 + 25*3 = 105
            outcome, calls = self._spied(expr, 64, R="a" * 30)
            self.assertIsInstance(outcome, _FoldTooBig, expr)
            self.assertEqual(calls, [], expr)  # str.replace never ran: nothing was allocated

    def test_r11_l1_replace_allows_what_fits_and_what_shrinks(self):
        for expr, expected in (("R.replace('a', 'bb')", "bb" * 32),          # exactly the cap: 64
                               ("R.replace('a', 'bbb', 9)", "bbb" * 9 + "a" * 23),  # count caps the growth: 32 + 9*2 = 50
                               ("R.replace('a', '')", ""),
                               ("R.replace('a', 'b')", "b" * 32),
                               ("R.replace('zz', 'yyyyyyyy')", "a" * 32),     # nothing to replace: 0 growth
                               ("R.replace('a', 'bbb', 0)", "a" * 32)):
            outcome, calls = self._spied(expr, 64, R="a" * 32)
            self.assertEqual(outcome, (expected, True), expr)
            self.assertEqual(calls, ["replace"], expr)
        outcome, _ = self._spied("R.replace('a', 'bb')", 64, R="a" * 33)  # one past the cap: 66
        self.assertIsInstance(outcome, _FoldTooBig)

    def test_r11_l1_replace_projection_at_the_real_cap_is_analytic(self):
        cap = _FLATTEN_MAX_CHARS
        self.assertEqual(cap, 200_000)  # the rows below are written for this cap
        for expr, receiver in (("R.replace('a', 'x' + 'xx')", "a" * 199_999),            # 199,999 * 3
                               ("R.replace('ab', 'xxxxx')", "ab" * 99_999),               # 199,998 + 99,999 * 3
                               ("R.replace('', 'xx')", "a" * 100_000)):                   # 100,000 + 100,001 * 2
            outcome, calls = self._spied(expr, None, R=receiver)
            self.assertIsInstance(outcome, _FoldTooBig, expr)
            self.assertEqual(calls, [], expr)  # the 600,000-character result was never built
        outcome, calls = self._spied("R.replace('a', 'x')", None, R="a" * 199_999)  # the projection fits (199,999): it runs
        self.assertEqual((outcome, calls), (("x" * 199_999, True), ["replace"]))

    def test_r11_l1_join_refuses_before_str_join_runs(self):
        piece = repr("a" * 30)
        outcome, calls = self._spied(f"S.join([{piece}, {piece}])", 64, S="-" * 10)  # 60 + 10
        self.assertIsInstance(outcome, _FoldTooBig)
        self.assertEqual(calls, [])
        outcome, calls = self._spied(f"S.join([{piece}, {piece}, {piece}])", 64, S="")  # 90 with no separator at all
        self.assertIsInstance(outcome, _FoldTooBig)
        self.assertEqual(calls, [])
        outcome, calls = self._spied(f"S.join([{piece}, {piece}])", 64, S="-" * 4)  # 64: exactly the cap
        self.assertEqual((outcome, calls), (("a" * 30 + "----" + "a" * 30, True), ["join"]))

    def test_r11_l1_templates_and_codecs_and_conversions_are_bounded_where_they_build(self):
        with mock.patch.dict(globals(), {"_FLATTEN_MAX_CHARS": 64}):
            thirty = ("a" * 30, True)
            with self.assertRaises(_FoldTooBig):  # one named field used three times: 90
                _fill_percent("%(x)s%(x)s%(x)s", [], {"x": thirty}, True)
            with self.assertRaises(_FoldTooBig):
                _fill_format("{0}{0}{0}", [thirty], {}, True)
            with self.assertRaises(_FoldTooBig):  # the padded copies of ONE field (width 40) add up before the join
                _fill_percent("%40s%40s", [thirty, thirty], {}, True)
            self.assertEqual(_fill_percent("%s%s", [thirty, thirty], {}, True), ("a" * 60, True))  # 60 fits
            self.assertEqual(_fill_format("{}{}", [thirty, thirty], {}, True), ("a" * 60, True))
            with self.assertRaises(_FoldTooBig):  # `!r` writes a four-to-ten-character escape for each character of 8 of them
                _apply_spec("\U0010ffff" * 8, "r", None)
            with self.assertRaises(_FoldTooBig):
                _apply_spec("\U0010ffff" * 8, "a", None)
            self.assertEqual(_apply_spec("abc", "r", None), "'abc'")
            for errors in ("backslashreplace", "xmlcharrefreplace"):  # ten characters out for each one in, and `namereplace` up to ninety
                call = ast.parse(f"x.encode('ascii', '{errors}')", mode="eval").body
                with self.assertRaises(_FoldTooBig, msg=errors):
                    _fold_codec(call, "\U0010ffff" * 8, True, {}, 0)
            call = ast.parse("x.decode('ascii', 'backslashreplace')", mode="eval").body  # a byte out of range becomes `\xNN`: four characters
            with self.assertRaises(_FoldTooBig):
                _fold_codec(call, "\xff" * 17, True, {}, 0)
            self.assertEqual(_fold_codec(ast.parse("x.encode('ascii', 'backslashreplace')", mode="eval").body, "ab\xe9", True, {}, 0), ("ab\\xe9", True))
            with self.assertRaises(_FoldTooBig):  # the hedge puts the texts side by side, and again backwards
                _hedge_texts(["a" * 40, "b" * 40])
            self.assertEqual(_hedge_texts(["a" * 20, "b" * 20]), _WILD)

    # ---- pass 30 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r12-7c801e52): A2 / A3, the bounds are upper bounds worked out from the characters ----
    def test_pass30_a3_the_bounds_never_fall_below_what_python_builds(self):
        quotes = ("'" * 256 + '"' * 256) * 3
        texts = ["", "abc", quotes, "it's \\ \"q\"", "\x01" * 40, "\xe9\xff" * 20, "中文" * 20, "\U0010ffff" * 20, "ﬃİA" * 20, "mixed \x00 \x7f \u0085 ퟿"]
        for text in texts:
            self.assertGreaterEqual(_repr_bound(text), len(repr(text)), repr(text)[:40])
            self.assertGreaterEqual(_repr_bound(text), len(ascii(text)), repr(text)[:40])
            if all(ord(c) < 256 for c in text):
                self.assertGreaterEqual(_repr_bound(text, True), len(repr(text.encode("latin-1"))), repr(text)[:40])
            for codec in ("ascii", "latin-1", "utf-8", "utf-16", "utf-16-le", "utf-32", "utf-32-be", "cp1252", "unicode_escape", "raw_unicode_escape"):
                for errors in ("strict", "ignore", "replace", "backslashreplace", "xmlcharrefreplace", "namereplace", "surrogateescape"):
                    try:
                        built = len(text.encode(codec, errors).decode("latin-1"))
                    except (UnicodeError, LookupError, TypeError):
                        continue
                    self.assertGreaterEqual(_encoded_bound(text, codec, errors), built, (repr(text)[:30], codec, errors))
            for method in ("upper", "lower", "casefold", "swapcase", "title", "capitalize"):
                self.assertGreaterEqual(len(text) + 2 * len(re.sub(r"[\x00-\x7f]", "", text)), len(getattr(text, method)()), (repr(text)[:30], method))

    def test_pass30_a2_a3_witnesses_are_refused_by_their_bound_not_by_building_them(self):
        quotes = ("'" * 256 + '"' * 256) * 261  # repr writes an escape for each of 66,816 quotes: 200,450 characters, which its per-chunk length (134,676) hid
        for conv in ("r", "a"):
            with self.assertRaises(_FoldTooBig, msg=conv):
                _apply_spec(quotes, conv, None)
        with self.assertRaises(_FoldTooBig):
            _fill_percent("%r", [("\x01" * 50_000, True)], {}, True)  # 200,002 characters
        with self.assertRaises(_FoldTooBig):
            _fill_percent("%a", [("\U0010ffff" * 20_001, True)], {}, True)
        with self.assertRaises(_FoldTooBig):
            _mapped("capitalize", ("İ" + "A" * 255) * 780)  # 200,459 characters, where the per-chunk length said 199,680
        for expr, text in (("x.encode('utf-32-le')", "a" * 50_001), ("x.encode('unicode_escape')", "\U0010ffff" * 20_001)):
            with self.assertRaises(_FoldTooBig, msg=expr):
                _fold_codec(ast.parse(expr, mode="eval").body, text, True, {}, 0)
        with self.assertRaises(_FoldTooBig):
            _formatted(ast.parse("'" + "\\x01" * 50_000 + "'.encode('latin-1')", mode="eval").body, {}, 0)  # bytes repr: four characters for each byte
        self.assertEqual(_apply_spec("abc", "r", None), "'abc'")  # short texts are never refused

    # ---- r13: sol r12 blocker -- `_encoded_bound` is an UPPER bound under the multi-byte codecs, and a codec without a proven row is hedged before `encode` runs ----
    # Safely bounded: the cap is patched down to 64 and ``str.encode`` is spied on, so a refusal that came AFTER the allocation fails the test. The one case at the real
    # cap only computes the bound.
    _WIDE_CODECS = ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be")
    _R13_HANDLERS = ("strict", "ignore", "replace", "backslashreplace", "xmlcharrefreplace", "namereplace", "surrogateescape", "surrogatepass")

    @staticmethod
    def _encode_call(codec: str, errors: str | None = None):
        args = repr(codec) + (f", {errors!r}" if errors is not None else "")
        return ast.parse(f"x.encode({args})", mode="eval").body

    def test_r13_utf16_utf32_replacement_text_is_refused_before_str_encode_runs(self):
        for codec in self._WIDE_CODECS:
            for errors in ("backslashreplace", "xmlcharrefreplace"):
                calls: list[str] = []
                with mock.patch.dict(globals(), {"_FLATTEN_MAX_CHARS": 64}):
                    with self.assertRaises(_FoldTooBig, msg=(codec, errors)):
                        _fold_codec(self._encode_call(codec, errors), self._SpyStr("\ud800" * 4, calls), True, {}, 0)
                self.assertNotIn("encode", calls, (codec, errors))  # nothing was allocated: the refusal came first

    def test_r13_sol_cap_64_witness_utf32_le_xmlcharrefreplace(self):
        calls: list[str] = []
        with mock.patch.dict(globals(), {"_FLATTEN_MAX_CHARS": 64}):
            self.assertEqual(len(("\ud800" * 3).encode("utf-32-le", "xmlcharrefreplace")), 96)  # what Python builds: past the cap of 64
            with self.assertRaises(_FoldTooBig):
                _fold_codec(self._encode_call("utf-32-le", "xmlcharrefreplace"), self._SpyStr("\ud800" * 3, calls), True, {}, 0)
        self.assertNotIn("encode", calls)

    def test_r13_opus_19999_surrogate_literal_is_projected_not_built(self):
        text = "\ud800" * 19_999  # a written-out literal; the old bound said 199,994, under the cap, and utf-32 + xmlcharrefreplace builds 639,968 bytes
        self.assertEqual(_FLATTEN_MAX_CHARS, 200_000)  # written for the real cap
        self.assertGreater(_encoded_bound(text, "utf-32", "xmlcharrefreplace"), _FLATTEN_MAX_CHARS)
        calls: list[str] = []
        with self.assertRaises(_FoldTooBig):
            _fold_codec(self._encode_call("utf-32", "xmlcharrefreplace"), self._SpyStr(text, calls), True, {}, 0)
        self.assertNotIn("encode", calls)

    def test_r13_ascii_literal_under_utf16_still_folds_to_its_known_value(self):
        for codec in ("utf-16", "utf-16-le", "utf-32-be"):
            for errors in (None, "backslashreplace", "xmlcharrefreplace"):
                calls: list[str] = []
                outcome = _fold_codec(self._encode_call(codec, errors), self._SpyStr("Start", calls), True, {}, 0)
                self.assertEqual(outcome, ("Start".encode(codec).decode("latin-1"), True), (codec, errors))
                self.assertEqual(calls, ["encode"])

    def test_r13_encoded_bound_is_an_upper_bound_for_every_admitted_codec_and_handler(self):
        texts = ["", "abc", "\ud800" * 5, "\udc80\udcff\udbff" * 3, "\U0010ffff" * 5, "\xe9\xff\x80", "中文€", "\x00\x01\\'\"\x7f", "a\ud800b\U0001f600\xe9",
                 "￾￿﷐", "ﬃİ\u0085"]
        checked = 0
        for name in sorted(_CODEC_KEYS | {"UTF-16LE", "utf_32_be", "UTF-8-SIG", "latin_1", "Unicode-Escape", "raw_unicode_escape", "u16", "u32"}):
            try:
                codecs.lookup(name)
            except LookupError:
                continue  # a key that is no codec (`utf8sig`): the call raises, and the fold reads it as unknown
            for errors in self._R13_HANDLERS:
                for text in texts:
                    try:
                        built = len(text.encode(name, errors).decode("latin-1"))
                    except (UnicodeError, LookupError, TypeError):
                        continue
                    self.assertGreaterEqual(_encoded_bound(text, name, errors), built, (name, errors, text[:12]))
                    checked += 1
        self.assertGreater(checked, 500)

    def test_r13_aliases_land_on_one_row_and_a_codec_without_a_row_is_hedged_before_encode(self):
        for alias in ("utf16", "UTF-16", "utf_16", "U16"):
            self.assertEqual(_encoded_bound("\ud800" * 5, alias, "backslashreplace"), _encoded_bound("\ud800" * 5, "utf-16", "backslashreplace"), alias)
        self.assertEqual(_encoded_bound("\ud800", "u32", "xmlcharrefreplace"), _encoded_bound("\ud800", "utf-32", "xmlcharrefreplace"))
        for name, errors in (("base64", "strict"), ("rot13", "strict"), ("utf-16", "no-such-handler"), ("\x00", "strict"), ("é", "strict")):
            with self.assertRaises(ValueError, msg=(name, errors)):
                _encoded_bound("abc", name, errors)
        calls: list[str] = []
        with mock.patch.dict(_CODEC_UNIT_BYTES):
            del _CODEC_UNIT_BYTES["utf-16"]  # a codec the table has no proven row for
            text, known = _fold_codec(self._encode_call("utf-16", "replace"), self._SpyStr("ab\xe9", calls), True, {}, 0)
        self.assertFalse(known)
        self.assertIn(_WILD, text)  # the wildcard: the consumers never read a guess as a literal
        self.assertNotIn("encode", calls)

    def test_r13_decode_stays_within_four_characters_for_each_byte(self):
        text = "".join(chr(i) for i in range(256)) * 2
        for name in ("utf-8", "ascii", "latin-1", "utf-16", "utf-16-le", "utf-32", "utf-32-be", "cp1252", "unicode_escape", "raw_unicode_escape"):
            for errors in ("ignore", "replace", "backslashreplace", "surrogateescape", "surrogatepass"):
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)  # unicode_escape reads `]` as an invalid escape
                        built = len(text.encode("latin-1").decode(name, errors))
                except (UnicodeError, LookupError, TypeError):
                    continue
                self.assertLessEqual(built, 4 * len(text), (name, errors))

    # ---- pass 32 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r13-373ed986): A1 / A2 ----
    def test_r13_pass32_a1_a_redirection_target_is_not_a_window_style_parameter(self):
        # `Start-Process cmd.exe > -WindowStyle Hidden` writes the output to a FILE named `-WindowStyle`; the launch has no -WindowStyle parameter at all.
        for op in (">", ">>", "2>", "2>>", "*>", "3>>", "1>"):
            for gap in (" ", "\t", "   "):
                source = f"script = 'Start-Process cmd.exe {op}{gap}-WindowStyle Hidden'\n"
                self.assertEqual(_start_process_violations(source), (1, [1]), source)
        for clean in ("Start-Process cmd.exe -WindowStyle Hidden",                      # the parameter itself
                      "Start-Process cmd.exe > out.txt -WindowStyle Hidden",             # a parameter may follow a redirection
                      'Start-Process cmd.exe -ArgumentList "a>" -WindowStyle Hidden',    # a quoted `>` redirects nothing
                      "Start-Process cmd.exe -WindowStyle Hidden > out.txt"):
            self.assertEqual(_start_process_violations(f"script = '{clean}'\n"), (1, []), clean)

    def test_r13_pass32_a2_a_pyw_launcher_is_enumerated_and_judged_like_a_py_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            flags = hidden_spawn.no_window_flags()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, timeout=60, creationflags=flags)
            (repo / "tools").mkdir()
            for name in ("launch_probe.pyw", "plain.py", "notes.txt"):
                (repo / "tools" / name).write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, timeout=60, creationflags=flags)
            self.assertEqual(_tracked_py_files(repo), ["tools/launch_probe.pyw", "tools/plain.py"])
        self.assertEqual(len(_unflagged_spawns_in_source("import subprocess\nsubprocess.run(['cmd.exe', '/c', 'exit'])\n", "tools/launch_probe.pyw")), 1)

    # ---- pass 33 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r13-5179ef24): A1 / A2 ----
    _P33_NAMED = "import asyncio\nasync def launch(p):\n    loop = asyncio.get_running_loop()\n    name = {name}\n    await loop.run_in_executor(None, print, name)\n"  # r15: a name position that is not a getattr one
    _P33_HEAD = "import asyncio\nasync def launch(p):\n    loop = asyncio.get_running_loop()\n    return await getattr(loop, {name})(asyncio.SubprocessProtocol, 'cmd.exe', '/c', 'exit')\n"

    @staticmethod
    def _fold_text(expr: str) -> tuple[str, bool]:
        return _flatten(ast.parse(expr, mode="eval").body, {})  # a name that is not bound is a run-time value of unknown text

    def test_r13_pass33_a1_percent_b_of_a_bytes_template_is_a_field_not_literal_text(self):
        # `b'%bexec' % b'subprocess_'` is b'subprocess_exec': `%b` is the bytes conversion for a text, so the `b` is not left in the name.
        self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name="(b'%bexec' % b'subprocess_').decode()"))), 1)
        self.assertEqual(_start_process_violations("script = (b'%b-Process cmd.exe' % b'Start').decode()\n")[0], 1)
        text, _ = self._fold_text("b'%bexec' % b'subprocess_'")
        self.assertNotIn("bexec", text)  # the `b` was read as a conversion, not kept as the name's second letter

    def test_r13_pass33_a2_a_precision_keeps_a_prefix_so_the_text_it_cuts_is_not_mandatory(self):
        # `'subprocess_ex%.0s' % x + 'ec'` is subprocess_exec whatever x is: a precision drops the end of the value, so the value is any text, not a substring that must be there.
        for name in ("('subprocess_ex%.0s' % b'junk') + 'ec'", "('subprocess_ex%.0s' % (p + 'junk')) + 'ec'", "('subprocess_ex%.0r' % (p + 'junk')) + 'ec'",
                     "('subprocess_ex%.0a' % (p + 'junk')) + 'ec'", "('subprocess_ex%.s' % (p + 'junk')) + 'ec'", "f\"subprocess_ex{(p + 'junk'):.0}ec\"",
                     "f\"subprocess_ex{(p + 'junk')!r:.0}ec\"", "'subprocess_ex{:.0}ec'.format(p + 'junk')", "'subprocess_ex{!r:.0}ec'.format(p + 'junk')",
                     "'subprocess_ex{x:.0}ec'.format_map({'x': p + 'junk'})", "'subprocess_ex{:.{}}ec'.format(p + 'junk', 0)", "'subprocess_ex{:{}}ec'.format(p + 'junk', '.0')"):
            self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name=name))), 1, name)
        for script in ("'sa%.0sps cmd.exe' % b'junk'", "'sa%.0sps cmd.exe' % (p + 'junk')", "f\"sa{(p + 'junk'):.0}ps cmd.exe\""):
            self.assertEqual(_start_process_violations(f"script = {script}\n")[0], 1, script)

    def test_r13_pass33_a2_a_value_through_a_conversion_without_a_precision_is_still_a_substring(self):
        # The sibling that must NOT widen: with no precision the whole value is printed (padded), so its literal text is still mandatory and a clean source stays clean.
        for expr in ("'x%rx' % (p + 'junk')", "f\"x{(p + 'junk')!r}x\"", "'x{!r:>9}x'.format(p + 'junk')"):
            self.assertIn("junk", self._fold_text(expr)[0], expr)

    def test_pass30_a2_literal_chains_and_joined_readings_obey_the_cap(self):
        with mock.patch.dict(globals(), {"_FLATTEN_MAX_CHARS": 64}):
            with self.assertRaises(_FoldTooBig):
                _joined(["a" * 40, "b" * 40])
            chain = "x = " + " + ".join(repr("a" * 30) for _ in range(3)) + "\n"
            self.assertEqual(_start_process_violations(chain)[0], 1)  # a chain of literals longer than the cap is reported, like every other fold
            self.assertEqual(_start_process_violations("x = " + " + ".join(repr("a" * 30) for _ in range(2)) + "\n")[0], 0)
        source = "x = " + " + ".join(repr("a" * 200) for _ in range(1001)) + "\n"  # 200,200 characters of source
        self.assertEqual(_start_process_violations(source)[0], 1)

    # ---- r14 (sol r13 on 199e9d56, the PR body's pass-34 A1 / A2): B1 a replace drops a partial pattern, B2 a bytes-type constructor reads as a str decoder ----
    # HUB RULING for this round: fail CONSERVATIVE, add no exact evaluation. A partial text through a replace keeps its shape; a bytes constructor encodes.
    _P34_BYTES = ("import asyncio\nasync def launch(p):\n    loop = asyncio.get_running_loop()\n{setup}    return await getattr(loop, {name})"
                  "(asyncio.SubprocessProtocol, 'cmd.exe', '/c', 'exit')\n")

    def test_r14_b1_a_replace_of_a_partial_text_keeps_the_pattern_it_could_not_apply(self):
        for name in ("('subpr' + p + 'zcess_exec').replace('z', 'o')", "('subpr' + p + 'zcess_exec').replace('z', 'o', 1)",
                     "('subpr' + p + 'ocess_ezec').replace('z', 'x')", "('subpr' + p + 'zcess_exec').replace('z', 'o', count=1)",
                     "('subpr' + p + 'zcess_exec').replace('z', p)", "('subpr' + p + 'ocess_exec').replace('', '')",
                     "('subpr' + p + 'ocess_exec').replace(p, 'q')", "'subprzcess_exec'.replace('z', p)",
                     "str.replace('subpr' + p + 'zcess_exec', 'z', 'o')"):
            self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name=name))), 1, name)

    def test_r14_b1_the_powershell_sibling_keeps_a_partial_launch_through_a_replace(self):
        for script in ("('Staz' + p + 'rt-Process cmd.exe').replace('z', '')", "('Staz' + p + 'rt-Process cmd.exe').replace('z', '', 1)",
                       "('Sta' + p + 'qrt-Process cmd.exe').replace('q', '')", "('saz' + p + 'ps cmd.exe').replace('z', '')"):
            self.assertEqual(_start_process_violations(f"def f(p):\n    script = {script}\n")[0], 1, script)

    def test_r14_b1_the_controls_keep_their_result(self):
        for name in ("('xsubpr' + p + 'ocess_exec').strip('x')", "('xsubpr' + p + 'ocess_exec')[1:]"):  # strip and slice controls stay reported
            self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name=name))), 1, name)
        self.assertEqual(self._fold_text("'subprzcess_exec'.replace('z', 'o')"), ("subprocess_exec", True))  # a fully known replace is exact, as before
        self.assertEqual(self._fold_text("'subprzcess_exec'.replace('z', 'o', 1)"), ("subprocess_exec", True))
        for name in ("'abc'.replace('a', 'b')", "('abc' + p + 'def').replace('x', 'y')", "('path/' + p).replace('/', '\\\\')"):  # no spawn name can come out: still clean
            self.assertEqual(self._bad(self._P33_NAMED.format(name=name)), [], name)
        self.assertEqual(_start_process_violations("def f(p):\n    script = ('log-' + p + '.txt').replace('-', '_')\n")[0], 0)

    def test_r14_b2_a_bytes_type_constructor_encodes_whatever_spelling_names_the_type(self):
        for setup, ctor in (("    B = type(b'')\n", "B"), ("    B = b''.__class__\n", "B"), ("", "type(b'')"), ("", "b''.__class__"), ("    B = bytes\n", "B"),
                            ("", "bytes"), ("    B = type(b'x')\n", "B"), ("    B = b'x'.__class__\n", "B")):
            name = f"{ctor}('subprocess_exe', 'utf-16-le').decode('utf-16-le') + 'c'"
            self.assertGreaterEqual(len(self._bad(self._P34_BYTES.format(setup=setup, name=name))), 1, (setup, ctor))
        for setup, ctor in (("    B = type(b'')\n", "B"), ("", "type(b'')")):  # utf-32 and a keyword codec take the same road
            name = f"{ctor}('subprocess_exe', encoding='utf-32-le').decode('utf-32-le') + 'c'"
            self.assertGreaterEqual(len(self._bad(self._P34_BYTES.format(setup=setup, name=name))), 1, (setup, ctor, "utf-32"))

    def test_r14_b2_the_type_a_name_proves_is_the_type_it_reads_as(self):
        _note_aliases(ast.parse("import builtins\n"))  # `builtins.str` is the builtin only through the builtins module the file imports
        for src, kind in (("bytes", "bytes"), ("str", "str"), ("type(b'')", "bytes"), ("type('')", "str"), ("b''.__class__", "bytes"), ("''.__class__", "str"),
                          ("builtins.bytes", "bytes"), ("builtins.str", "str"), ("type(p)", None), ("p.__class__", None), ("type(b'', 1)", None)):
            self.assertEqual(_type_kind(ast.parse(src, mode="eval").body), kind, src)
        _note_aliases(ast.parse("x = 1\n"))
        for src in ("builtins.str", "T.str", "x.bytes"):  # r15: no builtins import: the spelling of an attribute proves nothing
            self.assertIsNone(_type_kind(ast.parse(src, mode="eval").body), src)
        # r15 (HUB RULING, A3): a callee that is not proven to be the builtin is no constructor the fold models, and it is no longer told by its first argument
        self.assertNotEqual(self._fold_text("type(p)(b'subprocess_ex', 'ascii') + 'ec'")[0], "subprocess_exec")
        self.assertGreaterEqual(len(self._bad(self._P34_BYTES.format(setup="", name="type(p)('subprocess_exe', 'utf-16-le').decode('utf-16-le') + 'c'"))), 1)
        self.assertEqual(self._fold_text("type('')(b'subprocess_ex', 'ascii') + 'ec'")[0], "subprocess_exec")  # the str decode keeps its exact result

    # ---- r14 pass 35 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r14-0aae55d2): A1-A4, fixed as classes ----
    def test_r14_pass35_a1_a_replace_with_a_run_time_old_may_delete_any_characters(self):
        for name in ("'subprzocess_exec'.replace(p, '')", "'subprzocess_exec'.replace(p, '', 1)", "('subpr' + p + 'zocess_exec').replace(q, '')",
                     "'subprzocess_exec'.replace(p + q, '')", "'subprzocess_exec'.replace(p, q)"):
            self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name=name))), 1, name)
        for script in ("'Stazrt-Process cmd.exe'.replace(p, '')", "('saz' + q + 'ps cmd.exe').replace(p, '')", "('Staz' + p + 'rt-Process cmd.exe').replace(q, '')"):
            self.assertEqual(_start_process_violations(f"def f(p, q):\n    script = {script}\n")[0], 1, script)
        for name in ("'abc'.replace(p, '')", "'some_path_text'.replace(p, q)", "('log-' + p).replace(q, '_')"):  # no run of a name is held: clean
            self.assertEqual(self._bad(self._P33_NAMED.format(name=name)), [], name)
        self.assertEqual(_start_process_violations("def f(p):\n    script = 'log-file.txt'.replace(p, '')\n")[0], 0)

    def test_r14_pass35_a2_an_expression_nested_past_the_fold_depth_is_reported(self):
        for depth in (49, 60, 200):
            self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name="'subprocess_exe'" + ".lower()" * depth + " + 'c'"))), 1, depth)
            self.assertEqual(_start_process_violations("script = '{}ps cmd.exe'" + ".lower()" * depth + ".format('sa')\n")[0], 1, depth)
        self.assertEqual(self._bad(self._P33_HEAD.format(name="'abc'" + ".lower()" * 5)), [])  # a shallow benign chain stays clean

    def test_r14_pass35_a3_a_type_alias_is_followed_through_an_annotation_and_through_another_alias(self):
        for setup, ctor in (("    B: type = bytes\n", "B"), ("    B = bytes\n    C = B\n", "C"), ("    B = type(b'')\n    C = B\n    D = C\n", "D"),
                            ("    B: type = type(b'')\n", "B"), ("    C = B\n    B = bytes\n", "C")):
            name = f"{ctor}('subprocess_exe', 'utf-16-le').decode('utf-16-le') + 'c'"
            self.assertGreaterEqual(len(self._bad(self._P34_BYTES.format(setup=setup, name=name))), 1, (setup, ctor))
        self.assertGreaterEqual(len(self._bad("from builtins import bytes as B\n" + self._P34_BYTES.format(setup="    C = B\n", name="C('subprocess_exe', 'utf-16-le').decode('utf-16-le') + 'c'"))), 1)
        for setup in ("    S: type = str\n", "    B = str\n    S = B\n"):  # the str spellings keep the decode
            self.assertEqual(self._bad(self._P34_BYTES.format(setup=setup, name="S(b'subprocess_ex', 'ascii') + 'e'")), [], setup)  # `subprocess_ex` + `e` is no name
            self.assertGreaterEqual(len(self._bad(self._P34_BYTES.format(setup=setup, name="S(b'subprocess_ex', 'ascii') + 'ec'"))), 1, setup)

    def test_r14_pass35_a4_a_partial_text_through_a_cut_keeps_the_name_a_window_of_it_could_read_as(self):
        for name in ("('subpr' + p + 'ocess_execZ').rstrip('Z')", "('subpr' + p + 'ocess_execZ').strip('Z')", "('subpr' + p + 'ocess_execZ')[:-1]",
                     "('subpr' + p + 'ocess_execZ').removesuffix('Z')", "('subpr' + p + 'ocess_execZ').partition('Z')[0]", "('subpr' + p + 'ocess_execZ').split('Z')[0]",
                     "('subpr' + p + 'ocess_execZ').rsplit('Z', 1)[0]", "('ZZsubpr' + p + 'ocess_exec').lstrip('Z')", "('SUBPR' + p + 'OCESS_EXEC').capitalize().lower()",
                     "('SUBPR' + p + 'OCESS_EXEC').title().lower()", "('xsubpr' + p + 'ocess_exec')[p:]"):
            self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name=name))), 1, name)
        for script in ("('Sta' + p + 'rt-Process cmd.exeZ').rstrip('Z')", "('ZSta' + p + 'rt-Process cmd.exe').lstrip('Z')", "('Sta' + p + 'rt-Process cmd.exeZ')[:-1]"):
            self.assertEqual(_start_process_violations(f"def f(p):\n    script = {script}\n")[0], 1, script)
        for name in ("('abc' + p + 'def').rstrip('f')", "('log-' + p + '.txt')[:-1]", "('x_' + p + '_y').strip('_')"):  # no run of a name is held: clean
            self.assertEqual(self._bad(self._P33_NAMED.format(name=name)), [], name)

    def test_r14_pass35_the_pattern_items_are_refused_past_the_cap(self):
        # r15: the per-operation window / subsequence matchers are gone (HUB RULING); what is left of this test is the cap every item list is built under
        self.assertEqual(_pattern_items("ab" + _WILD + _WILD + "c"), ["a", "b", None, "c"])
        with self.assertRaises(_FoldTooBig):
            _pattern_items("a" * (_PATTERN_ITEM_CAP + 1))

    # ---- r15 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r14-b71b7880, pass 36): A1-A6, the HUB RULING: bound the class, delete the per-operation matchers ----
    def test_r15_a1_a_partial_text_through_a_cut_or_a_deletion_is_reported_when_it_holds_literal_text(self):
        for name in ("('sub' + p + 'junk')[:-4]", "('sub' + p + 'junk').replace(q, '')", "('sub' + p + 'junk').replace('junk', q)"):
            self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name=name))), 1, name)

    def test_r15_a2_an_inexact_replace_is_reported_whatever_shape_it_leaves(self):
        for name in ("'x'.replace('x', 'subprocess_ex' + p + 'JUNK')[:-4]", "('subprocess_ex' + p + 'JUNK').replace('JUNKsubprocess_ex', '')[:-4]",
                     "'x'.replace(p, 'subprocess_ex' + q + 'JUNK')[:-4]"):
            self.assertGreaterEqual(len(self._bad(self._P33_HEAD.format(name=name))), 1, name)

    def test_r15_a3_a_constructor_is_a_str_or_bytes_one_only_when_it_resolves_to_the_builtin(self):
        decoded = "{ctor}('subprocess_exe', 'utf-16-le').decode('utf-16-le') + 'c'"
        for setup, ctor in (("    class T:\n        str = bytes\n", "T.str"), ("    class T:\n        str = bytes\n    C = T.str\n", "C"),
                            ("    class T:\n        str = bytes\n    C: type = T.str\n", "C")):
            self.assertGreaterEqual(len(self._bad(self._P34_BYTES.format(setup=setup, name=decoded.format(ctor=ctor)))), 1, (setup, ctor))
        for src in ("T.str", "T.bytes", "x.str"):  # an attribute is never a proof of the type, whatever it is spelled
            self.assertIsNone(_type_kind(ast.parse(src, mode="eval").body), src)

    def test_r15_a4_a_block_comment_opens_only_at_the_start_of_a_token(self):
        for script in ("Write-Host a<#; Start-Process cmd.exe; #>", "Write-Host a<#; saps cmd.exe; #>", "Write-Host a<#; start cmd.exe; #>", "Write-Host -a<#; Start-Process cmd.exe; #>"):
            self.assertEqual(_start_process_violations(f"script = {script!r}\n")[0], 1, script)  # `a<#` is a bare word: the launch after it is code
        for script in ("Write-Host a <#; Start-Process cmd.exe; #>", "Write-Host 'a'<#; Start-Process cmd.exe; #>", "<#; Start-Process cmd.exe; #> Write-Host a"):
            self.assertEqual(_start_process_violations(f"script = {script!r}\n")[0], 0, script)  # a real comment hides nothing it holds from the launch count

    def test_r15_a5_a_launch_keyword_that_ends_inside_a_wildcard_is_reported(self):
        for script in ("f'sa{p}JUNK'", "'sa{}JUNK'.format(p)", "'sa%sJUNK' % p", "''.join(['sa', p, 'JUNK'])"):
            self.assertEqual(_start_process_violations(f"def f(p):\n    script = {script}\n")[0], 1, script)

    def test_r15_a6_a_pattern_past_the_item_cap_is_reported_not_read_clean(self):
        # a text of CAP characters (the wildcard counts as one) is searched, and a longer one is refused and reported: both matchers, both sides of the cap
        for size, reported in ((_PATTERN_ITEM_CAP - 2, False), (_PATTERN_ITEM_CAP - 1, False), (_PATTERN_ITEM_CAP, True), (_PATTERN_ITEM_CAP + 1, True)):
            self.assertEqual(len(self._bad("def f(p):\n    x = " + repr("a" * size) + " + p\n")) >= 1, reported, size)
            self.assertEqual(_start_process_violations("def f(p):\n    script = f'" + "a" * size + "{p}'\n")[0] == 1, reported, size)
        self.assertGreaterEqual(len(self._bad("def f(p):\n    return getattr(loop, " + repr("a" * _PATTERN_ITEM_CAP) + " + p)()\n")), 1)  # the shape the adversary ran

    # ---- r15 pass 37 (review-adversary-MLV-LANE-CONSOLE-HIDDEN-1-r15-11b17241): A1 / A2, both in the PowerShell readers ----
    def test_r15_pass37_a1_a_here_string_is_one_quoted_span_that_cannot_supply_the_outer_flag(self):
        for script in ('Start-Process cmd.exe -ArgumentList @"\nx " -WindowStyle Hidden "\n"@', "Start-Process cmd.exe -ArgumentList @'\nx ' -WindowStyle Hidden '\n'@",
                       'saps cmd.exe -ArgumentList @"\nx " -WindowStyle Hidden "\n"@', 'Start-Process cmd.exe -ArgumentList @"   \nx " -WindowStyle Hidden "\n"@'):
            self.assertEqual(_start_process_violations(f"script = {script!r}\n"), (1, [1]), script)
        for script in ('Start-Process cmd.exe -WindowStyle Hidden -ArgumentList @"\nx\n"@', "Start-Process cmd.exe -ArgumentList @'\nit''s\n'@ -WindowStyle Hidden"):
            self.assertEqual(_start_process_violations(f"script = {script!r}\n"), (1, []), script)  # the outer flag, with a here-string beside it, still counts

    def test_r15_pass37_a2_a_block_comment_after_a_bracket_or_any_white_space_is_a_comment_that_supplies_no_flag(self):
        for script in ("$a = @('/c', 'exit'); Start-Process cmd.exe -ArgumentList $a[0]<# -WindowStyle Hidden #>", "Start-Process cmd.exe -ArgumentList (1)<# -WindowStyle Hidden #>",
                       *(f"Start-Process cmd.exe{ws}<# -WindowStyle Hidden #>" for ws in ("\x0b", "\x0c", " ", " ", " "))):
            self.assertEqual(_start_process_violations(f"script = {script!r}\n")[0], 1, script)
            self.assertEqual(len(_start_process_violations(f"script = {script!r}\n")[1]), 1, script)
        self.assertEqual(_start_process_violations("script = 'Start-Process cmd.exe -WindowStyle Hidden <# note #>'\n"), (1, []))  # a real comment beside the flag is harmless
        self.assertEqual(_start_process_violations("script = 'Write-Host a<#; Start-Process cmd.exe; #>'\n")[0], 1)  # and the bare word of A4 is still read as code

    def test_r15_conservative_flips_a_name_that_is_not_a_constant_is_reported_in_a_getattr_name_position(self):
        # every one of these read clean before r15 (a matcher ruled the partial text out); the class rule reports them: a false positive is allowlisted by a reviewer, a false negative is not
        for expr in ("'.value_' + kind", "name + 'value'", "'loop.' + part", "f'{a}{b}'", "'allowed_roots.' + k", "'hello world'.replace(a, b)", "x", "'value_' + kind"):
            self.assertGreaterEqual(len(self._bad(self._launch(expr))), 1, expr)
        for expr in ("'value_'", "'subprocess_exec_' + 'x'", "'run'"):  # a constant name is clean
            self.assertEqual(self._bad(self._launch(expr)), [], expr)
        self.assertEqual(self._bad("import asyncio\ndef f(args, k):\n    return getattr(args, k)\n"), [])  # a receiver the file does not tie to a spawn module is not reported
        self.assertGreaterEqual(len(self._bad("import asyncio\ndef f(k):\n    return getattr(asyncio.get_running_loop(), k)\n")), 1)
        self.assertGreaterEqual(len(self._bad("import os\ndef f(k):\n    return getattr(os, k)\n")), 1)

    def test_r11_l1_a_one_step_growth_in_an_unused_function_is_reported_not_allocated(self):
        with mock.patch.dict(globals(), {"_FLATTEN_MAX_CHARS": 64}):
            for tail in (".replace('a', 'bbb')", ".replace('', 'xx')"):
                source = "def unused():\n    return " + repr("a" * 30) + tail + "\n"
                self.assertGreaterEqual(len(self._bad(source)), 1, tail)
                self.assertEqual(_start_process_violations(source)[0], 1, tail)
            self.assertEqual(self._bad("def unused():\n    return " + repr("a" * 30) + ".replace('a', 'b')\n"), [])


if __name__ == "__main__":
    unittest.main()
