#!/usr/bin/env python3
"""SAFETY-LINT-MASTER-PARITY-DIFF-1: replay one deny corpus through fork/master's safety lints and the head's.

WHY THIS EXISTS (FWO-DEFECT-PREVENT-1 item 1).  Several PRs on 2026-10-16 changed a safety lint and passed their own pinned
rows, yet let through a command that fork/master's lint DENIED (HOOK-NA2-REDIRECT-FALSE-POSITIVE-1 r8/r9, NA2-RULE4-PER-LAUNCHER-
SCOPE-1 `--clip` variants).  Nothing compared the head against master over a whole corpus.  This runner does: every corpus row
is judged by master's lint and by the head's, and a row master DENIES while the head ALLOWS is a LOOSENED row.  A LOOSENED row
fails the run unless safety_lint_parity_allow.json lists its id with a reason, a PR number and the row's text hash.

THE TWO LINTS.
  hook      tools/hooks/mlv-never-authorized.py, the PreToolUse hook.  Master's copy is loaded by `git show` into a temp module
            (never imported from the live tree) and both modules are driven IN PROCESS through their own `decide(payload, dir)`
            (or `main()` for a raw-stdin row), with the same environment and the same tmp board.  Zero subprocesses.
  playback  tools/coordination/lane-local-playback-lint.ps1, the hub-VM no-local-playback lint.  Master's copy is written to a
            temp file by `git show`; ONE pwsh per lint judges every row (the head's test module's own `_json_counts`, the same
            probe LintDifferentialPinTests uses), so a full run spawns exactly two pwsh processes.  A row is DENIED when the lint
            reports one or more findings.

THE CORPUS has two sources, both reported per row:
  * safety_lint_parity_corpus.json, tracked: rows (and groups / generators that expand to rows) with an id, a lint and a `why`
    naming the source.  This is where rows live that no test file on the head carries (the #455 and #465 branches' rows).
  * rows DERIVED at run time by importing the head's own test modules (`CASES` of test_mlv_never_authorized.py with
    expect == DENY; the must-refuse tuples of test_hub_vm_no_local_playback.py), so a deny row added to either suite joins the
    comparison with no edit here, UNIONED BY ID with the same rows read from the baseline's copy of those two modules (r5; since r6 BY (ID,
    CONTENT), since r8 a hook twin whose BUILT stimulus differs between the two builders also keeps its baseline row: see the r8 paragraph).
    A derived row that cannot be built is an ERROR, never a skip.

EXIT CODES.  0 = no unlisted LOOSENED row and no error.  1 = at least one unlisted LOOSENED row (reported even when an error
also occurred, because it is the definite finding).  2 = could not decide: a git failure, a lint that threw, an unparseable or
unrunnable row, a malformed corpus or allow-list.  A row that cannot be run is never a pass.

DERIVED ROWS ARE THE UNION OF THE HEAD'S AND THE BASELINE'S TEST MODULES (r5, CoS B1 on 778974a4; doctrine Recurrence 6: a master-
parity gate takes its expectations from the baseline, never only from the head under test).  Through r4 only the head's own test
modules were imported, so a PR that loosened a lint and then flipped the matching test row to `expect: ALLOW` (or deleted it) took
that row out of the comparison, and the exact row-count pins stayed green when one row went out and another came in.  Now the
baseline's copy of each test module (`git show <base>:<path>`, executed by load_source_as with `__file__` at the head test file's
path, like the r3 root fix) is read too, and its rows are unioned with the head's (r5: BY ID; r6: BY ID AND CONTENT, next paragraph).
A row only the baseline has is judged like any other: master verdict against head verdict, so a deleted or flipped master-denied
row that the head lint now allows is LOOSENED.  A baseline test module that cannot be read or executed is an ERROR (exit 2), never
a silent skip.  The exact derived pins in test_safety_lint_parity.py count the HEAD's rows only and still catch an in-place
deletion on their own; the union covers the swap they cannot see.

THE UNION IS BY (ID, CONTENT), AND IT COVERS THE STATIC CORPUS TOO (r6, CoS B2 on 9931280c).  r5 keyed the union by id alone, and a
hook row's id is "hook:" + slug(case name), so a head that kept a row's name and expectation but changed its INPUT (or its fixture,
or its raw stdin) still took the master-denied command out of the comparison, with every count pin green.  The static corpus had the
same hole: it was read from the head only.  Now (a) a derived hook row's content is its tool, input, fixture and raw, hashed with
short_hash; (b) a baseline row whose id the head also has, with a DIFFERENT content hash, joins as `<id>@base-<hash>` and is judged
like any other row (master verdict against head verdict); a baseline row whose (id, content) the head already has is not duplicated,
so with base == head nothing is added; (c) `collect` reads the baseline's safety_lint_parity_corpus.json with
`git show <base>:tools/repo_hygiene/safety_lint_parity_corpus.json`, parses it like the head's, and unions its static rows (a static
row's content is its lint, text, tool name, cwd and board root) the same way.  A baseline corpus that exists but cannot be shown or
parsed is an ERROR (exit 2); a corpus path that does not exist at the base at all (the card that introduces the file) is reported
as a `note:` line and unions nothing.  `--no-derived` still means the head's JSON corpus alone.  Playback derived ids already carry
the sentence's hash, so a reworded sentence was always a different row and needed no change.

THE BUILT STIMULUS, AND THE DERIVED PLAYBACK IDS (r8, acting-cos Dot B3 and B4 on c722b29c).  B3: the (id, content) key of a derived hook row is
the CASES fields, so a head that kept a CASES entry and changed the BUILDER (`_substitute`, `_render_content`, a fixture, the enable-literal
binder) fed BOTH lints the head-built command while the baseline-built command, the one master denies, never reached the head's lint.  For
every (id, content) twin the two builders' stimuli (stdin text, environment, venue; the scratch dir's random name masked) are now compared,
and a baseline row whose stimulus differs joins as `<id>@base-<hash>` like any other base row.  A builder that raises counts as different.
When the head's hook test module is the baseline's text (modulo line endings; it imports only the standard library) nothing is built twice.
B4: a derived playback id carries the hash of its sentence, and a static corpus row that holds such an id with DIFFERENT text is REFUSED
(exit 2, the id named); r5-r7 resolved the clash silently in favour of the static row, so a harmless row under a derived id deleted the
original sentence from the comparison.  The same id and the same text is one sentence, compared once.

ALLOW-LIST ENTRIES ARE BOUND TO THE ROW'S TEXT (r5, CoS I2).  An entry carries the row id AND `hash`, the short_hash of the row's text
(a hook row's command, a playback row's sentence; r8: for a static hook row the JSON of [hook, command, tool, cwd, board root]); the LOOSENED line prints it.  A row whose id matches but whose text hash does not
is treated as unlisted, so editing a row's text under an old id cannot inherit an old excuse.

KNOWN LIMITS.
  * Payload rows reach the hook through `decide()` directly (a raw-stdin row goes through `main()`), so a head change to `main()` or
    `parse_argv`, the fail-closed paths around `decide()`, is NOT compared by this runner.  A `main()` pass is a follow-up card (CoS I3).
  * Only the corpus (the head's and, since r6, the baseline's), the head's rows and the baseline's rows are judged.  A master-denied
    command that appears in none of them is invisible to the run, which is what safety_lint_parity_corpus.json and the defeater-
    corpus card are for.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
HOOK_REL = "tools/hooks/mlv-never-authorized.py"
LINT_REL = "tools/coordination/lane-local-playback-lint.ps1"
HOOK_TEST_REL = "tools/repo_hygiene/test_mlv_never_authorized.py"
PLAYBACK_TEST_REL = "tools/repo_hygiene/test_hub_vm_no_local_playback.py"
CORPUS_REL = "tools/repo_hygiene/safety_lint_parity_corpus.json"  # r6: where the BASELINE's copy of the static corpus is read from
DEFAULT_CORPUS = HERE / "safety_lint_parity_corpus.json"
DEFAULT_ALLOW = HERE / "safety_lint_parity_allow.json"
CORPUS_SCHEMA = "safety-lint-parity-corpus.v1"

EXIT_OK, EXIT_LOOSENED, EXIT_UNDECIDED = 0, 1, 2
LINTS = ("hook", "playback")
DENY, ALLOW = "DENY", "ALLOW"


class ParityError(Exception):
    """The comparison could not be made (exit 2). Never swallowed into a pass."""


# ------------------------------------------------------------------------------------------------ git / module loading

def git(root: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ParityError(f"git {' '.join(args)} could not run: {exc}") from exc
    if done.returncode != 0 or not done.stdout:
        raise ParityError(f"git {' '.join(args)} failed rc={done.returncode}: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return done.stdout.decode("utf-8")


def load_module(path: Path, name: str):
    """Load a module by file path under a private name; it is never imported by its package name."""
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ParityError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - reported, then the run is undecided
        raise ParityError(f"{path.name} failed to import: {type(exc).__name__}: {exc}") from exc
    return module


def load_source_as(source: str, name: str, as_file: Path):
    """Exec `source` as module `name` whose `__file__` is `as_file`.

    ROOT FIX (r3).  The hook derives its `worktree_root` from `os.path.abspath(__file__)`, three directories up (Ctx.__init__).
    Master's copy, written to a temp path, would resolve a different worktree_root than the head's, and every NA-7 / NA-10 row
    that compares against the worktree would flip: base == head then reports phantom "tightened" rows.  Executing master's bytes
    with `__file__` set to the HEAD hook's own path gives both copies the identical worktree_root, so only the code differs."""
    module = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(name, None))
    module.__file__ = str(as_file)
    sys.modules[name] = module
    try:
        exec(compile(source, str(as_file), "exec"), module.__dict__)  # noqa: S102 - master's own committed bytes
    except Exception as exc:  # noqa: BLE001 - reported, then the run is undecided
        raise ParityError(f"{name} failed to load: {type(exc).__name__}: {exc}") from exc
    return module


