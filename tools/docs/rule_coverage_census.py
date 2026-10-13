#!/usr/bin/env python3
"""Census: which rule bullets in our contract docs have a check behind them?

Why it exists
    CLAUDE.md and agents/*.md state rules as list-item bullets. Nothing counted how many
    of those bullets are enforced by a guard, a test or a hook, so "the contract says X"
    and "something fails when X is violated" drifted apart without anyone noticing. The
    airmypc fleet measured the same gap (3 of 42 contract bullets had a check, 7.14%,
    and two unchecked rules had live incidents; bus TRAPS.md:28188). This tool adopts
    that idea only: a census, no pre-commit step, no PowerShell.

What it does
    1. Walks the rule surfaces at the working tree: CLAUDE.md and agents/*.md. Every
       list-item bullet (`-`, `*`, `+`, `1.`, `1)`, nested or not, outside code fences)
       is one rule. A bullet runs from its marker line through any wrapped continuation
       lines, up to a blank line, heading, fence, table row or the next marker. A fence
       is closed only by the same character at least as long as its opener with nothing
       after it (CommonMark), so a ```` fence may hold ``` lines and ~~~ holds both.
    2. Gives each bullet a stable id: sha256 of its NORMALIZED text (see normalize()).
    3. Reads the tracked mapping file (tools/docs/rule-coverage-map.json) which maps an
       id to exactly one check, spelled as one of:
           guard:<NA-id>                a row of docs/never-authorized.json that has a
                                        real enforced_today (not "prose-only")
           test:<path>::[Class::]name   one test CASE: a def in a .py file at EXACTLY the
                                        scope spelled (a bare name is a module-level def,
                                        not any class's method), or a `-Name "<case>"` /
                                        It / function in a .ps1 (comments and here-strings
                                        do not count). Any other file type is refused. A
                                        file alone, or a class alone, is refused:
                                        deleting the asserting case must not leave the
                                        mapping standing. A case that a module-level
                                        `for` loop adds with setattr(<Class>, ...) from a
                                        table is named Class::test_<slug of the row's
                                        "name">; it resolves only while that live setattr
                                        and the row both exist
           hook:<path>                  a hook script that exists
           prose-only: <reason>         an admission that nothing checks it
       An entry may be an object with an optional "strength": "token-presence-pin"
       (the check only asserts that a token string is present in the doc); anything else
       is behavioural, except that an entry for a "Pinned contract tokens" bullet with no
       strength defaults to the pin. An unknown strength is a mapping error.
       Every path (test, hook, and the --map argument) is resolved with symlinks and
       junctions followed and must stay inside the repo root.
    4. Prints the BEHAVIOURAL coverage as the headline: behavioural checks over the rule
       bullets (the "Pinned contract tokens" list in CLAUDE.md is a derived inventory of
       strings, not rules, so it is excluded from the denominator). Token-presence pins are
       counted and printed apart; the figure that includes them is labelled "all mapped".
       Tests that exist but that hosted CI never collects (the LOCAL_ONLY_OR_UNCOLLECTED
       list in tools/repo_hygiene/test_ci_collects_every_test.py, or a non-python test no
       workflow names) are labelled "exists, not enforced in CI" and kept out of the
       "enforced in CI" line. A prose-only label NEVER raises coverage; it only moves a
       bullet out of the unmapped list.
    5. Fails (exit 1) when a mapping is dishonest: it names a guard, test, test case or
       hook that no longer exists (a missing case is reported as STALE), a guard that is
       itself prose-only, or an id that no longer matches any bullet (STALE: the bullet
       was reworded or deleted, so whoever did that must re-point or drop the mapping).
       Report-only otherwise: an unmapped bullet is counted and listed, never a failure.
       The ratchet that would make a rising unmapped count fail is a later card
       (RULE-COVERAGE-RATCHET-1).

Normalization (the id contract; changing it re-ids every bullet, so do not)
    Applied to the bullet text with its list marker removed and continuation lines joined:
      1. Unicode NFC.
      2. Backtick code spans are exempt from emphasis stripping (step 3), nothing else.
      3. Outside code spans: markdown emphasis markers removed (`**x**`, `__x__`, `*x*`,
         `_x_`; single markers only when they hug a word, so snake_case and a lone `*`
         survive).
      4. All whitespace runs (spaces, tabs, newlines) collapsed to one space; trimmed.
    Whitespace inside a code span is collapsed too, so a code span wrapped across two lines
    keeps its id. Case and punctuation are preserved, so a reworded rule gets a new id on purpose.
    A whitespace-only or emphasis-only edit does not change the id.

Exit status:
  0  every mapping resolves (unmapped bullets are reported, not failed)
  1  at least one mapping is stale, dangling or dishonest
  2  a surface or the mapping file could not be read or parsed, or --map escapes the repo
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sys
import unicodedata

SCHEMA = "rule-coverage-census.v1"
MAP_SCHEMA = "rule-coverage-map.v1"
MAP_PATH = "tools/docs/rule-coverage-map.json"
GUARD_REGISTRY = "docs/never-authorized.json"
CI_COLLECT_GUARD = "tools/repo_hygiene/test_ci_collects_every_test.py"
CI_EXCEPTIONS_NAME = "LOCAL_ONLY_OR_UNCOLLECTED"
WORKFLOW_DIR = ".github/workflows"
SURFACE_FILES = ("CLAUDE.md",)
SURFACE_DIRS = ("agents",)
PIN_STRENGTH = "token-presence-pin"
STRENGTHS = ("behavioural", PIN_STRENGTH)

_ITEM_RE = re.compile(r"^(\s*)(?:[-*+]|\d+[.)])\s+(.*)$")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE = re.compile(r"^\s*(`{3,}|~{3,})\s*$")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}(\s|$)")
_INVENTORY_HEADING_RE = re.compile(r"^pinned contract tokens\b", re.IGNORECASE)
_TABLE_RE = re.compile(r"^\s*\|")
_CODE_SPAN_RE = re.compile(r"(`[^`]*`)")
_STRONG_RE = re.compile(r"\*\*|__")
_STAR_EM_RE = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")
_UNDER_EM_RE = re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)")
_PROSE_ONLY_RE = re.compile(r"^prose-only:\s*(\S.*)$", re.DOTALL)
_PS_CASE_RES = (
    re.compile(r"""-Name\s+(["'])(.+?)\1"""),
    re.compile(r"""\b(?:It|Describe|Context)\s+(["'])(.+?)\1"""),
)
_PS_FUNCTION_RE = re.compile(r"(?im)^\s*function\s+([\w-]+)")
_PS_TOKEN_START = " \t\r\n;(){}|,="
CASE_FILE_TYPES = (".py", ".ps1")


