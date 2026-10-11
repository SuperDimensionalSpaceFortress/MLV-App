"""Fail when a tracked ``test_*.py`` is run by no hosted CI step.

A test file nobody runs is worse than no test: it reads as coverage. The workflows keep
their test lists by hand (a named file, a named directory, a ``--ignore`` list), and
nothing failed when a new file was left off one. "Confirm the test ran in CI, not just
that the job passed" (cos-feedback agent-bridge pr-221, adversarialllm pr-262).

A tracked ``test_*.py`` passes when ANY of these holds:

* a ``run:`` body EXECUTES it: ``pytest`` / ``python -m pytest`` / ``python -m unittest`` /
  ``python FILE`` names the file (or a unittest dotted module). The command head must be the
  runner, so ``echo``, ``cat``, ``Copy-Item`` or an ``on.*.paths`` filter naming it is not a run;
* a workflow step runs ``pytest`` on a directory above it and no ``--ignore`` /
  ``--ignore-glob`` of THAT command excludes it;
* it sits under a ``unittest discover`` root (``-s DIR -p PATTERN``), or under the root
  that ``ci_unittest_shard`` discovers (``START_DIR`` / ``PATTERN`` are read from that
  script, not assumed), and the workflow actually runs ``--shard`` of it (a ``--list``,
  ``--list-tests``, ``--help`` or ``--verify-partition`` command runs no test);
* it is on ``LOCAL_ONLY_OR_UNCOLLECTED`` below, with a non-empty reason.

No YAML parser is used (PyYAML is pinned in no hash-locked requirements file here), so
this is a bounded TEXT scan: only the value of a ``run:`` key is read, full-line and
trailing ``#`` comments are dropped, shell and PowerShell line continuations are joined,
and a character scanner with a quote state splits each ``run:`` body into simple commands on
``&&``, ``||``, ``|``, ``;``, braces, parentheses and line ends, OUTSIDE quotes only (text
inside quotes is one word, never a command), and into words on whitespace. Backslashes outside
an escape become slashes. The scan FAILS CLOSED rather than modelling a shell: a command inside
a ``function`` body (bash ``name() {``, ``function name {``; PowerShell ``function Name {``) and
every command after a top-level bare ``exit`` / ``return`` credit nothing, even when the
function is called later (name the file outside the function, or allowlist it). Not modelled:
a function call graph, ``$f = { ... }`` script blocks, a ``(subshell)`` function, here-doc
bodies, ``then`` / ``do`` as a command head. An option that needs a
value but has none (``pytest tools/a --ignore``) raises ``WorkflowParseError`` naming the
workflow, step and option; no parser loop can run without advancing. Limits, stated so
nobody reads more into a pass than it proves: a step's ``if:`` condition, a
``continue-on-error``, a ``-k`` / ``-m`` selection, and a method-level dotted unittest name
are NOT evaluated -- a runner command pointed at a file counts as running it. This guard
proves "some step runs the file", the failure that actually recurred, not "every test in
it passed".

Run the table:  ``py -3 -m tools.repo_hygiene.test_ci_collects_every_test --table``
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
import tempfile
import threading
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
# A command line that mentions a test but executes none of it.
_NO_EXECUTION_FLAGS = frozenset(
    {"-h", "--help", "--list", "--list-tests", "--collect-only", "--co", "--version",
     "--fixtures", "--markers", "--verify-partition", "--dry-run"}
)
_STEP_NAME = re.compile(r"^\s*-\s+name:\s*(.+?)\s*$")
_RUN_KEY = re.compile(r"^(\s*)(-\s+)?run:[ \t]*(.*)$")
_LITERAL_BLOCK = re.compile(r"^\|(?:[+-]\d?|\d[+-]?)?\s*(#.*)?$")
# A value that opens with a block indicator but is not a literal ``|`` one: a folded ``>`` body joins
# its lines into one, so this line-wise reader cannot say what executes. It gets no credit.
_UNREADABLE_BLOCK = re.compile(r"^[>|]")
_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_NAME = re.compile(r"^(?:python|python3|py)(?:\d+(?:\.\d+)*)?(?:\.exe)?$")
_WRAPPERS = frozenset({"&", "env", "sudo", "time", "call", "nohup"})


class WorkflowParseError(ValueError):
    """A workflow command this guard cannot read; the message names the workflow and option."""


# --------------------------------------------------------------------------- scanning


def run_bodies(text: str) -> list[tuple[str, list[str]]]:
    """``(step_name, logical_lines)`` for every ``run:`` body, comments and continuations resolved.

    Only text a shell would execute is returned: the value of a ``run:`` key, inline or block
    scalar. ``on.*.paths``, ``with:``, ``if:``, ``env:`` and every other YAML key are never a
    command, so a test path written there is not "run". Backslashes are left for the tokenizer,
    which must tell an escaped quote from a Windows path separator.
    """
    out: list[tuple[str, list[str]]] = []
    step = "(no step)"
    lines: list[str] = []
    pending = ""
    body_indent: int | None = None  # indent of the open run: key; None outside a run body
    unreadable = False  # the open body is a folded (or unrecognised) block: it earns no credit

    def close() -> None:
        nonlocal pending, lines, body_indent, unreadable
        if pending:
            lines.append(pending.strip())
        out.append((step, [] if unreadable else lines))
        pending, lines, body_indent, unreadable = "", [], None, False

    for raw in text.splitlines():
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip())
        if body_indent is not None and stripped and indent <= body_indent:
            close()
        if body_indent is None:
            found = _STEP_NAME.match(raw)
            if found:
                step = found.group(1).strip("\"'")
            key = _RUN_KEY.match(raw)
            if not key or stripped.startswith("#"):
                continue
            body_indent = len(key.group(1)) + (len(key.group(2)) if key.group(2) else 0)
            text_line = key.group(3)
            if not text_line or _LITERAL_BLOCK.match(text_line):
                continue
            if _UNREADABLE_BLOCK.match(text_line):
                unreadable = True
                continue
        else:
            text_line = raw
        if stripped.startswith("#"):
            continue
        line = re.sub(r"\s+#.*$", "", text_line).rstrip()
        if line.endswith("`") or re.search(r"\s\\$", line):
            pending += " " + line[:-1].strip()
            continue
        lines.append((pending + " " + line).strip())
        pending = ""
    if body_indent is not None:
        close()
    return out


_SEPARATORS = ("&&", "||", ";", "|", "{", "}", "(", ")")
_NEWLINE = "NL"
_QUOTES = "\"'"


def _tokenize(lines: list[str]) -> list[tuple[str, str]]:
    """``("word", text)`` / ``("sep", op)`` items of one run body, scanned with a quote state.

    A separator (``&&`` ``||`` ``;`` ``|``, braces, parentheses, end of line) splits commands only
    OUTSIDE single and double quotes, so a quoted string is one word and never yields a runner
    invocation. Escapes: a backtick (PowerShell) outside single quotes; a backslash before a
    quote or backslash inside double quotes, and before a quote outside quotes (bash). Any other
    backslash is a Windows path separator and becomes ``/``. A quote still open at the end of a
    line continues on the next (a multi-line string). A GitHub ``${{ ... }}`` expression and a
    shell ``${...}`` are one opaque word part.
    """
    items: list[tuple[str, str]] = []
    word: list[str] = []
    quote: str | None = None

    def flush() -> None:
        if word:
            items.append(("word", "".join(word)))
            word.clear()

    for line in lines:
        i, n = 0, len(line)
        while i < n:
            ch = line[i]
            nxt = line[i + 1] if i + 1 < n else ""
            if quote == "'":
                if ch == "'":
                    quote = None
                else:
                    word.append(ch)
            elif quote == '"':
                if ch == "`" and nxt:
                    word.append(nxt)
                    i += 1
                elif ch == "\\" and nxt in ('"', "\\"):
                    word.append(nxt)
                    i += 1
                elif ch == '"':
                    quote = None
                else:
                    word.append("/" if ch == "\\" else ch)
            elif ch in _QUOTES:
                quote = ch
            elif ch == "`" and nxt:
                word.append(nxt)
                i += 1
            elif ch == "\\":
                if nxt in _QUOTES:
                    word.append(nxt)
                    i += 1
                else:
                    word.append("/")
            elif line.startswith("${", i):
                close = "}}" if line.startswith("${{", i) else "}"
                end = line.find(close, i)
                word.append("$X")
                i = end + len(close) - 1 if end >= 0 else n
            elif ch.isspace():
                flush()
            else:
                op = next((s for s in _SEPARATORS if line.startswith(s, i)), None)
                if op is None:
                    word.append(ch)
                else:
                    flush()
                    items.append(("sep", op))
                    i += len(op) - 1
            i += 1
        if quote is None:
            flush()
            items.append(("sep", _NEWLINE))
        else:
            word.append("\n")
    flush()
    return items


_BLOCK_OPEN = frozenset({"if", "case", "for", "while", "until", "select"})
_BLOCK_CLOSE = frozenset({"fi", "esac", "done"})
_FUNCTION_KEYWORDS = frozenset({"function", "filter"})
_UNCONDITIONAL_LEAD = (";", _NEWLINE)


def _opens_function(items: list[tuple[str, str]], at: int) -> bool:
    """True when the ``{`` at ``items[at]`` opens a function body.

    The shapes: ``function NAME {``, ``function NAME ($p) {``, ``filter NAME {``, ``NAME() {``,
    and either with the ``{`` on the next line. A brace after any other command is a group or a
    script block, which runs.
    """

    def back(j: int) -> int:
        while j >= 0 and items[j] == ("sep", _NEWLINE):
            j -= 1
        return j

    j = back(at - 1)
    empty_parens = False
    if j >= 0 and items[j] == ("sep", ")"):
        k = j - 1
        while k >= 0 and items[k] != ("sep", "("):
            k -= 1
        if k < 0:
            return False
        empty_parens = k == j - 1
        j = back(k - 1)
    if j < 0 or items[j][0] != "word":
        return False
    if empty_parens:
        return True
    return j > 0 and items[j - 1][0] == "word" and items[j - 1][1].lower() in _FUNCTION_KEYWORDS


def _executed_commands(items: list[tuple[str, str]]) -> list[list[str]]:
    """Token lists of the commands a run body can reach: not in a function body, not after an exit.

    FAIL CLOSED, not a shell model. A command inside ``function NAME { ... }`` (bash or
    PowerShell) credits nothing, even when the function is called later. A bare ``exit`` or
    ``return`` at the top level (outside any brace and any ``if`` / ``for`` / ``while`` / ``case``
    block, and not the right side of ``&&`` / ``||`` / ``|``) ends the body, so nothing after it
    credits. A body that really calls its function therefore loses credit: name the file in a
    command outside the function, or allowlist it with a reason.
    """
    out: list[list[str]] = []
    braces: list[bool] = []  # one entry per open "{"; True = a function body
    blocks = 0
    dead = False
    cur: list[str] = []
    lead = _NEWLINE  # the separator that introduced the command being read
    for at, (kind, text) in enumerate(items + [("sep", _NEWLINE)]):
        if kind == "word":
            cur.append(text)
            continue
        if cur:
            head = cur[0].lower()
            if head in _BLOCK_OPEN and not (len(cur) == 1 and text == "("):
                blocks += 1  # a PowerShell ``if (`` opens a brace, which is tracked on its own
            elif head in _BLOCK_CLOSE:
                blocks = max(0, blocks - 1)
            elif head in ("exit", "return") and not braces and not blocks and lead in _UNCONDITIONAL_LEAD:
                dead = True
            if not dead and not any(braces):
                out.append([t[2:] if t.startswith("./") else t for t in cur])
            cur = []
        if text == "{":
            braces.append(_opens_function(items, at))
        elif text == "}" and braces:
            braces.pop()
        lead = text
    return out


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


def _is_option(tok: str, *names: str) -> str | None:
    """The option name ``tok`` spells (``--name`` or ``--name=value``), else None."""
    for name in names:
        if tok == name or tok.startswith(name + "="):
            return name
    return None


def _option_value(tokens: list[str], index: int, name: str) -> tuple[str, int]:
    """Value of the option at ``tokens[index]`` and the NEXT index (always ``> index``).

    A missing or empty value raises: skipping it silently would either loop on the same
    token or read the following option as a path.
    """
    tok = tokens[index]
    if tok.startswith(name + "="):
        value, step = tok[len(name) + 1:].strip("\"'"), 1
    elif index + 1 < len(tokens):
        value, step = tokens[index + 1], 2
    else:
        value, step = "", 1
    if not value or value.startswith("-"):
        raise WorkflowParseError(f"option {name} has no value in: {' '.join(tokens)}")
    return value, index + step


def _pytest_invocation(tokens: list[str], start: int) -> tuple[list[str], list[str], list[str]]:
    """``(targets, ignore_paths, ignore_globs)`` of the pytest command at ``tokens[start:]``."""
    targets: list[str] = []
    ignores: list[str] = []
    globs: list[str] = []
    i = start
    while i < len(tokens):
        before, tok = i, tokens[i]
        if _is_option(tok, "--ignore-glob"):
            value, i = _option_value(tokens, i, "--ignore-glob")
            globs.append(value)
        elif _is_option(tok, "--ignore"):
            value, i = _option_value(tokens, i, "--ignore")
            ignores.append(value.rstrip("/"))
        elif tok in _PYTEST_OPTIONS_WITH_VALUE:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            targets.append(tok.split("::", 1)[0].rstrip("/"))
            i += 1
        if i <= before:
            raise WorkflowParseError(f"option parsing made no progress at {tok!r}")
    return targets, ignores, globs


def _discover_args(tokens: list[str], start: int) -> tuple[str, str]:
    root, pattern = ".", "test*.py"
    i = start
    while i < len(tokens):
        before, tok = i, tokens[i]
        name = _is_option(tok, "-s", "--start-directory")
        if name:
            root, i = _option_value(tokens, i, name)
        else:
            name = _is_option(tok, "-p", "--pattern")
            if name:
                pattern, i = _option_value(tokens, i, name)
            else:
                i += 1
        if i <= before:
            raise WorkflowParseError(f"option parsing made no progress at {tok!r}")
    return root, pattern


def _runner(tokens: list[str]) -> tuple[str, int] | None:
    """``(kind, index_of_first_argument)`` when the command EXECUTES a test runner.

    ``kind`` is ``pytest``, ``unittest``, ``shard`` or ``script`` (a ``python FILE`` run).
    Only the command HEAD counts, so ``echo python tests/test_x.py`` and ``cat test_x.py``
    are not a run; neither is a command that only lists, collects or prints help.
    """
    i = 0
    while i < len(tokens) and (tokens[i] in _WRAPPERS or _ENV_ASSIGNMENT.match(tokens[i])):
        i += 1
    if i >= len(tokens) or any(t in _NO_EXECUTION_FLAGS for t in tokens):
        return None
    head = tokens[i].rsplit("/", 1)[-1]
    if head in ("pytest", "pytest.exe", "py.test"):
        return "pytest", i + 1
    if not _PYTHON_NAME.match(head):
        return None
    i += 1
    while i < len(tokens) and tokens[i].startswith("-"):
        if tokens[i] == "-c":
            return None
        if tokens[i] == "-m":
            module = tokens[i + 1] if i + 1 < len(tokens) else ""
            if module == "pytest":
                return "pytest", i + 2
            if module == "unittest":
                return "unittest", i + 2
            if module.rsplit(".", 1)[-1] == SHARD_MODULE:
                return "shard", i + 2
            return None
        i += 1
    if i < len(tokens):
        if tokens[i].rsplit("/", 1)[-1] == SHARD_MODULE + ".py":
            return "shard", i + 1
        return "script", i
    return None


def collect(
    paths: list[str],
    workflows: dict[str, str],
    shard_script_text: str | None = None,
) -> dict[str, list[tuple[str, str, str]]]:
    """Tracked ``test_*.py`` -> ``[(workflow, step, mechanism)]``; empty list means NONE.

    Raises ``WorkflowParseError`` (naming the workflow and step) on a command it cannot read.
    """
    files = set(paths)
    tests = sorted(p for p in files if _is_test_file(p))
    result: dict[str, list[tuple[str, str, str]]] = {t: [] for t in tests}
    shard_root = shard_discovery(shard_script_text)

    for workflow, text in sorted(workflows.items()):
        for step, lines in run_bodies(text):
            for tokens in _executed_commands(_tokenize(lines)):

                def hit(test: str, mechanism: str) -> None:
                    if (workflow, step, mechanism) not in result[test]:
                        result[test].append((workflow, step, mechanism))

                try:
                    _credit(tokens, tests, files, result, shard_root, hit)
                except WorkflowParseError as exc:
                    raise WorkflowParseError(f"{workflow}: step {step!r}: {exc}") from None
    return result


def _credit(tokens, tests, files, result, shard_root, hit) -> None:
    """Credit every test the ONE command ``tokens`` executes; a non-runner command credits none."""
    found = _runner(tokens)
    if found is None:
        return
    kind, first = found
    if kind == "pytest":
        targets, ignores, globs = _pytest_invocation(tokens, first)
        for test in tests:
            if any(test == i or test.startswith(i + "/") for i in ignores):
                continue
            if any(fnmatch.fnmatch(test, g) for g in globs):
                continue
            for target in targets:
                if test == target:
                    hit(test, "pytest-file")
                elif _under(test, target) and _pytest_collects(test):
                    hit(test, "pytest-dir")
    elif kind == "unittest":
        rest = tokens[first:]
        if rest and rest[0] == "discover":
            root, pattern = _discover_args(tokens, first + 1)
            for test in tests:
                if _discover_collects(test, root, pattern, files):
                    hit(test, "unittest-discover")
            return
        for name in rest:
            if name.startswith("-"):
                continue
            parts = name.split(".")
            for end in range(len(parts), 0, -1):
                module = "/".join(parts[:end]) + ".py"
                if module in result:
                    hit(module, "unittest-module")
                    break
    elif kind == "shard":
        if shard_root and any(t == "--shard" or t.startswith("--shard=") for t in tokens):
            for test in tests:
                if _discover_collects(test, shard_root[0], shard_root[1], files):
                    hit(test, "shard-discover")
    elif tokens[first] in result:
        hit(tokens[first], "named-file")


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


def _bounded(call, seconds: float = 10.0):
    """Run ``call()`` on a daemon thread; a parser that never advances fails, not hangs."""
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = call()
        except BaseException as exc:  # re-raised on the caller's thread
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise AssertionError(f"did not finish within {seconds}s: a parser loop without progress")
    if "error" in box:
        raise box["error"]
    return box["value"]


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

    def _orphan_problems(self, workflow: str) -> list[str]:
        return self._problems({"tools/a/test_orphan.py": "", ".github/workflows/t.yml": workflow})

    def test_a_path_filter_naming_a_test_does_not_run_it(self) -> None:
        problems = self._orphan_problems(
            "on:\n  push:\n    paths:\n      - tools/a/test_orphan.py\n"
            "jobs:\n  j:\n    steps:\n      - run: echo done\n"
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tools/a/test_orphan.py", problems[0])

    def test_a_yaml_key_other_than_run_naming_a_test_does_not_run_it(self) -> None:
        problems = self._orphan_problems(
            "jobs:\n  j:\n    steps:\n"
            "      - uses: actions/github-script@v7\n        with:\n          script: tools/a/test_orphan.py\n"
            "      - name: Env\n        env:\n          TARGET: tools/a/test_orphan.py\n        run: echo $TARGET\n"
        )
        self.assertEqual(len(problems), 1, problems)

    def test_a_command_that_only_names_a_test_does_not_run_it(self) -> None:
        for command in (
            "echo python tools/a/test_orphan.py",
            "echo pytest tools/a",
            "cat tools/a/test_orphan.py",
            "ls tools/a/test_orphan.py",
            "grep -n x tools/a/test_orphan.py",
            "printf '%s' tools/a/test_orphan.py",
            "Copy-Item tools/a/test_orphan.py $env:TEMP",
            "git diff -- tools/a/test_orphan.py",
            'python -c "print(1)" tools/a/test_orphan.py',
            "python -m pip install tools/a/test_orphan.py",
        ):
            problems = self._orphan_problems(_workflow(_step("Names only", command)))
            self.assertEqual(len(problems), 1, (command, problems))

    def test_a_listing_or_help_command_runs_no_test(self) -> None:
        files = {
            "tools/repo_hygiene/__init__.py": "",
            "tools/repo_hygiene/test_in_root.py": "",
            "tools/repo_hygiene/ci_unittest_shard.py": _SHARD_SCRIPT,
        }
        shard = "python -m tools.repo_hygiene.ci_unittest_shard --profile ubuntu --of 3 --shard 1"
        for command in (
            shard + " --list",
            shard + " --list-tests",
            shard + " --help",
            shard + " -h",
            "python -m pytest tools/repo_hygiene --collect-only",
            "python -m pytest tools/repo_hygiene --help",
        ):
            problems = self._problems({**files, ".github/workflows/t.yml": _workflow(_step("List", command))})
            self.assertEqual(len(problems), 1, (command, problems))

    def test_an_executing_runner_is_still_credited_in_every_command_shape(self) -> None:
        for command in (
            "cd tools/a && python tools/a/test_orphan.py",
            "echo start; python -m pytest tools/a/test_orphan.py",
            "echo start || python -m unittest tools.a.test_orphan -v",
            "FOO=1 python -m pytest tools/a",
            "& python -m pytest tools\\a\\test_orphan.py -q",
            "py -3 -m pytest tools/a",
            "pytest tools/a -q",
            "python -u -m unittest tools.a.test_orphan",
            "if ($true) { python -m pytest tools/a }",
        ):
            self.assertEqual(self._orphan_problems(_workflow(_step("Run", command))), [], command)
        self.assertEqual(
            self._orphan_problems("jobs:\n  j:\n    steps:\n      - run: python -m pytest tools/a -q\n"), []
        )

    def test_text_inside_quotes_is_never_a_runner_invocation(self) -> None:
        for command in (
            'echo "scheduled; python tools/a/test_orphan.py"',
            'echo "x | python tools/a/test_orphan.py"',
            "echo 'a && pytest tools/a'",
            'echo "a || python -m unittest tools.a.test_orphan"',
            'echo "say \\"hi\\"; python tools/a/test_orphan.py"',
            'Write-Host "ran; python tools/a/test_orphan.py"',
            'echo "line one\npython tools/a/test_orphan.py"',
        ):
            problems = self._orphan_problems(_workflow(_step("Quoted", command)))
            self.assertEqual(len(problems), 1, (command, problems))

    def test_a_runner_after_or_around_quoted_text_is_still_credited(self) -> None:
        for command in (
            'echo "ok"; python tools/a/test_orphan.py',
            'echo "a; b" && python -m pytest tools/a',
            'python "tools/a/test_orphan.py"',
            "python -m pytest 'tools/a' -k 'x or y'",
            'echo "done" | python -m unittest tools.a.test_orphan',
        ):
            self.assertEqual(self._orphan_problems(_workflow(_step("Run", command))), [], command)

    def test_a_function_body_that_is_never_a_command_credits_nothing(self) -> None:
        for command in (
            "function Invoke-Tests { python tools/a/test_orphan.py }; Write-Host scheduled",
            "function Invoke-Tests {\n  python tools/a/test_orphan.py\n}\nWrite-Host scheduled",
            "function Invoke-Tests\n{\n  python -m pytest tools/a\n}",
            "function Invoke-Tests ($a) {\n  if ($a) { python tools/a/test_orphan.py }\n}",
            "run_tests() {\n  python tools/a/test_orphan.py\n}\necho scheduled",
            "run_tests ()\n{\n  python -m pytest tools/a\n}",
            "function run_tests {\n  python tools/a/test_orphan.py\n}",
            "function run_tests() { python tools/a/test_orphan.py; }",
            "filter F { python tools/a/test_orphan.py }",
        ):
            problems = self._orphan_problems(_workflow(_step("Defined", command)))
            self.assertEqual(len(problems), 1, (command, problems))
        inline = "jobs:\n  j:\n    steps:\n      - run: function Invoke-Tests { python tools/a/test_orphan.py }; Write-Host scheduled\n"
        self.assertEqual(len(self._orphan_problems(inline)), 1)

    def test_a_command_outside_the_function_body_is_still_credited(self) -> None:
        for command in (
            "function F { echo x }\npython tools/a/test_orphan.py",
            "function F { if ($x) { echo y }; echo z }\npython -m pytest tools/a",
            "f() {\n  echo x\n}\nf\npython tools/a/test_orphan.py",
            "function F { exit 1 }\npython tools/a/test_orphan.py",
            "foreach ($t in $tests) { python tools/a/test_orphan.py }",
            "echo '${{ matrix.x }}'; python -m pytest ${{ matrix.y }} tools/a",
        ):
            self.assertEqual(self._orphan_problems(_workflow(_step("Run", command))), [], command)

    def test_nothing_after_an_unconditional_top_level_exit_is_credited(self) -> None:
        for command in (
            "exit 0\npython tools/a/test_orphan.py",
            "echo hi; exit\npython -m pytest tools/a",
            "exit $LASTEXITCODE\npython tools/a/test_orphan.py",
            "echo hi\nExit 0\npython tools/a/test_orphan.py",
            "return\npython tools/a/test_orphan.py",
            "exit 0; python tools/a/test_orphan.py",
        ):
            problems = self._orphan_problems(_workflow(_step("Exits", command)))
            self.assertEqual(len(problems), 1, (command, problems))
        files = {
            "tools/repo_hygiene/__init__.py": "",
            "tools/repo_hygiene/test_probe.py": "",
            "tools/repo_hygiene/ci_unittest_shard.py": _SHARD_SCRIPT,
        }
        early = _workflow(
            _step("Shard", "exit 0\npython -m tools.repo_hygiene.ci_unittest_shard --profile ubuntu --of 3 --shard 1")
        )
        problems = self._problems({**files, ".github/workflows/t.yml": early})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("test_probe.py", problems[0])

    def test_an_exit_that_is_conditional_or_after_the_runner_cuts_nothing(self) -> None:
        for command in (
            "python tools/a/test_orphan.py\nexit 0",
            "test -f x || exit 1\npython tools/a/test_orphan.py",
            "test -f x && exit 0\npython tools/a/test_orphan.py",
            "if ($x) { exit 1 }\npython tools/a/test_orphan.py",
            'if [ -z "$X" ]; then\n  exit 1\nfi\npython tools/a/test_orphan.py',
            "for f in a b; do\n  exit 1\ndone\npython tools/a/test_orphan.py",
            "python -m pytest tools/a\nif ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }",
            "python tools/a/test_orphan.py || exit 1",
            "echo 'exit 0'\npython tools/a/test_orphan.py",
        ):
            self.assertEqual(self._orphan_problems(_workflow(_step("Run", command))), [], command)

    def test_an_exit_in_one_step_does_not_cut_the_next_step(self) -> None:
        workflow = _workflow(_step("First", "exit 0"), _step("Second", "python tools/a/test_orphan.py"))
        self.assertEqual(self._orphan_problems(workflow), [])

    def test_a_folded_run_scalar_credits_nothing(self) -> None:
        # YAML folds the body to ONE line ("echo scheduled python tools/a/test_orphan.py"), which
        # prints and runs nothing; this reader is line-wise, so a folded body earns no credit.
        for indicator in (">", ">-", ">+", ">2", ">-2", ">2-", "> # folded"):
            workflow = (
                "jobs:\n  j:\n    steps:\n      - run: " + indicator + "\n"
                "          echo scheduled\n          python tools/a/test_orphan.py\n"
            )
            problems = self._orphan_problems(workflow)
            self.assertEqual(len(problems), 1, (indicator, problems))
            self.assertIn("tools/a/test_orphan.py", problems[0])

    def test_a_folded_body_does_not_hide_or_poison_the_next_step(self) -> None:
        workflow = (
            "jobs:\n  j:\n    steps:\n      - name: Folded\n        run: >-\n"
            "          python tools/a/test_orphan.py\n"
            "      - name: Literal\n        run: |\n          python tools/a/test_other.py\n"
        )
        files = {"tools/a/test_orphan.py": "", "tools/a/test_other.py": "", ".github/workflows/t.yml": workflow}
        problems = self._problems(files)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("tools/a/test_orphan.py", problems[0])

    def test_every_literal_block_indicator_still_credits(self) -> None:
        for indicator in ("|", "|-", "|+", "|2", "|-2", "|2-", "| # literal"):
            workflow = (
                "jobs:\n  j:\n    steps:\n      - run: " + indicator + "\n"
                "          python tools/a/test_orphan.py\n"
            )
            self.assertEqual(self._orphan_problems(workflow), [], indicator)

    def test_an_unrecognised_block_indicator_credits_nothing(self) -> None:
        workflow = "jobs:\n  j:\n    steps:\n      - run: |x\n          python tools/a/test_orphan.py\n"
        self.assertEqual(len(self._orphan_problems(workflow)), 1)

    def test_an_option_without_a_value_fails_fast_naming_the_workflow_and_option(self) -> None:
        for command, option in (
            ("python -m pytest tools/a --ignore", "--ignore"),
            ("python -m pytest tools/a --ignore-glob", "--ignore-glob"),
            ("python -m pytest tools/a --ignore=", "--ignore"),
            ("python -m pytest tools/a --ignore -q", "--ignore"),
            ("python -m unittest discover -s", "-s"),
        ):
            with self.assertRaises(ValueError, msg=command) as caught:
                _bounded(lambda: collect(["tools/a/test_orphan.py"], {"t.yml": f"run: {command}"}, None))
            self.assertIn("t.yml", str(caught.exception), command)
            self.assertIn(option, str(caught.exception), command)

    def test_an_option_that_only_starts_like_ignore_is_a_plain_flag_and_terminates(self) -> None:
        table = _bounded(
            lambda: collect(
                ["tools/a/test_orphan.py"], {"t.yml": "run: python -m pytest tools/a --ignored-thing"}, None
            )
        )
        self.assertTrue(table["tools/a/test_orphan.py"])

    def test_option_value_always_advances_or_raises(self) -> None:
        for tokens in (["--ignore", "x"], ["--ignore=x"], ["--ignore"], ["--ignore", "-q"], ["--ignore="]):
            try:
                value, index = _bounded(lambda: _option_value(tokens, 0, "--ignore"))
            except ValueError:
                continue
            self.assertGreater(index, 0, tokens)
            self.assertEqual(value, "x", tokens)

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
