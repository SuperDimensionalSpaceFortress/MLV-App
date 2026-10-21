"""Run a changed tool's pinned suites on every runtime the tool declares and this box has.

RUNTIME-FLOOR-LOCAL-MATRIX-1 (CoS work order FWO-DEFECT-PREVENT-1 item 5, "CI on declared runtime
platforms"; evidence agent-bridge #233 and MLV #468, where a tool ran on one PowerShell or Python and broke
on another). ``tools/coordination/runtime-floors.json`` (schema ``mlv-app/runtime-floors/v1``) maps tool
path globs to the runtimes each one declares and to the suites that pin it. Given a worktree and a base this
script maps the changed files to manifest rows and runs each pinned suite once per declared runtime that is
PRESENT:

* ``ps-parse``  the host's own PowerShell parser over the changed ``.ps1``/``.psm1`` files (catches a 7-only
                operator such as ``??`` or ``?.`` on Windows PowerShell 5.1);
* ``ps-script`` ``<host> -NoProfile -File <path>`` for a self-contained PowerShell test script;
* ``pytest``    ``<python> -m pytest <path>[::Class] -q -p no:cacheprovider`` under each declared
                ``python-3.X`` (a row with no declared python runs it once, under this interpreter).

A declared runtime that is absent, or older than the row's ``min_versions``, prints ``SKIP_ABSENT <runtime>``
and is never counted as a pass. A row whose floor is ``undeclared`` is listed (``UNDECLARED_FLOOR``) and its
pytest suites run once; nothing is invented for it.

Exit codes: 0 = every task that ran passed, or no row matched ("none mapped");
            1 = a task failed or timed out (a timeout is a failure), or ``--strict-absent`` and a declared
                runtime was absent;
            2 = could not decide (bad worktree, unresolvable base, git failure, invalid manifest, a matched
                row naming a suite file that does not exist) -- UNKNOWN, never a pass.

``--check-evidence`` prints the resolved line beside each declared floor's ``line`` hint and runs nothing else
(exit 1 only for an unresolvable citation; a drifted hint is advice).

Read-only on the worktree: git calls only read, and every child runs with PYTHONDONTWRITEBYTECODE=1 and
``-p no:cacheprovider``. ``--list`` prints the plan without running anything.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

SCHEMA = "mlv-app/runtime-floors/v1"
DEFAULT_MANIFEST = "tools/coordination/runtime-floors.json"
PS_RUNTIMES = ("powershell-5.1", "pwsh")
RUNTIME_RE = re.compile(r"^(powershell-5\.1|pwsh|python-3\.\d{1,2})$")
SUITE_KINDS = ("pytest", "ps-parse", "ps-script")
FLOORS = ("declared", "undeclared")
PS_SUFFIXES = (".ps1", ".psm1")
_IS_NT = os.name == "nt"
_KILL_SIGNAL = getattr(signal, "SIGKILL", signal.SIGTERM)
TASKKILL_TIMEOUT = 30


class Undecidable(Exception):
    """The run cannot be decided; the caller exits 2."""


class ManifestError(Undecidable):
    pass


# --- manifest -----------------------------------------------------------------------------------------------


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in str(text).strip().split("."):
        match = re.match(r"\d+", piece)
        if not match:
            break
        parts.append(int(match.group(0)))
    return tuple(parts)


def validate_manifest(data: object) -> list[str]:
    """Return every schema problem (empty list = valid). Never raises on bad shapes."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["manifest is not a JSON object"]
    if data.get("schema") != SCHEMA:
        problems.append(f"schema must be {SCHEMA!r}, got {data.get('schema')!r}")
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        return problems + ["rows must be a non-empty list"]
    seen: set[str] = set()
    for index, row in enumerate(rows):
        label = f"rows[{index}]"
        if not isinstance(row, dict):
            problems.append(f"{label} is not an object")
            continue
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            problems.append(f"{label} has no id")
        elif row_id in seen:
            problems.append(f"{label} duplicate id {row_id!r}")
        else:
            seen.add(row_id)
            label = f"row {row_id!r}"
        globs = row.get("globs")
        if not isinstance(globs, list) or not globs or not all(isinstance(g, str) and g for g in globs):
            problems.append(f"{label}: globs must be a non-empty list of strings")
        floor = row.get("floor")
        runtimes = row.get("runtimes", [])
        if floor not in FLOORS:
            problems.append(f"{label}: floor must be one of {FLOORS}, got {floor!r}")
        if not isinstance(runtimes, list) or not all(isinstance(r, str) and RUNTIME_RE.match(r) for r in runtimes):
            problems.append(f"{label}: runtimes must be a list of {RUNTIME_RE.pattern}")
            runtimes = []
        evidence = row.get("evidence", [])
        if floor == "declared":
            if not runtimes:
                problems.append(f"{label}: a declared floor needs at least one runtime")
            if not isinstance(evidence, list) or not evidence:
                problems.append(f"{label}: a declared floor needs evidence (file, contains; optional within, line hint)")
        elif floor == "undeclared":
            if runtimes:
                problems.append(f"{label}: an undeclared floor must not list runtimes (nothing is invented)")
            if not (isinstance(row.get("note"), str) and row["note"]):
                problems.append(f"{label}: an undeclared floor needs a note saying what was searched")
        if isinstance(evidence, list):
            for item in evidence:
                if not (isinstance(item, dict) and isinstance(item.get("file"), str)
                        and isinstance(item.get("contains"), str) and item["contains"]
                        and isinstance(item.get("line", 1), int) and not isinstance(item.get("line"), bool)
                        and isinstance(item.get("within", "x"), str) and item.get("within", "x")):
                    problems.append(f"{label}: each evidence item needs file, contains; line (int hint) and within (string) are optional")
        mins = row.get("min_versions", {})
        if not isinstance(mins, dict) or any(k not in runtimes or not _version_tuple(str(v)) for k, v in mins.items()):
            problems.append(f"{label}: min_versions keys must be declared runtimes with numeric versions")
        suites = row.get("suites", [])
        if not isinstance(suites, list):
            problems.append(f"{label}: suites must be a list")
            suites = []
        for suite in suites:
            if not isinstance(suite, dict) or suite.get("kind") not in SUITE_KINDS:
                problems.append(f"{label}: suite kind must be one of {SUITE_KINDS}")
                continue
            if "slow" in suite and not isinstance(suite["slow"], bool):
                problems.append(f"{label}: suite slow must be a boolean")
            if suite["kind"] in ("pytest", "ps-script") and not (isinstance(suite.get("path"), str) and suite["path"]):
                problems.append(f"{label}: a {suite['kind']} suite needs a path")
            if suite["kind"] in ("ps-parse", "ps-script") and floor == "declared" \
                    and not any(r in PS_RUNTIMES for r in runtimes):
                problems.append(f"{label}: a {suite['kind']} suite needs a declared PowerShell runtime")
    return problems


