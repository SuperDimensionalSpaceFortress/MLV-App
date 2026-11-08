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
    py -3 tools/repo_hygiene/red_mutation_verdict.py verdict --output red.txt --expect pkg.t.T.test_x --unittest-layout legacy
    py -3 tools/repo_hygiene/red_mutation_verdict.py probe --bug 3 --fix 3 --window 3 3

``--unittest-layout {3.11+,legacy}`` (keyword ``unittest_layout`` of ``verdict()``/``classify()``; default ``3.11+``) declares
the unittest header layout of the interpreter that ran the tests; the CLI line ends with ``[unittest-layout=<layout>]`` and
``Verdict.layout`` carries it, so the assumption is in the saved verdict (r16 hub ruling; see ``layout`` below).

THREAT MODEL (r14 hub ruling, HUB-TICK 20261107T2334Z, rule 1): "The checker reads runner TEXT. It guards against HONEST
probes that fail for the wrong reason. It does not guard against HOSTILE test code. A test that builds a code object with
a newline in co_name or co_filename (types.CodeType.replace, compile() with a crafted filename) could just as well print a
whole fake report to stdout. No text parser can detect that. Python source cannot declare a function name holding a line
break, so honest code never produces this shape." A false PASS from honest runner output is a defect; a forged report
built by the test itself is out of scope. The ``traceback line break`` refusal below is a cheap fail-closed tripwire on top
of that ruling, not a promise to detect hostile text.

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
* ``bracket`` (r13 hub ruling; twelve rounds of first-``[`` parsing each found a new false PASS): an id (run or
  expectation) with a ``[`` BEFORE its last separator: ``::`` for a pytest node id, ``.`` ``/`` ``\\`` for a dotted
  unittest id or an expectation without ``::``, ``/`` ``\\`` for a file path (a frame, a ``file:line`` expectation, a
  module ``ERROR`` id, a ``SKIPPED`` location). A class or file name set through globals() may hold ``[`` and a function
  name ``]``, so only a trailing ``[...]`` on the LAST segment is read as a parameter id, and one that holds the
  separator itself (``test_x[a::b]``) cannot be told from a name boundary (``TestA[x.py::test_y]``) and is refused too.
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
* ``result line`` (r11, Dot r9-A1, a GRAMMAR): the runner's last result line is parsed WHOLE, once, and its counts are
  the only ones any check reads (no digit substring is ever taken from a partly read line). unittest: ``OK``, ``OK
  (skipped=N, ...)`` or ``FAILED (failures=N, errors=N, skipped=N, expected failures=N, unexpected successes=N)``, each
  field at most once and in that order; the ``Ran N tests in T s`` footer likewise. pytest: ``=== <N word>, ... in T s
  ===`` with the words failed, passed, skipped, deselected, xfailed, xpassed, warning(s), error(s), rerun, each once,
  then only the elapsed-time clause (``in 0.12s``, ``in 75.12s (0:01:15)``). A COUNT is ASCII digits printed as ``%d``:
  no sign, no leading zero, no fraction, no exponent, no other script's digit (``failures=1.5``, ``-1 failed``, ``0.1
  passed``, ``01 failed`` are refused, even where a digit inside equals the readable count). A line of the result shape
  that does not parse whole is COULD-NOT-CHECK.
* ``frame`` (r11): a unittest ``File "...", line N`` frame ends after its ASCII line number with ``, in <name>`` or the
  end of the line, and a pytest ``file.py:N:`` location line with its ``:``. A line of either shape that does not parse
  whole (``line 2.5,``, ``line 2x``, ``t.py:2.5:``) is COULD-NOT-CHECK, never a frame at its digit prefix. A unittest
  frame is the line at exactly two spaces that the traceback module prints (r12, r11 pass 3 A3): a frame's source lines
  are printed four or more spaces in, so source text that reads ``File "victim.py", line 2`` (a multi-line string) is
  never a frame, and a source line holding ``", line `` twice is not refused (Dot e3f43d7).
* ``traceback line break`` (r14 hub ruling; r13 adversary fde24fbc A1/A2, Dot 4153241; ``cnc-traceback-line-break``): a
  line break inside a code object's name or file name prints a forged line. (a) In a unittest traceback, a second
  column-0 line shaped like an exception head (``Name:`` or ``Name`` alone) after the exception line, or a frame header
  after it, is COULD-NOT-CHECK: the real exception line is printed after the last frame, so a forged one before it always
  trips this. A message line of that shape trips it too (the safe direction); the chaining sentences never do. (b) A frame
  that DECIDES a ``file:line`` PASS and is printed directly next to another frame header (a unittest ``  File`` line, a
  pytest ``path:N:`` location line), with no source line between them, is COULD-NOT-CHECK. A sourceless frame (exec'd or
  frozen code) that decides nothing keeps its verdict.
* ``long summary line`` (r14, Dot 4153241): a short-summary line (PASSED/SKIPPED/XFAIL/XPASS, and FAILED/ERROR, the same
  class) longer than ``_SUMMARY_SCAN_MAX`` (4096) characters is ``short-summary line`` COULD-NOT-CHECK before its id or
  reason is scanned (the scan checks every candidate end of its id as its own prefix, quadratic in the line).
* ``summary split`` (r11, r10 adversary A1): a class or function name set through ``globals()`` may hold `` - ``, so the
  first `` - `` of a ``FAILED``/``ERROR`` line is only one candidate end of its node id, and an exception class of ANY
  name may follow it (``type('Bad-Name', ...)`` prints ``t.Bad-Name``; r11 pass 1 A1), so every later `` - `` with a node
  id on its left is another reading, readable or not: a left side that is a node id but for a non-ASCII or control
  character (a method ``test_`` plus an accented letter) makes the line COULD-NOT-CHECK, never a line read by dropping
  that reading (r12 A1-U, Dot e3f43d7). A line with more than one reading is read only when EXACTLY ONE of them is the
  account of one of the run's own failure sections, by its test name AND a frame in its file (r12, r11 pass 3 A1;
  default tracebacks print one section per failure); otherwise it is
  COULD-NOT-CHECK (``FAILED t.py::TestA - AssertionError::test_x - TypeError: boom``), and so is a parameter id holding
  ``] - ``. A line naming an import error blocks like one under every reading. The cost, accepted: under ``--tb=no`` an
  assertion message holding `` - `` (``assert x - 1 == y``) is refused. An assertion head needs its own delimiter:
  ``assert`` then whitespace or the end, ``AssertionError``/``Failed`` then ``: `` or the end (``assert.Name``,
  ``AssertionError::x`` and ``AssertionError:x`` are class names, not assertions; r11 pass 1 A2).
* ``runner``: one run has one runner. A unittest ``FAILED (...)`` trailer over pytest sections or summary lines, or a
  pytest bar over unittest blocks, is COULD-NOT-CHECK: the trailer's count does not cover the other runner's failures.
* ``pairing`` (r9 hub ruling, a STRUCTURE): every pytest ``FAILED`` line is one failure OCCURRENCE with its own exception
  account. A section takes the account of the one occurrence it pairs with; a node id printed more than once
  (``--keep-duplicates``), two node ids with one test name in files where one path is a suffix of the other
  (``a/test_s.py``, ``b/a/test_s.py``), or a section that pairs with more than one occurrence is COULD-NOT-CHECK, naming
  the node ids. An occurrence that several sections pair with (an inherited test, whose frames all sit in the base
  class's file) is none of theirs: each keeps its own ``E`` line, and the occurrence counts by its own summary line. No
  occurrence's message is given to another section.
* ``layout`` (r16 hub ruling, HUB-TICK 20261108T0259Z; ``cnc-layout``): a unittest traceback frame whose path sits in a
  CPython library directory of the OTHER layout's version, read from the same saved output and never from the host
  interpreter: ``lib/python3.10/``, ``lib\\python3.9\\``, ``Python310\\Lib\\``, ``Python39\\lib\\``, ``python310.zip\\`` (<= 3.10)
  contradicts the declared ``3.11+``; the same directories of a version >= 3.11 contradict ``legacy``. The reason names the
  frame. Either separator; the version is ASCII digits with no leading zero and ends with its directory, so 3.1, 3.10 and
  3.100 are three versions (3.1 is a version <= 3.10 and so is legacy evidence; 3.100 is not). Any frame trips it, a
  ``file:line`` expectation included. A frame in no such directory (``venv\\Lib\\``, ``lib/python3/``) is no evidence.
* ``internal-error``: any other exception while reading the text. Never an uncaught traceback.

A refusal is checked BEFORE the older reasons and the older reason is appended (``; also: ...``), so a
refusal that was already a refusal stays one. Only a PASS can be changed by this section, and only to REFUSED.
The refusal is the PRIMARY reason and the appended text is a secondary diagnostic: ``classify()`` returns them as
separate fields (``Verdict.reason``, ``Verdict.also``), and a check that pins why a case was refused matches the primary
reason only (r10, Dot R8-T1). ``verdict()`` joins them into one text for display.
REASON CODES (r11, r10 adversary A2): ``Verdict.code`` is the checker's own name for the primary reason (one of
``REASON_CODES``), set at the site that writes the reason; ``probe_classify()`` gives the same for a probe. A reason's
TEXT quotes its input (an id, a node, ``repr()`` of a name), so a check that pins why compares the code, never text.

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
- unittest LAYOUT (r15, Dot R18 B1; r16 hub ruling, Dot ef44c9a): 3.11+ prints ``name (module.Class.name)``, CPython
  <= 3.10 ``name (module.Class)``. The layout is DECLARED (``--unittest-layout``, default ``3.11+``). Under ``3.11+`` a
  group of two segments ending in ``.name`` (``test_x (t.test_x)``) cannot be 3.11+ (module, class and method are three
  segments), so it reads as the <= 3.10 class ``test_x`` of module ``t``: ``t.test_x.test_x`` (r15). Under ``legacy`` every
  group is ``module.Class``: ``test_x (pkg.t.test_x)`` is ``pkg.t.test_x.test_x``.
  NON-PROMISE (r16): a group of three or more segments ending in ``.name`` (``test_x (pkg.t.test_x)``) is BOTH the 3.11+
  ``pkg.t.test_x`` and the <= 3.10 class ``test_x`` of module ``pkg.t``, character for character, and no line of a run
  tells the two apart. UNDECLARED <= 3.10 OUTPUT THAT CARRIES NO VERSION EVIDENCE (see ``layout`` above) IS READ AS 3.11+,
  so its 3.11+ reading can PASS. The same holds for a doctest of method n of class n (``n (pkg.t.n)``, any version). Why
  this is the contract: the host runs CPython 3.14 (``py -3 --version``: ``Python 3.14.3``); hosted CI installs the version
  in ``.python-version`` (``3.13.15``) through ``actions/setup-python`` ``python-version-file`` (.github/workflows/tests.yml:42,
  :100 and every other setup-python step); CPython 3.10 reached end of life in October 2026. A run on <= 3.10 declares
  ``--unittest-layout legacy``. A ``file:line`` expectation does not depend on the layout (the tripwire still applies).
  STOP RULE (r16): a later layout variant under the declared or default layout whose output carries no version evidence is
  DISTINGUISHED under this NON-PROMISE.
