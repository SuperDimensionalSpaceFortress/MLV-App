"""Guard: a ``[string[]]`` script parameter must survive ``pwsh -File x.ps1 -P a,b``.

Under ``-File`` every argument is ONE literal string, so ``-Dirs a,b`` binds a single element
``"a,b"`` and a ``[string[]]`` parameter accepts it silently: a membership or count check then
passes or matches nothing. Bus TRAPS.md 2026-08-09 (first-hand, adversarialllm OPUS) and the
2026-10-04 agent-bridge entry (bus commit fa47038) record the same fail-open. This module turns
the trap into a check that hosted CI runs.

The rule: a script-level ``[string[]]`` parameter must split a comma-joined single element
(``-split ','``, ``.Split(',')`` or a ``$P = Resolve-...  $P`` helper) WHEN a tracked file shows
that script being invoked with ``-File`` (or in its own ``Usage:`` text) and a comma list for
that parameter.

NON-PROMISES (what this does NOT see):
- Regex over text, no PowerShell AST and no pwsh process. A comma list assembled at run time
  (``-join ','`` into a variable) is not seen; neither is an ARRAY variable handed to a ``-File``
  child (``-P $arr`` expands to separate tokens and everything after the first is dropped or
  lands in the next positional parameter; export-release-cuda-dogfood-kit.ps1 was this shape).
  The documented ``-File x.ps1 -AdditionalArgs @('--a', 'b')`` form is not seen either: a
  PowerShell caller expands the array literal into separate tokens, so only the first survives.
- Only the first column-0 ``param(`` of a file is read, so a function-level parameter is out of
  scope, and so is a script that nests its real param block in a here-string.
- Pass-through parameters (``-AdditionalArgs`` and friends) cannot be fixed by splitting on a
  comma, because their values may legitimately contain commas. They are listed in KNOWN_OPEN with
  the reason and are asserted to still be violations, so the list cannot go stale.
- tools/hooks/* is out of scope (read-only for this guard).

Collected by ``unittest discover -s tools/repo_hygiene -p "test_*.py" -t .``; no workflow names it.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_PREFIXES = ("tools/hooks/",)
TEXT_SUFFIXES = frozenset((
    ".ps1", ".psm1", ".py", ".md", ".json", ".yml", ".yaml", ".cmd", ".bat", ".txt", ".sh", ".mjs", ".js",
))
MAX_BYTES = 2_000_000

# (script path, parameter) -> why splitting does not apply and where the follow-up lives.
# Each entry is asserted to STILL be a violation: fix the script or its caller, then delete it.
KNOWN_OPEN: dict[tuple[str, str], str] = {}


@dataclass(frozen=True)
class Violation:
    script: str
    param: str
    caller: str
    line: int
    detail: str

    def render(self) -> str:
        return f"{self.script} -{self.param}: {self.caller}:{self.line}: {self.detail}"


def _blank_comments(text: str) -> str:
    """Return text with PowerShell comments replaced by spaces (newlines kept so line numbers hold)."""
    out: list[str] = []
    i, n = 0, len(text)
    quote = ""
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if quote == '"' and c == "`" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                if quote == "'" and i + 1 < n and text[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                quote = ""
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            out.append(c)
        elif c == "<" and text.startswith("<#", i):
            end = text.find("#>", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join(ch if ch in "\r\n" else " " for ch in text[i:end]))
            i = end
            continue
        elif c == "#":
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def script_string_array_params(text: str) -> list[tuple[str, int, int]]:
    """(name, line, param_block_end_offset) for each ``[string[]]`` parameter of the first column-0 param block."""
    clean = _blank_comments(text)
    m = re.search(r"(?im)^param\s*\(", clean)
    if not m:
        return []
    depth, i, quote = 1, m.end(), ""
    while i < len(clean) and depth:
        c = clean[i]
        if quote:
            if quote == '"' and c == "`":
                i += 2
                continue
            if c == quote:
                quote = ""
        elif c in ("'", '"'):
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        i += 1
    block = clean[m.end():i]
    found = []
    for pm in re.finditer(r"\[string\[\]\]\s*\$(\w+)", block, re.I):
        line = clean.count("\n", 0, m.end() + pm.start()) + 1
        found.append((pm.group(1), line, i))
    return found


def is_normalised(text: str, name: str, block_end: int) -> bool:
    """True when the script splits a comma-joined single element of ``$name`` after its param block."""
    body = _blank_comments(text)[block_end:]
    var = re.compile(r"\$" + re.escape(name) + r"\b", re.I)
    split = re.compile(r"(?:-split\b|\.Split\s*\()", re.I)
    lines = body.splitlines()
    for idx, line in enumerate(lines):
        if split.search(line) and any(var.search(w) for w in lines[max(0, idx - 2):idx + 3]):
            return True
    return bool(re.search(r"\$" + re.escape(name) + r"\s*=\s*Resolve-[\w-]+[^\r\n]*\$" + re.escape(name) + r"\b", body, re.I))


def _logical_commands(text: str) -> list[tuple[int, str]]:
    """Join backtick / backslash continuation lines; keep the first physical line number."""
    cmds: list[tuple[int, str]] = []
    cur, start = "", 0
    for no, raw in enumerate(text.splitlines(), 1):
        if not cur:
            start = no
        stripped = raw.rstrip()
        if stripped.endswith("`") or stripped.endswith("\\"):
            cur += stripped[:-1] + " "
            continue
        cmds.append((start, cur + raw))
        cur = ""
    if cur:
        cmds.append((start, cur))
    return cmds


def _comma_value(cmd: str, name: str) -> str | None:
    item = r"(?:'[^']*'|\"[^\"]*\"|[^\s`|),]+)"
    m = re.search(r"(?<![\w-])-" + re.escape(name) + r"(?:\s+|:)(" + item + r"(?:," + item + r")*)", cmd, re.I)
    if m and "," in m.group(1):
        return m.group(1)
    return None


def find_violations(files: dict[str, str]) -> list[Violation]:
    """files maps a repo-relative posix path to its text."""
    open_params: dict[str, list[str]] = {}
    for path, text in files.items():
        if not path.lower().endswith(".ps1"):
            continue
        names = [name for name, _line, end in script_string_array_params(text) if not is_normalised(text, name, end)]
        if names:
            open_params[path] = names
    out: list[Violation] = []
    for script, names in sorted(open_params.items()):
        base = script.rsplit("/", 1)[-1].lower()
        for path, text in sorted(files.items()):
            if base not in text.lower():
                continue
            for lineno, cmd in _logical_commands(text):
                if base not in cmd.lower():
                    continue
                has_file = re.search(r"(?<![\w-])-File\b", cmd, re.I) is not None
                has_command = re.search(r"(?<![\w-])-Command\b", cmd, re.I) is not None
                usage = re.search(r"\busage\s*:", cmd, re.I) is not None
                if not (has_file or (usage and not has_command)):
                    continue
                for name in names:
                    value = _comma_value(cmd, name)
                    if value is not None:
                        out.append(Violation(script, name, path, lineno, f"comma list {value[:60]!r} reaches a [string[]] with no split"))
    return out


def tracked_texts(root: Path = REPO_ROOT) -> dict[str, str]:
    raw = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True).stdout
    files: dict[str, str] = {}
    for rel in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not rel or rel.startswith(SKIP_PREFIXES) or Path(rel).suffix.lower() not in TEXT_SUFFIXES:
            continue
        p = root / rel
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            files[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files


class PwshStringArrayFileParams(unittest.TestCase):
    RED_SCRIPT = "param(\n    [Parameter(Mandatory = $true)][string[]]$Dirs,\n    [int]$Top = 1\n)\n$Dirs | ForEach-Object { $_ }\n"

    def test_red_usage_line_with_comma_list_and_no_split_is_flagged(self):
        files = {
            "tools/x/trace.ps1": "# Usage: trace.ps1 -Dirs dirA,dirB [-Top 3]\n" + self.RED_SCRIPT,
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], [(v.script, v.param) for v in find_violations(files)])

    def test_red_file_caller_with_continuation_lines_is_flagged(self):
        files = {
            "tools/x/trace.ps1": self.RED_SCRIPT,
            "docs/how.md": "pwsh -NoProfile -File tools\\x\\trace.ps1 `\n    -Top 3 `\n    -Dirs 'a','b'\n    -Dirs a,b\n",
        }
        self.assertEqual(1, len(find_violations(files)))

    def test_green_split_in_script_clears_it(self):
        fixed = self.RED_SCRIPT.replace(
            "$Dirs | ForEach",
            "$Dirs = @($Dirs | ForEach-Object { $_ -split ',' } | Where-Object { $_ })\n$Dirs | ForEach",
        )
        files = {"tools/x/trace.ps1": fixed, "docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        self.assertEqual([], find_violations(files))

    def test_green_dotnet_split_and_resolve_helper_clear_it(self):
        dot = self.RED_SCRIPT.replace("$Dirs | ForEach", "$parts = $Dirs[0].Split(',')\n$Dirs | ForEach")
        helper = self.RED_SCRIPT.replace("$Dirs | ForEach", "$Dirs = Resolve-Dirs -Dirs $Dirs\n$Dirs | ForEach")
        for text in (dot, helper):
            files = {"tools/x/trace.ps1": text, "docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
            self.assertEqual([], find_violations(files), text)

    def test_green_single_value_command_mode_and_in_process_callers_are_not_flagged(self):
        files = {
            "tools/x/trace.ps1": self.RED_SCRIPT,
            "docs/a.md": "pwsh -File tools/x/trace.ps1 -Dirs onlyone\n",
            "docs/b.md": "pwsh -Command \"& tools/x/trace.ps1 -Dirs a,b\"\n",
            "docs/c.md": "& tools/x/trace.ps1 -Dirs a,b\n",
        }
        self.assertEqual([], find_violations(files))

    def test_param_block_reader_ignores_comments_functions_and_nested_parens(self):
        text = (
            "[CmdletBinding()]\nparam(\n    # [string[]]$Commented,\n"
            "    [ValidatePattern('^(a|b)$')][string]$Plain,\n    [string[]]$Real = @('x')\n)\n"
            "function F {\n    param([string[]]$InFunction)\n}\n"
        )
        self.assertEqual(["Real"], [name for name, _line, _end in script_string_array_params(text)])

    def test_live_tree_has_no_unexplained_violations(self):
        found = find_violations(tracked_texts())
        unexplained = [v for v in found if (v.script, v.param) not in KNOWN_OPEN]
        self.assertEqual([], unexplained, "\n".join(v.render() for v in unexplained))

    def test_known_open_entries_are_still_violations(self):
        still = {(v.script, v.param) for v in find_violations(tracked_texts())}
        stale = sorted(set(KNOWN_OPEN) - still)
        self.assertEqual([], stale, f"fixed? delete these KNOWN_OPEN entries: {stale}")


if __name__ == "__main__":
    unittest.main()
