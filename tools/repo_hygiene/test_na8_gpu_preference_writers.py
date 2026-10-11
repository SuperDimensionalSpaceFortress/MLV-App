"""Pin NA-8 for venue-executed scripts: no tracked tool under ``tools/`` writes the per-app GPU-preference key.

``docs/never-authorized.json`` NA-8 says changing the owner's graphics/power machine state (the
DirectX per-app GPU-preference registry key, power plans) is never authorized for agents. The project
hook (``tools/hooks/mlv-never-authorized.py``) blocks that in an agent's own local commands, but a
script an agent commits and a VENUE then executes bypasses the hook: ``playback-attr-3-cuda-job.ps1``
wrote one value per staged exe path on every job (about 710 values accumulated, none pruned). This
test closes that gap at the repository: it fails when any tracked file under ``tools/`` carries a
registry-WRITE verb aimed at that key.

WHAT COUNTS AS "AIMED AT THE KEY" -- three passes over each file's logical lines (a trailing
backtick or backslash joins the next line):

1. LINE: one logical line names the key (or a variable assigned a string that names it) AND a write
   verb (``Set-ItemProperty``, ``New-ItemProperty``, ``New-Item``, ``Remove-ItemProperty``,
   ``reg add|delete|import``, ``SetValue``, ...).
2. FILE: the file names the key (or a ``GpuPreference=N;`` value) AND contains a strict
   value-setting verb (``Set-ItemProperty``, ``New-ItemProperty``, ``reg add|import``, ``SetValue``,
   ``RegSetValueEx``, ...) ANYWHERE. This is the pass that catches ``$k = '<key>'`` on one line and
   ``New-ItemProperty -Path $k`` ten lines later, and ``reg add`` of a bare ``GpuPreference=2;``.
3. VALUE: a ``GpuPreference=N;`` literal on a logical line with a registry-write verb.

Reading (``Get-ItemProperty``, ``reg query``, ``Test-Path``) is allowed everywhere: the venue scripts
may still observe the key. The ONE allowlisted file is the OWNER-RUN prune script, and only for
REMOVAL verbs -- a value-setting verb in it is still a violation.

The detector is itself falsifiable: ``DetectorTable`` feeds it positive and negative fixtures, so a
regex that rots to "matches nothing" turns red here instead of letting the tree test pass vacuously.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

KEY_RE = re.compile(r"UserGpuPreferences", re.IGNORECASE)
VALUE_RE = re.compile(r"GpuPreference\s*=\s*\d", re.IGNORECASE)

# Verbs that SET a value (or create the key to set one). Never allowed, anywhere, in a file that names the key.
SET_VERBS = (
    r"\bSet-ItemProperty\b",
    r"\bNew-ItemProperty\b",
    r"\bCopy-ItemProperty\b",
    r"\bMove-ItemProperty\b",
    r"\bRename-ItemProperty\b",
    r"\breg(?:\.exe)?\s+(?:add|import|copy|restore|load)\b",
    r"\bregedit(?:\.exe)?\b",
    r"\bSetValue\s*\(",
    r"\bSetValueEx\w*\b",
    r"\bRegSetValue\w*\b",
    r"\bRegSetKeyValue\w*\b",
    r"\bRegCreateKey\w*\b",
    r"\bwinreg\.CreateKey\w*\b",
)
# Verbs that REMOVE or create state. A violation on a line that names the key; allowed only for the prune script.
REMOVE_VERBS = (
    r"\bRemove-ItemProperty\b",
    r"\bClear-ItemProperty\b",
    r"\bRemove-Item\b",
    r"\bSet-Item\b",
    r"\bNew-Item\b",
    r"\breg(?:\.exe)?\s+delete\b",
    r"\bDeleteValue\s*\(",
    r"\bDeleteSubKey\w*\s*\(",
    r"\bCreateSubKey\s*\(",
    r"\bRegDeleteValue\w*\b",
    r"\bRegDeleteKey\w*\b",
    r"\bwinreg\.DeleteValue\b",
)
SET_RE = re.compile("|".join(SET_VERBS), re.IGNORECASE)
REMOVE_RE = re.compile("|".join(REMOVE_VERBS), re.IGNORECASE)
ANY_WRITE_RE = re.compile("|".join(SET_VERBS + REMOVE_VERBS), re.IGNORECASE)
VAR_ASSIGN_RE = re.compile(r"\$(?:script:|global:)?(\w+)\s*=\s*(.*)$")

# path -> reason. The only file that may carry a REMOVAL verb aimed at the key.
REMOVAL_ALLOWLIST: dict[str, str] = {
    "tools/profiling/venue/Prune-MlvGpuPreferences.ps1": (
        "OWNER-RUN prune script: removes stale per-build values, never sets one; no agent or lane "
        "executes it (NA-8). Value-setting verbs stay forbidden in it."
    ),
}

# Files that name the key as DATA about the rule, not as a writer.
EXCLUDED = {
    "tools/hooks/mlv-never-authorized.py": "the hook that enforces NA-8; its regexes name the verbs and the key",
    "tools/repo_hygiene/test_na8_gpu_preference_writers.py": "this test",
    "tools/repo_hygiene/test_mlv_never_authorized.py": "the hook's falsifier table",
    "tools/repo_hygiene/test_prune_mlv_gpu_preferences.py": "the prune script's offline selection test",
}

MAX_BYTES = 2_000_000


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """Join backtick / backslash continuations; return (first physical line number, joined text)."""
    out: list[tuple[int, str]] = []
    buf: list[str] = []
    start = 0
    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.rstrip()
        if not buf:
            start = number
        if stripped.endswith("`") or stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(stripped)
        out.append((start, " ".join(buf)))
        buf = []
    if buf:
        out.append((start, " ".join(buf)))
    return out


def find_violations(text: str, *, allow_removal: bool = False) -> list[tuple[int, str, str]]:
    """Return (line, pass-name, snippet) for every registry-write aimed at the GPU-preference key in ``text``."""
    lines = _logical_lines(text)
    names_key = bool(KEY_RE.search(text))
    names_value = bool(VALUE_RE.search(text))
    tainted: set[str] = set()
    for _, line in lines:
        match = VAR_ASSIGN_RE.search(line)
        if match and KEY_RE.search(match.group(2)):
            tainted.add(match.group(1).lower())

    def references_key(line: str) -> bool:
        if KEY_RE.search(line):
            return True
        return any(re.search(r"\$(?:script:|global:)?" + re.escape(name) + r"\b", line, re.IGNORECASE) for name in tainted)

    found: list[tuple[int, str, str]] = []
    seen: set[tuple[int, str]] = set()

    def add(number: int, name: str, line: str) -> None:
        if (number, name) not in seen:
            seen.add((number, name))
            found.append((number, name, line.strip()[:160]))

    for number, line in lines:
        if line.lstrip().startswith("#") or line.lstrip().startswith("//"):
            continue
        verbs = SET_RE if allow_removal else ANY_WRITE_RE
        if references_key(line) and verbs.search(line):
            add(number, "line", line)
        if VALUE_RE.search(line) and verbs.search(line):
            add(number, "value", line)
        if (names_key or names_value) and SET_RE.search(line):
            add(number, "file", line)
    return found


def tracked_tool_files() -> list[str]:
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--", "tools"],
        check=True,
        capture_output=True,
    )
    return [p for p in result.stdout.decode("utf-8").split("\0") if p]


class NA8TreeTest(unittest.TestCase):
    def test_no_tracked_tool_writes_the_gpu_preference_key(self) -> None:
        problems: list[str] = []
        scanned = 0
        for rel in tracked_tool_files():
            if rel in EXCLUDED:
                continue
            path = REPO_ROOT / rel
            if not path.is_file() or path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
            if b"\0" in data[:4096]:
                continue
            scanned += 1
            text = data.decode("utf-8", errors="replace")
            for number, name, snippet in find_violations(text, allow_removal=rel in REMOVAL_ALLOWLIST):
                problems.append(f"{rel}:{number} [{name}] {snippet}")
        self.assertGreater(scanned, 100, "the scan found almost no files; git ls-files or the filter is broken")
        self.assertEqual(
            problems,
            [],
            "NA-8: tracked tooling must not write the per-app GPU-preference registry key. The app already "
            "requests the discrete GPU itself (platform/qt/main.cpp: NvOptimusEnablement, "
            "AmdPowerXpressRequestHighPerformance); record the adapter actually used instead.",
        )

    def test_allowlisted_removal_files_exist(self) -> None:
        for rel in REMOVAL_ALLOWLIST:
            self.assertTrue((REPO_ROOT / rel).is_file(), f"allowlist names a file that does not exist: {rel}")

    def test_the_prune_script_is_dry_run_by_default_and_never_sets_a_value(self) -> None:
        text = (REPO_ROOT / "tools/profiling/venue/Prune-MlvGpuPreferences.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$Apply", text)
        self.assertEqual(find_violations(text, allow_removal=True), [])


class DetectorTable(unittest.TestCase):
    """The detector must flag these and pass those; otherwise the tree test above could be vacuous."""

    KEY = r"HKCU:\Software\Microsoft\DirectX\UserGpuPreferences"

    POSITIVE = {
        "new-itemproperty literal": "New-ItemProperty -Path '" + KEY + "' -Name $exe -Value 'GpuPreference=2;' -Force",
        "set-itemproperty literal": "Set-ItemProperty -Path '" + KEY + "' -Name $exe -Value 'x'",
        "reg add": r'reg add "HKCU\Software\Microsoft\DirectX\UserGpuPreferences" /v "$p" /t REG_SZ /d "GpuPreference=2;" /f',
        "reg add bare value": 'reg add "HKCU\\X" /v "$p" /d "GpuPreference=2;" /f',
        "variable indirection": "$k = '" + KEY + "'\nif (-not (Test-Path $k)) { }\nNew-ItemProperty -Path $k -Name $e -Value 'v'",
        "continued line": "New-ItemProperty `\n  -Path '" + KEY + "' `\n  -Name $e -Value 'GpuPreference=2;'",
        "dotnet setvalue": "[Microsoft.Win32.Registry]::SetValue('HKEY_CURRENT_USER\\Software\\Microsoft\\DirectX\\UserGpuPreferences', $n, 'v')",
        "remove-itemproperty": "Remove-ItemProperty -Path '" + KEY + "' -Name $e",
        "reg delete": 'reg delete "HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences" /v "$p" /f',
        "new-item key": "New-Item -Path '" + KEY + "' -Force | Out-Null",
        "python winreg": 'k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\DirectX\\UserGpuPreferences")',
        "cpp api": 'RegSetValueExA(h, exe, 0, REG_SZ, v, n); // "UserGpuPreferences"',
        "case folded": "new-itemproperty -path 'hkcu:\\software\\microsoft\\directx\\usergpupreferences' -name $e -value x",
    }

    NEGATIVE = {
        "read only get-itemproperty": "$v = Get-ItemProperty -LiteralPath '" + KEY + "' -Name $e -ErrorAction SilentlyContinue",
        "read only reg query": 'reg query "HKCU\\Software\\Microsoft\\DirectX\\UserGpuPreferences" /v "$p"',
        "test-path": "if (Test-Path '" + KEY + "') { 'present' }",
        "comment only": "# do not New-ItemProperty on " + KEY,
        "unrelated registry write": "New-ItemProperty -Path 'HKCU:\\Software\\Vendor\\App' -Name Level -Value 3",
        "unrelated new-item": "New-Item -ItemType Directory -Force -Path $outDir | Out-Null",
        "message text": "Write-Host 'the app exports NvOptimusEnablement'",
    }

    def test_positive_fixtures_are_flagged(self) -> None:
        for name, text in self.POSITIVE.items():
            with self.subTest(name):
                self.assertTrue(find_violations(text), f"detector missed: {name}")

    def test_negative_fixtures_pass(self) -> None:
        for name, text in self.NEGATIVE.items():
            with self.subTest(name):
                self.assertEqual(find_violations(text), [], f"detector over-fired on: {name}")

    def test_removal_is_allowed_only_for_the_allowlist(self) -> None:
        removal_only = "Remove-ItemProperty -Path '" + self.KEY + "' -Name $e"
        self.assertTrue(find_violations(removal_only))
        self.assertEqual(find_violations(removal_only, allow_removal=True), [])
        setter = "New-ItemProperty -Path '" + self.KEY + "' -Name $e -Value 'GpuPreference=2;'"
        self.assertTrue(find_violations(setter, allow_removal=True))


if __name__ == "__main__":
    unittest.main()