def _norm_path(path: str) -> str:
    return path.replace("\\", "/")


def normalize(text: str) -> str:
    """Return the canonical form hashed into a bullet id (see the module docstring)."""
    text = unicodedata.normalize("NFC", text)
    parts = _CODE_SPAN_RE.split(text)
    for i in range(0, len(parts), 2):  # even indices are outside code spans
        piece = _STRONG_RE.sub("", parts[i])
        piece = _STAR_EM_RE.sub(r"\1", piece)
        piece = _UNDER_EM_RE.sub(r"\1", piece)
        parts[i] = piece
    return " ".join("".join(parts).split())


def bullet_id(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def surface_paths(repo_root: str):
    """Repo-relative rule surfaces that exist, in a stable order."""
    paths = [p for p in SURFACE_FILES if os.path.isfile(os.path.join(repo_root, p))]
    for d in SURFACE_DIRS:
        full = os.path.join(repo_root, d)
        if os.path.isdir(full):
            paths.extend("%s/%s" % (d, n) for n in sorted(os.listdir(full))
                         if n.endswith(".md"))
    return paths


def _fence_open(line: str):
    """(char, length) when the line opens a code fence, else None. A backtick fence cannot
    carry a backtick in its info string (that is inline code, not a fence)."""
    m = _FENCE_OPEN_RE.match(line)
    if not m:
        return None
    marker = m.group(1)
    if marker[0] == "`" and "`" in m.group(2):
        return None
    return marker[0], len(marker)


def _fence_closes(line: str, fence) -> bool:
    m = _FENCE_CLOSE_RE.match(line)
    return bool(m) and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]


