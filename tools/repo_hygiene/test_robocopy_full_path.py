"""ROBOCOPY-FULL-PATH-TRACKED-1: tracked tooling never invokes a bare ``robocopy``.

The Windows Resource Kit ships its own ``robocopy.exe`` and on the hub VM and the benches it resolves FIRST on PATH. That copy rejects
``/BYTES`` and ignores ``/MAXAGE``, so a script that says ``robocopy.exe`` silently runs a different tool than the one it was written
against (DISK-SPACE-SAGA rule 2). The invocation has to name the OS copy by full path, for example
``Join-Path $env:SystemRoot 'System32\\robocopy.exe'``.

This lint scans every tracked ``*.ps1`` / ``*.psm1`` / ``*.cmd`` / ``*.bat`` / ``*.py`` under ``tools/`` for an INVOCATION of robocopy
that does not go through a ``System32`` path:

  * PowerShell: command position (``robocopy ...``, ``& robocopy.exe ...``, ``& 'robocopy.exe' ...``), ``Start-Process robocopy``,
    ``cmd /c robocopy``. Comments, here-strings and string literals (messages, ``Write-Host`` text) are masked out first.
  * cmd / bat: a command word (also after ``call``, ``start``, ``&``, ``do``, ``else``); ``rem`` and ``::`` lines are skipped.
  * Python (parsed with ``ast``): a subprocess-family call (``run``, ``Popen``, ``check_output``, ``system`` ...) whose argv list starts
    with, or whose command string starts with, a robocopy that is not a System32 path.

Not covered on purpose: a bare name smuggled through a variable (``$tool = 'robocopy.exe'; & $tool``) and an alias. The fix keeps the
variable form safe because the variable is built from ``$env:SystemRoot``.
"""

from __future__ import annotations

import ast
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIR = "tools"
SCAN_SUFFIXES = (".ps1", ".psm1", ".cmd", ".bat", ".py")

_TOKEN = r"(?:[^\s\"'|;&(){}=]*[\\/])?robocopy(?:\.exe)?"
_END = r"(?=\s|$|[;|)}])"

# PowerShell: bare word at command position (start of line, after ; | ( ) { } = or the call operator &). Not a hashtable key.
_PS_BARE = re.compile(rf"(?P<a>^\s*|[;|(){{}}=&])\s*(?P<path>{_TOKEN}){_END}(?!\s*=)", re.IGNORECASE)
# PowerShell: the call operator on a quoted name, & 'robocopy.exe' (a quoted name anywhere else is just a string).
_PS_AMP_QUOTED = re.compile(
    r"(?P<a>&)\s*(?P<q>[\"'])(?P<path>(?:[^\"']*[\\/])?robocopy(?:\.exe)?)(?P=q)", re.IGNORECASE
)
# PowerShell: Start-Process / saps with the robocopy as a whitespace-led argument, and cmd /c robocopy.
_PS_START = re.compile(
    rf"(?P<a>\b(?:Start-Process|saps)\b)[^;|\n]*?(?<=\s)(?P<q>[\"']?)(?P<path>{_TOKEN})(?P=q){_END}", re.IGNORECASE
)
_PS_CMDC = re.compile(rf"(?P<a>\bcmd(?:\.exe)?\s+/[ck]\s+)[\"']?(?P<path>{_TOKEN}){_END}", re.IGNORECASE)

_CMD_BARE = re.compile(
    rf"(?:^|&&|&|\||\bdo\b|\belse\b|\()\s*@?\s*(?:call\s+|start\s+(?:\"[^\"]*\"\s+)?(?:/\w+\s+)*)?"
    rf"(?P<q>\"?)(?P<path>(?:[^\s\"|&()]*[\\/])?robocopy(?:\.exe)?)(?P=q)(?=\s|$|[&|)])",
    re.IGNORECASE,
)

_PY_SPAWNERS = frozenset(
    {"run", "call", "check_call", "check_output", "Popen", "system", "popen", "getoutput", "getstatusoutput", "startfile"}
)
_PY_ARGV_ELEMENT = re.compile(r"^(?:.*[\\/])?robocopy(?:\.exe)?$", re.IGNORECASE)
_PY_COMMAND_STRING = re.compile(r"^\s*(?P<q>[\"']?)(?P<path>(?:[^\"']*[\\/])?robocopy(?:\.exe)?)(?P=q)(?:\s|$)", re.IGNORECASE)


