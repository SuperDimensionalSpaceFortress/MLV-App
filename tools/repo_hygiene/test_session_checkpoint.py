"""Offline regression fixtures; importing the checkpoint never invokes its hooks."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("session_checkpoint", ROOT / "tools/session-checkpoint.py")
CHECKPOINT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKPOINT)
SESSION = "12345678-1234-4321-aaaa-0123456789ab"


class SessionCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.fleet = self.repo / ".claude-state/fleet-runs"
        self.fleet.mkdir(parents=True)

    def receipt(self, nested=False, **overrides):
        run = self.fleet / "owned-run" if nested else self.fleet
        run.mkdir(exist_ok=True)
        prompt = run / "luna-001.prompt.txt"
        prompt.write_text("fixture only", encoding="utf-8")
        data = dict(sessionId=SESSION, lane="luna", card="FIXTURE", exitCode=None,
                    promptPath=str(prompt), outputPath=str(run / "luna-001.last.txt"),
                    state="reserved", complete=False, workDir=str(self.repo))
        data.update(overrides)
        path = run / "luna-001.receipt.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path, prompt

    def test_root_receipt_uses_explicit_prompt_path(self):
        custom = self.repo / "prompt with spaces.txt"
        custom.write_text("fixture", encoding="utf-8")
        receipt, _ = self.receipt(promptPath=str(custom))
        result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertIsNotNone(result)
        self.assertEqual(str(custom), result["prompt_path"])
        self.assertEqual(str(receipt), result["receipt"])

    def test_nested_receipt_falls_back_to_actual_prompt_suffix(self):
        receipt, prompt = self.receipt(nested=True, promptPath=None)
        result = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertIsNotNone(result)
        self.assertEqual(str(prompt), result["prompt_path"])
        self.assertEqual(str(receipt), result["receipt"])

    def test_scan_does_not_walk_below_run_dir_depth(self):
        deep = self.fleet / "hub-tools/legspec-precheck/copy"
        deep.mkdir(parents=True)
        (deep / "luna-001.receipt.json").write_text(json.dumps(dict(
            sessionId=SESSION, lane="luna", card="FIXTURE", exitCode=None,
            promptPath=str(deep / "luna-001.prompt.txt"), workDir=str(self.repo))), encoding="utf-8")
        self.assertIsNone(CHECKPOINT.scan_fleet_runs(self.repo, SESSION))

    def test_missing_empty_and_prefix_collision_session_are_not_owned(self):
        for owner in (None, "", "12345678-another-session"):
            with self.subTest(owner=owner):
                self.receipt(sessionId=owner)
                self.assertIsNone(CHECKPOINT.scan_fleet_runs(self.repo, SESSION))
        self.receipt()
        self.assertIsNone(CHECKPOINT.scan_fleet_runs(self.repo, ""))

    def test_malformed_receipt_is_not_a_hook_failure(self):
        receipt, _ = self.receipt()
        receipt.write_text("{", encoding="utf-8")
        self.assertIsNone(CHECKPOINT.scan_fleet_runs(self.repo, SESSION))

    def test_resumption_is_metadata_without_executable_mutation_advice(self):
        receipt, prompt = self.receipt()
        invocation = dict(lane="luna", card="FIXTURE", receipt=str(receipt),
                          prompt_path=str(prompt), output_path="output.txt",
                          worktree=str(self.repo), status="IN_PROGRESS")
        lines = CHECKPOINT.generate_resume_script(self.repo, "codex/fixture", SESSION,
                                                 ["owned.txt"], invocation)
        text = "\n".join(lines)
        for forbidden in ("git checkout", "git add", "git commit", " checkout ",
                          " add -- ", " commit -m ", "Invoke-Lane.ps1", "pwsh "):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, text)
        for pointer in ("codex/fixture", str(receipt), str(prompt), "FIXTURE", "owned.txt"):
            with self.subTest(pointer=pointer):
                self.assertIn(pointer, text)
        self.assertEqual([], CHECKPOINT.generate_resume_script(self.repo, "b", SESSION, [], None))
        invocation["status"] = "COMPLETED"
        self.assertIn(f"- receipt: {receipt}",
                      CHECKPOINT.generate_resume_script(self.repo, "b", SESSION, [], invocation))

    def test_successful_process_keeps_pointers_in_rendered_stop_checkpoint(self):
        receipt, prompt = self.receipt(nested=True, exitCode=0, state="complete", complete=True)
        invocation = CHECKPOINT.scan_fleet_runs(self.repo, SESSION)
        self.assertEqual("PROCESS_EXITED", invocation["status"])
        self.assertEqual(0, invocation["exit_code"])
        self.assertTrue(invocation["complete"])
        fake_home = self.repo / "fixture-home"
        def fake_git(cwd, *args):
            if "--git-common-dir" in args:
                return str(self.repo / ".git")
            if "symbolic-ref" in args:
                return "codex/fixture"
            return ""
        payload = json.dumps(dict(cwd=str(self.repo), session_id=SESSION, hook_event_name="Stop"))
        with patch.object(CHECKPOINT, "git", side_effect=fake_git), \
             patch.object(CHECKPOINT, "account_id", return_value=""), \
             patch.object(Path, "home", return_value=fake_home), \
             patch.object(CHECKPOINT.sys, "stdin", io.StringIO(payload)), \
             patch.object(CHECKPOINT.sys, "stdout", io.StringIO()):
            self.assertEqual(0, CHECKPOINT.main())
        checkpoint = fake_home / ".claude/session-checkpoints" / self.repo.name.replace(" ", "-") / f"SESSION-{SESSION[:8]}.md"
        text = checkpoint.read_text(encoding="utf-8")
        for value in (str(receipt), str(prompt), str(self.fleet / "owned-run/luna-001.last.txt"),
                      f"- worktree: {self.repo}", "PROCESS_EXITED", "- state: complete"):
            with self.subTest(value=value):
                self.assertIn(value, text)
        self.assertNotIn("COMPLETED", text)
        self.assertNotIn("delivery accepted", text.lower())

    def test_main_passes_full_session_identity_to_scan(self):
        import ast
        source = (ROOT / "tools/session-checkpoint.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "scan_fleet_runs"]
        self.assertEqual(1, len(calls))
        self.assertIsInstance(calls[0].args[1], ast.Name)
        self.assertEqual("session_id", calls[0].args[1].id)


if __name__ == "__main__":
    unittest.main()