def short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


def slug(text: str) -> str:
    return re.sub(r"[^0-9a-z]+", "-", text.lower()).strip("-")


# ------------------------------------------------------------------------------------------------ corpus + allow-list

def _expand(text: str, macros: dict) -> str:
    for key in sorted(macros, key=len, reverse=True):
        text = text.replace(key, macros[key])
    return text


def load_corpus(path: Path) -> list[dict]:
    """The static rows of the JSON corpus file at `path`, groups and generators expanded. Every row: id, lint, why, and command/text."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:  # ValueError: UnicodeDecodeError on a file that is not UTF-8, as before the r6 split
        raise ParityError(f"corpus {path} is unreadable: {exc}") from exc
    return parse_corpus(text, str(path))


def parse_corpus(text: str, label: str) -> list[dict]:
    """The static rows of one corpus document (r6: factored out of load_corpus so the BASELINE's copy, read with `git show`, is parsed
    by exactly the same code as the head's). `label` names the source in every error."""
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise ParityError(f"corpus {label} is unreadable: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != CORPUS_SCHEMA:
        raise ParityError(f"corpus {label} does not declare schema {CORPUS_SCHEMA}")
    macros = doc.get("macros", {})
    rows: list[dict] = []

    def add(row: dict, where: str) -> None:
        for key in ("id", "lint", "why"):
            if not isinstance(row.get(key), str) or not row[key].strip():
                raise ParityError(f"corpus {where}: row without a usable {key!r}: {str(row)[:160]}")
        if row["lint"] not in LINTS:
            raise ParityError(f"corpus {where}: row {row['id']} names unknown lint {row['lint']!r}")
        field = "command" if row["lint"] == "hook" else "text"
        if not isinstance(row.get(field), str) or not row[field]:
            raise ParityError(f"corpus {where}: row {row['id']} has no {field!r}")
        row["source"] = "corpus"
        rows.append(row)

    for row in doc.get("rows", []):
        row = dict(row)
        m = macros.get(row.get("lint"), {})
        for field in ("command", "text"):
            if field in row:
                row[field] = _expand(row[field], m)
        add(row, "rows")
    for group in doc.get("groups", []):
        field = "commands" if group.get("lint") == "hook" else "texts"
        m = macros.get(group.get("lint"), {})
        items = group.get(field)
        if not isinstance(items, list) or not items:
            raise ParityError(f"corpus group {group.get('id')}: no {field!r}")
        for n, item in enumerate(items, 1):
            row = {k: v for k, v in group.items() if k not in ("commands", "texts")}
            row["id"] = f"{group['id']}-{n:02d}"
            row["command" if group["lint"] == "hook" else "text"] = _expand(item, m)
            add(row, f"group {group['id']}")
    for gen in doc.get("generators", []):
        if gen.get("lint") != "hook":
            raise ParityError(f"corpus generator {gen.get('id')}: only hook generators are defined")
        m = macros.get("hook", {})
        target = _expand(gen["target"], m)
        seen: list[str] = []
        for verb in gen["verbs"]:
            for tail in gen["tails"]:
                inner = verb + "=" + tail.replace("{t}", target)
                for wrapper in gen["wrappers"]:
                    cmd = wrapper.replace("{x}", inner)
                    if cmd not in seen:
                        seen.append(cmd)
        for n, cmd in enumerate(seen, 1):
            row = {k: v for k, v in gen.items() if k not in ("verbs", "tails", "wrappers", "target")}
            row["id"] = f"{gen['id']}-{n:03d}"
            row["command"] = cmd
            add(row, f"generator {gen['id']}")
    ids = [r["id"] for r in rows]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ParityError(f"corpus ids are not unique: {dupes[:5]}")
    return rows


def git_path_exists(root: Path, ref: str, rel: str) -> bool:
    """True when `rel` is in the tree of `ref`. A git failure raises ParityError (it is never read as 'absent')."""
    try:
        # DG-GIT-PATHLIST (r7): quotepath off, so a non-ASCII path is never read back in its quoted form (only emptiness is tested here).
        done = subprocess.run(["git", "-C", str(root), "-c", "core.quotepath=false", "ls-tree", "--name-only", ref, "--", rel], capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ParityError(f"git ls-tree {ref} -- {rel} could not run: {exc}") from exc
    if done.returncode != 0:
        raise ParityError(f"git ls-tree {ref} -- {rel} failed rc={done.returncode}: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return bool(done.stdout.strip())


def static_content(row: dict) -> str:
    """Everything about a static corpus row that decides its verdict: the lint, the text, and (hook rows) the tool name, cwd and board root.
    The id, the `why` and the source are labels and are NOT part of it."""
    field = "command" if row["lint"] == "hook" else "text"
    return json.dumps([row["lint"], row.get(field), row.get("tool_name"), row.get("cwd"), row.get("board_root")], sort_keys=True, default=str)


def baseline_corpus(root: Path, base_ref: str) -> list[dict] | None:
    """The BASELINE's static corpus rows, read with `git show <base>:<corpus path>` and parsed like the head's (r6, CoS B2), or None when
    the corpus file does not exist at the base at all (the card that introduces it: nothing was there to protect, and the caller says so).
    A corpus that exists at the base but cannot be shown or parsed raises ParityError (exit 2), never a silent skip."""
    if not git_path_exists(root, base_ref, CORPUS_REL):
        return None
    try:
        return parse_corpus(git(root, "show", f"{base_ref}:{CORPUS_REL}"), f"{base_ref}:{CORPUS_REL}")
    except ParityError as exc:
        raise ParityError(f"baseline corpus could not be read: {exc}") from exc


def union_static(head_rows: list[dict], base_rows: list[dict]) -> list[dict]:
    """Head rows, plus every baseline row the head lacks by (id, content). A baseline row whose id the head has with DIFFERENT content
    joins as `<id>@base-<hash of its content>` so its master-denied text is still judged against the head's lint (r6, CoS B2); a
    baseline row whose id the head lacks joins under its own id; identical (id, content) rows are not duplicated."""
    have = {(r["id"], short_hash(static_content(r))) for r in head_rows}
    head_ids = {r["id"] for r in head_rows}
    out = list(head_rows)
    for row in base_rows:
        digest = short_hash(static_content(row))
        if (row["id"], digest) in have:
            continue
        joined = dict(row, source="corpus-base", why=f"{row['why']} (baseline copy of the corpus row)")
        if row["id"] in head_ids:
            joined["id"] = f"{row['id']}@base-{digest}"
        have.add((joined["id"], digest))
        out.append(joined)
    return out


def load_allow(path: Path) -> dict[str, dict]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ParityError(f"allow-list {path} is unreadable: {exc}") from exc
    if not isinstance(entries, list):
        raise ParityError(f"allow-list {path} must be a JSON list of {{id, reason, pr, hash}}")
    out: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("id", "")).strip() or not str(entry.get("reason", "")).strip() or entry.get("pr") in (None, "", 0):
            raise ParityError(f"allow-list entry needs an id, a reason and a pr number: {str(entry)[:160]}")
        # r5 (CoS I2): the entry is bound to the row's text hash, so an edited row under an old id is not excused by it
        if not isinstance(entry.get("hash"), str) or not re.fullmatch(r"[0-9a-f]{10}", entry["hash"]):
            raise ParityError(f"allow-list entry {entry['id']} needs a `hash` (the 10-hex short_hash of the row's text, printed on its LOOSENED line): {str(entry)[:160]}")
        if entry["id"] in out:
            raise ParityError(f"allow-list lists {entry['id']} twice")
        out[entry["id"]] = entry
    return out


# ------------------------------------------------------------------------------------------------ hook rows

class HookCase:
    """One hook row: how to build (stdin text, environment, venue) on demand, and a cleanup for its tmp board."""

    def __init__(self, row_id: str, why: str, source: str, build, tmp_names: list | None = None):
        self.id, self.why, self.source, self.build = row_id, why, source, build
        self.display = ""
        self.text = ""  # the row's full, run-stable text: what an allow-list entry's `hash` is taken over
        # r8 (B3): `build` appends the basename of the scratch dir it made, so `stimulus` can mask the one part that differs between two builds
        self.tmp_names = tmp_names if tmp_names is not None else []

    def stimulus(self) -> str:
        """The BUILT stimulus of this row (stdin text, environment, venue), canonicalised: the scratch dir's random name is masked.

        r8 (B3, acting-cos Dot on c722b29c).  `text` is the CASES fields only, so two builders that read the same entry and produce
        different commands (a changed `_substitute`, `_render_content`, fixture or enable-literal binder) share one content hash.  The
        union compares this string for such twins; it is deterministic across two builds of one module once the tmp name is masked."""
        del self.tmp_names[:]
        stdin_text, env, venue, cleanup = self.build()
        try:
            blob = repr((stdin_text, sorted(env.items()), venue))
            names = list(self.tmp_names)
        finally:
            with contextlib.suppress(Exception):
                cleanup()
        for name in names:
            blob = re.sub(re.escape(name), "<TMP>", blob, flags=re.IGNORECASE)
        return blob


def _hook_env(paths: dict, overrides: dict, unset, venue_key: str) -> tuple[dict, str | None]:
    """The environment `_invoke` of the hook suite builds: MLV_* stripped, the three roots pinned to the tmp board, overrides applied."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("MLV_")}
    env.pop("CLAUDE_PROJECT_DIR", None)
    env["MLV_BOARD_ROOT"] = paths["BOARD"]
    env["MLV_CLIP_CACHE_ROOT"] = paths["CACHE"]
    env["MLV_REQUIRED_CHECKS_SNAPSHOT"] = paths["SNAPSHOT"]
    overrides = dict(overrides)
    venue = overrides.pop(venue_key, None)
    for key in [k for k, v in overrides.items() if v is unset]:
        overrides.pop(key)
        env.pop(key, None)
    env.update(overrides)
    env["PYTHONIOENCODING"] = "utf-8"
    return env, venue


def _hook_cases_from(tm, source: str, which: str) -> list[HookCase]:
    """Every DENY row of one test module's CASES table (`which` names it: head or baseline), built exactly as the suite builds it,
    minus the subprocess.  `source` tags the row ("derived" / "derived-base")."""
    cases = []
    used: set[str] = set()
    for case in tm.CASES:
        if case.get("expect") != "DENY":
            continue
        row_id = "hook:" + slug(case["name"])
        while row_id in used:
            row_id += "-x"
        used.add(row_id)
        tmp_names: list[str] = []  # r8 (B3): see HookCase.stimulus

        def build(case=case, tm=tm, tmp_names=tmp_names):
            probe = tm.MlvNeverAuthorizedHookTests("test_hook_script_is_present_on_this_ref")
            probe.setUp()
            tmp_names.append(os.path.basename(probe.tmp))
            overrides = tm.FIXTURES[case.get("fixture", "default")](probe.paths)
            probe._bind_the_enable_literal()
            if "raw" in case:
                stdin_text = case["raw"]
            else:
                stdin_text = json.dumps({"tool_name": case["tool"],
                                         "tool_input": probe._render_content(probe._substitute(case["input"]))})
            env, venue = _hook_env(probe.paths, overrides, tm.UNSET, tm.VENUE_KEY)
            return stdin_text, env, venue, probe.doCleanups

        hc = HookCase(row_id, f"{HOOK_TEST_REL} CASES[{case['name']!r}] (group {case.get('group')}, {case.get('na', '?')}; {which} copy)",
                      source, build, tmp_names)
        hc.display = str(case.get("input", case.get("raw", "")))[:200]
        # r6 (CoS B2): the hashed text is EVERYTHING in the case that decides the verdict: tool, input, fixture (the environment the row
        # runs under) and raw (the stdin text of a raw row).  A fixture swap changes the verdict without changing the input.
        hc.text = json.dumps([case.get("tool"), case.get("input", case.get("raw", "")), case.get("fixture", "default"), case.get("raw")],
                             sort_keys=True, default=str)
        cases.append(hc)
    return cases


def union_hook_cases(head: list[HookCase], base: list[HookCase], same_source: bool = False) -> list[HookCase]:
    """Head cases, plus every baseline case the head lacks by (id, content) (r6, CoS B2: r5 unioned by id alone, and a hook row's id is its
    case NAME, so editing a row's input in place under the same name dropped the baseline's command from the comparison with the
    count unchanged).  A baseline case whose id the head has with a different content hash joins as `<id>@base-<hash>`; one whose id
    the head lacks joins under its own id; an identical (id, content) pair is not duplicated."""
    have = {(c.id, short_hash(c.text)) for c in head}
    head_ids = {c.id for c in head}
    twins = {(c.id, short_hash(c.text)): c for c in head}  # r8 (B3): the head's row for each (id, content) pair, to compare built stimuli
    out = list(head)
    for case in base:
        digest = short_hash(case.text)
        if (case.id, digest) in have:
            # r8 (B3): the same (id, content) is not the same ROW when the head's builder turns it into a different command.  Keep the
            # baseline row, built by the BASELINE's builder, as `<id>@base-<hash>` unless both builders produce the same stimulus.  A
            # builder that raises on either side counts as different (the row then errors visibly instead of being dropped).
            twin = twins.get((case.id, digest))
            if twin is not None and not same_source:
                try:
                    same = twin.stimulus() == case.stimulus()
                except Exception:  # noqa: BLE001 - an unbuildable row is kept, never dropped
                    same = False
                if same:
                    continue
            else:
                continue
        if case.id in head_ids:
            case.id = f"{case.id}@base-{digest}"
        have.add((case.id, digest))
        out.append(case)
    return out


def derived_hook_cases(root: Path, base_ref: str | None = None) -> list[HookCase]:
    """The head's DENY rows, plus (r5, CoS B1) every DENY row only the BASELINE's copy of the suite carries, unioned by (id, content) since r6
    and, since r8, also every (id, content) twin whose baseline-built stimulus differs from the head-built one (B3).

    `base_ref` is a git ref or sha.  With it, the baseline's test module is read with `git show` and executed by load_source_as at the
    head test file's own path (so the REPO_ROOT it derives from `__file__` is the head's, as with the r3 root fix); a base-only row
    is a deny row the head deleted or flipped to ALLOW, and the caller judges it like any other.  A baseline module that cannot be
    shown or executed raises ParityError (exit 2).  Without `base_ref` only the head's rows are returned (the exact pins count those)."""
    tm = load_module(root / HOOK_TEST_REL, "parity_head_hook_tests")
    cases = _hook_cases_from(tm, "derived", "head")
    if base_ref:
        base_source = git(root, "show", f"{base_ref}:{HOOK_TEST_REL}")
        base_tm = load_source_as(base_source, "parity_base_hook_tests", root / HOOK_TEST_REL)
        # r8 (B3): the hook suite imports only the standard library (and the hook, shared by both sides), so when the head module's file is
        # the baseline's text (modulo line endings) every builder is identical and the per-row stimulus comparison is skipped for speed.
        # The file LOADED is `__spec__.origin`, not `__file__`: a wrapper module may reassign `__file__` (the scratch heads of the tests do).
        head_file = getattr(getattr(tm, "__spec__", None), "origin", None)
        try:
            same_source = bool(head_file) and Path(head_file).read_text(encoding="utf-8").replace("\r\n", "\n") == base_source.replace("\r\n", "\n")
        except (OSError, ValueError):
            same_source = False
        cases = union_hook_cases(cases, _hook_cases_from(base_tm, "derived-base", "baseline"), same_source)  # r6: by (id, content)
    return cases


def corpus_hook_cases(rows: list[dict]) -> list[HookCase]:
    out = []
    for row in rows:
        if row["lint"] != "hook":
            continue
        tools = row.get("tool_name", "PowerShell")
        for tool in ([tools] if isinstance(tools, str) else list(tools)):
            row_id = row["id"] if isinstance(tools, str) else f"{row['id']}:{tool}"

            def build(row=row, tool=tool):
                tmp = tempfile.mkdtemp(prefix="parity-board-")
                board = row.get("board_root") or os.path.join(tmp, "board")
                paths = {"BOARD": board, "CACHE": os.path.join(tmp, "cache"),
                         "SNAPSHOT": os.path.join(board, ".claude-state", "coordination", "dual-lane", "receipts", "required-checks-live.jsonl")}
                env, _ = _hook_env(paths, {}, object(), "")
                stdin_text = json.dumps({"tool_name": tool, "tool_input": {"command": row["command"]}})
                return stdin_text, env, row.get("cwd"), lambda: shutil.rmtree(tmp, True)

            hc = HookCase(row_id, row["why"], row.get("source", "corpus"), build)  # r6: "corpus-base" for a row only the baseline's corpus has
            hc.display = row["command"][:200]
            # r8 (nonblocking, acting-cos Dot): the hash binds the semantic context too (tool, cwd, board root), not the command alone, so an
            # allow-list entry for a row cannot excuse the same command run under a different tool, venue or board.
            hc.text = json.dumps(["hook", row["command"], tool, row.get("cwd"), row.get("board_root")], sort_keys=True, default=str)
            out.append(hc)
    return out


@contextlib.contextmanager
def _process_state(env: dict, argv: list[str], stdin_text: str):
    saved_env, saved_argv = dict(os.environ), sys.argv
    saved_streams = (sys.stdin, sys.stdout, sys.stderr)
    os.environ.clear()
    os.environ.update(env)
    sys.argv = argv
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin_text), io.StringIO(), io.StringIO()
    try:
        yield sys.stderr
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved_streams
        sys.argv = saved_argv
        os.environ.clear()
        os.environ.update(saved_env)


def hook_verdict(module, stdin_text: str, env: dict, venue: str | None) -> tuple[str, str]:
    """(DENY|ALLOW, reason) from one hook module. A payload row goes through `decide` so a rule that throws surfaces as an error
    here instead of being turned into a fail-closed deny by `main`; a raw-stdin row can only go through `main`."""
    try:
        payload = json.loads(stdin_text)
    except ValueError:
        payload = None
    if payload is None or not isinstance(payload, dict):
        argv = [getattr(module, "__file__", "hook")] + (["--project-dir", venue] if venue is not None else [])
        with _process_state(env, argv, stdin_text) as err:
            code = module.main()
            reason = err.getvalue().strip()
    else:
        with _process_state(env, [getattr(module, "__file__", "hook")], ""):
            code, reason = module.decide(payload, venue)
    if code == 2:
        return DENY, str(reason).strip()
    if code == 0:
        return ALLOW, str(reason).strip()
    raise ParityError(f"hook returned exit code {code!r}")


def run_hook(base_module, head_module, cases: list[HookCase]) -> list[dict]:
    results = []
    for case in cases:
        res = {"id": case.id, "lint": "hook", "why": case.why, "source": case.source, "display": case.display,
               "text_hash": short_hash(case.text)}
        cleanup = None
        try:
            stdin_text, env, venue, cleanup = case.build()
            res["master"], res["master_reason"] = hook_verdict(base_module, stdin_text, env, venue)
            res["head"], res["head_reason"] = hook_verdict(head_module, stdin_text, env, venue)
        except Exception as exc:  # noqa: BLE001 - a lint that throws is a row that cannot be decided
            res["error"] = f"{type(exc).__name__}: {exc}"[:400]
        finally:
            if cleanup is not None:
                with contextlib.suppress(Exception):
                    cleanup()
        results.append(res)
    return results


# ------------------------------------------------------------------------------------------------ playback rows

def _playback_rows_from(pb, source: str, which: str) -> list[dict]:
    """Every must-refuse sentence of one playback test module (`which` names it: head or baseline); `source` tags the rows."""
    rows: dict[str, dict] = {}

    def add(name: str, text: str, where: str) -> None:
        rows.setdefault(f"playback:{name}:{short_hash(text)}", {"text": text, "why": f"{PLAYBACK_TEST_REL} {where} ({which} copy)"})

    lints = pb.PromptLintTests
    for name in sorted(dir(lints)):
        if name.startswith("CLAUSE_MUST_REFUSE"):
            for text in getattr(lints, name):
                add(name, text, f"PromptLintTests.{name}")
    for shape, kind, text in getattr(lints, "DEFEATER_AXIS", ()):
        add("DEFEATER_AXIS", text, f"PromptLintTests.DEFEATER_AXIS ({shape}/{kind})")
    for name in sorted(vars(pb)):
        value = getattr(pb, name)
        if re.fullmatch(r"[A-Z0-9_]+_(REFUSE|GUARDS|RED)", name) and isinstance(value, tuple) and value and all(isinstance(t, str) for t in value):
            for text in value:
                add(name, text, f"module-level {name}")
    add("INCIDENT_LINE", "## VENUE\n" + pb.INCIDENT_LINE + "\n", "INCIDENT_LINE (the 2026-10-11 producer-prompt incident)")
    return [{"id": k, "lint": "playback", "source": source, **v} for k, v in rows.items()]


def derived_playback_rows(root: Path, base_ref: str | None = None) -> tuple[list[dict], object]:
    """The head's must-refuse sentences, plus (r5, CoS B1) every sentence only the BASELINE's copy of the suite refuses, unioned by id
    (the id carries a hash of the text, so a reworded sentence is a different row).  Also returns the HEAD's module, whose
    `_write_probe` / `_psq` / `PWSH` helpers drive the probe.  A baseline module that cannot be shown or executed raises ParityError."""
    pb = load_module(root / PLAYBACK_TEST_REL, "parity_head_playback_tests")
    rows = _playback_rows_from(pb, "derived", "head")
    if base_ref:
        base_pb = load_source_as(git(root, "show", f"{base_ref}:{PLAYBACK_TEST_REL}"), "parity_base_playback_tests", root / PLAYBACK_TEST_REL)
        have = {r["id"] for r in rows}
        rows += [r for r in _playback_rows_from(base_pb, "derived-base", "baseline") if r["id"] not in have]
    return rows, pb


def parse_probe_output(returncode: int, out: str, err: str, expected: int) -> list[int]:
    """The finding counts of one playback probe, or ParityError.  r4 (sol r1 BLOCKER, PARITY-ERROR-01).

    The adopted collector (test_hub_vm_no_local_playback._collect_counts) accepted any right-length list when pwsh exited 0.  An
    unreadable baseline dot-source plus a missing finding function wrote to stderr, exited 0 and printed [0], which read as an ALLOW
    verdict (master=ALLOW against head=DENY is `tightened`, exit 0).  Here ANY stderr text is an evaluation error, whatever the exit
    code, and so is a count that is not a non-negative integer.  The probe sets $ErrorActionPreference = 'Stop', so a non-terminating
    error also arrives as a non-zero exit with a PARITY-PROBE-ERROR line."""
    if returncode != 0:
        raise ParityError(f"playback probe exited {returncode}: {(err or out).strip()[:300]}")
    if err.strip():
        raise ParityError(f"playback probe wrote to stderr (an evaluation error, even with exit 0): {err.strip()[:300]}")
    try:
        counts = json.loads(out.strip())
    except ValueError as exc:
        raise ParityError(f"playback probe printed no JSON count list: {out.strip()[:200]!r}") from exc
    if not isinstance(counts, list) or any(isinstance(c, bool) or not isinstance(c, int) or c < 0 for c in counts):
        raise ParityError(f"playback probe printed something other than a list of non-negative counts: {out.strip()[:200]!r}")
    if len(counts) != expected:
        raise ParityError(f"playback probe returned {len(counts)} verdicts for {expected} sentences")
    return counts


def playback_probe_counts(pb, lint: Path, texts: list[str], work: Path, tag: str) -> list[int]:
    """One pwsh judges every text under `lint`; same shape as the suite's `_json_counts`, but every evaluation error is fatal.

    Dot-sourcing and the whole loop sit inside try with $ErrorActionPreference = 'Stop', so an unreadable lint, a missing
    Get-LocalPlaybackPromptFindings or a non-terminating error inside it ends the probe (exit 3, PARITY-PROBE-ERROR on stderr)."""
    src = work / f"{tag}-in.json"
    src.write_text(json.dumps(texts), encoding="utf-8")
    probe = work / f"{tag}-probe.ps1"
    pb._write_probe(
        probe,
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n"
        ". " + pb._psq(lint) + "\n"
        "$texts = @(ConvertFrom-Json -InputObject ([System.IO.File]::ReadAllText(" + pb._psq(src) + ")))\n"
        "$out = New-Object System.Collections.Generic.List[int]\n"
        "foreach ($t in $texts) { $out.Add(@(Get-LocalPlaybackPromptFindings -Text ([string]$t)).Count) }\n"
        "ConvertTo-Json -InputObject @($out.ToArray()) -Compress\n"
        "} catch {\n"
        "[Console]::Error.WriteLine('PARITY-PROBE-ERROR: ' + $_.Exception.Message)\n"
        "exit 3\n"
        "}\n",
    )
    proc = subprocess.Popen([pb.PWSH, "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(probe)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        out, err = proc.communicate(timeout=1500)
    except subprocess.TimeoutExpired:
        pb._kill(proc)
        raise
    finally:
        pb._kill(proc)
    return parse_probe_output(proc.returncode, out, err, len(texts))


def run_playback(pb, base_lint: Path, head_lint: Path, rows: list[dict], work: Path, stats: dict) -> list[dict]:
    texts = [r["text"] for r in rows]
    counts: dict[str, list[int]] = {}
    failure = None
    for tag, lint in (("master", base_lint), ("head", head_lint)):
        stats["pwsh_spawns"] += 1
        try:
            # r4: was pb._json_counts(...), whose collector read a stderr-writing exit-0 probe as an ALLOW verdict (PARITY-ERROR-01)
            counts[tag] = playback_probe_counts(pb, lint, texts, work, f"parity-{tag}")
        except Exception as exc:  # noqa: BLE001 - one failed probe leaves every playback row undecided
            failure = f"{tag} lint probe failed: {type(exc).__name__}: {str(exc)[:300]}"
            break
    results = []
    for n, row in enumerate(rows):
        res = {"id": row["id"], "lint": "playback", "why": row["why"], "source": row["source"], "display": row["text"][:200],
               "text_hash": short_hash(row["text"])}
        if failure:
            res["error"] = failure
        else:
            res["master"] = DENY if counts["master"][n] > 0 else ALLOW
            res["head"] = DENY if counts["head"][n] > 0 else ALLOW
        results.append(res)
    return results


# ------------------------------------------------------------------------------------------------ the run

def classify(res: dict, allow: dict) -> str:
    if "error" in res:
        return "error"
    if res["master"] == res["head"]:
        return "same"
    if res["master"] == ALLOW:
        return "tightened"
    return "LOOSENED"


def collect(lints, corpus_path: Path, derived: bool, root: Path = ROOT, base_ref: str | None = None, notes: list[str] | None = None):
    """(hook cases, playback rows, playback module or None). Pure collection: nothing is judged here.
    `base_ref` (r5): the baseline whose test modules are unioned into the derived rows; ignored when `derived` is off.
    r6 (CoS B2): with `base_ref` the baseline's static corpus (`git show <base>:<corpus path>`) is unioned with the head's by (id, content)
    too, so a corpus row edited in place under its id still brings its baseline text into the comparison.  `--no-derived` means the
    head's JSON corpus alone: nothing is read from the baseline.  When the corpus file does not exist at the base at all (the card that
    introduces it), nothing is unioned and a line is appended to `notes`; a corpus that exists but cannot be read or parsed is an error."""
    base_ref = base_ref if derived else None
    static = load_corpus(corpus_path)
    if base_ref:
        base_static = baseline_corpus(root, base_ref)
        if base_static is None:
            if notes is not None:
                notes.append(f"no baseline corpus: {CORPUS_REL} does not exist at {base_ref[:8]}, so there is no baseline corpus row to union")
        else:
            static = union_static(static, base_static)
    hook_cases = corpus_hook_cases(static) if "hook" in lints else []
    playback = [r for r in static if r["lint"] == "playback"] if "playback" in lints else []
    pb = None
    if "hook" in lints and derived:
        hook_cases += derived_hook_cases(root, base_ref)
    if "playback" in lints and (derived or playback):
        extra, pb = derived_playback_rows(root, base_ref)
        if derived:
            # r8 (B4, acting-cos Dot on c722b29c): this was `r["id"] not in <static ids>`, so a static row reusing a derived sentence's id
            # with harmless text removed the original (or baseline) sentence from the comparison.  A derived id carries the hash of its
            # sentence, so a static row that holds the id with DIFFERENT text is either a mistake or a swap: REFUSED (exit 2), never
            # resolved in either direction.  The same id AND text is one sentence and is compared once, as the static row.
            static_text = {p["id"]: p["text"] for p in playback}
            for r in extra:
                if r["id"] in static_text and static_text[r["id"]] != r["text"]:
                    raise ParityError(f"static corpus row {r['id']} reuses the id of a derived playback sentence ({r['source']}) with different text "
                                      f"(static {short_hash(static_text[r['id']])} vs derived {short_hash(r['text'])}): rename the static row")
            playback = playback + [r for r in extra if r["id"] not in static_text]
    ids = [c.id for c in hook_cases] + [r["id"] for r in playback]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise ParityError(f"row ids are not unique across sources: {dupes[:5]}")
    return hook_cases, playback, pb


def run(base: str, lints, corpus_path: Path = DEFAULT_CORPUS, allow_path: Path = DEFAULT_ALLOW, head_hook: Path | None = None,
        head_lint: Path | None = None, derived: bool = True, root: Path = ROOT) -> dict:
    stats = {"pwsh_spawns": 0}
    allow = load_allow(allow_path)
    base_sha = git(root, "rev-parse", "--verify", base + "^{commit}").strip()
    head_sha = git(root, "rev-parse", "HEAD").strip()
    notes: list[str] = []
    hook_cases, playback_rows, pb = collect(lints, corpus_path, derived, root, base_sha, notes)
    work = Path(tempfile.mkdtemp(prefix="safety-lint-parity-"))
    results: list[dict] = []
    try:
        if hook_cases:
            head_hook_path = head_hook or (root / HOOK_REL)
            # master's bytes run as if they lived at the head hook's path (see load_source_as): one worktree_root for both.
            base_module = load_source_as(git(root, "show", f"{base_sha}:{HOOK_REL}"), "parity_base_hook", head_hook_path.resolve())
            # r7: the head loads from the SAME resolved spelling as master. Before, master got .resolve() and the head the raw path, so a head hook
            # under an alias (8.3 short name, junction, subst drive: a hosted Windows temp dir) derived a different worktree_root than master.
            head_module = load_module(head_hook_path.resolve(), "parity_head_hook")
            results += run_hook(base_module, head_module, hook_cases)
        if playback_rows:
            if pb is None or not getattr(pb, "PWSH", None):
                raise ParityError("pwsh is not on PATH: the playback lint cannot be judged")
            base_lint = work / "lint_master.ps1"
            base_lint.write_text(git(root, "show", f"{base_sha}:{LINT_REL}"), encoding="utf-8")
            results += run_playback(pb, base_lint, head_lint or (root / LINT_REL), playback_rows, work, stats)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for res in results:
        res["class"] = classify(res, allow)
        # r5 (CoS I2): an entry excuses a row only when its id AND its text hash match; a hash mismatch is treated as unlisted
        entry = allow.get(res["id"]) if res["class"] == "LOOSENED" else None
        res["allowed"] = entry is not None and entry["hash"] == res.get("text_hash")
        if res["allowed"]:
            res["allow_entry"] = entry
        elif entry is not None:
            res["allow_hash_mismatch"] = entry["hash"]
    loosened = [r for r in results if r["class"] == "LOOSENED" and not r["allowed"]]
    errors = [r for r in results if r["class"] == "error"]
    counts = {c: sum(1 for r in results if r["class"] == c) for c in ("same", "tightened", "LOOSENED", "error")}
    rc = EXIT_LOOSENED if loosened else (EXIT_UNDECIDED if errors else EXIT_OK)
    return {"base": base, "base_sha": base_sha, "head_sha": head_sha, "lints": list(lints), "rc": rc, "rows": len(results),
            "counts": counts, "loosened_unlisted": len(loosened), "allowed": sum(1 for r in results if r["allowed"]),
            "stale_allow_ids": sorted(set(allow) - {r["id"] for r in results if r["class"] == "LOOSENED"}),
            "pwsh_spawns": stats["pwsh_spawns"], "notes": notes, "results": results}


def render(report: dict) -> str:
    lines = [f"safety-lint-parity base={report['base']} ({report['base_sha'][:8]}) head={report['head_sha'][:8]} lints={','.join(report['lints'])}"]
    for r in report["results"]:
        if r["class"] == "LOOSENED" and not r["allowed"]:
            stale_hash = (f" (allow-list entry {r['id']} is bound to hash {r['allow_hash_mismatch']}, this row's text hashes to "
                          f"{r['text_hash']}: treated as unlisted)") if "allow_hash_mismatch" in r else ""
            lines.append(f"LOOSENED {r['id']} [{r['lint']}] hash={r['text_hash']} master={r['master']} head={r['head']}{stale_hash} :: {r['display']}")
        elif r["allowed"]:
            e = r["allow_entry"]
            lines.append(f"ALLOWED-LOOSENED {r['id']} [{r['lint']}] pr={e['pr']} reason={e['reason']} :: {r['display']}")
        elif r["class"] == "error":
            lines.append(f"ERROR {r['id']} [{r['lint']}] {r['error']}")
    for note in report.get("notes", []):
        lines.append(f"note: {note}")
    for stale in report["stale_allow_ids"]:
        lines.append(f"note: allow-list entry {stale} matches no LOOSENED row (a ceiling, not a requirement)")
    c = report["counts"]
    lines.append(f"SAFETY-LINT-PARITY rc={report['rc']} rows={report['rows']} same={c['same']} tightened={c['tightened']} "
                 f"loosened={c['LOOSENED']} (unlisted={report['loosened_unlisted']}, allowed={report['allowed']}) error={c['error']} "
                 f"pwsh_spawns={report['pwsh_spawns']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay one deny corpus through master's safety lints and the head's.")
    parser.add_argument("--base", default="fork/master", help="git ref whose lints are the baseline (default fork/master)")
    parser.add_argument("--lint", choices=("hook", "playback", "all"), default="all")
    parser.add_argument("--json", action="store_true", help="print the full report as JSON instead of the summary")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--allow", type=Path, default=DEFAULT_ALLOW)
    parser.add_argument("--head-hook", type=Path, default=None, help="judge this hook file as the head (default: the working tree's)")
    parser.add_argument("--head-lint", type=Path, default=None, help="judge this playback lint as the head (default: the working tree's)")
    parser.add_argument("--no-derived", action="store_true", help="skip rows derived from the head's test modules (the JSON corpus only)")
    args = parser.parse_args(argv)
    lints = LINTS if args.lint == "all" else (args.lint,)
    try:
        report = run(args.base, lints, args.corpus, args.allow, args.head_hook, args.head_lint, not args.no_derived)
    except ParityError as exc:
        print(f"SAFETY-LINT-PARITY rc={EXIT_UNDECIDED} CANNOT-DECIDE: {exc}")
        return EXIT_UNDECIDED
    print(json.dumps(report, indent=1) if args.json else render(report))
    return report["rc"]


if __name__ == "__main__":
    sys.exit(main())