def load_manifest(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    problems = validate_manifest(data)
    if problems:
        raise ManifestError("invalid manifest " + str(path) + ": " + "; ".join(problems[:5]))
    return data


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip())


def resolve_evidence(item: dict, lines: list[str]) -> tuple[int | None, str | None]:
    """Resolve one evidence item by its text anchor: (1-based line, None) or (None, problem).

    ``contains`` must match exactly one line of the scope. The scope is the whole file, or with ``within`` the
    block that opens at the one line holding that literal and runs over the lines indented deeper than it
    (a function, a YAML job). ``line`` is only a hint: an edit above the cited text never turns it red.

    The scope ends at the first non-blank line indented no deeper than the ``within`` line, so it ends EARLY at a
    comment at the same indent, a column-zero line (a here-string body), or an opening brace on the line after the
    ``within`` line. An early end can only turn a citation red (``matches 0 lines``): ``contains`` must still be
    unique inside the shorter scope, so it never resolves to a different line.
    """
    scope = range(len(lines))
    where = "the file"
    anchor = item.get("within")
    if anchor:
        opens = [i for i, text in enumerate(lines) if anchor in text]
        if len(opens) != 1:
            return None, f"'within' anchor {anchor!r} matches {len(opens)} lines (need exactly 1)"
        start = opens[0]
        end = start + 1
        while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > _indent(lines[start])):
            end += 1
        scope = range(start, end)
        where = f"the block under {anchor!r}"
    hits = [i for i in scope if item["contains"] in lines[i]]
    if len(hits) != 1:
        return None, f"{item['contains']!r} matches {len(hits)} lines of {where} (need exactly 1)"
    return hits[0] + 1, None


