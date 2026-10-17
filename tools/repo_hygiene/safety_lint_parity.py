#!/usr/bin/env python3
"""SAFETY-LINT-MASTER-PARITY-DIFF-1: replay one deny corpus through fork/master's safety lints and the head's.

WHY THIS EXISTS (FWO-DEFECT-PREVENT-1 item 1).  Several PRs on 2026-10-16 changed a safety lint and passed their own pinned
rows, yet let through a command that fork/master's lint DENIED (HOOK-NA2-REDIRECT-FALSE-POSITIVE-1 r8/r9, NA2-RULE4-PER-LAUNCHER-
SCOPE-1 `--clip` variants).  Nothing compared the head against master over a whole corpus.  This runner does: every corpus row
is judged by master's lint and by the head's, and a row master DENIES while the head ALLOWS is a LOOSENED row.  A LOOSENED row
fails the run unless safety_lint_parity_allow.json lists its id with a reason and a PR number.

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
    comparison with no edit here.  A derived row that cannot be built is an ERROR, never a skip.

EXIT CODES.  0 = no unlisted LOOSENED row and no error.  1 = at least one unlisted LOOSENED row (reported even when an error
also occurred, because it is the definite finding).  2 = could not decide: a git failure, a lint that threw, an unparseable or
unrunnable row, a malformed corpus or allow-list.  A row that cannot be run is never a pass.

KNOWN LIMIT.  Only the head's test modules are imported for derived rows.  A deny row the head DELETED from its tests escapes
unless the JSON corpus also carries it; the pinned minimum row counts in test_safety_lint_parity.py exist so that a shrinking
corpus is a red test, not a quiet one.
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
    """The static rows of the JSON corpus, groups and generators expanded. Every row: id, lint, why, and command/text."""
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ParityError(f"corpus {path} is unreadable: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != CORPUS_SCHEMA:
        raise ParityError(f"corpus {path} does not declare schema {CORPUS_SCHEMA}")
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


def load_allow(path: Path) -> dict[str, dict]:
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ParityError(f"allow-list {path} is unreadable: {exc}") from exc
    if not isinstance(entries, list):
        raise ParityError(f"allow-list {path} must be a JSON list of {{id, reason, pr}}")
    out: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not str(entry.get("id", "")).strip() or not str(entry.get("reason", "")).strip() or entry.get("pr") in (None, "", 0):
            raise ParityError(f"allow-list entry needs an id, a reason and a pr number: {str(entry)[:160]}")
        if entry["id"] in out:
            raise ParityError(f"allow-list lists {entry['id']} twice")
        out[entry["id"]] = entry
    return out


# ------------------------------------------------------------------------------------------------ hook rows

class HookCase:
    """One hook row: how to build (stdin text, environment, venue) on demand, and a cleanup for its tmp board."""

    def __init__(self, row_id: str, why: str, source: str, build):
        self.id, self.why, self.source, self.build = row_id, why, source, build
        self.display = ""


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


def derived_hook_cases(root: Path) -> list[HookCase]:
    """Every DENY row of the head's CASES table, built exactly as the suite builds it, minus the subprocess."""
    tm = load_module(root / HOOK_TEST_REL, "parity_head_hook_tests")
    cases = []
    used: set[str] = set()
    for case in tm.CASES:
        if case.get("expect") != "DENY":
            continue
        row_id = "hook:" + slug(case["name"])
        while row_id in used:
            row_id += "-x"
        used.add(row_id)

        def build(case=case, tm=tm):
            probe = tm.MlvNeverAuthorizedHookTests("test_hook_script_is_present_on_this_ref")
            probe.setUp()
            overrides = tm.FIXTURES[case.get("fixture", "default")](probe.paths)
            probe._bind_the_enable_literal()
            if "raw" in case:
                stdin_text = case["raw"]
            else:
                stdin_text = json.dumps({"tool_name": case["tool"],
                                         "tool_input": probe._render_content(probe._substitute(case["input"]))})
            env, venue = _hook_env(probe.paths, overrides, tm.UNSET, tm.VENUE_KEY)
            return stdin_text, env, venue, probe.doCleanups

        hc = HookCase(row_id, f"{HOOK_TEST_REL} CASES[{case['name']!r}] (group {case.get('group')}, {case.get('na', '?')})",
                      "derived", build)
        hc.display = str(case.get("input", case.get("raw", "")))[:200]
        cases.append(hc)
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

            hc = HookCase(row_id, row["why"], "corpus", build)
            hc.display = row["command"][:200]
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
        res = {"id": case.id, "lint": "hook", "why": case.why, "source": case.source, "display": case.display}
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

