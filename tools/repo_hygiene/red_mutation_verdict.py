"""Red-mutation verdict: did the mutation fail AT the intended assertion?

Bus trap 7cf7477 (airmypc, 2026-10-28): a test whose probe value cannot distinguish
the bug from the fix passes under both, and its mutation proof passes too. Local
instance: PR #705 B1, a 2.8 s probe that rounded to 3 on both the bug reading and the
fix reading, so the Math.Min(330, remainingSeconds) Int32-overload defect stayed
hidden. A red-mutation proof that only shows "the suite went red" inherits the hole:
the red can come from an import error or an unrelated test.

Two decidable pieces:

* ``probe_discriminates(bug_value, fix_value, window)``: the probe is usable only if
  the reading the assertion sees under the FIX lies inside the assertion window and
  the reading under the BUG lies outside it. Pass the readings AFTER any rounding or
  conversion the code under test applies, so a collision (2.8 and 2.8 both read as 3)
  shows up as a refusal.
* ``verdict(output, expected)``: reads saved unittest or pytest output of a mutation
  run plus the expected assertion (``file:line`` or a test id) and returns PASS only
  when that assertion is one that failed.

CLI (exit 0 = PASS, 1 = REFUSED, 2 = bad usage or an unreadable --output file)::

    py -3 tools/repo_hygiene/red_mutation_verdict.py verdict --output red.txt --expect tools/x/test_y.py:123
    py -3 tools/repo_hygiene/red_mutation_verdict.py probe --bug 3 --fix 3 --window 3 3

NON-PROMISES:
- It reads text. It cannot tell that the saved output came from a run of the mutated
  tree, or that the mutation was reverted afterwards; the restore proof is a separate
  check (project memory red-mutation-restore-is-not-a-rebuild-20261013).
- It reads the default ``unittest`` layout (``FAIL:``/``ERROR:`` blocks with ``File "...",
  line N`` frames) and the default ``pytest`` layout (``FAILURES``/``ERRORS`` sections with
  ``file.py:N:`` lines and ``E`` lines, plus the ``FAILED`` summary). Any other runner or
  ``--tb=no`` output with no ``FAILED`` summary lines is REFUSED as unrecognized, never PASSed.
- Matching is one-directional: the expectation may be a SUFFIX of what the run printed,
  on a segment boundary, and never the reverse. ``file:line`` matches when ANY traceback
  frame of a failing assertion sits at that line in a file whose path ENDS WITH the
  expected path (``pkg/test_x.py:5`` matches a frame in ``/w/pkg/test_x.py``;``elsewhere/test_x.py:5``
  does not match a frame ``test_x.py``), so naming the test body line or the helper line
  that raised both work. A test id is compared with the failure's FULL identity (unittest
  ``module.Class.method``; pytest node id from the ``FAILED`` summary line), so
  ``zz.Nope.test_scale`` never matches ``test_s.B.test_scale``. A bare ``test_scale`` that
  matches two different failing tests is REFUSED as ambiguous: name the class.
- An id is compared segment by segment, never flattened to a string: ``/`` and ``.`` inside a
  pytest parameter id (``test_x[a/b]``, ``test_x[a.b]``) stay inside it, ``a.py/t.py`` is not
  ``a/t.py``, and a node id may hold spaces.
- An ``ImportError``/``ModuleNotFoundError`` anywhere in the run blocks PASS like a collection
  error: in an ERROR or FAIL block, in any ``E`` line of a pytest section (chained exceptions
  included), or in a ``FAILED``/``ERROR`` summary line (``--tb=no``). So does an ``ERROR <file>``
  summary line, and a ``FAILED`` summary line that cannot be split into node id and message
  without guessing (a parameter id holding ``] - ``).
- The runner's FINAL result line decides green: unittest ``OK`` or a pytest bar with no
  failed/error count means no assertion failed, whatever failure-looking text a test printed above it.
- Whitespace inside an id is significant (`` a/t.py`` is not ``a/t.py``; ``test one`` is not ``test``).
- A pytest failure section is tied to its node id by test name AND by the file in its
  traceback frames. With no ``FAILED`` summary line (``-rN``) the section has no identity,
  so only ``file:line`` can match it.
- Other tests that also failed do not block a PASS; a collection or import error anywhere
  in the run does (the run is red for a reason that is not the assertion).
- It does not know whether the assertion is the RIGHT one: choosing the expected site is
  the author's claim, and this check only binds the run to it.
- INNERMOST FRAME (follow-up card RMV-INNERMOST-FRAME, not promised here): a ``file:line``
  match on ANY frame means an ``assert`` inside production code reached from the test, or a
  chained pytest exception whose earlier frame is the expected site, can read as PASS. The
  checker does not require the expected site to be the frame that raised.
- ``probe_discriminates`` compares two numbers against a closed window. It does not model
  how the code under test rounds; the caller computes the readings and writes the
  arithmetic next to the probe.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PASS = "PASS"
REFUSED = "REFUSED"

_EQ_RULE = re.compile(r"^={20,}\s*$")
_FRAME = re.compile(r'^\s*File "(?P<file>.+)", line (?P<line>\d+)')
_UT_HEAD = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<name>.+?)(?: \((?P<ident>[^()]*)\))?(?: \(.*\))?\s*$")
_PT_SECTION = re.compile(r"^_{2,} (?P<name>.+?) _{2,}\s*$")
_PT_TOP = re.compile(r"^={3,} (?P<title>.+?) ={3,}\s*$")
_PT_LOC = re.compile(r"^(?P<file>[^\s:][^\n]*?\.py):(?P<line>\d+):(?: (?P<rest>.*))?$")
_IMPORT_ERR = re.compile(r"^(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError)\b", re.M)
_EXPECT_SITE = re.compile(r"^(?P<file>.+\.\w+):(?P<line>\d+)$")
_RED_MARK = re.compile(r"^FAILED \(|^FAILED |\b\d+ (?:failed|errors?)\b|^Interrupted:", re.M)
_GREEN_MARK = re.compile(r"^OK\b|\b\d+ passed\b", re.M)
_RESULT_LINE = re.compile(r"^(?:OK\b.*|FAILED \(.*\)|=+ .*\bin \d[\d.]*s\b.*=+)\s*$", re.M)


@dataclass
class Failure:
    ids: list[str]
    frames: list[tuple[str, int]]
    assertion: bool
    collection: bool = False
    note: str = ""


@dataclass
class Parsed:
    failures: list[Failure] = field(default_factory=list)
    red: bool = False
    green: bool = False


def _norm_path(text: str) -> list[str]:
    return [p for p in text.replace("\\", "/").split("/") if p and p != "."]


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` outside square brackets (a pytest parameter id may hold any character)."""
    parts: list[str] = []
    depth = start = i = 0
    while i < len(text):
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]" and depth:
            depth -= 1
        elif depth == 0 and text.startswith(sep, i):
            parts.append(text[start:i])
            i += len(sep)
            start = i
            continue
        i += 1
    parts.append(text[start:])
    return [p for p in parts if p]


