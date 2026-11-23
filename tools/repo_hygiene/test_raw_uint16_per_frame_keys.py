"""Static tripwire: the CUDA texture route's per-frame record carries the raw-uint16 keys.

PLAYBACK-LJ92-DECODE-THROUGHPUT-1 round 3 (sol r2 BLOCKER cuda_per_frame_lj92_missing): the three
per-frame keys raw_uint16_source, raw_uint16_frame_lj92_ms and raw_uint16_inflight_wait_ms were
appended to playback_smoke.cpu_frame only. A CUDA texture-route series has to read them from the
GPU-route record, playback_smoke.gpu_frame, as well. Both lines append one shared string built from
the same slot.stageTimingTelemetry values, so the two records can never disagree.

The start line also logs MLVAPP_RAW_UINT16_PREFETCH_DECODERS, the lever's K knob, beside the
existing MLVAPP_DISABLE_RAW_UINT16_PREFETCH.
"""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
MAIN_WINDOW_CPP = ROOT / "platform/qt/MainWindow.cpp"

SHARED_KEYS = "rawUint16FrameKeys"
PER_FRAME_KEYS = (
    "raw_uint16_source",
    "raw_uint16_frame_lj92_ms",
    "raw_uint16_inflight_wait_ms",
)


def _statement_end(text: str, start: int) -> int:
    """Index of the ';' that ends the statement starting at start (skips literals and comments)."""
    depth = 0
    index = start
    while index < len(text):
        ch = text[index]
        if text.startswith("//", index):
            index = text.index("\n", index)
            continue
        if text.startswith("/*", index):
            index = text.index("*/", index) + 2
            continue
        if ch in "\"'":
            index += 1
            while text[index] != ch:
                index += 2 if text[index] == "\\" else 1
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            return index
        index += 1
    raise AssertionError("unterminated statement")


def _statement_containing(text: str, marker: str, opener: str) -> str:
    marker_end = text.index(marker) + len(marker)
    start = text.rindex(opener, 0, marker_end)
    return text[start:_statement_end(text, start) + 1]


_TRUE, _FALSE, _UNKNOWN = "true", "false", "unknown"
# Bare qmake scope names that are never active on macOS. Any other name besides macx stays UNKNOWN (never counts as live).
_QMAKE_NON_MACOS_SCOPES = frozenset((
    "win32", "win32-g++", "win32-msvc*", "linux", "linux-g++", "linux-g++*", "linux-g++-64", "android", "ios"))
_QMAKE_TRACKED_VARS = ("QMAKE_CFLAGS", "QMAKE_LFLAGS", "LIBS")
_OPENMP_LINK_TOKENS = ("-lomp", "-lgomp", "-llibgomp-1")
_QT_ARCH_ATOM = r"(?:equals|contains)\(\s*QT_ARCH\s*,\s*([A-Za-z0-9_]+)\s*\)"
# The only function-call conditions the model reads: the QT_ARCH test above and CONFIG(debug|release[, debug|release]).
# Any other call (unset(), system(), for(), contains(OTHER, ...), ...) can change a variable as a side effect of being
# evaluated as a condition, so it is refused rather than treated as an unknown that quietly never matches.
_QMAKE_CONFIG_ATOM = r"CONFIG\(\s*(?:debug|release)\s*(?:,\s*debug\s*\|\s*release\s*)?\)"
_QMAKE_ASSIGNMENT = re.compile(
    r"^(?P<cond>(?:[^=]*?:)?)\s*(?P<var>[A-Za-z_][A-Za-z0-9_.]*)\s*(?P<op>\+=|-=|\*=|~=|=)\s*(?P<value>.*)$")
_QMAKE_INCLUDE = re.compile(r"include\(\s*([^()$\"',\s]+)\s*\)")
# take_first(VAR) / take_last(VAR) return a value AND remove it from VAR, so an expansion of either is an edit of VAR wherever it
# appears, whatever variable it is assigned to (sol r3 B1). A function name assembled from another expansion could spell either, so
# it is refused too.
_QMAKE_MUTATING_EXPANSION = re.compile(r"take_(?:first|last)|\$\$\{?[A-Za-z0-9_.]*\$\$")
# A denylist of writers is never complete: $$system(cmd, mode, stsvar) also writes into a variable it names (Qt's own
# toolchain.prf does exactly that), so a value-side replace function is accepted only when it is known to read and nothing else
# (opus r4). Every other name, including system and any function a feature file defines, is refused wherever it appears.
# A driver option that takes its operand as the NEXT token consumes a following -fopenmp / -lomp as a directory, library or file
# name, so the token sits in the variable yet never reaches the compiler as a flag (sol r5 A1). A list of such options is never
# complete (the r5b list missed -iframeworkwithsysroot and ld64's -lazy_framework, which the `-l<name>` spelling hides: sol r6), so
# the model inverts it: in a live line of a flag variable, a token that starts with `-` is accepted only when it matches the
# allowlist below, which holds only options that take no operand or carry it joined to themselves. Any other dashed token is
# refused, listed anywhere or not. A project that needs another option adds it here with a test.
_QMAKE_ACCEPTED_OPTION = re.compile("|".join((
    r"-f(?:no-)?openmp", r"-nodefaultlibs", r"-nostdlib", r"-pthread", r"-f(?:PIC|PIE|pic|pie)",
    r"-std=[A-Za-z0-9+:_.-]+",
    r"-O(?:[0-3sgz]|fast)?", r"-g(?:[0-3]|gdb)?",
    r"-W(?![lap],)[A-Za-z0-9_+=.-]+",           # -Wall, -Wno-x, -Werror=x; never -Wl,/-Wa,/-Wp, (a joined option and its operand)
    r"-D[A-Za-z_][A-Za-z0-9_]*(?:=[^\s,]*)?",
    r"-[ILF][^\s,-][^\s,]*",                     # -I/path, -L/path, -F/path; the path is joined, never the next token
    # -lomp, -lgomp, -lc++, -llibgomp-1. No `_`: every ld64 -l* option with a separate operand (-lazy_framework, -lazy_library,
    # -lto_library, -load_hidden) has one, so a denylist of those four is replaced by the class (fable r7).
    r"-l[A-Za-z0-9+.-]+")))
# Every other QMAKE variable (QMAKE_RUN_CC*, QMAKE_CC_*, QMAKE_CXXFLAGS, QMAKE_EXTRA_COMPILERS, QMAKESPEC, ...) can change how
# the C compile or the link is spelled, and the model reads none of them (sol r5 A2): an assignment to one is refused unless it is
# one the project files use for something that touches neither line.
_QMAKE_BENIGN_UNMODELED_VARIABLES = frozenset(("QMAKE_SUBSTITUTES",))
_QMAKE_VALUE_FUNCTION_CALL = re.compile(r"\$\$\{?\s*([A-Za-z0-9_.]+)\s*\(")
_QMAKE_READONLY_VALUE_FUNCTIONS = frozenset((
    "absolute_path", "basename", "cat", "clean_path", "dirname", "escape_expand", "files", "find", "first", "format_number",
    "join", "last", "lower", "member", "native_path", "num_add", "quote", "re_escape", "relative_path", "replace", "reverse",
    "section", "shadowed", "shell_path", "shell_quote", "size", "sorted", "split", "sprintf", "str_member", "system_path",
    "system_quote", "unique", "upper", "val_escape"))


def _qmake_value_expansion_is_unsafe(value: str) -> bool:
    """Whether a right-hand side expands anything other than a variable, an environment variable, a qmake property or a
    value function on the read-only list above."""
    if _QMAKE_MUTATING_EXPANSION.search(value):
        return True
    return any(name not in _QMAKE_READONLY_VALUE_FUNCTIONS for name in _QMAKE_VALUE_FUNCTION_CALL.findall(value))
_QMAKE_MAX_INCLUDE_NESTING = 8
# Nesting alone leaves k^7 reads for a fan-out of k, so the total number of include() files one judgement reads is bounded too
# (fable r7 LJ92-PIN-INCLUDE-TOTAL-READ-BOUND-1). The project files the pin reads include a handful.
_QMAKE_MAX_INCLUDE_READS = 64
# CONFIG tokens a `+=` may add: none of them is a selector the model reads (macx, a platform name, debug/release).
_QMAKE_BENIGN_CONFIG_TOKENS = frozenset(("c++11", "c++14", "c++17", "c++20", "console", "warn_on", "warn_off"))
# Flag variables whose content can switch OpenMP off for the C compile (QMAKE_CFLAGS and its per-configuration forms) or
# for the link (QMAKE_LFLAGS and its forms). Their appends are watched for -fno-openmp even where the condition is unknown.
_QMAKE_COMPILE_FLAG_VARIABLE = re.compile(r"QMAKE_CFLAGS(?:_[A-Z0-9_]+)?|QMAKE_CC")
# The link line is QMAKE_LFLAGS* plus the library lists Qt emits after them (LIBS, LIBS_PRIVATE, QMAKE_LIBS*): a driver option
# in any of them reaches the link command (sol r2 A6).
_QMAKE_LINK_FLAG_VARIABLE = re.compile(
    r"QMAKE_LFLAGS(?:_[A-Z0-9_]+)?|LIBS|LIBS_PRIVATE|QMAKE_LIBS(?:_[A-Z0-9_]+)?|QMAKE_LINK(?:_[A-Z0-9_]+)?")
# Driver options that stop the compiler driver from adding the OpenMP runtime by itself (only the -fopenmp-in-QMAKE_LFLAGS
# credit relies on that auto-link; an explicit -lomp does not).
_QMAKE_LINK_OPENMP_DISABLERS = ("-fno-openmp", "-nodefaultlibs", "-nostdlib")
# Characters qmake gives a meaning to are ASCII; everything else (a non-breaking space, a form feed, a lone CR, a NUL) is
# part of an argument to qmake but a separator to str.split(), so a line carrying one is refused (sol r2 A3, A4).
_QMAKE_UNREADABLE_CHARACTER = re.compile(r"[^\x20-\x7e\t]")


_QMAKE_MAX_FILE_BYTES = 1024 * 1024