- Whitespace inside an id is significant (`` a/t.py`` is not ``a/t.py``; ``test one`` is not ``test``), and the
  expectation is never stripped: `` a/t.py:5`` and ``a/t.py:5\\n`` match nothing.
- It reads text, so text can be forged. r7 TERMINATION (hub ruling): an allowlisted shape is judged, anything outside
  it is refused, and a finding that only shows an input outside the allowlist being REFUSED is not a defect. Not
  defended, because the text is identical to what a genuine run prints: a test that forges its own node ids and
  whose run also hides the outcome it replaces (for example ``-rp`` listing a forged FAILED line while the real
  failure goes unlisted, which the counts, the order and the shapes cannot tell from a genuine ``-rpf`` run), and a
  class that prints under a builtin's bare name while it is something else: one that sets
  ``__module__ = 'builtins'``, and one DEFINED IN ``__main__`` (a test file run as a script, ``python test_x.py``),
  because traceback and pytest print a ``__main__`` class unqualified too (an ``AssertionError`` or a ``TypeError``
  that is an ``ImportError``; r9 and r10 adversary A2). Its line is the builtin's line, character for character, so
  no reading of the text can refuse it without refusing the builtin. Also not defended (OPEN, r11 pass 2 A2, present at
  e2d9dc08 too): a class whose ``__module__`` spells an assertion head (``__module__ = 'assert '`` prints ``assert
  .MissingDependency: boom``); a module name can be any text, so its printed line can be made to read as an assertion. Forgery shapes go to the P3 follow-up card, not to another round. Also not defended: an exception message that itself prints a complete
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

# REASON CODES (r11, r10 adversary A2): every verdict carries one of these, set by the checker AT THE SITE that writes the
# reason. A check that pins why a case was refused compares the code, never the rendered text: a reason quotes its
# input (an id, a node, repr() of a name), so a substring of it can always be supplied by that input.
REASON_CODES = frozenset((
    # PASS
    "assertion-failed", "probe-separates",
    # REFUSED, a semantic answer
    "no-expectation", "final-green", "green", "unrecognized", "red-no-assertion", "collection", "ambiguous-expectation",
    "non-assertion", "not-at",
    # REFUSED, COULD-NOT-CHECK (NARROWED PROMISE)
    "cnc-chained", "cnc-captured", "cnc-expectation", "cnc-line-framing", "cnc-decoding", "cnc-line-number", "cnc-frame",
    "cnc-identity", "cnc-pairing", "cnc-summary-split", "cnc-exception-type", "cnc-short-summary", "cnc-result-line",
    "cnc-no-result-line", "cnc-no-ran-footer", "cnc-runner", "cnc-count", "cnc-internal-error",
    "cnc-bracket-before-last-segment", "cnc-traceback-line-break", "cnc-layout",
    # REFUSED, probe_discriminates
    "probe-number", "probe-nan", "probe-infinite", "probe-empty-window", "probe-both-inside", "probe-neither-inside",
    "probe-bug-inside",
))


class _Reason(str):
    """A reason's text with the checker's CODE for it (see REASON_CODES). It is a str, so text-only readers are unchanged;
    the code travels with it from the site that wrote it to Verdict.code."""

    code: str

    def __new__(cls, code: str, text: str) -> "_Reason":
        if code not in REASON_CODES:
            raise ValueError(f"unknown reason code {code!r}")
        self = super().__new__(cls, text)
        self.code = code
        return self


_EQ_RULE = re.compile(r"^={20,}\s*$")
# A traceback frame is parsed WHOLE (r11, Dot r9-A1): the line number is ASCII digits followed by the runner's own
# delimiter, `, in <name>` or the end of the line (a SyntaxError location has no function). A line of the frame's shape
# that does not parse (`line 2.5,`, `line 2x`, a non-ASCII digit) is COULD-NOT-CHECK, never a frame at a digit prefix.
# r12 (r11 pass 3 A3, Dot e3f43d7): a frame is the line the traceback module prints for it, `  File "...", line N` at
# exactly TWO spaces. Its source lines are printed four or more spaces in (3.11+ prints every line of a multi-line call,
# with its caret lines), so a string's text `File "victim.py", line 2` is source, never a frame, and never a refusal.
_FRAME = re.compile(r'^  File "(?P<file>.+)", line (?P<line>[0-9]+)(?:, in .*)?[ \t]*$')
_FRAME_SHAPE = re.compile(r'^  File ".*", line ')
# r14 (adversary e905500a-A1): `rest` ends at its LAST non-space character, matched greedily. The lazy `.+?\s*$` it replaces
# rescanned the trailing whitespace from every position, quadratic in a run of spaces (a subTest parameter `' ' * 24000`
# took 6.9 s before the header-length guard). The same lazy form, _UT_HEAD, was unused and is removed.
_UT_KIND = re.compile(r"^(?P<kind>FAIL|ERROR): (?P<rest>.*\S)\s*$")
# r14 fix 2 (adversary 716e6018-A1): what unittest prints after a test's identity for a subTest, `_SubTest._subDescription`:
# ` [msg]`, ` (k=v, ...)`, both (` [msg] (k=v)`), ` (<subtest>)`, or nothing. A `[msg]` suffix was not accepted, so
# `test_x (t.T.test_x) [ordinary message]` fell to the legacy reading and became the invented id `t.T.test_x.test_x`.
_SUBTEST_SUFFIX = re.compile(r"(?: \[.*\])?(?: \(.*\))?")
_DASHES = re.compile(r"^-{70}\s*$")
# r16 HUB RULING (HUB-TICK 20261108T0259Z; Dot ef44c9a): the unittest LAYOUT is DECLARED by the caller, never guessed from
# the host interpreter. "3.11+" reads a header group as today (`name (module.Class.name)`, r15's two-segment rule kept);
# "legacy" (CPython <= 3.10) reads it only as `module.Class`.
LAYOUT_MODERN, LAYOUT_LEGACY = "3.11+", "legacy"
UNITTEST_LAYOUTS = (LAYOUT_MODERN, LAYOUT_LEGACY)
# The VERSION a traceback frame's path names, when it sits in a CPython library directory: POSIX `lib/python3.10/` (also
# lib64, abiflags such as `3.13t`), Windows `Python310\Lib\` (also `-32`/`-arm64`), and the embedded `python310.zip\`.
# Either separator, case-insensitive. The path is split into its segments ONCE and every segment is read (r16 adversary
# ea405b3c-A1: a regex scan consumed the separator one directory shares with the next, so `lib/python3.14/lib/python3.10/`
# named only 3.14). A segment is matched WHOLE: the digits are ASCII with no leading zero and end with the segment (POSIX
# abiflag letters aside), so 3.1, 3.10 and 3.100 are three versions, never a prefix of one another. The directory must
# hold the frame's file (a segment follows it). A path in no such directory (`venv\Lib\`, `lib/python3/`) is no evidence.
_PATH_SPLIT = re.compile(r"[/\\]")
_SEG_LIB = re.compile(r"lib(?:64)?", re.I | re.A)  # POSIX `lib/` or `lib64/`, followed by `pythonX.Y`
_SEG_POSIX_PY = re.compile(r"python(?P<major>[1-9][0-9]*)\.(?P<minor>0|[1-9][0-9]*)[a-z]*", re.I | re.A)
_SEG_WIN_PY = re.compile(r"python(?P<major>[1-9])(?P<minor>0|[1-9][0-9]*)t?(?:-32|-64|-arm64)?", re.I | re.A)  # + `Lib`
_SEG_WIN_LIB = re.compile(r"lib", re.I | re.A)
_SEG_ZIP = re.compile(r"python(?P<major>[1-9])(?P<minor>0|[1-9][0-9]*)\.zip", re.I | re.A)
_LAST_LEGACY = (3, 10)  # the last CPython whose unittest prints `name (module.Class)` (3.11 added the method segment)
_PT_CAPTURED = re.compile(r"^-{3,} Captured .+ -{3,}\s*$")
_CHAIN = ("The above exception was the direct cause of the following exception:",
          "During handling of the above exception, another exception occurred:")
