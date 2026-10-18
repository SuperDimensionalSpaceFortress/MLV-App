#!/usr/bin/env python3
"""Guard: doc splits must not drop substrings that closeout asserts.

This is the companion to the pinned check-doc-size (`tools/docs/run_doc_size.py`). That tool measures SIZE.
This one measures the thing that makes shrinking a doc dangerous *in this repo*.

Why it exists
    `tools/repo_hygiene/brokered_closeout.py` runs a tooling-baseline check built
    from exact {"path": ..., "contains": ...} assertions, and
    `tools/repo_hygiene/test_brokered_closeout.py` adds a second, independent set
    of plain `assertIn("literal", <doc>_text)` checks -- including a
    `for text in (a_text, b_text, ...)` loop form.

    Both bind a substring to a PATH. So demoting prose out of CLAUDE.md breaks
    them exactly as a rename would: the baseline raises `closeout_tooling_stale`
    and blocks finalize, or the suite reds. The obvious way to make an entry-tier
    doc fit a byte budget -- deleting prose -- is therefore the dangerous one.

    A split must keep every pinned token RESIDENT in the parent index, on the
    pointer line for the section that now holds its prose.

    This is not hypothetical: the first CLAUDE.md split in this repo satisfied
    the structured baseline, dropped 12 test-pinned tokens, and red-lighted
    `test_repo_state_dashboard_and_rollback_contract_required`. Parsing only one
    of the two surfaces is what caused it, which is why this reads both, and
    reads the test suite via AST rather than by pattern-matching text.

Both lists are DERIVED at runtime, never hardcoded, so this cannot drift from the
assertions it protects.

Exit status:
  0  every asserted substring resolves
  1  at least one is missing
  2  a pinning surface could not be read or parsed
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys

ASSERTION_SOURCE = "tools/repo_hygiene/brokered_closeout.py"
# Every test module that pins substrings against a governed doc. Adding a source here also
# fixes tools/docs/split_claude_md.py --refresh-tokens, which derives from this module.
#
# test_candidate_acceptance.py was the third surface and was missing: after a sanctioned
# CLAUDE.md split its five authority-boundary sentences went absent, check_pinned_tokens
# still exited 0, and the repo hygiene suite went red on
# test_agent_doctrine_matches_two_phase_acceptance_authority_boundary. A checker that knows
# about a subset of the pinning surfaces reports success for the surfaces it happens to know.
TEST_ASSERTION_SOURCES = (
    "tools/repo_hygiene/test_brokered_closeout.py",
    "tools/repo_hygiene/test_candidate_acceptance.py",
    # Fourth surface, found 2026-09-05 the same way the third was: an AGENTS.md split passed
    # this checker and still reddened the suite. test_repo_hygiene.py pins
    # "Human approval remains mandatory today" against AGENTS.md -- an AUTHORITY BOUNDARY, the
    # same category as the CLAUDE.md sentences that motivated adding the third source.
    # The lesson repeats: this list is only as complete as the last time someone looked.
    "tools/repo_hygiene/test_repo_hygiene.py",
)
# Retained for callers that referenced the single-source name.
TEST_ASSERTION_SOURCE = TEST_ASSERTION_SOURCES[0]

_ASSERTION_RE = re.compile(
    r'\{"path":\s*"([^"]+)",\s*"contains":\s*"((?:[^"\\]|\\.)*)"\}'
)


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _read(repo_root: str, rel: str):
    path = os.path.join(repo_root, rel)
    if not os.path.exists(path):
        return None, "assertion source not found: %s" % rel
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read(), None
    except OSError as exc:
        return None, "cannot read %s: %s" % (rel, exc)


def _doc_path_of(node):
    """Extract the relative doc path from `(ROOT / ... / "X.md").read_text(...)`.

    Accumulates every constant component of the `/` chain, so
    `ROOT / "docs" / "19-x.md"` yields "docs/19-x.md" rather than "19-x.md".
    Getting this wrong mis-attributes assertions to a nonexistent top-level file
    and reports them as spurious "file missing" rows.
    """
    if not isinstance(node, ast.Call):
        return None
    fn = node.func
    if not (isinstance(fn, ast.Attribute) and fn.attr == "read_text"):
        return None
    parts = []
    inner = fn.value
    while isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.Div):
        if isinstance(inner.right, ast.Constant) and isinstance(inner.right.value, str):
            parts.append(inner.right.value)
        inner = inner.left
    if not parts:
        return None
    rel = _norm("/".join(reversed(parts)))
    return rel if rel.endswith(".md") else None


def derive_test_pinned(repo_root: str):
    """Parse every pinning TEST module for `assertIn("literal", <doc>_text)` checks."""
    merged = {}
    for source in TEST_ASSERTION_SOURCES:
        one, err = _derive_test_pinned_one(repo_root, source)
        if err:
            return {}, err
        for path, needles in one.items():
            merged.setdefault(path, set()).update(needles)
    return merged, None


def _derive_test_pinned_one(repo_root: str, source: str):
    src, err = _read(repo_root, source)
    if err:
        return {}, err
    try:
        tree = ast.parse(src)
    except SyntaxError as exc:
        return {}, "cannot parse %s: %s" % (source, exc)

    pinned = {}

    def record(path, needle):
        if path and needle:
            pinned.setdefault(path, set()).add(needle)

    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        # var name -> doc path, e.g. claude_text -> CLAUDE.md
        var_map = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                doc = _doc_path_of(node.value)
                if isinstance(tgt, ast.Name) and doc:
                    var_map[tgt.id] = doc

        # loop var -> every doc path it iterates over
        loop_map = {}
        for node in ast.walk(fn):
            if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
                if isinstance(node.iter, (ast.Tuple, ast.List)):
                    docs = [var_map.get(e.id) for e in node.iter.elts
                            if isinstance(e, ast.Name)]
                    docs = [d for d in docs if d]
                    if docs:
                        loop_map[node.target.id] = docs

        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "assertIn"
                    and len(node.args) >= 2):
                continue
            needle_node, hay = node.args[0], node.args[1]
            if not (isinstance(needle_node, ast.Constant)
                    and isinstance(needle_node.value, str)):
                continue
            needle = needle_node.value
            if isinstance(hay, ast.Name):
                if hay.id in var_map:
                    record(var_map[hay.id], needle)
                elif hay.id in loop_map:
                    for d in loop_map[hay.id]:
                        record(d, needle)
            else:
                record(_doc_path_of(hay), needle)

    return {k: sorted(v) for k, v in pinned.items()}, None


def derive_pinned(repo_root: str):
    """Union of both pinning surfaces: {doc path -> [substring, ...]}."""
    text, err = _read(repo_root, ASSERTION_SOURCE)
    if err:
        return None, err

    pinned = {}
    for path, needle in _ASSERTION_RE.findall(text):
        if not path.endswith(".md"):
            continue
        pinned.setdefault(_norm(path), []).append(
            needle.encode().decode("unicode_escape"))

    from_tests, test_err = derive_test_pinned(repo_root)
    for path, needles in from_tests.items():
        bucket = pinned.setdefault(path, [])
        for n in needles:
            if n not in bucket:
                bucket.append(n)

    return pinned, test_err


def check_pinned(repo_root: str):
    """Verify every asserted substring still resolves in its target doc."""
    pinned, err = derive_pinned(repo_root)
    if err or pinned is None:
        return [], err
    missing = []
    for path, needles in sorted(pinned.items()):
        full = os.path.join(repo_root, path)
        if not os.path.exists(full):
            missing.extend({"path": path, "needle": n, "why": "file missing"}
                           for n in needles)
            continue
        with open(full, encoding="utf-8") as fh:
            body = fh.read()
        for needle in needles:
            if needle not in body:
                missing.append({"path": path, "needle": needle,
                                "why": "substring absent"})
    return missing, None


def hook_mode(repo_root: str, path: str):
    """PostToolUse mode: report only on the edited file. Always fails open."""
    rel = _norm(os.path.relpath(path, repo_root)) if os.path.isabs(path) else _norm(path)
    if not rel.endswith(".md"):
        return 0
    missing, err = check_pinned(repo_root)
    if err or not missing:
        return 0
    hit = [m for m in missing if m["path"] == rel]
    if not hit:
        return 0
    note = (
        "PINNED SUBSTRING REGRESSION in %s: %d asserted token(s) no longer present "
        "(e.g. %r). These are asserted by %s and %s. Restore them in the index -- "
        "on the pointer line for the section that now holds the prose -- or closeout "
        "fails with closeout_tooling_stale and the suite reds. Regenerate with: "
        "%s tools/docs/split_claude_md.py --refresh-tokens"
        % (rel, len(hit), hit[0]["needle"][:60],
           ASSERTION_SOURCE, TEST_ASSERTION_SOURCE, sys.executable)
    )
    sys.stdout.write(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": note,
    }}))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--hook", action="store_true",
                    help="PostToolUse mode: read hook JSON on stdin, fail open")
    args = ap.parse_args(argv)

    repo_root = args.repo_root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    repo_root = os.path.abspath(repo_root)

    if args.hook:
        # Fail open on absolutely anything -- must never brick an edit.
        try:
            data = json.load(sys.stdin)
            if not isinstance(data, dict):
                return 0
            if data.get("tool_name") not in ("Edit", "Write", "MultiEdit"):
                return 0
            tool_input = data.get("tool_input") or {}
            path = tool_input.get("file_path") or ""
            if not isinstance(path, str) or not path:
                return 0
            return hook_mode(repo_root, path)
        except Exception:
            return 0

    pinned, err = derive_pinned(repo_root)
    if err and pinned is None:
        sys.stderr.write("%s\n" % err)
        return 2
    missing, _ = check_pinned(repo_root)

    if args.as_json:
        print(json.dumps({
            "schema": "pinned-token-check.v1",
            "sources": [ASSERTION_SOURCE, TEST_ASSERTION_SOURCE],
            "pinned": {k: sorted(v) for k, v in (pinned or {}).items()},
            "missing": missing,
        }, indent=2))
        return 1 if missing else 0

    total = sum(len(v) for v in (pinned or {}).values())
    print("pinned-token check -- %d assertions across %d docs"
          % (total, len(pinned or {})))
    for path, needles in sorted((pinned or {}).items()):
        print("  %-50s %d" % (path, len(needles)))
    print()
    if missing:
        print("MISSING (%d) -- restore these in the parent index:" % len(missing))
        for m in missing:
            print("  %s :: %s (%s)" % (m["path"], m["needle"][:70], m["why"]))
        return 1
    print("PASS -- every asserted substring resolves.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
