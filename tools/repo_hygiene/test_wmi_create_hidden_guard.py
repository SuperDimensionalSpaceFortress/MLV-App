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


# r6 (PR #547, opus r5 H1 and sol r5 H2): the heredoc reader is one grammar, not a list of spellings.
#   * the delimiter word is bash's: up to unquoted whitespace or ; & | < > ( ) with quote removal; any quoted part makes the body literal
#   * the body belongs to the segment whose command line opened it, whatever segments follow on that line
#   * a terminator is the exact line (leading tabs allowed only for <<-); a heredoc with no terminator fails closed like an open quote
LITERAL_WORDS = ("'EOF'", '"EOF"', "\\EOF", 'E"OF"', "E'OF'", "EO\\F", "'E''OF'", '"E"OF', "E\\OF", "'E'\"O\"F")
PUNCT_TAGS = ("END-MESSAGE", "EOF.1", "EOF/END", "EOF_1", "END:X", "END+X", "1EOF", "a.b-c_d", "E-", "EOF=1", "EOF,1", "END!", "{END}", "EOF@x")
OPENER_WRITERS = ("cat > run.ps1", "cat >> run.ps1", "cat >| run.ps1", "tee run.ps1", "tee -a run.ps1")
OPENER_SEPS = ("&&", ";", "||", "|", "&")
DATA_FOLLOWERS = ("cat run.ps1", "echo ok", "grep -n x run.ps1", "ls")


