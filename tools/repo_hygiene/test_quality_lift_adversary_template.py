"""FLEET-QUALITY-LIFT-PUBLISH-1 r7: the adversary-pass template keeps a literal worktree path literal.

tools/coordination/quality_lift/adversary-pass-prompt.md prescribes two `git -C <worktree> ...` commands to a
PowerShell reviewer. Render the template with Render-AdversaryPrompt.ps1 for worktree paths that PowerShell would
otherwise interpret ($name, a single quote, a typographic quote, a backtick, $(...), a token-shaped name), evaluate
every prescribed command against a git argument-capture stub (no real git runs), and require the -C argument to
equal the literal path. Every row runs under each PowerShell the host has (pwsh 7, and Windows PowerShell 5.1,
which reads a BOM-less script as ANSI).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
QL = ROOT / "tools" / "coordination" / "quality_lift"
TEMPLATE = QL / "adversary-pass-prompt.md"
RENDERER = QL / "Render-AdversaryPrompt.ps1"
HARNESS = Path(__file__).resolve().parent / "quality_lift_run_prescribed_git.ps1"
SHELLS = [exe for exe in (shutil.which("pwsh"), shutil.which("powershell")) if exe]

HEAD = "0123456789abcdef0123456789abcdef01234567"
BASE = "feature/a$b"
TYPO = "".join(chr(c) for c in (0x2018, 0x2019, 0x201A, 0x201B))

WORKDIRS = {
    "dollar_name": "C:\\repo\\$reviewFolder",
    "single_quote": "C:\\repo\\it's here",
    "typographic_quote_right": "C:\\repo\\it" + chr(0x2019) + "s",
    "typographic_quotes_all": "C:\\repo\\" + TYPO,
    "backtick": "C:\\repo\\a`b",
    "subexpression": "C:\\repo\\$(Get-Date)",
    "bang_and_space": "C:\\!Layi Wkspc\\lane x",
    "token_shaped": "C:\\repo\\@@BASE@@",
    "semicolon": "C:\\repo\\a;b",
}


def _run(exe, args):
    return subprocess.run([exe, "-NoLogo", "-NoProfile", "-NonInteractive"] + args, capture_output=True, text=True)


def _render_args(out, workdir, head=HEAD, base=BASE, **extra):
    args = ["-File", str(RENDERER), "-TemplatePath", str(TEMPLATE), "-OutPath", str(out),
            "-WorkDir", workdir, "-Head", head, "-Base", base]
    for k, v in extra.items():
        args += ["-" + k, v]
    return args


@unittest.skipUnless(SHELLS, "no PowerShell on PATH")
class AdversaryTemplateLiteralPathTests(unittest.TestCase):
    def _prescribed_calls(self, exe: str, workdir: str) -> list[list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            captured = Path(tmp) / "captured.json"
            r = _run(exe, _render_args(prompt, workdir, Diff="D:\\d\\diff.patch", Intent="- subject", Card="C-1", Round="r7"))
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = _run(exe, ["-File", str(HARNESS), "-PromptPath", str(prompt), "-OutPath", str(captured)])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            data = json.loads(captured.read_text(encoding="utf-8"))
            self.assertEqual(data["count"], 2, "the template prescribes exactly two git -C commands")
            return data["calls"]

    def test_worktree_path_stays_literal_in_head_and_diff_checks(self):
        for exe in SHELLS:
            for name, workdir in WORKDIRS.items():
                with self.subTest(shell=Path(exe).stem, workdir=name):
                    calls = self._prescribed_calls(exe, workdir)
                    self.assertEqual(calls[0], ["-C", workdir, "rev-parse", "HEAD"])
                    self.assertEqual(calls[1], ["-C", workdir, "diff", "--stat", f"{BASE}...{HEAD}"])

    def test_renderer_refuses_inputs_it_cannot_quote(self):
        bad = {
            "empty_workdir": {"workdir": " "},
            "newline_workdir": {"workdir": "C:\\a\nb"},
            "double_backtick_workdir": {"workdir": "C:\\a``b"},
            "short_head": {"head": "abc123"},
            "uppercase_head": {"head": HEAD.upper()},
            "head_with_trailing_newline": {"head": HEAD + "\n"},
            "option_shaped_base": {"base": "--output=x"},
            "blank_base": {"base": " "},
        }
        with tempfile.TemporaryDirectory() as tmp:
            for exe in SHELLS:
                for label, override in bad.items():
                    with self.subTest(shell=Path(exe).stem, case=label):
                        out = Path(tmp) / (Path(exe).stem + "-" + label + ".md")
                        values = {"workdir": "C:\\w", "head": HEAD, "base": BASE, **override}
                        r = _run(exe, _render_args(out, **values))
                        self.assertNotEqual(r.returncode, 0)
                        self.assertFalse(out.exists())

    def test_render_is_single_pass_and_drops_the_comment_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            for exe in SHELLS:
                with self.subTest(shell=Path(exe).stem):
                    out = Path(tmp) / (Path(exe).stem + ".md")
                    r = _run(exe, _render_args(out, "C:\\w", Intent="- see @@WORKDIR@@", Card="C-1", Round="r7"))
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    text = out.read_text(encoding="utf-8")
                    self.assertNotIn("<!--", text)
                    self.assertIn("- see @@WORKDIR@@", text, "a value that looks like a token is not expanded again")
                    self.assertEqual(text.count("@@"), 2, "only the value above may keep an @@ token")
                    self.assertTrue(text.startswith(f"subject {HEAD} ADVERSARY PASS"))

    def test_base_lands_in_the_json_example_as_a_valid_json_string(self):
        base = 'feature/a"b\\c'
        with tempfile.TemporaryDirectory() as tmp:
            for exe in SHELLS:
                with self.subTest(shell=Path(exe).stem):
                    out = Path(tmp) / (Path(exe).stem + ".md")
                    r = _run(exe, _render_args(out, "C:\\w", base=base))
                    self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                    self.assertIn('"base":' + json.dumps(base), out.read_text(encoding="utf-8"))


class TemplateShapeTests(unittest.TestCase):
    def test_template_names_the_literal_path_attack_row(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("literal worktree path containing $name must remain literal in HEAD and diff checks", text)

    def test_powershell_files_of_this_change_are_ascii_only(self):
        # Windows PowerShell 5.1 reads a BOM-less script as ANSI, so a non-ASCII byte is misdecoded there.
        for path in (RENDERER, HARNESS):
            with self.subTest(path=path.name):
                path.read_bytes().decode("ascii")


if __name__ == "__main__":
    unittest.main()