def _not_system32(path: str) -> bool:
    """True when ``path`` is a bare name or a directory path that does not go through System32."""
    normalised = path.replace("/", "\\").lower()
    return "\\" not in normalised or "\\system32\\" not in normalised


def _ps_mask(text: str) -> list[tuple[str, list[bool]]]:
    """Per line: the text with comments blanked, plus a flag per character that is True inside a string (or here-string).

    Quote characters themselves are code. A multi-line here-string keeps every continuation line flagged, so prose inside it never
    reads as a command.
    """
    lines: list[tuple[str, list[bool]]] = []
    chars: list[str] = []
    flags: list[bool] = []
    state: str | None = None  # None | sq | dq | hsq | hdq | line | block
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\r":
            i += 1
            continue
        if ch == "\n":
            if state == "line":
                state = None
            lines.append(("".join(chars), flags))
            chars, flags = [], []
            i += 1
            continue
        if state is None:
            if ch == "#":
                state = "line"
                chars.append(" ")
                flags.append(False)
            elif ch == "<" and nxt == "#":
                state = "block"
                chars.extend("  ")
                flags.extend([False, False])
                i += 2
                continue
            elif ch == "@" and nxt in ("'", '"') and not text[i + 2 :].split("\n", 1)[0].strip():
                state = "hsq" if nxt == "'" else "hdq"
                chars.extend([ch, nxt])
                flags.extend([False, False])
                i += 2
                continue
            elif ch == "'":
                state = "sq"
                chars.append(ch)
                flags.append(False)
            elif ch == '"':
                state = "dq"
                chars.append(ch)
                flags.append(False)
            elif ch == "`" and nxt not in ("", "\n", "\r"):
                chars.extend([ch, nxt])
                flags.extend([False, False])
                i += 2
                continue
            else:
                chars.append(ch)
                flags.append(False)
        elif state == "line":
            chars.append(" ")
            flags.append(False)
        elif state == "block":
            if ch == "#" and nxt == ">":
                state = None
                chars.extend("  ")
                flags.extend([False, False])
                i += 2
                continue
            chars.append(" ")
            flags.append(False)
        elif state == "sq":
            if ch == "'":
                state = None
                chars.append(ch)
                flags.append(False)
            else:
                chars.append(ch)
                flags.append(True)
        elif state == "dq":
            if ch == "`" and nxt not in ("", "\n", "\r"):
                chars.extend([ch, nxt])
                flags.extend([True, True])
                i += 2
                continue
            if ch == '"':
                state = None
                chars.append(ch)
                flags.append(False)
            else:
                chars.append(ch)
                flags.append(True)
        else:  # here-string: closes only on '@ / "@ at the very start of a line
            closer = "'" if state == "hsq" else '"'
            if not chars and ch == closer and nxt == "@":
                state = None
                chars.extend([ch, nxt])
                flags.extend([False, False])
                i += 2
                continue
            chars.append(ch)
            flags.append(True)
        i += 1
    if chars:
        lines.append(("".join(chars), flags))
    return lines


def scan_powershell(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for number, (code, in_string) in enumerate(_ps_mask(text), start=1):
        hit = False
        for pattern in (_PS_BARE, _PS_AMP_QUOTED, _PS_START, _PS_CMDC):
            for m in pattern.finditer(code):
                anchor = m.group("a")
                at = m.start("a") if anchor.strip() else m.end("a")
                if at < len(in_string) and in_string[at]:
                    continue
                if _not_system32(m.group("path")):
                    hit = True
        if hit:
            found.append((number, code.strip()))
    return found


def scan_cmd(text: str) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if re.match(r"^\s*(?:@?rem\b|::)", line, re.IGNORECASE):
            continue
        for m in _CMD_BARE.finditer(line):
            if _not_system32(m.group("path")):
                found.append((number, line.strip()))
                break
    return found


def scan_python(text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
        if name not in _PY_SPAWNERS:
            continue
        first = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == "args"), None)
        bad = False
        if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
            head = first.elts[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str) and _PY_ARGV_ELEMENT.match(head.value):
                bad = _not_system32(head.value)
        elif isinstance(first, ast.Constant) and isinstance(first.value, str):
            m = _PY_COMMAND_STRING.match(first.value)
            bad = bool(m) and _not_system32(m.group("path"))
        if bad:
            found.append((node.lineno, lines[node.lineno - 1].strip()))
    return found