def grammar_rows():
    """(spelling, expected_deny, command) for the heredoc grammar: opener ownership, delimiter words, terminators, unterminated bodies."""
    launch = 'pwsh -NoProfile -Command "%s"' % UNHIDDEN_ARGS
    tick = "`pwsh -NoProfile -Command '%s'`" % UNHIDDEN_ARGS
    prose = "Guard " + FORM
    # H1: the body is the opener's, whichever data command follows it on the opener line
    for writer in OPENER_WRITERS:
        for sep in OPENER_SEPS:
            for follower in DATA_FOLLOWERS:
                for word in ("'EOF'", "EOF"):
                    yield "H1 %s <<%s %s %s" % (writer, word, sep, follower), True, "%s <<%s %s %s\n%s\nEOF" % (writer, word, sep, follower, UNHIDDEN_ARGS)
    for sep in OPENER_SEPS:
        for follower in DATA_FOLLOWERS:
            yield "H1 data opener %s %s, prose body" % (sep, follower), False, "cat <<'EOF' %s %s\n%s\nEOF" % (sep, follower, prose)
            yield "H1 interpreter opener %s %s" % (sep, follower), True, "pwsh -NoProfile -Command - <<'EOF' %s %s\n%s\nEOF" % (sep, follower, UNHIDDEN_ARGS)
    # the body is not the follower's: a code segment after a data opener does not read it, but a pipe into an interpreter does
    for sep in ("&&", ";", "||", "&"):
        yield "H1 data opener %s code follower, prose body" % sep, False, "cat <<'EOF' %s pwsh -NoProfile -File run.ps1\n%s\nEOF" % (sep, prose)
    yield "H1 data opener | interpreter, prose body", True, "cat <<'EOF' | pwsh -NoProfile -Command -\n%s\nEOF" % prose
    yield "H1 data opener ; hidden launch on the opener line", True, "cat <<'EOF'; %s\nmessage\nEOF" % launch
    # several heredocs on one line are read in order, each owned by its own opener
    yield "MULTI data then writer", True, "cat <<'A' && cat > run.ps1 <<'B'\n%s\nA\n%s\nB" % (prose, UNHIDDEN_ARGS)
    yield "MULTI writer then data", True, "cat > run.ps1 <<'A' && cat <<'B'\n%s\nA\n%s\nB" % (UNHIDDEN_ARGS, prose)
    yield "MULTI data and data", False, "cat <<'A' && cat <<'B'\n%s\nA\n%s\nB" % (prose, prose)
    yield "MULTI one command two heredocs", False, "cat <<'A' <<'B'\n%s\nA\n%s\nB" % (prose, prose)
    yield "MULTI one command, second body substitution", True, "cat <<'A' <<'B'\n%s\nA\n$(%s)\nB" % (prose, launch)
    yield "MULTI second unterminated", True, "cat <<'A' <<'B'\n%s\nA\n%s" % (prose, prose)
    yield "MULTI launch after both", True, "cat <<'A' <<'B'\nx\nA\ny\nB\n%s" % launch
    # H2: the delimiter word keeps its punctuation, quoted or not
    for tag in PUNCT_TAGS:
        for quote in ("", "'", '"'):
            word = quote + tag + quote
            yield "H2 <<%s launch after terminator" % word, True, "cat <<%s\nmessage\n%s\n%s" % (word, tag, launch)
            yield "H2 <<%s backtick body" % word, not quote, "cat <<%s\n%s\n%s" % (word, tick, tag)
            yield "H2 <<%s prose body" % word, False, "cat <<%s\n%s\n%s" % (word, prose, tag)
            yield "H2 <<%s wrong terminator fails closed" % word, True, "cat <<%s\n%s\n%sX" % (word, prose, tag)
    # quote removal: any quoted part makes the body literal, whatever the spelling
    for word in LITERAL_WORDS:
        yield "WORD <<%s launch after terminator" % word, True, "cat <<%s\nmessage\nEOF\n%s" % (word, launch)
        yield "WORD <<%s backtick body is literal" % word, False, "cat <<%s\n%s\nEOF" % (word, tick)
        yield "WORD <<%s prose body" % word, False, "cat <<%s\n%s\nEOF" % (word, prose)
        yield "WORD <<%s unterminated" % word, True, "cat <<%s\n%s\nEND" % (word, prose)
        yield "WORD <<-%s tab-indented terminator" % word, False, "cat <<-%s\n\t%s\n\tEOF" % (word, prose)
        if '"' not in word:  # a double quote in the delimiter cannot sit inside the double-quoted commit message
            yield "WORD commit idiom with %s" % word, False, "git commit -m \"$(cat <<%s\n%s\nEOF\n)\"" % (word, prose)
    # the word ends at an unquoted metacharacter
    yield "WORD <<EOF>out redirect", True, "cat <<EOF>out.ps1\n%s\nEOF" % UNHIDDEN_ARGS
    yield "WORD <<EOF|interpreter", True, "cat <<EOF|pwsh -NoProfile -Command -\n%s\nEOF" % UNHIDDEN_ARGS
    yield "WORD <<EOF&& data", False, "cat <<EOF&& cat x\n%s\nEOF" % prose
    yield "WORD <<EOF; launch", True, "cat <<EOF; %s\nx\nEOF" % launch
    yield "WORD <<'E OF' (a space inside quotes)", True, "cat <<'E OF'\nx\nE OF\n%s" % launch
    yield "WORD <<EOF with no space before it", True, "cat<<EOF\nx\nEOF\n%s" % launch
    # terminators: the exact line; <<- strips leading tabs only
    yield "TERM <<- tab-indented", False, "cat <<-'EOF'\n\t%s\n\tEOF" % prose
    yield "TERM <<- tab-indented, launch after", True, "cat <<-EOF\n\tx\n\tEOF\n%s" % launch
    yield "TERM <<- two tabs", False, "cat <<-'EOF'\n\t\t%s\n\t\tEOF" % prose
    yield "TERM <<- space-indented is not a terminator", True, "cat <<-'EOF'\n%s\n    EOF" % prose
    yield "TERM << tab-indented is not a terminator", True, "cat <<'EOF'\n%s\n\tEOF" % prose
    yield "TERM << space-indented is not a terminator", True, "cat <<'EOF'\n%s\n EOF" % prose
    yield "TERM trailing space is not a terminator", True, "cat <<'EOF'\n%s\nEOF " % prose
    yield "TERM trailing space, later real terminator", False, "cat <<'EOF'\n%s\nEOF \nmore\nEOF" % prose
    yield "TERM trailing tab is not a terminator", True, "cat <<'EOF'\n%s\nEOF\t" % prose
    yield "TERM terminator is a prefix of a longer line", True, "cat <<'EOF'\n%s\nEOFX" % prose
    yield "TERM <<- strips a tab but not the tag", True, "cat <<-'EOF'\n%s\n\tEOFX" % prose
    yield "TERM terminator on the last line, no newline", False, "cat <<'EOF'\n%s\nEOF" % prose
    yield "TERM terminator then newline then launch", True, "cat <<'EOF'\n%s\nEOF\n%s" % (prose, launch)
    yield "TERM empty quoted delimiter ends at an empty line", False, "cat <<''\n%s\n\n" % prose
    # fail closed: no terminator line means no exemption for anything after the opener
    yield "OPEN no terminator, prose body", True, "cat <<'EOF'\n%s" % prose
    yield "OPEN no terminator, launch after", True, "cat <<EOF\nmessage\n%s" % launch
    yield "OPEN opener with no body at all", True, "cat <<'EOF' %s" % prose
    yield "OPEN first terminated, second not", True, "cat <<'A'; cat <<'B'\n%s\nA\n%s" % (prose, prose)
    yield "OPEN unquoted unterminated, data command swallowing", True, "echo \"%s\"; cat <<EOF\nx" % UNHIDDEN_ARGS
    # the quoted-delimiter commit idiom stays data, with the same grammar
    yield "KEEP commit idiom", False, COMMIT_HEREDOC % prose
    yield "KEEP commit idiom <<-", False, "git commit -m \"$(cat <<-'EOF'\n\t%s\n\tEOF\n)\"" % prose
    yield "KEEP commit idiom punctuated tag", False, "git commit -m \"$(cat <<'END-MESSAGE'\n%s\nEND-MESSAGE\n)\"" % prose
    yield "KEEP commit idiom, wrong terminator is not the idiom", True, "git commit -m \"$(cat <<'END-MESSAGE'\n%s\nEND\n)\"" % prose
    yield "KEEP commit idiom, trailing-space terminator is not the idiom", True, "git commit -m \"$(cat <<'EOF'\n%s\nEOF \n)\"" % prose
    yield "KEEP commit idiom, unquoted punctuated tag is scanned", True, "git commit -m \"$(cat <<END-MSG\n%s\nEND-MSG\n)\"" % prose
    yield "KEEP commit idiom, redirect on the opener line", True, "git commit -m \"$(cat <<'EOF' > run.ps1\n%s\nEOF\n)\"" % UNHIDDEN_ARGS
    yield "KEEP here-string is not a heredoc", False, "cat <<< \"%s\"" % prose
    yield "KEEP here-string then launch", True, "cat <<< x; %s" % launch
    yield "KEEP no heredoc at all", False, "echo \"%s\"" % prose


