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
import subprocess
import tempfile
import unittest


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
_QMAKE_MAX_INCLUDE_NESTING = 8
# CONFIG tokens a `+=` may add: none of them is a selector the model reads (macx, a platform name, debug/release).
_QMAKE_BENIGN_CONFIG_TOKENS = frozenset(("c++11", "c++14", "c++17", "c++20", "console", "warn_on", "warn_off"))
# Flag variables whose content can switch OpenMP off for the C compile (QMAKE_CFLAGS and its per-configuration forms) or
# for the link (QMAKE_LFLAGS and its forms). Their appends are watched for -fno-openmp even where the condition is unknown.
_QMAKE_COMPILE_FLAG_VARIABLE = re.compile(r"QMAKE_CFLAGS(?:_[A-Z0-9_]+)?")
_QMAKE_LINK_FLAG_VARIABLE = re.compile(r"QMAKE_LFLAGS(?:_[A-Z0-9_]+)?")


def _qmake_logical_text(pro_text: str) -> str:
    """The .pro text qmake actually reads: comments dropped (a `#` runs to the end of its LF-terminated line, so only
    LF splits lines), then each backslash-newline continuation joined into one logical line. A continuation followed by
    a blank or comment-only line is refused: that is a shape this model does not try to interpret (fail closed)."""
    logical = []
    carry = ""
    for physical in pro_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = physical.split("#", 1)[0].rstrip()
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


def _qmake_atom_state(atom: str, arch: str) -> str:
    if atom.startswith("!!"):
        raise AssertionError("unsupported qmake construct: repeated '!' in selector " + repr(atom))
    negated = atom.startswith("!")
    atom = atom.lstrip("!").strip()
    arch_match = re.fullmatch(_QT_ARCH_ATOM, atom)
    if "QT_ARCH" in atom and not arch_match:
        raise AssertionError("unsupported qmake construct: unreadable QT_ARCH selector " + repr(atom))
    if arch_match:
        state = _TRUE if arch_match.group(1) == arch else _FALSE
    elif atom == "macx":
        state = _TRUE
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


def _qmake_condition_state(condition: str, arch: str) -> str:
    """Whether a scope header or single-line prefix is active on macOS with QT_ARCH=arch. `:` is AND, `|` is OR; a
    header mixing both, `else`, CONFIG(...) and any scope name not listed above stay UNKNOWN."""
    if not condition:
        return _TRUE
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


def _qmake_condition_is_arch_local(condition: str, arch: str) -> bool:
    """Whether a scope header or single-line prefix is itself a QT_ARCH=arch branch: an AND needs one such conjunct, an OR
    needs every alternative to be one (so `equals(QT_ARCH, x86_64)|equals(QT_ARCH, arm64)` counts, `macx|equals(...)` does
    not), and a header mixing the two is never one."""
    if not condition:
        return False
    alternatives = _qmake_split_top(condition, "|")
    conjuncts = _qmake_split_top(condition, ":")
    if len(alternatives) > 1 and len(conjuncts) > 1:
        return False
    if len(alternatives) > 1:
        return all(_qmake_atom_is_arch_local(alternative, arch) for alternative in alternatives)
    return any(_qmake_atom_is_arch_local(conjunct, arch) for conjunct in conjuncts)


