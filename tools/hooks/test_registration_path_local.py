"""LOCAL registration-path gate for the MLV-App project PreToolUse hook.

WHY THIS FILE IS NOT UNDER ``tools/repo_hygiene/``.  The repo-hygiene job collects
``tools/repo_hygiene/test_*.py`` by discovery and runs it on ``windows-latest`` and
``ubuntu-latest``.  The command registered in ``.claude/settings.json`` names a PER-USER
ABSOLUTE INTERPRETER that exists on no hosted runner (O81), so a case that executes it
would be red in CI and green locally -- the exact split this repo already paid for.
Hosted CI therefore proves the RULES (``tools/repo_hygiene/test_mlv_never_authorized.py``);
this file proves the REGISTRATION, and the hub runs it on the branch and again after 0.1's
``.claude/settings.json`` conflict resolution.

    python -m unittest tools.hooks.test_registration_path_local -v

A hook is (interpreter x script x REGISTRATION).  Hashing the script proves only the middle
term; this file executes the registered command string VERBATIM -- the same string the
harness runs, with only ``${CLAUDE_PROJECT_DIR}`` expanded, exactly as the harness expands
it -- from a disposable worktree-like directory, and asserts exit 0 on a benign payload and
exit 2 on a harmless one.  ``py -3`` hooks on this machine silently never executed until
2026-08-09; a hook that has never run looks exactly like a hook that always passes.

O126 -- AND SINCE THE FIFTH DELTA THE REGISTRATION CARRIES THE VENUE.  The command string
now ends ``--project-dir "${CLAUDE_PROJECT_DIR}"``, and three rules key on that argument
(NA-2 exception (iv), the S105 enable act, NA-10).  The previous revision read the
ENVIRONMENT variable of that name, which is absent from real hook processes, so the venue
test never fired.  This file is therefore the only place the ARGUMENT itself is proven to
travel: ``test_registered_command_carries_the_venue_argument`` sends an NA-10 payload whose
verdict is decided by nothing else, with the venue variable STRIPPED from the environment,
so a registration that lost the argument goes red here.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SETTINGS = os.path.join(REPO_ROOT, ".claude", "settings.json")

EXPECTED_MATCHER = "Bash|PowerShell|Write|Edit|NotebookEdit"
EXPECTED_COMMAND = (
    '"C:/Users/obabalola/AppData/Local/Python/bin/python.exe" '
    '"${CLAUDE_PROJECT_DIR}/tools/hooks/mlv-never-authorized.py" '
    '--project-dir "${CLAUDE_PROJECT_DIR}"'
)

ALLOW_PAYLOAD = {"tool_name": "Bash", "tool_input": {"command": "git status --porcelain"}}
# The INERT known-DENY payload named by the plan: it exercises the ANTHROPIC_ prefix rule,
# names no real credential, and the hook only READS it -- nothing is ever executed.
DENY_PAYLOAD = {
    "tool_name": "PowerShell",
    "tool_input": {"command": "if ($false) { setx ANTHROPIC_PROBE_TOKEN x }"},
}
# The VENUE payload (O126/NA-10).  A `Write` of THIS repo's own copy of the hook script.
# Its verdict depends on exactly one thing: whether `--project-dir` reached the process.
# The registration expands the argument to REPO_ROOT, which is not the board root, so NA-10
# fires; a registration that dropped the argument would also deny -- but with no argument
# there is no venue to be wrong about, and the assertion below pins the reason line to
# NA-10 either way, so the row that actually proves the wiring is the ALLOW one below it,
# which supplies the board root by hand and must then be permitted.
VENUE_DENY_PAYLOAD = {
    "tool_name": "Write",
    "tool_input": {
        "file_path": os.path.join(REPO_ROOT, "tools", "hooks", "mlv-never-authorized.py"),
        "content": "# a lane rewriting its own gate\n",
    },
}


def registered_pretooluse_entries():
    with open(SETTINGS, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    return document.get("hooks", {}).get("PreToolUse", [])


class RegistrationPathTests(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.entries = registered_pretooluse_entries()
        cls.command = None
        for block in cls.entries:
            if not isinstance(block, dict) or block.get("matcher") != EXPECTED_MATCHER:
                continue
            for hook in block.get("hooks", []):
                if (
                    isinstance(hook, dict)
                    and hook.get("type") == "command"
                    and "mlv-never-authorized.py" in str(hook.get("command", ""))
                ):
                    cls.command = hook["command"]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlv-reg-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _execute(self, payload, extra_env=None):
        """Run the REGISTERED command string verbatim, from a disposable directory.

        The venue variable is STRIPPED, not set (O126).  The hook no longer reads it, and
        leaving it in the environment would let a registration that lost ``--project-dir``
        still look wired.  Everything the venue rules see must arrive on the command line.
        """
        self.assertIsNotNone(self.command, "no PreToolUse registration to execute")
        command = self.command.replace("${CLAUDE_PROJECT_DIR}", REPO_ROOT)
        command = command.replace("$CLAUDE_PROJECT_DIR", REPO_ROOT)
        env = dict(os.environ)
        for key in list(env):
            if key.startswith("MLV_"):
                del env[key]
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(extra_env or {})
        return subprocess.run(
            command,
            shell=True,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.tmp,
            env=env,
        )

    def test_pretooluse_entry_is_registered_exactly(self):
        """Matcher, type and command are all EXACT and case-sensitive -- never a substring."""
        self.assertTrue(self.entries, "settings.json declares no PreToolUse hooks")
        matched = [
            hook
            for block in self.entries
            if isinstance(block, dict) and block.get("matcher") == EXPECTED_MATCHER
            for hook in block.get("hooks", [])
            if isinstance(hook, dict)
            and hook.get("type") == "command"
            and hook.get("command") == EXPECTED_COMMAND
        ]
        self.assertEqual(len(matched), 1, "expected exactly one exact PreToolUse registration")

    def test_registered_interpreter_exists(self):
        """A portable-but-wrong command fails OPEN; that is what cost two days here."""
        interpreter = EXPECTED_COMMAND.split('" "')[0].lstrip('"')
        self.assertTrue(os.path.isfile(interpreter), interpreter)

    def test_registered_command_allows_a_benign_payload(self):
        completed = self._execute(ALLOW_PAYLOAD)
        self.assertEqual(
            completed.returncode,
            0,
            "expected ALLOW; stdout=%r stderr=%r" % (completed.stdout, completed.stderr),
        )
        self.assertEqual(completed.stderr.strip(), "")

    def test_registered_command_denies_a_harmless_payload(self):
        completed = self._execute(DENY_PAYLOAD)
        self.assertEqual(
            completed.returncode,
            2,
            "expected DENY; stdout=%r stderr=%r" % (completed.stdout, completed.stderr),
        )
        lines = [line for line in completed.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "DENY must print exactly ONE line: %r" % lines)
        self.assertTrue(lines[0].startswith("NA-3:"), lines[0])

    def test_registered_command_carries_the_venue_argument(self):
        """O126: the VENUE must arrive on the COMMAND LINE, and this proves it does.

        Both halves run the same NA-10 payload through the same registered command with the
        venue variable stripped from the environment.  They differ in exactly one thing --
        which directory ``MLV_BOARD_ROOT`` names -- and the hook compares that against the
        ``--project-dir`` the REGISTRATION supplied.  The ALLOW half is the falsifier: it can
        only pass if the argument reached the process, so a registration that dropped it
        goes red here rather than silently reverting to "no venue, every exception shut".
        """
        away = self._execute(VENUE_DENY_PAYLOAD)
        self.assertEqual(
            away.returncode,
            2,
            "expected NA-10 DENY away from the board venue; stdout=%r stderr=%r"
            % (away.stdout, away.stderr),
        )
        lines = [line for line in away.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "DENY must print exactly ONE line: %r" % lines)
        self.assertTrue(lines[0].startswith("NA-10:"), lines[0])

        at_board = self._execute(VENUE_DENY_PAYLOAD, {"MLV_BOARD_ROOT": REPO_ROOT})
        self.assertEqual(
            at_board.returncode,
            0,
            "the registered --project-dir did not reach the hook: with MLV_BOARD_ROOT set "
            "to the very directory the registration substitutes, NA-10 must ALLOW; "
            "stdout=%r stderr=%r" % (at_board.stdout, at_board.stderr),
        )
        self.assertEqual(at_board.stderr.strip(), "")

    def test_registered_command_refuses_an_unknown_argument(self):
        """An argument this hook does not understand is a hook-error, never a silent pass."""
        self.assertIsNotNone(self.command, "no PreToolUse registration to execute")
        command = self.command.replace("${CLAUDE_PROJECT_DIR}", REPO_ROOT)
        command = command.replace("$CLAUDE_PROJECT_DIR", REPO_ROOT) + " --board-dir x"
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        completed = subprocess.run(
            command,
            shell=True,
            input=json.dumps(ALLOW_PAYLOAD),
            capture_output=True,
            text=True,
            encoding="utf-8",
            cwd=self.tmp,
            env=env,
        )
        self.assertEqual(completed.returncode, 2, completed.stderr)
        lines = [line for line in completed.stderr.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, "DENY must print exactly ONE line: %r" % lines)
        self.assertTrue(lines[0].startswith("hook-error:"), lines[0])


WMI_GUARD_SCRIPT = "wmi-create-hidden-guard.py"
WMI_EXPECTED_COMMAND = (
    '"C:/Users/obabalola/AppData/Local/Python/bin/python.exe" '
    '"${CLAUDE_PROJECT_DIR}/tools/hooks/wmi-create-hidden-guard.py"'
)
WMI_ALLOW_PAYLOAD = {"tool_name": "Bash", "tool_input": {"command": "git status --porcelain"}}
# The inline shape seen on the owner's desktop (WMI-CREATE-HIDDEN-GUARD-1); the hook only READS it.
WMI_DENY_PAYLOAD = {
    "tool_name": "Bash",
    "tool_input": {
        "command": "pwsh -NoProfile -Command \"Invoke-CimMethod -ClassName Win32_Process "
        "-MethodName Create -Arguments @{CommandLine='cmd.exe'}\""
    },
}


class WmiGuardRegistrationTests(unittest.TestCase):
    """WMI-CREATE-HIDDEN-GUARD-HOOK-1 r2: the SECOND PreToolUse entry, executed verbatim.

    A wrong script path makes CPython exit 2 ("can't open file"), which denies EVERY matched
    tool call, so a mistyped registration wedges the session rather than failing open.
    """

    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.commands = [
            hook["command"]
            for block in registered_pretooluse_entries()
            if isinstance(block, dict) and block.get("matcher") == EXPECTED_MATCHER
            for hook in block.get("hooks", [])
            if isinstance(hook, dict)
            and hook.get("type") == "command"
            and WMI_GUARD_SCRIPT in str(hook.get("command", ""))
        ]

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mlv-reg-wmi-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _execute(self, command, payload):
        command = command.replace("${CLAUDE_PROJECT_DIR}", REPO_ROOT).replace("$CLAUDE_PROJECT_DIR", REPO_ROOT)
        env = dict(os.environ)
        env.pop("CLAUDE_PROJECT_DIR", None)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            command, shell=True, input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", cwd=self.tmp, env=env,
        )

    def _registered(self):
        self.assertEqual(self.commands, [WMI_EXPECTED_COMMAND], "expected exactly one exact wmi guard registration")
        return self.commands[0]

    def test_registered_command_allows_a_benign_payload(self):
        completed = self._execute(self._registered(), WMI_ALLOW_PAYLOAD)
        self.assertEqual(completed.returncode, 0, "stdout=%r stderr=%r" % (completed.stdout, completed.stderr))
        self.assertEqual(completed.stderr.strip(), "")

    def test_registered_command_denies_an_unhidden_launch(self):
        completed = self._execute(self._registered(), WMI_DENY_PAYLOAD)
        self.assertEqual(completed.returncode, 2, "stdout=%r stderr=%r" % (completed.stdout, completed.stderr))
        self.assertIn("WMI-CREATE-HIDDEN-GUARD", completed.stderr)

    def test_a_mistyped_registration_fails_the_allow_proof(self):
        """The falsifier: the same ALLOW payload against a missing script must NOT exit 0."""
        mutated = self._registered().replace(WMI_GUARD_SCRIPT, "wmi-create-hidden-guard-MISSING.py")
        completed = self._execute(mutated, WMI_ALLOW_PAYLOAD)
        self.assertNotEqual(completed.returncode, 0, "a missing script must not look like an ALLOW")


if __name__ == "__main__":
    unittest.main()
