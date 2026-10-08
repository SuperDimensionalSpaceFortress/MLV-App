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

A caller that hands an ARRAY to a ``-File`` child is flagged whether or not the callee splits: ``-P $arr``,
``-P @(...)`` and the quoted ArgumentList form ``'-File', 'x.ps1', '-P', $arr`` expand to separate tokens, so
only the first element binds (export-release-cuda-dogfood-kit.ps1 and invoke-ultramagnus-p3-evidence.ps1 were
this shape). Fix the caller with ``($arr -join ',')`` and split in the script. A variable counts as an array
when the caller's file declares it ``[string[]]`` / ``[array]``, assigns it ``@(...)`` / a comma list / a
``-split`` / an ``...Array...`` helper, or aliases one of those. A split counts as normalisation only when it
is applied to the parameter itself (``$P -split``, ``$P[0].Split(``, ``$P | ... -split``) or assigned back to it.

NON-PROMISES (what this does NOT see):
- Regex over text, no PowerShell AST and no pwsh process. A comma list assembled at run time
  (``-join ','`` into a variable) is not seen; a variable of unknown type (``-P $x``) is not flagged;
  a caller that only appears in prose (docs/playback-attr-3-cuda.md names ``-CudaArchitectures
  sm_86,compute_86`` with no ``-File`` on the line) is not seen, so the dll-job split has no live-tree
  revert test. A ``[ValidateSet]`` on a top-level ``[string[]]`` parameter (it rejects the joined element at
  bind time, before any split) is checked only for scripts embedded as here-strings.
- Only the first column-0 ``param(`` of a file is read, so a function-level parameter is out of
  scope, and so is a script that nests its real param block in a here-string.
- Pass-through parameters (``-AdditionalArgs`` and friends) cannot be fixed by splitting on a
  comma, because their values may legitimately contain commas. They are listed in KNOWN_OPEN with
  the reason and are asserted to still be violations, so the list cannot go stale.
- tools/hooks/* is out of scope (read-only for this guard).

Collected by ``unittest discover -s tools/repo_hygiene -p "test_*.py" -t .``; no workflow names it.
"""

from __future__ import annotations

import functools
import re
import subprocess
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP_PREFIXES = ("tools/hooks/",)
# This module's fixtures spell out the very shapes it flags (and name real scripts), so it is not a caller.
SELF = "tools/repo_hygiene/test_pwsh_string_array_file_params.py"
TEXT_SUFFIXES = frozenset((
    ".ps1", ".psm1", ".py", ".md", ".json", ".yml", ".yaml", ".cmd", ".bat", ".txt", ".sh", ".mjs", ".js",
))
MAX_BYTES = 2_000_000
MAX_COMMA_JOIN = 60

# (script path, parameter) -> why splitting does not apply and where the follow-up lives.
# Each entry is asserted to STILL be a violation: fix the script or its caller, then delete it.
KNOWN_OPEN: dict[tuple[str, str], str] = {
    ("tools/profiling/run-release-gui-smoke.ps1", "ExtraEnvironment"): (
        "docs/04-external-auditor-guide.md:496 documents `-ExtraEnvironment @(...)`. A PowerShell caller expands the "
        "array into separate tokens, so only the first binds. Pass-through values may hold commas, so a comma split "
        "is not the fix; follow-up PWSH-FILE-ARRAY-PASSTHRU-1 (proposed in PR #311): fail loud or pass base64 JSON."
    ),
    ("tools/profiling/run-release-playback-profile.ps1", "AdditionalArgs"): (
        "docs/14-performance-benchmarking.md:544 documents `-AdditionalArgs @('--a', 'b')`; same expansion, same "
        "reason, same follow-up PWSH-FILE-ARRAY-PASSTHRU-1."
    ),
}


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


def _statements(body: str) -> list[str]:
    """Split comment-blanked PowerShell into whitespace-collapsed statements.

    A statement ends at a newline or ``;`` unless it is continued: a trailing ``|``, ``,`` or backtick, a next
    line that starts with ``|``, an open ``(`` / ``[``, or an open ``{`` of a pipeline script block (a ``{`` after
    a ``|``). Plain ``{`` / ``}`` (function, if, foreach bodies) end a statement, so one statement never spans a
    whole block and an unrelated ``-split`` cannot ride along with a parameter mention.
    """
    out: list[str] = []
    cur: list[str] = []
    braces: list[bool] = []
    depth, quote, i, n = 0, "", 0, len(body)

    def flush() -> None:
        stmt = " ".join("".join(cur).split())
        if stmt:
            out.append(stmt)
        cur.clear()

    while i < n:
        c = body[i]
        if quote:
            cur.append(c)
            if quote == '"' and c == "`" and i + 1 < n:
                cur.append(body[i + 1])
                i += 2
                continue
            if c == quote:
                if quote == "'" and i + 1 < n and body[i + 1] == "'":
                    cur.append("'")
                    i += 2
                    continue
                quote = ""
        elif c in ("'", '"'):
            quote = c
            cur.append(c)
        elif c == "`" and i + 1 < n and body[i + 1] in "\r\n":
            cur.append(" ")
            i += 2
            continue
        elif c in "([":
            depth += 1
            cur.append(c)
        elif c in ")]":
            depth = max(0, depth - 1)
            cur.append(c)
        elif c == "{":
            counted = "|" in "".join(cur)
            braces.append(counted)
            depth += 1 if counted else 0
            cur.append(c)
            if not counted and depth == 0:
                flush()
        elif c == "}":
            counted = braces.pop() if braces else False
            depth = max(0, depth - (1 if counted else 0))
            cur.append(c)
            if not counted and depth == 0:
                flush()
        elif c == ";" and depth == 0:
            flush()
        elif c == "\n":
            tail = "".join(cur).rstrip()
            ahead = body[i + 1:].lstrip()
            if depth or tail.endswith(("|", ",")) or ahead.startswith("|"):
                cur.append(" ")
            else:
                flush()
        else:
            cur.append(c)
        i += 1
    flush()
    return out


@functools.lru_cache(maxsize=8)
def _body_statements(text: str, block_end: int) -> tuple[str, ...]:
    """Statements after the param block; cached because every parameter of a script reads the same body."""
    return tuple(_statements(_blank_comments(text)[block_end:]))


def is_normalised(text: str, name: str, block_end: int) -> bool:
    """True when a statement after the param block splits ``$name`` itself, not merely something near it.

    Bound means one of: ``$Name -split`` / ``$Name[0].Split(`` (the split is applied to the parameter), a
    pipeline that starts at ``$Name`` and splits downstream, or ``$Name = Resolve-... $Name``.
    """
    var = r"\$" + re.escape(name) + r"\b"
    split = r"(?:-split\b|\.Split\s*\()"
    bound = (
        re.compile(var + r"(?:\[[^\]]*\])?\s*" + split, re.I),
        re.compile(var + r"[^|]*\|.*?" + split, re.I),
        re.compile(r"^" + var + r"\s*=\s*Resolve-[\w-]+.*" + var, re.I),
    )
    return any(rx.search(stmt) for stmt in _body_statements(text, block_end) for rx in bound)


def _logical_commands(text: str) -> list[tuple[int, str]]:
    """Join backtick / backslash / trailing-comma continuation lines; keep the first physical line number.

    Comma continuation is what keeps a multi-line ``@('-File', $script, '-P', $value)`` argument array in one
    command. It is capped so a comma-terminated data file cannot become one giant command.
    """
    cmds: list[tuple[int, str]] = []
    cur, start, joined = "", 0, 0
    for no, raw in enumerate(text.splitlines(), 1):
        if not cur:
            start, joined = no, 0
        stripped = raw.rstrip()
        if stripped.endswith("`") or stripped.endswith("\\"):
            cur += stripped[:-1] + " "
            continue
        if stripped.endswith(",") and joined < MAX_COMMA_JOIN:
            cur += stripped + " "
            joined += 1
            continue
        cmds.append((start, cur + raw))
        cur = ""
    if cur:
        cmds.append((start, cur))
    return cmds


def _array_variable_names(text: str) -> frozenset[str]:
    """Names (lower-case) that the file declares or assigns as arrays, following ``$a = $b`` aliases."""
    names: set[str] = set()
    for m in re.finditer(r"\[(?:string\[\]|object\[\]|array)\]\s*`?\$(\w+)", text, re.I):
        names.add(m.group(1).lower())
    assigns = re.findall(r"`?\$(\w+)\s*\+?=\s*([^\r\n]*)", text)
    arrayish = re.compile(r"^(?:@\(|.*Array|.*-split\b|(?:'[^']*'|\"[^\"]*\"|`?\$\w+)\s*,)", re.I)
    for lhs, rhs in assigns:
        if arrayish.match(rhs.strip()):
            names.add(lhs.lower())
    for _ in range(3):
        for lhs, rhs in assigns:
            alias = re.fullmatch(r"`?\$(\w+)", rhs.strip())
            if alias and alias.group(1).lower() in names:
                names.add(lhs.lower())
    return frozenset(names)


def _comma_value(cmd: str, name: str) -> str | None:
    item = r"(?:'[^']*'|\"[^\"]*\"|[^\s`|),]+)"
    m = re.search(r"(?<![\w-])-" + re.escape(name) + r"(?:\s+|:)(" + item + r"(?:," + item + r")*)", cmd, re.I)
    if m and "," in m.group(1):
        return m.group(1)
    return None


def embedded_launchers(text: str) -> list[str]:
    """Bodies of single-quoted here-strings that are themselves scripts (they start with ``param(``)."""
    return re.findall(r"@'\r?\n(\s*(?:\[CmdletBinding\(\)\]\s*)?param\(.*?)\r?\n'@", text, re.S | re.I)


def validateset_string_arrays(script: str) -> list[str]:
    """``[string[]]`` parameters guarded by ``[ValidateSet(...)]``: validation runs on the one joined element
    (``-P a,b``) before any in-script split can run, so a comma list is rejected at bind time."""
    names = []
    for name, _line, end in script_string_array_params(script):
        decl = _blank_comments(script)[max(0, end - 4000):end]
        if re.search(r"\[ValidateSet\([^)]*\)\]\s*(?:\[[^\]]*\]\s*)*\[string\[\]\]\s*\$" + re.escape(name) + r"\b", decl, re.I):
            names.append(name)
    return names


def _quoted_comma_token(cmd: str, name: str) -> str | None:
    """``'-P','a,b'`` (ArgumentList tokens): one quoted element holding a comma list."""
    m = re.search(r"(?<![\w-])-" + re.escape(name) + r"['\"]\s*,\s*('[^']*,[^']*'|\"[^\"]*,[^\"]*\")", cmd, re.I)
    return m.group(1) if m else None


def _array_value(cmd: str, name: str, array_vars: frozenset[str]) -> str | None:
    """An array variable or ``@(...)`` literal handed to ``-P`` (``-P $a`` or ``'-P',$a``).

    Under ``-File`` an array expands to separate tokens, so only its first element binds to ``-P``. A
    ``($a -join ',')`` value starts with ``(`` and is not matched; a scalar or unknown variable is not either.
    """
    pat = r"(?<![\w-])-" + re.escape(name) + r"['\"]?(?:\s*,\s*|\s+|:)\s*(@\(|`?\$(\w+)(?![\w.\[(:]))"
    for m in re.finditer(pat, cmd, re.I):
        if m.group(1) == "@(":
            return "@(...) literal"
        if m.group(2).lower() in array_vars:
            return f"array variable ${m.group(2)}"
    return None


def _caller_commands(text: str) -> list[tuple[int, str, str, bool]]:
    """(line, command, lower-cased command, token_file) for each command that can be a ``-File`` caller.

    ``token_file`` marks ``'-File', $script, '-P', $v``: an ArgumentList / argv array whose script is named
    elsewhere in the file rather than in the command itself.
    """
    out = []
    for lineno, cmd in _logical_commands(text):
        has_file = re.search(r"(?<![\w-])-File\b", cmd, re.I) is not None
        token_file = re.search(r"['\"]-File['\"]", cmd, re.I) is not None
        has_command = re.search(r"(?<![\w-])-Command\b", cmd, re.I) is not None
        usage = re.search(r"\busage\s*:", cmd, re.I) is not None
        if has_file or token_file or (usage and not has_command):
            out.append((lineno, cmd, cmd.lower(), token_file))
    return out


def find_violations(files: dict[str, str]) -> list[Violation]:
    """files maps a repo-relative posix path to its text."""
    scripts: dict[str, list[tuple[str, bool]]] = {}
    for path, text in files.items():
        if not path.lower().endswith(".ps1"):
            continue
        params = [(name, is_normalised(text, name, end)) for name, _line, end in script_string_array_params(text)]
        if params:
            scripts[path] = params
    out: list[Violation] = []
    lowered = {path: text.lower() for path, text in files.items()}
    per_file: dict[str, tuple[frozenset[str], list[tuple[int, str, str, bool]]]] = {}
    for script, params in sorted(scripts.items()):
        base = script.rsplit("/", 1)[-1].lower()
        open_names = [name for name, normalised in params if not normalised]
        for path, text in sorted(files.items()):
            if base not in lowered[path]:
                continue
            if path not in per_file:
                per_file[path] = (_array_variable_names(text), _caller_commands(text))
            array_vars, commands = per_file[path]
            for lineno, cmd, cmd_lower, token_file in commands:
                names_script = base in cmd_lower
                if names_script:
                    for name in open_names:
                        if "-" + name.lower() not in cmd_lower:
                            continue
                        value = _comma_value(cmd, name) or _quoted_comma_token(cmd, name)
                        if value is not None:
                            out.append(Violation(script, name, path, lineno, f"comma list {value[:60]!r} reaches a [string[]] with no split"))
                if names_script or token_file:
                    for name, _normalised in params:
                        if "-" + name.lower() not in cmd_lower:
                            continue
                        shape = _array_value(cmd, name, array_vars)
                        if shape is not None:
                            out.append(Violation(script, name, path, lineno, f"{shape} reaches a [string[]] over -File (only its first element binds; join it and split in the script)"))
    return out


def tracked_texts(root: Path = REPO_ROOT) -> dict[str, str]:
    raw = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True).stdout
    files: dict[str, str] = {}
    for rel in raw.decode("utf-8", "surrogateescape").split("\0"):
        if not rel or rel.startswith(SKIP_PREFIXES) or rel == SELF or Path(rel).suffix.lower() not in TEXT_SUFFIXES:
            continue
        p = root / rel
        try:
            if p.stat().st_size > MAX_BYTES:
                continue
            files[rel] = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files


@functools.cache
def _live_files() -> dict[str, str]:
    return tracked_texts()


@functools.cache
def _live_found() -> tuple[Violation, ...]:
    """One scan of the live tree shared by every test that needs it (each scan reads and parses ~1k files)."""
    return tuple(find_violations(_live_files()))


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

    def _split_fixed(self) -> str:
        return self.RED_SCRIPT.replace(
            "$Dirs | ForEach", "$Dirs = @($Dirs | ForEach-Object { $_ -split ',' })\n$Dirs | ForEach"
        )

    def _flagged(self, files: dict[str, str]) -> list[tuple[str, str]]:
        return [(v.script, v.param) for v in find_violations(files)]

    def test_red_array_variable_to_file_child_is_flagged_even_when_the_callee_splits(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "tools/x/run.ps1": "$dirs = @('a', 'b')\n& pwsh -NoProfile -File tools\\x\\trace.ps1 -Dirs $dirs -Top 3\n",
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))

    def test_red_quoted_argumentlist_tokens_with_array_variable_are_flagged(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "tools/x/run.ps1": "$Dirs = @('a', 'b')\nStart-Process pwsh -ArgumentList '-File','tools/x/trace.ps1','-Dirs',$Dirs\n",
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))

    def test_red_multiline_argument_array_naming_the_script_elsewhere_is_flagged(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "tools/x/run.ps1": (
                "$validator = Join-Path $repo 'tools\\x\\trace.ps1'\n$dirs = @('a', 'b')\n$argv = @(\n"
                "    '-NoProfile',\n    '-File',\n    $validator,\n    '-Dirs',\n    $dirs\n)\n"
            ),
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))

    def test_red_array_literal_and_quoted_comma_token_callers_are_flagged(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "docs/lit.md": "pwsh -File tools/x/trace.ps1 -Dirs @('a','b')\n",
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))
        files = {
            "tools/x/trace.ps1": self.RED_SCRIPT,
            "tools/x/run.ps1": "Start-Process pwsh -ArgumentList '-File','tools/x/trace.ps1','-Dirs','a,b'\n",
        }
        self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files))

    def test_green_joined_scalar_and_unknown_variable_callers_are_not_flagged(self):
        files = {
            "tools/x/trace.ps1": self._split_fixed(),
            "tools/x/run.ps1": (
                "$dirs = @('a', 'b')\n$one = 'a'\n"
                "& pwsh -File tools\\x\\trace.ps1 -Dirs ($dirs -join ',')\n"
                "& pwsh -File tools\\x\\trace.ps1 -Dirs $one\n"
                "& pwsh -File tools\\x\\trace.ps1 -Dirs $unknown\n"
            ),
        }
        self.assertEqual([], self._flagged(files))

    def test_red_unrelated_split_beside_the_parameter_does_not_clear_it(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        for nearby in (
            "Write-Host $Dirs\n$y = $z -split ';'\n$Dirs | ForEach",
            "Write-Host $Dirs; $y = $z -split ';'\n$Dirs | ForEach",
            "$Dirs = $env:DIRS -split ','\n$Dirs | ForEach",
        ):
            files = {"tools/x/trace.ps1": self.RED_SCRIPT.replace("$Dirs | ForEach", nearby), **callers}
            self.assertEqual([("tools/x/trace.ps1", "Dirs")], self._flagged(files), nearby)

    def test_green_multiline_normalisation_bound_to_the_parameter_clears_it(self):
        callers = {"docs/how.md": "pwsh -File tools/x/trace.ps1 -Dirs a,b\n"}
        for fixed in (
            "$Dirs = @($Dirs |\n    ForEach-Object { $_ -split ',' } |\n    Where-Object { $_ })\n$Dirs | ForEach",
            "$Dirs = @($Dirs | ForEach-Object {\n    $_ -split ','\n})\n$Dirs | ForEach",
            "$Dirs = $Dirs `\n    -split ','\n$Dirs | ForEach",
        ):
            files = {"tools/x/trace.ps1": self.RED_SCRIPT.replace("$Dirs | ForEach", fixed), **callers}
            self.assertEqual([], self._flagged(files), fixed)

    def test_reverting_each_guarded_311_fix_in_memory_is_flagged(self):
        files = _live_files()
        self.assertEqual([], [v for v in _live_found() if (v.script, v.param) not in KNOWN_OPEN])
        fixes = (
            (
                "tools/profiling/export-release-cuda-dogfood-kit.ps1",
                "-CdngCodecs ($DngCodecs -join ',') `\n",
                "-CdngCodecs $DngCodecs `\n",
                ("tools/profiling/run-local-cuda-playback-dng-smoke.ps1", "CdngCodecs"),
            ),
            (
                "tools/profiling/invoke-ultramagnus-p3-evidence.ps1",
                "(@(`$evidenceGitStatus) -join [string][char]10),",
                "`$evidenceGitStatus,",
                ("tools/profiling/run-ultramagnus-p3-validation.ps1", "EvidenceGitStatus"),
            ),
            (
                "tools/profiling/filmstrip-balance-trace.ps1",
                "$Dirs = @($Dirs | ForEach-Object { if (Test-Path -LiteralPath $_) { $_ } else { $_ -split ',' } } | ForEach-Object { $_.Trim() } | Where-Object { $_ })",
                "",
                ("tools/profiling/filmstrip-balance-trace.ps1", "Dirs"),
            ),
        )
        for path, fixed, reverted, expected in fixes:
            self.assertIn(fixed, files[path], f"{path}: the #311 fix moved; update this test with it")
            mutated = dict(files)
            mutated[path] = files[path].replace(fixed, reverted)
            self.assertIn(expected, self._flagged(mutated), path)

    def test_red_validateset_on_a_string_array_is_seen_and_a_split_first_launcher_is_not(self):
        red = "param(\n    [ValidateSet('a', 'b')]\n    [string[]]$Codecs = @('a')\n)\n"
        green = "param(\n    [string[]]$Codecs = @('a')\n)\n$Codecs = @($Codecs | ForEach-Object { $_ -split ',' })\n"
        self.assertEqual(["Codecs"], validateset_string_arrays(red))
        self.assertEqual([], validateset_string_arrays(green))
        self.assertEqual([red.rstrip("\n")], embedded_launchers("$s = @'\n" + red + "'@\n"))

    def test_generated_launchers_split_a_string_array_before_validating_it(self):
        kit = _live_files()["tools/profiling/export-release-cuda-dogfood-kit.ps1"]
        launchers = embedded_launchers(kit)
        self.assertEqual(1, len(launchers), "the kit script embeds RUN-CUDA-DOGFOOD.ps1 as a here-string")
        for script in launchers:
            self.assertEqual([], validateset_string_arrays(script))
            for name, _line, end in script_string_array_params(script):
                self.assertTrue(is_normalised(script, name, end), name)

    def test_param_block_reader_ignores_comments_functions_and_nested_parens(self):
        text = (
            "[CmdletBinding()]\nparam(\n    # [string[]]$Commented,\n"
            "    [ValidatePattern('^(a|b)$')][string]$Plain,\n    [string[]]$Real = @('x')\n)\n"
            "function F {\n    param([string[]]$InFunction)\n}\n"
        )
        self.assertEqual(["Real"], [name for name, _line, _end in script_string_array_params(text)])

    def test_live_tree_has_no_unexplained_violations(self):
        found = _live_found()
        unexplained = [v for v in found if (v.script, v.param) not in KNOWN_OPEN]
        self.assertEqual([], unexplained, "\n".join(v.render() for v in unexplained))

    def test_known_open_entries_are_still_violations(self):
        still = {(v.script, v.param) for v in _live_found()}
        stale = sorted(set(KNOWN_OPEN) - still)
        self.assertEqual([], stale, f"fixed? delete these KNOWN_OPEN entries: {stale}")


if __name__ == "__main__":
    unittest.main()
