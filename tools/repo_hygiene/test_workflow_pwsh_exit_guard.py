"""Pin: a multi-command pwsh ``run:`` block must fail closed on its first failing command.

GitHub's ``pwsh`` step wrapper ends the script with ``exit $LASTEXITCODE`` (default and
``shell: pwsh``), and a custom ``pwsh ... -Command ". '{0}'"`` shell ends with the status
of the LAST statement. Either way a failing native command followed by a passing one is
reported as a green step on Windows, while ubuntu's ``bash -e`` fails it. Tests gated to
Windows (``skipUnless(os.name == "nt")``) therefore only ran where their failures were
masked. See CI-HYGIENE-STEP-EXIT-MASK-1.

The check is textual (no YAML dependency in the hygiene suite): every native-command
statement in a pwsh-executed block must be guarded by the fail-closed prologue

    $ErrorActionPreference = 'Stop'
    $PSNativeCommandUseErrorActionPreference = $true

either placed before the first native command in the block or carried by the step's
(or job's) ``shell:`` template, or be followed directly by a ``$LASTEXITCODE`` check, or
be the final statement of the block (its status is then the step status).

The workflows use the ``shell:`` route: a project hook (NA-6) reads any edit to a ``run:``
body as a removed test step, and the aqt retry blocks must keep seeing a failing native
command so they can retry, so those steps keep the default shell and no ``run:`` body changed.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
WORKFLOW_DIR = ROOT / ".github" / "workflows"

_NATIVE_STATEMENT = re.compile(
    r"^\s*(?:\$[\w:]+\s*=\s*\(?\s*)?"
    r"(?:&\s+\S|(?:python3?|py|pip|git|choco|pwsh|powershell)\s|\.\\\S)"
)
_PROLOGUE_EAP = re.compile(r"^\s*\$ErrorActionPreference\s*=\s*['\"]Stop['\"]\s*$", re.IGNORECASE)
_PROLOGUE_NATIVE = re.compile(
    r"^\s*\$PSNativeCommandUseErrorActionPreference\s*=\s*\$true\s*$", re.IGNORECASE
)


def _shell_carries_prologue(shell: str | None) -> bool:
    """True when a pwsh ``shell:`` template sets both guard preferences before running the script."""
    if not shell or not _is_pwsh(shell, False):
        return False
    template = shell.strip()
    eap = re.search(r"\$ErrorActionPreference\s*=\s*'Stop'", template, re.IGNORECASE)
    native = re.search(r"\$PSNativeCommandUseErrorActionPreference\s*=\s*\$true", template, re.IGNORECASE)
    script = template.find("{0}")
    return bool(eap and native and script != -1 and eap.start() < script and native.start() < script)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _is_pwsh(shell: str | None, windows_job: bool) -> bool:
    if shell is None:
        return windows_job
    return shell.strip().lower().startswith(("pwsh", "powershell"))


def _logical_lines(body: list[str]) -> list[tuple[int, str]]:
    """Comment-free logical statements as (physical index, text); backtick lines are joined."""
    logical: list[tuple[int, str]] = []
    pending: tuple[int, str] | None = None
    for index, raw in enumerate(body):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if pending is not None:
            joined = pending[1] + " " + line.strip()
        else:
            joined = line
        if joined.endswith("`"):
            pending = (pending[0] if pending is not None else index, joined[:-1])
            continue
        logical.append((pending[0] if pending is not None else index, joined))
        pending = None
    if pending is not None:
        logical.append(pending)
    return logical


def unguarded_native_statements(body: list[str]) -> list[str]:
    logical = _logical_lines(body)
    native_positions = [i for i, (_, text) in enumerate(logical) if _NATIVE_STATEMENT.match(text)]
    if not native_positions:
        return []
    first_native = native_positions[0]
    eap_before = any(_PROLOGUE_EAP.match(text) for _, text in logical[:first_native])
    native_before = any(_PROLOGUE_NATIVE.match(text) for _, text in logical[:first_native])
    if eap_before and native_before:
        return []
    offenders = []
    for position in native_positions:
        text = logical[position][1]
        is_final = position == len(logical) - 1
        checked = "$LASTEXITCODE" in text or (
            position + 1 < len(logical) and "$LASTEXITCODE" in logical[position + 1][1]
        )
        if not (is_final or checked):
            offenders.append(text.strip())
    return offenders


def _job_blocks(text: str) -> list[tuple[str, list[str]]]:
    lines = text.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.rstrip() == "jobs:")
    except StopIteration:
        return []
    blocks: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    for line in lines[start + 1 :]:
        match = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if match:
            current = (match.group(1), [])
            blocks.append(current)
        elif current is not None:
            current[1].append(line)
    return blocks


def _job_is_windows(job_lines: list[str]) -> bool:
    for line in job_lines:
        stripped = line.strip()
        if stripped.startswith("runs-on:") and "windows" in stripped.lower():
            return True
        if stripped.startswith("os:") and "windows" in stripped.lower():
            return True
    return False


def _job_default_shell(job_lines: list[str]) -> str | None:
    in_defaults = False
    for line in job_lines:
        if line.startswith("    steps:"):
            return None
        if line.rstrip() == "    defaults:":
            in_defaults = True
        elif in_defaults:
            match = re.match(r"^\s{8}shell:\s*(.+?)\s*$", line)
            if match:
                return match.group(1)
    return None


def _steps(job_lines: list[str]) -> list[list[str]]:
    try:
        start = next(i for i, line in enumerate(job_lines) if line.rstrip() == "    steps:")
    except StopIteration:
        return []
    steps: list[list[str]] = []
    step_indent: int | None = None
    for line in job_lines[start + 1 :]:
        stripped = line.lstrip(" ")
        if stripped.startswith("- ") and (step_indent is None or _indent(line) == step_indent):
            step_indent = _indent(line)
            steps.append([line])
        elif steps:
            steps[-1].append(line)
    return steps


def _step_run_and_shell(step: list[str]) -> tuple[str | None, list[str], str | None]:
    """(step name, run body lines, step-level shell). Inline ``run: cmd`` is one command."""
    name = None
    shell = None
    body: list[str] = []
    key_indent = _indent(step[0]) + 2
    index = 0
    while index < len(step):
        line = step[index]
        stripped = line.strip()
        if stripped.startswith("- "):
            stripped = stripped[2:]
        if _indent(line) in (key_indent, key_indent - 2):
            if stripped.startswith("name:"):
                name = stripped[5:].strip()
            elif stripped.startswith("shell:"):
                shell = stripped[6:].strip()
            elif stripped.startswith("run:"):
                value = stripped[4:].strip()
                if value in ("|", "|-", "|+", ">", ">-"):
                    index += 1
                    while index < len(step) and (
                        not step[index].strip() or _indent(step[index]) > key_indent
                    ):
                        body.append(step[index])
                        index += 1
                    continue
                body = [value]
        index += 1
    return name, body, shell


def audit_workflow_text(text: str) -> list[tuple[str, str, str]]:
    """Return (job, step, statement) for every unguarded native command under pwsh."""
    findings: list[tuple[str, str, str]] = []
    for job, job_lines in _job_blocks(text):
        windows_job = _job_is_windows(job_lines)
        default_shell = _job_default_shell(job_lines)
        for step in _steps(job_lines):
            name, body, step_shell = _step_run_and_shell(step)
            if not body:
                continue
            shell = step_shell if step_shell is not None else default_shell
            if not _is_pwsh(shell, windows_job) or _shell_carries_prologue(shell):
                continue
            for statement in unguarded_native_statements(body):
                findings.append((job, name or "<unnamed>", statement))
    return findings


PROLOGUE = "$ErrorActionPreference = 'Stop'\n$PSNativeCommandUseErrorActionPreference = $true\n"


def _synthetic(run_body: str, *, runs_on: str = "windows-latest", shell: str | None = None) -> str:
    step_shell = f"        shell: {shell}\n" if shell else ""
    indented = "".join(f"          {line}\n" for line in run_body.strip("\n").splitlines())
    return (
        "name: Synthetic\n"
        "jobs:\n"
        "  probe:\n"
        f"    runs-on: {runs_on}\n"
        "    steps:\n"
        "      - name: Probe step\n"
        f"{step_shell}"
        "        run: |\n"
        f"{indented}"
    )


class WorkflowPwshExitGuardTests(unittest.TestCase):
    def test_every_multi_command_pwsh_run_block_fails_closed(self) -> None:
        workflows = sorted(WORKFLOW_DIR.glob("*.yml"))
        self.assertTrue(workflows, "no workflows discovered")
        findings = []
        for workflow in workflows:
            for job, step, statement in audit_workflow_text(workflow.read_text(encoding="utf-8")):
                findings.append(f"{workflow.name} :: {job} :: {step} :: {statement}")
        self.assertEqual(
            [],
            findings,
            "pwsh reports only the LAST command's status: add the fail-closed prologue "
            "($ErrorActionPreference = 'Stop'; $PSNativeCommandUseErrorActionPreference = $true) "
            "before the first native command, or check $LASTEXITCODE after it",
        )

    def test_the_audit_sees_the_pwsh_blocks_it_is_meant_to_police(self) -> None:
        # Guards the guard: a parser regression that silently sees zero blocks must not pass.
        seen = 0
        for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
            text = workflow.read_text(encoding="utf-8")
            for _, job_lines in _job_blocks(text):
                windows_job = _job_is_windows(job_lines)
                default_shell = _job_default_shell(job_lines)
                for step in _steps(job_lines):
                    _, body, step_shell = _step_run_and_shell(step)
                    shell = step_shell if step_shell is not None else default_shell
                    if body and _is_pwsh(shell, windows_job) and _logical_lines(body):
                        seen += 1
        self.assertGreaterEqual(seen, 40)

    def test_audit_flags_the_masking_shape_and_accepts_the_guards(self) -> None:
        masked = "python -m unittest a\npython -m unittest b\n"
        self.assertEqual(1, len(audit_workflow_text(_synthetic(masked))), "first command masked")
        self.assertEqual(
            1,
            len(audit_workflow_text(_synthetic(masked, runs_on="ubuntu-latest", shell="pwsh"))),
            "explicit shell: pwsh is policed on any OS",
        )
        self.assertEqual(
            1,
            len(audit_workflow_text(_synthetic(masked, shell="pwsh -NoLogo -Command \". '{0}'\""))),
            "custom pwsh shell templates are policed",
        )
        self.assertEqual([], audit_workflow_text(_synthetic(PROLOGUE + masked)))
        self.assertEqual(
            [],
            audit_workflow_text(
                _synthetic(
                    "python -m unittest a\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
                    "python -m unittest b\n"
                )
            ),
            "explicit per-command exit checks are an accepted alternative",
        )
        self.assertEqual([], audit_workflow_text(_synthetic("python -m unittest a\n")))
        self.assertEqual([], audit_workflow_text(_synthetic(masked, runs_on="ubuntu-latest")), "bash -e")
        self.assertEqual([], audit_workflow_text(_synthetic(masked, shell="bash")))

    def test_audit_accepts_only_a_complete_guarded_shell_template(self) -> None:
        masked = "python -m unittest a\npython -m unittest b\n"
        base = "pwsh -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command"
        guard = "$ErrorActionPreference = 'Stop'; $PSNativeCommandUseErrorActionPreference = $true; "
        self.assertEqual(
            [],
            audit_workflow_text(_synthetic(masked, shell=f"{base} \"{guard}. '{{0}}'\"")),
        )
        weakened = (
            f"{base} \". '{{0}}'\"",
            f"{base} \"$ErrorActionPreference = 'Stop'; . '{{0}}'\"",
            f"{base} \"$PSNativeCommandUseErrorActionPreference = $true; . '{{0}}'\"",
            f"{base} \". '{{0}}'; {guard}\"",
            f"{base} \"$ErrorActionPreference = 'Continue'; "
            f"$PSNativeCommandUseErrorActionPreference = $true; . '{{0}}'\"",
            f"{base} \"$ErrorActionPreference = 'Stop'; "
            f"$PSNativeCommandUseErrorActionPreference = $false; . '{{0}}'\"",
        )
        for shell in weakened:
            with self.subTest(shell=shell):
                self.assertNotEqual([], audit_workflow_text(_synthetic(masked, shell=shell)))

    def test_audit_rejects_weakened_or_misplaced_prologues(self) -> None:
        masked = "python -m unittest a\npython -m unittest b\n"
        weakened = (
            "$ErrorActionPreference = 'Stop'\n" + masked,
            "$PSNativeCommandUseErrorActionPreference = $true\n" + masked,
            "# $ErrorActionPreference = 'Stop'\n# $PSNativeCommandUseErrorActionPreference = $true\n"
            + masked,
            "$ErrorActionPreference = 'Continue'\n"
            "$PSNativeCommandUseErrorActionPreference = $true\n" + masked,
            "$ErrorActionPreference = 'Stop'\n"
            "$PSNativeCommandUseErrorActionPreference = $false\n" + masked,
            "python -m unittest a\n" + PROLOGUE + "python -m unittest b\n",
        )
        for body in weakened:
            with self.subTest(body=body):
                self.assertNotEqual([], audit_workflow_text(_synthetic(body)))

    def test_audit_polices_the_matrix_windows_default_shell(self) -> None:
        matrix = (
            "name: Synthetic\n"
            "jobs:\n"
            "  probe:\n"
            "    runs-on: ${{ matrix.os }}\n"
            "    strategy:\n"
            "      matrix:\n"
            "        os: [windows-latest, ubuntu-latest]\n"
            "    steps:\n"
            "      - name: Matrix step\n"
            "        run: |\n"
            "          python -m unittest a\n"
            "          python -m unittest b\n"
        )
        self.assertEqual(1, len(audit_workflow_text(matrix)))
        self.assertEqual([], audit_workflow_text(matrix.replace("windows-latest, ", "")))

    def test_audit_honours_job_default_shell(self) -> None:
        text = (
            "name: Synthetic\n"
            "jobs:\n"
            "  probe:\n"
            "    runs-on: windows-latest\n"
            "    defaults:\n"
            "      run:\n"
            "        shell: pwsh -NoLogo -NoProfile -Command \". '{0}'\"\n"
            "    steps:\n"
            "      - name: Build\n"
            "        run: |\n"
            "          & qmake.exe a.pro\n"
            "          & mingw32-make.exe -j2\n"
            "          Pop-Location\n"
        )
        self.assertEqual(2, len(audit_workflow_text(text)))


if __name__ == "__main__":
    unittest.main()