def _scan_bullets(body: str):
    """[(line_number, raw_text, section_heading)] for every list-item bullet outside fences."""
    lines = body.splitlines()
    fence = None
    section = ""
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if fence:
            if _fence_closes(line, fence):
                fence = None
            i += 1
            continue
        opener = _fence_open(line)
        if opener:
            fence = opener
            i += 1
            continue
        if _HEADING_RE.match(line):
            section = line.strip().strip("#").strip()
            i += 1
            continue
        m = _ITEM_RE.match(line)
        if not m:
            i += 1
            continue
        chunk = [m.group(2)]
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if (not nxt.strip() or _ITEM_RE.match(nxt) or _fence_open(nxt)
                    or _HEADING_RE.match(nxt) or _TABLE_RE.match(nxt)):
                break
            chunk.append(nxt)
            j += 1
        out.append((i + 1, "\n".join(chunk), section))
        i = j
    return out


def extract_bullets(body: str):
    """Yield (line_number, raw_text) for every list-item bullet outside code fences."""
    return [(line, raw) for line, raw, _ in _scan_bullets(body)]


def collect_bullets(repo_root: str):
    """Every bullet on every surface: [{file, line, id, text, section, token_inventory}].
    Raises OSError on read."""
    bullets = []
    for rel in surface_paths(repo_root):
        with open(os.path.join(repo_root, rel), encoding="utf-8-sig") as fh:
            body = fh.read()
        for line, raw, section in _scan_bullets(body):
            bullets.append({"file": rel, "line": line, "id": bullet_id(raw),
                            "text": normalize(raw), "section": section,
                            "token_inventory": bool(_INVENTORY_HEADING_RE.match(section))})
    return bullets


def _inside(repo_root: str, path: str) -> bool:
    """True when path, with every symlink and junction resolved, stays under repo_root."""
    root = os.path.normcase(os.path.realpath(repo_root))
    real = os.path.normcase(os.path.realpath(path))
    try:
        return os.path.commonpath([root, real]) == root
    except ValueError:  # different drives
        return False


def load_map(repo_root: str, map_path: str | None = None):
    """Return ({id: entry}, error). A missing map file is an empty map, not an error."""
    path = os.path.abspath(map_path) if map_path else os.path.join(repo_root, MAP_PATH)
    if not _inside(repo_root, path):
        return None, ("mapping file %s resolves outside the repo root %s; a mapping must be "
                      "tracked inside the repository" % (path, repo_root))
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "cannot read mapping file %s: %s" % (path, exc)
    if not isinstance(data, dict) or not isinstance(data.get("mappings"), dict):
        return None, "mapping file %s must be an object with a 'mappings' object" % path
    return data["mappings"], None


def load_guards(repo_root: str):
    """{guard id: enforced_today text} from docs/never-authorized.json, or ({}, error)."""
    path = os.path.join(repo_root, GUARD_REGISTRY)
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return None, "cannot read %s: %s" % (GUARD_REGISTRY, exc)
    guards = {}
    for item in data.get("items", []) if isinstance(data, dict) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            enforced = item.get("enforced_today")
            guards[item["id"]] = enforced if isinstance(enforced, str) else ""
    return guards, None


def _resolve_file(repo_root: str, rel: str):
    """(absolute path, None) for a repo-relative file, else (None, why) where why is
    'does not exist' (absent, or lexically outside) or 'resolves outside the repo root'
    (a symlink or junction under the repo points out of it)."""
    rel = _norm_path(rel.strip())
    if not rel or rel.startswith("/") or ":" in rel or ".." in rel.split("/"):
        return None, "does not exist"
    full = os.path.join(repo_root, rel)
    if not os.path.isfile(full):
        return None, "does not exist"
    if not _inside(repo_root, full):
        return None, "resolves outside the repo root"
    return full, None