def scan_text(relpath: str, text: str) -> list[tuple[int, str]]:
    suffix = Path(relpath).suffix.lower()
    if suffix in (".ps1", ".psm1"):
        return scan_powershell(text)
    if suffix in (".cmd", ".bat"):
        return scan_cmd(text)
    if suffix == ".py":
        return scan_python(text)
    return []


def tracked_tool_files() -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z", "--", SCAN_DIR],
        capture_output=True,
        check=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    ).stdout
    names = [n for n in out.decode("utf-8", errors="replace").split("\0") if n]
    return sorted(n for n in names if n.lower().endswith(SCAN_SUFFIXES))


def scan_repo() -> list[str]:
    findings: list[str] = []
    for rel in tracked_tool_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        for number, line in scan_text(rel, text):
            findings.append(f"{rel}:{number}: {line}")
    return findings


# (case name, file name, source). Every row must be flagged.
FLAGGED = [
    ("ps call operator, bare exe", "a.ps1", "& robocopy.exe @rcArgs | Out-Null\n"),
    ("ps call operator, bare name", "a.ps1", "    & robocopy $src $dst /MIR\n"),
    ("ps call operator, quoted bare name", "a.ps1", "& 'robocopy.exe' $src $dst\n"),
    ("ps command position at line start", "a.ps1", "robocopy $src $dst /E\n"),
    ("ps output captured", "a.ps1", "$output = & robocopy.exe @args 2>&1\n"),
    ("ps inside a script block", "a.ps1", "if ($go) { robocopy.exe $a $b }\n"),
    ("ps after a pipe or semicolon", "a.ps1", "Get-Date; robocopy.exe $a $b\n"),
    ("ps Start-Process bare", "a.ps1", "Start-Process robocopy.exe -ArgumentList $a -Wait\n"),
    ("ps Start-Process -FilePath bare", "a.ps1", "Start-Process -NoNewWindow -FilePath 'robocopy' -ArgumentList $a\n"),
    ("ps cmd /c robocopy", "a.ps1", "& cmd /c \"robocopy $a $b\"\n"),
    ("ps non-System32 directory (Resource Kit)", "a.ps1", "& 'C:\\Program Files\\Windows Resource Kits\\Tools\\robocopy.exe' $a $b\n"),
    ("ps module file", "a.psm1", "function Copy-It { robocopy $a $b }\n"),
    ("ps code after a here-string", "a.ps1", "$h = @'\nnote\n'@\nrobocopy $a $b\n"),
    ("cmd bare", "a.cmd", "@echo off\nrobocopy %1 %2 /E\n"),
    ("cmd call", "a.bat", "call robocopy.exe %1 %2\n"),
    ("cmd after &&", "a.cmd", "mkdir out && robocopy src out\n"),
    ("cmd after do", "a.cmd", "for %%f in (*) do robocopy %%f out\n"),
    ("py argv list", "a.py", "import subprocess\nsubprocess.run(['robocopy', 'a', 'b'], check=True)\n"),
    ("py argv list, .exe", "a.py", "import subprocess\nsubprocess.check_output(['robocopy.exe', 'a', 'b'])\n"),
    ("py args= keyword", "a.py", "import subprocess\nsubprocess.Popen(args=['ROBOCOPY.EXE', 'a'])\n"),
    ("py command string", "a.py", "import os\nos.system('robocopy a b /E')\n"),
    ("py non-System32 argv", "a.py", "import subprocess\nsubprocess.run(['C:\\\\Windows\\\\robocopy.exe', 'a'])\n"),
]

