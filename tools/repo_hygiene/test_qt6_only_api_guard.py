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

# A member call of a Qt 6.0+ only QMouseEvent/QSinglePointEvent accessor: `->position()`,
# `.position()`, and the global/scene variants. `QMouseEvent::position()` in prose does not match.
QT6_ONLY_CALL = re.compile(r"(?:->|\.)\s*(?:position|globalPosition|scenePosition)\s*\(\s*\)")
DIRECTIVE = re.compile(r"^\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)$")
QT6_CHECK = re.compile(r"QT_VERSION\s*(>=|<)\s*QT_VERSION_CHECK\s*\(\s*6\s*,")


def _strip_comments(text: str) -> str:
    """Drop /* */ and // comments, keeping line breaks so line numbers stay true."""
    text = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), text, flags=re.S)
    return "\n".join(line.split("//", 1)[0] for line in text.split("\n"))


def unguarded_qt6_calls(text: str) -> list[tuple[int, str]]:
    """Return (line number, line) for each Qt 6-only call outside a Qt 6 version arm."""
    # Each frame: [kind, arm]. kind "qt6ge" (#if QT_VERSION >= ...(6), "qt6lt" (#if QT_VERSION < ...(6)
    # or "other"; arm is "qt6" when the current arm compiles only on Qt 6, else "no".
    stack: list[list[str]] = []
    found: list[tuple[int, str]] = []
    for number, line in enumerate(_strip_comments(text).split("\n"), start=1):
        directive = DIRECTIVE.match(line)
        if directive:
            keyword, rest = directive.group(1), directive.group(2)
            if keyword in ("if", "ifdef", "ifndef"):
                check = QT6_CHECK.search(rest) if keyword == "if" else None
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


if __name__ == "__main__":
    unittest.main()
