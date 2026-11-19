"""Guard the viewer double-click headers against Qt 6-only QMouseEvent API with no Qt 5 branch.

PLAYER-DOUBLECLICK-FULLSCREEN-1 r3 (PR 856, opus key3 r2 H1; CoS record 010675f): Qt 5 is a
documented, supported kit (docs/11-build-macos-linux.md; .github/workflows/macOS-Intel.yml builds
the macOS Intel release with qt@5), but no PR check compiles it, because that workflow runs only on
workflow_dispatch. ``QMouseEvent::position()`` / ``globalPosition()`` / ``scenePosition()`` exist
only in Qt 6.0+, so a call outside a ``QT_VERSION_CHECK(6, ...)`` branch breaks the Qt 5 build of
MainWindow.cpp unseen. This test reads the scoped headers' text and fails on any such call that is
not inside the Qt 6 arm of a version check (precedent: GpuWindowViewerInputForwarder.h).
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCOPED_HEADERS = (
    ROOT / "platform" / "qt" / "ViewerDoubleClickFilter.h",
    ROOT / "platform" / "qt" / "ViewerDoubleClickPolicy.h",
)

# A member access of a Qt 6.0+ only QMouseEvent/QSinglePointEvent accessor: `->position`,
# `.position`, and the global/scene variants, with or without grouping parentheses before the call
# (r3 adversary A3: `(e->position)()`). `QMouseEvent::position()` does not match.
# A qualified member name (`e->QMouseEvent::position()`, pass 2 A2) names the same accessor.
QT6_ONLY_CALL = re.compile(
    r"(?:->|\.)\s*(?:::\s*)?(?:\w+\s*::\s*)*(?:position|globalPosition|scenePosition)\b"
)
# `%:` is the digraph spelling of `#` (pass 2 A3).
DIRECTIVE = re.compile(r"^\s*(?:#|%:)\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
RAW_STRING_PREFIXES = ("R", "LR", "uR", "UR", "u8R")
RAW_STRING_OPEN = re.compile(r'([^()\\\s"]{0,16})\(')
# Only the plain condition counts as a Qt 6 branch: a negated or compound one (r3 adversary A1:
# `!(...)`, `... || 1`, `... && 0`) is "other", so a call under it reads unguarded (fail closed).
QT6_CHECK = re.compile(
    r"QT_VERSION\s*(>=|<)\s*QT_VERSION_CHECK\s*\(\s*6\s*,\s*\d+\s*,\s*\d+\s*\)"
)


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Return (physical start line, logical line) with backslash-newline splices joined, and
    comments and string/char literal contents blanked, so line numbers stay true (A2, A3)."""
    out: list[tuple[int, str]] = []
    current: list[str] = []
    start = line = 1
    state = "code"  # code | line_comment | block_comment | string | char
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] == "\n":  # splice: same logical line
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
                current.append(ch)  # a digit separator (1'000), not a char literal (pass 2 A1)
                i += 1
                continue
            if ch == '"' and re.search(r"\w*$", token).group(0) in RAW_STRING_PREFIXES:
                opened = RAW_STRING_OPEN.match(text, i + 1)
                close = text.find(")" + opened.group(1) + '"', opened.end()) if opened else -1
                if close >= 0:  # a raw string ends only at )delim" (pass 2 A1)
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


def unguarded_qt6_calls(text: str) -> list[tuple[int, str]]:
    """Return (line number, line) for each Qt 6-only call outside a Qt 6 version arm."""
    # Each frame: [kind, arm]. kind "qt6ge" (#if QT_VERSION >= ...(6), "qt6lt" (#if QT_VERSION < ...(6)
    # or "other"; arm is "qt6" when the current arm compiles only on Qt 6, else "no".
    stack: list[list[str]] = []
    found: list[tuple[int, str]] = []
    for number, line in _logical_lines(text):
        directive = DIRECTIVE.match(line)
        if directive:
            keyword, rest = directive.group(1), directive.group(2)
            if keyword in ("if", "ifdef", "ifndef"):
                check = QT6_CHECK.fullmatch(rest.strip()) if keyword == "if" else None
                if check and check.group(1) == ">=":
                    stack.append(["qt6ge", "qt6"])
                elif check:
                    stack.append(["qt6lt", "no"])
                else:
                    stack.append(["other", "no"])
            elif keyword == "elif" and stack:
                stack[-1] = ["other", "no"]  # conservative: an #elif arm is never the Qt 6 arm
            elif keyword == "else" and stack:
                kind = stack[-1][0]
                stack[-1][1] = "qt6" if kind == "qt6lt" else "no"
            elif keyword == "endif" and stack:
                stack.pop()
            continue
        if QT6_ONLY_CALL.search(line) and not any(arm == "qt6" for _, arm in stack):
            found.append((number, line.strip()))
    return found


