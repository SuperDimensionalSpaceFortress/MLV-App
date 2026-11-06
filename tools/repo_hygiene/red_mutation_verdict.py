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

NARROWED PROMISE (r4 hub ruling; three adversary passes found 8, 9 and 6 new forged-text cases, so more
instance parsing does not converge): for any input it cannot classify with certainty the checker answers
REFUSED with a reason that starts ``COULD-NOT-CHECK (<typed reason>)``. That is the existing non-zero refusal
(exit 1): a COULD-NOT-CHECK is NOT a pass, and it is never turned into a guessed classification. It refuses:

* ``chained``: output holding a chained-exception marker (``During handling of the above exception`` /
  ``The above exception was the direct cause``). Which exception a chain "is" is not decided here.
* ``captured``: output holding a Captured block (pytest ``--- Captured stdout call ---`` and its relatives,
  unittest ``-b`` ``Stdout:``/``Stderr:``). What follows is the test's own text, which can say anything.
* ``separator`` / ``empty``: a unittest method name holding ``.`` ``/`` ``\\`` ``:``, an identity (run or
  expectation) with an empty segment (``test_s..T.test_a``, a trailing ``.`` or ``::``), or any id that does
  not split into exactly its own segments.
* ``line number``: a ``file:line`` expectation or a traceback frame whose line number has more than 9 digits.
* ``failure blocks`` / ``sections`` / ``short test summary``: the runner's own failed-or-errored count
  (``FAILED (failures=1, errors=1)``, ``2 failed``) disagrees with the failure blocks, sections or summary lines
  that could be read, or a short-summary line starts with no summary word. A header this parser cannot read (a
  test name holding a newline) would otherwise drop its failure, an ImportError included.
* ``number``: a probe reading or window bound no float can hold (an integer or ``1e400`` spelled out), or an
  infinite reading. A spelled ``inf`` window bound is a real unbounded window and is accepted.
* ``internal-error``: any other exception while reading the text. Never an uncaught traceback.

A refusal is checked BEFORE the older reasons and the older reason is appended (``; also: ...``), so a
refusal that was already a refusal stays one. Only a PASS can be changed by this section, and only to REFUSED.

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
- Whitespace inside an id is significant (`` a/t.py`` is not ``a/t.py``; ``test one`` is not ``test``), and the
  expectation is never stripped: `` a/t.py:5`` and ``a/t.py:5\\n`` match nothing.
