"""Guard PlayerTransportToolbar.h against Qt-version-specific API with no version branch.

PLAYER-TOOLBAR-TRANSPORT-BUTTONS-1 r2 (PR 857; CoS advisory record c5033af): Qt 5 is a documented,
supported kit (docs/11-build-macos-linux.md; .github/workflows/macOS-Intel.yml builds the macOS
Intel release with qt@5), but no PR check compiles it, because that workflow runs only on
workflow_dispatch. r1's mirroredIcon() called ``QImage::flipped()``, which exists only in Qt 6.9+,
and MainWindow.cpp includes the header, so the Qt 5 release kit failed to compile unseen. This test
reads the header's text and fails on any call to an API newer than Qt 5.15 that is not inside an
arm of a ``QT_VERSION`` check that compiles only on a Qt new enough for it. Same defect class as
the QMouseEvent::position() guard of PR 856 r3 (test_qt6_only_api_guard.py on that branch).
"""
from __future__ import annotations

import bisect
import re
import unittest
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCOPED_HEADERS = (ROOT / "platform" / "qt" / "PlayerTransportToolbar.h",)

# API -> the first Qt (major, minor) that has it. A member access (`.x` / `->x`, optionally
# qualified: `img.QImage::flipped`) or a `QImage::x` name (a member pointer) both count, with any
# whitespace, including a line break, CR or CRLF, between the accessor and the name or between
# `QImage` and `::` (r4).
NEWER_THAN_QT5_API = {
    "flipped": (6, 9),  # QImage::flipped(Qt::Orientations)
    "flip": (6, 9),  # QImage::flip(Qt::Orientations)
    "position": (6, 0),  # QSinglePointEvent / QMouseEvent accessors
    "globalPosition": (6, 0),
    "scenePosition": (6, 0),
}
_NAMES = "|".join(sorted(NEWER_THAN_QT5_API, key=len, reverse=True))
API_CALL = re.compile(
    rf"(?:(?:->|\.)\s*(?:::\s*)?(?:\w+\s*::\s*)*|\bQImage\s*::\s*)({_NAMES})\b"
)
# `%:` is the digraph and `??=` the (pre-C++17) trigraph spelling of `#` (adversary r2 A2).
_HASH = r"(?:#|%:|\?\?=)"
DIRECTIVE = re.compile(rf"^\s*{_HASH}\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
# A macro whose body names an API as a bare word (`#define FLIP flipped`, then `image.FLIP()`)
# hides the call from API_CALL, so the name in the #define itself counts (adversary r2 A2).
DEFINE = re.compile(rf"^\s*{_HASH}\s*define\b(.*)$")
BARE_NAME = re.compile(rf"\b({_NAMES})\b")
# The guard trusts Qt's own QT_VERSION / QT_VERSION_CHECK, so a #define or #undef of either makes
# every later version arm "other" (adversary r4 A3).
TOKEN_PASTE = re.compile(r"##|%:%:")
VERSION_MACRO_REDEFINED = re.compile(rf"^\s*{_HASH}\s*(?:define|undef)\s+QT_VERSION(?:_CHECK)?\b")
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
RAW_STRING_OPEN = re.compile(r'([^()\\\s"]{0,16})\(')


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


# Translation phase 1 where trigraphs are on (C++14 and older with -trigraphs or a strict -std).
_TRIGRAPH = re.compile(r"\?\?[=/'()!<>\-]")
_TRIGRAPH_CHAR = {"=": "#", "/": "\\", "'": "^", "(": "[", ")": "]", "!": "|", "<": "{", ">": "}", "-": "~"}


def _translate_trigraphs(text: str) -> str:
    return _TRIGRAPH.sub(lambda m: _TRIGRAPH_CHAR[m.group(0)[2]], text)


def _logical_lines(text: str) -> list[tuple[int, str, list[int]]]:
    """(physical start line, logical line, physical line of each character): backslash splices
    joined; comments and the contents of string, char and raw-string literals blanked, so a name
    inside them never counts. CRLF and a lone CR end a line as LF does, and an unterminated ' or "
    ends at the line end."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[tuple[int, str, list[int]]] = []
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
        if ch == "\\" and i + 1 < n and text[i + 1] == "\n":
            line += 1
            i += 2
            continue
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
            token = re.search(r"[\w'.]*$", "".join(current)).group(0)
            if ch == "'" and token[:1].isdigit():
                emit(ch)  # a digit separator (1'000), not a char literal
                i += 1
                continue
            if ch == '"' and re.search(r"\w*$", token).group(0) in ("R", "LR", "uR", "UR", "u8R"):
                opened = RAW_STRING_OPEN.match(text, i + 1)
                close = text.find(")" + opened.group(1) + '"', opened.end()) if opened else -1
                if close >= 0:
                    end = close + len(opened.group(1)) + 2
                    line += text.count("\n", i, end)
                    emit('""')
                    i = end
                    continue
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
    return out


def unguarded_newer_api_calls(text: str) -> list[tuple[int, str, str]]:
    """(line, api name, line text) for each call outside an arm that compiles only on a Qt new
    enough for that API. The text is read twice, as written and with trigraphs translated (a
    `??/` splice exists only where trigraphs are on, adversary r4 A1), and a finding in either
    reading counts; a call both readings see is one finding."""
    plain = _scan(text)
    found = list(plain)
    left = Counter((n, api) for n, api, _ in plain)
    for hit in _scan(_translate_trigraphs(text)):
        if left[hit[:2]]:
            left[hit[:2]] -= 1
        else:
            found.append(hit)
    found.sort(key=lambda hit: hit[0])
    return found


def _scan(text: str) -> list[tuple[int, str, str]]:
    # Each frame: [kind, version, arm_floor]. kind "ge" (#if QT_VERSION >= V), "lt" (#if QT_VERSION
    # < V) or "other"; V and arm_floor are QT_VERSION_CHECK values, arm_floor the lowest Qt the
    # current arm can compile on, or None.
    stack: list[list] = []
    found: list[tuple[int, str, str]] = []
    redefined = False  # a #define / #undef of QT_VERSION or QT_VERSION_CHECK was seen (A3)

    def trusted(api: str, floors: tuple[int, ...]) -> bool:
        needed = _qt_version_value(*NEWER_THAN_QT5_API[api], 0)
        return any(floor >= needed for floor in floors)

    def physical(where: list[int], k: int, number: int) -> int:
        return where[k] if 0 <= k < len(where) else number

    # r4 (r3 keys sol B1, opus, fable key3): the member access may span lines (`image.` then
    # `flipped` on the next line, or `QImage` then `::flipped`), so API_CALL runs once over the
    # code lines joined by "\n", directive and #define lines left empty; each match is mapped
    # back to the physical line its NAME is on and judged by the arms open there.
    starts: list[int] = []  # offset of each logical line in the joined text
    lines: list[tuple[int, str, list[int], tuple[int, ...]]] = []  # (start line, text, where, floors)
    parts: list[str] = []
    offset = 0
    for number, line, where in _logical_lines(text):
        code = ""
        if VERSION_MACRO_REDEFINED.match(line):
            redefined = True
        directive = DIRECTIVE.match(line)
        if directive:
            keyword, rest = directive.group(1), directive.group(2)
            if keyword in ("if", "ifdef", "ifndef"):
                check = VERSION_CHECK.fullmatch(rest.strip()) if keyword == "if" else None
                args = [_pp_int(check.group(k)) for k in (2, 3, 4)] if check else [None]
                version = None if redefined or None in args else _qt_version_value(*args)
                if check and version is not None:
                    if check.group(1) == ">=":
                        stack.append(["ge", version, version])
                    else:
                        stack.append(["lt", version, None])
                else:
                    stack.append(["other", None, None])
            elif keyword == "elif" and stack:
                stack[-1] = ["other", None, None]  # an #elif arm is never trusted
            elif keyword == "else" and stack:
                kind, version, _ = stack[-1]
                stack[-1][2] = version if kind == "lt" else None
            elif keyword == "endif" and stack:
                stack.pop()
            floors: tuple[int, ...] = ()
        else:
            floors = tuple(frame[2] for frame in stack if frame[2] is not None)
            define = DEFINE.match(line)
            if define:
                # A token paste can spell any name (`a ## b`, then JOIN(flip,ped)), and the scan
                # does not expand macros, so a paste is itself a finding (adversary r4 A2).
                for match in TOKEN_PASTE.finditer(line, define.start(1)):
                    found.append((physical(where, match.start(), number), "##", line.strip()))
                for match in BARE_NAME.finditer(line, define.start(1)):
                    if not trusted(match.group(1), floors):
                        found.append((physical(where, match.start(1), number), match.group(1), line.strip()))
            else:
                code = line
        starts.append(offset)
        lines.append((number, line, where, floors))
        parts.append(code)
        offset += len(code) + 1
    for match in API_CALL.finditer("\n".join(parts)):
        index = bisect.bisect_right(starts, match.start(1)) - 1
        number, line, where, floors = lines[index]
        if not trusted(match.group(1), floors):
            found.append((physical(where, match.start(1) - starts[index], number), match.group(1), line.strip()))
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
                    "(macOS-Intel.yml qt@5); add a #else branch (QImage::flipped -> mirrored()): "
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
                     "e.scenePosition()", "img.flip\\\nped()"):
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
            'auto r = R"(img.flipped())";\n'
            "auto p = image.mirrored( true, false );\n"
        )
        self.assertEqual(unguarded_newer_api_calls(fixture), [])
        self.assertEqual(len(unguarded_newer_api_calls('const char *s = "//"; auto p = i.flipped();\n')), 1)
        self.assertEqual(len(unguarded_newer_api_calls("const int n = 1'000; auto p = i.flipped();\n")), 1)

    def test_scanner_reads_digraph_directives(self) -> None:
        fixture = (
            "%: if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n"
            "return i.flipped();\n"
            "%:else\n"
            "return i.flipped();\n"
            "%:endif\n"
        )
        self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(fixture)], [4])

    def test_scanner_sees_macro_aliases_and_trigraph_directives(self) -> None:
        # adversary r2 A2 (da60b814): both inputs used to return no finding.
        alias = "#define FLIP flipped\nreturn image.FLIP(Qt::Horizontal);\n"
        self.assertEqual([(n, api) for n, api, _ in unguarded_newer_api_calls(alias)], [(1, "flipped")])
        trigraph = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n"
            "??=endif\n"
            "return image.flipped(Qt::Horizontal);\n"
        )
        self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(trigraph)], [3])
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

    def test_a_trigraph_splice_is_read_both_ways(self) -> None:
        # adversary r4 A1 (8a66187d): `??/` is a backslash only where trigraphs are on, so a finding
        # in either reading counts. Here the trigraph reading closes the arm before the call...
        closes_arm = "#if QT_VERSION >= QT_VERSION_CHECK(6,9,0)\n#en??/\ndif\nreturn image.flipped();\n"
        self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(closes_arm)], [(4, "flipped")])
        # ...and here the plain reading keeps the call out of the comment.
        ends_comment = "// note ??/\nreturn image.flipped();\n"
        self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(ends_comment)], [(2, "flipped")])
        # One call is one finding, even when both readings see it.
        self.assertEqual(len(unguarded_newer_api_calls("auto p = i.flipped(); auto q = i.flipped();\n")), 2)

    def test_a_token_paste_in_a_define_is_a_finding(self) -> None:
        # adversary r4 A2: the scan does not expand macros, so it refuses a paste instead of
        # following it, wherever the #define sits.
        for source in (
            "#define JOIN(a,b) a ## b\nreturn image.JOIN(flip,ped)(Qt::Horizontal);\n",
            "#define JOIN(a,b) a%:%:b\n",
            "#define JOIN(a,b) a ??=??= b\n",
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 9, 0)\n#define JOIN(a,b) a##b\n#endif\n",
        ):
            with self.subTest(source=source):
                self.assertEqual([a for _, a, _ in unguarded_newer_api_calls(source)], ["##"])
        # `#` stringizing and a `##` in a comment or string are not pastes.
        self.assertEqual(unguarded_newer_api_calls('#define S(x) #x /* a ## b */ "##"\n'), [])

    def test_a_redefined_version_macro_is_never_trusted(self) -> None:
        # adversary r4 A3: the guard trusts Qt's own QT_VERSION / QT_VERSION_CHECK, so a #define or
        # #undef of either in the scanned text makes every later arm untrusted.
        ge_form = (
            "#if QT_VERSION >= QT_VERSION_CHECK( 6, 9, 0 )\n"
            "    return image.flipped( Qt::Horizontal );\n"
            "#else\n"
            "    return image.mirrored( true, false );\n"
            "#endif\n"
        )
        for prefix in (
            "#undef QT_VERSION_CHECK\n#define QT_VERSION_CHECK(a,b,c) 0\n",
            "#define QT_VERSION_CHECK(a, b, c) 0\n",
            "#undef QT_VERSION\n",
            "#define QT_VERSION 0x070000\n",
            "%:  define QT_VERSION 0x070000\n",
        ):
            with self.subTest(prefix=prefix):
                fixture = prefix + ge_form
                line = prefix.count("\n") + 2
                self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(fixture)], [(line, "flipped")])
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
        # adversary r4 A5: a multi-line comment or raw string before the call on the same logical line.
        for source in (
            "/* x\n */ return image.flipped();\n",
            'auto r = R"(a\nb)"; return image.flipped();\n',
            "auto a = 1; /* one\n two */ auto p = e->\nposition();\n",
        ):
            with self.subTest(source=source):
                expected = source.rstrip("\n").count("\n") + 1
                self.assertEqual([n for n, _, _ in unguarded_newer_api_calls(source)], [expected])
        alias = "#define A /* x\n */ flipped\n"
        self.assertEqual([(n, a) for n, a, _ in unguarded_newer_api_calls(alias)], [(2, "flipped")])

    def test_names_that_only_contain_an_api_name_do_not_count(self) -> None:
        fixture = "auto a = img.flippedCopy(); auto b = flipped(); auto c = s.positions();\n"
        self.assertEqual(unguarded_newer_api_calls(fixture), [])


if __name__ == "__main__":
    unittest.main()