# r14 HUB RULING (fde24fbc-A1): a column-0 line shaped like an exception head, `Name:` or `Name` alone (a dotted module
# path and a `<locals>` qualname included; \w is Unicode). In one unittest traceback's exception region a SECOND such line
# is COULD-NOT-CHECK. The real exception line is always printed, so a forged one always trips it; a message line of this
# shape trips it too, which is the safe direction. The chaining sentences hold spaces, so they never match.
_EXC_HEAD_SHAPE = re.compile(r"^[^\W\d][\w.<>]*(?::|\s*$)")
_PT_SECTION = re.compile(r"^_{2,} (?P<name>.+?) _{2,}\s*$")
_PT_TOP = re.compile(r"^={3,} (?P<title>.+?) ={3,}\s*$")
_PT_LOC = re.compile(r"^(?P<file>[^\s:][^\n]*?\.py):(?P<line>[0-9]+):(?: (?P<rest>.*))?$")
# The pytest location line's shape (a `.py:` then any digit, \d on purpose so that a non-ASCII digit is caught): one that
# _PT_LOC does not parse whole is COULD-NOT-CHECK (r11). A source line (`>` or indented) and an `E` line are not one.
_PT_LOC_SHAPE = re.compile(r"^[^\s:>][^\n]*?\.py:\d")
# A leading "| " / "+-" run is the gutter an ExceptionGroup traceback puts in front of nested errors.
_IMPORT_ERR = re.compile(r"^[\s|+\-]*(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError)\b")
_IMPORT_ANYWHERE = re.compile(r"(?:^|[^\w])(?:[\w.]+\.)?(?:ModuleNotFoundError|ImportError)\b")
_EXC_GROUP = re.compile(r"^(?:[\w.]+\.)?(?:Base)?ExceptionGroup\b")
# An assertion message starts with `assert`, or is exactly the AssertionError class (word-bounded: a class
# named AssertionErrorImpostor is not one), or pytest's `Failed:`.
# r11: each head needs the runner's own delimiter after it: `assert` then whitespace or the end, the class name then `: `
# or the end. A word boundary is not one: `assert.MissingDependency` (a class in a module named `assert`, r11 pass 1 A2)
# and `AssertionError::test_x` / `AssertionError:x` (forged class names, r10 A1) are not assertions.
# r12 (adversary r12 pass 1 A2): `Failed` takes the same delimiter as the class name, so the bare `Failed` that
# pytest.fail() with no message prints is the head the docstring names (it was refused as a foreign type).
_ASSERT_HEAD = re.compile(r"(?:assert(?:\s|$)|AssertionError(?::[ \t]|:?\s*$)|Failed(?::[ \t]|:?\s*$))")
_UT_ASSERT = re.compile(r"AssertionError(?::[ \t]|:?\s*$)")
_EXPECT_SITE = re.compile(r"^(?P<file>.+\.\w+):(?P<line>[0-9]+)\Z")
# Every pattern below reads ONE framed line (see _frame): none is compiled multi-line, none searches the raw text.
_RED_MARK = re.compile(r"^FAILED \(|^FAILED |\b\d+ (?:failed|errors?)\b|^Interrupted:")
_GREEN_MARK = re.compile(r"^OK\b|\b\d+ passed\b")
# The SHAPE of a runner result line (a line that claims to be one). Its fields are read only by _read_result, which
# parses the whole line or refuses it: nothing reads a count from this shape.
_RESULT_LINE = re.compile(r"^(?:OK\b.*|FAILED \(.*\)|=+ .*\bin \d[\d.]*s\b.*=+)\s*$")
# COUNT SPELLING (r11, Dot r9-A1): a runner prints a count with %d, so a count is ASCII digits with no sign, no leading
# zero, no fraction and no exponent. Anything else in a count field refuses the whole line.
_COUNT = r"(?:0|[1-9][0-9]{0,8})"
_UT_TRAILER = re.compile(r"(?P<word>OK|FAILED)(?: \((?P<fields>.*)\))?")
_UT_ORDER = ("failures", "errors", "skipped", "expected failures", "unexpected successes")  # unittest's own print order
_UT_FIELD = re.compile(rf"(?P<name>{'|'.join(_UT_ORDER)})=(?P<n>{_COUNT})")
_PT_BAR = re.compile(r"=+ (?P<body>.+?) =+")
# the elapsed-time clause pytest prints: `in 0.12s`, and from a minute on `in 75.12s (0:01:15)` (a timedelta)
_PT_TIMED = re.compile(r"(?P<counts>.+?) in [0-9]+(?:\.[0-9]+)?s(?: \((?:[0-9]+ days?, )?[0-9]+:[0-9]{2}:[0-9]{2}\))?")
_PT_FIELD = re.compile(rf"(?P<n>{_COUNT}) (?P<label>failed|passed|skipped|deselected|xfailed|xpassed|warnings?|errors?|rerun)")
_PT_KEY = {"warning": "warnings", "error": "errors"}  # the singular pytest prints for exactly one


@dataclass(frozen=True)
class _Result:
    """A runner result line parsed whole: the runner, whether it is green, and its counts by field name. The ONE place a
    count is read; validation and reconciliation both consume these counts and never rescan the line."""
    runner: str  # "unittest" | "pytest"
    green: bool
    counts: dict


def _read_result(line: str) -> _Result:
    """Parse a result-SHAPED line (_RESULT_LINE) whole, or raise _Unclassifiable (cnc-result-line)."""
    s = line.rstrip()
    m = _UT_TRAILER.fullmatch(s)
    if m:
        counts: dict = {}
        for field in (m["fields"].split(", ") if m["fields"] is not None else ()):
            f = _UT_FIELD.fullmatch(field)
            if not f or f["name"] in counts:
                break
            counts[f["name"]] = int(f["n"])
        else:
            in_order = list(counts) == [k for k in _UT_ORDER if k in counts]
            ok_fields = m["word"] == "FAILED" or not {"failures", "errors", "unexpected successes"} & set(counts)
            if in_order and ok_fields and (m["fields"] is None) == (m["word"] == "OK" and not counts):
                return _Result("unittest", m["word"] == "OK", counts)
        raise _Unclassifiable("cnc-result-line", f"result line: {s[:60]!r} is not a unittest trailer of whole integer "
                              "counts (`OK`, `OK (skipped=N)`, `FAILED (failures=N, errors=N, ...)`: each field once, in "
                              "the runner's order, a count of ASCII digits with no sign, fraction or exponent)")
    m = _PT_BAR.fullmatch(s)
    t = _PT_TIMED.fullmatch(m["body"]) if m else None
    if t:
        counts = {}
        if t["counts"] == "no tests ran":
            return _Result("pytest", True, counts)
        for part in t["counts"].split(", "):
            f = _PT_FIELD.fullmatch(part)
            key = _PT_KEY.get(f["label"], f["label"]) if f else ""
            if not f or key in counts:
                break
            counts[key] = int(f["n"])
        else:
            # green as before r11: a bar that states no failed/error count at all (a stated `0 failed` is a count)
            return _Result("pytest", "failed" not in counts and "errors" not in counts, counts)
    raise _Unclassifiable("cnc-result-line", f"result line: {s[:60]!r} is not a pytest result bar of whole integer counts "
                          "(`=== N failed, N passed, ... in 0.12s ===`: known outcome words, each once, a count of ASCII "
                          "digits with no sign, fraction or exponent, and only the elapsed-time clause after them)")


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
    """The text holds something this checker will not classify; verdict() turns it into COULD-NOT-CHECK. ``code`` is the
    checker's name for the reason (REASON_CODES)."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.reason = _Reason(code, text)
        self.code = code


def _frame(text: str) -> list[str]:
    """THE one framing of the run's text into lines (r9 hub ruling). Only "\\n" and "\\r\\n" end a line; any other
    boundary str.splitlines() knows (see _FOREIGN_BREAKS) refuses the whole text. Every reader (unittest blocks, pytest
    sections and summary, the terminal result, the `Ran` footer, the counts, the green check, the precheck) consumes
    this list and never the raw text."""
    m = _FOREIGN_BREAK.search(text)
    if m:
        raise _Unclassifiable("cnc-line-framing", f"line framing: the text holds U+{ord(m.group(0)):04X}, a line boundary "
                              "other than \\n and \\r\\n, which some readers would split at and others would not")
    if "\ufffd" in text:  # r9 adversary pass 2, A2: a lossy decode put it where bytes were lost; any name may have held it
        raise _Unclassifiable("cnc-decoding", "decoding: the text holds U+FFFD, the replacement character a lossy decode "
                              "puts where bytes could not be read, so a file name or test id holding it is not the one "
                              "the run printed")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines[-1] == "":  # the newline that ends the last line starts no new line
        lines.pop()
    return lines


def _line_no(digits: str) -> int:
    if len(digits) > _MAX_LINE_DIGITS:
        raise _Unclassifiable("cnc-line-number", f"line number of {len(digits)} digits is not a line of a real file")
    return int(digits)


def _check_layout(layout: object) -> str:
    """The declared unittest layout, or ValueError: a caller's bad keyword is bad usage, not text to refuse."""
    if not isinstance(layout, str) or layout not in UNITTEST_LAYOUTS:
        raise ValueError(f"unittest_layout must be one of {UNITTEST_LAYOUTS}, got {layout!r}")
    return layout


def _stdlib_version(path: str) -> tuple[int, int] | None:
    """(major, minor) of the CPython library directory a frame path sits in (see _SEG_*), or None. A path naming two
    different versions gives None here; _layout_clash reads every version a path names (_stdlib_versions)."""
    found = _stdlib_versions(path)
    return found[0] if len(found) == 1 else None


def _stdlib_versions(path: str) -> list[tuple[int, int]]:
    """EVERY CPython library version the path names, in path order, without repeats. Each segment is tested on its own
    against each shape (no shared separator is consumed: r16 adversary ea405b3c-A1)."""
    segs = _PATH_SPLIT.split(path)
    seen: list[tuple[int, int]] = []
    for i, seg in enumerate(segs):
        m = None
        if i + 2 < len(segs) and _SEG_LIB.fullmatch(seg):
            m = _SEG_POSIX_PY.fullmatch(segs[i + 1])  # lib/pythonX.Y/<file>
        if m is None and i + 2 < len(segs) and _SEG_WIN_LIB.fullmatch(segs[i + 1]):
            m = _SEG_WIN_PY.fullmatch(seg)  # PythonXY\Lib\<file>
        if m is None and i + 1 < len(segs):
            m = _SEG_ZIP.fullmatch(seg)  # pythonXY.zip\<file>
        if m is not None:
            v = (int(m["major"]), int(m["minor"]))
            if v not in seen:
                seen.append(v)
    return seen


def _layout_clash(failures: list["Failure"], layout: str) -> str:
    """r16 HUB RULING, the fail-closed TRIPWIRE: the cnc-layout reason when a traceback frame of the run's unittest blocks
    sits in a CPython library directory of the OTHER layout's version (<= 3.10 contradicts 3.11+, >= 3.11 contradicts
    legacy), or "". Read from the SAME saved output, never from the host interpreter; no version evidence = the declared
    layout stands. Only real frames are read (_ut_walk): a source line that quotes a library path is not evidence."""
    for f in failures:
        for file, line in f.frames:
            for v in _stdlib_versions(file):
                if (v <= _LAST_LEGACY) == (layout == LAYOUT_MODERN):
                    seen = "<= 3.10, whose unittest prints `name (module.Class)`" if v <= _LAST_LEGACY else \
                        ">= 3.11, whose unittest prints `name (module.Class.name)`"
                    return _Reason("cnc-layout", f"layout: read under --unittest-layout {layout}, but the traceback frame "
                                   f"`File {file[:160]!r}, line {line}` sits in the library of CPython {v[0]}.{v[1]} "
                                   f"({seen}); declare the layout of the interpreter that ran the tests")
    return ""


