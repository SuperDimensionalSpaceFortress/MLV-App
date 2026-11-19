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

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCOPED_HEADERS = (ROOT / "platform" / "qt" / "PlayerTransportToolbar.h",)

# API -> the first Qt (major, minor) that has it. A member access (`.x` / `->x`, optionally
# qualified: `img.QImage::flipped`) or a `QImage::x` name (a member pointer) both count.
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
# Only the plain condition counts: a negated or compound one is "other" (fail closed).
VERSION_CHECK = re.compile(
    r"QT_VERSION\s*(>=|<)\s*QT_VERSION_CHECK\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*\d+\s*\)"
)
RAW_STRING_OPEN = re.compile(r'([^()\\\s"]{0,16})\(')


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """(physical start line, logical line): backslash splices joined; comments and the contents of
    string, char and raw-string literals blanked, so a name inside them never counts."""
    out: list[tuple[int, str]] = []
    current: list[str] = []
    start = line = 1
    state = "code"  # code | line_comment | block_comment | string | char
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] == "\n":
            line += 1
            i += 2
            continue
        if ch == "\n":
            if state == "line_comment":
                state = "code"
            line += 1
            if state == "block_comment":
                i += 1
                continue
            out.append((start, "".join(current)))
            current, start = [], line
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
                current.append(" ")
                i += 2
                continue
            token = re.search(r"[\w'.]*$", "".join(current)).group(0)
            if ch == "'" and token[:1].isdigit():
                current.append(ch)  # a digit separator (1'000), not a char literal
                i += 1
                continue
            if ch == '"' and re.search(r"\w*$", token).group(0) in ("R", "LR", "uR", "UR", "u8R"):
                opened = RAW_STRING_OPEN.match(text, i + 1)
                close = text.find(")" + opened.group(1) + '"', opened.end()) if opened else -1
                if close >= 0:
                    end = close + len(opened.group(1)) + 2
                    line += text.count("\n", i, end)
                    current.append('""')
                    i = end
                    continue
            if ch in "\"'":
                state = "string" if ch == '"' else "char"
            current.append(ch)
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
                current.append(ch)
        i += 1
    out.append((start, "".join(current)))
    return out


def unguarded_newer_api_calls(text: str) -> list[tuple[int, str, str]]:
    """(line, api name, line text) for each call outside an arm that compiles only on a Qt new
    enough for that API."""
    # Each frame: [kind, version, arm_floor]. kind "ge" (#if QT_VERSION >= V), "lt" (#if QT_VERSION
    # < V) or "other"; arm_floor is the lowest Qt the current arm can compile on, or None.
    stack: list[list] = []
    found: list[tuple[int, str, str]] = []
    for number, line in _logical_lines(text):
        directive = DIRECTIVE.match(line)
        if directive:
            keyword, rest = directive.group(1), directive.group(2)
            if keyword in ("if", "ifdef", "ifndef"):
                check = VERSION_CHECK.fullmatch(rest.strip()) if keyword == "if" else None
                if check:
                    version = (int(check.group(2)), int(check.group(3)))
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
            continue
        floors = [frame[2] for frame in stack if frame[2] is not None]
        define = DEFINE.match(line)
        for match in (BARE_NAME.finditer(define.group(1)) if define else API_CALL.finditer(line)):
            needed = NEWER_THAN_QT5_API[match.group(1)]
            if not any(floor >= needed for floor in floors):
                found.append((number, match.group(1), line.strip()))
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

    def test_names_that_only_contain_an_api_name_do_not_count(self) -> None:
        fixture = "auto a = img.flippedCopy(); auto b = flipped(); auto c = s.positions();\n"
        self.assertEqual(unguarded_newer_api_calls(fixture), [])


if __name__ == "__main__":
    unittest.main()
