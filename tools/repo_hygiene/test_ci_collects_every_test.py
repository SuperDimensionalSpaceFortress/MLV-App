"""Fail when a tracked ``test_*.py`` is run by no hosted CI step.

A test file nobody runs is worse than no test: it reads as coverage. The workflows keep
their test lists by hand (a named file, a named directory, a ``--ignore`` list), and
nothing failed when a new file was left off one. "Confirm the test ran in CI, not just
that the job passed" (cos-feedback agent-bridge pr-221, adversarialllm pr-262).

A tracked ``test_*.py`` passes when ANY of these holds:

* a workflow step names the file, or names it as a ``python -m unittest`` dotted module;
* a workflow step runs ``pytest`` on a directory above it and no ``--ignore`` /
  ``--ignore-glob`` of THAT command excludes it;
* it sits under a ``unittest discover`` root (``-s DIR -p PATTERN``), or under the root
  that ``ci_unittest_shard`` discovers (``START_DIR`` / ``PATTERN`` are read from that
  script, not assumed), and the workflow actually runs ``--shard`` of it;
* it is on ``LOCAL_ONLY_OR_UNCOLLECTED`` below, with a non-empty reason.

No YAML parser is used (PyYAML is pinned in no hash-locked requirements file here), so
this is a bounded TEXT scan: full-line and trailing ``#`` comments are dropped, shell and
PowerShell line continuations are joined, backslashes become slashes, and each logical
line is split on whitespace. Limits, stated so nobody reads more into a pass than it
proves: a step's ``if:`` condition, a ``continue-on-error``, a ``-k`` / ``-m`` selection,
and a method-level dotted unittest name are NOT evaluated -- naming a file counts as
running it. This guard proves "some step is pointed at the file", the failure that
actually recurred, not "every test in it passed".

Run the table:  ``py -3 -m tools.repo_hygiene.test_ci_collects_every_test --table``
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = ".github/workflows"
SHARD_SCRIPT = "tools/repo_hygiene/ci_unittest_shard.py"
SHARD_MODULE = "ci_unittest_shard"

# path -> reason. A path is here because no hosted step runs it; the reason says why and
# what closes it. An entry whose file is now collected, or no longer tracked, FAILS this
# guard, so the list can only shrink toward "every test runs".
LOCAL_ONLY_OR_UNCOLLECTED: dict[str, str] = {
    "tools/hooks/test_registration_path_local.py": (
        "local-only by design: it executes the .claude/settings.json hook command, which "
        "names a per-user interpreter that exists on no hosted runner (see the file's own "
        "docstring); hosted CI proves the rules in tools/repo_hygiene/test_mlv_never_authorized.py"
    ),
    "tools/coordination/test_get_doctrine_brief.py": (
        "UNCOLLECTED at acc811f8, follow-up CI-COLLECT-COORDINATION-TOOLS: tests.yml names only "
        "tools/coordination/test_coordination_guardrails.py and test_demote_factory_bridge.py"
    ),
    "tools/coordination/test_record_workstream_completion.py": (
        "UNCOLLECTED at acc811f8, follow-up CI-COLLECT-COORDINATION-TOOLS: tests.yml names only "
        "tools/coordination/test_coordination_guardrails.py and test_demote_factory_bridge.py"
    ),
    "tools/coordination/test_resolve_codex_tier.py": (
        "UNCOLLECTED at acc811f8, follow-up CI-COLLECT-COORDINATION-TOOLS: tests.yml names only "
        "tools/coordination/test_coordination_guardrails.py and test_demote_factory_bridge.py"
    ),
    "tools/profiling/test_frame_colour_spatial_metrics.py": (
        "UNCOLLECTED at acc811f8, follow-up CI-COLLECT-PROFILING-NUMPY-PILLOW: factory-bridge.yml "
        "passes it to --ignore because numpy and Pillow are pinned in no hash-locked requirements file"
    ),
    "tools/profiling/test_make_contact_sheet.py": (
        "UNCOLLECTED at acc811f8, follow-up CI-COLLECT-PROFILING-NUMPY-PILLOW: factory-bridge.yml "
        "passes it to --ignore; it imports numpy and Pillow, pinned in no hash-locked requirements file"
    ),
}

_PYTEST_OPTIONS_WITH_VALUE = frozenset(
    {"-k", "-m", "-p", "-c", "-o", "-W", "--rootdir", "--junitxml", "--maxfail", "--tb",
     "--basetemp", "--confcutdir", "--import-mode", "--durations", "--timeout"}
)
_IGNORE_OPTIONS = ("--ignore", "--ignore-glob")
_STEP_NAME = re.compile(r"^\s*-\s+name:\s*(.+?)\s*$")


# --------------------------------------------------------------------------- scanning


def logical_lines(text: str) -> list[tuple[str, str]]:
    """``(step_name, command_line)`` for every non-comment logical line of a workflow."""
    out: list[tuple[str, str]] = []
    step = "(no step)"
    pending = ""
    for raw in text.splitlines():
        if raw.lstrip().startswith("#"):
            continue
        found = _STEP_NAME.match(raw)
        if found:
            step = found.group(1).strip("\"'")
        line = re.sub(r"\s+#.*$", "", raw).rstrip()
        if line.endswith("`") or re.search(r"\s\\$", line):
            pending += " " + line[:-1].strip()
            continue
        out.append((step, (pending + " " + line).strip().replace("\\", "/")))
        pending = ""
    if pending:
        out.append((step, pending.strip().replace("\\", "/")))
    return out


def _tokens(line: str) -> list[str]:
    cleaned = (tok.strip("\"'(),;") for tok in line.split())
    return [tok[2:] if tok.startswith("./") else tok for tok in cleaned if tok]


def _is_test_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.startswith("test_") and name.endswith(".py")


def _under(path: str, root: str) -> bool:
    root = root.strip("/")
    return root in ("", ".") or path.startswith(root + "/")


def _pytest_collects(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return fnmatch.fnmatch(name, "test_*.py") or fnmatch.fnmatch(name, "*_test.py")


def _discover_collects(path: str, root: str, pattern: str, files: set[str]) -> bool:
    """``unittest discover`` recurses only into packages (directories with __init__.py)."""
    root = "" if root in (".", "") else root.strip("/")
    if not _under(path, root) or not fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern):
        return False
    parts = path.split("/")[:-1]
    depth = len(root.split("/")) if root else 0
    for end in range(depth + 1, len(parts) + 1):
        if "/".join(parts[:end] + ["__init__.py"]) not in files:
            return False
    return True


def shard_discovery(shard_script_text: str | None) -> tuple[str, str] | None:
    """``(START_DIR, PATTERN)`` as ``ci_unittest_shard`` declares them, else None."""
    if not shard_script_text:
        return None
    start = re.search(r'^START_DIR\s*=\s*"([^"]+)"', shard_script_text, re.MULTILINE)
    pattern = re.search(r'^PATTERN\s*=\s*"([^"]+)"', shard_script_text, re.MULTILINE)
    return (start.group(1), pattern.group(1)) if start and pattern else None


def _option_value(tokens: list[str], index: int, name: str) -> tuple[str | None, int]:
    tok = tokens[index]
    if tok.startswith(name + "="):
        return tok[len(name) + 1:].strip("\"'"), index + 1
    if tok == name and index + 1 < len(tokens):
        return tokens[index + 1], index + 2
    return None, index


def _pytest_invocation(tokens: list[str], start: int) -> tuple[list[str], list[str], list[str]]:
    """``(targets, ignore_paths, ignore_globs)`` of the pytest command at ``tokens[start:]``."""
    targets: list[str] = []
    ignores: list[str] = []
    globs: list[str] = []
    i = start
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("--ignore-glob"):
            value, i = _option_value(tokens, i, "--ignore-glob")
            if value is not None:
                globs.append(value)
            continue
        if tok.startswith("--ignore"):
            value, i = _option_value(tokens, i, "--ignore")
            if value is not None:
                ignores.append(value.rstrip("/"))
            continue
        if tok in _PYTEST_OPTIONS_WITH_VALUE:
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        targets.append(tok.split("::", 1)[0].rstrip("/"))
        i += 1
    return targets, ignores, globs


def _discover_args(tokens: list[str], start: int) -> tuple[str, str]:
    root, pattern = ".", "test*.py"
    i = start
    while i < len(tokens):
        for name in ("-s", "--start-directory"):
            value, nxt = _option_value(tokens, i, name)
            if value is not None:
                root, i = value, nxt
                break
        else:
            for name in ("-p", "--pattern"):
                value, nxt = _option_value(tokens, i, name)
                if value is not None:
                    pattern, i = value, nxt
                    break
            else:
                i += 1
    return root, pattern


def collect(
    paths: list[str],
    workflows: dict[str, str],
    shard_script_text: str | None = None,
) -> dict[str, list[tuple[str, str, str]]]:
    """Tracked ``test_*.py`` -> ``[(workflow, step, mechanism)]``; empty list means NONE."""
    files = set(paths)
    tests = sorted(p for p in files if _is_test_file(p))
    result: dict[str, list[tuple[str, str, str]]] = {t: [] for t in tests}
    shard_root = shard_discovery(shard_script_text)

    def hit(test: str, workflow: str, step: str, mechanism: str) -> None:
        if (workflow, step, mechanism) not in result[test]:
            result[test].append((workflow, step, mechanism))

    for workflow, text in sorted(workflows.items()):
        for step, line in logical_lines(text):
            tokens = _tokens(line)
            if not tokens:
                continue
            for index, tok in enumerate(tokens):
                if tok == "pytest" or tok.endswith("/pytest"):
                    targets, ignores, globs = _pytest_invocation(tokens, index + 1)
                    for test in tests:
                        if any(test == i or test.startswith(i + "/") for i in ignores):
                            continue
                        if any(fnmatch.fnmatch(test, g) for g in globs):
                            continue
                        for target in targets:
                            if test == target and not target.endswith("/"):
                                hit(test, workflow, step, "pytest-file")
                            elif _under(test, target) and target != test and _pytest_collects(test):
                                hit(test, workflow, step, "pytest-dir")
                elif tok == "unittest" and index > 0 and tokens[index - 1] == "-m":
                    rest = tokens[index + 1:]
                    if rest and rest[0] == "discover":
                        root, pattern = _discover_args(tokens, index + 2)
                        for test in tests:
                            if _discover_collects(test, root, pattern, files):
                                hit(test, workflow, step, "unittest-discover")
                        continue
                    for name in rest:
                        if name.startswith("-"):
                            continue
                        parts = name.split(".")
                        for end in range(len(parts), 0, -1):
                            module = "/".join(parts[:end]) + ".py"
                            if module in result:
                                hit(module, workflow, step, "unittest-module")
                                break
            if shard_root and "--shard" in tokens and any(SHARD_MODULE in t for t in tokens):
                for test in tests:
                    if _discover_collects(test, shard_root[0], shard_root[1], files):
                        hit(test, workflow, step, "shard-discover")
            skip_next = False
            for tok in tokens:
                if skip_next:
                    skip_next = False
                    continue
                if tok in _IGNORE_OPTIONS or tok == "--deselect":
                    skip_next = True
                    continue
                if tok.startswith("-"):
                    continue
                if tok in result:
                    hit(tok, workflow, step, "named-file")
    return result


def evaluate(
    paths: list[str],
    workflows: dict[str, str],
    shard_script_text: str | None,
    allowlist: dict[str, str],
) -> list[str]:
    """Every problem, one line each; an empty list means the guard passes."""
    table = collect(paths, workflows, shard_script_text)
    problems: list[str] = []
    for path, reason in sorted(allowlist.items()):
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"allowlist entry {path} has no reason; say why no hosted step runs it")
        if path not in table:
            problems.append(f"allowlist entry {path} is not a tracked test_*.py; remove the stale entry")
        elif table[path]:
            problems.append(f"allowlist entry {path} is now collected by CI; remove it from the allowlist")
    for path, steps in sorted(table.items()):
        if steps:
            continue
        reason = allowlist.get(path)
        if reason is not None and reason.strip():
            continue
        problems.append(
            f"{path} is run by no hosted CI step: name it (or its directory) in a workflow, "
            f"or add it to LOCAL_ONLY_OR_UNCOLLECTED with a reason"
        )
    return problems


# --------------------------------------------------------------------------- the tree


def git_lister(root: Path) -> list[str]:
    """Tracked files, NUL-separated so a path with a space or a newline cannot split."""
    out = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True
    ).stdout
    return [p.decode("utf-8") for p in out.split(b"\0") if p]


def walk_lister(root: Path) -> list[str]:
    found: list[str] = []
    for base, _dirs, names in os.walk(root):
        for name in names:
            found.append(Path(base, name).relative_to(root).as_posix())
    return found


def read_inputs(root: Path, lister=git_lister) -> tuple[list[str], dict[str, str], str | None]:
    paths = lister(root)
    workflows: dict[str, str] = {}
    wf_dir = root / WORKFLOW_DIR
    if wf_dir.is_dir():
        for entry in sorted(wf_dir.iterdir()):
            if entry.suffix in (".yml", ".yaml"):
                workflows[f"{WORKFLOW_DIR}/{entry.name}"] = entry.read_text(encoding="utf-8")
    shard = root / SHARD_SCRIPT
    return paths, workflows, shard.read_text(encoding="utf-8") if shard.is_file() else None


def render_table(root: Path = REPO_ROOT) -> str:
    paths, workflows, shard = read_inputs(root)
    table = collect(paths, workflows, shard)
    rows = []
    for path, steps in sorted(table.items()):
        if steps:
            workflow, step, mechanism = steps[0]
            extra = f" (+{len(steps) - 1} more)" if len(steps) > 1 else ""
            rows.append(f"{path}\t{mechanism}\t{workflow.rsplit('/', 1)[-1]}: {step}{extra}")
        else:
            allowed = "allowlisted" if path in LOCAL_ONLY_OR_UNCOLLECTED else "NOT ALLOWLISTED"
            rows.append(f"{path}\tNONE\t{allowed}")
    return "\n".join(rows) + "\n"


# --------------------------------------------------------------------------- tests

_HYGIENE_RUN = (
    "      - name: Run shard\n"
    "        run: python -m tools.repo_hygiene.ci_unittest_shard --profile ubuntu --of 3 --shard 1\n"
)
_SHARD_SCRIPT = 'START_DIR = "tools/repo_hygiene"\nPATTERN = "test_*.py"\n'


def _workflow(*bodies: str) -> str:
    return "jobs:\n  j:\n    steps:\n" + "".join(bodies)


def _step(name: str, run: str) -> str:
    indented = "".join(f"          {line}\n" for line in run.splitlines())
    return f"      - name: {name}\n        run: |\n{indented}"


class FixtureTreeTests(unittest.TestCase):
    """The guard on a temp tree: every verdict is proven in both directions."""

    def _tree(self, files: dict[str, str]) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for rel, body in files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return root

    def _problems(self, files: dict[str, str], allowlist: dict[str, str] | None = None) -> list[str]:
        root = self._tree(files)
        paths, workflows, shard = read_inputs(root, walk_lister)
        return evaluate(paths, workflows, shard, allowlist or {})

    def test_a_new_uncollected_file_fails_and_is_named(self) -> None:
        problems = self._problems(
            {
                "tools/newarea/test_orphan.py": "",
                ".github/workflows/t.yml": _workflow(_step("Other", "python -m pytest tests/other -q")),
            }
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tools/newarea/test_orphan.py", problems[0])

    def test_a_file_named_by_a_workflow_passes(self) -> None:
        self.assertEqual(
            self._problems(
                {
                    "tools/a/test_one.py": "",
                    "tools/a/test_two.py": "",
                    "tools/a/test_three.py": "",
                    ".github/workflows/t.yml": _workflow(
                        _step("Pytest", "python -m pytest tools\\a\\test_one.py -q"),
                        _step("Unittest", "python -m unittest tools.a.test_two -v"),
                        _step("Script", "python tools/a/test_three.py"),
                    ),
                }
            ),
            [],
        )

    def test_a_dotted_unittest_method_name_names_its_module(self) -> None:
        self.assertEqual(
            self._problems(
                {
                    "tools/agent-bridge/test_x.py": "",
                    ".github/workflows/t.yml": _workflow(
                        _step("Method", "python -m unittest tools.agent-bridge.test_x.Case.test_m -v")
                    ),
                }
            ),
            [],
        )

    def test_a_pytest_directory_collects_every_file_below_it(self) -> None:
        self.assertEqual(
            self._problems(
                {
                    "tools/a/test_one.py": "",
                    "tools/a/deep/test_two.py": "",
                    ".github/workflows/t.yml": _workflow(_step("Dir", "python -m pytest tools\\a -q")),
                }
            ),
            [],
        )

    def test_an_ignore_removes_a_file_from_a_directory_target(self) -> None:
        for ignore in ("--ignore=tools\\a\\test_skipped.py", "--ignore tools/a/test_skipped.py"):
            problems = self._problems(
                {
                    "tools/a/test_kept.py": "",
                    "tools/a/test_skipped.py": "",
                    ".github/workflows/t.yml": _workflow(
                        _step("Dir", f"python -m pytest `\n  tools\\a `\n  {ignore} `\n  -q")
                    ),
                }
            )
            self.assertEqual(len(problems), 1, (ignore, problems))
            self.assertIn("test_skipped.py", problems[0])

    def test_an_ignore_glob_and_a_directory_ignore_both_exclude(self) -> None:
        problems = self._problems(
            {
                "tools/a/test_one.py": "",
                "tools/a/sub/test_two.py": "",
                "tools/a/test_kept.py": "",
                ".github/workflows/t.yml": _workflow(
                    _step("Dir", "python -m pytest tools/a --ignore=tools/a/sub --ignore-glob='*one.py' -q")
                ),
            }
        )
        self.assertEqual(len(problems), 2, problems)

    def test_a_comment_that_names_a_file_does_not_collect_it(self) -> None:
        problems = self._problems(
            {
                "tools/a/test_one.py": "",
                ".github/workflows/t.yml": _workflow(
                    "      # tools/a/test_one.py is covered below\n",
                    _step("Other", "echo hi  # tools/a/test_one.py\npython -m pytest tests/x -q"),
                ),
            }
        )
        self.assertEqual(len(problems), 1, problems)

    def test_the_shard_runner_collects_its_discovery_root(self) -> None:
        files = {
            "tools/repo_hygiene/__init__.py": "",
            "tools/repo_hygiene/test_in_root.py": "",
            "tools/repo_hygiene/ci_unittest_shard.py": _SHARD_SCRIPT,
            ".github/workflows/t.yml": _workflow(_HYGIENE_RUN),
        }
        self.assertEqual(self._problems(files), [])

    def test_discovery_does_not_enter_a_directory_that_is_not_a_package(self) -> None:
        files = {
            "tools/repo_hygiene/__init__.py": "",
            "tools/repo_hygiene/loose/test_buried.py": "",
            "tools/repo_hygiene/ci_unittest_shard.py": _SHARD_SCRIPT,
            ".github/workflows/t.yml": _workflow(_HYGIENE_RUN),
        }
        problems = self._problems(files)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("loose/test_buried.py", problems[0])

    def test_verifying_the_partition_alone_runs_no_test(self) -> None:
        verify_only = _workflow(
            _step("Prove", "python -m tools.repo_hygiene.ci_unittest_shard --profile ubuntu --of 3 --verify-partition")
        )
        problems = self._problems(
            {
                "tools/repo_hygiene/__init__.py": "",
                "tools/repo_hygiene/test_in_root.py": "",
                "tools/repo_hygiene/ci_unittest_shard.py": _SHARD_SCRIPT,
                ".github/workflows/t.yml": verify_only,
            }
        )
        self.assertEqual(len(problems), 1, problems)

    def test_an_explicit_unittest_discover_root_collects_by_its_pattern(self) -> None:
        files = {
            "pkg/__init__.py": "",
            "pkg/test_a.py": "",
            "pkg/other_test_b.py": "",
            ".github/workflows/t.yml": _workflow(
                _step("Discover", 'python -m unittest discover -s pkg -p "test_*.py" -t .')
            ),
        }
        self.assertEqual(self._problems(files), [])

    def test_an_allowlisted_file_with_a_reason_passes(self) -> None:
        self.assertEqual(
            self._problems({"tools/x/test_local.py": ""}, {"tools/x/test_local.py": "needs a per-user interpreter"}),
            [],
        )

    def test_an_allowlisted_file_without_a_reason_fails(self) -> None:
        for reason in ("", "   "):
            problems = self._problems({"tools/x/test_local.py": ""}, {"tools/x/test_local.py": reason})
            self.assertTrue(any("has no reason" in p for p in problems), problems)
            self.assertTrue(any("run by no hosted CI step" in p for p in problems), problems)

    def test_a_stale_allowlist_entry_fails_both_ways(self) -> None:
        collected = {
            "tools/x/test_now_run.py": "",
            ".github/workflows/t.yml": _workflow(_step("Run", "python -m pytest tools/x -q")),
        }
        problems = self._problems(collected, {"tools/x/test_now_run.py": "was local"})
        self.assertTrue(any("now collected by CI" in p for p in problems), problems)
        problems = self._problems({"tools/x/test_a.py": ""}, {"tools/x/test_gone.py": "deleted"})
        self.assertTrue(any("not a tracked test_*.py" in p for p in problems), problems)


class RealTreeTests(unittest.TestCase):
    def test_every_tracked_test_file_is_collected_or_allowlisted_with_a_reason(self) -> None:
        paths, workflows, shard = read_inputs(REPO_ROOT)
        self.assertTrue(workflows, "no workflows found under .github/workflows")
        problems = evaluate(paths, workflows, shard, LOCAL_ONLY_OR_UNCOLLECTED)
        self.assertEqual(problems, [], "\n" + "\n".join(problems))

    def test_the_shard_discovery_root_is_read_from_the_script_not_assumed(self) -> None:
        paths, _workflows, shard = read_inputs(REPO_ROOT)
        self.assertEqual(shard_discovery(shard), ("tools/repo_hygiene", "test_*.py"))
        self.assertIn("tools/repo_hygiene/test_ci_collects_every_test.py", paths)

    def test_this_guard_itself_is_collected(self) -> None:
        paths, workflows, shard = read_inputs(REPO_ROOT)
        table = collect(paths, workflows, shard)
        self.assertTrue(table["tools/repo_hygiene/test_ci_collects_every_test.py"])

    def test_every_allowlist_reason_is_non_empty(self) -> None:
        for path, reason in LOCAL_ONLY_OR_UNCOLLECTED.items():
            self.assertTrue(reason.strip(), path)


if __name__ == "__main__":
    if "--table" in sys.argv:
        sys.stdout.write(render_table())
    else:
        unittest.main()
