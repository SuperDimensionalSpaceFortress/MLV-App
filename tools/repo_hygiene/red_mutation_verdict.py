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
* ``failure blocks`` / ``sections``: the runner's own failed-or-errored count (``FAILED (failures=1, errors=1)``,
  ``2 failed``; a stated ``0`` is a count too) disagrees with the unittest failure blocks or the pytest failure sections that could be read. A
  header this parser cannot read (a test name holding a newline) would otherwise drop its failure, an ImportError
  included.
* ``short-summary line`` (an ALLOWLIST since the r7 hub ruling; r5 and r6 each found 5-6 new forged shapes of one class,
  so the denylist of shapes was replaced): PASS needs a pytest short summary made ONLY of the shapes pytest prints
  under ``-rA``, and anything else is COULD-NOT-CHECK. Every line is exactly one of ``FAILED``/``ERROR <ascii
  file.ext>::<name> - <message>`` (single line, ``WORD node`` of at most 74 characters, which pytest cuts or drops the
  message of at its 80-column fallback width but not on a ``CI`` runner, so the line is judged before its message is
  read; a non-empty message that is not cut to ``...`` and holds no ``<file>::<name>``; a module-level ``ERROR
  <file.ext> - <message>`` is the one shape without ``::name``), ``PASSED <id>`` alone, ``SKIPPED [n] file:line:
  reason``, ``XFAIL``/``XPASS <id>``. The lines come in the ``-rA`` order (PASSED, SKIPPED, XFAIL, XPASS, then
  ERROR/FAILED), with no blank line, no other line and no rule line between the header and the final result bar, and
  nothing after that bar (a summary with no closing bar is refused, and so is an id with an empty segment in any
  line). Their counts equal the runner's own, and this holds with no summary section too (``-rN``): a bar that counts
  a pass while no PASSED line lists it is COULD-NOT-CHECK, so ``-rN`` is readable only for a run with no passes: FAILED+ERROR lines the failed+errored count, and the
  PASSED/SKIPPED/XFAIL/XPASS lines the passed/skipped/xfailed/xpassed counts, ZERO INCLUDED, so a default (``-rfE``)
  run that counts a pass but lists none is COULD-NOT-CHECK: a forged ``PASSED`` continuation of a multi-line id could
  stand in for the real passes that run omits. Run the mutation with ``-rA``. A multi-line, non-ASCII, over-long or
  otherwise unreadable id is none of these shapes; it is not parsed.
* ``exception type``: a failing test (any, not only the expected one) whose printed exception class is not EXACTLY a name
  on the fixed list ``_EXC_NAMES`` (the builtin exceptions less ImportError, ModuleNotFoundError, the ExceptionGroups and
  the warnings; a dotted ``mod.Name`` is never on it), an assertion or pytest's ``Failed:``. A run prints only the class
  NAME, and a class of any name may subclass ``ImportError`` (or hold one in an ``ExceptionGroup``), so a custom, aliased
  or subclass name cannot be called "not an import failure". Run the mutation so the only failures are assertions and
  builtin exceptions, or confirm by hand.
* ``number`` (an ALLOWLIST since r7): a probe reading or window bound is an ``int``, a ``float``, or a ``str`` of ASCII
  digits, a sign, a point and an exponent (a spelled ``inf``/``nan`` too). ``Decimal``, ``Fraction``, numpy-like scalars,
  a ``bool``, any other type, and text with another script's digits or a ``_`` are COULD-NOT-CHECK BEFORE any conversion,
  because their magnitude is not inspected here. Within the allowlist, a number no float can hold (an integer or
  ``1e400`` spelled out, or ``1e-400`` which a float would read as ``0.0``) or an infinite reading is refused. The corpus
  guard keeps each number's spelling (``json.loads(parse_float=str)``) so a recorded ``1e-400`` is not read as ``0.0``
  before this check sees it. A spelled ``inf`` window bound is a real unbounded window and is accepted.
* ``line framing`` (r9 hub ruling, a STRUCTURE): the text is framed into lines ONCE, at entry, and only ``\\n`` and
  ``\\r\\n`` end a line. Any other boundary ``str.splitlines()`` knows (``\\r`` alone, ``\\x0b``, ``\\x0c``, ``\\x1c``-``\\x1e``,
  ``\\x85``, U+2028, U+2029) refuses the whole run, and the reason names the code point. Every reader (blocks, sections,
  short summary, terminal result, ``Ran`` footer, counts, the green check) reads that one list of lines. A text holding
  U+FFFD (what a lossy decode leaves) is refused too, and the CLI reads ``--output`` as strict UTF-8 with no newline
  translation (a file that is not UTF-8 is exit 2).
* ``runner``: one run has one runner. A unittest ``FAILED (...)`` trailer over pytest sections or summary lines, or a
  pytest bar over unittest blocks, is COULD-NOT-CHECK: the trailer's count does not cover the other runner's failures.
