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
  carry -z or core.quotepath=false) and PowerShell by one source line; a command assembled
  from variables or one joined string is not seen. Test files are not scanned. A call that
  needs no path text (exit code, emptiness, a count) is listed in GIT_PATHLIST_ALLOW with its
  reason; git_paths() in tools/coordination/doctrine_outbox.py adds -z itself.
- DG-PS-NULL-COMPARE flags only a literal ``$null`` on the RIGHT of
  -eq/-ne/-ceq/-cne/-ieq/-ine. The ``-eq $false`` tri-state form on a possibly-null
  value (tools/profiling/compare-machine-perf.ps1, run-release-cuda-playback-ab.ps1)
  cannot be decided statically and is out of scope; review it by hand.
- PS-ONE-TRAP counts every top-level TrapStatementAst (typed or catch-all) in a
  file's own scope; traps inside functions or script blocks are not counted.

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
# {path, errors, nullRight, topTraps} object per path to -OutPath.
# ---------------------------------------------------------------------------
_PS_SCAN = r"""
param([Parameter(Mandatory)][string]$InPath, [Parameter(Mandatory)][string]$OutPath)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$enc = New-Object System.Text.UTF8Encoding($false)
$paths = @([IO.File]::ReadAllText($InPath, $enc) | ConvertFrom-Json)
$ops = @('Ieq', 'Ine', 'Ceq', 'Cne')
$nullName = '^(?:(?:global|script|local|private):)?null$'
function Unwrap($e) {
    while ($true) {
        if ($e -is [System.Management.Automation.Language.ParenExpressionAst]) { $e = $e.Pipeline; continue }
        if ($e -is [System.Management.Automation.Language.PipelineAst] -and $e.PipelineElements.Count -eq 1) {
            $e = $e.PipelineElements[0]; continue
        }
        if ($e -is [System.Management.Automation.Language.CommandExpressionAst]) { $e = $e.Expression; continue }
        return $e
    }
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
    $traps = @($ast.FindAll({
        param($n) $n -is [System.Management.Automation.Language.TrapStatementAst]
    }, $false) | ForEach-Object { $_.Extent.StartLineNumber })
    $results.Add([ordered]@{ path = [string]$p; errors = @($errs); nullRight = @($nulls); topTraps = @($traps) })
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
        if isinstance(res, dict) and set(res) == {"path", "errors", "nullRight", "topTraps"}:
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
        traps = scan[rel]["topTraps"]
        if len(traps) > 1:
            out.append(Violation("PS-ONE-TRAP", rel, traps[1],
                                 f"{len(traps)} top-level traps; PowerShell runs only the first"))
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
    ("tools/coordination/Retire-LaneWorktree.ps1", "`git status --porcelain -uall` is non-empty",
     "a comment in the script's own header block, not a call"),
    ("tools/coordination/Retire-LaneWorktree.ps1", "'status', '--porcelain', '-uall'",
     "refuses on a non-zero count and quotes the first line in the reason; the ignored-entry listing, "
     "whose paths ARE tested and moved, carries core.quotepath=false"),
    ("tools/gen-buildinfo.ps1", "GitOut @('status','--porcelain')",
     "dirty flag from 'any output'; the text is never read as a path"),
    ("tools/profiling/export-release-cuda-dogfood-kit.ps1", "-C $root status --porcelain",
     "dirty flag from a count; the text is never read as a path"),
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


def _git_pathlist_kind(tokens: set[str]) -> str | None:
    """Which path-listing git command a set of argument tokens spells, or None."""
    if "ls-files" in tokens and "--error-unmatch" not in tokens:  # --error-unmatch is read by exit code
        return "ls-files"
    if "ls-tree" in tokens and "--name-only" in tokens:
        return "ls-tree --name-only"
    if tokens & {"diff", "log", "diff-tree", "show", "stash"} and tokens & {"--name-only", "--name-status"}:
        return "diff/log/diff-tree --name-only|--name-status"
    if "status" in tokens and any(t.startswith("--porcelain") for t in tokens):
        return "status --porcelain"
    return None


def _git_pathlist_safe(tokens: set[str]) -> bool:
    return "-z" in tokens or any("core.quotepath=false" in t for t in tokens)


_PS_TOKEN = re.compile(r"--?[\w=.:-]+|[\w=.:-]+")
# Calls to these helpers add `-z` themselves (tools/coordination/doctrine_outbox.py git_paths).
_GIT_PATHLIST_HELPERS = frozenset(("git_paths",))


def scan_git_pathlist(root: Path, rel_paths: list[str]) -> list[tuple[Violation, str]]:
    """Every git path-list invocation without `-z` or `core.quotepath=false`, with its source text.

    Python: the string constants of one list/tuple/call spell the command, so `-z` must sit in the
    same list or call. PowerShell: one source line spells it (comment lines are skipped)."""
    found: list[tuple[Violation, str]] = []
    for rel in rel_paths:
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise CouldNotCheck(f"{rel} is not readable text: {exc}") from exc
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
            for number, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                tokens = set(_PS_TOKEN.findall(line))
                kind = _git_pathlist_kind(tokens)
                if kind and not _git_pathlist_safe(tokens):
                    found.append((Violation("DG-GIT-PATHLIST", rel, number,
                                            f"{kind} without -z or core.quotepath=false"), line))
    return found


def check_git_pathlist(root: Path, rel_paths: list[str]) -> list[Violation]:
    return [v for v, source in scan_git_pathlist(root, rel_paths)
            if not any(v.path == path and needle in source for path, needle, _why in GIT_PATHLIST_ALLOW)]


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
          "PowerShell runs only the FIRST trap in a scope; a second one is dead code",
          is_powershell, check_ps_one_trap, True),
    Guard("DG-GIT-PATHLIST", "de09cae",
          "git quotes a non-ASCII path in its plain path-list output, so a path-prefix test on it misses the path",
          is_git_pathlist_scanned, check_git_pathlist, False),
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
        },
        "green": {
            "a.ps1": "$x = @(1, $null)\nif ($null -eq $x) { 'n' }\n$v = @(1) | Where-Object { $null -ne $_ }\n",
            "b.ps1": "# $x -eq $null in a comment is not code\n$s = 'if ($x -ne $null) {}'\n"
                     "$h = @'\n$y -eq $null\n'@\n",
            "c.ps1": "$ok = $true\nif ($ok -eq $false) { 'tri-state form is out of scope' }\n"
                     "$nullish = 0\nif ($ok -ne $nullish) { 1 }\n",
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
                          "    git(repo, 'ls-tree', '-r', '--name-only', 'HEAD')\n",
            "tools/b.ps1": "$staged = @(& git -C $root diff --cached --name-only)\n"
                           "$d = @(Run-Git $wd @('status', '--porcelain', '-uall'))\n",
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
                          "    note = 'git ls-files is only text here'\n",
            "tools/b.ps1": "# git ls-files in a comment is not a call\n"
                           "$a = @(& git -C $root -c core.quotepath=false status --porcelain)\n"
                           "$b = @(& git -C $root diff --cached --name-only -z)\n"
                           "& git -C $root worktree list --porcelain\n",
        },
    },
    "PS-ONE-TRAP": {
        "red": {
            "a.ps1": "trap { Write-Error 'first'; break }\n'body'\ntrap { 'second, never runs'; break }\n",
        },
        "green": {
            "a.ps1": "trap { Write-Error 'only'; break }\nfunction F {\n    trap { 'own scope'; continue }\n    1\n}\n"
                     "$sb = { trap { continue }; 2 }\n",
            "b.ps1": "'no trap at all'\n",
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
        self._assert_red("DG-PS-NULL-COMPARE", {("a.ps1", 2), ("b.ps1", 1), ("c.psm1", 2), ("c.psm1", 3)})

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
                                             ("tools/a.py", 6), ("tools/a.py", 7),
                                             ("tools/b.ps1", 1), ("tools/b.ps1", 2)})

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
        self._assert_red("PS-ONE-TRAP", {("a.ps1", 3)})

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
        rels = [p for p in tracked_files(REPO_ROOT) if is_git_pathlist_scanned(p)]
        raw = scan_git_pathlist(REPO_ROOT, rels)
        stale = [f"{path}: {needle}" for path, needle, _why in GIT_PATHLIST_ALLOW
                 if not any(v.path == path and needle in source for v, source in raw)]
        self.assertEqual(stale, [], "allowlist entries that match no call site; remove them")


if __name__ == "__main__":
    unittest.main()