def _read(path: str) -> str:
    with open(path, encoding="utf-8-sig", errors="replace") as fh:
        return fh.read()


def _py_defs(body: str):
    """{scope tuple: {'def'|'class'}} for every def/class in the module, any nesting."""
    out = {}

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = scope + (child.name,)
                kind = "class" if isinstance(child, ast.ClassDef) else "def"
                out.setdefault(q, set()).add(kind)
                walk(child, q)
            elif isinstance(child, ast.stmt):
                walk(child, scope)

    walk(ast.parse(body), ())
    return out


def _slug(name: str) -> str:
    """The test-name slug of a generated case: the convention the table-driven suites use
    (`test_` + the row name lower-cased with every non-alphanumeric run turned into `_`)."""
    return "test_" + re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _walk_live(stmts):
    """Every node reachable from these statements WITHOUT entering a def, class or lambda
    body (those only run when something calls them)."""
    todo = list(stmts)
    while todo:
        node = todo.pop()
        yield node
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef,
                                      ast.Lambda)):
                todo.append(child)


def _py_generated(tree):
    """{class name: {generated test name}} for classes that a MODULE-LEVEL loop adds test
    methods to with setattr(<Class>, ...), the names being `_slug(<row name>)` for every
    `"name": "<row name>"` entry of a dict literal in the file. A setattr that was removed,
    commented out, or sits in a def nothing calls contributes nothing, so deleting the
    generator turns every case it generated stale."""
    classes = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
            for node in _walk_live(stmt.body):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                        and node.func.id == "setattr" and node.args
                        and isinstance(node.args[0], ast.Name)):
                    classes.add(node.args[0].id)
    if not classes:
        return {}
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and k.value == "name"
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    names.add(_slug(v.value))
    return {c: set(names) for c in classes}


def _ps_code_only(body: str) -> str:
    """The PowerShell source with comments (`# ...`, `<# ... #>`) and here-string bodies
    blanked, newlines kept. A case name that only appears in a comment or a here-string is
    not a case: nothing runs it."""
    out = []
    i, n = 0, len(body)

    def blank(chunk: str) -> str:
        return "".join(c if c in "\r\n" else " " for c in chunk)

    while i < n:
        c = body[i]
        if body.startswith("<#", i):
            end = body.find("#>", i + 2)
            end = n if end < 0 else end + 2
            out.append(blank(body[i:end]))
            i = end
        elif c == "@" and i + 1 < n and body[i + 1] in "'\"" \
                and re.match(r"[ \t]*\r?\n", body[i + 2:]):
            m = re.compile(r"(?m)^%s@" % re.escape(body[i + 1])).search(body, i + 2)
            end = n if not m else m.end()
            out.append(blank(body[i:end]))
            i = end
        elif c in "'\"":
            j = i + 1
            while j < n:
                if c == '"' and body[j] == "`":
                    j += 2
                    continue
                if body[j] == c:
                    break
                j += 1
            out.append(body[i:j + 1])
            i = j + 1
        elif c == "`":
            out.append(body[i:i + 2])
            i += 2
        elif c == "#" and (i == 0 or body[i - 1] in _PS_TOKEN_START):
            j = body.find("\n", i)
            j = n if j < 0 else j
            out.append(blank(body[i:j]))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _ps_cases(body: str):
    body = _ps_code_only(body)
    cases ={m.group(2) for rx in _PS_CASE_RES for m in rx.finditer(body)}
    cases.update(_PS_FUNCTION_RE.findall(body))
    return cases