_NODE_SEPS = ("::",)  # a pytest node id `dir/file.py::Class::name[param]`: its last segment follows its last `::`
_DOTTED_SEPS = (".", "/", "\\")  # a unittest `module.Class.name`, or an expectation that holds no `::`
_PATH_SEPS = ("/", "\\")  # a file path: a frame, a `file:line` expectation, a module ERROR id, a SKIPPED location


def _bracket_before_last_segment(text: str, seps: tuple[str, ...]) -> bool:
    """True when `text` has a `[` before its last separator (r13 HUB RULING, r12 adversary f2b4440f A1-A3, Dot 04ceb61).
    Only a trailing `[...]` on the LAST segment is a parameter id; a `[` earlier sits in a class, file or directory name
    set through globals(), or the text after it is a parameter id that holds the separator (`test_x[a::b]`), and those
    two read the same (`TestA[x.py::test_y]`: class `TestA[x.py` and function `test_y]`, or a parameter id). Every
    reader that cuts at the FIRST `[` (_empty_segment, _norm_path, _split_top, _segs, _node_name, _summary_readings) is
    reached by an identity only after this check, so it is told which reading a text means only when there is one."""
    j = text.find("[")
    return j >= 0 and any(sep in text[j + 1:] for sep in seps)


def _bracket_reason(what: str, text: str) -> _Reason:
    return _Reason("cnc-bracket-before-last-segment", f"bracket: {what} {text[:60]!r} has a `[` before its last "
                   "separator: a class, file or directory name may hold `[` (set through globals()), so where its "
                   "segments end and its parameter id starts cannot be told")


def _expectation_seps(text: str) -> tuple[str, ...]:
    return _NODE_SEPS if "::" in text else _DOTTED_SEPS


def _node_seps(node: str) -> tuple[str, ...]:
    """A summary node `file.ext::name` is a node id; one with no `::` (a module-level ERROR) is a file path."""
    return _NODE_SEPS if "::" in node else _PATH_SEPS


def _empty_segment(text: str) -> bool:
    """True when an id has an empty segment (`a..b`, `a.`, `a::`, `::a`, `a//b`): nothing real prints one, so an
    id with one is never matched, only refused. A leading `/` (an absolute path) is not an empty segment. It reads only
    the text before the first `[`; every caller first refuses a `[` before the last separator (r13, f2b4440f A3), so
    every separator is in that text."""
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
    # r14: indices into `frames` of the frame headers printed directly next to another frame header (no source line between
    # them). Such a frame may be a line break inside a code object's name or file name; it never decides a file:line PASS.
    joined: set[int] = field(default_factory=set)


@dataclass
class Parsed:
    failures: list[Failure] = field(default_factory=list)
    red: bool = False
    green: bool = False
    mismatch: str = ""  # the runner's own failure count disagrees with what was readable: see _count_mismatch
    result: _Result | None = None  # the LAST result line, parsed whole (None: no result line, or a malformed one)
    layout_clash: str = ""  # r16: the cnc-layout reason (see _layout_clash), or ""


def _norm_path(text: str) -> list[str]:
    """Path segments of `text`. A trailing pytest parameter id (from the first `[`) stays inside the last
    segment, so `/` and `\\` in it never split. A path with a `[` before a later `/` or `\\` (a directory name holding
    `[`) never gets here: _bracket_before_last_segment refuses it at the expectation, the node, the frame (r13)."""
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
    """Split on `sep` before the pytest parameter id, which starts at the FIRST `[` and runs to the end of the text;
    whatever it holds (`]`, `/`, `.`, unbalanced brackets) stays inside the last part. A name may hold `[` (set through
    globals()), so the first `[` is the parameter id only when no `sep` follows it: every identity reaches this split
    only after _bracket_before_last_segment refused that (r13). A pytest section title (`Class.name[1.5]`) is the
    runner's own heading of a node that passed that check, so a `.` after its `[` belongs to the parameter id."""
    j = text.find("[")
    head, tail = (text, "") if j < 0 else (text[:j], text[j:])
    parts = head.split(sep)
    parts[-1] += tail
    return [p for p in parts if p]


_DIR, _FILE, _NAME, _ANY = "dir", "file", "name", "any"  # the KIND of an identity segment (r10 adversary A1)


def _segs(text: str) -> list[tuple[str, str]]:
    """Segments of a test id, lossless for its own syntax, each as (kind, text). A pytest node
    `dir/file.py::Class::name[param]` gives typed parts: directories, the file (its .py suffix dropped), then the
    names. A unittest `module.Class.method`, a pytest section title `Class.name`, or a `::` id whose head names no .py
    file gives untyped (_ANY) parts: the text alone does not say where a module ends and a class begins. Separators
    inside `[...]` never split.
    KINDS (r10 adversary A1): the `.py` suffix used to be dropped and the kind forgotten, so a class `TestOther` in
    test_s.py satisfied the expectation `TestOther.py::test_x`, which names another FILE. See _id_ends_with."""
    if "::" in text:
        parts = _split_top(text, "::")
        if not parts:
            return []
        head, names = parts[0], parts[1:]
        path = _norm_path(head)
        if path and path[-1].endswith(".py") and len(path[-1]) > 3:
            return ([(_DIR, p) for p in path[:-1]] + [(_FILE, path[-1][:-3])] + [(_NAME, n) for n in names])
        return [(_ANY, p) for p in path + names]
    if len(_norm_path(text)) > 1:
        path = _norm_path(text)
        if path and path[-1].endswith(".py") and len(path[-1]) > 3:
            return [(_DIR, p) for p in path[:-1]] + [(_FILE, path[-1][:-3])]
        return [(_ANY, p) for p in path]
    return [(_ANY, p) for p in _split_top(text, ".")]


def _id_ends_with(whole: list[tuple[str, str]], tail: list[tuple[str, str]]) -> bool:
    """_ends_with for typed id segments. Texts must be equal. An untyped segment of the EXPECTATION matches a
    segment of any kind (it says no more than its text). A typed one (a directory, a file, a name) matches only a
    segment of the same kind: the run's id must say the same thing, so a typed expectation never matches an untyped
    unittest id, whose module/class boundary the text does not show (fail closed: REFUSED, never a guessed PASS)."""
    if not whole or not tail or len(tail) > len(whole):
        return False
    for (t_kind, t_text), (w_kind, w_text) in zip(tail, whole[-len(tail):]):
        if t_text != w_text or (t_kind != _ANY and t_kind != w_kind):
            return False
    return True


def _summary_readings(line: str, word: str) -> list[tuple[str, str]] | None:
    """`FAILED <node> - <msg>` / `ERROR <node> - <msg>` -> EVERY (node, msg) reading of the line, the plain one first.
    Without a parameter id the plain reading ends the node at the first ' - '. r11 (r10 adversary A1, r11 pass 1 A1): a
    class or function name set through globals() may hold ' - ' (`TestA - AssertionError`), and an exception class of
    any name prints after it (type('Bad-Name', ...) prints `t.Bad-Name`), so EVERY later ' - ' whose left side is a node
    id is another reading. No grammar of the message can rule one out. With a parameter id, the node ends at a ' - '
    that follows a `]`: each such separator is a reading, and so is the whole text when the message ends in `]` (r10
    adversary pass 2 A1: at a narrow width pytest drops the message and prints the node id alone). More than one reading
    is resolved only by the run's own failure section (see _parse_pytest); otherwise the line is COULD-NOT-CHECK."""
    if not line.startswith(word + " "):
        return None
    rest = line[len(word) + 1:].rstrip()
    seps = [i for i in range(len(rest)) if rest.startswith(" - ", i)]
    if not seps:
        return [(rest, "")]
    param = rest.find("[")
    if param < 0 or seps[0] < param:
        # r12 A1-U (Dot e3f43d7; r11 pass 3 A2): a later left side that is a node id but for a non-ASCII or control
        # character is a reading too, never dropped; the caller refuses a line with such a reading (_unreadable_reading)
        cuts = [seps[0]] + [i for i in seps[1:] if _node_plausible(word, rest[:i])]
        return [(rest[:i], rest[i + 3:]) for i in cuts]
    bracket = [i for i in seps if i > param and rest[i - 1] == "]"]
    # r12 pass 2 A1 (c62bbb01): the first `[` may sit in a CLASS name (a nested class `test_target[x] - AssertionError: x`
    # set through globals()), and then the node goes on past it. A ' - ' whose left side is a node id (readable or not)
    # with a `::` after that `[` is a reading too, never dropped; the caller refuses an unreadable one.
    extra = [i for i in seps if i not in bracket and "::" in rest[param:i] and _node_plausible(word, rest[:i])]
    readings = [(rest[:i], rest[i + 3:]) for i in sorted(bracket + extra)]
    if not bracket or rest[bracket[0] + 3:].endswith("]"):
        readings.append((rest, ""))
    return readings


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
    the caller.
    r15 (Dot R18 B1 6715619691; honest output): 3.11+ prints module, class qualname AND method, so its group has at least
    THREE dotted segments. A group of two that ends in `.name` is the <= 3.10 header of a class named like its method
    (module t, class test_x, method test_x prints `test_x (t.test_x)`), so its one reading is `t.test_x.test_x`, never
    `t.test_x`. That reading still counts toward the more-than-one refusal above."""
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
            if _SUBTEST_SUFFIX.fullmatch(after):
                # `ident` ends in "." + name, so a dot count of one is the two-segment `module.name`: the legacy layout
                found.append((name, ident if ident.count(".", 0, k) else f"{ident}.{name}"))
            k = tail.find(needle, k + 1)
    return found


def _line_break(what: str, ln: str) -> _Reason:
    return _Reason("cnc-traceback-line-break", f"traceback line break: {what} ({ln.strip()[:60]!r}); a line break inside "
                   "a frame's function or file name prints a forged line here, so which line the runner wrote cannot be "
                   "told")


def _ut_walk(block: list[str]) -> tuple[list[tuple[str, int]], str, set[int], str]:
    """(frames, exception line, joined frames, line-break reason) of the REAL tracebacks in a unittest block. A traceback
    is entered only at `Traceback (most recent call last):` (the first, or one that follows a chaining line); inside it
    the indented `File "...", line N` lines are frames and the first unindented line is the exception line. Its message
    text is never read as a frame or an exception line, but (r14 tripwire, see _EXC_HEAD_SHAPE) a second column-0 line
    shaped like an exception head, or a frame header, in that exception region gives the line-break reason: the block
    is COULD-NOT-CHECK. It is the block's `odd` reason, so a more basic one (a green run, no result line) still leads."""
    frames: list[tuple[str, int]] = []
    joined: set[int] = set()
    broke = ""
    exc, in_tb, expect = "", False, True
    region, prev_frame = False, False  # inside an exception region; the last non-blank traceback line was a frame header
    for ln in block:
        if not in_tb:
            if ln.rstrip() == "Traceback (most recent call last):" and expect:
                in_tb, exc, expect, region, prev_frame = True, "", False, False, False
            elif ln.strip() in _CHAIN:
                expect, region = True, False
            elif region and not broke:
                if _DASHES.match(ln):
                    region = False  # the rule unittest prints after the last block: the runner's footer follows
                elif _FRAME_SHAPE.match(ln):
                    # r14 (a), part 2: a column-0 line (the exception line read above) before a frame header. The real
                    # exception line is printed after the last frame, so the earlier one may be a co_name continuation.
                    broke = _line_break("a traceback frame follows a column-0 line of its traceback", ln)
                elif ln[:1] not in ("", " ", "\t") and _EXC_HEAD_SHAPE.match(ln):
                    # r14 (a), part 1 (fde24fbc-A1): a second exception-head-shaped line: the real one is always printed,
                    # so a forged one before it (`test_x\nAssertionError: forged`) always trips this
                    broke = _line_break("a second exception line in one traceback", ln)
            continue
        if not ln.strip():
            continue
        if ln[:1].isspace():
            if not _FRAME_SHAPE.match(ln):
                prev_frame = False
                continue  # a source or caret line: what it says is never read (r12, Dot e3f43d7; r11 pass 3 A3)
            if ln.count('", line ') > 1:
                # r11 pass 2 A1: a function's co_name may hold `"/victim.py", line 2, in target`, so the line reads as two
                # frames and which `", line N` ends the file name cannot be told. Frame lines only (r12): a source line
                # `self.fail('", line 1, ", line 2')` is not a frame and is not refused.
                raise _Unclassifiable("cnc-frame", f"traceback frame {ln.strip()[:60]!r} holds more than one "
                                      "`\", line N`: which one ends its file name cannot be told")
            m = _FRAME.match(ln)
            if not m:
                raise _Unclassifiable("cnc-frame", f"traceback frame {ln.strip()[:60]!r}: its line number is not ASCII "
                                      "digits followed by the frame's own delimiter (`, in <name>` or the end of the line)")
            if prev_frame:  # r14 (b) (fde24fbc-A2): two frame headers with no source line between them
                joined.update((len(frames) - 1, len(frames)))
            frames.append((m["file"], _line_no(m["line"])))
            prev_frame = True
            continue
        exc, in_tb, region = ln, False, True
    return frames, exc, joined, broke