class Qt6OnlyApiGuardTests(unittest.TestCase):
    def test_scoped_headers_exist(self) -> None:
        # A rename must not turn the guard below into a silent pass.
        for header in SCOPED_HEADERS:
            with self.subTest(header=header.name):
                self.assertTrue(header.is_file(), f"scoped header missing: {header}")

    def test_scoped_headers_have_no_unguarded_qt6_only_call(self) -> None:
        for header in SCOPED_HEADERS:
            with self.subTest(header=header.name):
                hits = unguarded_qt6_calls(header.read_text(encoding="utf-8"))
                self.assertFalse(
                    hits,
                    f"{header.name}: Qt 6-only QMouseEvent call outside a "
                    "`#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)` branch breaks the Qt 5 kit "
                    "(macOS-Intel.yml qt@5); add a #else using localPos()/windowPos()/screenPos(): "
                    + "; ".join(f"line {n}: {src}" for n, src in hits),
                )

    def test_scanner_flags_the_a32edb4a_shape(self) -> None:
        # The r2 head's own line: proves the guard bites on the defect it exists for.
        fixture = (
            "inline bool f( const QMouseEvent *mouse ) {\n"
            "    return gate.decide( button, modifiers, state( watched, mouse->position() ) );\n"
            "}\n"
        )
        self.assertEqual([n for n, _ in unguarded_qt6_calls(fixture)], [2])

    def test_scanner_flags_each_qt6_only_accessor_and_member_form(self) -> None:
        for call in ("e->position()", "e.position()", "e->globalPosition()", "e.scenePosition()"):
            with self.subTest(call=call):
                self.assertEqual(len(unguarded_qt6_calls(f"auto p = {call};\n")), 1)

    def test_scanner_accepts_the_guarded_shapes(self) -> None:
        ge_form = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)\n"
            "    return mouse->position();\n"
            "#else\n"
            "    return mouse->localPos();\n"
            "#endif\n"
        )
        lt_form = (
            "#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)\n"
            "    return mouse->localPos();\n"
            "#else\n"
            "    return mouse->position();\n"
            "#endif\n"
        )
        self.assertEqual(unguarded_qt6_calls(ge_form), [])
        self.assertEqual(unguarded_qt6_calls(lt_form), [])

    def test_scanner_flags_a_call_in_the_qt5_arm_or_after_the_branch(self) -> None:
        wrong_arm = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)\n"
            "    return mouse->localPos();\n"
            "#else\n"
            "    return mouse->position();\n"
            "#endif\n"
            "auto q = mouse->globalPosition();\n"
        )
        self.assertEqual([n for n, _ in unguarded_qt6_calls(wrong_arm)], [4, 6])

    def test_scanner_ignores_comments(self) -> None:
        fixture = (
            "// mouse->position() is Qt 6 only\n"
            "/* e.position() and\n"
            "   e->globalPosition() */\n"
            "auto p = mouse->localPos();\n"
        )
        self.assertEqual(unguarded_qt6_calls(fixture), [])

    def test_scanner_only_trusts_the_plain_version_condition(self) -> None:
        # r3 adversary A1: a negated or compound condition is not the Qt 6 arm (fail closed).
        for condition in (
            "!(QT_VERSION >= QT_VERSION_CHECK(6, 0, 0))",
            "QT_VERSION >= QT_VERSION_CHECK(6, 0, 0) || 1",
            "QT_VERSION < QT_VERSION_CHECK(6, 0, 0) && 0",
        ):
            with self.subTest(condition=condition):
                fixture = f"#if {condition}\nreturn e->position();\n#else\nreturn e->position();\n#endif\n"
                self.assertEqual([n for n, _ in unguarded_qt6_calls(fixture)], [2, 4])

    def test_scanner_sees_calls_next_to_comment_markers_in_string_literals(self) -> None:
        # r3 adversary A2: "//" or "/*" inside a string literal is not a comment.
        self.assertEqual(len(unguarded_qt6_calls('const char *s = "//"; auto p = e->position();\n')), 1)
        fixture = 'const char *a = "/*"; auto p = e->position(); const char *b = "*/";\n'
        self.assertEqual(len(unguarded_qt6_calls(fixture)), 1)
        self.assertEqual(len(unguarded_qt6_calls("char c = '\"'; auto p = e->position(); // \"\n")), 1)

    def test_scanner_sees_grouped_and_spliced_member_names(self) -> None:
        # r3 adversary A3: other legal spellings of the same call.
        for call in ("(e->position)()", "(e.globalPosition)()", "( e -> scenePosition ) ( )", "e->posi\\\ntion()"):
            with self.subTest(call=call):
                self.assertEqual(len(unguarded_qt6_calls(f"auto p = {call};\n")), 1)

    def test_scanner_sees_calls_after_digit_separators_and_raw_strings(self) -> None:
        # r3 adversary pass 2 A1: a digit separator is not a char literal; a raw string ends at )delim".
        for prefix in ("const int n = 1'000;", 'const char *s = R"("/*)";', 'auto s = u8R"x(")x";'):
            with self.subTest(prefix=prefix):
                self.assertEqual(len(unguarded_qt6_calls(f"{prefix} auto p = e->position();\n")), 1)
        self.assertEqual(unguarded_qt6_calls('auto s = R"(e->position())";\n'), [])

    def test_scanner_sees_qualified_member_calls(self) -> None:
        # r3 adversary pass 2 A2: `e->QMouseEvent::position()` names the same accessor.
        for call in ("e->QMouseEvent::position()", "e.QSinglePointEvent::globalPosition()", "e -> ::QMouseEvent :: scenePosition()"):
            with self.subTest(call=call):
                self.assertEqual(len(unguarded_qt6_calls(f"auto p = {call};\n")), 1)

    def test_scanner_reads_digraph_directives(self) -> None:
        # r3 adversary pass 2 A3: `%:` is the digraph spelling of `#`.
        mixed = (
            "#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)\n"
            "return e->localPos();\n"
            "%:else\n"
            "return e->position();\n"
            "%:endif\n"
        )
        self.assertEqual([n for n, _ in unguarded_qt6_calls(mixed)], [4])
        guarded = (
            "%: if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)\n"
            "return e->position();\n"
            "%:else\n"
            "return e->localPos();\n"
            "%:endif\n"
            "auto q = e->position();\n"
        )
        self.assertEqual([n for n, _ in unguarded_qt6_calls(guarded)], [6])

    def test_spliced_lines_keep_their_physical_line_numbers(self) -> None:
        fixture = "#define X a \\\n  b\nint y;\nauto p = e->position();\n"
        self.assertEqual([n for n, _ in unguarded_qt6_calls(fixture)], [4])


if __name__ == "__main__":
    unittest.main()
