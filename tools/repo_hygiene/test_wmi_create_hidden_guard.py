"""Fixtures for tools/hooks/wmi-create-hidden-guard.py (WMI-CREATE-HIDDEN-GUARD-1)."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "tools/hooks/wmi-create-hidden-guard.py"
SPEC = importlib.util.spec_from_file_location("wmi_create_hidden_guard", HOOK)
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)

# The two shapes seen on the owner's desktop.
SWAP_DRIVER_LINE = "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $rd }"
INLINE_HUBTICK = "pwsh -NoProfile -Command \"Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c pwsh -File declare-sr.ps1'}\""
HIDDEN = ("$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ ShowWindow = [uint16]0 }\n"
          "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; ProcessStartupInformation = $si }")
HELPER_CALL = "pwsh -NoProfile -File \"C:\\!Layi Wkspc\\MLV-App\\.claude-state\\fleet-runs\\hub-tools\\Start-HiddenDetached.ps1\" -Exe pwsh.exe -ArgumentList '-File','x.ps1' -StdoutPath o.txt"


def bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


class WmiCreateHiddenGuardTests(unittest.TestCase):
    def test_swap_driver_shape_in_a_written_script_is_blocked(self):
        msg = GUARD.decide({"tool_name": "Write", "tool_input": {"file_path": "C:/x/run/swap-driver.ps1", "content": "$cmd='a'\n" + SWAP_DRIVER_LINE + "\n"}})
        self.assertIsNotNone(msg)
        self.assertIn("Start-HiddenDetached.ps1", msg)
        self.assertIn("line 2", msg)

    def test_inline_hubtick_shape_is_blocked(self):
        self.assertIsNotNone(GUARD.decide(bash(INLINE_HUBTICK)))
        self.assertIsNotNone(GUARD.decide({"tool_name": "PowerShell", "tool_input": {"command": SWAP_DRIVER_LINE}}))

    def test_edit_new_string_is_checked(self):
        self.assertIsNotNone(GUARD.decide({"tool_name": "Edit", "tool_input": {"file_path": "C:/x/a.ps1", "new_string": SWAP_DRIVER_LINE}}))

    def test_hidden_create_and_helper_are_allowed(self):
        self.assertIsNone(GUARD.decide({"tool_name": "Write", "tool_input": {"file_path": "C:/x/a.ps1", "content": HIDDEN}}))
        self.assertIsNone(GUARD.decide(bash(HELPER_CALL)))

    def test_read_only_search_and_fixtures_and_comments_are_allowed(self):
        self.assertIsNone(GUARD.decide(bash("grep -n 'Invoke-CimMethod -ClassName Win32_Process -MethodName Create' x.ps1")))
        self.assertIsNone(GUARD.decide(bash("git grep -n 'Invoke-CimMethod Win32_Process Create'")))
        self.assertIsNone(GUARD.decide({"tool_name": "Write", "tool_input": {"file_path": "C:/x/Test-Foo.ps1", "content": SWAP_DRIVER_LINE}}))
        self.assertIsNone(GUARD.decide({"tool_name": "Write", "tool_input": {"file_path": "C:/x/a.ps1", "content": "# never: Invoke-CimMethod Win32_Process Create"}}))
        self.assertIsNone(GUARD.decide(bash("Get-CimInstance Win32_Process -Filter \"ProcessId=1\"")))
        self.assertIsNone(GUARD.decide({"tool_name": "Read", "tool_input": {"file_path": "C:/x/a.ps1"}}))

    def test_cli_exit_codes(self):
        def run(payload):
            return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=30)
        blocked = run(bash(INLINE_HUBTICK))
        self.assertEqual(2, blocked.returncode)
        self.assertIn("WMI-CREATE-HIDDEN-GUARD", blocked.stderr)
        self.assertEqual(0, run(bash(HELPER_CALL)).returncode)
        self.assertEqual(0, subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True, timeout=30).returncode)


# r2 (PR #547 fix round): prose vs code, compound commands, own source, breadth.
FORM = "Invoke-CimMethod -ClassName Win32_Process -MethodName Create"
D6A_LINE = "- Never hand-write Invoke-CimMethod / Invoke-WmiMethod / [wmiclass] Win32_Process Create, inline or in a script."
SCRIPT_EXTS = (".ps1", ".psm1", ".psd1", ".bat", ".cmd", ".py", ".js", ".mjs", ".vbs", ".sh")


def write(path, content):
    return {"tool_name": "Write", "tool_input": {"file_path": path, "content": content}}


def ps(cmd):
    return {"tool_name": "PowerShell", "tool_input": {"command": cmd}}


class WmiCreateHiddenGuardR2Tests(unittest.TestCase):
    def test_prose_quoting_the_form_is_allowed_outside_script_files(self):
        quoted = "Use the helper instead of `%s`." % FORM
        self.assertIsNone(GUARD.decide(write("C:/x/prompt.md", quoted)))
        self.assertIsNone(GUARD.decide(write("C:/x/lane-X-r1/prompt.md", "## TOOLING\n" + D6A_LINE + "\n")))
        self.assertIsNone(GUARD.decide({"tool_name": "Edit", "tool_input": {"file_path": "C:/x/subject-ledger.md", "new_string": quoted}}))
        for name in ("notes.txt", "data.json", "ci.yml", "ledger.jsonl"):
            self.assertIsNone(GUARD.decide(write("C:/x/" + name, quoted)), name)

    def test_executable_script_types_are_still_scanned(self):
        for ext in SCRIPT_EXTS:
            self.assertIsNotNone(GUARD.decide(write("C:/x/run" + ext, SWAP_DRIVER_LINE)), ext)

    def test_quoted_operands_of_data_commands_are_allowed(self):
        for cmd in (
            'cat "%s"' % FORM,
            'Get-Content "%s"' % FORM,
            "gh pr list --search '%s'" % FORM,
            "git commit -m 'Guard %s'" % FORM,
            'gh pr comment 547 --body "never hand-write %s"' % FORM,
            "Get-Content x.ps1 | Select-String '%s'" % FORM,
            "pwsh -NoProfile -File \"C:\\x\\Add-LedgerLine.ps1\" -Text 'avoid %s'" % FORM,
            "git commit -F - <<'EOF'\nGuard %s\nEOF" % FORM,
        ):
            self.assertIsNone(GUARD.decide(bash(cmd)), cmd)

    def test_data_commands_that_write_or_execute_are_still_scanned(self):
        for cmd in (
            "echo '%s' > run.ps1" % SWAP_DRIVER_LINE,
            "echo '%s' | Set-Content run.ps1" % SWAP_DRIVER_LINE,
            "cat > run.ps1 <<'EOF'\n%s\nEOF" % SWAP_DRIVER_LINE,
            "echo \"$(%s)\"" % SWAP_DRIVER_LINE,
        ):
            self.assertIsNotNone(GUARD.decide(bash(cmd)), cmd)

    def test_search_exemption_is_per_segment_not_first_word(self):
        self.assertIsNotNone(GUARD.decide(bash("rg x x.ps1; Set-Content run.ps1 '%s'" % SWAP_DRIVER_LINE)))
        self.assertIsNotNone(GUARD.decide(bash("rg 'a;b' . && %s" % SWAP_DRIVER_LINE)))
        self.assertIsNotNone(GUARD.decide(bash("rg x x.ps1\n%s" % SWAP_DRIVER_LINE)))
        self.assertIsNone(GUARD.decide(bash("cd d && rg -n '%s' ." % FORM)))
        self.assertIsNone(GUARD.decide(bash("cd d; git grep -n '%s'" % FORM)))

    def test_hook_does_not_trip_on_its_own_source(self):
        text = HOOK.read_text(encoding="utf-8")
        self.assertIsNone(GUARD.decide(write("C:/x/tools/hooks/wmi-create-hidden-guard.py", text)))
        self.assertIsNone(GUARD.decide({"tool_name": "Edit", "tool_input": {"file_path": "C:/x/tools/hooks/wmi-create-hidden-guard.py", "new_string": text}}))

    def test_notebookedit_allow_and_deny(self):
        nb = "C:/x/a.ipynb"
        self.assertIsNotNone(GUARD.decide({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": nb, "new_source": SWAP_DRIVER_LINE}}))
        self.assertIsNone(GUARD.decide({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": nb, "new_source": HIDDEN}}))
        self.assertIsNone(GUARD.decide({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": nb, "new_source": "see `%s`" % FORM, "cell_type": "markdown"}}))

    def test_powershell_and_edit_allow_cases(self):
        self.assertIsNone(GUARD.decide(ps(HIDDEN)))
        self.assertIsNone(GUARD.decide(ps("Get-CimInstance Win32_Process | Select-Object ProcessId")))
        self.assertIsNone(GUARD.decide({"tool_name": "Edit", "tool_input": {"file_path": "C:/x/a.ps1", "new_string": HIDDEN}}))

    def test_search_command_exemptions_and_test_file_exemption(self):
        for cmd in ("rg -n '%s' ." % FORM, "findstr /s /c:\"%s\" *.ps1" % FORM, "Select-String -Path x.ps1 -Pattern '%s'" % FORM):
            self.assertIsNone(GUARD.decide(bash(cmd)), cmd)
        self.assertIsNone(GUARD.decide(write("C:/x/test_live.py", SWAP_DRIVER_LINE)))

    def test_notebook_write_scans_code_cells_only(self):
        # r3 contract: ".ipynb code cells". A markdown cell quoting the form is prose.
        def nb(*cells):
            return json.dumps({"cells": [{"cell_type": kind, "source": src.splitlines(True)} for kind, src in cells]})
        path = "C:/x/a.ipynb"
        self.assertIsNone(GUARD.decide(write(path, nb(("markdown", "never `%s`" % FORM), ("code", "print(1)")))))
        self.assertIsNotNone(GUARD.decide(write(path, nb(("markdown", "prose"), ("code", FORM)))))
        self.assertIsNone(GUARD.decide(write(path, nb(("code", "$si = New-CimInstance -Property @{ ShowWindow = [uint16]0 }\n" + FORM)))))
        self.assertIsNone(GUARD.decide({"tool_name": "Edit", "tool_input": {"file_path": path, "new_string": nb(("markdown", FORM))}}))
        self.assertIsNotNone(GUARD.decide(write(path, "not json\n" + FORM)))

    def test_showwindow_in_another_segment_of_the_same_line_does_not_count(self):
        # r3 contract: each segment is judged on its own; the window spans neighbouring lines, not neighbouring segments.
        self.assertIsNotNone(GUARD.decide(ps("$si = 'ShowWindow'; " + FORM)))
        self.assertIsNotNone(GUARD.decide(ps(FORM + " | Out-Null; $x = 'ShowWindow'")))
        self.assertIsNotNone(GUARD.decide(bash("echo ShowWindow && " + FORM)))
        self.assertIsNone(GUARD.decide(ps("$si = 'ShowWindow'\n" + FORM)))
        self.assertIsNone(GUARD.decide(ps(FORM + " -Arguments @{ ShowWindow = 0 }")))
        # a semicolon inside a hashtable splits the line, but Win32_Process and Create are still found together
        self.assertIsNotNone(GUARD.decide(ps("Invoke-CimMethod -ClassName Win32_Process -Arguments @{ a = 1; b = 2 } -MethodName Create")))
        self.assertIsNone(GUARD.decide(write("C:/x/a.ps1", "$si = 'ShowWindow'; " + FORM)))

    def test_ledger_helper_pattern_keeps_its_verdicts_and_has_no_ambiguous_loop(self):
        # r3: CodeQL py/redos flagged the option loop. Same verdicts, unambiguous shape, linear on a pumped input.
        led = "Add-LedgerLine.ps1"
        for cmd, expect in (
            (led + " -Text x", True),
            ("& \"C:\\x\\%s\" -Text x" % led, True),
            ("pwsh -File %s -Text x" % led, True),
            ("pwsh.exe -NoProfile -File \"C:\\x\\%s\" -Text x" % led, True),
            ("pwsh -NoProfile -ExecutionPolicy Bypass -File C:\\x\\%s -Text x" % led, True),
            ("powershell -NoLogo -NonInteractive -File %s" % led, True),
            ("pwsh -NoProfile -Command %s" % led, False),
            ("pwsh -NoProfile %s" % led, False),
            ("pwsh -File other.ps1 -Text x", False),
            ("python %s" % led, False),
        ):
            self.assertEqual(expect, bool(GUARD._LEDGER.match(cmd)), cmd)
        self.assertNotIn(r"(?!-)", GUARD._LEDGER.pattern)
        pumped = "pwsh" + " -0" * 20000 + " x"
        started = time.monotonic()
        self.assertIsNone(GUARD._LEDGER.match(pumped))
        self.assertLess(time.monotonic() - started, 2.0)


if __name__ == "__main__":
    unittest.main()
