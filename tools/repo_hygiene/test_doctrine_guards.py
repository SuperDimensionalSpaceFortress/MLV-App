"""Doctrine guards: adopted fleet traps turned into checks that hosted CI runs.

Prose traps on the doctrine bus did not stop recurrence (bus RECEIPT 79f616c: 0 of
20 traps became guards; this board re-diagnosed an acked PowerShell 5.1 leak for
about 3 h on 2026-10-06). Each trap this board adopts and can check mechanically
gets one REGISTRY entry below: its id, the bus commit of the trap card it enforces,
a one-line trap statement and the check. Every guard has a RED fixture (input the
check must FAIL), a GREEN fixture (input it must PASS) and a live-tree assertion.
Template: agent-bridge DOCTRINE-GUARDS-1N (bus receipt 3da63d1) and airmypc G1-G3
(bus receipt e0ed577).

To adopt a trap, add a guard here, or write "Guard: none yet" with the reason in
the fold record (agents/doctrine-consumer.md).

Collected by the Repo Hygiene Python jobs through
``unittest discover -s tools/repo_hygiene -p "test_*.py" -t .``
(tools/repo_hygiene/ci_unittest_shard.py); no workflow step names this file.

NON-PROMISES:
- The PowerShell guards judge the PowerShell AST only (Parser.ParseFile); there is
  no regex over script text, so a trap pattern inside a comment or string is not
  flagged, and a file that does not parse fails the check as could-not-check.
- Only files git tracks are checked; untracked and user-scope files are not
  (user-scope hooks must be proven to fire by a receipt).
- Only the traps in REGISTRY are guarded.
- DG-GIT-PATHLIST reads Python by AST (the string constants of one list, tuple or call must
  carry -z or core.quotepath=false) and PowerShell by one source line with its comment tokens
  blanked first (the PowerShell tokenizer decides what a comment is: a -z or core.quotepath=false
  that sits only in a `#` or `<# #>` comment does not count, and a `#` inside a string is not a
  comment; the remaining text is still split into words by a regex, so a path-list command that
  is not spelled on one source line, and a command assembled from variables or one joined string,
  are not seen). A script that does not parse cannot be tokenized and fails as could-not-check. Test files are not scanned. A call that
  needs no path text (exit code, emptiness, a count) is listed in GIT_PATHLIST_ALLOW with its
  reason; git_paths() in tools/coordination/doctrine_outbox.py adds -z itself. Every ls-tree
  form (long form `ls-tree -r` included; only --object-only is exempt) and `status --short|-s|-sb`
  count, as well as --name-only, --name-status and --porcelain.
  Under tools/repo_hygiene/ the same guard also flags a Python subprocess call that lists git paths with
  text=True (or universal_newlines=True) and no encoding=, even with -z: the locale codec mangles a
  UTF-8 path (DURATION-SCAN-UTF8-PATHS-1). Calls that build the argument list elsewhere are not seen.
- DG-PS-NULL-COMPARE flags only a literal ``$null`` on the RIGHT of
  -eq/-ne/-ceq/-cne/-ieq/-ine, seen through parentheses and a one-statement ``$( )``
  (``$x -eq $(${null})``); a null reached through a variable, a call or a longer ``$( )`` is not seen.
  The ``-eq $false`` tri-state form on a possibly-null
  value (tools/profiling/compare-machine-perf.ps1, run-release-cuda-playback-ab.ps1)
  cannot be decided statically and is out of scope; review it by hand.
- PS-ONE-TRAP counts catch-all TrapStatementAst nodes per enclosing block (the script's
  begin/process/end, or an if/foreach/try body) and flags EVERY block holding more than
  one. A catch-all is an untyped trap or one typed [System.Exception] or
  [System.Management.Automation.RuntimeException]. A type name is resolved as written, under
  the implicit System. and under every `using namespace` of the script (so [Exception],
  [Management.Automation.RuntimeException], and [RuntimeException] beside
  `using namespace System.Management.Automation` are catch-alls); it counts when ANY of those
  candidates is a catch-all, so an ambiguous name is over-flagged, never under-flagged.
  No type is loaded and no alias beyond `using namespace` is followed; a name that no
  namespace in the script completes to a catch-all (pwsh itself refuses it: Unable to find
  type) is not counted. A trap
  of any other type and a trap in another block are other scopes and are not counted, so two
  traps of the same specific type (trap [IOException] twice) are not flagged; traps inside a
  function or script block are not searched.

If pwsh is not on PATH the AST guards SKIP with a reason; on a GitHub Actions
runner (whose images carry pwsh) a missing pwsh FAILS instead.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
PWSH_TIMEOUT_S = 300

# Copied from agent-bridge tools/doctrine_guards.py at 7a2acc8c (DOCTRINE-GUARDS-1N,
# bus receipt 3da63d1), whose source is https://code.claude.com/docs/en/hooks,
# fetched 2026-10-06. Claude Code ignores an unknown key under "hooks" SILENTLY.
HOOK_EVENTS: frozenset[str] = frozenset((
    "SessionStart", "Setup", "UserPromptSubmit", "UserPromptExpansion",
    "PreToolUse", "PermissionRequest", "PermissionDenied", "PostToolUse",
    "PostToolUseFailure", "PostToolBatch", "Notification", "MessageDisplay",
    "SubagentStart", "SubagentStop", "TaskCreated", "TaskCompleted", "Stop",
    "StopFailure", "TeammateIdle", "InstructionsLoaded", "ConfigChange",
    "CwdChanged", "DirectoryAdded", "FileChanged", "WorktreeCreate",
    "WorktreeRemove", "PreCompact", "PostCompact", "PreModelSwitch",
    "PostModelSwitch", "Elicitation", "ElicitationResult", "SessionEnd",
))


class CouldNotCheck(Exception):
    """The check could not run; a test that hits this fails, it never passes."""


@dataclass(frozen=True)
class Violation:
    guard: str
    path: str
    line: int
    detail: str

    def render(self) -> str:
        return f"{self.guard} {self.path}:{self.line}: {self.detail}"


# ---------------------------------------------------------------------------
# PowerShell AST scan: ONE pwsh process for any number of files.
# Reads a JSON list of absolute paths from -InPath and writes one
# {path, errors, nullRight, trapBlocks, comments} object per path to -OutPath.
# ---------------------------------------------------------------------------
_PS_SCAN = r"""
param([Parameter(Mandatory)][string]$InPath, [Parameter(Mandatory)][string]$OutPath)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$enc = New-Object System.Text.UTF8Encoding($false)
$paths = @([IO.File]::ReadAllText($InPath, $enc) | ConvertFrom-Json)
$ops = @('Ieq', 'Ine', 'Ceq', 'Cne')
$catchAll = @('system.exception', 'system.management.automation.runtimeexception')
$nullName = '^(?:(?:global|script|local|private):)?null$'
function Unwrap($e) {
    while ($true) {
        if ($e -is [System.Management.Automation.Language.ParenExpressionAst]) { $e = $e.Pipeline; continue }
        if ($e -is [System.Management.Automation.Language.PipelineAst] -and $e.PipelineElements.Count -eq 1) {
            $e = $e.PipelineElements[0]; continue
        }
        if ($e -is [System.Management.Automation.Language.CommandExpressionAst]) { $e = $e.Expression; continue }
        if ($e -is [System.Management.Automation.Language.SubExpressionAst] -and $e.SubExpression.Statements.Count -eq 1) {
            $e = $e.SubExpression.Statements[0]; continue
        }
        return $e
    }
}
function IsCatchAllType([string]$name, $prefixes) {
    foreach ($prefix in $prefixes) { if ($catchAll -contains ($prefix + $name)) { return $true } }
    return $false
}
$results = New-Object System.Collections.Generic.List[object]
foreach ($p in $paths) {
    $tokens = $null; $perr = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile([string]$p, [ref]$tokens, [ref]$perr)
    $errs = @(foreach ($pe in @($perr)) { if ($null -ne $pe) { '{0}: {1}' -f $pe.Extent.StartLineNumber, $pe.Message } })
    $nulls = @()
    $hits = $ast.FindAll({
        param($n)
        $n -is [System.Management.Automation.Language.BinaryExpressionAst] -and ($ops -contains $n.Operator.ToString())
    }, $true)
    foreach ($h in @($hits)) {
        $r = Unwrap $h.Right
        if ($r -is [System.Management.Automation.Language.VariableExpressionAst] -and -not $r.Splatted -and
            $r.VariablePath.UserPath -match $nullName) {
            $nulls += $h.ErrorPosition.StartLineNumber
        }
    }
    # PowerShell runs only the first catch-all trap per scope (bus eee0f66). A scope is one statement
    # block (named block or if/foreach/try body), so count catch-all traps per parent block and report
    # EVERY block holding more than one; a catch-all is an untyped trap or one typed with $catchAll, a
    # trap of any other type is a different dispatch, and a function or script block is not entered.
    # A trap type is resolved as native pwsh does: the name as written, under the implicit System. and
    # under every `using namespace`; it is a catch-all when ANY of those candidates is one (fail closed).
    $prefixes = @('', 'system.') + @($ast.UsingStatements | Where-Object { $_.UsingStatementKind -eq 'Namespace' } |
        ForEach-Object { $_.Name.Value.ToLowerInvariant() + '.' })
    $traps = @($ast.FindAll({
        param($n)
        $n -is [System.Management.Automation.Language.TrapStatementAst] -and
            ($null -eq $n.TrapType -or (IsCatchAllType $n.TrapType.TypeName.FullName.ToLowerInvariant() $prefixes))
    }, $false) | Group-Object { $_.Parent.Extent.StartOffset } | Where-Object { $_.Count -gt 1 } |
        ForEach-Object {
            $lines = @($_.Group | ForEach-Object { $_.Extent.StartLineNumber } | Sort-Object)
            [ordered]@{ count = $lines.Count; line = [int]$lines[1] }
        })
    # Comment tokens, so a caller can blank them: (start line, start column, end line, end column), 1-based,
    # columns in UTF-16 units, end column exclusive.
    $comments = @(foreach ($t in @($tokens)) {
        if ($t.Kind -eq [System.Management.Automation.Language.TokenKind]::Comment) {
            [ordered]@{ sl = $t.Extent.StartLineNumber; sc = $t.Extent.StartColumnNumber
                        el = $t.Extent.EndLineNumber; ec = $t.Extent.EndColumnNumber }
        }
    })
    $results.Add([ordered]@{ path = [string]$p; errors = @($errs); nullRight = @($nulls); trapBlocks = @($traps)
                             comments = @($comments) })
}
[IO.File]::WriteAllText($OutPath, (ConvertTo-Json -InputObject $results.ToArray() -Depth 6 -Compress), $enc)
"""


def find_pwsh() -> str | None:
    return shutil.which("pwsh")


_SCAN_CACHE: dict[tuple[str, tuple[str, ...]], dict[str, dict]] = {}


def ast_scan(root: Path, rel_paths: list[str]) -> dict[str, dict]:
    """Parse every file with the PowerShell AST in one pwsh process; keyed by rel path.

    The live-tree result is cached, so both AST guards share ONE pwsh process.
    """
    if not rel_paths:
        return {}
    key = (str(root.resolve()), tuple(rel_paths))
    if root.resolve() == REPO_ROOT and key in _SCAN_CACHE:
        return _SCAN_CACHE[key]
    out = _ast_scan_uncached(root, rel_paths)
    if root.resolve() == REPO_ROOT:
        _SCAN_CACHE[key] = out
    return out


def _ast_scan_uncached(root: Path, rel_paths: list[str]) -> dict[str, dict]:
    exe = find_pwsh()
    if exe is None:
        raise CouldNotCheck("pwsh (PowerShell 7+) was not found on PATH")
    abs_by_rel = {rel: str((root / rel).resolve()) for rel in rel_paths}
    with tempfile.TemporaryDirectory(prefix="doctrine-guards-") as tmp:
        script, inp, outp = (Path(tmp) / n for n in ("scan.ps1", "in.json", "out.json"))
        script.write_text(_PS_SCAN, encoding="utf-8")
        inp.write_text(json.dumps(list(abs_by_rel.values())), encoding="utf-8")
        try:
            proc = subprocess.run(
                [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script),
                 "-InPath", str(inp), "-OutPath", str(outp)],
                capture_output=True, check=False, timeout=PWSH_TIMEOUT_S, cwd=tmp)
        except (OSError, subprocess.SubprocessError) as exc:
            raise CouldNotCheck(f"pwsh could not run: {exc}") from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout).decode("utf-8", "replace").strip()[:600]
            raise CouldNotCheck(f"pwsh scan failed ({proc.returncode}): {err}")
        try:
            results = json.loads(outp.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CouldNotCheck(f"pwsh scan produced no usable result: {exc}") from exc
    if isinstance(results, dict):
        results = [results]
    by_abs = {}
    for res in results if isinstance(results, list) else []:
        if isinstance(res, dict) and set(res) == {"path", "errors", "nullRight", "trapBlocks", "comments"}:
            by_abs[res["path"]] = res
    out: dict[str, dict] = {}
    for rel, absolute in abs_by_rel.items():
        res = by_abs.get(absolute)
        if res is None:
            raise CouldNotCheck(f"pwsh scan returned no result for {rel}")
        if res["errors"]:
            raise CouldNotCheck(f"{rel} does not parse as PowerShell: {res['errors'][0]}")
        out[rel] = res
    return out


# ---------------------------------------------------------------------------
# File selection (git-tracked only) and the guard checks.
# ---------------------------------------------------------------------------
def tracked_files(root: Path) -> list[str]:
    try:
        proc = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                              capture_output=True, check=False, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CouldNotCheck(f"git ls-files could not run: {exc}") from exc
    if proc.returncode != 0:
        raise CouldNotCheck(f"git ls-files failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return [p for p in proc.stdout.decode("utf-8", "surrogateescape").split("\0") if p]


def is_powershell(rel: str) -> bool:
    return PurePosixPath(rel).suffix.lower() in (".ps1", ".psm1")


def is_claude_settings(rel: str) -> bool:
    p = PurePosixPath(rel)
    return p.parent.name == ".claude" and p.name.startswith("settings") and p.suffix == ".json"


def check_ps_null_compare(root: Path, rel_paths: list[str]) -> list[Violation]:
    scan = ast_scan(root, rel_paths)
    return [Violation("DG-PS-NULL-COMPARE", rel, line, "$null on the right of -eq/-ne (put $null on the left)")
            for rel in rel_paths for line in scan[rel]["nullRight"]]


def check_ps_one_trap(root: Path, rel_paths: list[str]) -> list[Violation]:
    scan = ast_scan(root, rel_paths)
    out = []
    for rel in rel_paths:
        for block in scan[rel]["trapBlocks"]:
            out.append(Violation("PS-ONE-TRAP", rel, block["line"],
                                 f"{block['count']} catch-all traps in one block; PowerShell runs only the first"))
    return out


def check_hook_event(root: Path, rel_paths: list[str]) -> list[Violation]:
    out = []
    for rel in rel_paths:
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise CouldNotCheck(f"{rel} is not readable JSON: {exc}") from exc
        hooks = data.get("hooks") if isinstance(data, dict) else None
        if hooks is None:
            continue
        if not isinstance(hooks, dict):
            out.append(Violation("DG-HOOK-EVENT", rel, 1, '"hooks" is not an object'))
            continue
        for key in hooks:
            if key not in HOOK_EVENTS and not key.startswith("_comment"):
                out.append(Violation("DG-HOOK-EVENT", rel, 1, f"unknown hook event {key!r} (fails silent)"))
    return out


# ---------------------------------------------------------------------------
# DG-GIT-PATHLIST (bus de09cae): git quotes a non-ASCII path in its plain path-list output
# (core.quotepath defaults to true), so a prefix or equality test on that output misses it.
# ---------------------------------------------------------------------------
# Display-only and emptiness-only call sites, by (tracked path, needle that must occur in the
# flagged call's source text, why it is safe). Each entry is checked for staleness against the
# live tree, so a call site that moves or is fixed leaves a failing entry behind, not a silent one.
GIT_PATHLIST_ALLOW: tuple[tuple[str, str, str], ...] = (
    ("tools/build-release.ps1", "$SourceRoot status --porcelain",
     "dirty-or-clean test on 'any output'; the lines are only echoed back to the console"),
    ("tools/coordination/Invoke-Workstream.ps1", "$existingWt status --porcelain",
     "refuses on a non-zero count and names the lines in a message; no path is tested or opened"),
    ("tools/coordination/Retire-LaneWorktree.ps1", "'status', '--porcelain', '-uall'",
     "refuses on a non-zero count and quotes the first line in the reason; the ignored-entry listing, "
     "whose paths ARE tested and moved, carries core.quotepath=false"),
    ("tools/gen-buildinfo.ps1", "GitOut @('status','--porcelain')",
     "dirty flag from 'any output'; the text is never read as a path"),
    ("tools/profiling/export-release-cuda-dogfood-kit.ps1", "-C $root status --porcelain",
     "dirty flag from a count; the text is never read as a path"),
    ("tools/profiling/export-release-cuda-dogfood-kit.ps1", "-C $root status --short --branch",
     "the lines are echoed into the kit manifest as display text; no path is tested or opened"),
    ("tools/profiling/invoke-ultramagnus-cdng-export-evidence.ps1", "-C $repo status --short --branch",
     "only the '## ' branch-line prefix is tested to tell dirty from clean; the lines are echoed in the refusal and the evidence"),
    ("tools/profiling/invoke-ultramagnus-p3-evidence.ps1", "-C $repo status --short --branch",
     "only the '## ' branch-line prefix is tested to tell dirty from clean; the lines are echoed in the refusal and the evidence"),
    ("tools/profiling/package-local-cuda-proof-result.ps1", "-C $Repo status --short --branch",
     "the lines are echoed into the packaged result as display text; no path is tested or opened"),
    ("tools/profiling/run-ultramagnus-p3-validation.ps1", "-C $Repo status --short --branch",
     "the lines are recorded as evidence text and only the '## ' branch-line prefix is tested; no path is tested or opened"),
    ("tools/release/build_stamp.py", 'git(root, "status", "--porcelain")',
     "refuses on 'any output' and echoes it in the error; no path is tested"),
    ("tools/repo_hygiene/brokered_closeout.py", '"status", "--porcelain=v1", "--", path',
     "one already-known path; only 'any output' is tested, the text is never parsed"),
    ("tools/session-checkpoint.py", '"diff", "--name-only"',
     "the dirty-file list is written to a checkpoint for display and compared only against its own "
     "earlier snapshot in the same quoted form; no prefix test"),
    ("tools/session-checkpoint.py", '"ls-files", "--others"',
     "the dirty-file list is written to a checkpoint for display and compared only against its own "
     "earlier snapshot in the same quoted form; no prefix test"),
)


def is_git_pathlist_scanned(rel: str) -> bool:
    """Python and PowerShell tool code. Test files are not scanned: they list scratch repos they
    populate with ASCII names, and they are the guard's own fixtures' neighbours, not its subject."""
    p = PurePosixPath(rel)
    if p.suffix.lower() not in (".py", ".ps1", ".psm1"):
        return False
    name = p.name.lower()
    is_test = ("tests" in p.parts[:-1] or name.startswith("test_") or name.endswith("_test.py")
               or name.endswith(".tests.ps1"))
    return not is_test


_GIT_STATUS_SHORT = frozenset(("--short", "-s", "-sb", "-bs"))


def _git_pathlist_kind(tokens: set[str]) -> str | None:
    """Which path-listing git command a set of argument tokens spells, or None."""
    if "ls-files" in tokens and "--error-unmatch" not in tokens:  # --error-unmatch is read by exit code
        return "ls-files"
    # Every ls-tree form prints a path (long form: `<mode> <type> <id> TAB <path>`) unless it prints ids only.
    if "ls-tree" in tokens and "--object-only" not in tokens:
        return "ls-tree"
    if tokens & {"diff", "log", "diff-tree", "show", "stash"} and tokens & {"--name-only", "--name-status"}:
        return "diff/log/diff-tree --name-only|--name-status"
    if "status" in tokens and (tokens & _GIT_STATUS_SHORT or any(t.startswith("--porcelain") for t in tokens)):
        return "status --short|--porcelain"
    return None


def _git_pathlist_safe(tokens: set[str]) -> bool:
    return "-z" in tokens or any("core.quotepath=false" in t for t in tokens)


_PS_TOKEN = re.compile(r"--?[\w=.:-]+|[\w=.:-]+")
# Calls to these helpers add `-z` themselves (tools/coordination/doctrine_outbox.py git_paths).
_GIT_PATHLIST_HELPERS = frozenset(("git_paths",))


_PS_NEWLINE = re.compile(r"\r\n|\n|\r")
_UTF16_SPACE = " ".encode("utf-16-le")
_BOM = chr(0xFEFF)


def _ps_source_lines(text: str) -> list[str]:
    """Split as the PowerShell tokenizer numbers lines (CRLF, LF or CR); the BOM is not part of line 1."""
    return _PS_NEWLINE.split(text[1:] if text.startswith(_BOM) else text)


def _ps_code_lines(rel: str, text: str, comments: list[dict]) -> list[str]:
    """The script's source lines with every PowerShell comment token blanked to spaces.

    `comments` are the tokenizer's Comment extents (1-based lines, UTF-16 columns, end exclusive), so a
    `#` inside a string or here-string is not a comment, and a block comment may span lines. A position
    that does not start a comment means the lines disagree with the tokenizer: could-not-check."""
    lines = _ps_source_lines(text)
    for c in comments:
        for number in range(c["sl"], c["el"] + 1):
            if not 1 <= number <= len(lines):
                raise CouldNotCheck(f"{rel}: comment token line {number} is outside the file")
            units = lines[number - 1].encode("utf-16-le", "surrogatepass")
            begin = (c["sc"] - 1) * 2 if number == c["sl"] else 0
            end = min((c["ec"] - 1) * 2, len(units)) if number == c["el"] else len(units)
            if number == c["sl"] and units[begin:begin + 2] not in ("#".encode("utf-16-le"), "<".encode("utf-16-le")):
                raise CouldNotCheck(f"{rel}:{number}: comment token does not start at a '#' or '<#'")
            units = units[:begin] + _UTF16_SPACE * ((end - begin) // 2) + units[end:]
            lines[number - 1] = units.decode("utf-16-le", "surrogatepass")
    return lines


def scan_git_pathlist(root: Path, rel_paths: list[str]) -> list[tuple[Violation, str]]:
    """Every git path-list invocation without `-z` or `core.quotepath=false`, with its source text.

    Python: the string constants of one list/tuple/call spell the command, so `-z` must sit in the
    same list or call. PowerShell: one source line spells it, read with its comment tokens blanked
    (the PowerShell tokenizer decides what a comment is, see _ps_code_lines)."""
    found: list[tuple[Violation, str]] = []
    texts: dict[str, str] = {}
    for rel in rel_paths:
        try:
            texts[rel] = (root / rel).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise CouldNotCheck(f"{rel} is not readable text: {exc}") from exc
    # Only a script that holds a '#' (every comment has one) and a line spelling a path-list command needs
    # the tokenizer (a block comment's inner lines carry no '#' of their own); one pwsh process then
    # tokenizes all of them.
    needs_tokens = [rel for rel, text in texts.items() if not rel.lower().endswith(".py") and "#" in text
                    and any(_git_pathlist_kind(set(_PS_TOKEN.findall(line))) for line in _ps_source_lines(text))]
    comments = ast_scan(root, needs_tokens) if needs_tokens else {}
    for rel in rel_paths:
        text = texts[rel]
        if rel.lower().endswith(".py"):
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                raise CouldNotCheck(f"{rel} does not parse as Python: {exc}") from exc
            for node in ast.walk(tree):
                if isinstance(node, (ast.List, ast.Tuple)):
                    parts = node.elts
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id in _GIT_PATHLIST_HELPERS:
                        continue
                    parts = node.args
                else:
                    continue
                tokens = {p.value for p in parts if isinstance(p, ast.Constant) and isinstance(p.value, str)}
                kind = _git_pathlist_kind(tokens)
                if kind and not _git_pathlist_safe(tokens):
                    found.append((Violation("DG-GIT-PATHLIST", rel, node.lineno,
                                            f"{kind} without -z or core.quotepath=false"),
                                  ast.get_source_segment(text, node) or ""))
        else:
            source = _ps_source_lines(text)
            code = _ps_code_lines(rel, text, comments[rel]["comments"]) if rel in comments else source
            for number, line in enumerate(code, 1):
                tokens = set(_PS_TOKEN.findall(line))
                kind = _git_pathlist_kind(tokens)
                if kind and not _git_pathlist_safe(tokens):
                    found.append((Violation("DG-GIT-PATHLIST", rel, number,
                                            f"{kind} without -z or core.quotepath=false"), source[number - 1]))
    return found


# DURATION-SCAN-UTF8-PATHS-1 (bus TRAPS.md "A locale decode turned a non-ASCII worktree path into a
# path that named nothing"): git prints UTF-8, and text=True with no encoding decodes with the locale
# codec (cp1252 on Windows outside UTF-8 mode), so even a `-z` path list arrives as mojibake.
GIT_TEXT_DECODE_SCOPE = "tools/repo_hygiene/"
_SUBPROCESS_READERS = frozenset(("run", "check_output", "Popen"))


def scan_git_text_decode(root: Path, rel_paths: list[str]) -> list[Violation]:
    """A subprocess call under tools/repo_hygiene/ that lists git paths with text=True (or
    universal_newlines=True) and no `encoding=`. Calls whose argument list is built elsewhere are not seen."""
    found: list[Violation] = []
    for rel in rel_paths:
        if not rel.startswith(GIT_TEXT_DECODE_SCOPE) or not rel.lower().endswith(".py"):
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, ValueError, SyntaxError) as exc:
            raise CouldNotCheck(f"{rel} is not readable Python: {exc}") from exc
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and node.args and isinstance(node.args[0], (ast.List, ast.Tuple))):
                continue
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name not in _SUBPROCESS_READERS:
                continue
            tokens = {p.value for p in node.args[0].elts if isinstance(p, ast.Constant) and isinstance(p.value, str)}
            kind = _git_pathlist_kind(tokens)
            kwargs = {k.arg: k.value for k in node.keywords if k.arg}
            text_mode = any(isinstance(kwargs.get(k), ast.Constant) and kwargs[k].value is True
                            for k in ("text", "universal_newlines"))
            if "git" in tokens and kind and text_mode and "encoding" not in kwargs:
                found.append(Violation("DG-GIT-PATHLIST", rel, node.lineno,
                                       f"{kind} read with text=True and no encoding= (locale codec mangles non-ASCII paths)"))
    return found


def check_git_pathlist(root: Path, rel_paths: list[str]) -> list[Violation]:
    quoted = [v for v, source in scan_git_pathlist(root, rel_paths)
              if not any(v.path == path and needle in source for path, needle, _why in GIT_PATHLIST_ALLOW)]
    return quoted + scan_git_text_decode(root, rel_paths)


@dataclass(frozen=True)
class Guard:
    id: str
    bus_trap: str  # bus commit sha of the trap card this guard enforces
    trap: str
    selects: Callable[[str], bool]
    check: Callable[[Path, list[str]], list[Violation]]
    needs_pwsh: bool


REGISTRY: tuple[Guard, ...] = (
    Guard("DG-PS-NULL-COMPARE", "99a1354",
          "`$x -eq $null` with an array $x filters the array instead of testing for null",
          is_powershell, check_ps_null_compare, True),
    Guard("DG-HOOK-EVENT", "99a1354",
          "a hook registered under a misspelt event name never runs and raises no error",
          is_claude_settings, check_hook_event, False),
    Guard("PS-ONE-TRAP", "eee0f66",
          "PowerShell runs only the FIRST catch-all trap (untyped, [System.Exception] or [RuntimeException]) in a block; a second one is dead code",
          is_powershell, check_ps_one_trap, True),
    Guard("DG-GIT-PATHLIST", "de09cae",
          "git quotes a non-ASCII path in its plain path-list output, so a path-prefix test on it misses the path",
          is_git_pathlist_scanned, check_git_pathlist, True),
)
GUARDS = {g.id: g for g in REGISTRY}


def run_guard(guard: Guard, root: Path, rel_paths: list[str] | None = None) -> list[Violation]:
    """Run one guard over rel_paths, or over every git-tracked file it selects."""
    if rel_paths is None:
        rel_paths = [p for p in tracked_files(root) if guard.selects(p)]
    return guard.check(root, rel_paths)


# ---------------------------------------------------------------------------
# Fixtures and tests.
# ---------------------------------------------------------------------------
FIXTURES: dict[str, dict[str, dict[str, str]]] = {
    "DG-PS-NULL-COMPARE": {
        "red": {
            "a.ps1": "$x = @(1, $null)\nif ($x -eq $null) { 'n' }\n",
            "b.ps1": "$v = @(1) | Where-Object { $_ -ne $null }\n",
            "c.psm1": "function F($y) {\n    if ($y -cne ($null)) { 1 }\n    if ($y -ieq $script:null) { 2 }\n}\n",
            "d.ps1": "if ($x -eq $(${null})) { 1 }\nif ($x -ne $($null)) { 2 }\nif ($x -ieq $(($null))) { 3 }\n",
        },
        "green": {
            "a.ps1": "$x = @(1, $null)\nif ($null -eq $x) { 'n' }\n$v = @(1) | Where-Object { $null -ne $_ }\n",
            "b.ps1": "# $x -eq $null in a comment is not code\n$s = 'if ($x -ne $null) {}'\n"
                     "$h = @'\n$y -eq $null\n'@\n",
            "c.ps1": "$ok = $true\nif ($ok -eq $false) { 'tri-state form is out of scope' }\n"
                     "$nullish = 0\nif ($ok -ne $nullish) { 1 }\n",
            "d.ps1": "$x = @(1)\nif ($x -eq $($y)) { 1 }\nif ($null -eq $(${x})) { 2 }\n",
        },
    },
    "DG-HOOK-EVENT": {
        "red": {
            ".claude/settings.json": json.dumps({"hooks": {"Start": [], "SessionStart": []}}),
            "sub/.claude/settings.local.json": json.dumps({"hooks": {"PretoolUse": []}}),
        },
        "green": {
            ".claude/settings.json": json.dumps(
                {"hooks": {"_comment_x": "note", "SessionStart": [], "PreToolUse": [], "Stop": []}}),
            "sub/.claude/settings.local.json": json.dumps({"permissions": {"allow": []}}),
        },
    },
    "DG-GIT-PATHLIST": {
        # Bus de09cae: `agents/caf<e-acute>.md` read as `"agents/caf\303\251.md"` and passed a prefix test.
        "red": {
            "tools/a.py": "import subprocess\n"
                          "def f(repo):\n"
                          "    subprocess.run(['git', '-C', repo, 'ls-files'])\n"
                          "    subprocess.run(['git', 'diff-tree', '--no-commit-id', '--name-only', '-r', 'HEAD'])\n"
                          "    git(repo, 'log', '--name-status')\n"
                          "    run_git(repo, ['status', '--porcelain=v1'])\n"
                          "    git(repo, 'ls-tree', '-r', '--name-only', 'HEAD')\n"
                          "    git(repo, 'ls-tree', '-r', 'HEAD', '--', 'legs')\n"
                          "    git(repo, 'ls-tree', '--full-tree', '-r', 'HEAD')\n"
                          "    git(repo, 'status', '--short')\n"
                          "    run_git(repo, ['status', '-sb'])\n",
            "tools/b.ps1": "$staged = @(& git -C $root diff --cached --name-only)\n"
                           "$d = @(Run-Git $wd @('status', '--porcelain', '-uall'))\n"
                           "$t = Invoke-Git -GitArgs @('ls-tree', '-r', $commit, '--', $dir)\n"
                           "$s = @(& git -C $root status --short --branch 2>$null)\n"
                           # DG-GIT-PATHLIST-PS-COMMENT-TOKEN-1: a -z or core.quotepath=false that sits in a comment is not an argument.
                           "& git -C $root diff --name-only # add -z later\n"
                           "$q = & git -C $root ls-files # core.quotepath=false\n"
                           "& git -C $root diff --name-only <# -z #>\n"
                           "& git -C $root diff --name-only <# multi-line\n"
                           "-z #>\n",
            # DURATION-SCAN-UTF8-PATHS-1: -z is present, but text=True decodes the UTF-8 paths with the locale codec.
            "tools/repo_hygiene/c.py": "import subprocess\n"
                                       "def f(repo):\n"
                                       "    subprocess.run(['git', '-C', repo, 'ls-files', '-z'], capture_output=True, text=True)\n"
                                       "    subprocess.check_output(['git', '-C', repo, 'diff', '--name-only', '-z'], universal_newlines=True)\n"
                                       "    subprocess.run(['git', '-C', repo, 'ls-files', '-z'], capture_output=True, text=True, errors='replace')\n",
        },
        "green": {
            "tools/a.py": "import subprocess\n"
                          "def f(repo):\n"
                          "    subprocess.run(['git', '-C', repo, 'ls-files', '-z'])\n"
                          "    subprocess.run(['git', '-c', 'core.quotepath=false', 'diff-tree', '--name-only', '-r', 'HEAD'])\n"
                          "    git(repo, 'diff', '--name-only', '-z', 'a', 'b')\n"
                          "    git_paths(repo, 'ls-tree', '-r', '--name-only', 'HEAD')\n"
                          "    run_git(repo, ['ls-files', '--error-unmatch', '--', 'x'])\n"
                          "    run_git(repo, ['worktree', 'list', '--porcelain'])\n"
                          "    run_git(repo, ['blame', '--porcelain', 'f'])\n"
                          "    run_git(repo, ['stash', 'list'])\n"
                          "    note = 'git ls-files is only text here'\n"
                          "    git(repo, 'ls-tree', '-r', '-z', 'HEAD', '--', 'legs')\n"
                          "    git(repo, 'ls-tree', '--object-only', 'HEAD', 'legs/x.json')\n"
                          "    git(repo, 'status', '--short', '-z')\n"
                          "    run_git(repo, ['-c', 'core.quotepath=false', 'status', '-sb'])\n",
            "tools/b.ps1": "# git ls-files in a comment is not a call\n"
                           "$a = @(& git -C $root -c core.quotepath=false status --porcelain)\n"
                           "$b = @(& git -C $root diff --cached --name-only -z)\n"
                           "& git -C $root worktree list --porcelain\n"
                           "$t = Invoke-Git -GitArgs @('ls-tree', '-r', '-z', $commit, '--', $dir)\n"
                           "$s = @(& git -C $root -c core.quotepath=false status --short --branch 2>$null)\n"
                           "& git -C $root diff --name-only -z # the comment may say anything, -z is a real argument\n"
                           "$h = \"a # b\"; & git -C $root diff --name-only -z\n"
                           "$p = @(& git -C $root ls-files -z <# inline #> )\n"
                           "<#\n& git -C $root diff --name-only\n#>\n",
            "tools/repo_hygiene/c.py": "import subprocess\n"
                                       "def f(repo):\n"
                                       "    subprocess.run(['git', '-C', repo, 'ls-files', '-z'], capture_output=True, text=True, encoding='utf-8', errors='surrogateescape')\n"
                                       "    subprocess.run(['git', '-C', repo, 'ls-files', '-z'], capture_output=True)\n"
                                       "    subprocess.run(['git', '-C', repo, 'rev-parse', 'HEAD'], capture_output=True, text=True)\n",
            # Out of scope: only tools/repo_hygiene/** is held to the explicit-encoding rule.
            "tools/other.py": "import subprocess\n"
                              "subprocess.run(['git', 'ls-files', '-z'], capture_output=True, text=True)\n",
        },
    },
    "PS-ONE-TRAP": {
        "red": {
            "a.ps1": "trap { Write-Error 'first'; break }\n'body'\ntrap { 'second, never runs'; break }\n",
            "b.ps1": "trap { 'first catch-all' }\ntrap [System.IO.IOException] { 'typed, legal' }\ntrap { 'second catch-all, dead' }\n",
            "c.ps1": "param($p)\nbegin {\n    trap { 'first' }\n    trap { 'second, dead' }\n}\n",
            "d.ps1": "if ($true) {\n    trap { 'first' }\n    trap { 'second, dead' }\n    1\n}\n",
            # Two offending blocks in one file: both are reported, not only the busiest.
            "e.ps1": "param($p)\nbegin {\n    trap { 'b1' }\n    trap { 'b2, dead' }\n}\n"
                     "process {\n    trap { 'p1' }\n    trap { 'p2, dead' }\n}\n",
            # A typed catch-all matches before an untyped trap, whichever is written first.
            "f.ps1": "trap { 'untyped, dead' }\ntrap [System.Exception] { 'typed catch-all wins' }\n",
            "g.ps1": "trap [System.Exception] { 'first' }\ntrap [System.Management.Automation.RuntimeException] { 'second, dead' }\n",
            "h.ps1": "trap [Exception] { 'short form' }\ntrap { 'untyped, dead' }\n",
            # DG-TRAP-NAMESPACE-RESOLVED-1: native pwsh resolves a short name through `using namespace`
            # (RD native-trap.txt: only the first trap runs), so the guard must resolve it the same way.
            "i.ps1": "using namespace System.Management.Automation\ntrap [RuntimeException] { 'first' }\n"
                     "trap { 'untyped, dead' }\n",
            "j.ps1": "using namespace System.Management\ntrap [Automation.RuntimeException] { 'first' }\n"
                     "trap [Exception] { 'second, dead' }\n",
            "k.ps1": "using namespace System\ntrap [Exception] { 'first' }\ntrap { 'untyped, dead' }\n",
            "l.ps1": "using namespace System.IO\nusing namespace System.Management.Automation\n"
                     "trap [IOException] { 'typed, legal' }\ntrap [RuntimeException] { 'first' }\n"
                     "trap { 'untyped, dead' }\n",
        },
        "green": {
            "a.ps1": "trap { Write-Error 'only'; break }\nfunction F {\n    trap { 'own scope'; continue }\n    1\n}\n"
                     "$sb = { trap { continue }; 2 }\n",
            "b.ps1": "'no trap at all'\n",
            # One catch-all plus typed traps: legal, and not the bus trap eee0f66 (two catch-alls).
            "c.ps1": "trap { 'catch-all'; continue }\ntrap [System.IO.IOException] { 'typed'; continue }\n",
            "d.ps1": "trap [System.IO.IOException] { 'io'; continue }\ntrap [System.ArgumentException] { 'arg'; continue }\n",
            # A trap in an if/foreach/try body is that statement block's own scope, not the script's.
            "e.ps1": "trap { 'script scope'; continue }\nif ($true) {\n    trap { 'if-block scope'; continue }\n    1\n}\n"
                     "foreach ($i in 1) { trap { 'foreach scope'; continue }; $i }\n",
            "f.ps1": "param($p)\nbegin { trap { 'begin scope' }; 1 }\nprocess { trap { 'process scope' }; 2 }\n",
            # One typed catch-all beside specific typed traps, and one in another block, are each legal.
            "g.ps1": "trap [System.Exception] { 'catch-all'; continue }\ntrap [System.IO.IOException] { 'io'; continue }\n"
                     "if ($true) {\n    trap { 'if-block scope'; continue }\n    1\n}\n",
            # A namespace that does not hold a catch-all type leaves the typed trap specific (native: the
            # IOException trap is skipped for a string throw and the untyped one runs).
            "h.ps1": "using namespace System.IO\ntrap [IOException] { 'io'; continue }\ntrap { 'catch-all'; continue }\n",
            "i.ps1": "using namespace System.Management.Automation\ntrap [RuntimeException] { 'only catch-all'; continue }\n"
                     "trap [System.IO.IOException] { 'io'; continue }\n",
            # No candidate resolution of [RuntimeException] is a catch-all without the namespace
            # (native pwsh refuses the script: Unable to find type), so it is not counted.
            "j.ps1": "trap [RuntimeException] { 'unresolvable' }\ntrap { 'untyped' }\n",
        },
    },
}

_LIVE: dict[str, tuple[list[Violation], float]] = {}


def _write_tree(root: Path, files: dict[str, str]) -> list[str]:
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
    return sorted(files)


class _PwshMixin:
    def require(self, guard: Guard) -> None:
        if guard.needs_pwsh and find_pwsh() is None:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                self.fail(f"{guard.id}: pwsh is missing on a GitHub Actions runner, whose images carry it")
            self.skipTest(f"{guard.id}: pwsh (PowerShell 7+) is not on PATH, so the AST guard cannot run")


class RegistryTests(unittest.TestCase):
    def test_registry_entries_are_complete_and_unique(self) -> None:
        ids = [g.id for g in REGISTRY]
        self.assertEqual(len(ids), len(set(ids)))
        for g in REGISTRY:
            self.assertRegex(g.bus_trap, r"^[0-9a-f]{7,40}$", g.id)
            self.assertTrue(g.trap.strip(), g.id)
            self.assertIn(g.id, FIXTURES, f"{g.id} has no RED/GREEN fixtures")
            self.assertTrue(FIXTURES[g.id]["red"] and FIXTURES[g.id]["green"], g.id)
        self.assertEqual(set(FIXTURES), set(GUARDS))

    def test_selection_is_by_tracked_path(self) -> None:
        self.assertTrue(is_powershell("tools/x/a.PS1"))
        self.assertTrue(is_powershell("m.psm1"))
        self.assertFalse(is_powershell("m.psd1"))
        self.assertTrue(is_claude_settings(".claude/settings.json"))
        self.assertTrue(is_claude_settings("a/b/.claude/settings.local.json"))
        self.assertFalse(is_claude_settings("tools/agent-bridge/settings.example.json"))
        self.assertFalse(is_claude_settings(".claude/hooks/settings.json"))
        self.assertTrue(is_git_pathlist_scanned("tools/coordination/doctrine_outbox.py"))
        self.assertTrue(is_git_pathlist_scanned("tools/dual-lane/lane-guard.ps1"))
        self.assertFalse(is_git_pathlist_scanned("tools/repo_hygiene/test_brokered_closeout.py"))
        self.assertFalse(is_git_pathlist_scanned("tests/coordination/test_doctrine_outbox.py"))
        self.assertFalse(is_git_pathlist_scanned("tools/coordination/doctrine_outbox.md"))

    def test_untracked_files_are_not_checked(self) -> None:
        if shutil.which("git") is None:
            self.fail("git is required to list tracked files")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True, timeout=60)
            _write_tree(root, {".claude/settings.json": json.dumps({"hooks": {"SessionStart": []}})})
            subprocess.run(["git", "-C", str(root), "add", ".claude/settings.json"], check=True, timeout=60)
            _write_tree(root, {"x/.claude/settings.json": json.dumps({"hooks": {"Start": []}})})
            self.assertEqual(run_guard(GUARDS["DG-HOOK-EVENT"], root), [])


class GuardFixtureTests(_PwshMixin, unittest.TestCase):
    def _run_fixture(self, guard_id: str, colour: str) -> list[Violation]:
        guard = GUARDS[guard_id]
        self.require(guard)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            return run_guard(guard, root, _write_tree(root, FIXTURES[guard_id][colour]))

    def _assert_red(self, guard_id: str, expected: set[tuple[str, int]]) -> None:
        found = {(v.path, v.line) for v in self._run_fixture(guard_id, "red")}
        self.assertEqual(found, expected, f"{guard_id} RED fixture must fail the check at exactly these sites")

    def _assert_green(self, guard_id: str) -> None:
        found = self._run_fixture(guard_id, "green")
        self.assertEqual(found, [], "\n".join(v.render() for v in found))

    def test_ps_null_compare_red_fails(self) -> None:
        self._assert_red("DG-PS-NULL-COMPARE", {("a.ps1", 2), ("b.ps1", 1), ("c.psm1", 2), ("c.psm1", 3),
                                                ("d.ps1", 1), ("d.ps1", 2), ("d.ps1", 3)})

    def test_ps_null_compare_green_passes(self) -> None:
        self._assert_green("DG-PS-NULL-COMPARE")

    def test_hook_event_red_fails(self) -> None:
        found = self._run_fixture("DG-HOOK-EVENT", "red")
        self.assertEqual(sorted((v.path, v.detail) for v in found), [
            (".claude/settings.json", "unknown hook event 'Start' (fails silent)"),
            ("sub/.claude/settings.local.json", "unknown hook event 'PretoolUse' (fails silent)"),
        ])

    def test_hook_event_green_passes(self) -> None:
        self._assert_green("DG-HOOK-EVENT")

    def test_git_pathlist_red_fails(self) -> None:
        self._assert_red("DG-GIT-PATHLIST", {("tools/a.py", 3), ("tools/a.py", 4), ("tools/a.py", 5),
                                             ("tools/a.py", 6), ("tools/a.py", 7), ("tools/a.py", 8),
                                             ("tools/a.py", 9), ("tools/a.py", 10), ("tools/a.py", 11),
                                             ("tools/b.ps1", 1), ("tools/b.ps1", 2),
                                             ("tools/b.ps1", 3), ("tools/b.ps1", 4),
                                             ("tools/b.ps1", 5), ("tools/b.ps1", 6),
                                             ("tools/b.ps1", 7), ("tools/b.ps1", 8),
                                             ("tools/repo_hygiene/c.py", 3), ("tools/repo_hygiene/c.py", 4),
                                             ("tools/repo_hygiene/c.py", 5)})

    def test_git_pathlist_green_passes(self) -> None:
        self._assert_green("DG-GIT-PATHLIST")

    def test_git_pathlist_allowlist_is_keyed_by_path(self) -> None:
        path, needle, _why = GIT_PATHLIST_ALLOW[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rels = _write_tree(root, {"tools/elsewhere.ps1": f"$x = (& git -C {needle})\n"})
            self.assertNotEqual(rels, [path])
            self.assertEqual(len(check_git_pathlist(root, rels)), 1)

    def test_ps_one_trap_red_fails(self) -> None:
        self._assert_red("PS-ONE-TRAP", {("a.ps1", 3), ("b.ps1", 3), ("c.ps1", 4),
                                          ("d.ps1", 3), ("e.ps1", 4), ("e.ps1", 8),
                                          ("f.ps1", 2), ("g.ps1", 2), ("h.ps1", 2),
                                          ("i.ps1", 3), ("j.ps1", 3), ("k.ps1", 3), ("l.ps1", 5)})

    def test_ps_one_trap_green_passes(self) -> None:
        self._assert_green("PS-ONE-TRAP")

    def test_unparseable_powershell_is_could_not_check_not_a_pass(self) -> None:
        guard = GUARDS["DG-PS-NULL-COMPARE"]
        self.require(guard)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rels = _write_tree(root, {"bad.ps1": "if ($x -eq $null {\n"})
            with self.assertRaises(CouldNotCheck):
                run_guard(guard, root, rels)


class LiveTreeTests(_PwshMixin, unittest.TestCase):
    """Every guard is green on the tracked tree at HEAD."""

    def _live(self, guard_id: str) -> list[Violation]:
        guard = GUARDS[guard_id]
        self.require(guard)
        if guard_id not in _LIVE:
            start = time.monotonic()
            found = run_guard(guard, REPO_ROOT)
            _LIVE[guard_id] = (found, time.monotonic() - start)
        return _LIVE[guard_id][0]

    def _assert_live_green(self, guard_id: str) -> None:
        found = self._live(guard_id)
        self.assertEqual(found, [], "\n".join(v.render() for v in found))

    def test_live_tree_has_files_for_every_guard(self) -> None:
        tracked = tracked_files(REPO_ROOT)
        for g in REGISTRY:
            self.assertTrue(any(g.selects(p) for p in tracked), f"{g.id} selects no tracked file")

    def test_live_tree_ps_null_compare(self) -> None:
        self._assert_live_green("DG-PS-NULL-COMPARE")

    def test_live_tree_hook_event(self) -> None:
        self._assert_live_green("DG-HOOK-EVENT")

    def test_live_tree_ps_one_trap(self) -> None:
        self._assert_live_green("PS-ONE-TRAP")

    def test_live_tree_git_pathlist(self) -> None:
        self._assert_live_green("DG-GIT-PATHLIST")

    def test_git_pathlist_allowlist_has_no_stale_entries(self) -> None:
        self.require(GUARDS["DG-GIT-PATHLIST"])
        rels = [p for p in tracked_files(REPO_ROOT) if is_git_pathlist_scanned(p)]
        raw = scan_git_pathlist(REPO_ROOT, rels)
        stale = [f"{path}: {needle}" for path, needle, _why in GIT_PATHLIST_ALLOW
                 if not any(v.path == path and needle in source for v, source in raw)]
        self.assertEqual(stale, [], "allowlist entries that match no call site; remove them")


if __name__ == "__main__":
    unittest.main()