class WmiCreateHiddenGuardR6Tests(unittest.TestCase):
    def _verdicts(self, command):
        return (GUARD.decide(bash(command)) is not None, GUARD.decide(ps(command)) is not None)

    def test_h1_repro_and_the_body_belongs_to_the_opener(self):
        # opus r5 H-16 verbatim: the writer's content was judged as the data command's.
        repro = "cat > run.ps1 <<'EOF' && cat run.ps1\n%s\nEOF" % UNHIDDEN_ARGS
        self.assertIsNotNone(GUARD.decide(bash(repro)))
        self.assertIsNotNone(GUARD.decide(bash(repro.replace("&& cat run.ps1", "; echo ok"))))
        self.assertIsNotNone(GUARD.decide(bash("tee run.ps1 <<'EOF' >/dev/null && echo ok\n%s\nEOF" % UNHIDDEN_ARGS)))
        # the segment that opened the heredoc holds its body; the follower on the same line does not
        rows = GUARD.split_shell(repro)
        self.assertIn(UNHIDDEN_ARGS, rows[0][0])
        self.assertNotIn(UNHIDDEN_ARGS, rows[1][0])
        rows = GUARD.split_shell("cat <<'A' && cat > run.ps1 <<'B'\nfirst\nA\nsecond\nB")
        self.assertIn("first", rows[0][0])
        self.assertNotIn("second", rows[0][0])
        self.assertIn("second", rows[1][0])
        # prose held by a data opener is allowed whatever follows on its line
        self.assertIsNone(GUARD.decide(bash("cat <<'EOF'; echo ok\nGuard %s\nEOF" % FORM)))

    def test_h2_repro_a_punctuated_delimiter_is_one_word(self):
        # sol r5 verbatim: END-MESSAGE was cut to END, the terminator was never found and the pwsh line was swallowed.
        launch = 'pwsh -NoProfile -Command "%s"' % UNHIDDEN_ARGS
        repro = "cat <<END-MESSAGE\nmessage\nEND-MESSAGE\n%s" % launch
        self.assertEqual((True, True), self._verdicts(repro))
        for tag in ("EOF.1", "EOF/END", "EOF_1"):
            self.assertEqual((True, True), self._verdicts("cat <<%s\nmessage\n%s\n%s" % (tag, tag, launch)), tag)
        rows = GUARD.split_shell(repro)
        self.assertEqual(["cat <<END-MESSAGE\nmessage\nEND-MESSAGE", launch], [r[0] for r in rows if r[0]])

    def test_unterminated_heredoc_fails_closed_like_an_open_quote(self):
        for command in ("cat <<'EOF'\nGuard %s" % FORM, "cat <<EOF\nGuard %s\nEOF \n" % FORM, "cat <<'EOF' Guard %s" % FORM):
            self.assertIsNone(GUARD.split_shell(command), command)
            self.assertEqual(command, GUARD.code_view(command))
            self.assertEqual((True, True), self._verdicts(command.replace("Guard " + FORM, UNHIDDEN_ARGS)), command)
        self.assertIsNotNone(GUARD.split_shell("cat <<'EOF'\nx\nEOF"))

    def test_heredoc_grammar_table(self):
        rows = list(grammar_rows())
        self.assertGreater(len(rows), 450)
        for spelling, deny, command in rows:
            self.assertEqual((deny, deny), self._verdicts(command), "%s: %r" % (spelling, command))


