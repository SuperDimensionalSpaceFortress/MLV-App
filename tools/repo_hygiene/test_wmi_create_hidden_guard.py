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


# r4 (PR #547, sol r3 blockers B1-B3): the code did less than the declared contract.
UNHIDDEN_ARGS = "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine='cmd.exe /c exit'}"
HIDDEN_ONE_LINE = ("Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
                   "@{ProcessStartupInformation=(New-CimInstance Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=0});CommandLine='cmd.exe /c exit'}")
QUOTED_PWSH = "'C:/Program Files/PowerShell/7/pwsh.exe'"
COMMIT_HEREDOC = "git commit -m \"$(cat <<'EOF'\n%s\nEOF\n)\""


class WmiCreateHiddenGuardR4Tests(unittest.TestCase):
    def test_every_create_on_a_line_is_judged_on_its_own_window(self):
        # B1: a hidden Create's ShowWindow must not mask a later unhidden Create on the same line.
        self.assertIsNotNone(GUARD.decide(ps(HIDDEN_ONE_LINE + "; " + UNHIDDEN_ARGS)))
        self.assertIsNotNone(GUARD.decide(bash(HIDDEN_ONE_LINE + " && " + UNHIDDEN_ARGS)))
        self.assertIsNotNone(GUARD.decide(ps(UNHIDDEN_ARGS + "; " + HIDDEN_ONE_LINE)))
        # two hidden Creates, and a hidden Create followed by prose that merely says Create, stay allowed
        self.assertIsNone(GUARD.decide(ps(HIDDEN_ONE_LINE + "; " + HIDDEN_ONE_LINE)))
        self.assertIsNone(GUARD.decide(ps(HIDDEN_ONE_LINE + "; Write-Host 'Create done'")))
        self.assertIsNone(GUARD.decide(ps(HIDDEN_ONE_LINE)))

    def test_a_quoted_interpreter_path_is_still_an_interpreter_sink(self):
        # B2: launch text piped into a quoted interpreter path is code, exactly as with the bare name.
        launch = 'Write-Output "%s"' % UNHIDDEN_ARGS
        for sink in ("pwsh -NoProfile -Command -",
                     "& %s -NoProfile -Command -" % QUOTED_PWSH,
                     "& \"C:\\Program Files\\PowerShell\\7\\pwsh.exe\" -NoProfile -Command -",
                     "%s -NoProfile -Command -" % QUOTED_PWSH,
                     ". \"powershell.exe\" -Command -"):
            self.assertIsNotNone(GUARD.decide(ps("%s | %s" % (launch, sink))), sink)
        # a quoted data tool is still data, and a quoted non-interpreter is not a sink
        self.assertIsNone(GUARD.decide(ps("Get-Content x.ps1 | & 'C:/tools/rg.exe' -n \"%s\"" % FORM)))
        self.assertIsNone(GUARD.decide(ps("%s | & 'C:/tools/other.exe' -x" % launch)))

    def test_commit_heredoc_exempts_only_the_heredoc_substitution(self):
        # B3: one heredoc marker must not exempt a second, executable dollar-paren substitution.
        second = "$(pwsh -NoProfile -Command \"%s\")" % UNHIDDEN_ARGS
        self.assertIsNotNone(GUARD.decide(bash(COMMIT_HEREDOC % "message" + " -m \"%s\"" % second)))
        self.assertIsNotNone(GUARD.decide(bash("git commit -m \"%s\" -m \"%s\"" % (second, "$(cat <<'EOF'\nmessage\nEOF\n)"))))
        # an unterminated heredoc substitution is not the idiom
        self.assertIsNotNone(GUARD.decide(bash("git commit -m \"$(cat <<'EOF'\nGuard %s\n)\"" % FORM)))
        # the idiom itself, with the form in its body or a dollar-paren inside its body, stays data
        self.assertIsNone(GUARD.decide(bash(COMMIT_HEREDOC % ("Guard " + FORM))))
        self.assertIsNone(GUARD.decide(bash(COMMIT_HEREDOC % "mentions $(pwsh -Command x) in prose")))
        self.assertIsNone(GUARD.decide(bash(COMMIT_HEREDOC % ("Guard " + FORM) + " -m 'second %s'" % FORM)))