def _case_error(full: str, rel: str, names):
    """Why the named test case cannot be found in the file, or None when it exists."""
    names = [n.split("[", 1)[0].strip() for n in names]
    if not names or not all(names):
        return ("test %r must name a test case: test:<path>::[Class::]<test> (a file alone "
                "survives deleting the case that asserts the rule)" % rel)
    try:
        body = _read(full)
    except OSError as exc:
        return "test file %r cannot be read: %s" % (rel, exc)
    spelled = "::".join(names)
    if rel.endswith(".py"):
        try:
            defs = _py_defs(body)
            generated = _py_generated(ast.parse(body))
        except (SyntaxError, ValueError) as exc:
            return "test file %r does not parse: %s" % (rel, exc)
        want = tuple(names)  # the scope the map spells, exactly: no suffix matching
        kinds = defs.get(want, set())
        if "def" in kinds:
            return None
        if kinds:
            return ("test %r names %r, a class, not a test case; name the method or function"
                    % (rel, spelled))
        if len(want) == 2 and want[1] in generated.get(want[0], ()):
            return None
    elif rel.endswith(".ps1"):
        if spelled in _ps_cases(body):
            return None
    else:
        return ("test %r is not a %s file, so no test case in it can be named; map a .py or "
                ".ps1 case" % (rel, " or ".join(CASE_FILE_TYPES)))
    return ("stale: test %r has no definition named %r (the asserting case was renamed or "
            "deleted; re-point or drop the mapping)" % (rel, spelled))


def classify(check, repo_root: str, guards, info: dict | None = None):
    """Resolve one mapping value. Returns (kind, error): kind in
    guard|test|hook|prose-only, or None with an error string. `info`, when given, receives
    'test_path' (repo-relative) for a resolved test."""
    if isinstance(check, dict):
        check = check.get("check")
    if not isinstance(check, str) or not check.strip():
        return None, "mapping has no check string"
    check = check.strip()
    prose = _PROSE_ONLY_RE.match(check)
    if prose:
        return "prose-only", None
    if check.startswith("prose-only"):
        return None, "prose-only needs a reason: 'prose-only: <reason>'"
    if check.startswith("guard:"):
        gid = check[len("guard:"):].strip()
        if gid not in guards:
            return None, "guard %r is not in %s" % (gid, GUARD_REGISTRY)
        if guards[gid].strip().lower().startswith("prose-only"):
            return None, ("guard %r is itself prose-only today, so it checks nothing"
                          % gid)
        return "guard", None
    if check.startswith("test:"):
        spec = check[len("test:"):].strip()
        rel, *names = [s.strip() for s in spec.split("::")]
        full, why = _resolve_file(repo_root, rel)
        if not full:
            return None, "test file %r %s" % (rel, why)
        err = _case_error(full, _norm_path(rel), names)
        if err:
            return None, err
        if info is not None:
            info["test_path"] = _norm_path(rel)
        return "test", None
    if check.startswith("hook:"):
        rel = check[len("hook:"):].strip()
        full, why = _resolve_file(repo_root, rel)
        if not full:
            return None, "hook file %r %s" % (rel, why)
        return "hook", None
    return None, ("unrecognized check %r (use guard:, test:, hook: or prose-only:)"
                  % check[:60])