# r7 (PR #547, sol r6 C1; opus r6 H-17/H-18): comments are closed by grammar, as r6 closed heredocs.
#   * a comment is an unquoted # at the start of a word (start of text, or after unquoted whitespace or ; & | ( )) to the end of its line
#   * the tokenizer removes it before it looks for heredoc openers, quotes or separators; the code before it on its line stays
#   * # mid-word (a#b, ${#x}, $#), inside quotes or after a backslash is not a comment; # in a heredoc body is body text
#   * PowerShell: backtick is the escape, a closing quote, { and , also start a token, <#...#> is a block comment; a # glued to an
#     operator (= > ...) is a comment only in expression mode, so that reading is ambiguous and fails closed like an open quote
COMMENT_OPENERS = ("<<'true'", "<<true", "<<-true", '<<"true"')
COMMENT_PREFIXES = ("echo ready # heredoc example: ", "echo ready\t# ", "echo ready;# ", "echo ready ;# ", "echo ready && # ",
                    "echo ready | # ", "echo ready & # ", "(echo ready) # ", "# ", "echo ready\n# ", "(# ")
COMMENT_QUOTES = (("don't block", "it's detached"), ('say "hi', 'bye" now'), ("a'b", "c'd"))
NOT_COMMENT_WORDS = ("a#b", "${#x}", "$#", "x#", "a\\#b", "a##b", "'# x'", '"# x"', "'a'b#")


