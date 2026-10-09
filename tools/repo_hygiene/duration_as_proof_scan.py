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

  - FABS_ZERO (HARDENING-2): ``fabs(<bare duration operand>)`` compared to a small
    literal/epsilon (``std::fabs(plan.expectedDurationSeconds) < 0.000001``), the
    near-zero spelling of a zero assert. ``fabs(d - 10.01)`` (a value comparison) and a
    literal above 1e-3 are not matched.

HARDENING-2 grammar notes: an operand may be parenthesized (``(elapsed_ms) > 0.0``,
``0.0 < (elapsed_ms)``, ``static_cast<double>(x_ms) > 0``, ``0 < (double)x_ms``) but a call
WITH arguments is not unwrapped (``foo(elapsed_ms) > 0`` compares foo's result). A trailing
``At``/``_at`` word is NOT evidence of a position (``elapsedAt = t.nsecsElapsed()`` is a
duration): there is no name- or position-based exemption, so such a name is flagged exactly as
master flags it (fail closed). An escape-proof position proof is a separate card.
Macro and ``fabs`` sites are found by balanced-paren extraction over the whole text, so a call
split across physical lines is ONE site, keyed by all its lines joined (see ``scan_text``).
Comments are stripped from the anchor view too, so a comment edit never re-keys a site, and
raw string literals (``R"delim(...)delim"``) are lexed as strings (see ``_strip_comments``).

A line can trigger more than one pattern; that does not create more than one candidate --
candidates are per site (a physical line, or the joined lines of a multi-line macro/fabs
statement), not per pattern. A negation (``!(x_ms > 0.0)``) or a ternary
(``x_ms == 0.0 ? a : b``) is still caught because the inner comparison itself matches one of
the shapes above; no special-casing is needed for those.

Known, deliberate non-goals (see the inventory's own ``lower_bound_caveat``): a bare
comparison (not a macro/``fabs`` call) split across two physical lines, a duration hidden
behind an intermediate variable or a helper function, and shapes with zero live instances in
this tree (``qFuzzyIsNull``, ``std::max(0.0, x_ms)``, a bare ``*Seconds()`` getter, chrono
``.count()`` compares) are not matched. Broadening for a shape with no live instance would
only add matcher surface with no coverage benefit, at the cost of harder-to-reason-about
false positives; grep for it again before deciding to add it.
"""

from __future__ import annotations

import bisect
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
_RE_2ARG_MACRO_OPEN = re.compile(
    r"\b(?:ASSERT|EXPECT)_(?:EQ|NE|GT|GE|LT|LE|DOUBLE_EQ|FLOAT_EQ)\s*\("
)
_RE_NEAR_MACRO_OPEN = re.compile(r"\b(?:ASSERT|EXPECT)_NEAR\s*\(")
_RE_FABS_OPEN = re.compile(r"(?<![A-Za-z0-9_])(?:std\s*::\s*)?f?abs\s*\(")
_RE_ZERO_FULL = re.compile(rf"^{_ZERO}$")

# `fabs(<bare duration operand>)` compared to a small literal/epsilon is a zero assert in
# disguise (`ASSERT_TRUE(std::fabs(plan.expectedDurationSeconds) < 0.000001)`). The operand
# must be a bare duration chain (no subtraction of a non-zero expected value: that is a value
# comparison, e.g. `fabs(x.durationSeconds - 10.01)`).
_RE_DURATION_OPERAND_CHAIN = re.compile(
    r"^((?:[A-Za-z_][A-Za-z0-9_]*\s*(?:\.|->|::)\s*)*)([A-Za-z_][A-Za-z0-9_]*)(\s*\(\s*\))?$"
)
_EPS_LITERAL = r"(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?[fFlL]*"
_EPS_NAME = r"[A-Za-z_][A-Za-z0-9_]*(?:[Ee]ps|[Ee]psilon|[Tt]ol|[Tt]olerance)[A-Za-z0-9_]*"
# `fabs(d) < eps` / `fabs(d) == eps` and the mirrored `eps > fabs(d)` / `eps == fabs(d)`.
_RE_EPS_AFTER = re.compile(rf"^\s*(?:<=|<|==)\s*({_EPS_LITERAL}|{_EPS_NAME})(?![A-Za-z0-9_.])")
_RE_EPS_BEFORE = re.compile(rf"(?<![A-Za-z0-9_.])({_EPS_LITERAL}|{_EPS_NAME})\s*(?:>=|>|==)\s*$")
_MAX_EPSILON = 1e-3  # a literal above this is a value comparison, not a "is it zero" check


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

    A trailing ``At``/``_at`` word is NEVER evidence (``elapsedAt = timer.nsecsElapsed()`` is a
    duration): no name- or position-based exemption exists, the unit word still decides.
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


_QUALIFIER_TAILS = (".", "->", "::")


def _is_duration_expr(text: str) -> bool:
    """True if `text` contains a duration identifier (bare or as a call), a JSON `_ms`
    read, or (recursively) a comparison already built from one of those -- used to decide
    whether a macro argument is "the duration side" of a duration-vs-zero predicate.
    """
    if re.search(_JSON_MS, text):
        return True
    return any(_is_duration_identifier(m.group(0)) for m in _RE_IDENT.finditer(text))


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


def _find_matching_close(text: str, open_idx: int) -> int | None:
    """Index of the `)` closing the `(` at `text[open_idx]`, string/char-literal aware and
    spanning newlines; None if it never closes (so the caller skips rather than guesses)."""
    depth = 0
    quote = None
    i, n = open_idx, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i if ch == ")" else None
        i += 1
    return None


def _unwrap_parens(text: str) -> str:
    """Strip redundant outer grouping parens: `((0))` -> `0`, `(a_ms)` -> `a_ms`."""
    text = text.strip()
    while text.startswith("(") and _find_matching_close(text, 0) == len(text) - 1:
        text = text[1:-1].strip()
    return text


def _pair_is_duration_zero_predicate(a: str, b: str) -> bool:
    a, b = _unwrap_parens(a), _unwrap_parens(b)
    a_zero, b_zero = bool(_RE_ZERO_FULL.match(a)), bool(_RE_ZERO_FULL.match(b))
    a_dur, b_dur = _is_duration_expr(a), _is_duration_expr(b)
    return (a_zero and b_dur) or (b_zero and a_dur)


def _fabs_arg_is_bare_duration(arg: str) -> bool:
    m = _RE_DURATION_OPERAND_CHAIN.match(_unwrap_parens(arg))
    return bool(m) and _is_duration_identifier(m.group(2))


def _is_epsilon(token: str) -> bool:
    try:
        return float(token.rstrip("fFlL")) <= _MAX_EPSILON
    except ValueError:
        return True  # an epsilon/tolerance NAME (the regex only lets those through)


def _macro_and_fabs_sites(code_text: str) -> list[tuple[int, int, str]]:
    """(start offset, end offset, trigger) of every macro-predicate / fabs-zero site, found
    by BALANCED-paren extraction over the whole comment-stripped text rather than per
    physical line, so a call split across lines (``ASSERT_TRUE( std::fabs(x.durationSeconds)``
    / ``< 0.000001 );``) is one site, and a trailing ``<< "msg"`` stream cannot unbalance it.
    """
    sites: list[tuple[int, int, str]] = []
    for rx, nargs in ((_RE_2ARG_MACRO_OPEN, 2), (_RE_NEAR_MACRO_OPEN, 3)):
        for m in rx.finditer(code_text):
            close = _find_matching_close(code_text, m.end() - 1)
            if close is None:
                continue
            args = _split_top_level_args(code_text[m.end():close])
            if args is None or len(args) != nargs:
                continue
            # ASSERT_NEAR(val1, val2, abs_error) -- the tolerance is not part of the predicate.
            if _pair_is_duration_zero_predicate(args[0], args[1]):
                sites.append((m.start(), close + 1, "assert_macro"))
    for m in _RE_FABS_OPEN.finditer(code_text):
        close = _find_matching_close(code_text, m.end() - 1)
        if close is None or not _fabs_arg_is_bare_duration(code_text[m.end():close]):
            continue
        after = _RE_EPS_AFTER.match(code_text[close + 1:close + 1 + 200])
        before = _RE_EPS_BEFORE.search(code_text[max(0, m.start() - 200):m.start()])
        if (after and _is_epsilon(after.group(1))) or (before and _is_epsilon(before.group(1))):
            sites.append((m.start(), close + 1 + (after.end() if after else 0), "fabs_zero"))
    return sites


_RE_LEFT_OPERAND = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\s*\(\s*\))?\s*$")
_RE_RIGHT_OPERAND = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)(\s*\(\s*\))?")
_RE_ZERO_AT_END = re.compile(rf"{_ZERO}\s*$")
_RE_ZERO_AT_START = re.compile(rf"^\s*{_ZERO}")


_CAST_TYPES = (
    "double", "float", "int", "unsigned", "long", "qreal", "qint64", "quint64",
    "size_t", "int64_t", "uint64_t",
)
_RE_STATIC_CAST_TAIL = re.compile(r"static_cast\s*<\s*[A-Za-z_][A-Za-z0-9_:\s]*>\s*$")
_RE_STATIC_CAST_HEAD = re.compile(r"^\s*static_cast\s*<\s*[A-Za-z_][A-Za-z0-9_:\s]*>\s*(?=\()")
_RE_C_CAST_HEAD = re.compile(rf"^\s*\(\s*(?:{'|'.join(_CAST_TYPES)})\s*\)\s*")


def _operand_of_group(inner: str) -> tuple[str, bool, bool] | None:
    """(identifier, is_niladic_call, is_qualified) if `inner` is a bare operand chain, looking
    through any further redundant grouping parens; None otherwise."""
    inner = _unwrap_parens(inner)
    m = _RE_DURATION_OPERAND_CHAIN.match(inner)
    return (m.group(2), bool(m.group(3)), bool(m.group(1))) if m else None


def _left_operand(left: str) -> tuple[str, bool, bool] | None:
    """The operand ending at the end of `left`: a bare ``ident`` / ``ident()`` (as before), or
    a PARENTHESIZED operand -- ``(elapsed_ms)``, ``((elapsed_ms))``, ``static_cast<double>(x)``.
    A call with arguments (``foo(elapsed_ms) > 0``) is deliberately NOT unwrapped: the paren
    there belongs to the call, so the compared value is foo's result, not the identifier."""
    s = left.rstrip()
    m = _RE_LEFT_OPERAND.search(s)
    if m:
        return m.group(1), bool(m.group(2)), s[:m.start()].rstrip().endswith(_QUALIFIER_TAILS)
    if not s.endswith(")"):
        return None
    depth = 0
    for idx in range(len(s) - 1, -1, -1):
        if s[idx] == ")":
            depth += 1
        elif s[idx] == "(":
            depth -= 1
            if depth == 0:
                pre = s[:idx].rstrip()
                if pre and (pre[-1].isalnum() or pre[-1] in "_]") and not _RE_STATIC_CAST_TAIL.search(pre):
                    return None
                if pre.endswith(">") and not _RE_STATIC_CAST_TAIL.search(pre):
                    return None
                return _operand_of_group(s[idx + 1:-1])
    return None


def _right_operand(right: str) -> tuple[str, bool, bool] | None:
    """Mirror of `_left_operand` for the operand starting at the start of `right`; also looks
    through a leading C-style numeric cast (``(double)elapsed_ms``)."""
    s = right.lstrip()
    c = _RE_C_CAST_HEAD.match(s)
    if c:
        s = s[c.end():]
    sc = _RE_STATIC_CAST_HEAD.match(s)
    if sc:
        s = s[sc.end():]
    else:
        m = _RE_RIGHT_OPERAND.match(s)
        if m:
            # Followed by `.`/`->`/`::` the matched word is only a qualifier of the real operand.
            return m.group(1), bool(m.group(2)), s[m.end():].lstrip().startswith(_QUALIFIER_TAILS)
    if not s.startswith("("):
        return None
    close = _find_matching_close(s, 0)
    if close is None:
        return None
    return _operand_of_group(s[1:close])


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
        left_op = _left_operand(left)
        right_op = _right_operand(right)
        zero_left = bool(_RE_ZERO_AT_END.search(left))
        zero_right = bool(_RE_ZERO_AT_START.match(right))
        if zero_right and left_op and _is_duration_identifier(left_op[0]):
            if left_op[1]:
                found_call = True
            else:
                found_ident = True
        if zero_left and right_op and _is_duration_identifier(right_op[0]):
            if right_op[1]:
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
    # git prints paths as UTF-8; text=True alone decodes with the locale codec (cp1252 here), which
    # turns tests/caf<e-acute>.cpp into a path that names nothing on disk.
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", *SCAN_GLOBS],
        capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", check=True,
    )
    return [part for part in proc.stdout.split("\0") if part.strip()]


_RE_RAW_STRING_OPEN = re.compile(r'(?:u8|u|U|L)?R"([^ ()\\\t\v\f\n\r"]{0,16})\(')


def _raw_string_at(text: str, i: int) -> re.Match[str] | None:
    """The raw-string opener (``R"delim(``, optionally ``u8``/``u``/``U``/``L``-prefixed)
    whose prefix begins at `text[i]`, or None. Refuses an `R` that is merely the tail of a
    longer identifier (``FOOR"x"`` is not a raw string)."""
    if i > 0 and (text[i - 1].isalnum() or text[i - 1] == "_"):
        return None
    return _RE_RAW_STRING_OPEN.match(text, i)


# Every character `str.splitlines()` treats as a line boundary. scan_text splits BOTH
# comment-stripped views with splitlines(), so blanking a raw-string body must keep these (not
# only \r\n) or a \v/\f/\x1c-\x1e/\x85/ /  inside a body would split the anchor view
# but not the trigger view and desynchronise their line numbers.
_LINE_BREAKS = "\r\n\v\f\x1c\x1d\x1e\x85  "


def _strip_comments(text: str, blank_raw_strings: bool = False) -> str:
    """Blank out `//` and `/* */` comment content (tracking string literals so a comment
    marker inside a string is not mistaken for a real one), keeping every newline in place
    so line numbers are unaffected. This function's job is to stop comment TEXT (e.g. a
    docstring that mentions ``Milliseconds() > 0.0`` as prose) from being read as code, and
    to keep a comment edit from re-keying a site: the ANCHOR of a genuine candidate is
    normalize_anchor() of the comment-stripped line too, so changing only a trailing comment
    on an otherwise-unchanged code line no longer changes that line's anchor.
    Deliberately spans physical lines for ``/* */`` (a same-line-only comment strip would
    miss a continuation line of a multi-line block comment).

    C++ raw string literals (``R"delim( ... )delim"``) are tracked as their own state: their
    body is not escape-processed, so a ``"``/``//``/``/*`` inside one neither ends the string
    early nor opens a comment (which used to HIDE the real code after it), and with
    `blank_raw_strings=True` (the TRIGGER view) the body is blanked to spaces so text inside
    an embedded shader/regex/JSON literal cannot FAKE a site. The ANCHOR view keeps the body
    verbatim: a change to a literal's data must still change the anchor.
    """
    out: list[str] = []
    state = "code"  # "code" | "string" | "line_comment" | "block_comment"
    quote = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if state == "code" and ch in ("R", "u", "U", "L"):
            raw = _raw_string_at(text, i)
            if raw:
                terminator = ")" + raw.group(1) + '"'
                end = text.find(terminator, raw.end())
                if end != -1:
                    body = text[raw.end():end]
                    out.append(raw.group(0))
                    out.append(
                        "".join(c if c in _LINE_BREAKS else " " for c in body)
                        if blank_raw_strings else body
                    )
                    out.append(terminator)
                    i = end + len(terminator)
                    continue
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
    # Two comment-stripped views of the same text: the TRIGGER view also blanks raw-string
    # bodies (so embedded non-C++ text cannot fake a site); the ANCHOR view keeps them (so a
    # literal's data still keys the site) but drops comments (so a comment edit does not).
    code_text = _strip_comments(text, blank_raw_strings=True)
    anchor_lines = _strip_comments(text).splitlines()
    code_only_lines = code_text.splitlines()
    line_starts: list[int] = []
    offset = 0
    for physical in code_text.splitlines(keepends=True):
        line_starts.append(offset)
        offset += len(physical)

    def line_of(pos: int) -> int:
        return bisect.bisect_right(line_starts, pos)  # 1-indexed

    sites: list[tuple[int, int, str]] = []  # (first line, last line, trigger)
    for lineno, code_only in enumerate(code_only_lines, start=1):
        sites.extend((lineno, lineno, t) for t in _line_triggers(code_only))
    for start, end, trigger in _macro_and_fabs_sites(code_text):
        sites.append((line_of(start), line_of(max(start, end - 1)), trigger))
    sites.sort()
    for first, last, trigger in sites:
        # A site spanning several physical lines is keyed by ALL its lines, joined.
        anchor = normalize_anchor(" ".join(anchor_lines[first - 1:last]))
        trig_set, lines = by_anchor.setdefault(anchor, (set(), []))
        trig_set.add(trigger)
        if first not in lines:
            lines.append(first)
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