- It reads text, so text can be forged. Not defended: an exception message that itself prints a complete
  unittest block header (rule of ``=``, ``FAIL:``/``ERROR:`` line, rule of ``-``); a pytest test whose
  FUNCTION NAME holds `` - `` (set through ``globals()``), which a ``--tb=no`` summary line cannot tell from a
  message; a directory name holding ``[`` (read as the start of a parameter id). An ``ImportError`` named
  anywhere in a ``FAILED``/``ERROR`` summary message, or a ``FAILED`` message that is an ``ExceptionGroup``
  (its nested errors are not shown with ``--tb=no``), blocks PASS even when an assertion message only quotes it.
- Test output is not runner output: unittest frames and the exception line are read only from inside a real
  ``Traceback (most recent call last):`` block, and a pytest section ignores everything after its
  ``Captured ... `` divider and any section or ``====`` rule whose width differs from the run's own. A test
  that prints a rule of exactly the run's width, or a whole forged traceback with its chaining line, is not
  defended. Every pytest ``ERROR`` summary line blocks PASS, because a class-level collection error and a
  setup error look the same there. A pytest section is judged by its ``FAILED`` summary message when there is
  one (the runner's own account of the exception type).
- unittest says ``FAIL`` for any ``failureException``, so an assertion is credited only when the exception
  printed for the LAST traceback is ``AssertionError``; pytest ``E`` lines and summary messages must start with
  ``assert`` or ``AssertionError`` on a word boundary.
- A pytest failure section is tied to its node id by test name AND by the file in its
  traceback frames. With no ``FAILED`` summary line (``-rN``) the section has no identity,
  so only ``file:line`` can match it.
- Other tests that also failed do not block a PASS; a collection or import error anywhere
  in the run does (the run is red for a reason that is not the assertion).
- It does not know whether the assertion is the RIGHT one: choosing the expected site is
  the author's claim, and this check only binds the run to it.
- INNERMOST FRAME (follow-up card RMV-INNERMOST-FRAME, not promised here): a ``file:line``
  match on ANY frame means an ``assert`` inside production code reached from the test can read
  as PASS (a chained exception is refused, see NARROWED PROMISE). The checker does not require
  the expected site to be the frame that raised.
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
_UT_KIND = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<rest>.+?)\s*$")
_DASHES = re.compile(r"^-{70}\s*$")
_PT_CAPTURED = re.compile(r"^-{3,} Captured .+ -{3,}\s*$")
_CHAIN = ("The above exception was the direct cause of the following exception:",
          "During handling of the above exception, another exception occurred:")
_PT_SECTION = re.compile(r"^_{2,} (?P<name>.+?) _{2,}\s*$")
_PT_TOP = re.compile(r"^={3,} (?P<title>.+?) ={3,}\s*$")
_PT_LOC = re.compile(r"^(?P<file>[^\s:][^\n]*?\.py):(?P<line>\d+):(?: (?P<rest>.*))?$")
# A leading "| " / "+-" run is the gutter an ExceptionGroup traceback puts in front of nested errors.
_IMPORT_ERR = re.compile(r"^[\s|+\-]*(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError)\b", re.M)
_IMPORT_ANYWHERE = re.compile(r"(?:^|[^\w])(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError)\b")
_EXC_GROUP = re.compile(r"^(?:[\w.]+\.)?(?:Base)?ExceptionGroup\b")
# An assertion message starts with `assert`, or is exactly the AssertionError class (word-bounded: a class
# named AssertionErrorImpostor is not one), or pytest's `Failed:`.
_ASSERT_HEAD = re.compile(r"(?:assert\b|AssertionError(?::|\s*$)|Failed:)")
_EXPECT_SITE = re.compile(r"^(?P<file>.+\.\w+):(?P<line>\d+)\Z")
_RED_MARK = re.compile(r"^FAILED \(|^FAILED |\b\d+ (?:failed|errors?)\b|^Interrupted:", re.M)
_GREEN_MARK = re.compile(r"^OK\b|\b\d+ passed\b", re.M)
_RESULT_LINE = re.compile(r"^(?:OK\b.*|FAILED \(.*\)|=+ .*\bin \d[\d.]*s\b.*=+)\s*$", re.M)


_CAPTURED_ANY = re.compile(r"^\s*-{2,}\s*Captured\b|^(?:Stdout|Stderr):\s*$", re.M)
_INT_TEXT = re.compile(r"\s*[+-]?\d+\s*")
_INF_TEXT = re.compile(r"\s*[+-]?inf(?:inity)?\s*", re.I)
_MAX_LINE_DIGITS = 9


class _Unclassifiable(Exception):
    """The text holds something this checker will not classify; verdict() turns it into COULD-NOT-CHECK."""


def _line_no(digits: str) -> int:
    if len(digits) > _MAX_LINE_DIGITS:
        raise _Unclassifiable(f"line number of {len(digits)} digits is not a line of a real file")
    return int(digits)


def _empty_segment(text: str) -> bool:
    """True when an id has an empty segment (`a..b`, `a.`, `a::`, `::a`, `a//b`): nothing real prints one, so an
    id with one is never matched, only refused. A leading `/` (an absolute path) is not an empty segment."""
    j = text.find("[")
    head = text if j < 0 else text[:j]
    dotted = "::" not in head  # `file.py::Class::name` names are not dotted; a unittest `module.Class.name` is
    if not dotted:
        parts = head.split("::")
        if any(not p for p in parts):
            return True
        head = parts[0]
    if "/" in head or "\\" in head:
        segs = head.replace("\\", "/").split("/")
        return any(not p for p in (segs[1:] if segs and not segs[0] else segs))
    return dotted and any(not p for p in head.split("."))


@dataclass
class Failure:
    ids: list[str]
    frames: list[tuple[str, int]]
    assertion: bool
    collection: bool = False
    note: str = ""
    odd: str = ""  # set when the identity cannot be told with certainty: verdict() refuses the whole run


@dataclass
class Parsed:
    failures: list[Failure] = field(default_factory=list)
    red: bool = False
    green: bool = False
    mismatch: str = ""  # the runner's own failure count disagrees with what was readable: see _count_mismatch


def _norm_path(text: str) -> list[str]:
    """Path segments of `text`. A trailing pytest parameter id (from the first `[`) stays inside the last
    segment, so `/` and `\\` in it never split."""
    j = text.find("[")
    head, tail = (text, "") if j < 0 else (text[:j], text[j:])
    parts = [p for p in head.replace("\\", "/").split("/") if p and p != "."]
    if tail:
        if head.endswith(("/", "\\")) or not parts:
            parts.append(tail)
        else:
            parts[-1] += tail
    return parts


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` before the pytest parameter id. A node's names are identifiers, so the FIRST `[`
    starts the parameter id and it runs to the end of the text; whatever it holds (`]`, `::`, `/`, `.`,
    unbalanced brackets) stays inside the last part. No bracket counting: user text cannot move a boundary."""
    j = text.find("[")
    head, tail = (text, "") if j < 0 else (text[:j], text[j:])
    parts = head.split(sep)
    parts[-1] += tail
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
    if len(_norm_path(text)) > 1:
        path = _norm_path(text)
        if path and path[-1].endswith(".py") and len(path[-1]) > 3:
            path[-1] = path[-1][:-3]
        return path
    return _split_top(text, ".")


def _parse_summary_line(line: str, word: str) -> tuple[str, str, bool] | None:
    """`FAILED <node> - <msg>` / `ERROR <node> - <msg>` -> (node, msg, ambiguous). Without a parameter id the
    node ends at the first ' - '. With one, the node ends at a ' - ' that follows a `]`; when more than one
    such separator exists the split cannot be told from a parameter id that contains it, so the line is
    flagged ambiguous."""
    if not line.startswith(word + " "):
        return None
    rest = line[len(word) + 1:].rstrip()
    seps = [i for i in range(len(rest)) if rest.startswith(" - ", i)]
    if not seps:
        return rest, "", False
    param = rest.find("[")
    if param < 0 or seps[0] < param:
        return rest[:seps[0]], rest[seps[0] + 3:], False
    after = [i for i in seps if i > param and rest[i - 1] == "]"]
    if not after:
        return rest, "", False
    return rest[:after[0]], rest[after[0] + 3:], len(after) > 1


def _ends_with(whole: list[str], tail: list[str]) -> bool:
    """True when `tail` is a non-empty segment-boundary suffix of `whole`. One-directional on purpose:
    an expectation longer than what the run printed is never a match."""
    return bool(whole) and bool(tail) and len(tail) <= len(whole) and whole[-len(tail):] == tail


def _ut_identity(rest: str) -> tuple[str, str] | None:
    """`name (module.Class.name)` (3.11+) with an optional ` (k=v)` subtest suffix -> (name, full id).
    The identity is the parenthesised group that ENDS WITH `.name`, so parentheses inside a method name
    cannot be read as the module part. Older Pythons print `name (module.Class)`, handled by the caller."""
    for i in (m.start() for m in re.finditer(r" \(", rest)):
        name, tail = rest[:i], rest[i + 2:]
        for j in (m.start() for m in re.finditer(r"\)", tail)):
            ident, after = tail[:j], tail[j + 1:]
            if ident.endswith("." + name) and (after == "" or re.fullmatch(r" \(.*\)", after)):
                return name, ident
    return None


def _ut_walk(block: list[str]) -> tuple[list[tuple[str, int]], str]:
    """(frames, exception line) of the REAL tracebacks in a unittest block. A traceback is entered only at
    `Traceback (most recent call last):` (the first, or one that follows a chaining line); inside it the
    indented `File "...", line N` lines are frames and the first unindented line is the exception line, after
    which message text (which may print anything, a forged File line included) is ignored."""
    frames: list[tuple[str, int]] = []
    exc, in_tb, expect = "", False, True
    for ln in block:
        if not in_tb:
            if ln.rstrip() == "Traceback (most recent call last):" and expect:
                in_tb, exc, expect = True, "", False
            elif ln.strip() in _CHAIN:
                expect = True
            continue
        if not ln.strip():
            continue
        if ln[:1].isspace():
            m = _FRAME.match(ln)
            if m:
                frames.append((m["file"], _line_no(m["line"])))
            continue
        exc, in_tb = ln, False
    return frames, exc


def _parse_unittest(text: str) -> list[Failure]:
    out: list[Failure] = []
    blocks: list[list[str]] = []
    current: list[str] | None = None
    lines = text.splitlines()
    for n, line in enumerate(lines):
        # A block starts only at the full three-line header unittest prints: a rule of `=`, `FAIL:`/`ERROR:`,
        # a rule of `-`. A bare `====` inside an exception message is message text, not a block boundary.
        if (_EQ_RULE.match(line) and n + 2 < len(lines) and _UT_KIND.match(lines[n + 1])
                and _DASHES.match(lines[n + 2])):
            current = []
            blocks.append(current)
        elif current is not None:
            current.append(line)
    for block in blocks:
        body = [ln for ln in block if ln.strip()]
        if not body:
            continue
        head = _UT_KIND.match(body[0])
        if not head:
            continue
        frames, exc_line = _ut_walk(block)
        joined = "\n".join(block)
        collection = ("_FailedTest" in body[0] or "unittest/loader.py" in joined.replace("\\", "/")
                      or bool(_IMPORT_ERR.search(joined)))
        got = _ut_identity(head["rest"])
        if got:
            name, full = got
        else:  # older Pythons: `name (module.Class)`
            # The module part is the LAST parenthesised dotted group; an earlier group belongs to the name.
            groups = list(re.finditer(r" \(([A-Za-z_][\w.]*)\)", head["rest"]))
            dotted = [g for g in groups if "." in g.group(1)]
            pick = (dotted or groups or [None])[-1]
            name = head["rest"][:pick.start()] if pick else head["rest"]
            full = f"{pick.group(1)}.{name}" if pick else name
        ids = [] if "unittest.loader" in full else [full]
        odd = ""
        if ids and any(ch in name for ch in "./\\:"):
            odd = f"separator in the method name {name[:40]!r}: its identity would split into invented segments"
        elif ids and (_empty_segment(full) or any(tok in full for tok in ("/", "\\", "::"))):
            odd = f"empty or foreign segment in the test identity {full[:60]!r}"
        # FAIL is only unittest's word for "failureException was raised", and a test may set that to any
        # class: the assertion claim rests on the exception actually printed.
        is_assertion = bool(re.match(r"AssertionError(?::|\s*$)", exc_line))
        out.append(Failure(ids, frames, assertion=head["kind"] == "FAIL" and is_assertion and not collection,
                           collection=collection, note=body[0].strip(), odd=odd))
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


_PT_SUMMARY_WORDS = ("FAILED", "ERROR", "SKIPPED", "XFAIL", "XPASS", "PASSED")


def _parse_pytest(text: str, stats: dict | None = None) -> list[Failure]:
    """`stats` (optional) receives what the text held that the runner's own count can be checked against:
    `sections` (failure/error sections), `summary` (FAILED/ERROR summary lines), `stray` (a summary-section line
    that starts with no known summary word, e.g. the continuation of a multi-line node id)."""
    stats = stats if stats is not None else {}
    stats.update(sections=0, summary=0, stray=0)
    out: list[Failure] = []
    nodes: list[tuple[str, str]] = []
    summary_blockers: list[str] = []
    odd_notes: list[str] = []
    top = ""
    cur: dict | None = None
    captured = False
    width = 0  # length of the run's first `====` rule: every real rule of that kind has this width (+-1)
    sec_width = 0  # the same for the `____ name ____` section headers

    def close() -> None:
        nonlocal cur
        if cur is not None:
            stats["sections"] += 1
            out.append(Failure([cur["name"]], cur["frames"], cur["assertion"], cur["collection"], cur["name"]))
        cur = None

    # Width is checked only INSIDE a captured block, the one place a test can print: there a real rule has the
    # width of the run's first rule of its kind (+-1); elsewhere any rule is accepted, so hand-trimmed logs work.
    def real_rule(line: str) -> bool:
        return not captured or not width or abs(len(line.rstrip()) - width) <= 1

    def real_section(line: str) -> bool:
        return not captured or not sec_width or abs(len(line.rstrip()) - sec_width) <= 1

    for line in text.splitlines():
        m_top = _PT_TOP.match(line)
        if m_top and real_rule(line):
            width = width or len(line.rstrip())
            close()
            captured = False
            top = m_top["title"].strip()
            continue
        if top.startswith("short test summary"):
            if line.strip() and not line.startswith(tuple(w + " " for w in _PT_SUMMARY_WORDS)) \
                    and line.strip() not in _PT_SUMMARY_WORDS:
                stats["stray"] += 1
            for word in ("FAILED", "ERROR"):
                got = _parse_summary_line(line, word)
                if not got:
                    continue
                stats["summary"] += 1
                node, msg, ambiguous = got
                if word == "FAILED":
                    nodes.append((node, msg))
                    if _empty_segment(node):
                        odd_notes.append(f"empty segment in the test identity {node[:60]!r}")
                if ambiguous:
                    summary_blockers.append(f"ambiguous summary line {word} {node[:60]}")
                elif _IMPORT_ANYWHERE.search(msg) or _EXC_GROUP.match(msg):
                    # An import error named anywhere in the message, or an ExceptionGroup whose nested
                    # errors a --tb=no line does not show, blocks like a collection error.
                    summary_blockers.append(f"{word} {node[:60]} - {msg[:40]}")
                elif word == "ERROR":
                    # A collection error of a class or module looks like this; so does a setup error.
                    # They cannot be told apart from the line, so every ERROR line blocks.
                    summary_blockers.append(f"ERROR {node[:60]} - {msg[:40]}")
            continue
        if top not in ("FAILURES", "ERRORS"):
            continue
        m_sec = _PT_SECTION.match(line)
        if m_sec and real_section(line):
            sec_width = sec_width or len(line.rstrip())
            close()
            captured = False
            name = m_sec["name"]
            cur = {"name": name, "frames": [], "assertion": None,
                   "collection": name.startswith("ERROR collecting")}
            continue
        if cur is None:
            continue
        if _IMPORT_ERR.match(line):  # nested ExceptionGroup errors print without an `E` prefix
            cur["collection"] = True
        if _PT_CAPTURED.match(line):
            captured = True  # what follows is the test's own output, not pytest's traceback
            continue
        if captured:
            continue
        if line.startswith("E") and line[1:2] in (" ", "\t"):
            first = line[1:].strip()
            if cur["assertion"] is None:
                cur["assertion"] = bool(_ASSERT_HEAD.match(first))
            if _IMPORT_ERR.match(first):  # any E line: a chained exception counts too
                cur["collection"] = True
            continue
        m_loc = _PT_LOC.match(line)
        if m_loc:
            cur["frames"].append((m_loc["file"], _line_no(m_loc["line"])))
    close()
    msgs = dict(nodes)
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
        known = [msgs[i] for i in f.ids if msgs.get(i)]
        if known and len(known) == len(f.ids):
            # The summary line is pytest's own account of the exception TYPE; test output printed inside a
            # section cannot change it.
            f.assertion = f.assertion and all(_ASSERT_HEAD.match(m) for m in known)
    paired = {i for f in out for i in f.ids}
    for node, msg in nodes:  # a failing node no section could be tied to still counts, by its summary line
        if node not in paired:
            out.append(Failure([node], [], bool(_ASSERT_HEAD.match(msg)), note=msg))
    for note in summary_blockers:
        out.append(Failure([], [], False, True, note))
    for note in odd_notes:
        out.append(Failure([], [], False, note=note, odd=note))
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


_UT_COUNTS = re.compile(r"[(,] ?(failures|errors)=(\d+)")
_UT_RAN = re.compile(r"^Ran \d+ tests? in ", re.M)


def _count_mismatch(text: str, ut_blocks: int, stats: dict) -> str:
    """The runner states how many tests failed; every one must have been recognized as a block, section or summary
    line. A header this parser could not read (a multi-line test name) would otherwise drop its failure, an import
    error included, without a trace. "" when the counts agree or the text states none."""
    last = None
    for m in _RESULT_LINE.finditer(text):
        last = m.group(0).strip()
    if last is None:
        return ""
    if last.startswith("FAILED (") and _UT_RAN.search(text):
        want = sum(int(n) for _kind, n in _UT_COUNTS.findall(last))
        if want and want != ut_blocks:
            return f"the runner counts {want} failing unittest tests but {ut_blocks} failure blocks were readable"
    elif last.startswith("="):
        want = sum(int(n) for n in re.findall(r"\b(\d+) (?:failed|errors?)\b", last))
        if want:
            for what in ("sections", "summary"):
                if stats[what] and stats[what] != want:
                    return f"the runner counts {want} failed or errored tests but {stats[what]} {what} were readable"
    if stats["stray"]:
        return "a line in the short test summary starts with no summary word (a multi-line test id?)"
    return ""


def parse_run(text: str) -> Parsed:
    stats: dict = {}
    ut = _parse_unittest(text)
    failures = ut + _parse_pytest(text, stats)
    return Parsed(failures=failures, red=bool(_RED_MARK.search(text)) or bool(failures),
                  green=bool(_GREEN_MARK.search(text)), mismatch=_count_mismatch(text, len(ut), stats))


def _matches(failure: Failure, expected: str) -> bool:
    site = _EXPECT_SITE.match(expected)
    if site:
        want_file, want_line = _norm_path(site["file"]), _line_no(site["line"])
        return any(line == want_line and _ends_with(_norm_path(file), want_file)
                   for file, line in failure.frames)
    want = _segs(expected)
    return any(_ends_with(_segs(i), want) for i in failure.ids)


def _precheck(output: str, expected: str) -> str:
    """The typed reason this text will not be classified, or "" (see NARROWED PROMISE in the module docstring)."""
    if not isinstance(output, str) or not isinstance(expected, str) or not expected.strip():
        return ""  # the plain checks below refuse these with their own reasons
    if any(ln.strip() in _CHAIN for ln in output.splitlines()):
        return "chained exception in the output: which exception the run reports is not decided here"
    if _CAPTURED_ANY.search(output):
        return "captured output in the run: the test's own text follows the divider and can say anything"
    site = _EXPECT_SITE.match(expected)
    if site:
        _line_no(site["line"])
    elif _empty_segment(expected):
        return "empty segment in the expected test identity (`..`, a trailing `.` or `::`): no such id exists"
    return parse_run(output).mismatch


def verdict(output: str, expected: str) -> tuple[str, str]:
    """Return (PASS|REFUSED, reason). PASS only when `expected` is an assertion that failed. Text it cannot classify
    with certainty is REFUSED with a reason that starts ``COULD-NOT-CHECK`` (NARROWED PROMISE, module docstring)."""
    pre, status, reason = "", REFUSED, ""
    try:
        pre = _precheck(output, expected)
        status, reason = _verdict(output, expected)
    except _Unclassifiable as exc:
        return REFUSED, f"COULD-NOT-CHECK ({exc})"
    except Exception as exc:  # noqa: BLE001 - the promise is a refusal, never an uncaught traceback
        return REFUSED, f"COULD-NOT-CHECK (internal-error {type(exc).__name__} while reading the text)"
    if pre:
        return REFUSED, f"COULD-NOT-CHECK ({pre})" + (f"; also: {reason}" if status == REFUSED else "")
    return status, reason


def _verdict(output: str, expected: str) -> tuple[str, str]:
    if not expected or not expected.strip():
        return REFUSED, "no expected assertion given (file:line or test id): a red suite proves nothing alone"
    if _final_green(output):
        return REFUSED, ("the run is green (final result line): failure-looking text above it was test output, "
                         "so no assertion failed")
    run = parse_run(output)
    for f in run.failures:
        if f.odd:
            raise _Unclassifiable(f.odd)
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


def _num(v: object) -> int | float:
    """An int stays an int: Python compares int and float exactly, while float(2**53 + 1) would round it."""
    if not isinstance(v, (int, float)):
        try:
            v = int(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            if isinstance(v, str) and _INT_TEXT.fullmatch(v):
                raise OverflowError("integer text beyond the int() digit limit") from None
            text = v
            v = float(v)  # type: ignore[arg-type]
            if math.isinf(v) and not _INF_TEXT.fullmatch(str(text)):
                raise OverflowError("a number spelled out beyond what a float can hold (read as inf)")
    if isinstance(v, int):
        float(v)  # OverflowError for an int no double can hold: not a real reading (callers refuse it)
    return v


def _cli_num(v: str) -> object:
    """argparse type: a number, or (for an integer no double can hold) the text itself, so that
    probe_discriminates refuses it by name instead of argparse raising OverflowError."""
    try:
        return _num(v)
    except OverflowError:
        return v


def probe_discriminates(bug_value: float, fix_value: float, window: tuple[float, float]) -> tuple[bool, str]:
    """True only when the FIX reading is inside the closed window and the BUG reading is outside."""
    try:
        lo, hi = window
        bug_value, fix_value, lo, hi = _num(bug_value), _num(fix_value), _num(lo), _num(hi)
    except (TypeError, ValueError, OverflowError):
        return False, "COULD-NOT-CHECK (number: probe readings and window must be numbers a float can hold)"
    if any(isinstance(v, float) and math.isnan(v) for v in (bug_value, fix_value, lo, hi)):
        return False, "NaN reading or window bound"
    if any(isinstance(v, float) and math.isinf(v) for v in (bug_value, fix_value)):
        return False, "COULD-NOT-CHECK (number: a probe reading must be finite; only a window bound may be infinite)"
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
    p.add_argument("--bug", type=_cli_num, required=True)
    p.add_argument("--fix", type=_cli_num, required=True)
    p.add_argument("--window", type=_cli_num, nargs=2, required=True, metavar=("LO", "HI"))
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # an expected id with a non-cp1252 letter must not crash the print
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
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