def derived_playback_rows(root: Path) -> tuple[list[dict], object]:
    """Every must-refuse sentence of the head's playback suite; also returns that module (its `_json_counts` is the probe)."""
    pb = load_module(root / PLAYBACK_TEST_REL, "parity_head_playback_tests")
    rows: dict[str, dict] = {}

    def add(name: str, text: str, where: str) -> None:
        rows.setdefault(f"playback:{name}:{short_hash(text)}", {"text": text, "why": f"{PLAYBACK_TEST_REL} {where}"})

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
    return [{"id": k, "lint": "playback", "source": "derived", **v} for k, v in rows.items()], pb


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
        res = {"id": row["id"], "lint": "playback", "why": row["why"], "source": row["source"], "display": row["text"][:200]}
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


def collect(lints, corpus_path: Path, derived: bool, root: Path = ROOT):
    """(hook cases, playback rows, playback module or None). Pure collection: nothing is judged here."""
    static = load_corpus(corpus_path)
    hook_cases = corpus_hook_cases(static) if "hook" in lints else []
    playback = [r for r in static if r["lint"] == "playback"] if "playback" in lints else []
    pb = None
    if "hook" in lints and derived:
        hook_cases += derived_hook_cases(root)
    if "playback" in lints and (derived or playback):
        extra, pb = derived_playback_rows(root)
        if derived:
            playback = playback + [r for r in extra if r["id"] not in {p["id"] for p in playback}]
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
    hook_cases, playback_rows, pb = collect(lints, corpus_path, derived, root)
    work = Path(tempfile.mkdtemp(prefix="safety-lint-parity-"))
    results: list[dict] = []
    try:
        if hook_cases:
            head_hook_path = head_hook or (root / HOOK_REL)
            # master's bytes run as if they lived at the head hook's path (see load_source_as): one worktree_root for both.
            base_module = load_source_as(git(root, "show", f"{base_sha}:{HOOK_REL}"), "parity_base_hook", head_hook_path.resolve())
            head_module = load_module(head_hook_path, "parity_head_hook")
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
        res["allowed"] = res["class"] == "LOOSENED" and res["id"] in allow
        if res["allowed"]:
            res["allow_entry"] = allow[res["id"]]
    loosened = [r for r in results if r["class"] == "LOOSENED" and not r["allowed"]]
    errors = [r for r in results if r["class"] == "error"]
    counts = {c: sum(1 for r in results if r["class"] == c) for c in ("same", "tightened", "LOOSENED", "error")}
    rc = EXIT_LOOSENED if loosened else (EXIT_UNDECIDED if errors else EXIT_OK)
    return {"base": base, "base_sha": base_sha, "head_sha": head_sha, "lints": list(lints), "rc": rc, "rows": len(results),
            "counts": counts, "loosened_unlisted": len(loosened), "allowed": sum(1 for r in results if r["allowed"]),
            "stale_allow_ids": sorted(set(allow) - {r["id"] for r in results if r["class"] == "LOOSENED"}),
            "pwsh_spawns": stats["pwsh_spawns"], "results": results}


def render(report: dict) -> str:
    lines = [f"safety-lint-parity base={report['base']} ({report['base_sha'][:8]}) head={report['head_sha'][:8]} lints={','.join(report['lints'])}"]
    for r in report["results"]:
        if r["class"] == "LOOSENED" and not r["allowed"]:
            lines.append(f"LOOSENED {r['id']} [{r['lint']}] master={r['master']} head={r['head']} :: {r['display']}")
        elif r["allowed"]:
            e = r["allow_entry"]
            lines.append(f"ALLOWED-LOOSENED {r['id']} [{r['lint']}] pr={e['pr']} reason={e['reason']} :: {r['display']}")
        elif r["class"] == "error":
            lines.append(f"ERROR {r['id']} [{r['lint']}] {r['error']}")
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