def load_ci_exceptions(repo_root: str):
    """{repo-relative path: reason} read from LOCAL_ONLY_OR_UNCOLLECTED in the CI collection
    guard (parsed, never imported), or None when it cannot be read."""
    full, _ = _resolve_file(repo_root, CI_COLLECT_GUARD)
    if not full:
        return None
    try:
        tree = ast.parse(_read(full))
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        else:
            continue
        if (isinstance(target, ast.Name) and target.id == CI_EXCEPTIONS_NAME
                and isinstance(value, ast.Dict)):
            out = {}
            for k, v in zip(value.keys, value.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    try:
                        reason = ast.literal_eval(v)
                    except ValueError:
                        reason = ""
                    out[_norm_path(k.value)] = reason if isinstance(reason, str) else ""
            return out
    return None


def _workflow_text(repo_root: str):
    """Concatenated workflow files, or None when there is no workflow directory."""
    wf = os.path.join(repo_root, WORKFLOW_DIR)
    if not os.path.isdir(wf):
        return None
    chunks = []
    for name in sorted(os.listdir(wf)):
        if name.endswith((".yml", ".yaml")):
            try:
                chunks.append(_read(os.path.join(wf, name)))
            except OSError:
                pass
    return "\n".join(chunks)


def ci_status(test_path: str, exceptions, workflows) -> str:
    """enforced | exists-not-enforced | unknown for one mapped test file."""
    if exceptions is None:
        return "unknown"
    if test_path in exceptions:
        return "exists-not-enforced"
    if not test_path.endswith(".py"):
        if workflows is None:
            return "unknown"
        return "enforced" if test_path.rsplit("/", 1)[-1] in workflows else "exists-not-enforced"
    return "enforced"


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def census(repo_root: str, map_path: str | None = None):
    """Compute the whole census. Returns (result dict, fatal error or None)."""
    try:
        bullets = collect_bullets(repo_root)
    except OSError as exc:
        return None, "cannot read a rule surface: %s" % exc
    mapping, err = load_map(repo_root, map_path)
    if mapping is None:
        return None, err
    guards, err = load_guards(repo_root)
    if guards is None:
        return None, err

    by_id = {}
    for b in bullets:
        by_id.setdefault(b["id"], []).append(b)

    errors = []
    kinds = {}
    details = {}
    for mid, entry in sorted(mapping.items()):
        info = {}
        kind, why = classify(entry, repo_root, guards, info)
        if mid not in by_id:
            errors.append({"id": mid, "why": "stale: no bullet has this id any more "
                           "(reworded or removed); re-point or drop the mapping"})
            continue
        if why:
            errors.append({"id": mid, "why": why})
            continue
        # A "Pinned contract tokens" bullet is a token-presence assertion unless the map
        # says otherwise: an omitted strength there defaults to pin, everywhere else to
        # behavioural.
        default = PIN_STRENGTH if any(b["token_inventory"] for b in by_id[mid]) \
            else "behavioural"
        strength = entry.get("strength", default) if isinstance(entry, dict) else default
        if strength not in STRENGTHS:
            errors.append({"id": mid, "why": "unknown strength %r (use %s or omit it)"
                           % (strength, " or ".join(repr(s) for s in STRENGTHS[1:]))})
            continue
        kinds[mid] = kind
        info["strength"] = strength
        details[mid] = info

    exceptions = load_ci_exceptions(repo_root)
    workflows = _workflow_text(repo_root)

    counts = {"guard": 0, "test": 0, "hook": 0, "prose-only": 0}
    unmapped = []
    ci = {"enforced": 0, "exists-not-enforced": 0, "unknown": 0}
    behavioural = pins = behavioural_tests = 0
    for b in bullets:
        kind = kinds.get(b["id"])
        b["status"] = ("mapped" if kind in ("guard", "test", "hook")
                       else "prose-only" if kind == "prose-only" else "unmapped")
        if kind:
            counts[kind] += 1
        if b["status"] == "unmapped":
            unmapped.append(b)
        if b["status"] != "mapped":
            continue
        info = details[b["id"]]
        b["strength"] = info["strength"]
        if kind == "test":
            b["ci_status"] = ci_status(info["test_path"], exceptions, workflows)
            if b["ci_status"] == "exists-not-enforced" and exceptions \
                    and info["test_path"] in exceptions:
                b["ci_reason"] = exceptions[info["test_path"]][:90]
            ci[b["ci_status"]] += 1
        else:
            b["ci_status"] = "n/a"
        if info["strength"] == PIN_STRENGTH:
            pins += 1
        else:
            behavioural += 1
            if b["ci_status"] == "enforced":
                behavioural_tests += 1
    total = len(bullets)
    mapped = counts["guard"] + counts["test"] + counts["hook"]
    inventory = sum(1 for b in bullets if b["token_inventory"])
    inventory_behavioural = sum(1 for b in bullets if b["token_inventory"]
                                and b.get("strength") == "behavioural")
    rule_bullets = total - inventory + inventory_behavioural
    per_file = {}
    for b in bullets:
        row = per_file.setdefault(b["file"], {"bullets": 0, "mapped": 0})
        row["bullets"] += 1
        row["mapped"] += 1 if b["status"] == "mapped" else 0
    return {
        "schema": SCHEMA,
        "total": total,
        "mapped": mapped,
        "mapped_by_kind": {k: counts[k] for k in ("guard", "test", "hook")},
        "prose_only": counts["prose-only"],
        "unmapped": len(unmapped),
        "coverage_percent": _pct(mapped, total),
        "behavioural_mapped": behavioural,
        "pin_mapped": pins,
        "token_inventory": inventory,
        "rule_bullets": rule_bullets,
        "behavioural_percent": _pct(behavioural, rule_bullets),
        "behavioural_ci_enforced": behavioural_tests,
        "exists_not_enforced": ci["exists-not-enforced"],
        "ci_status_unknown": ci["unknown"],
        "duplicate_ids": sorted(i for i, v in by_id.items() if len(v) > 1),
        "per_file": per_file,
        "errors": errors,
        "bullets": bullets,
    }, None


def _print_report(res, show_unmapped: bool):
    print("rule-coverage census -- %d bullets across %d surfaces"
          % (res["total"], len(res["per_file"])))
    for path, row in sorted(res["per_file"].items()):
        print("  %-48s %3d bullets  %3d mapped" % (path, row["bullets"], row["mapped"]))
    print()
    print("  bullets            %d" % res["total"])
    print("  mapped to a check  %d  (guard %d, test %d, hook %d)"
          % ((res["mapped"],) + tuple(res["mapped_by_kind"][k]
                                      for k in ("guard", "test", "hook"))))
    print("  prose-only         %d  (never counted as coverage)" % res["prose_only"])
    print("  unmapped           %d" % res["unmapped"])
    print()
    print("  behavioural coverage  %.2f%%  (%d of %d rule bullets; %d token-inventory bullets "
          "left out of the count)" % (res["behavioural_percent"], res["behavioural_mapped"],
                                      res["rule_bullets"], res["token_inventory"]))
    print("    enforced in CI                %d  (collected tests only)"
          % res["behavioural_ci_enforced"])
    print("    exists, not enforced in CI    %d  (test present, hosted CI never runs it; "
          "counts as mapped, not as enforced)" % res["exists_not_enforced"])
    if res["ci_status_unknown"]:
        print("    CI status unknown             %d  (CI collection list unreadable)"
              % res["ci_status_unknown"])
    print("  token-presence pins   %d  (mapped, but only prove a token string is present; "
          "not in the behavioural figure)" % res["pin_mapped"])
    print("  all mapped incl. pins %.2f%%  (%d of %d)"
          % (res["coverage_percent"], res["mapped"], res["total"]))
    if res["duplicate_ids"]:
        print("  note: %d id(s) shared by identical bullets" % len(res["duplicate_ids"]))
    print()
    if show_unmapped and res["unmapped"]:
        print("UNMAPPED (%d):" % res["unmapped"])
        for b in res["bullets"]:
            if b["status"] == "unmapped":
                print("  %s:%d  %s  %s" % (b["file"], b["line"], b["id"][:12],
                                           b["text"][:70]))
        print()
    if res["errors"]:
        print("MAPPING ERRORS (%d) -- fix %s:" % (len(res["errors"]), MAP_PATH))
        for e in res["errors"]:
            print("  %s  %s" % (e["id"][:12], e["why"]))
    else:
        print("PASS -- every mapping resolves.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo-root", default=None)
    ap.add_argument("--map", default=None,
                    help="mapping file inside the repo root (default %s)" % MAP_PATH)
    ap.add_argument("--json", action="store_true", dest="as_json")
    ap.add_argument("--no-list", action="store_true",
                    help="omit the per-bullet unmapped list")
    args = ap.parse_args(argv)

    # Bullet text may hold characters a legacy console codepage cannot encode.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    repo_root = args.repo_root or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    repo_root = os.path.abspath(repo_root)

    res, err = census(repo_root, args.map)
    if err:
        sys.stderr.write("%s\n" % err)
        return 2
    if args.as_json:
        print(json.dumps(res, indent=2))
    else:
        _print_report(res, show_unmapped=not args.no_list)
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