def _read_qmake_file(path: Path) -> str:
    """A .pro/.pri file as qmake sees its bytes: no universal-newline translation (a lone CR must stay a CR). Only a regular
    file of sane size is read: a FIFO or device blocks a synchronous read with no deadline, and a huge file stalls the gate.
    The pathname is opened exactly once and every judgement is made on that descriptor (sol r8 B2: a stat() of the pathname
    followed by a second open of it lets the file be replaced in between, by a larger file or by a link to a FIFO). The open is
    non-blocking where the host has the flag, so a FIFO swapped in is refused by the type check instead of waited on, and the
    read stops one byte past the limit whatever size the descriptor reported."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(str(path), flags)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise OSError("not a regular file: " + str(path))
        if status.st_size > _QMAKE_MAX_FILE_BYTES:
            raise OSError("larger than " + str(_QMAKE_MAX_FILE_BYTES) + " bytes: " + str(path))
        chunks = []
        remaining = _QMAKE_MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(descriptor)
    data = b"".join(chunks)
    if len(data) > _QMAKE_MAX_FILE_BYTES:
        raise OSError("larger than " + str(_QMAKE_MAX_FILE_BYTES) + " bytes: " + str(path))
    return data.decode("utf-8")


def _qmake_include_is_local(relative: str) -> bool:
    """Whether include(relative) is a path relative to the including file's directory: no leading `/` (a root path
    or a `//host/share` UNC path), no backslash (a Windows root or UNC path), no `:` (a drive, or the `//?/UNC/...` forms) and no
    `~`. Anything else could send the synchronous read to a network share, so it is refused before any file I/O."""
    return not (relative.startswith(("/", "~")) or "\\" in relative or ":" in relative)


def _qmake_include_target(directory: Path, relative: str) -> Path:
    """The file include(relative) opens: qmake cleans the path lexically (`link/../x.pri` is `x.pri`), it does not resolve
    symlinks first, so neither does the model."""
    return Path(os.path.normpath(str(directory / relative)))


def _qmake_include_leaves_checkout(target: Path, checkout_root: Path) -> bool:
    """Whether the cleaned include target is outside checkout_root, or reaches it through a symlink or junction (qmake cleans
    the path lexically, then the operating system follows any link on the way to wherever it leads). Lexical only for the first
    test, one lstat per path component below the root for the second: no read of anything outside the checkout."""
    root = os.path.normcase(os.path.abspath(str(checkout_root)))
    full = os.path.normcase(os.path.abspath(str(target)))
    try:
        if os.path.commonpath([root, full]) != root:
            return True
    except ValueError:  # another drive
        return True
    probe = os.path.abspath(str(checkout_root))
    for part in Path(os.path.relpath(os.path.abspath(str(target)), probe)).parts:
        probe = os.path.join(probe, part)
        is_junction = getattr(os.path, "isjunction", lambda _path: False)
        if os.path.islink(probe) or is_junction(probe):
            return True
    return False


def _qmake_refuse_malformed_value(statement: str, value: str) -> None:
    """A right-hand side with an unterminated quote or an unbalanced parenthesis makes qmake mark the project invalid."""
    depth = 0
    quote = ""
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == "\\":
                index += 1
            elif char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                break
        index += 1
    if quote or depth != 0:
        raise AssertionError("unsupported qmake construct (unterminated quote or unbalanced parenthesis): " + repr(statement))


def _qmake_logical_text(pro_text: str) -> str:
    """The .pro text qmake actually reads: comments dropped (a `#` runs to the end of its LF-terminated line, so only
    LF splits lines; a CR before the LF is part of a CRLF ending), then each backslash-newline continuation joined into
    one logical line. A continuation followed by a blank or comment-only line, and any character outside printable ASCII
    and tab in a live line, are refused: shapes this model does not try to interpret (fail closed)."""
    logical = []
    carry = ""
    for physical in pro_text.replace("\r\n", "\n").split("\n"):
        line = physical.split("#", 1)[0].rstrip(" \t")
        if _QMAKE_UNREADABLE_CHARACTER.search(line):
            raise AssertionError("unsupported qmake construct: a character qmake does not treat as space or text in " + repr(line))
        if carry and not line.strip():
            raise AssertionError("unsupported qmake construct: continuation followed by a blank or comment-only line")
        if line.endswith("\\"):
            carry += line[:-1] + " "
            continue
        logical.append(carry + line)
        carry = ""
    if carry:
        logical.append(carry)
    return "\n".join(logical)


def _qmake_split_top(text: str, separators: str) -> "list[str]":
    """Split at the given characters, but only outside parentheses (CONFIG(debug, debug|release) stays whole)."""
    parts = []
    depth = 0
    start = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char in separators and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return [part.strip() for part in parts]


def _qmake_atom_state(atom: str, arch: str, seen: "set[str] | None" = None) -> str:
    """`seen`, when given, collects every architecture a QT_ARCH test of the project names (see
    _qmake_named_architectures)."""
    if atom.startswith("!!"):
        raise AssertionError("unsupported qmake construct: repeated '!' in selector " + repr(atom))
    negated = atom.startswith("!")
    atom = atom.lstrip("!").strip()
    arch_match = re.fullmatch(_QT_ARCH_ATOM, atom)
    if "QT_ARCH" in atom and not arch_match:
        raise AssertionError("unsupported qmake construct: unreadable QT_ARCH selector " + repr(atom))
    if arch_match:
        if seen is not None:
            seen.add(arch_match.group(1))
        state = _TRUE if arch_match.group(1) == arch else _FALSE
    elif atom == "macx" or atom == "true":
        state = _TRUE
    elif atom == "false":
        state = _FALSE
    elif atom in _QMAKE_NON_MACOS_SCOPES:
        state = _FALSE
    elif "(" in atom and not re.fullmatch(_QMAKE_CONFIG_ATOM, atom):
        raise AssertionError("unsupported qmake construct: function call in a condition " + repr(atom))
    else:
        state = _UNKNOWN
    if negated and state != _UNKNOWN:
        state = _FALSE if state == _TRUE else _TRUE
    return state


def _qmake_and(states: "list[str]") -> str:
    if _FALSE in states:
        return _FALSE
    return _UNKNOWN if _UNKNOWN in states else _TRUE


def _qmake_condition_state(condition: str, arch: str, seen: "set[str] | None" = None) -> str:
    """Whether a scope header or single-line prefix is active on macOS with QT_ARCH=arch. `:` is AND, `|` is OR; a
    header mixing both, `else`, CONFIG(...) and any scope name not listed above stay UNKNOWN. Every atom is read first,
    so a function call anywhere in a mixed header is refused rather than hidden behind the UNKNOWN (sol r2 A1)."""
    if not condition:
        return _TRUE
    for atom in _qmake_split_top(condition, "|:"):
        _qmake_atom_state(atom, arch, seen)
    alternatives = _qmake_split_top(condition, "|")
    conjuncts = _qmake_split_top(condition, ":")
    if len(alternatives) > 1 and len(conjuncts) > 1:
        return _UNKNOWN
    if len(alternatives) > 1:
        states = [_qmake_atom_state(alternative, arch) for alternative in alternatives]
        if _TRUE in states:
            return _TRUE
        return _UNKNOWN if _UNKNOWN in states else _FALSE
    return _qmake_and([_qmake_atom_state(conjunct, arch) for conjunct in conjuncts])


_COMPILE_GAP = "C compile lacks -fopenmp"
_LINK_GAP = "link line lacks OpenMP (-fopenmp in QMAKE_LFLAGS or -lomp/-lgomp in LIBS)"
# The flag is there, but only because a line OUTSIDE the QT_ARCH branch sets it (sol r1 blocker 1 on ec94d560).
_LOCAL_COMPILE_GAP = "C compile -fopenmp is set outside the QT_ARCH branch, not inside it"
_LOCAL_LINK_GAP = "link line OpenMP flag is set outside the QT_ARCH branch, not inside it"


def _qmake_atom_is_arch_local(atom: str, arch: str) -> bool:
    """Whether an atom is a plain (not negated) QT_ARCH test naming exactly this architecture."""
    match = re.fullmatch(_QT_ARCH_ATOM, atom.strip())
    return bool(match) and match.group(1) == arch


def _qmake_atom_is_arch_test(atom: str) -> bool:
    """Whether an atom is a plain (not negated) QT_ARCH test, whichever architecture it names."""
    return bool(re.fullmatch(_QT_ARCH_ATOM, atom.strip()))


def _qmake_condition_is_arch_local(condition: str, arch: str) -> bool:
    """Whether a scope header or single-line prefix is itself a QT_ARCH=arch branch: an AND needs one such conjunct, an OR
    needs every alternative to be a QT_ARCH test and one of them to name this architecture (so
    `equals(QT_ARCH, x86_64)|equals(QT_ARCH, arm64)` counts, `macx|equals(...)` does not), and a header mixing the two is
    never one."""
    if not condition:
        return False
    alternatives = _qmake_split_top(condition, "|")
    conjuncts = _qmake_split_top(condition, ":")
    if len(alternatives) > 1 and len(conjuncts) > 1:
        return False
    if len(alternatives) > 1:
        return (all(_qmake_atom_is_arch_test(alternative) for alternative in alternatives)
                and any(_qmake_atom_is_arch_local(alternative, arch) for alternative in alternatives))
    return any(_qmake_atom_is_arch_local(conjunct, arch) for conjunct in conjuncts)


def _qmake_condition_has_macx(condition: str) -> bool:
    """Whether a scope header or single-line prefix confines what it guards to macOS: `macx` is one of its AND-ed conjuncts. An OR
    never does (`macx|linux` also guards Linux), and a header mixing both is read as not confining (sol r8 B1)."""
    if not condition or len(_qmake_split_top(condition, "|")) > 1:
        return False
    return any(conjunct == "macx" for conjunct in _qmake_split_top(condition, ":"))


def _qmake_simulate(
        pro_text: str, arch: str, source_dir: "Path | None" = None, seen: "set[str] | None" = None,
        checkout_root: Path = ROOT
) -> "tuple[dict[str, list[str]], dict[str, set[str]], dict[str, set[str]]]":
    """Simulate a macOS build with QT_ARCH=arch in file order, following include() files where qmake evaluates them, and only
    those inside checkout_root (the repository root unless a caller names another): an include whose cleaned path lies outside it,
    or runs through a link, is refused before any read (sol r6).
    Returns (values, branch_local, possible):
      values        effective tokens of QMAKE_CFLAGS, QMAKE_LFLAGS and LIBS. An addition counts only where every enclosing
                    condition is certainly true; a removal or reassignment applies wherever its condition is true OR
                    unknown, so the answer can only err toward reporting a lost flag.
      branch_local  the tokens whose certain-true addition came from a line inside (or prefixed by) a QT_ARCH=arch
                    branch, so a flag inherited from the enclosing macx scope is not credited to the branch.
      possible      per flag variable (QMAKE_CFLAGS*, QMAKE_LFLAGS*, LIBS), every token some line may add, including lines
                    whose condition is unknown; only a certain removal clears one. This is what catches -fno-openmp."""
    values: "dict[str, list[str]]" = {name: [] for name in _QMAKE_TRACKED_VARS}
    branch_local: "dict[str, set[str]]" = {name: set() for name in _QMAKE_TRACKED_VARS}
    possible: "dict[str, set[str]]" = {}
    reads_made = [0]

    def watched(variable: str) -> bool:
        return (variable in values or bool(_QMAKE_COMPILE_FLAG_VARIABLE.fullmatch(variable))
                or bool(_QMAKE_LINK_FLAG_VARIABLE.fullmatch(variable)))

    def apply(statement: str, scope: "tuple[str, bool, bool]", depth: int, directory: "Path | None", nesting: int) -> None:
        # Fail closed: a statement this model cannot read as `[conditions:]VARIABLE op value` is refused, never skipped.
        match = _QMAKE_ASSIGNMENT.match(statement)
        if not match:
            include = _QMAKE_INCLUDE.fullmatch(statement)
            if depth == 0 and include:
                follow(include.group(1), directory, nesting)
                return
            raise AssertionError("unsupported qmake construct: " + repr(statement))
        variable, operator, value = match.group("var"), match.group("op"), match.group("value")
        _qmake_refuse_malformed_value(statement, value)
        if _qmake_value_expansion_is_unsafe(value):
            raise AssertionError("unsupported qmake construct (an expansion that may write to a variable it names): "
                                 + repr(statement))
        tokens = re.split(r"[ \t]+", value.strip(" \t"))
        tokens = [token for token in tokens if token]
        if variable == "QT_ARCH" or (variable == "CONFIG" and (
                operator in ("=", "~=")
                or re.search(r"[$\"'`]", value)
                or (operator == "-=" and "macx" in tokens)
                or (operator in ("+=", "*=") and not set(tokens) <= _QMAKE_BENIGN_CONFIG_TOKENS))):
            raise AssertionError("unsupported qmake construct (it changes what a selector means): " + repr(statement))
        condition = match.group("cond").rstrip(":").strip()
        state = _qmake_and([scope[0], _qmake_condition_state(condition, arch, seen)])
        if not watched(variable):
            if (variable.startswith("QMAKE") and variable not in _QMAKE_BENIGN_UNMODELED_VARIABLES
                    and state != _FALSE):
                raise AssertionError("unsupported qmake construct (a QMAKE variable the model does not track, which can change "
                                     "the C compile or link command): " + repr(statement))
            return
        if operator == "~=" or re.search(r"[$\"']|(?:^|\s)@", value):
            raise AssertionError("unsupported qmake construct (substitution, expansion, quoting or response file): "
                                 + repr(statement))
        if state == _FALSE:
            return
        if operator != "-=" and any(token.startswith("-") and not _QMAKE_ACCEPTED_OPTION.fullmatch(token) for token in tokens):
            raise AssertionError("unsupported qmake construct (an option that is not on the model's allowlist of options that take "
                                 "no operand or carry it joined; one that takes the next token as its operand would consume the "
                                 "OpenMP flag after it): " + repr(statement))
        certain = state == _TRUE
        # A line is the QT_ARCH branch's own only where a macx scope AND a QT_ARCH=arch test both enclose it, in either order; an
        # architecture block outside macx also guards Linux and Windows builds, so it earns no macOS credit (sol r8 B1).
        local = ((scope[1] or _qmake_condition_has_macx(condition))
                 and (scope[2] or _qmake_condition_is_arch_local(condition, arch)))
        may_add = possible.setdefault(variable, set())
        if operator in ("+=", "*="):
            may_add.update(tokens)
        elif operator == "-=":
            if certain:
                may_add.difference_update(tokens)
        elif certain:
            possible[variable] = set(tokens)
        else:
            may_add.update(tokens)
        if variable not in values:
            return
        if operator == "+=":
            if certain:
                values[variable].extend(tokens)
                if local:
                    branch_local[variable].update(tokens)
        elif operator == "*=":
            if certain:
                values[variable].extend(token for token in dict.fromkeys(tokens) if token not in values[variable])
                if local:
                    branch_local[variable].update(tokens)
        elif operator == "-=":
            values[variable] = [token for token in values[variable] if token not in tokens]
            branch_local[variable].difference_update(tokens)
        else:
            values[variable] = tokens if certain else []
            branch_local[variable] = set(tokens) if certain and local else set()

    def follow(relative: str, directory: "Path | None", nesting: int) -> None:
        if directory is None:
            raise AssertionError("unsupported qmake construct: include(" + relative + ") with no directory to resolve it in")
        if nesting >= _QMAKE_MAX_INCLUDE_NESTING:
            raise AssertionError("unsupported qmake construct: include() nested more than "
                                 + str(_QMAKE_MAX_INCLUDE_NESTING) + " deep (a loop?)")
        if not _qmake_include_is_local(relative):
            raise AssertionError("unsupported qmake construct: include(" + relative + ") is not a path relative to the including "
                                 "file (a UNC share, drive or root path would read outside this checkout)")
        target = _qmake_include_target(directory, relative)
        if _qmake_include_leaves_checkout(target, checkout_root):
            raise AssertionError("unsupported qmake construct: include(" + relative + ") resolves outside the checkout (or through "
                                 "a link out of it); it is refused unread")
        reads_made[0] += 1
        if reads_made[0] > _QMAKE_MAX_INCLUDE_READS:
            raise AssertionError("unsupported qmake construct: more than " + str(_QMAKE_MAX_INCLUDE_READS)
                                 + " include() reads in one judgement (a fan-out of nested includes)")
        try:
            text = _read_qmake_file(target)
        except (OSError, UnicodeError) as error:
            raise AssertionError("unsupported qmake construct: include(" + relative + ") is unreadable: " + str(error))
        evaluate(text, target.parent, nesting + 1)

    def evaluate(text: str, directory: "Path | None", nesting: int) -> None:
        scopes: "list[tuple[str, bool, bool]]" = []
        buffer = ""
        in_reference = False

        def flush() -> None:
            nonlocal buffer
            statement = buffer.strip()
            buffer = ""
            if statement:
                apply(statement, scopes[-1] if scopes else (_TRUE, False, False), len(scopes), directory, nesting)

        for char in _qmake_logical_text(text):
            if in_reference:
                buffer += char
                in_reference = char != "}"
            elif char == "{" and buffer.endswith("$$"):
                buffer += char
                in_reference = True
            elif char == "{":
                header = buffer.strip()
                own = _qmake_condition_state(header, arch, seen)
                buffer = ""
                parent_state, parent_macx, parent_arch = scopes[-1] if scopes else (_TRUE, False, False)
                scopes.append((_qmake_and([parent_state, own]),
                               parent_macx or _qmake_condition_has_macx(header),
                               parent_arch or _qmake_condition_is_arch_local(header, arch)))
            elif char == "}":
                flush()
                if not scopes:
                    raise AssertionError("unbalanced '}' in qmake text")
                scopes.pop()
            elif char == "\n":
                flush()
            else:
                buffer += char
        flush()
        if scopes or in_reference:
            raise AssertionError("unterminated qmake scope or $${...} reference")

    evaluate(pro_text, source_dir, 0)
    return values, branch_local, possible


def _qmake_effective_flags(
        pro_text: str, arch: str, source_dir: "Path | None" = None, checkout_root: Path = ROOT) -> "dict[str, list[str]]":
    """Effective tokens of QMAKE_CFLAGS, QMAKE_LFLAGS and LIBS for a macOS build with QT_ARCH=arch (see _qmake_simulate)."""
    return _qmake_simulate(pro_text, arch, source_dir, None, checkout_root)[0]


def _macos_openmp_gaps(
        pro_text: str, arch: str, source_dir: "Path | None" = None, checkout_root: Path = ROOT) -> "list[str]":
    """The OpenMP flags a macOS QT_ARCH=arch build of this .pro would NOT get: the C compile flag and the link flag. Each
    must be present, carried by a line of the QT_ARCH branch itself (not inherited from the enclosing macx scope), and not
    switched off by a -fno-openmp that any line of the matching flag variables may add."""
    values, branch_local, possible = _qmake_simulate(pro_text, arch, source_dir, None, checkout_root)
    compile_variables = [name for name in possible if _QMAKE_COMPILE_FLAG_VARIABLE.fullmatch(name)]
    link_variables = [name for name in possible if _QMAKE_LINK_FLAG_VARIABLE.fullmatch(name)]
    compile_disabled = any("-fno-openmp" in possible[name] for name in compile_variables)
    link_flag_disabled = any(
        disabler in possible[name] for name in link_variables for disabler in _QMAKE_LINK_OPENMP_DISABLERS)
    gaps = []
    if "-fopenmp" not in values["QMAKE_CFLAGS"] or compile_disabled:
        gaps.append(_COMPILE_GAP)
    elif "-fopenmp" not in branch_local["QMAKE_CFLAGS"]:
        gaps.append(_LOCAL_COMPILE_GAP)
    link_present = ("-fopenmp" in values["QMAKE_LFLAGS"] and not link_flag_disabled,
                    any(token in values["LIBS"] for token in _OPENMP_LINK_TOKENS))
    link_local = ("-fopenmp" in branch_local["QMAKE_LFLAGS"] and not link_flag_disabled,
                  any(token in branch_local["LIBS"] for token in _OPENMP_LINK_TOKENS))
    if not any(link_present):
        gaps.append(_LINK_GAP)
    elif not any(link_local):
        gaps.append(_LOCAL_LINK_GAP)
    return gaps


def _qmake_named_architectures(
        pro_text: str, source_dir: "Path | None" = None, checkout_root: Path = ROOT) -> "set[str]":
    """Every architecture a QT_ARCH test of the project, or of an include() it follows, names: the ones qmake evaluates as
    conditions, not the ones a comment or a quoted string happens to mention."""
    seen: "set[str]" = set()
    _qmake_simulate(pro_text, "x86_64", source_dir, seen, checkout_root)
    return seen


def _project_openmp_gaps(
        project: Path, arch: str, pro_text: "str | None" = None, checkout_root: Path = ROOT) -> "list[str]":
    """_macos_openmp_gaps for a .pro file on disk (pro_text is the already-read root text, so one snapshot of the root is
    both inventoried and judged)."""
    return _macos_openmp_gaps(
        _read_qmake_file(project) if pro_text is None else pro_text, arch, project.parent, checkout_root)


_MACX_PIN_TEMPLATE = """\
macx {
    QMAKE_CFLAGS += -std=c99
    equals(QT_ARCH, x86_64) {
        QMAKE_CFLAGS += -fopenmp
        LIBS += -L/usr/local/opt/llvm/lib -lomp
    }
%(arm64)s
}
%(tail)s
"""
_ARM64_LIVE = "    equals(QT_ARCH, arm64) {\n        QMAKE_CFLAGS += -fopenmp\n        LIBS += -L/opt/homebrew/opt/llvm/lib -lomp\n    }"
_BOTH_GAPS = [_COMPILE_GAP, _LINK_GAP]


def _arm64_section(*lines: str, opener: str = "equals(QT_ARCH, arm64)") -> str:
    return "    " + opener + " {\n" + "".join("        " + line + "\n" for line in lines) + "    }"


# (case name, arm64 section, text after the macx scope, expected arm64 gaps). Each shape is one the adversary pass
# showed the raw-text guard accepted although qmake would not give macOS arm64 the flag (LJ92-PRO-MACOS-ARCH-PIN-1 r1).
_MACX_PIN_CASES = (
    ("live", _ARM64_LIVE, "", []),
    ("a U+2028 inside a comment is not a line break to qmake", _ARM64_LIVE, "# note macx:QMAKE_CFLAGS -= -fopenmp", []),
    ("*= adds a value only once", _arm64_section("QMAKE_CFLAGS *= -fopenmp", "QMAKE_CFLAGS *= -fopenmp", "LIBS += -lomp"), "", []),
    ("A11 -fno-openmp later on the compile line", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS += -fno-openmp", [_COMPILE_GAP]),
    ("continuation lines keep the flag", _arm64_section(
        "QMAKE_CFLAGS += \\", "    -fopenmp", "LIBS += -L/opt/homebrew/opt/llvm/lib \\", "    -lomp"), "", []),
    ("second macx scope is additive", _ARM64_LIVE, "macx {\n    DEFINES += EXTRA_DIAGNOSTICS\n}", []),
    ("A1 assignments commented out", _arm64_section(
        "# QMAKE_CFLAGS += -fopenmp", "# LIBS += -L/opt/homebrew/opt/llvm/lib -lomp"), "", _BOTH_GAPS),
    ("A1 whole branch commented out", "\n".join("    # " + line.strip() for line in _ARM64_LIVE.splitlines()), "", _BOTH_GAPS),
    ("A2 negated architecture", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp", opener="!equals(QT_ARCH, arm64)"), "", _BOTH_GAPS),
    ("A2 flags only under CONFIG(debug)", "    equals(QT_ARCH, arm64) {\n        CONFIG(debug, debug|release) {\n"
     "            QMAKE_CFLAGS += -fopenmp\n            LIBS += -lomp\n        }\n    }", "", _BOTH_GAPS),
    ("A3 release branch empty, debug branch carries the flags",
     _arm64_section(opener="CONFIG(release, debug|release):equals(QT_ARCH, arm64)") + "\n" + _arm64_section(
         "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp", opener="CONFIG(debug, debug|release):equals(QT_ARCH, arm64)"),
     "", _BOTH_GAPS),
    ("A4 variable name is only a suffix", _arm64_section(
        "SAVED_QMAKE_CFLAGS += -fopenmp", "SAVED_LIBS += -lomp"), "", _BOTH_GAPS),
    ("A4 flag only inside another argument", _arm64_section(
        "QMAKE_CFLAGS += -DSAVED_TEXT=-fopenmp", "SAVED_QMAKE_LFLAGS += -DSAVED_TEXT=-fopenmp"), "", _BOTH_GAPS),
    ("A4 look-alike link library", _arm64_section("QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp-west"), "", [_LINK_GAP]),
    ("A5 removal inside the branch", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp", "QMAKE_CFLAGS -= -fopenmp", "LIBS -= -lomp"), "", _BOTH_GAPS),
    ("A5 single-line removal after the macx scope", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS -= -fopenmp\nmacx:equals(QT_ARCH, arm64):LIBS -= -lomp", _BOTH_GAPS),
    ("A5 reassignment after the macx scope", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS = -std=c99", [_COMPILE_GAP]),
    ("a brace in a comment does not end the branch", _arm64_section(
        "# } never closes the scope", "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp"), "", []),
    # sol r1 blocker 1: the flag must be carried by the QT_ARCH branch itself, not inherited from an enclosing scope.
    ("B1 both flags moved to one assignment in the parent macx scope", _arm64_section(
        "INCLUDEPATH += /opt/homebrew/opt/llvm/include"),
     "macx:QMAKE_CFLAGS += -fopenmp\nmacx:LIBS += -L/opt/homebrew/opt/llvm/lib -lomp", [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP]),
    ("B1 both flags moved into a second macx brace scope", _arm64_section(
        "INCLUDEPATH += /opt/homebrew/opt/llvm/include"),
     "macx {\n    QMAKE_CFLAGS += -fopenmp\n    LIBS += -lomp\n}", [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP]),
    ("B1 compile flag only in the parent macx scope", _arm64_section("LIBS += -lomp"),
     "macx:QMAKE_CFLAGS += -fopenmp", [_LOCAL_COMPILE_GAP]),
    ("B1 -lomp only in the parent macx scope", _arm64_section("QMAKE_CFLAGS += -fopenmp"),
     "macx:LIBS += -lomp", [_LOCAL_LINK_GAP]),
    ("B1 -fopenmp link flag only in the parent macx scope", _arm64_section("QMAKE_CFLAGS += -fopenmp"),
     "macx:QMAKE_LFLAGS += -fopenmp", [_LOCAL_LINK_GAP]),
    ("B1 flag set for every platform at the top level", _arm64_section("LIBS += -lomp"),
     "QMAKE_CFLAGS += -fopenmp", [_LOCAL_COMPILE_GAP]),
    ("B1 a parent flag does not hide the branch's own flag", _ARM64_LIVE,
     "macx:QMAKE_CFLAGS += -fopenmp\nmacx:LIBS += -lomp", []),
    # sol r1 blocker 2: an append whose condition the model cannot decide (CONFIG(...)) can still disable OpenMP.
    ("B2 -fno-openmp appended for release builds", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):QMAKE_CFLAGS += -fno-openmp", [_COMPILE_GAP]),
    ("B2 -fno-openmp appended for debug builds", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):CONFIG(debug, debug|release):QMAKE_CFLAGS += -fno-openmp", [_COMPILE_GAP]),
    ("B2 -fno-openmp added with *= for release builds", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):QMAKE_CFLAGS *= -fno-openmp", [_COMPILE_GAP]),
    ("B2 -fno-openmp inside nested brace scopes", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64) {\n    CONFIG(release, debug|release) {\n        QMAKE_CFLAGS += -fno-openmp\n    }\n}",
     [_COMPILE_GAP]),
    ("B2 -fno-openmp in the per-configuration C flag variable", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS_RELEASE += -fno-openmp", [_COMPILE_GAP]),
    ("B2 -fno-openmp in a debug-only per-configuration C flag variable", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):CONFIG(debug, debug|release):QMAKE_CFLAGS_DEBUG += -fno-openmp", [_COMPILE_GAP]),
    ("B2 -fno-openmp appended to the link flags for debug builds", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -fopenmp"),
     "macx:equals(QT_ARCH, arm64):CONFIG(debug, debug|release):QMAKE_LFLAGS += -fno-openmp", [_LINK_GAP]),
    ("B2 an uncertain removal does not cancel an uncertain -fno-openmp", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):QMAKE_CFLAGS += -fno-openmp\n"
     "macx:equals(QT_ARCH, arm64):CONFIG(debug, debug|release):QMAKE_CFLAGS -= -fno-openmp", [_COMPILE_GAP]),
    ("B2 a certain removal does cancel the -fno-openmp again", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):QMAKE_CFLAGS += -fno-openmp\n"
     "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS -= -fno-openmp", []),
    # sol r2 (adversary pass 3 on ef997fb3): flags that reach the link line another way, literal booleans, OR-ed architectures
    # and a CR that is not a line break to qmake.
    ("C-A3 a bare CR does not end a comment, so it swallows the whole branch",
     "    # placeholder\r    equals(QT_ARCH, arm64) {\r        QMAKE_CFLAGS += -fopenmp\r        LIBS += -lomp\r    }", "",
     _BOTH_GAPS),
    ("C-A6 -fno-openmp appended to LIBS voids the -fopenmp link credit", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -fopenmp"),
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):LIBS += -fno-openmp", [_LINK_GAP]),
    ("C-A6 -fno-openmp appended to LIBS_PRIVATE voids the -fopenmp link credit", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -fopenmp"),
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):LIBS_PRIVATE += -fno-openmp", [_LINK_GAP]),
    ("C-A6 -fno-openmp appended to QMAKE_LIBS voids the -fopenmp link credit", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -fopenmp"),
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):QMAKE_LIBS += -fno-openmp", [_LINK_GAP]),
    ("C-A6 -fno-openmp appended to QMAKE_LIBS_PRIVATE voids the -fopenmp link credit", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -fopenmp"),
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):QMAKE_LIBS_PRIVATE += -fno-openmp", [_LINK_GAP]),
    ("C-A6 -nodefaultlibs voids the -fopenmp link credit", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -fopenmp"),
     "macx:equals(QT_ARCH, arm64):QMAKE_LFLAGS += -nodefaultlibs", [_LINK_GAP]),
    ("C-A6 an explicit -lomp is not voided by -fno-openmp in LIBS", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):CONFIG(release, debug|release):LIBS += -fno-openmp", []),
    ("C-sibling -fno-openmp inside the compiler command", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_CC = clang -fno-openmp", [_COMPILE_GAP]),
    # sol r3 blocker 1 controls: a replacement function that only READS a tracked variable changes nothing, so it stays readable.
    ("D-B1 control: last() only reads QMAKE_CFLAGS", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):SAVED = $$last(QMAKE_CFLAGS)", []),
    ("D-B1 control: first() only reads LIBS", _ARM64_LIVE, "SAVED = $$first(LIBS)", []),
    ("D-B1 control: clean_path() on an untracked value", _ARM64_LIVE, "SAVED = $$clean_path($$PWD/..)", []),
    ("C-sibling -fno-openmp inside the linker command", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -fopenmp"),
     "macx:equals(QT_ARCH, arm64):QMAKE_LINK = clang++ -fno-openmp", [_LINK_GAP]),
    ("C-A8 a literal true: prefix still opens the branch", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp", opener="true:equals(QT_ARCH, arm64)"), "", []),
    ("C-A8 a literal false: prefix makes a -fno-openmp append unreachable", _ARM64_LIVE,
     "false:macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS += -fno-openmp", []),
    ("C-A8 a literal false: prefix makes a removal unreachable", _ARM64_LIVE,
     "false:macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS -= -fopenmp", []),
    ("C-A8 a negated literal false: prefix is true", _ARM64_LIVE,
     "!false:macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS -= -fopenmp", [_COMPILE_GAP]),
    ("C-A9 an OR of architecture tests is an architecture branch", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp", opener="equals(QT_ARCH, arm64)|equals(QT_ARCH, x86_64)"), "", []),
    ("C-A9 an OR with a platform scope is not an architecture branch", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp", opener="macx|equals(QT_ARCH, arm64)"), "",
     [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP]),
    # sol r8 blocker 1: the QT_ARCH branch's own flag must come from a line that a macx scope AND the architecture test enclose.
    # An architecture block (or prefix) outside macx also guards Linux and Windows builds, so it earns no macOS credit. The empty
    # arm64 section leaves x86_64 as the only architecture branch inside macx.
    ("R8-B1 the arm64 block moved to the top level", "",
     "equals(QT_ARCH, arm64) {\n    QMAKE_CFLAGS += -fopenmp\n    LIBS += -L/opt/homebrew/opt/llvm/lib -lomp\n}",
     [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP]),
    ("R8-B1 top-level inline architecture prefixes", "",
     "equals(QT_ARCH, arm64):QMAKE_CFLAGS += -fopenmp\nequals(QT_ARCH, arm64):LIBS += -lomp",
     [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP]),
    ("R8-B1 only the compile flag moved to a top-level architecture prefix", _arm64_section("LIBS += -lomp"),
     "equals(QT_ARCH, arm64):QMAKE_CFLAGS += -fopenmp", [_LOCAL_COMPILE_GAP]),
    ("R8-B1 an OR with another platform does not confine the arm64 block to macOS", "",
     "macx|linux {\n    equals(QT_ARCH, arm64) {\n        QMAKE_CFLAGS += -fopenmp\n        LIBS += -lomp\n    }\n}",
     [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP]),
    ("R8-B1 control: an architecture block outside with macx prefixes inside is still the branch's own", "",
     "equals(QT_ARCH, arm64) {\n    macx:QMAKE_CFLAGS += -fopenmp\n    macx:LIBS += -lomp\n}", []),
    ("R8-B1 control: an architecture prefix with macx after it is still the branch's own", "",
     "equals(QT_ARCH, arm64):macx:QMAKE_CFLAGS += -fopenmp\nequals(QT_ARCH, arm64):macx:LIBS += -lomp", []),
    ("R8-B1 control: nested macx inside an architecture block inside a top-level scope", "",
     "true {\n    equals(QT_ARCH, arm64) {\n        macx {\n            QMAKE_CFLAGS += -fopenmp\n"
     "            LIBS += -lomp\n        }\n    }\n}", []),
)


_ARM64_BLOCK = "macx:equals(QT_ARCH, arm64) {\n%s\n}"
# (case name, arm64 section, text after the macx scope). The model reads a deliberately narrow qmake grammar and refuses
# everything else with an "unsupported qmake construct" error, so a shape it cannot interpret fails the test loudly
# instead of passing quietly (adversary pass 2 on 1163490a: quoting, aliases, unset/include, selector rewrites, ...).
_MACX_PIN_REFUSED = (
    ("A1 comment-only line inside a removal continuation", _ARM64_LIVE,
     _ARM64_BLOCK % "    QMAKE_CFLAGS -= \\\n        # compiler override\n        -fopenmp"),
    ("A2 quoted removal operand", _ARM64_LIVE, _ARM64_BLOCK % '    QMAKE_CFLAGS -= "-fopenmp"\n    LIBS -= "-lomp"'),
    ("A2 quoted addition", _arm64_section('QMAKE_CFLAGS += "-fopenmp"', 'LIBS += "-lomp"'), ""),
    ("A3 removal through a $$ alias", _ARM64_LIVE,
     _ARM64_BLOCK % "    OMP_C = -fopenmp\n    QMAKE_CFLAGS -= $$OMP_C\n    LIBS -= $${OMP_C}"),
    ("A4 flag value copied from another variable", _ARM64_LIVE,
     _ARM64_BLOCK % "    CONFIG(release, debug|release):QMAKE_LFLAGS += -fopenmp\n    QMAKE_CFLAGS -= $$QMAKE_LFLAGS"),
    ("A4 CONFIG gains a token", _ARM64_LIVE, "CONFIG += release"),
    ("A5 unset()", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):unset(QMAKE_CFLAGS)"),
    ("A5 conditional include()", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):include(omp-override.pri)"),
    ("A6 QT_ARCH reassigned", _ARM64_LIVE, "equals(QT_ARCH, arm64):QT_ARCH ="),
    ("A6 CONFIG gains the linux scope name", _ARM64_LIVE,
     _ARM64_BLOCK % "    CONFIG += linux\n    linux:QMAKE_CFLAGS -= -fopenmp"),
    ("A7 doubled negation", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp", opener="!!equals(QT_ARCH, x86_64)"), ""),
    ("A8 quoted architecture operand", _ARM64_LIVE, 'macx:equals(QT_ARCH, "arm64e"):DEFINES += ARM64E_BUILD'),
    ("A8 quoted operand on the live opener", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp", opener='equals(QT_ARCH, "arm64")'), ""),
    ("A8 selector text inside a message()", _ARM64_LIVE, 'message("For example, equals(QT_ARCH, example_arch)")'),
    ("A9 '=' inside a condition", _ARM64_LIVE,
     _ARM64_BLOCK % '    OMP_MODE = openmp=off\n    equals(OMP_MODE, "openmp=off"):QMAKE_CFLAGS -= -fopenmp'),
    ("A10 quoted brace", _ARM64_LIVE, 'message("A left brace is {")'),
    ("A10 unterminated $${ reference", _ARM64_LIVE, "DEFINES += $${BROKEN\n"),
    ("A12 ~= substitution", _ARM64_LIVE, _ARM64_BLOCK % "    QMAKE_CFLAGS ~= s/^-std=c99$/-std=gnu99/"),
    # sol r1 blocker 3: a condition that CALLS a function can change a tracked variable as a side effect.
    ("B3 unset() as a brace-scope condition", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):unset(LIBS) { }"),
    ("B3 unset() as an inline condition before an assignment", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):unset(QMAKE_CFLAGS):DEFINES += DIAGNOSTICS"),
    ("B3 unset() inside the branch", _ARM64_LIVE, _ARM64_BLOCK % "    unset(LIBS):DEFINES += DIAGNOSTICS"),
    ("B3 system() as an inline condition", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):system(true):DEFINES += DIAGNOSTICS"),
    ("B3 for() loop header", _ARM64_LIVE, "for(item, FLAG_LIST) {\n    DEFINES += DIAGNOSTICS\n}"),
    ("B3 contains() of another variable", _ARM64_LIVE, "contains(OMP_MODE, off):DEFINES += DIAGNOSTICS"),
    ("B3 include() with no directory to resolve it in", _ARM64_LIVE, "include(openmp-override.pri)"),
    ("B3 include() inside a scope", _ARM64_LIVE, _ARM64_BLOCK % "    include(openmp-override.pri)"),
    # sol r2 (adversary pass 3 on ef997fb3).
    ("C-A1 unset() hidden behind a mixed | and : header", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):unset(QMAKE_CFLAGS)|win32 {\n    DEFINES += CHECK\n}"),
    ("C-A1 unset() hidden behind a mixed | and : inline prefix", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):unset(LIBS)|win32:DEFINES += CHECK"),
    ("C-A2 CONFIG loses a selector through a $$ alias", _ARM64_LIVE, "PLATFORM_ALIAS = macx\nCONFIG -= $$PLATFORM_ALIAS"),
    ("C-A2 CONFIG loses a quoted selector", _ARM64_LIVE, 'CONFIG -= "macx"'),
    ("C-A2 CONFIG gains a quoted token", _ARM64_LIVE, "CONFIG += '$$X'"),
    ("C-A4 a non-breaking space is not a qmake separator", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp  -O2", "LIBS += -lomp"), ""),
    ("C-A4 a form feed is not a qmake separator", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp\x0c -O2", "LIBS += -lomp"), ""),
    ("C-A3 a bare CR inside a live line", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp\r        LIBS += -lomp"), ""),
    ("C-A7 a response file hides a -fno-openmp from the compile line", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp @omp.rsp", "LIBS += -lomp"), ""),
    ("C-A7 a response file hides a -fno-openmp from the link line", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -fopenmp @omp.rsp"), ""),
    ("C-A12 an unterminated quote in an untracked assignment", _ARM64_LIVE, 'PIN_NOTE = "unterminated\n'),
    ("C-A12 an unterminated function call in an untracked assignment", _ARM64_LIVE, "PIN_NOTE = $$clean_path(foo\n"),
    ("C-A12 a stray closing parenthesis in an untracked assignment", _ARM64_LIVE, "PIN_NOTE = foo)\n"),
    # sol r3 blocker 1: take_first()/take_last() REMOVE a value from the variable they read, so an assignment to an unwatched
    # variable that expands one can still strip -fopenmp or -lomp. Every operator, every tracked variable, every spelling.
    ("D-B1 take_last(QMAKE_CFLAGS) saved into an untracked variable", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):SAVED = $$take_last(QMAKE_CFLAGS)"),
    ("D-B1 take_last(LIBS) saved into an untracked variable", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):SAVED = $$take_last(LIBS)"),
    ("D-B1 take_first(QMAKE_LFLAGS) saved into an untracked variable", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):SAVED = $$take_first(QMAKE_LFLAGS)"),
    ("D-B1 take_last() with +=", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):SAVED += $$take_last(LIBS)"),
    ("D-B1 take_last() with *=", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):SAVED *= $$take_last(LIBS)"),
    ("D-B1 take_last() with -=", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):SAVED -= $$take_last(LIBS)"),
    ("D-B1 take_last() with no condition", _ARM64_LIVE, "SAVED = $$take_last(QMAKE_CFLAGS)"),
    ("D-B1 take_last() inside the branch", _ARM64_LIVE, _ARM64_BLOCK % "    SAVED = $$take_last(QMAKE_CFLAGS)"),
    ("D-B1 take_last() under a condition that is false for arm64", _ARM64_LIVE,
     "win32:SAVED = $$take_last(QMAKE_CFLAGS)"),
    ("D-B1 braced $${take_last(...)} expansion", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):SAVED = $${take_last(LIBS)}"),
    ("D-B1 take_last() as a later argument of another expansion", _ARM64_LIVE,
     "SAVED = $$join(KEEP, $$take_last(LIBS))"),
    ("D-B1 a function name assembled from another expansion", _ARM64_LIVE, "SAVED = $$take_$$WHICH(LIBS)"),
    ("D-B1 take_last() in a tracked variable's own value", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS_RELEASE = $$take_last(QMAKE_CFLAGS)"),
    # opus r4 blocker (DC-01 class): $$system(cmd, mode, stsvar) writes the exit status into stsvar, so an assignment to an
    # untracked variable can overwrite LIBS / QMAKE_CFLAGS / QMAKE_LFLAGS with "0". The class is "a value-side replace
    # function that is not known to only read", so the model allowlists readers instead of denylisting writers.
    ("E-B1 $$system() status variable is LIBS, conditional", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):SAVED = $$system(true, lines, LIBS)"),
    ("E-B1 $$system() status variable is QMAKE_CFLAGS, unconditional", _ARM64_LIVE,
     "SAVED = $$system(true, lines, QMAKE_CFLAGS)"),
    ("E-B1 braced $${system(...)} with QMAKE_LFLAGS", _ARM64_LIVE,
     'SAVED = $${system("true", blob, QMAKE_LFLAGS)}'),
    ("E-B1 $$system() with += and a status variable", _ARM64_LIVE, "SAVED += $$system(true, lines, LIBS)"),
    ("E-B1 $$system() inside the branch", _ARM64_LIVE, _ARM64_BLOCK % "    SAVED = $$system(true, lines, LIBS)"),
    ("E-B1 $$system() as a later argument of another expansion", _ARM64_LIVE,
     "SAVED = $$join(KEEP, $$system(true, lines, LIBS))"),
    ("E-B1 $$system() under a condition that is false for arm64", _ARM64_LIVE,
     "win32:SAVED = $$system(true, lines, LIBS)"),
    ("E-B1 $$system() with no status variable is still not a known reader", _ARM64_LIVE, "SAVED = $$system(true)"),
    ("E-B1 a replace function the model does not know", _ARM64_LIVE, "SAVED = $$somethingElse(LIBS)"),
    ("E-B1 a Qt-defined replace function the model does not know", _ARM64_LIVE, "SAVED = $$qtLibraryTarget(mlv)"),
    ("E-B1 a braced unknown replace function", _ARM64_LIVE, "SAVED = $${somethingElse(LIBS)}"),
    ("E-B1 $$system() in a tracked variable's own value", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS_RELEASE = $$system(true, lines, LIBS)"),
    # sol r5 A1: a driver option that takes its operand as the NEXT token consumes a following -fopenmp / -lomp as a directory,
    # library or file name, so the token is present in the variable but never reaches the compiler as a flag.
    ("F-A1 -I consumes the -fopenmp after it (the repro)", _arm64_section(
        "QMAKE_CFLAGS += -I -fopenmp", "QMAKE_LFLAGS += -L -fopenmp"), ""),
    ("F-A1 -I on one line, -fopenmp on the next", _arm64_section(
        "QMAKE_CFLAGS += -I", "QMAKE_CFLAGS += -fopenmp", "LIBS += -lomp"), ""),
    ("F-A1 -L consumes the -lomp after it", _arm64_section("QMAKE_CFLAGS += -fopenmp", "LIBS += -L -lomp"), ""),
    ("F-A1 -isystem consumes the -fopenmp after it", _arm64_section(
        "QMAKE_CFLAGS += -isystem -fopenmp", "LIBS += -lomp"), ""),
    ("F-A1 -framework consumes the -lomp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -framework -lomp"), ""),
    ("F-A1 -Xclang is an operand-taking option", _arm64_section(
        "QMAKE_CFLAGS += -Xclang -fopenmp", "LIBS += -lomp"), ""),
    ("F-A1 -o consumes the -fopenmp after it", _arm64_section(
        "QMAKE_CFLAGS = -o -fopenmp", "LIBS += -lomp"), ""),
    ("F-A1 --sysroot consumes the -fopenmp after it", _arm64_section(
        "QMAKE_CFLAGS += --sysroot -fopenmp", "LIBS += -lomp"), ""),
    ("F-A1 -arch consumes the -fopenmp after it", _arm64_section(
        "QMAKE_CFLAGS *= -arch -fopenmp", "LIBS += -lomp"), ""),
    # sol r5 A2: qmake writes the C compile recipe from the QMAKE_RUN_CC* templates and the QMAKE_CC_* pieces, none of which
    # the model reads, so a -fno-openmp carried by any unmodeled QMAKE variable would pass. The class is "a control the model
    # does not track": every assignment to a QMAKE variable it does not model is refused.
    ("F-A2 QMAKE_RUN_CC carries -fno-openmp (the repro)", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_RUN_CC = $(CC) -c $(CFLAGS) -fno-openmp $(INCPATH) -o $obj $src"),
    ("F-A2 QMAKE_RUN_CC_IMP carries -fno-openmp", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_RUN_CC_IMP = $(CC) -c $(CFLAGS) -fno-openmp $(INCPATH) -o $@ $<"),
    ("F-A2 QMAKE_CC_O_FLAG carries -fno-openmp", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_CC_O_FLAG = -fno-openmp -o"),
    ("F-A2 QMAKE_RUN_CC unconditional", _ARM64_LIVE, "QMAKE_RUN_CC = $(CC) -fno-openmp"),
    ("F-A2 QMAKE_RUN_CC under a condition the model cannot decide", _ARM64_LIVE,
     "CONFIG(release, debug|release):QMAKE_RUN_CC = $(CC) -fno-openmp"),
    ("F-A2 QMAKE_RUN_CC with +=", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):QMAKE_RUN_CC += -fno-openmp"),
    ("F-A2 QMAKE_RUN_CC inside the branch", _ARM64_LIVE, _ARM64_BLOCK % "    QMAKE_RUN_CC = $(CC) -fno-openmp"),
    ("F-A2 an extra compiler template", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_EXTRA_COMPILERS += omp_off"),
    ("F-A2 the compiler driver for C++ is not a modelled control either", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_CXXFLAGS += -fno-openmp"),
    ("F-A2 QMAKESPEC", _ARM64_LIVE, "macx:equals(QT_ARCH, arm64):QMAKESPEC = macx-g++"),
    # sol r6 R5-OPERAND-LIST: a list of operand-taking options is never complete (the r5b list missed -iframeworkwithsysroot and
    # ld64's -lazy_framework, which the `-l<name>` spelling hides), so a driver option is accepted only when it is on an allowlist
    # of options that take no operand or carry it joined to themselves. Every other option is refused, listed or not.
    ("G-A1 -iframeworkwithsysroot consumes the -fopenmp after it (the repro)", _arm64_section(
        "QMAKE_CFLAGS += -iframeworkwithsysroot -fopenmp", "LIBS += -lomp"), ""),
    ("G-A1 -lazy_framework consumes the -lomp after it (the repro)", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lazy_framework -lomp"), ""),
    ("G-A1 -lazy_library consumes the -lomp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lazy_library -lomp"), ""),
    ("G-A1 -lto_library consumes the -lomp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -lto_library -lomp"), ""),
    ("G-A1 -force_load consumes the -lomp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -force_load -lomp"), ""),
    ("G-A1 -order_file consumes the -lomp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -order_file -lomp", "LIBS += -lomp"), ""),
    ("G-A1 -bundle_loader consumes the -fopenmp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -bundle_loader -fopenmp"), ""),
    ("G-A1 -umbrella consumes the -lomp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -umbrella -lomp"), ""),
    ("G-A1 -Wl, joins an option to its operand inside one token", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -Wl,-lazy_framework,-lomp"), ""),
    ("G-A1 an option the model has never heard of in the compiler command", _ARM64_LIVE,
     "macx:equals(QT_ARCH, arm64):QMAKE_CC = clang -some-future-option"),
    ("G-A1 a bare dash", _arm64_section("QMAKE_CFLAGS += - -fopenmp", "LIBS += -lomp"), ""),
    ("G-A1 -I with nothing joined to it, on the line before an inherited flag", _arm64_section(
        "QMAKE_CFLAGS += -I", "LIBS += -lomp"), "macx:QMAKE_CFLAGS += -fopenmp"),
    # fable r7 R5-OPERAND-LIST: ld64's -load_hidden takes the next token as its archive path, and it hid behind the `-l<name>`
    # spelling the same way -lazy_framework did. The class is "every ld64 -l* option with a separate operand": all four contain `_`,
    # no library name the project links does, so `_` is not a character of the -l<name> alternative.
    ("H-A1 -load_hidden consumes the -lomp after it (the repro)", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -load_hidden -lomp"), ""),
    ("H-A1 -load_hidden consumes the -lgomp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -load_hidden -lgomp"), ""),
    ("H-A1 -load_hidden consumes the -llibgomp-1 after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -load_hidden -llibgomp-1"), ""),
    ("H-A1 -load_hidden in QMAKE_LFLAGS consumes the -lomp after it", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "QMAKE_LFLAGS += -load_hidden -lomp", "LIBS += -lomp"), ""),
    ("H-A1 -load_hidden on one line, -lomp on the next", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp", "LIBS += -load_hidden", "LIBS += -lomp"), ""),
    ("H-A1 -load_hidden in the project's own other flag variable", _arm64_section(
        "QMAKE_CFLAGS += -fopenmp -load_hidden", "LIBS += -lomp"), ""),
)

# QMAKE variables the project files use that change neither the C compile line nor the link line.
_QMAKE_BENIGN_VARIABLE_LINES = (
    "QMAKE_SUBSTITUTES += mlvapp_test_source_root",
    "win32:QMAKE_RUN_CC = $(CC) -fno-openmp",
    "QMAKE_CFLAGS_RELEASE -= -O2",
    "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS += -g -Wall -I/opt/homebrew/opt/llvm/include -L/opt/lib -O3",
    "QMAKE_CFLAGS += -Dfoo=1 -std=c99 -lomp",
    # Options the allowlist of sol r6 accepts: no operand, or the operand joined to the option.
    "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS += -fPIC -pthread -Os -ggdb -Wno-unused-variable -DNDEBUG -DTEXT=-fopenmp -I/opt/x",
    "macx:equals(QT_ARCH, arm64):LIBS += -L/opt/y -lm -lc++ -llibgomp-1 -lomp-west /opt/z/libextra.a",
)

# Value-side replace functions the model accepts: every one only reads its arguments (the project files the pin reads use
# clean_path, relative_path, files, lower, quote and escape_expand).
_READONLY_VALUE_FUNCTION_LINES = (
    "SAVED = $$clean_path($$PWD/..)",
    "SAVED = $$relative_path($$PWD/x, $$OUT_PWD)",
    "SAVED = $$files($$PWD/x/*.cpp)",
    "SAVED = $$lower($$(MLVAPP_ENABLE_AVX))",
    "SAVED = $$quote($$PWD/x.zip) $$escape_expand(\\n\\t)",
    "SAVED = $${join(LIST, -)}",
)


class RawUint16PerFrameKeysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = MAIN_WINDOW_CPP.read_text(encoding="utf-8")

    def _shared_definition(self) -> str:
        marker = "const QString " + SHARED_KEYS + " ="
        self.assertIn(marker, self.source)
        return _statement_containing(self.source, marker, marker)

    def test_shared_string_carries_all_three_keys_from_stage_timing(self) -> None:
        definition = self._shared_definition()
        for key in PER_FRAME_KEYS:
            self.assertIn(" " + key + "=%", definition)
            self.assertIn('"' + key + '"', definition, "value must come from the frame's timing map")

    def test_gpu_frame_record_appends_the_keys(self) -> None:
        statement = _statement_containing(self.source, '"playback_smoke.gpu_frame ', "qInfo()")
        self.assertIn("+ " + SHARED_KEYS, statement)
        self.assertLess(
            self.source.index("const QString " + SHARED_KEYS + " ="),
            self.source.index('"playback_smoke.gpu_frame '),
            "the shared string must be built before the gpu_frame record uses it")

    def test_cpu_frame_record_appends_the_same_keys(self) -> None:
        statement = _statement_containing(self.source, '"playback_smoke.cpu_frame ', "qInfo()")
        self.assertIn("+ " + SHARED_KEYS, statement)

    def test_start_line_logs_the_prefetch_decoder_knob(self) -> None:
        statement = _statement_containing(self.source, '"playback_smoke.start ', "qInfo()")
        self.assertIn("env_raw_uint16_prefetch_decoders=%", statement)
        self.assertIn('envValueForLog( "MLVAPP_RAW_UINT16_PREFETCH_DECODERS" )', statement)


class Lj92SlowHuffConfigurationTests(unittest.TestCase):
    """LJ92-PRED1-FAST-PATH-1 r11 (adversary A1, introduced major): the u64 predictor-1 reader read self->hufflut and
    self->huffbits unconditionally, but a -DSLOW_HUFF build compiles the ljp struct without those two members, so
    lj92.c stopped compiling in that (legacy, commented-out in the source, never defined by a project) configuration,
    which master still compiled. The legacy configuration stays a compilable one."""

    def _syntax_check(self, *defines: str) -> "subprocess.CompletedProcess[str]":
        compiler = shutil.which("gcc") or shutil.which("cc")
        if compiler is None:
            self.skipTest("no C compiler on PATH")
        return subprocess.run(
            [compiler, "-std=c99", "-fopenmp", "-fsyntax-only", *defines, "-I", str(ROOT / "src"),
             str(ROOT / "src/mlv/liblj92/lj92.c")],
            capture_output=True, text=True, timeout=120, check=False)

    def test_lj92_compiles_in_the_default_configuration(self) -> None:
        result = self._syntax_check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lj92_compiles_with_slow_huff_defined(self) -> None:
        result = self._syntax_check("-DSLOW_HUFF")
        self.assertEqual(result.returncode, 0, result.stderr)

    # A 1x1 8-bit predictor-1 frame whose DHT holds the symbols {0, 1} at code length 2 (so the largest SSSS is 1).
    _SLOW_HUFF_DRIVER = r"""