def _qmake_simulate(
        pro_text: str, arch: str, source_dir: "Path | None" = None
) -> "tuple[dict[str, list[str]], dict[str, set[str]], dict[str, set[str]]]":
    """Simulate a macOS build with QT_ARCH=arch in file order, following include() files where qmake evaluates them.
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

    def watched(variable: str) -> bool:
        return (variable in values or bool(_QMAKE_COMPILE_FLAG_VARIABLE.fullmatch(variable))
                or bool(_QMAKE_LINK_FLAG_VARIABLE.fullmatch(variable)))

    def apply(statement: str, scope: "tuple[str, bool]", depth: int, directory: "Path | None", nesting: int) -> None:
        # Fail closed: a statement this model cannot read as `[conditions:]VARIABLE op value` is refused, never skipped.
        match = _QMAKE_ASSIGNMENT.match(statement)
        if not match:
            include = _QMAKE_INCLUDE.fullmatch(statement)
            if depth == 0 and include:
                follow(include.group(1), directory, nesting)
                return
            raise AssertionError("unsupported qmake construct: " + repr(statement))
        variable, operator, value = match.group("var"), match.group("op"), match.group("value")
        tokens = value.split()
        if variable == "QT_ARCH" or (variable == "CONFIG" and (
                operator in ("=", "~=")
                or (operator == "-=" and "macx" in tokens)
                or (operator in ("+=", "*=") and not set(tokens) <= _QMAKE_BENIGN_CONFIG_TOKENS))):
            raise AssertionError("unsupported qmake construct (it changes what a selector means): " + repr(statement))
        condition = match.group("cond").rstrip(":").strip()
        state = _qmake_and([scope[0], _qmake_condition_state(condition, arch)])
        if not watched(variable):
            return
        if operator == "~=" or re.search(r"[$\"']", value):
            raise AssertionError("unsupported qmake construct (substitution, expansion or quoting): " + repr(statement))
        if state == _FALSE:
            return
        certain = state == _TRUE
        local = scope[1] or _qmake_condition_is_arch_local(condition, arch)
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
        target = directory / relative
        try:
            text = target.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise AssertionError("unsupported qmake construct: include(" + relative + ") is unreadable: " + str(error))
        evaluate(text, target.parent, nesting + 1)

    def evaluate(text: str, directory: "Path | None", nesting: int) -> None:
        scopes: "list[tuple[str, bool]]" = []
        buffer = ""
        in_reference = False

        def flush() -> None:
            nonlocal buffer
            statement = buffer.strip()
            buffer = ""
            if statement:
                apply(statement, scopes[-1] if scopes else (_TRUE, False), len(scopes), directory, nesting)

        for char in _qmake_logical_text(text):
            if in_reference:
                buffer += char
                in_reference = char != "}"
            elif char == "{" and buffer.endswith("$$"):
                buffer += char
                in_reference = True
            elif char == "{":
                header = buffer.strip()
                own = _qmake_condition_state(header, arch)
                buffer = ""
                parent_state, parent_local = scopes[-1] if scopes else (_TRUE, False)
                scopes.append((_qmake_and([parent_state, own]),
                               parent_local or _qmake_condition_is_arch_local(header, arch)))
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


def _qmake_effective_flags(pro_text: str, arch: str, source_dir: "Path | None" = None) -> "dict[str, list[str]]":
    """Effective tokens of QMAKE_CFLAGS, QMAKE_LFLAGS and LIBS for a macOS build with QT_ARCH=arch (see _qmake_simulate)."""
    return _qmake_simulate(pro_text, arch, source_dir)[0]


def _macos_openmp_gaps(pro_text: str, arch: str, source_dir: "Path | None" = None) -> "list[str]":
    """The OpenMP flags a macOS QT_ARCH=arch build of this .pro would NOT get: the C compile flag and the link flag. Each
    must be present, carried by a line of the QT_ARCH branch itself (not inherited from the enclosing macx scope), and not
    switched off by a -fno-openmp that any line of the matching flag variables may add."""
    values, branch_local, possible = _qmake_simulate(pro_text, arch, source_dir)
    compile_variables = [name for name in possible if _QMAKE_COMPILE_FLAG_VARIABLE.fullmatch(name)]
    link_variables = [name for name in possible if _QMAKE_LINK_FLAG_VARIABLE.fullmatch(name)]
    compile_disabled = any("-fno-openmp" in possible[name] for name in compile_variables)
    link_flag_disabled = any("-fno-openmp" in possible[name] for name in link_variables)
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


def _project_openmp_gaps(project: Path, arch: str) -> "list[str]":
    """_macos_openmp_gaps for a .pro file on disk."""
    return _macos_openmp_gaps(project.read_text(encoding="utf-8"), arch, project.parent)


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
        """Top-level `name { ... }` qmake scopes of a .pro file, name -> body (nested scopes stay in the body)."""
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
                    scopes[name] = pro_text[body_start:index]
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
            pro_text = (ROOT / relative).read_text(encoding="utf-8")
            named = {"x86_64", "arm64"} | set(re.findall(_QT_ARCH_ATOM, _qmake_logical_text(pro_text)))
            for arch in sorted(named):
                for gap in _project_openmp_gaps(ROOT / relative, arch):
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
            self.assertEqual(_project_openmp_gaps(project, "arm64"), _BOTH_GAPS)
            self.assertEqual(_project_openmp_gaps(project, "x86_64"), [])
        with tempfile.TemporaryDirectory() as scratch:
            project = self._project_with_include(
                scratch, "include(../outer.pri)",
                {"outer.pri": "include(inner/inner.pri)\n", "inner/inner.pri": removal})
            self.assertEqual(_project_openmp_gaps(project, "arm64"), _BOTH_GAPS)
        with tempfile.TemporaryDirectory() as scratch:
            project = self._project_with_include(
                scratch, "include(../defaults.pri)",
                {"defaults.pri": "REPO_ROOT = $$clean_path($$PWD/..)\nCONFIG += c++17 console\nCONFIG -= app_bundle\n"
                                 "helper.input = $$PWD/helper.h.in\nSOURCES += $$REPO_ROOT/x.c \\\n    $$REPO_ROOT/y.c\n"})
            self.assertEqual(_project_openmp_gaps(project, "arm64"), [])

    def test_macos_arch_pin_refuses_include_files_it_cannot_interpret(self) -> None:
        unreadable = (
            ("a missing include", "include(../nothing-here.pri)", {}),
            ("an include that unsets a tracked variable", "include(../u.pri)",
             {"u.pri": "macx:equals(QT_ARCH, arm64):unset(LIBS) { }\n"}),
            ("an include that quotes a flag", "include(../q.pri)", {"q.pri": 'QMAKE_CFLAGS -= "-fopenmp"\n'}),
            ("an include that adds a platform name to CONFIG", "include(../c.pri)", {"c.pri": "CONFIG += linux\n"}),
            ("an include loop", "include(../loop.pri)", {"loop.pri": "include(loop.pri)\n"}),
            ("an include named through a variable", "include($$PWD/../v.pri)", {"v.pri": "DEFINES += X\n"}),
        )
        for name, include_line, files in unreadable:
            with self.subTest(name), tempfile.TemporaryDirectory() as scratch:
                project = self._project_with_include(scratch, include_line, files)
                with self.assertRaisesRegex(AssertionError, "unsupported qmake construct"):
                    _project_openmp_gaps(project, "arm64")

    def test_macos_arch_pin_model_returns_effective_tokens(self) -> None:
        text = _MACX_PIN_TEMPLATE % {
            "arm64": _arm64_section("QMAKE_CFLAGS *= -fopenmp", "QMAKE_CFLAGS *= -fopenmp", "LIBS += -lomp"), "tail": ""}
        values = _qmake_effective_flags(text, "arm64")
        self.assertEqual(values["QMAKE_CFLAGS"], ["-std=c99", "-fopenmp"])
        self.assertEqual(values["LIBS"], ["-lomp"])


if __name__ == "__main__":
    unittest.main()