def _segs(text: str) -> list[str]:
    """Segments of a test id, lossless for its own syntax: pytest `dir/file.py::Class::name[param]`
    gives path parts (the .py suffix dropped) then the names; unittest `module.Class.method` gives
    its dotted parts. Separators inside `[...]` never split."""
    if "::" in text:
        parts = _split_top(text, "::")
        if not parts:
            return []
        head, names = parts[0], parts[1:]
        path = _norm_path(head)
        if path and path[-1].endswith(".py") and len(path[-1]) > 3:
            path[-1] = path[-1][:-3]
        return path + names
    if len(_split_top(text.replace("\\", "/") if "[" not in text else text, "/")) > 1:
        path = _norm_path(text)
        if path and path[-1].endswith(".py") and len(path[-1]) > 3:
            path[-1] = path[-1][:-3]
        return path
    return _split_top(text, ".")


def _parse_summary_line(line: str, word: str) -> tuple[str, str, bool] | None:
    """`FAILED <node> - <msg>` / `ERROR <node> - <msg>` -> (node, msg, ambiguous). The node ends at the
    first ' - ' outside brackets; when the node holds a parameter id and the message holds '] - ' the
    split cannot be told from a parameter id that contains it, so the line is flagged ambiguous."""
    if not line.startswith(word + " "):
        return None
    rest = line[len(word) + 1:].rstrip()
    depth = 0
    for i, c in enumerate(rest):
        if c == "[":
            depth += 1
        elif c == "]" and depth:
            depth -= 1
        elif depth == 0 and rest.startswith(" - ", i):
            node, msg = rest[:i], rest[i + 3:]
            return node, msg, "[" in node and "] - " in msg
    return rest, "", False


def _ends_with(whole: list[str], tail: list[str]) -> bool:
    """True when `tail` is a non-empty segment-boundary suffix of `whole`. One-directional on purpose:
    an expectation longer than what the run printed is never a match."""
    return bool(whole) and bool(tail) and len(tail) <= len(whole) and whole[-len(tail):] == tail