def _walk_evidence(manifest: dict, root: Path):
    """Yield (row id, evidence item, resolved 1-based line or None, problem or None) for every citation."""
    for row in manifest["rows"]:
        for item in row.get("evidence", []):
            target = Path(root) / item["file"]
            if not target.is_file():
                yield row["id"], item, None, f"evidence file missing: {item['file']}"
                continue
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
            resolved, problem = resolve_evidence(item, lines)
            yield row["id"], item, resolved, (f"{item['file']}: {problem}" if problem else None)


def check_evidence(manifest: dict, root: Path) -> list[str]:
    """Each declared floor must cite, by a unique text anchor, a real file that contains the quoted text."""
    return [f"{row_id}: {problem}" for row_id, _, _, problem in _walk_evidence(manifest, root) if problem]


def evidence_report(manifest: dict, root: Path) -> list[str]:
    """One line per citation: the resolved line beside its ``line`` hint. Drift is advice, never a failure; only an
    UNRESOLVED citation (the problems ``check_evidence`` returns) is one."""
    report = []
    for row_id, item, resolved, problem in _walk_evidence(manifest, root):
        if problem:
            report.append(f"{row_id}: UNRESOLVED {problem}")
            continue
        hint = item.get("line")
        if hint is None:
            note = "no hint"
        elif hint == resolved:
            note = f"hint {hint} (in sync)"
        else:
            note = f"hint {hint} (drifted by {abs(hint - resolved)})"
        report.append(f"{row_id}: {item['file']}: resolved line {resolved}, {note}")
    return report


# --- runtimes -----------------------------------------------------------------------------------------------


def _probe(argv: list[str], timeout: int = 60) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() if done.returncode == 0 else None


def detect_runtime(runtime_id: str) -> dict:
    """Return {"id", "argv", "version"}; argv is None when the runtime is absent."""
    absent = {"id": runtime_id, "argv": None, "version": ""}
    if runtime_id in PS_RUNTIMES:
        exe = shutil.which("powershell.exe" if runtime_id == "powershell-5.1" else "pwsh")
        if not exe:
            return absent
        version = _probe([exe, "-NoProfile", "-NonInteractive", "-Command", "$PSVersionTable.PSVersion.ToString()"])
        if not version:
            return absent
        wanted = _version_tuple(version)[:2] == (5, 1) if runtime_id == "powershell-5.1" else _version_tuple(version)[:1] >= (7,)
        return {"id": runtime_id, "argv": [exe], "version": version} if wanted else absent
    major, minor = (int(p) for p in runtime_id.split("-", 1)[1].split("."))
    candidates: list[list[str]] = []
    if sys.version_info[:2] == (major, minor):
        candidates.append([sys.executable])
    if shutil.which("py"):
        candidates.append(["py", f"-{major}.{minor}"])
    if shutil.which(f"python{major}.{minor}"):
        candidates.append([f"python{major}.{minor}"])
    for argv in candidates:
        version = _probe(argv + ["-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])"])
        if version and _version_tuple(version)[:2] == (major, minor):
            return {"id": runtime_id, "argv": argv, "version": version}
    return absent


# --- git ----------------------------------------------------------------------------------------------------


