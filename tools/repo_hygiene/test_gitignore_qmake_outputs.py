"""In-tree qmake/mingw32-make output under platform/qt must be git-ignored.

A lane worktree that was built in place carries .qmake.stash, Makefile*, moc/ obj/ rcc/ ui/ and
debug/ release/ under platform/qt. While those were untracked-but-not-ignored, the worktree-debris
reclaim stage and the retire gate read them as possible user work and refused to remove the
worktree (GITIGNORE-QMAKE-INTREE-AND-RETIRE-HOLDER-1). These tests pin both halves: every
build-output path is ignored, and no tracked file is swallowed by .gitignore.
"""
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# One representative path per ignore pattern. Paths need not exist: check-ignore matches by name.
QMAKE_OUTPUT_PATHS = (
    "platform/qt/.qmake.stash",
    "platform/qt/Makefile",
    "platform/qt/Makefile.Debug",
    "platform/qt/Makefile.Release",
    "platform/qt/MLVApp_resource.rc",
    "platform/qt/object_script.MLVApp.Debug",
    "platform/qt/object_script.MLVApp.Release",
    "platform/qt/debug/MLVApp.exe",
    "platform/qt/release/MLVApp.exe",
    "platform/qt/moc/moc_MainWindow.cpp",
    "platform/qt/obj/main.o",
    "platform/qt/rcc/qrc_MainWindow.cpp",
    "platform/qt/ui/ui_MainWindow.h",
    "platform/qt/.obj/main.o",
    "platform/qt/.moc/moc_MainWindow.cpp",
    "platform/qt/.rcc/qrc_MainWindow.cpp",
    "platform/qt/.ui/ui_MainWindow.h",
)


def _git(*args, stdin=None):
    return subprocess.run(
        ["git", "-C", str(ROOT), *args], input=stdin, text=True, capture_output=True
    )


class GitignoreQmakeOutputsTests(unittest.TestCase):
    def test_every_qmake_output_path_is_ignored(self):
        # --no-index: judge the pattern, not whether the path happens to be tracked or present.
        missing = [
            p for p in QMAKE_OUTPUT_PATHS
            if _git("check-ignore", "--no-index", "-q", "--", p).returncode != 0
        ]
        self.assertEqual(missing, [], "qmake output not matched by .gitignore")

    def test_no_tracked_file_under_platform_qt_is_ignored(self):
        # -c -i --exclude-standard lists tracked files that an ignore rule would match.
        r = _git("ls-files", "-ci", "--exclude-standard", "--", "platform/qt")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), [], "a .gitignore rule swallows a tracked file")

    def test_sources_next_to_the_outputs_are_not_ignored(self):
        # The patterns name build products only; a source extension must never match.
        for src in (
            "platform/qt/MainWindow.cpp",
            "platform/qt/MainWindow.h",
            "platform/qt/MainWindow.ui",
            "platform/qt/MLVApp.pro",
            "platform/qt/RetinaIMG/MLVAPP.png",
        ):
            with self.subTest(src=src):
                self.assertNotEqual(
                    _git("check-ignore", "--no-index", "-q", "--", src).returncode, 0,
                    f"{src} must not be ignored",
                )


if __name__ == "__main__":
    unittest.main()