def comment_rows():
    """(spelling, expected_deny, command) for the comment grammar, judged the same by the Bash and PowerShell tools."""
    launch = 'pwsh -NoProfile -Command "%s"' % UNHIDDEN_ARGS
    hidden = 'pwsh -NoProfile -Command "%s"' % HIDDEN_ONE_LINE
    prose = "Guard " + FORM
    # C1: a heredoc opener inside a comment opens nothing, wherever the comment starts
    for prefix in COMMENT_PREFIXES:
        for opener in COMMENT_OPENERS:
            close = "\n)" if prefix.startswith("(#") else ""
            yield "C1 %r %s" % (prefix, opener), True, "%s%s\n%s\ntrue%s" % (prefix, opener, launch, close)
    yield "C1 full-line comment holding <<EOF, then a launch", True, "# see <<EOF\n%s\nEOF" % launch
    yield "C1 data line, full-line comment holding <<EOF, then a launch", True, "echo start\n# see <<EOF\n%s\nEOF" % launch
    # opus H-17: a quote inside a comment does not unbalance anything
    for first, second in COMMENT_QUOTES:
        for prefix in ("echo starting # ", "echo starting;# ", "echo starting\t# "):
            yield "QUOTE %r %r around a launch" % (first, second), True, "%s%s\n%s\necho done # %s" % (prefix, first, launch, second)
    yield "QUOTE line-continued comment around a launch", True, "echo a \\\n# don't\n%s\necho b # it's" % launch
    # opus H-18: a comment heredoc does not swallow a real writer heredoc
    yield "H18 comment <<EOF then a writer heredoc", True, "echo start # via <<EOF\ncat > run.ps1 <<EOF\n%s\nEOF\npwsh -File run.ps1" % UNHIDDEN_ARGS
    # not a comment: # mid-word or inside quotes; the heredoc after it is real, so its literal body is data and a launch after it is code
    for word in NOT_COMMENT_WORDS:
        yield "WORD %s <<'EOF', launch in the literal body" % word, False, "echo %s <<'EOF'\n%s\nEOF" % (word, UNHIDDEN_ARGS)
        yield "WORD %s <<'EOF', launch after the terminator" % word, True, "echo %s <<'EOF'\nmessage\nEOF\n%s" % (word, launch)
    for arg in ('"# <<EOF"', "'# <<EOF'", "\"# don't <<EOF\""):
        yield "QUOTED %s argument, then a launch" % arg, True, "echo %s\n%s" % (arg, launch)
    yield "QUOTED search pattern '# <<EOF', then a launch", True, "grep -n '# <<EOF' x.sh\n%s" % launch
    # the comment hides only itself: code before it on its line stays, and comment text is not code
    yield "SAME launch before a comment holding a heredoc opener", True, "%s # don't <<'EOF'" % launch
    yield "SAME launch before ;#", True, "%s;# note" % launch
    yield "SAME launch in a later segment before a comment", True, "echo ok; %s # note" % launch
    yield "TEXT launch text after # is a comment", False, "echo ok # %s" % launch
    yield "TEXT launch text after ;# is a comment", False, "echo ok;# %s" % launch
    yield "TEXT launch text on a full-line comment", False, "# %s" % launch
    yield "TEXT apostrophe in a comment after a heredoc opener", False, "cat <<'EOF' # it's a note\n%s\nEOF" % prose
    # a comment after a hidden launch does not hide a later unhidden one
    yield "HIDDEN comment after a hidden launch, unhidden launch later", True, "%s # hidden, don't worry\necho one\necho two\necho three\n%s" % (hidden, launch)
    yield "HIDDEN comment heredoc after a hidden launch", True, "%s # note <<'X'\necho one\necho two\necho three\n%s\nX" % (hidden, launch)
    yield "HIDDEN hidden launch, data line with a comment heredoc", True, "%s\necho ok # see <<'X'\necho one\necho two\n%s\nX" % (hidden, launch)
    # inside a heredoc body # is body text; the r6 rules still decide literal versus scanned
    yield "BODY # in a literal body", False, "cat <<'EOF'\n# body comment <<X don't\n%s\nEOF" % UNHIDDEN_ARGS
    # (a substitution on the same line after ' #' is the declared space-hash non-promise, WMI-GUARD-EVASIONS-1)
    yield "BODY # line in an unquoted body, the substitution after it still runs", True, "cat <<EOF\n# don't <<X\n$(%s)\nEOF" % launch
    yield "BODY # in a writer body", True, "cat > run.ps1 <<'EOF'\n# comment\n%s\nEOF" % UNHIDDEN_ARGS
    # the quoted-delimiter commit idiom with comment lines stays data
    yield "KEEP commit idiom, # line in the body", False, COMMIT_HEREDOC % ("# comment line\n" + prose)
    yield "KEEP commit idiom, # line with a quote and <<EOF in the body", False, COMMIT_HEREDOC % ("# don't <<EOF it's\n" + prose)
    yield "KEEP commit idiom, trailing comment", False, COMMIT_HEREDOC % prose + " # trailing comment"
    yield "KEEP commit -F idiom, # line in the body", False, "git commit -F - <<'EOF'\n# comment, don't\n%s\nEOF" % prose