def _git(worktree: Path, *args: str, nul: bool = False) -> list[str]:
    """Output records, decoded as UTF-8. ``nul=True`` splits on NUL for a ``-z`` command, so a path holding a
    quote, tab, newline or non-ASCII character is returned as written, never as a quoted escape string."""
    try:
        done = subprocess.run(["git", "-C", str(worktree), "-c", "core.quotepath=off", *args],
                              capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Undecidable(f"git {' '.join(args)} could not run: {exc}") from exc
    if done.returncode != 0:
        raise Undecidable(f"git {' '.join(args)} failed (exit {done.returncode}): "
                          f"{done.stderr.decode('utf-8', 'replace').strip()[:200]}")
    return _records(done.stdout, nul)


def _records(stdout: bytes, nul: bool) -> list[str]:
    """Decode git output as UTF-8 and split it into records, leaving every character of a name as written."""
    out = stdout.decode("utf-8", "replace")
    return [rec for rec in (out.split("\0") if nul else out.splitlines()) if rec]


def changed_files(worktree: Path, base: str) -> tuple[str, list[str]]:
    """Committed (merge-base..HEAD) plus uncommitted tracked changes, sorted. git writes ``/`` as the separator in
    ``-z`` output on every OS, so names are returned as git wrote them: a ``\\`` is a legal POSIX filename character."""
    if not Path(worktree).is_dir():
        raise Undecidable(f"worktree does not exist: {worktree}")
    _git(worktree, "rev-parse", "--git-dir")
    merge_base = (_git(worktree, "merge-base", base, "HEAD") or [""])[0].strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", merge_base):
        raise Undecidable(f"git merge-base returned a value that is not a commit sha: {merge_base!r}")
    names = _git(worktree, "diff", "--name-only", "-z", "--no-renames", f"{merge_base}..HEAD", nul=True)
    names += _git(worktree, "diff", "--name-only", "-z", "--no-renames", "HEAD", nul=True)
    return merge_base, sorted(set(names))


# --- plan ---------------------------------------------------------------------------------------------------


def _hit(path: str, globs: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path.lower(), g.lower()) for g in globs)


def match_rows(manifest: dict, changed: list[str]) -> list[tuple[dict, list[str]]]:
    """Rows whose globs, or whose own suite files (the pin itself moved), touch the changed set.

    A path claimed by a declared row is not also reported by an undeclared catch-all row.
    """
    matched = []
    claimed: set[str] = set()
    for floor in ("declared", "undeclared"):
        for row in manifest["rows"]:
            if row["floor"] != floor:
                continue
            suite_files = {s["path"].split("::", 1)[0].lower() for s in row.get("suites", []) if s.get("path")}
            because = [p for p in changed if _hit(p, row["globs"]) or p.lower() in suite_files]
            if floor == "undeclared":
                because = [p for p in because if p not in claimed]
            if because:
                matched.append((row, because))
                if floor == "declared":
                    claimed.update(because)
    return matched


def build_plan(matched: list[tuple[dict, list[str]]], worktree: Path, detect=detect_runtime,
               include_slow: bool = False) -> list[dict]:
    """One task per (suite, runtime). status: RUN | SKIP_ABSENT | SKIP_SLOW | NA. Raises Undecidable on a missing suite file."""
    cache: dict[str, dict] = {}

    def runtime(rid: str) -> dict:
        if rid not in cache:
            cache[rid] = detect(rid)
        return cache[rid]

    tasks: list[dict] = []
    seen: set[tuple] = set()

    def add(task: dict) -> None:
        key = (task["kind"], task["suite"], task["runtime"], tuple(task.get("files", ())))
        if key not in seen:
            seen.add(key)
            tasks.append(task)

    for row, because in matched:
        for suite in row.get("suites", []):
            kind, path = suite["kind"], suite.get("path", "")
            if path and not (Path(worktree) / path.split("::", 1)[0]).is_file():
                raise Undecidable(f"row {row['id']!r} names a suite file that does not exist: {path.split('::', 1)[0]}")
        declared = row["floor"] == "declared"
        if not declared:
            add({"kind": "row-note", "suite": row["id"], "runtime": None, "status": "UNDECLARED_FLOOR",
                 "row": row["id"], "files": because, "note": row.get("note", "")})
        for suite in row.get("suites", []):
            kind, path = suite["kind"], suite.get("path", "")
            if kind == "pytest":
                py = [r for r in row.get("runtimes", []) if r.startswith("python-")] if declared else []
                targets = py or [None]
            else:
                targets = [r for r in row.get("runtimes", []) if r in PS_RUNTIMES] if declared else []
                if not targets:
                    add({"kind": kind, "suite": path or "(parse)", "runtime": None, "status": "NA", "row": row["id"],
                         "files": because, "note": "floor undeclared: no host to run it on"})
                    continue
            files = [p for p in because if p.lower().endswith(PS_SUFFIXES) and (Path(worktree) / p).is_file()]
            for rid in targets:
                base = {"kind": kind, "suite": path or "(parse)", "runtime": rid, "row": row["id"],
                        "files": files if kind == "ps-parse" else []}
                if kind == "ps-parse" and not files:
                    add({**base, "status": "NA", "note": "no changed .ps1/.psm1 file to parse"})
                    continue
                if suite.get("slow") and not include_slow:
                    add({**base, "status": "SKIP_SLOW", "note": "marked slow in the manifest; pass --include-slow"})
                    continue
                if rid is None:
                    add({**base, "status": "RUN", "argv_prefix": [sys.executable]})
                    continue
                info = runtime(rid)
                need = row.get("min_versions", {}).get(rid)
                if info["argv"] is None:
                    add({**base, "status": "SKIP_ABSENT", "note": "not installed on this box"})
                elif need and _version_tuple(info["version"]) < _version_tuple(need):
                    add({**base, "status": "SKIP_ABSENT", "note": f"have {info['version']}, need >={need}"})
                else:
                    add({**base, "status": "RUN", "argv_prefix": info["argv"], "version": info["version"]})
    return tasks