# r5 (PR #547, sol r4 blockers B1-B3): r4 closed one spelling of each family. The row generators below are the family tables.
SINK_NAMES = ("pwsh", "pwsh.exe", "powershell", "powershell.exe", "cmd", "cmd.exe", "bash", "sh", "python", "py", "node", "wscript", "cscript")
SINK_PATHS = ("C:/Program Files/PowerShell/7/pwsh.exe", "C:\\Windows\\System32\\cmd.exe", "/usr/bin/python",
              "C:/Program Files/Git/bin/bash.exe", "C:\\Windows\\System32\\wscript.exe")
CALL_OPS = ("", "& ", "&  ", "&", ". ", ".")
LAUNCH = 'Write-Output "%s"' % UNHIDDEN_ARGS
DATA_LAUNCH = 'echo "%s"' % UNHIDDEN_ARGS


def invocation_rows():
    """(spelling, command) for a launch fed to an interpreter spelled with a call operator, a quote, or both. Every row is DENY."""
    for target in SINK_NAMES + SINK_PATHS:
        for quote in ("", "'", '"'):
            if quote == "" and " " in target:
                continue
            for op in CALL_OPS:
                if quote == "" and op == ".":
                    continue  # `.pwsh` is a file name, not a dot-source
                head = "%s%s%s%s" % (op, quote, target, quote)
                kind = "op=%r quote=%s" % (op, quote or "bare")
                yield kind + " piped ' | '", "%s | %s -NoProfile -Command -" % (LAUNCH, head)
                yield kind + " piped '|'", "%s|%s -NoProfile -Command -" % (LAUNCH, head)
                yield kind + " inline", "%s -NoProfile -Command \"%s\"" % (head, UNHIDDEN_ARGS)


REDIRECT_WRITES = (
    "> run.ps1", ">run.ps1", ">> run.ps1", ">>run.ps1", ">| run.ps1", ">|run.ps1", ">& run.ps1", ">&run.ps1", ">& 'run.ps1'",
    "&> run.ps1", "&>run.ps1", "&>> run.ps1", "1> run.ps1", "1>> run.ps1", "1>run.ps1", "3> run.ps1", "2> run.ps1", "2>> run.ps1",
    "*> run.ps1", "*>> run.ps1", "*>run.ps1", "2>&1 > run.ps1", "> run.ps1 2>&1", ">& run.ps1 2>&1",
    "| tee run.ps1", "|tee run.ps1", "| tee -a run.ps1", "|& tee run.ps1", "| Out-File run.ps1", "| Out-File -FilePath run.ps1",
    "| Set-Content run.ps1", "| Set-Content -Path run.ps1 -Encoding utf8", "| Add-Content run.ps1", "| Tee-Object run.ps1",
)
FD_DUPLICATIONS = ("2>&1", ">&2", "1>&2", ">& 2", ">&-", "2>&-", "2>/dev/null", "2> /dev/null", "2>>/dev/null", "2>$null", "2>nul", "*>&1", "2>&1 | Select-String x")


def redirect_rows():
    """(spelling, expected_deny, command): a data command whose output goes to a file is code; fd duplication is not a redirect."""
    for tail in REDIRECT_WRITES:
        yield tail, True, "%s %s; pwsh -NoProfile -File run.ps1" % (DATA_LAUNCH, tail)
    for tail in FD_DUPLICATIONS:
        yield tail, False, "%s %s; echo done" % (DATA_LAUNCH, tail)
        yield tail, False, "grep -n \"%s\" x.ps1 %s" % (FORM, tail)