def _parse_unittest(text: str) -> list[Failure]:
    out: list[Failure] = []
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for line in text.splitlines():
        if _EQ_RULE.match(line):
            current = []
            blocks.append(current)
        elif current is not None:
            current.append(line)
    for block in blocks:
        body = [ln for ln in block if ln.strip()]
        if not body:
            continue
        head = _UT_HEAD.match(body[0])
        if not head:
            continue
        frames = [(m["file"], int(m["line"])) for m in map(_FRAME.match, block) if m]
        joined = "\n".join(block)
        collection = ("_FailedTest" in body[0] or "unittest/loader.py" in joined.replace("\\", "/")
                      or bool(_IMPORT_ERR.search(joined)))
        ident, name = head["ident"] or "", head["name"]
        # 3.11+ prints the full id in the parentheses; older Pythons print module.Class only.
        full = ident if ident.rsplit(".", 1)[-1] == name else (f"{ident}.{name}" if ident else name)
        ids = [] if "unittest.loader" in full else [full]
        out.append(Failure(ids, frames, assertion=head["kind"] == "FAIL" and not collection,
                           collection=collection, note=body[0].strip()))
    return out


def _same_file(a: list[str], b: list[str]) -> bool:
    return _ends_with(a, b) or _ends_with(b, a)


def _pair_nodes(want: list[str], frames: list[tuple[str, int]], nodes: list[str]) -> list[str]:
    cands = [n for n in nodes if _ends_with(_segs(n), want)]
    if frames:
        def in_file(n: str, file: str) -> bool:
            return _same_file(_norm_path(_split_top(n, "::")[0]), _norm_path(file))

        first = [n for n in cands if in_file(n, frames[0][0])]  # the test function's own file
        return first or [n for n in cands if any(in_file(n, file) for file, _line in frames)]
    return cands if len(cands) == 1 else []


def _parse_pytest(text: str) -> list[Failure]:
    out: list[Failure] = []
    nodes: list[tuple[str, str]] = []
    summary_blockers: list[str] = []
    top = ""
    cur: dict | None = None

    def close() -> None:
        nonlocal cur
        if cur is not None:
            out.append(Failure([cur["name"]], cur["frames"], cur["assertion"], cur["collection"], cur["name"]))
        cur = None

    for line in text.splitlines():
        m_top = _PT_TOP.match(line)
        if m_top:
            close()
            top = m_top["title"].strip()
            continue
        if top.startswith("short test summary"):
            for word in ("FAILED", "ERROR"):
                got = _parse_summary_line(line, word)
                if not got:
                    continue
                node, msg, ambiguous = got
                if word == "FAILED":
                    nodes.append((node, msg))
                if ambiguous:
                    summary_blockers.append(f"ambiguous summary line {word} {node[:60]}")
                elif _IMPORT_ERR.match(msg):
                    summary_blockers.append(f"{word} {node[:60]} - {msg[:40]}")
                elif word == "ERROR" and "::" not in node:
                    summary_blockers.append(f"ERROR {node}")
            continue
        if top not in ("FAILURES", "ERRORS"):
            continue
        m_sec = _PT_SECTION.match(line)
        if m_sec:
            close()
            name = m_sec["name"]
            cur = {"name": name, "frames": [], "assertion": None,
                   "collection": name.startswith("ERROR collecting")}
            continue
        if cur is None:
            continue
        if line.startswith("E") and line[1:2] in (" ", "\t"):
            first = line[1:].strip()
            if cur["assertion"] is None:
                cur["assertion"] = first.startswith(("assert ", "assert\t", "AssertionError", "Failed:")) or first == "assert"
            if _IMPORT_ERR.match(first):  # any E line: a chained exception counts too
                cur["collection"] = True
            continue
        m_loc = _PT_LOC.match(line)
        if m_loc:
            cur["frames"].append((m_loc["file"], int(m_loc["line"])))
    close()
    for f in out:
        f.assertion = bool(f.assertion) and not f.collection
        bare = f.ids[0]
        for prefix in ("ERROR at setup of ", "ERROR at teardown of "):
            if bare.startswith(prefix):  # a prefix only: a parameter id may hold this text
                bare = bare[len(prefix):]
                break
        # The section header is a bare name (`T.test_a`, or just `test_x` for a module-level
        # function), so it is not an identity. Its identity is the FAILED-summary node id whose
        # tail is that name and whose file appears in the section's own traceback frames.
        f.ids = _pair_nodes(_segs(bare), f.frames, [n for n, _msg in nodes])
    if not out:
        for node, msg in nodes:
            out.append(Failure([node], [], bool(re.match(r"(assert\b|AssertionError)", msg)), note=msg))
    for note in summary_blockers:
        out.append(Failure([], [], False, True, note))
    return out


def _final_green(text: str) -> bool:
    """True when the LAST runner result line is green (unittest OK, or a pytest bar with no failed/error)."""
    last = None
    for m in _RESULT_LINE.finditer(text):
        last = m.group(0).strip()
    if last is None:
        return False
    if last.startswith("OK"):
        return True
    return last.startswith("=") and not re.search(r"\b(?:failed|errors?)\b", last)