#include <stdint.h>
#include <stdio.h>
#include "mlv/liblj92/lj92.h"
int main(void)
{
    uint8_t frame[] = {
        0xFF, 0xD8,
        0xFF, 0xC4, 0x00, 0x15, 0x00, 0,2,0,0,0,0,0,0,0,0,0,0,0,0,0,0, 0x00, 0x01,
        0xFF, 0xC3, 0x00, 0x0B, 0x08, 0x00, 0x01, 0x00, 0x01, 0x01, 0x01, 0x11, 0x00,
        0xFF, 0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x01, 0x00, 0x00,
        0x00, 0x00, 0xFF, 0xD9 };
    lj92 h = NULL;
    int w = 0, hh = 0, bits = 0, comps = 0;
    uint16_t out[1] = { 0xA5A5u };
    if (lj92_open(&h, frame, (int)sizeof(frame), &w, &hh, &bits, &comps) != LJ92_ERROR_NONE) { puts("open failed"); return 2; }
    if (lj92_decode(h, out, 1, 0, NULL, 0) != LJ92_ERROR_NONE) { puts("decode failed"); return 3; }
    printf("max_ssss=%d\n", lj92_get_last_huff_max_ssss());
    lj92_close(h);
    return lj92_get_last_huff_max_ssss() == 1 ? 0 : 1;
}
"""

    def _build_and_run_slow_huff_driver(self, *defines: str) -> "subprocess.CompletedProcess[str]":
        compiler = shutil.which("gcc") or shutil.which("cc")
        if compiler is None:
            self.skipTest("no C compiler on PATH")
        with tempfile.TemporaryDirectory() as scratch:
            driver = Path(scratch) / "slow_huff_driver.c"
            driver.write_text(self._SLOW_HUFF_DRIVER, encoding="utf-8")
            exe = Path(scratch) / ("slow_huff_driver.exe" if os.name == "nt" else "slow_huff_driver")
            build = subprocess.run(
                [compiler, "-std=c99", "-fopenmp", *defines, "-I", str(ROOT / "src"), str(driver),
                 str(ROOT / "src/mlv/liblj92/lj92.c"), "-o", str(exe)],
                capture_output=True, text=True, timeout=240, check=False)
            self.assertEqual(build.returncode, 0, build.stderr)
            return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60, check=False)

    def test_last_huff_max_ssss_is_reported_in_the_default_configuration(self) -> None:
        result = self._build_and_run_slow_huff_driver()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_last_huff_max_ssss_is_reported_with_slow_huff_defined(self) -> None:
        """LJ92-PRED1-DECODER-ONLY-1 r2b (sol r1 blocker S4): the new getter stayed -1 in a -DSLOW_HUFF build even though
        the DHT names a maximum symbol; the scan is stamped in both ljp variants."""
        result = self._build_and_run_slow_huff_driver("-DSLOW_HUFF")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @staticmethod
    def _platform_scopes(pro_text: str) -> "dict[str, str]":
        """Top-level `name { ... }` qmake scopes of a .pro file, name -> body (nested scopes stay in the body). A `#` comment
        is dropped from each LF line first: qmake never sees it, so a brace or a flag inside one is neither a delimiter nor a
        setting (sol r5 hardening LJ92-OPENMP-SCOPE-TEST-COMMENT-BLIND-1)."""
        pro_text = "\n".join(line.split("#", 1)[0] for line in pro_text.split("\n"))
        scopes: "dict[str, str]" = {}
        depth = 0
        name = ""
        body_start = 0
        header_start = 0
        for index, char in enumerate(pro_text):
            if char == "{":
                if depth == 0:
                    name = pro_text[header_start:index].strip()
                    body_start = index + 1
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    # a second scope of the same name is additive in qmake, so its body joins the first one's
                    scopes[name] = scopes.get(name, "") + "\n" + pro_text[body_start:index]
                    header_start = index + 1
            elif char == "\n" and depth == 0:
                header_start = index + 1
        return scopes

    def test_standalone_lj92_targets_link_openmp_on_every_declared_platform(self) -> None:
        """LJ92-PRED1-DECODER-ONLY-1 r1 (adversary A1, introduced major): lj92.c reaches omp_get_wtime through
        mlv_stage_timing_now's fallbacks (src/debug/StageTiming.h), so a standalone target that compiles it needs OpenMP
        on its compile AND link line in every platform scope it declares, as pipeline_tests.pro does. The reader tool's
        win32 scope had it; its linux-g++* and macx scopes, and every scope of the fuzz target, did not."""
        for relative in ("tests/console/lj92_pred1_reader_tool.pro", "tests/fuzz/fuzz_lj92.pro"):
            scopes = self._platform_scopes((ROOT / relative).read_text(encoding="utf-8"))
            for platform in ("win32", "linux-g++*", "macx"):
                self.assertIn(platform, scopes, relative + " lost its " + platform + " scope")
                body = scopes[platform]
                self.assertRegex(body, r"QMAKE_CFLAGS\s*\+=[^\n]*-fopenmp", relative + " " + platform + ": C compile lacks -fopenmp")
                self.assertRegex(
                    body, r"(QMAKE_LFLAGS\s*\+=[^\n]*-fopenmp|LIBS\s*\+=[^\n]*-l(libgomp-1|gomp|omp)\b)",
                    relative + " " + platform + ": link line lacks OpenMP")

    def test_standalone_lj92_targets_pin_openmp_for_each_macos_architecture(self) -> None:
        """LJ92-PRO-MACOS-ARCH-PIN-1 r1 (Dot advisory 9dab5e8d on #880): the test above reads the WHOLE macx body, so
        a flag that survives in only one QT_ARCH branch still passes and the other macOS architecture can lose OpenMP
        while CI stays green. For every architecture the .pro names (x86_64 and arm64 always), the flags qmake would
        really apply on macOS (live lines, not comments; exact tokens; removals honoured; include() files followed)
        must include the C compile flag and an OpenMP link flag, each set by a line of that QT_ARCH branch itself (sol r1
        on #888: a flag inherited from the enclosing macx scope does not count), and no line whose configuration the
        model cannot decide may append -fno-openmp. Quoted lines read, in both files: `equals(QT_ARCH, x86_64) {` and
        `equals(QT_ARCH, arm64) {`, each holding `QMAKE_CFLAGS += -fopenmp` and `LIBS += -L<llvm>/lib -lomp`. A construct
        outside the model's grammar (a condition that calls a function, an include it cannot read) fails the test."""
        for relative in ("tests/console/lj92_pred1_reader_tool.pro", "tests/fuzz/fuzz_lj92.pro"):
            pro_text = _read_qmake_file(ROOT / relative)
            named = {"x86_64", "arm64"} | _qmake_named_architectures(pro_text, (ROOT / relative).parent)
            for arch in sorted(named):
                for gap in _project_openmp_gaps(ROOT / relative, arch, pro_text):
                    self.fail(relative + " macx QT_ARCH=" + arch + ": " + gap)

    def test_macos_arch_pin_model_rejects_every_shape_qmake_would_not_honour(self) -> None:
        """The same check, fed the shapes the adversary pass showed a raw-text match accepts. Every case is judged for
        macOS arm64 (x86_64 stays live in the template) and must report exactly the listed gaps."""
        for name, arm64_section, tail, expected in _MACX_PIN_CASES:
            with self.subTest(name):
                text = _MACX_PIN_TEMPLATE % {"arm64": arm64_section, "tail": tail}
                self.assertEqual(_macos_openmp_gaps(text, "arm64"), expected)
                self.assertEqual(_macos_openmp_gaps(text, "x86_64"), [], "x86_64 must stay live in every case")

    def test_macos_arch_pin_model_refuses_unbalanced_scopes(self) -> None:
        for text in ("macx {\n    QMAKE_CFLAGS += -fopenmp\n", "macx {\n}\n}\n"):
            with self.subTest(text):
                with self.assertRaises(AssertionError):
                    _qmake_effective_flags(text, "arm64")

    def test_macos_arch_pin_model_refuses_constructs_it_cannot_interpret(self) -> None:
        """Fail closed: a shape outside the narrow grammar raises 'unsupported qmake construct' (or an unterminated
        scope), for either architecture, so nothing it cannot read can leave the pin passing."""
        for name, arm64_section, tail in _MACX_PIN_REFUSED:
            with self.subTest(name):
                text = _MACX_PIN_TEMPLATE % {"arm64": arm64_section, "tail": tail}
                with self.assertRaisesRegex(AssertionError, "unsupported qmake construct|unterminated"):
                    _macos_openmp_gaps(text, "arm64")

    def test_macos_arch_pin_model_still_reads_value_functions_that_only_read(self) -> None:
        """The allowlist is not a blanket refusal: a project using the read-only replace functions still gets judged."""
        for line in _READONLY_VALUE_FUNCTION_LINES:
            for arch in ("arm64", "x86_64"):
                with self.subTest(line + " " + arch):
                    text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": line}
                    self.assertEqual(_macos_openmp_gaps(text, arch), [])

    def test_macos_arch_pin_model_still_reads_benign_qmake_variables_and_joined_option_operands(self) -> None:
        """The refusals of sol r5 are not blanket: QMAKE_SUBSTITUTES, a QMAKE control on a platform that is not macOS, and
        options whose operand is joined to them (-I/path, -L/path, -Dfoo=1) are still judged, so the real projects pass."""
        for line in _QMAKE_BENIGN_VARIABLE_LINES:
            for arch in ("arm64", "x86_64"):
                with self.subTest(line + " " + arch):
                    text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": line}
                    self.assertEqual(_macos_openmp_gaps(text, arch), [])

    def test_macos_arch_pin_model_keeps_disablers_and_inherited_flags_per_architecture(self) -> None:
        """A disabler aimed at one architecture is that architecture's gap only; a flag one architecture inherits from the
        parent scope does not hide that the other architecture's branch carries its own."""
        disabled_x86 = _MACX_PIN_TEMPLATE % {
            "arm64": _ARM64_LIVE,
            "tail": "macx:equals(QT_ARCH, x86_64):CONFIG(release, debug|release):QMAKE_CFLAGS_RELEASE += -fno-openmp"}
        self.assertEqual(_macos_openmp_gaps(disabled_x86, "arm64"), [])
        self.assertEqual(_macos_openmp_gaps(disabled_x86, "x86_64"), [_COMPILE_GAP])
        parent_removed = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "macx:QMAKE_CFLAGS -= -fopenmp"}
        for arch in ("arm64", "x86_64"):
            self.assertEqual(_macos_openmp_gaps(parent_removed, arch), [_COMPILE_GAP])
        # A branch's *= after the parent already added the token still counts as the branch's own flag line.
        starred = ("macx {\n    QMAKE_CFLAGS += -fopenmp\n    LIBS += -lomp\n    equals(QT_ARCH, arm64) {\n"
                   "        QMAKE_CFLAGS *= -fopenmp\n        LIBS *= -lomp\n    }\n}\n")
        self.assertEqual(_macos_openmp_gaps(starred, "arm64"), [])
        self.assertEqual(_macos_openmp_gaps(starred, "x86_64"), [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP])

    def _project_with_include(self, scratch: str, include_line: str, files: "dict[str, str]") -> Path:
        root = Path(scratch)
        for name, text in files.items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        project = root / "sub" / "target.pro"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text(_MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": include_line}, encoding="utf-8")
        return project

    def test_macos_arch_pin_follows_include_files_where_qmake_evaluates_them(self) -> None:
        """sol r1 blocker 3: an include() the model skipped could hold a removal. The included text is evaluated in place,
        relative to the including file; an unreadable include is a failure, never a pass."""
        removal = "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS -= -fopenmp\nmacx:equals(QT_ARCH, arm64):LIBS -= -lomp\n"
        with tempfile.TemporaryDirectory() as scratch:
            project = self._project_with_include(
                scratch, "include(../openmp-override.pri)", {"openmp-override.pri": removal})
            self.assertEqual(_project_openmp_gaps(project, "arm64", checkout_root=Path(scratch)), _BOTH_GAPS)
            self.assertEqual(_project_openmp_gaps(project, "x86_64", checkout_root=Path(scratch)), [])
        with tempfile.TemporaryDirectory() as scratch:
            project = self._project_with_include(
                scratch, "include(../outer.pri)",
                {"outer.pri": "include(inner/inner.pri)\n", "inner/inner.pri": removal})
            self.assertEqual(_project_openmp_gaps(project, "arm64", checkout_root=Path(scratch)), _BOTH_GAPS)
        with tempfile.TemporaryDirectory() as scratch:
            project = self._project_with_include(
                scratch, "include(../defaults.pri)",
                {"defaults.pri": "REPO_ROOT = $$clean_path($$PWD/..)\nCONFIG += c++17 console\nCONFIG -= app_bundle\n"
                                 "helper.input = $$PWD/helper.h.in\nSOURCES += $$REPO_ROOT/x.c \\\n    $$REPO_ROOT/y.c\n"})
            self.assertEqual(_project_openmp_gaps(project, "arm64", checkout_root=Path(scratch)), [])

    def test_macos_arch_pin_refuses_include_files_it_cannot_interpret(self) -> None:
        unreadable = (
            ("a missing include", "include(../nothing-here.pri)", {}),
            ("an include that unsets a tracked variable", "include(../u.pri)",
             {"u.pri": "macx:equals(QT_ARCH, arm64):unset(LIBS) { }\n"}),
            ("an include that quotes a flag", "include(../q.pri)", {"q.pri": 'QMAKE_CFLAGS -= "-fopenmp"\n'}),
            ("an include that adds a platform name to CONFIG", "include(../c.pri)", {"c.pri": "CONFIG += linux\n"}),
            ("an include loop", "include(../loop.pri)", {"loop.pri": "include(loop.pri)\n"}),
            ("an include named through a variable", "include($$PWD/../v.pri)", {"v.pri": "DEFINES += X\n"}),
            ("an include whose $$system() status variable is LIBS", "include(../s.pri)",
             {"s.pri": "macx:equals(QT_ARCH, arm64):SAVED = $$system(true, lines, LIBS)\n"}),
            ("an include whose $$system() status variable is QMAKE_CFLAGS", "include(../s.pri)",
             {"s.pri": "SAVED = $$system(true, lines, QMAKE_CFLAGS)\n"}),
        )
        for name, include_line, files in unreadable:
            with self.subTest(name), tempfile.TemporaryDirectory() as scratch:
                project = self._project_with_include(scratch, include_line, files)
                with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                    _project_openmp_gaps(project, "arm64", checkout_root=Path(scratch))

    # sol r3 blocker 2: an include path that is not relative to the including file (a UNC share, a drive, a root path) would
    # send the synchronous read off this machine's checkout, onto a network share with no deadline.
    _NON_LOCAL_INCLUDES = (
        "//build-share/qt/openmp.pri",
        "\\\\build-share\\qt\\openmp.pri",
        "//?/UNC/build-share/qt/openmp.pri",
        "C:/qt/openmp.pri",
        "C:openmp.pri",
        "c:\\qt\\openmp.pri",
        "/opt/qt/openmp.pri",
        "\\qt\\openmp.pri",
        "..\\common\\openmp.pri",
    )

    def _reads_made_by(self, project_text: str, directory: Path, checkout_root: Path = ROOT) -> "list[Path]":
        """Judge project_text with every file read intercepted; returns the paths the model tried to open."""
        reads: "list[Path]" = []

        def spy(path: Path) -> str:
            reads.append(path)
            raise FileNotFoundError(str(path))

        with mock.patch.dict(globals(), {"_read_qmake_file": spy}):
            with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                _macos_openmp_gaps(project_text, "arm64", directory, checkout_root=checkout_root)
        return reads

    def test_macos_arch_pin_refuses_include_paths_that_leave_the_checkout_before_any_read(self) -> None:
        directory = Path("proj") / "sub"
        for relative in self._NON_LOCAL_INCLUDES:
            with self.subTest("root project " + relative):
                text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(" + relative + ")"}
                self.assertEqual(
                    self._reads_made_by(text, directory, Path("proj")), [], "a non-local include must be refused unread")
            with self.subTest("nested include " + relative):
                with tempfile.TemporaryDirectory() as scratch:
                    root = Path(scratch)
                    (root / "outer.pri").write_text("include(" + relative + ")\n", encoding="utf-8")
                    reads: "list[Path]" = []
                    real_read = _read_qmake_file

                    def spy(path: Path) -> str:
                        reads.append(path)
                        if path.name == "outer.pri":
                            return real_read(path)
                        raise FileNotFoundError(str(path))

                    text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(outer.pri)"}
                    with mock.patch.dict(globals(), {"_read_qmake_file": spy}):
                        with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                            _macos_openmp_gaps(text, "arm64", root, checkout_root=root)
                    self.assertEqual([path.name for path in reads], ["outer.pri"], "only the local include may be read")

    # sol r6 blocker: a relative path that climbs out of the checkout (`../../../outside.pri` from tests/fuzz) is local in spelling
    # yet opens a file the repository does not own, so the read must be refused before it happens, at the root project and at
    # every nesting depth. The checkout root is the repository root unless the caller names another one.
    _CLIMBING_INCLUDES = (
        "../../../outside.pri",
        "../../..",
        "sub/../../../../outside.pri",
        "../../../../../../../../outside.pri",
        "a/../../../../outside.pri",
    )

    def test_macos_arch_pin_refuses_include_paths_that_climb_out_of_the_checkout_before_any_read(self) -> None:
        directory = ROOT / "tests" / "fuzz"
        for relative in self._CLIMBING_INCLUDES:
            with self.subTest("root project " + relative):
                text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(" + relative + ")"}
                self.assertEqual(self._reads_made_by(text, directory), [], "an include outside the checkout must be refused unread")
            with self.subTest("nested include " + relative):
                reads: "list[Path]" = []

                def spy(path: Path) -> str:
                    reads.append(path)
                    if path.name == "outer.pri":
                        return "include(" + relative + ")\n"
                    raise FileNotFoundError(str(path))

                text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(outer.pri)"}
                with mock.patch.dict(globals(), {"_read_qmake_file": spy}):
                    with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                        _macos_openmp_gaps(text, "arm64", directory, checkout_root=ROOT)
                self.assertEqual([path.name for path in reads], ["outer.pri"], "only the include inside the checkout may be read")

    def test_macos_arch_pin_refuses_a_climb_from_a_nested_include_that_leaves_only_by_two_hops(self) -> None:
        """Each hop alone stays inside (`../` from tests/fuzz/sub lands in tests/fuzz); the sum does not."""
        directory = ROOT / "tests" / "fuzz"
        reads: "list[Path]" = []

        def spy(path: Path) -> str:
            reads.append(path)
            if path.name == "outer.pri":
                return "include(../../../../../outside.pri)\n"
            raise FileNotFoundError(str(path))

        text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(deep/er/outer.pri)"}
        with mock.patch.dict(globals(), {"_read_qmake_file": spy}):
            with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                _macos_openmp_gaps(text, "arm64", directory, checkout_root=ROOT)
        self.assertEqual([path.name for path in reads], ["outer.pri"])

    def test_macos_arch_pin_refuses_a_checkout_symlink_that_leads_out_of_it(self) -> None:
        """qmake cleans the path lexically but the operating system then follows a link on the way: an in-checkout link to a
        directory elsewhere would send the read there."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "checkout"
            outside = Path(scratch) / "outside"
            (root / "sub").mkdir(parents=True)
            outside.mkdir()
            (outside / "x.pri").write_text("", encoding="utf-8")
            try:
                os.symlink(outside, root / "sub" / "link", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("this host cannot create a directory symlink")
            text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(link/x.pri)"}
            with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                _macos_openmp_gaps(text, "arm64", root / "sub", checkout_root=root)

    def test_macos_arch_pin_refuses_an_include_that_is_not_a_regular_file(self) -> None:
        """A synchronous read of a FIFO or device blocks with no deadline, and a huge file stalls the gate: only a regular
        file of sane size is read."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "dir.pri").mkdir()
            (root / "big.pri").write_bytes(b"#" * (2 * 1024 * 1024))
            for name in ("dir.pri", "big.pri"):
                with self.subTest(name):
                    text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(" + name + ")"}
                    with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                        _macos_openmp_gaps(text, "arm64", root, checkout_root=root)

    def test_macos_arch_pin_gives_no_credit_to_an_x86_64_block_moved_outside_macx(self) -> None:
        """sol r8 B1, the other architecture: the same move for x86_64 (arm64 stays intact inside macx) is x86_64's gap only."""
        text = ("macx {\n    QMAKE_CFLAGS += -std=c99\n" + _ARM64_LIVE + "\n}\n"
                "equals(QT_ARCH, x86_64) {\n    QMAKE_CFLAGS += -fopenmp\n    LIBS += -L/usr/local/opt/llvm/lib -lomp\n}\n")
        self.assertEqual(_macos_openmp_gaps(text, "arm64"), [])
        self.assertEqual(_macos_openmp_gaps(text, "x86_64"), [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP])

    def test_macos_arch_pin_gives_no_credit_to_an_included_architecture_block_outside_macx(self) -> None:
        """sol r8 B1 through include(): an include() at the top level whose file holds the arm64 block is outside macx too."""
        block = "equals(QT_ARCH, arm64) {\n    QMAKE_CFLAGS += -fopenmp\n    LIBS += -lomp\n}\n"
        with tempfile.TemporaryDirectory() as scratch:
            (Path(scratch) / "arch.pri").write_text(block, encoding="utf-8")
            project = Path(scratch) / "sub" / "target.pro"
            project.parent.mkdir()
            project.write_text(_MACX_PIN_TEMPLATE % {"arm64": "", "tail": "include(../arch.pri)"}, encoding="utf-8")
            self.assertEqual(
                _project_openmp_gaps(project, "arm64", checkout_root=Path(scratch)), [_LOCAL_COMPILE_GAP, _LOCAL_LINK_GAP])

    @staticmethod
    def _stat_result(size: int, kind: int = stat.S_IFREG) -> os.stat_result:
        return os.stat_result((kind | 0o644, 0, 0, 1, 0, 0, size, 0, 0, 0))

    def test_macos_arch_pin_reader_judges_the_file_it_opened_not_the_pathname_it_checked(self) -> None:
        """sol r8 B2: the pathname was stat()ed, then opened a second time by read_bytes(), so the file could be replaced between
        the two (a larger file; a link to a FIFO that blocks the open). The reader now opens once and judges the descriptor: a
        stat of the pathname that lies, or an fstat that lies, changes nothing, and the read itself stops one byte past the
        limit."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            big = root / "big.pri"
            big.write_bytes(b"#" * (_QMAKE_MAX_FILE_BYTES + 1))
            small = root / "small.pri"
            small.write_bytes(b"DEFINES += X\n")
            lie_target = os.path.normcase(os.path.abspath(str(big)))
            real_stat, real_path_stat = os.stat, Path.stat

            def lying_stat(path, *args, **kwargs):
                if os.path.normcase(os.path.abspath(str(path))) == lie_target:
                    return self._stat_result(32)
                return real_stat(path, *args, **kwargs)

            def lying_path_stat(path, *args, **kwargs):
                if os.path.normcase(os.path.abspath(str(path))) == lie_target:
                    return self._stat_result(32)
                return real_path_stat(path, *args, **kwargs)

            with self.subTest("the pathname stat reports a 32-byte regular file, the open yields more than the limit"):
                with mock.patch.object(os, "stat", lying_stat), mock.patch.object(Path, "stat", lying_path_stat):
                    with self.assertRaisesRegex(OSError, "larger than"):
                        _read_qmake_file(big)
            with self.subTest("the descriptor itself reports 32 bytes: the read is still bounded"):
                with mock.patch.object(os, "fstat", lambda _descriptor: self._stat_result(32)):
                    with self.assertRaisesRegex(OSError, "larger than"):
                        _read_qmake_file(big)
            with self.subTest("the descriptor is not a regular file (a link swapped to a FIFO)"):
                with mock.patch.object(os, "fstat", lambda _descriptor: self._stat_result(0, stat.S_IFIFO)):
                    with self.assertRaisesRegex(OSError, "not a regular file"):
                        _read_qmake_file(small)
            with self.subTest("a file of exactly the limit is still read"):
                exact = root / "exact.pri"
                exact.write_bytes(b"#" * _QMAKE_MAX_FILE_BYTES)
                self.assertEqual(len(_read_qmake_file(exact)), _QMAKE_MAX_FILE_BYTES)
            with self.subTest("a small file is read through its single open, never by pathname again"):
                def forbidden(*_args, **_kwargs):
                    raise AssertionError("the pathname was used again after the open")
                with mock.patch.object(Path, "stat", forbidden), mock.patch.object(Path, "read_bytes", forbidden), \
                        mock.patch.object(Path, "open", forbidden), mock.patch.object(Path, "read_text", forbidden):
                    self.assertEqual(_read_qmake_file(small), "DEFINES += X\n")
            with self.subTest("the open asks not to block (a FIFO swapped in is refused, never waited on)"):
                sentinel, seen_flags, real_open = 1 << 30, [], os.open

                def recording_open(path, flags, *args, **kwargs):
                    seen_flags.append(flags)
                    return real_open(path, flags & ~sentinel, *args, **kwargs)

                with mock.patch.object(os, "O_NONBLOCK", sentinel, create=True), mock.patch.object(os, "open", recording_open):
                    _read_qmake_file(small)
                self.assertEqual(len(seen_flags), 1, "exactly one open of the pathname")
                self.assertTrue(seen_flags[0] & sentinel, "O_NONBLOCK must be among the open flags")

    def test_macos_arch_pin_refuses_an_include_enlarged_between_the_check_and_the_open(self) -> None:
        """sol r8 B2 end to end: direct and nested include(), where the pathname stat said 32 bytes and the open yields more than
        the limit. The pin must refuse, not read the enlarged file and stay green."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "big.pri").write_bytes(b"#" * (_QMAKE_MAX_FILE_BYTES + 1))
            (root / "outer.pri").write_text("include(big.pri)\n", encoding="utf-8")
            lie_target = os.path.normcase(os.path.abspath(str(root / "big.pri")))
            real_stat, real_path_stat = os.stat, Path.stat

            def lying(real):
                def stat_of(path, *args, **kwargs):
                    if os.path.normcase(os.path.abspath(str(path))) == lie_target:
                        return self._stat_result(32)
                    return real(path, *args, **kwargs)
                return stat_of

            for name, include in (("direct", "include(big.pri)"), ("nested", "include(outer.pri)")):
                with self.subTest(name):
                    text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": include}
                    with mock.patch.object(os, "stat", lying(real_stat)), mock.patch.object(Path, "stat", lying(real_path_stat)):
                        with self.assertRaisesRegex(AssertionError, "unsupported qmake construct.*larger than"):
                            _macos_openmp_gaps(text, "arm64", root, checkout_root=root)

    def test_macos_arch_pin_link_guard_is_tested_without_needing_the_symlink_right(self) -> None:
        """fable r7 LJ92-PIN-LINK-GUARD-TEST-COVERAGE-1: the symlink test above skips on a host that cannot create a link, and
        nothing exercised the junction branch, so deleting either check left the suite green there. A real directory stands in
        for the link and the operating-system probe is told it is one, so both branches are judged on every host."""
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "checkout"
            (root / "sub" / "link").mkdir(parents=True)
            (root / "sub" / "link" / "x.pri").write_text("", encoding="utf-8")
            marked = os.path.join(os.path.abspath(str(root)), "sub", "link")
            text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(link/x.pri)"}
            self.assertEqual(_macos_openmp_gaps(text, "arm64", root / "sub", checkout_root=root), [], "a plain directory is fine")
            real_islink, real_isjunction = os.path.islink, getattr(os.path, "isjunction", lambda _path: False)
            for name, patched in (
                    ("symlink", {"islink": lambda path: path == marked or real_islink(path)}),
                    ("junction", {"isjunction": lambda path: path == marked or real_isjunction(path)})):
                with self.subTest(name), mock.patch.multiple(os.path, create=True, **patched):
                    with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                        _macos_openmp_gaps(text, "arm64", root / "sub", checkout_root=root)

    def test_macos_arch_pin_bounds_the_total_number_of_includes_it_reads(self) -> None:
        """fable r7 LJ92-PIN-INCLUDE-TOTAL-READ-BOUND-1: nesting is bounded, the count is not: a chain with a fan-out of k at each
        of seven levels costs k^7 reads. The total is bounded too, and the refusal comes before the budget is spent."""
        fan_out = 4
        reads: "list[Path]" = []

        def spy(path: Path) -> str:
            reads.append(path)
            level = int(path.stem[1:])
            return "include(a%d.pri)\n" % (level + 1) * fan_out if level < 7 else ""

        text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(a0.pri)"}
        with mock.patch.dict(globals(), {"_read_qmake_file": spy}):
            with self.assertRaisesRegex(AssertionError, "unsupported qmake construct.*more than"):
                _macos_openmp_gaps(text, "arm64", Path("proj"), checkout_root=Path("proj"))
        self.assertLessEqual(len(reads), _QMAKE_MAX_INCLUDE_READS, "the refusal must come before the budget is overspent")

    def test_macos_arch_pin_accepts_no_dash_l_option_that_carries_an_underscore(self) -> None:
        """fable r7 R5-OPERAND-LIST, class form: every ld64 `-l*` option that takes its operand as the next token has an `_`
        in its name; the library names the project links do not. So the -l<name> alternative must refuse any `_`."""
        for token in ("-lazy_framework", "-lazy_library", "-lto_library", "-load_hidden", "-lfuture_option", "-l_x"):
            with self.subTest(token):
                self.assertIsNone(_QMAKE_ACCEPTED_OPTION.fullmatch(token))
        for token in ("-lomp", "-lgomp", "-llibgomp-1", "-lc++", "-lm", "-lomp-west", "-lz.1"):
            with self.subTest(token):
                self.assertIsNotNone(_QMAKE_ACCEPTED_OPTION.fullmatch(token))

    def test_macos_arch_pin_still_follows_parent_and_nested_relative_includes(self) -> None:
        """The refusal above must not turn away the includes the real projects use (`../common/x.pri`, `sub/x.pri`)."""
        directory = Path("proj") / "sub"
        for relative in ("../common/test_defaults.pri", "fuzz_target_defaults.pri", "inner/./x.pri", "a..b/x.pri"):
            with self.subTest(relative):
                text = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(" + relative + ")"}
                reads: "list[Path]" = []

                def spy(path: Path) -> str:
                    reads.append(path)
                    return ""

                with mock.patch.dict(globals(), {"_read_qmake_file": spy}):
                    self.assertEqual(_macos_openmp_gaps(text, "arm64", directory, checkout_root=Path("proj")), [])
                self.assertEqual(len(reads), 1)

    def test_legacy_platform_scope_reader_merges_additive_scopes_of_the_same_name(self) -> None:
        """sol r3 hardening LJ92-OPENMP-SCOPE-TEST-COMMENT-BLIND-1: a second `macx { ... }` scope is additive in qmake, but
        the scope reader overwrote the first one, so the legacy whole-scope test went red on a project that still carries the
        flags."""
        project = ("macx {\n    QMAKE_CFLAGS += -fopenmp\n    LIBS += -lomp\n}\nmacx {\n    DEFINES += DIAGNOSTICS\n}\n"
                   "win32 {\n    QMAKE_CFLAGS += -fopenmp\n}\n")
        scopes = self._platform_scopes(project)
        self.assertIn("-fopenmp", scopes["macx"])
        self.assertIn("-lomp", scopes["macx"])
        self.assertIn("DIAGNOSTICS", scopes["macx"])
        self.assertNotIn("DIAGNOSTICS", scopes["win32"])

    def test_legacy_platform_scope_reader_ignores_braces_and_flags_in_comments(self) -> None:
        """sol r5 hardening LJ92-OPENMP-SCOPE-TEST-COMMENT-BLIND-1: qmake drops a `#` comment, so a `}` or `{` in one is not
        a scope delimiter, and a flag named only in a comment is not a flag. Judged on the two real projects with a comment
        inserted straight after `macx {`, and on a synthetic project."""
        for relative in ("tests/console/lj92_pred1_reader_tool.pro", "tests/fuzz/fuzz_lj92.pro"):
            with self.subTest(relative):
                original = (ROOT / relative).read_text(encoding="utf-8")
                self.assertRegex(original, r"(?m)^macx\s*\{[ \t]*$", relative + " no longer opens `macx {` on its own line")
                for comment in ("# } build comment", "# { build comment", "#}"):
                    mutated = re.sub(r"(?m)^(macx\s*\{[ \t]*)$", lambda match: match.group(1) + "\n    " + comment, original, count=1)
                    scopes = self._platform_scopes(mutated)
                    self.assertEqual(
                        sorted(scopes), sorted(self._platform_scopes(original)), "a comment brace changed the scope set: " + comment)
                    self.assertRegex(scopes["macx"], r"QMAKE_CFLAGS\s*\+=[^\n]*-fopenmp", comment)
        commented_flag = "macx {\n    # QMAKE_CFLAGS += -fopenmp\n    # } LIBS += -lomp\n    DEFINES += X\n}\n"
        scopes = self._platform_scopes(commented_flag)
        self.assertNotIn("-fopenmp", scopes["macx"])
        self.assertNotIn("-lomp", scopes["macx"])
        self.assertIn("DEFINES += X", scopes["macx"])

    def test_macos_arch_pin_resolves_include_paths_the_way_qmake_cleans_them(self) -> None:
        """sol r2 A13: qmake cleans an include path lexically (`link/../x.pri` is `x.pri`), whatever `link` points at; the
        guard must open the same file, not the one the operating system finds by following a symlink."""
        directory = Path("proj") / "sub"
        self.assertEqual(_qmake_include_target(directory, "link/../override.pri"), Path(os.path.normpath("proj/sub/override.pri")))
        self.assertEqual(_qmake_include_target(directory, "../x/./y.pri"), Path(os.path.normpath("proj/x/y.pri")))
        removal = "macx:equals(QT_ARCH, arm64):QMAKE_CFLAGS -= -fopenmp\nmacx:equals(QT_ARCH, arm64):LIBS -= -lomp\n"
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            (root / "alt" / "deep").mkdir(parents=True)
            (root / "alt" / "override.pri").write_text("", encoding="utf-8")
            (root / "override.pri").write_text(removal, encoding="utf-8")
            try:
                os.symlink(root / "alt" / "deep", root / "link", target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("this host cannot create a directory symlink")
            project = root / "target.pro"
            project.write_text(
                _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "include(link/../override.pri)"}, encoding="utf-8")
            self.assertEqual(_project_openmp_gaps(project, "arm64", checkout_root=root), _BOTH_GAPS)

    def test_macos_arch_pin_discovers_architectures_the_project_really_tests(self) -> None:
        """sol r2 A5/A10: the architectures to judge are the ones a condition of the project or of a followed include tests,
        not the ones a quoted description happens to mention, and they come from the same text the flags are judged on."""
        with tempfile.TemporaryDirectory() as scratch:
            project = self._project_with_include(
                scratch, "include(../extra.pri)", {"extra.pri": "macx:equals(QT_ARCH, arm64e):DEFINES += ARM64E\n"})
            named = _qmake_named_architectures(project.read_text(encoding="utf-8"), project.parent, Path(scratch))
            self.assertEqual(named, {"x86_64", "arm64", "arm64e"})
        described = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": 'DESCRIPTION = "equals(QT_ARCH, arm64e)"'}
        self.assertEqual(_qmake_named_architectures(described, None), {"x86_64", "arm64"})
        commented = _MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": "# macx:equals(QT_ARCH, arm64e):DEFINES += ARM64E"}
        self.assertEqual(_qmake_named_architectures(commented, None), {"x86_64", "arm64"})

    def test_macos_arch_pin_reads_project_bytes_without_universal_newlines(self) -> None:
        """sol r2 A3: a lone CR is not a line break to qmake, so reading a project with universal newlines would turn a
        comment that qmake swallows whole into live lines; CRLF files still read as ordinary lines."""
        cr_branch = "    # placeholder\r    equals(QT_ARCH, arm64) {\r        QMAKE_CFLAGS += -fopenmp\r        LIBS += -lomp\r    }"
        with tempfile.TemporaryDirectory() as scratch:
            project = Path(scratch) / "cr.pro"
            project.write_bytes((_MACX_PIN_TEMPLATE % {"arm64": cr_branch, "tail": ""}).encode("utf-8"))
            self.assertEqual(_project_openmp_gaps(project, "arm64", checkout_root=Path(scratch)), _BOTH_GAPS)
            project.write_bytes((_MACX_PIN_TEMPLATE % {"arm64": _ARM64_LIVE, "tail": ""}).replace("\n", "\r\n").encode("utf-8"))
            self.assertEqual(_project_openmp_gaps(project, "arm64", checkout_root=Path(scratch)), [])

    def test_macos_arch_pin_model_returns_effective_tokens(self) -> None:
        text = _MACX_PIN_TEMPLATE % {
            "arm64": _arm64_section("QMAKE_CFLAGS *= -fopenmp", "QMAKE_CFLAGS *= -fopenmp", "LIBS += -lomp"), "tail": ""}
        values = _qmake_effective_flags(text, "arm64")
        self.assertEqual(values["QMAKE_CFLAGS"], ["-std=c99", "-fopenmp"])
        self.assertEqual(values["LIBS"], ["-lomp"])


if __name__ == "__main__":
    unittest.main()