def _parse_unittest(lines: list[str], layout: str = LAYOUT_MODERN) -> list[Failure]:
    """The unittest failure blocks of `lines`. Under the declared ``legacy`` layout (r16 hub ruling) a header group is
    read ONLY as `module.Class` (the fallback below): no 3.11+ reading is made."""
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
        frames, exc_line, joined, broke = _ut_walk(block)
        collection = ("_FailedTest" in body[0]
                      or any("unittest/loader.py" in ln.replace("\\", "/") or _IMPORT_ERR.match(ln) for ln in block))
        if len(head["rest"]) > _UT_HEADER_MAX:  # adversary A2: bounded work, and no real test name is this long
            raise _Unclassifiable("cnc-identity", f"unittest header of {len(head['rest'])} characters is longer than "
                                  f"{_UT_HEADER_MAX}: its test identity is not read")
        readings = _ut_identities(head["rest"]) if layout == LAYOUT_MODERN else []
        if readings:
            name, full = readings[0]
        else:  # older Pythons: `name (module.Class)` (a two-segment `module.name` group is read in _ut_identities, r15),
            # and every header under the declared legacy layout (r16)
            # The module part is the LAST parenthesised dotted group; an earlier group belongs to the name. r14 fix 2
            # (716e6018-A1): only a group the subTest suffix (or nothing) follows, so a group inside `[msg]` or `(params)`
            # is never the class, and a 3.11+ identity with another suffix is never read as `module.Class` plus a name.
            groups = [g for g in re.finditer(r" \(([A-Za-z_][\w.]*)\)", head["rest"])
                      if _SUBTEST_SUFFIX.fullmatch(head["rest"][g.end():])]
            dotted = [g for g in groups if "." in g.group(1)]
            pick = (dotted or groups or [None])[-1]
            name = head["rest"][:pick.start()] if pick else head["rest"]
            full = f"{pick.group(1)}.{name}" if pick else name
        ids = [] if "unittest.loader" in full else [full]
        odd = ""
        if len(readings) > 1:
            odd = _Reason("cnc-identity", f"ambiguous identity: the header {head['rest'][:60]!r} reads as "
                          f"{len(readings)} different test identities")
        elif ids and any(ch in name for ch in "./\\:"):
            odd = _Reason("cnc-identity", f"separator in the method name {name[:40]!r}: its identity would split into "
                          "invented segments")
        elif ids and _bracket_before_last_segment(full, _DOTTED_SEPS):  # r13 (f2b4440f A3: `t.T[x]..Oops.test_a`)
            odd = _bracket_reason("the unittest identity", full)
        elif ids and (_empty_segment(full) or any(tok in full for tok in ("/", "\\", "::"))):
            odd = _Reason("cnc-identity", f"empty or foreign segment in the test identity {full[:60]!r}")
        elif broke:  # r14 (a): the traceback's exception region holds a second exception line or a frame (see _ut_walk)
            odd = broke
        # FAIL is only unittest's word for "failureException was raised", and a test may set that to any
        # class: the assertion claim rests on the exception actually printed.
        is_assertion = bool(_UT_ASSERT.match(exc_line))
        out.append(Failure(ids, frames, assertion=head["kind"] == "FAIL" and is_assertion and not collection,
                           collection=collection, note=body[0].strip(), odd=odd, exc=exc_line.strip(), joined=joined))
    return out


def _same_file(a: list[str], b: list[str]) -> bool:
    return _ends_with(a, b) or _ends_with(b, a)


def _pair_nodes(want: list[tuple[str, str]], frames: list[tuple[str, int]], nodes: list[str]) -> list[int]:
    """Indices of the FAILED occurrences (`nodes`, in summary order) a section with test name `want` and traceback
    `frames` may be. More than one is an ambiguous pairing, which the caller refuses (r9 hub ruling)."""
    cands = [i for i, n in enumerate(nodes) if _id_ends_with(_segs(n), want)]
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
            notes.append(_Reason("cnc-pairing", f"pairing: the FAILED node id {node[:60]!r} occurs {k} times "
                                 "(--keep-duplicates?): which failure section is which occurrence, and so which "
                                 "exception belongs to which, cannot be told"))
    groups: dict[tuple[str, ...], dict[tuple[str, ...], str]] = {}
    for node in seen:
        parts = _split_top(node, "::")
        if not parts:
            continue
        group = groups.setdefault(tuple(parts[1:]), {})
        rev = tuple(reversed(_norm_path(parts[0])))
        if rev in group:  # the same file spelled two ways (`./a/t.py` and `a/t.py`)
            notes.append(_Reason("cnc-pairing", f"pairing: FAILED {group[rev][:60]!r} and {node[:60]!r} name the same "
                                 "test in the same file"))
            continue
        group[rev] = node
    for group in groups.values():
        for rev, node in group.items():
            for k in range(1, len(rev)):
                other = group.get(rev[:k])
                if other is not None:
                    notes.append(_Reason("cnc-pairing", f"pairing: FAILED {other[:60]!r} and {node[:60]!r} share a test "
                                         "name and one file path is a suffix of the other: which failure section is "
                                         "which occurrence cannot be told"))
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
_SKIPPED_BODY = re.compile(rf"\[({_COUNT})\] [!-~][ -~]*?:[0-9]+: [ -~]*")  # the [n] group count: the one count spelling


# The node SHAPE without the ASCII/printable test (r12 A1-U): `<file>.<ext>::<name>` (or, for ERROR, `<file>.<ext>`) of
# any characters. A real node id may hold any (`test_é`, a tab, U+200B set through globals()), so a left side of this
# shape is a plausible boundary whatever it holds; that it cannot be READ is a refusal, not a reason to drop it.
_PLAUSIBLE_NODE = re.compile(r".+?\.\w+::.+")  # one framed line: no line break to match
_PLAUSIBLE_MODULE = re.compile(r".+?\.\w+")


def _node_plausible(word: str, text: str) -> bool:
    """True when `text` could be the node id of a `word` summary line, readable or not: `<file.ext>::<name>` (or, for
    ERROR, a module `<file.ext>`) of any characters. The readable subset is the ASCII printable one (_summary_problem)."""
    return bool(_PLAUSIBLE_NODE.fullmatch(text) or (word == "ERROR" and _PLAUSIBLE_MODULE.fullmatch(text)))


def _unreadable_reading(word: str, line: str, readings: list[tuple[str, str]]) -> str:
    """"" or why a FAILED/ERROR line with more than one reading cannot be read: one reading's node id holds a non-ASCII
    or control character (r12 A1-U, Dot e3f43d7). Such a boundary is unsupported and plausible, so the line is
    COULD-NOT-CHECK whichever reading a section would name: an ASCII prefix of it is never the node by elimination."""
    if len(readings) < 2:
        return ""
    for node, _msg in readings:
        if not node.isascii() or not node.isprintable():
            return (f"{word} line {line[:60]!r} splits at a later ' - ' whose left side {node[:60]!r} is a node id but for "
                    "a non-ASCII or control character: that id cannot be read, so which reading is the node cannot be told")
    return ""