* ``pairing`` (r9 hub ruling, a STRUCTURE): every pytest ``FAILED`` line is one failure OCCURRENCE with its own exception
  account. A section takes the account of the one occurrence it pairs with; a node id printed more than once
  (``--keep-duplicates``), two node ids with one test name in files where one path is a suffix of the other
  (``a/test_s.py``, ``b/a/test_s.py``), or a section that pairs with more than one occurrence is COULD-NOT-CHECK, naming
  the node ids. An occurrence that several sections pair with (an inherited test, whose frames all sit in the base
  class's file) is none of theirs: each keeps its own ``E`` line, and the occurrence counts by its own summary line. No
  occurrence's message is given to another section.
* ``internal-error``: any other exception while reading the text. Never an uncaught traceback.

A refusal is checked BEFORE the older reasons and the older reason is appended (``; also: ...``), so a
refusal that was already a refusal stays one. Only a PASS can be changed by this section, and only to REFUSED.
The refusal is the PRIMARY reason and the appended text is a secondary diagnostic: ``classify()`` returns them as
separate fields (``Verdict.reason``, ``Verdict.also``), and a check that pins why a case was refused matches the primary
reason only (r10, Dot R8-T1). ``verdict()`` joins them into one text for display.

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
- It reads text, so text can be forged. r7 TERMINATION (hub ruling): an allowlisted shape is judged, anything outside
  it is refused, and a finding that only shows an input outside the allowlist being REFUSED is not a defect. Not
  defended, because the text is identical to what a genuine run prints: a test that forges its own node ids and
  whose run also hides the outcome it replaces (for example ``-rp`` listing a forged FAILED line while the real
  failure goes unlisted, which the counts, the order and the shapes cannot tell from a genuine ``-rpf`` run), and a
  class that sets ``__module__ = 'builtins'`` so that it prints unqualified (``AssertionError`` that is an
  ``ImportError``). Forgery shapes go to the P3 follow-up card, not to another round. Also not defended: an exception message that itself prints a complete
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
from fractions import Fraction
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
_IMPORT_ERR = re.compile(r"^[\s|+\-]*(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError)\b")
_IMPORT_ANYWHERE = re.compile(r"(?:^|[^\w])(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError)\b")
_EXC_GROUP = re.compile(r"^(?:[\w.]+\.)?(?:Base)?ExceptionGroup\b")
# An assertion message starts with `assert`, or is exactly the AssertionError class (word-bounded: a class
# named AssertionErrorImpostor is not one), or pytest's `Failed:`.
_ASSERT_HEAD = re.compile(r"(?:assert\b|AssertionError(?::|\s*$)|Failed:)")
_EXPECT_SITE = re.compile(r"^(?P<file>.+\.\w+):(?P<line>\d+)\Z")
# Every pattern below reads ONE framed line (see _frame): none is compiled multi-line, none searches the raw text.
_RED_MARK = re.compile(r"^FAILED \(|^FAILED |\b\d+ (?:failed|errors?)\b|^Interrupted:")
_GREEN_MARK = re.compile(r"^OK\b|\b\d+ passed\b")
_RESULT_LINE = re.compile(r"^(?:OK\b.*|FAILED \(.*\)|=+ .*\bin \d[\d.]*s\b.*=+)\s*$")


_CAPTURED_ANY = re.compile(r"^\s*-{2,}\s*Captured\b|^(?:Stdout|Stderr):\s*$")
# LINE FRAMING (r9 hub ruling): str.splitlines() also breaks a line at each of these, while a `^`/`$` anchor or a "\n"
# split does not, so two readers of one text could see two different runs (r8 A3: a final `OK` that one reader saw and
# the other did not). A text holding one is not framed at all: COULD-NOT-CHECK, naming the code point.
_FOREIGN_BREAKS = ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029", "\r")
_FOREIGN_BREAK = re.compile("\r(?!\n)|[\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]")
# NUMBER ALLOWLIST (r7 hub ruling): a reading or window bound is an int, a float, or ASCII text of the shapes below.
# Spelled out with [0-9] and re.ASCII so that no other script's digit (float() accepts them) and no `_` separator can slip in.
_INT_TEXT = re.compile(r"[ \t]*[+-]?[0-9]+[ \t]*")
_DEC_TEXT = re.compile(r"[ \t]*[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?[ \t]*")
_SPECIAL_TEXT = re.compile(r"[ \t]*[+-]?(?:inf(?:inity)?|nan)[ \t]*", re.I | re.A)
_MAX_LINE_DIGITS = 9

# EXCEPTION-NAME ALLOWLIST (r7 hub ruling): the printed class names this checker will read as "a builtin that is not
# an import failure". A literal list, not a look-up in ``builtins`` at run time: ImportError, ModuleNotFoundError and
# the ExceptionGroup types are deliberately absent (the first two are an import failure, the last may hold one), and
# so is every warning class. Any other name, dotted (``mod.Name``) or not, is a custom, aliased or subclass name.
_EXC_NAMES = frozenset((
    "ArithmeticError", "AttributeError", "BaseException", "BlockingIOError", "BrokenPipeError", "BufferError",
    "ChildProcessError", "ConnectionAbortedError", "ConnectionError", "ConnectionRefusedError", "ConnectionResetError",
    "EOFError", "Exception", "FileExistsError", "FileNotFoundError", "FloatingPointError", "GeneratorExit",
    "IndentationError", "IndexError", "InterruptedError", "IsADirectoryError", "KeyError", "KeyboardInterrupt",
    "LookupError", "MemoryError", "NameError", "NotADirectoryError", "NotImplementedError", "OSError", "OverflowError",
    "PermissionError", "ProcessLookupError", "RecursionError", "ReferenceError", "RuntimeError", "StopAsyncIteration",
    "StopIteration", "SyntaxError", "SystemError", "SystemExit", "TabError", "TimeoutError", "TypeError",
    "UnboundLocalError", "UnicodeDecodeError", "UnicodeEncodeError", "UnicodeError", "UnicodeTranslateError",
    "ValueError", "ZeroDivisionError",
))


class _Unclassifiable(Exception):
    """The text holds something this checker will not classify; verdict() turns it into COULD-NOT-CHECK."""


def _frame(text: str) -> list[str]:
    """THE one framing of the run's text into lines (r9 hub ruling). Only "\\n" and "\\r\\n" end a line; any other
    boundary str.splitlines() knows (see _FOREIGN_BREAKS) refuses the whole text. Every reader (unittest blocks, pytest
    sections and summary, the terminal result, the `Ran` footer, the counts, the green check, the precheck) consumes
    this list and never the raw text."""
    m = _FOREIGN_BREAK.search(text)
    if m:
        raise _Unclassifiable(f"line framing: the text holds U+{ord(m.group(0)):04X}, a line boundary other than \\n "
                              "and \\r\\n, which some readers would split at and others would not")
    if "\ufffd" in text:  # r9 adversary pass 2, A2: a lossy decode put it where bytes were lost; any name may have held it
        raise _Unclassifiable("decoding: the text holds U+FFFD, the replacement character a lossy decode puts where bytes "
                              "could not be read, so a file name or test id holding it is not the one the run printed")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":  # the newline that ends the last line starts no new line
        lines.pop()
    return lines


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
    exc: str = ""  # the exception line the runner printed for this failure ("TypeError: boom"), "" when none was read


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


_UT_HEADER_MAX = 4096  # characters of a `FAIL: ...` header that are read; a longer one (a huge subTest repr) is refused


def _ut_identities(rest: str) -> list[tuple[str, str]]:
    """EVERY reading of `name (module.Class.name)` (3.11+) with an optional ` (k=v)` subtest suffix -> [(name, full id)].
    A reading is a parenthesised group that ENDS WITH `.name`. A method name that itself holds ` (x.name)` gives
    two readings (r7 adversary A1: ``test_x (bogus.test_x)`` reads as the name ``test_x`` of module ``bogus``), and the
    caller refuses a header with more than one rather than choose. Older Pythons print `name (module.Class)`, handled by
    the caller."""
    found: list[tuple[str, str]] = []
    for i in (m.start() for m in re.finditer(r" \(", rest)):
        name, tail = rest[:i], rest[i + 2:]
        # Only a `)` preceded by `.name` can close a reading, so look for ".name)" itself instead of trying every `)`
        # (r8, opus r7 RMV-UT-IDENTITIES-SCALING-1: a large subTest repr made the old pair loop quadratic).
        needle = "." + name + ")"
        k = tail.find(needle)
        while k != -1:
            end = k + len(needle)
            ident, after = tail[:end - 1], tail[end:]
            if after == "" or re.fullmatch(r" \(.*\)", after):
                found.append((name, ident))
            k = tail.find(needle, k + 1)
    return found


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


def _parse_unittest(lines: list[str]) -> list[Failure]:
    out: list[Failure] = []
    blocks: list[list[str]] = []
    current: list[str] | None = None
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
        collection = ("_FailedTest" in body[0]
                      or any("unittest/loader.py" in ln.replace("\\", "/") or _IMPORT_ERR.match(ln) for ln in block))
        if len(head["rest"]) > _UT_HEADER_MAX:  # adversary A2: bounded work, and no real test name is this long
            raise _Unclassifiable(f"unittest header of {len(head['rest'])} characters is longer than {_UT_HEADER_MAX}: "
                                  "its test identity is not read")
        readings = _ut_identities(head["rest"])
        if readings:
            name, full = readings[0]
        else:  # older Pythons: `name (module.Class)`
            # The module part is the LAST parenthesised dotted group; an earlier group belongs to the name.
            groups = list(re.finditer(r" \(([A-Za-z_][\w.]*)\)", head["rest"]))
            dotted = [g for g in groups if "." in g.group(1)]
            pick = (dotted or groups or [None])[-1]
            name = head["rest"][:pick.start()] if pick else head["rest"]
            full = f"{pick.group(1)}.{name}" if pick else name
        ids = [] if "unittest.loader" in full else [full]
        odd = ""
        if len(readings) > 1:
            odd = f"ambiguous identity: the header {head['rest'][:60]!r} reads as {len(readings)} different test identities"
        elif ids and any(ch in name for ch in "./\\:"):
            odd = f"separator in the method name {name[:40]!r}: its identity would split into invented segments"
        elif ids and (_empty_segment(full) or any(tok in full for tok in ("/", "\\", "::"))):
            odd = f"empty or foreign segment in the test identity {full[:60]!r}"
        # FAIL is only unittest's word for "failureException was raised", and a test may set that to any
        # class: the assertion claim rests on the exception actually printed.
        is_assertion = bool(re.match(r"AssertionError(?::|\s*$)", exc_line))
        out.append(Failure(ids, frames, assertion=head["kind"] == "FAIL" and is_assertion and not collection,
                           collection=collection, note=body[0].strip(), odd=odd, exc=exc_line.strip()))
    return out


def _same_file(a: list[str], b: list[str]) -> bool:
    return _ends_with(a, b) or _ends_with(b, a)


def _pair_nodes(want: list[str], frames: list[tuple[str, int]], nodes: list[str]) -> list[int]:
    """Indices of the FAILED occurrences (`nodes`, in summary order) a section with test name `want` and traceback
    `frames` may be. More than one is an ambiguous pairing, which the caller refuses (r9 hub ruling)."""
    cands = [i for i, n in enumerate(nodes) if _ends_with(_segs(n), want)]
    if frames:
        def in_file(n: str, file: str) -> bool:
            return _same_file(_norm_path(_split_top(n, "::")[0]), _norm_path(file))

        first = [i for i in cands if in_file(nodes[i], frames[0][0])]  # the test function's own file
        return first or [i for i in cands if any(in_file(nodes[i], file) for file, _line in frames)]
    return cands if len(cands) == 1 else []


def _ambiguous_occurrences(nodes: list[str]) -> list[str]:
    """Why the FAILED occurrences cannot each be told apart by a section, or []: a node id printed more than once
    (``--keep-duplicates``), or two node ids with the same test name whose files `_same_file` relates (one path is a
    suffix of the other: ``a/test_s.py`` and ``b/a/test_s.py``). Linear in the number of lines: each path is looked up
    by its reversed segments, so a suffix is a prefix of the reversed path."""
    notes: list[str] = []
    seen: dict[str, int] = {}
    for node in nodes:
        seen[node] = seen.get(node, 0) + 1
    for node, k in seen.items():
        if k > 1:
            notes.append(f"pairing: the FAILED node id {node[:60]!r} occurs {k} times (--keep-duplicates?): which failure "
                         "section is which occurrence, and so which exception belongs to which, cannot be told")
    groups: dict[tuple[str, ...], dict[tuple[str, ...], str]] = {}
    for node in seen:
        parts = _split_top(node, "::")
        if not parts:
            continue
        group = groups.setdefault(tuple(parts[1:]), {})
        rev = tuple(reversed(_norm_path(parts[0])))
        if rev in group:  # the same file spelled two ways (`./a/t.py` and `a/t.py`)
            notes.append(f"pairing: FAILED {group[rev][:60]!r} and {node[:60]!r} name the same test in the same file")
            continue
        group[rev] = node
    for group in groups.values():
        for rev, node in group.items():
            for k in range(1, len(rev)):
                other = group.get(rev[:k])
                if other is not None:
                    notes.append(f"pairing: FAILED {other[:60]!r} and {node[:60]!r} share a test name and one file path "
                                 "is a suffix of the other: which failure section is which occurrence cannot be told")
    return notes


# pytest keeps a message only when `width - len("WORD node") >= 6` (" - " and an ellipsis); at its own fallback width
# of 80 that is a `WORD node` of at most 74 characters. A longer one may have been cut by the runner's width.
_SUMMARY_LINE_MAX_ID = 74


# ALLOWLIST of short-summary line shapes (r7 hub ruling; replaces the denylist of forged continuations of r5 and r6).
# Rank = the order pytest prints the groups in under ``-rA`` (reportchars ``PpsxXEf``): a continuation line of a
# multi-line test id sits AFTER the line that started the id, so a PASSED/SKIPPED/XFAIL/XPASS line that follows a
# FAILED/ERROR line is out of order. ERROR and FAILED share a rank (a default ``-rfE`` run prints them as f, E).
_SUMMARY_RANK = {"PASSED": 0, "SKIPPED": 1, "XFAIL": 2, "XPASS": 3, "ERROR": 4, "FAILED": 4}
_OTHER_NODE = re.compile(r"[!-~][ -~]*?\.\w+::[ -~]+")  # `<ascii file>.ext::<name>`: the one readable id shape
_SKIPPED_BODY = re.compile(r"\[([0-9]+)\] [!-~][ -~]*?:[0-9]+: [ -~]*")


def _other_line(word: str, line: str) -> tuple[int, str]:
    """(how many tests the line stands for, "") for a PASSED/SKIPPED/XFAIL/XPASS summary line of exactly the shape pytest
    prints under ``-rA``; (0, reason) for anything else. ASCII only, so no wide or invisible character can hide a line
    break or a second id inside it."""
    body = line[len(word) + 1:]
    if not line.isascii() or not line.isprintable():
        return 0, f"{word} line with a non-ASCII or control character"
    if word == "SKIPPED":
        m = _SKIPPED_BODY.fullmatch(body)
        if not m:
            return 0, "a SKIPPED line that is not `SKIPPED [n] file:line: reason`"
        if _empty_segment(body[m.end(1) + 2:].split(":", 1)[0]):
            return 0, "a SKIPPED line whose file path has an empty segment"
        return int(m[1]), ""
    if not _OTHER_NODE.fullmatch(body):
        return 0, f"a {word} line that is not `{word} <file>::<name>`"
    if word == "PASSED" and " - " in body:
        return 0, "a PASSED line holding ' - ' (a real one is `PASSED <id>` alone)"
    if _empty_segment(body.split(" - ", 1)[0]):
        return 0, f"a {word} line whose id has an empty segment (`::::`, a trailing `::`, `//`): no such id exists"
    return 1, ""


def _summary_problem(word: str, node: str, msg: str) -> str:
    """The CLASS RULE for one short-summary `FAILED`/`ERROR` line (r5 hub ruling, an allowlist since r7): it must split
    into exactly `<ascii file.ext>::<name> - <message>` with a message that is not empty and a width that no terminal
    can have cut. "" when it does. Every shape a test name can forge (a multi-line, non-ASCII or over-long id, a ' - '
    inside a directory name) fails this one test, so no case is repaired one by one. A module-level collection
    `ERROR <file.ext> - <message>` has no `::name` and is allowed."""
    if not node.isascii() or not node.isprintable():
        return f"{word} {node[:60]!r} is not an ASCII, single-line node id"
    if len(f"{word} {node}") > _SUMMARY_LINE_MAX_ID:
        # pytest drops or ellipsizes the message when the line does not fit the terminal width (it does not, only
        # when the CI / BUILD_NUMBER environment variable is set), so the same run prints a different line at
        # width 80, at width 200 and on a CI runner. A line this long may have lost its message to the width of
        # whoever ran it; that is judged BEFORE the message is read, so the answer never depends on the width.
        return f"{word} {node[:60]!r} is too long to rule out a width-cut message (more than {_SUMMARY_LINE_MAX_ID} chars)"
    file_part, sep, name = node.partition("::")
    if not re.search(r"\.\w+$", file_part) or (word == "FAILED" and not (sep and name)):
        return f"{word} {node[:60]!r} is not <file>::<name> (a ' - ' inside a directory name?)"
    if not msg.strip():
        return f"{word} {node[:60]!r} has no message after the ' - ' (a multi-line or over-long node id?)"
    if msg.endswith("...") and not _ASSERT_HEAD.match(msg):
        return f"{word} {node[:60]!r} has a message cut to '...' before its exception type shows"
    if re.search(r"\.\w+::", msg):
        # A directory name holding `<x>.ext::<y> - ` makes the first ' - ' look like the end of the node, and the
        # real `file.ext::name` then sits in the "message". A message that holds a node of its own cannot be split.
        return f"{word} {node[:60]!r} has a message that holds another <file>::<name> (the id may end later)"
    return ""


def _parse_pytest(lines: list[str], stats: dict | None = None) -> list[Failure]:
    """`stats` (optional) receives what the text held that the runner's own count can be checked against:
    `sections` (failure/error sections), `summary` (FAILED/ERROR summary lines), `has_summary` (a short test summary
    section exists), `bad` (the first line that fails the class rule, see _summary_problem), `stray` (a summary-section
    line that starts with no known summary word, e.g. the continuation of a multi-line node id)."""
    stats = stats if stats is not None else {}
    stats.update(sections=0, summary=0, stray=0, has_summary=False, bad="",
                 words={"PASSED": 0, "XFAIL": 0, "XPASS": 0, "SKIPPED": 0})
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
            out.append(Failure([cur["name"]], cur["frames"], cur["assertion"], cur["collection"], cur["name"],
                               exc=cur["first"]))
        cur = None

    # Width is checked only INSIDE a captured block, the one place a test can print: there a real rule has the
    # width of the run's first rule of its kind (+-1); elsewhere any rule is accepted, so hand-trimmed logs work.
    def real_rule(line: str) -> bool:
        return not captured or not width or abs(len(line.rstrip()) - width) <= 1

    def real_section(line: str) -> bool:
        return not captured or not sec_width or abs(len(line.rstrip()) - sec_width) <= 1

    after_result = False  # the final result bar of the short summary was seen: nothing may follow it
    rank = 0  # highest group rank seen in the short summary (see _SUMMARY_RANK)
    for line in lines:
        m_top = _PT_TOP.match(line)
        if after_result and line.strip():
            # A result-shaped bar in the middle of a summary (a forged one inside a multi-line id) is followed by more
            # text; the real final bar is the last line of the run.
            stats["bad"] = stats["bad"] or f"text {line.strip()[:40]!r} after the final result bar (a multi-line test id?)"
        if m_top and real_rule(line) and top.startswith("short test summary") and not _RESULT_LINE.match(line):
            # Only the final result bar may follow the summary header. Any other `=== x ===` line there is part of
            # a test id that spans lines (a forged section header), not pytest's own text.
            stats["bad"] = stats["bad"] or f"a rule line {line.strip()[:40]!r} inside the short summary (a multi-line test id?)"
        if m_top and real_rule(line):
            width = width or len(line.rstrip())
            close()
            captured = False
            after_result = after_result or (top.startswith("short test summary") and bool(_RESULT_LINE.match(line)))
            top = m_top["title"].strip()
            continue
        if top.startswith("short test summary"):
            stats["has_summary"] = True
            word = line.split(" ", 1)[0]
            if not line.strip() or word not in _SUMMARY_RANK or not line.startswith(word + " "):
                stats["stray"] += 1  # pytest prints no blank line here and every line starts with a summary word
                continue
            if _SUMMARY_RANK[word] < rank:
                stats["bad"] = stats["bad"] or f"a {word} line after a FAILED/ERROR line (out of the -rA order: a multi-line test id?)"
            rank = max(rank, _SUMMARY_RANK[word])
            if word in stats["words"]:  # tallied; _count_mismatch reconciles each with the runner's own count
                n, why = _other_line(word, line)
                stats["words"][word] += n
                if why:
                    stats["bad"] = stats["bad"] or f"{line[:50]!r}: {why}"
            for word in ("FAILED", "ERROR"):
                got = _parse_summary_line(line, word)
                if not got:
                    continue
                stats["summary"] += 1
                node, msg, ambiguous = got
                stats["bad"] = stats["bad"] or _summary_problem(word, node, msg)
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
            cur = {"name": name, "frames": [], "assertion": None, "first": "",
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
                cur["first"] = first
            if _IMPORT_ERR.match(first):  # any E line: a chained exception counts too
                cur["collection"] = True
            continue
        m_loc = _PT_LOC.match(line)
        if m_loc:
            cur["frames"].append((m_loc["file"], _line_no(m_loc["line"])))
    close()
    if stats["has_summary"] and not after_result:
        # The summary is closed by the runner's own result bar; without it the counts cannot be reconciled at all.
        stats["bad"] = stats["bad"] or "the short summary is not closed by a final result bar (a cut-off or forged log?)"
    # ONE EXCEPTION ACCOUNT PER FAILURE OCCURRENCE (r9 hub ruling): `nodes` is the ordered list of FAILED occurrences and
    # is never keyed by node id. A section takes the message of the ONE occurrence it pairs with. A section that pairs
    # with more than one occurrence is COULD-NOT-CHECK (r8 A1, A2). An occurrence that more than one section pairs with
    # (an inherited test: every section's frames sit in the base class's file) is the account of NONE of them: each such
    # section keeps its own `E` line and no identity, and the occurrence counts by its own summary line. So no
    # occurrence's message is ever given to another section, and every exception account stays visible.
    names = [n for n, _msg in nodes]
    odd_notes.extend(_ambiguous_occurrences(names))
    pairs: list[list[int]] = []
    claims = [0] * len(nodes)
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
        idx = _pair_nodes(_segs(bare), f.frames, names)
        if len(idx) > 1:
            odd_notes.append(f"pairing: the failure section {bare[:60]!r} pairs with {len(idx)} FAILED occurrences ("
                             + ", ".join(repr(names[i][:60]) for i in idx[:4]) + "): which one is its account cannot be told")
        for i in idx:
            claims[i] += 1
        pairs.append(idx)
    for f, idx in zip(out, pairs):
        if len(idx) == 1 and claims[idx[0]] > 1:
            f.ids = []  # a shared occurrence is nobody's account (see above)
            continue
        f.ids = [names[i] for i in idx]
        if len(idx) == 1 and nodes[idx[0]][1]:
            # The summary line is pytest's own account of the exception TYPE; test output printed inside a
            # section cannot change it.
            msg = nodes[idx[0]][1]
            f.exc = msg
            f.assertion = f.assertion and bool(_ASSERT_HEAD.match(msg))
    for (node, msg), k in zip(nodes, claims):
        # a failing node no section is the account of (none paired, or several) still counts, by its summary line
        if k != 1:
            out.append(Failure([node], [], bool(_ASSERT_HEAD.match(msg)), note=msg, exc=msg))
    for note in summary_blockers:
        out.append(Failure([], [], False, True, note))
    for note in odd_notes:
        out.append(Failure([], [], False, note=note, odd=note))
    return out


def _foreign_type(failure: Failure) -> str:
    """The reason a failure's exception type cannot be classified, or "". A run prints only the CLASS NAME, and a
    class of any name may subclass ImportError (or an ExceptionGroup holding one), so only a builtin exception that
    is not an ImportError, an assertion, or pytest's ``Failed:`` is known not to be an import failure."""
    text = failure.exc.strip()
    if not text and not failure.assertion:
        # r8 adversary pass 2, A1: an exception group prints no `E` line (and -rN prints no summary line), so nothing
        # shows the type of this failure; "" must never read as "known not to be an import error".
        return ("no exception line is shown for a failing test (an exception group prints none): its class may "
                "subclass ImportError, which the run does not say")
    if not text or _ASSERT_HEAD.match(text):
        return ""
    token = re.match(r"[\w.]+", text)
    name = token.group(0) if token else text[:40]
    if token and name in _EXC_NAMES and text[len(name):len(name) + 1] in ("", ":"):
        return ""  # an exact name from the fixed list (ImportError and its builtin subclasses are not on it)
    return (f"exception type {name[:40]!r} of a failing test is not on the fixed list of builtin names: its class may "
            "subclass ImportError, which the run does not say")


def _last_result(lines: list[str]) -> str | None:
    """The LAST framed line that is a runner result line, stripped; None when there is none."""
    last = None
    for ln in lines:
        if _RESULT_LINE.match(ln):
            last = ln.strip()
    return last


def _final_green(lines: list[str]) -> bool:
    """True when the LAST runner result line is green (unittest OK, or a pytest bar with no failed/error)."""
    last = _last_result(lines)
    if last is None:
        return False
    if last.startswith("OK"):
        return True
    return last.startswith("=") and not re.search(r"\b(?:failed|errors?)\b", last)


_UT_COUNTS = re.compile(r"[(,] ?(failures|errors)=(\d+)")
_UT_RAN = re.compile(r"^Ran \d+ tests?\b")  # the footer line; the elapsed time after it is not needed


def _count_mismatch(lines: list[str], ut_blocks: int, stats: dict) -> str:
    """The runner states how many tests failed; every one must have been recognized as a block, section or summary
    line. A header this parser could not read (a multi-line test name) would otherwise drop its failure, an import
    error included, without a trace. "" when the counts agree, or when the text holds neither a failure nor a result
    line (the plain checks refuse that as unrecognized). A text WITH failures and NO result line is never "" (r8).

    The pytest short summary is judged as a CLASS (r5 hub ruling), typed ``short-summary line``: a FAILED/ERROR line
    that does not split into exactly ``<file>::<name> - <message>`` (see _summary_problem), a line that starts with
    no summary word, or a number of FAILED/ERROR lines that differs from the runner's own failed+errored count."""
    if stats["bad"]:
        return f"short-summary line: {stats['bad']}"
    if stats["stray"]:
        return "short-summary line: a line starts with no summary word (a multi-line test id?)"
    last = _last_result(lines)
    if ut_blocks or stats["sections"] or stats["has_summary"] or stats["summary"]:
        # R7-C1 (r8): failures were read, so the run must END in the runner's own result: the LAST non-blank line is a
        # result line (adversary A1: a result-shaped line in the middle of a cut log, printed by a failing test or pasted
        # in, is message text), and a unittest `OK`/`FAILED (...)` trailer has its `Ran N tests` footer before it.
        # Otherwise the runner's counts are missing and a later failure (an import error included) may have been cut.
        body = [ln for ln in lines if ln.strip()]
        final = body[-1] if body else ""  # NOT stripped: a real result line starts at column 0 (adversary pass 2, A2)
        if not _RESULT_LINE.match(final):
            return ("no final result line (the run must end in a unittest `OK`/`FAILED (...)` trailer or a pytest "
                    "`=== N failed ... ===` bar): a cut-off log cannot be reconciled with the runner's counts")
        if not final.startswith("=") and not any(_UT_RAN.match(ln) for ln in body[:-1]):
            return ("no final result line the counts can be read from: the unittest trailer has no `Ran N tests` footer "
                    "before it")
    if last is None:
        return ""
    pt_read = stats["sections"] or stats["summary"] or stats["has_summary"]
    # ONE RUN, ONE RUNNER (r9 adversary pass 2, A1): the terminal result reconciles only its own runner's failures, so a
    # failure read by the other runner's parser would never be counted. Either kind under the other's trailer refuses.
    if last.startswith("FAILED (") and pt_read:
        return ("runner: a unittest `FAILED (...)` trailer closes a run in which pytest failure sections or summary lines "
                "were read; one run has one runner, and these failures are not in its count")
    if last.startswith("=") and ut_blocks:
        return ("runner: a pytest result bar closes a run in which unittest failure blocks were read; one run has one "
                "runner, and these failures are not in its count")
    if last.startswith("FAILED (") and any(_UT_RAN.match(ln) for ln in lines):
        want = sum(int(n) for _kind, n in _UT_COUNTS.findall(last))
        if want != ut_blocks:  # a stated 0 counts too: `FAILED (failures=0)` over a readable block is a contradiction
            return f"the runner counts {want} failing unittest tests but {ut_blocks} failure blocks were readable"
    elif last.startswith("="):
        want = sum(int(n) for n in re.findall(r"\b(\d+) (?:failed|errors?)\b", last))
        if stats["sections"] and stats["sections"] != want:
            return f"the runner counts {want} failed or errored tests but {stats['sections']} sections were readable"
        if stats["has_summary"] and stats["summary"] != want:
            return (f"short-summary line: the runner counts {want} failed or errored tests but "
                    f"{stats['summary']} FAILED/ERROR lines were readable")
        for word, label in (("PASSED", "passed"), ("XFAIL", "xfailed"), ("XPASS", "xpassed"), ("SKIPPED", "skipped")):
            # ALLOWLIST (r7): only a run that lists every one of these outcomes can be reconciled, which is what
            # ``-rA`` prints. A multi-line test id can start a continuation line with any summary word, and a default
            # (``-rfE``) run prints no PASSED/SKIPPED/XFAIL/XPASS line at all, so a forged one could stand in for the
            # real ones the runner counts. The lines must add up to the runner's own count, zero included.
            stated = re.search(rf"\b(\d+) {label}\b", last)
            shown = int(stated[1]) if stated else 0
            if stats["words"][word] != shown:  # also with no summary section at all (-rN): the passes are still unlisted
                hint = (": this summary lists none of them, so the run was not made with -rA (default reporting omits "
                        "PASSED/SKIPPED/XFAIL/XPASS lines)" if not stats["words"][word] else " (a multi-line test id?)")
                return (f"short-summary line: {stats['words'][word]} {word} lines were read but the runner counts "
                        f"{shown} {label}{hint}")
    return ""


def parse_run(text: str | list[str]) -> Parsed:
    """Parse a run given as text (framed here, see _frame) or as the lines _frame already returned."""
    lines = _frame(text) if isinstance(text, str) else text
    stats: dict = {}
    ut = _parse_unittest(lines)
    failures = ut + _parse_pytest(lines, stats)
    return Parsed(failures=failures, red=any(_RED_MARK.search(ln) for ln in lines) or bool(failures),
                  green=any(_GREEN_MARK.search(ln) for ln in lines), mismatch=_count_mismatch(lines, len(ut), stats))


def _matches(failure: Failure, expected: str) -> bool:
    site = _EXPECT_SITE.match(expected)
    if site:
        want_file, want_line = _norm_path(site["file"]), _line_no(site["line"])
        return any(line == want_line and _ends_with(_norm_path(file), want_file)
                   for file, line in failure.frames)
    want = _segs(expected)
    return any(_ends_with(_segs(i), want) for i in failure.ids)


def _precheck(lines: list[str] | None, expected: str) -> str:
    """The typed reason this text will not be classified, or "" (see NARROWED PROMISE in the module docstring)."""
    if lines is None or not isinstance(expected, str) or not expected.strip():
        return ""  # the plain checks below refuse these with their own reasons
    if any(ln.strip() in _CHAIN for ln in lines):
        return "chained exception in the output: which exception the run reports is not decided here"
    if any(_CAPTURED_ANY.search(ln) for ln in lines):
        return "captured output in the run: the test's own text follows the divider and can say anything"
    site = _EXPECT_SITE.match(expected)
    if site:
        _line_no(site["line"])
    elif _empty_segment(expected):
        return "empty segment in the expected test identity (`..`, a trailing `.` or `::`): no such id exists"
    return parse_run(lines).mismatch


@dataclass(frozen=True)
class Verdict:
    """One classification. ``reason`` is the PRIMARY reason, the one the status was decided for. ``also`` holds the
    secondary diagnostics: what the older checks would have said about text the precheck already refused. A diagnostic
    never decides anything, so a check that pins WHY a run was refused reads ``reason`` only (r10, Dot R8-T1: a
    semantic reason matched inside an appended diagnostic hid the format refusal that actually decided the case)."""
    status: str
    reason: str
    also: tuple[str, ...] = ()


def verdict(output: str, expected: str) -> tuple[str, str]:
    """Return (PASS|REFUSED, text): the primary reason, then each diagnostic after ``; also: `` (see classify). PASS
    only when `expected` is an assertion that failed. Text it cannot classify with certainty is REFUSED with a reason
    that starts ``COULD-NOT-CHECK`` (NARROWED PROMISE, module docstring). For display: match a reason on classify()."""
    v = classify(output, expected)
    return v.status, v.reason + "".join(f"; also: {a}" for a in v.also)


def classify(output: str, expected: str) -> Verdict:
    """The verdict with its primary reason and its secondary diagnostics kept apart (see Verdict)."""
    pre, status, reason = "", REFUSED, ""
    try:
        lines = _frame(output) if isinstance(output, str) else None  # the ONE framing; every reader gets these lines
        pre = _precheck(lines, expected)
        status, reason = _verdict(lines, expected)
    except _Unclassifiable as exc:
        # a count or summary problem found by the precheck is the more basic one: it leads, the other follows
        return Verdict(REFUSED, f"COULD-NOT-CHECK ({pre or exc})", (str(exc),) if pre else ())
    except Exception as exc:  # noqa: BLE001 - the promise is a refusal, never an uncaught traceback
        return Verdict(REFUSED, f"COULD-NOT-CHECK (internal-error {type(exc).__name__} while reading the text)")
    if pre:
        return Verdict(REFUSED, f"COULD-NOT-CHECK ({pre})", (reason,) if status == REFUSED else ())
    return Verdict(status, reason)


def _verdict(lines: list[str] | None, expected: str) -> tuple[str, str]:
    if not expected or not expected.strip():
        return REFUSED, "no expected assertion given (file:line or test id): a red suite proves nothing alone"
    if lines is None:
        raise TypeError("the run output is not text")  # verdict() refuses it as an internal error, as before r9
    if _final_green(lines):
        return REFUSED, ("the run is green (final result line): failure-looking text above it was test output, "
                         "so no assertion failed")
    run = parse_run(lines)
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
    for f in run.failures:
        why = _foreign_type(f)
        if why:
            raise _Unclassifiable(why)
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
    """NUMBER ALLOWLIST (r7 hub ruling): accepts exactly an ``int``, a ``float``, or a ``str`` made of ASCII digits, a
    sign, a point and an exponent (plus a spelled ``inf``/``nan``). ``Decimal``, ``Fraction``, numpy-like scalars, a
    ``bool`` and every other type raise ``TypeError`` BEFORE any conversion, and text in another script's digits or with
    a ``_`` separator raises ``ValueError``: ``float()`` reads those, and a magnitude cannot be inspected in a form the
    checker has not enumerated. An int stays an int: Python compares int and float exactly, while float(2**53 + 1) would
    round it. A nonzero spelling that a float reads as 0.0 or inf raises ``OverflowError``."""
    if type(v) is float:
        return v
    if type(v) is int:
        float(v)  # OverflowError for an int no double can hold: not a real reading (callers refuse it)
        return v
    if type(v) is not str:
        raise TypeError(f"{type(v).__name__} is not an int, a float or ASCII numeric text")
    if _INT_TEXT.fullmatch(v):
        try:
            n = int(v)
        except ValueError:
            raise OverflowError("integer text beyond the int() digit limit") from None
        float(n)
        return n
    if _SPECIAL_TEXT.fullmatch(v):
        return float(v)  # a spelled `inf` window bound is a real unbounded window; `nan` is refused by the caller
    if not _DEC_TEXT.fullmatch(v):
        raise ValueError("not ASCII numeric text")
    x = float(v)
    if math.isinf(x):
        raise OverflowError("a number spelled out beyond what a float can hold (read as inf)")
    if x == 0.0 and re.search(r"[1-9]", re.split(r"[eE]", v)[0]):
        raise OverflowError("a nonzero number spelled out beyond what a float can hold (read as 0.0)")
    if abs(x) >= 2 ** 53:
        # From 2**53 a float skips integers, while integer text stays an exact int: read the same number two ways
        # and `9007199254740993` differs from `9007199254740993.0`. Integral text is therefore returned as the exact
        # int, and a non-integral value that large cannot be held by either type (r7 adversary A2).
        if len(v) > 400:
            raise OverflowError("a spelling too long to read exactly")
        exact = Fraction(v.strip())
        if exact.denominator != 1:
            raise OverflowError("a non-integral number beyond 2**53, which neither an int nor a float holds exactly")
        return int(exact)
    return x


def _cli_num(v: str) -> object:
    """argparse type: a number, or (for text outside the allowlist that a float would still read) the text itself, so
    that probe_discriminates refuses it by name instead of argparse raising. Plain garbage stays an argparse error."""
    try:
        return _num(v)
    except (OverflowError, TypeError):
        return v
    except ValueError:
        try:
            float(v)
        except ValueError:
            raise
        return v


def probe_discriminates(bug_value: float, fix_value: float, window: tuple[float, float]) -> tuple[bool, str]:
    """True only when the FIX reading is inside the closed window and the BUG reading is outside."""
    try:
        if type(window) not in (list, tuple) or len(window) != 2:  # a dict's keys or a 2-character string are not bounds
            raise TypeError("window must be a list or tuple of exactly two bounds")
        lo, hi = window
        bug_value, fix_value, lo, hi = _num(bug_value), _num(fix_value), _num(lo), _num(hi)
    except (TypeError, ValueError, OverflowError):
        return False, "COULD-NOT-CHECK (number: probe readings and window must be numbers a float can hold, given as an int, a float or ASCII numeric text)"
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
            # newline="": no newline translation, so _frame sees the saved file's own line ends (a lone CR included;
            # read_text() would turn it into LF first: r9 adversary A1).
            # errors="strict": a file that is not UTF-8 is unreadable (exit 2); a replacement character could stand in
            # for any letter of a name and so match an expectation that holds one (r9 adversary pass 2, A2).
            with Path(args.output).open(encoding="utf-8", errors="strict", newline="") as fh:
                text = fh.read()
        except (OSError, ValueError) as exc:  # ValueError: a path holding NUL (r9 adversary A2), UnicodeDecodeError
            print(f"cannot read --output {args.output!r}: {type(exc).__name__}: {getattr(exc, 'strerror', None) or exc}",
                  file=sys.stderr)
            return 2
        status, reason = verdict(text, args.expect)
    else:
        ok, reason = probe_discriminates(args.bug, args.fix, tuple(args.window))
        status = PASS if ok else REFUSED
    print(f"{status} {reason}")
    return 0 if status == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