def parse_run(text: str) -> Parsed:
    failures = _parse_unittest(text) + _parse_pytest(text)
    return Parsed(failures=failures, red=bool(_RED_MARK.search(text)) or bool(failures),
                  green=bool(_GREEN_MARK.search(text)))


def _matches(failure: Failure, expected: str) -> bool:
    site = _EXPECT_SITE.match(expected)
    if site:
        want_file, want_line = _norm_path(site["file"]), int(site["line"])
        return any(line == want_line and _ends_with(_norm_path(file), want_file)
                   for file, line in failure.frames)
    want = _segs(expected)
    return any(_ends_with(_segs(i), want) for i in failure.ids)


def verdict(output: str, expected: str) -> tuple[str, str]:
    """Return (PASS|REFUSED, reason). PASS only when `expected` is an assertion that failed."""
    if not expected or not expected.strip():
        return REFUSED, "no expected assertion given (file:line or test id): a red suite proves nothing alone"
    expected = expected.strip()
    if _final_green(output):
        return REFUSED, ("the run is green (final result line): failure-looking text above it was test output, "
                         "so no assertion failed")
    run = parse_run(output)
    if not run.failures and not run.red:
        if run.green:
            return REFUSED, "the run is green: the mutation was not detected by any assertion"
        return REFUSED, "unrecognized output: no unittest or pytest result found, so nothing is proven"
    if not run.failures:
        return REFUSED, "the run is red but names no failing assertion (no FAIL/FAILED block with a location)"
    broken = [f for f in run.failures if f.collection]
    if broken:
        return REFUSED, ("collection or import error in the run (" + broken[0].note[:80]
                         + "): red for a reason that is not the assertion")
    hit = [f for f in run.failures if _matches(f, expected)]
    if not _EXPECT_SITE.match(expected):
        named = sorted({i for f in hit for i in f.ids})
        if len(named) > 1:
            return REFUSED, (f"{expected} is ambiguous: it matches {len(named)} failing tests ("
                             + ", ".join(named[:4]) + "); name the class")
    if any(f.assertion for f in hit):
        return PASS, f"assertion failed at {expected}"
    if hit:
        return REFUSED, f"{expected} raised a non-assertion error ({hit[0].note[:80]}): the mutation did not fail the assertion"
    others = sorted({f.ids[0] if f.ids else f.note for f in run.failures})
    return REFUSED, f"the run is red but not at {expected}; it failed at: " + ", ".join(others[:5])


def probe_discriminates(bug_value: float, fix_value: float, window: tuple[float, float]) -> tuple[bool, str]:
    """True only when the FIX reading is inside the closed window and the BUG reading is outside."""
    try:
        lo, hi = window
        bug_value, fix_value, lo, hi = float(bug_value), float(fix_value), float(lo), float(hi)
    except (TypeError, ValueError, OverflowError):
        return False, "probe readings and window must be numbers"
    if any(math.isnan(v) for v in (bug_value, fix_value, lo, hi)):
        return False, "NaN reading or window bound"
    if lo > hi:
        return False, f"empty window ({lo} > {hi})"
    bug_in, fix_in = lo <= bug_value <= hi, lo <= fix_value <= hi
    if bug_in and fix_in:
        return False, f"bug reading {bug_value} and fix reading {fix_value} both sit inside [{lo}, {hi}]: the assertion passes under both"
    if not bug_in and not fix_in:
        return False, f"neither reading sits inside [{lo}, {hi}]: the assertion fails under the fix"
    if bug_in:
        return False, f"only the bug reading {bug_value} sits inside [{lo}, {hi}]: the assertion passes under the bug and fails under the fix"
    return True, f"fix reading {fix_value} inside [{lo}, {hi}], bug reading {bug_value} outside"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verdict")
    v.add_argument("--output", required=True, help="saved unittest or pytest output of the mutation run")
    v.add_argument("--expect", required=True, help="file:line or test id of the intended assertion")
    p = sub.add_parser("probe")
    p.add_argument("--bug", type=float, required=True)
    p.add_argument("--fix", type=float, required=True)
    p.add_argument("--window", type=float, nargs=2, required=True, metavar=("LO", "HI"))
    args = ap.parse_args(argv)
    if args.cmd == "verdict":
        try:
            text = Path(args.output).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"cannot read --output {args.output!r}: {type(exc).__name__}: {exc.strerror or exc}", file=sys.stderr)
            return 2
        status, reason = verdict(text, args.expect)
    else:
        ok, reason = probe_discriminates(args.bug, args.fix, tuple(args.window))
        status = PASS if ok else REFUSED
    print(f"{status} {reason}")
    return 0 if status == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
