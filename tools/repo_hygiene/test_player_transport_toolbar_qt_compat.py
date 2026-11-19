"""Guard PlayerTransportToolbar.h against Qt-version-specific API with no version branch.

PLAYER-TOOLBAR-TRANSPORT-BUTTONS-1 r2 (PR 857; CoS advisory record c5033af): Qt 5 is a documented,
supported kit (docs/11-build-macos-linux.md; .github/workflows/macOS-Intel.yml builds the macOS
Intel release with qt@5), but no PR check compiles it, because that workflow runs only on
workflow_dispatch. r1's mirroredIcon() called ``QImage::flipped()``, which exists only in Qt 6.9+,
and MainWindow.cpp includes the header, so the Qt 5 release kit failed to compile unseen. This test
reads the header's text and fails on any call to an API newer than Qt 5.15 that is not inside an
arm of a ``QT_VERSION`` check that compiles only on a Qt new enough for it. Same defect class as
the QMouseEvent::position() guard of PR 856 r3 (test_qt6_only_api_guard.py on that branch).

GRAMMAR (r5, fail closed; hub STOP RULE 20261119T0912Z). The scanner reads a file only where it
is certain what a compiler sees. Anything else it REFUSES: the file is reported unprovable (one
``REFUSED <kind>`` finding per refused form), which fails the test, and nothing else in it is read.

Read:
  * comments (``//``, ``/* */``) and the bodies of ordinary string and character literals are
    blanked; an unterminated ' or " ends at the line end; a ' inside a pp-number is a digit
    separator; CR, LF and CRLF each end a line;
  * every identifier token that IS an API name of NEWER_THAN_QT5_API is a use, wherever it
    stands: a member access (also one split across lines), a qualified name, a macro argument,
    a #define body, a conditional directive or a bare call. Each use is judged by the arms open
    at the physical line its name is on;
  * ``#if``/``#ifdef``/``#ifndef``/``#elif``/``#elifdef``/``#elifndef``/``#else``/``#endif``:
    only ``#if QT_VERSION >= QT_VERSION_CHECK(a, b, c)`` and ``#if QT_VERSION <
    QT_VERSION_CHECK(a, b, c)``, with a, b and c C preprocessor integer literals, open a trusted
    arm (the >= arm, or the #else of the < form); every other condition and every #elif,
    #elifdef and #elifndef arm is untrusted;
  * the other directives it models are ``#define``, ``#undef``, ``#include``, ``#pragma``,
    ``#error``, ``#warning``, ``#line`` and the null directive ``#``.

Refused, never read:
  * a backslash-newline splice anywhere in the file, comments and literals included, also with
    spaces or tabs before the line break (adversary r4 pass 6 A1 and every splice sibling);
  * a trigraph (``??=``, ``??/``, ...) anywhere in the file;
  * a digraph (``<:``, ``:>``, ``<%``, ``%>``, ``%:``) in code;
  * a raw string literal (``R"``, ``u8R"``, ...): its d-chars and body are not modelled;
  * a backslash in code outside a literal (a universal-character-name can spell a name);
  * a token paste (``##``) in code;
  * macro indirection: a #define whose replacement list names a macro this file #defines
    (adversary r4 pass 6 A2: ``#define INDIRECT APPLY``);
  * a #define or #undef of QT_VERSION or QT_VERSION_CHECK;
  * any other directive name (``#include_next``, ``#import``, ``#embed``, a ``# 12 "f"`` line
    marker, ...: adversary r5 A2), and a C++20 ``import``/``module`` line, which can import macros;
  * a character in code outside printable ASCII, tab, VT and FF (a BOM, NBSP, U+2028, a control
    character, a non-ASCII identifier: adversary r5 A1).

Non-promises: the scanner does not preprocess. It does not follow #include, does not expand a
macro defined in another file (an external macro that pastes pieces into an API name is not
seen), evaluates no #if but the two trusted forms, and reads no file that has a refused form.
It proves one thing about the scoped headers: no API name token stands outside a trusted arm.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCOPED_HEADERS = (ROOT / "platform" / "qt" / "PlayerTransportToolbar.h",)

# API -> the first Qt (major, minor) that has it.
NEWER_THAN_QT5_API = {
    "flipped": (6, 9),  # QImage::flipped(Qt::Orientations)
    "flip": (6, 9),  # QImage::flip(Qt::Orientations)
    "position": (6, 0),  # QSinglePointEvent / QMouseEvent accessors
    "globalPosition": (6, 0),
    "scenePosition": (6, 0),
}
_NAMES = "|".join(sorted(NEWER_THAN_QT5_API, key=len, reverse=True))
# r5: every identifier token that IS an API name is a use, however it is reached: `.`/`->`, any
# qualified name, a line break inside the access, a macro argument, a #define body or a bare call.
# One token rule replaces the r4 member-access matcher and the macro-argument walk, so no macro
# shape (an alias of a function-like macro included, adversary r4 pass 6 A2) can carry the name
# past it.
API_NAME = re.compile(rf"\b({_NAMES})\b")
IDENTIFIER = re.compile(r"\b[^\W\d]\w*")
DIRECTIVE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|elifdef|elifndef|else|endif)\b(.*)$")
# Any other directive name is refused (adversary r5 A2: `#elifdef` was not read); `#` alone is the
# null directive.
DIRECTIVE_NAME = re.compile(r"^\s*#\s*(\w*)")
MODELLED_DIRECTIVES = frozenset((
    "if", "ifdef", "ifndef", "elif", "elifdef", "elifndef", "else", "endif",
    "define", "undef", "include", "pragma", "error", "warning", "line", "",
))
# Every directive pattern is anchored at the line start and `\s` is wider than a compiler's
# whitespace, so code holds printable ASCII, tab, VT and FF only (adversary r5 A1: a BOM).
NOT_PRINTABLE_ASCII = re.compile(r"[^\t\v\f\x20-\x7e]")
# A C++20 module or header-unit import is a directive without `#` and can import macros.
MODULE_DIRECTIVE = re.compile(r"^\s*(?:export\s+)?(?:import|module)\b")
# Group 3 is the replacement list; a `(` straight after the name opens a parameter list.
DEFINE = re.compile(r"^\s*#\s*define\s+(\w+)(\([^)]*\))?(.*)$")
VERSION_MACRO_REDEFINED = re.compile(r"^\s*#\s*(?:define|undef)\s+QT_VERSION(?:_CHECK)?\b")
# Refused in the raw text, anywhere (comments and literals included): translation phases 1-2
# are what the scanner does not model, so it refuses every input where they change the text.
SPLICE = re.compile(r"\\[ \t\f\v]*\n")
TRIGRAPH = re.compile(r"\?\?[=/'()!<>\-]")
# Refused in code (comments and literal bodies blanked).
DIGRAPH = re.compile(r"<:|:>|<%|%>|%:")
TOKEN_PASTE = re.compile(r"##")
# Only the plain condition counts: a negated or compound one is "other" (fail closed). Each
# argument is a pp-number, read as a C preprocessor integer literal by _pp_int (r4, fable key3 r3).
_PP_NUMBER = r"(\.?\d(?:[eEpP][+-]|'[0-9A-Za-z_]|[0-9A-Za-z_.])*)"
VERSION_CHECK = re.compile(
    rf"QT_VERSION\s*(>=|<)\s*QT_VERSION_CHECK\s*\(\s*{_PP_NUMBER}\s*,\s*{_PP_NUMBER}\s*,\s*{_PP_NUMBER}\s*\)"
)
_INT_LITERAL = re.compile(
    r"(?:0[xX](?P<hex>[0-9A-Fa-f](?:'?[0-9A-Fa-f])*)|0[bB](?P<bin>[01](?:'?[01])*)"
    r"|(?P<oct>0(?:'?[0-7])*)|(?P<dec>[1-9](?:'?[0-9])*))"
    r"(?:[uU](?:ll|LL|[lL])?|(?:ll|LL|[lL])[uU]?)?"
)


def _pp_int(token: str) -> int | None:
    """The value the preprocessor gives an integer literal (leading 0 octal, 0x hex, 0b binary,
    ' digit separators, u/l/ll suffixes), or None when it is not one: an unparsable version
    argument makes the condition "other", which is never trusted."""
    literal = _INT_LITERAL.fullmatch(token)
    if not literal:
        return None
    for group, base in (("hex", 16), ("bin", 2), ("oct", 8), ("dec", 10)):
        digits = literal.group(group)
        if digits is not None:
            value = int(digits.replace("'", ""), base)
            return value if value < 2**63 else None  # past intmax_t the #if is ill-formed
    return None


def _qt_version_value(major: int, minor: int, patch: int) -> int | None:
    """QT_VERSION_CHECK(major, minor, patch) is ((major << 16) | (minor << 8) | (patch)). A value
    of 2**31 or more is None: the preprocessor's intmax_t / uintmax_t arithmetic can overflow or
    wrap there (adversary r4 A4: (2**48 + 5ULL) << 16 wraps to 0x050900), and no Qt is near it."""
    value = (major << 16) | (minor << 8) | patch
    return value if value < 2**31 else None


Line = tuple[int, str, list[int]]  # (physical start line, logical line, physical line per char)


def _physical(where: list[int], k: int, number: int) -> int:
    return where[k] if 0 <= k < len(where) else number


# Preprocessing tokens by maximal munch, enough to tell where a pp-number ends: a pp-number is a
# digit or `.digit`, then digits, identifier characters, `.`, `e+ e- E+ E- p+ p- P+ P-`, and `'`
# followed by a digit or nondigit ([lex.ppnumber]).
_PP_TOKEN = re.compile(r"(\.?[0-9](?:[eEpP][+-]|'[0-9A-Za-z_]|[\w.])*)|[^\W\d]\w*|.", re.S)
_PP_NONDIGIT_OR_DIGIT = re.compile(r"[0-9A-Za-z_]")


def _ends_in_pp_number(code: str) -> bool:
    last = None
    for last in _PP_TOKEN.finditer(code):
        pass
    return bool(last and last.group(1) and last.end() == len(code))


def _logical_lines(text: str) -> tuple[list[Line], list[tuple[int, str]]]:
    """The logical lines of a splice-free, trigraph-free, LF-only text, and the first refusal the
    lexer meets (a raw string, or a backslash in code). Comments and the bodies of string and char
    literals are blanked, so a name inside them never counts; a block comment that spans lines
    joins them into one logical line, as the preprocessor reads it; an unterminated ' or " ends at
    the line end."""
    out: list[Line] = []
    current: list[str] = []
    where: list[int] = []  # physical line of each character in `current` (adversary r4 A5)
    start = line = 1
    state = "code"  # code | line_comment | block_comment | string | char

    def emit(chars: str) -> None:
        current.extend(chars)
        where.extend([line] * len(chars))

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\n":
            if state in ("line_comment", "string", "char"):
                state = "code"
            line += 1
            if state == "block_comment":
                i += 1
                continue
            out.append((start, "".join(current), where))
            current, where, start = [], [], line
            i += 1
            continue
        nxt = text[i + 1] if i + 1 < n else ""
        if state == "code":
            if ch == "/" and nxt == "/":
                state = "line_comment"
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block_comment"
                emit(" ")
                i += 2
                continue
            code = "".join(current)
            # A ' continues a pp-number only when a digit or nondigit follows it (1'000, .5'0,
            # 1e+a'0); anywhere else it opens a char literal (u8'x', 1'"'), by maximal munch.
            if ch == "'" and _PP_NONDIGIT_OR_DIGIT.match(nxt) and _ends_in_pp_number(code):
                emit(ch)
                i += 1
                continue
            if ch == '"' and re.search(r"\w*$", code).group(0).endswith("R"):
                return out, [(line, "raw-string")]  # R"d(...)d", u8R"...": not modelled
            if ch == "\\":
                return out, [(line, "backslash")]  # a universal-character-name can spell an API name
            if ch in "\"'":
                state = "string" if ch == '"' else "char"
            emit(ch)
        elif state == "block_comment":
            if ch == "*" and nxt == "/":
                state = "code"
                i += 2
                continue
        elif state in ("string", "char"):
            if ch == "\\" and i + 1 < n:
                i += 2
                continue
            if ch == ('"' if state == "string" else "'"):
                state = "code"
                emit(ch)
        i += 1
    out.append((start, "".join(current), where))
    return out, []


def _refused_forms(lines: list[Line]) -> list[tuple[int, str]]:
    """(physical line, kind) of each form the scanner refuses in code (see the GRAMMAR)."""
    refused: list[tuple[int, str]] = []
    defined = {m.group(1) for _, line, _ in lines if (m := DEFINE.match(line))}
    for number, line, where in lines:
        for kind, pattern in (("digraph", DIGRAPH), ("token-paste", TOKEN_PASTE), ("non-ascii", NOT_PRINTABLE_ASCII)):
            refused.extend((_physical(where, m.start(), number), kind) for m in pattern.finditer(line))
        name = DIRECTIVE_NAME.match(line)
        if name and name.group(1) not in MODELLED_DIRECTIVES:
            refused.append((_physical(where, name.start(1), number), "unknown-directive"))
        if MODULE_DIRECTIVE.match(line):
            refused.append((number, "unknown-directive"))
        if VERSION_MACRO_REDEFINED.match(line):
            refused.append((number, "version-macro-redefined"))
        define = DEFINE.match(line)
        if define:
            for name in IDENTIFIER.finditer(line, define.start(3)):
                if name.group(0) in defined:
                    refused.append((_physical(where, name.start(), number), "macro-indirection"))
    return refused


def unguarded_newer_api_calls(text: str) -> list[tuple[int, str, str]]:
    """(line, api name, line text) for each API name token outside an arm that compiles only on a
    Qt new enough for it; or, when the text has any form the GRAMMAR refuses, only the refusals,
    as (line, "REFUSED <kind>", line text): such a file is unprovable and nothing else is read."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    physical_lines = text.split("\n")
    refused = [
        (text.count("\n", 0, m.start()) + 1, kind)
        for kind, pattern in (("splice", SPLICE), ("trigraph", TRIGRAPH))
        for m in pattern.finditer(text)
    ]
    lines: list[Line] = []
    if not refused:
        lines, refused = _logical_lines(text)
    if not refused:
        refused = _refused_forms(lines)
    if refused:
        return sorted({(n, f"REFUSED {kind}", physical_lines[n - 1].strip()) for n, kind in refused})
    return _scan(lines)


def _scan(lines: list[Line]) -> list[tuple[int, str, str]]:
    # Each frame: [kind, version, arm_floor]. kind "ge" (#if QT_VERSION >= V), "lt" (#if QT_VERSION
    # < V) or "other"; V and arm_floor are QT_VERSION_CHECK values, arm_floor the lowest Qt the
    # current arm can compile on, or None.
    stack: list[list] = []
    found: list[tuple[int, str, str]] = []

    def trusted(api: str, floors: tuple[int, ...]) -> bool:
        needed = _qt_version_value(*NEWER_THAN_QT5_API[api], 0)
        return any(floor >= needed for floor in floors)

    for number, line, where in lines:
        directive = DIRECTIVE.match(line)
        # A name in a conditional directive is judged with no arm open (fail closed); on any
        # other line, by the arms open there. Each name is placed on its own physical line.
        floors = () if directive else tuple(frame[2] for frame in stack if frame[2] is not None)
        for match in API_NAME.finditer(line):
            if not trusted(match.group(1), floors):
                found.append((_physical(where, match.start(1), number), match.group(1), line.strip()))
        if directive:
            keyword, rest = directive.group(1), directive.group(2)
            if keyword in ("if", "ifdef", "ifndef"):
                check = VERSION_CHECK.fullmatch(rest.strip()) if keyword == "if" else None
                args = [_pp_int(check.group(k)) for k in (2, 3, 4)] if check else [None]
                version = None if None in args else _qt_version_value(*args)
                if check and version is not None:
                    if check.group(1) == ">=":
                        stack.append(["ge", version, version])
                    else:
                        stack.append(["lt", version, None])
                else:
                    stack.append(["other", None, None])
            elif keyword.startswith("elif") and stack:
                stack[-1] = ["other", None, None]  # an #elif / #elifdef / #elifndef arm is never trusted
            elif keyword == "else" and stack:
                kind, version, _ = stack[-1]
                stack[-1][2] = version if kind == "lt" else None
            elif keyword == "endif" and stack:
                stack.pop()
    found.sort(key=lambda hit: hit[0])
    return found


class PlayerTransportToolbarQtCompatTests(unittest.TestCase):
    def test_scoped_headers_exist(self) -> None:
        # A rename must not turn the guard below into a silent pass.
        for header in SCOPED_HEADERS:
            with self.subTest(header=header.name):
                self.assertTrue(header.is_file(), f"scoped header missing: {header}")

    def test_scoped_headers_have_no_unguarded_newer_qt_api(self) -> None:
        for header in SCOPED_HEADERS:
            with self.subTest(header=header.name):
                hits = unguarded_newer_api_calls(header.read_text(encoding="utf-8"))
                self.assertFalse(
                    hits,
                    f"{header.name}: an API newer than Qt 5.15 outside a matching "
                    "`#if QT_VERSION >= QT_VERSION_CHECK(...)` arm breaks the Qt 5 release kit "
                    "(macOS-Intel.yml qt@5); add a #else branch (QImage::flipped -> mirrored()). "
                    "A REFUSED finding is a form the scanner does not read (see its GRAMMAR): "
                    + "; ".join(f"line {n}: {api}: {src}" for n, api, src in hits),
                )

    def test_scanner_flags_the_09077b0d_shape(self) -> None:
        # r1's own line: proves the guard bites on the defect it exists for.
        fixture = (
            "inline QIcon mirroredIcon( const QString &resource )\n"
            "{\n"
            "    const QImage image( resource );\n"
            "    return QIcon( QPixmap::fromImage( image.flipped( Qt::Horizontal ) ) );\n"
            "}\n"
        )
        self.assertEqual([(n, api) for n, api, _ in unguarded_newer_api_calls(fixture)], [(4, "flipped")])

    def test_scanner_flags_each_api_and_member_form(self) -> None:
        for call in ("img.flipped()", "img->flipped( Qt::Vertical )", "img.flip()", "img.QImage::flipped()",
                     "&QImage::flipped", "( img . flipped )()", "e->position()", "e.globalPosition()",
                     "e.scenePosition()"):
            with self.subTest(call=call):
                self.assertEqual(len(unguarded_newer_api_calls(f"auto p = {call};\n")), 1)

    def test_scanner_accepts_the_guarded_shapes(self) -> None:
        ge_form = (
            "#if QT_VERSION >= QT_VERSION_CHECK( 6, 9, 0 )\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#else\n"
            "    return image.mirrored( true, false );\n"
            "#endif\n"
        )
        lt_form = (
            "#if QT_VERSION < QT_VERSION_CHECK(6, 9, 0)\n"
            "    return image.mirrored( true, false );\n"
            "#else\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#endif\n"
        )
        newer = "#if QT_VERSION >= QT_VERSION_CHECK(7, 0, 0)\nreturn image.flipped();\n#endif\n"
        self.assertEqual(unguarded_newer_api_calls(ge_form), [])
        self.assertEqual(unguarded_newer_api_calls(lt_form), [])
        self.assertEqual(unguarded_newer_api_calls(newer), [])

    def test_a_qt6_0_branch_is_not_enough_for_a_qt6_9_api(self) -> None:
        # flipped() needs 6.9: a Qt 6.0 arm still breaks Qt 6.0-6.8, but is enough for position().
        fixture = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)\n"
            "return image.flipped();\n"
            "auto p = e->position();\n"
            "#endif\n"
        )
        self.assertEqual([(n, api) for n, api, _ in unguarded_newer_api_calls(fixture)], [(2, "flipped")])

    def test_scanner_flags_a_call_in_the_old_arm_or_after_the_branch(self) -> None:
        fixture = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "    return image.mirrored( true, false );\n"
            "#else\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#endif\n"
            "auto q = image.flipped();\n"
        )
        self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(fixture)], [4, 6])

    def test_scanner_only_trusts_the_plain_version_condition(self) -> None:
        for condition in (
            "!(QT_VERSION >= QT_VERSION_CHECK(6, 9, 0))",
            "QT_VERSION >= QT_VERSION_CHECK(6, 9, 0) || 1",
            "QT_VERSION < QT_VERSION_CHECK(6, 9, 0) && 0",
        ):
            with self.subTest(condition=condition):
                fixture = f"#if {condition}\nreturn i.flipped();\n#else\nreturn i.flipped();\n#endif\n"
                self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(fixture)], [2, 4])
        elif_form = (
            "#if QT_VERSION < QT_VERSION_CHECK(5, 0, 0)\nx();\n"
            "#elif QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\nreturn i.flipped();\n#endif\n"
        )
        self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(elif_form)], [4])

    def test_scanner_ignores_comments_and_literals(self) -> None:
        fixture = (
            "// image.flipped() is Qt 6.9 only\n"
            "/* img.flip() and\n"
            "   e->position() */\n"
            'const char *s = "img.flipped()";\n'
            "const char c = 'f', *t = \"e->position() \\\" img.flip()\";\n"
            "auto p = image.mirrored( true, false );\n"
        )
        self.assertEqual(unguarded_newer_api_calls(fixture), [])
        self.assertEqual(len(unguarded_newer_api_calls('const char *s = "//"; auto p = i.flipped();\n')), 1)
        self.assertEqual(len(unguarded_newer_api_calls("const int n = 1'000; auto p = i.flipped();\n")), 1)

    def assertRefused(self, source: str, kinds: list[str], lines: list[int] | None = None) -> None:
        # r5 GRAMMAR: a refused form makes the whole file unprovable; only refusals are reported.
        hits = unguarded_newer_api_calls(source)
        self.assertEqual(sorted({api for _, api, _ in hits}), sorted(f"REFUSED {k}" for k in set(kinds)), hits)
        if lines is not None:
            self.assertEqual(sorted({n for n, _, _ in hits}), lines, hits)

    def test_digraphs_are_refused(self) -> None:
        # r5: digraphs are not modelled (r2 read `%:` as `#`), so a digraph in code is refused.
        fixture = (
            "%: if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "return i.flipped();\n"
            "%:else\n"
            "return i.flipped();\n"
            "%:endif\n"
        )
        self.assertRefused(fixture, ["digraph"], [1, 3, 5])
        for source in ("int a<:1:>;\n", "void f() <% %>\n", "x = a <::b;\n"):
            with self.subTest(source=source):
                self.assertRefused(source, ["digraph"], [1])
        # In a comment or a literal a digraph is text.
        self.assertEqual(unguarded_newer_api_calls('// <: %:\nauto s = ":/a<%";\n'), [])

    def test_scanner_sees_macro_aliases_and_refuses_trigraphs(self) -> None:
        # adversary r2 A2 (da60b814): both inputs used to return no finding.
        alias = "#define FLIP flipped\nreturn image.FLIP(Qt::Horizontal);\n"
        self.assertEqual([(n, api) for n, api, _ in unguarded_newer_api_calls(alias)], [(1, "flipped")])
        trigraph = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n"
            "??=endif\n"
            "return image.flipped(Qt::Horizontal);\n"
        )
        self.assertRefused(trigraph, ["trigraph"], [2])
        guarded_alias = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "#define FLIP( i ) ( i ).flipped( Qt::Horizontal )\n"
            "#else\n"
            "#define FLIP( i ) ( i ).mirrored( true, false )\n"
            "#endif\n"
            "#define MIRROR mirrored\n"
        )
        self.assertEqual(unguarded_newer_api_calls(guarded_alias), [])

    def test_scanner_sees_a_line_break_inside_the_member_access(self) -> None:
        # r3 keys on fcbae668 (sol B1, opus, fable key3): each input used to return no finding,
        # because the matcher ran per logical line. Whitespace, including a line break, CR or CRLF,
        # between the accessor and the name, or between QImage and ::, is inside the grammar.
        for source, api in (
            ("return image.\nflipped(Qt::Horizontal);\n", "flipped"),
            ("return image->\nflipped(Qt::Horizontal);\n", "flipped"),
            ("auto f = &QImage::\nflipped;\n", "flipped"),
            ("return image.QImage::\nflipped(Qt::Horizontal);\n", "flipped"),
            ("auto p = event->\nposition();\n", "position"),
            ("return image.\r\nflipped(Qt::Horizontal);\r\n", "flipped"),
            ("auto f = &QImage ::\nflipped;\n", "flipped"),
            ("auto f = &QImage\n::flipped;\n", "flipped"),
            ("return image\n.\nflipped();\n", "flipped"),
            ("return image. /* c */\n// line\nflipped();\n", "flipped"),
            ("return image.\r\r\nflipped();\n", "flipped"),
        ):
            with self.subTest(source=source):
                self.assertEqual([a for _, a, _ in unguarded_newer_api_calls(source)], [api])
        # The finding names the line the API name is on.
        self.assertEqual(
            [(n, a) for n, a, _ in unguarded_newer_api_calls("x();\nreturn image.\nflipped();\n")],
            [(3, "flipped")],
        )

    def test_a_line_break_member_access_is_judged_at_the_name(self) -> None:
        # The name token is what needs the newer Qt: inside the arm it is guarded, outside it is not.
        guarded = (
            "return image.\n"
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "flipped( Qt::Horizontal );\n"
            "#else\n"
            "mirrored( true, false );\n"
            "#endif\n"
        )
        self.assertEqual(unguarded_newer_api_calls(guarded), [])
        unguarded = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "return image.\n"
            "#endif\n"
            "flipped( Qt::Horizontal );\n"
        )
        self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(unguarded)], [(4, "flipped")])

    def test_version_arguments_are_read_as_preprocessor_integer_literals(self) -> None:
        # fable key3 r3: 010 is octal 8, so this arm also compiles on Qt 6.8 and is not a guard.
        def arm(major: str, minor: str, patch: str = "0", op: str = ">=") -> str:
            body = "return image.flipped();\n"
            other = "return image.mirrored( true, false );\n"
            head = f"#if QT_VERSION {op} QT_VERSION_CHECK( {major}, {minor}, {patch} )\n"
            return head + (body + "#else\n" + other if op == ">=" else other + "#else\n" + body) + "#endif\n"

        flagged = (
            ("6", "010", "0"),  # octal 8
            ("6", "0x8", "0"),  # hex 8
            ("6", "0b1000", "0"),  # binary 8
            ("6", "08", "0"),  # not a valid octal literal: unparsable is never a pass
            ("6", "9.0", "0"),  # not an integer literal
            ("6", "9z", "0"),  # not a preprocessor integer suffix
            ("6", "9", "0x"),  # a malformed patch is unparsable too
            ("6", "8", "255"),  # 6.8.255 is still below 6.9
        )
        for args in flagged:
            for op in (">=", "<"):
                with self.subTest(args=args, op=op):
                    self.assertEqual([a for _, a, _ in unguarded_newer_api_calls(arm(*args, op=op))], ["flipped"])
        accepted = (
            ("6", "011", "0"),  # octal 9
            ("6", "0x9", "0"),
            ("6", "0X0A", "0"),
            ("6", "0b1001", "0"),
            ("6", "1'0", "0"),  # 10, digit separator
            ("6u", "9U", "0ul"),
            ("6LL", "9llu", "0"),
            ("06", "9", "0"),
            ("6", "8", "256"),  # QT_VERSION_CHECK is (6<<16)|(8<<8)|256 == 6.9.0
            ("7", "0", "0"),
        )
        for args in accepted:
            for op in (">=", "<"):
                with self.subTest(args=args, op=op):
                    self.assertEqual(unguarded_newer_api_calls(arm(*args, op=op)), [])

    def test_an_apostrophe_in_directive_text_does_not_hide_later_code(self) -> None:
        # An unterminated ' or " ends at the line end, as the preprocessor reads it.
        for directive in ("#error it's Qt 6 only", '#warning "unterminated', "#pragma message 'x"):
            with self.subTest(directive=directive):
                fixture = f"{directive}\nreturn image.flipped();\n"
                self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(fixture)], [(2, "flipped")])

    def test_a_trigraph_is_refused_anywhere(self) -> None:
        # adversary r4 A1 (8a66187d) read `??/` both ways; r5 refuses every trigraph instead, in
        # code, a directive, a comment or a literal.
        for source, line in (
            ("#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n#en??/\ndif\nreturn image.flipped();\n", 2),
            ("// note ??/\nreturn image.flipped();\n", 1),
            ('auto s = "what??!";\n', 1),
            ("x();\nint a ??( 1 ??);\n", 2),
        ):
            with self.subTest(source=source):
                self.assertRefused(source, ["trigraph"], [line])
        # One call is one finding.
        self.assertEqual(len(unguarded_newer_api_calls("auto p = i.flipped(); auto q = i.flipped();\n")), 2)

    def test_every_backslash_newline_splice_is_refused(self) -> None:
        # adversary r4 pass 5 A1 (202596e2) and pass 6 A1 (3c82e35e): r5 refuses every splice, with
        # or without spaces or tabs before the line break, wherever it stands.
        for source, line in (
            ("#define JOIN(a,b) a #\\ \n# b\nreturn image.JOIN(flipp,ed)(Qt::Horizontal);\n", 1),
            ("#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n#en\\\t \ndif\nreturn image.flipped();\n", 2),
            ("// note \\  \nreturn image.flipped();\n", 1),
            ("auto p = img.flip\\\nped();\n", 1),
            ("/\\\n* x */ auto p = i.flipped();\n", 1),
            ("/* x *\\\n/ auto p = i.flipped();\n", 1),
            ("/* x *\\\r\n/ auto p = i.flipped();\r\n", 1),
            ("x();\n/* x *\\\r/ auto p = i.flipped();\r", 2),
            ('auto s = "a\\\nb";\n', 1),
            ("#define F(x) \\\n  x\n", 1),
        ):
            with self.subTest(source=source):
                self.assertRefused(source, ["splice"], [line])
        # A backslash that does not end its line is not a splice (an escape in a literal).
        self.assertEqual(unguarded_newer_api_calls('auto s = "a\\\\"; auto c = \'\\n\';\n'), [])

    def test_a_backslash_in_code_is_refused(self) -> None:
        # r5: a universal-character-name can spell an API name; any backslash in code is refused.
        self.assertRefused("auto p = img.fl\\u0069pped();\n", ["backslash"], [1])
        self.assertRefused("x();\nauto p = \\U00000066lip;\n", ["backslash"], [2])

    def test_a_digit_separator_does_not_open_a_char_literal(self) -> None:
        # r5 sibling sweep: a pp-number may start with `.digit`, so `.5'0` is one number and the '
        # is a separator; read as a char literal it hid the rest of the line.
        # ...and a ' that a digit or nondigit does not follow opens a char literal even after a
        # pp-number, so the literal's `"` must not open a string (all inside a discarded macro arg).
        for source in ("auto d = .5'0; return image.flipped();\n", "auto d = x + 0x1'f; auto p = i.flip();\n",
                       "auto d = 1e+5'0; auto p = e->position();\n", "F(1e+a'0, image.flipped());\n",
                       "F(e+5'0, image.flipped());\n", "F(1'\"', image.flipped());\n",
                       "F(1'é\"', image.flipped());\n"):
            with self.subTest(source=source):
                self.assertEqual(len(unguarded_newer_api_calls(source)), 1)
        # A char literal after an identifier with a digit (u8'x') is still a literal.
        self.assertEqual(unguarded_newer_api_calls("auto c = u8'f'; auto d = U'\"'; x();\n"), [])
        self.assertEqual(len(unguarded_newer_api_calls("auto c = u8'\"'; auto p = i.flipped();\n")), 1)

    def test_an_api_name_passed_to_a_function_like_macro_is_a_finding(self) -> None:
        # adversary r4 pass 5 A2: the scan does not expand macros, so an API name in the arguments
        # of a function-like macro this text defines counts where the macro is used.
        for source in (
            "#define APPLY(i, member) (i).member(Qt::Horizontal)\nreturn APPLY(image, flipped);\n",
            "#define CALL( f ) image.f()\nauto p = CALL(\n  position );\n",
            "#define WRAP(x) x\nreturn WRAP( g( image.mirrored(), flip ) );\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(len(unguarded_newer_api_calls(source)), 1)
                self.assertGreaterEqual(unguarded_newer_api_calls(source)[0][0], 2)
        guarded = (
            "#define APPLY(i, member) (i).member(Qt::Horizontal)\n"
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\nreturn APPLY(image, flipped);\n#endif\n"
            "return APPLY(image, mirrored);\n"
        )
        self.assertEqual(unguarded_newer_api_calls(guarded), [])

    def test_any_qualified_api_name_is_a_finding(self) -> None:
        # adversary r4 pass 5 A3: an alias (`using Image = QImage;`) hides the class name, so any
        # `X::name` counts, not only `QImage::name`.
        for source in (
            "using Image = QImage;\nauto fn = &Image::flipped;\n",
            "typedef QImage I;\nauto fn = &I :: flip;\n",
            "auto fn = &decltype(image)::flipped;\n",
            "auto fn = &std::remove_cvref_t<decltype(image)>::flipped;\n",
            "auto p = &QMouseEvent::position;\n",
        ):
            with self.subTest(source=source):
                self.assertEqual(len(unguarded_newer_api_calls(source)), 1)

    def test_a_raw_string_is_refused(self) -> None:
        # adversary r4 pass 5 A4 found a d-char (`"`) the r4 lexer did not model; r5 refuses every
        # raw string literal, whatever its prefix and delimiter.
        for source in ('auto s = R""(//)""; return image.flipped(Qt::Horizontal);\n',
                       'auto s = R"a"b(/* )a"b"; return image.flipped();\n',
                       'auto s = R"(img.flipped())";\n', 'auto s = u8R"x()x";\n', 'auto s = LR"(a)";\n'):
            with self.subTest(source=source):
                self.assertRefused(source, ["raw-string"], [1])
        # An ordinary string or a prefix that is not raw is read.
        self.assertEqual(unguarded_newer_api_calls('auto s = u8"(a)"; auto t = L"R";\n'), [])

    def test_a_token_paste_is_refused(self) -> None:
        # adversary r4 A2: the scan does not expand macros, so it refuses a paste instead of
        # following it, wherever it sits.
        self.assertRefused("#define JOIN(a,b) a ## b\nreturn image.JOIN(flip,ped)(Qt::Horizontal);\n",
                           ["token-paste"], [1])
        self.assertRefused("#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n#define JOIN(a,b) a##b\n#endif\n",
                           ["token-paste"], [2])
        self.assertRefused("#define JOIN(a,b) a%:%:b\n", ["digraph"], [1])
        self.assertRefused("#define JOIN(a,b) a ??=??= b\n", ["trigraph"], [1])
        # `#` stringizing and a `##` in a comment or string are not pastes.
        self.assertEqual(unguarded_newer_api_calls('#define S(x) #x /* a ## b */ "##"\n'), [])

    def test_macro_indirection_is_refused(self) -> None:
        # adversary r4 pass 6 A2: a #define whose replacement names a macro this file #defines
        # (before or after it, object-like or function-like) is refused.
        for source, lines in (
            ("#define INDIRECT APPLY\n#define APPLY(i, m) (i).m()\nreturn INDIRECT(image, mirrored);\n", [1]),
            ("#define A B\n#define B C\n#define C(x) x\n", [1, 2]),
            ("#define SELF SELF\n", [1]),
            ("#define F(x) G(x)\n#define G(x) x\n", [1]),
        ):
            with self.subTest(source=source):
                self.assertRefused(source, ["macro-indirection"], lines)
        # A replacement that names only parameters, keywords or names from other files is read.
        self.assertEqual(unguarded_newer_api_calls("#define F(x, m) (x).m(Qt::Horizontal)\n#define G Q_UNUSED\n"), [])

    def test_a_redefined_version_macro_is_refused(self) -> None:
        # adversary r4 A3: the guard trusts Qt's own QT_VERSION / QT_VERSION_CHECK, so r5 refuses a
        # #define or #undef of either in the scanned text.
        ge_form = (
            "#if QT_VERSION >= QT_VERSION_CHECK( 6, 9, 0 )\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#else\n"
            "    return image.mirrored( true, false );\n"
            "#endif\n"
        )
        for prefix, lines in (
            ("#undef QT_VERSION_CHECK\n#define QT_VERSION_CHECK(a,b,c) 0\n", [1, 2]),
            ("#define QT_VERSION_CHECK(a, b, c) 0\n", [1]),
            ("#undef QT_VERSION\n", [1]),
            ("#define QT_VERSION 0x070000\n", [1]),
            ("  #  define QT_VERSION 0x070000\n", [1]),
        ):
            with self.subTest(prefix=prefix):
                self.assertRefused(prefix + ge_form, ["version-macro-redefined"], lines)
        self.assertRefused("%:  define QT_VERSION 0x070000\n" + ge_form, ["digraph"], [1])
        # A name that only starts with the macro name is not a redefinition.
        self.assertEqual(unguarded_newer_api_calls("#define QT_VERSION_CHECKED 1\n" + ge_form), [])

    def test_a_version_past_the_preprocessor_range_is_never_trusted(self) -> None:
        # adversary r4 A4: (2**48 + 5ULL) << 16 wraps to 0x050900 in 64-bit preprocessing.
        for major in ("281474976710661ULL", "32768", "0x8000"):
            for op in (">=", "<"):
                with self.subTest(major=major, op=op):
                    body, other = "return image.flipped();\n", "return image.mirrored( true, false );\n"
                    arms = body + "#else\n" + other if op == ">=" else other + "#else\n" + body
                    fixture = f"#if QT_VERSION {op} QT_VERSION_CHECK({major},9,0)\n{arms}#endif\n"
                    self.assertEqual([a for _, a, _ in unguarded_newer_api_calls(fixture)], ["flipped"])

    def test_a_finding_names_the_physical_line_of_the_name(self) -> None:
        # adversary r4 A5: a multi-line comment before the call on the same logical line.
        for source in (
            "/* x\n */ return image.flipped();\n",
            "x(); /* a\n\nb */ return image.flipped();\n",
            "auto a = 1; /* one\n two */ auto p = e->\nposition();\n",
        ):
            with self.subTest(source=source):
                expected = source.rstrip("\n").count("\n") + 1
                self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(source)], [expected])
        alias = "#define A /* x\n */ flipped\n"
        self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(alias)], [(2, "flipped")])

    def _header_with_branch(self, branch: str) -> str:
        # The real header, its mirroredImage() version branch replaced by `branch` (in memory).
        text = SCOPED_HEADERS[0].read_text(encoding="utf-8")
        start = text.index("#if QT_VERSION >= QT_VERSION_CHECK( 6, 9, 0 )")
        end = text.index("#endif", start) + len("#endif")
        return text[:start] + branch.rstrip("\n") + text[end:]

    def test_a_splice_inside_a_comment_delimiter_is_refused(self) -> None:
        # adversary r4 pass 6 A1 (3c82e35e): a splice inside `/*` or `*/` used to hide the call.
        for branch in ('/\\\n* " */ return image.flipped(Qt::Horizontal);\n',
                       "/* x *\\\n/ return image.flipped(Qt::Horizontal);\n"):
            with self.subTest(branch=branch):
                hits = unguarded_newer_api_calls(self._header_with_branch(branch))
                self.assertTrue(hits)
                self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)

    def test_an_object_like_alias_of_a_function_like_macro_is_refused(self) -> None:
        # adversary r4 pass 6 A2 (3c82e35e): INDIRECT expands to APPLY, whose arguments escaped.
        branch = ("#define APPLY(i, member) (i).member(Qt::Horizontal)\n#define INDIRECT APPLY\n"
                  "return INDIRECT(image, flipped);\n")
        for source in (branch, self._header_with_branch(branch)):
            with self.subTest(source=source[:40]):
                hits = unguarded_newer_api_calls(source)
                self.assertTrue(hits)
                self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)

    def test_a_character_outside_printable_ascii_in_code_is_refused(self) -> None:
        # adversary r5 A1 (4d52622e): a BOM before `#define QT_VERSION_CHECK` hid the anchored
        # directive, and the later arm was trusted. Every directive pattern is anchored at the line
        # start, so r5 refuses any character in code outside printable ASCII, tab, VT and FF.
        bom = "﻿#define QT_VERSION_CHECK(a,b,c) 0\n#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n" \
              "return image.flipped(Qt::Horizontal);\n#endif\n"
        self.assertRefused(bom, ["non-ascii"], [1])
        hits = unguarded_newer_api_calls("﻿#define QT_VERSION_CHECK(a,b,c) 0\n"
                                         + SCOPED_HEADERS[0].read_text(encoding="utf-8"))
        self.assertTrue(hits)
        self.assertTrue(all(api.startswith("REFUSED") for _, api, _ in hits), hits)
        for prefix in (" ", " ", "　", "\x1c", "\x00", "​"):
            with self.subTest(prefix=repr(prefix)):
                self.assertRefused(f"x();\n{prefix}#undef Y\n", ["non-ascii"], [2])
        # In a comment or a literal it is text.
        self.assertEqual(unguarded_newer_api_calls('// ﻿ café\nauto s = "é";\n'), [])

    def test_a_directive_the_scanner_does_not_model_is_refused(self) -> None:
        # adversary r5 A2 (4d52622e): `#elifdef` (C++23) was not recognised, so its arm kept the
        # trusted floor of the `#if` above it. r5 reads `#elifdef`/`#elifndef` as an untrusted
        # `#elif`, and refuses any directive name it does not model.
        for api in NEWER_THAN_QT5_API:
            for keyword in ("elifdef", "elifndef"):
                with self.subTest(api=api, keyword=keyword):
                    source = (f"#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\nreturn image.mirrored(true,false);\n"
                              f"#{keyword} __cplusplus\nreturn image.{api}(Qt::Horizontal);\n#endif\n")
                    self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(source)], [(4, api)])
        for directive in ("#include_next <QImage>", "#import <x>", "#ident \"x\"", "#assert machine(x)",
                          "#embed \"x\"", "# 12 \"file.h\"", "#sccs \"x\"", "  # /* c */ unassert x",
                          "import \"qt_override.h\";", "export import <QImage>;", "module;", "export module m;"):
            with self.subTest(directive=directive):
                self.assertRefused(f"x();\n{directive}\n", ["unknown-directive"], [2])
        # The directives the scanner models, and the null directive, are read.
        self.assertEqual(unguarded_newer_api_calls(
            "#pragma once\n#include <QImage>\n#\n#undef X\n#line 10\n#error x\n#warning x\n"), [])

    def test_every_api_name_token_is_a_use(self) -> None:
        # r5: the token rule is fail closed: a bare call, a macro argument (of any macro, defined
        # here or not) or a conditional directive that names an API is a use too.
        for source in ("auto b = flipped();\n", "Q_UNUSED( flip );\n", "EXTERNAL(image, flipped);\n",
                       "#if defined(position)\n#endif\n", "#error flipped is Qt 6.9\n"):
            with self.subTest(source=source):
                self.assertEqual(len(unguarded_newer_api_calls(source)), 1)

    def test_names_that_only_contain_an_api_name_do_not_count(self) -> None:
        fixture = "auto a = img.flippedCopy(); auto b = unflipped(); auto c = s.positions(); auto d = flip_x;\n"
        self.assertEqual(unguarded_newer_api_calls(fixture), [])


if __name__ == "__main__":
    unittest.main()