def _parse_snippet(files: list[str]) -> str:
    quoted = ",".join("'" + f.replace("'", "''") + "'" for f in files)
    return (
        "$ErrorActionPreference = 'Stop'; $bad = 0; "
        f"foreach ($f in @({quoted})) {{ $t = $null; $e = $null; "
        "[void][System.Management.Automation.Language.Parser]::ParseFile($f, [ref]$t, [ref]$e); "
        "if ($e -and $e.Count -gt 0) { $bad++; ('PARSE-ERR {0} :: {1} @{2}' -f $f, $e[0].Message, $e[0].Extent.StartLineNumber) } "
        "else { ('parsed {0}' -f $f) } }; "
        "if ($bad -gt 0) { exit 1 }; exit 0"
    )


def task_argv(task: dict, worktree: Path) -> list[str]:
    prefix = list(task["argv_prefix"])
    if task["kind"] == "pytest":
        return prefix + ["-m", "pytest", task["suite"], "-q", "-p", "no:cacheprovider"]
    ps = ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
    if task["kind"] == "ps-script":
        return prefix + ps + ["-File", str(Path(worktree) / task["suite"])]
    files = [str(Path(worktree) / f) for f in task["files"]]
    encoded = base64.b64encode(_parse_snippet(files).encode("utf-16-le")).decode("ascii")
    return prefix + ps + ["-EncodedCommand", encoded]


def _spawn(argv: list[str], **kwargs):
    """The one place a suite child is started; tests replace it to capture argv and cwd without a real shell."""
    return subprocess.Popen(argv, **kwargs)


