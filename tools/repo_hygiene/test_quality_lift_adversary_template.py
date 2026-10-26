"""FLEET-QUALITY-LIFT-PUBLISH-1 r7: the adversary-pass template keeps a literal worktree path literal.

tools/coordination/quality_lift/adversary-pass-prompt.md prescribes two `git -C <worktree> ...` commands to a
PowerShell reviewer. Render the template with Render-AdversaryPrompt.ps1 for worktree paths that PowerShell would
otherwise interpret ($name, a single quote, a typographic quote, a backtick, $(...), a token-shaped name), evaluate
every prescribed command against a git argument-capture stub (no real git runs), and require the -C argument to
equal the literal path.
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
PWSH = shutil.which("pwsh")

HEAD = "0123456789abcdef0123456789abcdef01234567"
BASE = "feature/a$b"

WORKDIRS = {
    "dollar_name": "C:\\repo\\$reviewFolder",
    "single_quote": "C:\\repo\\it's here",
    "typographic_quotes": "C:\\repo\\it\u2019s \u2018x\u2019 \u201ay\u201b",
    "backtick": "C:\\repo\\a`b",
    "subexpression": "C:\\repo\\$(Get-Date)",
    "bang_and_space": "C:\\!Layi Wkspc\\lane x",
    "token_shaped": "C:\\repo\\@@BASE@@",
    "semicolon": "C:\\repo\\a;b",
}


def _run(args):
    return subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive"] + args, capture_output=True, text=True)


@unittest.skipIf(PWSH is None, "pwsh is not on PATH")
class AdversaryTemplateLiteralPathTests(unittest.TestCase):
    def _prescribed_calls(self, workdir: str) -> list[list[str]]:
        with tempfile.TemporaryDirectory() as tmp:
            prompt = Path(tmp) / "prompt.md"
            captured = Path(tmp) / "captured.json"
            r = _run(["-File", str(RENDERER), "-TemplatePath", str(TEMPLATE), "-OutPath", str(prompt),
                      "-WorkDir", workdir, "-Head", HEAD, "-Base", BASE, "-Diff", "D:\\d\\diff.patch",
                      "-Intent", "- subject", "-Card", "C-1", "-Round", "r7"])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = _run(["-File", str(HARNESS), "-PromptPath", str(prompt), "-OutPath", str(captured)])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            data = json.loads(captured.read_text(encoding="utf-8"))
            self.assertEqual(data["count"], 2, "the template prescribes exactly two git -C commands")
            return data["calls"]

    def test_worktree_path_stays_literal_in_head_and_diff_checks(self):
        for name, workdir in WORKDIRS.items():
            with self.subTest(workdir=name):
                calls = self._prescribed_calls(workdir)
                self.assertEqual(calls[0], ["-C", workdir, "rev-parse", "HEAD"])
                self.assertEqual(calls[1], ["-C", workdir, "diff", "--stat", f"{BASE}...{HEAD}"])

    def test_renderer_refuses_inputs_it_cannot_quote(self):
        good = {"-WorkDir": "C:\\w", "-Head": HEAD, "-Base": BASE}
        bad = {
            "empty_workdir": {"-WorkDir": " "},
            "newline_workdir": {"-WorkDir": "C:\\a\nb"},
            "double_backtick_workdir": {"-WorkDir": "C:\\a``b"},
            "short_head": {"-Head": "abc123"},
            "uppercase_head": {"-Head": HEAD.upper()},
            "option_shaped_base": {"-Base": "--output=x"},
            "blank_base": {"-Base": " "},
        }
        with tempfile.TemporaryDirectory() as tmp:
            for label, override in bad.items():
                with self.subTest(case=label):
                    out = Path(tmp) / (label + ".md")
                    values = {**good, **override}
                    args = [x for kv in values.items() for x in kv]
                    r = _run(["-File", str(RENDERER), "-TemplatePath", str(TEMPLATE), "-OutPath", str(out)] + args)
                    self.assertNotEqual(r.returncode, 0)
                    self.assertFalse(out.exists())

    def test_render_is_single_pass_and_drops_the_comment_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "p.md"
            r = _run(["-File", str(RENDERER), "-TemplatePath", str(TEMPLATE), "-OutPath", str(out),
                      "-WorkDir", "C:\\w", "-Head", HEAD, "-Base", BASE, "-Intent", "- see @@WORKDIR@@",
                      "-Card", "C-1", "-Round", "r7"])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = out.read_text(encoding="utf-8")
            self.assertNotIn("<!--", text)
            self.assertIn("- see @@WORKDIR@@", text, "a value that looks like a token is not expanded again")
            self.assertEqual(text.count("@@"), 2, "only the value above may keep an @@ token")
            self.assertTrue(text.startswith(f"subject {HEAD} ADVERSARY PASS"))


class TemplateShapeTests(unittest.TestCase):
    def test_template_names_the_literal_path_attack_row(self):
        text = TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("literal worktree path containing $name must remain literal in HEAD and diff checks", text)


if __name__ == "__main__":
    unittest.main()
