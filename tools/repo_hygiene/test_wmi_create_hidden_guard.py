"""Fixtures for tools/hooks/wmi-create-hidden-guard.py (WMI-CREATE-HIDDEN-GUARD-1)."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
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


if __name__ == "__main__":
    unittest.main()