def _kill_tree(proc) -> None:
    """Kill the timed-out child and everything it started. Every step is bounded or best-effort, so the kill
    itself can never hang the runner."""
    if _IS_NT:
        try:
            subprocess.run(["taskkill.exe", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           timeout=TASKKILL_TIMEOUT)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(proc.pid, _KILL_SIGNAL)  # the child leads its own session, so its pid is the group id
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_task(task: dict, worktree: Path, timeout: int) -> dict:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    started = time.monotonic()
    status, out = "PASS", ""
    own_session = {} if _IS_NT else {"start_new_session": True}
    try:
        proc = _spawn(task_argv(task, worktree), cwd=str(worktree), env=env, stdout=subprocess.PIPE,
                      stderr=subprocess.STDOUT, text=True, errors="replace", **own_session)
        try:
            out, _ = proc.communicate(timeout=timeout)
            if proc.returncode != 0:
                status = "FAIL"
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                out, _ = proc.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                out = ""
            status = "TIMEOUT"
    except OSError as exc:
        status, out = "FAIL", f"runner error: {exc}"
    tail = [line for line in (out or "").splitlines() if line.strip()][-25:]
    return {"status": status, "seconds": round(time.monotonic() - started, 1), "tail": tail}


# --- main ---------------------------------------------------------------------------------------------------


def _label(task: dict) -> str:
    runtime = task["runtime"] or "default"
    return f"{task['kind']} {task['suite']} [{runtime}] (row {task['row']})"


def main(argv: list[str] | None = None, detect=detect_runtime) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--worktree", required=True)
    parser.add_argument("--base", default="fork/master")
    parser.add_argument("--manifest", default=None, help=f"default: <worktree>/{DEFAULT_MANIFEST}")
    parser.add_argument("--timeout", type=int, default=900, help="seconds per task; a timeout is a failure")
    parser.add_argument("--list", action="store_true", help="print the plan and run nothing")
    parser.add_argument("--strict-absent", action="store_true", help="a declared runtime that is absent exits 1")
    parser.add_argument("--include-slow", action="store_true", help="also run suites the manifest marks slow (over about a minute)")
    parser.add_argument("--check-evidence", action="store_true",
                        help="print the resolved line beside each evidence hint and run nothing else; exit 1 only on an UNRESOLVED citation")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    # Absolute once, before planning and spawning: every child runs with cwd=<worktree>, so a relative value kept
    # as given and prefixed onto a file argument would be looked up under <worktree>/<worktree>/...
    worktree = Path(os.path.abspath(args.worktree))
    result: dict ={"worktree": str(worktree), "base": args.base, "changed": [], "tasks": [], "verdict": "UNKNOWN"}

    def emit(code: int, verdict: str, note: str) -> int:
        result["verdict"], result["note"] = verdict, note
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"{verdict}: {note}")
        return code

    try:
        manifest = load_manifest(Path(args.manifest) if args.manifest else worktree / DEFAULT_MANIFEST)
        if args.check_evidence:
            report = evidence_report(manifest, worktree)
            result["evidence"] = report
            if not args.json:
                print("\n".join(report))
            bad = sum("UNRESOLVED" in line for line in report)
            if bad:
                return emit(1, "FAIL", f"{bad} of {len(report)} citation(s) unresolved")
            return emit(0, "PASS", f"{len(report)} citation(s) resolved")
        merge_base, changed = changed_files(worktree, args.base)
        result.update(mergeBase=merge_base, changed=changed)
        matched = match_rows(manifest, changed)
        tasks = build_plan(matched, worktree, detect, args.include_slow)
    except Undecidable as exc:
        return emit(2, "UNKNOWN", str(exc))
    result["tasks"] = tasks
    if not args.json:
        print(f"WorkDir   : {worktree}")
        print(f"Base      : {args.base} (merge-base {merge_base})")
        print(f"Changed   : {len(changed)} file(s); {len(matched)} manifest row(s) matched")
    if not matched:
        return emit(0, "NONE-MAPPED", "none mapped: no changed file belongs to a runtime-floors row")
    skipped = failed = ran = 0
    for task in tasks:
        status = task["status"]
        if status == "UNDECLARED_FLOOR":
            line = f"UNDECLARED_FLOOR {task['row']} (listed, not invented): {task['note']}"
        elif status == "NA":
            line = f"N/A     {_label(task)}: {task['note']}"
        elif status == "SKIP_SLOW":
            line = f"SKIP_SLOW {_label(task)}: {task['note']}"
        elif status == "SKIP_ABSENT":
            skipped += 1
            line = f"SKIP_ABSENT {task['runtime']} ({_label(task)}): {task['note']}"
        elif args.list:
            line = f"WOULD-RUN {_label(task)}" + (f" on {task['version']}" if task.get("version") else "")
        else:
            outcome = run_task(task, worktree, args.timeout)
            task.update(outcome)
            ran += 1
            failed += outcome["status"] != "PASS"
            line = f"{outcome['status']:<7} {_label(task)}  ({outcome['seconds']:.1f}s)"
            if outcome["status"] != "PASS":
                line += "".join(f"\n    | {t}" for t in outcome["tail"])
        if not args.json:
            print(line)
    if args.list:
        return emit(0, "NOT-RUN", f"plan only; {skipped} declared runtime task(s) would be SKIP_ABSENT")
    if failed:
        return emit(1, "FAIL", f"{failed} of {ran} task(s) failed or timed out; {skipped} SKIP_ABSENT")
    if skipped and args.strict_absent:
        return emit(1, "FAIL", f"--strict-absent: {skipped} declared runtime task(s) were SKIP_ABSENT; {ran} ran and passed")
    return emit(0, "PASS", f"{ran} task(s) ran and passed; {skipped} SKIP_ABSENT (an absent runtime is never a pass)")


if __name__ == "__main__":
    sys.exit(main())