def _bare_section_name(name: str) -> str:
    """A pytest section header's test name without its `ERROR at setup of ` / `ERROR at teardown of ` prefix (a prefix
    only: a parameter id may hold this text)."""
    for prefix in ("ERROR at setup of ", "ERROR at teardown of "):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def _node_name(node: str) -> str:
    """The test name a pytest section header prints for `node` (`t.py::A::b[p]` -> `A.b[p]`), or "" for a module id."""
    return ".".join(_split_top(node, "::")[1:])


def _section_tells(section: Failure, node: str) -> bool:
    """True when a failure section may be the account of the summary reading `node`: its header prints exactly that
    node's test name (`Class.name`, `name[param]`) AND its FIRST frame, the test function's own, sits in that node's file
    (r12, r11 pass 3 A1: u.py's `TestA.test_target` section named t.py's invented reading by its name alone; r12 pass 1
    A1: a helper frame in t.py lower in that section is not the test's file). A section with no frame tells nothing. The
    caller also refuses a section whose name another FAILED line prints (see _parse_pytest)."""
    name = _node_name(node)
    if not name or not section.ids or not section.frames or name != _bare_section_name(section.ids[0]):
        return False
    return _same_file(_norm_path(_split_top(node, "::")[0]), _norm_path(section.frames[0][0]))


# r14 (Dot 4153241): the longest short-summary line whose id and reason are scanned. A longer one is COULD-NOT-CHECK
# (`cnc-short-summary`); at this length the per-prefix checks of _other_line (PASSED/SKIPPED/XFAIL/XPASS) and of
# _summary_readings (every ' - ' of a FAILED/ERROR line, the same class) stay a few milliseconds.
_SUMMARY_SCAN_MAX = 4096


def _other_line(word: str, line: str) -> tuple[int, str]:
    """(how many tests the line stands for, "") for a PASSED/SKIPPED/XFAIL/XPASS summary line of exactly the shape pytest
    prints under ``-rA``; (0, reason) for anything else. ASCII only, so no wide or invisible character can hide a line
    break or a second id inside it."""
    body = line[len(word) + 1:]
    if not line.isascii() or not line.isprintable():
        return 0, f"{word} line with a non-ASCII or control character"
    if len(line) > _SUMMARY_SCAN_MAX:
        # r14 (Dot 4153241, :845/:859): every candidate end of the location or id below is checked as its own prefix, so
        # the work grows with the square of the line; a line this long is refused before any of it is read
        return 0, f"a {word} line of {len(line)} characters is longer than {_SUMMARY_SCAN_MAX}: its reason is not scanned"
    if word == "SKIPPED":
        m = _SKIPPED_BODY.fullmatch(body)
        if not m:
            return 0, "a SKIPPED line that is not `SKIPPED [n] file:line: reason`"
        # r13 pass 1 A1 (11a64a80): the file ends at ANY `:<line>: `, not at the first `:` (a drive letter `C:\a[x]\t.py`
        # was read as `C`), so EVERY reading of it is checked; raised, so the bracket code is the primary one
        rest = body[m.end(1) + 2:]
        wheres = [rest[:k.start()] for k in re.finditer(r":[0-9]+: ", rest)]
        for where in wheres:
            if _bracket_before_last_segment(where, _PATH_SEPS):
                raise _Unclassifiable("cnc-bracket-before-last-segment", _bracket_reason("the SKIPPED location", where))
        if any(_empty_segment(where) for where in wheres):
            return 0, "a SKIPPED line whose file path has an empty segment"
        return int(m[1]), ""
    if not _OTHER_NODE.fullmatch(body):
        return 0, f"a {word} line that is not `{word} <file>::<name>`"
    if word == "PASSED" and " - " in body:
        return 0, "a PASSED line holding ' - ' (a real one is `PASSED <id>` alone)"
    # r13 pass 1 A2 (11a64a80): an XFAIL/XPASS id may hold ' - ' (in its parameter id, or a class name set through
    # globals()), so the id ends at ANY ' - ' or runs to the end of the line: EVERY reading of it is checked, never only
    # the text before the first ' - ' (the reason text is then checked too, which only ever refuses)
    nodes = [body[:k.start()] for k in re.finditer(" - ", body)] + [body]
    for node in nodes:
        if _bracket_before_last_segment(node, _node_seps(node)):  # r13 (f2b4440f A3: `PASSED t.py::TestB[x]::test_x::[p]`)
            raise _Unclassifiable("cnc-bracket-before-last-segment", _bracket_reason(f"the {word} id", node))
    if any(_empty_segment(node) for node in nodes):
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
    reading_names: list[tuple[int, str]] = []  # (summary line number, the test name of one of its readings)
    told: list[tuple[int, str, str]] = []  # (summary line number, the name of the reading a section told, the line)
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
                               exc=cur["first"], joined=cur["joined"]))
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
            if word in ("FAILED", "ERROR") and len(line) > _SUMMARY_SCAN_MAX:
                # r14 (the FAILED/ERROR sibling of Dot 4153241 :845/:859): every ' - ' is a reading, each checked as its own
                # prefix; the line still counts, and is refused before any reading of it is built
                stats["summary"] += 1
                stats["bad"] = stats["bad"] or (f"a {word} line of {len(line)} characters is longer than "
                                                f"{_SUMMARY_SCAN_MAX}: its node id and message are not scanned")
                continue
            for word in ("FAILED", "ERROR"):
                readings = _summary_readings(line, word)
                if not readings:
                    continue
                stats["summary"] += 1
                stats["bad"] = stats["bad"] or _unreadable_reading(word, line, readings)
                line_no = stats["summary"]
                # r13 HUB RULING (f2b4440f A1, A2, A3): a node with a `[` before its last `::` is COULD-NOT-CHECK. The one
                # reading of a line is refused at once; a line with more than one reading is refused already (below), and
                # one of its readings with such a `[` keeps every section from telling it (_node_name cannot say which
                # heading that reading prints), so it stays COULD-NOT-CHECK.
                bracketed = [r[0] for r in readings if _bracket_before_last_segment(r[0], _node_seps(r[0]))]
                if bracketed and len(readings) == 1:
                    raise _Unclassifiable("cnc-bracket-before-last-segment", _bracket_reason(f"the {word} node",
                                                                                             bracketed[0]))
                reading_names.extend((line_no, _node_name(r[0])) for r in readings)
                if len(readings) > 1 and not bracketed:
                    # r11: the run's own failure sections (they precede the summary, so `out` holds them) name their
                    # tests; a reading is told only when EXACTLY ONE of them is the account of a section, by its test
                    # name AND its file (r12, r11 pass 3 A1: a name alone may belong to another file's section).
                    tells = [(r, f) for r in readings for f in out if _section_tells(f, r[0])]
                    if len({r for r, _f in tells}) == 1:
                        told.append((line_no, _node_name(tells[0][0][0]), line))
                        readings = [tells[0][0]]
                node, msg = readings[0]
                ambiguous = len(readings) > 1
                stats["bad"] = stats["bad"] or _summary_problem(word, node, msg)
                if word == "FAILED":
                    nodes.append((node, msg))
                    if _empty_segment(node):
                        odd_notes.append(_Reason("cnc-identity", f"empty segment in the test identity {node[:60]!r}"))
                if ambiguous and _IMPORT_ANYWHERE.search(line):
                    # every reading of the line names an import error: it blocks like one, whichever is the node
                    summary_blockers.append(f"{word} {node[:60]} - {msg[:40]}")
                elif ambiguous:
                    # r11 (r10 adversary A1): a split with more than one reading is COULD-NOT-CHECK, never a guessed
                    # node id and account; the line still blocks PASS like a collection error (both are refusals).
                    odd_notes.append(_Reason("cnc-summary-split", f"summary split: the {word} line {line[:70]!r} splits "
                                             "into node id and message at more than one ' - ' (a name holding ' - ' set "
                                             "through globals(), or a parameter id holding '] - '): which is the node "
                                             "cannot be told"))
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
                   "collection": name.startswith("ERROR collecting"), "joined": set(), "prev_loc": False}
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
        # r14 (b): was the previous non-blank line of this section a location line? Real pytest never prints two in a row
        # (long: source, `E` lines, then the location; short: the location, then its source or `???`)
        was_loc = cur["prev_loc"]
        if line.strip():
            cur["prev_loc"] = False
        if line.startswith("E") and line[1:2] in (" ", "\t"):
            first = line[1:].strip()
            if cur["assertion"] is None:
                cur["assertion"] = bool(_ASSERT_HEAD.match(first))
                cur["first"] = first
            if _IMPORT_ERR.match(first):  # any E line: a chained exception counts too
                cur["collection"] = True
            continue
        m_loc = _PT_LOC.match(line)
        if m_loc and len(re.findall(r"\.py:\d+:", line)) > 1:
            # r11 pass 2 A1 (pytest sibling): `victim.py:2: sub/real.py:3: AssertionError` (a directory named
            # `victim.py:2: sub`) reads as two locations; which one is the frame cannot be told
            raise _Unclassifiable("cnc-frame", f"location line {line.strip()[:60]!r} holds more than one `<file>.py:N:`: "
                                  "which one is the frame cannot be told")
        if m_loc:
            if was_loc:  # r14 (b) (fde24fbc-A2 pytest sibling: `victim.py:2: in injected` then `real.py:1: AssertionError`)
                cur["joined"].update((len(cur["frames"]) - 1, len(cur["frames"])))
            cur["frames"].append((m_loc["file"], _line_no(m_loc["line"])))
            cur["prev_loc"] = True
        elif _PT_LOC_SHAPE.match(line):
            raise _Unclassifiable("cnc-frame", f"location line {line.strip()[:60]!r}: its line number is not ASCII digits "
                                  "followed by the runner's own `:` delimiter")
    close()
    for line_no, name, line in told:
        # r12 pass 1 A1: the section that told a split may be another line's account (an inherited test of u.py whose
        # first frame is the base class in t.py). A section is the account of ONE line, so a name that another FAILED or
        # ERROR line also prints (in any of its readings) tells nothing, and the split stays COULD-NOT-CHECK.
        if any(other != line_no and other_name == name for other, other_name in reading_names):
            odd_notes.append(_Reason("cnc-summary-split", f"summary split: the line {line[:70]!r} was told by a failure "
                                     f"section named {name[:40]!r}, a name another summary line prints too: which line "
                                     "that section is the account of cannot be told"))
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
        bare = _bare_section_name(f.ids[0])
        # The section header is a bare name (`T.test_a`, or just `test_x` for a module-level
        # function), so it is not an identity. Its identity is the FAILED-summary node id whose
        # tail is that name and whose file appears in the section's own traceback frames.
        idx = _pair_nodes(_segs(bare), f.frames, names)
        if len(idx) > 1:
            odd_notes.append(_Reason("cnc-pairing", f"pairing: the failure section {bare[:60]!r} pairs with {len(idx)} "
                                     "FAILED occurrences (" + ", ".join(repr(names[i][:60]) for i in idx[:4])
                                     + "): which one is its account cannot be told"))
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
        return _Reason("cnc-exception-type", "no exception line is shown for a failing test (an exception group prints "
                       "none): its class may subclass ImportError, which the run does not say")
    if not text or _ASSERT_HEAD.match(text):
        return ""
    token = re.match(r"[\w.]+", text)
    name = token.group(0) if token else text[:40]
    after = text[len(name):]
    if token and name in _EXC_NAMES and (after in ("", ":") or after.startswith(": ")):  # r11: `TypeError:x` is not one
        return ""  # an exact name from the fixed list (ImportError and its builtin subclasses are not on it)
    return _Reason("cnc-exception-type", f"exception type {name[:40]!r} of a failing test is not on the fixed list of "
                   "builtin names: its class may subclass ImportError, which the run does not say")