# Every row must NOT be flagged.
NOT_FLAGGED = [
    ("ps comment", "a.ps1", "# robocopy exit codes < 8 are success\n"),
    ("ps trailing comment", "a.ps1", "$x = 1  # & robocopy.exe is the Resource Kit copy\n"),
    ("ps block comment", "a.ps1", "<#\nrobocopy /MIR\n& robocopy.exe x y\n#>\n"),
    ("ps Write-Host text", "a.ps1", "Write-Host (\"robocopy {0} -> {1}\" -f $a, $b)\n"),
    ("ps error string", "a.ps1", "throw \"robocopy failed ($LASTEXITCODE): $src -> $dst\"\n"),
    ("ps message beginning with the word", "a.ps1", "$msg = \"robocopy: nothing to copy\"\n"),
    ("ps string holding a call", "a.ps1", "$doc = 'then & robocopy.exe runs'\n"),
    ("ps here-string prose", "a.ps1", "$h = @\"\nrobocopy /MIR is destructive\n& robocopy.exe x y\n\"@\n"),
    ("ps name used as an argument", "a.ps1", "$c = Get-Command robocopy.exe -ErrorAction SilentlyContinue\n"),
    ("ps function name", "a.ps1", "function Invoke-RobocopyMirror {\n}\nInvoke-RobocopyMirror -Source $a\n"),
    ("ps call through a variable", "a.ps1", "& $robocopyExe @rcArgs | Out-Null\n"),
    ("ps System32 via SystemRoot", "a.ps1", "& \"$env:SystemRoot\\System32\\robocopy.exe\" $a $b\n"),
    ("ps System32 literal path", "a.ps1", "& 'C:\\Windows\\System32\\robocopy.exe' $a $b\n"),
    ("ps System32 path built with Join-Path", "a.ps1", "$robocopyExe = Join-Path $env:SystemRoot 'System32\\robocopy.exe'\n"),
    ("ps Start-Process System32", "a.ps1", "Start-Process -FilePath \"$env:SystemRoot\\System32\\robocopy.exe\" -ArgumentList $a\n"),
    ("ps hashtable key", "a.ps1", "$t = @{ robocopy = 1 }\n"),
    ("ps variable named like the tool", "a.ps1", "$robocopy = 1\n"),
    ("cmd rem", "a.cmd", "rem robocopy %1 %2\n"),
    ("cmd colon comment", "a.bat", ":: robocopy %1 %2\n"),
    ("cmd echo", "a.cmd", "echo robocopy is not used here\n"),
    ("cmd System32", "a.cmd", "%SystemRoot%\\System32\\robocopy.exe %1 %2 /E\n"),
    ("py comment", "a.py", "# subprocess.run(['robocopy', 'a', 'b'])\n"),
    ("py docstring prose", "a.py", "def f():\n    \"\"\"robocopy mirror deleted a folder\"\"\"\n"),
    ("py membership test", "a.py", "ok = 'robocopy' in heading\n"),
    ("py message call", "a.py", "parser.error('robocopy failed')\n"),
    ("py fixture string handed to a scanner", "a.py", "scan('& robocopy.exe @a')\n"),
    ("py System32 argv", "a.py", "import subprocess\nsubprocess.run(['C:\\\\Windows\\\\System32\\\\robocopy.exe', 'a'])\n"),
    ("py System32 built at runtime", "a.py", "import subprocess\nsubprocess.run([robocopy_path, 'a'])\n"),
    ("py other tool", "a.py", "import subprocess\nsubprocess.run(['xcopy', 'a', 'b'])\n"),
    ("py unparseable file", "a.py", "def (:\n robocopy\n"),
]


class ScannerFixtureTests(unittest.TestCase):
    def test_every_flagged_case_is_flagged(self) -> None:
        for name, fname, source in FLAGGED:
            with self.subTest(name):
                self.assertTrue(scan_text(fname, source), f"{name}: expected a finding in {source!r}")

    def test_every_not_flagged_case_is_clean(self) -> None:
        for name, fname, source in NOT_FLAGGED:
            with self.subTest(name):
                self.assertEqual(scan_text(fname, source), [], f"{name}: unexpected finding in {source!r}")

    def test_finding_names_the_line(self) -> None:
        self.assertEqual(scan_text("a.ps1", "$a = 1\n& robocopy.exe $a\n"), [(2, "& robocopy.exe $a")])

    def test_other_suffixes_are_not_scanned(self) -> None:
        self.assertEqual(scan_text("notes.md", "& robocopy.exe x y\n"), [])


class TrackedToolsTests(unittest.TestCase):
    def test_scan_covers_the_known_robocopy_scripts(self) -> None:
        tracked = tracked_tool_files()
        for rel in (
            "tools/profiling/deploy-to-ultra-magnus.ps1",
            "tools/profiling/invoke-ultramagnus-cdng-export-evidence.ps1",
            "tools/profiling/invoke-ultramagnus-p3-evidence.ps1",
        ):
            self.assertIn(rel, tracked)

    def test_no_tracked_tool_calls_a_bare_robocopy(self) -> None:
        findings = scan_repo()
        self.assertEqual(
            findings,
            [],
            "bare robocopy invocation (the Resource Kit copy shadows the OS one on PATH; DISK-SPACE-SAGA rule 2). "
            "Call Join-Path $env:SystemRoot 'System32\\robocopy.exe' instead:\n" + "\n".join(findings),
        )


if __name__ == "__main__":
    unittest.main()
