"""PROD-TELEMETRY-DURATION-AS-PROOF-4/4B: the live scanner behind
test_duration_as_proof_inventory.py.

Finds candidate "duration used as run-proof" sites: places that compare a duration-named
value against zero (or check it via a gtest predicate macro) to decide whether some stage
ran, produced output, or is used as an exclusivity/fallback signal. This is a LOWER BOUND,
not a completeness proof -- see the inventory JSON's own header for the same caveat, and
tests/test_positive_control in the test module for the seeded snippets that pin the
matcher's actual behaviour (one it must flag, one it must not, per pattern shape).

Patterns matched (all case-sensitive, per-physical-line):
  - IDENT_COMPARE: a plain identifier that is a "duration identifier" (see
    ``_is_duration_identifier`` below), compared against a zero literal
    (``0``, ``0.0``, ``0.0f``, ``0L``, ...) with one of ``>= <= == != > <``, in either
    operand order. A duration identifier is recognized by TOKEN, not just suffix: its
    camelCase/snake_case words are split and checked against a fixed unit vocabulary
    (``ms``, ``msec``, ``millis``/``milliseconds``, ``seconds``, ``secs``, ``sec``,
    ``duration``, ``elapsed``, ``micros``, ``nanos``), plus ``us``/``ns`` but only as a
    whole underscore-delimited segment (``elapsed_us``, ``stage_ns``), never as a bare
    camelCase ``Us``/``Ns`` token (too collision-prone with unrelated abbreviations).
    Token-based matching means the unit word can appear ANYWHERE in the identifier, not
    only as a suffix, so both ``wallMsDelta`` (unit then qualifier) and
    ``expectedDurationSeconds`` (qualifier then unit) match, and a bare ``ms``/``sec``/
    ``seconds`` identifier matches too (single-token identifier equal to a unit word).
  - DURATION_GETTER: a call of the shape ``fooBar()`` where the called name is itself a
    duration identifier by the same token rule (e.g. ``getSeededProbeMilliseconds()``),
    compared against zero the same way. Kept as a distinct trigger from IDENT_COMPARE only
    for reporting; the identifier rule is shared.
  - JSON_MS_KEY: a JSON/QJsonObject-style read of a key ending in ``_ms`` via
    ``.toDouble()``/``.toInt()`` -- with or without a default-value argument (e.g.
    ``.toDouble(-1.0)``) -- compared against zero the same way.
  - ASSERT_MACRO: a gtest predicate macro whose arguments encode a duration-vs-zero
    comparison:
      * 2-arg ``ASSERT_*``/``EXPECT_*`` (``EQ NE GT GE LT LE DOUBLE_EQ FLOAT_EQ``) whose
        two top-level arguments are a zero literal and a duration expression (any of the
        three shapes above, or a plain duration identifier/call with no operator at all,
        since the macro itself encodes the comparison).
      * 3-arg ``ASSERT_NEAR``/``EXPECT_NEAR(val1, val2, abs_error)``: the same duration-vs-
        zero check applied to (val1, val2), ignoring the tolerance argument.

A line can trigger more than one pattern; that does not create more than one candidate --
candidates are per PHYSICAL LINE (a "site"), not per pattern. A negation (``!(x_ms > 0.0)``)
or a ternary (``x_ms == 0.0 ? a : b``) is still caught because the inner comparison itself
matches one of the shapes above; no special-casing is needed for those.

Known, deliberate non-goals (see the inventory's own ``lower_bound_caveat``): a comparison
split across two physical lines, a duration hidden behind an intermediate variable or a
helper function, and shapes with zero live instances in this tree at round-4B time
(``qFuzzyIsNull``, ``std::max(0.0, x_ms)``, a bare ``*Seconds()`` getter, chrono ``.count()``
compares) are not matched. Broadening for a shape with no live instance would only add
matcher surface with no coverage benefit, at the cost of harder-to-reason-about false
positives; grep for it again before deciding to add it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SCAN_GLOBS = (
    "src/*.c", "src/*.cpp", "src/*.h", "src/*.hpp",
    "platform/qt/*.c", "platform/qt/*.cpp", "platform/qt/*.h", "platform/qt/*.hpp",
    "tests/*.c", "tests/*.cpp", "tests/*.h", "tests/*.hpp",
)


# `(?<![0-9.])` / `(?![0-9.])` are load-bearing, not decorative: without them, the tail of
# a non-zero literal like `10.0` or `0.5` (matched at its trailing/leading "0") would be
# misread as a standalone zero literal. `\b` alone does not guard this because digits are
# all word characters, so there is no word-boundary between them.
_ZERO = r"(?<![0-9.])0(?:\.0+)?[fFlLuU]*(?![0-9.])"
_OP = r"(?:>=|<=|==|!=|>|<)"

# Token vocabulary for `_is_duration_identifier`. Matched case-insensitively PER TOKEN
# (after camelCase/snake_case splitting), never as a raw substring of the whole identifier
# -- see that function's docstring for why this avoids e.g. "Msg" or "Section".
_DURATION_UNIT_WORDS = frozenset({
    "ms", "msec", "millis", "milliseconds",
    "seconds", "secs", "sec",
    "duration", "elapsed",
    "micros", "nanos",
})
# Only recognized as a whole underscore-delimited segment (see docstring), never as a bare
# camelCase token -- "us"/"ns" are too short and too collision-prone to allow in general.
_DURATION_UNDERSCORE_ONLY_WORDS = frozenset({"us", "ns"})

_RE_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RE_CAMEL_TOKEN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

# Two closing parens after the quoted key: one for `QStringLiteral(...)`, one for the
# enclosing `.value(...)` call. The trailing `to(?:Double|Int)(...)` argument is now
# unconstrained (`[^()]*`, was previously required empty) so a default-value read like
# `.toDouble(-1.0)` is still recognized, not just the bare `.toDouble()`.
_JSON_MS = r'"[A-Za-z0-9_]*_ms"\s*\)\s*\)\s*\.\s*to(?:Double|Int)\s*\([^()]*\)'

_RE_JSON_MS = re.compile(
    rf"{_JSON_MS}\s*{_OP}\s*{_ZERO}|{_ZERO}\s*{_OP}\s*{_JSON_MS}"
)
_RE_2ARG_MACRO_CALL = re.compile(
    r"\b(?:ASSERT|EXPECT)_(?:EQ|NE|GT|GE|LT|LE|DOUBLE_EQ|FLOAT_EQ)\s*\(([^;]*)\)\s*;"
)
_RE_NEAR_MACRO_CALL = re.compile(r"\b(?:ASSERT|EXPECT)_NEAR\s*\(([^;]*)\)\s*;")
_RE_ZERO_FULL = re.compile(rf"^{_ZERO}$")


def _is_duration_identifier(ident: str) -> bool:
    """True if any word of `ident` (camelCase- or snake_case-split) names a duration unit.

    Position-independent by design: the unit word may be the whole identifier (a bare
    ``ms``), the last word (a suffix like ``_ms``/``Ms``/``Seconds``), or an earlier word
    followed by a qualifier (``wallMsDelta``, ``expectedDurationSeconds``). This is what
    lets the same rule catch suffix-only, qualifier-suffixed, and qualifier-prefixed names
    without three separate patterns.

    ``us``/``ns`` are special-cased to only count when they appear as an underscore-
    delimited segment on their own (``elapsed_us``, or the bare identifier ``us``), not as
    a camelCase token (``Us``/``Ns``) embedded in a larger name -- those two-letter tokens
    are too generic to trust outside an explicit underscore boundary.
    """
    underscore_parts = ident.split("_")
    multi_part = len(underscore_parts) > 1
    for part in underscore_parts:
        if not part:
            continue
        lowered = part.lower()
        if lowered in _DURATION_UNDERSCORE_ONLY_WORDS and (multi_part or lowered == ident.lower()):
            return True
        for token in _RE_CAMEL_TOKEN.findall(part):
            if token.lower() in _DURATION_UNIT_WORDS:
                return True
    return False


def _is_duration_expr(text: str) -> bool:
    """True if `text` contains a duration identifier (bare or as a call), a JSON `_ms`
    read, or (recursively) a comparison already built from one of those -- used to decide
    whether a macro argument is "the duration side" of a duration-vs-zero predicate.
    """
    if re.search(_JSON_MS, text):
        return True
    for m in _RE_IDENT.finditer(text):
        if _is_duration_identifier(m.group(0)):
            return True
    return False


def _split_top_level_args(arg_text: str) -> list[str] | None:
    """Split a macro's argument text on top-level commas (paren/bracket/quote aware).

    Returns None if parens are unbalanced (e.g. the macro call actually spans multiple
    physical lines) so the caller can skip it rather than mis-split it.
    """
    depth = 0
    quote = None
    parts: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(arg_text):
        ch = arg_text[i]
        if quote:
            current.append(ch)
            if ch == "\\" and i + 1 < len(arg_text):
                i += 1
                current.append(arg_text[i])
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            current.append(ch)
        elif ch in "([{":
            depth += 1
            current.append(ch)
        elif ch in ")]}":
            depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    if depth != 0 or quote is not None:
        return None
    parts.append("".join(current))
    return parts


def _pair_is_duration_zero_predicate(a: str, b: str) -> bool:
    a, b = a.strip(), b.strip()
    a_zero, b_zero = bool(_RE_ZERO_FULL.match(a)), bool(_RE_ZERO_FULL.match(b))
    a_dur, b_dur = _is_duration_expr(a), _is_duration_expr(b)
    return (a_zero and b_dur) or (b_zero and a_dur)


def _macro_is_duration_zero_predicate(line: str) -> bool:
    for m in _RE_2ARG_MACRO_CALL.finditer(line):
        args = _split_top_level_args(m.group(1))
        if args is None or len(args) != 2:
            continue
        if _pair_is_duration_zero_predicate(args[0], args[1]):
            return True
    for m in _RE_NEAR_MACRO_CALL.finditer(line):
        args = _split_top_level_args(m.group(1))
        if args is None or len(args) != 3:
            continue
        # ASSERT_NEAR(val1, val2, abs_error) -- the tolerance is not part of the predicate.
        if _pair_is_duration_zero_predicate(args[0], args[1]):
            return True
    return False


_RE_LEFT_OPERAND = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\s*\(\s*\))?\s*$")
_RE_RIGHT_OPERAND = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(\s*\(\s*\))?")
_RE_ZERO_AT_END = re.compile(rf"{_ZERO}\s*$")
_RE_ZERO_AT_START = re.compile(rf"^\s*{_ZERO}")


def _ident_compare_trigger(line: str) -> str | None:
    """Scan `line` for `<duration-ident>[()] OP zero` or `zero OP <duration-ident>[()]`.

    Returns "ident_compare" if a matching bare identifier was found, "duration_getter" if
    only a called (niladic) form (`ident()`) was found -- e.g. `getFooMilliseconds()` -- or
    None. A line can contain both shapes; "ident_compare" wins for de-duplication purposes
    since both are proven live by the seeded positive controls independently.
    """
    found_ident = False
    found_call = False
    for op_m in re.finditer(_OP, line):
        left = line[: op_m.start()]
        right = line[op_m.end():]
        left_m = _RE_LEFT_OPERAND.search(left)
        right_m = _RE_RIGHT_OPERAND.match(right)
        zero_left = bool(_RE_ZERO_AT_END.search(left))
        zero_right = bool(_RE_ZERO_AT_START.match(right))
        if zero_right and left_m and _is_duration_identifier(left_m.group(1)):
            if left_m.group(2):
                found_call = True
            else:
                found_ident = True
        if zero_left and right_m and _is_duration_identifier(right_m.group(1)):
            if right_m.group(2):
                found_call = True
            else:
                found_ident = True
    if found_ident:
        return "ident_compare"
    if found_call:
        return "duration_getter"
    return None


def _line_triggers(line: str) -> list[str]:
    triggers = []
    ident_trigger = _ident_compare_trigger(line)
    if ident_trigger:
        triggers.append(ident_trigger)
    if _RE_JSON_MS.search(line):
        triggers.append("json_ms_key")
    if _macro_is_duration_zero_predicate(line):
        triggers.append("assert_macro")
    return triggers


_RE_STRING_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"|' r"'(?:\\.|[^'\\])*'")

# A minimal C/C++-ish lexer, only precise enough for anchor identity (not a real parser):
# identifier/keyword, a simple numeric literal, then the multi-character operators this
# codebase actually uses (longest alternatives first so e.g. `>=` never lexes as `>` `=`),
# and finally any other single non-whitespace character (punctuation) as its own token.
_RE_TOKEN = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*"
    r"|[0-9]+\.?[0-9]*[fFlLuU]*"
    r"|>>=|<<=|\+=|-=|\*=|/=|%=|&=|\|=|\^="
    r"|==|!=|>=|<=|&&|\|\||<<|>>|::|->|\+\+|--"
    r"|\S"
)


def normalize_anchor(line: str) -> str:
    """The anchor is the site's identity: normalized text, never a line number.

    Token-aware: the line is lexed into atoms (identifiers, numbers, operators/punctuation,
    and whole string literals) and rejoined with exactly one space between every pair, so
    spacing CHOICES around operators/punctuation -- indentation, alignment, ``if(x)`` vs
    ``if ( x )`` -- never force a spurious reclassification; both normalize to the same
    ``if ( x )``. A string literal is kept as a single atom, verbatim, so whitespace INSIDE
    one (``"a  b"`` vs ``"a b"``) is never touched: that is a change to the program's data,
    not a formatting choice, and must still change the anchor.
    """
    atoms: list[str] = []
    pos = 0
    for m in _RE_STRING_LITERAL.finditer(line):
        atoms.extend(_RE_TOKEN.findall(line[pos:m.start()]))
        atoms.append(m.group(0))
        pos = m.end()
    atoms.extend(_RE_TOKEN.findall(line[pos:]))
    return " ".join(atoms)


@dataclass(frozen=True)
class Candidate:
    path: str  # POSIX-style, relative to repo root
    anchor: str
    triggers: tuple[str, ...]
    lines: tuple[int, ...]  # 1-indexed line numbers this anchor text was found at


def _tracked_files(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", *SCAN_GLOBS],
        capture_output=True, text=True, check=True,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def _strip_comments(text: str) -> str:
    """Blank out `//` and `/* */` comment content (tracking string literals so a comment
    marker inside a string is not mistaken for a real one), keeping every newline in place
    so line numbers are unaffected. Used ONLY to decide whether a line triggers -- the
    anchor for a genuine candidate is still normalize_anchor() of the ORIGINAL line, so an
    edit to just a trailing comment on an otherwise-unchanged code line still (as before)
    changes that line's anchor; this function's only job is to stop comment TEXT (e.g. a
    docstring that mentions ``Milliseconds() > 0.0`` as prose) from being read as code.
    Deliberately spans physical lines for ``/* */`` (a same-line-only comment strip would
    miss a continuation line of a multi-line block comment).
    """
    out: list[str] = []
    state = "code"  # "code" | "string" | "line_comment" | "block_comment"
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if state == "string":
            out.append(ch)
            if ch == "\\" and i + 1 < n and text[i + 1] != "\n":
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                state = "code"
            i += 1
            continue
        if state == "line_comment":
            if ch == "\n":
                out.append(ch)
                state = "code"
            else:
                out.append(" ")
            i += 1
            continue
        if state == "block_comment":
            if ch == "*" and i + 1 < n and text[i + 1] == "/":
                out.append("  ")
                i += 2
                state = "code"
            elif ch == "\n":
                out.append(ch)
                i += 1
            else:
                out.append(" ")
                i += 1
            continue
        # state == "code"
        if ch in ('"', "'"):
            quote = ch
            state = "string"
            out.append(ch)
            i += 1
        elif ch == "/" and i + 1 < n and text[i + 1] == "/":
            out.append("  ")
            state = "line_comment"
            i += 2
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            out.append("  ")
            state = "block_comment"
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def scan_text(text: str, source: str = "<text>") -> list[Candidate]:
    """Scan already-loaded text (used directly by the positive-control tests)."""
    by_anchor: dict[str, tuple[set[str], list[int]]] = {}
    original_lines = text.splitlines()
    code_only_lines = _strip_comments(text).splitlines()
    for lineno, (line, code_only) in enumerate(zip(original_lines, code_only_lines), start=1):
        triggers = _line_triggers(code_only)
        if not triggers:
            continue
        anchor = normalize_anchor(line)
        trig_set, lines = by_anchor.setdefault(anchor, (set(), []))
        trig_set.update(triggers)
        lines.append(lineno)
    return [
        Candidate(path=source, anchor=anchor, triggers=tuple(sorted(trig_set)), lines=tuple(lines))
        for anchor, (trig_set, lines) in by_anchor.items()
    ]


def scan_repo(root: Path | None = None) -> list[Candidate]:
    root = root or ROOT
    candidates: list[Candidate] = []
    for rel_path in _tracked_files(root):
        abs_path = root / rel_path
        try:
            text = abs_path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        candidates.extend(scan_text(text, source=rel_path.replace("\\", "/")))
    return candidates


if __name__ == "__main__":
    import json
    import sys

    rows = [
        {"path": c.path, "anchor": c.anchor, "triggers": list(c.triggers), "lines": list(c.lines)}
        for c in sorted(scan_repo(), key=lambda c: (c.path, c.lines[0]))
    ]
    json.dump(rows, sys.stdout, indent=2)
    sys.stdout.write("\n")