def _last_result(lines: list[str]) -> str | None:
    """The LAST framed line that has the SHAPE of a runner result line (_RESULT_LINE), stripped; None when there is none.
    Its fields are read only by _read_result."""
    last = None
    for ln in lines:
        if _RESULT_LINE.match(ln):
            last = ln.strip()
    return last


# The unittest footer, parsed whole like the trailer (r11): `Ran N test(s)` with the one count spelling and an optional
# elapsed time. A footer of another spelling (`Ran 1.5 tests`) is not the runner's, so the trailer has no footer.
_UT_RAN = re.compile(rf"^Ran {_COUNT} tests?(?: in [0-9]+(?:\.[0-9]+)?s)?[ \t]*$")


def _count_mismatch(lines: list[str], ut_blocks: int, stats: dict, result: _Result | None) -> str:
    """The runner states how many tests failed; every one must have been recognized as a block, section or summary
    line. A header this parser could not read (a multi-line test name) would otherwise drop its failure, an import
    error included, without a trace. "" when the counts agree, or when the text holds neither a failure nor a result
    line (the plain checks refuse that as unrecognized). A text WITH failures and NO result line is never "" (r8).

    The pytest short summary is judged as a CLASS (r5 hub ruling), typed ``short-summary line``: a FAILED/ERROR line
    that does not split into exactly ``<file>::<name> - <message>`` (see _summary_problem), a line that starts with
    no summary word, or a number of FAILED/ERROR lines that differs from the runner's own failed+errored count."""
    if stats["bad"]:
        return _Reason("cnc-short-summary", f"short-summary line: {stats['bad']}")
    if stats["stray"]:
        return _Reason("cnc-short-summary", "short-summary line: a line starts with no summary word (a multi-line test id?)")
    if ut_blocks or stats["sections"] or stats["has_summary"] or stats["summary"]:
        # R7-C1 (r8): failures were read, so the run must END in the runner's own result: the LAST non-blank line is a
        # result line (adversary A1: a result-shaped line in the middle of a cut log, printed by a failing test or pasted
        # in, is message text), and a unittest `OK`/`FAILED (...)` trailer has its `Ran N tests` footer before it.
        # Otherwise the runner's counts are missing and a later failure (an import error included) may have been cut.
        body = [ln for ln in lines if ln.strip()]
        final = body[-1] if body else ""  # NOT stripped: a real result line starts at column 0 (adversary pass 2, A2)
        if not _RESULT_LINE.match(final):
            return _Reason("cnc-no-result-line", "no final result line (the run must end in a unittest `OK`/`FAILED (...)` "
                           "trailer or a pytest `=== N failed ... ===` bar): a cut-off log cannot be reconciled with the "
                           "runner's counts")
        if not final.startswith("=") and not any(_UT_RAN.match(ln) for ln in body[:-1]):
            return _Reason("cnc-no-ran-footer", "no final result line the counts can be read from: the unittest trailer "
                           "has no `Ran N tests` footer before it")
    if result is None:
        return ""
    counts = result.counts  # parsed WHOLE by _read_result (r11): the only counts read below, never a rescan of the line
    pt_read = stats["sections"] or stats["summary"] or stats["has_summary"]
    # ONE RUN, ONE RUNNER (r9 adversary pass 2, A1): the terminal result reconciles only its own runner's failures, so a
    # failure read by the other runner's parser would never be counted. Either kind under the other's trailer refuses.
    if result.runner == "unittest" and not result.green and pt_read:
        return _Reason("cnc-runner", "runner: a unittest `FAILED (...)` trailer closes a run in which pytest failure "
                       "sections or summary lines were read; one run has one runner, and these failures are not in its count")
    if result.runner == "pytest" and ut_blocks:
        return _Reason("cnc-runner", "runner: a pytest result bar closes a run in which unittest failure blocks were read; "
                       "one run has one runner, and these failures are not in its count")
    if result.runner == "unittest" and not result.green and any(_UT_RAN.match(ln) for ln in lines):
        want = counts.get("failures", 0) + counts.get("errors", 0)
        if want != ut_blocks:  # a stated 0 counts too: `FAILED (failures=0)` over a readable block is a contradiction
            return _Reason("cnc-count", f"the runner counts {want} failing unittest tests but {ut_blocks} failure blocks "
                           "were readable")
    elif result.runner == "pytest":
        want = counts.get("failed", 0) + counts.get("errors", 0)
        if stats["sections"] and stats["sections"] != want:
            return _Reason("cnc-count", f"the runner counts {want} failed or errored tests but {stats['sections']} "
                           "sections were readable")
        if stats["has_summary"] and stats["summary"] != want:
            return _Reason("cnc-short-summary", f"short-summary line: the runner counts {want} failed or errored tests "
                           f"but {stats['summary']} FAILED/ERROR lines were readable")
        for word, label in (("PASSED", "passed"), ("XFAIL", "xfailed"), ("XPASS", "xpassed"), ("SKIPPED", "skipped")):
            # ALLOWLIST (r7): only a run that lists every one of these outcomes can be reconciled, which is what
            # ``-rA`` prints. A multi-line test id can start a continuation line with any summary word, and a default
            # (``-rfE``) run prints no PASSED/SKIPPED/XFAIL/XPASS line at all, so a forged one could stand in for the
            # real ones the runner counts. The lines must add up to the runner's own count, zero included.
            shown = counts.get(label, 0)
            if stats["words"][word] != shown:  # also with no summary section at all (-rN): the passes are still unlisted
                hint = (": this summary lists none of them, so the run was not made with -rA (default reporting omits "
                        "PASSED/SKIPPED/XFAIL/XPASS lines)" if not stats["words"][word] else " (a multi-line test id?)")
                return _Reason("cnc-short-summary", f"short-summary line: {stats['words'][word]} {word} lines were read "
                               f"but the runner counts {shown} {label}{hint}")
    return ""


def parse_run(text: str | list[str], *, unittest_layout: str = LAYOUT_MODERN) -> Parsed:
    """Parse a run given as text (framed here, see _frame) or as the lines _frame already returned. The LAST result line
    is parsed whole ONCE (_read_result); a malformed one is the run's mismatch, and its counts are the only ones that
    _count_mismatch and the green check read. Its unittest blocks are read under the declared ``unittest_layout``."""
    layout = _check_layout(unittest_layout)
    lines = _frame(text) if isinstance(text, str) else text
    stats: dict = {}
    ut = _parse_unittest(lines, layout)
    failures = ut + _parse_pytest(lines, stats)
    for f in failures:  # r13: a frame file is a path, and _norm_path cuts it at its first `[` (see _bracket_before_last_segment)
        for file, _line in f.frames:
            if _bracket_before_last_segment(file, _PATH_SEPS):
                raise _Unclassifiable("cnc-bracket-before-last-segment", _bracket_reason("the frame file", file))
    last, result, bad = _last_result(lines), None, ""
    if last is not None:
        try:
            result = _read_result(last)
        except _Unclassifiable as exc:
            bad = exc.reason
    return Parsed(failures=failures, red=any(_RED_MARK.search(ln) for ln in lines) or bool(failures),
                  green=any(_GREEN_MARK.search(ln) for ln in lines),
                  mismatch=bad or _count_mismatch(lines, len(ut), stats, result), result=result,
                  layout_clash=_layout_clash(ut, layout))


def _site_frames(failure: Failure, site: re.Match) -> list[int]:
    """Indices of the failure's frames that a `file:line` expectation names."""
    want_file, want_line = _norm_path(site["file"]), _line_no(site["line"])
    return [k for k, (file, line) in enumerate(failure.frames)
            if line == want_line and _ends_with(_norm_path(file), want_file)]


def _matches(failure: Failure, expected: str) -> bool:
    site = _EXPECT_SITE.match(expected)
    if site:
        return bool(_site_frames(failure, site))
    want = _segs(expected)
    return any(_id_ends_with(_segs(i), want) for i in failure.ids)