class WmiCreateHiddenGuardR7Tests(unittest.TestCase):
    def _verdicts(self, command):
        return (GUARD.decide(bash(command)) is not None, GUARD.decide(ps(command)) is not None)

    def test_c1_repro_a_heredoc_opener_in_a_comment_opens_nothing(self):
        # sol r6 C1 verbatim shape: the commented <<'true' swallowed the launch into the echo data row.
        launch = 'pwsh -NoProfile -Command "%s"' % UNHIDDEN_ARGS
        repro = "echo ready # heredoc example: <<'true'\n%s\ntrue" % launch
        self.assertEqual((True, True), self._verdicts(repro))
        self.assertEqual((True, True), self._verdicts(repro.replace(" # heredoc example: <<'true'", "")))
        rows = GUARD.split_shell(repro)
        self.assertEqual(["echo ready ", launch, "true"], [r[0] for r in rows])

    def test_opus_h17_h18_comment_quotes_and_comment_heredocs(self):
        launch = 'pwsh -NoProfile -Command "%s"' % UNHIDDEN_ARGS
        h17 = "echo starting # don't block\n%s\necho done # it's detached" % launch
        h18 = "echo start # via <<EOF\ncat > run.ps1 <<EOF\n%s\nEOF\npwsh -File run.ps1" % UNHIDDEN_ARGS
        self.assertEqual((True, True), self._verdicts(h17))
        self.assertEqual((True, True), self._verdicts(h18))
        # comment text is removed, the newline that ends its line is kept
        self.assertEqual([["pwsh -File x.ps1 ", "\n", False], ["ls", "", False]], GUARD.split_shell("pwsh -File x.ps1 # don't\nls"))

    def test_comment_grammar_table(self):
        rows = list(comment_rows())
        self.assertGreater(len(rows), 90)
        for spelling, deny, command in rows:
            self.assertEqual((deny, deny), self._verdicts(command), "%s: %r" % (spelling, command))

    def test_each_shell_reads_its_own_escapes_and_token_starts(self):
        # (spelling, bash_deny, powershell_deny, command): bash escapes with a backslash, PowerShell with a backtick; PowerShell
        # also starts a token after a closing quote; a bash comment inside a backtick substitution ends at the closing backtick.
        launch = 'pwsh -NoProfile -Command "%s"' % UNHIDDEN_ARGS
        for spelling, bash_deny, ps_deny, command in (
            ("backslash-space keeps # mid-word in bash only", False, True, "echo a\\ #<<'EOF'\n%s\nEOF" % UNHIDDEN_ARGS),
            ("an escaped backslash leaves the space real", True, True, "echo a\\\\ #<<'EOF'\n%s\nEOF" % UNHIDDEN_ARGS),
            # bash glues a# across the backslash-newline, so # is mid-word; the segmenter still cuts at that newline
            # (WMI-GUARD-LINE-CONTINUATION-1 debt), so the bash verdict stays on the conservative side
            ("backslash-newline glues # to the word in bash", True, True, "echo a\\\n# <<'EOF'\n%s\nEOF" % launch),
            ("# after a closing double quote: PowerShell comment", False, True, "echo \"a\"#<<'EOF'\n%s\nEOF" % UNHIDDEN_ARGS),
            ("# after a closing single quote: PowerShell comment", False, True, "echo 'a'#<<'EOF'\n%s\nEOF" % UNHIDDEN_ARGS),
            # bash reads "a"# as one word, so its quote really runs to it's and the launch is quoted text
            ("PowerShell closing-quote comments around a launch", False, True,"echo \"a\"# don't\n%s\necho \"b\"# it's" % launch),
            ("bash comment in backticks ends at the backtick", True, False, "echo `echo a #c` ; %s" % launch),
            ("a backtick in a top-level comment opens nothing", True, False, "echo ok # a `tick\necho `echo b #c` ; %s" % launch),
            # bash starts a word after < or >, so <# is a comment there (a syntax error); in PowerShell it opens an unclosed block
            ("<# holding a heredoc opener: a bash comment, an unclosed PowerShell block", True, True, "echo ready <# <<'true'\n%s\ntrue" % launch),
            ("# glued to = : ambiguous in PowerShell, fails closed", False, True,"echo ok =#<<'EOF'\nGuard %s\nEOF" % FORM),
        ):
            self.assertEqual((bash_deny, ps_deny), self._verdicts(command), "%s: %r" % (spelling, command))

    def test_powershell_block_comment(self):
        launch = 'pwsh -NoProfile -Command "%s"' % UNHIDDEN_ARGS
        for spelling, deny, command in (
            ("a heredoc opener in a block comment opens nothing", True, "echo ready <# heredoc <<'true' #>\n%s\ntrue" % launch),
            ("quotes in block comments do not pair", True, "echo ready <# don't\n#>\n%s\necho done <# it's #>" % launch),
            ("launch text in a one-line block comment", False, "echo ok <# %s #>" % launch),
            ("launch text in a multi-line block comment", False, "echo ok <#\n%s\n#>" % launch),
            ("an unclosed block comment fails closed", True, "echo ok <# never closed\n%s" % launch),
            ("<# mid-word is not a block comment", True, "echo a<#b\n%s\n#>" % launch),
        ):
            self.assertEqual(deny, GUARD.decide(ps(command)) is not None, "%s: %r" % (spelling, command))
        # a multi-line block comment keeps its newlines, so line numbers still line up
        self.assertIn("line 4", GUARD.decide(ps("echo ok <# a\nb\n#>\n%s" % launch)))


if __name__ == "__main__":
    unittest.main()