def heredoc_rows():
    """(spelling, expected_deny, command) for the commit-message heredoc idiom and for standalone heredocs."""
    subst = "$(pwsh -NoProfile -Command \"%s\")" % UNHIDDEN_ARGS
    tick = "`pwsh -NoProfile -Command '%s'`" % UNHIDDEN_ARGS
    for opener, literal in (("'EOF'", True), ("\"EOF\"", True), ("\\EOF", True), ("-'EOF'", True), ("-\"EOF\"", True), ("-\\EOF", True),
                            ("EOF", False), ("-EOF", False), (" EOF", False), ("- EOF", False)):
        commit = "git commit -m \"$(cat <<%s\n%%s\nEOF\n)\"" % opener
        yield "commit %s prose" % opener, not literal, commit % ("Guard " + FORM)
        yield "commit %s dollar-paren body" % opener, not literal, commit % subst
        yield "commit %s backtick body" % opener, not literal, commit % tick
        yield "commit %s second substitution" % opener, True, (commit % "message") + " -m \"%s\"" % subst
        standalone = "cat <<%s\n%%s\nEOF" % opener
        yield "standalone %s dollar-paren body" % opener, True, standalone % subst
        yield "standalone %s backtick body" % opener, not literal, standalone % tick
        yield "standalone %s prose" % opener, False, standalone % ("Guard " + FORM)
        yield "commit -F %s dollar-paren body" % opener, True, "git commit -F - <<%s\n%s\nEOF" % (opener, subst)
        yield "interpreter fed %s" % opener, True, "pwsh -NoProfile -Command - <<%s\n%s\nEOF" % (opener, UNHIDDEN_ARGS)


class WmiCreateHiddenGuardR5Tests(unittest.TestCase):
    def _verdicts(self, command):
        return (GUARD.decide(bash(command)) is not None, GUARD.decide(ps(command)) is not None)

    def test_invocation_family_every_spelling_is_a_sink(self):
        # B1: a call operator, a dot-source, or a quote in any combination does not hide the interpreter.
        rows = list(invocation_rows())
        self.assertGreater(len(rows), 800)
        for spelling, command in rows:
            self.assertEqual((True, True), self._verdicts(command), "%s: %s" % (spelling, command))

    def test_invocation_family_controls_and_separators(self):
        # a quoted data tool or a non-interpreter stays data/ALLOW; `&` after a command is a separator, not a call operator
        self.assertIsNone(GUARD.decide(ps("Get-Content x.ps1 | &'C:/tools/rg.exe' -n \"%s\"" % FORM)))
        self.assertIsNone(GUARD.decide(ps("%s | &'C:/tools/other.exe' -x" % LAUNCH)))
        self.assertIsNone(GUARD.decide(ps("%s |&\"C:/tools/other.exe\" -x" % LAUNCH)))
        self.assertIsNone(GUARD.decide(ps("%s | . 'C:/tools/other.exe'" % LAUNCH)))
        self.assertIsNotNone(GUARD.decide(bash("echo hi &'pwsh' -c \"%s\"" % UNHIDDEN_ARGS)))
        self.assertIsNotNone(GUARD.decide(bash("echo hi ;&'pwsh' -c \"%s\"" % UNHIDDEN_ARGS)))
        self.assertIsNotNone(GUARD.decide(ps("%s | ForEach-Object { $_ } |&'C:/Program Files/PowerShell/7/pwsh.exe' -NoProfile -Command -" % LAUNCH)))

    def test_redirect_family_every_spelling(self):
        # B2: any file redirect or writer turns a data command into code; fd duplication does not.
        rows = list(redirect_rows())
        self.assertGreater(len(rows), 40)
        for spelling, deny, command in rows:
            self.assertEqual((deny, deny), self._verdicts(command), "%s: %s" % (spelling, command))
        # the sol repro verbatim
        self.assertIsNotNone(GUARD.decide(bash("echo \"%s\" >& run.ps1; pwsh -NoProfile -File run.ps1" % UNHIDDEN_ARGS)))

    def test_heredoc_family_only_a_quoted_delimiter_is_literal(self):
        # B3: an unquoted heredoc body expands its substitutions, so it is scanned like any other command text.
        rows = list(heredoc_rows())
        self.assertGreater(len(rows), 80)
        for spelling, deny, command in rows:
            self.assertEqual((deny, deny), self._verdicts(command), "%s: %s" % (spelling, command))
        # the sol repro verbatim, and the allowed commit idiom with a quoted delimiter
        self.assertIsNotNone(GUARD.decide(bash("git commit -m \"$(cat <<EOF\n$(pwsh -NoProfile -Command \"%s\")\nEOF\n)\"" % UNHIDDEN_ARGS)))
        self.assertIsNone(GUARD.decide(bash(COMMIT_HEREDOC % ("Guard " + FORM))))


if __name__ == "__main__":
    unittest.main()