def _precheck(lines: list[str] | None, expected: str, layout: str = LAYOUT_MODERN) -> str:
    """The typed reason this text will not be classified, or "" (see NARROWED PROMISE in the module docstring). A
    reason returned here is a _Reason: it carries its code."""
    if lines is None or not isinstance(expected, str) or not expected.strip():
        return ""  # the plain checks below refuse these with their own reasons
    if any(ln.strip() in _CHAIN for ln in lines):
        return _Reason("cnc-chained", "chained exception in the output: which exception the run reports is not decided here")
    if any(_CAPTURED_ANY.search(ln) for ln in lines):
        return _Reason("cnc-captured", "captured output in the run: the test's own text follows the divider and can say "
                       "anything")
    site = _EXPECT_SITE.match(expected)
    # r13 HUB RULING (f2b4440f A2, Dot 04ceb61: `TestA[x].py::test_x[p]` lost its FILE kind at the first `[`): checked
    # before the empty-segment check, which cuts at the first `[` too
    if site and _bracket_before_last_segment(site["file"], _PATH_SEPS):
        return _bracket_reason("the expected file path", site["file"])
    if not site and _bracket_before_last_segment(expected, _expectation_seps(expected)):
        return _bracket_reason("the expected test identity", expected)
    if site:
        _line_no(site["line"])
        if _empty_segment(site["file"]):  # r11 pass 2 A3: `a//t.py:2` lost its empty segment in _norm_path and matched
            return _Reason("cnc-expectation", "empty segment in the expected file path (`//`, `\\\\`): no such file "
                           "exists")
    elif _empty_segment(expected):
        return _Reason("cnc-expectation", "empty segment in the expected test identity (`..`, a trailing `.` or `::`): no "
                       "such id exists")
    run = parse_run(lines, unittest_layout=layout)
    return run.mismatch or run.layout_clash  # r16: a count problem is the more basic one and leads


@dataclass(frozen=True)
class Verdict:
    """One classification. ``reason`` is the PRIMARY reason, the one the status was decided for. ``also`` holds the
    secondary diagnostics: what the older checks would have said about text the precheck already refused. A diagnostic
    never decides anything, so a check that pins WHY a run was refused reads ``reason`` only (r10, Dot R8-T1: a
    semantic reason matched inside an appended diagnostic hid the format refusal that actually decided the case).
    ``code`` is the checker's own name for the primary reason (REASON_CODES), set at the site that wrote it (r11, r10
    adversary A2): a reason's TEXT quotes its input, so a check that pins why compares ``code``, never text."""
    status: str
    reason: str
    also: tuple[str, ...] = ()
    code: str = "cnc-internal-error"
    layout: str = LAYOUT_MODERN  # r16: the unittest layout the run was read under (declared by the caller)


def verdict(output: str, expected: str, *, unittest_layout: str = LAYOUT_MODERN) -> tuple[str, str]:
    """Return (PASS|REFUSED, text): the primary reason, then each diagnostic after ``; also: `` (see classify). PASS
    only when `expected` is an assertion that failed. Text it cannot classify with certainty is REFUSED with a reason
    that starts ``COULD-NOT-CHECK`` (NARROWED PROMISE, module docstring). For display: pin a reason on classify().code."""
    v = classify(output, expected, unittest_layout=unittest_layout)
    return v.status, v.reason + "".join(f"; also: {a}" for a in v.also)


def classify(output: str, expected: str, *, unittest_layout: str = LAYOUT_MODERN) -> Verdict:
    """The verdict with its primary reason, its code and its secondary diagnostics kept apart (see Verdict). The run's
    unittest headers are read under ``unittest_layout`` (``"3.11+"``, the default, or ``"legacy"`` for CPython <= 3.10;
    r16 hub ruling); any other value is ValueError. ``Verdict.layout`` names the layout used."""
    layout = _check_layout(unittest_layout)
    pre, status, reason = "", REFUSED, ""
    try:
        lines = _frame(output) if isinstance(output, str) else None  # the ONE framing; every reader gets these lines
        pre = _precheck(lines, expected, layout)
        status, reason = _verdict(lines, expected, layout)
    except _Unclassifiable as exc:
        # a count or summary problem found by the precheck is the more basic one: it leads, the other follows
        return Verdict(REFUSED, f"COULD-NOT-CHECK ({pre or exc})", (str(exc),) if pre else (),
                       pre.code if pre else exc.code, layout)
    except Exception as exc:  # noqa: BLE001 - the promise is a refusal, never an uncaught traceback
        return Verdict(REFUSED, f"COULD-NOT-CHECK (internal-error {type(exc).__name__} while reading the text)",
                       code="cnc-internal-error", layout=layout)
    if pre:
        return Verdict(REFUSED, f"COULD-NOT-CHECK ({pre})", (reason,) if status == REFUSED else (), pre.code, layout)
    return Verdict(status, reason, code=reason.code, layout=layout)


def _verdict(lines: list[str] | None, expected: str, layout: str = LAYOUT_MODERN) -> tuple[str, _Reason]:
    if not expected or not expected.strip():
        return REFUSED, _Reason("no-expectation", "no expected assertion given (file:line or test id): a red suite proves "
                                "nothing alone")
    if lines is None:
        raise TypeError("the run output is not text")  # verdict() refuses it as an internal error, as before r9
    run = parse_run(lines, unittest_layout=layout)  # the precheck parsed these lines already: it raised anything first
    if run.result is not None and run.result.green:  # the LAST result line, parsed whole (r11)
        return REFUSED, _Reason("final-green", "the run is green (final result line): failure-looking text above it was "
                                "test output, so no assertion failed")
    for f in run.failures:
        if f.odd:
            raise _Unclassifiable(f.odd.code, f.odd)
    if not run.failures and not run.red:
        if run.green:
            return REFUSED, _Reason("green", "the run is green: the mutation was not detected by any assertion")
        return REFUSED, _Reason("unrecognized", "unrecognized output: no unittest or pytest result found, so nothing is "
                                "proven")
    if not run.failures:
        return REFUSED, _Reason("red-no-assertion", "the run is red but names no failing assertion (no FAIL/FAILED block "
                                "with a location)")
    broken = [f for f in run.failures if f.collection]
    if broken:
        return REFUSED, _Reason("collection", "collection or import error in the run (" + broken[0].note[:80]
                                + "): red for a reason that is not the assertion")
    for f in run.failures:
        why = _foreign_type(f)
        if why:
            raise _Unclassifiable(why.code, why)
    hit = [f for f in run.failures if _matches(f, expected)]
    if not _EXPECT_SITE.match(expected):
        named = sorted({i for f in hit for i in f.ids})
        if len(named) > 1:
            return REFUSED, _Reason("ambiguous-expectation", f"{expected} is ambiguous: it matches {len(named)} failing "
                                    "tests (" + ", ".join(named[:4]) + "); name the class")
    site = _EXPECT_SITE.match(expected)
    for f in hit:
        # r14 (b): the frame that DECIDES a file:line PASS was printed directly next to another frame header, so it may be
        # a line break inside a code object's name or file name (fde24fbc-A2). A frame that decides nothing is not read.
        if f.assertion and site and any(k in f.joined for k in _site_frames(f, site)):
            why = _line_break(f"the frame that decides {expected} is printed directly next to another frame header, with "
                              "no source line between them", expected)
            raise _Unclassifiable(why.code, why)
    if any(f.assertion for f in hit):
        return PASS, _Reason("assertion-failed", f"assertion failed at {expected}")
    if hit:
        return REFUSED, _Reason("non-assertion", f"{expected} raised a non-assertion error ({hit[0].note[:80]}): the "
                                "mutation did not fail the assertion")
    others = sorted({f.ids[0] if f.ids else f.note for f in run.failures})
    return REFUSED, _Reason("not-at", f"the run is red but not at {expected}; it failed at: " + ", ".join(others[:5]))


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
    v = probe_classify(bug_value, fix_value, window)
    return v.status == PASS, v.reason


def probe_classify(bug_value: float, fix_value: float, window: tuple[float, float]) -> Verdict:
    """probe_discriminates as a Verdict with its reason code (r11): PASS only when the probe separates bug from fix."""
    def no(code: str, text: str) -> Verdict:
        return Verdict(REFUSED, text, code=code)

    try:
        if type(window) not in (list, tuple) or len(window) != 2:  # a dict's keys or a 2-character string are not bounds
            raise TypeError("window must be a list or tuple of exactly two bounds")
        lo, hi = window
        bug_value, fix_value, lo, hi = _num(bug_value), _num(fix_value), _num(lo), _num(hi)
    except (TypeError, ValueError, OverflowError):
        return no("probe-number", "COULD-NOT-CHECK (number: probe readings and window must be numbers a float can hold, "
                  "given as an int, a float or ASCII numeric text)")
    if any(isinstance(v, float) and math.isnan(v) for v in (bug_value, fix_value, lo, hi)):
        return no("probe-nan", "NaN reading or window bound")
    if any(isinstance(v, float) and math.isinf(v) for v in (bug_value, fix_value)):
        return no("probe-infinite", "COULD-NOT-CHECK (number: a probe reading must be finite; only a window bound may be "
                  "infinite)")
    if lo > hi:
        return no("probe-empty-window", f"empty window ({lo} > {hi})")
    bug_in, fix_in = lo <= bug_value <= hi, lo <= fix_value <= hi
    if bug_in and fix_in:
        return no("probe-both-inside", f"bug reading {bug_value} and fix reading {fix_value} both sit inside "
                  f"[{lo}, {hi}]: the assertion passes under both")
    if not bug_in and not fix_in:
        return no("probe-neither-inside", f"neither reading sits inside [{lo}, {hi}]: the assertion fails under the fix")
    if bug_in:
        return no("probe-bug-inside", f"only the bug reading {bug_value} sits inside [{lo}, {hi}]: the assertion passes "
                  "under the bug and fails under the fix")
    return Verdict(PASS, f"fix reading {fix_value} inside [{lo}, {hi}], bug reading {bug_value} outside",
                   code="probe-separates")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verdict")
    v.add_argument("--output", required=True, help="saved unittest or pytest output of the mutation run")
    v.add_argument("--expect", required=True, help="file:line or test id of the intended assertion")
    v.add_argument("--unittest-layout", choices=UNITTEST_LAYOUTS, default=LAYOUT_MODERN,
                   help="the unittest header layout of the interpreter that ran the tests: 3.11+ (default) prints "
                        "`name (module.Class.name)`, legacy (CPython <= 3.10) `name (module.Class)`")
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
        status, reason = verdict(text, args.expect, unittest_layout=args.unittest_layout)
        reason += f" [unittest-layout={args.unittest_layout}]"  # r16: the saved verdict names the layout it assumed
    else:
        ok, reason = probe_discriminates(args.bug, args.fix, tuple(args.window))
        status = PASS if ok else REFUSED
    print(f"{status} {reason}")
    return 0 if status == PASS else 1


if __name__ == "__main__":
    sys.exit(main())
